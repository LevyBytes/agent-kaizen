"""Remote-dispatch lifecycle engine over a FleetStore (v8 M14, plan §B.5, gate F4).

Cross-machine work: a dispatcher hands a run to a target node ONLY after the required leases are
granted; the target's supervisor runs it in its assigned worktree/branch and returns patch/branch/
artifact metadata; applying the returned patch INTO the local worktree needs an EXPLICIT approval
(§B.5). This module is the pure lifecycle over an injected :class:`fleet.store.FleetStore`: every
transition APPENDS one ``dispatch/<marker>`` coord_event through the store's single write path (kind x
marker + redaction + immediate commit) and INSERTs/UPDATEs the ``remote_dispatches`` row; nothing
mutates a mutable coord row. It reuses the M9 dispatch markers {requested, accepted, started,
completed, failed, canceled} -- NO new coord kinds/markers.

Discipline held here (mirrors :mod:`fleet.coordination` / :class:`fleet.coordination.HandoffEngine`):

- **Lease-gated request (§B.5 / M11 endpoint parity).** ``request_dispatch`` refuses
  ``DENIED_DISPATCH_UNLEASED`` unless a FRESH ``reduce_lease`` shows the origin node as the active
  GRANTED holder of scope_key -- the exact gate the M11 control endpoint enforces, reused server-side.
- **Target-side claim.** Only the row's ``target_node_id == store.node_id`` may ``accept`` (else
  ``DENIED_DISPATCH_NOT_TARGET``); an illegal transition (e.g. complete before accept) refuses
  ``DENIED_DISPATCH_STATE`` -- transitions are gated against the FRESH reduced state.
- **Redaction pass-path (basename/sha only).** A ``completed`` payload carries ONLY {artifact:
  BASENAME, sha, branch} -- never an absolute path (the HandoffEngine basename-only precedent). The
  full artifact path stays in the returned record, never in a synced coord_event.
- **Apply needs approval (§B.5).** ``apply_dispatch`` refuses ``DENIED_DISPATCH_APPLY_UNAPPROVED``
  unless an injected ``approvals_lookup(approval_id)`` returns an APPROVED (state "approved") C4-shaped
  record; then ``git apply --check`` (dry-run) FIRST -- a non-zero check leaves the tree UNTOUCHED and
  refuses ``DENIED_DISPATCH_APPLY_FAILED`` -- and only on a clean check the real ``git apply``.

The git runner is injected (:data:`fleet.coordination.GitRunner` shape); tests inject a runner bound to
a SCRATCH repo, so this module never defaults to acting on the real repo.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from ..denials import KaizenDenied
from . import identity, reducers
from .coordination import GitRunner, _default_git_runner, current_leases

# The dispatch coord markers (already in COORD_EVENT_KIND_MARKERS -- reused, no new markers).
_TERMINAL = frozenset({"completed", "failed", "canceled"})

# Legal forward transitions of the FRESH reduced dispatch state. A request is the genesis (no prior
# state); every other transition requires the exact predecessor. Terminal states accept nothing further.
_LEGAL_NEXT: dict[str | None, frozenset[str]] = {
    None: frozenset({"accepted"}),           # a freshly-requested dispatch (no accept yet) may be accepted
    "requested": frozenset({"accepted"}),
    "accepted": frozenset({"started"}),
    "started": frozenset({"completed", "failed"}),
    # canceled is reachable from any NON-terminal state (a truthful abort); handled explicitly below.
}


# --- fresh reads (never cached) ------------------------------------------------------------------

def current_dispatches(store: Any) -> dict[str, dict[str, Any]]:
    """The fresh per-dispatch projection ``{dispatch_id: {...}}`` (always re-reduced from coord_events)."""
    return reducers.reduce_dispatches(store.coord_events())


def _reduced(store: Any, dispatch_id: str) -> dict[str, Any] | None:
    """Fresh reduced entry for one dispatch_id or None."""
    return current_dispatches(store).get(dispatch_id)


def _dispatch_row(store: Any, dispatch_id: str) -> tuple[Any, ...] | None:
    """Raw remote_dispatches row tuple (id, created_at, target_node_id, required_leases_json, status, payload_json) or None -- COLUMN ORDER IS LOAD-BEARING: row[2]=target_node_id drives every NOT_TARGET check (accept/start/complete/fail); schema confirmed db_schema.py:1056-1063."""
    rows = store._read_all(
        "SELECT id, created_at, target_node_id, required_leases_json, status, payload_json "
        "FROM remote_dispatches WHERE id = ?",
        (dispatch_id,),
    )
    return rows[0] if rows else None


def _row_payload(row: tuple[Any, ...]) -> dict[str, Any]:
    """Safe JSON-decode of row[5] payload_json to dict ({} on malformed/non-dict); source of scope_key for _advance's synced event."""
    try:
        payload = json.loads(row[5]) if row[5] else {}
    except (ValueError, TypeError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


# --- request (dispatcher side; LEASE-GATED) ------------------------------------------------------

def request_dispatch(
    store: Any,
    *,
    target_node: str,
    task: str,
    scope_key: str,
    required_leases: list[str] | None = None,
    origin_node: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """LEASE-GATED dispatch request (§B.5). The origin node (``origin_node`` or ``store.node_id``) must
    be the active GRANTED holder of ``scope_key`` in a FRESH ``reduce_lease`` -- else
    ``DENIED_DISPATCH_UNLEASED`` (exit 2). This is the same gate the M11 ``/v1/dispatch`` endpoint
    enforces, reused server-side.

    On success: mint a node-tagged ``dispatch_id`` (``identity.coord_id("rd", ...)``), append
    ``dispatch/requested`` {dispatch_id, target_node, task, scope_key, origin_node}, and INSERT a
    ``remote_dispatches`` row (status "requested", payload_json carrying task/scope). The id tag names
    the appending node for collision resistance; ``origin_node`` remains the authoritative origin."""
    origin = origin_node or store.node_id
    active = current_leases(store).get(scope_key)
    if not active or active.get("state") != "held" or active.get("holder") != origin:
        raise KaizenDenied(
            "DENIED_DISPATCH_UNLEASED",
            {
                "scope_key": scope_key,
                "origin_node": origin,
                "current_holder": active.get("holder") if active else None,
                "required_action": "hold the granted lease on scope_key before dispatching",
            },
            exit_code=2,
        )
    dispatch_id = identity.coord_id("rd", store.node_id)
    leases = list(required_leases) if required_leases is not None else [scope_key]
    payload: dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "target_node": target_node,
        "task": task,
        "scope_key": scope_key,
    }
    payload["origin_node"] = origin
    # Append the requested event FIRST (append-only commits immediately -- the ledger is the source of
    # truth); then the row is the queryable status projection the executor/target reads.
    event = store.append_coord_event(
        "dispatch",
        "requested",
        summary=summary or f"Dispatch requested to {target_node} for {scope_key}.",
        scope_key=scope_key,
        payload=payload,
    )
    created = event["created_at"]
    row_payload = {"task": task, "scope_key": scope_key, "origin_node": origin}
    leases_json = json.dumps(leases, ensure_ascii=False, sort_keys=True)
    row_payload_json = json.dumps(row_payload, ensure_ascii=False, sort_keys=True)

    def op(conn: Any) -> None:
        conn.execute(
            "INSERT INTO remote_dispatches "
            "(id, created_at, target_node_id, required_leases_json, status, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (dispatch_id, created, target_node, leases_json, "requested", row_payload_json),
        )

    store._write(op)
    return {
        "status": "OK",
        "dispatch_id": dispatch_id,
        "target_node": target_node,
        "scope_key": scope_key,
        "task": task,
        "origin_node": origin,
        "required_leases": leases,
        "state": "requested",
        "coord_event_id": event["id"],
    }


# --- target-side transitions ---------------------------------------------------------------------

def accept_dispatch(store: Any, dispatch_id: str, *, summary: str | None = None) -> dict[str, Any]:
    """TARGET-side claim: only the row's ``target_node_id == store.node_id`` may accept (else
    ``DENIED_DISPATCH_NOT_TARGET``, exit 2). Transition requested->accepted; refuses
    ``DENIED_DISPATCH_STATE`` for an illegal transition (e.g. accepting a completed/failed dispatch).
    Appends ``dispatch/accepted`` {dispatch_id}."""
    row = _require_row(store, dispatch_id)
    if row[2] != store.node_id:
        raise KaizenDenied(
            "DENIED_DISPATCH_NOT_TARGET",
            {
                "dispatch_id": dispatch_id,
                "target_node": row[2],
                "node_id": store.node_id,
                "required_action": "only the dispatch's target node may accept it",
            },
            exit_code=2,
        )
    return _advance(store, dispatch_id, row, "accepted", summary=summary or f"Dispatch {dispatch_id} accepted.")


def start_dispatch(store: Any, dispatch_id: str, *, summary: str | None = None) -> dict[str, Any]:
    """TARGET-side: accepted->started. Appends ``dispatch/started`` {dispatch_id}. Refuses
    ``DENIED_DISPATCH_NOT_TARGET`` for a non-target, ``DENIED_DISPATCH_STATE`` for an illegal
    transition."""
    row = _require_target_row(store, dispatch_id)
    return _advance(store, dispatch_id, row, "started", summary=summary or f"Dispatch {dispatch_id} started.")


def complete_dispatch(
    store: Any,
    dispatch_id: str,
    *,
    artifact_name: str | None = None,
    artifact_sha: str | None = None,
    branch: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """TARGET-side: started->completed. The completed payload carries ONLY {artifact: BASENAME, sha,
    branch} -- NEVER an absolute path (redaction pass-path; the HandoffEngine basename-only precedent);
    an absolute ``artifact_name`` is reduced to its basename before it enters the synced event. Refuses
    ``DENIED_DISPATCH_NOT_TARGET`` / ``DENIED_DISPATCH_STATE``."""
    row = _require_target_row(store, dispatch_id)
    extra: dict[str, Any] = {}
    if artifact_name:
        # Basename ONLY -- an absolute path would trip the redaction pass-path (user_home_path).
        extra["artifact"] = Path(str(artifact_name)).name
    if artifact_sha:
        extra["sha"] = artifact_sha
    if branch:
        extra["branch"] = branch
    return _advance(
        store, dispatch_id, row, "completed",
        summary=summary or f"Dispatch {dispatch_id} completed.",
        payload_extra=extra,
    )


def fail_dispatch(store: Any, dispatch_id: str, reason: str, *, summary: str | None = None) -> dict[str, Any]:
    """TARGET-side: started->failed (a truthful failure). Appends ``dispatch/failed`` {dispatch_id,
    reason}. ``reason`` is truncated here and the store's redaction gate refuses secrets before sync.
    Refuses ``DENIED_DISPATCH_NOT_TARGET`` / ``DENIED_DISPATCH_STATE``."""
    row = _require_target_row(store, dispatch_id)
    return _advance(
        store, dispatch_id, row, "failed",
        summary=summary or f"Dispatch {dispatch_id} failed.",
        payload_extra={"reason": str(reason)[:180]},
    )


def cancel_dispatch(store: Any, dispatch_id: str, *, reason: str | None = None, summary: str | None = None) -> dict[str, Any]:
    """Cancel a NON-terminal dispatch (a truthful abort from any non-terminal state). Appends
    ``dispatch/canceled`` {dispatch_id[, reason]}. Refuses ``DENIED_DISPATCH_STATE`` when the dispatch
    is already terminal (a terminated dispatch is not re-cancelable). NOT target-gated: either side may
    cancel an in-flight dispatch (the dispatcher may recall it, the target may abort it)."""
    row = _require_row(store, dispatch_id)
    state = _state_of(store, dispatch_id)
    if state in _TERMINAL:
        raise KaizenDenied(
            "DENIED_DISPATCH_STATE",
            {
                "dispatch_id": dispatch_id,
                "current_state": state,
                "attempted": "canceled",
                "required_action": "a terminal dispatch (completed/failed/canceled) cannot be canceled",
            },
            exit_code=2,
        )
    extra = {"reason": str(reason)[:180]} if reason else {}
    return _advance(
        store, dispatch_id, row, "canceled",
        summary=summary or f"Dispatch {dispatch_id} canceled.",
        payload_extra=extra,
        allow_from_any_nonterminal=True,
    )


# --- dispatcher-side APPLY (needs explicit approval) ---------------------------------------------

def apply_dispatch(
    store: Any,
    git_runner: GitRunner | None,
    dispatch_id: str,
    *,
    artifact_path: str,
    approval_id: str,
    approvals_lookup: Callable[[str], dict[str, Any] | None],
    repo_root: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """Dispatcher-side APPLY of the returned patch INTO the local worktree (§B.5: "apply into the active
    worktree needs explicit approval").

    REQUIRES an explicit approval: ``approvals_lookup(approval_id)`` must return an APPROVED C4-shaped
    record (``state`` == "approved") -- else ``DENIED_DISPATCH_APPLY_UNAPPROVED`` (exit 2), tree
    untouched. Then ``git apply --check`` (dry-run) FIRST: a non-zero check refuses
    ``DENIED_DISPATCH_APPLY_FAILED`` with the tree UNTOUCHED (the check does not mutate); only on a clean
    check does the real ``git apply`` run (a non-zero real apply also refuses APPLY_FAILED). On success
    append ``resolution/recorded`` {dispatch_id, applied: true, artifact: BASENAME, approval_id}."""
    record = approvals_lookup(approval_id) if callable(approvals_lookup) else None
    state = (record or {}).get("state") if isinstance(record, dict) else None
    if state != "approved":
        raise KaizenDenied(
            "DENIED_DISPATCH_APPLY_UNAPPROVED",
            {
                "dispatch_id": dispatch_id,
                "approval_id": approval_id,
                "approval_state": state,
                "required_action": "apply into the active worktree needs an APPROVED approval (C4 state 'approved')",
            },
            exit_code=2,
        )
    git = git_runner if git_runner is not None else _default_git_runner(str(repo_root or "."))
    # Dry-run FIRST so a bad patch never touches the tree (--check verifies applicability, mutates
    # nothing). A non-zero check == the patch does not apply cleanly -> refuse, tree untouched.
    check = git("apply", "--check", "--", artifact_path)
    if check.returncode != 0:
        raise KaizenDenied(
            "DENIED_DISPATCH_APPLY_FAILED",
            {
                "dispatch_id": dispatch_id,
                "stage": "check",
                "reason": (check.stderr or check.stdout or "git apply --check failed")[:180],
                "required_action": "the returned patch does not apply cleanly onto the local tree; the tree was NOT modified",
            },
            exit_code=2,
        )
    applied = git("apply", "--", artifact_path)
    if applied.returncode != 0:
        raise KaizenDenied(
            "DENIED_DISPATCH_APPLY_FAILED",
            {
                "dispatch_id": dispatch_id,
                "stage": "apply",
                "reason": (applied.stderr or applied.stdout or "git apply failed")[:180],
                "required_action": "git apply failed after a clean check; inspect the tree state",
            },
            exit_code=2,
        )
    event = store.append_coord_event(
        "resolution",
        "recorded",
        summary=summary or f"Dispatch {dispatch_id} patch applied to worktree ({approval_id}).",
        payload={
            "dispatch_id": dispatch_id,
            "applied": True,
            "artifact": Path(str(artifact_path)).name,  # basename only in the synced payload
            "approval_id": approval_id,
        },
    )
    return {
        "status": "OK",
        "dispatch_id": dispatch_id,
        "applied": True,
        "approval_id": approval_id,
        "resolution_event_id": event["id"],
    }


# --- list / status -------------------------------------------------------------------------------

def list_dispatches(store: Any) -> dict[str, Any]:
    """List all dispatches via the reducer (the coord ledger is the source of truth). Returns
    ``{status, dispatches: [...], count}`` sorted by (created_at, dispatch_id)."""
    reduced = current_dispatches(store)
    items = sorted(
        reduced.values(),
        key=lambda d: (str(d.get("created_at") or ""), str(d.get("dispatch_id") or "")),
    )
    return {"status": "OK", "dispatches": items, "count": len(items)}


def status_dispatch(store: Any, dispatch_id: str) -> dict[str, Any]:
    """The reduced state of one dispatch (``DENIED_DISPATCH_NOT_FOUND`` when unknown to the ledger)."""
    reduced = _reduced(store, dispatch_id)
    if reduced is None:
        raise KaizenDenied(
            "DENIED_DISPATCH_NOT_FOUND",
            {"dispatch_id": dispatch_id, "required_action": "check the dispatch id with list"},
            exit_code=1,
        )
    return {"status": "OK", **reduced}


# --- internals -----------------------------------------------------------------------------------

def _require_row(store: Any, dispatch_id: str) -> tuple[Any, ...]:
    """Return the dispatch row or deny with DENIED_DISPATCH_NOT_FOUND."""
    row = _dispatch_row(store, dispatch_id)
    if row is None:
        raise KaizenDenied(
            "DENIED_DISPATCH_NOT_FOUND",
            {"dispatch_id": dispatch_id, "required_action": "request the dispatch first (no remote_dispatches row)"},
            exit_code=1,
        )
    return row


def _require_target_row(store: Any, dispatch_id: str) -> tuple[Any, ...]:
    """Return a dispatch owned by this target node or deny with DENIED_DISPATCH_NOT_TARGET."""
    row = _require_row(store, dispatch_id)
    if row[2] != store.node_id:
        raise KaizenDenied(
            "DENIED_DISPATCH_NOT_TARGET",
            {
                "dispatch_id": dispatch_id,
                "target_node": row[2],
                "node_id": store.node_id,
                "required_action": "only the dispatch's target node may advance this transition",
            },
            exit_code=2,
        )
    return row


def _state_of(store: Any, dispatch_id: str) -> str | None:
    """Return the fresh reduced-ledger state for a dispatch when present."""
    reduced = _reduced(store, dispatch_id)
    return reduced.get("state") if reduced else None


def _advance(
    store: Any,
    dispatch_id: str,
    row: tuple[Any, ...],
    marker: str,
    *,
    summary: str,
    payload_extra: dict[str, Any] | None = None,
    allow_from_any_nonterminal: bool = False,
) -> dict[str, Any]:
    """Append the ``dispatch/<marker>`` transition after checking it is legal from the FRESH reduced
    state, then UPDATE the remote_dispatches row status. Refuses ``DENIED_DISPATCH_STATE`` for an illegal
    transition (record-NOTHING-then-refuse: an illegal transition is a caller bug, not an auditable
    split-brain, so unlike the epoch fence it does not append a conflict event first)."""
    state = _state_of(store, dispatch_id)
    legal = allow_from_any_nonterminal and state not in _TERMINAL
    if not legal:
        allowed = _LEGAL_NEXT.get(state, frozenset())
        if marker not in allowed:
            raise KaizenDenied(
                "DENIED_DISPATCH_STATE",
                {
                    "dispatch_id": dispatch_id,
                    "current_state": state,
                    "attempted": marker,
                    "allowed": sorted(allowed),
                    "required_action": f"a dispatch in state {state!r} cannot transition to {marker!r}",
                },
                exit_code=2,
            )
    payload: dict[str, Any] = {"dispatch_id": dispatch_id}
    if payload_extra:
        payload.update(payload_extra)
    scope_key = _row_payload(row).get("scope_key")
    event = store.append_coord_event(
        "dispatch",
        marker,
        summary=summary,
        scope_key=scope_key,
        payload=payload,
    )

    def op(conn: Any) -> None:
        conn.execute(
            "UPDATE remote_dispatches SET status = ? WHERE id = ?",
            (marker, dispatch_id),
        )

    store._write(op)
    result: dict[str, Any] = {
        "status": "OK",
        "dispatch_id": dispatch_id,
        "state": marker,
        "coord_event_id": event["id"],
    }
    if payload_extra:
        result.update(payload_extra)
    return result


def sha256_text(text: str) -> str:
    """sha256 hex of a patch artifact's utf-8 bytes -- the completion metadata a target returns alongside
    the basename (the sha travels in the synced payload; the bytes/path do not)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
