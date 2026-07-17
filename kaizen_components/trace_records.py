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
from .text_search import like_pattern


def _payload_from_args(args: Any) -> dict[str, Any]:
    """Private; parses --payload-json / --payload-json-file (file wins), returns {} for empty, denies non-object with DENIED_PAYLOAD_TYPE (exit 2)."""
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
    """T1 add path: merges flag defaults into payload, validates trace_event, runs the redaction gate over free-text fields, inserts one trace_events row; returns {status,id,content_hash}."""
    payload = _payload_from_args(args)
    if getattr(args, "kind", None):
        payload["kind"] = args.kind
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
    is_test = 1 if getattr(args, "test", False) else 0

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO trace_events "
            "(id, created_at, task_id, trace_id, parent_event_id, kind, name, level, environment, session_id, "
            "status, status_message, model, provider, prompt_tokens, completion_tokens, total_tokens, cost, "
            "latency_ms, tags_json, input_ref, output_ref, redaction_status, summary, body, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                is_test,
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "content_hash": content_hash}


_MODEL_CALL_LANES = ("embedding", "rerank", "pii", "text")


def record_model_call(
    *,
    lane: str,
    model: str | None,
    provider: str | None,
    latency_ms: int,
    count: int | None = None,
    dimension: int | None = None,
    task_id: str | None = None,
    trace_id: str | None = None,
    is_test: int = 0,
) -> str | None:
    """Best-effort observability trace for a model USE that B2/B4 do not already record -- the
    embedder, reranker, and PII lanes. Writes ``kind='model_call'`` with ``name=<lane>`` so the B6
    monitor's activity feed and ``T*`` history reflect ALL model usage, not just text-gen/judge.

    ADVISORY ONLY: it stores counts + latency (never the embedded/scanned text), and a write failure
    is swallowed so observability can never break the ingest/search/dedup op that triggered it.
    Returns the trace id, or None on an unknown lane / swallowed write error.
    """
    if lane not in _MODEL_CALL_LANES:
        return None
    latency_ms = int(latency_ms)
    parts = [lane]
    if count is not None:
        parts.append(f"{count} item{'' if count == 1 else 's'}")
    if dimension is not None:
        parts.append(f"dim {dimension}")
    parts.append(f"{latency_ms}ms")
    summary = "model use: " + ", ".join(parts)
    payload = {
        "kind": "model_call",
        "name": lane,
        "model": model,
        "provider": provider,
        "latency_ms": latency_ms,
        "level": "default",
        "status": "recorded",
        "summary": summary,
    }
    clean_payload = {key: value for key, value in payload.items() if value is not None}
    assert_redacted({"summary": summary})
    validate_record("trace_event", clean_payload)
    record_id = new_id("te")
    created = now()
    content_hash = utc_text_hash({"id": record_id, **payload})

    try:
        def op(conn: Any, _attempt: int) -> None:
            conn.execute(
                "INSERT INTO trace_events "
                "(id, created_at, task_id, trace_id, kind, name, level, status, model, provider, "
                "latency_ms, redaction_status, summary, body, content_hash, is_test) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id, created, task_id, trace_id, "model_call", lane, "default", "recorded",
                    model, provider, latency_ms, "scanned_clean", summary, "", content_hash, is_test,
                ),
            )

        write_tx(op)
        return record_id
    except Exception:  # noqa: BLE001 -- observability must never break the op it traces
        return None


def add_eval_score(args: Any) -> dict[str, Any]:
    """T3 CLI wrapper: parses payload from args, delegates to write_eval_score; --test marks the row removable (is_test=1)."""
    return write_eval_score(_payload_from_args(args), is_test=1 if getattr(args, "test", False) else 0)


def write_eval_score(payload: dict[str, Any], *, is_test: int = 0) -> dict[str, Any]:
    """Validate + write one eval_scores row from an explicit payload (reused by B4/O4). ``is_test``
    marks the row removable (K7); B4/O4 rows also cascade-delete via their is_test judge trace."""
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
        if isinstance(value, bool):
            truthy = value
        elif isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
            truthy = bool(value)
        elif isinstance(value, str) and value.strip().lower() in {"true", "false", "yes", "no", "1", "0", "on", "off"}:
            truthy = value.strip().lower() in {"true", "yes", "1", "on"}
        else:
            raise KaizenDenied(
                "DENIED_SCORE_VALUE",
                {"required_action": "a boolean score requires true|false, yes|no, on|off, or 1|0"},
                exit_code=2,
            )
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
            "data_type, source, comment, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                1 if is_test else 0,
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
        conditions.append("name LIKE ? ESCAPE '\\'")
        params.append(like_pattern(query))
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    raw_limit = getattr(args, "limit", None)
    limit = int(50 if raw_limit is None else raw_limit)
    if limit < 0:
        raise KaizenDenied(
            "DENIED_LIMIT_INVALID",
            {"limit": limit, "required_action": "limit must be zero or greater"},
            exit_code=2,
        )
    params.append(limit)
    rows = fetch_all(
        "SELECT id, created_at, trace_event_id, eval_run_id, verification_id, name, value_num, value_label, "
        f"data_type, source, comment FROM eval_scores{where} ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )
    numeric = [float(r[6]) for r in rows if r[6] is not None and r[8] == "numeric"]
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
    """Renders a per-task/-trace markdown report to EXPORT_ROOT/reports, returns totals + repo-relative path + sha256; requires --task-id or --trace-id else DENIED_ID_REQUIRED. Caveat: key is unsanitized (F1 traversal)."""
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
        f"SELECT id, created_at, kind, level, status, model, total_tokens, cost, summary "
        f"FROM trace_events WHERE {column} = ? ORDER BY created_at",
        (key,),
    )
    total_tokens = sum(int(r[6] or 0) for r in rows)
    total_cost = sum(float(r[7] or 0) for r in rows)
    out_dir = EXPORT_ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_key = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(key))[:80] or "trace"
    path = out_dir / f"trace-{safe_key}-{utc_text_hash(str(key))[:12]}.md"
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
            f"tok={r[6] or 0} cost={r[7] or 0} {r[8]}"
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
