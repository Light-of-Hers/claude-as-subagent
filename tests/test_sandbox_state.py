"""State and installation regressions using disposable files only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import sandbox_state as state
from scripts import stage_runtime


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cc-state-test-")
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.home = self.root / ".work.cc-home"
        self.etc = self.root / "etc"
        self.etc.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, reset=False):
        state.prepare(str(self.home), str(self.etc), "model-test", str(self.work), "UTC",
                      os.getuid(), os.getgid(), reset)

    def test_file_symlink_cannot_overwrite_external_file(self):
        self.prepare()
        outside = self.root / "outside"
        outside.write_text("UNCHANGED")
        settings = self.home / ".claude/settings.json"
        settings.unlink()
        settings.symlink_to(outside)
        # The old .etc is writable state and must never be used for mount sources.
        (self.home / ".etc").mkdir()
        (self.home / ".etc/hostname").symlink_to(outside)
        self.prepare()
        self.assertEqual(outside.read_text(), "UNCHANGED")
        self.assertFalse(settings.is_symlink())
        self.assertEqual((self.etc / "hostname").read_text(), "workstation\n")

    def test_directory_symlink_and_hardlinked_state_rejected(self):
        self.home.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "settings.json").write_text("UNCHANGED")
        (self.home / ".claude").symlink_to(outside)
        with self.assertRaises(OSError):
            self.prepare()
        self.assertEqual((outside / "settings.json").read_text(), "UNCHANGED")
        (self.home / ".claude").unlink()
        self.prepare()
        config = self.home / ".claude.json"
        config.unlink()
        source = outside / "settings.json"
        os.link(source, config)
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(source.read_text(), "UNCHANGED")

    def test_reset_preserves_work_and_does_not_traverse_links(self):
        self.prepare()
        old_seed = (self.home / ".id-seed").read_text()
        (self.work / "progress.txt").write_text("checkpoint")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("UNCHANGED")
        (self.home / ".claude/history-link").symlink_to(outside)
        self.prepare(reset=True)
        self.assertNotEqual((self.home / ".id-seed").read_text(), old_seed)
        self.assertEqual((self.work / "progress.txt").read_text(), "checkpoint")
        self.assertEqual((outside / "keep.txt").read_text(), "UNCHANGED")
        self.assertFalse((self.home / ".claude/history-link").exists())
        self.assertEqual(json.loads((self.home / ".claude/settings.json").read_text())["env"]["ANTHROPIC_MODEL"], "model-test[1m]")

    def test_parent_symlink_is_rejected(self):
        link = self.root / "alias"
        link.symlink_to(self.work)
        with self.assertRaises(OSError):
            with state.directory(link):
                pass


class RuntimeTests(unittest.TestCase):
    def test_only_runtime_files_are_staged_and_ca_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="cc-install-test-") as tmp:
            root = Path(tmp)
            source, destination = root / "system-prefix", root / "runtime"
            (source / "bin").mkdir(parents=True)
            (source / "share/other-package").mkdir(parents=True)
            (source / "share/other-package/never-copy").write_text("private")
            package = source / "lib/node_modules/@anthropic-ai/claude-code"
            package.mkdir(parents=True)
            (package / "cli.js").write_text("#!/bin/sh\nprintf 'TEST_CLI\\n'\n")
            (package / "cli.js").chmod(0o755)
            (source / "bin/claude").symlink_to("../lib/node_modules/@anthropic-ai/claude-code/cli.js")
            (source / "bin/node").write_text("#!/bin/sh\nexit 0\n")
            (source / "bin/node").chmod(0o755)
            (destination / "ca").mkdir(parents=True)
            (destination / "ca/ca.key").write_text("DO_NOT_TOUCH")
            for _ in range(2):
                stage_runtime.stage_runtime(source, destination)
                self.assertFalse((destination / "share").exists())
                self.assertEqual((destination / "ca/ca.key").read_text(), "DO_NOT_TOUCH")
                result = subprocess.run([str(destination / "bin/claude"), "--version"],
                                        capture_output=True, text=True, check=True)
                self.assertIn("TEST_CLI", result.stdout)


if __name__ == "__main__":
    unittest.main()
