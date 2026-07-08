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

    def test_workflow_file_alias(self):
        wf = self._workflow_path()
        rc, p = self.kz("Y1", "--workflow-file", str(wf), "--template", "alias", "--dry-run", "--test", "--summary", "Alias.")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("seed"), "42424242", p)

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


class RouteRecordingTest(IsolatedDBTest):
    def _wf(self) -> Path:
        path = self.root / "wf.json"
        path.write_text(json.dumps(_WORKFLOW), encoding="utf-8")
        return path

    def test_dry_run_writes_api_route_row(self):
        rc, p = self.kz("Y1", "--workflow-file", str(self._wf()), "--dry-run", "--test", "--summary", "Route.")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("route"), "api", p)
        rc2, p2 = self.kz("Y2", "--id", p["id"])
        self.assertEqual(rc2, 0, p2)
        self.assertEqual(p2["record"]["route_info"]["route"], "api", p2)

    def test_validate_conflicts_with_dry_run(self):
        rc, p = self.kz("Y1", "--workflow-file", str(self._wf()), "--dry-run", "--validate", "--test", "--summary", "X.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_FLAG_CONFLICT", p)


class GenerateY8Test(IsolatedDBTest):
    def _wf(self, obj) -> Path:
        path = self.root / "wf.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return path

    def test_prompt_substitution_and_api_route(self):
        wf = self._wf({
            "3": {"class_type": "KSampler", "inputs": {"seed": 5, "steps": 8}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a {{PROMPT}} scene"}},
        })
        rc, p = self.kz("Y8", "--workflow-file", str(wf), "--template", "gen", "--prompt", "sunset",
                        "--dry-run", "--test", "--summary", "Gen.")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("route"), "api", p)
        resolved = list((self.root / "AI" / "generation" / "gen").glob("y8-*.json"))
        self.assertTrue(resolved, "resolved workflow not written")
        self.assertIn("sunset", resolved[0].read_text(encoding="utf-8"))

    def test_placeholder_missing_denied(self):
        wf = self._wf(_WORKFLOW)  # no {{PROMPT}}
        rc, p = self.kz("Y8", "--workflow-file", str(wf), "--prompt", "x", "--dry-run", "--test", "--summary", "M.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_PROMPT_PLACEHOLDER_MISSING", p)

    def test_seed_override_recorded(self):
        wf = self._wf(_WORKFLOW)  # KSampler seed 42424242
        rc, p = self.kz("Y8", "--workflow-file", str(wf), "--seed", "999", "--dry-run", "--test", "--summary", "S.")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("seed"), "999", p)

    def test_bare_y8_denies_path_required(self):
        rc, p = self.kz("Y8")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_PATH_REQUIRED", p)

    def test_bare_y9_denies_path_required(self):
        rc, p = self.kz("Y9")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_PATH_REQUIRED", p)


class GenerativeRouteSchemaTest(IsolatedDBTest):
    def test_route_schema_validates(self):
        rc, p = self.kz("Q8", "--kind", "generative_run_route", "--payload-json", '{"run_id":"gr_x","route":"api"}')
        self.assertEqual(rc, 0, p)

    def test_route_enum_rejected(self):
        rc, p = self.kz("Q8", "--kind", "generative_run_route", "--payload-json", '{"run_id":"gr_x","route":"bogus"}')
        self.assertEqual(rc, 2, p)


if __name__ == "__main__":
    unittest.main()
