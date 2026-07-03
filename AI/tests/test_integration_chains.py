"""End-to-end chains across families: G/L/R learning promotion, W/A/Q/R verification, T traces/scores, A1 containment."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from _harness import IsolatedDBTest

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class IntegrationChainsTest(IsolatedDBTest):
    # --- Scenario 1: G1 -> L2 -> L3 -> R3 -> L10 -------------------------------------

    def test_gotcha_promotion_chain_report_and_learned_context(self):
        rc, p = self.kz(
            "G1",
            "--title", "Stale cache reads",
            "--summary", "Cache reads go stale after schema changes.",
            "--body", "Observed stale rows after a migration.",
        )
        self.assertEqual(rc, 0, p)
        gid = p["id"]

        rc, p = self.kz("L2", "--id", gid)
        self.assertEqual(rc, 0, p)
        lid = p["id"]

        # Linkage: the LEARNING row must carry source_gotcha_id back to the GOTCHA.
        rc, p = self.kz("L6", "--id", lid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["record"]["source_gotcha_id"], gid, p)
        self.assertEqual(p["record"]["title"], "Stale cache reads", p)

        rc, p = self.kz("L3", "--id", lid)
        self.assertEqual(rc, 0, p)
        ldid = p["id"]

        rc, p = self.kz("L9", "--id", ldid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["record"]["source_learning_id"], lid, p)

        # R3 learning report sees the promoted LEARNING row and writes a real file.
        rc, p = self.kz("R3")
        self.assertEqual(rc, 0, p)
        self.assertGreaterEqual(p["rows"], 1, p)
        report = self.root / p["path"]
        self.assertTrue(report.is_file(), p)
        self.assertIn(lid, report.read_text(encoding="utf-8"))

        # L10 stitches the full genealogy into one narrative record.
        rc, p = self.kz("L10")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 1, p)
        rec = p["records"][0]
        self.assertEqual(rec["id"], ldid, rec)
        self.assertEqual(rec["chain"]["learning"]["id"], lid, rec)
        self.assertEqual(rec["chain"]["gotcha"]["id"], gid, rec)
        self.assertTrue(rec["narrative"].startswith("GOTCHA:"), rec)
        self.assertIn(" -> LEARNING:", rec["narrative"], rec)
        self.assertIn(" -> LEARNED:", rec["narrative"], rec)

    # --- Scenario 2: W1 -> A1 -> Q2 -> Q9 -> R0 -> R4 --------------------------------

    def test_task_artifact_verification_digest_and_proof_report(self):
        rc, p = self.kz(
            "W1",
            "--title", "Ship artifact check",
            "--summary", "Verify the artifact pipeline end to end.",
            "--body", "Chain W1, A1, Q2, Q9, R0, R4.",
        )
        self.assertEqual(rc, 0, p)
        tid = p["id"]

        evidence = self.root / "evidence.txt"
        evidence.write_text("golden output mismatch", encoding="utf-8")
        rc, p = self.kz(
            "A1",
            "--path", str(evidence),
            "--task-id", tid,
            "--summary", "Evidence file for the verification chain.",
        )
        self.assertEqual(rc, 0, p)
        aid = p["id"]
        self.assertTrue(_HASH_RE.match(p.get("sha256", "")), p)

        rc, p = self.kz("A3", "--id", aid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["record"]["task_id"], tid, p)

        rc, p = self.kz(
            "Q2",
            "--task-id", tid,
            "--artifact-ids", json.dumps([aid]),
            "--conclusion", "VERIFICATION_FAILED",
            "--severity", "high",
            "--summary", "Output check failed on the golden case.",
        )
        self.assertEqual(rc, 0, p)
        vid = p["id"]

        # A second, non-blocking, low-severity verification to prove filtering.
        rc, p = self.kz(
            "Q2",
            "--task-id", tid,
            "--conclusion", "VERIFIED_ACCEPTABLE",
            "--severity", "low",
            "--summary", "Formatting check passed.",
        )
        self.assertEqual(rc, 0, p)
        vid_ok = p["id"]

        # Linkage: the stored verification carries task_id and the artifact id list.
        rc, p = self.kz("Q7", "--kind", "verifier", "--id", vid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["record"]["task_id"], tid, p)
        self.assertEqual(p["record"]["conclusion"], "VERIFICATION_FAILED", p)
        self.assertEqual(json.loads(p["record"]["artifact_ids_json"]), [aid], p)

        rc, p = self.kz("Q9", "--task-id", tid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 2, p)
        self.assertEqual({r["id"] for r in p["records"]}, {vid, vid_ok}, p)

        rc, p = self.kz("Q9", "--task-id", tid, "--severity", "high")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 1, p)
        self.assertEqual(p["records"][0]["id"], vid, p)

        rc, p = self.kz("R0")
        self.assertEqual(rc, 0, p)
        blocker_ids = [b["id"] for b in p["blocking_verifications"]]
        self.assertIn(vid, blocker_ids, p)
        self.assertNotIn(vid_ok, blocker_ids, p)
        self.assertEqual(p["counts"]["blocking_verifications"], 1, p)

        rc, p = self.kz("R4", "--severity", "high")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["rows"], 1, p)
        report = self.root / p["path"]
        self.assertTrue(report.is_file(), p)
        text = report.read_text(encoding="utf-8")
        self.assertIn(vid, text)
        self.assertNotIn(vid_ok, text)

    def test_q2_missing_summary_is_denied(self):
        rc, p = self.kz("Q2", "--task-id", "t1", "--conclusion", "VERIFICATION_FAILED")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_SUMMARY_REQUIRED", p)

    def test_l2_unknown_gotcha_id_is_not_found(self):
        rc, p = self.kz("L2", "--id", "g_00000000000000_deadbeef00")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_RECORD_NOT_FOUND", p)

    def test_r3_severity_filter_is_unsupported(self):
        # --severity applies to R4 (verification_events) only; R3 must deny, not guess.
        rc, p = self.kz("R3", "--severity", "high")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_FILTER_UNSUPPORTED", p)

    # --- Scenario 3: T1 -> T2 -> T4 ---------------------------------------------------

    def test_trace_score_chain_with_aggregates(self):
        rc, p = self.kz(
            "T1",
            "--payload-json",
            json.dumps({"kind": "tool_call", "summary": "Ran the fixture tool once.", "task_id": "task-int-1"}),
        )
        self.assertEqual(rc, 0, p)
        te_id = p["id"]

        rc, p = self.kz(
            "T2",
            "--payload-json",
            json.dumps(
                {
                    "name": "correctness",
                    "value": 0.9,
                    "data_type": "numeric",
                    "source": "deterministic",
                    "trace_event_id": te_id,
                }
            ),
        )
        self.assertEqual(rc, 0, p)
        es_id = p["id"]

        rc, p = self.kz("T4", "--trace-id", te_id)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["aggregates"]["count"], 1, p)
        self.assertEqual(p["aggregates"]["numeric_count"], 1, p)
        self.assertAlmostEqual(p["aggregates"]["mean"], 0.9, places=9)
        self.assertAlmostEqual(p["aggregates"]["min"], 0.9, places=9)
        self.assertAlmostEqual(p["aggregates"]["max"], 0.9, places=9)
        rec = p["records"][0]
        self.assertEqual(rec["id"], es_id, rec)
        self.assertEqual(rec["trace_event_id"], te_id, rec)
        self.assertEqual(rec["data_type"], "numeric", rec)
        self.assertAlmostEqual(rec["value_num"], 0.9, places=9)

    def test_t4_no_matches_returns_zero_aggregates(self):
        rc, p = self.kz("T4", "--trace-id", "te_00000000000000_deadbeef00")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["records"], [], p)
        self.assertEqual(p["aggregates"]["count"], 0, p)
        self.assertEqual(p["aggregates"]["numeric_count"], 0, p)
        self.assertNotIn("mean", p["aggregates"], p)

    def test_t2_score_source_outside_vocabulary_is_denied(self):
        # "verifier" is a trace_kind, not a score_source; the enum gate must reject it.
        rc, p = self.kz(
            "T2",
            "--payload-json",
            json.dumps({"name": "correctness", "value": 0.9, "data_type": "numeric", "source": "verifier"}),
        )
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_SCHEMA_ENUM", p)
        self.assertIn("deterministic", p.get("allowed", []), p)

    # --- Scenario 4: A1 path containment ----------------------------------------------

    def test_a1_path_outside_repo_is_denied_and_writes_nothing(self):
        outside_dir = Path(tempfile.mkdtemp(prefix="kaizen-escape-"))
        self.addCleanup(shutil.rmtree, outside_dir, ignore_errors=True)
        outside_file = outside_dir / "outside.txt"
        outside_file.write_text("external evidence", encoding="utf-8")

        rc, p = self.kz("A1", "--path", str(outside_file))
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_PATH_OUTSIDE_REPO", p)

        rc, p = self.kz("A1", "--path", "..\\escape.txt")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_PATH_OUTSIDE_REPO", p)

        # Both denials must leave the artifact table untouched.
        rc, p = self.kz("A4")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["records"], [], p)

    def test_a1_relative_path_inside_repo_round_trips(self):
        (self.root / "notes.txt").write_text("inside the isolated repo", encoding="utf-8")
        rc, p = self.kz("A1", "--path", "notes.txt")
        self.assertEqual(rc, 0, p)
        aid = p["id"]
        self.assertEqual(p["path"], "notes.txt", p)

        rc, p = self.kz("A3", "--id", aid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["record"]["path"], "notes.txt", p)
        self.assertTrue(_HASH_RE.match(p["record"]["sha256"] or ""), p)

        rc, p = self.kz("A5", "--id", aid)
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["match"], p)


if __name__ == "__main__":
    unittest.main()
