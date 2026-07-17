"""Self-tests that the canonical runner confines scratch to AI/work and rejects an external KAIZEN_TEST_TEMP_ROOT."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "tests" / "run_tests.py"
CASE_ROOT = REPO_ROOT / "AI" / "work" / "test-temp" / f"runner-selftest-{os.getpid()}"


class CanonicalTestRunnerTest(unittest.TestCase):
    def test_imported_child_receives_pinned_workspace_temp_and_cleanup(self) -> None:
        if os.path.lexists(CASE_ROOT):
            self.assertFalse(CASE_ROOT.is_symlink())
            shutil.rmtree(CASE_ROOT)
        CASE_ROOT.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, CASE_ROOT, True)
        env = dict(os.environ)
        env["KAIZEN_TEST_TEMP_ROOT"] = str(CASE_ROOT)
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "_temp_guard_probe.TempGuardProbe"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(list(CASE_ROOT.glob("python-suite-*")), [], "the child runner must clean its per-run temp root")

    def test_explicit_root_outside_ai_work_is_rejected_before_child_import(self) -> None:
        env = dict(os.environ)
        env["KAIZEN_TEST_TEMP_ROOT"] = str(REPO_ROOT / "extension" / "test")
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "_temp_guard_probe.TempGuardProbe"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("test temp base escaped AI/work", proc.stdout + proc.stderr)
