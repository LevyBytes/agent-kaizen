"""Skill manager: read-only inspection, hash-confirmed mutation, and rollback."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRATCH_ROOT = REPO_ROOT / "AI" / "work" / "skill-management-tests"
SCRIPT = REPO_ROOT / "support_scripts" / "skill_manager.py"

sys.path.insert(0, str(REPO_ROOT))

import support_scripts.skill_management.links as link_module  # noqa: E402
import support_scripts.skill_management.policy as policy_module  # noqa: E402
from support_scripts.skill_management import (  # noqa: E402
    SkillManagementError,
    apply_index_plan,
    apply_link_plan,
    apply_policy_plan,
    apply_store_index_plan,
    build_index_plan,
    build_link_plan,
    build_policy_plan,
    build_policy_restore_plan,
    build_store_index_plan,
    discover_skills,
    index_status,
    package_sha256,
    policy_status,
    restore_policy,
    validate_skill_package,
    validation_status,
)


def write_leaf(root: Path, name: str, keywords: list[str] | None = None) -> Path:
    skill = root / name
    (skill / "references").mkdir(parents=True)
    (skill / "evals").mkdir()
    (skill / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                f"name: {name}",
                f"description: Use when working with {name} fixtures and validation tests.",
                "---",
                "",
                f"# {name}",
                "",
                "## Workflow",
                "",
                "Read the relevant reference before acting.",
                "",
                "## References",
                "",
                "Start with [the index](references/INDEX.md).",
                "",
                "## Gotchas",
                "",
                "See [evals/GOTCHA.md](evals/GOTCHA.md).",
                "",
                "## Verification",
                "",
                "Run the package validator.",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    (skill / "references" / "topic.md").write_text("# Topic\n\nVerified guidance.\n", encoding="utf-8")
    (skill / "references" / "INDEX.md").write_text(
        "# References\n\n- [Topic](topic.md)\n", encoding="utf-8"
    )
    (skill / "references" / "topics.json").write_text(
        json.dumps(
            {
                "topics": [
                    {
                        "topic": "Topic",
                        "summary": "Verified topic guidance.",
                        "keywords": keywords or [name, "shared-entity"],
                        "file": "references/topic.md",
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (skill / "evals" / "GOTCHA.md").write_text("# Gotchas\n\n- Verify first.\n", encoding="utf-8")
    return skill


def write_router(root: Path, name: str, child_name: str = "child") -> tuple[Path, Path]:
    router = root / name
    (router / "evals").mkdir(parents=True)
    child = write_leaf(router, child_name)
    (router / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                f"name: {name}",
                f"description: Use when routing {name} work to a product subskill.",
                "---",
                "",
                f"# {name}",
                "",
                "## Workflow",
                "",
                f"Open [{child_name}]({child_name}/SKILL.md).",
                "",
                "## References",
                "",
                "Each child owns its references.",
                "",
                "## Gotchas",
                "",
                "See [evals/GOTCHA.md](evals/GOTCHA.md).",
                "",
                "## Verification",
                "",
                "Validate the complete router package.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (router / "evals" / "GOTCHA.md").write_text("# Gotchas\n\n- Route deliberately.\n", encoding="utf-8")
    return router, child


def create_directory_link(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if proc.returncode != 0:
            raise OSError(proc.stderr or proc.stdout)
    else:
        os.symlink(target, path, target_is_directory=True)


def create_file_link(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, path, target_is_directory=False)


class ScratchTest(unittest.TestCase):
    def setUp(self) -> None:
        SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=str(SCRATCH_ROOT)))
        self.project = self.root / "project"
        self.store = self.root / "store"
        self.project.mkdir()
        self.store.mkdir()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)


class ValidationTest(ScratchTest):
    def test_leaf_validation_and_hash_change(self):
        skill = write_leaf(self.store, "alpha")
        result = validate_skill_package(skill)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["layout"], "flat")
        before = package_sha256(skill)
        (skill / "references" / "topic.md").write_text("# Topic\n\nChanged.\n", encoding="utf-8")
        self.assertNotEqual(package_sha256(skill), before)

    def test_required_authoring_gates_are_errors(self):
        skill = write_leaf(self.store, "alpha")
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        text = text.replace("Use when working with", "Handles").replace("## Verification", "## Result")
        (skill / "SKILL.md").write_text(text, encoding="utf-8")
        result = validate_skill_package(skill)
        self.assertFalse(result["valid"])
        self.assertTrue(any("trigger language" in error for error in result["errors"]))
        self.assertTrue(any("verification guidance" in error for error in result["errors"]))

    def test_malformed_reference_index_is_a_structured_validation_error(self):
        skill = write_leaf(self.store, "alpha")
        (skill / "references" / "INDEX.md").write_bytes(b"\xff\xfe")
        result = validate_skill_package(skill)
        self.assertFalse(result["valid"])
        self.assertTrue(any("INDEX.md is unreadable" in error for error in result["errors"]), result)

    def test_markdown_links_ignore_code_and_accept_balanced_destinations(self):
        skill = write_leaf(self.store, "alpha")
        topic = skill / "references" / "topic.md"
        topic.write_text(
            """# Topic

`.*[.]([^b]..|.[^a].|..[^t])$`

```cpp
client->Open([client](Result result, uint64_t value) {});
```

~~~text
[not a link](missing-from-fence.md)
~~~

[ICO](<https://en.wikipedia.org/wiki/ICO_(file_format)>)
[method](https://example.test/api/function(argument))
[local](topic_(v2).md)
""",
            encoding="utf-8",
        )
        (skill / "references" / "topic_(v2).md").write_text("# V2\n", encoding="utf-8")
        result = validate_skill_package(skill)
        self.assertTrue(result["valid"], result)

    def test_markdown_links_preserve_malformed_and_missing_local_diagnostics(self):
        skill = write_leaf(self.store, "alpha")
        (skill / "references" / "topic.md").write_text(
            """# Topic

[missing](absent.md)
[corrupt]([https://example.test](https://example.test):
[balanced-corrupt]([https://example.test](https://example.test))
""",
            encoding="utf-8",
        )
        result = validate_skill_package(skill)
        self.assertFalse(result["valid"])
        self.assertTrue(any("missing local link target: absent.md" in error for error in result["errors"]), result)
        self.assertGreaterEqual(
            sum("malformed Markdown link destination" in error for error in result["errors"]),
            2,
            result,
        )

    def test_router_child_diagnostics_fail_closed(self):
        router, child = write_router(self.store, "router")
        valid = validate_skill_package(router)
        self.assertTrue(valid["valid"], valid)
        self.assertEqual(valid["layout"], "router")
        self.assertEqual(len(valid["subskills"]), 1)
        (child / "references" / "topics.json").write_text("{}\n", encoding="utf-8")
        invalid = validate_skill_package(router)
        self.assertFalse(invalid["valid"])
        self.assertFalse(invalid["subskills"][0]["valid"])
        self.assertTrue(any("subskill `child` failed" in error for error in invalid["errors"]))

    def test_router_must_reference_every_child(self):
        router, _ = write_router(self.store, "router")
        text = (router / "SKILL.md").read_text(encoding="utf-8").replace("child", "product")
        (router / "SKILL.md").write_text(text, encoding="utf-8")
        result = validate_skill_package(router)
        self.assertFalse(result["valid"])
        self.assertIn("subskill not referenced in router SKILL.md: child", result["errors"])

    def test_nested_router_validation_is_recursive(self):
        router, child = write_router(self.store, "outer")
        shutil.rmtree(child)
        _child_router, grandchild = write_router(router, "child", "grandchild")
        self.assertTrue(validate_skill_package(router)["valid"])
        (grandchild / "references" / "topics.json").write_text("{}\n", encoding="utf-8")
        result = validate_skill_package(router)
        self.assertFalse(result["valid"])
        self.assertEqual(result["subskills"][0]["layout"], "router")
        self.assertFalse(result["subskills"][0]["subskills"][0]["valid"])

    def test_duplicate_frontmatter_key_is_rejected(self):
        skill = write_leaf(self.store, "alpha")
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        text = text.replace("description:", "name: alpha\ndescription:", 1)
        (skill / "SKILL.md").write_text(text, encoding="utf-8")
        result = validate_skill_package(skill)
        self.assertIn("duplicate frontmatter key: name", result["errors"])

    def test_nested_package_link_is_rejected(self):
        skill = write_leaf(self.store, "alpha")
        outside = self.root / "outside"
        outside.mkdir()
        try:
            create_directory_link(skill / "linked-content", outside)
        except OSError as exc:
            self.skipTest(f"directory links unavailable: {exc}")
        result = validate_skill_package(skill)
        self.assertFalse(result["valid"])
        self.assertTrue(any("package links are not allowed" in error for error in result["errors"]))

    def test_linked_router_child_outside_package_is_rejected(self):
        router, child = write_router(self.store, "router")
        shutil.rmtree(child)
        outside = write_leaf(self.root, "child")
        try:
            create_directory_link(router / "child", outside)
        except OSError as exc:
            self.skipTest(f"directory links unavailable: {exc}")
        result = validate_skill_package(router)
        self.assertFalse(result["valid"])
        self.assertTrue(any("package links are not allowed: child" in error for error in result["errors"]))


class DiscoveryAndLinksTest(ScratchTest):
    def test_inventory_has_explicit_hosts_and_complete_scan(self):
        write_leaf(self.store, "alpha")
        result = discover_skills(self.project, self.store)
        self.assertTrue(result["complete"])
        self.assertEqual(result["scan_errors"], [])
        self.assertEqual(result["hosts"], ["codex", "claude"])
        self.assertEqual(result["skills"][0]["hosts"]["codex"]["status"], "missing")
        self.assertIn(str(Path(".agents") / "skills"), result["skills"][0]["hosts"]["codex"]["path"])

    def test_missing_store_is_incomplete_not_empty_success(self):
        result = discover_skills(self.project, self.root / "absent")
        self.assertFalse(result["complete"])
        self.assertTrue(result["scan_errors"])

    def test_invalid_enumerated_package_is_preserved(self):
        (self.store / "broken").mkdir()
        result = discover_skills(self.project, self.store)
        self.assertEqual([row["name"] for row in result["skills"]], ["broken"])
        self.assertFalse(result["skills"][0]["valid"])

    def test_store_package_link_escape_marks_scan_incomplete(self):
        outside = self.root / "outside-skill"
        write_leaf(self.root, "outside-skill")
        try:
            create_directory_link(self.store / "escape", outside)
        except OSError as exc:
            self.skipTest(f"directory links unavailable: {exc}")
        result = discover_skills(self.project, self.store)
        self.assertFalse(result["complete"])
        self.assertTrue(any("escapes the explicit store root" in error for error in result["scan_errors"]))

    def test_orphan_surface_directory_is_an_sk2_conflict(self):
        write_leaf(self.store, "alpha")
        write_leaf(self.project / ".agents" / "skills", "orphan")
        result = validation_status(self.project, self.store, ("codex",))
        self.assertFalse(result["valid"])
        orphan = [row for row in result["link_conflicts"] if row.get("orphan")]
        self.assertEqual([row["name"] for row in orphan], ["orphan"])

    def test_orphan_surface_link_is_an_sk2_conflict(self):
        write_leaf(self.store, "alpha")
        outside = write_leaf(self.root, "orphan")
        try:
            create_directory_link(self.project / ".claude" / "skills" / "orphan", outside)
        except OSError as exc:
            self.skipTest(f"directory links unavailable: {exc}")
        result = validation_status(self.project, self.store, ("claude",))
        self.assertFalse(result["valid"])
        orphan = [row for row in result["link_conflicts"] if row.get("orphan")]
        self.assertEqual(orphan[0]["status"], "wrong_target")

    def test_hidden_skill_surface_is_an_sk2_conflict_but_metadata_file_is_ignored(self):
        write_leaf(self.store, "alpha")
        surface = self.project / ".agents" / "skills"
        write_leaf(surface, ".rogue")
        (surface / ".gitkeep").write_text("", encoding="utf-8")
        result = validation_status(self.project, self.store, ("codex",))
        self.assertFalse(result["valid"])
        orphan = [row for row in result["link_conflicts"] if row.get("orphan")]
        self.assertEqual([row["name"] for row in orphan], [".rogue"])

    def test_present_file_surface_root_marks_inventory_incomplete(self):
        write_leaf(self.store, "alpha")
        surface = self.project / ".agents" / "skills"
        surface.parent.mkdir(parents=True)
        surface.write_text("not a directory\n", encoding="utf-8")
        result = discover_skills(self.project, self.store, ("codex",))
        self.assertFalse(result["complete"])
        self.assertTrue(any("not a readable directory" in error for error in result["scan_errors"]), result)

    def test_unreadable_surface_enumeration_marks_inventory_incomplete(self):
        write_leaf(self.store, "alpha")
        surface = self.project / ".agents" / "skills"
        surface.mkdir(parents=True)
        original_iterdir = Path.iterdir

        def permission_denied(path):
            if path == surface:
                raise PermissionError("synthetic surface denial")
            return original_iterdir(path)

        with mock.patch.object(Path, "iterdir", permission_denied):
            result = discover_skills(self.project, self.store, ("codex",))
        self.assertFalse(result["complete"])
        self.assertTrue(any("cannot enumerate codex skill surface" in error for error in result["scan_errors"]), result)

    def test_dangling_top_level_package_link_marks_scan_incomplete(self):
        outside = write_leaf(self.root, "outside-skill")
        link = self.store / "dangling"
        try:
            create_directory_link(link, outside)
        except OSError as exc:
            self.skipTest(f"directory links unavailable: {exc}")
        shutil.rmtree(outside)
        result = discover_skills(self.project, self.store)
        self.assertFalse(result["complete"])
        self.assertTrue(result["scan_errors"], result)
        plan = build_store_index_plan(self.store)
        self.assertTrue(plan["errors"], plan)
        with self.assertRaises(SkillManagementError):
            apply_store_index_plan(self.store, confirm_plan=plan["plan_sha256"])

    def test_link_plan_requires_explicit_skill(self):
        write_leaf(self.store, "alpha")
        plan = build_link_plan(self.project, self.store, hosts=("codex",))
        self.assertEqual(plan["operations"], [])
        self.assertTrue(any("explicit skill_names" in error for error in plan["errors"]))

    def test_confirmed_link_apply_and_status(self):
        write_leaf(self.store, "alpha")
        plan = build_link_plan(self.project, self.store, hosts=("codex",), skill_names=("alpha",))
        self.assertFalse(plan["errors"], plan)
        with self.assertRaises(SkillManagementError):
            apply_link_plan(self.project, self.store, ("codex",), ("alpha",), confirm_plan="0" * 64)
        result = apply_link_plan(
            self.project,
            self.store,
            ("codex",),
            ("alpha",),
            confirm_plan=plan["plan_sha256"],
        )
        self.assertEqual(result["applied"], 1)
        self.assertTrue(result["restart_required"])
        inventory = discover_skills(self.project, self.store, ("codex",))
        self.assertEqual(inventory["skills"][0]["hosts"]["codex"]["status"], "correct")

    def test_failed_link_postcondition_rolls_back_the_registered_created_link(self):
        target = write_leaf(self.store, "alpha")
        path = self.project / ".agents" / "skills" / "alpha"
        wrong = self.store / "wrong"
        plan = build_link_plan(self.project, self.store, ("codex",), ("alpha",))
        original_link_target = link_module.link_target
        verification_failed = False

        def fail_first_verification(candidate):
            nonlocal verification_failed
            if candidate == path and not verification_failed:
                verification_failed = True
                return wrong
            return original_link_target(candidate)

        with mock.patch.object(link_module, "link_target", side_effect=fail_first_verification):
            with self.assertRaises(SkillManagementError):
                apply_link_plan(
                    self.project,
                    self.store,
                    ("codex",),
                    ("alpha",),
                    confirm_plan=plan["plan_sha256"],
                )
        self.assertTrue(verification_failed)
        self.assertFalse(os.path.lexists(path))

    def test_real_directory_and_wrong_target_are_rejected(self):
        write_leaf(self.store, "alpha")
        collision = self.project / ".agents" / "skills" / "alpha"
        collision.mkdir(parents=True)
        plan = build_link_plan(self.project, self.store, ("codex",), ("alpha",))
        self.assertTrue(any("real_directory" in error for error in plan["errors"]))
        shutil.rmtree(collision)
        first = build_link_plan(self.project, self.store, ("codex",), ("alpha",))
        apply_link_plan(self.project, self.store, ("codex",), ("alpha",), confirm_plan=first["plan_sha256"])
        other_store = self.root / "other-store"
        other_store.mkdir()
        write_leaf(other_store, "alpha")
        wrong = build_link_plan(self.project, other_store, ("codex",), ("alpha",))
        self.assertTrue(any("wrong_target" in error for error in wrong["errors"]))

    def test_prune_is_separate_and_explicit(self):
        write_leaf(self.store, "alpha")
        write_leaf(self.store, "beta")
        first = build_link_plan(self.project, self.store, ("codex",), ("alpha",))
        apply_link_plan(self.project, self.store, ("codex",), ("alpha",), confirm_plan=first["plan_sha256"])
        keep = build_link_plan(self.project, self.store, ("codex",), ("beta",), prune=False)
        self.assertFalse(any(op["op"] == "remove_link" for op in keep["operations"]))
        prune = build_link_plan(self.project, self.store, ("codex",), ("beta",), prune=True)
        self.assertTrue(any(op["op"] == "remove_link" and op["name"] == "alpha" for op in prune["operations"]))


class IndexTest(ScratchTest):
    def _surface(self, host: str = "codex") -> None:
        write_leaf(self.store, "alpha")
        plan = build_link_plan(self.project, self.store, (host,), ("alpha",))
        apply_link_plan(self.project, self.store, (host,), ("alpha",), confirm_plan=plan["plan_sha256"])

    def test_index_plan_is_read_only_and_apply_is_confirmed(self):
        self._surface()
        index_path = self.project / ".agents" / "skills" / "INDEX.md"
        plan = build_index_plan(self.project, ("codex",))
        self.assertFalse(index_path.exists())
        self.assertFalse(plan["errors"], plan)
        with self.assertRaises(SkillManagementError):
            apply_index_plan(self.project, ("codex",), "bad")
        result = apply_index_plan(self.project, ("codex",), plan["plan_sha256"])
        self.assertEqual(result["applied"], 1)
        text = index_path.read_text(encoding="utf-8")
        self.assertIn("`.agents/skills/`", text)
        self.assertIn("## Related skills", text)
        self.assertEqual(index_status(self.project, ("codex",))["hosts"][0]["status"], "up_to_date")

    def test_sk4_rejects_index_link_redirect_within_project(self):
        self._surface()
        index_path = self.project / ".agents" / "skills" / "INDEX.md"
        protected = self.project / "AGENTS.md"
        protected.write_text("protected\n", encoding="utf-8")
        safe_plan = build_index_plan(self.project, ("codex",))
        try:
            create_file_link(index_path, protected)
        except OSError:
            index_path.write_text("redirect placeholder\n", encoding="utf-8")
            original_is_symlink = Path.is_symlink

            def pretend_index_link(path: Path) -> bool:
                return path == index_path or original_is_symlink(path)

            link_guard = mock.patch.object(Path, "is_symlink", new=pretend_index_link)
        else:
            link_guard = nullcontext()
        with link_guard:
            unsafe_plan = build_index_plan(self.project, ("codex",))
            self.assertTrue(any("INDEX.md must not be a link" in error for error in unsafe_plan["errors"]), unsafe_plan)
            with mock.patch(
                "support_scripts.skill_management.indexing.build_index_plan",
                return_value=safe_plan,
            ):
                with self.assertRaises(SkillManagementError):
                    apply_index_plan(self.project, ("codex",), safe_plan["plan_sha256"])
        self.assertEqual(protected.read_text(encoding="utf-8"), "protected\n")

    def test_absent_surface_is_skipped_and_non_directory_surface_is_error(self):
        absent = build_index_plan(self.project, ("codex",))
        self.assertEqual(absent["operations"], [])
        self.assertFalse(absent["errors"])
        self.assertEqual(index_status(self.project, ("codex",))["hosts"][0]["status"], "surface_missing")
        (self.project / ".agents").mkdir()
        (self.project / ".agents" / "skills").write_text("not a directory\n", encoding="utf-8")
        invalid = build_index_plan(self.project, ("codex",))
        self.assertEqual(invalid["operations"], [])
        self.assertTrue(invalid["errors"])

    def test_aggregate_validation_reports_stale_then_current_index(self):
        self._surface()
        stale = validation_status(self.project, self.store, ("codex",))
        self.assertFalse(stale["valid"])
        self.assertTrue(stale["stale_indexes"])
        plan = build_index_plan(self.project, ("codex",))
        apply_index_plan(self.project, ("codex",), plan["plan_sha256"])
        current = validation_status(self.project, self.store, ("codex",))
        self.assertTrue(current["valid"], current)

    def test_host_index_semantic_findings_are_diagnostic_not_blocking(self):
        self._surface()
        skill = self.store / "alpha"
        (skill / "references" / "INDEX.md").write_text("# References\n", encoding="utf-8")
        plan = build_index_plan(self.project, ("codex",))
        self.assertFalse(plan["errors"], plan)
        self.assertTrue(plan["diagnostics"], plan)
        self.assertEqual(len(plan["operations"]), 1)
        self.assertIn("alpha", plan["operations"][0]["content"])
        applied = apply_index_plan(self.project, ("codex",), plan["plan_sha256"])
        self.assertEqual(applied["applied"], 1)
        status = index_status(self.project, ("codex",))
        self.assertFalse(status["errors"], status)
        self.assertTrue(status["diagnostics"], status)
        self.assertEqual(status["hosts"][0]["status"], "up_to_date")

    def test_rich_store_index_and_seed_covers(self):
        write_leaf(self.store, "alpha", ["shared-entity", "alpha-only"])
        write_leaf(self.store, "beta", ["shared-entity", "beta-only"])
        plan = build_store_index_plan(self.store, seed_covers=True)
        self.assertFalse((self.store / "INDEX.md").exists())
        self.assertTrue(any(op["op"] == "write_covers" for op in plan["operations"]))
        self.assertIn("## Entity → skill map (shared)", next(op["content"] for op in plan["operations"] if op["op"] == "write_index"))
        result = apply_store_index_plan(self.store, seed_covers=True, confirm_plan=plan["plan_sha256"])
        self.assertGreaterEqual(result["applied"], 3)
        self.assertIn("covers:\n", (self.store / "alpha" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("shared-entity", (self.store / "INDEX.md").read_text(encoding="utf-8"))

    def test_store_index_rejects_link_redirect_outside_managed_root(self):
        write_leaf(self.store, "alpha")
        index_path = self.store / "INDEX.md"
        protected = self.root / "outside-index.md"
        protected.write_text("protected\n", encoding="utf-8")
        safe_plan = build_store_index_plan(self.store)
        try:
            create_file_link(index_path, protected)
        except OSError:
            index_path.write_text("redirect placeholder\n", encoding="utf-8")
            original_is_symlink = Path.is_symlink

            def pretend_index_link(path: Path) -> bool:
                return path == index_path or original_is_symlink(path)

            link_guard = mock.patch.object(Path, "is_symlink", new=pretend_index_link)
        else:
            link_guard = nullcontext()
        with link_guard:
            unsafe_plan = build_store_index_plan(self.store)
            self.assertTrue(any("INDEX.md must not be a link" in error for error in unsafe_plan["errors"]), unsafe_plan)
            with mock.patch(
                "support_scripts.skill_management.indexing.build_store_index_plan",
                return_value=safe_plan,
            ):
                with self.assertRaises(SkillManagementError):
                    apply_store_index_plan(self.store, confirm_plan=safe_plan["plan_sha256"])
        self.assertEqual(protected.read_text(encoding="utf-8"), "protected\n")

    def test_store_index_accepts_linked_host_surface(self):
        target = write_leaf(self.store, "alpha")
        surface = self.project / ".agents" / "skills"
        try:
            create_directory_link(surface / "alpha", target)
        except OSError as exc:
            self.skipTest(f"directory links unavailable: {exc}")
        plan = build_store_index_plan(surface)
        self.assertFalse(plan["errors"], plan)
        self.assertTrue(any(operation["op"] == "write_index" for operation in plan["operations"]))
        applied = apply_store_index_plan(surface, confirm_plan=plan["plan_sha256"])
        self.assertEqual(applied["applied"], 1)
        self.assertTrue((surface / "INDEX.md").is_file())

    def test_seed_covers_deduplicates_linked_mirror_target(self):
        target = write_leaf(self.store, "alpha", ["shared-entity", "alpha-only"])
        codex = self.project / ".agents" / "skills"
        claude = self.project / ".claude" / "skills"
        try:
            create_directory_link(codex / "alpha", target)
            create_directory_link(claude / "alpha", target)
        except OSError as exc:
            self.skipTest(f"directory links unavailable: {exc}")
        plan = build_store_index_plan(codex, mirror_root=claude, seed_covers=True)
        self.assertFalse(plan["errors"], plan)
        self.assertEqual(sum(operation["op"] == "write_covers" for operation in plan["operations"]), 1)
        self.assertEqual(sum(operation["op"] == "write_index" for operation in plan["operations"]), 2)
        applied = apply_store_index_plan(
            codex,
            mirror_root=claude,
            seed_covers=True,
            confirm_plan=plan["plan_sha256"],
        )
        self.assertEqual(applied["applied"], 3)
        self.assertTrue((codex / "INDEX.md").is_file())
        self.assertTrue((claude / "INDEX.md").is_file())

    def test_store_shim_and_sk4_share_one_host_index_representation(self):
        write_leaf(self.store, "alpha", ["shared-entity", "alpha-only"])
        links = build_link_plan(self.project, self.store, ("codex", "claude"), ("alpha",))
        apply_link_plan(
            self.project,
            self.store,
            ("codex", "claude"),
            ("alpha",),
            confirm_plan=links["plan_sha256"],
        )
        codex = self.project / ".agents" / "skills"
        claude = self.project / ".claude" / "skills"
        shim_plan = build_store_index_plan(claude, mirror_root=codex)
        sk4_plan = build_index_plan(self.project, ("codex", "claude"))
        shim_indexes = {
            operation["path"]: operation["after_sha256"]
            for operation in shim_plan["operations"]
            if operation["op"] == "write_index"
        }
        sk4_indexes = {operation["path"]: operation["after_sha256"] for operation in sk4_plan["operations"]}
        self.assertEqual(shim_indexes, sk4_indexes)

        apply_store_index_plan(claude, mirror_root=codex, confirm_plan=shim_plan["plan_sha256"])
        statuses = index_status(self.project, ("codex", "claude"))["hosts"]
        self.assertEqual([row["status"] for row in statuses], ["up_to_date", "up_to_date"])

    def test_store_index_plan_is_stable_across_python_hash_seeds(self):
        for name in ("alpha", "beta", "gamma", "delta"):
            write_leaf(self.store, name, ["shared-entity"])
        plans = []
        for seed in ("1", "2"):
            env = os.environ.copy()
            env["PYTHONHASHSEED"] = seed
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--json",
                    "store-index",
                    "plan",
                    "--skills-root",
                    str(self.store),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plans.append(json.loads(result.stdout))
        self.assertEqual(plans[0]["plan_sha256"], plans[1]["plan_sha256"])
        self.assertEqual(plans[0]["operations"], plans[1]["operations"])

    def test_semantic_package_findings_do_not_block_store_index(self):
        skill = write_leaf(self.store, "alpha")
        (skill / "references" / "INDEX.md").write_text("# References\n", encoding="utf-8")
        plan = build_store_index_plan(self.store, seed_covers=True)
        self.assertFalse(plan["errors"], plan)
        self.assertTrue(plan["diagnostics"], plan)
        self.assertFalse(any(operation["op"] == "write_covers" for operation in plan["operations"]))
        self.assertTrue(any(operation["op"] == "write_index" for operation in plan["operations"]))
        applied = apply_store_index_plan(self.store, seed_covers=True, confirm_plan=plan["plan_sha256"])
        self.assertEqual(applied["applied"], 1)

    def test_store_index_rejects_duplicate_link_targets(self):
        target = write_leaf(self.store, "alpha")
        surface = self.project / ".agents" / "skills"
        try:
            create_directory_link(surface / "alpha", target)
            create_directory_link(surface / "alias", target)
        except OSError as exc:
            self.skipTest(f"directory links unavailable: {exc}")
        plan = build_store_index_plan(surface)
        self.assertTrue(any("duplicate skill package target" in error for error in plan["errors"]), plan)

    def test_generated_index_escapes_untrusted_frontmatter_and_topic_cells(self):
        skill = write_leaf(self.store, "alpha", ["bad|cell", "`tick`"])
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        (skill / "SKILL.md").write_text(
            text.replace("fixtures and validation tests.", "fixtures | injected and validation tests."),
            encoding="utf-8",
        )
        plan = build_store_index_plan(self.store)
        rendered = next(operation["content"] for operation in plan["operations"] if operation["op"] == "write_index")
        self.assertIn("bad\\|cell", rendered)
        self.assertIn("\\`tick\\`", rendered)
        self.assertIn("fixtures \\| injected", rendered)


class PolicyTest(ScratchTest):
    def setUp(self) -> None:
        super().setUp()
        write_leaf(self.store, "alpha")
        links = build_link_plan(self.project, self.store, ("claude",), ("alpha",))
        apply_link_plan(self.project, self.store, ("claude",), ("alpha",), confirm_plan=links["plan_sha256"])

    def test_policy_apply_and_explicit_restore(self):
        status = policy_status(self.project)
        self.assertEqual(status["host"], "claude")
        self.assertFalse(status["settings_exists"])
        plan = build_policy_plan(self.project, {"alpha": "off"})
        self.assertFalse(plan["errors"], plan)
        with self.assertRaises(SkillManagementError):
            apply_policy_plan(self.project, {"alpha": "off"}, "bad")
        applied = apply_policy_plan(self.project, {"alpha": "off"}, plan["plan_sha256"])
        self.assertTrue(applied["restart_required"])
        self.assertEqual(policy_status(self.project)["skills"][0]["current_policy"], "off")
        record = applied["rollback_record"]
        restore_plan = build_policy_restore_plan(self.project, record)
        restored = restore_policy(self.project, record, restore_plan["plan_sha256"])
        self.assertTrue(restored["restart_required"])
        self.assertFalse((self.project / ".claude" / "settings.local.json").exists())

    def test_policy_rejects_unsurfaced_skill(self):
        plan = build_policy_plan(self.project, {"missing": "off"})
        self.assertTrue(any("not surfaced" in error for error in plan["errors"]))

    def test_public_policy_plans_never_expose_unrelated_settings_values(self):
        settings = self.project / ".claude" / "settings.local.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text('{"apiKey":"do-not-echo","unrelated":{"token":"private"}}\n', encoding="utf-8")
        plan = build_policy_plan(self.project, {"alpha": "off"})
        self.assertNotIn("do-not-echo", json.dumps(plan))
        self.assertNotIn("private", json.dumps(plan))
        self.assertNotIn("settings_content", plan)
        applied = apply_policy_plan(self.project, {"alpha": "off"}, plan["plan_sha256"])
        written = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(written["apiKey"], "do-not-echo")
        rollback = json.loads(Path(applied["rollback_record"]).read_text(encoding="utf-8"))
        self.assertNotIn("before_base64", rollback)
        self.assertNotIn("do-not-echo", json.dumps(rollback))
        restore_plan = build_policy_restore_plan(self.project, applied["rollback_record"])
        self.assertNotIn("do-not-echo", json.dumps(restore_plan))
        self.assertNotIn("content_base64", json.dumps(restore_plan))
        restored = restore_policy(self.project, applied["rollback_record"], restore_plan["plan_sha256"])
        self.assertEqual(restored["restored"], 1)
        after_restore = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(after_restore["apiKey"], "do-not-echo")
        self.assertNotIn("skillOverrides", after_restore)

    def test_restore_fails_closed_after_settings_drift(self):
        plan = build_policy_plan(self.project, {"alpha": "off"})
        applied = apply_policy_plan(self.project, {"alpha": "off"}, plan["plan_sha256"])
        settings = self.project / ".claude" / "settings.local.json"
        settings.write_text('{"skillOverrides":{"alpha":"on"}}\n', encoding="utf-8")
        restore_plan = build_policy_restore_plan(self.project, applied["rollback_record"])
        self.assertTrue(restore_plan["errors"])
        with self.assertRaises(SkillManagementError):
            restore_policy(self.project, applied["rollback_record"], restore_plan["plan_sha256"])

    def test_public_policy_plans_do_not_expose_unrelated_settings(self):
        settings = self.project / ".claude" / "settings.local.json"
        settings.write_text(
            json.dumps({"apiToken": "private-value", "skillOverrides": {"alpha": "on"}}) + "\n",
            encoding="utf-8",
        )
        plan = build_policy_plan(self.project, {"alpha": "off"})
        serialized = json.dumps(plan, sort_keys=True)
        self.assertNotIn("private-value", serialized)
        self.assertNotIn("apiToken", serialized)
        applied = apply_policy_plan(self.project, {"alpha": "off"}, plan["plan_sha256"])
        restore_plan = build_policy_restore_plan(self.project, applied["rollback_record"])
        restore_serialized = json.dumps(restore_plan, sort_keys=True)
        self.assertNotIn("private-value", restore_serialized)
        self.assertNotIn("apiToken", restore_serialized)

    def test_restore_record_must_use_the_canonical_policy_rollback_location(self):
        forged = self.project / "AI" / "work" / "forged.json"
        forged.parent.mkdir(parents=True, exist_ok=True)
        forged.write_text("{}\n", encoding="utf-8")
        plan = build_policy_restore_plan(self.project, forged)
        self.assertTrue(plan["errors"])
        self.assertIn("canonical", plan["errors"][0])

    def test_rollback_record_rejects_forged_before_base64_field(self):
        apply_plan = build_policy_plan(self.project, {"alpha": "off"})
        applied = apply_policy_plan(self.project, {"alpha": "off"}, apply_plan["plan_sha256"])
        record = Path(applied["rollback_record"])
        value = json.loads(record.read_text(encoding="utf-8"))
        value["before_base64"] = "eyJhcmJpdHJhcnkiOiJub3QtYWxsb3dlZCJ9"
        record.write_text(json.dumps(value) + "\n", encoding="utf-8")
        restore_plan = build_policy_restore_plan(self.project, record)
        self.assertTrue(restore_plan["errors"], restore_plan)
        self.assertNotIn("before_base64", json.dumps(restore_plan))
        with self.assertRaises(SkillManagementError):
            restore_policy(self.project, record, restore_plan["plan_sha256"])
        self.assertEqual(policy_status(self.project)["skills"][0]["current_policy"], "off")

    def test_tampered_rollback_operation_fails_apply_plan_provenance(self):
        apply_plan = build_policy_plan(self.project, {"alpha": "off"})
        applied = apply_policy_plan(self.project, {"alpha": "off"}, apply_plan["plan_sha256"])
        record = Path(applied["rollback_record"])
        value = json.loads(record.read_text(encoding="utf-8"))
        value["operations"][0]["before"] = "on"
        record.write_text(json.dumps(value) + "\n", encoding="utf-8")
        restore_plan = build_policy_restore_plan(self.project, record)
        self.assertTrue(any("apply plan" in error for error in restore_plan["errors"]), restore_plan)
        with self.assertRaises(SkillManagementError):
            restore_policy(self.project, record, restore_plan["plan_sha256"])
        self.assertEqual(policy_status(self.project)["skills"][0]["current_policy"], "off")

    def test_restore_write_failure_rolls_back_to_the_pre_restore_settings(self):
        settings = self.project / ".claude" / "settings.local.json"
        settings.write_text('{"unrelated":"keep"}\n', encoding="utf-8")
        apply_plan = build_policy_plan(self.project, {"alpha": "off"})
        applied = apply_policy_plan(self.project, {"alpha": "off"}, apply_plan["plan_sha256"])
        before_restore = settings.read_bytes()
        restore_plan = build_policy_restore_plan(self.project, applied["rollback_record"])
        original_write = policy_module.atomic_write_bytes
        calls = 0

        def fail_after_first_write(path, data):
            nonlocal calls
            calls += 1
            original_write(path, data)
            if calls == 1:
                raise OSError("synthetic post-write failure")

        with mock.patch.object(policy_module, "atomic_write_bytes", side_effect=fail_after_first_write):
            with self.assertRaises(SkillManagementError):
                restore_policy(self.project, applied["rollback_record"], restore_plan["plan_sha256"])
        self.assertEqual(settings.read_bytes(), before_restore)

    def test_restore_delete_verification_failure_rolls_back_to_the_pre_restore_settings(self):
        apply_plan = build_policy_plan(self.project, {"alpha": "off"})
        applied = apply_policy_plan(self.project, {"alpha": "off"}, apply_plan["plan_sha256"])
        settings = self.project / ".claude" / "settings.local.json"
        before_restore = settings.read_bytes()
        restore_plan = build_policy_restore_plan(self.project, applied["rollback_record"])
        self.assertTrue(restore_plan["delete_settings"], restore_plan)
        original_sha256_file = policy_module.sha256_file
        verification_failed = False

        def fail_deleted_settings_verification(path):
            nonlocal verification_failed
            if Path(path) == settings and not settings.exists() and not verification_failed:
                verification_failed = True
                return "f" * 64
            return original_sha256_file(path)

        with mock.patch.object(policy_module, "sha256_file", side_effect=fail_deleted_settings_verification):
            with self.assertRaises(SkillManagementError):
                restore_policy(self.project, applied["rollback_record"], restore_plan["plan_sha256"])
        self.assertTrue(verification_failed)
        self.assertEqual(settings.read_bytes(), before_restore)


class StandaloneCliTest(ScratchTest):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--json", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    def test_inventory_json(self):
        write_leaf(self.store, "alpha")
        proc = self.run_cli("inventory", "--project-root", str(self.project), "--store-root", str(self.store))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["skills"][0]["name"], "alpha")

    def test_codex_policy_is_explicitly_audit_only(self):
        proc = self.run_cli("policy", "plan", "--project-root", str(self.project), "--host", "codex", "--policy", "alpha=off")
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("audit-only", payload["message"])


if __name__ == "__main__":
    unittest.main()
