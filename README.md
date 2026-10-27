# fla-npu-ci-deploy

在 Ubuntu NPU 主机上部署 Flash Linear Attention NPU CI，并在指定源码目录复现 CI。支持 A2 ARM64 / ascend910b、A5 X64 / ascend950；编译统一使用 `-j16`。

## 快速使用

以日常运行 CI 的普通用户（例如 `ubuntu`）登录主机：

```bash
git clone https://github.com/alvazu/fla-npu-ci-deploy.git /home/huangjunzhe/ci/fla-npu-ci-deploy
cd /home/huangjunzhe/ci/fla-npu-ci-deploy

# A5 部署；A2 将 a5 改为 a2。
bash deploy.sh --platform a5

# 在指定源码目录运行整套 quick CI，与当前工作目录无关。
bash test-ci.sh --platform a5 --repo /home/huangjunzhe/GDN/flash-linear-attention-npu
```

部署时按隐藏输入提示提供订阅地址和**新生成的仓库 runner 注册 token**；已有有效部署重复执行不需要重新输入，也不重启正在工作的 runner。测试入口默认加载 CI 根目录下的 `vpn-proxy.sh`。

## 主机准备与部署

主机需要 Ubuntu、Python 3.10+、systemd、Docker Engine、可用的 Ascend 驱动和 `npu-smi`。CI 的 CANN、PyTorch 等构建依赖位于对应 Docker 镜像。脚本检查主机环境，不替换驱动或已有 Docker 服务。

默认部署到 `/home/huangjunzhe/ci`，可用 `--ci-root` 指定其他绝对目录。主机第一次准备目录、Docker/设备访问组和用户服务自启动时，使用：

```bash
bash deploy.sh --platform a5 --setup-host
```

`--setup-host` 明确允许一次性的定向 sudo 操作：准备基础命令、访问组、目录权限和 linger。新增组尚未进入当前登录或 systemd 用户管理器时，脚本会返回明确错误，待现有用户任务结束并刷新登录会话后重新执行。脚本保留正在运行的用户服务。日常部署、检查和 CI 测试都使用普通用户，无需 sudo。

部署脚本统一创建 `ci/tmp`、`ci/cache_root` 和 `ci/tmp/npu-locks` 等持久基础目录；初始化时临时目录、缓存和设备锁目录使用 `2775`，现有目录与锁文件在普通部署重跑时保持原样。Workflow 直接使用部署好的主机环境，无需额外增加目录或权限检查步骤。宿主 `TMPDIR` 使用已准备好的静态 `ci/tmp` 根目录，清理时保留该目录；每次运行独立的 `CI_HOST_TMPDIR` 由 CI 容器调用脚本创建并按本次运行清理。

镜像默认要求已准备好：

| 平台 | 镜像 | 源码内的默认 Dockerfile |
| --- | --- | --- |
| A2 ARM64 | `fla-npu-ci:9.1.0-910b` | `ci/Dockerfile` |
| A5 X64 | `fla-npu-ci:9.1.0-950` | `ci/Dockerfile.ascend950` |

也可以在部署时从指定源码构建镜像：

```bash
bash deploy.sh --platform a5 --source-dir /path/to/flash-linear-attention-npu --build-image
```

支持 `--image NAME`、`--dockerfile PATH`。A5 构建必须使用匹配 ascend950 / X64 的配方；旧源码缺少对应文件时，指定正确的 `--dockerfile` 或先 `docker load` 导入镜像。

部署会完成：

- 下载并校验 GitHub Actions runner 2.338.0；分别注册 A2/A5 标签。
- 安装固定版本的官方 [clashctl](https://github.com/nelvko/clashctl)，导入订阅并创建 mihomo 用户服务。GitHub 下载失败时使用 `https://ghfast.top`。
- 部署由用户提供版本适配的 `vpn-proxy.sh`，保留 `proxyon` / `proxyoff` 命令。
- 创建 `fla-npu-runner.service` 用户服务并启用开机启动。幂等重复执行保留现有 runner 身份和活动任务。

| 平台 | 自动标签 | 自定义标签 |
| --- | --- | --- |
| A5 | `self-hosted`, `Linux`, `X64` | `npu`, `ascend950`, `flash-linear-attention-npu-a5` |
| A2 | `self-hosted`, `Linux`, `ARM64` | `npu`, `flash-linear-attention-npu` |

默认注册仓库为 `https://github.com/flashserve/flash-linear-attention-npu`，可通过 `--repo-url`、`--runner-name` 修改。已有身份与指定仓库或名称不一致时，脚本停止并保留原注册。

无人值守部署可通过环境变量 `FLA_CI_SUBSCRIPTION_URL`、`RUNNER_TOKEN`，或当前用户拥有、权限为 `0600` 的私有 JSON 传入凭据：

```json
{
  "subscription_url": "<填写订阅地址>",
  "runner_token": "<填写新的仓库注册 token>"
}
```

```bash
chmod 600 /path/to/ci.secrets.json
bash deploy.sh --platform a5 --secret-file /path/to/ci.secrets.json
```

私有配置放在仓库外。注册与订阅安装日志使用私有权限；注册 token 不写入服务 unit。`--skip-runner` 可仅部署代理及准备本地测试环境。

本地健康检查与服务管理：

```bash
bash deploy.sh --platform a5 --check
systemctl --user status fla-npu-runner.service mihomo.service
journalctl --user -u fla-npu-runner.service -n 80
source /home/huangjunzhe/ci/vpn-proxy.sh
proxyon
```

`--check` 只读检查目录、Docker、设备、用户管理器组权限、镜像、代理配置及本地服务状态。GitHub 的 online 状态和实际标签在仓库 Settings → Actions → Runners 查看；检查输出中的 `expected_custom_labels` 是预期配置。

## 指定目录、提交或 PR 的本地验证

`--repo` 指向当前用户拥有的 Git 仓库根目录。默认直接测试该目录当前内容，保留已有未提交修改；报告记录 diff 和未跟踪源码的哈希。构建会在该源码目录生成正常构建产物。同一源码目录的两个本地验证使用独占锁。

```bash
# 从任意当前目录调用工具，验证指定源码。
bash /home/huangjunzhe/ci/fla-npu-ci-deploy/test-ci.sh \
  --platform a5 --repo /path/to/flash-linear-attention-npu

# 只运行环境契约阶段，快速复现初始化、wheel/runtime 契约问题。
bash test-ci.sh --platform a5 --repo /path/to/flash-linear-attention-npu \
  --stage environment-contracts

# 将已有 Git ref 放到隔离 worktree 测试，保留原工作区。
bash test-ci.sh --platform a5 --repo /path/to/flash-linear-attention-npu \
  --ref feature-branch --base upstream/main

# 从 upstream 获取特定 PR 的 head，在隔离 worktree 运行整套 quick CI。
bash test-ci.sh --platform a5 --repo /path/to/flash-linear-attention-npu \
  --pr 123 --remote upstream --base upstream/main
```

`--pr` 使用 `refs/pull/<number>/head`，记录本次实际获取到的完整 SHA；`--ref` 使用已经存在的 Git ref。二者均不切换原工作区的分支。`--base` 记录指定比较基线与 merge-base，测试对象为所选择的 head。

默认执行六阶段：`environment-contracts`、`opp-package`、`standalone-layout`、`torch-adapter`、`gdr-example-st`、`scoped-overlay`，并开启 standalone wheel layout、scoped wheel install 和 Example/ST 检查。`--stage NAME` 执行截至该阶段的前缀；`--mode full` 切换完整模式。局部验证的报告始终标记 `full_ci_passed=false`。

CI 源码必须支持嵌套构建的 16 并发限制，参见主仓库 [PR #973](https://github.com/flashserve/flash-linear-attention-npu/pull/973)。旧 PR 需要借用已修复的 CI 时，显式指定可信 CI ref：

```bash
git -C /path/to/flash-linear-attention-npu fetch upstream \
  refs/pull/973/head:refs/remotes/upstream/ci-storage-fix

bash test-ci.sh --platform a5 --repo /path/to/flash-linear-attention-npu \
  --pr 123 --remote upstream --ci-ref upstream/ci-storage-fix
```

`--ci-ref` 仅在隔离 worktree 覆盖该 ref 的 `ci/` 和 `cmake/scripts/prepare.sh`，完整保存来源 SHA 和覆盖 diff，报告标识 `ci_override`。这类结果明确对应“PR 源码 + 指定 CI”，合入判断按报告里的源码与 CI 来源评估。

测试默认复用预置镜像，可加 `--build-image` 允许缺失镜像时构建。平台、模式、镜像、临时目录和 16 并发参数均由本次命令确定，避免继承线上遗留的 `CI_*` 参数。可用 `--no-proxy` 禁用自动代理加载。

设备健康策略默认与主 CI 一致：记录实际选中设备及健康状态，不额外要求必须为 `OK`。需要严格拒绝告警卡时添加 `--require-healthy-npu`；报告会记录本次策略与实际设备状态。

## 验证证据与目录

默认目录布局：

```text
/home/huangjunzhe/ci/
├── actions-runner/      # 注册身份与工作目录
├── clashctl/            # 私有订阅、mihomo 配置与工具
├── vpn-proxy.sh
├── cache_root/          # 多次测试复用的缓存
├── tmp/
│   ├── npu-locks/       # 与线上新 CI 共用的持久设备锁
│   ├── source-locks/    # 同一源码目录的本地验证锁
│   └── ...             # 每次运行独立的临时目录和隔离 worktree
└── results/<run-id>/
    ├── summary.md
    ├── evidence.json
    ├── ci.log
    ├── runner.log
    ├── stages.json
    ├── accuracy.json    # 执行精度阶段时生成
    ├── build-environment.json
    ├── tested-source.patch
    ├── tested-source-untracked.json
    └── SHA256SUMS
```

容器临时目录固定为 `/tmp/fla-npu-ci`，绑定本次宿主 `ci/tmp` 目录，以兼容 A5 BiSheng。结束或中断时，脚本仅停止本次命名容器、恢复源码/临时产物所有权、删除本次临时目录与隔离 worktree，保留缓存、设备锁和结果。

报告包含平台、SOC、镜像 ID、源码 SHA、dirty 状态、源码前后指纹、CI 覆盖来源、实际命令、构建参数、阶段及精度结果。整套通过要求六阶段全部成功、精度用例全部执行且通过、CI 退出码为零、源码运行前后稳定且清理成功。缺少报告、旧报告来源不匹配、源码中途变化或中断均返回非零并保存失败证据。

线上 CI 故障时，分别在 A2/A5 对同一 PR SHA 执行命令，将各自 `results/<run-id>` 的报告、日志和 `SHA256SUMS` 一并提供给维护者。依据报告中的 commit、平台、测试范围和 CI 来源判断本地验证是否覆盖拟合入内容。

## 工具自身测试

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n deploy.sh test-ci.sh scripts/vpn-proxy.sh
```

测试包括部署幂等/身份保护/归档和凭据处理，以及 mock Docker/NPU 的完整 CLI 流程：指定目录、真实 Git worktree、失败退出、缺失报告、局部验证和源码变化检测。fixture 不调用真实 NPU 构建。

A2/A5 的回归、部署检查及真实入口测试结果见[验证记录](docs/validation.md)。
