"""Report-op tests: R0 session digest, R1-R6 report files, R4 filters/extras, R9/R10 windows."""

from __future__ import annotations

import hashlib
import unittest

from _harness import IsolatedDBTest


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReportOpsTest(IsolatedDBTest):
    # ---- R0 session digest -------------------------------------------------

    def test_r0_empty_db_is_pure_read_with_zero_counts(self):
        rc, p = self.kz("R0")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK")
        self.assertEqual(
            p.get("counts"),
            {
                "policies_active": 0,
                "gotchas_active": 0,
                "blocking_verifications": 0,
                "learned_total": 0,
                "tasks_active": 0,
                "active_tasks_without_ledger": 0,
                "ledger_events_last_7d": 0,
                "verifications_last_7d": 0,
                "agent_runs_active": 0,
                "agent_runs_waiting_approval": 0,
                "live_children": 0,
                "parent_completed_with_live_children": 0,
            },
            p,
        )
        for section in (
            "policies", "active_gotchas", "blocking_verifications", "recent_learned", "active_tasks",
            "active_agent_runs", "waiting_approvals",
        ):
            self.assertEqual(p.get(section), [], f"{section}: {p}")
        self.assertIn("R0 after compaction", p.get("required_action", ""), p)
        # Pure read: no report file path and no new record id in the payload.
        self.assertNotIn("path", p)
        self.assertNotIn("id", p)

    def test_r0_seeded_digest_surfaces_each_section(self):
        rc, p = self.kz("X1", "--title", "Pin the venv", "--summary", "Always use the project venv.")
        self.assertEqual(rc, 0, p)
        pol_id = p["id"]
        rc, p = self.kz(
            "G1",
            "--title", "Cache drift",
            "--summary", "Caches drift from source.",
            "--body", "Evidence-driven note.",
        )
        self.assertEqual(rc, 0, p)
        gid = p["id"]
        rc, p = self.kz("Q2", "--conclusion", "VERIFICATION_FAILED", "--summary", "Check failed on rerun.")
        self.assertEqual(rc, 0, p)
        qid = p["id"]
        rc, p = self.kz("L2", "--id", gid)
        self.assertEqual(rc, 0, p)
        lid = p["id"]
        rc, p = self.kz("L3", "--id", lid)
        self.assertEqual(rc, 0, p)
        learned_id = p["id"]
        # A second, unpromoted gotcha: the promoted one (gid) must leave active_gotchas.
        rc, p = self.kz(
            "G1", "--title", "Open pitfall",
            "--summary", "Still open pitfall.", "--body", "Evidence-driven note.",
        )
        self.assertEqual(rc, 0, p)
        open_gid = p["id"]
        rc, p = self.kz("W1", "--title", "Ship digest", "--summary", "Track the digest work.")
        self.assertEqual(rc, 0, p)
        tid = p["id"]

        rc, digest = self.kz("R0")
        self.assertEqual(rc, 0, digest)
        policy = next((r for r in digest["policies"] if r["id"] == pol_id), None)
        self.assertIsNotNone(policy, digest)
        self.assertEqual(policy["body"], "")  # full body column is surfaced, even when empty
        active_ids = [r["id"] for r in digest["active_gotchas"]]
        self.assertIn(open_gid, active_ids, digest)
        self.assertNotIn(gid, active_ids, digest)  # promoted by L2 -> status 'promoted'
        blocker = next((r for r in digest["blocking_verifications"] if r["id"] == qid), None)
        self.assertIsNotNone(blocker, digest)
        self.assertEqual(blocker["conclusion"], "VERIFICATION_FAILED")
        self.assertIn(learned_id, [r["id"] for r in digest["recent_learned"]], digest)
        task = next((r for r in digest["active_tasks"] if r["id"] == tid), None)
        self.assertIsNotNone(task, digest)
        self.assertEqual(task["status"], "active")
        counts = digest["counts"]
        self.assertEqual(counts["policies_active"], 1, counts)
        self.assertEqual(counts["gotchas_active"], 1, counts)
        self.assertEqual(counts["blocking_verifications"], 1, counts)
        self.assertEqual(counts["learned_total"], 1, counts)
        self.assertEqual(counts["tasks_active"], 1, counts)
        # A9 drift signals: the W1 task has no --task-id-linked ledger event yet, and every
        # lifecycle write above auto-appended a ledger row inside the 7-day window.
        self.assertEqual(counts["active_tasks_without_ledger"], 1, counts)
        self.assertGreaterEqual(counts["ledger_events_last_7d"], 4, counts)
        self.assertEqual(counts["verifications_last_7d"], 1, counts)

    def test_r0_policies_sorted_by_priority(self):
        rc, p = self.kz("X1", "--title", "Low rule", "--summary", "Low priority rule.", "--priority", "low")
        self.assertEqual(rc, 0, p)
        low_id = p["id"]
        rc, p = self.kz("X1", "--title", "Critical rule", "--summary", "Critical priority rule.", "--priority", "critical")
        self.assertEqual(rc, 0, p)
        critical_id = p["id"]
        rc, digest = self.kz("R0")
        self.assertEqual(rc, 0, digest)
        ordered = [r["id"] for r in digest["policies"]]
        self.assertEqual(ordered, [critical_id, low_id], digest)

    def test_r0_excludes_inactive_and_non_blocking_records(self):
        rc, p = self.kz(
            "G1",
            "--title", "Resolved pitfall",
            "--summary", "Already resolved.",
            "--body", "b",
            "--status", "resolved",
        )
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("W1", "--title", "Done task", "--summary", "Finished work.", "--status", "done")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("Q2", "--conclusion", "VERIFIED_ACCEPTABLE", "--summary", "Verified clean.")
        self.assertEqual(rc, 0, p)
        rc, digest = self.kz("R0")
        self.assertEqual(rc, 0, digest)
        self.assertEqual(digest["active_gotchas"], [], digest)
        self.assertEqual(digest["active_tasks"], [], digest)
        self.assertEqual(digest["blocking_verifications"], [], digest)
        self.assertEqual(digest["counts"]["gotchas_active"], 0, digest)
        self.assertEqual(digest["counts"]["tasks_active"], 0, digest)
        self.assertEqual(digest["counts"]["blocking_verifications"], 0, digest)

    def test_r0_limit_caps_lists_but_not_counts(self):
        for n in range(3):
            rc, p = self.kz("G1", "--title", f"Pitfall {n}", "--summary", f"Pitfall number {n}.", "--body", "b")
            self.assertEqual(rc, 0, p)
        rc, digest = self.kz("R0", "--limit", "2")
        self.assertEqual(rc, 0, digest)
        self.assertEqual(len(digest["active_gotchas"]), 2, digest)
        self.assertEqual(digest["counts"]["gotchas_active"], 3, digest)

    # ---- promotion linkage (feeds R0 recent_learned) -----------------------

    def test_promotion_chain_linkage_round_trip(self):
        rc, p = self.kz("G1", "--title", "Chain pitfall", "--summary", "Chain source gotcha.", "--body", "Chain body.")
        self.assertEqual(rc, 0, p)
        gid = p["id"]
        rc, p = self.kz("L2", "--id", gid)
        self.assertEqual(rc, 0, p)
        lid = p["id"]
        rc, p = self.kz("L6", "--id", lid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["record"]["source_gotcha_id"], gid, p)
        self.assertEqual(p["record"]["title"], "Chain pitfall", p)
        rc, p = self.kz("L3", "--id", lid)
        self.assertEqual(rc, 0, p)
        learned_id = p["id"]
        rc, p = self.kz("L9", "--id", learned_id)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["record"]["source_learning_id"], lid, p)
        self.assertEqual(p["record"]["summary"], "Chain source gotcha.", p)

    # ---- R1-R6 report files ------------------------------------------------

    def test_r1_to_r6_write_report_files_after_seeding(self):
        # Seed one row per report table (W1 also writes the ledger event R2 reads).
        rc, p = self.kz("W1", "--title", "Report task", "--summary", "Seed the tasks table.")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("L1", "--title", "Report learning", "--summary", "Seed the learning table.")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("Q2", "--summary", "Seed the verification table.")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("Q3", "--title", "Report case", "--summary", "Seed the eval case table.")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz(
            "S1",
            "--source-id", "report-src",
            "--authority-tier", "official_docs",
            "--url-or-repository", "https://example.test/docs",
            "--summary", "Seed the source lock table.",
        )
        self.assertEqual(rc, 0, p)

        for code in ("R1", "R2", "R3", "R4", "R5", "R6"):
            rc, p = self.kz(code)
            self.assertEqual(rc, 0, f"{code}: {p}")
            self.assertEqual(p.get("status"), "OK", f"{code}: {p}")
            self.assertGreaterEqual(p.get("rows", 0), 1, f"{code}: {p}")
            report = self.root / p["path"]
            self.assertTrue(report.is_file(), f"{code}: missing {report}")
            self.assertGreater(report.stat().st_size, 0, f"{code}: empty {report}")
            self.assertEqual(p.get("sha256"), _sha256(report), f"{code}: {p}")

    # ---- R4 extras and filters ----------------------------------------------

    def test_r4_line_carries_conclusion_and_severity_extras(self):
        rc, p = self.kz(
            "Q2",
            "--conclusion", "VERIFICATION_FAILED",
            "--severity", "high",
            "--summary", "Severity extras probe.",
        )
        self.assertEqual(rc, 0, p)
        qid = p["id"]
        rc, p = self.kz("R4")
        self.assertEqual(rc, 0, p)
        self.assertEqual(
            p.get("columns"),
            ["id", "created_at", "conclusion", "severity", "actionability", "task_id", "summary"],
            p,
        )
        text = (self.root / p["path"]).read_text(encoding="utf-8")
        self.assertIn(f"`{qid}`", text)
        self.assertIn("[conclusion=VERIFICATION_FAILED severity=high]", text)
        self.assertIn("Severity extras probe.", text)

    def test_r4_severity_filter_narrows_rows(self):
        rc, p = self.kz("Q2", "--severity", "high", "--summary", "High severity probe.")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("Q2", "--severity", "low", "--summary", "Low severity probe.")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("R4", "--severity", "high")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("rows"), 1, p)
        text = (self.root / p["path"]).read_text(encoding="utf-8")
        self.assertIn("High severity probe.", text)
        self.assertNotIn("Low severity probe.", text)

    def test_r4_actionability_filter_narrows_rows(self):
        rc, p = self.kz("Q2", "--actionability", "now", "--summary", "Actionable now probe.")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("Q2", "--actionability", "later", "--summary", "Actionable later probe.")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("R4", "--actionability", "now")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("rows"), 1, p)
        text = (self.root / p["path"]).read_text(encoding="utf-8")
        self.assertIn("Actionable now probe.", text)
        self.assertNotIn("Actionable later probe.", text)

    def test_proof_filters_denied_on_non_proof_reports(self):
        rc, p = self.kz("R1", "--severity", "high")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("status"), "DENIED")
        self.assertEqual(p.get("code"), "DENIED_FILTER_UNSUPPORTED")
        self.assertEqual(p.get("flag"), "--severity")
        self.assertEqual(p.get("report"), "R1")
        rc, p = self.kz("R2", "--actionability", "now")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_FILTER_UNSUPPORTED")
        self.assertEqual(p.get("flag"), "--actionability")

    # ---- R9/R10 windows ------------------------------------------------------

    def test_r9_r10_windows_include_fresh_ledger_event(self):
        rc, p = self.kz("W1", "--title", "Window seed", "--summary", "Seed a fresh ledger event.")
        self.assertEqual(rc, 0, p)
        for code in ("R9", "R10"):
            rc, p = self.kz(code)
            self.assertEqual(rc, 0, f"{code}: {p}")
            self.assertGreaterEqual(p.get("rows", 0), 1, f"{code}: {p}")
            self.assertTrue((self.root / p["path"]).is_file(), f"{code}: {p}")


if __name__ == "__main__":
    unittest.main()

class DriftCountsTest(IsolatedDBTest):
    """A9: advisory drift signals in the R0 digest (observability, not enforcement)."""

    def test_task_ledger_linkage_drives_drift_count(self):
        rc, p = self.kz("W1", "--title", "Drifting task", "--summary", "Task with no linked ledger yet.")
        self.assertEqual(rc, 0, p)
        tid = p["id"]
        rc, digest = self.kz("R0")
        self.assertEqual(rc, 0, digest)
        self.assertEqual(digest["counts"]["active_tasks_without_ledger"], 1, digest)
        self.assertGreaterEqual(digest["counts"]["ledger_events_last_7d"], 1, digest)
        self.assertEqual(digest["counts"]["verifications_last_7d"], 0, digest)
        rc, p = self.kz("W2", "--id", tid, "--task-id", tid, "--summary", "Linked ledger update.")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("Q2", "--task-id", tid, "--conclusion", "VERIFIED_ACCEPTABLE", "--summary", "Checked.")
        self.assertEqual(rc, 0, p)
        rc, digest = self.kz("R0")
        self.assertEqual(rc, 0, digest)
        self.assertEqual(digest["counts"]["active_tasks_without_ledger"], 0, digest)
        self.assertEqual(digest["counts"]["verifications_last_7d"], 1, digest)
