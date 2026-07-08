"""Y6 comfy-runtime, offline paths: status, provision (emit-vs-ok), unknown-action denial, and
stale-pidfile stop cleanup. start/stop against a real server live in the runbook. Endpoint is
pinned to a closed port so reachability is deterministic regardless of the host machine."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import IsolatedDBTest  # noqa: E402

_CLOSED = {"KAIZEN_COMFYUI_URL": "http://127.0.0.1:6"}


def _env(home: Path) -> dict:
    return {"KAIZEN_COMFYUI_HOME": str(home), **_CLOSED}


class RuntimeStatusTest(IsolatedDBTest):
    def test_bare_y6_is_status_ok(self):
        rc, p = self.kz("Y6", env=_env(self.root / "ComfyUI"))
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("action"), "status", p)
        self.assertFalse(p["main_py"], p)
        self.assertFalse(p["venv_python"], p)
        self.assertFalse(p["reachable"], p)

    def test_no_c_drive_paths_in_output(self):
        rc, p = self.kz("Y6", env=_env(self.root / "ComfyUI"))
        self.assertEqual(rc, 0, p)
        self.assertNotIn("C:\\", json.dumps(p), p)

    def test_status_honors_home_override(self):
        home = self.root / "cf-home"
        rc, p = self.kz("Y6", "--action", "status", env=_env(home))
        self.assertEqual(rc, 0, p)
        self.assertEqual(Path(p["runtime_home"]), home, p)

    def test_provision_emits_installer_command(self):
        rc, p = self.kz("Y6", "--action", "provision", env=_env(self.root / "ComfyUI"))
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_RUNTIME_MISSING", p)
        self.assertIn("install-comfyui", p.get("required_action", ""), p)

    def test_provision_ok_when_layout_present(self):
        home = self.root / "ComfyUI"
        home.mkdir(parents=True, exist_ok=True)
        (home / "main.py").write_text("# stub", encoding="utf-8")
        vp = home / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
        vp.parent.mkdir(parents=True, exist_ok=True)
        vp.write_text("", encoding="utf-8")
        rc, p = self.kz("Y6", "--action", "provision", env=_env(home))
        self.assertEqual(rc, 0, p)
        self.assertTrue(p.get("provisioned"), p)

    def test_venv_override_provision_ok(self):
        home = self.root / "ComfyUI"
        home.mkdir(parents=True, exist_ok=True)
        (home / "main.py").write_text("# stub", encoding="utf-8")
        venv = self.root / "venvs" / "comfyui"  # venv lives outside the ComfyUI home
        vp = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        vp.parent.mkdir(parents=True, exist_ok=True)
        vp.write_text("", encoding="utf-8")
        env = {"KAIZEN_COMFYUI_HOME": str(home), "KAIZEN_COMFYUI_VENV": str(venv), **_CLOSED}
        rc, p = self.kz("Y6", "--action", "provision", env=env)
        self.assertEqual(rc, 0, p)
        self.assertTrue(p.get("provisioned"), p)
        rc2, s = self.kz("Y6", "--action", "status", env=env)
        self.assertEqual(rc2, 0, s)
        self.assertEqual(Path(s["venv_dir"]), venv, s)

    def test_unknown_action_denied(self):
        rc, p = self.kz("Y6", "--action", "explode", env=_env(self.root / "ComfyUI"))
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_ACTION_UNKNOWN", p)

    def test_stale_pidfile_stop_cleanup(self):
        state = self.root / "AI" / "work" / "comfyui"
        state.mkdir(parents=True, exist_ok=True)
        (state / "runtime.json").write_text(json.dumps({"pid": 999999999, "port": 8188}), encoding="utf-8")
        rc, p = self.kz("Y6", "--action", "stop", env=_env(self.root / "ComfyUI"))
        self.assertEqual(rc, 0, p)
        self.assertTrue(p.get("stale_pidfile_removed"), p)
        self.assertFalse((state / "runtime.json").is_file(), "stale pidfile should be removed")


if __name__ == "__main__":
    unittest.main()
