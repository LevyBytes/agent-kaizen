"""Q10 contract-lint: deterministic contract-density lint. Pure ``lint_text`` unit tests plus
CLI dispatch (pass / fail / deny) through the isolated harness. No model, no network, no DB write."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import IsolatedDBTest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kaizen_components.contract_lint import lint_text  # noqa: E402


TERSE = "Add B4 model-judge; write one eval_scores row with source=model linked to the trace event."
FILLER = (
    "In order to basically dogfood the judge, it should be noted that we go for broke. "
    "In order to basically dogfood the judge, it should be noted that we go for broke."
)


class LintTextUnitTest(unittest.TestCase):
    def test_terse_passes(self):
        r = lint_text(TERSE)
        self.assertEqual(r["verdict"], "pass", r)
        self.assertEqual(r["filler_hits"], [])
        self.assertEqual(r["duplicate_sentences"], [])

    def test_filler_fails_with_categories(self):
        r = lint_text(FILLER)
        self.assertEqual(r["verdict"], "fail", r)
        labels = {h["label"] for h in r["filler_hits"]}
        self.assertEqual(labels, {"wordy", "hedge_phrase", "colloquial"}, r)
        self.assertGreater(r["hedge_density"], 0.05, r)

    def test_near_duplicate_sentences_flagged(self):
        r = lint_text(FILLER)
        self.assertTrue(r["duplicate_sentences"], r)
        self.assertGreaterEqual(r["duplicate_sentences"][0]["jaccard"], 0.6, r)

    def test_deterministic(self):
        self.assertEqual(lint_text(FILLER), lint_text(FILLER))

    def test_empty_text_passes_trivially(self):
        r = lint_text("")
        self.assertEqual(r["verdict"], "pass", r)
        self.assertEqual(r["word_count"], 0, r)


class ContractLintCliTest(IsolatedDBTest):
    def test_cli_pass(self):
        rc, p = self.kz("Q10", "--body", TERSE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["verdict"], "pass", p)

    def test_cli_fail(self):
        rc, p = self.kz("Q10", "--body", FILLER)
        self.assertEqual(rc, 0, p)  # the analysis succeeds; the verdict is 'fail'
        self.assertEqual(p["verdict"], "fail", p)
        self.assertTrue(p["reasons"], p)

    def test_cli_requires_text(self):
        rc, p = self.kz("Q10")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_CONTRACT_TEXT_REQUIRED", p)

    def test_alias_dispatches(self):
        rc, p = self.kz("contract-lint", "--body", TERSE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["verdict"], "pass", p)


if __name__ == "__main__":
    unittest.main()
