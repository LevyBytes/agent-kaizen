"""Orchestration session records (v8 M2 / C1-C5) -- record-only, process/network-free.

Durable session/goal/approval records for the true-orchestration control plane. These are RECORD
handlers only: they never spawn processes, open sockets, or drive vendor children (the supervisor
does that at M1+). The import-guard test enforces the purity boundary (no subprocess/socket/asyncio/
http.server here).

- C1 :func:`session_start`   -- create or resume an ``agent_sessions`` envelope.
- C2 :func:`instruction_add` -- append a session-scoped ``user_instructions`` row.
- C3 :func:`goal_upsert`     -- create/update a ``goals`` row (Kaizen goal is canonical cross-vendor).
- C4 :func:`approval_upsert` -- create/update an ``approval_requests`` row; the C4 state machine
                                refuses re-deciding an already-decided request.
- C5 :func:`session_timeline`-- read a session with its instructions/goals/approvals joined.

The ``mode_profiles`` table (extensible per-engine ``profile_json`` mapping) ships schema-only at M2:
its shape is the exit criterion (Claude drops in additively at M-CLAUDE with zero migration), it is
Q8-validatable via the ``mode_profile`` kind, and K7-purgeable; a dedicated CLI op arrives with its
first live producer (the supervisor adapters, M4+).

controller/mode are validated through :mod:`kaizen_components.orchestration.modes` (the single parse
source); ``auth_mode`` is validated here against {none, subscription, api-key} -- 'subscription' and
'api-key' are a RESERVED value space for the deferred M-CLAUDE Claude lanes (accepted now with no live
producer, proving the record layer accepts the deferred value space).

v8 M10b node fence (§B.3): when ``dist_mode() != "off"`` a session is node-owned -- C1 stamps
owning_node/node_epoch and the C2/C3/C4 mutations pass a ``write_tx`` ``session_fence`` so a FOREIGN
node's mutation of a node-owned session is refused ``DENIED_STALE_FENCE``. dist_mode()=="off" (the
default) is INERT: no stamping, no fence, byte-identical to pre-M10b -- and fleet.identity is imported
LAZILY, so an off-mode process never loads the fleet package.
"""

from __future__ import annotations

import json
from typing import Any

from .db import fetch_all, fetch_one, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import utc_text_hash
from .orchestration.modes import dist_mode, parse_controller, parse_session_mode
from .paths import DB_ROOT, REPO_ROOT, path_in_repo, read_text_file
from .redaction import assert_redacted
from .schemas import validate_record
from .session_protocol import canonical_title

# auth_mode billing lane. 'subscription'/'api-key' are RESERVED (no live producer before M-CLAUDE);
# validated here rather than in modes.py because it is a session-record field, not a config seam.
AUTH_MODES: tuple[str, ...] = ("none", "subscription", "api-key")

# v8 H2.1 UI permission ceiling (plan|ask|agent|full). DISTINCT from the session mode (orchestration
# semantics, orchestration.modes.SESSION_MODES): the permission_mode is the owner-selected permission
# profile carried on the C1 session + its T5 runs. Validated here rather than in modes.py because it is a
# session-record field. Mode SEMANTICS live in code; mode_profiles stores only designated roots.
PERMISSION_MODES: tuple[str, ...] = ("plan", "ask", "agent", "full")

# C4 approval state machine. An OPEN request may move to any decided/cleared/deferred state; a request
# already in a DECIDED terminal state may not be re-decided (the exit-criterion invariant). 'deferred'
# is re-openable (the engine may re-surface it), so it is not terminal.
_APPROVAL_DECIDED = {"approved", "denied", "canceled", "cleared_by_engine"}
_APPROVAL_STATES = ("open", "approved", "denied", "canceled", "cleared_by_engine", "deferred")
_APPROVAL_TYPES = ("tool_approval", "clarifying_question", "plan_exit", "requestUserInput")


def _text(args: Any, name: str, default: str = "") -> str:
    """None falls back to <name>_file then default; an EXPLICIT "" returns as-is (no fallback) -- see R2."""
    value = getattr(args, name, None)
    if value is None:
        file_value = getattr(args, f"{name}_file", None)
        if file_value:
            return read_text_file(file_value)
        return default
    return value


def _require(args: Any, name: str, code: str, action: str) -> str:
    """Denies exit 2 (code + required_action) when the attr is falsy; returns the value otherwise."""
    value = getattr(args, name, None)
    if not value:
        raise KaizenDenied(code, {"required_action": action}, exit_code=2)
    return value


def _payload(args: Any) -> dict[str, Any]:
    """Parses --payload-json / --payload-json-file to a dict; DENIED_PAYLOAD_TYPE on non-object; empty -> {}."""
    raw = getattr(args, "payload_json", None)
    if getattr(args, "payload_json_file", None):
        raw = read_text_file(args.payload_json_file)
    payload = json.loads(raw) if raw else {}
    if not isinstance(payload, dict):
        raise KaizenDenied(
            "DENIED_PAYLOAD_TYPE",
            {"required_action": "--payload-json must be a JSON object"},
            exit_code=2,
        )
    return payload


def _fetch_session(conn: Any, session_id: str) -> tuple[Any, ...] | None:
    """Existence probe (id row or None) on the given write/read conn."""
    return conn.execute("SELECT id FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()


def insert_approval_conn(
    conn: Any,
    *,
    approval_id: str,
    session_id: str,
    correlation_id: str | None,
    request_type: str,
    state: str,
    summary: str,
    decided_by: str | None = None,
    rule_id: str | None = None,
    created_at: str | None = None,
    is_test: bool = False,
) -> dict[str, Any]:
    """Insert one C4 row on an existing write connection.

    The daemon approval broker composes this with a T6 event in the *same* ``write_tx``. Keeping this
    helper connection-scoped avoids the old C4/T6 split-commit window without adding a table or event
    kind. Callers remain responsible for their surrounding transaction and optional session fence.
    """
    if _fetch_session(conn, session_id) is None:
        raise KaizenDenied(
            "DENIED_SESSION_NOT_FOUND",
            {"session_id": session_id, "required_action": "open the session with C1 first"},
            exit_code=1,
        )
    if state not in _APPROVAL_STATES:
        raise KaizenDenied(
            "DENIED_APPROVAL_STATE_INVALID",
            {"state": state, "allowed": list(_APPROVAL_STATES), "required_action": "use an allowed approval state"},
            exit_code=2,
        )
    if request_type not in _APPROVAL_TYPES:
        raise KaizenDenied(
            "DENIED_APPROVAL_TYPE_INVALID",
            {"request_type": request_type, "allowed": list(_APPROVAL_TYPES),
             "required_action": "use tool_approval|clarifying_question|plan_exit|requestUserInput"},
            exit_code=2,
        )
    clean = {
        k: v
        for k, v in {
            "session_id": session_id, "correlation_id": correlation_id, "request_type": request_type,
            "state": state, "decided_by": decided_by, "rule_id": rule_id, "summary": summary,
        }.items()
        if v not in (None, "")
    }
    validate_record("approval_request", clean)
    assert_redacted({"summary": summary, "correlation_id": correlation_id or "", "rule_id": rule_id or ""})
    created = created_at or now()
    content_hash = utc_text_hash({"id": approval_id, **clean})
    conn.execute(
        "INSERT INTO approval_requests "
        "(id, created_at, updated_at, session_id, correlation_id, request_type, state, decided_by, "
        "rule_id, summary, content_hash, is_test) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (approval_id, created, created, session_id, correlation_id, request_type, state, decided_by,
         rule_id, summary, content_hash, 1 if is_test else 0),
    )
    return {"id": approval_id, "session_id": session_id, "correlation_id": correlation_id,
            "request_type": request_type, "state": state, "summary": summary,
            "decided_by": decided_by, "rule_id": rule_id, "content_hash": content_hash}


def transition_approval_conn(
    conn: Any,
    approval_id: str,
    target_state: str,
    *,
    decided_by: str | None = None,
    rule_id: str | None = None,
    summary: str | None = None,
    open_only: bool = False,
) -> dict[str, Any]:
    """Atomically transition a C4 row on an existing write connection.

    ``open_only=True`` is the broker resolution primitive: the guarded UPDATE elects exactly one
    timeout/accept/reject winner. A duplicate or late decision performs no write and returns the stable
    ``DENIED_APPROVAL_ALREADY_DECIDED`` denial. ``open_only=False`` retains the legacy CLI's ability to
    move a deferred request while still closing its read/update TOCTOU window. The post-update state
    and content-hash comparison is the final backstop proving this caller's exact transition landed.
    """
    if target_state not in _APPROVAL_STATES:
        raise KaizenDenied(
            "DENIED_APPROVAL_STATE_INVALID",
            {"state": target_state, "allowed": list(_APPROVAL_STATES), "required_action": "use an allowed approval state"},
            exit_code=2,
        )
    row = conn.execute(
        "SELECT session_id, correlation_id, request_type, state, summary FROM approval_requests WHERE id = ?",
        (approval_id,),
    ).fetchone()
    if row is None:
        raise KaizenDenied(
            "DENIED_APPROVAL_NOT_FOUND",
            {"id": approval_id, "required_action": "check the approval id with C5"},
            exit_code=1,
        )
    current_state = row[3]
    blocked = current_state != "open" if open_only else current_state in _APPROVAL_DECIDED
    if blocked:
        raise KaizenDenied(
            "DENIED_APPROVAL_ALREADY_DECIDED",
            {"id": approval_id, "current_state": current_state, "attempted_state": target_state,
             "required_action": "an approval is decided once; inspect it with C5"},
            exit_code=2,
        )
    resolved_summary = summary or row[4]
    clean = {
        k: v
        for k, v in {
            "session_id": row[0], "correlation_id": row[1], "request_type": row[2],
            "state": target_state, "decided_by": decided_by, "rule_id": rule_id,
            "summary": resolved_summary,
        }.items()
        if v not in (None, "")
    }
    validate_record("approval_request", clean)
    assert_redacted({"summary": resolved_summary, "correlation_id": row[1] or "", "rule_id": rule_id or ""})
    updated = now()
    content_hash = utc_text_hash({"id": approval_id, **clean})
    # Compare against the state read on this transaction's connection. Under a concurrent commit,
    # BEGIN CONCURRENT retries the entire operation; the retry then observes the durable winner above.
    conn.execute(
        "UPDATE approval_requests SET updated_at = ?, state = ?, decided_by = ?, rule_id = ?, "
        "summary = ?, content_hash = ? WHERE id = ? AND state = ?",
        (updated, target_state, decided_by, rule_id, resolved_summary, content_hash, approval_id, current_state),
    )
    landed = conn.execute(
        "SELECT state, content_hash FROM approval_requests WHERE id = ?", (approval_id,)
    ).fetchone()
    if landed is None or landed[0] != target_state or landed[1] != content_hash:
        durable_state = landed[0] if landed else "missing"
        raise KaizenDenied(
            "DENIED_APPROVAL_ALREADY_DECIDED",
            {"id": approval_id, "current_state": durable_state, "attempted_state": target_state,
             "required_action": "an approval is decided once; inspect it with C5"},
            exit_code=2,
        )
    return {"id": approval_id, "session_id": row[0], "correlation_id": row[1],
            "request_type": row[2], "state": target_state, "summary": resolved_summary,
            "decided_by": decided_by, "rule_id": rule_id, "content_hash": content_hash}


def _validated_title(value: Any) -> str | None:
    """Validate an optional, already-canonical and redaction-safe C1 title."""

    if value is None:
        return None
    canonical = canonical_title(value)
    if canonical is None or canonical != value:
        raise KaizenDenied(
            "DENIED_TITLE_INVALID",
            {"required_action": "title must be the nonempty canonical first-prompt title (80 code points maximum)"},
            exit_code=2,
        )
    try:
        assert_redacted({"title": value})
    except KaizenDenied as denied:
        raise KaizenDenied(
            "DENIED_TITLE_INVALID",
            {"required_action": "omit unsafe title content and let the daemon derive a safe title"},
            exit_code=2,
        ) from denied
    return value


# --- v8 M10b node-aware fence (off-mode INERT; §B.3) --------------------------------------------
#
# When distribution is active (dist_mode() != "off") the session envelope is node-owned: C1 stamps
# owning_node = this node + node_epoch = 0 (epoch-at-creation default; the coordinator epoch wiring
# refines this at M14), and the C2/C3/C4 mutations pass a write_tx session_fence so a FOREIGN node's
# mutation of a node-owned session is refused DENIED_STALE_FENCE. dist_mode()=="off" (the default) skips
# ALL of this: no stamping, no fence, byte-identical to pre-M10b. fleet.identity is imported LAZILY so an
# off-mode process never even loads the fleet package.

_FENCE_CREATION_EPOCH = 0


def _dist_active() -> bool:
    return dist_mode() != "off"


def _my_node_id() -> str:
    """This process's fleet node id (lazy import: only reached when distribution is active, so an
    off-mode process never loads fleet.identity)."""
    from .fleet import identity as fleet_identity

    return fleet_identity.node_id()


def _session_fence(session_id: str) -> tuple[str, str | None, int | None] | None:
    """The write_tx ``session_fence`` for a mutation targeting ``session_id`` when distribution is
    active, else None (off-mode ⇒ no fence). The honest M10b v1 shape asserts the owning-node axis only
    (``expected_node_epoch=None``): the fence proves THIS process owns the session; a real cross-node
    epoch is passed at M14's attach/steer handoff. A legacy NULL-owner row is exempted inside write_tx."""
    if not _dist_active():
        return None
    return (session_id, _my_node_id(), None)


# --- C1 session create/resume -------------------------------------------------------------------

def session_start(args: Any) -> dict[str, Any]:
    """C1: create a controlled agent session, or resume one when --id names an existing session.

    controller/mode parse through orchestration.modes (single source); auth_mode validates against the
    reserved value space. Resume (--id present) is a no-op read that returns the existing envelope, so
    the supervisor can rebind to a session across restarts without minting a duplicate."""
    payload = _payload(args)
    resume_id = getattr(args, "id", None)
    if resume_id:
        row = fetch_one(
            "SELECT id, task_id, controller, mode, engine, auth_mode, state, summary, "
            "requested_model, requested_reasoning_effort, permission_mode, profile_hash, title "
            "FROM agent_sessions WHERE id = ?",
            (resume_id,),
        )
        if row is None:
            raise KaizenDenied(
                "DENIED_SESSION_NOT_FOUND",
                {"session_id": resume_id, "required_action": "omit --id to create a new session, or pass a real session id"},
                exit_code=1,
            )
        return {
            "status": "OK",
            "id": row[0],
            "resumed": True,
            "session": {
                "id": row[0], "task_id": row[1], "controller": row[2], "mode": row[3],
                "engine": row[4], "auth_mode": row[5], "state": row[6], "summary": row[7],
                "requested_model": row[8], "requested_reasoning_effort": row[9],
                "permission_mode": row[10], "profile_hash": row[11], "title": row[12],
            },
        }

    controller = parse_controller(getattr(args, "controller", None) or payload.get("controller"))
    mode = parse_session_mode(getattr(args, "mode", None) or payload.get("mode"))
    auth_raw = (getattr(args, "auth_mode", None) or payload.get("auth_mode") or "none").strip().lower()
    if auth_raw not in AUTH_MODES:
        raise KaizenDenied(
            "DENIED_AUTH_MODE_INVALID",
            {"auth_mode": auth_raw, "allowed": list(AUTH_MODES), "required_action": "use none|subscription|api-key"},
            exit_code=2,
        )
    summary = _text(args, "summary", payload.get("summary", ""))
    title_input = getattr(args, "title", None)
    title = _validated_title(title_input if title_input is not None else payload.get("title"))
    task_id = getattr(args, "task_id", None) or payload.get("task_id")
    engine = getattr(args, "engine", None) or payload.get("engine")
    state = payload.get("state", "open")

    # v8 H2.1 conversation-profile fields (optional; validated only when present). permission_mode is the
    # UI plan|ask|agent|full ceiling; requested_* are the owner-selected model/effort; profile_hash is the
    # immutable snapshot hash. All NULL for a legacy/pre-H2 session (which stays readable).
    permission_mode = (getattr(args, "permission_mode", None) or payload.get("permission_mode"))
    if permission_mode is not None:
        permission_mode = str(permission_mode).strip().lower()
        if permission_mode not in PERMISSION_MODES:
            raise KaizenDenied(
                "DENIED_PERMISSION_MODE_INVALID",
                {"permission_mode": permission_mode, "allowed": list(PERMISSION_MODES),
                 "required_action": "use plan|ask|agent|full"},
                exit_code=2,
            )
    requested_model = getattr(args, "requested_model", None) or payload.get("requested_model")
    requested_reasoning_effort = (
        getattr(args, "requested_reasoning_effort", None) or payload.get("requested_reasoning_effort")
    )
    profile_hash = getattr(args, "profile_hash", None) or payload.get("profile_hash")

    # v8 M10b: when distribution is active, a session is node-owned. Default owning_node = this node and
    # node_epoch = 0 (epoch-at-creation; refined by the coordinator epoch wiring at M14) unless the
    # payload already carries them. dist_mode()=="off" ⇒ both stay None ⇒ byte-identical to pre-M10b.
    owning_node = payload.get("owning_node")
    node_epoch = payload.get("node_epoch")
    if _dist_active():
        if owning_node is None:
            owning_node = _my_node_id()
        if node_epoch is None:
            node_epoch = _FENCE_CREATION_EPOCH

    clean = {
        k: v
        for k, v in {
            "task_id": task_id,
            "controller": controller,
            "mode": mode,
            "engine": engine,
            "auth_mode": auth_raw,
            "owning_node": owning_node,
            "node_epoch": node_epoch,
            "vendor_session_root_id": payload.get("vendor_session_root_id"),
            "vendor_thread_id": payload.get("vendor_thread_id"),
            "cwd": payload.get("cwd"),
            "git_branch": payload.get("git_branch"),
            "state": state,
            "requested_model": requested_model,
            "requested_reasoning_effort": requested_reasoning_effort,
            "permission_mode": permission_mode,
            "profile_hash": profile_hash,
            "title": title,
            "policy_snapshot": payload.get("policy_snapshot"),
            "summary": summary,
        }.items()
        if v not in (None, "")
    }
    validate_record("agent_session", clean)
    assert_redacted(
        {
            "summary": summary,
            "cwd": payload.get("cwd", "") or "",
            "git_branch": payload.get("git_branch", "") or "",
            "vendor_session_root_id": payload.get("vendor_session_root_id", "") or "",
            "vendor_thread_id": payload.get("vendor_thread_id", "") or "",
        }
    )
    record_id = new_id("as")
    created = now()
    content_hash = utc_text_hash({"id": record_id, **clean})
    is_test = 1 if getattr(args, "test", False) else 0

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO agent_sessions "
            "(id, created_at, task_id, controller, mode, engine, auth_mode, owning_node, node_epoch, "
            "vendor_session_root_id, vendor_thread_id, cwd, git_branch, state, "
            "requested_model, requested_reasoning_effort, permission_mode, profile_hash, policy_snapshot, "
            "title, summary, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id, created, task_id, controller, mode, engine, auth_raw,
                owning_node, node_epoch,
                payload.get("vendor_session_root_id"), payload.get("vendor_thread_id"),
                payload.get("cwd"), payload.get("git_branch"), state,
                requested_model, requested_reasoning_effort, permission_mode, profile_hash,
                payload.get("policy_snapshot"),
                title, summary, content_hash, is_test,
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "resumed": False, "content_hash": content_hash}


def set_policy_snapshot(session_id: str, snapshot_json: str) -> bool:
    """Trusted daemon bookkeeping (legacy healing): persist an immutable policy snapshot onto a C1
    that predates snapshot storage. First-writer-wins -- an existing snapshot is never overwritten."""
    if not session_id or not snapshot_json:
        return False

    def op(conn: Any, _attempt: int) -> bool:
        conn.execute(
            "UPDATE agent_sessions SET policy_snapshot = ? "
            "WHERE id = ? AND (policy_snapshot IS NULL OR policy_snapshot = '')",
            (snapshot_json, session_id),
        )
        row = conn.execute("SELECT policy_snapshot FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
        return bool(row and row[0] == snapshot_json)

    return bool(write_tx(op))


def session_reopen(session_id: str) -> bool:
    """Trusted daemon bookkeeping (continuation): a closed/failed C1 whose conversation continues on a
    new linked leg returns to ``open`` -- 'ended' is not a terminal fate for a conversation."""
    if not session_id:
        return False

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "UPDATE agent_sessions SET state = 'open' WHERE id = ? AND state != 'open'",
            (session_id,),
        )

    write_tx(op)
    row = fetch_one("SELECT state FROM agent_sessions WHERE id = ?", (session_id,))
    return bool(row and row[0] == "open")


def set_vendor_session_root(session_id: str, vendor_session_root_id: str) -> bool:
    """Trusted daemon bookkeeping: persist the driven conversation's vendor resume key on its C1 row,
    first-writer-wins (an already-set DIFFERENT root is never overwritten -- the caller's adapter
    init-mismatch check is the enforcement seam; this is durable recording only). Returns True when
    the row now carries this root id."""
    if not session_id or not vendor_session_root_id:
        return False

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "UPDATE agent_sessions SET vendor_session_root_id = ? "
            "WHERE id = ? AND (vendor_session_root_id IS NULL OR vendor_session_root_id = '')",
            (vendor_session_root_id, session_id),
        )

    write_tx(op)
    row = fetch_one("SELECT vendor_session_root_id FROM agent_sessions WHERE id = ?", (session_id,))
    return bool(row and row[0] == vendor_session_root_id)


# --- C2 instruction add -------------------------------------------------------------------------

def instruction_add(args: Any) -> dict[str, Any]:
    """Append one gapless session instruction under the M10b owning-node fence.

    A foreign active-mode writer is denied ``DENIED_STALE_FENCE``; distribution-off mode supplies no
    fence and retains the legacy behavior. The ordinal is allocated on the same write connection.
    """
    session_id = _require(args, "session_id", "DENIED_SESSION_ID_REQUIRED", "resubmit with --session-id")
    payload = _payload(args)
    instruction = _text(args, "body", payload.get("instruction", ""))
    if not instruction:
        raise KaizenDenied(
            "DENIED_INSTRUCTION_REQUIRED",
            {"required_action": "resubmit with --body (the instruction text)"},
            exit_code=2,
        )
    summary = _text(args, "summary", payload.get("summary", "")) or instruction[:120]
    clean = {"session_id": session_id, "instruction": instruction, "summary": summary}
    validate_record("user_instruction", clean)
    assert_redacted({"instruction": instruction, "summary": summary})
    record_id = new_id("ui")
    created = now()
    content_hash = utc_text_hash({"id": record_id, **clean})
    is_test = 1 if getattr(args, "test", False) else 0

    def op(conn: Any, _attempt: int) -> dict[str, Any]:
        if _fetch_session(conn, session_id) is None:
            raise KaizenDenied(
                "DENIED_SESSION_NOT_FOUND",
                {"session_id": session_id, "required_action": "open the session with C1 first"},
                exit_code=1,
            )
        # Single-writer libSQL: MAX+1 is a gapless per-session ordinal (same idiom as agent_events).
        seq = int(conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM user_instructions WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]) + 1
        conn.execute(
            "INSERT INTO user_instructions "
            "(id, created_at, session_id, seq, instruction, summary, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (record_id, created, session_id, seq, instruction, summary, content_hash, is_test),
        )
        return {"seq": seq}

    # M10b node fence: a foreign node mutating a node-owned session is refused DENIED_STALE_FENCE
    # (off-mode ⇒ session_fence None ⇒ no fence).
    result = write_tx(op, session_fence=_session_fence(session_id))
    return {"status": "OK", "id": record_id, "content_hash": content_hash, **result}


# --- C3 goal create/update ----------------------------------------------------------------------

def goal_upsert(args: Any) -> dict[str, Any]:
    """C3: create a goal, or update one in place when --id names an existing goal. The Kaizen goal is
    the canonical cross-vendor goal (engines with no native goal store project here)."""
    payload = _payload(args)
    goal_id = getattr(args, "id", None)
    summary = _text(args, "summary", payload.get("summary", ""))
    body = _text(args, "body", payload.get("body", ""))
    title = _text(args, "title", payload.get("title", ""))
    state = getattr(args, "status", None) or payload.get("state") or "open"

    if goal_id:
        row = fetch_one(
            "SELECT session_id, state, title, summary, body FROM goals WHERE id = ?", (goal_id,)
        )
        if row is None:
            raise KaizenDenied("DENIED_GOAL_NOT_FOUND", {"id": goal_id, "required_action": "check the id with C5"}, exit_code=1)
        session_id = row[0]
        title = title or row[2]
        summary = summary or row[3]
        body = body or row[4]
        clean = {"session_id": session_id, "state": state, "title": title, "summary": summary, "body": body}
        validate_record("goal", clean)
        assert_redacted({"title": title, "summary": summary, "body": body})
        updated = now()
        content_hash = utc_text_hash({"id": goal_id, **clean})

        def op_update(conn: Any, _attempt: int) -> None:
            existing = conn.execute("SELECT session_id FROM goals WHERE id = ?", (goal_id,)).fetchone()
            if existing is None or existing[0] != session_id:
                raise KaizenDenied(
                    "DENIED_RECORD_NOT_FOUND", {"id": goal_id, "table": "goals"}, exit_code=1,
                )
            conn.execute(
                "UPDATE goals SET updated_at = ?, state = ?, title = ?, summary = ?, body = ?, content_hash = ? WHERE id = ?",
                (updated, state, title, summary, body, content_hash, goal_id),
            )

        write_tx(op_update, session_fence=_session_fence(session_id))
        return {"status": "OK", "id": goal_id, "updated": True, "state": state, "content_hash": content_hash}

    session_id = _require(args, "session_id", "DENIED_SESSION_ID_REQUIRED", "resubmit with --session-id (or --id to update)")
    clean = {"session_id": session_id, "state": state, "title": title, "summary": summary, "body": body}
    validate_record("goal", clean)
    assert_redacted({"title": title, "summary": summary, "body": body})
    record_id = new_id("goal")
    created = now()
    content_hash = utc_text_hash({"id": record_id, **clean})
    is_test = 1 if getattr(args, "test", False) else 0

    def op_create(conn: Any, _attempt: int) -> None:
        if _fetch_session(conn, session_id) is None:
            raise KaizenDenied(
                "DENIED_SESSION_NOT_FOUND",
                {"session_id": session_id, "required_action": "open the session with C1 first"},
                exit_code=1,
            )
        conn.execute(
            "INSERT INTO goals "
            "(id, created_at, updated_at, session_id, state, title, summary, body, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record_id, created, created, session_id, state, title, summary, body, content_hash, is_test),
        )

    write_tx(op_create, session_fence=_session_fence(session_id))
    return {"status": "OK", "id": record_id, "updated": False, "state": state, "content_hash": content_hash}


# --- C4 approval create/update ------------------------------------------------------------------

def approval_upsert(args: Any) -> dict[str, Any]:
    """C4: create an approval request, or update one in place when --id names an existing request.

    The C4 state machine: an approval already in a decided terminal state ({approved, denied, canceled,
    cleared_by_engine}) may not be re-decided (DENIED_APPROVAL_ALREADY_DECIDED). decided_by/rule_id are
    recorded on the transition to a decided state."""
    payload = _payload(args)
    approval_id = getattr(args, "id", None)
    state = (getattr(args, "status", None) or payload.get("state") or "open").strip()
    decided_by = payload.get("decided_by")
    rule_id = payload.get("rule_id")
    summary = _text(args, "summary", payload.get("summary", ""))

    if state not in _APPROVAL_STATES:
        raise KaizenDenied(
            "DENIED_APPROVAL_STATE_INVALID",
            {"state": state, "allowed": list(_APPROVAL_STATES), "required_action": "use an allowed approval state"},
            exit_code=2,
        )

    if approval_id:
        # The preliminary read is only for selecting the session fence; the authoritative state check
        # and guarded UPDATE both run on the transaction connection in transition_approval_conn.
        row = fetch_one("SELECT session_id FROM approval_requests WHERE id = ?", (approval_id,))
        if row is None:
            raise KaizenDenied(
                "DENIED_APPROVAL_NOT_FOUND",
                {"id": approval_id, "required_action": "check the id with C5"},
                exit_code=1,
            )

        def op_update(conn: Any, _attempt: int) -> dict[str, Any]:
            return transition_approval_conn(
                conn, approval_id, state, decided_by=decided_by, rule_id=rule_id,
                summary=summary or None,
            )

        result = write_tx(op_update, session_fence=_session_fence(row[0]))
        return {"status": "OK", "id": approval_id, "updated": True, "state": result["state"],
                "content_hash": result["content_hash"]}

    session_id = _require(args, "session_id", "DENIED_SESSION_ID_REQUIRED", "resubmit with --session-id (or --id to update)")
    request_type = (payload.get("request_type") or "tool_approval").strip()
    if request_type not in _APPROVAL_TYPES:
        raise KaizenDenied(
            "DENIED_APPROVAL_TYPE_INVALID",
            {"request_type": request_type, "allowed": list(_APPROVAL_TYPES),
             "required_action": "use tool_approval|clarifying_question|plan_exit|requestUserInput"},
            exit_code=2,
        )
    correlation_id = payload.get("correlation_id")
    clean = {
        k: v
        for k, v in {
            "session_id": session_id, "correlation_id": correlation_id, "request_type": request_type,
            "state": state, "decided_by": decided_by, "rule_id": rule_id, "summary": summary,
        }.items()
        if v not in (None, "")
    }
    validate_record("approval_request", clean)
    assert_redacted({"summary": summary, "correlation_id": correlation_id or "", "rule_id": rule_id or ""})
    record_id = new_id("apr")
    created = now()
    is_test = 1 if getattr(args, "test", False) else 0

    def op_create(conn: Any, _attempt: int) -> dict[str, Any]:
        # Transaction-local replay check: ordinary duplicate asks reuse the durable open request; a
        # concurrent write conflict retries the operation and then observes the committed winner.
        if state == "open" and correlation_id:
            existing = conn.execute(
                "SELECT id, content_hash FROM approval_requests "
                "WHERE session_id = ? AND correlation_id = ? AND state = 'open' AND is_test = ?",
                (session_id, correlation_id, is_test),
            ).fetchone()
            if existing is not None:
                return {"id": existing[0], "content_hash": existing[1], "deduplicated": True}
        inserted = insert_approval_conn(
            conn, approval_id=record_id, session_id=session_id, correlation_id=correlation_id,
            request_type=request_type, state=state, decided_by=decided_by, rule_id=rule_id,
            summary=summary, created_at=created, is_test=bool(is_test),
        )
        return {"id": record_id, "content_hash": inserted["content_hash"], "deduplicated": False}

    result = write_tx(op_create, session_fence=_session_fence(session_id))
    return {"status": "OK", "id": result["id"], "updated": False, "state": state,
            "content_hash": result["content_hash"], "deduplicated": result["deduplicated"]}


# --- v8 M14 cross-machine attach + programmatic approval-decide (concrete-epoch fence) ----------

def attach_session(
    session_id: str,
    *,
    new_owning_node: str,
    expected_owning_node: str,
    expected_node_epoch: int,
) -> dict[str, Any]:
    """M14 cross-machine attach (§B.5 / plan §5.3 M14 exit): the CONCRETE-epoch fence wiring.

    An operator on node B takes a session's ownership + bumps the epoch (serialized) to become the
    epoch-current controller. This rides ``write_tx(session_fence=(session_id, expected_owning_node,
    expected_node_epoch))`` -- a CONCRETE ``expected_node_epoch`` (unlike the C2/C3/C4 owning-node-only
    fence) -- wrapping ``UPDATE agent_sessions SET owning_node=new_owning_node,
    node_epoch=expected_node_epoch+1``. A STALE ``expected_node_epoch`` ⇒ ``DENIED_STALE_FENCE`` from the
    fence itself (the exit criterion: a stale attach is refused). A missing session ⇒ structured
    ``DENIED_SESSION_NOT_FOUND``. An UNFENCED (owning_node NULL) session attaches cleanly (the fence is
    a no-op for a legacy/off-mode row; the UPDATE stamps the new owner + epoch anyway).

    Returns ``{session_id, owning_node, node_epoch}`` (the NEW owner + bumped epoch)."""
    new_epoch = int(expected_node_epoch) + 1

    def op(conn: Any, _attempt: int) -> dict[str, Any]:
        row = conn.execute(
            "SELECT owning_node, node_epoch FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise KaizenDenied(
                "DENIED_SESSION_NOT_FOUND",
                {"session_id": session_id, "required_action": "attach targets an existing session; check the id with C5"},
                exit_code=1,
            )
        conn.execute(
            "UPDATE agent_sessions SET owning_node = ?, node_epoch = ? WHERE id = ?",
            (new_owning_node, new_epoch, session_id),
        )
        return {"owning_node": new_owning_node, "node_epoch": new_epoch}

    # The CONCRETE-epoch fence: a stale expected_node_epoch refuses DENIED_STALE_FENCE inside write_tx
    # (on the write conn, no TOCTOU) BEFORE the UPDATE. The fence exempts an UNFENCED (owning_node NULL)
    # row, so attaching a legacy/off-mode session still runs the UPDATE.
    result = write_tx(op, session_fence=(session_id, expected_owning_node, int(expected_node_epoch)))
    return {"status": "OK", "session_id": session_id, **result}


def decide_approval(approval_id: str, decision: str, *, decided_by: str = "human", session_fence: Any = None) -> dict[str, Any]:
    """Programmatic decide of an OPEN C4 approval (the supervisor's loopback ``approve`` path).

    A thin, Namespace-free sibling of :func:`approval_upsert`'s update branch for in-process callers
    (the supervisor decides an approval over loopback, not via the CLI). ``decision`` is ``approve`` or
    ``deny`` (mapped to the C4 states approved|denied). The C4 state machine is preserved: an approval
    already in a DECIDED terminal state refuses ``DENIED_APPROVAL_ALREADY_DECIDED``; a missing row
    refuses ``DENIED_APPROVAL_NOT_FOUND``.

    ``session_fence`` (default None) is passed straight to ``write_tx`` so the caller can bind the decide
    to the epoch-current controller (the supervisor passes the approval's session fenced to THIS node at
    the current epoch, so a stale-epoch daemon's decide refuses DENIED_STALE_FENCE)."""
    target_state = {"approve": "approved", "deny": "denied"}.get(str(decision).strip().lower())
    if target_state is None:
        raise KaizenDenied(
            "DENIED_APPROVAL_DECISION_INVALID",
            {"decision": decision, "allowed": ["approve", "deny"], "required_action": "decision must be approve|deny"},
            exit_code=2,
        )
    def op(conn: Any, _attempt: int) -> dict[str, Any]:
        return transition_approval_conn(
            conn, approval_id, target_state, decided_by=decided_by, open_only=True,
        )

    result = write_tx(op, session_fence=session_fence)
    return {"status": "OK", "id": approval_id, "session_id": result["session_id"],
            "state": result["state"], "decided_by": decided_by, "content_hash": result["content_hash"]}


# --- C5 session timeline read -------------------------------------------------------------------

def session_timeline(args: Any) -> dict[str, Any]:
    """C5: read a session with its instructions, goals, and approvals joined in one read
    (ordered for a coherent timeline). Read-only."""
    session_id = _require(args, "session_id", "DENIED_SESSION_ID_REQUIRED", "resubmit with --session-id")
    session = fetch_one(
        "SELECT id, created_at, task_id, controller, mode, engine, auth_mode, owning_node, node_epoch, "
        "vendor_session_root_id, vendor_thread_id, cwd, git_branch, state, summary, "
        "requested_model, requested_reasoning_effort, permission_mode, profile_hash, title "
        "FROM agent_sessions WHERE id = ?",
        (session_id,),
    )
    if session is None:
        raise KaizenDenied(
            "DENIED_SESSION_NOT_FOUND",
            {"session_id": session_id, "required_action": "check the id with R0 or C1"},
            exit_code=1,
        )
    envelope = {
        "id": session[0], "created_at": session[1], "task_id": session[2], "controller": session[3],
        "mode": session[4], "engine": session[5], "auth_mode": session[6], "owning_node": session[7],
        "node_epoch": session[8], "vendor_session_root_id": session[9], "vendor_thread_id": session[10],
        "cwd": session[11], "git_branch": session[12], "state": session[13], "summary": session[14],
        # v8 H2.1 conversation-profile fields (NULL on legacy rows -> rendered as legacy/unknown by the UI).
        "requested_model": session[15], "requested_reasoning_effort": session[16],
        "permission_mode": session[17], "profile_hash": session[18], "title": session[19],
    }
    instructions = [
        {"id": r[0], "seq": r[1], "created_at": r[2], "instruction": r[3], "summary": r[4]}
        for r in fetch_all(
            "SELECT id, seq, created_at, instruction, summary FROM user_instructions "
            "WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
    ]
    goals = [
        {"id": r[0], "created_at": r[1], "updated_at": r[2], "state": r[3], "title": r[4], "summary": r[5]}
        for r in fetch_all(
            "SELECT id, created_at, updated_at, state, title, summary FROM goals "
            "WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
    ]
    approvals = [
        {"id": r[0], "created_at": r[1], "updated_at": r[2], "correlation_id": r[3],
         "request_type": r[4], "state": r[5], "decided_by": r[6], "rule_id": r[7], "summary": r[8]}
        for r in fetch_all(
            "SELECT id, created_at, updated_at, correlation_id, request_type, state, decided_by, rule_id, summary "
            "FROM approval_requests WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
    ]
    return {
        "status": "OK",
        "session": envelope,
        "instructions": instructions,
        "goals": goals,
        "approvals": approvals,
        "counts": {
            "instructions": len(instructions),
            "goals": len(goals),
            "approvals": len(approvals),
        },
    }


# --- C6 mode-profiles (list/show/set) -----------------------------------------------------------
#
# v8 H2.1. mode_profiles stores ONLY owner-specific designated write roots + snapshot metadata; mode
# SEMANTICS live in code (the policy lane). The v1 payload is {permission_mode, designated_write_roots}.
# Default Plan roots (AI/work, AI/generation) seed on first read when absent (idempotent). A designated
# root must already EXIST under the repo root, resolve without a traversal escape, and survive a
# junction/symlink check (its RESOLVED path must stay under the repo root); AI/db can NEVER be designated.
# `set` is an idempotent name-keyed upsert. An empty designated_write_roots list = read-only Plan.

_DEFAULT_PLAN_ROOTS = ("AI/work", "AI/generation")


def _validate_designated_roots(roots: Any) -> list[str]:
    """Validate + normalize a designated_write_roots list (the C6 root rules, applied at set time).

    Each root must be a repo-relative string that resolves (dereferencing junctions/symlinks via
    .resolve()) to an EXISTING directory strictly under REPO_ROOT and not under AI/db. Returns the
    normalized (repo-relative, forward-slash) list; raises KaizenDenied on the first violation. An empty
    list is valid (read-only Plan)."""
    if not isinstance(roots, list):
        raise KaizenDenied(
            "DENIED_MODE_PROFILE_ROOTS",
            {"required_action": "designated_write_roots must be a JSON list of repo-relative paths"},
            exit_code=2,
        )
    db_resolved = DB_ROOT.resolve()
    repo_resolved = REPO_ROOT.resolve()
    normalized: list[str] = []
    for raw in roots:
        if not isinstance(raw, str) or not raw.strip():
            raise KaizenDenied(
                "DENIED_MODE_PROFILE_ROOTS",
                {"root": raw, "required_action": "each designated root must be a non-empty repo-relative path"},
                exit_code=2,
            )
        candidate = (REPO_ROOT / raw).resolve()
        # path_in_repo (assert_under with .resolve()) rejects ../ traversal AND a junction/symlink whose
        # target leaves the repo -- the resolved path must stay under REPO_ROOT.
        if not path_in_repo(candidate) or candidate == repo_resolved:
            raise KaizenDenied(
                "DENIED_MODE_PROFILE_ROOT_ESCAPE",
                {"root": raw, "resolved": str(candidate),
                 "required_action": "a designated root must resolve to a directory strictly inside the repo (no traversal/junction escape)"},
                exit_code=2,
            )
        if candidate == db_resolved or db_resolved in candidate.parents:
            raise KaizenDenied(
                "DENIED_MODE_PROFILE_ROOT_PROTECTED",
                {"root": raw, "required_action": "AI/db can never be a designated write root"},
                exit_code=2,
            )
        if not candidate.is_dir():
            raise KaizenDenied(
                "DENIED_MODE_PROFILE_ROOT_MISSING",
                {"root": raw, "resolved": str(candidate),
                 "required_action": "a designated root must already exist under the repo root"},
                exit_code=2,
            )
        rel = candidate.relative_to(repo_resolved).as_posix()
        if rel not in normalized:
            normalized.append(rel)
    return normalized


def _seed_default_plan(is_test: bool = False) -> dict[str, Any]:
    """Seed the default Plan profile (permission_mode=plan, designated_write_roots=AI/work+AI/generation
    that exist) when no 'plan' row exists yet. Idempotent: a no-op once the row is present. Returns the
    stored profile dict."""
    existing = _fetch_mode_profile_row("plan")
    if existing is not None:
        return _profile_row_to_dict(existing)
    seed_roots = [r for r in _DEFAULT_PLAN_ROOTS if (REPO_ROOT / r).is_dir()]
    profile = {"permission_mode": "plan", "designated_write_roots": seed_roots}
    return _upsert_mode_profile("plan", profile, is_test=is_test, summary="Default read/plan write roots.")


def _fetch_mode_profile_row(name: str) -> tuple[Any, ...] | None:
    """Name-keyed single-row fetch; row tuple or None."""
    return fetch_one(
        "SELECT id, created_at, updated_at, name, profile_json, summary FROM mode_profiles WHERE name = ?",
        (name,),
    )


def _profile_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    """Maps a mode_profiles row to dict; JSON-decodes profile_json (malformed -> {})."""
    try:
        profile = json.loads(row[4]) if row[4] else {}
    except json.JSONDecodeError:
        profile = {}
    return {
        "id": row[0], "created_at": row[1], "updated_at": row[2], "name": row[3],
        "profile": profile, "summary": row[5],
    }


def _upsert_mode_profile(name: str, profile: dict[str, Any], *, is_test: bool, summary: str) -> dict[str, Any]:
    """Idempotent name-keyed upsert of a mode_profiles row. profile is stored as profile_json."""
    profile_json = json.dumps(profile, sort_keys=True, ensure_ascii=True)
    clean = {"name": name, "profile_json": profile_json, "summary": summary}
    validate_record("mode_profile", clean)
    assert_redacted({"profile_json": profile_json, "summary": summary})
    updated = now()
    content_hash = utc_text_hash(clean)
    test_flag = 1 if is_test else 0

    def op(conn: Any, _attempt: int) -> dict[str, Any]:
        row = conn.execute("SELECT id FROM mode_profiles WHERE name = ?", (name,)).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE mode_profiles SET updated_at = ?, profile_json = ?, summary = ?, content_hash = ? WHERE id = ?",
                (updated, profile_json, summary, content_hash, row[0]),
            )
            return {"id": row[0], "created": False}
        record_id = new_id("mp")
        conn.execute(
            "INSERT INTO mode_profiles "
            "(id, created_at, updated_at, name, profile_json, summary, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (record_id, updated, updated, name, profile_json, summary, content_hash, test_flag),
        )
        return {"id": record_id, "created": True}

    result = write_tx(op)
    return {"id": result["id"], "created": result["created"], "name": name,
            "profile": profile, "content_hash": content_hash}


def get_mode_profile(name: str) -> dict[str, Any]:
    """Read API for other components (the policy lane consumes this NEXT wave).

    Returns ``{permission_mode, designated_write_roots (normalized repo-relative list), updated_at,
    name, summary}`` for the named profile. The 'plan' profile seeds its defaults (AI/work, AI/generation
    that exist) on first read if absent (idempotent). A non-'plan' name with no row returns
    ``permission_mode=None`` + an empty roots list (the caller decides the fallback). designated_write_roots
    is exactly what was stored at ``set`` time (already root-validated + normalized)."""
    if name == "plan":
        _seed_default_plan()
    row = _fetch_mode_profile_row(name)
    if row is None:
        return {"name": name, "permission_mode": None, "designated_write_roots": [], "updated_at": None}
    data = _profile_row_to_dict(row)
    profile = data["profile"]
    roots = profile.get("designated_write_roots")
    return {
        "name": name,
        "permission_mode": profile.get("permission_mode"),
        "designated_write_roots": roots if isinstance(roots, list) else [],
        "updated_at": data["updated_at"],
        "summary": data["summary"],
    }


def mode_profile(args: Any) -> dict[str, Any]:
    """C6: manage owner mode profiles. --action list | show --name <n> | set --name <n> --payload-json-file.

    v1 payload: {"permission_mode": "plan", "designated_write_roots": ["AI/work", "AI/generation"]}. The
    'plan' profile's defaults seed on first list/show if absent (idempotent). set validates the roots (must
    exist under the repo, no traversal/junction escape, never AI/db) then upserts by name."""
    action = (getattr(args, "action", None) or "list").strip().lower()
    is_test = bool(getattr(args, "test", False))

    if action == "list":
        _seed_default_plan(is_test=is_test)
        rows = fetch_all(
            "SELECT id, created_at, updated_at, name, profile_json, summary FROM mode_profiles ORDER BY name"
        )
        profiles = []
        for row in rows:
            data = _profile_row_to_dict(row)
            roots = data["profile"].get("designated_write_roots", [])
            roots = roots if isinstance(roots, list) else []
            profiles.append({
                "name": data["name"], "permission_mode": data["profile"].get("permission_mode"),
                "designated_write_roots": roots,
                "updated_at": data["updated_at"], "summary": data["summary"],
            })
        return {"status": "OK", "count": len(profiles), "profiles": profiles}

    name = getattr(args, "name", None)
    if not name:
        raise KaizenDenied(
            "DENIED_MODE_PROFILE_NAME_REQUIRED",
            {"required_action": "resubmit with --name (the profile name, e.g. plan)"},
            exit_code=2,
        )

    if action == "show":
        if name == "plan":
            _seed_default_plan(is_test=is_test)
        row = _fetch_mode_profile_row(name)
        if row is None:
            raise KaizenDenied(
                "DENIED_MODE_PROFILE_NOT_FOUND",
                {"name": name, "required_action": "seed it with C6 --action set, or list existing profiles"},
                exit_code=1,
            )
        data = _profile_row_to_dict(row)
        roots = data["profile"].get("designated_write_roots", [])
        roots = roots if isinstance(roots, list) else []
        return {"status": "OK", "profile": {
            "name": data["name"], "permission_mode": data["profile"].get("permission_mode"),
            "designated_write_roots": roots,
            "updated_at": data["updated_at"], "summary": data["summary"],
        }}

    if action == "set":
        payload = _payload(args)
        permission_mode = (payload.get("permission_mode") or "").strip().lower()
        if permission_mode not in PERMISSION_MODES:
            raise KaizenDenied(
                "DENIED_PERMISSION_MODE_INVALID",
                {"permission_mode": permission_mode, "allowed": list(PERMISSION_MODES),
                 "required_action": "payload permission_mode must be plan|ask|agent|full"},
                exit_code=2,
            )
        roots = _validate_designated_roots(payload.get("designated_write_roots", []))
        summary = _text(args, "summary", payload.get("summary", "")) or f"Mode profile {name}."
        profile = {"permission_mode": permission_mode, "designated_write_roots": roots}
        result = _upsert_mode_profile(name, profile, is_test=is_test, summary=summary)
        return {"status": "OK", **result}

    raise KaizenDenied(
        "DENIED_MODE_PROFILE_ACTION",
        {"action": action, "allowed": ["list", "show", "set"],
         "required_action": "use --action list|show|set"},
        exit_code=2,
    )
