from __future__ import annotations

import copy
import json
from typing import Any

from . import TOOL_VERSION
from .db import fetch_all, fetch_one, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import utc_text_hash, validate_text_fields
from .paths import read_text_file
from .text_search import like_pattern
from .schemas import has_schema, validate_record


LIFECYCLE_TABLES = {
    "gotcha": ("gotcha", "gotcha_revision", "gotcha_id", "g"),
    "learning": ("learning", "learning_revision", "learning_id", "l"),
    "learned": ("learned", "learned_revision", "learned_id", "ld"),
    "tasks": ("tasks", "task_revision", "task_id", "t"),
}

# W2 completion vocabulary. task.status is free-form (any string), so this is a closed set of
# completion-intent statuses matched strip().lower(); reaching one while a linked agent run still has
# live children/approvals is denied (recorded orchestration truth outranks a premature "done").
_TERMINAL_TASK_STATUSES = {"done", "completed", "resolved", "closed"}


def _text_arg(args: Any, name: str, default: str = "") -> str:
    """Resolves a text arg, falling back to `<name>_file` (read_text_file) then `default`."""
    value = getattr(args, name, None)
    if value is None:
        file_value = getattr(args, f"{name}_file", None)
        if file_value:
            return read_text_file(file_value)
        return default
    return value


def _base_fields(args: Any, *, title_required: bool = True) -> dict[str, str]:
    """Assembles common lifecycle fields from args (title required unless `title_required=False`); validates summary/body."""
    title = _text_arg(args, "title", "")
    if title_required and not title:
        raise KaizenDenied(
            "DENIED_TITLE_REQUIRED",
            {"field": "title", "required_action": "resubmit with --title"},
            exit_code=2,
        )
    fields = {
        "scope": getattr(args, "scope", None) or "project",
        "status": getattr(args, "status", None) or "active",
        "title": title,
        "summary": _text_arg(args, "summary", ""),
        "body": _text_arg(args, "body", ""),
        "source_task_id": getattr(args, "task_id", None) or "",
        "source_command": getattr(args, "operation", None) or "",
        "writer_role": getattr(args, "writer_role", None) or "agent",
    }
    validate_text_fields({"summary": fields["summary"], "body": fields["body"]})
    return fields


def add_lifecycle_record(kind: str, args: Any, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Insert a lifecycle head + first revision atomically; hash the same mutable fields as updates."""
    table, rev_table, rev_fk, prefix = LIFECYCLE_TABLES[kind]
    fields = _base_fields(args)
    if has_schema(kind):
        clean = {
            "title": fields["title"],
            "summary": fields["summary"],
            "body": fields["body"],
            "scope": fields["scope"],
            "status": fields["status"],
            "writer_role": fields["writer_role"],
        }
        if fields["source_task_id"]:
            clean["task_id"] = fields["source_task_id"]
        validate_record(kind, clean)
    record_id = new_id(prefix)
    revision_id = new_id(f"{prefix}r")
    created = now()
    is_test = 1 if getattr(args, "test", False) else 0  # removable live-test row (K7 purge-test)
    extra = extra or {}
    content_hash = utc_text_hash({
        "id": record_id, "summary": fields["summary"], "body": fields["body"], "status": fields["status"],
    })

    def op(conn: Any, _attempt: int) -> None:
        columns = [
            "id",
            "created_at",
            "updated_at",
            "scope",
            "status",
            "title",
            "summary",
            "body",
            "source_command",
            "writer_role",
        ]
        values = [
            record_id,
            created,
            created,
            fields["scope"],
            fields["status"],
            fields["title"],
            fields["summary"],
            fields["body"],
            fields["source_command"],
            fields["writer_role"],
        ]
        if table != "tasks":
            columns.extend(["source_task_id", "artifact_ids_json"])
            values.extend([fields["source_task_id"] or None, json.dumps(extra.get("artifact_ids", []))])
        if table == "learning":
            columns.append("source_gotcha_id")
            values.append(extra.get("source_gotcha_id"))
        if table == "learned":
            columns.append("source_learning_id")
            values.append(extra.get("source_learning_id"))
        columns.extend(["content_hash", "current_revision_id", "is_test"])
        values.extend([content_hash, revision_id, is_test])
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in values)})",
            tuple(values),
        )
        conn.execute(
            f"INSERT INTO {rev_table} "
            f"(id, {rev_fk}, created_at, revision_number, summary, body, status, content_hash, previous_hash, source_command) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_id,
                record_id,
                created,
                1,
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


def update_lifecycle_record(kind: str, args: Any) -> dict[str, Any]:
    """Appends a revision + updates head; enforces task completion gate (terminal status blocked while a linked agent run has live children/unresolved approvals, recomputed on the write conn); `previous_hash` chains the prior head hash."""
    table, rev_table, rev_fk, prefix = LIFECYCLE_TABLES[kind]
    record_id = getattr(args, "id", None)
    if not record_id:
        raise KaizenDenied("DENIED_ID_REQUIRED", {"required_action": "resubmit with --id"}, exit_code=2)
    row = fetch_one(
        f"SELECT summary, body, status, content_hash FROM {table} WHERE id = ?",
        (record_id,),
    )
    if row is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": record_id, "table": table}, exit_code=1)
    summary = _text_arg(args, "summary", row[0])
    body = _text_arg(args, "body", row[1])
    status = getattr(args, "status", None) or row[2]
    validate_text_fields({"summary": summary, "body": body})
    revision_id = new_id(f"{prefix}r")
    created = now()
    content_hash = utc_text_hash({"id": record_id, "summary": summary, "body": body, "status": status})
    gate_terminal = kind == "tasks" and (status or "").strip().lower() in _TERMINAL_TASK_STATUSES

    def op(conn: Any, _attempt: int) -> None:
        # Completion gate, recomputed from agent_events on THIS write connection (no TOCTOU window):
        # a task cannot reach a terminal status while a linked, non-terminal agent run still has an
        # open child or unresolved approval. Fail-open: non-task kinds and non-terminal statuses skip.
        if gate_terminal:
            from .agent_runs import task_live_orchestration

            block = task_live_orchestration(conn, record_id)
            if block:
                raise KaizenDenied(
                    "DENIED_TASK_HAS_LIVE_CHILDREN",
                    {
                        "task_id": record_id,
                        "status": status,
                        **block,
                        "required_action": "finalize the linked agent run(s) with T8 before closing the task",
                    },
                    exit_code=2,
                )
        current = conn.execute(f"SELECT content_hash FROM {table} WHERE id = ?", (record_id,)).fetchone()
        if current is None:
            raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": record_id, "table": table}, exit_code=1)
        revision_count = conn.execute(
            f"SELECT COUNT(*) FROM {rev_table} WHERE {rev_fk} = ?", (record_id,),
        ).fetchone()[0]
        conn.execute(
            f"UPDATE {table} SET updated_at = ?, summary = ?, body = ?, status = ?, "
            "content_hash = ?, current_revision_id = ? WHERE id = ?",
            (created, summary, body, status, content_hash, revision_id, record_id),
        )
        conn.execute(
            f"INSERT INTO {rev_table} "
            f"(id, {rev_fk}, created_at, revision_number, summary, body, status, content_hash, previous_hash, source_command) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_id,
                record_id,
                created,
                int(revision_count) + 1,
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


def list_records(table: str, args: Any) -> dict[str, Any]:
    """Recent records DESC (`--limit` default 20); `table` MUST be a trusted literal (identifier is f-string-interpolated, not bound)."""
    limit = int(getattr(args, "limit", None) or 20)
    rows = fetch_all(
        f"SELECT id, status, scope, title, summary, created_at FROM {table} ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    records = [
        {"id": r[0], "status": r[1], "scope": r[2], "title": r[3], "summary": r[4], "created_at": r[5]}
        for r in rows
    ]
    return {"status": "OK", "records": records, "count": len(records)}


def query_records(table: str, args: Any) -> dict[str, Any]:
    """Escaped-LIKE search over title/summary/body; requires `--query`; same trusted-`table` caveat."""
    query = getattr(args, "query", None) or ""
    if not query:
        raise KaizenDenied("DENIED_QUERY_REQUIRED", {"required_action": "resubmit with --query"}, exit_code=2)
    pattern = like_pattern(query)
    rows = fetch_all(
        f"SELECT id, status, scope, title, summary, created_at FROM {table} "
        "WHERE title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' "
        "ORDER BY created_at DESC LIMIT ?",
        (pattern, pattern, pattern, int(getattr(args, "limit", None) or 20)),
    )
    records = [
        {"id": r[0], "status": r[1], "scope": r[2], "title": r[3], "summary": r[4], "created_at": r[5]}
        for r in rows
    ]
    return {"status": "OK", "records": records, "count": len(records), "query": query}


def inspect_record(table: str, args: Any) -> dict[str, Any]:
    """Full row for `--id` via `SELECT *` + `PRAGMA table_info` zip; requires `--id`; trusted-`table` caveat."""
    record_id = getattr(args, "id", None)
    if not record_id:
        raise KaizenDenied("DENIED_ID_REQUIRED", {"required_action": "resubmit with --id"}, exit_code=2)
    row = fetch_one(f"SELECT * FROM {table} WHERE id = ?", (record_id,))
    if row is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": record_id, "table": table}, exit_code=1)
    columns = [r[1] for r in fetch_all(f"PRAGMA table_info({table})")]
    return {"status": "OK", "record": dict(zip(columns, row))}


def add_ledger_event(args: Any, *, title: str | None = None, body: str | None = None) -> dict[str, Any]:
    """Writes one append-only ledger_events row (no revision table); `title`/`body` kwargs override arg-derived text; NOTE reads live `args.summary` (see F2 interaction)."""
    ledger_args = copy.copy(args)
    ledger_args.title = title or _text_arg(args, "title", "ledger event")
    ledger_args.summary = _text_arg(args, "summary", title or "Ledger event recorded.")
    ledger_args.body = body or _text_arg(args, "body", "")
    ledger_args.status = getattr(args, "status", None) or "noted"
    fields = _base_fields(ledger_args, title_required=False)
    fields["task_id"] = fields.pop("source_task_id") or None
    record_id = new_id("led")
    created = now()
    content_hash = utc_text_hash({"id": record_id, **fields})

    is_test = 1 if getattr(args, "test", False) else 0

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO ledger_events "
            "(id, created_at, task_id, scope, status, title, summary, body, source_command, writer_role, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                created,
                fields["task_id"],
                fields["scope"],
                fields["status"],
                fields["title"],
                fields["summary"],
                fields["body"],
                fields["source_command"],
                fields["writer_role"],
                content_hash,
                is_test,
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "content_hash": content_hash}


def _transition_promoted_source(kind: str, source_id: str, operation: str, result: dict[str, Any]) -> None:
    """Mark a promotion's source record status='promoted' (with a revision row).

    Runs AFTER the new record is committed, so a transition failure must never fail the
    promotion (that would invite a duplicate retry) -- it surfaces as a warning instead.
    Without this, promoted GOTCHAs stayed 'active' forever and polluted the R0 digest.
    """
    from types import SimpleNamespace

    try:
        update_lifecycle_record(kind, SimpleNamespace(id=source_id, status="promoted", operation=operation))
        result[f"source_{kind}_status"] = "promoted"
    except Exception as error:  # noqa: BLE001 -- promotion is committed; warn, never fail
        result[f"{kind}_transition_warning"] = f"source {kind} not marked promoted: {error}"


def promote_gotcha_to_learning(args: Any) -> dict[str, Any]:
    source_id = getattr(args, "id", None)
    if not source_id:
        raise KaizenDenied("DENIED_ID_REQUIRED", {"required_action": "resubmit with --id"}, exit_code=2)
    row = fetch_one("SELECT scope, title, summary, body FROM gotcha WHERE id = ?", (source_id,))
    if row is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": source_id, "table": "gotcha"}, exit_code=1)
    promoted_args = copy.copy(args)
    promoted_args.scope, promoted_args.title, promoted_args.summary, promoted_args.body = row[0], row[1], row[2], row[3]
    result = add_lifecycle_record("learning", promoted_args, extra={"source_gotcha_id": source_id})
    _transition_promoted_source("gotcha", source_id, "L2", result)
    return result


def promote_learning_to_learned(args: Any) -> dict[str, Any]:
    source_id = getattr(args, "id", None)
    if not source_id:
        raise KaizenDenied("DENIED_ID_REQUIRED", {"required_action": "resubmit with --id"}, exit_code=2)
    row = fetch_one("SELECT scope, title, summary, body FROM learning WHERE id = ?", (source_id,))
    if row is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": source_id, "table": "learning"}, exit_code=1)
    promoted_args = copy.copy(args)
    promoted_args.scope, promoted_args.title, promoted_args.summary, promoted_args.body = row[0], row[1], row[2], row[3]
    result = add_lifecycle_record("learned", promoted_args, extra={"source_learning_id": source_id})
    _transition_promoted_source("learning", source_id, "L3", result)
    return result


def learned_context(args: Any) -> dict[str, Any]:
    """L10: export recent LEARNED lessons with their promotion chain for context injection.

    Each record carries its GOTCHA -> LEARNING -> LEARNED genealogy plus a one-line
    narrative, so an agent (or a system prompt) can consume past lessons directly
    instead of stitching L7/L9/G4 lookups. Read-only; optional --query filters by text.
    """
    query = getattr(args, "query", None)
    limit = int(getattr(args, "limit", None) or 10)
    if query:
        pattern = like_pattern(query)
        rows = fetch_all(
            "SELECT id, title, summary, created_at, source_learning_id FROM learned "
            "WHERE title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC LIMIT ?",
            (pattern, pattern, pattern, limit),
        )
    else:
        rows = fetch_all(
            "SELECT id, title, summary, created_at, source_learning_id FROM learned "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    records = []
    for r in rows:
        learned_id, title, summary, created_at, source_learning_id = r
        chain: dict[str, Any] = {}
        if source_learning_id:
            learning = fetch_one(
                "SELECT id, title, summary, source_gotcha_id FROM learning WHERE id = ?",
                (source_learning_id,),
            )
            if learning:
                chain["learning"] = {"id": learning[0], "title": learning[1], "summary": learning[2]}
                if learning[3]:
                    gotcha = fetch_one("SELECT id, title, summary FROM gotcha WHERE id = ?", (learning[3],))
                    if gotcha:
                        chain["gotcha"] = {"id": gotcha[0], "title": gotcha[1], "summary": gotcha[2]}
        narrative_parts = []
        if "gotcha" in chain:
            narrative_parts.append(f"GOTCHA: {chain['gotcha']['summary']}")
        if "learning" in chain:
            narrative_parts.append(f"LEARNING: {chain['learning']['summary']}")
        narrative_parts.append(f"LEARNED: {summary}")
        records.append(
            {
                "id": learned_id,
                "title": title,
                "summary": summary,
                "created_at": created_at,
                "chain": chain,
                "narrative": " -> ".join(narrative_parts),
            }
        )
    return {"status": "OK", "records": records, "count": len(records)}


def version_payload() -> dict[str, Any]:
    """Returns TOOL_VERSION status payload (trivial; minor gap)."""
    return {"status": "OK", "tool_version": TOOL_VERSION}


__all__ = [
    "LIFECYCLE_TABLES",
    "add_ledger_event",
    "add_lifecycle_record",
    "inspect_record",
    "learned_context",
    "list_records",
    "promote_gotcha_to_learning",
    "promote_learning_to_learned",
    "query_records",
    "update_lifecycle_record",
    "version_payload",
]
