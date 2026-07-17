"""Generated host skill indexes with read-only status/plan and confirmed apply."""

from __future__ import annotations

import collections
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from .core import (
    HOST_SURFACES,
    SCHEMA_VERSION,
    SkillManagementError,
    assert_under,
    atomic_write_bytes,
    atomic_write_text,
    finalize_plan,
    is_directory_link,
    link_target,
    path_text,
    require_confirmation,
    require_hosts,
    require_plan_clean,
    resolved,
    same_path,
    sha256_bytes,
    sha256_file,
    surface_root,
)
from .discovery import iter_skill_dirs
from .validation import parse_frontmatter, validate_skill_package


_STOP_WORDS = {"and", "the", "for", "with", "use", "using", "a", "an", "to", "of", "in", "on", "or"}


def _index_target_error(path: Path) -> str | None:
    """Return why a generated INDEX.md target is unsafe to replace."""
    if not os.path.lexists(path):
        return None
    if path.is_symlink():
        return f"generated INDEX.md must not be a link: {path}"
    if not path.is_file():
        return f"generated INDEX.md must be a regular file: {path}"
    return None


def _markdown_inline(value: object) -> str:
    """Collapse and escape untrusted metadata for one Markdown inline field."""
    text = " ".join(str(value or "").split())
    for raw, escaped in (("\\", "\\\\"), ("|", "\\|"), ("`", "\\`"), ("[", "\\["), ("]", "\\]"), ("*", "\\*")):
        text = text.replace(raw, escaped)
    return text


def derive_covers(skill: str | os.PathLike[str]) -> list[str]:
    """Derive covered entities from router children or flat-skill topic keywords."""
    root = resolved(skill)
    children = [child.name for child in iter_skill_dirs(root)]
    if children:
        return children
    terms: collections.Counter[str] = collections.Counter()
    topics = root / "references" / "topics.json"
    if topics.is_file():
        try:
            value = json.loads(topics.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            value = {"topics": []}
        rows = value.get("topics", []) if isinstance(value, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            keywords = row.get("keywords", [])
            for keyword in keywords if isinstance(keywords, list) else []:
                term = str(keyword).replace("_", "-").strip().lower()
                if term and term not in _STOP_WORDS and 3 <= len(term) <= 30 and term.count("-") <= 3 and not term.isdigit():
                    terms[term] += 1
    return [term for term, _ in terms.most_common()]


def _authoring_record(skill: Path) -> dict[str, object]:
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    frontmatter, _ = parse_frontmatter(text)
    derived = derive_covers(skill)
    covers = frontmatter.get("covers") or derived
    if not isinstance(covers, list):
        covers = []
    description = str(frontmatter.get("description", ""))
    trigger = re.split(r"(?<=[.])\s", description, 1)[0] if description else ""
    return {
        "name": str(frontmatter.get("name", skill.name)),
        "directory": skill.name,
        "description": description,
        "trigger": trigger[:200],
        "covers": [str(item) for item in covers],
        "derived_covers": derived,
    }


def _master_text_from_records(records: list[dict[str, object]], intro: str) -> str:
    infos = {str(row["directory"]): row for row in records}
    entity_to_skills: dict[str, set[str]] = collections.defaultdict(set)
    for name, row in infos.items():
        for entity in row["derived_covers"]:
            entity_to_skills[str(entity)].add(name)
    related: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for owners in entity_to_skills.values():
        if len(owners) < 2:
            continue
        for left in owners:
            for right in owners:
                if left != right:
                    related[left][right] += 1
    lines = [
        "# Skills — Master Index",
        "",
        intro,
        "",
        "## Skills",
        "",
        "| Skill | Covers (top) | Trigger |",
        "| --- | --- | --- |",
    ]
    for name in sorted(infos):
        row = infos[name]
        covers = [_markdown_inline(item) for item in row["covers"]]
        cover_text = ", ".join(covers[:8]) + (" …" if len(covers) > 8 else "")
        trigger = _markdown_inline(row["trigger"])
        lines.append(f"| [{_markdown_inline(name)}]({quote(name, safe='-._~')}/SKILL.md) | {cover_text} | {trigger} |")
    lines.extend(["", "## Related skills", "", "Skills that share covered entities:", ""])
    for name in sorted(related):
        ranked = sorted(related[name].items(), key=lambda item: (-item[1], item[0].casefold()))[:6]
        peers = ", ".join(f"{_markdown_inline(peer)} ({count})" for peer, count in ranked)
        lines.append(f"- **{_markdown_inline(name)}** ↔ {peers}")
    if not related:
        lines.append("- (no shared entities across skills)")
    shared = {entity: sorted(owners) for entity, owners in entity_to_skills.items() if len(owners) > 1}
    lines.extend(
        [
            "",
            "## Entity → skill map (shared)",
            "",
            "Entities documented by more than one skill (where to look, and overlaps):",
            "",
            "| Entity | Skills |",
            "| --- | --- |",
        ]
    )
    for entity in sorted(shared):
        lines.append(f"| {_markdown_inline(entity)} | {', '.join(_markdown_inline(name) for name in shared[entity])} |")
    if not shared:
        lines.append("| (none) | |")
    return "\n".join(lines).rstrip() + "\n"


def build_master_text(skills_root: str | os.PathLike[str]) -> str:
    """Build the rich authoring master index used by skill-drafting compatibility shims."""
    root = resolved(skills_root)
    if not root.is_dir():
        raise SkillManagementError(f"skills root does not exist: {root}")
    records = [_authoring_record(skill) for skill in iter_skill_dirs(root)]
    intro = "Catalog of every skill in this folder, with the entities/domains each covers and which skills are related. This is a discovery and audit entry point; agents still route via each skill's `description`. Generated by Agent Kaizen's skill manager."
    return _master_text_from_records(records, intro)


def _seeded_skill_text(text: str, covers: list[str]) -> str:
    match = re.match(r"^(---\n)(.*?)(\n---\n)", text, re.DOTALL)
    if not match:
        raise SkillManagementError("SKILL.md has no writable YAML frontmatter")
    lines = match.group(2).split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        current = re.match(r"^covers:\s*(\S.*)?$", lines[index])
        if not current:
            kept.append(lines[index])
            index += 1
            continue
        index += 1
        if not current.group(1):
            while index < len(lines) and re.match(r"^\s+-\s", lines[index]):
                index += 1
    body = "\n".join(kept).rstrip("\n")
    block = "covers:\n" + "\n".join(f"  - {cover}" for cover in covers)
    return match.group(1) + body + "\n" + block + match.group(3) + text[match.end():]


def _scan_index_skill_dirs(root: Path) -> tuple[list[Path], list[str], bool]:
    """Enumerate an authoring or host-surface root without treating valid package links as escapes."""
    if not root.is_dir():
        return [], [f"skills root does not exist or is not a directory: {root}"], False
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        return [], [f"cannot enumerate skills root {root}: {type(exc).__name__}: {exc}"], False
    candidates: list[Path] = []
    errors: list[str] = []
    seen_names: set[str] = set()
    seen_targets: dict[str, str] = {}
    for child in children:
        if child.name.startswith(".") or child.name in {"INDEX.md", "__pycache__", "node_modules"}:
            continue
        name_key = child.name.casefold()
        if name_key in seen_names:
            errors.append(f"duplicate skill entry name: {child.name}")
            continue
        seen_names.add(name_key)
        if is_directory_link(child):
            target = link_target(child)
            if target is None or not target.is_dir():
                errors.append(f"dangling or unreadable skill package link: {child}")
                continue
        elif child.is_dir():
            try:
                target = child.resolve(strict=True)
            except OSError as exc:
                errors.append(f"cannot resolve skill package {child}: {type(exc).__name__}: {exc}")
                continue
        else:
            errors.append(f"unsupported entry in skills root: {child}")
            continue
        skill_md = target / "SKILL.md"
        if not skill_md.is_file():
            errors.append(f"skill package is missing SKILL.md: {child}")
            continue
        try:
            skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"cannot read skill package {child}: {type(exc).__name__}: {exc}")
            continue
        target_key = os.path.normcase(str(target.resolve(strict=True)))
        prior_name = seen_targets.get(target_key)
        if prior_name is not None:
            errors.append(f"duplicate skill package target: {prior_name} and {child.name} -> {target}")
            continue
        seen_targets[target_key] = child.name
        candidates.append(child)
    return candidates, errors, not errors


def _hard_validation_errors(record: dict[str, object]) -> list[str]:
    """Return structural/read-safety failures; ordinary semantic findings remain SK2 diagnostics."""
    prefixes = (
        "package links are not allowed:",
        "duplicate frontmatter key:",
        "unsupported frontmatter line ",
        "frontmatter `name` must ",
        "frontmatter name ",
        "frontmatter `description` must ",
        "SKILL.md is not UTF-8:",
        "cannot read ",
        "cannot enumerate package path:",
        "missing SKILL.md",
        "skill package directory does not exist",
    )
    hard = [str(error) for error in record.get("errors", []) if str(error).startswith(prefixes)]
    for child in record.get("subskills", []):
        if isinstance(child, dict):
            hard.extend(_hard_validation_errors(child))
    return sorted(set(hard))


def _host_surface_identity(root: Path) -> tuple[Path, str] | None:
    """Return ``(project, host)`` when ``root`` is a canonical project skill surface."""
    for host, parts in HOST_SURFACES.items():
        if root.name == parts[-1] and root.parent.name == parts[0]:
            return root.parent.parent, host
    return None


def build_store_index_plan(
    skills_root: str | os.PathLike[str],
    mirror_root: str | os.PathLike[str] | None = None,
    seed_covers: bool = False,
    flat_cap: int = 12,
) -> dict[str, object]:
    """Plan rich authoring INDEX.md output and optional covers seeding/mirroring."""
    root = resolved(skills_root)
    mirror = resolved(mirror_root) if mirror_root is not None else None
    root_surface = _host_surface_identity(root)
    errors: list[str] = []
    operations: list[dict[str, object]] = []
    if not root.is_dir():
        errors.append(f"skills root does not exist: {root}")
    if mirror is not None and not mirror.is_dir():
        errors.append(f"mirror skills root does not exist: {mirror}")
    if flat_cap < 1:
        errors.append("flat_cap must be at least 1")
    skill_dirs, scan_errors, _complete = _scan_index_skill_dirs(root) if root.is_dir() else ([], [], False)
    errors.extend(scan_errors)
    if mirror is not None and mirror.is_dir():
        _mirror_dirs, mirror_errors, _mirror_complete = _scan_index_skill_dirs(mirror)
        errors.extend(mirror_errors)
    records: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    planned_targets: set[str] = set()
    package_names: set[str] = set()
    for skill in skill_dirs:
        validation = validate_skill_package(skill)
        if not validation["valid"]:
            if root_surface is None:
                diagnostics.append({"skill": skill.name, "errors": list(validation["errors"])})
            hard_errors = _hard_validation_errors(validation)
            if hard_errors:
                errors.append(f"skill `{skill.name}` has unsafe structural failures: " + "; ".join(hard_errors))
                continue
        try:
            record = _authoring_record(skill)
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"cannot read {skill / 'SKILL.md'}: {type(exc).__name__}: {exc}")
            continue
        package_name = str(record["name"])
        package_name_key = package_name.casefold()
        if package_name_key in package_names:
            errors.append(f"duplicate package frontmatter name: {package_name}")
            continue
        package_names.add(package_name_key)
        if package_name != skill.name:
            errors.append(f"package frontmatter name does not match surfaced entry: {package_name} != {skill.name}")
            continue
        derived = list(record["derived_covers"])
        is_router = bool(list(iter_skill_dirs(skill)))
        seeded = derived if is_router else derived[:flat_cap]
        if seed_covers and validation["valid"]:
            record["covers"] = seeded
            for target_root in (root, mirror):
                if target_root is None:
                    continue
                skill_md = target_root / skill.name / "SKILL.md"
                if not skill_md.is_file():
                    continue
                identity = os.path.normcase(str(skill_md.resolve(strict=False)))
                if identity in planned_targets:
                    continue
                planned_targets.add(identity)
                try:
                    before_text = skill_md.read_text(encoding="utf-8")
                    after_text = _seeded_skill_text(before_text, seeded)
                except (OSError, UnicodeDecodeError, SkillManagementError) as exc:
                    errors.append(f"cannot seed {skill_md}: {exc}")
                    continue
                before_hash = sha256_bytes(before_text.encode("utf-8"))
                after_hash = sha256_bytes(after_text.encode("utf-8"))
                if before_hash != after_hash:
                    operations.append(
                        {
                            "op": "write_covers",
                            "skill": skill.name,
                            "path": path_text(skill_md),
                            "before_sha256": before_hash,
                            "after_sha256": after_hash,
                            "content": after_text,
                            "resolved_path": path_text(skill_md.resolve(strict=True)),
                        }
                    )
        records.append(record)
    intro = "Catalog of every skill in this folder, with the entities/domains each covers and which skills are related. This is a discovery and audit entry point; agents still route via each skill's `description`. Generated by Agent Kaizen's skill manager."
    index_text = _master_text_from_records(records, intro)
    for target_root in (root, mirror):
        if target_root is None or not target_root.is_dir():
            continue
        target_text = index_text
        target_count = len(records)
        target_surface = _host_surface_identity(target_root)
        if target_surface is not None:
            target_project, target_host = target_surface
            host_records, host_errors, host_diagnostics, present = _surface_records(target_project, target_host)
            errors.extend(host_errors)
            diagnostics.extend(host_diagnostics)
            if not present:
                errors.append(f"{target_host} skill surface is absent: {target_root}")
                continue
            target_text = render_index(target_host, host_records)
            target_count = len(host_records)
        index_path = target_root / "INDEX.md"
        target_error = _index_target_error(index_path)
        if target_error:
            errors.append(target_error)
            continue
        identity = os.path.normcase(path_text(index_path))
        if identity in planned_targets:
            continue
        planned_targets.add(identity)
        before_hash = sha256_file(index_path)
        after_hash = sha256_bytes(target_text.encode("utf-8"))
        if before_hash != after_hash:
            operations.append(
                {
                    "op": "write_index",
                    "path": path_text(index_path),
                    "before_sha256": before_hash,
                    "after_sha256": after_hash,
                    "content": target_text,
                    "skill_count": target_count,
                    "managed_root": path_text(target_root),
                    "resolved_path": path_text(index_path.resolve(strict=False)),
                }
            )
    operations.sort(key=lambda row: (str(row["path"]).casefold(), str(row["op"])))
    return finalize_plan(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "skill-store-index",
            "skills_root": path_text(root),
            "mirror_root": path_text(mirror) if mirror is not None else None,
            "seed_covers": bool(seed_covers),
            "flat_cap": int(flat_cap),
            "operations": operations,
            "diagnostics": [
                json.loads(value)
                for value in sorted({json.dumps(row, ensure_ascii=False, sort_keys=True) for row in diagnostics})
            ],
            "errors": sorted(set(errors)),
        }
    )


def apply_store_index_plan(
    skills_root: str | os.PathLike[str],
    mirror_root: str | os.PathLike[str] | None = None,
    seed_covers: bool = False,
    flat_cap: int = 12,
    confirm_plan: str | None = None,
) -> dict[str, object]:
    """Recompute, confirm, and apply a rich authoring-index plan atomically."""
    plan = build_store_index_plan(skills_root, mirror_root, seed_covers, flat_cap)
    require_confirmation(plan, confirm_plan)
    require_plan_clean(plan)
    roots = [resolved(skills_root)]
    if mirror_root is not None:
        roots.append(resolved(mirror_root))
    originals: list[tuple[Path, bytes | None]] = []
    try:
        for operation in plan["operations"]:
            lexical_path = Path(os.path.abspath(str(operation["path"])))
            if not any(lexical_path == root or root in lexical_path.parents for root in roots):
                raise SkillManagementError(f"planned authoring-index path escapes managed roots: {lexical_path}")
            if operation["op"] == "write_index":
                managed_root = resolved(str(operation.get("managed_root", "")))
                if not any(same_path(managed_root, root) for root in roots):
                    raise SkillManagementError(f"planned INDEX.md has an unknown managed root: {managed_root}")
                expected_path = managed_root / "INDEX.md"
                if not same_path(lexical_path, expected_path):
                    raise SkillManagementError(f"planned INDEX.md is not the managed root target: {lexical_path}")
                target_error = _index_target_error(lexical_path)
                if target_error:
                    raise SkillManagementError(target_error)
                if not same_path(lexical_path.parent.resolve(strict=True), managed_root):
                    raise SkillManagementError(f"managed INDEX.md parent changed after planning: {lexical_path.parent}")
                path = lexical_path
            else:
                path = resolved(lexical_path)
            expected_resolved = operation.get("resolved_path")
            if expected_resolved is not None and path_text(path.resolve(strict=False)) != expected_resolved:
                raise SkillManagementError(f"authoring-index package target changed after planning: {lexical_path}")
            before = path.read_bytes() if path.is_file() else None
            current_hash = sha256_bytes(before) if before is not None else ""
            if current_hash != operation["before_sha256"]:
                raise SkillManagementError(f"authoring-index input changed after planning: {path}")
            originals.append((path, before))
            atomic_write_text(path, str(operation["content"]))
            if sha256_file(path) != operation["after_sha256"]:
                raise SkillManagementError(f"authoring-index verification failed: {path}")
    except Exception as exc:
        rollback_errors: list[str] = []
        for path, before in reversed(originals):
            try:
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write_bytes(path, before)
            except Exception as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        suffix = "" if not rollback_errors else "; rollback failures: " + "; ".join(rollback_errors)
        raise SkillManagementError(f"authoring-index apply failed: {exc}{suffix}") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "OK",
        "action": "apply",
        "kind": "skill-store-index",
        "plan_sha256": plan["plan_sha256"],
        "applied": len(plan["operations"]),
        "restart_required": bool(plan["operations"]),
        "operations": plan["operations"],
    }


# Naming retained for callers that use "authoring index" rather than "store index".
build_authoring_index_plan = build_store_index_plan
apply_authoring_index_plan = apply_store_index_plan


def _surface_records(
    project: Path,
    host: str,
) -> tuple[list[dict[str, object]], list[str], list[dict[str, object]], bool]:
    records: list[dict[str, object]] = []
    errors: list[str] = []
    diagnostics: list[dict[str, object]] = []
    root = surface_root(project, host)
    if not os.path.lexists(root):
        return records, errors, diagnostics, False
    if not root.is_dir():
        return records, [f"{host} skill surface is not a readable directory: {root}"], diagnostics, True
    skill_dirs, scan_errors, _complete = _scan_index_skill_dirs(root)
    errors.extend(scan_errors)
    for skill_dir in skill_dirs:
        validation = validate_skill_package(skill_dir)
        name = skill_dir.name
        if not validation["valid"]:
            diagnostics.append({"host": host, "skill": name, "errors": list(validation["errors"])})
            hard_errors = _hard_validation_errors(validation)
            if hard_errors:
                errors.append(f"{host}/{name} has unsafe structural failures: " + "; ".join(hard_errors))
                continue
        if str(validation["name"]) != name:
            errors.append(f"{host}/{name} package frontmatter name mismatch: {validation['name']}")
            continue
        derived_covers = derive_covers(skill_dir)
        records.append(
            {
                "name": name,
                "directory": name,
                "display_name": validation["display_name"],
                "description": validation["description"],
                "covers": validation["covers"] or derived_covers,
                "derived_covers": derived_covers,
                "trigger": re.split(r"(?<=[.])\s", str(validation["description"]), 1)[0][:200],
                "skill_md_sha256": validation["skill_md_sha256"],
                "valid": validation["valid"],
            }
        )
    records.sort(key=lambda row: str(row["name"]).casefold())
    return records, errors, diagnostics, True


def render_index(host: str, records: list[dict[str, object]]) -> str:
    """Render a deterministic, host-local master skill index."""
    surface = "/".join(HOST_SURFACES[host])
    intro = f"Catalog of the skill packages currently surfaced under `{surface}/`, with their covered entities and overlaps. Package validation and host policy remain authoritative; this file is a generated discovery view."
    return _master_text_from_records(records, intro)


def index_status(
    project_root: str | os.PathLike[str],
    hosts: tuple[str, ...] | list[str] = ("codex", "claude"),
) -> dict[str, object]:
    """Return whether each host INDEX.md matches its current surfaced skills."""
    project = resolved(project_root)
    selected = require_hosts(hosts)
    rows: list[dict[str, object]] = []
    all_errors: list[str] = []
    all_diagnostics: list[dict[str, object]] = []
    for host in selected:
        records, errors, diagnostics, present = _surface_records(project, host)
        all_errors.extend(errors)
        all_diagnostics.extend(diagnostics)
        index_path = surface_root(project, host) / "INDEX.md"
        if not present:
            rows.append(
                {
                    "host": host,
                    "path": path_text(index_path),
                    "status": "surface_missing",
                    "skill_count": 0,
                    "current_sha256": "",
                    "expected_sha256": "",
                    "skills": [],
                }
            )
            continue
        target_error = _index_target_error(index_path)
        if target_error:
            all_errors.append(target_error)
            rows.append(
                {
                    "host": host,
                    "path": path_text(index_path),
                    "status": "invalid",
                    "skill_count": len(records),
                    "current_sha256": "",
                    "expected_sha256": "",
                    "skills": records,
                }
            )
            continue
        if errors:
            rows.append(
                {
                    "host": host,
                    "path": path_text(index_path),
                    "status": "invalid",
                    "skill_count": len(records),
                    "current_sha256": sha256_file(index_path),
                    "expected_sha256": "",
                    "skills": records,
                }
            )
            continue
        expected = render_index(host, records)
        expected_hash = sha256_bytes(expected.encode("utf-8"))
        current_hash = sha256_file(index_path)
        state = "missing" if not index_path.is_file() else (
            "up_to_date" if current_hash == expected_hash else "stale"
        )
        rows.append(
            {
                "host": host,
                "path": path_text(index_path),
                "status": state,
                "skill_count": len(records),
                "current_sha256": current_hash,
                "expected_sha256": expected_hash,
                "skills": records,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "project_root": path_text(project),
        "hosts": rows,
        "diagnostics": all_diagnostics,
        "errors": sorted(set(all_errors)),
    }


def build_index_plan(
    project_root: str | os.PathLike[str],
    hosts: tuple[str, ...] | list[str] = ("codex", "claude"),
) -> dict[str, object]:
    """Build a read-only plan to refresh stale or absent host indexes."""
    project = resolved(project_root)
    selected = require_hosts(hosts)
    operations: list[dict[str, object]] = []
    errors: list[str] = []
    diagnostics: list[dict[str, object]] = []
    for host in selected:
        records, host_errors, host_diagnostics, present = _surface_records(project, host)
        errors.extend(host_errors)
        diagnostics.extend(host_diagnostics)
        if not present or host_errors:
            continue
        managed_root = surface_root(project, host)
        if is_directory_link(managed_root):
            errors.append(f"{host} skill surface must not redirect index writes: {managed_root}")
            continue
        try:
            assert_under(project, managed_root.resolve(strict=True))
        except (OSError, SkillManagementError) as exc:
            errors.append(f"{host} skill surface is not a safe managed root: {exc}")
            continue
        index_path = managed_root / "INDEX.md"
        target_error = _index_target_error(index_path)
        if target_error:
            errors.append(target_error)
            continue
        expected = render_index(host, records)
        before_hash = sha256_file(index_path)
        after_hash = sha256_bytes(expected.encode("utf-8"))
        if before_hash != after_hash:
            operations.append(
                {
                    "op": "write_index",
                    "host": host,
                    "path": path_text(index_path),
                    "before_sha256": before_hash,
                    "after_sha256": after_hash,
                    "content": expected,
                    "skill_count": len(records),
                    "managed_root": path_text(managed_root),
                    "resolved_path": path_text(index_path.resolve(strict=False)),
                }
            )
    return finalize_plan(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "skill-index",
            "project_root": path_text(project),
            "hosts": list(selected),
            "operations": operations,
            "diagnostics": diagnostics,
            "errors": sorted(set(errors)),
        }
    )


def apply_index_plan(
    project_root: str | os.PathLike[str],
    hosts: tuple[str, ...] | list[str] = ("codex", "claude"),
    confirm_plan: str | None = None,
) -> dict[str, object]:
    """Recompute, confirm, and atomically apply host INDEX.md changes."""
    plan = build_index_plan(project_root, hosts)
    require_confirmation(plan, confirm_plan)
    require_plan_clean(plan)
    project = resolved(project_root)
    originals: list[tuple[Path, bytes | None]] = []
    try:
        for operation in plan["operations"]:
            host = str(operation.get("host", ""))
            if host not in plan["hosts"]:
                raise SkillManagementError(f"index operation has an unplanned host: {host}")
            managed_root = surface_root(project, host)
            if is_directory_link(managed_root):
                raise SkillManagementError(f"{host} skill surface must not redirect index writes: {managed_root}")
            assert_under(project, managed_root.resolve(strict=True))
            if not same_path(managed_root, resolved(str(operation.get("managed_root", "")))):
                raise SkillManagementError(f"index managed root changed after planning: {managed_root}")
            lexical_path = Path(os.path.abspath(str(operation["path"])))
            if not same_path(lexical_path, managed_root / "INDEX.md"):
                raise SkillManagementError(f"index operation is not the exact {host} surface target: {lexical_path}")
            target_error = _index_target_error(lexical_path)
            if target_error:
                raise SkillManagementError(target_error)
            if not same_path(lexical_path.parent.resolve(strict=True), managed_root):
                raise SkillManagementError(f"index surface changed after planning: {lexical_path.parent}")
            expected_resolved = str(operation.get("resolved_path", ""))
            if path_text(lexical_path.resolve(strict=False)) != expected_resolved:
                raise SkillManagementError(f"index target changed after planning: {lexical_path}")
            path = lexical_path
            before = path.read_bytes() if path.is_file() else None
            current = sha256_bytes(before) if before is not None else ""
            if current != operation["before_sha256"]:
                raise SkillManagementError(f"index changed after planning: {path}")
            originals.append((path, before))
            atomic_write_text(path, str(operation["content"]))
            if sha256_file(path) != operation["after_sha256"]:
                raise SkillManagementError(f"index verification failed: {path}")
    except Exception as exc:
        rollback_errors: list[str] = []
        for path, before in reversed(originals):
            try:
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write_bytes(path, before)
            except Exception as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        suffix = "" if not rollback_errors else "; rollback failures: " + "; ".join(rollback_errors)
        raise SkillManagementError(f"index apply failed: {exc}{suffix}") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "OK",
        "action": "apply",
        "kind": "skill-index",
        "plan_sha256": plan["plan_sha256"],
        "applied": len(plan["operations"]),
        "restart_required": bool(plan["operations"]),
        "operations": plan["operations"],
    }
