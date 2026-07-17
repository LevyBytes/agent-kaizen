r"""Loopback control channel for the supervisor daemon (v8 M1, plan §D / ledger #19).

Per-OS, owner-only local IPC so the extension/CLI (M8/M14) can drive the daemon:

- Windows: a named pipe ``\\.\pipe\kaizen-<workspace-hash>`` created with an
  owner-only security descriptor via stdlib ctypes (NO pywin32). If pipe creation
  fails, fall back to a loopback TCP listener plus an owner-only token file.
- POSIX: a Unix domain socket at ``<runtime>/control.sock`` chmod 0600, with a
  SO_PEERCRED uid check on every connection. If the workspace path cannot fit
  in the platform's UDS address field, fall back to owner-only loopback TCP.

Wire protocol = JSON-Lines: the client writes one ``{op, args, token, epoch}`` JSON
object terminated by ``\n``; the server replies with one JSON object + ``\n``. Every
request must carry the shared ``token`` (minted at daemon start, stored owner-only under
the runtime dir) or it is rejected with ``DENIED_LOOPBACK_AUTH`` -- this is the auth teeth
the exit criteria (6) assert.

The token file MUST NOT land in a redirected TEMP: ``paths.ensure_runtime_dirs`` rewrites
TEMP/TMP/TMPDIR at import time, so we anchor everything under the repo's own
``AI/work/orchestration/runtime/`` (inside gitignored ``AI/work``), never ``tempfile``.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable

IS_WINDOWS = os.name == "nt"

# One request/response line is small (control ops, not payloads); cap to keep a
# hostile client from streaming an unbounded line into memory.
MAX_LINE_BYTES = 1 << 20  # Public transport-frame contract.
_MAX_LINE_BYTES = MAX_LINE_BYTES
_PIPE_BUFFER = 65536
_MAX_DRAIN_BYTES = _MAX_LINE_BYTES
_OVERSIZE_FRAME = object()


def _payload_too_large() -> dict[str, Any]:
    """Canonical PAYLOAD_TOO_LARGE refusal frame (constant shape)."""
    return {"status": "DENIED", "code": "PAYLOAD_TOO_LARGE", "retryable": False,
            "limit_bytes": _MAX_LINE_BYTES}


def _encode_response(response: dict[str, Any]) -> bytes:
    """Serialize one bounded JSON-Lines response; never emit a frame the peer cannot replay."""
    try:
        payload = (json.dumps(response) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        payload = (json.dumps({"status": "ERROR", "code": "ERROR_LOOPBACK_RESPONSE"}) + "\n").encode("utf-8")
    if len(payload) > _MAX_LINE_BYTES:
        return (json.dumps(_payload_too_large()) + "\n").encode("utf-8")
    return payload

# ctypes surface (Windows named pipe with an owner-only DACL). Imported lazily so
# POSIX never touches it and a broken ctypes build degrades to the TCP fallback.
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.LocalFree.argtypes = [wintypes.LPVOID]
    _kernel32.LocalFree.restype = wintypes.LPVOID
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    _kernel32.ConnectNamedPipe.restype = wintypes.BOOL
    _kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    _kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL
    _kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _kernel32.FlushFileBuffers.restype = wintypes.BOOL
    _kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    _kernel32.WaitNamedPipeW.restype = wintypes.BOOL

    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    PIPE_ACCESS_DUPLEX = 0x00000003
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_READMODE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    PIPE_UNLIMITED_INSTANCES = 255
    ERROR_PIPE_CONNECTED = 535
    ERROR_BROKEN_PIPE = 109
    ERROR_NO_DATA = 232

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    TOKEN_QUERY = 0x0008
    TokenUser = 1

    def _current_user_sid_string() -> str:
        """SID string of the process token's user, for an owner-scoped DACL. The
        creator-owner well-known SID (OW) cannot be assigned as an object owner, so we
        bind the ACE to the concrete user SID instead."""
        _advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
        ]
        token = wintypes.HANDLE()
        proc = _kernel32.GetCurrentProcess()
        if not _advapi32.OpenProcessToken(proc, TOKEN_QUERY, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD(0)
            _advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            if not _advapi32.GetTokenInformation(token, TokenUser, buf, size, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            # TOKEN_USER = SID_AND_ATTRIBUTES; first pointer-sized field is the PSID.
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            str_ptr = ctypes.c_wchar_p()
            _advapi32.ConvertSidToStringSidW.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)
            ]
            if not _advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_ptr)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return str(str_ptr.value)
            finally:
                _kernel32.LocalFree(str_ptr)
        finally:
            _kernel32.CloseHandle(token)

    def _owner_only_security_attributes() -> "SECURITY_ATTRIBUTES":
        # DACL grants Generic-All to the current user SID and to SYSTEM (SY) only. No
        # world/authenticated-users ACE -> another user cannot open the pipe.
        sid = _current_user_sid_string()
        sddl = f"D:(A;;GA;;;{sid})(A;;GA;;;SY)"
        psd = wintypes.LPVOID()
        convert = _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.ULONG),
        ]
        convert.restype = wintypes.BOOL
        if not convert(sddl, 1, ctypes.byref(psd), None):
            raise ctypes.WinError(ctypes.get_last_error())
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = psd
        sa.bInheritHandle = False
        return sa


def workspace_hash(repo_root: Path) -> str:
    """Stable per-workspace id for the pipe name / socket, derived from the resolved
    repo root so two clones never collide on one machine."""
    import hashlib

    text = str(repo_root.resolve()).lower() if IS_WINDOWS else str(repo_root.resolve())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def pipe_name(repo_root: Path) -> str:
    """Return `\\\\.\\pipe\\kaizen-<hash>` for the workspace."""
    return rf"\\.\pipe\kaizen-{workspace_hash(repo_root)}"


class LoopbackServer:
    """Owner-only JSON-Lines control server. ``handler(request_dict) -> response_dict``
    runs per connection; auth (token match) is enforced here, before the handler sees it.

    ``transport`` is one of ``pipe`` | ``uds`` | ``tcp`` so status/tests can assert which
    per-OS path is live. The TCP fallback also writes an owner-only ``<runtime>/control.addr``
    with ``host:port`` so a same-user client can find it without a redirected TEMP hop.
    """

    def __init__(
        self,
        repo_root: Path,
        runtime_dir: Path,
        token: str,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.repo_root = repo_root
        self.runtime_dir = runtime_dir
        self.token = token
        self.handler = handler
        self.transport: str | None = None
        self.address: str | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._sock: socket.socket | None = None
        self._uds_path: Path | None = None
        self._addr_file: Path | None = None
        self._pipe_name: str | None = None

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Per-OS transport order (pipe→tcp on Windows, uds on POSIX); spawns daemon accept thread."""
        if IS_WINDOWS:
            try:
                self._start_pipe()
                return
            except OSError:
                # Named pipe unavailable (rare) -> owner-only loopback TCP + token file.
                self._start_tcp()
                return
        try:
            self._start_uds()
        except OSError:
            # Fall back only when the failed UDS attempt left no endpoint to shadow TCP.
            try:
                (self.runtime_dir / "control.sock").lstat()
            except FileNotFoundError:
                pass
            else:
                raise
            self._start_tcp()

    def stop(self) -> None:
        """Set stop, nudge a pipe accept, join threads, close listeners, and unlink endpoint files."""
        self._stop.set()
        # Socket accepts already wake every 0.5 seconds; only ConnectNamedPipe needs a nudge.
        try:
            if self.transport == "pipe" and self._pipe_name:
                # Open+close our own pipe to release a blocked ConnectNamedPipe.
                try:
                    with open(self._pipe_name, "r+b", buffering=0):
                        pass
                except OSError:
                    pass
        except OSError:
            pass
        for thread in self._threads:
            thread.join(timeout=2.0)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        for path in (self._uds_path, self._addr_file):
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass

    # --- transports --------------------------------------------------------

    def _start_uds(self) -> None:
        """Start the owner-only POSIX UDS listener and set socket mode 0600."""
        path = self.runtime_dir / "control.sock"
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        try:
            sock.bind(str(path))
            bound = True
            os.chmod(path, 0o600)
            sock.listen(16)
            sock.settimeout(0.5)
        except OSError:
            sock.close()
            if bound:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise
        self._sock = sock
        self._uds_path = path
        self.transport = "uds"
        self.address = str(path)
        self._spawn_accept_loop(self._accept_socket_loop)

    def _start_tcp(self) -> None:
        """Start the loopback TCP fallback and publish its owner-only address file."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        addr_file = self.runtime_dir / "control.addr"
        addr_touched = False
        try:
            sock.bind(("127.0.0.1", 0))
            sock.listen(16)
            sock.settimeout(0.5)
            host, port = sock.getsockname()
            address = f"{host}:{port}"
            addr_touched = True
            addr_file.write_text(address, encoding="utf-8")
            _harden_file(addr_file)
            self._sock = sock
            self.transport = "tcp"
            self.address = address
            self._addr_file = addr_file
            self._spawn_accept_loop(self._accept_socket_loop)
        except BaseException:
            try:
                sock.close()
            finally:
                try:
                    if addr_touched:
                        addr_file.unlink()
                except FileNotFoundError:
                    pass
                finally:
                    self._sock = None
                    self.transport = None
                    self.address = None
                    self._addr_file = None
            raise

    def _start_pipe(self) -> None:
        """Start the Windows first-instance named-pipe listener with owner-only security."""
        self._pipe_name = pipe_name(self.repo_root)
        self.transport = "pipe"
        self.address = self._pipe_name
        # Validate we can create the first instance before committing to the transport
        # (raises OSError -> caller falls back to TCP).
        handle = self._create_pipe_instance(first=True)
        self._spawn_accept_loop(lambda: self._accept_pipe_loop(handle))

    def _spawn_accept_loop(self, target: Callable[[], None]) -> None:
        thread = threading.Thread(target=target, name="kaizen-loopback", daemon=True)
        thread.start()
        self._threads.append(thread)

    # --- socket (uds/tcp) accept + serve ----------------------------------

    def _accept_socket_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self._stop.is_set():
                conn.close()
                break
            if self.transport == "uds" and not _peer_is_owner(conn):
                conn.close()
                continue
            threading.Thread(target=self._serve_socket, args=(conn,), daemon=True).start()

    def _serve_socket(self, conn: socket.socket) -> None:
        try:
            line = _read_line_socket(conn)
            if line is None:
                return
            response = _payload_too_large() if line is _OVERSIZE_FRAME else self._dispatch(line)
            conn.sendall(_encode_response(response))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # --- named pipe accept + serve ----------------------------------------

    def _create_pipe_instance(self, first: bool = False):
        """Create one owner-only byte-mode pipe instance and release its temporary DACL."""
        sa = _owner_only_security_attributes()
        open_mode = PIPE_ACCESS_DUPLEX
        if first:
            open_mode |= FILE_FLAG_FIRST_PIPE_INSTANCE
        create_named_pipe = _kernel32.CreateNamedPipeW
        create_named_pipe.restype = wintypes.HANDLE
        create_named_pipe.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        ]
        try:
            handle = create_named_pipe(
                self._pipe_name,
                open_mode,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                PIPE_UNLIMITED_INSTANCES,
                _PIPE_BUFFER,
                _PIPE_BUFFER,
                0,
                ctypes.byref(sa),
            )
        finally:
            _kernel32.LocalFree(sa.lpSecurityDescriptor)
        if handle == INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def _create_pipe_instance_with_retry(self):
        """Create the next listening instance with bounded transient-failure retries."""

        last_error: OSError | None = None
        for attempt in range(5):
            try:
                return self._create_pipe_instance()
            except OSError as error:
                last_error = error
                if self._stop.wait(0.05 * (attempt + 1)):
                    break
        if last_error is not None:
            raise last_error
        raise OSError("named-pipe listener stopped")

    def _accept_pipe_loop(self, first_handle) -> None:
        # Concurrency contract (socket-path parity, fixed 2026-07-10): the NEXT instance is created
        # BEFORE the connected one is served on a worker thread. Serving inline on the sole instance
        # starved every other client for the duration of a handler -- a parked session/events
        # long-poll (up to 25s, and the idle event pump long-polls back-to-back) made the pipe
        # non-existent to WaitNamedPipe, so status/observed/timeline polls flapped DAEMON_UNREACHABLE.
        handle = first_handle
        while not self._stop.is_set():
            connected = _kernel32.ConnectNamedPipe(handle, None)
            err = ctypes.get_last_error()
            if not connected and err not in (0, ERROR_PIPE_CONNECTED):
                _close_handle(handle)
                if self._stop.is_set():
                    return
                try:
                    handle = self._create_pipe_instance_with_retry()
                    continue
                except OSError:
                    return
            if self._stop.is_set():
                _close_handle(handle)
                return
            connected_handle = handle
            try:
                handle = self._create_pipe_instance_with_retry()
            except OSError:
                handle = None
            threading.Thread(
                target=self._serve_pipe_connection, args=(connected_handle,), daemon=True
            ).start()
            if handle is None:
                return

    def _serve_pipe_connection(self, handle) -> None:
        try:
            self._serve_pipe(handle)
        finally:
            _kernel32.DisconnectNamedPipe(handle)
            _close_handle(handle)

    def _serve_pipe(self, handle) -> None:
        try:
            line = _read_line_pipe(handle)
            if line is None:
                return
            response = _payload_too_large() if line is _OVERSIZE_FRAME else self._dispatch(line)
            _write_pipe(handle, _encode_response(response))
        except OSError:
            pass

    # --- dispatch + auth ---------------------------------------------------

    def _dispatch(self, line: bytes) -> dict[str, Any]:
        """Parses/validates JSON, enforces token auth before handler, wraps handler exceptions."""
        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"status": "DENIED", "code": "DENIED_LOOPBACK_MALFORMED"}
        if not isinstance(request, dict):
            return {"status": "DENIED", "code": "DENIED_LOOPBACK_MALFORMED"}
        if request.get("token") != self.token:
            return {"status": "DENIED", "code": "DENIED_LOOPBACK_AUTH",
                    "required_action": "resend with the daemon's control token"}
        try:
            return self.handler(request)
        except Exception as error:  # noqa: BLE001 -- a handler bug must not kill the accept loop
            return {"status": "ERROR", "code": "ERROR_LOOPBACK_HANDLER", "message": str(error)}


# --- client -------------------------------------------------------------------

def send_request(
    repo_root: Path,
    runtime_dir: Path,
    request: dict[str, Any],
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Loopback client used by ``daemon status`` and tests. Resolves the live transport
    (pipe -> uds -> tcp) and returns the parsed response. Raises ``ConnectionError`` when
    no daemon is listening (clean 'not running' path for exit criterion status)."""
    payload = (json.dumps(request) + "\n").encode("utf-8")
    if len(payload) > _MAX_LINE_BYTES:
        return _payload_too_large()
    if IS_WINDOWS:
        name = pipe_name(repo_root)
        if _pipe_exists(name):
            return _client_pipe(name, payload, timeout)
        addr_file = runtime_dir / "control.addr"
        if addr_file.is_file():
            host, port_number = _read_control_address(addr_file)
            return _client_tcp(host, port_number, payload, timeout)
        raise ConnectionError("no loopback transport (pipe/addr) present")
    sock_path = runtime_dir / "control.sock"
    if sock_path.exists():
        return _client_uds(sock_path, payload, timeout)
    addr_file = runtime_dir / "control.addr"
    if addr_file.is_file():
        host, port_number = _read_control_address(addr_file)
        return _client_tcp(host, port_number, payload, timeout)
    raise ConnectionError("no loopback transport (uds/addr) present")


def _read_control_address(path: Path) -> tuple[str, int]:
    """Read one exact loopback TCP address from the owner-only endpoint file."""
    try:
        host, port = path.read_text(encoding="utf-8").strip().rsplit(":", 1)
        port_number = int(port)
    except (OSError, ValueError) as error:
        raise ConnectionError("invalid loopback address file") from error
    if host != "127.0.0.1" or not 1 <= port_number <= 65_535:
        raise ConnectionError("invalid loopback address file")
    return host, port_number


def _client_uds(path: Path, payload: bytes, timeout: float) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(payload)
        return _recv_json_socket(sock)


def _client_tcp(host: str, port: int, payload: bytes, timeout: float) -> dict[str, Any]:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload)
        return _recv_json_socket(sock)


def _client_pipe(name: str, payload: bytes, timeout: float) -> dict[str, Any]:
    # Nonblocking pipe I/O keeps the whole open/write/read exchange under one deadline.
    deadline = time.monotonic() + timeout
    last_err: OSError | None = None
    while time.monotonic() < deadline:
        try:
            fh = open(name, "r+b", buffering=0)
        except OSError as error:
            last_err = error
            time.sleep(0.05)
            continue
        try:
            os.set_blocking(fh.fileno(), False)
            sent = 0
            while sent < len(payload):
                if time.monotonic() >= deadline:
                    raise TimeoutError("named-pipe request timed out during write")
                try:
                    count = fh.write(payload[sent:])
                except BlockingIOError:
                    count = None
                if count:
                    sent += count
                else:
                    time.sleep(0.01)
            buf = b""
            while b"\n" not in buf:
                if time.monotonic() >= deadline:
                    raise TimeoutError("named-pipe request timed out awaiting response")
                try:
                    chunk = fh.read(_PIPE_BUFFER)
                except BlockingIOError:
                    chunk = None
                if chunk is None:
                    time.sleep(0.01)
                    continue
                if not chunk:
                    break
                buf += chunk
                if len(buf) > _MAX_LINE_BYTES:
                    break
            if len(buf) > _MAX_LINE_BYTES:
                return _payload_too_large()
            if b"\n" not in buf:
                raise ConnectionError("named-pipe response ended before a complete frame")
            try:
                return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ConnectionError("named-pipe response was invalid") from error
        finally:
            fh.close()
    raise ConnectionError(f"named pipe not reachable: {last_err}")


# --- low-level helpers --------------------------------------------------------

def _read_line_socket(conn: socket.socket) -> bytes | object | None:
    """Reads one bounded newline frame; returns bytes/_OVERSIZE_FRAME/None; ~2MiB absorb ceiling (F5)."""
    conn.settimeout(5.0)
    buf = b""
    while b"\n" not in buf:
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            return None
        if not chunk:
            return None
        buf += chunk
        if len(buf) > _MAX_LINE_BYTES:
            drained = len(buf)
            while b"\n" not in chunk and drained < _MAX_DRAIN_BYTES:
                try:
                    chunk = conn.recv(min(4096, _MAX_DRAIN_BYTES - drained))
                except socket.timeout:
                    break
                if not chunk:
                    break
                drained += len(chunk)
            return _OVERSIZE_FRAME
    return buf.split(b"\n", 1)[0]


def _recv_json_socket(sock: socket.socket) -> dict[str, Any]:
    """Client bounded read+parse of one response line; can raise on truncation (F3)."""
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > _MAX_LINE_BYTES:
            return _payload_too_large()
    if b"\n" not in buf:
        raise ConnectionError("loopback response ended before a complete frame")
    try:
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConnectionError("loopback response was invalid") from error


def _peer_is_owner(conn: socket.socket) -> bool:
    """UDS peer-cred uid check (Linux SO_PEERCRED). Non-Linux POSIX without the option
    falls back to the 0600 mode already set on the socket path."""
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid == os.getuid()
    except (OSError, AttributeError):
        return True


def _harden_file(path: Path) -> None:
    """POSIX chmod 0600; Windows no-op (token secrecy + gitignored AI/work are the gate)."""
    if IS_WINDOWS:
        # Best-effort ACL lockdown; the token's secrecy is the real gate. icacls is
        # avoided (subprocess in a control path); the file sits under gitignored AI/work.
        return
    os.chmod(path, 0o600)


if IS_WINDOWS:

    def _close_handle(handle) -> None:
        """Close a Windows kernel handle."""
        _kernel32.CloseHandle(handle)

    def _pipe_exists(name: str) -> bool:
        """Note the misleading dead `or last_error==0` fallback (F1)."""
        # 250ms grace covers the server's instant of no-listening-instance between accepting a
        # connection and creating the next spare (the server pre-creates, so this is microseconds).
        return bool(_kernel32.WaitNamedPipeW(name, 250))

    def _read_line_pipe(handle) -> bytes | object | None:
        """Pipe analogue of _read_line_socket; same tri-state return + oversize drain."""
        buf = b""
        chunk = ctypes.create_string_buffer(_PIPE_BUFFER)
        read = wintypes.DWORD(0)
        while b"\n" not in buf:
            ok = _kernel32.ReadFile(handle, chunk, _PIPE_BUFFER, ctypes.byref(read), None)
            if not ok or read.value == 0:
                return buf.split(b"\n", 1)[0] if buf else None
            buf += chunk.raw[: read.value]
            if len(buf) > _MAX_LINE_BYTES:
                drained = 0
                while b"\n" not in chunk.raw[: read.value] and drained < _MAX_DRAIN_BYTES:
                    ok = _kernel32.ReadFile(handle, chunk, _PIPE_BUFFER, ctypes.byref(read), None)
                    if not ok or read.value == 0:
                        break
                    drained += read.value
                return _OVERSIZE_FRAME
        return buf.split(b"\n", 1)[0]

    def _write_pipe(handle, data: bytes) -> None:
        """Write and flush one response frame to a Windows named pipe."""
        written = wintypes.DWORD(0)
        if not _kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None) \
                or written.value != len(data):
            raise ctypes.WinError(ctypes.get_last_error())
        if not _kernel32.FlushFileBuffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())
