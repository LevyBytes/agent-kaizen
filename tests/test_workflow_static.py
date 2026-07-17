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

    def test_unreleased_extension_is_not_a_public_ci_gate(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("actions/setup-node@", text)
        self.assertNotRegex(text, r"(?i)working-directory\s*:\s*['\"]?\.?[/\\]?extension\b")
        self.assertNotRegex(text, r"(?i)\b(?:npm|npx)(?:\.cmd)?\b")
        self.assertNotIn("tests/extension", text)
        self.assertNotRegex(text, r"--lane(?:=|\s+)extension\b")
        self.assertNotIn("test_test_extension", text)

    def test_required_ci_uses_only_core_and_bounded_platform_lanes(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        lane_args = re.findall(r"(?m)^\s+lane_args:\s*(.+)$", text)
        self.assertEqual(lane_args, ["--lane core --lane platform", "--lane platform"])
        self.assertIn("run: python tests/run_tests.py ${{ matrix.lane_args }}", text)
        self.assertNotIn("run: python tests/run_tests.py\n", text)

    def test_slow_lane_requires_an_explicit_manual_dispatch(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("run_slow:", text)
        self.assertIn("if: github.event_name == 'workflow_dispatch' && inputs.run_slow", text)
        self.assertEqual(text.count("run: python tests/run_tests.py --lane slow"), 1)
        self.assertNotIn("--lane live", text)
        self.assertNotIn("--lane extension", text)


if __name__ == "__main__":
    unittest.main()
