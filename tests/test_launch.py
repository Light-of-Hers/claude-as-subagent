"""Opt-in root sandbox regression; uses the installed runtime and dummy key."""
import os
from pathlib import Path
import select
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("CLAUDE_SUBAGENT_TEST_SANDBOX") == "1" and os.geteuid() == 0,
                     "set CLAUDE_SUBAGENT_TEST_SANDBOX=1 on a prepared root host")
class LaunchTests(unittest.TestCase):
    def test_lock_signal_cleanup_and_in_place_files(self):
        doctor = subprocess.run(["bash", str(REPO / "scripts/enter.sh"), "--doctor"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        with tempfile.TemporaryDirectory(prefix="cc-launch-test-") as tmp:
            work = Path(tmp) / "work"
            work.mkdir()
            (work / "deleted.txt").write_text("delete in sandbox")
            command = ["bash", str(REPO / "scripts/enter.sh"), "--api-key", "DUMMY_ONLY", "--proxy", "", str(work)]
            proc = subprocess.Popen(command + ["/usr/bin/bash", "-c", "printf 'READY\\n'; exec sleep 40"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                assert proc.stdout is not None
                ready, _, _ = select.select([proc.stdout], [], [], 8)
                self.assertTrue(ready, "sandbox startup timed out")
                self.assertEqual(proc.stdout.readline().strip(), "READY")
                duplicate = subprocess.run(command + ["/usr/bin/true"], capture_output=True, text=True, timeout=5)
                self.assertNotEqual(duplicate.returncode, 0)
                self.assertIn("already has an active sandbox", duplicate.stderr)
                proc.terminate()
                proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 143)
                check = subprocess.run(command + ["/usr/bin/bash", "-c",
                    "set -e; test ! -e /root/.pi; test ! -w /etc/hostname; "
                    "test ! -r /opt/node/ca/ca.key; rm deleted.txt; printf DONE > result.txt"],
                    capture_output=True, text=True, timeout=8)
                self.assertEqual(check.returncode, 0, check.stderr)
                self.assertFalse((work / "deleted.txt").exists())
                self.assertEqual((work / "result.txt").read_text(), "DONE")
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        # Avoid waiting indefinitely if a regression leaves a child
                        # holding an inherited pipe open.
                        proc.wait(timeout=2)
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()
                subprocess.run(["git", "config", "--global", "--fixed-value", "--unset-all",
                                "safe.directory", str(work)], capture_output=True)


if __name__ == "__main__":
    unittest.main()
