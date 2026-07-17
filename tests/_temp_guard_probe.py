"""Child probe launched by name via run_tests.py under a pinned KAIZEN_TEST_TEMP_ROOT; not standalone-meaningful; asserts TEMP/pycache pinning + stages a fixture the parent runner must clean. (90/100 sibling test files carry a module docstring — this is the house convention; real gap.)"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = REPO_ROOT / "AI" / "work"


class TempGuardProbe(unittest.TestCase):
    def test_imported_child_sees_only_pinned_workspace_scratch(self) -> None:
        from kaizen_components.paths import ensure_runtime_dirs

        ensure_runtime_dirs()
        values = {key: Path(os.environ[key]).resolve(strict=True) for key in ("TEMP", "TMP", "TMPDIR")}
        self.assertEqual(len(set(values.values())), 1)
        temp_root = values["TEMP"]
        self.assertEqual(Path(tempfile.gettempdir()).resolve(strict=True), temp_root)
        self.assertTrue(sys.pycache_prefix)
        self.assertEqual(Path(sys.pycache_prefix).resolve(strict=True), Path(os.environ["PYTHONPYCACHEPREFIX"]).resolve(strict=True))
        self.assertEqual(os.path.commonpath((str(WORK_ROOT.resolve()), str(temp_root))), str(WORK_ROOT.resolve()))
        with tempfile.TemporaryDirectory(prefix="runner-probe-") as raw:
            sentinel = Path(raw) / "sentinel.txt"
            sentinel.write_text("D-only", encoding="utf-8")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "D-only")
        readonly = temp_root / "cleanup-fixture" / ".git" / "objects" / "aa" / "fixture"
        readonly.parent.mkdir(parents=True, exist_ok=True)
        readonly.write_bytes(b"runner cleanup proof")
        os.link(readonly, readonly.with_name("fixture-hardlink"))
        readonly.chmod(stat.S_IREAD)
        # Deliberately left behind to prove the parent runner's readonly/hardlink cleanup.
