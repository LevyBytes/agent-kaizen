#!/usr/bin/env python3
r"""claude_hook_shim.py -- the thin executable Claude Code's hook command line invokes (v8 M-CLAUDE / M5b).

The `daemon hooks install` verb writes a `.claude/settings.local.json` whose six supported hook
commands invoke this script. Claude runs it PER hook event: it reads the hook JSON on stdin and
forwards it to the live Kaizen supervisor daemon over loopback. PreToolUse/UserPromptSubmit use
`hooks/decide` and may render a synchronous governor decision. SessionStart/Stop/StopFailure/
SessionEnd use record-only `hooks/record`: they emit no stdout and always exit 0, including when
recording fails.

CRITICAL fail-closed contract:
a hook whose executable is MISSING or that CRASHES fails OPEN -- Claude lets the tool proceed. So this
shim must NEVER let an exception escape: it catches EVERYTHING and, in `hooked-strict` mode with the
daemon unreachable (or any internal error), emits an EXPLICIT deny/block + exit 2. `hooked-observe`
mode is PASSIVE: it ALWAYS emits nothing (empty body, exit 0) -- the daemon still computes and records
the decision, but the shim renders no allow/ask/deny/block to Claude (H0 exit criterion), so an
unreachable daemon and a live deny look identical on the wire. Enrichment only; Claude's own gates
apply. The shim is the component that turns "transport failure" into "fail closed", because the
governor decision logic (`hooked.py`) is pure and the daemon may be down.

Usage (the hook command line the installer writes):
  python kaizen_components/orchestration/claude_hook_shim.py --mode hooked-strict --event PreToolUse
  python kaizen_components/orchestration/claude_hook_shim.py --mode hooked-observe --event UserPromptSubmit
  python kaizen_components/orchestration/claude_hook_shim.py --mode hooked-observe --event SessionStart

stdlib only; repo root is found from this file's location so it runs from any cwd. stdout carries ONLY
the single hook JSON line (or empty); all diagnostics go to stderr so Claude's parser sees clean output.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _log(message: str) -> None:
    """Diagnostics to stderr only (stdout is reserved for the hook JSON line)."""
    print(f"[claude_hook_shim] {message}", file=sys.stderr, flush=True)


def _read_stdin_json() -> dict:
    """(nit) Non-obvious contract: empty/whitespace stdin returns `{}` (not an error); otherwise `json.loads` and may raise (caller's guard converts to fail-closed). One line would document the empty->{} behavior. Low value in a terse codebase."""
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def _daemon_decide(hook_event_name: str, payload: dict, *, timeout: float):
    """Forward the hook payload to the live daemon over loopback (op `hooks/decide`). Returns the
    daemon's response dict. Raises DaemonUnreachable when no daemon is listening OR the daemon reports
    it cannot decide (no live policy engine) -- both are transport-class failures the strict path must
    fail closed on.

    Imports are LOCAL so a broken import still lands in the caller's blanket try/except (fail-closed),
    never a bare traceback that fails open."""
    from kaizen_components.orchestration.hooked import DaemonUnreachable
    from kaizen_components.orchestration.supervisor import send_control

    response = send_control(
        op="hooks/decide",
        args={"hook_event_name": hook_event_name, "payload": payload},
        timeout=timeout,
    )
    # send_control returns a clean not-running payload (never raises) when the daemon is down.
    if response.get("running") is False:
        raise DaemonUnreachable("daemon not running")
    status = response.get("status")
    if status == "DENIED":
    # The daemon is up but cannot decide (for example, its policy engine is absent). In strict mode
    # this MUST fail closed -- treat it as unreachable so the caller renders a block.
        raise DaemonUnreachable(str(response.get("code") or "DENIED_HOOK_DECISION"))
    return response


def _daemon_record(hook_event_name: str, payload: dict, *, timeout: float) -> dict:
    """Best-effort observed lifecycle delivery. The caller deliberately ignores the response: a
    record-only hook never emits decision JSON and never returns a failing exit code to Claude."""
    from kaizen_components.orchestration.supervisor import send_control

    return send_control(
        op="hooks/record",
        args={"hook_event_name": hook_event_name, "payload": payload},
        timeout=timeout,
    )


def _outcome_from_response(hook_event_name: str, response: dict, *, strict: bool):
    """Turn the daemon's `hooks/decide` response into a HookOutcome. For PreToolUse the response carries
    a Decision ({result, reason, invariant_id?, rule_id?}); for UserPromptSubmit it carries {block,
    reason}. Rendering (JSON + exit code) reuses hooked.py so the shim and the in-process runner agree."""
    from kaizen_components.orchestration import hooked, policy

    if hook_event_name == hooked.USER_PROMPT_SUBMIT:
        return hooked.userpromptsubmit_output(
            block=bool(response.get("block")), reason=str(response.get("reason") or ""), strict=strict
        )
    decision = policy.Decision(
        result=str(response.get("result") or policy.ASK),
        reason=str(response.get("reason") or ""),
        dedupe_key=str(response.get("dedupe_key") or ""),
        rule_id=response.get("rule_id"),
        invariant_id=response.get("invariant_id"),
    )
    return hooked.pretooluse_output(decision, strict=strict)


def run(mode: str, event: str, *, timeout: float = 5.0) -> int:
    """Read stdin, decide via the daemon, print the hook JSON on stdout, return the exit code. NEVER
    raises: every failure path renders a fail-closed (strict) or pass-through (observe) outcome so the
    hook can never fail OPEN through a crash."""
    # Import here so even an import failure is caught by this function's blanket guard below.
    try:
        from kaizen_components.orchestration import hooked
        from kaizen_components.orchestration.hooked import DaemonUnreachable
    except Exception as error:  # noqa: BLE001 -- an import failure must still fail closed in strict
        return _emergency_fail_closed(mode, event, f"shim import error: {type(error).__name__}: {error}")

    if mode not in hooked.MODES:
        return _emergency_fail_closed(hooked.MODE_STRICT, event, f"invalid hook mode: {mode!r}")
    strict = mode == hooked.MODE_STRICT
    record_only = event in hooked.RECORD_ONLY_EVENTS
    try:
        payload = _read_stdin_json()
    except Exception as error:  # noqa: BLE001 -- unreadable stdin => fail closed (strict) / pass (observe)
        _log(f"stdin read/parse error: {error}")
        if record_only:
            return 0
        return _render(hooked, hooked._unreachable_outcome(event, strict=strict, reason="unreadable hook payload"))

    if record_only:
        try:
            _daemon_record(event, payload, timeout=timeout)
        except Exception as error:  # noqa: BLE001 -- lifecycle recording is enrichment-only
            _log(f"record error (lifecycle unaffected): {type(error).__name__}: {error}")
        return 0

    try:
        response = _daemon_decide(event, payload, timeout=timeout)
    except DaemonUnreachable as unreachable:
        _log(f"daemon unreachable ({unreachable}); mode={mode} => {'fail-closed' if strict else 'pass-through'}")
        return _render(hooked, hooked._unreachable_outcome(event, strict=strict, reason=f"policy daemon unreachable: {unreachable}"))
    except Exception as error:  # noqa: BLE001 -- ANY error is fail-closed in strict (never fail open)
        _log(f"decide error: {type(error).__name__}: {error}")
        return _render(hooked, hooked._unreachable_outcome(event, strict=strict, reason=f"governor error: {type(error).__name__}"))

    try:
        outcome = _outcome_from_response(event, response, strict=strict)
    except Exception as error:  # noqa: BLE001 -- a render error is fail-closed in strict
        _log(f"render error: {type(error).__name__}: {error}")
        return _render(hooked, hooked._unreachable_outcome(event, strict=strict, reason=f"render error: {type(error).__name__}"))
    return _render(hooked, outcome)


def _render(hooked_mod, outcome) -> int:
    """Write the outcome's JSON body (or nothing) to stdout and return its exit code."""
    text = hooked_mod.render(outcome)
    if text:
        sys.stdout.write(text)
        sys.stdout.flush()
    return outcome.exit_code


def _emergency_fail_closed(mode: str, event: str, message: str) -> int:
    """Last-ditch fail-closed when even hooked.py could not be imported: hand-render the block JSON so a
    strict hook still blocks. Observe mode passes through (empty body, exit 0)."""
    _log(message)
    if event in {"SessionStart", "Stop", "StopFailure", "SessionEnd"}:
        return 0
    strict = mode == "hooked-strict"
    if not strict:
        return 0
    if event == "UserPromptSubmit":
        body = {"decision": "block", "reason": "policy governor unavailable (fail-closed)"}
    else:
        body = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": "policy governor unavailable (fail-closed)"}}
    sys.stdout.write(json.dumps(body, ensure_ascii=True))
    sys.stdout.flush()
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude_hook_shim.py",
        description="Claude Code hook shim: govern decision hooks and best-effort record observed lifecycle hooks.",
    )
    parser.add_argument("--mode", default="hooked-strict", choices=["hooked-strict", "hooked-observe"],
                        help="hooked-strict fails CLOSED when the daemon is unreachable; hooked-observe passes through")
    parser.add_argument("--event", required=True, choices=[
                            "SessionStart", "UserPromptSubmit", "PreToolUse", "Stop", "StopFailure", "SessionEnd",
                        ],
                        help="the hook event name (matches the settings.local.json hook block)")
    parser.add_argument("--timeout", type=float, default=5.0, help="loopback timeout seconds")
    # argparse itself can SystemExit(2) on a bad flag; that is a config error at install time, not a
    # runtime hook failure, so we let it surface (the installer validates the command it writes).
    args = parser.parse_args(argv)
    return run(args.mode, args.event, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
