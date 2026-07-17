"""Codex live acceptance (plan v3 "Live acceptance" + H2.3 live legs), gated + fail-closed-durable.

Two classes, both structured to leave a durable NOT-RUN reason in test output rather than a silent pass:

- InstalledCodexProbeTest runs whenever the ``codex`` binary is on PATH (OFFLINE -- installed_capability
  spawns codex only for schema GENERATION, never an authenticated app-server turn). It asserts the
  canonical descriptor is ALWAYS structurally complete and lands on one of the two legal shapes: the
  schema-pass ``degraded`` shape, or a fail-closed denial. On the installed codex-cli 0.130 the bounded
  read-access schema probe fails, so the descriptor is the ``policy_gate_unavailable`` denial and the
  message must name the read/bounded limitation.
- LiveCodexDrivenLegsTest is gated by KAIZEN_RUN_LIVE=1 + codex on PATH. It re-probes and, unless the
  descriptor is the schema-pass shape, skipTest("blocked-by-vendor-version: ...") -- the intended durable
  record on today's machine. When schema-pass, it drives the plan's live legs (explicit-profile start ->
  two context-linked turns -> close success; one approval; one Plan-denial with the file proven absent)
  through the real Supervisor session ops, exactly as test_session_drive.LiveDrivenSmokeTest does for
  local_llm, confined to a gitignored scratch root under AI/work.

The real ``codex`` binary is spawned here (schema generation, and app-server only on a schema-pass build);
all scratch is workspace-owned, id-scoped, and cleaned up.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import REPO_ROOT, kaizen  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))
from kaizen_components.orchestration.adapters import codex  # noqa: E402
from test_session_drive import _PREAMBLE, _rmtree  # noqa: E402

_LIVE = os.environ.get("KAIZEN_RUN_LIVE") == "1"
_HAVE_CODEX = shutil.which("codex") is not None

# The two fail-closed availability codes a non-schema-pass installed build may report.
_FAIL_CLOSED_CODES = frozenset({codex.DENIED_POLICY_GATE_UNAVAILABLE, codex.DENIED_ENGINE_UNAVAILABLE})
# Scratch capability cache + live-leg workspace live under gitignored AI/work.
_CAPABILITY_CACHE = REPO_ROOT / "AI/work/orchestration/runtime/capability-cache/codex-live-test"
_LIVE_SCRATCH = REPO_ROOT / "AI/work/h2.3-live-codex"


def _is_schema_pass(descriptor: dict) -> bool:
    # The schema-pass shape (installed_capability): degraded (runtime validates at open) + full mode set.
    availability = descriptor.get("availability") or {}
    return (availability.get("state") == "degraded"
            and list(descriptor.get("permission_modes") or []) == list(codex.SUPPORTED_PERMISSION_MODES))


@unittest.skipUnless(_HAVE_CODEX, "codex binary not on PATH")
class InstalledCodexProbeTest(unittest.TestCase):
    """OFFLINE structural probe of the installed codex capability descriptor (no authenticated turn)."""

    def _probe(self) -> dict:
        _CAPABILITY_CACHE.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="probe-", dir=str(_CAPABILITY_CACHE)))
        self.addCleanup(_rmtree, scratch)
        return codex.installed_capability(runtime_dir=str(scratch))

    def test_installed_descriptor_is_structurally_complete_and_legal(self):
        descriptor = self._probe()
        # Structural completeness holds for EVERY shape.
        self.assertEqual(descriptor["id"], "codex")
        self.assertIsInstance(descriptor.get("warnings"), list)
        self.assertIsInstance(descriptor.get("permission_modes"), list)
        availability = descriptor.get("availability")
        self.assertIsInstance(availability, dict)
        for key in ("state", "code", "message"):
            self.assertTrue(availability.get(key), f"availability.{key} must be non-empty: {descriptor}")

        # Exactly one of the two legal shapes.
        if _is_schema_pass(descriptor):
            self.assertEqual(list(descriptor["permission_modes"]), list(codex.SUPPORTED_PERMISSION_MODES))
        else:
            self.assertIn(availability["code"], _FAIL_CLOSED_CODES, descriptor)
            self.assertTrue(str(availability["message"]).strip())
            self.assertEqual(descriptor["permission_modes"], [])
            # The installed 0.130 lands here (bounded read-access probe fails): the message must name the
            # read/bounded limitation (lenient substring -- exact wording may evolve across builds).
            if availability["state"] == "policy_gate_unavailable":
                message = str(availability["message"]).lower()
                if not message.startswith("codex app-server is missing required methods:"):
                    self.assertTrue("read" in message or "bounded" in message, availability["message"])


@unittest.skipUnless(
    _LIVE and _HAVE_CODEX,
    "live codex driven legs -- set KAIZEN_RUN_LIVE=1 with codex on PATH",
)
class LiveCodexDrivenLegsTest(unittest.TestCase):
    """The plan's per-engine live acceptance for Codex, driven through the real Supervisor session ops.

    Re-probes first: unless the installed build is the schema-pass shape, the legs are blocked-by-vendor-
    version and skip with the structured availability message (the durable not-run reason). On a schema-
    pass build these drive: explicit-profile start (engine codex, permission_mode ask) -> two context-
    linked turns -> close with terminal_state 'success'; one approval leg; one Plan-denial leg with the
    denied out-of-profile target proven absent afterwards. All writes stay under a workspace-owned scratch root.
    """

    def setUp(self) -> None:
        _CAPABILITY_CACHE.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="probe-", dir=str(_CAPABILITY_CACHE)))
        self.addCleanup(_rmtree, scratch)
        descriptor = codex.installed_capability(runtime_dir=str(scratch))
        if not _is_schema_pass(descriptor):
            availability = descriptor.get("availability") or {}
            self.skipTest("blocked-by-vendor-version: " + str(availability.get("message") or availability))

    def _drive_real(self, body: str) -> dict:
        # A fresh, K1-initialized AI/work scratch KAIZEN_REPO_ROOT; the scenario runs in a child process
        # driving a real in-process Supervisor over the real codex adapter (no _adapter_factory seam).
        script = "BODY = " + repr(body) + "\n" + _PREAMBLE
        full = dict(os.environ)
        _LIVE_SCRATCH.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="codex-live-", dir=str(_LIVE_SCRATCH)))
        try:
            rc, payload = kaizen(root, "K1", timeout=60.0)
            self.assertEqual(rc, 0, payload)
            full["KAIZEN_REPO_ROOT"] = str(root)
            proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                  cwd=str(REPO_ROOT), env=full, timeout=600)
            self.assertEqual(
                proc.returncode,
                0,
                f"scenario child exited {proc.returncode}.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-2000:]}",
            )
            for line in proc.stdout.splitlines():
                if line.startswith("RESULT "):
                    return json.loads(line[len("RESULT "):])
            self.fail(f"no RESULT.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-2000:]}")
        finally:
            _rmtree(root)

    def test_codex_driven_two_turns_share_context_and_close_once(self) -> None:
        # Explicit-profile start (codex, ask) -> two context-linked turns -> non-terminal while idle ->
        # explicit close writes exactly one successful T8; the marker from turn 1 recurs in turn 2's reply.
        marker = "KZCODEX8317"
        first_prompt = (
            f'Remember marker {marker}. Reply with exactly {{"final": "remembered {marker}"}} and nothing else.'
        )
        second_prompt = (
            'Reply with exactly one JSON object whose "final" value contains the marker from my previous prompt.'
        )
        body = (
            "sup = Supervisor(); sup.boot()\n"
            "sup._driven_test_records = True\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'codex', 'prompt': "
            + repr(first_prompt) + ", 'max_turns': 3, 'profile': {'permission_mode': 'ask'}}})\n"
            "rid, sid = start.get('agent_run_id'), start.get('session_id')\n"
            "wait_idle(sup, rid, budget=180.0)\n"
            "pre1 = sup._safe_reduce(rid)\n"
            "turn2 = sup._handle_control({'op': 'session/turn', 'args': {'agent_run_id': rid, 'prompt': "
            + repr(second_prompt) + "}})\n"
            "wait_idle(sup, rid, budget=180.0)\n"
            "from kaizen_components import db\n"
            "pre2 = sup._safe_reduce(rid)\n"
            "t8_before = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
            "close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "state = sup._safe_reduce(rid)\n"
            "sess = db.fetch_one('SELECT id, is_test FROM agent_sessions WHERE id = ?', (sid,))\n"
            "runrow = db.fetch_one('SELECT id, surface, is_test FROM agent_runs WHERE id = ?', (rid,))\n"
            "t8_after = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
            "chat = [json.loads(r[0]) for r in db.fetch_all(\"SELECT body FROM agent_events WHERE agent_run_id = ? AND event_kind = 'chat_message' ORDER BY sequence_no\", (rid,))]\n"
            "children_after = len(sup._children)\n"
            "sup.shutdown()\n"
            "out = {'start_status': start.get('status'), 'session': bool(sess),\n"
            "       'run_surface': runrow[1] if runrow else None, 'turn2': turn2, 'close': close,\n"
            "       'pre1': pre1['terminal'] if pre1 else None, 'pre2': pre2['terminal'] if pre2 else None,\n"
            "       't8_before': t8_before, 't8_after': t8_after, 'chat': chat,\n"
            "       'children_after': children_after,\n"
            "       'terminal': state['terminal'] if state else None,\n"
            "       'terminal_state': state.get('terminal_state') if state else None,\n"
            "       'run_id': rid, 'session_id': sid}\n"
        )
        out = self._drive_real(body)
        self.assertEqual(out["start_status"], "OK", out)
        self.assertTrue(out["session"])
        self.assertEqual(out["run_surface"], "app-server")
        self.assertEqual(out["turn2"]["status"], "OK")
        self.assertFalse(out["pre1"])
        self.assertFalse(out["pre2"])
        self.assertEqual(out["t8_before"], 0)
        self.assertEqual([m["role"] for m in out["chat"]], ["user", "assistant", "user", "assistant"], out)
        self.assertIn(marker, out["chat"][-1]["text"])
        self.assertEqual(out["close"]["status"], "OK")
        self.assertEqual(out["t8_after"], 1)
        self.assertEqual(out["children_after"], 0)
        self.assertTrue(out["terminal"])
        self.assertEqual(out["terminal_state"], "success")

    def test_codex_approval_round_trip_by_correlation_id(self) -> None:
        # The governed lane: the agent proposes an in-profile write, the ask lattice gates, the driver
        # approves by the stream correlation_id, the tool runs (ground truth: the file content), the turn
        # finals. All rows + the written file are deleted by id afterwards.
        target_rel = "AI/work/harness-ui-v1/codex-live-approval.txt"
        marker = "kaizen-codex-live-approval"
        prompt = (
            'Write the text "' + marker + '" to the file "' + target_rel + '". After the tool result '
            'arrives, reply with {"final": "written"}.'
        )
        body = (
            "sup = Supervisor(); sup.boot()\n"
            "sup._driven_test_records = True\n"
            "from pathlib import Path as _P\n"
            "target = _P(str(sup.repo_root)) / " + repr(target_rel) + "\n"
            "if target.exists(): target.unlink()\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'codex', 'prompt': "
            + repr(prompt) + ", 'max_turns': 4, 'approval_timeout': 180.0, 'profile': {'permission_mode': 'ask'}}})\n"
            "rid, sid = start.get('agent_run_id'), start.get('session_id')\n"
            "corr = wait_open_approval(sup, rid, budget=180.0)\n"
            "parked = wait_waiter_parked(sup, rid, corr, budget=30.0) if corr else False\n"
            "dec = sup._handle_control({'op': 'approve', 'args': {'correlation_id': corr, 'session_id': sid, "
            "'decision': 'approve'}}) if corr else {'status': 'NO_ASK'}\n"
            "state = wait_terminal(sup, rid, budget=180.0)\n"
            "from kaizen_components import db\n"
            "c4 = db.fetch_one('SELECT state, decided_by FROM approval_requests WHERE session_id = ? AND "
            "correlation_id = ?', (sid, corr)) if corr else None\n"
            "written = target.read_text(encoding='utf-8') if target.exists() else None\n"
            "if target.exists(): target.unlink()\n"
            "children_after = len(sup._children)\n"
            "sup.shutdown()\n"
            "out = {'start_status': start.get('status'), 'corr': corr, 'parked': parked,\n"
            "       'decide_status': dec.get('status'), 'waiter_released': dec.get('waiter_released'),\n"
            "       'c4': list(c4) if c4 else None, 'written': written, 'children_after': children_after,\n"
            "       'terminal': state['terminal'] if state else None,\n"
            "       'terminal_state': state.get('terminal_state') if state else None,\n"
            "       'run_id': rid, 'session_id': sid}\n"
        )
        out = self._drive_real(body)
        self.assertEqual(out["start_status"], "OK", out)
        self.assertIsNotNone(out["corr"], "no approval/open reached the stream (policy did not ASK?)")
        self.assertTrue(out["parked"])
        self.assertEqual(out["decide_status"], "OK")
        self.assertTrue(out["waiter_released"])
        self.assertEqual(out["c4"], ["approved", "human"])
        self.assertEqual(out["written"], marker)
        self.assertEqual(out["children_after"], 0)
        self.assertTrue(out["terminal"])
        self.assertEqual(out["terminal_state"], "success")

    def test_codex_plan_denies_out_of_profile_write_and_closes_once(self) -> None:
        # Negative live proof. The target is a harmless workspace-owned scratch file outside the isolated
        # KAIZEN_REPO_ROOT; Plan's write ceiling must deny before tool execution, the conversation stays
        # idle/non-terminal until explicit close, and the file is proven absent afterwards.
        target = REPO_ROOT / "AI/work/harness-ui-v1/codex-live-denied-outside.txt"
        marker = "kaizen-codex-must-not-exist"
        prompt = (
            'Write the text "' + marker + '" to the file "' + str(target) + '". If the tool is denied, '
            'reply with exactly {"final": "denied"}.'
        )
        if target.exists():
            target.unlink()
        try:
            body = (
                "sup = Supervisor(); sup.boot()\n"
                "sup._driven_test_records = True\n"
                "from pathlib import Path as _P\n"
                "target = _P(" + repr(str(target)) + ")\n"
                "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'codex', 'prompt': "
                + repr(prompt) + ", 'max_turns': 4, 'profile': {'permission_mode': 'plan'}}})\n"
                "rid, sid = start.get('agent_run_id'), start.get('session_id')\n"
                "wait_idle(sup, rid, budget=180.0)\n"
                "from kaizen_components import db\n"
                "pre = sup._safe_reduce(rid)\n"
                "t8_before = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
                "declined = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'approval' AND marker = 'declined'\", (rid,))[0]\n"
                "exists_before_close = target.exists()\n"
                "close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
                "state = sup._safe_reduce(rid)\n"
                "t8_after = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
                "if target.exists(): target.unlink()\n"
                "absent_after = not target.exists()\n"
                "children_after = len(sup._children)\n"
                "sup.shutdown()\n"
                "out = {'start_status': start.get('status'), 'declined': declined,\n"
                "       'exists_before_close': exists_before_close, 'absent_after': absent_after,\n"
                "       'pre_terminal': pre['terminal'] if pre else None, 't8_before': t8_before,\n"
                "       't8_after': t8_after, 'close': close, 'children_after': children_after,\n"
                "       'terminal': state['terminal'] if state else None,\n"
                "       'terminal_state': state.get('terminal_state') if state else None,\n"
                "       'run_id': rid, 'session_id': sid}\n"
            )
            out = self._drive_real(body)
        finally:
            if target.exists():
                target.unlink()
        self.assertEqual(out["start_status"], "OK", out)
        self.assertGreaterEqual(out["declined"], 1, out)
        self.assertFalse(out["exists_before_close"], out)
        self.assertTrue(out["absent_after"], out)
        self.assertFalse(out["pre_terminal"], out)
        self.assertEqual(out["t8_before"], 0, out)
        self.assertEqual(out["close"]["status"], "OK", out)
        self.assertEqual(out["t8_after"], 1, out)
        self.assertEqual(out["children_after"], 0, out)
        self.assertTrue(out["terminal"], out)
        self.assertEqual(out["terminal_state"], "success", out)


if __name__ == "__main__":
    unittest.main()
