"""Schema / DB initialization tests (K1, K2)."""

from __future__ import annotations

from _harness import IsolatedDBTest


class SchemaTest(IsolatedDBTest):
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

    def test_db_backup_and_manifest(self):
        rc, _ = self.kz("K3")
        self.assertEqual(rc, 0)
        rc, payload = self.kz("K6")
        self.assertEqual(rc, 0)
        self.assertIn("manifest", payload)
