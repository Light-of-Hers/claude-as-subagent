# claude-as-subagent

[中文文档](./README_ZH.md)

An Agent Skill (`SKILL.md` + zero-trust Linux sandbox) that lets an orchestrating coding agent (Codex, Pi, OpenCode, etc.) delegate hard or long-horizon engineering tasks to the official **Claude Code CLI/TUI** inside an isolated, anonymized OS sandbox.

Instead of wrapping Claude Code in a rigid custom task runner, `claude-as-subagent` provides a thin, hardened sandbox entry point (`scripts/enter.sh`) so your main agent—or you—can drive native `claude` directly inside `tmux` or via `claude -p`.

## Key Features

- **Main-Agent-Owned Workdir Preparation & Sanitization**: The main agent creates a dedicated working directory for each task, stages the required loose files, minimal reproducers, or repositories, and uses persistent storage for long-running work. Repository copies have internal remotes and sensitive Git history removed, with an optional fresh `git init` for a clean diff baseline. The main agent removes credentials, internal addresses, personal information, confidential project names, and other sensitive material case by case while preserving the truthful technical context needed to understand and verify the task.

    If execution triggers a **safeguard refusal**, the main agent pauses the delegation, saves the code, progress notes, and verification results, reviews the refusal reason and the task's authorization, and checks the relevant prompts, files, and logs for unnecessary sensitive information. When unclear context caused a misunderstanding, it clarifies the actual purpose, permissions, and constraints and continues from the saved progress within the permitted scope. If the refusal reflects a genuine usage restriction, it stops that task branch and reports the limitation to the operator.

- **OS-Level Filesystem & Namespace Isolation**: Runs as an unprivileged user (`claude-runner`) inside a `bubblewrap` (`bwrap`) filesystem whitelist with separate mount, PID, IPC, UTS, and network namespaces. Startup fails if full isolation is unavailable. Persistent state is updated without following symbolic links, generated system files are mounted read-only, and a lock prevents concurrent launches on the same working directory.
- **Zero-Trust Credential & Gateway Injection**: Inside the sandbox, `.claude/settings.json` only ever sees the official `https://api.anthropic.com` endpoint and a format-valid synthetic `sk-ant-api03-...` placeholder token. The host-side `proxy_bridge.py` intercepts traffic over a UNIX domain socket, injects the real `--api-key`, and forwards `/v1/*` requests to your real `--base-url` in memory—preventing prompt injection or sandboxed commands from exfiltrating your real API key or gateway URL.
- **Host Environment Settings**: Provides a separate hostname, timezone, machine ID, and network namespace. Selected kernel, DMI, and GeoIP values are synthetic fixtures. These settings do not change the real egress IP or guarantee that all host or container characteristics are hidden.
- **Accelerator Passthrough**: Exposes `/dev` and read-only system/toolchain mounts, including CUDA and ROCm paths, while adding `claude-runner` to available `video`/`render` groups. Tasks can use permitted accelerator devices. Shared devices and kernel drivers remain part of the host's trust boundary.
- **Long-Horizon Resilience**:
    - Locks every `opus`/`fable` tier request to your chosen `--model` at the proxy layer (preventing Claude Code's own model downgrade/fallback), while leaving lightweight `sonnet`/`haiku` sub-agent requests untouched.
    - Sends SSE `: ping` keepalives every 5s at the proxy layer and transparently retries transient upstream 5xx/402/429 errors, TLS handshake timeouts, broken streams, and concurrency rate limits, finalizing as soon as an `event: message_stop` frame arrives.
    - Parses complete SSE events without matching error words inside ordinary text. Returns refusals and non-retryable errors to the caller; cancels the upstream worker when the client disconnects or its request times out.
    - Supports one-flag identity & session rotation (`--reset-id`) while keeping working directory files intact.

## Installation

### 1. Install as an Agent Skill

Clone into your skills directory (e.g. `~/.agents/skills/claude-as-subagent` or `~/.claude/skills/claude-as-subagent`):

```bash
git clone https://github.com/Light-of-Hers/claude-as-subagent.git ~/.agents/skills/claude-as-subagent
```

### 2. One-Time Sandbox Runtime Setup (Root)

Requires Linux, root/sudo, Python 3.11+, Node.js 20+, OpenSSL, and the
`runuser`, `unshare`, `mount`, `flock`, and `ip` commands. The installer installs
`bubblewrap` if needed. `--doctor` must succeed before use. In a container, the
runtime must permit the required mount, PID, and network namespaces.

```bash
sudo ~/.agents/skills/claude-as-subagent/scripts/install.sh
sudo ~/.agents/skills/claude-as-subagent/scripts/enter.sh --doctor
```

Optional environment variables during `install.sh`:

- `CLAUDE_SUBAGENT_NODE_SRC=/path/to/node-prefix`: specify source Node.js prefix if not in PATH or `~/.nvm`.
- `CLAUDE_SUBAGENT_HIDE_PATTERNS="regex"`: regex of internal binary or Python package names under `/usr/local/bin` and `dist-packages` to hide from the sandbox.

## Usage

### Interactive TUI in `tmux` (Recommended for Long-Horizon Tasks)

```bash
SKILL_DIR="$HOME/.agents/skills/claude-as-subagent"
WORKDIR="/path/to/dedicated/workdir"

# Set CLAUDE_SUBAGENT_API_KEY from your API provider before this command.
tmux new-session -d -s cc sudo "$SKILL_DIR/scripts/enter.sh" \
  --api-key "${CLAUDE_SUBAGENT_API_KEY:?Set your API key}" \
  --base-url "${CLAUDE_SUBAGENT_BASE_URL:-https://api.anthropic.com}" \
  --model "${CLAUDE_SUBAGENT_MODEL:-claude-opus-5-5}" "$WORKDIR" claude

# Send task instructions
tmux send-keys -t cc "Read TASK.md, build and benchmark iteratively until all targets are met." Enter

# Monitor output
tmux capture-pane -pt cc -S -100
```

### One-Shot Headless Execution

```bash
sudo ~/.agents/skills/claude-as-subagent/scripts/enter.sh \
  --api-key "$CLAUDE_SUBAGENT_API_KEY" \
  /path/to/dedicated/workdir \
  claude -p "Analyze the codebase and write your findings to REPORT.md."
```

### CLI Options (`scripts/enter.sh`)

| Option | Default | Description |
|---|---|---|
| `--api-key <key>` | `$CLAUDE_SUBAGENT_API_KEY` or `$ANTHROPIC_API_KEY` | Real upstream API key (injected on host side only) |
| `--base-url <url>` | `$CLAUDE_SUBAGENT_BASE_URL` or `https://api.anthropic.com` | Real upstream API base URL |
| `--model <model>` | `$CLAUDE_SUBAGENT_MODEL` or `claude-opus-5-5` | Primary model for `opus`/`fable` tier requests |
| `--tz <timezone>` | `America/Los_Angeles` | Sandbox timezone (`TZ`, `/etc/localtime`, `/etc/timezone`) |
| `--ip <ip>` | `104.28.14.92` | Public IP returned to in-sandbox GeoIP self-checks |
| `--proxy <url>` | `$https_proxy` | Upstream HTTP/HTTPS proxy (`""` for direct connection) |
| `--mode <mode>` | `auto` | `auto` and `bwrap` both require full isolation; `user` is rejected |
| `--reset-id` | off | Rotate sandbox `userID`/`machineID`/`/etc/machine-id` and clear `.claude` history while keeping `<workdir>` files |
| `--doctor` | off | Run environment diagnostics; exit nonzero when required isolation is unavailable |

Working directories must be dedicated, persistent directories without symbolic
links in their parent path. Files inside are preserved in place, including
deletions; runtime state stays in the sibling `.<name>.cc-home` directory.
Stop an active sandbox before using `--reset-id` on its working directory.
The API key passed as an argument may be visible to other host users via process
listings; the sandbox has a separate PID namespace. Protect the host accordingly.

## Verification

From the repository root, run the local protocol, state, and installation
regressions. They use temporary files, loopback servers, and dummy credentials:

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/enter.sh scripts/install.sh
```

All tests should pass. Runtime validation on a target host additionally requires
`sudo scripts/enter.sh --doctor` to exit successfully. Hardware access depends on
the host's device permissions and is not covered by these local tests.

On a prepared root host, also exercise namespace startup, concurrent-launch
rejection, termination cleanup, and in-place file changes:

```bash
sudo env CLAUDE_SUBAGENT_TEST_SANDBOX=1 python3 -m unittest discover -s tests -v
```

## License

MIT
