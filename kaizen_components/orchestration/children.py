"""Child process ownership and env composition (v8 M1, plan §A.3 / ledger #10).

Every vendor/tool child the supervisor spawns is OWNED so that a daemon crash or
close reaps the whole tree -- never leaving orphaned grandchildren (Continue #2896,
Kilo #8571):

- Windows: a Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` via ctypes
  (``CreateJobObjectW`` / ``SetInformationJobObject`` / ``AssignProcessToJobObject``).
  When the daemon exits and the last handle to the job closes, the kernel terminates
  every process in the job -- child AND grandchild. NO pywin32.
- POSIX: a new process group (``start_new_session=True``); ``kill_tree`` signals the
  whole group.

The Job Object is an OPTIMIZATION; the boot orphan-sweep (supervisor.py) is the
invariant that guarantees truth even if the reap loses a race.

Env composition (§A.3, D4 billing-flip defense): every child env strips
``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` / ``PYTHONIOENCODING``, then sets
``PYTHONUTF8=1`` and ``PYTHONIOENCODING=utf-8``. Popen uses ``encoding='utf-8',
errors='replace'`` so a stray byte can never crash the daemon's stream reader.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any

IS_WINDOWS = os.name == "nt"

STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "PYTHONIOENCODING")
FORCE_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


class ChildTerminationError(RuntimeError):
    """The owned process tree could not be proven terminated."""


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    CREATE_SUSPENDED = 0x00000004
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.IsProcessInJob.restype = wintypes.BOOL
    _kernel32.IsProcessInJob.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL),
    ]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]


def compose_child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return a child env with the API keys / stale encoding stripped and UTF-8 forced.
    Pure and side-effect-free (never mutates ``os.environ``)."""
    env = dict(os.environ if base is None else base)
    for key in STRIP_ENV:
        env.pop(key, None)
    env.update(FORCE_ENV)
    return env


class OwnedChild:
    """A spawned child owned by a Windows Job Object or a POSIX process group.

    ``process`` is the ``subprocess.Popen``. On Windows a job handle is kept on the
    instance; closing it (``release``) or the daemon exiting terminates the whole tree.
    """

    def __init__(self, process: subprocess.Popen, job_handle: Any | None) -> None:
        self.process = process
        self._job_handle = job_handle
        # ``start_new_session=True`` makes the POSIX root PID the process-group ID.
        # Retain it because a short-lived launcher may exit while descendants remain.
        self._process_group_id = None if IS_WINDOWS else process.pid

    @property
    def pid(self) -> int:
        return self.process.pid

    def poll(self) -> int | None:
        """Return None while running, otherwise the child root's exit code."""
        return self.process.poll()

    def owns_pid(self, pid: int) -> bool:
        """Check current tree membership; callers must pair PID evidence with their retained nonce."""
        if type(pid) is not int or pid <= 0:
            return False
        if IS_WINDOWS:
            return self._job_handle is not None and _pid_in_windows_job(self._job_handle, pid)
        try:
            return os.getpgid(pid) == self._process_group_id
        except OSError:
            return False

    def kill_tree(self, timeout: float = 5.0) -> None:
        """Terminate the child tree and return only after its root is proven dead."""
        if self.process.poll() is not None:
            self.release()
            return
        if IS_WINDOWS:
            # Closing the job handle triggers KILL_ON_JOB_CLOSE for the whole tree.
            self.release()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:
                    self.process.wait(timeout=timeout)
                except subprocess.TimeoutExpired as error:
                    raise ChildTerminationError(f"child {self.pid} remained alive after forced termination") from error
        else:
            try:
                os.killpg(self._process_group_id, signal.SIGTERM)
            except ProcessLookupError:
                if self.process.poll() is None:
                    raise ChildTerminationError(f"child {self.pid} process group was unavailable")
            except PermissionError as error:
                raise ChildTerminationError(f"child {self.pid} process group could not be terminated") from error
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self._process_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    if self.process.poll() is None:
                        raise ChildTerminationError(
                            f"child {self.pid} process group disappeared before forced termination"
                        )
                except PermissionError as error:
                    raise ChildTerminationError(
                        f"child {self.pid} process group could not be force-terminated"
                    ) from error
                try:
                    self.process.wait(timeout=timeout)
                except subprocess.TimeoutExpired as error:
                    raise ChildTerminationError(f"child {self.pid} remained alive after forced termination") from error
        if self.process.poll() is None:
            raise ChildTerminationError(f"child {self.pid} termination was not proven")

    def release(self) -> None:
        """Close the retained job handle (Windows). With KILL_ON_JOB_CLOSE and no other
        handle open, this terminates the tree. No-op on POSIX."""
        if IS_WINDOWS and self._job_handle is not None:
            _close_windows_job(self._job_handle)
            self._job_handle = None


def spawn_owned(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stdin: Any = subprocess.DEVNULL,
    stdout: Any = subprocess.PIPE,
    stderr: Any = subprocess.PIPE,
) -> OwnedChild:
    """Spawn ``cmd`` as an owned child. Env is always composed (§A.3) unless ``env`` is
    provided pre-composed; pass ``compose_child_env()`` explicitly to layer extra vars."""
    child_env = compose_child_env() if env is None else env
    if IS_WINDOWS:
        return _spawn_windows(cmd, cwd, child_env, stdin, stdout, stderr)
    return _spawn_posix(cmd, cwd, child_env, stdin, stdout, stderr)


def _spawn_posix(cmd, cwd, env, stdin, stdout, stderr) -> OwnedChild:
    """One-liner: "POSIX owned spawn: new session => own process group for kill_tree."."""
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,  # own process group -> kill_tree reaps descendants
    )
    return OwnedChild(process, None)


def _spawn_windows(cmd, cwd, env, stdin, stdout, stderr) -> OwnedChild:
    """One-liner: "Windows owned spawn: CREATE_SUSPENDED -> assign-to-job -> resume, with rollback."."""
    job = _create_kill_on_close_job()
    # Start suspended so no grandchild can spawn before the process is in the job;
    # CREATE_NEW_PROCESS_GROUP keeps our Ctrl+C out of the child's group.
    creationflags = CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
    except Exception:
        _close_windows_job(job)
        raise
    try:
        _assign_to_job(job, process.pid)
        _resume_main_thread(process)
    except Exception:
        _cleanup_failed_windows_spawn(process, job)
        raise
    return OwnedChild(process, job)


def _cleanup_failed_windows_spawn(process: subprocess.Popen, job: Any, timeout: float = 5.0) -> None:
    """Rollback a post-Popen Windows ownership failure without leaving a suspended child."""
    close_error: OSError | None = None
    try:
        if process.poll() is None:
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
    finally:
        try:
            _close_windows_job(job)
        except OSError as error:
            close_error = error
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise ChildTerminationError(
                f"child {process.pid} remained alive after failed Windows spawn rollback"
            ) from error
    if process.poll() is None:
        raise ChildTerminationError(f"child {process.pid} Windows spawn rollback was not proven")
    if close_error is not None:
        raise ChildTerminationError(f"child {process.pid} Windows job handle could not be closed") from close_error


if IS_WINDOWS:

    def _close_windows_job(job: Any) -> None:
        if not _kernel32.CloseHandle(job):
            raise ctypes.WinError(ctypes.get_last_error())

    def _create_kill_on_close_job():
        """Create a Windows job whose members terminate when its final handle closes."""
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            _kernel32.CloseHandle(job)
            raise ctypes.WinError(ctypes.get_last_error())
        return job

    def _assign_to_job(job, pid: int) -> None:
        """Assign a suspended child before resume so descendants cannot escape the job."""
        handle = _kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _kernel32.AssignProcessToJobObject(job, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _kernel32.CloseHandle(handle)

    def _pid_in_windows_job(job: Any, pid: int) -> bool:
        """Return whether ``pid`` is in the exact retained Job Object; never infer by ancestry."""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            result = wintypes.BOOL(False)
            if not _kernel32.IsProcessInJob(handle, job, ctypes.byref(result)):
                return False
            return bool(result.value)
        finally:
            _kernel32.CloseHandle(handle)

    def _resume_main_thread(process: subprocess.Popen) -> None:
        """Resume the suspended child's threads through Toolhelp because Popen retains no thread handle."""
        _resume_all_threads(process.pid)

    TH32CS_SNAPTHREAD = 0x00000004
    THREAD_SUSPEND_RESUME = 0x0002
    ERROR_NO_MORE_FILES = 18

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    _kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]

    def _resume_all_threads(pid: int) -> None:
        """Resume every enumerated child thread or raise when none can be proven resumed."""
        snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if snap == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        resumed = False
        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(THREADENTRY32)
            if not _kernel32.Thread32First(snap, ctypes.byref(entry)):
                raise ctypes.WinError(ctypes.get_last_error())
            while True:
                if entry.th32OwnerProcessID == pid:
                    thread = _kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if _kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        resumed = True
                    finally:
                        _kernel32.CloseHandle(thread)
                if not _kernel32.Thread32Next(snap, ctypes.byref(entry)):
                    error = ctypes.get_last_error()
                    if error not in (0, ERROR_NO_MORE_FILES):
                        raise ctypes.WinError(error)
                    break
        finally:
            _kernel32.CloseHandle(snap)
        if not resumed:
            raise ChildTerminationError(f"child {pid} main thread could not be resumed")


def pid_alive(pid: int) -> bool:
    """True iff a process with ``pid`` currently exists. Used by the orphan-sweep to
    decide whether a recorded owner is dead (pid+start-nonce, never pid alone)."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        WAIT_OBJECT_0 = 0x00000000
        WAIT_TIMEOUT = 0x00000102
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            wait = _kernel32.WaitForSingleObject(handle, 0)
            if wait == WAIT_OBJECT_0:
                return False
            if wait == WAIT_TIMEOUT:
                return True
            return True
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
