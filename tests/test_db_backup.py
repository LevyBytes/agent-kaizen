"""Crash-consistent database backup and integrity-scan regressions."""

from __future__ import annotations

import json
import gc
import unittest
from pathlib import Path

import turso

from _harness import IsolatedDBTest


class DatabaseBackupTest(IsolatedDBTest):
    def test_vacuum_snapshot_is_standalone_readable_and_manifest_lists_only_snapshot(self) -> None:
        db_path = Path(self.k1_payload["schema"]["db_path"])
        live = turso.connect(str(db_path))
        try:
            live.execute(
                "INSERT INTO db_settings(key,value,updated_at) VALUES (?,?,?)",
                ("backup_probe", "present", "2026-07-16T00:00:00+00:00"),
            )
            live.commit()
        finally:
            live.close()
        del live
        gc.collect()
        rc, result = self.kz("K3")
        self.assertEqual(rc, 0, result)

        backup = self.root / result["files"][0]["path"]
        manifest_path = self.root / result["manifest"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(manifest["files"], result["files"])
        self.assertEqual(Path(manifest["files"][0]["path"]).name, db_path.name)
        self.assertNotIn("-wal", json.dumps(manifest))
        self.assertNotIn("-log", json.dumps(manifest))
        snapshot = turso.connect(str(backup))
        try:
            self.assertEqual(snapshot.execute("SELECT value FROM db_settings WHERE key='backup_probe'").fetchone()[0], "present")
            self.assertIsNotNone(snapshot.execute("SELECT schema_version FROM schema_version WHERE id='current'").fetchone())
        finally:
            snapshot.close()


class DatabaseIntegrityTest(IsolatedDBTest):
    def test_chunk_embedding_orphan_is_reported_by_integrity_scan(self) -> None:
        db_path = str(self.k1_payload["schema"]["db_path"])
        conn = turso.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO chunk_embeddings(chunk_id,embedding_model,dim,embedding,created_at,is_test) VALUES (?,?,?,vector32(?),?,?)",
                ("missing-chunk", "fixture", 1, "[1.0]", "2026-07-16T00:00:00+00:00", 0),
            )
            conn.commit()
        finally:
            conn.close()
        del conn
        gc.collect()
        rc, result = self.kz("K1", "--integrity")
        self.assertEqual(rc, 0, result)
        integrity = result["integrity"]
        self.assertFalse(integrity["ok"])
        orphan = next(row for row in integrity["orphaned_relationships"] if row["child"] == "chunk_embeddings.chunk_id")
        self.assertEqual(orphan["parent"], "evidence_chunks.id")
        self.assertEqual(orphan["orphans"], 1)


if __name__ == "__main__":
    unittest.main()
