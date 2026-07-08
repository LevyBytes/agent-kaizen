"""Managed local ComfyUI runtime control (Y6).

``provision`` NEVER installs: it validates the runtime and emits the exact installer command
(the owner runs installers). ``start``/``stop`` manage a detached local server process with a
pidfile under ``AI/work/comfyui/``; ``doctor`` composes the ``runtime_profile`` recorded on runs.
No C-drive writes: state lives under the repo, the runtime under ``$DEVROOT``.
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .comfyui import _endpoint, probe
from .db import now
from .denials import KaizenDenied
from .paths import AI_ROOT, REPO_ROOT, repo_relative

_STATE_DIR = AI_ROOT / "work" / "comfyui"
_PIDFILE = _STATE_DIR / "runtime.json"
_ACTIONS = ("status", "provision", "start", "stop", "doctor")


def _runtime_home() -> Path:
    return Path(os.environ.get("KAIZEN_COMFYUI_HOME") or REPO_ROOT.parent / "ComfyUI")


def _venv_dir(home: Path) -> Path:
    """ComfyUI venv location. KAIZEN_COMFYUI_VENV overrides the default ``<home>/.venv`` so the venv
    can live under a custom root (e.g. $DEVROOT/Python/venvs/comfyui) to match a venv layout."""
    override = os.environ.get("KAIZEN_COMFYUI_VENV")
    return Path(override) if override else home / ".venv"


def _venv_python(home: Path) -> Path:
    return _venv_dir(home) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _port(args: Any) -> int:
    import urllib.parse

    return urllib.parse.urlparse(_endpoint(args)).port or 8188


def _pid_alive(pid: int | None) -> bool:
    """Non-destructive existence check. On Windows os.kill(pid, 0) would TerminateProcess,
    so use OpenProcess instead; on POSIX signal 0 is a safe probe."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):  # garbage in a hand-edited pidfile reads as dead, not a crash
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pidfile() -> dict[str, Any] | None:
    if not _PIDFILE.is_file():
        return None
    try:
        data = json.loads(_PIDFILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _write_pidfile(data: dict[str, Any]) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _PIDFILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _reachable(args: Any, probe_timeout: float = 5.0) -> bool:
    # probe_timeout is the per-probe socket cap, kept small and decoupled from --timeout
    # (which governs only the overall poll deadline in start/stop, never a single probe).
    try:
        probe(_endpoint(args), timeout=probe_timeout)
        return True
    except Exception:  # noqa: BLE001 -- unreachable is a valid, non-fatal status signal
        return False


def _missing_denial(home: Path) -> KaizenDenied:
    return KaizenDenied(
        "DENIED_RUNTIME_MISSING",
        {
            "runtime_home": str(home),
            "required_action": (
                "owner runs: setup\\install-comfyui.ps1 -Gpu (or bash setup/install-comfyui.sh --gpu); "
                "RTX 5080 (Blackwell) needs a cu128+ torch wheel -- verify the installer's CUDA index"
            ),
        },
        exit_code=1,
    )


def runtime_status(args: Any) -> dict[str, Any]:
    home = _runtime_home()
    return {
        "status": "OK",
        "action": "status",
        "runtime_home": str(home),
        "venv_dir": str(_venv_dir(home)),
        "main_py": (home / "main.py").is_file(),
        "venv_python": _venv_python(home).is_file(),
        "pidfile": _read_pidfile(),
        "endpoint": _endpoint(args),
        "reachable": _reachable(args),
    }


def runtime_provision(args: Any) -> dict[str, Any]:
    home = _runtime_home()
    if (home / "main.py").is_file() and _venv_python(home).is_file():
        return {"status": "OK", "action": "provision", "provisioned": True, "runtime_home": str(home)}
    raise _missing_denial(home)


def runtime_start(args: Any) -> dict[str, Any]:
    home = _runtime_home()
    if not ((home / "main.py").is_file() and _venv_python(home).is_file()):
        raise _missing_denial(home)
    existing = _read_pidfile()
    if existing and _pid_alive(existing.get("pid")) and _reachable(args):
        raise KaizenDenied(
            "DENIED_RUNTIME_ALREADY_RUNNING",
            {"pid": existing.get("pid"), "endpoint": _endpoint(args), "required_action": "use Y6 --action stop first"},
            exit_code=2,
        )
    port = _port(args)
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = "".join(ch for ch in now() if ch.isdigit())
    log_path = _STATE_DIR / f"server-{stamp}.log"
    flags = 0
    if os.name == "nt":
        # detached + CREATE_NO_WINDOW: a background server must not flash a console (GOTCHA g_20260707024705)
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            [str(_venv_python(home)), "main.py", "--port", str(port), "--listen", "127.0.0.1"],
            cwd=str(home), stdout=log, stderr=subprocess.STDOUT, creationflags=flags,
        )
    _write_pidfile({"pid": proc.pid, "port": port, "started_at": now(), "log": repo_relative(log_path)})
    deadline = time.monotonic() + float(getattr(args, "timeout", None) or 120)
    while time.monotonic() < deadline:
        if _reachable(args, probe_timeout=min(5.0, max(0.1, deadline - time.monotonic()))):
            stats = probe(_endpoint(args), timeout=5)
            system = stats.get("system", {}) if isinstance(stats, dict) else {}
            return {
                "status": "OK",
                "action": "start",
                "pid": proc.pid,
                "port": port,
                "endpoint": _endpoint(args),
                "log": repo_relative(log_path),
                "comfyui_version": system.get("comfyui_version"),
            }
        time.sleep(1.0)
    proc.terminate()
    _PIDFILE.unlink(missing_ok=True)
    raise KaizenDenied(
        "DENIED_RUN_TIMEOUT",
        {"log": repo_relative(log_path), "required_action": "raise --timeout or inspect the server log"},
        exit_code=1,
    )


def runtime_stop(args: Any) -> dict[str, Any]:
    data = _read_pidfile()
    if data is None:
        return {"status": "OK", "action": "stop", "stopped": False, "reason": "no pidfile"}
    pid = data.get("pid")
    if not _pid_alive(pid):
        _PIDFILE.unlink(missing_ok=True)
        return {"status": "OK", "action": "stop", "stopped": False, "stale_pidfile_removed": True}
    try:
        os.kill(int(pid), signal.SIGTERM)  # Windows: maps to TerminateProcess
    except OSError as error:
        _PIDFILE.unlink(missing_ok=True)
        return {"status": "OK", "action": "stop", "stopped": False, "reason": str(error), "stale_pidfile_removed": True}
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _reachable(args, probe_timeout=min(5.0, max(0.1, deadline - time.monotonic()))):
            break
        time.sleep(0.5)
    _PIDFILE.unlink(missing_ok=True)
    return {"status": "OK", "action": "stop", "stopped": True, "pid": pid}


def _torch_report(home: Path) -> dict[str, Any]:
    py = _venv_python(home)
    if not py.is_file():
        return {"torch_version": None, "cuda": None, "torch_error": "venv python not found"}
    try:
        proc = subprocess.run(
            [str(py), "-c", "import torch;print(torch.__version__);print(torch.cuda.is_available())"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as error:  # noqa: BLE001
        return {"torch_version": None, "cuda": None, "torch_error": str(error)}
    if proc.returncode != 0:
        return {"torch_version": None, "cuda": None, "torch_error": (proc.stderr or "").strip()[:300]}
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    version = lines[0] if lines else None
    cuda = lines[1].lower() == "true" if len(lines) > 1 else None
    return {"torch_version": version, "cuda": cuda}


def runtime_doctor(args: Any) -> dict[str, Any]:
    home = _runtime_home()
    stats = probe(_endpoint(args), timeout=float(getattr(args, "timeout", None) or 5))  # capability gate: denies if unreachable
    system = stats.get("system", {}) if isinstance(stats, dict) else {}
    devices = stats.get("devices", []) if isinstance(stats, dict) else []
    torch = _torch_report(home)
    ckpt_dir = home / "models" / "checkpoints"
    checkpoints = sorted(p.name for p in ckpt_dir.glob("*") if p.is_file()) if ckpt_dir.is_dir() else []
    free_bytes = shutil.disk_usage(home).free if home.is_dir() else None
    gpu_name = devices[0].get("name") if devices and isinstance(devices[0], dict) else None
    runtime_profile = (
        f"comfyui={system.get('comfyui_version')};python={system.get('python_version')};"
        f"torch={torch.get('torch_version')};cuda={torch.get('cuda')};gpu={gpu_name}"
    )
    return {
        "status": "OK",
        "action": "doctor",
        "runtime_home": str(home),
        "endpoint": _endpoint(args),
        "comfyui_version": system.get("comfyui_version"),
        "python_version": system.get("python_version"),
        "gpu": gpu_name,
        "devices": devices,
        "checkpoints": checkpoints,
        "free_disk_bytes": free_bytes,
        "runtime_profile": runtime_profile,
        **torch,
    }


_DISPATCH = {
    "status": runtime_status,
    "provision": runtime_provision,
    "start": runtime_start,
    "stop": runtime_stop,
    "doctor": runtime_doctor,
}


def comfy_runtime(args: Any) -> dict[str, Any]:
    """Y6: manage the managed local ComfyUI runtime (bare Y6 defaults to status)."""
    action = (getattr(args, "action", None) or "status").lower()
    if action not in _ACTIONS:
        raise KaizenDenied(
            "DENIED_ACTION_UNKNOWN",
            {"action": action, "allowed": list(_ACTIONS), "required_action": "resubmit with --action from the allowed list"},
            exit_code=2,
        )
    return _DISPATCH[action](args)
