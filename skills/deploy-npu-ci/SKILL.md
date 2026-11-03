---
name: deploy-npu-ci
description: 部署、检查或修复 fla-npu-ci-deploy 管理的 A2/A5 GitHub runner、clashctl 和 proxyon 环境；适用于指定 NPU 主机的 CI 环境准备。
---

# 部署 A2/A5 CI 环境

先定位 `fla-npu-ci-deploy` 工作副本，读取其 [README](../../README.md) 和 `bash deploy.sh --help`。以下路径均相对于该仓库；若技能被单独复制安装，先找到用户指定的部署仓库。用正式脚本完成部署，不在技能中另写安装器。

## 确定目标与已有部署

- 使用用户指定主机、SSH 身份和 CI 普通用户。A2 为 ARM64 / `ascend910b`，A5 为 x86_64 / `ascend950`；镜像、runner 标签以 `scripts/deploy.py` 为准。
- 确认 CI 根目录，默认 `/home/huangjunzhe/ci`。先运行 `bash deploy.sh --platform a5 --check`，查看现有 runner 身份、服务、目录和镜像。保留活动任务、已有注册和共享缓存。
- 日常命令以 CI 普通用户执行。需要准备组、目录或 linger 时使用 `--setup-host` 的定向 sudo；不要将整个部署以 root 运行。新增组未生效时按脚本提示刷新会话，先确认没有活动任务。

## 部署与验收

在用户已授权的目标上执行对应平台部署：

```bash
bash deploy.sh --platform a5 --setup-host
bash deploy.sh --platform a5 --check
```

主机已准备好时省略 `--setup-host`。只补齐代理时使用 `--proxy-only`，以 `--proxy-only --check` 验收；该模式不要求 Docker/NPU/镜像，也不注册或重启 runner。`--skip-runner` 则仍要求本地 CI 前置条件。已有镜像则复用；需要构建时使用 `--source-dir /path/to/source --build-image`。订阅和新的 runner 注册 token 通过脚本隐藏输入、环境变量或仓库外私有 `0600` JSON 传递；凭据不可进入 Git、技能、公开报告或日志。已有健康代理可复用，不为获取订阅而输出其私有配置。

代理只管理当前部署的用户服务。通过部署入口安装固定版本 clashctl 和 `vpn-proxy.sh`，验收时检查：

```bash
systemctl --user status mihomo.service fla-npu-runner.service
source /home/huangjunzhe/ci/vpn-proxy.sh
clashctl --help
proxyon
proxycheck
proxyoff
```

`proxyoff` 只清除当前 shell 的代理环境，不停止共享服务。部署会让交互 shell 加载 helper，不会登录即启用代理。现有 runner 与代理的所有者不同时，以实际 runner 用户验收，不迁移既有服务。网络验收既要通过 `--proxy-only --check` 确认代理真实访问 GitHub，也要用 `test-ci.sh --pr N --prepare-only` 确认源码获取先直连、失败后启用代理；不能仅以端口监听或 `proxyon` 退出码判断成功。失败分支可在隔离环境制造直连失败并验证回退，不改生产 Git 配置。记录每次尝试、最终 SHA 与失败原因，不在工作中的 runner 目录做 Git 实验。

完成后报告主机、CI 用户/根目录、服务和网络检查结果、对应源码 SHA、仍缺的依赖。部署健康不等于完整 NPU CI 已通过。若脚本因身份冲突、活动任务或缺少凭据停止，保留现场并说明具体阻碍；不要删除旧注册、停掉其他任务或循环重试。
