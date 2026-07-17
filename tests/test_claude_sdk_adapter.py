"""State that the suite covers Claude SDK adapter lifecycle, protocol fail-closed behavior, safe metadata, environment sanitization, and capability probes."""
from __future__ import annotations

import hashlib
import os
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kaizen_components.orchestration.claude_worker_protocol import (
    CAPABILITY_PROBE_FEATURES,
    WorkerProtocolError,
)
from kaizen_components.orchestration import policy
from kaizen_components.orchestration.apply_evidence import normalize_apply_evidence
from kaizen_components.orchestration.adapters import create_adapter
from kaizen_components.orchestration.adapters import claude_cli, claude_sdk
from kaizen_components.orchestration.proposal_executor import WorkspaceProposalExecutor
from kaizen_components.orchestration.adapters.claude_sdk import (
    ClaudeSdkAdapter,
    ClaudeSdkAdapterError,
    _sanitized_worker_env,
    probe_capability,
)


FAKE = Path(__file__).with_name("fake_claude_agent_worker.py")
AI_WORK = Path(__file__).resolve().parents[1] / "AI" / "work"


class FakeGateway:
    """State that this gateway double records broker/guard binding and simulates governed-tool cancellation/close proof."""
    def __init__(self) -> None:
        self.broker = None
        self.guard = None
        self.termination_proven = True
        self.cancel_calls = 0
        self.close_calls = 0

    def set_approval_broker(self, callback):
        self.broker = callback

    def set_mutation_guard(self, callback):
        self.guard = callback

    def invoke(self, name, args, *, correlation_id):
        return {"status": "OK", "content": f"{name}:{correlation_id}", "fatal": False}

    def cancel_active_processes(self, _timeout):
        self.cancel_calls += 1
        return {"termination_proven": self.termination_proven, "active_count": 0}

    def close(self):
        self.close_calls += 1
        return True


class ClaudeSdkAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=AI_WORK)
        self.root = Path(self.tmp.name).resolve()
        self.events: list[dict] = []
        self.gateway = FakeGateway()
        self.adapter = ClaudeSdkAdapter(
            workspace_root=self.root,
            worker_command=[sys.executable, str(FAKE)],
            recorder=self.events.append,
            gateway_factory=lambda **_kwargs: self.gateway,
        )
        setattr(self.adapter, "_kaizen_vendor_runtime", str(self.root / "runtime"))

    def tearDown(self) -> None:
        self.adapter.kill()
        self.tmp.cleanup()

    def open(self) -> dict:
        return dict(self.adapter.open({
            "model": "claude-test-model",
            "reasoning_effort": "high",
            "permission_mode": "plan",
            "auth_mode": "subscription",
            "max_turns": 8,
        }, None))

    def use_fault_worker(self, fault: str, *, logger=None) -> None:
        self.adapter.kill()
        self.adapter = ClaudeSdkAdapter(
            workspace_root=self.root,
            worker_command=[sys.executable, str(FAKE)],
            recorder=self.events.append,
            logger=logger,
            gateway_factory=lambda **_kwargs: self.gateway,
            env={"KAIZEN_FAKE_CLAUDE_FAULT": fault},
        )
        setattr(self.adapter, "_kaizen_vendor_runtime", str(self.root / ("runtime-" + fault)))

    def test_legacy_engine_and_import_alias_construct_the_sdk_adapter(self) -> None:
        self.assertIs(claude_cli.ClaudeCliAdapter, ClaudeSdkAdapter)
        alias_adapter = create_adapter(
            "claude_cli", workspace_root=self.root,
            worker_command=[sys.executable, str(FAKE)],
        )
        self.addCleanup(alias_adapter.kill)
        self.assertIsInstance(alias_adapter, ClaudeSdkAdapter)
        self.assertEqual(alias_adapter.engine_name, "claude")

    def test_open_stream_turn_and_close(self) -> None:
        opened = self.open()
        self.assertEqual(opened["profile"]["model"], "claude-test-model")
        self.assertEqual(opened["models"][0]["reasoning_efforts"], ["low", "high"])
        self.adapter.bind_session("as-test")
        deltas: list[str] = []
        self.adapter.on_delta(lambda event: deltas.append(event["text"]))
        result = self.adapter.run_turn("hello")
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.final_text, "FAKE_CLAUDE_OK")
        self.assertEqual("".join(deltas), "FAKE_CLAUDE_OK")
        self.assertEqual([event["marker"] for event in self.events if event["event_kind"] == "turn"],
                         ["open", "close_ok"])
        closed = self.adapter.close()
        self.assertEqual(closed, {"status": "OK", "closed": True, "termination_proven": True})
        self.assertEqual(self.gateway.close_calls, 1)

    def test_initial_initialize_outer_timeout_exceeds_worker_bound(self) -> None:
        self.assertEqual(claude_sdk._INITIALIZE_TIMEOUT_SECONDS, 135.0)
        self.assertGreater(claude_sdk._INITIALIZE_TIMEOUT_SECONDS, 120.0)
        with mock.patch.object(self.adapter, "_request", wraps=self.adapter._request) as request:
            self.open()
        initialize = next(call for call in request.call_args_list if call.args[0] == "initialize")
        self.assertEqual(initialize.kwargs["timeout"], claude_sdk._INITIALIZE_TIMEOUT_SECONDS)

    def test_request_timeout_retains_safe_operation_stage(self) -> None:
        child = mock.Mock()
        child.poll.return_value = None
        child.process.stdin = mock.Mock()
        self.adapter._child = child
        try:
            with self.assertRaises(ClaudeSdkAdapterError) as raised:
                self.adapter._request(
                    "initialize", session_id=self.adapter._worker_session_id, body={}, timeout=0.001,
                )
        finally:
            self.adapter._child = None
        self.assertEqual(raised.exception.code, "DENIED_WORKER_PROTOCOL")
        self.assertEqual(raised.exception.field, "initialize.timeout")

    def test_fatal_event_preserves_safe_field_through_pending_request_fanout(self) -> None:
        inbox: queue.Queue[dict] = queue.Queue(maxsize=1)
        self.adapter._pending["rq-pending"] = inbox
        self.adapter._handle_event({
            "event": "fatal", "session_id": self.adapter._worker_session_id, "seq": 1,
            "body": {"code": "DENIED_WORKER_PROTOCOL", "field": "account"},
        })
        self.assertEqual(inbox.get_nowait()["error"], {
            "code": "DENIED_WORKER_PROTOCOL", "field": "account",
        })
        unsafe: queue.Queue[dict] = queue.Queue(maxsize=1)
        self.adapter._pending = {"rq-unsafe": unsafe}
        self.adapter._worker_failed(
            "DENIED_WORKER_PROTOCOL", "account\nPRIVATE_FIELD_MUST_NOT_ESCAPE",
        )
        self.assertEqual(unsafe.get_nowait()["error"], {"code": "DENIED_WORKER_PROTOCOL"})

    def test_host_protocol_wrappers_preserve_safe_fields(self) -> None:
        with mock.patch.object(
            claude_sdk, "sanitize_model_catalog", side_effect=WorkerProtocolError(field="models[0]"),
        ), self.assertRaises(ClaudeSdkAdapterError) as opened:
            self.open()
        self.assertEqual(opened.exception.field, "models[0]")

        self.adapter = ClaudeSdkAdapter(
            workspace_root=self.root,
            worker_command=[sys.executable, str(FAKE)],
            recorder=self.events.append,
            gateway_factory=lambda **_kwargs: self.gateway,
        )
        setattr(self.adapter, "_kaizen_vendor_runtime", str(self.root / "runtime-refresh-field"))
        self.open()
        with mock.patch.object(
            claude_sdk, "sanitize_model_catalog", side_effect=WorkerProtocolError(field="models[1]"),
        ), self.assertRaises(ClaudeSdkAdapterError) as refreshed:
            self.adapter.refresh_models()
        self.assertEqual(refreshed.exception.field, "models[1]")

    def test_model_without_effort_opens_without_null_or_invented_effort(self) -> None:
        self.adapter.kill()
        self.adapter = ClaudeSdkAdapter(
            workspace_root=self.root,
            worker_command=[sys.executable, str(FAKE)],
            recorder=self.events.append,
            gateway_factory=lambda **_kwargs: self.gateway,
            env={"KAIZEN_FAKE_CLAUDE_NO_EFFORT": "1"},
        )
        setattr(self.adapter, "_kaizen_vendor_runtime", str(self.root / "runtime-no-effort"))
        opened = self.adapter.open({
            "model": "claude-test-model",
            "permission_mode": "plan",
            "auth_mode": "subscription",
            "max_turns": 1,
        }, None)
        self.assertEqual(opened["models"][0]["reasoning_efforts"], [])
        self.assertEqual(opened["profile"], {
            "model": "claude-test-model",
            "max_turns": 1,
            "auth_mode": "subscription",
            "permission_mode": "plan",
        })

    def test_malformed_and_oversize_stdout_frames_fail_closed(self) -> None:
        for fault, code in (
            ("malformed-stdout", "DENIED_WORKER_PROTOCOL"),
            ("oversize-stdout", "DENIED_WORKER_OVERSIZE"),
            ("multibyte-oversize-stdout", "DENIED_WORKER_OVERSIZE"),
        ):
            with self.subTest(fault=fault):
                self.use_fault_worker(fault)
                with self.assertRaises(ClaudeSdkAdapterError) as raised:
                    self.open()
                self.assertEqual(raised.exception.code, code)
                self.assertIsNone(self.adapter._child)

    def test_worker_death_during_turn_retains_worker_died_outcome(self) -> None:
        self.use_fault_worker("worker-death-on-turn")
        self.open()
        self.adapter.bind_session("as-worker-death")
        result = self.adapter.run_turn("die deterministically")
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.error_code, "WORKER_DIED")
        self.assertTrue(result.fatal)

    def test_hanging_interrupt_uses_bounded_wait_and_proven_tree_cleanup(self) -> None:
        self.assertEqual(claude_sdk._INTERRUPT_TIMEOUT_SECONDS, 2.0)
        self.use_fault_worker("hang-interrupt")
        self.open()
        self.adapter._active_turn = "ct-hang-interrupt"
        started = time.monotonic()
        with mock.patch.object(claude_sdk, "_INTERRUPT_TIMEOUT_SECONDS", 0.05), \
                mock.patch.object(claude_sdk, "_CLOSE_TIMEOUT_SECONDS", 0.25):
            result = self.adapter.interrupt()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(result, {
            "status": "ERROR", "code": "WORKER_DIED", "termination_proven": True,
        })
        self.assertIsNone(self.adapter._child)

    def test_hanging_close_uses_bounded_wait_and_proven_tree_cleanup(self) -> None:
        self.assertEqual(claude_sdk._CLOSE_TIMEOUT_SECONDS, 5.0)
        self.use_fault_worker("hang-close")
        self.open()
        started = time.monotonic()
        with mock.patch.object(claude_sdk, "_CLOSE_TIMEOUT_SECONDS", 0.05):
            result = self.adapter.close()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(result, {"status": "OK", "closed": True, "termination_proven": True})
        self.assertIsNone(self.adapter._child)

    def test_interrupt_timeout_attempts_worker_kill_even_when_tool_cleanup_is_unproven(self) -> None:
        self.open()
        self.adapter._active_turn = "ct-unproven-interrupt"
        self.gateway.termination_proven = False
        with mock.patch.object(
            self.adapter, "_request", side_effect=ClaudeSdkAdapterError(
                "DENIED_WORKER_PROTOCOL", required_action="restart the unresponsive test worker",
            ),
        ), mock.patch.object(self.adapter, "_kill_tree", return_value=True) as kill_tree:
            result = self.adapter.interrupt()
        kill_tree.assert_called_once_with()
        self.assertEqual(result, {
            "status": "ERROR",
            "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
            "termination_proven": False,
        })

    def test_unproven_close_kill_reports_recovery_required(self) -> None:
        self.use_fault_worker("hang-close")
        self.open()
        with mock.patch.object(claude_sdk, "_CLOSE_TIMEOUT_SECONDS", 0.01), \
                mock.patch.object(self.adapter, "_kill_tree", return_value=False):
            result = self.adapter.close()
        self.assertEqual(result, {
            "status": "ERROR",
            "closed": True,
            "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
            "termination_proven": False,
        })

    def test_stderr_is_capped_and_redacted_before_logging(self) -> None:
        messages: list[str] = []
        self.use_fault_worker("stderr-flood", logger=messages.append)
        self.open()
        self.adapter.close()
        self.assertEqual(self.adapter._stderr_bytes, 64 * 1024 + 1)
        self.assertEqual(messages, ["Claude worker stderr suppressed (65536 bytes)"])
        self.assertNotIn("PRIVATE_STDERR_MUST_NOT_ESCAPE", "".join(messages))

    def test_profile_model_effort_and_auth_fail_closed(self) -> None:
        cases = [
            ({"model": "gone", "reasoning_effort": "high", "auth_mode": "subscription",
              "max_turns": 8}, "DENIED_MODEL_UNAVAILABLE"),
            ({"model": "claude-test-model", "reasoning_effort": "max", "auth_mode": "subscription",
              "max_turns": 8}, "DENIED_EFFORT_UNSUPPORTED"),
            ({"model": "claude-test-model", "reasoning_effort": "high", "auth_mode": "api-key",
              "max_turns": 8}, "DENIED_AUTH_MODE_MISMATCH"),
        ]
        for profile, code in cases:
            with self.subTest(code=code):
                self.adapter.kill()
                self.adapter = ClaudeSdkAdapter(
                    workspace_root=self.root, worker_command=[sys.executable, str(FAKE)],
                    gateway_factory=lambda **_kwargs: self.gateway,
                )
                setattr(self.adapter, "_kaizen_vendor_runtime", str(self.root / ("runtime-" + code)))
                with self.assertRaises(ClaudeSdkAdapterError) as raised:
                    self.adapter.open(profile, None)
                self.assertEqual(raised.exception.code, code)

    def test_non_oauth_worker_is_denied_without_identity_details(self) -> None:
        self.adapter._env_extra.update({
            "KAIZEN_FAKE_CLAUDE_AUTH_SOURCE": "api-key",
            "KAIZEN_FAKE_CLAUDE_CLAIM_SUBSCRIPTION": "1",
        })
        with self.assertRaises(ClaudeSdkAdapterError) as raised:
            self.open()
        self.assertEqual(raised.exception.code, "DENIED_AUTH_MODE_MISMATCH")
        self.assertNotIn("api-key", str(raised.exception.payload()))

    def test_callbacks_forward_to_gateway(self) -> None:
        self.open()
        broker = lambda _request: "denied"
        guard = lambda _action: None
        self.adapter.set_approval_broker(broker)
        self.adapter.set_mutation_guard(guard)
        self.assertIs(self.gateway.broker, broker)
        self.assertIs(self.gateway.guard, guard)

    def test_close_reports_unproven_governed_tool_termination_fail_closed(self) -> None:
        self.open()
        self.gateway.termination_proven = False
        closed = self.adapter.close()
        self.assertEqual(closed["status"], "ERROR")
        self.assertEqual(closed["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertFalse(closed["termination_proven"])
        self.assertEqual(self.gateway.cancel_calls, 1)

    def test_prompt_is_staged_as_metadata_reference(self) -> None:
        self.open()
        self.adapter.bind_session("as-test")
        self.assertEqual(self.adapter.run_turn("not-on-jsonl").status, "OK")
        inputs = list((self.root / "runtime" / "inputs").iterdir())
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0].read_text(encoding="utf-8"), "not-on-jsonl")

    def test_process_card_metadata_excludes_output(self) -> None:
        opened = self.adapter._safe_tool_metadata({
            "call_id": "tool-1",
            "name": "kaizen_run_process",
            "input": {
                "executable": "tool.exe",
                "argv": ["--check"],
                "cwd": ".",
                "timeout_ms": 120_000,
            },
        })
        closed = self.adapter._safe_tool_result_metadata({
            "status": "OK",
            "result": {
                "exit_code": 0,
                "stdout": "private output",
                "stderr": "private error",
                "timed_out": False,
                "truncated": False,
                "effects_unknown": True,
                "policy_decision": "allow",
            },
        })
        self.assertEqual(opened["executable"], "tool.exe")
        self.assertEqual(opened["argv"], ["--check"])
        self.assertEqual(closed["exit_code"], 0)
        self.assertEqual(closed["stdout_sha256"], hashlib.sha256(b"private output").hexdigest())
        self.assertEqual(closed["stdout_bytes"], len(b"private output"))
        self.assertNotIn("stdout", closed)
        self.assertNotIn("stderr", closed)

    def test_proposal_card_retains_only_bounded_apply_truth(self) -> None:
        closed = self.adapter._safe_tool_result_metadata({
            "status": "OK",
            "result": {
                "applied": True,
                "partial_apply": False,
                "change_count": 2,
                "executor_status": "OK",
                "mismatches": [],
                "content": "must not persist",
            },
        })
        self.assertEqual(closed, {
            "applied": True,
            "partial_apply": False,
            "change_count": 2,
            "executor_status": "OK",
            "mismatches": [],
            "mismatch_count": 0,
            "mismatch_evidence_complete": True,
            "mismatch_evidence_uncertain": False,
            "mismatch_path_encoding": "tilde-codepoint-ascii-v1",
        })

    def test_fatal_gateway_result_terminalizes_before_later_worker_success(self) -> None:
        mismatch = {
            "path": "nested/name with space.txt",
            "reason": "final_state_mismatch",
            "expected_exists": True,
            "actual_exists": False,
            "expected_sha256": "a" * 64,
            "actual_sha256": None,
        }
        self.gateway.invoke = lambda *_args, **_kwargs: {
            "status": "DENIED",
            "code": "DENIED_APPROVAL_STALE_RERUN_REQUIRED",
            "fatal": True,
            "partial_apply": True,
            "mismatches": [mismatch],
        }
        self.adapter._gateway = self.gateway
        self.adapter._active_turn = "ct-fatal-tool"
        self.adapter._turn_result = None

        with mock.patch.object(self.adapter, "_request", return_value={}) as request:
            self.adapter._handle_tool("ct-fatal-tool", {
                "name": "kaizen_propose_changes",
                "tool_call_id": "tool-fatal",
                "args": {"changes": [{"kind": "create", "path": "x.txt", "content": "x"}]},
            })

        terminal = self.adapter._turn_result
        self.assertIsNotNone(terminal)
        self.assertTrue(terminal.fatal)
        self.assertEqual(terminal.error_code, "DENIED_APPROVAL_STALE_RERUN_REQUIRED")
        self.assertTrue(self.adapter._turn_done.is_set())
        request.assert_called_once()
        close = [event for event in self.events if event["event_kind"] == "tool_call"][-1]
        self.assertTrue(close["persistence_required"])
        evidence = close["payload"]["result"]
        self.assertEqual(evidence["mismatch_count"], 1)
        self.assertTrue(evidence["mismatch_evidence_complete"])

        self.adapter._handle_event({
            "event": "turn.result",
            "session_id": self.adapter._worker_session_id,
            "turn_id": "ct-fatal-tool",
            "seq": 1,
            "body": {"status": "OK", "final_text": "must not win", "num_turns": 1},
        })
        self.assertIs(self.adapter._turn_result, terminal)
        self.assertIsNone(terminal.final_text)

    def test_fatal_gateway_result_survives_oversize_staging_failure(self) -> None:
        mismatches = [{
            "path": f"d{index:03}/" + " " * 1_018 + "z",
            "reason": "final_state_mismatch",
            "expected_exists": True,
            "actual_exists": False,
            "expected_sha256": "a" * 64,
            "actual_sha256": None,
        } for index in range(128)]
        evidence = normalize_apply_evidence({
            "partial_apply": True,
            "mismatches": mismatches,
        })
        self.gateway.invoke = lambda *_args, **_kwargs: {
            "status": "DENIED",
            "code": "DENIED_APPROVAL_STALE_RERUN_REQUIRED",
            "fatal": True,
            **evidence,
        }
        self.adapter._gateway = self.gateway
        self.adapter._active_turn = "ct-fatal-stage"
        self.adapter._turn_result = None

        with mock.patch.object(
            self.adapter, "_stage_runtime_bytes", side_effect=OSError("runtime stage unavailable"),
        ) as stage, mock.patch.object(self.adapter, "_request", return_value={}) as request:
            self.adapter._handle_tool("ct-fatal-stage", {
                "name": "kaizen_propose_changes",
                "tool_call_id": "tool-fatal-stage",
                "args": {"changes": [{"kind": "create", "path": "x.txt", "content": "x"}]},
            })

        stage.assert_called_once()
        terminal = self.adapter._turn_result
        self.assertIsNotNone(terminal)
        self.assertTrue(terminal.fatal)
        self.assertEqual(terminal.error_code, "DENIED_APPROVAL_STALE_RERUN_REQUIRED")
        self.assertTrue(self.adapter._turn_done.is_set())
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["body"]["error"], {
            "status": "DENIED",
            "code": "DENIED_APPROVAL_STALE_RERUN_REQUIRED",
            "fatal": True,
        })

        self.adapter._handle_event({
            "event": "turn.result",
            "session_id": self.adapter._worker_session_id,
            "turn_id": "ct-fatal-stage",
            "seq": 1,
            "body": {"status": "OK", "final_text": "must not win", "num_turns": 1},
        })
        self.assertIs(self.adapter._turn_result, terminal)
        self.assertIsNone(terminal.final_text)

    def test_critical_recorder_failure_forces_recovery_terminal_result(self) -> None:
        self.gateway.invoke = lambda *_args, **_kwargs: {
            "status": "DENIED",
            "code": "DENIED_APPROVAL_STALE_RERUN_REQUIRED",
            "fatal": True,
            "partial_apply": True,
            "mismatches": [{"path": "", "reason": "apply_outcome_uncertain"}],
        }
        self.adapter._gateway = self.gateway
        self.adapter._recorder = mock.Mock(side_effect=RuntimeError("durable sink rejected"))
        self.adapter._active_turn = "ct-recorder-fail"
        self.adapter._turn_result = None

        with mock.patch.object(self.adapter, "_request", return_value={}) as request:
            self.adapter._handle_tool("ct-recorder-fail", {
                "name": "kaizen_propose_changes",
                "tool_call_id": "tool-recorder-fail",
                "args": {"changes": [{"kind": "create", "path": "x.txt", "content": "x"}]},
            })

        self.assertEqual(self.adapter._turn_result.error_code, "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertTrue(self.adapter._turn_result.fatal)
        sent = request.call_args.kwargs["body"]["error"]
        self.assertEqual(sent["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertTrue(sent["fatal"])

    def test_read_card_metadata_retains_only_reproducible_locator_and_hash(self) -> None:
        opened = self.adapter._safe_tool_metadata({
            "tool_call_id": "tool-read-1",
            "name": "kaizen_read_file",
            "input": {
                "path": "te-context.txt", "start_line": 1, "end_line": 2,
                "max_bytes": 4096, "unexpected": "drop me",
            },
        })
        closed = self.adapter._safe_tool_result_metadata({
            "status": "OK",
            "result": {
                "path": "te-context.txt", "sha256": "a" * 64, "text": "private file text",
                "start_line": 1, "end_line": 2, "total_lines": 2, "truncated": False,
            },
        })
        self.assertEqual(opened, {
            "name": "kaizen_read_file", "tool_call_id": "tool-read-1", "path": "te-context.txt",
            "start_line": 1, "end_line": 2, "max_bytes": 4096,
        })
        self.assertEqual(closed, {
            "path": "te-context.txt", "sha256": "a" * 64, "start_line": 1,
            "end_line": 2, "total_lines": 2, "truncated": False,
        })
        self.assertNotIn("text", closed)

    def test_budget_exhaustion_and_reported_overrun_are_terminal(self) -> None:
        self.open()
        self.adapter._active_turn = "ct-budget"
        self.adapter._tool_call_ids.add("tool-from-budget-turn")
        self.adapter._handle_event({
            "event": "turn.result", "session_id": self.adapter._worker_session_id,
            "turn_id": "ct-budget", "seq": 1,
            "body": {"status": "FAILED", "code": "MODEL_CALL_BUDGET_EXHAUSTED",
                     "num_turns": 8, "fatal": True},
        })
        exhausted = self.adapter._turn_result
        self.assertIsNotNone(exhausted)
        self.assertTrue(exhausted.fatal)
        self.assertEqual(exhausted.error_code, "MODEL_CALL_BUDGET_EXHAUSTED")
        self.assertEqual(self.adapter._tool_call_ids, set())

        self.adapter._active_turn = "ct-overrun"
        self.adapter._turn_result = None
        self.adapter._handle_event({
            "event": "turn.result", "session_id": self.adapter._worker_session_id,
            "turn_id": "ct-overrun", "seq": 1,
            "body": {"status": "OK", "final_text": "must not survive", "num_turns": 9},
        })
        overrun = self.adapter._turn_result
        self.assertIsNotNone(overrun)
        self.assertTrue(overrun.fatal)
        self.assertEqual(overrun.error_code, "MODEL_CALL_BUDGET_EXHAUSTED")
        self.assertIsNone(overrun.final_text)

    def test_events_are_bound_to_exact_worker_session_active_turn_and_unique_tool_call(self) -> None:
        self.adapter._active_turn = "ct-bound"
        valid = {
            "event": "delta", "session_id": self.adapter._worker_session_id,
            "turn_id": "ct-bound", "seq": 1, "body": {"text": "ok"},
        }
        self.adapter._handle_event(valid)
        for changed in (
            {**valid, "session_id": "ws-other", "seq": 2},
            {**valid, "turn_id": "ct-other", "seq": 2},
        ):
            with self.assertRaises(ValueError):
                self.adapter._handle_event(changed)
        self.adapter._tool_call_ids.add("tool-repeat")
        with self.assertRaises(ValueError):
            self.adapter._handle_event({
                "event": "tool.invoke", "session_id": self.adapter._worker_session_id,
                "turn_id": "ct-bound", "seq": 2,
                "body": {"call_id": "tool-repeat", "name": "kaizen_read_file", "input": {"path": "x"}},
            })

    def test_steer_neutralizes_raw_vendor_mentions_before_staging(self) -> None:
        self.adapter._active_turn = "ct-steer"
        with mock.patch.object(self.adapter, "_request", return_value={"accepted": True}):
            result = self.adapter.steer("inspect @secret.txt and user@example.invalid")
        self.assertEqual(result["status"], "OK")
        staged = list((self.root / "runtime" / "inputs").iterdir())
        self.assertEqual(len(staged), 1)
        text = staged[0].read_text(encoding="utf-8")
        self.assertNotIn("@", text)
        self.assertIn("＠secret.txt", text)

    def test_proposal_rehash_denies_stale_base_before_any_target_mutation(self) -> None:
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        first.write_text("approved-base-1", encoding="utf-8")
        second.write_text("approved-base-2", encoding="utf-8")
        proposal = {"changes": [
            {"kind": "modify", "path": "first.txt", "content": "new-1"},
            {"kind": "modify", "path": "second.txt", "content": "new-2"},
        ]}
        snapshot = policy.build_policy_snapshot(
            "claude", "ask", str(self.root), [], [], protected_paths=[], vendor_config_paths=[],
        )
        self.adapter._gateway_factory = None
        recovery_factory = lambda: None
        self.adapter._apply_recovery_callback_factory = recovery_factory
        gateway = self.adapter._make_gateway(snapshot)
        self.adapter._gateway = gateway
        self.assertIsInstance(gateway._proposal_executor, WorkspaceProposalExecutor)
        self.assertIs(gateway._recovery_callback_factory, recovery_factory)
        prepared = gateway._proposal_executor.prepare(proposal)
        second.write_text("changed-after-approval", encoding="utf-8")
        result = prepared.apply(proposal)
        self.assertEqual(result["status"], "DENIED")
        self.assertEqual(result["code"], "DENIED_APPROVAL_STALE_RERUN_REQUIRED")
        self.assertFalse(result["partial_apply"])
        self.assertEqual(first.read_text(encoding="utf-8"), "approved-base-1")
        self.assertEqual(second.read_text(encoding="utf-8"), "changed-after-approval")


class CapabilityAndEnvironmentTest(unittest.TestCase):
    def test_env_keeps_only_identity_bootstrap_and_d_scoped_controls(self) -> None:
        with mock.patch.dict(os.environ, {
            "HOME": "D:\\identity-home",
            "USERPROFILE": "D:\\identity-placeholder",
            "HOMEDRIVE": "D:",
            "HOMEPATH": "\\identity-placeholder",
            "APPDATA": "D:\\identity-placeholder\\appdata",
            "LOCALAPPDATA": "D:\\identity-placeholder\\localappdata",
            "XDG_CONFIG_HOME": "D:\\identity-placeholder\\config",
            "CLAUDE_CONFIG_DIR": "D:\\identity-placeholder\\claude",
            "SystemRoot": "D:\\windows-placeholder",
            "ComSpec": "D:\\windows-placeholder\\cmd.exe",
            "ANTHROPIC_API_KEY": "sk-ant-not-forwarded-1234567890",
            "SOME_API_KEY": "not-forwarded",
            "HTTP_PROXY": "http://proxy.invalid",
            "NODE_EXTRA_CA_CERTS": "D:\\hostile-ca.pem",
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
            "OTEL_EXPORTER_OTLP_HEADERS": "secret",
            "PATH": "D:\\ambient-path-must-not-pass",
            "UNRELATED_INNOCUOUS_FLAG": "must-not-pass",
            "DATABASE_URL": "must-not-pass",
        }, clear=True):
            env = _sanitized_worker_env({
                "PATH": "D:\\dev\\node",
                "TEMP": "D:\\tmp",
                "TMP": "D:\\tmp",
                "TMPDIR": "D:\\tmp",
                "XDG_CACHE_HOME": "D:\\cache",
                "KAIZEN_CLAUDE_SESSION_ROOT": "D:\\session",
            })
        self.assertEqual(env["HOME"], "D:\\identity-home")
        self.assertEqual(env["USERPROFILE"], "D:\\identity-placeholder")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "D:\\identity-placeholder\\claude")
        self.assertEqual(env["SYSTEMROOT"], "D:\\windows-placeholder")
        self.assertEqual(env["COMSPEC"], "D:\\windows-placeholder\\cmd.exe")
        self.assertEqual(env["PATH"], "D:\\dev\\node")
        self.assertEqual(env["TEMP"], "D:\\tmp")
        self.assertEqual(env["XDG_CACHE_HOME"], "D:\\cache")
        self.assertEqual(env["KAIZEN_CLAUDE_SESSION_ROOT"], "D:\\session")
        self.assertEqual(env["CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS"], "1")
        for forbidden in (
            "ANTHROPIC_API_KEY", "SOME_API_KEY", "HTTP_PROXY", "NODE_EXTRA_CA_CERTS",
            "NODE_TLS_REJECT_UNAUTHORIZED", "OTEL_EXPORTER_OTLP_HEADERS",
            "UNRELATED_INNOCUOUS_FLAG", "DATABASE_URL",
        ):
            self.assertNotIn(forbidden, env)

    def test_env_stripping_is_case_insensitive_and_extra_cannot_reinject_or_widen(self) -> None:
        with mock.patch.dict(os.environ, {
            "USERPROFILE": "D:\\identity-placeholder",
            "mixed_Session_Token": "secret",
            "Provider_CREDENTIALS_Path": "secret-file",
            "node_options": "--require=attacker.js",
            "NODE_PATH": "D:\\attacker",
        }, clear=True):
            env = _sanitized_worker_env({
                "AnThRoPiC_ApI_KeY": "reinjected",
                "Node_Options": "--inspect",
                "SAFE_FLAG": "must-not-pass",
                "KAIZEN_UNRELATED_FLAG": "must-not-pass",
                "KAIZEN_FAKE_CLAUDE_FAULT": "worker-death",
            })
        self.assertEqual(env["USERPROFILE"], "D:\\identity-placeholder")
        self.assertEqual(env["KAIZEN_FAKE_CLAUDE_FAULT"], "worker-death")
        lowered = {key.casefold() for key in env}
        for forbidden in (
            "mixed_session_token", "provider_credentials_path", "node_options", "node_path",
            "anthropic_api_key", "safe_flag", "kaizen_unrelated_flag",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_adapter_rebuilds_path_temp_and_cache_before_worker_spawn(self) -> None:
        captured: dict[str, str] = {}

        def refuse_spawn(*_args, **kwargs):
            captured.update(kwargs["env"])
            raise OSError("stop after environment capture")

        with tempfile.TemporaryDirectory(dir=AI_WORK) as tmp:
            root = Path(tmp).resolve()
            runtime = root / "runtime"
            adapter = ClaudeSdkAdapter(
                workspace_root=root,
                worker_command=[sys.executable, str(FAKE)],
                spawner=refuse_spawn,
            )
            setattr(adapter, "_kaizen_vendor_runtime", str(runtime))
            with mock.patch.dict(os.environ, {
                "USERPROFILE": "D:\\identity-placeholder",
                "PATH": "D:\\ambient-path-must-not-pass",
                "TEMP": "D:\\ambient-temp-must-not-pass",
                "XDG_CACHE_HOME": "D:\\ambient-cache-must-not-pass",
            }, clear=True), self.assertRaises(ClaudeSdkAdapterError):
                adapter.open({"auth_mode": "subscription", "permission_mode": "plan"}, None)
        expected_temp = str(runtime / "temp")
        expected_cache = str(root / "AI" / "work" / "orchestration" / "ui-cache")
        self.assertEqual(captured["PATH"], str(Path(sys.executable).resolve().parent))
        self.assertEqual(captured["TEMP"], expected_temp)
        self.assertEqual(captured["TMP"], expected_temp)
        self.assertEqual(captured["TMPDIR"], expected_temp)
        self.assertEqual(captured["XDG_CACHE_HOME"], expected_cache)
        self.assertEqual(captured["USERPROFILE"], "D:\\identity-placeholder")

    def test_probe_returns_dynamic_models_and_proves_close(self) -> None:
        with tempfile.TemporaryDirectory(dir=AI_WORK) as tmp:
            result = probe_capability(tmp, worker_command=[sys.executable, str(FAKE)])
        self.assertTrue(result["drivable"])
        self.assertEqual(result["models"][0]["id"], "claude-test-model")
        self.assertEqual(result["auth_modes"], ["subscription"])
        self.assertEqual(result["max_turns"], {"min": 1, "default": 8, "max": 32})
        self.assertEqual(result["_probed_features"], list(CAPABILITY_PROBE_FEATURES))
        self.assertFalse(any(key.endswith("_proven") for key in result if key != "_subscription_auth_proven"))

    def test_probe_logs_only_the_bounded_worker_refusal_code_and_field(self) -> None:
        messages: list[str] = []
        with tempfile.TemporaryDirectory(dir=AI_WORK) as tmp:
            result = probe_capability(
                tmp, worker_command=[sys.executable, str(FAKE)],
                env={"KAIZEN_FAKE_CLAUDE_FAULT": "initialize-field"}, logger=messages.append,
            )
        self.assertFalse(result["drivable"])
        self.assertEqual(result["availability"]["code"], "DENIED_WORKER_PROTOCOL")
        self.assertEqual(messages, ["Claude capability probe denied code=DENIED_WORKER_PROTOCOL field=account"])

    def test_probe_exposes_only_a_fixed_initialization_timeout_message(self) -> None:
        descriptor = claude_sdk.unavailable_capability("DENIED_SDK_UNAVAILABLE", "initialize_timeout")
        self.assertEqual(
            descriptor["availability"]["message"],
            "Claude subscription initialization timed out before account and model discovery.",
        )
        self.assertNotIn("initialize_timeout", descriptor["availability"]["message"])

    def test_probe_replaces_invalid_or_oversize_worker_error_field_without_leakage(self) -> None:
        for fault in ("initialize-invalid-field", "initialize-oversize-field"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=AI_WORK) as tmp:
                messages: list[str] = []
                result = probe_capability(
                    tmp, worker_command=[sys.executable, str(FAKE)],
                    env={"KAIZEN_FAKE_CLAUDE_FAULT": fault}, logger=messages.append,
                )
                self.assertFalse(result["drivable"])
                self.assertEqual(result["availability"]["code"], "DENIED_WORKER_PROTOCOL")
                self.assertEqual(
                    messages,
                    ["Claude capability probe denied code=DENIED_WORKER_PROTOCOL field=unknown"],
                )
                self.assertNotIn("PRIVATE_FIELD_MUST_NOT_ESCAPE", "".join(messages))

    def test_explicit_probe_refresh_replaces_removed_model_with_supported_models_result(self) -> None:
        with tempfile.TemporaryDirectory(dir=AI_WORK) as tmp:
            initial = probe_capability(
                tmp, worker_command=[sys.executable, str(FAKE)],
                env={"KAIZEN_FAKE_CLAUDE_REFRESH_CATALOG": "replace"},
            )
            refreshed = probe_capability(
                tmp, worker_command=[sys.executable, str(FAKE)],
                env={"KAIZEN_FAKE_CLAUDE_REFRESH_CATALOG": "replace"}, refresh_models=True,
            )
        self.assertEqual(
            [item["id"] for item in initial["models"]],
            ["claude-stable-model", "claude-removed-model"],
        )
        self.assertEqual(
            [item["id"] for item in refreshed["models"]],
            ["claude-stable-model", "claude-new-model"],
        )

    def test_malformed_supported_models_refresh_fails_closed_without_partial_replacement(self) -> None:
        with tempfile.TemporaryDirectory(dir=AI_WORK) as tmp:
            adapter = ClaudeSdkAdapter(
                workspace_root=tmp, worker_command=[sys.executable, str(FAKE)],
                env={"KAIZEN_FAKE_CLAUDE_REFRESH_CATALOG": "malformed"},
                gateway_factory=lambda **_kwargs: FakeGateway(),
            )
            setattr(adapter, "_kaizen_vendor_runtime", str(Path(tmp) / "runtime-malformed-refresh"))
            try:
                opened = adapter.open({"auth_mode": "subscription", "permission_mode": "plan"}, None)
                before = opened["models"]
                with self.assertRaises(ClaudeSdkAdapterError) as raised:
                    adapter.refresh_models()
                self.assertEqual(raised.exception.code, "DENIED_MODEL_UNAVAILABLE")
                self.assertEqual(adapter.models, before)
            finally:
                adapter.kill()

    def test_probe_evidence_is_per_feature_and_malformed_claims_stay_dark(self) -> None:
        with tempfile.TemporaryDirectory(dir=AI_WORK) as tmp:
            result = probe_capability(
                tmp,
                worker_command=[sys.executable, str(FAKE)],
                env={
                    "KAIZEN_FAKE_CLAUDE_PROBE_DARK": "image_attachments",
                    "KAIZEN_FAKE_CLAUDE_PROBE_CROSS_CLAIM": "controlled_tools",
                    "KAIZEN_FAKE_CLAUDE_PROBE_MALFORMED": "process_execution",
                },
            )
        self.assertTrue(result["drivable"])
        self.assertEqual(result["_probed_features"], [
            "streaming", "governed_context", "diff_snapshots",
        ])

    def test_initialize_and_close_alone_never_prove_advanced_features(self) -> None:
        with tempfile.TemporaryDirectory(dir=AI_WORK) as tmp:
            result = probe_capability(
                tmp,
                worker_command=[sys.executable, str(FAKE)],
                env={"KAIZEN_FAKE_CLAUDE_PROBE_DARK": ",".join(CAPABILITY_PROBE_FEATURES)},
            )
        self.assertTrue(result["drivable"])
        self.assertEqual(result["_probed_features"], [])


if __name__ == "__main__":
    unittest.main()
