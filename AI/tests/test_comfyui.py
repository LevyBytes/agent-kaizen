"""ComfyUI generative-run ops (Y*): dispatch, dry-run recording, deterministic hash,
graceful backend-absent denial. No ComfyUI server is required — the dry-run path makes
no network call, and the doctor test asserts the graceful capability denial."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import IsolatedDBTest  # noqa: E402


_WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 42424242, "steps": 12, "cfg": 7.0}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "demo_v1.safetensors"}},
}


class ComfyDoctorTest(IsolatedDBTest):
    def test_doctor_unreachable_denies_gracefully(self):
        # No ComfyUI server on this closed port -> graceful capability denial, never a crash.
        rc, p = self.kz("Y5", "--endpoint", "http://127.0.0.1:6", "--timeout", "2")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNAVAILABLE", p)


class ComfyDryRunTest(IsolatedDBTest):
    def _workflow_path(self) -> Path:
        path = self.root / "wf.json"
        path.write_text(json.dumps(_WORKFLOW), encoding="utf-8")
        return path

    def test_dry_run_records_queued_run_without_network(self):
        wf = self._workflow_path()
        rc, p = self.kz("Y1", "--path", str(wf), "--template", "smoke", "--dry-run", "--summary", "Test dry run.")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("run_status"), "queued", p)
        self.assertTrue(p.get("dry_run"), p)
        self.assertEqual(len(p.get("workflow_hash", "")), 64, p)
        self.assertEqual(p.get("seed"), "42424242", p)
        self.assertEqual(p.get("models"), ["demo_v1.safetensors"], p)
        self.assertTrue(p.get("workflow_artifact_id"), p)

        run_id = p["id"]
        rc2, ins = self.kz("Y2", "--id", run_id)
        self.assertEqual(rc2, 0, ins)
        self.assertEqual(ins["record"]["status"], "queued", ins)
        self.assertEqual(ins["record"]["backend"], "comfyui", ins)
        self.assertTrue(ins["record"]["workflow_artifact_id"], ins)

        rc3, lst = self.kz("Y3")
        self.assertEqual(rc3, 0, lst)
        self.assertEqual(len(lst["records"]), 1, lst)

    def test_deterministic_workflow_hash(self):
        wf = self._workflow_path()
        rc1, p1 = self.kz("Y1", "--path", str(wf), "--template", "a", "--dry-run", "--summary", "One.")
        rc2, p2 = self.kz("Y1", "--path", str(wf), "--template", "b", "--dry-run", "--summary", "Two.")
        self.assertEqual(rc1, 0, p1)
        self.assertEqual(rc2, 0, p2)
        self.assertEqual(p1["workflow_hash"], p2["workflow_hash"], "same workflow must hash identically")


class ComfyDenialTest(IsolatedDBTest):
    def test_missing_path_denied(self):
        rc, p = self.kz("Y1", "--template", "smoke", "--dry-run")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_PATH_REQUIRED", p)

    def test_replay_unknown_run_denied(self):
        rc, p = self.kz("Y4", "--id", "gr_does_not_exist")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_RECORD_NOT_FOUND", p)

    def test_bad_workflow_type_denied(self):
        path = self.root / "bad.json"
        path.write_text('["not", "an", "object"]', encoding="utf-8")
        rc, p = self.kz("Y1", "--path", str(path), "--dry-run")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_WORKFLOW_TYPE", p)


if __name__ == "__main__":
    unittest.main()
