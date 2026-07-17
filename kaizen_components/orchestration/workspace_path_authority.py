"""Handle-anchored authority for bounded workspace file operations.

The authority deliberately binds the lexical workspace path instead of resolving it.  POSIX
operations are relative to ``dir_fd`` handles opened with ``O_NOFOLLOW``.  Windows keeps a
non-delete-shared handle for every directory from the volume root through the workspace and for
every operation-local parent, rejecting reparse points and verifying each handle's final path.

This is not a general adversarial compare-and-swap filesystem.  Windows proposal-file promotion
proves the target while excluding writers, disposes that exact handle, and then uses no-clobber
promotion; a failure after disposition retains the caller-journaled staged file and requires
recovery.  POSIX proposal promotion is intentionally narrower: no-clobber creation is supported,
but existing-file promotion is refused before target mutation because portable stdlib APIs cannot
enforce the same proof boundary.  ``atomic_replace`` is a separate crash-atomic primitive solely
for Kaizen-owned private single-writer registry state.

This module is intentionally independent of the writer registry and proposal executor.  Those
callers may compose it without creating a second ownership or recovery protocol.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


MAX_AUTHORITY_BYTES = 64 * 1024 * 1024
MAX_RELATIVE_PARTS = 128
MAX_RELATIVE_BYTES = 4096
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REPARSE_POINT = 0x400


class WorkspacePathError(RuntimeError):
    """A workspace path or file identity could not be proven safely."""

    code = "DENIED_WORKSPACE_PATH_AUTHORITY"


class _WindowsHandleError(WorkspacePathError):
    def __init__(self, message: str, winerror: int) -> None:
        super().__init__(f"{message} ({winerror})")
        self.winerror = winerror


class _WorkspaceRecoveryRequired(WorkspacePathError):
    def __init__(self, message: str, staged_parts: tuple[str, ...]) -> None:
        super().__init__(message)
        self.staged_relative = Path(*staged_parts).as_posix()


@dataclass(frozen=True)
class FileIdentity:
    """Stable identity returned by this authority and accepted by ``atomic_replace``."""

    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class StableRead:
    """Purpose (proven read result) and field meaning; the `size == len(data) == identity.size` invariant re-checked in `_expected_target` (L889)."""
    data: bytes
    identity: FileIdentity
    sha256: str
    size: int


class WorkspaceProcessLock:
    """One nonblocking OS byte-range/flock held until explicit close."""

    def __init__(self, handle: object) -> None:
        self._handle = handle
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            from .claude_runtime import _release_file_lock

            _release_file_lock(handle)
        finally:
            handle.close()


def _identity(info: os.stat_result) -> FileIdentity:
    return FileIdentity(
        device=int(info.st_dev),
        inode=int(info.st_ino),
        size=int(info.st_size),
        mtime_ns=int(info.st_mtime_ns),
    )


def _regular(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not (
        int(getattr(info, "st_file_attributes", 0)) & _REPARSE_POINT
    )


def _directory(info: os.stat_result) -> bool:
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not (
        int(getattr(info, "st_file_attributes", 0)) & _REPARSE_POINT
    )


def _bound(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= MAX_AUTHORITY_BYTES):
        raise WorkspacePathError("operation byte bound is invalid")
    return value


def _relative_parts(value: str | os.PathLike[str]) -> tuple[str, ...]:
    """Security contract: rejects abs/anchor/empty/`.`/`..`/NUL, enforces byte+part caps, and Windows reserved-device/trailing-dot-space filtering; returns lexical parts only."""
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw or len(raw.encode("utf-8")) > MAX_RELATIVE_BYTES:
        raise WorkspacePathError("workspace-relative path is invalid")
    candidate = Path(raw)
    if candidate.is_absolute() or candidate.anchor:
        raise WorkspacePathError("workspace path must be relative")
    parts = candidate.parts
    if not parts or len(parts) > MAX_RELATIVE_PARTS or any(part in {"", ".", ".."} for part in parts):
        raise WorkspacePathError("workspace-relative path is invalid")
    if os.name == "nt":
        for part in parts:
            if ":" in part or part.endswith((" ", ".")) or len(part) > 255:
                raise WorkspacePathError("Windows workspace path component is invalid")
            stem = part.split(".", 1)[0].casefold()
            if stem in {"con", "nul", "prn", "aux", "conin$", "conout$", "clock$"} \
                    or re.fullmatch(r"(?:com|lpt)(?:[0-9]|[¹²³])", stem) is not None:
                raise WorkspacePathError("Windows reserved device path component is invalid")
    return tuple(parts)


def _read_fd(descriptor: int, maximum: int) -> bytes:
    """Reads a descriptor bounded by `maximum` via a +1 over-read probe that raises one byte past the bound."""
    chunks: list[bytes] = []
    retained = 0
    while True:
        chunk = os.read(descriptor, min(1 << 20, maximum + 1 - retained))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        retained += len(chunk)
        if retained > maximum:
            raise WorkspacePathError("workspace file exceeds its operation bound")


class _WindowsApi:
    """One-line: ctypes/kernel32 shim for handle-anchored open/verify/read/write/rename/dispose."""
    FILE_READ_ATTRIBUTES = 0x00000080
    FILE_LIST_DIRECTORY = 0x00000001
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    DELETE = 0x00010000
    SHARE_READ = 0x00000001
    SHARE_WRITE = 0x00000002
    SHARE_DELETE = 0x00000004
    CREATE_NEW = 1
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_BEGIN = 0
    FILE_RENAME_INFO = 3
    FILE_RENAME_INFO_EX = 22
    FILE_DISPOSITION_INFO = 4
    FILE_RENAME_FLAG_REPLACE_IF_EXISTS = 0x00000001
    FILE_RENAME_FLAG_POSIX_SEMANTICS = 0x00000002
    ERROR_FILE_NOT_FOUND = 2
    ERROR_PATH_NOT_FOUND = 3
    ERROR_ALREADY_EXISTS = 183

    def __init__(self) -> None:
        from ctypes import wintypes

        self.wintypes = wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.CreateFileW = self.kernel32.CreateFileW
        self.CreateFileW.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        self.CreateFileW.restype = wintypes.HANDLE
        self.CloseHandle = self.kernel32.CloseHandle
        self.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.CloseHandle.restype = wintypes.BOOL
        self.GetFinalPathNameByHandleW = self.kernel32.GetFinalPathNameByHandleW
        self.GetFinalPathNameByHandleW.argtypes = (
            wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        )
        self.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self.GetFileInformationByHandle = self.kernel32.GetFileInformationByHandle
        self.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
        self.GetFileInformationByHandle.restype = wintypes.BOOL
        self.ReadFile = self.kernel32.ReadFile
        self.ReadFile.argtypes = (
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        )
        self.ReadFile.restype = wintypes.BOOL
        self.WriteFile = self.kernel32.WriteFile
        self.WriteFile.argtypes = (
            wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        )
        self.WriteFile.restype = wintypes.BOOL
        self.SetFilePointerEx = self.kernel32.SetFilePointerEx
        self.SetFilePointerEx.argtypes = (
            wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
        )
        self.SetFilePointerEx.restype = wintypes.BOOL
        self.FlushFileBuffers = self.kernel32.FlushFileBuffers
        self.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
        self.FlushFileBuffers.restype = wintypes.BOOL
        self.SetFileInformationByHandle = self.kernel32.SetFileInformationByHandle
        self.SetFileInformationByHandle.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        )
        self.SetFileInformationByHandle.restype = wintypes.BOOL
        self.CreateDirectoryW = self.kernel32.CreateDirectoryW
        self.CreateDirectoryW.argtypes = (wintypes.LPCWSTR, wintypes.LPVOID)
        self.CreateDirectoryW.restype = wintypes.BOOL
        self.invalid = ctypes.c_void_p(-1).value

        class FileInformation(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("creation_low", wintypes.DWORD), ("creation_high", wintypes.DWORD),
                ("access_low", wintypes.DWORD), ("access_high", wintypes.DWORD),
                ("write_low", wintypes.DWORD), ("write_high", wintypes.DWORD),
                ("volume", wintypes.DWORD),
                ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
                ("links", wintypes.DWORD),
                ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD),
            ]

        self.FileInformation = FileInformation

    @staticmethod
    def normalized_path(raw: str) -> str:
        if raw.startswith("\\\\?\\UNC\\"):
            raw = "\\\\" + raw[8:]
        elif raw.startswith("\\\\?\\"):
            raw = raw[4:]
        return os.path.normcase(os.path.abspath(raw))

    def _failure(self, message: str) -> _WindowsHandleError:
        return _WindowsHandleError(message, ctypes.get_last_error())

    def close(self, handle: int) -> None:
        if handle is not None and int(handle) != self.invalid:
            self.CloseHandle(handle)

    def open(self, path: Path, access: int, share: int, creation: int, flags: int) -> int:
        handle = self.CreateFileW(str(path), access, share, None, creation, flags, None)
        if int(handle) == self.invalid:
            raise self._failure("workspace handle open failed")
        return handle

    def create_directory(self, path: Path) -> None:
        if self.CreateDirectoryW(str(path), None):
            return
        error = ctypes.get_last_error()
        if error != self.ERROR_ALREADY_EXISTS:
            raise _WindowsHandleError("workspace directory creation failed", error)

    def information(self, handle: int) -> ctypes.Structure:
        value = self.FileInformation()
        if not self.GetFileInformationByHandle(handle, ctypes.byref(value)):
            raise self._failure("workspace handle identity failed")
        return value

    def final_path(self, handle: int) -> str:
        required = self.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not required:
            raise self._failure("workspace handle path failed")
        buffer = ctypes.create_unicode_buffer(required + 1)
        if not self.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0):
            raise self._failure("workspace handle path failed")
        return self.normalized_path(buffer.value)

    def verify_directory(self, handle: int, expected: Path) -> None:
        info = self.information(handle)
        if not info.attributes & self.FILE_ATTRIBUTE_DIRECTORY or info.attributes & _REPARSE_POINT:
            raise WorkspacePathError("workspace directory is redirected or invalid")
        if self.final_path(handle) != self.normalized_path(str(expected)):
            raise WorkspacePathError("workspace directory handle escaped its lexical path")

    def verify_file(self, handle: int, expected: Path) -> FileIdentity:
        info = self.information(handle)
        if info.attributes & (self.FILE_ATTRIBUTE_DIRECTORY | _REPARSE_POINT):
            raise WorkspacePathError("workspace file is redirected or invalid")
        if self.final_path(handle) != self.normalized_path(str(expected)):
            raise WorkspacePathError("workspace file handle escaped its lexical path")
        return FileIdentity(
            device=int(info.volume),
            inode=(int(info.index_high) << 32) | int(info.index_low),
            size=(int(info.size_high) << 32) | int(info.size_low),
            mtime_ns=((int(info.write_high) << 32) | int(info.write_low)) * 100,
        )

    def read(self, handle: int, maximum: int) -> bytes:
        if not self.SetFilePointerEx(handle, 0, None, self.FILE_BEGIN):
            raise self._failure("workspace file seek failed")
        chunks: list[bytes] = []
        retained = 0
        while True:
            capacity = min(1 << 20, maximum + 1 - retained)
            buffer = ctypes.create_string_buffer(capacity)
            count = self.wintypes.DWORD()
            if not self.ReadFile(handle, buffer, capacity, ctypes.byref(count), None):
                raise self._failure("workspace file read failed")
            if count.value == 0:
                return b"".join(chunks)
            chunks.append(buffer.raw[:count.value])
            retained += int(count.value)
            if retained > maximum:
                raise WorkspacePathError("workspace file exceeds its operation bound")

    def write(self, handle: int, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + (1 << 20)]
            count = self.wintypes.DWORD()
            buffer = ctypes.create_string_buffer(chunk)
            if not self.WriteFile(handle, buffer, len(chunk), ctypes.byref(count), None) or count.value != len(chunk):
                raise self._failure("workspace file write failed")
            offset += int(count.value)
        if not self.FlushFileBuffers(handle):
            raise self._failure("workspace file flush failed")

    def disposition(self, handle: int) -> None:
        delete = self.wintypes.BOOL(True)
        if not self.SetFileInformationByHandle(
            handle, self.FILE_DISPOSITION_INFO, ctypes.byref(delete), ctypes.sizeof(delete),
        ):
            raise self._failure("workspace exact-handle unlink failed")

    def rename(self, handle: int, target: Path, *, replace: bool) -> None:
        # The documented Win32 FILE_RENAME_INFO contract requires RootDirectory to be NULL.  The
        # absolute destination is nevertheless stable here because the caller holds the verified
        # non-delete-shared directory chain through the operation.
        """FILE_RENAME_INFO(_EX) byte-layout contract (root_offset/length_offset/name_offset, +2 NUL storage) and REPLACE/POSIX-flag vs info-class selection; the NULL-RootDirectory + held-chain safety note."""
        encoded = str(target).encode("utf-16-le")
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        root_offset = 8 if pointer_size == 8 else 4
        length_offset = root_offset + pointer_size
        name_offset = length_offset + ctypes.sizeof(ctypes.c_uint32)
        # FileNameLength excludes the terminator, but SetFileInformationByHandle requires storage for
        # one trailing UTF-16 NUL.  Omitting it makes Windows consume adjacent bytes into the name.
        buffer = ctypes.create_string_buffer(name_offset + len(encoded) + 2)
        if replace:
            ctypes.c_uint32.from_buffer(buffer, 0).value = (
                self.FILE_RENAME_FLAG_REPLACE_IF_EXISTS | self.FILE_RENAME_FLAG_POSIX_SEMANTICS
            )
        else:
            ctypes.c_uint32.from_buffer(buffer, 0).value = 0
        ctypes.c_void_p.from_buffer(buffer, root_offset).value = None
        ctypes.c_uint32.from_buffer(buffer, length_offset).value = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded, len(encoded))
        information_class = self.FILE_RENAME_INFO_EX if replace else self.FILE_RENAME_INFO
        if not self.SetFileInformationByHandle(handle, information_class, buffer, ctypes.sizeof(buffer)):
            raise self._failure("workspace atomic rename failed")


class WorkspacePathAuthority:
    """Bind one lexical workspace root and perform only handle-anchored file operations."""

    def __init__(self, workspace_root: str | os.PathLike[str]) -> None:
        raw = os.fspath(workspace_root)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise WorkspacePathError("workspace root is invalid")
        self.workspace_root = Path(os.path.abspath(raw))
        self._lock = threading.RLock()
        self._closed = False
        self._windows: _WindowsApi | None = _WindowsApi() if os.name == "nt" else None
        self._windows_root_handles: list[int] = []
        self._posix_root_fd: int | None = None
        try:
            if self._windows is not None:
                self._bind_windows_root()
            else:
                self._posix_root_fd = self._bind_posix_root()
        except Exception:
            self.close()
            raise

    def __enter__(self) -> "WorkspacePathAuthority":
        self._ensure_open()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    @contextmanager
    def exclusive(self) -> Iterator["WorkspacePathAuthority"]:
        """Hold this authority's process-local critical section across multiple operations."""

        with self._lock:
            self._ensure_open()
            yield self

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._windows is not None:
                for handle in reversed(self._windows_root_handles):
                    self._windows.close(handle)
                self._windows_root_handles.clear()
            if self._posix_root_fd is not None:
                os.close(self._posix_root_fd)
                self._posix_root_fd = None

    def _ensure_open(self) -> None:
        if self._closed:
            raise WorkspacePathError("workspace path authority is closed")

    def _bind_windows_root(self) -> None:
        assert self._windows is not None
        anchor = Path(self.workspace_root.anchor)
        if not anchor.anchor:
            raise WorkspacePathError("Windows workspace root must be absolute")
        paths = [anchor]
        current = anchor
        for part in self.workspace_root.parts[1:]:
            current = current / part
            paths.append(current)
        try:
            for expected in paths:
                handle = self._windows.open(
                    expected,
                    self._windows.FILE_LIST_DIRECTORY | self._windows.FILE_READ_ATTRIBUTES,
                    self._windows.SHARE_READ | self._windows.SHARE_WRITE,
                    self._windows.OPEN_EXISTING,
                    self._windows.FILE_FLAG_BACKUP_SEMANTICS | self._windows.FILE_FLAG_OPEN_REPARSE_POINT,
                )
                self._windows_root_handles.append(handle)
                self._windows.verify_directory(handle, expected)
        except Exception:
            for handle in reversed(self._windows_root_handles):
                self._windows.close(handle)
            self._windows_root_handles.clear()
            raise

    def _bind_posix_root(self) -> int:
        if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
            raise WorkspacePathError("POSIX no-follow directory handles are unavailable")
        required_dir_fd = (os.open, os.stat, os.unlink, os.link, os.replace, os.mkdir)
        if any(function not in os.supports_dir_fd for function in required_dir_fd) \
                or os.stat not in os.supports_follow_symlinks \
                or os.link not in os.supports_follow_symlinks:
            raise WorkspacePathError("required POSIX dir-fd path authority is unavailable")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) \
            | getattr(os, "O_NOFOLLOW", 0)
        anchor = self.workspace_root.anchor or os.sep
        descriptor = os.open(anchor, flags)
        try:
            if not _directory(os.fstat(descriptor)):
                raise WorkspacePathError("workspace root anchor is invalid")
            for part in self.workspace_root.parts[1:]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                if not _directory(os.fstat(descriptor)):
                    raise WorkspacePathError("workspace root contains a redirected component")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @contextmanager
    def _posix_parent(self, parts: tuple[str, ...]) -> Iterator[tuple[int, str]]:
        assert self._posix_root_fd is not None
        descriptor = os.dup(self._posix_root_fd)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) \
            | getattr(os, "O_NOFOLLOW", 0)
        try:
            for part in parts[:-1]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                if not _directory(os.fstat(descriptor)):
                    raise WorkspacePathError("workspace parent contains a redirected component")
            yield descriptor, parts[-1]
        except WorkspacePathError:
            raise
        except OSError as error:
            raise WorkspacePathError("workspace parent could not be secured") from error
        finally:
            os.close(descriptor)

    @contextmanager
    def _windows_parent(self, parts: tuple[str, ...]) -> Iterator[tuple[int, Path, str]]:
        assert self._windows is not None and self._windows_root_handles
        handles: list[int] = []
        current = self.workspace_root
        try:
            for part in parts[:-1]:
                current = current / part
                handle = self._windows.open(
                    current,
                    self._windows.FILE_LIST_DIRECTORY | self._windows.FILE_READ_ATTRIBUTES,
                    self._windows.SHARE_READ | self._windows.SHARE_WRITE,
                    self._windows.OPEN_EXISTING,
                    self._windows.FILE_FLAG_BACKUP_SEMANTICS | self._windows.FILE_FLAG_OPEN_REPARSE_POINT,
                )
                handles.append(handle)
                self._windows.verify_directory(handle, current)
            yield handles[-1] if handles else self._windows_root_handles[-1], current, parts[-1]
        finally:
            for handle in reversed(handles):
                self._windows.close(handle)

    def _posix_identity_at(self, parent: int, name: str) -> FileIdentity | None:
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise WorkspacePathError("workspace destination identity is unavailable") from error
        if not _regular(info):
            raise WorkspacePathError("workspace destination is redirected or invalid")
        return _identity(info)

    def _windows_identity_at(self, target: Path) -> FileIdentity | None:
        assert self._windows is not None
        share = self._windows.SHARE_READ | self._windows.SHARE_WRITE
        try:
            handle = self._windows.open(
                target, self._windows.FILE_READ_ATTRIBUTES, share, self._windows.OPEN_EXISTING,
                self._windows.FILE_ATTRIBUTE_NORMAL | self._windows.FILE_FLAG_OPEN_REPARSE_POINT,
            )
        except _WindowsHandleError as error:
            if error.winerror in {self._windows.ERROR_FILE_NOT_FOUND, self._windows.ERROR_PATH_NOT_FOUND}:
                return None
            raise error
        try:
            return self._windows.verify_file(handle, target)
        finally:
            self._windows.close(handle)

    def acquire_process_lock(
        self,
        relative: str | os.PathLike[str],
        *,
        max_bytes: int = 1024,
    ) -> WorkspaceProcessLock:
        """Acquire one nonblocking lifetime lock on a handle-anchored persistent file."""

        maximum = _bound(max_bytes)
        parts = _relative_parts(relative)
        with self._lock:
            self._ensure_open()
            identity = self.identity(relative)
            if identity is None:
                try:
                    identity = self.create_exact_exclusive(
                        relative, b"\0", max_bytes=maximum,
                    ).identity
                except WorkspacePathError:
                    identity = self.identity(relative)
                    if identity is None:
                        raise
            if identity.size < 1 or identity.size > maximum:
                raise WorkspacePathError("workspace process lock file size is invalid")
            if self._windows is not None:
                handle = self._open_windows_process_lock(parts, identity)
            else:
                handle = self._open_posix_process_lock(parts, identity)
            try:
                from .claude_runtime import _try_file_lock

                _try_file_lock(handle)
            except Exception:
                handle.close()
                raise
            return WorkspaceProcessLock(handle)

    def _open_posix_process_lock(self, parts: tuple[str, ...], expected: FileIdentity):
        with self._posix_parent(parts) as (parent, name):
            flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=parent)
            except OSError as error:
                raise WorkspacePathError("workspace process lock could not be opened") from error
            try:
                if not _regular(os.fstat(descriptor)) or _identity(os.fstat(descriptor)) != expected \
                        or self._posix_identity_at(parent, name) != expected:
                    raise WorkspacePathError("workspace process lock identity changed")
                return os.fdopen(descriptor, "r+b", buffering=0)
            except Exception:
                os.close(descriptor)
                raise

    def _open_windows_process_lock(self, parts: tuple[str, ...], expected: FileIdentity):
        assert self._windows is not None
        import msvcrt

        with self._windows_parent(parts) as (_parent, path, name):
            target = path / name
            handle = self._windows.open(
                target,
                self._windows.GENERIC_READ | self._windows.GENERIC_WRITE
                | self._windows.FILE_READ_ATTRIBUTES,
                self._windows.SHARE_READ | self._windows.SHARE_WRITE,
                self._windows.OPEN_EXISTING,
                self._windows.FILE_ATTRIBUTE_NORMAL | self._windows.FILE_FLAG_OPEN_REPARSE_POINT,
            )
            descriptor: int | None = None
            try:
                if self._windows.verify_file(handle, target) != expected:
                    raise WorkspacePathError("workspace process lock identity changed")
                descriptor = msvcrt.open_osfhandle(
                    int(handle), os.O_RDWR | getattr(os, "O_BINARY", 0),
                )
                handle = 0
                return os.fdopen(descriptor, "r+b", buffering=0)
            except Exception:
                if descriptor is not None:
                    os.close(descriptor)
                elif handle:
                    self._windows.close(handle)
                raise

    def identity(self, relative: str | os.PathLike[str]) -> FileIdentity | None:
        parts = _relative_parts(relative)
        with self._lock:
            self._ensure_open()
            if self._windows is not None:
                try:
                    with self._windows_parent(parts) as (_parent, path, name):
                        return self._windows_identity_at(path / name)
                except _WindowsHandleError as error:
                    if error.winerror in {
                        self._windows.ERROR_FILE_NOT_FOUND, self._windows.ERROR_PATH_NOT_FOUND,
                    }:
                        return None
                    raise
            try:
                with self._posix_parent(parts) as (parent, name):
                    return self._posix_identity_at(parent, name)
            except WorkspacePathError as error:
                if isinstance(error.__cause__, FileNotFoundError):
                    return None
                raise

    def ensure_directory(self, relative: str | os.PathLike[str]) -> None:
        """Create an exact workspace-relative directory chain without following redirects."""

        parts = _relative_parts(relative)
        with self._lock:
            self._ensure_open()
            if self._windows is not None:
                current = self.workspace_root
                handles: list[int] = []
                try:
                    for part in parts:
                        current = current / part
                        try:
                            handle = self._windows.open(
                                current,
                                self._windows.FILE_LIST_DIRECTORY | self._windows.FILE_READ_ATTRIBUTES,
                                self._windows.SHARE_READ | self._windows.SHARE_WRITE,
                                self._windows.OPEN_EXISTING,
                                self._windows.FILE_FLAG_BACKUP_SEMANTICS
                                | self._windows.FILE_FLAG_OPEN_REPARSE_POINT,
                            )
                        except _WindowsHandleError as error:
                            if error.winerror not in {
                                self._windows.ERROR_FILE_NOT_FOUND,
                                self._windows.ERROR_PATH_NOT_FOUND,
                            }:
                                raise
                            self._windows.create_directory(current)
                            handle = self._windows.open(
                                current,
                                self._windows.FILE_LIST_DIRECTORY | self._windows.FILE_READ_ATTRIBUTES,
                                self._windows.SHARE_READ | self._windows.SHARE_WRITE,
                                self._windows.OPEN_EXISTING,
                                self._windows.FILE_FLAG_BACKUP_SEMANTICS
                                | self._windows.FILE_FLAG_OPEN_REPARSE_POINT,
                            )
                        handles.append(handle)
                        self._windows.verify_directory(handle, current)
                finally:
                    for handle in reversed(handles):
                        self._windows.close(handle)
                return

            assert self._posix_root_fd is not None
            descriptor = os.dup(self._posix_root_fd)
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) \
                | getattr(os, "O_NOFOLLOW", 0)
            try:
                for part in parts:
                    try:
                        child = os.open(part, flags, dir_fd=descriptor)
                    except FileNotFoundError:
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                        child = os.open(part, flags, dir_fd=descriptor)
                    if not _directory(os.fstat(child)):
                        os.close(child)
                        raise WorkspacePathError("workspace directory contains a redirected component")
                    os.close(descriptor)
                    descriptor = child
            except WorkspacePathError:
                raise
            except OSError as error:
                raise WorkspacePathError("workspace directory could not be secured") from error
            finally:
                os.close(descriptor)

    def read(self, relative: str | os.PathLike[str], max_bytes: int) -> StableRead:
        maximum = _bound(max_bytes)
        parts = _relative_parts(relative)
        with self._lock:
            self._ensure_open()
            if self._windows is not None:
                return self._read_windows(parts, maximum)
            return self._read_posix(parts, maximum)

    def _read_posix(self, parts: tuple[str, ...], maximum: int) -> StableRead:
        with self._posix_parent(parts) as (parent, name):
            before = self._posix_identity_at(parent, name)
            if before is None:
                raise WorkspacePathError("workspace file is missing")
            if before.size > maximum:
                raise WorkspacePathError("workspace file exceeds its operation bound")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) \
                | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=parent)
            except OSError as error:
                raise WorkspacePathError("workspace file could not be opened safely") from error
            try:
                opened = os.fstat(descriptor)
                if not _regular(opened) or _identity(opened) != before:
                    raise WorkspacePathError("workspace file changed before read")
                data = _read_fd(descriptor, maximum)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            current = self._posix_identity_at(parent, name)
            if not _regular(after) or _identity(after) != before or current != before \
                    or len(data) != before.size:
                raise WorkspacePathError("workspace file changed while read")
            return StableRead(data, before, hashlib.sha256(data).hexdigest(), len(data))

    def _read_windows(self, parts: tuple[str, ...], maximum: int) -> StableRead:
        assert self._windows is not None
        with self._windows_parent(parts) as (_parent, path, name):
            target = path / name
            handle = self._windows.open(
                target, self._windows.GENERIC_READ | self._windows.FILE_READ_ATTRIBUTES,
                self._windows.SHARE_READ, self._windows.OPEN_EXISTING,
                self._windows.FILE_ATTRIBUTE_NORMAL | self._windows.FILE_FLAG_OPEN_REPARSE_POINT,
            )
            try:
                before = self._windows.verify_file(handle, target)
                if before.size > maximum:
                    raise WorkspacePathError("workspace file exceeds its operation bound")
                data = self._windows.read(handle, maximum)
                after = self._windows.verify_file(handle, target)
                if after != before or len(data) != before.size:
                    raise WorkspacePathError("workspace file changed while read")
                return StableRead(data, before, hashlib.sha256(data).hexdigest(), len(data))
            finally:
                self._windows.close(handle)

    def create_exact_exclusive(
        self,
        relative: str | os.PathLike[str],
        data: bytes,
        *,
        max_bytes: int,
    ) -> StableRead:
        """Create one caller-named staged file and prove the exact durable bytes written."""

        maximum = _bound(max_bytes)
        if not isinstance(data, bytes) or len(data) > maximum:
            raise WorkspacePathError("staged bytes are invalid or oversized")
        parts = _relative_parts(relative)
        with self._lock:
            self._ensure_open()
            if self._windows is not None:
                return self._create_exact_windows(parts, data, maximum)
            return self._create_exact_posix(parts, data, maximum)

    def _create_exact_posix(
        self, parts: tuple[str, ...], data: bytes, maximum: int,
    ) -> StableRead:
        with self._posix_parent(parts) as (parent, name):
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) \
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor: int | None = None
            completed = False
            try:
                descriptor = os.open(name, flags, 0o600, dir_fd=parent)
                offset = 0
                while offset < len(data):
                    count = os.write(descriptor, data[offset:])
                    if count <= 0:
                        raise WorkspacePathError("staged file write was incomplete")
                    offset += count
                os.fsync(descriptor)
                before = os.fstat(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                verified = _read_fd(descriptor, maximum)
                after = os.fstat(descriptor)
                identity = _identity(before)
                if not _regular(before) or _identity(after) != identity \
                        or self._posix_identity_at(parent, name) != identity or verified != data:
                    raise WorkspacePathError("staged file could not be verified")
                try:
                    os.fsync(parent)
                except OSError:
                    pass
                completed = True
                return StableRead(verified, identity, hashlib.sha256(verified).hexdigest(), len(verified))
            except WorkspacePathError:
                raise
            except OSError as error:
                raise WorkspacePathError("exclusive staged file creation failed") from error
            finally:
                if descriptor is not None:
                    if not completed:
                        try:
                            opened = _identity(os.fstat(descriptor))
                            if self._posix_identity_at(parent, name) == opened:
                                os.unlink(name, dir_fd=parent)
                        except (OSError, WorkspacePathError):
                            pass
                    os.close(descriptor)

    def _create_exact_windows(
        self, parts: tuple[str, ...], data: bytes, maximum: int,
    ) -> StableRead:
        assert self._windows is not None
        with self._windows_parent(parts) as (_parent, path, name):
            target = path / name
            handle = self._windows.open(
                target,
                self._windows.GENERIC_READ | self._windows.GENERIC_WRITE | self._windows.DELETE
                | self._windows.FILE_READ_ATTRIBUTES,
                self._windows.SHARE_READ,
                self._windows.CREATE_NEW,
                self._windows.FILE_ATTRIBUTE_NORMAL | self._windows.FILE_FLAG_OPEN_REPARSE_POINT,
            )
            completed = False
            try:
                self._windows.write(handle, data)
                before = self._windows.verify_file(handle, target)
                verified = self._windows.read(handle, maximum)
                after = self._windows.verify_file(handle, target)
                if before != after or before.size != len(data) or verified != data:
                    raise WorkspacePathError("staged file could not be verified")
                completed = True
                return StableRead(verified, before, hashlib.sha256(verified).hexdigest(), len(verified))
            finally:
                try:
                    if not completed:
                        self._windows.disposition(handle)
                finally:
                    self._windows.close(handle)

    @staticmethod
    def _expected_target(
        expected: StableRead | None,
    ) -> tuple[FileIdentity | None, StableRead | None]:
        if expected is None:
            return None, None
        if isinstance(expected, StableRead):
            if expected.size != len(expected.data) or expected.identity.size != expected.size \
                    or _SHA256.fullmatch(expected.sha256) is None \
                    or hashlib.sha256(expected.data).hexdigest() != expected.sha256:
                raise WorkspacePathError("expected destination proof is invalid")
            return expected.identity, expected
        raise WorkspacePathError("expected destination proof is invalid")

    def replace_from_exact(
        self,
        staged_relative: str | os.PathLike[str],
        target_relative: str | os.PathLike[str],
        *,
        staged_sha256: str,
        staged_size: int,
        expected_target: StableRead | None,
        max_bytes: int,
    ) -> FileIdentity:
        """Promote a caller-journaled staged file after exact source and target proof.

        Ancestor directory handles remain held across the mutation.  Existing destinations require
        the identity, SHA-256, and size proof returned by ``read``.  On Windows, replacement removes
        the exact proven target before a no-clobber promotion, so callers must journal recovery bytes
        first.  POSIX refuses existing destinations before target mutation.
        """

        maximum = _bound(max_bytes)
        if not isinstance(staged_sha256, str) or _SHA256.fullmatch(staged_sha256) is None \
                or isinstance(staged_size, bool) or not isinstance(staged_size, int) \
                or not (0 <= staged_size <= maximum):
            raise WorkspacePathError("staged file evidence is invalid")
        expected_identity, expected_read = self._expected_target(expected_target)
        staged_parts = _relative_parts(staged_relative)
        target_parts = _relative_parts(target_relative)
        staged_key = tuple(os.path.normcase(part) for part in staged_parts) if os.name == "nt" \
            else staged_parts
        target_key = tuple(os.path.normcase(part) for part in target_parts) if os.name == "nt" \
            else target_parts
        if staged_key == target_key:
            raise WorkspacePathError("staged and destination paths must differ")
        with self._lock:
            self._ensure_open()
            if self._windows is not None:
                return self._promote_windows(
                    staged_parts, target_parts, staged_sha256, staged_size,
                    expected_identity, expected_read, maximum,
                )
            if expected_identity is not None:
                raise WorkspacePathError(
                    "POSIX existing-destination promotion is unsupported safely"
                )
            return self._promote_posix(
                staged_parts, target_parts, staged_sha256, staged_size, maximum,
            )

    def _promote_posix(
        self,
        staged_parts: tuple[str, ...],
        target_parts: tuple[str, ...],
        digest: str,
        size: int,
        maximum: int,
    ) -> FileIdentity:
        with self._posix_parent(staged_parts) as (source_parent, source_name), \
                self._posix_parent(target_parts) as (target_parent, target_name):
            source_current = self._posix_identity_at(source_parent, source_name)
            if source_current is None or source_current.size != size:
                raise WorkspacePathError("staged file identity or size does not match")
            source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) \
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                source = os.open(source_name, source_flags, dir_fd=source_parent)
            except OSError as error:
                raise WorkspacePathError("staged file could not be opened safely") from error
            try:
                source_opened = os.fstat(source)
                source_data = _read_fd(source, maximum)
                source_after = os.fstat(source)
                if not _regular(source_opened) or _identity(source_opened) != source_current \
                        or _identity(source_after) != source_current or len(source_data) != size \
                        or hashlib.sha256(source_data).hexdigest() != digest:
                    raise WorkspacePathError("staged file hash or identity does not match")

                if self._posix_identity_at(source_parent, source_name) != source_current:
                    raise WorkspacePathError("staged file path changed before promotion")
                self._require_expected(self._posix_identity_at(target_parent, target_name), None)
                # linkat is the portable stdlib no-clobber primitive.  It also leaves both exact
                # names recoverable if the process stops before the staged name unlink.
                os.link(
                    source_name, target_name,
                    src_dir_fd=source_parent, dst_dir_fd=target_parent,
                    follow_symlinks=False,
                )
                linked = self._posix_identity_at(target_parent, target_name)
                if linked != source_current \
                        or self._posix_identity_at(source_parent, source_name) != source_current:
                    raise WorkspacePathError(
                        "no-clobber promotion link could not be verified; recovery is required"
                    )
                os.unlink(source_name, dir_fd=source_parent)
                final = self._posix_identity_at(target_parent, target_name)
                os.lseek(source, 0, os.SEEK_SET)
                final_data = _read_fd(source, maximum)
                if final != source_current or _identity(os.fstat(source)) != source_current \
                        or self._posix_identity_at(source_parent, source_name) is not None \
                        or len(final_data) != size or hashlib.sha256(final_data).hexdigest() != digest:
                    raise WorkspacePathError("promoted file could not be verified; recovery is required")
                try:
                    os.fsync(target_parent)
                    if source_parent != target_parent:
                        os.fsync(source_parent)
                except OSError:
                    pass
                return final
            except WorkspacePathError:
                raise
            except OSError as error:
                raise WorkspacePathError("exact staged file promotion failed") from error
            finally:
                os.close(source)

    def _promote_windows(
        self,
        staged_parts: tuple[str, ...],
        target_parts: tuple[str, ...],
        digest: str,
        size: int,
        expected: FileIdentity | None,
        expected_read: StableRead | None,
        maximum: int,
    ) -> FileIdentity:
        assert self._windows is not None
        with self._windows_parent(staged_parts) as (_source_parent, source_path, source_name), \
                self._windows_parent(target_parts) as (_target_parent, target_path, target_name):
            source_target = source_path / source_name
            destination = target_path / target_name
            source = self._windows.open(
                source_target,
                self._windows.GENERIC_READ | self._windows.DELETE | self._windows.FILE_READ_ATTRIBUTES,
                self._windows.SHARE_READ,
                self._windows.OPEN_EXISTING,
                self._windows.FILE_ATTRIBUTE_NORMAL | self._windows.FILE_FLAG_OPEN_REPARSE_POINT,
            )
            target: int | None = None
            target_disposed = False
            try:
                source_identity = self._windows.verify_file(source, source_target)
                source_data = self._windows.read(source, maximum)
                if self._windows.verify_file(source, source_target) != source_identity \
                        or source_identity.size != size or len(source_data) != size \
                        or hashlib.sha256(source_data).hexdigest() != digest:
                    raise WorkspacePathError("staged file hash or identity does not match")

                if expected is None:
                    self._require_expected(self._windows_identity_at(destination), None)
                else:
                    target = self._windows.open(
                        destination,
                        self._windows.GENERIC_READ | self._windows.DELETE
                        | self._windows.FILE_READ_ATTRIBUTES,
                        self._windows.SHARE_READ,
                        self._windows.OPEN_EXISTING,
                        self._windows.FILE_ATTRIBUTE_NORMAL | self._windows.FILE_FLAG_OPEN_REPARSE_POINT,
                    )
                    self._require_expected(self._windows.verify_file(target, destination), expected)
                    if expected.size > maximum:
                        raise WorkspacePathError("expected destination exceeds its operation bound")
                    target_data = self._windows.read(target, maximum)
                    if self._windows.verify_file(target, destination) != expected \
                            or len(target_data) != expected.size:
                        raise WorkspacePathError("expected destination changed while read")
                    target_digest = hashlib.sha256(target_data).hexdigest()
                    if expected_read is not None and (
                        expected_read.size != len(target_data) or expected_read.sha256 != target_digest
                    ):
                        raise WorkspacePathError("expected destination bytes changed")

                if self._windows.verify_file(source, source_target) != source_identity:
                    raise WorkspacePathError("staged file changed before promotion")
                repeated_source = self._windows.read(source, maximum)
                if len(repeated_source) != size or hashlib.sha256(repeated_source).hexdigest() != digest:
                    raise WorkspacePathError("staged file bytes changed before promotion")
                if target is None:
                    self._require_expected(self._windows_identity_at(destination), None)
                else:
                    self._require_expected(self._windows.verify_file(target, destination), expected)
                    repeated_target = self._windows.read(target, maximum)
                    if hashlib.sha256(repeated_target).hexdigest() != target_digest:
                        raise WorkspacePathError("expected destination changed before promotion")
                    self._windows.disposition(target)
                    target_disposed = True
                    self._windows.close(target)
                    target = None
                    try:
                        self._require_expected(self._windows_identity_at(destination), None)
                    except WorkspacePathError as error:
                        raise _WorkspaceRecoveryRequired(
                            "exact destination disposition is pending; recovery is required",
                            staged_parts,
                        ) from error
                try:
                    self._windows.rename(source, destination, replace=False)
                except WorkspacePathError as error:
                    if target_disposed:
                        raise _WorkspaceRecoveryRequired(
                            "exact destination was removed but no-clobber promotion failed; "
                            "recovery is required",
                            staged_parts,
                        ) from error
                    raise
                final = self._windows.verify_file(source, destination)
                final_data = self._windows.read(source, maximum)
                if final != source_identity or len(final_data) != size \
                        or hashlib.sha256(final_data).hexdigest() != digest \
                        or self._windows_identity_at(source_target) is not None:
                    raise WorkspacePathError("promoted file could not be verified; recovery is required")
                return final
            finally:
                if target is not None:
                    self._windows.close(target)
                self._windows.close(source)

    def atomic_replace(
        self,
        relative: str | os.PathLike[str],
        data: bytes,
        *,
        expected: StableRead | None,
        max_bytes: int,
    ) -> FileIdentity:
        """Atomically replace private single-writer state after exact content reproving.

        This method is intentionally narrower than ``replace_from_exact``: it is for Kaizen-owned
        private registry state protected by a process lock, not proposal-controlled workspace
        files.  Its final rename is crash-atomic, so the destination is either the old complete file
        or the new complete file and is never intentionally absent.
        """

        maximum = _bound(max_bytes)
        if not isinstance(data, bytes) or len(data) > maximum:
            raise WorkspacePathError("replacement bytes are invalid or oversized")
        expected_identity, expected_read = self._expected_target(expected)
        parts = _relative_parts(relative)
        temporary_parts = parts[:-1] + (f".kaizen-{os.getpid()}-{secrets.token_hex(12)}.tmp",)
        with self._lock:
            self._ensure_open()
            if self._windows is not None:
                staged = self._create_exact_windows(temporary_parts, data, maximum)
            else:
                staged = self._create_exact_posix(temporary_parts, data, maximum)
            try:
                if self._windows is not None:
                    return self._atomic_replace_windows(
                        temporary_parts, parts, staged.sha256, staged.size,
                        expected_identity, expected_read, maximum,
                    )
                return self._atomic_replace_posix(
                    temporary_parts, parts, staged.sha256, staged.size,
                    expected_identity, expected_read, maximum,
                )
            except Exception:
                try:
                    if self._windows is not None:
                        self._unlink_windows(temporary_parts, staged.sha256, staged.size, maximum)
                    else:
                        self._unlink_posix(temporary_parts, staged.sha256, staged.size, maximum)
                except WorkspacePathError as cleanup_error:
                    raise WorkspacePathError(
                        "atomic replacement staging cleanup failed; recovery is required"
                    ) from cleanup_error
                raise

    def _atomic_replace_posix(
        self,
        staged_parts: tuple[str, ...],
        target_parts: tuple[str, ...],
        digest: str,
        size: int,
        expected: FileIdentity | None,
        expected_read: StableRead | None,
        maximum: int,
    ) -> FileIdentity:
        if staged_parts[:-1] != target_parts[:-1]:
            raise WorkspacePathError("private atomic staging must share the destination parent")
        with self._posix_parent(target_parts) as (parent, target_name):
            source_name = staged_parts[-1]
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) \
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                source = os.open(source_name, flags, dir_fd=parent)
            except OSError as error:
                raise WorkspacePathError("private atomic staging could not be opened") from error
            target: int | None = None
            try:
                source_identity = _identity(os.fstat(source))
                source_data = _read_fd(source, maximum)
                if not _regular(os.fstat(source)) or source_identity.size != size \
                        or len(source_data) != size \
                        or hashlib.sha256(source_data).hexdigest() != digest \
                        or self._posix_identity_at(parent, source_name) != source_identity:
                    raise WorkspacePathError("private atomic staging proof failed")

                self._require_expected(self._posix_identity_at(parent, target_name), expected)
                if expected is not None:
                    try:
                        target = os.open(target_name, flags, dir_fd=parent)
                    except OSError as error:
                        raise WorkspacePathError("private atomic target could not be opened") from error
                    target_data = _read_fd(target, maximum)
                    if not _regular(os.fstat(target)) or _identity(os.fstat(target)) != expected \
                            or expected_read is None or len(target_data) != expected_read.size \
                            or hashlib.sha256(target_data).hexdigest() != expected_read.sha256:
                        raise WorkspacePathError("private atomic target proof failed")

                if self._posix_identity_at(parent, source_name) != source_identity:
                    raise WorkspacePathError("private atomic staging changed before rename")
                self._require_expected(self._posix_identity_at(parent, target_name), expected)
                if target is not None:
                    os.lseek(target, 0, os.SEEK_SET)
                    repeated_target = _read_fd(target, maximum)
                    if _identity(os.fstat(target)) != expected \
                            or hashlib.sha256(repeated_target).hexdigest() != expected_read.sha256:
                        raise WorkspacePathError("private atomic target changed before rename")
                os.replace(source_name, target_name, src_dir_fd=parent, dst_dir_fd=parent)
                final = self._posix_identity_at(parent, target_name)
                os.lseek(source, 0, os.SEEK_SET)
                final_data = _read_fd(source, maximum)
                if final != source_identity or _identity(os.fstat(source)) != source_identity \
                        or self._posix_identity_at(parent, source_name) is not None \
                        or len(final_data) != size or hashlib.sha256(final_data).hexdigest() != digest:
                    raise WorkspacePathError(
                        "private atomic replacement could not be verified; recovery is required"
                    )
                try:
                    os.fsync(parent)
                except OSError:
                    pass
                return final
            except WorkspacePathError:
                raise
            except OSError as error:
                raise WorkspacePathError("private atomic replacement failed") from error
            finally:
                if target is not None:
                    os.close(target)
                os.close(source)

    def _atomic_replace_windows(
        self,
        staged_parts: tuple[str, ...],
        target_parts: tuple[str, ...],
        digest: str,
        size: int,
        expected: FileIdentity | None,
        expected_read: StableRead | None,
        maximum: int,
    ) -> FileIdentity:
        assert self._windows is not None
        if staged_parts[:-1] != target_parts[:-1]:
            raise WorkspacePathError("private atomic staging must share the destination parent")
        with self._windows_parent(target_parts) as (_parent, path, target_name):
            source_path = path / staged_parts[-1]
            destination = path / target_name
            source = self._windows.open(
                source_path,
                self._windows.GENERIC_READ | self._windows.DELETE | self._windows.FILE_READ_ATTRIBUTES,
                self._windows.SHARE_READ,
                self._windows.OPEN_EXISTING,
                self._windows.FILE_ATTRIBUTE_NORMAL | self._windows.FILE_FLAG_OPEN_REPARSE_POINT,
            )
            target: int | None = None
            try:
                source_identity = self._windows.verify_file(source, source_path)
                source_data = self._windows.read(source, maximum)
                if self._windows.verify_file(source, source_path) != source_identity \
                        or source_identity.size != size or len(source_data) != size \
                        or hashlib.sha256(source_data).hexdigest() != digest:
                    raise WorkspacePathError("private atomic staging proof failed")

                if expected is None:
                    self._require_expected(self._windows_identity_at(destination), None)
                else:
                    target = self._windows.open(
                        destination,
                        self._windows.GENERIC_READ | self._windows.FILE_READ_ATTRIBUTES,
                        self._windows.SHARE_READ | self._windows.SHARE_DELETE,
                        self._windows.OPEN_EXISTING,
                        self._windows.FILE_ATTRIBUTE_NORMAL | self._windows.FILE_FLAG_OPEN_REPARSE_POINT,
                    )
                    self._require_expected(self._windows.verify_file(target, destination), expected)
                    target_data = self._windows.read(target, maximum)
                    if self._windows.verify_file(target, destination) != expected \
                            or expected_read is None or len(target_data) != expected_read.size \
                            or hashlib.sha256(target_data).hexdigest() != expected_read.sha256:
                        raise WorkspacePathError("private atomic target proof failed")

                if self._windows.verify_file(source, source_path) != source_identity:
                    raise WorkspacePathError("private atomic staging changed before rename")
                repeated_source = self._windows.read(source, maximum)
                if len(repeated_source) != size or hashlib.sha256(repeated_source).hexdigest() != digest:
                    raise WorkspacePathError("private atomic staging bytes changed before rename")
                if target is None:
                    self._require_expected(self._windows_identity_at(destination), None)
                else:
                    self._require_expected(self._windows.verify_file(target, destination), expected)
                    repeated_target = self._windows.read(target, maximum)
                    if hashlib.sha256(repeated_target).hexdigest() != expected_read.sha256:
                        raise WorkspacePathError("private atomic target changed before rename")
                self._windows.rename(source, destination, replace=target is not None)
                final = self._windows.verify_file(source, destination)
                final_data = self._windows.read(source, maximum)
                if final != source_identity or len(final_data) != size \
                        or hashlib.sha256(final_data).hexdigest() != digest \
                        or self._windows_identity_at(source_path) is not None:
                    raise WorkspacePathError(
                        "private atomic replacement could not be verified; recovery is required"
                    )
                return final
            finally:
                if target is not None:
                    self._windows.close(target)
                self._windows.close(source)

    @staticmethod
    def _require_expected(current: FileIdentity | None, expected: FileIdentity | None) -> None:
        if current != expected:
            raise WorkspacePathError("workspace destination identity changed")

    def unlink_exact(
        self,
        relative: str | os.PathLike[str],
        *,
        sha256: str,
        size: int,
        max_bytes: int,
    ) -> bool:
        maximum = _bound(max_bytes)
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None \
                or isinstance(size, bool) or not isinstance(size, int) or not (0 <= size <= maximum):
            raise WorkspacePathError("exact unlink evidence is invalid")
        parts = _relative_parts(relative)
        with self._lock:
            self._ensure_open()
            if self._windows is not None:
                return self._unlink_windows(parts, sha256, size, maximum)
            return self._unlink_posix(parts, sha256, size, maximum)

    def _unlink_posix(self, parts: tuple[str, ...], digest: str, size: int, maximum: int) -> bool:
        with self._posix_parent(parts) as (parent, name):
            current = self._posix_identity_at(parent, name)
            if current is None:
                return False
            if current.size != size or current.size > maximum:
                raise WorkspacePathError("exact unlink size does not match")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) \
                | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=parent)
            except OSError as error:
                raise WorkspacePathError("exact unlink target could not be opened") from error
            try:
                opened = os.fstat(descriptor)
                data = _read_fd(descriptor, maximum)
                after = os.fstat(descriptor)
                if not _regular(opened) or _identity(opened) != current or _identity(after) != current \
                        or len(data) != size or hashlib.sha256(data).hexdigest() != digest \
                        or self._posix_identity_at(parent, name) != current:
                    raise WorkspacePathError("exact unlink hash or identity does not match")
                try:
                    os.unlink(name, dir_fd=parent)
                except OSError as error:
                    raise WorkspacePathError("exact unlink failed") from error
                if self._posix_identity_at(parent, name) is not None or _identity(os.fstat(descriptor)) != current:
                    raise WorkspacePathError("exact unlink could not be verified; recovery is required")
            finally:
                os.close(descriptor)
            return True

    def _unlink_windows(self, parts: tuple[str, ...], digest: str, size: int, maximum: int) -> bool:
        assert self._windows is not None
        with self._windows_parent(parts) as (_parent, path, name):
            target = path / name
            if self._windows_identity_at(target) is None:
                return False
            handle = self._windows.open(
                target,
                self._windows.GENERIC_READ | self._windows.DELETE | self._windows.FILE_READ_ATTRIBUTES,
                self._windows.SHARE_READ,
                self._windows.OPEN_EXISTING,
                self._windows.FILE_ATTRIBUTE_NORMAL | self._windows.FILE_FLAG_OPEN_REPARSE_POINT,
            )
            try:
                before = self._windows.verify_file(handle, target)
                if before.size != size or before.size > maximum:
                    raise WorkspacePathError("exact unlink size does not match")
                data = self._windows.read(handle, maximum)
                after = self._windows.verify_file(handle, target)
                if after != before or len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                    raise WorkspacePathError("exact unlink hash or identity does not match")
                self._windows.disposition(handle)
            finally:
                self._windows.close(handle)
            if self._windows_identity_at(target) is not None:
                raise WorkspacePathError("exact unlink could not be verified; recovery is required")
            return True


__all__ = [
    "FileIdentity",
    "MAX_AUTHORITY_BYTES",
    "StableRead",
    "WorkspacePathAuthority",
    "WorkspacePathError",
    "WorkspaceProcessLock",
]
