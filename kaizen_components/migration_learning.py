"""Safely migrate allowlisted legacy learning surfaces into eval stubs with timestamped backups."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .denials import KaizenDenied
from .hashing import file_sha256
from .markdown_exports import is_stub, stub_for
from .paths import REPO_ROOT, operation_task_dir, repo_relative


SURFACE_NAMES = {"GOTCHA.md", "LEARNING.md", "LEARNED.md"}
PRUNE_DIRS = {
    ".git",
    ".agents",
    ".claude",
    "node_modules",
    ".venv",
    "__pycache__",
    "_src",
    "_corpus",
    "_tmp",
    "AI",
    "evals",
}


def _target_path(directory: Path, name: str) -> Path:
    return directory / "evals" / name


def _old_sibling_path(directory: Path, name: str) -> Path:
    return directory / name


def _allowlist() -> list[Path]:
    roots = [REPO_ROOT.resolve()]
    skills_root = os.environ.get("KAIZEN_SKILLS_ROOT")
    if skills_root:
        roots.append(Path(skills_root).resolve())
    return roots


def _allowed_root(path: Path) -> Path:
    """Traversal guard: resolves (follows symlinks) and requires membership/ancestry in ALLOWLIST or raises DENIED_ROOT_NOT_ALLOWLISTED."""
    resolved = path.resolve()
    allowlist = _allowlist()
    for root in allowlist:
        if resolved == root or root in resolved.parents:
            return root
    raise KaizenDenied(
        "DENIED_ROOT_NOT_ALLOWLISTED",
        {
            "root": str(resolved),
            "allowed_roots": [str(p) for p in allowlist],
            "required_action": "resubmit with an explicit approved root",
        },
        exit_code=2,
    )


def _roots(args: Any) -> list[Path]:
    raw = getattr(args, "root", None) or []
    if not raw:
        raw = [str(REPO_ROOT)]
    roots = []
    for item in raw:
        path = Path(item)
        _allowed_root(path)
        roots.append(path.resolve())
    return roots


def scan_roots(args: Any) -> dict[str, Any]:
    """Read-only inventory of target vs old-sibling existence/stub/sha256 across roots."""
    records = []
    for root in _roots(args):
        if not root.exists():
            records.append({"root": str(root), "exists": False, "files": []})
            continue
        files = []
        for directory in target_dirs(root):
            for name in sorted(SURFACE_NAMES):
                target = _target_path(directory, name).resolve()
                old = _old_sibling_path(directory, name).resolve()
                files.append(
                    {
                        "root": str(directory),
                        "target_path": str(target),
                        "target_exists": target.exists(),
                        "target_stub": is_stub(target) if target.exists() else False,
                        "target_sha256": file_sha256(target) if target.exists() else None,
                        "old_sibling_path": str(old),
                        "old_sibling_exists": old.exists(),
                        "old_sibling_stub": is_stub(old) if old.exists() else False,
                        "old_sibling_sha256": file_sha256(old) if old.exists() else None,
                    }
                )
        records.append({"root": str(root), "exists": True, "files": files, "count": len(files)})
    return {"status": "OK", "roots": records}


def target_dirs(root: Path) -> list[Path]:
    """Root + every descendant dir (minus PRUNE_DIRS) holding a surface file or SKILL.md; walk errors are silently ignored."""
    dirs = {root.resolve()}
    def raise_walk_error(error: OSError) -> None:
        raise error

    for dirpath, dirnames, filenames in os.walk(root, onerror=raise_walk_error):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        names = set(filenames)
        if names & SURFACE_NAMES or "SKILL.md" in names:
            dirs.add(Path(dirpath).resolve())
    return sorted(dirs, key=lambda p: str(p))


def dry_run(args: Any) -> dict[str, Any]:
    """Pure/read-only plan; enumerate the 5 action kinds (keep-stub, backup-and-replace-stub, backup-and-remove-old-sibling, relocate-sibling-to-evals, create-stub) and their trigger conditions."""
    actions = []
    for root in _roots(args):
        if not root.exists():
            continue
        for directory in target_dirs(root):
            for name in sorted(SURFACE_NAMES):
                target = _target_path(directory, name)
                old = _old_sibling_path(directory, name)
                if target.exists():
                    action = "keep-stub" if is_stub(target) else "backup-and-replace-stub"
                    actions.append(
                        {
                            "path": str(target),
                            "target_path": str(target),
                            "old_sibling_path": str(old),
                            "action": action,
                            "sha256": file_sha256(target),
                        }
                    )
                    if old.exists():
                        actions.append(
                            {
                                "path": str(old),
                                "target_path": str(target),
                                "old_sibling_path": str(old),
                                "action": "backup-and-remove-old-sibling",
                                "sha256": file_sha256(old),
                            }
                        )
                elif old.exists():
                    actions.append(
                        {
                            "path": str(target),
                            "target_path": str(target),
                            "old_sibling_path": str(old),
                            "action": "relocate-sibling-to-evals",
                            "sha256": file_sha256(old),
                        }
                    )
                else:
                    actions.append(
                        {
                            "path": str(target),
                            "target_path": str(target),
                            "old_sibling_path": str(old),
                            "action": "create-stub",
                            "sha256": None,
                        }
                    )
    return {"status": "OK", "dry_run": True, "actions": actions, "count": len(actions)}


def apply(args: Any) -> dict[str, Any]:
    """DESTRUCTIVE: backs up then overwrites/unlinks/moves surface files per the `dry_run` plan; writes a timestamped backups dir + `manifest.json`; returns changed paths. Highest-value docstring."""
    plan = dry_run(args)
    task_dir = operation_task_dir("kaizen-v4-migration")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = task_dir / "backups" / stamp
    files_root = backup_root / "files"
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "files": []}
    changed = []
    def backup(path: Path) -> dict[str, str]:
        files_root.mkdir(parents=True, exist_ok=True)
        backup_path = files_root / (str(len(manifest["files"]) + 1).zfill(4) + "-" + path.name)
        shutil.copy2(path, backup_path)
        record = {
            "source_path": str(path),
            "backup_path": repo_relative(backup_path),
            "sha256": file_sha256(backup_path),
        }
        manifest["files"].append(record)
        return record

    for item in plan["actions"]:
        target = Path(item["target_path"]).resolve()
        old = Path(item["old_sibling_path"]).resolve()
        _allowed_root(target)
        _allowed_root(old)
        if item["action"] == "keep-stub":
            continue
        if item["action"] == "backup-and-replace-stub":
            if target.exists() and is_stub(target):
                continue
            if target.exists():
                backup(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(stub_for(target.name), encoding="utf-8")
            changed.append(str(target))
            continue
        if item["action"] == "backup-and-remove-old-sibling":
            if old.exists():
                backup(old)
                old.unlink()
                changed.append(str(old))
            continue
        if item["action"] == "relocate-sibling-to-evals":
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise KaizenDenied(
                    "DENIED_MIGRATION_TARGET_EXISTS",
                    {"path": str(target), "required_action": "rerun the dry-run and reconcile the new target"},
                    exit_code=2,
                )
            if old.exists():
                backup(old)
                if is_stub(old):
                    shutil.move(str(old), str(target))
                else:
                    target.write_text(stub_for(target.name), encoding="utf-8")
                    old.unlink()
            else:
                target.write_text(stub_for(target.name), encoding="utf-8")
            changed.append(str(target))
            continue
        if item["action"] == "create-stub":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(stub_for(target.name), encoding="utf-8")
            changed.append(str(target))
    manifest_path = None
    if manifest["files"]:
        manifest_path = backup_root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {
        "status": "OK",
        "changed": changed,
        "count": len(changed),
        "manifest": repo_relative(manifest_path) if manifest_path else None,
    }


def verify(args: Any) -> dict[str, Any]:
    """Post-migration invariant: every target exists as a stub and no old sibling remains; else FAILED + `unmigrated` list. Note: coverage is bounded by `target_dirs`, whose walk silently skips unreadable subtrees (see F2)."""
    bad = []
    for root in _roots(args):
        if not root.exists():
            bad.append(str(root))
            continue
        for directory in target_dirs(root):
            for name in sorted(SURFACE_NAMES):
                target = _target_path(directory, name)
                old = _old_sibling_path(directory, name)
                if not target.exists() or not is_stub(target):
                    bad.append(str(target))
                if old.exists():
                    bad.append(str(old))
    return {"status": "OK" if not bad else "FAILED", "unmigrated": bad, "count": len(bad)}


def migration_report(args: Any) -> dict[str, Any]:
    """Writes a timestamped human-facing MD summary of the `dry_run` plan under the task reports dir."""
    result = dry_run(args)
    task_dir = operation_task_dir("kaizen-v4-migration")
    report_dir = task_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"migration-report-{stamp}.md"
    lines = ["# Kaizen Migration Report", "", f"Actions: {result['count']}", ""]
    for item in result["actions"]:
        lines.append(f"- {item['action']}: `{item['path']}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"status": "OK", "path": repo_relative(path), "count": result["count"]}
