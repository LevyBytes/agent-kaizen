#!/usr/bin/env python3
"""Session-scoped Codex PreToolUse defense-in-depth shim.

The adapter injects this command through thread/start.config; no vendor config file is written. The
shim reads one hook payload from stdin, calls the authenticated Kaizen daemon with an opaque gate id
bound to the active immutable policy snapshot, and emits the synchronous Codex hook decision. Any
transport, payload, or helper failure denies with exit 2. The Codex sandbox plus approval channel is
the enforcement boundary; this hook is an additional invariant check only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_SENSITIVE_ENV = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "KAIZEN_CODEX_API_KEY_FILE", "KAIZEN_CLAUDE_API_KEY_FILE",
)


def _log(message: str) -> None:
    print(f"[codex_hook_shim] {message}", file=sys.stderr, flush=True)


def _deny(reason: str) -> tuple[dict[str, Any], int]:
    """Builds the canonical fail-closed PreToolUse deny body + exit-code-2 tuple (the single shape used by every failure path)."""
    return ({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, 2)


def _send(gate_id: str, profile_hash: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    from kaizen_components.orchestration.supervisor import send_control

    return send_control(
        op="session/policy-check",
        args={"gate_id": gate_id, "profile_hash": profile_hash, "payload": payload},
        timeout=timeout,
    )


def run(
    gate_id: str,
    profile_hash: str,
    *,
    timeout: float = 5.0,
    read_payload: Callable[[], Any] | None = None,
    send: Callable[[str, str, dict[str, Any], float], dict[str, Any]] | None = None,
    write: Callable[[str], Any] | None = None,
) -> int:
    """Execute one hook decision, including the credential-boundary check; injection points keep the fail-closed contract hermetically testable."""
    read_payload = read_payload or (lambda: json.loads(sys.stdin.buffer.read().decode("utf-8")))
    send = send or _send
    flush_stdout = write is None
    write = write or sys.stdout.write
    try:
        if any(name in os.environ for name in _SENSITIVE_ENV):
            raise RuntimeError("credential boundary unavailable")
        payload = read_payload()
        if not gate_id or not profile_hash or not isinstance(payload, dict):
            raise ValueError("invalid hook identity or payload")
        response = send(gate_id, profile_hash, payload, timeout)
        if response.get("status") != "OK":
            raise RuntimeError(str(response.get("code") or "policy gate unavailable"))
        raw_decision = response.get("result")
        decision = raw_decision if isinstance(raw_decision, str) else ""
        if decision not in ("allow", "ask", "deny"):
            raise ValueError("invalid daemon decision")
        # A synchronous Codex hook cannot safely park for Kaizen's C4 approval UI.
        # An ASK at this defense-in-depth layer therefore denies; the app-server's
        # native approval channel remains the only interactive approval path.
        if decision == "ask":
            decision = "deny"
            response = {**response, "reason": "interactive approval must use the native Codex approval channel"}
        body: dict[str, Any] = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
            }
        }
        reason = response.get("reason")
        if reason:
            body["hookSpecificOutput"]["permissionDecisionReason"] = str(reason)
        exit_code = 2 if decision == "deny" else 0
    except Exception as error:  # noqa: BLE001 -- a hook crash would fail open; always render a real deny
        _log(f"policy gate failure: {type(error).__name__}")
        body, exit_code = _deny("Kaizen session policy gate unavailable (fail-closed)")
    write(json.dumps(body, ensure_ascii=True))
    if flush_stdout:
        sys.stdout.flush()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Parse the process arguments and delegate the complete fail-closed decision to run."""
    parser = argparse.ArgumentParser(description="Codex PreToolUse to Kaizen session policy gate")
    parser.add_argument("--gate-id", required=True)
    parser.add_argument("--profile-hash", required=True)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)
    return run(args.gate_id, args.profile_hash, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
