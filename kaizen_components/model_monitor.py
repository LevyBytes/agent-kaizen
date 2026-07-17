"""B6 model-monitor: read-only view of which model backends are live right now.

A dev/user monitoring surface for the model-capability layer. It aggregates four cheap, read-only
signals and renders them as a terminal "lane" view (or ``--json`` for machine use):

- GPU snapshot (``nvidia-smi``): temp, fan, util, VRAM, power -- the 'is it hot / how full' panel.
- Ollama loaded models (``/api/ps``): the only persistently resident kaizen backend + keep-alive.
- Configured kaizen lanes (embed / text / rerank / pii): model + requested device, via
  ``describe_backends`` -- what WOULD run on the next op (torch lanes are ephemeral, not resident).
- Recent model activity: the last N model-use traces from the DB, labelled by lane -- ``embedding``
  / ``rerank`` / ``pii`` (from the trace ``name``), text-gen ``model_call`` (B2), and ``judge`` --
  so EVERY model call shows, not just text-gen/judge.

Thermal-safe by construction: it runs NO model inference. ``--watch`` re-renders on an interval
using only these read-only probes; it NEVER loads a model on the loop. ``--probe`` (one-shot, opt-in)
is the only path that instantiates backends (loads weights into VRAM, like B1) to report real
device/dim/reachability.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .backends import (
    describe_backends,
    get_embedding_backend,
    get_pii_backend,
    get_rerank_backend,
    get_text_backend,
)
from .denials import KaizenDenied

_DEFAULT_INTERVAL = 2.0
_MIN_INTERVAL = 0.5
_DEFAULT_RECENT = 5
_GPU_TIMEOUT = 4.0
_OLLAMA_TIMEOUT = 2.0
_GPU_FIELDS = (
    "index",
    "name",
    "temperature.gpu",
    "fan.speed",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "power.draw",
    "power.limit",
)
# Suppress the console window Windows would flash for each nvidia-smi spawn -- otherwise, under a
# no-console host (pythonw GUI, or the --watch loop), a black rectangle blinks every poll AND steals
# focus mid-drag. 0 on POSIX (the flag does not exist there).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------- GPU (nvidia-smi)

def _num(text: str) -> float | int | None:
    """Parse a CSV cell to int/float; blank / `N/A` / `[N/A...]` → None; int when integral."""
    text = text.strip()
    if not text or text.upper().startswith("[N/A") or text.upper() == "N/A":
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


def gpu_snapshot() -> dict[str, Any]:
    """Run the fixed nvidia-smi query; return ``available: False`` when the tool is absent/failing."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={','.join(_GPU_FIELDS)}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=_GPU_TIMEOUT, creationflags=_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as error:
        return {"available": False, "reason": type(error).__name__}
    if proc.returncode != 0:
        return {"available": False, "reason": (proc.stderr or "nvidia-smi non-zero exit").strip()[:200]}
    devices: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) != len(_GPU_FIELDS):
            continue
        devices.append({
            "index": _num(cells[0]),
            "name": cells[1],
            "temp_c": _num(cells[2]),
            "fan_pct": _num(cells[3]),
            "util_pct": _num(cells[4]),
            "mem_used_mb": _num(cells[5]),
            "mem_total_mb": _num(cells[6]),
            "power_w": _num(cells[7]),
            "power_limit_w": _num(cells[8]),
        })
    return {"available": bool(devices), "devices": devices}


# Executables that mean "a model workload", used to filter the GPU process list down from the WDDM
# firehose (explorer, browsers, Discord, ...) to actual AI processes -- ANY project's, no config.
_AI_PROC_HINTS = ("python", "ollama", "llama-server", "llama_server", "comfyui", "koboldcpp", "vllm")


def gpu_processes(exclude_pid: int | None = None) -> dict[str, Any]:
    """Processes using the GPU, filtered to AI-workload executables (python / ollama / llama-server /
    ComfyUI / ...). Cross-project by construction: it reads the driver, not any env, so every project's
    model process shows regardless of how this monitor is configured. Per-process VRAM is frequently
    ``[N/A]`` on Windows WDDM -> reported as None (the header still shows aggregate VRAM). Never raises."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=_GPU_TIMEOUT, creationflags=_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {"available": False, "procs": []}
    if proc.returncode != 0:
        return {"available": False, "procs": []}
    procs: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 3:
            continue
        base = cells[1].replace("\\", "/").split("/")[-1]
        if not any(hint in base.lower() for hint in _AI_PROC_HINTS):
            continue
        try:
            pid = int(cells[0])
        except ValueError:
            continue
        if exclude_pid is not None and pid == exclude_pid:
            continue  # the monitor itself (pythonw) renders on the GPU -> don't list it
        procs.append({"pid": pid, "name": base, "vram_mb": _num(cells[2]), "kind": _proc_kind(base)})
    # Enrich with real per-process usage the WDDM driver denies nvidia-smi (only when there IS an AI
    # process to annotate -- typeperf costs ~1.7s, so the common idle case stays fast).
    if procs:
        usage = gpu_per_process()
        for p in procs:
            stat = usage.get(p["pid"])
            if stat:
                p["gpu_mem_mb"] = stat["gpu_mem_mb"]      # committed dedicated VRAM (Task Manager's column)
                p["gpu_util_pct"] = stat["gpu_util_pct"]  # peak engine utilization %
    return {"available": True, "procs": procs}


def _proc_kind(base: str) -> str:
    """Map an executable basename to a display kind (ollama/comfyui/torch-python/gpu)."""
    low = base.lower()
    if "llama" in low or "ollama" in low:
        return "ollama"
    if "comfy" in low:
        return "comfyui"
    if "python" in low:
        return "torch/python"
    return "gpu"


_PID_RE = re.compile(r"pid_(\d+)_")


def gpu_per_process(timeout: float = 8.0) -> dict[int, dict[str, float]]:
    """Windows/WDDM per-process GPU stats via PDH (typeperf), keyed by PID: ``{pid: {gpu_mem_mb,
    gpu_util_pct}}``. ``gpu_mem_mb`` sums ``\\GPU Process Memory\\Dedicated Usage`` across adapters
    (Task Manager's per-process 'Dedicated GPU memory' -- COMMITTED, can exceed resident); ``gpu_util_pct``
    is the max ``\\GPU Engine\\Utilization Percentage`` across that PID's engines. This is the per-process
    data nvidia-smi returns as ``[N/A]`` under WDDM. Never raises; ``{}`` on any failure (typeperf absent
    -> non-Windows, timeout, localized/absent counters, empty GPU, parse error). Verified non-elevated,
    ~1.7s, on an RTX 5080 (WDDM)."""
    try:
        proc = subprocess.run(
            ["typeperf", r"\GPU Process Memory(*)\Dedicated Usage",
             r"\GPU Engine(*)\Utilization Percentage", "-sc", "1"],
            capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    out = proc.stdout or ""
    if "PDH-CSV" not in out:
        return {}
    try:
        rows = list(csv.reader(io.StringIO(out)))
    except csv.Error:
        return {}
    header = data = None
    for row in rows:
        if row and row[0] == "(PDH-CSV 4.0)":
            header = row
        elif header and row and re.match(r"\d\d/\d\d/\d{4}", row[0]):
            data = row
            break
    if not header or not data:
        return {}
    mem: dict[int, float] = {}
    util: dict[int, float] = {}
    for name, val in zip(header[1:], data[1:]):
        match = _PID_RE.search(name)
        if not match:
            continue
        pid = int(match.group(1))
        low = name.lower()
        if "dedicated usage" in low:            # bytes; sum across LUID/adapters (skip PDH's -1 sentinel)
            value = _num(val)
            if isinstance(value, (int, float)) and value > 0:
                mem[pid] = mem.get(pid, 0.0) + value
        elif "utilization percentage" in low:   # percent; max across engines
            value = _num(val)
            if isinstance(value, (int, float)) and value > util.get(pid, 0.0):
                util[pid] = value
    result: dict[int, dict[str, float]] = {}
    for pid in set(mem) | set(util):
        mb = mem.get(pid, 0.0) / (1024.0 * 1024.0)
        pct = util.get(pid, 0.0)
        if mb > 0.0 or pct > 0.0:
            result[pid] = {"gpu_mem_mb": round(mb, 1), "gpu_util_pct": round(pct, 2)}
    return result


# ------------------------------------------------------------------------- Ollama (/api/ps)

def _ollama_root(base_url: str) -> str:
    """Native API root from an OpenAI-compat base (strip a trailing ``/v1``)."""
    return re.sub(r"/v1/?$", "", base_url.rstrip("/")) or base_url


def _keep_alive(expires_at: str | None) -> str | None:
    """Format an ISO `expires_at` into `Xm Ys` remaining / `expired` / None (fractional seconds ≥7 digits truncate to µs on 3.11+; no parse failure)."""
    if not expires_at:
        return None
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    remaining = int((expiry - datetime.now(timezone.utc)).total_seconds())
    if remaining <= 0:
        return "expired"
    minutes, seconds = divmod(remaining, 60)
    return f"{minutes}m{seconds}s" if minutes else f"{seconds}s"


def ollama_loaded() -> dict[str, Any]:
    """GET {root}/api/ps -> resident models + keep-alive. ``reachable: False`` on any error."""
    base = os.environ.get("KAIZEN_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    root = _ollama_root(base)
    url = f"{root}/api/ps"
    try:
        with urllib.request.urlopen(url, timeout=_OLLAMA_TIMEOUT) as resp:  # noqa: S310 -- fixed local scheme
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        return {"reachable": False, "endpoint": root, "reason": type(error).__name__, "loaded": []}
    loaded: list[dict[str, Any]] = []
    for model in data.get("models", []) if isinstance(data, dict) else []:
        vram = model.get("size_vram")
        loaded.append({
            "model": model.get("name") or model.get("model"),
            "size_vram_mb": round(vram / (1024 * 1024)) if isinstance(vram, (int, float)) else None,
            "expires_at": model.get("expires_at"),
            "keep_alive": _keep_alive(model.get("expires_at")),
        })
    return {"reachable": True, "endpoint": root, "loaded": loaded}


# ---------------------------------------------------------------------------- emergency stop

def _ollama_unload(root: str, model: str) -> None:
    """Ask Ollama to evict a model from VRAM immediately (keep_alive=0)."""
    payload = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
    req = urllib.request.Request(
        f"{root}/api/generate", data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_OLLAMA_TIMEOUT) as resp:  # noqa: S310 -- fixed local scheme
        resp.read()


def _ollama_set_keepalive(root: str, model: str, seconds: int) -> None:
    """Bound a model's residency to ``seconds`` (Ollama evicts it after that). So even a hard
    window-close mid-hold auto-frees VRAM shortly after, without relying on our process cleanup."""
    payload = json.dumps({"model": model, "keep_alive": f"{int(seconds)}s"}).encode("utf-8")
    req = urllib.request.Request(
        f"{root}/api/generate", data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_OLLAMA_TIMEOUT) as resp:  # noqa: S310 -- fixed local scheme
        resp.read()


def _kill_pid(pid: int) -> None:
    """Force-kill a pid (taskkill /F on Windows, kill -9 elsewhere); does NOT check the subprocess exit code (see F2)."""
    cmd = ["taskkill", "/F", "/PID", str(pid)] if os.name == "nt" else ["kill", "-9", str(pid)]
    subprocess.run(cmd, capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW, check=True)


def stop_gpu_models(kill_processes: bool = True) -> dict[str, Any]:
    """EMERGENCY: free the GPU now. Unload every Ollama-resident model (keep_alive=0) and, when
    ``kill_processes``, terminate the GPU AI processes (torch/python/comfy/llama-server) that hold
    VRAM. Returns what was stopped; never raises (collects per-target errors). The monitor's own pid
    is excluded so it does not kill itself."""
    result: dict[str, Any] = {"status": "OK", "ollama_unloaded": [], "processes_killed": [], "errors": []}
    ol = ollama_loaded()
    root = ol.get("endpoint")
    if ol.get("reachable") and root:
        for model in ol.get("loaded", []):
            name = model.get("model")
            if not name:
                continue
            try:
                _ollama_unload(root, name)
                result["ollama_unloaded"].append(name)
            except Exception as error:  # noqa: BLE001 -- best-effort emergency stop
                result["errors"].append(f"ollama {name}: {type(error).__name__}")
    if kill_processes:
        for proc in gpu_processes(exclude_pid=os.getpid()).get("procs", []):
            pid = proc.get("pid")
            if not isinstance(pid, int):
                continue
            try:
                _kill_pid(pid)
                result["processes_killed"].append({"pid": pid, "name": proc.get("name")})
            except Exception as error:  # noqa: BLE001
                result["errors"].append(f"kill {pid}: {type(error).__name__}")
    result["total"] = len(result["ollama_unloaded"]) + len(result["processes_killed"])
    return result


# ---------------------------------------------------------------------------- recent activity

def recent_activity(limit: int) -> list[dict[str, Any]]:
    """Last N model_call/judge traces (read-only). Returns [] on any DB error (monitor stays alive)."""
    try:
        from .db import fetch_all

        rows = fetch_all(
            "SELECT id, created_at, kind, name, model, provider, latency_ms FROM trace_events "
            "WHERE kind IN ('model_call', 'judge') ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        )
    except Exception:  # noqa: BLE001 -- a monitor never fails on a read
        return []
    # `lane` is the display label: the embedder/reranker/PII traces carry it in `name`
    # (embedding/rerank/pii); text-gen (B2) has no name -> 'model_call'; judge -> its kind.
    return [
        {"id": r[0], "created_at": r[1], "kind": r[2], "lane": r[3] or r[2], "model": r[4],
         "provider": r[5], "latency_ms": r[6]}
        for r in rows
    ]


# ---------------------------------------------------------------------------- optional probe

def _backend_factories() -> dict[str, Any]:
    """Resolve backend factories at call time so tests and runtime configuration can patch them."""
    return {
        "embed": get_embedding_backend,
        "text": get_text_backend,
        "rerank": get_rerank_backend,
        "pii": get_pii_backend,
    }


def _probe_lanes(lanes: list[dict[str, Any]]) -> None:
    """One-shot: instantiate each configured backend and merge its real probe() into the lane.

    LOADS MODEL WEIGHTS (VRAM, like B1) -- opt-in via --probe, never on the --watch loop.
    """
    factories = _backend_factories()
    for lane in lanes:
        if not lane.get("configured"):
            continue
        factory = factories.get(lane["lane"])
        if factory is None:
            continue
        try:
            backend = factory()
            if backend is None:
                continue
            lane["probe"] = backend.probe()
            lane["reachable"] = True
        except KaizenDenied as denied:
            lane["reachable"] = False
            lane["probe_reason"] = denied.payload().get("reason")
        except Exception as error:  # noqa: BLE001 -- probing is best-effort
            lane["reachable"] = False
            lane["probe_reason"] = str(error)[:200]


def _probe_hold(args: Any, hold: int, interval: float, *, as_json: bool) -> dict[str, Any]:
    """``--probe --hold SEC``: load every configured backend and KEEP the instances referenced so their
    weights stay resident in VRAM for ``hold`` seconds -- a sustained, observable load (the per-op test
    loads are too brief for a poll to catch). Re-renders the live GPU/Ollama panels each ``interval``
    (it does NOT reload -- the held refs hold the weights). ``Ctrl-C`` releases early. Loads weights
    (like B1); reached only via the opt-in ``--probe``."""
    factories = _backend_factories()
    raw_limit = getattr(args, "limit", None)
    limit = int(_DEFAULT_RECENT if raw_limit is None else raw_limit)
    held: list[Any] = []  # references keep the in-process torch weights from being freed during the hold
    ollama_targets: list[tuple[str, str]] = []  # (root, model) of Ollama models the probe loaded
    lanes = describe_backends()
    for lane in lanes:
        if not lane.get("configured"):
            continue
        factory = factories.get(lane["lane"])
        if factory is None:
            continue
        try:
            backend = factory()
            if backend is None:
                continue
            lane["probe"] = backend.probe()  # loads weights into VRAM (torch) or the Ollama server
            lane["reachable"] = True
            held.append(backend)
            if lane.get("backend") == "ollama" and lane.get("model"):
                root = _ollama_root(lane.get("transport") or os.environ.get("KAIZEN_LLM_BASE_URL", ""))
                ollama_targets.append((root, lane["model"]))
                try:  # bound residency to the hold so a hard window-close still auto-frees VRAM
                    _ollama_set_keepalive(root, lane["model"], max(1, hold) + 15)
                except Exception:  # noqa: BLE001 -- keep-alive tuning is best-effort
                    pass
        except KaizenDenied as denied:
            lane["reachable"] = False
            lane["probe_reason"] = denied.payload().get("reason")
        except Exception as error:  # noqa: BLE001 -- one lane failing must not abort the hold
            lane["reachable"] = False
            lane["probe_reason"] = str(error)[:200]

    def snap() -> dict[str, Any]:
        return {
            "status": "OK",
            "gpu": gpu_snapshot(),
            "gpu_processes": gpu_processes(exclude_pid=os.getpid()),
            "ollama": ollama_loaded(),
            "backends": lanes,
            "recent": recent_activity(limit),
        }

    held_count = len(held)
    try:
        if as_json:
            time.sleep(max(0, hold))  # hold resident, then emit one snapshot
            result = snap()
            result["held"] = held_count
            return result

        _enable_vt()
        end = time.monotonic() + max(0, hold)
        try:
            while True:
                remaining = max(0, int(round(end - time.monotonic())))
                frame = render(snap(), interval=None)
                footer = f"\nHOLDING {held_count} backend(s) resident -- {remaining}s left (Ctrl-C to release)"
                sys.stdout.write("\x1b[H\x1b[2J" + frame + footer + "\n")
                sys.stdout.flush()
                if time.monotonic() >= end:
                    break
                time.sleep(min(interval, max(0.0, end - time.monotonic())))
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            sys.stdout.flush()
        return {"status": "OK", "message": f"probe-hold complete; released {held_count} backend(s)."}
    finally:
        _release_held(held, ollama_targets)  # ALWAYS free VRAM on the way out (end / Ctrl-C / error)


def _release_held(held: list[Any], ollama_targets: list[tuple[str, str]]) -> None:
    """Free everything a ``--hold`` probe loaded: evict the Ollama models it touched (keep_alive=0) and
    drop the in-process torch backends, then empty the CUDA cache. Best-effort; never raises."""
    for root, model in ollama_targets:
        try:
            _ollama_unload(root, model)
        except Exception:  # noqa: BLE001 -- eviction is best-effort (keep-alive is the backstop)
            pass
    held.clear()
    torch = sys.modules.get("torch")  # only if a torch backend was actually loaded (don't import it here)
    if torch is not None:
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------- aggregate + render

def collect(args: Any) -> dict[str, Any]:
    """Aggregate the four read-only signals into a snapshot dict; runs `_probe_lanes` only when `args.probe`."""
    raw_limit = getattr(args, "limit", None)
    limit = int(_DEFAULT_RECENT if raw_limit is None else raw_limit)
    lanes = describe_backends()
    if getattr(args, "probe", False):
        _probe_lanes(lanes)
    return {
        "status": "OK",
        "gpu": gpu_snapshot(),
        "gpu_processes": gpu_processes(exclude_pid=os.getpid()),
        "ollama": ollama_loaded(),
        "backends": lanes,
        "recent": recent_activity(limit),
    }


def _lane_state(lane: dict[str, Any], loaded_names: set[str]) -> str:
    """Derive a lane display state (off/ready/unreachable/loaded/idle/armed) from config + probe + Ollama residency."""
    if not lane.get("configured"):
        return "off"
    if "reachable" in lane:  # --probe ran
        return "ready" if lane["reachable"] else "unreachable"
    if lane.get("backend") == "ollama":
        model = lane.get("model") or ""
        normalized = {name.removesuffix(":latest") for name in loaded_names}
        return "loaded" if model.removesuffix(":latest") in normalized else "idle"
    return "armed"  # in-process torch lane: loads per op, not resident


def render(snapshot: dict[str, Any], *, interval: float | None) -> str:
    """Render a snapshot dict to the multi-line terminal view; `interval` labels watch vs one-shot."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = f"watch {interval:g}s" if interval else "one-shot"
    lines = [f"Agent Kaizen -- backend monitor   {now}   [{mode}]"]

    gpu = snapshot["gpu"]
    if not gpu.get("available"):
        lines.append(f"GPU:  nvidia-smi unavailable ({gpu.get('reason', 'not found')})")
    else:
        for dev in gpu["devices"]:
            used, total = dev.get("mem_used_mb"), dev.get("mem_total_mb")
            fan = dev.get("fan_pct")
            lines.append(
                f"GPU{dev.get('index', 0)} {dev.get('name', '?')}  "
                f"{_fmt(dev.get('temp_c'), 'C')}  fan {_fmt(fan, '%')}  util {_fmt(dev.get('util_pct'), '%')}  "
                f"VRAM {used}/{total} MB  {_fmt(dev.get('power_w'), 'W')}/{_fmt(dev.get('power_limit_w'), 'W')}"
            )

    ol = snapshot["ollama"]
    loaded_names = {m["model"] for m in ol.get("loaded", []) if m.get("model")}

    # --- What is actually running on the GPU right now (system-wide, any project, no config) ---
    lines.append("")
    lines.append("Live GPU model processes (system-wide):")
    procs = snapshot.get("gpu_processes", {})
    ai = procs.get("procs", []) if procs.get("available") else []
    if not procs.get("available"):
        lines.append("  nvidia-smi unavailable")
    elif not ai:
        lines.append("  none (no python/ollama/comfyui process on the GPU)")
    else:
        for p in ai:
            lines.append(
                f"  [{p.get('kind', 'gpu'):<12}] pid {str(p.get('pid', '?')):<7} {p.get('name', '?'):<22} "
                f"{_proc_mem(p):>10}  util {_fmt(p.get('gpu_util_pct'), '%')}"
            )

    lines.append("")
    if not ol.get("reachable"):
        lines.append(f"Ollama-resident models (system-wide):  unreachable at {ol.get('endpoint', '?')}")
    elif not ol.get("loaded"):
        lines.append("Ollama-resident models (system-wide):  none resident")
    else:
        lines.append("Ollama-resident models (system-wide):")
        for m in ol["loaded"]:
            vram = f"{m['size_vram_mb']} MB" if m.get("size_vram_mb") is not None else "?"
            ka = f"expires in {m['keep_alive']}" if m.get("keep_alive") else "keep-alive ?"
            lines.append(f"  {m.get('model', '?'):<38} {vram:<9} {ka}")

    lines.append("")
    recent = snapshot["recent"]
    if not recent:
        lines.append("Recent Kaizen model calls:  none recorded")
    else:
        lines.append("Recent Kaizen model calls:")
        for r in recent:
            ts = (r.get("created_at") or "")[11:19] or (r.get("created_at") or "")
            lat = f"{r['latency_ms']}ms" if r.get("latency_ms") is not None else "-"
            lane = r.get("lane") or r.get("kind", "?")
            lines.append(f"  {ts}  {str(lane):<11} {str(r.get('model', '?')):<28} {lat}")

    # Local project backend config (what THIS repo would run) -- a footer, not the live signal.
    lines.append("")
    if not any(lane.get("configured") for lane in snapshot["backends"]):
        lines.append("This project's backend config: none set (embed/text/rerank/pii unconfigured)")
    else:
        summary = "  ".join(f"{lane['lane']}={_lane_state(lane, loaded_names)}" for lane in snapshot["backends"])
        lines.append(f"This project's backend config:  {summary}")

    return "\n".join(lines)


def _fmt(value: Any, unit: str) -> str:
    """Value+unit`, or `?unit` when value is None."""
    return f"{value}{unit}" if value is not None else f"?{unit}"


def _proc_mem(p: dict[str, Any]) -> str:
    """Per-process GPU memory label: perf-counter committed MB, else nvidia-smi's value, else n/a."""
    mb = p.get("gpu_mem_mb")
    if mb is None:
        mb = p.get("vram_mb")
    return f"{mb} MB" if mb is not None else "mem n/a"


# ---------------------------------------------------------------------------- watch loop

def _enable_vt() -> None:
    """Enable ANSI/VT processing on a Windows console so the clear-screen codes render."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:  # noqa: BLE001 -- best-effort; fall back to no clear
        pass


def _watch(args: Any, interval: float) -> dict[str, Any]:
    """Blocking watch loop: re-render `collect(args)` every `interval`, clear-screen each frame, exit on Ctrl-C."""
    _enable_vt()
    try:
        while True:
            frame = render(collect(args), interval=interval)
            sys.stdout.write("\x1b[H\x1b[2J" + frame + "\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
    return {"status": "OK", "message": "backend monitor stopped."}


# ---------------------------------------------------------------------------- entry point

def model_monitor(args: Any) -> dict[str, Any]:
    """B6: run probe-hold when requested, otherwise TTY-watch, JSON snapshot, or one-shot rendered snapshot in that precedence; ordinary collection is read-only and runs no inference."""
    as_json = bool(getattr(args, "json", False))
    watch = bool(getattr(args, "watch", False))
    hold = int(getattr(args, "hold", 0) or 0)
    interval = max(_MIN_INTERVAL, float(getattr(args, "interval", None) or _DEFAULT_INTERVAL))

    # --probe --hold: sustained, observable load (loads weights, then holds them resident).
    if getattr(args, "probe", False) and hold > 0:
        return _probe_hold(args, hold, interval, as_json=as_json)

    # --watch needs a TTY to clear/redraw; piped/redirected (and every test) falls back to one-shot.
    if watch and not as_json and sys.stdout.isatty():
        return _watch(args, interval)

    snapshot = collect(args)
    if as_json:
        return snapshot
    snapshot["message"] = render(snapshot, interval=None)
    return snapshot
