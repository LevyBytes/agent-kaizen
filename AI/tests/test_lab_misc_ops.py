"""Improvement lab (O1-O3), source export (S4), evidence inspect (E5), and new redaction classes."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unittest
from pathlib import Path

# redaction.py imports only `re` and `.denials` (no DB), so importing it in-process is safe.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kaizen_components import redaction  # noqa: E402

from _harness import IsolatedDBTest  # noqa: E402

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class LabOpsTest(IsolatedDBTest):
    def _propose(self, contract: str, title: str, baseline: float | None = None, candidate: float | None = None) -> str:
        args = [
            "O2",
            "--contract", contract,
            "--title", title,
            "--summary", "Candidate variant recorded for ranking.",
            "--metric", "accuracy",
        ]
        if baseline is not None:
            args += ["--baseline-score", str(baseline)]
        if candidate is not None:
            args += ["--candidate-score", str(candidate)]
        rc, p = self.kz(*args)
        self.assertEqual(rc, 0, p)
        return p["id"]

    def test_lab_ops_require_contract(self):
        for code in ("O1", "O2", "O3"):
            rc, p = self.kz(code)
            self.assertEqual(rc, 2, f"{code}: {p}")
            self.assertEqual(p.get("code"), "DENIED_CONTRACT_REQUIRED", f"{code}: {p}")

    def test_o1_assembles_seeded_caseset_and_promotion_linkage(self):
        # Seed one eval case (Q3).
        rc, p = self.kz(
            "Q3",
            "--title", "Summary stays short",
            "--summary", "Output summary must be one sentence.",
            "--body", "Given a long diff, the summary stays under the limit.",
        )
        self.assertEqual(rc, 0, p)
        case_id = p["id"]

        # Seed an ACTIVE gotcha, then promote it G1 -> L2 -> L3 to create a learned exemplar.
        rc, p = self.kz(
            "G1",
            "--title", "Prompt drift pitfall",
            "--summary", "Prompt variants drift from the contract.",
            "--body", "Observed drift when the contract text is not restated.",
        )
        self.assertEqual(rc, 0, p)
        gotcha_id = p["id"]

        rc, p = self.kz("L2", "--id", gotcha_id)
        self.assertEqual(rc, 0, p)
        learning_id = p["id"]
        rc, p = self.kz("L6", "--id", learning_id)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["record"]["source_gotcha_id"], gotcha_id, p)

        rc, p = self.kz("L3", "--id", learning_id)
        self.assertEqual(rc, 0, p)
        learned_id = p["id"]
        rc, p = self.kz("L9", "--id", learned_id)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["record"]["source_learning_id"], learning_id, p)

        # A resolved gotcha must NOT count toward active_gotchas.
        rc, p = self.kz(
            "G1",
            "--title", "Old resolved pitfall",
            "--summary", "Already fixed pitfall.",
            "--body", "Kept for history only.",
            "--status", "resolved",
        )
        self.assertEqual(rc, 0, p)
        resolved_id = p["id"]

        # The promoted gotcha now carries status 'promoted' (L2 transition), so a separate
        # still-active gotcha supplies the failure-signal slot in the case set.
        rc, p = self.kz(
            "G1",
            "--title", "Open pitfall",
            "--summary", "Still open pitfall.",
            "--body", "Not yet promoted.",
        )
        self.assertEqual(rc, 0, p)
        open_id = p["id"]

        rc, p = self.kz("O1", "--contract", "Review Loop")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["eval_cases"], 1, p)
        self.assertEqual(p["exemplars"], 1, p)
        self.assertEqual(p["active_gotchas"], 1, p)
        self.assertTrue(p["path"].endswith("lab-caseset-review-loop.md"), p)
        self.assertTrue(_HASH_RE.match(p.get("sha256", "")), p)

        caseset = self.root / p["path"]
        self.assertTrue(caseset.is_file(), caseset)
        content = caseset.read_text(encoding="utf-8")
        self.assertIn(f"`{case_id}`", content)
        self.assertIn(f"`{learned_id}`", content)
        self.assertIn(f"`{open_id}`", content)
        self.assertNotIn(f"`{gotcha_id}`", content)  # promoted -> no longer an active failure signal
        self.assertNotIn(f"`{resolved_id}`", content)

        # --category filters eval cases only; the other sections are unaffected.
        rc, p = self.kz("O1", "--contract", "Review Loop", "--category", "no-such-category")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["eval_cases"], 0, p)
        self.assertEqual(p["exemplars"], 1, p)
        self.assertEqual(p["active_gotchas"], 1, p)

    def test_o1_empty_db_writes_report_with_zero_counts(self):
        rc, p = self.kz("O1", "--contract", "fresh-contract")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["contract"], "fresh-contract", p)
        self.assertEqual((p["eval_cases"], p["exemplars"], p["active_gotchas"]), (0, 0, 0), p)
        self.assertTrue((self.root / p["path"]).is_file(), p)

    def test_o2_requires_title(self):
        rc, p = self.kz("O2", "--contract", "review-loop")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_TITLE_REQUIRED", p)

    def test_o2_payload_json_must_be_object(self):
        rc, p = self.kz("O2", "--contract", "review-loop", "--title", "variant", "--payload-json", "[1,2]")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_PAYLOAD_TYPE", p)

    def test_o2_success_records_proposal(self):
        rc, p = self.kz(
            "O2",
            "--contract", "review-loop",
            "--title", "Restate contract in prompt",
            "--summary", "Variant restates the contract before answering.",
            "--metric", "accuracy",
            "--baseline-score", "0.5",
            "--candidate-score", "0.8",
        )
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["status"], "OK", p)
        self.assertTrue(p["id"].startswith("ip"), p)
        self.assertTrue(_HASH_RE.match(p.get("content_hash", "")), p)

    def test_o3_ranks_by_delta_and_writes_report(self):
        small = self._propose("review-loop", "small gain", baseline=0.5, candidate=0.6)
        big = self._propose("review-loop", "big gain", baseline=0.5, candidate=0.8)
        rc, p = self.kz("O3", "--contract", "review-loop")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["proposals"], 2, p)
        self.assertEqual(p["top_proposal"]["id"], big, p)
        self.assertEqual(p["top_proposal"]["baseline_score"], 0.5, p)
        self.assertEqual(p["top_proposal"]["candidate_score"], 0.8, p)
        self.assertEqual(p["top_proposal"]["metric"], "accuracy", p)
        self.assertTrue(p["path"].endswith("lab-report-review-loop.md"), p)
        report = self.root / p["path"]
        self.assertTrue(report.is_file(), report)
        content = report.read_text(encoding="utf-8")
        self.assertIn(f"`{big}`", content)
        self.assertIn(f"`{small}`", content)

    def test_o3_unscored_proposals_rank_below_scored(self):
        # A fully scored proposal outranks an unscored one even with a NEGATIVE delta.
        unscored = self._propose("review-loop", "unscored idea")
        regression = self._propose("review-loop", "scored regression", baseline=0.9, candidate=0.4)
        rc, p = self.kz("O3", "--contract", "review-loop")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["proposals"], 2, p)
        self.assertEqual(p["top_proposal"]["id"], regression, p)
        self.assertNotEqual(p["top_proposal"]["id"], unscored, p)


class SourceExportTest(IsolatedDBTest):
    def test_s4_exports_source_locks_round_trip(self):
        rc, p = self.kz("S4")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 0, p)

        for source_id, url in (("zeta-docs", "https://docs.example.org/zeta"), ("alpha-docs", "https://docs.example.org/alpha")):
            rc, p = self.kz(
                "S1",
                "--source-id", source_id,
                "--authority-tier", "official_docs",
                "--url-or-repository", url,
                "--summary", "Documentation root for the export test.",
            )
            self.assertEqual(rc, 0, p)

        rc, p = self.kz("S4")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 2, p)
        self.assertEqual(p["path"], "AI/db/exports/sources.lock.json", p)
        lock_file = self.root / p["path"]
        self.assertTrue(lock_file.is_file(), lock_file)
        self.assertEqual(hashlib.sha256(lock_file.read_bytes()).hexdigest(), p["sha256"], p)

        data = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertIn("generated_at", data)
        # S4 orders by source_id; both seeded sources round-trip with their fields.
        self.assertEqual([s["source_id"] for s in data["sources"]], ["alpha-docs", "zeta-docs"], data)
        self.assertEqual(data["sources"][0]["url_or_repository"], "https://docs.example.org/alpha", data)
        self.assertEqual(data["sources"][0]["authority_tier"], "official_docs", data)


class EvidenceInspectTest(IsolatedDBTest):
    def test_e1_then_e5_document_round_trip(self):
        note = self.root / "evidence-note.md"
        note.write_text("# Heading\n\nFirst paragraph of evidence.\n\nSecond paragraph of evidence.\n", encoding="utf-8")
        rc, p = self.kz("E1", "--path", str(note), "--summary", "Ingest a small note for inspection.")
        self.assertEqual(rc, 0, p)
        doc_id = p["id"]
        source_lock_id = p["source_lock_id"]
        block_count = p["block_count"]
        sha = p["sha256"]
        self.assertEqual(block_count, 3, p)

        rc, p = self.kz("E5", "--id", doc_id)
        self.assertEqual(rc, 0, p)
        record = p["record"]
        self.assertEqual(record["id"], doc_id, record)
        self.assertEqual(record["source_lock_id"], source_lock_id, record)
        self.assertEqual(record["origin_ref"], "evidence-note.md", record)
        self.assertEqual(record["block_count"], block_count, record)
        self.assertEqual(record["origin_kind"], "file", record)

        # The auto-created source lock (provenance) is inspectable and carries the file hash.
        rc, p = self.kz("S3", "--id", source_lock_id)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["record"]["content_hash"], sha, p)
        self.assertEqual(p["record"]["url_or_repository"], "evidence-note.md", p)

    def test_e5_requires_id_and_validates_kind(self):
        rc, p = self.kz("E5")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_ID_REQUIRED", p)

        rc, p = self.kz("E5", "--id", "ed-anything", "--kind", "paragraph")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_KIND_INVALID", p)

    def test_e5_unknown_id_is_not_found(self):
        rc, p = self.kz("E5", "--id", "ed-does-not-exist")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_RECORD_NOT_FOUND", p)
        self.assertEqual(p.get("table"), "evidence_documents", p)


class RedactionNewPatternsTest(unittest.TestCase):
    def test_anthropic_key_reports_own_class_not_openai(self):
        text = "key sk-ant-api03-notreal-notreal-0123456789 in log"  # not a real credential
        hits = redaction.scan_for_secrets(text)
        self.assertIn("anthropic_key", hits, hits)
        self.assertNotIn("openai_key", hits, hits)

    def test_new_secret_classes_detected(self):
        cases = {
            "anthropic_key": "sk-ant-api03-notreal-notreal-0123456789",  # not a real credential
            "huggingface_token": "hf_abcdefghijklmnopqrstuvwx",  # not a real credential
            "stripe_key": "sk_test_fakefakefake1234",  # not a real credential
            "azure_account_key": "AccountKey=abc123abc123abc123abc123abc123abc123abc123==",  # not a real credential
            "db_url_credentials": "postgres://fakeuser:fakepass@db.internal.example/app",  # not a real credential
        }
        for expected, text in cases.items():
            hits = redaction.scan_for_secrets(text)
            self.assertIn(expected, hits, f"{expected} not detected in {text!r} (got {hits})")

    def test_stripe_live_and_restricted_variants(self):
        for text in (
            "sk_live_fakefakefake1234",  # not a real credential
            "rk_live_fakefakefake1234",  # not a real credential
        ):
            hits = redaction.scan_for_secrets(text)
            self.assertIn("stripe_key", hits, f"stripe_key not detected in {text!r} (got {hits})")

    def test_near_miss_values_not_flagged(self):
        # Too short for the huggingface pattern (needs 20+ token chars).
        self.assertNotIn("huggingface_token", redaction.scan_for_secrets("hf_short123"))
        # A connection URL WITHOUT inline user:pass credentials is fine.
        self.assertNotIn("db_url_credentials", redaction.scan_for_secrets("postgres://db.example.com/app"))


if __name__ == "__main__":
    unittest.main()
