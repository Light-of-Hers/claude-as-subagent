#!/usr/bin/env python3
"""Stage only Node and Claude Code, never a system installation prefix."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def copy_file(source: Path, destination: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        shutil.copy2(source.resolve(strict=True), temporary)
        temporary.chmod(0o755)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def stage_runtime(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    destination = destination.resolve(strict=True)
    # install.sh has already validated the destination as a dedicated,
    # root-owned directory; it must not be /usr, /usr/local, or another prefix.
    for part in (destination / 'bin', destination / 'lib', destination / 'lib/node_modules',
                 destination / 'lib/node_modules/@anthropic-ai'):
        if part.is_symlink():
            raise ValueError('Runtime destination contains a symlink')
        part.mkdir(exist_ok=True)
        part.chmod(0o755)
    node = source / 'bin/node'
    if node.resolve() != (destination / 'bin/node').resolve():
        copy_file(node, destination / 'bin/node')
    claude = source / 'bin/claude'
    native = False
    if claude.exists():
        with claude.resolve().open('rb') as stream:
            native = stream.read(4) == b'\x7fELF'
    if native:
        copy_file(claude, destination / 'bin/claude')
        return
    package = source / 'lib/node_modules/@anthropic-ai/claude-code'
    target = destination / 'lib/node_modules/@anthropic-ai/claude-code'
    if package.is_dir() and package.resolve() != target.resolve():
        if target.is_symlink():
            raise ValueError('Claude package destination is a symlink')
        # Copy only this package. Native companion packages can be installed by
        # npm below when the resulting CLI cannot start.
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(package, target, symlinks=False)
        entry = claude.resolve()
        try:
            relative = entry.relative_to(package.resolve())
        except ValueError:
            relative = Path('cli.js')
        link = destination / 'bin/claude'
        link.unlink(missing_ok=True)
        link.symlink_to(os.path.relpath(target / relative, link.parent))
    env = dict(os.environ, PATH=str(destination / 'bin') + ':' + os.environ.get('PATH', ''))
    installed = destination / 'bin/claude'
    if installed.exists():
        result = subprocess.run([str(installed), '--version'], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        if result.returncode == 0:
            return
    npm = source / 'bin/npm'
    if not npm.exists():
        raise ValueError('Claude Code is missing and npm is unavailable; install it in the source runtime first')
    subprocess.run([str(npm), 'install', '--global', '--prefix', str(destination),
                    '@anthropic-ai/claude-code'], env=env, check=True)


if __name__ == '__main__':
    stage_runtime(Path(sys.argv[1]), Path(sys.argv[2]))
