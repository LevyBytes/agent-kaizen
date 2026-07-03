"""Model-backend ops (B*) + the embedding seam in E3/E4, all exercised WITHOUT a live model
server. Confirms the opt-in/graceful contract: no backend configured -> deterministic chunking +
lexical search still work; configured-but-unreachable -> the doctor reports it and the ops deny
cleanly rather than crashing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import IsolatedDBTest  # noqa: E402


# Force "no backend configured" regardless of the outer environment.
_UNCONFIGURED = {"KAIZEN_EMBED_MODEL": "", "KAIZEN_LLM_MODEL": ""}


class BackendDoctorTest(IsolatedDBTest):
    def test_doctor_unconfigured(self):
        rc, p = self.kz("B1", env=_UNCONFIGURED)
        self.assertEqual(rc, 0, p)
        self.assertFalse(p["embedding"]["configured"], p)
        self.assertFalse(p["text"]["configured"], p)

    def test_doctor_configured_but_unreachable(self):
        env = {"KAIZEN_EMBED_MODEL": "nomic-embed-text", "KAIZEN_EMBED_BASE_URL": "http://127.0.0.1:6/v1"}
        rc, p = self.kz("B1", env=env)
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["embedding"]["configured"], p)
        self.assertFalse(p["embedding"].get("reachable", True), p)


class BackendUnconfiguredDenialTest(IsolatedDBTest):
    def test_model_run_requires_backend(self):
        rc, p = self.kz("B2", "--prompt", "hello", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNCONFIGURED", p)

    def test_reembed_requires_backend(self):
        rc, p = self.kz("B3", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNCONFIGURED", p)

    def test_semantic_query_requires_backend(self):
        rc, p = self.kz("E4", "--query", "anything", "--semantic", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNCONFIGURED", p)


class EvidenceNoBackendTest(IsolatedDBTest):
    def test_chunk_without_backend_then_lexical_query(self):
        doc = self.root / "doc.md"
        doc.write_text("# Title\n\nAlpha bravo charlie.\n\nDelta echo foxtrot.\n", encoding="utf-8")
        rc, ing = self.kz("E1", "--path", str(doc), env=_UNCONFIGURED)
        self.assertEqual(rc, 0, ing)
        doc_id = ing["id"]

        rc2, ch = self.kz("E3", "--id", doc_id, env=_UNCONFIGURED)
        self.assertEqual(rc2, 0, ch)
        self.assertFalse(ch.get("embedded"), ch)          # no backend -> no embeddings stored
        self.assertIsNone(ch.get("embedding_model"), ch)

        rc3, q = self.kz("E4", "--query", "bravo", env=_UNCONFIGURED)
        self.assertEqual(rc3, 0, q)
        self.assertEqual(q.get("mode"), "like", q)         # graceful lexical baseline
        self.assertTrue(any("bravo" in (r.get("snippet") or "") for r in q.get("records", [])), q)


if __name__ == "__main__":
    unittest.main()
