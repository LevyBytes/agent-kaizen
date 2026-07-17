"""Codex ``app-server`` adapter (v8 M4, plan §A.1 Codex column / §A.2 / §A.3, probe P-B).

``codex app-server`` driven as an OWNED child behind the M1 supervisor and the M3 policy
chokepoint. The app-server speaks stdio, newline-delimited JSON-RPC 2.0 (one object per line):
client requests get id-correlated results, and the server ALSO issues server->client requests
(server-assigned id) the client must answer by id -- notably the per-item approval
``item/commandExecution/requestApproval``. This adapter is that answering side, and every
approval is funneled through :meth:`policy.PolicyEngine.decide` PER ITEM before the reply is sent.

Ground truth is the P-B probe verdict (``AI/work/orchestration-v8/probes/P-B-codex/verdict.md``):

- initialize -> result carries ``userAgent`` (embeds the CLI semver, e.g. ".../0.130.0 ..."),
  ``codexHome``, ``platformFamily``; there is NO ``protocolVersion`` field, so the version floor is
  the parsed userAgent semver + the method surface, nothing else. Client then sends the
  ``initialized`` notification. One startup ``remoteControl/status/changed`` may arrive.
- thread/start -> ``result.thread.id`` (also ``.sessionId`` = the rollout root, ``.path``). Params of
  note: cwd, approvalPolicy, sandbox, model, ``config`` (the ONLY sanctioned per-spawn config path --
  never edit ``~/.codex/config.toml``), ``ephemeral``.
- turn/start {threadId, input} -> ``result.turn.id``; stream turn/started, item/started|completed,
  turn/completed {status}. turn/interrupt {threadId, turnId} -> ``{}`` ack, then
  turn/completed(status=interrupted). CAVEAT (P-B 2e): the in-flight commandExecution ITEM emits NO
  terminal event on interrupt, so turn/completed(interrupted) is treated as closing every still-open
  child item span (synthesized ``close_canceled``).
- Second controller (P-B 2c): the app-server enforces NO thread lock; two servers can resume one
  persisted thread and corrupt the shared rollout jsonl. The ORCHESTRATOR owns single-controller
  arbitration -- this adapter keeps a per-threadId lease under the supervisor runtime dir and
  detect-and-refuses a resume held by a different LIVE controller.
- Side-channels (P-B 2d): ``command/exec`` (threadless, synchronous, honors only its own sandboxPolicy,
  no approval) is exposed ONLY as :meth:`exec_wrapped` (explicit restrictive sandbox required, routed
  through the policy engine first); ``thread/shellCommand`` runs FULLY UNSANDBOXED with no approval and
  is NEVER exposed -- no code path here sends that method except the refusal constant.
- Approval-trigger nuance (P-B 2b): under a read-only sandbox a read-only command runs to completion
  with NO approval even under approvalPolicy=untrusted; approvals fire only when the sandbox would BLOCK
  the action. Any live gate must therefore use a sandbox-violating action (tests script the fake).

Windows reality (§A.1): no OS sandbox exists for Codex, so ``sandbox_mode`` is recorded as
``effectively-unsandboxed`` on win32 -- the supervisor is the only real gate.

Headless-auth note for M14 (P-B 2f): tokens live in a plain file ``$CODEX_HOME/.codex/auth.json``
(no OS keychain); a non-interactive host reuses auth by placing a valid auth.json under a controlled
``CODEX_HOME`` and servicing the ``account/chatgptAuthTokens/refresh`` server request (or refreshing
out-of-band). Per-agent CODEX_HOME doubles as auth + rollout isolation.

STDOUT stays pristine: every log line goes to the injected logger (default stderr), so a consumer can
hand the child's stdout straight through. This module writes NO ledger rows itself -- it emits
normalized recorder events through an injected ``recorder`` callable (the supervisor funnels them to
T5/T6/T8); tests capture them in a list.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import children, policy
from ..supervisor import RUNTIME_DIR
from ..vendor_env_scrub import SENSITIVE_ENV as _SENSITIVE_ENV
from . import (
    ApprovalBrokerCallback,
    BrokerApprovalResult,
    MutationGuard,
    check_mutation_guard,
    mutation_guard_required,
    resolve_broker_result,
)
from ..session_artifacts import neutralize_at_refs

# --- version floor -----------------------------------------------------------------------------

# P-B recommendation: pin codex-cli >= 0.130.0. There is no initialize.protocolVersion, so the floor
# is the semver parsed out of the initialize result userAgent (below floor OR unparseable => observe
# only). The method-surface probe (assert_required_methods) is the second half of the gate.
VERSION_FLOOR: tuple[int, int, int] = (0, 130, 0)

# The complete H2.3 app-server surface. ``schema_from_live_dump`` reads the installed protocol bundle
# without starting an authenticated turn. Drivability additionally requires bounded read access (see
# ``probe_schema_capabilities``): hooks alone are defense-in-depth and never satisfy that boundary.
CORE_CLIENT_METHODS: frozenset[str] = frozenset({
    "model/list",
    "config/read",
    "hooks/list",
    "thread/start",
    "turn/start",
    "turn/steer",
    "turn/interrupt",
    "command/exec",
    "thread/shellCommand",
    "windowsSandbox/readiness",
})
APPROVAL_METHODS: frozenset[str] = frozenset({
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
})
INTERACTION_METHODS: frozenset[str] = frozenset({
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
})
LEGACY_APPROVAL_METHODS: frozenset[str] = frozenset({"execCommandApproval", "applyPatchApproval"})
REQUIRED_METHODS: frozenset[str] = frozenset({
    *CORE_CLIENT_METHODS,
    *APPROVAL_METHODS,
    *INTERACTION_METHODS,
})

# Server->client request names. The singular constant is retained for the v8 compatibility tests.
APPROVAL_METHOD = "item/commandExecution/requestApproval"
FILE_CHANGE_APPROVAL_METHOD = "item/fileChange/requestApproval"
PERMISSIONS_APPROVAL_METHOD = "item/permissions/requestApproval"
USER_INPUT_METHODS = frozenset({"item/tool/requestUserInput", "tool/requestUserInput"})
MCP_ELICITATION_METHOD = "mcpServer/elicitation/request"
LEGACY_EXEC_APPROVAL_METHOD = "execCommandApproval"
LEGACY_PATCH_APPROVAL_METHOD = "applyPatchApproval"
# Auth-refresh server request (P-B 2f). Not answered in tests; recorded for M14. It is an unknown
# request as far as this adapter's answering logic goes -> it gets the safe default reply.
AUTH_REFRESH_METHOD = "account/chatgptAuthTokens/refresh"
# The unsandboxed full-access side channel. Named ONLY here (the refusal constant) and never sent.
SHELL_COMMAND_METHOD = "thread/shellCommand"

# Internal decision vocabulary (ledger/recorder events + on_approval callbacks). The WIRE speaks the
# vendor-native ReviewDecision verbs instead: P-B's live app-server REJECTED a bare
# {"decision":"denied"} and expects accept|acceptForSession|decline|cancel (v8 §A.1 pins the same),
# so every approval reply maps through _WIRE_DECISION before it is sent. acceptForSession is
# deliberately never sent -- per-item gating forbids batch/session-wide grants.
DECISION_APPROVED = "approved"
DECISION_DENIED = "denied"
DECISION_ABORT = "abort"
WIRE_ACCEPT = "accept"
WIRE_DECLINE = "decline"
WIRE_CANCEL = "cancel"
_WIRE_DECISION = {DECISION_APPROVED: WIRE_ACCEPT, DECISION_DENIED: WIRE_DECLINE, DECISION_ABORT: WIRE_CANCEL}

# Refusal codes (DENIED_* payloads on the observe/refuse paths).
DENIED_CODEX_VERSION = "DENIED_CODEX_VERSION"
DENIED_THREAD_LEASED = "DENIED_THREAD_LEASED"
DENIED_UNSANDBOXED_SIDE_CHANNEL = "DENIED_UNSANDBOXED_SIDE_CHANNEL"
DENIED_SANDBOX_REQUIRED = "DENIED_SANDBOX_REQUIRED"
DENIED_SIDE_CHANNEL_POLICY = "DENIED_SIDE_CHANNEL_POLICY"
DENIED_POLICY_GATE_UNAVAILABLE = "DENIED_POLICY_GATE_UNAVAILABLE"
DENIED_PROFILE_UNSUPPORTED = "DENIED_PROFILE_UNSUPPORTED"
DENIED_PROFILE_MISMATCH = "DENIED_PROFILE_MISMATCH"
DENIED_CREDENTIAL_FILE = "DENIED_CREDENTIAL_FILE"
DENIED_ENGINE_UNAVAILABLE = "DENIED_ENGINE_UNAVAILABLE"
DENIED_USER_INPUT_ANSWER_REQUIRED = "DENIED_USER_INPUT_ANSWER_REQUIRED"

CODEX_TURN_TIMEOUT = "CODEX_TURN_TIMEOUT"
CODEX_PROTOCOL_ERROR = "CODEX_PROTOCOL_ERROR"
CODEX_FINAL_MESSAGE_MISSING = "CODEX_FINAL_MESSAGE_MISSING"
CODEX_CHILD_EXITED = "CODEX_CHILD_EXITED"
RUNTIME_CLEANUP_FAILED = "RUNTIME_CLEANUP_FAILED"

SUPPORTED_PERMISSION_MODES: tuple[str, ...] = ("plan", "ask", "agent")
SUPPORTED_AUTH_MODES: tuple[str, ...] = ("subscription", "api-key")
API_KEY_FILE_ENV = "KAIZEN_CODEX_API_KEY_FILE"
MAX_API_KEY_FILE_BYTES = 16 * 1024
# Identity vars restored to ambient for subscription children: a private empty CODEX_HOME -- or the
# supervisor vendor-runtime masks (HOME/USERPROFILE/...) -- hides auth.json and breaks the installed
# login (pinned 2026-07-10: subscription = installed Codex auth). api-key children keep the full mask.
_SUBSCRIPTION_IDENTITY_ENV = (
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "XDG_CONFIG_HOME",
    "CODEX_HOME",
)

_GRANULAR_APPROVAL_POLICY: dict[str, Any] = {
    "granular": {
        "mcp_elicitations": True,
        "request_permissions": True,
        "rules": True,
        "sandbox_approval": True,
        "skill_approval": True,
    }
}

# Restrictive sandbox policies exec_wrapped will accept. danger-full-access is refused outright; the
# caller must pass one of these explicit restrictive shapes (never the user default).
_SAFE_SANDBOX_TYPES: frozenset[str] = frozenset({"read-only", "readOnly", "workspace-write", "workspaceWrite"})
_DANGER_SANDBOX_TYPES: frozenset[str] = frozenset({"danger-full-access", "dangerFullAccess"})

_VERSION_RE = re.compile(r"/(\d+)\.(\d+)\.(\d+)")


def _turn_result(
    status: str,
    *,
    vendor_turn_id: str | None = None,
    final_text: str | None = None,
    error_code: str | None = None,
    fatal: bool = False,
) -> Any:
    """Late import avoids a package-initialization cycle while returning the frozen H2.2 type."""
    from . import TurnResult

    return TurnResult(
        status=status,
        vendor_turn_id=vendor_turn_id,
        final_text=final_text,
        error_code=error_code,
        fatal=fatal,
    )


def parse_user_agent_version(user_agent: str | None) -> tuple[int, int, int] | None:
    """Parse the CLI semver out of the initialize result ``userAgent`` (e.g. ".../0.130.0 ..."). The
    floor gate is (this semver >= VERSION_FLOOR); an unparseable userAgent returns None -> observe-only."""
    if not user_agent:
        return None
    match = _VERSION_RE.search(user_agent)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def probe_method_surface(schema_json: Mapping[str, Any] | dict[str, Any]) -> set[str]:
    """Extract request method names from a generated schema or compact fixture.

    Codex 0.130 emits ``ClientRequest.json`` / ``ServerRequest.json`` as nested ``oneOf`` schemas whose
    request variants carry ``properties.method.enum``. Older tests use compact ``methods`` and
    ``requests`` containers. This walker accepts both without mistaking arbitrary ``name`` fields for
    RPC methods.
    """
    names: set[str] = set()

    def _harvest_fixture(entry: Any) -> None:
        if isinstance(entry, str):
            names.add(entry)
        elif isinstance(entry, Mapping):
            value = entry.get("method") or entry.get("name")
            if isinstance(value, str):
                names.add(value)

    def _walk(node: Any) -> None:
        if isinstance(node, Mapping):
            props = node.get("properties")
            if isinstance(props, Mapping):
                method_schema = props.get("method")
                if isinstance(method_schema, Mapping):
                    enum = method_schema.get("enum")
                    if isinstance(enum, list):
                        names.update(str(value) for value in enum if isinstance(value, str))
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    if isinstance(schema_json, Mapping):
        for container_key in ("methods", "clientRequests", "requests"):
            container = schema_json.get(container_key)
            if isinstance(container, list):
                for entry in container:
                    _harvest_fixture(entry)
            elif isinstance(container, Mapping):
                names.update(k for k in container.keys() if isinstance(k, str))
        _walk(schema_json)
        if not names:
            # Bare mapping of method-name -> spec (the last-resort shape).
            names.update(k for k in schema_json.keys() if isinstance(k, str) and "/" in k)
    return names


def _load_schema(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise CodexCapabilityError([f"malformed schema: {path.name} ({type(error).__name__})"]) from error
    if not isinstance(value, dict):
        raise CodexCapabilityError([f"malformed schema: {path.name} (root is not an object)"])
    return value


def _variant_properties(definition: Any, variant_type: str) -> set[str]:
    """Return property names for the ``SandboxPolicy`` variant with ``type == variant_type``."""
    if not isinstance(definition, Mapping):
        return set()
    for variant in definition.get("oneOf", []):
        if not isinstance(variant, Mapping):
            continue
        props = variant.get("properties")
        if not isinstance(props, Mapping):
            continue
        type_schema = props.get("type")
        enum = type_schema.get("enum") if isinstance(type_schema, Mapping) else None
        if isinstance(enum, list) and variant_type in enum:
            return {str(key) for key in props}
    return set()


def probe_schema_capabilities(bundle_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Inspect a generated app-server schema bundle without starting an authenticated Codex turn.

    The H2.3 boundary requires outside-workspace reads to be restrictable for Plan/Ask/Agent. A
    ``PreToolUse`` hook is deliberately not accepted as a substitute. Compatible schemas therefore
    need both ``readOnly.access`` and ``workspaceWrite.readOnlyAccess`` on ``SandboxPolicy`` in
    addition to the complete request surface and command-hook support.
    """
    root = Path(bundle_dir)
    client = _load_schema(root / "ClientRequest.json")
    server = _load_schema(root / "ServerRequest.json")
    turn_start = _load_schema(root / "v2" / "TurnStartParams.json")
    hooks_list = _load_schema(root / "v2" / "HooksListResponse.json")
    methods = probe_method_surface(client) | probe_method_surface(server)
    sandbox = (turn_start.get("definitions") or {}).get("SandboxPolicy")
    read_only = _variant_properties(sandbox, "readOnly")
    workspace_write = _variant_properties(sandbox, "workspaceWrite")
    bounded_reads = "access" in read_only and "readOnlyAccess" in workspace_write
    hook_defs = hooks_list.get("definitions") or {}
    event_enum = ((hook_defs.get("HookEventName") or {}).get("enum") or [])
    handler_enum = ((hook_defs.get("HookHandlerType") or {}).get("enum") or [])
    pre_tool_hook = "preToolUse" in event_enum and "command" in handler_enum
    missing = sorted(REQUIRED_METHODS - methods)
    return {
        "methods": methods,
        "missing_methods": missing,
        "bounded_read_access": bounded_reads,
        "pre_tool_command_hook": pre_tool_hook,
        "read_only_fields": sorted(read_only),
        "workspace_write_fields": sorted(workspace_write),
    }


def assert_required_methods(surface: set[str]) -> None:
    """Raise if the probed method surface is missing any REQUIRED_METHODS entry. The separable half of
    the version gate (parse_user_agent_version is the other): both must pass to orchestrate."""
    missing = REQUIRED_METHODS - set(surface)
    if missing:
        raise CodexCapabilityError(sorted(missing))


class CodexCapabilityError(Exception):
    """The app-server method surface is missing a required method -> orchestrate is refused."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"codex app-server missing required methods: {missing}")
        self.missing = missing


class CodexAdapterError(Exception):
    """A refusal the adapter raises on an orchestrate path (observe-only, leased thread, side channel).
    Carries a DENIED_* code + fields so the caller can surface a structured denial."""

    def __init__(self, code: str, **fields: Any) -> None:
        super().__init__(f"{code}: {fields}")
        self.code = code
        self.fields = fields

    def payload(self) -> dict[str, Any]:
        return {"status": "DENIED", "code": self.code, **self.fields}


def load_api_key_file(path: str | os.PathLike[str] | None) -> str:
    """Read one owner-managed API token without normalizing or exposing it.

    Contract: regular UTF-8 file, 1..16 KiB, exactly one non-empty token line. Only one final LF/CRLF
    is removed; all other whitespace/control characters are malformed. Every failure returns the same
    safe denial code and never includes file content.
    """
    if not path:
        raise CodexAdapterError(
            DENIED_CREDENTIAL_FILE,
            required_action=f"set {API_KEY_FILE_ENV} to an owner-protected UTF-8 token file",
        )
    source = Path(path)
    try:
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size < 1 or info.st_size > MAX_API_KEY_FILE_BYTES:
            raise ValueError("credential file must be regular and within the size limit")
        raw = source.read_bytes()
        text = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeError, ValueError) as error:
        raise CodexAdapterError(
            DENIED_CREDENTIAL_FILE,
            error_type=type(error).__name__,
            required_action=f"provide a readable regular UTF-8 token file at {API_KEY_FILE_ENV} (max 16 KiB)",
        ) from error
    if text.endswith("\r\n"):
        token = text[:-2]
    elif text.endswith("\n"):
        token = text[:-1]
    else:
        token = text
    if not token or any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in token):
        raise CodexAdapterError(
            DENIED_CREDENTIAL_FILE,
            required_action=f"{API_KEY_FILE_ENV} must contain exactly one non-empty token line",
        )
    return token


# --- JSON-RPC client over the child stdio ------------------------------------------------------

class _JsonRpcClient:
    """Newline-delimited JSON-RPC 2.0 over an OwnedChild's stdio. One reader thread drains child
    stdout: id-correlated results wake per-request waiters; notifications dispatch to a callback;
    server->client requests are answered by an answerer callback (unknown ones get a safe default).

    stdin/stdout are utf-8/errors=replace (children.spawn_owned already set that on the child), so a
    stray byte can never crash the reader. All logging goes to the injected logger, never stdout."""

    def __init__(
        self,
        child: children.OwnedChild,
        *,
        on_notification: Callable[[str, dict[str, Any]], None],
        on_server_request: Callable[[int | str, str, dict[str, Any]], dict[str, Any]],
        on_disconnect: Callable[[], None] | None = None,
        logger: Callable[[str], None],
        request_timeout: float = 30.0,
    ) -> None:
        self._child = child
        self._proc = child.process
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._on_disconnect = on_disconnect
        self._log = logger
        self._timeout = request_timeout
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, threading.Event] = {}
        self._results: dict[int, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name="codex-rpc-reader", daemon=True)

    def start(self) -> None:
        self._reader.start()

    def _read_loop(self) -> None:
        stdout = self._proc.stdout
        if stdout is None:
            return
        try:
            for line in stdout:
                if self._closed.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    self._log(f"rpc: dropped unparseable line ({len(line)} chars)")
                    continue
                if not isinstance(message, dict):
                    continue
                self._dispatch(message)
        except (ValueError, OSError):
            # Child stream closed/broke -> stop cleanly. Never raise past the reader thread.
            pass
        finally:
            # Wake every waiter so no request hangs after the child dies.
            with self._pending_lock:
                for event in self._pending.values():
                    event.set()
            if self._on_disconnect is not None:
                try:
                    self._on_disconnect()
                except Exception as error:  # noqa: BLE001 -- disconnect reporting cannot kill reader cleanup
                    self._log(f"rpc: disconnect callback error ({type(error).__name__})")

    def _dispatch(self, message: dict[str, Any]) -> None:
        has_method = "method" in message
        has_id = "id" in message and message["id"] is not None
        if has_method and has_id:
            # Server -> client REQUEST: answer by id (unknown methods get a safe default).
            method = message.get("method")
            params = message.get("params")
            if not isinstance(method, str) or not isinstance(params, Mapping):
                self._send({
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32600, "message": "malformed server request"},
                })
                return
            self._answer_server_request(message["id"], method, dict(params))
            return
        if has_method:
            # Notification.
            method = message.get("method")
            params = message.get("params")
            if not isinstance(method, str) or not isinstance(params, Mapping):
                self._log("rpc: dropped malformed notification")
                return
            try:
                self._on_notification(method, dict(params))
            except Exception as error:  # noqa: BLE001 -- a handler bug must not kill the reader
                self._log(f"rpc: notification handler error ({type(error).__name__})")
            return
        if has_id:
            # Response to one of our requests.
            self._deliver_result(message)

    def _answer_server_request(self, request_id: int | str, method: str, params: dict[str, Any]) -> None:
        try:
            result = self._on_server_request(request_id, method, params)
        except Exception as error:  # noqa: BLE001 -- never crash the reader over an answerer bug
            self._log(f"rpc: server-request answerer error for {method} ({type(error).__name__})")
            result = {"error": {"code": -32603, "message": "internal answerer error"}}
        self._send({"jsonrpc": "2.0", "id": request_id, **_as_response_body(result)})

    def _deliver_result(self, message: dict[str, Any]) -> None:
        req_id = message["id"]
        if not isinstance(req_id, int):
            return
        with self._pending_lock:
            event = self._pending.get(req_id)
            if event is None:
                return
            self._results[req_id] = message
            event.set()

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        """Send a client request, block for the id-correlated result. Raises TimeoutError on the
        per-request deadline and RuntimeError if the child died before answering."""
        with self._id_lock:
            req_id = self._next_id
            self._next_id += 1
        event = threading.Event()
        with self._pending_lock:
            self._pending[req_id] = event
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        if not event.wait(timeout if timeout is not None else self._timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(f"codex rpc request timed out: {method}")
        with self._pending_lock:
            self._pending.pop(req_id, None)
            message = self._results.pop(req_id, None)
        if message is None:
            raise RuntimeError(f"codex rpc child closed before answering: {method}")
        if "error" in message:
            raise RuntimeError(f"codex rpc error for {method}")
        return message.get("result") or {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a client notification (no id, no reply expected) -- e.g. ``initialized``."""
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        self._send(body)

    def _send(self, body: dict[str, Any]) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            return
        line = json.dumps(body) + "\n"
        with self._write_lock:
            try:
                stdin.write(line)
                stdin.flush()
            except (OSError, ValueError):
                self._log("rpc: write to child stdin failed (child gone)")

    def close(self) -> None:
        self._closed.set()


def _as_response_body(result: dict[str, Any]) -> dict[str, Any]:
    """A server-request answer is either a JSON-RPC ``result`` or ``error`` member. When the answerer
    returns a dict already shaped as {"error": ...} pass it through; otherwise wrap as ``result``."""
    if isinstance(result, dict) and "error" in result and set(result.keys()) == {"error"}:
        return {"error": result["error"]}
    return {"result": result}


# --- thread lease (single-controller arbitration) ----------------------------------------------

_LEASE_THREAD_LOCKS: dict[str, threading.Lock] = {}
_LEASE_THREAD_LOCKS_GUARD = threading.Lock()


def _lease_thread_lock(path: Path) -> threading.Lock:
    """Return the process-local half of the per-registry serialization lock."""

    key = os.path.normcase(str(path.resolve()))
    with _LEASE_THREAD_LOCKS_GUARD:
        return _LEASE_THREAD_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _lease_file_lock(path: Path):
    """Hold a blocking cross-process advisory lock for one lease-registry transaction."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()

class _LeaseRegistry:
    """Per-threadId lease file under the supervisor runtime dir: ``{threadId: {pid, nonce}}``. The
    app-server has NO thread lock (P-B 2c), so the ORCHESTRATOR owns arbitration. resume() with a lease
    held by a LIVE pid on a DIFFERENT nonce refuses (detect-and-refuse); a dead owner is reclaimed."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock_path = path.with_name(f"{path.name}.lock")
        self._thread_lock = _lease_thread_lock(path)

    @contextmanager
    def _transaction(self):
        with self._thread_lock:
            with _lease_file_lock(self._lock_path):
                yield

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    def _store(self, registry: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(registry), encoding="utf-8")

    def held_by_live_other(self, thread_id: str, nonce: str) -> dict[str, Any] | None:
        """Return the current lease iff a DIFFERENT live controller holds it, else None (free/dead/ours)."""
        with self._transaction():
            owner = self._load().get(thread_id)
            if owner is None:
                return None
            try:
                owner_pid = int(owner.get("pid", 0))
            except (TypeError, ValueError):
                owner_pid = 0
            owner_nonce = str(owner.get("nonce", ""))
            if owner_nonce == nonce:
                return None  # our own lease
            if children.pid_alive(owner_pid):
                return dict(owner)
            return None  # dead owner -> reclaimable

    def acquire(self, thread_id: str, pid: int, nonce: str) -> tuple[dict[str, Any] | None, bool]:
        """Atomically refuse a live foreign owner or store this owner; report dead-owner reclamation."""

        with self._transaction():
            registry = self._load()
            owner = registry.get(thread_id)
            owner_nonce = str(owner.get("nonce", "")) if isinstance(owner, Mapping) else ""
            try:
                owner_pid = int(owner.get("pid", 0)) if isinstance(owner, Mapping) else 0
            except (TypeError, ValueError):
                owner_pid = 0
            if owner is not None and owner_nonce != nonce and children.pid_alive(owner_pid):
                return dict(owner), False
            reclaimed = owner is not None and owner_nonce != nonce
            registry[thread_id] = {"pid": pid, "nonce": nonce}
            self._store(registry)
            return None, reclaimed

    def release(self, thread_id: str, nonce: str) -> None:
        with self._transaction():
            registry = self._load()
            owner = registry.get(thread_id)
            if owner is not None and str(owner.get("nonce", "")) == nonce:
                registry.pop(thread_id, None)
                self._store(registry)


# --- adapter -----------------------------------------------------------------------------------

class CodexAdapter:
    """``codex app-server`` as a governed EngineAdapter (§A.1). Spawns the OWNED child, runs the
    handshake + version gate, drives thread/turn/steer/interrupt, and intercepts every approval through
    the M3 policy chokepoint per item. Below the version floor (or unparseable) it enters OBSERVE-ONLY:
    the observe APIs still work, but every orchestrate verb refuses with DENIED_CODEX_VERSION.

    Collaborators are injected: ``spawner`` (default children.spawn_owned -- the app-server child is
    OWNED), ``policy_engine`` (M3 PolicyEngine), ``recorder`` (callable(event_dict) -- tests capture
    in-memory; NO hard-wired DB writes), ``logger`` (default stderr; stdout stays pristine), ``clock``,
    ``env`` extras, and ``codex_home`` (per-agent CODEX_HOME isolation per P-B)."""

    def __init__(
        self,
        policy_engine: policy.PolicyEngine,
        *,
        cmd: list[str] | None = None,
        spawner: Callable[..., children.OwnedChild] | None = None,
        recorder: Callable[[dict[str, Any]], None] | None = None,
        logger: Callable[[str], None] | None = None,
        clock: Callable[[], float] | None = None,
        env: Mapping[str, str] | None = None,
        codex_home: str | None = None,
        lease_path: Path | None = None,
        engine_name: str = "codex",
        client_name: str = "agent-kaizen",
        client_version: str = "0.0.0",
        request_timeout: float = 30.0,
        approval_timeout: float = 30.0,
        turn_timeout: float = 300.0,
        hook_script: str | os.PathLike[str] | None = None,
        schema_capabilities: Mapping[str, Any] | None = None,
    ) -> None:
        self.policy_engine = policy_engine
        self._cmd = cmd or ["codex", "app-server"]
        self._spawner = spawner or children.spawn_owned
        self._recorder = recorder or (lambda _event: None)
        self._log = logger or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self._clock = clock or __import__("time").monotonic
        self._env_extra = dict(env or {})
        self._codex_home = codex_home
        self._engine = engine_name
        self._client_name = client_name
        self._client_version = client_version
        self._request_timeout = request_timeout
        self._approval_timeout = approval_timeout
        self._turn_timeout = turn_timeout
        self._mutation_guard: MutationGuard | None = None
        self._lease = _LeaseRegistry(lease_path or (RUNTIME_DIR / "codex-leases.json"))
        repo_root = Path(__file__).resolve().parents[3]
        orchestration_root = repo_root / "kaizen_components" / "orchestration"
        self._hook_script = Path(hook_script) if hook_script else orchestration_root / "codex_hook_shim.py"
        self._scrubber_script = orchestration_root / "vendor_env_scrub.py"
        self._schema_capabilities = dict(schema_capabilities) if schema_capabilities is not None else None
        self._runtime_dir: Path | None = None
        self._runtime_home: Path | None = None
        self._runtime_temp: Path | None = None

        self._child: children.OwnedChild | None = None
        self._rpc: _JsonRpcClient | None = None
        self._nonce = __import__("secrets").token_hex(8)
        self._hook_gate_id = __import__("secrets").token_hex(24)
        self._closing = False
        self._disconnected = False

        # Version gate state.
        self.user_agent: str | None = None
        self.server_version: tuple[int, int, int] | None = None
        self.observe_only = False

        # Session/thread/turn state. Span bookkeeping is touched by BOTH the client thread (start_turn/
        # interrupt/kill) and the reader thread (turn/started, item/*, turn/completed notifications), so
        # a lock guards it and open/close emission is IDEMPOTENT (dedupe via the *_opened / *_closed
        # sets) -- the wire ordering vs the request-result return is a race, and either side may emit an
        # open/close first.
        self.session_id: str | None = None       # bound C1 session id (for record_ask), if any
        self.thread_id: str | None = None
        self.vendor_session_root_id: str | None = None
        self.current_epoch = 0
        self._span_lock = threading.RLock()
        self._active_turn: str | None = None
        self._turn_opened: set[str] = set()       # turn ids that already emitted `turn open`
        self._turn_closed: set[str] = set()       # turn ids that already emitted a terminal marker
        self._open_items: set[str] = set()        # item ids with an open (unclosed) child span
        self._item_details: dict[str, dict[str, Any]] = {}
        self._leased_thread: str | None = None

        # H2.3 logical-conversation state. One connection + thread remains alive until close/kill.
        # Terminal notifications can arrive before turn/start's result returns, so rendezvous state is
        # keyed by vendor turn id and retained until run_turn consumes it.
        self._turn_condition = threading.Condition(self._span_lock)
        self._turn_terminal: dict[str, dict[str, Any]] = {}
        self._turn_final_text: dict[str, str] = {}
        self._profile: dict[str, Any] = {}
        self._effective_profile: dict[str, Any] = {}
        self._policy_snapshot: policy.PolicySnapshot | None = None
        self._runtime_models: list[dict[str, Any]] = []
        self._runtime_config: dict[str, Any] = {}
        self._boundary: dict[str, Any] = {}
        self._opened = False

        # Callbacks (on_event / on_approval).
        self._event_cb: Callable[[dict[str, Any]], None] | None = None
        self._approval_cb: Callable[[dict[str, Any]], str] | None = None
        self._approval_broker_cb: ApprovalBrokerCallback | None = None

    # --- capabilities --------------------------------------------------------------------------

    @property
    def sandbox_mode(self) -> str:
        """On Windows there is no OS sandbox for Codex (§A.1) -> effectively-unsandboxed; the supervisor
        is the only real gate. Elsewhere the vendor sandbox is nominally enforced."""
        return "effectively-unsandboxed" if os.name == "nt" else "vendor-sandboxed"

    @property
    def active_turn_id(self) -> str | None:
        with self._span_lock:
            return self._active_turn

    @property
    def hook_registration(self) -> dict[str, str]:
        """Opaque registration material the supervisor binds to this session's immutable snapshot.

        The gate id is not an approval secret: the authenticated daemon token remains the transport
        credential. It prevents one driven session from asking the daemon to evaluate against another
        session's snapshot.
        """
        return {
            "gate_id": self._hook_gate_id,
            "profile_hash": self._policy_snapshot.profile_hash if self._policy_snapshot else "",
        }

    @property
    def effective_profile(self) -> dict[str, Any]:
        return dict(self._effective_profile)

    def capabilities(self) -> dict[str, Any]:
        """Static capability report. ``sandbox_mode`` carries the Windows-unsandboxed reality;
        ``orchestrate`` flips false once the version gate has put the adapter in observe-only."""
        return {
            "engine": self._engine,
            "session": True,
            "turn": True,
            "steer": True,
            "approval": True,
            "subagent": True,
            "resume": True,
            "sandbox_mode": self.sandbox_mode,
            "orchestrate": not self.observe_only,
            "version_floor": ".".join(str(x) for x in VERSION_FLOOR),
            "server_version": ".".join(str(x) for x in self.server_version) if self.server_version else None,
            "models": [dict(model) for model in self._runtime_models],
            "permission_modes": list(SUPPORTED_PERMISSION_MODES),
            "auth_modes": list(SUPPORTED_AUTH_MODES),
        }

    # --- lifecycle: spawn + handshake + version gate -------------------------------------------

    def _create_private_runtime(self, cwd: str | None) -> None:
        """Create one bounded off-system-drive workspace runtime for CODEX_HOME and temp variables."""
        if self._runtime_dir is not None:
            return
        workspace = Path(cwd or Path.cwd()).resolve()
        if _is_system_drive(workspace):
            raise CodexAdapterError(
                DENIED_POLICY_GATE_UNAVAILABLE,
                required_action="Codex driven runtime must be inside an off-system-drive workspace",
            )
        default_parent = workspace / "AI" / "work" / "orchestration" / "runtime" / "codex-driving"
        parent = Path(self._codex_home).resolve() if self._codex_home else default_parent
        try:
            parent.relative_to(workspace)
        except ValueError as error:
            raise CodexAdapterError(
                DENIED_POLICY_GATE_UNAVAILABLE,
                required_action="CODEX_HOME runtime base must remain inside the active workspace",
            ) from error
        runtime = parent / __import__("uuid").uuid4().hex
        home = runtime / "codex-home"
        temp = runtime / "tmp"
        home.mkdir(parents=True, exist_ok=False)
        temp.mkdir(parents=True, exist_ok=False)
        self._runtime_dir = runtime
        self._runtime_home = home
        self._runtime_temp = temp

    def _stop_runtime(self) -> list[str]:
        """Remove only the verified bounded runtime; return non-secret cleanup error codes."""
        runtime = self._runtime_dir
        if runtime is None:
            return []
        workspace = Path(self._policy_snapshot.workspace_root if self._policy_snapshot else Path.cwd()).resolve()
        expected = workspace / "AI" / "work" / "orchestration" / "runtime" / "codex-driving"
        if self._codex_home:
            expected = Path(self._codex_home).resolve()
        try:
            resolved = runtime.resolve()
            resolved.relative_to(expected.resolve())
        except (OSError, ValueError):
            return ["runtime_outside_boundary"]
        try:
            shutil.rmtree(resolved)
        except OSError as error:
            self._log(f"codex runtime cleanup failed ({type(error).__name__})")
            return ["runtime_remove_failed"]
        if resolved.exists():
            return ["runtime_still_present"]
        self._runtime_dir = None
        self._runtime_home = None
        self._runtime_temp = None
        return []

    def _compose_child_env(self, profile: Mapping[str, Any] | None) -> dict[str, str]:
        """Build the one child environment immediately before spawn.

        Both vendor keys are always removed first. Subscription mode relies only on Codex's installed
        login; API-key mode reads the owner file at this last possible point and injects OPENAI_API_KEY
        into this child only. The token is never retained on the adapter.
        """
        child_env = children.compose_child_env()
        child_env.update(self._env_extra)
        credential_path = self._env_extra.get(API_KEY_FILE_ENV) or os.environ.get(API_KEY_FILE_ENV)
        for key in _SENSITIVE_ENV:
            child_env.pop(key, None)
        auth_mode = str((profile or {}).get("auth_mode") or "")
        if auth_mode == "api-key":
            child_env["OPENAI_API_KEY"] = load_api_key_file(credential_path)
        if self._runtime_home is None or self._runtime_temp is None:
            raise CodexAdapterError(
                DENIED_POLICY_GATE_UNAVAILABLE,
                required_action="create the private Codex runtime before spawning app-server",
            )
        if auth_mode == "api-key":
            # api-key: fully private sealed CODEX_HOME (no ambient login reachable).
            child_env["CODEX_HOME"] = str(self._runtime_home)
        else:
            # subscription: installed Codex login. Restore ambient identity behind any vendor-runtime
            # masks; the thread/turn sandbox+approval config stays the boundary regardless of CODEX_HOME.
            for key in _SUBSCRIPTION_IDENTITY_ENV:
                child_env.pop(key, None)
                ambient = os.environ.get(key)
                if ambient:
                    child_env[key] = ambient
        for name in ("TEMP", "TMP", "TMPDIR"):
            child_env[name] = str(self._runtime_temp)
        return child_env

    def start_session(self, cwd: str | None = None, profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Spawn the OWNED app-server child, run initialize/initialized, and enforce the version floor.

        Returns a session-ref dict (userAgent, server_version, observe_only). Below the floor or an
        unparseable userAgent => observe-only (recorded), and every orchestrate verb subsequently
        refuses; the observe APIs still function. Emits a ``session open`` recorder event carrying the
        Windows-unsandboxed sandbox_mode (§A.1)."""
        self._create_private_runtime(cwd)
        child_env = self._compose_child_env(profile)
        self._closing = False
        self._disconnected = False
        # Resolve the launcher through PATH/PATHEXT: on Windows the npm `codex` shim is a .cmd, which
        # CreateProcess only finds via the which-resolved full path (bare "codex" => WinError 2).
        cmd = [shutil.which(self._cmd[0]) or self._cmd[0], *self._cmd[1:]]
        self._child = self._spawner(
            cmd, cwd=cwd, env=child_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._rpc = _JsonRpcClient(
            self._child,
            on_notification=self._handle_notification,
            on_server_request=self._handle_server_request,
            on_disconnect=self._handle_disconnect,
            logger=self._log,
            request_timeout=self._request_timeout,
        )
        self._rpc.start()
        # NEVER optional: codex logs verbosely to child stderr (a line per skill/session event). An
        # undrained stderr pipe fills (~64KB) and wedges the child MID-REQUEST -- live thread/start
        # hung exactly this way in the M4 live leg (the fake writes too little stderr to catch it).
        threading.Thread(target=self._drain_stderr, name="codex-stderr", daemon=True).start()

        result = self._rpc.request(
            "initialize",
            {
                "clientInfo": {"name": self._client_name, "version": self._client_version},
                "capabilities": {"experimentalApi": True},
            },
        )
        self.user_agent = result.get("userAgent")
        self.server_version = parse_user_agent_version(self.user_agent)
        # Complete the handshake regardless (so observe-mode reads still work over a live channel).
        self._rpc.notify("initialized")

        if self.server_version is None or self.server_version < VERSION_FLOOR:
            self.observe_only = True
            self._log(
                f"codex version gate: userAgent={self.user_agent!r} parsed={self.server_version} "
                f"floor={VERSION_FLOOR} -> OBSERVE-ONLY"
            )

        self._emit(
            "session", "open",
            correlation_id=self.thread_id or "session",
            summary="codex app-server session started",
            payload={
                "engine": self._engine,
                "user_agent": self.user_agent,
                "server_version": list(self.server_version) if self.server_version else None,
                "observe_only": self.observe_only,
                "sandbox_mode": self.sandbox_mode,
                "codex_home_isolated": self._runtime_home is not None,
                "temp_isolated": self._runtime_temp is not None,
            },
        )
        return {
            "status": "OK",
            "user_agent": self.user_agent,
            "server_version": list(self.server_version) if self.server_version else None,
            "observe_only": self.observe_only,
            "sandbox_mode": self.sandbox_mode,
        }

    def _drain_stderr(self) -> None:
        """Drain child stderr without forwarding vendor-controlled text or credentials."""
        child = self._child
        if child is None or child.process.stderr is None:
            return
        count = 0
        try:
            for line in child.process.stderr:
                if line:
                    count += 1
        except (ValueError, OSError):
            pass
        if count:
            self._log(f"codex stderr suppressed ({count} lines)")

    def bind_session(self, session_id: str | None) -> None:
        """Bind a C1 ``agent_sessions`` id so ``ask`` decisions persist via the engine's record_ask.
        An unbound adapter cannot surface interactive asks; C4 persistence is mandatory before UI open."""
        self.session_id = session_id

    def _require_orchestrate(self, verb: str) -> None:
        if self.observe_only:
            raise CodexAdapterError(
                DENIED_CODEX_VERSION,
                verb=verb,
                user_agent=self.user_agent,
                server_version=list(self.server_version) if self.server_version else None,
                floor=list(VERSION_FLOOR),
                required_action="upgrade codex-cli to >= 0.130.0; adapter is observe-only",
            )

    # --- H2.3 common adapter contract ----------------------------------------------------------

    def _assert_schema_safety(self, capabilities: Mapping[str, Any]) -> None:
        missing = list(capabilities.get("missing_methods") or [])
        if missing:
            raise CodexAdapterError(
                DENIED_POLICY_GATE_UNAVAILABLE,
                missing_methods=sorted(str(method) for method in missing),
                required_action="upgrade Codex to an app-server build exposing the complete H2.3 method surface",
            )
        if not capabilities.get("pre_tool_command_hook"):
            raise CodexAdapterError(
                DENIED_POLICY_GATE_UNAVAILABLE,
                required_action="upgrade Codex to a build with session-flag PreToolUse command hooks",
            )
        if not capabilities.get("bounded_read_access"):
            raise CodexAdapterError(
                DENIED_POLICY_GATE_UNAVAILABLE,
                read_only_fields=list(capabilities.get("read_only_fields") or []),
                workspace_write_fields=list(capabilities.get("workspace_write_fields") or []),
                required_action=(
                    "upgrade Codex to a build whose SandboxPolicy supports bounded outside-workspace reads "
                    "(readOnly.access + workspaceWrite.readOnlyAccess); PreToolUse is defense-in-depth only"
                ),
            )

    def _probe_runtime_catalog(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self._rpc is None:
            raise RuntimeError("codex app-server is not connected")
        model_result = self._rpc.request("model/list", {"includeHidden": True})
        config_result = self._rpc.request("config/read", {"includeLayers": False})
        if os.name == "nt":
            readiness = self._rpc.request("windowsSandbox/readiness", {})
            if readiness.get("status") != "ready":
                raise CodexAdapterError(
                    DENIED_POLICY_GATE_UNAVAILABLE,
                    sandbox_readiness=readiness.get("status"),
                    required_action="configure/update the Codex Windows sandbox before driving a session",
                )
        raw_models = model_result.get("data") if isinstance(model_result, Mapping) else None
        models: list[dict[str, Any]] = []
        for entry in raw_models if isinstance(raw_models, list) else []:
            if not isinstance(entry, Mapping):
                continue
            model_id = entry.get("id") or entry.get("model")
            if not isinstance(model_id, str) or not model_id:
                continue
            efforts: list[str] = []
            for option in entry.get("supportedReasoningEfforts") or []:
                if isinstance(option, Mapping) and isinstance(option.get("reasoningEffort"), str):
                    efforts.append(str(option["reasoningEffort"]))
                elif isinstance(option, str):
                    efforts.append(option)
            models.append({
                "id": model_id,
                "label": str(entry.get("displayName") or model_id),
                "reasoning_efforts": efforts,
                "default_effort": entry.get("defaultReasoningEffort"),
                "is_default": bool(entry.get("isDefault")),
            })
        config = config_result.get("config") if isinstance(config_result, Mapping) else {}
        return models, dict(config) if isinstance(config, Mapping) else {}

    def _boundary_for_mode(self, permission_mode: str) -> dict[str, Any]:
        if permission_mode not in SUPPORTED_PERMISSION_MODES:
            raise CodexAdapterError(
                DENIED_PROFILE_UNSUPPORTED,
                field="permission_mode",
                permission_mode=permission_mode,
                supported=list(SUPPORTED_PERMISSION_MODES),
                required_action=(
                    "Codex Full is unavailable because danger-full-access cannot preserve the invariant floor; "
                    "choose plan, ask, or agent"
                ),
            )
        snapshot = self._policy_snapshot
        workspace = snapshot.workspace_root if snapshot else None
        readable_roots = [workspace] if workspace else []
        read_access = {
            "type": "restricted",
            "includePlatformDefaults": True,
            "readableRoots": readable_roots,
        }
        if permission_mode in ("plan", "ask"):
            return {
                "thread_sandbox": "read-only",
                "turn_sandbox": {"type": "readOnly", "access": read_access, "networkAccess": False},
                "approval_policy": json.loads(json.dumps(_GRANULAR_APPROVAL_POLICY)),
            }
        writable = []
        for root in ([workspace] if workspace else []) + list(snapshot.designated_write_roots if snapshot else ()):
            if root and root not in writable:
                writable.append(root)
        return {
            "thread_sandbox": "workspace-write",
            "turn_sandbox": {
                "type": "workspaceWrite",
                "writableRoots": writable,
                "readOnlyAccess": read_access,
                "networkAccess": False,
                "excludeSlashTmp": True,
                "excludeTmpdirEnvVar": True,
            },
            "approval_policy": json.loads(json.dumps(_GRANULAR_APPROVAL_POLICY)),
        }

    def _policy_hook_config(self) -> dict[str, Any]:
        if not self._hook_script.is_file():
            raise CodexAdapterError(
                DENIED_POLICY_GATE_UNAVAILABLE,
                required_action="restore kaizen_components/orchestration/codex_hook_shim.py before driving Codex",
            )
        if not self._scrubber_script.is_file():
            raise CodexAdapterError(
                DENIED_POLICY_GATE_UNAVAILABLE,
                required_action="restore kaizen_components/orchestration/vendor_env_scrub.py before driving Codex",
            )
        profile_hash = self._policy_snapshot.profile_hash if self._policy_snapshot else ""
        argv = [
            sys.executable,
            str(self._scrubber_script.resolve()),
            "--",
            sys.executable,
            str(self._hook_script.resolve()),
            "--gate-id",
            self._hook_gate_id,
            "--profile-hash",
            profile_hash,
        ]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        return {
            "hooks": {
                "PreToolUse": [{
                    "matcher": ".*",
                    "hooks": [{
                        "type": "command",
                        "command": command,
                        "timeout": 10,
                        "statusMessage": "Checking Kaizen policy",
                    }],
                }],
            },
        }

    def _verify_policy_hook(self, cwd: str | None) -> None:
        if self._rpc is None:
            raise RuntimeError("codex app-server is not connected")
        result = self._rpc.request("hooks/list", {"cwds": [cwd] if cwd else []})
        found = False
        for entry in result.get("data", []) if isinstance(result, Mapping) else []:
            if not isinstance(entry, Mapping):
                continue
            if entry.get("errors"):
                continue
            for hook in entry.get("hooks", []):
                if not isinstance(hook, Mapping):
                    continue
                if (hook.get("eventName") == "preToolUse" and hook.get("handlerType") == "command"
                        and hook.get("source") == "sessionFlags" and hook.get("enabled") is True):
                    found = True
                    break
        if not found:
            raise CodexAdapterError(
                DENIED_POLICY_GATE_UNAVAILABLE,
                required_action="Codex did not load the session-scoped PreToolUse hook from thread config",
            )

    def open(self, profile: Mapping[str, Any], policy_snapshot: policy.PolicySnapshot) -> dict[str, Any]:
        """Open one persistent app-server connection and one thread without submitting a prompt."""
        if self._opened:
            return {"status": "OK", "thread_id": self.thread_id, "profile": self.effective_profile}
        if not isinstance(profile, Mapping):
            raise CodexAdapterError(DENIED_PROFILE_UNSUPPORTED, required_action="profile must be an object")
        permission_mode = str(profile.get("permission_mode") or "plan")
        auth_mode = str(profile.get("auth_mode") or "subscription")
        if auth_mode not in SUPPORTED_AUTH_MODES:
            raise CodexAdapterError(
                DENIED_PROFILE_UNSUPPORTED,
                field="auth_mode",
                auth_mode=auth_mode,
                supported=list(SUPPORTED_AUTH_MODES),
                required_action="Codex auth_mode must be subscription or api-key",
            )
        if not isinstance(policy_snapshot, policy.PolicySnapshot) or policy_snapshot.engine != self._engine:
            raise CodexAdapterError(
                DENIED_PROFILE_UNSUPPORTED,
                field="policy_snapshot",
                required_action="open Codex with its immutable codex PolicySnapshot",
            )
        self._policy_snapshot = policy_snapshot
        self.policy_engine = policy_snapshot.build_engine()
        self._profile = {
            "model": profile.get("model"),
            "reasoning_effort": profile.get("reasoning_effort"),
            "permission_mode": permission_mode,
            "auth_mode": auth_mode,
        }
        self._boundary = self._boundary_for_mode(permission_mode)
        try:
            schema_caps = self._schema_capabilities
            if schema_caps is None:
                schema_caps = schema_capabilities_from_installed(self._cmd, runtime_dir=RUNTIME_DIR)
                self._schema_capabilities = dict(schema_caps)
            self._assert_schema_safety(schema_caps)
            self.start_session(cwd=policy_snapshot.workspace_root, profile=self._profile)
            self._require_orchestrate("open")
            self._runtime_models, self._runtime_config = self._probe_runtime_catalog()

            requested_model = self._profile.get("model")
            requested_effort = self._profile.get("reasoning_effort")
            selected = next((m for m in self._runtime_models if m["id"] == requested_model), None)
            if requested_model is not None and selected is None:
                raise CodexAdapterError(
                    DENIED_PROFILE_UNSUPPORTED,
                    field="model",
                    model=requested_model,
                    supported=[entry["id"] for entry in self._runtime_models],
                    required_action="choose a model advertised by model/list",
                )
            if selected is not None and requested_effort is not None and requested_effort not in selected["reasoning_efforts"]:
                raise CodexAdapterError(
                    DENIED_PROFILE_UNSUPPORTED,
                    field="reasoning_effort",
                    model=requested_model,
                    supported=selected["reasoning_efforts"],
                    required_action="choose a reasoning effort advertised by model/list",
                )
            config = self._policy_hook_config()
            if requested_effort is not None:
                config["model_reasoning_effort"] = requested_effort
            thread = self.start_thread(
                cwd=policy_snapshot.workspace_root,
                approval_policy=self._boundary["approval_policy"],
                sandbox=self._boundary["thread_sandbox"],
                model=requested_model if isinstance(requested_model, str) else None,
                config=config,
                ephemeral=True,
            )
            effective_model = thread.get("model")
            effective_effort = thread.get("reasoning_effort")
            if requested_model is not None and effective_model != requested_model:
                raise CodexAdapterError(
                    DENIED_PROFILE_MISMATCH,
                    field="model",
                    requested=requested_model,
                    effective=effective_model,
                    required_action="start a new conversation with the vendor-effective model",
                )
            if requested_effort is not None and effective_effort != requested_effort:
                raise CodexAdapterError(
                    DENIED_PROFILE_MISMATCH,
                    field="reasoning_effort",
                    requested=requested_effort,
                    effective=effective_effort,
                    required_action="start a new conversation with the vendor-effective reasoning effort",
                )
            self._verify_policy_hook(policy_snapshot.workspace_root)
            self._effective_profile = {
                "model": effective_model,
                "reasoning_effort": effective_effort,
                "permission_mode": permission_mode,
                "auth_mode": auth_mode,
                "requested_model": requested_model,
                "requested_reasoning_effort": requested_effort,
            }
            self._opened = True
            return {
                "status": "OK",
                "thread_id": self.thread_id,
                "profile": self.effective_profile,
                "hook_registration": self.hook_registration,
            }
        except CodexAdapterError:
            self.kill()
            raise
        except Exception as error:  # noqa: BLE001 -- startup failures are safe structured denials
            self.kill()
            raise CodexAdapterError(
                DENIED_POLICY_GATE_UNAVAILABLE,
                error_type=type(error).__name__,
                required_action="repair the Codex app-server capability/safety handshake before retrying",
            ) from error

    def run_turn(self, prompt: str) -> Any:
        """Submit one turn and block until its matching terminal notification arrives."""
        if not self._opened or self._rpc is None or self.thread_id is None:
            return _turn_result("FAILED", error_code=CODEX_PROTOCOL_ERROR, fatal=True)
        if self.active_turn_id is not None:
            return _turn_result("FAILED", error_code="DENIED_TURN_IN_PROGRESS", fatal=False)
        overrides = {
            "model": self._effective_profile.get("model"),
            "effort": self._effective_profile.get("reasoning_effort"),
            "approvalPolicy": self._boundary.get("approval_policy"),
            "sandboxPolicy": self._boundary.get("turn_sandbox"),
        }
        try:
            started = self.start_turn(prompt, overrides=overrides)
            turn_id = started.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                self.kill()
                return _turn_result("FAILED", error_code=CODEX_PROTOCOL_ERROR, fatal=True)
        except Exception as error:  # noqa: BLE001 -- normalize transport rejection into TurnResult
            fatal = self._child is None or self._child.poll() is not None
            return _turn_result("FAILED", error_code=type(error).__name__, fatal=fatal)

        deadline = self._clock() + self._turn_timeout
        with self._turn_condition:
            while turn_id not in self._turn_terminal and not self._disconnected:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._turn_condition.wait(remaining)
            terminal = self._turn_terminal.pop(turn_id, None)
            final_text = self._turn_final_text.pop(turn_id, None)
            disconnected = self._disconnected
        if terminal is None:
            self.kill()
            return _turn_result(
                "FAILED",
                vendor_turn_id=turn_id,
                error_code=CODEX_CHILD_EXITED if disconnected else CODEX_TURN_TIMEOUT,
                fatal=True,
            )
        status = terminal.get("status")
        if status == "interrupted":
            return _turn_result("CANCELED", vendor_turn_id=turn_id, fatal=False)
        if status != "completed":
            error = terminal.get("error")
            code = error.get("codexErrorInfo") if isinstance(error, Mapping) else None
            return _turn_result(
                "FAILED",
                vendor_turn_id=turn_id,
                error_code=str(code or "CODEX_TURN_FAILED"),
                fatal=False,
            )
        if not isinstance(final_text, str):
            self.kill()
            return _turn_result(
                "FAILED",
                vendor_turn_id=turn_id,
                error_code=CODEX_FINAL_MESSAGE_MISSING,
                fatal=True,
            )
        return _turn_result("OK", vendor_turn_id=turn_id, final_text=final_text, fatal=False)

    # --- thread / turn / steer / interrupt -----------------------------------------------------

    def start_thread(
        self,
        *,
        cwd: str | None = None,
        approval_policy: Any = "untrusted",
        sandbox: str = "read-only",
        model: str | None = None,
        config: Mapping[str, Any] | None = None,
        ephemeral: bool = True,
    ) -> dict[str, Any]:
        """thread/start -> a controlled thread. Default ``ephemeral=True`` (opt into persistence
        explicitly) so a default thread leaves no shared rollout for a second controller to corrupt.
        ``config`` is the ONLY sanctioned per-spawn override (never ~/.codex/config.toml). Records a
        ``session open`` ledger event with vendor_thread_id + vendor_session_root_id (forks keep root)."""
        self._require_orchestrate("thread/start")
        params: dict[str, Any] = {
            "cwd": cwd,
            "approvalPolicy": approval_policy,
            "sandbox": sandbox,
            "ephemeral": ephemeral,
        }
        if model:
            params["model"] = model
        if config:
            params["config"] = dict(config)
        result = self._rpc.request("thread/start", {k: v for k, v in params.items() if v is not None})
        thread = result.get("thread") or {}
        self.thread_id = thread.get("id")
        self.vendor_session_root_id = thread.get("sessionId") or self.thread_id
        persisted = not bool(thread.get("ephemeral", ephemeral))
        # Take the single-controller lease for this thread (detect-and-refuse guards resume()).
        if self.thread_id:
            owner, _reclaimed = self._lease.acquire(self.thread_id, os.getpid(), self._nonce)
            if owner is not None:
                raise CodexAdapterError(
                    DENIED_THREAD_LEASED,
                    thread_id=self.thread_id,
                    owner_pid=owner.get("pid"),
                    required_action="another live controller owns this thread",
                )
            self._leased_thread = self.thread_id
        self._emit(
            "session", "open",
            correlation_id=self.thread_id or "thread",
            summary="codex thread started",
            payload={
                "vendor_thread_id": self.thread_id,
                "vendor_session_root_id": self.vendor_session_root_id,
                "persisted": persisted,
                "ephemeral": bool(thread.get("ephemeral", ephemeral)),
                "path": thread.get("path"),
            },
        )
        return {
            "status": "OK",
            "thread_id": self.thread_id,
            "vendor_session_root_id": self.vendor_session_root_id,
            "persisted": persisted,
            "model": result.get("model"),
            "reasoning_effort": result.get("reasoningEffort"),
            "approval_policy": result.get("approvalPolicy"),
            "sandbox": result.get("sandbox"),
        }

    def _open_turn(self, turn_id: str) -> None:
        """Emit ``turn open`` exactly once for ``turn_id`` (idempotent across the start_turn result and
        the turn/started notification, whichever wins the race)."""
        with self._span_lock:
            # A fast fake/vendor may complete the turn before turn/start's response wakes the caller.
            # Never resurrect that already-terminal turn when start_turn processes the late response.
            if turn_id in self._turn_closed:
                return
            self._active_turn = turn_id
            if turn_id in self._turn_opened:
                return
            self._turn_opened.add(turn_id)
        self._emit("turn", "open", correlation_id=turn_id, summary="codex turn started",
                   payload={"turn_id": turn_id, "thread_id": self.thread_id})

    def _close_turn(self, turn_id: str, marker: str, *, status: str | None) -> None:
        """Emit a terminal turn marker exactly once for ``turn_id`` (idempotent). Also clears
        ``_active_turn`` when it was this turn."""
        with self._span_lock:
            if turn_id in self._turn_closed:
                return
            self._turn_closed.add(turn_id)
            if self._active_turn == turn_id:
                self._active_turn = None
        self._emit("turn", marker, correlation_id=turn_id, summary=f"codex turn {status or marker}",
                   payload={"turn_id": turn_id, "status": status})

    def start_turn(self, prompt: str, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """turn/start {threadId, input:[{type:text,text:prompt}], ...overrides} -> a turn span. Records
        a ``turn open`` ledger event keyed by the turn id (idempotent: the turn/started notification may
        emit it first over the reader thread)."""
        self._require_orchestrate("turn/start")
        if self.thread_id is None:
            raise CodexAdapterError("DENIED_NO_THREAD", required_action="start_thread before start_turn")
        params: dict[str, Any] = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": neutralize_at_refs(prompt)}],
        }
        for key in ("model", "cwd", "sandboxPolicy", "approvalPolicy", "effort"):
            if overrides and overrides.get(key) is not None:
                params[key] = overrides[key]
        result = self._rpc.request("turn/start", params)
        turn = result.get("turn") or {}
        turn_id = turn.get("id")
        if turn_id:
            self._open_turn(turn_id)
        return {"status": "OK", "turn_id": turn_id}

    def steer(self, *args: str, model: str | None = None, cwd: str | None = None,
              sandbox: str | None = None) -> dict[str, Any]:
        """turn/steer is APPEND-ONLY and needs ``expectedTurnId`` (P-B / §A.1). A model/cwd/sandbox
        CHANGE cannot be steered -- it REQUIRES a fresh turn/start; enforced here in code (REFUSE steer
        with DENIED_STEER_REQUIRES_NEW_TURN, never silently sent)."""
        self._require_orchestrate("turn/steer")
        if len(args) == 1:
            turn, msg = self.active_turn_id, args[0]
        elif len(args) == 2:
            turn, msg = args
        else:
            raise TypeError("steer expects text or turn_id, text")
        if not turn:
            raise CodexAdapterError("DENIED_NO_ACTIVE_TURN", required_action="steer while a turn is active")
        if model is not None or cwd is not None or sandbox is not None:
            raise CodexAdapterError(
                "DENIED_STEER_REQUIRES_NEW_TURN",
                changed=[k for k, v in (("model", model), ("cwd", cwd), ("sandbox", sandbox)) if v is not None],
                required_action="model/cwd/sandbox changes need a new turn/start, not steer",
            )
        result = self._rpc.request(
            "turn/steer",
            {"threadId": self.thread_id, "expectedTurnId": turn,
             "input": [{"type": "text", "text": neutralize_at_refs(msg)}]},
        )
        return {"status": "OK", "turn_id": turn, "result": result}

    def interrupt(self, turn: str | None = None) -> dict[str, Any]:
        """turn/interrupt {threadId, turnId} -> ``{}`` ack, then turn/completed(interrupted). Per the
        P-B caveat the in-flight command ITEM emits NO terminal event, so this synthesizes a
        ``close_canceled`` for the turn AND for every still-open child item span, then returns to idle."""
        self._require_orchestrate("turn/interrupt")
        if self.thread_id is None:
            raise CodexAdapterError("DENIED_NO_THREAD", required_action="no thread to interrupt")
        turn = turn or self.active_turn_id
        if not turn:
            raise CodexAdapterError("DENIED_NO_ACTIVE_TURN", required_action="interrupt while a turn is active")
        self._rpc.request("turn/interrupt", {"threadId": self.thread_id, "turnId": turn})
        # turn/completed(interrupted) may also arrive over the notification stream; close idempotently
        # here so the caller gets a synchronous, deterministic result and no span is left open.
        self._close_turn_canceled(turn, reason="interrupt")
        return {"status": "OK", "turn_id": turn, "interrupted": True}

    def _synthesize_item_closes(self, reason: str) -> None:
        # Synthesize close_canceled for every open child item span (P-B 2e: no per-item terminal on
        # interrupt/kill). Lock-guarded snapshot-and-clear so the reader thread cannot double-close.
        """(confirmed) synthesizes close_canceled for every open child item span on interrupt/kill (P-B 2e); lock-guarded snapshot-and-clear so the reader thread cannot double-close."""
        with self._span_lock:
            open_items = sorted(self._open_items)
            self._open_items.clear()
        for item_id in open_items:
            self._emit(
                "subagent", "close_canceled",
                correlation_id=item_id,
                summary=f"child item span synthesized-closed ({reason})",
                payload={"item_id": item_id, "reason": reason, "synthesized": True},
            )

    def _close_turn_canceled(self, turn: str, *, reason: str) -> None:
        # Synthesize open child-item closes, then close the turn close_canceled (idempotent).
        """(confirmed) synthesizes open child-item closes then closes the turn as close_canceled; idempotent; reason is the terminal status label."""
        self._synthesize_item_closes(reason)
        self._close_turn(turn, "close_canceled", status=reason)

    # --- resume (single-controller lease) ------------------------------------------------------

    def resume(self, thread_id: str) -> dict[str, Any]:
        """Resume a persisted thread under the single-controller lease. The app-server has NO thread
        lock (P-B 2c), so this detect-and-refuses when a DIFFERENT live controller holds the lease
        (DENIED_THREAD_LEASED); a dead owner is reclaimed and taken over. Then thread/resume on the
        live channel binds the thread."""
        self._require_orchestrate("thread/resume")
        rpc = self._rpc
        if rpc is None:
            raise CodexAdapterError(
                DENIED_ENGINE_UNAVAILABLE,
                required_action="open the Codex app-server channel before resuming a thread",
            )
        owner, reclaimed = self._lease.acquire(thread_id, os.getpid(), self._nonce)
        if owner is not None:
            raise CodexAdapterError(
                DENIED_THREAD_LEASED,
                thread_id=thread_id,
                owner_pid=owner.get("pid"),
                required_action="another live controller owns this thread; do not resume it",
            )
        claimed_here = self._leased_thread != thread_id
        try:
            result = rpc.request("thread/resume", {"threadId": thread_id})
            thread = result.get("thread") if isinstance(result, Mapping) else None
            if not isinstance(thread, Mapping):
                raise CodexAdapterError(
                    CODEX_PROTOCOL_ERROR,
                    required_action="repair the malformed thread/resume response",
                )
        except Exception:
            if claimed_here:
                self._lease.release(thread_id, self._nonce)
            raise
        self._leased_thread = thread_id
        self.thread_id = thread_id
        self.vendor_session_root_id = thread.get("sessionId") or self.vendor_session_root_id or thread_id
        self._emit(
            "session", "open",
            correlation_id=thread_id,
            summary="codex thread resumed under lease",
            payload={"vendor_thread_id": thread_id, "vendor_session_root_id": self.vendor_session_root_id,
                     "reclaimed": reclaimed},
        )
        return {"status": "OK", "thread_id": thread_id, "reclaimed": reclaimed}

    # --- side channels (deny / wrap matrix) ----------------------------------------------------

    def exec_wrapped(self, command: list[str], *, sandbox_policy: Mapping[str, Any] | None = None,
                     cwd: str | None = None) -> dict[str, Any]:
        """The ONLY exposed form of ``command/exec`` (threadless, synchronous, no vendor approval).
        (a) requires an EXPLICIT restrictive ``sandbox_policy`` (omitted => DENIED_SANDBOX_REQUIRED;
        danger-full-access => DENIED_SANDBOX_REQUIRED) -- never the user default; (b) routes through
        :meth:`policy.PolicyEngine.decide` as a pseudo-run BEFORE sending, deny/ask handled exactly like
        an approval (a policy deny is refused before any RPC leaves the process). Background/process-
        spawn helpers are NOT exposed."""
        self._require_orchestrate("command/exec")
        stype_ = _sandbox_type(sandbox_policy)
        if sandbox_policy is None or stype_ is None or stype_ in _DANGER_SANDBOX_TYPES:
            raise CodexAdapterError(
                DENIED_SANDBOX_REQUIRED,
                sandbox_policy=dict(sandbox_policy) if sandbox_policy else None,
                required_action="pass an explicit restrictive sandboxPolicy (read-only|workspace-write); "
                                "danger-full-access is refused",
            )
        if stype_ not in _SAFE_SANDBOX_TYPES:
            raise CodexAdapterError(
                DENIED_SANDBOX_REQUIRED,
                sandbox_type=stype_,
                required_action="sandboxPolicy.type must be read-only or workspace-write",
            )
        # Pseudo-run through the chokepoint BEFORE any RPC. deny/ask never reach the wire.
        action = policy.RequestedAction(
            actor=policy.Actor(self._engine, self.session_id or "codex", self.current_epoch, self.thread_id),
            verb="exec",
            command=" ".join(command),
            raw={"cwd": cwd, "sandbox_policy": dict(sandbox_policy), "side_channel": "command/exec"},
        )
        decision = self.policy_engine.decide(action, self.current_epoch)
        if decision.result != policy.ALLOW:
            resolved = self._resolve_non_allow(decision, correlation_hint=" ".join(command))
            if resolved != DECISION_APPROVED:
                if self._approval_broker_cb is None or decision.result != policy.ASK:
                    self._emit(
                        "approval", "declined",
                        correlation_id=decision.correlation_hash,
                        summary="command/exec pseudo-run denied by policy",
                        payload={"result": decision.result, "invariant_id": decision.invariant_id,
                                 "rule_id": decision.rule_id, "command": command},
                        code=decision.invariant_id or decision.rule_id,
                    )
                raise CodexAdapterError(
                    DENIED_SIDE_CHANNEL_POLICY,
                    result=decision.result,
                    invariant_id=decision.invariant_id,
                    rule_id=decision.rule_id,
                    required_action="command/exec refused by the policy chokepoint",
                )
        denial = check_mutation_guard(self._mutation_guard, action)
        if denial is not None:
            self._emit_mutation_guard_denial(
                action, decision, {"request_type": "commandExec", "item_id": None}, denial,
            )
            fields: dict[str, Any] = {
                "retryable": bool(denial.get("retryable", False)),
                "required_action": str(denial.get("required_action") or "reconcile workspace writer state"),
            }
            if isinstance(denial.get("holder"), Mapping):
                fields["holder"] = dict(denial["holder"])
            raise CodexAdapterError(str(denial["code"]), **fields)
        params: dict[str, Any] = {"command": command, "sandboxPolicy": dict(sandbox_policy)}
        if cwd:
            params["cwd"] = cwd
        result = self._rpc.request("command/exec", params)
        return {"status": "OK", **result}

    def shell_command(self, *_args: Any, **_kwargs: Any) -> None:
        """``thread/shellCommand`` runs FULLY UNSANDBOXED with NO approval (P-B 2d) -> NEVER exposed.
        This surface exists only to refuse it; no code path sends SHELL_COMMAND_METHOD."""
        raise CodexAdapterError(
            DENIED_UNSANDBOXED_SIDE_CHANNEL,
            method=SHELL_COMMAND_METHOD,
            required_action="thread/shellCommand is unsandboxed full-access and is never exposed",
        )

    # --- callbacks -----------------------------------------------------------------------------

    def on_event(self, cb: Callable[[dict[str, Any]], None]) -> None:
        """Register a raw-event sink (item/*/delta streaming lands here for T1 later; span open/close
        still funnels to the recorder)."""
        self._event_cb = cb

    def on_approval(self, cb: Callable[[dict[str, Any]], str]) -> None:
        """Register the human-decision resolver for ``ask`` outcomes. ``cb(request)`` returns one of
        DECISION_APPROVED / DECISION_DENIED (tests resolve it programmatically). The approval reply
        blocks on it; a timeout fails closed to denied."""
        self._approval_cb = cb

    def set_mutation_guard(self, callback: MutationGuard | None) -> None:
        """Install the final policy-approved action gate used by the daemon writer lease."""

        self._mutation_guard = callback

    def set_approval_broker(self, callback: ApprovalBrokerCallback | None) -> None:
        """Install the daemon-owned synchronous approval authority for ASK decisions."""

        self._approval_broker_cb = callback

    # --- notification + server-request handling ------------------------------------------------

    def _handle_disconnect(self) -> None:
        """(confirmed) marks the RPC channel disconnected (unless intentionally closing) and wakes any turn waiter so run_turn returns CODEX_CHILD_EXITED."""
        with self._turn_condition:
            self._disconnected = not self._closing
            self._turn_condition.notify_all()

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        # Deltas + non-span notifications go to on_event only (T1 wiring later). Span-shaped
        # notifications drive the recorder.
        """(confirmed) dispatches vendor notifications (turn/item started/completed) to span open/close + final-text rendezvous; on_event sink receives all raw notifications first."""
        if self._event_cb is not None:
            try:
                self._event_cb({"method": method, "params": params})
            except Exception as error:  # noqa: BLE001 -- an event-sink bug must not affect the reader
                self._log(f"on_event sink error: {error}")

        if method == "turn/started":
            turn = params.get("turn") or {}
            turn_id = turn.get("id")
            if turn_id:
                # The reader may see turn/started before start_turn's result returns; open idempotently.
                self._open_turn(turn_id)
        elif method == "item/started":
            item = params.get("item") or {}
            item_id = item.get("id")
            if item_id and isinstance(item, Mapping):
                with self._span_lock:
                    self._item_details[str(item_id)] = dict(item)
            if item_id and item.get("type") == "commandExecution":
                with self._span_lock:
                    new = item_id not in self._open_items
                    self._open_items.add(item_id)
                if new:
                    self._emit("subagent", "open", correlation_id=item_id,
                               summary="codex command item started",
                               payload={"item_id": item_id, "item_type": item.get("type")})
        elif method == "item/completed":
            item = params.get("item") or {}
            item_id = item.get("id")
            turn_id = params.get("turnId")
            if item_id and isinstance(item, Mapping):
                with self._span_lock:
                    self._item_details[str(item_id)] = dict(item)
            if item.get("type") == "agentMessage" and isinstance(turn_id, str) and isinstance(item.get("text"), str):
                # A completed final_answer wins over commentary. A later final item naturally replaces
                # an earlier one; the terminal rendezvous consumes the last authoritative message.
                with self._turn_condition:
                    if item.get("phase") == "final_answer" or turn_id not in self._turn_final_text:
                        self._turn_final_text[turn_id] = item["text"]
            if item_id:
                with self._span_lock:
                    was_open = item_id in self._open_items
                    self._open_items.discard(item_id)
                if was_open:
                    status = item.get("status")
                    marker = "close_fail" if status in ("declined", "failed", "aborted") else "close_ok"
                    # received_decision/command_run echo the vendor's view of the approval reply --
                    # the wire-vocabulary regression guard (tests assert accept/decline arrived).
                    self._emit("subagent", marker, correlation_id=item_id,
                               summary="codex command item completed",
                               payload={"item_id": item_id, "status": status,
                                        "received_decision": item.get("receivedDecision"),
                                        "command_run": item.get("commandRun")})
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            turn_id = turn.get("id") or self._active_turn
            status = turn.get("status")
            if turn_id:
                with self._turn_condition:
                    for item in turn.get("items", []) if isinstance(turn.get("items"), list) else []:
                        if (isinstance(item, Mapping) and item.get("type") == "agentMessage"
                                and isinstance(item.get("text"), str)):
                            if item.get("phase") == "final_answer" or turn_id not in self._turn_final_text:
                                self._turn_final_text[str(turn_id)] = str(item["text"])
                if status == "interrupted":
                    self._close_turn_canceled(turn_id, reason="interrupted")
                else:
                    self._close_turn(turn_id, "close_ok" if status == "completed" else "close_fail",
                                     status=status)
                with self._turn_condition:
                    self._turn_terminal[str(turn_id)] = dict(turn) if isinstance(turn, Mapping) else {}
                    self._turn_condition.notify_all()

    @staticmethod
    def _malformed_approval_reply(method: str) -> dict[str, Any]:
        if method in (APPROVAL_METHOD, FILE_CHANGE_APPROVAL_METHOD):
            return {"decision": WIRE_DECLINE}
        if method == PERMISSIONS_APPROVAL_METHOD:
            return {"permissions": {}, "scope": "turn"}
        if method in USER_INPUT_METHODS:
            return {"answers": {}}
        if method == MCP_ELICITATION_METHOD:
            return {"action": "decline"}
        if method in LEGACY_APPROVAL_METHODS:
            return {"decision": "denied"}
        return {"error": {"code": -32601, "message": "unhandled server request"}}

    def _approval_shape_valid(self, method: str, params: Mapping[str, Any]) -> bool:
        """Validate only protocol fields needed for a safe, attributable policy decision."""
        def nonempty(name: str) -> bool:
            return isinstance(params.get(name), str) and bool(str(params[name]).strip())

        if method == APPROVAL_METHOD:
            command = params.get("command")
            return (nonempty("threadId") and nonempty("turnId") and nonempty("itemId")
                    and ((isinstance(command, str) and bool(command.strip()))
                         or (isinstance(command, list) and bool(command)
                             and all(isinstance(part, str) for part in command))))
        if method == FILE_CHANGE_APPROVAL_METHOD:
            return (nonempty("threadId") and nonempty("turnId") and nonempty("itemId")
                    and bool(self._file_targets(params)))
        if method == PERMISSIONS_APPROVAL_METHOD:
            return (nonempty("threadId") and nonempty("turnId") and nonempty("itemId")
                    and isinstance(params.get("permissions"), Mapping) and bool(params["permissions"]))
        if method in USER_INPUT_METHODS:
            questions = params.get("questions")
            return (nonempty("threadId") and nonempty("turnId") and isinstance(questions, list)
                    and bool(questions) and all(
                        isinstance(question, Mapping) and isinstance(question.get("id"), str)
                        and bool(str(question.get("id") or "").strip())
                        for question in questions
                    ))
        if method == MCP_ELICITATION_METHOD:
            return (nonempty("threadId") and nonempty("turnId")
                    and params.get("mode") in ("form", "openai/form")
                    and isinstance(params.get("requestedSchema"), Mapping))
        if method == LEGACY_EXEC_APPROVAL_METHOD:
            command = params.get("command")
            return (nonempty("callId") and nonempty("conversationId")
                    and ((isinstance(command, str) and bool(command.strip()))
                         or (isinstance(command, list) and bool(command))))
        if method == LEGACY_PATCH_APPROVAL_METHOD:
            return (nonempty("callId") and nonempty("conversationId")
                    and isinstance(params.get("fileChanges"), Mapping) and bool(params["fileChanges"]))
        return False

    def _handle_server_request(self, request_id: int | str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Answer every installed approval/interaction family through the immutable policy snapshot."""
        if method in (APPROVAL_METHOD, FILE_CHANGE_APPROVAL_METHOD, PERMISSIONS_APPROVAL_METHOD,
                      MCP_ELICITATION_METHOD, LEGACY_EXEC_APPROVAL_METHOD, LEGACY_PATCH_APPROVAL_METHOD) \
                or method in USER_INPUT_METHODS:
            if not self._approval_shape_valid(method, params):
                self._emit(
                    "transport", "point", summary="malformed codex approval request denied",
                    payload={"method": method}, code=CODEX_PROTOCOL_ERROR,
                )
                return self._malformed_approval_reply(method)
        if method == APPROVAL_METHOD:
            return self._answer_approval(params)
        if method == FILE_CHANGE_APPROVAL_METHOD:
            return self._answer_file_change(params, legacy=False)
        if method == PERMISSIONS_APPROVAL_METHOD:
            return self._answer_permissions(params)
        if method in USER_INPUT_METHODS:
            return self._answer_user_input(params)
        if method == MCP_ELICITATION_METHOD:
            return self._answer_mcp_elicitation(params)
        if method == LEGACY_EXEC_APPROVAL_METHOD:
            return self._answer_legacy_exec(params)
        if method == LEGACY_PATCH_APPROVAL_METHOD:
            return self._answer_file_change(params, legacy=True)
        if method == AUTH_REFRESH_METHOD:
            self._emit("auth", "point", summary="codex auth-refresh server request (M14)",
                       payload={"method": method})
            return {"error": {"code": -32000, "message": "auth refresh not serviced by adapter (M14)"}}
        self._log("codex: unknown server request denied safely")
        self._emit("transport", "point", summary="unknown codex server request denied safely",
                   payload={"method": method})
        return {"error": {"code": -32601, "message": f"unhandled server request: {method}"}}

    def _actor(self, params: Mapping[str, Any]) -> policy.Actor:
        thread_id = params.get("threadId") or params.get("conversationId") or self.thread_id
        return policy.Actor(self._engine, self.session_id or "codex", self.current_epoch,
                            str(thread_id) if thread_id else None)

    def _resolve_action(self, action: policy.RequestedAction, params: Mapping[str, Any], request_type: str) -> bool:
        decision = self.policy_engine.decide(action, self.current_epoch)
        item_id = params.get("itemId") or params.get("callId")
        base = {"request_type": request_type, "item_id": item_id}
        guardable = request_type not in ("requestUserInput", "mcpElicitation") \
            and mutation_guard_required(action)
        if decision.result == policy.ALLOW:
            denial = check_mutation_guard(self._mutation_guard, action) if guardable else None
            if denial is not None:
                self._emit_mutation_guard_denial(action, decision, base, denial)
                return False
            self._emit(
                "approval", "resolved", correlation_id=decision.correlation_hash,
                summary=f"{request_type} allowed by policy",
                payload={**base, "decision": DECISION_APPROVED, "rule_id": decision.rule_id},
                code=decision.rule_id,
            )
            return True
        if decision.result == policy.DENY:
            self._emit(
                "approval", "declined", correlation_id=decision.correlation_hash,
                summary=f"{request_type} denied by policy",
                payload={**base, "decision": DECISION_DENIED, "invariant_id": decision.invariant_id,
                         "rule_id": decision.rule_id},
                code=decision.invariant_id or decision.rule_id,
            )
            return False
        resolved = self._resolve_non_allow(decision, correlation_hint=item_id)
        approved = resolved == DECISION_APPROVED
        if approved:
            denial = check_mutation_guard(self._mutation_guard, action) if guardable else None
            if denial is not None:
                self._emit_mutation_guard_denial(action, decision, base, denial)
                return False
        if self._approval_broker_cb is None:
            self._emit(
                "approval", "resolved" if approved else "declined", correlation_id=decision.correlation_hash,
                summary=f"{request_type} resolved ({'approve' if approved else 'deny'})",
                payload={**base, "decision": DECISION_APPROVED if approved else DECISION_DENIED},
            )
        return approved

    def _emit_mutation_guard_denial(
        self,
        action: policy.RequestedAction,
        decision: policy.Decision,
        base: Mapping[str, Any],
        denial: Mapping[str, Any],
    ) -> None:
        """Record a final workspace denial before the adapter returns any vendor allow response."""

        code = str(denial["code"])
        payload: dict[str, Any] = {
            **dict(base),
            "decision": DECISION_DENIED,
            "denial_code": code,
            "verb": action.verb,
        }
        if isinstance(denial.get("holder"), Mapping):
            payload["holder"] = dict(denial["holder"])
        if self._approval_broker_cb is None or decision.result != policy.ASK:
            self._emit(
                "approval", "declined", correlation_id=decision.correlation_hash,
                summary=f"{base.get('request_type') or 'action'} denied by workspace writer guard",
                payload=payload, code=code,
            )
        item_id = base.get("item_id")
        self._emit(
            "tool_call", "close_fail",
            correlation_id=str(item_id or decision.correlation_hash),
            summary="codex action denied by workspace writer guard",
            payload={"item_id": item_id, "verb": action.verb, "denial_code": code}, code=code,
        )

    def _command_action(self, params: Mapping[str, Any]) -> policy.RequestedAction:
        """(confirmed) builds a RequestedAction (verb=net for networkApprovalContext, else exec) from an approval params mapping for the policy chokepoint."""
        command = params.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        network = params.get("networkApprovalContext")
        if isinstance(network, Mapping):
            target = "://".join(str(network.get(key) or "") for key in ("protocol", "host")).strip(":/")
            return policy.RequestedAction(
                actor=self._actor(params), verb="net", targets=(target,) if target else (),
                raw={"cwd": params.get("cwd"), "vendor": dict(params)},
            )
        return policy.RequestedAction(
            actor=self._actor(params), verb="exec", command=command if isinstance(command, str) else None,
            raw={"cwd": params.get("cwd"), "vendor": dict(params)},
        )

    def _answer_approval(self, params: dict[str, Any]) -> dict[str, Any]:
        approved = self._resolve_action(self._command_action(params), params, "commandExecution")
        return {"decision": WIRE_ACCEPT if approved else WIRE_DECLINE}

    def _answer_legacy_exec(self, params: dict[str, Any]) -> dict[str, Any]:
        approved = self._resolve_action(self._command_action(params), params, "legacyExecCommand")
        return {"decision": "approved" if approved else "denied"}

    def _file_targets(self, params: Mapping[str, Any]) -> tuple[str, ...]:
        """(added) dedupes file_write policy targets from grantRoot + fileChanges keys + the cached item's changes (path/movePath/move_path); order-preserving. Non-obvious and security-relevant (drives the file_write decision targets)."""
        targets: list[str] = []
        grant_root = params.get("grantRoot")
        if isinstance(grant_root, str):
            targets.append(grant_root)
        file_changes = params.get("fileChanges")
        if isinstance(file_changes, Mapping):
            targets.extend(str(path) for path in file_changes if isinstance(path, str))
        item_id = params.get("itemId")
        item = self._item_details.get(str(item_id), {}) if item_id else {}
        for change in item.get("changes", []) if isinstance(item.get("changes"), list) else []:
            if not isinstance(change, Mapping):
                continue
            for key in ("path", "movePath", "move_path"):
                value = change.get(key)
                if isinstance(value, str):
                    targets.append(value)
        return tuple(dict.fromkeys(targets))

    def _answer_file_change(self, params: dict[str, Any], *, legacy: bool) -> dict[str, Any]:
        cwd = params.get("cwd")
        if not cwd and self._policy_snapshot is not None:
            cwd = self._policy_snapshot.workspace_root
        action = policy.RequestedAction(
            actor=self._actor(params), verb="file_write", targets=self._file_targets(params),
            raw={"cwd": cwd, "vendor": dict(params)},
        )
        approved = self._resolve_action(action, params, "legacyPatch" if legacy else "fileChange")
        if legacy:
            return {"decision": "approved" if approved else "denied"}
        return {"decision": WIRE_ACCEPT if approved else WIRE_DECLINE}

    def _permission_actions(self, params: Mapping[str, Any]) -> list[policy.RequestedAction]:
        """(confirmed) fans a request-permissions payload into one RequestedAction per net/file_read/file_write grant; empty/unmodeled shape falls back to a single generic tool action."""
        requested = params.get("permissions")
        if not isinstance(requested, Mapping):
            return [policy.RequestedAction(actor=self._actor(params), verb="tool", raw={"vendor": dict(params)})]
        actions: list[policy.RequestedAction] = []
        network = requested.get("network")
        if isinstance(network, Mapping) and network.get("enabled"):
            actions.append(policy.RequestedAction(actor=self._actor(params), verb="net", raw={"vendor": dict(params)}))
        filesystem = requested.get("fileSystem")
        if isinstance(filesystem, Mapping):
            for verb, key in (("file_read", "read"), ("file_write", "write")):
                values = filesystem.get(key)
                if isinstance(values, list) and values:
                    actions.append(policy.RequestedAction(
                        actor=self._actor(params), verb=verb,
                        targets=tuple(str(value) for value in values), raw={"cwd": params.get("cwd")},
                    ))
            for entry in filesystem.get("entries", []) if isinstance(filesystem.get("entries"), list) else []:
                if not isinstance(entry, Mapping):
                    continue
                access = entry.get("access")
                path_spec = entry.get("path")
                target = path_spec.get("path") if isinstance(path_spec, Mapping) else None
                verb = "file_write" if access == "write" else "file_read"
                actions.append(policy.RequestedAction(
                    actor=self._actor(params), verb=verb,
                    targets=(str(target),) if target else (), raw={"cwd": params.get("cwd"), "vendor": dict(entry)},
                ))
        return actions or [policy.RequestedAction(actor=self._actor(params), verb="tool", raw={"vendor": dict(params)})]

    def _answer_permissions(self, params: dict[str, Any]) -> dict[str, Any]:
        approved = all(self._resolve_action(action, params, "permissions")
                       for action in self._permission_actions(params))
        requested = params.get("permissions")
        granted = dict(requested) if approved and isinstance(requested, Mapping) else {}
        return {"permissions": granted, "scope": "turn"}

    @staticmethod
    def _choice_label(question: Mapping[str, Any], approved: bool) -> str | None:
        """(confirmed) selects an option label matching accept/decline intent (case-folded); returns None for free-text or when no matching label is found on the decline path."""
        options = question.get("options")
        labels = [str(option.get("label")) for option in options or []
                  if isinstance(option, Mapping) and isinstance(option.get("label"), str)]
        preferred = ("accept", "approve", "allow", "yes", "continue") if approved else (
            "decline", "deny", "reject", "no", "cancel")
        for needle in preferred:
            for label in labels:
                if needle in label.casefold():
                    return label
        return labels[0] if approved and labels else None

    def _answer_user_input(self, params: dict[str, Any]) -> dict[str, Any]:
        action = policy.RequestedAction(actor=self._actor(params), verb="tool", raw={"vendor": dict(params)})
        questions = params.get("questions") if isinstance(params.get("questions"), list) else []
        resolved = self._resolve_user_input_action(action, params, questions)
        approved = resolved.decision == DECISION_APPROVED
        free_text_ids = {
            str(question["id"])
            for question in questions
            if isinstance(question, Mapping)
            and isinstance(question.get("id"), str)
            and not self._question_has_choices(question)
        }
        explicit = self._explicit_user_answers(resolved.updated_input, free_text_ids) if approved else None
        if free_text_ids and explicit is None:
            approved = False
        answers: dict[str, Any] = {}
        for question in questions:
            if not isinstance(question, Mapping) or not isinstance(question.get("id"), str):
                continue
            question_id = str(question["id"])
            answer = self._choice_label(question, approved) if self._question_has_choices(question) else (
                explicit.get(question_id) if approved and explicit is not None else None
            )
            answers[question_id] = {"answers": [answer] if answer is not None else []}
        return {"answers": answers}

    @staticmethod
    def _question_has_choices(question: Mapping[str, Any]) -> bool:
        """Return whether a question exposes at least one labeled choice."""

        options = question.get("options")
        return isinstance(options, list) and any(
            isinstance(option, Mapping) and isinstance(option.get("label"), str)
            for option in options
        )

    @staticmethod
    def _explicit_user_answers(
        updated_input: Mapping[str, Any] | None,
        question_ids: set[str],
    ) -> dict[str, str] | None:
        """Validate exact broker-supplied free-text answers; binary approval alone is insufficient."""

        if not question_ids:
            return {}
        if not isinstance(updated_input, Mapping) or set(updated_input) != {"answers"}:
            return None
        raw_answers = updated_input.get("answers")
        if not isinstance(raw_answers, Mapping) or set(raw_answers) != question_ids:
            return None
        answers: dict[str, str] = {}
        for question_id, value in raw_answers.items():
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value.encode("utf-8")) > 64 * 1024
                or "\x00" in value
                or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
            ):
                return None
            answers[str(question_id)] = value
        return answers

    def _resolve_user_input_action(
        self,
        action: policy.RequestedAction,
        params: Mapping[str, Any],
        questions: list[Any],
    ) -> BrokerApprovalResult:
        """Resolve user input while retaining explicit answer data from the typed approval broker."""

        decision = self.policy_engine.decide(action, self.current_epoch)
        item_id = params.get("itemId") or params.get("callId")
        base = {"request_type": "requestUserInput", "item_id": item_id}
        free_text_ids = {
            str(question["id"])
            for question in questions
            if isinstance(question, Mapping)
            and isinstance(question.get("id"), str)
            and not self._question_has_choices(question)
        }
        if decision.result == policy.ALLOW:
            if free_text_ids:
                self._emit(
                    "approval", "declined", correlation_id=decision.correlation_hash,
                    summary="requestUserInput requires explicit answer content",
                    payload={**base, "decision": DECISION_DENIED},
                    code=DENIED_USER_INPUT_ANSWER_REQUIRED,
                )
                return BrokerApprovalResult(DECISION_DENIED)
            self._emit(
                "approval", "resolved", correlation_id=decision.correlation_hash,
                summary="requestUserInput allowed by policy",
                payload={**base, "decision": DECISION_APPROVED, "rule_id": decision.rule_id},
                code=decision.rule_id,
            )
            return BrokerApprovalResult(DECISION_APPROVED)
        if decision.result == policy.DENY:
            self._emit(
                "approval", "declined", correlation_id=decision.correlation_hash,
                summary="requestUserInput denied by policy",
                payload={**base, "decision": DECISION_DENIED, "invariant_id": decision.invariant_id,
                         "rule_id": decision.rule_id},
                code=decision.invariant_id or decision.rule_id,
            )
            return BrokerApprovalResult(DECISION_DENIED)
        resolved = self._resolve_non_allow_result(
            decision,
            correlation_hint=item_id,
            approval_request={
                "request_type": "requestUserInput",
                "questions": [dict(question) for question in questions if isinstance(question, Mapping)],
            },
        )
        if (
            resolved.decision == DECISION_APPROVED
            and free_text_ids
            and self._explicit_user_answers(resolved.updated_input, free_text_ids) is None
        ):
            self._emit(
                "approval", "declined", correlation_id=decision.correlation_hash,
                summary="requestUserInput explicit answer missing or invalid",
                payload={**base, "decision": DECISION_DENIED},
                code=DENIED_USER_INPUT_ANSWER_REQUIRED,
            )
            return BrokerApprovalResult(DECISION_DENIED)
        if self._approval_broker_cb is None:
            self._emit(
                "approval", "resolved" if resolved.decision == DECISION_APPROVED else "declined",
                correlation_id=decision.correlation_hash,
                summary=f"requestUserInput resolved ({'approve' if resolved.decision == DECISION_APPROVED else 'deny'})",
                payload={**base, "decision": resolved.decision},
            )
        return resolved

    def _answer_mcp_elicitation(self, params: dict[str, Any]) -> dict[str, Any]:
        action = policy.RequestedAction(actor=self._actor(params), verb="net", raw={"vendor": dict(params)})
        approved = self._resolve_action(action, params, "mcpElicitation")
        # Never open a URL or invent required form data. Accept only a form whose required values all
        # have defaults; every other shape is a safe decline even after a binary UI approval.
        mode = params.get("mode")
        schema = params.get("requestedSchema")
        content: dict[str, Any] = {}
        if approved and mode in ("form", "openai/form") and isinstance(schema, Mapping):
            properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
            required = schema.get("required") if isinstance(schema.get("required"), list) else []
            for key, definition in properties.items():
                if isinstance(definition, Mapping) and "default" in definition:
                    content[str(key)] = definition["default"]
            approved = all(str(key) in content for key in required)
        else:
            approved = False
        return {"action": "accept", "content": content} if approved else {"action": "decline"}

    def _resolve_non_allow_result(
        self,
        decision: policy.Decision,
        *,
        correlation_hint: Any = None,
        approval_request: Mapping[str, Any] | None = None,
    ) -> BrokerApprovalResult:
        """Resolve ASK through the typed broker or the legacy persisted binary approval path."""

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
        if not self.session_id:
            self._log("record_ask unavailable without a bound session; approval denied")
            return BrokerApprovalResult(DECISION_DENIED)
        try:
            persisted = self.policy_engine.record_ask(decision, self.session_id)
        except Exception as error:  # noqa: BLE001 -- ask persistence must not break the reply path
            self._log(f"record_ask failed ({type(error).__name__})")
            return BrokerApprovalResult(DECISION_DENIED)
        if not isinstance(persisted, Mapping) or persisted.get("status") != "OK":
            self._log("record_ask returned non-OK; approval denied")
            return BrokerApprovalResult(DECISION_DENIED)
        self._emit(
            "approval", "open", correlation_id=decision.correlation_hash,
            summary="codex action requires approval",
            payload={
                "decision_hint": decision.result,
                "reason": decision.reason,
                "item": correlation_hint,
            },
        )
        if self._approval_cb is None:
            return BrokerApprovalResult(DECISION_DENIED)  # fail-closed: no human surface -> deny
        box: dict[str, str] = {}
        done = threading.Event()

        def _run() -> None:
            try:
                box["decision"] = self._approval_cb(request)
            except Exception as error:  # noqa: BLE001 -- resolver bug fails closed
                self._log(f"on_approval resolver error ({type(error).__name__})")
                box["decision"] = DECISION_DENIED
            finally:
                done.set()

        threading.Thread(target=_run, name="codex-approval-resolver", daemon=True).start()
        if not done.wait(self._approval_timeout):
            self._log("approval resolver timed out -> deny (fail-closed)")
            return BrokerApprovalResult(DECISION_DENIED)
        normalized = DECISION_APPROVED if box.get("decision") == DECISION_APPROVED else DECISION_DENIED
        return BrokerApprovalResult(normalized)

    def _resolve_non_allow(self, decision: policy.Decision, *, correlation_hint: Any = None) -> str:
        """Compatibility wrapper returning only the typed ASK result's decision."""

        return self._resolve_non_allow_result(decision, correlation_hint=correlation_hint).decision

    # --- teardown ------------------------------------------------------------------------------

    def close(self) -> dict[str, Any]:
        """Close an idle logical conversation and reap its persistent app-server child."""
        if self.active_turn_id is not None:
            raise CodexAdapterError(
                "DENIED_SESSION_NOT_IDLE",
                required_action="interrupt or kill the active Codex turn before close",
            )
        result = self.kill()
        if result.get("status") != "OK":
            return {**result, "closed": False}
        return {"status": "OK", "closed": True, "termination_proven": True}

    def kill(self) -> dict[str, Any]:
        """Terminal teardown (§A.3 every-exit-path-terminal). Stop the reader first (so it cannot race
        the synthesized closes), then reap the OWNED child tree before releasing its thread lease and
        runtime. A failed reap stays attached for an idempotent retry and is reported fail-closed."""
        self._closing = True
        self._opened = False
        cleanup_errors: list[str] = []
        rpc, self._rpc = self._rpc, None
        if rpc is not None:
            try:
                rpc.close()
            except Exception as error:  # noqa: BLE001 -- continue to the owned-child hard stop
                self._log(f"rpc close error ({type(error).__name__})")
                cleanup_errors.append("rpc_close_failed")

        termination_proven = True
        child = self._child
        if child is not None:
            try:
                child.kill_tree()
                termination_proven = child.poll() is not None
            except Exception as error:  # noqa: BLE001 -- surface a bounded fail-closed result
                self._log(f"child kill_tree error ({type(error).__name__})")
                termination_proven = False
            if termination_proven:
                # Close pipe streams only after death is proven. An unproven child stays attached so
                # a later kill can retry instead of discarding the only ownership handle.
                for stream in (child.process.stdin, child.process.stdout, child.process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                self._child = None

        # Terminalize spans without claiming a failed termination was a completed kill.
        with self._span_lock:
            active_turn = self._active_turn
        close_reason = "killed" if termination_proven else "termination_unproven"
        if active_turn is not None:
            self._close_turn_canceled(active_turn, reason=close_reason)
        else:
            self._synthesize_item_closes(close_reason)

        if not termination_proven:
            with self._turn_condition:
                self._turn_condition.notify_all()
            return {
                "status": "ERROR",
                "code": RUNTIME_CLEANUP_FAILED,
                "cleanup_errors": cleanup_errors + ["child_termination_unproven"],
                "killed": False,
                "termination_proven": False,
            }

        if self._leased_thread is not None:
            try:
                self._lease.release(self._leased_thread, self._nonce)
            except Exception as error:  # noqa: BLE001 -- preserve fail-closed lease state
                self._log(f"thread lease release error ({type(error).__name__})")
                cleanup_errors.append("thread_lease_release_failed")
            else:
                self._leased_thread = None
        cleanup_errors.extend(self._stop_runtime())
        with self._turn_condition:
            self._turn_condition.notify_all()
        if cleanup_errors:
            return {
                "status": "ERROR",
                "code": RUNTIME_CLEANUP_FAILED,
                "cleanup_errors": cleanup_errors,
                "killed": True,
                "termination_proven": True,
            }
        return {"status": "OK", "killed": True, "termination_proven": True}

    # --- recorder ------------------------------------------------------------------------------

    def _emit(self, event_kind: str, marker: str, *, summary: str,
              correlation_id: str | None = None, payload: dict[str, Any] | None = None,
              code: str | None = None) -> None:
        """Emit one normalized recorder event {event_kind, marker, correlation_id, summary, payload}.
        The supervisor funnels these to T5/T6/T8; tests capture them in a list. NEVER writes the DB
        here and NEVER prints to stdout (logging goes to the injected logger)."""
        event: dict[str, Any] = {
            "event_kind": event_kind,
            "marker": marker,
            "correlation_id": correlation_id,
            "summary": summary,
            "payload": payload or {},
        }
        if code:
            event["code"] = code
        try:
            self._recorder(event)
        except Exception as error:  # noqa: BLE001 -- a recorder sink bug must not break the adapter
            self._log(f"recorder sink error ({type(error).__name__})")


def _sandbox_type(sandbox_policy: Mapping[str, Any] | None) -> str | None:
    if not isinstance(sandbox_policy, Mapping):
        return None
    value = sandbox_policy.get("type")
    return str(value) if value is not None else None


# --- installed schema/capability probe (never submits a turn) ---------------------------------

def _command_prefix(command: str | os.PathLike[str] | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(command, (list, tuple)):
        if not command:
            raise ValueError("empty codex command")
        prefix = [str(value) for value in command]
    else:
        prefix = [str(command)]
    prefix[0] = shutil.which(prefix[0]) or prefix[0]
    return prefix


def _is_system_drive(path: Path) -> bool:
    """Return whether a Windows path is missing a drive or resolves to the OS system drive."""

    if os.name != "nt":
        return False
    system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\/").casefold()
    drive = path.drive.rstrip("\\/").casefold()
    return not drive or drive == system_drive


def _with_generated_schema(
    codex_cmd: str | os.PathLike[str] | list[str] | tuple[str, ...],
    callback: Callable[[Path], Any],
    *,
    runtime_dir: str | os.PathLike[str] | None,
    timeout: float,
) -> Any:
    parent = Path(runtime_dir) if runtime_dir is not None else RUNTIME_DIR
    parent = parent.resolve()
    if _is_system_drive(parent):
        raise RuntimeError("Codex schema runtime must be off the system drive")
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex-schema-", dir=str(parent)) as temp:
        root = Path(temp)
        out = root / "schema"
        home = root / "codex-home"
        temp_root = root / "tmp"
        out.mkdir()
        home.mkdir()
        temp_root.mkdir()
        child_env = children.compose_child_env()
        for name in _SENSITIVE_ENV:
            child_env.pop(name, None)
        child_env["CODEX_HOME"] = str(home)
        for name in ("TEMP", "TMP", "TMPDIR"):
            child_env[name] = str(temp_root)
        command = [*_command_prefix(codex_cmd), "app-server", "generate-json-schema",
                   "--experimental", "--out", str(out)]
        proc = subprocess.run(  # noqa: S603 -- fixed local binary/schema generator; no authenticated turn
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"codex schema generation failed with exit code {proc.returncode}")
        return callback(out)


def schema_from_live_dump(
    codex_cmd: str | os.PathLike[str] | list[str] | tuple[str, ...] = "codex",
    timeout: float = 30.0,
    *,
    runtime_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Generate and return the four schemas the H2.3 safety probe consumes.

    Codex 0.130 changed this command from stdout JSON to ``--out <DIR>``. The temporary directory is
    always under the D-drive Kaizen runtime by default, never the OS/user temp directory or CODEX_HOME.
    """
    def _read(out: Path) -> dict[str, Any]:
        return {
            "client": _load_schema(out / "ClientRequest.json"),
            "server": _load_schema(out / "ServerRequest.json"),
            "turn_start": _load_schema(out / "v2" / "TurnStartParams.json"),
            "hooks_list": _load_schema(out / "v2" / "HooksListResponse.json"),
        }

    return _with_generated_schema(codex_cmd, _read, runtime_dir=runtime_dir, timeout=timeout)


def schema_capabilities_from_installed(
    codex_cmd: str | os.PathLike[str] | list[str] | tuple[str, ...] = "codex",
    *,
    runtime_dir: str | os.PathLike[str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    return _with_generated_schema(
        codex_cmd,
        probe_schema_capabilities,
        runtime_dir=runtime_dir,
        timeout=timeout,
    )


def installed_capability(
    codex_cmd: str | os.PathLike[str] | list[str] | tuple[str, ...] = "codex",
    *,
    runtime_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return the canonical capability descriptor from the non-turn installed-schema probe.

    A schema pass is reported ``degraded`` rather than drivable because model/config, sandbox readiness,
    and session-hook loading still run at ``open``. A schema failure is an exact fail-closed denial. The
    installed 0.130 build lands on the bounded-read denial and therefore never starts app-server here.
    """
    base = {
        "id": "codex",
        "label": "Codex",
        "models": [],
        "default_model": None,
        "default_reasoning_effort": None,
        "auth_modes": list(SUPPORTED_AUTH_MODES),
        "warnings": [],
    }
    try:
        caps = schema_capabilities_from_installed(codex_cmd, runtime_dir=runtime_dir)
    except Exception as error:  # noqa: BLE001 -- capability discovery is a non-throwing API surface
        return {
            **base,
            "drivable": False,
            "availability": {
                "state": "unavailable",
                "code": DENIED_ENGINE_UNAVAILABLE,
                "message": f"Codex schema probe failed ({type(error).__name__})",
            },
            "permission_modes": [],
            "warnings": ["No authenticated turn was started."],
        }
    missing = list(caps.get("missing_methods") or [])
    if missing:
        return {
            **base,
            "drivable": False,
            "availability": {
                "state": "policy_gate_unavailable",
                "code": DENIED_POLICY_GATE_UNAVAILABLE,
                "message": f"Codex app-server is missing required methods: {', '.join(missing)}",
            },
            "permission_modes": [],
            "warnings": ["Upgrade Codex before enabling driven sessions."],
        }
    if not caps.get("pre_tool_command_hook") or not caps.get("bounded_read_access"):
        return {
            **base,
            "drivable": False,
            "availability": {
                "state": "policy_gate_unavailable",
                "code": DENIED_POLICY_GATE_UNAVAILABLE,
                "message": (
                    "Installed Codex cannot boundary-gate outside-workspace reads; PreToolUse remains "
                    "defense-in-depth and cannot substitute for readOnly.access/workspaceWrite.readOnlyAccess"
                ),
            },
            "permission_modes": [],
            "warnings": [
                "Upgrade to a Codex app-server build with bounded read access. Full remains unsupported."
            ],
        }
    return {
        **base,
        "drivable": False,
        "availability": {
            "state": "degraded",
            "code": "DENIED_ENGINE_DEGRADED",
            "message": "Schema safety passed; runtime model/config, sandbox readiness, and hook loading validate at start",
        },
        "permission_modes": list(SUPPORTED_PERMISSION_MODES),
        "warnings": ["Full is unavailable because it cannot preserve the invariant floor."],
    }
