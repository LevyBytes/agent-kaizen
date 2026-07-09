"""Agent-run orchestration ledger (T5-T8): pure reducer, CLI round-trips, atomic completion gates,
the T8 escape hatch, redaction, idempotent replay, the K1 child-leak invariant, and K7 purge."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from _harness import KAIZEN, REPO_ROOT, IsolatedDBTest

sys.path.insert(0, str(REPO_ROOT))
from kaizen_components.agent_runs import reduce, run_blocks_completion  # noqa: E402

BRIDGE = REPO_ROOT / "support_scripts" / "agent_event_bridge.py"


def ev(seq, kind, marker, corr=None, code=None, created=None):
    return {
        "id": f"ae_{seq}", "created_at": created or f"2026-07-08T00:00:{seq:02d}+00:00",
        "sequence_no": seq, "event_kind": kind, "marker": marker, "correlation_id": corr, "code": code,
    }


class ReducerUnitTest(unittest.TestCase):
    """Pure, in-process reduce() -- no DB. Fast, exhaustive on ordering/idempotency/leak edges."""

    def test_empty_run_not_terminal_not_blocking(self):
        state = reduce([])
        self.assertFalse(state["terminal"])
        self.assertEqual(state["open_children"], [])
        self.assertFalse(run_blocks_completion(state))

    def test_unresolved_child_blocks(self):
        state = reduce([ev(1, "subagent", "open", "c1")])
        self.assertEqual(state["open_children"], ["c1"])
        self.assertTrue(run_blocks_completion(state))

    def test_resolved_child_clears(self):
        state = reduce([ev(1, "subagent", "open", "c1"), ev(2, "subagent", "close_ok", "c1")])
        self.assertEqual(state["open_children"], [])
        self.assertFalse(run_blocks_completion(state))

    def test_unresolved_approval_blocks(self):
        state = reduce([ev(1, "approval", "open", "a1")])
        self.assertEqual(state["unresolved_approvals"], ["a1"])
        self.assertTrue(run_blocks_completion(state))

    def test_resolved_approval_clears(self):
        state = reduce([ev(1, "approval", "open", "a1"), ev(2, "approval", "resolved", "a1")])
        self.assertEqual(state["unresolved_approvals"], [])

    def test_out_of_order_close_before_open_is_closed(self):
        # Set-of-markers fold is commutative: a close seen for a span closes it regardless of order.
        forward = reduce([ev(1, "subagent", "open", "c1"), ev(2, "subagent", "close_ok", "c1")])
        reversed_ = reduce([ev(2, "subagent", "close_ok", "c1"), ev(1, "subagent", "open", "c1")])
        self.assertEqual(forward["open_children"], reversed_["open_children"])
        self.assertEqual(reversed_["open_children"], [])

    def test_duplicate_open_is_idempotent(self):
        one = reduce([ev(1, "subagent", "open", "c1")])
        dup = reduce([ev(1, "subagent", "open", "c1"), ev(2, "subagent", "open", "c1")])
        self.assertEqual(one["open_children"], dup["open_children"])

    def test_context_is_pure_annotation(self):
        state = reduce([ev(1, "turn", "open", "t1"), ev(2, "context", "point")])
        self.assertFalse(state["terminal"])
        self.assertEqual(state["open_children"], [])
        self.assertFalse(run_blocks_completion(state))  # open turns/context never gate

    def test_failure_category_is_last_failing_point(self):
        state = reduce([ev(1, "transport", "point", code="timeout"), ev(2, "rate_limit", "point", code="429")])
        self.assertEqual(state["failure_category"], "rate_limit")

    def test_terminal_clean_run(self):
        state = reduce([
            ev(1, "subagent", "open", "c1"), ev(2, "subagent", "close_ok", "c1"), ev(3, "finalization", "close_ok"),
        ])
        self.assertTrue(state["terminal"])
        self.assertEqual(state["terminal_state"], "success")
        self.assertFalse(state["child_leak"])

    def test_child_leak_terminal_success_with_open_child(self):
        state = reduce([ev(1, "subagent", "open", "c1"), ev(2, "finalization", "close_ok")])
        self.assertTrue(state["child_leak"])

    def test_failure_finalize_with_open_child_is_not_a_leak(self):
        state = reduce([ev(1, "subagent", "open", "c1"), ev(2, "finalization", "close_fail")])
        self.assertEqual(state["terminal_state"], "failure")
        self.assertFalse(state["child_leak"])
        self.assertFalse(run_blocks_completion(state))  # terminal runs never block


class AgentRunCliTest(IsolatedDBTest):
    def _start(self, **envelope):
        payload = {"agent_type": "claude", "surface": "cli", **envelope}
        rc, p = self.kz("T5", "--summary", "Run.", "--payload-json", json.dumps(payload))
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["id"].startswith("ar_"), p)
        return p["id"]

    def _event(self, run_id, **event):
        return self.kz("T6", "--agent-run-id", run_id, "--payload-json", json.dumps(event))

    def test_t5_start_via_convenience_flags(self):
        rc, p = self.kz("T5", "--agent-type", "codex", "--surface", "app-server", "--summary", "Run.")
        self.assertEqual(rc, 0, p)

    def test_t5_missing_required_denied(self):
        rc, p = self.kz("T5", "--summary", "Run.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_SCHEMA_REQUIRED", p)

    def test_t6_matrix_rejects_bad_pair(self):
        run_id = self._start()
        rc, p = self._event(run_id, event_kind="transport", marker="open", summary="bad")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_EVENT_KIND_MARKER", p)

    def test_t6_span_requires_correlation_id(self):
        run_id = self._start()
        rc, p = self._event(run_id, event_kind="subagent", marker="open", summary="child")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_CORRELATION_ID_REQUIRED", p)

    def test_t6_unknown_run_denied(self):
        rc, p = self._event("ar_nope", event_kind="context", marker="point", summary="x")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_AGENT_RUN_NOT_FOUND", p)

    def test_t6_source_event_id_idempotent(self):
        run_id = self._start()
        rc, p1 = self._event(run_id, event_kind="subagent", marker="open", correlation_id="c1",
                             source_event_id="v1", summary="child")
        self.assertEqual(rc, 0, p1)
        rc, p2 = self._event(run_id, event_kind="subagent", marker="open", correlation_id="c1",
                             source_event_id="v1", summary="child again")
        self.assertEqual(rc, 0, p2)
        self.assertTrue(p2.get("deduplicated"), p2)
        self.assertEqual(p1["id"], p2["id"], (p1, p2))

    def test_t6_redaction_denies_secret(self):
        run_id = self._start()
        rc, p = self._event(run_id, event_kind="context", marker="point",
                            summary="token sk-ant-abcdef0123456789XYZ leaked")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_TRACE_REDACTION", p)

    def test_t5_redaction_denies_personal_path(self):
        rc, p = self.kz("T5", "--agent-type", "claude", "--surface", "cli", "--summary", "Run.",
                        "--payload-json", json.dumps({"agent_type": "claude", "surface": "cli",
                                                      "worktree_path": r"C:\Users\bob\wt"}))
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_TRACE_REDACTION", p)

    def test_t7_inspect_recomputes_state(self):
        run_id = self._start(host="wsl", sandbox_mode="workspace-write")
        self._event(run_id, event_kind="subagent", marker="open", correlation_id="c1", summary="child")
        rc, p = self.kz("T7", "--agent-run-id", run_id)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["state"]["open_children"], ["c1"], p)
        self.assertTrue(p["blocks_completion"], p)
        self.assertEqual(p["envelope"]["host"], "wsl", p)

    def test_t8_success_denied_while_child_open(self):
        run_id = self._start()
        self._event(run_id, event_kind="subagent", marker="open", correlation_id="c1", summary="child")
        rc, p = self.kz("T8", "--agent-run-id", run_id, "--conclusion", "success", "--summary", "done")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_AGENT_RUN_NOT_TERMINAL", p)

    def test_t8_success_after_child_closed(self):
        run_id = self._start()
        self._event(run_id, event_kind="subagent", marker="open", correlation_id="c1", summary="child")
        self._event(run_id, event_kind="subagent", marker="close_ok", correlation_id="c1", summary="done")
        rc, p = self.kz("T8", "--agent-run-id", run_id, "--conclusion", "success", "--summary", "done")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["forced_close"], 0, p)

    def test_t8_failure_force_terminates_and_records_leak(self):
        run_id = self._start()
        self._event(run_id, event_kind="subagent", marker="open", correlation_id="c1", summary="child")
        rc, p = self.kz("T8", "--agent-run-id", run_id, "--conclusion", "failed", "--summary", "aborted")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["forced_close"], 1, p)
        self.assertTrue(p["child_leak"], p)

    def test_t8_double_finalize_denied(self):
        run_id = self._start()
        self.kz("T8", "--agent-run-id", run_id, "--conclusion", "success", "--summary", "done")
        rc, p = self.kz("T8", "--agent-run-id", run_id, "--conclusion", "success", "--summary", "again")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_AGENT_RUN_ALREADY_FINALIZED", p)


class CompletionGateTest(IsolatedDBTest):
    def _run_with_open_child(self, task_id):
        rc, p = self.kz("T5", "--task-id", task_id, "--agent-type", "claude", "--surface", "cli", "--summary", "Run.")
        self.assertEqual(rc, 0, p)
        run_id = p["id"]
        rc, p = self.kz("T6", "--agent-run-id", run_id, "--payload-json",
                        json.dumps({"event_kind": "subagent", "marker": "open", "correlation_id": "c1", "summary": "child"}))
        self.assertEqual(rc, 0, p)
        return run_id

    def test_q2_success_denied_while_child_live(self):
        self._run_with_open_child("task-gate-1")
        rc, p = self.kz("Q2", "--task-id", "task-gate-1", "--conclusion", "VERIFIED_ACCEPTABLE",
                        "--summary", "Looks done.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_TASK_HAS_LIVE_CHILDREN", p)
        self.assertEqual(p.get("live_children"), 1, p)

    def test_q2_failure_conclusion_allowed_while_child_live(self):
        self._run_with_open_child("task-gate-2")
        rc, p = self.kz("Q2", "--task-id", "task-gate-2", "--conclusion", "VERIFICATION_FAILED",
                        "--summary", "Blocked; child still running.")
        self.assertEqual(rc, 0, p)

    def test_q2_no_linked_run_is_fail_open(self):
        # Regression guard for the ~all pre-T5 workflows: a task with no agent_run is never gated.
        rc, p = self.kz("Q2", "--task-id", "task-unlinked", "--conclusion", "VERIFIED_ACCEPTABLE",
                        "--summary", "Nothing to wait on.")
        self.assertEqual(rc, 0, p)

    def test_q2_no_task_id_is_fail_open(self):
        rc, p = self.kz("Q2", "--conclusion", "VERIFIED_ACCEPTABLE", "--summary", "No task link.")
        self.assertEqual(rc, 0, p)

    def test_q2_success_allowed_after_escape_hatch(self):
        run_id = self._run_with_open_child("task-gate-3")
        # Only sanctioned exit for a leaked child: finalize the run as failed.
        rc, p = self.kz("T8", "--agent-run-id", run_id, "--conclusion", "failed", "--summary", "aborted")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("Q2", "--task-id", "task-gate-3", "--conclusion", "VERIFIED_ACCEPTABLE",
                        "--summary", "Now clear.")
        self.assertEqual(rc, 0, p)

    def test_w2_terminal_denied_while_child_live(self):
        rc, p = self.kz("W1", "--title", "Parent task", "--summary", "Has a live child.")
        self.assertEqual(rc, 0, p)
        task_id = p["id"]
        rc, p = self.kz("T5", "--task-id", task_id, "--agent-type", "claude", "--surface", "cli", "--summary", "Run.")
        run_id = p["id"]
        self.kz("T6", "--agent-run-id", run_id, "--payload-json",
                json.dumps({"event_kind": "subagent", "marker": "open", "correlation_id": "c1", "summary": "child"}))
        rc, p = self.kz("W2", "--id", task_id, "--status", "done", "--summary", "Closing.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_TASK_HAS_LIVE_CHILDREN", p)

    def test_w2_non_terminal_status_not_gated(self):
        rc, p = self.kz("W1", "--title", "Parent task", "--summary", "Has a live child.")
        task_id = p["id"]
        rc, p = self.kz("T5", "--task-id", task_id, "--agent-type", "claude", "--surface", "cli", "--summary", "Run.")
        run_id = p["id"]
        self.kz("T6", "--agent-run-id", run_id, "--payload-json",
                json.dumps({"event_kind": "subagent", "marker": "open", "correlation_id": "c1", "summary": "child"}))
        rc, p = self.kz("W2", "--id", task_id, "--status", "active", "--summary", "Still working.")
        self.assertEqual(rc, 0, p)


class IntegrityPurgeReportTest(IsolatedDBTest):
    def test_k1_integrity_detects_child_leak(self):
        # Reachable via CLI: finalize success (allowed, no children), then a late/replayed child-open
        # arrives -> terminal-success run with a live child == the invariant violation.
        rc, p = self.kz("T5", "--agent-type", "claude", "--surface", "cli", "--summary", "Run.")
        run_id = p["id"]
        self.kz("T8", "--agent-run-id", run_id, "--conclusion", "success", "--summary", "done")
        self.kz("T6", "--agent-run-id", run_id, "--payload-json",
                json.dumps({"event_kind": "subagent", "marker": "open", "correlation_id": "late", "summary": "late child"}))
        rc, p = self.kz("K1", "--integrity")
        self.assertEqual(rc, 0, p)
        scan = p["integrity"]
        self.assertFalse(scan["ok"], scan)
        self.assertEqual(scan["total_invariant_violations"], 1, scan)

    def test_k7_purge_removes_test_runs_and_events(self):
        rc, p = self.kz("T5", "--agent-type", "claude", "--surface", "cli", "--summary", "Run.", "--test")
        run_id = p["id"]
        self.kz("T6", "--agent-run-id", run_id, "--test", "--payload-json",
                json.dumps({"event_kind": "context", "marker": "point", "summary": "note"}))
        rc, p = self.kz("K7")
        self.assertEqual(rc, 0, p)
        self.assertGreaterEqual(p["purged"].get("agent_runs", 0), 1, p)
        rc, p = self.kz("T7", "--agent-run-id", run_id)
        self.assertEqual(rc, 1, p)  # purged -> not found

    def test_r0_surfaces_active_runs_and_waiting_approvals(self):
        rc, p = self.kz("T5", "--agent-type", "claude", "--surface", "cli", "--summary", "Run.")
        run_id = p["id"]
        self.kz("T6", "--agent-run-id", run_id, "--payload-json",
                json.dumps({"event_kind": "approval", "marker": "open", "correlation_id": "a1", "summary": "need approval"}))
        rc, p = self.kz("R0")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["counts"]["agent_runs_active"], 1, p)
        self.assertEqual(len(p["waiting_approvals"]), 1, p)
        self.assertEqual(p["counts"]["parent_completed_with_live_children"], 0, p)


class BridgeReplayTest(IsolatedDBTest):
    def _bridge(self, jsonl_path):
        env = dict(os.environ)
        env["KAIZEN_REPO_ROOT"] = str(self.root)
        proc = subprocess.run(
            [sys.executable, str(BRIDGE), "ingest", str(jsonl_path), "--test", "--json"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
        )
        return proc.returncode, json.loads(proc.stdout)

    def test_jsonl_replay_is_idempotent_and_reduces(self):
        lines = [
            {"op": "run_start", "run_key": "vrun", "summary": "vendor run",
             "envelope": {"agent_type": "codex", "surface": "app-server", "host": "local"}},
            {"op": "event", "run_key": "vrun",
             "event": {"event_kind": "subagent", "marker": "open", "correlation_id": "c1",
                       "source_event_id": "v1", "summary": "child"}},
            {"op": "event", "run_key": "vrun",  # duplicate source_event_id -> no-op
             "event": {"event_kind": "subagent", "marker": "open", "correlation_id": "c1",
                       "source_event_id": "v1", "summary": "child replay"}},
            {"op": "event", "run_key": "vrun",
             "event": {"event_kind": "approval", "marker": "open", "correlation_id": "a1",
                       "source_event_id": "v2", "summary": "approval"}},
        ]
        path = self.root / "events.jsonl"
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        rc, out = self._bridge(path)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["runs_started"], 1, out)
        self.assertEqual(out["deduplicated"], 1, out)
        self.assertEqual(out["errors"], [], out)
        run_id = out["started"][0]["agent_run_id"]
        rc, p = self.kz("T7", "--agent-run-id", run_id)
        self.assertEqual(rc, 0, p)
        self.assertEqual(sorted(p["state"]["open_children"]), ["c1"], p)
        self.assertEqual(sorted(p["state"]["unresolved_approvals"]), ["a1"], p)


if __name__ == "__main__":
    unittest.main()
