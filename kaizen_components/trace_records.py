"""Local trace/score data plane (Layer C).

Records a per-task tree of typed activity (`trace_events`) and judgments about
that activity (`eval_scores`). Structured input arrives as a validated JSON
payload (schema-first), input/output are stored as refs/hashes (never raw), and a
redaction gate denies any leaky write.
"""

from __future__ import annotations

import json
from typing import Any

from .db import fetch_all, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import file_sha256, utc_text_hash
from .paths import EXPORT_ROOT, read_text_file, repo_relative
from .redaction import assert_redacted
from .schemas import validate_record
from .task_records import _text_arg


def _payload_from_args(args: Any) -> dict[str, Any]:
    raw = getattr(args, "payload_json", None)
    if getattr(args, "payload_json_file", None):
        raw = read_text_file(args.payload_json_file)
    payload: dict[str, Any] = json.loads(raw) if raw else {}
    if not isinstance(payload, dict):
        raise KaizenDenied(
            "DENIED_PAYLOAD_TYPE",
            {"required_action": "--payload-json must be a JSON object"},
            exit_code=2,
        )
    return payload


def add_trace_event(args: Any) -> dict[str, Any]:
    payload = _payload_from_args(args)
    if getattr(args, "kind", None):
        payload.setdefault("kind", args.kind)
    if getattr(args, "task_id", None):
        payload.setdefault("task_id", args.task_id)
    summary = _text_arg(args, "summary", payload.get("summary", ""))
    body = _text_arg(args, "body", payload.get("body", ""))
    payload["summary"] = summary
    payload["body"] = body
    payload.setdefault("level", "default")
    payload.setdefault("status", "recorded")

    validate_record("trace_event", payload)
    assert_redacted(
        {
            "summary": summary,
            "body": body,
            "name": payload.get("name", ""),
            "status_message": payload.get("status_message", ""),
            "input_ref": payload.get("input_ref", ""),
            "output_ref": payload.get("output_ref", ""),
            "environment": payload.get("environment", "") or "",
            "session_id": payload.get("session_id", "") or "",
            "tags": " ".join(str(tag) for tag in payload.get("tags", []) or []),
        }
    )
    # Set only after assert_redacted has passed (it raises on any hit), so the stored
    # provenance reflects the scan outcome rather than an unconditional literal.
    redaction_status = "scanned_clean"

    record_id = new_id("te")
    created = now()
    content_hash = utc_text_hash({"id": record_id, **payload})
    tags_json = json.dumps(payload.get("tags", []))

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO trace_events "
            "(id, created_at, task_id, trace_id, parent_event_id, kind, name, level, environment, session_id, "
            "status, status_message, model, provider, prompt_tokens, completion_tokens, total_tokens, cost, "
            "latency_ms, tags_json, input_ref, output_ref, redaction_status, summary, body, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                created,
                payload.get("task_id"),
                payload.get("trace_id"),
                payload.get("parent_event_id"),
                payload["kind"],
                payload.get("name"),
                payload["level"],
                payload.get("environment"),
                payload.get("session_id"),
                payload["status"],
                payload.get("status_message"),
                payload.get("model"),
                payload.get("provider"),
                payload.get("prompt_tokens"),
                payload.get("completion_tokens"),
                payload.get("total_tokens"),
                payload.get("cost"),
                payload.get("latency_ms"),
                tags_json,
                payload.get("input_ref"),
                payload.get("output_ref"),
                redaction_status,
                summary,
                body,
                content_hash,
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "content_hash": content_hash}


def add_eval_score(args: Any) -> dict[str, Any]:
    payload = _payload_from_args(args)
    validate_record("eval_score", payload)

    value = payload["value"]
    data_type = payload["data_type"]
    value_num: float | None = None
    value_label: str | None = None
    if data_type == "numeric":
        if isinstance(value, bool):
            raise KaizenDenied(
                "DENIED_SCORE_VALUE",
                {"required_action": "a numeric score requires a number value, not a boolean (use data_type boolean)"},
                exit_code=2,
            )
        try:
            value_num = float(value)
        except (TypeError, ValueError):
            raise KaizenDenied(
                "DENIED_SCORE_VALUE",
                {"required_action": "a numeric score requires a number value"},
                exit_code=2,
            )
    elif data_type == "boolean":
        truthy = value in (True, 1, "true", "True", "yes")
        value_num = 1.0 if truthy else 0.0
        value_label = str(value)
    else:
        value_label = str(value)

    # Same redaction guarantee the module docstring promises for T1: deny a leaky
    # score write rather than persisting a raw secret/personal path in free text.
    assert_redacted(
        {
            "name": payload["name"],
            "comment": payload.get("comment", ""),
            "value_label": value_label or "",
        }
    )

    record_id = new_id("es")
    created = now()
    content_hash = utc_text_hash({"id": record_id, **payload})

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO eval_scores "
            "(id, created_at, trace_event_id, eval_run_id, verification_id, name, value_num, value_label, "
            "data_type, source, comment, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                created,
                payload.get("trace_event_id"),
                payload.get("eval_run_id"),
                payload.get("verification_id"),
                payload["name"],
                value_num,
                value_label,
                data_type,
                payload["source"],
                payload.get("comment"),
                content_hash,
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "content_hash": content_hash}


def query_eval_scores(args: Any) -> dict[str, Any]:
    """T4: read eval scores back out of the record plane, with aggregates.

    Filter by --trace-id (matched against trace_event_id), --id (eval_run_id), and/or
    --query (score name LIKE). Numeric/boolean scores are aggregated (count, mean,
    min, max) so quality trends are visible without exporting rows to a spreadsheet.
    """
    conditions: list[str] = []
    params: list[Any] = []
    trace_event_id = getattr(args, "trace_id", None)
    if trace_event_id:
        conditions.append("trace_event_id = ?")
        params.append(trace_event_id)
    eval_run_id = getattr(args, "id", None)
    if eval_run_id:
        conditions.append("eval_run_id = ?")
        params.append(eval_run_id)
    query = getattr(args, "query", None)
    if query:
        conditions.append("name LIKE ?")
        params.append(f"%{query}%")
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(int(getattr(args, "limit", None) or 50))
    rows = fetch_all(
        "SELECT id, created_at, trace_event_id, eval_run_id, verification_id, name, value_num, value_label, "
        f"data_type, source, comment FROM eval_scores{where} ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )
    numeric = [float(r[6]) for r in rows if r[6] is not None]
    aggregates: dict[str, Any] = {"count": len(rows), "numeric_count": len(numeric)}
    if numeric:
        aggregates.update(
            {
                "mean": sum(numeric) / len(numeric),
                "min": min(numeric),
                "max": max(numeric),
            }
        )
    return {
        "status": "OK",
        "records": [
            {
                "id": r[0],
                "created_at": r[1],
                "trace_event_id": r[2],
                "eval_run_id": r[3],
                "verification_id": r[4],
                "name": r[5],
                "value_num": r[6],
                "value_label": r[7],
                "data_type": r[8],
                "source": r[9],
                "comment": r[10],
            }
            for r in rows
        ],
        "aggregates": aggregates,
    }


def trace_report(args: Any) -> dict[str, Any]:
    task_id = getattr(args, "task_id", None)
    trace_id = getattr(args, "trace_id", None)
    if task_id:
        column, key = "task_id", task_id
    elif trace_id:
        column, key = "trace_id", trace_id
    else:
        raise KaizenDenied(
            "DENIED_ID_REQUIRED",
            {"required_action": "pass --task-id or --trace-id"},
            exit_code=2,
        )
    rows = fetch_all(
        f"SELECT id, created_at, kind, level, status, model, prompt_tokens, completion_tokens, total_tokens, "
        f"cost, latency_ms, summary FROM trace_events WHERE {column} = ? ORDER BY created_at",
        (key,),
    )
    total_tokens = sum(int(r[8] or 0) for r in rows)
    total_cost = sum(float(r[9] or 0) for r in rows)
    out_dir = EXPORT_ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"trace-{key}.md"
    lines = [
        f"# Trace report {key}",
        "",
        f"Events: {len(rows)}",
        f"Total tokens: {total_tokens}",
        f"Total cost: {total_cost}",
        "",
    ]
    for r in rows:
        lines.append(
            f"- `{r[0]}` {r[1]} [{r[2]}:{r[3]}:{r[4]}] {r[5] or ''} "
            f"tok={r[8] or 0} cost={r[9] or 0} {r[11]}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "status": "OK",
        "key": key,
        "events": len(rows),
        "total_tokens": total_tokens,
        "total_cost": total_cost,
        "path": repo_relative(path),
        "sha256": file_sha256(path),
    }
