"""Own one fail-closed Claude SDK worker and framed streaming session per conversation with sanitized worker environment."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import queue
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .. import children
from ..apply_evidence import normalize_apply_evidence, staged_cleanup_paths
from ..claude_worker_protocol import (
    CAPABILITY_PROBE_FEATURES,
    MAX_DELTA_BYTES,
    MAX_FRAME_BYTES,
    MAX_TURNS_DEFAULT,
    TOOL_NAMES,
    WorkerProtocolError,
    decode_frame,
    encode_frame,
    sanitize_model_catalog,
    validate_capability_probe_result,
    validate_profile,
    validate_reference,
)
from ..proposal_executor import WorkspaceProposalExecutor
from . import ApprovalBrokerCallback, BrokerApprovalResult, MutationGuard, TurnResult


DENIED_SDK_UNAVAILABLE = "DENIED_SDK_UNAVAILABLE"
DENIED_AUTH_UNAVAILABLE = "DENIED_AUTH_UNAVAILABLE"
DENIED_AUTH_MODE_MISMATCH = "DENIED_AUTH_MODE_MISMATCH"
DENIED_MODEL_UNAVAILABLE = "DENIED_MODEL_UNAVAILABLE"
DENIED_EFFORT_UNSUPPORTED = "DENIED_EFFORT_UNSUPPORTED"
DENIED_WORKER_PROTOCOL = "DENIED_WORKER_PROTOCOL"
DENIED_WORKER_OVERSIZE = "DENIED_WORKER_OVERSIZE"
DENIED_TOOL_UNSUPPORTED = "DENIED_TOOL_UNSUPPORTED"
DENIED_APPROVAL_STALE_RERUN_REQUIRED = "DENIED_APPROVAL_STALE_RERUN_REQUIRED"
DENIED_WORKSPACE_RECOVERY_REQUIRED = "DENIED_WORKSPACE_RECOVERY_REQUIRED"
MODEL_CALL_BUDGET_EXHAUSTED = "MODEL_CALL_BUDGET_EXHAUSTED"
WORKER_DIED = "WORKER_DIED"

_REQUEST_TIMEOUT_SECONDS = 30.0
# The owned worker bounds cold provider initialization at 120 seconds.  The host must remain alive long
# enough to receive that authoritative result instead of manufacturing an earlier protocol timeout.
_INITIALIZE_TIMEOUT_SECONDS = 135.0
_CAPABILITY_PROBE_TIMEOUT_SECONDS = 5.0
_INTERRUPT_TIMEOUT_SECONDS = 2.0
_CLOSE_TIMEOUT_SECONDS = 5.0
_TOOL_RESULT_INLINE_BYTES = 256 * 1024
_CAPABILITY_PROMPT = b"KAIZEN_CAPABILITY_PROBE_PROMPT"
_CAPABILITY_CONTEXT = b"KAIZEN_CAPABILITY_CONTEXT"
_CAPABILITY_IMAGE = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
    "0000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082"
)
_STRIP_ENV_EXACT = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY",
    "CODEX_API_KEY", "GOOGLE_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_PROTOCOL",
    "NODE_OPTIONS", "NODE_PATH", "SENTRY_DSN", "CLAUDE_CODE_MAX_RETRIES",
})
_FORCE_ENV = {
    "CLAUDE_AGENT_SDK_CLIENT_APP": "agent-kaizen",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_AUTO_CONNECT_IDE": "false",
    "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
}
_AMBIENT_WORKER_ENV = frozenset({
    # Vendor-owned subscription identity discovery. Kaizen forwards the locations but never opens
    # or interprets credential material beneath them.
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR",
    # Minimal process bootstrap/locale state. PATH and scratch/cache are rebuilt from trusted D-only
    # adapter inputs below rather than inherited from the daemon.
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "OS",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432", "NUMBER_OF_PROCESSORS",
    "LANG", "LC_ALL", "LC_CTYPE", "PYTHONUTF8", "PYTHONIOENCODING",
    # The visible test-extension plane alone may disable provider retries.
    "KAIZEN_TEST_EXTENSION_PROVIDER_RETRIES",
})
_CONTROLLED_WORKER_ENV = frozenset({
    "PATH", "TEMP", "TMP", "TMPDIR", "XDG_CACHE_HOME",
    "KAIZEN_WORKSPACE_ROOT", "KAIZEN_CLAUDE_SESSION_ROOT", "KAIZEN_CLAUDE_CACHE_ROOT",
    "KAIZEN_TEST_EXTENSION_PROVIDER_RETRIES",
})
_CONTROLLED_WORKER_ENV_PREFIXES = ("KAIZEN_FAKE_CLAUDE_",)  # hermetic worker-test seam only


def _safe_worker_error_field(value: Any) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    return value if all(character.isalnum() or character in "._[]:-" for character in value) else None


class ClaudeSdkAdapterError(RuntimeError):
    """Denial-carrying error: `code`/`required_action`/`retryable`/sanitized `field`; `payload()` shape."""
    def __init__(self, code: str, *, required_action: str, retryable: bool = False,
                 field: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.required_action = required_action
        self.retryable = retryable
        self.field = _safe_worker_error_field(field)

    def payload(self) -> dict[str, Any]:
        return {"status": "DENIED", "code": self.code, "retryable": self.retryable,
                "required_action": self.required_action}


def _blocked_worker_env_key(key: str) -> bool:
    """Return whether an environment key belongs to a secret or telemetry credential class."""
    upper = key.upper()
    return (
        upper in _STRIP_ENV_EXACT
        or upper.startswith("OTEL_")
        or "CREDENTIAL" in upper
        or upper.endswith((
            "_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN", "_SESSION_TOKEN", "_BEARER_TOKEN",
            "_PASSWORD", "_SECRET", "_SECRET_KEY", "_TOKEN",
        ))
    )


def _sanitized_worker_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the worker allowlist, strip secrets, apply controlled overrides, then force Kaizen invariants."""
    source = children.compose_child_env()
    folded = {str(key).upper(): str(value) for key, value in source.items()}
    env = {
        key: folded[key]
        for key in _AMBIENT_WORKER_ENV
        if key in folded and not _blocked_worker_env_key(key)
    }
    for key, value in (extra or {}).items():
        canonical = str(key).upper()
        controlled = (
            canonical in _CONTROLLED_WORKER_ENV
            or canonical.startswith(_CONTROLLED_WORKER_ENV_PREFIXES)
        )
        if controlled and not _blocked_worker_env_key(canonical):
            env[canonical] = str(value)
    env.update(_FORCE_ENV)
    return env


def _runtime_command(runtime_root: Path) -> list[str]:
    from ..claude_runtime import resolve_worker_command

    return resolve_worker_command(runtime_root)


class ClaudeSdkAdapter:
    """One owned Node worker and one in-memory SDK streaming session per conversation."""

    def __init__(
        self,
        *,
        engine_name: str = "claude",
        recorder: Callable[[dict[str, Any]], None] | None = None,
        logger: Callable[[str], None] | None = None,
        approval_timeout: float = 300.0,
        spawner: Callable[..., children.OwnedChild] | None = None,
        env: Mapping[str, str] | None = None,
        workspace_root: str | Path | None = None,
        runtime_root: str | Path | None = None,
        worker_command: Sequence[str] | None = None,
        gateway_factory: Callable[..., Any] | None = None,
        apply_recovery_callback_factory: Callable[
            [], Callable[[Mapping[str, Any] | None], bool] | None
        ] | None = None,
        workspace_path_authority: Any | None = None,
    ) -> None:
        """Injection seams (spawner, gateway_factory, apply_recovery_callback_factory, workspace_path_authority); `claude_cli`->`claude` collapse."""
        self.engine_name = "claude" if engine_name in ("claude", "claude_cli") else str(engine_name)
        self._recorder = recorder or (lambda _event: None)
        self._logger = logger or (lambda _message: None)
        self._approval_timeout = float(approval_timeout)
        self._spawner = spawner or children.spawn_owned
        self._env_extra = dict(env or {})
        self._workspace_root = Path(os.path.abspath(workspace_root)) if workspace_root is not None else None
        self._runtime_root = Path(runtime_root).resolve() if runtime_root is not None else None
        self._worker_command = list(worker_command) if worker_command is not None else None
        self._gateway_factory = gateway_factory
        self._apply_recovery_callback_factory = apply_recovery_callback_factory
        self._workspace_path_authority = workspace_path_authority
        self._gateway: Any = None
        self._child: children.OwnedChild | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._write_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._pending_lock = threading.RLock()
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._turn_done = threading.Event()
        self._turn_result: TurnResult | None = None
        self._fatal_code: str | None = None
        self._session_id: str | None = None
        self._worker_session_id = "ws-" + secrets.token_hex(12)
        self._active_turn: str | None = None
        self._seq_by_turn: dict[str, int] = {}
        self._tool_call_ids: set[str] = set()
        self._profile: dict[str, Any] = {}
        self._models: list[dict[str, Any]] = []
        self._delta_cb: Callable[[dict[str, Any]], None] | None = None
        self._approval_cb: Callable[[dict[str, Any]], str] | None = None
        self._approval_broker_cb: ApprovalBrokerCallback | None = None
        self._mutation_guard: MutationGuard | None = None
        self._next_attachments: list[dict[str, Any]] = []
        self._closed = False
        self._stderr_bytes = 0
        self._tool_termination_proven = True
        self._workspace_recovery_paths: tuple[str, ...] = ()

    @property
    def active_turn_id(self) -> str | None:
        with self._state_lock:
            return self._active_turn

    @property
    def workspace_recovery_paths(self) -> tuple[str, ...]:
        with self._state_lock:
            return self._workspace_recovery_paths

    @property
    def models(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._models)

    def bind_session(self, session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ClaudeSdkAdapterError(DENIED_WORKER_PROTOCOL, required_action="bind a valid Kaizen session id")
        self._session_id = session_id

    def probe_features(self) -> tuple[str, ...]:
        """Exercise each advanced worker path independently without starting a model turn."""

        if self._child is None or self._closed:
            return ()
        try:
            prompt_ref = self._stage_runtime_bytes("capability", _CAPABILITY_PROMPT, encoding="utf-8")
            context_ref = self._stage_runtime_bytes("capability", _CAPABILITY_CONTEXT, encoding="utf-8")
            image_ref = self._stage_runtime_bytes(
                "capability", _CAPABILITY_IMAGE, media_type="image/png",
            )
        except Exception:
            return ()
        proven: list[str] = []
        for feature in CAPABILITY_PROBE_FEATURES:
            child = self._child
            if child is None or child.poll() is not None:
                break
            challenge = "cp-" + secrets.token_hex(16)
            body: dict[str, Any] = {"feature": feature, "challenge": challenge}
            if feature == "image_attachments":
                body.update({"prompt_ref": prompt_ref, "image_ref": image_ref})
            elif feature == "governed_context":
                body.update({"prompt_ref": prompt_ref, "context_ref": context_ref})
            try:
                raw = self._request(
                    "capability.probe", session_id=self._worker_session_id, body=body,
                    timeout=_CAPABILITY_PROBE_TIMEOUT_SECONDS,
                )
                result = validate_capability_probe_result(raw, feature=feature, challenge=challenge)
                if feature == "diff_snapshots":
                    expected = f"KAIZEN_CAPABILITY_DIFF:{challenge}"
                    if self._read_runtime_reference(result["artifact_ref"], maximum=1024) != expected:
                        raise WorkerProtocolError(field="capability.artifact_ref")
                    self._discard_runtime_reference(result["artifact_ref"])
                proven.append(feature)
            except (ClaudeSdkAdapterError, WorkerProtocolError, OSError, UnicodeError, ValueError):
                # Advanced capabilities are optional. A malformed, refused, or unavailable probe keeps
                # this exact feature dark while basic authenticated chat remains independently usable.
                continue
        return tuple(proven)

    def refresh_models(self) -> list[dict[str, Any]]:
        """Replace the catalog from the worker's live ``supportedModels()`` result or fail closed."""

        if self._child is None or self._closed or self._child.poll() is not None:
            raise ClaudeSdkAdapterError(
                WORKER_DIED, required_action="restart the Claude capability probe before refreshing models",
            )
        try:
            refreshed = self._request(
                "initialize", session_id=self._worker_session_id, body={}, timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            models = sanitize_model_catalog(refreshed.get("models"))
        except WorkerProtocolError as error:
            raise ClaudeSdkAdapterError(
                error.code, required_action="refresh a valid account model catalog",
                field=error.field,
            ) from error
        self._models = models
        return self.models

    def open(self, profile: Mapping[str, Any], policy_snapshot: Any) -> Mapping[str, Any]:
        """Subscription-only, oauth-proven, exact tool allowlist + effective-model match; spawns worker/reader/stderr threads + gateway; kill()+raise on any failure."""
        if self._child is not None or self._closed:
            raise ClaudeSdkAdapterError(DENIED_WORKER_PROTOCOL, required_action="use a fresh worker adapter")
        auth_mode = profile.get("auth_mode", "subscription")
        if auth_mode != "subscription":
            raise ClaudeSdkAdapterError(DENIED_AUTH_MODE_MISMATCH,
                                        required_action="select the existing subscription authentication mode")
        workspace = self._workspace_root
        if workspace is None:
            raw_workspace = getattr(policy_snapshot, "workspace_root", None)
            if not isinstance(raw_workspace, str) or not raw_workspace:
                raise ClaudeSdkAdapterError(DENIED_WORKER_PROTOCOL,
                                            required_action="provide the immutable workspace policy root")
            workspace = Path(os.path.abspath(raw_workspace))
            self._workspace_root = workspace
        command = self._worker_command
        if command is None:
            runtime = self._runtime_root or workspace
            try:
                command = _runtime_command(runtime)
            except Exception as error:
                raise ClaudeSdkAdapterError(
                    DENIED_SDK_UNAVAILABLE,
                    required_action="install or repair the pinned Claude Agent SDK provider runtime",
                ) from error
        session_root = self._session_runtime_root()
        cache_root = workspace / "AI" / "work" / "orchestration" / "ui-cache"
        temporary = session_root / "temp"
        temporary.mkdir(parents=True, exist_ok=True)
        cache_root.mkdir(parents=True, exist_ok=True)
        command_root = Path(command[0]).expanduser().resolve().parent
        env = _sanitized_worker_env({
            **self._env_extra,
            "PATH": str(command_root),
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "TMPDIR": str(temporary),
            "XDG_CACHE_HOME": str(cache_root),
            "KAIZEN_WORKSPACE_ROOT": str(workspace),
            "KAIZEN_CLAUDE_SESSION_ROOT": str(session_root),
            "KAIZEN_CLAUDE_CACHE_ROOT": str(cache_root),
        })
        try:
            self._child = self._spawner(
                list(command), cwd=str(workspace), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except Exception as error:
            raise ClaudeSdkAdapterError(
                DENIED_SDK_UNAVAILABLE, required_action="repair the owned Node worker runtime",
            ) from error
        process = self._child.process
        if process.stdin is None or process.stdout is None or process.stderr is None:
            self._terminate_unproven()
            raise ClaudeSdkAdapterError(DENIED_WORKER_PROTOCOL,
                                        required_action="repair worker stdio ownership")
        self._reader = threading.Thread(target=self._read_stdout, name="claude-sdk-worker-stdout", daemon=True)
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr, name="claude-sdk-worker-stderr", daemon=True,
        )
        self._reader.start()
        self._stderr_reader.start()
        max_turns = profile.get("max_turns", MAX_TURNS_DEFAULT)
        model = profile.get("model")
        effort = profile.get("reasoning_effort")
        body = {
            "workspace_root": str(workspace),
            "runtime_root": str(self._session_runtime_root()),
            **({"model": model} if model is not None else {}),
            **({"reasoning_effort": effort} if effort is not None else {}),
            "max_turns": max_turns,
            "auth_mode": auth_mode,
            "approval_timeout_seconds": self._approval_timeout,
            "tools": list(TOOL_NAMES),
        }
        try:
            initialized = self._request(
                "initialize", session_id=self._worker_session_id, body=body,
                timeout=_INITIALIZE_TIMEOUT_SECONDS,
            )
            models = sanitize_model_catalog(initialized.get("models"))
            auth_source = initialized.get("auth_source")
            if auth_source != "oauth":
                code = DENIED_AUTH_UNAVAILABLE if auth_source in (None, "") else DENIED_AUTH_MODE_MISMATCH
                raise ClaudeSdkAdapterError(code, required_action="authenticate through the vendor-owned subscription flow")
            if model is not None or effort is not None:
                # validate_profile rejects effort without an explicit model before effective-model
                # comparison, so the comparison below always has a concrete requested model.
                validated = validate_profile(models, model, effort, max_turns, auth_mode)
                effective_model = initialized.get("effective_model", initialized.get("selected_model", model))
                if effective_model != model:
                    raise ClaudeSdkAdapterError(DENIED_MODEL_UNAVAILABLE,
                                                required_action="select the exact account model reported at initialization")
                self._profile = {**validated, "permission_mode": profile.get("permission_mode", "plan")}
            else:
                self._profile = {"auth_mode": auth_mode, "max_turns": max_turns,
                                 "permission_mode": profile.get("permission_mode", "plan")}
            if tuple(initialized.get("tools") or ()) != TOOL_NAMES:
                raise ClaudeSdkAdapterError(DENIED_TOOL_UNSUPPORTED,
                                            required_action="repair the exact Kaizen tool allowlist")
            self._models = models
            self._gateway = self._make_gateway(policy_snapshot)
            return {"status": "OK", "profile": dict(self._profile), "models": self.models,
                    "runtime_kind": "claude-agent-sdk", "runtime_version": initialized.get("runtime_version")}
        except ClaudeSdkAdapterError:
            self.kill()
            raise
        except WorkerProtocolError as error:
            self.kill()
            raise ClaudeSdkAdapterError(
                error.code, required_action="repair the private worker protocol", field=error.field,
            ) from error
        except Exception as error:
            self.kill()
            raise ClaudeSdkAdapterError(DENIED_WORKER_PROTOCOL,
                                        required_action="repair the private worker initialization") from error

    def run_turn(self, prompt: str) -> TurnResult:
        """Single active turn; stages prompt ref, blocks on `_turn_done`; terminal TurnResult (FAILED/WORKER_DIED on missing result)."""
        if self._child is None or self._closed:
            return TurnResult("FAILED", error_code=WORKER_DIED, fatal=True)
        if not isinstance(prompt, str) or not prompt:
            return TurnResult("FAILED", error_code=DENIED_WORKER_PROTOCOL, fatal=True)
        with self._state_lock:
            if self._active_turn is not None:
                return TurnResult("FAILED", error_code="DENIED_SESSION_NOT_IDLE", fatal=False)
            turn_id = "ct-" + secrets.token_hex(12)
            self._active_turn = turn_id
            self._turn_result = None
            self._workspace_recovery_paths = ()
            self._turn_done.clear()
        prompt_ref = self._stage_runtime_bytes("inputs", prompt.encode("utf-8"), encoding="utf-8")
        attachments = self._consume_attachments()
        try:
            self._request(
                "turn.start", session_id=self._worker_session_id, turn_id=turn_id,
                body={"prompt_ref": prompt_ref, "attachments": attachments}, timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            self._turn_done.wait()
            with self._state_lock:
                result = self._turn_result
                fatal = self._fatal_code
            if result is not None:
                return result
            return TurnResult("FAILED", vendor_turn_id=turn_id, error_code=fatal or WORKER_DIED, fatal=True)
        except ClaudeSdkAdapterError as error:
            with self._state_lock:
                result = self._turn_result
                fatal = self._fatal_code
            return result or TurnResult(
                "FAILED", vendor_turn_id=turn_id, error_code=fatal or error.code, fatal=True,
            )
        except WorkerProtocolError as error:
            return TurnResult("FAILED", vendor_turn_id=turn_id, error_code=error.code, fatal=True)
        finally:
            with self._state_lock:
                self._active_turn = None

    def set_next_turn_artifacts(self, attachments: Sequence[Mapping[str, Any]]) -> None:
        staged: list[dict[str, Any]] = []
        for item in attachments:
            worker_ref = item.get("worker_ref") if isinstance(item, Mapping) else None
            if not isinstance(worker_ref, Mapping):
                raise ClaudeSdkAdapterError(DENIED_WORKER_PROTOCOL,
                                            required_action="stage attachment bytes through the host cache")
            staged.append(dict(worker_ref))
        with self._state_lock:
            if self._active_turn is not None:
                raise ClaudeSdkAdapterError(
                    DENIED_WORKER_PROTOCOL,
                    required_action="attach images only while the conversation is idle",
                )
            self._next_attachments = staged

    def clear_next_turn_artifacts(self) -> None:
        """Erase the one-turn envelope after any host-side prelaunch failure."""

        with self._state_lock:
            self._next_attachments = []

    def stage_attachment(self, content: bytes, *, media_type: str) -> dict[str, Any]:
        if not isinstance(content, bytes) or not (1 <= len(content) <= 4 * 1024 * 1024):
            raise ClaudeSdkAdapterError(DENIED_WORKER_OVERSIZE,
                                        required_action="stage an image no larger than 4 MiB")
        if media_type not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            raise ClaudeSdkAdapterError(DENIED_WORKER_PROTOCOL,
                                        required_action="stage a supported image media type")
        return self._stage_runtime_bytes("attachments", content, media_type=media_type)

    def steer(self, text: str) -> Mapping[str, Any]:
        """Queue neutralized steering text for the active turn or return a stable denial map."""
        turn_id = self.active_turn_id
        if turn_id is None:
            return {"status": "DENIED", "code": "DENIED_NO_ACTIVE_TURN"}
        # Vendor @file expansion is not a Kaizen-governed context surface.  Fullwidth @ preserves
        # the visible instruction while ensuring no raw expansion marker reaches the SDK.
        ref = self._stage_runtime_bytes(
            "inputs", str(text).replace("@", "＠").encode("utf-8"), encoding="utf-8",
        )
        self._request("turn.steer", session_id=self._worker_session_id, turn_id=turn_id,
                      body={"prompt_ref": ref})
        return {"status": "OK", "queued": True, "turn_id": turn_id}

    def interrupt(self) -> Mapping[str, Any]:
        """Interrupt the active turn and report whether fallback teardown was proven."""
        turn_id = self.active_turn_id
        if turn_id is None:
            return {"status": "DENIED", "code": "DENIED_NO_ACTIVE_TURN"}
        try:
            self._request("turn.interrupt", session_id=self._worker_session_id, turn_id=turn_id,
                          body={}, timeout=_INTERRUPT_TIMEOUT_SECONDS)
            return {"status": "OK", "interrupted": True, "turn_id": turn_id}
        except Exception:
            tools_proven = self._cancel_gateway_tools()
            worker_proven = self._kill_tree()
            proven = tools_proven and worker_proven
            return {
                "status": "ERROR",
                "code": WORKER_DIED if proven else DENIED_WORKSPACE_RECOVERY_REQUIRED,
                "termination_proven": proven,
            }

    def close(self) -> Mapping[str, Any]:
        """Close the worker fail-closed, reporting status, code, and termination proof."""
        tools_proven = self._cancel_gateway_tools()
        if not tools_proven:
            self._closed = True
            worker_proven = self._kill_tree()
            return {
                "status": "ERROR",
                "closed": True,
                "code": DENIED_WORKSPACE_RECOVERY_REQUIRED,
                "termination_proven": tools_proven and worker_proven,
            }
        if self.active_turn_id is not None:
            return {"status": "DENIED", "code": "DENIED_SESSION_NOT_IDLE", "termination_proven": True}
        if self._child is None:
            self._closed = True
            proven = self._close_gateway()
            return {"status": "OK" if proven else "ERROR", "closed": True,
                    "termination_proven": proven}
        try:
            self._request("session.close", session_id=self._worker_session_id, body={},
                          timeout=_CLOSE_TIMEOUT_SECONDS)
        except Exception:
            pass
        self._closed = True
        proven = self._wait_or_kill(_CLOSE_TIMEOUT_SECONDS)
        proven = self._close_gateway() and proven
        result = {"status": "OK" if proven else "ERROR", "closed": True, "termination_proven": proven}
        if not proven:
            result["code"] = DENIED_WORKSPACE_RECOVERY_REQUIRED
        return result

    def kill(self) -> Mapping[str, Any]:
        """Force terminal teardown and report whether gateway and worker termination were proven."""
        self._closed = True
        tools_proven = self._cancel_gateway_tools()
        worker_proven = self._kill_tree()
        gateway_proven = self._close_gateway() if tools_proven else False
        proven = tools_proven and worker_proven and gateway_proven
        with self._state_lock:
            self._fatal_code = WORKER_DIED
            if self._active_turn is not None and self._turn_result is None:
                self._turn_result = TurnResult("CANCELED", vendor_turn_id=self._active_turn,
                                               error_code=WORKER_DIED, fatal=True)
            self._turn_done.set()
        return {"status": "OK" if proven else "ERROR", "killed": True, "termination_proven": proven}

    def _cancel_gateway_tools(self) -> bool:
        gateway = self._gateway
        cancel = getattr(gateway, "cancel_active_processes", None)
        if not callable(cancel):
            return self._tool_termination_proven
        try:
            outcome = cancel(_CLOSE_TIMEOUT_SECONDS)
            proven = isinstance(outcome, Mapping) and outcome.get("termination_proven") is True
        except Exception:
            proven = False
        with self._state_lock:
            self._tool_termination_proven = self._tool_termination_proven and proven
            return self._tool_termination_proven

    def _close_gateway(self) -> bool:
        close = getattr(self._gateway, "close", None)
        if not callable(close):
            return True
        try:
            return close() is True
        except Exception:
            return False

    def on_approval(self, callback: Callable[[dict[str, Any]], str]) -> None:
        self._approval_cb = callback

    def on_delta(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._delta_cb = callback

    def set_mutation_guard(self, callback: MutationGuard | None) -> None:
        self._mutation_guard = callback
        if self._gateway is not None and hasattr(self._gateway, "set_mutation_guard"):
            self._gateway.set_mutation_guard(callback)

    def set_approval_broker(self, callback: ApprovalBrokerCallback | None) -> None:
        self._approval_broker_cb = callback
        if self._gateway is not None and hasattr(self._gateway, "set_approval_broker"):
            self._gateway.set_approval_broker(callback)

    def _make_gateway(self, snapshot: Any) -> Any:
        if self._gateway_factory is not None:
            gateway = self._gateway_factory(
                workspace_root=self._workspace_root, policy_snapshot=snapshot, spawner=self._spawner,
                approval_timeout=self._approval_timeout,
            )
        else:
            try:
                from ..tool_gateway import ToolGateway

                if snapshot is None or not hasattr(snapshot, "build_engine"):
                    return None
                engine = snapshot.build_engine()

                gateway = ToolGateway(
                    self._workspace_root,
                    decide=lambda action, epoch, authority=engine: authority.decide(action, current_epoch=epoch),
                    approval_broker=lambda request: self._approval_broker_cb(request)
                    if self._approval_broker_cb is not None else {
                        "decision": "denied", "code": "ERROR_TOOL_APPROVAL_TRANSPORT", "fatal": True,
                    },
                    mutation_guard=lambda action: self._mutation_guard(action)
                    if self._mutation_guard is not None else {
                        "status": "DENIED", "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                        "required_action": "bind the daemon writer lease before mutation",
                    },
                    proposal_executor=WorkspaceProposalExecutor(
                        self._workspace_root,
                        path_authority=self._workspace_path_authority,
                    ),
                    recovery_callback_factory=self._apply_recovery_callback_factory,
                    spawner=self._spawner,
                )
            except (ImportError, TypeError, ValueError):
                gateway = None
        if gateway is not None:
            if hasattr(gateway, "set_mutation_guard"):
                gateway.set_mutation_guard(self._mutation_guard)
            if hasattr(gateway, "set_approval_broker"):
                gateway.set_approval_broker(self._approval_broker_cb)
        return gateway

    def _request(self, op: str, *, body: Mapping[str, Any], session_id: str | None = None,
                 turn_id: str | None = None, timeout: float = _REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
        """Correlated request/response over stdio; per-request inbox queue(maxsize=1); timeout->DENIED_WORKER_PROTOCOL; worker-refusal mapping."""
        child = self._child
        if child is None or child.poll() is not None or child.process.stdin is None:
            raise ClaudeSdkAdapterError(WORKER_DIED, required_action="restart the Claude conversation")
        request_id = "rq-" + secrets.token_hex(12)
        frame: dict[str, Any] = {"v": 1, "type": "request", "id": request_id, "op": op, "body": dict(body)}
        if session_id is not None:
            frame["session_id"] = session_id
        if turn_id is not None:
            frame["turn_id"] = turn_id
        inbox: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = inbox
        try:
            encoded = encode_frame(frame).decode("ascii")
            with self._write_lock:
                child.process.stdin.write(encoded)
                child.process.stdin.flush()
            try:
                response = inbox.get(timeout=timeout)
            except queue.Empty as error:
                raise ClaudeSdkAdapterError(DENIED_WORKER_PROTOCOL,
                                            required_action="restart the unresponsive Claude worker",
                                            field=f"{op}.timeout") from error
            if response.get("ok") is not True:
                raw_error = response.get("error")
                code = raw_error.get("code") if isinstance(raw_error, Mapping) else DENIED_WORKER_PROTOCOL
                field = raw_error.get("field") if isinstance(raw_error, Mapping) else None
                raise ClaudeSdkAdapterError(
                    str(code) if isinstance(code, str) else DENIED_WORKER_PROTOCOL,
                    required_action="resolve the worker refusal before retrying",
                    field=field if isinstance(field, str) else None,
                )
            result = response.get("body")
            if not isinstance(result, Mapping):
                raise ClaudeSdkAdapterError(DENIED_WORKER_PROTOCOL, required_action="repair worker response shape")
            return dict(result)
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def _read_stdout(self) -> None:
        """Read bounded worker frames, enforcing session and turn sequencing until terminal EOF."""
        child = self._child
        stream = child.process.stdout if child is not None else None
        if stream is None:
            self._worker_failed(DENIED_WORKER_PROTOCOL, "stdout")
            return
        try:
            while True:
                binary = getattr(stream, "buffer", None)
                if binary is not None:
                    raw_line = binary.readline(MAX_FRAME_BYTES + 2)
                    if raw_line == b"":
                        break
                    if not raw_line.endswith(b"\n") or len(raw_line) > MAX_FRAME_BYTES:
                        raise WorkerProtocolError(DENIED_WORKER_OVERSIZE, "frame")
                    try:
                        line = raw_line.decode("utf-8")
                    except UnicodeDecodeError as error:
                        raise WorkerProtocolError(field="frame.encoding") from error
                else:
                    # Hermetic test streams may expose only the text API; retain the authoritative
                    # encoded-byte ceiling even though production reads the underlying byte buffer.
                    line = stream.readline(MAX_FRAME_BYTES + 2)
                    if line == "":
                        break
                    if not line.endswith("\n") or len(line.encode("utf-8")) > MAX_FRAME_BYTES:
                        raise WorkerProtocolError(DENIED_WORKER_OVERSIZE, "frame")
                frame = decode_frame(line)
                if frame["type"] == "response":
                    with self._pending_lock:
                        inbox = self._pending.get(frame["id"])
                    if inbox is not None:
                        try:
                            inbox.put_nowait(frame)
                        except queue.Full:
                            raise WorkerProtocolError(field="duplicate response")
                else:
                    self._handle_event(frame)
        except WorkerProtocolError as error:
            self._worker_failed(error.code, error.field)
            return
        except Exception:
            self._worker_failed(DENIED_WORKER_PROTOCOL, "stdout")
            return
        if not self._closed:
            self._worker_failed(WORKER_DIED, "stdout_eof")

    def _handle_event(self, frame: Mapping[str, Any]) -> None:
        """Validate and route one sequenced worker event to turn state or tool dispatch."""
        event = str(frame["event"])
        body = dict(frame["body"])
        if frame.get("session_id") != self._worker_session_id:
            raise WorkerProtocolError(field="session_id")
        raw_turn_id = frame.get("turn_id")
        with self._state_lock:
            active_turn = self._active_turn
        if event == "initialized":
            if raw_turn_id is not None or active_turn is not None:
                raise WorkerProtocolError(field="turn_id")
        elif event == "fatal":
            if (active_turn is None and raw_turn_id is not None) \
                    or (active_turn is not None and raw_turn_id != active_turn):
                raise WorkerProtocolError(field="turn_id")
        elif active_turn is None or raw_turn_id != active_turn:
            raise WorkerProtocolError(field="turn_id")
        turn_id = str(raw_turn_id or "session")
        seq = int(frame["seq"])
        previous = self._seq_by_turn.get(turn_id, 0)
        if seq <= previous:
            raise WorkerProtocolError(field="seq")
        self._seq_by_turn[turn_id] = seq
        if event == "delta":
            text = body.get("text")
            if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_DELTA_BYTES:
                raise WorkerProtocolError(DENIED_WORKER_OVERSIZE, "delta")
            callback = self._delta_cb
            if callback is not None:
                callback({"turn_id": turn_id, "text": text})
        elif event == "tool.invoke":
            call_id = body.get("tool_call_id", body.get("call_id"))
            if not isinstance(call_id, str) or not call_id:
                raise WorkerProtocolError(field="call_id")
            with self._state_lock:
                if call_id in self._tool_call_ids:
                    raise WorkerProtocolError(field="duplicate_call_id")
                self._tool_call_ids.add(call_id)
            threading.Thread(target=self._handle_tool, args=(turn_id, body),
                             name="claude-sdk-tool", daemon=True).start()
        elif event == "tool.open":
            self._emit(
                "tool_call",
                "open",
                correlation_id=str(body.get("tool_call_id") or body.get("call_id") or "") or None,
                summary="Claude tool opened",
                payload=self._safe_tool_metadata(body),
            )
        elif event == "tool.close":
            # The SDK worker knows only whether the model-facing result was accepted.  The host
            # emits the authoritative, sanitized close card after its governed gateway returns.
            pass
        elif event == "turn.open":
            self._emit("turn", "open", correlation_id=turn_id, summary="Claude turn opened",
                       payload={"turn_id": turn_id})
        elif event == "turn.result":
            with self._state_lock:
                if self._turn_result is not None and self._turn_result.fatal:
                    return  # a fatal governed tool result already terminalized this turn
            status = str(body.get("status") or ("OK" if body.get("ok") is True else "FAILED")).upper()
            final = body.get("final_text", body.get("result"))
            result_ref = body.get("result_ref")
            reference_failed = False
            if final is None and isinstance(result_ref, Mapping):
                try:
                    final = self._read_runtime_reference(result_ref, maximum=MAX_FRAME_BYTES)
                except Exception:
                    status = "FAILED"
                    final = None
                    reference_failed = True
            code = body.get("error_code", body.get("code"))
            num_turns = body.get("num_turns")
            configured_max = self._profile.get("max_turns")
            if (isinstance(num_turns, bool) or not isinstance(num_turns, int) or num_turns < 0
                    or isinstance(configured_max, bool) or not isinstance(configured_max, int)):
                status = "FAILED"
                final = None
                code = DENIED_WORKER_PROTOCOL
                fatal = True
            elif num_turns > configured_max:
                # Independent host-side ceiling check: a compromised or regressed worker cannot turn an
                # over-budget provider result into a resumable conversation.
                status = "FAILED"
                final = None
                code = MODEL_CALL_BUDGET_EXHAUSTED
                fatal = True
            elif reference_failed:
                code = DENIED_WORKER_PROTOCOL
                fatal = True
            else:
                fatal = body.get("fatal") is True or code == MODEL_CALL_BUDGET_EXHAUSTED
                if fatal and code == MODEL_CALL_BUDGET_EXHAUSTED:
                    status = "FAILED"
            result = TurnResult(status, vendor_turn_id=turn_id,
                                final_text=final if isinstance(final, str) else None,
                                error_code=code if isinstance(code, str) else None,
                                fatal=fatal)
            marker = "close_ok" if status == "OK" else "close_canceled" if status == "CANCELED" else "close_fail"
            self._emit("turn", marker, correlation_id=turn_id, summary=f"Claude turn {status.lower()}",
                       payload={"status": status, "num_turns": num_turns}, code=result.error_code)
            with self._state_lock:
                self._tool_call_ids.clear()
                self._turn_result = result
                self._turn_done.set()
        elif event == "fatal":
            self._worker_failed(str(body.get("code") or WORKER_DIED), body.get("field"))

    def _handle_tool(self, turn_id: str, body: Mapping[str, Any]) -> None:
        """Deduplicate and dispatch one worker tool request on a daemon thread."""
        name = body.get("name")
        call_id = body.get("tool_call_id", body.get("call_id"))
        args = body.get("args", body.get("input"))
        if name == "kaizen_propose_changes" and isinstance(args, Mapping):
            try:
                args = self._materialize_proposal_refs(args)
            except Exception:
                args = None
        if name not in TOOL_NAMES or not isinstance(call_id, str) or not call_id or not isinstance(args, Mapping):
            result: Mapping[str, Any] = {"status": "DENIED", "code": DENIED_TOOL_UNSUPPORTED,
                                         "fatal": True}
        elif self._gateway is None:
            result = {"status": "DENIED", "code": DENIED_TOOL_UNSUPPORTED, "fatal": True}
        else:
            try:
                execute = getattr(self._gateway, "execute", None)
                if callable(execute):
                    from .. import policy
                    from ..tool_gateway import ToolContext

                    value = execute(
                        str(name), dict(args),
                        ToolContext(
                            policy.Actor("claude", self._session_id or "unbound", 0, turn_id),
                            0,
                            call_id,
                        ),
                    )
                else:
                    value = self._gateway.invoke(str(name), dict(args), correlation_id=call_id)
                result = value if isinstance(value, Mapping) else {
                    "status": "DENIED", "code": DENIED_TOOL_UNSUPPORTED, "fatal": True,
                }
            except Exception:
                result = {"status": "DENIED", "code": DENIED_TOOL_UNSUPPORTED, "fatal": True}
        payload = dict(result)
        status = str(payload.get("status") or "DENIED").upper()
        code = payload.get("code") if isinstance(payload.get("code"), str) else None
        safe_result = self._safe_tool_result_metadata(payload)
        recovery_paths = staged_cleanup_paths(safe_result)
        if recovery_paths:
            with self._state_lock:
                self._workspace_recovery_paths = recovery_paths
        persistence_required = safe_result.get("partial_apply") is True \
            or safe_result.get("mismatch_evidence_uncertain") is True \
            or code == DENIED_WORKSPACE_RECOVERY_REQUIRED
        try:
            self._emit(
                "tool_call",
                "close_ok" if status == "OK" else "close_fail",
                correlation_id=call_id if isinstance(call_id, str) else None,
                summary=f"Claude tool {status.lower()}",
                payload={"name": name, "tool_call_id": call_id, "result": safe_result},
                code=code,
                persistence_required=persistence_required,
            )
        except Exception:
            payload = {
                "status": "DENIED",
                "code": DENIED_WORKSPACE_RECOVERY_REQUIRED,
                "fatal": True,
            }
            status = "DENIED"
            code = DENIED_WORKSPACE_RECOVERY_REQUIRED
        authoritative_fatal = payload.get("fatal") is True
        authoritative_code = code or DENIED_TOOL_UNSUPPORTED
        if authoritative_fatal:
            # Terminalize before the callback round trip: a fast worker cannot race a later success over
            # this authoritative governed failure. Encoding and staging are also fallible and therefore
            # happen only after the durable close and authoritative terminal result exist.
            self._terminalize_tool_failure(turn_id, authoritative_code)
        try:
            encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            if len(encoded) > _TOOL_RESULT_INLINE_BYTES:
                payload = {"status": payload.get("status"), "result_ref": self._stage_runtime_bytes(
                    "tool-results", encoded, encoding="utf-8",
                ), "fatal": authoritative_fatal}
        except Exception:  # noqa: BLE001 -- callback staging cannot erase durable governed truth
            fallback_code = authoritative_code if authoritative_fatal else DENIED_WORKER_PROTOCOL
            payload = {"status": "DENIED", "code": fallback_code, "fatal": True}
            if not authoritative_fatal:
                self._terminalize_tool_failure(turn_id, fallback_code)
        try:
            ok = payload.get("status") == "OK"
            self._request(
                "tool.result", session_id=self._worker_session_id, turn_id=turn_id,
                body={"call_id": call_id, "ok": ok, "result" if ok else "error": payload},
            )
        except Exception:
            if not authoritative_fatal:
                self._worker_failed(DENIED_WORKER_PROTOCOL)
            return

    def _terminalize_tool_failure(self, turn_id: str, code: str) -> None:
        """Make a fatal governed tool result authoritative before later worker output can race it."""

        with self._state_lock:
            if self._active_turn != turn_id:
                return
            self._fatal_code = code
            if self._turn_result is None:
                self._turn_result = TurnResult(
                    "FAILED", vendor_turn_id=turn_id, error_code=code, fatal=True,
                )
            self._turn_done.set()

    def _worker_failed(self, code: str, field: Any = None) -> None:
        if not self._cancel_gateway_tools():
            code = DENIED_WORKSPACE_RECOVERY_REQUIRED
        with self._state_lock:
            self._fatal_code = code
            if self._active_turn is not None and self._turn_result is None:
                self._turn_result = TurnResult("FAILED", vendor_turn_id=self._active_turn,
                                               error_code=code, fatal=True)
            self._turn_done.set()
        with self._pending_lock:
            pending = list(self._pending.values())
        safe_field = _safe_worker_error_field(field)
        error_body = {"code": code, **({"field": safe_field} if safe_field is not None else {})}
        failure = {"v": 1, "type": "response", "id": "worker-failed", "ok": False,
                   "error": error_body}
        for inbox in pending:
            try:
                inbox.put_nowait(failure)
            except queue.Full:
                pass

    def _materialize_proposal_refs(self, args: Mapping[str, Any]) -> dict[str, Any]:
        clean = dict(args)
        changes = clean.get("changes")
        if not isinstance(changes, list):
            raise WorkerProtocolError(field="proposal.changes")
        materialized: list[dict[str, Any]] = []
        total = 0
        for raw in changes:
            if not isinstance(raw, Mapping):
                raise WorkerProtocolError(field="proposal.change")
            change = dict(raw)
            reference = change.pop("content_ref", None)
            if reference is not None:
                text = self._read_runtime_reference(reference, maximum=8 * 1024 * 1024)
                total += len(text.encode("utf-8"))
                if total > 64 * 1024 * 1024:
                    raise WorkerProtocolError(DENIED_WORKER_OVERSIZE, "proposal.changes")
                change["content"] = text
            materialized.append(change)
        clean["changes"] = materialized
        return clean

    def _read_runtime_reference(self, raw: Any, *, maximum: int) -> str:
        """Runtime-root containment + per-component symlink/reparse guard + size + sha256 verify; UTF-8 text. Security-critical."""
        reference = validate_reference(raw)
        if reference["root"] != "runtime" or reference["bytes"] > maximum:
            raise WorkerProtocolError(DENIED_WORKER_OVERSIZE, "reference")
        path = self._resolve_runtime_reference_path(reference["path"])
        content = path.read_bytes()
        if len(content) != reference["bytes"] or hashlib.sha256(content).hexdigest() != reference["sha256"]:
            raise WorkerProtocolError(field="reference.sha256")
        return content.decode("utf-8")

    def _discard_runtime_reference(self, raw: Any) -> None:
        reference = validate_reference(raw)
        if reference["root"] != "runtime":
            raise WorkerProtocolError(field="reference.root")
        target = self._resolve_runtime_reference_path(reference["path"])
        target.unlink()

    def _resolve_runtime_reference_path(self, relative_path: str) -> Path:
        """Resolve an in-runtime path only after rejecting every symlink/reparse component."""
        root = self._session_runtime_root()
        current = root
        for part in Path(relative_path).parts:
            current = current / part
            if current.is_symlink() or bool(getattr(os.lstat(current), "st_file_attributes", 0) & 0x400):
                raise WorkerProtocolError(field="reference.path")
        target = (root / relative_path).resolve(strict=True)
        target.relative_to(root)
        return target

    def _drain_stderr(self) -> None:
        child = self._child
        stream = child.process.stderr if child is not None else None
        if stream is None:
            return
        # This reader thread is the sole writer; close/kill reads only after joining it.
        for line in stream:
            self._stderr_bytes = min(64 * 1024 + 1, self._stderr_bytes + len(line.encode("utf-8")))
        if self._stderr_bytes:
            self._logger(f"Claude worker stderr suppressed ({min(self._stderr_bytes, 64 * 1024)} bytes)")

    def _session_runtime_root(self) -> Path:
        raw = getattr(self, "_kaizen_vendor_runtime", None)
        root = Path(raw).resolve() if raw else None
        if root is None:
            workspace = self._workspace_root
            if workspace is None:
                raise ClaudeSdkAdapterError(DENIED_WORKER_PROTOCOL, required_action="bind a D-only runtime root")
            root = (workspace / "AI" / "work" / "orchestration" / "runtime" / "claude-worker-test").resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _stage_runtime_bytes(self, category: str, content: bytes, **metadata: Any) -> dict[str, Any]:
        """Content-addressed atomic staging (temp+fsync+os.replace), idempotent on existing digest, containment check; reference metadata."""
        digest = hashlib.sha256(content).hexdigest()
        root = self._session_runtime_root()
        directory = root / category
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / digest
        if target.parent != directory:
            raise ClaudeSdkAdapterError(DENIED_WORKER_PROTOCOL, required_action="repair runtime containment")
        if target.exists():
            landed = target.read_bytes()
            if hashlib.sha256(landed).hexdigest() != digest:
                raise ClaudeSdkAdapterError(DENIED_WORKER_PROTOCOL, required_action="repair corrupt runtime input")
        else:
            partial = directory / f".{digest}.{secrets.token_hex(8)}.part"
            with partial.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(partial, target)
        value = {"root": "runtime", "path": f"{category}/{digest}", "sha256": digest, "bytes": len(content)}
        value.update(metadata)
        return value

    def _consume_attachments(self) -> list[dict[str, Any]]:
        with self._state_lock:
            value, self._next_attachments = self._next_attachments, []
            return value

    def _wait_or_kill(self, timeout: float) -> bool:
        child = self._child
        if child is None:
            return True
        try:
            child.process.wait(timeout=timeout)
            child.release()
            self._close_child_streams(child)
            self._child = None
            return True
        except subprocess.TimeoutExpired:
            return self._kill_tree()

    def _kill_tree(self) -> bool:
        child = self._child
        if child is None:
            return True
        try:
            child.kill_tree(timeout=_CLOSE_TIMEOUT_SECONDS)
            proven = child.poll() is not None
        except Exception:
            proven = False
        if proven:
            self._close_child_streams(child)
            self._child = None
        return proven

    def _close_child_streams(self, child: children.OwnedChild) -> None:
        current = threading.current_thread()
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread is not current:
                thread.join(timeout=1.0)
        for stream in (child.process.stdin, child.process.stdout, child.process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass

    def _terminate_unproven(self) -> None:
        if not self._kill_tree():
            with self._state_lock:
                self._fatal_code = WORKER_DIED

    @staticmethod
    def _safe_tool_metadata(body: Mapping[str, Any]) -> dict[str, Any]:
        value = {
            key: body[key]
            for key in ("name", "tool_call_id", "call_id", "status", "code")
            if key in body
        }
        tool_input = body.get("input")
        if body.get("name") == "kaizen_run_process" and isinstance(tool_input, Mapping):
            for key in ("executable", "argv", "cwd", "timeout_ms"):
                if key in tool_input:
                    value[key] = tool_input[key]
        elif body.get("name") == "kaizen_read_file" and isinstance(tool_input, Mapping):
            # Durable proof may retain the governed workspace-relative locator and numeric bounds,
            # never the file bytes returned to the model.
            for key in ("path", "start_line", "end_line", "max_bytes"):
                if key in tool_input:
                    value[key] = tool_input[key]
        return value

    @staticmethod
    def _safe_tool_result_metadata(result: Mapping[str, Any]) -> dict[str, Any]:
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
        for key in ("path", "sha256", "start_line", "end_line", "total_lines"):
            if key in source:
                safe[key] = source[key]
        stdout = source.get("stdout")
        if isinstance(stdout, str):
            encoded = stdout.encode("utf-8")
            safe.update({"stdout_sha256": hashlib.sha256(encoded).hexdigest(), "stdout_bytes": len(encoded)})
        safe.update(normalize_apply_evidence(source))
        return safe

    def _emit(self, event_kind: str, marker: str, *, summary: str,
              correlation_id: str | None = None, payload: Mapping[str, Any] | None = None,
              code: str | None = None, persistence_required: bool = False) -> None:
        event: dict[str, Any] = {"event_kind": event_kind, "marker": marker, "summary": summary,
                                "payload": dict(payload or {})}
        if correlation_id is not None:
            event["correlation_id"] = correlation_id
        if code is not None:
            event["code"] = code
        if persistence_required:
            event["persistence_required"] = True
        try:
            self._recorder(event)
        except Exception:
            self._logger("Claude SDK recorder failure")
            if persistence_required:
                raise RuntimeError("critical recorder failure") from None


def probe_capability(
    workspace_root: str | Path,
    *,
    runtime_root: str | Path | None = None,
    worker_command: Sequence[str] | None = None,
    spawner: Callable[..., children.OwnedChild] | None = None,
    env: Mapping[str, str] | None = None,
    refresh_models: bool = False,
    logger: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Initialize without a prompt, return a sanitized dynamic catalog, then prove teardown."""

    workspace = Path(workspace_root).resolve()
    adapter = ClaudeSdkAdapter(
        workspace_root=workspace, runtime_root=runtime_root, worker_command=worker_command,
        spawner=spawner, env=env, logger=logger,
    )
    setattr(adapter, "_kaizen_vendor_runtime", str(
        workspace / "AI" / "work" / "orchestration" / "runtime" / "capability-cache" / "claude-sdk-probe"
    ))
    try:
        opened = adapter.open({"auth_mode": "subscription", "permission_mode": "plan"}, None)
        models = adapter.refresh_models() if refresh_models else opened["models"]
        probed_features = adapter.probe_features()
        closed = adapter.close()
        if closed.get("termination_proven") is not True:
            raise ClaudeSdkAdapterError(WORKER_DIED, required_action="repair Claude worker teardown")
        return {
            "id": "claude", "label": "Claude", "drivable": True,
            "availability": {"state": "available", "code": None, "message": ""},
            "models": models, "default_model": None, "default_reasoning_effort": None,
            "auth_modes": ["subscription"], "permission_modes": ["plan", "ask", "agent", "full"],
            "runtime": {"kind": "claude-agent-sdk", "version": opened.get("runtime_version"),
                        "status": "ready"},
            "max_turns": {"min": 1, "default": MAX_TURNS_DEFAULT, "max": 32},
            "_subscription_auth_proven": True,
            "_probed_features": list(probed_features),
        }
    except ClaudeSdkAdapterError as error:
        adapter.kill()
        if logger is not None:
            logger(f"Claude capability probe denied code={error.code} field={error.field or 'unknown'}")
        return unavailable_capability(error.code, error.field)


def unavailable_capability(code: str = DENIED_SDK_UNAVAILABLE, field: str | None = None) -> dict[str, Any]:
    """Path-free fail-closed descriptor for a missing or invalid provider package."""
    message = (
        "Claude subscription initialization timed out before account and model discovery."
        if field == "initialize_timeout"
        else "Claude subscription runtime is not ready."
    )
    return {
        "id": "claude", "label": "Claude", "drivable": False,
        "availability": {"state": "auth_required" if code == DENIED_AUTH_UNAVAILABLE else "unavailable",
                         "code": code, "message": message},
        "models": [], "default_model": None, "default_reasoning_effort": None,
        "auth_modes": ["subscription"], "permission_modes": ["plan", "ask", "agent", "full"],
        "runtime": {"kind": "claude-agent-sdk", "version": None, "status": "unavailable"},
        "max_turns": {"min": 1, "default": MAX_TURNS_DEFAULT, "max": 32},
        "warnings": [],
    }


__all__ = [
    "ClaudeSdkAdapter", "ClaudeSdkAdapterError", "probe_capability", "unavailable_capability", "_sanitized_worker_env",
]
