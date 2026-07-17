"""Supervisor daemon runtime (v8 M1, plan §3.3 / §A.3 / §5.3).

The runtime behind ``kaizen.py daemon run``. It owns vendor/tool children truthfully
on Windows + POSIX and keeps the agent-run ledger honest across crashes:

- Single-instance: a pidfile + start-nonce under ``AI/work/orchestration/runtime/``.
  A second ``daemon run`` whose pidfile names a live process refuses.
- Loopback control channel (loopback.py): per-OS owner-only IPC; ``daemon status`` is
  a loopback client.
- Child ownership (children.py): Windows Job Object KILL_ON_JOB_CLOSE / POSIX process
  group -> a daemon close reaps the whole tree. THE invariant is the boot orphan-sweep;
  the Job Object is an optimization.
- Boot orphan-sweep: for every non-terminal run this workspace owns whose recorded
  pid+start-nonce is dead, truthfully force-finalize (T8 ``canceled``, dangling spans
  closed via the finalize escape hatch). Also runs ``git worktree prune``.
- Heartbeat + parent-liveness (ppid poll) + exponential backoff on the reconnect loop.
- Event funnel: normalized ``event_kind``x``marker`` appended via the agent_runs entry
  points CONNECT-PER-CALL (no standing kaizen.db handle); a reducer snapshot per run
  every 50 events.

STDOUT is pristine: every log line goes to stderr and a file under the runtime dir,
never stdout, so a future adapter can hand a child's stdout straight through.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import agent_runs
from ..denials import KaizenDenied
from ..paths import REPO_ROOT, WORK_ROOT
from ..redaction import assert_redacted
from ..session_protocol import (
    SessionProtocolError,
    canonical_snippet,
    canonical_title,
    pack_session_policy_snapshot,
    parse_feature_flags,
    read_resume_metadata,
    read_session_client_features,
    validate_context_refs,
    validate_durable_context_refs,
    validate_image_refs,
    validate_resume_metadata,
)
from . import children, loopback, policy
from .artifact_cache import ArtifactCache
from .approvals import ApprovalBroker
from .continuation import CONTINUATION_MAX_BYTES, ContinuationTooLarge, build_continuation_prompt
from .claude_worker_protocol import MODEL_CATALOG_TTL_SECONDS
from .diff_snapshots import DiffSnapshotError, DiffSnapshotManager
from .modes import dist_mode
from .session_drive import (
    DRIVEN_APPROVAL_TIMEOUT_DEFAULT,
    SESSION_LONG_POLL_MAX_S,
    DrivenSession,
    TurnReservation,
    TURN_IDLE,
    TURN_RUNNING,
    TURN_TERMINAL,
    WriterClaimBinding,
    run_driven_turn,
)
from .session_artifacts import (
    MaterializedTurn,
    SessionArtifactError,
    SessionArtifactMaterializer,
    compose_governed_prompt,
)
from .writer_lease import OwnershipRegistry, OwnershipStateError, WorkspaceWriterLease, WriterClaim
from .workspace_path_authority import WorkspacePathAuthority, WorkspacePathError

# Runtime dir lives inside gitignored AI/work, never a redirected TEMP (paths.py
# rewrites TEMP/TMP at import; the token/pidfile must not follow it -- ledger #19/§D).
RUNTIME_DIR = WORK_ROOT / "orchestration" / "runtime"
PIDFILE = RUNTIME_DIR / "supervisor.pid"
TOKENFILE = RUNTIME_DIR / "control.token"
OWNERSHIP_FILE = RUNTIME_DIR / "owned_runs.json"
LOGFILE = RUNTIME_DIR / "supervisor.log"

SNAPSHOT_EVERY = 50  # Reducer snapshot cadence in normalized events per agent run.
_HEARTBEAT_SECONDS = 2.0
_BACKOFF_BASE = 0.5
_BACKOFF_MAX = 30.0
_SOURCE_WRITER_MODES = frozenset({"ask", "agent", "full"})
_LOOPBACK_RESPONSE_BUDGET = loopback.MAX_LINE_BYTES - 4096
_SESSION_LIST_RUN_LIMIT = 200
_PIDFILE_MAX_BYTES = 64 * 1024  # Maximum serialized private pidfile bytes.
_TOKENFILE_MAX_BYTES = 1024  # Maximum serialized loopback-token bytes.
_LOGFILE_MAX_BYTES = 4 * 1024 * 1024  # Bounded supervisor log size in bytes.
_LOG_TRUNCATION_MARKER = b"[older supervisor log bytes truncated at the bounded file limit]\n"
_USER_INPUT_MAX_QUESTIONS = 32
_USER_INPUT_BODY_MAX_BYTES = 64 * 1024
_USER_INPUT_ANSWER_MAX_BYTES = 64 * 1024
_USER_INPUT_ANSWERS_MAX_BYTES = 256 * 1024
_USER_INPUT_MAX_OPTIONS = 20
_CLAUDE_MAX_TURNS_CEILING = 32
_CLAUDE_DEFAULT_MAX_TURNS = 8


def _bounded_user_input_text(value: Any, max_bytes: int, *, required: bool) -> str | None:
    """Return bounded Unicode text without NUL/surrogates, or None when it violates the approval-card contract."""
    if not isinstance(value, str) or required and not value.strip():
        return None
    if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        return None
    return value if len(value.encode("utf-8")) <= max_bytes else None


def _sanitize_user_input_questions(value: Any) -> list[dict[str, Any]] | None:
    """Normalize the bounded requestUserInput card schema persisted for the approval UI."""
    if not isinstance(value, list) or not value or len(value) > _USER_INPUT_MAX_QUESTIONS:
        return None
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            return None
        question_id = _bounded_user_input_text(raw.get("id"), 256, required=True)
        question = _bounded_user_input_text(raw.get("question"), 4096, required=True)
        if question_id is None or question is None or question_id in seen:
            return None
        seen.add(question_id)
        clean: dict[str, Any] = {"id": question_id, "question": question}
        if "header" in raw:
            header = _bounded_user_input_text(raw.get("header"), 256, required=False)
            if header is None:
                return None
            clean["header"] = header
        raw_options = raw.get("options")
        if raw_options is not None:
            if not isinstance(raw_options, list) or len(raw_options) > _USER_INPUT_MAX_OPTIONS:
                return None
            options: list[dict[str, str]] = []
            for raw_option in raw_options:
                if not isinstance(raw_option, Mapping):
                    return None
                label = _bounded_user_input_text(raw_option.get("label"), 256, required=True)
                if label is None:
                    return None
                option = {"label": label}
                if "description" in raw_option:
                    description = _bounded_user_input_text(raw_option.get("description"), 4096, required=False)
                    if description is None:
                        return None
                    option["description"] = description
                options.append(option)
            if options:
                clean["options"] = options
        questions.append(clean)
    encoded = json.dumps({"questions": questions}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return questions if len(encoded) <= _USER_INPUT_BODY_MAX_BYTES else None


def _validated_user_input_answers(record: Mapping[str, Any], value: Any) -> dict[str, str] | None:
    """Validate exact free-text answer ids and bounded text against a persisted requestUserInput card."""
    active = record.get("active")
    body = active.get("body") if isinstance(active, Mapping) else None
    questions = body.get("questions") if isinstance(body, Mapping) else None
    clean_questions = _sanitize_user_input_questions(questions)
    if clean_questions is None:
        return None
    free_text_ids = {
        question["id"] for question in clean_questions if not question.get("options")
    }
    if not free_text_ids:
        return {} if value is None or value == {} else None
    if not isinstance(value, Mapping) or set(value) != free_text_ids:
        return None
    answers: dict[str, str] = {}
    total = 0
    for question_id, raw_answer in value.items():
        answer = _bounded_user_input_text(raw_answer, _USER_INPUT_ANSWER_MAX_BYTES, required=True)
        if answer is None:
            return None
        total += len(question_id.encode("utf-8")) + len(answer.encode("utf-8"))
        if total > _USER_INPUT_ANSWERS_MAX_BYTES:
            return None
        answers[str(question_id)] = answer
    return answers

# M-CLAUDE (M5b) hooked governor. The ONLY surface `daemon hooks install` writes is the workspace-local
# .claude/settings.local.json (NEVER ~/.claude, C:\Program Files\ClaudeCode, or .vscode/settings.json).
# The shim path is relative-from-repo-root so the written command is portable across clones.
CLAUDE_LOCAL_SETTINGS_REL = (".claude", "settings.local.json")
HOOK_SHIM_REL = ("kaizen_components", "orchestration", "claude_hook_shim.py")
# A stable marker the install stamps into each hook entry so verify/remove can identify OUR entries
# without disturbing any hand-authored hooks the user added to the same file.
_HOOK_MARKER = "kaizen-governor"


# H2.1 canonical engine ids on the wire + UI (plan "Canonical engine names"). The internal registry
# lane key stays `claude_cli`; normalization to `claude` happens at THIS boundary only. WIRE_ENGINES is
# the public order the capabilities response emits; ENGINE_ALIASES maps input aliases -> the wire id.
WIRE_ENGINES: tuple[str, ...] = ("local_llm", "codex", "claude")
ENGINE_ALIASES: dict[str, str] = {"claude_cli": "claude"}

# Ollama tags probe timeout (seconds) for the local_llm model catalog. Short + bounded: a missing
# server degrades the lane rather than blocking the capabilities response.
_OLLAMA_TAGS_TIMEOUT = 2.0
_CLAUDE_PROVIDER_RUNTIME_ENV = "KAIZEN_CLAUDE_PROVIDER_RUNTIME_ROOT"

# The normal local lane is Kaizen code, not an opaque vendor runtime. These exact capabilities are
# therefore proven by the fixed in-process implementation boundary: the local adapter emits ordered
# deltas, the supervisor materializes governed context, and the provider-neutral ToolGateway owns
# proposals plus the canonical controlled-tool/direct-argv process surfaces. Keep this map exact: an
# absent, extra, or changed claim stays dark. Ollama vision remains deferred.
_LOCAL_LLM_CODE_FEATURE_EVIDENCE: dict[str, str] = {
    "streaming": "local-llm-adapter-ordered-delta",
    "governed_context": "supervisor-session-artifact-materializer",
    "diff_snapshots": "tool-gateway-workspace-proposal-executor",
    "controlled_tools": "tool-gateway-canonical-tool-set",
    "process_execution": "tool-gateway-direct-argv-process",
}


@dataclass(frozen=True)
class _ClaudeProviderTarget:
    """Frozen (node_exe, worker.js) command pair + integrity fingerprint for a validated off-system-drive Claude provider runtime."""
    command: tuple[str, str]
    fingerprint: str


def _validated_claude_provider_target(repo_root: Path) -> _ClaudeProviderTarget | None:
    """Freeze one fully validated provider version target; session cache/runtime stay workspace-local."""
    from . import claude_runtime

    raw = os.environ.get(_CLAUDE_PROVIDER_RUNTIME_ENV)
    candidate = Path(raw).expanduser() if raw else repo_root
    if not candidate.is_absolute():
        return None
    candidate = candidate.resolve()
    if os.name == "nt":
        system_drive = os.environ.get("SystemDrive", "").rstrip("\\/").casefold()
        if not candidate.drive or (system_drive and candidate.drive.rstrip("\\/").casefold() == system_drive):
            return None
    try:
        validated = claude_runtime.validate_runtime(
            candidate, source_root=claude_runtime.source_package_root(),
        )
        target = Path(validated["target"]).resolve()
        integrity = validated["integrity"]
        node = Path(str(integrity["node_executable"])).resolve()
        worker = (target / "dist" / "worker.js").resolve()
        if target not in worker.parents or not worker.is_file() or not node.is_file():
            return None
        pointer, integrity = validated["pointer"], validated["integrity"]
        fingerprint = hashlib.sha256(json.dumps(
            {"pointer": pointer, "integrity": integrity}, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()
        return _ClaudeProviderTarget((str(node), str(worker)), fingerprint)
    except (OSError, KeyError, TypeError, claude_runtime.ClaudeRuntimeError):
        return None


def _validated_claude_provider_command(repo_root: Path) -> list[str] | None:
    """Expose the validated command list as a narrow provider-validation test seam."""

    target = _validated_claude_provider_target(repo_root)
    return list(target.command) if target is not None else None


@dataclass(frozen=True)
class _SessionPolicyGate:
    """One authenticated Codex hook identity bound to one immutable session snapshot."""

    gate_id: str
    profile_hash: str
    agent_run_id: str
    session_id: str
    snapshot: policy.PolicySnapshot


@dataclass(frozen=True)
class _DurableContinuationHistory:
    """Rehydrated durable messages, dropped-invalid count, expired-artifact descriptors — payload of `_driven_continuation_history`."""
    messages: tuple[dict[str, Any], ...]
    invalid_message_count: int
    expired_artifacts: tuple[dict[str, Any], ...]


class _BufferedRecorder:
    """Hold adapter preflight events until a durable T5 recorder exists.

    Vendor ``open`` validates launcher, credentials, protocol/version, sandbox, and hook loading before
    C1/T5 creation. It may also emit harmless lifecycle diagnostics. This sink keeps those events out of
    the ledger until the run exists, then flushes them behind the mandatory first ``profile/point`` event.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._sink: Callable[[dict[str, Any]], None] | None = None

    def __call__(self, event: dict[str, Any]) -> None:
        with self._lock:
            if self._sink is None:
                self._events.append(dict(event))
                return
            self._sink(event)

    def flush_to(self, sink: Callable[[dict[str, Any]], None]) -> None:
        # Named flush_to (not bind): the structural control-ingress guard treats any ``.bind(`` code
        # line as a listener construct.
        with self._lock:
            self._sink = sink
            pending, self._events = self._events, []
            for event in pending:
                sink(event)


class _VendorPreflightRefused(Exception):
    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__(str(response.get("code") or "vendor preflight refused"))
        self.response = response


class _MutationInterceptionUnavailable(RuntimeError):
    """A production adapter could not install the mandatory final mutation gate."""


class _ApprovalBrokerUnavailable(RuntimeError):
    """A production adapter could not install the mandatory approval broker."""


def ensure_runtime_dir() -> Path:
    """Creates/returns the gitignored runtime dir; idempotent."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    return RUNTIME_DIR


def normalize_engine(engine: str | None) -> str:
    """Map an input engine id (incl. the `claude_cli` alias) to its canonical wire id. Non-alias ids
    pass through unchanged; the boundary NEVER emits `claude_cli`."""
    e = str(engine or "").strip()
    return ENGINE_ALIASES.get(e, e)


def _registered_engines() -> list[str]:
    """Return the frozen three-entry internal adapter registry (not plugin discovery)."""

    from .adapters import ADAPTER_CONSTRUCTORS

    return sorted(ADAPTER_CONSTRUCTORS)


class SingleInstanceError(Exception):
    """A live daemon already owns this workspace's pidfile."""

    def __init__(self, pid: int, nonce: str) -> None:
        super().__init__(f"daemon already running: pid={pid} nonce={nonce}")
        self.pid = pid
        self.nonce = nonce


class _Namespace(argparse.Namespace):
    """Minimal Namespace builder for the agent_runs entry points (they take argparse
    Namespaces; we build them in code so the funnel never shells out to kaizen.py)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        for key, value in kwargs.items():
            setattr(self, key, value)


class Supervisor:
    """In-process supervisor. Drivable directly (``boot`` / ``spawn_child`` / ``shutdown``)
    so tests never background ``kaizen.py daemon run``; the CLI wiring is a thin loop."""

    def __init__(self, repo_root: Path | None = None) -> None:
        """Enumerate injectable test seams (`_adapter_factory`, `_dispatch_runner`, `_clock`, `_sleep`, `_driven_test_records`)."""
        self.repo_root = Path(os.path.abspath(repo_root or REPO_ROOT))
        self.runtime_dir = self.repo_root / "AI" / "work" / "orchestration" / "runtime"
        self.nonce = secrets.token_hex(16)
        self.token = secrets.token_hex(32)
        self._log_lock = threading.RLock()
        self._children: dict[str, children.OwnedChild] = {}
        self._event_counts_lock = threading.RLock()
        self._event_counts: dict[str, int] = {}
        self._loopback: loopback.LoopbackServer | None = None
        self._stop = threading.Event()
        self._parent_ppid = os.getppid()
        self._booted = False
        self.policy: policy.PolicyEngine | None = None
        # v8 M9 fleet: the daemon-exclusive fleet.db handle, constructed at boot ONLY when distribution
        # is on. Off mode => stays None and no fleet.db file is ever created (off-unchanged invariant).
        self._fleet: Any = None
        self._dist_mode = "off"
        # v8 M11 HTTP control service (plan §C.2). Constructed at boot ONLY when the fleet is up AND
        # KAIZEN_CONTROL_BIND is set; otherwise stays None (no server, no listening socket).
        self._control: Any = None
        # v8 M14 dispatch executor runner (plan §B.5). DEFAULT None ⇒ the executor skips execution and
        # fail_dispatch("no runner configured") TRUTHFULLY. A real deployment wires an adapter runner
        # here (task, workdir) -> {artifact_text, branch?}; tests inject one. NO background thread: the
        # poll is loopback/CLI-driven (deterministic).
        self._dispatch_runner: Any = None
        # H0 driven-session spine. _driven maps agent_run_id -> DrivenSession (the live turn thread +
        # approval waiters). _driven_lock guards the registry across the loopback handler threads and the
        # turn threads. _adapter_factory is a test seam mirroring _dispatch_runner: DEFAULT None ⇒ a REAL
        # LocalLLMAdapter (default_chat_provider, lazy real Ollama); tests inject a factory returning an
        # adapter over a scripted provider. _clock/_sleep are injectable so session/events long-poll is
        # deterministic under test (default: real time).
        self._driven: dict[str, Any] = {}
        self._driven_lock = threading.RLock()
        self._resume_locks: dict[str, threading.RLock] = {}
        self._resume_locks_guard = threading.RLock()
        self._adapter_factory: Any = None
        # Private verification seam: live smoke rows can be is_test-marked and exact-id cleaned without
        # adding a public session/start wire field. Production remains False.
        self._driven_test_records = False
        self._clock: Any = None
        self._sleep: Any = None
        self._approval_before_park: Callable[[dict[str, Any]], None] | None = None
        # H2.5 observed Claude lane. A vendor session id maps to one durable C1 envelope and the active
        # linked T5 host lifecycle. SessionEnd finalizes that T5; a later SessionStart/resume opens a new
        # linked T5 while reusing C1. DB lookup reconstructs both maps after daemon restart. The lock spans
        # lookup, dedupe, and append so concurrent loopback hook deliveries retain one total order.
        self._hook_sessions: dict[str, str] = {}
        self._hook_runs: dict[str, str] = {}
        self._hook_runs_lock = threading.RLock()
        # H2.1 session/capabilities cache. Built on first request (or boot) and reused; refresh:true or a
        # boot rebuilds it. Guarded by a lock (loopback handler threads may request concurrently). None ⇒
        # not yet built.
        self._capabilities: list[dict[str, Any]] | None = None
        self._capabilities_built_at = 0.0
        self._capabilities_lock = threading.RLock()
        self._claude_provider_target: _ClaudeProviderTarget | None = None
        self._claude_provider_target_frozen = False
        self._claude_provider_target_lock = threading.RLock()
        # H2.3 Codex PreToolUse defense-in-depth gates. The loopback token authenticates transport;
        # gate_id + profile_hash select exactly one frozen policy snapshot. Reverse ownership makes every
        # close/kill/finalize/start-failure/shutdown path able to unregister without trusting adapter state.
        self._session_policy_gates: dict[str, _SessionPolicyGate] = {}
        self._policy_gate_by_run: dict[str, str] = {}
        self._session_policy_gates_lock = threading.RLock()
        # P0 writer serialization. The in-memory slot is authoritative while this daemon lives; its
        # reserved OWNERSHIP_FILE marker exists only to make pre-record crashes recoverable.
        ownership_file = self.repo_root / "AI" / "work" / "orchestration" / "runtime" / "owned_runs.json"
        self._ownership_registry = OwnershipRegistry(ownership_file, workspace_root=self.repo_root)
        self._instance_lock: Any = None
        try:
            self._runtime_relative = ownership_file.parent.relative_to(self.repo_root)
            self._instance_lock_relative = (self._runtime_relative / "supervisor.lock").as_posix()
            self._pidfile_relative = (self._runtime_relative / "supervisor.pid").as_posix()
            self._tokenfile_relative = (self._runtime_relative / "control.token").as_posix()
            self._logfile_relative = (self._runtime_relative / "supervisor.log").as_posix()
            self._ownership_registry.authority.ensure_directory(self._runtime_relative.as_posix())
            self._pidfile_bytes: bytes | None = None
            self._tokenfile_bytes: bytes | None = None
            self._writer_lease = WorkspaceWriterLease(
                self._ownership_registry, pid=os.getpid(), nonce=self.nonce, clock=agent_runs.now,
                workspace_root=self.repo_root,
            )
            self._artifact_cache = ArtifactCache(self.repo_root, logger=self.log)
            self._session_artifacts = SessionArtifactMaterializer(self.repo_root, self._artifact_cache)
            self._diff_snapshots = DiffSnapshotManager(
                self.repo_root,
                self._artifact_cache,
                workspace_path_authority=self._ownership_registry.authority,
            )
            self._approved_broker_payloads: dict[tuple[str, str], Any] = {}
            self._approved_broker_payloads_lock = threading.RLock()
            self._approval_broker = ApprovalBroker(release_waiter=self._release_broker_waiter)
        except BaseException:
            self._ownership_registry.close()
            raise

    # --- logging (stderr + file; NEVER stdout) ----------------------------

    def log(self, message: str) -> None:
        line = f"{agent_runs.now()} {message}"
        try:
            with self._log_lock:
                authority = self._ownership_registry.authority
                with authority.exclusive():
                    previous = self._read_private_runtime(
                        self._logfile_relative, _LOGFILE_MAX_BYTES,
                    )
                    line_bytes = (line + "\n").encode("utf-8", errors="replace")
                    tail_limit = _LOGFILE_MAX_BYTES - len(_LOG_TRUNCATION_MARKER)
                    if len(line_bytes) > tail_limit:
                        line_bytes = line_bytes[-tail_limit:].decode("utf-8", errors="ignore").encode("utf-8")
                    prior_bytes = previous.data if previous is not None else b""
                    payload = prior_bytes + line_bytes
                    if len(payload) > _LOGFILE_MAX_BYTES:
                        tail = payload[-tail_limit:].decode("utf-8", errors="ignore").encode("utf-8")
                        payload = _LOG_TRUNCATION_MARKER + tail
                    authority.atomic_replace(
                        self._logfile_relative,
                        payload,
                        expected=previous,
                        max_bytes=_LOGFILE_MAX_BYTES,
                    )
        except (OSError, WorkspacePathError):
            pass
        # stderr keeps stdout pristine for adapter stdio pass-through.
        print(line, file=sys.stderr, flush=True)

    # --- single-instance --------------------------------------------------

    def _read_private_runtime(self, relative: str, maximum: int) -> Any | None:
        """Read an identity-gated private runtime file, returning None when absent."""
        authority = self._ownership_registry.authority
        if authority.identity(relative) is None:
            return None
        return authority.read(relative, maximum)

    def _publish_private_runtime(self, relative: str, data: bytes, maximum: int) -> None:
        """Atomically publish private runtime bytes and verify the exact read-back."""
        authority = self._ownership_registry.authority
        previous = self._read_private_runtime(relative, maximum)
        authority.atomic_replace(relative, data, expected=previous, max_bytes=maximum)
        published = authority.read(relative, maximum)
        if published.data != data:
            raise WorkspacePathError("private daemon runtime publication could not be verified")

    def _remove_private_runtime(self, relative: str, data: bytes, maximum: int) -> bool:
        """Unlink a private runtime file only when its exact bytes still match."""
        return self._ownership_registry.authority.unlink_exact(
            relative,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            max_bytes=maximum,
        )

    def _read_pidfile(self) -> tuple[int, str] | None:
        stable = self._read_private_runtime(self._pidfile_relative, _PIDFILE_MAX_BYTES)
        if stable is None:
            return None
        try:
            data = json.loads(stable.data.decode("utf-8"))
            return int(data["pid"]), str(data["nonce"])
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            return None

    def _read_pidfile_for_claim(self) -> tuple[int, str] | None:
        """Read claim state, translating unsafe pidfiles into a locked denial."""
        try:
            return self._read_pidfile()
        except (OSError, WorkspacePathError):
            raise SingleInstanceError(0, "locked") from None

    def _claim_single_instance(self) -> None:
        if self._instance_lock is not None:
            return
        try:
            instance_lock = self._ownership_registry.authority.acquire_process_lock(
                self._instance_lock_relative,
            )
        except (OSError, WorkspacePathError):
            existing = self._read_pidfile_for_claim()
            pid, nonce = existing if existing is not None else (0, "locked")
            raise SingleInstanceError(pid, nonce) from None
        pidfile_bytes: bytes | None = None
        tokenfile_bytes: bytes | None = None
        try:
            existing = self._read_pidfile_for_claim()
            if existing is not None:
                pid, nonce = existing
                if pid != os.getpid() and children.pid_alive(pid):
                    raise SingleInstanceError(pid, nonce)
            pidfile_bytes = json.dumps({
                "pid": os.getpid(), "nonce": self.nonce, "started_at": agent_runs.now(),
            }).encode("utf-8")
            self._publish_private_runtime(
                self._pidfile_relative, pidfile_bytes, _PIDFILE_MAX_BYTES,
            )
            tokenfile_bytes = self.token.encode("utf-8")
            self._publish_private_runtime(
                self._tokenfile_relative, tokenfile_bytes, _TOKENFILE_MAX_BYTES,
            )
        except BaseException:
            if tokenfile_bytes is not None:
                try:
                    self._remove_private_runtime(
                        self._tokenfile_relative, tokenfile_bytes, _TOKENFILE_MAX_BYTES,
                    )
                except (OSError, WorkspacePathError):
                    pass
            if pidfile_bytes is not None:
                try:
                    self._remove_private_runtime(
                        self._pidfile_relative, pidfile_bytes, _PIDFILE_MAX_BYTES,
                    )
                except (OSError, WorkspacePathError):
                    pass
            instance_lock.close()
            raise
        self._pidfile_bytes = pidfile_bytes
        self._tokenfile_bytes = tokenfile_bytes
        self._instance_lock = instance_lock

    def _release_single_instance(self) -> None:
        # Exact-byte unlinking is sufficient ownership proof even if pidfile decoding now fails.
        for relative, data, maximum in (
            (self._tokenfile_relative, self._tokenfile_bytes, _TOKENFILE_MAX_BYTES),
            (self._pidfile_relative, self._pidfile_bytes, _PIDFILE_MAX_BYTES),
        ):
            if data is None:
                continue
            try:
                self._remove_private_runtime(relative, data, maximum)
            except (OSError, WorkspacePathError):
                pass
        self._pidfile_bytes = None
        self._tokenfile_bytes = None
        instance_lock, self._instance_lock = self._instance_lock, None
        if instance_lock is not None:
            try:
                instance_lock.close()
            except Exception:
                pass

    # --- ownership registry (pid+start-nonce per owned run) ---------------

    def _load_ownership(self) -> dict[str, dict[str, Any]]:
        try:
            return self._ownership_registry.runs()
        except (OwnershipStateError, OSError) as error:
            self.log(f"ownership registry unreadable: {type(error).__name__}")
            return {}

    def _record_ownership(self, agent_run_id: str) -> None:
        self._ownership_registry.record_run(agent_run_id, pid=os.getpid(), nonce=self.nonce)

    def _clear_ownership(self, agent_run_id: str) -> None:
        self._ownership_registry.clear_run(agent_run_id)
        with self._event_counts_lock:
            self._event_counts.pop(agent_run_id, None)

    # --- one workspace-writer lease -----------------------------------------------------------

    def _acquire_writer_claim(self, permission_mode: str, *, session_id: str | None = None,
                              agent_run_id: str | None = None) -> tuple[WriterClaim | None, dict[str, Any] | None]:
        if permission_mode not in _SOURCE_WRITER_MODES and permission_mode != "plan":
            return None, {
                "status": "DENIED", "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED", "retryable": False,
                "required_action": "repair the invalid permission mode before acquiring a writer lease",
            }
        return self._writer_lease.acquire(
            permission_mode, session_id=session_id, agent_run_id=agent_run_id,
        )

    def _release_claim(self, claim_id: str | None, *, termination_proven: bool = True) -> bool:
        released = self._writer_lease.release(claim_id, termination_proven=termination_proven)
        if claim_id is not None and not released and not self._writer_lease.recovery_required:
            self._writer_lease.require_recovery("writer claim release could not be proven")
        return released

    def _kill_adapter_proven(self, adapter: Any) -> bool:
        try:
            result = adapter.kill()
        except Exception:  # noqa: BLE001 -- uncertainty is represented by the retained recovery marker
            self._writer_lease.require_recovery("adapter termination raised before it could be proven")
            return False
        proven = (
            isinstance(result, Mapping) and result.get("status") == "OK"
            and result.get("killed") is True and result.get("termination_proven") is not False
        )
        if not proven:
            self._writer_lease.require_recovery("adapter termination returned without explicit proof")
        return proven

    @staticmethod
    def _adapter_child_proven_dead(adapter: Any) -> bool:
        missing = object()
        child = getattr(adapter, "_child", missing)
        if child is None:
            return True
        if child is missing:
            return False
        poll = getattr(child, "poll", None)
        if not callable(poll):
            return False
        try:
            return poll() is not None
        except Exception:  # noqa: BLE001 -- an unreadable child state is not proof
            return False

    def _close_adapter_proven(self, session: DrivenSession, result: Any) -> bool:
        if not isinstance(result, Mapping) or result.get("status") != "OK" or result.get("closed") is not True:
            return False
        return (
            result.get("termination_proven") is True
            or session.engine == "local_llm"
            or self._adapter_child_proven_dead(session.adapter)
        )

    def _writer_spawner(self, binding: WriterClaimBinding) -> Callable[..., children.OwnedChild]:
        """Wrap vendor spawning with evidence for whichever writer claim is current at spawn time."""

        def spawn(*args: Any, **kwargs: Any) -> children.OwnedChild:
            claim_id = binding.current()
            if claim_id is None:
                return children.spawn_owned(*args, **kwargs)
            self._writer_lease.begin_spawn(claim_id)
            try:
                child = children.spawn_owned(*args, **kwargs)
            except Exception:
                # A spawner may fail after CreateProcess/fork. With no returned handle, absence cannot
                # be proven; retain spawn_pending and require operator/restart reconciliation.
                self._writer_lease.require_recovery("vendor spawn failed with an unknown child outcome")
                raise
            try:
                self._writer_lease.track_spawned_child(claim_id, child.pid)
            except Exception:
                # Best-effort containment does not erase the durable uncertainty marker.
                try:
                    child.kill_tree()
                except Exception:
                    pass
                self._writer_lease.require_recovery("spawned vendor child could not be tracked durably")
                raise
            return child

        return spawn

    def _writer_apply_recovery_factory(
        self,
        binding: WriterClaimBinding,
    ) -> Callable[[], Callable[[Mapping[str, Any] | None], bool] | None]:
        """Snapshot the post-guard claim once; delayed callbacks can never journal a later claim."""

        def factory() -> Callable[[Mapping[str, Any] | None], bool] | None:
            claim_id = binding.current()
            if claim_id is None:
                return None

            def persist(state: Mapping[str, Any] | None, captured: str = claim_id) -> bool:
                return self._writer_lease.set_apply_recovery(captured, state)

            return persist

        return factory

    def _track_current_adapter_child(self, session: DrivenSession) -> dict[str, Any] | None:
        """Attach an already-running persistent vendor child to the session's current writer claim."""

        claim_id = session.writer_claim_token
        if claim_id is None:
            return None
        child = getattr(session.adapter, "_child", None)
        if child is None:
            return None
        pid = getattr(child, "pid", None)
        try:
            self._writer_lease.begin_spawn(claim_id)
            self._writer_lease.track_spawned_child(claim_id, pid)
        except Exception:  # noqa: BLE001 -- the durable marker already records recovery-required
            self._writer_lease.require_recovery("persistent vendor child could not attach to the current claim")
            return self._writer_lease.recovery_denial()
        return None

    def _bind_mutation_guard(self, session: DrivenSession, snapshot: policy.PolicySnapshot) -> bool:
        setter = getattr(session.adapter, "set_mutation_guard", None)
        if not callable(setter):
            return False
        try:
            setter(lambda action, s=session, snap=snapshot: self._guard_session_mutation(s, snap, action))
        except Exception:
            return False
        return True

    def _bind_approval_broker(self, session: DrivenSession) -> bool:
        setter = getattr(session.adapter, "set_approval_broker", None)
        if not callable(setter):
            return False
        try:
            setter(lambda request, s=session: self._broker_adapter_approval(s, request))
        except Exception:
            return False
        return True

    def _bind_delta_stream(self, session: DrivenSession) -> bool:
        """Bind normalized adapter deltas only for an explicitly negotiated, proven feature."""

        if not session.streaming:
            return True
        setter = getattr(session.adapter, "on_delta", None)
        if not callable(setter):
            session.streaming = False
            return True  # optional lane fails closed without denying basic chat

        def sink(event: dict[str, Any]) -> None:
            if not isinstance(event, Mapping):
                return
            turn_id = event.get("turn_id")
            text = event.get("text")
            if isinstance(turn_id, str) and isinstance(text, str) and turn_id and text:
                session.append_delta(turn_id, text)

        try:
            setter(sink)
        except Exception:
            session.streaming = False
            return True
        return True

    def _live_broker_approval_ids(self, *, exclude_run_ids: set[str] | None = None) -> set[str]:
        excluded = exclude_run_ids or set()
        with self._driven_lock:
            sessions = list(self._driven.values())
        live: set[str] = set()
        for session in sessions:
            if session.agent_run_id not in excluded:
                live.update(session.live_approval_ids())
        return live

    def _reconcile_approval_orphans(self, *, exclude_run_ids: set[str] | None = None) -> bool:
        try:
            self._approval_broker.reconcile_orphans(
                self._live_broker_approval_ids(exclude_run_ids=exclude_run_ids),
            )
            return True
        except Exception as error:  # noqa: BLE001 -- writer release must fail closed on DB uncertainty
            self.log(f"approval orphan reconciliation failed: {type(error).__name__}")
            self._writer_lease.require_recovery("approval orphan reconciliation did not commit")
            return False

    def _guard_session_mutation(self, session: DrivenSession, snapshot: policy.PolicySnapshot,
                                action: Any) -> dict[str, Any] | None:
        """Final adapter seam: verify a source claim or acquire Plan's first designated write claim."""

        token = session.writer_claim_token
        if session.permission_mode in _SOURCE_WRITER_MODES:
            return self._writer_lease.verify(token, agent_run_id=session.agent_run_id)
        if session.permission_mode != "plan" or not isinstance(action, policy.RequestedAction) \
                or action.verb != "file_write":
            return {
                "status": "DENIED", "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED", "retryable": False,
                "required_action": "the adapter could not prove a governed Plan designated-root write",
            }
        targets = action.canonical_targets()
        if not targets or not all(
            any(policy.prefix_match(target, root) for root in snapshot.designated_write_roots)
            for target in targets
        ):
            return {
                "status": "DENIED", "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED", "retryable": False,
                "required_action": "Plan writes require canonical targets inside designated roots",
            }
        if token is not None:
            return self._writer_lease.verify(token, agent_run_id=session.agent_run_id)
        claim, denial = self._acquire_writer_claim(
            "plan", session_id=session.session_id, agent_run_id=session.agent_run_id,
        )
        if denial is not None:
            return denial
        if claim is None:  # defensive: acquire returns exactly one of claim/denial
            return self._writer_lease.recovery_denial()
        session.set_writer_claim(claim.claim_id)
        return self._track_current_adapter_child(session)

    def _writer_turn_finally(self, session: DrivenSession, result: Any,
                             termination_proven: bool) -> bool:
        self._diff_snapshots.discard_run(session.agent_run_id)
        with self._approved_broker_payloads_lock:
            self._approved_broker_payloads = {
                key: value for key, value in self._approved_broker_payloads.items()
                if key[0] != session.agent_run_id
            }
        token = session.writer_claim_token
        reconciled = self._reconcile_approval_orphans(exclude_run_ids={session.agent_run_id})
        result_code = result.get("error_code") if isinstance(result, Mapping) \
            else getattr(result, "error_code", None)
        if result_code == "DENIED_WORKSPACE_RECOVERY_REQUIRED":
            if not self._writer_lease.apply_recovery_active:
                raw_paths = getattr(session.adapter, "workspace_recovery_paths", ())
                cleanup_paths = tuple(raw_paths) if isinstance(raw_paths, tuple) \
                    and all(isinstance(path, str) for path in raw_paths) else ()
                self._writer_lease.require_recovery(
                    "staged workspace mutation cleanup could not be proven",
                    cleanup_paths=cleanup_paths,
                )
        if token is None:
            return reconciled and not self._writer_lease.recovery_required
        released = self._release_claim(token, termination_proven=termination_proven and reconciled)
        if released:
            session.clear_writer_claim(token)
        return released

    # --- boot orphan-sweep (THE invariant) --------------------------------

    def boot(self) -> dict[str, Any]:
        """Claim single-instance, run the orphan-sweep, start the loopback channel.
        Returns a summary (transport, swept runs) for status/tests."""
        try:
            self._claim_single_instance()
            return self._boot_claimed()
        except BaseException:
            self.shutdown()
            raise

    def _boot_claimed(self) -> dict[str, Any]:
        """Complete boot after the lifetime lock is held; every exception is cleaned by ``boot``."""

        swept = self.orphan_sweep()
        approvals_reconciled = self._reconcile_approval_orphans()
        writer_reconciled = self._writer_lease.reconcile_after_orphan_sweep(
            children.pid_alive,
            allow_clear=approvals_reconciled,
            blocked_reason="approval orphan reconciliation did not commit",
        )
        if not approvals_reconciled:
            self._writer_lease.require_recovery("approval orphan reconciliation did not commit")
            writer_reconciled = False
        try:
            self._artifact_cache.cleanup(is_session_resumable=self._session_is_resumable)
        except Exception as error:  # noqa: BLE001 -- retention cleanup never blocks daemon activation
            self.log(f"artifact cleanup skipped at boot: {type(error).__name__}")
        self._git_worktree_prune()
        # M3 policy chokepoint: load protected paths + authority rules at daemon start (per contract).
        self.policy = policy.build_engine_from_db()
        # H2.1: build the session/capabilities cache at boot (a live Ollama tags probe, degrading
        # cleanly). refresh:true rebuilds it later.
        with self._capabilities_lock:
            self._capabilities = self._build_capabilities()
            # The boot probe is the cache build.  Timestamp it here so the first client read does not
            # immediately repeat the SDK/catalog probes merely because the constructor sentinel was 0.
            self._capabilities_built_at = time.monotonic()
        # M9 fleet: hold the SINGLE fleet.db handle when distribution is on (D*-ops read via loopback so
        # nothing else opens fleet.db). Off mode constructs nothing (no fleet.db ever created).
        self._boot_fleet()
        self._start_loopback()
        # M11: HTTP control service, only when the fleet is up AND an operator set KAIZEN_CONTROL_BIND.
        control = self._start_control()
        self._booted = True
        self.log(f"booted nonce={self.nonce} transport={self._loopback.transport if self._loopback else None}")
        return {
            "status": "OK",
            "booted": True,
            "nonce": self.nonce,
            "transport": self._loopback.transport if self._loopback else None,
            "swept": swept,
            "approvals_reconciled": approvals_reconciled,
            "writer_reconciled": writer_reconciled,
            "policy_loaded": True,
            "policy": {
                "protected_paths": len(self.policy.protected_paths),
                "rules": len(self.policy.rules),
            },
            "dist_mode": self._dist_mode,
            "fleet": self._fleet is not None,
            "control": control,
        }

    @staticmethod
    def _session_is_resumable(session_id: str) -> bool:
        """A durable driven C1 remains resumable until that conversation record is removed."""

        from .. import db

        try:
            row = db.fetch_one(
                "SELECT 1 FROM agent_sessions WHERE id = ? AND controller = 'kaizen'", (session_id,),
            )
        except Exception:  # noqa: BLE001 -- cleanup treats an uncertain reference as resumable
            return True
        return row is not None

    def _boot_fleet(self) -> None:
        """Construct the daemon-held fleet.db handle when distribution is on. Best-effort: a fleet
        open failure logs and degrades to no-fleet rather than blocking daemon boot (fleet.db is a
        disposable, re-bootstrappable cache)."""
        self._dist_mode = dist_mode()
        if self._dist_mode == "off":
            return
        try:
            from ..fleet import sync as fleet_sync
            from ..fleet.store import FleetStore

            self._fleet = FleetStore(
                sync_url=fleet_sync.sync_url_from_env(),
                auth_token=fleet_sync.sync_auth_token_from_env(),
                logger=self.log,
            )
            self.log(f"fleet.db opened (dist_mode={self._dist_mode} sync={self._fleet.sync.synced})")
        except Exception as error:  # noqa: BLE001 -- fleet is a disposable cache; never block boot
            self._fleet = None
            self.log(f"fleet.db open failed (degrading to no-fleet): {error}")

    def _start_control(self) -> dict[str, Any]:
        """Start the M11 HTTP control service (plan §C.2), but ONLY when the fleet is up AND an operator
        set KAIZEN_CONTROL_BIND. It ensures the node's Ed25519 signing identity, then binds. A
        KaizenDenied from the bind guard (wildcard / off-tailnet) is LOUD but non-fatal: the daemon still
        boots with control degraded to off (the refusal is logged and carried in the boot payload). The
        relay is the in-process loopback HANDLER without a socket hop (the service lives IN this process).

        Boot payload shape: {active: bool, address: "host:port"|None, refused: <code> (present only on a
        bind refusal)}."""
        from ..fleet import identity as fleet_identity
        from ..fleet import net as fleet_net
        from ..fleet.control_http import CONTROL_BIND_ENV, ControlService

        bind = os.environ.get(CONTROL_BIND_ENV)
        if self._fleet is None or not bind:
            return {"active": False, "address": None}
        try:
            identity = fleet_identity.ensure_signing_identity()
            service = ControlService(
                store=self._fleet,
                identity=identity,
                bind=bind,
                relay=self._control_relay,
                tailnet_probe=fleet_net.on_tailnet,
                logger=self.log,
            )
            started = service.start()
            self._control = service
            address = started.get("address")
            self.log(f"control http started at {address}")
            return {"active": True, "address": address}
        except KaizenDenied as denied:
            # A refused bind is loud (log + boot payload) but the daemon keeps booting with no control.
            self.log(f"control http refused ({denied.code}); degrading to no-control")
            self._control = None
            return {"active": False, "address": None, "refused": denied.code}
        except Exception as error:  # noqa: BLE001 -- control is optional; never block daemon boot
            self.log(f"control http failed to start (degrading to no-control): {error}")
            self._control = None
            return {"active": False, "address": None}

    def _control_relay(self, request: dict[str, Any]) -> dict[str, Any]:
        """The in-process relay the control service calls for live steer/cancel. It is the loopback
        HANDLER without a socket hop: the control service runs IN this daemon process, so a relayed op
        goes straight to :meth:`_handle_control`. As of M14 the steer/cancel handlers are LIVE: a steer
        for a known session/run records it; an unknown target answers a truthful structured denial (e.g.
        DENIED_SESSION_NOT_FOUND / DENIED_AGENT_RUN_NOT_FOUND), no longer DENIED_UNKNOWN_OP. The relay
        carries origin_node through in the args so a remote steer is attributable."""
        args = dict(request.get("args") or {})
        origin_node = request.get("origin_node")
        if origin_node and "origin_node" not in args:
            args["origin_node"] = origin_node
        return self._handle_control({"op": request["op"], "args": args})

    def orphan_sweep(self) -> dict[str, Any]:
        """Force-finalize every non-terminal run this workspace owns whose recorded
        pid+start-nonce is dead. pid+start-nonce (never pid alone): a reused pid on a
        different nonce is NOT the old owner (ledger #10, false-orphan guard)."""
        registry = self._load_ownership()
        finalized: list[str] = []
        for agent_run_id, owner in list(registry.items()):
            owner_pid = int(owner.get("pid", 0))
            owner_nonce = str(owner.get("nonce", ""))
            # This process reclaiming its own prior registry (same nonce) never sweeps.
            owner_dead = owner_nonce != self.nonce and not children.pid_alive(owner_pid)
            if not owner_dead:
                continue
            state = self._safe_reduce(agent_run_id)
            if state is None:
                # Run vanished (purged) -> drop the stale ownership entry.
                self._clear_ownership(agent_run_id)
                continue
            if state["terminal"]:
                self._clear_ownership(agent_run_id)
                continue
            self._force_finalize(agent_run_id, state)
            finalized.append(agent_run_id)
            self._clear_ownership(agent_run_id)
        return {"finalized": finalized, "scanned": len(registry)}

    def _safe_reduce(self, agent_run_id: str) -> dict[str, Any] | None:
        try:
            ns = _Namespace(agent_run_id=agent_run_id)
            result = agent_runs.agent_run_inspect(ns)
            return result["state"]
        except Exception:  # noqa: BLE001 -- a missing/purged run reduces to None (drop it)
            return None

    def _force_finalize(self, agent_run_id: str, state: dict[str, Any]) -> None:
        """T8 canceled over the run's immutable history. The finalize escape hatch closes
        every dangling span (open children/approvals) as a recorded leak -- truthful, never
        a silent success."""
        dangling = len(state["open_children"]) + len(state["unresolved_approvals"])
        # This EXACT summary is the resume-eligibility marker (_resume_driven_conversation): only a
        # daemon-death orphan may continue as a new linked leg; other finalizations stay terminal.
        summary = self._ORPHAN_FINALIZE_SUMMARY
        body = f"closed {dangling} dangling span(s) over immutable history" if dangling else ""
        ns = _Namespace(
            agent_run_id=agent_run_id,
            conclusion="canceled",
            finalization_code="ORPHAN_SWEEP_FINALIZED",
            summary=summary,
            body=body,
        )
        agent_runs.agent_run_finalize(ns)
        self.log(f"orphan-sweep finalized {agent_run_id} (dangling={dangling})")

    def _git_worktree_prune(self) -> None:
        """Best-effort ``git worktree prune`` in the boot sweep (ledger #18). Tolerate an
        absent git, a non-repo cwd, or any failure -- never blocks boot."""
        git = shutil.which("git")
        if not git:
            return
        try:
            subprocess.run(
                [git, "worktree", "prune"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    # --- child spawning + event funnel ------------------------------------

    def spawn_child(
        self,
        agent_run_id: str,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdout: Any = subprocess.PIPE,
        stderr: Any = subprocess.PIPE,
    ) -> children.OwnedChild:
        """Spawn an OWNED child bound to ``agent_run_id`` and record ownership so a crash
        makes the run sweepable. Env is composed (§A.3) unless a pre-composed env is given."""
        child = children.spawn_owned(cmd, cwd=cwd, env=env, stdout=stdout, stderr=stderr)
        self._children[agent_run_id] = child
        self._record_ownership(agent_run_id)
        return child

    def funnel_event(
        self,
        agent_run_id: str,
        event_kind: str,
        marker: str,
        *,
        summary: str,
        correlation_id: str | None = None,
        code: str | None = None,
        source_event_id: str | None = None,
        body: str = "",
    ) -> dict[str, Any]:
        """Append one normalized lifecycle event via T6 CONNECT-PER-CALL. Never holds a
        kaizen.db handle. Emits a reducer snapshot to the log every SNAPSHOT_EVERY events."""
        payload = {"event_kind": event_kind, "marker": marker}
        if correlation_id:
            payload["correlation_id"] = correlation_id
        if code:
            payload["code"] = code
        if source_event_id:
            payload["source_event_id"] = source_event_id
        ns = _Namespace(
            agent_run_id=agent_run_id,
            payload_json=json.dumps(payload),
            summary=summary,
            body=body,
        )
        result = agent_runs.agent_event_add(ns)
        with self._event_counts_lock:
            count = self._event_counts.get(agent_run_id, 0) + 1
            self._event_counts[agent_run_id] = count
        if count % SNAPSHOT_EVERY == 0:
            self._snapshot(agent_run_id, count)
        return result

    def _snapshot(self, agent_run_id: str, count: int) -> None:
        state = self._safe_reduce(agent_run_id)
        if state is not None:
            self.log(
                f"snapshot {agent_run_id} @ {count} events: "
                f"open_children={len(state['open_children'])} "
                f"open_turns={len(state['open_turns'])} terminal={state['terminal']}"
            )

    # --- loopback control -------------------------------------------------

    def _start_loopback(self) -> None:
        server = loopback.LoopbackServer(
            self.repo_root, self.runtime_dir, self.token, self._handle_control
        )
        server.start()
        self._loopback = server

    def _handle_control(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if op == "status":
            return self.status_payload()
        if op == "ping":
            return {"status": "OK", "pong": True, "nonce": self.nonce}
        if op == "shutdown":
            self._stop.set()
            return {"status": "OK", "shutting_down": True}
        if op in (
            "fleet/node-register", "fleet/heartbeat", "fleet/digest", "fleet/stats",
            "fleet/coordinator", "fleet/lease", "fleet/dispatch", "fleet/reconcile", "fleet/metrics",
        ):
            return self._handle_fleet(op, request.get("args") or {})
        # M14 loopback control ops (§B.5 / plan §D control verbs -- the F4 prerequisite). Each is a thin
        # in-process handler; origin_node (when the control-http relay set it) is carried through.
        if op == "steer":
            return self._handle_steer(request.get("args") or {})
        if op == "cancel":
            return self._handle_cancel(request.get("args") or {})
        if op == "approve":
            return self._handle_approve(request.get("args") or {})
        if op == "attach":
            return self._handle_attach(request.get("args") or {})
        if op == "dispatch/poll":
            return self._handle_dispatch_poll(request.get("args") or {})
        if op == "orchestrate":
            return self._handle_orchestrate(request.get("args") or {})
        # H0 driven-session ops (§app-server): the daemon DRIVES a local-LLM turn under the ledger and
        # streams T6 events back. session/start gates the engine BEFORE any C1/T5 write; the rest proxy
        # the live adapter / read the run's event stream.
        if op == "session/capabilities":
            return self._handle_session_capabilities(request.get("args") or {})
        if op == "session/policy-check":
            return self._handle_session_policy_check(request.get("args") or {})
        if op == "session/start":
            return self._handle_session_start(request.get("args") or {})
        if op == "session/turn":
            return self._handle_session_turn(request.get("args") or {})
        if op == "session/close":
            return self._handle_session_close(request.get("args") or {})
        if op == "session/list":
            return self._handle_session_list(request.get("args") or {})
        if op == "session/events":
            return self._handle_session_events(request.get("args") or {})
        if op == "session/steer":
            return self._handle_session_steer(request.get("args") or {})
        if op == "session/interrupt":
            return self._handle_session_interrupt(request.get("args") or {})
        if op == "session/kill":
            return self._handle_session_kill(request.get("args") or {})
        # M-CLAUDE (M5b) hooked-governor ops. install/verify/remove manage the workspace-local
        # .claude/settings.local.json hooks block; decide routes a live hook payload through the policy
        # engine (the shim's loopback call). These are daemon control ops, excluded from op-coverage.
        if op in ("hooks/install", "hooks/verify", "hooks/remove", "hooks/decide", "hooks/record"):
            return self._handle_hooks(op, request.get("args") or {})
        return {"status": "DENIED", "code": "DENIED_UNKNOWN_OP",
                "op": op, "required_action": "use status|ping|shutdown|fleet/*|steer|cancel|approve|attach|dispatch/poll|orchestrate|session/*|hooks/*"}

    def _handle_fleet(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """Serve a fleet op against the daemon-held handle. The daemon is the SOLE fleet.db opener, so
        D*-ops route here rather than opening a second handle (failure register #21)."""
        if self._fleet is None:
            return {"status": "DENIED", "code": "DENIED_FLEET_NOT_ACTIVE",
                    "required_action": "distribution is off or fleet.db failed to open on this daemon"}
        try:
            if op == "fleet/node-register":
                return self._fleet.register_node(
                    args.get("role"),
                    tailnet_name=args.get("tailnet_name"),
                    capabilities=args.get("capabilities"),
                )
            if op == "fleet/heartbeat":
                return self._fleet.heartbeat()
            if op == "fleet/digest":
                return self._fleet.digest(limit=args.get("limit"))
            if op == "fleet/coordinator":
                return self._handle_fleet_coordinator(args)
            if op == "fleet/lease":
                return self._handle_fleet_lease(args)
            if op == "fleet/dispatch":
                return self._handle_fleet_dispatch(args)
            if op == "fleet/reconcile":
                return self._handle_fleet_reconcile(args)
            if op == "fleet/metrics":
                # M17 built-in metrics over the daemon-held handle (stats() and metrics stay
                # daemon-only surfaces -- §5.3 M17; pure projections, appends nothing).
                from ..fleet import metrics

                return metrics.fleet_metrics(self._fleet)
            return self._fleet.stats()
        except KaizenDenied as denied:
            # A grantor/holder refusal is a structured DENIED payload, not a daemon error.
            return denied.payload()
        except Exception as error:  # noqa: BLE001 -- a fleet op error must not kill the accept loop
            return {"status": "ERROR", "code": "ERROR_FLEET_OP", "op": op, "message": str(error)}

    def _handle_fleet_coordinator(self, args: dict[str, Any]) -> dict[str, Any]:
        """D4 against the daemon-held handle: claim | transfer | release the coordinator role (M10a)."""
        from ..fleet import coordination

        action = args.get("action")
        summary = args.get("summary")
        if action == "claim":
            return coordination.claim_coordinator(self._fleet, mode=args.get("mode", "roaming"), summary=summary)
        if action == "transfer":
            return coordination.transfer_coordinator(self._fleet, args.get("to_node"), summary=summary)
        if action == "release":
            return coordination.release_coordinator(self._fleet, summary=summary)
        return {"status": "DENIED", "code": "DENIED_COORDINATOR_ACTION_REQUIRED",
                "required_action": "action must be claim|transfer|release"}

    def _handle_fleet_lease(self, args: dict[str, Any]) -> dict[str, Any]:
        """D5 against the daemon-held handle: request | grant | renew | release | handoff (M10a shadow)."""
        from ..fleet import coordination
        from ..paths import REPO_ROOT as _REPO_ROOT

        action = args.get("action")
        scope_key = args.get("scope_key")
        summary = args.get("summary")
        ttl = int(args["ttl_s"]) if args.get("ttl_s") is not None else coordination.DEFAULT_TTL_S
        if action == "request":
            return coordination.request_lease(self._fleet, scope_key, mode=args.get("mode", "advisory"), ttl_s=ttl, summary=summary)
        if action == "grant":
            return coordination.grant_lease(self._fleet, scope_key, args.get("holder"), ttl_s=ttl, summary=summary)
        if action == "renew":
            return coordination.renew_lease(self._fleet, scope_key, ttl_s=ttl, summary=summary)
        if action == "release":
            return coordination.release_lease(self._fleet, scope_key, summary=summary)
        if action == "handoff":
            artifact_dir = self.repo_root_work_handoff_dir()
            engine = coordination.HandoffEngine(
                self._fleet, str(_REPO_ROOT), allow_wip_commit=False, allow_push=False, artifact_dir=str(artifact_dir)
            )
            return engine.shadow_handoff(scope_key)
        return {"status": "DENIED", "code": "DENIED_LEASE_ACTION_REQUIRED",
                "required_action": "action must be request|grant|renew|release|handoff"}

    def _handle_fleet_dispatch(self, args: dict[str, Any]) -> dict[str, Any]:
        """D7 against the daemon-held handle: request | accept | start | complete | fail | cancel |
        apply | list a remote dispatch (M14, plan §B.5). apply reads the local C4 approval via
        session_records (kaizen.db) -- the laptop's approval gates the apply into its own worktree."""
        from ..fleet import dispatch_remote

        action = args.get("action")
        scope_key = args.get("scope_key")
        dispatch_id = args.get("dispatch_id")
        summary = args.get("summary")
        if action == "request":
            return dispatch_remote.request_dispatch(
                self._fleet, target_node=args.get("target_node"), task=args.get("task"),
                scope_key=scope_key, required_leases=args.get("required_leases"), summary=summary,
            )
        if action == "accept":
            return dispatch_remote.accept_dispatch(self._fleet, dispatch_id, summary=summary)
        if action == "start":
            return dispatch_remote.start_dispatch(self._fleet, dispatch_id, summary=summary)
        if action == "complete":
            return dispatch_remote.complete_dispatch(
                self._fleet, dispatch_id, artifact_name=args.get("artifact_name"),
                artifact_sha=args.get("artifact_sha"), branch=args.get("branch"), summary=summary,
            )
        if action == "fail":
            return dispatch_remote.fail_dispatch(self._fleet, dispatch_id, args.get("reason") or "unspecified", summary=summary)
        if action == "cancel":
            return dispatch_remote.cancel_dispatch(self._fleet, dispatch_id, reason=args.get("reason"), summary=summary)
        if action == "apply":
            from ..fleet.records import _local_approval_lookup

            return dispatch_remote.apply_dispatch(
                self._fleet, None, dispatch_id, artifact_path=args.get("artifact_path"),
                approval_id=args.get("approval_id"), approvals_lookup=_local_approval_lookup,
                repo_root=str(self.repo_root), summary=summary,
            )
        if action == "list":
            return dispatch_remote.list_dispatches(self._fleet)
        return {"status": "DENIED", "code": "DENIED_DISPATCH_ACTION_REQUIRED",
                "required_action": "action must be request|accept|start|complete|fail|cancel|apply|list"}

    def _handle_fleet_reconcile(self, args: dict[str, Any]) -> dict[str, Any]:
        """D9 against the daemon-held handle: reconcile | sweep | status (M15, plan §B.6). Routes the same
        wire args to the reconcile engine over the daemon-held store. A git_runner is built ONLY when the
        operator gave an explicit hub_remote; allow_publish stays False (owner-gated -- no daemon path
        enables publication at M15)."""
        from ..fleet import coordination, reconcile
        from ..paths import REPO_ROOT as _REPO_ROOT

        action = args.get("action") or "reconcile"
        if action == "status":
            return {"status": "OK", **reconcile.offline_status(self._fleet)}
        if action == "sweep":
            kwargs: dict[str, Any] = {}
            if args.get("stale_after_s") is not None:
                kwargs["stale_after_s"] = float(args["stale_after_s"])
            if args.get("skew_margin_s") is not None:
                kwargs["skew_margin_s"] = float(args["skew_margin_s"])
            return reconcile.sweep_stale_nodes(self._fleet, **kwargs)
        if action == "reconcile":
            hub_remote = args.get("hub_remote")
            git_runner = coordination._default_git_runner(str(_REPO_ROOT)) if hub_remote else None
            return reconcile.reconcile(self._fleet, git_runner, hub_remote=hub_remote, allow_publish=False)
        return {"status": "DENIED", "code": "DENIED_RECONCILE_ACTION_REQUIRED",
                "required_action": "action must be reconcile|sweep|status"}

    # --- M14 loopback control ops (steer/cancel/approve/attach) + dispatch executor -------------

    def _my_node_id(self) -> str | None:
        """This daemon's fleet node id when distribution is on, else None (off-mode has no node)."""
        if self._fleet is None:
            return None
        return self._fleet.node_id

    def _handle_steer(self, args: dict[str, Any]) -> dict[str, Any]:
        """Record a steer instruction (§B.5). session_id present ⇒ append a session-scoped C2
        user_instruction; agent_run_id present ⇒ ALSO funnel a transport/point agent event on the run
        (the live steer surfaces on the run's timeline). An unknown run/session surfaces a STRUCTURED
        denial (this is what the updated control_http supervisor-wiring test asserts -- the real handler
        answers a truthful unknown-target denial, no longer DENIED_UNKNOWN_OP)."""
        instruction = args.get("instruction")
        session_id = args.get("session_id")
        agent_run_id = args.get("agent_run_id")
        if not instruction:
            return {"status": "DENIED", "code": "DENIED_STEER_INSTRUCTION_REQUIRED",
                    "required_action": "steer needs an instruction"}
        if not (session_id or agent_run_id):
            return {"status": "DENIED", "code": "DENIED_STEER_TARGET_REQUIRED",
                    "required_action": "steer needs a session_id or agent_run_id"}
        recorded: dict[str, Any] = {"status": "OK", "steered": True}
        try:
            if session_id:
                from .. import session_records

                ns = _Namespace(session_id=session_id, body=instruction,
                                summary=f"steer: {instruction[:80]}", payload_json=None,
                                payload_json_file=None, test=False)
                result = session_records.instruction_add(ns)
                recorded["instruction_id"] = result["id"]
                recorded["session_id"] = session_id
            if agent_run_id:
                # A live steer surfaces on the run timeline as a transport/point event (no span opened).
                self.funnel_event(agent_run_id, "transport", "point",
                                  summary=f"steer: {instruction[:80]}")
                recorded["agent_run_id"] = agent_run_id
        except KaizenDenied as denied:
            # Unknown run/session (or a fenced-session refusal) surfaces truthfully as its structured
            # denial -- the relay seam reaches the real handler and gets a real answer.
            return denied.payload()
        except Exception as error:  # noqa: BLE001 -- a steer error must not kill the accept loop
            return {"status": "ERROR", "code": "ERROR_STEER", "message": str(error)}
        return recorded

    def _handle_cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        """Truthful-cancel a run (§B.5: never pretend-resume). If this daemon owns a LIVE child for the
        run: kill_tree + T8 force-finalize (conclusion 'canceled', dangling spans closed as leak). Else
        if the run exists and is non-terminal: force-finalize truthfully. Reports what ACTUALLY happened
        (killed_child + finalized), never a fabricated resume."""
        agent_run_id = args.get("agent_run_id")
        if not agent_run_id:
            return {"status": "DENIED", "code": "DENIED_CANCEL_RUN_REQUIRED",
                    "required_action": "cancel needs an agent_run_id"}
        state = self._safe_reduce(agent_run_id)
        if state is None:
            return {"status": "DENIED", "code": "DENIED_AGENT_RUN_NOT_FOUND",
                    "agent_run_id": agent_run_id, "required_action": "no such run on this node"}
        killed_child = False
        child = self._children.get(agent_run_id)
        if child is not None:
            try:
                child.kill_tree()
                killed_child = True
            except Exception:  # noqa: BLE001 -- a kill failure still force-finalizes truthfully
                pass
            self._children.pop(agent_run_id, None)
            self._clear_ownership(agent_run_id)
        if state["terminal"]:
            # Already terminal: nothing to finalize (a child we just killed of a terminal run is a leak
            # we still report, but T8 refuses a second finalize -- so we do not call it).
            return {"status": "OK", "agent_run_id": agent_run_id, "killed_child": killed_child,
                    "finalized": False, "already_terminal": True, "terminal_state": state["terminal_state"]}
        try:
            self._force_finalize(agent_run_id, state)
        except KaizenDenied as denied:
            return denied.payload()
        return {"status": "OK", "agent_run_id": agent_run_id, "killed_child": killed_child,
                "finalized": True, "conclusion": "canceled"}

    def _release_broker_waiter(self, session_id: str, agent_run_id: str,
                               correlation_id: str, decision: Any) -> bool:
        """Release the exact waiter; stash a rich approved decision for approve-before-park recovery, and discard a non-approved request's staged diff snapshot."""

        session = self._get_driven(agent_run_id)
        if session is None or session.session_id != session_id:
            return False
        approval_id = session.live_approval_id(correlation_id)
        released = session.resolve_waiter(correlation_id, decision)
        normalized = getattr(decision, "decision", decision)
        if not released and normalized == "approved":
            with self._approved_broker_payloads_lock:
                self._approved_broker_payloads[(agent_run_id, correlation_id)] = decision
        if normalized != "approved" and approval_id is not None:
            self._diff_snapshots.discard(approval_id)
        return released

    def _session_write_fence(self, session_id: str) -> tuple[str, str | None, int | None] | None:
        """Returns `(session_id, this-node, node_epoch)` write fence; None off-mode or when the session is unfenced (owning_node NULL)."""
        node_id = self._my_node_id()
        if node_id is None:
            return None
        from .. import db

        row = db.fetch_one("SELECT owning_node, node_epoch FROM agent_sessions WHERE id = ?", (session_id,))
        if row is None or row[0] is None:
            return None
        return (session_id, node_id, int(row[1]) if row[1] is not None else None)

    def _ensure_diff_writer_claim(self, session: DrivenSession) -> dict[str, Any] | None:
        """Hold the workspace writer lease before approval-time target rehash."""

        token = session.writer_claim_token
        if token is not None:
            return self._writer_lease.verify(token, agent_run_id=session.agent_run_id)
        claim, denial = self._acquire_writer_claim(
            session.permission_mode, session_id=session.session_id, agent_run_id=session.agent_run_id,
        )
        if denial is not None:
            return denial
        if claim is None:
            return self._writer_lease.recovery_denial()
        session.set_writer_claim(claim.claim_id)
        child_denial = self._track_current_adapter_child(session)
        return child_denial

    def _resolve_diff_accept(
        self,
        approval_id: str,
        agent_run_id: str,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Verify one negotiated Accept outside DB transactions, then persist or refresh atomically."""

        from .adapters import BrokerApprovalResult
        from .approvals import (
            DENIED_BODY_INVALID,
            DENIED_SNAPSHOT_INVALID,
            DENIED_STALE,
        )

        checked = self._approval_broker.check_accept(
            approval_id,
            agent_run_id,
            expected_revision=args.get("expected_revision"),
            snapshot_set_sha256=args.get("snapshot_set_sha256"),
            metadata_confirmed=args.get("metadata_confirmed") is True,
        )
        if checked.get("status") != "OK":
            if checked.get("code") in (DENIED_BODY_INVALID, DENIED_SNAPSHOT_INVALID):
                record = self._approval_broker.get(approval_id) or {}
                return self._approval_broker.deny_invalid(
                    session_id=str(record.get("session_id") or ""), agent_run_id=agent_run_id,
                    correlation_id=str(record.get("correlation_id") or ""),
                    denial_code=str(checked.get("code")), approval_id=approval_id,
                    session_fence=self._approval_session_fence(approval_id),
                )
            return checked
        if checked.get("negotiated") is not True:
            return self._approval_broker.resolve(
                approval_id, agent_run_id, "approve", decided_by="human",
                session_fence=self._approval_session_fence(approval_id),
            )
        session = self._get_driven(agent_run_id)
        if session is None:
            return self._approval_broker.resolve(
                approval_id, agent_run_id, "deny", decided_by="auto", denial_code=DENIED_STALE,
                session_fence=self._approval_session_fence(approval_id),
            )
        writer_denial = self._ensure_diff_writer_claim(session)
        if writer_denial is not None:
            return writer_denial
        outcome = self._diff_snapshots.validate_release({
            "approval_id": approval_id,
            "session_id": checked["record"]["session_id"],
            "agent_run_id": agent_run_id,
            "correlation_id": checked["record"]["correlation_id"],
            "approval": checked["approval"],
        })
        if outcome.get("status") == "REFRESH":
            refreshed = self._approval_broker.refresh(
                approval_id, agent_run_id, outcome["body"],
                session_fence=self._approval_session_fence(approval_id),
            )
            if refreshed.get("status") != "OK":
                return refreshed
            commit = outcome.get("commit")
            if callable(commit):
                commit()
            body = outcome["body"]
            return {
                "status": "DENIED", "code": "DENIED_APPROVAL_REVISION_MISMATCH",
                "retryable": True, "required_action": "refresh_preview",
                "current_revision": body["approval_revision"],
                "current_snapshot_set_sha256": body["snapshot_set_sha256"],
                "metadata_confirmation_required": any(
                    change["preview_mode"] == "metadata" for change in body["file_changes"]
                ),
            }
        if outcome.get("status") != "OK":
            code = outcome.get("code")
            record = checked["record"]
            if code == DENIED_SNAPSHOT_INVALID:
                return self._approval_broker.deny_invalid(
                    session_id=str(record["session_id"]), agent_run_id=agent_run_id,
                    correlation_id=str(record["correlation_id"]),
                    denial_code=DENIED_SNAPSHOT_INVALID, approval_id=approval_id,
                    session_fence=self._approval_session_fence(approval_id),
                )
            return self._approval_broker.resolve(
                approval_id, agent_run_id, "deny", decided_by="auto", denial_code=DENIED_STALE,
                session_fence=self._approval_session_fence(approval_id),
            )
        release_value = BrokerApprovalResult(
            "approved",
            updated_input=outcome.get("updated_input") if isinstance(outcome.get("updated_input"), Mapping) else None,
            post_apply=outcome.get("post_apply") if callable(outcome.get("post_apply")) else None,
            approval_revision=outcome.get("approval_revision")
            if isinstance(outcome.get("approval_revision"), int) else None,
            snapshot_set_sha256=outcome.get("snapshot_set_sha256")
            if isinstance(outcome.get("snapshot_set_sha256"), str) else None,
            approved_bases=tuple(outcome["approved_bases"])
            if isinstance(outcome.get("approved_bases"), tuple) else None,
        )
        return self._approval_broker.resolve(
            approval_id,
            agent_run_id,
            "approve",
            expected_revision=args.get("expected_revision"),
            snapshot_set_sha256=args.get("snapshot_set_sha256"),
            metadata_confirmed=args.get("metadata_confirmed") is True,
            decided_by="human",
            release_value=release_value,
            session_fence=self._approval_session_fence(approval_id),
        )

    def _broker_adapter_approval(self, session: DrivenSession, request: Any) -> Any:
        """Atomic broker open, post-commit park, sole timeout, and terminal decision rendezvous."""

        from .adapters.local_llm import DECISION_APPROVED, DECISION_DENIED

        if not isinstance(request, Mapping):
            return DECISION_DENIED
        correlation = request.get("correlation_id")
        if not isinstance(correlation, str) or not correlation:
            return DECISION_DENIED
        request_type = str(request.get("request_type") or "tool_approval")
        raw_body = request.get("approval_body", request.get("body"))
        body = raw_body if isinstance(raw_body, Mapping) else None
        if request_type == "requestUserInput":
            questions = _sanitize_user_input_questions(request.get("questions"))
            if questions is None:
                return {"decision": "denied", "code": "DENIED_APPROVAL_BODY_INVALID", "fatal": True}
            body = {"questions": questions}
        negotiated = session.diff_snapshots and request.get("negotiated") is True
        summary = request.get("summary")
        if not isinstance(summary, str) or not summary:
            summary = "Approval required."
        prepared = None
        if negotiated:
            diff_request = request.get("diff_request")
            try:
                if not isinstance(diff_request, Mapping):
                    raise DiffSnapshotError("DENIED_APPROVAL_SNAPSHOT_INVALID")
                prepared = self._diff_snapshots.prepare_request(
                    session.engine, diff_request, scope_id=session.session_id,
                )
                self._diff_snapshots.stage(session.agent_run_id, correlation, prepared)
                body = prepared.body
            except DiffSnapshotError as error:
                try:
                    self._approval_broker.deny_invalid(
                        session_id=session.session_id, agent_run_id=session.agent_run_id,
                        correlation_id=correlation, denial_code=error.code,
                        request_type=str(request.get("request_type") or "tool_approval"),
                        is_test=self._driven_test_records,
                        session_fence=self._session_write_fence(session.session_id),
                    )
                except Exception as denied_error:  # noqa: BLE001 -- no pending card may survive
                    self.log(f"diff approval construction failed ({session.agent_run_id}): {type(denied_error).__name__}")
                return {"decision": "denied", "code": str(error.code), "fatal": True}
        try:
            opened = self._approval_broker.open(
                session_id=session.session_id,
                agent_run_id=session.agent_run_id,
                correlation_id=correlation,
                body=body,
                negotiated=negotiated,
                request_type=request_type,
                summary=summary,
                is_test=self._driven_test_records,
                session_fence=self._session_write_fence(session.session_id),
            )
        except Exception as error:  # noqa: BLE001 -- an uncommitted ask cannot park
            self.log(f"approval broker open failed ({session.agent_run_id}): {type(error).__name__}")
            if prepared is not None:
                self._diff_snapshots.unstage(
                    session.agent_run_id, correlation, prepared.body["snapshot_set_sha256"],
                )
            return {"decision": "denied", "code": "ERROR_TOOL_APPROVAL_TRANSPORT", "fatal": True}
        if opened.get("status") != "OK" or opened.get("waiter_should_park") is not True:
            if prepared is not None:
                self._diff_snapshots.unstage(
                    session.agent_run_id, correlation, prepared.body["snapshot_set_sha256"],
                )
            return {"decision": "denied", "code": "DENIED_APPROVAL_BODY_INVALID", "fatal": True}
        approval_id = str(opened["id"])
        if prepared is not None:
            current = self._approval_broker.get(approval_id)
            active = current.get("active") if isinstance(current, Mapping) else None
            active_body = active.get("body") if isinstance(active, Mapping) else None
            if not isinstance(active_body, Mapping) \
                    or active_body.get("snapshot_set_sha256") != prepared.body["snapshot_set_sha256"]:
                self._diff_snapshots.unstage(
                    session.agent_run_id, correlation, prepared.body["snapshot_set_sha256"],
                )
                return {"decision": "denied", "code": "DENIED_APPROVAL_SNAPSHOT_INVALID", "fatal": True}
            self._diff_snapshots.register(
                approval_id, prepared, agent_run_id=session.agent_run_id,
            )
            self._diff_snapshots.unstage(
                session.agent_run_id, correlation, prepared.body["snapshot_set_sha256"],
            )
        if self._approval_before_park is not None:
            self._approval_before_park({
                "approval_id": approval_id, "session_id": session.session_id,
                "agent_run_id": session.agent_run_id, "correlation_id": correlation,
            })
        waiter = session.bind_broker_approval(correlation, approval_id)
        try:
            # Close approve-before-park: the broker commit may have won before the waiter mapping existed.
            current = self._approval_broker.get(approval_id)
            if current is not None and current.get("state") in ("approved", "denied"):
                if current["state"] == "approved":
                    with self._approved_broker_payloads_lock:
                        value = self._approved_broker_payloads.pop(
                            (session.agent_run_id, correlation), DECISION_APPROVED,
                        )
                    waiter.release(value)
                else:
                    waiter.release(DECISION_DENIED)
            decision, timed_out = waiter.wait_result(session.approval_timeout)
            if not timed_out:
                normalized = getattr(decision, "decision", decision)
                return decision if normalized == DECISION_APPROVED else DECISION_DENIED
            try:
                timeout = self._approval_broker.timeout(
                    approval_id,
                    session.agent_run_id,
                    session_fence=self._approval_session_fence(approval_id),
                )
            except Exception as error:  # noqa: BLE001 -- leave open for finally reconciliation
                self.log(f"approval timeout commit failed ({approval_id}): {type(error).__name__}")
                return {"decision": "denied", "code": "ERROR_TOOL_APPROVAL_TRANSPORT", "fatal": True}
            if timeout.get("code") == "DENIED_APPROVAL_ALREADY_DECIDED":
                current = self._approval_broker.get(approval_id)
                if current is not None and current.get("state") == "approved":
                    with self._approved_broker_payloads_lock:
                        return self._approved_broker_payloads.pop(
                            (session.agent_run_id, correlation), DECISION_APPROVED,
                        )
            return {"decision": "denied", "code": "DENIED_APPROVAL_TIMEOUT", "fatal": True}
        finally:
            with self._approved_broker_payloads_lock:
                self._approved_broker_payloads.pop((session.agent_run_id, correlation), None)
            session.clear_broker_approval(correlation, approval_id)

    def _handle_approve(self, args: dict[str, Any]) -> dict[str, Any]:
        """Decide a C4 approval over loopback (§B.5: approval authority binds to the epoch-current
        controller). When the approval's session is FENCED (owning_node set), the decide rides
        write_tx(session_fence=(session, this-node, current-epoch)) so a STALE-epoch daemon refuses
        DENIED_STALE_FENCE. decision is approve|deny.

        H0 EXTENDED alt-key: a driven-session client approves by the RACE-FREE ``correlation_id`` from the
        approval/open stream event + ``session_id`` (the adapter emits the correlation_id BEFORE it
        persists the C4 row -- ``LocalLlmAdapter._resolve_ask`` emits then calls ``record_ask`` -- so an approval_id-in-event races; correlation
        is the safe handle). When ``approval_id`` is absent we resolve it via policy._open_approval_id
        (the open row for that session+correlation). AFTER a successful C4 decide we release the LIVE
        adapter waiter parked on that correlation, so the parked turn thread unblocks. Double-approve of
        an already-decided row surfaces DENIED_APPROVAL_ALREADY_DECIDED from the C4 state machine."""
        approval_id = args.get("approval_id")
        decision = args.get("decision")
        correlation_id = args.get("correlation_id")
        session_id = args.get("session_id")
        agent_run_id = args.get("agent_run_id")
        if not decision:
            return {"status": "DENIED", "code": "DENIED_APPROVE_FIELDS_REQUIRED",
                    "required_action": "approve needs a decision (approve|deny) and approval_id, or correlation_id+session_id"}
        broker_record: dict[str, Any] | None = None
        if not approval_id:
            if not (correlation_id and session_id):
                return {"status": "DENIED", "code": "DENIED_APPROVE_FIELDS_REQUIRED",
                        "required_action": "approve needs approval_id, or correlation_id+session_id"}
            approval_id, broker_record, resolution_denial = self._resolve_approval_alt_key(
                str(session_id), str(correlation_id),
                str(agent_run_id) if agent_run_id is not None else None,
            )
            if resolution_denial is not None:
                return resolution_denial
            if approval_id is None:
                return {"status": "DENIED", "code": "DENIED_APPROVAL_NOT_FOUND",
                        "correlation_id": correlation_id, "session_id": session_id,
                        "required_action": "no approval for this session+correlation_id (it may not be persisted yet; retry)"}
        try:
            if broker_record is None:
                broker_record = self._approval_broker.get(str(approval_id))
            scope_denial = self._approval_scope_denial(
                broker_record, session_id=session_id, correlation_id=correlation_id,
                agent_run_id=agent_run_id,
            )
            if scope_denial is not None:
                return scope_denial
            if broker_record is not None and broker_record.get("broker_managed") is True:
                active = broker_record.get("active")
                if not isinstance(active, Mapping) or not isinstance(active.get("agent_run_id"), str):
                    return {"status": "DENIED", "code": "DENIED_APPROVAL_BODY_INVALID",
                            "required_action": "rerun_turn"}
                if str(decision).strip().lower() == "approve" and active.get("negotiated") is True:
                    return self._resolve_diff_accept(
                        str(approval_id), str(active["agent_run_id"]), args,
                    )
                release_value = None
                if str(decision).strip().lower() == "approve" \
                        and broker_record.get("request_type") == "requestUserInput":
                    from .adapters import BrokerApprovalResult

                    answers = _validated_user_input_answers(broker_record, args.get("answers"))
                    if answers is None:
                        return {
                            "status": "DENIED",
                            "code": "DENIED_USER_INPUT_ANSWER_REQUIRED",
                            "retryable": True,
                            "required_action": "submit exact non-empty answers for every free-text question",
                        }
                    release_value = BrokerApprovalResult(
                        "approved",
                        updated_input={"answers": answers} if answers else None,
                    )
                return self._approval_broker.resolve(
                    str(approval_id),
                    str(active["agent_run_id"]),
                    str(decision),
                    expected_revision=args.get("expected_revision"),
                    snapshot_set_sha256=args.get("snapshot_set_sha256"),
                    metadata_confirmed=args.get("metadata_confirmed") is True,
                    decided_by="human",
                    release_value=release_value,
                    session_fence=self._approval_session_fence(str(approval_id)),
                )

            from .. import session_records

            fence = self._approval_session_fence(approval_id)
            result = session_records.decide_approval(approval_id, decision, decided_by="human", session_fence=fence)
        except KaizenDenied as denied:
            return denied.payload()
        except Exception as error:  # noqa: BLE001 -- an approve error must not kill the accept loop
            return {"status": "ERROR", "code": "ERROR_APPROVE", "message": str(error)}
        # Release the LIVE driven waiter (if any) parked on this correlation so the turn thread unblocks.
        # Legacy rows have no broker run binding; keep their compatibility release session-scoped.
        record_session_id = broker_record.get("session_id") if broker_record is not None else session_id
        record_correlation = broker_record.get("correlation_id") if broker_record is not None else None
        released = self._release_driven_waiter(
            str(record_session_id) if record_session_id else None,
            str(record_correlation or correlation_id) if (record_correlation or correlation_id) else
            self._approval_correlation_id(str(approval_id)),
            decision,
        )
        return {**result, "waiter_released": released}

    def _resolve_approval_alt_key(
        self,
        session_id: str,
        correlation_id: str,
        agent_run_id: str | None,
    ) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
        """Resolve session+correlation broker-first; use legacy C4 only without a scoped live waiter."""

        with self._driven_lock:
            sessions = [session for session in self._driven.values() if session.session_id == session_id]
        live = [
            (session.agent_run_id, approval_id)
            for session in sessions
            if (approval_id := session.live_approval_id(correlation_id)) is not None
        ]
        scoped_live = [item for item in live if agent_run_id is None or item[0] == agent_run_id]
        if agent_run_id is None and len(scoped_live) > 1:
            return None, None, {
                "status": "DENIED", "code": "DENIED_APPROVAL_SCOPE_AMBIGUOUS", "retryable": False,
                "required_action": "retry with approval_id or agent_run_id",
            }
        if len(scoped_live) == 1:
            live_run_id, live_id = scoped_live[0]
            record = self._approval_broker.get(live_id)
            active = record.get("active") if isinstance(record, Mapping) else None
            if not isinstance(record, Mapping) or record.get("broker_managed") is not True \
                    or not isinstance(active, Mapping) or active.get("agent_run_id") != live_run_id:
                self._writer_lease.require_recovery("live approval waiter lost its broker-managed run binding")
                return None, None, self._writer_lease.recovery_denial()
            return live_id, dict(record), None

        records = [
            record for approval_id in policy._approval_ids(session_id, correlation_id)
            if (record := self._approval_broker.get(approval_id)) is not None
        ]
        managed = [record for record in records if record.get("broker_managed") is True]
        if agent_run_id is not None:
            managed = [
                record for record in managed
                if isinstance(record.get("active"), Mapping)
                and record["active"].get("agent_run_id") == agent_run_id
            ]
        if len(managed) > 1:
            return None, None, {
                "status": "DENIED", "code": "DENIED_APPROVAL_SCOPE_AMBIGUOUS", "retryable": False,
                "required_action": "retry with approval_id",
            }
        if managed:
            return str(managed[0]["id"]), managed[0], None
        if live or agent_run_id is not None:
            return None, None, {
                "status": "DENIED", "code": "DENIED_APPROVAL_SCOPE_MISMATCH", "retryable": False,
                "required_action": "refresh the approval and retry with its exact session and run",
            }
        legacy = [record for record in records if record.get("broker_managed") is not True]
        if legacy:
            open_legacy = next((record for record in legacy if record.get("state") == "open"), None)
            selected = open_legacy or legacy[0]
            return str(selected["id"]), selected, None
        return None, None, None

    @staticmethod
    def _approval_scope_denial(
        record: Mapping[str, Any] | None,
        *,
        session_id: Any,
        correlation_id: Any,
        agent_run_id: Any,
    ) -> dict[str, Any] | None:
        """Returns a SCOPE_MISMATCH denial when record session/correlation/active-run disagrees with caller ids, else None."""
        if record is None:
            return None
        mismatch = (
            session_id is not None and record.get("session_id") != str(session_id)
        ) or (
            correlation_id is not None and record.get("correlation_id") != str(correlation_id)
        )
        active = record.get("active")
        if agent_run_id is not None:
            mismatch = mismatch or not isinstance(active, Mapping) \
                or active.get("agent_run_id") != str(agent_run_id)
        if not mismatch:
            return None
        return {
            "status": "DENIED", "code": "DENIED_APPROVAL_SCOPE_MISMATCH", "retryable": False,
            "required_action": "refresh the approval and retry with its exact session and run",
        }

    def _approval_correlation_id(self, approval_id: str) -> str | None:
        """DB lookup of an approval row's correlation_id; None when absent/empty."""
        from .. import db

        row = db.fetch_one("SELECT correlation_id FROM approval_requests WHERE id = ?", (approval_id,))
        return row[0] if row and row[0] else None

    def _release_driven_waiter(self, session_id: str | None, correlation_hash: str | None,
                               decision: str) -> bool:
        """Release one legacy waiter by its complete public alternate key."""
        if not session_id or not correlation_hash:
            return False
        from .adapters.local_llm import DECISION_APPROVED, DECISION_DENIED

        mapped = DECISION_APPROVED if str(decision).strip().lower() == "approve" else DECISION_DENIED
        with self._driven_lock:
            sessions = [session for session in self._driven.values() if session.session_id == session_id]
        for session in sessions:
            if session.resolve_waiter(correlation_hash, mapped):
                return True
        return False

    def _driven_approval_recheck(self, session_id: str, correlation_id: str) -> str | None:
        """On-park approval re-check (the race fix). Connect-per-call: is there an ALREADY-DECIDED C4 row
        for (session_id, correlation_id)? Return the adapter decision string (approved/denied) so the
        parked waiter resolves instantly instead of stranding until approval_timeout when an approve
        landed between the adapter's C4-decide and its park. Open/absent -> None (block normally). Any DB
        error -> None (fall through to normal parking; the fail-closed clock is unchanged)."""
        from .adapters.local_llm import DECISION_APPROVED, DECISION_DENIED
        from .. import db

        try:
            row = db.fetch_one(
                "SELECT state FROM approval_requests WHERE session_id = ? AND correlation_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (session_id, correlation_id),
            )
        except Exception:  # noqa: BLE001 -- a recheck DB error must never crash the ask path
            return None
        if row is None:
            return None
        state = row[0]
        if state == "approved":
            return DECISION_APPROVED
        if state == "denied":
            return DECISION_DENIED
        return None  # open (or another non-approve/deny terminal) -> block on the adapter clock

    def _approval_session_fence(self, approval_id: str) -> tuple[str, str | None, int | None] | None:
        """The CONCRETE-epoch session fence for deciding ``approval_id`` (§B.5). When the approval's
        session is FENCED (owning_node set), bind the decide to (session, this-node, the session's CURRENT
        epoch) so a stale-epoch daemon refuses. Off-mode / unfenced session ⇒ None (no fence)."""
        node_id = self._my_node_id()
        if node_id is None:
            return None
        from .. import db

        row = db.fetch_one(
            "SELECT s.owning_node, s.node_epoch FROM approval_requests a "
            "JOIN agent_sessions s ON s.id = a.session_id WHERE a.id = ?",
            (approval_id,),
        )
        if row is None or row[0] is None:
            return None  # unknown approval (decide's own not-found handles it) or unfenced session
        # Bind to THIS node at the session's CURRENT epoch: a daemon whose node is not the owner, or whose
        # view of the epoch is stale, refuses DENIED_STALE_FENCE.
        return (self._approval_session_id(approval_id), node_id, int(row[1]) if row[1] is not None else None)

    def _approval_session_id(self, approval_id: str) -> str:
        """DB lookup of an approval's session_id; FALLS BACK to returning `approval_id` when the row is missing."""
        from .. import db

        row = db.fetch_one("SELECT session_id FROM approval_requests WHERE id = ?", (approval_id,))
        return row[0] if row else approval_id

    def _handle_attach(self, args: dict[str, Any]) -> dict[str, Any]:
        """Cross-machine attach over loopback (§B.5): take a session's ownership + bump its epoch, with
        new_owning_node = THIS daemon's node. A stale expected_node_epoch refuses DENIED_STALE_FENCE."""
        session_id = args.get("session_id")
        expected_owning_node = args.get("expected_owning_node")
        expected_node_epoch = args.get("expected_node_epoch")
        node_id = self._my_node_id()
        if node_id is None:
            return {"status": "DENIED", "code": "DENIED_FLEET_NOT_ACTIVE",
                    "required_action": "attach needs distribution on (a fleet node id)"}
        if not (session_id and expected_owning_node is not None and expected_node_epoch is not None):
            return {"status": "DENIED", "code": "DENIED_ATTACH_FIELDS_REQUIRED",
                    "required_action": "attach needs session_id, expected_owning_node, expected_node_epoch"}
        try:
            from .. import session_records

            return session_records.attach_session(
                session_id, new_owning_node=node_id,
                expected_owning_node=expected_owning_node, expected_node_epoch=int(expected_node_epoch),
            )
        except KaizenDenied as denied:
            return denied.payload()
        except Exception as error:  # noqa: BLE001 -- an attach error must not kill the accept loop
            return {"status": "ERROR", "code": "ERROR_ATTACH", "message": str(error)}

    def _handle_dispatch_poll(self, args: dict[str, Any]) -> dict[str, Any]:
        """One executor cycle: accept/start/run/complete self-targeted requested dispatches (bounded).
        Returns {executed: [...], skipped: n}. No fleet ⇒ DENIED_FLEET_NOT_ACTIVE."""
        if self._fleet is None:
            return {"status": "DENIED", "code": "DENIED_FLEET_NOT_ACTIVE",
                    "required_action": "distribution is off or fleet.db failed to open on this daemon"}
        limit = int(args["max"]) if args.get("max") is not None else 1
        return self._poll_dispatches(max_per_poll=limit)

    def _handle_orchestrate(self, args: dict[str, Any]) -> dict[str, Any]:
        """The headless start-work verb (§B.5 / §D): a convenience = a D7 dispatch request to SELF (this
        node, which must hold the granted lease on scope) + an immediate dispatch/poll cycle. Returns
        {request, poll}. No fleet ⇒ DENIED_FLEET_NOT_ACTIVE; the request refuses DENIED_DISPATCH_UNLEASED
        when this node does not hold the lease (surfaced structured)."""
        if self._fleet is None:
            return {"status": "DENIED", "code": "DENIED_FLEET_NOT_ACTIVE",
                    "required_action": "distribution is off or fleet.db failed to open on this daemon"}
        scope_key = args.get("scope")
        task = args.get("task")
        if not (scope_key and task):
            return {"status": "DENIED", "code": "DENIED_ORCHESTRATE_FIELDS_REQUIRED",
                    "required_action": "orchestrate needs --scope and --task"}
        from ..fleet import dispatch_remote

        try:
            request = dispatch_remote.request_dispatch(
                self._fleet, target_node=self._fleet.node_id, task=task, scope_key=scope_key,
                summary=f"Orchestrate (self-dispatch) {task}.",
            )
        except KaizenDenied as denied:
            return denied.payload()
        poll = self._poll_dispatches(max_per_poll=1)
        return {"status": "OK", "request": request, "poll": poll}

    def _poll_dispatches(self, *, max_per_poll: int = 1) -> dict[str, Any]:
        """The M14 dispatch executor (§B.5), loopback/CLI-driven -- NO background thread (deterministic).

        Read a FRESH reduce_dispatches; for each dispatch whose target is THIS node in state 'requested'
        (bounded by max_per_poll): accept -> start -> run the injected ``self._dispatch_runner(task,
        workdir)`` in a scratch worktree dir under RUNTIME_DIR/dispatch-work/<id> -> write the returned
        patch bytes to RUNTIME_DIR/dispatch-artifacts/<id>.patch -> complete_dispatch with basename +
        sha256. DEFAULT runner is None ⇒ skip execution and fail_dispatch('no runner configured')
        TRUTHFULLY (a real deployment wires an adapter; tests inject a runner). A runner EXCEPTION ⇒
        fail_dispatch(reason class) truthfully. Returns {status, executed: [...], skipped: n}."""
        from ..fleet import dispatch_remote

        node_id = self._fleet.node_id
        reduced = dispatch_remote.current_dispatches(self._fleet)
        pending = sorted(
            (d for d in reduced.values()
             if d.get("target_node") == node_id and d.get("state") == "requested"),
            key=lambda d: (str(d.get("created_at") or ""), str(d.get("dispatch_id") or "")),
        )
        executed: list[dict[str, Any]] = []
        skipped = 0
        for entry in pending[: max(0, max_per_poll)]:
            dispatch_id = entry["dispatch_id"]
            try:
                executed.append(self._execute_one_dispatch(dispatch_id, entry))
            except KaizenDenied as denied:
                # A transition refusal (e.g. a race stole the dispatch) is recorded and skipped, not fatal.
                self.log(f"dispatch {dispatch_id} skipped: {denied.code}")
                skipped += 1
        return {"status": "OK", "executed": executed, "skipped": skipped, "pending": len(pending)}

    def _execute_one_dispatch(self, dispatch_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        """Accept -> start -> run -> complete/fail ONE dispatch. Truthful: a missing runner or a runner
        exception fails the dispatch with a real reason (never a fabricated completion)."""
        from ..fleet import dispatch_remote

        dispatch_remote.accept_dispatch(self._fleet, dispatch_id)
        dispatch_remote.start_dispatch(self._fleet, dispatch_id)
        row_payload = self._dispatch_row_task(dispatch_id)
        task = row_payload.get("task", "")
        if self._dispatch_runner is None:
            # TRUTHFUL: no runner wired (the default). A real deployment injects an adapter runner.
            dispatch_remote.fail_dispatch(self._fleet, dispatch_id, "no runner configured")
            return {"dispatch_id": dispatch_id, "state": "failed", "reason": "no runner configured"}
        workdir = RUNTIME_DIR / "dispatch-work" / dispatch_id
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            produced = self._dispatch_runner(task, str(workdir))
        except Exception as error:  # noqa: BLE001 -- a runner failure fails the dispatch truthfully
            self.log(f"dispatch {dispatch_id} runner error: {error}")
            dispatch_remote.fail_dispatch(self._fleet, dispatch_id, f"runner error: {type(error).__name__}")
            return {"dispatch_id": dispatch_id, "state": "failed", "reason": f"runner error: {type(error).__name__}"}
        artifact_text = (produced or {}).get("artifact_text", "") if isinstance(produced, dict) else ""
        branch = (produced or {}).get("branch") if isinstance(produced, dict) else None
        artifacts_dir = RUNTIME_DIR / "dispatch-artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifacts_dir / f"{dispatch_id}.patch"
        artifact_path.write_bytes(artifact_text.encode("utf-8"))
        sha = dispatch_remote.sha256_text(artifact_text)
        dispatch_remote.complete_dispatch(
            self._fleet, dispatch_id, artifact_name=artifact_path.name, artifact_sha=sha, branch=branch,
        )
        return {"dispatch_id": dispatch_id, "state": "completed", "artifact": artifact_path.name,
                "sha": sha, "artifact_path": str(artifact_path)}

    def _dispatch_row_task(self, dispatch_id: str) -> dict[str, Any]:
        """Parse the dispatch row's `payload_json` to a dict; `{}` on missing/invalid JSON."""
        rows = self._fleet._read_all("SELECT payload_json FROM remote_dispatches WHERE id = ?", (dispatch_id,))
        if not rows or not rows[0][0]:
            return {}
        try:
            payload = json.loads(rows[0][0])
        except (ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    # --- H2.1 session/capabilities (per-engine capability discovery) ----------------------------

    def _handle_session_capabilities(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return per-engine capability descriptors (plan "Canonical engine names and capability
        discovery"). Engine ids on the wire are the canonical set (``local_llm``, ``codex``, ``claude``);
        the internal ``claude_cli`` lane key is normalized away here. Cached in daemon memory; rebuilt on
        boot or when ``refresh: true``. ``local_llm`` probes the Ollama server live (read-only) for its
        model catalog, degrading to state ``degraded`` (drivable stays true, models []) when the probe
        fails. Vendor descriptors come from their adapters' fail-closed capability probes; discovery
        never starts an authenticated turn and Codex schema scratch stays below the D-workspace runtime."""
        refresh = args.get("refresh") is True
        with self._capabilities_lock:
            expired = self._capabilities_built_at > 0.0 and (
                time.monotonic() - self._capabilities_built_at >= MODEL_CATALOG_TTL_SECONDS
            )
            if self._capabilities is None or refresh or expired:
                self._capabilities = self._build_capabilities(
                    refresh_claude_catalog=True,
                ) if refresh else self._build_capabilities()
                self._capabilities_built_at = time.monotonic()
            # Return a detached canonical value: a client cannot mutate a nested cached availability/model.
            engines = json.loads(json.dumps(self._capabilities))
        return {"status": "OK", "engines": engines}

    def _build_capabilities(self, *, refresh_claude_catalog: bool = False) -> list[dict[str, Any]]:
        """Materialize canonical wire-order descriptors under the cache lock, mutating each temporary descriptor to consume private probe evidence keys and inject its validated public features."""
        descriptors = [
            self._local_llm_capability(),
            self._codex_capability(),
            self._claude_capability(refresh_catalog=refresh_claude_catalog),
        ]
        # Opaque vendor features require a targeted bounded probe of that exact feature. Legacy
        # ``_*_proven`` booleans are intentionally discarded: opening an adapter, listing models, and
        # proving teardown establishes basic-chat readiness, not streaming/artifact/tool behavior.
        # The Claude worker returns ``_probed_features`` only after one bounded operation per exact
        # feature. The in-process local lane instead supplies one exact code-evidence map. Neither
        # evidence form can enable the other descriptor class; absent/malformed/duplicate/unknown
        # evidence stays dark.
        from .claude_worker_protocol import CAPABILITY_PROBE_FEATURES

        for descriptor in descriptors:
            probed = descriptor.pop("_probed_features", ())
            raw_probed = list(probed) if isinstance(probed, (list, tuple)) else []
            probed_features = {
                feature for feature in CAPABILITY_PROBE_FEATURES if raw_probed.count(feature) == 1
            }
            code_evidence = descriptor.pop("_code_proven_features", None)
            code_proven_features = set(_LOCAL_LLM_CODE_FEATURE_EVIDENCE) if (
                descriptor.get("id") == "local_llm"
                and isinstance(code_evidence, Mapping)
                and dict(code_evidence) == _LOCAL_LLM_CODE_FEATURE_EVIDENCE
            ) else set()
            proven_features = code_proven_features if descriptor.get("id") == "local_llm" \
                else probed_features
            for legacy_key in (
                "_streaming_proven", "_diff_snapshots_proven", "_image_attachments_proven",
                "_governed_context_proven", "_controlled_tools_proven", "_process_execution_proven",
            ):
                descriptor.pop(legacy_key, None)
            subscription_auth_proven = descriptor.pop("_subscription_auth_proven", False) is True
            drivable = descriptor.get("drivable") is True
            descriptor["features"] = {
                "streaming": drivable and "streaming" in proven_features,
                "image_attachments": drivable and "image_attachments" in proven_features,
                "governed_context": drivable and "governed_context" in proven_features,
                "diff_snapshots": drivable and "diff_snapshots" in proven_features,
                "writer_leasing": True,
                "subscription_auth": subscription_auth_proven and drivable,
                "controlled_tools": drivable and "controlled_tools" in proven_features,
                "process_execution": drivable and "process_execution" in proven_features,
                # Additive UI contract support, not proof that an authenticated acceptance run passed.
                "test_extension": descriptor.get("id") in {"local_llm", "claude"},
            }
        return descriptors

    _ALL_PERMISSION_MODES = list(policy.PERMISSION_MODES)

    def _local_llm_capability(self) -> dict[str, Any]:
        """The driven local-LLM lane. Models are probed live from the Ollama server (read-only reuse of
        the backends transport: native ``/api/tags`` off the configured base URL). A probe failure ->
        state ``degraded`` + a warning + models [] (drivable STAYS true: a model can still be validated
        at start via the editable-model field). reasoning_efforts are omitted (the backend does not
        expose them); auth_modes is ``["none"]`` (a local model has no billing path)."""
        models, warnings, state = self._probe_ollama_models()
        return {
            "id": "local_llm",
            "label": "Local LLM (Ollama)",
            "drivable": True,
            "availability": {"state": state,
                             "code": None if state == "available" else "DENIED_ENGINE_DEGRADED",
                             "message": "" if state == "available"
                             else "the Ollama model catalog is unavailable; the model field is editable and validated at start"},
            "models": models,
            "default_model": None,
            "default_reasoning_effort": None,
            "auth_modes": ["none"],
            "permission_modes": list(self._ALL_PERMISSION_MODES),
            "warnings": warnings,
            "_code_proven_features": dict(_LOCAL_LLM_CODE_FEATURE_EVIDENCE),
        }

    def _codex_capability(self) -> dict[str, Any]:
        """Run Codex's non-turn installed-schema probe with D-workspace runtime scratch."""

        from .adapters import codex

        root = (self.runtime_dir / "capability-cache" / "codex").resolve()
        return dict(codex.installed_capability(runtime_dir=root / "schema"))

    def _claude_capability(self, *, refresh_catalog: bool = False) -> dict[str, Any]:
        """Probe the pinned official SDK runtime without a prompt and return only sanitized fields."""

        from .adapters import claude_sdk
        target = self._frozen_claude_provider_target()
        if target is None:
            return dict(claude_sdk.unavailable_capability())
        kwargs: dict[str, Any] = {
            "runtime_root": self.repo_root,
            "worker_command": list(target.command),
        }
        if refresh_catalog:
            kwargs["refresh_models"] = True
        return dict(claude_sdk.probe_capability(self.repo_root, logger=self.log, **kwargs))

    def _frozen_claude_provider_target(self) -> _ClaudeProviderTarget | None:
        """Validate and cache the external provider pointer exactly once under the lock, including a validated-None result; every session uses that frozen target."""
        with self._claude_provider_target_lock:
            if not self._claude_provider_target_frozen:
                self._claude_provider_target = _validated_claude_provider_target(self.repo_root)
                self._claude_provider_target_frozen = True
            return self._claude_provider_target

    def _capability_for_engine(self, engine: str) -> dict[str, Any] | None:
        """Return a shallow copy of the canonical engine descriptor, rebuilding the shared cache under its lock when absent or TTL-expired."""

        wire_id = normalize_engine(engine)
        with self._capabilities_lock:
            expired = self._capabilities_built_at > 0.0 and (
                time.monotonic() - self._capabilities_built_at >= MODEL_CATALOG_TTL_SECONDS
            )
            if self._capabilities is None or expired:
                self._capabilities = self._build_capabilities()
                self._capabilities_built_at = time.monotonic()
            for descriptor in self._capabilities:
                if descriptor.get("id") == wire_id:
                    return dict(descriptor)
        return None

    def _engine_feature(self, engine: str, name: str) -> bool:
        """True iff the engine's cached descriptor has feature `name` enabled."""
        descriptor = self._capability_for_engine(engine)
        features = descriptor.get("features") if isinstance(descriptor, Mapping) else None
        return isinstance(features, Mapping) and features.get(name) is True

    def _probe_ollama_models(self) -> tuple[list[dict[str, Any]], list[str], str]:
        """Read-only live probe of the Ollama model catalog (native ``/api/tags`` off the configured base
        URL, tags root = base with a trailing ``/v1`` stripped -- the same v1-vs-native handling the
        backends layer uses). Returns (models, warnings, state): state ``available`` with the tag list on
        success; state ``degraded`` with an empty list + a warning on ANY failure (unreachable server,
        bad payload, timeout). NEVER raises -- a probe failure degrades the lane, it does not break the
        capabilities response."""
        import re
        import urllib.error
        import urllib.parse
        import urllib.request

        from ..backends import _DEFAULT_OLLAMA_BASE

        base = os.environ.get("KAIZEN_LLM_BASE_URL", _DEFAULT_OLLAMA_BASE)
        root = re.sub(r"/v1/?$", "", base.rstrip("/")) or base
        url = f"{root}/api/tags"
        if urllib.parse.urlparse(url).scheme not in ("http", "https"):
            return [], ["model catalog unavailable: endpoint scheme must be http or https"], "degraded"
        try:
            with urllib.request.urlopen(url, timeout=_OLLAMA_TAGS_TIMEOUT) as resp:  # noqa: S310 -- fixed local scheme
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
            return [], [f"model catalog unavailable at {root}: {type(error).__name__}"], "degraded"
        models: list[dict[str, Any]] = []
        for entry in (data.get("models", []) if isinstance(data, dict) else []):
            name = entry.get("name") or entry.get("model")
            if name:
                models.append({"id": name, "label": name, "reasoning_efforts": [], "default_effort": None})
        return models, [], "available"

    def _vendor_preflight(
        self,
        engine: str,
        effective: dict[str, Any],
        snapshot: policy.PolicySnapshot,
        approval_timeout: float,
        requested_model: Any,
        requested_reasoning_effort: Any,
        writer_claim_binding: WriterClaimBinding,
        streaming: bool = False,
    ) -> dict[str, Any]:
        """Open a vendor adapter without a prompt or durable records, buffering lifecycle events."""

        recorder = _BufferedRecorder()
        adapter: Any = None
        try:
            adapter = self._build_driven_adapter(
                f"preflight-{engine}-{secrets.token_hex(8)}",
                {"engine_name": engine, "approval_timeout": approval_timeout, "streaming": streaming},
                snapshot=snapshot,
                recorder_override=recorder,
                writer_claim_binding=writer_claim_binding,
            )
            opened = adapter.open(effective, snapshot)
            if not isinstance(opened, Mapping) or opened.get("status") != "OK":
                refusal = dict(opened) if isinstance(opened, Mapping) else {}
                refusal.setdefault("status", "DENIED")
                refusal.setdefault("code", "DENIED_POLICY_GATE_UNAVAILABLE")
                refusal.setdefault("required_action", "repair the vendor startup handshake before retrying")
                raise _VendorPreflightRefused(refusal)

            reported = opened.get("profile")
            if isinstance(reported, Mapping):
                reported_profile = dict(reported)
            else:
                reported_profile = {
                    key: opened.get(key)
                    for key in self._PROFILE_FIELDS
                    if opened.get(key) is not None
                }
            resolved = dict(effective)
            for key in self._PROFILE_FIELDS:
                if reported_profile.get(key) is not None:
                    resolved[key] = reported_profile[key]

            for key in ("permission_mode", "auth_mode"):
                if resolved.get(key) != effective.get(key):
                    raise _VendorPreflightRefused({
                        "status": "DENIED", "code": "DENIED_PROFILE_MISMATCH", "field": key,
                        "requested": effective.get(key), "effective": resolved.get(key),
                        "required_action": "start a new conversation with the vendor-effective profile",
                    })
            for key, requested in (("model", requested_model),
                                   ("reasoning_effort", requested_reasoning_effort)):
                if requested is not None and resolved.get(key) != requested:
                    raise _VendorPreflightRefused({
                        "status": "DENIED", "code": "DENIED_PROFILE_MISMATCH", "field": key,
                        "requested": requested, "effective": resolved.get(key),
                        "required_action": "start a new conversation with the vendor-effective profile",
                    })
            return {"status": "OK", "adapter": adapter, "recorder": recorder, "effective": resolved}
        except _VendorPreflightRefused as refused:
            denial = refused.response
        except Exception as error:  # noqa: BLE001 -- known adapter errors expose a structured payload
            payload_fn = getattr(error, "payload", None)
            if callable(payload_fn):
                value = payload_fn()
                denial = dict(value) if isinstance(value, Mapping) else {}
            else:
                denial = {}
            denial.setdefault("status", "DENIED")
            denial.setdefault("code", "DENIED_POLICY_GATE_UNAVAILABLE")
            denial.setdefault("required_action", "repair the vendor startup handshake before retrying")
        termination_proven = adapter is None
        if adapter is not None:
            termination_proven = self._kill_adapter_proven(adapter)
            self._cleanup_vendor_runtime(adapter)
        denial["_termination_proven"] = termination_proven
        denial["engine"] = engine
        return denial

    # --- H0 driven-session ops (§app-server: the daemon DRIVES a local-LLM turn under the ledger) -----

    def _handle_session_start(self, args: dict[str, Any]) -> dict[str, Any]:
        """Start one capability-gated driven conversation and launch its first background turn.

        The ENGINE GATE fires FIRST, before any C1/T5 write (so a denied/unknown engine leaves NO ledger
        rows). Local is always available; Codex/Claude consult their cached installed safety descriptor
        and return its exact denial code unless explicitly drivable. Unknown adapters return
        DENIED_UNKNOWN_ENGINE. ``claude_cli`` normalizes to canonical wire id ``claude``.

        H2.1 profile: an optional ``profile {model?, reasoning_effort?, permission_mode?, auth_mode?}`` +
        ``full_opt_in?`` gates the session. Deterministic profile/model/full validation runs BEFORE any
        C1/T5 write (a denial leaves ZERO ledger rows). After validation an immutable per-session
        PolicySnapshot is cut (designated roots from the mode profile, rules from the DB) and drives the
        non-factory adapter; C1/T5 persist the effective profile + snapshot hash; a profile/point event is
        emitted as the FIRST stream event after T5.

        Vendor ``open`` runs with a buffered recorder before C1/T5 and never submits a prompt. After the
        rows exist: ownership -> profile/point -> bind approval/gate -> flush -> first turn. Local retains
        its established post-record open path."""
        engine = normalize_engine(args.get("engine"))
        prompt = args.get("prompt")
        if not engine:
            return {"status": "DENIED", "code": "DENIED_ENGINE_REQUIRED",
                    "required_action": "session/start needs an engine"}
        if prompt is None or prompt == "":
            return {"status": "DENIED", "code": "DENIED_PROMPT_REQUIRED",
                    "required_action": "session/start needs a prompt"}
        gate = self._gate_driven_engine(engine)
        if gate is not None:
            return gate  # engine denied BEFORE any C1/T5 write (no ledger rows for a bad engine)

        # Deterministic arg validation BEFORE any C1/T5 write: a malformed prompt/model/max_turns/
        # approval_timeout is DENIED here (structured, with required_action) so no half-open session/run
        # rows are ever written for bad input (mirrors the engine gate's zero-rows guarantee). This also
        # keeps the int()/float() coercions below from raising into the accept loop on garbage.
        model = args.get("model")
        denial = self._validate_driven_args(prompt, model, args.get("max_turns"), args.get("approval_timeout"))
        if denial is not None:
            return denial
        denial = self._validate_artifact_refs(args)
        if denial is not None:
            return denial
        session_title, denial = self._safe_session_title(str(prompt), args.get("title"))
        if denial is not None:
            return denial
        client_features = parse_feature_flags(args.get("client_features"))
        streaming_enabled = self._engine_feature(engine, "streaming")
        image_attachments_enabled = self._engine_feature(engine, "image_attachments")
        governed_context_enabled = self._engine_feature(engine, "governed_context")
        diff_snapshots_enabled = (
            client_features.get("diff_snapshots") is True
            and self._engine_feature(engine, "diff_snapshots")
        )
        client_features["diff_snapshots"] = diff_snapshots_enabled
        if engine == "codex" and args.get("max_turns") is not None:
            return {"status": "DENIED", "code": "DENIED_PROFILE_UNSUPPORTED", "field": "max_turns",
                    "required_action": "Codex conversations do not accept the Claude/local max_turns option"}
        if engine == "claude" and args.get("max_turns") is not None \
                and int(args["max_turns"]) > _CLAUDE_MAX_TURNS_CEILING:
            return {"status": "DENIED", "code": "MODEL_CALL_BUDGET_EXHAUSTED", "field": "max_turns",
                    "required_action": f"Claude max_turns must be an integer from 1 through {_CLAUDE_MAX_TURNS_CEILING}"}

        # H2.1 profile validation (also pre-C1/T5). Resolves the effective profile (model/reasoning_effort/
        # permission_mode/auth_mode) from the profile object + the legacy top-level model, rejecting a
        # conflict / unknown fields / an unsupported effort / a Full without opt-in. ZERO rows on denial.
        prof = self._validate_profile(engine, model, args.get("profile"), bool(args.get("full_opt_in")))
        if isinstance(prof, dict) and prof.get("status") == "DENIED":
            return prof
        eff_model, reasoning_effort, permission_mode, auth_mode = (
            prof["model"], prof["reasoning_effort"], prof["permission_mode"], prof["auth_mode"]
        )

        max_turns = int(args["max_turns"]) if args.get("max_turns") is not None \
            else _CLAUDE_DEFAULT_MAX_TURNS if engine == "claude" else None
        approval_timeout = (
            float(args["approval_timeout"]) if args.get("approval_timeout") is not None
            else DRIVEN_APPROVAL_TIMEOUT_DEFAULT
        )

        # Immutable per-session policy snapshot (H2.1): designated roots from the mode profile resolved
        # against repo_root, authority rules from the DB, workspace = repo_root. The snapshot's profile_hash
        # rides the C1/T5 rows and the profile/point body; the snapshot drives the non-factory adapter.
        snapshot = self._build_session_snapshot(engine, permission_mode)
        profile_hash = snapshot.profile_hash

        # Source-capable modes reserve the one workspace writer before materialization, vendor
        # preflight, child launch, or durable work. Plan intentionally acquires only at its first
        # intercepted designated write.
        writer_claim: WriterClaim | None = None
        if permission_mode in _SOURCE_WRITER_MODES:
            writer_claim, writer_denial = self._acquire_writer_claim(permission_mode)
            if writer_denial is not None:
                return writer_denial
        writer_claim_binding = WriterClaimBinding(writer_claim.claim_id if writer_claim is not None else None)

        try:
            materialized, denial = self._materialize_request_artifacts(
                engine=engine, snapshot=snapshot, scope_id="session-start",
                args=args, image_enabled=image_attachments_enabled,
                context_enabled=governed_context_enabled,
            )
        except Exception:  # noqa: BLE001 -- unexpected materializer faults are pre-record denials
            materialized, denial = None, {
                "status": "DENIED", "code": "DENIED_CONTEXT_INVALID",
                "required_action": "retry without invalid attachment or context references",
            }
        if denial is not None or materialized is None:
            if writer_claim is not None and not self._release_claim(writer_claim.claim_id):
                return self._writer_lease.recovery_denial()
            return denial or {
                "status": "DENIED", "code": "DENIED_CONTEXT_INVALID",
                "required_action": "retry without invalid attachment or context references",
            }
        # requested reflects what the owner ASKED for (profile.model or the legacy top-level model);
        # effective is what actually drives the session (they coincide until a vendor reports a different
        # effective value, an H2.3/H2.4 concern).
        requested_model = prof.get("profile_model") if prof.get("profile_model") is not None else model
        requested_reasoning_effort = prof.get("profile_reasoning_effort")
        requested = {"model": requested_model, "reasoning_effort": requested_reasoning_effort,
                     "permission_mode": permission_mode, "auth_mode": auth_mode, "max_turns": max_turns}
        effective = {"model": eff_model, "reasoning_effort": reasoning_effort,
                     "permission_mode": permission_mode, "auth_mode": auth_mode, "max_turns": max_turns}

        # Vendor launcher/credential/version/sandbox/hook validation happens before C1/T5 and without a
        # prompt. Local retains the stable H2.2 post-record open path. Claude may now be drivable when its
        # installed probe (version/help/effort) passes; the installed Codex descriptor is still denied by
        # the earlier gate until an upgraded app-server build passes the bounded-read schema probe.
        adapter: Any = None
        buffered_recorder: _BufferedRecorder | None = None
        vendor_preopened = engine in ("codex", "claude")
        if vendor_preopened:
            preflight = self._vendor_preflight(
                engine,
                effective,
                snapshot,
                approval_timeout,
                requested_model,
                requested_reasoning_effort,
                writer_claim_binding,
                streaming_enabled,
            )
            if preflight.get("status") != "OK":
                termination_proven = bool(preflight.pop("_termination_proven", False))
                released = writer_claim is None or self._release_claim(
                    writer_claim.claim_id, termination_proven=termination_proven,
                )
                if not termination_proven or not released:
                    return self._writer_lease.recovery_denial()
                return preflight
            adapter = preflight["adapter"]
            buffered_recorder = preflight["recorder"]
            effective = dict(preflight["effective"])
            eff_model = effective.get("model")
            reasoning_effort = effective.get("reasoning_effort")

        attachments_prepared = False
        if adapter is not None:
            attachment_denial = self._stage_adapter_attachments(adapter, materialized.attachments)
            if attachment_denial is not None:
                termination_proven = self._kill_adapter_proven(adapter)
                self._cleanup_vendor_runtime(adapter)
                released = writer_claim is None or self._release_claim(
                    writer_claim.claim_id, termination_proven=termination_proven,
                )
                if not termination_proven or not released:
                    return self._writer_lease.recovery_denial()
                return attachment_denial
            attachments_prepared = True

        # C1 session envelope (kaizen-controlled, orchestrate mode). auth_mode + permission profile ride it.
        try:
            from .. import session_records

            session_payload: dict[str, Any] = {"engine": engine}
            if eff_model:
                session_payload["model"] = eff_model
            # Conversation continuation (owner ask 2026-07-10): the C1 carries its immutable snapshot
            # so a later daemon can rehydrate the ORIGINAL policy inputs, never live rules.
            session_payload["policy_snapshot"] = pack_session_policy_snapshot(
                policy.snapshot_to_json(snapshot), client_features,
            )
            session_ns = _Namespace(
                payload_json=json.dumps(session_payload),
                controller="kaizen", mode="orchestrate", auth_mode=auth_mode, engine=engine,
                permission_mode=permission_mode, requested_model=requested_model,
                requested_reasoning_effort=requested_reasoning_effort, profile_hash=profile_hash,
                title=session_title, summary=f"driven {engine} session", id=None, task_id=None,
                test=self._driven_test_records,
            )
            session_result = session_records.session_start(session_ns)
            session_id = session_result["id"]
        except KaizenDenied as denied:
            termination_proven = adapter is None
            if adapter is not None:
                self._clear_adapter_attachments(adapter)
                termination_proven = self._kill_adapter_proven(adapter)
                self._cleanup_vendor_runtime(adapter)
            released = writer_claim is None or self._release_claim(
                writer_claim.claim_id, termination_proven=termination_proven,
            )
            if not termination_proven or not released:
                return self._writer_lease.recovery_denial()
            return denied.payload()
        except Exception as error:  # noqa: BLE001 -- no preflight child may survive a record-plane error
            termination_proven = adapter is None
            if adapter is not None:
                self._clear_adapter_attachments(adapter)
                termination_proven = self._kill_adapter_proven(adapter)
                self._cleanup_vendor_runtime(adapter)
            released = writer_claim is None or self._release_claim(
                writer_claim.claim_id, termination_proven=termination_proven,
            )
            self.log(f"driven session record start failed: {type(error).__name__}")
            if not termination_proven or not released:
                return self._writer_lease.recovery_denial()
            return {"status": "ERROR", "code": "ERROR_SESSION_RECORD", "engine": engine,
                    "required_action": "repair the session record plane before retrying"}

        # T5 agent-run envelope (agent_type other, surface app-server -- validated enums). session_id
        # soft-links C1; engine is the wire id; model = the EFFECTIVE model; the profile fields ride too.
        try:
            run_payload: dict[str, Any] = {"agent_type": "other", "surface": "app-server",
                                           "session_id": session_id, "engine": engine, "auth_mode": auth_mode,
                                           "permission_mode": permission_mode, "profile_hash": profile_hash}
            if eff_model:
                run_payload["model"] = eff_model
            if requested_model is not None:
                run_payload["requested_model"] = requested_model
            if reasoning_effort:
                run_payload["reasoning_effort"] = reasoning_effort
            if requested_reasoning_effort is not None:
                run_payload["requested_reasoning_effort"] = requested_reasoning_effort
            run_ns = _Namespace(
                payload_json=json.dumps(run_payload),
                summary=f"driven {engine} run", body="", task_id=None,
                agent_type="other", surface="app-server", test=self._driven_test_records,
            )
            run_result = agent_runs.agent_run_start(run_ns)
            agent_run_id = run_result["id"]
        except KaizenDenied as denied:
            termination_proven = adapter is None
            if adapter is not None:
                self._clear_adapter_attachments(adapter)
                termination_proven = self._kill_adapter_proven(adapter)
                self._cleanup_vendor_runtime(adapter)
            self._close_session_record(session_id, "failed")
            released = writer_claim is None or self._release_claim(
                writer_claim.claim_id, termination_proven=termination_proven,
            )
            if not termination_proven or not released:
                return self._writer_lease.recovery_denial()
            return denied.payload()
        except Exception as error:  # noqa: BLE001 -- C1 compensation + child cleanup are mandatory
            termination_proven = adapter is None
            if adapter is not None:
                self._clear_adapter_attachments(adapter)
                termination_proven = self._kill_adapter_proven(adapter)
                self._cleanup_vendor_runtime(adapter)
            self._close_session_record(session_id, "failed")
            released = writer_claim is None or self._release_claim(
                writer_claim.claim_id, termination_proven=termination_proven,
            )
            self.log(f"driven run record start failed ({session_id}): {type(error).__name__}")
            if not termination_proven or not released:
                return self._writer_lease.recovery_denial()
            return {"status": "ERROR", "code": "ERROR_SESSION_RECORD", "session_id": session_id,
                    "engine": engine, "required_action": "repair the agent-run record plane before retrying"}

        # COMPENSATING FINALIZATION: after the C1/T5 inserts, the remaining startup steps (adapter build,
        # bind, waiter wiring, start_session, registry insert, thread start) can fail. Any failure here
        # would otherwise leave a dangling non-terminal run until the boot orphan sweep. Instead: finalize
        # the run 'failed' (T5-atomic, idempotent-tolerant -- also denies any parked waiter, clears
        # ownership, and deregisters via _finalize_driven), then return a structured ERROR rather than
        # raising into the accept loop. A partially-registered session is torn down cleanly.
        try:
            if writer_claim is not None:
                writer_claim = self._writer_lease.promote(
                    writer_claim.claim_id, session_id=session_id, agent_run_id=agent_run_id,
                )
            # Ownership first makes every later post-insert failure sweepable. profile/point remains the
            # first durable event; vendor preflight diagnostics flush only after it.
            self._record_ownership(agent_run_id)
            self._emit_profile_event(agent_run_id, requested, effective, profile_hash)

            if adapter is None:
                adapter_kwargs: dict[str, Any] = {"engine_name": engine, "model": eff_model,
                                                  "approval_timeout": approval_timeout}
                if max_turns is not None:
                    adapter_kwargs["max_turns"] = max_turns
                adapter = self._build_driven_adapter(
                    agent_run_id, adapter_kwargs, snapshot=snapshot,
                    writer_claim_binding=writer_claim_binding,
                )
            adapter.bind_session(session_id)

            session = DrivenSession(
                session_id=session_id, agent_run_id=agent_run_id, adapter=adapter,
                engine=engine, approval_timeout=approval_timeout,
                permission_mode=permission_mode, diff_snapshots=diff_snapshots_enabled,
                image_attachments=image_attachments_enabled, governed_context=governed_context_enabled,
                streaming=streaming_enabled, policy_snapshot=snapshot,
                approval_recheck=self._driven_approval_recheck,
                writer_claim_binding=writer_claim_binding,
            )
            if writer_claim is not None:
                session.set_writer_claim(writer_claim.claim_id)
                child_denial = self._track_current_adapter_child(session)
                if child_denial is not None:
                    raise RuntimeError("persistent vendor child tracking failed")
            adapter.on_approval(session.on_approval)
            if not self._bind_delta_stream(session):
                raise RuntimeError("streaming adapter callback unavailable")
            if not self._bind_approval_broker(session) and self._adapter_factory is None:
                raise _ApprovalBrokerUnavailable("production adapter approval broker unavailable")
            if not self._bind_mutation_guard(session, snapshot) and self._adapter_factory is None:
                raise _MutationInterceptionUnavailable("production adapter mutation guard unavailable")
            # Register the live conversation before any post-record action so compensating finalization
            # can close both C1 and T5. Vendor open already succeeded pre-record; local opens below.
            with self._driven_lock:
                self._driven[agent_run_id] = session
            if vendor_preopened:
                if engine == "codex":
                    self._register_codex_policy_gate(adapter, snapshot, agent_run_id, session_id)
                if buffered_recorder is None:
                    raise RuntimeError("vendor preflight recorder missing")
                buffered_recorder.flush_to(self._make_recorder(agent_run_id))
            elif self._adapter_factory is not None:
                # Preserve the pre-H2 local scripted-adapter seam unchanged.
                adapter.start_session(cwd=str(self.repo_root), profile=effective)
            else:
                adapter.open(effective, snapshot)
            launched = self._launch_driven_turn(
                session, str(prompt), materialized=materialized, owned_claim=writer_claim,
                attachments_prepared=attachments_prepared,
            )
            if launched.get("status") != "OK":
                raise RuntimeError(str(launched.get("code") or "turn launch refused"))
        except Exception as error:  # noqa: BLE001 -- a post-insert startup failure must not dangle the run
            error_type = type(error).__name__
            self.log(f"driven session start failed ({agent_run_id}): {error_type}")
            termination_proven = adapter is None
            if adapter is not None:
                self._clear_adapter_attachments(adapter)
                termination_proven = self._kill_adapter_proven(adapter)
                if not termination_proven:
                    self.log(f"driven startup cleanup unproven ({agent_run_id})")
                self._cleanup_vendor_runtime(adapter)
            registered = self._get_driven(agent_run_id) is not None
            self._finalize_driven(agent_run_id, "failed", {
                "status": "FAILED", "error_code": "ERROR_SESSION_START", "fatal": True,
            }, termination_proven=termination_proven)
            released = registered or writer_claim is None or self._release_claim(
                writer_claim.claim_id, termination_proven=termination_proven,
            )
            interception_failed = isinstance(error, (_MutationInterceptionUnavailable, _ApprovalBrokerUnavailable))
            broker_failed = isinstance(error, _ApprovalBrokerUnavailable)
            if not termination_proven or not released or self._writer_lease.recovery_required:
                return {
                    **self._writer_lease.recovery_denial(), "agent_run_id": agent_run_id,
                    "session_id": session_id, "engine": engine,
                }
            return {"status": "DENIED" if interception_failed else "ERROR",
                    "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED" if interception_failed else "ERROR_SESSION_START",
                    "agent_run_id": agent_run_id,
                    "session_id": session_id, "engine": engine, "message": "adapter failed to start",
                    "required_action": (
                        "repair the production adapter approval broker before retrying"
                        if broker_failed else
                        "repair the production adapter mutation interception before retrying"
                        if interception_failed else
                        "the driven session failed to start; the run was finalized failed"
                    )}
        return {"status": "OK", "session_id": session_id, "agent_run_id": agent_run_id, "engine": engine,
                "profile": effective, "profile_hash": profile_hash, "turn_state": TURN_RUNNING}

    def _gate_driven_engine(self, engine: str) -> dict[str, Any] | None:
        """Return None only for a registered engine whose cached canonical capability is drivable.

        Local keeps the stable H2.2 path. Vendor availability is not inferred from registration: the exact
        structured availability code is returned before C1/T5 unless the installed safety probe explicitly
        opens it. This lets a future compatible capability use the vendor path without weakening today's
        fail-closed Codex/Claude descriptors."""
        registered = _registered_engines()
        internal = "claude_cli" if engine == "claude" else engine
        if internal not in registered:
            return {"status": "DENIED", "code": "DENIED_UNKNOWN_ENGINE", "engine": engine,
                    "registered": registered,
                    "required_action": "select a registered engine from session/capabilities"}
        if engine == "local_llm":
            return None
        capability = self._capability_for_engine(engine)
        if capability is None:
            return {"status": "DENIED", "code": "DENIED_ENGINE_UNAVAILABLE", "engine": engine,
                    "required_action": "refresh session/capabilities and select an available engine"}
        if not capability.get("drivable"):
            availability = capability.get("availability")
            if not isinstance(availability, Mapping):
                availability = {}
            return {
                "status": "DENIED",
                "code": str(availability.get("code") or "DENIED_ENGINE_UNAVAILABLE"),
                "engine": engine,
                "message": str(availability.get("message") or "engine is not safely drivable"),
                "required_action": "resolve the engine availability issue reported by session/capabilities",
            }
        return None

    def _validate_driven_args(self, prompt: Any, model: Any, max_turns: Any,
                              approval_timeout: Any) -> dict[str, Any] | None:
        """Deterministic session/start arg validation. None ⇒ valid. Else a structured DENIED (code +
        required_action). Runs BEFORE any C1/T5 write so bad input leaves ZERO ledger rows. bool is
        rejected for max_turns (a bool is an int subclass but never a turn count); approval_timeout must
        be a finite number > 0 (NaN/inf refused)."""
        import math

        if not isinstance(prompt, str) or not prompt.strip():
            return {"status": "DENIED", "code": "DENIED_PROMPT_INVALID",
                    "required_action": "prompt must be a non-empty string"}
        if model is not None and not isinstance(model, str):
            return {"status": "DENIED", "code": "DENIED_MODEL_INVALID",
                    "required_action": "model must be a string when present"}
        if max_turns is not None and (isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1):
            return {"status": "DENIED", "code": "DENIED_MAX_TURNS_INVALID",
                    "required_action": "max_turns must be an integer >= 1 when present"}
        if approval_timeout is not None:
            if isinstance(approval_timeout, bool) or not isinstance(approval_timeout, (int, float)):
                return {"status": "DENIED", "code": "DENIED_APPROVAL_TIMEOUT_INVALID",
                        "required_action": "approval_timeout must be a finite number > 0 when present"}
            if not math.isfinite(approval_timeout) or approval_timeout <= 0:
                return {"status": "DENIED", "code": "DENIED_APPROVAL_TIMEOUT_INVALID",
                        "required_action": "approval_timeout must be a finite number > 0 when present"}
        return None

    _PROFILE_FIELDS = ("model", "reasoning_effort", "permission_mode", "auth_mode")

    def _validate_profile(self, engine: str, legacy_model: Any, profile: Any,
                          full_opt_in: bool) -> dict[str, Any]:
        """Deterministic H2.1 profile validation (BEFORE any C1/T5 write). Returns either a structured
        DENIED dict (status='DENIED') or a resolved profile dict
        {model, reasoning_effort, permission_mode, auth_mode, profile_model} on success.

        Rules (plan §"Session protocol"):
        - profile must be a dict when present; unknown fields -> DENIED_PROFILE_FIELD_UNKNOWN.
        - legacy top-level ``model`` conflicting with profile.model -> DENIED_MODEL_CONFLICT.
        - permission_mode in PERMISSION_MODES (default 'plan').
        - auth_mode for local_llm must be 'none' (defaulted); reasoning_effort on local_llm ->
          DENIED_PROFILE_UNSUPPORTED (the backend does not expose reasoning effort).
        - permission_mode 'full' without full_opt_in true -> DENIED_FULL_CONFIRMATION_REQUIRED."""
        prof = profile if profile is not None else {}
        if not isinstance(prof, dict):
            return {"status": "DENIED", "code": "DENIED_PROFILE_INVALID",
                    "required_action": "profile must be a JSON object {model?, reasoning_effort?, permission_mode?, auth_mode?}"}
        unknown = [k for k in prof if k not in self._PROFILE_FIELDS]
        if unknown:
            return {"status": "DENIED", "code": "DENIED_PROFILE_FIELD_UNKNOWN", "unknown": unknown,
                    "allowed": list(self._PROFILE_FIELDS),
                    "required_action": "profile accepts only model|reasoning_effort|permission_mode|auth_mode"}

        profile_model = prof.get("model")
        if profile_model is not None and not isinstance(profile_model, str):
            return {"status": "DENIED", "code": "DENIED_MODEL_INVALID",
                    "required_action": "profile.model must be a string when present"}
        # Legacy top-level model is temporarily accepted; a conflict with profile.model is DENIED.
        if legacy_model is not None and profile_model is not None and legacy_model != profile_model:
            return {"status": "DENIED", "code": "DENIED_MODEL_CONFLICT",
                    "legacy_model": legacy_model, "profile_model": profile_model,
                    "required_action": "pass model via profile.model OR the legacy top-level model, not both with different values"}
        capability = self._capability_for_engine(engine) if engine != "local_llm" else None
        eff_model = profile_model if profile_model is not None else legacy_model
        if eff_model is None and capability is not None:
            eff_model = capability.get("default_model")

        permission_mode = prof.get("permission_mode") or "plan"
        if permission_mode not in policy.PERMISSION_MODES:
            return {"status": "DENIED", "code": "DENIED_PERMISSION_MODE_INVALID",
                    "permission_mode": permission_mode, "allowed": list(policy.PERMISSION_MODES),
                    "required_action": "permission_mode must be plan|ask|agent|full"}
        if capability is not None:
            advertised_permissions = list(capability.get("permission_modes") or [])
            if permission_mode not in advertised_permissions:
                return {"status": "DENIED", "code": "DENIED_PROFILE_UNSUPPORTED",
                        "field": "permission_mode", "permission_mode": permission_mode,
                        "allowed": advertised_permissions,
                        "required_action": "select only a permission mode advertised by session/capabilities"}

        requested_reasoning_effort = prof.get("reasoning_effort")
        reasoning_effort = requested_reasoning_effort
        if reasoning_effort is None and capability is not None:
            reasoning_effort = capability.get("default_reasoning_effort")
        if reasoning_effort is not None and engine == "local_llm":
            return {"status": "DENIED", "code": "DENIED_PROFILE_UNSUPPORTED", "field": "reasoning_effort",
                    "required_action": "the local_llm backend does not expose reasoning effort; omit it"}
        if reasoning_effort is not None and not isinstance(reasoning_effort, str):
            return {"status": "DENIED", "code": "DENIED_PROFILE_UNSUPPORTED", "field": "reasoning_effort",
                    "required_action": "reasoning_effort must be an advertised string value"}
        selected = None
        if capability is not None:
            selected = next(
                (entry for entry in (capability.get("models") or [])
                 if isinstance(entry, Mapping) and entry.get("id") == eff_model),
                None,
            )
        if engine == "claude" and selected is None:
            return {"status": "DENIED", "code": "DENIED_MODEL_UNAVAILABLE", "field": "model",
                    "model": eff_model,
                    "required_action": "refresh the account model catalog and select one exact returned id"}
        if selected is not None:
            advertised_efforts = list(selected.get("reasoning_efforts") or [])
            if engine == "claude" and advertised_efforts and requested_reasoning_effort is None:
                return {"status": "DENIED", "code": "DENIED_EFFORT_UNSUPPORTED",
                        "field": "reasoning_effort", "reasoning_effort": requested_reasoning_effort,
                        "allowed": advertised_efforts,
                        "required_action": "select a reasoning effort advertised for the selected model"}
            if reasoning_effort is not None and reasoning_effort not in advertised_efforts:
                return {"status": "DENIED", "code": "DENIED_EFFORT_UNSUPPORTED",
                        "field": "reasoning_effort", "reasoning_effort": reasoning_effort,
                        "allowed": advertised_efforts,
                        "required_action": "select a reasoning effort advertised for the selected model"}

        auth_mode = prof.get("auth_mode")
        if engine == "local_llm":
            if auth_mode not in (None, "none"):
                return {"status": "DENIED", "code": "DENIED_AUTH_MODE_INVALID", "auth_mode": auth_mode,
                        "required_action": "local_llm auth_mode must be none"}
            auth_mode = "none"
        elif engine == "claude":
            auth_mode = auth_mode or "subscription"
            if auth_mode != "subscription":
                return {"status": "DENIED", "code": "DENIED_AUTH_MODE_MISMATCH", "field": "auth_mode",
                        "required_action": "Claude uses only the existing vendor-managed subscription identity"}
            advertised_auth = list((capability or {}).get("auth_modes") or [])
            if "subscription" not in advertised_auth:
                return {"status": "DENIED", "code": "DENIED_AUTH_UNAVAILABLE", "field": "auth_mode",
                        "required_action": "complete the external vendor-owned subscription authentication flow"}
        else:
            auth_mode = auth_mode or "subscription"
            advertised_auth = list((capability or {}).get("auth_modes") or [])
            if auth_mode not in ("subscription", "api-key") or auth_mode not in advertised_auth:
                return {"status": "DENIED", "code": "DENIED_PROFILE_UNSUPPORTED", "field": "auth_mode",
                        "auth_mode": auth_mode, "allowed": advertised_auth,
                        "required_action": "vendor auth_mode must be subscription|api-key and advertised by session/capabilities"}

        if permission_mode == "full" and not full_opt_in:
            return {"status": "DENIED", "code": "DENIED_FULL_CONFIRMATION_REQUIRED",
                    "required_action": "Full mode needs a fresh, never-persisted confirmation: resend with full_opt_in true"}

        return {"model": eff_model, "reasoning_effort": reasoning_effort,
                "permission_mode": permission_mode, "auth_mode": auth_mode, "profile_model": profile_model,
                "profile_reasoning_effort": requested_reasoning_effort}

    def _build_session_snapshot(self, engine: str, permission_mode: str) -> Any:
        """Cut the immutable per-session PolicySnapshot (H2.1). Designated roots come from the mode
        profile's ``designated_write_roots`` (repo-relative, root-validated at set time) resolved against
        repo_root; rules load from the DB; workspace_root = repo_root. build_policy_snapshot always merges
        the shipped protected floor and materializes the canonical forms + profile_hash."""
        from .. import session_records

        mode_profile = session_records.get_mode_profile(permission_mode)
        rel_roots = mode_profile.get("designated_write_roots") or []
        designated = [str((self.repo_root / r)) for r in rel_roots]
        rules = policy.load_rules()
        return policy.build_policy_snapshot(
            engine=engine, permission_mode=permission_mode,
            workspace_root=str(self.repo_root), designated_write_roots=designated, rules=rules,
        )

    def _emit_profile_event(self, agent_run_id: str, requested: dict[str, Any],
                            effective: dict[str, Any], profile_hash: str,
                            *, resume_metadata: Mapping[str, Any] | None = None) -> None:
        """Emit the profile/point event as the FIRST stream event after T5 (plan §"Durable events"). The
        body is NON-SECRET JSON: requested profile, effective profile, profile_hash, permission-mode
        version. Best-effort: a funnel failure is logged, never fatal (the run already exists)."""
        body_value: dict[str, Any] = {
            "requested": requested, "effective": effective, "profile_hash": profile_hash,
            "permission_mode_version": policy.PERMISSION_MODE_VERSION,
        }
        if resume_metadata is not None:
            body_value.update(dict(resume_metadata))
        body = json.dumps(body_value)
        try:
            self.funnel_event(agent_run_id, "profile", "point",
                              summary=f"profile {effective.get('permission_mode')} ({profile_hash[:12]})",
                              body=body)
        except Exception as error:  # noqa: BLE001 -- profile record is best-effort; never fatal
            self.log(f"profile event funnel failed ({agent_run_id}): {error}")

    # --- H2.1 session request-shape validators (colocated; H2.2 reuses them for turn/close/list) ------

    @staticmethod
    def _validate_artifact_refs(args: Mapping[str, Any]) -> dict[str, Any] | None:
        """Validates attachments/context_refs via SessionProtocol validators; None => valid, else structured DENIED (code/field from SessionProtocolError)."""
        try:
            attachments = validate_image_refs(args.get("attachments"))
            context_refs = validate_context_refs(args.get("context_refs"))
        except SessionProtocolError as error:
            return {
                "status": "DENIED", "code": error.code, "field": error.field,
                "required_action": "correct or remove the invalid reference before retrying",
            }
        return None

    def _materialize_request_artifacts(
        self,
        *,
        engine: str,
        snapshot: policy.PolicySnapshot,
        scope_id: str,
        args: Mapping[str, Any],
        image_enabled: bool,
        context_enabled: bool,
    ) -> tuple[MaterializedTurn | None, dict[str, Any] | None]:
        """Delegates to _session_artifacts.materialize; returns (MaterializedTurn | None, denial | None); an unproven rollback flips the writer lease to recovery-required and returns recovery_denial."""
        try:
            return self._session_artifacts.materialize(
                engine=engine, snapshot=snapshot, scope_id=scope_id,
                attachments=args.get("attachments"), context_refs=args.get("context_refs"),
                image_supported=image_enabled, context_supported=context_enabled,
            ), None
        except SessionArtifactError as error:
            if error.rollback_unproven:
                self._writer_lease.require_recovery("context artifact rollback was unproven")
                return None, self._writer_lease.recovery_denial()
            return None, error.payload()

    @staticmethod
    def _safe_session_title(prompt: str, hint: Any) -> tuple[str | None, dict[str, Any] | None]:
        """Derives canonical title from prompt (dropped if redaction denies); a non-canonical or mismatched hint => DENIED_TITLE_INVALID; returns (title | None, denial | None)."""
        title = canonical_title(prompt)
        if title is not None:
            try:
                assert_redacted({"title": title})
            except KaizenDenied:
                title = None
        if hint is not None and (
            not isinstance(hint, str) or canonical_title(hint) != hint or hint != title
        ):
            return None, {
                "status": "DENIED", "code": "DENIED_TITLE_INVALID", "field": "title",
                "required_action": "omit title or send the daemon-canonical first-prompt title",
            }
        return title, None

    def validate_turn_request(self, args: dict[str, Any]) -> dict[str, Any] | None:
        """session/turn shape: {agent_run_id (str), prompt (non-empty str)}. None ⇒ valid. The H2.2 op
        handler is ``_handle_session_turn``."""
        if not isinstance(args.get("agent_run_id"), str) or not args["agent_run_id"]:
            return {"status": "DENIED", "code": "DENIED_AGENT_RUN_REQUIRED",
                    "required_action": "session/turn needs an agent_run_id"}
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"status": "DENIED", "code": "DENIED_PROMPT_INVALID",
                    "required_action": "session/turn needs a non-empty prompt"}
        return self._validate_artifact_refs(args)

    def validate_close_request(self, args: dict[str, Any]) -> dict[str, Any] | None:
        """session/close shape: {agent_run_id (str)}. None ⇒ valid."""
        if not isinstance(args.get("agent_run_id"), str) or not args["agent_run_id"]:
            return {"status": "DENIED", "code": "DENIED_AGENT_RUN_REQUIRED",
                    "required_action": "session/close needs an agent_run_id"}
        return None

    def validate_list_request(self, args: dict[str, Any]) -> dict[str, Any] | None:
        """session/list shape: {controller? in driven|observed, limit? int 1..1000}. None ⇒ valid."""
        controller = args.get("controller")
        if controller is not None and controller not in ("driven", "observed"):
            return {"status": "DENIED", "code": "DENIED_LIST_CONTROLLER_INVALID", "controller": controller,
                    "required_action": "controller must be driven|observed when present"}
        limit = args.get("limit")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000):
            return {"status": "DENIED", "code": "DENIED_LIST_LIMIT_INVALID",
                    "required_action": "limit must be an integer 1..1000 when present"}
        return None

    # --- H2.2 open/running/idle/terminal lifecycle --------------------------------------------

    _CHAT_PLACEHOLDERS = {
        "DENIED_CHAT_MESSAGE_REDACTED": "[message omitted: redaction policy]",
        "DENIED_CHAT_MESSAGE_OVERSIZE": "[message omitted: exceeds 1 MiB limit]",
    }

    def _record_chat_message(self, agent_run_id: str, role: str, text: str,
                             *, turn_id: str | None = None, source: str = "driven",
                             source_event_id: str | None = None,
                             correlation_id: str | None = None,
                             attachments: tuple[dict[str, Any], ...] = (),
                             context_refs: tuple[dict[str, Any], ...] = ()) -> str:
        """Persist one complete message, substituting an explicit safe placeholder on denial.

        ``agent_runs.agent_event_add`` owns the H2.1 body schema, redaction scan, and byte cap. A rejected
        raw body never consumes a sequence number; the placeholder write therefore keeps replay gapless.
        Returns the text actually persisted so a later T8 body cannot reintroduce rejected content.
        """

        body: dict[str, Any] = {"role": role, "text": text, "source": source}
        if turn_id:
            body["turn_id"] = turn_id
        if attachments:
            body["attachments"] = [dict(item) for item in attachments]
        if context_refs:
            body["context_refs"] = [dict(item) for item in context_refs]
        event_correlation = correlation_id or turn_id
        try:
            self.funnel_event(
                agent_run_id,
                "chat_message",
                "point",
                summary=f"{source} {role} message",
                correlation_id=event_correlation,
                source_event_id=source_event_id,
                body=json.dumps(body),
            )
            return text
        except KaizenDenied as denied:
            placeholder = self._CHAT_PLACEHOLDERS.get(denied.code)
            if placeholder is None:
                raise
            body["text"] = placeholder
            self.funnel_event(
                agent_run_id,
                "chat_message",
                "point",
                summary=f"{source} {role} message omitted",
                correlation_id=event_correlation,
                code=denied.code,
                source_event_id=source_event_id,
                body=json.dumps(body),
            )
            return placeholder

    @staticmethod
    def _clear_adapter_attachments(adapter: Any) -> bool:
        """Return True when clear/set hooks prove the one-turn attachment envelope empty or no hooks exist; return False only when a hook raises."""

        clearer = getattr(adapter, "clear_next_turn_artifacts", None)
        setter = getattr(adapter, "set_next_turn_artifacts", None)
        try:
            if callable(clearer):
                clearer()
                return True
            if callable(setter):
                setter([])
                return True
        except Exception:
            return False
        return True

    def _stage_adapter_attachments(
        self,
        adapter: Any,
        attachments: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Stage one attachment envelope or fail closed and clear it.

        Adapter staging refs must be content-addressed and reclaimed by its clear/session teardown contract,
        including refs created before a later item fails.
        """

        if not self._clear_adapter_attachments(adapter):
            return {"status": "DENIED", "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                    "required_action": "restart the provider runtime before retrying the turn"}
        if not attachments:
            return None
        stager = getattr(adapter, "stage_attachment", None)
        setter = getattr(adapter, "set_next_turn_artifacts", None)
        if not callable(stager) or not callable(setter):
            return {"status": "DENIED", "code": "DENIED_ATTACHMENT_UNSUPPORTED",
                    "required_action": "refresh capabilities and use an engine with image support"}
        staged: list[dict[str, Any]] = []
        try:
            for item in attachments:
                content = self._artifact_cache.read(
                    "images", str(item["artifact_ref"]),
                    expected_sha256=str(item["sha256"]), expected_bytes=int(item["bytes"]),
                )
                worker_ref = stager(content, media_type=str(item["media_type"]))
                staged.append({"worker_ref": worker_ref})
            setter(staged)
            return None
        except Exception as error:  # noqa: BLE001 -- cache/worker staging fails closed before provider call
            if not self._clear_adapter_attachments(adapter):
                return {"status": "DENIED", "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                        "required_action": "restart the provider runtime before retrying the turn"}
            code = getattr(error, "code", "DENIED_ATTACHMENT_INVALID")
            return {"status": "DENIED", "code": str(code),
                    "required_action": "remove the invalid image and retry the turn"}

    @staticmethod
    def _turn_denial(session: DrivenSession, denial: str) -> dict[str, Any]:
        action = "wait for the active turn to become idle" if denial == "DENIED_TURN_IN_PROGRESS" \
            else "start a new conversation"
        return {"status": "DENIED", "code": denial, "agent_run_id": session.agent_run_id,
                "turn_state": session.turn_state, "required_action": action}

    def _reserve_driven_turn(
        self,
        session: DrivenSession,
        *,
        owned_claim: WriterClaim | None = None,
    ) -> tuple[TurnReservation | None, WriterClaim | None, dict[str, Any] | None]:
        """Return (reservation, request-owned writer claim, denial): reserve state first, bind only this request's claim, and refuse an idle source session that retained a prior writer token."""

        reservation, state_denial = session.reserve_turn()
        if state_denial is not None:
            return None, None, self._turn_denial(session, state_denial)
        assert reservation is not None

        request_claim = owned_claim
        if session.permission_mode not in _SOURCE_WRITER_MODES:
            return reservation, None, None

        token = session.writer_claim_token
        if owned_claim is None:
            # An idle source-capable session must never carry a prior request's token. Refuse instead of
            # verifying/borrowing it; this is the claim-borrow race's fail-closed boundary.
            if token is not None:
                session.rollback_turn(reservation)
                self._writer_lease.require_recovery("idle driven session retained another request's claim")
                return None, None, self._writer_lease.recovery_denial()
            request_claim, writer_denial = self._acquire_writer_claim(
                session.permission_mode, session_id=session.session_id,
                agent_run_id=session.agent_run_id,
            )
            if writer_denial is not None:
                session.rollback_turn(reservation)
                return None, None, writer_denial
            if request_claim is None:  # defensive: acquire returns exactly one of claim/denial
                session.rollback_turn(reservation)
                return None, None, self._writer_lease.recovery_denial()
            session.set_writer_claim(request_claim.claim_id)
        elif token != owned_claim.claim_id:
            session.rollback_turn(reservation)
            self._writer_lease.require_recovery("preowned writer claim was not bound to its request session")
            return None, None, self._writer_lease.recovery_denial()

        writer_denial = self._writer_lease.verify(
            request_claim.claim_id, agent_run_id=session.agent_run_id,
        )
        if writer_denial is not None:
            return reservation, request_claim, self._cancel_reserved_turn(
                session, reservation, request_claim, writer_denial,
            )
        child_denial = self._track_current_adapter_child(session)
        if child_denial is not None:
            return reservation, request_claim, self._cancel_reserved_turn(
                session, reservation, request_claim, child_denial,
            )
        return reservation, request_claim, None

    def _cancel_reserved_turn(
        self,
        session: DrivenSession,
        reservation: TurnReservation,
        request_claim: WriterClaim | None,
        denial: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Undo a prelaunch request exactly; uncertainty terminalizes and retains recovery."""

        from .adapters import TurnResult

        attachments_cleared = self._clear_adapter_attachments(session.adapter)
        claim_released = True
        claim_cleared = True
        if request_claim is not None:
            claim_released = self._release_claim(request_claim.claim_id)
            if claim_released:
                claim_cleared = session.clear_writer_claim(request_claim.claim_id)
        rolled_back = claim_released and claim_cleared and attachments_cleared \
            and session.rollback_turn(reservation)
        if not rolled_back:
            termination_proven = self._kill_adapter_proven(session.adapter)
            session.mark_terminal()
            self._finalize_driven(
                session.agent_run_id, "failed",
                TurnResult("FAILED", error_code="DENIED_WORKSPACE_RECOVERY_REQUIRED", fatal=True).as_dict(),
                termination_proven=termination_proven,
            )
            return {**self._writer_lease.recovery_denial(), "agent_run_id": session.agent_run_id,
                    "turn_state": TURN_TERMINAL}
        return dict(denial)

    def _launch_driven_turn(
        self,
        session: DrivenSession,
        prompt: str,
        adapter_prompt: str | None = None,
        *,
        materialized: MaterializedTurn | None = None,
        adapter_prompt_is_final: bool = False,
        turn_reserved: bool = False,
        reservation: TurnReservation | None = None,
        request_claim: WriterClaim | None = None,
        owned_claim: WriterClaim | None = None,
        attachments_prepared: bool = False,
    ) -> dict[str, Any]:
        """Persist the raw user prompt and launch its adapter worker; either consume the supplied pre-reserved reservation/claims/prepared attachments or reserve/stage them here, while adapter_prompt is the model-only continuation text."""

        if not turn_reserved:
            reservation, request_claim, reservation_denial = self._reserve_driven_turn(
                session, owned_claim=owned_claim,
            )
            if reservation_denial is not None:
                return reservation_denial
        if reservation is None:
            return self._writer_lease.recovery_denial()
        # The accepted prompt is durable before the worker can call the model. The adapter receives the
        # original text; only persistence substitutes a placeholder when the redaction gate requires it.
        turn_artifacts = materialized or MaterializedTurn()
        if not attachments_prepared:
            attachment_denial = self._stage_adapter_attachments(session.adapter, turn_artifacts.attachments)
            if attachment_denial is not None:
                return self._cancel_reserved_turn(session, reservation, request_claim, attachment_denial)
        if not session.commit_turn(reservation):
            return self._cancel_reserved_turn(session, reservation, request_claim, {
                "status": "DENIED", "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                "required_action": "restart the daemon before retrying the turn",
            })
        if turn_artifacts.attachments:
            try:
                self._artifact_cache.add_image_reference(
                    [item["artifact_ref"] for item in turn_artifacts.attachments], session.session_id,
                )
            except Exception as error:  # noqa: BLE001 -- committed turn must terminalize truthfully
                self._clear_adapter_attachments(session.adapter)
                if bool(getattr(error, "rollback_unproven", False)):
                    self._writer_lease.require_recovery("image retention metadata rollback was unproven")
                raise
        self._record_chat_message(
            session.agent_run_id, "user", prompt,
            attachments=turn_artifacts.attachments, context_refs=turn_artifacts.context_refs,
        )
        model_prompt = (
            adapter_prompt
            if adapter_prompt is not None and adapter_prompt_is_final
            else compose_governed_prompt(
                adapter_prompt if adapter_prompt is not None else prompt,
                turn_artifacts.runtime_context,
            )
        )

        def record_assistant(result: Any) -> Any:
            """Records the assistant chat_message with redaction-safe placeholder substitution + first-writer-wins vendor-root persistence; returns a TurnResult carrying the safe (possibly substituted) text."""
            from .adapters import TurnResult

            stream_turn_id = session.complete_delta_turn()
            safe_text = self._record_chat_message(
                session.agent_run_id,
                "assistant",
                str(result.final_text),
                turn_id=result.vendor_turn_id,
                correlation_id=stream_turn_id or result.vendor_turn_id,
            )
            # Durable vendor resume key (continuation across daemon restarts): first-writer-wins on the
            # C1 row; bookkeeping only, never fails the turn.
            vendor_root = getattr(session.adapter, "vendor_session_id", None)
            if vendor_root:
                try:
                    from .. import session_records

                    session_records.set_vendor_session_root(session.session_id, str(vendor_root))
                except Exception as error:  # noqa: BLE001 -- bookkeeping must not affect the turn
                    self.log(f"vendor-root persistence failed ({session.session_id}): {type(error).__name__}")
            return TurnResult(
                status=result.status,
                vendor_turn_id=result.vendor_turn_id,
                final_text=safe_text,
                error_code=result.error_code,
                fatal=result.fatal,
            )

        thread = threading.Thread(
            target=run_driven_turn,
            args=(session, session.adapter, model_prompt),
            kwargs={
                "finalize": lambda conclusion, result, termination_proven,
                rid=session.agent_run_id: self._finalize_driven(
                    rid, conclusion, result, termination_proven=termination_proven
                ),
                "record_assistant": record_assistant,
                "turn_finally": self._writer_turn_finally,
                "logger": self.log,
            },
            name=f"driven-turn-{session.agent_run_id}",
            daemon=True,
        )
        session.thread = thread
        try:
            thread.start()
        except Exception:
            session.mark_terminal()
            raise
        with session.lock:
            turn_state = session.state
            turn_result = session.result
        if turn_state == TURN_TERMINAL:
            return {
                "status": "ERROR",
                "code": str(getattr(turn_result, "error_code", None) or "ERROR_SESSION_TURN"),
                "agent_run_id": session.agent_run_id,
                "turn_state": turn_state,
            }
        return {"status": "OK", "agent_run_id": session.agent_run_id, "turn_state": turn_state}

    _ORPHAN_FINALIZE_SUMMARY = "orphan-sweep force-finalize: owning daemon dead"

    def _driven_continuation_history(self, session_id: str) -> _DurableContinuationHistory:
        """Load complete durable text plus validated artifact metadata, never historical bytes."""
        from .. import db

        rows = db.fetch_all(
            "SELECT e.body FROM agent_events e JOIN agent_runs r ON e.agent_run_id = r.id "
            "WHERE r.session_id = ? AND e.event_kind = 'chat_message' "
            "ORDER BY r.created_at, r.id, e.sequence_no",
            (session_id,),
        )
        messages: list[dict[str, Any]] = []
        invalid = 0
        unavailable: dict[tuple[str, str, int, str], dict[str, Any]] = {}
        availability_cache: dict[tuple[str, str, str, int], str] = {}

        def artifact_availability(kind: str, reference: str, digest: str, byte_count: int) -> str:
            key = (kind, reference, digest, byte_count)
            if key not in availability_cache:
                availability_cache[key] = self._artifact_cache.availability(
                    kind, reference, expected_sha256=digest, expected_bytes=byte_count,
                )
            return availability_cache[key]

        for (body,) in rows:
            try:
                message = json.loads(body) if body else {}
                if not isinstance(message, Mapping):
                    raise ValueError("chat body is not an object")
                role = message.get("role")
                text = message.get("text")
                source = message.get("source")
                if role not in ("user", "assistant") or not isinstance(text, str) or source not in ("driven", "observed"):
                    raise ValueError("chat body is incomplete")
                attachments = validate_image_refs(message.get("attachments"))
                context_refs = validate_durable_context_refs(message.get("context_refs"))
            except (ValueError, SessionProtocolError):
                invalid += 1
                continue
            clean: dict[str, Any] = {"role": role, "text": text}
            if attachments:
                clean_attachments: list[dict[str, Any]] = []
                for item in attachments:
                    availability = artifact_availability(
                        "images", str(item["artifact_ref"]), str(item["sha256"]), int(item["bytes"]),
                    )
                    clean_attachments.append({**item, "availability": availability})
                    if availability != "available":
                        metadata = {
                            "kind": "image", "sha256": item["sha256"], "bytes": item["bytes"],
                            "availability": availability,
                        }
                        if item.get("name"):
                            metadata["name"] = item["name"]
                        unavailable[("image", item["sha256"], item["bytes"], availability)] = metadata
                clean["attachments"] = clean_attachments
            if context_refs:
                clean_context: list[dict[str, Any]] = []
                for item in context_refs:
                    reference = item.get("snapshot_ref") or f"sha256:{item['sha256']}"
                    availability = artifact_availability(
                        "context", str(reference), str(item["sha256"]), int(item["bytes"]),
                    )
                    clean_context.append({**item, "availability": availability})
                    if availability != "available":
                        metadata = {
                            "kind": item["kind"], "sha256": item["sha256"], "bytes": item["bytes"],
                            "availability": availability,
                        }
                        unavailable[(item["kind"], item["sha256"], item["bytes"], availability)] = metadata
                clean["context_refs"] = clean_context
            messages.append(clean)
        expired = tuple(unavailable[key] for key in sorted(unavailable))
        return _DurableContinuationHistory(tuple(messages), invalid, expired)

    @staticmethod
    def _profile_event_max_turns(profile_body: Any, engine: str) -> int | None:
        """Read durable max_turns; cap legacy Claude values at the current ceiling."""

        if not isinstance(profile_body, Mapping):
            return None
        effective = profile_body.get("effective")
        value = effective.get("max_turns") if isinstance(effective, Mapping) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return None
        return min(value, _CLAUDE_MAX_TURNS_CEILING) if engine == "claude" else value

    def _durable_max_turns(self, agent_run_id: str, engine: str) -> int | None:
        """Recover max_turns from T6 profile/point; legacy Claude legs use the documented default."""

        from .. import db

        row = db.fetch_one(
            "SELECT body FROM agent_events WHERE agent_run_id = ? AND event_kind = 'profile' "
            "AND marker = 'point' ORDER BY sequence_no DESC LIMIT 1",
            (agent_run_id,),
        )
        try:
            body = json.loads(row[0]) if row and isinstance(row[0], str) else None
        except ValueError:
            body = None
        selected = self._profile_event_max_turns(body, engine)
        return selected if selected is not None \
            else _CLAUDE_DEFAULT_MAX_TURNS if engine == "claude" else None

    def _resume_lock_for_run(self, agent_run_id: str) -> threading.RLock | None:
        """Return the stable admission lock for the run's C1 conversation."""

        from .. import db

        row = db.fetch_one("SELECT session_id FROM agent_runs WHERE id = ?", (agent_run_id,))
        if row is None or not row[0]:
            return None
        session_id = str(row[0])
        with self._resume_locks_guard:
            return self._resume_locks.setdefault(session_id, threading.RLock())

    def _resume_driven_conversation(
        self,
        agent_run_id: str,
        prompt: str,
        request_args: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Continue ANY driven conversation as a NEW linked T5 under its C1 (owner decision 2026-07-11:
        no conversation dead-ends -- 'ended' is not a thing; closed/killed/legacy rows all continue).

        Eligibility (None -> the caller's standard denial): the run has a C1 link to a driven envelope;
        it is the LATEST leg (no forking older legs); that leg is TERMINAL (a live leg elsewhere means
        the conversation is not ours to touch); no live leg is registered on this daemon; the engine
        still gates drivable. Fidelity tiers, recorded honestly: a stored snapshot rehydrates VERBATIM
        (rule edits since never leak in); a legacy row without one gets a FRESH snapshot cut from
        current policy and HEALED onto the C1 (new profile_hash, recorded in the resumed leg's profile
        event). A vendor conversation with its persisted resume key re-attaches full vendor context;
        without one the first resumed turn is TRANSCRIPT-SEEDED (the durable chat history rides a
        context preamble). approval_timeout is not persisted. max_turns is recovered from the existing
        durable profile/point body (legacy Claude legs use the documented default)."""
        from .. import db

        run_row = db.fetch_one(
            "SELECT session_id, engine, auth_mode, permission_mode, model, reasoning_effort, "
            "requested_model, requested_reasoning_effort FROM agent_runs WHERE id = ?",
            (agent_run_id,),
        )
        if run_row is None or not run_row[0]:
            return None
        session_id = str(run_row[0])
        engine = normalize_engine(run_row[1])
        if not engine:
            return None
        session_row = db.fetch_one(
            "SELECT controller, state, vendor_session_root_id, policy_snapshot, auth_mode, "
            "permission_mode, profile_hash, requested_model, requested_reasoning_effort "
            "FROM agent_sessions WHERE id = ?",
            (session_id,),
        )
        if session_row is None or session_row[0] != "kaizen":
            return None
        latest = db.fetch_one(
            "SELECT id FROM agent_runs WHERE session_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (session_id,),
        )
        if latest is None or str(latest[0]) != agent_run_id:
            return None
        reduced = self._safe_reduce(agent_run_id)
        if reduced is None or not reduced.get("terminal"):
            return None  # a non-terminal leg is live somewhere (or dangling until the next boot sweep)
        with self._driven_lock:
            if any(live.session_id == session_id for live in self._driven.values()):
                return None
        gate = self._gate_driven_engine(engine)
        if gate is not None:
            return gate  # structured denial: the engine is no longer drivable

        auth_mode = str(run_row[2] or session_row[4] or "none")
        permission_mode = str(run_row[3] or session_row[5] or "plan")
        snapshot: policy.PolicySnapshot | None = None
        snapshot_json = session_row[3]
        client_features = read_session_client_features(snapshot_json)
        streaming_enabled = self._engine_feature(engine, "streaming")
        image_attachments_enabled = self._engine_feature(engine, "image_attachments")
        governed_context_enabled = self._engine_feature(engine, "governed_context")
        diff_snapshots_enabled = (
            client_features.get("diff_snapshots") is True
            and self._engine_feature(engine, "diff_snapshots")
        )
        if snapshot_json:
            try:
                snapshot = policy.snapshot_from_json(str(snapshot_json))
            except (ValueError, KeyError, TypeError):
                self.log(f"resume ({session_id}): stored policy snapshot unreadable; cutting a fresh one")
        snapshot_rebuilt = snapshot is None
        if snapshot is None:
            # Legacy healing: cut a fresh immutable snapshot from CURRENT policy and persist it -- the
            # conversation becomes fully durable from this leg onward.
            snapshot = self._build_session_snapshot(engine, permission_mode)

        vendor_root = str(session_row[2] or "")
        native_vendor_resume = engine in ("codex", "claude") and bool(vendor_root)
        history = self._driven_continuation_history(session_id)

        writer_claim: WriterClaim | None = None
        if permission_mode in _SOURCE_WRITER_MODES:
            writer_claim, writer_denial = self._acquire_writer_claim(
                permission_mode, session_id=session_id,
            )
            if writer_denial is not None:
                return writer_denial
        writer_claim_binding = WriterClaimBinding(writer_claim.claim_id if writer_claim is not None else None)

        try:
            materialized, artifact_denial = self._materialize_request_artifacts(
                engine=engine, snapshot=snapshot, scope_id=session_id,
                args=request_args or {}, image_enabled=image_attachments_enabled,
                context_enabled=governed_context_enabled,
            )
        except Exception:  # noqa: BLE001 -- unexpected materializer faults stay pre-record
            materialized, artifact_denial = None, {
                "status": "DENIED", "code": "DENIED_CONTEXT_INVALID",
                "required_action": "retry without invalid attachment or context references",
            }
        if artifact_denial is not None or materialized is None:
            if writer_claim is not None and not self._release_claim(writer_claim.claim_id):
                return self._writer_lease.recovery_denial()
            return artifact_denial or {
                "status": "DENIED", "code": "DENIED_CONTEXT_INVALID",
                "required_action": "retry without invalid attachment or context references",
            }
        if native_vendor_resume:
            adapter_prompt = compose_governed_prompt(prompt, materialized.runtime_context)
            if len(adapter_prompt.encode("utf-8")) > CONTINUATION_MAX_BYTES:
                if writer_claim is not None and not self._release_claim(writer_claim.claim_id):
                    return self._writer_lease.recovery_denial()
                return {
                    "status": "DENIED", "code": "DENIED_CONTEXT_TOO_LARGE", "field": "prompt",
                    "limit_bytes": CONTINUATION_MAX_BYTES,
                    "required_action": "reduce the prompt or current governed context before retrying",
                }
            resume_fidelity = "full"
            omitted_message_count = 0
        else:
            try:
                continuation = build_continuation_prompt(
                    history.messages, prompt, materialized.runtime_context,
                    already_omitted=history.invalid_message_count,
                )
            except ContinuationTooLarge:
                if writer_claim is not None and not self._release_claim(writer_claim.claim_id):
                    return self._writer_lease.recovery_denial()
                return {
                    "status": "DENIED", "code": "DENIED_CONTEXT_TOO_LARGE", "field": "prompt",
                    "limit_bytes": CONTINUATION_MAX_BYTES,
                    "required_action": "reduce the prompt or current governed context before retrying",
                }
            adapter_prompt = continuation.adapter_prompt
            resume_fidelity = "reduced"
            omitted_message_count = continuation.omitted_message_count
        resume_metadata = validate_resume_metadata({
            "resume_fidelity": resume_fidelity,
            "omitted_message_count": omitted_message_count,
            "expired_artifacts": list(history.expired_artifacts),
        })
        if snapshot_rebuilt:
            try:
                from .. import session_records

                session_records.set_policy_snapshot(session_id, policy.snapshot_to_json(snapshot))
            except Exception as error:  # noqa: BLE001 -- healing is best-effort; the resume proceeds
                self.log(f"resume snapshot healing failed ({session_id}): {type(error).__name__}")
        transcript_seeded = resume_fidelity == "reduced"

        eff_model = run_row[4]
        reasoning_effort = run_row[5]
        max_turns = self._durable_max_turns(agent_run_id, engine)
        requested_model = run_row[6] if run_row[6] is not None else session_row[7]
        requested_reasoning_effort = run_row[7] if run_row[7] is not None else session_row[8]
        profile_hash = snapshot.profile_hash if snapshot_rebuilt else str(session_row[6] or snapshot.profile_hash)
        effective = {"model": eff_model, "reasoning_effort": reasoning_effort,
                     "permission_mode": permission_mode, "auth_mode": auth_mode,
                     "max_turns": max_turns}
        requested = {"model": requested_model, "reasoning_effort": requested_reasoning_effort,
                     "permission_mode": permission_mode, "auth_mode": auth_mode,
                     "max_turns": max_turns}
        approval_timeout = DRIVEN_APPROVAL_TIMEOUT_DEFAULT

        adapter: Any = None
        buffered_recorder: _BufferedRecorder | None = None
        vendor_preopened = engine in ("codex", "claude")
        if vendor_preopened:
            preflight = self._vendor_preflight(
                engine, effective, snapshot, approval_timeout, requested_model, requested_reasoning_effort,
                writer_claim_binding, streaming_enabled,
            )
            if preflight.get("status") != "OK":
                termination_proven = bool(preflight.pop("_termination_proven", False))
                released = writer_claim is None or self._release_claim(
                    writer_claim.claim_id, termination_proven=termination_proven,
                )
                if not termination_proven or not released:
                    return self._writer_lease.recovery_denial()
                return preflight
            adapter = preflight["adapter"]
            buffered_recorder = preflight["recorder"]
            effective = dict(preflight["effective"])
            if not transcript_seeded:
                try:
                    adapter.adopt_vendor_session(vendor_root)
                except Exception as error:  # noqa: BLE001 -- refuse cleanly, reap the preflight child
                    self.log(f"resume adopt failed ({session_id}): {type(error).__name__}")
                    self._clear_adapter_attachments(adapter)
                    termination_proven = self._kill_adapter_proven(adapter)
                    self._cleanup_vendor_runtime(adapter)
                    released = writer_claim is None or self._release_claim(
                        writer_claim.claim_id, termination_proven=termination_proven,
                    )
                    if not termination_proven or not released:
                        return self._writer_lease.recovery_denial()
                    return {"status": "ERROR", "code": "ERROR_SESSION_RESUME", "agent_run_id": agent_run_id,
                            "required_action": "the conversation could not re-attach its vendor session; start a new conversation"}

        attachments_prepared = False
        if adapter is not None:
            attachment_denial = self._stage_adapter_attachments(adapter, materialized.attachments)
            if attachment_denial is not None:
                termination_proven = self._kill_adapter_proven(adapter)
                self._cleanup_vendor_runtime(adapter)
                released = writer_claim is None or self._release_claim(
                    writer_claim.claim_id, termination_proven=termination_proven,
                )
                if not termination_proven or not released:
                    return self._writer_lease.recovery_denial()
                return attachment_denial
            attachments_prepared = True

        # NEW linked T5 under the SAME C1 (the observed linked-run pattern, adopted for driven legs).
        try:
            run_payload: dict[str, Any] = {"agent_type": "other", "surface": "app-server",
                                           "session_id": session_id, "engine": engine,
                                           "auth_mode": auth_mode, "permission_mode": permission_mode,
                                           "profile_hash": profile_hash}
            if eff_model:
                run_payload["model"] = eff_model
            if requested_model is not None:
                run_payload["requested_model"] = requested_model
            if reasoning_effort:
                run_payload["reasoning_effort"] = reasoning_effort
            if requested_reasoning_effort is not None:
                run_payload["requested_reasoning_effort"] = requested_reasoning_effort
            run_ns = _Namespace(
                payload_json=json.dumps(run_payload),
                summary=f"driven {engine} run (resumed leg)", body="", task_id=None,
                agent_type="other", surface="app-server", test=self._driven_test_records,
            )
            run_result = agent_runs.agent_run_start(run_ns)
            new_run_id = run_result["id"]
        except Exception as error:  # noqa: BLE001 -- no preflight child may survive a record failure
            termination_proven = adapter is None
            if adapter is not None:
                self._clear_adapter_attachments(adapter)
                termination_proven = self._kill_adapter_proven(adapter)
                self._cleanup_vendor_runtime(adapter)
            released = writer_claim is None or self._release_claim(
                writer_claim.claim_id, termination_proven=termination_proven,
            )
            self.log(f"resume run record failed ({session_id}): {type(error).__name__}")
            if not termination_proven or not released:
                return self._writer_lease.recovery_denial()
            return {"status": "ERROR", "code": "ERROR_SESSION_RECORD", "session_id": session_id,
                    "required_action": "repair the agent-run record plane before retrying"}

        try:
            if writer_claim is not None:
                writer_claim = self._writer_lease.promote(
                    writer_claim.claim_id, session_id=session_id, agent_run_id=new_run_id,
                )
            self._record_ownership(new_run_id)
            self._emit_profile_event(
                new_run_id, requested, effective, profile_hash,
                resume_metadata=resume_metadata,
            )
            if adapter is None:
                adapter_kwargs: dict[str, Any] = {"engine_name": engine, "model": eff_model,
                                                  "approval_timeout": approval_timeout}
                if max_turns is not None:
                    adapter_kwargs["max_turns"] = max_turns
                adapter = self._build_driven_adapter(
                    new_run_id, adapter_kwargs, snapshot=snapshot,
                    writer_claim_binding=writer_claim_binding,
                )
            adapter.bind_session(session_id)
            session = DrivenSession(
                session_id=session_id, agent_run_id=new_run_id, adapter=adapter,
                engine=engine, approval_timeout=approval_timeout,
                permission_mode=permission_mode, diff_snapshots=diff_snapshots_enabled,
                image_attachments=image_attachments_enabled, governed_context=governed_context_enabled,
                streaming=streaming_enabled, policy_snapshot=snapshot,
                approval_recheck=self._driven_approval_recheck,
                writer_claim_binding=writer_claim_binding,
            )
            if writer_claim is not None:
                session.set_writer_claim(writer_claim.claim_id)
                child_denial = self._track_current_adapter_child(session)
                if child_denial is not None:
                    raise RuntimeError("persistent vendor child tracking failed")
            adapter.on_approval(session.on_approval)
            if not self._bind_delta_stream(session):
                raise RuntimeError("streaming adapter callback unavailable")
            if not self._bind_approval_broker(session) and self._adapter_factory is None:
                raise _ApprovalBrokerUnavailable("production adapter approval broker unavailable")
            if not self._bind_mutation_guard(session, snapshot) and self._adapter_factory is None:
                raise _MutationInterceptionUnavailable("production adapter mutation guard unavailable")
            with self._driven_lock:
                self._driven[new_run_id] = session
            if vendor_preopened:
                if engine == "codex":
                    self._register_codex_policy_gate(adapter, snapshot, new_run_id, session_id)
                if buffered_recorder is None:
                    raise RuntimeError("vendor preflight recorder missing")
                buffered_recorder.flush_to(self._make_recorder(new_run_id))
            elif self._adapter_factory is not None:
                adapter.start_session(cwd=str(self.repo_root), profile=effective)
            else:
                adapter.open(effective, snapshot)
            # Reopen before the worker can finalize. This bookkeeping is required: a synchronous fatal
            # turn must close the C1 *after* reopen, never race with a later best-effort reopen to `open`.
            from .. import session_records

            if not session_records.session_reopen(session_id):
                raise RuntimeError("resume reopen bookkeeping failed")
            launched = self._launch_driven_turn(
                session, prompt, adapter_prompt=adapter_prompt, materialized=materialized,
                adapter_prompt_is_final=True, owned_claim=writer_claim,
                attachments_prepared=attachments_prepared,
            )
            if launched.get("status") != "OK":
                raise RuntimeError(str(launched.get("code") or "turn launch refused"))
        except Exception as error:  # noqa: BLE001 -- a post-insert resume failure must not dangle the run
            self.log(f"resume startup failed ({new_run_id}): {type(error).__name__}")
            termination_proven = adapter is None
            if adapter is not None:
                self._clear_adapter_attachments(adapter)
                termination_proven = self._kill_adapter_proven(adapter)
                self._cleanup_vendor_runtime(adapter)
            registered = self._get_driven(new_run_id) is not None
            self._finalize_driven(new_run_id, "failed", {
                "status": "FAILED", "error_code": "ERROR_SESSION_RESUME", "fatal": True,
            }, termination_proven=termination_proven)
            released = registered or writer_claim is None or self._release_claim(
                writer_claim.claim_id, termination_proven=termination_proven,
            )
            interception_failed = isinstance(error, (_MutationInterceptionUnavailable, _ApprovalBrokerUnavailable))
            broker_failed = isinstance(error, _ApprovalBrokerUnavailable)
            if not termination_proven or not released or self._writer_lease.recovery_required:
                return {
                    **self._writer_lease.recovery_denial(), "agent_run_id": new_run_id,
                    "session_id": session_id,
                }
            return {"status": "DENIED" if interception_failed else "ERROR",
                    "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED" if interception_failed else "ERROR_SESSION_RESUME",
                    "agent_run_id": new_run_id,
                    "resumed_from": agent_run_id,
                    "session_id": session_id,
                    "turn_state": session.turn_state,
                    "required_action": (
                        "repair the production adapter approval broker before retrying"
                        if broker_failed else
                        "repair the production adapter mutation interception before retrying"
                        if interception_failed else
                        "the conversation failed to resume; start a new conversation"
                    )}
        self.log(
            f"resumed driven conversation {session_id}: {agent_run_id} -> {new_run_id}"
            + (" (transcript-seeded)" if transcript_seeded else "")
            + (" (snapshot healed)" if snapshot_rebuilt else "")
        )
        return {"status": "OK", "session_id": session_id, "agent_run_id": new_run_id,
                "resumed_from": agent_run_id, "engine": engine, "profile": effective,
                "profile_hash": profile_hash, "turn_state": session.turn_state,
                "transcript_seeded": transcript_seeded,
                "resume_fidelity": resume_fidelity,
                "omitted_message_count": omitted_message_count}

    def _not_live_session_denial(self, agent_run_id: str) -> dict[str, Any]:
        if self._conversation_run_controller(agent_run_id) == "observed":
            return {"status": "DENIED", "code": "DENIED_OBSERVED_SESSION_READ_ONLY",
                    "agent_run_id": agent_run_id,
                    "required_action": "observed Claude runs support session/list and session/events only"}
        state = self._safe_reduce(agent_run_id)
        if state is not None and state.get("terminal"):
            return {"status": "DENIED", "code": "DENIED_SESSION_TERMINAL",
                    "agent_run_id": agent_run_id, "turn_state": TURN_TERMINAL,
                    "required_action": "start a new conversation"}
        return {"status": "DENIED", "code": "DENIED_AGENT_RUN_NOT_DRIVEN",
                "agent_run_id": agent_run_id,
                "required_action": "start the conversation with session/start on this daemon"}

    def _handle_session_turn(self, args: dict[str, Any]) -> dict[str, Any]:
        denial = self.validate_turn_request(args)
        if denial is not None:
            return denial
        agent_run_id = str(args["agent_run_id"])
        session = self._get_driven(agent_run_id)
        if session is None:
            # Conversation continuation: a run this daemon does not hold live may be the latest leg of
            # a restart-orphaned conversation -- rehydrate it as a NEW linked T5 under the same C1.
            resume_lock = self._resume_lock_for_run(agent_run_id)
            if resume_lock is None:
                return self._not_live_session_denial(agent_run_id)
            # The second caller waits through the first caller's latest-check, new T5 creation, and live
            # registration. Its own re-check then sees an older leg and cannot fork the conversation.
            with resume_lock:
                resumed = self._resume_driven_conversation(
                    agent_run_id, str(args["prompt"]), request_args=args,
                )
            if resumed is not None:
                return resumed
            return self._not_live_session_denial(agent_run_id)
        if (args.get("attachments") or args.get("context_refs")) and session.policy_snapshot is None:
            return {"status": "DENIED", "code": "DENIED_CONTEXT_POLICY",
                    "required_action": "restart the conversation with a valid frozen policy snapshot"}
        reservation, request_claim, reservation_denial = self._reserve_driven_turn(session)
        if reservation_denial is not None:
            return reservation_denial
        assert reservation is not None
        try:
            materialized, artifact_denial = self._materialize_request_artifacts(
                engine=session.engine, snapshot=session.policy_snapshot,
                scope_id=session.session_id, args=args,
                image_enabled=session.image_attachments, context_enabled=session.governed_context,
            )
        except Exception:  # noqa: BLE001 -- pre-launch artifact faults restore the reserved turn
            return self._cancel_reserved_turn(session, reservation, request_claim, {
                "status": "DENIED", "code": "DENIED_CONTEXT_INVALID",
                "required_action": "retry without invalid attachment or context references",
            })
        if artifact_denial is not None:
            return self._cancel_reserved_turn(session, reservation, request_claim, artifact_denial)
        if materialized is None:
            return self._cancel_reserved_turn(session, reservation, request_claim, {
                "status": "DENIED", "code": "DENIED_CONTEXT_INVALID",
                "required_action": "retry without invalid attachment or context references",
            })
        try:
            return self._launch_driven_turn(
                session, str(args["prompt"]), materialized=materialized,
                turn_reserved=True, reservation=reservation, request_claim=request_claim,
            )
        except Exception as error:  # noqa: BLE001 -- an accepted turn cannot leave a dangling run
            self.log(f"driven turn launch failed ({agent_run_id}): {type(error).__name__}")
            session.mark_terminal()
            self._clear_adapter_attachments(session.adapter)
            termination_proven = self._kill_adapter_proven(session.adapter)
            if session.thread is not None and session.thread is not threading.current_thread():
                session.thread.join(timeout=2.0)
                termination_proven = termination_proven and not session.thread.is_alive()
            recovery_required = self._writer_lease.recovery_required
            terminal_code = "DENIED_WORKSPACE_RECOVERY_REQUIRED" if recovery_required \
                else "ERROR_SESSION_TURN"
            self._finalize_driven(agent_run_id, "failed", {
                "status": "FAILED", "error_code": terminal_code, "fatal": True,
            }, termination_proven=termination_proven)
            if not termination_proven or recovery_required or self._writer_lease.recovery_required:
                return {
                    **self._writer_lease.recovery_denial(), "agent_run_id": agent_run_id,
                    "killed": termination_proven, "turn_state": TURN_TERMINAL,
                }
            return {"status": "ERROR", "code": "ERROR_SESSION_TURN", "agent_run_id": agent_run_id,
                    "turn_state": TURN_TERMINAL,
                    "required_action": "the accepted turn failed to launch; start a new conversation"}

    def _handle_session_close(self, args: dict[str, Any]) -> dict[str, Any]:
        denial = self.validate_close_request(args)
        if denial is not None:
            return denial
        agent_run_id = str(args["agent_run_id"])
        session = self._get_driven(agent_run_id)
        if session is None:
            return self._not_live_session_denial(agent_run_id)
        refusal = session.begin_close()
        if refusal is not None:
            return {"status": "DENIED", "code": refusal, "agent_run_id": agent_run_id,
                    "turn_state": session.turn_state,
                    "required_action": "interrupt or kill the active turn before closing"}
        closed: Any = None
        try:
            closed = session.adapter.close()
        except Exception as error:  # noqa: BLE001 -- kill below is the final proof attempt
            self.log(f"driven close failed ({agent_run_id}): {type(error).__name__}")
        close_acknowledged = (
            isinstance(closed, Mapping) and closed.get("status") == "OK" and closed.get("closed") is True
        )
        termination_proven = self._close_adapter_proven(session, closed)
        killed = False
        if not termination_proven:
            killed = self._kill_adapter_proven(session.adapter)
            termination_proven = killed
        if session.thread is not None and session.thread is not threading.current_thread():
            session.thread.join(timeout=2.0)
            termination_proven = termination_proven and not session.thread.is_alive()
        if not termination_proven:
            self._writer_lease.require_recovery("session close could not prove adapter termination")
            self._finalize_driven(agent_run_id, "failed", {
                "status": "FAILED", "error_code": "DENIED_WORKSPACE_RECOVERY_REQUIRED", "fatal": True,
            }, termination_proven=False)
            return {
                **self._writer_lease.recovery_denial(), "agent_run_id": agent_run_id,
                "closed": False, "killed": False, "terminal": True, "turn_state": TURN_TERMINAL,
            }
        if not close_acknowledged:
            self._finalize_driven(agent_run_id, "failed", {
                "status": "FAILED", "error_code": "ERROR_SESSION_CLOSE", "fatal": True,
            }, termination_proven=True)
            return {"status": "ERROR", "code": "ERROR_SESSION_CLOSE", "agent_run_id": agent_run_id,
                    "closed": False, "killed": killed, "terminal": True, "turn_state": TURN_TERMINAL,
                    "required_action": "adapter close failed; start a new conversation"}
        with session.lock:
            result = session.result.as_dict() if session.result is not None else {"status": "OK"}
        self._finalize_driven(agent_run_id, "success", result, termination_proven=True)
        return {"status": "OK", "agent_run_id": agent_run_id, "closed": True, "killed": killed,
                "terminal": True, "turn_state": TURN_TERMINAL}

    def _handle_session_list(self, args: dict[str, Any]) -> dict[str, Any]:
        """List conversations with batched runs/events/snippets and bounded incremental encoding."""
        denial = self.validate_list_request(args)
        if denial is not None:
            return denial
        from .. import db

        requested_controller = args.get("controller")
        stored_controller = {"driven": "kaizen", "observed": "observed"}.get(requested_controller)
        limit = int(args.get("limit") or 100)
        where = " WHERE controller = ?" if stored_controller else ""
        filter_params: tuple[Any, ...] = (stored_controller,) if stored_controller else ()
        total_row = db.fetch_one(f"SELECT COUNT(*) FROM agent_sessions{where}", filter_params)
        total_sessions = int(total_row[0] if total_row else 0)
        params: tuple[Any, ...] = (*filter_params, limit)
        rows = db.fetch_all(
            "SELECT id, created_at, task_id, controller, mode, engine, auth_mode, state, "
            "requested_model, requested_reasoning_effort, permission_mode, profile_hash, summary, title "
            f"FROM agent_sessions{where} ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        )
        session_ids = [str(row[0]) for row in rows]
        run_rows_by_session: dict[str, list[tuple[Any, ...]]] = {session_id: [] for session_id in session_ids}
        run_totals: dict[str, int] = {session_id: 0 for session_id in session_ids}
        for offset in range(0, len(session_ids), 400):
            chunk = session_ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            ranked_rows = db.fetch_all(
                "SELECT session_id, id, created_at, agent_type, surface, state, engine, auth_mode, model, "
                "requested_model, requested_reasoning_effort, reasoning_effort, permission_mode, "
                "profile_hash, summary, run_total FROM ("
                "SELECT r.session_id, r.id, r.created_at, r.agent_type, r.surface, r.state, r.engine, "
                "r.auth_mode, r.model, r.requested_model, r.requested_reasoning_effort, "
                "r.reasoning_effort, r.permission_mode, r.profile_hash, r.summary, "
                "COUNT(*) OVER (PARTITION BY r.session_id) AS run_total, "
                "ROW_NUMBER() OVER (PARTITION BY r.session_id ORDER BY r.created_at DESC, r.id DESC) AS run_rank "
                f"FROM agent_runs r WHERE r.session_id IN ({placeholders})"
                ") WHERE run_rank <= ? ORDER BY session_id, created_at, id",
                (*chunk, _SESSION_LIST_RUN_LIMIT),
            )
            for run_row in ranked_rows:
                session_id = str(run_row[0])
                run_rows_by_session[session_id].append(run_row)
                run_totals[session_id] = int(run_row[15])

        returned_run_ids = [str(run_row[1]) for values in run_rows_by_session.values() for run_row in values]
        engine_by_run = {
            str(run_row[1]): normalize_engine(run_row[6])
            for values in run_rows_by_session.values() for run_row in values
        }
        events_by_run: dict[str, list[dict[str, Any]]] = {run_id: [] for run_id in returned_run_ids}
        resume_by_run: dict[str, dict[str, Any]] = {}
        max_turns_by_run: dict[str, int] = {}
        for offset in range(0, len(returned_run_ids), 400):
            chunk = returned_run_ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            event_rows = db.fetch_all(
                "SELECT agent_run_id, id, created_at, sequence_no, event_kind, marker, correlation_id, code, "
                "CASE WHEN event_kind = 'profile' THEN body ELSE NULL END "
                f"FROM agent_events WHERE agent_run_id IN ({placeholders}) "
                "ORDER BY agent_run_id, sequence_no",
                tuple(chunk),
            )
            for event_row in event_rows:
                run_id = str(event_row[0])
                events_by_run[run_id].append({
                    "id": event_row[1], "created_at": event_row[2], "sequence_no": event_row[3],
                    "event_kind": event_row[4], "marker": event_row[5],
                    "correlation_id": event_row[6], "code": event_row[7],
                })
                if event_row[4] == "profile" and isinstance(event_row[8], str):
                    try:
                        profile_body = json.loads(event_row[8])
                    except ValueError:
                        profile_body = None
                    metadata = read_resume_metadata(profile_body)
                    if metadata:
                        resume_by_run[run_id] = metadata
                    selected_max = self._profile_event_max_turns(profile_body, engine_by_run.get(run_id, ""))
                    if selected_max is not None:
                        max_turns_by_run[run_id] = selected_max

        snippets: dict[str, Any] = {}
        for offset in range(0, len(session_ids), 400):
            chunk = session_ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            snippet_rows = db.fetch_all(
                "SELECT session_id, message_text FROM ("
                "SELECT r.session_id, json_extract(e.body, '$.text') AS message_text, "
                "ROW_NUMBER() OVER (PARTITION BY r.session_id "
                "ORDER BY r.created_at, r.id, e.sequence_no) AS message_rank "
                "FROM agent_events e JOIN agent_runs r ON r.id = e.agent_run_id "
                f"WHERE r.session_id IN ({placeholders}) AND e.event_kind = 'chat_message' "
                "AND json_valid(e.body) AND json_extract(e.body, '$.role') = 'user'"
                ") WHERE message_rank = 1",
                tuple(chunk),
            )
            snippets.update({str(session_id): text for session_id, text in snippet_rows})

        with self._driven_lock:
            live_turn_states = {run_id: session.turn_state for run_id, session in self._driven.items()}
            live_session_ids = {session.session_id for session in self._driven.values()}
        sessions: list[dict[str, Any]] = []
        response: dict[str, Any] = {
            "status": "OK", "sessions": sessions, "truncated": False,
            "sessions_returned": 0, "sessions_total": total_sessions,
            "sessions_omitted": total_sessions,
        }
        base_bytes = self._response_bytes(response)
        encoded_entries_bytes = 0
        for row in rows:
            session_id = str(row[0])
            controller = "driven" if row[3] == "kaizen" else "observed"
            run_rows = run_rows_by_session.get(session_id, [])
            run_total = run_totals.get(session_id, 0)
            runs: list[dict[str, Any]] = []
            for run_row in run_rows:
                run_id = str(run_row[1])
                reduced = agent_runs.reduce(events_by_run.get(run_id, []))
                terminal = bool(reduced.get("terminal"))
                terminal_state = reduced.get("terminal_state")
                turn_state = (
                    live_turn_states.get(run_id, TURN_TERMINAL if terminal else TURN_IDLE)
                    if controller == "driven" else (TURN_TERMINAL if terminal else "open")
                )
                run_profile = {
                    "requested_model": run_row[9],
                    "model": run_row[8],
                    "requested_reasoning_effort": run_row[10],
                    "reasoning_effort": run_row[11],
                    "permission_mode": run_row[12],
                    "auth_mode": run_row[7],
                    "profile_hash": run_row[13],
                }
                if run_id in max_turns_by_run:
                    run_profile["max_turns"] = max_turns_by_run[run_id]
                runs.append({
                    "id": run_id,
                    "created_at": run_row[2],
                    "agent_type": run_row[3],
                    "surface": run_row[4],
                    "state": run_row[5],
                    "engine": normalize_engine(run_row[6] or row[5]),
                    "turn_state": turn_state,
                    "terminal": terminal,
                    "terminal_state": terminal_state,
                    "profile": run_profile,
                    "summary": run_row[14],
                })
            latest = runs[-1] if runs else None
            session_profile = {
                "requested_model": row[8],
                "requested_reasoning_effort": row[9],
                "permission_mode": row[10],
                "auth_mode": row[6],
                "profile_hash": row[11],
            }
            if latest is not None and "max_turns" in latest["profile"]:
                session_profile["max_turns"] = latest["profile"]["max_turns"]
            entry: dict[str, Any] = {
                "session_id": session_id,
                "created_at": row[1],
                "task_id": row[2],
                "controller": controller,
                "mode": row[4],
                "engine": normalize_engine(row[5]),
                "state": row[7],
                "profile": session_profile,
                "summary": row[12],
                "title": row[13] if isinstance(row[13], str) else None,
                "snippet": canonical_snippet(snippets.get(session_id)),
                "runs": runs,
                "runs_total": run_total,
                "runs_returned": len(runs),
                "runs_truncated": len(runs) < run_total,
                "latest_run_id": latest["id"] if latest else None,
                "latest_run_state": latest["turn_state"] if latest else None,
                "latest_terminal_state": latest["terminal_state"] if latest else None,
                # AUTHORITATIVE continuation verdict (owner 2026-07-11: no conversation dead-ends).
                # Any driven conversation whose latest leg is terminal and not live here continues --
                # the turn itself re-verifies everything including the engine gate.
                "resumable": (
                    controller == "driven" and latest is not None and bool(latest["terminal"])
                    and session_id not in live_session_ids
                ),
            }
            if latest is not None:
                entry.update(resume_by_run.get(str(latest["id"]), {}))
            entry_bytes = len(json.dumps(entry, ensure_ascii=True).encode("utf-8"))
            separator_bytes = 2 if sessions else 0
            while runs and (
                base_bytes + encoded_entries_bytes + separator_bytes + entry_bytes + 128
                > _LOOPBACK_RESPONSE_BUDGET
            ):
                runs.pop(0)  # retain the newest linked legs; their order remains oldest-first
                entry.update({
                    "runs_returned": len(runs), "runs_truncated": True,
                })
                entry_bytes = len(json.dumps(entry, ensure_ascii=True).encode("utf-8"))
            if base_bytes + encoded_entries_bytes + separator_bytes + entry_bytes + 128 > _LOOPBACK_RESPONSE_BUDGET:
                response["truncated"] = True
                break
            sessions.append(entry)
            encoded_entries_bytes += separator_bytes + entry_bytes
        response["sessions_returned"] = len(sessions)
        response["truncated"] = bool(response["truncated"] or len(sessions) < total_sessions)
        response["sessions_omitted"] = max(0, total_sessions - len(sessions))
        while sessions and self._response_bytes(response) > _LOOPBACK_RESPONSE_BUDGET:
            sessions.pop()
            response["truncated"] = True
            response["sessions_returned"] = len(sessions)
            response["sessions_omitted"] = max(0, total_sessions - len(sessions))
        return response

    @staticmethod
    def _response_bytes(payload: Mapping[str, Any]) -> int:
        """Match loopback's ensure-ASCII JSON-Lines encoding, including its trailing newline."""

        return len((json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8"))

    def _session_has_live_leg(self, session_id: str) -> bool:
        with self._driven_lock:
            return any(live.session_id == session_id for live in self._driven.values())

    def _new_vendor_runtime(self, engine: str) -> tuple[Path, dict[str, str]]:
        """Create one D-workspace-only vendor home/temp root for preflight and real children."""

        base = (self.repo_root / "AI" / "work" / "orchestration" / "runtime" / "vendor-driving").resolve()
        root = (base / f"{engine}-{secrets.token_hex(12)}").resolve()
        if base not in root.parents:
            raise ValueError("vendor runtime escaped the workspace runtime root")
        home = root / "home"
        temp = root / "tmp"
        appdata = home / "appdata"
        localappdata = home / "localappdata"
        config = home / "config"
        for path in (temp, appdata, localappdata, config):
            path.mkdir(parents=True, exist_ok=True)
        env = {
            "TEMP": str(temp),
            "TMP": str(temp),
            "TMPDIR": str(temp),
        }
        if engine == "codex":
            env.update({
                "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(appdata),
                "LOCALAPPDATA": str(localappdata), "XDG_CONFIG_HOME": str(config),
            })
            env["CODEX_HOME"] = str(home / "codex")
        return root, env

    def _cleanup_vendor_runtime(self, adapter: Any) -> None:
        root_value = getattr(adapter, "_kaizen_vendor_runtime", None)
        if not root_value:
            return
        base = (self.repo_root / "AI" / "work" / "orchestration" / "runtime" / "vendor-driving").resolve()
        try:
            root = Path(root_value).resolve()
            if root != base and base in root.parents:
                shutil.rmtree(root, ignore_errors=True)
        except OSError:
            pass
        try:
            setattr(adapter, "_kaizen_vendor_runtime", None)
        except Exception:  # noqa: BLE001 -- cleanup metadata is best-effort
            pass

    def _build_driven_adapter(self, agent_run_id: str, adapter_kwargs: dict[str, Any],
                              snapshot: Any = None,
                              recorder_override: Callable[[dict[str, Any]], None] | None = None,
                              writer_claim_binding: WriterClaimBinding | None = None) -> Any:
        """Build the driven adapter with the recorder bridged to the T6 funnel. DEFAULT (no factory): a
        real LocalLLMAdapter over default_chat_provider() (lazy real Ollama) carrying the exact five-tool
        provider-neutral ToolGateway. The gateway decides from the PER-SESSION ``snapshot`` (H2.1), uses
        the daemon approval broker and mutation guard bound before ``open``, and owns process descendants
        through the current writer-aware spawner. Explicit test factories keep their injected ToolSpec
        registries unchanged.

        A test seam factory (_adapter_factory, mirroring _dispatch_runner) receives (agent_run_id, recorder,
        kwargs) and returns an adapter over a scripted provider -- the recorder MUST be the passed bridge so
        events still funnel to T6. The factory seam builds its OWN engine (snapshot is NOT threaded into
        it), so existing offline flows are unchanged."""
        from .adapters import create_adapter

        engine_name = normalize_engine(str(adapter_kwargs.get("engine_name") or "local_llm"))
        recorder = recorder_override or self._make_recorder(agent_run_id)
        vendor_runtime: Path | None = None
        if engine_name == "local_llm":
            constructor_kwargs = dict(adapter_kwargs)
        else:
            # model/max_turns/tools are LocalLLM constructor concepts. Vendor model/effort belong to
            # open(profile, snapshot), never constructor kwargs where they can silently diverge.
            constructor_kwargs = {
                "engine_name": engine_name,
                "approval_timeout": adapter_kwargs.get("approval_timeout", DRIVEN_APPROVAL_TIMEOUT_DEFAULT),
            }
            if writer_claim_binding is not None:
                constructor_kwargs["spawner"] = self._writer_spawner(writer_claim_binding)
            vendor_runtime, vendor_env = self._new_vendor_runtime(engine_name)
            constructor_kwargs["env"] = vendor_env
            constructor_kwargs["workspace_root"] = self.repo_root
            constructor_kwargs["runtime_root"] = self.repo_root
            if engine_name == "claude":
                provider_target = self._frozen_claude_provider_target()
                if provider_target is not None:
                    constructor_kwargs["worker_command"] = list(provider_target.command)
            if engine_name == "codex":
                constructor_kwargs["codex_home"] = vendor_env["CODEX_HOME"]
        if self._adapter_factory is not None:
            try:
                adapter = self._adapter_factory(agent_run_id, recorder, constructor_kwargs)
            except Exception:
                if vendor_runtime is not None:
                    shutil.rmtree(vendor_runtime, ignore_errors=True)
                raise
            if vendor_runtime is not None:
                setattr(adapter, "_kaizen_vendor_runtime", str(vendor_runtime))
            return adapter
        recovery_callback_factory = self._writer_apply_recovery_factory(writer_claim_binding) \
        if writer_claim_binding is not None else None
        registry = getattr(self, "_ownership_registry", None)
        workspace_path_authority = getattr(registry, "authority", None)
        if engine_name == "claude":
            constructor_kwargs["apply_recovery_callback_factory"] = recovery_callback_factory
            constructor_kwargs["workspace_path_authority"] = workspace_path_authority
        if engine_name in ("codex", "claude"):
            try:
                if engine_name == "codex":
                    if not isinstance(snapshot, policy.PolicySnapshot):
                        raise ValueError("Codex adapter construction requires an immutable PolicySnapshot")
                    adapter = create_adapter(
                        "codex",
                        snapshot.build_engine(),
                        recorder=recorder,
                        logger=self.log,
                        **constructor_kwargs,
                    )
                else:
                    adapter = create_adapter(
                        "claude",
                        recorder=recorder,
                        logger=self.log,
                        **constructor_kwargs,
                    )
            except Exception:
                if vendor_runtime is not None:
                    shutil.rmtree(vendor_runtime, ignore_errors=True)
                raise
            setattr(adapter, "_kaizen_vendor_runtime", str(vendor_runtime))
            return adapter
        if snapshot is not None:
            engine = policy.PolicyEngine.from_snapshot(snapshot)
        else:
            engine = self.policy or policy.build_engine_from_db()
        tool_spawner = self._writer_spawner(writer_claim_binding) \
            if writer_claim_binding is not None else children.spawn_owned
        return create_adapter(
            "local_llm",
            engine,
            recorder=recorder,
            logger=self.log,
            workspace_root=self.repo_root,
            tool_spawner=tool_spawner,
            apply_recovery_callback_factory=recovery_callback_factory,
            workspace_path_authority=workspace_path_authority,
            **constructor_kwargs,
        )

    def _make_recorder(self, agent_run_id: str) -> Callable[[dict[str, Any]], None]:
        """A recorder sink that bridges the adapter's normalized events to the T6 funnel (supervisor.py
        funnel_event). The recorder payload becomes the T6 body (body=json.dumps(payload)) so the full
        event detail is preserved on the run's authoritative stream and replays to the webview."""
        from ..schemas.registry import AGENT_EVENT_KIND_MARKERS

        def sink(event: dict[str, Any]) -> None:
            # The adapter vocabulary is wider than the T6 (kind x marker) matrix (session open, turn
            # close_canceled): those are not stream events -- skip quietly instead of funnel-denying.
            if event.get("marker") not in AGENT_EVENT_KIND_MARKERS.get(str(event.get("event_kind")), ()):
                return
            payload = event.get("payload") or {}
            persistence_required = event.get("persistence_required") is True
            try:
                self.funnel_event(
                    agent_run_id,
                    event["event_kind"],
                    event["marker"],
                    summary=event.get("summary", ""),
                    correlation_id=event.get("correlation_id"),
                    code=event.get("code"),
                    body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload else "",
                )
            except Exception as error:  # noqa: BLE001 -- critical apply evidence must fail closed
                self.log(f"driven recorder funnel failed ({agent_run_id}): {error}")
                if persistence_required:
                    self._writer_lease.require_recovery("critical apply evidence could not be persisted")
                    raise

        return sink

    def _register_codex_policy_gate(self, adapter: Any, snapshot: policy.PolicySnapshot,
                                    agent_run_id: str, session_id: str) -> None:
        """Bind the adapter-generated opaque gate id to this run's exact immutable snapshot."""

        registration = getattr(adapter, "hook_registration", None)
        if not isinstance(registration, Mapping):
            raise ValueError("Codex adapter did not expose hook registration material")
        gate_id = str(registration.get("gate_id") or "")
        profile_hash = str(registration.get("profile_hash") or "")
        if not gate_id or not profile_hash or not secrets.compare_digest(profile_hash, snapshot.profile_hash):
            raise ValueError("Codex hook registration does not match the immutable policy snapshot")
        binding = _SessionPolicyGate(
            gate_id=gate_id,
            profile_hash=profile_hash,
            agent_run_id=agent_run_id,
            session_id=session_id,
            snapshot=snapshot,
        )
        with self._session_policy_gates_lock:
            existing = self._session_policy_gates.get(gate_id)
            if existing is not None and existing != binding:
                raise ValueError("Codex hook gate id collision")
            previous = self._policy_gate_by_run.get(agent_run_id)
            if previous is not None and previous != gate_id:
                self._session_policy_gates.pop(previous, None)
            self._session_policy_gates[gate_id] = binding
            self._policy_gate_by_run[agent_run_id] = gate_id

    def _unregister_session_policy_gate(self, agent_run_id: str) -> None:
        with self._session_policy_gates_lock:
            gate_id = self._policy_gate_by_run.pop(agent_run_id, None)
            if gate_id is not None:
                self._session_policy_gates.pop(gate_id, None)

    def _handle_session_policy_check(self, args: dict[str, Any]) -> dict[str, Any]:
        """Evaluate one authenticated Codex PreToolUse payload against its bound frozen snapshot."""

        from . import hooked

        gate_id = str(args.get("gate_id") or "")
        supplied_hash = str(args.get("profile_hash") or "")
        payload = args.get("payload")
        if not gate_id or not supplied_hash or not isinstance(payload, dict):
            return {"status": "DENIED", "code": "DENIED_POLICY_GATE_UNAVAILABLE",
                    "required_action": "provide a registered gate_id, profile_hash, and hook payload"}
        with self._session_policy_gates_lock:
            binding = self._session_policy_gates.get(gate_id)
        if binding is None:
            return {"status": "DENIED", "code": "DENIED_POLICY_GATE_UNAVAILABLE",
                    "required_action": "the driven Codex session gate is unknown or already closed"}
        if not secrets.compare_digest(supplied_hash, binding.profile_hash):
            return {"status": "DENIED", "code": "DENIED_PROFILE_MISMATCH",
                    "required_action": "the hook profile hash must match its registered immutable snapshot"}

        normalized = dict(payload)
        normalized["session_id"] = binding.session_id
        normalized["permission_mode"] = binding.snapshot.permission_mode
        epoch_value = normalized.get("epoch")
        epoch = int(epoch_value) if str(epoch_value or "").lstrip("-").isdigit() else 0
        action = hooked.action_from_pretooluse(
            normalized,
            engine=binding.snapshot.engine,
            epoch=epoch,
        )
        decision = binding.snapshot.build_engine().decide(action, current_epoch=epoch)
        if decision.result == policy.ALLOW and action.verb in policy.MUTATING_VERBS:
            session = self._get_driven(binding.agent_run_id)
            if session is None:
                return {
                    "status": "OK", "result": policy.DENY,
                    "reason": "DENIED_WORKSPACE_RECOVERY_REQUIRED: live writer session unavailable",
                    "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                }
            writer_denial = self._guard_session_mutation(session, binding.snapshot, action)
            if writer_denial is not None:
                return {
                    "status": "OK", "result": policy.DENY,
                    "reason": str(writer_denial.get("code") or "DENIED_WORKSPACE_RECOVERY_REQUIRED"),
                    "code": writer_denial.get("code"),
                    "holder": writer_denial.get("holder"),
                }
        return {
            "status": "OK",
            "result": decision.result,
            "reason": decision.reason,
            "dedupe_key": decision.dedupe_key,
            "rule_id": decision.rule_id,
            "invariant_id": decision.invariant_id,
        }

    def _finalize_driven(self, agent_run_id: str, conclusion: str, result: dict[str, Any], *,
                         termination_proven: bool = False) -> None:
        """Write the conversation's sole T8 and clean up its live ownership.

        Normal turn completion never calls this method. It is reserved for explicit close, fatal adapter
        failure, kill, or shutdown and is idempotent-tolerant against races.
        """

        if not termination_proven:
            self._writer_lease.require_recovery("driven adapter termination could not be proven")
        session = self._get_driven(agent_run_id)
        self._diff_snapshots.discard_run(agent_run_id)
        with self._approved_broker_payloads_lock:
            self._approved_broker_payloads = {
                key: value for key, value in self._approved_broker_payloads.items()
                if key[0] != agent_run_id
            }
        self._unregister_session_policy_gate(agent_run_id)
        if session is not None:
            session.mark_terminal()
        summary = f"driven session {conclusion}"
        # Kept for compatibility; chat_message is the authoritative transcript source. The worker has
        # already replaced rejected assistant text with a safe placeholder before it reaches here.
        body = (
            result.get("final_text") or result.get("final") or result.get("error_code")
            or result.get("code") or ""
        )
        try:
            ns = _Namespace(agent_run_id=agent_run_id, conclusion=conclusion, summary=summary, body=str(body))
            agent_runs.agent_run_finalize(ns)
        except KaizenDenied as denied:
            if denied.code != "DENIED_AGENT_RUN_ALREADY_FINALIZED":
                self.log(f"driven finalize denied ({agent_run_id}): {denied.code}")
        except Exception as error:  # noqa: BLE001 -- finalize failure must not leave the thread hung
            self.log(f"driven finalize error ({agent_run_id}): {type(error).__name__}")
        session_id = session.session_id if session is not None else self._session_id_for_run(agent_run_id)
        self._close_session_record(session_id, conclusion)
        deregistered = self._deregister_driven(agent_run_id)
        if deregistered is not None:
            deregistered.deny_all_waiters()  # any still-parked ask fails closed on terminal
            approvals_reconciled = self._reconcile_approval_orphans()
            self._cleanup_vendor_runtime(deregistered.adapter)
            token = deregistered.writer_claim_token
            if token is not None and self._release_claim(
                token, termination_proven=termination_proven and approvals_reconciled,
            ):
                deregistered.clear_writer_claim(token)
        self._clear_ownership(agent_run_id)

    @staticmethod
    def _session_id_for_run(agent_run_id: str) -> str | None:
        """Recover the C1 link when startup failed before the live session registry was populated."""

        from .. import db

        try:
            row = db.fetch_one("SELECT session_id FROM agent_runs WHERE id = ?", (agent_run_id,))
        except Exception:  # noqa: BLE001 -- T8 remains authoritative if the cache lookup fails
            return None
        return str(row[0]) if row and row[0] else None

    def _close_session_record(self, session_id: str | None, conclusion: str) -> None:
        """Close the C1 envelope through trusted daemon bookkeeping (no public schema mutation)."""

        if not session_id:
            return
        from .. import db

        state = {"success": "closed", "canceled": "canceled"}.get(conclusion, "failed")

        def op(conn: Any, _attempt: int) -> None:
            conn.execute("UPDATE agent_sessions SET state = ? WHERE id = ?", (state, session_id))

        try:
            db.write_tx(op)
        except Exception as error:  # noqa: BLE001 -- T8 remains authoritative if C1 cache update fails
            self.log(f"driven session row close failed ({session_id}): {type(error).__name__}")

    def _deregister_driven(self, agent_run_id: str) -> Any:
        with self._driven_lock:
            return self._driven.pop(agent_run_id, None)

    def _handle_session_events(self, args: dict[str, Any]) -> dict[str, Any]:
        """Replay a driven or observed run's authoritative T6 events (connect-per-call). WHERE agent_run_id=? AND
        sequence_no>? ORDER BY sequence_no LIMIT ?. ``since=0`` (default) returns full history (the
        webview replay depends on this gapless-from-1 cursor). Long-poll: return immediately if events
        exist or wait<=0; else poll every 0.5s up to min(wait, SESSION_LONG_POLL_MAX_S). A terminal run
        never waits (its stream is closed). cursor is the max sequence_no returned (or ``since`` when
        empty); terminal/terminal_state come from the reduced run state.

        Only driven app-server runs and runs linked to an observed C1 are readable here. Other ledger
        runs remain outside the conversation API."""
        agent_run_id = args.get("agent_run_id")
        if not agent_run_id:
            return {"status": "DENIED", "code": "DENIED_AGENT_RUN_REQUIRED",
                    "required_action": "session/events needs an agent_run_id"}
        controller = self._conversation_run_controller(str(agent_run_id))
        if controller is None:
            return {"status": "DENIED", "code": "DENIED_AGENT_RUN_NOT_DRIVEN",
                    "agent_run_id": agent_run_id,
                    "required_action": "session/events accepts driven or observed conversation runs only"}
        since_raw = args.get("since", 0)
        delta_requested = "delta_since" in args
        delta_since_raw = args.get("delta_since", 0)
        limit_raw = args.get("limit", 500)
        wait_raw = args.get("wait", 0.0)
        if isinstance(since_raw, bool) or not isinstance(since_raw, int) or since_raw < 0:
            return {"status": "DENIED", "code": "DENIED_EVENTS_CURSOR_INVALID",
                    "required_action": "since must be a nonnegative integer"}
        if (isinstance(delta_since_raw, bool) or not isinstance(delta_since_raw, int)
                or delta_since_raw < 0):
            return {"status": "DENIED", "code": "DENIED_DELTA_CURSOR_INVALID",
                    "required_action": "delta_since must be a nonnegative integer"}
        if isinstance(limit_raw, bool) or not isinstance(limit_raw, int) or limit_raw < 0:
            return {"status": "DENIED", "code": "DENIED_EVENTS_LIMIT_INVALID",
                    "required_action": "limit must be a nonnegative integer"}
        if isinstance(wait_raw, bool) or not isinstance(wait_raw, (int, float)) or wait_raw < 0:
            return {"status": "DENIED", "code": "DENIED_EVENTS_WAIT_INVALID",
                    "required_action": "wait must be a nonnegative number"}
        since = since_raw
        delta_since = delta_since_raw
        limit = min(limit_raw, 500)
        wait = float(wait_raw)
        clock = self._clock or __import__("time").monotonic
        sleep = self._sleep or __import__("time").sleep

        events = self._read_run_events(str(agent_run_id), since, limit)
        delta_state = self._run_delta_state(
            str(agent_run_id), controller, delta_since,
        ) if delta_requested else None
        terminal, terminal_state = self._driven_terminal(str(agent_run_id))
        if events or (delta_state is not None and (delta_state[0] or delta_state[2])) or wait <= 0 or terminal:
            return self._events_payload(
                str(agent_run_id), events, since, terminal, terminal_state, controller,
                delta_state=delta_state,
            )

        # Long-poll: 0.5s poll-sleep loop up to the capped wait; a run that terminalizes mid-poll stops.
        deadline = clock() + min(wait, SESSION_LONG_POLL_MAX_S)
        while clock() < deadline:
            sleep(0.5)
            events = self._read_run_events(str(agent_run_id), since, limit)
            delta_state = self._run_delta_state(
                str(agent_run_id), controller, delta_since,
            ) if delta_requested else None
            terminal, terminal_state = self._driven_terminal(str(agent_run_id))
            if events or (delta_state is not None and (delta_state[0] or delta_state[2])) or terminal:
                break
        return self._events_payload(
            str(agent_run_id), events, since, terminal, terminal_state, controller,
            delta_state=delta_state,
        )

    def _run_delta_state(self, agent_run_id: str, controller: str,
                         since: int) -> tuple[list[dict[str, Any]], int, bool]:
        """Read live ephemeral deltas without consuming them; observed runs never stream."""

        if controller != "driven":
            return [], since, False
        session = self._get_driven(agent_run_id)
        if session is None:
            return [], 0, since > 0  # daemon restart/deregistration lost the ephemeral ring
        return session.read_deltas(since)

    def _events_payload(self, agent_run_id: str, events: list[dict[str, Any]], since: int,
                        terminal: bool, terminal_state: str | None, controller: str = "driven",
                        *, delta_state: tuple[list[dict[str, Any]], int, bool] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "OK", "agent_run_id": agent_run_id, "events": [],
            "cursor": since, "terminal": terminal, "controller": controller,
            "turn_state": (
                self._driven_turn_state(agent_run_id, terminal)
                if controller == "driven" else (TURN_TERMINAL if terminal else "open")
            ),
            "truncated": False,
        }
        if delta_state is not None:
            payload["deltas"], payload["delta_cursor"], payload["delta_dropped"] = delta_state
        if terminal_state is not None:
            payload["terminal_state"] = terminal_state
        batched: list[dict[str, Any]] = payload["events"]
        for event in events:
            candidate = {**payload, "events": [*batched, event], "cursor": event["sequence_no"]}
            if self._response_bytes(candidate) <= _LOOPBACK_RESPONSE_BUDGET:
                batched.append(event)
                payload["cursor"] = event["sequence_no"]
                continue
            if batched:
                payload["truncated"] = True
                break
            # A legacy event may predate body caps. Consume it gaplessly with explicit metadata rather
            # than making its entire run unreplayable behind an oversized frame.
            safe_event = {
                "sequence_no": event["sequence_no"],
                "event_kind": event.get("event_kind"),
                "marker": event.get("marker"),
                "summary": str(event.get("summary") or "")[:256],
                "correlation_id": event.get("correlation_id"),
                "code": event.get("code"),
                "body": None,
                "body_omitted": True,
                "body_omission_code": "PAYLOAD_TOO_LARGE",
            }
            batched.append(safe_event)
            payload["cursor"] = event["sequence_no"]
            payload["truncated"] = len(events) > 1
            payload["body_omitted"] = True
            break
        payload["events_remaining"] = max(0, len(events) - len(batched))
        return payload

    def _driven_turn_state(self, agent_run_id: str, terminal: bool | None = None) -> str:
        session = self._get_driven(agent_run_id)
        if session is not None:
            return session.turn_state
        if terminal is None:
            terminal, _ = self._driven_terminal(agent_run_id)
        return TURN_TERMINAL if terminal else TURN_IDLE

    def _read_run_events(self, agent_run_id: str, since: int, limit: int) -> list[dict[str, Any]]:
        """Connect-per-call read of a run's T6 events after ``since`` (the gapless sequence_no cursor).
        Returns the webview WireEvent shape (sequence_no/event_kind/marker/summary/correlation_id/code/
        body)."""
        from .. import db

        rows = db.fetch_all(
            "SELECT sequence_no, event_kind, marker, summary, correlation_id, code, body "
            "FROM agent_events WHERE agent_run_id = ? AND sequence_no > ? ORDER BY sequence_no LIMIT ?",
            (agent_run_id, since, max(0, limit)),
        )
        events: list[dict[str, Any]] = []
        for row in rows:
            event = {
                "sequence_no": row[0], "event_kind": row[1], "marker": row[2], "summary": row[3],
                "correlation_id": row[4], "code": row[5], "body": row[6],
            }
            if row[1] == "turn":
                turn_id = row[4]
                try:
                    body = json.loads(row[6]) if row[6] else {}
                    if isinstance(body, Mapping) and isinstance(body.get("turn_id"), str):
                        turn_id = body["turn_id"]
                except (TypeError, ValueError):
                    pass
                if isinstance(turn_id, str) and turn_id:
                    event["turn_id"] = turn_id
            events.append(event)
        return events

    def _driven_terminal(self, agent_run_id: str) -> tuple[bool, str | None]:
        """Terminal state for the events stream: prefer the LIVE driven session's done/result, else the
        reduced ledger state (a run finalized+deregistered still answers terminal from its events)."""
        state = self._safe_reduce(agent_run_id)
        if state is None:
            return False, None
        return bool(state["terminal"]), state.get("terminal_state")

    def _conversation_run_controller(self, agent_run_id: str) -> str | None:
        """Return the public conversation controller for a replayable run, else None."""
        with self._driven_lock:
            if agent_run_id in self._driven:
                return "driven"
        from .. import db

        row = db.fetch_one(
            "SELECT ar.surface, s.controller FROM agent_runs ar "
            "LEFT JOIN agent_sessions s ON s.id = ar.session_id WHERE ar.id = ?",
            (agent_run_id,),
        )
        if row and row[0] == "app-server":
            return "driven"
        if row and row[1] == "observed":
            return "observed"
        return None

    def _get_driven(self, agent_run_id: str) -> Any:
        with self._driven_lock:
            return self._driven.get(agent_run_id)

    def _handle_session_steer(self, args: dict[str, Any]) -> dict[str, Any]:
        """Proxy a mid-turn steer to the driven adapter (inject-next-iteration). Unknown/finished run ->
        DENIED_AGENT_RUN_NOT_DRIVEN; no active turn -> the adapter's DENIED_NO_ACTIVE_TURN surfaces."""
        agent_run_id = args.get("agent_run_id")
        instruction = args.get("instruction")
        if not (agent_run_id and instruction):
            return {"status": "DENIED", "code": "DENIED_STEER_FIELDS_REQUIRED",
                    "required_action": "session/steer needs an agent_run_id and instruction"}
        session = self._get_driven(str(agent_run_id))
        if session is None:
            return self._not_live_session_denial(str(agent_run_id))
        if session.turn_state != TURN_RUNNING:
            return {"status": "DENIED", "code": "DENIED_SESSION_NOT_IDLE",
                    "agent_run_id": agent_run_id, "turn_state": session.turn_state,
                    "required_action": "steer is accepted only while a turn is running"}

        try:
            return dict(session.adapter.steer(str(instruction)))
        except Exception as error:  # adapter-specific structured refusal
            payload = getattr(error, "payload", None)
            if callable(payload):
                return payload()
            return {"status": "ERROR", "code": "ERROR_SESSION_STEER",
                    "required_action": "the adapter rejected the steer; inspect session/events"}

    def _handle_session_interrupt(self, args: dict[str, Any]) -> dict[str, Any]:
        """Proxy an interrupt to the driven adapter (honored at the next loop top; non-preemptive).
        Unknown/finished run -> DENIED_AGENT_RUN_NOT_DRIVEN."""
        agent_run_id = args.get("agent_run_id")
        if not agent_run_id:
            return {"status": "DENIED", "code": "DENIED_AGENT_RUN_REQUIRED",
                    "required_action": "session/interrupt needs an agent_run_id"}
        session = self._get_driven(str(agent_run_id))
        if session is None:
            return self._not_live_session_denial(str(agent_run_id))
        if session.turn_state != TURN_RUNNING:
            return {"status": "DENIED", "code": "DENIED_SESSION_NOT_IDLE",
                    "agent_run_id": agent_run_id, "turn_state": session.turn_state,
                    "required_action": "interrupt is accepted only while a turn is running"}
        return dict(session.adapter.interrupt())

    def _handle_session_kill(self, args: dict[str, Any]) -> dict[str, Any]:
        """Permanently stop a driven run: deny every parked approval waiter (fail-closed), kill the
        adapter (closes any open turn close_canceled), then T8-finalize canceled. Unknown/finished run ->
        DENIED_AGENT_RUN_NOT_DRIVEN. The turn thread's own finalize races this one; whoever wins writes
        the terminal marker (T8 refuses the second), so kill reports killed truthfully either way."""
        agent_run_id = args.get("agent_run_id")
        if not agent_run_id:
            return {"status": "DENIED", "code": "DENIED_AGENT_RUN_REQUIRED",
                    "required_action": "session/kill needs an agent_run_id"}
        session = self._get_driven(str(agent_run_id))
        if session is None:
            return self._not_live_session_denial(str(agent_run_id))
        denied = session.deny_all_waiters()   # parked asks fail closed BEFORE the adapter dies
        session.mark_terminal()
        termination_proven = self._kill_adapter_proven(session.adapter)
        if session.thread is not None and session.thread is not threading.current_thread():
            session.thread.join(timeout=2.0)
            termination_proven = termination_proven and not session.thread.is_alive()
        if not termination_proven:
            self.log(f"driven kill termination unproven ({agent_run_id})")
        # Finalize canceled directly; a worker racing to finish sees terminal and never writes T8.
        self._finalize_driven(str(agent_run_id), "canceled", {
            "status": "CANCELED", "error_code": "SESSION_KILLED", "fatal": True,
        }, termination_proven=termination_proven)
        if not termination_proven:
            return {
                **self._writer_lease.recovery_denial(), "agent_run_id": agent_run_id,
                "killed": False, "waiters_denied": denied, "terminal": True,
                "turn_state": TURN_TERMINAL,
            }
        return {"status": "OK", "agent_run_id": agent_run_id, "killed": True,
                "waiters_denied": denied, "terminal": True, "turn_state": TURN_TERMINAL}

    def _shutdown_driven(self) -> None:
        """Teardown every live driven session BEFORE loopback stops: deny waiters (fail-closed), kill the
        adapter, join the turn thread briefly, then force-finalize any run left non-terminal. Idempotent;
        never raises past shutdown."""
        with self._driven_lock:
            sessions = list(self._driven.values())
        for session in sessions:
            session.mark_terminal()
            termination_proven = False
            try:
                session.deny_all_waiters()
                termination_proven = self._kill_adapter_proven(session.adapter)
            except Exception:  # noqa: BLE001 -- teardown must not raise past shutdown
                pass
            if session.thread is not None:
                session.thread.join(timeout=2.0)
                termination_proven = termination_proven and not session.thread.is_alive()
            try:
                state = self._safe_reduce(session.agent_run_id)
                if state is not None and not state["terminal"]:
                    self._finalize_driven(session.agent_run_id, "canceled", {
                        "status": "CANCELED", "error_code": "DAEMON_SHUTDOWN", "fatal": True,
                    }, termination_proven=termination_proven)
                elif session.writer_claim_token is not None:
                    token = session.writer_claim_token
                    approvals_reconciled = self._reconcile_approval_orphans(
                        exclude_run_ids={session.agent_run_id},
                    )
                    if self._release_claim(
                        token, termination_proven=termination_proven and approvals_reconciled,
                    ):
                        session.clear_writer_claim(token)
            except Exception:  # noqa: BLE001 -- teardown finalize is best-effort
                pass
            self._unregister_session_policy_gate(session.agent_run_id)
            self._cleanup_vendor_runtime(session.adapter)
            self._clear_ownership(session.agent_run_id)
        with self._driven_lock:
            self._driven.clear()

    def repo_root_work_handoff_dir(self) -> Path:
        """The shadow-handoff patch-artifact directory under the runtime dir (gitignored)."""
        return RUNTIME_DIR / "handoff-artifacts"

    # --- M-CLAUDE (M5b) hooked-governor control ops -------------------------------------------

    def _handle_hooks(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """Route a hooks/* loopback op. install/verify/remove manage the workspace .claude/
        settings.local.json; decide routes policy hooks; record ingests observed lifecycle hooks. Structured
        denials, never a traceback (a handler error must not kill the accept loop)."""
        try:
            if op == "hooks/install":
                return self._hooks_install(args)
            if op == "hooks/verify":
                return self._hooks_verify()
            if op == "hooks/remove":
                return self._hooks_remove()
            if op == "hooks/decide":
                return self._hooks_decide(args)
            if op == "hooks/record":
                return self._hooks_record(args)
        except KaizenDenied as denied:
            return denied.payload()
        except Exception as error:  # noqa: BLE001 -- a hooks op error must not kill the accept loop
            return {"status": "ERROR", "code": "ERROR_HOOKS_OP", "op": op, "message": str(error)}
        return {"status": "DENIED", "code": "DENIED_HOOKS_UNKNOWN_OP", "op": op,
                "required_action": "use hooks/install|hooks/verify|hooks/remove|hooks/decide|hooks/record"}

    def _claude_settings_path(self) -> Path:
        """Returns workspace-local .claude/settings.local.json Path."""
        return self.repo_root.joinpath(*CLAUDE_LOCAL_SETTINGS_REL)

    def _hook_shim_command(self, mode: str) -> str:
        """The hook command line written into settings.local.json. Uses THIS interpreter + the
        repo-relative shim path so the entry is portable and self-locating (the shim adds the repo root
        to sys.path itself)."""
        shim = self.repo_root.joinpath(*HOOK_SHIM_REL)
        return f'"{sys.executable}" "{shim}" --mode {mode} --event'

    def _desired_hooks_block(self, mode: str) -> dict[str, Any]:
        """The six-hook block this install asserts. The `_kaizen` marker key tags
        OUR entries so verify/remove never disturb a user's own hooks in the same file."""
        from . import hooked

        base = self._hook_shim_command(mode)
        def entry(event: str) -> dict[str, Any]:
            return {
                "matcher": "*",
                "_kaizen": _HOOK_MARKER,
                "hooks": [{"type": "command", "command": f"{base} {event}"}],
            }
        return {event: [entry(event)] for event in hooked.HOOK_EVENTS}

    def _is_git_tracked(self, path: Path) -> bool:
        """True iff ``path`` is tracked by git (``git ls-files --error-unmatch`` exits 0). A tracked
        settings file is REFUSED (we never write a committed file). Absent git / non-repo => not tracked
        (the file is safe to write). Best-effort + bounded, never raises."""
        git = shutil.which("git")
        if not git:
            return False
        try:
            rel = path.resolve().relative_to(self.repo_root.resolve())
        except ValueError:
            rel = path
        try:
            proc = subprocess.run(
                [git, "ls-files", "--error-unmatch", "--", str(rel)],
                cwd=str(self.repo_root), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def _load_settings(self, path: Path) -> dict[str, Any]:
        """Reads+parses settings; {} on missing/invalid/non-dict (never raises)."""
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _hooks_current_match_desired(self, settings: dict[str, Any], desired: dict[str, Any]) -> bool:
        """Warm-check: do the file's hooks already equal the desired block for our events? Compares only
        our marker-tagged entries (user entries, including entries on the same event, are irrelevant)."""
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            return False
        for event, want in desired.items():
            entries = hooks.get(event)
            if not isinstance(entries, list):
                return False
            ours = [entry for entry in entries if isinstance(entry, dict) and entry.get("_kaizen") == _HOOK_MARKER]
            if ours != want:
                return False
        return True

    def _marker_owned_mode(self, settings: dict[str, Any]) -> str | None:
        """Return one consistent supported mode from marker-owned commands, including stale paths."""
        import re

        from . import hooked

        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            return None
        modes: set[str] = set()
        marker_seen = False
        pattern = re.compile(r"(?:^|\s)--mode\s+(hooked-(?:strict|observe))(?:\s|$)")
        for event in hooked.HOOK_EVENTS:
            entries = hooks.get(event)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("_kaizen") != _HOOK_MARKER:
                    continue
                marker_seen = True
                commands = entry.get("hooks")
                if not isinstance(commands, list) or not commands:
                    return None
                for command in commands:
                    if not isinstance(command, dict) or command.get("type") != "command":
                        return None
                    match = pattern.search(str(command.get("command") or ""))
                    if match is None:
                        return None
                    modes.add(match.group(1))
        return next(iter(modes)) if marker_seen and len(modes) == 1 else None

    @staticmethod
    def _write_settings_atomic(path: Path, settings: dict[str, Any]) -> None:
        """Write newline-normalized JSON to an exclusive same-directory temp, fsync it, and atomically os.replace the workspace-local settings file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(settings, indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _hooks_install(self, args: dict[str, Any]) -> dict[str, Any]:
        """Install (idempotent, validate-and-skip-warm). REFUSES if settings.local.json is git-tracked
        (never writes a committed file). On a WARM re-run whose hooks already match, does NO write and
        reports skipped=True. Otherwise writes only the workspace-local file, replacing marker-tagged
        Kaizen entries while preserving all user-authored settings and same-event hook entries."""
        from .modes import parse_session_mode

        # The session_mode gate: install only makes sense in a hooked session. `mode` here is the
        # governor enforcement mode (strict/observe); the session_mode 'hooked' is the record-plane mode.
        mode = str(args.get("mode") or "hooked-strict")
        if mode not in ("hooked-strict", "hooked-observe"):
            return {"status": "DENIED", "code": "DENIED_HOOKS_MODE_INVALID",
                    "required_action": "mode must be hooked-strict|hooked-observe"}
        _ = parse_session_mode("hooked")  # assert the reserved session mode parses (M0 seam)

        path = self._claude_settings_path()
        if self._is_git_tracked(path):
            return {"status": "DENIED", "code": "DENIED_HOOKS_SETTINGS_TRACKED",
                    "path": str(path),
                    "required_action": ".claude/settings.local.json is git-tracked; untrack it (it must stay local/uncommitted) before installing governor hooks"}

        desired = self._desired_hooks_block(mode)
        settings = self._load_settings(path)
        if path.is_file() and self._hooks_current_match_desired(settings, desired):
            # Warm re-run: already correct. Do no work (idempotent skip-warm).
            return {"status": "OK", "installed": True, "skipped": True, "mode": mode, "path": str(path)}

        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        # Replace only our marker-tagged entry per event. A stale command line/mode is overwritten while
        # user-authored hooks on this same event retain their order and content.
        for event, block in desired.items():
            entries = hooks.get(event)
            kept = (
                [entry for entry in entries
                 if not (isinstance(entry, dict) and entry.get("_kaizen") == _HOOK_MARKER)]
                if isinstance(entries, list) else []
            )
            hooks[event] = kept + block
        settings["hooks"] = hooks
        self._write_settings_atomic(path, settings)
        self.log(f"hooks installed (mode={mode}) -> {path}")
        return {"status": "OK", "installed": True, "skipped": False, "mode": mode, "path": str(path)}

    def _hooks_verify(self) -> dict[str, Any]:
        """Report exact marker-owned command state, tracking risk, and shim presence."""
        from . import hooked

        path = self._claude_settings_path()
        settings = self._load_settings(path)
        hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
        markers = {}
        for event in hooked.HOOK_EVENTS:
            entries = hooks.get(event) if isinstance(hooks, dict) else None
            markers[event] = bool(
                isinstance(entries, list)
                and any(isinstance(e, dict) and e.get("_kaizen") == _HOOK_MARKER for e in entries)
            )
        mode = self._marker_owned_mode(settings)
        desired = self._desired_hooks_block(mode) if mode else {}
        events_current = {}
        for event in hooked.HOOK_EVENTS:
            entries = hooks.get(event) if isinstance(hooks, dict) else None
            ours = (
                [entry for entry in entries
                 if isinstance(entry, dict) and entry.get("_kaizen") == _HOOK_MARKER]
                if isinstance(entries, list) else []
            )
            events_current[event] = bool(mode and ours == desired.get(event))
        installed = all(events_current.values())
        shim = self.repo_root.joinpath(*HOOK_SHIM_REL)
        return {
            "status": "OK",
            "installed": installed,
            "events": events_current,
            "markers": markers,
            "mode": mode,
            "stale": any(markers.values()) and not installed,
            "path": str(path),
            "exists": path.is_file(),
            "tracked": self._is_git_tracked(path),
            "shim_present": shim.is_file(),
        }

    def _hooks_remove(self) -> dict[str, Any]:
        """Remove OUR governor hook entries (idempotent). Strips only entries tagged with the kaizen
        marker from the installed event set; leaves any user-authored entries and the rest of the
        file intact. A missing file / already-absent entries reports removed=False cleanly."""
        from . import hooked

        path = self._claude_settings_path()
        if not path.is_file():
            return {"status": "OK", "removed": False, "reason": "no settings.local.json", "path": str(path)}
        if self._is_git_tracked(path):
            return {"status": "DENIED", "code": "DENIED_HOOKS_SETTINGS_TRACKED",
                    "path": str(path),
                    "required_action": ".claude/settings.local.json is git-tracked; untrack it before the governor edits it"}
        settings = self._load_settings(path)
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            return {"status": "OK", "removed": False, "reason": "no hooks block", "path": str(path)}
        removed_any = False
        for event in hooked.HOOK_EVENTS:
            entries = hooks.get(event)
            if not isinstance(entries, list):
                continue
            kept = [e for e in entries if not (isinstance(e, dict) and e.get("_kaizen") == _HOOK_MARKER)]
            if len(kept) != len(entries):
                removed_any = True
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)
        if not hooks:
            settings.pop("hooks", None)
        else:
            settings["hooks"] = hooks
        self._write_settings_atomic(path, settings)
        self.log(f"hooks removed -> {path} (removed_any={removed_any})")
        return {"status": "OK", "removed": removed_any, "path": str(path)}

    def _hooks_decide(self, args: dict[str, Any]) -> dict[str, Any]:
        """Route a live hook payload (the shim's loopback call) through the policy engine. PreToolUse ⇒
        build a RequestedAction and return the Decision fields; UserPromptSubmit ⇒ run the @-ref/prompt
        guard and return {block, reason}. Fails CLOSED-friendly: no policy engine ⇒ DENIED_POLICY_NOT_LOADED
        (the shim treats that as unreachable and blocks in strict). The epoch passed to decide is the
        payload's epoch when present, else 0 (the daemon holds no per-session epoch fence off-fleet).

        The decision-bearing hook is also BEST-EFFORT appended to the observed conversation lane.
        Recording is enrichment-only: it never changes the response returned to the shim."""
        from . import hooked

        if self.policy is None:
            return {"status": "DENIED", "code": "DENIED_POLICY_NOT_LOADED",
                    "required_action": "the daemon has no policy engine loaded"}
        payload = args.get("payload")
        if not isinstance(payload, dict):
            return {"status": "DENIED", "code": "DENIED_HOOKS_PAYLOAD_REQUIRED",
                    "required_action": "hooks/decide needs a payload object"}
        hook_event_name = str(args.get("hook_event_name") or payload.get("hook_event_name") or "")
        if hook_event_name not in hooked.DECISION_EVENTS:
            return {"status": "DENIED", "code": "DENIED_HOOK_EVENT_INVALID",
                    "hook_event_name": hook_event_name,
                    "required_action": "hooks/decide accepts PreToolUse|UserPromptSubmit"}
        try:
            epoch = int(payload.get("epoch"))
        except (TypeError, ValueError):
            epoch = 0
        if hook_event_name == hooked.USER_PROMPT_SUBMIT:
            cwd = payload.get("cwd")
            block, reason = hooked.guard_prompt(
                str(payload.get("prompt") or ""), engine=self.policy, cwd=str(cwd) if cwd else None
            )
            response = {"status": "OK", "hook_event_name": hook_event_name, "block": block, "reason": reason}
        else:
            # Default to the PreToolUse path (the perm-relevant gate).
            action = hooked.action_from_pretooluse(payload, engine="claude", epoch=epoch)
            decision = self.policy.decide(action, current_epoch=epoch)
            response = {
                "status": "OK",
                "hook_event_name": hook_event_name or hooked.PRE_TOOL_USE,
                "result": decision.result,
                "reason": decision.reason,
                "dedupe_key": decision.dedupe_key,
                "rule_id": decision.rule_id,
                "invariant_id": decision.invariant_id,
            }
        self._record_hook_decision(payload, response)
        return response

    def _hooks_record(self, args: dict[str, Any]) -> dict[str, Any]:
        """Best-effort ingress for record-only observed Claude lifecycle hooks.

        The shim ignores this response and always exits 0. Recorder failures become OK/not-recorded so
        a ledger outage cannot alter Claude's host lifecycle.
        """
        from . import hooked

        payload = args.get("payload")
        if not isinstance(payload, dict):
            return {"status": "OK", "recorded": False, "code": "OBSERVED_PAYLOAD_INVALID"}
        hook_event_name = str(args.get("hook_event_name") or payload.get("hook_event_name") or "")
        if hook_event_name not in hooked.RECORD_ONLY_EVENTS:
            return {"status": "OK", "recorded": False, "code": "OBSERVED_EVENT_INVALID"}
        normalized = dict(payload)
        normalized["hook_event_name"] = hook_event_name
        try:
            return {"status": "OK", **self._record_observed_payload(normalized)}
        except Exception as error:  # noqa: BLE001 -- lifecycle capture must never affect Claude
            self.log(f"observed hook record failed (lifecycle unaffected): {type(error).__name__}: {error}")
            return {"status": "OK", "recorded": False, "code": "OBSERVED_RECORD_FAILED"}

    def _record_hook_decision(self, payload: dict[str, Any], response: dict[str, Any]) -> None:
        """Best-effort observed capture. A ledger failure never mutates the policy response."""
        try:
            normalized = dict(payload)
            normalized["hook_event_name"] = str(
                response.get("hook_event_name") or payload.get("hook_event_name") or ""
            )
            self._record_observed_payload(normalized, response=response)
        except Exception as error:  # noqa: BLE001 -- recording never affects the policy decision
            self.log(f"hook decision record failed (decision unaffected): {type(error).__name__}: {error}")

    def _record_observed_payload(self, payload: dict[str, Any],
                                 response: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append one observed hook delivery under the vendor session's current linked T5."""
        from . import hooked

        vendor_session_id = str(payload.get("session_id") or "").strip()
        event = str(payload.get("hook_event_name") or "")
        if not vendor_session_id:
            return {"recorded": False, "code": "OBSERVED_SESSION_ID_MISSING"}

        with self._hook_runs_lock:
            if event == hooked.SESSION_START:
                run_id, deduplicated = self._start_observed_lifecycle(vendor_session_id, payload)
                return {"recorded": True, "session_id": self._hook_sessions[vendor_session_id],
                        "agent_run_id": run_id, "deduplicated": deduplicated}
            if event == hooked.SESSION_END:
                return self._end_observed_lifecycle(vendor_session_id, payload)

            run_id = self._hook_run_for(vendor_session_id, create=True)
            if run_id is None:
                return {"recorded": False, "code": "OBSERVED_RUN_UNAVAILABLE"}

            if event == hooked.USER_PROMPT_SUBMIT:
                chat_source = self._record_observed_chat(run_id, "user", str(payload.get("prompt") or ""))
                self._record_observed_policy_point(
                    run_id, payload, response or {}, source_event_id=f"{chat_source}-policy"
                )
            elif event == hooked.PRE_TOOL_USE:
                self._record_observed_policy_point(run_id, payload, response or {})
            elif event == hooked.STOP:
                message = payload.get("last_assistant_message")
                if isinstance(message, str) and message:
                    chat_source = self._record_observed_chat(run_id, "assistant", message)
                    stop_source = f"{chat_source}-stop"
                else:
                    stop_source = self._turn_scoped_source_id(run_id, "stop-empty", payload)
                self._record_observed_hook_point(
                    run_id, "Stop", source_event_id=stop_source, code="stop"
                )
            elif event == hooked.STOP_FAILURE:
                self._record_observed_stop_failure(run_id, payload)
            else:
                return {"recorded": False, "code": "OBSERVED_EVENT_INVALID"}
            return {"recorded": True, "session_id": self._hook_sessions[vendor_session_id],
                    "agent_run_id": run_id}

    @staticmethod
    def _hook_digest(value: Any) -> str:
        """Return a 128-bit hex digest of canonical JSON-native hook identity fields."""

        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _session_start_source_id(self, payload: dict[str, Any]) -> str:
        """Stable SessionStart dedupe id over field-subset (session_id, source, model, transcript_path, cwd)."""
        stable = {key: payload.get(key) for key in (
            "session_id", "source", "model", "transcript_path", "cwd"
        )}
        return f"claude-session-start-{self._hook_digest(stable)}"

    def _observed_session_for(self, vendor_session_id: str, *, create: bool) -> str | None:
        """Resolve/create observed claude session for a vendor id; cache→DB truth; create-gated."""
        from .. import db, session_records

        cached = self._hook_sessions.get(vendor_session_id)
        if cached:
            row = db.fetch_one("SELECT id FROM agent_sessions WHERE id = ?", (cached,))
            if row:
                return cached
            self._hook_sessions.pop(vendor_session_id, None)
        row = db.fetch_one(
            "SELECT id FROM agent_sessions WHERE controller = 'observed' AND engine = 'claude' "
            "AND vendor_session_root_id = ? ORDER BY created_at, id LIMIT 1",
            (vendor_session_id,),
        )
        if row:
            session_id = str(row[0])
            self._hook_sessions[vendor_session_id] = session_id
            return session_id
        if not create:
            return None
        result = session_records.session_start(_Namespace(
            payload_json=json.dumps({
                "controller": "observed", "mode": "hooked", "engine": "claude",
                "auth_mode": "none", "vendor_session_root_id": vendor_session_id,
                "state": "open", "summary": "observed Claude conversation",
            }),
            test=self._driven_test_records,
        ))
        session_id = str(result["id"])
        self._hook_sessions[vendor_session_id] = session_id
        return session_id

    def _latest_observed_run(self, session_id: str) -> tuple[str, bool] | None:
        """(run_id, terminal?) of newest run for a session, else None."""
        from .. import db

        row = db.fetch_one(
            "SELECT id FROM agent_runs WHERE session_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (session_id,),
        )
        if not row:
            return None
        run_id = str(row[0])
        state = self._safe_reduce(run_id)
        return run_id, bool(state and state.get("terminal"))

    def _hook_run_for(self, vendor_session_id: str, *, create: bool = True) -> str | None:
        """Resolve the active observed T5 from memory or DB truth, optionally opening one."""
        existing = self._hook_runs.get(vendor_session_id)
        if existing:
            state = self._safe_reduce(existing)
            if state is not None and not state.get("terminal"):
                return existing
            self._hook_runs.pop(vendor_session_id, None)
        session_id = self._observed_session_for(vendor_session_id, create=create)
        if session_id is None:
            return None
        latest = self._latest_observed_run(session_id)
        if latest and not latest[1]:
            self._hook_runs[vendor_session_id] = latest[0]
            return latest[0]
        if not create:
            return None
        return self._open_observed_run(vendor_session_id, session_id)

    def _open_observed_run(self, vendor_session_id: str, session_id: str,
                           *, model: str | None = None) -> str:
        """Opens a T5 observed run, records ownership, caches; returns run_id."""
        run_payload: dict[str, Any] = {
            "agent_type": "claude", "surface": "cli", "session_id": session_id,
            "engine": "claude", "auth_mode": "none",
            "summary": "observed Claude host lifecycle", "body": "",
        }
        if model:
            run_payload["model"] = model
        run_result = agent_runs.agent_run_start(_Namespace(
            payload_json=json.dumps(run_payload),
            summary=None, body=None, task_id=None, agent_type=None, surface=None,
            test=self._driven_test_records,
        ))
        run_id = str(run_result["id"])
        self._record_ownership(run_id)
        self._hook_runs[vendor_session_id] = run_id
        return run_id

    def _start_observed_lifecycle(self, vendor_session_id: str,
                                  payload: dict[str, Any]) -> tuple[str, bool]:
        """Idempotent SessionStart: dedupe by source id, supersede stale run, open + record start."""
        session_id = self._observed_session_for(vendor_session_id, create=True)
        assert session_id is not None
        source_event_id = self._session_start_source_id(payload)
        current = self._hook_run_for(vendor_session_id, create=False)
        if current and self._event_exists(current, source_event_id):
            return current, True
        if current:
            self._finalize_observed_run(
                vendor_session_id, current, "canceled", "observed lifecycle superseded by SessionStart"
            )
        model_value = payload.get("model")
        model = str(model_value) if isinstance(model_value, str) and model_value else None
        run_id = self._open_observed_run(vendor_session_id, session_id, model=model)
        source = self._safe_hook_code(payload.get("source") or "startup")
        self._record_observed_hook_point(
            run_id, "SessionStart", source_event_id=source_event_id, code=source,
            body={"hook_event_name": "SessionStart", "source": source},
        )
        return run_id, False

    def _end_observed_lifecycle(self, vendor_session_id: str,
                                payload: dict[str, Any]) -> dict[str, Any]:
        """SessionEnd: record end point + finalize; dedup/already-final payload shape."""
        session_id = self._observed_session_for(vendor_session_id, create=False)
        if session_id is None:
            return {"recorded": False, "code": "OBSERVED_SESSION_NOT_FOUND"}
        run_id = self._hook_run_for(vendor_session_id, create=False)
        if run_id is None:
            latest = self._latest_observed_run(session_id)
            if latest and latest[1]:
                return {"recorded": True, "session_id": session_id, "agent_run_id": latest[0],
                        "deduplicated": True, "finalized": True, "finalized_now": False}
            return {"recorded": False, "session_id": session_id, "code": "OBSERVED_RUN_NOT_ACTIVE"}
        reason = self._safe_hook_code(payload.get("reason") or "session_end")
        source_event_id = f"claude-session-end-{self._hook_digest({
            'session_id': payload.get('session_id'), 'reason': reason,
        })}"
        self._record_observed_hook_point(
            run_id, "SessionEnd", source_event_id=source_event_id, code=reason,
            body={"hook_event_name": "SessionEnd", "reason": reason},
        )
        finalized = self._finalize_observed_run(
            vendor_session_id, run_id, "success", "observed Claude lifecycle ended"
        )
        return {"recorded": True, "session_id": session_id, "agent_run_id": run_id,
                "deduplicated": not finalized, "finalized": True, "finalized_now": finalized}

    def _finalize_observed_run(self, vendor_session_id: str, run_id: str,
                               conclusion: str, summary: str) -> bool:
        """Finalize, tolerate ALREADY_FINALIZED, drop cache + clear ownership; returns finalized-now."""
        finalized = True
        try:
            agent_runs.agent_run_finalize(_Namespace(
                agent_run_id=run_id, conclusion=conclusion, summary=summary, body=""
            ))
        except KaizenDenied as denied:
            if denied.code != "DENIED_AGENT_RUN_ALREADY_FINALIZED":
                raise
            finalized = False
        if self._hook_runs.get(vendor_session_id) == run_id:
            self._hook_runs.pop(vendor_session_id, None)
        self._clear_ownership(run_id)
        return finalized

    def _event_exists(self, run_id: str, source_event_id: str) -> bool:
        """True iff an agent_event (run_id, source_event_id) exists."""
        from .. import db

        return bool(db.fetch_one(
            "SELECT id FROM agent_events WHERE agent_run_id = ? AND source_event_id = ?",
            (run_id, source_event_id),
        ))

    def _record_observed_chat(self, run_id: str, role: str, text: str) -> str:
        """Records a chat_message event; returns its source_event_id."""
        source_event_id = self._chat_source_event_id(run_id, role, text)
        self._record_chat_message(
            run_id, role, text, source="observed", source_event_id=source_event_id
        )
        return source_event_id

    def _chat_source_event_id(self, run_id: str, role: str, text: str) -> str:
        """Dedupe id for a chat msg (reuse last if same role+digest, else ordinal+digest)."""
        from .. import db

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
        latest = db.fetch_one(
            "SELECT source_event_id, body FROM agent_events WHERE agent_run_id = ? "
            "AND event_kind = 'chat_message' ORDER BY sequence_no DESC LIMIT 1",
            (run_id,),
        )
        if latest and str(latest[0] or "").endswith(f"-{digest}"):
            try:
                latest_body = json.loads(latest[1] or "{}")
            except (TypeError, ValueError):
                latest_body = {}
            if latest_body.get("role") == role:
                return str(latest[0])
        count_row = db.fetch_one(
            "SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'chat_message'",
            (run_id,),
        )
        ordinal = int(count_row[0] if count_row else 0) + 1
        return f"claude-chat-{role}-{ordinal}-{digest}"

    def _turn_scoped_source_id(self, run_id: str, label: str, payload: dict[str, Any]) -> str:
        """Per-turn dedupe id from chat count and stable hook identity fields."""
        from .. import db

        count_row = db.fetch_one(
            "SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'chat_message'",
            (run_id,),
        )
        turn_ordinal = int(count_row[0] if count_row else 0)
        stable = {key: payload.get(key) for key in (
            "session_id", "hook_event_name", "tool_use_id", "error_type", "error", "reason",
        )}
        return f"claude-{label}-{turn_ordinal}-{self._hook_digest(stable)}"

    def _record_observed_policy_point(self, run_id: str, payload: dict[str, Any],
                                      response: dict[str, Any],
                                      source_event_id: str | None = None) -> None:
        """Records a PreToolUse/UserPromptSubmit decision point."""
        event = str(payload.get("hook_event_name") or "")
        if response.get("block") is not None:
            decision = "block" if response.get("block") else "allow"
        else:
            decision = self._safe_hook_code(response.get("result") or "unknown")
        tool_name = self._safe_hook_code(payload.get("tool_name") or "tool")
        tool_use_id = str(payload.get("tool_use_id") or "").strip()
        if source_event_id is None:
            digest = self._hook_digest(tool_use_id) if tool_use_id else self._hook_digest({
                "event": event, "tool": tool_name, "tool_input": payload.get("tool_input")
            })
            source_event_id = f"claude-policy-{digest}"
        correlation_id = tool_use_id or str(payload.get("session_id") or "")
        body = {
            "hook_event_name": event,
            "decision": decision,
            "tool_name": tool_name if event == "PreToolUse" else None,
            "tool_use_id": tool_use_id or None,
            "rule_id": response.get("rule_id"),
            "invariant_id": response.get("invariant_id"),
        }
        self._record_observed_hook_point(
            run_id, event, source_event_id=source_event_id, code=decision,
            correlation_id=correlation_id, body={k: v for k, v in body.items() if v is not None},
        )

    def _record_observed_stop_failure(self, run_id: str, payload: dict[str, Any]) -> None:
        """Maps StopFailure error_type to a failure event_kind; funnels directly (not via hook_point)."""
        error_value: Any = payload.get("error_type") or payload.get("error") or payload.get("reason")
        if isinstance(error_value, dict):
            error_value = error_value.get("type") or error_value.get("code") or error_value.get("name")
        error_type = self._safe_hook_code(error_value or "unknown")
        event_kind = {
            "rate_limit": "rate_limit",
            "billing_error": "auth",
            "authentication_failed": "auth",
            "invalid_request": "context",
        }.get(error_type, "transport")
        source_event_id = self._turn_scoped_source_id(run_id, f"stop-failure-{error_type}", payload)
        self.funnel_event(
            run_id, event_kind, "point",
            summary=f"observed StopFailure {error_type}", code=error_type,
            source_event_id=source_event_id,
            body=json.dumps({"hook_event_name": "StopFailure", "error_type": error_type}),
        )

    @staticmethod
    def _safe_hook_code(value: Any) -> str:
        """Sanitize to lowercase alnum/_-. , <=80 chars, "unknown" fallback."""
        text = str(value or "unknown").strip().lower().replace(" ", "_")
        safe = "".join(ch for ch in text if ch.isalnum() or ch in ("_", "-", "."))
        return (safe or "unknown")[:80]

    def _record_observed_hook_point(self, run_id: str, event: str, *, source_event_id: str,
                                    code: str, correlation_id: str | None = None,
                                    body: dict[str, Any] | None = None) -> None:
        """Funnels a generic observed "hook_event" point."""
        self.funnel_event(
            run_id, "hook_event", "point", summary=f"observed {event} {code}",
            correlation_id=correlation_id, code=code, source_event_id=source_event_id,
            body=json.dumps(body or {"hook_event_name": event}),
        )

    def status_payload(self) -> dict[str, Any]:
        """Daemon status snapshot; -1 sentinels on DB read failure (fails closed)."""
        try:
            from .. import db
            connection = db.connect()
            try:
                pending_row = connection.execute(
                    "SELECT COUNT(*) FROM approval_requests WHERE state = 'pending'"
                ).fetchone()
                pending_approvals = int(pending_row[0] if pending_row else 0)
                active_row = connection.execute(
                    "SELECT COUNT(*) FROM agent_runs AS run WHERE NOT EXISTS ("
                    "SELECT 1 FROM agent_events AS event WHERE event.agent_run_id = run.id "
                    "AND event.event_kind = 'finalization' "
                    "AND event.marker IN ('close_ok','close_fail','close_canceled'))"
                ).fetchone()
                active_durable_runs = int(active_row[0] if active_row else 0)
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 -- status stays available but fails closed on unknown cleanup state
            pending_approvals = -1
            active_durable_runs = -1
        with self._driven_lock:
            driven_sessions = len(self._driven)
        return {
            "status": "OK",
            "running": True,
            "pid": os.getpid(),
            "nonce": self.nonce,
            "transport": self._loopback.transport if self._loopback else None,
            "owned_children": len(self._children),
            "driven_sessions": driven_sessions,
            "pending_approvals": pending_approvals,
            "active_durable_runs": active_durable_runs,
            "writer_claim_active": self._writer_lease.active,
            "provider_target_fingerprint": (
                self._claude_provider_target.fingerprint if self._claude_provider_target_frozen
                and self._claude_provider_target is not None else None
            ),
            "repo_root": str(self.repo_root),
            # M8 (§D): the UI's dynamic engine selector enumerates lanes from ADAPTER REGISTRATION
            # (modules present in orchestration/adapters), never a hard-coded engine list -- an absent
            # lane (e.g. claude before M-CLAUDE) simply is not reported and the UI greys it out.
            "engines": _registered_engines(),
            # M8: the UI gates unowned sessions read-only against THIS daemon's fleet node id (None when
            # distribution is off -- ownership fences only exist on fleet-fenced sessions).
            "node_id": self._my_node_id(),
        }

    # --- run loop ---------------------------------------------------------

    def run_forever(self, exit_after_boot: bool = False) -> int:
        """CLI loop: boot, then heartbeat with parent-liveness + exponential backoff.
        ``exit_after_boot`` (undocumented test seam) returns straight after the sweep."""
        if not self._booted:
            self.boot()
        if exit_after_boot:
            self.shutdown()
            return 0
        backoff = _BACKOFF_BASE
        try:
            while not self._stop.is_set():
                if not self._parent_alive():
                    self.log("parent process gone; self-quiescing")
                    break
                # Steady heartbeat; on any transient hiccup the backoff prevents a hot
                # loop (never a fixed spin).
                healthy = self._heartbeat()
                if healthy:
                    backoff = _BACKOFF_BASE
                    self._stop.wait(_HEARTBEAT_SECONDS)
                else:
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX)
        finally:
            self.shutdown()
        return 0

    def _parent_alive(self) -> bool:
        """Check the original parent; only POSIX permits a live reparent to keep the daemon running."""

        current_ppid = os.getppid()
        if current_ppid == self._parent_ppid:
            return children.pid_alive(current_ppid)
        return os.name == "posix" and current_ppid > 1 and children.pid_alive(current_ppid)

    def _heartbeat(self) -> bool:
        # Reap any child that exited so its ownership can be cleared; keep the loop cheap.
        """REFINE: reaps exited children, returns True (healthy sentinel — always)."""
        for run_id, child in list(self._children.items()):
            if child.poll() is not None:
                self._children.pop(run_id, None)
        return True

    def shutdown(self) -> None:
        """Reap every owned child (Job Object close / process-group kill), stop loopback,
        release the pidfile. Idempotent."""
        for run_id, child in list(self._children.items()):
            try:
                child.kill_tree()
            except Exception:  # noqa: BLE001 -- teardown must not raise past shutdown
                pass
            self._children.pop(run_id, None)
        # H0: tear down driven sessions BEFORE loopback stops (deny waiters -> kill -> join -> force-
        # finalize any non-terminal run). A parked approval must fail closed before the control channel
        # dies, and a driven run must not be left non-terminal across a daemon stop.
        self._shutdown_driven()
        with self._session_policy_gates_lock:
            self._session_policy_gates.clear()
            self._policy_gate_by_run.clear()
        # M11: stop the HTTP control service BEFORE loopback + fleet teardown (its worker threads reach
        # the fleet handle; close them first). Idempotent .stop().
        if self._control is not None:
            try:
                self._control.stop()
            except Exception:  # noqa: BLE001 -- teardown must not raise past shutdown
                pass
            self._control = None
        if self._loopback is not None:
            self._loopback.stop()
            self._loopback = None
        if self._fleet is not None:
            try:
                self._fleet.close()
            except Exception:  # noqa: BLE001 -- teardown must not raise past shutdown
                pass
            self._fleet = None
        self._release_single_instance()
        try:
            self._ownership_registry.close()
        except Exception:  # noqa: BLE001 -- final handle release must not escape teardown
            pass
        self._booted = False


# --- CLI entry points (thin) --------------------------------------------------

def run_daemon(exit_after_boot: bool = False) -> dict[str, Any]:
    """``daemon run`` runtime. Returns a JSON-able summary; on single-instance conflict
    returns a DENIED payload (exit 2) instead of raising."""
    supervisor = Supervisor()
    try:
        try:
            boot = supervisor.boot()
        except SingleInstanceError as error:
            return {
                "status": "DENIED",
                "code": "DENIED_DAEMON_ALREADY_RUNNING",
                "pid": error.pid,
                "nonce": error.nonce,
                "required_action": "a supervisor already owns this workspace; use daemon status",
                "exit_code": 2,
            }
        if exit_after_boot:
            return {
                "status": "OK",
                "booted_and_exited": True,
                "nonce": supervisor.nonce,
                "swept": boot["swept"],
                "repo_root": str(supervisor.repo_root),
            }
        supervisor.run_forever()
        return {"status": "OK", "stopped": True}
    finally:
        supervisor.shutdown()


def query_status(repo_root: Path | None = None, timeout: float = 5.0) -> dict[str, Any]:
    """``daemon status`` = a loopback client. Clean 'not running' when no daemon listens."""
    return send_control(op="status", args={}, repo_root=repo_root, timeout=timeout)


def send_control(*, op: str, args: dict[str, Any], repo_root: Path | None = None, timeout: float = 5.0) -> dict[str, Any]:
    """Generic loopback client for the daemon control ops (M14 daemon verbs use this like ``status``).
    Resolves the control token + transport under the runtime dir; a clean 'not running' payload when no
    daemon listens (never a traceback) -- the same not-running shape ``status`` returns."""
    root = Path(os.path.abspath(repo_root or REPO_ROOT))
    runtime = root / "AI" / "work" / "orchestration" / "runtime"
    relative = (runtime / "control.token").relative_to(root).as_posix()
    authority = WorkspacePathAuthority(root)
    try:
        if authority.identity(relative) is None:
            return {"status": "OK", "running": False, "reason": "no control token (daemon not running)"}
        token = authority.read(relative, _TOKENFILE_MAX_BYTES).data.decode("utf-8").strip()
    except (UnicodeDecodeError, WorkspacePathError):
        return {
            "status": "DENIED",
            "code": "DENIED_WORKSPACE_PATH_AUTHORITY",
            "running": False,
            "reason": "control token path could not be proven",
        }
    finally:
        authority.close()
    try:
        response = loopback.send_request(
            root, runtime, {"op": op, "token": token, "args": args, "epoch": 0}, timeout=timeout
        )
    except (ConnectionError, OSError) as error:
        return {"status": "OK", "running": False, "reason": f"daemon not reachable: {error}"}
    return response
