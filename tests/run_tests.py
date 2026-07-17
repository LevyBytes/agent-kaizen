#!/usr/bin/env python3
"""Run explicit Python test lanes with every artifact confined beneath AI/work."""

from __future__ import annotations

import argparse
import os
import stat
import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True

from lane_manifest import LANE_DESCRIPTIONS, lane_modules


REPO_ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = REPO_ROOT / "AI" / "work"
DEFAULT_TEMP_BASE = WORK_ROOT / "test-temp"
PYCACHE_BASE = WORK_ROOT / "test-pycache"
TEST_ROOT = REPO_ROOT / "tests"

_DELETE = 0x00010000
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_INVALID_HANDLE_VALUE = -1
_FILE_DISPOSITION_INFO_EX = 21
_FILE_DISPOSITION_DELETE = 0x00000001
_FILE_DISPOSITION_POSIX_SEMANTICS = 0x00000002
_FILE_DISPOSITION_IGNORE_READONLY = 0x00000010


def _unlink_readonly_windows(path: Path) -> None:
    """Delete a readonly file on Windows 10 1709+ via FileDispositionInfoEx on handle close."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    set_information.restype = wintypes.BOOL
    close = kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handle = create_file(
        str(path), _DELETE | _FILE_READ_ATTRIBUTES, _FILE_SHARE_ALL,
        None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT, None,
    )
    if handle == ctypes.c_void_p(_INVALID_HANDLE_VALUE).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        flags = wintypes.DWORD(
            _FILE_DISPOSITION_DELETE | _FILE_DISPOSITION_POSIX_SEMANTICS | _FILE_DISPOSITION_IGNORE_READONLY
        )
        if not set_information(handle, _FILE_DISPOSITION_INFO_EX, ctypes.byref(flags), ctypes.sizeof(flags)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close(handle)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _is_descendant(parent: Path, child: Path) -> bool:
    try:
        return os.path.commonpath((os.path.abspath(parent), os.path.abspath(child))) == os.path.abspath(parent) and not _same_path(parent, child)
    except ValueError:
        return False


def _recover_readonly(candidate: Path):
    """Shutil.rmtree onexc handler; re-raises unless the failing path is a physical regular file under candidate, then clears readonly (delete-on-close on Windows) and retries."""
    def recover(function: object, raw_path: str, error: BaseException) -> None:
        path = Path(os.path.abspath(raw_path))
        if not isinstance(error, PermissionError) or not (_same_path(candidate, path) or _is_descendant(candidate, path)):
            raise error
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise error
        physical = Path(os.path.realpath(path))
        if not (_same_path(candidate, physical) or _is_descendant(candidate, physical)):
            raise error
        details = os.lstat(path)
        if stat.S_ISREG(details.st_mode) and os.name == "nt":
            _unlink_readonly_windows(path)
            return
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        function(raw_path)  # type: ignore[operator]

    return recover


def _reject_system_drive(candidate: Path) -> None:
    if os.name != "nt":
        return
    if candidate.drive.startswith("\\\\"):
        raise RuntimeError(f"test scratch must use a local Windows drive, not UNC storage: {candidate}")
    system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\/").casefold()
    if candidate.drive.rstrip("\\/").casefold() == system_drive:
        raise RuntimeError(f"test scratch must not use the Windows system drive: {candidate}")


def _assert_physical_directory(candidate: Path, label: str) -> None:
    if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
        raise RuntimeError(f"{label} must not be a link or junction: {candidate}")
    if not candidate.is_dir():
        raise RuntimeError(f"{label} must be a directory: {candidate}")
    physical = Path(os.path.realpath(candidate))
    if not _same_path(physical, candidate):
        raise RuntimeError(f"{label} must not traverse a link or junction: {candidate}")


def _create_physical_directory(candidate: Path, label: str) -> None:
    """Walks to nearest existing ancestor, asserts physical dir (no link/junction), mkdirs, re-asserts."""
    probe = candidate
    while not os.path.lexists(probe):
        if probe.parent == probe:
            raise RuntimeError(f"no existing ancestor for {label}: {candidate}")
        probe = probe.parent
    _assert_physical_directory(probe, f"{label} ancestor")
    candidate.mkdir(parents=True, exist_ok=True)
    _assert_physical_directory(candidate, label)


def _prepare_child(base: Path, name: str) -> Path:
    candidate = base / name
    if not _is_descendant(base, candidate):
        raise RuntimeError(f"test scratch escaped its base: {candidate}")
    if os.path.lexists(candidate):
        _assert_physical_directory(candidate, "existing test scratch")
        _assert_physical_directory(candidate, "existing test scratch before removal")
        shutil.rmtree(candidate, onexc=_recover_readonly(candidate))
    candidate.mkdir()
    _assert_physical_directory(candidate, "test scratch")
    return candidate


def _safe_cleanup(base: Path, candidate: Path) -> None:
    """Recursively removes scratch with readonly/junction/hardlink recovery; refuses to traverse links or targets outside candidate."""
    if not _is_descendant(base, candidate) or not os.path.lexists(candidate):
        return
    _assert_physical_directory(candidate, "test cleanup target")
    _assert_physical_directory(candidate, "test cleanup target before removal")
    shutil.rmtree(candidate, onexc=_recover_readonly(candidate))


def _lane_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the fast core lane by default or an explicit bounded test lane.",
        epilog="Targeted unittest selectors remain supported without --lane, for example: tests.test_schema.",
    )
    parser.add_argument(
        "--lane",
        action="append",
        choices=tuple(LANE_DESCRIPTIONS),
        help="explicit lane; repeat to combine lanes (slow/live/extension are never implicit)",
    )
    parser.add_argument("--list-lanes", action="store_true", help="list lane ownership and exit")
    return parser


def _resolve_test_selection(args: list[str]) -> tuple[list[str], str] | None:
    parser = _lane_parser()
    parsed, unittest_args = parser.parse_known_args(args)
    if parsed.list_lanes:
        if parsed.lane or unittest_args:
            parser.error("--list-lanes cannot be combined with a lane or unittest arguments")
        for name, description in LANE_DESCRIPTIONS.items():
            print(f"{name:9} {len(lane_modules(name)):3} selectors  {description}")
        return None
    if parsed.lane:
        if unittest_args:
            parser.error("--lane cannot be combined with raw unittest arguments; run a target directly instead")
        selected = sorted({module for name in parsed.lane for module in lane_modules(name)})
        return selected, "+".join(dict.fromkeys(parsed.lane))
    if unittest_args:
        return unittest_args, "explicit unittest selection"
    return list(lane_modules("core")), "core"


def _child_environment(selection_name: str) -> dict[str, str]:
    """Copy the host environment and honor live gates only for an explicit live lane."""
    env = dict(os.environ)
    if "live" not in selection_name.split("+"):
        env.pop("KAIZEN_RUN_LIVE", None)
    return env


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 12):
        raise RuntimeError("tests/run_tests.py requires Python 3.12+ for safe shutil.rmtree(onexc=...) cleanup")
    args = list(sys.argv[1:] if argv is None else argv)
    selection = _resolve_test_selection(args)
    if selection is None:
        return 0
    test_args, selection_name = selection
    configured = os.environ.get("KAIZEN_TEST_TEMP_ROOT", "").strip()
    if configured and not Path(configured).is_absolute():
        raise RuntimeError("KAIZEN_TEST_TEMP_ROOT must be absolute")
    temp_base = Path(os.path.abspath(configured or DEFAULT_TEMP_BASE))
    pycache_base = Path(os.path.abspath(PYCACHE_BASE))
    _reject_system_drive(temp_base)
    _reject_system_drive(pycache_base)
    if not _is_descendant(WORK_ROOT, temp_base):
        raise RuntimeError(f"test temp base escaped AI/work: {temp_base}")
    if not _is_descendant(WORK_ROOT, pycache_base):
        raise RuntimeError(f"test pycache base escaped AI/work: {pycache_base}")
    _create_physical_directory(temp_base, "test temp base")
    _create_physical_directory(pycache_base, "test pycache base")
    temp_root = _prepare_child(temp_base, f"python-suite-{os.getpid()}")
    pycache_root = _prepare_child(pycache_base, f"python-suite-{os.getpid()}")
    env = _child_environment(selection_name)
    env.update(
        {
            "KAIZEN_TEST_TEMP_ROOT": str(temp_root),
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
            "TMPDIR": str(temp_root),
            "PYTHONPYCACHEPREFIX": str(pycache_root),
            "PYTHONPATH": os.pathsep.join(filter(None, (str(TEST_ROOT), os.environ.get("PYTHONPATH", "")))),
        }
    )
    command = [sys.executable, "-m", "unittest"]
    command.extend(test_args)
    print(f"test selection: {selection_name} ({len(test_args)} selectors)", flush=True)
    try:
        return subprocess.run(command, cwd=REPO_ROOT, env=env, check=False).returncode
    finally:
        _safe_cleanup(temp_base, temp_root)
        _safe_cleanup(pycache_base, pycache_root)


if __name__ == "__main__":
    raise SystemExit(main())
