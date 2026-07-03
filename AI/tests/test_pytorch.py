"""PyTorch / sentence-transformers backend selection + the semantic chunker, exercised WITHOUT
the heavy extra installed. Confirms: selecting the in-process embedder is recognized, the absent
extra denies cleanly (rather than crashing), the `neural` chunker is reserved, and `semantic`
chunking requires a configured embedding backend. Tests that need the extra ABSENT are skipped
when it happens to be installed."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import IsolatedDBTest  # noqa: E402


_ST_PRESENT = importlib.util.find_spec("sentence_transformers") is not None
_ST_SELECT = {"KAIZEN_EMBED_BACKEND": "sentence-transformers", "KAIZEN_EMBED_MODEL": ""}
_UNCONFIGURED = {"KAIZEN_EMBED_BACKEND": "ollama", "KAIZEN_EMBED_MODEL": ""}

_DOC = (
    "# Intro\n\nApples and oranges grow on fruit trees.\n\n"
    "Quantum entanglement links distant particles.\n\nGardening needs water and sunlight.\n"
)


class SentenceTransformersSelectionTest(IsolatedDBTest):
    def test_doctor_selects_sentence_transformers(self):
        rc, p = self.kz("B1", env=_ST_SELECT)
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["embedding"]["configured"], p)

    @unittest.skipIf(_ST_PRESENT, "extra installed: graceful-absent path not applicable")
    def test_doctor_unreachable_when_extra_absent(self):
        rc, p = self.kz("B1", env=_ST_SELECT)
        self.assertEqual(rc, 0, p)
        self.assertFalse(p["embedding"].get("reachable", True), p)


class SemanticChunkerTest(IsolatedDBTest):
    def _ingest(self, env: dict) -> str:
        doc = self.root / "doc.md"
        doc.write_text(_DOC, encoding="utf-8")
        rc, ing = self.kz("E1", "--path", str(doc), env=env)
        self.assertEqual(rc, 0, ing)
        return ing["id"]

    def test_neural_chunker_is_reserved(self):
        doc_id = self._ingest(_UNCONFIGURED)
        rc, p = self.kz("E3", "--id", doc_id, "--chunker", "neural", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_CHUNKER_UNSUPPORTED", p)

    def test_semantic_without_backend_denied(self):
        doc_id = self._ingest(_UNCONFIGURED)
        rc, p = self.kz("E3", "--id", doc_id, "--chunker", "semantic", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNCONFIGURED", p)

    @unittest.skipIf(_ST_PRESENT, "extra installed: graceful-absent path not applicable")
    def test_semantic_with_st_selected_but_extra_absent(self):
        doc_id = self._ingest(_ST_SELECT)
        rc, p = self.kz("E3", "--id", doc_id, "--chunker", "semantic", env=_ST_SELECT)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNAVAILABLE", p)


if __name__ == "__main__":
    unittest.main()
