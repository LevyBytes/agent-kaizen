"""Project-only Claude skill invocation policy status, plan, apply, and restore."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .core import (
    SCHEMA_VERSION,
    SkillManagementError,
    assert_under,
    atomic_write_bytes,
    atomic_write_json,
    finalize_plan,
    path_text,
    plan_sha256,
    require_confirmation,
    require_plan_clean,
    resolved,
    same_path,
    sha256_bytes,
    sha256_file,
    surface_root,
)
from .discovery import iter_skill_dirs


POLICY_STATES = ("on", "name-only", "user-invocable-only", "off")
DESIRED_STATES = POLICY_STATES + ("default",)
ROLLBACK_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "project_root",
        "settings_path",
        "settings_existed",
        "before_sha256",
        "applied_settings_sha256",
        "apply_plan_sha256",
        "desired",
        "operations",
    }
)
ROLLBACK_OPERATION_KEYS = frozenset({"op", "name", "before", "after"})
ROLLBACK_RECORD_NAME = re.compile(
    r"policy-rollback-\d{8}T\d{6}\.\d{6}Z-([0-9a-f]{12})\.json"
)


def _settings_path(project: Path) -> Path:
    return project / ".claude" / "settings.local.json"


def _load_settings(path: Path) -> tuple[dict[str, object], bytes | None, list[str]]:
    if not path.exists():
        return {}, None, []
    if not path.is_file():
        return {}, None, [f"Claude settings path is not a file: {path}"]
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, None, [f"cannot parse Claude settings {path}: {type(exc).__name__}: {exc}"]
    if not isinstance(value, dict):
        return {}, raw, [f"Claude settings root must be a JSON object: {path}"]
    overrides = value.get("skillOverrides", {})
    if not isinstance(overrides, dict):
        return value, raw, ["Claude `skillOverrides` must be a JSON object"]
    invalid = sorted(
        name for name, state in overrides.items() if not isinstance(name, str) or state not in POLICY_STATES
    )
    errors = ["Claude `skillOverrides` contains invalid entries: " + ", ".join(invalid)] if invalid else []
    return value, raw, errors


def _surface_names(project: Path) -> list[str]:
    return sorted(skill.name for skill in iter_skill_dirs(surface_root(project, "claude")))


def _settings_bytes(settings: Mapping[str, object]) -> bytes:
    return (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def policy_status(project_root: str | os.PathLike[str]) -> dict[str, object]:
    """Return project-local Claude policy without consulting user settings."""
    project = resolved(project_root)
    path = _settings_path(project)
    settings, raw, errors = _load_settings(path)
    overrides = settings.get("skillOverrides", {}) if isinstance(settings.get("skillOverrides", {}), dict) else {}
    surfaced = _surface_names(project)
    names = sorted(set(surfaced) | set(str(name) for name in overrides))
    skills = [
        {
            "name": name,
            "surfaced": name in surfaced,
            "current_policy": overrides.get(name, "on"),
            "explicit": name in overrides,
        }
        for name in names
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "host": "claude",
        "codex_advisory": "Codex policy is audit-only; no supported project-local per-skill invocation-policy writer exists.",
        "project_root": path_text(project),
        "settings_path": path_text(path),
        "settings_exists": raw is not None,
        "settings_sha256": sha256_bytes(raw) if raw is not None else "",
        "skills": skills,
        "errors": errors,
    }


def _normalized_desired(desired: Mapping[str, str]) -> dict[str, str]:
    return {str(name): str(state) for name, state in sorted(desired.items())}


def _policy_plan_material(
    project_root: str | os.PathLike[str],
    desired: Mapping[str, str],
) -> tuple[dict[str, object], bytes | None]:
    """Build a public policy plan plus private post-apply bytes."""
    project = resolved(project_root)
    path = _settings_path(project)
    settings, raw, errors = _load_settings(path)
    choices = _normalized_desired(desired)
    if not choices:
        errors.append("policy planning requires at least one explicit skill policy")
    surfaced = set(_surface_names(project))
    for name, state in choices.items():
        if not name or Path(name).name != name:
            errors.append(f"invalid skill name: {name!r}")
        if state not in DESIRED_STATES:
            errors.append(f"invalid policy for `{name}`: {state}")
        if name not in surfaced:
            errors.append(f"Claude skill is not surfaced in this project: {name}")
    current = settings.get("skillOverrides", {})
    if not isinstance(current, dict):
        current = {}
    new_overrides = dict(current)
    changes: list[dict[str, str]] = []
    for name, desired_state in choices.items():
        if desired_state not in DESIRED_STATES or name not in surfaced:
            continue
        before = str(current.get(name, "default"))
        if desired_state == "default":
            new_overrides.pop(name, None)
        else:
            new_overrides[name] = desired_state
        after = desired_state
        if before != after:
            changes.append({"op": "set_policy", "name": name, "before": before, "after": after})
    after_settings = dict(settings)
    if new_overrides:
        after_settings["skillOverrides"] = dict(sorted(new_overrides.items()))
    else:
        after_settings.pop("skillOverrides", None)
    before_hash = sha256_bytes(raw) if raw is not None else ""
    after_bytes = _settings_bytes(after_settings)
    after_hash = sha256_bytes(after_bytes)
    write_required = bool(changes) and before_hash != after_hash
    plan = finalize_plan(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "skill-policy",
            "project_root": path_text(project),
            "settings_path": path_text(path),
            "desired": choices,
            "before_sha256": before_hash,
            "after_sha256": after_hash,
            "settings_existed": raw is not None,
            "operations": changes if write_required else [],
            "errors": sorted(set(errors)),
        }
    )
    return plan, after_bytes if write_required else None


def build_policy_plan(
    project_root: str | os.PathLike[str],
    desired: Mapping[str, str],
) -> dict[str, object]:
    """Build a read-only plan without exposing unrelated Claude settings values."""
    plan, _after_bytes = _policy_plan_material(project_root, desired)
    return plan


def _record_path(project: Path, plan_hash: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return project / "AI" / "work" / "skill-management" / f"policy-rollback-{stamp}-{plan_hash[:12]}.json"


def apply_policy_plan(
    project_root: str | os.PathLike[str],
    desired: Mapping[str, str],
    confirm_plan: str | None = None,
) -> dict[str, object]:
    """Recompute, confirm, and atomically apply project-local Claude policy."""
    plan, after_bytes = _policy_plan_material(project_root, desired)
    require_confirmation(plan, confirm_plan)
    require_plan_clean(plan)
    project = resolved(project_root)
    path = assert_under(project, resolved(plan["settings_path"]))
    if not plan["operations"]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "OK",
            "action": "apply",
            "kind": "skill-policy",
            "plan_sha256": plan["plan_sha256"],
            "applied": 0,
            "rollback_record": None,
            "restart_required": False,
            "operations": [],
        }
    if after_bytes is None:
        raise SkillManagementError("policy plan omitted required private write material")
    before = path.read_bytes() if path.is_file() else None
    before_hash = sha256_bytes(before) if before is not None else ""
    if before_hash != plan["before_sha256"]:
        raise SkillManagementError(f"Claude settings changed after planning: {path}")
    record_path = assert_under(project, _record_path(project, str(plan["plan_sha256"])))
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": "skill-policy-rollback",
        "project_root": plan["project_root"],
        "settings_path": plan["settings_path"],
        "settings_existed": plan["settings_existed"],
        "before_sha256": plan["before_sha256"],
        "applied_settings_sha256": plan["after_sha256"],
        "apply_plan_sha256": plan["plan_sha256"],
        "desired": plan["desired"],
        "operations": plan["operations"],
    }
    atomic_write_json(record_path, record)
    try:
        atomic_write_bytes(path, after_bytes)
        if sha256_file(path) != plan["after_sha256"]:
            raise SkillManagementError(f"Claude settings verification failed: {path}")
    except Exception as exc:
        try:
            if before is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_bytes(path, before)
            record_path.unlink(missing_ok=True)
        except Exception as rollback_exc:
            raise SkillManagementError(
                f"policy apply failed: {exc}; rollback failed: {rollback_exc}"
            ) from exc
        raise SkillManagementError(f"policy apply failed and was rolled back: {exc}") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "OK",
        "action": "apply",
        "kind": "skill-policy",
        "plan_sha256": plan["plan_sha256"],
        "applied": len(plan["operations"]),
        "rollback_record": path_text(record_path),
        "restart_required": True,
        "operations": plan["operations"],
    }


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object while rejecting duplicate keys."""
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _is_sha256(value: object, *, allow_empty: bool = False) -> bool:
    """Return whether ``value`` is canonical lowercase SHA-256 hex."""
    if allow_empty and value == "":
        return True
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _load_rollback_record(project: Path, record_path: str | os.PathLike[str]) -> tuple[Path, dict[str, object], list[str]]:
    raw_path = Path(record_path)
    path = Path(os.path.abspath(str(raw_path if raw_path.is_absolute() else project / raw_path)))
    errors: list[str] = []
    rollback_root = Path(os.path.abspath(str(project / "AI" / "work" / "skill-management")))
    name_match = ROLLBACK_RECORD_NAME.fullmatch(path.name)
    if not same_path(path.parent, rollback_root) or name_match is None:
        return path, {}, ["rollback record must be a canonical AI/work/skill-management/policy-rollback-*.json file"]
    try:
        if path.is_symlink() or not same_path(path, Path(os.path.realpath(str(path)))):
            return path, {}, ["policy rollback record must be a non-redirected canonical file"]
        if not path.is_file():
            return path, {}, [f"policy rollback record is not a regular file: {path}"]
        if path.stat().st_size > 1_000_000:
            return path, {}, [f"policy rollback record is unreasonably large: {path}"]
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return path, {}, [f"cannot read rollback record {path}: {type(exc).__name__}: {exc}"]
    if not isinstance(value, dict):
        errors.append(f"invalid policy rollback record: {path}")
        return path, {}, errors
    if set(value) != ROLLBACK_RECORD_KEYS:
        errors.append("policy rollback record fields do not match the canonical schema")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != SCHEMA_VERSION:
        errors.append("rollback record schema_version is invalid")
    if value.get("kind") != "skill-policy-rollback":
        errors.append("rollback record kind is invalid")
    if value.get("project_root") != path_text(project):
        errors.append("rollback record project_root does not match this project")
    if value.get("settings_path") != path_text(_settings_path(project)):
        errors.append("rollback record settings_path does not match this project")
    if not isinstance(value.get("settings_existed"), bool):
        errors.append("rollback record settings_existed must be boolean")
    before_hash = value.get("before_sha256")
    if not _is_sha256(before_hash, allow_empty=not bool(value.get("settings_existed"))):
        errors.append("rollback record before_sha256 is invalid")
    if value.get("settings_existed") is False and before_hash != "":
        errors.append("rollback record before_sha256 must be empty when settings did not exist")
    applied_hash = value.get("applied_settings_sha256")
    if not _is_sha256(applied_hash):
        errors.append("rollback record applied_settings_sha256 is invalid")
    apply_plan_hash = value.get("apply_plan_sha256")
    if not _is_sha256(apply_plan_hash):
        errors.append("rollback record apply_plan_sha256 is invalid")
    elif name_match is not None and name_match.group(1) != str(apply_plan_hash)[:12]:
        errors.append("rollback record filename does not match its apply plan")
    desired = value.get("desired")
    if not isinstance(desired, dict) or not desired:
        errors.append("rollback record desired policy map is invalid")
    else:
        for name, state in desired.items():
            if not isinstance(name, str) or not name or Path(name).name != name or state not in DESIRED_STATES:
                errors.append("rollback record desired policy map contains an invalid entry")
    operations = value.get("operations")
    if not isinstance(operations, list) or not operations:
        errors.append("rollback record has no policy operations")
    else:
        seen: set[str] = set()
        ordered_names: list[str] = []
        for operation in operations:
            if not isinstance(operation, dict):
                errors.append("rollback record policy operation is not an object")
                continue
            if set(operation) != ROLLBACK_OPERATION_KEYS:
                errors.append("rollback record policy operation fields do not match the canonical schema")
            name = operation.get("name")
            before = operation.get("before")
            after = operation.get("after")
            if operation.get("op") != "set_policy" or not isinstance(name, str) or not name or Path(name).name != name:
                errors.append("rollback record contains an invalid policy operation")
                continue
            if name in seen:
                errors.append(f"rollback record contains duplicate policy operation: {name}")
            seen.add(name)
            ordered_names.append(name)
            if before not in DESIRED_STATES or after not in DESIRED_STATES or before == after:
                errors.append(f"rollback record contains invalid policy states for `{name}`")
            if isinstance(desired, dict) and desired.get(name) != after:
                errors.append(f"rollback record operation does not match desired policy for `{name}`")
        if ordered_names != sorted(ordered_names):
            errors.append("rollback record policy operations are not in canonical order")
    if not errors:
        apply_material = {
            "schema_version": SCHEMA_VERSION,
            "kind": "skill-policy",
            "project_root": value["project_root"],
            "settings_path": value["settings_path"],
            "desired": value["desired"],
            "before_sha256": value["before_sha256"],
            "after_sha256": value["applied_settings_sha256"],
            "settings_existed": value["settings_existed"],
            "operations": value["operations"],
            "errors": [],
        }
        if plan_sha256(apply_material) != value["apply_plan_sha256"]:
            errors.append("rollback record content does not match its apply plan")
    return path, value if not errors else {}, errors


def _policy_restore_material(
    project_root: str | os.PathLike[str],
    record_path: str | os.PathLike[str],
) -> tuple[dict[str, object], bytes | None]:
    """Build a policy-scoped restore plan while preserving unrelated live settings."""
    project = resolved(project_root)
    record_file, record, errors = _load_rollback_record(project, record_path)
    settings_path = _settings_path(project)
    settings, raw, settings_errors = _load_settings(settings_path)
    errors.extend(settings_errors)
    current_hash = sha256_bytes(raw) if raw is not None else ""
    current_overrides = settings.get("skillOverrides", {})
    if not isinstance(current_overrides, dict):
        current_overrides = {}
    new_overrides = dict(current_overrides)
    surfaced = set(_surface_names(project))
    changes: list[dict[str, str]] = []
    if record and not errors:
        for operation in record["operations"]:
            name = str(operation["name"])
            applied_state = str(operation["after"])
            restore_state = str(operation["before"])
            current_state = str(current_overrides.get(name, "default"))
            if name not in surfaced:
                errors.append(f"Claude skill is no longer surfaced in this project: {name}")
                continue
            if current_state != applied_state:
                errors.append(
                    f"Claude policy for `{name}` changed after apply: expected {applied_state}, found {current_state}"
                )
                continue
            if restore_state == "default":
                new_overrides.pop(name, None)
            else:
                new_overrides[name] = restore_state
            changes.append({"op": "restore_policy", "name": name, "before": current_state, "after": restore_state})
    after_settings = dict(settings)
    if new_overrides:
        after_settings["skillOverrides"] = dict(sorted(new_overrides.items()))
    else:
        after_settings.pop("skillOverrides", None)
    delete_settings = bool(record and not record.get("settings_existed") and not after_settings)
    after_bytes = None if delete_settings else _settings_bytes(after_settings)
    after_hash = "" if delete_settings else sha256_bytes(after_bytes or b"")
    plan = finalize_plan(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "skill-policy-restore",
            "project_root": path_text(project),
            "settings_path": path_text(settings_path),
            "record_path": path_text(record_file),
            "record_sha256": sha256_file(record_file),
            "before_sha256": current_hash,
            "after_sha256": after_hash,
            "applied_settings_sha256": record.get("applied_settings_sha256") if record else None,
            "applied_settings_hash_match": bool(record and current_hash == record.get("applied_settings_sha256")),
            "delete_settings": delete_settings,
            "operations": changes if record and not errors else [],
            "errors": sorted(set(errors)),
        }
    )
    return plan, after_bytes if record and not errors else None


def build_policy_restore_plan(
    project_root: str | os.PathLike[str],
    record_path: str | os.PathLike[str],
) -> dict[str, object]:
    """Build a read-only restore plan without exposing prior settings values."""
    plan, _before_bytes = _policy_restore_material(project_root, record_path)
    return plan


def restore_policy(
    project_root: str | os.PathLike[str],
    record_path: str | os.PathLike[str],
    confirm_plan: str | None = None,
) -> dict[str, object]:
    """Recompute, confirm, and restore project-local Claude settings."""
    plan, after_bytes = _policy_restore_material(project_root, record_path)
    require_confirmation(plan, confirm_plan)
    require_plan_clean(plan)
    project = resolved(project_root)
    if not plan["operations"]:
        raise SkillManagementError("policy restore plan has no operations")
    path = assert_under(project, resolved(plan["settings_path"]))
    before = path.read_bytes() if path.is_file() else None
    current_hash = sha256_bytes(before) if before is not None else ""
    if current_hash != plan["before_sha256"]:
        raise SkillManagementError(f"Claude settings changed after restore planning: {path}")
    try:
        if plan["delete_settings"]:
            path.unlink(missing_ok=True)
        else:
            if after_bytes is None:
                raise SkillManagementError("restore plan omitted required private settings material")
            atomic_write_bytes(path, after_bytes)
        if sha256_file(path) != plan["after_sha256"]:
            raise SkillManagementError(f"Claude settings restore verification failed: {path}")
    except Exception as exc:
        try:
            if before is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_bytes(path, before)
            restored_hash = sha256_file(path)
            if restored_hash != current_hash:
                raise SkillManagementError(f"rollback verification failed for {path}")
        except Exception as rollback_exc:
            raise SkillManagementError(f"policy restore failed: {exc}; rollback failed: {rollback_exc}") from exc
        raise SkillManagementError(f"policy restore failed and was rolled back: {exc}") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "OK",
        "action": "restore",
        "kind": "skill-policy",
        "plan_sha256": plan["plan_sha256"],
        "restored": len(plan["operations"]),
        "record_path": plan["record_path"],
        "restart_required": True,
        "operations": plan["operations"],
    }
