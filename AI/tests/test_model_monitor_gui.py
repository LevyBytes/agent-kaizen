"""support_scripts/model_monitor_gui.py: the native backend-monitor window. Verified HEADLESS only --
--once (no Qt) through an isolated plane, and --smoke (offscreen Qt gate) when PySide6 is present.
No real window, no model load, no display. The GUI reuses the B6 collect() layer, so the data path is
already covered by test_model_monitor; this guards the GUI wrapper + the offscreen render gate."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import REPO_ROOT, IsolatedDBTest  # noqa: E402

_GUI = REPO_ROOT / "support_scripts" / "model_monitor_gui.py"
_HAS_QT = importlib.util.find_spec("PySide6") is not None
_DEAD_OLLAMA = {"KAIZEN_LLM_BASE_URL": "http://127.0.0.1:1/v1"}  # deterministic: unreachable


class GuiHeadlessTest(IsolatedDBTest):
    def _run(self, *flags: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["KAIZEN_REPO_ROOT"] = str(self.root)
        env.update(_DEAD_OLLAMA)
        return subprocess.run(
            [sys.executable, str(_GUI), *flags],
            capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
        )

    def test_once_emits_four_lanes_no_qt(self):
        proc = self._run("--once")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload.get("status"), "OK", payload)
        self.assertEqual(len(payload["backends"]), 4, payload)
        self.assertFalse(payload["ollama"]["reachable"], payload)  # dead port

    @unittest.skipUnless(_HAS_QT, "PySide6 not installed (opt-in GUI extra)")
    def test_smoke_offscreen_gate(self):
        proc = self._run("--smoke")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("smoke: OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
