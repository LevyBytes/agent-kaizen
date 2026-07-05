from __future__ import annotations

from typing import Any

from .db import fetch_all, fetch_one, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import utc_text_hash, validate_text_fields, validate_word_limit
from .task_records import _text_arg
from .text_search import like_pattern


def _priority_rank(priority: str) -> int:
    ranks = {
        "critical": 0,
        "high": 1,
        "normal": 2,
        "medium": 2,
        "low": 3,
    }
    return ranks.get((priority or "").strip().lower(), 2)


# SQL twin of _priority_rank, applied in ORDER BY so the LIMIT window is priority-aware.
# Without it, truncation was recency-based and an OLD critical rule silently dropped out of
# X5/R0 once newer low-priority rules exceeded the limit. Keep both in sync.
PRIORITY_ORDER_SQL = (
    "CASE lower(trim(priority)) WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
    "WHEN 'normal' THEN 2 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 2 END"
)


def _policy_fields(args: Any) -> dict[str, str]:
    title = _text_arg(args, "title", "")
    if not title:
        raise KaizenDenied(
            "DENIED_TITLE_REQUIRED",
            {"field": "title", "required_action": "resubmit with --title"},
            exit_code=2,
        )
    fields = {
        "scope": getattr(args, "scope", None) or "project",
        "trigger": getattr(args, "trigger", None) or "session-start",
        "priority": getattr(args, "priority", None) or "normal",
        "status": getattr(args, "status", None) or "active",
        "title": title,
        "summary": _text_arg(args, "summary", ""),
        "body": _text_arg(args, "body", ""),
        "source_command": getattr(args, "operation", "") or "",
        "writer_role": getattr(args, "writer_role", None) or "agent",
    }
    validate_text_fields({"summary": fields["summary"], "body": fields["body"]})
    validate_word_limit("trigger", fields["trigger"], limit=120)
    validate_word_limit("priority", fields["priority"], limit=20)
    return fields


def add_policy(args: Any) -> dict[str, Any]:
    fields = _policy_fields(args)
    record_id = new_id("pol")
    revision_id = new_id("polr")
    created = now()
    content_hash = utc_text_hash({"id": record_id, **fields})

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO private_policy "
            "(id, created_at, updated_at, scope, trigger, priority, status, title, summary, body, "
            "source_command, writer_role, content_hash, current_revision_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                created,
                created,
                fields["scope"],
                fields["trigger"],
                fields["priority"],
                fields["status"],
                fields["title"],
                fields["summary"],
                fields["body"],
                fields["source_command"],
                fields["writer_role"],
                content_hash,
                revision_id,
            ),
        )
        conn.execute(
            "INSERT INTO private_policy_revision "
            "(id, policy_id, created_at, revision_number, trigger, priority, summary, body, status, "
            "content_hash, previous_hash, source_command) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_id,
                record_id,
                created,
                1,
                fields["trigger"],
                fields["priority"],
                fields["summary"],
                fields["body"],
                fields["status"],
                content_hash,
                None,
                fields["source_command"],
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "revision_id": revision_id, "content_hash": content_hash}


def list_policies(args: Any) -> dict[str, Any]:
    limit = int(getattr(args, "limit", None) or 20)
    rows = fetch_all(
        "SELECT id, status, scope, trigger, priority, title, summary, created_at FROM private_policy "
        f"ORDER BY {PRIORITY_ORDER_SQL}, created_at DESC LIMIT ?",
        (limit,),
    )
    records = [
        {
            "id": r[0],
            "status": r[1],
            "scope": r[2],
            "trigger": r[3],
            "priority": r[4],
            "title": r[5],
            "summary": r[6],
            "created_at": r[7],
        }
        for r in rows
    ]
    records.sort(key=lambda item: (_priority_rank(item["priority"]), item["created_at"]))
    return {"status": "OK", "records": records, "count": len(records)}


def query_policies(args: Any) -> dict[str, Any]:
    query = getattr(args, "query", None) or ""
    if not query:
        raise KaizenDenied("DENIED_QUERY_REQUIRED", {"required_action": "resubmit with --query"}, exit_code=2)
    pattern = like_pattern(query)
    rows = fetch_all(
        "SELECT id, status, scope, trigger, priority, title, summary, created_at FROM private_policy "
        "WHERE title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' "
        "OR trigger LIKE ? ESCAPE '\\' OR scope LIKE ? ESCAPE '\\' "
        f"ORDER BY {PRIORITY_ORDER_SQL}, created_at DESC LIMIT ?",
        (pattern, pattern, pattern, pattern, pattern, int(getattr(args, "limit", None) or 20)),
    )
    records = [
        {
            "id": r[0],
            "status": r[1],
            "scope": r[2],
            "trigger": r[3],
            "priority": r[4],
            "title": r[5],
            "summary": r[6],
            "created_at": r[7],
        }
        for r in rows
    ]
    records.sort(key=lambda item: (_priority_rank(item["priority"]), item["created_at"]))
    return {"status": "OK", "records": records, "count": len(records), "query": query}


def inspect_policy(args: Any) -> dict[str, Any]:
    record_id = getattr(args, "id", None)
    if not record_id:
        raise KaizenDenied("DENIED_ID_REQUIRED", {"required_action": "resubmit with --id"}, exit_code=2)
    row = fetch_one("SELECT * FROM private_policy WHERE id = ?", (record_id,))
    if row is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": record_id, "table": "private_policy"}, exit_code=1)
    columns = [r[1] for r in fetch_all("PRAGMA table_info(private_policy)")]
    return {"status": "OK", "record": dict(zip(columns, row))}


def session_context(args: Any) -> dict[str, Any]:
    limit = int(getattr(args, "limit", None) or 50)
    trigger = getattr(args, "trigger", None)
    if trigger:
        rows = fetch_all(
            "SELECT id, scope, trigger, priority, title, summary, body, created_at FROM private_policy "
            f"WHERE status = ? AND (trigger = ? OR trigger = ?) ORDER BY {PRIORITY_ORDER_SQL}, created_at DESC LIMIT ?",
            ("active", trigger, "session-start", limit),
        )
    else:
        rows = fetch_all(
            "SELECT id, scope, trigger, priority, title, summary, body, created_at FROM private_policy "
            f"WHERE status = ? ORDER BY {PRIORITY_ORDER_SQL}, created_at DESC LIMIT ?",
            ("active", limit),
        )
    records = [
        {
            "id": r[0],
            "scope": r[1],
            "trigger": r[2],
            "priority": r[3],
            "title": r[4],
            "summary": r[5],
            "body": r[6],
            "created_at": r[7],
        }
        for r in rows
    ]
    records.sort(key=lambda item: (_priority_rank(item["priority"]), item["created_at"]))
    return {
        "status": "OK",
        "message": "Private policy context loaded.",
        "records": records,
        "count": len(records),
        "required_action": "apply these private policy records during this session and reload after compaction",
    }
