"""Q8 output-validate: schema listing, accept-valid, reject-invalid."""

from __future__ import annotations

from _harness import IsolatedDBTest
from kaizen_components.denials import KaizenDenied
from kaizen_components.hashing import validate_word_limit
from kaizen_components.schemas.validator import validate_against_spec


class WordLimitBandTest(IsolatedDBTest):
    def test_split_threshold_scales_with_the_field_limit(self):
        with self.assertRaises(KaizenDenied) as short:
            validate_word_limit("priority", " ".join(["x"] * 21), limit=20)
        self.assertEqual(short.exception.code, "DENIED_FIELD_WORD_LIMIT")
        with self.assertRaises(KaizenDenied) as split:
            validate_word_limit("body", " ".join(["x"] * 27), limit=20)
        self.assertEqual(split.exception.code, "DENIED_FIELD_SPLIT_REQUIRED")


class CompactSchemaFieldTypeTest(IsolatedDBTest):
    def test_known_types_and_explicit_any_are_accepted(self):
        values = {
            "str": "value", "int": 7, "float": 2.5, "bool": True,
            "list": [1], "dict": {"key": "value"}, "any": object(),
        }
        for type_name, value in values.items():
            with self.subTest(type_name=type_name):
                payload = {"field": value}
                self.assertIs(
                    validate_against_spec(
                        payload, {"fields": {"field": {"type": type_name}}}, record_type="fixture",
                    ),
                    payload,
                )

    def test_unknown_type_name_is_a_schema_error(self):
        for payload in ({"field": 7}, {}):
            with self.subTest(payload=payload), self.assertRaises(KaizenDenied) as denied:
                validate_against_spec(
                    payload, {"fields": {"field": {"type": "integer"}}}, record_type="fixture",
                )
            self.assertEqual(denied.exception.code, "DENIED_SCHEMA_FIELD_TYPE")
            self.assertEqual(denied.exception.fields["expected"], "integer")


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
        # gotcha requires title+summary+body; omitting only body must deny.
        rc, p = self.kz("Q8", "--kind", "gotcha", "--payload-json", '{"title":"T","summary":"S"}')
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("status"), "DENIED")
        self.assertEqual(p.get("code"), "DENIED_SCHEMA_REQUIRED")

    def test_unknown_kind_denied(self):
        rc, p = self.kz("Q8", "--kind", "not_a_real_type", "--payload-json", "{}")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("status"), "DENIED")
        self.assertEqual(p.get("code"), "DENIED_SCHEMA_UNKNOWN_TYPE")
