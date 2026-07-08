"""In-process transformers TextBackend: KAIZEN_TEXT_BACKEND=transformers selection + the graceful
-absent path, exercised WITHOUT the heavy extra. Confirms the selector returns the backend, B1
reports it configured (and unreachable when transformers is absent), and B2 denies cleanly. Tests
that need the extra ABSENT are skipped when it happens to be installed."""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import IsolatedDBTest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kaizen_components.backends import get_text_backend  # noqa: E402

_TF_PRESENT = importlib.util.find_spec("transformers") is not None
_TF_SELECT = {"KAIZEN_TEXT_BACKEND": "transformers", "KAIZEN_LLM_MODEL": ""}


class TransformersSelectionUnitTest(unittest.TestCase):
    def test_selector_returns_transformers_backend(self):
        keys = ("KAIZEN_TEXT_BACKEND", "KAIZEN_LLM_BACKEND", "KAIZEN_LLM_MODEL")
        saved = {k: os.environ.get(k) for k in keys}
        try:
            os.environ["KAIZEN_TEXT_BACKEND"] = "transformers"
            os.environ.pop("KAIZEN_LLM_BACKEND", None)
            os.environ.pop("KAIZEN_LLM_MODEL", None)
            backend = get_text_backend()  # lazy: no torch import until chat/probe
            self.assertIsNotNone(backend)
            self.assertEqual(backend.name, "transformers")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class TransformersCliTest(IsolatedDBTest):
    def test_doctor_reports_configured(self):
        rc, p = self.kz("B1", env=_TF_SELECT)
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["text"]["configured"], p)

    @unittest.skipIf(_TF_PRESENT, "extra installed: graceful-absent path not applicable")
    def test_doctor_unreachable_when_extra_absent(self):
        rc, p = self.kz("B1", env=_TF_SELECT)
        self.assertEqual(rc, 0, p)
        self.assertFalse(p["text"].get("reachable", True), p)

    @unittest.skipIf(_TF_PRESENT, "extra installed: graceful-absent path not applicable")
    def test_run_denies_when_extra_absent(self):
        rc, p = self.kz("B2", "--prompt", "hello", env=_TF_SELECT)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNAVAILABLE", p)


if __name__ == "__main__":
    unittest.main()
