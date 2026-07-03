"""L* lifecycle ops: learning add/promote/list/query/inspect and the L10 learned-context chain."""

from __future__ import annotations

import re

from _harness import IsolatedDBTest

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_G_TITLE = "Stale cache gotcha"
_G_SUMMARY = "Editor caches drift from their source records."
_G_BODY = "Verified drift between cache and source during review."


class LearningOpsTest(IsolatedDBTest):
    # -- helpers -------------------------------------------------------------

    def _add_gotcha(self, title: str = _G_TITLE, summary: str = _G_SUMMARY, body: str = _G_BODY) -> str:
        rc, p = self.kz("G1", "--title", title, "--summary", summary, "--body", body)
        self.assertEqual(rc, 0, p)
        return p["id"]

    def _add_learning(self, title: str, summary: str, body: str = "Learning body.") -> str:
        rc, p = self.kz("L1", "--title", title, "--summary", summary, "--body", body)
        self.assertEqual(rc, 0, p)
        return p["id"]

    def _full_chain(self, title: str = _G_TITLE, summary: str = _G_SUMMARY) -> tuple[str, str, str]:
        """G1 -> L2 -> L3; returns (gotcha_id, learning_id, learned_id)."""
        gid = self._add_gotcha(title=title, summary=summary)
        rc, p = self.kz("L2", "--id", gid)
        self.assertEqual(rc, 0, p)
        lid = p["id"]
        rc, p = self.kz("L3", "--id", lid)
        self.assertEqual(rc, 0, p)
        return gid, lid, p["id"]

    # -- L1 add / L6 inspect ---------------------------------------------------

    def test_l1_add_and_l6_inspect_round_trip(self):
        rc, p = self.kz(
            "L1",
            "--title", "Pin the cutoff format",
            "--summary", "Lexicographic date filters need one timestamp format.",
            "--body", "All created_at values must come from db.now().",
        )
        self.assertEqual(rc, 0, p)
        lid = p["id"]
        self.assertTrue(lid.startswith("l_"), p)
        self.assertTrue(_HASH_RE.match(p.get("content_hash", "")), p)

        rc, p = self.kz("L6", "--id", lid)
        self.assertEqual(rc, 0, p)
        rec = p["record"]
        self.assertEqual(rec["id"], lid)
        self.assertEqual(rec["title"], "Pin the cutoff format")
        self.assertEqual(rec["summary"], "Lexicographic date filters need one timestamp format.")
        self.assertEqual(rec["body"], "All created_at values must come from db.now().")
        self.assertEqual(rec["scope"], "project")
        self.assertEqual(rec["status"], "active")
        self.assertIsNone(rec["source_gotcha_id"])

    def test_l1_without_title_is_denied(self):
        rc, p = self.kz("L1", "--summary", "No title given.", "--body", "b")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("status"), "DENIED")
        self.assertEqual(p.get("code"), "DENIED_TITLE_REQUIRED")

    # -- L2 promote gotcha -> learning ------------------------------------------

    def test_l2_promotion_copies_fields_and_links_gotcha(self):
        gid = self._add_gotcha()
        rc, p = self.kz("L2", "--id", gid)
        self.assertEqual(rc, 0, p)
        self.assertNotIn("ledger_warning", p, p)
        lid = p["id"]
        self.assertTrue(lid.startswith("l_"), p)

        rc, p = self.kz("L6", "--id", lid)
        self.assertEqual(rc, 0, p)
        rec = p["record"]
        self.assertEqual(rec["source_gotcha_id"], gid)
        self.assertEqual(rec["title"], _G_TITLE)
        self.assertEqual(rec["summary"], _G_SUMMARY)
        self.assertEqual(rec["body"], _G_BODY)
        self.assertEqual(rec["scope"], "project")
        self.assertEqual(rec["source_command"], "L2")

    def test_l2_denials(self):
        rc, p = self.kz("L2")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_ID_REQUIRED")

        rc, p = self.kz("L2", "--id", "g_00000000000000_ffffffffff")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_RECORD_NOT_FOUND")
        self.assertEqual(p.get("table"), "gotcha")

    # -- L3 promote learning -> learned ------------------------------------------

    def test_l3_promotion_copies_fields_and_links_learning(self):
        lid = self._add_learning("Verify before synthesis", "Ground truth beats synthesis-only review.")
        rc, p = self.kz("L3", "--id", lid)
        self.assertEqual(rc, 0, p)
        self.assertNotIn("ledger_warning", p, p)
        ldid = p["id"]
        self.assertTrue(ldid.startswith("ld_"), p)

        rc, p = self.kz("L9", "--id", ldid)
        self.assertEqual(rc, 0, p)
        rec = p["record"]
        self.assertEqual(rec["source_learning_id"], lid)
        self.assertEqual(rec["title"], "Verify before synthesis")
        self.assertEqual(rec["summary"], "Ground truth beats synthesis-only review.")
        self.assertEqual(rec["source_command"], "L3")

    def test_l3_denials(self):
        rc, p = self.kz("L3")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_ID_REQUIRED")

        rc, p = self.kz("L3", "--id", "l_00000000000000_ffffffffff")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_RECORD_NOT_FOUND")
        self.assertEqual(p.get("table"), "learning")

    # -- L4 / L7 list ---------------------------------------------------------

    def test_l4_and_l7_list_return_records(self):
        lid_a = self._add_learning("Learning alpha", "First learning fixture.")
        lid_b = self._add_learning("Learning beta", "Second learning fixture.")
        rc, p = self.kz("L3", "--id", lid_b)
        self.assertEqual(rc, 0, p)
        ldid = p["id"]

        rc, p = self.kz("L4")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 2, p)
        ids = [r["id"] for r in p["records"]]
        self.assertIn(lid_a, ids)
        self.assertIn(lid_b, ids)

        rc, p = self.kz("L7")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 1, p)
        self.assertEqual(p["records"][0]["id"], ldid)
        self.assertEqual(p["records"][0]["title"], "Learning beta")

    # -- L5 / L8 query ----------------------------------------------------------

    def test_l5_and_l8_query_match_and_miss(self):
        lid = self._add_learning("Quantum flux note", "The quantum-flux capacitor drains on idle.")
        rc, p = self.kz("L3", "--id", lid)
        self.assertEqual(rc, 0, p)
        ldid = p["id"]

        rc, p = self.kz("L5", "--query", "quantum-flux")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["query"], "quantum-flux")
        self.assertIn(lid, [r["id"] for r in p["records"]], p)

        rc, p = self.kz("L5", "--query", "zzz-not-present")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 0, p)

        rc, p = self.kz("L8", "--query", "quantum-flux")
        self.assertEqual(rc, 0, p)
        self.assertIn(ldid, [r["id"] for r in p["records"]], p)

        rc, p = self.kz("L8", "--query", "zzz-not-present")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 0, p)

    def test_l5_and_l8_without_query_are_denied(self):
        for code in ("L5", "L8"):
            rc, p = self.kz(code)
            self.assertEqual(rc, 2, f"{code}: {p}")
            self.assertEqual(p.get("code"), "DENIED_QUERY_REQUIRED", f"{code}: {p}")

    # -- L6 / L9 inspect denials -----------------------------------------------

    def test_l6_and_l9_inspect_denials(self):
        for code in ("L6", "L9"):
            rc, p = self.kz(code)
            self.assertEqual(rc, 2, f"{code}: {p}")
            self.assertEqual(p.get("code"), "DENIED_ID_REQUIRED", f"{code}: {p}")

        rc, p = self.kz("L6", "--id", "l_00000000000000_ffffffffff")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_RECORD_NOT_FOUND")
        self.assertEqual(p.get("table"), "learning")

        rc, p = self.kz("L9", "--id", "ld_00000000000000_ffffffffff")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_RECORD_NOT_FOUND")
        self.assertEqual(p.get("table"), "learned")

    # -- L10 learned-context ------------------------------------------------------

    def test_l10_full_chain_context_and_narrative(self):
        gid, lid, ldid = self._full_chain()
        rc, p = self.kz("L10")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 1, p)
        rec = p["records"][0]
        self.assertEqual(rec["id"], ldid)
        self.assertEqual(rec["title"], _G_TITLE)
        self.assertEqual(rec["chain"]["gotcha"]["id"], gid)
        self.assertEqual(rec["chain"]["learning"]["id"], lid)
        # Promotion copies the summary down the chain, so all three segments repeat it.
        expected = f"GOTCHA: {_G_SUMMARY} -> LEARNING: {_G_SUMMARY} -> LEARNED: {_G_SUMMARY}"
        self.assertEqual(rec["narrative"], expected)

    def test_l10_empty_db_then_partial_chain_without_gotcha(self):
        rc, p = self.kz("L10")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 0, p)
        self.assertEqual(p["records"], [], p)

        # A learning added directly (no gotcha) yields a two-segment narrative.
        summary = "Direct learning without a gotcha parent."
        lid = self._add_learning("Direct learning", summary)
        rc, p = self.kz("L3", "--id", lid)
        self.assertEqual(rc, 0, p)

        rc, p = self.kz("L10")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 1, p)
        rec = p["records"][0]
        self.assertEqual(rec["chain"]["learning"]["id"], lid)
        self.assertNotIn("gotcha", rec["chain"], rec)
        self.assertEqual(rec["narrative"], f"LEARNING: {summary} -> LEARNED: {summary}")

    def test_l10_query_filters_learned_records(self):
        _, _, ld_apple = self._full_chain(title="Apple gotcha", summary="Apple-orchard cache never expires.")
        _, _, ld_pear = self._full_chain(title="Pear gotcha", summary="Pear-grove index skips rebuilds.")

        rc, p = self.kz("L10", "--query", "Apple-orchard")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 1, p)
        self.assertEqual(p["records"][0]["id"], ld_apple)
        self.assertNotEqual(p["records"][0]["id"], ld_pear)

        rc, p = self.kz("L10", "--query", "zzz-not-present")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 0, p)

    # -- ledger side effects --------------------------------------------------------

    def test_promotions_auto_write_ledger_events(self):
        gid = self._add_gotcha()
        rc, p = self.kz("L2", "--id", gid)
        self.assertEqual(rc, 0, p)
        self.assertNotIn("ledger_warning", p, p)
        lid = p["id"]

        # The L2 ledger event's body embeds the promotion result, so a ledger
        # report queried by the new learning id must find it.
        rc, p = self.kz("R2", "--query", lid)
        self.assertEqual(rc, 0, p)
        self.assertGreaterEqual(p["rows"], 1, p)
        self.assertTrue((self.root / p["path"]).exists(), p)

        # G1 and L2 each auto-write a ledger event.
        rc, p = self.kz("R2")
        self.assertEqual(rc, 0, p)
        self.assertGreaterEqual(p["rows"], 2, p)


class PromotionLifecycleTest(IsolatedDBTest):
    """A5: promotions mark their source 'promoted'; G5 gives GOTCHAs a status op."""

    def _gotcha(self) -> str:
        rc, p = self.kz("G1", "--title", "Pitfall", "--summary", "One-sentence pitfall.", "--body", "Evidence.")
        self.assertEqual(rc, 0, p)
        return p["id"]

    def test_l2_marks_source_gotcha_promoted_and_leaves_digest(self):
        gid = self._gotcha()
        rc, p = self.kz("L2", "--id", gid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("source_gotcha_status"), "promoted", p)
        self.assertNotIn("gotcha_transition_warning", p, p)
        rc, g = self.kz("G4", "--id", gid)
        self.assertEqual(rc, 0, g)
        self.assertEqual(g["record"]["status"], "promoted", g)
        rc, digest = self.kz("R0")
        self.assertEqual(rc, 0, digest)
        self.assertNotIn(gid, [r["id"] for r in digest["active_gotchas"]], digest)

    def test_l3_marks_source_learning_promoted(self):
        gid = self._gotcha()
        rc, p = self.kz("L2", "--id", gid)
        self.assertEqual(rc, 0, p)
        lid = p["id"]
        rc, p = self.kz("L3", "--id", lid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("source_learning_status"), "promoted", p)
        rc, rec = self.kz("L6", "--id", lid)
        self.assertEqual(rc, 0, rec)
        self.assertEqual(rec["record"]["status"], "promoted", rec)

    def test_g5_round_trip_and_denial(self):
        gid = self._gotcha()
        rc, p = self.kz("G5", "--id", gid, "--status", "resolved", "--summary", "Resolved after the fix landed.")
        self.assertEqual(rc, 0, p)
        rc, g = self.kz("G4", "--id", gid)
        self.assertEqual(rc, 0, g)
        self.assertEqual(g["record"]["status"], "resolved", g)
        rc, p = self.kz("G5", "--status", "resolved")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_ID_REQUIRED", p)
