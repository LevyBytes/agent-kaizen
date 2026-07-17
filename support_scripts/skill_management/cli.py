"""Standalone argparse surface for the skill-management operator library."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (
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
    link_status,
    policy_status,
    restore_policy,
    validate_skill_package,
)


DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _hosts(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(args.host or ("codex", "claude"))


def _desired(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SkillManagementError(f"invalid --policy value `{value}`; expected NAME=STATE")
        name, state = value.split("=", 1)
        if not name or not state:
            raise SkillManagementError(f"invalid --policy value `{value}`; expected NAME=STATE")
        result[name] = state
    return result


def _emit(payload: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return
    if isinstance(payload, dict):
        status = payload.get("status", "OK" if not payload.get("errors") else "FAIL")
        print(f"{status} {payload.get('kind', payload.get('action', 'skill-management'))}")
        if payload.get("plan_sha256"):
            print(f"plan_sha256: {payload['plan_sha256']}")
        for error in payload.get("errors", []):
            print(f"- {error}", file=sys.stderr)
        if "skills" in payload and isinstance(payload["skills"], list):
            print(f"skills: {len(payload['skills'])}")
        if "operations" in payload:
            print(f"operations: {len(payload['operations'])}")
        return
    print(payload)


def _inventory(args: argparse.Namespace) -> tuple[int, object]:
    result = discover_skills(args.project_root, args.store_root, _hosts(args))
    return (0 if result["complete"] else 1), result


def _validate(args: argparse.Namespace) -> tuple[int, object]:
    records = [validate_skill_package(path) for path in args.skill_dir]
    return (0 if all(row["valid"] for row in records) else 1), {
        "schema_version": 1,
        "action": "validate",
        "valid": all(row["valid"] for row in records),
        "skills": records,
    }


def _links(args: argparse.Namespace) -> tuple[int, object]:
    hosts = _hosts(args)
    names = args.skill or None
    if args.action == "status":
        result = link_status(args.project_root, args.store_root, hosts, names)
    elif args.action == "plan":
        result = build_link_plan(args.project_root, args.store_root, hosts, names)
    else:
        result = apply_link_plan(
            args.project_root,
            args.store_root,
            hosts,
            names,
            confirm_plan=args.confirm_plan,
        )
    return (0 if not result.get("errors") else 1), result


def _index(args: argparse.Namespace) -> tuple[int, object]:
    hosts = _hosts(args)
    if args.action == "status":
        result = index_status(args.project_root, hosts)
    elif args.action == "plan":
        result = build_index_plan(args.project_root, hosts)
    else:
        result = apply_index_plan(args.project_root, hosts, args.confirm_plan)
    return (0 if not result.get("errors") else 1), result


def _store_index(args: argparse.Namespace) -> tuple[int, object]:
    if args.action == "plan":
        result = build_store_index_plan(args.skills_root, args.mirror_root, args.seed_covers, args.flat_cap)
    else:
        result = apply_store_index_plan(
            args.skills_root,
            args.mirror_root,
            args.seed_covers,
            args.flat_cap,
            args.confirm_plan,
        )
    return (0 if not result.get("errors") else 1), result


def _policy(args: argparse.Namespace) -> tuple[int, object]:
    if args.host == "codex":
        if args.action == "status":
            return 0, {
                "schema_version": 1,
                "host": "codex",
                "status": "OK",
                "supported": False,
                "advisory": "Codex skill policy is audit-only; no project-local per-skill policy writer is supported.",
            }
        raise SkillManagementError("Codex skill policy is audit-only; plan/apply/restore are unsupported")
    desired = _desired(args.policy)
    if args.action == "status":
        result = policy_status(args.project_root)
    elif args.action == "plan":
        result = build_policy_plan(args.project_root, desired)
    elif args.action == "apply":
        result = apply_policy_plan(args.project_root, desired, args.confirm_plan)
    elif args.action == "restore-plan":
        if not args.record:
            raise SkillManagementError("policy restore-plan requires --record")
        result = build_policy_restore_plan(args.project_root, args.record)
    else:
        if not args.record:
            raise SkillManagementError("policy restore requires --record")
        result = restore_policy(args.project_root, args.record, args.confirm_plan)
    return (0 if not result.get("errors") else 1), result


def build_parser() -> argparse.ArgumentParser:
    """Build the standalone skill-manager parser."""
    parser = argparse.ArgumentParser(
        prog="skill_manager.py",
        description="Inspect and explicitly reconcile project skill packages, links, indexes, and Claude policy.",
    )
    parser.add_argument("--json", action="store_true", help="emit stable JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    common_project = argparse.ArgumentParser(add_help=False)
    common_project.add_argument("--project-root", default=str(DEFAULT_PROJECT_ROOT))

    inventory = sub.add_parser("inventory", parents=[common_project], help="read-only store/project inventory")
    inventory.add_argument("--store-root", required=True)
    inventory.add_argument("--host", action="append", choices=("codex", "claude"))

    validate = sub.add_parser("validate", help="read-only structured package validation")
    validate.add_argument("skill_dir", nargs="+")

    links = sub.add_parser("links", parents=[common_project], help="project link status/plan/apply")
    links.add_argument("action", nargs="?", choices=("status", "plan", "apply"), default="status")
    links.add_argument("--store-root", required=True)
    links.add_argument("--host", action="append", choices=("codex", "claude"))
    links.add_argument("--skill", action="append", default=[], help="explicit skill name; repeatable")
    links.add_argument("--confirm-plan", help="exact SHA-256 from the current plan")

    index = sub.add_parser("index", parents=[common_project], help="host INDEX status/plan/apply")
    index.add_argument("action", nargs="?", choices=("status", "plan", "apply"), default="status")
    index.add_argument("--host", action="append", choices=("codex", "claude"))
    index.add_argument("--confirm-plan", help="exact SHA-256 from the current plan")

    store_index = sub.add_parser("store-index", help="rich skills-store index plan/apply")
    store_index.add_argument("action", nargs="?", choices=("plan", "apply"), default="plan")
    store_index.add_argument("--skills-root", required=True)
    store_index.add_argument("--mirror-root")
    store_index.add_argument("--seed-covers", action="store_true")
    store_index.add_argument("--flat-cap", type=int, default=12)
    store_index.add_argument("--confirm-plan", help="exact SHA-256 from the current plan")

    policy = sub.add_parser("policy", parents=[common_project], help="project-only Claude policy")
    policy.add_argument(
        "action",
        nargs="?",
        choices=("status", "plan", "apply", "restore-plan", "restore"),
        default="status",
    )
    policy.add_argument("--host", choices=("codex", "claude"), default="claude")
    policy.add_argument("--policy", action="append", default=[], metavar="NAME=STATE")
    policy.add_argument("--record", help="explicit rollback record beneath the project root")
    policy.add_argument("--confirm-plan", help="exact SHA-256 from the current plan")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "inventory": _inventory,
        "validate": _validate,
        "links": _links,
        "index": _index,
        "store-index": _store_index,
        "policy": _policy,
    }
    try:
        code, payload = handlers[args.command](args)
    except SkillManagementError as exc:
        payload = {"status": "DENIED", "code": "SKILL_MANAGEMENT_DENIED", "message": str(exc)}
        _emit(payload, args.json)
        return 1
    except OSError as exc:
        payload = {"status": "ERROR", "code": "SKILL_MANAGEMENT_IO", "message": f"{type(exc).__name__}: {exc}"}
        _emit(payload, args.json)
        return 1
    _emit(payload, args.json)
    return code
