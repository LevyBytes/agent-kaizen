"""B4 model-judge + O4 lab-evaluate: advisory LLM-as-judge. Deny / dry-run / schema paths through
the isolated harness -- no live model. Confirms the judge denies cleanly when unconfigured, --dry-run
builds the prompt without calling, the rubric/candidate are required, and 'judge' is a valid trace kind."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import IsolatedDBTest  # noqa: E402

_UNCONFIGURED = {"KAIZEN_LLM_MODEL": ""}
# Configured (constructor is lazy, so --dry-run never touches the network / this endpoint):
_CONFIGURED = {"KAIZEN_LLM_MODEL": "hf.co/unsloth/Qwen3.5-9B-GGUF:Q4_K_M", "KAIZEN_LLM_BASE_URL": "http://127.0.0.1:11434/v1"}


class JudgeDenyDryRunTest(IsolatedDBTest):
    def test_b4_denies_without_backend(self):
        rc, p = self.kz("B4", "--prompt", "candidate", "--query", "rubric", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNCONFIGURED", p)

    def test_b4_dry_run_builds_prompt_without_calling(self):
        rc, p = self.kz("B4", "--prompt", "candidate output", "--query", "must be terse", "--dry-run", env=_CONFIGURED)
        self.assertEqual(rc, 0, p)
        self.assertTrue(p.get("dry_run"), p)
        self.assertIn("prompt_sha256", p)
        self.assertTrue(p.get("advisory"), p)

    def test_b4_requires_rubric(self):
        rc, p = self.kz("B4", "--prompt", "candidate", "--dry-run", env=_CONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_RUBRIC_REQUIRED", p)

    def test_b4_requires_candidate(self):
        rc, p = self.kz("B4", "--query", "rubric", "--dry-run", env=_CONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_CANDIDATE_REQUIRED", p)

    def test_o4_denies_without_backend(self):
        rc, p = self.kz("O4", "--contract", "chunking", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNCONFIGURED", p)

    def test_judge_is_a_valid_trace_kind(self):
        rc, p = self.kz("Q8", "--kind", "trace_event", "--payload-json", '{"kind": "judge", "summary": "advisory judge verdict."}')
        self.assertEqual(rc, 0, p)
        self.assertTrue(p.get("valid"), p)


if __name__ == "__main__":
    unittest.main()
