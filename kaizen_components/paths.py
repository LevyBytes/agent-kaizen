"""Define data-plane path anchors, repository-root overrides, and shared path-trust helpers."""

from __future__ import annotations

import os
import re
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
# v8 M9 fleet plane. fleet.db is the SEPARATE synced coordination DB (daemon-owned handle; append-only
# coord_events sync to the hub). It NEVER shares kaizen.db's schema/manifest and is disposable
# (re-bootstrappable from the hub). node_identity.json is machine-local and sync-excluded by
# construction (only fleet.db syncs); both live in gitignored AI/db next to kaizen.db.
FLEET_DB_PATH = DB_ROOT / "fleet.db"
NODE_IDENTITY_PATH = DB_ROOT / "node_identity.json"
WORK_ROOT = AI_ROOT / "work"
RUNTIME_ROOT = WORK_ROOT / "kaizen-runtime"
RUNTIME_TMP = RUNTIME_ROOT / "tmp"
EXPORT_ROOT = DB_ROOT / "exports"
MANIFEST_ROOT = DB_ROOT / "manifests"
# Generated assets (e.g. ComfyUI outputs). `AI/generation/` is whitelisted in AI/.gitignore and has its
# own .gitignore, so the folder stays in the repo tree while its binary contents are ignored.
GENERATED_ROOT = AI_ROOT / "generation"


def _temporary_root() -> Path:
    """Returns sanctioned temp root (RUNTIME_TMP default, else validated absolute KAIZEN_TEST_TEMP_ROOT under engine AI/work); raises RuntimeError on relative/escaping config. Note: strict resolves at 42-43 assume caller (ensure_runtime_dirs) has created dirs or the env dir pre-exists."""
    configured = os.environ.get("KAIZEN_TEST_TEMP_ROOT", "").strip()
    if not configured:
        return RUNTIME_TMP
    candidate = Path(configured)
    if not candidate.is_absolute():
        raise RuntimeError("KAIZEN_TEST_TEMP_ROOT must be absolute")
    resolved = candidate.resolve(strict=False)
    engine_work = (Path(__file__).resolve().parents[1] / "AI" / "work").resolve(strict=False)
    try:
        if os.path.commonpath((str(engine_work), str(resolved))) != str(engine_work):
            raise RuntimeError("KAIZEN_TEST_TEMP_ROOT must stay under the engine AI/work root")
    except ValueError as error:
        raise RuntimeError("KAIZEN_TEST_TEMP_ROOT must stay under the engine AI/work root") from error
    return resolved


def ensure_runtime_dirs() -> None:
    """Idempotently creates runtime dirs AND redirects TEMP/TMP/TMPDIR/SQLITE_TMPDIR process-global env — flag the side effect (relied on by loopback.py)."""
    temporary = _temporary_root()
    for path in (DB_ROOT, WORK_ROOT, RUNTIME_ROOT, temporary, EXPORT_ROOT, MANIFEST_ROOT, GENERATED_ROOT):
        path.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(temporary)
    os.environ["TMP"] = str(temporary)
    os.environ["TMPDIR"] = str(temporary)
    os.environ["SQLITE_TMPDIR"] = str(temporary)


def repo_relative(path: Path) -> str:
    """Repo-relative POSIX path for portable records; falls back to absolute resolved path when outside REPO_ROOT."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def assert_under(root: Path, target: Path) -> Path:
    """Returns resolved target iff equal to or contained by root (component-wise via Path.parents); ValueError otherwise; resolves symlinks first."""
    root_resolved = REPO_ROOT if root == REPO_ROOT else root.resolve()
    target_resolved = target.resolve()
    if target_resolved != root_resolved and root_resolved not in target_resolved.parents:
        raise ValueError(f"path is outside allowed root: {target_resolved}")
    return target_resolved


def path_in_repo(path: Path) -> bool:
    """Boolean containment of a path within REPO_ROOT."""
    try:
        path.resolve().relative_to(REPO_ROOT)
        return True
    except ValueError:
        return False


def resolve_user_path(
    raw_path: str | None,
    *,
    require_file: bool = True,
    repo_only: bool = True,
    allow_external_hint: bool = False,
) -> Path:
    """One path-trust policy for every ``--path``-taking op (A1/A2/E1/...).

    Repo-only by default: records must stay portable and free of machine paths, so a
    path outside REPO_ROOT (including ``../`` traversal) is denied unless the op
    explicitly opted out via ``repo_only=False`` (the ``--allow-external`` flag).
    ``allow_external_hint`` picks the denial wording for ops that HAVE that flag.
    """
    if not raw_path:
        raise KaizenDenied("DENIED_PATH_REQUIRED", {"required_action": "resubmit with --path"}, exit_code=2)
    path = Path(raw_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    path = path.resolve()
    if repo_only and path != REPO_ROOT and REPO_ROOT not in path.parents:
        hint = (
            ", or resubmit with --allow-external"
            if allow_external_hint
            else "; copy external evidence under AI/work/ first"
        )
        raise KaizenDenied(
            "DENIED_PATH_OUTSIDE_REPO",
            {
                "path": str(path),
                "required_action": f"reference a file inside the repository{hint}",
            },
            exit_code=2,
        )
    if require_file and not path.is_file():
        raise KaizenDenied("DENIED_FILE_NOT_FOUND", {"path": str(path)}, exit_code=1)
    return path


def operation_task_dir(name: str) -> Path:
    """Slugifies name to an fs-safe segment under WORK_ROOT (fallback kaizen-task), creates and returns it; traversal chars neutralized."""
    safe = re.sub(r"-+", "-", "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip().lower()))
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
    try:
        return path.read_text(encoding=encoding)
    except UnicodeDecodeError as error:
        raise KaizenDenied(
            "DENIED_FILE_NOT_UTF8",
            {"path": str(path), "required_action": "re-encode the file as UTF-8 and resubmit"},
            exit_code=2,
        ) from error
