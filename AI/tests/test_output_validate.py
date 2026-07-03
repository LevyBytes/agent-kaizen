"""Q8 output-validate: schema listing, accept-valid, reject-invalid."""

from __future__ import annotations

from _harness import IsolatedDBTest


class OutputValidateTest(IsolatedDBTest):
    def test_lists_schemas_without_kind(self):
        rc, p = self.kz("Q8")
        self.assertEqual(rc, 0, p)
        self.assertIn("schemas", p)
        self.assertIn("gotcha", p["schemas"])

    def test_accepts_valid_payload(self):
        rc, p = self.kz("Q8", "--kind", "gotcha", "--payload-json", '{"title":"T","summary":"S","body":"B"}')
        self.assertEqual(rc, 0, p)
        self.assertTrue(p.get("valid"))

    def test_rejects_invalid_payload(self):
        # gotcha requires title+summary+body; omitting body must deny.
        rc, p = self.kz("Q8", "--kind", "gotcha", "--payload-json", '{"title":"T"}')
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("status"), "DENIED")

    def test_unknown_kind_denied(self):
        rc, p = self.kz("Q8", "--kind", "not_a_real_type", "--payload-json", "{}")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_SCHEMA_UNKNOWN_TYPE")
