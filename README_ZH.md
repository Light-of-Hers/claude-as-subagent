# claude-as-subagent

[English README](./README.md)

将官方 **Claude Code CLI/TUI** 放入操作系统级零信任隔离沙箱中，作为上层主 Agent（Codex、Pi、OpenCode 等）的子代理（Sub-Agent）来执行高难度或长程工程任务的 Agent Skill。

`claude-as-subagent` 不引入臃肿的自定义任务封装层，而是提供极简的硬化沙箱入口 `scripts/enter.sh`：将准备好的独立工作目录挂载进沙箱后，主 Agent（或操作者）直接在 `tmux` 中操控原生的 `claude` TUI 或使用 `claude -p`。

## 核心特性

- **让 main-agent 自己进行 sub-agent 任务相关文件的准备与脱敏**：由主 Agent 按具体任务新建独立工作目录，放入必需的零散文件、最小复现样例或若干代码仓库；长任务目录放在持久存储上。准备仓库副本时移除内部 remote 与敏感 Git 历史，必要时重新 `git init` 建立干净的 diff 基线。主 Agent 逐项清理凭据、内部地址、个人信息、保密项目代号等敏感信息，并保留理解和验证任务所必需的真实技术背景。

    若执行中触发 **safeguard（安全防护拒绝）**，主 Agent 暂停当前委派，保存已有代码、进度和验证结果，复核拒绝原因及任务的授权范围，再检查相关提示词、文件与日志是否包含不必要的敏感信息。对背景不清导致的误解，如实补充用途、权限和约束，在允许的范围内从已保存的进度继续；涉及真实使用限制时，停止该任务分支并向操作者说明。

- **OS 级文件系统与命名空间隔离**：以非特权用户 `claude-runner` 运行在 `bubblewrap`（`bwrap`）文件系统白名单中，使用独立的 mount、PID、IPC、UTS 和网络命名空间。无法提供完整隔离时启动失败。更新持久状态时不跟随符号链接，生成的系统文件只读挂载，同一工作目录通过锁防止并发启动。
- **零信任凭据与网关内存注入**：沙箱内的 `.claude/settings.json` 固定只写入官方 `https://api.anthropic.com` 与高仿真 `sk-ant-api03-...` 占位密钥；真实的 `--api-key` 与 `--base-url` 仅保存在宿主机侧 `proxy_bridge.py` 进程内存中，通过 UNIX domain socket 透明改写请求，防止沙箱内命令或 Prompt Injection 窃取真实密钥与网关地址。
- **宿主机环境设置**：提供独立的主机名、时区、机器 ID 和网络命名空间；部分内核、DMI 与 GeoIP 响应使用合成值。这些设置不会改变真实出口 IP，也不保证隐藏全部宿主机或容器特征。
- **GPU / 加速器透传**：挂载 `/dev` 和只读的系统、工具链目录（包括 CUDA、ROCm 路径），并将 `claude-runner` 加入存在的 `video`/`render` 组，使任务能使用获准访问的加速器。共享设备和内核驱动仍属于宿主机的信任边界。
- **面向长程任务的稳定性优化**：
    - 在代理层将所有 `opus`/`fable` 档位请求锁定为指定的 `--model`（避免 claude-code 层的降档），同时保留内部 `sonnet`/`haiku` 轻量请求原样不变。
    - 代理层每 5 秒向客户端发送 `: ping` SSE 保活帧，对上游 5xx/402/429、TLS 握手超时、断流及并发限流自动退避重试，检测到 `event: message_stop` 自动收尾。
    - 按完整 SSE 事件解析响应，正文中引用的错误文字不会触发重试。拒绝和不可重试错误交给调用方处理；客户端断开或请求超时后终止上游工作进程。
    - 支持 `--reset-id` 一键轮换沙箱设备指纹并清空旧会话历史，同时完整保留 `<workdir>` 内的代码与进度文件。

## 快速开始

### 1. 安装 Skill

```bash
git clone https://github.com/Light-of-Hers/claude-as-subagent.git ~/.agents/skills/claude-as-subagent
```

### 2. 初始化沙箱底座（Root，幂等）

需要 Linux、root/sudo、Python 3.11+、Node.js 20+、OpenSSL，以及 `runuser`、`unshare`、`mount`、`flock`、`ip` 命令。安装器会在缺失时安装 `bubblewrap`；使用前必须通过 `--doctor` 检查。容器环境需要允许创建 mount、PID 和网络命名空间。

```bash
sudo ~/.agents/skills/claude-as-subagent/scripts/install.sh
sudo ~/.agents/skills/claude-as-subagent/scripts/enter.sh --doctor
```

### 3. 在 `tmux` 中启动隔离的 Claude Code TUI

```bash
SKILL_DIR="$HOME/.agents/skills/claude-as-subagent"
WORKDIR="/path/to/dedicated/workdir"

# 先将 API 提供方的密钥放入 CLAUDE_SUBAGENT_API_KEY。
tmux new-session -d -s cc sudo "$SKILL_DIR/scripts/enter.sh" \
  --api-key "${CLAUDE_SUBAGENT_API_KEY:?请设置 API key}" \
  --base-url "${CLAUDE_SUBAGENT_BASE_URL:-https://api.anthropic.com}" \
  --model "${CLAUDE_SUBAGENT_MODEL:-claude-opus-5-5}" "$WORKDIR" claude

# 发送任务或追问
tmux send-keys -t cc "Read TASK.md, build and benchmark iteratively until all targets are met." Enter

# 查看进度
tmux capture-pane -pt cc -S -100
```

工作目录应放在持久存储上，路径及父目录不得是符号链接。文件直接保存在原位置，删除操作也会保留；运行状态位于同级的 `.<name>.cc-home`。使用 `--reset-id` 前先停止该目录上的活动沙箱。`--mode auto` 和 `--mode bwrap` 均要求完整隔离，`--mode user` 会被拒绝。

通过参数传入的 API key 可能被其他宿主机用户通过进程列表看到；沙箱使用独立 PID 命名空间。请相应保护宿主机。完整参数说明见 [英文 README](./README.md#cli-options-scriptsentersh)。

## 验证

在仓库根目录运行本地回归；测试只使用临时文件、回环地址服务和占位凭据：

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/enter.sh scripts/install.sh
```

所有测试都应通过。目标机器上的运行验证还需要 `sudo scripts/enter.sh --doctor` 成功退出。硬件访问取决于宿主机设备权限，本地回归不验证硬件功能。

在已准备好环境的 root 主机上，还可以验证命名空间启动、并发拒绝、终止清理和原地文件修改：

```bash
sudo env CLAUDE_SUBAGENT_TEST_SANDBOX=1 python3 -m unittest discover -s tests -v
```

## 许可证

MIT
