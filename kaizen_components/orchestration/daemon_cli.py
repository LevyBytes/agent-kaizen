"""``kaizen.py daemon`` subcommand (v8 M1 runtime; M14 control verbs).

``daemon run`` starts the per-workspace supervisor (single-instance, boot orphan-sweep,
loopback control channel, owned children); ``daemon status`` is a loopback client. The M14
control verbs -- ``steer``, ``approve``, ``cancel``, ``attach``, ``dispatch-poll``,
``orchestrate`` -- are ALSO thin loopback clients (they resolve the control token like
``status`` and send one JSON-lines request to the live daemon; a clean 'not running' payload
when no daemon listens, never a traceback). These are the open-source, user-controlled ingress
the Control-Ingress invariant mandates and the real F4 prerequisite (plan §D).

NONE of these verbs is an operation code in ``args.ALIASES``/``REGISTRY``: the daemon is a
long-lived process manager, not a record verb, so it sits OUTSIDE the public op-coverage matrix
by design (documented daemon rationale, plan §4). The D7 ``remote-dispatch`` op IS the public
record verb; these subcommands are its live loopback drivers.

``run`` accepts ``--exit-after-boot`` (an undocumented test seam): boot + sweep + exit,
so a CLI-level test can exercise the invariant without backgrounding a live daemon.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..output import emit, emit_error

# `session events` long-polls up to SESSION_LONG_POLL_MAX_S (25s) server-side; the loopback client
# timeout MUST exceed that (send_control default is ~5s) or a legitimate long-poll would time out on the
# wire before the server returns. 30s = the 25s cap + connect/read slack.
_SESSION_EVENTS_TIMEOUT = 30.0
_ANSWERS_FILE_MAX_BYTES = 256 * 1024


def _load_answers_file(path: str) -> dict[str, object]:
    """Read one bounded UTF-8 JSON object for requestUserInput answer transport."""
    raw = Path(path).read_bytes()
    if len(raw) > _ANSWERS_FILE_MAX_BYTES:
        raise ValueError("answers file exceeds 256 KiB")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("answers file must contain a JSON object with string question ids")
    return value


def build_daemon_parser() -> argparse.ArgumentParser:
    """Returns the fully-populated top-level `daemon` argparse parser (run/status + M14 control verbs + H0 session subtree + hooks subtree). Low priority; signature self-describing."""
    parser = argparse.ArgumentParser(
        prog="kaizen.py daemon",
        description="Kaizen supervisor daemon: owns vendor children truthfully (M1); live control verbs (M14).",
        allow_abbrev=False,
    )
    common = argparse.ArgumentParser(add_help=False)
    # Keep --json valid after any subcommand without letting an omitted inner copy overwrite an
    # explicitly true outer value. argparse.SUPPRESS leaves the earlier parser's value untouched.
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="emit machine-readable JSON")
    sub = parser.add_subparsers(dest="daemon_command")
    run = sub.add_parser("run", parents=[common], help="run the per-workspace supervisor daemon")
    # Undocumented test seam: boot + orphan-sweep + exit, no run loop.
    run.add_argument("--exit-after-boot", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("status", parents=[common], help="query the local daemon over loopback")

    # --- M14 live control verbs (loopback clients; excluded from op-coverage per the daemon rationale) --
    steer = sub.add_parser("steer", parents=[common], help="steer a live run/session (loopback)")
    steer.add_argument("--run", dest="run_id", help="agent run id to steer")
    steer.add_argument("--session", dest="session_id", help="session id to steer (records a C2 instruction)")
    steer.add_argument("--instruction", required=True, help="the steering instruction text")

    approve = sub.add_parser("approve", parents=[common], help="decide a live approval (loopback)")
    approve.add_argument("--approval-id", dest="approval_id", help="the C4 approval id")
    approve.add_argument("--correlation-id", dest="correlation_id",
                         help="H0 alt-key: the approval/open stream correlation_id (race-free; pair with --session)")
    approve.add_argument("--session", dest="session_id", help="H0 alt-key: the driven session id (pair with --correlation-id)")
    approve.add_argument("--decision", choices=["approve", "deny"], required=True, help="approve or deny")
    approve.add_argument("--answers-json-file", dest="answers_json_file", help="requestUserInput only: bounded UTF-8 JSON object mapping every free-text question id to its answer")

    cancel = sub.add_parser("cancel", parents=[common], help="truthfully cancel a run (loopback)")
    cancel.add_argument("--run", dest="run_id", required=True, help="agent run id to cancel")

    attach = sub.add_parser("attach", parents=[common], help="cross-machine attach a session (loopback)")
    attach.add_argument("--session", dest="session_id", required=True, help="session id to attach")
    attach.add_argument("--expected-node", dest="expected_node", required=True, help="the session's current owning node")
    attach.add_argument("--expected-epoch", dest="expected_epoch", type=int, required=True, help="the session's current node epoch")

    sub.add_parser("dispatch-poll", parents=[common], help="run one dispatch executor cycle (loopback)")

    orchestrate = sub.add_parser("orchestrate", parents=[common], help="self-dispatch + poll: the headless start-work verb (loopback)")
    orchestrate.add_argument("--task", required=True, help="the task token to dispatch to this node")
    orchestrate.add_argument("--scope", required=True, help="the lease scope this node must hold")

    # --- H0 driven-session verbs (loopback clients; the app-server drives a local-LLM turn under the
    # ledger). Excluded from op-coverage per the daemon rationale, like steer/approve. `session events`
    # long-polls up to the 25s server cap, so its client timeout MUST exceed that (send_control default
    # is ~5s) -- see _SESSION_EVENTS_TIMEOUT below.
    session = sub.add_parser("session", parents=[common], help="drive a multi-turn conversation under one ledger run (loopback)")
    session_sub = session.add_subparsers(dest="session_command")
    s_caps = session_sub.add_parser("capabilities", parents=[common], help="list per-engine capabilities (cached; --refresh reprobes)")
    s_caps.add_argument("--refresh", action="store_true", help="rebuild the capability cache (reprobe the model catalog)")
    s_start = session_sub.add_parser("start", parents=[common], help="start a driven local-LLM session + first turn")
    s_start.add_argument("--engine", required=True, help="the local-LLM engine lane (see session capabilities)")
    s_start.add_argument("--prompt", required=True, help="the first-turn prompt")
    s_start.add_argument("--model", help="the model id (adapter default when omitted)")
    s_start.add_argument("--permission-mode", dest="permission_mode", choices=["plan", "ask", "agent", "full"],
                         help="the UI permission mode (server default: plan)")
    s_start.add_argument("--auth-mode", dest="auth_mode", choices=["none", "subscription", "api-key"],
                         help="the billing/auth mode (local_llm: none)")
    s_start.add_argument("--reasoning-effort", dest="reasoning_effort", help="the reasoning effort (vendor lanes only)")
    s_start.add_argument("--full-opt-in", dest="full_opt_in", action="store_true",
                         help="the fresh, never-persisted confirmation Full mode requires")
    s_start.add_argument("--max-turns", dest="max_turns", type=int, help="turn ceiling override")
    s_start.add_argument("--approval-timeout", dest="approval_timeout", type=float, help="approval wait seconds (server default: 300)")
    s_turn = session_sub.add_parser("turn", parents=[common], help="submit the next prompt to an idle conversation")
    s_turn.add_argument("--run", dest="run_id", required=True, help="the driven agent run id")
    s_turn.add_argument("--prompt", required=True, help="the next-turn prompt")
    s_close = session_sub.add_parser("close", parents=[common], help="close an idle conversation and write its sole success finalization")
    s_close.add_argument("--run", dest="run_id", required=True, help="the driven agent run id")
    s_list = session_sub.add_parser("list", parents=[common], help="list conversations with ordered linked runs")
    s_list.add_argument("--controller", choices=["driven", "observed"], help="filter by conversation controller")
    s_list.add_argument("--limit", type=int, help="maximum sessions (1..1000; default 100)")
    s_events = session_sub.add_parser("events", parents=[common], help="replay a driven or observed run's ledger events (long-poll)")
    s_events.add_argument("--run", dest="run_id", required=True, help="the conversation agent run id")
    s_events.add_argument("--since", type=int, default=0, help="return events after this sequence_no cursor (0 = full history)")
    s_events.add_argument("--wait", type=float, default=0.0, help="long-poll seconds to wait for new events (server-capped at 25s)")
    s_events.add_argument("--limit", type=int, help="max events per response")
    s_steer = session_sub.add_parser("steer", parents=[common], help="inject a mid-turn steer into a driven run")
    s_steer.add_argument("--run", dest="run_id", required=True, help="the driven agent run id")
    s_steer.add_argument("--instruction", required=True, help="the steering instruction text")
    s_interrupt = session_sub.add_parser("interrupt", parents=[common], help="interrupt a driven run at the next loop top")
    s_interrupt.add_argument("--run", dest="run_id", required=True, help="the driven agent run id")
    s_kill = session_sub.add_parser("kill", parents=[common], help="permanently stop a driven run")
    s_kill.add_argument("--run", dest="run_id", required=True, help="the driven agent run id")

    # --- M-CLAUDE (M5b) hooked-governor install verbs (loopback clients; excluded from op-coverage per
    # the daemon rationale, like steer/approve). Manage the workspace-local .claude/settings.local.json
    # six-hook block that points Claude Code's policy and lifecycle hooks at the orchestration-owned shim.
    hooks = sub.add_parser("hooks", parents=[common], help="manage workspace Claude governor + observed-capture hooks (loopback)")
    hooks_sub = hooks.add_subparsers(dest="hooks_command")
    hooks_install = hooks_sub.add_parser("install", parents=[common], help="install the governor hooks into .claude/settings.local.json (idempotent)")
    hooks_install.add_argument("--mode", default="hooked-strict", choices=["hooked-strict", "hooked-observe"],
                               help="hooked-strict fails CLOSED on an unreachable daemon; hooked-observe passes through")
    hooks_sub.add_parser("verify", parents=[common], help="report whether marker-owned hook commands exactly match the current shim")
    hooks_sub.add_parser("remove", parents=[common], help="remove the governor hooks from .claude/settings.local.json (idempotent)")
    return parser


def daemon_main(argv: list[str]) -> int:
    """Parses `argv`, lazily imports the supervisor runtime, dispatches the resolved subcommand to a loopback control call (or boots the daemon on bare `run`), and returns a process exit code (0 OK / non-zero DENIED). Exit-code contract is the load-bearing detail."""
    parser = build_daemon_parser()
    args = parser.parse_args(argv)
    if not args.daemon_command:
        parser.print_help()
        return 0

    # Import the runtime lazily so a bare `daemon` / `daemon --help` never pays the cost
    # (and never touches the DB / ctypes surface just to print help).
    from .supervisor import query_status, run_daemon, send_control

    if args.daemon_command == "status":
        emit(query_status(), as_json=args.json)
        return 0

    if args.daemon_command == "steer":
        control_args: dict = {"instruction": args.instruction}
        if getattr(args, "run_id", None):
            control_args["agent_run_id"] = args.run_id
        if getattr(args, "session_id", None):
            control_args["session_id"] = args.session_id
        return _emit_control(send_control(op="steer", args=control_args), args.json)

    if args.daemon_command == "approve":
        # Exactly one selector: C4 approval_id or H0 alt-key (correlation_id + session_id).
        has_approval_id = bool(getattr(args, "approval_id", None))
        has_correlation_id = bool(getattr(args, "correlation_id", None))
        has_session_id = bool(getattr(args, "session_id", None))
        valid_selector = has_approval_id and not has_correlation_id and not has_session_id \
            or not has_approval_id and has_correlation_id and has_session_id
        if not valid_selector:
            return emit_error(
                {
                    "status": "DENIED",
                    "code": "DENIED_APPROVE_FIELDS_REQUIRED",
                    "required_action": "pass --approval-id alone, or pass both --correlation-id and --session",
                    "exit_code": 2,
                },
                as_json=args.json,
            )
        approve_args: dict = {"decision": args.decision}
        for key in ("approval_id", "correlation_id", "session_id"):
            value = getattr(args, key, None)
            if value:
                approve_args[key] = value
        if getattr(args, "answers_json_file", None):
            try:
                approve_args["answers"] = _load_answers_file(args.answers_json_file)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
                return emit_error(
                    {
                        "status": "DENIED",
                        "code": "DENIED_USER_INPUT_ANSWERS_INVALID",
                        "reason": str(error),
                        "required_action": "pass --answers-json-file with a bounded UTF-8 JSON object",
                    },
                    as_json=args.json,
                )
        return _emit_control(send_control(op="approve", args=approve_args), args.json)

    if args.daemon_command == "cancel":
        return _emit_control(send_control(op="cancel", args={"agent_run_id": args.run_id}), args.json)

    if args.daemon_command == "attach":
        return _emit_control(
            send_control(op="attach", args={
                "session_id": args.session_id,
                "expected_owning_node": args.expected_node,
                "expected_node_epoch": args.expected_epoch,
            }),
            args.json,
        )

    if args.daemon_command == "dispatch-poll":
        return _emit_control(send_control(op="dispatch/poll", args={}), args.json)

    if args.daemon_command == "orchestrate":
        return _emit_control(send_control(op="orchestrate", args={"task": args.task, "scope": args.scope}), args.json)

    if args.daemon_command == "session":
        session_command = getattr(args, "session_command", None)
        if not session_command:
            _print_subparser_help(parser, "session")
            return 2
        if session_command == "capabilities":
            return _emit_control(
                send_control(op="session/capabilities", args={"refresh": bool(getattr(args, "refresh", False))}),
                args.json,
            )
        if session_command == "start":
            control_args: dict = {"engine": args.engine, "prompt": args.prompt}
            if getattr(args, "model", None):
                control_args["model"] = args.model
            # H2.1 profile: pack the mode/auth/effort flags into the profile object the supervisor validates.
            profile: dict = {}
            if getattr(args, "permission_mode", None):
                profile["permission_mode"] = args.permission_mode
            if getattr(args, "auth_mode", None):
                profile["auth_mode"] = args.auth_mode
            if getattr(args, "reasoning_effort", None):
                profile["reasoning_effort"] = args.reasoning_effort
            if profile:
                control_args["profile"] = profile
            if getattr(args, "full_opt_in", False):
                control_args["full_opt_in"] = True
            if getattr(args, "max_turns", None) is not None:
                control_args["max_turns"] = args.max_turns
            if getattr(args, "approval_timeout", None) is not None:
                control_args["approval_timeout"] = args.approval_timeout
            return _emit_control(send_control(op="session/start", args=control_args), args.json)
        if session_command == "events":
            control_args = {"agent_run_id": args.run_id, "since": args.since, "wait": args.wait}
            if getattr(args, "limit", None) is not None:
                control_args["limit"] = args.limit
            # Client timeout must exceed the 25s server long-poll cap (see _SESSION_EVENTS_TIMEOUT).
            return _emit_control(
                send_control(op="session/events", args=control_args, timeout=_SESSION_EVENTS_TIMEOUT), args.json
            )
        if session_command == "turn":
            return _emit_control(
                send_control(op="session/turn", args={"agent_run_id": args.run_id, "prompt": args.prompt}),
                args.json,
            )
        if session_command == "close":
            return _emit_control(
                send_control(op="session/close", args={"agent_run_id": args.run_id}),
                args.json,
            )
        if session_command == "list":
            control_args = {}
            if getattr(args, "controller", None):
                control_args["controller"] = args.controller
            if getattr(args, "limit", None) is not None:
                control_args["limit"] = args.limit
            return _emit_control(send_control(op="session/list", args=control_args), args.json)
        if session_command == "steer":
            return _emit_control(
                send_control(op="session/steer", args={"agent_run_id": args.run_id, "instruction": args.instruction}),
                args.json,
            )
        if session_command == "interrupt":
            return _emit_control(send_control(op="session/interrupt", args={"agent_run_id": args.run_id}), args.json)
        if session_command == "kill":
            return _emit_control(send_control(op="session/kill", args={"agent_run_id": args.run_id}), args.json)
        return emit_error(
            {"status": "DENIED", "code": "DENIED_SESSION_COMMAND_INVALID", "exit_code": 2}, as_json=args.json,
        )

    if args.daemon_command == "hooks":
        hooks_command = getattr(args, "hooks_command", None)
        if not hooks_command:
            _print_subparser_help(parser, "hooks")
            return 2
        control_args: dict = {}
        if hooks_command == "install":
            control_args["mode"] = args.mode
        return _emit_control(send_control(op=f"hooks/{hooks_command}", args=control_args), args.json)

    payload = run_daemon(exit_after_boot=getattr(args, "exit_after_boot", False))
    if payload.get("status") == "DENIED":
        return emit_error(payload, as_json=args.json)
    return emit(payload, as_json=args.json)


def _print_subparser_help(parser: argparse.ArgumentParser, name: str) -> None:
    """Print a named subparser's help WITHOUT argparse's ``--help`` SystemExit (mirrors the top-level
    no-command ``parser.print_help()`` path). Falls back to the top-level help if the subparser is not
    found (defensive; the name is a literal we control)."""
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and name in choices:
            choices[name].print_help()
            return
    parser.print_help()


def _emit_control(payload: dict, as_json: bool) -> int:
    """Emit a loopback control response: a DENIED payload exits non-zero (its exit_code or 1), an
    OK/not-running payload exits 0."""
    if payload.get("status") == "DENIED":
        return emit_error(payload, as_json=as_json)
    return emit(payload, as_json=as_json)
