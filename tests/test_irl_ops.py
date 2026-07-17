"""IRL Review ops: I1 create, I2-I4 events require a review id, I5 report round-trip."""

from __future__ import annotations

import re

from _harness import IsolatedDBTest

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class IrlOpsTest(IsolatedDBTest):
    def _create_review(self, **extra_flags: str) -> str:
        """I1 with a minimal valid summary; returns the review id."""
        flags = ["--summary", "Review of the cache fix."]
        for name, value in extra_flags.items():
            flags += [f"--{name.replace('_', '-')}", value]
        rc, p = self.kz("I1", *flags)
        self.assertEqual(rc, 0, p)
        return p["review_id"]

    # ---- I1 -----------------------------------------------------------

    def test_i1_create_review_id_defaults_to_record_id(self):
        rc, p = self.kz("I1", "--summary", "Review kickoff for the cache fix.")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK")
        self.assertEqual(p["review_id"], p["id"], p)
        self.assertEqual(p["event_type"], "review")
        self.assertTrue(p["id"].startswith("irl_"), p)

    def test_i1_alias_irl_create_works(self):
        rc, p = self.kz("irl-create", "--summary", "Alias-created review.")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["event_type"], "review")
        self.assertEqual(p["review_id"], p["id"], p)

    def test_i1_honors_explicit_review_id(self):
        rc, p = self.kz("I1", "--review-id", "rev-custom", "--summary", "Review under a caller-chosen id.")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["review_id"], "rev-custom")
        self.assertNotEqual(p["id"], "rev-custom", p)

    def test_i1_missing_summary_denied(self):
        rc, p = self.kz("I1")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("status"), "DENIED")
        self.assertEqual(p.get("code"), "DENIED_SUMMARY_REQUIRED")

    # ---- I2/I3/I4 -----------------------------------------------------

    def test_i2_i3_i4_without_review_id_denied(self):
        # Summary must be valid so the review-id check (which runs after text
        # validation) is what actually denies.
        for code in ("I2", "I3", "I4"):
            rc, p = self.kz(code, "--summary", "Event without a parent review.")
            self.assertEqual(rc, 2, f"{code}: {p}")
            self.assertEqual(p.get("status"), "DENIED", f"{code}: {p}")
            self.assertEqual(p.get("code"), "DENIED_REVIEW_ID_REQUIRED", f"{code}: {p}")

    def test_event_ops_link_to_review_and_carry_event_types(self):
        rid = self._create_review()
        expected = {"I2": "prediction", "I3": "user_correction", "I4": "observed_outcome"}
        for code, event_type in expected.items():
            rc, p = self.kz(code, "--review-id", rid, "--summary", f"{event_type} event.")
            self.assertEqual(rc, 0, f"{code}: {p}")
            self.assertEqual(p["event_type"], event_type, p)
            self.assertEqual(p["review_id"], rid, p)
            self.assertNotEqual(p["id"], rid, p)
        # Linkage proof: the report query (WHERE review_id = ?) sees all 4 rows.
        rc, p = self.kz("I5", "--review-id", rid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["rows"], 4, p)

    def test_decision_falls_back_from_flag_to_title_to_event_type(self):
        rid = self._create_review(decision="APPROVED-BY-FLAG", title="title-should-lose")
        rc, p = self.kz("I2", "--review-id", rid, "--title", "title-fallback-wins",
                        "--summary", "Prediction with title-only decision.")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("I3", "--review-id", rid, "--summary", "Correction with no decision or title.")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("I5", "--review-id", rid)
        self.assertEqual(rc, 0, p)
        text = (self.root / p["path"]).read_text(encoding="utf-8")
        self.assertIn("[review:recorded] APPROVED-BY-FLAG - ", text)  # --decision beats --title
        self.assertNotIn("title-should-lose", text)
        self.assertIn("[prediction:recorded] title-fallback-wins - ", text)  # --title fallback
        self.assertIn("[user_correction:recorded] user_correction - ", text)  # event_type fallback

    def test_status_flag_overrides_recorded_default(self):
        rid = self._create_review(status="open")
        rc, p = self.kz("I4", "--review-id", rid, "--summary", "Outcome with default status.")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("I5", "--review-id", rid)
        self.assertEqual(rc, 0, p)
        text = (self.root / p["path"]).read_text(encoding="utf-8")
        self.assertIn("[review:open]", text)
        self.assertIn("[observed_outcome:recorded]", text)

    # ---- I5 -----------------------------------------------------------

    def test_i5_missing_review_id_denied(self):
        rc, p = self.kz("I5")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("status"), "DENIED")
        self.assertEqual(p.get("code"), "DENIED_REVIEW_ID_REQUIRED")

    def test_i5_report_rows_and_file_contents(self):
        rid = self._create_review()
        rc, pred = self.kz("I2", "--review-id", rid, "--summary", "Predicted green tests.")
        self.assertEqual(rc, 0, pred)
        rc, out = self.kz("I4", "--review-id", rid, "--summary", "Observed green tests.")
        self.assertEqual(rc, 0, out)

        rc, p = self.kz("I5", "--review-id", rid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK")
        self.assertEqual(p["review_id"], rid)
        self.assertEqual(p["rows"], 3, p)
        self.assertTrue(_HASH_RE.match(p.get("sha256", "")), p)

        report = self.root / p["path"]
        self.assertTrue(report.is_file(), report)
        self.assertEqual(report.name, f"irl-review-{rid}.md")
        text = report.read_text(encoding="utf-8")
        self.assertIn(f"# IRL Review {rid}", text)
        self.assertIn("Rows: 3", text)
        for record_id in (rid, pred["id"], out["id"]):
            self.assertIn(f"- `{record_id}`", text)
        self.assertIn("[prediction:recorded]", text)
        self.assertIn("Predicted green tests.", text)
        self.assertIn("Observed green tests.", text)

    def test_i5_accepts_id_flag_as_review_id(self):
        rid = self._create_review()
        rc, p = self.kz("I5", "--id", rid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["review_id"], rid)
        self.assertEqual(p["rows"], 1, p)

    def test_i5_unknown_review_id_writes_empty_report(self):
        rc, p = self.kz("I5", "--review-id", "rev-never-created")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["rows"], 0, p)
        report = self.root / p["path"]
        self.assertTrue(report.is_file(), report)
        self.assertIn("Rows: 0", report.read_text(encoding="utf-8"))

    def test_i5_slugs_untrusted_review_id_into_reports_directory(self):
        raw_id = "../../evil\\review"
        rc, payload = self.kz("I5", "--review-id", raw_id)
        self.assertEqual(rc, 0, payload)
        report = (self.root / payload["path"]).resolve()
        reports = (self.root / "AI" / "db" / "exports" / "reports").resolve()
        report.relative_to(reports)
        self.assertNotIn("..", report.name)
        self.assertNotIn("/", report.name)
        self.assertNotIn("\\", report.name)
        self.assertTrue(report.is_file(), report)
