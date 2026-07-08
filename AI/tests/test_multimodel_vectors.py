"""Multi-model vector store (SCHEMA v2): the chunk_embeddings side table, B7 embed-index, the rolling
reversible embedder upgrade, the v1->v2 migration, DENIED_EMBED_INDEX_ABSENT, and the K7 cascade.

The rolling-upgrade + purge tests reuse the in-process mock OpenAI embedder from test_backends_live
(a deterministic keyword vector), so E3/B3/E4 run end to end with no real model. All on isolated
KAIZEN_REPO_ROOT planes."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import IsolatedDBTest, kaizen  # noqa: E402
from test_backends_live import _OpenAIHandler  # noqa: E402  # reuse the mock OpenAI-compatible server

_DOC = "# Topics\n\nApples and oranges grow on fruit trees.\n\nQuantum links distant particles.\n"


def _read_sql(db_path: str, sql: str) -> list:
    """Run one read-only query in a SEPARATE process (turso releases the Windows file lock only on
    process exit) and return the rows as JSON."""
    script = (
        "import sys, json, turso\n"
        "conn = turso.connect(sys.argv[1])\n"
        "rows = [list(r) for r in conn.execute(sys.argv[2]).fetchall()]\n"
        "conn.close()\n"
        "print(json.dumps(rows))\n"
    )
    proc = subprocess.run([sys.executable, "-c", script, str(db_path), sql], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"read SQL failed: {proc.stderr or proc.stdout}")
    return json.loads(proc.stdout.strip() or "[]")


def _direct_sql(db_path: str, *statements: str) -> None:
    """Execute raw SQL in a separate process (deterministic lock release)."""
    script = (
        "import sys, turso\n"
        "conn = turso.connect(sys.argv[1])\n"
        "for stmt in sys.argv[2:]:\n"
        "    conn.execute(stmt)\n"
        "conn.commit(); conn.close()\n"
    )
    proc = subprocess.run([sys.executable, "-c", script, str(db_path), *statements], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"direct SQL failed: {proc.stderr or proc.stdout}")


# ----------------------------------------------------------------------------------------------------
# v1 -> v2 migration
# ----------------------------------------------------------------------------------------------------
# A hand-built pre-v2 DB: inline evidence_chunks.embedding/embedding_model + a v1 schema_version row.
# The single argv-passed vector keeps the builder shell-safe.
_BUILD_V1 = r'''
import json, sys, turso
dbp, vec = sys.argv[1], sys.argv[2]
conn = turso.connect(dbp)
conn.execute("PRAGMA journal_mode = 'mvcc'").fetchone()
conn.execute("CREATE TABLE schema_version (id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, "
             "migration_id TEXT NOT NULL, applied_at TEXT NOT NULL, tool_version TEXT NOT NULL, manifest_hash TEXT)")
conn.execute("INSERT INTO schema_version VALUES ('current', 1, 'kaizen-system-foundation-v1', '2026-01-01', '0.0', 'oldhash')")
conn.execute("CREATE TABLE evidence_documents (id TEXT PRIMARY KEY, created_at TEXT, source_lock_id TEXT, "
             "origin_kind TEXT, origin_ref TEXT, backend TEXT, summary TEXT, body TEXT, content_hash TEXT, is_test INTEGER DEFAULT 0)")
conn.execute("INSERT INTO evidence_documents VALUES ('doc1', '2026-01-01', NULL, 'file', 'x', 'native', 's', '', 'h', 1)")
conn.execute("CREATE TABLE evidence_chunks (id TEXT PRIMARY KEY, created_at TEXT, document_id TEXT, "
             "source_lock_id TEXT, chunk_index INTEGER, text TEXT NOT NULL, start_index INTEGER, end_index INTEGER, "
             "token_count INTEGER, context TEXT, chunker TEXT NOT NULL, backend TEXT, embedding BLOB, "
             "embedding_model TEXT, neighbor_prev_id TEXT, neighbor_next_id TEXT, content_hash TEXT NOT NULL)")
conn.execute("INSERT INTO evidence_chunks (id, created_at, document_id, chunk_index, text, chunker, embedding, embedding_model, content_hash) "
             "VALUES ('ch0', '2026-01-01', 'doc1', 0, 'alpha', 'recursive', vector32(?), 'granite-311m', 'h0')", (vec,))
conn.execute("INSERT INTO evidence_chunks (id, created_at, document_id, chunk_index, text, chunker, content_hash) "
             "VALUES ('ch1', '2026-01-01', 'doc1', 1, 'bravo', 'recursive', 'h1')")
conn.commit(); conn.close()
'''


class MigrationV1ToV2Test(unittest.TestCase):
    """K1 migrates a pre-v2 DB: backfills the inline embedding into chunk_embeddings, seeds the active
    model, drops the inline columns, stamps v2 -- idempotently and without data loss."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-mig-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.db = self.root / "AI" / "db" / "kaizen.db"
        self.db.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [sys.executable, "-c", _BUILD_V1, str(self.db), json.dumps([1.0, 0.0, 0.5, 0.25])],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_migration_backfills_and_drops_and_is_idempotent(self):
        rc, p = kaizen(self.root, "K1")
        self.assertEqual(rc, 0, p)
        # Version number stays v1 (owner freeze): the inline -> chunk_embeddings move is a data
        # migration, not a numbered bump. On a pre-existing DB the new additive tables read as
        # manifest drift until an explicit restamp (a plain K1 never silently repairs it).
        self.assertEqual(p["schema"]["schema_version"], 1, p)
        self.assertTrue(p["schema"]["schema_ok"], p)
        self.assertFalse(p["schema"]["manifest_match"], p)

        cols = [r[1] for r in _read_sql(self.db, "PRAGMA table_info(evidence_chunks)")]
        self.assertNotIn("embedding", cols)
        self.assertNotIn("embedding_model", cols)

        che = _read_sql(self.db, "SELECT chunk_id, embedding_model, dim, is_test FROM chunk_embeddings")
        self.assertEqual(che, [["ch0", "granite-311m", 4, 1]], che)  # only the embedded chunk; is_test mirrors doc

        active = _read_sql(self.db, "SELECT value FROM db_settings WHERE key='active_embedding_model'")
        self.assertEqual(active, [["granite-311m"]], active)

        # text (source of truth) is retained for BOTH chunks
        n_chunks = _read_sql(self.db, "SELECT COUNT(*) FROM evidence_chunks")[0][0]
        self.assertEqual(n_chunks, 2, n_chunks)

        # reconcile the benign additive-DDL drift explicitly (no version bump)
        rc, pr = kaizen(self.root, "K1", "--restamp-manifest")
        self.assertEqual(rc, 0, pr)

        # re-run K1: no duplicate backfill, still v1, now matching
        rc, p2 = kaizen(self.root, "K1")
        self.assertEqual(rc, 0, p2)
        self.assertEqual(p2["schema"]["schema_version"], 1, p2)
        self.assertTrue(p2["schema"]["manifest_match"], p2)
        self.assertEqual(_read_sql(self.db, "SELECT COUNT(*) FROM chunk_embeddings")[0][0], 1)


# ----------------------------------------------------------------------------------------------------
# Rolling, reversible upgrade + B7 lifecycle (mock embedder)
# ----------------------------------------------------------------------------------------------------
class _MockEmbedTest(IsolatedDBTest):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
        base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.db = self.root / "AI" / "db" / "kaizen.db"

    def _env(self, model: str) -> dict:
        return {"KAIZEN_EMBED_BACKEND": "ollama", "KAIZEN_EMBED_MODEL": model,
                "KAIZEN_EMBED_BASE_URL": f"http://127.0.0.1:{self.server.server_address[1]}/v1"}

    def _ingest_and_index(self, model: str) -> str:
        doc = self.root / "doc.md"
        doc.write_text(_DOC, encoding="utf-8")
        rc, ing = self.kz("E1", "--path", str(doc), env=self._env(model))
        self.assertEqual(rc, 0, ing)
        rc, ch = self.kz("E3", "--id", ing["id"], env=self._env(model))  # recursive; embeds into chunk_embeddings
        self.assertEqual(rc, 0, ch)
        self.assertTrue(ch.get("embedded"), ch)
        return ing["id"]


class RollingUpgradeTest(_MockEmbedTest):
    def test_rolling_upgrade_activate_rollback_prune(self):
        self._ingest_and_index("embed-a")
        total = _read_sql(self.db, "SELECT COUNT(*) FROM evidence_chunks")[0][0]

        # B7 --list shows one model, not yet active
        rc, lst = self.kz("B7", "--list")
        self.assertEqual(rc, 0, lst)
        self.assertEqual([m["model"] for m in lst["models"]], ["embed-a"], lst)
        self.assertFalse(lst["models"][0]["is_active"], lst)

        # activate embed-a (full coverage) -> becomes the active retrieval index
        rc, act = self.kz("B7", "--activate", "--model", "embed-a")
        self.assertEqual(rc, 0, act)
        self.assertEqual(act["active_embedding_model"], "embed-a", act)
        self.assertEqual(act["indexed_chunks"], total, act)

        # E4 --semantic serves the active index
        rc, q = self.kz("E4", "--query", "apples", "--semantic", env=self._env("embed-a"))
        self.assertEqual(rc, 0, q)
        self.assertEqual(q.get("mode"), "semantic", q)
        self.assertEqual(q.get("embedding_model"), "embed-a", q)

        # pre-stage a NEW model's index while embed-a keeps serving (non-blocking)
        rc, b3 = self.kz("B3", "--model", "embed-b", env=self._env("embed-a"))
        self.assertEqual(rc, 0, b3)
        self.assertEqual(b3["reembedded"], total, b3)
        rc, lst = self.kz("B7", "--list")
        self.assertEqual({m["model"] for m in lst["models"]}, {"embed-a", "embed-b"}, lst)

        # flip to embed-b; keep=2 retains both (rollback still possible)
        rc, act = self.kz("B7", "--activate", "--model", "embed-b")
        self.assertEqual(rc, 0, act)
        self.assertEqual(act["pruned"], [], act)
        rc, q = self.kz("E4", "--query", "apples", "--semantic", env=self._env("embed-b"))
        self.assertEqual(q.get("embedding_model"), "embed-b", q)

        # ROLLBACK: activate embed-a again -> instant, no re-embed
        rc, act = self.kz("B7", "--activate", "--model", "embed-a")
        self.assertEqual(rc, 0, act)
        self.assertEqual(act["active_embedding_model"], "embed-a", act)

        # cannot prune the active model
        rc, denied = self.kz("B7", "--prune", "--model", "embed-a")
        self.assertEqual(rc, 2, denied)
        self.assertEqual(denied.get("code"), "DENIED_PRUNE_ACTIVE", denied)

        # prune the previous (embed-b) -> only embed-a remains
        rc, pr = self.kz("B7", "--prune", "--previous")
        self.assertEqual(rc, 0, pr)
        self.assertEqual(pr["pruned"], ["embed-b"], pr)
        rc, lst = self.kz("B7", "--list")
        self.assertEqual([m["model"] for m in lst["models"]], ["embed-a"], lst)

    def test_activate_incomplete_index_denies(self):
        self._ingest_and_index("embed-a")
        # embed-b indexes nothing -> activation must refuse (would silently drop chunks from retrieval)
        rc, denied = self.kz("B7", "--activate", "--model", "embed-b")
        self.assertEqual(rc, 2, denied)
        self.assertEqual(denied.get("code"), "DENIED_EMBED_INDEX_ABSENT", denied)

    def test_retention_auto_prunes_beyond_keep(self):
        self._ingest_and_index("embed-a")
        self.kz("B7", "--activate", "--model", "embed-a")
        self.kz("B3", "--model", "embed-b", env=self._env("embed-a"))
        # KEEP=1 -> activating embed-b prunes embed-a (active only)
        rc, act = self.kz("B7", "--activate", "--model", "embed-b", env={"KAIZEN_EMBED_KEEP": "1"})
        self.assertEqual(rc, 0, act)
        self.assertEqual(act["pruned"], ["embed-a"], act)
        rc, lst = self.kz("B7", "--list")
        self.assertEqual([m["model"] for m in lst["models"]], ["embed-b"], lst)


class ChunkEmbeddingsPurgeTest(_MockEmbedTest):
    def test_k7_purges_chunk_embeddings_but_keeps_db_settings(self):
        # is_test doc -> its chunk_embeddings rows inherit is_test and must purge; db_settings (config) survives.
        doc = self.root / "doc.md"
        doc.write_text(_DOC, encoding="utf-8")
        rc, ing = self.kz("E1", "--path", str(doc), "--test", env=self._env("embed-a"))
        self.assertEqual(rc, 0, ing)
        rc, ch = self.kz("E3", "--id", ing["id"], "--test", env=self._env("embed-a"))
        self.assertEqual(rc, 0, ch)
        self.kz("B7", "--activate", "--model", "embed-a")

        self.assertGreaterEqual(_read_sql(self.db, "SELECT COUNT(*) FROM chunk_embeddings")[0][0], 1)
        self.assertEqual(_read_sql(self.db, "SELECT value FROM db_settings WHERE key='active_embedding_model'"),
                         [["embed-a"]])

        rc, purge = self.kz("K7")
        self.assertEqual(rc, 0, purge)
        self.assertEqual(_read_sql(self.db, "SELECT COUNT(*) FROM chunk_embeddings")[0][0], 0)  # cascade removed them
        self.assertEqual(_read_sql(self.db, "SELECT COUNT(*) FROM evidence_chunks")[0][0], 0)
        # db_settings is config, excluded from purge
        self.assertEqual(_read_sql(self.db, "SELECT value FROM db_settings WHERE key='active_embedding_model'"),
                         [["embed-a"]])


class EmbedIndexAbsentTest(IsolatedDBTest):
    def test_activate_on_empty_corpus_records_preference(self):
        # Empty corpus: full coverage is vacuous, so B7 --activate records the model as a preference.
        rc, act = self.kz("B7", "--activate", "--model", "future-model")
        self.assertEqual(rc, 0, act)
        self.assertEqual(act.get("active_embedding_model"), "future-model", act)
        self.assertEqual(act.get("total_chunks"), 0, act)
        rc, lst = self.kz("B7", "--list")
        self.assertEqual(lst.get("active_embedding_model"), "future-model", lst)

    def test_semantic_denies_when_active_model_unindexed(self):
        # chunks exist (no backend at chunk time) but no index for the active model -> deny, before any network.
        doc = self.root / "doc.md"
        doc.write_text(_DOC, encoding="utf-8")
        unconf = {"KAIZEN_EMBED_BACKEND": "ollama", "KAIZEN_EMBED_MODEL": ""}
        rc, ing = self.kz("E1", "--path", str(doc), env=unconf)
        self.assertEqual(rc, 0, ing)
        rc, _ = self.kz("E3", "--id", ing["id"], env=unconf)
        self.assertEqual(rc, 0)

        # configured (but never contacted: the index-absent check precedes the query embed)
        cfg = {"KAIZEN_EMBED_BACKEND": "ollama", "KAIZEN_EMBED_MODEL": "some-model",
               "KAIZEN_EMBED_BASE_URL": "http://127.0.0.1:6/v1"}
        rc, denied = self.kz("E4", "--query", "apples", "--semantic", env=cfg)
        self.assertEqual(rc, 2, denied)
        self.assertEqual(denied.get("code"), "DENIED_EMBED_INDEX_ABSENT", denied)
        self.assertEqual(denied.get("active_model"), "some-model", denied)


if __name__ == "__main__":
    unittest.main()
