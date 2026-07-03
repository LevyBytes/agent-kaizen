"""Migration ops M1-M5: scan, dry-run, apply, verify, and report against an isolated root."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from _harness import IsolatedDBTest

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_STUB_MARKER = "This file is a Kaizen command surface"
_SURFACES = ("GOTCHA.md", "LEARNED.md", "LEARNING.md")  # sorted(SURFACE_NAMES) order
_NO_SKILLS = {"KAIZEN_SKILLS_ROOT": ""}  # pin the allowlist to the isolated root only


class MigrationOpsTest(IsolatedDBTest):
    # -- helpers -----------------------------------------------------------

    def _snapshot(self) -> dict[str, str]:
        """Hash every file (and list every dir) under the root, excluding AI/."""
        listing: dict[str, str] = {}
        for path in self.root.rglob("*"):
            rel = path.relative_to(self.root)
            if rel.parts and rel.parts[0] == "AI":
                continue
            if path.is_file():
                listing[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            elif path.is_dir():
                listing[rel.as_posix() + "/"] = "dir"
        return listing

    def _outside_dir(self) -> Path:
        outside = Path(tempfile.mkdtemp(prefix="kaizen-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        return outside

    def _make_skill_project(self, name: str = "proj") -> Path:
        proj = self.root / name
        proj.mkdir()
        (proj / "SKILL.md").write_text("# Demo skill\n", encoding="utf-8")
        return proj

    # -- M1 scan -----------------------------------------------------------

    def test_m1_scan_default_root_lists_three_surface_targets(self):
        rc, p = self.kz("M1")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["status"], "OK")
        self.assertEqual(len(p["roots"]), 1, p)
        rec = p["roots"][0]
        self.assertTrue(rec["exists"], rec)
        self.assertEqual(Path(rec["root"]), self.root.resolve())
        self.assertEqual(rec["count"], 3, rec)
        names = [Path(f["target_path"]).name for f in rec["files"]]
        self.assertEqual(names, list(_SURFACES))
        for entry in rec["files"]:
            self.assertEqual(Path(entry["target_path"]).parent, (self.root / "evals").resolve())
            self.assertFalse(entry["target_exists"], entry)
            self.assertIsNone(entry["target_sha256"], entry)
            self.assertFalse(entry["old_sibling_exists"], entry)

    def test_m1_scan_detects_skill_dir_and_legacy_sibling(self):
        proj = self._make_skill_project()
        (proj / "GOTCHA.md").write_text("legacy gotcha notes\n", encoding="utf-8")
        rc, p = self.kz("M1")
        self.assertEqual(rc, 0, p)
        rec = p["roots"][0]
        self.assertEqual(rec["count"], 6, rec)  # root itself + proj, 3 surfaces each
        matches = [
            f for f in rec["files"]
            if Path(f["old_sibling_path"]) == (proj / "GOTCHA.md").resolve()
        ]
        self.assertEqual(len(matches), 1, rec)
        entry = matches[0]
        self.assertTrue(entry["old_sibling_exists"], entry)
        self.assertFalse(entry["old_sibling_stub"], entry)
        self.assertTrue(_HASH_RE.match(entry["old_sibling_sha256"] or ""), entry)
        self.assertFalse(entry["target_exists"], entry)

    def test_m1_scan_explicit_subdirectory_root_is_allowed(self):
        proj = self._make_skill_project()
        rc, p = self.kz("M1", "--root", str(proj))
        self.assertEqual(rc, 0, p)
        rec = p["roots"][0]
        self.assertEqual(Path(rec["root"]), proj.resolve())
        self.assertTrue(rec["exists"], rec)
        self.assertEqual(rec["count"], 3, rec)

    def test_m1_scan_missing_root_under_allowlist_reports_exists_false(self):
        rc, p = self.kz("M1", "--root", str(self.root / "not-there"))
        self.assertEqual(rc, 0, p)
        rec = p["roots"][0]
        self.assertFalse(rec["exists"], rec)
        self.assertEqual(rec["files"], [])

    def test_m1_scan_disallowed_root_is_denied(self):
        outside = self._outside_dir()
        rc, p = self.kz("M1", "--root", str(outside), env=_NO_SKILLS)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p["status"], "DENIED")
        self.assertEqual(p["code"], "DENIED_ROOT_NOT_ALLOWLISTED")
        self.assertEqual(Path(p["root"]), outside.resolve())
        self.assertIn(self.root.resolve(), [Path(a) for a in p["allowed_roots"]])

    def test_skills_root_env_extends_allowlist(self):
        skills = Path(tempfile.mkdtemp(prefix="kaizen-skills-"))
        self.addCleanup(shutil.rmtree, skills, ignore_errors=True)
        toolkit = skills / "toolkit"
        toolkit.mkdir()
        (toolkit / "SKILL.md").write_text("# Toolkit skill\n", encoding="utf-8")
        rc, p = self.kz("M1", "--root", str(skills), env=_NO_SKILLS)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p["code"], "DENIED_ROOT_NOT_ALLOWLISTED")
        rc, p = self.kz("M1", "--root", str(skills), env={"KAIZEN_SKILLS_ROOT": str(skills)})
        self.assertEqual(rc, 0, p)
        rec = p["roots"][0]
        self.assertTrue(rec["exists"], rec)
        self.assertEqual(rec["count"], 6, rec)  # skills root + toolkit dir

    # -- M2 dry-run ---------------------------------------------------------

    def test_m2_dry_run_reports_create_stub_and_writes_nothing(self):
        before = self._snapshot()
        rc, p = self.kz("M2")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["status"], "OK")
        self.assertIs(p["dry_run"], True)
        self.assertEqual(p["count"], 3, p)
        self.assertEqual({a["action"] for a in p["actions"]}, {"create-stub"})
        for action in p["actions"]:
            self.assertIsNone(action["sha256"], action)
            self.assertEqual(action["path"], action["target_path"], action)
        self.assertEqual(self._snapshot(), before, "dry-run must not create or modify files")
        self.assertFalse((self.root / "evals").exists())

    def test_m2_dry_run_classifies_replace_remove_relocate_and_create(self):
        evals = self.root / "evals"
        evals.mkdir()
        (evals / "GOTCHA.md").write_text("real gotcha content, not a stub\n", encoding="utf-8")
        (self.root / "GOTCHA.md").write_text("old sibling gotcha\n", encoding="utf-8")
        (self.root / "LEARNING.md").write_text("old sibling learning\n", encoding="utf-8")
        before = self._snapshot()
        rc, p = self.kz("M2")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 4, p)
        by_name: dict[str, list[str]] = {}
        for action in p["actions"]:
            by_name.setdefault(Path(action["target_path"]).name, []).append(action["action"])
        self.assertEqual(by_name["GOTCHA.md"], ["backup-and-replace-stub", "backup-and-remove-old-sibling"])
        self.assertEqual(by_name["LEARNING.md"], ["relocate-sibling-to-evals"])
        self.assertEqual(by_name["LEARNED.md"], ["create-stub"])
        replace = next(a for a in p["actions"] if a["action"] == "backup-and-replace-stub")
        self.assertTrue(_HASH_RE.match(replace["sha256"] or ""), replace)
        self.assertEqual(self._snapshot(), before, "dry-run must not create or modify files")

    # -- M3 apply ------------------------------------------------------------

    def test_m3_apply_creates_stub_files_and_empty_backup_manifest(self):
        rc, p = self.kz("M3")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["status"], "OK")
        self.assertEqual(p["count"], 3, p)
        self.assertEqual(len(p["changed"]), 3, p)
        expected_codes = {"GOTCHA.md": "G2", "LEARNED.md": "L7", "LEARNING.md": "L4"}
        for name in _SURFACES:
            stub = self.root / "evals" / name
            self.assertTrue(stub.is_file(), f"missing stub {stub}")
            text = stub.read_text(encoding="utf-8")
            self.assertTrue(text.startswith(f"# {name[:-3]}"), text)
            self.assertIn(_STUB_MARKER, text)
            self.assertIn(f"python kaizen.py {expected_codes[name]}", text)
        manifest_rel = p["manifest"]
        self.assertTrue(manifest_rel.startswith("AI/work/kaizen-v4-migration/backups/"), p)
        manifest = self.root / manifest_rel
        self.assertTrue(manifest.is_file(), manifest)
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(data["files"], [], "nothing pre-existing, so no backups expected")

    def test_m3_apply_relocates_legacy_sibling_with_backup(self):
        proj = self._make_skill_project()
        legacy = "legacy gotcha body to preserve\n"
        (proj / "GOTCHA.md").write_text(legacy, encoding="utf-8")
        rc, p = self.kz("M3")
        self.assertEqual(rc, 0, p)
        self.assertFalse((proj / "GOTCHA.md").exists(), "old sibling must be removed")
        target = proj / "evals" / "GOTCHA.md"
        self.assertTrue(target.is_file(), p)
        self.assertIn(_STUB_MARKER, target.read_text(encoding="utf-8"))
        self.assertTrue(any(Path(c) == target.resolve() for c in p["changed"]), p)
        data = json.loads((self.root / p["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(len(data["files"]), 1, data)
        entry = data["files"][0]
        self.assertEqual(Path(entry["source_path"]), (proj / "GOTCHA.md").resolve())
        self.assertTrue(_HASH_RE.match(entry["sha256"]), entry)
        backup = self.root / entry["backup_path"]
        self.assertEqual(backup.read_text(encoding="utf-8"), legacy, "backup must hold the legacy content")

    def test_m3_apply_replaces_non_stub_target_with_backup(self):
        evals = self.root / "evals"
        evals.mkdir()
        real = "important learned notes that are not a stub\n"
        (evals / "LEARNED.md").write_text(real, encoding="utf-8")
        rc, p = self.kz("M3")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 3, p)  # replace LEARNED + create GOTCHA/LEARNING
        text = (evals / "LEARNED.md").read_text(encoding="utf-8")
        self.assertIn(_STUB_MARKER, text)
        self.assertNotIn("important learned notes", text)
        data = json.loads((self.root / p["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(len(data["files"]), 1, data)
        entry = data["files"][0]
        self.assertEqual(Path(entry["source_path"]), (evals / "LEARNED.md").resolve())
        self.assertEqual((self.root / entry["backup_path"]).read_text(encoding="utf-8"), real)

    def test_m3_apply_is_idempotent_and_m2_reports_keep_stub(self):
        rc, p = self.kz("M3")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("M3")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 0, p)
        self.assertEqual(p["changed"], [], p)
        rc, p = self.kz("M2")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 3, p)
        self.assertEqual({a["action"] for a in p["actions"]}, {"keep-stub"})

    def test_m3_apply_disallowed_root_denies_before_writing(self):
        outside = self._outside_dir()
        rc, p = self.kz("M3", "--root", str(outside), env=_NO_SKILLS)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p["code"], "DENIED_ROOT_NOT_ALLOWLISTED")
        self.assertFalse((self.root / "AI" / "work" / "kaizen-v4-migration").exists(),
                         "denied apply must not create the migration task dir")
        self.assertFalse((self.root / "evals").exists())

    # -- M4 verify -----------------------------------------------------------

    def test_m4_verify_fails_before_apply_and_passes_after(self):
        rc, p = self.kz("M4")
        self.assertEqual(rc, 0, p)  # --json emit returns 0; FAILED is in the payload
        self.assertEqual(p["status"], "FAILED")
        self.assertEqual(p["count"], 3, p)
        rc, p = self.kz("M3")
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("M4")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["status"], "OK")
        self.assertEqual(p["unmigrated"], [])
        self.assertEqual(p["count"], 0, p)
        # A reintroduced old sibling flips verify back to FAILED.
        stray = self.root / "GOTCHA.md"
        stray.write_text("stray sibling\n", encoding="utf-8")
        rc, p = self.kz("M4")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["status"], "FAILED")
        self.assertEqual(p["count"], 1, p)
        self.assertTrue(any(Path(u) == stray.resolve() for u in p["unmigrated"]), p)

    def test_m4_verify_missing_root_reports_failed(self):
        rc, p = self.kz("M4", "--root", str(self.root / "not-there"))
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["status"], "FAILED")
        self.assertEqual(p["count"], 1, p)

    # -- M5 migration report ---------------------------------------------------

    def test_m5_report_writes_markdown_under_work_dir(self):
        rc, p = self.kz("M5")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["status"], "OK")
        self.assertEqual(p["count"], 3, p)
        self.assertTrue(
            p["path"].startswith("AI/work/kaizen-v4-migration/reports/migration-report-"), p
        )
        report = self.root / p["path"]
        self.assertTrue(report.is_file(), report)
        text = report.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Kaizen Migration Report"), text)
        self.assertIn("Actions: 3", text)
        self.assertIn("- create-stub: `", text)
        # The report is built from a dry-run plan: no stubs may appear.
        self.assertFalse((self.root / "evals").exists())


if __name__ == "__main__":
    unittest.main()
