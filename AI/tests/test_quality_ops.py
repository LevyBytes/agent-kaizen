"""Quality ops Q1-Q7/Q9 and artifact ops A2-A4: defaults, denials, and DB round-trips."""

from __future__ import annotations

import hashlib
import json
import re
import unittest

from _harness import IsolatedDBTest

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class QualityOpsTest(IsolatedDBTest):
    # -- Q1 proof-add ------------------------------------------------------

    def test_q1_default_conclusion_proof_recorded(self):
        # Q1 without --conclusion must store PROOF_RECORDED (dispatch default).
        rc, p = self.kz("Q1", "--summary", "Proof captured for the unit test.")
        self.assertEqual(rc, 0, p)
        vid = p["id"]
        self.assertTrue(vid.startswith("qv_"), p)
        self.assertTrue(_HASH_RE.match(p.get("content_hash", "")), p)

        rc, p = self.kz("Q7", "--kind", "verifier", "--id", vid)
        self.assertEqual(rc, 0, p)
        record = p["record"]
        self.assertEqual(record["id"], vid)
        self.assertEqual(record["conclusion"], "PROOF_RECORDED")
        self.assertEqual(record["summary"], "Proof captured for the unit test.")

    def test_q1_missing_summary_denied(self):
        rc, p = self.kz("Q1")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("status"), "DENIED")
        self.assertEqual(p.get("code"), "DENIED_SUMMARY_REQUIRED")

    # -- Q2 verification-add -----------------------------------------------

    def test_q2_explicit_conclusion_and_linkage(self):
        rc, p = self.kz("Q1", "--summary", "Baseline proof for linkage.")
        self.assertEqual(rc, 0, p)
        proof_id = p["id"]

        rc, p = self.kz(
            "Q2",
            "--conclusion", "VERIFIED_ACCEPTABLE",
            "--task-id", "task-42",
            "--proof-id", proof_id,
            "--evidence", '["AI/work/verify.log"]',
            "--summary", "Verified against the recorded proof.",
        )
        self.assertEqual(rc, 0, p)
        vid = p["id"]

        rc, p = self.kz("Q7", "--kind", "verifier", "--id", vid)
        self.assertEqual(rc, 0, p)
        record = p["record"]
        self.assertEqual(record["conclusion"], "VERIFIED_ACCEPTABLE")
        self.assertEqual(record["task_id"], "task-42")
        self.assertEqual(record["proof_id"], proof_id)
        self.assertEqual(json.loads(record["evidence_locations_json"]), ["AI/work/verify.log"])

    def test_q2_defaults_to_needs_human_decision(self):
        # Q2's handler default (no --conclusion) is NEEDS_HUMAN_DECISION, not Q1's PROOF_RECORDED.
        rc, p = self.kz("Q2", "--summary", "Verification recorded without a conclusion.")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("Q7", "--kind", "verifier", "--id", p["id"])
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["record"]["conclusion"], "NEEDS_HUMAN_DECISION")

    def test_q2_stores_severity_scope_label_actionability(self):
        rc, p = self.kz(
            "Q2",
            "--conclusion", "VERIFICATION_FAILED",
            "--severity", "high",
            "--scope-label", "module",
            "--actionability", "immediate",
            "--summary", "Login regression reproduced twice.",
        )
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("Q7", "--kind", "verifier", "--id", p["id"])
        self.assertEqual(rc, 0, p)
        record = p["record"]
        self.assertEqual(record["conclusion"], "VERIFICATION_FAILED")
        self.assertEqual(record["severity"], "high")
        self.assertEqual(record["scope_label"], "module")
        self.assertEqual(record["actionability"], "immediate")

    def test_q2_invalid_conclusion_denied_by_schema(self):
        rc, p = self.kz("Q2", "--conclusion", "TOTALLY_BOGUS", "--summary", "Bad conclusion value.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_SCHEMA_ENUM")
        self.assertEqual(p.get("field"), "conclusion")
        self.assertIn("VERIFIED_ACCEPTABLE", p.get("allowed", []))

    # -- Q3 eval-case-add ----------------------------------------------------

    def test_q3_title_denial_and_category_default(self):
        rc, p = self.kz("Q3", "--summary", "Eval case with no title.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_TITLE_REQUIRED")

        rc, p = self.kz("Q3", "--title", "Routing sanity case", "--summary", "Default-category eval case.")
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["id"].startswith("qe_"), p)
        self.assertTrue(_HASH_RE.match(p.get("content_hash", "")), p)

        # O1 filters eval_cases by category: the case must land under the default
        # "behavior" and NOT under any other category.
        rc, p = self.kz("O1", "--contract", "probe", "--category", "behavior")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("eval_cases"), 1, p)
        self.assertTrue((self.root / p["path"]).is_file(), p)
        rc, p = self.kz("O1", "--contract", "probe", "--category", "routing")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("eval_cases"), 0, p)

    # -- Q4 eval-run-add -----------------------------------------------------

    def test_q4_eval_run_add_and_summary_denial(self):
        rc, p = self.kz("Q3", "--title", "Case for a run", "--summary", "Parent eval case.")
        self.assertEqual(rc, 0, p)
        case_id = p["id"]

        rc, p = self.kz("Q4", "--eval-case-id", case_id, "--summary", "Run recorded against the case.")
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["id"].startswith("qr_"), p)
        self.assertTrue(_HASH_RE.match(p.get("content_hash", "")), p)

        rc, p = self.kz("Q4")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_SUMMARY_REQUIRED")

    # -- Q5/Q6 anti-patterns ---------------------------------------------------

    def test_q5_missing_required_fields_listed(self):
        rc, p = self.kz("Q5", "--title", "Retry storm", "--summary", "Partial anti-pattern submission.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_REQUIRED_FIELDS")
        self.assertEqual(
            p.get("fields"),
            [
                "symptom",
                "maintainability_harm",
                "trigger_evidence",
                "preferred_correction",
                "valid_exceptions",
                "verification",
            ],
            p,
        )

    def test_q5_add_then_q6_finds_by_symptom(self):
        rc, p = self.kz(
            "Q5",
            "--title", "Blind retry loop",
            "--symptom", "zigzag-retry hammering a failing endpoint",
            "--maintainability-harm", "Masks the real fault and floods logs.",
            "--trigger-evidence", "CI log shows 40 identical retries.",
            "--preferred-correction", "Bounded backoff with a circuit breaker.",
            "--valid-exceptions", "Known-transient network blips.",
            "--verification", "Retry count stays under the bound in tests.",
            "--summary", "Unbounded retries hide the underlying fault.",
        )
        self.assertEqual(rc, 0, p)
        ap_id = p["id"]
        self.assertTrue(ap_id.startswith("ap_"), p)

        # Q6 without --query is a usage denial.
        rc, p = self.kz("Q6")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_QUERY_REQUIRED")

        # Q6 matches on symptom text (not just title/summary).
        rc, p = self.kz("Q6", "--query", "zigzag-retry")
        self.assertEqual(rc, 0, p)
        self.assertIn(ap_id, [r["id"] for r in p["records"]], p)

        rc, p = self.kz("Q6", "--query", "no-such-anti-pattern")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["records"], [], p)

    # -- Q7 quality-inspect ------------------------------------------------------

    def test_q7_kind_selects_table(self):
        f = self.root / "evidence.txt"
        f.write_text("inspect me", encoding="utf-8")
        rc, p = self.kz("A1", "--path", str(f), "--summary", "Evidence file for Q7.")
        self.assertEqual(rc, 0, p)
        artifact_id = p["id"]
        rc, p = self.kz("Q1", "--summary", "Proof row for Q7 table routing.")
        self.assertEqual(rc, 0, p)
        verification_id = p["id"]

        # Default kind inspects the artifacts table.
        rc, p = self.kz("Q7", "--id", artifact_id)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["record"]["id"], artifact_id)
        self.assertEqual(p["record"]["path"], "evidence.txt")

        # --kind verifier switches to verification_events.
        rc, p = self.kz("Q7", "--kind", "verifier", "--id", verification_id)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["record"]["conclusion"], "PROOF_RECORDED")

        # Cross-table lookups miss: each id exists only in its own table.
        rc, p = self.kz("Q7", "--kind", "verifier", "--id", artifact_id)
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_RECORD_NOT_FOUND")
        self.assertEqual(p.get("table"), "verification_events")
        rc, p = self.kz("Q7", "--id", verification_id)
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("table"), "artifacts")

        rc, p = self.kz("Q7")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_ID_REQUIRED")

    # -- Q9 verify-query -----------------------------------------------------------

    def _seed_verifications(self) -> tuple[str, str, str]:
        rc, p = self.kz("Q1", "--summary", "Baseline proof event.")
        self.assertEqual(rc, 0, p)
        v1 = p["id"]
        rc, p = self.kz(
            "Q2", "--conclusion", "VERIFICATION_FAILED", "--severity", "high",
            "--actionability", "immediate", "--task-id", "task-a",
            "--summary", "Login regression reproduced.",
        )
        self.assertEqual(rc, 0, p)
        v2 = p["id"]
        rc, p = self.kz(
            "Q2", "--conclusion", "VERIFIED_ACCEPTABLE", "--severity", "low",
            "--task-id", "task-b", "--summary", "Checkout flow verified clean.",
        )
        self.assertEqual(rc, 0, p)
        v3 = p["id"]
        return v1, v2, v3

    def test_q9_no_filters_lists_recent(self):
        v1, v2, v3 = self._seed_verifications()
        rc, p = self.kz("Q9")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("count"), 3, p)
        ids = [r["id"] for r in p["records"]]
        self.assertEqual(sorted(ids), sorted([v1, v2, v3]), p)
        failed = next(r for r in p["records"] if r["id"] == v2)
        self.assertEqual(failed["conclusion"], "VERIFICATION_FAILED")
        self.assertEqual(failed["severity"], "high")
        self.assertEqual(failed["actionability"], "immediate")

    def test_q9_filters_combine(self):
        _v1, v2, v3 = self._seed_verifications()

        rc, p = self.kz("Q9", "--conclusion", "VERIFICATION_FAILED")
        self.assertEqual(rc, 0, p)
        self.assertEqual([r["id"] for r in p["records"]], [v2], p)

        rc, p = self.kz("Q9", "--task-id", "task-b")
        self.assertEqual(rc, 0, p)
        self.assertEqual([r["id"] for r in p["records"]], [v3], p)

        rc, p = self.kz("Q9", "--severity", "high")
        self.assertEqual(rc, 0, p)
        self.assertEqual([r["id"] for r in p["records"]], [v2], p)

        rc, p = self.kz("Q9", "--query", "Checkout")
        self.assertEqual(rc, 0, p)
        self.assertEqual([r["id"] for r in p["records"]], [v3], p)

        # Filters AND together: task-a produced no VERIFIED_ACCEPTABLE event.
        rc, p = self.kz("Q9", "--task-id", "task-a", "--conclusion", "VERIFIED_ACCEPTABLE")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["records"], [], p)
        rc, p = self.kz("Q9", "--task-id", "task-a", "--conclusion", "VERIFICATION_FAILED")
        self.assertEqual(rc, 0, p)
        self.assertEqual([r["id"] for r in p["records"]], [v2], p)

    # -- A2/A3/A4 artifacts -----------------------------------------------------------

    def test_a2_artifact_hash(self):
        rc, p = self.kz("A2", "--path", str(self.root / "missing.txt"))
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_FILE_NOT_FOUND")

        content = b"kaizen quality ops fixture\n"
        f = self.root / "hash-me.txt"
        f.write_bytes(content)
        rc, p = self.kz("A2", "--path", str(f))
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["sha256"], hashlib.sha256(content).hexdigest(), p)
        self.assertEqual(p["bytes"], len(content), p)
        self.assertEqual(p["path"], "hash-me.txt", p)

    def test_a3_artifact_inspect(self):
        rc, p = self.kz("A3")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_ID_REQUIRED")

        rc, p = self.kz("A3", "--id", "a_does_not_exist")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_RECORD_NOT_FOUND")
        self.assertEqual(p.get("table"), "artifacts")

        f = self.root / "report.txt"
        f.write_text("proof output", encoding="utf-8")
        rc, p = self.kz("A1", "--path", str(f), "--task-id", "task-7", "--summary", "Report artifact.")
        self.assertEqual(rc, 0, p)
        aid, sha = p["id"], p["sha256"]

        rc, p = self.kz("A3", "--id", aid)
        self.assertEqual(rc, 0, p)
        record = p["record"]
        self.assertEqual(record["id"], aid)
        self.assertEqual(record["path"], "report.txt")
        self.assertEqual(record["sha256"], sha)
        self.assertEqual(record["task_id"], "task-7")
        self.assertEqual(record["kind"], "file")
        self.assertEqual(record["summary"], "Report artifact.")

    def test_a4_artifact_list_with_and_without_query(self):
        for name, summary in (("alpha.log", "Alpha evidence log."), ("beta.png", "Beta screenshot capture.")):
            f = self.root / name
            f.write_text(f"content of {name}", encoding="utf-8")
            rc, p = self.kz("A1", "--path", str(f), "--summary", summary)
            self.assertEqual(rc, 0, p)

        rc, p = self.kz("A4")
        self.assertEqual(rc, 0, p)
        paths = [r["path"] for r in p["records"]]
        self.assertIn("alpha.log", paths, p)
        self.assertIn("beta.png", paths, p)

        rc, p = self.kz("A4", "--query", "Alpha evidence")
        self.assertEqual(rc, 0, p)
        self.assertEqual([r["path"] for r in p["records"]], ["alpha.log"], p)

        rc, p = self.kz("A4", "--query", "no-match-token")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["records"], [], p)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

class JsonFileAndQ7KindTest(IsolatedDBTest):
    """A3 *-file fallbacks for JSON flags + A7 Q7 routing for eval runs/cases."""

    def test_q2_evidence_file_fallback_round_trips(self):
        evidence_path = self.root / "evidence.json"
        evidence_path.write_text('["AI/tests"]', encoding="utf-8")
        rc, p = self.kz(
            "Q2", "--conclusion", "VERIFIED_ACCEPTABLE",
            "--summary", "Checks passed.", "--evidence-file", str(evidence_path),
        )
        self.assertEqual(rc, 0, p)
        rc, rec = self.kz("Q7", "--kind", "verifier", "--id", p["id"])
        self.assertEqual(rc, 0, rec)
        self.assertEqual(rec["record"]["evidence_locations_json"], '["AI/tests"]', rec)

    def test_q2_evidence_file_missing_denies(self):
        rc, p = self.kz(
            "Q2", "--conclusion", "VERIFIED_ACCEPTABLE",
            "--summary", "Checks passed.", "--evidence-file", str(self.root / "absent.json"),
        )
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_FILE_NOT_FOUND", p)

    def test_q7_routes_eval_run_and_eval_case(self):
        rc, p = self.kz("Q3", "--title", "Case", "--summary", "One-sentence case.")
        self.assertEqual(rc, 0, p)
        case_id = p["id"]
        rc, p = self.kz("Q4", "--eval-case-id", case_id, "--summary", "One-sentence run.")
        self.assertEqual(rc, 0, p)
        run_id = p["id"]
        rc, rec = self.kz("Q7", "--kind", "eval-run", "--id", run_id)
        self.assertEqual(rc, 0, rec)
        self.assertEqual(rec["record"]["eval_case_id"], case_id, rec)
        rc, rec = self.kz("Q7", "--kind", "eval-case", "--id", case_id)
        self.assertEqual(rc, 0, rec)
        self.assertEqual(rec["record"]["id"], case_id, rec)
