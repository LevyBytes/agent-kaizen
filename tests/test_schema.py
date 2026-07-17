"""Schema / DB initialization tests (K1, K2), manifest-drift gating, and integrity scan."""

from __future__ import annotations

import json
import subprocess
import sys

from _harness import IsolatedDBTest


def _direct_sql(db_path, *statements: str) -> None:
    """Execute raw SQL against the isolated DB in a SEPARATE process.

    Process exit deterministically releases turso's Windows file lock, so the CLI
    subprocess that runs next never races the tamper handle (GOTCHA: turso os-error-33).
    """
    script = (
        "import sys, turso\n"
        "conn = turso.connect(sys.argv[1])\n"
        "for stmt in sys.argv[2:]:\n"
        "    conn.execute(stmt)\n"
        "conn.commit()\n"
        "conn.close()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(db_path), *statements],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(f"direct SQL failed: {proc.stderr or proc.stdout}")


def _table_columns(db_path, table: str) -> list[str]:
    """Return ordered column names from the isolated DB out of process to avoid the turso lock conflict."""
    script = (
        "import json, sys, turso\n"
        "conn = turso.connect(sys.argv[1])\n"
        "print(json.dumps([r[1] for r in conn.execute('PRAGMA table_info(' + sys.argv[2] + ')').fetchall()]))\n"
        "conn.close()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(db_path), table],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(f"column query failed: {proc.stderr or proc.stdout}")
    return json.loads(proc.stdout)


class SchemaTest(IsolatedDBTest):
    """Schema creation, versioning, and migration compatibility checks."""
    def test_k1_is_idempotent(self):
        # setUp already ran K1 once; a second run must also succeed and stay OK.
        rc, payload = self.kz("K1")
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload.get("status"), "OK")

    def test_k2_schema_ok(self):
        rc, payload = self.kz("K2")
        self.assertEqual(rc, 0, payload)
        schema = payload.get("schema", {})
        self.assertTrue(schema.get("exists"))
        self.assertTrue(schema.get("schema_ok"), schema)
        self.assertEqual(schema.get("schema_version"), 1)
        # A freshly initialized DB is in sync with its own DDL.
        self.assertTrue(schema.get("manifest_match"), schema)

    def test_db_backup_and_manifest(self):
        rc, _ = self.kz("K3")
        self.assertEqual(rc, 0)
        rc, payload = self.kz("K6")
        self.assertEqual(rc, 0)
        self.assertIn("manifest", payload)

    def test_agent_session_title_is_fresh_and_idempotent_additive(self):
        db_path = self.root / "AI" / "db" / "kaizen.db"
        self.assertIn("title", _table_columns(db_path, "agent_sessions"))
        rc, payload = self.kz("K1")
        self.assertEqual(rc, 0, payload)
        self.assertEqual(_table_columns(db_path, "agent_sessions").count("title"), 1)
        rc, status = self.kz("K2")
        self.assertEqual(rc, 0, status)
        self.assertEqual(status["schema"]["schema_version"], 1)
        self.assertTrue(status["schema"]["manifest_match"], status)

    def test_agent_session_title_repairs_a_warm_pre_column_db(self):
        db_path = self.root / "AI" / "db" / "kaizen.db"
        _direct_sql(db_path, "ALTER TABLE agent_sessions DROP COLUMN title")
        self.assertNotIn("title", _table_columns(db_path, "agent_sessions"))
        rc, payload = self.kz("K1")
        self.assertEqual(rc, 0, payload)
        self.assertEqual(_table_columns(db_path, "agent_sessions").count("title"), 1)


class ManifestDriftGateTest(IsolatedDBTest):
    """DDL drift (manifest hash changed without a migration bump) must fail closed on writes,
    and K1 --restamp-manifest reconciles a benign additive drift."""

    def _db_path(self):
        return self.root / "AI" / "db" / "kaizen.db"

    def test_write_denied_on_manifest_drift_then_restamp_repairs(self):
        _direct_sql(
            self._db_path(),
            "UPDATE schema_version SET manifest_hash = 'tampered-drift-hash' WHERE id = 'current'",
        )

        # Reads still work and report the drift...
        rc, k2 = self.kz("K2")
        self.assertEqual(rc, 0, k2)
        self.assertFalse(k2["schema"]["manifest_match"], k2)
        self.assertTrue(k2["schema"]["schema_ok"], k2)

        # ...but a write is denied, fail-closed.
        rc, denied = self.kz("G1", "--title", "T", "--summary", "One sentence.", "--body", "x")
        self.assertEqual(rc, 1, denied)
        self.assertEqual(denied.get("code"), "DENIED_SCHEMA_DRIFT", denied)

        # Restamp reconciles the benign drift (schema_ok is still true).
        rc, restamp = self.kz("K1", "--restamp-manifest")
        self.assertEqual(rc, 0, restamp)
        self.assertTrue(restamp.get("restamped"), restamp)

        # Writes flow again.
        rc, ok = self.kz("G1", "--title", "T", "--summary", "One sentence.", "--body", "x")
        self.assertEqual(rc, 0, ok)

    def test_restamp_denied_when_schema_version_mismatch(self):
        # A real migration (wrong migration_id) is NOT drift; restamp must refuse to paper over it.
        _direct_sql(
            self._db_path(),
            "UPDATE schema_version SET migration_id = 'some-future-migration' WHERE id = 'current'",
        )
        rc, denied = self.kz("K1", "--restamp-manifest")
        self.assertEqual(rc, 2, denied)
        self.assertEqual(denied.get("code"), "DENIED_SCHEMA_STATUS", denied)

    def test_restamp_noop_when_already_matching(self):
        rc, restamp = self.kz("K1", "--restamp-manifest")
        self.assertEqual(rc, 0, restamp)
        self.assertFalse(restamp.get("restamped"), restamp)


class IntegrityScanTest(IsolatedDBTest):
    """K1 integrity scanning for orphaned governed references."""
    def _db_path(self):
        return self.root / "AI" / "db" / "kaizen.db"

    def test_clean_db_reports_no_orphans(self):
        rc, payload = self.kz("K1", "--integrity")
        self.assertEqual(rc, 0, payload)
        integ = payload.get("integrity", {})
        self.assertTrue(integ.get("ok"), integ)
        self.assertEqual(integ.get("total_orphans"), 0, integ)
        self.assertGreater(integ.get("relationships_checked", 0), 0, integ)

    def test_orphaned_reference_is_detected(self):
        # eval_scores.eval_run_id -> eval_runs.id, with a run id that does not exist.
        _direct_sql(
            self._db_path(),
            "INSERT INTO eval_scores (id, created_at, eval_run_id, name, data_type, source, content_hash) "
            "VALUES ('es_orphan', '2026-01-01T00:00:00+00:00', 'run_missing', 'n', 'numeric', 'test', 'h')",
        )

        rc, payload = self.kz("K1", "--integrity")
        self.assertEqual(rc, 0, payload)
        integ = payload["integrity"]
        self.assertFalse(integ["ok"], integ)
        self.assertGreaterEqual(integ["total_orphans"], 1, integ)
        pairs = {(r["child"], r["parent"]) for r in integ["orphaned_relationships"]}
        self.assertIn(("eval_scores.eval_run_id", "eval_runs.id"), pairs, integ)
