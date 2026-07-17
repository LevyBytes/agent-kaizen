"""SK1-SK8 aliases, safe defaults, confirmation gates, and read-only facade smoke tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kaizen_components import args as cli
from kaizen_components.denials import KaizenDenied


ALIASES = {
    "SK1": "skill-inventory",
    "SK2": "skill-validate",
    "SK3": "skill-links",
    "SK4": "skill-index",
    "SK5": "skill-policy",
    "SK6": "skill-context-sync",
    "SK7": "skill-context-query",
    "SK8": "skill-context-status",
}


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class SkillAliasParserTest(unittest.TestCase):
    def test_codes_and_long_aliases_are_identical(self):
        for code, alias in ALIASES.items():
            with self.subTest(code=code):
                self.assertEqual(cli.canonical_operation(code), code)
                self.assertEqual(cli.canonical_operation(alias), code)

    def test_every_alias_is_unique_case_insensitively(self):
        flattened = [alias.lower() for aliases in cli.ALIASES.values() for alias in aliases]
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertEqual(len(cli.ALIAS_TO_CODE), len(flattened))

    def test_skill_flags_parse_with_repeatable_hosts_and_skills(self):
        parsed = cli.build_parser().parse_args(
            [
                "skill-policy",
                "--store-root",
                "D:/skills",
                "--host",
                "codex",
                "--host",
                "claude",
                "--skill",
                "git",
                "--skill",
                "github",
                "--desired",
                "name-only",
                "--policy",
                "git=name-only",
                "--confirm-plan",
                "a" * 64,
            ]
        )
        self.assertEqual(parsed.host, ["codex", "claude"])
        self.assertEqual(parsed.skill, ["git", "github"])
        self.assertEqual(parsed.desired, "name-only")
        self.assertEqual(parsed.policy, ["git=name-only"])
        self.assertEqual(parsed.confirm_plan, "a" * 64)

    def test_k0_finds_skill_context_query(self):
        parsed = cli.build_parser().parse_args(["K0", "--query", "load skill context for a task"])
        result = cli.op_lookup(parsed)
        self.assertIn("SK7", [record["code"] for record in result["records"][:3]], result)


class SkillDefaultSafetyTest(unittest.TestCase):
    def test_management_defaults_call_only_read_only_status_handlers(self):
        cases = (
            ("SK3", "link_status", "apply_link_plan"),
            ("SK4", "index_status", "apply_index_plan"),
            ("SK5", "policy_status", "apply_policy_plan"),
        )
        for operation, status_name, apply_name in cases:
            with self.subTest(operation=operation), mock.patch(
                f"support_scripts.skill_management.{status_name}", return_value={"kind": operation}
            ) as status_handler, mock.patch(
                f"support_scripts.skill_management.{apply_name}", return_value={"status": "OK"}
            ) as apply_handler:
                result = cli.dispatch(cli.build_parser().parse_args([operation]))
                self.assertEqual(result["status"], "OK")
                self.assertEqual(result["action"], "status")
                status_handler.assert_called_once()
                apply_handler.assert_not_called()

    def test_context_sync_defaults_to_read_only_plan(self):
        with mock.patch("kaizen_components.skill_context.context_sync", return_value={"status": "OK"}) as handler:
            parsed = cli.build_parser().parse_args(["skill-context-sync", "--host", "both"])
            result = cli.dispatch(parsed)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(handler.call_args.args[0].action, "plan")
        self.assertEqual(handler.call_args.args[0].host, ["both"])

    def test_context_query_and_status_long_aliases_dispatch(self):
        with mock.patch("kaizen_components.skill_context.context_query", return_value={"status": "OK"}) as query:
            result = cli.dispatch(
                cli.build_parser().parse_args(
                    ["skill-context-query", "--query", "review installer", "--host", "codex"]
                )
            )
        self.assertEqual(result["status"], "OK")
        query.assert_called_once()
        with mock.patch("kaizen_components.skill_context.context_status", return_value={"status": "OK"}) as status:
            result = cli.dispatch(cli.build_parser().parse_args(["skill-context-status"]))
        self.assertEqual(result["status"], "OK")
        status.assert_called_once()

    def test_public_host_both_expands_for_support_facade(self):
        with mock.patch(
            "support_scripts.skill_management.discover_skills",
            return_value={"skills": [], "orphan_surfaces": []},
        ) as discover:
            result = cli.dispatch(cli.build_parser().parse_args(["SK1", "--host", "both"]))
        self.assertEqual(result["status"], "OK")
        self.assertEqual(discover.call_args.args[2], ("codex", "claude"))

    def test_every_apply_denies_before_handler_without_confirm_plan(self):
        cases = (
            ("SK3", ["--action", "apply", "--skill", "demo"], "apply_link_plan"),
            ("SK4", ["--action", "apply"], "apply_index_plan"),
            ("SK5", ["--action", "apply", "--host", "claude", "--policy", "demo=off"], "apply_policy_plan"),
        )
        for operation, extra, handler_name in cases:
            with self.subTest(operation=operation), mock.patch(
                f"support_scripts.skill_management.{handler_name}", return_value={"status": "OK"}
            ) as handler:
                with self.assertRaises(KaizenDenied) as denied:
                    cli.dispatch(cli.build_parser().parse_args([operation, *extra]))
                self.assertEqual(denied.exception.code, "DENIED_SKILL_CONFIRM_PLAN_REQUIRED")
                handler.assert_not_called()

        with mock.patch("kaizen_components.skill_context.context_sync", return_value={"status": "OK"}) as handler:
            with self.assertRaises(KaizenDenied) as denied:
                cli.dispatch(cli.build_parser().parse_args(["SK6", "--action", "apply"]))
            self.assertEqual(denied.exception.code, "DENIED_SKILL_CONFIRM_PLAN_REQUIRED")
            handler.assert_not_called()

    def test_codex_policy_is_status_only_and_mutations_deny(self):
        with mock.patch("support_scripts.skill_management.policy_status") as claude_status:
            status = cli.dispatch(cli.build_parser().parse_args(["SK5", "--host", "codex"]))
        self.assertEqual(status["policy_mode"], "audit-only")
        self.assertFalse(status["supported"])
        claude_status.assert_not_called()
        with mock.patch("support_scripts.skill_management.build_policy_plan") as planner:
            with self.assertRaises(KaizenDenied) as denied:
                cli.dispatch(
                    cli.build_parser().parse_args(
                        ["SK5", "--host", "codex", "--action", "plan", "--policy", "demo=off"]
                    )
                )
        self.assertEqual(denied.exception.code, "DENIED_SKILL_POLICY_HOST_UNSUPPORTED")
        planner.assert_not_called()

    def test_valid_confirm_plan_reaches_recomputing_apply_handler(self):
        confirmation = "b" * 64
        with mock.patch(
            "support_scripts.skill_management.apply_link_plan",
            return_value={"status": "OK", "action": "apply", "plan_sha256": confirmation},
        ) as handler:
            result = cli.dispatch(
                cli.build_parser().parse_args(
                    ["SK3", "--action", "apply", "--skill", "demo", "--confirm-plan", confirmation]
                )
            )
        self.assertEqual(result["plan_sha256"], confirmation)
        self.assertEqual(handler.call_args.kwargs["confirm_plan"], confirmation)


class SkillReadOnlyFacadeSmokeTest(unittest.TestCase):
    def test_inventory_validation_and_default_status_do_not_write(self):
        with tempfile.TemporaryDirectory(prefix="kaizen-skill-ops-") as temporary:
            root = Path(temporary)
            project = root / "project"
            store = root / "store"
            package = store / "demo"
            project.mkdir()
            package.mkdir(parents=True)
            (package / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Use for isolated skill-operation tests.\n---\n\n# Demo\n",
                encoding="utf-8",
            )
            before = _snapshot(root)
            with mock.patch.object(cli, "REPO_ROOT", project):
                inv = cli.dispatch(
                    cli.build_parser().parse_args(["skill-inventory", "--store-root", str(store)])
                )
                valid = cli.dispatch(
                    cli.build_parser().parse_args(["skill-validate", "--store-root", str(store)])
                )
                links = cli.dispatch(
                    cli.build_parser().parse_args(["skill-links", "--store-root", str(store)])
                )
                indexes = cli.dispatch(cli.build_parser().parse_args(["skill-index"]))
                policy = cli.dispatch(cli.build_parser().parse_args(["skill-policy"]))
            self.assertEqual(inv["count"], 1, inv)
            self.assertEqual(valid["count"], 1, valid)
            self.assertEqual(links["action"], "status")
            self.assertEqual(indexes["action"], "status")
            self.assertEqual(policy["action"], "status")
            self.assertEqual(_snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
