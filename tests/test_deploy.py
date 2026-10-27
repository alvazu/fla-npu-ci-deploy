import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("deploy", Path(__file__).resolve().parents[1] / "scripts/deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class DeployTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Linux deployment paths")
    def test_validate_root_rejects_host_roots_and_user_home(self):
        for root in ("/", "/tmp", "/var/tmp", "/etc", "/home/ubuntu", str(Path.home())):
            with self.subTest(root=root), self.assertRaises(deploy.DeployError):
                deploy.validate_root(root)
        self.assertEqual(deploy.validate_root("/tmp/fla-ci-test"), Path("/tmp/fla-ci-test"))
        self.assertEqual(deploy.validate_root("/home/ubuntu/ci"), Path("/home/ubuntu/ci"))

    def test_platforms_keep_default_runner_labels_and_add_requested_custom_labels(self):
        self.assertEqual(deploy.PLATFORMS["a5"]["labels"].split(","),
                         ["npu", "ascend950", "flash-linear-attention-npu-a5"])
        self.assertEqual(deploy.PLATFORMS["a2"]["labels"].split(","),
                         ["npu", "flash-linear-attention-npu"])
        # The deployer does not disable the runner's automatic self-hosted/Linux/arch labels.
        source = Path(deploy.__file__).read_text()
        self.assertNotIn('"--no-default-labels"', source)

    def test_active_runner_is_never_restarted_or_reconfigured(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            home = Path(directory)
            root = home / "ci"
            path = home / ".config/systemd/user" / deploy.RUNNER_UNIT
            path.parent.mkdir(parents=True)
            path.write_text(deploy.runner_unit_text(root))
            with patch.object(deploy.Path, "home", return_value=home), \
                    patch.object(deploy, "service_active", return_value=True), \
                    patch.object(deploy, "systemctl", return_value=subprocess.CompletedProcess([], 0)) as systemctl:
                deploy.ensure_runner_service(root)
                systemctl.assert_called_once_with("is-enabled", deploy.RUNNER_UNIT, check=False)

    def test_unknown_existing_active_service_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            home = Path(directory)
            path = home / ".config/systemd/user" / deploy.RUNNER_UNIT
            path.parent.mkdir(parents=True)
            path.write_text("[Service]\nWorkingDirectory=/other/runner\n")
            with patch.object(deploy.Path, "home", return_value=home), \
                    patch.object(deploy, "service_active", return_value=True), \
                    patch.object(deploy, "systemctl") as systemctl:
                with self.assertRaises(deploy.DeployError):
                    deploy.ensure_runner_service(home / "ci")
                systemctl.assert_not_called()

    def test_registered_runner_rejects_different_repository_or_name(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            runner = root / "actions-runner"
            runner.mkdir()
            identity = {"gitHubUrl": "https://github.com/flashserve/flash-linear-attention-npu",
                        "agentName": "existing", "workFolder": "_work"}
            (runner / ".runner").write_text(json.dumps(identity))
            (runner / ".credentials").write_text("{}")
            self.assertEqual(deploy.existing_runner(root, identity["gitHubUrl"]), identity)
            with self.assertRaises(deploy.DeployError):
                deploy.existing_runner(root, "https://github.com/other/repository")
            with self.assertRaises(deploy.DeployError):
                deploy.existing_runner(root, identity["gitHubUrl"], "replacement")

    def test_unit_contains_only_paths_not_provisioning_secrets(self):
        with patch.dict(deploy.os.environ, {"RUNNER_TOKEN": "super-secret-token",
                                           "FLA_CI_SUBSCRIPTION_URL": "https://private.invalid/subscription"}), \
                patch.object(deploy.os, "getuid", return_value=1000, create=True):
            unit = deploy.runner_unit_text(PurePosixPath("/opt/ci"))
            env = deploy.user_env()
        self.assertNotIn("super-secret", unit)
        self.assertNotIn("private.invalid", unit)
        self.assertNotIn("RUNNER_TOKEN", env)
        self.assertNotIn("FLA_CI_SUBSCRIPTION_URL", env)
        self.assertIn("/opt/ci/vpn-proxy.sh", unit)

    @unittest.skipUnless(os.name == "posix", "POSIX owner/mode validation")
    def test_private_log_restricts_existing_mode_before_writing(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            log = Path(directory) / "install.log"
            log.write_text("old")
            log.chmod(0o644)
            with deploy.private_log(log) as stream:
                self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
                stream.write("private")
            self.assertEqual(log.read_text(), "private")

    @unittest.skipUnless(os.name == "posix", "POSIX symlink validation")
    def test_private_log_refuses_symlink_without_truncating_target(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            target = Path(directory) / "target"
            target.write_text("untouched")
            log = Path(directory) / "install.log"
            log.symlink_to(target)
            with self.assertRaises(deploy.DeployError):
                deploy.private_log(log)
            self.assertEqual(target.read_text(), "untouched")

    def test_registered_runner_does_not_need_registration_token_or_download(self):
        args = deploy.argparse.Namespace(repo_url="https://github.com/flashserve/repository", runner_name=None)
        with patch.object(deploy, "existing_runner", return_value={"agentName": "already-present"}), \
                patch.object(deploy, "ensure_runner_service") as service, \
                patch.object(deploy, "secret_value") as secret, \
                patch.object(deploy, "download") as download:
            root = Path("/opt/ci")
            self.assertEqual(deploy.ensure_runner(args, root, deploy.PLATFORMS["a5"]), "already-present")
            secret.assert_not_called()
            download.assert_not_called()
            service.assert_called_once_with(root)

    def test_errors_do_not_disclose_subprocess_secrets(self):
        failure = subprocess.CompletedProcess(["tool"], 1, "secret-in-output", "subscription-token")
        with patch.object(deploy.subprocess, "run", return_value=failure):
            with self.assertRaises(deploy.DeployError) as failure:
                deploy.run(["tool", "secret-in-argument"])
        self.assertEqual(str(failure.exception), "Command failed: tool")

    def test_download_uses_mirror_and_checks_hash_before_rename(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            target = Path(directory) / "package.tar.gz"
            payload = b"known-good-release"
            expected = deploy.hashlib.sha256(payload).hexdigest()
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                destination = Path(args[args.index("--output") + 1])
                destination.write_bytes(b"wrong" if len(calls) == 1 else payload)
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch.object(deploy, "run", side_effect=fake_run):
                deploy.download("https://github.com/example/release", target, digest=expected)
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(calls[1][-1], "https://ghfast.top/https://github.com/example/release")
            self.assertIn("--max-time", calls[0])

    @unittest.skipUnless(os.name == "posix", "POSIX symlink validation")
    def test_download_refuses_destination_and_partial_symlinks(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            original = root / "original"
            original.write_text("untouched")
            target = root / "package.tar.gz"
            with patch.object(deploy, "run") as run:
                target.symlink_to(original)
                with self.assertRaises(deploy.DeployError):
                    deploy.download("https://github.com/example/release", target)
                target.unlink()
                target.with_name(target.name + ".part").symlink_to(original)
                with self.assertRaises(deploy.DeployError):
                    deploy.download("https://github.com/example/release", target)
                run.assert_not_called()
            self.assertEqual(original.read_text(), "untouched")

    def test_archive_refuses_traversal_before_writing(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            archive = root / "bad.tar"
            with tarfile.open(archive, "w") as tar:
                member = tarfile.TarInfo("../escaped")
                member.size = 3
                tar.addfile(member, io.BytesIO(b"bad"))
            target = root / "target"
            target.mkdir()
            with self.assertRaises(deploy.DeployError):
                deploy.extract_archive(archive, target)
            self.assertFalse((root / "escaped").exists())

    def test_archive_refuses_external_symlink(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            archive = root / "bad.tar"
            with tarfile.open(archive, "w") as tar:
                member = tarfile.TarInfo("link")
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
                tar.addfile(member)
            with self.assertRaises(deploy.DeployError):
                deploy.extract_archive(archive, root / "target")


if __name__ == "__main__":
    unittest.main()
