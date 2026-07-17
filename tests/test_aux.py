"""Regression for the prompt-builder import-time crash on a fresh clone.

skill-package-prompt-builder.py used to read external skill-drafting scripts at module
top level; on a clone without the skills junction that raised FileNotFoundError before
the script could run. The read is now guarded, so importing it must always succeed and
the helper must return a stub string for a missing file.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import REPO_ROOT

PROMPT_BUILDER = REPO_ROOT / "prompt-builder-scripts" / "ai-support" / "skill-package-prompt-builder.py"


class PromptBuilderImportTest(unittest.TestCase):
    def test_imports_and_canonical_checker_is_guarded(self):
        spec = importlib.util.spec_from_file_location("spb_under_test", PROMPT_BUILDER)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        # Register before exec so @dataclass definitions can resolve their own module.
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)  # must not raise (was the Blocker)

        stub = module._read_canonical_checker("definitely-missing-checker-xyz.py")
        self.assertIsInstance(stub, str)
        self.assertTrue(stub.startswith("#"), stub)

        with tempfile.TemporaryDirectory(prefix="prompt-builder-", dir=REPO_ROOT / "AI" / "work") as temp:
            checker = Path(temp) / ".agents" / "skills" / "skill-drafting" / "scripts" / "present.py"
            checker.parent.mkdir(parents=True)
            checker.write_text("print('present')\n", encoding="utf-8")
            module.REPO_ROOT = Path(temp)
            self.assertEqual(module._read_canonical_checker("present.py"), "print('present')\n")

        self.assertIsInstance(module.CANONICAL_CLAUDE_USAGE_CHECKER, str)
        self.assertIsInstance(module.CANONICAL_CODEX_USAGE_CHECKER, str)
