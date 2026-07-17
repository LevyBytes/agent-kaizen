"""Plan, persist, query, and inspect validated skill-context snapshots.

The external skill store remains authoritative. This module stores only portable metadata and
hashes; SKILL.md text is read from disk after a live hash check and is never persisted here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from support_scripts.skill_management import (
    SkillManagementError,
    discover_skills,
    plan_sha256,
    policy_status,
    validate_skill_package,
)

from .db import new_id, now, read_retry, schema_status, write_tx
from .denials import KaizenDenied
from .hashing import utc_text_hash
from .paths import DB_PATH, REPO_ROOT


_HOSTS = ("codex", "claude")
_SURFACE_ROOT = {"codex": ".agents", "claude": ".claude"}
_SURFACE_STATUSES = {"missing", "correct", "wrong_target", "dangling_link", "real_directory"}
_POLICY_STATES = ("on", "name-only", "user-invocable-only", "off")
_PUBLICATION_STATUSES = ("published", "staged")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_WORDS = re.compile(r"[a-z0-9]+")
_QUERY_STOPWORDS = frozenset(
    {
        "a",
        "also",
        "an",
        "and",
        "by",
        "even",
        "for",
        "in",
        "not",
        "of",
        "on",
        "or",
        "other",
        "the",
        "to",
        "use",
        "using",
        "when",
        "with",
        "work",
        "working",
    }
)


def _store_root(args: Any) -> Path:
    """Resolve an explicit store root, then KAIZEN_SKILLS_ROOT, DEVROOT, or the repo sibling."""
    raw = getattr(args, "store_root", None) or os.environ.get("KAIZEN_SKILLS_ROOT")
    if raw:
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = REPO_ROOT / candidate
    elif os.environ.get("DEVROOT"):
        candidate = Path(os.environ["DEVROOT"]) / "SKILLS" / "skills"
    else:
        candidate = REPO_ROOT.parent / "SKILLS" / "skills"
    root = candidate.resolve(strict=False)
    if not root.is_dir():
        raise KaizenDenied(
            "DENIED_SKILL_STORE_NOT_FOUND",
            {"required_action": "pass --store-root pointing at the external SKILLS/skills directory"},
            exit_code=1,
        )
    return root


def _hosts(args: Any) -> tuple[str, ...]:
    raw = getattr(args, "host", None)
    values = list(raw) if isinstance(raw, (list, tuple)) else ([raw] if raw else list(_HOSTS))
    if "both" in values:
        values = list(_HOSTS)
    hosts: list[str] = []
    for value in values:
        host = str(value).strip().lower()
        if host not in _HOSTS:
            raise KaizenDenied(
                "DENIED_SKILL_HOST_INVALID",
                {"host": host, "allowed": [*_HOSTS, "both"], "required_action": "pass --host codex|claude|both"},
                exit_code=2,
            )
        if host not in hosts:
            hosts.append(host)
    return tuple(host for host in _HOSTS if host in hosts)


def _query_host(args: Any) -> str:
    """Require one concrete host for automatic context retrieval."""
    raw = getattr(args, "host", None)
    values = list(raw) if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
    if len(values) != 1 or str(values[0]).strip().lower() not in _HOSTS:
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_HOST_REQUIRED",
            {
                "allowed": list(_HOSTS),
                "required_action": "pass exactly one --host codex|claude so host policy cannot be bypassed",
            },
            exit_code=2,
        )
    return str(values[0]).strip().lower()


def _policy_states(hosts: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """Read live project-local host policy without persisting host settings or absolute paths."""
    states: dict[str, dict[str, str]] = {host: {} for host in hosts}
    if "claude" not in hosts:
        return states
    try:
        observed = policy_status(REPO_ROOT)
    except (OSError, SkillManagementError, TypeError, ValueError) as error:
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_POLICY_INVALID",
            {"reason": type(error).__name__, "required_action": "repair Claude project skill policy and retry"},
            exit_code=2,
        ) from error
    errors = observed.get("errors") if isinstance(observed, dict) else None
    skills = observed.get("skills") if isinstance(observed, dict) else None
    if errors or not isinstance(skills, list):
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_POLICY_INVALID",
            {
                "error_count": len(errors) if isinstance(errors, list) else 1,
                "required_action": "repair Claude project skill policy and retry",
            },
            exit_code=2,
        )
    for item in skills:
        if not isinstance(item, dict):
            raise KaizenDenied(
                "DENIED_SKILL_CONTEXT_POLICY_INVALID",
                {"required_action": "repair malformed Claude project skill policy and retry"},
                exit_code=2,
            )
        name = str(item.get("name") or "")
        state = str(item.get("current_policy") or "")
        if not name or Path(name).name != name or state not in _POLICY_STATES or name in states["claude"]:
            raise KaizenDenied(
                "DENIED_SKILL_CONTEXT_POLICY_INVALID",
                {"required_action": "repair malformed or duplicate Claude project skill policy and retry"},
                exit_code=2,
            )
        states["claude"][name] = state
    return states


def _portable_relative(raw: Any, field: str) -> str:
    value = str(raw or "").replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]):
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_PATH_INVALID",
            {"field": field, "required_action": "re-run validation with a store-relative skill path"},
            exit_code=2,
        )
    return path.as_posix()


def _sha256(raw: Any, field: str) -> str:
    value = str(raw or "").lower()
    if not _HEX64.fullmatch(value):
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
            {"field": field, "required_action": "re-run package discovery and validation"},
            exit_code=2,
        )
    return value


def _portable_errors(errors: list[str], *, store_root: str, source_path: str) -> list[str]:
    """Redact known machine-absolute roots from persisted validator diagnostics."""
    replacements = ((source_path, "<skill-package>"), (store_root, "<skill-store>"))
    portable: list[str] = []
    for error in errors:
        text = error
        for raw, marker in replacements:
            for variant in {raw, raw.replace("\\", "/")} if raw else ():
                text = text.replace(variant, marker)
        portable.append(text)
    return sorted(set(portable))


def _portable_orphan_surfaces(inventory: Any, hosts: tuple[str, ...]) -> list[dict[str, str]]:
    """Validate orphan-surface diagnostics and remove machine-absolute paths."""
    raw_orphans = inventory.get("orphan_surfaces") if isinstance(inventory, dict) else None
    if not isinstance(raw_orphans, list):
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
            {"required_action": "re-run package discovery; orphan_surfaces must be a list"},
            exit_code=2,
        )
    portable: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_orphans:
        if not isinstance(raw, dict):
            raise KaizenDenied(
                "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
                {"required_action": "re-run package discovery; every orphan surface must be an object"},
                exit_code=2,
            )
        host = str(raw.get("host") or "")
        name = str(raw.get("name") or "")
        status = str(raw.get("status") or "")
        if (
            host not in hosts
            or not name
            or Path(name).name != name
            or name in {".", ".."}
            or any(ord(char) < 32 for char in name)
            or status not in {"wrong_target", "dangling_link", "real_directory"}
            or (host, name) in seen
        ):
            raise KaizenDenied(
                "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
                {"required_action": "repair malformed or duplicate orphan-surface diagnostics and retry"},
                exit_code=2,
            )
        seen.add((host, name))
        portable.append(
            {
                "name": name,
                "host": host,
                "status": status,
                "surface_relpath": f"{_SURFACE_ROOT[host]}/skills/{name}",
            }
        )
    return sorted(portable, key=lambda row: (_HOSTS.index(row["host"]), row["name"].casefold()))


def _portable_surface_conflicts(
    records: list[dict[str, Any]],
    orphan_surfaces: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Return unsafe host-visible surfaces without absolute paths or targets."""
    conflicts: list[dict[str, Any]] = []
    for record in records:
        for surface in record["surfaces"]:
            if surface["validation_status"] in {"wrong_target", "dangling_link", "real_directory"}:
                conflicts.append(
                    {
                        "name": record["name"],
                        "host": surface["host"],
                        "status": surface["validation_status"],
                        "surface_relpath": surface["surface_relpath"],
                        "orphan": False,
                    }
                )
    conflicts.extend({**surface, "orphan": True} for surface in orphan_surfaces)
    return sorted(
        conflicts,
        key=lambda row: (_HOSTS.index(row["host"]), str(row["name"]).casefold(), bool(row["orphan"])),
    )


def _normalize_inventory(
    inventory: Any,
    hosts: tuple[str, ...],
    policies: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Validate discovery output and reduce it to portable snapshot records."""
    if (
        not isinstance(inventory, dict)
        or inventory.get("schema_version") != 1
        or not isinstance(inventory.get("skills"), list)
        or inventory.get("complete") is not True
        or inventory.get("scan_errors")
        or inventory.get("hosts") != list(hosts)
    ):
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
            {
                "scan_errors": list(inventory.get("scan_errors") or [])[:10] if isinstance(inventory, dict) else [],
                "required_action": "repair the scan failure and re-run package discovery with the supported schema",
            },
            exit_code=2,
        )

    store_root = str(inventory.get("store_root") or "")
    policy_states = policies or {host: {} for host in hosts}
    if set(policy_states) != set(hosts) or any(not isinstance(values, dict) for values in policy_states.values()):
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_POLICY_INVALID",
            {"required_action": "re-read host policy for the selected host scope"},
            exit_code=2,
        )
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in inventory["skills"]:
        if not isinstance(raw, dict):
            raise KaizenDenied(
                "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
                {"required_action": "re-run package discovery; every skill record must be an object"},
                exit_code=2,
            )
        source_relpath = _portable_relative(raw.get("source_relpath"), "source_relpath")
        source_parts = PurePosixPath(source_relpath).parts
        name = source_parts[0] if len(source_parts) == 1 else ""
        if not name or name in {".", ".."} or any(ord(char) < 32 for char in name) or name in seen:
            raise KaizenDenied(
                "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
                {"skill": name, "required_action": "repair duplicate or unsafe package directory names before syncing"},
                exit_code=2,
            )
        seen.add(name)
        covers_raw = raw.get("covers") or []
        if not isinstance(covers_raw, list) or any(not isinstance(item, str) for item in covers_raw):
            raise KaizenDenied(
                "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
                {"skill": name, "field": "covers", "required_action": "re-run package validation"},
                exit_code=2,
            )
        covers = sorted({item.strip() for item in covers_raw if item.strip()}, key=str.casefold)
        publication_status = str(raw.get("publication_status") or "")
        publication_reason = str(raw.get("publication_reason") or "")
        if publication_status not in _PUBLICATION_STATUSES or not _SAFE_REASON.fullmatch(publication_reason):
            raise KaizenDenied(
                "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
                {
                    "skill": name,
                    "field": "publication_status",
                    "required_action": "re-run package discovery with validated non-sensitive publication metadata",
                },
                exit_code=2,
            )
        relpath = _portable_relative(raw.get("skill_md_relative_path"), "skill_md_relative_path")
        if PurePosixPath(relpath).parts != (name, "SKILL.md"):
            raise KaizenDenied(
                "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
                {
                    "skill": name,
                    "field": "skill_md_relative_path",
                    "required_action": "re-run package discovery; SKILL.md must be the package-root entrypoint",
                },
                exit_code=2,
            )
        host_records = raw.get("hosts") or {}
        if not isinstance(host_records, dict):
            raise KaizenDenied(
                "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
                {"skill": name, "field": "hosts", "required_action": "re-run package discovery"},
                exit_code=2,
            )
        surfaces: list[dict[str, str]] = []
        for host in hosts:
            observation = host_records.get(host) or {"status": "missing"}
            if not isinstance(observation, dict):
                raise KaizenDenied(
                    "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
                    {"skill": name, "host": host, "required_action": "re-run package discovery"},
                    exit_code=2,
                )
            status = str(observation.get("status") or "missing")
            if status not in _SURFACE_STATUSES:
                raise KaizenDenied(
                    "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
                    {"skill": name, "host": host, "required_action": "re-run package discovery"},
                    exit_code=2,
                )
            path_kind = "real_directory" if status == "real_directory" else ("missing" if status == "missing" else "link")
            policy_state = str(policy_states[host].get(name, "on"))
            if policy_state not in _POLICY_STATES:
                raise KaizenDenied(
                    "DENIED_SKILL_CONTEXT_POLICY_INVALID",
                    {"skill": name, "host": host, "required_action": "repair host skill policy and retry"},
                    exit_code=2,
                )
            surfaces.append(
                {
                    "host": host,
                    "surface_relpath": f"{_SURFACE_ROOT[host]}/skills/{name}",
                    "path_kind": path_kind,
                    "validation_status": status,
                    "policy_state": policy_state,
                }
            )
        errors = raw.get("errors") or []
        if not isinstance(errors, list) or any(not isinstance(item, str) for item in errors):
            raise KaizenDenied(
                "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
                {"skill": name, "field": "errors", "required_action": "re-run package discovery"},
                exit_code=2,
            )
        is_valid = raw.get("valid") is True
        raw_skill_hash = str(raw.get("skill_md_sha256") or "").lower()
        skill_hash = _sha256(raw_skill_hash, "skill_md_sha256") if is_valid or raw_skill_hash else ""
        record = {
            "name": name,
            "description": str(raw.get("description") or "").strip(),
            "covers": covers,
            "skill_md_relpath": relpath,
            "skill_sha256": skill_hash,
            "package_sha256": _sha256(raw.get("package_sha256"), "package_sha256"),
            "validation_status": "valid" if is_valid else "invalid",
            "validation_errors": _portable_errors(
                errors,
                store_root=store_root,
                source_path=str(raw.get("source_path") or ""),
            ),
            "publication_status": publication_status,
            "publication_reason": publication_reason,
            "surfaces": surfaces,
        }
        for surface in surfaces:
            surface["content_hash"] = utc_text_hash(surface)
        record["content_hash"] = utc_text_hash(record)
        records.append(record)
    return sorted(records, key=lambda record: record["name"].casefold())


def _inventory_material(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Return portable public snapshot rows and their deterministic inventory hash."""
    portable = [{key: value for key, value in record.items() if key != "content_hash"} for record in records]
    return portable, utc_text_hash({"skills": portable})


def _discover_snapshot(root: Path, hosts: tuple[str, ...]) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]], str]:
    """Read and normalize one live package, surface, publication, and policy snapshot."""
    try:
        inventory = discover_skills(REPO_ROOT, root, hosts=hosts)
    except (OSError, SkillManagementError) as error:
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE",
            {"reason": str(error), "required_action": "repair the skill inventory scan and retry"},
            exit_code=2,
        ) from error
    orphan_surfaces = _portable_orphan_surfaces(inventory, hosts)
    records = _normalize_inventory(inventory, hosts, _policy_states(hosts))
    surface_conflicts = _portable_surface_conflicts(records, orphan_surfaces)
    _portable, inventory_hash = _inventory_material(records)
    return records, orphan_surfaces, surface_conflicts, inventory_hash


def _plan(args: Any) -> tuple[dict[str, Any], Path]:
    root = _store_root(args)
    hosts = _hosts(args)
    records, orphan_surfaces, surface_conflicts, inventory_hash = _discover_snapshot(root, hosts)
    if surface_conflicts:
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_ORPHAN_SURFACE"
            if all(conflict["orphan"] for conflict in surface_conflicts)
            else "DENIED_SKILL_CONTEXT_SURFACE_CONFLICT",
            {
                "orphan_surfaces": orphan_surfaces,
                "surface_conflicts": surface_conflicts,
                "required_action": "remove or explicitly reconcile unsafe host skill surfaces against the authoritative store",
            },
            exit_code=2,
        )
    portable_snapshot = [{key: value for key, value in record.items() if key != "content_hash"} for record in records]
    core = {
        "operation": "skill-context-sync",
        "store_root": str(root),
        "hosts": list(hosts),
        "is_test": bool(getattr(args, "test", False)),
        "inventory_hash": inventory_hash,
        "skill_count": len(records),
        "surface_count": sum(len(record["surfaces"]) for record in records),
        "skills": portable_snapshot,
    }
    return {**core, "plan_sha256": plan_sha256(core), "records": records}, root


def _require_context_tables() -> None:
    if not DB_PATH.is_file():
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_SCHEMA_REQUIRED",
            {"required_action": "run K1 before querying skill context"},
            exit_code=1,
        )
    status = schema_status()
    if not status.get("schema_ok"):
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_SCHEMA_REQUIRED",
            {"required_action": "inspect with K2 and initialize or repair schema v1 with K1"},
            exit_code=2,
        )
    required_columns = {
        "skill_context_syncs": {
            "id",
            "created_at",
            "completed_at",
            "status",
            "plan_hash",
            "inventory_hash",
            "skill_count",
            "surface_count",
            "hosts_json",
            "is_current",
            "summary",
            "content_hash",
            "is_test",
        },
        "skill_contexts": {
            "id",
            "sync_id",
            "skill_name",
            "description",
            "covers_json",
            "skill_md_relpath",
            "skill_sha256",
            "package_sha256",
            "validation_status",
            "validation_errors_json",
            "publication_status",
            "publication_reason",
            "content_hash",
        },
        "skill_context_surfaces": {
            "id",
            "context_id",
            "host",
            "surface_relpath",
            "path_kind",
            "validation_status",
            "policy_state",
            "content_hash",
        },
        "skill_context_events": {
            "id",
            "sync_id",
            "created_at",
            "event_type",
            "skill_name",
            "summary",
            "payload_json",
            "content_hash",
        },
    }
    observed_columns = read_retry(
        lambda conn: {
            table: {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for table in required_columns
        }
    )
    missing_columns = sorted(
        f"{table}.{column}"
        for table, columns in required_columns.items()
        for column in columns - observed_columns[table]
    )
    if missing_columns:
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_SCHEMA_REQUIRED",
            {
                "missing_columns": missing_columns,
                "required_action": "run K1 to install the additive schema-v1 skill context tables and columns",
            },
            exit_code=1,
        )


def _read_current_rows(
    conn: Any,
) -> tuple[tuple[Any, ...] | None, list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Read one production-current snapshot from an existing connection."""
    sync = conn.execute(
        "SELECT id, created_at, completed_at, status, plan_hash, inventory_hash, skill_count, "
        "surface_count, hosts_json, content_hash FROM skill_context_syncs "
        "WHERE is_current = 1 AND is_test = 0"
    ).fetchone()
    if sync is None:
        return None, [], []
    contexts = list(
        conn.execute(
            "SELECT id, skill_name, description, covers_json, skill_md_relpath, skill_sha256, "
            "package_sha256, validation_status, validation_errors_json, publication_status, "
            "publication_reason, content_hash FROM skill_contexts "
            "WHERE sync_id = ? ORDER BY skill_name",
            (sync[0],),
        ).fetchall()
    )
    surfaces = list(
        conn.execute(
            "SELECT s.context_id, s.host, s.surface_relpath, s.path_kind, s.validation_status, "
            "s.policy_state, s.content_hash "
            "FROM skill_context_surfaces s JOIN skill_contexts c ON c.id = s.context_id "
            "WHERE c.sync_id = ? ORDER BY c.skill_name, s.host",
            (sync[0],),
        ).fetchall()
    )
    return sync, contexts, surfaces


def _current_rows() -> tuple[tuple[Any, ...] | None, list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    _require_context_tables()
    result = read_retry(_read_current_rows)
    _verify_current_integrity(*result)
    return result


def _verify_current_integrity(
    sync: tuple[Any, ...] | None,
    contexts: list[tuple[Any, ...]],
    surfaces: list[tuple[Any, ...]],
) -> None:
    """Fail closed when routing metadata no longer matches its stored hashes."""
    if sync is None:
        return
    try:
        if not _HEX64.fullmatch(str(sync[4] or "")) or not _HEX64.fullmatch(str(sync[5] or "")):
            raise ValueError("invalid snapshot plan or inventory hash")
        hosts = json.loads(sync[8])
        if (
            not isinstance(hosts, list)
            or hosts != [host for host in _HOSTS if host in hosts]
            or any(host not in _HOSTS for host in hosts)
        ):
            raise ValueError("invalid hosts_json")
        if int(sync[6]) != len(contexts) or int(sync[7]) != len(surfaces):
            raise ValueError("snapshot counts do not match child rows")
        sync_material = {
            "status": sync[3],
            "plan_hash": sync[4],
            "inventory_hash": sync[5],
            "skill_count": sync[6],
            "surface_count": sync[7],
            "hosts": hosts,
            "is_test": False,
        }
        if utc_text_hash(sync_material) != sync[9]:
            raise ValueError("snapshot content hash mismatch")
        by_context: dict[str, list[dict[str, str]]] = {}
        for context_id, host, relpath, path_kind, validation_status, policy_state, content_hash in surfaces:
            if policy_state not in _POLICY_STATES:
                raise ValueError(f"invalid or legacy policy state for {context_id}/{host}")
            surface = {
                "host": host,
                "surface_relpath": relpath,
                "path_kind": path_kind,
                "validation_status": validation_status,
                "policy_state": policy_state,
            }
            if utc_text_hash(surface) != content_hash:
                raise ValueError(f"surface content hash mismatch for {context_id}/{host}")
            surface["content_hash"] = content_hash
            by_context.setdefault(context_id, []).append(surface)
        verified_records: list[dict[str, Any]] = []
        for row in contexts:
            (
                context_id,
                name,
                description,
                covers_json,
                relpath,
                skill_hash,
                package_hash,
                validation,
                errors_json,
                publication_status,
                publication_reason,
                content_hash,
            ) = row
            if publication_status not in _PUBLICATION_STATUSES or not _SAFE_REASON.fullmatch(
                str(publication_reason or "")
            ):
                raise ValueError(f"invalid or legacy publication state for {name}")
            record = {
                "name": name,
                "description": description,
                "covers": json.loads(covers_json),
                "skill_md_relpath": relpath,
                "skill_sha256": skill_hash,
                "package_sha256": package_hash,
                "validation_status": validation,
                "validation_errors": json.loads(errors_json),
                "publication_status": publication_status,
                "publication_reason": publication_reason,
                "surfaces": sorted(by_context.get(context_id, []), key=lambda item: _HOSTS.index(item["host"])),
            }
            if utc_text_hash(record) != content_hash:
                raise ValueError(f"context content hash mismatch for {name}")
            record["content_hash"] = content_hash
            verified_records.append(record)
        _portable, observed_inventory_hash = _inventory_material(
            sorted(verified_records, key=lambda item: str(item["name"]).casefold())
        )
        if observed_inventory_hash != sync[5]:
            raise ValueError("snapshot inventory hash does not match child rows")
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_INTEGRITY",
            {
                "reason": str(error),
                "required_action": "inspect DB integrity, then reapply a freshly reviewed SK6 plan",
            },
            exit_code=2,
        ) from error


def context_sync(args: Any) -> dict[str, Any]:
    """SK6: build a read-only plan, or atomically apply that exact recomputed plan."""
    action = str(getattr(args, "action", None) or "plan").strip().lower()
    if action not in {"plan", "apply"}:
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_ACTION_INVALID",
            {"action": action, "allowed": ["plan", "apply"], "required_action": "pass --action plan|apply"},
            exit_code=2,
        )
    plan, _root = _plan(args)
    public_plan = {key: value for key, value in plan.items() if key != "records"}
    if action == "plan":
        return {"status": "OK", "action": "plan", **public_plan, "writes": 0}

    confirmed = str(getattr(args, "confirm_plan", None) or "").strip().lower()
    if not confirmed:
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_CONFIRM_REQUIRED",
            {
                "plan_sha256": plan["plan_sha256"],
                "required_action": "re-run apply with --confirm-plan <plan_sha256>",
            },
            exit_code=2,
        )
    if confirmed != plan["plan_sha256"]:
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_PLAN_MISMATCH",
            {
                "confirmed_plan_sha256": confirmed,
                "observed_plan_sha256": plan["plan_sha256"],
                "required_action": "inspect the recomputed plan and confirm its current hash",
            },
            exit_code=2,
        )

    _require_context_tables()

    sync_id = new_id("sks")
    created = now()
    is_test = int(bool(getattr(args, "test", False)))
    records = plan["records"]

    def persist(conn: Any, _attempt: int) -> dict[str, Any]:
        current_sync, current_contexts, current_surfaces = _read_current_rows(conn)
        current_integrity_ok = True
        try:
            _verify_current_integrity(current_sync, current_contexts, current_surfaces)
        except KaizenDenied as error:
            if error.code != "DENIED_SKILL_CONTEXT_INTEGRITY":
                raise
            current_integrity_ok = False
        if (
            not is_test
            and current_integrity_ok
            and current_sync
            and current_sync[4] == plan["plan_sha256"]
            and current_sync[5] == plan["inventory_hash"]
        ):
            return {
                "sync_id": current_sync[0],
                "no_op": True,
                "changes": {"added": 0, "updated": 0, "retired": 0},
            }
        old_rows = {row[1]: row[-1] for row in current_contexts}
        if not is_test:
            conn.execute("UPDATE skill_context_syncs SET is_current = 0 WHERE is_current = 1 AND is_test = 0")
        sync_hash = utc_text_hash(
            {
                "status": "complete",
                "plan_hash": plan["plan_sha256"],
                "inventory_hash": plan["inventory_hash"],
                "skill_count": plan["skill_count"],
                "surface_count": plan["surface_count"],
                "hosts": plan["hosts"],
                "is_test": bool(is_test),
            }
        )
        conn.execute(
            "INSERT INTO skill_context_syncs "
            "(id, created_at, completed_at, status, plan_hash, inventory_hash, skill_count, surface_count, "
            "hosts_json, is_current, summary, content_hash, is_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sync_id,
                created,
                created,
                "complete",
                plan["plan_sha256"],
                plan["inventory_hash"],
                plan["skill_count"],
                plan["surface_count"],
                json.dumps(plan["hosts"], separators=(",", ":")),
                0 if is_test else 1,
                "Validated skill context snapshot applied.",
                sync_hash,
                is_test,
            ),
        )
        current_hashes: dict[str, str] = {}
        for record in records:
            context_id = new_id("skc")
            current_hashes[record["name"]] = record["content_hash"]
            conn.execute(
                "INSERT INTO skill_contexts "
                "(id, sync_id, skill_name, description, covers_json, skill_md_relpath, skill_sha256, "
                "package_sha256, validation_status, validation_errors_json, publication_status, "
                "publication_reason, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    context_id,
                    sync_id,
                    record["name"],
                    record["description"],
                    json.dumps(record["covers"], ensure_ascii=False, separators=(",", ":")),
                    record["skill_md_relpath"],
                    record["skill_sha256"],
                    record["package_sha256"],
                    record["validation_status"],
                    json.dumps(record["validation_errors"], ensure_ascii=False, separators=(",", ":")),
                    record["publication_status"],
                    record["publication_reason"],
                    record["content_hash"],
                ),
            )
            for surface in record["surfaces"]:
                conn.execute(
                    "INSERT INTO skill_context_surfaces "
                    "(id, context_id, host, surface_relpath, path_kind, validation_status, policy_state, "
                    "content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id("sksf"),
                        context_id,
                        surface["host"],
                        surface["surface_relpath"],
                        surface["path_kind"],
                        surface["validation_status"],
                        surface["policy_state"],
                        surface["content_hash"],
                    ),
                )

        changes = {
            "added": sorted(set(current_hashes) - set(old_rows), key=str.casefold),
            "updated": sorted(
                (name for name in set(current_hashes) & set(old_rows) if current_hashes[name] != old_rows[name]),
                key=str.casefold,
            ),
            "retired": sorted(set(old_rows) - set(current_hashes), key=str.casefold),
        }
        events = [(kind, name, {}) for kind, names in changes.items() for name in names]
        events.append(("snapshot_applied", None, {key: len(value) for key, value in changes.items()}))
        for event_type, skill_name, payload in events:
            payload_text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            summary = f"Skill context {event_type.replace('_', ' ')}."
            conn.execute(
                "INSERT INTO skill_context_events "
                "(id, sync_id, created_at, event_type, skill_name, summary, payload_json, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("ske"),
                    sync_id,
                    created,
                    event_type,
                    skill_name,
                    summary,
                    payload_text,
                    utc_text_hash(
                        {"sync_id": sync_id, "event_type": event_type, "skill_name": skill_name, "payload": payload}
                    ),
                ),
            )
        return {
            "sync_id": sync_id,
            "no_op": False,
            "changes": {key: len(value) for key, value in changes.items()},
        }

    outcome = write_tx(persist)
    return {
        "status": "OK",
        "action": "apply",
        "sync_id": outcome["sync_id"],
        "plan_sha256": plan["plan_sha256"],
        "inventory_hash": plan["inventory_hash"],
        "skill_count": plan["skill_count"],
        "surface_count": plan["surface_count"],
        "no_op": outcome["no_op"],
        "changes": outcome["changes"],
    }


def _live_skill(root: Path, relpath: str) -> tuple[Path | None, str, bytes | None]:
    """Read one contained SKILL.md once and return its path, hash/status, and exact bytes."""
    candidate = (root / Path(relpath)).resolve(strict=False)
    if candidate != root and root not in candidate.parents:
        return None, "outside_store", None
    if not candidate.is_file():
        return None, "missing", None
    try:
        body = candidate.read_bytes()
    except OSError:
        return None, "unreadable", None
    return candidate, hashlib.sha256(body).hexdigest(), body


def _score(query: str, name: str, description: str, covers: list[str]) -> int:
    phrase = query.casefold().strip()
    name_text = name.casefold()
    covers_text = " ".join(covers).casefold()
    description_text = description.casefold()
    tokens = set(_WORDS.findall(phrase)) - _QUERY_STOPWORDS
    name_tokens = set(_WORDS.findall(name_text))
    score = 100 if phrase == name_text else 0
    if name_tokens and name_tokens <= tokens:
        score += 40
    for token in tokens:
        if token in name_tokens:
            score += 12
        if token in set(_WORDS.findall(covers_text)):
            score += 6
        if token in set(_WORDS.findall(description_text)):
            score += 2
    return score


def context_query(args: Any) -> dict[str, Any]:
    """SK7: return host-eligible live SKILL.md context without bypassing host policy."""
    query = str(getattr(args, "query", None) or "").strip()
    if not query:
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_QUERY_REQUIRED",
            {"required_action": "pass --query describing the current task intent"},
            exit_code=2,
        )
    try:
        limit = int(getattr(args, "limit", None) if getattr(args, "limit", None) is not None else 5)
    except (TypeError, ValueError) as error:
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_LIMIT_INVALID",
            {"required_action": "pass --limit as an integer from 0 through 50"},
            exit_code=2,
        ) from error
    if limit < 0 or limit > 50:
        raise KaizenDenied(
            "DENIED_SKILL_CONTEXT_LIMIT_INVALID",
            {"limit": limit, "required_action": "pass --limit from 0 through 50"},
            exit_code=2,
        )
    host = _query_host(args)
    try:
        root = _store_root(args)
        sync, contexts, surfaces = _current_rows()
    except KaizenDenied as error:
        if error.code not in {
            "DENIED_SKILL_STORE_NOT_FOUND",
            "DENIED_SKILL_CONTEXT_SCHEMA_REQUIRED",
            "DENIED_SKILL_CONTEXT_INTEGRITY",
        }:
            raise
        return {
            "status": "OK",
            "available": False,
            "current": False,
            "query": query,
            "host": host,
            "count": 0,
            "matches": [],
            "reason": error.code,
            "required_action": error.fields.get("required_action"),
        }
    if sync is None:
        return {
            "status": "OK",
            "available": False,
            "current": False,
            "query": query,
            "host": host,
            "count": 0,
            "matches": [],
            "reason": "no_applied_snapshot",
            "required_action": "run SK6 --action plan, review it, then apply the confirmed snapshot",
        }

    stored_hosts = tuple(json.loads(sync[8]))
    if host not in stored_hosts:
        return {
            "status": "OK",
            "available": False,
            "current": False,
            "query": query,
            "host": host,
            "count": 0,
            "matches": [],
            "reason": "host_not_synced",
            "required_action": "apply a freshly reviewed SK6 snapshot that includes this host",
        }
    try:
        live_records, orphan_surfaces, _surface_conflicts, live_inventory_hash = _discover_snapshot(
            root, stored_hosts
        )
    except KaizenDenied as error:
        if error.code not in {"DENIED_SKILL_CONTEXT_SCAN_INCOMPLETE", "DENIED_SKILL_CONTEXT_POLICY_INVALID"}:
            raise
        return {
            "status": "OK",
            "available": False,
            "current": False,
            "query": query,
            "host": host,
            "count": 0,
            "matches": [],
            "reason": error.code,
            "required_action": error.fields.get("required_action"),
        }

    matches: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    observed_drift = False
    live_by_name = {record["name"]: record for record in live_records}
    stored_surfaces = {
        (context_id, surface_host): {
            "surface_relpath": relpath,
            "path_kind": path_kind,
            "validation_status": validation_status,
            "policy_state": policy_state,
        }
        for context_id, surface_host, relpath, path_kind, validation_status, policy_state, _hash in surfaces
    }
    for row in contexts:
        (
            context_id,
            name,
            description,
            covers_json,
            relpath,
            expected_hash,
            package_hash,
            validation,
            _validation_errors_json,
            stored_publication,
            stored_publication_reason,
            _content_hash,
        ) = row
        covers = json.loads(covers_json)
        score = _score(query, name, description, covers)
        if score <= 0:
            continue
        live = live_by_name.get(name)
        publication = live["publication_status"] if live is not None else stored_publication
        publication_reason = live["publication_reason"] if live is not None else stored_publication_reason

        def exclude(reason: str) -> None:
            excluded.append(
                {
                    "name": name,
                    "reason": reason,
                    "publication_status": publication,
                    "publication_reason": publication_reason,
                }
            )

        if live is None:
            observed_drift = True
            exclude("missing_package")
            continue
        live_surface = next((surface for surface in live["surfaces"] if surface["host"] == host), None)
        stored_surface = stored_surfaces.get((context_id, host))
        if live_surface is None or stored_surface is None:
            observed_drift = True
            exclude("surface_not_snapshotted")
            continue
        if live_surface["validation_status"] != "correct":
            exclude(f"surface_{live_surface['validation_status']}")
            continue
        if live_surface["policy_state"] != "on":
            exclude(f"policy_{live_surface['policy_state'].replace('-', '_')}")
            continue
        if any(
            stored_surface[field] != live_surface[field]
            for field in ("surface_relpath", "path_kind", "validation_status")
        ):
            observed_drift = True
            exclude("stale_surface")
            continue
        if stored_surface["policy_state"] != live_surface["policy_state"]:
            observed_drift = True
            exclude("stale_policy")
            continue
        if validation != "valid" or live["validation_status"] != "valid":
            exclude("invalid")
            continue
        path, observed, body = _live_skill(root, relpath)
        if path is None:
            observed_drift = True
            exclude(observed)
            continue
        live_package = validate_skill_package(path.parent)
        if observed != expected_hash:
            observed_drift = True
            exclude("stale_skill")
            continue
        if live_package.get("package_sha256") != package_hash:
            observed_drift = True
            exclude("stale_package")
            continue
        if live_package.get("valid") is not True:
            exclude("invalid")
            continue
        try:
            context = (body or b"").decode("utf-8-sig")
        except UnicodeDecodeError:
            exclude("invalid")
            continue
        matches.append(
            {
                "name": name,
                "description": description,
                "covers": covers,
                "score": score,
                "skill_sha256": expected_hash,
                "skill_md_relpath": relpath,
                "host": host,
                "policy_state": live_surface["policy_state"],
                "publication_status": publication,
                "publication_reason": publication_reason,
                "context": context,
            }
        )
    matches.sort(key=lambda item: (-item["score"], item["name"].casefold()))
    matches = matches[:limit]
    return {
        "status": "OK",
        "available": True,
        "current": live_inventory_hash == sync[5] and not orphan_surfaces and not observed_drift,
        "query": query,
        "host": host,
        "sync_id": sync[0],
        "count": len(matches),
        "matches": matches,
        "excluded": excluded,
    }


def context_status(args: Any) -> dict[str, Any]:
    """SK8: compare the current persisted snapshot with a fresh read-only package inventory."""
    try:
        root = _store_root(args)
        sync, contexts, surfaces = _current_rows()
    except KaizenDenied as error:
        if error.code not in {
            "DENIED_SKILL_STORE_NOT_FOUND",
            "DENIED_SKILL_CONTEXT_SCHEMA_REQUIRED",
            "DENIED_SKILL_CONTEXT_INTEGRITY",
        }:
            raise
        return {
            "status": "OK",
            "available": False,
            "current": False,
            "skill_count": 0,
            "surface_count": 0,
            "reason": error.code,
            "required_action": error.fields.get("required_action"),
        }
    if sync is None:
        return {
            "status": "OK",
            "available": False,
            "current": False,
            "skill_count": 0,
            "surface_count": 0,
            "reason": "no_applied_snapshot",
            "required_action": "run SK6 --action plan, review it, then apply the confirmed snapshot",
        }
    hosts = _hosts(args)
    stored_hosts = json.loads(sync[8])
    host_scope_drift = stored_hosts != list(hosts)
    normalized_records, orphan_surfaces, surface_conflicts, live_inventory_hash = _discover_snapshot(root, hosts)
    live_records = {record["name"]: record for record in normalized_records}
    stored_names = {row[1] for row in contexts}
    fresh = stale = missing = invalid = 0
    details: list[dict[str, str]] = []
    for row in contexts:
        (
            _context_id,
            name,
            _description,
            _covers_json,
            relpath,
            expected_hash,
            package_hash,
            validation,
            _validation_errors_json,
            stored_publication,
            _stored_publication_reason,
            _hash,
        ) = row
        live = live_records.get(name)
        path, observed, _body = _live_skill(root, relpath)
        state = "fresh"
        if live is not None and live["validation_status"] != "valid":
            state = "invalid"
            invalid += 1
        elif path is None or live is None:
            state = "missing"
            missing += 1
        elif (
            validation != live["validation_status"]
            or observed != expected_hash
            or live["package_sha256"] != package_hash
        ):
            state = "stale"
            stale += 1
        else:
            fresh += 1
        details.append(
            {
                "name": name,
                "state": state,
                "publication_status": live["publication_status"] if live is not None else stored_publication,
            }
        )
    unsynced = sorted(set(live_records) - stored_names, key=str.casefold)
    invalid += sum(live_records[name]["validation_status"] != "valid" for name in unsynced)
    context_names = {row[0]: row[1] for row in contexts}
    stored_surface_states = {
        (context_names[context_id], host): (relpath, path_kind, status)
        for context_id, host, relpath, path_kind, status, _policy_state, _content_hash in surfaces
        if context_id in context_names and host in hosts
    }
    live_surface_states = {
        (name, surface["host"]): (
            surface["surface_relpath"],
            surface["path_kind"],
            surface["validation_status"],
        )
        for name, record in live_records.items()
        for surface in record["surfaces"]
    }
    surface_drift = sum(
        stored_surface_states.get(key) != live_surface_states.get(key)
        for key in set(stored_surface_states) | set(live_surface_states)
    )
    stored_policy_states = {
        (context_names[context_id], host): policy_state
        for context_id, host, _relpath, _path_kind, _status, policy_state, _content_hash in surfaces
        if context_id in context_names and host in hosts
    }
    live_policy_states = {
        (name, surface["host"]): surface["policy_state"]
        for name, record in live_records.items()
        for surface in record["surfaces"]
    }
    policy_drift = sum(
        stored_policy_states.get(key) != live_policy_states.get(key)
        for key in set(stored_policy_states) | set(live_policy_states)
    )
    stored_publication_states = {row[1]: (row[9], row[10]) for row in contexts}
    live_publication_states = {
        name: (record["publication_status"], record["publication_reason"])
        for name, record in live_records.items()
    }
    publication_drift = sum(
        stored_publication_states.get(name) != live_publication_states.get(name)
        for name in set(stored_publication_states) | set(live_publication_states)
    )
    policy_counts = {state: 0 for state in _POLICY_STATES}
    for state in live_policy_states.values():
        policy_counts[state] += 1
    publication_counts = {state: 0 for state in _PUBLICATION_STATUSES}
    for status in live_publication_states.values():
        publication_counts[status[0]] += 1
    automatic_context_surfaces = sum(
        record["validation_status"] == "valid"
        and surface["validation_status"] == "correct"
        and surface["policy_state"] == "on"
        for record in live_records.values()
        for surface in record["surfaces"]
    )
    surface_counts = {state: 0 for state in sorted(_SURFACE_STATUSES)}
    for record in live_records.values():
        for surface in record["surfaces"]:
            surface_counts[surface["validation_status"]] += 1
    return {
        "status": "OK",
        "available": True,
        "current": live_inventory_hash == sync[5] and not host_scope_drift and not orphan_surfaces,
        "sync_id": sync[0],
        "plan_sha256": sync[4],
        "inventory_hash": sync[5],
        "observed_inventory_hash": live_inventory_hash,
        "inventory_hash_match": live_inventory_hash == sync[5],
        "hosts": stored_hosts,
        "skill_count": len(contexts),
        "surface_count": len(surfaces),
        "fresh": fresh,
        "stale": stale,
        "missing": missing,
        "invalid": invalid,
        "validation_healthy": invalid == 0,
        "surface_drift": surface_drift,
        "surface_counts": surface_counts,
        "policy_drift": policy_drift,
        "publication_drift": publication_drift,
        "policy_counts": policy_counts,
        "publication_counts": publication_counts,
        "automatic_context_surfaces": automatic_context_surfaces,
        "policy_restricted_surfaces": sum(policy_counts[state] for state in _POLICY_STATES if state != "on"),
        "host_scope_drift": host_scope_drift,
        "orphan_surface_count": len(orphan_surfaces),
        "orphan_surfaces": orphan_surfaces,
        "surface_conflict_count": len(surface_conflicts),
        "surface_conflicts": surface_conflicts,
        "unsynced": unsynced,
        "skills": details,
    }
