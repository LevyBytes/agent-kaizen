"""Redaction gate: unit tests on the scanner + the trace-write regression."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# redaction.py imports only `re` and `.denials` (no DB), so importing it in-process is safe.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kaizen_components import redaction  # noqa: E402
from kaizen_components.denials import KaizenDenied  # noqa: E402

from _harness import IsolatedDBTest  # noqa: E402


class RedactionUnitTest(unittest.TestCase):
    """In-process scan_for_secrets and assert_redacted pattern coverage."""
    def test_detects_secret_classes(self):
        cases = {
            "openai_key": "sk-abcdefghijklmnopqrstuvwx12345678",
            "github_token": "ghp_abcdefghijklmnopqrstuvwxyz0123",
            "aws_access_key": "AKIAIOSFODNN7EXAMPLE",
            "google_api_key": "AIzaSyA1234567890abcdefghijklmnopqrstuv",
            "user_home_path": r"C:\Users\victim\secret.txt",
            "posix_home_path": "/home/victim/.ssh/id",
        }
        for expected, text in cases.items():
            hits = redaction.scan_for_secrets(text)
            self.assertIn(expected, hits, f"{expected} not detected in {text!r} (got {hits})")

    def test_detects_prefixed_openai_key_formats_without_reclassifying_anthropic(self):
        for prefix in ("proj", "svcacct", "admin"):
            self.assertIn("openai_key", redaction.scan_for_secrets(f"sk-{prefix}-" + "a" * 24))
        self.assertEqual(redaction.scan_for_secrets("sk-ant-" + "a" * 24), ["anthropic_key"])

    def test_personal_email_flagged_but_allowlist_clean(self):
        self.assertIn("email", redaction.scan_for_secrets("ping me at real.person@gmail.com"))
        self.assertEqual(redaction.scan_for_secrets("contact noreply@example.com please"), [])

    def test_clean_text_has_no_hits(self):
        self.assertEqual(redaction.scan_for_secrets("an ordinary sentence about reports"), [])

    def test_assert_redacted_raises_on_secret(self):
        with self.assertRaises(KaizenDenied):
            redaction.assert_redacted({"environment": "AKIAIOSFODNN7EXAMPLE"})

    def test_assert_redacted_passes_clean(self):
        redaction.assert_redacted({"summary": "clean", "body": "also clean"})  # must not raise


class TraceRedactionRegressionTest(IsolatedDBTest):
    """End-to-end T1 trace-write redaction through the subprocess CLI boundary."""
    def test_secret_in_environment_is_denied(self):
        rc, p = self.kz("T1", "--payload-json", '{"kind":"tool_call","summary":"x","environment":"/home/victim/x"}')
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_TRACE_REDACTION")

    def test_secret_in_tags_is_denied(self):
        rc, p = self.kz("T1", "--payload-json", '{"kind":"tool_call","summary":"x","tags":["ghp_abcdefghijklmnopqrstuvwxyz0123"]}')
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_TRACE_REDACTION")

    def test_clean_trace_is_accepted(self):
        rc, p = self.kz("T1", "--payload-json", '{"kind":"tool_call","summary":"clean trace"}')
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK")
