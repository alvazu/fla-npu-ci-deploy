#!/usr/bin/env python3
"""Run the repository's NPU CI and retain verifiable local evidence (no sudo)."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import uuid

STAGES = ("environment-contracts", "opp-package", "standalone-layout",
          "torch-adapter", "gdr-example-st", "atk-accuracy")
BUILD_KINDS = ("full", "incremental", "torch", "base")
PLATFORMS = {"a2": ("ascend910b", "fla-npu-ci:9.1.0-910b", "ci/Dockerfile"),
             "a5": ("ascend950", "fla-npu-ci:9.1.0-950", "ci/Dockerfile.ascend950")}
PROXY_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY",
              "no_proxy", "NO_PROXY", "GIT_HTTP_PROXY", "GIT_HTTPS_PROXY", "PROXY_HTTP_PORT",
              "PROXY_SOCKS_PORT", "PROXY_MIXED_PORT")


class Failure(RuntimeError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def redact(text):
    # Do not copy credential-bearing remote/proxy URLs into artifacts.
    text = re.sub(r"(https?|socks5h?)://[^/\s@]+@", r"\1://[redacted]@", text)
    return re.sub(r"(?i)([?&](?:token|password|secret|key)=)[^&\s]+", r"\1[redacted]", text)


def safe_path(path):
    path = Path(path).expanduser().resolve()
    if any(c in str(path) for c in (":", "\n", "\r", "\x00")):
        raise Failure("Docker bind paths must not contain colons or line breaks")
    return path


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_reports(stage_report, accuracy_report, requested, expected, atk_ops=""):
    """Validate provenance and the selected stage prefix, rather than exit code alone."""
    errors = []
    count = len(STAGES) if requested == "all" else STAGES.index(requested) + 1
    if not isinstance(stage_report, dict):
        return ["missing or invalid stage report"]
    if stage_report.get("schema") != "npu-ci-stage-report-v1":
        errors.append("unsupported stage report schema")
    if stage_report.get("complete") is not True or stage_report.get("status") != "success":
        errors.append("stage report is incomplete or unsuccessful")
    stage_metadata = stage_report.get("metadata")
    stage_metadata = stage_metadata if isinstance(stage_metadata, dict) else {}
    stages = stage_report.get("stages")
    stages = stages if isinstance(stages, dict) else {}
    for key, value in expected.items():
        if stage_metadata.get(key) != value:
            errors.append("stage report provenance mismatch: " + key)
    for name in STAGES[:count]:
        entry = stages.get(name, {})
        entry = entry if isinstance(entry, dict) else {}
        if name == "atk-accuracy" and not atk_ops:
            if entry.get("status") != "skipped" or entry.get("reason") != "no ATK operators selected":
                errors.append("ATK without selected operators must report the documented skip reason")
            continue
        if entry.get("status") != "success" or entry.get("exit_code") != 0:
            errors.append("stage did not succeed: " + name)
    # CI_STAGE selects a prefix. Later stages must not accidentally be reported as tested.
    for name in STAGES[count:]:
        entry = stages.get(name, {})
        if not isinstance(entry, dict) or entry.get("status") != "skipped":
            errors.append("unselected stage was not skipped: " + name)
    if count < STAGES.index("gdr-example-st") + 1:
        return errors
    if not isinstance(accuracy_report, dict):
        return errors + ["missing or invalid accuracy report"]
    if accuracy_report.get("schema") != "gdr-accuracy-report-v1" or accuracy_report.get("complete") is not True:
        errors.append("accuracy report is incomplete or has an unsupported schema")
    accuracy_metadata = accuracy_report.get("metadata")
    accuracy_metadata = accuracy_metadata if isinstance(accuracy_metadata, dict) else {}
    for key, value in expected.items():
        if accuracy_metadata.get(key) != value:
            errors.append("accuracy report provenance mismatch: " + key)
    summary = accuracy_report.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    cases = accuracy_report.get("cases")
    if not isinstance(cases, list) or not cases:
        return errors + ["accuracy report has no cases"]
    total = summary.get("total")
    if type(total) is not int or total <= 0 or total != len(cases):
        errors.append("accuracy case total is invalid")
    if summary.get("passed") != total or summary.get("failed") != 0 or summary.get("not_run") != 0:
        errors.append("accuracy report contains failed or unexecuted cases")
    accuracy_cases = [c for c in cases if isinstance(c, dict) and
                      (c.get("accuracy_check") or c.get("accuracy_status") == "not_run")]
    if not accuracy_cases or summary.get("accuracy_total") != len(accuracy_cases):
        errors.append("accuracy report has no verified precision cases or an invalid total")
    if (summary.get("accuracy_passed") != len(accuracy_cases) or
            summary.get("accuracy_failed") != 0 or summary.get("accuracy_not_run") != 0):
        errors.append("precision checks did not all pass")
    for case in cases:
        if not isinstance(case, dict) or case.get("status") != "passed" or case.get("return_code") != 0:
            errors.append("individual case did not pass")
    if any(c.get("accuracy_status") != "passed" for c in accuracy_cases):
        errors.append("individual precision check did not pass")
    return errors


def source_changed(before, after):
    return any(before.get(key) != after.get(key)
               for key in ("head_sha", "tracked_diff_sha256", "untracked"))


class Run:
    def __init__(self, args):
        self.args = args
        self.run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
        self.root = safe_path(args.ci_root)
        self.result = self.root / "results" / self.run_id
        self.result.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.log = (self.result / "runner.log").open("w", encoding="utf-8", buffering=1)
        self.commands = []
        self.env = dict(os.environ)
        # Deployment credentials are unrelated to source validation.
        for key in ("RUNNER_TOKEN", "FLA_CI_SUBSCRIPTION_URL"):
            self.env.pop(key, None)
        if args.no_proxy:
            self.env = {key: value for key, value in self.env.items() if key not in PROXY_KEYS}
        self.errors = []
        self.repo = None
        self.original = None
        self.worktree = None
        self.fetch_ref = None
        self.child = None
        self.container = "fla-local-ci-" + self.run_id.lower()
        self.host_tmp = self.root / "tmp" / ("container-" + self.run_id)
        self.host_scratch = self.root / "tmp" / ("host-" + self.run_id)
        self.started_ci = False
        self.source_lock = None
        self.meta = {"schema": "fla-local-ci-evidence-v1", "run_id": self.run_id,
                     "started_at": now(), "status": "failure", "full_ci_passed": False,
                     "requested_stage": args.stage, "mode": args.mode,
                     "source_preparation_only": args.prepare_only,
                     "build_plan": {"kind": args.build_kind, "operators": args.build_ops,
                                    "atk_operators": args.atk_ops, "base_ref": args.base_ref,
                                    "device_id": args.device_id},
                     "platform": args.platform, "soc": PLATFORMS[args.platform][0],
                     "host_arch": platform.machine(), "host_user_uid": os.getuid(),
                     "result_directory": str(self.result), "container_name": self.container,
                     "ci_exit_code": None, "errors": self.errors, "commands": self.commands}
        self.meta["verifier"] = {"script_sha256": digest(__file__), "python": sys.version.split()[0]}
        self.meta["proxy"] = {"source": "disabled" if args.no_proxy else "deferred until a direct fetch fails"}

    def say(self, text):
        text = redact(text)
        print(text, flush=True)
        self.log.write(text + "\n")

    def command(self, argv, cwd=None, check=True, binary=False, env=None, timeout=None):
        argv = [str(x) for x in argv]
        item = {"argv": [redact(x) for x in argv], "cwd": str(cwd or Path.cwd())}
        self.commands.append(item)
        options = {"cwd": cwd, "env": self.env if env is None else env,
                   "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
        if timeout is None:
            p = subprocess.run(argv, **options)
        else:
            # A fetch can spawn git-remote-https. Kill its process group on a
            # timeout so an orphan cannot keep pipes or repository locks open.
            with subprocess.Popen(argv, start_new_session=os.name == "posix", **options) as child:
                try:
                    stdout, stderr = child.communicate(timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    if os.name == "posix":
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        child.kill()
                    exc.output, exc.stderr = child.communicate()
                    item.update({"exit_code": 124, "timed_out": True, "timeout_seconds": timeout})
                    raise
                p = subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
        item["exit_code"] = p.returncode
        if check and p.returncode:
            raise Failure("command failed: " + shlex.join(argv) + "\n" + redact(p.stderr.decode(errors="replace")))
        return p if binary else p.stdout.decode("utf-8", errors="replace").strip()

    def load_proxy(self):
        if self.args.no_proxy:
            for key in PROXY_KEYS:
                self.env.pop(key, None)
            self.meta["proxy"] = {"source": "disabled"}
            return
        path = Path(self.args.proxy_script).expanduser().resolve() if self.args.proxy_script else self.root / "vpn-proxy.sh"
        if not path.is_file():
            raise Failure("proxy script is missing: " + str(path) +
                          "; deploy clashctl/proxyon or pass --proxy-script")
        code = "import json,os; print(json.dumps({k:os.environ.get(k,'') for k in " + repr(PROXY_KEYS) + "}))"
        shell = 'set -euo pipefail; source "$1" >/dev/null; proxyon >/dev/null; "$2" -c "$3"'
        raw = self.command(["bash", "-c", shell, "local-ci-proxy", path, sys.executable, code])
        try:
            values = json.loads(raw)
        except ValueError as exc:
            raise Failure("proxy script did not return a valid environment") from exc
        if not isinstance(values, dict) or set(values) != set(PROXY_KEYS):
            raise Failure("proxy script did not return exactly the allowed proxy variables")
        for key, value in values.items():
            if not isinstance(value, str) or any(c in value for c in ("\r", "\n", "\x00")):
                raise Failure("proxy variable contains an invalid value: " + key)
            self.env[key] = value
        if not (values.get("https_proxy") or values.get("http_proxy")):
            raise Failure("proxyon did not export an HTTP(S) proxy; repair vpn-proxy.sh or use --no-proxy")
        self.meta["proxy"] = {"source": "script", "script": str(path), "script_sha256": digest(path),
                              "exported_variables": sorted(k for k, v in values.items() if v)}

    def fetch_source(self, *arguments):
        """Every source fetch starts without inherited or configured Git proxies."""
        def attempt(argv, env, transport):
            record = {"transport": transport}
            try:
                result = self.command(argv, env=env, check=False, binary=True, timeout=self.args.fetch_timeout)
            except subprocess.TimeoutExpired:
                message = "%s Git fetch timed out after %s seconds" % (transport, self.args.fetch_timeout)
                self.say("[local-ci] " + message)
                record.update({"timed_out": True, "timeout_seconds": self.args.fetch_timeout})
                result = subprocess.CompletedProcess(argv, 124, b"", message.encode("utf-8"))
            record["exit_code"] = result.returncode
            self.meta.setdefault("source_fetch_attempts", []).append(record)
            return result

        direct_env = {key: value for key, value in self.env.items() if key not in PROXY_KEYS}
        direct_env.update({"no_proxy": "*", "NO_PROXY": "*", "GIT_TERMINAL_PROMPT": "0"})
        configs = ["-c", "http.proxy=", "-c", "https.proxy=", "-c", "core.gitProxy="]
        if re.fullmatch(r"[A-Za-z0-9_.-]+", self.args.remote):
            configs += ["-c", "remote." + self.args.remote + ".proxy="]
        command = ["git", "-C", self.original, *configs, "fetch", "--no-tags", "--", *arguments]
        self.say("[local-ci] fetching source directly (proxy disabled)")
        result = attempt(command, direct_env, "direct")
        if not result.returncode:
            return
        if self.args.no_proxy:
            raise Failure("direct Git fetch failed and --no-proxy disabled retry: " +
                          redact(result.stderr.decode(errors="replace")))
        self.say("[local-ci] direct Git fetch failed; enabling proxyon and retrying")
        self.load_proxy()
        proxy_env = dict(self.env, GIT_TERMINAL_PROMPT="0")
        proxy_url = self.env.get("https_proxy") or self.env.get("http_proxy")
        if not proxy_url:
            raise Failure("proxyon did not export an HTTP(S) proxy; repair vpn-proxy.sh")
        retry_configs = ["-c", "http.proxy=" + proxy_url, "-c", "https.proxy=" + proxy_url]
        if re.fullmatch(r"[A-Za-z0-9_.-]+", self.args.remote):
            retry_configs += ["-c", "remote." + self.args.remote + ".proxy=" + proxy_url]
        retry = attempt(["git", "-C", self.original, *retry_configs,
                         "fetch", "--no-tags", "--", *arguments], proxy_env, "proxy")
        if retry.returncode:
            raise Failure("Git fetch failed through proxyon after the direct attempt; check proxy health, "
                          "remote access and the PR number: " + redact(retry.stderr.decode(errors="replace")))

    def git(self, *argv, **kwargs):
        return self.command(["git", "-C", self.original, *argv], **kwargs)

    def snapshot(self, repo, prefix):
        def git(*args, **kw):
            return self.command(["git", "-C", repo, *args], **kw)
        head = git("rev-parse", "HEAD")
        status = git("status", "--porcelain=v1", "-z", "--untracked-files=all", binary=True).stdout
        patch = git("diff", "--binary", "HEAD", "--", binary=True).stdout
        (self.result / (prefix + ".patch")).write_bytes(patch)
        paths = git("ls-files", "--others", "--exclude-standard", "-z", binary=True).stdout.split(b"\0")
        untracked = []
        for raw in paths:
            if not raw:
                continue
            name = os.fsdecode(raw)
            path = repo / name
            # Git paths are relative; never read a symlink target outside the checkout.
            if path.is_symlink():
                target = os.readlink(path)
                untracked.append({"path": name, "symlink": target,
                                  "sha256": hashlib.sha256(os.fsencode(target)).hexdigest()})
            elif path.is_file():
                untracked.append({"path": name, "size": path.stat().st_size, "sha256": digest(path)})
            else:
                raise Failure("cannot hash untracked file: " + name)
        write_json(self.result / (prefix + "-untracked.json"), untracked)
        branch = git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        remotes = redact(git("remote", "-v"))
        base = None
        if self.args.base:
            base = {"ref": self.args.base, "sha": git("rev-parse", self.args.base + "^{commit}"),
                    "merge_base": git("merge-base", "HEAD", self.args.base)}
        return {"path": str(repo), "head_sha": head, "branch": branch or "(detached)",
                "dirty": bool(status), "tracked_diff_sha256": hashlib.sha256(patch).hexdigest(),
                "status_porcelain": os.fsdecode(status).split("\0"),
                "untracked": untracked, "remotes": remotes.splitlines(), "base": base}

    def prepare_source(self):
        self.original = safe_path(self.args.repo)
        if not self.original.is_dir():
            raise Failure("--repo is not a directory")
        top = safe_path(self.git("rev-parse", "--show-toplevel"))
        if top != self.original:
            raise Failure("--repo must name the Git repository root")
        if self.root == top or top in self.root.parents:
            raise Failure("--ci-root must be outside the source repository")
        if top.stat().st_uid != os.getuid() and not self.args.prepare_only:
            raise Failure("the source directory must belong to the invoking non-root user")
        self.meta["input_source"] = self.snapshot(self.original, "input-source")
        needs_worktree = bool(self.args.pr or self.args.ref)
        if needs_worktree:
            target = self.args.ref or "HEAD"
            if self.args.pr:
                self.fetch_ref = "refs/fla-local-ci/" + self.run_id
                self.fetch_source(self.args.remote,
                                  "refs/pull/%d/head:%s" % (self.args.pr, self.fetch_ref))
                target = self.fetch_ref
            sha = self.git("rev-parse", target + "^{commit}")
            self.worktree = self.root / "tmp" / ("source-" + self.run_id)
            self.git("worktree", "add", "--detach", self.worktree, sha)
            self.repo = self.worktree
            self.meta["selection"] = {"pr": self.args.pr, "ref": self.args.ref,
                                      "remote": self.args.remote, "resolved_sha": sha}
        else:
            self.repo = self.original
            self.meta["selection"] = {"kind": "existing checkout, including its local changes"}
        self.source_lock_file()
        self.meta["tested_source"] = self.snapshot(self.repo, "tested-source")
        required = ("ci/run_ci_container.sh", "ci/run_checks.sh", "ci/prepare_ci_package.sh")
        for path in required:
            if not (self.repo / path).is_file():
                raise Failure("selected source is missing " + path +
                              "; use a revision containing the CI changes from PR #1041")
        checks = (self.repo / "ci/run_checks.sh").read_text()
        if "CI_BUILD_KIND" not in checks or "atk-accuracy" not in checks:
            raise Failure("selected source does not support the build-plan/ATK CI pipeline; "
                          "use a revision containing PR #1041; CI always comes from the selected source")
        if self.args.atk_ops and not (self.repo / "tests/atk/run_test_cpu.sh").is_file():
            raise Failure("selected source is missing the ATK entry tests/atk/run_test_cpu.sh")

    def source_lock_file(self):
        import fcntl
        locks = self.root / "tmp" / "source-locks"
        locks.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(os.fsencode(self.repo)).hexdigest() + ".lock"
        self.source_lock = (locks / name).open("a")
        try:
            fcntl.flock(self.source_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Failure("another local CI uses this checkout; use --ref for an isolated worktree") from exc

    def restore_permissions(self):
        targets = [(self.repo, "/ci-repo"), (self.host_tmp, "/ci-tmp")]
        command = ["docker", "run", "--rm", "--network", "none", "--entrypoint", "bash"]
        for host, container in targets:
            command += ["-v", str(host) + ":" + container]
        command += [self.args.image, "-c", 'chown -Rh -- "$1:$2" /ci-repo /ci-tmp',
                    "--", str(os.getuid()), str(os.getgid())]
        self.command(command)

    def run_ci(self):
        # Source retrieval tries direct access first. Package/dependency downloads
        # inside the real CI need the deployed proxy even if no fetch failed.
        self.load_proxy()
        for tool in ("git", "docker", "bash", "flock", "npu-smi"):
            if shutil.which(tool) is None:
                raise Failure("required host executable is missing: " + tool)
        self.meta["tools"] = {"python": sys.version.split()[0], "git": self.command(["git", "--version"]),
                              "docker": self.command(["docker", "version", "--format", "{{.Client.Version}} / {{.Server.Version}}"]),
                              "bash": self.command(["bash", "--version"]).splitlines()[0],
                              "npu_smi": self.command(["npu-smi", "info"], check=False)}
        self.command(["docker", "info"])
        image = self.command(["docker", "image", "inspect", self.args.image,
                              "--format", "{{.Id}}"], check=False)
        if not image and not self.args.build_image:
            raise Failure("required preloaded image is missing: " + self.args.image + "; deploy it first or pass --build-image")
        self.meta["image"] = {"name": self.args.image, "id_before": image or None,
                              "dockerfile": self.args.dockerfile, "build_allowed": self.args.build_image}
        self.host_tmp.mkdir(parents=True, exist_ok=False)
        self.host_scratch.mkdir(parents=True, exist_ok=False)
        (self.root / "cache_root").mkdir(parents=True, exist_ok=True)
        (self.root / "tmp" / "npu-locks").mkdir(parents=True, exist_ok=True)
        if image:
            self.restore_permissions()
        report_dir = self.host_tmp / "reports"
        report_dir.mkdir(parents=True, exist_ok=False)
        self.report_dir = report_dir
        soc = PLATFORMS[self.args.platform][0]
        controlled = {"CI_MODE": self.args.mode, "CI_STAGE": self.args.stage, "CI_SOC": soc, "FLA_NPU_SOC": soc,
                      "CI_BUILD_KIND": self.args.build_kind, "CI_BUILD_OPS": self.args.build_ops,
                      "CI_ATK_OPS": self.args.atk_ops, "CI_BASE_REF": self.args.base_ref,
                      "CI_DEVICE_ID": self.args.device_id,
                      "CI_IMAGE": self.args.image, "CI_DOCKERFILE": self.args.dockerfile,
                      "CI_REQUIRE_PRELOADED_IMAGE": "false" if self.args.build_image else "true",
                      "CI_JOBS": "16", "CI_CPACK_JOBS": "16", "CMAKE_BUILD_PARALLEL_LEVEL": "16",
                      "MAKEFLAGS": "-j16", "MAX_JOBS": "16", "FLA_NPU_BUILD_ARGS": "-j16",
                      "CI_CONTAINER_NAME": self.container, "CI_TMPDIR": "/tmp/fla-npu-ci",
                      "CI_HOST_TMPDIR": str(self.host_tmp), "TMPDIR": str(self.host_scratch),
                      "CI_CACHE_ROOT": str(self.root / "cache_root"),
                      "CI_NPU_LOCK_DIR": str(self.root / "tmp" / "npu-locks"),
                      "CI_RUN_STANDALONE_WHEEL_LAYOUT_CHECK": "true",
                      "CI_RUN_EXAMPLE_ST": "true",
                      "CI_REQUIRE_HEALTHY_NPU": str(self.args.require_healthy_npu).lower(),
                      "CI_LOG_CLEANUP_ENABLED": "false",
                      "CI_STAGE_REPORT_FILE": "/tmp/fla-npu-ci/reports/stages.json",
                      "CI_ACCURACY_REPORT_FILE": "/tmp/fla-npu-ci/reports/accuracy.json",
                      "CI_ACCURACY_PLATFORM": self.args.platform,
                      "CI_ACCURACY_HEAD_SHA": self.meta["tested_source"]["head_sha"],
                      "CI_ACCURACY_RUN_ID": self.run_id, "CI_ACCURACY_RUN_ATTEMPT": "1"}
        write_json(self.result / "build-environment.json", controlled)
        self.meta["environment"] = controlled
        env = {k: v for k, v in self.env.items() if not k.startswith(("CI_", "FLA_NPU_", "NPU_", "GITHUB_", "GDR_"))}
        env.update(controlled)
        command = ["bash", "ci/run_ci_container.sh"]
        self.commands.append({"argv": command, "cwd": str(self.repo), "environment_file": "build-environment.json"})
        self.say("[local-ci] " + shlex.join(command) + " (cwd=" + str(self.repo) + ")")
        self.started_ci = True
        with (self.result / "ci.log").open("w", encoding="utf-8", buffering=1) as log:
            self.child = subprocess.Popen(command, cwd=self.repo, env=env, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
            for line in self.child.stdout:
                line = redact(line)
                selected = re.match(r"^\[CI\] Running .* on NPU (\d+) \((.*), health=([^,]*), free=([^)]*)\)", line)
                if selected:
                    self.meta["selected_npu"] = dict(zip(("device", "name", "health", "free"), selected.groups()))
                log.write(line)
                print(line, end="", flush=True)
            self.meta["ci_exit_code"] = self.child.wait()
            self.commands[-1]["exit_code"] = self.meta["ci_exit_code"]
        self.child = None
        if self.meta["ci_exit_code"]:
            self.errors.append("CI command exited with code " + str(self.meta["ci_exit_code"]))

    def cleanup(self):
        if self.started_ci:
            try:
                # Stop container work while the shell still holds its NPU flock.
                # Killing the Docker client first can leave a daemon-side orphan.
                inspected = self.command(["docker", "container", "inspect", self.container], check=False)
                if inspected:
                    self.command(["docker", "rm", "-f", self.container])
                else:
                    self.command(["docker", "info"])
            except Exception as exc:
                self.errors.append("container cleanup failed: " + str(exc))
        if self.child and self.child.poll() is None:
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
                self.child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait()
            except ProcessLookupError:
                pass
        if self.started_ci:
            try:
                self.restore_permissions()
                self.meta["image"]["id_after"] = self.command(
                    ["docker", "image", "inspect", self.args.image, "--format", "{{.Id}}"])
            except Exception as exc:
                self.errors.append("permission cleanup failed: " + str(exc))
        if hasattr(self, "report_dir"):
            reports = {}
            for name in ("stages", "accuracy"):
                path = self.report_dir / (name + ".json")
                if path.is_file() and not path.is_symlink():
                    try:
                        shutil.copy2(path, self.result / path.name)
                        reports[name] = json.loads(path.read_text())
                    except (OSError, ValueError) as exc:
                        self.errors.append("invalid " + name + " report: " + str(exc))
            expected = {"platform": self.args.platform, "soc": PLATFORMS[self.args.platform][0],
                        "head_sha": self.meta["tested_source"]["head_sha"], "run_id": self.run_id,
                        "run_attempt": "1"}
            self.errors.extend(validate_reports(reports.get("stages"), reports.get("accuracy"), self.args.stage,
                                                expected, self.args.atk_ops))
            stages = reports.get("stages")
            accuracy = reports.get("accuracy")
            self.meta["stage_results"] = stages.get("stages", {}) if isinstance(stages, dict) else {}
            self.meta["case_summary"] = accuracy.get("summary") if isinstance(accuracy, dict) else None
            atk_results = self.repo / ".ci-tmp" / "atk-results"
            if atk_results.is_dir():
                try:
                    if atk_results.is_symlink() or any(path.is_symlink() for path in atk_results.rglob("*")):
                        raise Failure("ATK result directory contains a symlink")
                    shutil.copytree(atk_results, self.result / "atk-results")
                    self.meta["atk_results"] = "atk-results"
                except (OSError, Failure) as exc:
                    self.errors.append("ATK artifact capture failed: " + str(exc))
        for path in (self.host_tmp, self.host_scratch):
            try:
                if path.exists():
                    if path.is_symlink() or path.parent != self.root / "tmp" or not path.name.endswith(self.run_id):
                        raise Failure("refusing unsafe cleanup path: " + str(path))
                    shutil.rmtree(path)
            except Exception as exc:
                self.errors.append("temporary directory cleanup failed: " + str(exc))
        self.meta["cleanup"] = {"host_tmp_removed": not self.host_tmp.exists(),
                                "host_scratch_removed": not self.host_scratch.exists(),
                                "shared_cache_and_npu_locks_preserved": True}
        if self.repo:
            try:
                self.meta["source_after"] = self.snapshot(self.repo, "source-after")
                before = self.meta.get("tested_source")
                if before:
                    changed = source_changed(before, self.meta["source_after"])
                    self.meta["source_changed_during_run"] = changed
                    if changed:
                        self.errors.append("source HEAD, tracked changes or non-ignored untracked files changed during the run")
            except Exception as exc:
                self.errors.append("post-run source capture failed: " + str(exc))
        if self.worktree:
            try:
                self.git("worktree", "remove", "--force", self.worktree)
                self.meta["cleanup"]["isolated_worktree_removed"] = True
            except Exception as exc:
                self.errors.append("isolated worktree cleanup failed: " + str(exc))
        if self.fetch_ref:
            try:
                self.git("update-ref", "-d", self.fetch_ref)
            except Exception as exc:
                self.errors.append("temporary fetch ref cleanup failed: " + str(exc))
        if self.source_lock:
            self.source_lock.close()

    def finish(self):
        self.meta["finished_at"] = now()
        if not self.errors and self.meta["ci_exit_code"] == 0:
            self.meta["status"] = "success"
            self.meta["full_ci_passed"] = self.args.stage == "all" and not self.args.prepare_only
        tested = self.meta.get("tested_source", {})
        self.meta["unmodified_commit_test"] = bool(tested and not tested.get("dirty") and not self.args.prepare_only
                                                  and self.meta.get("source_changed_during_run") is False)
        self.say("[local-ci] " + self.meta["status"] + "; evidence: " + str(self.result))
        write_json(self.result / "evidence.json", self.meta)
        lines = ["# Local NPU CI evidence", "", "- Result: **" + self.meta["status"] + "**",
                 "- Full six-stage CI passed: " + str(self.meta["full_ci_passed"]),
                 "- Source HEAD: `" + tested.get("head_sha", "unavailable") + "`",
                 "- Dirty source: " + str(tested.get("dirty", "unknown")),
                 "- Unmodified commit tested: " + str(self.meta["unmodified_commit_test"]),
                 "- Platform / SOC: " + self.args.platform + " / " + PLATFORMS[self.args.platform][0],
                 "- Requested stage / mode: " + self.args.stage + " / " + self.args.mode,
                 "- Source preparation only: " + str(self.args.prepare_only),
                 "- Build / build operators / ATK operators: " + self.args.build_kind + " / " +
                    (self.args.build_ops or "none") + " / " + (self.args.atk_ops or "none"),
                 "- Base package branch / requested physical NPU: " + self.args.base_ref + " / " +
                    (self.args.device_id or "automatic"),
                 "- Require healthy NPU: " + str(self.args.require_healthy_npu),
                 "- Selected NPU health: " + self.meta.get("selected_npu", {}).get("health", "not selected; see ci.log / host npu-smi evidence"),
                 "- CI exit code: " + str(self.meta["ci_exit_code"]), "",
                 "A local result records this machine, image, configuration and source snapshot. "
                 "A source preparation, partial stage run or dirty source is not proof that the unmodified PR passed full CI.", ""]
        if self.errors:
            lines += ["Failures:", ""] + ["- " + redact(e).replace("\n", " ") for e in self.errors] + [""]
        lines += ["Artifacts: `ci.log`, `runner.log`, `stages.json`, `accuracy.json` (when required), "
                  "`build-environment.json`, source patches/untracked hashes, `evidence.json`, `SHA256SUMS`."]
        (self.result / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.log.close()
        sums = [digest(p) + "  " + p.relative_to(self.result).as_posix()
                for p in sorted(self.result.rglob("*")) if p.is_file() and p.name != "SHA256SUMS"]
        (self.result / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
        return 0 if self.meta["status"] == "success" else (self.meta["ci_exit_code"] or 1)


def operator_list(value):
    if not value:
        return ""
    values = value.split(",")
    if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", item.strip()) for item in values):
        raise argparse.ArgumentTypeError("operators must be comma-separated names (letters, digits, _, ., -)")
    return ",".join(sorted(set(item.strip() for item in values)))


def atk_operator_list(value):
    if value.strip() == "null":
        return "null"
    operators = operator_list(value)
    if "null" in operators.split(","):
        raise argparse.ArgumentTypeError("null disables ATK and cannot be combined with operators")
    return operators


def device_id(value):
    if not value:
        return ""
    if not re.fullmatch(r"[0-9]+", value):
        raise argparse.ArgumentTypeError("device ID must be a nonnegative physical NPU ID")
    return str(int(value))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", required=True, help="absolute or relative path to an existing Git checkout")
    p.add_argument("--platform", choices=PLATFORMS, default="a2" if platform.machine().lower() in ("aarch64", "arm64") else "a5")
    p.add_argument("--ci-root", default="/home/huangjunzhe/ci")
    p.add_argument("--stage", choices=("all",) + STAGES, default="all", help="CI prefix ending at this stage")
    p.add_argument("--mode", choices=("quick", "full"), default="quick")
    p.add_argument("--build-kind", choices=BUILD_KINDS, default="full",
                   help="explicit package plan; full mode always forces a full build")
    p.add_argument("--build-ops", type=operator_list, default="", help="comma-separated incremental build operators")
    p.add_argument("--atk-ops", type=atk_operator_list, default="",
                   help="comma-separated ATK accuracy operators; null disables all ATK validation")
    p.add_argument("--device-id", type=device_id, default="", help="physical NPU ID; omitted means automatic selection")
    p.add_argument("--base-ref", default="main", help="Daily base package branch, such as main or v0.6.0")
    p.add_argument("--prepare-only", action="store_true", help="prepare source and retain evidence without Docker/NPU work")
    p.add_argument("--image")
    p.add_argument("--dockerfile")
    p.add_argument("--build-image", action="store_true", help="allow CI to build the image if missing")
    p.add_argument("--require-healthy-npu", action="store_true",
                   help="reject a selected NPU unless health=OK; default follows upstream CI (false)")
    proxy = p.add_mutually_exclusive_group()
    proxy.add_argument("--proxy-script", help="proxyon script for failed direct fetches and CI dependency downloads; default: <ci-root>/vpn-proxy.sh")
    proxy.add_argument("--no-proxy", action="store_true", help="disable fetch retry and remove inherited proxy variables")
    selection = p.add_mutually_exclusive_group()
    selection.add_argument("--pr", type=int, help="fetch PR head from --remote into an isolated worktree")
    selection.add_argument("--ref", help="existing Git ref to test in an isolated detached worktree")
    p.add_argument("--remote", default="upstream")
    p.add_argument("--fetch-timeout", type=int, default=300,
                   help="timeout in seconds per direct/proxy Git fetch attempt (default: 300); CI builds are unaffected")
    p.add_argument("--base", help="existing comparison ref; record its SHA and merge-base (does not merge)")
    return p


def validate_args(args):
    build_atk_ops = args.build_ops
    if args.pr is not None and args.pr < 1:
        raise Failure("--pr must be positive")
    if args.fetch_timeout <= 0:
        raise Failure("--fetch-timeout must be positive")
    if args.mode == "full":
        args.build_kind, args.build_ops = "full", ""
    elif args.build_kind == "incremental" and not args.build_ops:
        raise Failure("--build-kind incremental requires --build-ops")
    elif args.build_kind != "incremental" and args.build_ops:
        raise Failure("--build-ops is only valid with --build-kind incremental")
    # Explicit null overrides the automatically selected build operators, as in GitHub CI.
    args.atk_ops = "" if args.atk_ops == "null" else operator_list(",".join(filter(None, (args.atk_ops, build_atk_ops))))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", args.base_ref) or ".." in args.base_ref:
        raise Failure("--base-ref must be a branch name without whitespace or '..'")
    if not args.remote or args.remote.startswith("-") or any(c in args.remote for c in ("\r", "\n", "\x00")):
        raise Failure("--remote must be a Git remote name or URL")
    return args


def main(argv=None):
    try:
        args = validate_args(parser().parse_args(argv))
    except Failure as exc:
        parser().error(str(exc))
    if os.name != "posix" or platform.system() != "Linux":
        parser().error("run this script on the Linux NPU host")
    if os.getuid() == 0 and not args.prepare_only:
        parser().error("run as the CI user (for example ubuntu), without sudo")
    args.image = args.image or PLATFORMS[args.platform][1]
    args.dockerfile = args.dockerfile or PLATFORMS[args.platform][2]
    os.umask(0o077)
    try:
        run = Run(args)
    except (OSError, Failure) as exc:
        parser().error("cannot create local CI evidence directory: " + str(exc))
    old_handlers = {}
    def interrupted(signum, frame):
        raise KeyboardInterrupt("signal " + str(signum))
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, interrupted)
    try:
        run.prepare_source()
        if args.prepare_only:
            run.meta["ci_exit_code"] = 0
            run.say("[local-ci] source prepared; Docker/NPU execution was not requested")
        else:
            run.run_ci()
    except KeyboardInterrupt as exc:
        run.meta["ci_exit_code"] = 130
        run.errors.append("interrupted: " + str(exc))
    except Exception as exc:
        run.errors.append(redact(str(exc)))
    finally:
        # A second Ctrl-C cannot bypass ownership recovery and evidence finalization.
        for sig in old_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            run.cleanup()
        except Exception as exc:
            run.errors.append("unexpected cleanup error: " + redact(str(exc)))
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
