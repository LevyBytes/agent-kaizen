"""Local-LLM adapter (v8 M6): each exit criterion asserted against a HERMETIC scripted provider.

No network, no child process, no real model: the "engine" is a scripted ``chat_provider`` closure over
a list of canned reply dicts, so the whole Kaizen-owned loop (prompt build -> parse -> decide() gate ->
executor -> events) runs deterministically in-process. PolicyEngine is constructed directly with
vendor=[] (never resolves the real ~/.claude / ~/.codex config paths -- test_policy.py owns the DB
legs); the recorder is a pure in-memory list; workspaces are tempdirs. Tests must pass on Windows
(primary) and be POSIX-tolerant.

Exit criteria -> proving test:
- gated multi-tool task completes ......... GatedMultiToolTaskTest
- injection suite (ledger #21) green ...... InjectionSuiteTest
- never exceeds max-turns ................. MaxTurnsTest
- parse failure yields no tool ............ ParseFailureNoToolTest
- ask fails closed ........................ AskFailClosedTest
- steer / interrupt shims ................. SteerInterruptTest (steer-inject, interrupt-cancel, no-turn)
- capabilities() shims .................... CapabilitiesShimsTest
- subagent inheritance ................... SubagentSimTest
- records use agent_type='other' ......... AgentTypeOtherTest
- stdout-pristine ........................ StdoutPristineTest
- untrusted output returned verbatim ..... UntrustedOutputTest
- every-exit-path-terminal ............... EveryExitPathTerminalTest
- canonical ToolGateway reuse ............ CanonicalToolGatewayTest
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from _harness import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT))

from kaizen_components.orchestration import policy  # noqa: E402
from kaizen_components.orchestration.adapters import BrokerApprovalResult, TurnResult  # noqa: E402
from kaizen_components.orchestration.adapters import local_llm as L  # noqa: E402
from kaizen_components.orchestration.supervisor import Supervisor  # noqa: E402


# --- helpers ----------------------------------------------------------------------------------

def engine(rules=(), protected=()) -> policy.PolicyEngine:
    # vendor=[] so the engine does not resolve the real ~/.claude / ~/.codex config paths in a unit test.
    return policy.PolicyEngine(list(protected), list(rules), [])


def path_prefix_allow(verb: str, prefix: str, rid: str) -> dict:
    return {"id": rid, "rule_type": "allow", "verb": verb, "match_kind": "path_prefix",
            "pattern": prefix, "engine": None, "enabled": True}


def scripted_provider(replies):
    """A chat_provider closure that returns canned dicts in order (then repeats the last one so a loop
    that over-calls does not IndexError -- the ceiling/final assertions catch over-calling explicitly).
    Records every call's message list so tests can assert steer/corrective injection."""
    state = {"i": 0, "calls": []}

    def provider(messages, **opts):
        state["calls"].append([dict(m) for m in messages])
        idx = min(state["i"], len(replies) - 1)
        state["i"] += 1
        reply = replies[idx]
        return {"text": reply} if isinstance(reply, str) else dict(reply)

    provider.state = state  # type: ignore[attr-defined]
    return provider


def tool_reply(name: str, **args) -> str:
    return json.dumps({"tool": name, "args": args})


def final_reply(answer: str) -> str:
    return json.dumps({"final": answer})


def deterministic_ids():
    """A deterministic id_factory: monotonically numbered per prefix (turn-1, toolcall-1, ...)."""
    counters: dict[str, int] = {}

    def factory(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]}"

    return factory


def make_adapter(eng, provider, *, tools=None, recorder=None, max_turns=8, approval_timeout=30.0,
                 model=None):
    events = recorder if recorder is not None else []
    adapter = L.LocalLLMAdapter(
        eng,
        chat_provider=provider,
        tools=tools,
        recorder=events.append,
        logger=lambda _msg: None,  # silence adapter stderr logging during tests
        id_factory=deterministic_ids(),
        model=model,
        max_turns=max_turns,
        approval_timeout=approval_timeout,
    )
    adapter.test_events = events  # type: ignore[attr-defined]
    return adapter


def markers(events, kind):
    return [(e["marker"], e.get("correlation_id"), e.get("payload", {})) for e in events
            if e["event_kind"] == kind]


def tmp_workspace(case: unittest.TestCase) -> Path:
    d = Path(tempfile.mkdtemp(prefix="kaizen-m6-ws-"))
    case.addCleanup(__import__("shutil").rmtree, d, ignore_errors=True)
    return d


def marker_seq(events):
    return [(e["event_kind"], e["marker"]) for e in events]


class CanonicalToolGatewayTest(unittest.TestCase):
    def snapshot(self, root: Path, mode: str = "plan") -> policy.PolicySnapshot:
        return policy.build_policy_snapshot(
            "local_llm",
            mode,
            str(root),
            [],
            [],
            protected_paths=[],
            vendor_config_paths=[],
        )

    def production_adapter(
        self,
        root: Path,
        snapshot: policy.PolicySnapshot,
        provider,
        events: list[dict],
    ) -> L.LocalLLMAdapter:
        supervisor = Supervisor.__new__(Supervisor)
        supervisor.repo_root = root
        supervisor._adapter_factory = None
        supervisor.policy = snapshot.build_engine()
        supervisor.log = lambda _message: None
        adapter = supervisor._build_driven_adapter(
            "ar_gateway",
            {"engine_name": "local_llm", "model": "fixture", "max_turns": 3},
            snapshot=snapshot,
            recorder_override=events.append,
        )
        adapter._chat_provider = provider
        adapter._id_factory = deterministic_ids()
        adapter.bind_session("as_gateway")
        return adapter

    def test_default_local_gateway_carries_apply_recovery_factory(self):
        root = tmp_workspace(self)
        snapshot = self.snapshot(root)
        recovery_factory = lambda: None
        adapter = L.LocalLLMAdapter(
            snapshot.build_engine(),
            workspace_root=root,
            apply_recovery_callback_factory=recovery_factory,
        )

        gateway = adapter._make_gateway(snapshot)

        self.assertIs(gateway._recovery_callback_factory, recovery_factory)

    def test_normal_supervisor_adapter_exposes_exact_gateway_and_plan_process_denies_pre_execution(self):
        root = tmp_workspace(self)
        snapshot = self.snapshot(root)
        provider = scripted_provider([
            tool_reply(
                "kaizen_run_process",
                executable=sys.executable,
                argv=["-c", "print('MUST_NOT_RUN')"],
                cwd=".",
                timeout_ms=5000,
            ),
            final_reply("structured denial observed"),
        ])
        events: list[dict] = []
        adapter = self.production_adapter(root, snapshot, provider, events)
        broker = mock.Mock(side_effect=AssertionError("Plan denial must not open an approval"))
        guard = mock.Mock(side_effect=AssertionError("Plan denial must not reach the mutation guard"))
        adapter.set_approval_broker(broker)
        adapter.set_mutation_guard(guard)
        adapter.open({"model": "fixture", "permission_mode": "plan"}, snapshot)

        # Changing the adapter's legacy authority after open cannot alter the gateway's frozen snapshot.
        adapter.policy_engine = engine(rules=[{
            "id": "allow_exec_after_open", "rule_type": "allow", "verb": "exec",
            "match_kind": "command_regex", "pattern": ".*", "engine": None, "enabled": True,
        }])
        result = adapter.start_turn("attempt the bounded control process")

        self.assertEqual(result["status"], "OK", result)
        broker.assert_not_called()
        guard.assert_not_called()
        system_prompt = provider.state["calls"][0][0]["content"]
        self.assertEqual(
            [name for name in L.CANONICAL_TOOL_NAMES if name in system_prompt],
            list(L.CANONICAL_TOOL_NAMES),
        )
        self.assertNotIn("write_file", system_prompt)
        tool_events = [event for event in events if event["event_kind"] == "tool_call"]
        self.assertEqual([event["marker"] for event in tool_events], ["open", "close_fail"])
        self.assertEqual(tool_events[0]["payload"], {
            "name": "kaizen_run_process",
            "tool_call_id": "toolcall-1",
            "turn_id": "turn-1",
            "executable": sys.executable,
            "argv": ["-c", "print('MUST_NOT_RUN')"],
            "cwd": ".",
            "timeout_ms": 5000,
        })
        self.assertEqual(tool_events[1]["code"], "MODE_CEILING:exec")
        self.assertEqual(tool_events[1]["payload"]["name"], "kaizen_run_process")
        self.assertFalse((root / "AI" / "work" / "orchestration" / "tool-runtime").exists())
        self.assertTrue(adapter.close()["termination_proven"])

    def test_canonical_read_uses_gateway_once_and_returns_raw_text_only_to_model(self):
        root = tmp_workspace(self)
        (root / "note.txt").write_text("gateway-codeword", encoding="utf-8")
        snapshot = self.snapshot(root)
        provider = scripted_provider([
            tool_reply("kaizen_read_file", path="note.txt"),
            final_reply("gateway-codeword"),
        ])
        events: list[dict] = []
        adapter = self.production_adapter(root, snapshot, provider, events)
        guard = mock.Mock(side_effect=AssertionError("read must not reach mutation guard"))
        adapter.set_approval_broker(lambda _request: "denied")
        adapter.set_mutation_guard(guard)
        adapter.open({"model": "fixture", "permission_mode": "plan"}, snapshot)

        result = adapter.start_turn("read the note")

        self.assertEqual(result["status"], "OK", result)
        guard.assert_not_called()
        self.assertIn("gateway-codeword", provider.state["calls"][1][-1]["content"])
        tool_events = [event for event in events if event["event_kind"] == "tool_call"]
        self.assertEqual([event["marker"] for event in tool_events], ["open", "close_ok"])
        self.assertEqual(tool_events[0]["payload"]["name"], "kaizen_read_file")
        self.assertEqual(tool_events[1]["payload"]["result"], {"truncated": False})

    def test_normal_local_proposal_uses_authoritative_executor_after_broker_and_guard(self):
        root = tmp_workspace(self)
        target = root / "stale.txt"
        target.write_text("authoritative-before", encoding="utf-8")
        snapshot = self.snapshot(root, "ask")
        provider = scripted_provider([
            tool_reply(
                "kaizen_propose_changes",
                summary="approved local proposal",
                changes=[{"kind": "modify", "path": "stale.txt", "content": "authoritative-after"}],
            ),
            final_reply("proposal applied"),
        ])
        events: list[dict] = []
        adapter = self.production_adapter(root, snapshot, provider, events)
        broker_requests: list[dict] = []

        def broker(request):
            broker_requests.append(dict(request))
            self.assertEqual(target.read_text(encoding="utf-8"), "authoritative-before")
            return BrokerApprovalResult(
                L.DECISION_APPROVED,
                updated_input=request["diff_request"]["updated_input"],
                post_apply=lambda: {
                    "status": "OK" if target.read_text(encoding="utf-8") == "authoritative-after" else "DENIED",
                    "partial_apply": False,
                    "mismatches": [],
                },
            )

        broker = mock.Mock(side_effect=broker)
        guard = mock.Mock(return_value=None)
        adapter._apply_recovery_callback_factory = lambda: lambda _state: True
        adapter.set_approval_broker(broker)
        adapter.set_mutation_guard(guard)
        adapter.open({"model": "fixture", "permission_mode": "ask"}, snapshot)

        result = adapter.start_turn("propose the approved modification")

        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(target.read_text(encoding="utf-8"), "authoritative-after")
        broker.assert_called_once()
        guard.assert_called_once()
        self.assertTrue(broker_requests[0]["negotiated"])
        self.assertNotIn("authoritative-after", str(events))
        self.assertFalse(any(event["event_kind"] == "approval" for event in events))
        tool_events = [event for event in events if event["event_kind"] == "tool_call"]
        self.assertEqual([event["marker"] for event in tool_events], ["open", "close_ok"])
        self.assertNotIn("code", tool_events[-1])

    def test_durable_fatal_gateway_result_never_serializes_or_reaches_second_model_iteration(self):
        root = tmp_workspace(self)
        snapshot = self.snapshot(root)
        provider = scripted_provider([
            tool_reply("kaizen_read_file", path="note.txt"),
            final_reply("must not run"),
        ])
        events: list[dict] = []
        adapter = self.production_adapter(root, snapshot, provider, events)
        adapter.open({"model": "fixture", "permission_mode": "plan"}, snapshot)
        adapter._gateway.execute = mock.Mock(return_value={
            "status": "DENIED",
            "code": "DENIED_APPROVAL_STALE_RERUN_REQUIRED",
            "fatal": True,
            "partial_apply": True,
            "mismatches": [{"path": "", "reason": "apply_outcome_uncertain"}],
            "opaque_non_serializable": object(),
        })

        result = adapter.start_turn("exercise a fatal gateway result")

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["code"], "DENIED_APPROVAL_STALE_RERUN_REQUIRED")
        self.assertEqual(provider.state["i"], 1)
        close = [event for event in events if event["event_kind"] == "tool_call"][-1]
        self.assertEqual(close["marker"], "close_fail")
        self.assertTrue(close["persistence_required"])
        self.assertTrue(close["payload"]["result"]["mismatch_evidence_uncertain"])

    def test_nonfatal_gateway_serialization_failure_terminalizes_without_second_model_call(self):
        root = tmp_workspace(self)
        snapshot = self.snapshot(root)
        provider = scripted_provider([
            tool_reply("kaizen_read_file", path="note.txt"),
            final_reply("must not run"),
        ])
        events: list[dict] = []
        adapter = self.production_adapter(root, snapshot, provider, events)
        adapter.open({"model": "fixture", "permission_mode": "plan"}, snapshot)
        adapter._gateway.execute = mock.Mock(return_value={
            "status": "OK", "fatal": False, "result": {"truncated": False},
        })

        with mock.patch.object(L.json, "dumps", side_effect=MemoryError("encoder exhausted")):
            result = adapter.start_turn("exercise model-facing serialization failure")

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["code"], L.DENIED_TOOL_GATEWAY_UNAVAILABLE)
        self.assertEqual(provider.state["i"], 1)

    def test_recovery_required_recorder_failure_stays_terminal_and_never_reenters_model(self):
        root = tmp_workspace(self)
        snapshot = self.snapshot(root)
        provider = scripted_provider([
            tool_reply("kaizen_read_file", path="note.txt"),
            final_reply("must not run"),
        ])
        adapter = self.production_adapter(root, snapshot, provider, [])
        adapter.open({"model": "fixture", "permission_mode": "plan"}, snapshot)
        adapter._gateway.execute = mock.Mock(return_value={
            "status": "DENIED",
            "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
            "fatal": True,
            "partial_apply": False,
            "mismatches": [{"path": "target.txt", "reason": "staged_cleanup_unproven"}],
            "mismatch_evidence_uncertain": False,
        })
        adapter._recorder = mock.Mock(side_effect=RuntimeError("durable sink rejected"))

        result = adapter.start_turn("exercise durable recorder failure")

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertEqual(provider.state["i"], 1)

    def test_close_and_kill_release_gateway_authority(self):
        class Gateway:
            def __init__(self):
                self.close_calls = 0

            def cancel_active_processes(self, _timeout):
                return {"termination_proven": True, "active_count": 0}

            def close(self):
                self.close_calls += 1
                return True

        for operation in ("close", "kill"):
            with self.subTest(operation=operation):
                adapter = make_adapter(engine(), scripted_provider([final_reply("unused")]), tools={})
                gateway = Gateway()
                adapter._gateway = gateway
                result = getattr(adapter, operation)()
                self.assertTrue(result["termination_proven"])
                self.assertEqual(gateway.close_calls, 1)


# --- 1. gated multi-tool task -----------------------------------------------------------------

class GatedMultiToolTaskTest(unittest.TestCase):
    """A gated multi-tool task completes: write_file -> read_file -> final, each tool ALLOWed by a
    path_prefix rule on the tmp root, files really written/read, the full event sequence emitted, status
    OK with the final text, iterations == 3."""

    def test_write_then_read_then_final(self):
        ws = tmp_workspace(self)
        root = str(ws)
        eng = engine(rules=[
            path_prefix_allow("file_write", root, "r_w"),
            path_prefix_allow("file_read", root, "r_r"),
        ])
        target = str(ws / "note.txt")
        provider = scripted_provider([
            tool_reply("write_file", path=target, content="hello-m6"),
            tool_reply("read_file", path=target),
            final_reply("done: the file says hello-m6"),
        ])
        adapter = make_adapter(eng, provider, tools=L.build_default_tools(ws))
        adapter.start_session(cwd=root)
        out = adapter.start_turn("write then read the note")

        self.assertEqual(out["status"], "OK", out)
        self.assertIn("hello-m6", out["final"])
        self.assertEqual(out["iterations"], 3)
        # The file was really written and its content read back.
        self.assertTrue((ws / "note.txt").is_file())
        self.assertEqual((ws / "note.txt").read_text(encoding="utf-8"), "hello-m6")
        # The read tool's result surfaced back into the message list (provider saw it on call #3).
        third_call = provider.state["calls"][2]
        self.assertTrue(any("hello-m6" in m["content"] for m in third_call if m["role"] == "user"))

        # Event sequence: session open, turn open, tool_call open/close_ok x2, approval resolved x2,
        # turn close_ok.
        seq = marker_seq(adapter.test_events)
        self.assertIn(("session", "open"), seq)
        self.assertIn(("turn", "open"), seq)
        tool_opens = [m for m in seq if m == ("tool_call", "open")]
        tool_oks = [m for m in seq if m == ("tool_call", "close_ok")]
        appr_res = [m for m in seq if m == ("approval", "resolved")]
        self.assertEqual(len(tool_opens), 2)
        self.assertEqual(len(tool_oks), 2)
        self.assertEqual(len(appr_res), 2)
        self.assertEqual(seq[-1], ("turn", "close_ok"))
        # The allow events carry their rule_id (approval resolved code == rule_id).
        res_codes = {e.get("code") for e in adapter.test_events
                     if e["event_kind"] == "approval" and e["marker"] == "resolved"}
        self.assertEqual(res_codes, {"r_w", "r_r"})


# --- 2. injection suite (ledger #21) ----------------------------------------------------------

def marker_writer_tool(marker_path: Path) -> L.ToolSpec:
    """An injected exec ToolSpec whose runner writes a MARKER file. Used to PROVE a denied intent never
    reaches the runner: if the marker exists, the runner ran (a policy-bypass). command() = args.command
    so INV_GIT_PUSH scans the shell string."""
    def _run(args):
        marker_path.write_text("RAN", encoding="utf-8")
        return "command executed"

    return L.ToolSpec("run_command", "exec", "run a shell command", _run,
                      command=lambda a: a.get("command"), arg_hints=("command",))


class InjectionSuiteTest(unittest.TestCase):
    """Ledger #21: model output containing git push / a protected-path write is denied IDENTICALLY to
    Codex. (a) git push through an injected exec tool; (b) protected-path write (file + exec redirect);
    (c) cross-adapter identity -- the SAME engine denies a codex-shaped and a local-llm-shaped git push
    with the same invariant_id."""

    def test_a_git_push_intent_denied_marker_absent(self):
        ws = tmp_workspace(self)
        marker = ws / "MARKER"
        eng = engine()
        provider = scripted_provider([
            tool_reply("run_command", command="git push origin main"),
            final_reply("aborted the push"),
        ])
        adapter = make_adapter(eng, provider, tools={"run_command": marker_writer_tool(marker)})
        adapter.start_session(cwd=str(ws))
        out = adapter.start_turn("please push")

        # The runner never ran: no marker file.
        self.assertFalse(marker.exists(), "denied git-push runner must NOT have executed")
        # The approval was declined with invariant_id == INV_GIT_PUSH, code == INV_GIT_PUSH.
        declined = [e for e in adapter.test_events
                    if e["event_kind"] == "approval" and e["marker"] == "declined"]
        self.assertTrue(declined)
        self.assertEqual(declined[0]["payload"]["invariant_id"], policy.INV_GIT_PUSH)
        self.assertEqual(declined[0]["code"], policy.INV_GIT_PUSH)
        # The tool_call closed close_fail (with the same code); the turn still closed OK (deny recovers).
        tool_fail = [e for e in adapter.test_events
                     if e["event_kind"] == "tool_call" and e["marker"] == "close_fail"]
        self.assertTrue(tool_fail)
        self.assertEqual(tool_fail[0]["code"], policy.INV_GIT_PUSH)
        self.assertEqual(out["status"], "OK")
        self.assertEqual(marker_seq(adapter.test_events)[-1], ("turn", "close_ok"))

    def test_b_protected_path_write_denied_file_and_exec(self):
        ws = tmp_workspace(self)
        protected = "c:\\kaizen-m6-protected-test"
        eng = engine(protected=[protected])

        # (b1) file_write tool targeting the protected path -> INV_PROTECTED_PATH, runner not called.
        write_calls = {"n": 0}

        def _counting_write(args):
            write_calls["n"] += 1
            return "wrote"

        write_tool = L.ToolSpec("write_file", "file_write", "write a file", _counting_write,
                                arg_hints=("path", "content"))
        provider1 = scripted_provider([
            tool_reply("write_file", path="c:\\kaizen-m6-protected-test\\x.txt", content="x"),
            final_reply("gave up on the protected write"),
        ])
        a1 = make_adapter(eng, provider1, tools={"write_file": write_tool})
        a1.start_session(cwd=str(ws))
        out1 = a1.start_turn("write the protected file")
        self.assertEqual(write_calls["n"], 0, "protected write runner must not be called")
        declined1 = [e for e in a1.test_events
                     if e["event_kind"] == "approval" and e["marker"] == "declined"]
        self.assertTrue(declined1)
        self.assertEqual(declined1[0]["payload"]["invariant_id"], policy.INV_PROTECTED_PATH)
        self.assertEqual(out1["status"], "OK")

        # (b2) exec tool with a redirect into the protected path -> INV_PROTECTED_PATH, marker absent.
        marker = ws / "MARKER2"
        provider2 = scripted_provider([
            tool_reply("run_command",
                       command="cmd /c echo x > c:\\kaizen-m6-protected-test\\x.txt"),
            final_reply("aborted"),
        ])
        a2 = make_adapter(eng, provider2, tools={"run_command": marker_writer_tool(marker)})
        a2.start_session(cwd=str(ws))
        a2.start_turn("write via shell")
        self.assertFalse(marker.exists())
        declined2 = [e for e in a2.test_events
                     if e["event_kind"] == "approval" and e["marker"] == "declined"]
        self.assertTrue(declined2)
        self.assertEqual(declined2[0]["payload"]["invariant_id"], policy.INV_PROTECTED_PATH)

    def test_c_cross_adapter_identity_git_push_denied_identically(self):
        # One shared engine. The codex adapter's _answer_approval submits a RequestedAction shaped
        # (engine "codex", verb exec, command "git push origin main"); the local-llm adapter submits
        # (engine "local-llm", verb exec, same command). Both must DENY with the SAME invariant_id.
        eng = engine()
        cmd = "git push origin main"
        codex_action = policy.RequestedAction(
            actor=policy.Actor("codex", "codex", 0, "thread-x"), verb="exec", command=cmd, raw={})
        local_action = policy.RequestedAction(
            actor=policy.Actor("local-llm", "local-llm", 0, "turn-x"), verb="exec", command=cmd,
            raw={"tool": "run_command"})
        d_codex = eng.decide(codex_action, 0)
        d_local = eng.decide(local_action, 0)
        self.assertEqual(d_codex.result, policy.DENY)
        self.assertEqual(d_local.result, policy.DENY)
        self.assertEqual(d_codex.invariant_id, policy.INV_GIT_PUSH)
        self.assertEqual(d_local.invariant_id, policy.INV_GIT_PUSH)
        # "denied identically to Codex": same invariant on both engine shapes.
        self.assertEqual(d_codex.invariant_id, d_local.invariant_id)


# --- 3. max-turns ------------------------------------------------------------------------------

class MaxTurnsTest(unittest.TestCase):
    """A local model does not self-terminate. With a provider that never emits a final the loop hits the
    ceiling: EXACTLY max_turns provider calls (never a max_turns+1-th), turn close_fail
    MAX_TURNS_EXHAUSTED. A permanent parse-garbage provider hits the same ceiling (parse failures count)."""

    def test_never_final_hits_ceiling_exactly(self):
        ws = tmp_workspace(self)
        eng = engine(rules=[path_prefix_allow("file_read", str(ws), "r_r")])
        # An allowed no-op read tool the model calls forever (never final).
        noop = L.ToolSpec("noop", "file_read", "no-op", lambda a: "ok", targets=lambda a: ())
        provider = scripted_provider([tool_reply("noop")])
        adapter = make_adapter(eng, provider, tools={"noop": noop}, max_turns=4)
        adapter.start_session(cwd=str(ws))
        out = adapter.start_turn("loop forever")

        self.assertEqual(out["status"], "FAILED")
        self.assertEqual(out["code"], L.MAX_TURNS_EXHAUSTED)
        self.assertEqual(out["iterations"], 4)
        self.assertEqual(provider.state["i"], 4, "exactly max_turns provider calls (no +1)")
        fails = [e for e in adapter.test_events
                 if e["event_kind"] == "turn" and e["marker"] == "close_fail"]
        self.assertTrue(fails)
        self.assertEqual(fails[0]["code"], L.MAX_TURNS_EXHAUSTED)

    def test_permanent_parse_garbage_hits_same_ceiling(self):
        eng = engine()
        provider = scripted_provider(["this is not json at all"])
        adapter = make_adapter(eng, provider, tools={}, max_turns=3)
        adapter.start_session()
        out = adapter.start_turn("go")
        self.assertEqual(out["code"], L.MAX_TURNS_EXHAUSTED)
        self.assertEqual(out["iterations"], 3)
        self.assertEqual(provider.state["i"], 3)


# --- 4. parse failure yields no tool ----------------------------------------------------------

class _DecideSpyEngine(policy.PolicyEngine):
    """A PolicyEngine subclass that counts decide() calls (to prove a parse failure never gates)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.decide_calls = 0

    def decide(self, action, current_epoch):
        self.decide_calls += 1
        return super().decide(action, current_epoch)


class ParseFailureNoToolTest(unittest.TestCase):
    """A garbage reply => ZERO decide() calls (no tool gated), no tool_call events, a corrective message
    in the NEXT provider call's messages, and it still counts toward iterations."""

    def test_garbage_reply_no_decide_no_tool_events(self):
        eng = _DecideSpyEngine([], [], [])
        # First reply is garbage; second is a final so the turn terminates deterministically.
        provider = scripted_provider(["<<not json>>", final_reply("recovered")])
        adapter = make_adapter(eng, provider, tools={}, max_turns=5)
        adapter.start_session()
        out = adapter.start_turn("go")

        self.assertEqual(out["status"], "OK")
        self.assertEqual(eng.decide_calls, 0, "a parse failure must never call decide()")
        self.assertEqual([e for e in adapter.test_events if e["event_kind"] == "tool_call"], [])
        # The garbage reply counted (2 iterations: garbage + final).
        self.assertEqual(out["iterations"], 2)
        # The corrective message landed in the SECOND provider call's messages.
        second_call = provider.state["calls"][1]
        self.assertTrue(any("could not be parsed" in m["content"]
                            for m in second_call if m["role"] == "user"))


# --- 5. ask fails closed ----------------------------------------------------------------------

class AskFailClosedTest(unittest.TestCase):
    """No rules => default ask. No callback => approval open + declined, runner never called. An
    approving callback => runner called + approval resolved. A slow callback with a tiny approval_timeout
    => declined (fail-closed timeout)."""

    def _tool(self, ran_flag):
        def _run(args):
            ran_flag["ran"] = True
            return "did it"
        return L.ToolSpec("act", "file_read", "an ask-defaulting tool", _run, targets=lambda a: ())

    def test_no_callback_fails_closed(self):
        eng = engine()  # no rules -> ask
        ran = {"ran": False}
        provider = scripted_provider([tool_reply("act"), final_reply("done")])
        adapter = make_adapter(eng, provider, tools={"act": self._tool(ran)})
        adapter.start_session()
        adapter.start_turn("go")
        self.assertFalse(ran["ran"], "no approval surface -> runner must not run")
        kinds = marker_seq(adapter.test_events)
        self.assertIn(("approval", "open"), kinds)
        self.assertIn(("approval", "declined"), kinds)

    def test_approving_callback_runs_tool(self):
        eng = engine()
        ran = {"ran": False}
        provider = scripted_provider([tool_reply("act"), final_reply("done")])
        adapter = make_adapter(eng, provider, tools={"act": self._tool(ran)})
        adapter.on_approval(lambda _req: L.DECISION_APPROVED)
        adapter.start_session()
        adapter.start_turn("go")
        self.assertTrue(ran["ran"], "an approving callback must run the tool")
        self.assertIn(("approval", "resolved"), marker_seq(adapter.test_events))

    def test_slow_callback_times_out_to_deny(self):
        eng = engine()
        ran = {"ran": False}
        provider = scripted_provider([tool_reply("act"), final_reply("done")])
        adapter = make_adapter(eng, provider, tools={"act": self._tool(ran)}, approval_timeout=0.05)

        def _slow(_req):
            time.sleep(1.0)
            return L.DECISION_APPROVED

        adapter.on_approval(_slow)
        adapter.start_session()
        adapter.start_turn("go")
        self.assertFalse(ran["ran"], "a timed-out approval fails closed -> runner must not run")
        self.assertIn(("approval", "declined"), marker_seq(adapter.test_events))

    def test_broker_delegation_skips_direct_c4_resolver_and_approval_events(self):
        ran = {"ran": False}
        adapter = make_adapter(
            engine(), scripted_provider([tool_reply("act"), final_reply("done")]),
            tools={"act": self._tool(ran)},
        )
        direct_c4 = mock.Mock(side_effect=AssertionError("record_ask must be broker-owned"))
        legacy = mock.Mock(side_effect=AssertionError("legacy resolver must be skipped"))
        broker = mock.Mock(return_value=L.DECISION_APPROVED)
        adapter.bind_session("as_broker_fixture")
        adapter.policy_engine.record_ask = direct_c4
        adapter.on_approval(legacy)
        adapter.set_approval_broker(broker)

        adapter.start_session()
        adapter.start_turn("go")

        self.assertTrue(ran["ran"])
        broker.assert_called_once()
        direct_c4.assert_not_called()
        legacy.assert_not_called()
        self.assertFalse(any(event["event_kind"] == "approval" for event in adapter.test_events))
        self.assertTrue(any(event["event_kind"] == "tool_call" and event["marker"] == "close_ok"
                            for event in adapter.test_events))

    def test_broker_denial_keeps_tool_failure_event_without_approval_events(self):
        ran = {"ran": False}
        adapter = make_adapter(
            engine(), scripted_provider([tool_reply("act"), final_reply("done")]),
            tools={"act": self._tool(ran)},
        )
        adapter.set_approval_broker(lambda _request: L.DECISION_DENIED)
        adapter.start_session()
        adapter.start_turn("go")

        self.assertFalse(ran["ran"])
        self.assertFalse(any(event["event_kind"] == "approval" for event in adapter.test_events))
        self.assertTrue(any(event["event_kind"] == "tool_call" and event["marker"] == "close_fail"
                            for event in adapter.test_events))

    def test_broker_typed_result_replaces_input_and_audits_before_close(self):
        ws = tmp_workspace(self)
        target = ws / "typed.txt"
        seen: list[dict] = []

        def run(args):
            seen.append(dict(args))
            target.write_bytes(str(args["content"]).encode("utf-8"))
            return "done"

        adapter = make_adapter(
            engine(),
            scripted_provider([
                tool_reply("write_file", path=str(target), content="unapproved"),
                final_reply("done"),
            ]),
            tools={
                "write_file": L.ToolSpec(
                    "write_file", "file_write", "write", run,
                    targets=lambda args: (str(args["path"]),),
                ),
            },
        )

        def broker(request):
            self.assertTrue(request["negotiated"])
            self.assertNotIn("content", request)
            return BrokerApprovalResult(
                L.DECISION_APPROVED,
                updated_input={"path": str(target), "content": "approved"},
                post_apply=lambda: {
                    "status": "OK", "partial_apply": False, "mismatches": [],
                },
            )

        adapter.set_approval_broker(broker)
        adapter.set_mutation_guard(lambda _action: None)
        adapter.start_session(cwd=str(ws))
        out = adapter.start_turn("go")

        self.assertEqual(out["status"], "OK")
        self.assertEqual(seen, [{"path": str(target), "content": "approved"}])
        self.assertEqual(target.read_bytes(), b"approved")
        event_order = marker_seq(adapter.test_events)
        self.assertLess(event_order.index(("verification", "point")), event_order.index(("tool_call", "close_ok")))

    def test_broker_post_apply_mismatch_is_fatal_and_never_claims_rollback(self):
        ws = tmp_workspace(self)
        target = ws / "mismatch.txt"
        tools = L.build_default_tools(ws)
        adapter = make_adapter(
            engine(),
            scripted_provider([
                tool_reply("write_file", path=str(target), content="after"),
                final_reply("must not be reached"),
            ]),
            tools={"write_file": tools["write_file"]},
        )
        mismatch = {
            "path": target.name, "expected_exists": True, "actual_exists": True,
            "expected_sha256": "1" * 64, "actual_sha256": "2" * 64,
        }
        adapter.set_approval_broker(lambda _request: BrokerApprovalResult(
            L.DECISION_APPROVED,
            post_apply=lambda: {
                "status": "DENIED", "code": L.DENIED_PARTIAL_APPLY,
                "partial_apply": True, "mismatches": [mismatch],
            },
        ))
        adapter.set_mutation_guard(lambda _action: None)
        adapter.start_session(cwd=str(ws))
        out = adapter.start_turn("go")

        self.assertEqual(out["status"], "FAILED")
        self.assertEqual(out["code"], L.DENIED_PARTIAL_APPLY)
        verification = [
            event for event in adapter.test_events
            if event["event_kind"] == "verification" and event["marker"] == "point"
        ]
        self.assertEqual(len(verification), 1)
        self.assertEqual(verification[0]["payload"]["mismatches"], [{
            **mismatch, "reason": "final_state_mismatch",
        }])
        self.assertEqual(verification[0]["payload"]["mismatch_count"], 1)
        self.assertTrue(verification[0]["payload"]["mismatch_evidence_complete"])
        tool_terminal = [
            event for event in adapter.test_events
            if event["event_kind"] == "tool_call" and event["marker"].startswith("close_")
        ]
        self.assertEqual([event["marker"] for event in tool_terminal], ["close_fail"])
        self.assertNotIn("rollback", json.dumps(verification).casefold())


class MutationGuardTest(unittest.TestCase):
    """The final workspace guard runs once after approval and before the trusted runner."""

    @staticmethod
    def _write_tool(target: Path, order: list[str]) -> L.ToolSpec:
        def run(_args):
            order.append("runner")
            target.write_text("mutated", encoding="utf-8")
            return "done"

        return L.ToolSpec("act", "file_write", "write", run, targets=lambda _args: (str(target),))

    def test_allow_then_guard_denial_closes_without_mutation(self):
        ws = tmp_workspace(self)
        target = ws / "guarded.txt"
        order: list[str] = []
        actions: list[policy.RequestedAction] = []
        adapter = make_adapter(
            engine(rules=[path_prefix_allow("file_write", str(ws), "r_allow")]),
            scripted_provider([tool_reply("act"), final_reply("done")]),
            tools={"act": self._write_tool(target, order)},
        )

        def guard(action):
            order.append("guard")
            actions.append(action)
            return {
                "status": "DENIED",
                "code": "DENIED_WORKSPACE_WRITER_BUSY",
                "retryable": True,
                "required_action": "wait for the current writer",
            }

        adapter.set_mutation_guard(guard)
        adapter.start_session(cwd=str(ws))
        adapter.start_turn("go")

        self.assertEqual(order, ["guard"])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].verb, "file_write")
        self.assertFalse(target.exists())
        declined = [e for e in adapter.test_events
                    if e["event_kind"] == "approval" and e["marker"] == "declined"]
        failed = [e for e in adapter.test_events
                  if e["event_kind"] == "tool_call" and e["marker"] == "close_fail"]
        self.assertEqual(declined[-1]["code"], "DENIED_WORKSPACE_WRITER_BUSY")
        self.assertEqual(failed[-1]["code"], "DENIED_WORKSPACE_WRITER_BUSY")

    def test_human_approval_then_guard_then_runner_order(self):
        ws = tmp_workspace(self)
        target = ws / "approved.txt"
        order: list[str] = []
        adapter = make_adapter(
            engine(),
            scripted_provider([tool_reply("act"), final_reply("done")]),
            tools={"act": self._write_tool(target, order)},
        )
        adapter.on_approval(lambda _request: order.append("human") or L.DECISION_APPROVED)
        adapter.set_mutation_guard(lambda _action: order.append("guard") or None)
        adapter.start_session(cwd=str(ws))
        adapter.start_turn("go")

        self.assertEqual(order, ["human", "guard", "runner"])
        self.assertTrue(target.is_file())

    def test_hard_policy_deny_never_calls_guard(self):
        ws = tmp_workspace(self)
        target = ws / "denied.txt"
        order: list[str] = []
        adapter = make_adapter(
            engine(protected=[str(ws)]),
            scripted_provider([tool_reply("act"), final_reply("done")]),
            tools={"act": self._write_tool(target, order)},
        )
        adapter.set_mutation_guard(lambda _action: order.append("guard") or None)
        adapter.start_session(cwd=str(ws))
        adapter.start_turn("go")

        self.assertEqual(order, [])
        self.assertFalse(target.exists())

    def test_nonmutating_read_does_not_call_mutation_guard(self):
        ws = tmp_workspace(self)
        target = ws / "read.txt"
        target.write_text("safe", encoding="utf-8")
        order: list[str] = []
        tool = L.ToolSpec(
            "read", "file_read", "read", lambda _args: order.append("runner") or "safe",
            targets=lambda _args: (str(target),),
        )
        adapter = make_adapter(
            engine(rules=[path_prefix_allow("file_read", str(ws), "r_read")]),
            scripted_provider([tool_reply("read"), final_reply("done")]),
            tools={"read": tool},
        )
        adapter.set_mutation_guard(lambda _action: order.append("guard") or None)
        adapter.start_session(cwd=str(ws))
        adapter.start_turn("read")

        self.assertEqual(order, ["runner"])


# --- 6. steer / interrupt ---------------------------------------------------------------------

class SteerInterruptTest(unittest.TestCase):
    """A provider that blocks between replies: steer() lands the injected message in a later provider
    call's messages; interrupt() => turn close_canceled + status CANCELED; steer with no active turn
    raises DENIED_NO_ACTIVE_TURN."""

    def test_steer_injects_into_a_later_provider_call(self):
        # A provider that blocks between replies so the test can steer while a call is in flight. The
        # steered message is drained at the top of the NEXT iteration and must appear in that call's
        # messages (steer = inject-next-iteration).
        eng = engine()
        gate1 = threading.Event()     # release the 1st (blocked) call
        gate2 = threading.Event()     # release the 2nd (blocked) call
        at_first = threading.Event()
        at_second = threading.Event()
        state = {"i": 0, "calls": []}

        def provider(messages, **opts):
            state["calls"].append([dict(m) for m in messages])
            idx = state["i"]
            state["i"] += 1
            if idx == 0:
                at_first.set()
                gate1.wait(2.0)       # block call #1 until the test steers
                return {"text": "not-json-1"}
            if idx == 1:
                at_second.set()
                gate2.wait(2.0)
                return {"text": final_reply("done")}
            return {"text": final_reply("done")}

        events: list = []
        adapter = L.LocalLLMAdapter(eng, chat_provider=provider, tools={},
                                    recorder=events.append, logger=lambda _m: None,
                                    id_factory=deterministic_ids(), max_turns=10)
        adapter.start_session()
        box: dict = {}
        t = threading.Thread(target=lambda: box.__setitem__("out", adapter.start_turn("go")), daemon=True)
        t.start()
        self.assertTrue(at_first.wait(3.0), "provider should reach call #1")
        steer_out = adapter.steer("turn-1", "STEERED-INSTRUCTION")
        self.assertEqual(steer_out["status"], "OK")
        gate1.set()                    # let call #1 return; the loop drains the steer, then calls again
        self.assertTrue(at_second.wait(3.0), "provider should reach call #2 with the steered message")
        gate2.set()
        t.join(5.0)
        self.assertFalse(t.is_alive())
        # The steered message landed in call #2's messages (drained at the next iteration top).
        second_call_users = [m["content"] for m in state["calls"][1] if m["role"] == "user"]
        self.assertIn("STEERED-INSTRUCTION", second_call_users)
        self.assertEqual(box["out"]["status"], "OK")

    def test_interrupt_cancels_turn(self):
        # A provider that blocks on a non-final reply; interrupt() while blocked => at the next iteration
        # top the loop sees the flag and closes close_canceled with status CANCELED (non-preemptive: the
        # in-flight call completes first).
        eng = engine()
        gate = threading.Event()
        at_call = threading.Event()
        state = {"i": 0}

        def provider(messages, **opts):
            idx = state["i"]
            state["i"] += 1
            if idx == 0:
                at_call.set()
                gate.wait(2.0)         # block; the test interrupts while we are here
                return {"text": "not-json-keep-going"}   # non-final -> loop continues to the next top
            return {"text": final_reply("never reached")}

        events: list = []
        adapter = L.LocalLLMAdapter(eng, chat_provider=provider, tools={},
                                    recorder=events.append, logger=lambda _m: None,
                                    id_factory=deterministic_ids(), max_turns=10)
        adapter.start_session()
        box: dict = {}
        t = threading.Thread(target=lambda: box.__setitem__("out", adapter.start_turn("go")), daemon=True)
        t.start()
        self.assertTrue(at_call.wait(3.0))
        adapter.interrupt("turn-1")
        gate.set()                     # release the blocked call; the loop then honors the interrupt
        t.join(5.0)
        self.assertFalse(t.is_alive(), "the turn thread should finish after interrupt")
        self.assertEqual(box["out"]["status"], "CANCELED")
        self.assertIn("close_canceled", [m for m, _c, _p in markers(events, "turn")])

    def test_steer_without_active_turn_raises(self):
        adapter = make_adapter(engine(), scripted_provider([final_reply("x")]), tools={})
        adapter.start_session()
        with self.assertRaises(L.LocalLLMAdapterError) as ctx:
            adapter.steer("no-turn", "hi")
        self.assertEqual(ctx.exception.code, L.DENIED_NO_ACTIVE_TURN)

    def test_interrupt_does_not_poison_next_turn(self):
        # The interrupt flag is PER-TURN: an interrupt with no turn in flight (or from a prior turn)
        # must not cancel the NEXT turn -- start_turn clears the flag on entry.
        adapter = make_adapter(engine(), scripted_provider([final_reply("fresh")]), tools={})
        adapter.start_session()
        adapter.interrupt("stale")
        out = adapter.start_turn("go")
        self.assertEqual(out["status"], "OK", out)
        self.assertEqual(out["final"], "fresh")


# --- 7. capabilities shims --------------------------------------------------------------------

class CapabilitiesShimsTest(unittest.TestCase):
    """capabilities() carries the exact shims (agent_type 'other', resume False, steer
    inject-next-iteration, subagent kaizen-simulated, orchestrate True); resume() raises
    DENIED_NO_RESUME."""

    def test_capabilities_exact(self):
        adapter = make_adapter(engine(), scripted_provider([final_reply("x")]), tools={}, max_turns=8)
        caps = adapter.capabilities()
        self.assertEqual(caps["engine"], "local-llm")
        self.assertEqual(caps["agent_type"], "other")
        self.assertFalse(caps["resume"])
        self.assertEqual(caps["steer"], "inject-next-iteration")
        self.assertEqual(caps["subagent"], "kaizen-simulated")
        self.assertTrue(caps["orchestrate"])
        self.assertEqual(caps["sandbox_mode"], "none-kaizen-gated")
        self.assertEqual(caps["max_turns"], 8)

    def test_resume_refuses(self):
        adapter = make_adapter(engine(), scripted_provider([final_reply("x")]), tools={})
        with self.assertRaises(L.LocalLLMAdapterError) as ctx:
            adapter.resume("anything")
        self.assertEqual(ctx.exception.code, L.DENIED_NO_RESUME)


class MultiTurnContractTest(unittest.TestCase):
    def test_common_turn_result_history_and_clean_close(self):
        provider = scripted_provider([final_reply("first"), final_reply("second")])
        adapter = make_adapter(engine(), provider, tools={})
        adapter.start_session()

        first = adapter.run_turn("one")
        second = adapter.run_turn("two")

        self.assertIsInstance(first, TurnResult)
        self.assertEqual(first.as_dict(), {
            "status": "OK", "vendor_turn_id": "turn-1", "final_text": "first",
            "error_code": None, "fatal": False,
        })
        self.assertEqual(second.status, "OK")
        self.assertEqual(second.vendor_turn_id, "turn-2")
        self.assertEqual(
            [(m["role"], m["content"]) for m in provider.state["calls"][1][1:]],
            [("user", "one"), ("assistant", "first"), ("user", "two")],
        )
        self.assertEqual(
            [(m["role"], m["content"]) for m in adapter.conversation_history],
            [("user", "one"), ("assistant", "first"), ("user", "two"), ("assistant", "second")],
        )
        self.assertEqual(adapter.close()["status"], "OK")
        with self.assertRaises(L.LocalLLMAdapterError) as caught:
            adapter.run_turn("three")
        self.assertEqual(caught.exception.code, L.DENIED_CLOSED)

    def test_failed_turn_does_not_commit_an_orphan_user_message(self):
        calls = 0

        def provider(messages, **_options):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("fixture backend failure")
            return {"text": final_reply(f"answer-{calls}")}

        adapter = make_adapter(engine(), provider, tools={})
        adapter.start_session()
        self.assertEqual(adapter.run_turn("accepted-one").status, "OK")
        self.assertEqual(adapter.run_turn("rejected-two").status, "FAILED")
        self.assertEqual(adapter.run_turn("accepted-three").status, "OK")
        self.assertEqual(
            [(message["role"], message["content"]) for message in adapter.conversation_history],
            [("user", "accepted-one"), ("assistant", "answer-1"),
             ("user", "accepted-three"), ("assistant", "answer-3")],
        )

    def test_history_is_capped_at_64_complete_messages(self):
        adapter = make_adapter(engine(), scripted_provider([final_reply("latest-answer")]), tools={})
        adapter.seed_history([
            {"role": role, "content": f"{role}-{pair}"}
            for pair in range(32)
            for role in ("user", "assistant")
        ])
        adapter.start_session()
        self.assertEqual(adapter.run_turn("latest-user").status, "OK")
        history = adapter.conversation_history
        self.assertEqual(len(history), 64)
        self.assertEqual(history[0], {"role": "user", "content": "user-1"})
        self.assertEqual(history[-2:], (
            {"role": "user", "content": "latest-user"},
            {"role": "assistant", "content": "latest-answer"},
        ))

    def test_close_claims_logical_closure_before_physical_gateway_teardown(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingGateway:
            def cancel_active_processes(self, _timeout):
                entered.set()
                release.wait(5.0)
                return {"termination_proven": True}

            def close(self):
                return True

        adapter = make_adapter(engine(), scripted_provider([final_reply("must not run")]), tools={})
        adapter.start_session()
        adapter._gateway = BlockingGateway()
        result: dict[str, object] = {}
        closer = threading.Thread(target=lambda: result.update(adapter.close()), daemon=True)
        closer.start()
        self.assertTrue(entered.wait(2.0), "gateway teardown did not begin")
        with self.assertRaises(L.LocalLLMAdapterError) as denied:
            adapter.run_turn("raced turn")
        self.assertEqual(denied.exception.code, L.DENIED_CLOSED)
        release.set()
        closer.join(5.0)
        self.assertFalse(closer.is_alive(), "close did not finish")
        self.assertEqual(result, {
            "status": "OK", "closed": True, "already_closed": False, "termination_proven": True,
        })


# --- 8. subagent inheritance ------------------------------------------------------------------

class SubagentSimTest(unittest.TestCase):
    """A Kaizen-simulated subagent: subagent open/close_ok emitted, the child turn runs scripted, and a
    child git-push intent is denied via the SAME engine (inheritance proof)."""

    def test_subagent_runs_and_inherits_decider(self):
        ws = tmp_workspace(self)
        marker = ws / "CHILD_MARKER"
        eng = engine()
        # Child first tries a git push (denied by the inherited engine), then finals.
        provider = scripted_provider([
            tool_reply("run_command", command="git push origin main"),
            final_reply("child done"),
        ])
        adapter = make_adapter(eng, provider, tools={"run_command": marker_writer_tool(marker)})
        adapter.start_session(cwd=str(ws))
        out = adapter.spawn_subagent("do child work")

        self.assertEqual(out["status"], "OK")
        self.assertIn("subagent_id", out)
        # subagent open + close_ok emitted.
        subs = markers(adapter.test_events, "subagent")
        self.assertIn("open", [m for m, _c, _p in subs])
        self.assertIn("close_ok", [m for m, _c, _p in subs])
        # The child's git-push intent was denied by the SAME engine (inheritance): marker absent + a
        # declined approval with INV_GIT_PUSH.
        self.assertFalse(marker.exists())
        declined = [e for e in adapter.test_events
                    if e["event_kind"] == "approval" and e["marker"] == "declined"]
        self.assertTrue(declined)
        self.assertEqual(declined[0]["payload"]["invariant_id"], policy.INV_GIT_PUSH)


# --- 9. agent_type='other' --------------------------------------------------------------------

class AgentTypeOtherTest(unittest.TestCase):
    """The session-open recorder event payload carries agent_type == 'other' (§A.1: local models have no
    native session type)."""

    def test_session_open_payload_agent_type_other(self):
        adapter = make_adapter(engine(), scripted_provider([final_reply("x")]), tools={}, model="mymodel")
        adapter.start_session()
        sess = [e for e in adapter.test_events if e["event_kind"] == "session" and e["marker"] == "open"]
        self.assertTrue(sess)
        self.assertEqual(sess[0]["payload"]["agent_type"], "other")
        self.assertEqual(sess[0]["payload"]["model"], "mymodel")


# --- 10. stdout-pristine ----------------------------------------------------------------------

class StdoutPristineTest(unittest.TestCase):
    """The adapter never prints to stdout: capture stdout during a full turn (with a tool + gate) and
    assert it stayed empty (all logging goes to the injected logger, §A.3)."""

    def test_no_stdout_during_full_turn(self):
        ws = tmp_workspace(self)
        eng = engine(rules=[path_prefix_allow("file_write", str(ws), "r_w")])
        provider = scripted_provider([
            tool_reply("write_file", path=str(ws / "o.txt"), content="z"),
            final_reply("done"),
        ])
        adapter = make_adapter(eng, provider, tools=L.build_default_tools(ws))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            adapter.start_session(cwd=str(ws))
            adapter.start_turn("write it")
        self.assertEqual(buffer.getvalue(), "", f"adapter wrote to stdout: {buffer.getvalue()!r}")


# --- 11. untrusted output verbatim ------------------------------------------------------------

class UntrustedOutputTest(unittest.TestCase):
    """A final answer containing shell/template metacharacters is returned VERBATIM and never raises
    (nothing templates/interpolates model text)."""

    def test_metacharacters_returned_verbatim(self):
        payload = "{braces} $(subst) `ticks` %VARS% \\backslash\\ \"quotes\""
        eng = engine()
        provider = scripted_provider([final_reply(payload)])
        adapter = make_adapter(eng, provider, tools={})
        adapter.start_session()
        out = adapter.start_turn("give me the string")
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["final"], payload)  # byte-for-byte, no substitution


# --- 12. every-exit-path-terminal -------------------------------------------------------------

class EveryExitPathTerminalTest(unittest.TestCase):
    """Every exit path emits exactly one terminal turn event, and the whole emitted vocabulary stays
    within the codex-parity span grammar (§A.3)."""

    def test_backend_error_is_one_terminal_close_fail(self):
        eng = engine()

        def provider(messages, **opts):
            raise RuntimeError("boom")

        adapter = make_adapter(eng, provider, tools={})
        adapter.start_session()
        out = adapter.start_turn("go")
        self.assertEqual(out["status"], "FAILED")
        self.assertEqual(out["code"], L.BACKEND_ERROR)
        terminal = [e for e in adapter.test_events
                    if e["event_kind"] == "turn" and e["marker"] in ("close_ok", "close_fail", "close_canceled")]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["marker"], "close_fail")
        self.assertEqual(terminal[0]["code"], L.BACKEND_ERROR)

    def test_kill_midturn_is_one_close_canceled_idempotent(self):
        eng = engine()
        started = threading.Event()
        release = threading.Event()

        def provider(messages, **opts):
            started.set()
            release.wait(2.0)  # block so kill() lands mid-turn
            return {"text": final_reply("late")}

        events: list = []
        adapter = L.LocalLLMAdapter(eng, chat_provider=provider, tools={},
                                    recorder=events.append, logger=lambda _m: None,
                                    id_factory=deterministic_ids(), max_turns=5)
        adapter.start_session()
        box: dict = {}
        t = threading.Thread(target=lambda: box.__setitem__("out", adapter.start_turn("go")), daemon=True)
        t.start()
        self.assertTrue(started.wait(3.0))
        adapter.kill()          # close_canceled from kill()'s thread
        release.set()           # release the provider; the loop then closes idempotently (no-op)
        t.join(5.0)
        self.assertFalse(t.is_alive(), "killed turn thread did not terminate")
        # Exactly one terminal turn marker, and it is close_canceled (kill won the race).
        terminal = [e for e in events
                    if e["event_kind"] == "turn" and e["marker"] in ("close_ok", "close_fail", "close_canceled")]
        self.assertEqual(len(terminal), 1, [e["marker"] for e in terminal])
        self.assertEqual(terminal[0]["marker"], "close_canceled")
        # kill() is idempotent.
        self.assertEqual(adapter.kill()["status"], "OK")

    def test_normal_turn_is_one_close_ok(self):
        adapter = make_adapter(engine(), scripted_provider([final_reply("ok")]), tools={})
        adapter.start_session()
        adapter.start_turn("go")
        terminal = [e for e in adapter.test_events
                    if e["event_kind"] == "turn" and e["marker"] in ("close_ok", "close_fail", "close_canceled")]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["marker"], "close_ok")

    def test_start_turn_after_kill_refuses(self):
        # kill() is PERMANENT (interrupt is per-turn): a killed adapter refuses new turns with
        # DENIED_KILLED and, because the refusal precedes the span open, leaves no dangling turn span.
        adapter = make_adapter(engine(), scripted_provider([final_reply("x")]), tools={})
        adapter.start_session()
        self.assertEqual(adapter.kill()["status"], "OK")
        with self.assertRaises(L.LocalLLMAdapterError) as ctx:
            adapter.start_turn("go")
        self.assertEqual(ctx.exception.code, L.DENIED_KILLED)
        self.assertEqual([e for e in adapter.test_events if e["event_kind"] == "turn"], [],
                         "a refused turn must open no span")

    def test_full_vocabulary_sweep(self):
        # Drive a run that exercises many event kinds, then assert EVERY emitted (event_kind, marker) is
        # in the codex-parity vocabulary. Cover: session open, turn open/close_ok, tool_call open/
        # close_ok/close_fail, approval open/resolved/declined, subagent open/close_ok.
        ws = tmp_workspace(self)
        eng = engine(rules=[path_prefix_allow("file_write", str(ws), "r_w")])
        # allow a write; deny a git push (declined + tool_call close_fail); ask a no-rule tool (approval
        # open + resolved via callback); then final.
        ask_tool = L.ToolSpec("ask_tool", "file_read", "ask-defaulting", lambda a: "ok",
                              targets=lambda a: ())
        tools = {
            "write_file": L.build_default_tools(ws)["write_file"],
            "run_command": marker_writer_tool(ws / "M"),
            "ask_tool": ask_tool,
        }
        provider = scripted_provider([
            tool_reply("write_file", path=str(ws / "o.txt"), content="z"),  # allow -> resolved + ok
            tool_reply("run_command", command="git push origin main"),      # deny -> declined + fail
            tool_reply("ask_tool"),                                         # ask -> open + resolved
            final_reply("all done"),
        ])
        adapter = make_adapter(eng, provider, tools=tools)
        adapter.on_approval(lambda _req: L.DECISION_APPROVED)
        adapter.start_session(cwd=str(ws))
        adapter.spawn_subagent("noop child")  # emits subagent open/close_ok around a child turn
        # Reset the provider for the parent turn (spawn consumed replies); use a fresh adapter run below.

        # Run the parent turn on a second adapter sharing the recorder to accumulate the full vocabulary.
        provider2 = scripted_provider([
            tool_reply("write_file", path=str(ws / "o2.txt"), content="z"),
            tool_reply("run_command", command="git push origin main"),
            tool_reply("ask_tool"),
            final_reply("all done"),
        ])
        adapter2 = L.LocalLLMAdapter(eng, chat_provider=provider2, tools=tools,
                                     recorder=adapter.test_events.append, logger=lambda _m: None,
                                     id_factory=deterministic_ids())
        adapter2.on_approval(lambda _req: L.DECISION_APPROVED)
        adapter2.start_session(cwd=str(ws))
        adapter2.start_turn("do many things")

        emitted = {(e["event_kind"], e["marker"]) for e in adapter.test_events}
        for kind, marker in emitted:
            self.assertIn(kind, L.EVENT_VOCABULARY, f"unknown event_kind {kind!r}")
            self.assertIn(marker, L.EVENT_VOCABULARY[kind],
                          f"marker {marker!r} not in vocabulary for {kind!r}")
        # Sanity: the run actually exercised the breadth we claim.
        self.assertTrue({("approval", "resolved"), ("approval", "declined"), ("approval", "open"),
                         ("tool_call", "close_ok"), ("tool_call", "close_fail"),
                         ("subagent", "open"), ("subagent", "close_ok")} <= emitted)


if __name__ == "__main__":
    unittest.main()
