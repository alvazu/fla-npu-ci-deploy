---
name: run-npu-ci
description: 使用 fla-npu-ci-deploy 复现 A2/A5 NPU CI、运行指定构建或 ATK 范围，并排查源码获取、代理、阶段报告和线上 /run-npu-ci 触发问题。
---

# 运行与排查 NPU CI

定位 `fla-npu-ci-deploy` 工作副本，读取 [README](../../README.md) 和 `bash test-ci.sh --help`。以下命令在部署仓库根目录执行；若技能被单独复制安装，先找到用户指定的部署仓库。构建和测试实现由待测源码的 `ci/` 维护，本工具只负责调用与取证。

## 选择真实的验证范围

普通用户运行，明确平台、源码目录和目标提交；`--pr N --remote upstream` 拉取 PR head 后在隔离 worktree 执行，`--ref REF` 选择已有本地 ref。`--base` 记录比较基线与 merge-base，**不合并**，所以本地 head 验证不能声称等同线上 base+PR 合并验证。保留 dirty 工作区，不覆盖其他版本的 CI 脚本。

```bash
bash test-ci.sh --platform a5 --repo /path/to/source \
  --pr 1041 --remote upstream --base upstream/main --mode full

bash test-ci.sh --platform a5 --repo /path/to/source \
  --build-kind incremental --build-ops chunk_fwd_o \
  --atk-ops chunk_fwd_o --base-ref main --device-id 0
```

本地默认 `--build-kind full`，不会自动模拟 GitHub 文件分类。按用户要求或线上失败报告填写 `full/incremental/torch/base`、`--build-ops`、`--atk-ops`；增量编译算子自动并入 ATK 集合，手动 ATK 在此基础上追加。显式 `--atk-ops null` 覆盖整个集合并跳过 ATK，不影响编译或其他阶段。底包模式的 `--base-ref` 必须对应真实 PR 目标分支。`--mode full` 强制全量，并比 quick 多运行完整 PyTorch 适配测试；环境、布局、GDR 和同一 ATK 集合的验证范围一致。`--device-id` 是宿主物理卡，容器测试使用逻辑设备 0。

Daily 底包与线上共用 `<ci-root>/pkg-cache`，默认 `/home/huangjunzhe/ci/pkg-cache`，由 `CI_PACKAGE_CACHE` 传入容器。按分支、SOC、CPU、Python/ABI、发布源和指定版本隔离；北京时间每日第一次需要底包时下载最新匹配 wheel，日内校验 SHA256 和元数据后复用。`--update-package` 为本次 base/incremental/torch 验证强制刷新，仍继续执行所选 CI；full 无需下载底包。刷新失败应修复源或网络，不沿用旧日包。

用户只要求单机刷新缓存时，使用待测主仓 `ci/update_package_cache.sh`，指定 `CI_BASE_REF`、`CI_SOC`、`CI_IMAGE`、`CI_PACKAGE_CACHE`，按需先加载代理。该入口在预置匹配镜像内以只读源码执行 `python ci/prepare_ci_package.py --update-package`，只下载校验，不安装、不编译、不申请 NPU；不要误用本地 `--update-package` 执行整套验证。

`--stage` 只执行截至目标阶段的前缀。需要排查初始化可用 `--stage environment-contracts`，但局部结果不能作为整套 CI 通过证据。六阶段、代理重试和取证语义以当前脚本为准；旧源码不支持当前契约时报告版本不匹配，不叠加其他 CI 目录绕过。

只验收拉取与源码契约时使用 `--pr N --prepare-only`，不启动 Docker/NPU，`full_ci_passed=false`。先无代理直连，失败后才加载代理重试；记录两次尝试与最终 SHA。需要触发失败分支时使用隔离环境，不改生产 Git/网络配置。

## 线上触发

用户明确要求触发线上 CI 时，才发送 PR 评论或启动 Actions；检查部署或本地运行本身不授予对外发评论权限。命令由主仓 [仓库规则](https://github.com/flashserve/flash-linear-attention-npu/blob/main/docs/repository-rules.md) 和所选 workflow 的实际实现决定：

```text
/run-npu-ci quick
/run-npu-ci quick atk-ops=chunk_fwd_o device-id=0
/run-npu-ci quick atk-ops=null
/run-npu-ci full
/update-package
/update-package ci_ref=<CI定义分支>
```

先读取所选 workflow 的实际权限和脚本来源策略：部分编译与缓存 PR 使用仓库 Admin 触发、固定 workflow SHA 的可信 CI，并保留 PR 合并后的 `ci/example_st_cases.json`；“独立权限与CI来源策略PR”使用默认分支 `.github/npu-ci-users.json` 名单和 PR 的 `ci/`，非 Markdown 的 `ci/`、`.github/` 修改还要求名单内 Admin 显式 `ci_modified=true`。本地工具始终使用指定 checkout 的 `ci/`，不能据此推断线上脚本来源。

`atk-ops` 追加自动选中集合，`atk-ops=null` 则覆盖整个集合并跳过 ATK；`full` 不自动扩大 ATK 集合。`ops=` 仅为 01/02 编译诊断，不满足正式门禁，不能与 `full` 或 `atk-ops` 组合。`/update-package` 刷新 PR 目标分支的底包，分别调度一个 A2 和一个 A5 runner，仅刷新被选主机的缓存；不编译、不安装、不运行 NPU 测试。其他同平台机器在其当天首次需要底包的 CI 自动刷新。触发及重跑权限遵循所选 workflow。

## 失败定位与证据

先看 `results/<run-id>/summary.md`、`evidence.json`、`ci.log` 和 `stages.json`，核对 head SHA、平台、SOC、镜像、构建/ATK 范围和退出码，再定位第一个失败阶段。源码拉取应有无代理直连和失败后代理重试证据；不要因网络失败改测别的 commit。完整日志、报告、源码指纹和 `SHA256SUMS` 是复现依据，不凭命令退出码或最后一行认定通过。

ATK 只经待测源码的 `tests/atk/run_test_cpu.sh` 调用；不从部署工具解析其用例资产。每个选中算子至少 200 个精度 case、perf/mss 各 1 个，最多 16 CPU/任务。缺资产、缺 ATK 环境、底包/ABI 不匹配等错误应给出修复项；不可降低门槛、跳过选中算子或直接覆盖完整底包来制造成功。

优先采用失败报告的复现命令。汇报区分网络/源码验收、局部阶段、整套本地 CI 和线上双平台门禁，并明确尚未执行的验证。
