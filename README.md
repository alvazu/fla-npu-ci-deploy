# fla-npu-ci-deploy

在 Ubuntu NPU 主机上部署 Flash Linear Attention NPU CI，并在指定源码目录复现 CI。支持 A2 ARM64 / ascend910b、A5 X64 / ascend950；编译统一使用 `-j16`。

## 快速使用

以日常运行 CI 的普通用户（例如 `ubuntu`）登录主机：

```bash
git clone https://github.com/alvazu/fla-npu-ci-deploy.git /home/huangjunzhe/ci/fla-npu-ci-deploy
cd /home/huangjunzhe/ci/fla-npu-ci-deploy

# A5 部署；A2 将 a5 改为 a2。
bash deploy.sh --platform a5

# 在指定源码目录运行 quick CI；本地默认完整 wheel 构建。
bash test-ci.sh --platform a5 --repo /home/huangjunzhe/GDN/flash-linear-attention-npu
```

部署时按隐藏输入提示提供订阅地址和**新生成的仓库 runner 注册 token**；已有有效部署重复执行不需要重新输入，也不重启正在工作的 runner。源码获取先直连，失败后使用 CI 根目录下的 `vpn-proxy.sh` 重试。

## 主机准备与部署

主机需要 Ubuntu、Python 3.10+、systemd、Docker Engine、可用的 Ascend 驱动和 `npu-smi`。CI 的 CANN、PyTorch 等构建依赖位于对应 Docker 镜像。脚本检查主机环境，不替换驱动或已有 Docker 服务。

默认部署到 `/home/huangjunzhe/ci`，可用 `--ci-root` 指定其他绝对目录。主机第一次准备目录、Docker/设备访问组和用户服务自启动时，使用：

```bash
bash deploy.sh --platform a5 --setup-host
```

`--setup-host` 明确允许一次性的定向 sudo 操作：准备基础命令、访问组、目录权限和 linger。新增组尚未进入当前登录或 systemd 用户管理器时，脚本会返回明确错误，待现有用户任务结束并刷新登录会话后重新执行。脚本保留正在运行的用户服务。日常部署、检查和 CI 测试都使用普通用户，无需 sudo。

部署脚本统一创建 `ci/tmp`、`ci/cache_root`、`ci/pkg-cache` 和 `ci/tmp/npu-locks` 等持久基础目录；初始化时临时目录、缓存和设备锁目录使用 `2775`，现有目录与锁文件在普通部署重跑时保持原样。Workflow 直接使用部署好的主机环境，无需额外增加目录或权限检查步骤。宿主 `TMPDIR` 使用已准备好的静态 `ci/tmp` 根目录，清理时保留该目录；每次运行独立的 `CI_HOST_TMPDIR` 由 CI 容器调用脚本创建并按本次运行清理。

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
- 安装固定版本的官方 [clashctl](https://github.com/nelvko/clashctl)，导入订阅并创建 mihomo 用户服务。下载先清空代理直连，失败后使用可用本地代理，最后使用 `https://ghfast.top`；固定归档始终校验哈希。
- 部署 `vpn-proxy.sh`，提供 `proxyon` / `proxyoff` / `proxycheck`；在当前用户 `.bashrc` 写入受管加载段，交互 shell 可直接调用，登录不自动开启代理。
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

私有配置放在仓库外。注册与订阅安装日志使用私有权限；注册 token 不写入服务 unit。`--skip-runner` 跳过 runner 注册，但仍检查本地 CI 的 Docker/NPU/镜像前置条件。

已有 runner 或只需要补齐代理时使用 `--proxy-only`，无需 runner token，也不要求 Docker/NPU/镜像：

```bash
bash deploy.sh --platform a5 --proxy-only --setup-host
bash deploy.sh --platform a5 --proxy-only --check
```

它只管理当前用户在指定 CI 根目录下的 clashctl、mihomo 用户服务和代理 helper，不替换、注册或重启既有 runner。首次主机基础准备才需要 `--setup-host`。同一机器的系统级 runner 与代理若属于不同用户，须让实际 runner 用户能够加载代理入口，并以该用户完成网络验收；不要为统一服务形式而迁移正在运行的 runner。

已有 clashctl 属于其他用户时，以原配置所有者执行部署。helper 提供的 `clashctl`、`proxyon` 会以配置所有者读取端口、调用官方 CLI 和用户服务；跨用户调用需要现有的 `sudo -n -u <配置所有者>` 权限，不放宽订阅配置权限。配置保存在 `clashctl/data`，mihomo 的资源工作目录固定为 `clashctl/resources`，避免启动时重新下载已有的地理数据。

本地健康检查与服务管理：

```bash
bash deploy.sh --platform a5 --check
systemctl --user status fla-npu-runner.service mihomo.service
journalctl --user -u fla-npu-runner.service -n 80
source /home/huangjunzhe/ci/vpn-proxy.sh
clashctl --help
proxyon
proxycheck
proxyoff
```

`--check` 只读检查目录、Docker、设备、用户管理器组权限、镜像、代理配置及本地服务状态。`--proxy-only --check` 仅检查代理部署，包含真实 HTTP 代理访问 GitHub HTTPS，不以端口监听代替网络成功。`proxycheck` 只检查代理；`proxyoff` 仅清空当前 shell 环境，保留 mihomo 服务。GitHub 的 online 状态和实际标签在仓库 Settings → Actions → Runners 查看；检查输出中的 `expected_custom_labels` 是预期配置。

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

`--pr` 获取源码时首次禁用继承代理直连；失败后才加载 `<ci-root>/vpn-proxy.sh` 并执行 `proxyon` 重试。`--proxy-script PATH` 可指定代理入口，`--no-proxy` 则禁用代理及代理重试。源码获取后记录实际完整 SHA，不因网络失败切换到其他提交。容器阶段按本次配置加载代理，用于包和依赖下载。

每次 Git fetch 默认最多等待 300 秒，可用 `--fetch-timeout N` 调整；直连超时也会进入代理重试，代理重试超时则明确失败。该限制仅作用于源码拉取，不限制编译或测试时长。

仅验收源码获取与代理重试链路、不运行 Docker/NPU，可添加 `--prepare-only`：

```bash
bash test-ci.sh --platform a5 --repo /path/to/flash-linear-attention-npu \
  --pr 1041 --remote upstream --prepare-only
```

该模式仍核验源码能力并保留 SHA、网络尝试和清理证据，但 `full_ci_passed=false`。直连成功时不会触发代理回退；需要验证失败分支时，使用隔离测试环境定向制造直连失败并核对代理重试成功，保留生产 Git/网络配置。

本地工具使用所选 PR/checkout 自身的 `ci/`，不提供 `--ci-ref` 覆盖；旧源码与当前构建/报告契约不兼容时直接提示修复源码。线上脚本来源由所选 workflow 决定，见下文策略说明。线上 CI 测试固定 base 与 PR 合并结果，本工具 `--pr` 直接测试 PR head，二者不能混为同一份验证。

### 编译、ATK 与设备范围

本地工具不调用 GitHub 文件分类，默认 `--build-kind full`。按线上失败报告或预期修改范围显式选择：

| 参数 | 行为 |
| --- | --- |
| `--build-kind full` | 通过主仓 `scripts/build_wheel.py` 构建并安装完整目标 SOC wheel |
| `--build-kind incremental --build-ops a,b` | 获取缓存中的完整 Daily 底包，只构建指定算子并经主仓 overlay 入口安全叠加 |
| `--build-kind torch` | 安装完整 Daily 底包，只重建 torch_custom |
| `--build-kind base` | 只安装完整 Daily 底包，不编译 |
| `--base-ref main` | 选择底包对应的真实 PR 目标分支，默认 `main`；维护分支使用对应 `vX.Y.Z` |
| `--update-package` | 使用底包的本次验证前强制刷新匹配缓存；全量编译无需底包，不下载 |
| `--atk-ops a,b` | 追加精度验证算子，与 `--build-ops` 合并去重；最终集合为空时不运行 ATK |
| `--atk-ops null` | 显式跳过 ATK，覆盖 `--build-ops` 自动加入的集合；编译与其他验证照常 |
| `--device-id N` | 指定宿主物理 NPU 编号；省略则自动择卡，容器测试使用逻辑设备 `0` |
| `--mode full` | 强制完整 wheel 构建，并额外运行全部 PyTorch 适配测试 |

```bash
# 修改算子的增量编译和对应 ATK 验证。
bash test-ci.sh --platform a5 --repo /path/to/flash-linear-attention-npu \
  --build-kind incremental --build-ops chunk_fwd_o \
  --atk-ops chunk_fwd_o --base-ref main --device-id 0

# 仅底包验证，不编译。
bash test-ci.sh --platform a5 --repo /path/to/flash-linear-attention-npu \
  --build-kind base --base-ref main

# 强制获取远程最新匹配底包，然后运行本次底包验证。
bash test-ci.sh --platform a5 --repo /path/to/flash-linear-attention-npu \
  --build-kind base --base-ref main --update-package
```

底包选择、分支/SOC/架构校验、ABI 检查和 overlay 由待测主仓 `ci/prepare_ci_package.py` 执行。完整底包来自华为云 nightly 源；匹配包缺失或 ABI 不兼容时应补发底包或改用完整构建，不能混用其他分支/平台包或将部分 `.run` 直接覆盖完整包。

本地与线上共用每台宿主的 `<ci-root>/pkg-cache`，默认 `/home/huangjunzhe/ci/pkg-cache`；工具设置 `CI_PACKAGE_CACHE`，容器内挂载为 `/ci-pkg-cache`。缓存按目标分支、SOC、CPU、Python/ABI 标签、发布源及指定版本隔离。北京时间每日第一次需要底包时下载远程最新匹配 wheel，日内校验 SHA256 和 wheel 元数据后复用；同维度下载使用进程锁，校验后原子发布。刷新失败明确报错，不沿用旧日底包。`--update-package` 传入 `CI_UPDATE_PACKAGE=true` 强制刷新，不是独立的只下载模式。

仅刷新单台机器、不运行编译和 NPU 验证时，使用待测主仓的镜像入口：

```bash
CI_PACKAGE_CACHE=/home/huangjunzhe/ci/pkg-cache CI_BASE_REF=main \
  CI_SOC=ascend950 CI_IMAGE=fla-npu-ci:9.1.0-950 \
  bash /path/to/flash-linear-attention-npu/ci/update_package_cache.sh
```

A2 改用 `CI_SOC=ascend910b CI_IMAGE=fla-npu-ci:9.1.0-910b`，并按需先加载代理。该入口要求预置匹配镜像，只读挂载源码，在镜像内执行 `python ci/prepare_ci_package.py --update-package`；只下载、校验并输出缓存版本，不安装、不占用 NPU。

默认顺序执行六阶段：`environment-contracts`、`opp-package`、`standalone-layout`、`torch-adapter`、`gdr-example-st`、`atk-accuracy`。未选 ATK 时，最后阶段必须以 `no ATK operators selected` 明确跳过；选中时必须成功。`--stage NAME` 执行截至该阶段的前缀；局部验证始终标记 `full_ci_passed=false`。quick/full 都检查环境、已安装布局、GDR 和同一选中集合的 ATK；除编译外，full 额外执行全部 PyTorch 适配测试。

ATK 只通过源码中的 `tests/atk/run_test_cpu.sh` 调用。每个选中算子需至少 200 个精度 case、perf 和 mss 各 1 个；实际执行全部精度 case，最多 16 个 CPU/并发任务。镜像必须已安装并配置 ATK 及其 CPU 标杆依赖，具体依赖以源码 `tests/atk/README.md` 为准；部署代理并不意味着镜像具备 ATK。

测试默认复用预置镜像，可加 `--build-image` 允许缺失镜像时构建。平台、模式、构建与 ATK 范围、镜像、临时目录和 16 并发参数均由本次命令确定，避免继承线上遗留的 `CI_*` 参数。

设备健康策略默认与主 CI 一致：记录实际选中设备及健康状态，不额外要求必须为 `OK`。需要严格拒绝告警卡时添加 `--require-healthy-npu`；报告会记录本次策略与实际设备状态。

## 验证证据与目录

默认目录布局：

```text
/home/huangjunzhe/ci/
├── actions-runner/      # 注册身份与工作目录
├── clashctl/            # 私有订阅、mihomo 配置与工具
├── vpn-proxy.sh
├── cache_root/          # 多次测试复用的编译缓存
├── pkg-cache/           # 按北京时间每日刷新的完整 Daily wheel 缓存
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
    ├── atk-results/     # 选中 ATK 时保留入口生成的报告
    ├── build-environment.json
    ├── tested-source.patch
    ├── tested-source-untracked.json
    └── SHA256SUMS
```

容器临时目录固定为 `/tmp/fla-npu-ci`，绑定本次宿主 `ci/tmp` 目录，以兼容 A5 BiSheng。结束或中断时，脚本仅停止本次命名容器、恢复源码/临时产物所有权、删除本次临时目录与隔离 worktree，保留缓存、设备锁和结果。

报告包含平台、SOC、镜像 ID、源码 SHA、dirty 状态、源码前后指纹、实际命令、编译/ATK 范围、设备选择、阶段及精度结果。整套通过要求所有必需阶段成功、未选 ATK 有明确跳过原因、GDR 及选中 ATK 用例通过、CI 退出码为零、源码运行前后稳定且清理成功。缺少报告、旧报告来源不匹配、源码中途变化或中断均返回非零并保存失败证据。

线上 CI 故障时，分别在 A2/A5 对同一 PR SHA 执行命令，将各自 `results/<run-id>` 的报告、日志和 `SHA256SUMS` 一并提供给维护者。依据报告中的 commit、平台、镜像和测试范围判断本地验证是否覆盖拟合入内容。

## 线上 CI 与技能

线上文件分类、触发权限、脚本来源和双平台状态规则以主仓 [仓库规则](https://github.com/flashserve/flash-linear-attention-npu/blob/main/docs/repository-rules.md) 及所选 Actions 定义分支的实际实现为准。部分编译与缓存 PR 使用仓库 Admin 触发、固定 workflow SHA 的可信 `ci/`，仅保留合并后 PR 的 `ci/example_st_cases.json`；“独立权限与CI来源策略PR”改为独立用户名名单触发、使用 PR 的 `ci/`，并要求 CI 文件修改由名单内 Admin 显式 `ci_modified=true` 确认。本工具不改变线上策略或分支保护。

```text
/run-npu-ci quick
/run-npu-ci quick atk-ops=chunk_fwd_o device-id=0
/run-npu-ci quick atk-ops=null
/run-npu-ci full
/update-package
/update-package ci_ref=<CI定义分支>
```

`atk-ops` 追加自动集合，`atk-ops=null` 则覆盖整个集合并跳过 ATK；`full` 不自动扩大 ATK 范围。`ops=` 只运行 01/02 编译诊断，不满足正式门禁；不能和 `full`、`atk-ops` 组合。每次触发及最终结果新增评论。

`/update-package` 刷新 PR 目标分支的 Daily 底包，可选 `ci_ref` 选择 Actions 定义分支。任务各调度一个 A2 和一个 A5 runner，只刷新被选主机的匹配缓存；不编译、不安装、不运行 NPU 测试。其他同平台主机在其当天首次需要底包的 CI 自动刷新；该命令不代表整个平台所有主机均已刷新。触发与重跑遵循所选 workflow 的权限策略。

本仓提供两个可由 Codex 等代理按路径读取的技能：

- [deploy-npu-ci](skills/deploy-npu-ci/SKILL.md)：部署、检查与修复 A2/A5 runner、clashctl、proxyon。
- [run-npu-ci](skills/run-npu-ci/SKILL.md)：选择构建/ATK 范围、复现本地 CI、按证据排查线上问题。

技能调用本仓正式脚本，不携带凭据，也不复制安装实现。单独安装技能目录后，仍需定位本仓工作副本。运行本地验证或检查部署不会自动授权代理发送 GitHub 评论；用户明确要求线上触发时才执行。

## 工具自身测试

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n deploy.sh test-ci.sh scripts/vpn-proxy.sh
```

测试包括部署幂等/身份保护/归档和凭据处理，以及 mock Docker/NPU 的完整 CLI 流程：指定目录、真实 Git worktree、失败退出、缺失报告、局部验证和源码变化检测。fixture 不调用真实 NPU 构建。

A2/A5 的回归、部署检查及真实入口测试结果见[验证记录](docs/validation.md)。
