"""HTTP control service + Ed25519-signed request envelopes (v8 M11, plan §C.2).

NON-AUTHORITATIVE live control. Every MUTATION this service accepts APPENDS one coord_event through the
FleetStore's single write path; the PURE reducers decide truth. The service NEVER mutates authoritative
state directly (no lease is granted here, no nodes row is force-set to "held") -- it records a request
and lets :mod:`fleet.reducers` / the coordinator resolve it. It is the wire M11 lands so a peer on the
tailnet can drive the daemon over HTTP; the loopback lab remains the contract's accepted local form.

Trust model:
- Read-only endpoints (``/v1/probe``, ``/v1/events``) are UNSIGNED (health + ledger tail).
- Every mutating POST carries an Ed25519 envelope ``{node_id, nonce, ts, sig}`` over the canonical
  message (path + envelope fields + body). The signer's pubkey is resolved from the synced nodes row
  (distributed as tailnet-membership bootstrap: publish_pubkey rides the pub half out on fleet.db).
- The bind guard refuses a non-loopback bind unless this node is on the tailnet (a control port must
  never be exposed on a public interface); a wildcard bind is always refused.

Stdlib only (http.server ThreadingHTTPServer + urllib client). fleet/* is import-guard allowlisted for
process/network capability. Signature VERIFICATION of PULLED coord_events is a SEPARATE concern (M16) --
this module verifies only the live request envelope, not historical ledger rows.
"""

from __future__ import annotations

import http.client
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit

from ..denials import KaizenDenied
from . import net, reducers

# The env var an operator sets to enable + address the control service (supervisor reads it at boot).
CONTROL_BIND_ENV = "KAIZEN_CONTROL_BIND"

# Request body hard cap -- a hostile client must not stream an unbounded body into memory.
_MAX_BODY_BYTES = 1 << 20  # 1 MiB
# How much of an OVERSIZED body the 413 path drains before closing: enough that a legitimate client
# finishing its write can read the 413 (never a mid-write reset), bounded so a hostile Content-Length
# cannot make the server read forever.
_MAX_DRAIN_BYTES = 8 << 20  # 8 MiB
_MAX_EVENTS_LIMIT = 5000

# Loopback host spellings that are always allowed to bind (no tailnet needed).
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
# Wildcard binds (all interfaces) are always refused -- a control port is never world-facing.
_WILDCARD_HOSTS = {"", "0.0.0.0", "::"}

# HTTP status mapping by denial family. Auth failures are 403 (forbidden), contention is 409 (conflict),
# everything else structured is 400.
_AUTH_CODES = {
    "DENIED_CONTROL_UNSIGNED",
    "DENIED_CONTROL_SIG_INVALID",
    "DENIED_CONTROL_UNKNOWN_NODE",
    "DENIED_CONTROL_REPLAY",
    "DENIED_CONTROL_TS_SKEW",
}
_CONTENTION_CODES = {
    "DENIED_DISPATCH_UNLEASED",
    "DENIED_NOT_HOLDER",
    "DENIED_LEASE_NOT_HELD",
    "DENIED_LEASE_HELD",
}

_ENDPOINTS = (
    "GET /v1/probe",
    "GET /v1/health",
    "GET /v1/events",
    "POST /v1/heartbeat",
    "POST /v1/lease/claim",
    "POST /v1/lease/release",
    "POST /v1/dispatch",
    "POST /v1/steer",
    "POST /v1/cancel",
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- canonical signing message -------------------------------------------------------------------

def canonical_message(node_id: str, nonce: str, ts: str, path: str, body: Any) -> bytes:
    """The exact bytes an envelope signs (plan §C.2). ``path`` + the envelope fields (node_id, nonce,
    ts) are bound INTO the signed message alongside the body: an envelope minted for one endpoint cannot
    be replayed against another (path is authenticated), and nonce/ts are authenticated too -- the plan
    says "over the canonical body", but leaving nonce/ts unbound would let an attacker forge them while
    reusing a captured signature, so they are folded in. Deterministic JSON (sorted keys, tight
    separators, ascii-escaped) so both sides serialize identically byte-for-byte."""
    return json.dumps(
        {"body": body, "node_id": node_id, "nonce": nonce, "path": path, "ts": ts},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sign_request(path: str, body: Any, identity: dict) -> dict[str, Any]:
    """Sign a request for the server-normalized route and return its wire envelope."""
    from . import identity as fleet_identity

    key = fleet_identity.signing_key_from(identity)
    if key is None:
        raise KaizenDenied(
            "DENIED_CONTROL_NO_SIGNING_KEY",
            {"required_action": "ensure_signing_identity() before signing a control request"},
            exit_code=2,
        )
    node_id = identity["node_id"]
    nonce = secrets.token_hex(16)
    ts = _utc_iso()
    sig = key.sign(canonical_message(node_id, nonce, ts, _route_of(path), body)).signature.hex()
    return {"envelope": {"node_id": node_id, "nonce": nonce, "ts": ts, "sig": sig}, "body": body}


# --- envelope verification -----------------------------------------------------------------------

class EnvelopeVerifier:
    """Verifies a mutating request's Ed25519 envelope (v8 M11, plan §C.2). Thread-safe (a lock guards
    the nonce cache) because the ThreadingHTTPServer verifies on many worker threads at once.

    Check ORDER is load-bearing: shape -> ts skew -> pubkey resolve -> signature -> nonce replay. The
    nonce is recorded ONLY AFTER the signature verifies, so an UNAUTHENTICATED attacker cannot poison the
    replay cache with a chosen nonce (a bad-sig request leaves the nonce reusable by the legitimate
    holder). The cache prunes expired entries on insert and is bounded (oldest evicted at max_nonces)."""

    def __init__(
        self,
        pubkey_resolver: Callable[[str], str | None],
        *,
        skew_s: float = 120.0,
        nonce_ttl_s: float = 300.0,
        max_nonces: int = 4096,
    ) -> None:
        self._resolve = pubkey_resolver
        self._skew_s = float(skew_s)
        self._nonce_ttl_s = float(nonce_ttl_s)
        self._max_nonces = int(max_nonces)
        self._lock = threading.Lock()
        # node_id|nonce -> monotonic insert time; pruned on insert, evicted oldest-first when full.
        self._seen: dict[str, float] = {}

    def verify(self, path: str, wire: Any) -> Any:
        """Verify the wire envelope for ``path``; return the request body on success, else raise a
        structured KaizenDenied (exit 2). Never records the nonce until the signature is valid."""
        if not isinstance(wire, dict):
            raise self._deny("DENIED_CONTROL_UNSIGNED", {"reason": "wire is not a JSON object"})
        envelope = wire.get("envelope")
        body = wire.get("body")
        if not isinstance(envelope, dict):
            raise self._deny("DENIED_CONTROL_UNSIGNED", {"reason": "missing envelope"})
        node_id = envelope.get("node_id")
        nonce = envelope.get("nonce")
        ts = envelope.get("ts")
        sig_hex = envelope.get("sig")
        if not (isinstance(node_id, str) and isinstance(nonce, str) and isinstance(ts, str) and isinstance(sig_hex, str)):
            raise self._deny("DENIED_CONTROL_UNSIGNED", {"reason": "envelope missing node_id/nonce/ts/sig"})

        # (1) timestamp skew -- cheap, pre-crypto reject of a stale/forward-dated envelope.
        skew = _ts_skew_seconds(ts)
        if skew is None:
            raise self._deny("DENIED_CONTROL_TS_SKEW", {"reason": "unparsable ts", "ts": ts})
        if skew > self._skew_s:
            raise self._deny(
                "DENIED_CONTROL_TS_SKEW",
                {"skew_s": round(skew, 3), "max_skew_s": self._skew_s, "ts": ts},
            )

        # (2) resolve the signer's pubkey (from the synced nodes row).
        pubkey_hex = self._resolve(node_id)
        if not pubkey_hex:
            raise self._deny("DENIED_CONTROL_UNKNOWN_NODE", {"node_id": node_id})

        # (3) Ed25519 verify over the canonical message. Any failure (bad sig, wrong key, tampered body,
        # bad hex) is one DENIED_CONTROL_SIG_INVALID -- do not leak which check failed.
        from nacl import signing  # noqa: PLC0415 -- lazy: only the verify path loads PyNaCl
        from nacl.exceptions import BadSignatureError

        message = canonical_message(node_id, nonce, ts, path, body)
        try:
            verify_key = signing.VerifyKey(bytes.fromhex(pubkey_hex))
            verify_key.verify(message, bytes.fromhex(sig_hex))
        except (BadSignatureError, ValueError):
            raise self._deny("DENIED_CONTROL_SIG_INVALID", {"node_id": node_id})

        # (4) replay: record the nonce ONLY now (post-verify), so a forged request never consumes it.
        self._record_nonce(node_id, nonce)
        return body

    def _record_nonce(self, node_id: str, nonce: str) -> None:
        key = f"{node_id}|{nonce}"
        now = time.monotonic()
        with self._lock:
            # Prune expired entries first so the cache does not grow unbounded with old nonces.
            if self._seen:
                cutoff = now - self._nonce_ttl_s
                expired = [k for k, t in self._seen.items() if t < cutoff]
                for k in expired:
                    del self._seen[k]
            if key in self._seen:
                raise self._deny("DENIED_CONTROL_REPLAY", {"node_id": node_id})
            # Bound the cache: evict the oldest entry when full (insertion order == age order).
            if len(self._seen) >= self._max_nonces:
                oldest = min(self._seen, key=self._seen.__getitem__)
                del self._seen[oldest]
            self._seen[key] = now

    @staticmethod
    def _deny(code: str, fields: dict[str, Any]) -> KaizenDenied:
        return KaizenDenied(code, {**fields, "required_action": "resend a fresh signed control envelope"}, exit_code=2)


def _ts_skew_seconds(ts: str) -> float | None:
    """Absolute seconds between ``ts`` (ISO) and now (UTC), or None when unparsable. A naive ts is
    treated as UTC (the signer emits UTC ISO)."""
    try:
        parsed = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return abs((datetime.now(timezone.utc) - parsed).total_seconds())


# --- bind guard ----------------------------------------------------------------------------------

def assert_bind_allowed(host: str, tailnet_probe: Callable[[], bool]) -> None:
    """Pure guard, run BEFORE binding (plan §C.2). Loopback (127.0.0.1/::1/localhost) is always allowed
    with no network. A wildcard (""/0.0.0.0/::) is always refused (a control port must never be exposed
    on every interface). Any other host requires ``tailnet_probe()`` True -- a non-loopback control bind
    is legitimate only on the tailnet, never a bare public interface."""
    normalized = (host or "").strip().lower()
    if normalized in _LOOPBACK_HOSTS:
        return
    if normalized in _WILDCARD_HOSTS:
        raise KaizenDenied(
            "DENIED_CONTROL_BIND_WILDCARD",
            {"host": host, "required_action": "bind 127.0.0.1 for local control, or a tailnet address"},
            exit_code=2,
        )
    if not tailnet_probe():
        raise KaizenDenied(
            "DENIED_CONTROL_BIND_OFF_TAILNET",
            {"host": host, "required_action": "bind a tailnet interface (join the tailnet) or use loopback"},
            exit_code=2,
        )


# --- the service ---------------------------------------------------------------------------------

class ControlService:
    """The M11 HTTP control service over a FleetStore. Boots a ThreadingHTTPServer on ``bind``, verifies
    Ed25519 envelopes on mutating POSTs, and appends coord_events for every accepted mutation (never
    grants/mutates authoritative state directly). ``.address`` exposes the bound (host, port) after
    start (supports :0 -> a real ephemeral port). ``stop()`` is idempotent.

    ``logger`` is a plain CALLABLE convention (the supervisor passes its ``log`` method) -- NEVER assume
    a logging.Logger. The BaseHTTPRequestHandler's log_message is overridden to route through it so
    stdout/stderr stay pristine (an M10b audit fix; do not regress to logging.Logger)."""

    def __init__(
        self,
        store: Any,
        identity: dict,
        *,
        bind: str,
        relay: Callable[[dict], dict] | None = None,
        tailnet_probe: Callable[[], bool] = net.on_tailnet,
        logger: Callable[[str], None] | None = None,
        long_poll_max_s: float = 25.0,
    ) -> None:
        self._store = store
        self._identity = identity
        self._relay = relay
        self._tailnet_probe = tailnet_probe
        self._logger = logger
        self._long_poll_max_s = float(long_poll_max_s)
        host, port = _split_bind(bind)
        self._bind_host = host
        self._bind_port = port
        self._verifier = EnvelopeVerifier(store.node_pubkey)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.address: tuple[str, int] | None = None
        self._started_monotonic: float | None = None

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> dict[str, Any]:
        """Guard the bind, publish this node's pubkey (key distribution bootstrap), then serve_forever on
        a daemon thread. The caller passes an ALREADY-ensured identity (ensure_signing_identity), so the
        pub half exists; publishing it upserts nodes.pubkey + appends node/updated so a peer that pulls
        fleet.db can verify this node's future envelopes."""
        assert_bind_allowed(self._bind_host, self._tailnet_probe)
        pub_hex = self._identity.get("ed25519_pub_hex")
        if pub_hex:
            self._store.publish_pubkey(pub_hex)
        handler = _make_handler(self)
        httpd = ThreadingHTTPServer((self._bind_host, self._bind_port), handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        self.address = httpd.server_address[0], httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, name="kaizen-control-http", daemon=True)
        thread.start()
        self._thread = thread
        self._started_monotonic = time.monotonic()
        self._log(f"control http listening on {self.address[0]}:{self.address[1]}")
        return {"status": "OK", "address": f"{self.address[0]}:{self.address[1]}"}

    def stop(self) -> None:
        """Idempotent shutdown: stop serve_forever, close the socket, join the thread."""
        httpd = self._httpd
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:  # noqa: BLE001 -- teardown must not raise
                pass
            try:
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._httpd = None
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None

    def _log(self, message: str) -> None:
        if self._logger is not None:
            try:
                self._logger(message)
            except Exception:  # noqa: BLE001 -- logging must never break a control path
                pass

    # --- request handling (called from the handler; returns (status, payload)) ------------------

    def handle_get(self, path: str, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        """Dispatch GET route -> (status, payload); unsigned reads only; unknown route -> 404 DENIED_CONTROL_UNKNOWN_ENDPOINT."""
        route = _route_of(path)
        if route == "/v1/probe":
            return 200, {
                "status": "OK",
                "node_id": self._store.node_id,
                "ts": _utc_iso(),
                "endpoints": list(_ENDPOINTS),
            }
        if route == "/v1/health":
            return self._handle_health()
        if route == "/v1/events":
            return self._handle_events(query)
        return 404, {"status": "DENIED", "code": "DENIED_CONTROL_UNKNOWN_ENDPOINT", "path": path}

    def _handle_health(self) -> tuple[int, dict[str, Any]]:
        """The M17 healthcheck: unsigned read-only (same trust as /v1/probe -- liveness probes carry no
        keys), appends NOTHING. Uptime + event count + the replica-view staleness block (fleet.metrics
        pure projections) -- enough for a monitor to alert on a wedged or stale node without auth."""
        from . import metrics

        events = self._store.coord_events()
        uptime = time.monotonic() - self._started_monotonic if self._started_monotonic is not None else 0.0
        return 200, {
            "status": "OK",
            "node_id": self._store.node_id,
            "ts": _utc_iso(),
            "uptime_s": round(max(uptime, 0.0), 3),
            "coord_events": len(events),
            "sync_staleness": metrics.sync_staleness(events, self._store.node_id, _utc_iso()),
            "synced": bool(getattr(getattr(self._store, "sync", None), "synced", False)),
        }

    def handle_post(self, path: str, wire: Any) -> tuple[int, dict[str, Any]]:
        """Verify the route-bound envelope, dispatch it, and map structured failures to HTTP status."""
        route = _route_of(path)
        signed_routes = {
            "/v1/heartbeat": self._op_heartbeat,
            "/v1/lease/claim": self._op_lease_claim,
            "/v1/lease/release": self._op_lease_release,
            "/v1/dispatch": self._op_dispatch,
            "/v1/steer": self._op_steer,
            "/v1/cancel": self._op_cancel,
        }
        handler = signed_routes.get(route)
        if handler is None:
            return 404, {"status": "DENIED", "code": "DENIED_CONTROL_UNKNOWN_ENDPOINT", "path": path}
        # Verify the envelope (route-bound canonical message). A refusal maps to its HTTP family below.
        verified_body = self._verifier.verify(route, wire)
        origin_node = wire["envelope"]["node_id"]
        result = handler(verified_body if isinstance(verified_body, dict) else {}, origin_node)
        # A handler may return a structured DENIED (contention) or ERROR (relay fault) payload; both map
        # through the status table -- an ERROR body must ride a 500, never a 200 (truthful HTTP status).
        if isinstance(result, dict) and result.get("status") in ("DENIED", "ERROR"):
            return _status_for_code(result.get("code")), result
        return 200, result

    # --- read: events (unsigned, long-poll-capable) ---------------------------------------------

    def _handle_events(self, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        since = _first(query, "since", "")
        wait = _float_arg(_first(query, "wait", "0"), 0.0)
        limit = min(max(1, int(_float_arg(_first(query, "limit", "500"), 500.0))), _MAX_EVENTS_LIMIT)
        events, cursor = self._events_since(since, limit)
        if events or wait <= 0:
            return 200, {"status": "OK", "events": events, "cursor": cursor}
        # Long-poll: re-poll the store every 0.5s up to min(wait, long_poll_max_s); on timeout return an
        # EMPTY events list truthfully (a timeout is a truthful "nothing new", not an error).
        deadline = time.monotonic() + min(wait, self._long_poll_max_s)
        while time.monotonic() < deadline:
            time.sleep(0.5)
            events, cursor = self._events_since(since, limit)
            if events:
                break
        return 200, {"status": "OK", "events": events, "cursor": cursor}

    def _events_since(self, since: str, limit: int) -> tuple[list[dict[str, Any]], str]:
        """Return coord_events strictly after the opaque cursor ``"<created_at>|<id>"`` (ordered by
        (created_at, id)), capped at ``limit``, plus the new cursor (the last returned row's pair, or the
        incoming cursor when nothing new). An absent/empty ``since`` starts from the beginning."""
        # FleetStore returns canonical (created_at, id) order; re-sorting every long-poll tick is wasteful.
        rows = self._store.coord_events()
        after = _parse_cursor(since)
        out: list[dict[str, Any]] = []
        for row in rows:
            pair = (str(row.get("created_at") or ""), str(row.get("id") or ""))
            if after is not None and pair <= after:
                continue
            out.append(row)
            if len(out) >= max(1, limit):
                break
        if out:
            last = out[-1]
            cursor = f"{last.get('created_at')}|{last.get('id')}"
        else:
            cursor = since or ""
        return out, cursor

    # --- mutating ops (each APPENDS a coord_event; never mutates authoritative state) ------------

    def _op_heartbeat(self, body: dict[str, Any], origin_node: str) -> dict[str, Any]:
        """A peer's liveness heartbeat. Appends heartbeat/point with the ORIGIN in the payload -- it does
        NOT call store.heartbeat() (that would advance the RECEIVER's nodes.last_heartbeat; a peer's
        liveness must be attributed to the ORIGIN, not the daemon that received it). The appender column
        is truthfully the receiver; the payload carries who is actually alive."""
        payload: dict[str, Any] = {"ts": _utc_iso(), "via": "control-http", "origin_node": origin_node}
        capabilities = body.get("capabilities")
        if isinstance(capabilities, dict):
            payload["capabilities"] = capabilities
        event = self._store.append_coord_event("heartbeat", "point", summary="Peer heartbeat via control-http.", payload=payload)
        return {"status": "OK", "origin_node": origin_node, "coord_event_id": event["id"]}

    def _op_lease_claim(self, body: dict[str, Any], origin_node: str) -> dict[str, Any]:
        """Record a lease REQUEST on behalf of the origin. NEVER grants (§C.2: the control service appends
        lease/requested events and the reducer/coordinator decides). origin_node rides the payload."""
        from . import coordination

        scope_key = body.get("scope_key")
        if not scope_key:
            return {"status": "DENIED", "code": "DENIED_LEASE_SCOPE_REQUIRED",
                    "required_action": "body must carry scope_key"}
        mode = body.get("mode", "advisory")
        try:
            ttl = int(body["ttl_s"]) if body.get("ttl_s") is not None else coordination.DEFAULT_TTL_S
        except (TypeError, ValueError):
            # A signed-but-garbage ttl_s is a structured 400, not an untyped 500.
            return {"status": "DENIED", "code": "DENIED_CONTROL_MALFORMED",
                    "required_action": "ttl_s must be an integer"}
        return coordination.request_lease(self._store, scope_key, mode=mode, ttl_s=ttl, origin_node=origin_node)

    def _op_lease_release(self, body: dict[str, Any], origin_node: str) -> dict[str, Any]:
        """Release the origin's lease. coordination.release_lease with origin_node=envelope node -- the
        verified signature substitutes for the self-node check, so a remote holder releases its OWN lease
        through the daemon. Structured denials (DENIED_NOT_HOLDER / DENIED_LEASE_NOT_HELD) surface as
        409 via the caller's status mapping."""
        from . import coordination

        scope_key = body.get("scope_key")
        if not scope_key:
            return {"status": "DENIED", "code": "DENIED_LEASE_SCOPE_REQUIRED",
                    "required_action": "body must carry scope_key"}
        try:
            return coordination.release_lease(self._store, scope_key, origin_node=origin_node)
        except KaizenDenied as denied:
            return denied.payload()

    def _op_dispatch(self, body: dict[str, Any], origin_node: str) -> dict[str, Any]:
        """LEASE-GATED dispatch request. The gate is AT THE REDUCER: a fresh reducers.reduce_lease over
        the store's coord_events (with the store clock as ``now``) must show the ORIGIN node as the
        active GRANTED holder of scope_key, else DENIED_DISPATCH_UNLEASED (409). When held, append
        dispatch/requested {origin_node, target_node, task, scope_key}. EXECUTION is M14; M11 records the
        gated request only."""
        scope_key = body.get("scope_key")
        target_node = body.get("target_node")
        task = body.get("task")
        if not (scope_key and target_node and task):
            return {"status": "DENIED", "code": "DENIED_DISPATCH_FIELDS_REQUIRED",
                    "required_action": "body must carry scope_key, target_node, task"}
        leases = reducers.reduce_lease(self._store.coord_events(), now=_utc_iso())
        active = leases.get(scope_key)
        if not active or active.get("state") != "held" or active.get("holder") != origin_node:
            return {
                "status": "DENIED",
                "code": "DENIED_DISPATCH_UNLEASED",
                "scope_key": scope_key,
                "origin_node": origin_node,
                "current_holder": active.get("holder") if active else None,
                "required_action": "hold the lease on scope_key before dispatching",
            }
        event = self._store.append_coord_event(
            "dispatch",
            "requested",
            summary=f"Dispatch requested to {target_node} for {scope_key}.",
            scope_key=scope_key,
            payload={"origin_node": origin_node, "target_node": target_node, "task": task, "scope_key": scope_key},
        )
        return {"status": "OK", "scope_key": scope_key, "target_node": target_node, "coord_event_id": event["id"]}

    def _op_steer(self, body: dict[str, Any], origin_node: str) -> dict[str, Any]:
        return self._relay_op("steer", body, origin_node)

    def _op_cancel(self, body: dict[str, Any], origin_node: str) -> dict[str, Any]:
        return self._relay_op("cancel", body, origin_node)

    def _relay_op(self, op: str, body: dict[str, Any], origin_node: str) -> dict[str, Any]:
        """Relay a steer/cancel to the in-process supervisor handler and surface its response VERBATIM.
        No relay configured => DENIED_CONTROL_NO_SUPERVISOR. Today the supervisor answers
        DENIED_UNKNOWN_OP (truthful -- the steer/cancel handlers land at M14); the relay seam is what M11
        proves works end to end."""
        if self._relay is None:
            return {"status": "DENIED", "code": "DENIED_CONTROL_NO_SUPERVISOR",
                    "required_action": "no in-process supervisor relay is configured for live steer/cancel"}
        try:
            return self._relay({"op": op, "args": body, "origin_node": origin_node})
        except Exception as error:  # noqa: BLE001 -- a relay bug is a 500, not a crashed server
            return {"status": "ERROR", "code": "ERROR_CONTROL_INTERNAL", "message": str(error)}


# --- request handler factory ---------------------------------------------------------------------

def _make_handler(service: "ControlService"):
    """Build a BaseHTTPRequestHandler bound to one ControlService. log_message routes through the
    service logger (callable) so stdout/stderr stay pristine; a handler exception becomes a 500
    {code: ERROR_CONTROL_INTERNAL} and NEVER kills the server (ThreadingHTTPServer would otherwise log a
    traceback)."""

    class _ControlHandler(BaseHTTPRequestHandler):
        # A short protocol version keeps connection handling simple; the client reads Content-Length.
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802 -- stdlib override name
            service._log("control-http " + (fmt % args))

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            # ALWAYS explicit utf-8 encode -- never rely on the default encoding (Windows cp1252 GOTCHA
            # g_20260708084954: a default-encoded emit corrupts non-ASCII and can raise).
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 -- stdlib handler name
            try:
                split = urlsplit(self.path)
                status, payload = service.handle_get(split.path, parse_qs(split.query))
                self._send(status, payload)
            except KaizenDenied as denied:
                self._send(_status_for_code(denied.code), denied.payload())
            except Exception as error:  # noqa: BLE001 -- a handler bug is a 500, never a server crash
                self._send(500, {"status": "ERROR", "code": "ERROR_CONTROL_INTERNAL", "message": str(error)})

        def do_POST(self) -> None:  # noqa: N802 -- stdlib handler name
            try:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    self._send(400, {"status": "DENIED", "code": "DENIED_CONTROL_MALFORMED",
                                     "required_action": "Content-Length must be a non-negative integer"})
                    return
                if length < 0:
                    self._send(400, {"status": "DENIED", "code": "DENIED_CONTROL_MALFORMED",
                                     "required_action": "Content-Length must be a non-negative integer"})
                    return
                if length > _MAX_BODY_BYTES:
                    # DRAIN the in-flight body (bounded) BEFORE responding: the client is still writing;
                    # responding without reading resets its send mid-write (WinError 10053 flake) and it
                    # may never see the 413. A hostile huge Content-Length is drained only up to the cap,
                    # then the connection closes rather than reading forever.
                    remaining = min(length, _MAX_DRAIN_BYTES)
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                    self.close_connection = True
                    self._send(413, {"status": "DENIED", "code": "DENIED_CONTROL_BODY_TOO_LARGE",
                                     "max_bytes": _MAX_BODY_BYTES})
                    return
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    wire = json.loads(raw.decode("utf-8")) if raw else {}
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"status": "DENIED", "code": "DENIED_CONTROL_MALFORMED",
                                     "required_action": "send a JSON body"})
                    return
                split = urlsplit(self.path)
                status, payload = service.handle_post(split.path, wire)
                self._send(status, payload)
            except KaizenDenied as denied:
                self._send(_status_for_code(denied.code), denied.payload())
            except Exception as error:  # noqa: BLE001 -- a handler bug is a 500, never a server crash
                self._send(500, {"status": "ERROR", "code": "ERROR_CONTROL_INTERNAL", "message": str(error)})

    return _ControlHandler


# --- HTTP status mapping -------------------------------------------------------------------------

def _status_for_code(code: str | None) -> int:
    """Map a denial code to its HTTP status: auth family -> 403, contention -> 409, ERROR_* -> 500, any
    other DENIED -> 400."""
    if code in _AUTH_CODES:
        return 403
    if code in _CONTENTION_CODES:
        return 409
    if code and code.startswith("ERROR_"):
        return 500
    return 400


# --- small parse helpers -------------------------------------------------------------------------

def _split_bind(bind: str) -> tuple[str, int]:
    """Split ``host:port`` or bracketed IPv6; bare hosts, including IPv6, use port 0."""
    text = (bind or "").strip()
    if text.startswith("["):
        close = text.find("]")
        if close > 0:
            host, suffix = text[1:close], text[close + 1:]
            if not suffix:
                return host, 0
            if suffix.startswith(":"):
                try:
                    return host, int(suffix[1:])
                except ValueError:
                    return text, 0
        return text, 0
    if text.count(":") > 1:
        return text, 0
    if ":" in text:
        host, port = text.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return text, 0
    return text, 0


def _route_of(path: str) -> str:
    """Normalize URL path to route key (strip query + trailing slash, empty -> "/"); load-bearing for BOTH signature binding (F6) and routing."""
    return urlsplit(path).path.rstrip("/") or "/"


def _parse_cursor(cursor: str) -> tuple[str, str] | None:
    if not cursor or "|" not in cursor:
        return None
    created_at, _, row_id = cursor.partition("|")
    return created_at, row_id


def _first(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key)
    return values[0] if values else default


def _float_arg(value: str, default: float) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


# --- client helpers (urllib; denials come back as structured JSON, not exceptions) ---------------

def probe(base_url: str, timeout: float = 5.0) -> dict[str, Any]:
    """GET /v1/probe (unsigned health)."""
    return _get(base_url, "/v1/probe", timeout)


def get_events(base_url: str, since: str = "", wait: float = 0, timeout: float = 30.0) -> dict[str, Any]:
    """GET /v1/events?since=&wait= (unsigned read; long-poll when wait>0)."""
    path = f"/v1/events?since={quote(since)}&wait={wait}"
    return _get(base_url, path, timeout)


def signed_post(base_url: str, path: str, body: Any, identity: dict, timeout: float = 5.0) -> dict[str, Any]:
    """Sign ``body`` for ``path`` with the identity's Ed25519 seed and POST the ``{envelope, body}``
    wire. On an HTTPError the JSON body is READ and PARSED (denials are structured payloads, not raw
    exceptions), so the caller always gets a dict."""
    wire = sign_request(path, body, identity)
    return _post(base_url, path, wire, timeout)


def _get(base_url: str, path: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(base_url.rstrip("/") + path, method="GET")
    return _send(request, timeout)


def _post(base_url: str, path: str, wire: Any, timeout: float) -> dict[str, Any]:
    data = json.dumps(wire).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    return _send(request, timeout)


def _send(request: urllib.request.Request, timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # A DENIED/ERROR body is structured JSON on the error stream -- read + parse it so the caller
        # sees {status: DENIED, code: ...} rather than an exception.
        try:
            return json.loads(error.read().decode("utf-8"))
        except (ValueError, OSError, http.client.HTTPException):
            return {"status": "ERROR", "code": "ERROR_CONTROL_HTTP", "http_status": error.code}
