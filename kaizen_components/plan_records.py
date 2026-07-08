from __future__ import annotations

from typing import Any

from .db import fetch_all, fetch_one, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import utc_text_hash, validate_text_fields
from .task_records import _text_arg


def add_plan(args: Any) -> dict[str, Any]:
    title = _text_arg(args, "title", "")
    if not title:
        raise KaizenDenied("DENIED_TITLE_REQUIRED", {"required_action": "resubmit with --title"}, exit_code=2)
    summary = _text_arg(args, "summary", "")
    body = _text_arg(args, "body", "")
    validate_text_fields({"summary": summary, "body": body})
    record_id = new_id("p")
    revision_id = new_id("pr")
    created = now()
    status = getattr(args, "status", None) or "draft"
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
    record_id = getattr(args, "id", None)
    if not record_id:
        raise KaizenDenied("DENIED_ID_REQUIRED", {"required_action": "resubmit with --id"}, exit_code=2)
    row = fetch_one("SELECT summary, body, status, content_hash FROM plans WHERE id = ?", (record_id,))
    if row is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": record_id, "table": "plans"}, exit_code=1)
    summary = _text_arg(args, "summary", row[0])
    body = _text_arg(args, "body", row[1])
    status = getattr(args, "status", None) or row[2]
    validate_text_fields({"summary": summary, "body": body})
    revision_id = new_id("pr")
    created = now()
    count = fetch_one("SELECT COUNT(*) FROM plan_revision WHERE plan_id = ?", (record_id,))[0]
    content_hash = utc_text_hash({"id": record_id, "summary": summary, "body": body, "status": status})

    def op(conn: Any, _attempt: int) -> None:
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
                row[3],
                getattr(args, "operation", ""),
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "revision_id": revision_id, "content_hash": content_hash}


def list_plans(args: Any) -> dict[str, Any]:
    rows = fetch_all(
        "SELECT id, task_id, status, title, summary, created_at FROM plans ORDER BY created_at DESC LIMIT ?",
        (int(getattr(args, "limit", None) or 20),),
    )
    return {
        "status": "OK",
        "records": [
            {"id": r[0], "task_id": r[1], "status": r[2], "title": r[3], "summary": r[4], "created_at": r[5]}
            for r in rows
        ],
    }
