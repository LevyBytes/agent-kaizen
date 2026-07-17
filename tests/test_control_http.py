"""HTTP control service + Ed25519 signing (v8 M11, plan §C.2).

Hermetic but REAL WIRE: real sockets on 127.0.0.1:0, real PyNaCl keys, real HTTP through the client
helpers (this is the milestone's live wire; the loopback lab is the contract's accepted local form). No
test touches the real AI/db plane and none leaks KAIZEN_* env:

- FleetStore is constructed with an explicit scratch ``db_path`` under a per-test temp dir and an
  INJECTED ``node`` identity dict (mirrors test_fleet_core's FleetStore(db_path=..., node=...) idiom), so
  no node_identity.json is minted and no real fleet.db is opened.
- The supervisor wiring leg runs the real ``kaizen.py daemon run`` in a SUBPROCESS pinned to a fresh
  KAIZEN_REPO_ROOT (the test_supervisor convention), so the env override never leaks into this process.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from _harness import REPO_ROOT, run

sys.path.insert(0, str(REPO_ROOT))

from kaizen_components.fleet import control_http, coordination, identity, reducers  # noqa: E402
from kaizen_components.fleet.store import FleetStore  # noqa: E402


def _mint_identity(node_id: str) -> dict:
    """A full signing identity dict (node_id + Ed25519 seed/pub hex) minted in-memory -- never written
    to a node_identity.json, so it is hermetic."""
    from nacl import signing

    key = signing.SigningKey.generate()
    return {
        "node_id": node_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "ed25519_seed_hex": key.encode().hex(),
        "ed25519_pub_hex": key.verify_key.encode().hex(),
    }


class _ServiceHarness(unittest.TestCase):
    """A running ControlService over an isolated scratch fleet.db, with a server node + a registered
    client node whose pubkey the server can resolve. Real socket on 127.0.0.1:0."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-m11-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.db_path = self.root / "fleet.db"
        self.server_ident = _mint_identity("nserver00000001")
        self.client_ident = _mint_identity("nclient00000001")
        self.store = FleetStore(db_path=self.db_path, node=self.server_ident)
        self.addCleanup(self.store.close)
        # Register the client's pubkey so the envelope verifier can resolve it (the server learns a peer
        # key via the synced nodes row; here we seed it directly).
        self._register_pubkey(self.client_ident["node_id"], self.client_ident["ed25519_pub_hex"])
        self.relay_seen: list[dict] = []
        self.service = control_http.ControlService(
            store=self.store,
            identity=self.server_ident,
            bind="127.0.0.1:0",
            relay=self._relay,
            tailnet_probe=lambda: True,
            logger=lambda _msg: None,
        )
        self.service.start()
        self.addCleanup(self.service.stop)
        host, port = self.service.address
        self.base = f"http://{host}:{port}"

    def _relay(self, request: dict) -> dict:
        self.relay_seen.append(request)
        return {"status": "DENIED", "code": "DENIED_UNKNOWN_OP", "echo": request}

    def _register_pubkey(self, node_id: str, pubkey_hex: str) -> None:
        from kaizen_components import db

        def op(conn):
            conn.execute(
                "INSERT INTO nodes (node_id, pubkey, registered_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET pubkey = excluded.pubkey",
                (node_id, pubkey_hex, db.now(), db.now()),
            )

        self.store._write(op)


# --- identity ------------------------------------------------------------------------------------

class SigningIdentityTest(unittest.TestCase):
    def test_ensure_signing_identity_mints_once_and_reloads_stable(self) -> None:
        # Point NODE_IDENTITY_PATH at a scratch file so no real identity is touched.
        from kaizen_components.fleet import identity as fid

        scratch = Path(tempfile.mkdtemp(prefix="kaizen-m11-id-"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        path = scratch / "node_identity.json"
        original = fid.NODE_IDENTITY_PATH
        fid.NODE_IDENTITY_PATH = path
        self.addCleanup(setattr, fid, "NODE_IDENTITY_PATH", original)

        a = fid.ensure_signing_identity()
        b = fid.ensure_signing_identity()
        self.assertEqual(a["ed25519_seed_hex"], b["ed25519_seed_hex"])  # minted once, stable on reload
        self.assertEqual(a["node_id"], b["node_id"])
        # Existing fields preserved; pub derives from seed.
        from nacl import signing

        key = signing.SigningKey(bytes.fromhex(a["ed25519_seed_hex"]))
        self.assertEqual(key.verify_key.encode().hex(), a["ed25519_pub_hex"])
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["ed25519_seed_hex"], a["ed25519_seed_hex"])
        self.assertIn("node_id", on_disk)

    def test_signing_key_from_none_without_seed(self) -> None:
        self.assertIsNone(identity.signing_key_from({"node_id": "n0"}))
        ident = _mint_identity("nx")
        self.assertIsNotNone(identity.signing_key_from(ident))


# --- bind guard ----------------------------------------------------------------------------------

class BindGuardTest(unittest.TestCase):
    def test_loopback_always_allowed(self) -> None:
        for host in ("127.0.0.1", "::1", "localhost"):
            control_http.assert_bind_allowed(host, lambda: False)  # no raise even off-tailnet

    def test_wildcard_refused(self) -> None:
        for host in ("", "0.0.0.0", "::"):
            with self.assertRaises(control_http.KaizenDenied) as ctx:
                control_http.assert_bind_allowed(host, lambda: True)
            self.assertEqual(ctx.exception.code, "DENIED_CONTROL_BIND_WILDCARD")

    def test_non_loopback_off_tailnet_refused(self) -> None:
        with self.assertRaises(control_http.KaizenDenied) as ctx:
            control_http.assert_bind_allowed("100.115.170.5", lambda: False)
        self.assertEqual(ctx.exception.code, "DENIED_CONTROL_BIND_OFF_TAILNET")

    def test_non_loopback_on_tailnet_ok(self) -> None:
        control_http.assert_bind_allowed("100.115.170.5", lambda: True)  # no raise


# --- probe + read --------------------------------------------------------------------------------

class ProbeTest(_ServiceHarness):
    def test_probe_unsigned_200(self) -> None:
        resp = control_http.probe(self.base)
        self.assertEqual(resp["status"], "OK")
        self.assertEqual(resp["node_id"], self.server_ident["node_id"])
        self.assertIn("GET /v1/probe", resp["endpoints"])


# --- signed heartbeat ----------------------------------------------------------------------------

class SignedHeartbeatTest(_ServiceHarness):
    def test_signed_heartbeat_attributes_origin(self) -> None:
        resp = control_http.signed_post(self.base, "/v1/heartbeat", {"capabilities": {"has_gpu": True}}, self.client_ident)
        self.assertEqual(resp["status"], "OK")
        events = [e for e in self.store.coord_events() if e["event_kind"] == "heartbeat" and e["marker"] == "point"]
        self.assertTrue(events)
        hb = events[-1]
        # Truthful appender attribution: the event node_id is the SERVER (receiver); payload.origin_node
        # is the CLIENT (who is actually alive).
        self.assertEqual(hb["node_id"], self.server_ident["node_id"])
        self.assertEqual(hb["payload"]["origin_node"], self.client_ident["node_id"])
        self.assertEqual(hb["payload"]["via"], "control-http")
        self.assertEqual(hb["payload"]["capabilities"], {"has_gpu": True})


# --- envelope refusals ---------------------------------------------------------------------------

class EnvelopeRefusalTest(_ServiceHarness):
    def test_unsigned_mutating_403(self) -> None:
        resp = control_http._post(self.base, "/v1/heartbeat", {"body": {}}, 5.0)
        self.assertEqual(resp["code"], "DENIED_CONTROL_UNSIGNED")

    def test_unknown_node_403(self) -> None:
        stranger = _mint_identity("nstranger000001")  # never registered on the server
        resp = control_http.signed_post(self.base, "/v1/heartbeat", {}, stranger)
        self.assertEqual(resp["code"], "DENIED_CONTROL_UNKNOWN_NODE")

    def test_wrong_key_signature_403(self) -> None:
        # Register the client node id but with a DIFFERENT pubkey than the signer's -> sig invalid.
        other = _mint_identity(self.client_ident["node_id"])
        self._register_pubkey(self.client_ident["node_id"], other["ed25519_pub_hex"])
        resp = control_http.signed_post(self.base, "/v1/heartbeat", {}, self.client_ident)
        self.assertEqual(resp["code"], "DENIED_CONTROL_SIG_INVALID")

    def test_tampered_body_403(self) -> None:
        wire = control_http.sign_request("/v1/heartbeat", {"scope_key": "orig"}, self.client_ident)
        wire["body"] = {"scope_key": "tampered"}  # sig no longer matches the body
        resp = control_http._post(self.base, "/v1/heartbeat", wire, 5.0)
        self.assertEqual(resp["code"], "DENIED_CONTROL_SIG_INVALID")

    def test_signing_normalizes_query_to_the_server_route(self) -> None:
        path = "/v1/heartbeat?client_note=ignored"
        wire = control_http.sign_request(path, {"capabilities": {}}, self.client_ident)
        response = control_http._post(self.base, path, wire, 5.0)
        self.assertEqual(response["status"], "OK")

    def test_captured_envelope_replay_403(self) -> None:
        # Explicit exit criterion: identical wire bytes twice -> the second is a replay.
        wire = control_http.sign_request("/v1/heartbeat", {}, self.client_ident)
        first = control_http._post(self.base, "/v1/heartbeat", wire, 5.0)
        second = control_http._post(self.base, "/v1/heartbeat", wire, 5.0)
        self.assertEqual(first["status"], "OK")
        self.assertEqual(second["code"], "DENIED_CONTROL_REPLAY")

    def test_stale_ts_403(self) -> None:
        wire = control_http.sign_request("/v1/heartbeat", {}, self.client_ident)
        # Re-sign with a ts far in the past (>skew). The canonical message binds ts, so re-sign it.
        from nacl import signing

        key = signing.SigningKey(bytes.fromhex(self.client_ident["ed25519_seed_hex"]))
        node_id = self.client_ident["node_id"]
        nonce = wire["envelope"]["nonce"]
        stale_ts = "2000-01-01T00:00:00+00:00"
        body = {}
        sig = key.sign(control_http.canonical_message(node_id, nonce, stale_ts, "/v1/heartbeat", body)).signature.hex()
        stale_wire = {"envelope": {"node_id": node_id, "nonce": nonce, "ts": stale_ts, "sig": sig}, "body": body}
        resp = control_http._post(self.base, "/v1/heartbeat", stale_wire, 5.0)
        self.assertEqual(resp["code"], "DENIED_CONTROL_TS_SKEW")

    def test_bad_sig_nonce_not_consumed(self) -> None:
        # A bad-sig request with nonce N must NOT poison the replay cache: a later VALID request reusing
        # N succeeds (the nonce is recorded only after the signature verifies).
        from nacl import signing

        key = signing.SigningKey(bytes.fromhex(self.client_ident["ed25519_seed_hex"]))
        node_id = self.client_ident["node_id"]
        nonce = "deadbeefdeadbeefdeadbeefdeadbeef"
        ts = control_http._utc_iso()
        body = {}
        good_sig = key.sign(control_http.canonical_message(node_id, nonce, ts, "/v1/heartbeat", body)).signature.hex()
        bad_wire = {"envelope": {"node_id": node_id, "nonce": nonce, "ts": ts, "sig": "00" * 64}, "body": body}
        bad = control_http._post(self.base, "/v1/heartbeat", bad_wire, 5.0)
        self.assertEqual(bad["code"], "DENIED_CONTROL_SIG_INVALID")
        good_wire = {"envelope": {"node_id": node_id, "nonce": nonce, "ts": ts, "sig": good_sig}, "body": body}
        good = control_http._post(self.base, "/v1/heartbeat", good_wire, 5.0)
        self.assertEqual(good["status"], "OK")  # the nonce was never consumed by the bad-sig attempt


# --- leases --------------------------------------------------------------------------------------

class LeaseClaimTest(_ServiceHarness):
    def test_claim_appends_request_and_never_grants(self) -> None:
        resp = control_http.signed_post(self.base, "/v1/lease/claim", {"scope_key": "p/main"}, self.client_ident)
        self.assertEqual(resp["status"], "OK")
        requested = [e for e in self.store.coord_events() if e["event_kind"] == "lease" and e["marker"] == "requested"]
        self.assertTrue(requested)
        self.assertEqual(requested[-1]["payload"]["origin_node"], self.client_ident["node_id"])
        # Digest/reduce shows NO holder (a claim never grants).
        leases = reducers.reduce_lease(self.store.coord_events())
        self.assertNotIn("p/main", {k for k, v in leases.items() if v.get("state") == "held"})


class LeaseClaimTtlGuardTest(_ServiceHarness):
    def test_garbage_ttl_is_structured_400(self) -> None:
        # Audit regression: a signed-but-garbage ttl_s must be a structured DENIED_CONTROL_MALFORMED
        # (400), never an untyped 500 from int().
        resp = control_http.signed_post(
            self.base, "/v1/lease/claim", {"scope_key": "p/main", "ttl_s": "not-a-number"}, self.client_ident
        )
        self.assertEqual(resp["code"], "DENIED_CONTROL_MALFORMED")


class LeaseReleaseTest(_ServiceHarness):
    def test_release_by_true_holder_ok_and_non_holder_409(self) -> None:
        # Make the server the coordinator, then grant the lease to the CLIENT (the holder).
        coordination.claim_coordinator(self.store, summary="c")
        coordination.grant_lease(self.store, "p/main", self.client_ident["node_id"], summary="g")
        # True holder (client) releases its own lease over HTTP -> OK.
        ok = control_http.signed_post(self.base, "/v1/lease/release", {"scope_key": "p/main"}, self.client_ident)
        self.assertEqual(ok["status"], "OK")
        # A different registered node that is NOT the holder -> 409 DENIED_NOT_HOLDER.
        coordination.grant_lease(self.store, "p/feat", self.client_ident["node_id"], summary="g2")
        stranger = _mint_identity("nother000000001")
        self._register_pubkey(stranger["node_id"], stranger["ed25519_pub_hex"])
        resp = control_http.signed_post(self.base, "/v1/lease/release", {"scope_key": "p/feat"}, stranger)
        self.assertEqual(resp["code"], "DENIED_NOT_HOLDER")


# --- dispatch (lease-gated at the reducer) --------------------------------------------------------

class DispatchTest(_ServiceHarness):
    def test_dispatch_without_lease_409(self) -> None:
        resp = control_http.signed_post(
            self.base, "/v1/dispatch",
            {"scope_key": "p/main", "target_node": "ntarget00000001", "task": "build"},
            self.client_ident,
        )
        self.assertEqual(resp["code"], "DENIED_DISPATCH_UNLEASED")

    def test_dispatch_with_granted_lease_appends(self) -> None:
        coordination.claim_coordinator(self.store, summary="c")
        coordination.grant_lease(self.store, "p/main", self.client_ident["node_id"], summary="g")
        resp = control_http.signed_post(
            self.base, "/v1/dispatch",
            {"scope_key": "p/main", "target_node": "ntarget00000001", "task": "build"},
            self.client_ident,
        )
        self.assertEqual(resp["status"], "OK")
        dispatched = [e for e in self.store.coord_events() if e["event_kind"] == "dispatch" and e["marker"] == "requested"]
        self.assertTrue(dispatched)
        payload = dispatched[-1]["payload"]
        self.assertEqual(payload["origin_node"], self.client_ident["node_id"])
        self.assertEqual(payload["target_node"], "ntarget00000001")


# --- events (read + long-poll) -------------------------------------------------------------------

class EventsTest(_ServiceHarness):
    def test_full_read_and_cursor_advance(self) -> None:
        self.store.append_coord_event("heartbeat", "point", summary="one", payload={"ts": "t1"})
        self.store.append_coord_event("heartbeat", "point", summary="two", payload={"ts": "t2"})
        first = control_http.get_events(self.base)
        self.assertEqual(first["status"], "OK")
        self.assertGreaterEqual(len(first["events"]), 2)
        cursor = first["cursor"]
        # A read from the cursor returns nothing new.
        again = control_http.get_events(self.base, since=cursor)
        self.assertEqual(again["events"], [])
        # Append one more; the cursor advances to it.
        self.store.append_coord_event("heartbeat", "point", summary="three", payload={"ts": "t3"})
        delta = control_http.get_events(self.base, since=cursor)
        self.assertEqual(len(delta["events"]), 1)

    def test_long_poll_delivers_mid_poll_append(self) -> None:
        cursor = control_http.get_events(self.base)["cursor"]

        def _append_later() -> None:
            time.sleep(0.6)
            self.store.append_coord_event("heartbeat", "point", summary="late", payload={"ts": "late"})

        thread = threading.Thread(target=_append_later)
        thread.start()
        resp = control_http.get_events(self.base, since=cursor, wait=5, timeout=10)
        thread.join()
        self.assertEqual(resp["status"], "OK")
        self.assertEqual(len(resp["events"]), 1)  # the mid-poll append was delivered

    def test_long_poll_timeout_returns_empty_truthfully(self) -> None:
        cursor = control_http.get_events(self.base)["cursor"]
        start = time.monotonic()
        resp = control_http.get_events(self.base, since=cursor, wait=1, timeout=10)
        elapsed = time.monotonic() - start
        self.assertEqual(resp["events"], [])  # truthful empty on timeout
        self.assertGreaterEqual(elapsed, 0.9)  # it actually waited

    def test_limit_is_capped_at_5000_without_resorting_store_rows(self) -> None:
        class CanonicalRows(list):
            def sort(self, *_args, **_kwargs):
                raise AssertionError("control service must trust FleetStore canonical order")

        rows = CanonicalRows({"created_at": f"2026-01-01T00:00:{index:05d}Z", "id": f"e-{index:05d}"}
                             for index in range(5_001))
        with mock.patch.object(self.store, "coord_events", return_value=rows):
            status, payload = self.service.handle_get("/v1/events", {"limit": ["999999"]})
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["events"]), 5_000)
        self.assertEqual(payload["events"][0]["id"], "e-00000")
        self.assertEqual(payload["events"][-1]["id"], "e-04999")


# --- steer / cancel relay ------------------------------------------------------------------------

class SteerCancelTest(_ServiceHarness):
    def test_relay_sees_op_args_origin_and_surfaces_verbatim(self) -> None:
        resp = control_http.signed_post(self.base, "/v1/steer", {"instruction": "focus"}, self.client_ident)
        self.assertEqual(resp["code"], "DENIED_UNKNOWN_OP")  # surfaced verbatim from the injected relay
        self.assertTrue(self.relay_seen)
        seen = self.relay_seen[-1]
        self.assertEqual(seen["op"], "steer")
        self.assertEqual(seen["args"], {"instruction": "focus"})
        self.assertEqual(seen["origin_node"], self.client_ident["node_id"])

    def test_relay_exception_is_http_500_error(self) -> None:
        # Audit regression: a relay FAULT ({status: ERROR, code: ERROR_CONTROL_INTERNAL}) must ride an
        # HTTP 500, never a 200 -- only OK results are 200.
        def _boom(_request: dict) -> dict:
            raise RuntimeError("relay exploded")

        svc = control_http.ControlService(
            store=self.store, identity=self.server_ident, bind="127.0.0.1:0",
            relay=_boom, tailnet_probe=lambda: True, logger=lambda _m: None,
        )
        svc.start()
        self.addCleanup(svc.stop)
        host, port = svc.address
        wire = control_http.sign_request("/v1/steer", {"x": 1}, self.client_ident)
        raw = json.dumps(wire).encode("utf-8")
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            f"http://{host}:{port}/v1/steer", data=raw, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 500)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(body["code"], "ERROR_CONTROL_INTERNAL")

    def test_no_relay_configured_denies(self) -> None:
        # A service with relay=None refuses steer/cancel with DENIED_CONTROL_NO_SUPERVISOR.
        svc = control_http.ControlService(
            store=self.store, identity=self.server_ident, bind="127.0.0.1:0",
            relay=None, tailnet_probe=lambda: True, logger=lambda _m: None,
        )
        svc.start()
        self.addCleanup(svc.stop)
        host, port = svc.address
        resp = control_http.signed_post(f"http://{host}:{port}", "/v1/cancel", {}, self.client_ident)
        self.assertEqual(resp["code"], "DENIED_CONTROL_NO_SUPERVISOR")


# --- malformed / unknown / oversized -------------------------------------------------------------

class MalformedTest(_ServiceHarness):
    def test_malformed_json_400(self) -> None:
        # Raw non-JSON body on a POST.
        resp = self._raw_post("/v1/heartbeat", b"not-json-at-all")
        self.assertEqual(resp["status_code"], 400)
        self.assertEqual(resp["body"]["code"], "DENIED_CONTROL_MALFORMED")

    def test_unknown_endpoint_404(self) -> None:
        resp = self._raw_post("/v1/nope", json.dumps({"body": {}}).encode("utf-8"))
        self.assertEqual(resp["status_code"], 404)
        self.assertEqual(resp["body"]["code"], "DENIED_CONTROL_UNKNOWN_ENDPOINT")

    def test_oversized_body_413(self) -> None:
        big = b"x" * ((1 << 20) + 10)  # > 1 MiB
        resp = self._raw_post("/v1/heartbeat", big)
        self.assertEqual(resp["status_code"], 413)
        self.assertEqual(resp["body"]["code"], "DENIED_CONTROL_BODY_TOO_LARGE")

    def test_invalid_or_negative_content_length_is_400(self) -> None:
        for length in ("not-an-integer", "-1"):
            with self.subTest(length=length):
                response = self._raw_post("/v1/heartbeat", b"", content_length=length)
                self.assertEqual(response["status_code"], 400)
                self.assertEqual(response["body"]["code"], "DENIED_CONTROL_MALFORMED")

    def _raw_post(self, path: str, data: bytes, *, content_length: str | None = None) -> dict:
        """A minimal raw HTTP/1.1 POST returning {status_code, body} so the test can assert the HTTP
        status code (the urllib helpers collapse to the JSON body)."""
        host, port = self.service.address
        with socket.create_connection((host, port), timeout=5) as sock:
            request = (
                f"POST {path} HTTP/1.1\r\nHost: {host}\r\nContent-Length: {content_length or len(data)}\r\n"
                "Content-Type: application/json\r\nConnection: close\r\n\r\n"
            ).encode("utf-8") + data
            sock.sendall(request)
            raw = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                raw += chunk
        head, _, payload = raw.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        status_code = int(status_line.split()[1])
        return {"status_code": status_code, "body": json.loads(payload.decode("utf-8"))}


# --- store: publish_pubkey / signed append -------------------------------------------------------

class StoreSigningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-m11-store-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.db_path = self.root / "fleet.db"

    def test_publish_pubkey_upsert_and_event(self) -> None:
        ident = _mint_identity("nsign0000000001")
        store = FleetStore(db_path=self.db_path, node=ident)
        self.addCleanup(store.close)
        result = store.publish_pubkey(ident["ed25519_pub_hex"])
        self.assertEqual(result["status"], "OK")
        self.assertEqual(store.node_pubkey(ident["node_id"]), ident["ed25519_pub_hex"])
        # A node/updated event carries the pubkey.
        events = [e for e in store.coord_events() if e["event_kind"] == "node" and e["marker"] == "updated"]
        self.assertTrue(events)
        self.assertEqual(events[-1]["payload"]["pubkey"], ident["ed25519_pub_hex"])
        # Upsert: a second publish updates in place (still one nodes row for this id).
        store.publish_pubkey(ident["ed25519_pub_hex"])
        rows = store._read_all("SELECT COUNT(*) FROM nodes WHERE node_id = ?", (ident["node_id"],))
        self.assertEqual(rows[0][0], 1)

    def test_append_signs_when_seed_present(self) -> None:
        ident = _mint_identity("nsign0000000002")
        store = FleetStore(db_path=self.db_path, node=ident)
        self.addCleanup(store.close)
        result = store.append_coord_event("heartbeat", "point", summary="hb", payload={"ts": "t"})
        self.assertTrue(result["signed"])
        row = store._read_all("SELECT content_hash, sig FROM coord_events WHERE id = ?", (result["id"],))[0]
        content_hash, sig = row
        self.assertIsNotNone(sig)
        # Test-side verification: the hex sig verifies against the pub over the content_hash bytes.
        from nacl import signing

        verify_key = signing.VerifyKey(bytes.fromhex(ident["ed25519_pub_hex"]))
        verify_key.verify(content_hash.encode("utf-8"), bytes.fromhex(sig))  # no raise == valid

    def test_append_no_seed_leaves_sig_none(self) -> None:
        # An injected node WITHOUT a seed produces the byte-identical legacy path: sig NULL.
        store = FleetStore(db_path=self.db_path, node={"node_id": "nnoseed00000001"})
        self.addCleanup(store.close)
        result = store.append_coord_event("heartbeat", "point", summary="hb", payload={"ts": "t"})
        self.assertFalse(result["signed"])
        row = store._read_all("SELECT sig FROM coord_events WHERE id = ?", (result["id"],))[0]
        self.assertIsNone(row[0])


# --- coordination regression + store lock --------------------------------------------------------

class CoordinationRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-m11-co-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.store = FleetStore(db_path=self.root / "fleet.db", node=_mint_identity("nco00000000001"))
        self.addCleanup(self.store.close)

    def test_request_lease_no_origin_node_key_when_kwarg_absent(self) -> None:
        # The additive origin_node kwarg must default to byte-identical pre-M11 behavior: no key.
        coordination.request_lease(self.store, "p/x", summary="r")
        requested = [e for e in self.store.coord_events() if e["event_kind"] == "lease" and e["marker"] == "requested"]
        self.assertTrue(requested)
        self.assertNotIn("origin_node", requested[-1]["payload"])


class StoreLockSmokeTest(unittest.TestCase):
    def test_concurrent_appends_all_land(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="kaizen-m11-lock-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        store = FleetStore(db_path=root / "fleet.db", node=_mint_identity("nlock0000000001"))
        self.addCleanup(store.close)
        errors: list[str] = []
        n_threads = 8
        per_thread = 5

        def worker(tag: int) -> None:
            try:
                for i in range(per_thread):
                    store.append_coord_event("heartbeat", "point", summary=f"t{tag}-{i}", payload={"ts": f"{tag}-{i}"})
            except Exception as error:  # noqa: BLE001 -- collect, assert clean below
                errors.append(str(error))

        threads = [threading.Thread(target=worker, args=(t,), daemon=True) for t in range(n_threads)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 15
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        self.assertEqual([thread.name for thread in threads if thread.is_alive()], [], "writer threads deadlocked")
        self.assertEqual(errors, [], f"concurrent append raised: {errors}")
        count = store._read_all("SELECT COUNT(*) FROM coord_events WHERE event_kind = 'heartbeat'")[0][0]
        self.assertEqual(count, n_threads * per_thread)  # every row landed, no lost writes


# --- supervisor wiring (real daemon boot in a subprocess, scratch plane) --------------------------

class SupervisorWiringTest(unittest.TestCase):
    """Drives the REAL supervisor via a small in-subprocess script pinned to a fresh KAIZEN_REPO_ROOT
    (test_supervisor convention: never touch the real plane, never leak env). With
    KAIZEN_CONTROL_BIND=127.0.0.1:0 + KAIZEN_DIST_MODE=observe the boot payload reports control.active
    and a real probe hits the bound port; steer over HTTP relays to the REAL _handle_control (proving the
    relay seam). As of M14 the steer handler is LIVE, so a steer for an UNKNOWN run surfaces the truthful
    DENIED_AGENT_RUN_NOT_FOUND (the seam reaches the real handler and gets a real structured answer -- no
    longer DENIED_UNKNOWN_OP). Without the env, control is absent."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-m11-sup-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        rc = run(self.root, "K1", "--json", timeout=60).returncode
        self.assertEqual(rc, 0)

    def _drive(self, body: str, env: dict) -> dict:
        """Run a bounded child in the scratch plane, require zero exit, and return its RESULT payload."""
        script = "BODY = " + repr(body) + "\n" + _SUP_PREAMBLE
        full_env = dict(os.environ)
        full_env["KAIZEN_REPO_ROOT"] = str(self.root)
        full_env.update(env)
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(REPO_ROOT), env=full_env,
            timeout=120,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"supervisor child exited {proc.returncode}.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-4000:]}",
        )
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        self.fail(f"no RESULT.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")

    def test_control_active_and_probe_and_relay_seam(self) -> None:
        out = self._drive(
            "sup = Supervisor()\n"
            "boot = sup.boot()\n"
            "ctrl = boot['control']\n"
            "addr = ctrl.get('address')\n"
            "probe = control_http.probe('http://' + addr) if addr else None\n"
            "# steer over HTTP relays to the REAL _handle_control. M14: the handler is live, so a steer\n"
            "# for an UNKNOWN run answers DENIED_AGENT_RUN_NOT_FOUND -- the seam reached the real handler.\n"
            "ident = fid.ensure_signing_identity()\n"
            "steer = control_http.signed_post('http://' + addr, '/v1/steer', {'agent_run_id': 'nope-no-such-run', 'instruction': 'focus'}, ident) if addr else None\n"
            "port_open = _port_open(addr)\n"
            "sup.shutdown()\n"
            "closed = not _port_open(addr)\n"
            "out = {'active': ctrl['active'], 'probe_ok': (probe or {}).get('status'), 'steer_code': (steer or {}).get('code'), 'port_open': port_open, 'closed_after_shutdown': closed}\n",
            env={"KAIZEN_DIST_MODE": "observe", "KAIZEN_CONTROL_BIND": "127.0.0.1:0"},
        )
        self.assertTrue(out["active"])
        self.assertEqual(out["probe_ok"], "OK")
        # M14: the real (now-live) steer handler answers a truthful unknown-run denial through the relay
        # seam (the seam still reaches _handle_control; the truthful response is what changed).
        self.assertEqual(out["steer_code"], "DENIED_AGENT_RUN_NOT_FOUND")  # the real relay seam answered
        self.assertTrue(out["port_open"])
        self.assertTrue(out["closed_after_shutdown"])  # shutdown stopped the server (port closed)

    def test_control_absent_without_env(self) -> None:
        out = self._drive(
            "sup = Supervisor()\n"
            "boot = sup.boot()\n"
            "sup.shutdown()\n"
            "out = {'control': boot['control']}\n",
            env={"KAIZEN_DIST_MODE": "observe"},  # no KAIZEN_CONTROL_BIND
        )
        self.assertFalse(out["control"]["active"])
        self.assertIsNone(out["control"]["address"])


_SUP_PREAMBLE = r"""
import json, socket, sys
from kaizen_components.orchestration.supervisor import Supervisor
from kaizen_components.fleet import control_http
from kaizen_components.fleet import identity as fid

def _port_open(addr):
    if not addr:
        return False
    host, port = addr.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=1):
            return True
    except OSError:
        return False

out = None
exec(BODY)
print("RESULT " + json.dumps(out))
"""


if __name__ == "__main__":
    unittest.main()
