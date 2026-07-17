"""Offline-safe installer and resolver for the pinned Claude Agent SDK worker.

The provider package is source material.  A usable runtime exists only after an
audited npm lock is installed into the local runtime root and every recorded
artifact is revalidated.  This module never performs credential discovery.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SDK_PACKAGE = "@anthropic-ai/claude-agent-sdk"
SDK_VERSION = "0.3.207"
ZOD_VERSION = "4.4.3"
TYPESCRIPT_VERSION = "5.9.3"
NODE_TYPES_VERSION = "22.20.1"
WORKER_PROTOCOL = 1
RUNTIME_KIND = "claude-agent-sdk"
RUNTIME_RELATIVE = Path("AI") / "work" / "orchestration" / "provider-runtime" / "claude-agent"
SOURCE_RELATIVE = Path("vendor_workers") / "claude_agent"
INTEGRITY_FILE = "runtime-integrity.json"
CURRENT_FILE = "current.json"
MAX_JSON_BYTES = 1 << 20
PARTIAL_MAX_AGE_SECONDS = 24 * 60 * 60
_BLOCKED_RUNTIME_ENV_EXACT = frozenset({
    "NODE_OPTIONS", "NODE_PATH", "SENTRY_DSN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
})
_BLOCKED_NPM_ENV_EXACT = frozenset({
    "ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED",
})
_NPM_REGISTRY = "https://registry.npmjs.org/"
_NATIVE_PACKAGES = tuple(
    f"@anthropic-ai/claude-agent-sdk-{os_id}-{arch}"
    for os_id in ("darwin", "linux", "win32")
    for arch in ("arm64", "x64")
)
_INSTALLER_LOCK_TIMEOUT_SECONDS = 30.0
_TARGET_NAME_RE = re.compile(r"^[0-9a-f]{64}-(?:darwin|linux|win32)-(?:arm64|x64)$")
_SOURCE_IDENTITY_DOMAIN = b"agent-kaizen.claude-runtime.source-v1\0"
_SOURCE_INPUTS = ("package.json", "package-lock.json", "runtime-manifest.json", "tsconfig.json", "worker.ts")
_INSTALLER_THREAD_LOCKS: dict[str, threading.Lock] = {}
_INSTALLER_THREAD_LOCKS_GUARD = threading.Lock()


class ClaudeRuntimeError(RuntimeError):
    """Sanitized runtime failure exposing stable code and field attributes; code defaults to DENIED_SDK_UNAVAILABLE."""

    def __init__(self, code: str = "DENIED_SDK_UNAVAILABLE", field: str = "runtime") -> None:
        super().__init__(f"{code}: {field}")
        self.code = code
        self.field = field


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _blocked_runtime_env_key(key: str) -> bool:
    upper = key.upper()
    return (
        upper in _BLOCKED_RUNTIME_ENV_EXACT
        or upper.startswith("OTEL_")
        or "CREDENTIAL" in upper
        or upper.endswith((
            "_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN", "_SESSION_TOKEN", "_BEARER_TOKEN",
            "_PASSWORD", "_SECRET", "_SECRET_KEY", "_TOKEN",
        ))
    )


def _blocked_npm_env_key(key: str) -> bool:
    upper = key.upper()
    return (
        _blocked_runtime_env_key(key)
        or upper in _BLOCKED_NPM_ENV_EXACT
        or upper.startswith("NPM_CONFIG_")
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_identity(source: Path, *, field: str) -> str:
    """Content-address every allowlisted input that can change the built worker."""
    digest = hashlib.sha256(_SOURCE_IDENTITY_DOMAIN)
    for name in _SOURCE_INPUTS:
        path = _regular_file(source / name, field)
        digest.update(name.encode("utf-8") + b"\0" + bytes.fromhex(_sha256(path)))
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    """Hash names, types, link-target text, and bytes; reject escaping symlink targets and every unsupported entry type."""

    if not root.is_dir() or _is_reparse(root, field="installed_packages"):
        raise ClaudeRuntimeError(field="installed_packages")
    resolved_root = root.resolve()
    digest = hashlib.sha256()
    try:
        entries = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    except OSError as error:
        raise ClaudeRuntimeError(field="installed_packages") from error
    for entry in entries:
        relative = entry.relative_to(root).as_posix().encode("utf-8")
        try:
            info = entry.lstat()
        except OSError as error:
            raise ClaudeRuntimeError(field="installed_packages") from error
        if entry.is_symlink():
            resolved = entry.resolve()
            if not _contained(resolved, resolved_root):
                raise ClaudeRuntimeError(field="installed_packages")
            digest.update(b"L\0" + relative + b"\0" + os.readlink(entry).encode("utf-8") + b"\0")
        elif stat.S_ISDIR(info.st_mode):
            digest.update(b"D\0" + relative + b"\0")
        elif stat.S_ISREG(info.st_mode):
            digest.update(b"F\0" + relative + b"\0" + str(info.st_size).encode("ascii") + b"\0")
            digest.update(_sha256(entry).encode("ascii") + b"\0")
        else:
            raise ClaudeRuntimeError(field="installed_packages")
    return digest.hexdigest()


def _is_reparse(path: Path, *, field: str = "runtime_integrity", info: os.stat_result | None = None) -> bool:
    try:
        info = info or path.lstat()
    except OSError as error:
        raise ClaudeRuntimeError(field=field) from error
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _regular_file(path: Path, field: str, *, nonempty: bool = True) -> Path:
    try:
        info = path.lstat()
    except OSError as error:
        raise ClaudeRuntimeError(field=field) from error
    if _is_reparse(path, field=field, info=info) or not stat.S_ISREG(info.st_mode) or (nonempty and info.st_size < 1):
        raise ClaudeRuntimeError(field=field)
    return path


def _read_json(path: Path, field: str) -> dict[str, Any]:
    _regular_file(path, field)
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_JSON_BYTES:
            raise ClaudeRuntimeError(field=field)
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClaudeRuntimeError(field=field) from error
    if not isinstance(value, dict):
        raise ClaudeRuntimeError(field=field)
    return value


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    payload = json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            pass
        else:
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _contained(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _installer_thread_lock(root: Path) -> threading.Lock:
    key = str(root.resolve())
    if os.name == "nt":
        key = key.casefold()
    with _INSTALLER_THREAD_LOCKS_GUARD:
        return _INSTALLER_THREAD_LOCKS.setdefault(key, threading.Lock())


def _try_file_lock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_file_lock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _installer_lock(root: Path, *, timeout: float = _INSTALLER_LOCK_TIMEOUT_SECONDS):
    """Hold per-root thread and process locks or raise a runtime_lock denial on timeout."""
    deadline = time.monotonic() + timeout
    thread_lock = _installer_thread_lock(root)
    if not thread_lock.acquire(timeout=max(0.0, timeout)):
        raise ClaudeRuntimeError(field="runtime_lock")
    handle = None
    descriptor: int | None = None
    locked = False
    try:
        lock_path = root / ".install.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            handle = os.fdopen(descriptor, "r+b", buffering=0)
            descriptor = None
            path_info = lock_path.lstat()
            if (
                _is_reparse(lock_path, field="runtime_lock", info=path_info)
                or not stat.S_ISREG(path_info.st_mode)
                or not os.path.samestat(os.fstat(handle.fileno()), path_info)
            ):
                raise ClaudeRuntimeError(field="runtime_lock")
            if path_info.st_size < 1:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, ValueError) as error:
            raise ClaudeRuntimeError(field="runtime_lock") from error
        while True:
            try:
                _try_file_lock(handle)
                locked = True
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise ClaudeRuntimeError(field="runtime_lock") from error
                if time.monotonic() >= deadline:
                    raise ClaudeRuntimeError(field="runtime_lock") from error
                time.sleep(0.05)
        yield
    finally:
        if handle is not None:
            if locked:
                try:
                    _release_file_lock(handle)
                except OSError:
                    pass
            handle.close()
        elif descriptor is not None:
            os.close(descriptor)
        thread_lock.release()


def _runtime_root(repo_root_or_runtime_root: str | os.PathLike[str]) -> Path:
    candidate = Path(repo_root_or_runtime_root).expanduser().resolve()
    if candidate.name == "claude-agent" or (candidate / CURRENT_FILE).exists():
        return candidate
    if (candidate / "kaizen.py").is_file() or (candidate / "AI").is_dir():
        return (candidate / RUNTIME_RELATIVE).resolve()
    return candidate


def source_package_root() -> Path:
    return (Path(__file__).resolve().parent / SOURCE_RELATIVE).resolve()


def _require_non_system_runtime(root: Path) -> None:
    """On Windows, require a concrete drive distinct from the OS system drive."""
    if os.name != "nt":
        return
    system_drive = os.environ.get("SystemDrive", "").rstrip("\\/").casefold()
    drive = root.drive.rstrip("\\/").casefold()
    if not drive or (system_drive and drive == system_drive):
        raise ClaudeRuntimeError(field="runtime_root")


def _platform_id(system: str | None = None, machine: str | None = None) -> tuple[str, str]:
    raw_system = (system or sys.platform).casefold()
    if raw_system.startswith("win"):
        os_id = "win32"
    elif raw_system.startswith("linux"):
        os_id = "linux"
    elif raw_system.startswith("darwin") or raw_system.startswith("mac"):
        os_id = "darwin"
    else:
        raise ClaudeRuntimeError(field="platform")
    raw_machine = (machine or platform.machine()).casefold()
    if raw_machine in {"amd64", "x86_64", "x64"}:
        arch = "x64"
    elif raw_machine in {"aarch64", "arm64"}:
        arch = "arm64"
    else:
        raise ClaudeRuntimeError(field="architecture")
    return os_id, arch


def _source_manifest(source: Path) -> dict[str, Any]:
    manifest = _read_json(source / "runtime-manifest.json", "runtime_manifest")
    expected = {
        "schema": 1,
        "worker_protocol": WORKER_PROTOCOL,
        "sdk_package": SDK_PACKAGE,
        "sdk_version": SDK_VERSION,
        "zod_version": ZOD_VERSION,
        "worker_entry": "dist/worker.js",
        "native_binary_required": True,
        "lifecycle_scripts_allowed": False,
        "max_frame_bytes": 1_048_576,
        "max_delta_bytes": 32_768,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ClaudeRuntimeError(field="runtime_manifest")
    tools = manifest.get("tools")
    if tools != [
        "kaizen_read_file", "kaizen_list_files", "kaizen_search_text",
        "kaizen_run_process", "kaizen_propose_changes",
    ]:
        raise ClaudeRuntimeError(field="runtime_manifest.tools")
    return manifest


def _validate_lock_package(package_path: object, entry: object) -> dict[str, Any]:
    if not isinstance(package_path, str) or not isinstance(entry, dict):
        raise ClaudeRuntimeError(field="package_lock.packages")
    parts = package_path.split("/")
    if (
        not parts
        or parts[0] != "node_modules"
        or any(part in {"", ".", ".."} for part in parts)
        or "\\" in package_path
        or entry.get("link") is True
        or not isinstance(entry.get("version"), str)
    ):
        raise ClaudeRuntimeError(field="package_lock.source")
    resolved = entry.get("resolved")
    if not isinstance(resolved, str):
        raise ClaudeRuntimeError(field="package_lock.source")
    parsed = urlsplit(resolved)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "registry.npmjs.org"
        or not parsed.path.startswith("/")
        or not parsed.path.endswith(".tgz")
        or parsed.query
        or parsed.fragment
    ):
        raise ClaudeRuntimeError(field="package_lock.source")
    integrity = entry.get("integrity")
    if not isinstance(integrity, str) or not integrity.startswith("sha512-") or len(integrity.split()) != 1:
        raise ClaudeRuntimeError(field="package_lock.integrity")
    try:
        digest = base64.b64decode(integrity.removeprefix("sha512-"), validate=True)
    except ValueError as error:
        raise ClaudeRuntimeError(field="package_lock.integrity") from error
    if len(digest) != 64:
        raise ClaudeRuntimeError(field="package_lock.integrity")
    return entry


def _lock_contract(source: Path) -> tuple[Path, str]:
    lock_path = source / "package-lock.json"
    lock = _read_json(lock_path, "package_lock")
    if lock.get("lockfileVersion") != 3:
        raise ClaudeRuntimeError(field="package_lock.version")
    if (
        lock.get("name") != "@agent-kaizen/claude-agent-worker"
        or lock.get("version") != "0.1.0"
        or lock.get("requires") is not True
    ):
        raise ClaudeRuntimeError(field="package_lock.root")
    packages = lock.get("packages")
    if not isinstance(packages, dict) or not isinstance(packages.get(""), dict):
        raise ClaudeRuntimeError(field="package_lock.packages")
    root = packages[""]
    if root.get("name") != "@agent-kaizen/claude-agent-worker" or root.get("version") != "0.1.0":
        raise ClaudeRuntimeError(field="package_lock.root")
    dependencies = root.get("dependencies")
    if dependencies != {SDK_PACKAGE: SDK_VERSION, "zod": ZOD_VERSION}:
        raise ClaudeRuntimeError(field="package_lock.dependencies")
    if root.get("devDependencies") != {"@types/node": NODE_TYPES_VERSION, "typescript": TYPESCRIPT_VERSION}:
        raise ClaudeRuntimeError(field="package_lock.dev_dependencies")
    for package_path, entry in packages.items():
        if package_path:
            _validate_lock_package(package_path, entry)
    for package_path, version in (
        ("node_modules/@anthropic-ai/claude-agent-sdk", SDK_VERSION),
        ("node_modules/zod", ZOD_VERSION),
        ("node_modules/typescript", TYPESCRIPT_VERSION),
        ("node_modules/@types/node", NODE_TYPES_VERSION),
    ):
        entry = packages.get(package_path)
        if not isinstance(entry, dict) or entry.get("version") != version:
            raise ClaudeRuntimeError(field="package_lock.packages")
    for package in _NATIVE_PACKAGES:
        entry = packages.get(f"node_modules/{package}")
        if not isinstance(entry, dict) or entry.get("version") != SDK_VERSION or entry.get("optional") is not True:
            raise ClaudeRuntimeError(field="package_lock.native_packages")
    return lock_path, _sha256(lock_path)


def _native_relative(os_id: str, arch: str) -> Path:
    package = f"claude-agent-sdk-{os_id}-{arch}"
    executable = "claude.exe" if os_id == "win32" else "claude"
    return Path("node_modules") / "@anthropic-ai" / package / executable


def _run(runner: Runner, argv: list[str], *, cwd: Path, env: Mapping[str, str], timeout: int,
         field: str) -> None:
    try:
        result = runner(argv, cwd=str(cwd), env=dict(env), capture_output=True, text=True,
                        timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise ClaudeRuntimeError(field=field) from error
    if not isinstance(result, subprocess.CompletedProcess) or result.returncode != 0:
        raise ClaudeRuntimeError(field=field)


def _npm_environment(runtime_root: Path) -> dict[str, str]:
    npm_root = runtime_root / "_npm"
    cache, temporary, config, prefix = (
        npm_root / "cache", npm_root / "temp", npm_root / "config", npm_root / "prefix",
    )
    for directory in (cache, temporary, config, prefix):
        directory.mkdir(parents=True, exist_ok=True)
    env = {str(key): str(value) for key, value in os.environ.items()
           if not _blocked_npm_env_key(str(key))}
    inherited_path = next((value for key, value in env.items() if key == "PATH"), None)
    if inherited_path is None:
        inherited_path = next((value for key, value in env.items() if key.casefold() == "path"), "")
    env = {key: value for key, value in env.items() if key.casefold() != "path"}
    if inherited_path:
        env["PATH"] = inherited_path
    env.update({
        "TEMP": str(temporary),
        "TMP": str(temporary),
        "TMPDIR": str(temporary),
        "NPM_CONFIG_CACHE": str(cache),
        "NPM_CONFIG_USERCONFIG": str(config / "user.npmrc"),
        "NPM_CONFIG_GLOBALCONFIG": str(config / "global.npmrc"),
        "NPM_CONFIG_PREFIX": str(prefix),
        "NPM_CONFIG_REGISTRY": _NPM_REGISTRY,
        "NPM_CONFIG_REPLACE_REGISTRY_HOST": "never",
        "NPM_CONFIG_STRICT_SSL": "true",
        "NPM_CONFIG_IGNORE_SCRIPTS": "true",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_AUDIT": "false",
        "NODE_REPL_HISTORY": str(temporary / "node_repl_history"),
    })
    return env


def cleanup_abandoned_partials(repo_root_or_runtime_root: str | os.PathLike[str], *,
                               now: float | None = None) -> list[str]:
    """Remove only direct, older-than-24h installer stages from the selected runtime root."""

    root = _runtime_root(repo_root_or_runtime_root)
    if not root.exists():
        return []
    root = root.resolve()
    removed: list[str] = []
    cutoff = (time.time() if now is None else now) - PARTIAL_MAX_AGE_SECONDS
    for entry in root.iterdir():
        if not entry.name.startswith(".partial-") or _is_reparse(entry, field="runtime_cleanup"):
            continue
        try:
            modified = entry.stat().st_mtime
        except OSError:
            continue
        resolved = entry.resolve()
        if modified < cutoff and resolved.parent == root and _contained(resolved, root):
            if entry.is_dir():
                shutil.rmtree(entry)
            elif entry.is_file():
                entry.unlink()
            removed.append(entry.name)
    return sorted(removed)


def _validate_target(
    target: Path,
    *,
    expected_lock_hash: str | None = None,
    expected_source_identity: str | None = None,
) -> dict[str, Any]:
    """Verify schema/version pins, file and tree hashes, node identity, and source-derived target name."""
    if _is_reparse(target, field="runtime_target") or not target.is_dir():
        raise ClaudeRuntimeError(field="runtime_target")
    integrity = _read_json(target / INTEGRITY_FILE, "runtime_integrity")
    required = {
        "schema", "runtime_kind", "sdk_version", "zod_version", "worker_protocol",
        "lock_sha256", "manifest_sha256", "package_json_sha256", "worker_sha256",
        "native_relative", "native_sha256", "node_modules_sha256", "node_executable", "node_sha256",
    }
    if set(integrity) != required or integrity.get("schema") != 1:
        raise ClaudeRuntimeError(field="runtime_integrity")
    if integrity.get("runtime_kind") != RUNTIME_KIND or integrity.get("sdk_version") != SDK_VERSION:
        raise ClaudeRuntimeError(field="runtime_integrity")
    if integrity.get("zod_version") != ZOD_VERSION or integrity.get("worker_protocol") != WORKER_PROTOCOL:
        raise ClaudeRuntimeError(field="runtime_integrity")
    if expected_lock_hash is not None and integrity.get("lock_sha256") != expected_lock_hash:
        raise ClaudeRuntimeError(field="runtime_integrity")
    source_identity = _source_identity(target, field="runtime_integrity")
    if expected_source_identity is not None and source_identity != expected_source_identity:
        raise ClaudeRuntimeError(field="runtime_integrity")
    if _TARGET_NAME_RE.fullmatch(target.name) is not None and target.name[:64] != source_identity:
        raise ClaudeRuntimeError(field="runtime_integrity")
    recorded = {
        "package-lock.json": integrity["lock_sha256"],
        "runtime-manifest.json": integrity["manifest_sha256"],
        "package.json": integrity["package_json_sha256"],
        "dist/worker.js": integrity["worker_sha256"],
        str(integrity["native_relative"]): integrity["native_sha256"],
    }
    for relative, digest in recorded.items():
        if not isinstance(digest, str) or len(digest) != 64:
            raise ClaudeRuntimeError(field="runtime_integrity")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ClaudeRuntimeError(field="runtime_integrity")
        candidate = (target / relative_path).resolve()
        if not _contained(candidate, target.resolve()):
            raise ClaudeRuntimeError(field="runtime_integrity")
        path = _regular_file(candidate, "runtime_integrity")
        if _sha256(path) != digest:
            raise ClaudeRuntimeError(field="runtime_integrity")
    node_path = Path(str(integrity["node_executable"]))
    _require_non_system_runtime(node_path)
    node = _regular_file(node_path, "node_executable")
    if not node.is_absolute() or _sha256(node) != integrity["node_sha256"]:
        raise ClaudeRuntimeError(field="node_executable")
    node_modules_digest = integrity.get("node_modules_sha256")
    if not isinstance(node_modules_digest, str) or _tree_sha256(target / "node_modules") != node_modules_digest:
        raise ClaudeRuntimeError(field="installed_packages")
    for package_name, expected in ((SDK_PACKAGE, SDK_VERSION), ("zod", ZOD_VERSION)):
        package_json = target / "node_modules" / Path(package_name) / "package.json"
        if _read_json(package_json, "installed_package").get("version") != expected:
            raise ClaudeRuntimeError(field="installed_package")
    return integrity


def _read_pointer(root: Path) -> dict[str, Any]:
    """Validate the exact pointer shape and target names without checking target existence."""
    pointer = _read_json(root / CURRENT_FILE, "runtime_pointer")
    allowed = {"schema", "active", "previous"}
    if set(pointer) != allowed or pointer.get("schema") != 1:
        raise ClaudeRuntimeError(field="runtime_pointer")
    for field in ("active", "previous"):
        value = pointer.get(field)
        if value is not None and (not isinstance(value, str) or _TARGET_NAME_RE.fullmatch(value) is None):
            raise ClaudeRuntimeError(field="runtime_pointer")
    if not isinstance(pointer.get("active"), str):
        raise ClaudeRuntimeError(field="runtime_pointer")
    return pointer


def validate_runtime(
    repo_root_or_runtime_root: str | os.PathLike[str],
    *,
    source_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    root = _runtime_root(repo_root_or_runtime_root)
    pointer = _read_pointer(root)
    target = (root / pointer["active"]).resolve()
    if target.parent != root.resolve():
        raise ClaudeRuntimeError(field="runtime_pointer")
    expected_source_identity = None if source_root is None else _source_identity(
        Path(source_root).expanduser().resolve(), field="runtime_source",
    )
    integrity = _validate_target(target, expected_source_identity=expected_source_identity)
    return {"root": root, "target": target, "pointer": pointer, "integrity": integrity}


def runtime_capability(repo_root_or_runtime_root: str | os.PathLike[str]) -> dict[str, object]:
    """Return a path-free capability descriptor suitable for loopback/UI responses."""

    try:
        validated = validate_runtime(repo_root_or_runtime_root)
    except ClaudeRuntimeError as error:
        return {
            "runtime_kind": RUNTIME_KIND,
            "runtime_version": SDK_VERSION,
            "runtime_status": "unavailable",
            "worker_protocol": WORKER_PROTOCOL,
            "code": error.code,
        }
    return {
        "runtime_kind": RUNTIME_KIND,
        "runtime_version": validated["integrity"]["sdk_version"],
        "runtime_status": "ready",
        "worker_protocol": validated["integrity"]["worker_protocol"],
    }


def resolve_worker_command(repo_root_or_runtime_root: str | os.PathLike[str]) -> list[str]:
    """Resolve only a fully validated active runtime into an owned-child argv."""

    validated = validate_runtime(repo_root_or_runtime_root)
    node = str(validated["integrity"]["node_executable"])
    worker = str(validated["target"] / "dist" / "worker.js")
    return [node, worker]


def _quarantine_corrupt_target(runtime_root: Path, target: Path) -> None:
    """Rename a validated in-root corrupt target to a partial quarantine, or no-op if absent."""
    try:
        target.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ClaudeRuntimeError(field="runtime_target") from error
    if _is_reparse(target, field="runtime_target") or _TARGET_NAME_RE.fullmatch(target.name) is None:
        raise ClaudeRuntimeError(field="runtime_target")
    resolved = target.resolve()
    if resolved.parent != runtime_root.resolve():
        raise ClaudeRuntimeError(field="runtime_target")
    quarantine = runtime_root / f".partial-corrupt-{os.getpid()}-{secrets.token_hex(8)}"
    try:
        os.replace(target, quarantine)
    except OSError as error:
        raise ClaudeRuntimeError(field="runtime_target") from error


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ClaudeRuntimeError(field="runtime_target") from error


def _install_runtime_locked(
    repo_root_or_runtime_root: str | os.PathLike[str],
    *,
    node_executable: str | os.PathLike[str],
    npm_executable: str | os.PathLike[str],
    source_root: str | os.PathLike[str] | None = None,
    runner: Runner = subprocess.run,
    system: str | None = None,
    machine: str | None = None,
) -> dict[str, object]:
    """Install while the caller holds _installer_lock; validate/quarantine a corrupt active target, roll current.json back to a valid previous target when possible, then stage and promote without package updates or lock generation."""

    runtime_root = _runtime_root(repo_root_or_runtime_root)
    _require_non_system_runtime(runtime_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    cleanup_abandoned_partials(runtime_root)
    source = (Path(source_root).resolve() if source_root is not None else source_package_root())
    _source_manifest(source)
    lock_path, lock_hash = _lock_contract(source)
    package_json = _read_json(source / "package.json", "package_json")
    if package_json.get("dependencies") != {SDK_PACKAGE: SDK_VERSION, "zod": ZOD_VERSION}:
        raise ClaudeRuntimeError(field="package_json.dependencies")
    if package_json.get("devDependencies") != {"@types/node": NODE_TYPES_VERSION,
                                                "typescript": TYPESCRIPT_VERSION}:
        raise ClaudeRuntimeError(field="package_json.dev_dependencies")
    source_identity = _source_identity(source, field="runtime_source")
    node = _regular_file(Path(node_executable).expanduser().resolve(), "node_executable")
    npm = _regular_file(Path(npm_executable).expanduser().resolve(), "npm_executable")
    _require_non_system_runtime(node)
    _require_non_system_runtime(npm)
    os_id, arch = _platform_id(system, machine)
    target_name = f"{source_identity}-{os_id}-{arch}"
    target = runtime_root / target_name
    previous: str | None = None
    pointer: dict[str, Any] | None = None
    try:
        pointer = _read_pointer(runtime_root)
    except ClaudeRuntimeError:
        pass
    active_valid = False
    prior_target: Path | None = None
    valid_previous: str | None = None
    if pointer is not None:
        prior_target = (runtime_root / pointer["active"]).resolve()
        if prior_target.parent == runtime_root.resolve():
            try:
                _validate_target(prior_target)
                active_valid = True
            except ClaudeRuntimeError:
                pass
        prior_previous = pointer.get("previous")
        if isinstance(prior_previous, str):
            rollback_target = (runtime_root / prior_previous).resolve()
            if rollback_target.parent == runtime_root.resolve():
                try:
                    _validate_target(rollback_target)
                    valid_previous = prior_previous
                except ClaudeRuntimeError:
                    pass
        if active_valid:
            previous = pointer["active"] if pointer["active"] != target_name else valid_previous
        else:
            if valid_previous is not None:
                _write_json_atomic(runtime_root / CURRENT_FILE, {
                    "schema": 1,
                    "active": valid_previous,
                    "previous": None,
                })
                previous = valid_previous
            if prior_target.parent == runtime_root.resolve():
                _quarantine_corrupt_target(runtime_root, prior_target)
    pointer_previous = previous if previous != target_name else None
    if _entry_exists(target):
        try:
            integrity = _validate_target(
                target, expected_lock_hash=lock_hash, expected_source_identity=source_identity,
            )
        except ClaudeRuntimeError:
            _quarantine_corrupt_target(runtime_root, target)
        else:
            _write_json_atomic(runtime_root / CURRENT_FILE, {
                "schema": 1,
                "active": target_name,
                "previous": pointer_previous,
            })
            return {"status": "ready", "warm": True, "runtime": target_name,
                    "sdk_version": integrity["sdk_version"]}
    stage = runtime_root / f".partial-{os.getpid()}-{secrets.token_hex(8)}"
    stage.mkdir()
    for name in ("package.json", "package-lock.json", "runtime-manifest.json", "tsconfig.json", "worker.ts"):
        _regular_file(source / name, f"source.{name}")
        shutil.copy2(source / name, stage / name)
    if _source_identity(stage, field="runtime_source") != source_identity:
        raise ClaudeRuntimeError(field="runtime_source")
    env = _npm_environment(runtime_root)
    inherited_path = env.get("PATH", "")
    env["PATH"] = str(node.parent) if not inherited_path else os.pathsep.join((str(node.parent), inherited_path))
    npm_argv = [
        str(npm), "ci", "--include=optional", "--ignore-scripts", "--no-audit", "--no-fund",
        f"--registry={_NPM_REGISTRY}", "--replace-registry-host=never",
    ]
    _run(runner, npm_argv, cwd=stage, env=env, timeout=900, field="npm_ci")
    compiler = stage / "node_modules" / "typescript" / "bin" / "tsc"
    if _read_json(stage / "node_modules" / "typescript" / "package.json",
                  "typescript_package").get("version") != TYPESCRIPT_VERSION:
        raise ClaudeRuntimeError(field="typescript_package")
    _regular_file(compiler, "typescript_compiler")
    _run(runner, [str(node), str(compiler), "-p", str(stage / "tsconfig.json")], cwd=stage,
         env=env, timeout=120, field="worker_build")
    worker = _regular_file(stage / "dist" / "worker.js", "worker_build")
    native_relative = _native_relative(os_id, arch)
    native = _regular_file(stage / native_relative, "native_binary")
    native_package = native.parent / "package.json"
    if _read_json(native_package, "native_package").get("version") != SDK_VERSION:
        raise ClaudeRuntimeError(field="native_package")
    import_script = (
        "import('@anthropic-ai/claude-agent-sdk').then(m=>{"
        "if(typeof m.query!=='function'||typeof m.createSdkMcpServer!=='function'||typeof m.tool!=='function')"
        "process.exit(3)}).catch(()=>process.exit(2))"
    )
    _run(runner, [str(node), "--input-type=module", "--eval", import_script], cwd=stage,
         env=env, timeout=30, field="sdk_import")
    _run(runner, [str(npm), "prune", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"],
         cwd=stage, env=env, timeout=300, field="npm_prune")
    if (stage / "node_modules" / "typescript").exists() or (stage / "node_modules" / "@types" / "node").exists():
        raise ClaudeRuntimeError(field="npm_prune")
    _regular_file(stage / native_relative, "native_binary")
    _regular_file(stage / "dist" / "worker.js", "worker_build")
    _run(runner, [str(node), "--input-type=module", "--eval", import_script], cwd=stage,
         env=env, timeout=30, field="sdk_import")
    integrity = {
        "schema": 1,
        "runtime_kind": RUNTIME_KIND,
        "sdk_version": SDK_VERSION,
        "zod_version": ZOD_VERSION,
        "worker_protocol": WORKER_PROTOCOL,
        "lock_sha256": _sha256(stage / lock_path.name),
        "manifest_sha256": _sha256(stage / "runtime-manifest.json"),
        "package_json_sha256": _sha256(stage / "package.json"),
        "worker_sha256": _sha256(worker),
        "native_relative": native_relative.as_posix(),
        "native_sha256": _sha256(native),
        "node_modules_sha256": _tree_sha256(stage / "node_modules"),
        "node_executable": str(node),
        "node_sha256": _sha256(node),
    }
    _write_json_atomic(stage / INTEGRITY_FILE, integrity)
    _validate_target(
        stage, expected_lock_hash=lock_hash, expected_source_identity=source_identity,
    )
    try:
        os.replace(stage, target)
    except OSError as error:
        raise ClaudeRuntimeError(field="runtime_finalize") from error
    _validate_target(
        target, expected_lock_hash=lock_hash, expected_source_identity=source_identity,
    )
    _write_json_atomic(runtime_root / CURRENT_FILE, {
        "schema": 1,
        "active": target_name,
        "previous": pointer_previous,
    })
    return {"status": "ready", "warm": False, "runtime": target_name,
            "sdk_version": SDK_VERSION}


def install_runtime(
    repo_root_or_runtime_root: str | os.PathLike[str],
    *,
    node_executable: str | os.PathLike[str],
    npm_executable: str | os.PathLike[str],
    source_root: str | os.PathLike[str] | None = None,
    runner: Runner = subprocess.run,
    system: str | None = None,
    machine: str | None = None,
) -> dict[str, object]:
    """Install the audited lock exactly, or validate and reuse the warm runtime.

    No package update or lock generation occurs here. Missing locks, lifecycle
    scripts, native optionals, and integrity drift fail closed. One local and
    cross-process lock serializes cleanup through atomic pointer publication.
    """

    runtime_root = _runtime_root(repo_root_or_runtime_root)
    _require_non_system_runtime(runtime_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    with _installer_lock(runtime_root):
        try:
            return _install_runtime_locked(
                runtime_root,
                node_executable=node_executable,
                npm_executable=npm_executable,
                source_root=source_root,
                runner=runner,
                system=system,
                machine=machine,
            )
        except Exception:
            # The installer lock excludes a second same-process stage. Remove only this process's
            # unpublished build directories; aged cleanup remains the backstop for hard termination.
            prefix = f".partial-{os.getpid()}-"
            for entry in runtime_root.iterdir():
                if entry.name.startswith(prefix):
                    try:
                        if not _is_reparse(entry, field="runtime_cleanup") and entry.is_dir():
                            shutil.rmtree(entry)
                    except (OSError, ClaudeRuntimeError):
                        pass
            raise


__all__ = [
    "ClaudeRuntimeError", "NODE_TYPES_VERSION", "RUNTIME_KIND", "SDK_PACKAGE", "SDK_VERSION",
    "TYPESCRIPT_VERSION", "WORKER_PROTOCOL", "ZOD_VERSION", "cleanup_abandoned_partials",
    "install_runtime", "resolve_worker_command",
    "runtime_capability", "source_package_root", "validate_runtime",
]
