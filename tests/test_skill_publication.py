"""Per-package GitHub publication classification without network or credential exposure."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support_scripts.skill_management.discovery import _is_github_remote, discover_skills


class SkillPublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.store = self.root / "skills"
        self.project.mkdir()
        self.store.mkdir()

    def package(self, name: str, remote: str | None = None) -> Path:
        package = self.store / name
        package.mkdir()
        (package / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Use when testing {name}.\n---\n\n## Workflow\n\nRun it.\n\n## References\n\nNone.\n\n## Verification\n\nVerify it.\n\n## Gotchas\n\nNone.\n",
            encoding="utf-8",
        )
        if remote is not None:
            metadata = package / ".git"
            metadata.mkdir()
            (metadata / "config").write_text(
                f'[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = {remote}\n',
                encoding="utf-8",
            )
        return package

    def test_standard_credential_free_github_forms(self) -> None:
        self.assertTrue(_is_github_remote("https://github.com/owner/repo.git"))
        self.assertTrue(_is_github_remote("git@github.com:owner/repo.git"))
        self.assertTrue(_is_github_remote("ssh://git@github.com/owner/repo.git"))
        self.assertFalse(_is_github_remote("https://token@github.com/owner/repo.git"))
        self.assertFalse(_is_github_remote("https://github.example/owner/repo.git"))
        self.assertFalse(_is_github_remote("git://github.com/owner/repo.git"))
        self.assertFalse(_is_github_remote("https://github.com:invalid/owner/repo.git"))

    def test_discovery_reports_independent_publication_state_without_remote_value(self) -> None:
        self.package("published", "https://github.com/owner/published.git")
        self.package("staged")
        self.package("elsewhere", "https://gitlab.com/owner/elsewhere.git")
        result = discover_skills(self.project, self.store)
        records = {row["name"]: row for row in result["skills"]}
        self.assertEqual(records["published"]["publication_status"], "published")
        self.assertEqual(records["published"]["publication_reason"], "github_remote")
        self.assertEqual(records["staged"]["publication_status"], "staged")
        self.assertEqual(records["staged"]["publication_reason"], "no_git_metadata")
        self.assertEqual(records["elsewhere"]["publication_status"], "staged")
        self.assertEqual(records["elsewhere"]["publication_reason"], "no_github_remote")
        self.assertNotIn("remote", records["published"])
        self.assertNotIn("github.com", repr(records["published"]))


if __name__ == "__main__":
    unittest.main()
