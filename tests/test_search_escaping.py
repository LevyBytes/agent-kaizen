"""LIKE-wildcard escaping across the record/report query surfaces.

Every text search routes through text_search.like_pattern + `LIKE ? ESCAPE '\\'`, so a
literal `_` or `%` in a query matches literally instead of behaving as a SQL wildcard.
Each integration case seeds a HIT record containing the literal token `a_b` and a DECOY
containing `axb`; querying `a_b` must return the hit and NOT the decoy (unescaped, the `_`
would match any single char and pull in the decoy). The distinct query functions exercised
here are query_records (G3/L5), query_policies (X3), source_query (S2), and
query_verifications (Q9); list_artifacts, query_anti_patterns, make_report, and
query_eval_scores call the same one-line helper proven by the unit test below.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import IsolatedDBTest  # noqa: E402
from kaizen_components.text_search import like_pattern  # noqa: E402


class LikePatternUnitTest(unittest.TestCase):
    """Unit proof of backslash-first escaping for SQL LIKE wildcards."""
    def test_escapes_wildcards_and_backslash(self):
        self.assertEqual(like_pattern("a_b"), r"%a\_b%")
        self.assertEqual(like_pattern("50%off"), r"%50\%off%")
        self.assertEqual(like_pattern(r"c:\x"), r"%c:\\x%")
        self.assertEqual(like_pattern("plain"), "%plain%")


class SearchEscapingTest(IsolatedDBTest):
    """End-to-end proof that each CLI query surface treats underscore and percent literally."""
    def _titles(self, payload):
        """Extract the title set from a query operation's records payload."""
        return {r.get("title") for r in payload.get("records", [])}

    def test_gotcha_query_g3(self):
        self.assertEqual(self.kz("G1", "--title", "ghit", "--summary", "token a_b here", "--body", "x")[0], 0)
        self.assertEqual(self.kz("G1", "--title", "gdecoy", "--summary", "token axb here", "--body", "x")[0], 0)
        rc, p = self.kz("G3", "--query", "a_b")
        self.assertEqual(rc, 0, p)
        self.assertIn("ghit", self._titles(p), p)
        self.assertNotIn("gdecoy", self._titles(p), p)

    def test_learning_query_l5(self):
        self.assertEqual(self.kz("L1", "--title", "lhit", "--summary", "token a_b here", "--body", "x")[0], 0)
        self.assertEqual(self.kz("L1", "--title", "ldecoy", "--summary", "token axb here", "--body", "x")[0], 0)
        rc, p = self.kz("L5", "--query", "a_b")
        self.assertEqual(rc, 0, p)
        self.assertIn("lhit", self._titles(p), p)
        self.assertNotIn("ldecoy", self._titles(p), p)

    def test_policy_query_x3(self):
        self.assertEqual(
            self.kz("X1", "--title", "xhit", "--summary", "token a_b here", "--body", "r", "--priority", "high")[0], 0
        )
        self.assertEqual(
            self.kz("X1", "--title", "xdecoy", "--summary", "token axb here", "--body", "r", "--priority", "high")[0], 0
        )
        rc, p = self.kz("X3", "--query", "a_b")
        self.assertEqual(rc, 0, p)
        self.assertIn("xhit", self._titles(p), p)
        self.assertNotIn("xdecoy", self._titles(p), p)

    def test_policy_query_keeps_newest_first_within_priority(self):
        for title in ("older recency-token", "newer recency-token"):
            self.assertEqual(
                self.kz("X1", "--title", title, "--summary", "recency-token", "--body", "r",
                        "--priority", "high")[0],
                0,
            )
        rc, p = self.kz("X3", "--query", "recency-token")
        self.assertEqual(rc, 0, p)
        titles = [row["title"] for row in p["records"]]
        self.assertEqual(titles[:2], ["newer recency-token", "older recency-token"])

    def test_source_query_s2(self):
        self.assertEqual(
            self.kz("S1", "--source-id", "shit", "--authority-tier", "implementation",
                    "--url-or-repository", "docs/hit.md", "--summary", "token a_b here")[0], 0)
        self.assertEqual(
            self.kz("S1", "--source-id", "sdecoy", "--authority-tier", "implementation",
                    "--url-or-repository", "docs/decoy.md", "--summary", "token axb here")[0], 0)
        rc, p = self.kz("S2", "--query", "a_b")
        self.assertEqual(rc, 0, p)
        ids = {r.get("source_id") for r in p.get("records", [])}
        self.assertIn("shit", ids, p)
        self.assertNotIn("sdecoy", ids, p)

    def test_verification_query_q9(self):
        self.assertEqual(
            self.kz("Q2", "--task-id", "t_x", "--conclusion", "VERIFIED_ACCEPTABLE",
                    "--summary", "token a_b here")[0], 0)
        self.assertEqual(
            self.kz("Q2", "--task-id", "t_x", "--conclusion", "VERIFIED_ACCEPTABLE",
                    "--summary", "token axb here")[0], 0)
        rc, p = self.kz("Q9", "--query", "a_b")
        self.assertEqual(rc, 0, p)
        summaries = [r.get("summary", "") for r in p.get("records", [])]
        self.assertTrue(any("a_b" in s for s in summaries), p)
        self.assertFalse(any("axb" in s for s in summaries), p)


if __name__ == "__main__":
    unittest.main()
