#!/usr/bin/env python3
"""Prepare sandbox state without following links in task-controlled directories."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
from typing import Iterator


@contextmanager
def directory(path: str | Path, *, create: bool = False) -> Iterator[int]:
    """Pin every component with O_NOFOLLOW, including directories below /root."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Expected an absolute directory path without '..'")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def child_directory(parent: int, name: str, uid: int, gid: int) -> Iterator[int]:
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o700)
        yield fd
    finally:
        os.close(fd)


def read_file(parent: int, name: str, limit: int = 4 * 1024 * 1024) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError(f"Unsafe sandbox state file: {name}")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"Sandbox state file too large: {name}")
        return data


def write_file(parent: int, name: str, data: str, uid: int, gid: int) -> None:
    # Replace the directory entry atomically instead of truncating an existing
    # file. Neither hard links nor a last-moment symlink can redirect the write.
    temporary = ".prepare-" + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fchown(stream.fileno(), uid, gid)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def remove_entry(parent: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("This Python build lacks safe directory removal")
        shutil.rmtree(name, dir_fd=parent)
    else:
        os.unlink(name, dir_fd=parent)


def prepare(home: str, etc: str, model: str, workdir: str, tz: str,
            uid: int, gid: int, reset: bool = False) -> None:
    with directory(workdir) as work_fd:
        os.fchown(work_fd, uid, gid)
        subprocess.run(["chown", "-Rh", f"{uid}:{gid}", f"/proc/self/fd/{work_fd}/."],
                       pass_fds=(work_fd,), check=True)
    with directory(Path(home).parent) as parent:
        with child_directory(parent, Path(home).name, uid, gid) as home_fd:
            if reset:
                for name in (".claude", ".claude.json", ".id-seed", ".etc"):
                    remove_entry(home_fd, name)
            seed = read_file(home_fd, ".id-seed", 128)
            if seed is None:
                seed = secrets.token_hex(32).encode()
                write_file(home_fd, ".id-seed", seed.decode() + "\n", uid, gid)
            seed = seed.strip()
            if len(seed) != 64 or any(c not in b"0123456789abcdef" for c in seed):
                raise ValueError("Invalid sandbox identity seed")
            # A per-sandbox placeholder needs no real API key as input.
            fake_key = "sk-ant-api03-" + base64.urlsafe_b64encode(
                hashlib.sha512(b"token:" + seed).digest() + hashlib.sha512(seed).digest()
            ).decode()[:93] + "AA"
            settings = {
                "hasCompletedOnboarding": True, "disableNonEssentialModelCalls": True,
                "includeCoAuthoredBy": False, "cleanupPeriodDays": 7,
                "skipDangerousModePermissionPrompt": True, "autoCompactWindow": 900000,
                "promptCacheTtl": "1h", "subagentPromptCacheTtl": "1h",
                "permissions": {"defaultMode": "bypassPermissions"},
                "env": {
                    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                    "ANTHROPIC_AUTH_TOKEN": fake_key,
                    "ANTHROPIC_MODEL": model if "[" in model else model + "[1m]",
                    "CLAUDE_CODE_PROMPT_CACHE_TTL": "1h",
                    "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": "1h",
                    "ENABLE_PROMPT_CACHING_1H": "1",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
                    "DISABLE_AUTOUPDATER": "1",
                },
            }
            with child_directory(home_fd, ".claude", uid, gid) as config_fd:
                write_file(config_fd, "settings.json", json.dumps(settings, indent=2) + "\n", uid, gid)
            raw = read_file(home_fd, ".claude.json")
            try:
                state = json.loads(raw) if raw else {}
            except (ValueError, UnicodeError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            for field in ("userID", "machineID"):
                if not isinstance(state.get(field), str) or len(state[field]) != 64:
                    state[field] = hashlib.sha256(field.encode() + b":" + seed).hexdigest()
            state.update(hasCompletedOnboarding=True, bypassPermissionsModeAccepted=True,
                         hasSeenAutoDefaultNudge=True, hasSeenAutoDefaultNotice=True)
            projects = state.get("projects")
            if not isinstance(projects, dict):
                projects = {}
            for path in ("/work", workdir):
                project = projects.get(path)
                if not isinstance(project, dict):
                    project = {}
                project.update(hasTrustDialogAccepted=True, hasCompletedProjectOnboarding=True)
                projects[path] = project
            state["projects"] = projects
            write_file(home_fd, ".claude.json", json.dumps(state, indent=2) + "\n", uid, gid)

    # Generated mount sources live in a new, root-owned per-launch directory.
    # They are never read back from the runner-writable persistent home.
    machine_id = hashlib.sha256(b"machine-id:" + seed).hexdigest()[:32]
    group_lines = ["root:x:0:", "nobody:x:65534:", f"dev:x:{gid}:"]
    for line in Path("/etc/group").read_text().splitlines():
        parts = line.split(":")
        if parts[0] in ("video", "render"):
            group_lines.append(f"{parts[0]}:x:{parts[2]}:dev")
    files = {
        "machine-id": machine_id + "\n", "hostname": "workstation\n",
        "hosts": "127.0.0.1\tlocalhost\n192.168.1.100\tworkstation.local workstation\n::1\tlocalhost\n",
        "timezone": tz + "\n", "resolv.conf": "nameserver 8.8.8.8\nnameserver 1.1.1.1\n",
        "proc_version": "Linux version 6.1.0-21-amd64 (debian-kernel@lists.debian.org) "
                        "(gcc-12 (Debian 12.2.0-14) 12.2.0, GNU ld (GNU Binutils for Debian) 2.40) "
                        "#1 SMP PREEMPT_DYNAMIC Debian 6.1.90-1 (2024-05-03)\n",
        "osrelease": "6.1.0-21-amd64\n",
        "passwd": f"root:x:0:0:root:/root:/bin/bash\nnobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
                  f"dev:x:{uid}:{gid}:Developer:/home/dev:/bin/bash\n",
        "group": "\n".join(group_lines) + "\n",
    }
    with directory(etc) as etc_fd:
        for name, content in files.items():
            write_file(etc_fd, name, content, os.geteuid(), os.getegid())
            os.chmod(name, 0o644, dir_fd=etc_fd, follow_symlinks=False)


def bind_directory(source: str, target: str) -> None:
    # Pin the mount source so renaming it or replacing it with a symlink cannot
    # expose some other host directory between validation and mount(2).
    with directory(source) as fd:
        subprocess.run(["mount", "--no-canonicalize", "--bind", f"/proc/self/fd/{fd}", target],
                       pass_fds=(fd,), check=True)


if __name__ == "__main__":
    if sys.argv[1] == "bind":
        bind_directory(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "mkdir":
        path = os.path.abspath(sys.argv[2])
        if path == "/":
            raise ValueError("Workdir must be a dedicated task directory")
        with directory(path, create=True):
            print(path)
    else:
        home, etc, model, workdir, tz, uid, gid, reset = sys.argv[1:]
        prepare(home, etc, model, workdir, tz, int(uid), int(gid), reset == "1")
