"""Codex app-server adapter (v8 M4): each exit criterion asserted against a HERMETIC fake.

The real ``codex`` binary is NEVER invoked here -- every test drives ``fake_codex_app_server.py`` as a
REAL subprocess (so spawn/ownership/reader paths are exercised) via children.spawn_owned. PolicyEngine
is constructed directly in-process (no DB -- test_policy.py owns the DB legs); the recorder is a pure
in-memory list. Tests must pass on Windows (primary) and be POSIX-tolerant.

Exit criteria -> proving test:
- handshake + version gate ........... VersionGateTest
- assert_required_methods fixture .... MethodSurfaceTest
- approval per-item approve + deny ... ApprovalInterceptionTest
- interrupt closes spans ............. InterruptTest
- second-controller lease ............ SecondControllerTest
- side-channel deny/wrap matrix ...... SideChannelMatrixTest
- Windows-unsandboxed ................ WindowsUnsandboxedTest
- unknown server request survives .... UnknownServerRequestTest
- stdout-pristine .................... StdoutPristineTest
- every-exit-path-terminal ........... TerminalOnKillTest
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
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

from kaizen_components.orchestration import children, policy  # noqa: E402
from kaizen_components.orchestration.adapters import codex as C  # noqa: E402

FAKE = str(Path(__file__).resolve().parent / "fake_codex_app_server.py")
COMPATIBLE_SCHEMA = {
    "methods": set(C.REQUIRED_METHODS),
    "missing_methods": [],
    "bounded_read_access": True,
    "pre_tool_command_hook": True,
    "read_only_fields": ["access", "networkAccess", "type"],
    "workspace_write_fields": ["networkAccess", "readOnlyAccess", "type", "writableRoots"],
}


# --- helpers ----------------------------------------------------------------------------------

def fake_cmd(version: str = "0.130.0", scenario: str = "plain-turn") -> list[str]:
    return [sys.executable, FAKE, "--version-string", version, "--scenario", scenario]


def engine(rules=(), protected=()) -> policy.PolicyEngine:
    # vendor=[] so the engine does not resolve the real ~/.claude / ~/.codex config paths in a unit test.
    return policy.PolicyEngine(list(protected), list(rules), [])


def allow_all_exec() -> policy.PolicyEngine:
    # A permissive engine: allow any exec (used to prove the approve path drives an "approved" reply).
    return engine(rules=[{
        "id": "r_allow_any", "rule_type": "allow", "verb": "exec",
        "match_kind": "path_prefix", "pattern": "", "engine": None, "enabled": True,
    }])


def make_adapter(eng: policy.PolicyEngine, *, version: str = "0.130.0", scenario: str = "plain-turn",
                 lease_path: Path | None = None, recorder=None) -> C.CodexAdapter:
    events: list = recorder if recorder is not None else []
    adapter = C.CodexAdapter(
        eng,
        cmd=fake_cmd(version, scenario),
        recorder=events.append,
        logger=lambda _msg: None,  # silence the adapter's stderr logging during tests
        lease_path=lease_path,
        request_timeout=45.0,
        approval_timeout=45.0,
    )
    adapter.bind_session("as_test_codex")
    adapter.policy_engine.record_ask = lambda decision, _session: {
        "status": "OK", "id": f"ap_{decision.correlation_hash[:12]}", "created": True,
    }
    adapter.test_events = events  # type: ignore[attr-defined]
    return adapter


def snapshot(mode: str = "ask") -> policy.PolicySnapshot:
    return policy.build_policy_snapshot(
        "codex",
        mode,
        str(REPO_ROOT),
        [str(Path(REPO_ROOT) / "AI" / "work")],
        [],
        protected_paths=[],
        vendor_config_paths=[],
    )


def make_h2_adapter(
    *,
    mode: str = "ask",
    scenario: str = "plain-turn",
    auth_mode: str = "subscription",
    env: dict[str, str] | None = None,
    approval_timeout: float = 2.0,
    turn_timeout: float = 2.0,
    logger=None,
) -> C.CodexAdapter:
    snap = snapshot(mode)
    events: list[dict] = []
    adapter = C.CodexAdapter(
        snap.build_engine(),
        cmd=fake_cmd(scenario=scenario),
        recorder=events.append,
        logger=logger or (lambda _message: None),
        env=env,
        request_timeout=2.0,
        approval_timeout=approval_timeout,
        turn_timeout=turn_timeout,
        schema_capabilities=COMPATIBLE_SCHEMA,
    )
    adapter.test_events = events  # type: ignore[attr-defined]
    profile = {
        "model": "gpt-fake",
        "reasoning_effort": "medium",
        "permission_mode": mode,
        "auth_mode": auth_mode,
    }
    adapter.open(profile, snap)
    adapter.bind_session("as_test_codex_h2")
    adapter.policy_engine.record_ask = lambda decision, _session: {
        "status": "OK", "id": f"ap_{decision.correlation_hash[:12]}", "created": True,
    }
    return adapter


def markers(events: list, kind: str) -> list:
    return [(e["marker"], e.get("correlation_id"), e.get("payload", {})) for e in events if e["event_kind"] == kind]


# Generous default so a loaded machine (real-subprocess spawn + JSON-RPC round-trips) never trips a
# wait before the fake finishes its turn -- the assertions still fail fast on a genuine break because
# the predicate flips as soon as the terminal event lands.
def wait_for(predicate, timeout: float = 45.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --- allow-permissive engine (path_prefix allow on exec must NOT allow an opaque command; the policy
# contract forbids widening an exec via path_prefix). For the "approve" leg we need a genuine allow, so
# use an exact_command allow keyed to the fake's scenario command normal-form. ----------------------

def exact_allow_for(command_argv: list) -> policy.PolicyEngine:
    normal = " ".join(" ".join(str(p) for p in command_argv).split())
    return engine(rules=[{
        "id": "r_exact_allow", "rule_type": "allow", "verb": "exec",
        "match_kind": "exact_command", "pattern": normal, "engine": None, "enabled": True,
    }])


# The benign command the fake's approval/plain scenarios carry (adapter joins argv with spaces).
FAKE_APPROVAL_CMD = ["powershell", "-Command", "Set-Content probe.txt hi"]


class VersionGateTest(unittest.TestCase):
    """Handshake + version floor. @0.130.0 orchestrates; @0.129.0 => observe-only and every orchestrate
    verb refuses DENIED_CODEX_VERSION while observe APIs still work."""

    def test_at_floor_orchestrates(self):
        adapter = make_adapter(allow_all_exec(), version="0.130.0")
        try:
            ref = adapter.start_session(cwd=str(REPO_ROOT))
            self.assertFalse(ref["observe_only"], ref)
            self.assertEqual(adapter.server_version, (0, 130, 0))
            self.assertTrue(adapter.capabilities()["orchestrate"])
            # An orchestrate verb succeeds (thread starts).
            out = adapter.start_thread(cwd=str(REPO_ROOT))
            self.assertEqual(out["status"], "OK")
            self.assertTrue(out["thread_id"])
        finally:
            adapter.kill()

    def test_below_floor_is_observe_only_and_refuses_orchestrate(self):
        adapter = make_adapter(engine(), version="0.129.0")
        try:
            ref = adapter.start_session()
            self.assertTrue(ref["observe_only"], ref)
            self.assertFalse(adapter.capabilities()["orchestrate"])
            # Observe API still works.
            caps = adapter.capabilities()
            self.assertEqual(caps["engine"], "codex")
            # Every orchestrate verb refuses with DENIED_CODEX_VERSION.
            for call in (
                lambda: adapter.start_thread(),
                lambda: adapter.start_turn("hi"),
                lambda: adapter.interrupt("t"),
                lambda: adapter.exec_wrapped(["echo", "x"], sandbox_policy={"type": "read-only"}),
                lambda: adapter.resume("thread-x"),
            ):
                with self.assertRaises(C.CodexAdapterError) as ctx:
                    call()
                self.assertEqual(ctx.exception.code, C.DENIED_CODEX_VERSION)
        finally:
            adapter.kill()

    def test_unparseable_user_agent_is_observe_only(self):
        # An empty/garbage version string -> no semver parse -> observe-only.
        self.assertIsNone(C.parse_user_agent_version("no-version-here"))
        self.assertIsNone(C.parse_user_agent_version(None))
        self.assertEqual(C.parse_user_agent_version("x/0.130.0 y"), (0, 130, 0))


class MethodSurfaceTest(unittest.TestCase):
    """assert_required_methods: a fixture surface with every required method passes; a missing one
    raises CodexCapabilityError. Unit-tested against a fixture dict (never the live dump)."""

    def test_present_surface_passes(self):
        surface = C.probe_method_surface({"methods": [{"method": m} for m in C.REQUIRED_METHODS]})
        self.assertEqual(surface, set(C.REQUIRED_METHODS))
        C.assert_required_methods(surface)  # no raise

    def test_missing_method_fails(self):
        partial = set(C.REQUIRED_METHODS) - {"thread/shellCommand"}
        surface = C.probe_method_surface({"methods": [{"method": m} for m in partial]})
        with self.assertRaises(C.CodexCapabilityError) as ctx:
            C.assert_required_methods(surface)
        self.assertIn("thread/shellCommand", ctx.exception.missing)

    def test_probe_surface_bare_mapping_shape(self):
        # A bare {method-name: spec} mapping is also parsed.
        surface = C.probe_method_surface({m: {} for m in C.REQUIRED_METHODS})
        self.assertEqual(surface, set(C.REQUIRED_METHODS))


class ApprovalInterceptionTest(unittest.TestCase):
    """Per-item approval interception, approve AND deny. Also: a policy deny (git push in the approval
    params) => denied with invariant_id INV_GIT_PUSH."""

    def _run_turn_and_wait(self, adapter: C.CodexAdapter):
        adapter.start_session(cwd=str(REPO_ROOT))
        adapter.start_thread(cwd=str(REPO_ROOT))
        adapter.start_turn("do the thing")
        # Wait until the turn closes (approval answered -> item/completed -> turn/completed).
        self.assertTrue(wait_for(
            lambda: any(e["event_kind"] == "turn" and e["marker"] in ("close_ok", "close_fail")
                        for e in adapter.test_events),
        ), "turn did not complete")

    def _wire_echo(self, adapter: C.CodexAdapter) -> dict:
        """The fake echoes the WIRE decision it received into the item payload; the adapter's item-close
        event carries it through. This is the wire-vocabulary regression guard: the live server rejects
        approved/denied (P-B), so the fake fails the item unless accept/decline arrived."""
        closes = [e for e in adapter.test_events
                  if e["event_kind"] == "subagent" and e["marker"].startswith("close")]
        self.assertTrue(closes, "an item close event was recorded")
        return closes[-1]["payload"]

    @staticmethod
    def _busy(_action):
        return {
            "status": "DENIED",
            "code": "DENIED_WORKSPACE_WRITER_BUSY",
            "retryable": True,
            "required_action": "wait for the current writer",
        }

    def test_allow_rule_yields_approved(self):
        adapter = make_adapter(exact_allow_for(FAKE_APPROVAL_CMD), scenario="approval")
        try:
            self._run_turn_and_wait(adapter)
        finally:
            adapter.kill()
        appr = [e for e in adapter.test_events if e["event_kind"] == "approval"]
        self.assertTrue(appr, "an approval event was recorded")
        self.assertEqual(appr[0]["marker"], "resolved")
        self.assertEqual(appr[0]["payload"]["decision"], C.DECISION_APPROVED)
        self.assertEqual(appr[0]["payload"]["rule_id"], "r_exact_allow")
        echo = self._wire_echo(adapter)
        self.assertEqual(echo["received_decision"], C.WIRE_ACCEPT)  # vendor verb on the wire
        self.assertTrue(echo["command_run"])

    def test_default_ask_resolved_deny_via_callback(self):
        # Default engine (no rule) => ask; the on_approval resolver returns deny => fake receives decline.
        adapter = make_adapter(engine(), scenario="approval")
        adapter.on_approval(lambda _req: C.DECISION_DENIED)
        try:
            self._run_turn_and_wait(adapter)
        finally:
            adapter.kill()
        appr = [e for e in adapter.test_events if e["event_kind"] == "approval"]
        self.assertTrue(appr)
        self.assertEqual(appr[-1]["marker"], "declined")
        self.assertEqual(appr[-1]["payload"]["decision"], C.DECISION_DENIED)
        echo = self._wire_echo(adapter)
        self.assertEqual(echo["received_decision"], C.WIRE_DECLINE)  # vendor verb on the wire
        self.assertFalse(echo["command_run"])

    def test_default_ask_resolved_approve_via_callback(self):
        adapter = make_adapter(engine(), scenario="approval")
        adapter.on_approval(lambda _req: C.DECISION_APPROVED)
        try:
            self._run_turn_and_wait(adapter)
        finally:
            adapter.kill()
        appr = [e for e in adapter.test_events if e["event_kind"] == "approval"]
        self.assertTrue(appr)
        self.assertEqual(appr[-1]["payload"]["decision"], C.DECISION_APPROVED)

    def test_broker_delegation_is_single_authority_without_adapter_approval_events(self):
        adapter = make_adapter(engine(), scenario="approval")
        direct_c4 = mock.Mock(side_effect=AssertionError("record_ask must be broker-owned"))
        legacy = mock.Mock(side_effect=AssertionError("legacy resolver must be skipped"))
        broker = mock.Mock(return_value=C.DECISION_APPROVED)
        adapter.policy_engine.record_ask = direct_c4
        adapter.on_approval(legacy)
        adapter.set_approval_broker(broker)
        try:
            self._run_turn_and_wait(adapter)
        finally:
            adapter.kill()

        broker.assert_called_once()
        direct_c4.assert_not_called()
        legacy.assert_not_called()
        self.assertFalse(any(event["event_kind"] == "approval" for event in adapter.test_events))
        echo = self._wire_echo(adapter)
        self.assertEqual(echo["received_decision"], C.WIRE_ACCEPT)

    def test_malformed_broker_result_denies_fail_closed(self):
        adapter = make_adapter(engine(), scenario="approval")
        adapter.set_approval_broker(lambda _request: {"decision": C.DECISION_APPROVED})  # type: ignore[arg-type]
        try:
            self._run_turn_and_wait(adapter)
        finally:
            adapter.kill()

        self.assertFalse(any(event["event_kind"] == "approval" for event in adapter.test_events))
        self.assertEqual(self._wire_echo(adapter)["received_decision"], C.WIRE_DECLINE)

    def test_policy_allow_then_guard_denial_returns_vendor_decline(self):
        actions: list[policy.RequestedAction] = []
        adapter = make_adapter(exact_allow_for(FAKE_APPROVAL_CMD), scenario="approval")

        def guard(action):
            actions.append(action)
            return self._busy(action)

        adapter.set_mutation_guard(guard)
        try:
            self._run_turn_and_wait(adapter)
        finally:
            adapter.kill()

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].verb, "exec")
        echo = self._wire_echo(adapter)
        self.assertEqual(echo["received_decision"], C.WIRE_DECLINE)
        self.assertFalse(echo["command_run"])
        declined = [e for e in adapter.test_events
                    if e["event_kind"] == "approval" and e["marker"] == "declined"]
        failed = [e for e in adapter.test_events
                  if e["event_kind"] == "tool_call" and e["marker"] == "close_fail"]
        self.assertEqual(declined[-1]["code"], "DENIED_WORKSPACE_WRITER_BUSY")
        self.assertEqual(failed[-1]["code"], "DENIED_WORKSPACE_WRITER_BUSY")

    def test_human_approval_precedes_single_guard_pass(self):
        order: list[str] = []
        adapter = make_adapter(engine(), scenario="approval")
        adapter.on_approval(lambda _request: order.append("human") or C.DECISION_APPROVED)
        adapter.set_mutation_guard(lambda _action: order.append("guard") or None)
        try:
            self._run_turn_and_wait(adapter)
        finally:
            adapter.kill()

        self.assertEqual(order, ["human", "guard"])
        echo = self._wire_echo(adapter)
        self.assertEqual(echo["received_decision"], C.WIRE_ACCEPT)
        self.assertTrue(echo["command_run"])

    def test_policy_deny_git_push_yields_denied_with_invariant(self):
        # The 'deny' scenario carries a git-push command in the approval params -> INV_GIT_PUSH.
        calls: list[str] = []
        adapter = make_adapter(engine(), scenario="deny")
        adapter.set_mutation_guard(lambda _action: calls.append("guard") or None)
        try:
            self._run_turn_and_wait(adapter)
        finally:
            adapter.kill()
        appr = [e for e in adapter.test_events if e["event_kind"] == "approval"]
        self.assertTrue(appr)
        self.assertEqual(appr[0]["marker"], "declined")
        self.assertEqual(appr[0]["payload"]["decision"], C.DECISION_DENIED)
        self.assertEqual(appr[0]["payload"]["invariant_id"], policy.INV_GIT_PUSH)
        echo = self._wire_echo(adapter)
        self.assertEqual(echo["received_decision"], C.WIRE_DECLINE)
        self.assertFalse(echo["command_run"])
        self.assertEqual(calls, [])

    def test_no_approval_callback_fails_closed_to_deny(self):
        # ask with no on_approval resolver registered => fail-closed deny.
        adapter = make_adapter(engine(), scenario="approval")
        try:
            self._run_turn_and_wait(adapter)
        finally:
            adapter.kill()
        appr = [e for e in adapter.test_events if e["event_kind"] == "approval"]
        self.assertTrue(appr)
        self.assertEqual(appr[-1]["payload"]["decision"], C.DECISION_DENIED)

    def test_two_items_two_independent_decisions(self):
        # Two approval items in one turn => two independent decide() calls. Drive two RequestedActions
        # directly through the engine and assert distinct dedupe keys + independent Decision objects
        # (per-item gating; the fake raises one approval per turn, so the multiplicity is proven at the
        # chokepoint the adapter funnels through).
        eng = engine()
        a1 = policy.RequestedAction(policy.Actor("codex", "s", 1, "tt"), "exec",
                                    command="powershell -Command Set-Content a.txt 1", raw={})
        a2 = policy.RequestedAction(policy.Actor("codex", "s", 1, "tt"), "exec",
                                    command="powershell -Command Set-Content b.txt 2", raw={})
        d1, d2 = eng.decide(a1, 1), eng.decide(a2, 1)
        self.assertNotEqual(a1.dedupe_key(), a2.dedupe_key())
        self.assertIsNot(d1, d2)


class InterruptTest(unittest.TestCase):
    """interrupt-hang scenario: interrupt(turn) => recorder shows the turn close_canceled AND a
    synthesized close for the still-open child item span; adapter returns to idle (P-B 2e caveat)."""

    def test_interrupt_closes_turn_and_synthesizes_item_close(self):
        adapter = make_adapter(engine(), scenario="interrupt-hang")
        try:
            adapter.start_session(cwd=str(REPO_ROOT))
            adapter.start_thread(cwd=str(REPO_ROOT))
            out = adapter.start_turn("hang please")
            turn_id = out["turn_id"]
            # Wait until the item span is open (item/started(commandExecution) processed).
            self.assertTrue(wait_for(lambda: any(
                e["event_kind"] == "subagent" and e["marker"] == "open" for e in adapter.test_events)),
                "child item span should open before interrupt")
            adapter.interrupt(turn_id)
            turns = markers(adapter.test_events, "turn")
            subs = markers(adapter.test_events, "subagent")
            self.assertIn("open", [m for m, _c, _p in subs])
            # The open child item span was synthesized-closed as close_canceled.
            self.assertIn("close_canceled", [m for m, _c, _p in subs])
            self.assertTrue(any(p.get("synthesized") for m, _c, p in subs if m == "close_canceled"))
            # The turn closed close_canceled.
            self.assertIn("close_canceled", [m for m, _c, _p in turns])
            # Idle: no open item spans remain and no active turn.
            self.assertEqual(adapter._open_items, set())
            self.assertIsNone(adapter._active_turn)
        finally:
            adapter.kill()


class SecondControllerTest(unittest.TestCase):
    """Second-controller lease (P-B 2c: no server-side lock; the orchestrator arbitrates). A lease file
    naming a LIVE pid + FOREIGN nonce => resume refuses DENIED_THREAD_LEASED; a DEAD pid => reclaims."""

    def _lease_path(self) -> Path:
        work = Path(REPO_ROOT) / "AI" / "work"
        work.mkdir(parents=True, exist_ok=True)
        d = Path(tempfile.mkdtemp(prefix="codex-lease-", dir=work))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d / "codex-leases.json"

    def test_live_foreign_owner_refuses_resume(self):
        lease = self._lease_path()
        # A live pid (THIS process) + a nonce that is not the adapter's.
        lease.write_text(json.dumps({"thread-live": {"pid": os.getpid(), "nonce": "foreign-nonce"}}),
                         encoding="utf-8")
        adapter = make_adapter(engine(), lease_path=lease)
        try:
            adapter.start_session()
            with self.assertRaises(C.CodexAdapterError) as ctx:
                adapter.resume("thread-live")
            self.assertEqual(ctx.exception.code, C.DENIED_THREAD_LEASED)
        finally:
            adapter.kill()

    def test_competing_acquires_are_atomic_and_exactly_one_owner_wins(self):
        lease = self._lease_path()
        registries = [C._LeaseRegistry(lease), C._LeaseRegistry(lease)]
        barrier = threading.Barrier(2)
        results: list[tuple[str, tuple[dict | None, bool]]] = []
        failures: list[BaseException] = []

        def acquire(index: int) -> None:
            try:
                nonce = f"owner-{index}"
                barrier.wait()
                results.append((nonce, registries[index].acquire("thread-race", os.getpid(), nonce)))
            except BaseException as error:  # surface worker failures on the main test thread
                failures.append(error)

        workers = [threading.Thread(target=acquire, args=(index,)) for index in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5.0)
            self.assertFalse(worker.is_alive(), "lease contender did not finish")
        if failures:
            raise failures[0]

        winners = [(nonce, outcome) for nonce, outcome in results if outcome[0] is None]
        refused = [(nonce, outcome) for nonce, outcome in results if outcome[0] is not None]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(refused), 1, results)
        stored = json.loads(lease.read_text(encoding="utf-8"))["thread-race"]
        self.assertEqual(stored, {"pid": os.getpid(), "nonce": winners[0][0]})
        self.assertEqual(refused[0][1][0], stored)

    def test_dead_owner_is_reclaimed(self):
        lease = self._lease_path()
        # A dead pid (999999) -> the lease is reclaimable.
        lease.write_text(json.dumps({"thread-dead": {"pid": 999999, "nonce": "old-nonce"}}),
                         encoding="utf-8")
        adapter = make_adapter(engine(), lease_path=lease)
        try:
            adapter.start_session()
            out = adapter.resume("thread-dead")
            self.assertEqual(out["status"], "OK")
            self.assertTrue(out["reclaimed"])
            # The lease is now held by this adapter's pid.
            data = json.loads(lease.read_text(encoding="utf-8"))
            self.assertEqual(data["thread-dead"]["pid"], os.getpid())
        finally:
            adapter.kill()

    def test_lease_released_on_kill(self):
        lease = self._lease_path()
        adapter = make_adapter(engine(), lease_path=lease)
        adapter.start_session()
        adapter.start_thread()  # acquires a lease for the fake thread id
        data = json.loads(lease.read_text(encoding="utf-8"))
        self.assertIn(adapter.thread_id, data)
        adapter.kill()
        data_after = json.loads(lease.read_text(encoding="utf-8"))
        self.assertNotIn(adapter.thread_id, data_after)  # released on kill

    def test_resume_rpc_failure_releases_newly_claimed_lease(self):
        lease = self._lease_path()
        adapter = make_adapter(engine(), lease_path=lease)
        try:
            adapter.start_session()
            assert adapter._rpc is not None
            with mock.patch.object(adapter._rpc, "request", side_effect=RuntimeError("resume failed")):
                with self.assertRaisesRegex(RuntimeError, "resume failed"):
                    adapter.resume("thread-failed")
            registry = json.loads(lease.read_text(encoding="utf-8")) if lease.is_file() else {}
            self.assertNotIn("thread-failed", registry)
            self.assertIsNone(adapter._leased_thread)
        finally:
            adapter.kill()


class SideChannelMatrixTest(unittest.TestCase):
    """Side-channel deny/wrap matrix: exec_wrapped requires an explicit restrictive sandboxPolicy;
    the fake echoes the received policy (never the user default); a git-push through exec_wrapped is
    denied BEFORE any RPC; thread/shellCommand is unreachable (no adapter API + structural grep)."""

    def test_exec_wrapped_requires_explicit_restrictive_sandbox(self):
        adapter = make_adapter(engine())
        try:
            adapter.start_session()
            # Omitted sandbox -> refused.
            with self.assertRaises(C.CodexAdapterError) as ctx:
                adapter.exec_wrapped(["echo", "x"])
            self.assertEqual(ctx.exception.code, C.DENIED_SANDBOX_REQUIRED)
            # danger-full-access -> refused.
            with self.assertRaises(C.CodexAdapterError) as ctx2:
                adapter.exec_wrapped(["echo", "x"], sandbox_policy={"type": "danger-full-access"})
            self.assertEqual(ctx2.exception.code, C.DENIED_SANDBOX_REQUIRED)
        finally:
            adapter.kill()

    def test_exec_wrapped_echoes_the_explicit_policy(self):
        # A restrictive policy is passed and the fake echoes exactly it back (proving it arrived, and it
        # is never the user default). Use an allow-exact rule so the pseudo-run permits the exec.
        cmd = ["cmd", "/c", "echo", "hi"]
        adapter = make_adapter(exact_allow_for(cmd))
        try:
            adapter.start_session()
            policy_obj = {"type": "read-only", "networkAccess": False}
            out = adapter.exec_wrapped(cmd, sandbox_policy=policy_obj)
            self.assertEqual(out["status"], "OK")
            self.assertEqual(out["echoedSandboxPolicy"], policy_obj)  # explicit policy, echoed verbatim
        finally:
            adapter.kill()

    def test_exec_wrapped_guard_denial_prevents_rpc(self):
        cmd = ["cmd", "/c", "echo", "hi"]
        adapter = make_adapter(exact_allow_for(cmd))
        calls: list[policy.RequestedAction] = []

        def guard(action):
            calls.append(action)
            return {
                "status": "DENIED",
                "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                "retryable": False,
                "required_action": "reconcile the writer lease",
            }

        adapter.set_mutation_guard(guard)
        try:
            adapter.start_session()
            with self.assertRaises(C.CodexAdapterError) as ctx:
                adapter.exec_wrapped(cmd, sandbox_policy={"type": "read-only"})
        finally:
            adapter.kill()

        self.assertEqual(ctx.exception.code, "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].verb, "exec")
        declined = [e for e in adapter.test_events
                    if e["event_kind"] == "approval" and e["marker"] == "declined"]
        failed = [e for e in adapter.test_events
                  if e["event_kind"] == "tool_call" and e["marker"] == "close_fail"]
        self.assertEqual(declined[-1]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertEqual(failed[-1]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")

    def test_git_push_through_exec_wrapped_denied_before_any_rpc(self):
        # A git-push argv is hard-denied by INV_GIT_PUSH at the pseudo-run, before any command/exec RPC
        # leaves the process (the fake would have echoed a result; we assert the refusal instead).
        adapter = make_adapter(engine())
        try:
            adapter.start_session()
            with self.assertRaises(C.CodexAdapterError) as ctx:
                adapter.exec_wrapped(["git", "push", "origin", "main"],
                                     sandbox_policy={"type": "read-only"})
            self.assertEqual(ctx.exception.code, C.DENIED_SIDE_CHANNEL_POLICY)
            self.assertEqual(ctx.exception.fields.get("invariant_id"), policy.INV_GIT_PUSH)
        finally:
            adapter.kill()

    def test_thread_shell_command_is_unreachable(self):
        adapter = make_adapter(engine())
        try:
            adapter.start_session()
            with self.assertRaises(C.CodexAdapterError) as ctx:
                adapter.shell_command("echo x")
            self.assertEqual(ctx.exception.code, C.DENIED_UNSANDBOXED_SIDE_CHANNEL)
        finally:
            adapter.kill()

    def test_adapter_never_sends_thread_shell_command_structurally(self):
        # Structural grep: the adapter source references thread/shellCommand ONLY via the refusal
        # constant SHELL_COMMAND_METHOD (defined once) -- there is no rpc.request/notify of it.
        src = (Path(REPO_ROOT) / "kaizen_components" / "orchestration" / "adapters" / "codex.py").read_text(encoding="utf-8")
        # The literal string appears only in the SHELL_COMMAND_METHOD assignment (+ docstrings), never
        # in a request(...) / notify(...) call.
        for line in src.splitlines():
            if "thread/shellCommand" in line and ("request(" in line or "notify(" in line):
                self.fail(f"adapter sends thread/shellCommand: {line.strip()}")
        # And the method is routed only through the deny path (SHELL_COMMAND_METHOD constant exists).
        self.assertIn('SHELL_COMMAND_METHOD = "thread/shellCommand"', src)


class WindowsUnsandboxedTest(unittest.TestCase):
    """On win32 capabilities() and the session-start recorder event carry sandbox_mode
    'effectively-unsandboxed' (§A.1: no OS sandbox for Codex; the supervisor is the only gate)."""

    def test_sandbox_mode_reported(self):
        adapter = make_adapter(engine())
        try:
            adapter.start_session()
            caps = adapter.capabilities()
            expected = "effectively-unsandboxed" if os.name == "nt" else "vendor-sandboxed"
            self.assertEqual(caps["sandbox_mode"], expected)
            # The session-open recorder event carries the same sandbox_mode.
            sess = [e for e in adapter.test_events if e["event_kind"] == "session" and e["marker"] == "open"]
            self.assertTrue(sess)
            self.assertEqual(sess[0]["payload"]["sandbox_mode"], expected)
        finally:
            adapter.kill()

    def test_win32_is_effectively_unsandboxed(self):
        if os.name != "nt":
            self.skipTest("win32-only assertion")
        adapter = make_adapter(engine())
        try:
            adapter.start_session()
            self.assertEqual(adapter.sandbox_mode, "effectively-unsandboxed")
        finally:
            adapter.kill()


class UnknownServerRequestTest(unittest.TestCase):
    """An unknown server->client request gets a safe (error) reply + a recorded event, and the reader
    thread survives to drive the rest of the turn to completion."""

    def test_unknown_request_safe_reply_and_survival(self):
        adapter = make_adapter(engine(), scenario="unknown-request")
        try:
            adapter.start_session(cwd=str(REPO_ROOT))
            adapter.start_thread(cwd=str(REPO_ROOT))
            adapter.start_turn("go")
            # The reader survives: the turn still completes after answering the bogus request.
            self.assertTrue(wait_for(lambda: any(
                e["event_kind"] == "turn" and e["marker"] in ("close_ok", "close_fail")
                for e in adapter.test_events)), "turn should complete after the unknown request")
            # A transport 'point' event recorded the safe denial.
            pts = [e for e in adapter.test_events if e["event_kind"] == "transport" and e["marker"] == "point"]
            self.assertTrue(pts)
            self.assertIn("unknownServerRequest", pts[0]["payload"]["method"])
        finally:
            adapter.kill()


class StdoutPristineTest(unittest.TestCase):
    """The adapter never prints to stdout: capture process stdout during a full fake turn and assert it
    stayed empty (a future adapter hands the child's stdout straight through, §A.3)."""

    def test_no_stdout_during_full_turn(self):
        buffer = io.StringIO()
        adapter = make_adapter(exact_allow_for(FAKE_APPROVAL_CMD), scenario="approval")
        try:
            with redirect_stdout(buffer):
                adapter.start_session(cwd=str(REPO_ROOT))
                adapter.start_thread(cwd=str(REPO_ROOT))
                adapter.start_turn("do it")
                self.assertTrue(wait_for(lambda: any(
                    e["event_kind"] == "turn" and e["marker"] in ("close_ok", "close_fail")
                    for e in adapter.test_events
                )), "turn did not complete")
        finally:
            adapter.kill()
        self.assertEqual(buffer.getvalue(), "", f"adapter wrote to stdout: {buffer.getvalue()!r}")


class TerminalOnKillTest(unittest.TestCase):
    """Every exit path emits a terminal event (§A.3): kill() mid-turn synthesizes close_canceled for the
    open turn + child item spans and reaps the OWNED child."""

    def test_kill_midturn_emits_terminal_spans(self):
        adapter = make_adapter(engine(), scenario="interrupt-hang")
        adapter.start_session(cwd=str(REPO_ROOT))
        adapter.start_thread(cwd=str(REPO_ROOT))
        out = adapter.start_turn("hang")
        turn_id = out["turn_id"]
        self.assertTrue(wait_for(lambda: any(
            e["event_kind"] == "subagent" and e["marker"] == "open" for e in adapter.test_events)))
        child_pid = adapter._child.pid  # type: ignore[union-attr]
        adapter.kill()
        # Terminal spans synthesized for the open turn + item.
        turns = markers(adapter.test_events, "turn")
        subs = markers(adapter.test_events, "subagent")
        self.assertIn("close_canceled", [m for m, _c, _p in turns])
        self.assertIn("close_canceled", [m for m, _c, _p in subs])
        # The OWNED child tree is reaped.
        self.assertTrue(wait_for(lambda: not children.pid_alive(child_pid), timeout=10.0),
                        "owned child must be reaped on kill")
        # kill() is idempotent.
        self.assertEqual(adapter.kill()["status"], "OK")

    def test_kill_before_any_turn_is_clean(self):
        adapter = make_adapter(engine())
        adapter.start_session()
        self.assertEqual(adapter.kill()["status"], "OK")

    def test_kill_failure_keeps_child_and_never_claims_termination(self):
        for outcome in ("raises", "still-live"):
            with self.subTest(outcome=outcome):
                adapter = make_adapter(engine())
                child = mock.Mock()
                child.process.stdin = None
                child.process.stdout = None
                child.process.stderr = None
                child.poll.return_value = None
                if outcome == "raises":
                    child.kill_tree.side_effect = children.ChildTerminationError("fixture remains alive")
                adapter._child = child

                result = adapter.kill()

                self.assertEqual(result["status"], "ERROR")
                self.assertEqual(result["code"], C.RUNTIME_CLEANUP_FAILED)
                self.assertFalse(result["killed"])
                self.assertFalse(result["termination_proven"])
                self.assertIn("child_termination_unproven", result["cleanup_errors"])
                self.assertIs(adapter._child, child)
                retry = adapter.kill()
                self.assertEqual(retry["status"], "ERROR")
                self.assertFalse(retry["killed"])


class H23LogicalConversationTest(unittest.TestCase):
    """One app-server connection/thread survives turns; run_turn rendezvous captures final text/id."""

    def test_two_turns_share_child_thread_and_context(self):
        adapter = make_h2_adapter()
        self.addCleanup(adapter.kill)
        pid = adapter._child.pid
        thread_id = adapter.thread_id
        first = adapter.run_turn("remember alpha")
        second = adapter.run_turn("what came first?")
        self.assertEqual(first.status, "OK")
        self.assertEqual(second.status, "OK")
        self.assertNotEqual(first.vendor_turn_id, second.vendor_turn_id)
        self.assertIn("remember alpha", second.final_text)
        self.assertEqual(adapter._child.pid, pid)
        self.assertEqual(adapter.thread_id, thread_id)
        self.assertIsNone(adapter.active_turn_id)

    def test_terminal_notification_before_start_reply_is_not_lost(self):
        adapter = make_h2_adapter(scenario="early-terminal")
        self.addCleanup(adapter.kill)
        result = adapter.run_turn("race")
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.vendor_turn_id, "turn-fake-0001")

    def test_last_final_agent_message_wins_and_turn_id_is_vendor_id(self):
        adapter = make_h2_adapter(scenario="multiple-final")
        self.addCleanup(adapter.kill)
        result = adapter.run_turn("final ordering")
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.vendor_turn_id, "turn-fake-0001")
        self.assertEqual(result.final_text, "last final for turn 1")

    def test_steer_and_interrupt_target_real_active_turn(self):
        adapter = make_h2_adapter(scenario="interrupt-hang")
        self.addCleanup(adapter.kill)
        box: dict[str, object] = {}

        def run_turn() -> None:
            try:
                box["result"] = adapter.run_turn("hang")
            except BaseException as error:  # pragma: no cover - re-raised with the original traceback below
                box["error"] = error

        worker = threading.Thread(target=run_turn)
        worker.start()
        self.assertTrue(wait_for(lambda: adapter.active_turn_id is not None))
        active = adapter.active_turn_id
        steered = adapter.steer("more detail")
        self.assertEqual(steered["turn_id"], active)
        adapter.interrupt()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        error = box.get("error")
        if isinstance(error, BaseException):
            raise error
        result = box["result"]
        self.assertEqual(result.status, "CANCELED")
        self.assertEqual(result.vendor_turn_id, active)

    def test_close_reaps_idle_persistent_child(self):
        adapter = make_h2_adapter()
        pid = adapter._child.pid
        self.assertEqual(adapter.run_turn("done").status, "OK")
        self.assertEqual(adapter.close()["status"], "OK")
        self.assertTrue(wait_for(lambda: not children.pid_alive(pid), timeout=10.0))


class H23ApprovalFamiliesTest(unittest.TestCase):
    """Every installed request family uses the policy callback and emits its vendor-native reply."""

    def test_all_current_and_legacy_families(self):
        cases = {
            "approval": "commandExecution",
            "file-approval": "fileChange",
            "permissions-approval": "permissions",
            "user-input": "requestUserInput",
            "mcp-elicitation": "mcpElicitation",
            "legacy-exec": "legacyExecCommand",
            "legacy-patch": "legacyPatch",
        }
        for scenario, request_type in cases.items():
            with self.subTest(scenario=scenario):
                adapter = make_h2_adapter(scenario=scenario)
                try:
                    adapter.on_approval(lambda _request: C.DECISION_APPROVED)
                    result = adapter.run_turn("approve once")
                    self.assertEqual(result.status, "OK")
                    events = [event for event in adapter.test_events
                              if event["event_kind"] == "approval"
                              and event["payload"].get("request_type") == request_type]
                    self.assertTrue(events, adapter.test_events)
                    self.assertEqual(events[-1]["payload"]["decision"], C.DECISION_APPROVED)
                finally:
                    adapter.kill()

    def test_early_resolution_before_wait_is_consumed_once(self):
        adapter = make_h2_adapter(scenario="approval", approval_timeout=0.2)
        calls: list[str] = []
        adapter.on_approval(lambda request: calls.append(request["correlation_id"]) or C.DECISION_APPROVED)
        try:
            result = adapter.run_turn("early")
        finally:
            adapter.kill()
        self.assertEqual(result.status, "OK")
        self.assertEqual(len(calls), 1)

    def test_policy_denial_never_calls_helper(self):
        adapter = make_h2_adapter(mode="plan", scenario="deny")
        helper_calls: list[object] = []
        adapter.on_approval(lambda request: helper_calls.append(request) or C.DECISION_APPROVED)
        try:
            result = adapter.run_turn("push")
        finally:
            adapter.kill()
        self.assertEqual(result.status, "OK")
        self.assertEqual(helper_calls, [], "hard denial invoked the approval helper")
        declined = [event for event in adapter.test_events
                    if event["event_kind"] == "approval" and event["marker"] == "declined"]
        self.assertTrue(declined)
        self.assertEqual(declined[0]["payload"]["invariant_id"], policy.INV_GIT_PUSH)

    def test_approval_helper_exception_fails_closed_without_stranding_turn(self):
        adapter = make_h2_adapter(scenario="approval")

        def broken(_request):
            raise RuntimeError("helper failed")

        adapter.on_approval(broken)
        try:
            result = adapter.run_turn("safe failure")
        finally:
            adapter.kill()
        self.assertEqual(result.status, "OK")
        self.assertTrue(any(event["marker"] == "declined" for event in adapter.test_events
                            if event["event_kind"] == "approval"))

    def test_c4_persists_before_single_open_and_resolver(self):
        adapter = make_h2_adapter(scenario="approval")
        adapter.bind_session("as_fixture")
        order: list[str] = []
        adapter._recorder = lambda event: order.append(
            "open" if event["event_kind"] == "approval" and event["marker"] == "open" else "event"
        )
        adapter.policy_engine.record_ask = lambda _decision, _session: (
            order.append("persist") or {"status": "OK", "id": "ap_fixture"}
        )
        adapter.on_approval(lambda _request: order.append("resolver") or C.DECISION_APPROVED)
        try:
            result = adapter.run_turn("ordered approval")
        finally:
            adapter.kill()
        self.assertEqual(result.status, "OK")
        self.assertEqual(order.count("open"), 1)
        self.assertEqual(order.count("resolver"), 1)
        self.assertLess(order.index("persist"), order.index("open"))
        self.assertLess(order.index("open"), order.index("resolver"))

    def test_c4_persistence_failure_denies_without_open_or_resolver(self):
        adapter = make_h2_adapter(scenario="approval")
        adapter.bind_session("as_fixture")
        adapter.policy_engine.record_ask = mock.Mock(side_effect=OSError("fixture persistence failure"))
        resolver = mock.Mock(return_value=C.DECISION_APPROVED)
        adapter.on_approval(resolver)
        try:
            result = adapter.run_turn("fail closed")
        finally:
            adapter.kill()
        self.assertEqual(result.status, "OK")
        resolver.assert_not_called()
        self.assertFalse(any(event["event_kind"] == "approval" and event["marker"] == "open"
                             for event in adapter.test_events))

    def test_malformed_and_unknown_approval_shapes_deny_without_policy(self):
        adapter = make_h2_adapter()
        resolver = mock.Mock(return_value=C.DECISION_APPROVED)
        adapter.on_approval(resolver)
        try:
            cases = {
                C.APPROVAL_METHOD: {"decision": C.WIRE_DECLINE},
                C.FILE_CHANGE_APPROVAL_METHOD: {"decision": C.WIRE_DECLINE},
                C.PERMISSIONS_APPROVAL_METHOD: {"permissions": {}, "scope": "turn"},
                "item/tool/requestUserInput": {"answers": {}},
                C.MCP_ELICITATION_METHOD: {"action": "decline"},
                C.LEGACY_EXEC_APPROVAL_METHOD: {"decision": "denied"},
                C.LEGACY_PATCH_APPROVAL_METHOD: {"decision": "denied"},
            }
            for method, expected in cases.items():
                with self.subTest(method=method):
                    self.assertEqual(adapter._handle_server_request(1, method, {}), expected)
            unknown = adapter._handle_server_request(2, "item/newApproval/request", {})
            self.assertIn("error", unknown)
        finally:
            adapter.kill()
        resolver.assert_not_called()

    def test_free_text_user_input_requires_exact_typed_broker_answers(self):
        adapter = make_h2_adapter()
        question = {"questions": [{"id": "reason", "header": "Decision", "question": "Continue?"}]}
        try:
            adapter.set_approval_broker(lambda _request: C.BrokerApprovalResult(
                C.DECISION_APPROVED, updated_input={"answers": {"reason": "Explicit operator answer"}},
            ))
            self.assertEqual(adapter._answer_user_input(question), {
                "answers": {"reason": {"answers": ["Explicit operator answer"]}},
            })

            adapter.set_approval_broker(lambda _request: C.BrokerApprovalResult(C.DECISION_APPROVED))
            self.assertEqual(adapter._answer_user_input(question), {"answers": {"reason": {"answers": []}}})

            malformed = (
                {"answers": {"other": "wrong id"}},
                {"answers": {"reason": ""}},
                {"answers": {"reason": "x" * (64 * 1024 + 1)}},
                {"answers": {"reason": "contains\x00nul"}},
                {"answers": {"reason": 7}},
            )
            for updated_input in malformed:
                with self.subTest(updated_input=updated_input):
                    adapter.set_approval_broker(lambda _request, value=updated_input: C.BrokerApprovalResult(
                        C.DECISION_APPROVED, updated_input=value,
                    ))
                    self.assertEqual(adapter._answer_user_input(question), {
                        "answers": {"reason": {"answers": []}},
                    })
        finally:
            adapter.kill()

    def test_choice_user_input_keeps_vendor_labels_for_binary_decisions(self):
        adapter = make_h2_adapter()
        question = {"questions": [{
            "id": "decision", "header": "Approve", "question": "Continue?",
            "options": [{"label": "Accept", "description": "run"}, {"label": "Decline", "description": "stop"}],
        }]}
        try:
            for decision, expected in ((C.DECISION_APPROVED, "Accept"), (C.DECISION_DENIED, "Decline")):
                with self.subTest(decision=decision):
                    adapter.set_approval_broker(lambda _request, value=decision: C.BrokerApprovalResult(value))
                    self.assertEqual(adapter._answer_user_input(question), {
                        "answers": {"decision": {"answers": [expected]}},
                    })
        finally:
            adapter.kill()


class H23FailureAndProfileTest(unittest.TestCase):
    def _unopened(self, scenario: str = "plain-turn", *, turn_timeout: float = 0.2) -> C.CodexAdapter:
        snap = snapshot("ask")
        adapter = C.CodexAdapter(
            snap.build_engine(), cmd=fake_cmd(scenario=scenario), logger=lambda _message: None,
            request_timeout=2.0, turn_timeout=turn_timeout, schema_capabilities=COMPATIBLE_SCHEMA,
        )
        adapter.test_snapshot = snap  # type: ignore[attr-defined]
        return adapter

    def _profile(self, mode: str = "ask", auth: str = "subscription") -> dict:
        return {"model": "gpt-fake", "reasoning_effort": "medium",
                "permission_mode": mode, "auth_mode": auth}

    def test_malformed_terminal_is_fatal_and_reaped(self):
        adapter = make_h2_adapter(scenario="malformed-notification", turn_timeout=0.2)
        pid = adapter._child.pid
        result = adapter.run_turn("bad terminal")
        self.assertEqual(result.status, "FAILED")
        self.assertTrue(result.fatal)
        self.assertEqual(result.error_code, C.CODEX_TURN_TIMEOUT)
        self.assertTrue(wait_for(lambda: not children.pid_alive(pid), timeout=10.0))

    def test_child_exit_is_fatal_and_does_not_hang(self):
        adapter = make_h2_adapter(scenario="child-failure", turn_timeout=1.0)
        result = adapter.run_turn("die")
        self.assertEqual(result.status, "FAILED")
        self.assertTrue(result.fatal)
        self.assertEqual(result.error_code, C.CODEX_CHILD_EXITED)
        adapter.kill()

    def test_profile_mismatch_denied_before_first_prompt(self):
        adapter = self._unopened("profile-mismatch")
        with self.assertRaises(C.CodexAdapterError) as ctx:
            adapter.open(self._profile(), adapter.test_snapshot)
        self.assertEqual(ctx.exception.code, C.DENIED_PROFILE_MISMATCH)
        self.assertIsNone(adapter._child)

    def test_helper_not_loaded_denies_open_and_reaps(self):
        adapter = self._unopened("helper-failure")
        with self.assertRaises(C.CodexAdapterError) as ctx:
            adapter.open(self._profile(), adapter.test_snapshot)
        self.assertEqual(ctx.exception.code, C.DENIED_POLICY_GATE_UNAVAILABLE)
        self.assertIsNone(adapter._child)

    def test_full_denied_before_spawn(self):
        adapter = self._unopened()
        full_snapshot = snapshot("full")
        with self.assertRaises(C.CodexAdapterError) as ctx:
            adapter.open(self._profile("full"), full_snapshot)
        self.assertEqual(ctx.exception.code, C.DENIED_PROFILE_UNSUPPORTED)
        self.assertIsNone(adapter._child)


class H23CredentialTest(unittest.TestCase):
    def test_subscription_scrubs_both_vendor_keys_and_credential_paths(self):
        # Subscription: keys/credential paths scrubbed; scratch dirs stay private; CODEX_HOME is NOT
        # forced to the private empty home (installed login must stay reachable -- pinned 2026-07-10).
        ambient_home = os.environ.get("CODEX_HOME")
        adapter = make_h2_adapter(env={
            "OPENAI_API_KEY": "not-forwarded",
            "ANTHROPIC_API_KEY": "not-forwarded",
            "KAIZEN_CODEX_API_KEY_FILE": "D:\\not-forwarded",
        })
        try:
            observed = adapter._runtime_config["_test_env"]
            self.assertFalse(observed["openai_key_present"])
            self.assertFalse(observed["anthropic_key_present"])
            self.assertFalse(observed["credential_path_present"])
            runtime = adapter._runtime_dir.resolve()
            for name in ("temp", "tmp", "tmpdir"):
                root = Path(observed[name]).resolve()
                root.relative_to(runtime)
                if os.name == "nt":
                    self.assertEqual(root.drive.casefold(), "d:")
            self.assertEqual(observed["codex_home"], ambient_home)  # ambient (None when unset), never the seal
        finally:
            adapter.kill()

    def test_subscription_drops_vendor_runtime_identity_masks(self):
        # Supervisor vendor-runtime isolation injects HOME/USERPROFILE/CODEX_HOME masks via env_extra;
        # subscription children restore ambient identity so auth.json stays reachable. api-key children
        # keep the seal (covered by test_api_key_file_injected_only_into_selected_child_without_echo).
        ambient_profile = os.environ.get("USERPROFILE") or os.environ.get("HOME")
        adapter = make_h2_adapter(env={
            "USERPROFILE": "D:\\mask-home", "HOME": "D:\\mask-home", "CODEX_HOME": "D:\\mask-codex",
        })
        try:
            env = adapter._compose_child_env({"auth_mode": "subscription"})
            self.assertNotEqual(env.get("USERPROFILE"), "D:\\mask-home")
            self.assertNotEqual(env.get("CODEX_HOME"), "D:\\mask-codex")
            if ambient_profile:
                self.assertEqual(env.get("USERPROFILE") or env.get("HOME"), ambient_profile)
        finally:
            adapter.kill()

    def test_api_key_file_injected_only_into_selected_child_without_echo(self):
        work = Path(REPO_ROOT) / "AI" / "work"
        with tempfile.TemporaryDirectory(prefix="codex-key-test-", dir=work) as temp:
            token = "sk-test-codex-private-value"
            key_file = Path(temp) / "key.txt"
            key_file.write_text(token + "\n", encoding="utf-8")
            adapter = make_h2_adapter(auth_mode="api-key", env={
                C.API_KEY_FILE_ENV: str(key_file),
                "ANTHROPIC_API_KEY": "not-forwarded",
            })
            try:
                observed = adapter._runtime_config["_test_env"]
                self.assertTrue(observed["openai_key_present"])
                self.assertFalse(observed["anthropic_key_present"])
                self.assertFalse(observed["credential_path_present"])
                rendered = json.dumps({"config": adapter._runtime_config, "events": adapter.test_events})
                self.assertNotIn(token, rendered)
            finally:
                adapter.kill()

    def test_malformed_key_files_fail_safely(self):
        work = Path(REPO_ROOT) / "AI" / "work"
        cases = {"empty": b"", "multiline": b"one\ntwo\n", "spaces": b"one two\n",
                 "invalid_utf8": b"\xff", "oversize": b"x" * (C.MAX_API_KEY_FILE_BYTES + 1)}
        with tempfile.TemporaryDirectory(prefix="codex-key-invalid-", dir=work) as temp:
            for name, data in cases.items():
                with self.subTest(name=name):
                    path = Path(temp) / name
                    path.write_bytes(data)
                    with self.assertRaises(C.CodexAdapterError) as ctx:
                        C.load_api_key_file(path)
                    self.assertEqual(ctx.exception.code, C.DENIED_CREDENTIAL_FILE)
                    self.assertNotIn(repr(data[:20]), str(ctx.exception))

    def test_vendor_stderr_is_suppressed_without_secret_echo(self):
        sentinel = "codex-stderr-private-sentinel"
        logs: list[str] = []
        adapter = make_h2_adapter(
            scenario="stderr-secret", env={"KAIZEN_TEST_STDERR_SENTINEL": sentinel}, logger=logs.append,
        )
        adapter.kill()
        self.assertTrue(wait_for(lambda: any("stderr suppressed" in line for line in logs)))
        self.assertNotIn(sentinel, "\n".join(logs))

    def test_scrubbed_hook_runner_removes_vendor_key_sentinels(self):
        work = Path(REPO_ROOT) / "AI" / "work"
        with tempfile.TemporaryDirectory(prefix="codex-scrub-test-", dir=work) as temp:
            probe = Path(temp) / "probe.py"
            probe.write_text(
                "import json, os\n"
                "print(json.dumps({k: (k in os.environ) for k in "
                "['OPENAI_API_KEY','ANTHROPIC_API_KEY','KAIZEN_CODEX_API_KEY_FILE']}))\n",
                encoding="utf-8",
            )
            runner = Path(REPO_ROOT) / "kaizen_components" / "orchestration" / "vendor_env_scrub.py"
            env = dict(os.environ)
            env.update({
                "OPENAI_API_KEY": "openai-private-sentinel",
                "ANTHROPIC_API_KEY": "anthropic-private-sentinel",
                "KAIZEN_CODEX_API_KEY_FILE": str(Path(temp) / "private.key"),
            })
            completed = subprocess.run(
                [sys.executable, str(runner), "--", sys.executable, str(probe)],
                capture_output=True, text=True, encoding="utf-8", env=env, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), {
                "OPENAI_API_KEY": False,
                "ANTHROPIC_API_KEY": False,
                "KAIZEN_CODEX_API_KEY_FILE": False,
            })

    def test_runtime_cleanup_failure_is_surfaced_and_retryable(self):
        adapter = make_h2_adapter()
        runtime = adapter._runtime_dir
        with mock.patch.object(C.shutil, "rmtree", side_effect=OSError("fixture locked")):
            result = adapter.close()
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["code"], C.RUNTIME_CLEANUP_FAILED)
        self.assertTrue(runtime.exists())
        retry = adapter.kill()
        self.assertEqual(retry["status"], "OK")
        self.assertFalse(runtime.exists())


class H23CapabilityProbeTest(unittest.TestCase):
    def test_generated_compatible_fake_schema_passes_complete_surface(self):
        work = Path(REPO_ROOT) / "AI" / "work"
        caps = C.schema_capabilities_from_installed([sys.executable, FAKE], runtime_dir=work)
        self.assertFalse(caps["missing_methods"])
        self.assertTrue(caps["bounded_read_access"])
        self.assertTrue(caps["pre_tool_command_hook"])

    def test_missing_bounded_reads_marks_engine_not_drivable(self):
        work = Path(REPO_ROOT) / "AI" / "work"
        command = [sys.executable, FAKE, "--incompatible-schema"]
        caps = C.schema_capabilities_from_installed(command, runtime_dir=work)
        self.assertFalse(caps["bounded_read_access"])
        descriptor = C.installed_capability(command, runtime_dir=work)
        self.assertFalse(descriptor["drivable"])
        self.assertEqual(descriptor["availability"]["code"], C.DENIED_POLICY_GATE_UNAVAILABLE)
        self.assertEqual(descriptor["permission_modes"], [])

    def test_schema_subprocess_env_is_scrubbed_and_d_scoped(self):
        work = Path(REPO_ROOT) / "AI" / "work"
        original_run = C.subprocess.run
        captured: dict[str, str] = {}

        def spy(*args, **kwargs):
            captured.update(kwargs.get("env") or {})
            return original_run(*args, **kwargs)

        with mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "schema-private-sentinel",
            "ANTHROPIC_API_KEY": "schema-private-sentinel",
            C.API_KEY_FILE_ENV: "D:\\private.key",
        }, clear=False), mock.patch.object(C.subprocess, "run", side_effect=spy):
            caps = C.schema_capabilities_from_installed([sys.executable, FAKE], runtime_dir=work)
        self.assertTrue(caps["bounded_read_access"])
        for key in C._SENSITIVE_ENV:
            self.assertNotIn(key, captured)
        for key in ("CODEX_HOME", "TEMP", "TMP", "TMPDIR"):
            root = Path(captured[key]).resolve()
            if os.name == "nt":
                self.assertEqual(root.drive.casefold(), "d:")
            else:
                root.relative_to(work.resolve())


class H23PerModeBoundaryTest(unittest.TestCase):
    """The per-mode sandbox/approval-policy boundary (plan v3 PINNED Codex 2026-07-10). Asserts the EXACT
    vendor flag values _boundary_for_mode builds, that full is refused before any spawn, and -- at the
    integration seam -- that a driven fake turn per mode carries the mode-mapped thread sandbox +
    approvalPolicy (the fake echoes both on thread/start; the adapter's _boundary is the sent payload)."""

    def _bounded_adapter(self, mode: str) -> tuple[C.CodexAdapter, policy.PolicySnapshot]:
        # A boundary-only adapter: no open()/spawn; _policy_snapshot is set so the roots resolve.
        snap = snapshot(mode)
        adapter = C.CodexAdapter(
            snap.build_engine(), cmd=fake_cmd(), logger=lambda _message: None,
            request_timeout=2.0, schema_capabilities=COMPATIBLE_SCHEMA,
        )
        adapter._policy_snapshot = snap
        return adapter, snap

    def test_supported_modes_are_exactly_plan_ask_agent(self):
        self.assertEqual(C.SUPPORTED_PERMISSION_MODES, ("plan", "ask", "agent"))

    def test_plan_and_ask_map_to_read_only_boundary(self):
        for mode in ("plan", "ask"):
            with self.subTest(mode=mode):
                adapter, snap = self._bounded_adapter(mode)
                boundary = adapter._boundary_for_mode(mode)
                self.assertEqual(boundary["thread_sandbox"], "read-only")
                self.assertEqual(boundary["turn_sandbox"], {
                    "type": "readOnly",
                    "access": {
                        "type": "restricted",
                        "includePlatformDefaults": True,
                        "readableRoots": [snap.workspace_root],
                    },
                    "networkAccess": False,
                })
                # Granular per-item approval policy (deep-equal to the pinned constant, not aliased).
                self.assertEqual(boundary["approval_policy"], C._GRANULAR_APPROVAL_POLICY)
                self.assertIsNot(boundary["approval_policy"], C._GRANULAR_APPROVAL_POLICY)

    def test_agent_maps_to_workspace_write_boundary(self):
        adapter, snap = self._bounded_adapter("agent")
        boundary = adapter._boundary_for_mode("agent")
        self.assertEqual(boundary["thread_sandbox"], "workspace-write")
        writable = [snap.workspace_root, *snap.designated_write_roots]
        self.assertEqual(boundary["turn_sandbox"], {
            "type": "workspaceWrite",
            "writableRoots": writable,
            "readOnlyAccess": {
                "type": "restricted",
                "includePlatformDefaults": True,
                "readableRoots": [snap.workspace_root],
            },
            "networkAccess": False,
            "excludeSlashTmp": True,
            "excludeTmpdirEnvVar": True,
        })
        # workspace is first, the designated (AI/work) root follows it, and there is no duplicate entry.
        self.assertEqual(writable[0], snap.workspace_root)
        self.assertEqual(len(snap.designated_write_roots), 1)
        self.assertNotEqual(snap.designated_write_roots[0], snap.workspace_root)
        self.assertEqual(len(writable), len(set(writable)))
        self.assertEqual(boundary["approval_policy"], C._GRANULAR_APPROVAL_POLICY)

    def test_full_refused_by_boundary_before_spawn(self):
        # _boundary_for_mode itself refuses full with DENIED_PROFILE_UNSUPPORTED -- the boundary is where
        # the mode is rejected, before any child spawns (test_full_denied_before_spawn covers open()).
        adapter, _snap = self._bounded_adapter("ask")
        self.assertIsNone(adapter._child)
        with self.assertRaises(C.CodexAdapterError) as ctx:
            adapter._boundary_for_mode("full")
        self.assertEqual(ctx.exception.code, C.DENIED_PROFILE_UNSUPPORTED)
        self.assertEqual(ctx.exception.fields.get("supported"), list(C.SUPPORTED_PERMISSION_MODES))
        self.assertIsNone(adapter._child)

    def test_driven_turn_carries_mode_mapped_boundary_per_mode(self):
        # Integration seam: a full fake turn per mode. open() drives thread/start through the fake (which
        # stores thread_params + echoes sandbox/approvalPolicy) and one turn/start; the turn only reaches
        # OK if the fake accepted the mode-mapped payload. _boundary is the exact sent thread/turn config.
        expected_thread = {"plan": "read-only", "ask": "read-only", "agent": "workspace-write"}
        expected_turn_type = {"plan": "readOnly", "ask": "readOnly", "agent": "workspaceWrite"}
        for mode in ("plan", "ask", "agent"):
            with self.subTest(mode=mode):
                adapter = make_h2_adapter(mode=mode)
                try:
                    result = adapter.run_turn("map the boundary")
                    self.assertEqual(result.status, "OK", adapter.test_events)
                    self.assertEqual(adapter._boundary["thread_sandbox"], expected_thread[mode])
                    self.assertEqual(adapter._boundary["turn_sandbox"]["type"], expected_turn_type[mode])
                    self.assertEqual(adapter._boundary["turn_sandbox"]["networkAccess"], False)
                    self.assertEqual(adapter._boundary["approval_policy"], C._GRANULAR_APPROVAL_POLICY)
                finally:
                    adapter.kill()

    def test_fake_echoes_mode_mapped_thread_config(self):
        # Prove the FAKE captured the mode-mapped thread params: drive thread/start directly with each
        # mode's boundary and assert the fake echoed the sandbox type + the granular approvalPolicy back
        # (fake_codex_app_server stores params in thread_params and returns them on the thread/start reply).
        expected_sandbox_type = {"read-only": "readOnly", "workspace-write": "workspaceWrite"}
        for mode in ("plan", "ask", "agent"):
            with self.subTest(mode=mode):
                adapter, _snap = self._bounded_adapter(mode)
                boundary = adapter._boundary_for_mode(mode)
                adapter.start_session(cwd=str(REPO_ROOT))
                try:
                    out = adapter.start_thread(
                        cwd=str(REPO_ROOT),
                        approval_policy=boundary["approval_policy"],
                        sandbox=boundary["thread_sandbox"],
                    )
                    self.assertEqual(out["status"], "OK")
                    self.assertEqual(out["sandbox"]["type"], expected_sandbox_type[boundary["thread_sandbox"]])
                    self.assertEqual(out["approval_policy"], C._GRANULAR_APPROVAL_POLICY)
                finally:
                    adapter.kill()


class H23HookShimTest(unittest.TestCase):
    def test_default_helpers_are_orchestration_owned(self) -> None:
        adapter = C.CodexAdapter(engine(), logger=lambda _message: None)
        helper_root = Path(REPO_ROOT) / "kaizen_components" / "orchestration"
        self.assertEqual(adapter._hook_script, helper_root / "codex_hook_shim.py")
        self.assertEqual(adapter._scrubber_script, helper_root / "vendor_env_scrub.py")

    def test_allow_ask_deny_and_helper_failure_render_synchronously(self):
        from kaizen_components.orchestration import codex_hook_shim as shim

        for decision, expected_exit in (("allow", 0), ("ask", 2), ("deny", 2)):
            with self.subTest(decision=decision):
                output: list[str] = []
                exit_code = shim.run(
                    "gate", "hash",
                    read_payload=lambda: {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
                    send=lambda _gate, _hash, _payload, _timeout: {
                        "status": "OK", "result": decision, "reason": "fixture",
                    },
                    write=output.append,
                )
                body = json.loads("".join(output))
                self.assertEqual(exit_code, expected_exit)
                expected_decision = "allow" if decision == "allow" else "deny"
                self.assertEqual(body["hookSpecificOutput"]["permissionDecision"], expected_decision)

        output = []
        exit_code = shim.run(
            "gate", "hash", read_payload=lambda: {},
            send=lambda *_args: (_ for _ in ()).throw(RuntimeError("dead helper")),
            write=output.append,
        )
        body = json.loads("".join(output))
        self.assertEqual(exit_code, 2)
        self.assertEqual(body["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
