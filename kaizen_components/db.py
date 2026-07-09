from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import TOOL_VERSION
from .db_retry import ATTEMPTS, is_retryable, retry_delay, with_retry
from .db_schema import (
    ADDITIVE_COLUMNS,
    DDL,
    FTS_INDEX_SQL,
    INDEXES,
    MIGRATION_ID,
    REFERENCES,
    SCHEMA_VERSION,
    TEST_COUNT_SQL,
    TEST_PURGE_SQL,
    schema_manifest,
)
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


def _apply_additive_columns(conn) -> None:
    """Add benign additive columns (is_test) to every root record table. Idempotent: skips a column
    already present via PRAGMA table_info. This is the sole source of is_test for most tables (it is
    NOT in their CREATE DDL, so ddl_sha256 / the manifest is unchanged); a fresh DB gets the column from
    this ALTER right after the CREATE, an existing DB from this ALTER on the next K1."""
    for table, column, coldef in ADDITIVE_COLUMNS:
        try:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except Exception:  # noqa: BLE001 -- table missing/unreadable -> the CREATE DDL handles it
            continue
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")


def purge_test_records() -> dict[str, Any]:
    """K7: delete every is_test=1 record (roots + their descendants) from the real DB. Safe and
    deterministic -- only rows explicitly flagged is_test=1 and their children are removed, in one
    transaction. Counts are captured before the deletes so the report shows what was purged."""
    ensure_schema()

    def op(conn, _attempt):
        counts = {table: conn.execute(sql).fetchone()[0] for table, sql in TEST_COUNT_SQL.items()}
        for stmt in TEST_PURGE_SQL:
            conn.execute(stmt)
        return counts

    counts = with_retry(connect, op)
    return {"status": "OK", "message": "Purged is_test records.", "purged": counts, "total": sum(counts.values())}


def _stored_schema_version(conn) -> int | None:
    """The schema_version integer stamped in the DB, or None on a brand-new DB (table/row absent)."""
    try:
        row = conn.execute("SELECT schema_version FROM schema_version WHERE id = 'current'").fetchone()
    except Exception:  # noqa: BLE001 -- table not created yet on a fresh DB
        return None
    return int(row[0]) if row and row[0] is not None else None


def _seed_active_model_if_absent(conn) -> None:
    """Record the active embedding model = the dominant model already in chunk_embeddings, when the
    setting is unset. No-op on a fresh/empty DB (E4 then falls back to the configured backend model)."""
    existing = conn.execute("SELECT value FROM db_settings WHERE key = 'active_embedding_model'").fetchone()
    if existing and existing[0]:
        return
    dominant = conn.execute(
        "SELECT embedding_model FROM chunk_embeddings GROUP BY embedding_model "
        "ORDER BY COUNT(*) DESC, embedding_model LIMIT 1"
    ).fetchone()
    if dominant and dominant[0]:
        conn.execute(
            "INSERT OR REPLACE INTO db_settings (key, value, updated_at) VALUES ('active_embedding_model', ?, ?)",
            (dominant[0], now()),
        )


def _migrate(conn) -> None:
    """Idempotent forward migrations, run inside K1 after the CREATE/ALTER/index pass.

    v1 -> v2: move the inline evidence_chunks.embedding/embedding_model into the normalized
    chunk_embeddings side table, seed active_embedding_model, then DROP the two inline columns. Runs
    whenever those columns are still present (so a partial or hand-built pre-v2 DB converges); the
    guards make a re-run a no-op. No data is at risk -- evidence_chunks.text is the source of truth and
    the embedding index is rebuildable from it (B3)."""
    inline_cols = {r[1] for r in conn.execute("PRAGMA table_info(evidence_chunks)").fetchall()}
    if "embedding" in inline_cols and "embedding_model" in inline_cols:
        # Backfill exact vector32 blobs (raw bytes, no float round-trip); dim from vector_extract length.
        rows = conn.execute(
            "SELECT ec.id, ec.embedding_model, ec.embedding, vector_extract(ec.embedding), ec.created_at, "
            "COALESCE(ed.is_test, 0) FROM evidence_chunks ec "
            "LEFT JOIN evidence_documents ed ON ec.document_id = ed.id "
            "WHERE ec.embedding IS NOT NULL AND ec.embedding_model IS NOT NULL"
        ).fetchall()
        for cid, emodel, blob, extracted, created, is_test in rows:
            try:
                dim = len(json.loads(extracted)) if extracted else 0
            except Exception:  # noqa: BLE001 -- a corrupt extract falls back to dim 0 (still queryable-by-model)
                dim = 0
            conn.execute(
                "INSERT OR IGNORE INTO chunk_embeddings "
                "(chunk_id, embedding_model, dim, embedding, created_at, is_test) VALUES (?, ?, ?, ?, ?, ?)",
                (cid, emodel, dim, blob, created or now(), int(is_test or 0)),
            )
        _seed_active_model_if_absent(conn)
        conn.execute("ALTER TABLE evidence_chunks DROP COLUMN embedding")
        conn.execute("ALTER TABLE evidence_chunks DROP COLUMN embedding_model")
    else:
        _seed_active_model_if_absent(conn)


def initialize() -> dict[str, Any]:
    ensure_runtime_dirs()
    conn = connect()
    applied_at = now()
    try:
        prior_version = _stored_schema_version(conn)  # read BEFORE the DDL creates the table on a fresh DB
        for stmt in DDL:
            conn.execute(stmt)
        _apply_additive_columns(conn)  # ALTER existing tables that CREATE IF NOT EXISTS cannot touch
        for stmt in INDEXES:
            conn.execute(stmt)
        # Opt-in experimental FTS index, created here (K1) rather than on the E4 query hot
        # path. Best-effort: the Tantivy engine is gated in some builds, so never fail K1.
        if os.environ.get("KAIZEN_TURSO_FTS") == "1":
            try:
                conn.execute(FTS_INDEX_SQL)
            except Exception:
                pass
        _migrate(conn)  # idempotent forward migrations (v1 -> v2 inline-embedding -> side table)
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
        # Bring an older stamped row current AFTER a forward migration ran. Scoped to a real version
        # bump so a plain K1 never silently repairs manifest drift -- that stays gated behind
        # K1 --restamp-manifest (see restamp_manifest / the DENIED_SCHEMA_DRIFT write gate).
        if prior_version is not None and prior_version < SCHEMA_VERSION:
            conn.execute(
                "UPDATE schema_version SET schema_version = ?, migration_id = ?, applied_at = ?, "
                "tool_version = ?, manifest_hash = ? WHERE id = 'current'",
                (SCHEMA_VERSION, MIGRATION_ID, applied_at, TOOL_VERSION, manifest_hash),
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
        # Gating on writes: the manifest hashes the DDL text, so this flags engine-vs-DB
        # drift (e.g. a column added in code) that version/migration ids miss. A benign
        # additive engine update is reconciled with K1 --restamp-manifest.
        "expected_manifest_hash": expected_manifest_hash,
        "manifest_match": row[4] == expected_manifest_hash,
        "schema_ok": int(row[0]) == SCHEMA_VERSION and row[1] == MIGRATION_ID,
    }


def get_active_embedding_model(default: str | None = None) -> str | None:
    """The active embedding model recorded in db_settings (E4/O5 rank against this model's index),
    or ``default`` when unset. Stored fact, not an env guess: it survives across processes and
    embedder-config changes, so retrieval always targets the index the operator activated."""
    row = fetch_one("SELECT value FROM db_settings WHERE key = 'active_embedding_model'")
    return row[0] if row and row[0] else default


def set_active_embedding_model(model: str) -> None:
    """Point retrieval at ``model``'s index (B3 --activate / B7 --activate). Upsert on the kv row."""
    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO db_settings (key, value, updated_at) VALUES ('active_embedding_model', ?, ?)",
            (model, now()),
        )

    write_tx(op)


def get_setting(key: str, default: str | None = None) -> str | None:
    """Read a durable key/value setting (db_settings), or ``default`` when unset. Generic
    accessor for stored facts like the pinned MCP candidate (``active_comfy_mcp``)."""
    row = fetch_one("SELECT value FROM db_settings WHERE key = ?", (key,))
    return row[0] if row and row[0] else default


def set_setting(key: str, value: str) -> None:
    """Upsert a durable key/value setting (db_settings)."""
    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO db_settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now()),
        )

    write_tx(op)


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
    # Fail closed on DDL drift: the stored manifest hash no longer matches the engine's DDL,
    # so a table shape changed without a MIGRATION_ID bump. Reads stay open; writes stop.
    if status.get("manifest_match") is False:
        raise KaizenDenied(
            "DENIED_SCHEMA_DRIFT",
            {
                "db_path": str(DB_PATH),
                "stored_manifest_hash": status.get("manifest_hash"),
                "expected_manifest_hash": status.get("expected_manifest_hash"),
                "required_action": (
                    "inspect with K2; if this is a known additive engine update (not a real "
                    "migration), reconcile with: python kaizen.py K1 --restamp-manifest"
                ),
            },
        )


def restamp_manifest() -> dict[str, Any]:
    """Reconcile the stored DDL manifest hash to the current engine.

    Only allowed when schema_ok (version + migration id still match): that scopes this to
    benign additive DDL drift and refuses to paper over a real, unmigrated schema change.
    """
    status = schema_status()
    if not status.get("exists"):
        raise KaizenDenied(
            "DENIED_SCHEMA_STATUS",
            {"db_path": str(DB_PATH), "required_action": "run K1 to initialize the DB first"},
            exit_code=2,
        )
    if not status.get("schema_ok"):
        raise KaizenDenied(
            "DENIED_SCHEMA_STATUS",
            {
                "db_path": str(DB_PATH),
                "reason": status.get("reason", "schema_version/migration_id mismatch is a real migration, not drift"),
                "required_action": "this is a version/migration change, not additive drift; migrate the schema instead of restamping",
            },
            exit_code=2,
        )
    old_hash = status.get("manifest_hash")
    new_hash = status.get("expected_manifest_hash")
    if old_hash == new_hash:
        return {"restamped": False, "manifest_hash": new_hash, "note": "manifest already matches; nothing to do"}
    with_retry(
        connect,
        lambda conn, _a: conn.execute(
            "UPDATE schema_version SET manifest_hash = ?, tool_version = ? WHERE id = ?",
            (new_hash, TOOL_VERSION, "current"),
        ),
    )
    return {"restamped": True, "old_manifest_hash": old_hash, "manifest_hash": new_hash}


def integrity_scan() -> dict[str, Any]:
    """Read-only cross-table reference scan (the schema ships no FK constraints).

    For each child (table, column) -> parent (table, column) in REFERENCES, count non-NULL
    child values with no matching parent row. NULL child references are valid (optional).
    """
    relationships: list[dict[str, Any]] = []
    total_orphans = 0
    for child_table, child_col, parent_table, parent_col in REFERENCES:
        rows = read_retry(
            lambda conn, ct=child_table, cc=child_col, pt=parent_table, pc=parent_col: list(
                conn.execute(
                    # rowid, not a literal "id" column: child tables need not be id-keyed
                    # (e.g. generative_run_routes is keyed by run_id), and every rowid table works.
                    f"SELECT ct.{cc}, ct.rowid FROM {ct} AS ct "
                    f"LEFT JOIN {pt} AS pt ON ct.{cc} = pt.{pc} "
                    f"WHERE ct.{cc} IS NOT NULL AND pt.{pc} IS NULL "
                    f"ORDER BY ct.rowid LIMIT 6"
                ).fetchall()
            )
        )
        count = len(rows)
        total_orphans += count
        entry: dict[str, Any] = {
            "child": f"{child_table}.{child_col}",
            "parent": f"{parent_table}.{parent_col}",
            "orphans": count,
        }
        if rows:
            entry["sample"] = [{"child_id": r[1], "missing_ref": r[0]} for r in rows[:5]]
            if count > 5:
                entry["sample_truncated"] = True
        relationships.append(entry)
    # Child-leak invariant: a terminal-SUCCESS agent run that still has an open child. Not a
    # REFERENCES orphan (which checks id existence); this is a structural consistency fault the
    # Q2/W2 completion gate prevents live, but crash-path/out-of-order-replay/pre-gate data can
    # still commit. Recomputed from agent_events. Lazy import avoids a db <-> agent_runs cycle.
    from .agent_runs import find_child_leaks

    child_leaks = find_child_leaks()
    total_violations = int(child_leaks["violations"])
    return {
        "ok": total_orphans == 0 and total_violations == 0,
        "total_orphans": total_orphans,
        "relationships_checked": len(REFERENCES),
        # Only surface relationships that actually have orphans, to keep the payload compact.
        "orphaned_relationships": [r for r in relationships if r["orphans"]],
        "invariants": [child_leaks] if total_violations else [],
        "total_invariant_violations": total_violations,
        "note": "read-only reference scan; NULL references are valid. The schema ships no FK constraints.",
    }


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
