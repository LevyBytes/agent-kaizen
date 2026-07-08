"""Static guardrail (fast, no model load): the four backend default models and the Ollama
installer/doc judge ref must equal the approved, validated plan pins (see
AI/work/backend-capability-plan/research-sources.md). This catches any default drifting from the
approved model without loading anything; each model is exercised live in test_model_integration.py."""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import REPO_ROOT  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))  # import the backend modules to read their _DEFAULT_MODEL constants

# Single source of truth: the approved, validated defaults.
APPROVED = {
    "sentence_transformers_backend": "codefuse-ai/F2LLM-v2-1.7B",                            # embedder
    "transformers_backend": "Qwen/Qwen3.5-9B",                                              # in-process text
    "cross_encoder_backend": "cross-encoder/ettin-reranker-150m-v1",                        # reranker
    "gliner_pii_backend": "fastino/gliner2-privacy-filter-PII-multi",                       # PII
}
OLLAMA_JUDGE_GGUF = "hf.co/unsloth/Qwen3.5-9B-GGUF:Q4_K_M"  # judge served via Ollama GGUF


class ModelPinTest(unittest.TestCase):
    def test_backend_defaults_match_approved_plan(self):
        for module, expected in APPROVED.items():
            mod = importlib.import_module(f"kaizen_components.backends.{module}")
            self.assertEqual(mod._DEFAULT_MODEL, expected, f"{module}._DEFAULT_MODEL drifted from the plan")

    def test_ollama_installer_and_docs_default_to_qwen_judge(self):
        for rel in ("setup/install-ollama.ps1", "setup/install-ollama.sh", "setup/OLLAMA.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn(OLLAMA_JUDGE_GGUF, text, f"{rel} must default the judge/chat model to Qwen3.5-9B GGUF")


if __name__ == "__main__":
    unittest.main()
