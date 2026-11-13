"""Command capture and foreground process group lifecycle."""

from __future__ import annotations

import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

FORGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FORGE / "common"))

from zeta_forge.process import CommandError, run_command


class ProcessTests(unittest.TestCase):
    def test_capture_failure_and_private_probe_timeout(self) -> None:
        output = io.StringIO()
        command = [
            sys.executable, "-c",
            "import os, sys; print(os.environ['FORGE_TEST_PROXY']); "
            "print('diagnostic', file=sys.stderr); sys.exit(7)",
        ]
        with contextlib.redirect_stdout(output):
            result = run_command(command, env={"FORGE_TEST_PROXY": "private proxy"},
                                 capture_output=True, check=False, log_command=False)
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (7, "private proxy\n", "diagnostic\n"))
            with self.assertRaisesRegex(CommandError, "exit code 7") as caught:
                run_command(command, env={"FORGE_TEST_PROXY": "private proxy"},
                            capture_output=True, log_command=False)
            self.assertNotIn("FORGE_TEST_PROXY", str(caught.exception))
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                run_command([sys.executable, "-c", "import time; time.sleep(5)", "private arg"],
                            capture_output=True, timeout=0.1, log_command=False)
            self.assertNotIn("private arg", str(caught.exception))
        self.assertEqual(output.getvalue(), "")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process state required")
    def test_interrupt_terminates_foreground_child_and_grandchild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_file = root / "child"
            grandchild_file = root / "grandchild"
            worker = root / "worker.py"
            worker.write_text(
                "import os, signal, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "if len(sys.argv) == 1:\n"
                "    subprocess.Popen([sys.executable, __file__, 'grandchild'])\n"
                "    Path('child').write_text(str(os.getpid()))\n"
                "else:\n"
                "    signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                "    Path('grandchild').write_text(str(os.getpid()))\n"
                "time.sleep(60)\n",
            )
            controller = (
                "from pathlib import Path; import sys; from zeta_forge.process import run_program\n"
                "try:\n"
                "    run_program([sys.executable, 'worker.py'], cwd=Path.cwd())\n"
                "except KeyboardInterrupt:\n"
                "    sys.exit(130)\n"
            )
            parent = subprocess.Popen(
                [sys.executable, "-c", controller], cwd=root,
                env=dict(os.environ, PYTHONPATH=str(FORGE / "common")),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            child = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if all(path.is_file() and path.read_text() for path in (child_file, grandchild_file)):
                        break
                    if parent.poll() is not None:
                        break
                    time.sleep(0.02)
                self.assertTrue(child_file.is_file())
                child = int(child_file.read_text())
                self.assertTrue(grandchild_file.is_file())
                grandchild = int(grandchild_file.read_text())
                parent.send_signal(signal.SIGINT)
                stdout, stderr = parent.communicate(timeout=15)
                self.assertEqual(parent.returncode, 130, (stdout, stderr))
                self.assertFalse(Path(f"/proc/{child}").exists(), "child was not reaped")
                grandchild_stat = Path(f"/proc/{grandchild}/stat")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        if grandchild_stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                            break
                    except FileNotFoundError:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("grandchild still running")
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.communicate(timeout=5)
                if child is not None:
                    try:
                        os.killpg(child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
