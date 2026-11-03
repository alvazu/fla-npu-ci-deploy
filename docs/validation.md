# 验证记录

## 2026-11-03：两台 A5 代理部署与源码链路验收

参考实际 A2 主机 `179.178.1.2` 的 clashctl 布局和 `mihomo.service` 用户服务，两台 A5 统一使用 `/home/huangjunzhe/ci/clashctl`、`vpn-proxy.sh` 和相同端口：HTTP 7899、mixed 7898、SOCKS 7891，仅本机监听。

| 项目 | 179.228.2.2 | 179.223.2.2 |
| --- | --- | --- |
| 代理服务用户 | cann | ubuntu，复用已有配置和服务 |
| systemd 用户服务 | active、enabled，linger=yes | active、enabled，linger=yes |
| 实际 runner | 原 root 系统服务保持 active | 原 root 系统服务保持 active |
| 普通用户与 root 的 `proxyon` / `proxycheck` / `clashctl --help` | 通过 | 通过 |
| `deploy.sh --platform a5 --proxy-only --check` | 全部通过 | 全部通过 |
| 工具 Linux 单元及 CLI fixture | 67 项全部通过 | 67 项全部通过 |
| 容器经宿主代理访问 GitHub HTTPS | 通过 | 通过 |
| 容器经宿主代理访问 Daily 底包 HTTPS 索引 | 通过 | 通过 |
| PR 源码首次直连 | 成功 | 60 秒超时后正确回退代理 |
| 隔离制造首次直连失败，代理真实抓取 | 通过 | 通过 |
| 固定 base/head 抓取与现场合并 | 通过 | 通过 |

228 从现有 A5 部署通过加密 SSH 流复用私有配置和同架构程序，配置不落到本地工作区。新建服务的资源目录使用 `clashctl/resources`。223 保持原代理 PID 5015，两个原 runner PID 4696 / 5286 保持运行；未重新注册、迁移或重启 runner。

真实 `test-ci.sh --pr 1041 --prepare-only --fetch-timeout 60` 源码检查记录如下。测试源码固定为 `15ca12c1f594e5b45fabe683ab8a8dce55d146fe`，原工作区未切换，隔离 worktree 均已清理。

| 主机 | 运行 ID | 抓取结果 |
| --- | --- | --- |
| 228 | `20261103T041543Z-58c48aec90b4` | direct=0 |
| 228 | `20261103T041625Z-805818c627e3` | 隔离注入 direct=97，真实 proxy=0 |
| 223 | `20261103T041003Z-a3a28f52c143` | direct 超时=124，真实 proxy=0 |
| 223 | `20261103T041108Z-177e3e789bec` | 隔离注入 direct=97，真实 proxy=0 |

随后提取主仓 workflow 的实际 PR 抓取与合并脚本执行，固定 base `34e36de0a35609042d5007b8444037543cf2ee0e` 和上述 PR head。首次 base checkout 用等价 Git 操作验收，在独立 PATH shim 中定向拒绝无代理 fetch，后续通过真实代理访问 GitHub；未修改生产 Git 配置。两台均确认最终合并提交包含指定 head。

主仓另修复了容器阶段重新加载代理、Docker 显式透传代理变量，以及 PR 抓取直连/代理超时；Linux 266 项主仓 CI 契约/行为测试全部通过。代理值采用 `docker -e NAME` 传递，不放进 Docker 命令参数。实际无 NPU 容器分别验证 GitHub 与 `https://mirrors.huaweicloud.com/ascend/repos/pypi/nightly/` 可达。

证据位于各主机：

- `/home/huangjunzhe/ci/tmp/network-check-20261103/results/<运行 ID>/`：`evidence.json`、源码指纹、日志与 `SHA256SUMS`。
- `/home/huangjunzhe/ci/tmp/deploy-tool-tests-final.log`：部署工具测试。
- `/home/huangjunzhe/ci/tmp/workflow-source-check.log`：真实抓取、固定 SHA 与合并检查。
- 223 的 `/home/huangjunzhe/ci/tmp/main-ci-proxy-tests-final.log`：主仓 266 项测试。

本次只验收部署、网络、源码和无 NPU 契约，不运行算子编译或 NPU 精度测试；`prepare-only` 的 `full_ci_passed` 均为 false。镜像内 ATK 及用例数量要求仍以主仓 CI 为准。

## 2026-10-27：初始工具验收

验证日期：2026-10-27。均由 Ubuntu 普通用户执行，CI 入口未使用 sudo。

## 工具回归与部署检查

| 检查 | A2 ARM64 | A5 X64 |
| --- | --- | --- |
| 工具自身单元/CLI fixture 测试 | 31 项通过 | 31 项通过 |
| 三个 Bash 脚本语法 | 通过 | 通过 |
| `deploy.sh --check` | 全部检查通过 | 全部检查通过 |
| Docker、设备与用户服务管理器权限 | 通过 | 通过 |
| 已有 runner / mihomo 服务 | active、enabled | active、enabled |

CLI fixture 使用真实 Git 仓库及 worktree，模拟 Docker/NPU 命令，覆盖整套成功、非零 CI 退出、缺失报告、局部阶段、已有 dirty 源码、运行中源码变化、隔离 worktree 回收及健康策略。

## 真实一键入口验证

从独立当前目录 `/home/ubuntu`，使用绝对工具路径和 `--repo` 指定另一源码目录，分别运行：

```bash
bash /path/to/fla-npu-ci-deploy/test-ci.sh \
  --platform a2 --repo /path/to/flash-linear-attention-npu \
  --stage environment-contracts
# A5 将 a2 改为 a5。
```

两个平台均使用干净源码提交 `087909a344d8e9df027cd11e06aa7154e7e500e9`，验证器脚本 SHA-256 为 `eaa3e26932c01b4b045d1434eba5dfab0006ad84b28c23f8a307b8e4c31c964e`。

| 结果 | A2 | A5 |
| --- | --- | --- |
| CI 退出码 / 证据状态 | 0 / success | 0 / success |
| 运行 ID | `20261027T050716Z-d7df4b07b52d` | `20261027T050913Z-4dfd5bbb95df` |
| 环境契约阶段 | success | success |
| `full_ci_passed` | false | false |
| `unmodified_commit_test` | true | true |
| 实际 NPU 健康状态 | OK | Alarm |
| Python | 3.10.12 | 3.12.3 |
| 本次宿主临时目录已清理 | 是 | 是 |
| 缓存与设备锁已保留 | 是 | 是 |
| 结果 `SHA256SUMS` 校验 | 通过 | 通过 |

真实测试仅执行环境契约阶段，其余五阶段正确标记 skipped。A5 的设备健康策略沿用主 CI 默认值，实际 Alarm 状态保留在日志和证据中；显式 `--require-healthy-npu` 会拒绝该卡。

完整日志、源码信息、镜像 ID、阶段结果及校验和保留在各主机 `/home/huangjunzhe/ci/results/<运行 ID>/`。工具的局部测试结果与此前主仓库 PR #973 的双机六阶段验证分别记录，不能将本表的局部结果视为一键工具重新跑完六阶段。

## 部署验证范围

本次复用两台已经部署的真实 runner/代理进行只读检查，未重新注册或重启它们。全新安装、异常续装、包校验、私有日志、身份冲突和活跃 runner 幂等保护通过隔离测试覆盖。
