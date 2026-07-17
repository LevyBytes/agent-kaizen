r"""Hooked governor over the official Claude Code extension (v8 M-CLAUDE / M5b, plan §5.4).

A SECONDARY code-invariant net that runs INSIDE Claude Code's own hook chokepoints
(`PreToolUse`, `UserPromptSubmit`) and routes every gated action through the ONE
:func:`policy.decide` seam every lane shares. It is the hooked-extension analogue of the
M5a `claude -p` permission-prompt-tool adapter: same policy engine, different surface.

Ground truth (probe `AI/work/orchestration-v8/probes/P-A-claude/verdict.md`, folded into
`M-CLAUDE-declaration.md`):

- PreToolUse ``exit 2`` blocks a tool BEFORE Claude's permission-prompt-tool even runs,
  so this net genuinely precedes the primary gate. The missing-executable probe FAILS OPEN -- the
  tool proceeds -- so the *shim* (`kaizen_components/orchestration/claude_hook_shim.py`) must implement
  fail-closed ITSELF when the daemon is unreachable; a crash is not a block.
- The ``@file`` probe inlines file content with ZERO tool call / ZERO permission request, so a
  protected-path read via ``@`` never reaches PreToolUse or the perm tool. UserPromptSubmit is
  the ONLY chokepoint for it, and it blocks via ``exit 2`` / ``{"decision":"block"}``.
- Hook payload probe: PreToolUse ``{cwd, hook_event_name, permission_mode, session_id,
  tool_input, tool_name, tool_use_id, transcript_path}``; UserPromptSubmit ``{cwd,
  hook_event_name, permission_mode, prompt, session_id, transcript_path}``. ``transcript_path``
  is archive-only (never parsed as a schema; the ``--fork-session`` dual-writer hazard, §9 row
  19, is why).

Two enforcement modes (`mode=`):

- ``hooked-strict`` -- ENFORCING. The computed decision is rendered to Claude (allow/ask/deny,
  block) and a daemon-UNREACHABLE ⇒ FAIL-CLOSED (block, exit 2). Every gated action must clear
  the live policy engine or it does not run.
- ``hooked-observe`` -- PASSIVE. The decision is still computed (the daemon is still called, the
  ledger still records) but NOTHING is emitted to Claude: every render is an EMPTY body + exit 0,
  so the governor never allows/asks/denies/blocks a Claude action here (H0 exit criterion). A
  daemon-unreachable is likewise a silent pass-through. Enrichment only; Claude's own gates apply.

Output contract (Claude Code hook JSON -- STRICT mode only; OBSERVE always renders an empty body):

- PreToolUse allow ⇒ ``{"hookSpecificOutput":{"hookEventName":"PreToolUse",
  "permissionDecision":"allow"}}`` (exit 0). deny ⇒ ``permissionDecision:"deny"`` + reason AND
  ``exit 2`` (the precedes-the-gate block proven by the live probe). ask ⇒ ``permissionDecision:"ask"``.
- UserPromptSubmit block ⇒ ``{"decision":"block","reason":...}`` + ``exit 2``.

Purity: this module is record-plane + stdlib only. It imports :mod:`policy` (pure logic),
NOT subprocess/socket. The shipped shim owns loopback transport and renders the daemon's
``_hooks_decide`` response through this module's output helpers; tests inject an in-process
PolicyEngine directly into the same pure action/guard primitives.

Supersession: this live hooked path retired the earlier bridge-only observer.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

from . import policy

# --- modes ------------------------------------------------------------------------------------

MODE_STRICT = "hooked-strict"
MODE_OBSERVE = "hooked-observe"
MODES: tuple[str, ...] = (MODE_STRICT, MODE_OBSERVE)

# Hook event names on the wire.  PreToolUse/UserPromptSubmit are decision-bearing governor hooks;
# the other four are record-only lifecycle hooks used by the H2.5 observed transcript lane.
SESSION_START = "SessionStart"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
PRE_TOOL_USE = "PreToolUse"
STOP = "Stop"
STOP_FAILURE = "StopFailure"
SESSION_END = "SessionEnd"
HOOK_EVENTS: tuple[str, ...] = (
    SESSION_START,
    USER_PROMPT_SUBMIT,
    PRE_TOOL_USE,
    STOP,
    STOP_FAILURE,
    SESSION_END,
)
DECISION_EVENTS: tuple[str, ...] = (USER_PROMPT_SUBMIT, PRE_TOOL_USE)
RECORD_ONLY_EVENTS: tuple[str, ...] = (SESSION_START, STOP, STOP_FAILURE, SESSION_END)

# Exit codes. 0 = the decision rides the JSON body (Claude reads permissionDecision / decision);
# 2 = a hard block that precedes the permission-prompt-tool. 1 stays a generic error.
EXIT_OK = 0
EXIT_BLOCK = 2


# --- tool -> verb mapping ----------------------------------------------------------------------
# Claude tool_name -> the vendor-neutral policy verb + which tool_input key carries the target(s).
# Mapping to file_write / file_read / exec / net / spawn is what lets the code INVARIANTS (git-push,
# protected-path, vendor-config) fire on a Write's file_path or a Bash command. Anything unmapped is a
# generic 'tool' verb (still gated -- default-deny asks). The value is (verb, target_key, command_key):
# target_key/command_key are None when that dimension does not apply.
_TOOL_VERBS: dict[str, tuple[str, str | None, str | None]] = {
    "Write": ("file_write", "file_path", None),
    "Edit": ("file_write", "file_path", None),
    "MultiEdit": ("file_write", "file_path", None),
    "NotebookEdit": ("file_write", "notebook_path", None),
    "Read": ("file_read", "file_path", None),
    "NotebookRead": ("file_read", "notebook_path", None),
    "Bash": ("exec", None, "command"),
    "BashOutput": ("exec", None, "command"),
    "WebFetch": ("net", "url", None),
    "WebSearch": ("net", "url", None),
    "Task": ("spawn", None, "prompt"),
    "Agent": ("spawn", None, "prompt"),
}

# tool_input keys that may ALSO carry a path target for a generic tool (defense in depth: an MCP tool
# that writes a file surfaces its path so protected-path can still fire).
_GENERIC_PATH_KEYS: tuple[str, ...] = ("file_path", "path", "notebook_path", "target_file")


def _targets_from_input(tool_input: Mapping[str, Any], key: str | None) -> tuple[str, ...]:
    """Pull scalar target strings for ``key`` from ``tool_input``; nested mappings are ignored."""
    if key is None:
        return ()
    value = tool_input.get(key)
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if isinstance(v, (str, int, float)) and not isinstance(v, bool))
    if isinstance(value, bool):
        return ()
    return (str(value),)


def action_from_pretooluse(payload: Mapping[str, Any], *, engine: str, epoch: int) -> policy.RequestedAction:
    """Build a :class:`policy.RequestedAction` from a PreToolUse hook payload. The Actor is the hook's
    session_id at ``epoch`` (the caller supplies the epoch-current value; a stale epoch is the daemon's
    to hard-deny via INV_STALE_EPOCH). ``raw`` carries ``cwd`` so a relative Write path canonicalizes."""
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping):
        tool_input = {}
    verb, target_key, command_key = _TOOL_VERBS.get(tool_name, ("tool", None, None))

    targets = _targets_from_input(tool_input, target_key)
    # A generic tool may still carry a path -> surface it so protected-path/vendor-config can fire.
    if verb == "tool" and not targets:
        extra: list[str] = []
        for key in _GENERIC_PATH_KEYS:
            extra.extend(_targets_from_input(tool_input, key))
        targets = tuple(extra)

    command = None
    if command_key is not None:
        cmd_val = tool_input.get(command_key)
        command = str(cmd_val) if cmd_val is not None else None

    tool_use_id = payload.get("tool_use_id")
    actor = policy.Actor(
        engine=engine,
        session_id=str(payload.get("session_id") or ""),
        epoch=epoch,
        thread_id=str(tool_use_id) if tool_use_id else None,
    )
    raw: dict[str, Any] = {
        "cwd": payload.get("cwd"),
        "tool_name": tool_name,
        "tool_input": dict(tool_input),
        "tool_use_id": tool_use_id,
        "permission_mode": payload.get("permission_mode"),
        "transcript_path": payload.get("transcript_path"),
    }
    return policy.RequestedAction(actor=actor, verb=verb, targets=targets, command=command, raw=raw)


# --- decision -> Claude hook output ------------------------------------------------------------

@dataclass(frozen=True)
class HookOutcome:
    """A rendered hook result: the JSON body Claude reads on stdout + the process exit code.
    ``blocked`` is a convenience flag (deny/block) for the caller's event funnel/logging."""

    body: dict[str, Any]
    exit_code: int
    blocked: bool
    decision: str  # allow | deny | ask | block | passthrough


def observe_passthrough(decision_label: str, *, blocked: bool) -> HookOutcome:
    """The OBSERVE-mode render for a COMPUTED decision: empty body + exit 0 (emit nothing to Claude)
    while RETAINING the true decision label on the outcome. Observe is passive -- the decision is
    computed and recorded but never rendered, so the governor cannot allow/ask/deny/block a Claude
    action (H0 exit criterion). ``blocked`` carries the deny/block truth through for the caller's
    event funnel/logging; ``decision_label`` is the real allow|ask|deny|block."""
    return HookOutcome({}, EXIT_OK, blocked=blocked, decision=decision_label)


def pretooluse_output(decision: policy.Decision, *, strict: bool) -> HookOutcome:
    """Render a PreToolUse :class:`policy.Decision` to the Claude hook JSON + exit code.

    STRICT (enforcing): allow ⇒ permissionDecision allow, exit 0. ask ⇒ permissionDecision ask,
    exit 0. deny ⇒ permissionDecision deny + reason AND exit 2 (the block that precedes the perm
    tool, as verified by the live probe). The reason surfaces the invariant/rule id so the denial is traceable in Claude's
    transcript.

    OBSERVE (passive): NOTHING is rendered -- an EMPTY body + exit 0 for every decision -- but the
    HookOutcome RETAINS the true computed decision (allow/ask/deny) for callers/tests. The governor
    never emits allow/ask/deny JSON to Claude in observe mode (H0 exit criterion).
    """
    if not strict:
        return observe_passthrough(decision.result, blocked=decision.result == policy.DENY)
    result = decision.result
    inner: dict[str, Any] = {"hookEventName": PRE_TOOL_USE, "permissionDecision": result}
    if result == policy.DENY:
        inner["permissionDecisionReason"] = _reason_text(decision)
        # strict-only path: a deny exits 2 (the block that precedes the permission tool).
        return HookOutcome({"hookSpecificOutput": inner}, EXIT_BLOCK, blocked=True, decision=policy.DENY)
    if result == policy.ASK:
        inner["permissionDecisionReason"] = _reason_text(decision)
        return HookOutcome({"hookSpecificOutput": inner}, EXIT_OK, blocked=False, decision=policy.ASK)
    # allow
    return HookOutcome({"hookSpecificOutput": inner}, EXIT_OK, blocked=False, decision=policy.ALLOW)


def userpromptsubmit_output(*, block: bool, reason: str, strict: bool) -> HookOutcome:
    """Render a UserPromptSubmit guard result.

    STRICT (enforcing): a block emits ``{"decision":"block","reason":...}`` (the documented
    prompt-block shape) AND exits 2; a pass emits an empty body, exit 0 (the prompt proceeds).

    OBSERVE (passive): NOTHING is rendered -- an EMPTY body + exit 0 whether or not the guard would
    block -- but the HookOutcome RETAINS the true decision (block|allow) for callers/tests. The
    governor never emits a block to Claude in observe mode (H0 exit criterion)."""
    if not strict:
        return observe_passthrough("block" if block else "allow", blocked=block)
    if block:
        return HookOutcome({"decision": "block", "reason": reason}, EXIT_BLOCK, blocked=True, decision="block")
    return HookOutcome({}, EXIT_OK, blocked=False, decision="allow")


def fail_closed_output(hook_event_name: str, *, reason: str) -> HookOutcome:
    """The STRICT-mode fail-closed result when the daemon is UNREACHABLE. A provider hook error
    otherwise fails OPEN, so the shim renders THIS explicit deny + exit 2 instead of
    crashing). Shaped per the hook event so Claude reads a real block, not a malformed body."""
    if hook_event_name == USER_PROMPT_SUBMIT:
        return HookOutcome({"decision": "block", "reason": reason}, EXIT_BLOCK, blocked=True, decision="block")
    inner = {"hookEventName": PRE_TOOL_USE, "permissionDecision": policy.DENY, "permissionDecisionReason": reason}
    return HookOutcome({"hookSpecificOutput": inner}, EXIT_BLOCK, blocked=True, decision=policy.DENY)


def passthrough_output(hook_event_name: str) -> HookOutcome:
    """The generic OBSERVE-mode pass-through when NO decision was computed -- the daemon-unreachable
    case (via ``_unreachable_outcome``): empty body, exit 0, decision="passthrough". The governor
    adds nothing and Claude's own gates apply. NEVER weakens Claude (it emits no allow), it abstains.
    (A COMPUTED observe decision instead renders via :func:`observe_passthrough`, which keeps the true
    allow/ask/deny/block label; this generic form is only for the no-decision transport-fail path.)"""
    return HookOutcome({}, EXIT_OK, blocked=False, decision="passthrough")


def _reason_text(decision: policy.Decision) -> str:
    """Format a decision reason with its invariant or rule trace tag when present."""
    trace = decision.invariant_id or decision.rule_id
    return f"{decision.reason} [{trace}]" if trace else decision.reason


# --- @-ref / prompt guard (UserPromptSubmit) ---------------------------------------------------
# `@file` inlines file content with 0 tool-call / 0 permission request: the ONLY chokepoint for a
# protected-path read-via-@ is here. The guard blocks a prompt whose @-refs resolve under a protected
# prefix (the same prefixes the policy engine loads). A bare @-ref to a non-protected path is allowed
# (over-blocking every @ would break normal use); the engine's protected set is the gate.

# Match an @-reference token: @ followed by a path-ish run (letters/digits/sep/dot/dash/underscore/colon).
# Coarse by design -- it over-captures harmlessly (a stray @ in prose yields a token that simply won't
# canonicalize under a protected prefix).
_AT_REF_RE = re.compile(r"(?<!\w)@([A-Za-z0-9_./\\:\-~]+)")


def extract_at_refs(prompt: str) -> list[str]:
    """The @-referenced path tokens in a prompt's provider inlining surface."""
    return [m.group(1) for m in _AT_REF_RE.finditer(prompt or "")]


def guard_prompt(
    prompt: str,
    *,
    engine: policy.PolicyEngine,
    cwd: str | None = None,
) -> tuple[bool, str]:
    """Decide whether a UserPromptSubmit prompt must be BLOCKED. Returns (block, reason). Blocks when
    any @-ref resolves under a protected prefix (the @-inline read bypass). The engine's
    public guarded-target predicate uses the SAME protected sets the PreToolUse path enforces, so the two
    chokepoints agree. A prompt with no protected @-ref is not blocked here."""
    refs = extract_at_refs(prompt)
    for ref in refs:
        canon = policy.canonicalize_path(os.path.expanduser(ref), cwd=cwd)
        if engine.is_guarded_target(canon):
            return True, (
                f"@-reference to a protected/vendor-config path is blocked (@{ref}); "
                "protected content cannot be inlined into a prompt (bypasses the tool gate)"
            )
    return False, ""


def render(outcome: HookOutcome) -> str:
    """The stdout string for a HookOutcome. An empty body renders '' (a pass-through emits no JSON so
    Claude's parser treats it as no-op); otherwise a compact JSON object. Callers write this to stdout
    and exit ``outcome.exit_code``. stdout stays a single clean JSON line (or empty), never log noise."""
    if not outcome.body:
        return ""
    return json.dumps(outcome.body, ensure_ascii=True, separators=(",", ":"))
