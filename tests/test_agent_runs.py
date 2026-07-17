"""Agent-run orchestration ledger (T5-T8): pure reducer, CLI round-trips, atomic completion gates,
the T8 escape hatch, redaction, idempotent replay, the K1 child-leak invariant, and K7 purge."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from _harness import REPO_ROOT, IsolatedDBTest

sys.path.insert(0, str(REPO_ROOT))
from kaizen_components.agent_runs import reduce, run_blocks_completion  # noqa: E402

def ev(seq, kind, marker, corr=None, code=None, created=None):
    """Synthetic agent_event dict factory: fields + defaults (created_at derived from seq; corr/code optional)."""
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
        # reduce() sorts first; this guards the stable sort+fold result for reversed input.
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

    def test_t6_without_source_event_id_uses_non_deduplicating_insert(self):
        run_id = self._start()
        rc, first = self._event(run_id, event_kind="context", marker="point", summary="same")
        self.assertEqual(rc, 0, first)
        rc, second = self._event(run_id, event_kind="context", marker="point", summary="same")
        self.assertEqual(rc, 0, second)
        self.assertFalse(first.get("deduplicated"), first)
        self.assertFalse(second.get("deduplicated"), second)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["sequence_no"], first["sequence_no"] + 1)

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
        first_rc, first = self.kz("T8", "--agent-run-id", run_id, "--conclusion", "success", "--summary", "done")
        self.assertEqual(first_rc, 0, first)
        rc, p = self.kz("T8", "--agent-run-id", run_id, "--conclusion", "success", "--summary", "again")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_AGENT_RUN_ALREADY_FINALIZED", p)


class ChatMessageMetadataTest(IsolatedDBTest):
    """Durable chat metadata is reference-only, bounded, exact, and denied before T6 insertion."""

    @staticmethod
    def _image(digest: str = "a" * 64) -> dict:
        return {
            "id": "img-1",
            "kind": "image",
            "artifact_ref": f"sha256:{digest}",
            "sha256": digest,
            "bytes": 1234,
            "media_type": "image/png",
            "name": "captures/ui:variant/screenshot.png",
        }

    @staticmethod
    def _file_ref(index: int = 1, *, size: int = 64) -> dict:
        return {
            "id": f"file-{index}",
            "kind": "file",
            "source_path": f"src/file-{index}.py",
            "sha256": "b" * 64,
            "bytes": size,
            "encoding": "utf-8",
        }

    @staticmethod
    def _selection_ref() -> dict:
        digest = "c" * 64
        return {
            "id": "selection-1",
            "kind": "selection",
            "source_path": "src/selected.py",
            "range": {
                "start": {"line": 1, "character": 2},
                "end": {"line": 3, "character": 4},
            },
            "snapshot_ref": f"sha256:{digest}",
            "sha256": digest,
            "bytes": 42,
            "encoding": "utf-8",
        }

    def _start_run(self) -> str:
        rc, payload = self.kz("T5", "--agent-type", "claude", "--surface", "cli", "--summary", "Run.")
        self.assertEqual(rc, 0, payload)
        return payload["id"]

    def _chat(self, run_id: str, body: dict) -> tuple[int, dict]:
        return self.kz(
            "T6",
            "--agent-run-id",
            run_id,
            "--payload-json",
            json.dumps({
                "event_kind": "chat_message",
                "marker": "point",
                "summary": "chat message",
                "body": json.dumps(body),
            }),
        )

    def _event_count(self, run_id: str) -> int:
        rc, payload = self.kz("T7", "--agent-run-id", run_id)
        self.assertEqual(rc, 0, payload)
        return payload["state"]["event_count"]

    def _persisted_chat_bodies(self, run_id: str) -> list[dict]:
        """Read persisted chat-message bodies through a child bound to this test's isolated DB."""
        script = (
            "import json\n"
            "from kaizen_components import db\n"
            f"rows=db.fetch_all(\"SELECT body FROM agent_events WHERE agent_run_id = ? "
            f"AND event_kind = 'chat_message' ORDER BY sequence_no\", ({run_id!r},))\n"
            "print(json.dumps([json.loads(row[0]) for row in rows]))\n"
        )
        env = dict(os.environ)
        env["KAIZEN_REPO_ROOT"] = str(self.root)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        bodies = json.loads(proc.stdout)
        self.assertIsInstance(bodies, list)
        return bodies

    def test_legacy_and_valid_reference_metadata_persist_exactly(self):
        run_id = self._start_run()
        legacy = {"role": "user", "text": "legacy", "source": "driven"}
        selection = self._selection_ref()
        selection["range"] = {
            "start": {"line": 2**40, "character": 0},
            "end": {"line": 2**40, "character": 1},
        }
        metadata = {
            "role": "assistant",
            "text": "with references",
            "source": "driven",
            "turn_id": "turn-1",
            "attachments": [self._image()],
            "context_refs": [self._file_ref(), selection],
        }
        nullable = {
            "role": "user",
            "text": "nullable metadata",
            "source": "observed",
            "attachments": None,
            "context_refs": None,
        }
        for body in (legacy, metadata, nullable):
            rc, payload = self._chat(run_id, body)
            self.assertEqual(rc, 0, payload)

        self.assertEqual(self._event_count(run_id), 3)
        self.assertEqual(self._persisted_chat_bodies(run_id), [legacy, metadata, nullable])

    def test_shared_protocol_denial_is_stable_and_does_not_echo_metadata(self):
        run_id = self._start_run()
        secret_name = "owner@private-domain.test"
        image = self._image()
        image["name"] = secret_name
        rc, payload = self._chat(
            run_id,
            {"role": "user", "text": "safe", "source": "driven", "attachments": [image]},
        )
        self.assertEqual(rc, 2, payload)
        self.assertEqual(payload.get("code"), "DENIED_CHAT_MESSAGE_BODY", payload)
        self.assertNotIn(secret_name, json.dumps(payload, sort_keys=True))
        self.assertEqual(self._event_count(run_id), 0)

    def test_unknown_raw_and_cache_fields_are_denied_with_zero_events(self):
        run_id = self._start_run()
        base = {"role": "user", "text": "safe", "source": "driven"}
        image_data = self._image()
        image_data["data_url"] = "data:image/png;base64,AAAA"
        image_cache = self._image()
        image_cache["cache_path"] = "AI/work/orchestration/ui-cache/images/object"
        file_content = self._file_ref()
        file_content["content"] = "raw source"
        selection_diff = self._selection_ref()
        selection_diff["unified_diff"] = "--- a\n+++ b"
        cache_ref = self._file_ref()
        cache_ref["source_path"] = "AI/work/orchestration/ui-cache/context/object"
        cases = [
            {**base, "content": "raw body"},
            {**base, "attachments": [image_data]},
            {**base, "attachments": [image_cache]},
            {**base, "context_refs": [file_content]},
            {**base, "context_refs": [selection_diff]},
            {**base, "context_refs": [cache_ref]},
        ]
        for body in cases:
            with self.subTest(body_keys=sorted(body)):
                rc, payload = self._chat(run_id, body)
                self.assertEqual(rc, 2, payload)
                self.assertEqual(payload.get("code"), "DENIED_CHAT_MESSAGE_BODY", payload)
                self.assertEqual(self._event_count(run_id), 0)

    def test_malformed_count_size_hash_and_range_shapes_create_zero_events(self):
        run_id = self._start_run()
        base = {"role": "assistant", "text": "safe", "source": "driven"}
        five_images = []
        for index in range(5):
            image = self._image()
            image["id"] = f"img-{index}"
            five_images.append(image)
        oversized_context = [self._file_ref(index, size=256 * 1024) for index in range(1, 6)]
        bad_range = self._selection_ref()
        bad_range["range"] = {
            "start": {"line": 5, "character": 0},
            "end": {"line": 4, "character": 0},
        }
        uppercase_hash = self._image("A" * 64)
        cases = [
            {**base, "attachments": {"not": "an array"}},
            {**base, "attachments": five_images},
            {**base, "attachments": [{**self._image(), "bytes": 0}]},
            {**base, "attachments": [uppercase_hash]},
            {**base, "context_refs": [self._file_ref(index) for index in range(1, 10)]},
            {**base, "context_refs": oversized_context},
            {**base, "context_refs": [bad_range]},
            {**base, "context_refs": [{**self._file_ref(), "source_path": "../outside.py"}]},
            {**base, "context_refs": [{**self._file_ref(), "bytes": True}]},
        ]
        for body in cases:
            with self.subTest(body_keys=sorted(body)):
                rc, payload = self._chat(run_id, body)
                self.assertEqual(rc, 2, payload)
                self.assertEqual(payload.get("code"), "DENIED_CHAT_MESSAGE_BODY", payload)
                self.assertEqual(self._event_count(run_id), 0)


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
        self.assertEqual(rc, 0, p)
        run_id = p["id"]
        rc, p = self.kz("T6", "--agent-run-id", run_id, "--payload-json",
                        json.dumps({"event_kind": "subagent", "marker": "open", "correlation_id": "c1", "summary": "child"}))
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("W2", "--id", task_id, "--status", "done", "--summary", "Closing.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_TASK_HAS_LIVE_CHILDREN", p)

    def test_w2_non_terminal_status_not_gated(self):
        rc, p = self.kz("W1", "--title", "Parent task", "--summary", "Has a live child.")
        task_id = p["id"]
        rc, p = self.kz("T5", "--task-id", task_id, "--agent-type", "claude", "--surface", "cli", "--summary", "Run.")
        self.assertEqual(rc, 0, p)
        run_id = p["id"]
        rc, p = self.kz("T6", "--agent-run-id", run_id, "--payload-json",
                        json.dumps({"event_kind": "subagent", "marker": "open", "correlation_id": "c1", "summary": "child"}))
        self.assertEqual(rc, 0, p)
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


if __name__ == "__main__":
    unittest.main()
