"""O5 lab-dedup: the OFFLINE evidence path (cluster over STORED evidence_chunks.embedding with no
backend/network), the markdown + reports-row write, the no-mutation guarantee, and the deny paths.

Vectors are seeded directly as Turso vector32 blobs, so the offline path is exercised without loading
any embedding model. Runs on an isolated KAIZEN_REPO_ROOT plane (never the real AI/db)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import IsolatedDBTest  # noqa: E402

# get_embedding_backend() -> None: Ollama selector with no model = unconfigured, no torch import.
_UNCONFIGURED = {"KAIZEN_EMBED_BACKEND": "ollama", "KAIZEN_EMBED_MODEL": ""}


def _seed_evidence_vectors(db_path: str, vectors: list[list[float]]) -> list[str]:
    """Insert one evidence_document + one chunk per vector, with the vector stored in the SCHEMA v2
    chunk_embeddings side table (vector32 blob). Returns the chunk ids. Direct DB write so the offline
    read path can be tested without an embedding model."""
    import turso

    conn = turso.connect(db_path)
    conn.execute("PRAGMA journal_mode = 'mvcc'").fetchone()
    conn.execute(
        "INSERT INTO evidence_documents "
        "(id, created_at, origin_kind, origin_ref, backend, summary, body, content_hash, is_test) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("doc_dedup_test", "2026-07-07T00:00:00+00:00", "file", "fixture://dedup", "test",
         "dedup fixture", "", "h_doc", 1),
    )
    ids: list[str] = []
    for i, vec in enumerate(vectors):
        cid = f"ch_dedup_{i}"
        conn.execute(
            "INSERT INTO evidence_chunks "
            "(id, created_at, document_id, chunk_index, text, chunker, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid, "2026-07-07T00:00:00+00:00", "doc_dedup_test", i, f"chunk {i} text", "recursive", f"h_{i}"),
        )
        conn.execute(
            "INSERT INTO chunk_embeddings "
            "(chunk_id, embedding_model, dim, embedding, created_at, is_test) VALUES (?, ?, ?, vector32(?), ?, ?)",
            (cid, "test-embed", len(vec), json.dumps(vec), "2026-07-07T00:00:00+00:00", 1),
        )
        ids.append(cid)
    conn.commit()
    conn.close()
    return ids


def _count(db_path: str, sql: str) -> int:
    import turso

    conn = turso.connect(db_path)
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


class DedupEvidenceOfflineTest(IsolatedDBTest):
    def _db_path(self) -> str:
        rc, p = self.kz("K1")
        self.assertEqual(rc, 0, p)
        return p["schema"]["db_path"]

    def test_evidence_clusters_from_stored_vectors_no_backend(self):
        db = self._db_path()
        # two near-identical + one orthogonal -> exactly one cluster of 2
        _seed_evidence_vectors(db, [[1.0, 0.0, 0.0], [0.99, 0.01, 0.0], [0.0, 1.0, 0.0]])

        rc, p = self.kz("O5", "--kind", "evidence", "--threshold", "0.9", env=_UNCONFIGURED)
        self.assertEqual(rc, 0, p)                              # OFFLINE: no backend required
        self.assertEqual(p.get("source"), "stored-embeddings", p)
        self.assertEqual(p.get("clusters"), 1, p)
        self.assertEqual(set(p.get("top_clusters", [[]])[0]), {"ch_dedup_0", "ch_dedup_1"}, p)
        self.assertTrue(p.get("path"), p)                      # a markdown report was written
        self.assertEqual(_count(db, "SELECT COUNT(*) FROM reports WHERE report_type='dedup'"), 1, p)
        # O5 never merges or mutates: the source chunks are all still present.
        self.assertEqual(_count(db, "SELECT COUNT(*) FROM evidence_chunks"), 3, p)

    def test_evidence_no_stored_embeddings_denies_without_backend(self):
        # No seeded vectors + no backend -> the fallback needs a backend to embed from text.
        self._db_path()
        rc, p = self.kz("O5", "--kind", "evidence", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNCONFIGURED", p)

    def test_gotcha_without_backend_denies(self):
        rc, p = self.kz("O5", "--kind", "gotcha", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNCONFIGURED", p)


if __name__ == "__main__":
    unittest.main()
