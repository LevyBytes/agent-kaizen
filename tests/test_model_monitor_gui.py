"""Headless coverage for the operational backend monitor and its nonblocking GUI factory."""

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
            timeout=30,
        )

    def test_once_emits_four_lanes_no_qt(self):
        proc = self._run("--once")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload.get("status"), "OK", payload)
        self.assertEqual(len(payload["backends"]), 4, payload)
        self.assertFalse(payload["ollama"]["reachable"], payload)  # dead port

    @unittest.skipUnless(_HAS_QT, "PySide6 not installed (opt-in GUI extra)")
    def test_build_gui_offscreen_with_synthetic_snapshots(self):
        previous_platform = os.environ.get("QT_QPA_PLATFORM")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

        def restore_platform() -> None:
            if previous_platform is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = previous_platform

        self.addCleanup(restore_platform)
        spec = importlib.util.spec_from_file_location("kaizen_model_monitor_gui_test", _GUI)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def snap(temp: int, used: int, running: bool) -> dict:
            return {
                "gpu": {"available": True, "devices": [{"index": 0, "name": "RTX 5080", "temp_c": temp, "fan_pct": 0, "util_pct": 5, "mem_used_mb": used, "mem_total_mb": 16303, "power_w": 30, "power_limit_w": 360}]},
                "gpu_processes": {"available": True, "procs": ([
                    {"pid": 36832, "name": "llama-server.exe", "vram_mb": None, "gpu_mem_mb": 1603.6, "gpu_util_pct": 4.2, "kind": "ollama"},
                    {"pid": 44100, "name": "python.exe", "vram_mb": None, "gpu_mem_mb": 940.0, "gpu_util_pct": 61.0, "kind": "torch/python"},
                ] if running else [])},
                "ollama": {"reachable": True, "endpoint": "http://127.0.0.1:11434", "loaded": ([{"model": "Qwen3.5-9B:Q4_K_M", "size_vram_mb": 1200, "keep_alive": "3m41s"}] if running else [])},
                "backends": [{"lane": "embed", "configured": False}, {"lane": "text", "configured": False}, {"lane": "rerank", "configured": False}, {"lane": "pii", "configured": False}],
                "recent": [
                    {"created_at": "2026-07-06T17:36:10Z", "kind": "judge", "lane": "judge", "model": "Qwen3.5-9B", "latency_ms": 840},
                    {"created_at": "2026-07-06T17:36:09Z", "kind": "model_call", "lane": "embedding", "model": "F2LLM-v2-1.7B", "latency_ms": 42},
                    {"created_at": "2026-07-06T17:36:08Z", "kind": "model_call", "lane": "rerank", "model": "ettin-reranker-150m", "latency_ms": 31},
                    {"created_at": "2026-07-06T17:36:07Z", "kind": "model_call", "lane": "pii", "model": "gliner2-pii", "latency_ms": 55},
                ],
            }

        class StaticSource:
            def snapshot(self) -> dict:
                return snap(40, 1300, running=False)

        app, win = module._build_gui(StaticSource(), 60_000)
        try:
            win.poller.halt()
            self.assertTrue(win.poller.wait(1500), "model-monitor poller did not stop")
            for _ in range(3):
                app.processEvents()
            try:
                win.poller.snap.disconnect()
                win.poller.fail.disconnect()
            except (RuntimeError, TypeError):
                pass

            def pump(count: int = 4) -> None:
                for _ in range(count):
                    app.processEvents()

            win._on_snap(snap(40, 1300, running=False))
            pump()
            self.assertIn("nothing running", win.running.toPlainText())
            base = win.writes
            win._on_snap(snap(41, 2800, running=True))
            pump()
            running = win.running.toPlainText()
            self.assertIn("llama-server.exe", running)
            self.assertIn("python.exe", running)
            self.assertIn("1603.6 MB", running)
            self.assertIn("61.0%", running)
            self.assertIn("Qwen3.5-9B", running)
            self.assertGreater(win.writes, base)
            recent = win.recent.toPlainText()
            for lane in ("judge", "embedding", "rerank", "pii"):
                self.assertIn(lane, recent)
            purged = snap(42, 2800, running=True)
            purged["recent"] = []
            win._on_snap(purged)
            pump()
            self.assertIn("Qwen3.5-9B", win.recent.toPlainText())
            win._on_fail("DatabaseError: database is locked")
            pump()
            self.assertTrue(win.fail_lbl.isVisible())
            self.assertEqual(win._pt_for_width(win._MIN_W), win._MIN_PT)
            self.assertEqual(win._pt_for_width((win._MIN_W + win._REF_W) // 2), 17)
            self.assertEqual(win._pt_for_width(99999), win._MAX_PT)
            self.assertEqual(win._pt_for_width(0), win._MIN_PT)
        finally:
            win.close()
            app.processEvents()
            app.quit()


if __name__ == "__main__":
    unittest.main()
