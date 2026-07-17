#!/usr/bin/env python
"""Deterministic end-to-end live verification of the ComfyUI Y6-Y9 harness.

Runs the whole flow with no manual id copying and prints PASS/FAIL per step:
  Y6 doctor -> Y8 --validate (approval gate) -> Y8 --route api -> Y2 inspect
  --mcp also runs: Y7 bakeoff -> Y8 --route mcp -> Y9 api-vs-mcp parity

Prerequisites: ComfyUI installed + running (python kaizen.py Y6 --action start) with a checkpoint in
place, KAIZEN_COMFYUI_VENV set if the venv is off the default, and for --mcp the optional client
(pip install "mcp>=1.0,<2") plus Node >= 22. All runs use --test; the records are purged at the end
unless --keep is passed. Inherits os.environ, so KAIZEN_COMFYUI_VENV/KAIZEN_COMFYUI_URL flow through.
The required --workflow selects the API-format workflow; --template, --prompt, and --seed customize the run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
KAIZEN = REPO_ROOT / "kaizen.py"


def _first_json_object(text: str) -> dict | None:
    """Return the first JSON object embedded in a stream, ignoring surrounding log text."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def kz(*args: str) -> tuple[int, dict]:
    """Run a kaizen op --json and return its code plus the first object in stdout or stderr."""
    proc = subprocess.run(
        [sys.executable, str(KAIZEN), *args, "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(REPO_ROOT),
    )
    out = (proc.stdout or "").strip()
    payload = _first_json_object(out) or _first_json_object(proc.stderr or "")
    if payload is not None:
        return proc.returncode, payload
    err = proc.stderr or ""
    return proc.returncode, {"status": "ERROR", "_raw": (out or err)[-400:]}


_PASS, _FAIL = "PASS", "FAIL"
_results: list[tuple[str, str, str]] = []


def step(name: str, ok: bool, detail: str) -> bool:
    """Record (name,PASS/FAIL,detail), print, return ok."""
    _results.append((name, _PASS if ok else _FAIL, detail))
    print(f"[{_PASS if ok else _FAIL}] {name}: {detail}")
    return ok


def main(argv: list[str] | None = None) -> int:
    """Parse args, run the live Y6->Y8->Y2 (+MCP Y7->Y8->Y9) flow, purge is_test unless --keep, return 0 iff all steps pass."""
    ap = argparse.ArgumentParser(description="Live-verify the ComfyUI Y6-Y9 harness end to end.")
    ap.add_argument("--workflow", required=True, help="API-format workflow with a {{PROMPT}} placeholder")
    ap.add_argument("--template", default="txt2img")
    ap.add_argument("--prompt", default="a red bicycle on a beach at sunset")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--mcp", action="store_true", help="also run the MCP lane (needs the mcp client + Node)")
    ap.add_argument("--keep", action="store_true", help="keep the is_test records instead of purging them")
    a = ap.parse_args(argv)
    wf = ["--template", a.template, "--workflow-file", a.workflow]
    gen = wf + ["--prompt", a.prompt, "--seed", str(a.seed), "--test"]

    # 1. runtime doctor
    rc, d = kz("Y6", "--action", "doctor")
    step("Y6 doctor", d.get("status") == "OK",
         f"rc={rc} cuda={d.get('cuda')} gpu={d.get('gpu')} checkpoints={d.get('checkpoints')}")

    # 2. api generation with live /object_info validation, then auto-inspect (no manual id copy).
    # If the checkpoint is absent this denies DENIED_ASSET_MISSING -- the approval gate doing its job.
    rc, d = kz("Y8", *gen, "--route", "api", "--validate")
    api_id = d.get("id")
    if d.get("code") in ("DENIED_ASSET_MISSING", "DENIED_WORKFLOW_NODES_UNKNOWN"):
        step("Y8 --route api", False,
             f"rc={rc} approval gate fired ({d.get('code')}): place the model and re-run -- {d.get('missing_assets') or d.get('unknown_class_types')}")
    else:
        step("Y8 --route api", d.get("status") == "OK" and d.get("run_status") == "completed",
             f"rc={rc} run_status={d.get('run_status')} id={api_id} outputs={d.get('outputs')}")
    if api_id:
        rc, d = kz("Y2", "--id", api_id)
        r = d.get("record", {})
        ri = r.get("route_info") or {}
        step("Y2 inspect", r.get("status") == "completed" and ri.get("route") == "api",
             f"rc={rc} route={ri.get('route')} runtime_profile={ri.get('runtime_profile')}")
    else:
        step("Y2 inspect", False, "skipped because Y8 returned no run id")

    if a.mcp:
        # 4. bakeoff -> pin winner
        rc, d = kz("Y7", "--action", "bakeoff", "--test")
        step("Y7 bakeoff", d.get("status") == "OK" and bool(d.get("winner")),
             f"rc={rc} winner={d.get('winner')} candidates={d.get('candidates')}")
        # 5. mcp generation
        # The approval gate is demonstrated once on the API lane; MCP avoids a duplicate gate.
        rc, d = kz("Y8", *gen, "--route", "mcp")
        step("Y8 --route mcp", d.get("status") == "OK" and d.get("run_status") == "completed",
             f"rc={rc} run_status={d.get('run_status')} route={d.get('route')} outputs={d.get('outputs')}")
        # 6. A/B parity
        rc, d = kz("Y9", *gen)
        p = d.get("parity", {})
        step("Y9 A/B parity", d.get("status") == "OK" and p.get("both_completed"),
             f"rc={rc} identical_hashes={p.get('identical_hashes')} latency={p.get('latency_ms')} report={d.get('report')}")

    # cleanup
    if not a.keep:
        rc, d = kz("K7")
        print(f"[cleanup] K7 rc={rc} purged={d.get('purged')} total={d.get('total')}")

    passed = sum(1 for _, s, _ in _results if s == _PASS)
    total = len(_results)
    print(f"\n=== {passed}/{total} steps PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
