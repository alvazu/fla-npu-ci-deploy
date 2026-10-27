# 验证记录

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
