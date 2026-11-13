#!/usr/bin/env bash
# Launch native Claude Code in a filesystem + PID + network namespace sandbox.
# <workdir> is mounted in place. Persistent state lives in .<name>.cc-home.
# Missing isolation is a startup error; there is no unisolated user fallback.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RUNNER_USER="${CLAUDE_SUBAGENT_USER:-claude-runner}"
NODE_HOME_DEFAULT="${CLAUDE_SUBAGENT_NODE_HOME:-/opt/node}"
API_KEY="${CLAUDE_SUBAGENT_API_KEY:-${ANTHROPIC_API_KEY:-}}"
BASE_URL="${CLAUDE_SUBAGENT_BASE_URL:-${ANTHROPIC_BASE_URL:-https://api.anthropic.com}}"
MODEL="${CLAUDE_SUBAGENT_MODEL:-${CLAUDE_MODEL:-claude-opus-5-5}}"
TZ_VAL="${CLAUDE_SUBAGENT_TZ:-${CLAUDE_TZ:-America/Los_Angeles}}"
FAKE_IP="${CLAUDE_SUBAGENT_FAKE_IP:-${CLAUDE_FAKE_IP:-104.28.14.92}}"
PROXY_VAL="${https_proxy:-${HTTPS_PROXY:-}}"
SANDBOX_MODE="${CLAUDE_SANDBOX_MODE:-auto}"
RESET_ID=0
DOCTOR_ONLY=0

usage() {
	cat <<'EOF'
Usage:
  enter.sh [options] <workdir> [command...]
  enter.sh --doctor

Options:
  --api-key <key>       API key (default: CLAUDE_SUBAGENT_API_KEY or ANTHROPIC_API_KEY)
  --base-url <url>      Upstream API base URL (default: https://api.anthropic.com)
  --model <model>       Primary model (default: claude-opus-5-5)
  --tz <timezone>       Sandbox timezone (default: America/Los_Angeles)
  --ip <ip>             IP returned by GeoIP fixtures (default: 104.28.14.92)
  --proxy <url>         Upstream HTTP(S) proxy (default: https_proxy; "" means direct)
  --mode auto|bwrap     Both modes require full namespace isolation
  --reset-id           Reset sandbox identity/session state; preserve workdir files
  --doctor             Check prerequisites and exit nonzero if isolation is unavailable
  -h, --help           Show this help
EOF
}

need_value() {
	if [[ $# -lt 2 || -z "$2" ]]; then
		echo "Error: $1 requires a value" >&2; exit 2
	fi
}
while [[ $# -gt 0 ]]; do
	case "$1" in
		--api-key) need_value "$@"; API_KEY="$2"; shift 2 ;;
		--api-key=*) API_KEY="${1#*=}"; shift ;;
		--base-url) need_value "$@"; BASE_URL="$2"; shift 2 ;;
		--base-url=*) BASE_URL="${1#*=}"; shift ;;
		--model) need_value "$@"; MODEL="$2"; shift 2 ;;
		--model=*) MODEL="${1#*=}"; shift ;;
		--tz) need_value "$@"; TZ_VAL="$2"; shift 2 ;;
		--tz=*) TZ_VAL="${1#*=}"; shift ;;
		--ip) need_value "$@"; FAKE_IP="$2"; shift 2 ;;
		--ip=*) FAKE_IP="${1#*=}"; shift ;;
		--proxy)
			if [[ $# -lt 2 ]]; then echo 'Error: --proxy requires a value (use "" for direct)' >&2; exit 2; fi
			PROXY_VAL="$2"; shift 2 ;;
		--proxy=*) PROXY_VAL="${1#*=}"; shift ;;
		--mode) need_value "$@"; SANDBOX_MODE="$2"; shift 2 ;;
		--mode=*) SANDBOX_MODE="${1#*=}"; shift ;;
		--reset-id) RESET_ID=1; shift ;;
		--doctor) DOCTOR_ONLY=1; shift ;;
		-h|--help) usage; exit 0 ;;
		--) shift; break ;;
		-*) echo "Unknown option: $1" >&2; exit 2 ;;
		*) break ;;
	esac
done

if [[ "${EUID}" -ne 0 ]]; then
	echo 'Error: run enter.sh as root (or with sudo)' >&2; exit 1
fi
case "${SANDBOX_MODE}" in
	auto|bwrap) ;;
	user) echo 'Error: user mode cannot isolate host processes or credentials; use a host/container with namespace support' >&2; exit 2 ;;
	*) echo 'Error: --mode must be auto or bwrap' >&2; exit 2 ;;
esac

can_isolate() {
	command -v bwrap >/dev/null 2>&1 || return 1
	unshare -m --propagation private -n -p -f --kill-child bash -c '
		set -e
		mount -t proc proc /proc -o nosuid,nodev,noexec,relatime,hidepid=2
		ip link set lo up
		runuser -u "$1" -- bwrap --unshare-ipc --unshare-uts \
			--ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/sbin /sbin \
			--symlink usr/lib /lib --symlink usr/lib64 /lib64 \
			--ro-bind /proc /proc --dev-bind /dev /dev /usr/bin/true
	' _ "${RUNNER_USER}" >/dev/null 2>&1
}
NODE_HOME=""
for p in "${NODE_HOME_DEFAULT}" /usr/local /usr; do
	if [[ -x "${p}/bin/node" && -x "${p}/bin/claude" ]]; then NODE_HOME="$p"; break; fi
done
CA_DIR="${NODE_HOME_DEFAULT}/ca"
if [[ "${DOCTOR_ONLY}" -eq 1 ]]; then
	missing=0
	if getent passwd "${RUNNER_USER}" >/dev/null; then echo 'runner: OK'; else echo 'runner: MISSING'; missing=1; fi
	if [[ -n "${NODE_HOME}" ]]; then echo "runtime: ${NODE_HOME}"; else echo 'runtime: MISSING'; missing=1; fi
	if can_isolate; then echo 'filesystem/PID/network isolation: OK'; else echo 'filesystem/PID/network isolation: UNAVAILABLE'; missing=1; fi
	if [[ -f "${CA_DIR}/geoip.key" && -f "${CA_DIR}/ca-bundle.crt" ]]; then echo 'proxy certificate: OK'; else echo 'proxy certificate: MISSING'; missing=1; fi
	exit "${missing}"
fi
if [[ $# -lt 1 ]]; then usage >&2; exit 2; fi
if [[ -z "${API_KEY}" || -z "${MODEL}" ]]; then echo 'Error: API key and model must be nonempty' >&2; exit 2; fi
if ! getent passwd "${RUNNER_USER}" >/dev/null || [[ -z "${NODE_HOME}" ]]; then
	echo "Error: run sudo ${SCRIPT_DIR}/install.sh first" >&2; exit 1
fi
if ! can_isolate; then
	echo 'Error: full bubblewrap + mount/PID/network isolation is unavailable; use a host or container that permits it' >&2
	exit 1
fi
if [[ ! -f "${CA_DIR}/geoip.key" || ! -f "${CA_DIR}/geoip.crt" || ! -f "${CA_DIR}/ca-bundle.crt" ]]; then
	echo "Error: proxy certificates missing; run sudo ${SCRIPT_DIR}/install.sh" >&2; exit 1
fi
if [[ "${TZ_VAL}" == /* || "${TZ_VAL}" == *..* || ! -f "/usr/share/zoneinfo/${TZ_VAL}" ]]; then
	echo 'Error: unknown timezone' >&2; exit 2
fi
if [[ -n "${PROXY_VAL}" && "${PROXY_VAL}" != *://* ]]; then PROXY_VAL="http://${PROXY_VAL}"; fi

WORKDIR_ARG="$1"; shift
ABS_WORKDIR="$(python3 -I "${SCRIPT_DIR}/sandbox_state.py" mkdir "${WORKDIR_ARG}")"
HOST_SANDBOX_HOME="$(dirname "${ABS_WORKDIR}")/.$(basename "${ABS_WORKDIR}").cc-home"
RUNNER_UID="$(id -u "${RUNNER_USER}")"
RUNNER_GID="$(id -g "${RUNNER_USER}")"
if [[ "${RUNNER_UID}" -eq 0 ]]; then echo 'Error: runner must not be root' >&2; exit 2; fi
if [[ $# -gt 0 ]]; then CMD=("$@"); else CMD=(/usr/bin/bash); fi

# Root-only lock directory protects lock files from replacement by the runner.
LOCK_DIR=/run/claude-as-subagent
if [[ -L "${LOCK_DIR}" ]]; then echo 'Error: unsafe lock directory' >&2; exit 1; fi
install -d -m 700 -o root -g root "${LOCK_DIR}"
LOCK_NAME="$(printf '%s' "${ABS_WORKDIR}" | sha256sum | cut -d ' ' -f 1)"
exec 8>"${LOCK_DIR}/${LOCK_NAME}.lock"
if ! flock -n 8; then echo 'Error: this workdir already has an active sandbox' >&2; exit 1; fi

RUN_DIR="$(mktemp -d /tmp/claude-as-subagent.XXXXXXXX)"
chmod 711 "${RUN_DIR}"
PROXY_PID=""
SANDBOX_PID=""
cleanup() {
	trap - EXIT INT TERM
	# unshare --fork blocks TERM while waiting. Killing its supervisor activates
	# --kill-child, which terminates PID 1 and therefore the whole PID namespace.
	if [[ -n "${SANDBOX_PID}" ]]; then kill -KILL "${SANDBOX_PID}" 2>/dev/null || true; fi
	if [[ -n "${PROXY_PID}" ]]; then kill "${PROXY_PID}" 2>/dev/null || true; fi
	for pid in "${SANDBOX_PID}" "${PROXY_PID}"; do
		if [[ -n "$pid" ]]; then wait "$pid" 2>/dev/null || true; fi
	done
	rm -rf -- "${RUN_DIR}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
SANDBOX_ETC="${RUN_DIR}/etc"
mkdir -m 755 "${SANDBOX_ETC}"
python3 -I "${SCRIPT_DIR}/sandbox_state.py" "${HOST_SANDBOX_HOME}" "${SANDBOX_ETC}" \
	"${MODEL}" "${ABS_WORKDIR}" "${TZ_VAL}" "${RUNNER_UID}" "${RUNNER_GID}" "${RESET_ID}"

if [[ -n "${SUDO_USER:-}" ]] && command -v setfacl >/dev/null; then
	setfacl -R -m "u:${SUDO_USER}:rwX" -d -m "u:${SUDO_USER}:rwX" "${ABS_WORKDIR}" 2>/dev/null || true
fi
if command -v git >/dev/null; then
	if ! git config --global --get-all safe.directory 2>/dev/null | grep -Fxq -- "${ABS_WORKDIR}"; then
		git config --global --add safe.directory "${ABS_WORKDIR}"
	fi
fi
PATH_DIRS=("${NODE_HOME}/bin")
for d in /usr/local/cuda/bin /opt/rocm/bin /usr/local/Ascend/ascend-toolkit/latest/bin /usr/local/sbin /usr/local/bin /usr/sbin /usr/bin /sbin /bin; do
	[[ -d "$d" ]] && PATH_DIRS+=("$d")
done
SANDBOX_PATH="$(IFS=:; echo "${PATH_DIRS[*]}")"
LD_DIRS=()
for d in /usr/local/cuda/lib64 /usr/local/cuda/targets/x86_64-linux/lib /opt/rocm/lib /usr/local/Ascend/ascend-toolkit/latest/lib64 /usr/local/lib; do
	[[ -d "$d" ]] && LD_DIRS+=("$d")
done
SANDBOX_LD="$(IFS=:; echo "${LD_DIRS[*]}")"

PROXY_SOCK="${RUN_DIR}/proxy.sock"
REAL_BASE_URL="${BASE_URL}" REAL_API_KEY="${API_KEY}" FORCE_CLAUDE_MODEL="${MODEL}" UPSTREAM_PROXY_URL="${PROXY_VAL}" \
	python3 -I "${SCRIPT_DIR}/proxy_bridge.py" unix "${PROXY_SOCK}" '' 0 "${FAKE_IP}" "${TZ_VAL}" "${CA_DIR}" 8>&- &
PROXY_PID=$!
for _ in {1..100}; do
	[[ -S "${PROXY_SOCK}" ]] && break
	if ! kill -0 "${PROXY_PID}" 2>/dev/null; then echo 'Error: proxy failed to start' >&2; exit 1; fi
	sleep 0.05
done
if [[ ! -S "${PROXY_SOCK}" ]]; then echo 'Error: proxy startup timed out' >&2; exit 1; fi
chown "${RUNNER_UID}:${RUNNER_GID}" "${PROXY_SOCK}"
unset API_KEY BASE_URL PROXY_VAL

MNT_BASE="${RUN_DIR}/mounts"
mkdir -p "${MNT_BASE}/work" "${MNT_BASE}/home"
BWRAP_ARGS=(
	--unshare-ipc --unshare-uts --hostname workstation --die-with-parent
	--tmpfs /tmp --dev-bind /dev /dev --ro-bind /sys /sys
	--ro-bind /proc /proc --ro-bind "${SANDBOX_ETC}/proc_version" /proc/version
	--ro-bind "${SANDBOX_ETC}/osrelease" /proc/sys/kernel/osrelease
	--ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/sbin /sbin
	--symlink usr/lib /lib --symlink usr/lib64 /lib64
	--ro-bind-try /usr/local /usr/local --ro-bind-try /etc/alternatives /etc/alternatives
	--ro-bind-try /etc/ld.so.cache /etc/ld.so.cache --ro-bind-try /etc/ld.so.conf /etc/ld.so.conf
	--ro-bind-try /etc/ld.so.conf.d /etc/ld.so.conf.d --ro-bind-try /etc/ssl /etc/ssl
	--ro-bind-try /etc/ca-certificates /etc/ca-certificates --ro-bind-try /etc/pki /etc/pki
	--ro-bind "${CA_DIR}/ca-bundle.crt" /etc/ssl/certs/ca-certificates.crt
	--ro-bind-try /etc/nsswitch.conf /etc/nsswitch.conf
)
for name in hostname machine-id hosts resolv.conf passwd group timezone; do
	BWRAP_ARGS+=(--ro-bind "${SANDBOX_ETC}/${name}" "/etc/${name}")
done
BWRAP_ARGS+=(--ro-bind "/usr/share/zoneinfo/${TZ_VAL}" /etc/localtime)
for d in /sys/devices/virtual/dmi/id /sys/hypervisor; do
	[[ -d "$d" ]] && BWRAP_ARGS+=(--tmpfs "$d")
done
for runtime in "${NODE_HOME}" "${NODE_HOME_DEFAULT}"; do
	BWRAP_ARGS+=(--ro-bind "${runtime}" "${runtime}")
done
for opt_dir in /opt/rocm* /opt/amdgpu* /opt/intel*; do
	if [[ -e "${opt_dir}" ]]; then BWRAP_ARGS+=(--ro-bind "${opt_dir}" "${opt_dir}"); fi
done
CLEAN_LOCAL="${NODE_HOME_DEFAULT}/clean-usr-local"
if [[ -d "${CLEAN_LOCAL}" ]]; then
	BWRAP_ARGS+=(--ro-bind "${CLEAN_LOCAL}" "${CLEAN_LOCAL}"
		--ro-bind-try /usr/local/bin "${CLEAN_LOCAL}/.backing/bin" --ro-bind "${CLEAN_LOCAL}/bin" /usr/local/bin)
	for dp in /usr/local/lib/python3*/dist-packages; do
		if [[ -d "$dp" ]]; then
			BWRAP_ARGS+=(--ro-bind "$dp" "${CLEAN_LOCAL}/.backing/dist-packages"
				--ro-bind "${CLEAN_LOCAL}/dist-packages" "$dp")
			break
		fi
	done
fi
BWRAP_ARGS+=(
	--bind "${MNT_BASE}/home" /home/dev --bind "${MNT_BASE}/work" /work --chdir /work --clearenv
	--setenv HOME /home/dev --setenv USER dev --setenv LOGNAME dev --setenv TERM "${TERM:-xterm-256color}"
	--setenv LANG C.UTF-8 --setenv LC_ALL C.UTF-8 --setenv TZ "${TZ_VAL}"
	--setenv PATH "${SANDBOX_PATH}" --setenv LD_LIBRARY_PATH "${SANDBOX_LD}"
	--setenv NODE_OPTIONS --dns-result-order=ipv4first
	--setenv NODE_EXTRA_CA_CERTS /etc/ssl/certs/ca-certificates.crt
	--setenv SSL_CERT_FILE /etc/ssl/certs/ca-certificates.crt
	--setenv REQUESTS_CA_BUNDLE /etc/ssl/certs/ca-certificates.crt
	--setenv CURL_CA_BUNDLE /etc/ssl/certs/ca-certificates.crt
	--setenv CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC 1
	--setenv HTTPS_PROXY http://127.0.0.1:18118 --setenv HTTP_PROXY http://127.0.0.1:18118
	--setenv https_proxy http://127.0.0.1:18118 --setenv http_proxy http://127.0.0.1:18118
	--setenv NO_PROXY localhost,127.0.0.1 --setenv no_proxy localhost,127.0.0.1
)
if [[ -f "${NODE_HOME_DEFAULT}/lib/libfakeuname.so" ]]; then
	printf '%s\n' "${NODE_HOME_DEFAULT}/lib/libfakeuname.so" >"${SANDBOX_ETC}/ld.so.preload"
	chmod 644 "${SANDBOX_ETC}/ld.so.preload"
	BWRAP_ARGS+=(--ro-bind "${SANDBOX_ETC}/ld.so.preload" /etc/ld.so.preload)
fi
ARGS_FILE="${RUN_DIR}/bwrap.args"
printf '%s\0' "${BWRAP_ARGS[@]}" >"${ARGS_FILE}"
chmod 644 "${ARGS_FILE}"
if [[ "${CMD[0]}" == claude || "${CMD[0]}" == "${NODE_HOME}/bin/claude" ]]; then
	CMD[0]="${NODE_HOME}/bin/claude"
	has_perm_flag=0
	for arg in "${CMD[@]:1}"; do
		[[ "$arg" == --dangerously-skip-permissions || "$arg" == --permission-mode* ]] && has_perm_flag=1
	done
	if [[ "${has_perm_flag}" -eq 0 ]]; then CMD=("${CMD[0]}" --dangerously-skip-permissions "${CMD[@]:1}"); fi
fi

# The namespace supervisor only inherits a clean environment. In particular,
# the real key and gateway never appear in its command line or environment.
unshare -m --propagation private -n -p -f --kill-child env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
	bash -c '
		set -euo pipefail
		mount -t proc proc /proc -o nosuid,nodev,noexec,relatime,hidepid=2
		ip link set lo up
		if ip link add eth0 type dummy 2>/dev/null; then
			ip addr add 192.168.1.100/24 brd 192.168.1.255 dev eth0
			ip link set eth0 up
		fi
		mnt="$1"; work="$2"; home="$3"; sock="$4"; runner="$5"; args="$6"; helper="$7"; shift 7
		python3 -I "$helper" bind "$work" "$mnt/work"
		python3 -I "$helper" bind "$home" "$mnt/home"
		python3 - "$sock" <<\PY &
import asyncio, sys
async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        w.close()
async def handle(cr, cw):
    try:
        ur, uw = await asyncio.open_unix_connection(sys.argv[1], limit=64*1024*1024)
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
    except (ConnectionError, OSError):
        cw.close()
async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", 18118, limit=64*1024*1024)
    async with server:
        await server.serve_forever()
asyncio.run(main())
PY
		relay=$!
		trap "kill $relay 2>/dev/null || true" EXIT
		ready=0
		for _ in {1..100}; do
			if (exec 3<>/dev/tcp/127.0.0.1/18118) >/dev/null 2>&1; then ready=1; break; fi
			kill -0 "$relay" 2>/dev/null || exit 1
			sleep 0.05
		done
		[[ "$ready" -eq 1 ]] || exit 1
		runuser -u "$runner" -- bash -c '\''exec 9<"$1"; shift; exec bwrap --args 9 -- "$@"'\'' _ "$args" "$@"
	' _ "${MNT_BASE}" "${ABS_WORKDIR}" "${HOST_SANDBOX_HOME}" "${PROXY_SOCK}" "${RUNNER_USER}" \
	"${ARGS_FILE}" "${SCRIPT_DIR}/sandbox_state.py" "${CMD[@]}" 8>&- <&0 &
SANDBOX_PID=$!
set +e
wait "${SANDBOX_PID}"
RC=$?
SANDBOX_PID=""
set -e
exit "${RC}"
