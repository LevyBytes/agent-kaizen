"""Live Claude driving smoke legs (H2.4 release gate) through the REAL supervisor + REAL claude binary.

Gated OFF by default: every class @skipUnless(KAIZEN_RUN_LIVE == "1" and shutil.which("claude")). The
orchestrator runs these AFTER the offline suite; a normal `unittest discover` run imports and skips them.

Conventions mirror test_session_drive.LiveDrivenSmokeTest: a real in-process Supervisor runs in a subprocess pinned to a fresh gitignored KAIZEN_REPO_ROOT under AI/work, with subscription auth, bounded timeouts, and id-scoped row cleanup. Each scenario prints a RESULT JSON line. Four legs: two-turn resumed session, an ask-mode approval round-trip, a plan-mode out-of-workspace denial, and a designated-root plan-mode allow case.
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
from test_session_drive import _PREAMBLE, _rmtree  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))

_LIVE = os.environ.get("KAIZEN_RUN_LIVE") == "1"
_HAVE_CLAUDE = shutil.which("claude") is not None


@unittest.skipUnless(
    _LIVE and _HAVE_CLAUDE,
    "live Claude smoke -- set KAIZEN_RUN_LIVE=1 with the real claude CLI installed",
)
class LiveClaudeDrivenSmokeTest(unittest.TestCase):
    """Real subscription-auth Claude turns in an isolated AI/work workspace via the production adapter."""

    # Id-scoped cleanup of one driven run's rows (events -> run -> approvals -> session).
    _CLEANUP = (
        "conn = db.connect()\n"
        "conn.execute('DELETE FROM agent_events WHERE agent_run_id = ?', (rid,))\n"
        "conn.execute('DELETE FROM agent_runs WHERE id = ?', (rid,))\n"
        "conn.execute('DELETE FROM approval_requests WHERE session_id = ?', (sid,))\n"
        "conn.execute('DELETE FROM agent_sessions WHERE id = ?', (sid,))\n"
        "conn.commit()\n"
    )

    def _drive_real(self, body: str, *, timeout: int = 600) -> dict:
        """State fresh-plane initialization, bounded child execution, RESULT parsing, and unconditional plane cleanup."""
        script = "BODY = " + repr(body) + "\n" + _PREAMBLE
        full = dict(os.environ)
        scratch_parent = REPO_ROOT / "AI/work/h2.4-live-claude"
        scratch_parent.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="h2.4-live-claude-", dir=str(scratch_parent)))
        try:
            rc, payload = kaizen(root, "K1", timeout=60.0)
            self.assertEqual(rc, 0, payload)
            full["KAIZEN_REPO_ROOT"] = str(root)
            proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                  cwd=str(REPO_ROOT), env=full, timeout=timeout)
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

    def test_live_two_turn_resumed_session_shares_context(self) -> None:
        # Two context-linked turns share one C1/T5; the second reply must recall the first-turn codeword
        # (real vendor-session resume). Explicit close writes exactly one successful T8; no leaked children.
        codeword = "kzn-" + os.urandom(4).hex()
        first_prompt = f"Remember the codeword: {codeword}. Reply exactly OK."
        second_prompt = "What was the codeword? Reply with just the codeword."
        body = (
            "sup = Supervisor(); sup.boot()\n"
            "sup._driven_test_records = True\n"
            "children_before = len(sup._children)\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'claude', 'prompt': "
            + repr(first_prompt) + ", 'profile': {'permission_mode': 'ask', 'auth_mode': 'subscription'}}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "wait_idle(sup, rid, budget=180.0)\n"
            "pre1 = sup._safe_reduce(rid)\n"
            "from kaizen_components import db\n"
            "t8_before = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
            "turn2 = sup._handle_control({'op': 'session/turn', 'args': {'agent_run_id': rid, 'prompt': "
            + repr(second_prompt) + "}})\n"
            "wait_idle(sup, rid, budget=180.0)\n"
            "chat = [json.loads(r[0]) for r in db.fetch_all(\"SELECT body FROM agent_events WHERE agent_run_id = ? AND event_kind = 'chat_message' ORDER BY sequence_no\", (rid,))]\n"
            "counts = [db.fetch_one('SELECT COUNT(*) FROM agent_sessions WHERE id = ?', (sid,))[0],\n"
            "          db.fetch_one('SELECT COUNT(*) FROM agent_runs WHERE id = ?', (rid,))[0]]\n"
            "close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "state = sup._safe_reduce(rid)\n"
            "t8_after = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization' AND marker = 'close_ok'\", (rid,))[0]\n"
            "from pathlib import Path as _P\n"
            "rroot = _P(str(sup.repo_root)) / 'AI' / 'work' / 'orchestration' / 'runtime' / 'claude-driving'\n"
            "leftover = sorted(p.name for p in rroot.iterdir()) if rroot.exists() else []\n"
            "children_after = len(sup._children)\n"
            "sup.shutdown()\n"
            + self._CLEANUP +
            "out = {'start': start['status'], 'turn2': turn2['status'], 'close': close['status'],\n"
            "    'counts': counts, 'pre1_terminal': pre1['terminal'], 't8_before': t8_before, 't8_after': t8_after,\n"
            "    'chat_roles': [m['role'] for m in chat], 'last_text': chat[-1]['text'] if chat else '',\n"
            "    'leftover': leftover, 'children_before': children_before, 'children_after': children_after,\n"
            "    'terminal': state['terminal'] if state else None, 'terminal_state': state.get('terminal_state') if state else None}\n"
        )
        out = self._drive_real(body)
        self.assertEqual(out["start"], "OK", out)
        self.assertEqual(out["turn2"], "OK", out)
        self.assertEqual(out["counts"], [1, 1], out)
        self.assertFalse(out["pre1_terminal"], out)
        self.assertEqual(out["t8_before"], 0, out)
        self.assertEqual(out["chat_roles"], ["user", "assistant", "user", "assistant"], out)
        self.assertIn(codeword, out["last_text"], out)  # real first-turn context continuity
        self.assertEqual(out["close"], "OK", out)
        self.assertEqual(out["t8_after"], 1, out)
        self.assertEqual(out["leftover"], [], out)
        self.assertEqual(out["children_before"], 0, out)
        self.assertEqual(out["children_after"], 0, out)
        self.assertTrue(out["terminal"], out)
        self.assertEqual(out["terminal_state"], "success", out)

    def test_live_ask_mode_approval_round_trip_writes_file(self) -> None:
        # ask mode: a workspace write gates ASK -> an approval event; approve by the stream
        # correlation_id; the file exists after the turn; close success. The prompt names an ABSOLUTE
        # in-plane target: the plane nests inside the real repo (AI/work/h2.4-live-claude/<plane>), so a
        # relative path whose segments match the plane's parent chain lets the model escape to the
        # enclosing repo (observed live 2026-07-10: gated write_out + approved into the REAL repo).
        marker = "kaizen-live-claude-approve"
        body = (
            "sup = Supervisor(); sup.boot()\n"
            "sup._driven_test_records = True\n"
            "from pathlib import Path as _P\n"
            "target = _P(str(sup.repo_root)) / 'live-claude-approval.txt'\n"
            "if target.exists(): target.unlink()\n"
            "prompt = 'Use the Write tool to create the file at the absolute path ' + json.dumps(str(target)) + "
            "' with the exact contents \"" + marker + "\". After it is written, reply exactly DONE.'\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'claude', 'prompt': prompt,"
            " 'approval_timeout': 180.0, 'profile': {'permission_mode': 'ask', 'auth_mode': 'subscription'}}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "corr = wait_open_approval(sup, rid, budget=180.0)\n"
            "parked = wait_waiter_parked(sup, rid, corr, budget=60.0) if corr else False\n"
            "dec = sup._handle_control({'op': 'approve', 'args': {'correlation_id': corr, 'session_id': sid, 'decision': 'approve'}}) if corr else {'status': 'NO_ASK'}\n"
            "wait_idle(sup, rid, budget=180.0)\n"
            "from kaizen_components import db\n"
            "written = target.read_text(encoding='utf-8') if target.exists() else None\n"
            "if target.exists(): target.unlink()\n"
            "close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "state = sup._safe_reduce(rid)\n"
            "children_after = len(sup._children)\n"
            "sup.shutdown()\n"
            + self._CLEANUP +
            "out = {'start': start['status'], 'corr': corr, 'parked': parked, 'decide': dec.get('status'),\n"
            "    'written': written, 'close': close['status'], 'children_after': children_after,\n"
            "    'terminal': state['terminal'] if state else None, 'terminal_state': state.get('terminal_state') if state else None}\n"
        )
        out = self._drive_real(body)
        self.assertEqual(out["start"], "OK", out)
        self.assertIsNotNone(out["corr"], "no approval/open reached the stream (ask-mode write did not gate?)")
        self.assertTrue(out["parked"], out)
        self.assertEqual(out["decide"], "OK", out)
        self.assertEqual(out["written"], marker, out)  # file exists with approved contents after the turn
        self.assertEqual(out["close"], "OK", out)
        self.assertEqual(out["children_after"], 0, out)
        self.assertTrue(out["terminal"], out)
        self.assertEqual(out["terminal_state"], "success", out)

    def test_live_plan_mode_allows_designated_root_write_without_ask(self) -> None:
        # H2.7 positive live proof (plan v3 live acceptance: "one allowed designated write" per drivable
        # engine). Plan mode seeds AI/work + AI/generation as designated write roots, so a Write into the
        # plane's AI/work must pass the mediated gate as ALLOW (no approval event, no C4 row) and land on
        # disk. Absolute in-plane target for the same live-observed reason as the approval leg.
        marker = "kaizen-live-claude-designated-allow"
        body = (
            "sup = Supervisor(); sup.boot()\n"
            "sup._driven_test_records = True\n"
            "from pathlib import Path as _P\n"
            "target = _P(str(sup.repo_root)) / 'AI' / 'work' / 'h2.7-claude-designated-write.txt'\n"
            "if target.exists(): target.unlink()\n"
            "prompt = 'Use the Write tool to create the file at the absolute path ' + json.dumps(str(target)) + "
            "' with the exact contents \"" + marker + "\". After it is written, reply exactly DONE.'\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'claude', 'prompt': prompt,"
            " 'profile': {'permission_mode': 'plan', 'auth_mode': 'subscription'}}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "wait_idle(sup, rid, budget=180.0)\n"
            "from kaizen_components import db\n"
            "markers = [r[0] for r in db.fetch_all(\"SELECT marker FROM agent_events WHERE agent_run_id = ? AND event_kind = 'approval' ORDER BY sequence_no\", (rid,))]\n"
            "c4_rows = db.fetch_one('SELECT COUNT(*) FROM approval_requests WHERE session_id = ?', (sid,))[0]\n"
            "written = target.read_text(encoding='utf-8') if target.exists() else None\n"
            "if target.exists(): target.unlink()\n"
            "close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "state = sup._safe_reduce(rid)\n"
            "t8_after = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization' AND marker = 'close_ok'\", (rid,))[0]\n"
            "children_after = len(sup._children)\n"
            "sup.shutdown()\n"
            + self._CLEANUP +
            "out = {'start': start['status'], 'markers': markers, 'c4_rows': c4_rows, 'written': written,\n"
            "    'close': close['status'], 't8_after': t8_after, 'children_after': children_after,\n"
            "    'terminal': state['terminal'] if state else None, 'terminal_state': state.get('terminal_state') if state else None}\n"
        )
        out = self._drive_real(body)
        self.assertEqual(out["start"], "OK", out)
        # Allowed WITHOUT asking: no approval/open, no decline, no C4 row -- only the policy-allow
        # audit event (approval/resolved) the gate emits for every terminal ALLOW.
        self.assertNotIn("open", out["markers"], out)
        self.assertNotIn("declined", out["markers"], out)
        self.assertIn("resolved", out["markers"], out)
        self.assertEqual(out["c4_rows"], 0, out)
        self.assertEqual(out["written"], marker, out)
        self.assertEqual(out["close"], "OK", out)
        self.assertEqual(out["t8_after"], 1, out)
        self.assertEqual(out["children_after"], 0, out)
        self.assertTrue(out["terminal"], out)
        self.assertEqual(out["terminal_state"], "success", out)

    def test_live_plan_mode_denies_out_of_profile_write(self) -> None:
        # plan mode: an ABSOLUTE plane-root write OUTSIDE the designated roots (AI/work + AI/generation)
        # hits the plan write_workspace ceiling -> denied before execution. The turn completes with the
        # file proven absent and at least one declined policy event; explicit close writes one success T8.
        # Absolute target for the same live-observed reason as the approval leg (no model path discretion).
        marker = "kaizen-h2.4-must-not-exist"
        body = (
            "sup = Supervisor(); sup.boot()\n"
            "sup._driven_test_records = True\n"
            "from pathlib import Path as _P\n"
            "target = _P(str(sup.repo_root)) / 'kaizen-h2.4-live-plan-denied.txt'\n"
            "if target.exists(): target.unlink()\n"
            "prompt = 'Use the Write tool to create the file at the absolute path ' + json.dumps(str(target)) + "
            "' with the contents \"" + marker + "\". If the tool is denied, reply exactly DENIED.'\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'claude', 'prompt': prompt,"
            " 'profile': {'permission_mode': 'plan', 'auth_mode': 'subscription'}}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "wait_idle(sup, rid, budget=180.0)\n"
            "from kaizen_components import db\n"
            "pre = sup._safe_reduce(rid)\n"
            "declined = [r[0] for r in db.fetch_all(\"SELECT code FROM agent_events WHERE agent_run_id = ? AND event_kind = 'approval' AND marker = 'declined' ORDER BY sequence_no\", (rid,))]\n"
            "exists_before_close = target.exists()\n"
            "close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "state = sup._safe_reduce(rid)\n"
            "t8_after = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization' AND marker = 'close_ok'\", (rid,))[0]\n"
            "if target.exists(): target.unlink()\n"
            "absent_after = not target.exists()\n"
            "children_after = len(sup._children)\n"
            "sup.shutdown()\n"
            + self._CLEANUP +
            "out = {'start': start['status'], 'declined': declined, 'exists_before_close': exists_before_close,\n"
            "    'absent_after': absent_after, 'close': close['status'], 't8_after': t8_after,\n"
            "    'children_after': children_after,\n"
            "    'terminal': state['terminal'] if state else None, 'terminal_state': state.get('terminal_state') if state else None}\n"
        )
        out = self._drive_real(body)
        self.assertEqual(out["start"], "OK", out)
        self.assertFalse(out["exists_before_close"], out)  # denied before execution
        self.assertTrue(out["absent_after"], out)
        self.assertTrue(out["declined"], out)  # at least one declined/denied policy event
        self.assertTrue(any("MODE_CEILING:write_workspace" in code for code in out["declined"]), out)
        self.assertEqual(out["close"], "OK", out)
        self.assertEqual(out["t8_after"], 1, out)
        self.assertEqual(out["children_after"], 0, out)
        self.assertTrue(out["terminal"], out)
        self.assertEqual(out["terminal_state"], "success", out)


if __name__ == "__main__":
    unittest.main()
