---
name: claude-as-subagent
description: "Delegate engineering, optimization, modeling, or analysis tasks to the official Claude Code CLI/TUI in a Linux sandbox. Requires root, bubblewrap, and mount/PID/network namespace support. The main agent prepares the task files, drives Claude through tmux or the native CLI, and verifies its results."
disable-model-invocation: false
---

# Claude as Sub-Agent

Prepare a dedicated working directory and launch the native `claude` CLI/TUI
with `scripts/enter.sh`. Commands below are relative to this skill directory.
The scripts prepare the execution environment; the main agent owns task scope,
file preparation, supervision, and acceptance of the results.

## Prepare the target host

Requires Linux, root/sudo, Python 3.11+, Node.js 20+, OpenSSL, and the `runuser`,
`unshare`, `mount`, `flock`, and `ip` commands. The installer installs
`bubblewrap` when missing and stages only the required Node/Claude runtime
files. It preserves existing certificates.

```bash
sudo scripts/install.sh
sudo scripts/enter.sh --doctor
```

`--doctor` must exit successfully. Both `--mode auto` and `--mode bwrap` require
full mount, PID, and network isolation. Startup fails when isolation is
unavailable; `--mode user` is rejected.

When hardware is on a remote machine, run these same commands on that machine.
Inside Docker or Podman, the container must permit these namespaces and mount
operations. Expose only the task directory, skill files, and needed devices;
keep host home directories and credentials outside the container.

## Prepare the task

Create a dedicated directory in persistent storage. On hosts with an agent
workspace, use its `workfiles/` directory; on other hosts, choose a persistent
task directory. Avoid `/tmp` for long-running work. The directory and its parent
path must not contain symbolic links.

Stage only what the task requires:

- Loose source files, scripts, datasets, or a minimal reproducer.
- One or more repository copies, each in its own subdirectory. Remove internal
    remotes and sensitive Git history; optionally initialize a clean local Git
    baseline for reviewing changes.
- An empty directory for a new project.

Inspect both the prompt and files for credentials, private addresses, personal
information, internal paths, and confidential identifiers. Remove unnecessary
sensitive information while preserving the actual technical context and data
needed to reproduce and verify the work. Do not mount the full host workspace.

Write a task brief with the objective, relevant inputs, constraints, validation
commands, and measurable acceptance criteria. Interact in English. If relevant
and accurate, include prior attempts and why their results were unsatisfactory.
Keep the task broad enough for independent exploration without hiding its
purpose or authorization.

## Launch and supervise

```bash
scripts/enter.sh [--api-key <key>] [--base-url <url>] [--model <model>] \
                 [--tz <timezone>] [--ip <fixture-ip>] [--proxy <url>] \
                 [--mode auto|bwrap] [--reset-id] <workdir> [command...]
```

- `--api-key`: defaults to `$CLAUDE_SUBAGENT_API_KEY` or `$ANTHROPIC_API_KEY`.
- `--base-url`: defaults to `$CLAUDE_SUBAGENT_BASE_URL`, `$ANTHROPIC_BASE_URL`, or
    `https://api.anthropic.com`. An optional path such as `/v1` is supported.
- `--model`: defaults to `$CLAUDE_SUBAGENT_MODEL`, `$CLAUDE_MODEL`, or
    `claude-opus-5-5`. The proxy maps `opus`/`fable` requests to this model and
    preserves `sonnet`/`haiku` requests.
- `--proxy`: defaults to `$https_proxy` or `$HTTPS_PROXY`. Pass `""` for direct
    connections without inheriting a proxy from the host environment.
- `--tz`: defaults to `America/Los_Angeles`.
- `--ip`: defaults to `104.28.14.92` for selected synthetic GeoIP responses.
    This does not change the real network egress IP.
- `--reset-id`: clears Claude session state and replaces the per-task identity
    seed while preserving workdir files. Stop the active sandbox first.

The key and real API URL are injected by the host-side proxy and are not placed
in sandbox settings. A key supplied on the command line can be visible to other
host users through process listings. Protect the host accordingly.

### Interactive TUI in tmux

The example uses tmux's multiple command arguments to preserve paths and values
without building an interpolated shell command. Set `CLAUDE_SUBAGENT_API_KEY`
from your API provider and choose the persistent `WORKDIR` prepared above.

```bash
WORKDIR="/path/to/dedicated/task"

tmux new-session -d -s cc sudo "$PWD/scripts/enter.sh" \
  --api-key "${CLAUDE_SUBAGENT_API_KEY:?Set your API key}" \
  --base-url "${CLAUDE_SUBAGENT_BASE_URL:-https://api.anthropic.com}" \
  --model "${CLAUDE_SUBAGENT_MODEL:-claude-opus-5-5}" "$WORKDIR" claude

tmux send-keys -t cc "Read TASK.md and work toward its acceptance criteria." Enter
tmux capture-pane -pt cc -S -100
```

Use native features such as `/effort high`, `/resume`, `claude --continue`,
`Escape`, and `Ctrl-C`. Inspect progress and outputs, then provide focused
follow-ups. Keep only one active sandbox per workdir; the launcher enforces this
with a lock. Separate workdirs may run concurrently.

For background jobs launched by Claude, record their PID and exit status.
Avoid `pgrep -f myjob.py` in a wrapper containing the same text: it can match
itself. Use `$!` with `wait` in the launching shell, or a pattern such as
`pgrep -f '[m]yjob.py'` when checking an existing process.

Have Claude save checkpoints, decisions, validation results, and remaining work
inside the workdir. Context capacity depends on the model and gateway; a
client-side `[1m]` declaration does not establish an upstream limit. If context
quality degrades, save progress and start a fresh native session from the notes.
Do not assume a universal 150k-token threshold or rotate identity merely to
manage context length.

### Headless execution

```bash
sudo scripts/enter.sh --api-key "${CLAUDE_SUBAGENT_API_KEY:?Set your API key}" \
  "$WORKDIR" claude -p "Read TASK.md, implement the task, and record validation results."
```

For `--output-format stream-json`, also pass `--verbose` as required by Claude
Code. Use its native CLI options instead of adding a custom task wrapper.

## Handle errors and refusals

The proxy sends SSE keepalives while it waits, retries transient network,
server, quota, and concurrency errors with a bounded budget, and cancels an
upstream worker on client disconnect or timeout. Complete refusal responses
and non-retryable errors are returned to the caller. Request messages are not
rewritten during retries.

If a safeguard refusal occurs, pause the delegation and preserve existing code,
progress notes, and validation results. Review the refusal reason, permissions,
and scope. Check the relevant prompts, files, and logs for unnecessary sensitive
information. Clarify the actual purpose and constraints when missing context
caused a misunderstanding, then continue only within the permitted scope.
If the restriction applies to the task, stop that branch and report the
limitation to the operator.

## Collect and verify

`<workdir>` is mounted in place as `/work`. Changes, including deletions, remain
on disk. Persistent Claude state is in the sibling `.<name>.cc-home` directory;
generated system files are per-launch, read-only mounts. State preparation
rejects unsafe directory links and does not truncate files through symlinks.

Read the outputs and diffs from the host, run independent validation, and apply
accepted changes to the real project. Exposed accelerator devices and kernel
drivers remain part of the host's trust boundary. Synthetic identity/GeoIP
values do not guarantee that every host characteristic is hidden.

Local regressions require no live model API:

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/enter.sh scripts/install.sh
```

The tests should all pass. On a prepared root host, set
`CLAUDE_SUBAGENT_TEST_SANDBOX=1` to include startup, lock, and cleanup checks.
Validate hardware access separately with the task's build and benchmark commands.
