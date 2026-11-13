"""The whole product in an isolated headless shell against the live server. See e2e.sh.

Skipped unless asked for, and it never reads or stores the key itself. The server must be
named, as the default is any program listening on this computer:

    TUXISTANT_LIVE=1 TUXISTANT_SERVER=host:8080 TUXISTANT_API_KEY=... \
        python3 tests/shell_harness/test_e2e_live.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

E2E = Path(__file__).resolve().parent / "e2e.sh"
sys.path.insert(0, str(E2E.parent.parent.parent))
from tools.eval_config import run_server  # noqa: E402


@unittest.skipUnless(os.environ.get("TUXISTANT_LIVE") and os.environ.get("TUXISTANT_API_KEY") and
                     shutil.which("gnome-shell"), "live server test")
class EndToEnd(unittest.TestCase):
    def test_everything(self):
        try:
            run_server([])
        except SystemExit as refused:   # e2e.sh refuses too, but this says why
            self.fail(str(refused))
        with tempfile.TemporaryDirectory(prefix="tux-e2e-out.") as temp:
            out = Path(os.environ.get("TUX_E2E_OUT") or temp)
            done = subprocess.run([str(E2E), str(out)], capture_output=True, text=True, timeout=520)
            report = json.loads((out / "report.json").read_text())
        failed = [check for check in report["checks"] if not check["ok"]]
        self.assertEqual(failed, [], done.stdout[-4000:])
        self.assertGreater(len(report["checks"]), 90)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])


if __name__ == "__main__":
    unittest.main()
