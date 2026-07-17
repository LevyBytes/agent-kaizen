#!/usr/bin/env python3
"""Run ONLY the backend-model tests (the plan's model implementations) -- not the whole harness suite.
For VERIFICATION, and to make a live run VISIBLE in the monitor.

What actually touches the GPU (be precise -- the earlier version overstated this):

  - test_model_integration  -> LIVE: exercises each real backend AS INTENDED. The embedder / reranker /
                               PII models load in-process (torch, on the GPU) but only for a few seconds
                               per op; the Qwen judge is NOT loaded here -- it runs in the Ollama SERVER
                               over HTTP (so it shows in the monitor's Ollama panel, not as a python proc).
  - test_transformers_backend -> loads the 9B into CPU RAM (real weights, slow; not on the GPU).
  - everything else          -> static / mock / deny / unit tests: they do NOT load real weights.

Flow (live): (1) a robust PREFLIGHT so a no-op run explains itself; (2) auto-launch the monitor GUI so
it is open for the WHOLE run; (3) run the backend tests -- ``test_model_integration`` (the live models,
every backend-plan op x its real model) first, ``test_purge_test`` last; (4) a sustained
``B6 --probe --hold`` that loads all four backends and holds them resident in the GUI -- the reliable
"watch it load" moment, since each per-op test load is too brief for a 2s poll (also:
``kaizen.py B6 --probe --hold 30`` standalone); (5) a final K7 that purges any is_test rows the live
tests wrote to the REAL DB (each test also self-cleans in tearDownClass, so this is a belt-and-suspenders
guarantee even if a test crashed before teardown). The held backends free themselves (Ollama evicted,
torch cache emptied) on exit.

Usage:
    tests\\run-backend-tests.cmd
    python tests/run_backend_tests.py
    python tests/run_backend_tests.py --no-live    # skip real-weight loaders (fast, no GPU/heat)
    python tests/run_backend_tests.py --no-gui      # don't auto-launch the monitor GUI
    python tests/run_backend_tests.py --no-hold     # skip the sustained end-of-run held probe
    python tests/run_backend_tests.py --hold-seconds 40
    python tests/run_backend_tests.py -k judge      # only files whose name contains 'judge'

Requires the opt-in extras (requirements-pytorch.txt), HF_HOME at the weight cache (setup/PYTORCH.md),
and Ollama serving the GGUF judge (setup/OLLAMA.md)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TESTS = _REPO / "tests"
_GUI = _REPO / "support_scripts" / "model_monitor_gui.py"
_KAIZEN = _REPO / "kaizen.py"

# The live backend selection the tests use per-op, gathered here so the preflight + the end-of-run held
# probe load the SAME four models the suite exercises (embedder default = F2LLM-v2-1.7B via in-process
# sentence-transformers; text = Qwen via Ollama).
_JUDGE_MODEL = "hf.co/unsloth/Qwen3.5-9B-GGUF:Q4_K_M"
_OLLAMA_BASE = "http://127.0.0.1:11434/v1"
LIVE_BACKEND_ENV = {
    "KAIZEN_EMBED_BACKEND": "sentence-transformers",
    "KAIZEN_RERANK_BACKEND": "sentence-transformers",
    "KAIZEN_RERANK_MODEL": "cross-encoder/ettin-reranker-150m-v1",
    "KAIZEN_PII_BACKEND": "gliner2",
    "KAIZEN_PII_MODEL": "fastino/gliner2-privacy-filter-PII-multi",
    "KAIZEN_LLM_MODEL": _JUDGE_MODEL,
    "KAIZEN_LLM_BASE_URL": _OLLAMA_BASE,
    "KAIZEN_TORCH_DEVICE": "auto",  # GPU-first
}

# (module, loads_real_weights). loads_real_weights=True => skipped by --no-live. Only
# test_model_integration exercises real models ON THE GPU; it runs first so it is observable up front.
BACKEND_TESTS = [
    ("test_model_integration", True),    # LIVE on GPU: embedder/reranker/PII in-process, judge via Ollama
    ("test_transformers_backend", True), # loads the 9B into CPU RAM (real weights, slow)
    ("test_model_pins", False),          # defaults pinned to the approved plan (static)
    ("test_backends", False),            # backend Protocols + factories (mock)
    ("test_backends_live", False),       # embedder plumbing via a MOCK vector (no real weights)
    ("test_pytorch", False),             # sentence-transformers selection / deny paths
    ("test_retrieval", False),           # E4 rerank/hybrid selection + RRF unit (mock / deny)
    ("test_judge", False),               # B4 LLM-as-judge deny / dry-run
    ("test_pii_scan", False),            # B5 PII scan (regex path) + B2/B4 PII-attach wiring
    ("test_dedup", False),               # O5 dedup offline evidence + deny paths
    ("test_purge_test", False),          # is_test marker + K7 purge-test
]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)


def _ok(flag: bool) -> str:
    return "[ok]" if flag else "[--]"


def _gpu_snapshot() -> dict:
    """Imports+returns model_monitor.gpu_snapshot(); note sys.path.insert side effect (F1)."""
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    from kaizen_components.model_monitor import gpu_snapshot

    return gpu_snapshot()


def _ollama_models() -> list[str]:
    """Returns Ollama tag names from <root>/api/tags; [] on any error/unreachable (a reported preflight state, not a crash)."""
    base = os.environ.get("KAIZEN_LLM_BASE_URL", _OLLAMA_BASE)
    root = base.rstrip("/")[:-3] if base.rstrip("/").endswith("/v1") else base.rstrip("/")
    try:
        with urllib.request.urlopen(f"{root}/api/tags", timeout=3) as resp:  # noqa: S310 -- fixed local scheme
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 -- unreachable Ollama is a reported preflight state, not a crash
        return []
    return [m.get("name") or m.get("model") or "" for m in (data.get("models") or [])]


def preflight() -> None:
    """Print a PASS/skip line per live-run prerequisite so a no-op run is self-explaining."""
    print("preflight (live backend prerequisites):")

    for mod in ("torch", "sentence_transformers", "transformers", "gliner2"):
        present = importlib.util.find_spec(mod) is not None
        note = "" if present else "  -> pip install -r requirements-pytorch.txt (its lane will DENY/skip)"
        print(f"  {_ok(present)} extra {mod}{note}")

    hf = os.environ.get("HF_HOME")
    hf_ok = bool(hf) and Path(hf).exists()
    print(f"  {_ok(hf_ok)} HF_HOME {hf or '(unset)'}" + ("" if hf_ok else "  -> weights re-download; see setup/PYTORCH.md"))

    gpu = _gpu_snapshot()
    if gpu.get("available"):
        d = gpu["devices"][0]
        print(f"  [ok] GPU {d.get('name')}  VRAM {d.get('mem_used_mb')}/{d.get('mem_total_mb')} MB  "
              f"device={os.environ.get('KAIZEN_TORCH_DEVICE', 'auto')}")
    else:
        print(f"  [--] GPU nvidia-smi unavailable ({gpu.get('reason', 'not found')})  -> torch lanes fall back to CPU")

    models = _ollama_models()
    if not models:
        print(f"  [--] Ollama unreachable at {_OLLAMA_BASE}  -> the judge (B4) test will DENY; start Ollama (setup/OLLAMA.md)")
    else:
        judge_ok = any("qwen3.5-9b" in m.lower() for m in models)
        note = "" if judge_ok else f"  -> pull it: ollama pull {_JUDGE_MODEL}"
        print(f"  {_ok(judge_ok)} Ollama judge model {'present' if judge_ok else 'NOT pulled'}{note}")
    print()


def launch_gui() -> None:
    """Auto-launch the monitor GUI, detached (venv pythonw on Windows, else sys.executable). Best-effort."""
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        print("monitor GUI: skipped (no display)\n")
        return
    exe = sys.executable
    if os.name == "nt":
        pyw = Path(sys.executable).with_name("pythonw.exe")
        if pyw.exists():
            exe = str(pyw)
    try:
        kwargs: dict = {"cwd": str(_REPO), "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = _NO_WINDOW | _DETACHED
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([exe, str(_GUI), "--interval", "1.5"], **kwargs)
        print("monitor GUI: launched (watch it for the sustained load at the end)\n")
    except Exception as error:  # noqa: BLE001 -- the GUI is a convenience, never fail the run on it
        print(f"monitor GUI: could not launch ({type(error).__name__}); run support_scripts\\model-monitor.cmd\n")


def held_probe(hold_seconds: int) -> None:
    """Load all four configured backends and hold them resident -- the reliable, observable load."""
    env = dict(os.environ)
    env.update(LIVE_BACKEND_ENV)
    print(f"\nSustained load: holding embed/rerank/pii + Qwen judge resident for {hold_seconds}s "
          f"(watch the monitor GUI). Ctrl-C releases early.\n")
    subprocess.run([sys.executable, str(_KAIZEN), "B6", "--probe", "--hold", str(hold_seconds)],
                   cwd=str(_REPO), env=env)


def cleanup_real_db() -> None:
    """Final safety net: purge any is_test rows the live tests wrote to the real DB. Each live test also
    self-cleans in tearDownClass, so this normally reports 0 -- but it guarantees a clean DB even if a
    test crashed before its teardown ran."""
    proc = subprocess.run([sys.executable, str(_KAIZEN), "K7", "--json"],
                          cwd=str(_REPO), capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"cleanup warning: K7 exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        total = json.loads(proc.stdout or "{}").get("total", "?")
    except Exception:  # noqa: BLE001 -- cleanup reporting is best-effort
        total = "?"
    print(f"cleanup: K7 purged {total} real-DB is_test row(s)")


def _gpu_line(label: str) -> None:
    """Prints one-line GPU temp/VRAM/util tagged label; silent no-op if GPU unavailable or on error."""
    try:
        gpu = _gpu_snapshot()
        if gpu.get("available"):
            d = gpu["devices"][0]
            print(f"[{label}] GPU {d.get('temp_c')}C  VRAM {d.get('mem_used_mb')}/{d.get('mem_total_mb')} MB  "
                  f"util {d.get('util_pct')}%")
    except Exception:  # noqa: BLE001 -- the GPU line is a convenience, never fail the run on it
        pass


def main(argv=None) -> int:
    """Argv=None -> argparse reads sys.argv; orchestrates preflight/gui/suite/held-probe/cleanup; returns exit code (0 pass / 1 fail or no-match)."""
    ap = argparse.ArgumentParser(description="Run only the backend-model tests (with a visible sustained load).")
    ap.add_argument("--no-live", action="store_true", help="skip the real-weight loaders (fast, no GPU/heat)")
    ap.add_argument("--no-gui", action="store_true", help="do not auto-launch the monitor GUI")
    ap.add_argument("--no-hold", action="store_true", help="skip the sustained end-of-run held probe")
    ap.add_argument("--hold-seconds", type=int, default=30, help="seconds to hold the backends resident at the end")
    ap.add_argument("-k", metavar="SUBSTR", dest="filter", default="", help="only files whose name contains this")
    args = ap.parse_args(argv)

    selected = [(name, live) for name, live in BACKEND_TESTS if not (args.no_live and live)]
    if args.filter:
        selected = [(name, live) for name, live in selected if args.filter.lower() in name.lower()]
    if not selected:
        print("no matching backend tests")
        return 1

    real = [name for name, live in selected if live]
    if real and not args.no_live:
        os.environ.setdefault("KAIZEN_RUN_LIVE", "1")   # must be set BEFORE the modules import (they read it)
        preflight()
        if not args.no_gui:
            launch_gui()   # open the GUI up front so the whole run (tests + held probe) is observable

    sys.path.insert(0, str(_TESTS))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name, _live in selected:
        suite.addTests(loader.loadTestsFromName(name))

    print(f"backend-model tests: {len(selected)} file(s); real-model tests (GPU/CPU load): "
          f"{', '.join(real) if real else 'none (--no-live)'}")
    _gpu_line("before")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    _gpu_line("after")

    ok = result.wasSuccessful()
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}  "
          f"(ran {result.testsRun}, failures {len(result.failures)}, errors {len(result.errors)}, skipped {len(result.skipped)})")

    # End the live run with the sustained, observable load, then a real-DB cleanup so no is_test row lingers.
    if real and not args.no_live:
        if not args.no_hold:
            held_probe(args.hold_seconds)
        cleanup_real_db()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
