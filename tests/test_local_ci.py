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
from types import SimpleNamespace
from unittest.mock import Mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test_ci.py"
spec = importlib.util.spec_from_file_location("local_ci", SCRIPT)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def reports():
    metadata = {"platform": "a5", "soc": "ascend950", "head_sha": "abc123", "run_id": "unique", "run_attempt": "1"}
    stages = {"schema": "npu-ci-stage-report-v1", "complete": True, "status": "success",
              "metadata": metadata, "stages": {s: {"status": "success", "exit_code": 0} for s in ci.STAGES}}
    stages["stages"]["atk-accuracy"] = {"status": "skipped", "exit_code": None,
                                       "reason": "no ATK operators selected"}
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

    def test_selected_atk_must_succeed(self):
        stages, accuracy, expected = reports()
        self.assertIn("stage did not succeed: atk-accuracy",
                      ci.validate_reports(stages, accuracy, "all", expected, "gdn"))
        stages["stages"]["atk-accuracy"] = {"status": "success", "exit_code": 0}
        self.assertEqual(ci.validate_reports(stages, accuracy, "all", expected, "gdn"), [])
        self.assertTrue(ci.validate_reports(stages, accuracy, "all", expected))

    def test_unselected_atk_needs_an_explicit_reason(self):
        stages, accuracy, expected = reports()
        stages["stages"]["atk-accuracy"]["reason"] = "prerequisite failed"
        self.assertTrue(ci.validate_reports(stages, accuracy, "all", expected))


class ArgumentsAndFetchTest(unittest.TestCase):
    def parse(self, *options):
        return ci.validate_args(ci.parser().parse_args(["--repo", "/source", *options]))

    def test_build_plan_normalizes_operators_and_requires_their_atk(self):
        args = self.parse("--build-kind", "incremental", "--build-ops", "gdn,cp,gdn",
                          "--atk-ops", "kda", "--device-id", "03", "--base-ref", "v0.6.0")
        self.assertEqual((args.build_kind, args.build_ops, args.atk_ops),
                         ("incremental", "cp,gdn", "cp,gdn,kda"))
        self.assertEqual((args.device_id, args.base_ref), ("3", "v0.6.0"))

    def test_full_mode_overrides_compilation_plan(self):
        args = self.parse("--mode", "full", "--build-kind", "incremental", "--build-ops", "gdn")
        self.assertEqual((args.build_kind, args.build_ops), ("full", ""))
        self.assertEqual(args.atk_ops, "gdn")

    def test_invalid_or_ambiguous_plans_are_rejected(self):
        for options in (("--build-kind", "incremental"), ("--build-kind", "base", "--build-ops", "gdn"),
                        ("--remote", "-origin"), ("--base-ref", "a/../b"), ("--fetch-timeout", "0")):
            with self.subTest(options=options), self.assertRaises((ci.Failure, SystemExit)):
                self.parse(*options)
        with self.assertRaises(SystemExit):
            self.parse("--ci-ref", "HEAD")

    def fetch_run(self, returncodes, no_proxy=False):
        run = object.__new__(ci.Run)
        run.args = SimpleNamespace(remote="upstream", no_proxy=no_proxy, fetch_timeout=300)
        run.original = Path("/source")
        run.env = {"https_proxy": "http://inherited.invalid:9000", "HTTP_PROXY": "http://inherited.invalid:9000"}
        run.meta = {}
        run.say = Mock()
        def load_proxy():
            run.env.update({"https_proxy": "http://127.0.0.1:7890", "http_proxy": "http://127.0.0.1:7890"})
        run.load_proxy = Mock(side_effect=load_proxy)
        run.command = Mock(side_effect=[subprocess.CompletedProcess([], rc, b"", b"fetch failed") for rc in returncodes])
        return run

    def test_fetch_success_never_enables_proxy(self):
        run = self.fetch_run([0])
        run.fetch_source("upstream", "refs/pull/7/head:refs/test/7")
        run.load_proxy.assert_not_called()
        arguments, keywords = run.command.call_args
        self.assertIn("http.proxy=", arguments[0])
        self.assertIn("remote.upstream.proxy=", arguments[0])
        self.assertNotIn("https_proxy", keywords["env"])
        self.assertNotIn("HTTP_PROXY", keywords["env"])
        self.assertEqual(keywords["env"]["NO_PROXY"], "*")
        self.assertEqual(keywords["timeout"], 300)

    def test_failed_direct_fetch_uses_proxy_once(self):
        run = self.fetch_run([1, 0])
        run.fetch_source("upstream", "refs/pull/7/head:refs/test/7")
        run.load_proxy.assert_called_once()
        self.assertEqual(run.meta["source_fetch_attempts"], [
            {"transport": "direct", "exit_code": 1}, {"transport": "proxy", "exit_code": 0}])
        arguments, keywords = run.command.call_args
        self.assertIn("http.proxy=http://127.0.0.1:7890", arguments[0])
        self.assertEqual(keywords["env"]["https_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(keywords["timeout"], 300)

    def test_direct_timeout_is_recorded_and_retried_through_proxy(self):
        run = self.fetch_run([])
        run.command.side_effect = [subprocess.TimeoutExpired(["git", "fetch"], 300),
                                   subprocess.CompletedProcess([], 0, b"", b"")]
        run.fetch_source("upstream", "HEAD")
        run.load_proxy.assert_called_once()
        self.assertEqual(run.meta["source_fetch_attempts"], [
            {"transport": "direct", "exit_code": 124, "timed_out": True, "timeout_seconds": 300},
            {"transport": "proxy", "exit_code": 0}])

    def test_proxy_timeout_fails_with_bounded_diagnostic(self):
        run = self.fetch_run([])
        run.command.side_effect = [subprocess.CompletedProcess([], 1, b"", b"failed"),
                                   subprocess.TimeoutExpired(["git", "fetch"], 300)]
        with self.assertRaisesRegex(ci.Failure, "proxy Git fetch timed out after 300 seconds"):
            run.fetch_source("upstream", "HEAD")
        self.assertEqual(run.meta["source_fetch_attempts"][-1]["exit_code"], 124)

    def test_real_child_process_timeout_is_recorded(self):
        run = object.__new__(ci.Run)
        run.env, run.commands = dict(os.environ), []
        with self.assertRaises(subprocess.TimeoutExpired):
            run.command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)
        self.assertTrue(run.commands[0]["timed_out"])
        self.assertEqual(run.commands[0]["exit_code"], 124)

    def test_proxy_failure_and_disabled_proxy_fail_explicitly(self):
        run = self.fetch_run([1, 1])
        with self.assertRaisesRegex(ci.Failure, "Git fetch failed through proxyon"):
            run.fetch_source("upstream", "HEAD")
        run = self.fetch_run([1], no_proxy=True)
        with self.assertRaisesRegex(ci.Failure, "--no-proxy disabled retry"):
            run.fetch_source("upstream", "HEAD")
        run.load_proxy.assert_not_called()


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
        self.ci_root.mkdir()
        (self.ci_root / "vpn-proxy.sh").write_text(
            "proxyon() { export http_proxy=http://fixture-proxy.invalid:7890; "
            "export https_proxy=$http_proxy HTTP_PROXY=$http_proxy HTTPS_PROXY=$http_proxy; }\n")
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.outside = self.root / "outside cwd"
        self.outside.mkdir()
        for folder in ("ci", "cmake/scripts", "tests/atk"):
            (self.repo / folder).mkdir(parents=True)
        (self.repo / "ci/run_checks.sh").write_text(
            "CI_BUILD_KIND=full\n# stage: atk-accuracy\n")
        (self.repo / "ci/prepare_ci_package.sh").write_text("#!/bin/bash\nexit 0\n")
        (self.repo / "tests/atk/run_test_cpu.sh").write_text("#!/bin/bash\nexit 0\n")
        (self.repo / "cmake/scripts/prepare.sh").write_text('CPU_NUM=${CMAKE_BUILD_PARALLEL_LEVEL:-8}\n')
        (self.repo / "ci/run_ci_container.sh").write_text("#!/bin/bash\nexec python3 ci/fixture.py\n")
        fixture = '''import json, os, pathlib, sys
stages = ("environment-contracts", "opp-package", "standalone-layout", "torch-adapter", "gdr-example-st", "atk-accuracy")
e = os.environ
expected_proxy = e.get("FIXTURE_EXPECT_PROXY", "http://fixture-proxy.invalid:7890")
assert e.get("https_proxy", "") == expected_proxy, "CI did not receive the selected proxy"
if not expected_proxy:
    assert not any(e.get(key) for key in ("http_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")), "--no-proxy leaked an inherited proxy"
meta = {k: e[v] for k,v in {"platform":"CI_ACCURACY_PLATFORM", "soc":"CI_SOC", "head_sha":"CI_ACCURACY_HEAD_SHA", "run_id":"CI_ACCURACY_RUN_ID", "run_attempt":"CI_ACCURACY_RUN_ATTEMPT"}.items()}
n = len(stages) if e["CI_STAGE"] == "all" else stages.index(e["CI_STAGE"]) + 1
report = {"schema":"npu-ci-stage-report-v1", "complete":True,"status":"success","metadata":meta,"stages":{s:{"status":"success" if i<n else "skipped","exit_code":0 if i<n else None} for i,s in enumerate(stages)}}
if n == len(stages) and not e.get("CI_ATK_OPS"):
    report["stages"]["atk-accuracy"] = {"status":"skipped","exit_code":None,"reason":"no ATK operators selected"}
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
        self.assertEqual(e["environment"]["CI_BUILD_KIND"], "full")
        self.assertNotIn("CI_RUN_SCOPED_WHEEL_INSTALL_CHECK", e["environment"])

    def test_incremental_plan_and_atk_are_forwarded(self):
        p, e, _ = self.invoke("--build-kind", "incremental", "--build-ops", "gdn", "--atk-ops", "kda",
                              "--device-id", "2", "--base-ref", "v0.6.0")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(e["environment"]["CI_BUILD_KIND"], "incremental")
        self.assertEqual(e["environment"]["CI_BUILD_OPS"], "gdn")
        self.assertEqual(e["environment"]["CI_ATK_OPS"], "gdn,kda")
        self.assertEqual(e["environment"]["CI_DEVICE_ID"], "2")
        self.assertEqual(e["environment"]["CI_BASE_REF"], "v0.6.0")

    def test_prepare_only_fetches_pr_without_docker_or_npu(self):
        self.git("update-ref", "refs/pull/7/head", "HEAD")
        p, e, _ = self.invoke("--pr", "7", "--remote", str(self.repo), "--prepare-only")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(e["status"], "success")
        self.assertFalse(e["full_ci_passed"])
        self.assertFalse(e["unmodified_commit_test"])
        self.assertTrue(e["source_preparation_only"])
        self.assertTrue(e["cleanup"]["isolated_worktree_removed"])
        self.assertEqual(e["source_fetch_attempts"], [{"transport": "direct", "exit_code": 0}])
        self.assertEqual(e["proxy"]["source"], "deferred until a direct fetch fails")
        self.assertFalse(any(item["argv"][0] in ("docker", "npu-smi") for item in e["commands"]))

    def test_direct_pr_fetch_still_loads_proxy_before_ci(self):
        self.git("update-ref", "refs/pull/7/head", "HEAD")
        p, e, _ = self.invoke("--pr", "7", "--remote", str(self.repo))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(e["source_fetch_attempts"], [{"transport": "direct", "exit_code": 0}])
        self.assertEqual(e["proxy"]["source"], "script")

    def test_ref_loads_explicit_proxy_for_dependency_downloads(self):
        proxy = self.root / "custom proxy.sh"
        proxy.write_text("proxyon() { export http_proxy=http://explicit.invalid:7891; "
                         "export https_proxy=$http_proxy; }\n")
        p, e, _ = self.invoke("--ref", "HEAD", "--proxy-script", str(proxy),
                              FIXTURE_EXPECT_PROXY="http://explicit.invalid:7891")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("source_fetch_attempts", e)
        self.assertEqual(e["proxy"]["script"], str(proxy))

    def test_no_proxy_clears_inherited_values_before_ci(self):
        p, e, _ = self.invoke("--no-proxy", FIXTURE_EXPECT_PROXY="",
                              https_proxy="http://inherited.invalid:8000", HTTP_PROXY="http://inherited.invalid:8000")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(e["proxy"]["source"], "disabled")

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
