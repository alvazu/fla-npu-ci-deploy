"""Evidence validation and a harmless end-to-end CI fixture (no NPU/container work)."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test_ci.py"
spec = importlib.util.spec_from_file_location("local_ci", SCRIPT)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def reports():
    metadata = {"platform": "a5", "soc": "ascend950", "head_sha": "abc123", "run_id": "unique", "run_attempt": "1"}
    stages = {"schema": "npu-ci-stage-report-v1", "complete": True, "status": "success",
              "metadata": metadata, "stages": {s: {"status": "success", "exit_code": 0} for s in ci.STAGES}}
    cases = [{"status": "passed", "return_code": 0, "accuracy_check": True, "accuracy_status": "passed"},
             {"status": "passed", "return_code": 0, "accuracy_check": False, "accuracy_status": "not_requested"}]
    accuracy = {"schema": "gdr-accuracy-report-v1", "complete": True, "metadata": metadata,
                "cases": cases, "summary": {"total": 2, "passed": 2, "failed": 0, "not_run": 0,
                "accuracy_total": 1, "accuracy_passed": 1, "accuracy_failed": 0, "accuracy_not_run": 0}}
    return stages, accuracy, metadata


class ReportsTest(unittest.TestCase):
    def test_success_is_not_hardcoded_to_eight_cases(self):
        stages, accuracy, expected = reports()
        self.assertEqual(ci.validate_reports(stages, accuracy, "all", expected), [])

    def test_finalize_success_does_not_hide_skipped_stage(self):
        stages, accuracy, expected = reports()
        stages["stages"]["opp-package"] = {"status": "skipped", "exit_code": None}
        self.assertTrue(ci.validate_reports(stages, accuracy, "all", expected))

    def test_partial_prefix_needs_no_accuracy_but_later_stages_skipped(self):
        stages, _, expected = reports()
        for stage in ci.STAGES[2:]:
            stages["stages"][stage] = {"status": "skipped", "exit_code": None}
        self.assertEqual(ci.validate_reports(stages, None, "opp-package", expected), [])
        self.assertTrue(ci.validate_reports(stages, None, "all", expected))

    def test_provenance_rejects_old_pass_reports(self):
        stages, accuracy, expected = reports()
        accuracy = copy.deepcopy(accuracy)
        accuracy["metadata"]["run_id"] = "previous-run"
        self.assertIn("accuracy report provenance mismatch: run_id", ci.validate_reports(stages, accuracy, "all", expected))

    def test_individual_case_failure_cannot_hide_in_pass_summary(self):
        stages, accuracy, expected = reports()
        accuracy["cases"][0]["return_code"] = 2
        self.assertTrue(ci.validate_reports(stages, accuracy, "all", expected))

    def test_precision_did_not_run_is_failure(self):
        stages, accuracy, expected = reports()
        accuracy["cases"][0]["accuracy_status"] = "not_run"
        self.assertTrue(ci.validate_reports(stages, accuracy, "all", expected))

    def test_malformed_nested_json_is_a_failure_not_an_exception(self):
        stages, accuracy, expected = reports()
        for key, value in (("metadata", []), ("stages", []), ("stages", {s: "oops" for s in ci.STAGES})):
            malformed = copy.deepcopy(stages)
            malformed[key] = value
            self.assertTrue(ci.validate_reports(malformed, accuracy, "all", expected))
        for key, value in (("metadata", None), ("summary", []), ("cases", [None]), ("cases", [])):
            malformed = copy.deepcopy(accuracy)
            malformed[key] = value
            self.assertTrue(ci.validate_reports(stages, malformed, "all", expected))

    def test_source_hash_changes_are_detected(self):
        before = {"head_sha": "a", "tracked_diff_sha256": "b", "untracked": [{"path": "x", "sha256": "c"}]}
        self.assertFalse(ci.source_changed(before, copy.deepcopy(before)))
        for key, replacement in (("head_sha", "new"), ("tracked_diff_sha256", "new"), ("untracked", [])):
            after = copy.deepcopy(before)
            after[key] = replacement
            self.assertTrue(ci.source_changed(before, after))

    def test_credentials_redacted_from_url_artifacts(self):
        self.assertNotIn("hunter2", ci.redact("https://user:hunter2@example.com/repo?token=hunter2"))


@unittest.skipUnless(platform.system() == "Linux" and hasattr(os, "getuid") and os.getuid() != 0
                     and shutil.which("git") and shutil.which("bash"), "CLI fixture runs on a non-root Linux host")
class CliFixtureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source with spaces"
        self.repo.mkdir()
        self.ci_root = self.root / "ci storage"
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.outside = self.root / "outside cwd"
        self.outside.mkdir()
        for folder in ("ci", "cmake/scripts"):
            (self.repo / folder).mkdir(parents=True)
        (self.repo / "ci/run_checks.sh").write_text(
            "export CMAKE_BUILD_PARALLEL_LEVEL=16\nexport MAX_JOBS=16\nexport FLA_NPU_BUILD_ARGS=-j16\n")
        (self.repo / "cmake/scripts/prepare.sh").write_text('CPU_NUM=${CMAKE_BUILD_PARALLEL_LEVEL:-8}\n')
        (self.repo / "ci/run_ci_container.sh").write_text("#!/bin/bash\nexec python3 ci/fixture.py\n")
        fixture = '''import json, os, pathlib, sys
stages = ("environment-contracts", "opp-package", "standalone-layout", "torch-adapter", "gdr-example-st", "scoped-overlay")
e = os.environ
meta = {k: e[v] for k,v in {"platform":"CI_ACCURACY_PLATFORM", "soc":"CI_SOC", "head_sha":"CI_ACCURACY_HEAD_SHA", "run_id":"CI_ACCURACY_RUN_ID", "run_attempt":"CI_ACCURACY_RUN_ATTEMPT"}.items()}
n = len(stages) if e["CI_STAGE"] == "all" else stages.index(e["CI_STAGE"]) + 1
report = {"schema":"npu-ci-stage-report-v1", "complete":True,"status":"success","metadata":meta,"stages":{s:{"status":"success" if i<n else "skipped","exit_code":0 if i<n else None} for i,s in enumerate(stages)}}
# Emulate the container's existing /tmp bind mount without starting Docker.
path = pathlib.Path(e["CI_HOST_TMPDIR"]) / "reports"
path.mkdir(exist_ok=True)
if e.get("FIXTURE_MISSING") != "1":
    (path / "stages.json").write_text(json.dumps(report))
accuracy = {"schema":"gdr-accuracy-report-v1", "complete":True,"metadata":meta,"cases":[{"status":"passed","return_code":0,"accuracy_check":True,"accuracy_status":"passed"}],"summary":{"total":1,"passed":1,"failed":0,"not_run":0,"accuracy_total":1,"accuracy_passed":1,"accuracy_failed":0,"accuracy_not_run":0}}
(path / "accuracy.json").write_text(json.dumps(accuracy))
if e.get("FIXTURE_MUTATE") == "1":
    pathlib.Path("source.txt").write_text("changed during CI")
print("fixture jobs=" + e["CI_JOBS"] + " scratch=" + e["TMPDIR"])
sys.exit(int(e.get("FIXTURE_EXIT", "0")))
'''
        (self.repo / "ci/fixture.py").write_text(fixture)
        (self.repo / "source.txt").write_text("original")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        docker = '''#!/bin/bash
case "$1 $2" in
"image inspect") echo sha256:fixture ;;
"version --format") echo fixture-client/server ;;
"container inspect") exit 1 ;;
esac
exit 0
'''
        for name, body in (("docker", docker), ("npu-smi", "#!/bin/bash\necho fixture-npu\n"), ("flock", "#!/bin/bash\nexit 0\n")):
            path = self.tools / name
            path.write_text(body)
            path.chmod(0o755)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def invoke(self, *args, **variables):
        env = dict(os.environ, PATH=str(self.tools) + os.pathsep + os.environ["PATH"], **variables)
        p = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo), "--ci-root", str(self.ci_root), *args],
                           cwd=self.outside, env=env, capture_output=True, text=True)
        result = sorted((self.ci_root / "results").iterdir())[-1]
        evidence = json.loads((result / "evidence.json").read_text())
        self.assertTrue((result / "SHA256SUMS").is_file(), p.stderr)
        self.assertTrue(evidence["cleanup"]["host_tmp_removed"])
        self.assertTrue(evidence["cleanup"]["host_scratch_removed"])
        return p, evidence, result

    def test_exact_directory_from_other_cwd_and_full_success(self):
        p, e, result = self.invoke()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(e["full_ci_passed"])
        self.assertTrue(e["unmodified_commit_test"])
        self.assertEqual(e["tested_source"]["path"], str(self.repo))
        self.assertEqual(e["environment"]["CI_JOBS"], "16")
        self.assertEqual(e["environment"]["CI_REQUIRE_HEALTHY_NPU"], "false")
        self.assertEqual(e["environment"]["CI_STAGE_REPORT_FILE"], "/tmp/fla-npu-ci/reports/stages.json")

    def test_nonzero_ci_cannot_be_overruled_by_success_reports(self):
        p, e, _ = self.invoke(FIXTURE_EXIT="7")
        self.assertEqual(p.returncode, 7)
        self.assertEqual(e["status"], "failure")
        self.assertFalse(e["full_ci_passed"])

    def test_missing_report_fails_even_when_command_returns_zero(self):
        p, e, _ = self.invoke(FIXTURE_MISSING="1")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(e["status"], "failure")

    def test_partial_run_does_not_claim_full_pass(self):
        p, e, _ = self.invoke("--stage", "environment-contracts")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(e["full_ci_passed"])

    def test_health_requirement_is_an_explicit_opt_in(self):
        p, e, _ = self.invoke("--stage", "environment-contracts", "--require-healthy-npu")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(e["environment"]["CI_REQUIRE_HEALTHY_NPU"], "true")

    def test_source_mutation_during_ci_invalidates_evidence(self):
        p, e, _ = self.invoke(FIXTURE_MUTATE="1")
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(e["source_changed_during_run"])
        self.assertFalse(e["unmodified_commit_test"])

    def test_existing_dirty_source_is_preserved_and_identified(self):
        (self.repo / "source.txt").write_text("local edit")
        (self.repo / "untracked.txt").write_text("untracked edit")
        p, e, result = self.invoke()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(e["tested_source"]["dirty"])
        self.assertFalse(e["unmodified_commit_test"])
        self.assertEqual((self.repo / "source.txt").read_text(), "local edit")
        self.assertIn("local edit", (result / "tested-source.patch").read_text())
        self.assertEqual(e["tested_source"]["untracked"][0]["sha256"], ci.digest(self.repo / "untracked.txt"))

    def test_ref_uses_isolation_and_cleans_only_its_worktree(self):
        (self.repo / "source.txt").write_text("local edit")
        p, e, _ = self.invoke("--ref", "HEAD")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotEqual(e["tested_source"]["path"], str(self.repo))
        self.assertTrue(e["cleanup"]["isolated_worktree_removed"])
        self.assertEqual((self.repo / "source.txt").read_text(), "local edit")


if __name__ == "__main__":
    unittest.main()
