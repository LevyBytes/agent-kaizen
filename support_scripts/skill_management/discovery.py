"""Explicit project/store skill discovery and host-surface classification."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from .core import (
    SCHEMA_VERSION,
    is_directory_link,
    link_target,
    path_text,
    require_hosts,
    resolved,
    same_path,
    surface_root,
)
from .validation import validate_skill_package


_REMOTE_SECTION_RE = re.compile(r'^\s*\[\s*remote\s+"(?:[^"\\]|\\.)+"\s*\]\s*$', re.IGNORECASE)
_SECTION_RE = re.compile(r"^\s*\[")
_REMOTE_URL_RE = re.compile(r"^\s*url\s*=\s*(.*?)\s*$", re.IGNORECASE)
_GITHUB_SCP_RE = re.compile(
    r"^git@github\.com:(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_GITHUB_PATH_RE = re.compile(r"^/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?$")
_MAX_GIT_CONFIG_BYTES = 1_000_000


def _is_github_remote(value: str) -> bool:
    """Return whether a local remote URL is a credential-free standard GitHub repository URL."""
    remote = value.strip()
    if len(remote) >= 2 and remote[0] == remote[-1] == '"':
        remote = remote[1:-1]
    if _GITHUB_SCP_RE.fullmatch(remote):
        return True
    try:
        parsed = urlsplit(remote)
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"https", "ssh"} or hostname is None:
        return False
    if hostname.casefold() != "github.com" or password or parsed.query or parsed.fragment:
        return False
    if parsed.scheme == "https" and (username is not None or port not in {None, 443}):
        return False
    if parsed.scheme == "ssh" and (username != "git" or port not in {None, 22}):
        return False
    return bool(_GITHUB_PATH_RE.fullmatch(parsed.path))


def _publication_state(skill_dir: Path) -> tuple[str, str]:
    """Classify a package from its contained local Git metadata without exposing remote values."""
    git_dir = skill_dir / ".git"
    config = git_dir / "config"
    try:
        if not git_dir.exists():
            return "staged", "no_git_metadata"
        if not git_dir.is_dir() or is_directory_link(git_dir) or config.is_symlink() or not config.is_file():
            return "staged", "git_metadata_unreadable"
        resolved_skill = skill_dir.resolve(strict=True)
        resolved_git = git_dir.resolve(strict=True)
        resolved_config = config.resolve(strict=True)
        if resolved_git.parent != resolved_skill or resolved_config.parent != resolved_git:
            return "staged", "git_metadata_unreadable"
        if config.stat().st_size > _MAX_GIT_CONFIG_BYTES:
            return "staged", "git_metadata_unreadable"
        text = config.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return "staged", "git_metadata_unreadable"
    in_remote = False
    saw_remote = False
    for line in text.splitlines():
        if _REMOTE_SECTION_RE.fullmatch(line):
            in_remote = True
            continue
        if _SECTION_RE.match(line):
            in_remote = False
            continue
        if not in_remote:
            continue
        match = _REMOTE_URL_RE.fullmatch(line)
        if match is None:
            continue
        saw_remote = True
        if _is_github_remote(match.group(1)):
            return "published", "github_remote"
    return "staged", "no_github_remote" if saw_remote else "no_remote"


def iter_skill_dirs(root: Path):
    """Yield immediate, non-hidden child directories containing SKILL.md."""
    if not root.is_dir():
        return
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return
    for child in children:
        if child.name.startswith(".") or child.name == "INDEX.md":
            continue
        if child.is_dir() and (child / "SKILL.md").is_file():
            yield child


def _scan_store_dirs(root: Path) -> tuple[list[Path], list[str], bool]:
    """Enumerate candidate store packages and distinguish scan failure from invalid packages."""
    if not root.is_dir():
        return [], [f"skill store does not exist or is not a directory: {root}"], False
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        return [], [f"cannot enumerate skill store {root}: {type(exc).__name__}: {exc}"], False
    candidates: list[Path] = []
    errors: list[str] = []
    root_resolved = root.resolve(strict=True)
    for child in children:
        if child.name.startswith(".") or child.name in {"INDEX.md", "__pycache__", "node_modules"}:
            continue
        try:
            mode = child.stat(follow_symlinks=False).st_mode
        except OSError as exc:
            errors.append(f"cannot inspect skill-store entry {child}: {type(exc).__name__}: {exc}")
            continue
        linked = is_directory_link(child)
        if not linked and stat.S_ISREG(mode):
            continue
        if not linked and not stat.S_ISDIR(mode):
            errors.append(f"unsupported skill-store entry type: {child}")
            continue
        try:
            target = child.resolve(strict=True)
        except OSError as exc:
            errors.append(f"cannot resolve skill package {child}: {type(exc).__name__}: {exc}")
            continue
        if not target.is_dir():
            errors.append(f"linked skill package target is not a directory: {child} -> {target}")
            continue
        if target.parent != root_resolved:
            errors.append(f"skill package escapes the explicit store root: {child} -> {target}")
            continue
        candidates.append(child)
    return candidates, errors, not errors


def classify_surface(path: Path, expected: Path | None, host: str) -> dict[str, object]:
    """Classify one project host path without changing it."""
    exists_or_link = os.path.lexists(path)
    target = link_target(path)
    if not exists_or_link:
        status = "missing"
    elif is_directory_link(path):
        if target is None or not target.exists():
            status = "dangling_link"
        elif expected is not None and same_path(target, expected):
            status = "correct"
        else:
            status = "wrong_target"
    else:
        status = "real_directory"
    return {
        "host": host,
        "path": path_text(path),
        "status": status,
        "target": path_text(target) if target is not None else None,
        "expected_target": path_text(expected) if expected is not None else None,
    }


def discover_skills(
    project_root: str | os.PathLike[str],
    store_root: str | os.PathLike[str],
    hosts: tuple[str, ...] | list[str] = ("codex", "claude"),
) -> dict[str, object]:
    """Inventory explicit store packages and their project host surfaces.

    No user-home directory is consulted. ``store_root`` must name the directory
    whose immediate children are skill packages.
    """
    project = resolved(project_root)
    store = resolved(store_root)
    selected = require_hosts(hosts)
    skills: list[dict[str, object]] = []
    known: set[str] = set()
    skill_dirs, scan_errors, complete = _scan_store_dirs(store)
    for skill_dir in skill_dirs:
        known.add(skill_dir.name)
        validation = validate_skill_package(skill_dir)
        publication_status, publication_reason = _publication_state(skill_dir)
        host_records = {
            host: classify_surface(surface_root(project, host) / skill_dir.name, skill_dir, host)
            for host in selected
        }
        skills.append(
            {
                "name": skill_dir.name,
                "display_name": str(validation["display_name"]),
                "description": str(validation["description"]),
                "covers": list(validation["covers"]),
                "source_path": path_text(skill_dir),
                "source_relpath": skill_dir.relative_to(store).as_posix(),
                "skill_md_path": str(validation["skill_md_path"]),
                "skill_md_relative_path": (skill_dir.relative_to(store) / "SKILL.md").as_posix(),
                "skill_md_sha256": str(validation["skill_md_sha256"]),
                "package_sha256": str(validation["package_sha256"]),
                "publication_status": publication_status,
                "publication_reason": publication_reason,
                "valid": bool(validation["valid"]),
                "errors": list(validation["errors"]),
                "warnings": list(validation["warnings"]),
                "hosts": host_records,
            }
        )
    orphan_surfaces: list[dict[str, object]] = []
    for host in selected:
        root = surface_root(project, host)
        if not os.path.lexists(root):
            continue
        try:
            root_mode = root.stat().st_mode
        except OSError as exc:
            complete = False
            scan_errors.append(f"cannot inspect {host} skill surface {root}: {type(exc).__name__}: {exc}")
            continue
        if not stat.S_ISDIR(root_mode):
            complete = False
            scan_errors.append(f"{host} skill surface is not a readable directory: {root}")
            continue
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            complete = False
            scan_errors.append(f"cannot enumerate {host} skill surface {root}: {type(exc).__name__}: {exc}")
            continue
        for child in children:
            if child.name == "INDEX.md" or child.name in known:
                continue
            if not os.path.lexists(child):
                continue
            if child.name.startswith(".") and not (child / "SKILL.md").is_file():
                continue
            orphan_surfaces.append(classify_surface(child, None, host))
            orphan_surfaces[-1]["name"] = child.name
    return {
        "schema_version": SCHEMA_VERSION,
        "project_root": path_text(project),
        "store_root": path_text(store),
        "hosts": list(selected),
        "complete": complete,
        "scan_errors": sorted(set(scan_errors)),
        "skills": skills,
        "orphan_surfaces": orphan_surfaces,
    }
