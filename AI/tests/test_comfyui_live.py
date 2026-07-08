"""ComfyUI LIVE path (Y1 submit -> wait -> fetch -> register -> completed; Y4 replay), exercised
against an in-process MOCK ComfyUI HTTP server. No real ComfyUI/GPU needed -- the mock answers
/system_stats, /prompt, /history/{id}, /view, so the real client code in comfyui.py runs end to end."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import IsolatedDBTest  # noqa: E402

_PNG = b"\x89PNG\r\n\x1a\nmock-image-bytes"


class _ComfyHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/system_stats"):
            self._json({"system": {"comfyui_version": "mock-1.0", "python_version": "3.x.mock"}})
        elif self.path.startswith("/object_info"):
            self._json({
                "KSampler": {"input": {"required": {"seed": ["INT"], "steps": ["INT"]}}},
                "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["mock.safetensors", "demo_v1.safetensors"]]}}},
            })
        elif self.path.startswith("/history/"):
            prompt_id = self.path.rsplit("/", 1)[1]
            # Multiple output keys (image + video + audio) exercise the key-agnostic fetch.
            self._json({
                prompt_id: {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {"9": {
                        "images": [{"filename": "out.png", "subfolder": "", "type": "output"}],
                        "gifs": [{"filename": "out.mp4", "subfolder": "", "type": "output"}],
                        "audio": [{"filename": "out.flac", "subfolder": "", "type": "output"}],
                    }},
                }
            })
        elif self.path.startswith("/view"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_PNG)))
            self.end_headers()
            self.wfile.write(_PNG)
        else:
            self._json({}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.path.startswith("/prompt"):
            self._json({"prompt_id": "mockprompt1"})
        else:
            self._json({}, 404)


class ComfyLiveTest(IsolatedDBTest):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ComfyHandler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        # addCleanup is LIFO: register server_close first so shutdown() runs before it.
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _workflow(self) -> Path:
        path = self.root / "wf.json"
        path.write_text(
            json.dumps({
                "3": {"class_type": "KSampler", "inputs": {"seed": 777, "steps": 8}},
                "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "mock.safetensors"}},
            }),
            encoding="utf-8",
        )
        return path

    def test_doctor_reachable(self):
        rc, p = self.kz("Y5", "--endpoint", self.url, "--timeout", "10")
        self.assertEqual(rc, 0, p)
        self.assertTrue(p.get("reachable"), p)
        self.assertEqual(p.get("comfyui_version"), "mock-1.0", p)

    def test_live_run_saves_outputs_and_replays(self):
        wf = self._workflow()
        rc, p = self.kz("Y1", "--path", str(wf), "--template", "live", "--endpoint", self.url,
                        "--summary", "Live mock run.", "--timeout", "10")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("run_status"), "completed", p)
        self.assertEqual(p.get("prompt_id"), "mockprompt1", p)
        self.assertEqual(p.get("seed"), "777", p)
        self.assertTrue(p.get("outputs"), p)
        self.assertTrue(p.get("output_artifact_ids"), p)
        self.assertIsNotNone(p.get("latency_ms"), p)

        saved = self.root / "AI" / "generation" / "live" / "out.png"
        self.assertTrue(saved.is_file(), f"expected saved output at {saved}")
        self.assertEqual(saved.read_bytes(), _PNG)

        rc2, p2 = self.kz("Y4", "--id", p["id"], "--endpoint", self.url, "--timeout", "10")
        self.assertEqual(rc2, 0, p2)
        self.assertEqual(p2.get("replay_of"), p["id"], p2)
        self.assertEqual(p2.get("run_status"), "completed", p2)

    def test_multitype_outputs_all_saved(self):
        wf = self._workflow()
        rc, p = self.kz("Y1", "--path", str(wf), "--template", "multi", "--endpoint", self.url,
                        "--summary", "Multi output.", "--timeout", "10")
        self.assertEqual(rc, 0, p)
        base = self.root / "AI" / "generation" / "multi"
        for name in ("out.png", "out.mp4", "out.flac"):
            self.assertTrue((base / name).is_file(), f"expected {name} saved")
        self.assertEqual(len(p.get("output_artifact_ids", [])), 3, p)

    def test_validate_passes_for_present_ckpt(self):
        wf = self._workflow()  # ckpt mock.safetensors is in the mock /object_info choices
        rc, p = self.kz("Y1", "--path", str(wf), "--template", "valok", "--endpoint", self.url,
                        "--validate", "--summary", "Validated run.", "--timeout", "10")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("run_status"), "completed", p)

    def test_validate_denies_missing_asset(self):
        path = self.root / "bad_ckpt.json"
        path.write_text(json.dumps({
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "missing.safetensors"}},
        }), encoding="utf-8")
        rc, p = self.kz("Y1", "--path", str(path), "--endpoint", self.url, "--validate",
                        "--summary", "Missing asset.", "--timeout", "10")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_ASSET_MISSING", p)

    def test_validate_denies_unknown_node(self):
        path = self.root / "bad_node.json"
        path.write_text(json.dumps({
            "1": {"class_type": "NoSuchNode", "inputs": {}},
        }), encoding="utf-8")
        rc, p = self.kz("Y1", "--path", str(path), "--endpoint", self.url, "--validate",
                        "--summary", "Unknown node.", "--timeout", "10")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_WORKFLOW_NODES_UNKNOWN", p)

    def test_y8_api_run_against_mock(self):
        wf = self._workflow()
        rc, p = self.kz("Y8", "--workflow-file", str(wf), "--template", "y8live", "--route", "api",
                        "--endpoint", self.url, "--summary", "Y8 run.", "--timeout", "10")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("run_status"), "completed", p)
        self.assertEqual(p.get("route"), "api", p)

    def test_y9_denies_when_mcp_unpinned(self):
        wf = self._workflow()  # seed 777 present, so the seed gate passes and the pin gate fires
        rc, p = self.kz("Y9", "--workflow-file", str(wf), "--endpoint", self.url, "--summary", "Y9.", "--timeout", "10")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_MCP_NOT_PINNED", p)
        rc2, lst = self.kz("Y3")
        self.assertEqual(rc2, 0, lst)
        self.assertEqual(lst.get("records"), [], "api lane must not half-record when the MCP pin is missing")


if __name__ == "__main__":
    unittest.main()
