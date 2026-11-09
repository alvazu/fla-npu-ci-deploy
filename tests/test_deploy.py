import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import shutil
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("deploy", Path(__file__).resolve().parents[1] / "scripts/deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class DeployTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX CI directory modes")
    def test_prepare_storage_creates_all_persistent_paths_with_shared_modes(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory) / "ci"
            previous = os.umask(0o077)
            try:
                deploy.prepare_storage(root)
            finally:
                os.umask(previous)
            for path, mode in deploy.storage_layout(root):
                self.assertTrue(path.is_dir())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
            self.assertTrue((root / "tmp/npu-locks").is_dir())

    @unittest.skipUnless(os.name == "posix", "POSIX CI directory modes")
    def test_prepare_storage_preserves_existing_permissions_and_lock_files(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory) / "ci"
            deploy.prepare_storage(root)
            root.chmod(0o770)
            locks = root / "tmp/npu-locks"
            lock_file = locks / "fla-npu-ci-npu-0.lock"
            lock_file.write_text("retained")
            deploy.prepare_storage(root)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o770)
            self.assertEqual(lock_file.read_text(), "retained")

    @unittest.skipUnless(os.name == "posix", "Linux deployment paths")
    def test_validate_root_rejects_host_roots_and_user_home(self):
        for root in ("/", "/tmp", "/var/tmp", "/etc", "/home/ubuntu", str(Path.home())):
            with self.subTest(root=root), self.assertRaises(deploy.DeployError):
                deploy.validate_root(root)
        self.assertEqual(deploy.validate_root("/tmp/fla-ci-test"), Path("/tmp/fla-ci-test"))
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory).resolve() / "ci"
            self.assertEqual(deploy.validate_root(str(root)), root)

    @unittest.skipUnless(os.name == "posix", "Linux deployment paths")
    def test_validate_root_permission_error_has_actionable_owner_advice(self):
        with patch.object(deploy.Path, "is_symlink", side_effect=PermissionError):
            with self.assertRaisesRegex(deploy.DeployError, "deployment owner"):
                deploy.validate_root("/home/private-user/ci")

    def test_proxy_only_storage_does_not_touch_npu_lock_or_cache_directories(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory) / "ci"
            deploy.prepare_storage(root, proxy_only=True)
            self.assertTrue((root / "tmp").is_dir())
            self.assertFalse((root / "tmp/npu-locks").exists())
            self.assertFalse((root / "cache_root").exists())

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

    def test_download_tries_direct_before_local_proxy_even_with_inherited_proxy(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            target = Path(directory) / "package.tar.gz"
            calls = []
            environment = {"PATH": os.environ.get("PATH", ""), "https_proxy": "http://127.0.0.1:7899",
                           "NO_PROXY": "*", "RUNNER_TOKEN": "do-not-inherit"}

            def fake_run(args, **kwargs):
                calls.append((args, kwargs["env"]))
                Path(args[args.index("--output") + 1]).write_bytes(b"release")
                return subprocess.CompletedProcess(args, 1 if len(calls) == 1 else 0, "", "")

            with patch.object(deploy, "run", side_effect=fake_run):
                deploy.download("https://github.com/example/release", target, env=environment)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][0][-1], calls[1][0][-1])
            self.assertIn("--noproxy", calls[0][0])
            self.assertEqual(calls[0][0][calls[0][0].index("--noproxy") + 1], "*")
            self.assertNotIn("https_proxy", calls[0][1])
            self.assertNotIn("NO_PROXY", calls[0][1])
            self.assertNotIn("RUNNER_TOKEN", calls[0][1])
            self.assertEqual(calls[1][1]["https_proxy"], "http://127.0.0.1:7899")
            self.assertEqual(calls[1][0][1], "--disable")

    def test_existing_proxy_config_needs_no_subscription_and_does_not_reinstall(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            (root / "clashctl").mkdir()
            (root / "clashctl/config-present").write_text("private")
            with patch.object(deploy, "proxy_health", return_value=True), \
                    patch.object(deploy, "ensure_proxy_ready") as ready, \
                    patch.object(deploy, "secret_value") as secret, \
                    patch.object(deploy, "download") as download:
                deploy.ensure_proxy(deploy.argparse.Namespace(), root)
            ready.assert_called_once_with(root)
            secret.assert_not_called()
            download.assert_not_called()

    def test_active_proxy_is_refreshed_and_checked_without_restart(self):
        root = Path("/home/ubuntu/ci")
        with patch.object(deploy, "ensure_proxy_service") as service, \
                patch.object(deploy, "ensure_actions_proxy_rule") as routes, \
                patch.object(deploy, "install_proxy_helper") as helper, \
                patch.object(deploy, "ensure_proxy_shell_hook"), \
                patch.object(deploy, "service_active", return_value=True), \
                patch.object(deploy, "systemctl", return_value=subprocess.CompletedProcess([], 0)) as systemctl, \
                patch.object(deploy, "proxy_environment") as environment, \
                patch.object(deploy, "proxy_connectivity", return_value=True):
            deploy.ensure_proxy_ready(root)
        service.assert_called_once_with(root)
        routes.assert_called_once_with(root)
        helper.assert_called_once_with(root)
        environment.assert_called_once_with(root)
        systemctl.assert_called_once_with("is-enabled", deploy.PROXY_UNIT, check=False)

    def test_proxy_without_github_connectivity_reports_repair_advice(self):
        with patch.object(deploy, "ensure_proxy_service"), \
                patch.object(deploy, "ensure_actions_proxy_rule"), \
                patch.object(deploy, "install_proxy_helper"), \
                patch.object(deploy, "ensure_proxy_shell_hook"), \
                patch.object(deploy, "service_active", return_value=True), \
                patch.object(deploy, "systemctl", return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(deploy, "proxy_environment"), \
                patch.object(deploy, "proxy_connectivity", return_value=False):
            with self.assertRaisesRegex(deploy.DeployError, "proxycheck.*subscription/selected node"):
                deploy.ensure_proxy_ready(Path("/home/ubuntu/ci"))

    def test_proxy_service_never_overwrites_another_deployment(self):
        unit = subprocess.CompletedProcess([], 0, "ExecStart=/other/private/bin/mihomo", "")
        with patch.object(deploy, "systemctl", return_value=unit) as systemctl:
            with self.assertRaisesRegex(deploy.DeployError, "another deployment"):
                deploy.ensure_proxy_service(Path("/home/ubuntu/ci"))
        systemctl.assert_called_once_with("cat", deploy.PROXY_UNIT, check=False)

    def test_unmanaged_proxy_listener_requires_explicit_migration(self):
        with patch.object(deploy, "systemctl", return_value=subprocess.CompletedProcess([], 1)), \
                patch.object(deploy, "service_active", return_value=False), \
                patch.object(deploy, "run", return_value=subprocess.CompletedProcess([], 0, "7899\n7898\n7891\n", "")), \
                patch.object(deploy.socket, "create_connection"):
            with self.assertRaisesRegex(deploy.DeployError, "identify its owner"):
                deploy.ensure_proxy_service(Path("/home/ubuntu/ci"))

    def test_private_existing_config_can_get_its_first_user_service_without_subscription(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            home = Path(directory)
            root = home / "ci"
            commands = []

            def systemctl(*args, **kwargs):
                commands.append(args)
                return subprocess.CompletedProcess([], 1 if args[0] == "cat" else 0, "", "")

            with patch.object(deploy.Path, "home", return_value=home), \
                    patch.object(deploy, "systemctl", side_effect=systemctl), \
                    patch.object(deploy, "service_active", return_value=False), \
                    patch.object(deploy, "run", return_value=subprocess.CompletedProcess([], 0, "7899\n7898\n7891\n", "")), \
                    patch.object(deploy.socket, "create_connection", side_effect=OSError):
                deploy.ensure_proxy_service(root)
            unit = (home / ".config/systemd/user/mihomo.service").read_text().replace("\\", "/")
            self.assertIn(f"ExecStart={root.as_posix()}/clashctl/bin/mihomo", unit)
            self.assertIn(f"-d {root.as_posix()}/clashctl/resources", unit)
            self.assertIn(f"-f {root.as_posix()}/clashctl/data/runtime.yaml", unit)
            self.assertEqual(commands, [("cat", deploy.PROXY_UNIT), ("daemon-reload",)])

    def test_shell_hook_is_idempotent_preserves_user_commands_and_does_not_enable_proxy(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            home = Path(directory)
            bashrc = home / ".bashrc"
            bashrc.write_text("# user customization\nalias ll='ls -l'\n")
            with patch.object(deploy.Path, "home", return_value=home):
                deploy.ensure_proxy_shell_hook(PurePosixPath("/home/ubuntu/ci"))
                first = bashrc.read_text()
                deploy.ensure_proxy_shell_hook(PurePosixPath("/home/ubuntu/ci"))
                self.assertEqual(bashrc.read_text(), first)
                deploy.ensure_proxy_shell_hook(PurePosixPath("/home/ubuntu/new-ci"))
            updated = bashrc.read_text()
            self.assertIn("alias ll='ls -l'", updated)
            self.assertEqual(updated.count("# >>> fla-npu-ci proxy >>>"), 1)
            self.assertIn('/home/ubuntu/new-ci/vpn-proxy.sh', updated)
            self.assertNotIn('/home/ubuntu/ci/vpn-proxy.sh', updated)
            self.assertNotIn("proxyon;", updated)

    def test_proxy_only_preflight_does_not_require_docker_npu_or_image(self):
        with patch.object(deploy.shutil, "which", return_value="/bin/tool"), \
                patch.object(deploy, "writable_directory", return_value=True), \
                patch.object(deploy, "run", return_value=subprocess.CompletedProcess([], 0, "yes\n", "")) as run, \
                patch.object(deploy, "systemctl", return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(deploy, "manager_can_access") as manager:
            checks = deploy.preflight(Path("/home/ubuntu/ci"), "not-installed", proxy_only=True)
        self.assertTrue(all(checks.values()))
        self.assertNotIn("command:docker", checks)
        self.assertNotIn("npu_devices", checks)
        self.assertNotIn("image", checks)
        manager.assert_not_called()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(list(deploy.storage_layout(Path("/ci/root"), proxy_only=True)),
                         [(Path("/ci/root"), 0o775), (Path("/ci/root/tmp"), 0o2775)])

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


class ActionsProxyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.home = self.root / "clashctl"
        (self.home / "data").mkdir(parents=True)
        self.mixin = self.home / "data/mixin.yaml"
        self.runtime = self.home / "data/runtime.yaml"
        self.mixin.write_text(json.dumps({"port": 7899, "rules": {
            "prepend": ["DOMAIN,keep.example,DIRECT",
                        "DOMAIN-SUFFIX,blob.core.windows.net,DIRECT"],
            "append": ["MATCH,DIRECT"]}}), encoding="utf-8")
        self.base_rules = ["DOMAIN-SUFFIX,windows.net,DIRECT", "MATCH,🚀节点选择"]
        self.runtime.write_text(json.dumps({"proxy-groups": [{"name": "🚀节点选择"}],
                                          "rules": self.base_rules}), encoding="utf-8")
        self.originals = {path: path.read_bytes() for path in (self.mixin, self.runtime)}
        self.merge_error = False
        self.merges = 0
        self.addCleanup(patch.stopall)
        patch.object(deploy, "run", side_effect=self.fake_run).start()
        patch.object(deploy, "user_env", return_value={}).start()
        self.active = patch.object(deploy, "service_active", return_value=True).start()
        self.reload = patch.object(deploy, "reload_proxy_config").start()

    def fake_run(self, args, **kwargs):
        if args[0] == "bash":
            self.assertIn("_merge_config", args[-1])
            self.assertNotIn("restart", args[-1])
            self.assertEqual(kwargs["env"]["CLASHCTL_HOME"], str(self.home))
            self.merges += 1
            if self.merge_error:
                raise deploy.DeployError("invalid candidate")
            config = json.loads(self.runtime.read_text(encoding="utf-8"))
            config["rules"] = json.loads(self.mixin.read_text(encoding="utf-8"))["rules"]["prepend"] + self.base_rules
            self.runtime.write_text(json.dumps(config), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        path = Path(args[-1])
        data = json.loads(path.read_text(encoding="utf-8"))
        if "-i" in args:
            data["rules"]["prepend"] = json.loads(args[-2].split(" = ", 1)[1])
            path.write_text(json.dumps(data), encoding="utf-8")
        elif args[-2] == ".rules.prepend // []":
            data = data["rules"]["prepend"]
        return subprocess.CompletedProcess(args, 0, json.dumps(data), "")

    def test_blob_rule_precedes_direct_preserves_other_config_and_is_idempotent(self):
        deploy.ensure_actions_proxy_rule(self.root)
        mixin = json.loads(self.mixin.read_text(encoding="utf-8"))
        rule = "DOMAIN-SUFFIX,blob.core.windows.net,🚀节点选择"
        self.assertEqual(mixin["rules"]["prepend"], [rule, "DOMAIN,keep.example,DIRECT"])
        self.assertEqual(mixin["port"], 7899)
        self.assertEqual(mixin["rules"]["append"], ["MATCH,DIRECT"])
        self.assertEqual(json.loads(self.runtime.read_text(encoding="utf-8"))["rules"][0], rule)
        backups = sorted((self.home / "data").glob("*.bak-*"))
        self.assertEqual(len(backups), 2)
        for path in backups:
            original = self.home / "data" / path.name.split(".bak-", 1)[0]
            self.assertEqual(path.read_bytes(), self.originals[original])
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        deploy.ensure_actions_proxy_rule(self.root)
        self.assertEqual(len(list((self.home / "data").glob("*.bak-*"))), 2)
        self.assertEqual(self.merges, 1)
        self.reload.assert_called_once_with(self.home)

    def test_inactive_proxy_gets_persistent_rules_without_reload(self):
        self.active.return_value = False
        deploy.ensure_actions_proxy_rule(self.root)
        self.assertEqual(self.merges, 1)
        self.reload.assert_not_called()

    def test_failed_validation_restores_both_files_without_reloading(self):
        self.merge_error = True
        with self.assertRaisesRegex(deploy.DeployError, "original mixin/runtime restored"):
            deploy.ensure_actions_proxy_rule(self.root)
        for path, original in self.originals.items():
            self.assertEqual(path.read_bytes(), original)
        self.reload.assert_not_called()

    def test_failed_reload_restores_files_and_reloads_original_config(self):
        self.reload.side_effect = [deploy.DeployError("reload failed"), None]
        with self.assertRaisesRegex(deploy.DeployError, "original mixin/runtime restored"):
            deploy.ensure_actions_proxy_rule(self.root)
        for path, original in self.originals.items():
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.reload.call_count, 2)

    def test_alternative_subscription_group_and_missing_group(self):
        self.assertEqual(deploy.actions_proxy_rule({"proxy-groups": [{"name": "Proxy"}]}),
                         "DOMAIN-SUFFIX,blob.core.windows.net,Proxy")
        with self.assertRaisesRegex(deploy.DeployError, "requires a proxy group"):
            deploy.actions_proxy_rule({"proxy-groups": [{"name": "DIRECT"}]})


class ProxyReloadTests(unittest.TestCase):
    def test_reload_uses_authenticated_loopback_and_disables_inherited_proxy(self):
        config = {"external-controller": "0.0.0.0:9099", "secret": "private-controller-secret",
                  "proxy-groups": [{"name": "🚀节点选择"}]}
        with patch.object(deploy, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(config))), \
                patch.object(deploy, "build_opener") as opener:
            deploy.reload_proxy_config(Path("/ci/clashctl"))
            self.assertEqual(opener.call_args.args[0].proxies, {})
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:9099/configs")
            self.assertEqual(request.method, "PUT")
            self.assertEqual(request.get_header("Authorization"), "Bearer private-controller-secret")
            self.assertEqual(json.loads(json.loads(request.data)["payload"]), config)
            # YAML rejects JSON surrogate-pair escapes for the emoji group name.
            self.assertIn("🚀节点选择", json.loads(request.data)["payload"])

    def test_reload_error_does_not_expose_controller_secret(self):
        config = {"external-controller": "127.0.0.1:9099", "secret": "private-controller-secret"}
        with patch.object(deploy, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(config))), \
                patch.object(deploy, "build_opener", side_effect=OSError("private-controller-secret")):
            with self.assertRaises(deploy.DeployError) as error:
                deploy.reload_proxy_config(Path("/ci/clashctl"))
        self.assertNotIn("private-controller-secret", str(error.exception))


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Linux Bash proxy fixture")
class ProxyShellTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root / "clashctl/data").mkdir(parents=True)
        self.runtime = self.root / "clashctl/data/runtime.yaml"
        self.runtime.write_text("port: 7899\nmixed-port: 7898\nsocks-port: 7891\n")
        self.helper = self.root / "vpn-proxy.sh"
        shutil.copyfile(Path(deploy.__file__).with_name("vpn-proxy.sh"), self.helper)
        self.commands = self.root / "commands.log"
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        self.tool("systemctl", r'''printf 'systemctl %s\n' "$*" >> "$FLA_TEST_COMMANDS"
[ "$1" = --user ] && shift
case "$1" in
show)
  if [ "${FLA_FAKE_UNIT_OTHER:-0}" = 1 ]; then printf '%s\n' '/other/bin/mihomo'; else printf '%s\n' "$FLA_TEST_ROOT/clashctl/bin/mihomo -f runtime.yaml"; fi ;;
is-active) [ "${FLA_FAKE_ACTIVE:-1}" = 1 ] ;;
start) exit 0 ;;
*) exit 8 ;;
esac
''')
        self.tool("timeout", "exit 0\n")
        self.tool("curl", r'''printf 'curl\n' >> "$FLA_TEST_COMMANDS"
printf '<%s>\n' "$@" >> "$FLA_TEST_COMMANDS"
exit "${FLA_FAKE_CURL_STATUS:-0}"
''')
        self.tool("sudo", r'''printf 'sudo %s\n' "$*" >> "$FLA_TEST_COMMANDS"
while [ "$1" != -- ]; do shift; done
shift
exec "$@"
''')
        self.environment = {key: value for key, value in os.environ.items() if key not in deploy.PROXY_KEYS}
        self.environment.update(PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                                FLA_TEST_ROOT=str(self.root), FLA_TEST_COMMANDS=str(self.commands))

    def tool(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)

    def shell(self, command, **environment):
        return subprocess.run(["bash", "-ec", 'source "$1"; ' + command, "proxy-fixture", str(self.helper)],
                              env={**self.environment, **environment}, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)

    def test_proxyon_sets_runtime_ports_and_proxycheck_forces_real_proxy_without_restart(self):
        result = self.shell('proxyon; proxycheck; printf "HTTP=%s SOCKS=%s\\n" "$https_proxy" "$all_proxy"; '
                            'proxyoff; test -z "${https_proxy-}"', NO_PROXY="*", https_proxy="http://invalid:1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("HTTP=http://127.0.0.1:7899 SOCKS=socks5h://127.0.0.1:7891", result.stdout)
        commands = self.commands.read_text()
        self.assertNotIn("start", commands)
        self.assertNotIn("restart", commands)
        self.assertIn("<--disable>", commands)
        self.assertIn("<--proxy>\n<http://127.0.0.1:7899>", commands)
        self.assertIn("<--noproxy>\n<>", commands)
        self.assertIn("<https://github.com/>", commands)

    def test_proxycheck_is_read_only_when_service_is_stopped(self):
        result = self.shell("proxycheck", FLA_FAKE_ACTIVE="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("run proxyon first", result.stderr)
        commands = self.commands.read_text()
        self.assertNotIn(" start ", commands)
        self.assertNotIn("curl", commands)

    def test_failed_activation_clears_stale_proxy_environment_and_does_not_start_foreign_service(self):
        result = self.shell('if proxyon; then exit 9; fi; '
                            'test -z "${https_proxy-}"; test -z "${HTTP_PROXY-}"; '
                            'test -z "${ALL_PROXY-}"; test -z "${GIT_HTTP_PROXY-}"',
                            FLA_FAKE_UNIT_OTHER="1", https_proxy="http://retained:1",
                            HTTP_PROXY="http://retained:1", ALL_PROXY="socks5h://retained:2",
                            GIT_HTTP_PROXY="http://retained:1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("another installation", result.stderr)
        self.assertNotIn("start", self.commands.read_text())

    def test_mixed_port_can_replace_disabled_dedicated_http_and_socks_ports(self):
        self.runtime.write_text("port: 0\nmixed-port: 12345\nsocks-port: 0\n")
        result = self.shell('proxyon; test "$http_proxy" = http://127.0.0.1:12345; '
                            'test "$all_proxy" = socks5h://127.0.0.1:12345')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cross_user_caller_uses_only_the_config_owners_user_manager(self):
        result = self.shell('_fla_proxy_owner_uid() { printf 2345; }; proxyon; proxycheck')
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands.read_text()
        self.assertIn("sudo -n -u #2345 -- test -r", commands)
        self.assertIn("XDG_RUNTIME_DIR=/run/user/2345", commands)
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/2345/bus", commands)
        self.assertNotIn("restart", commands)

    def test_proxycheck_explains_https_failure(self):
        result = self.shell("proxycheck", FLA_FAKE_CURL_STATUS="22")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("subscription and selected node", result.stderr)

    def test_clashctl_invokes_official_function_as_configuration_owner(self):
        command = self.root / "clashctl/scripts/cmd/clashctl.sh"
        command.parent.mkdir(parents=True)
        command.write_text('clashctl() { printf "HOME=%s\\n" "$CLASHCTL_HOME"; printf "ARG=<%s>\\n" "$@"; }\n')
        result = self.shell('_fla_proxy_owner_uid() { printf 2345; }; clashctl status "one argument"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"HOME={self.root}/clashctl", result.stdout)
        self.assertIn("ARG=<status>\nARG=<one argument>", result.stdout)
        self.assertIn("sudo -n -u #2345 -- env CLASHCTL_HOME=", self.commands.read_text())


if __name__ == "__main__":
    unittest.main()
