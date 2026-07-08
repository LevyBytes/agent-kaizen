"""Y7 comfy-mcp offline paths: pure gate scoring + tool classification + candidate table + the
action-required / unknown-candidate / mcp-absent denials. Live doctor/bakeoff/run need the optional
mcp client, node, and running servers -- exercised in the runbook behind KAIZEN_RUN_COMFY_MCP_LIVE."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import REPO_ROOT, IsolatedDBTest  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))
from kaizen_components.comfy_mcp import CANDIDATES, classify_tools, score_candidate  # noqa: E402

try:
    import mcp  # noqa: F401
    _MCP = True
except ImportError:
    _MCP = False


def _good_doctor() -> dict:
    return {
        "prereq_ok": True,
        "probe_ok": True,
        "capabilities": {"submit": ["enqueue_workflow"], "status": ["get_job_status"], "fetch_outputs": ["list_output_images"]},
    }


_GOOD_SPEC = {"command": "npx", "transport": "stdio", "env": {"COMFYUI_URL": "<endpoint>"}, "license": "MIT"}


class ScoreCandidateTest(unittest.TestCase):
    def test_all_gates_pass(self):
        s = score_candidate(_good_doctor(), _GOOD_SPEC)
        self.assertTrue(s["hard_pass"], s)

    def test_no_submit_tool_fails_hard(self):
        d = _good_doctor()
        d["capabilities"].pop("submit")
        s = score_candidate(d, _GOOD_SPEC)
        self.assertFalse(s["hard_pass"], s)
        self.assertEqual(s["gates"]["submit"], "fail", s)

    def test_unlaunchable_candidate_fails_reproducible(self):
        spec = {**_GOOD_SPEC, "command": None}
        s = score_candidate(_good_doctor(), spec)
        self.assertFalse(s["hard_pass"], s)
        self.assertEqual(s["gates"]["install_reproducible"], "fail", s)
        self.assertEqual(s["gates"]["spawns_stdio"], "fail", s)

    def test_http_transport_fails_stdio_gate(self):
        spec = {**_GOOD_SPEC, "transport": "http"}
        s = score_candidate(_good_doctor(), spec)
        self.assertEqual(s["gates"]["spawns_stdio"], "fail", s)


class ClassifyToolsTest(unittest.TestCase):
    def test_keyword_mapping(self):
        caps = classify_tools([
            {"name": "enqueue_workflow", "description": "submit a workflow"},
            {"name": "get_job_status", "description": "poll status"},
        ])
        self.assertIn("submit", caps)
        self.assertIn("status", caps)


class CandidateTableTest(unittest.TestCase):
    def test_every_candidate_has_repo_and_license(self):
        self.assertIn("artokun", CANDIDATES)
        for slug, spec in CANDIDATES.items():
            self.assertIn("/", spec["repo"], slug)
            self.assertTrue(spec["license"], slug)


class McpCliTest(IsolatedDBTest):
    def test_bare_y7_denies_action_required(self):
        rc, p = self.kz("Y7")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_ACTION_REQUIRED", p)

    def test_unknown_candidate_doctor_denied(self):
        rc, p = self.kz("Y7", "--action", "doctor", "--candidate", "nope")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_CANDIDATE_UNKNOWN", p)

    @unittest.skipIf(_MCP, "mcp client installed: bakeoff would spawn real servers")
    def test_bakeoff_denies_without_mcp(self):
        rc, p = self.kz("Y7", "--action", "bakeoff")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNAVAILABLE", p)


if __name__ == "__main__":
    unittest.main()
