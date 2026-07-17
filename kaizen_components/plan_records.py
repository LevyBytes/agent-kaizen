"""Create and append hash-chained W-family plan records with statuses draft, active, blocked, completed, or canceled."""

from __future__ import annotations

from typing import Any

from .db import fetch_all, fetch_one, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import utc_text_hash, validate_text_fields
from .task_records import _text_arg


PLAN_STATUSES = frozenset({"draft", "active", "blocked", "completed", "canceled"})


def _plan_status(value: Any) -> str:
    """Return an exact supported plan status or deny unknown input/stored data before it is hashed or surfaced."""
    if not isinstance(value, str) or value not in PLAN_STATUSES:
        raise KaizenDenied(
            "DENIED_PLAN_STATUS_INVALID",
            {
                "status": value,
                "allowed": sorted(PLAN_STATUSES),
                "required_action": "use one of the documented exact plan statuses",
            },
            exit_code=2,
        )
    return value


def _limit(args: Any) -> int:
    """Return the default or a strictly positive explicit record limit."""
    value = getattr(args, "limit", None)
    if value is None:
        return 20
    if type(value) is not int or value <= 0:
        raise KaizenDenied(
            "DENIED_LIMIT_INVALID",
            {"limit": value, "required_action": "pass a positive integer --limit"},
            exit_code=2,
        )
    return value


def add_plan(args: Any) -> dict[str, Any]:
    """Creates plans row + rev1 (previous_hash=None); requires --title (DENIED_TITLE_REQUIRED exit2); returns {status,id,revision_id,content_hash}."""
    title = _text_arg(args, "title", "")
    if not title:
        raise KaizenDenied("DENIED_TITLE_REQUIRED", {"required_action": "resubmit with --title"}, exit_code=2)
    summary = _text_arg(args, "summary", "")
    body = _text_arg(args, "body", "")
    validate_text_fields({"summary": summary, "body": body})
    record_id = new_id("p")
    revision_id = new_id("pr")
    created = now()
    raw_status = getattr(args, "status", None)
    status = _plan_status("draft" if raw_status is None else raw_status)
    is_test = 1 if getattr(args, "test", False) else 0
    content_hash = utc_text_hash({"id": record_id, "title": title, "summary": summary, "body": body})

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO plans "
            "(id, task_id, created_at, updated_at, status, title, summary, body, source_command, writer_role, "
            "content_hash, current_revision_id, is_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                getattr(args, "task_id", None),
                created,
                created,
                status,
                title,
                summary,
                body,
                getattr(args, "operation", ""),
                getattr(args, "writer_role", None) or "agent",
                content_hash,
                revision_id,
                is_test,
            ),
        )
        conn.execute(
            "INSERT INTO plan_revision "
            "(id, plan_id, created_at, revision_number, summary, body, status, content_hash, previous_hash, source_command) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (revision_id, record_id, created, 1, summary, body, status, content_hash, None, getattr(args, "operation", "")),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "revision_id": revision_id, "content_hash": content_hash}


def revise_plan(args: Any) -> dict[str, Any]:
    """Appends plan_revision n+1, updates plans head; carries prior summary/body/status when unset; previous_hash=prior content_hash; DENIED_ID_REQUIRED exit2 / DENIED_RECORD_NOT_FOUND exit1."""
    record_id = getattr(args, "id", None)
    if not record_id:
        raise KaizenDenied("DENIED_ID_REQUIRED", {"required_action": "resubmit with --id"}, exit_code=2)
    row = fetch_one("SELECT summary, body, status, content_hash FROM plans WHERE id = ?", (record_id,))
    if row is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": record_id, "table": "plans"}, exit_code=1)
    summary_supplied = getattr(args, "summary", None) is not None or bool(getattr(args, "summary_file", None))
    summary = _text_arg(args, "summary", row[0])
    body = _text_arg(args, "body", row[1])
    raw_status = getattr(args, "status", None)
    status = _plan_status(row[2] if raw_status is None else raw_status)
    validate_text_fields({"summary": summary, "body": body}, summary_required=summary_supplied or bool(row[0]))
    revision_id = new_id("pr")
    created = now()
    content_hash = utc_text_hash({"id": record_id, "summary": summary, "body": body, "status": status})

    def op(conn: Any, _attempt: int) -> None:
        current = conn.execute("SELECT content_hash FROM plans WHERE id = ?", (record_id,)).fetchone()
        if current is None:
            raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": record_id, "table": "plans"}, exit_code=1)
        count = conn.execute("SELECT COUNT(*) FROM plan_revision WHERE plan_id = ?", (record_id,)).fetchone()[0]
        conn.execute(
            "UPDATE plans SET updated_at = ?, summary = ?, body = ?, status = ?, content_hash = ?, "
            "current_revision_id = ? WHERE id = ?",
            (created, summary, body, status, content_hash, revision_id, record_id),
        )
        conn.execute(
            "INSERT INTO plan_revision "
            "(id, plan_id, created_at, revision_number, summary, body, status, content_hash, previous_hash, source_command) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_id,
                record_id,
                created,
                int(count) + 1,
                summary,
                body,
                status,
                content_hash,
                current[0],
                getattr(args, "operation", ""),
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "revision_id": revision_id, "content_hash": content_hash}


def list_plans(args: Any) -> dict[str, Any]:
    """Most-recent-first, default LIMIT 20; returns {status,records[...]}."""
    rows = fetch_all(
        "SELECT id, task_id, status, title, summary, created_at FROM plans ORDER BY created_at DESC LIMIT ?",
        (_limit(args),),
    )
    records = [
        {"id": r[0], "task_id": r[1], "status": _plan_status(r[2]), "title": r[3], "summary": r[4], "created_at": r[5]}
        for r in rows
    ]
    return {
        "status": "OK",
        "records": records,
    }


__all__ = ["PLAN_STATUSES", "add_plan", "list_plans", "revise_plan"]
