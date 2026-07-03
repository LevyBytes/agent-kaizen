from __future__ import annotations

import json
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import TOOL_VERSION
from .db_retry import ATTEMPTS, is_retryable, retry_delay, with_retry
from .db_schema import DDL, INDEXES, MIGRATION_ID, SCHEMA_VERSION, schema_manifest
from .denials import KaizenDenied
from .hashing import file_sha256, utc_text_hash
from .paths import DB_PATH, DB_ROOT, MANIFEST_ROOT, ensure_runtime_dirs, repo_relative

ensure_runtime_dirs()

import turso  # noqa: E402  # environment is routed before the native module loads


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:10]}"


def connect():
    ensure_runtime_dirs()
    last_error: Exception | None = None
    attempts_made = 0
    for attempt in range(1, ATTEMPTS + 1):
        attempts_made = attempt
        conn = None
        try:
            conn = turso.connect(str(DB_PATH))
            conn.execute("PRAGMA journal_mode = 'mvcc'").fetchone()
            return conn
        except Exception as error:
            last_error = error
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if not is_retryable(error) or attempt == ATTEMPTS:
                break
            time.sleep(retry_delay(attempt - 1))
    raise KaizenDenied(
        "DENIED_DB_CONNECT_RETRY_EXHAUSTED",
        {
            "attempts": attempts_made,
            "reason": str(last_error),
            "retryable": bool(last_error and is_retryable(last_error)),
            "required_action": "standby for user or retry later; do not rewrite schema",
        },
    ) from last_error


def initialize() -> dict[str, Any]:
    ensure_runtime_dirs()
    conn = connect()
    applied_at = now()
    try:
        for stmt in DDL:
            conn.execute(stmt)
        for stmt in INDEXES:
            conn.execute(stmt)
        # INSERT OR IGNORE instead of check-then-insert: two processes racing K1 would
        # otherwise both see no row and collide on the PRIMARY KEY (UNIQUE violations are
        # not in is_retryable(), so the loser would fail hard instead of retrying).
        manifest_hash = utc_text_hash(schema_manifest())
        conn.execute(
            "INSERT OR IGNORE INTO schema_version "
            "(id, schema_version, migration_id, applied_at, tool_version, manifest_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("current", SCHEMA_VERSION, MIGRATION_ID, applied_at, TOOL_VERSION, manifest_hash),
        )
        conn.commit()
    finally:
        conn.close()
    return schema_status()


def schema_status() -> dict[str, Any]:
    exists = DB_PATH.exists()
    if not exists:
        return {
            "db_path": str(DB_PATH),
            "exists": False,
            "expected_schema_version": SCHEMA_VERSION,
            "expected_migration_id": MIGRATION_ID,
            "schema_ok": False,
        }
    row = read_retry(
        lambda conn: conn.execute(
            "SELECT schema_version, migration_id, applied_at, tool_version, manifest_hash "
            "FROM schema_version WHERE id = ?",
            ("current",),
        ).fetchone()
    )
    if row is None:
        return {
            "db_path": str(DB_PATH),
            "exists": True,
            "expected_schema_version": SCHEMA_VERSION,
            "expected_migration_id": MIGRATION_ID,
            "schema_ok": False,
            "reason": "schema_version row missing",
        }
    expected_manifest_hash = utc_text_hash(schema_manifest())
    return {
        "db_path": str(DB_PATH),
        "exists": True,
        "schema_version": row[0],
        "migration_id": row[1],
        "applied_at": row[2],
        "tool_version": row[3],
        "manifest_hash": row[4],
        "expected_schema_version": SCHEMA_VERSION,
        "expected_migration_id": MIGRATION_ID,
        # Informational, not gating: the manifest now hashes the DDL text, so this flags
        # engine-vs-DB drift (e.g. a column added in code) that version/migration ids miss.
        "expected_manifest_hash": expected_manifest_hash,
        "manifest_match": row[4] == expected_manifest_hash,
        "schema_ok": int(row[0]) == SCHEMA_VERSION and row[1] == MIGRATION_ID,
    }


def ensure_schema() -> None:
    if not DB_PATH.exists():
        initialize()
        return
    status = schema_status()
    if not status.get("schema_ok"):
        raise KaizenDenied(
            "DENIED_SCHEMA_STATUS",
            {
                "reason": status.get("reason", "unknown or unsupported schema version"),
                "db_path": str(DB_PATH),
                "required_action": "inspect with K2; do not write until schema is repaired or migrated",
            },
        )


def write_tx(operation: Callable[[Any, int], Any]) -> Any:
    ensure_schema()
    return with_retry(connect, operation)


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    ensure_schema()
    return read_retry(lambda conn: conn.execute(sql, params).fetchone())


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    ensure_schema()
    return read_retry(lambda conn: list(conn.execute(sql, params).fetchall()))


def read_retry(operation: Callable[[Any], Any]) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        conn = connect()
        try:
            return operation(conn)
        except Exception as error:
            last_error = error
            if not is_retryable(error) or attempt == ATTEMPTS:
                break
            time.sleep(retry_delay(attempt - 1))
        finally:
            conn.close()
    raise KaizenDenied(
        "DENIED_DB_READ_RETRY_EXHAUSTED",
        {
            "attempts": ATTEMPTS,
            "reason": str(last_error),
            "retryable": bool(last_error and is_retryable(last_error)),
            "required_action": "standby for user or retry later; do not rewrite schema",
        },
    ) from last_error


def table_count(table: str) -> int:
    row = fetch_one(f"SELECT COUNT(*) FROM {table}")
    return int(row[0]) if row else 0


def backup_db() -> dict[str, Any]:
    ensure_schema()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = DB_ROOT / "backups" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for path in (DB_PATH, DB_PATH.with_name(DB_PATH.name + "-log"), DB_PATH.with_name(DB_PATH.name + "-wal")):
        if path.exists():
            target = out_dir / path.name
            shutil.copy2(path, target)
            copied.append(
                {
                    "path": repo_relative(target),
                    "sha256": file_sha256(target),
                    "bytes": target.stat().st_size,
                }
            )
    manifest = {"created_at": now(), "db_path": str(DB_PATH), "files": copied}
    manifest_path = out_dir / "backup-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"backup_dir": repo_relative(out_dir), "files": copied, "manifest": repo_relative(manifest_path)}


def export_manifest() -> dict[str, Any]:
    ensure_schema()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tables = schema_manifest()["tables"]
    counts = {table: table_count(str(table)) for table in tables}
    files = []
    for path in (DB_PATH, DB_PATH.with_name(DB_PATH.name + "-log"), DB_PATH.with_name(DB_PATH.name + "-wal")):
        if path.exists():
            files.append({"path": repo_relative(path), "sha256": file_sha256(path), "bytes": path.stat().st_size})
    manifest = {
        "created_at": now(),
        "schema": schema_status(),
        "table_counts": counts,
        "files": files,
    }
    path = MANIFEST_ROOT / f"kaizen-db-manifest-{stamp}.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"manifest_path": repo_relative(path), "manifest": manifest}
