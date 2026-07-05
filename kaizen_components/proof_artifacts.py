from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import fetch_all, fetch_one, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import file_sha256, utc_text_hash, validate_text_fields
from .paths import REPO_ROOT, assert_under, path_in_repo, read_text_file, repo_relative, resolve_user_path
from .schemas import validate_record
from .task_records import _text_arg
from .text_search import like_pattern


def add_artifact(args: Any) -> dict[str, Any]:
    raw_path = getattr(args, "path", None)
    if not raw_path:
        raise KaizenDenied("DENIED_PATH_REQUIRED", {"required_action": "resubmit with --path"}, exit_code=2)
    path = Path(raw_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    # Artifact records must stay repo-portable: repo_relative() and A5 (verify) both
    # anchor on REPO_ROOT, so a path outside it (including ../ traversal) is denied.
    try:
        path = assert_under(REPO_ROOT, path)
    except ValueError:
        raise KaizenDenied(
            "DENIED_PATH_OUTSIDE_REPO",
            {
                "path": str(path),
                "required_action": "reference a file inside the repository; copy external evidence under AI/work/ first",
            },
            exit_code=2,
        ) from None
    sha = file_sha256(path) if path.is_file() else None
    size = path.stat().st_size if path.is_file() else None
    summary = _text_arg(args, "summary", f"Artifact reference for {repo_relative(path)}.")
    body = _text_arg(args, "body", "")
    validate_text_fields({"summary": summary, "body": body})
    record_id = new_id("a")
    created = now()
    content_hash = utc_text_hash({"id": record_id, "path": str(path), "sha256": sha, "body": body})

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO artifacts "
            "(id, created_at, task_id, kind, path, sha256, bytes, summary, body, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                created,
                getattr(args, "task_id", None),
                getattr(args, "kind", None) or "file",
                repo_relative(path),
                sha,
                size,
                summary,
                body,
                content_hash,
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "path": repo_relative(path), "sha256": sha, "bytes": size}


def hash_file(args: Any) -> dict[str, Any]:
    allow_external = bool(getattr(args, "allow_external", False))
    path = resolve_user_path(
        getattr(args, "path", None),
        require_file=True,
        repo_only=not allow_external,
        allow_external_hint=not allow_external,
    )
    # External files report a sanitized origin, never an absolute machine path.
    shown = repo_relative(path) if path_in_repo(path) else f"external:{path.name}"
    return {"status": "OK", "path": shown, "sha256": file_sha256(path), "bytes": path.stat().st_size}


def list_artifacts(args: Any) -> dict[str, Any]:
    query = getattr(args, "query", None)
    if query:
        pattern = like_pattern(query)
        rows = fetch_all(
            "SELECT id, kind, path, sha256, bytes, summary, created_at FROM artifacts "
            "WHERE path LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC LIMIT ?",
            (pattern, pattern, pattern, int(getattr(args, "limit", None) or 20)),
        )
    else:
        rows = fetch_all(
            "SELECT id, kind, path, sha256, bytes, summary, created_at FROM artifacts ORDER BY created_at DESC LIMIT ?",
            (int(getattr(args, "limit", None) or 20),),
        )
    return {
        "status": "OK",
        "records": [
            {"id": r[0], "kind": r[1], "path": r[2], "sha256": r[3], "bytes": r[4], "summary": r[5], "created_at": r[6]}
            for r in rows
        ],
    }


def inspect_artifact(args: Any) -> dict[str, Any]:
    record_id = getattr(args, "id", None)
    if not record_id:
        raise KaizenDenied("DENIED_ID_REQUIRED", {"required_action": "resubmit with --id"}, exit_code=2)
    row = fetch_one(
        "SELECT id, created_at, task_id, kind, path, sha256, bytes, summary, body, content_hash "
        "FROM artifacts WHERE id = ?",
        (record_id,),
    )
    if row is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": record_id, "table": "artifacts"}, exit_code=1)
    return {
        "status": "OK",
        "record": {
            "id": row[0],
            "created_at": row[1],
            "task_id": row[2],
            "kind": row[3],
            "path": row[4],
            "sha256": row[5],
            "bytes": row[6],
            "summary": row[7],
            "body": row[8],
            "content_hash": row[9],
        },
    }


def verify_artifact_hash(args: Any) -> dict[str, Any]:
    record_id = getattr(args, "id", None)
    if not record_id:
        raise KaizenDenied("DENIED_ID_REQUIRED", {"required_action": "resubmit with --id"}, exit_code=2)
    row = fetch_one("SELECT path, sha256 FROM artifacts WHERE id = ?", (record_id,))
    if row is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": record_id, "table": "artifacts"}, exit_code=1)
    path = REPO_ROOT / row[0]
    if not path.is_file():
        raise KaizenDenied("DENIED_FILE_NOT_FOUND", {"path": str(path)}, exit_code=1)
    actual = file_sha256(path)
    return {"status": "OK", "id": record_id, "path": row[0], "expected": row[1], "actual": actual, "match": row[1] == actual}


def add_verification(args: Any) -> dict[str, Any]:
    summary = _text_arg(args, "summary", "")
    body = _text_arg(args, "body", "")
    validate_text_fields({"summary": summary, "body": body})
    record_id = new_id("qv")
    created = now()
    evidence = _json_arg(args, "evidence", [])
    findings = _json_arg(args, "findings", [])
    remedies = _json_arg(args, "remedies", [])
    artifact_ids = _json_arg(args, "artifact_ids", [])
    payload = {
        "id": record_id,
        "conclusion": getattr(args, "conclusion", None) or "NEEDS_HUMAN_DECISION",
        "summary": summary,
        "body": body,
        "evidence": evidence,
        "findings": findings,
        "remedies": remedies,
    }
    validate_record(
        "verification",
        {
            k: v
            for k, v in {
                "task_id": getattr(args, "task_id", None),
                "proof_id": getattr(args, "proof_id", None),
                "conclusion": payload["conclusion"],
                "summary": summary,
                "body": body,
                "evidence": evidence,
                "findings": findings,
                "remedies": remedies,
                "severity": getattr(args, "severity", None),
                "scope_label": getattr(args, "scope_label", None),
                "actionability": getattr(args, "actionability", None),
                "artifact_ids": artifact_ids,
            }.items()
            if v not in (None, "")
        },
    )
    content_hash = utc_text_hash(payload)

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO verification_events "
            "(id, created_at, task_id, proof_id, conclusion, evidence_locations_json, findings_json, remedies_json, "
            "severity, scope_label, actionability, summary, body, artifact_ids_json, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                created,
                getattr(args, "task_id", None),
                getattr(args, "proof_id", None),
                payload["conclusion"],
                json.dumps(evidence),
                json.dumps(findings),
                json.dumps(remedies),
                getattr(args, "severity", None),
                getattr(args, "scope_label", None),
                getattr(args, "actionability", None),
                summary,
                body,
                json.dumps(artifact_ids),
                content_hash,
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "content_hash": content_hash}


def query_verifications(args: Any) -> dict[str, Any]:
    """Q9: read verification conclusions back out of the record plane.

    Filter by --task-id, --conclusion, --severity, and/or --query (summary/body LIKE);
    with no filters it lists the most recent events. This is how a later session
    surfaces VERIFICATION_FAILED / NEEDS_HUMAN_DECISION blockers that Q1/Q2 recorded.
    """
    conditions: list[str] = []
    params: list[Any] = []
    for flag, column in (("task_id", "task_id"), ("conclusion", "conclusion"), ("severity", "severity")):
        value = getattr(args, flag, None)
        if value:
            conditions.append(f"{column} = ?")
            params.append(value)
    query = getattr(args, "query", None)
    if query:
        pattern = like_pattern(query)
        conditions.append("(summary LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
        params.extend((pattern, pattern))
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(int(getattr(args, "limit", None) or 20))
    rows = fetch_all(
        "SELECT id, created_at, task_id, proof_id, conclusion, severity, scope_label, actionability, summary "
        f"FROM verification_events{where} ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )
    return {
        "status": "OK",
        "records": [
            {
                "id": r[0],
                "created_at": r[1],
                "task_id": r[2],
                "proof_id": r[3],
                "conclusion": r[4],
                "severity": r[5],
                "scope_label": r[6],
                "actionability": r[7],
                "summary": r[8],
            }
            for r in rows
        ],
        "count": len(rows),
    }


def add_eval_case(args: Any) -> dict[str, Any]:
    title = _text_arg(args, "title", "")
    if not title:
        raise KaizenDenied("DENIED_TITLE_REQUIRED", {"required_action": "resubmit with --title"}, exit_code=2)
    summary = _text_arg(args, "summary", "")
    body = _text_arg(args, "body", "")
    validate_text_fields({"summary": summary, "body": body})
    record_id = new_id("qe")
    created = now()
    content_hash = utc_text_hash({"id": record_id, "title": title, "summary": summary, "body": body})

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO eval_cases "
            "(id, created_at, category, scope, status, title, summary, body, fixture_path, expected_json, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                created,
                getattr(args, "category", None) or "behavior",
                getattr(args, "scope", None) or "project",
                getattr(args, "status", None) or "active",
                title,
                summary,
                body,
                getattr(args, "path", None),
                _text_arg(args, "expected_json", "") or None,
                content_hash,
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "content_hash": content_hash}


def add_eval_run(args: Any) -> dict[str, Any]:
    summary = _text_arg(args, "summary", "")
    body = _text_arg(args, "body", "")
    validate_text_fields({"summary": summary, "body": body})
    record_id = new_id("qr")
    created = now()
    content_hash = utc_text_hash({"id": record_id, "summary": summary, "body": body})

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO eval_runs "
            "(id, created_at, eval_case_id, status, summary, body, artifact_ids_json, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                created,
                getattr(args, "eval_case_id", None),
                getattr(args, "status", None) or "recorded",
                summary,
                body,
                json.dumps(_json_arg(args, "artifact_ids", [])),
                content_hash,
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "content_hash": content_hash}


def add_anti_pattern(args: Any) -> dict[str, Any]:
    required = ["title", "symptom", "maintainability_harm", "trigger_evidence", "preferred_correction", "valid_exceptions", "verification", "summary"]
    values = {name: _text_arg(args, name, "") for name in required}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise KaizenDenied(
            "DENIED_REQUIRED_FIELDS",
            {"fields": missing, "required_action": "resubmit with every anti-pattern field"},
            exit_code=2,
        )
    validate_text_fields(values)
    record_id = new_id("ap")
    created = now()
    content_hash = utc_text_hash({"id": record_id, **values})

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO anti_patterns "
            "(id, created_at, scope, status, title, symptom, maintainability_harm, trigger_evidence, "
            "preferred_correction, valid_exceptions, verification, summary, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                created,
                getattr(args, "scope", None) or "project",
                getattr(args, "status", None) or "active",
                values["title"],
                values["symptom"],
                values["maintainability_harm"],
                values["trigger_evidence"],
                values["preferred_correction"],
                values["valid_exceptions"],
                values["verification"],
                values["summary"],
                content_hash,
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "content_hash": content_hash}


def query_anti_patterns(args: Any) -> dict[str, Any]:
    query = getattr(args, "query", None)
    if not query:
        raise KaizenDenied("DENIED_QUERY_REQUIRED", {"required_action": "resubmit with --query"}, exit_code=2)
    pattern = like_pattern(query)
    rows = fetch_all(
        "SELECT id, status, scope, title, summary, created_at FROM anti_patterns "
        "WHERE title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR symptom LIKE ? ESCAPE '\\' "
        "OR trigger_evidence LIKE ? ESCAPE '\\' "
        "ORDER BY created_at DESC LIMIT ?",
        (pattern, pattern, pattern, pattern, int(getattr(args, "limit", None) or 20)),
    )
    return {
        "status": "OK",
        "records": [
            {"id": r[0], "status": r[1], "scope": r[2], "title": r[3], "summary": r[4], "created_at": r[5]}
            for r in rows
        ],
    }


def inspect_quality(args: Any) -> dict[str, Any]:
    # Q7 routes by --kind; eval-run/eval-case give Q4/Q3 records a read-back path.
    kind_tables = {"verifier": "verification_events", "eval-run": "eval_runs", "eval-case": "eval_cases"}
    table = kind_tables.get(getattr(args, "kind", None) or "", "artifacts")
    record_id = getattr(args, "id", None)
    if not record_id:
        raise KaizenDenied("DENIED_ID_REQUIRED", {"required_action": "resubmit with --id"}, exit_code=2)
    row = fetch_one(f"SELECT * FROM {table} WHERE id = ?", (record_id,))
    if row is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": record_id, "table": table}, exit_code=1)
    columns = [r[1] for r in fetch_all(f"PRAGMA table_info({table})")]
    return {"status": "OK", "record": dict(zip(columns, row))}


def _json_arg(args: Any, name: str, default: Any) -> Any:
    """Read a JSON-valued flag, falling back to its ``--<name>-file`` twin.

    Mirrors ``_text_arg``: the file form exists because Windows PowerShell 5.1
    strips quotes inside inline JSON argv, so agents need a quoting-proof path."""
    raw = getattr(args, name, None)
    if raw is None or raw == "":
        file_value = getattr(args, f"{name}_file", None)
        if not file_value:
            return default
        raw = read_text_file(file_value)
    return json.loads(raw)
