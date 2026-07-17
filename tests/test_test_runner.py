"""Self-tests that the canonical runner confines scratch to AI/work and rejects an external KAIZEN_TEST_TEMP_ROOT."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

from lane_manifest import EXTENSION_MODULES, LIVE_MODULES, PLATFORM_MODULES, SLOW_MODULES, lane_modules, root_test_modules, validate_manifest
from run_tests import _child_environment, _resolve_test_selection


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "tests" / "run_tests.py"
CASE_ROOT = REPO_ROOT / "AI" / "work" / "test-temp" / f"runner-selftest-{os.getpid()}"


class CanonicalTestRunnerTest(unittest.TestCase):
    def test_default_is_only_the_fast_core_lane(self) -> None:
        selected, name = _resolve_test_selection([]) or ([], "")
        self.assertEqual(name, "core")
        self.assertEqual(selected, list(lane_modules("core")))
        self.assertIn("test_schema", selected)
        self.assertIn("test_workflow_static", selected)
        self.assertTrue(set(selected).isdisjoint(PLATFORM_MODULES | SLOW_MODULES | LIVE_MODULES | EXTENSION_MODULES))

    def test_expensive_and_unreleased_lanes_are_explicit(self) -> None:
        for lane in ("slow", "live", "extension"):
            selected, name = _resolve_test_selection(["--lane", lane]) or ([], "")
            self.assertEqual(name, lane)
            self.assertEqual(selected, list(lane_modules(lane)))
        self.assertIn("test_test_extension", lane_modules("extension"))
        self.assertIn("test_session_drive", lane_modules("slow"))

    def test_unclassified_module_fails_closed(self) -> None:
        with mock.patch("lane_manifest.root_test_modules", return_value=root_test_modules() | {"test_unclassified"}):
            with self.assertRaisesRegex(RuntimeError, "unclassified"):
                validate_manifest()

    def test_ambient_live_gate_is_honored_only_by_explicit_live_lane(self) -> None:
        with mock.patch.dict(os.environ, {"KAIZEN_RUN_LIVE": "1"}):
            self.assertNotIn("KAIZEN_RUN_LIVE", _child_environment("core"))
            self.assertNotIn("KAIZEN_RUN_LIVE", _child_environment("slow"))
            self.assertNotIn("KAIZEN_RUN_LIVE", _child_environment("explicit unittest selection"))
            self.assertEqual(_child_environment("core+live")["KAIZEN_RUN_LIVE"], "1")

    def test_targeted_unittest_selection_remains_supported(self) -> None:
        selected, name = _resolve_test_selection(["test_skill_context.SkillContextTest"]) or ([], "")
        self.assertEqual(name, "explicit unittest selection")
        self.assertEqual(selected, ["test_skill_context.SkillContextTest"])

    def test_list_lanes_does_not_prepare_test_scratch(self) -> None:
        before = set((REPO_ROOT / "AI" / "work" / "test-temp").glob("python-suite-*"))
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--list-lanes"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("core", proc.stdout)
        self.assertIn("slow", proc.stdout)
        after = set((REPO_ROOT / "AI" / "work" / "test-temp").glob("python-suite-*"))
        self.assertEqual(after, before)

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
