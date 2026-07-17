"""Shared path, hashing, and write primitives for skill management."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
HOST_SURFACES = {
    "codex": (".agents", "skills"),
    "claude": (".claude", "skills"),
}


class SkillManagementError(RuntimeError):
    """Raised when a skill-management operation cannot safely proceed."""


def resolved(path: str | os.PathLike[str]) -> Path:
    """Return an absolute normalized path without requiring it to exist."""
    return Path(path).expanduser().resolve(strict=False)


def require_hosts(hosts: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Validate, deduplicate, and return host names in stable order."""
    requested = tuple(dict.fromkeys(str(host) for host in hosts))
    unknown = sorted(set(requested) - set(HOST_SURFACES))
    if unknown:
        raise SkillManagementError(f"unknown skill host(s): {', '.join(unknown)}")
    if not requested:
        raise SkillManagementError("at least one skill host is required")
    return tuple(host for host in HOST_SURFACES if host in requested)


def surface_root(project_root: str | os.PathLike[str], host: str) -> Path:
    """Return a project's explicit skill surface for ``host``."""
    require_hosts((host,))
    return resolved(project_root).joinpath(*HOST_SURFACES[host])


def path_text(path: Path) -> str:
    """Return a stable lexical absolute path without following its final link."""
    return os.path.abspath(str(path))


def same_path(left: Path, right: Path) -> bool:
    """Compare paths case-insensitively where the platform does so."""
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
        os.path.abspath(str(right))
    )


def is_directory_link(path: Path) -> bool:
    """Return whether ``path`` is a symlink or Windows directory junction."""
    if path.is_symlink():
        return True
    checker = getattr(path, "is_junction", None)
    if checker is not None:
        try:
            return bool(checker())
        except OSError:
            return False
    if not path.exists():
        return False
    try:
        return not same_path(path, Path(os.path.realpath(str(path))))
    except OSError:
        return False


def link_target(path: Path) -> Path | None:
    """Return a link/junction's resolved target, including dangling symlinks."""
    if not os.path.lexists(path) or not is_directory_link(path):
        return None
    try:
        if path.is_symlink():
            raw = Path(os.readlink(path))
            return (path.parent / raw).resolve(strict=False) if not raw.is_absolute() else raw.resolve(strict=False)
        return Path(os.path.realpath(str(path))).resolve(strict=False)
    except OSError:
        return None


def sha256_bytes(data: bytes) -> str:
    """Return lowercase SHA-256 hex for ``data``."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Return SHA-256 for ``path`` or an empty string when unreadable."""
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return ""


def canonical_json(value: Any) -> bytes:
    """Serialize ``value`` deterministically for hashing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def plan_sha256(plan: Mapping[str, Any]) -> str:
    """Hash a plan while ignoring its self-referential ``plan_sha256`` field."""
    material = dict(plan)
    material.pop("plan_sha256", None)
    return sha256_bytes(canonical_json(material))


def finalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Return ``plan`` with its deterministic confirmation hash attached."""
    plan = dict(plan)
    plan["plan_sha256"] = plan_sha256(plan)
    return plan


def require_confirmation(plan: Mapping[str, Any], confirmation: str | None) -> None:
    """Require an exact hash confirmation for a freshly recomputed plan."""
    expected = plan_sha256(plan)
    if not confirmation:
        raise SkillManagementError(
            f"apply requires --confirm-plan {expected} from the current plan"
        )
    if confirmation.strip().lower() != expected:
        raise SkillManagementError(
            f"plan confirmation mismatch: current plan is {expected}; inspect it and confirm again"
        )


def require_plan_clean(plan: Mapping[str, Any]) -> None:
    """Reject a plan carrying any preflight error."""
    errors = list(plan.get("errors") or [])
    if errors:
        raise SkillManagementError("plan is not applicable: " + "; ".join(str(e) for e in errors))


def assert_under(root: Path, path: Path) -> Path:
    """Require ``path`` to resolve beneath ``root`` (or equal it)."""
    root_r = root.resolve(strict=False)
    path_r = path.resolve(strict=False)
    if path_r != root_r and root_r not in path_r.parents:
        raise SkillManagementError(f"path escapes managed root {root_r}: {path_r}")
    return path_r


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically replace ``path`` with ``data`` using a sibling temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write UTF-8 text with LF newlines."""
    atomic_write_bytes(path, text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    """Atomically write indented UTF-8 JSON with a trailing newline."""
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
