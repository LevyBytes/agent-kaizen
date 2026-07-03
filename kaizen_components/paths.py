from __future__ import annotations

import os
from pathlib import Path

from .denials import KaizenDenied


# REPO_ROOT anchors the whole data plane (db, work, exports, manifests). By default it is THIS package's
# own repo. A project that LINKS this engine via a junction (so .resolve() would point here, the master)
# sets KAIZEN_REPO_ROOT to keep its OWN data plane local. Unset => unchanged master behavior; set => every
# derived path below follows it. This is what lets many projects share the engine but NOT the data plane
# (and, as an edge case, deliberately share one data plane by pointing several projects at one root).
_ENV_ROOT = os.environ.get("KAIZEN_REPO_ROOT")
REPO_ROOT = Path(_ENV_ROOT).resolve() if _ENV_ROOT else Path(__file__).resolve().parents[1]
AI_ROOT = REPO_ROOT / "AI"
DB_ROOT = AI_ROOT / "db"
DB_PATH = DB_ROOT / "kaizen.db"
WORK_ROOT = AI_ROOT / "work"
RUNTIME_ROOT = WORK_ROOT / "kaizen-runtime"
RUNTIME_TMP = RUNTIME_ROOT / "tmp"
EXPORT_ROOT = DB_ROOT / "exports"
MANIFEST_ROOT = DB_ROOT / "manifests"
# Generated assets (e.g. ComfyUI outputs). Under AI/ so AI/.gitignore's `*` keeps binaries out of git.
GENERATED_ROOT = AI_ROOT / "generated"


def ensure_runtime_dirs() -> None:
    for path in (DB_ROOT, WORK_ROOT, RUNTIME_ROOT, RUNTIME_TMP, EXPORT_ROOT, MANIFEST_ROOT, GENERATED_ROOT):
        path.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(RUNTIME_TMP)
    os.environ["TMP"] = str(RUNTIME_TMP)
    os.environ["TMPDIR"] = str(RUNTIME_TMP)
    os.environ["SQLITE_TMPDIR"] = str(RUNTIME_TMP)


def repo_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def assert_under(root: Path, target: Path) -> Path:
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    if target_resolved != root_resolved and root_resolved not in target_resolved.parents:
        raise ValueError(f"path is outside allowed root: {target_resolved}")
    return target_resolved


def operation_task_dir(name: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip().lower())
    path = WORK_ROOT / (safe or "kaizen-task")
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_text_file(raw_path: str, *, encoding: str = "utf-8-sig") -> str:
    """Read a user-supplied input file, denying cleanly when it is missing.

    Used for ``--summary-file`` / ``--body-file`` / ``--payload-json-file`` inputs so a
    bad path returns a structured ``DENIED_FILE_NOT_FOUND`` instead of a raw traceback.
    """
    path = Path(raw_path)
    if not path.is_file():
        raise KaizenDenied(
            "DENIED_FILE_NOT_FOUND",
            {"path": str(path), "required_action": "pass a path to an existing file"},
            exit_code=2,
        )
    return path.read_text(encoding=encoding)
