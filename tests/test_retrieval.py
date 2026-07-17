"""E4 retrieval extras: --hybrid Reciprocal Rank Fusion + --rerank cross-encoder staging.
Deterministic RRF unit test plus CLI deny/degrade paths through the isolated harness. The heavy
sentence-transformers extra is NOT required; tests that need it absent are skipped when present."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import IsolatedDBTest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kaizen_components.evidence import _rrf_fuse  # noqa: E402

_ST_PRESENT = importlib.util.find_spec("sentence_transformers") is not None
_UNCONFIGURED = {"KAIZEN_EMBED_BACKEND": "ollama", "KAIZEN_EMBED_MODEL": ""}
_RERANK_ST = {"KAIZEN_RERANK_BACKEND": "sentence-transformers", "KAIZEN_RERANK_MODEL": ""}

_DOC = (
    "# Intro\n\nApples and oranges grow on fruit trees.\n\n"
    "Quantum entanglement links distant particles.\n\nGardening needs water and sunlight.\n"
)


def _rec(chunk_id: str) -> dict:
    return {"id": chunk_id, "snippet": chunk_id, "score": 0, "_text": chunk_id}


class RrfFusionUnitTest(unittest.TestCase):
    def test_agreement_ranks_first_and_is_deterministic(self):
        lexical = [_rec("a"), _rec("b"), _rec("c")]
        vector = [_rec("b"), _rec("a"), _rec("d")]
        fused = _rrf_fuse([lexical, vector])
        ids = [r["id"] for r in fused]
        self.assertEqual(set(ids[:2]), {"a", "b"}, ids)  # both-list ids beat single-list ids
        self.assertEqual(fused, _rrf_fuse([lexical, vector]))  # deterministic
        self.assertTrue(all("_text" in r for r in fused))  # internal field carried for rerank

    def test_empty_lists_fuse_to_empty(self):
        self.assertEqual(_rrf_fuse([[], []]), [])


class HybridRerankCliTest(IsolatedDBTest):
    def _ingest(self) -> None:
        doc = self.root / "doc.md"
        doc.write_text(_DOC, encoding="utf-8")
        rc, ing = self.kz("E1", "--path", str(doc), env=_UNCONFIGURED)
        self.assertEqual(rc, 0, ing)
        rc2, ch = self.kz("E3", "--id", ing["id"], env=_UNCONFIGURED)  # E1 ingests; E3 makes chunks
        self.assertEqual(rc2, 0, ch)

    def test_lexical_baseline_still_works(self):
        self._ingest()
        rc, p = self.kz("E4", "--query", "fruit", env=_UNCONFIGURED)
        self.assertEqual(rc, 0, p)
        self.assertIn(p["mode"], ("like", "fts"), p)
        self.assertTrue(p["records"], p)

    def test_hybrid_without_embed_backend_degrades_to_lexical(self):
        self._ingest()
        rc, p = self.kz("E4", "--query", "fruit", "--hybrid", env=_UNCONFIGURED)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["mode"], "hybrid(lexical-only)", p)

    def test_rerank_without_backend_denied(self):
        self._ingest()
        rc, p = self.kz("E4", "--query", "fruit", "--rerank", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNCONFIGURED", p)

    @unittest.skipIf(_ST_PRESENT, "extra installed: graceful-absent path not applicable")
    def test_rerank_st_selected_but_extra_absent(self):
        self._ingest()
        rc, p = self.kz("E4", "--query", "fruit", "--rerank", env={**_UNCONFIGURED, **_RERANK_ST})
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNAVAILABLE", p)


if __name__ == "__main__":
    unittest.main()
