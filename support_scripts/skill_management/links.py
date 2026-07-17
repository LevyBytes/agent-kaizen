"""Project skill-link status, deterministic planning, and confirmed apply."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .core import (
    SCHEMA_VERSION,
    SkillManagementError,
    assert_under,
    finalize_plan,
    is_directory_link,
    link_target,
    path_text,
    require_confirmation,
    require_hosts,
    require_plan_clean,
    resolved,
    same_path,
    surface_root,
)
from .discovery import classify_surface, discover_skills


def link_status(
    project_root: str | os.PathLike[str],
    store_root: str | os.PathLike[str],
    hosts: tuple[str, ...] | list[str] = ("codex", "claude"),
    skill_names: tuple[str, ...] | list[str] | None = None,
) -> dict[str, object]:
    """Return read-only link state for store skills and project surfaces."""
    inventory = discover_skills(project_root, store_root, hosts)
    requested = set(str(name) for name in skill_names) if skill_names is not None else None
    if requested is not None:
        inventory["skills"] = [
            row for row in inventory["skills"] if str(row["name"]) in requested
        ]
    counts: dict[str, int] = {}
    for skill in inventory["skills"]:
        for surface in skill["hosts"].values():
            status = str(surface["status"])
            counts[status] = counts.get(status, 0) + 1
    inventory["counts"] = dict(sorted(counts.items()))
    return inventory


def build_link_plan(
    project_root: str | os.PathLike[str],
    store_root: str | os.PathLike[str],
    hosts: tuple[str, ...] | list[str] = ("codex", "claude"),
    skill_names: tuple[str, ...] | list[str] | None = None,
    prune: bool = False,
) -> dict[str, object]:
    """Build a read-only plan for project skill links.

    Missing links become ``create_link`` operations. Existing real directories,
    dangling links, and links to a different target are reported as hard errors;
    they are never replaced. Removal is planned only when ``prune=True``.
    """
    project = resolved(project_root)
    store = resolved(store_root)
    selected_hosts = require_hosts(hosts)
    requested = None if skill_names is None else tuple(sorted(set(str(name) for name in skill_names if str(name))))
    inventory = discover_skills(project, store, selected_hosts)
    by_rel = {str(row["name"]): row for row in inventory["skills"]}
    errors: list[str] = []
    operations: list[dict[str, object]] = []
    if not inventory["complete"]:
        errors.extend(inventory["scan_errors"])
    if requested is None or not requested:
        errors.append("link planning requires at least one explicit skill_names entry")
    desired_names = [] if requested is None else list(requested)
    unknown = sorted(set(desired_names) - set(by_rel))
    if unknown:
        errors.append("requested skill(s) not found in store: " + ", ".join(unknown))
    for name in desired_names:
        row = by_rel.get(name)
        if row is None:
            continue
        if not row["valid"]:
            errors.append(f"skill `{name}` failed package validation: " + "; ".join(row["errors"]))
            continue
        for host in selected_hosts:
            surface = row["hosts"][host]
            status = surface["status"]
            if status == "missing":
                operations.append(
                    {
                        "op": "create_link",
                        "host": host,
                        "name": name,
                        "path": surface["path"],
                        "target": row["source_path"],
                    }
                )
            elif status != "correct":
                errors.append(
                    f"{host}/{name} is {status}; refusing to replace it ({surface['path']})"
                )
    if prune:
        desired = set(desired_names)
        for host in selected_hosts:
            root = surface_root(project, host)
            if not root.is_dir():
                continue
            try:
                children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
            except OSError as exc:
                errors.append(f"cannot inspect {root}: {type(exc).__name__}: {exc}")
                continue
            for child in children:
                if child.name.startswith(".") or child.name == "INDEX.md" or child.name in desired:
                    continue
                if not os.path.lexists(child):
                    continue
                if not is_directory_link(child):
                    errors.append(f"{host}/{child.name} is a real directory; refusing to prune it")
                    continue
                target = link_target(child)
                if target is None or not target.exists():
                    errors.append(f"{host}/{child.name} is a dangling link; refusing to prune it")
                    continue
                operations.append(
                    {
                        "op": "remove_link",
                        "host": host,
                        "name": child.name,
                        "path": path_text(child),
                        "target": path_text(target),
                    }
                )
    operations.sort(key=lambda row: (str(row["host"]), str(row["name"]), str(row["op"])))
    return finalize_plan(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "skill-links",
            "project_root": path_text(project),
            "store_root": path_text(store),
            "hosts": list(selected_hosts),
            "skill_names": list(requested) if requested is not None else None,
            "prune": bool(prune),
            "operations": operations,
            "errors": sorted(set(errors)),
        }
    )


def _create_directory_link(path: Path, target: Path) -> None:
    """Create one directory symlink (POSIX) or junction (Windows)."""
    if os.path.lexists(path):
        raise SkillManagementError(f"link destination appeared after planning: {path}")
    if not target.is_dir():
        raise SkillManagementError(f"link target is not a directory: {target}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        unsafe = "&|<>^%!\r\n"
        if any(character in str(path) or character in str(target) for character in unsafe):
            raise SkillManagementError("Windows junction paths contain cmd.exe metacharacters")
        proc = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise SkillManagementError(f"could not create junction {path}: {detail or proc.returncode}")
    else:
        os.symlink(target, path, target_is_directory=True)


def _verify_directory_link(path: Path, target: Path) -> None:
    """Verify that one created directory link resolves to its planned target."""
    actual = link_target(path)
    if actual is None or not same_path(actual, target):
        raise SkillManagementError(f"created link did not resolve to expected target: {path}")


def _remove_directory_link(path: Path, expected_target: Path) -> None:
    """Remove one verified link/junction without touching its target."""
    actual = link_target(path)
    if actual is None or not same_path(actual, expected_target):
        raise SkillManagementError(f"refusing to remove changed or non-link path: {path}")
    if path.is_symlink():
        path.unlink()
    else:
        os.rmdir(path)


def apply_link_plan(
    project_root: str | os.PathLike[str],
    store_root: str | os.PathLike[str],
    hosts: tuple[str, ...] | list[str] = ("codex", "claude"),
    skill_names: tuple[str, ...] | list[str] | None = None,
    prune: bool = False,
    confirm_plan: str | None = None,
) -> dict[str, object]:
    """Recompute, confirm, and apply a skill-link plan with rollback on error."""
    plan = build_link_plan(project_root, store_root, hosts, skill_names, prune)
    require_confirmation(plan, confirm_plan)
    require_plan_clean(plan)
    project = resolved(project_root)
    created: list[tuple[Path, Path]] = []
    removed: list[tuple[Path, Path]] = []
    try:
        for operation in plan["operations"]:
            if operation["op"] != "create_link":
                continue
            path = assert_under(project, resolved(operation["path"]))
            target = resolved(operation["target"])
            _create_directory_link(path, target)
            created.append((path, target))
            _verify_directory_link(path, target)
        for operation in plan["operations"]:
            if operation["op"] != "remove_link":
                continue
            path = Path(os.path.abspath(str(operation["path"])))
            assert_under(project, path.parent)
            target = resolved(operation["target"])
            _remove_directory_link(path, target)
            removed.append((path, target))
    except Exception as exc:
        rollback_errors: list[str] = []
        for path, target in reversed(removed):
            try:
                _create_directory_link(path, target)
                _verify_directory_link(path, target)
            except Exception as rollback_exc:
                rollback_errors.append(f"restore {path}: {rollback_exc}")
        for path, target in reversed(created):
            try:
                if os.path.lexists(path):
                    _remove_directory_link(path, target)
            except Exception as rollback_exc:
                rollback_errors.append(f"remove {path}: {rollback_exc}")
        suffix = "" if not rollback_errors else "; rollback failures: " + "; ".join(rollback_errors)
        raise SkillManagementError(f"link apply failed: {exc}{suffix}") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "OK",
        "action": "apply",
        "kind": "skill-links",
        "plan_sha256": plan["plan_sha256"],
        "applied": len(plan["operations"]),
        "restart_required": bool(plan["operations"]),
        "operations": plan["operations"],
    }
