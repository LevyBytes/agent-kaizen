"""Authoritative agent-orchestration ledger (Layer C, T5-T8).

An `agent_runs` row is one tracked agent execution ENVELOPE (runtime host/sandbox/approval
reproducibility fields, soft-linked to a task). `agent_events` is the append-only, AUTHORITATIVE
lifecycle stream for a run. :func:`reduce` PROJECTS run state from those events; it is pure,
importable, and time-free (a missing close holds a span open forever -- the sanctioned exit is an
explicit close event or a T8 failure-finalize, never a clock-driven transition).

Events are the source of truth. `agent_runs.state`/`failure_category` are a denormalized cache for
R0/T7 display ONLY -- the completion gates (Q2/W2) and the K1 child-leak invariant always recompute
from events on the write connection, never from the cached column.
"""

from __future__ import annotations

import json
from typing import Any

from .db import fetch_all, fetch_one, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import utc_text_hash
from .paths import read_text_file
from .redaction import assert_redacted
from .schemas import validate_record
from .schemas.registry import AGENT_EVENT_KIND_MARKERS


# Reducer semantics. A span = (event_kind, correlation_id). Set-of-markers fold (order-independent):
# OPEN iff an open marker was seen and no close marker was seen; a close-before-open is still closed.
_SPAN_KINDS = ("subagent", "approval", "turn", "tool_call")
_SPAN_CLOSE = {"close_ok", "close_fail", "close_canceled"}
_APPROVAL_CLOSE = {"resolved", "declined", "timed_out"}
_FAILURE_POINT_KINDS = {"transport", "auth", "rate_limit", "context"}
# T8 --conclusion -> finalization marker. Only `success` is gated; the failure conclusions
# force-terminate a run regardless of open work (the sole escape hatch for a leaked/hung child).
_FINALIZE_MARKERS = {
    "success": "close_ok",
    "failed": "close_fail",
    "canceled": "close_canceled",
    "timed_out": "close_fail",
}


def _span_open(kind: str, markers: set[str]) -> bool:
    closed = bool(markers & (_APPROVAL_CLOSE if kind == "approval" else _SPAN_CLOSE))
    return ("open" in markers) and not closed


def reduce(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure projection of one run's events -> run state. `events` is a list of dicts with keys
    sequence_no, created_at, id, event_kind, marker, correlation_id, code. Safe under out-of-order,
    duplicate, and close-before-open input."""
    ordered = sorted(events, key=lambda e: (e.get("sequence_no") or 0, e.get("created_at") or "", e.get("id") or ""))
    spans: dict[tuple[str, Any], set[str]] = {}
    finalization_marker: str | None = None
    failure_category: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    for event in ordered:
        kind = event.get("event_kind")
        marker = event.get("marker")
        created = event.get("created_at")
        if created:
            first_seen = first_seen or created
            last_seen = created
        if kind == "finalization":
            finalization_marker = marker
            continue
        if kind in _FAILURE_POINT_KINDS and marker == "point":
            failure_category = kind  # last failing category wins (ordered)
            continue
        if kind in _SPAN_KINDS:
            spans.setdefault((kind, event.get("correlation_id")), set()).add(marker)

    def _open_of(kind: str) -> list[Any]:
        return [corr for (k, corr), markers in spans.items() if k == kind and _span_open(k, markers)]

    open_children = _open_of("subagent")
    unresolved_approvals = _open_of("approval")
    terminal = finalization_marker is not None
    terminal_state = None
    if terminal:
        terminal_state = "success" if finalization_marker == "close_ok" else "failure"
    return {
        "open_children": open_children,
        "unresolved_approvals": unresolved_approvals,
        "open_turns": _open_of("turn"),
        "open_tool_calls": _open_of("tool_call"),
        "terminal": terminal,
        "terminal_state": terminal_state,
        # child_leak is the K1 invariant target: a SUCCESS finalization written while a child was
        # still open (only reachable via crash-path/out-of-order/pre-gate data -- T8 blocks it live).
        "child_leak": bool(terminal and terminal_state == "success" and open_children),
        "failure_category": failure_category,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "event_count": len(events),
    }


def run_blocks_completion(state: dict[str, Any]) -> bool:
    """True iff this run must block a parent's success-verification / terminal task-status write:
    a NON-terminal run with an open child or an unresolved approval. Terminal runs never block."""
    return (not state["terminal"]) and bool(state["open_children"] or state["unresolved_approvals"])


def _events_for_run(conn: Any, agent_run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, created_at, sequence_no, event_kind, marker, correlation_id, code "
        "FROM agent_events WHERE agent_run_id = ?",
        (agent_run_id,),
    ).fetchall()
    return [
        {
            "id": r[0], "created_at": r[1], "sequence_no": r[2], "event_kind": r[3],
            "marker": r[4], "correlation_id": r[5], "code": r[6],
        }
        for r in rows
    ]


def reduce_run_conn(conn: Any, agent_run_id: str) -> dict[str, Any]:
    """Reduce a run's state on an OPEN write connection -- used by the atomic Q2/W2 gates and T6/T8
    so the check and the dependent write share one transaction (no TOCTOU window)."""
    return reduce(_events_for_run(conn, agent_run_id))


def reduce_run(agent_run_id: str) -> dict[str, Any]:
    """Read-only reduction of a run's state (own connection) -- used by T7, K1, and R0."""
    rows = fetch_all(
        "SELECT id, created_at, sequence_no, event_kind, marker, correlation_id, code "
        "FROM agent_events WHERE agent_run_id = ?",
        (agent_run_id,),
    )
    return reduce([
        {"id": r[0], "created_at": r[1], "sequence_no": r[2], "event_kind": r[3],
         "marker": r[4], "correlation_id": r[5], "code": r[6]}
        for r in rows
    ])


def task_live_orchestration(conn: Any, task_id: str) -> dict[str, Any] | None:
    """Recompute, on the write connection, whether any agent_run linked to `task_id` still has live
    work. Returns None when nothing blocks (fail-open: no linked run, or all terminal/idle -> allow),
    else the aggregate the gate reports. Recomputed from events; the cached run.state is never trusted."""
    rows = conn.execute("SELECT id FROM agent_runs WHERE task_id = ?", (task_id,)).fetchall()
    blocking_runs: list[str] = []
    live_children = 0
    unresolved_approvals = 0
    for (run_id,) in rows:
        state = reduce_run_conn(conn, run_id)
        if run_blocks_completion(state):
            blocking_runs.append(run_id)
            live_children += len(state["open_children"])
            unresolved_approvals += len(state["unresolved_approvals"])
    if not blocking_runs:
        return None
    return {
        "blocking_agent_runs": blocking_runs,
        "live_children": live_children,
        "unresolved_approvals": unresolved_approvals,
    }


def _cache_label(state: dict[str, Any]) -> str:
    if state["terminal"]:
        return "completed" if state["terminal_state"] == "success" else "failed"
    if state["unresolved_approvals"]:
        return "waiting_approval"
    return "running"


def _refresh_cache(conn: Any, agent_run_id: str, state: dict[str, Any] | None = None) -> None:
    state = state or reduce_run_conn(conn, agent_run_id)
    conn.execute(
        "UPDATE agent_runs SET state = ?, failure_category = ? WHERE id = ?",
        (_cache_label(state), state["failure_category"], agent_run_id),
    )


# --- CLI-arg plumbing (local; agent_runs must not import task_records/trace_records to stay cycle-free)

def _payload(args: Any) -> dict[str, Any]:
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


def _text(args: Any, name: str, default: str = "") -> str:
    value = getattr(args, name, None)
    if value is None:
        file_value = getattr(args, f"{name}_file", None)
        if file_value:
            return read_text_file(file_value)
        return default
    return value


def _require(args: Any, name: str, code: str, action: str) -> str:
    value = getattr(args, name, None)
    if not value:
        raise KaizenDenied(code, {"required_action": action}, exit_code=2)
    return value


# --- T5-T8 handlers

def agent_run_start(args: Any) -> dict[str, Any]:
    """T5: open an authoritative agent-run envelope."""
    payload = _payload(args)
    if getattr(args, "task_id", None):
        payload.setdefault("task_id", args.task_id)
    if getattr(args, "agent_type", None):
        payload.setdefault("agent_type", args.agent_type)
    if getattr(args, "surface", None):
        payload.setdefault("surface", args.surface)
    payload["summary"] = _text(args, "summary", payload.get("summary", ""))
    payload["body"] = _text(args, "body", payload.get("body", ""))
    clean = {k: v for k, v in payload.items() if v not in (None, "")}
    validate_record("agent_run", clean)
    assert_redacted(
        {
            "summary": payload.get("summary", ""),
            "body": payload.get("body", ""),
            # path/version fields are untrusted envelope input: deny secrets and personal absolute
            # paths (C:\\Users\\..., /home/..., /Users/...). Callers/bridge pass repo-relative or
            # external:<name> forms for out-of-repo worktrees.
            "worktree_path": payload.get("worktree_path", "") or "",
            "cwd": payload.get("cwd", "") or "",
            "git_branch": payload.get("git_branch", "") or "",
            "git_commit": payload.get("git_commit", "") or "",
            "model": payload.get("model", "") or "",
            "extension_version": payload.get("extension_version", "") or "",
            "agent_version": payload.get("agent_version", "") or "",
        }
    )
    record_id = new_id("ar")
    created = now()
    content_hash = utc_text_hash({"id": record_id, **clean})
    is_test = 1 if getattr(args, "test", False) else 0

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO agent_runs "
            "(id, created_at, task_id, agent_type, surface, host, sandbox_mode, approval_mode, os, "
            "extension_version, agent_version, model, worktree_path, cwd, git_branch, git_commit, "
            "state, failure_category, summary, body, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id, created, payload.get("task_id"), payload["agent_type"], payload["surface"],
                payload.get("host"), payload.get("sandbox_mode"), payload.get("approval_mode"), payload.get("os"),
                payload.get("extension_version"), payload.get("agent_version"), payload.get("model"),
                payload.get("worktree_path"), payload.get("cwd"), payload.get("git_branch"), payload.get("git_commit"),
                "started", None, payload["summary"], payload["body"], content_hash, is_test,
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "content_hash": content_hash}


def agent_event_add(args: Any) -> dict[str, Any]:
    """T6: append one authoritative lifecycle event to a run (idempotent on source_event_id)."""
    agent_run_id = _require(args, "agent_run_id", "DENIED_AGENT_RUN_ID_REQUIRED", "resubmit with --agent-run-id")
    payload = _payload(args)
    payload["summary"] = _text(args, "summary", payload.get("summary", ""))
    payload["body"] = _text(args, "body", payload.get("body", ""))
    clean = {k: v for k, v in payload.items() if v not in (None, "")}
    validate_record("agent_event", clean)

    kind = payload["event_kind"]
    marker = payload["marker"]
    allowed = AGENT_EVENT_KIND_MARKERS.get(kind, [])
    if marker not in allowed:
        raise KaizenDenied(
            "DENIED_EVENT_KIND_MARKER",
            {"event_kind": kind, "marker": marker, "allowed": allowed,
             "required_action": "use a marker valid for this event_kind"},
            exit_code=2,
        )
    correlation_id = payload.get("correlation_id")
    if kind in _SPAN_KINDS and not correlation_id:
        raise KaizenDenied(
            "DENIED_CORRELATION_ID_REQUIRED",
            {"event_kind": kind,
             "required_action": "span events (subagent/approval/turn/tool_call) need correlation_id to pair open/close"},
            exit_code=2,
        )
    assert_redacted(
        {
            "summary": payload.get("summary", ""),
            "body": payload.get("body", ""),
            "code": payload.get("code", "") or "",
            "name": payload.get("name", "") or "",
            "status_message": payload.get("status_message", "") or "",
            "correlation_id": correlation_id or "",
            "source_event_id": payload.get("source_event_id", "") or "",
        }
    )

    source_event_id = payload.get("source_event_id")
    record_id = new_id("ae")
    created = now()
    content_hash = utc_text_hash({"id": record_id, "agent_run_id": agent_run_id, **clean})

    def op(conn: Any, _attempt: int) -> dict[str, Any]:
        if fetch_run(conn, agent_run_id) is None:
            raise KaizenDenied(
                "DENIED_AGENT_RUN_NOT_FOUND",
                {"agent_run_id": agent_run_id, "required_action": "open the run with T5 first"},
                exit_code=1,
            )
        if source_event_id is not None:
            existing = conn.execute(
                "SELECT id, sequence_no FROM agent_events WHERE agent_run_id = ? AND source_event_id = ?",
                (agent_run_id, source_event_id),
            ).fetchone()
            if existing:
                return {"id": existing[0], "sequence_no": existing[1], "deduplicated": True}
        # sequence_no assigned AFTER the dedup check so a rejected duplicate leaves no gap. libSQL is
        # single-writer, so MAX+1 is a gapless total order within the run.
        seq = int(conn.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) FROM agent_events WHERE agent_run_id = ?",
            (agent_run_id,),
        ).fetchone()[0]) + 1
        conn.execute(
            "INSERT OR IGNORE INTO agent_events "
            "(id, created_at, agent_run_id, sequence_no, source_event_id, correlation_id, event_kind, "
            "marker, code, name, status_message, summary, body, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id, created, agent_run_id, seq, source_event_id, correlation_id, kind, marker,
                payload.get("code"), payload.get("name"), payload.get("status_message"),
                payload["summary"], payload["body"], content_hash,
            ),
        )
        # A concurrent writer may have won the partial-unique race; re-read the effective row.
        if source_event_id is not None:
            landed = conn.execute(
                "SELECT id, sequence_no FROM agent_events WHERE agent_run_id = ? AND source_event_id = ?",
                (agent_run_id, source_event_id),
            ).fetchone()
            landed_id, landed_seq = landed[0], landed[1]
            deduplicated = landed_id != record_id
        else:
            landed_id, landed_seq, deduplicated = record_id, seq, False
        _refresh_cache(conn, agent_run_id)
        return {"id": landed_id, "sequence_no": landed_seq, "deduplicated": deduplicated}

    result = write_tx(op)
    return {"status": "OK", "content_hash": content_hash, **result}


def agent_run_inspect(args: Any) -> dict[str, Any]:
    """T7: read-only single-run reducer projection, recomputed from events (cache never trusted)."""
    agent_run_id = _require(args, "agent_run_id", "DENIED_AGENT_RUN_ID_REQUIRED", "resubmit with --agent-run-id")
    row = fetch_one(
        "SELECT id, created_at, task_id, agent_type, surface, host, sandbox_mode, approval_mode, os, "
        "extension_version, agent_version, model, worktree_path, cwd, git_branch, git_commit, state, "
        "failure_category, summary FROM agent_runs WHERE id = ?",
        (agent_run_id,),
    )
    if row is None:
        raise KaizenDenied(
            "DENIED_AGENT_RUN_NOT_FOUND",
            {"agent_run_id": agent_run_id, "required_action": "check the id with T3/R0"},
            exit_code=1,
        )
    state = reduce_run(agent_run_id)
    envelope = {
        "id": row[0], "created_at": row[1], "task_id": row[2], "agent_type": row[3], "surface": row[4],
        "host": row[5], "sandbox_mode": row[6], "approval_mode": row[7], "os": row[8],
        "extension_version": row[9], "agent_version": row[10], "model": row[11], "worktree_path": row[12],
        "cwd": row[13], "git_branch": row[14], "git_commit": row[15], "cached_state": row[16],
        "cached_failure_category": row[17], "summary": row[18],
    }
    return {
        "status": "OK",
        "agent_run_id": agent_run_id,
        "envelope": envelope,
        "state": state,
        "blocks_completion": run_blocks_completion(state),
    }


def agent_run_finalize(args: Any) -> dict[str, Any]:
    """T8: terminalize a run. Success is denied while children/approvals are live; a failure
    conclusion force-terminates regardless, recording the leak as truth (the sole escape hatch)."""
    agent_run_id = _require(args, "agent_run_id", "DENIED_AGENT_RUN_ID_REQUIRED", "resubmit with --agent-run-id")
    conclusion = _require(args, "conclusion", "DENIED_CONCLUSION_REQUIRED", "resubmit with --conclusion")
    if conclusion not in _FINALIZE_MARKERS:
        raise KaizenDenied(
            "DENIED_FINALIZE_CONCLUSION",
            {"conclusion": conclusion, "allowed": sorted(_FINALIZE_MARKERS),
             "required_action": "use success|failed|canceled|timed_out"},
            exit_code=2,
        )
    summary = _text(args, "summary", "")
    body = _text(args, "body", "")
    if not summary:
        raise KaizenDenied("DENIED_SUMMARY_REQUIRED", {"required_action": "resubmit with --summary"}, exit_code=2)
    assert_redacted({"summary": summary, "body": body})
    marker = _FINALIZE_MARKERS[conclusion]
    record_id = new_id("ae")
    created = now()

    def op(conn: Any, _attempt: int) -> dict[str, Any]:
        if fetch_run(conn, agent_run_id) is None:
            raise KaizenDenied(
                "DENIED_AGENT_RUN_NOT_FOUND",
                {"agent_run_id": agent_run_id, "required_action": "open the run with T5 first"},
                exit_code=1,
            )
        state = reduce_run_conn(conn, agent_run_id)
        if state["terminal"]:
            raise KaizenDenied(
                "DENIED_AGENT_RUN_ALREADY_FINALIZED",
                {"agent_run_id": agent_run_id, "terminal_state": state["terminal_state"],
                 "required_action": "a run is finalized once; inspect with T7"},
                exit_code=2,
            )
        forced_close = len(state["open_children"]) + len(state["unresolved_approvals"])
        if conclusion == "success" and forced_close:
            raise KaizenDenied(
                "DENIED_AGENT_RUN_NOT_TERMINAL",
                {"agent_run_id": agent_run_id, "open_children": state["open_children"],
                 "unresolved_approvals": state["unresolved_approvals"],
                 "required_action": "resolve the children/approvals, or finalize as failed|canceled|timed_out"},
                exit_code=2,
            )
        child_leak = conclusion != "success" and forced_close > 0
        final_body = body or (
            f"forced-close of {forced_close} open span(s) recorded as leak" if child_leak else ""
        )
        content_hash = utc_text_hash(
            {"id": record_id, "agent_run_id": agent_run_id, "marker": marker, "conclusion": conclusion}
        )
        seq = int(conn.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) FROM agent_events WHERE agent_run_id = ?",
            (agent_run_id,),
        ).fetchone()[0]) + 1
        conn.execute(
            "INSERT INTO agent_events "
            "(id, created_at, agent_run_id, sequence_no, source_event_id, correlation_id, event_kind, "
            "marker, code, name, status_message, summary, body, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id, created, agent_run_id, seq, None, None, "finalization", marker,
                conclusion, None, None, summary, final_body, content_hash,
            ),
        )
        _refresh_cache(conn, agent_run_id)
        return {"forced_close": forced_close, "child_leak": child_leak, "content_hash": content_hash}

    result = write_tx(op)
    return {
        "status": "OK",
        "agent_run_id": agent_run_id,
        "conclusion": conclusion,
        "marker": marker,
        "event_id": record_id,
        **result,
    }


def fetch_run(conn: Any, agent_run_id: str) -> tuple[Any, ...] | None:
    return conn.execute("SELECT id FROM agent_runs WHERE id = ?", (agent_run_id,)).fetchone()


def find_child_leaks(limit: int = 6) -> dict[str, Any]:
    """K1 invariant: terminal-SUCCESS runs that still have an open child. Not a REFERENCES orphan
    (that checks id existence); this is a structural consistency fault the completion gate exists to
    prevent but that crash-path/out-of-order/pre-gate data can still commit. Read-only."""
    run_ids = [r[0] for r in fetch_all("SELECT id FROM agent_runs")]
    violations: list[dict[str, Any]] = []
    for run_id in run_ids:
        state = reduce_run(run_id)
        if state["child_leak"]:
            violations.append({"agent_run_id": run_id, "open_children": state["open_children"]})
    return {
        "invariant": "parent_completed_with_live_children",
        "violations": len(violations),
        "sample": violations[:limit],
    }


def session_digest_sections(limit: int = 5) -> dict[str, Any]:
    """R0 orchestration visibility (read-only). Non-terminal runs are recomputed from events for
    accurate open counts; the cached state column only pre-filters the scan. parent_completed_with_
    live_children is the leak canary (should always be 0 once the gate is in force)."""
    rows = fetch_all(
        "SELECT id, task_id, agent_type, surface, created_at FROM agent_runs "
        "WHERE state IS NULL OR state NOT IN ('completed', 'failed') ORDER BY created_at DESC"
    )
    active: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    live_children = 0
    for r in rows:
        state = reduce_run(r[0])
        if state["terminal"]:
            continue
        entry = {
            "id": r[0], "task_id": r[1], "agent_type": r[2], "surface": r[3],
            "open_children": len(state["open_children"]),
            "unresolved_approvals": len(state["unresolved_approvals"]),
            "created_at": r[4],
        }
        active.append(entry)
        live_children += len(state["open_children"])
        if state["unresolved_approvals"]:
            waiting.append(entry)
    leaks = find_child_leaks()
    return {
        "active_agent_runs": active[:limit],
        "waiting_approvals": waiting[:limit],
        "live_children": live_children,
        "parent_completed_with_live_children": leaks["violations"],
        "counts": {
            "agent_runs_active": len(active),
            "agent_runs_waiting_approval": len(waiting),
            "live_children": live_children,
            "parent_completed_with_live_children": leaks["violations"],
        },
    }
