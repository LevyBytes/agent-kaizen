"""Offline coverage for the is_test marker + K7 purge-test (no model load). --test flags a record as
removable; K7 deletes ONLY is_test=1 rows (roots + their descendants), leaves normal records alone, and
is idempotent. Two layers: (1) a real op (B5, regex-only) threads --test end to end; (2) a table-agnostic
sweep proves K7's purge cascade covers EVERY root record table plus a revision child."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root -> import db_schema
from _harness import IsolatedDBTest  # noqa: E402
from kaizen_components.db_schema import TEST_ROOT_TABLES  # noqa: E402


def _insert_generic(conn, table: str, tag: str, is_test: int) -> str:
    """Insert a minimal valid row into `table` (satisfying every NOT-NULL-without-default column with a
    placeholder) plus is_test. Returns the row id. SCHEMA v1 has no CHECK/FK constraints, so placeholders
    are accepted -- this lets the test cover every table without per-table column knowledge."""
    names, vals = [], []
    row_id = f"{table}_{tag}"
    for _cid, name, ctype, notnull, dflt, pk in conn.execute(f"PRAGMA table_info({table})").fetchall():
        if name == "is_test":
            names.append(name); vals.append(is_test); continue
        # include PKs too: "id TEXT PRIMARY KEY" reports notnull=0, but we must supply it (unique + FK link)
        if not (pk or (notnull and dflt is None)):
            continue  # nullable non-PK with a default/none -> omit
        names.append(name)
        upper = (ctype or "").upper()
        if name == "id":
            vals.append(row_id)
        elif name in ("created_at", "updated_at", "retrieved_at"):
            vals.append("2026-07-07T00:00:00+00:00")
        elif "INT" in upper:
            vals.append(0)
        elif any(t in upper for t in ("REAL", "FLOA", "DOUB")):
            vals.append(0.0)
        else:
            vals.append("x")
    conn.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in vals)})", vals)
    return row_id


def _seed_all_tables(db_path: str) -> None:
    """Seed one is_test=0 (keep) + one is_test=1 (drop) row into every root table, plus a revision child
    of the is_test gotcha. Scoped so the connection is closed+released before any CLI subprocess runs."""
    import turso

    conn = turso.connect(db_path)
    conn.execute("PRAGMA journal_mode = 'mvcc'").fetchone()
    for table in TEST_ROOT_TABLES:
        _insert_generic(conn, table, "keep", 0)
        _insert_generic(conn, table, "drop", 1)
    conn.execute(
        "INSERT INTO gotcha_revision (id, gotcha_id, created_at, revision_number, summary, body, status, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("gr_drop", "gotcha_drop", "2026-07-07T00:00:00+00:00", 1, "s", "b", "active", "h"),
    )
    conn.commit()
    conn.close()


def _table_counts(db_path: str) -> dict:
    """Read {table: (is_test_count, keep_count)} + the revision-orphan flag; connection scoped + closed."""
    import turso

    conn = turso.connect(db_path)
    try:
        counts = {
            table: (
                conn.execute(f"SELECT COUNT(*) FROM {table} WHERE is_test = 1").fetchone()[0],
                conn.execute(f"SELECT COUNT(*) FROM {table} WHERE is_test = 0").fetchone()[0],
            )
            for table in TEST_ROOT_TABLES
        }
        counts["_gr_orphan"] = conn.execute("SELECT COUNT(*) FROM gotcha_revision WHERE id = 'gr_drop'").fetchone()[0]
        return counts
    finally:
        conn.close()


class PurgeTestOpTest(IsolatedDBTest):
    """K7 isolation and cascade coverage proving only is_test root records are purged across every governed table."""
    def test_k7_purges_only_test_flagged_records(self):
        rc, keep = self.kz("B5", "--prompt", "normal record alice@example.com")   # is_test=0 -> kept
        self.assertEqual(rc, 0, keep)
        rc, marked = self.kz("B5", "--prompt", "throwaway bob@example.com", "--test")  # is_test=1 -> purgeable
        self.assertEqual(rc, 0, marked)

        rc, purged = self.kz("K7")
        self.assertEqual(rc, 0, purged)
        self.assertEqual(purged.get("purged", {}).get("pii_scan"), 1, purged)     # only the flagged row
        self.assertEqual(purged.get("total"), 1, purged)                          # normal record NOT counted

        rc, again = self.kz("K7")                                                 # idempotent
        self.assertEqual(rc, 0, again)
        self.assertEqual(again.get("total"), 0, again)

    def test_k7_on_empty_db_is_zero(self):
        rc, p = self.kz("K7")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("total"), 0, p)

    def test_k7_counts_child_rows_marked_test_even_when_parents_are_not(self):
        import gc
        import turso

        db_path = str(self.k1_payload["schema"]["db_path"])
        conn = turso.connect(db_path)
        try:
            created = "2026-07-16T00:00:00+00:00"
            conn.execute(
                "INSERT INTO evidence_documents(id,created_at,origin_kind,origin_ref,backend,summary,body,content_hash,is_test) VALUES (?,?,?,?,?,?,?,?,0)",
                ("doc-keep", created, "file", "fixture://keep", "fixture", "keep", "", "doc-hash"),
            )
            conn.execute(
                "INSERT INTO evidence_chunks(id,created_at,document_id,chunk_index,text,chunker,content_hash) VALUES (?,?,?,?,?,?,?)",
                ("chunk-keep", created, "doc-keep", 0, "text", "recursive", "chunk-hash"),
            )
            conn.execute(
                "INSERT INTO chunk_embeddings(chunk_id,embedding_model,dim,embedding,created_at,is_test) VALUES (?,?,?,vector32(?),?,1)",
                ("chunk-keep", "fixture", 1, "[1.0]", created),
            )
            conn.execute(
                "INSERT INTO generative_runs(id,created_at,backend,template,workflow_hash,status,summary,content_hash,is_test) VALUES (?,?,?,?,?,?,?,?,0)",
                ("run-keep", created, "comfyui", "fixture", "workflow", "complete", "keep", "run-hash"),
            )
            conn.execute(
                "INSERT INTO generative_run_routes(run_id,created_at,route,is_test) VALUES (?,?,?,1)",
                ("run-keep", created, "api"),
            )
            conn.commit()
        finally:
            conn.close()
        del conn
        gc.collect()

        rc, purged = self.kz("K7")
        self.assertEqual(rc, 0, purged)
        self.assertEqual(purged["purged"]["chunk_embeddings"], 1, purged)
        self.assertEqual(purged["purged"]["generative_run_routes"], 1, purged)


class ComprehensivePurgeTest(IsolatedDBTest):
    """Every root record table an op writes carries is_test; K7 must purge each one (+ a revision child)."""

    def test_k7_covers_every_root_table_and_a_revision_child(self):
        rc, k1 = self.kz("K1")
        self.assertEqual(rc, 0, k1)
        db = k1["schema"]["db_path"]

        _seed_all_tables(db)  # connection closed before K7 runs (avoids a Windows file lock)

        rc, purged = self.kz("K7")
        self.assertEqual(rc, 0, purged)
        self.assertEqual(purged.get("total"), len(TEST_ROOT_TABLES), purged)  # one is_test row per root table

        counts = _table_counts(db)
        for table in TEST_ROOT_TABLES:
            n_test, n_keep = counts[table]
            self.assertEqual(n_test, 0, f"{table}: an is_test row survived K7")
            self.assertGreaterEqual(n_keep, 1, f"{table}: a non-test row was wrongly purged")
        self.assertEqual(counts["_gr_orphan"], 0, "revision child of an is_test gotcha was not cascade-deleted")

        rc, again = self.kz("K7")  # idempotent
        self.assertEqual(again.get("total"), 0, again)


if __name__ == "__main__":
    unittest.main()
