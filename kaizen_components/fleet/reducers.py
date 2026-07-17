"""PURE reducers over coord_event row dicts (v8 M9, plan §B.1 / §B.2, ledger #20 / #24).

No DB, no imports beyond stdlib. Every reducer is DETERMINISTIC under reordering and duplication of
its input: it sorts internally by ``(created_at, id)`` and dedupes by ``id`` first, then folds. This is
the §6 fleet CI invariant -- two synthetic node_ids' interleaved/reordered/duplicated events reduce to
IDENTICAL projections on every node -- and the reason the store NEVER caches reduced state across a
``pull()`` (ledger #24): it re-runs these fresh after every pull.

Coordinator + lease are MAX-EPOCH-WINS (ledger #20): a later-CREATED batch carrying a LOWER epoch (a
hub restored from an old snapshot) must NOT displace a higher epoch already seen. Coordinator ties use
``(epoch, created_at, id)``; lease ties additionally use ``grant_seq``. Leases are a reducer PROJECTION of
coord_events, never a mutable mutex row.

M10a semantics (plan §B.2 / §B.3, this session):

- **claimed-vs-granted (coordinator).** A ``claimed`` is a REQUEST; a ``granted`` is the AUTHORITY. So a
  bare higher-epoch ``claimed`` (a CONTESTED claim against a live holder) does NOT win: ``granted`` is
  the sole authoritative hold, and ``claimed`` counts ONLY as a bootstrap fallback when NO ``granted``
  exists at any epoch. This is the correct §B.3 reading (the claim is the request, the grant is the
  authority) and is what keeps ``claim_coordinator`` non-displacing in shadow mode.
- **payload.to_node (coordinator).** A grantor appends ``coordinator/granted`` when TRANSFERRING to
  another node; the row's ``node_id`` is the APPENDING (old) node, so the new holder is read from
  ``payload.to_node`` when present, else the event's own ``node_id`` (self-grant on vacancy).
- **payload.mode (coordinator).** The winning event's ``payload.mode`` (roaming|pinned, default
  "roaming") is surfaced in the result.
- **grant_seq (lease).** Same-``(scope_key, lease_epoch)`` grants are ordered by a grantor-local
  monotonic ``payload.grant_seq`` (default 0) as a secondary key: a renew re-grants at the same epoch
  with grant_seq+1 and the highest seq wins. The grantor is single by construction (the coordinator),
  so no gapless cross-node sequence exists or is needed.
- **payload.holder (lease).** A grantor appends ``lease/granted`` on BEHALF of a holder node; the
  appender is the grantor, so the holder is read from ``payload.holder`` when present, else the event's
  ``node_id``.
- **expiry (lease).** A grant may carry ``payload.expires_at`` (ISO). When a ``now`` (ISO str) is passed
  and ``now > expires_at``, the reduced state is "expired" (holder None) even with no explicit
  ``lease/expired`` event -- expiry is a pure function of the clock, so both replicas reduce identically
  for the same ``now``. ``sweep_expired`` (coordination.py) later materializes an explicit event.

M10b passthroughs (plan §B.2 / §B.3, this session) -- record-level surfacing only; enforcement lives in
:mod:`fleet.coordination`, not here (the reducers stay pure and never refuse):

- **payload.mode (lease).** The grant's advisory|authoritative mode (default advisory when absent, the
  M9/M10a shape) is surfaced so a reader distinguishes a CP mutex grant from an AP best-effort hint.
- **payload.iso / iso_sentinel (coordinator).** When the winning coordinator event was minted isolated
  (§B.3 iso: sentinel), ``iso: true`` (+ the sentinel) is surfaced; reconcile is M15.
"""

from __future__ import annotations

from typing import Any


def _canonical(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe by id, then sort by ``(created_at, id)`` -- the single normalization every reducer runs
    so its output is independent of input order and duplication."""
    seen: dict[str, dict[str, Any]] = {}
    for event in events:
        eid = event.get("id")
        if eid is None:
            # An id-less event cannot be deduped deterministically; key it by identity so it still
            # participates without ever colliding with a real id.
            seen[f"_anon_{id(event)}"] = event
            continue
        # First occurrence wins; duplicates (replayed rows) are identical by construction (content_hash
        # covers the core), so which copy is kept does not matter.
        seen.setdefault(str(eid), event)
    return sorted(seen.values(), key=lambda e: (str(e.get("created_at") or ""), str(e.get("id") or "")))


def _epoch(event: dict[str, Any]) -> int:
    """Coerce event["epoch"] to int; return -1 when absent/non-numeric (no-epoch sentinel)."""
    try:
        value = event.get("epoch")
        return int(value) if value is not None else -1
    except (TypeError, ValueError):
        return -1


def max_epoch(events: list[dict[str, Any]]) -> int:
    """Highest ``epoch`` across all events (-1 when none carry an epoch). The watermark input."""
    highest = -1
    for event in events:
        highest = max(highest, _epoch(event))
    return highest


def reduce_nodes(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fold node/heartbeat events into last-known per node_id: ``{node_id: {node_id, role,
    tailnet_name, last_registered_at, last_heartbeat, retired}}``. Later (created_at, id) wins per
    field -- registration sets role/tailnet_name; heartbeat advances last_heartbeat; a node/retired
    marks retired. Deterministic: driven off the canonical (deduped, sorted) sequence."""
    nodes: dict[str, dict[str, Any]] = {}
    for event in _canonical(events):
        node = event.get("node_id")
        if not node:
            continue
        kind = event.get("event_kind")
        marker = event.get("marker")
        entry = nodes.setdefault(
            node,
            {
                "node_id": node,
                "role": None,
                "tailnet_name": None,
                "last_registered_at": None,
                "last_heartbeat": None,
                "retired": False,
            },
        )
        if kind == "node":
            if marker in ("registered", "updated"):
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                entry["role"] = payload.get("role", entry["role"])
                entry["tailnet_name"] = payload.get("tailnet_name", entry["tailnet_name"])
                entry["last_registered_at"] = event.get("created_at")
                entry["retired"] = False
            elif marker == "retired":
                entry["retired"] = True
        elif kind == "heartbeat" and marker == "point":
            entry["last_heartbeat"] = event.get("created_at")
    return nodes


def _coordinator_holder(event: dict[str, Any]) -> Any:
    """The node a coordinator event confers the role on: ``payload.to_node`` (a grantor transferring to
    another node) when present, else the event's own ``node_id`` (self-grant on vacancy)."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    to_node = payload.get("to_node")
    return to_node if to_node else event.get("node_id")


def _coordinator_mode(event: dict[str, Any]) -> str:
    """Return payload.mode when in {roaming,pinned}, else default "roaming"."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    mode = payload.get("mode")
    return mode if mode in ("roaming", "pinned") else "roaming"


def reduce_coordinator(events: list[dict[str, Any]]) -> dict[str, Any]:
    """MAX-EPOCH-WINS coordinator projection (ledger #20) with M10a claimed-vs-granted semantics.

    ``granted`` is the SOLE authoritative hold; ``claimed`` is a REQUEST that counts ONLY when NO
    ``granted`` exists at any epoch (bootstrap). So a contested higher-epoch ``claimed`` against a live
    granted holder does NOT displace it (§B.3: the claim is the request, the grant is the authority).

    The winner is the highest-epoch authoritative event NOT released at that same epoch. A release
    permanently fences every same-epoch re-grant; producers must advance the epoch before granting again.
    A hub-restore replaying a lower-epoch grant later cannot displace a higher epoch already seen. Ties break by
    ``(epoch, created_at, id)``. The holder is ``payload.to_node`` when the grant is a transfer, else the
    event's ``node_id``; the winning event's ``payload.mode`` (roaming|pinned, default roaming) is
    surfaced. Returns ``{holder, epoch, mode, id}`` or ``{holder: None, epoch: -1, mode: "roaming"}``
    when unheld."""
    canonical = _canonical(events)
    # Track released epochs so a grant/claim whose epoch was released is not counted as held.
    released_epochs: set[int] = set()
    has_granted = False
    for event in canonical:
        if event.get("event_kind") != "coordinator":
            continue
        marker = event.get("marker")
        if marker == "released":
            released_epochs.add(_epoch(event))
        elif marker == "granted":
            has_granted = True

    # granted is authoritative; claimed counts only as a bootstrap fallback when no granted exists.
    authoritative_markers = ("granted",) if has_granted else ("claimed",)

    best: dict[str, Any] | None = None
    best_key: tuple[int, str, str] | None = None
    for event in canonical:
        if event.get("event_kind") != "coordinator":
            continue
        if event.get("marker") not in authoritative_markers:
            continue
        epoch = _epoch(event)
        if epoch in released_epochs:
            continue
        key = (epoch, str(event.get("created_at") or ""), str(event.get("id") or ""))
        if best_key is None or key > best_key:
            best_key = key
            best = {
                "holder": _coordinator_holder(event),
                "epoch": epoch,
                "mode": _coordinator_mode(event),
                "id": event.get("id"),
            }
            # §B.3 iso: sentinel passthrough (M10b): a record-level marker that the winning authority
            # was minted isolated (no hub reconcile). Surfaced only when the winning event carries it;
            # reconcile is M15.
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if payload.get("iso"):
                best["iso"] = True
                if payload.get("iso_sentinel"):
                    best["iso_sentinel"] = payload["iso_sentinel"]
    if best is None:
        return {"holder": None, "epoch": -1, "mode": "roaming"}
    return best


def _grant_seq(event: dict[str, Any], payload: dict[str, Any] | None = None) -> int:
    """The grantor-local monotonic ``payload.grant_seq`` (default 0) -- the secondary sort key that
    orders same-``(scope, epoch)`` grants so a renew (grant_seq+1) wins over its predecessor."""
    if payload is None:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    try:
        value = payload.get("grant_seq")
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def reduce_lease(events: list[dict[str, Any]], *, now: str | None = None) -> dict[str, dict[str, Any]]:
    """Per-scope_key lease projection (MAX-EPOCH-WINS + grant_seq + expiry).

    For each scope_key the winner is the top ``lease/granted`` ranked by
    ``(epoch, grant_seq, created_at, id)`` -- grant_seq (``payload.grant_seq``, default 0) is the
    grantor-local monotonic secondary key so a renew re-granting at the SAME epoch with grant_seq+1 wins.
    The holder is ``payload.holder`` when present (the appender is the grantor, granting on behalf of a
    holder node), else the event's ``node_id``.

    State resolution is grant_seq-aware so a NEW grant at the SAME epoch after a release/expiry takes the
    scope cleanly (expiry releases): a release/revoke/expired at ``(scope, epoch)`` frees the winning
    grant only when the released grant_seq >= the winner's grant_seq. A release-side event carrying NO
    ``payload.grant_seq`` frees ANY grant at that epoch (a sentinel high seq -- the M9 shape). Otherwise,
    when ``now`` (ISO str) is given and the grant carries a ``payload.expires_at`` with ``now >
    expires_at``, the state is ``expired`` (holder None); else ``held``. Expiry is a pure clock function,
    so both replicas reduce identically for the same ``now``.

    Returns ``{scope_key: {scope_key, holder, epoch, grant_seq, state in held|free|expired, expires_at,
    id}}``."""
    canonical = _canonical(events)
    # (scope_key, epoch) -> highest released grant_seq (a release without grant_seq => sentinel that
    # frees any grant at that epoch, matching the M9 shape). A grant at (scope, epoch, seq) is freed only
    # when released_up_to[(scope, epoch)] >= seq.
    _RELEASE_ALL = 1 << 62
    released_up_to: dict[tuple[str, int], int] = {}
    for event in canonical:
        if event.get("event_kind") != "lease":
            continue
        if event.get("marker") in ("released", "revoked", "expired"):
            scope = event.get("scope_key")
            if scope is None:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            seq = _grant_seq(event, payload) if payload.get("grant_seq") is not None else _RELEASE_ALL
            key = (scope, _epoch(event))
            released_up_to[key] = max(released_up_to.get(key, -1), seq)

    best: dict[str, dict[str, Any]] = {}
    best_key: dict[str, tuple[int, int, str, str]] = {}
    for event in canonical:
        if event.get("event_kind") != "lease" or event.get("marker") != "granted":
            continue
        scope = event.get("scope_key")
        if scope is None:
            continue
        epoch = _epoch(event)
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        grant_seq = _grant_seq(event, payload)
        key = (epoch, grant_seq, str(event.get("created_at") or ""), str(event.get("id") or ""))
        if scope not in best_key or key > best_key[scope]:
            best_key[scope] = key
            expires_at = payload.get("expires_at")
            holder = payload.get("holder") or event.get("node_id")
            if released_up_to.get((scope, epoch), -1) >= grant_seq:
                state = "free"
            elif now is not None and expires_at and str(now) > str(expires_at):
                state = "expired"
            else:
                state = "held"
            entry = {
                "scope_key": scope,
                "holder": holder if state == "held" else None,
                "epoch": epoch,
                "grant_seq": grant_seq,
                "state": state,
                "expires_at": expires_at,
                "id": event.get("id"),
            }
            # M10b lease-mode passthrough: the grant's advisory|authoritative mode (default advisory for
            # M9/M10a grants that carried none), so a reader can tell a CP mutex from an AP best-effort
            # hint. Record-level only; the mutex enforcement lives in coordination.grant/renew_lease.
            mode = payload.get("mode")
            entry["mode"] = mode if mode in ("advisory", "authoritative") else "advisory"
            best[scope] = entry
    return best


def reduce_conflicts(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Fork / merge-conflict blocking-span projection (v8 M12, plan §B.4).

    A ``conflict/detected`` row opens a blocking span; it stays OPEN until a LATER ``conflict/resolved``
    whose ``payload.source_conflict_id`` equals its id closes it. Returns ``{open: [...], resolved_count:
    int}`` where each open entry is ``{id, scope_key, created_at, decision, options, recommendation}``
    (the surface-and-confirm fields), sorted by ``(created_at, id)``.

    Deterministic under reorder/duplication like every reducer here: it folds over the canonical
    (deduped, ``(created_at, id)``-sorted) sequence, so a detected/resolved pair reduces identically
    regardless of arrival order (a resolved seen before its detected still closes it -- membership, not
    sequence, decides)."""
    canonical = _canonical(events)
    resolved_ids: set[str] = set()
    resolved_count = 0
    detected: dict[str, dict[str, Any]] = {}
    for event in canonical:
        if event.get("event_kind") != "conflict":
            continue
        marker = event.get("marker")
        if marker == "detected":
            eid = event.get("id")
            if eid is None:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            detected[str(eid)] = {
                "id": eid,
                "scope_key": event.get("scope_key"),
                "created_at": event.get("created_at"),
                "decision": payload.get("decision"),
                "options": payload.get("options"),
                "recommendation": payload.get("recommendation"),
            }
        elif marker == "resolved":
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            source = payload.get("source_conflict_id")
            resolved_count += 1
            if source is not None:
                resolved_ids.add(str(source))
    open_entries = [entry for eid, entry in detected.items() if eid not in resolved_ids]
    open_entries.sort(key=lambda e: (str(e.get("created_at") or ""), str(e.get("id") or "")))
    return {"open": open_entries, "resolved_count": resolved_count}


# Dispatch lifecycle ranking (v8 M14, plan §B.5). The state is the lifecycle-LATEST marker with
# TERMINAL-WINS: once a dispatch reaches a terminal marker (completed/failed/canceled) a stray later
# non-terminal marker (a duplicate/reordered requested/accepted) NEVER reopens it. Non-terminal markers
# order by this lifecycle rank; a terminal marker outranks every non-terminal. Two terminals at the same
# dispatch (should not happen -- the engine gates transitions) tiebreak by (created_at, id) like every
# reducer here, so the fold stays deterministic under reorder/duplication.
_DISPATCH_RANK: dict[str, int] = {
    "requested": 0,
    "accepted": 1,
    "started": 2,
    "completed": 3,
    "failed": 3,
    "canceled": 3,
}
_DISPATCH_TERMINAL = frozenset({"completed", "failed", "canceled"})


def reduce_dispatches(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-dispatch remote-dispatch projection (v8 M14, plan §B.5), keyed by ``payload.dispatch_id``.

    Folds dispatch-kind events (markers requested/accepted/started/completed/failed/canceled) into one
    entry per ``dispatch_id``. The reduced ``state`` is the lifecycle-latest marker with TERMINAL-WINS:
    completed/failed/canceled are terminal and a stray later non-terminal marker never reopens a
    terminated dispatch. Non-terminal markers rank by lifecycle order (requested<accepted<started); a
    terminal outranks every non-terminal; ties break by (rank, created_at, id).

    Facts are carried from any event that declares them; current producers put target/scope/origin on
    ``requested`` and artifact metadata on ``completed``. ``id``/``created_at`` are the winning event's. Deterministic
    under reorder/duplication (module invariant): driven off the canonical (deduped, sorted) sequence.

    Returns ``{dispatch_id: {dispatch_id, state, target_node, scope_key, origin_node, id, created_at
    [, artifact]}}``."""
    canonical = _canonical(events)
    out: dict[str, dict[str, Any]] = {}
    # Per-dispatch ranking key of the state-deciding event: (rank, created_at, id).
    best_key: dict[str, tuple[int, str, str]] = {}
    for event in canonical:
        if event.get("event_kind") != "dispatch":
            continue
        marker = event.get("marker")
        rank = _DISPATCH_RANK.get(marker)
        if rank is None:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        dispatch_id = payload.get("dispatch_id")
        if not dispatch_id:
            continue
        dispatch_id = str(dispatch_id)
        entry = out.setdefault(
            dispatch_id,
            {
                "dispatch_id": dispatch_id,
                "state": None,
                "target_node": None,
                "scope_key": None,
                "origin_node": None,
                "id": None,
                "created_at": None,
            },
        )
        # Carry declared facts from whichever event holds them (request payload / completion payload),
        # independent of which event wins the state (a later terminal need not re-carry the request fields).
        if payload.get("target_node") is not None:
            entry["target_node"] = payload.get("target_node")
        if payload.get("origin_node") is not None:
            entry["origin_node"] = payload.get("origin_node")
        scope = event.get("scope_key")
        if scope is not None:
            entry["scope_key"] = scope
        if marker == "completed" and payload.get("artifact") is not None:
            entry["artifact"] = {
                "artifact": payload.get("artifact"),
                "sha": payload.get("sha"),
                "branch": payload.get("branch"),
            }
        key = (rank, str(event.get("created_at") or ""), str(event.get("id") or ""))
        prior = best_key.get(dispatch_id)
        # Terminal-wins: a terminal marker already recorded is never displaced by a later non-terminal,
        # even one that sorts later by (created_at, id) -- rank is the primary key and terminal rank (3)
        # tops every non-terminal, so this is already handled by the (rank, ...) comparison. The only
        # extra guard: among the SAME rank the higher (created_at, id) wins (deterministic tiebreak).
        if prior is None or key > prior:
            best_key[dispatch_id] = key
            entry["state"] = marker
            entry["id"] = event.get("id")
            entry["created_at"] = event.get("created_at")
    return out
