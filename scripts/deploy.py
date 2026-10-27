#!/usr/bin/env python3
"""Install a user-owned NPU CI runner without replacing an existing identity."""
import argparse
import getpass
try:
    import grp
except ImportError:  # Permit --help/static tests on non-Linux development hosts.
    grp = None
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time

RUNNER_VERSION = "2.338.0"
CLASHCTL_REV = "e38d643725594c38c3fb02f355a2c89eba9c296c"
# Official codeload archive for the pinned commit; verify mirrors before executing.
CLASHCTL_SHA256 = "172e68840d6b81e454950f90e81439fc0ca424e589b1f2f5de36aa9a39d12fef"
RUNNER_UNIT = "fla-npu-runner.service"
PROXY_UNIT = "mihomo.service"
PLATFORMS = {
    "a5": {"arch": "x64", "machine": "x86_64", "image": "fla-npu-ci:9.1.0-950",
           "sha256": "af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32",
           "labels": "npu,ascend950,flash-linear-attention-npu-a5"},
    "a2": {"arch": "arm64", "machine": "aarch64", "image": "fla-npu-ci:9.1.0-910b",
           "sha256": "628b4a7258487b80c1d3c221095a7ded349f5ae0175fd3dfab708d07428f041b",
           "labels": "npu,flash-linear-attention-npu"},
}


class DeployError(Exception):
    pass


def run(args, *, env=None, cwd=None, timeout=120, check=True):
    """Never include subprocess output/arguments in errors: they may contain secrets."""
    try:
        result = subprocess.run(args, env=env, cwd=cwd, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeployError("Command failed or timed out: " + Path(args[0]).name) from None
    if check and result.returncode:
        raise DeployError("Command failed: " + Path(args[0]).name)
    return result


def user_env():
    env = os.environ.copy()
    for name in ("RUNNER_TOKEN", "FLA_CI_SUBSCRIPTION_URL"):
        env.pop(name, None)
    uid = os.getuid()
    env.update(XDG_RUNTIME_DIR=f"/run/user/{uid}",
               DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{uid}/bus")
    return env


def systemctl(*args, check=True):
    return run(["systemctl", "--user", *args], env=user_env(), check=check)


def service_active(name):
    return systemctl("is-active", name, check=False).returncode == 0


def validate_root(value):
    path = Path(value)
    if not path.is_absolute() or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
        raise DeployError("--ci-root must be an absolute path using letters, numbers, _, . or -")
    protected = {Path("/"), Path("/tmp"), Path("/var/tmp"), Path.home()}
    if (".." in path.parts or path in protected or path.parent == Path("/")
            or path.parent == Path("/home")):
        raise DeployError("Refusing an unsafe CI root")
    if path.is_symlink():
        raise DeployError("CI root must not be a symlink")
    return path


def load_secret_file(path):
    """Validate the opened descriptor, including ownership, instead of only the name."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise DeployError("Secret file must belong to the current user with mode 0600")
            data = json.load(stream)
    except (OSError, ValueError):
        raise DeployError("Unable to read private JSON secret file") from None
    if not isinstance(data, dict) or any(not isinstance(v, str) for v in data.values()):
        raise DeployError("Secret file must contain a JSON object of strings")
    return data


def secret_value(args, name):
    env_name = {"subscription_url": "FLA_CI_SUBSCRIPTION_URL", "runner_token": "RUNNER_TOKEN"}[name]
    value = os.environ.get(env_name) or args.secrets.get(name, "")
    if not value and sys.stdin.isatty():
        value = getpass.getpass(f"{env_name} (hidden): ")
    if not value or "\n" in value or "\r" in value:
        raise DeployError(f"Provide {env_name} through the environment, private secret file, or a terminal")
    return value


def write_private(path, text):
    if path.is_symlink():
        raise DeployError("Refusing to write through a symlink")
    with path.open("w") as stream:
        stream.write(text)
    path.chmod(0o600)


def private_log(path):
    """Tighten existing permissions before truncating or writing a sensitive log."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            os.close(fd)
            raise DeployError("Private log must be a regular file owned by the current user")
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        return os.fdopen(fd, "w")
    except OSError:
        raise DeployError("Cannot open a private log safely") from None


def writable_directory(path):
    return path.is_dir() and not path.is_symlink() and os.access(path, os.R_OK | os.W_OK | os.X_OK)


def setup_host(root):
    """Only the explicit bootstrap flag grants sudo operations. Never restart users/services."""
    user = getpass.getuser()
    # The explicit flag allows a visible sudo prompt once; later commands never
    # wait on an invisible password prompt while their diagnostic output is captured.
    try:
        authorization = subprocess.run(["sudo", "-v"], env=user_env(), timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise DeployError("Unable to authenticate sudo for --setup-host") from None
    if authorization.returncode:
        raise DeployError("sudo authentication failed for --setup-host")
    dependencies = {"curl": "curl", "tar": "tar", "unzip": "unzip", "git": "git",
                    "systemctl": "systemd", "loginctl": "systemd", "flock": "util-linux"}
    missing = sorted({package for command, package in dependencies.items() if not shutil.which(command)})
    if missing:
        run(["sudo", "-n", "apt-get", "update"], timeout=600)
        run(["sudo", "-n", "apt-get", "install", "-y", *missing, "ca-certificates"], timeout=1200)
    try:
        grp.getgrnam("docker")
    except KeyError:
        raise DeployError("Install and enable Docker Engine first; this tool does not replace host Docker/Ascend drivers")
    run(["sudo", "-n", "groupadd", "-f", "fla-npu-ci"])
    groups = ["docker", "fla-npu-ci"]
    try:
        grp.getgrnam("HwHiAiUser")
        groups.append("HwHiAiUser")
    except KeyError:
        pass
    run(["sudo", "-n", "usermod", "-aG", ",".join(groups), user])
    # Do not recursively chown a shared CI root, its caches, or another user's files.
    for path, mode in ((root, "0775"), (root / "tmp", "2775"), (root / "cache_root", "2775")):
        if path.is_symlink():
            raise DeployError("Refusing a symlink in the CI storage layout")
        run(["sudo", "-n", "install", "-d", "-m", mode, "-g", "fla-npu-ci", str(path)])
    run(["sudo", "-n", "loginctl", "enable-linger", user])
    required = {grp.getgrnam(g).gr_gid for g in groups}
    if not required.issubset(set(os.getgroups()) | {os.getgid()}):
        raise DeployError("Host setup completed. New group membership needs a fresh login; log out/in and rerun without --setup-host. Existing user services were left running")


def preflight(root, image, *, require_image=True):
    checks = {}
    for command in ("bash", "curl", "tar", "gzip", "unzip", "sha256sum", "docker", "systemctl", "loginctl", "flock"):
        checks["command:" + command] = bool(shutil.which(command))
    checks["storage"] = all(writable_directory(p) for p in (root, root / "tmp", root / "cache_root"))
    linger = run(["loginctl", "show-user", getpass.getuser(), "--property=Linger", "--value"], check=False)
    checks["linger"] = linger.returncode == 0 and linger.stdout.strip() == "yes"
    manager = systemctl("show-environment", check=False)
    checks["user_systemd"] = manager.returncode == 0
    if checks["command:docker"]:
        checks["docker"] = run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False).returncode == 0
        if checks["user_systemd"]:
            # Inspect the existing manager's groups without starting/stopping any unit.
            # A manager started before usermod may still lack docker/NPU groups.
            checks["user_manager_docker"] = manager_can_access(Path("/var/run/docker.sock"))
        if require_image:
            checks["image"] = run(["docker", "image", "inspect", image], check=False).returncode == 0
    npu_smi = shutil.which("npu-smi")
    checks["npu_smi"] = bool(npu_smi) and run([npu_smi, "info"], check=False).returncode == 0
    devices = list(Path("/dev").glob("davinci[0-9]*"))
    checks["npu_devices"] = bool(devices) and all(os.access(p, os.R_OK | os.W_OK) for p in devices)
    checks["user_manager_devices"] = bool(devices) and all(manager_can_access(p) for p in devices)
    return checks


def manager_can_access(path):
    result = run(["systemctl", "show", f"user@{os.getuid()}.service", "--property=MainPID", "--value"], check=False)
    if result.returncode or not result.stdout.strip().isdigit():
        return False
    try:
        status = Path("/proc", result.stdout.strip(), "status").read_text()
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        uid = int(fields["Uid"].split()[1])
        groups = {int(value) for value in fields["Groups"].split()}
        groups.update(int(value) for value in fields["Gid"].split())
        info = path.stat()
        bits = (info.st_mode >> (6 if info.st_uid == uid else 3 if info.st_gid in groups else 0)) & 0o7
        return bits & 0o6 == 0o6
    except (OSError, KeyError, ValueError):
        return False


def require_checks(checks):
    missing = [name for name, passed in checks.items() if not passed]
    if missing:
        suffix = ""
        if "user_manager_docker" in missing or "user_manager_devices" in missing:
            suffix = "; the user manager may have stale groups. Finish existing user jobs before arranging a fresh login/user-manager session; this tool will not stop it"
        raise DeployError("Preflight failed: " + ", ".join(missing) + suffix)


def sha256(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def download(url, destination, *, digest=None, gh_proxy="https://ghfast.top", env=None):
    if destination.is_symlink():
        raise DeployError("Download destination must not be a symlink")
    if destination.is_file() and digest and sha256(destination) == digest:
        return
    urls = [url, gh_proxy.rstrip("/") + "/" + url]
    part = destination.with_name(destination.name + ".part")
    if part.is_symlink():
        raise DeployError("Partial download must not be a symlink")
    for candidate in urls:
        result = run(["curl", "--fail", "--location", "--silent", "--show-error",
                      "--connect-timeout", "15", "--max-time", "600", "--retry", "2",
                      "--output", str(part), candidate], timeout=1900, check=False, env=env)
        if result.returncode == 0 and part.is_file() and (not digest or sha256(part) == digest):
            part.replace(destination)
            return
        part.unlink(missing_ok=True)
    raise DeployError("Download/checksum failed for " + destination.name)


def extract_archive(archive_path, target):
    """Reject traversal and external links before extracting either upstream archive."""
    base = target.resolve()
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        for member in members:
            destination = (base / member.name).resolve()
            if not destination.is_relative_to(base) or member.isdev() or member.isfifo():
                raise DeployError("Unsafe archive member")
            if member.issym() or member.islnk():
                link_base = destination.parent if member.issym() else base
                if not (link_base / member.linkname).resolve().is_relative_to(base):
                    raise DeployError("Unsafe archive link")
        archive.extractall(base, members=members)


def proxy_environment(root):
    script = root / "vpn-proxy.sh"
    result = run(["bash", "-ec", 'source "$1"; proxyon >/dev/null; env -0', "proxy-env", str(script)], env=user_env())
    env = user_env()
    allowed = {"http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY", "GIT_HTTP_PROXY", "GIT_HTTPS_PROXY"}
    for item in result.stdout.split("\0"):
        key, _, value = item.partition("=")
        if key in allowed:
            env[key] = value
    return env


def proxy_health(root):
    home = root / "clashctl"
    runtime = home / "data/runtime.yaml"
    if not (home / "bin/mihomo").is_file() or not runtime.is_file():
        return False
    # clashctl's own pinned yq validates subscription contents without printing them.
    yq = home / "bin/yq"
    if not yq.is_file():
        return False
    return run([str(yq), "-e", '(.proxies | length) > 0 or (."proxy-providers" | length) > 0',
                str(runtime)], check=False).returncode == 0


def ensure_proxy(args, root):
    home = root / "clashctl"
    script = root / "vpn-proxy.sh"
    if home.is_symlink() or script.is_symlink():
        raise DeployError("Proxy installation paths must not be symlinks")
    scratch = root / "tmp/proxy-bootstrap"
    marker = scratch / "owned-install.json"
    owned_install = {"home": str(home), "commit": CLASHCTL_REV}
    retry_owned = marker.is_file() and marker.read_text() == json.dumps(owned_install)
    if home.exists() and any(home.iterdir()) and proxy_health(root) and script.is_file():
        unit = systemctl("cat", PROXY_UNIT, check=False)
        if unit.returncode or str(home / "bin/mihomo") not in unit.stdout:
            raise DeployError("mihomo.service belongs to another deployment; refusing to replace it")
        if not service_active(PROXY_UNIT):
            systemctl("start", PROXY_UNIT)
        if systemctl("is-enabled", PROXY_UNIT, check=False).returncode:
            systemctl("enable", PROXY_UNIT)
        return
    if home.exists() and any(home.iterdir()) and not retry_owned:
        raise DeployError("Existing clashctl installation is incomplete; refusing to replace it. Repair its subscription/service first")
    if (service_active(PROXY_UNIT) or script.exists()) and not retry_owned:
        raise DeployError("Existing proxy service/script found; refusing to overwrite it")
    if retry_owned and service_active(RUNNER_UNIT):
        raise DeployError("A runner is active; refusing to repair/restart its incomplete proxy during a possible job")
    subscription = secret_value(args, "subscription_url")
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch.chmod(0o700)
    write_private(marker, json.dumps(owned_install))
    source_archive = scratch / "clashctl-source.tar.gz"
    download(f"https://github.com/nelvko/clashctl/archive/{CLASHCTL_REV}.tar.gz", source_archive,
             digest=CLASHCTL_SHA256, gh_proxy=args.gh_proxy)
    source = scratch / ("clashctl-" + CLASHCTL_REV)
    if not source.exists():
        extract_archive(source_archive, scratch)
    mixin = source / "resources/mixin.yaml.example"
    text = mixin.read_text()
    text = text.replace("mixed-port: 7890", "port: 7899\nmixed-port: 7898\nsocks-port: 7891\nbind-address: 127.0.0.1")
    text = text.replace("0.0.0.0:9090", "127.0.0.1:9099")
    if "store-selected:" not in text:
        text += "\nprofile:\n  store-selected: true\n"
    mixin.write_text(text)
    # Shared parent directories may carry setgid; clashctl private locks require 0700.
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    env = user_env()
    env.update(CI="1", TMPDIR=str(scratch), CLASHCTL_DOWNLOAD_TIMEOUT="180")
    log_path = scratch / "install.log"
    with private_log(log_path) as log:
        try:
            result = subprocess.run(["bash", "install.sh", "--local", "--home", str(home),
                                     "--kernel", "mihomo", "--service", "systemd-user",
                                     "--gh-proxy", args.gh_proxy, "--sub", subscription],
                                    cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=2400, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise DeployError("Proxy installer failed/timed out; inspect private proxy-bootstrap/install.log") from None
    if result.returncode or not proxy_health(root):
        raise DeployError("Proxy install/subscription validation failed; inspect private proxy-bootstrap/install.log (it may contain the subscription URL)")
    template = Path(__file__).with_name("vpn-proxy.sh").read_bytes()
    script.write_bytes(template)
    script.chmod(0o644)
    if not service_active(PROXY_UNIT):
        systemctl("start", PROXY_UNIT)


def runner_unit_text(root):
    return f"""[Unit]
Description=Flash Linear Attention NPU GitHub Actions runner
After=network-online.target mihomo.service
Requires=mihomo.service

[Service]
WorkingDirectory={root}/actions-runner
ExecStart=/bin/bash -ec 'source {root}/vpn-proxy.sh; proxyon; exec {root}/actions-runner/bin/runsvc.sh'
Restart=always
RestartSec=5
KillMode=process
KillSignal=SIGTERM
TimeoutStopSec=5min

[Install]
WantedBy=default.target
"""


def existing_runner(root, repo_url, runner_name=None):
    identity = root / "actions-runner/.runner"
    if not identity.is_file():
        return None
    try:
        data = json.loads(identity.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise DeployError("Unable to read existing runner identity") from None
    if (data.get("gitHubUrl", "").rstrip("/").lower() != repo_url.rstrip("/").lower()
            or data.get("workFolder") != "_work"
            or not data.get("agentName")
            or (runner_name and data.get("agentName") != runner_name)):
        raise DeployError("Existing runner identity differs; refusing to replace its registration")
    if not (root / "actions-runner/.credentials").is_file():
        raise DeployError("Existing runner credentials are missing; refusing to replace its registration")
    return data


def ensure_runner_service(root):
    """An active runner may already be executing a job. Never restart it on reruns."""
    path = Path.home() / ".config/systemd/user" / RUNNER_UNIT
    expected = runner_unit_text(root)
    active = service_active(RUNNER_UNIT)
    if path.exists():
        content = path.read_text()
        if (f"WorkingDirectory={root}/actions-runner" not in content
                or f"exec {root}/actions-runner/bin/runsvc.sh" not in content):
            raise DeployError("Existing runner user unit belongs to another deployment")
        if active:
            if systemctl("is-enabled", RUNNER_UNIT, check=False).returncode:
                systemctl("enable", RUNNER_UNIT)
            return
    elif active:
        raise DeployError("Unknown active runner unit; refusing to replace it")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_private(path, expected)
        systemctl("daemon-reload")
    systemctl("enable", RUNNER_UNIT)
    systemctl("start", RUNNER_UNIT)
    # Check without restarting: a newly connected listener can immediately claim a job.
    for _ in range(10):
        if service_active(RUNNER_UNIT):
            return
        time.sleep(1)
    raise DeployError("Runner user service did not become active; inspect its user journal")


def ensure_runner(args, root, config):
    if (root / "actions-runner").is_symlink():
        raise DeployError("Runner installation directory must not be a symlink")
    identity = existing_runner(root, args.repo_url, args.runner_name)
    if identity:
        ensure_runner_service(root)
        return identity["agentName"]
    if service_active(RUNNER_UNIT):
        raise DeployError("Runner service is active but its identity is missing; refusing replacement")
    token = secret_value(args, "runner_token")
    runner = root / "actions-runner"
    marker = runner / ".fla-ci-bootstrap-sha256"
    prepared = marker.is_file() and marker.read_text().strip() == config["sha256"]
    if runner.exists() and any(runner.iterdir()) and not prepared:
        raise DeployError("Runner directory is nonempty and unrecognized; refusing to overwrite it")
    runner.mkdir(mode=0o700, exist_ok=True)
    runner.chmod(0o700)
    if not prepared:
        downloads = root / "tmp/runner-download"
        if downloads.is_symlink():
            raise DeployError("Runner download directory must not be a symlink")
        downloads.mkdir(mode=0o700, exist_ok=True)
        downloads.chmod(0o700)
        package = downloads / f"actions-runner-linux-{config['arch']}-{RUNNER_VERSION}.tar.gz"
        url = f"https://github.com/actions/runner/releases/download/v{RUNNER_VERSION}/{package.name}"
        download(url, package, digest=config["sha256"], gh_proxy=args.gh_proxy,
                 env=proxy_environment(root))
        extract_archive(package, runner)
        write_private(marker, config["sha256"] + "\n")
    # Native runtime checks detect missing ICU/OpenSSL/etc. without running a root installer.
    env = proxy_environment(root)
    try:
        run([str(runner / "bin/Runner.Listener"), "--version"], cwd=runner, env=env)
        run([str(runner / "externals/node24/bin/node"), "--version"], env=env)
    except DeployError:
        if not args.setup_host:
            raise DeployError("Runner native dependencies are missing. Rerun with --setup-host to allow the SHA-verified official dependency installer") from None
        run(["sudo", "-n", "bash", str(runner / "bin/installdependencies.sh")], env=env, timeout=1200)
        run([str(runner / "bin/Runner.Listener"), "--version"], cwd=runner, env=env)
        run([str(runner / "externals/node24/bin/node"), "--version"], env=env)
    name = args.runner_name or re.sub(r"[^a-zA-Z0-9_.-]", "-", f"fla-npu-{args.platform}-{socket.gethostname()}")[:64]
    log_path = root / "tmp/runner-registration.log"
    with private_log(log_path) as log:
        try:
            result = subprocess.run(["bash", "./config.sh", "--url", args.repo_url, "--token", token,
                                     "--name", name, "--labels", config["labels"], "--work", "_work",
                                     "--unattended"], cwd=runner, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise DeployError("Runner registration failed/timed out; inspect private runner-registration.log") from None
    if result.returncode:
        raise DeployError("Runner registration failed; obtain a fresh repository registration token and inspect private runner-registration.log")
    existing_runner(root, args.repo_url, name)
    ensure_runner_service(root)
    return name


def build_image(args, root, config):
    if not args.source_dir:
        raise DeployError("--build-image requires --source-dir")
    source = Path(args.source_dir).resolve()
    dockerfile = args.dockerfile
    if not dockerfile:
        dockerfile = "ci/Dockerfile.ascend950" if args.platform == "a5" else "ci/Dockerfile"
    if not (source / dockerfile).is_file():
        raise DeployError("The requested Dockerfile does not exist")
    env = proxy_environment(root)
    command = ["docker", "build", "--network=host", "--file", str(source / dockerfile), "--tag", args.image]
    for variable in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        if env.get(variable):
            command += ["--build-arg", variable]
    command.append(str(source))
    # Build output is useful and contains no subscription or registration secret.
    result = subprocess.run(command, env=env, check=False)
    if result.returncode:
        raise DeployError("Docker image build failed")


def check_deployment(args, root, config):
    checks = preflight(root, args.image)
    checks["proxy_config"] = proxy_health(root)
    checks["proxy_active"] = service_active(PROXY_UNIT)
    checks["proxy_enabled"] = systemctl("is-enabled", PROXY_UNIT, check=False).returncode == 0
    name = None
    if not args.skip_runner:
        identity = existing_runner(root, args.repo_url, args.runner_name)
        checks["runner_identity"] = identity is not None
        name = identity["agentName"] if identity else None
        checks["runner_active"] = service_active(RUNNER_UNIT)
        checks["runner_enabled"] = systemctl("is-enabled", RUNNER_UNIT, check=False).returncode == 0
        unit = systemctl("cat", RUNNER_UNIT, check=False)
        checks["runner_unit_path"] = unit.returncode == 0 and f"WorkingDirectory={root}/actions-runner" in unit.stdout
    result = {"platform": args.platform, "ci_root": str(root), "image": args.image,
              "runner_name": name, "expected_custom_labels": config["labels"].split(","), "checks": checks,
              "ok": all(checks.values())}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--platform", choices=PLATFORMS, required=True)
    p.add_argument("--ci-root", default="/home/huangjunzhe/ci")
    p.add_argument("--repo-url", default="https://github.com/flashserve/flash-linear-attention-npu")
    p.add_argument("--runner-name")
    p.add_argument("--secret-file", help="Current-user-owned 0600 JSON with subscription_url and/or runner_token")
    p.add_argument("--skip-runner", action="store_true")
    p.add_argument("--check", action="store_true", help="Read-only validation; no install, service start or reconfiguration")
    p.add_argument("--setup-host", action="store_true", help="Allow one-time targeted sudo group/directory/linger setup")
    p.add_argument("--gh-proxy", default="https://ghfast.top")
    p.add_argument("--image")
    p.add_argument("--source-dir")
    p.add_argument("--build-image", action="store_true")
    p.add_argument("--dockerfile")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if platform.system() != "Linux" or os.getuid() == 0:
            raise DeployError("Run this command in an Ubuntu Linux login as the target ordinary user (not root)")
        config = PLATFORMS[args.platform]
        if platform.machine() != config["machine"]:
            raise DeployError(f"{args.platform} requires Linux {config['machine']}")
        root = validate_root(args.ci_root)
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?", args.repo_url):
            raise DeployError("--repo-url must be a GitHub repository HTTPS URL")
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?", args.gh_proxy):
            raise DeployError("--gh-proxy must be an HTTPS proxy origin")
        if args.runner_name and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.runner_name):
            raise DeployError("--runner-name must contain 1-64 letters, digits, _, . or -")
        args.image = args.image or config["image"]
        if args.check:
            if args.setup_host or args.build_image:
                raise DeployError("--check cannot be combined with mutating bootstrap/build options")
            return check_deployment(args, root, config)
        os.umask(0o077)
        args.secrets = load_secret_file(args.secret_file) if args.secret_file else {}
        if args.setup_host:
            setup_host(root)
        else:
            for directory in (root, root / "tmp", root / "cache_root"):
                try:
                    directory.mkdir(parents=True, exist_ok=True)
                except OSError:
                    raise DeployError("CI storage is not writable; use --setup-host once or ask the host administrator") from None
        require_checks(preflight(root, args.image, require_image=not args.build_image))
        ensure_proxy(args, root)
        if args.build_image:
            build_image(args, root, config)
        name = None if args.skip_runner else ensure_runner(args, root, config)
        print("Deployment ready" + (f": {name}" if name else ": proxy and host prerequisites"))
        return check_deployment(args, root, config)
    except DeployError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
