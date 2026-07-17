"""Y6 comfy-runtime, offline paths: status, provision (emit-vs-ok), unknown-action denial, and
stale-pidfile stop cleanup. start/stop against a real server live in the runbook. Endpoint is
pinned to a closed port so reachability is deterministic regardless of the host machine."""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import IsolatedDBTest  # noqa: E402
from kaizen_components import comfyui  # noqa: E402
from kaizen_components.denials import KaizenDenied  # noqa: E402

_CLOSED = {"KAIZEN_COMFYUI_URL": "http://127.0.0.1:6"}


def _env(home: Path) -> dict:
    # KAIZEN_COMFYUI_VENV is pinned INTO the plane: a machine-wide override (set by the real ComfyUI
    # setup) would otherwise leak in and make bare-status assertions depend on the host (surfaced
    # 2026-07-11 when the drive-swap parity restore brought the shared venv's python back).
    return {"KAIZEN_COMFYUI_HOME": str(home), "KAIZEN_COMFYUI_VENV": str(home / ".venv"), **_CLOSED}


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
        self.assertNotIn("c:\\", json.dumps(p).replace("/", "\\").casefold(), p)

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


class ComfyEndpointBoundaryTest(unittest.TestCase):
    def test_argument_precedes_environment_and_remote_endpoints_deny_before_network(self):
        with mock.patch.dict(os.environ, {"KAIZEN_COMFYUI_URL": "http://example.invalid:8188"}):
            self.assertEqual(comfyui._endpoint(SimpleNamespace(endpoint="http://127.0.0.2:8188/")), "http://127.0.0.2:8188")
        with mock.patch.dict(os.environ, {"KAIZEN_COMFYUI_URL": "http://127.0.0.1:8188"}):
            for endpoint in ("http://192.0.2.10:8188", "http://example.invalid:8188", "https://localhost:8188"):
                with self.subTest(endpoint=endpoint), mock.patch.object(comfyui, "http_request") as request:
                    with self.assertRaises(KaizenDenied) as denied:
                        comfyui.probe(endpoint)
                    self.assertEqual(denied.exception.code, "DENIED_COMFYUI_ENDPOINT_NON_LOOPBACK")
                    request.assert_not_called()

    def test_loopback_hostname_ipv4_range_and_ipv6_are_accepted(self):
        for endpoint in ("http://localhost:8188/", "http://127.25.1.9:8188", "http://[::1]:8188"):
            with self.subTest(endpoint=endpoint), mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(comfyui._endpoint(SimpleNamespace(endpoint=endpoint)), endpoint.rstrip("/"))

    def test_redirects_are_revalidated_before_follow(self):
        requests: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 -- stdlib HTTP handler contract
                requests.append(self.path)
                if self.path == "/system_stats":
                    self.send_response(302)
                    self.send_header("Location", "http://192.0.2.10/blocked")
                    self.end_headers()
                    return
                if self.path == "/object_info":
                    self.send_response(302)
                    self.send_header("Location", "/ok")
                    self.end_headers()
                    return
                body = b'{"allowed":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with self.assertRaises(KaizenDenied) as denied:
                comfyui.probe(base, timeout=0.5)
            self.assertEqual(denied.exception.code, "DENIED_COMFYUI_ENDPOINT_NON_LOOPBACK")
            self.assertEqual(requests, ["/system_stats"], "non-loopback redirect was denied before follow")
            self.assertEqual(comfyui.fetch_object_info(base, timeout=0.5), {"allowed": True})
            self.assertEqual(requests, ["/system_stats", "/object_info", "/ok"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
