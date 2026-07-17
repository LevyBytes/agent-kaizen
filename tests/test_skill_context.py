"""SK6-SK8 validated skill-context snapshots, live retrieval, freshness, and purge behavior."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from _harness import REPO_ROOT, IsolatedDBTest
from kaizen_components.hashing import utc_text_hash


def _create_directory_link(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if proc.returncode:
            raise OSError(proc.stderr or proc.stdout)
    else:
        os.symlink(target, path, target_is_directory=True)


def _read_sql(db_path: Path, sql: str) -> list[list[object]]:
    script = (
        "import json,sys,turso\n"
        "conn=turso.connect(sys.argv[1])\n"
        "rows=[list(row) for row in conn.execute(sys.argv[2]).fetchall()]\n"
        "conn.close()\n"
        "print(json.dumps(rows))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(db_path), sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if proc.returncode:
        raise AssertionError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout)


def _execute_sql(db_path: Path, sql: str) -> subprocess.CompletedProcess:
    script = (
        "import sys,turso\n"
        "conn=turso.connect(sys.argv[1])\n"
        "conn.execute(sys.argv[2])\n"
        "conn.commit()\n"
        "conn.close()\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script, str(db_path), sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


class SkillContextTest(IsolatedDBTest):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.root / "external-skills"
        self.store.mkdir()
        self.db_path = self.root / "AI" / "db" / "kaizen.db"

    def skill(
        self,
        name: str,
        *,
        description: str = "Use when working with Git commits and repository history.",
        covers: tuple[str, ...] = ("git", "commit", "repository"),
        body: str = "# Git\n\nUse repository evidence before changing history.\n",
        valid: bool = True,
    ) -> Path:
        root = self.store / name
        root.mkdir(parents=True, exist_ok=True)
        if valid:
            frontmatter = (
                "---\n"
                f"name: {name}\n"
                f"description: {description}\n"
                f"covers: [{', '.join(covers)}]\n"
                "---\n\n"
            )
        else:
            frontmatter = f"---\nname: {name}\n---\n\n"
        guidance = (
            "\n## Workflow\n\nInspect first, then apply the bounded change.\n"
            "\n## References\n\nUse [the topic reference](references/topic.md).\n"
            "\n## Verification\n\nRe-run the focused deterministic check.\n"
            "\n## Gotchas\n\nReject stale evidence before acting.\n"
        )
        (root / "SKILL.md").write_text(frontmatter + body + guidance, encoding="utf-8")
        references = root / "references"
        references.mkdir()
        (references / "topic.md").write_text("# Topic\n\nVerified topic guidance.\n", encoding="utf-8")
        (references / "INDEX.md").write_text("# References\n\n- [Topic](topic.md)\n", encoding="utf-8")
        (references / "topics.json").write_text(
            json.dumps(
                {
                    "topics": [
                        {
                            "topic": "Topic",
                            "summary": "Verified topic guidance.",
                            "keywords": ["topic"],
                            "file": "references/topic.md",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return root

    def surface(self, name: str, *hosts: str) -> None:
        for host in hosts or ("codex", "claude"):
            root = ".agents" if host == "codex" else ".claude"
            _create_directory_link(self.root / root / "skills" / name, self.store / name)

    def claude_policy(self, **states: str) -> None:
        path = self.root / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"skillOverrides": states}), encoding="utf-8")

    def plan(self, *, test: bool = False, hosts: tuple[str, ...] = ()) -> dict:
        args = ["SK6", "--action", "plan", "--store-root", str(self.store)]
        for host in hosts:
            args.extend(("--host", host))
        if test:
            args.append("--test")
        rc, payload = self.kz(*args)
        self.assertEqual(rc, 0, payload)
        return payload

    def apply(self, *, test: bool = False, hosts: tuple[str, ...] = ()) -> dict:
        plan = self.plan(test=test, hosts=hosts)
        args = [
            "skill-context-sync",
            "--action",
            "apply",
            "--confirm-plan",
            plan["plan_sha256"],
            "--store-root",
            str(self.store),
        ]
        for host in hosts:
            args.extend(("--host", host))
        if test:
            args.append("--test")
        rc, payload = self.kz(*args)
        self.assertEqual(rc, 0, payload)
        return payload

    def test_plan_apply_query_and_status_are_portable_and_query_is_read_only(self) -> None:
        self.skill("git")
        self.surface("git", "codex")
        plan = self.plan()
        self.assertEqual(plan["writes"], 0)
        self.assertEqual(plan["store_root"], str(self.store.resolve()))
        self.assertEqual(plan["hosts"], ["codex", "claude"])
        self.assertFalse(plan["is_test"])
        self.assertEqual(plan["skill_count"], 1)
        self.assertEqual(plan["surface_count"], 2)
        self.assertEqual(_read_sql(self.db_path, "SELECT COUNT(*) FROM skill_context_syncs"), [[0]])

        applied = self.apply()
        self.assertEqual(applied["skill_count"], 1)
        self.assertEqual(applied["surface_count"], 2)
        self.assertEqual(
            _read_sql(self.db_path, "SELECT skill_md_relpath FROM skill_contexts"),
            [["git/SKILL.md"]],
        )
        self.assertEqual(
            _read_sql(self.db_path, "SELECT host, surface_relpath FROM skill_context_surfaces ORDER BY host"),
            [["claude", ".claude/skills/git"], ["codex", ".agents/skills/git"]],
        )
        before = _read_sql(self.db_path, "SELECT COUNT(*) FROM skill_context_events")
        rc, query = self.kz(
            "SK7", "--query", "prepare a git commit", "--host", "codex", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, query)
        self.assertEqual(query["count"], 1)
        self.assertEqual(query["matches"][0]["name"], "git")
        self.assertEqual(query["matches"][0]["publication_status"], "staged")
        self.assertEqual(query["matches"][0]["policy_state"], "on")
        self.assertIn("Use repository evidence", query["matches"][0]["context"])
        self.assertEqual(_read_sql(self.db_path, "SELECT COUNT(*) FROM skill_context_events"), before)

        rc, status = self.kz("SK8", "--store-root", str(self.store))
        self.assertEqual(rc, 0, status)
        self.assertTrue(status["current"], status)
        self.assertEqual(status["fresh"], 1)
        self.assertEqual(status["surface_drift"], 0)

    def test_query_requires_one_concrete_host(self) -> None:
        self.skill("git")
        self.apply()
        for host_args in ((), ("--host", "both"), ("--host", "codex", "--host", "claude")):
            with self.subTest(host_args=host_args):
                rc, payload = self.kz(
                    "SK7", "--query", "git commit", *host_args, "--store-root", str(self.store)
                )
                self.assertEqual(rc, 2, payload)
                self.assertEqual(payload["code"], "DENIED_SKILL_CONTEXT_HOST_REQUIRED")

    def test_stable_missing_surface_is_ineligible_but_not_stale(self) -> None:
        self.skill("git")
        self.apply()
        rc, payload = self.kz(
            "SK7", "--query", "git commit", "--host", "codex", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, payload)
        self.assertTrue(payload["current"], payload)
        self.assertEqual(payload["matches"], [])
        self.assertEqual(payload["excluded"][0]["reason"], "surface_missing")
        rc, status = self.kz("SK8", "--store-root", str(self.store))
        self.assertEqual(rc, 0, status)
        self.assertTrue(status["current"], status)
        self.assertEqual(status["surface_counts"]["missing"], 2)
        self.assertEqual(status["automatic_context_surfaces"], 0)
        self.surface("git", "codex")
        rc, changed = self.kz(
            "SK7", "--query", "git commit", "--host", "codex", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, changed)
        self.assertFalse(changed["current"], changed)
        self.assertEqual(changed["excluded"][0]["reason"], "stale_surface")

    def test_publication_and_host_policy_are_independent_live_dimensions(self) -> None:
        self.skill("git")
        self.surface("git", "codex", "claude")
        self.claude_policy(git="name-only")
        self.apply()
        rows = _read_sql(
            self.db_path,
            "SELECT c.publication_status, c.publication_reason, s.host, s.policy_state "
            "FROM skill_contexts c JOIN skill_context_surfaces s ON s.context_id = c.id ORDER BY s.host",
        )
        self.assertEqual(
            rows,
            [
                ["staged", "no_git_metadata", "claude", "name-only"],
                ["staged", "no_git_metadata", "codex", "on"],
            ],
        )

        rc, codex = self.kz(
            "SK7", "--query", "git commit", "--host", "codex", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, codex)
        self.assertEqual(codex["matches"][0]["publication_status"], "staged")
        rc, claude = self.kz(
            "SK7", "--query", "git commit", "--host", "claude", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, claude)
        self.assertEqual(claude["matches"], [])
        self.assertEqual(claude["excluded"][0]["reason"], "policy_name_only")
        self.assertTrue(claude["current"], claude)

        rc, status = self.kz("SK8", "--store-root", str(self.store))
        self.assertEqual(rc, 0, status)
        self.assertTrue(status["current"], status)
        self.assertEqual(status["policy_counts"]["name-only"], 1)
        self.assertEqual(status["publication_counts"], {"published": 0, "staged": 1})
        self.assertEqual(status["policy_restricted_surfaces"], 1)

        self.claude_policy(git="user-invocable-only")
        rc, changed = self.kz(
            "SK7", "--query", "git commit", "--host", "claude", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, changed)
        self.assertEqual(changed["excluded"][0]["reason"], "policy_user_invocable_only")
        self.assertFalse(changed["current"], changed)

        self.claude_policy(git="off")
        rc, changed = self.kz(
            "SK7", "--query", "git commit", "--host", "claude", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, changed)
        self.assertEqual(changed["excluded"][0]["reason"], "policy_off")
        self.assertFalse(changed["current"], changed)
        rc, status = self.kz("SK8", "--store-root", str(self.store))
        self.assertEqual(rc, 0, status)
        self.assertFalse(status["current"], status)
        self.assertEqual(status["policy_drift"], 1)

    def test_validated_github_remote_is_persisted_without_its_url(self) -> None:
        package = self.skill("git")
        metadata = package / ".git"
        metadata.mkdir()
        (metadata / "config").write_text(
            '[remote "origin"]\n\turl = git@github.com:example/git-skill.git\n',
            encoding="utf-8",
        )
        self.surface("git", "codex")
        self.apply(hosts=("codex",))
        self.assertEqual(
            _read_sql(
                self.db_path,
                "SELECT publication_status, publication_reason FROM skill_contexts",
            ),
            [["published", "github_remote"]],
        )
        rc, payload = self.kz(
            "SK7", "--query", "git commit", "--host", "codex", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["matches"][0]["publication_status"], "published")
        self.assertNotIn("github.com", json.dumps(payload))

    def test_orphan_host_surface_blocks_sync_and_makes_status_not_current(self) -> None:
        self.skill("git")
        self.apply()
        orphan = self.root / ".agents" / "skills" / "rogue"
        orphan.mkdir(parents=True)
        (orphan / "SKILL.md").write_text("# Rogue\n", encoding="utf-8")

        rc, status = self.kz("SK8", "--store-root", str(self.store))
        self.assertEqual(rc, 0, status)
        self.assertFalse(status["current"], status)
        self.assertEqual(status["orphan_surface_count"], 1)
        self.assertEqual(
            status["orphan_surfaces"],
            [
                {
                    "name": "rogue",
                    "host": "codex",
                    "status": "real_directory",
                    "surface_relpath": ".agents/skills/rogue",
                }
            ],
        )
        self.assertNotIn(str(self.root), json.dumps(status))

        rc, denied = self.kz("SK6", "--action", "plan", "--store-root", str(self.store))
        self.assertEqual(rc, 2, denied)
        self.assertEqual(denied["code"], "DENIED_SKILL_CONTEXT_ORPHAN_SURFACE")
        self.assertNotIn(str(self.root), json.dumps(denied))

    def test_same_name_real_directory_surface_blocks_sync_and_is_not_current(self) -> None:
        self.skill("git")
        self.apply()
        shadow = self.root / ".agents" / "skills" / "git"
        shadow.mkdir(parents=True)
        (shadow / "SKILL.md").write_text("# Shadow\n", encoding="utf-8")

        rc, status = self.kz("SK8", "--store-root", str(self.store))
        self.assertEqual(rc, 0, status)
        self.assertFalse(status["current"], status)
        self.assertEqual(status["surface_conflict_count"], 1)
        self.assertEqual(status["surface_conflicts"][0]["status"], "real_directory")
        self.assertFalse(status["surface_conflicts"][0]["orphan"])
        self.assertNotIn(str(self.root), json.dumps(status))

        rc, denied = self.kz("SK6", "--action", "plan", "--store-root", str(self.store))
        self.assertEqual(rc, 2, denied)
        self.assertEqual(denied["code"], "DENIED_SKILL_CONTEXT_SURFACE_CONFLICT")

    def test_same_name_wrong_target_surface_blocks_sync_and_is_not_current(self) -> None:
        self.skill("git")
        self.apply()
        outside = self.root / "outside-shadow"
        outside.mkdir()
        (outside / "SKILL.md").write_text("# Shadow\n", encoding="utf-8")
        try:
            _create_directory_link(self.root / ".agents" / "skills" / "git", outside)
        except OSError as exc:
            self.skipTest(f"directory links unavailable: {exc}")

        rc, status = self.kz("SK8", "--store-root", str(self.store))
        self.assertEqual(rc, 0, status)
        self.assertFalse(status["current"], status)
        self.assertEqual(status["surface_conflicts"][0]["status"], "wrong_target")
        self.assertFalse(status["surface_conflicts"][0]["orphan"])
        self.assertNotIn(str(self.root), json.dumps(status))

        rc, denied = self.kz("SK6", "--action", "plan", "--store-root", str(self.store))
        self.assertEqual(rc, 2, denied)
        self.assertEqual(denied["code"], "DENIED_SKILL_CONTEXT_SURFACE_CONFLICT")

    def test_sk1_through_sk5_integrated_read_only_defaults(self) -> None:
        self.skill("git")
        rc, inventory = self.kz("SK1", "--store-root", str(self.store))
        self.assertEqual(rc, 0, inventory)
        rc, validation = self.kz("SK2", "--store-root", str(self.store))
        self.assertEqual(rc, 0, validation)
        rc, links = self.kz("SK3", "--store-root", str(self.store))
        self.assertEqual(rc, 0, links)
        rc, indexes = self.kz("SK4", "--store-root", str(self.store))
        self.assertEqual(rc, 0, indexes)
        rc, policy = self.kz("SK5", "--store-root", str(self.store))
        self.assertEqual(rc, 0, policy)
        self.assertEqual(inventory["action"], "inventory")
        self.assertEqual(validation["action"], "validate")
        self.assertEqual(links["action"], "status")
        self.assertEqual(indexes["action"], "status")
        self.assertEqual(policy["action"], "status")

    def test_package_only_drift_is_excluded_without_a_telemetry_write(self) -> None:
        root = self.skill(
            "git",
            body="# Git\n\nSee [rules](references/rules.md) before changing history.\n",
        )
        references = root / "references"
        (references / "rules.md").write_text("Initial rules.\n", encoding="utf-8")
        self.surface("git", "codex")
        self.apply()
        skill_hash = _read_sql(self.db_path, "SELECT skill_sha256 FROM skill_contexts")[0][0]
        event_count = _read_sql(self.db_path, "SELECT COUNT(*) FROM skill_context_events")

        (references / "rules.md").write_text("Changed rules.\n", encoding="utf-8")
        self.assertEqual(_read_sql(self.db_path, "SELECT skill_sha256 FROM skill_contexts")[0][0], skill_hash)
        rc, payload = self.kz(
            "SK7", "--query", "git history", "--host", "codex", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["matches"], [])
        self.assertEqual(payload["excluded"][0]["reason"], "stale_package")
        self.assertEqual(payload["excluded"][0]["publication_status"], "staged")
        self.assertFalse(payload["current"])
        self.assertEqual(_read_sql(self.db_path, "SELECT COUNT(*) FROM skill_context_events"), event_count)

    def test_fully_enumerated_invalid_package_is_retained_but_never_loaded(self) -> None:
        self.skill("git")
        self.skill("broken", valid=False, body="# Broken\n")
        self.surface("git", "codex")
        self.surface("broken", "codex")
        (self.store / "missing-skill-md").mkdir()
        plan = self.plan()
        self.assertEqual(plan["skill_count"], 3)
        self.apply()
        rows = _read_sql(
            self.db_path,
            "SELECT skill_name, validation_status FROM skill_contexts ORDER BY skill_name",
        )
        self.assertEqual(rows, [["broken", "invalid"], ["git", "valid"], ["missing-skill-md", "invalid"]])

        rc, payload = self.kz(
            "SK7", "--query", "broken git commit", "--host", "codex", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, payload)
        self.assertEqual([match["name"] for match in payload["matches"]], ["git"])
        self.assertIn("invalid", [item["reason"] for item in payload["excluded"] if item["name"] == "broken"])
        rc, status = self.kz("SK8", "--store-root", str(self.store))
        self.assertEqual(rc, 0, status)
        self.assertTrue(status["current"], status)
        self.assertFalse(status["validation_healthy"])
        self.assertEqual(status["invalid"], 2)

    def test_persisted_validation_errors_redact_known_absolute_roots(self) -> None:
        from kaizen_components import skill_context as sc

        package = self.store / "broken"
        package.mkdir()
        inventory = {
            "schema_version": 1,
            "complete": True,
            "scan_errors": [],
            "store_root": str(self.store),
            "hosts": ["codex", "claude"],
            "skills": [
                {
                    "name": "broken",
                    "source_path": str(package),
                    "source_relpath": "broken",
                    "skill_md_relative_path": "broken/SKILL.md",
                    "skill_md_sha256": "",
                    "package_sha256": "a" * 64,
                    "publication_status": "staged",
                    "publication_reason": "no_remote",
                    "valid": False,
                    "description": "",
                    "covers": [],
                    "errors": [f"cannot read {package / 'SKILL.md'}"],
                    "hosts": {},
                }
            ],
        }
        records = sc._normalize_inventory(inventory, ("codex", "claude"))
        diagnostic = records[0]["validation_errors"][0]
        self.assertNotIn(str(self.store), diagnostic)
        self.assertIn("<skill-package>", diagnostic)

    def test_failed_recompute_does_not_retire_the_current_snapshot(self) -> None:
        self.skill("git")
        applied = self.apply()
        before = _read_sql(
            self.db_path,
            "SELECT id, is_current, inventory_hash FROM skill_context_syncs WHERE is_current = 1",
        )
        missing = self.root / "missing-store"
        rc, denied = self.kz(
            "SK6",
            "--action",
            "apply",
            "--confirm-plan",
            applied["plan_sha256"],
            "--store-root",
            str(missing),
        )
        self.assertNotEqual(rc, 0, denied)
        self.assertEqual(denied["code"], "DENIED_SKILL_STORE_NOT_FOUND")
        self.assertEqual(
            _read_sql(self.db_path, "SELECT id, is_current, inventory_hash FROM skill_context_syncs WHERE is_current = 1"),
            before,
        )

        script = (
            "import json,sys\n"
            "from types import SimpleNamespace\n"
            "from kaizen_components import skill_context as sc\n"
            "from kaizen_components.denials import KaizenDenied\n"
            "sc.discover_skills=lambda *a,**k:{'schema_version':1,'complete':False,'scan_errors':['enumeration failed'],'skills':[]}\n"
            "args=SimpleNamespace(action='apply',store_root=sys.argv[1],host=None,confirm_plan=sys.argv[2],test=False)\n"
            "try:\n"
            " sc.context_sync(args)\n"
            "except KaizenDenied as exc:\n"
            " print(json.dumps(exc.payload()))\n"
            "else:\n"
            " raise SystemExit('expected denial')\n"
        )
        env = dict(os.environ)
        env["KAIZEN_REPO_ROOT"] = str(self.root)
        env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(self.root / "AI" / "work")
        proc = subprocess.run(
            [sys.executable, "-c", script, str(self.store), applied["plan_sha256"]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(REPO_ROOT),
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertEqual(json.loads(proc.stdout)["code"], "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE")
        self.assertEqual(
            _read_sql(self.db_path, "SELECT id, is_current, inventory_hash FROM skill_context_syncs WHERE is_current = 1"),
            before,
        )

    def test_test_snapshot_purges_children_before_its_root(self) -> None:
        self.skill("git")
        self.apply(test=True)
        rc, payload = self.kz("K7")
        self.assertEqual(rc, 0, payload)
        for table in ("skill_context_surfaces", "skill_context_events", "skill_contexts", "skill_context_syncs"):
            self.assertEqual(_read_sql(self.db_path, f"SELECT COUNT(*) FROM {table}"), [[0]], table)

    def test_replayed_apply_is_a_no_op_without_snapshot_or_event_churn(self) -> None:
        self.skill("git")
        first = self.apply()
        before_syncs = _read_sql(self.db_path, "SELECT COUNT(*) FROM skill_context_syncs")
        before_events = _read_sql(self.db_path, "SELECT COUNT(*) FROM skill_context_events")
        plan = self.plan()
        rc, replay = self.kz(
            "SK6",
            "--action",
            "apply",
            "--confirm-plan",
            plan["plan_sha256"],
            "--store-root",
            str(self.store),
        )
        self.assertEqual(rc, 0, replay)
        self.assertTrue(replay["no_op"])
        self.assertEqual(replay["sync_id"], first["sync_id"])
        self.assertEqual(_read_sql(self.db_path, "SELECT COUNT(*) FROM skill_context_syncs"), before_syncs)
        self.assertEqual(_read_sql(self.db_path, "SELECT COUNT(*) FROM skill_context_events"), before_events)

    def test_integrity_failure_is_unavailable_and_confirmed_reapply_repairs_it(self) -> None:
        self.skill("git")
        first = self.apply()
        corrupted = _execute_sql(self.db_path, "UPDATE skill_contexts SET description = 'corrupt'")
        self.assertEqual(corrupted.returncode, 0, corrupted.stderr)
        rc, query = self.kz(
            "SK7", "--query", "git commit", "--host", "codex", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, query)
        self.assertFalse(query["available"])
        self.assertEqual(query["reason"], "DENIED_SKILL_CONTEXT_INTEGRITY")
        plan = self.plan()
        rc, repaired = self.kz(
            "SK6",
            "--action",
            "apply",
            "--confirm-plan",
            plan["plan_sha256"],
            "--store-root",
            str(self.store),
        )
        self.assertEqual(rc, 0, repaired)
        self.assertFalse(repaired["no_op"])
        self.assertNotEqual(repaired["sync_id"], first["sync_id"])
        rc, status = self.kz("SK8", "--store-root", str(self.store))
        self.assertEqual(rc, 0, status)
        self.assertTrue(status["current"], status)

    def test_self_consistent_child_forgery_cannot_bypass_snapshot_inventory_hash(self) -> None:
        self.skill("git")
        first = self.apply()
        row = _read_sql(
            self.db_path,
            "SELECT id, skill_name, description, covers_json, skill_md_relpath, skill_sha256, "
            "package_sha256, validation_status, validation_errors_json, publication_status, "
            "publication_reason FROM skill_contexts",
        )[0]
        surface_rows = _read_sql(
            self.db_path,
            "SELECT host, surface_relpath, path_kind, validation_status, policy_state, content_hash "
            "FROM skill_context_surfaces WHERE context_id = '" + row[0] + "' ORDER BY host",
        )
        forged_description = "forged routing metadata"
        record = {
            "name": row[1],
            "description": forged_description,
            "covers": json.loads(row[3]),
            "skill_md_relpath": row[4],
            "skill_sha256": row[5],
            "package_sha256": row[6],
            "validation_status": row[7],
            "validation_errors": json.loads(row[8]),
            "publication_status": row[9],
            "publication_reason": row[10],
            "surfaces": [
                {
                    "host": surface[0],
                    "surface_relpath": surface[1],
                    "path_kind": surface[2],
                    "validation_status": surface[3],
                    "policy_state": surface[4],
                    "content_hash": surface[5],
                }
                for surface in sorted(surface_rows, key=lambda item: ("codex", "claude").index(item[0]))
            ],
        }
        forged_hash = utc_text_hash(record)
        update = _execute_sql(
            self.db_path,
            "UPDATE skill_contexts SET description = 'forged routing metadata', content_hash = '"
            + forged_hash
            + "'",
        )
        self.assertEqual(update.returncode, 0, update.stderr)
        rc, query = self.kz(
            "SK7", "--query", "git commit", "--host", "codex", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, query)
        self.assertFalse(query["available"])
        self.assertEqual(query["reason"], "DENIED_SKILL_CONTEXT_INTEGRITY")
        plan = self.plan()
        rc, repaired = self.kz(
            "SK6",
            "--action",
            "apply",
            "--confirm-plan",
            plan["plan_sha256"],
            "--store-root",
            str(self.store),
        )
        self.assertEqual(rc, 0, repaired)
        self.assertFalse(repaired["no_op"])
        self.assertNotEqual(repaired["sync_id"], first["sync_id"])

    def test_missing_additive_column_returns_structured_k1_guidance(self) -> None:
        dropped = _execute_sql(self.db_path, "ALTER TABLE skill_context_surfaces DROP COLUMN policy_state")
        self.assertEqual(dropped.returncode, 0, dropped.stderr)
        rc, payload = self.kz(
            "SK7", "--query", "git commit", "--host", "codex", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, payload)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "DENIED_SKILL_CONTEXT_SCHEMA_REQUIRED")
        self.assertIn("run K1", payload["required_action"])
        rc, initialized = self.kz("K1")
        self.assertEqual(rc, 0, initialized)

    def test_test_snapshot_never_replaces_or_deletes_live_current_snapshot(self) -> None:
        self.skill("git")
        live = self.apply()
        tested = self.apply(test=True)
        self.assertNotEqual(tested["sync_id"], live["sync_id"])
        self.assertEqual(
            _read_sql(self.db_path, "SELECT id FROM skill_context_syncs WHERE is_current = 1"),
            [[live["sync_id"]]],
        )
        rc, payload = self.kz("K7")
        self.assertEqual(rc, 0, payload)
        self.assertEqual(
            _read_sql(self.db_path, "SELECT id FROM skill_context_syncs WHERE is_current = 1"),
            [[live["sync_id"]]],
        )

    def test_host_order_is_canonical_and_part_of_the_plan_hash(self) -> None:
        self.skill("git")
        reversed_hosts = self.plan(hosts=("claude", "codex"))
        normal_hosts = self.plan(hosts=("codex", "claude"))
        codex_only = self.plan(hosts=("codex",))
        self.assertEqual(reversed_hosts["hosts"], ["codex", "claude"])
        self.assertEqual(reversed_hosts["plan_sha256"], normal_hosts["plan_sha256"])
        self.assertNotEqual(reversed_hosts["plan_sha256"], codex_only["plan_sha256"])

    def test_query_is_unavailable_not_denied_before_store_bootstrap(self) -> None:
        missing = self.root / "missing-store"
        rc, payload = self.kz(
            "SK7", "--query", "git commit", "--host", "codex", "--store-root", str(missing)
        )
        self.assertEqual(rc, 0, payload)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "DENIED_SKILL_STORE_NOT_FOUND")

    def test_existing_v1_additive_upgrade_requires_reviewed_manifest_restamp(self) -> None:
        self.skill("git")
        for table in ("skill_context_events", "skill_context_surfaces", "skill_contexts", "skill_context_syncs"):
            dropped = _execute_sql(self.db_path, f"DROP TABLE {table}")
            self.assertEqual(dropped.returncode, 0, dropped.stderr)
        drifted = _execute_sql(
            self.db_path,
            "UPDATE schema_version SET manifest_hash = 'pre-skill-context-v1' WHERE id = 'current'",
        )
        self.assertEqual(drifted.returncode, 0, drifted.stderr)
        rc, initialized = self.kz("K1")
        self.assertEqual(rc, 0, initialized)
        self.assertFalse(initialized["schema"]["manifest_match"])
        plan = self.plan()
        rc, denied = self.kz(
            "SK6",
            "--action",
            "apply",
            "--confirm-plan",
            plan["plan_sha256"],
            "--store-root",
            str(self.store),
        )
        self.assertNotEqual(rc, 0, denied)
        self.assertEqual(denied["code"], "DENIED_SCHEMA_DRIFT")
        rc, restamped = self.kz("K1", "--restamp-manifest")
        self.assertEqual(rc, 0, restamped)
        self.assertTrue(restamped["restamped"])
        rc, applied = self.kz(
            "SK6",
            "--action",
            "apply",
            "--confirm-plan",
            plan["plan_sha256"],
            "--store-root",
            str(self.store),
        )
        self.assertEqual(rc, 0, applied)

    def test_name_matching_uses_tokens_not_substrings(self) -> None:
        self.skill("git")
        self.apply()
        rc, payload = self.kz(
            "SK7", "--query", "digital painting", "--host", "codex", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["matches"], [])

    def test_description_stopwords_do_not_route_unrelated_skills(self) -> None:
        self.skill("git")
        unrelated = (
            ("cli-design", "Use when working with a terminal interface or other command-line work."),
            ("davinci-resolve", "Use when working with a video timeline or other editing work."),
            ("discord-developers", "Use when working with a chat integration or other bot work."),
        )
        for name, description in unrelated:
            self.skill(name, description=description, covers=(name,))
        for name in ("git", *(item[0] for item in unrelated)):
            self.surface(name, "codex")
        self.apply()

        rc, payload = self.kz(
            "SK7", "--query", "prepare a git commit", "--host", "codex", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, payload)
        self.assertEqual([match["name"] for match in payload["matches"]], ["git"])
        self.assertEqual(payload["excluded"], [])

        rc, boilerplate = self.kz(
            "SK7", "--query", "use git", "--host", "codex", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, boilerplate)
        self.assertEqual([match["name"] for match in boilerplate["matches"]], ["git"])
        self.assertEqual(boilerplate["excluded"], [])

        for query in ("working with git", "work with git"):
            with self.subTest(query=query):
                rc, live_shape = self.kz(
                    "SK7", "--query", query, "--host", "codex", "--store-root", str(self.store)
                )
                self.assertEqual(rc, 0, live_shape)
                self.assertEqual([match["name"] for match in live_shape["matches"]], ["git"])
                self.assertEqual(live_shape["excluded"], [])

        rc, description_match = self.kz(
            "SK7", "--query", "terminal interface", "--host", "codex", "--store-root", str(self.store)
        )
        self.assertEqual(rc, 0, description_match)
        self.assertEqual([match["name"] for match in description_match["matches"]], ["cli-design"])

    def test_schema_rejects_values_outside_finite_context_vocabularies(self) -> None:
        self.skill("git")
        self.apply()
        statements = (
            "UPDATE skill_context_syncs SET status = 'partial' WHERE is_current = 1",
            "UPDATE skill_context_syncs SET is_current = 2 WHERE is_current = 1",
            "UPDATE skill_context_surfaces SET host = 'agents'",
            "UPDATE skill_context_surfaces SET path_kind = 'copy'",
            "UPDATE skill_contexts SET publication_status = 'private'",
            "UPDATE skill_context_surfaces SET policy_state = 'default'",
            "UPDATE skill_context_events SET event_type = 'queried'",
        )
        for statement in statements:
            proc = _execute_sql(self.db_path, statement)
            self.assertNotEqual(proc.returncode, 0, statement)
        self.assertEqual(_read_sql(self.db_path, "SELECT status, is_current FROM skill_context_syncs"), [["complete", 1]])
        self.assertEqual(
            _read_sql(self.db_path, "SELECT DISTINCT host FROM skill_context_surfaces ORDER BY host"),
            [["claude"], ["codex"]],
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
