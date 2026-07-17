"""Static contracts for repository CI workflows."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"


class DependencyAuditWorkflowTest(unittest.TestCase):
    def test_pip_audit_scans_every_pinned_requirement_set_once(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        invocations = re.findall(r"(?m)^\s*pip-audit\b([^\r\n]*)$", text)
        self.assertEqual(len(invocations), 1, "tests.yml must have one auditable pip-audit invocation")
        requirements = re.findall(r"(?:^|\s)-r\s+(\S+)", invocations[0])
        self.assertEqual(
            requirements,
            ["requirements-kaizen.txt", "requirements-docs.txt", "requirements-pytorch.txt"],
        )


if __name__ == "__main__":
    unittest.main()
