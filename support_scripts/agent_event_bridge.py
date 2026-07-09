#!/usr/bin/env python3
"""agent_event_bridge.py -- feed NORMALIZED orchestration events into the Kaizen ledger (T5/T6).

The bridge is the ONLY sanctioned path from an agent runtime into the agent-run ledger. It reads
normalized JSONL (one event per line) and shells out to `kaizen.py T5`/`T6`; it never touches the DB
directly and never scrapes a VS Code webview/DOM. UI text is a projection -- authoritative lifecycle
comes from a CLI/app-server/event stream (Codex App Server `item/*` + `serverRequest/resolved`;
Claude Code hooks). Wiring a live listener for those feeds is out of v1 scope; the vendor->marker maps
below are the reference contract for whoever builds one.

Normalized JSONL line shapes:
  {"op":"run_start","run_key":"vendor-run-1","summary":"...","envelope":{"agent_type":"claude","surface":"vscode-extension"}}
  {"op":"event","run_key":"vendor-run-1","event":{"event_kind":"subagent","marker":"open","correlation_id":"c1","source_event_id":"v-9","summary":"child started"}}
  {"op":"event","agent_run_id":"ar_...","event":{...}}   # reference an already-open run directly

`run_key` is the vendor's run id; the bridge maps it to the minted `agent_run_id` so later events
resolve. `source_event_id` makes replay idempotent (T6 INSERT OR IGNORE on (run, source_event_id)).

Usage (stdlib only; run from anywhere -- repo root found via this file's location):
  python support_scripts/agent_event_bridge.py ingest events.jsonl --json
  cat events.jsonl | python support_scripts/agent_event_bridge.py ingest - --json
  python support_scripts/agent_event_bridge.py ingest events.jsonl --test   # write removable is_test rows
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KAIZEN = REPO_ROOT / "kaizen.py"


# Reference vendor->(event_kind, marker) maps. NOT wired to a live listener in v1; documented so a
# future bridge feed translates authoritative events (never UI text) into normalized lines above.
# Claude Code hooks (observe-only; the hook's stdin JSON carries agent_id / tool_name / request id):
CLAUDE_HOOK_MAP: dict[str, tuple[str, str]] = {
    "SubagentStart": ("subagent", "open"),
    "SubagentStop": ("subagent", "close_ok"),          # close_fail when the hook reports an error
    "PreToolUse": ("tool_call", "open"),
    "PostToolUse": ("tool_call", "close_ok"),
    "PostToolUseFailure": ("tool_call", "close_fail"),
    "PermissionRequest": ("approval", "open"),
    "PreCompact": ("context", "point"),
    "Stop": ("turn", "close_ok"),
    "StopFailure": ("turn", "close_fail"),
    "TaskCompleted": ("finalization", "close_ok"),
}
# Codex App Server (`item/*` notifications are the source of truth; `serverRequest/resolved` closes an
# approval): item.started->open, item.completed->close_ok, item.failed->close_fail by item type.
CODEX_ITEM_MAP: dict[str, str] = {
    "started": "open",
    "completed": "close_ok",
    "failed": "close_fail",
    "canceled": "close_canceled",
    "resolved": "resolved",
    "declined": "declined",
}


def _kaizen(args: list[str]) -> dict:
    proc = subprocess.run(
        [sys.executable, str(KAIZEN), *args, "--json"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    out = (proc.stdout or "").strip()
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        payload = {"status": "ERROR", "raw": out, "stderr": (proc.stderr or "").strip()}
    payload["_rc"] = proc.returncode
    return payload


def _lines(source: str):
    if source == "-":
        for line in sys.stdin:
            if line.strip():
                yield line
        return
    for line in Path(source).read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield line


def ingest(source: str, *, is_test: bool) -> dict:
    run_map: dict[str, str] = {}
    started: list[dict] = []
    events: list[dict] = []
    errors: list[dict] = []
    test_flag = ["--test"] if is_test else []
    for lineno, raw in enumerate(_lines(source), 1):
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append({"line": lineno, "error": f"invalid JSON: {exc}"})
            continue
        op = rec.get("op")
        if op == "run_start":
            envelope = dict(rec.get("envelope") or {})
            call = ["T5", "--summary", rec.get("summary", "agent run"),
                    "--payload-json", json.dumps(envelope), *test_flag]
            result = _kaizen(call)
            if result.get("status") == "OK":
                run_key = rec.get("run_key")
                if run_key:
                    run_map[run_key] = result["id"]
                started.append({"line": lineno, "run_key": rec.get("run_key"), "agent_run_id": result["id"]})
            else:
                errors.append({"line": lineno, "op": op, "denied": result})
        elif op == "event":
            run_id = rec.get("agent_run_id") or run_map.get(rec.get("run_key"))
            if not run_id:
                errors.append({"line": lineno, "op": op, "error": "unresolved run_key/agent_run_id"})
                continue
            result = _kaizen(["T6", "--agent-run-id", run_id,
                              "--payload-json", json.dumps(rec.get("event") or {}), *test_flag])
            if result.get("status") == "OK":
                events.append({"line": lineno, "agent_run_id": run_id, "id": result.get("id"),
                               "deduplicated": result.get("deduplicated", False)})
            else:
                errors.append({"line": lineno, "op": op, "denied": result})
        else:
            errors.append({"line": lineno, "error": f"unknown op: {op!r} (want run_start|event)"})
    return {
        "status": "OK" if not errors else "PARTIAL",
        "runs_started": len(started),
        "events_added": len(events),
        "deduplicated": sum(1 for e in events if e.get("deduplicated")),
        "errors": errors,
        "started": started,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Feed normalized orchestration JSONL into the Kaizen ledger (T5/T6).")
    sub = parser.add_subparsers(dest="command", required=True)
    ing = sub.add_parser("ingest", help="ingest normalized JSONL (file path or - for stdin)")
    ing.add_argument("source", help="JSONL file path, or - for stdin")
    ing.add_argument("--test", action="store_true", help="write removable is_test=1 rows (K7 purge-test)")
    ing.add_argument("--json", action="store_true", help="emit the ingest summary as JSON")
    args = parser.parse_args(argv)
    result = ingest(args.source, is_test=args.test)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"runs_started={result['runs_started']} events_added={result['events_added']} "
              f"deduplicated={result['deduplicated']} errors={len(result['errors'])}")
    return 0 if result["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
