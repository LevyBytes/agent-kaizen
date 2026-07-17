"""Aggregate read-only validation status for SK2 and operator diagnostics."""

from __future__ import annotations

import os

from .core import SCHEMA_VERSION
from .discovery import discover_skills
from .indexing import index_status


def validation_status(
    project_root: str | os.PathLike[str],
    store_root: str | os.PathLike[str],
    hosts: tuple[str, ...] | list[str] = ("codex", "claude"),
) -> dict[str, object]:
    """Validate packages and report their hashes, link states, and host indexes."""
    inventory = discover_skills(project_root, store_root, hosts)
    indexes = index_status(project_root, hosts)
    package_errors = [
        {"name": row["name"], "errors": row["errors"]}
        for row in inventory["skills"]
        if not row["valid"]
    ]
    link_conflicts: list[dict[str, object]] = []
    for row in inventory["skills"]:
        for host, surface in row["hosts"].items():
            if surface["status"] in {"wrong_target", "dangling_link", "real_directory"}:
                link_conflicts.append(
                    {
                        "name": row["name"],
                        "host": host,
                        "status": surface["status"],
                        "path": surface["path"],
                    }
                )
    for orphan in inventory["orphan_surfaces"]:
        link_conflicts.append(
            {
                "name": orphan.get("name"),
                "host": orphan.get("host"),
                "status": orphan.get("status"),
                "path": orphan.get("path"),
                "orphan": True,
                "reason": "surface entry is absent from the authoritative skill store",
            }
        )
    stale_indexes = [
        {"host": row["host"], "status": row["status"], "path": row["path"]}
        for row in indexes["hosts"]
        if row["status"] not in {"up_to_date", "surface_missing"}
    ]
    errors = list(inventory["scan_errors"]) + list(indexes["errors"])
    valid = bool(inventory["complete"]) and not package_errors and not link_conflicts and not stale_indexes and not errors
    return {
        "schema_version": SCHEMA_VERSION,
        "valid": valid,
        "complete": inventory["complete"],
        "scan_errors": inventory["scan_errors"],
        "packages": inventory["skills"],
        "orphan_surfaces": inventory["orphan_surfaces"],
        "package_errors": package_errors,
        "link_conflicts": link_conflicts,
        "indexes": indexes["hosts"],
        "stale_indexes": stale_indexes,
        "errors": sorted(set(str(error) for error in errors)),
    }
