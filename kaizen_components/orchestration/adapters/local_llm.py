"""Local-LLM adapter (v8 M6, plan §A.1 local-LLM column / §A.2 / §A.3).

Engines with NO native sessions/approvals under the same governance as Codex (M4). A local model
(Ollama, transformers, or any injected chat provider) emits text and nothing else -- no vendor
session, no vendor approval, no self-termination. Kaizen therefore owns the ENTIRE loop:

    prompt build -> model output -> tool-intent parse (strict JSON; a parse failure yields NO tool
    and still counts toward max_turns) -> default-deny :meth:`policy.PolicyEngine.decide` gate
    BETWEEN the parse and the execute -> policy-scoped executor -> append recorder events -> loop.

The security spine is the same seam Codex funnels through: every tool intent becomes a
:class:`policy.RequestedAction` and passes :meth:`policy.PolicyEngine.decide` PER ITERATION before
anything runs. ALL model output -- the reply text, the parsed tool name, its args, any command string
-- is UNTRUSTED DATA: it is never eval/exec/template/f-string-interpolated into anything executable.
Our OWN trusted tool registry (names, verbs, arg hints) is the only thing templated into the system
prompt. A model reply carrying ``git push`` or a protected-path write is denied IDENTICALLY to Codex
(same invariant_id on the same ``approval``/``declined`` event) -- that cross-adapter parity closes the
ledger #21 proof obligation.

Because a local model does not stop on its own, the loop is bounded two ways: an explicit ``max_turns``
ceiling (never a ``max_turns+1``-th model call) and a satisfied-tool-call detector -- the model signals
completion by emitting the ``{"final": ...}`` shape, which is the only thing that ends the turn OK.

Capability shims (no vendor binary exists, so there is no version gate):
- resume = none (:meth:`resume` refuses DENIED_NO_RESUME).
- steer = inject-next-iteration (a steered message is queued and drained into the message list at the
  top of the next loop iteration; an in-flight chat/tool call completes first).
- subagent = Kaizen-simulated child run (:meth:`spawn_subagent` builds a CHILD adapter that SHARES the
  policy engine, chat provider, tools, recorder, logger, session binding, and epoch, then runs a turn).

STDOUT stays pristine: every log line goes to the injected logger (default stderr). This module writes
NO ledger rows itself -- it emits normalized recorder events (same shape as codex.py's ``_emit``)
through an injected ``recorder`` callable (the supervisor funnels them to T5/T6/T8); tests capture them
in a list. There is no child process: the "engine" is the injected ``chat_provider`` callable, so no
supervisor spawn/ownership is involved (contrast M4, whose engine is an OWNED subprocess).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import children, policy
from ..apply_evidence import normalize_apply_evidence, staged_cleanup_paths
from . import (
    ApprovalBrokerCallback,
    BrokerApprovalResult,
    MutationGuard,
    TurnResult,
    check_mutation_guard,
    mutation_guard_required,
    resolve_broker_result,
)
from ..diff_snapshots import DENIED_PARTIAL_APPLY
from ..proposal_executor import WorkspaceProposalExecutor
from ..session_artifacts import neutralize_at_refs

# --- refusal codes (DENIED_* payloads on the shim / no-active-turn paths) -----------------------

DENIED_NO_RESUME = "DENIED_NO_RESUME"                # resume=none shim: local models cannot resume
DENIED_NO_ACTIVE_TURN = "DENIED_NO_ACTIVE_TURN"      # steer() with no turn in flight
DENIED_BACKEND_OFF = "DENIED_BACKEND_OFF"            # default provider requested but no backend configured
DENIED_KILLED = "DENIED_KILLED"                      # start_turn() on a killed adapter
DENIED_CLOSED = "DENIED_CLOSED"                      # run_turn() after a clean logical close
DENIED_SESSION_NOT_IDLE = "DENIED_SESSION_NOT_IDLE"  # close() while a turn is active
DENIED_TOOL_GATEWAY_UNAVAILABLE = "DENIED_TOOL_GATEWAY_UNAVAILABLE"

# Terminal turn-failure codes (every non-OK exit path names one).
BACKEND_ERROR = "BACKEND_ERROR"                      # chat_provider raised
MAX_TURNS_EXHAUSTED = "MAX_TURNS_EXHAUSTED"          # ceiling hit without a final
TOOL_ERROR = "TOOL_ERROR"                            # an ALLOWED tool's runner raised

# Internal decision vocabulary (recorder events + on_approval callbacks). Mirrors codex.py -- the local
# loop has no vendor wire, so these stay purely internal (no _WIRE_DECISION mapping needed).
DECISION_APPROVED = "approved"
DECISION_DENIED = "denied"


# The provider-neutral v5 model surface.  These are policy-contract names, not provider aliases.
# Legacy injected ``ToolSpec`` registries remain available for the v8 hermetic/dispatch seams, while
# normal Supervisor-created Ollama sessions use this exact gateway surface.
CANONICAL_TOOL_NAMES = (
    "kaizen_read_file",
    "kaizen_list_files",
    "kaizen_search_text",
    "kaizen_run_process",
    "kaizen_propose_changes",
)
_CANONICAL_TOOL_PROMPT: dict[str, tuple[str, tuple[str, ...], str]] = {
    "kaizen_read_file": (
        "file_read", ("path", "start_line?", "end_line?", "max_bytes?"),
        "read one bounded UTF-8 workspace file",
    ),
    "kaizen_list_files": (
        "file_read", ("path?", "glob?", "max_depth?", "max_entries?"),
        "list bounded sorted workspace entries",
    ),
    "kaizen_search_text": (
        "file_read", ("query", "path?", "mode?", "glob?", "case_sensitive?", "max_results?"),
        "search bounded workspace text",
    ),
    "kaizen_run_process": (
        "exec", ("executable", "argv?", "cwd?", "timeout_ms?"),
        "run one permission-gated direct-argv process",
    ),
    "kaizen_propose_changes": (
        "file_write", ("summary?", "changes"),
        "propose one whole-request governed workspace change set",
    ),
}


class LocalLLMAdapterError(Exception):
    """A refusal the adapter raises on a shim / no-active-turn path. Carries a DENIED_* code + fields
    so the caller can surface a structured denial (codex.CodexAdapterError parity)."""

    def __init__(self, code: str, **fields: Any) -> None:
        super().__init__(f"{code}: {fields}")
        self.code = code
        self.fields = fields

    def payload(self) -> dict[str, Any]:
        return {"status": "DENIED", "code": self.code, **self.fields}


# --- default chat provider (LAZY; the live leg exercises it, not the hermetic tests) ------------

def default_chat_provider() -> Callable[[list[dict[str, str]]], dict[str, Any]]:
    """Build the default ``chat_provider`` from the configured text backend, LAZILY.

    The backend import is deferred into this function so importing this module never pulls in
    ``backends`` (and, transitively, torch). ``get_text_backend()`` returns None when no text model is
    configured -> raise DENIED_BACKEND_OFF (the loop is unusable without an engine). When a backend is
    present the provider is a closure that keeps the chat-completions message shape:

    - Ollama / OpenAI-compatible backends expose ``.client`` (an ``OpenAICompatClient`` with
      ``.chat(messages, model, **opts) -> {"text","usage","model"}``) + ``.model`` -> the closure calls
      ``client.chat(messages, model, **opts)`` directly (native chat-turn shape preserved).
    - A ``.chat(prompt)``-only backend (transformers) has no message API, so the closure FLATTENS the
      message list to ``"role: content"`` lines joined by newlines and calls ``backend.chat(flat, **opts)``.

    Hermetic tests ALWAYS inject a scripted provider; this default is only reached on the live leg.
    """
    # THREE dots: this module is kaizen_components.orchestration.adapters.local_llm, and the backends
    # package is kaizen_components.backends. The M6 live leg caught a two-dot form resolving to the
    # nonexistent orchestration.backends -- hermetic tests always inject a provider, so ONLY a live
    # default-provider consumer exercises this import.
    from ...backends import get_text_backend

    backend = get_text_backend()
    if backend is None:
        raise LocalLLMAdapterError(
            DENIED_BACKEND_OFF,
            required_action="configure a text backend (KAIZEN_LLM_MODEL or KAIZEN_TEXT_BACKEND=transformers) "
                            "to run the local-LLM orchestration loop, or inject an explicit chat_provider",
        )

    client = getattr(backend, "client", None)
    model = getattr(backend, "model", None)
    if client is not None and model is not None:
        def _provider_chat(messages: list[dict[str, str]], **opts: Any) -> dict[str, Any]:
            message_chat = getattr(backend, "chat_messages", None)
            return message_chat(messages, **opts) if callable(message_chat) else client.chat(messages, model, **opts)

        def _provider_stream(
            messages: list[dict[str, str]], on_delta: Callable[[str], None], **opts: Any,
        ) -> dict[str, Any]:
            message_stream = getattr(backend, "chat_messages_stream", None)
            if callable(message_stream):
                return message_stream(messages, on_delta, **opts)
            return client.chat_stream(messages, model, on_delta, **opts)

        setattr(_provider_chat, "stream_chat", _provider_stream)

        return _provider_chat

    def _provider_flat(messages: list[dict[str, str]], **opts: Any) -> dict[str, Any]:
        # No message API -> flatten to "role: content" lines. Model text is DATA here, never templated
        # into anything executable; string-joining it for a prompt is inert. Flat backends expose no
        # streaming method, so registered delta sinks intentionally receive only message-capable lanes.
        flat = "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages)
        return backend.chat(flat, **opts)

    return _provider_flat


# --- tool registry -----------------------------------------------------------------------------

def _default_targets(args: Mapping[str, Any]) -> tuple[str, ...]:
    """Default target extractor: the ``path`` arg (when present) as a single-element tuple, else ()."""
    value = args.get("path") if isinstance(args, Mapping) else None
    return (str(value),) if value else ()


def _default_command(spec_verb: str, args: Mapping[str, Any]) -> str | None:
    """Default command extractor: ``args['command']`` for exec/spawn verbs, else None. A non-shell verb
    never carries a shell string, so the policy engine only scans a command for the verbs that have one."""
    if spec_verb in policy.SHELL_VERBS:
        value = args.get("command") if isinstance(args, Mapping) else None
        return str(value) if value is not None else None
    return None


@dataclass
class ToolSpec:
    """One trusted, Kaizen-owned tool the model may request by name.

    ``verb`` is a :data:`policy.VERBS` member (the policy decision axis); ``summary`` + the arg hints
    feed the system prompt; ``run(args)`` is the policy-scoped executor invoked ONLY after decide()
    ALLOWs. ``targets(args)``/``command(args)`` project the model's (untrusted) args into the
    :class:`policy.RequestedAction` fields the chokepoint gates on -- they READ the args, never execute
    them. Defaults: ``targets`` = the ``path`` arg; ``command`` = the ``command`` arg for exec/spawn.
    """

    name: str
    verb: str
    summary: str
    run: Callable[[Mapping[str, Any]], str]
    targets: Callable[[Mapping[str, Any]], tuple[str, ...]] | None = None
    command: Callable[[Mapping[str, Any]], str | None] | None = None
    arg_hints: tuple[str, ...] = ()

    def resolve_targets(self, args: Mapping[str, Any]) -> tuple[str, ...]:
        if self.targets is not None:
            return tuple(self.targets(args))
        return _default_targets(args)

    def resolve_command(self, args: Mapping[str, Any]) -> str | None:
        if self.command is not None:
            return self.command(args)
        return _default_command(self.verb, args)


def _resolve_within(workspace_root: Path, raw_path: Any) -> Path:
    """Resolve ``raw_path`` and assert it stays inside ``workspace_root`` (resolved-path containment).

    Belt-and-suspenders ONLY: the policy engine gates FIRST (a path_prefix allow rule scopes the tool),
    so this is the second wall against a traversal in an ALLOWED tool's own runner. An escape raises --
    the runner is refused before it touches the filesystem. Uses ``Path.resolve()`` so ``..`` and
    symlink components are collapsed before the containment check.
    """
    root = workspace_root.resolve()
    candidate = Path(str(raw_path))
    resolved = candidate if candidate.is_absolute() else (root / candidate)
    resolved = resolved.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path escapes workspace root: {raw_path!r} -> {resolved}")
    return resolved


def build_default_tools(workspace_root: str | Path) -> dict[str, ToolSpec]:
    """The default, root-scoped tool set: ``read_file``/``list_dir`` (verb file_read) and ``write_file``
    (verb file_write). Every runner is confined to ``workspace_root`` by a resolved-path containment
    check (:func:`_resolve_within`); an escape raises before any filesystem touch. NO exec tool is in
    the defaults -- an exec/spawn tool is always CALLER-INJECTED (the injection suite injects one), so a
    default local-LLM loop cannot shell out at all."""
    root = Path(str(workspace_root))

    def _read_file(args: Mapping[str, Any]) -> str:
        target = _resolve_within(root, args.get("path", ""))
        return target.read_text(encoding="utf-8")

    def _write_file(args: Mapping[str, Any]) -> str:
        target = _resolve_within(root, args.get("path", ""))
        content = args.get("content", "")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(str(content).encode("utf-8"))
        return f"wrote {len(str(content))} chars to {target.name}"

    def _list_dir(args: Mapping[str, Any]) -> str:
        target = _resolve_within(root, args.get("path", "."))
        return "\n".join(sorted(entry.name for entry in target.iterdir()))

    return {
        "read_file": ToolSpec("read_file", "file_read", "read a UTF-8 file under the workspace root",
                              _read_file, arg_hints=("path",)),
        "write_file": ToolSpec("write_file", "file_write", "write a UTF-8 file under the workspace root",
                               _write_file, arg_hints=("path", "content")),
        "list_dir": ToolSpec("list_dir", "file_read", "list directory entries under the workspace root",
                             _list_dir, arg_hints=("path",)),
    }


# --- tool-intent parse (pure; model output is DATA) --------------------------------------------

# The three shapes parse_tool_intent returns. A local model does not self-terminate, so the FINAL shape
# is the satisfied-tool-call detector -- the only reply that ends a turn OK.
PARSE_FINAL = "final"
PARSE_TOOL = "tool"
PARSE_ERROR = "parse_error"

_FENCE_LANGS = ("json",)


def _unwrap_single_fence(text: str) -> str | None:
    """If ``text`` is exactly one ``` / ```json fenced block (optionally with surrounding whitespace),
    return its inner body; else None. This is the ONLY tolerated deviation from raw ``json.loads`` --
    there is deliberately NO regex extraction of an object embedded in prose (that would let a model's
    prose smuggle a tool call). A block whose fences do not bracket the whole (stripped) reply fails."""
    stripped = text.strip()
    if not stripped.startswith("```") or not stripped.endswith("```") or len(stripped) < 6:
        return None
    inner = stripped[3:-3]
    # Drop an optional language tag on the opening fence's first line.
    newline = inner.find("\n")
    if newline != -1:
        first_line = inner[:newline].strip().lower()
        if first_line in _FENCE_LANGS or first_line == "":
            inner = inner[newline + 1:]
    return inner


def parse_tool_intent(text: str) -> tuple[Any, ...]:
    """Parse ONE model reply into a tool intent. Strict + pure; the reply is untrusted DATA.

    Returns exactly one of:
    - ``(PARSE_FINAL, answer_str)`` -- the ``{"final": "<answer>"}`` shape (the satisfied detector).
    - ``(PARSE_TOOL, name_str, args_dict)`` -- the ``{"tool": "<name>", "args": {...}}`` shape.
    - ``(PARSE_ERROR, reason_str)`` -- anything else (non-JSON, non-dict, both/neither key, wrong types).

    Parsing = ``json.loads`` on the stripped text, with a SINGLE fallback: unwrap one ``` / ```json
    fenced block then ``json.loads`` again. No regex extraction of an embedded object -- a reply that is
    not itself a single JSON object (bare or fenced) is a parse_error and yields no tool.
    """
    if not isinstance(text, str):
        return (PARSE_ERROR, "model reply was not text")
    stripped = text.strip()
    if not stripped:
        return (PARSE_ERROR, "empty model reply")

    _unset = object()
    obj: Any = _unset
    try:
        obj = json.loads(stripped)
    except (ValueError, TypeError):
        inner = _unwrap_single_fence(stripped)
        if inner is not None:
            try:
                obj = json.loads(inner.strip())
            except (ValueError, TypeError):
                obj = _unset
    if obj is _unset:
        return (PARSE_ERROR, "reply was not a single JSON object (bare or ``` fenced)")

    if not isinstance(obj, dict):
        return (PARSE_ERROR, "reply JSON was not an object")

    has_final = "final" in obj
    has_tool = "tool" in obj
    if has_final and has_tool:
        return (PARSE_ERROR, "reply carried both 'final' and 'tool' keys")
    if has_final:
        final = obj["final"]
        if not isinstance(final, str):
            return (PARSE_ERROR, "'final' must be a string")
        return (PARSE_FINAL, final)
    if has_tool:
        name = obj["tool"]
        if not isinstance(name, str):
            return (PARSE_ERROR, "'tool' must be a string")
        args = obj.get("args", {})
        if not isinstance(args, dict):
            return (PARSE_ERROR, "'args' must be an object")
        return (PARSE_TOOL, name, args)
    return (PARSE_ERROR, "reply object had neither 'final' nor 'tool'")


class _FinalDeltaExtractor:
    """Incrementally decode only a top-level ``{"final":"..."}`` string.

    Local providers stream their protocol wrapper, not display text. This tiny lexer suppresses tool
    objects, keys, arguments, fences, and JSON punctuation; malformed suffixes may leave an ephemeral
    partial which the later durable terminal/final event replaces, but never expose tool metadata.
    """

    _ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
                "n": "\n", "r": "\r", "t": "\t"}

    def __init__(self) -> None:
        self._phase = "prefix"
        self._prefix = ""
        self._key_raw = ""
        self._key_escape = False
        self._escape = False
        self._unicode = ""
        self._pending_high: int | None = None
        self._closed_object = False

    def feed(self, fragment: str) -> str:
        """Consumes one stream fragment; returns newly-decoded final display text ("" when suppressed/disabled/non-str)."""
        if self._phase == "disabled" or not isinstance(fragment, str) or not fragment:
            return ""
        out: list[str] = []
        for char in fragment:
            if self._phase == "prefix":
                if char == "{":
                    if self._prefix.strip().lower() not in ("", "```", "```json"):
                        self._phase = "disabled"
                    else:
                        self._phase = "key_start"
                    continue
                self._prefix += char
                if len(self._prefix) > 32:
                    self._phase = "disabled"
                continue
            if self._phase == "key_start":
                if char.isspace():
                    continue
                if char != '"':
                    self._phase = "disabled"
                    continue
                self._key_raw = '"'
                self._key_escape = False
                self._phase = "key"
                continue
            if self._phase == "key":
                self._key_raw += char
                if len(self._key_raw) > 128:
                    self._phase = "disabled"
                    continue
                if self._key_escape:
                    self._key_escape = False
                    continue
                if char == "\\":
                    self._key_escape = True
                    continue
                if char == '"':
                    try:
                        key = json.loads(self._key_raw)
                    except (TypeError, ValueError):
                        self._phase = "disabled"
                        continue
                    self._phase = "colon" if key == "final" else "disabled"
                continue
            if self._phase == "colon":
                if char.isspace():
                    continue
                self._phase = "value_start" if char == ":" else "disabled"
                continue
            if self._phase == "value_start":
                if char.isspace():
                    continue
                self._phase = "value" if char == '"' else "disabled"
                continue
            if self._phase == "value":
                self._consume_value_char(char, out)
                continue
            if self._phase == "suffix":
                if not self._closed_object:
                    if char.isspace():
                        continue
                    if char == "}":
                        self._closed_object = True
                    else:
                        self._phase = "disabled"
                elif not char.isspace() and char != "`":
                    self._phase = "disabled"
        return "".join(out)

    def _consume_value_char(self, char: str, out: list[str]) -> None:
        """Decodes one JSON-string char into out; disables on illegal escape / control char / mispaired surrogate."""
        if self._unicode:
            if char not in "0123456789abcdefABCDEF":
                self._phase = "disabled"
                return
            self._unicode += char
            if len(self._unicode) == 5:
                self._emit_code_unit(int(self._unicode[1:], 16), out)
                self._unicode = ""
                self._escape = False
            return
        if self._escape:
            if char == "u":
                self._unicode = "u"
                return
            decoded = self._ESCAPES.get(char)
            if decoded is None:
                self._phase = "disabled"
                return
            if self._pending_high is not None:
                self._phase = "disabled"
                return
            out.append(decoded)
            self._escape = False
            return
        if char == "\\":
            self._escape = True
            return
        if char == '"':
            self._phase = "suffix" if self._pending_high is None else "disabled"
            return
        if ord(char) < 0x20 or self._pending_high is not None:
            self._phase = "disabled"
            return
        out.append(char)

    def _emit_code_unit(self, value: int, out: list[str]) -> None:
        """Fold a \\uXXXX unit into output, pairing valid surrogate units."""
        if 0xD800 <= value <= 0xDBFF:
            if self._pending_high is not None:
                self._phase = "disabled"
            else:
                self._pending_high = value
            return
        if 0xDC00 <= value <= 0xDFFF:
            if self._pending_high is None:
                self._phase = "disabled"
                return
            high = self._pending_high
            self._pending_high = None
            out.append(chr(0x10000 + ((high - 0xD800) << 10) + value - 0xDC00))
            return
        if self._pending_high is not None:
            self._phase = "disabled"
            return
        out.append(chr(value))


# --- adapter -----------------------------------------------------------------------------------

# The complete recorder vocabulary this adapter emits. Kept as a module constant so the test-suite
# vocabulary sweep (§A.3) can assert every emitted (event_kind, marker) is in-vocabulary, and so the
# marker strings match the codex.py span grammar exactly (session/turn/tool_call/approval/subagent).
EVENT_VOCABULARY: dict[str, frozenset[str]] = {
    "session": frozenset({"open"}),
    "turn": frozenset({"open", "close_ok", "close_fail", "close_canceled"}),
    "tool_call": frozenset({"open", "close_ok", "close_fail"}),
    "approval": frozenset({"open", "resolved", "declined", "timed_out"}),
    "subagent": frozenset({"open", "close_ok", "close_fail", "close_canceled"}),
}
_MAX_HISTORY_MESSAGES = 64


class LocalLLMAdapter:
    """A local model as a governed EngineAdapter (§A.1) with Kaizen owning the whole loop.

    Collaborators are injected (codex.CodexAdapter parity): ``chat_provider`` (the "engine" -- a
    callable, NOT a child process; default built lazily by :func:`default_chat_provider`), ``tools``
    (the trusted registry; default :func:`build_default_tools` needs a workspace root, so with no tools
    injected the registry is empty and every intent is unknown-tool), ``recorder`` (callable(event_dict)
    -- tests capture in-memory; NO hard-wired DB writes), ``logger`` (default stderr; stdout stays
    pristine), ``clock``, and ``id_factory`` (deterministic ids in tests). Records use
    ``agent_type='other'`` (no native session type).
    """

    def __init__(
        self,
        policy_engine: policy.PolicyEngine,
        *,
        chat_provider: Callable[..., dict[str, Any]] | None = None,
        tools: Mapping[str, ToolSpec] | None = None,
        recorder: Callable[[dict[str, Any]], None] | None = None,
        logger: Callable[[str], None] | None = None,
        clock: Callable[[], float] | None = None,
        id_factory: Callable[[str], str] | None = None,
        engine_name: str = "local-llm",
        model: str | None = None,
        max_turns: int = 8,
        approval_timeout: float = 30.0,
        workspace_root: str | Path | None = None,
        tool_spawner: Callable[..., children.OwnedChild] | None = None,
        gateway_factory: Callable[..., Any] | None = None,
        apply_recovery_callback_factory: Callable[
            [], Callable[[Mapping[str, Any] | None], bool] | None
        ] | None = None,
        workspace_path_authority: Any | None = None,
    ) -> None:
        """Configure bounded turns/history, approval timing, tool/gateway seams, and workspace authority."""
        self.policy_engine = policy_engine
        self._chat_provider = chat_provider
        self._tools: dict[str, ToolSpec] = dict(tools or {})
        self._recorder = recorder or (lambda _event: None)
        self._log = logger or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self._clock = clock or __import__("time").monotonic
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex[:12]}")
        self._engine = engine_name
        self._model = model
        self._max_turns = max_turns
        self._approval_timeout = approval_timeout
        self._workspace_root = Path(os.path.abspath(workspace_root)) if workspace_root is not None else None
        self._tool_spawner = tool_spawner or children.spawn_owned
        self._gateway_factory = gateway_factory
        self._apply_recovery_callback_factory = apply_recovery_callback_factory
        self._workspace_path_authority = workspace_path_authority
        self._gateway_mode = self._workspace_root is not None and tools is None
        self._gateway: Any = None
        self._gateway_failure: dict[str, Any] | None = None
        self._mutation_guard: MutationGuard | None = None
        self._approval_cb: Callable[[dict[str, Any]], str] | None = None
        self._approval_broker_cb: ApprovalBrokerCallback | None = None
        self._delta_cb: Callable[[dict[str, Any]], None] | None = None
        self._post_apply_failure: dict[str, Any] | None = None
        self._workspace_recovery_paths: tuple[str, ...] = ()

        # Session/epoch/turn state. Span bookkeeping is touched by the loop thread (start_turn) AND by
        # kill()/interrupt() off another thread, so a lock guards it and open/close emission is
        # IDEMPOTENT (dedupe via the *_opened / *_closed sets) -- kill() vs the loop-thread's own later
        # close is a race, and either may fire the terminal marker first (codex.py parity).
        self.session_id: str | None = None       # bound C1 session id (for record_ask), if any
        self.session_cwd: str | None = None
        self.current_epoch = 0
        self._span_lock = threading.RLock()
        self._active_turn: str | None = None
        self._turn_opened: set[str] = set()       # turn ids that already emitted `turn open`
        self._turn_closed: set[str] = set()       # turn ids that already emitted a terminal marker

        # Steer queue + interrupt flag (thread-safe; drained/checked at the top of each loop iteration).
        # Both are PER-TURN: start_turn clears them so a prior turn's interrupt/steer never poisons the
        # next turn. kill() is the only permanent stop (_killed).
        self._steer_lock = threading.Lock()
        self._steer_queue: deque[str] = deque()
        self._interrupt = threading.Event()
        self._killed = False
        self._opened = False
        self._closed = False

        # H2 logical-conversation memory. Only accepted user messages and COMPLETE final assistant
        # messages survive into later turns; transient JSON/tool-loop chatter is scoped to its turn.
        self._history_lock = threading.RLock()
        self._history: list[dict[str, str]] = []

    def seed_history(self, messages: list[dict[str, str]]) -> None:
        """Daemon-restart continuation: rebuild the logical-conversation memory of a FRESH adapter from
        the durable chat_message events (the DB is the source of truth; the RAM history is a cache).
        Refused once any turn has produced history of its own."""
        with self._history_lock:
            if self._history:
                raise LocalLLMAdapterError(
                    DENIED_SESSION_NOT_IDLE,
                    required_action="seed_history only seeds a fresh adapter before its first turn",
                )
            self._history = [
                {"role": str(m.get("role", "")), "content": str(m.get("content", ""))}
                for m in messages
                if m.get("role") in ("user", "assistant") and m.get("content")
            ][-_MAX_HISTORY_MESSAGES:]
            if self._history and self._history[0]["role"] == "assistant":
                self._history.pop(0)

    # --- capabilities --------------------------------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        """Static capability report. No vendor binary exists, so there is NO version gate (contrast
        codex): the shims name what Kaizen simulates -- resume none, steer inject-next-iteration,
        subagent kaizen-simulated -- and sandbox_mode is ``none-kaizen-gated`` (the policy chokepoint is
        the only gate; no OS/vendor sandbox is involved)."""
        return {
            "engine": self._engine,
            "agent_type": "other",
            "session": True,
            "turn": True,
            "steer": "inject-next-iteration",
            "approval": True,
            "subagent": "kaizen-simulated",
            "resume": False,
            "sandbox_mode": "none-kaizen-gated",
            "orchestrate": True,
            "max_turns": self._max_turns,
            "controlled_tools": self._gateway_mode,
            "process_execution": self._gateway_mode,
            "tools": list(CANONICAL_TOOL_NAMES) if self._gateway_mode else list(self._tools),
        }

    # --- lifecycle -----------------------------------------------------------------------------

    def start_session(self, cwd: str | None = None, profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Open a Kaizen-owned session (no vendor handshake -- there is no child). Records the cwd for
        RequestedAction.raw['cwd'] on later tool gates and emits a ``session open`` event carrying
        ``agent_type='other'`` (§A.1: local models have no native session type)."""
        with self._span_lock:
            if self._killed or self._closed:
                raise LocalLLMAdapterError(
                    DENIED_CLOSED,
                    required_action="a closed local-LLM adapter cannot be reopened; build a fresh adapter",
                )
            self.session_cwd = cwd
            self._opened = True
        self._emit(
            "session", "open",
            correlation_id=self.session_id or self._engine,
            summary="local-llm session started",
            payload={
                "engine": self._engine,
                "agent_type": "other",
                "model": self._model,
                "sandbox_mode": "none-kaizen-gated",
                "max_turns": self._max_turns,
            },
        )
        return {
            "status": "OK",
            "session_ref": self.session_id or self._engine,
            "agent_type": "other",
            "model": self._model,
            "sandbox_mode": "none-kaizen-gated",
            "max_turns": self._max_turns,
        }

    def open(self, profile: Mapping[str, Any], policy_snapshot: Any) -> Mapping[str, Any]:
        """Open the logical adapter without submitting a prompt (the frozen H2 contract).

        The immutable snapshot replaces any constructor-time policy engine and supplies the canonical
        workspace cwd. This is a pure local handshake: the provider is still resolved lazily by the first
        turn so importing/help/capability paths never initialize a backend.
        """

        if policy_snapshot is not None:
            self.policy_engine = policy.PolicyEngine.from_snapshot(policy_snapshot)
        if profile.get("model") is not None:
            self._model = str(profile["model"])
        if self._gateway_mode:
            self._gateway = self._make_gateway(policy_snapshot)
            if self._gateway is None:
                raise LocalLLMAdapterError(
                    DENIED_TOOL_GATEWAY_UNAVAILABLE,
                    required_action="repair the provider-neutral tool gateway before opening this conversation",
                )
        cwd = getattr(policy_snapshot, "workspace_root", None)
        return self.start_session(cwd=str(cwd) if cwd else None, profile=profile)

    def _make_gateway(self, snapshot: Any) -> Any:
        """Bind the canonical tools to the same frozen authority and daemon seams as Claude."""

        if self._workspace_root is None:
            return None
        if self._gateway_factory is not None:
            return self._gateway_factory(
                workspace_root=self._workspace_root,
                policy_snapshot=snapshot,
                spawner=self._tool_spawner,
                approval_timeout=self._approval_timeout,
            )
        try:
            from ..tool_gateway import TOOL_NAMES, ToolGateway

            if tuple(TOOL_NAMES) != CANONICAL_TOOL_NAMES:
                return None
            authority = snapshot.build_engine() if snapshot is not None and hasattr(snapshot, "build_engine") \
                else self.policy_engine
            return ToolGateway(
                self._workspace_root,
                decide=lambda action, epoch: authority.decide(action, current_epoch=epoch),
                approval_broker=lambda request: self._approval_broker_cb(request)
                if self._approval_broker_cb is not None else {
                    "decision": "denied",
                    "code": "ERROR_TOOL_APPROVAL_TRANSPORT",
                    "fatal": True,
                },
                mutation_guard=lambda action: self._mutation_guard(action)
                if self._mutation_guard is not None else {
                    "status": "DENIED",
                    "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                    "required_action": "bind the daemon writer lease before mutation",
                },
                proposal_executor=WorkspaceProposalExecutor(
                    self._workspace_root,
                    path_authority=self._workspace_path_authority,
                ),
                recovery_callback_factory=self._apply_recovery_callback_factory,
                spawner=self._tool_spawner,
            )
        except (ImportError, TypeError, ValueError):
            return None

    def bind_session(self, session_id: str | None) -> None:
        """Bind a C1 ``agent_sessions`` id so ``ask`` decisions persist via the engine's record_ask
        (codex.bind_session parity). Optional -- when unbound, an ask still surfaces through on_approval
        but is not DB-persisted here."""
        self.session_id = session_id

    def resume(self, *_args: Any, **_kwargs: Any) -> None:
        """resume=none shim: a local model has no persisted session to resume, so this always refuses
        with DENIED_NO_RESUME (capabilities()['resume'] is False)."""
        raise LocalLLMAdapterError(
            DENIED_NO_RESUME,
            engine=self._engine,
            required_action="local-LLM sessions are not resumable; start a fresh session",
        )

    # --- system prompt (OUR trusted registry only -- never model text) -------------------------

    def _system_prompt(self) -> str:
        """Build the SYSTEM prompt from OUR trusted tool registry + the wire rules. Only registry strings
        we own (tool names, verbs, arg hints, summaries) are interpolated here -- NEVER any model output.
        The wire contract: reply with EXACTLY ONE JSON object, either a tool call or a final answer."""
        lines = [
            "You are a tool-using agent governed by Kaizen. On each turn reply with EXACTLY ONE JSON "
            "object and nothing else.",
            "To call a tool: {\"tool\": \"<name>\", \"args\": {...}}.",
            "To finish with an answer: {\"final\": \"<answer>\"}.",
            "Do not wrap the JSON in prose. Available tools:",
        ]
        if self._gateway_mode:
            for name in CANONICAL_TOOL_NAMES:
                verb, hints, summary = _CANONICAL_TOOL_PROMPT[name]
                lines.append(f"- {name} (verb={verb}; args: {', '.join(hints)}): {summary}")
        elif self._tools:
            for spec in self._tools.values():
                hint = ", ".join(spec.arg_hints) if spec.arg_hints else "(none)"
                lines.append(f"- {spec.name} (verb={spec.verb}; args: {hint}): {spec.summary}")
        else:
            lines.append("- (no tools available; reply with a final answer)")
        return "\n".join(lines)

    # --- turn loop -----------------------------------------------------------------------------

    def _open_turn(self, turn_id: str) -> None:
        """Emit ``turn open`` exactly once for ``turn_id`` (idempotent; codex parity)."""
        with self._span_lock:
            self._active_turn = turn_id
            if turn_id in self._turn_opened:
                return
            self._turn_opened.add(turn_id)
        self._emit("turn", "open", correlation_id=turn_id, summary="local-llm turn started",
                   payload={"turn_id": turn_id})

    def _close_turn(self, turn_id: str, marker: str, *, status: str | None, code: str | None = None,
                    payload_extra: dict[str, Any] | None = None) -> None:
        """Emit a terminal turn marker exactly once for ``turn_id`` (idempotent). Clears ``_active_turn``
        when it was this turn. kill()/interrupt() and the loop thread race to close; whoever wins emits,
        the loser no-ops (codex._close_turn parity)."""
        with self._span_lock:
            if turn_id in self._turn_closed:
                return
            self._turn_closed.add(turn_id)
            if self._active_turn == turn_id:
                self._active_turn = None
        payload: dict[str, Any] = {"turn_id": turn_id, "status": status}
        if payload_extra:
            payload.update(payload_extra)
        self._emit("turn", marker, correlation_id=turn_id, summary=f"local-llm turn {status or marker}",
                   payload=payload, code=code)

    def start_turn(self, prompt: str, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run the synchronous Kaizen-owned loop for ONE turn. Every model call counts toward
        ``max_turns`` (parse failures included); the loop never makes a ``max_turns+1``-th chat call.
        EVERY exit path is terminal -- it closes the turn span with exactly one marker AND returns a
        status dict.

        Per-iteration: (1) interrupt check -> close_canceled + CANCELED; (2) drain the steer queue into
        user messages; (3) call the chat provider -- ANY exception => close_fail BACKEND_ERROR + FAILED;
        (4) count the iteration; (5) parse the reply -> final (close_ok + OK), parse_error/unknown-tool/
        malformed-args (append a corrective user message, no decide(), continue), or a tool intent ->
        decide() (THE GATE) -> allow: run the runner; deny: declined (identical to codex); ask: fail-
        closed via on_approval. Loop exhausted without a final => close_fail MAX_TURNS_EXHAUSTED.
        """
        with self._span_lock:
            if self._killed:
                raise LocalLLMAdapterError(
                    DENIED_KILLED,
                    required_action="a killed adapter starts no turns; build a fresh adapter",
                )
            if self._closed:
                raise LocalLLMAdapterError(
                    DENIED_CLOSED,
                    required_action="a closed adapter starts no turns; build a fresh adapter",
                )
        opts = self._chat_opts(overrides)
        safe_prompt = neutralize_at_refs(prompt)
        # Resolve the provider BEFORE opening the span: a missing backend (DENIED_BACKEND_OFF) must
        # refuse structurally with NO span opened (codex parity: refusals precede span opens), so the
        # every-exit-path-terminal rule never sees a dangling turn.
        provider = self._resolve_provider()
        turn_id = self._id_factory("turn")
        # Fresh-turn hygiene: the interrupt flag is PER-TURN (a prior turn's interrupt must not cancel
        # this one) and a steer that raced the previous turn's close must not bleed into this one.
        self._interrupt.clear()
        self._post_apply_failure = None
        self._gateway_failure = None
        self._workspace_recovery_paths = ()
        with self._steer_lock:
            self._steer_queue.clear()
        self._open_turn(turn_id)

        # Copy accepted history for this request. Commit this prompt only with a final assistant reply;
        # a failed/canceled turn must not leave an orphan user message that breaks role alternation.
        with self._history_lock:
            prior_history = [dict(message) for message in self._history]
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt()},
            *prior_history,
            {"role": "user", "content": safe_prompt},
        ]
        iterations = 0
        while True:
            # (1) Interrupt honored at the iteration top (an in-flight chat/tool call completed first).
            if self._interrupt.is_set():
                self._close_turn(turn_id, "close_canceled", status="interrupt")
                return {"status": "CANCELED", "turn_id": turn_id, "iterations": iterations}

            # (2) Drain steered messages injected since the last iteration.
            self._drain_steer_into(messages)

            # Ceiling guard BEFORE the call: never make a (max_turns+1)-th chat call.
            if iterations >= self._max_turns:
                self._close_turn(turn_id, "close_fail", status="max_turns", code=MAX_TURNS_EXHAUSTED,
                                 payload_extra={"iterations": iterations})
                return {"status": "FAILED", "code": MAX_TURNS_EXHAUSTED, "turn_id": turn_id,
                        "iterations": iterations}

            # (3) The model call. ANY exception is a terminal turn failure (codex BACKEND_ERROR parity).
            try:
                stream_chat = getattr(provider, "stream_chat", None)
                if self._delta_cb is not None and callable(stream_chat):
                    extractor = _FinalDeltaExtractor()

                    def emit(fragment: str) -> None:
                        display = extractor.feed(fragment)
                        if display:
                            self._emit_delta(turn_id, display)

                    resp = stream_chat(messages, emit, **opts) if opts else stream_chat(messages, emit)
                else:
                    resp = provider(messages, **opts) if opts else provider(messages)
            except Exception as error:  # noqa: BLE001 -- any provider failure ends the turn terminally
                error_type = type(error).__name__
                self._log(f"chat provider error: {error_type}")
                self._close_turn(turn_id, "close_fail", status="backend_error", code=BACKEND_ERROR,
                                 payload_extra={"iterations": iterations, "error_type": error_type})
                return {"status": "FAILED", "code": BACKEND_ERROR, "turn_id": turn_id,
                        "iterations": iterations, "error_type": error_type}

            # (4) EVERY model call counts (parse failures included) -- the ceiling is on model calls.
            iterations += 1
            text = resp.get("text", "") if isinstance(resp, Mapping) else str(resp)
            messages.append({"role": "assistant", "content": text})

            # (5) Parse the (untrusted) reply into an intent.
            parsed = parse_tool_intent(text)
            kind = parsed[0]

            if kind == PARSE_FINAL:
                # The satisfied-tool-call detector: the model signalled completion.
                with self._history_lock:
                    self._history.extend((
                        {"role": "user", "content": safe_prompt},
                        {"role": "assistant", "content": parsed[1]},
                    ))
                    del self._history[:-_MAX_HISTORY_MESSAGES]
                self._close_turn(turn_id, "close_ok", status="completed",
                                 payload_extra={"iterations": iterations})
                return {"status": "OK", "final": parsed[1], "turn_id": turn_id, "iterations": iterations}

            if kind == PARSE_ERROR:
                # No tool; still counted. Feed a corrective message back and loop (never crash the turn).
                messages.append({"role": "user", "content": (
                    f"Your last reply could not be parsed ({parsed[1]}). Reply with EXACTLY ONE JSON "
                    "object: a tool call or a final answer.")})
                continue

            # kind == PARSE_TOOL
            name, args = parsed[1], parsed[2]
            spec = self._tools.get(name)
            known_gateway_tool = self._gateway_mode and name in CANONICAL_TOOL_NAMES
            if spec is None and not known_gateway_tool:
                # Unknown tool: no decide(), no spans -- correct and re-prompt with the tool list.
                available = ", ".join(CANONICAL_TOOL_NAMES if self._gateway_mode else self._tools.keys()) \
                    or "(none)"
                messages.append({"role": "user", "content": (
                    f"Unknown tool {name!r}. Available tools: {available}. Reply with one JSON object.")})
                continue

            # A canonical tool reaches the one provider-neutral gateway policy/broker/mutation path.
            # Explicit legacy ToolSpecs keep the pre-v5 hermetic/dispatch seam unchanged.
            if known_gateway_tool:
                self._run_gateway_tool(turn_id, name, args, messages)
                if self._gateway_failure is not None:
                    failure = dict(self._gateway_failure)
                    code = str(failure.get("code") or DENIED_TOOL_GATEWAY_UNAVAILABLE)
                    self._close_turn(
                        turn_id, "close_fail", status="tool_gateway", code=code,
                        payload_extra={"iterations": iterations},
                    )
                    return {
                        "status": "FAILED", "code": code, "turn_id": turn_id,
                        "iterations": iterations,
                    }
            else:
                self._run_gated_tool(turn_id, spec, name, args, messages)
            if self._post_apply_failure is not None:
                failure_code = str(self._post_apply_failure.get("code") or DENIED_PARTIAL_APPLY)
                self._close_turn(
                    turn_id, "close_fail", status="partial_apply", code=failure_code,
                    payload_extra={
                        key: value for key, value in self._post_apply_failure.items() if key != "code"
                    },
                )
                return {
                    "status": "FAILED", "code": failure_code, "turn_id": turn_id,
                    "iterations": iterations, "partial_apply": True,
                }

    def run_turn(self, prompt: str) -> TurnResult:
        """Run one turn and normalize the legacy local-loop mapping to the common H2 result."""

        result = self.start_turn(prompt)
        status = str(result.get("status") or "FAILED").upper()
        code = result.get("code")
        # A provider connection/protocol failure makes the local logical adapter unhealthy. Exhausting
        # a bounded model loop is a turn failure but leaves the provider usable for another prompt.
        fatal = status == "FAILED" and (
            code in (BACKEND_ERROR, DENIED_PARTIAL_APPLY, "DENIED_WORKSPACE_RECOVERY_REQUIRED")
            or self._gateway_failure is not None
        )
        return TurnResult(
            status=status,
            vendor_turn_id=str(result["turn_id"]) if result.get("turn_id") is not None else None,
            final_text=str(result["final"]) if result.get("final") is not None else None,
            error_code=str(code) if code is not None else None,
            fatal=fatal,
        )

    @property
    def active_turn_id(self) -> str | None:
        with self._span_lock:
            return self._active_turn

    @property
    def workspace_recovery_paths(self) -> tuple[str, ...]:
        return self._workspace_recovery_paths

    @property
    def conversation_history(self) -> tuple[dict[str, str], ...]:
        """Read-only copy of the accepted complete-message history (primarily a proof surface)."""

        with self._history_lock:
            return tuple(dict(message) for message in self._history)

    @staticmethod
    def _gateway_open_metadata(
        turn_id: str, tool_call_id: str, name: str, args: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Builds tool_call open payload; includes run_process argv/exec fields ONLY for kaizen_run_process (never leaks write args)."""
        payload: dict[str, Any] = {
            "name": name,
            "tool_call_id": tool_call_id,
            "turn_id": turn_id,
        }
        if name == "kaizen_run_process":
            for key in ("executable", "argv", "cwd", "timeout_ms"):
                if key in args:
                    payload[key] = args[key]
        return payload

    @staticmethod
    def _gateway_result_metadata(result: Mapping[str, Any]) -> dict[str, Any]:
        """Allowlists a fixed safe key set and replaces raw stdout with sha256+byte-length (secret/size hygiene)."""
        nested = result.get("result")
        source = nested if isinstance(nested, Mapping) else result
        safe = {
            key: source[key]
            for key in (
                "exit_code", "timed_out", "truncated", "effects_unknown", "policy_decision",
                "applied", "change_count", "executor_status",
            )
            if key in source
        }
        stdout = source.get("stdout")
        if isinstance(stdout, str):
            encoded = stdout.encode("utf-8")
            safe.update({"stdout_sha256": hashlib.sha256(encoded).hexdigest(), "stdout_bytes": len(encoded)})
        safe.update(normalize_apply_evidence(source))
        return safe

    def _run_gateway_tool(
        self,
        turn_id: str,
        name: str,
        args: Mapping[str, Any],
        messages: list[dict[str, str]],
    ) -> None:
        """Invoke one canonical tool once; ToolGateway is the sole policy/broker/mutation authority."""

        tool_call_id = self._id_factory("toolcall")
        self._emit(
            "tool_call", "open", correlation_id=tool_call_id,
            summary="Ollama tool opened",
            payload=self._gateway_open_metadata(turn_id, tool_call_id, name, args),
        )
        try:
            from ..tool_gateway import ToolContext

            if self._gateway is None:
                raise RuntimeError("gateway unavailable")
            result = self._gateway.execute(
                name,
                dict(args),
                ToolContext(
                    policy.Actor(
                        self._engine,
                        self.session_id or "unbound",
                        self.current_epoch,
                        thread_id=turn_id,
                    ),
                    self.current_epoch,
                    tool_call_id,
                ),
            )
            if not isinstance(result, Mapping):
                raise RuntimeError("gateway result invalid")
            normalized = dict(result)
        except Exception:  # noqa: BLE001 -- model-facing failures are always safe structured values
            normalized = {
                "status": "ERROR",
                "tool": name,
                "code": DENIED_TOOL_GATEWAY_UNAVAILABLE,
                "fatal": True,
            }
        status = str(normalized.get("status") or "DENIED").upper()
        code = normalized.get("code") if isinstance(normalized.get("code"), str) else None
        safe_result = self._gateway_result_metadata(normalized)
        recovery_paths = staged_cleanup_paths(safe_result)
        if recovery_paths:
            self._workspace_recovery_paths = recovery_paths
        persistence_required = safe_result.get("partial_apply") is True \
            or safe_result.get("mismatch_evidence_uncertain") is True \
            or code == "DENIED_WORKSPACE_RECOVERY_REQUIRED"
        try:
            self._emit(
                "tool_call",
                "close_ok" if status == "OK" else "close_fail",
                correlation_id=tool_call_id,
                summary=f"Ollama tool {status.lower()}",
                payload={"name": name, "tool_call_id": tool_call_id, "result": safe_result},
                code=code,
                persistence_required=persistence_required,
            )
        except Exception:
            normalized = {
                "status": "DENIED",
                "tool": name,
                "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                "fatal": True,
            }
        if normalized.get("fatal") is True:
            # The durable tool close is authoritative. Stop this turn before any fallible model-facing
            # serialization can race a second provider iteration over the governed fatal result.
            self._gateway_failure = dict(normalized)
            return
        try:
            serialized = json.dumps(
                normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            )
        except Exception:  # noqa: BLE001 -- model-facing serialization failure terminalizes this turn
            self._gateway_failure = {
                "status": "DENIED",
                "tool": name,
                "code": DENIED_TOOL_GATEWAY_UNAVAILABLE,
                "fatal": True,
            }
            return
        messages.append({"role": "user", "content": f"tool_result {name}: " + serialized})

    def _run_gated_tool(self, turn_id: str, spec: ToolSpec, name: str, args: Mapping[str, Any],
                        messages: list[dict[str, str]]) -> None:
        """THE GATE BETWEEN PARSE AND EXECUTE. Build a policy.RequestedAction from the (untrusted) tool
        name/args, open a ``tool_call`` span, run decide(), then dispatch on the result: allow -> run the
        runner (codex approved-path parity); deny -> declined with invariant_id (the ledger #21 proof
        surface, EXACT codex parity); ask -> fail-closed resolve. Every branch appends a user message so
        the loop can continue; a deny/tool error NEVER crashes the turn."""
        tool_call_id = self._id_factory("toolcall")
        self._emit("tool_call", "open", correlation_id=tool_call_id, summary=f"tool call {name}",
                   payload={"tool": name, "args_keys": sorted(args.keys()), "turn_id": turn_id})

        # Project the untrusted args into the RequestedAction the chokepoint gates on (READ, never exec).
        action = policy.RequestedAction(
            actor=policy.Actor(self._engine, self.session_id or self._engine, self.current_epoch,
                               thread_id=turn_id),
            verb=spec.verb,
            targets=spec.resolve_targets(args),
            command=spec.resolve_command(args),
            raw={"cwd": self.session_cwd, "tool": name, "args": dict(args)},
        )
        decision = self.policy_engine.decide(action, self.current_epoch)

        if decision.result == policy.ALLOW:
            denial = check_mutation_guard(self._mutation_guard, action) if mutation_guard_required(action) else None
            if denial is not None:
                self._emit_mutation_guard_denial(decision, tool_call_id, name, denial, messages)
                return
            self._emit("approval", "resolved", correlation_id=decision.correlation_hash,
                       summary="tool allowed by policy rule",
                       payload={"decision": DECISION_APPROVED, "rule_id": decision.rule_id,
                                "tool_call_id": tool_call_id},
                       code=decision.rule_id)
            self._execute_tool(turn_id, tool_call_id, spec, name, args, messages)
            return

        if decision.result == policy.DENY:
            # EXACT codex parity: same event shape, same code (invariant_id or rule_id). This is what
            # closes ledger #21 -- a model-injected git push / protected write denies identically here.
            code = decision.invariant_id or decision.rule_id
            self._emit("approval", "declined", correlation_id=decision.correlation_hash,
                       summary="tool denied by policy invariant/rule",
                       payload={"decision": DECISION_DENIED, "invariant_id": decision.invariant_id,
                                "rule_id": decision.rule_id, "tool_call_id": tool_call_id},
                       code=code)
            self._emit("tool_call", "close_fail", correlation_id=tool_call_id,
                       summary=f"tool call {name} denied by policy",
                       payload={"tool": name, "tool_call_id": tool_call_id,
                                "invariant_id": decision.invariant_id, "rule_id": decision.rule_id},
                       code=code)
            messages.append({"role": "user", "content": (
                f"Tool {name!r} was denied by policy: {decision.reason}. Choose a different action or "
                "reply with a final answer.")})
            return

        # ask: fail-closed resolve (codex._resolve_non_allow parity).
        requires_diff_approval = name == "write_file" and spec.verb == "file_write"
        resolved = self._resolve_non_allow(
            decision,
            correlation_hint=tool_call_id,
            approval_request={
                "negotiated": True,
                "summary": f"Review file change from {name}",
                "diff_request": {"tool_name": name, "tool_input": dict(args)},
            } if requires_diff_approval else None,
        )
        if resolved.decision == DECISION_APPROVED:
            denial = check_mutation_guard(self._mutation_guard, action) if mutation_guard_required(action) else None
            if denial is not None:
                self._emit_mutation_guard_denial(decision, tool_call_id, name, denial, messages)
                return
            if self._approval_broker_cb is None:
                self._emit("approval", "resolved", correlation_id=decision.correlation_hash,
                           summary="tool approval resolved (human approve)",
                           payload={"decision": DECISION_APPROVED, "tool_call_id": tool_call_id})
            execute_args = dict(resolved.updated_input) if resolved.updated_input is not None else args
            self._execute_tool(
                turn_id, tool_call_id, spec, name, execute_args, messages,
                post_apply=resolved.post_apply,
            )
            return
        if self._approval_broker_cb is None:
            self._emit("approval", "declined", correlation_id=decision.correlation_hash,
                       summary="tool approval resolved (human/timeout deny)",
                       payload={"decision": DECISION_DENIED, "tool_call_id": tool_call_id})
        self._emit("tool_call", "close_fail", correlation_id=tool_call_id,
                   summary=f"tool call {name} denied at approval",
                   payload={"tool": name, "tool_call_id": tool_call_id})
        messages.append({"role": "user", "content": (
            f"Tool {name!r} was not approved. Choose a different action or reply with a final answer.")})

    def _emit_mutation_guard_denial(
        self,
        decision: policy.Decision,
        tool_call_id: str,
        name: str,
        denial: Mapping[str, Any],
        messages: list[dict[str, str]],
    ) -> None:
        """Close an otherwise-approved tool fail-closed when the workspace writer guard refuses it."""

        code = str(denial["code"])
        payload: dict[str, Any] = {
            "decision": DECISION_DENIED,
            "denial_code": code,
            "tool": name,
            "tool_call_id": tool_call_id,
        }
        if isinstance(denial.get("holder"), Mapping):
            payload["holder"] = dict(denial["holder"])
        if self._approval_broker_cb is None or decision.result != policy.ASK:
            self._emit(
                "approval", "declined", correlation_id=decision.correlation_hash,
                summary="tool denied by workspace writer guard", payload=payload, code=code,
            )
        self._emit(
            "tool_call", "close_fail", correlation_id=tool_call_id,
            summary=f"tool call {name} denied by workspace writer guard",
            payload={"tool": name, "tool_call_id": tool_call_id, "denial_code": code}, code=code,
        )
        messages.append({
            "role": "user",
            "content": f"Tool {name!r} was not run: workspace writer guard denied it ({code}).",
        })

    def _execute_tool(self, turn_id: str, tool_call_id: str, spec: ToolSpec, name: str,
                      args: Mapping[str, Any], messages: list[dict[str, str]],
                      *, post_apply: Callable[[], Mapping[str, Any]] | None = None) -> None:
        """Run an ALLOWED tool's runner (decide() already ALLOWed). A runner exception is a tool error,
        not a turn error: close the tool_call close_fail (code TOOL_ERROR) and feed the error back so the
        model can recover -- the turn continues. Success closes close_ok and appends the tool_result."""
        try:
            result = spec.run(args)
        except Exception as error:  # noqa: BLE001 -- a tool failure recovers, it never ends the turn
            self._log(f"tool {name} runner error: {error}")
            self._emit("tool_call", "close_fail", correlation_id=tool_call_id,
                       summary=f"tool call {name} runner error", code=TOOL_ERROR,
                       payload={"tool": name, "tool_call_id": tool_call_id, "error": str(error)})
            messages.append({"role": "user", "content": f"tool_error {name}: {error}"})
            if post_apply is not None:
                self._audit_tool_apply(tool_call_id, name, post_apply, close_tool_on_failure=False)
            return
        if post_apply is not None and not self._audit_tool_apply(tool_call_id, name, post_apply):
            return
        self._emit("tool_call", "close_ok", correlation_id=tool_call_id,
                   summary=f"tool call {name} completed",
                   payload={"tool": name, "tool_call_id": tool_call_id})
        messages.append({"role": "user", "content": f"tool_result {name}: {result}"})

    def _audit_tool_apply(
        self,
        tool_call_id: str,
        name: str,
        callback: Callable[[], Mapping[str, Any]],
        *,
        close_tool_on_failure: bool = True,
    ) -> bool:
        """Return apply proof; on failure record recovery state and optionally close the tool span."""
        try:
            result = callback()
        except Exception:  # noqa: BLE001 -- missing audit proof pauses the conversation fail-closed
            result = {
                "status": "DENIED",
                "code": DENIED_PARTIAL_APPLY,
                "partial_apply": True,
                "mismatches": [{"path": "", "reason": "post_apply_audit_unavailable"}],
            }
        if isinstance(result, Mapping) and result.get("status") == "OK":
            self._emit(
                "verification", "point", correlation_id=tool_call_id,
                summary=f"tool call {name} post-apply hashes verified",
                payload={"tool": name, "tool_call_id": tool_call_id,
                         "partial_apply": False, "mismatches": []},
            )
            return True
        evidence = normalize_apply_evidence(
            result if isinstance(result, Mapping) else {"partial_apply": True, "mismatches": None},
        )
        self._post_apply_failure = {"code": DENIED_PARTIAL_APPLY, **evidence}
        payload = {"tool": name, "tool_call_id": tool_call_id, **evidence}
        try:
            self._emit(
                "verification", "point", correlation_id=tool_call_id,
                summary=f"tool call {name} post-apply hashes mismatched", code=DENIED_PARTIAL_APPLY,
                payload=payload, persistence_required=True,
            )
        except Exception:
            self._post_apply_failure["code"] = "DENIED_WORKSPACE_RECOVERY_REQUIRED"
            return False
        if close_tool_on_failure:
            try:
                self._emit(
                    "tool_call", "close_fail", correlation_id=tool_call_id,
                    summary=f"tool call {name} post-apply hash mismatch", code=DENIED_PARTIAL_APPLY,
                    payload=payload, persistence_required=True,
                )
            except Exception:
                self._post_apply_failure["code"] = "DENIED_WORKSPACE_RECOVERY_REQUIRED"
        return False

    def _resolve_non_allow(
        self,
        decision: policy.Decision,
        *,
        correlation_hint: Any = None,
        approval_request: Mapping[str, Any] | None = None,
    ) -> BrokerApprovalResult:
        """Resolve ASK through the daemon broker when installed, else the legacy C4/resolver path."""
        request = {
            "decision_hint": decision.result,
            "reason": decision.reason,
            "correlation_id": decision.correlation_hash,
            "item": correlation_hint,
        }
        if approval_request is not None:
            request.update(dict(approval_request))
        if decision.result == policy.ASK and self._approval_broker_cb is not None:
            return resolve_broker_result(self._approval_broker_cb, request)
        self._emit("approval", "open", correlation_id=decision.correlation_hash,
                   summary="tool requires approval (ask)",
                   payload={"decision_hint": decision.result, "reason": decision.reason,
                            "item": correlation_hint})
        if self.session_id:
            try:
                self.policy_engine.record_ask(decision, self.session_id)
            except Exception as error:  # noqa: BLE001 -- ask persistence must not break the reply path
                self._log(f"record_ask failed: {error}")
        if self._approval_cb is None:
            return BrokerApprovalResult(DECISION_DENIED)  # fail-closed: no human surface -> deny
        resolved_decision = DECISION_DENIED
        done = threading.Event()

        def _run() -> None:
            nonlocal resolved_decision
            try:
                candidate = self._approval_cb(request)
                resolved_decision = DECISION_APPROVED if candidate == DECISION_APPROVED else DECISION_DENIED
            except Exception as error:  # noqa: BLE001 -- resolver bug fails closed
                self._log(f"on_approval resolver error: {error}")
            finally:
                done.set()

        # The resolver is daemonized because an arbitrary UI callback cannot be force-stopped. On
        # timeout its only possible late mutation is this function's local decision value.
        threading.Thread(target=_run, name="local-llm-approval-resolver", daemon=True).start()
        if not done.wait(self._approval_timeout):
            self._log("approval resolver timed out -> deny (fail-closed)")
            return BrokerApprovalResult(DECISION_DENIED)
        return BrokerApprovalResult(resolved_decision)

    # --- steer / interrupt ---------------------------------------------------------------------

    def steer(self, text: str, msg: str | None = None) -> dict[str, Any]:
        """inject-next-iteration steer: thread-safely queue ``msg`` for consumption at the top of the
        next loop iteration (an in-flight chat/tool call completes first). No active turn => refuse
        DENIED_NO_ACTIVE_TURN (there is nothing to steer)."""
        # ``steer(turn, msg)`` is the temporary legacy shim; H2 calls ``steer(text)`` and targets the
        # adapter's real active vendor turn internally.
        instruction = neutralize_at_refs(msg if msg is not None else text)
        with self._span_lock:
            active = self._active_turn
        if active is None:
            raise LocalLLMAdapterError(
                DENIED_NO_ACTIVE_TURN,
                required_action="no local-llm turn is in flight to steer",
            )
        with self._steer_lock:
            self._steer_queue.append(instruction)
        return {"status": "OK", "queued": True, "turn_id": active}

    def _drain_steer_into(self, messages: list[dict[str, str]]) -> None:
        """Drain queued steer text under lock and append each item as a user message."""
        with self._steer_lock:
            queued = list(self._steer_queue)
            self._steer_queue.clear()
        for msg in queued:
            messages.append({"role": "user", "content": msg})

    def interrupt(self, turn: str | None = None) -> dict[str, Any]:
        """Set the interrupt flag and return immediately; the loop honors it at the next iteration top
        (an in-flight chat/tool call completes first -- documented, non-preemptive). Idempotent when no
        turn is active, and PER-TURN: start_turn clears the flag, so an interrupt never cancels a later
        turn (kill() is the permanent stop)."""
        with self._span_lock:
            active = self._active_turn
        self._interrupt.set()
        termination_proven = self._cancel_gateway_tools(2.0)
        return {
            "status": "OK" if termination_proven else "ERROR",
            "interrupted": True,
            "turn_id": active or turn,
            "termination_proven": termination_proven,
        }

    # --- subagent (Kaizen-simulated child run) -------------------------------------------------

    def spawn_subagent(self, prompt: str, *, max_turns: int | None = None) -> dict[str, Any]:
        """Kaizen-simulated subagent: a CHILD LocalLLMAdapter that SHARES the policy engine, chat
        provider, tools, recorder, and logger, plus this adapter's session binding + epoch (per M3 the
        subagent INHERITS the decider -- the same engine gates it, so a child git-push intent is denied
        by the same invariant). Emits ``subagent open`` then runs one child turn and emits ``close_ok``/
        ``close_fail`` on the child's status. Returns the child result + subagent_id."""
        subagent_id = self._id_factory("subagent")
        self._emit("subagent", "open", correlation_id=subagent_id,
                   summary="kaizen-simulated subagent started",
                   payload={"parent_engine": self._engine, "subagent_id": subagent_id})
        child = LocalLLMAdapter(
            self.policy_engine,
            chat_provider=self._chat_provider,
            tools=None if self._gateway_mode else self._tools,
            recorder=self._recorder,
            logger=self._log,
            clock=self._clock,
            id_factory=self._id_factory,
            engine_name=self._engine,
            model=self._model,
            max_turns=max_turns if max_turns is not None else self._max_turns,
            approval_timeout=self._approval_timeout,
            workspace_root=self._workspace_root,
            tool_spawner=self._tool_spawner,
            gateway_factory=self._gateway_factory,
            apply_recovery_callback_factory=self._apply_recovery_callback_factory,
            workspace_path_authority=self._workspace_path_authority,
        )
        child.bind_session(self.session_id)
        child.current_epoch = self.current_epoch
        if self._approval_cb is not None:
            child.on_approval(self._approval_cb)
        child.set_approval_broker(self._approval_broker_cb)
        child.set_mutation_guard(self._mutation_guard)
        if child._gateway_mode:
            child._gateway = child._make_gateway(None)
        try:
            child.start_session(cwd=self.session_cwd)
            result = child.start_turn(prompt)
        except BaseException:
            teardown = child.kill()
            if teardown.get("termination_proven") is not True:
                raise LocalLLMAdapterError(
                    "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                    required_action="stop after unproven simulated-subagent teardown",
                ) from None
            raise
        try:
            teardown = child.close()
        except LocalLLMAdapterError:
            teardown = child.kill()
        if teardown.get("termination_proven") is not True:
            result = {
                "status": "FAILED",
                "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                "fatal": True,
                "required_action": "stop after unproven simulated-subagent teardown",
            }
        marker = "close_ok" if result.get("status") == "OK" else "close_fail"
        self._emit("subagent", marker, correlation_id=subagent_id,
                   summary=f"kaizen-simulated subagent {result.get('status')}",
                   payload={"subagent_id": subagent_id, "status": result.get("status")})
        return {**result, "subagent_id": subagent_id}

    # --- teardown ------------------------------------------------------------------------------

    def close(self) -> Mapping[str, Any]:
        """Cleanly close an idle logical conversation. Idempotent and distinct from ``kill``."""

        with self._span_lock:
            if self._active_turn is not None:
                raise LocalLLMAdapterError(
                    DENIED_SESSION_NOT_IDLE,
                    required_action="interrupt or kill the active turn before closing the session",
                )
            if self._closed:
                return {"status": "OK", "closed": True, "already_closed": True, "termination_proven": True}
            # Claim the logical close while holding the same lock used by competing close/start checks.
            # A failed physical teardown remains closed and can be retried only through kill().
            self._closed = True
        termination_proven = self._cancel_gateway_tools(5.0)
        if termination_proven:
            termination_proven = self._close_gateway()
        return {
            "status": "OK" if termination_proven else "ERROR",
            "closed": True,
            "already_closed": False,
            "termination_proven": termination_proven,
        }

    def kill(self) -> dict[str, Any]:
        """Terminal teardown (§A.3 every-exit-path-terminal). Set the killed + interrupt flags, then
        close any open turn ``close_canceled`` under the span lock -- idempotent with the loop thread's
        own later close (whichever wins emits, the other no-ops). No session-close event (codex parity:
        a session has no terminal marker). Idempotent. Unlike interrupt() (per-turn), kill() is
        PERMANENT: subsequent start_turn calls refuse DENIED_KILLED (there is no process to reap, so the
        killed flag is what "the child is gone" means here)."""
        with self._span_lock:
            self._killed = True
        self._interrupt.set()
        termination_proven = self._cancel_gateway_tools(5.0)
        if termination_proven:
            termination_proven = self._close_gateway()
        with self._span_lock:
            active_turn = self._active_turn
        if active_turn is not None:
            self._close_turn(active_turn, "close_canceled", status="killed")
        return {
            "status": "OK" if termination_proven else "ERROR",
            "killed": True,
            "termination_proven": termination_proven,
        }

    def _cancel_gateway_tools(self, timeout: float) -> bool:
        """Return whether all gateway-process termination is proven within ``timeout``."""
        gateway = self._gateway
        cancel = getattr(gateway, "cancel_active_processes", None)
        if not callable(cancel):
            # No gateway or a non-gateway adapter cannot own gateway child processes.
            return gateway is None or not self._gateway_mode
        try:
            outcome = cancel(timeout)
        except Exception:
            return False
        return isinstance(outcome, Mapping) and outcome.get("termination_proven") is True

    def _close_gateway(self) -> bool:
        """Return true when no close hook exists or the gateway proves a successful close."""
        close = getattr(self._gateway, "close", None)
        if not callable(close):
            return True
        try:
            return close() is True
        except Exception:
            return False

    # --- internals -----------------------------------------------------------------------------

    def _resolve_provider(self) -> Callable[..., dict[str, Any]]:
        """Return the chat provider, building the default LAZILY on first use (so importing this module
        never imports backends). A cached injected/default provider is reused across turns."""
        if self._chat_provider is None:
            self._chat_provider = default_chat_provider()
        return self._chat_provider

    @staticmethod
    def _chat_opts(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
        """Sanctioned pass-through generation options for the provider (e.g. max_tokens/temperature).
        Only an explicit ``options`` mapping in overrides is forwarded; nothing model-authored reaches
        here (overrides come from the CALLER, never the model)."""
        if overrides and isinstance(overrides.get("options"), Mapping):
            return dict(overrides["options"])
        return {}

    def on_approval(self, cb: Callable[[dict[str, Any]], str]) -> None:
        """Register the human-decision resolver for ``ask`` outcomes (codex.on_approval parity).
        ``cb(request)`` returns DECISION_APPROVED / DECISION_DENIED (tests resolve it programmatically).
        The ask reply blocks on it, bounded by approval_timeout; a timeout fails closed to denied."""
        self._approval_cb = cb

    def on_delta(self, cb: Callable[[dict[str, Any]], None]) -> None:
        """Register an ephemeral normalized text-delta sink for providers that prove streaming."""

        self._delta_cb = cb

    def _emit_delta(self, turn_id: str, fragment: Any) -> None:
        """Emit a nonempty text delta best-effort without allowing sink errors to fail the turn."""
        if self._delta_cb is None or not isinstance(fragment, str) or not fragment:
            return
        try:
            self._delta_cb({"turn_id": turn_id, "text": fragment})
        except Exception as error:  # noqa: BLE001 -- an ephemeral UI sink never fails the model turn
            # Keep delta-sink diagnostics metadata-only; exception messages can contain streamed text.
            self._log(f"delta sink error: {type(error).__name__}")

    def set_mutation_guard(self, callback: MutationGuard | None) -> None:
        """Install the final policy-approved action gate used by the daemon writer lease."""

        self._mutation_guard = callback

    def set_approval_broker(self, callback: ApprovalBrokerCallback | None) -> None:
        """Install the daemon-owned synchronous approval authority for ASK decisions."""

        self._approval_broker_cb = callback

    # --- recorder ------------------------------------------------------------------------------

    def _emit(self, event_kind: str, marker: str, *, summary: str,
              correlation_id: str | None = None, payload: dict[str, Any] | None = None,
              code: str | None = None, persistence_required: bool = False) -> None:
        """Emit one normalized recorder event {event_kind, marker, correlation_id, summary, payload}
        (codex._emit parity). The supervisor funnels these to T5/T6/T8; tests capture them in a list.
        NEVER writes the DB here and NEVER prints to stdout (logging goes to the injected logger); a
        recorder-sink bug is logged. A critical partial/uncertain apply close is the sole exception: its
        failure propagates so the turn enters recovery instead of claiming unrecorded durable truth."""
        event: dict[str, Any] = {
            "event_kind": event_kind,
            "marker": marker,
            "correlation_id": correlation_id,
            "summary": summary,
            "payload": payload or {},
        }
        if code:
            event["code"] = code
        if persistence_required:
            event["persistence_required"] = True
        try:
            self._recorder(event)
        except Exception as error:  # noqa: BLE001 -- a recorder sink bug must not break the adapter
            self._log(f"recorder sink error: {error}")
            if persistence_required:
                raise RuntimeError("critical recorder failure") from None
