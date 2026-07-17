"""FleetStore: the fleet.db handle + coord append/register/heartbeat/digest (v8 M9, plan §B.1 / §3.2).

The DAEMON-OWNED handle over ``fleet.db`` (the SEPARATE synced coordination DB). A daemon MAY hold this
handle long-lived (P-C: held handle OK); direct-open is BREAK-GLASS only when no live daemon holds the
pidfile (failure register #21). Ordering invariants encoded here:

- append is APPEND-ONLY and commits IMMEDIATELY -- a coord tx never stays open (the P-C rule; and it is
  what lets ``pull()`` run safely afterward);
- ``pull_and_reduce`` re-runs the PURE reducers FRESH after every pull -- reduced coord state is NEVER
  cached across a pull (ledger #24);
- the per-node ``max_seen_epoch`` watermark is persisted in **kaizen.db** (un-synced by construction),
  never in fleet.db -- so a hub restored from an old snapshot is detectable. M10b ENFORCES the
  regression check (``_check_epoch_regression``): on a SYNCED store, a reduced epoch below the stored
  watermark ⇒ ``DENIED_EPOCH_REGRESSION`` (hub-restore suspected; re-bootstrap per runbooks/hub.md),
  with a one-shot ``KAIZEN_FLEET_ACCEPT_EPOCH_REGRESSION=1`` operator override. An unsynced local store
  (labs/tests) never refuses.

Retry discipline mirrors :mod:`kaizen_components.db`'s connect/execute shape but is FLEET-LOCAL: it does
NOT import ``db.connect`` (that is bound to kaizen.db). ``db.set_setting`` IS imported for the watermark
(connect-per-call kaizen.db; fleet/* is import-guard allowlisted).
"""

from __future__ import annotations

import json
import os
import platform
import threading
import time
from typing import Any

from .. import db
from ..db_retry import ATTEMPTS, is_retryable, retry_delay
from ..db_schema import FLEET_DDL, FLEET_INDEXES, FLEET_SCHEMA_VERSION
from ..denials import KaizenDenied
from ..hashing import utc_text_hash
from ..paths import FLEET_DB_PATH, ensure_runtime_dirs
from ..redaction import assert_redacted
from ..schemas import validate_record
from ..schemas.registry import COORD_EVENT_KIND_MARKERS
from . import identity, sync as fleet_sync


def _now() -> str:
    return db.now()


def _payload_redaction_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Render nested payload values and key/value pairs for the redaction gate."""
    fields: dict[str, Any] = {}

    def visit(value: Any, path: str, key: str | None = None) -> None:
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        fields[path] = rendered
        if key is not None:
            fields[f"{path}.pair"] = f"{key}: {rendered}"
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                visit(nested_value, f"{path}.{nested_key}", str(nested_key))
        elif isinstance(value, list):
            for index, nested_value in enumerate(value):
                visit(nested_value, f"{path}[{index}]")

    for payload_key, payload_value in payload.items():
        visit(payload_value, f"payload.{payload_key}", str(payload_key))
    return fields


def daemon_is_live() -> bool:
    """True iff a live supervisor daemon currently holds the workspace pidfile.

    Uses the supervisor's PIDFILE + the pid+nonce liveness idiom (same false-orphan guard: a reused pid
    is only 'live' if the pidfile is well-formed and the process exists). Imported lazily to avoid a
    load-order cycle and to keep a plain FleetStore construction free of supervisor imports."""
    from ..orchestration import children
    from ..orchestration.supervisor import PIDFILE

    if not PIDFILE.is_file():
        return False
    try:
        data = json.loads(PIDFILE.read_text(encoding="utf-8"))
        pid = int(data["pid"])
    except (ValueError, KeyError, OSError):
        return False
    return children.pid_alive(pid)


class FleetStore:
    """Held handle over fleet.db. Opens (sync or plain) via :mod:`fleet.sync`, applies FLEET_DDL +
    FLEET_INDEXES idempotently, stamps ``fleet_schema`` (version 1). ``.close()`` is idempotent."""

    def __init__(
        self,
        db_path: Any = FLEET_DB_PATH,
        *,
        sync_url: str | None = None,
        auth_token: str | None = None,
        node: dict | None = None,
        logger: Any = None,
    ) -> None:
        ensure_runtime_dirs()
        self.db_path = str(db_path)
        self._node = node or identity.load_or_mint_node_identity()
        if not isinstance(self._node, dict) or not isinstance(self._node.get("node_id"), str) or not self._node["node_id"]:
            raise KaizenDenied(
                "DENIED_FLEET_NODE_IDENTITY",
                {"required_action": "provide a node identity dict with a non-empty node_id"},
                exit_code=2,
            )
        self._logger = logger
        # M11: the ThreadingHTTPServer's per-connection worker threads + loopback threads + the daemon
        # thread now all reach this ONE turso connection, which is not thread-safe. This RLock serializes
        # every _write and _read_all so concurrent control-service requests cannot interleave on the
        # handle. RLock (not Lock): append_coord_event holds it across a _write whose op body issues its
        # own reads, and publish_pubkey's _write appends within the same call stack -- re-entrant by design.
        self._lock = threading.RLock()
        self._in_tx = False  # the tx state the sync tx_guard reads; flipped only inside _write
        self._conn, synced = self._connect(sync_url, auth_token)
        self.sync = fleet_sync.SyncHandle(self._conn, synced=synced, in_tx_getter=lambda: self._in_tx)
        self._apply_schema()

    # --- identity passthroughs -----------------------------------------------------------------

    @property
    def node_id(self) -> str:
        return self._node["node_id"]

    # --- connection + retry (fleet-local; never imports db.connect) -----------------------------

    def _connect(self, sync_url: str | None, auth_token: str | None):
        """Open the fleet.db handle with the shipped retry backoff shape (Windows file-lock contention
        is transient). Fleet-local: uses fleet.sync.open_connection, not db.connect (kaizen.db-bound)."""
        last_error: Exception | None = None
        for attempt in range(1, ATTEMPTS + 1):
            conn = None
            try:
                conn, synced = fleet_sync.open_connection(self.db_path, sync_url, auth_token)
                return conn, synced
            except Exception as error:  # noqa: BLE001 -- classify + retry transient lock/contention only
                last_error = error
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                if not is_retryable(error) or attempt == ATTEMPTS:
                    break
                time.sleep(retry_delay(attempt - 1))
        raise KaizenDenied(
            "DENIED_FLEET_DB_CONNECT_RETRY_EXHAUSTED",
            {
                "attempts": ATTEMPTS,
                "reason": str(last_error),
                "db_path": self.db_path,
                "required_action": "standby or retry later; fleet.db is disposable (re-bootstrap from hub)",
            },
        ) from last_error

    def _write(self, operation: Any) -> Any:
        """Run ``operation(conn)`` inside a single BEGIN/COMMIT with the shipped retry backoff.

        Sets ``in_tx`` for the duration so the sync tx_guard refuses a ``pull()`` mid-write, and ALWAYS
        clears it + commits/rolls back before returning -- a coord tx never outlives one call.

        Serialized under ``self._lock`` (M11): control-service worker threads share the single turso
        connection, so a BEGIN/COMMIT block must never interleave with another thread's read/write."""
        with self._lock:
            for attempt in range(1, ATTEMPTS + 1):
                try:
                    self._in_tx = True
                    self._conn.execute("BEGIN")
                    result = operation(self._conn)
                    self._conn.execute("COMMIT")
                    return result
                except Exception as error:  # noqa: BLE001
                    try:
                        self._conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    if not is_retryable(error) or attempt == ATTEMPTS:
                        raise
                    time.sleep(retry_delay(attempt - 1))
                finally:
                    self._in_tx = False

    def _read_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        # Serialized under the same lock as _write (M11) so a read never races an in-flight tx on the
        # shared connection.
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _apply_schema(self) -> None:
        """Create fleet.db tables/indexes idempotently and stamp fleet_schema (version 1). One tx."""
        def op(conn: Any) -> None:
            for stmt in FLEET_DDL:
                conn.execute(stmt)
            for stmt in FLEET_INDEXES:
                conn.execute(stmt)
            conn.execute(
                "INSERT OR IGNORE INTO fleet_schema (id, fleet_schema_version, applied_at) VALUES (?, ?, ?)",
                ("current", FLEET_SCHEMA_VERSION, _now()),
            )

        self._write(op)

    def close(self) -> None:
        """Idempotent close of the held handle."""
        conn = getattr(self, "_conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 -- teardown must not raise
                pass
            self._conn = None

    def __enter__(self) -> "FleetStore":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # --- coord append (append-only; commits immediately) ----------------------------------------

    def append_coord_event(
        self,
        event_kind: str,
        marker: str,
        *,
        summary: str,
        scope_key: str | None = None,
        epoch: int | None = None,
        payload: dict | None = None,
        project_id: str | None = None,
        source_event_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one coord_event. Validation order (structured refusals, exit-code-2 KaizenDenied):

        1. (kind x marker) pair against COORD_EVENT_KIND_MARKERS (DENIED_COORD_KIND_MARKER) -- the
           registry enum bounds each axis independently, so the PAIR is checked here like T6;
        2. validate_record("coord_event", ...) -- shape/enum/summary;
        3. assert_redacted over ALL string fields (summary, scope_key, serialized payload VALUES) --
           the redaction pass-path: legit coord fields (MagicDNS/scope/sha/epoch) pass; a token or home
           path DENIES.

        Then content_hash the row core, INSERT with a node-tagged coord_id, and COMMIT immediately
        (append-only; a coord tx is never left open). source_event_id dupes are INSERT OR IGNORE
        (idempotent replay) and reported ``{deduped: true}``."""
        if event_kind not in COORD_EVENT_KIND_MARKERS or marker not in COORD_EVENT_KIND_MARKERS[event_kind]:
            raise KaizenDenied(
                "DENIED_COORD_KIND_MARKER",
                {
                    "event_kind": event_kind,
                    "marker": marker,
                    "allowed_markers": COORD_EVENT_KIND_MARKERS.get(event_kind, []),
                    "allowed_kinds": list(COORD_EVENT_KIND_MARKERS),
                    "required_action": "use an allowed (event_kind x marker) pair",
                },
                exit_code=2,
            )

        clean = {
            k: v
            for k, v in {
                "node_id": self.node_id,
                "project_id": project_id,
                "event_kind": event_kind,
                "marker": marker,
                "scope_key": scope_key,
                "epoch": epoch,
                "payload": payload if isinstance(payload, dict) else None,
                "summary": summary,
            }.items()
            if v not in (None, "")
        }
        validate_record("coord_event", clean)

        # Redaction pass-path: scan every string field plus each payload entry rendered as a
        # "key: value" pair (so a secret-y key like `password:` triggers assigned_secret even when the
        # value alone looks benign) AND the raw serialized value. Legit coord facts (MagicDNS name,
        # scope_key path-ish token, sha, epoch) are clean; a secret or personal path in the
        # summary/scope/payload denies with DENIED_TRACE_REDACTION.
        redaction_fields: dict[str, Any] = {"summary": summary}
        if scope_key:
            redaction_fields["scope_key"] = scope_key
        if isinstance(payload, dict):
            redaction_fields.update(_payload_redaction_fields(payload))
        assert_redacted(redaction_fields)

        created = _now()
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True) if isinstance(payload, dict) else None
        content_hash = utc_text_hash(
            {
                "node_id": self.node_id,
                "project_id": project_id,
                "event_kind": event_kind,
                "marker": marker,
                "scope_key": scope_key,
                "epoch": epoch,
                "payload_json": payload_json,
                "created_at": created,
            }
        )
        record_id = identity.coord_id("ce", self.node_id)
        # M11 Ed25519 signing (plan §C.2): when THIS node carries a signing seed, sign the content_hash
        # (utf-8 bytes) and store the hex signature in the existing sig column (hard-None before M11). An
        # injected test node / a node with no minted seed produces sig=None -- a BYTE-IDENTICAL legacy
        # append. Signature VERIFICATION of pulled events is M16 (not built here); this only ATTESTS
        # locally-appended rows so a peer can later verify origin.
        signature = self._sign_content_hash(content_hash)

        def op(conn: Any) -> dict[str, Any]:
            conn.execute(
                "INSERT OR IGNORE INTO coord_events "
                "(id, created_at, node_id, project_id, event_kind, marker, scope_key, epoch, "
                "payload_json, source_event_id, content_hash, sig) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id, created, self.node_id, project_id, event_kind, marker, scope_key, epoch,
                    payload_json, source_event_id, content_hash, signature,
                ),
            )
            inserted = int(conn.execute("SELECT changes()").fetchone()[0]) > 0
            if inserted:
                return {"inserted": True, "id": record_id, "created_at": created, "content_hash": content_hash, "sig": signature}
            if source_event_id:
                stored = conn.execute(
                    "SELECT id, created_at, content_hash, sig FROM coord_events WHERE source_event_id = ?",
                    (source_event_id,),
                ).fetchone()
            else:
                stored = conn.execute(
                    "SELECT id, created_at, content_hash, sig FROM coord_events WHERE id = ?",
                    (record_id,),
                ).fetchone()
            if not stored:
                raise AssertionError("ignored coord event has no persisted row")
            return {"inserted": False, "id": stored[0], "created_at": stored[1], "content_hash": stored[2], "sig": stored[3]}

        result = self._write(op)
        return {
            "status": "OK",
            "id": result["id"],
            "event_kind": event_kind,
            "marker": marker,
            "created_at": result["created_at"],
            "content_hash": result["content_hash"],
            "deduped": not result["inserted"],
            "signed": result["sig"] is not None,
        }

    def _sign_content_hash(self, content_hash: str) -> str | None:
        """Ed25519-sign ``content_hash`` (utf-8 bytes) with this node's seed, returning a hex signature;
        None when the node carries no ``ed25519_seed_hex`` (the byte-identical legacy path). Lazy PyNaCl
        import so a plain/unsigned FleetStore never loads it."""
        seed_hex = self._node.get("ed25519_seed_hex")
        if not seed_hex:
            return None
        from nacl import signing  # noqa: PLC0415 -- lazy: only the signing path loads PyNaCl

        key = signing.SigningKey(bytes.fromhex(seed_hex))
        return key.sign(content_hash.encode("utf-8")).signature.hex()

    # --- node registration + heartbeat ----------------------------------------------------------

    def register_node(self, role: str, tailnet_name: str | None = None, capabilities: dict | None = None) -> dict[str, Any]:
        """Upsert this node into ``nodes`` (keyed by node_id) and append a node (registered|updated)
        coord_event -- the marker is chosen by whether a nodes row already existed. os/arch come from
        the platform module. tailnet_name defaults to the never-an-IP display name."""
        display = tailnet_name or identity.node_display_name()
        os_name = platform.system().lower() or None
        arch = platform.machine() or None
        caps_json = (
            json.dumps(capabilities, ensure_ascii=False, sort_keys=True) if isinstance(capabilities, dict) else None
        )
        now = _now()
        event_payload: dict[str, Any] = {"role": role, "tailnet_name": display}
        if isinstance(capabilities, dict):
            event_payload["capabilities"] = capabilities
        preview_updated = bool(self._read_all("SELECT 1 FROM nodes WHERE node_id = ?", (self.node_id,)))
        preview_marker = "updated" if preview_updated else "registered"
        preview_summary = f"Node {'updated' if preview_updated else 'registered'} as {role}."
        # Fail before advancing the mutable node row when the corresponding audit event would be denied.
        validate_record(
            "coord_event",
            {
                "node_id": self.node_id,
                "event_kind": "node",
                "marker": preview_marker,
                "payload": event_payload,
                "summary": preview_summary,
            },
        )
        assert_redacted({"summary": preview_summary, **_payload_redaction_fields(event_payload)})

        def op(conn: Any) -> dict[str, Any]:
            existing = conn.execute("SELECT node_id, registered_at FROM nodes WHERE node_id = ?", (self.node_id,)).fetchone()
            registered_at = existing[1] if existing and existing[1] else now
            conn.execute(
                "INSERT OR REPLACE INTO nodes "
                "(node_id, role, tailnet_name, os, arch, capabilities_json, pubkey, last_heartbeat, "
                "registered_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, "
                "(SELECT pubkey FROM nodes WHERE node_id = ?), "
                "(SELECT last_heartbeat FROM nodes WHERE node_id = ?), ?, ?)",
                (self.node_id, role, display, os_name, arch, caps_json, self.node_id, self.node_id, registered_at, now),
            )
            return {"existed": existing is not None}

        existed = self._write(op)["existed"]
        marker = "updated" if existed else "registered"
        summary = f"Node {'updated' if existed else 'registered'} as {role}."
        event = self.append_coord_event("node", marker, summary=summary, payload=event_payload)
        return {
            "status": "OK",
            "node_id": self.node_id,
            "role": role,
            "tailnet_name": display,
            "marker": marker,
            "coord_event_id": event["id"],
        }

    def heartbeat(self) -> dict[str, Any]:
        """Advance ``nodes.last_heartbeat`` and append a heartbeat/point coord_event (payload {ts})."""
        now = _now()

        def op(conn: Any) -> None:
            conn.execute("UPDATE nodes SET last_heartbeat = ?, updated_at = ? WHERE node_id = ?", (now, now, self.node_id))

        self._write(op)
        event = self.append_coord_event("heartbeat", "point", summary="Node heartbeat.", payload={"ts": now})
        return {"status": "OK", "node_id": self.node_id, "ts": now, "coord_event_id": event["id"]}

    # --- pubkey publish / resolve (M11; the tailnet-membership key-distribution bootstrap) -------

    def publish_pubkey(self, pubkey_hex: str) -> dict[str, Any]:
        """Upsert this node's Ed25519 public key into ``nodes.pubkey`` and append a ``node/updated``
        coord_event carrying it (v8 M11, plan §C.2). The pub half rides the synced nodes row, so a peer
        that pulls fleet.db learns this node's key -- key distribution IS tailnet-membership bootstrap
        (only the PUB half ever leaves; the seed stays machine-local). The upsert keys on node_id,
        setting pubkey + updated_at (and registered_at on a fresh insert). ``"pubkey": ...`` in the
        summary/payload does NOT match the assigned_secret redaction pattern (verified: the pattern needs
        a password/secret/token-class key), so the append passes the redaction pass-path cleanly."""
        now = _now()

        def op(conn: Any) -> None:
            conn.execute(
                "INSERT INTO nodes (node_id, pubkey, registered_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET pubkey = excluded.pubkey, updated_at = excluded.updated_at",
                (self.node_id, pubkey_hex, now, now),
            )

        self._write(op)
        event = self.append_coord_event(
            "node", "updated", summary="Node pubkey published.", payload={"pubkey": pubkey_hex}
        )
        return {"status": "OK", "node_id": self.node_id, "pubkey": pubkey_hex, "coord_event_id": event["id"]}

    def node_pubkey(self, node_id: str) -> str | None:
        """The stored Ed25519 public key (hex) for a node, or None when unknown/unset. The
        EnvelopeVerifier's pubkey resolver reads through this."""
        rows = self._read_all("SELECT pubkey FROM nodes WHERE node_id = ?", (node_id,))
        return rows[0][0] if rows and rows[0][0] else None

    # --- digest (pure reducers run FRESH; never cached) -----------------------------------------

    def _coord_event_rows(self) -> list[dict[str, Any]]:
        """Read canonical ``(created_at, id)`` order and decode payload JSON, using None when malformed."""
        rows = self._read_all(
            "SELECT id, created_at, node_id, project_id, event_kind, marker, scope_key, epoch, payload_json "
            "FROM coord_events ORDER BY created_at, id"
        )
        events: list[dict[str, Any]] = []
        for r in rows:
            payload = None
            if r[8]:
                try:
                    payload = json.loads(r[8])
                except ValueError:
                    payload = None
            events.append(
                {
                    "id": r[0], "created_at": r[1], "node_id": r[2], "project_id": r[3],
                    "event_kind": r[4], "marker": r[5], "scope_key": r[6], "epoch": r[7],
                    "payload": payload,
                }
            )
        return events

    def coord_events(self) -> list[dict[str, Any]]:
        """Public read of the canonical coord_event rows (the coordination engine's read path). A thin
        alias over the private reader so :mod:`fleet.coordination` never touches ``_coord_event_rows``
        directly."""
        return self._coord_event_rows()

    def digest(self, limit: int | None = None) -> dict[str, Any]:
        """Fleet digest = fresh pure-reducer projection over coord_events, merged with the nodes table.

        Runs :mod:`fleet.reducers` FRESH every call (never a cached projection). Node rows are merged
        with the reducer's per-node view and sorted deterministically by node_id; ``heartbeat_age_s``
        is derived from ``last_heartbeat``. Also records the ``max_seen_epoch`` watermark (below)."""
        from . import reducers

        events = self._coord_event_rows()
        reduced_nodes = reducers.reduce_nodes(events)
        coordinator = reducers.reduce_coordinator(events)
        # Expiry-aware digest (M10a): pass the store clock so a lease past its ttl reduces to 'expired'.
        leases = reducers.reduce_lease(events, now=_now())
        # M12 (plan §B.4): fork / merge-conflict blocking spans, surfaced in the digest so an open
        # conflict blocks completion like an open approval. Additive key -- pre-M12 callers ignore it.
        conflicts = reducers.reduce_conflicts(events)
        # M14 (plan §B.5): remote-dispatch projection. Non-terminal dispatches are surfaced (open work
        # in flight across the fleet). Additive key -- pre-M14 callers ignore it.
        dispatches = reducers.reduce_dispatches(events)
        epoch = reducers.max_epoch(events)

        node_rows = {
            r[0]: r
            for r in self._read_all(
                "SELECT node_id, role, tailnet_name, last_heartbeat, registered_at FROM nodes"
            )
        }
        now_dt = _parse_iso(_now())
        node_ids = sorted(set(node_rows) | set(reduced_nodes))
        nodes: list[dict[str, Any]] = []
        for nid in node_ids:
            row = node_rows.get(nid)
            reduced = reduced_nodes.get(nid, {})
            last_heartbeat = (row[3] if row else None) or reduced.get("last_heartbeat")
            nodes.append(
                {
                    "node_id": nid,
                    "role": (row[1] if row else None) or reduced.get("role"),
                    "tailnet_name": (row[2] if row else None) or reduced.get("tailnet_name"),
                    "last_heartbeat": last_heartbeat,
                    "heartbeat_age_s": _age_seconds(last_heartbeat, now_dt),
                    "retired": bool(reduced.get("retired", False)),
                }
            )
        if limit is not None:
            nodes = nodes[: max(0, int(limit))]

        project = identity.project_id()
        # M10b hub-restore regression REFUSAL, only on a synced store (a fresh bootstrap from a rewound
        # hub manifests on the first digest; an unsynced local/lab store never regresses, never refuses).
        if self.sync.synced:
            self._check_epoch_regression(project["project_id"], epoch)
        self._record_watermark(project["project_id"], epoch)
        return {
            "status": "OK",
            "project": project,
            "node_id": self.node_id,
            "nodes": nodes,
            "coordinator": coordinator,
            "leases": leases,
            "conflicts": {"open": conflicts["open"], "open_count": len(conflicts["open"])},
            "dispatches": {
                "open": [d for d in dispatches.values() if d.get("state") not in ("completed", "failed", "canceled")],
                "open_count": sum(
                    1 for d in dispatches.values() if d.get("state") not in ("completed", "failed", "canceled")
                ),
            },
            "max_epoch": epoch,
            "sync": self.sync.synced,
            "counts": {"coord_events": len(events), "nodes": len(node_ids), "nodes_returned": len(nodes)},
        }

    # --- watermark (kaizen.db db_settings; un-synced by construction) ---------------------------

    def _record_watermark(self, project_id: str, epoch: int) -> None:
        """Advance the per-node max-seen project epoch monotonically in kaizen.db, never fleet.db; _check_epoch_regression uses it to refuse restored/regressed fleet state."""
        if epoch < 0:
            return
        key = f"fleet_max_seen_epoch_{project_id}"
        try:
            current_raw = db.get_setting(key)
            current = int(current_raw) if current_raw else -1
        except (ValueError, KaizenDenied):
            current = -1
        if epoch > current:
            db.set_setting(key, str(epoch))

    def watermark(self, project_id: str | None = None) -> int:
        """Read the stored ``max_seen_epoch`` watermark for a project (-1 when unset)."""
        pid = project_id or identity.project_id()["project_id"]
        raw = db.get_setting(f"fleet_max_seen_epoch_{pid}")
        try:
            return int(raw) if raw else -1
        except ValueError:
            return -1

    def _check_epoch_regression(self, project_id: str, epoch: int) -> None:
        """Hub-restore epoch-regression REFUSAL (M10b, plan §B.3 / ledger #20 / #23).

        A hub file-restore silently rewinds fleet.db to an old snapshot with NO protocol error (P-C
        confirmed). The reducers are max-epoch-wins, and each node persists an un-synced
        ``max_seen_epoch`` watermark in kaizen.db; so a reduced epoch BELOW a positive stored watermark
        means the ledger regressed -- refuse ``DENIED_EPOCH_REGRESSION`` (exit 2) and point at the
        re-bootstrap runbook. Called only on a SYNCED store (an unsynced local/lab store cannot regress
        from a hub it never pulls, so it never refuses).

        Escape hatch (one-shot, per-call so it naturally clears next session):
        ``KAIZEN_FLEET_ACCEPT_EPOCH_REGRESSION=1`` ⇒ instead of refusing, LOG + append a
        ``divergence/detected`` {accepted: true} coord_event AND reset the stored watermark down to the
        reduced epoch (a deliberate operator acknowledgment of the rewind)."""
        stored = self.watermark(project_id)
        if stored <= -1 or epoch >= stored:
            return
        accept = os.environ.get("KAIZEN_FLEET_ACCEPT_EPOCH_REGRESSION") == "1"
        if accept:
            if self._logger is not None:
                # The fleet/orchestration logger convention is a plain CALLABLE (the supervisor passes
                # its log method) -- never assume a logging.Logger.
                try:
                    self._logger(
                        f"fleet epoch regression ACCEPTED (operator override): stored={stored} "
                        f"reduced={epoch} project={project_id}"
                    )
                except Exception:  # noqa: BLE001 -- logging must never break the accept path
                    pass
            # Record the accepted divergence for the audit trail, then reset the watermark DOWN so the
            # re-bootstrapped-from-rewound-hub ledger becomes the new baseline (one-shot acknowledgment).
            try:
                self.append_coord_event(
                    "divergence",
                    "detected",
                    summary="Epoch regression accepted (operator override); watermark reset to rewound ledger.",
                    payload={
                        "would_block": "DENIED_EPOCH_REGRESSION",
                        "accepted": True,
                        "stored_watermark": int(stored),
                        "reduced_epoch": int(epoch),
                    },
                )
            except Exception:  # noqa: BLE001 -- the accept must proceed even if the audit append fails
                pass
            db.set_setting(f"fleet_max_seen_epoch_{project_id}", str(int(epoch)))
            return
        raise KaizenDenied(
            "DENIED_EPOCH_REGRESSION",
            {
                "stored_watermark": int(stored),
                "reduced_epoch": int(epoch),
                "project_id": project_id,
                "required_action": (
                    "hub-restore suspected -- verify the hub, then re-bootstrap per runbooks/hub.md; "
                    "set KAIZEN_FLEET_ACCEPT_EPOCH_REGRESSION=1 for ONE deliberate acceptance"
                ),
            },
            exit_code=2,
        )

    # --- sync passthroughs (re-reduce fresh after pull; never cache) ----------------------------

    def push(self) -> dict[str, Any]:
        with self._lock:
            return self.sync.push()

    def checkpoint(self) -> dict[str, Any]:
        with self._lock:
            return self.sync.checkpoint()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return self.sync.stats()

    def pull_and_reduce(self, limit: int | None = None) -> dict[str, Any]:
        """Pull remote coord_events, then recompute the digest FRESH (ledger #24) and re-record the
        watermark. ``pull()`` asserts no open tx via the SyncHandle guard. When sync is off this is a
        plain fresh digest (the pull is a structured no-op).

        M10b: after the pull and BEFORE the digest, run the hub-restore regression check on a synced
        store -- a pulled ledger below the un-synced watermark is a rewound hub and refuses
        DENIED_EPOCH_REGRESSION here (the pull is exactly where a rewind arrives). ``digest()`` repeats
        the check idempotently (monotonic watermark; the accept path resets it), covering direct digests."""
        from . import reducers

        with self._lock:
            pull_result = self.sync.pull()
            if self.sync.synced:
                project = identity.project_id()
                reduced_epoch = reducers.max_epoch(self._coord_event_rows())
                self._check_epoch_regression(project["project_id"], reduced_epoch)
            digest = self.digest(limit=limit)
        return {"status": "OK", "pull": pull_result, "digest": digest}


def open_store_breakglass(**kwargs: Any) -> FleetStore:
    """Break-glass direct-open of fleet.db, ONLY when no live daemon holds the pidfile (failure
    register #21). Refuses with DENIED_FLEET_DAEMON_LIVE when a daemon is live -- the daemon owns the
    handle and D*-ops must go through its loopback instead of a second opener."""
    if daemon_is_live():
        raise KaizenDenied(
            "DENIED_FLEET_DAEMON_LIVE",
            {
                "reason": "a live supervisor daemon holds the fleet.db handle",
                "required_action": "route the op through the daemon loopback; do not open a second fleet.db handle",
            },
            exit_code=2,
        )
    return FleetStore(**kwargs)


# --- small time helpers (stdlib; no external dep) ------------------------------------------------

def _parse_iso(text: str | None):
    """Parse an ISO datetime as UTC-aware, accepting ``Z`` and assuming UTC when naive."""
    from datetime import datetime, timezone

    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _age_seconds(last_heartbeat: str | None, now_dt) -> float | None:
    """Return rounded age for UTC-normalized datetimes, or None on invalid input."""
    if not last_heartbeat or now_dt is None:
        return None
    then = _parse_iso(last_heartbeat)
    if then is None:
        return None
    try:
        return round((now_dt - then).total_seconds(), 3)
    except (TypeError, ValueError):
        return None
