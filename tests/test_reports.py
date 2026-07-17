"""Report tests: R7 --query (regression), R8-R10 windows, R11 topic requires query."""

from __future__ import annotations

import re

from _harness import IsolatedDBTest

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class ReportTest(IsolatedDBTest):
    """Focused R7-R11 smoke and regression coverage over an isolated report database."""
    def test_r7_query_does_not_hit_missing_body_column(self):
        # Regression: anti_patterns has no `body` column; R7 --query used to raise
        # 'no such column: body'. It must now return cleanly.
        rc, payload = self.kz("R7", "--query", "anything")
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload.get("status"), "OK")
        self.assertEqual(payload.get("rows"), 0)

    def test_windowed_ledger_reports_run(self):
        for code in ("R8", "R9", "R10"):
            rc, payload = self.kz(code)
            self.assertEqual(rc, 0, f"{code}: {payload}")
            self.assertEqual(payload.get("status"), "OK")

    def test_weekly_window_includes_a_recent_event(self):
        # W1 writes a ledger event (created now); the 7-day weekly window (R8) must include it,
        # proving the created_at filter passes recent rows rather than silently dropping the window.
        rc, _ = self.kz("W1", "--title", "win", "--summary", "Seed a recent ledger event.", "--body", "b")
        self.assertEqual(rc, 0)
        rc, payload = self.kz("R8")
        self.assertEqual(rc, 0, payload)
        self.assertGreaterEqual(payload.get("rows", 0), 1, payload)

    def test_r11_topic_requires_query(self):
        rc, payload = self.kz("R11")
        self.assertEqual(rc, 2, payload)
        self.assertEqual(payload.get("code"), "DENIED_QUERY_REQUIRED")

    def test_r11_topic_with_query_runs(self):
        rc, payload = self.kz("R11", "--query", "onboarding")
        self.assertEqual(rc, 0, payload)
        self.assertTrue(_HASH_RE.match(payload.get("sha256", "")), payload)
