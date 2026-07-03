"""Private policy ops (X1-X5): add, list, query, inspect, and session-context."""

from __future__ import annotations

import re

from _harness import IsolatedDBTest

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class PolicyOpsTest(IsolatedDBTest):
    def _add(self, title: str, **flags: str) -> str:
        """Add a policy rule via X1 (summary is engine-required) and return its id."""
        flags.setdefault("summary", f"{title} summary.")
        args = ["X1", "--title", title]
        for key, value in flags.items():
            args.extend([f"--{key.replace('_', '-')}", value])
        rc, p = self.kz(*args)
        self.assertEqual(rc, 0, p)
        return p["id"]

    # ---- X1 policy-add -------------------------------------------------

    def test_x1_without_title_is_denied(self):
        rc, p = self.kz("X1", "--summary", "No title supplied.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("status"), "DENIED")
        self.assertEqual(p.get("code"), "DENIED_TITLE_REQUIRED")

    def test_x1_without_summary_is_denied(self):
        rc, p = self.kz("X1", "--title", "Title but no summary")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_SUMMARY_REQUIRED")

    def test_x1_add_returns_id_revision_and_hash(self):
        rc, p = self.kz(
            "X1",
            "--title", "Never push without review",
            "--summary", "Pushes require an explicit owner request.",
            "--body", "Agents never run git push unless the owner asked.",
        )
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK")
        self.assertTrue(p.get("id"), p)
        self.assertTrue(p.get("revision_id"), p)
        self.assertNotEqual(p["id"], p["revision_id"], p)
        self.assertTrue(_HASH_RE.match(p.get("content_hash", "")), p)

    def test_x1_defaults_round_trip_via_inspect(self):
        # X1 with just title+summary fills the field defaults; X4 must read them back.
        pid = self._add("Defaults probe")
        rc, p = self.kz("X4", "--id", pid)
        self.assertEqual(rc, 0, p)
        rec = p["record"]
        self.assertEqual(rec["scope"], "project", rec)
        self.assertEqual(rec["trigger"], "session-start", rec)
        self.assertEqual(rec["priority"], "normal", rec)
        self.assertEqual(rec["status"], "active", rec)
        self.assertEqual(rec["writer_role"], "agent", rec)
        self.assertEqual(rec["source_command"], "X1", rec)

    # ---- X2 policy-list ------------------------------------------------

    def test_x2_lists_added_rules_sorted_critical_first(self):
        low = self._add("Low rule", priority="low", summary="Low priority rule.")
        crit = self._add("Critical rule", priority="critical", summary="Critical priority rule.")
        rc, p = self.kz("X2")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("count"), 2, p)
        ids = [r["id"] for r in p["records"]]
        # priority rank sorts critical (0) ahead of low (3) regardless of insert order
        self.assertEqual(ids, [crit, low], p)
        by_id = {r["id"]: r for r in p["records"]}
        self.assertEqual(by_id[low]["title"], "Low rule", p)
        self.assertEqual(by_id[low]["summary"], "Low priority rule.", p)
        self.assertEqual(by_id[crit]["status"], "active", p)

    # ---- X3 policy-query -----------------------------------------------

    def test_x3_without_query_is_denied(self):
        rc, p = self.kz("X3")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("status"), "DENIED")
        self.assertEqual(p.get("code"), "DENIED_QUERY_REQUIRED")

    def test_x3_finds_rule_by_title_text(self):
        pid = self._add("Quarantine flaky suites", summary="Flaky suites get quarantined.")
        self._add("Unrelated rule", summary="Nothing shared here.")
        rc, p = self.kz("X3", "--query", "Quarantine flaky")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("query"), "Quarantine flaky", p)
        self.assertEqual([r["id"] for r in p["records"]], [pid], p)
        rc, p = self.kz("X3", "--query", "zz-no-such-token")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("count"), 0, p)

    # ---- X4 policy-inspect ---------------------------------------------

    def test_x4_without_id_is_denied(self):
        rc, p = self.kz("X4")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_ID_REQUIRED")

    def test_x4_unknown_id_is_not_found(self):
        rc, p = self.kz("X4", "--id", "pol-does-not-exist")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_RECORD_NOT_FOUND")

    def test_x4_returns_full_record_linked_to_revision(self):
        rc, add = self.kz(
            "X1",
            "--title", "Inspect me",
            "--summary", "Round-trip inspection target.",
            "--body", "Body text for inspection.",
            "--trigger", "pre-commit",
            "--priority", "high",
        )
        self.assertEqual(rc, 0, add)
        rc, p = self.kz("X4", "--id", add["id"])
        self.assertEqual(rc, 0, p)
        rec = p["record"]
        self.assertEqual(rec["id"], add["id"], rec)
        self.assertEqual(rec["title"], "Inspect me", rec)
        self.assertEqual(rec["summary"], "Round-trip inspection target.", rec)
        self.assertEqual(rec["body"], "Body text for inspection.", rec)
        self.assertEqual(rec["trigger"], "pre-commit", rec)
        self.assertEqual(rec["priority"], "high", rec)
        # linkage back to the X1 payload: revision pointer and content hash
        self.assertEqual(rec["current_revision_id"], add["revision_id"], rec)
        self.assertEqual(rec["content_hash"], add["content_hash"], rec)

    # ---- X5 policy-session-context ---------------------------------------

    def test_x5_empty_db_is_ok_with_zero_count(self):
        rc, p = self.kz("X5")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK")
        self.assertEqual(p.get("count"), 0, p)
        self.assertEqual(p.get("records"), [], p)

    def test_x5_required_action_mentions_reload_after_compaction(self):
        rc, p = self.kz("X5")
        self.assertEqual(rc, 0, p)
        action = p.get("required_action", "")
        self.assertIn("reload", action, p)
        self.assertIn("compaction", action, p)

    def test_x5_sorts_records_critical_first(self):
        low = self._add("Low bar", priority="low")
        crit = self._add("Critical bar", priority="critical")
        high = self._add("High bar", priority="high")
        rc, p = self.kz("X5")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("count"), 3, p)
        self.assertEqual([r["id"] for r in p["records"]], [crit, high, low], p)

    def test_x5_returns_only_active_rules(self):
        active = self._add("Active rule")
        retired = self._add("Retired rule", status="retired")
        rc, p = self.kz("X5")
        self.assertEqual(rc, 0, p)
        ids = [r["id"] for r in p["records"]]
        self.assertIn(active, ids, p)
        self.assertNotIn(retired, ids, p)
        # the retired rule still exists in the DB (X2 does not filter by status)
        rc, p = self.kz("X2")
        self.assertEqual(rc, 0, p)
        self.assertIn(retired, [r["id"] for r in p["records"]], p)

    def test_x5_trigger_filter_includes_session_start_rules(self):
        start = self._add("Session start rule")  # default trigger session-start
        precommit = self._add("Pre-commit rule", trigger="pre-commit")
        deploy = self._add("Deploy rule", trigger="deploy")
        rc, p = self.kz("X5", "--trigger", "pre-commit")
        self.assertEqual(rc, 0, p)
        ids = [r["id"] for r in p["records"]]
        self.assertIn(precommit, ids, p)
        self.assertIn(start, ids, p)
        self.assertNotIn(deploy, ids, p)
        # unfiltered X5 sees all three active rules regardless of trigger
        rc, p = self.kz("X5")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("count"), 3, p)


if __name__ == "__main__":
    import unittest

    unittest.main()

class PriorityWindowTest(IsolatedDBTest):
    """A4 regression: the LIMIT window is priority-aware, so an OLD critical rule can no
    longer be pushed out of X5/X2 by newer low-priority rules (fails on recency-only SQL)."""

    def _add(self, title: str, priority: str) -> str:
        rc, p = self.kz("X1", "--title", title, "--summary", "One-sentence rule.", "--priority", priority)
        self.assertEqual(rc, 0, p)
        return p["id"]

    def test_old_critical_rule_survives_recency_window(self):
        critical_id = self._add("Old critical rule", "critical")
        for index in range(3):
            self._add(f"Newer normal rule {index}", "normal")
        rc, p = self.kz("X5", "--limit", "3")
        self.assertEqual(rc, 0, p)
        self.assertIn(critical_id, [r["id"] for r in p["records"]], p)
        rc, p = self.kz("X2", "--limit", "3")
        self.assertEqual(rc, 0, p)
        self.assertIn(critical_id, [r["id"] for r in p["records"]], p)
