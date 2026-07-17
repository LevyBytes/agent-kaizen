"""Security hardening battery (v8 M16, plan §C.4 + §5.3 M16).

Five legs, all hermetic (scratch stores, minted in-memory keys, real loopback sockets where a wire is
exercised; the real AI/db plane is never touched):

1. **Ledger verification pass** (`fleet/ledger_verify.py`) -- the read-side counterpart of M11
   sig-on-append: signed rows verify against the signer's KEY HISTORY (nodes.pubkey + the
   node/registered|updated payload trail, so rotated-away keys still verify); unsigned legacy rows count
   truthfully; content tamper and sig tamper are detected; the advisory divergence/detected record is
   append-once (re-verification appends nothing); a foreign signer with no resolvable key is
   unknown_key, never a crash.
2. **Control-Ingress invariant, STRUCTURAL** -- the package contains exactly TWO listening surfaces
   (fleet/control_http.py ThreadingHTTPServer, orchestration/loopback.py pipe/UDS/loopback-TCP); the
   loopback TCP fallback binds 127.0.0.1 only; every mutating control route refuses an unsigned POST;
   no third-party (discord/webhook/telegram/slack) ingress exists anywhere in the package.
3. **Forged origin** -- a body-supplied ``origin_node`` can NEVER override the cryptographically
   verified envelope identity (attribution + authz both use the envelope).
4. **Hub-push invariant fleet-wide** -- every push-capable seam defaults OFF and is owner-gated
   (HandoffEngine flags, reconcile allow_publish literals at both call sites, mirror.py push-free), and
   the M3 policy INV_GIT_PUSH denies identically across adapter lanes and dispatched-session actors.
5. **Cross-node protected-path write denied** -- INV_PROTECTED_PATH fires identically for local and
   dispatched/remote actor contexts, and a dispatch whose runner is denied fails TRUTHFULLY through the
   M14 executor (no partial state).

OWNER DECISION D7: single-machine; "multi-node" = multiple minted identities over scratch stores.
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import REPO_ROOT  # noqa: E402

from kaizen_components.denials import KaizenDenied  # noqa: E402
from kaizen_components.fleet import coordination, ledger_verify  # noqa: E402
from kaizen_components.fleet.store import FleetStore  # noqa: E402
from kaizen_components.orchestration import policy as P  # noqa: E402

from test_control_http import _ServiceHarness, _mint_identity  # noqa: E402

from kaizen_components.fleet import control_http  # noqa: E402

PKG_ROOT = Path(REPO_ROOT) / "kaizen_components"


def _action(engine: str, session: str, verb: str, targets=(), command=None, epoch: int = 1):
    """Construct a fixed-thread RequestedAction fixture for policy decisions."""
    return P.RequestedAction(P.Actor(engine, session, epoch, "t1"), verb, tuple(targets), command, {})


# --- 1. ledger verification pass ------------------------------------------------------------------

class _LedgerCase(unittest.TestCase):
    """Scratch shared-fleet ledger fixture with signed and legacy unsigned store factories."""
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-m16-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.db_path = self.root / "fleet.db"

    def signing_store(self, node_id: str, ident: dict | None = None) -> FleetStore:
        """Create and auto-close a FleetStore with a signing identity."""
        ident = ident or _mint_identity(node_id)
        s = FleetStore(db_path=self.db_path, node=ident)
        self.addCleanup(s.close)
        return s

    def plain_store(self, node_id: str) -> FleetStore:
        """Create and auto-close a seedless legacy FleetStore without signatures."""
        s = FleetStore(db_path=self.db_path, node={"node_id": node_id, "created_at": "t"})
        self.addCleanup(s.close)
        return s


class LedgerVerifySignedTest(_LedgerCase):
    def test_signed_rows_verify_via_published_key(self) -> None:
        ident = _mint_identity("nSigner0000001")
        s = self.signing_store("nSigner0000001", ident)
        s.publish_pubkey(ident["ed25519_pub_hex"])  # nodes.pubkey + node/updated trail
        coordination.claim_coordinator(s, summary="Claim.")
        s.heartbeat()
        report = ledger_verify.verify_ledger(s)
        self.assertEqual(report["status"], "OK")
        self.assertGreaterEqual(report["verified"], 3)
        self.assertEqual(report["unsigned"], 0)
        self.assertEqual(report["tampered_content"], [])
        self.assertEqual(report["invalid_sig"], [])
        self.assertEqual(report["unknown_key"], [])

    def test_unsigned_rows_counted_truthfully_never_fail(self) -> None:
        s = self.plain_store("nLegacy0000001")  # no seed -> sig NULL (byte-identical legacy append)
        coordination.claim_coordinator(s, summary="Claim.")
        report = ledger_verify.verify_ledger(s)
        self.assertGreaterEqual(report["unsigned"], 2)
        self.assertEqual(report["verified"], 0)
        self.assertEqual(report["invalid_sig"], [])

    def test_rotation_history_still_verifies_old_rows(self) -> None:
        # Era 1: key1 signs + publishes. Era 2 (rotation): SAME node id, key2 -- old rows must still
        # verify via the node/updated payload trail (the M11 rotation verdict: the ledger IS the key
        # history), new rows via the current nodes.pubkey.
        node = "nRotate0000001"
        ident1 = _mint_identity(node)
        s1 = self.signing_store(node, ident1)
        s1.publish_pubkey(ident1["ed25519_pub_hex"])
        coordination.claim_coordinator(s1, summary="Era one claim.")
        s1.close()
        ident2 = _mint_identity(node)  # fresh keypair, same node id = a rotation
        s2 = self.signing_store(node, ident2)
        s2.publish_pubkey(ident2["ed25519_pub_hex"])  # upserts nodes.pubkey to key2 + trail event
        s2.heartbeat()
        report = ledger_verify.verify_ledger(s2)
        self.assertEqual(report["invalid_sig"], [])
        self.assertEqual(report["unknown_key"], [])
        self.assertEqual(report["tampered_content"], [])
        self.assertGreaterEqual(report["verified"], 5)  # both eras fully verified

    def test_foreign_signer_with_no_key_is_unknown_never_a_crash(self) -> None:
        ghost_ident = _mint_identity("nGhost00000001")
        ghost = self.signing_store("nGhost00000001", ghost_ident)  # signs but NEVER publishes a key
        ghost.heartbeat()
        report = ledger_verify.verify_ledger(ghost)
        self.assertEqual(len(report["unknown_key"]), 1)
        self.assertEqual(report["verified"], 0)


class LedgerVerifyTamperTest(_LedgerCase):
    def _tamper(self, store: FleetStore, sql: str, params: tuple) -> None:
        """Mutate a ledger row through raw store SQL to simulate on-disk tampering."""
        def op(conn):  # noqa: ANN001
            conn.execute(sql, params)

        store._write(op)

    def test_content_tamper_detected_and_advisory_appended_once(self) -> None:
        ident = _mint_identity("nTamper0000001")
        s = self.signing_store("nTamper0000001", ident)
        s.publish_pubkey(ident["ed25519_pub_hex"])
        appended = s.append_coord_event("heartbeat", "point", summary="Victim row.", payload={"ts": "x"})
        self._tamper(s, "UPDATE coord_events SET payload_json = ? WHERE id = ?", ('{"ts":"FORGED"}', appended["id"]))
        report = ledger_verify.verify_ledger(s, record=True)
        self.assertEqual(report["tampered_content"], [appended["id"]])
        self.assertEqual(len(report["recorded"]), 1)
        # Re-verification appends NOTHING (dedupe) and still reports the tamper.
        again = ledger_verify.verify_ledger(s, record=True)
        self.assertEqual(again["tampered_content"], [appended["id"]])
        self.assertEqual(again["recorded"], [])

    def test_sig_tamper_detected(self) -> None:
        ident = _mint_identity("nSigTamper0001")
        s = self.signing_store("nSigTamper0001", ident)
        s.publish_pubkey(ident["ed25519_pub_hex"])
        appended = s.append_coord_event("heartbeat", "point", summary="Victim row.", payload={"ts": "x"})
        self._tamper(s, "UPDATE coord_events SET sig = ? WHERE id = ?", ("ab" * 64, appended["id"]))
        report = ledger_verify.verify_ledger(s)
        self.assertIn(appended["id"], report["invalid_sig"])

    def test_verification_is_read_side_only_without_record(self) -> None:
        ident = _mint_identity("nReadonly00001")
        s = self.signing_store("nReadonly00001", ident)
        s.publish_pubkey(ident["ed25519_pub_hex"])
        appended = s.append_coord_event("heartbeat", "point", summary="Victim row.", payload={"ts": "x"})
        self._tamper(s, "UPDATE coord_events SET sig = ? WHERE id = ?", ("ab" * 64, appended["id"]))
        before = len(s.coord_events())
        ledger_verify.verify_ledger(s)  # record defaults False
        self.assertEqual(len(s.coord_events()), before)  # NEVER a write gate; nothing appended


# --- 2. Control-Ingress invariant (structural) -----------------------------------------------------

_SANCTIONED_LISTENERS = {
    Path("fleet") / "control_http.py",       # tailnet-gated, Ed25519-signed command ingress
    Path("orchestration") / "loopback.py",   # owner-only local pipe/UDS/loopback-TCP ingress
    # M-CLAUDE (M5b): 127.0.0.1-only, READ-ONLY OTLP telemetry ingest. NOT a command surface -- it
    # parses redacted enrichment events and never routes a mutation/command through decide(); the
    # loopback-only bind is asserted by test_otel_rx_binds_loopback_only below.
    Path("orchestration") / "otel_rx.py",
}

_LISTENER_RE = re.compile(r"ThreadingHTTPServer\(|HTTPServer\(|\.listen\(|\.bind\(")


class StructuralIngressTest(unittest.TestCase):
    def _package_sources(self):
        """Yield each package Python file's relative path and source text for static invariant checks."""
        for path in sorted(PKG_ROOT.rglob("*.py")):
            yield path.relative_to(PKG_ROOT), path.read_text(encoding="utf-8", errors="replace")

    def test_only_sanctioned_modules_listen(self) -> None:
        # Structural Control-Ingress invariant: NO module outside the two sanctioned servers constructs
        # a listening socket -- which is ALSO the "closed-source/third-party ingress structurally
        # impossible" proof: with exactly one Ed25519-signed tailnet-gated server and one owner-only
        # loopback server, there is no surface a third-party client could command (any notifier the
        # future adds is egress-only by this same assertion). Connect-side socket use -- sync client,
        # tailscaled probe, backend HTTP -- is egress and does not match: bind/listen/HTTPServer
        # construction only. Comment lines are stripped to avoid false hits.
        offenders: list[str] = []
        for rel, source in self._package_sources():
            code_lines = [
                line for line in source.splitlines()
                if not line.lstrip().startswith("#") and _LISTENER_RE.search(line)
            ]
            if code_lines and rel not in _SANCTIONED_LISTENERS:
                offenders.append(f"{rel}: {code_lines[:2]}")
        self.assertEqual(offenders, [], f"unsanctioned listener construct(s): {offenders}")

    def test_loopback_tcp_fallback_binds_loopback_only(self) -> None:
        source = (PKG_ROOT / "orchestration" / "loopback.py").read_text(encoding="utf-8")
        # Every AF_INET bind tuple in the module names 127.0.0.1 explicitly (never 0.0.0.0/"" wildcard).
        inet_binds = re.findall(r"\.bind\(\((.*?)\)\)", source)
        self.assertTrue(inet_binds, "expected at least the TCP fallback bind")
        for args in inet_binds:
            self.assertIn("127.0.0.1", args, f"non-loopback TCP bind: {args}")
        self.assertNotIn("0.0.0.0", source)

    def test_otel_rx_binds_loopback_only(self) -> None:
        # The sanctioned OTEL receiver (M5b) must stay a 127.0.0.1-only, READ-ONLY enrichment listener:
        # the host default is loopback, no wildcard bind, and it never routes a mutation through the
        # policy engine (no decide()/record_ask/append_coord_event) -- a future edit that widened the
        # bind or added a command path would trip here, keeping the Control-Ingress invariant honest.
        import inspect

        from kaizen_components.orchestration import otel_rx

        source = (PKG_ROOT / "orchestration" / "otel_rx.py").read_text(encoding="utf-8")
        signature = inspect.signature(otel_rx.OtelReceiver.__init__)
        self.assertEqual(signature.parameters["host"].default, "127.0.0.1")
        self.assertNotIn("0.0.0.0", source)
        for command_surface in (".decide(", "record_ask", "append_coord_event", "write_tx"):
            self.assertNotIn(command_surface, source,
                             f"otel_rx must stay read-only enrichment (found {command_surface})")



class UnsignedMutationRefusedEverywhereTest(_ServiceHarness):
    def test_every_mutating_route_refuses_unsigned_post(self) -> None:
        # Fleet-wide form of the M11 heartbeat leg: EVERY signed route refuses a bare (unsigned) POST
        # with a structured DENIED -- there is no unsigned command ingress on the control surface.
        for route in ("/v1/heartbeat", "/v1/lease/claim", "/v1/lease/release", "/v1/dispatch", "/v1/steer", "/v1/cancel"):
            resp = control_http._post(self.base, route, {"body": {"scope_key": "p/x"}}, 5.0)
            self.assertEqual(resp.get("status"), "DENIED", route)
            self.assertTrue(str(resp.get("code", "")).startswith("DENIED_"), route)


# --- 3. forged origin ------------------------------------------------------------------------------

class ForgedOriginTest(_ServiceHarness):
    def test_body_origin_never_overrides_envelope_attribution(self) -> None:
        resp = control_http.signed_post(
            self.base, "/v1/heartbeat", {"origin_node": "nEvil000000001"}, self.client_ident
        )
        self.assertEqual(resp.get("status"), "OK")
        beats = [
            e for e in self.store.coord_events()
            if e["event_kind"] == "heartbeat" and (e.get("payload") or {}).get("via") == "control-http"
        ]
        self.assertEqual(len(beats), 1)
        # Attribution = the VERIFIED envelope identity, never the body claim.
        self.assertEqual(beats[0]["payload"]["origin_node"], self.client_ident["node_id"])
        self.assertNotIn("nEvil000000001", str(beats[0]["payload"]))

    def test_release_authz_uses_verified_identity_not_body_claim(self) -> None:
        # Server (coordinator) grants the CLIENT a lease; a STRANGER with a registered key tries to
        # release it while body-claiming to be the client -> DENIED_NOT_HOLDER (authz reads the
        # envelope), NOT a signature error and NOT a successful release.
        coordination.claim_coordinator(self.store, summary="Server claims.")
        coordination.grant_lease(self.store, "p/held", self.client_ident["node_id"], ttl_s=10**9, summary="Grant to client.")
        stranger = _mint_identity("nStranger00001")
        self._register_pubkey(stranger["node_id"], stranger["ed25519_pub_hex"])
        resp = control_http.signed_post(
            self.base, "/v1/lease/release",
            {"scope_key": "p/held", "origin_node": self.client_ident["node_id"]},
            stranger,
        )
        self.assertEqual(resp.get("code"), "DENIED_NOT_HOLDER")
        # The client itself (verified envelope) CAN release its own lease.
        ok = control_http.signed_post(self.base, "/v1/lease/release", {"scope_key": "p/held"}, self.client_ident)
        self.assertEqual(ok.get("status"), "OK")


# --- 4. hub-push invariant fleet-wide ---------------------------------------------------------------

class HubPushInvariantFleetWideTest(unittest.TestCase):
    def test_handoff_engine_push_and_wip_default_off(self) -> None:
        import inspect

        sig = inspect.signature(coordination.HandoffEngine.__init__)
        self.assertIs(sig.parameters["allow_push"].default, False)
        self.assertIs(sig.parameters["allow_wip_commit"].default, False)

    def test_no_call_site_enables_push_or_publish(self) -> None:
        # The publish/push gates are owner-gated: NO CALL SITE inside the shipped package passes
        # allow_publish=True or allow_push=True (labs/owner enable them explicitly OUTSIDE the
        # package). AST-level scan of real keyword arguments -- docstrings/comments cannot false-hit.
        import ast

        offenders: list[str] = []
        for path in sorted(PKG_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg in ("allow_publish", "allow_push") and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        offenders.append(f"{path.relative_to(PKG_ROOT)}:{node.lineno} {kw.arg}=True")
        self.assertEqual(offenders, [])

    def test_mirror_module_is_push_free(self) -> None:
        # S17: push capability is absent from mirror.py ENTIRELY (hub push lives only behind
        # HandoffEngine.allow_push). Every operation passed to the module's GitRunner seam must be a
        # static token so a constructed/variable push cannot evade this proof.
        import ast

        source = (PKG_ROOT / "fleet" / "mirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        operations: list[str] = []
        dynamic: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "_git_out":
                continue
            if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant) or not isinstance(node.args[1].value, str):
                dynamic.append(node.lineno)
            else:
                operations.append(node.args[1].value.casefold())
        self.assertTrue(operations, "expected mirror GitRunner operations")
        self.assertEqual(dynamic, [], f"dynamic mirror GitRunner operation(s): {dynamic}")
        self.assertNotIn("push", operations)

    def test_policy_denies_git_push_identically_across_lanes_and_dispatch(self) -> None:
        engine = P.PolicyEngine(protected_paths=[], rules=[])
        actors = [
            ("codex", "local-session"),
            ("local_llm", "local-session"),
            ("codex", "dispatched-rd_20260710_abc"),      # a remotely-dispatched session's actor
            ("local_llm", "dispatched-rd_20260710_def"),
        ]
        for engine_name, session in actors:
            decision = engine.decide(
                _action(engine_name, session, "exec", command="git push origin main"), 1
            )
            self.assertEqual(decision.result, "deny", (engine_name, session))
            self.assertEqual(decision.invariant_id, P.INV_GIT_PUSH, (engine_name, session))

    def test_supervisor_loopback_and_daemon_expose_no_push_op(self) -> None:
        import ast

        sup = (PKG_ROOT / "orchestration" / "supervisor.py").read_text(encoding="utf-8")
        cli = (PKG_ROOT / "orchestration" / "daemon_cli.py").read_text(encoding="utf-8")
        # No loopback op name carries push/publish semantics.
        fleet_ops = re.findall(r'"(fleet/[a-z-]+)"', sup)
        self.assertTrue(fleet_ops)
        for op in fleet_ops:
            self.assertNotIn("push", op)
            self.assertNotIn("publish", op)
        # The daemon CLI exposes no push/publish parser verb; prose and identifiers are irrelevant.
        tree = ast.parse(cli)
        verbs = {
            node.args[0].value.casefold()
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_parser"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
        self.assertTrue(verbs, "expected daemon argparse verbs")
        self.assertTrue({"push", "publish"}.isdisjoint(verbs), verbs)
        # Positive gate evidence: the two supervisor seams that COULD push/publish pass explicit False.
        self.assertIn("allow_push=False", sup)
        self.assertIn("allow_publish=False", sup)


# --- 5. cross-node protected-path write denied ------------------------------------------------------

class CrossNodeProtectedPathTest(unittest.TestCase):
    PREFIX = "D:/protected-lab-root"

    def test_protected_write_denied_identically_for_local_and_dispatched_actors(self) -> None:
        engine = P.PolicyEngine(protected_paths=[self.PREFIX], rules=[])
        target = f"{self.PREFIX}/secrets/cred.txt"
        for engine_name, session in (
            ("codex", "local-session"),
            ("codex", "dispatched-rd_20260710_abc"),
            ("local_llm", "dispatched-rd_20260710_def"),
        ):
            decision = engine.decide(_action(engine_name, session, "file_write", targets=(target,)), 1)
            self.assertEqual(decision.result, "deny", (engine_name, session))
            self.assertEqual(decision.invariant_id, P.INV_PROTECTED_PATH, (engine_name, session))
        # Shell-string write evidence to the same prefix denies too (the opaque-shell net).
        shell = engine.decide(
            _action("codex", "dispatched-rd_x", "exec", command=f'cmd /c echo pwned > "{target}"'), 1
        )
        self.assertEqual(shell.result, "deny")
        self.assertEqual(shell.invariant_id, P.INV_PROTECTED_PATH)

    def test_dispatch_executor_fails_truthfully_when_runner_is_denied(self) -> None:
        # The M14 executor seam on the TARGET node: a runner whose action is policy-denied raises; the
        # dispatch must fail TRUTHFULLY (terminal failed + reason), never complete, never half-apply.
        from kaizen_components.fleet import dispatch_remote

        root = Path(tempfile.mkdtemp(prefix="kaizen-m16-disp-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        origin = FleetStore(db_path=root / "fleet.db", node={"node_id": "nOrigin0000001", "created_at": "t"})
        self.addCleanup(origin.close)
        coordination.claim_coordinator(origin, summary="Origin claims.")
        coordination.grant_lease(origin, "p/task", "nOrigin0000001", ttl_s=10**9, summary="Origin holds.")
        req = dispatch_remote.request_dispatch(
            origin, target_node="nTarget0000001", task="protected-write", scope_key="p/task", summary="Dispatch."
        )
        dispatch_id = req["dispatch_id"]
        target = FleetStore(db_path=root / "fleet.db", node={"node_id": "nTarget0000001", "created_at": "t"})
        self.addCleanup(target.close)
        engine = P.PolicyEngine(protected_paths=[self.PREFIX], rules=[])

        def denied_runner(_task: str, _workdir: str) -> dict:
            decision = engine.decide(
                _action("codex", f"dispatched-{dispatch_id}", "file_write", targets=(f"{self.PREFIX}/x",)), 1
            )
            if decision.result == "deny":
                raise KaizenDenied(
                    "DENIED_POLICY",
                    {"invariant_id": decision.invariant_id, "required_action": "denied by policy"},
                    exit_code=2,
                )
            return {}

        from kaizen_components.orchestration import supervisor as supervisor_module

        supervisor = supervisor_module.Supervisor(root)
        self.addCleanup(supervisor.shutdown)
        supervisor._fleet = target
        supervisor._dispatch_runner = denied_runner
        with mock.patch.object(supervisor_module, "RUNTIME_DIR", root / "runtime"):
            result = supervisor._poll_dispatches(max_per_poll=1)
        self.assertEqual(result["executed"], [{
            "dispatch_id": dispatch_id,
            "state": "failed",
            "reason": "runner error: KaizenDenied",
        }])
        state = dispatch_remote.current_dispatches(target)[dispatch_id]
        self.assertEqual(state["state"], "failed")
        failures = [
            event for event in target.coord_events()
            if event["event_kind"] == "dispatch" and event["marker"] == "failed"
            and (event.get("payload") or {}).get("dispatch_id") == dispatch_id
        ]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["payload"]["reason"], "runner error: KaizenDenied")


if __name__ == "__main__":
    unittest.main()
