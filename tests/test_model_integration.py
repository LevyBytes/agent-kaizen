"""LIVE model integration: each approved plan model, exercised AS INTENDED, writing is_test=1 records
to the REAL DB that K7 purge-test removes in teardown. This is the suite that proves the models are
actually used -- and it asserts the ACTUAL model identity, so any default drifting from the approved
model fails here rather than passing silently.

GATED: runs only with KAIZEN_RUN_LIVE=1, because it loads real models (download / VRAM / heat):
    $env:KAIZEN_RUN_LIVE=1; <venv>\\Scripts\\python.exe -m unittest discover -s tests -p test_model_integration.py

Backends must be installed (requirements-pytorch.txt) and Ollama serving the Qwen judge (setup/OLLAMA.md).
Records are written to the real AI/db with --test (is_test=1) and purged by K7 in tearDownClass, so the
suite leaves the DB clean (per the deletable-test-records design)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import KAIZEN, REPO_ROOT  # noqa: E402

_LIVE = os.environ.get("KAIZEN_RUN_LIVE") == "1"

# Approved, validated plan pins (research-sources.md). The live backends must report THESE.
JUDGE_MODEL = "hf.co/unsloth/Qwen3.5-9B-GGUF:Q4_K_M"
JUDGE_MARK = "Qwen3.5-9B"           # must appear in the judge trace's model (the approved judge)
EMBED_MARK = "F2LLM-v2-1.7B"
PII_MODEL = "fastino/gliner2-privacy-filter-PII-multi"

_EMBED_ENV = {"KAIZEN_EMBED_BACKEND": "sentence-transformers"}
_RERANK_ENV = {"KAIZEN_EMBED_BACKEND": "sentence-transformers", "KAIZEN_RERANK_BACKEND": "sentence-transformers"}
_PII_ENV = {"KAIZEN_PII_BACKEND": "gliner2", "KAIZEN_PII_MODEL": PII_MODEL}
_JUDGE_ENV = {"KAIZEN_LLM_MODEL": JUDGE_MODEL, "KAIZEN_LLM_BASE_URL": "http://127.0.0.1:11434/v1"}
_DOCTOR_ENV = {**_EMBED_ENV, **_JUDGE_ENV}          # B1 probes both embedding + text lanes
_JUDGE_PII_ENV = {**_JUDGE_ENV, **_PII_ENV}         # B4 with the PII backend on -> advisory PII attach fires
_CONTRACT = "kz-live-contract"                      # O2/O4 improvement-lab contract for this suite


def _kz(*args: str, env: dict | None = None) -> tuple[int, dict]:
    """Run kaizen.py against the REAL DB (NO KAIZEN_REPO_ROOT override) with --json."""
    full = dict(os.environ)
    full.pop("KAIZEN_REPO_ROOT", None)  # real plane, per the deletable-real-records directive
    if env:
        full.update(env)
    proc = subprocess.run(
        [sys.executable, str(KAIZEN), *args, "--json"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=full,
        timeout=600,
    )
    raw = proc.stdout.strip() or proc.stderr.strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"_stdout": proc.stdout, "_stderr": proc.stderr}
    return proc.returncode, payload


@unittest.skipUnless(_LIVE, "live model integration -- set KAIZEN_RUN_LIVE=1 to run (loads real models)")
class LiveModelIntegrationTest(unittest.TestCase):
    """Ordered: setUpClass ingests+embeds a fixture corpus (embedder); tests query it (rerank), scan
    PII (gliner), and judge (Qwen). tearDownClass purges every is_test=1 row this suite wrote."""

    @classmethod
    def setUpClass(cls) -> None:
        rc, _ = _kz("K1")  # ensure the real DB + is_test columns exist
        assert rc == 0, "K1 init failed"
        cls.addClassCleanup(_kz, "K7")
        cls._tmp = Path(tempfile.mkdtemp(prefix="kz-live-"))
        cls.addClassCleanup(shutil.rmtree, cls._tmp, ignore_errors=True)
        cls._fixture = cls._tmp / "corpus.md"
        cls._fixture.write_text(
            "# Retrieval fixture\n\n"
            "The reranker reorders retrieved candidates with a cross-encoder.\n"
            "Vector search ranks evidence chunks by cosine distance.\n"
            "Advisory models never gate acceptance in the Kaizen harness.\n"
            "Semantic chunking was excluded per Qu et al.\n",
            encoding="utf-8",
        )
        # Embedder AS INTENDED: ingest -> semantic (embedded) chunking. Marked is_test on the document.
        rc, ing = _kz("E1", "--path", str(cls._fixture), "--allow-external",
                      "--summary", "[live] embedder+reranker corpus", "--test", env=_EMBED_ENV)
        assert rc == 0, f"E1 failed: {ing}"
        cls._doc_id = ing["id"]
        rc, cls._chunked = _kz("E3", "--id", cls._doc_id, "--chunker", "semantic", "--test", env=_EMBED_ENV)
        assert rc == 0, f"E3 failed: {cls._chunked}"

    def test_1_embedder_used_and_semantic_search(self):
        self.assertTrue(self._chunked.get("embedded"), self._chunked)          # chunks were embedded
        self.assertIn(EMBED_MARK, json.dumps(self._chunked), self._chunked)    # F2LLM-v2-1.7B actually used
        rc, se = _kz("E4", "--semantic", "--query", "how does the reranker reorder results", "--test", env=_EMBED_ENV)
        self.assertEqual(rc, 0, se)
        self.assertEqual(se.get("mode"), "semantic", se)
        self.assertTrue(se.get("records"), se)                                 # vector search returned hits

    def test_2_reranker_used(self):
        # --rerank draws candidates from the LIKE lexical baseline, so the query must lexically overlap
        # the corpus (a substring of a fixture sentence) for the cross-encoder to have rows to reorder.
        rc, rr = _kz("E4", "--rerank", "--query", "reranker reorders retrieved candidates", "--test", env=_RERANK_ENV)
        self.assertEqual(rc, 0, rr)
        self.assertIn("rerank", str(rr.get("mode")), rr)                       # ettin cross-encoder ran
        self.assertTrue(rr.get("records"), rr)                                 # candidates were retrieved + reranked
        self.assertIsNotNone(rr["records"][0].get("rerank_score"), rr)         # each row carries a cross-encoder score

    def test_3_pii_gliner_detects(self):
        rc, p = _kz("B5", "--prompt", "Contact John Doe at john.doe@example.com; SSN 123-45-6789.",
                    "--test", env=_PII_ENV)
        self.assertEqual(rc, 0, p)
        self.assertGreater(len(p.get("model_hits", [])), 0, p)                 # gliner2 actually detected PII

    def test_4_judge_uses_approved_qwen_model(self):
        rc, b = _kz("B4", "--prompt", "The Earth orbits the Sun.",
                    "--query", "Is the statement factually correct? Give a 0-1 score.", "--test", env=_JUDGE_ENV)
        self.assertEqual(rc, 0, b)
        self.assertTrue(b.get("advisory"), b)
        self.assertIn(JUDGE_MARK, str(b.get("model")), b)                      # the approved Qwen3.5-9B judge
        self.assertIsNotNone(b.get("verdict"), b)

    def test_5_hybrid_uses_embedder(self):
        # --hybrid fuses the lexical + vector rank lists with RRF; the vector leg needs the embedder.
        rc, h = _kz("E4", "--hybrid", "--query", "reranker reorders retrieved candidates", "--test", env=_EMBED_ENV)
        self.assertEqual(rc, 0, h)
        self.assertIn("hybrid", str(h.get("mode")), h)                         # RRF fusion ran
        self.assertTrue(h.get("records"), h)

    def test_6_advisory_text_run(self):
        # B2 advisory model-run through the Qwen text backend, recorded as a model_call trace (is_test).
        rc, b = _kz("B2", "--prompt", "In one sentence, state that model verdicts stay advisory.",
                    "--test", env=_JUDGE_ENV)
        self.assertEqual(rc, 0, b)
        self.assertTrue(b.get("advisory"), b)
        self.assertIn(JUDGE_MARK, str(b.get("model")), b)                      # the Qwen text backend ran

    def test_7_doctor_and_reembed_use_embedder(self):
        # B1 doctor probes both lanes; B3 backfills embeddings on a fresh recursive-chunked doc.
        rc, d = _kz("B1", env=_DOCTOR_ENV)
        self.assertEqual(rc, 0, d)
        self.assertTrue(d.get("embedding", {}).get("configured"), d)
        self.assertTrue(d.get("text", {}).get("configured"), d)

        doc = self._tmp / "reembed.md"
        doc.write_text("Fixed-size recursive chunking is the shipped baseline.\n", encoding="utf-8")
        rc, ing = _kz("E1", "--path", str(doc), "--allow-external", "--summary", "[live] reembed fixture", "--test")
        self.assertEqual(rc, 0, ing)
        rc, _ = _kz("E3", "--id", ing["id"], "--chunker", "recursive", "--test")  # no embeddings yet
        self.assertEqual(rc, 0)
        rc, re = _kz("B3", "--id", ing["id"], "--test", env=_EMBED_ENV)         # embedder backfills them
        self.assertEqual(rc, 0, re)
        self.assertGreaterEqual(re.get("reembedded", 0), 1, re)

    def test_8_judge_with_pii_attaches(self):
        # B4 with the PII backend on: the advisory PII scan runs on the candidate and attaches to the result.
        rc, b = _kz("B4", "--prompt", "The quarterly report was prepared by Jane Smith in Toronto for Acme Corp.",
                    "--query", "Is the statement coherent? Give a 0-1 score.", "--test", env=_JUDGE_PII_ENV)
        self.assertEqual(rc, 0, b)
        self.assertTrue(b.get("advisory"), b)
        self.assertTrue(b.get("pii_advisory"), b)                              # gliner2 attached label+span_hash hits
        self.assertTrue(all("span_hash" in h and "label" in h for h in b["pii_advisory"]), b)

    def test_9_lab_evaluate_judges_proposals(self):
        # O4: seed one eval case + one proposal, then the judge scores the proposal against the case.
        rc, _ = _kz("Q3", "--title", "advisory verdict case", "--summary", "The candidate should be advisory-only.",
                    "--expected-json", '{"pass": true}', "--test")
        self.assertEqual(rc, 0)
        rc, _ = _kz("O2", "--contract", _CONTRACT, "--title", "advisory phrasing",
                    "--summary", "State plainly that the verdict is advisory.", "--test")
        self.assertEqual(rc, 0)
        rc, o = _kz("O4", "--contract", _CONTRACT, "--metric", "advisory", "--limit", "1", "--test", env=_JUDGE_ENV)
        self.assertEqual(rc, 0, o)
        self.assertTrue(o.get("advisory"), o)
        self.assertGreaterEqual(o.get("evaluated", 0), 1, o)                   # the judge scored the proposal

    def test_a_dedup_gotcha_uses_embedder(self):
        # O5 --kind gotcha clusters lifecycle records by cosine over the REAL embedder (not stored vectors).
        _kz("G1", "--title", "Windows quote stripping",
            "--summary", "PowerShell strips quotes inside JSON arguments on Windows.", "--test")
        _kz("G1", "--title", "PowerShell JSON quoting",
            "--summary", "On Windows, PowerShell removes quotes within JSON-valued arguments.", "--test")
        rc, d = _kz("O5", "--kind", "gotcha", "--threshold", "0.5", "--test", env=_EMBED_ENV)
        self.assertEqual(rc, 0, d)
        self.assertEqual(d.get("source"), "embedded", d)                       # embedded from title+summary text
        self.assertGreaterEqual(d.get("records", 0), 2, d)
        self.assertTrue(d.get("path"), d)

    def test_b_monitor_feed_shows_all_lanes(self):
        # The B6 monitor feed must reflect ALL model usage, not just judge/text-gen: run one op per
        # non-judge lane, then assert the embedder / reranker / PII lanes surface in recent_activity
        # (the exact feed the GUI reads). Each op carries --test so its observability trace is purgeable.
        _kz("E4", "--semantic", "--query", "vector search ranks evidence chunks", "--test", env=_EMBED_ENV)
        _kz("E4", "--rerank", "--query", "reranker reorders retrieved candidates", "--test", env=_RERANK_ENV)
        _kz("B5", "--prompt", "Reach Jane Smith at jane@example.com.", "--test", env=_PII_ENV)
        rc, snap = _kz("B6", "--limit", "25")
        self.assertEqual(rc, 0, snap)
        lanes = {r.get("lane") for r in snap.get("recent", [])}
        for lane in ("embedding", "rerank", "pii"):
            self.assertIn(lane, lanes, snap)

    def test_zz_records_are_purgeable(self):
        # Everything above wrote is_test=1 rows across many tables (evidence/traces/pii/gotcha/proposals/
        # eval_cases/eval_scores/reports); K7 must find + remove them all (tearDownClass repeats it).
        rc, k = _kz("K7")
        self.assertEqual(rc, 0, k)
        self.assertGreaterEqual(k.get("total", 0), 1, k)                       # our test rows existed and were purged
        rc, k2 = _kz("K7")
        self.assertEqual(k2.get("total"), 0, k2)                              # idempotent: nothing left


if __name__ == "__main__":
    unittest.main()
