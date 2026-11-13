#!/usr/bin/env bash
# Idempotent setup for claude-as-subagent:
#   1. Installs bubblewrap (apt / dnf / yum / pacman)
#   2. Creates the unprivileged user `claude-runner` (no home dir; added to
#      `video`/`render` groups when present for GPU/accelerator access)
#   3. Stages a standalone node + @anthropic-ai/claude-code runtime under
#      /opt/node (or reuses an existing world-executable installation)
#   4. Compiles /opt/node/lib/libfakeuname.so to anonymize host kernel/hostname
#   5. Generates a local Root CA under /opt/node/ca for zero-trust credential
#      injection and GeoIP self-check anonymization
#   6. Optionally builds a filtered /usr/local view when
#      CLAUDE_SUBAGENT_HIDE_PATTERNS is provided
#
# Run as root (or via sudo). Re-run with no active sandbox using this runtime.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RUNNER_USER="${CLAUDE_SUBAGENT_USER:-claude-runner}"
NODE_DST="${CLAUDE_SUBAGENT_NODE_HOME:-/opt/node}"
HIDE_PATTERNS="${CLAUDE_SUBAGENT_HIDE_PATTERNS:-}"

if [[ "${EUID}" -ne 0 ]]; then
	echo "Error: install.sh must be run as root (e.g. sudo ./scripts/install.sh)" >&2
	exit 1
fi
for required in python3 openssl runuser unshare ip mount flock; do
	if ! command -v "${required}" >/dev/null 2>&1; then
		echo "Error: required command is missing: ${required}" >&2
		exit 1
	fi
done
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
	echo 'Error: Python 3.11 or later is required' >&2
	exit 1
fi

# This is an application directory, never a system prefix or a symlink into
# one. Do not remove it: existing certificates and active runtimes may use it.
python3 - "${NODE_DST}" <<'PYEOF'
import os, stat, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.is_absolute() or '..' in p.parts or p in map(Path, ('/', '/usr', '/usr/local', '/bin', '/sbin', '/lib', '/lib64', '/etc', '/opt', '/var', '/root', '/home', '/tmp')):
    sys.exit('Error: NODE_HOME must be a dedicated absolute runtime directory')
current = Path('/')
for component in p.parts[1:]:
    current /= component
    if current.is_symlink():
        sys.exit('Error: runtime path must not contain symlinks')
    current.mkdir(exist_ok=True)
    info = current.stat()
    if info.st_uid != 0 or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
        sys.exit('Error: runtime path must be root-owned and not writable by other users')
if p.stat().st_mode & 0o022:
    sys.exit('Error: runtime directory must not be writable by other users')
PYEOF

install_pkg() {
	local pkg="$1"
	if command -v apt-get >/dev/null 2>&1; then
		export DEBIAN_FRONTEND=noninteractive
		apt-get update -y && apt-get install -y --no-install-recommends "${pkg}"
	elif command -v dnf >/dev/null 2>&1; then
		dnf install -y "${pkg}"
	elif command -v yum >/dev/null 2>&1; then
		yum install -y "${pkg}"
	elif command -v pacman >/dev/null 2>&1; then
		pacman -Sy --noconfirm "${pkg}"
	else
		return 1
	fi
}

# 1) bubblewrap -------------------------------------------------------------
if ! command -v bwrap >/dev/null 2>&1; then
	if ! install_pkg bubblewrap; then
		echo "Error: bubblewrap is required; installation failed" >&2
		exit 1
	fi
fi

# 2) Unprivileged user & accelerator device groups --------------------------
if ! getent passwd "${RUNNER_USER}" >/dev/null 2>&1; then
	useradd -M -d /nonexistent -s /bin/bash -c "Sandboxed Claude Code sub-agent" "${RUNNER_USER}"
else
	usermod -d /nonexistent "${RUNNER_USER}" 2>/dev/null || true
fi

for grp in video render; do
	if getent group "${grp}" >/dev/null 2>&1; then
		usermod -aG "${grp}" "${RUNNER_USER}" 2>/dev/null || true
	fi
done

# 3) Node.js + @anthropic-ai/claude-code runtime ----------------------------
runner_can_exec() {
	local p="$1"
	[[ -x "${p}/bin/node" && -x "${p}/bin/claude" ]] || return 1
	"${p}/bin/node" -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 20 ? 0 : 1)' >/dev/null 2>&1 || return 1
	runuser -u "${RUNNER_USER}" -- "${p}/bin/node" --version >/dev/null 2>&1 || return 1
	runuser -u "${RUNNER_USER}" -- env PATH="${p}/bin:/usr/bin:/bin" "${p}/bin/claude" --version >/dev/null 2>&1 || return 1
	return 0
}

CHOSEN_NODE_HOME=""
for p in "${NODE_DST}" /usr/local /usr; do
	if runner_can_exec "$p"; then
		CHOSEN_NODE_HOME="$p"
		break
	fi
done

find_node_src() {
	if [[ -n "${CLAUDE_SUBAGENT_NODE_SRC:-}" && -x "${CLAUDE_SUBAGENT_NODE_SRC}/bin/node" ]]; then
		echo "${CLAUDE_SUBAGENT_NODE_SRC}"
		return 0
	fi
	local candidates=()
	if [[ -n "${SUDO_USER:-}" ]]; then
		local uhome
		uhome="$(getent passwd "${SUDO_USER}" | cut -d: -f6)"
		[[ -n "${uhome}" ]] && candidates+=("${uhome}/.nvm/versions/node"/v* "${uhome}/.volta" "${uhome}/.local/share/fnm/node-versions"/v*/installation)
	fi
	candidates+=(/root/.nvm/versions/node/v* /usr/local /usr)
	local p
	while IFS= read -r p; do
		if [[ -x "${p}/bin/node" ]] && "${p}/bin/node" -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 20 ? 0 : 1)' >/dev/null 2>&1; then
			echo "$p"
			return 0
		fi
	done < <(printf '%s\n' "${candidates[@]}" | sort -V -r)
	return 1
}

if [[ -z "${CHOSEN_NODE_HOME}" ]]; then
	NODE_SRC="$(find_node_src || true)"
	if [[ -z "${NODE_SRC}" ]]; then
		cat >&2 <<EOF
Error: could not find a Node.js installation.
Please install Node.js (>= 20) and @anthropic-ai/claude-code, or set:
  CLAUDE_SUBAGENT_NODE_SRC=/path/to/node-prefix sudo -E ./scripts/install.sh
EOF
		exit 1
	fi
	python3 -I "${SCRIPT_DIR}/stage_runtime.py" "${NODE_SRC}" "${NODE_DST}"
	chmod -R a+rX,go-w "${NODE_DST}/bin" "${NODE_DST}/lib/node_modules"
	if ! runner_can_exec "${NODE_DST}"; then
		echo 'Error: staged Node/Claude runtime cannot run as the sandbox user (Node >= 20 required)' >&2
		exit 1
	fi
	CHOSEN_NODE_HOME="${NODE_DST}"
fi

mkdir -p "${NODE_DST}/lib" "${NODE_DST}/ca"

# 4) Compile libfakeuname.so (host kernel / hostname anonymization) ---------
if command -v gcc >/dev/null 2>&1; then
	tmp_c="$(mktemp --suffix=.c)"
	cat > "${tmp_c}" <<'EOF'
#define _GNU_SOURCE
#include <sys/utsname.h>
#include <dlfcn.h>
#include <string.h>
#include <unistd.h>

int uname(struct utsname *buf) {
    int (*real_uname)(struct utsname *) = dlsym(RTLD_NEXT, "uname");
    if (!real_uname) return -1;
    int r = real_uname(buf);
    if (r == 0 && buf) {
        strncpy(buf->nodename, "workstation", sizeof(buf->nodename) - 1);
        buf->nodename[sizeof(buf->nodename) - 1] = '\0';
        strncpy(buf->release, "6.1.0-21-amd64", sizeof(buf->release) - 1);
        buf->release[sizeof(buf->release) - 1] = '\0';
        strncpy(buf->version, "#1 SMP PREEMPT_DYNAMIC Debian 6.1.90-1 (2024-05-03)", sizeof(buf->version) - 1);
        buf->version[sizeof(buf->version) - 1] = '\0';
    }
    return r;
}

int gethostname(char *name, size_t len) {
    if (!name || len == 0) return -1;
    strncpy(name, "workstation", len - 1);
    name[len - 1] = '\0';
    return 0;
}
EOF
	gcc -O2 -shared -fPIC "${tmp_c}" -o "${NODE_DST}/lib/libfakeuname.so" -ldl
	chmod 755 "${NODE_DST}/lib/libfakeuname.so"
	rm -f "${tmp_c}"
fi

# 5) Local Sandbox CA & TLS certificate for proxy bridge --------------------
CA_DIR="${NODE_DST}/ca"
SYS_CA_BUNDLE=""
for ca_cand in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/ca-bundle.pem; do
	if [[ -f "${ca_cand}" ]]; then
		SYS_CA_BUNDLE="${ca_cand}"
		break
	fi
done

if [[ ! -f "${CA_DIR}/ca-bundle.crt" || ! -f "${CA_DIR}/geoip.crt" || ! -f "${CA_DIR}/geoip.key" ]] || \
   ! grep -q "api.anthropic.com" "${CA_DIR}/san.cnf" 2>/dev/null; then
	openssl req -x509 -newkey rsa:2048 -nodes \
		-keyout "${CA_DIR}/ca.key" -out "${CA_DIR}/ca.crt" -days 3650 \
		-subj "/CN=Workstation Local Root CA" \
		-addext "basicConstraints=critical,CA:TRUE" \
		-addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
	cat > "${CA_DIR}/san.cnf" <<'EOF'
[req]
distinguished_name = req_dn
req_extensions = v3_req
prompt = no
[req_dn]
CN = api.anthropic.com
[v3_req]
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names
[alt_names]
DNS.1 = ipinfo.io
DNS.2 = *.ipinfo.io
DNS.3 = ifconfig.me
DNS.4 = *.ifconfig.me
DNS.5 = ifconfig.co
DNS.6 = ip-api.com
DNS.7 = *.ip-api.com
DNS.8 = api.ipify.org
DNS.9 = ipify.org
DNS.10 = *.ipify.org
DNS.11 = icanhazip.com
DNS.12 = ipv4.icanhazip.com
DNS.13 = ident.me
DNS.14 = v4.ident.me
DNS.15 = checkip.amazonaws.com
DNS.16 = ip.sb
DNS.17 = api.ip.sb
DNS.18 = cip.cc
DNS.19 = myip.ipip.net
DNS.20 = wtfismyip.com
DNS.21 = ipecho.net
DNS.22 = httpbin.org
DNS.23 = myip.com
DNS.24 = api.myip.com
DNS.25 = ipwhois.app
DNS.26 = ipapi.co
DNS.27 = freeipapi.com
DNS.28 = www.cloudflare.com
DNS.29 = cloudflare.com
DNS.30 = api.anthropic.com
DNS.31 = *.anthropic.com
EOF
	openssl req -newkey rsa:2048 -nodes \
		-keyout "${CA_DIR}/geoip.key" -out "${CA_DIR}/geoip.csr" \
		-config "${CA_DIR}/san.cnf" 2>/dev/null
	openssl x509 -req -in "${CA_DIR}/geoip.csr" \
		-CA "${CA_DIR}/ca.crt" -CAkey "${CA_DIR}/ca.key" -CAcreateserial \
		-out "${CA_DIR}/geoip.crt" -days 3650 \
		-extfile "${CA_DIR}/san.cnf" -extensions v3_req 2>/dev/null
	if [[ -n "${SYS_CA_BUNDLE}" ]]; then
		cat "${SYS_CA_BUNDLE}" "${CA_DIR}/ca.crt" > "${CA_DIR}/ca-bundle.crt"
	else
		cp "${CA_DIR}/ca.crt" "${CA_DIR}/ca-bundle.crt"
	fi
	chmod 644 "${CA_DIR}/ca.crt" "${CA_DIR}/geoip.crt" "${CA_DIR}/ca-bundle.crt"
	chmod 600 "${CA_DIR}/ca.key" "${CA_DIR}/geoip.key"
fi

# 6) Optional /usr/local filtered view (when CLAUDE_SUBAGENT_HIDE_PATTERNS is set)
if [[ -n "${HIDE_PATTERNS}" ]]; then
	python3 - "${NODE_DST}/clean-usr-local" "${HIDE_PATTERNS}" <<'PYEOF'
import glob, os, re, shutil, sys
from pathlib import Path

base = Path(sys.argv[1])
pattern = sys.argv[2]
if base.exists():
    shutil.rmtree(base)
(base / "bin").mkdir(parents=True, exist_ok=True)
(base / "dist-packages").mkdir(parents=True, exist_ok=True)
(base / ".backing" / "bin").mkdir(parents=True, exist_ok=True)
(base / ".backing" / "dist-packages").mkdir(parents=True, exist_ok=True)
os.chmod(base, 0o755)
os.chmod(base / ".backing", 0o711)

bad = re.compile(pattern, re.IGNORECASE)

if Path("/usr/local/bin").is_dir():
    for name in os.listdir("/usr/local/bin"):
        if bad.search(name):
            continue
        os.symlink(f"{base}/.backing/bin/{name}", base / "bin" / name)

for dp in sorted(glob.glob("/usr/local/lib/python3*/dist-packages")):
    if Path(dp).is_dir():
        for name in os.listdir(dp):
            if bad.search(name) or (base / "dist-packages" / name).exists():
                continue
            os.symlink(f"{base}/.backing/dist-packages/{name}", base / "dist-packages" / name)
        break
PYEOF
fi

echo "claude-as-subagent runtime ready:"
echo "  user    ${RUNNER_USER} (uid=$(id -u "${RUNNER_USER}"), groups=$(id -Gn "${RUNNER_USER}"))"
echo "  bwrap   $(bwrap --version)"
echo "  node    $("${CHOSEN_NODE_HOME}/bin/node" --version) @ ${CHOSEN_NODE_HOME}"
echo "  claude  $("${CHOSEN_NODE_HOME}/bin/claude" --version 2>&1 | head -1)"
