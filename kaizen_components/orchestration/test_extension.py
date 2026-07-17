"""Bounded outer runner for the Kaizen Test Extension acceptance plane.

The runner starts only from an explicit visible terminal.  It creates a fresh
non-system-drive data plane and owns the Kaizen daemon and VS Code Extension
Development Host as sibling child trees.  The child extension drives the normal
session protocol; this process enforces the wall clock and suite call reservation,
records sanitized JSONL evidence, and proves teardown.

Credential files are never opened, copied, hashed, named, or logged.  The official
provider runtime discovers any existing vendor-managed identity outside Kaizen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import struct
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import turso

from .. import agent_runs
from . import children, claude_runtime, loopback
from .writer_lease import OwnershipRegistry, OwnershipStateError

MAX_WALL_SECONDS = 30 * 60
WALL_CLOCK_CODE = "TEST_EXTENSION_WALL_CLOCK_EXHAUSTED"
MAX_SIMULTANEOUS_TURNS = 2
MAX_CALL_CEILING = 256
CONTROL_VERSION = 1
ACTION_PROTOCOL = "test-extension-action-v2"
ACTION_REQUEST_MAX_BYTES = 32 * 1024
ACTION_LIMIT = 64
EDH_READY_MAX_BYTES = 16 * 1024
EDH_READY_TIMEOUT_SECONDS = 60.0
EDH_MAIN_LOG_MAX_BYTES = 256 * 1024
EDH_LOG_DIRECTORY_LIMIT = 8
EDH_EXIT_CODE = "DENIED_TEST_EXTENSION_EDH_EXIT"
EDH_SPAWN_CODE = "DENIED_TEST_EXTENSION_EDH_SPAWN"
VSCODE_UPDATE_IN_PROGRESS_CODE = "DENIED_TEST_EXTENSION_VSCODE_UPDATE_IN_PROGRESS"
_VSCODE_UPDATE_IN_PROGRESS_SIGNATURE = (
    b"Code is currently being updated. Please wait for the update to complete before launching."
)
# Worst bounded provider discovery is 137s (Ollama 2 + Codex 30 + Claude open/catalog/probes/teardown
# 105). Allow cold process and filesystem startup margin without exceeding the suite's 30-minute cap.
DAEMON_READY_TIMEOUT_SECONDS = 210.0
INTERNAL_ERROR_CODE = "DENIED_TEST_EXTENSION_INTERNAL_ERROR"

CLAUDE_SCENARIOS = frozenset(
    {
        "claude-text-stream",
        "claude-image-codeword",
        "claude-governed-context",
        "claude-diff-accept",
        "claude-diff-reject",
        "claude-process-approval",
        "claude-plan-controls",
        "claude-traversal-zero-record",
        "claude-diff-stale",
        "claude-diff-corrupt",
        "claude-diff-timeout",
        "claude-writer-conflict",
        "claude-interrupt-restart",
        "cleanup-leak-state",
    }
)
OLLAMA_SCENARIOS = frozenset(
    {"ollama-text-stream", "ollama-governed-context", "ollama-tool-policy"}
)
ALL_SCENARIOS = CLAUDE_SCENARIOS | OLLAMA_SCENARIOS

EXTERNAL_SCENARIO_BLOCK_CODES = frozenset(
    {
        "DENIED_AUTH_UNAVAILABLE",
        "DENIED_AUTH_MODE_MISMATCH",
        "DENIED_MODEL_UNAVAILABLE",
        "DENIED_SDK_UNAVAILABLE",
        "DENIED_PROVIDER_CAPACITY",
        "RATE_LIMIT",
        "RATE_LIMITED",
        "RATE_LIMIT_EXHAUSTED",
        "DENIED_RATE_LIMIT",
        "DENIED_RATE_LIMITED",
        "DENIED_RATE_LIMIT_EXHAUSTED",
        "QUOTA_EXHAUSTED",
        "DENIED_QUOTA_EXHAUSTED",
        "SUBSCRIPTION_RATE_LIMIT_EXHAUSTED",
        "SUBSCRIPTION_QUOTA_EXHAUSTED",
    }
)

_CLIENT_STRING_FIELDS = frozenset(
    {
        "scenario", "provider", "status", "code", "attested_model", "attested_effort",
        "approval_status", "decision", "tool_status", "denial_code", "invariant_denial_code",
        "process_request_sha256",
        "process_stdout_sha256", "diff_path", "diff_snapshot_set_sha256",
        "diff_refreshed_snapshot_set_sha256", "diff_before_sha256", "diff_proposed_sha256",
        "diff_final_sha256", "diff_mutated_base_sha256", "diff_refreshed_before_sha256",
        "diff_corrupted_artifact_sha256",
        "session_id", "agent_run_id", "correlation_id",
        "previous_agent_run_id",
    }
)
_CLIENT_INTEGER_FIELDS = frozenset(
    {
        "calls_reserved", "delta_count", "durable_replacements", "image_refs", "context_refs",
        "diff_card_count", "tool_card_count", "process_exit_code", "process_stdout_bytes",
        "pending_approvals", "running_cards", "passed", "failed", "not_run",
        "diff_revision", "diff_refreshed_revision",
    }
)
_CLIENT_BOOLEAN_FIELDS = frozenset(
    {
        "ordered", "codeword_seen", "approval_ui", "profile_attested", "stream_suppressed",
        "selection_exact", "diff_path_matches", "diff_before_matches", "diff_proposed_hashed",
        "diff_final_matches", "diff_revision_refreshed", "diff_corrupt", "tool_request_matches",
        "tool_result_matches", "tool_zero_execution", "tool_zero_stdout", "zero_records",
        "conversation_idle", "interrupted", "restart_resumable",
        "tool_decision_approved", "tool_output_complete", "diff_second_accept",
        "diff_authoritative_denial", "writer_conflict_seen", "writer_loser_zero_records",
        "writer_lease_retained", "writer_lease_released", "restart_same_session",
        "restart_new_run", "restart_reduced_fidelity", "queue_empty", "runtime_children_clean",
    }
)
_CLIENT_FIELDS = _CLIENT_STRING_FIELDS | _CLIENT_INTEGER_FIELDS | _CLIENT_BOOLEAN_FIELDS

_ACTION_COMMON_FIELDS = frozenset(
    {
        "protocol", "suite_nonce", "request_sha256", "sequence", "action_id", "scenario_id", "phase",
    }
)
_ACTION_APPROVAL_FIELDS = frozenset(
    {
        "conversation_id", "session_id", "agent_run_id", "correlation_id", "revision",
        "snapshot_set_sha256", "workspace_path", "before_sha256", "proposed_sha256",
    }
)
_ACTION_PHASES: dict[str, tuple[str, frozenset[str]]] = {
    "suite.stop": ("suite", _ACTION_COMMON_FIELDS | {"reason"}),
    "traversal.arm_zero_records": (
        "claude-traversal-zero-record",
        _ACTION_COMMON_FIELDS,
    ),
    "traversal.verify_zero_records": (
        "claude-traversal-zero-record",
        _ACTION_COMMON_FIELDS | {"denial_code"},
    ),
    "stale.mutate_base": ("claude-diff-stale", _ACTION_COMMON_FIELDS | _ACTION_APPROVAL_FIELDS),
    "corrupt.snapshot": ("claude-diff-corrupt", _ACTION_COMMON_FIELDS | _ACTION_APPROVAL_FIELDS),
    "writer.holder_open": ("claude-writer-conflict", _ACTION_COMMON_FIELDS | _ACTION_APPROVAL_FIELDS),
    "writer.loser_arm": (
        "claude-writer-conflict",
        _ACTION_COMMON_FIELDS | _ACTION_APPROVAL_FIELDS | {"loser_conversation_id", "loser_request_token"},
    ),
    "writer.loser_verify": (
        "claude-writer-conflict",
        _ACTION_COMMON_FIELDS | _ACTION_APPROVAL_FIELDS
        | {"loser_conversation_id", "loser_request_token", "denial_code"},
    ),
    "writer.holder_release": ("claude-writer-conflict", _ACTION_COMMON_FIELDS | _ACTION_APPROVAL_FIELDS),
    "restart.daemon": (
        "claude-interrupt-restart",
        _ACTION_COMMON_FIELDS | {"conversation_id", "session_id", "agent_run_id"},
    ),
    "restart.cleanup": (
        "claude-interrupt-restart",
        _ACTION_COMMON_FIELDS
        | {"conversation_id", "session_id", "agent_run_id", "previous_agent_run_id"},
    ),
}
_SCENARIO_PHASE_ORDER: dict[str, tuple[str, ...]] = {
    "claude-traversal-zero-record": (
        "traversal.arm_zero_records", "traversal.verify_zero_records",
    ),
    "claude-diff-stale": ("stale.mutate_base",),
    "claude-diff-corrupt": ("corrupt.snapshot",),
    "claude-writer-conflict": (
        "writer.holder_open", "writer.loser_arm", "writer.loser_verify", "writer.holder_release",
    ),
    "claude-interrupt-restart": ("restart.daemon", "restart.cleanup"),
}
_ACTION_FILE_RE = re.compile(r"^(?P<sequence>[0-9]{8})-(?P<action_id>[A-Za-z0-9_-]{1,96})\.json$")


class TestExtensionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _TestExtensionStopRequested(RuntimeError):
    """Internal control flow after an exact nonce-bound preapproval Stop."""


@dataclass(frozen=True)
class RunLayout:
    run_id: str
    root: Path
    workspace: Path
    user_data: Path
    extensions: Path
    temp: Path
    evidence: Path
    client_evidence: Path
    control: Path
    action_root: Path
    action_requests: Path
    action_receipts: Path
    action_suite: Path
    manifest: Path
    edh_ready: Path

    @property
    def shared_data(self) -> Path:
        return self.root / "vscode-shared-data"


@dataclass(frozen=True)
class SuiteRequest:
    provider: str
    model: str
    effort: str | None
    max_turns: int
    call_ceiling: int
    scenarios: tuple[str, ...]
    provider_retries: int = 0
    suite_nonce: str = ""
    request_sha256: str = ""

    @property
    def reserved_calls(self) -> int:
        # Reserve the entire per-turn ceiling for every scenario.  This is a hard,
        # deterministic upper bound even when a provider performs tool round trips.  Restart recovery
        # intentionally opens a second provider turn; the losing writer is denied before provider use.
        turns = sum(2 if scenario == "claude-interrupt-restart" else 1 for scenario in self.scenarios)
        return turns * self.max_turns


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_request_sha256(value: Mapping[str, Any]) -> str:
    canonical = {key: item for key, item in value.items() if key != "request_sha256"}
    return hashlib.sha256(_canonical_json(canonical)).hexdigest()


def _under(root: Path, candidate: Path) -> bool:
    root, candidate = root.resolve(), candidate.resolve()
    return candidate == root or root in candidate.parents


def require_non_system_drive(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_absolute():
        raise TestExtensionError("DENIED_TEST_EXTENSION_PATH", f"{label} must be absolute")
    system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\/").casefold()
    if os.name == "nt" and resolved.drive.rstrip("\\/").casefold() == system_drive:
        raise TestExtensionError("DENIED_TEST_EXTENSION_SYSTEM_DRIVE", f"{label} must be on a non-system drive")
    return resolved


def default_plane_base(source_root: Path, environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    devroot = env.get("DEVROOT")
    base = Path(devroot) / "test-extension" if devroot else source_root.parent / "test-extension"
    return require_non_system_drive(base, label="plane base")


def create_layout(source_root: Path, plane_base: Path, *, run_id: str | None = None) -> RunLayout:
    source = require_non_system_drive(source_root, label="source root")
    base = require_non_system_drive(plane_base, label="plane base")
    if _under(source, base):
        raise TestExtensionError(
            "DENIED_TEST_EXTENSION_SOURCE_MUTATION", "the test plane must be outside the source workspace"
        )
    identifier = run_id or f"te-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    if not identifier.startswith("te-") or any(
        ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in identifier
    ):
        raise TestExtensionError("DENIED_TEST_EXTENSION_RUN_ID", "invalid test-extension run id")
    root = (base / "runs" / identifier).resolve()
    if root.exists():
        raise TestExtensionError("DENIED_TEST_EXTENSION_RUN_EXISTS", "test-extension run root already exists")
    action_root = root / "actions"
    return RunLayout(
        identifier,
        root,
        root / "fixture-workspace",
        root / "vscode-user-data",
        root / "vscode-extensions",
        root / "tmp",
        root / "evidence.jsonl",
        root / "client-evidence.jsonl",
        root / "control.json",
        action_root,
        action_root / "requests",
        action_root / "receipts",
        action_root / "suite.json",
        root / "manifest.json",
        root / "edh-ready.json",
    )


_FONT: dict[str, tuple[str, ...]] = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
}


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def codeword_png(text: str, scale: int = 4) -> bytes:
    glyphs = [_FONT.get(ch, _FONT["-"]) for ch in text.upper()]
    width, height = 4 * scale + max(1, len(glyphs) * 6 - 1) * scale, 11 * scale
    pixels = [bytearray([255] * width) for _ in range(height)]
    x, y = 2 * scale, 2 * scale
    for glyph in glyphs:
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit == "1":
                    for dy in range(scale):
                        start = x + column * scale
                        pixels[y + row * scale + dy][start : start + scale] = b"\x00" * scale
        x += 6 * scale
    raw = b"".join(b"\x00" + bytes(row) for row in pixels)
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def prepare_plane(layout: RunLayout, source_root: Path) -> dict[str, str]:
    source = require_non_system_drive(source_root, label="source root")
    for directory in (
        layout.workspace / ".git",
        layout.workspace / "AI" / "db",
        layout.workspace / "AI" / "work",
        layout.user_data / "User",
        layout.extensions,
        layout.shared_data,
        layout.temp,
        layout.action_requests,
        layout.action_receipts,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    short = layout.run_id.rsplit("-", 1)[-1].upper()
    codewords = {
        "text": f"TETEXT_{short}",
        "context": f"TECTX_{short}",
        "selection": f"TESEL_{short}",
        "image": f"TEIMG_{short}",
    }
    (layout.workspace / ".git" / "HEAD").write_text("ref: refs/heads/test-extension-fixture\n", encoding="utf-8")
    (layout.workspace / "te-context.txt").write_text(
        f"Deterministic governed context codeword: {codewords['context']}\n", encoding="utf-8"
    )
    (layout.workspace / "te-selection.txt").write_text("saved baseline without the in-memory selection\n", encoding="utf-8")
    (layout.workspace / "te-diff-target.txt").write_text("Test Extension diff fixture baseline\n", encoding="utf-8")
    (layout.workspace / "te-image.png").write_bytes(codeword_png(codewords["image"]))
    launcher = (
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(source)!r})\n"
        f"runpy.run_path({str(source / 'kaizen.py')!r}, run_name='__main__')\n"
    )
    (layout.workspace / "kaizen.py").write_text(launcher, encoding="utf-8")
    settings = {
        "telemetry.telemetryLevel": "off",
        "update.mode": "none",
        "extensions.autoCheckUpdates": False,
        "extensions.autoUpdate": False,
        "security.workspace.trust.enabled": False,
        "workbench.startupEditor": "none",
    }
    (layout.user_data / "User" / "settings.json").write_text(
        json.dumps(settings, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    manifest = {
        "v": 1,
        "run_id": layout.run_id,
        "suite_nonce": secrets.token_hex(32),
        "workspace_path": str(layout.workspace.resolve()),
        "created_at": _utc_now(),
        "limits": {
            "max_simultaneous_turns": MAX_SIMULTANEOUS_TURNS,
            "max_wall_seconds": MAX_WALL_SECONDS,
            "provider_retries": 0,
        },
        "codewords": codewords,
        "fixtures": {
            "context": "te-context.txt",
            "selection": "te-selection.txt",
            "image": "te-image.png",
            "diff_target": "te-diff-target.txt",
        },
    }
    layout.manifest.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return codewords


def source_fingerprint(source_root: Path) -> dict[str, str]:
    """Hash the bounded executable/source surface the isolated plane is allowed to read."""
    source = source_root.resolve()
    candidates = [
        source / "kaizen.py",
        source / "extension" / "package.json",
        source / "extension" / "media" / "kaizen.svg",
        source / "extension" / "media" / "webview" / "chat.html",
        source / "extension" / "media" / "webview" / "chat.css",
        source / "tests" / "run_test_extension.py",
    ]
    for root, patterns in (
        (source / "kaizen_components", ("*.py",)),
        (source / "extension" / "out", ("*.js",)),
    ):
        if root.is_dir():
            for pattern in patterns:
                candidates.extend(root.rglob(pattern))
    result: dict[str, str] = {}
    for candidate in sorted(set(candidates)):
        if not candidate.is_file():
            continue
        relative = candidate.resolve().relative_to(source).as_posix()
        result[relative] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return result


def provider_target_fingerprint(runtime_root: Path) -> str:
    """Independently validate and fingerprint one immutable Claude provider target."""
    validated = claude_runtime.validate_runtime(
        runtime_root.resolve(), source_root=claude_runtime.source_package_root(),
    )
    payload = {
        "pointer": validated["pointer"],
        "integrity": validated["integrity"],
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


class EvidenceWriter:
    def __init__(self, path: Path, run_id: str, clock: Callable[[], str] = _utc_now) -> None:
        self.path, self.run_id, self.clock, self.sequence = path, run_id, clock, 0

    def append(self, event: str, status: str, **safe: Any) -> dict[str, Any]:
        if not event or not all(ch.isalnum() or ch in "._-" for ch in event):
            raise ValueError("invalid test-extension evidence event")
        if status not in {"START", "READY", "PASS", "FAIL", "NOT_RUN", "STOP", "INFO"}:
            raise ValueError("invalid test-extension evidence status")
        forbidden = ("credential", "secret", "token", "email", "account", "organization", "prompt", "output")
        if any(any(word in key.casefold() for word in forbidden) for key in safe):
            raise ValueError("unsafe test-extension evidence field")
        self.sequence += 1
        body = {
            "v": 1,
            "seq": self.sequence,
            "at": self.clock(),
            "run_id": self.run_id,
            "event": event,
            "status": status,
            **safe,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n")
        return body


def _safe_evidence_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    """Translate accepted client proof names into the stricter durable-evidence vocabulary."""
    safe = dict(value)
    if "tool_output_complete" in safe:
        safe["tool_result_complete"] = safe.pop("tool_output_complete")
    return safe


def _durable_connection(database: Path):
    """Open the isolated authority plane through the same Turso driver used by K1."""
    return turso.connect(str(database))


def validate_suite_request(value: Any, capabilities: Mapping[str, Any]) -> SuiteRequest:
    exact_keys = {
        "v", "action", "suite_nonce", "request_sha256", "provider", "model", "effort",
        "max_turns", "call_ceiling", "scenarios", "provider_retries",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != exact_keys
        or type(value.get("v")) is not int
        or value.get("v") != 1
        or value.get("action") != "start"
    ):
        raise TestExtensionError("DENIED_TEST_EXTENSION_CONTROL", "invalid control request")
    suite_nonce, request_sha256 = value.get("suite_nonce"), value.get("request_sha256")
    if not _valid_sha256(suite_nonce):
        raise TestExtensionError("DENIED_TEST_EXTENSION_SUITE_NONCE", "suite nonce is invalid")
    if not _valid_sha256(request_sha256) or not secrets.compare_digest(
        request_sha256, _canonical_request_sha256(value),
    ):
        raise TestExtensionError("DENIED_TEST_EXTENSION_REQUEST_DIGEST", "suite request digest is invalid")
    if value.get("provider_retries") != 0 or isinstance(value.get("provider_retries"), bool):
        raise TestExtensionError("DENIED_TEST_EXTENSION_PROVIDER_RETRIES", "provider retries must be integer zero")
    provider_value = value.get("provider")
    if not isinstance(provider_value, str) or provider_value not in {"claude", "ollama"}:
        raise TestExtensionError("DENIED_TEST_EXTENSION_PROVIDER", "provider must be claude or ollama")
    provider = provider_value
    model_value = value.get("model")
    if not isinstance(model_value, str) or not model_value:
        raise TestExtensionError("DENIED_TEST_EXTENSION_MODEL", "model is not in the discovered catalog")
    model = model_value
    effort_value = value.get("effort")
    if effort_value is not None and (not isinstance(effort_value, str) or not effort_value):
        raise TestExtensionError("DENIED_TEST_EXTENSION_EFFORT", "effort is unsupported")
    effort = effort_value
    max_turns, ceiling, scenarios_value = value.get("max_turns"), value.get("call_ceiling"), value.get("scenarios")
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or not 1 <= max_turns <= 32:
        raise TestExtensionError("DENIED_TEST_EXTENSION_MAX_TURNS", "max_turns must be 1..32")
    if not isinstance(ceiling, int) or isinstance(ceiling, bool) or not 1 <= ceiling <= MAX_CALL_CEILING:
        raise TestExtensionError("DENIED_TEST_EXTENSION_CALL_CEILING", "call_ceiling must be 1..256")
    if not isinstance(scenarios_value, list) or not scenarios_value or len(scenarios_value) > len(ALL_SCENARIOS):
        raise TestExtensionError("DENIED_TEST_EXTENSION_SCENARIOS", "select known scenarios")
    if any(not isinstance(item, str) for item in scenarios_value):
        raise TestExtensionError("DENIED_TEST_EXTENSION_SCENARIOS", "scenario selection is invalid")
    scenarios = tuple(scenarios_value)
    if len(set(scenarios)) != len(scenarios) or any(item not in ALL_SCENARIOS for item in scenarios):
        raise TestExtensionError("DENIED_TEST_EXTENSION_SCENARIOS", "scenario selection is invalid")
    allowed = CLAUDE_SCENARIOS if provider == "claude" else OLLAMA_SCENARIOS
    if any(item not in allowed for item in scenarios):
        raise TestExtensionError("DENIED_TEST_EXTENSION_PROVIDER_SCENARIO", "scenario belongs to another provider")
    engine_id = "claude" if provider == "claude" else "local_llm"
    engines = capabilities.get("engines") if isinstance(capabilities, Mapping) else None
    engine = next(
        (entry for entry in engines or [] if isinstance(entry, Mapping) and entry.get("id") == engine_id), None
    )
    availability = engine.get("availability") if isinstance(engine, Mapping) and isinstance(
        engine.get("availability"), Mapping,
    ) else {}
    runtime = engine.get("runtime") if isinstance(engine, Mapping) and isinstance(
        engine.get("runtime"), Mapping,
    ) else {}
    if (
        engine is None
        or engine.get("drivable") is not True
        or availability.get("state") != "available"
        or (provider == "claude" and runtime.get("status") != "ready")
        or not (isinstance(engine.get("features"), Mapping) and engine["features"].get("test_extension") is True)
    ):
        raise TestExtensionError("DENIED_TEST_EXTENSION_PROVIDER_UNAVAILABLE", "provider test extension is unavailable")
    models = engine.get("models") if isinstance(engine.get("models"), list) else []
    selected = next((entry for entry in models if isinstance(entry, Mapping) and entry.get("id") == model), None)
    if selected is None:
        raise TestExtensionError("DENIED_TEST_EXTENSION_MODEL", "model is not in the discovered catalog")
    efforts = selected.get("reasoning_efforts") if isinstance(selected.get("reasoning_efforts"), list) else []
    if efforts and effort is None:
        raise TestExtensionError("DENIED_TEST_EXTENSION_EFFORT_REQUIRED", "select a discovered effort")
    if effort is not None and effort not in efforts:
        raise TestExtensionError("DENIED_TEST_EXTENSION_EFFORT", "effort is unsupported")
    request = SuiteRequest(
        provider, model, effort, max_turns, ceiling, scenarios,
        suite_nonce=suite_nonce, request_sha256=request_sha256,
    )
    if request.reserved_calls > request.call_ceiling:
        raise TestExtensionError(
            "DENIED_TEST_EXTENSION_CALL_BUDGET",
            f"{request.reserved_calls} reserved calls exceed ceiling {request.call_ceiling}",
        )
    return request


def validate_preapproval_stop(
    value: Any,
    *,
    suite_nonce: str,
    seen_stop_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Validate the one-shot plane control available before an action binding exists."""
    base_fields = {"v", "action", "suite_nonce", "stop_id", "reason"}
    if not isinstance(value, Mapping) or set(value) not in (base_fields, base_fields | {"request_sha256"}):
        raise TestExtensionError("DENIED_TEST_EXTENSION_STOP_SCHEMA", "pre-approval stop fields are not exact")
    if (
        type(value.get("v")) is not int
        or value.get("v") != CONTROL_VERSION
        or value.get("action") != "stop"
        or value.get("reason") != "user"
    ):
        raise TestExtensionError("DENIED_TEST_EXTENSION_STOP_SCHEMA", "pre-approval stop is invalid")
    supplied_nonce = value.get("suite_nonce")
    if not _valid_sha256(supplied_nonce) or not _valid_sha256(suite_nonce) or not secrets.compare_digest(
        supplied_nonce, suite_nonce,
    ):
        raise TestExtensionError("DENIED_TEST_EXTENSION_STOP_FORGED", "pre-approval stop is not bound to this plane")
    stop_id = value.get("stop_id")
    if not _valid_sha256(stop_id):
        raise TestExtensionError("DENIED_TEST_EXTENSION_STOP_ID", "pre-approval stop id is invalid")
    if stop_id in seen_stop_ids:
        raise TestExtensionError("DENIED_TEST_EXTENSION_STOP_REPLAY", "pre-approval stop was already accepted")
    if "request_sha256" in value and not _valid_sha256(value["request_sha256"]):
        raise TestExtensionError("DENIED_TEST_EXTENSION_REQUEST_DIGEST", "pending suite digest is invalid")
    return dict(value)


def external_scenario_block_code(value: Any) -> str | None:
    """Return one exact external provider block code; internal failures never qualify."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_]{1,128}", normalized):
        return None
    return normalized if normalized in EXTERNAL_SCENARIO_BLOCK_CODES else None


def _capability_external_block_code(provider: str, capabilities: Mapping[str, Any]) -> str | None:
    """Derive only a provider-external NOT_RUN reason from the current sanitized catalog."""
    if provider not in {"claude", "ollama"}:
        return None
    engine_id = "claude" if provider == "claude" else "local_llm"
    engines_value = capabilities.get("engines") if isinstance(capabilities, Mapping) else None
    engines = engines_value if isinstance(engines_value, list) else []
    engine = next(
        (entry for entry in engines or [] if isinstance(entry, Mapping) and entry.get("id") == engine_id),
        None,
    )
    if engine is None or not (
        isinstance(engine.get("features"), Mapping) and engine["features"].get("test_extension") is True
    ):
        return None
    availability = engine.get("availability") if isinstance(engine.get("availability"), Mapping) else {}
    runtime = engine.get("runtime") if isinstance(engine.get("runtime"), Mapping) else {}
    state, runtime_status = availability.get("state"), runtime.get("status")
    explicit = external_scenario_block_code(availability.get("code"))
    if state == "auth_required" or runtime_status == "auth_required":
        return explicit or "DENIED_AUTH_UNAVAILABLE"
    if runtime_status == "unavailable":
        return explicit or "DENIED_SDK_UNAVAILABLE"
    if provider == "claude" and runtime_status != "ready":
        return None
    models = engine.get("models") if isinstance(engine.get("models"), list) else []
    if state == "available" and engine.get("drivable") is True and not models:
        return explicit or "DENIED_MODEL_UNAVAILABLE"
    if state != "available" and engine.get("drivable") is True:
        return explicit
    return None


def validate_external_block(
    value: Any,
    *,
    suite_nonce: str,
    capabilities: Mapping[str, Any],
    seen_block_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Validate a one-shot, nonce-bound provider-external NOT_RUN control."""
    exact_fields = {"v", "action", "suite_nonce", "block_id", "provider", "code", "scenarios"}
    if (
        not isinstance(value, Mapping)
        or set(value) != exact_fields
        or type(value.get("v")) is not int
        or value.get("v") != CONTROL_VERSION
        or value.get("action") != "external_block"
    ):
        raise TestExtensionError(
            "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_SCHEMA", "external block fields are not exact",
        )
    supplied_nonce = value.get("suite_nonce")
    if not _valid_sha256(supplied_nonce) or not _valid_sha256(suite_nonce) or not secrets.compare_digest(
        supplied_nonce, suite_nonce,
    ):
        raise TestExtensionError(
            "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_FORGED", "external block is not bound to this plane",
        )
    block_id = value.get("block_id")
    if not _valid_sha256(block_id):
        raise TestExtensionError("DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_ID", "external block id is invalid")
    if block_id in seen_block_ids:
        raise TestExtensionError("DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_REPLAY", "external block was already accepted")
    provider = value.get("provider")
    if provider not in {"claude", "ollama"}:
        raise TestExtensionError(
            "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_PROVIDER", "external block provider is invalid",
        )
    code = external_scenario_block_code(value.get("code"))
    if code is None or value.get("code") != code:
        raise TestExtensionError("DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_CODE", "external block code is invalid")
    scenarios_value = value.get("scenarios")
    if not isinstance(scenarios_value, list) or not scenarios_value:
        raise TestExtensionError(
            "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_SCENARIOS", "external block scenarios are invalid",
        )
    scenarios = tuple(scenarios_value)
    allowed = CLAUDE_SCENARIOS if provider == "claude" else OLLAMA_SCENARIOS
    if (
        len(scenarios) > len(allowed)
        or any(not isinstance(scenario, str) or scenario not in allowed for scenario in scenarios)
        or len(set(scenarios)) != len(scenarios)
    ):
        raise TestExtensionError(
            "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_SCENARIOS", "external block scenarios are invalid",
        )
    justified = _capability_external_block_code(provider, capabilities)
    if justified is None or not secrets.compare_digest(code, justified):
        raise TestExtensionError(
            "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_UNJUSTIFIED",
            "external block is not justified by the current provider capability",
        )
    return dict(value)


def sanitize_capabilities(payload: Any) -> dict[str, Any]:
    def bounded_text(value: Any, maximum: int) -> str | None:
        if not isinstance(value, str) or len(value) > maximum or not value.strip():
            return None
        return value.strip()

    output: list[dict[str, Any]] = []
    engines = payload.get("engines") if isinstance(payload, Mapping) else None
    for raw in engines if isinstance(engines, list) else []:
        if not isinstance(raw, Mapping):
            continue
        engine_id = bounded_text(raw.get("id"), 128)
        label = bounded_text(raw.get("label"), 256)
        if engine_id not in {"claude", "local_llm"} or label is None or not isinstance(raw.get("drivable"), bool):
            continue
        features = raw.get("features") if isinstance(raw.get("features"), Mapping) else {}
        models: list[dict[str, Any]] = []
        for model in raw.get("models") if isinstance(raw.get("models"), list) else []:
            if not isinstance(model, Mapping):
                continue
            model_id = bounded_text(model.get("id"), 256)
            model_label = bounded_text(model.get("label"), 256)
            if model_id is None or model_label is None:
                continue
            efforts: list[str] = []
            for item in model.get("reasoning_efforts") if isinstance(model.get("reasoning_efforts"), list) else []:
                effort = bounded_text(item, 64)
                if effort is not None and effort not in efforts:
                    efforts.append(effort)
            models.append(
                {
                    "id": model_id,
                    "label": model_label,
                    "reasoning_efforts": efforts,
                }
            )
        availability = raw.get("availability") if isinstance(raw.get("availability"), Mapping) else {}
        runtime = raw.get("runtime") if isinstance(raw.get("runtime"), Mapping) else {}
        state = bounded_text(availability.get("state"), 64) or "unavailable"
        code = bounded_text(availability.get("code"), 128)
        runtime_kind = bounded_text(runtime.get("kind"), 128)
        runtime_status = bounded_text(runtime.get("status"), 32)
        runtime_valid = runtime_kind is not None and runtime_status in {"ready", "auth_required", "unavailable"}
        if runtime_valid and state == "auth_required":
            runtime_status = "auth_required"
        output.append(
            {
                "id": engine_id,
                "label": label,
                "drivable": raw.get("drivable"),
                "availability": {
                    "state": state,
                    **({"code": code} if code is not None else {}),
                },
                "models": models,
                "features": {"test_extension": features.get("test_extension") is True},
                **({"runtime": {"status": runtime_status}} if runtime_valid else {}),
            }
        )
    return {"engines": output}


def daemon_readiness_failures(
    status: Mapping[str, Any], capabilities: Mapping[str, Any], child: children.OwnedChild,
    expected_workspace: Path,
) -> tuple[str, ...]:
    """Return bounded gate names only; ownership is the retained tree, not a launcher PID."""
    failed: list[str] = []
    pid = status.get("pid")
    if status.get("running") is not True:
        failed.append("running")
    if type(pid) is not int or pid <= 0:
        failed.append("reported_pid")
    elif not child.owns_pid(pid):
        failed.append("owned_process")
    nonce = status.get("nonce")
    if (
        not isinstance(nonce, str)
        or len(nonce) != 32
        or any(character not in "0123456789abcdef" for character in nonce)
    ):
        failed.append("nonce")
    status_root = status.get("repo_root")
    expected_root = os.path.normcase(os.path.normpath(str(expected_workspace.absolute())))
    if (
        not isinstance(status_root, str)
        or not Path(status_root).is_absolute()
        or os.path.normcase(os.path.normpath(status_root)) != expected_root
    ):
        failed.append("workspace")
    status_engines = status.get("engines")
    if (
        not isinstance(status_engines, list)
        or any(not isinstance(engine, str) for engine in status_engines)
        or not {"claude_cli", "local_llm"}.issubset(status_engines)
    ):
        failed.append("registered_engines")
    capability_engines = capabilities.get("engines")
    capability_ids = {
        engine.get("id") for engine in capability_engines
        if isinstance(engine, Mapping)
    } if isinstance(capability_engines, list) else set()
    if capability_ids != {"claude", "local_llm"}:
        failed.append("capability_engines")
    return tuple(failed)


def _sanitize_client_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key in _CLIENT_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if key in _CLIENT_STRING_FIELDS:
            if not isinstance(item, str) or not 1 <= len(item) <= 256 or any(ord(ch) < 32 for ch in item):
                raise TestExtensionError("DENIED_TEST_EXTENSION_CLIENT_EVIDENCE", f"{key} is invalid")
        elif key in _CLIENT_INTEGER_FIELDS:
            minimum, maximum = (-2_147_483_648, 2_147_483_647) if key == "process_exit_code" else (0, 1_000_000)
            if not isinstance(item, int) or isinstance(item, bool) or not minimum <= item <= maximum:
                raise TestExtensionError("DENIED_TEST_EXTENSION_CLIENT_EVIDENCE", f"{key} is invalid")
        elif not isinstance(item, bool):
            raise TestExtensionError("DENIED_TEST_EXTENSION_CLIENT_EVIDENCE", f"{key} is invalid")
        safe[key] = item
    return safe


def _require_pass_fields(safe: Mapping[str, Any], **expected: Any) -> None:
    if any(key not in safe or safe[key] != value for key, value in expected.items()):
        raise TestExtensionError(
            "DENIED_TEST_EXTENSION_PASS_PROOF", "scenario PASS evidence is missing or contradicts required proof"
        )


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _bounded_identifier(value: Any, *, maximum: int = 256) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and all(32 < ord(character) < 127 for character in value)
    )


def _validate_action_request(
    value: Any,
    *,
    suite_nonce: str,
    request_sha256: str,
    selected: Sequence[str],
    expected_sequence: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_SCHEMA", "action request must be an object")
    phase = value.get("phase")
    definition = _ACTION_PHASES.get(phase) if isinstance(phase, str) else None
    if definition is None:
        raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_PHASE", "action phase is unsupported")
    scenario, exact_fields = definition
    if set(value) != set(exact_fields):
        raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_SCHEMA", "action request fields are not exact")
    if value.get("protocol") != ACTION_PROTOCOL:
        raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_PROTOCOL", "action protocol is invalid")
    if not isinstance(value.get("sequence"), int) or isinstance(value.get("sequence"), bool):
        raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_SEQUENCE", "action sequence is invalid")
    if value["sequence"] != expected_sequence or not 1 <= expected_sequence <= ACTION_LIMIT:
        raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_SEQUENCE", "action sequence is out of order")
    action_id = value.get("action_id")
    if not isinstance(action_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,96}", action_id) is None:
        raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_ID", "action id is invalid")
    if value.get("scenario_id") != scenario or scenario not in selected:
        if scenario != "suite" or value.get("scenario_id") != "suite":
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_SCENARIO", "action scenario is not selected")
    if not isinstance(value.get("suite_nonce"), str) or not secrets.compare_digest(value["suite_nonce"], suite_nonce):
        raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_FORGED", "action suite nonce does not match")
    if not isinstance(value.get("request_sha256"), str) or not secrets.compare_digest(
        value["request_sha256"], request_sha256,
    ):
        raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_FORGED", "action suite digest does not match")
    for key in (
        "conversation_id", "session_id", "agent_run_id", "correlation_id", "previous_agent_run_id",
        "loser_conversation_id",
    ):
        if key in value and not _bounded_identifier(value[key]):
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_BINDING", f"{key} is invalid")
    if "loser_request_token" in value and not _valid_sha256(value["loser_request_token"]):
        raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_BINDING", "loser request token is invalid")
    if "denial_code" in value:
        expected_denial = (
            "DENIED_CONTEXT_INVALID"
            if phase == "traversal.verify_zero_records"
            else "DENIED_WORKSPACE_WRITER_BUSY"
        )
        if value["denial_code"] != expected_denial:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_BINDING", "action denial code is invalid")
    if phase == "suite.stop" and value.get("reason") != "user":
        raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_BINDING", "stop reason is invalid")
    if _ACTION_APPROVAL_FIELDS.issubset(value):
        if not isinstance(value["revision"], int) or isinstance(value["revision"], bool) or not 1 <= value["revision"] <= 1_000_000:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_REVISION", "approval revision is invalid")
        for key in ("snapshot_set_sha256", "before_sha256", "proposed_sha256"):
            if not _valid_sha256(value[key]):
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_DIGEST", f"{key} is invalid")
        workspace_path = value["workspace_path"]
        if not isinstance(workspace_path, str) or not 1 <= len(workspace_path) <= 256:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_PATH", "workspace path is invalid")
        candidate = Path(workspace_path)
        if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() != workspace_path:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_PATH", "workspace path is not canonical relative")
    return dict(value)


def _process_request_sha256(python: Path, argv: Sequence[str], cwd: str, timeout_ms: int) -> str:
    canonical = "\n".join((str(python.resolve()), *argv, cwd, str(timeout_ms))).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_pass_proof(scenario: str, safe: Mapping[str, Any], request: SuiteRequest, python: Path) -> None:
    _require_pass_fields(safe, calls_reserved=request.max_turns * (2 if scenario == "claude-interrupt-restart" else 1))
    if scenario == "claude-traversal-zero-record":
        _require_pass_fields(safe, denial_code="DENIED_CONTEXT_INVALID", zero_records=True)
        return

    _require_pass_fields(safe, profile_attested=True, attested_model=request.model)
    if request.effort is None:
        if "attested_effort" in safe:
            raise TestExtensionError("DENIED_TEST_EXTENSION_PASS_PROOF", "unexpected effort attestation")
    else:
        _require_pass_fields(safe, attested_effort=request.effort)

    if scenario in {"claude-text-stream", "ollama-text-stream"}:
        _require_pass_fields(safe, codeword_seen=True, ordered=True, stream_suppressed=False)
        if safe.get("delta_count", 0) < 1 or safe.get("durable_replacements", 0) < 1:
            raise TestExtensionError("DENIED_TEST_EXTENSION_PASS_PROOF", "stream/durable proof is incomplete")
    elif scenario == "claude-image-codeword":
        _require_pass_fields(safe, codeword_seen=True, image_refs=1)
    elif scenario in {"claude-governed-context", "ollama-governed-context"}:
        _require_pass_fields(safe, codeword_seen=True, context_refs=2, selection_exact=True)
    elif scenario in {"claude-diff-accept", "claude-diff-reject"}:
        decision = "approve" if scenario.endswith("accept") else "deny"
        approval_status = "approved" if decision == "approve" else "denied"
        _require_pass_fields(
            safe, approval_ui=True, decision=decision, approval_status=approval_status, diff_card_count=1,
            diff_path_matches=True, diff_before_matches=True, diff_proposed_hashed=True, diff_final_matches=True,
        )
    elif scenario == "claude-process-approval":
        _require_pass_fields(
            safe, approval_ui=True, decision="approve", approval_status="approved", tool_card_count=1,
            tool_status="ok", tool_request_matches=True, tool_result_matches=True, process_exit_code=0,
            tool_decision_approved=True, tool_output_complete=True,
            process_stdout_bytes=len(b"TE_PROCESS_OK\n"),
            process_request_sha256=_process_request_sha256(
                python, ("-c", "print('TE_PROCESS_OK')"), ".", 5000,
            ),
            process_stdout_sha256=hashlib.sha256(b"TE_PROCESS_OK\n").hexdigest(),
        )
    elif scenario == "claude-plan-controls":
        _require_pass_fields(
            safe, tool_card_count=2, tool_status="blocked", tool_request_matches=True,
            tool_zero_execution=True, tool_zero_stdout=True, process_stdout_bytes=0,
            denial_code="MODE_CEILING:exec",
            invariant_denial_code="INV_GIT_PUSH",
            process_request_sha256=_process_request_sha256(
                python, ("-c", "print('MUST_NOT_RUN')"), ".", 5000,
            ),
        )
        if "process_exit_code" in safe or "process_stdout_sha256" in safe:
            raise TestExtensionError("DENIED_TEST_EXTENSION_PASS_PROOF", "blocked process reported execution output")
    elif scenario == "ollama-tool-policy":
        _require_pass_fields(
            safe, tool_card_count=1, tool_status="blocked", tool_request_matches=True,
            tool_zero_execution=True, tool_zero_stdout=True, process_stdout_bytes=0,
            denial_code="MODE_CEILING:exec",
            process_request_sha256=_process_request_sha256(
                python, ("-c", "print('MUST_NOT_RUN')"), ".", 5000,
            ),
        )
        if "invariant_denial_code" in safe or "process_exit_code" in safe or "process_stdout_sha256" in safe:
            raise TestExtensionError("DENIED_TEST_EXTENSION_PASS_PROOF", "Ollama Plan denial reported foreign proof")
    elif scenario == "claude-diff-timeout":
        _require_pass_fields(
            safe, approval_ui=True, approval_status="timed_out", diff_card_count=1,
            diff_path_matches=True, diff_before_matches=True, diff_proposed_hashed=True, diff_final_matches=True,
        )
    elif scenario == "claude-diff-stale":
        _require_pass_fields(
            safe, approval_ui=True, decision="approve", approval_status="approved", diff_card_count=1,
            diff_revision_refreshed=True, diff_second_accept=True, diff_path="te-diff-target.txt",
            diff_final_matches=True,
        )
        if safe.get("diff_refreshed_revision") != safe.get("diff_revision", 0) + 1:
            raise TestExtensionError("DENIED_TEST_EXTENSION_PASS_PROOF", "stale revision did not increment once")
        for key in (
            "diff_snapshot_set_sha256", "diff_refreshed_snapshot_set_sha256", "diff_before_sha256",
            "diff_mutated_base_sha256", "diff_refreshed_before_sha256", "diff_proposed_sha256", "diff_final_sha256",
        ):
            if not _valid_sha256(safe.get(key)):
                raise TestExtensionError("DENIED_TEST_EXTENSION_PASS_PROOF", "stale diff hash proof is incomplete")
        if safe["diff_mutated_base_sha256"] != safe["diff_refreshed_before_sha256"] or safe["diff_final_sha256"] != safe["diff_proposed_sha256"]:
            raise TestExtensionError("DENIED_TEST_EXTENSION_PASS_PROOF", "stale diff hashes do not reconcile")
    elif scenario == "claude-diff-corrupt":
        _require_pass_fields(
            safe, approval_ui=True, decision="approve", approval_status="denied", diff_card_count=1,
            diff_corrupt=True, diff_authoritative_denial=True, diff_path="te-diff-target.txt",
            denial_code="DENIED_APPROVAL_SNAPSHOT_INVALID", diff_final_matches=True,
        )
        for key in (
            "diff_snapshot_set_sha256", "diff_before_sha256", "diff_proposed_sha256",
            "diff_final_sha256", "diff_corrupted_artifact_sha256",
        ):
            if not _valid_sha256(safe.get(key)):
                raise TestExtensionError("DENIED_TEST_EXTENSION_PASS_PROOF", "corrupt diff hash proof is incomplete")
        if safe["diff_final_sha256"] != safe["diff_before_sha256"]:
            raise TestExtensionError("DENIED_TEST_EXTENSION_PASS_PROOF", "corrupt diff changed its target")
    elif scenario == "claude-writer-conflict":
        _require_pass_fields(
            safe, approval_ui=True, decision="deny", approval_status="denied",
            denial_code="DENIED_WORKSPACE_WRITER_BUSY", zero_records=True, writer_conflict_seen=True,
            writer_loser_zero_records=True, writer_lease_retained=True, writer_lease_released=True,
        )
    elif scenario == "claude-interrupt-restart":
        _require_pass_fields(
            safe, interrupted=True, restart_resumable=True, restart_same_session=True,
            restart_new_run=True, restart_reduced_fidelity=True, queue_empty=True,
            runtime_children_clean=True, conversation_idle=True,
        )
    elif scenario == "cleanup-leak-state":
        _require_pass_fields(safe, conversation_idle=True, pending_approvals=0, running_cards=0)
    else:
        raise TestExtensionError("DENIED_TEST_EXTENSION_PASS_PROOF", "scenario PASS proof contract is undefined")


def _parse_json_output(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    raw = result.stdout.strip() or result.stderr.strip()
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, AttributeError) as error:
        raise TestExtensionError("DENIED_TEST_EXTENSION_CHILD_RESPONSE", "child returned invalid JSON") from error
    if not isinstance(value, dict):
        raise TestExtensionError("DENIED_TEST_EXTENSION_CHILD_RESPONSE", "child response is invalid")
    return value


class OuterRunner:
    def __init__(
        self,
        *,
        source_root: Path,
        layout: RunLayout,
        python: Path,
        code_path: Path,
        wall_seconds: int = MAX_WALL_SECONDS,
        spawner: Callable[..., children.OwnedChild] = children.spawn_owned,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.source_root = require_non_system_drive(source_root, label="source root")
        self.layout, self.python, self.code_path = layout, python.resolve(), code_path.resolve()
        if not 1 <= wall_seconds <= MAX_WALL_SECONDS:
            raise TestExtensionError("DENIED_TEST_EXTENSION_WALL_CLOCK", "wall clock must be 1..1800 seconds")
        self.wall_seconds, self.spawner, self.command_runner = wall_seconds, spawner, command_runner
        self.monotonic, self.sleep = monotonic, sleep
        self.evidence = EvidenceWriter(layout.evidence, layout.run_id)
        self.children: list[tuple[str, children.OwnedChild]] = []
        self.capabilities: dict[str, Any] = {"engines": []}
        self._client_offset = 0
        self._approved_request: SuiteRequest | None = None
        self._scenario_results: dict[str, str] = {}
        self._suite_counts: dict[str, int] | None = None
        self._source_fingerprint: dict[str, str] | None = None
        self._provider_target_fingerprint: str | None = None
        self._provider_runtime_root: Path | None = None
        self._provider_disk_fingerprint: str | None = None
        self._provider_final_verified = False
        self._provider_final_ok: bool | None = None
        self._provider_post_cleanup_verified = False
        self._provider_post_cleanup_ok: bool | None = None
        self._next_action_sequence = 1
        self._handled_actions: dict[str, tuple[int, str, Path]] = {}
        self._scenario_phase_index: dict[str, int] = {}
        self._scenario_bindings: dict[str, dict[str, Any]] = {}
        self._action_proof: dict[str, Any] = {}
        self._suite_record_baseline: dict[str, frozenset[str]] | None = None
        self._scenario_bound_runs: dict[str, tuple[str, ...]] = {}
        self._bound_run_owner: dict[str, str] = {}
        self._bound_session_owner: dict[str, str] = {}
        self._bound_correlations: dict[tuple[str, str], str] = {}
        self._bound_approval_ids: dict[str, str] = {}
        self._provider_calls_by_run: dict[str, int] = {}
        self._stop_requested = False
        self._preapproval_stop_ids: set[str] = set()
        self._launcher_stop_consumed = False
        self._external_block_ids: set[str] = set()
        self._run_deadline: float | None = None

    def _phase_deadline(self, seconds: float) -> float:
        deadline = self.monotonic() + seconds
        return min(deadline, self._run_deadline) if self._run_deadline is not None else deadline

    def _bounded_active_timeout(self, seconds: float) -> float:
        if self._run_deadline is None:
            return seconds
        remaining = self._run_deadline - self.monotonic()
        if remaining <= 0:
            raise TestExtensionError(WALL_CLOCK_CODE, "test-extension outer wall clock is exhausted")
        return min(seconds, remaining)

    def _active_sleep(self, seconds: float) -> None:
        self.sleep(self._bounded_active_timeout(seconds))

    def _wall_exhausted(self) -> bool:
        return self._run_deadline is not None and self.monotonic() >= self._run_deadline

    def _durable_counts(self) -> dict[str, int]:
        database = self.layout.workspace / "AI" / "db" / "kaizen.db"
        with _durable_connection(database) as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("agent_sessions", "user_instructions", "agent_runs", "agent_events", "approval_requests")
            }

    def _durable_id_snapshot(self) -> dict[str, frozenset[str]]:
        database = self.layout.workspace / "AI" / "db" / "kaizen.db"
        tables = ("agent_sessions", "user_instructions", "agent_runs", "agent_events", "approval_requests")
        try:
            with _durable_connection(database) as connection:
                return {
                    table: frozenset(str(row[0]) for row in connection.execute(f"SELECT id FROM {table}"))
                    for table in tables
                }
        except turso.Error as error:
            raise self._durable_denial("durable row identity snapshot is unreadable") from error

    def _arm_traversal_zero_records(self) -> dict[str, Any]:
        if self._suite_record_baseline is None:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_BEFORE_START", "traversal attestation preceded approval")
        self._action_proof["traversal"] = {"before_ids": self._durable_id_snapshot()}
        return {"zero_record_baseline_armed": True}

    def _attest_traversal_zero_records(self, denial_code: str) -> dict[str, Any]:
        proof = self._action_proof.get("traversal")
        baseline = proof.get("before_ids") if isinstance(proof, Mapping) else None
        if not isinstance(baseline, Mapping):
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_BEFORE_START", "traversal baseline was not armed")
        after = self._durable_id_snapshot()
        zero_records = after == baseline
        passed = denial_code == "DENIED_CONTEXT_INVALID" and zero_records
        proof.update({"after_ids": after, "code": denial_code})
        self.evidence.append(
            "traversal.zero_record", "PASS" if passed else "FAIL",
            code=denial_code, zero_records=zero_records, child_controller_driven=True,
        )
        if not passed:
            raise TestExtensionError(
                "DENIED_TEST_EXTENSION_TRAVERSAL_PROOF",
                "child-driven traversal denial changed durable row identities",
            )
        return {"denial_code": denial_code, "zero_records": True}

    @staticmethod
    def _write_atomic_json(target: Path, body: Mapping[str, Any], *, replace: bool = False) -> None:
        """Exclusive-create (`xb`) + flush + fsync + atomic `os.replace`; `replace=False` fails closed on an existing target (DENIED_..._ACTION_REPLAY); temp always unlinked in `finally`."""
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = _canonical_json(body) + b"\n"
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if not replace and target.exists():
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_REPLAY", "action artifact already exists")
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _manifest_payload(self) -> dict[str, Any]:
        """Reads/validates the isolated suite manifest: ≤64KiB, dict, `suite_nonce` is sha256, `workspace_path` absolute and `== resolved layout workspace` under root; raises DENIED_TEST_EXTENSION_MANIFEST otherwise."""
        try:
            raw = self.layout.manifest.read_bytes()
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TestExtensionError("DENIED_TEST_EXTENSION_MANIFEST", "test-extension manifest is unreadable") from error
        if len(raw) > 64 * 1024 or not isinstance(value, dict) or not _valid_sha256(value.get("suite_nonce")):
            raise TestExtensionError("DENIED_TEST_EXTENSION_MANIFEST", "test-extension manifest is invalid")
        workspace_path = value.get("workspace_path")
        expected_workspace = self.layout.workspace.resolve()
        if (
            not isinstance(workspace_path, str)
            or not Path(workspace_path).is_absolute()
            or workspace_path != str(expected_workspace)
            or Path(workspace_path).resolve() != expected_workspace
            or not _under(self.layout.root, expected_workspace)
        ):
            raise TestExtensionError(
                "DENIED_TEST_EXTENSION_MANIFEST", "test-extension manifest workspace is invalid"
            )
        return value

    def _write_suite_binding(self) -> None:
        request = self._approved_request
        if request is None:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_BEFORE_START", "suite binding preceded approval")
        body = {
            "protocol": ACTION_PROTOCOL,
            "suite_nonce": request.suite_nonce,
            "request_sha256": request.request_sha256,
        }
        if self.layout.action_suite.exists():
            try:
                existing = json.loads(self.layout.action_suite.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_FORGED", "suite binding is unreadable") from error
            if existing != body:
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_FORGED", "suite binding changed")
            return
        self._write_atomic_json(self.layout.action_suite, body)

    @staticmethod
    def _is_reparse(info: os.stat_result) -> bool:
        attributes = int(getattr(info, "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        return stat.S_ISLNK(info.st_mode) or bool(reparse_flag and attributes & reparse_flag)

    def _plain_action_file(self, target: Path) -> bytes:
        try:
            info = target.lstat()
        except OSError as error:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_FILE", "action request is unavailable") from error
        if (
            not stat.S_ISREG(info.st_mode)
            or self._is_reparse(info)
            or target.resolve().parent != self.layout.action_requests.resolve()
        ):
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_REPARSE", "action request is not a plain file")
        if info.st_size > ACTION_REQUEST_MAX_BYTES:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_OVERSIZE", "action request is oversized")
        return target.read_bytes()

    def _run_session_id(self, agent_run_id: str) -> str | None:
        database = self.layout.workspace / "AI" / "db" / "kaizen.db"
        try:
            with _durable_connection(database) as connection:
                row = connection.execute("SELECT session_id FROM agent_runs WHERE id = ?", (agent_run_id,)).fetchone()
        except turso.Error as error:
            raise self._durable_denial("agent run/session binding ledger is unreadable") from error
        return row[0] if row and isinstance(row[0], str) else None

    def _active_run_ids(self) -> set[str]:
        database = self.layout.workspace / "AI" / "db" / "kaizen.db"
        try:
            with _durable_connection(database) as connection:
                run_ids = [str(row[0]) for row in connection.execute("SELECT id FROM agent_runs").fetchall()]
                return {run_id for run_id in run_ids if not agent_runs.reduce_run_conn(connection, run_id)["terminal"]}
        except turso.Error as error:
            raise self._durable_denial("agent run ledger is unreadable") from error

    def _approval_open(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        if self._run_session_id(str(binding["agent_run_id"])) != binding["session_id"]:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_CROSS_RUN", "approval run/session binding is invalid")
        matches: list[dict[str, Any]] = []
        for event in self._approval_events():
            if (
                event["marker"] != "open"
                or event["agent_run_id"] != binding["agent_run_id"]
                or event["correlation_id"] != binding["correlation_id"]
                or not isinstance(event["body"], Mapping)
                or event["body"].get("approval_revision") != binding["revision"]
                or event["body"].get("snapshot_set_sha256") != binding["snapshot_set_sha256"]
            ):
                continue
            change = self._diff_change(event, str(binding["workspace_path"]))
            if (
                self._side_sha(change, "before") == binding["before_sha256"]
                and self._side_sha(change, "proposed") == binding["proposed_sha256"]
            ):
                matches.append(event)
        if len(matches) != 1:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_APPROVAL", "approval binding is not unique")
        return matches[0]

    def _writer_marker(self) -> dict[str, Any] | None:
        target = self.layout.workspace / "AI" / "work" / "orchestration" / "runtime" / "owned_runs.json"
        try:
            return OwnershipRegistry(
                target, workspace_root=self.layout.workspace,
            ).writer_marker()
        except (OSError, OwnershipStateError) as error:
            raise TestExtensionError("DENIED_TEST_EXTENSION_WRITER_PROOF", "writer registry is unreadable") from error

    def _require_exact_writer_holder(self, binding: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        marker, status = self._writer_marker(), self._daemon_status()
        if (
            marker is None
            or marker.get("session_id") != binding["session_id"]
            or marker.get("agent_run_id") != binding["agent_run_id"]
            or status.get("writer_claim_active") is not True
        ):
            raise TestExtensionError("DENIED_TEST_EXTENSION_WRITER_PROOF", "exact writer holder is not retained")
        return marker, status

    def _fixed_fixture_path(self, supplied: Any) -> Path:
        fixtures = self._manifest_payload().get("fixtures")
        expected = fixtures.get("diff_target") if isinstance(fixtures, Mapping) else None
        if not isinstance(supplied, str) or supplied != expected:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_PATH", "test action path is not the diff fixture")
        target = self.layout.workspace / supplied
        try:
            info = target.lstat()
        except OSError as error:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_PATH", "test action target is unavailable") from error
        if (
            not stat.S_ISREG(info.st_mode)
            or self._is_reparse(info)
            or target.resolve() != (self.layout.workspace / expected).resolve()
        ):
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_PATH", "test action target is not a plain fixture")
        if info.st_size > 8 * 1024 * 1024:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_OVERSIZE", "test action target is oversized")
        return target

    @staticmethod
    def _sha256_path(target: Path) -> str:
        return hashlib.sha256(target.read_bytes()).hexdigest()

    def _daemon_status(self) -> dict[str, Any]:
        return self._run_kaizen("daemon", "status", timeout=5.0)

    def _direct_writer_conflict_probe(self) -> dict[str, Any]:
        approved = self._approved_request
        if approved is None:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_BEFORE_START", "writer probe preceded approval")
        runtime_dir = self.layout.workspace / "AI" / "work" / "orchestration" / "runtime"
        token = (runtime_dir / "control.token").read_text(encoding="utf-8").strip()
        engine = "claude" if approved.provider == "claude" else "local_llm"
        profile: dict[str, Any] = {
            "model": approved.model,
            "permission_mode": "ask",
            "auth_mode": "subscription" if approved.provider == "claude" else "none",
        }
        if approved.effort is not None:
            profile["reasoning_effort"] = approved.effort
        return loopback.send_request(
            self.layout.workspace,
            runtime_dir,
            {
                "op": "session/start",
                "token": token,
                "args": {
                    "engine": engine,
                    "prompt": "Test Extension direct writer-conflict pre-provider probe",
                    "model": approved.model,
                    "max_turns": approved.max_turns,
                    "profile": profile,
                },
            },
            timeout=5.0,
        )

    def _restart_isolated_daemon(self) -> dict[str, Any]:
        match = next(((index, child) for index, (name, child) in enumerate(self.children) if name == "daemon"), None)
        if match is None:
            raise TestExtensionError("DENIED_TEST_EXTENSION_DAEMON_MISSING", "isolated daemon child is missing")
        index, child = match
        old_status = self._daemon_status()
        old_pid, old_nonce = old_status.get("pid"), old_status.get("nonce")
        if not isinstance(old_pid, int) or not _bounded_identifier(old_nonce):
            raise TestExtensionError("DENIED_TEST_EXTENSION_DAEMON_RESTART_UNPROVEN", "old daemon identity is invalid")
        try:
            child.kill_tree(timeout=5.0)
        except children.ChildTerminationError as error:
            raise TestExtensionError(
                "DENIED_TEST_EXTENSION_DAEMON_RESTART_UNPROVEN", "old isolated daemon did not terminate"
            ) from error
        if child.poll() is None:
            raise TestExtensionError("DENIED_TEST_EXTENSION_DAEMON_RESTART_UNPROVEN", "old daemon is still running")
        self.children.pop(index)
        self.evidence.append("action.daemon_old", "PASS", termination_proven=True)
        self.start_daemon()
        new_status = self._daemon_status()
        new_pid, new_nonce = new_status.get("pid"), new_status.get("nonce")
        if (
            not isinstance(new_pid, int)
            or not _bounded_identifier(new_nonce)
            or new_pid == old_pid
            or new_nonce == old_nonce
        ):
            raise TestExtensionError("DENIED_TEST_EXTENSION_DAEMON_RESTART_UNPROVEN", "new daemon identity is not fresh")
        return {
            "old_pid": old_pid,
            "old_nonce": old_nonce,
            "old_termination_proven": True,
            "new_pid": new_pid,
            "new_nonce": new_nonce,
            "new_boot_proven": True,
        }

    @staticmethod
    def _approval_binding(request: Mapping[str, Any]) -> dict[str, Any]:
        return {key: request[key] for key in _ACTION_APPROVAL_FIELDS}

    def _require_phase_order(self, request: Mapping[str, Any]) -> None:
        scenario, phase = str(request["scenario_id"]), str(request["phase"])
        if phase == "suite.stop":
            if self._stop_requested:
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_PHASE", "stop phase was already accepted")
            return
        order = _SCENARIO_PHASE_ORDER[scenario]
        index = self._scenario_phase_index.get(scenario, 0)
        if index >= len(order) or phase != order[index]:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_PHASE", "action phase is repeated or out of order")
        if scenario == "claude-writer-conflict":
            binding = self._approval_binding(request)
            prior = self._scenario_bindings.get(scenario)
            if prior is None:
                self._scenario_bindings[scenario] = binding
            elif binding != prior:
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_CROSS_REVISION", "writer approval binding changed")
            if phase == "writer.loser_verify":
                armed = self._action_proof.get("writer", {}).get("loser")
                if not isinstance(armed, Mapping) or any(
                    request.get(key) != armed.get(key) for key in ("loser_conversation_id", "loser_request_token")
                ):
                    raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_REPLAY", "loser request binding changed")
        elif scenario == "claude-interrupt-restart" and phase == "restart.cleanup":
            prior = self._scenario_bindings.get(scenario)
            if (
                not isinstance(prior, Mapping)
                or request["conversation_id"] != prior.get("conversation_id")
                or request["session_id"] != prior.get("session_id")
                or request["previous_agent_run_id"] != prior.get("agent_run_id")
                or request["agent_run_id"] == prior.get("agent_run_id")
            ):
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_CROSS_RUN", "restart run binding changed")
        elif scenario == "claude-interrupt-restart":
            self._scenario_bindings[scenario] = {
                key: request[key] for key in ("conversation_id", "session_id", "agent_run_id")
            }

    def _execute_action(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatches a validated phase-ordered action to its effect; returns the proof dict persisted into the receipt; raises TestExtensionError for any unmet invariant; unsupported phase → DENIED_..._ACTION_PHASE."""
        phase = str(request["phase"])
        if phase == "suite.stop":
            self._stop_requested = True
            return {"stop_accepted": True}
        if phase == "traversal.arm_zero_records":
            return self._arm_traversal_zero_records()
        if phase == "traversal.verify_zero_records":
            return self._attest_traversal_zero_records(str(request["denial_code"]))
        if phase == "stale.mutate_base":
            self._approval_open(request)
            target = self._fixed_fixture_path(request.get("workspace_path"))
            before = self._sha256_path(target)
            if request["before_sha256"] != before:
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_STALE", "diff fixture changed before mutation")
            content = target.read_bytes()
            target.write_bytes(b"Test Extension controlled stale-base mutation\n" + content)
            after = self._sha256_path(target)
            self._action_proof["stale"] = {
                "path": request["workspace_path"], "before": before, "mutated": after,
                "binding": self._approval_binding(request),
            }
            return {"mutated_sha256": after}
        if phase == "corrupt.snapshot":
            self._approval_open(request)
            digest = str(request["proposed_sha256"])
            target = self.layout.workspace / "AI" / "work" / "orchestration" / "ui-cache" / "diffs" / "sha256" / digest
            try:
                info = target.lstat()
            except OSError as error:
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_SNAPSHOT", "snapshot artifact is unavailable") from error
            if (
                not stat.S_ISREG(info.st_mode)
                or self._is_reparse(info)
                or info.st_size > 8 * 1024 * 1024
                or target.resolve().parent != target.parent.resolve()
                or self._sha256_path(target) != digest
            ):
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_SNAPSHOT", "snapshot artifact is not the declared plain object")
            target.write_bytes(b"TEST_EXTENSION_CONTROLLED_CORRUPTION\n")
            corrupted = self._sha256_path(target)
            self._action_proof["corrupt"] = {
                "digest": digest, "corrupted": corrupted, "binding": self._approval_binding(request),
            }
            return {"corrupted_sha256": corrupted}
        if phase == "writer.holder_open":
            self._approval_open(request)
            marker, status = self._require_exact_writer_holder(request)
            isolated = (
                self._active_run_ids() == {str(request["agent_run_id"])}
                and status.get("active_durable_runs") == 1
                and status.get("driven_sessions") == 1
                and status.get("pending_approvals") == 1
            )
            if not isolated:
                raise TestExtensionError("DENIED_TEST_EXTENSION_WRITER_PROOF", "writer holder is not isolated and parked")
            self._action_proof["writer"] = {"retained": False, "released": False}
            return {
                "writer_lease_active": True,
                "holder_agent_run_id": marker["agent_run_id"],
                "holder_session_id": marker["session_id"],
                "isolated_parked": True,
            }
        if phase == "writer.loser_arm":
            marker, status = self._require_exact_writer_holder(request)
            if self._active_run_ids() != {str(request["agent_run_id"])} or status.get("pending_approvals") != 1:
                raise TestExtensionError("DENIED_TEST_EXTENSION_WRITER_PROOF", "unrelated work is active before loser submit")
            counts = self._durable_counts()
            ids = self._durable_id_snapshot()
            proof = self._action_proof.get("writer")
            if not isinstance(proof, dict):
                raise TestExtensionError("DENIED_TEST_EXTENSION_WRITER_PROOF", "writer holder proof is missing")
            proof["loser"] = {
                "loser_conversation_id": request["loser_conversation_id"],
                "loser_request_token": request["loser_request_token"],
                "counts": counts,
                "ids": ids,
            }
            return {
                "counts_before": counts,
                "holder_agent_run_id": marker["agent_run_id"],
                "isolated_parked": True,
            }
        if phase == "writer.loser_verify":
            proof = self._action_proof.get("writer")
            loser = proof.get("loser") if isinstance(proof, dict) else None
            if not isinstance(loser, Mapping) or not isinstance(loser.get("counts"), Mapping) \
                    or not isinstance(loser.get("ids"), Mapping):
                raise TestExtensionError("DENIED_TEST_EXTENSION_WRITER_PROOF", "writer loser baseline is missing")
            marker, _status = self._require_exact_writer_holder(request)
            counts_after = self._durable_counts()
            ids_after_ui = self._durable_id_snapshot()
            zero_records = counts_after == loser["counts"] and ids_after_ui == loser["ids"]
            same_holder = marker.get("agent_run_id") == request["agent_run_id"]
            if not zero_records or not same_holder:
                raise TestExtensionError("DENIED_TEST_EXTENSION_WRITER_PROOF", "losing writer changed records or lease")
            response = self._direct_writer_conflict_probe()
            ids_after_direct = self._durable_id_snapshot()
            direct_denied = (
                response.get("status") == "DENIED"
                and response.get("code") == "DENIED_WORKSPACE_WRITER_BUSY"
                and ids_after_direct == ids_after_ui
            )
            marker_after, _status_after = self._require_exact_writer_holder(request)
            if not direct_denied or marker_after.get("agent_run_id") != request["agent_run_id"]:
                raise TestExtensionError(
                    "DENIED_TEST_EXTENSION_WRITER_PROOF",
                    "direct pre-provider writer conflict did not preserve exact durable identities",
                )
            proof.update({"zero_records": True, "retained": True, "direct_denied": True})
            return {
                "counts_after": counts_after,
                "zero_record_delta": True,
                "same_holder_retained": True,
                "direct_conflict_denied": True,
            }
        if phase == "writer.holder_release":
            proof = self._action_proof.get("writer")
            status = self._daemon_status()
            released = status.get("writer_claim_active") is False and self._writer_marker() is None
            if (
                not isinstance(proof, dict)
                or proof.get("retained") is not True
                or not released
                or status.get("active_durable_runs") != 0
            ):
                raise TestExtensionError("DENIED_TEST_EXTENSION_WRITER_PROOF", "writer lease was not released")
            proof["released"] = True
            return {"writer_lease_released": True}
        if phase == "restart.daemon":
            if self._run_session_id(str(request["agent_run_id"])) != request["session_id"]:
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_CROSS_RUN", "restart run/session binding is invalid")
            restart = self._restart_isolated_daemon()
            self._action_proof["restart"] = {"restarted": True, "clean": False, **restart}
            return restart
        if phase == "restart.cleanup":
            proof = self._action_proof.get("restart")
            status = self._daemon_status()
            clean = (
                status.get("owned_children") == 0
                and status.get("driven_sessions") == 0
                and status.get("pending_approvals") == 0
                and status.get("active_durable_runs") == 0
                and status.get("writer_claim_active") is False
            )
            if (
                not isinstance(proof, dict)
                or not clean
                or self._run_session_id(str(request["agent_run_id"])) != request["session_id"]
            ):
                raise TestExtensionError("DENIED_TEST_EXTENSION_RESTART_PROOF", "restarted daemon runtime is not clean")
            proof["clean"] = True
            return {
                "runtime_children_clean": True,
                "zero_pending_approvals": True,
                "zero_active_durable_runs": True,
                "writer_lease_released": True,
            }
        raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_PHASE", "test action is unsupported")

    def _receipt_path(self, request_path: Path) -> Path:
        return self.layout.action_receipts / request_path.name

    def _consume_action_requests(self) -> None:
        """Scans `action_requests` in filename order, enforces gapless sequence + replay-safe idempotency, executes each once, writes an atomic receipt + evidence line."""
        approved = self._approved_request
        if approved is None:
            return
        try:
            entries = sorted(self.layout.action_requests.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_FILE", "action request directory is unreadable") from error
        for target in entries:
            if target.name.startswith(".") and target.name.endswith(".tmp"):
                continue
            match = _ACTION_FILE_RE.fullmatch(target.name)
            if match is None:
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_FILE", "action request filename is invalid")
            raw = self._plain_action_file(target)
            digest = hashlib.sha256(raw).hexdigest()
            action_id = match.group("action_id")
            if action_id in self._handled_actions:
                prior_sequence, prior_digest, receipt_path = self._handled_actions[action_id]
                if prior_sequence != int(match.group("sequence")) or not secrets.compare_digest(prior_digest, digest):
                    raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_REPLAY", "handled action was replayed or changed")
                if not receipt_path.is_file():
                    raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_RECEIPT", "idempotent action receipt is missing")
                continue
            sequence = int(match.group("sequence"))
            if sequence != self._next_action_sequence:
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_SEQUENCE", "action files are missing or reordered")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_SCHEMA", "action request is invalid JSON") from error
            request = _validate_action_request(
                value,
                suite_nonce=approved.suite_nonce,
                request_sha256=approved.request_sha256,
                selected=approved.scenarios,
                expected_sequence=self._next_action_sequence,
            )
            if request["action_id"] != action_id:
                raise TestExtensionError("DENIED_TEST_EXTENSION_ACTION_ID", "action id does not match its filename")
            self._require_phase_order(request)
            receipt_path = self._receipt_path(target)
            status, code, proof = "OK", None, {}
            try:
                proof = self._execute_action(request)
            except TestExtensionError as error:
                status, code = "FAILED", error.code
            receipt: dict[str, Any] = {**request, "status": status, "proof": proof}
            if code is not None:
                receipt["code"] = code
            self._write_atomic_json(receipt_path, receipt)
            self._handled_actions[action_id] = (sequence, digest, receipt_path)
            self._next_action_sequence += 1
            self._scenario_phase_index[str(request["scenario_id"])] = (
                self._scenario_phase_index.get(str(request["scenario_id"]), 0) + 1
            )
            safe_proof = {
                key: value for key, value in proof.items()
                if isinstance(value, (str, int, bool))
            }
            self.evidence.append(
                f"action.{request['phase']}", "PASS" if status == "OK" else "FAIL",
                action_id=action_id, sequence=sequence, **({"code": code} if code else {}), **safe_proof,
            )

    def _approval_events(self) -> list[dict[str, Any]]:
        database = self.layout.workspace / "AI" / "db" / "kaizen.db"
        try:
            with _durable_connection(database) as connection:
                rows = connection.execute(
                    "SELECT agent_run_id,sequence_no,source_event_id,correlation_id,marker,code,body "
                    "FROM agent_events WHERE event_kind='approval' ORDER BY agent_run_id,sequence_no"
                ).fetchall()
        except turso.Error as error:
            raise self._durable_denial("approval event ledger is unreadable") from error
        output: list[dict[str, Any]] = []
        for agent_run_id, sequence_no, source_event_id, correlation_id, marker, code, body in rows:
            try:
                parsed = json.loads(body)
            except (TypeError, json.JSONDecodeError):
                parsed = {}
            output.append({
                "agent_run_id": agent_run_id, "sequence_no": sequence_no, "source_event_id": source_event_id,
                "correlation_id": correlation_id, "marker": marker, "code": code,
                "body": parsed if isinstance(parsed, dict) else {},
            })
        return output

    @staticmethod
    def _durable_denial(message: str) -> TestExtensionError:
        return TestExtensionError("DENIED_TEST_EXTENSION_DURABLE_PROOF", message)

    def _run_events(self, agent_run_id: str) -> list[dict[str, Any]]:
        database = self.layout.workspace / "AI" / "db" / "kaizen.db"
        try:
            with _durable_connection(database) as connection:
                rows = connection.execute(
                    "SELECT id,created_at,sequence_no,source_event_id,correlation_id,event_kind,marker,code,name,summary,body "
                    "FROM agent_events WHERE agent_run_id = ? ORDER BY sequence_no",
                    (agent_run_id,),
                ).fetchall()
        except turso.Error as error:
            raise self._durable_denial("agent event ledger is unreadable") from error
        sequences = [row[2] for row in rows]
        if not rows or sequences != list(range(1, len(rows) + 1)) or len({row[0] for row in rows}) != len(rows):
            raise self._durable_denial("agent event sequence is missing, duplicate, or non-gapless")
        return [
            {
                "id": row[0], "created_at": row[1], "sequence_no": row[2], "source_event_id": row[3],
                "correlation_id": row[4], "event_kind": row[5], "marker": row[6], "code": row[7],
                "name": row[8], "summary": row[9], "body_raw": row[10],
            }
            for row in rows
        ]

    def _event_body(self, event: Mapping[str, Any]) -> dict[str, Any]:
        raw = event.get("body_raw")
        if raw in (None, ""):
            return {}
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 1_048_576:
            raise self._durable_denial("durable event body is invalid or oversized")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as error:
            raise self._durable_denial("durable event body is malformed JSON") from error
        if not isinstance(body, dict):
            raise self._durable_denial("durable event body is not an object")
        return body

    @staticmethod
    def _scenario_permission(scenario: str) -> str:
        return "ask" if scenario in {
            "claude-diff-accept", "claude-diff-reject", "claude-process-approval",
            "claude-diff-stale", "claude-diff-corrupt", "claude-diff-timeout",
            "claude-writer-conflict",
        } else "plan"

    def _require_locator(self, safe: Mapping[str, Any], *, correlation: bool = False) -> tuple[str, str, str | None]:
        session_id, agent_run_id = safe.get("session_id"), safe.get("agent_run_id")
        correlation_id = safe.get("correlation_id")
        if not _bounded_identifier(session_id, maximum=160) or not _bounded_identifier(agent_run_id, maximum=160):
            raise self._durable_denial("scenario-local session/run locator is missing or invalid")
        if correlation and not _bounded_identifier(correlation_id, maximum=160):
            raise self._durable_denial("scenario-local correlation locator is missing or invalid")
        return str(session_id), str(agent_run_id), str(correlation_id) if isinstance(correlation_id, str) else None

    def _require_run_envelope(
        self,
        scenario: str,
        safe: Mapping[str, Any],
        request: SuiteRequest,
        *,
        agent_run_id: str | None = None,
        session_id: str | None = None,
        resumed_from: str | None = None,
        finalization: str = "close_ok",
        session_state: str = "closed",
    ) -> tuple[str, str, list[dict[str, Any]], dict[str, Any]]:
        """Asserts the C1/T5 run+session profile binding, terminal-clean reducer, one ordered exact finalization, and the unique first profile point match the approved SuiteRequest; returns (sid, run_id, events, profile)."""
        located_session, located_run, _correlation = self._require_locator(safe)
        run_id = agent_run_id or located_run
        sid = session_id or located_session
        database = self.layout.workspace / "AI" / "db" / "kaizen.db"
        try:
            with _durable_connection(database) as connection:
                run_rows = connection.execute(
                    "SELECT session_id,agent_type,surface,engine,auth_mode,permission_mode,model,reasoning_effort,"
                    "requested_model,requested_reasoning_effort,profile_hash "
                    "FROM agent_runs WHERE id = ?",
                    (run_id,),
                ).fetchall()
                session_rows = connection.execute(
                    "SELECT controller,mode,engine,auth_mode,permission_mode,requested_model,"
                    "requested_reasoning_effort,profile_hash,state FROM agent_sessions WHERE id = ?",
                    (sid,),
                ).fetchall()
                reduced = agent_runs.reduce_run_conn(connection, run_id)
        except turso.Error as error:
            raise self._durable_denial("C1/T5 envelope is unreadable") from error
        expected_engine = "claude" if request.provider == "claude" else "local_llm"
        expected_auth = "subscription" if request.provider == "claude" else "none"
        expected_permission = self._scenario_permission(scenario)
        if len(run_rows) != 1 or len(session_rows) != 1:
            raise self._durable_denial("C1/T5 locator is missing or duplicate")
        run_row, session_row = run_rows[0], session_rows[0]
        if (
            run_row[0] != sid or run_row[1:3] != ("other", "app-server")
            or run_row[3] != expected_engine or run_row[4] != expected_auth
            or run_row[5] != expected_permission or run_row[6] != request.model
            or run_row[7] != request.effort
            or run_row[8] != request.model or run_row[9] != request.effort
            or session_row[0:2] != ("kaizen", "orchestrate")
            or session_row[2] != expected_engine or session_row[3] != expected_auth
            or session_row[4] != expected_permission
            or session_row[5] != request.model or session_row[6] != request.effort
            or session_row[8] != session_state
        ):
            raise self._durable_denial("C1/T5 profile binding disagrees with the approved suite")
        if (
            not reduced.get("terminal")
            or reduced.get("open_children")
            or reduced.get("unresolved_approvals")
            or reduced.get("open_turns")
            or reduced.get("open_tool_calls")
        ):
            raise self._durable_denial("agent run reducer is not terminal and clean")
        events = self._run_events(run_id)
        finalizations = [event for event in events if event["event_kind"] == "finalization"]
        if (
            len(finalizations) != 1
            or finalizations[0]["marker"] != finalization
            or finalizations[0]["sequence_no"] != len(events)
        ):
            raise self._durable_denial("run does not have one ordered exact finalization")
        profiles = [event for event in events if event["event_kind"] == "profile" and event["marker"] == "point"]
        if len(profiles) != 1 or profiles[0]["sequence_no"] != 1:
            raise self._durable_denial("first durable event is not the unique profile point")
        profile = self._event_body(profiles[0])
        effective, requested = profile.get("effective"), profile.get("requested")
        if not isinstance(effective, Mapping) or not isinstance(requested, Mapping):
            raise self._durable_denial("durable profile body is incomplete")
        expected_profile = {
            "model": request.model,
            "reasoning_effort": request.effort,
            "permission_mode": expected_permission,
            "auth_mode": expected_auth,
            "max_turns": request.max_turns,
        }
        if any(effective.get(key) != value for key, value in expected_profile.items()) \
                or any(requested.get(key) != value for key, value in expected_profile.items()):
            raise self._durable_denial("durable requested/effective profile is not exact")
        if (
            not _valid_sha256(profile.get("profile_hash"))
            or run_row[10] != profile.get("profile_hash")
            or session_row[7] != profile.get("profile_hash")
        ):
            raise self._durable_denial("durable profile hash is invalid")
        if resumed_from is not None:
            omitted = profile.get("omitted_message_count")
            if (
                profile.get("resume_fidelity") != "reduced"
                or not isinstance(omitted, int)
                or isinstance(omitted, bool)
                or omitted < 0
            ):
                raise self._durable_denial("resumed profile does not prove reduced-fidelity continuation")
        return sid, run_id, events, profile

    def _require_turn_span(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        terminal: str | Sequence[str] = "close_ok",
    ) -> tuple[str, int, int]:
        """Asserts exactly one single-correlation turn open strictly before one allowed terminal marker; returns (correlation, open_seq, close_seq)."""
        turns = [event for event in events if event.get("event_kind") == "turn"]
        correlations = {event.get("correlation_id") for event in turns}
        allowed = {terminal} if isinstance(terminal, str) else set(terminal)
        if len(correlations) != 1 or None in correlations:
            raise self._durable_denial("turn span is missing or cross-correlated")
        correlation = str(next(iter(correlations)))
        opened = [event for event in turns if event.get("marker") == "open"]
        closed = [event for event in turns if event.get("marker") in {"close_ok", "close_fail", "close_canceled"}]
        if (
            len(opened) != 1 or len(closed) != 1 or closed[0].get("marker") not in allowed
            or int(opened[0]["sequence_no"]) >= int(closed[0]["sequence_no"])
        ):
            raise self._durable_denial("turn span does not have one ordered expected terminal")
        return correlation, int(opened[0]["sequence_no"]), int(closed[0]["sequence_no"])

    def _provider_call_count(
        self,
        events: Sequence[Mapping[str, Any]],
        request: SuiteRequest,
        *,
        allow_zero: bool = False,
    ) -> int:
        """Reads the sole terminal turn body, returns the provider-specific model-call count in [min,max_turns], rejecting the foreign provider's key."""
        terminals = [
            event for event in events
            if event.get("event_kind") == "turn"
            and event.get("marker") in {"close_ok", "close_fail", "close_canceled"}
        ]
        if len(terminals) != 1:
            raise self._durable_denial("provider-call proof does not have one terminal turn event")
        body = self._event_body(terminals[0])
        key, foreign = ("num_turns", "iterations") if request.provider == "claude" else ("iterations", "num_turns")
        value = body.get(key)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not (0 if allow_zero else 1) <= value <= request.max_turns
            or foreign in body
        ):
            raise self._durable_denial("provider-specific terminal model-call count is missing or malformed")
        return value

    def _bind_scenario_runs(
        self,
        scenario: str,
        session_id: str,
        run_ids: Sequence[str],
        correlations: Sequence[tuple[str, str]] = (),
    ) -> None:
        baseline = self._suite_record_baseline
        if baseline is None:
            raise self._durable_denial("suite durable-record baseline is missing")
        exact_runs = tuple(run_ids)
        if not exact_runs or len(set(exact_runs)) != len(exact_runs) or scenario in self._scenario_bound_runs:
            raise self._durable_denial("scenario run binding is empty, duplicate, or already consumed")
        if session_id in baseline["agent_sessions"] or any(run_id in baseline["agent_runs"] for run_id in exact_runs):
            raise self._durable_denial("scenario reused a session or run that existed before suite approval")
        if any(run_id in self._bound_run_owner for run_id in exact_runs):
            raise self._durable_denial("scenario run was already consumed by another scenario")
        prior_session_owner = self._bound_session_owner.get(session_id)
        if prior_session_owner is not None and prior_session_owner != scenario:
            raise self._durable_denial("scenario session was already consumed by another scenario")
        database = self.layout.workspace / "AI" / "db" / "kaizen.db"
        try:
            with _durable_connection(database) as connection:
                linked = tuple(
                    str(row[0]) for row in connection.execute(
                        "SELECT id FROM agent_runs WHERE session_id = ? ORDER BY created_at,id", (session_id,),
                    ).fetchall()
                )
        except turso.Error as error:
            raise self._durable_denial("scenario session/run binding is unreadable") from error
        if set(linked) != set(exact_runs) or len(linked) != len(exact_runs):
            raise self._durable_denial("scenario session contains a missing or foreign run")
        for run_id, correlation_id in correlations:
            if run_id not in exact_runs or not _bounded_identifier(correlation_id, maximum=160):
                raise self._durable_denial("scenario correlation binding is malformed")
            key = (run_id, correlation_id)
            if key in self._bound_correlations:
                raise self._durable_denial("scenario run/correlation was already consumed")
            self._bound_correlations[key] = scenario
        self._scenario_bound_runs[scenario] = exact_runs
        self._bound_session_owner[session_id] = scenario
        for run_id in exact_runs:
            self._bound_run_owner[run_id] = scenario

    def _record_provider_calls(self, scenario: str, run_counts: Mapping[str, int]) -> None:
        if set(run_counts) != set(self._scenario_bound_runs.get(scenario, ())):
            raise self._durable_denial("provider-call proof is not exact for the scenario run binding")
        for run_id, count in run_counts.items():
            if run_id in self._provider_calls_by_run or not isinstance(count, int) or isinstance(count, bool):
                raise self._durable_denial("provider-call count is duplicate or malformed")
            self._provider_calls_by_run[run_id] = count

    def _chat_messages(self, events: Sequence[Mapping[str, Any]]) -> list[tuple[dict[str, Any], Mapping[str, Any]]]:
        output: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
        for event in events:
            if event.get("event_kind") != "chat_message" or event.get("marker") != "point":
                continue
            body = self._event_body(event)
            if body.get("role") not in {"user", "assistant"} or body.get("source") != "driven" \
                    or not isinstance(body.get("text"), str):
                raise self._durable_denial("durable chat message body is malformed")
            output.append((body, event))
        return output

    def _require_turn_messages(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        terminal: str,
        assistant: bool,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, str, int, int]:
        correlation, opened, closed = self._require_turn_span(events, terminal=terminal)
        messages = self._chat_messages(events)
        users = [(body, event) for body, event in messages if body["role"] == "user"]
        assistants = [(body, event) for body, event in messages if body["role"] == "assistant"]
        valid_assistant = len(assistants) == 1 if assistant else len(assistants) == 0
        if (
            len(users) != 1
            or not valid_assistant
            or int(users[0][1]["sequence_no"]) >= opened
            or users[0][0].get("turn_id") is not None
        ):
            raise self._durable_denial("durable chat/turn binding is not exact")
        assistant_body: dict[str, Any] | None = None
        if assistant:
            assistant_body, assistant_event = assistants[0]
            if (
                int(assistant_event["sequence_no"]) <= closed
                or assistant_body.get("turn_id") != correlation
                or assistant_event.get("correlation_id") != correlation
            ):
                raise self._durable_denial("durable assistant is not bound after the exact terminal turn")
        return users[0][0], assistant_body, correlation, opened, closed

    def _require_codeword_chat(
        self,
        scenario: str,
        events: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        manifest = self._manifest_payload()
        words = {
            "claude-text-stream": [manifest["codewords"]["text"]],
            "ollama-text-stream": [manifest["codewords"]["text"]],
            "claude-image-codeword": [manifest["codewords"]["image"]],
            "claude-governed-context": [manifest["codewords"]["context"], manifest["codewords"]["selection"]],
            "ollama-governed-context": [manifest["codewords"]["context"], manifest["codewords"]["selection"]],
        }[scenario]
        user, assistant, _correlation, _opened, _closed = self._require_turn_messages(
            events, terminal="close_ok", assistant=True,
        )
        if assistant is None or any(word not in assistant["text"] for word in words):
            raise self._durable_denial("durable chat does not contain one exact codeword response")
        return user, assistant

    def _read_cached_reference(self, kind: str, value: Mapping[str, Any], reference_key: str) -> bytes:
        from .artifact_cache import ArtifactCache, ArtifactCacheError

        reference, digest, size = value.get(reference_key), value.get("sha256"), value.get("bytes")
        if not isinstance(reference, str) or not _valid_sha256(digest) \
                or not isinstance(size, int) or isinstance(size, bool):
            raise self._durable_denial("durable artifact reference metadata is invalid")
        try:
            return ArtifactCache(self.layout.workspace).read(
                kind, reference, expected_sha256=str(digest), expected_bytes=size,
            )
        except (ArtifactCacheError, OSError) as error:
            raise self._durable_denial("durable artifact reference cannot be rehashed") from error

    def _require_attachment_truth(self, scenario: str, user: Mapping[str, Any]) -> None:
        from ..session_protocol import validate_durable_context_refs, validate_image_refs

        manifest = self._manifest_payload()
        if scenario == "claude-image-codeword":
            try:
                attachments = validate_image_refs(user.get("attachments"))
            except Exception as error:
                raise self._durable_denial("image metadata is not reference-only and exact") from error
            if len(attachments) != 1 or user.get("context_refs") not in (None, []):
                raise self._durable_denial("image message reference count is not exact")
            cached = self._read_cached_reference("images", attachments[0], "artifact_ref")
            fixture = (self.layout.workspace / manifest["fixtures"]["image"]).read_bytes()
            if cached != fixture:
                raise self._durable_denial("image cache bytes disagree with the isolated fixture")
            return
        try:
            context = validate_durable_context_refs(user.get("context_refs"))
        except Exception as error:
            raise self._durable_denial("context metadata is not reference-only and exact") from error
        if len(context) != 2 or user.get("attachments") not in (None, []):
            raise self._durable_denial("context message reference count is not exact")
        by_kind = {str(item["kind"]): item for item in context}
        if set(by_kind) != {"file", "selection"}:
            raise self._durable_denial("context file/selection union is not exact")
        file_item, selection = by_kind["file"], by_kind["selection"]
        if (
            file_item["source_path"] != manifest["fixtures"]["context"]
            or selection["source_path"] != manifest["fixtures"]["selection"]
        ):
            raise self._durable_denial("context paths disagree with the isolated fixtures")
        file_bytes = (self.layout.workspace / str(file_item["source_path"])).read_bytes()
        if len(file_bytes) != file_item["bytes"] or hashlib.sha256(file_bytes).hexdigest() != file_item["sha256"]:
            raise self._durable_denial("governed file snapshot hash is not reproducible")
        selection_bytes = self._read_cached_reference("context", selection, "snapshot_ref")
        exact_selection = f"unsaved dirty selection codeword {manifest['codewords']['selection']}"
        expected_range = {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": len(exact_selection)},
        }
        if selection_bytes != exact_selection.encode("utf-8") or selection.get("range") != expected_range:
            raise self._durable_denial("selection snapshot bytes or requested range are not exact")

    def _require_tool_span(
        self,
        events: Sequence[Mapping[str, Any]],
        correlation_id: str,
        *,
        name: str,
        terminal: str,
        code: str | None = None,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], dict[str, Any], dict[str, Any]]:
        foreign = [event for event in events if event.get("event_kind") == "tool_call"
                   and event.get("correlation_id") != correlation_id]
        span = [event for event in events if event.get("event_kind") == "tool_call"
                and event.get("correlation_id") == correlation_id]
        opened = [event for event in span if event.get("marker") == "open"]
        closed = [event for event in span if event.get("marker") in {"close_ok", "close_fail", "close_canceled"}]
        if foreign or len(opened) != 1 or len(closed) != 1 or closed[0].get("marker") != terminal \
                or (code is not None and closed[0].get("code") != code) \
                or int(opened[0]["sequence_no"]) >= int(closed[0]["sequence_no"]):
            raise self._durable_denial("tool span is foreign, duplicate, or has the wrong terminal")
        open_body, close_body = self._event_body(opened[0]), self._event_body(closed[0])
        if open_body.get("name", open_body.get("tool")) != name \
                or close_body.get("name", close_body.get("tool")) != name:
            raise self._durable_denial("tool span name is not exact")
        return opened[0], closed[0], open_body, close_body

    def _sole_tool_correlation(self, events: Sequence[Mapping[str, Any]], name: str) -> str:
        opened = [event for event in events if event.get("event_kind") == "tool_call" and event.get("marker") == "open"]
        if len(opened) != 1:
            raise self._durable_denial("scenario does not contain exactly one tool open")
        body = self._event_body(opened[0])
        correlation = opened[0].get("correlation_id")
        if body.get("name", body.get("tool")) != name or not _bounded_identifier(correlation, maximum=160):
            raise self._durable_denial("scenario tool name/correlation is malformed")
        return str(correlation)

    def _require_approval_chain(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        session_id: str,
        agent_run_id: str,
        correlation_id: str,
        opens: int,
        terminal: str,
        state: str,
        decided_by: str,
        code: str | None = None,
        diff: bool = False,
        revisions: Sequence[int] | None = None,
    ) -> tuple[list[tuple[Mapping[str, Any], dict[str, Any]]], Mapping[str, Any], str]:
        """Validates the C4 approval open/terminal sequence, shared approval identity + per-open revision, persisted terminal decision body, and the single non-baseline `approval_requests` row; binds approval_id→run."""
        approvals_for_run = [event for event in events if event.get("event_kind") == "approval"]
        if any(event.get("correlation_id") != correlation_id for event in approvals_for_run):
            raise self._durable_denial("approval run contains a foreign correlation")
        opened = [event for event in approvals_for_run if event.get("marker") == "open"]
        terminals = [event for event in approvals_for_run if event.get("marker") in {"resolved", "declined", "timed_out"}]
        if (
            len(opened) != opens or len(terminals) != 1 or terminals[0].get("marker") != terminal
            or terminals[0].get("code") != code
            or any(int(event["sequence_no"]) >= int(terminals[0]["sequence_no"]) for event in opened)
            or any(int(event["sequence_no"]) > int(terminals[0]["sequence_no"]) for event in approvals_for_run)
        ):
            raise self._durable_denial("approval open/terminal sequence is not exact")
        expected_revisions = list(revisions if revisions is not None else range(1, opens + 1))
        if len(expected_revisions) != opens:
            raise self._durable_denial("approval revision expectation is malformed")
        approval_id: str | None = None
        parsed: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
        for event, expected_revision in zip(opened, expected_revisions):
            source = event.get("source_event_id")
            match = re.fullmatch(r"approval:([^:]+):open:([1-9][0-9]*)", source or "")
            if match is None or int(match.group(2)) != expected_revision:
                raise self._durable_denial("approval open source identity or revision is invalid")
            if approval_id is None:
                approval_id = match.group(1)
            elif approval_id != match.group(1):
                raise self._durable_denial("approval revisions do not share one approval identity")
            body = self._event_body(event)
            if diff:
                from .approvals import ApprovalValidationError, validate_approval_open_body

                try:
                    body = validate_approval_open_body(body)
                except ApprovalValidationError as error:
                    raise self._durable_denial("diff approval open body is invalid") from error
                if event.get("name") != "diff_snapshot":
                    raise self._durable_denial("diff approval is missing its negotiated name")
                if body.get("approval_revision") != expected_revision:
                    raise self._durable_denial("diff approval body revision disagrees with its source identity")
            parsed.append((event, body))
        if approval_id is None or terminals[0].get("source_event_id") != f"approval:{approval_id}:terminal":
            raise self._durable_denial("approval terminal does not close the exact approval identity")
        terminal_body = self._event_body(terminals[0])
        if code is None:
            expected_terminal_body = {"decision": state}
        else:
            presentation = "timed_out" if code == "DENIED_APPROVAL_TIMEOUT" else (
                "unavailable" if code in {
                    "DENIED_APPROVAL_BODY_INVALID", "DENIED_APPROVAL_SNAPSHOT_INVALID",
                } else "stale"
            )
            expected_terminal_body = {
                "decision": "denied", "denial_code": code, "presentation": presentation,
            }
        if terminal_body != expected_terminal_body:
            raise self._durable_denial("approval terminal body is not the exact persisted decision")
        database = self.layout.workspace / "AI" / "db" / "kaizen.db"
        try:
            with _durable_connection(database) as connection:
                rows = connection.execute(
                    "SELECT id,session_id,state,decided_by,request_type,correlation_id "
                    "FROM approval_requests WHERE id = ?",
                    (approval_id,),
                ).fetchall()
        except turso.Error as error:
            raise self._durable_denial("approval request ledger is unreadable") from error
        if (
            len(rows) != 1 or rows[0][1] != session_id or rows[0][2] != state or rows[0][3] != decided_by
            or rows[0][4] != "tool_approval" or rows[0][5] != correlation_id
            or (
                self._suite_record_baseline is not None
                and approval_id in self._suite_record_baseline["approval_requests"]
            )
        ):
            raise self._durable_denial("C4 state is missing, duplicate, or cross-correlated")
        prior_owner = self._bound_approval_ids.get(approval_id)
        if prior_owner is not None and prior_owner != agent_run_id:
            raise self._durable_denial("approval identity was already consumed by another run")
        self._bound_approval_ids[approval_id] = agent_run_id
        return parsed, terminals[0], approval_id

    def _require_no_durable_approval(
        self,
        session_id: str,
        events: Sequence[Mapping[str, Any]],
        *,
        allow_policy_declined: bool = False,
    ) -> None:
        approvals_for_run = [event for event in events if event.get("event_kind") == "approval"]
        if (
            (approvals_for_run and not allow_policy_declined)
            or any(event.get("marker") != "declined" for event in approvals_for_run)
        ):
            raise self._durable_denial("Plan/tool-policy denial contains an approval open or foreign terminal")
        database = self.layout.workspace / "AI" / "db" / "kaizen.db"
        try:
            with _durable_connection(database) as connection:
                count = int(connection.execute(
                    "SELECT COUNT(*) FROM approval_requests WHERE session_id = ?", (session_id,),
                ).fetchone()[0])
        except turso.Error as error:
            raise self._durable_denial("Plan/tool-policy C4 state is unreadable") from error
        if count != 0:
            raise self._durable_denial("Plan/tool-policy denial created a C4 row")

    def _require_process_request(
        self,
        open_body: Mapping[str, Any],
        correlation_id: str,
        executable: str,
        argv: Sequence[str],
    ) -> None:
        call_id = open_body.get("tool_call_id", open_body.get("call_id"))
        observed_executable = open_body.get("executable")
        try:
            executable_matches = isinstance(observed_executable, str) and (
                Path(observed_executable).resolve() == Path(executable).resolve()
                if Path(executable).is_absolute() else observed_executable == executable
            )
        except OSError:
            executable_matches = False
        if (
            call_id != correlation_id
            or not executable_matches
            or open_body.get("argv") != list(argv)
            or open_body.get("cwd") != "."
            or open_body.get("timeout_ms") != 5000
        ):
            raise self._durable_denial("durable process request is not the exact bounded command")

    def _require_process_result(self, close_body: Mapping[str, Any], correlation_id: str) -> None:
        call_id = close_body.get("tool_call_id", close_body.get("call_id"))
        result = close_body.get("result")
        expected = {
            "exit_code": 0,
            "timed_out": False,
            "truncated": False,
            "effects_unknown": True,
            "policy_decision": "ask",
            "stdout_sha256": hashlib.sha256(b"TE_PROCESS_OK\n").hexdigest(),
            "stdout_bytes": len(b"TE_PROCESS_OK\n"),
        }
        if call_id != correlation_id or not isinstance(result, Mapping) \
                or any(result.get(key) != value for key, value in expected.items()):
            raise self._durable_denial("durable process result is incomplete or disagrees with execution truth")

    def _require_diff_open(
        self,
        parsed: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
        index: int = -1,
        *,
        verify_proposed: bool = True,
    ) -> tuple[Mapping[str, Any], str, str]:
        manifest = self._manifest_payload()
        try:
            event, body = parsed[index]
        except IndexError as error:
            raise self._durable_denial("diff approval open is missing") from error
        changes = body.get("file_changes")
        target = str(manifest["fixtures"]["diff_target"])
        if not isinstance(changes, list) or len(changes) != 1:
            raise self._durable_denial("diff approval does not contain exactly one fixture change")
        change = changes[0]
        before, proposed = self._side_sha(change, "before"), self._side_sha(change, "proposed")
        if not isinstance(change, Mapping) or change.get("path") != target or before is None or proposed is None:
            raise self._durable_denial("diff approval fixture path or side hashes are invalid")
        cache = self.layout.workspace / "AI" / "work" / "orchestration" / "ui-cache" / "diffs" / "sha256"
        sides_to_verify = (("before", before), ("proposed", proposed)) if verify_proposed else (("before", before),)
        for side_name, digest in sides_to_verify:
            side = change.get(side_name)
            target_object = cache / digest
            try:
                info = target_object.lstat()
                raw = target_object.read_bytes()
                valid = (
                    stat.S_ISREG(info.st_mode)
                    and not self._is_reparse(info)
                    and info.st_size <= 8 * 1024 * 1024
                    and target_object.resolve().parent == cache.resolve()
                    and hashlib.sha256(raw).hexdigest() == digest
                    and isinstance(side, Mapping)
                    and side.get("bytes") == len(raw)
                )
            except OSError:
                valid = False
            if not valid:
                raise self._durable_denial("diff snapshot object cannot be rehashed from its durable manifest")
        return event, before, proposed

    @staticmethod
    def _diff_change(event: Mapping[str, Any], target_path: str) -> Mapping[str, Any] | None:
        body = event.get("body")
        changes = body.get("file_changes") if isinstance(body, Mapping) else None
        if not isinstance(changes, list):
            return None
        return next((change for change in changes if isinstance(change, Mapping) and change.get("path") == target_path), None)

    @staticmethod
    def _side_sha(change: Mapping[str, Any] | None, side: str) -> str | None:
        value = change.get(side) if isinstance(change, Mapping) else None
        digest = value.get("sha256") if isinstance(value, Mapping) else None
        return digest if _valid_sha256(digest) else None

    def _validate_controlled_pass(self, scenario: str, safe: Mapping[str, Any]) -> None:
        if scenario == "claude-diff-stale":
            action = self._action_proof.get("stale")
            if not isinstance(action, Mapping):
                raise TestExtensionError("DENIED_TEST_EXTENSION_CONTROL_PROOF", "stale control action is missing")
            binding = action.get("binding")
            if (
                not isinstance(binding, Mapping)
                or any(safe.get(key) != binding.get(key) for key in ("session_id", "agent_run_id", "correlation_id"))
                or safe.get("diff_before_sha256") != action.get("before")
                or safe.get("diff_mutated_base_sha256") != action.get("mutated")
                or safe.get("diff_revision") != binding.get("revision")
                or safe.get("diff_snapshot_set_sha256") != binding.get("snapshot_set_sha256")
                or safe.get("diff_path") != action.get("path")
            ):
                raise TestExtensionError("DENIED_TEST_EXTENSION_CONTROL_PROOF", "stale base hashes disagree with outer action")
            if self._sha256_path(self.layout.workspace / str(action["path"])) != safe.get("diff_final_sha256"):
                raise TestExtensionError("DENIED_TEST_EXTENSION_CONTROL_PROOF", "stale revision proof is not durable")
        elif scenario == "claude-diff-corrupt":
            action = self._action_proof.get("corrupt")
            binding = action.get("binding") if isinstance(action, Mapping) else None
            if (
                not isinstance(action, Mapping)
                or not isinstance(binding, Mapping)
                or any(safe.get(key) != binding.get(key) for key in ("session_id", "agent_run_id", "correlation_id"))
                or safe.get("diff_snapshot_set_sha256") != binding.get("snapshot_set_sha256")
                or safe.get("diff_revision") != binding.get("revision")
                or safe.get("diff_corrupted_artifact_sha256") != action.get("corrupted")
                or action.get("corrupted") == action.get("digest")
                or safe.get("diff_proposed_sha256") != action.get("digest")
            ):
                raise TestExtensionError("DENIED_TEST_EXTENSION_CONTROL_PROOF", "corrupt action proof is missing")
            if self._sha256_path(self.layout.workspace / str(safe.get("diff_path"))) != safe.get("diff_final_sha256"):
                raise TestExtensionError("DENIED_TEST_EXTENSION_CONTROL_PROOF", "corrupt denial proof is not durable")
        elif scenario == "claude-writer-conflict":
            action = self._action_proof.get("writer")
            binding = self._scenario_bindings.get(scenario)
            if (
                not isinstance(action, Mapping)
                or not isinstance(binding, Mapping)
                or any(safe.get(key) != binding.get(key) for key in ("session_id", "agent_run_id", "correlation_id"))
                or not all(action.get(key) is True for key in ("zero_records", "retained", "released", "direct_denied"))
            ):
                raise TestExtensionError("DENIED_TEST_EXTENSION_CONTROL_PROOF", "writer conflict proof is incomplete")
        elif scenario == "claude-interrupt-restart":
            action = self._action_proof.get("restart")
            binding = self._scenario_bindings.get(scenario)
            if (
                not isinstance(action, Mapping)
                or not isinstance(binding, Mapping)
                or safe.get("session_id") != binding.get("session_id")
                or safe.get("previous_agent_run_id") != binding.get("agent_run_id")
                or safe.get("agent_run_id") == safe.get("previous_agent_run_id")
                or action.get("restarted") is not True
                or action.get("clean") is not True
            ):
                raise TestExtensionError("DENIED_TEST_EXTENSION_CONTROL_PROOF", "restart control proof is incomplete")

    def _validate_authoritative_pass(
        self,
        scenario: str,
        safe: Mapping[str, Any],
        request: SuiteRequest,
    ) -> None:
        """Per-scenario durable-ledger PASS contract: asserts run envelope/turn/tool/approval spans, sha256 target invariants, sequence ordering, binds runs+correlations; raises _durable_denial on any mismatch."""
        if scenario == "claude-traversal-zero-record":
            proof = self._action_proof.get("traversal")
            if (
                not isinstance(proof, Mapping)
                or not isinstance(proof.get("before_ids"), Mapping)
                or proof.get("after_ids") != proof.get("before_ids")
                or proof.get("code") != "DENIED_CONTEXT_INVALID"
            ):
                raise self._durable_denial("traversal denial changed durable row identities")
            return

        if scenario == "claude-interrupt-restart":
            self._validate_controlled_pass(scenario, safe)
            session_id, new_run_id, _ = self._require_locator(safe)
            old_run_id = safe.get("previous_agent_run_id")
            if not _bounded_identifier(old_run_id, maximum=160) or old_run_id == new_run_id:
                raise self._durable_denial("restart old/new run locators are missing or reused")
            database = self.layout.workspace / "AI" / "db" / "kaizen.db"
            with _durable_connection(database) as connection:
                linked = tuple(str(row[0]) for row in connection.execute(
                    "SELECT id FROM agent_runs WHERE session_id = ? ORDER BY created_at,id", (session_id,),
                ).fetchall())
            if linked != (str(old_run_id), new_run_id):
                raise self._durable_denial("restart run chronology is not exactly old then resumed")
            _sid, _old, old_events, _old_profile = self._require_run_envelope(
                scenario, safe, request, agent_run_id=str(old_run_id), session_id=session_id,
                finalization="close_canceled", session_state="closed",
            )
            _sid, _new, new_events, _new_profile = self._require_run_envelope(
                scenario, safe, request, agent_run_id=new_run_id, session_id=session_id,
                resumed_from=str(old_run_id), finalization="close_ok", session_state="closed",
            )
            self._require_turn_messages(old_events, terminal="close_canceled", assistant=False)
            old_final = old_events[-1]
            if old_final.get("summary") != "orphan-sweep force-finalize: owning daemon dead" \
                    or old_final.get("code") != "canceled":
                raise self._durable_denial("restart old run lacks the exact orphan-sweep finalization")
            _user, assistant, _turn, _opened, _closed = self._require_turn_messages(
                new_events, terminal="close_ok", assistant=True,
            )
            if assistant is None or self._manifest_payload()["codewords"]["text"] not in assistant["text"]:
                raise self._durable_denial("restart continuation lacks its durable codeword response")
            self._require_no_durable_approval(session_id, old_events)
            self._require_no_durable_approval(session_id, new_events)
            self._bind_scenario_runs(scenario, session_id, (str(old_run_id), new_run_id))
            self._record_provider_calls(scenario, {
                str(old_run_id): self._provider_call_count(old_events, request, allow_zero=True),
                new_run_id: self._provider_call_count(new_events, request),
            })
            return

        fatal = scenario in {"claude-diff-corrupt", "claude-diff-timeout"}
        session_id, run_id, events, _profile = self._require_run_envelope(
            scenario, safe, request,
            finalization="close_fail" if fatal else "close_ok",
            session_state="failed" if fatal else "closed",
        )
        run_calls = self._provider_call_count(events, request)
        correlations: list[tuple[str, str]] = []

        if scenario in {
            "claude-text-stream", "ollama-text-stream", "claude-image-codeword",
            "claude-governed-context", "ollama-governed-context",
        }:
            user, _assistant = self._require_codeword_chat(scenario, events)
            if scenario in {"claude-image-codeword", "claude-governed-context", "ollama-governed-context"}:
                self._require_attachment_truth(scenario, user)
            elif user.get("attachments") not in (None, []) or user.get("context_refs") not in (None, []):
                raise self._durable_denial("plain text scenario contains unexpected structured references")
            if any(event.get("event_kind") == "tool_call" for event in events):
                raise self._durable_denial("codeword-only scenario contains a tool span")
            self._require_no_durable_approval(session_id, events)

        elif scenario == "cleanup-leak-state":
            user, _assistant, turn_correlation, turn_opened, turn_closed = self._require_turn_messages(
                events, terminal="close_ok", assistant=True,
            )
            if user.get("attachments") not in (None, []) or user.get("context_refs") not in (None, []):
                raise self._durable_denial("cleanup scenario contains unexpected structured references")
            tool_correlation = self._sole_tool_correlation(events, "kaizen_read_file")
            tool_open, tool_close, open_body, close_body = self._require_tool_span(
                events, tool_correlation, name="kaizen_read_file", terminal="close_ok",
            )
            fixture_path = str(self._manifest_payload()["fixtures"]["context"])
            fixture = self.layout.workspace / fixture_path
            result = close_body.get("result")
            if (
                open_body.get("tool_call_id", open_body.get("call_id")) != tool_correlation
                or open_body.get("path") != fixture_path
                or not isinstance(result, Mapping)
                or result.get("path") != fixture_path
                or result.get("sha256") != self._sha256_path(fixture)
                or result.get("truncated") is not False
                or turn_correlation == tool_correlation
                or not turn_opened < int(tool_open["sequence_no"]) < int(tool_close["sequence_no"]) < turn_closed
            ):
                raise self._durable_denial("cleanup read tool does not prove one exact bounded fixture read")
            self._require_no_durable_approval(session_id, events)
            correlations.append((run_id, tool_correlation))

        elif scenario in {"claude-plan-controls", "ollama-tool-policy"}:
            _sid, _rid, correlation = self._require_locator(safe, correlation=True)
            assert correlation is not None
            _user, _assistant, turn_correlation, _opened, turn_closed = self._require_turn_messages(
                events, terminal="close_ok", assistant=True,
            )
            tool_name = "kaizen_run_process"
            tool_correlations = tuple(dict.fromkeys(
                str(event["correlation_id"])
                for event in events
                if event.get("event_kind") == "tool_call" and event.get("name") == tool_name
                and event.get("correlation_id") is not None
            ))
            expected_count = 2 if scenario == "claude-plan-controls" else 1
            if len(tool_correlations) != expected_count or correlation not in tool_correlations:
                raise self._durable_denial("client tool locator disagrees with the exact durable tool spans")
            plan_correlation = correlation
            plan_events = [
                event for event in events
                if event.get("event_kind") != "tool_call" or event.get("correlation_id") == plan_correlation
            ]
            plan_open, plan_close, plan_body, plan_result = self._require_tool_span(
                plan_events, plan_correlation, name=tool_name, terminal="close_fail",
                code=str(safe.get("denial_code")),
            )
            self._require_process_request(
                plan_body, plan_correlation, str(self.python), ("-c", "print('MUST_NOT_RUN')"),
            )
            result = plan_result.get("result")
            if not isinstance(result, Mapping) or result:
                raise self._durable_denial("blocked Plan process contains execution-result fields")
            if not int(plan_open["sequence_no"]) < int(plan_close["sequence_no"]) < turn_closed:
                raise self._durable_denial("Plan tool denial is not ordered inside its terminal turn")
            if turn_correlation == plan_correlation:
                raise self._durable_denial("turn and tool spans improperly reuse one correlation")
            correlations.append((run_id, plan_correlation))
            if scenario == "claude-plan-controls":
                invariant_correlation = next(item for item in tool_correlations if item != plan_correlation)
                invariant_events = [
                    event for event in events
                    if event.get("event_kind") != "tool_call"
                    or event.get("correlation_id") == invariant_correlation
                ]
                inv_open, inv_close, inv_body, inv_result = self._require_tool_span(
                    invariant_events, invariant_correlation, name=tool_name, terminal="close_fail",
                    code=str(safe.get("invariant_denial_code")),
                )
                self._require_process_request(
                    inv_body, invariant_correlation, "git", ("push", "origin", "main"),
                )
                invariant_result = inv_result.get("result")
                if not isinstance(invariant_result, Mapping) or invariant_result:
                    raise self._durable_denial("blocked invariant process contains execution-result fields")
                if not (
                    int(plan_close["sequence_no"]) < int(inv_open["sequence_no"])
                    < int(inv_close["sequence_no"]) < turn_closed
                ):
                    raise self._durable_denial("Plan and invariant denials are not ordered inside the turn")
                if turn_correlation == invariant_correlation:
                    raise self._durable_denial("turn and invariant spans improperly reuse one correlation")
                correlations.append((run_id, invariant_correlation))
            self._require_no_durable_approval(
                session_id, events, allow_policy_declined=scenario == "ollama-tool-policy",
            )

        elif scenario == "claude-process-approval":
            _sid, _rid, correlation = self._require_locator(safe, correlation=True)
            assert correlation is not None
            _user, _assistant, _turn, _opened, turn_closed = self._require_turn_messages(
                events, terminal="close_ok", assistant=True,
            )
            tool_correlation = self._sole_tool_correlation(events, "kaizen_run_process")
            tool_open, tool_close, open_body, close_body = self._require_tool_span(
                events, tool_correlation, name="kaizen_run_process", terminal="close_ok",
            )
            parsed, approval_terminal, _approval_id = self._require_approval_chain(
                events, session_id=session_id, agent_run_id=run_id, correlation_id=correlation,
                opens=1, terminal="resolved", state="approved", decided_by="human",
            )
            approval_body = parsed[0][1]
            expected_body = {
                "tool": "kaizen_run_process", "executable": str(self.python),
                "argv": ["-c", "print('TE_PROCESS_OK')"], "cwd": ".", "timeout_ms": 5000,
            }
            if approval_body != expected_body:
                raise self._durable_denial("process approval body is not the exact bounded request")
            self._require_process_request(
                open_body, tool_correlation, str(self.python), ("-c", "print('TE_PROCESS_OK')"),
            )
            self._require_process_result(close_body, tool_correlation)
            if not (
                int(tool_open["sequence_no"]) < int(parsed[0][0]["sequence_no"])
                < int(approval_terminal["sequence_no"]) < int(tool_close["sequence_no"]) < turn_closed
            ):
                raise self._durable_denial("process tool/approval/result chain is out of order")
            correlations.extend(((run_id, correlation), (run_id, tool_correlation)))

        elif scenario in {
            "claude-diff-accept", "claude-diff-reject", "claude-diff-stale",
            "claude-diff-corrupt", "claude-diff-timeout", "claude-writer-conflict",
        }:
            _sid, _rid, correlation = self._require_locator(safe, correlation=True)
            assert correlation is not None
            if scenario in {"claude-diff-stale", "claude-diff-corrupt", "claude-writer-conflict"}:
                self._validate_controlled_pass(scenario, safe)
            turn_terminal = "close_fail" if fatal else "close_ok"
            assistant_required = not fatal
            _user, _assistant, _turn, _opened, turn_closed = self._require_turn_messages(
                events, terminal=turn_terminal, assistant=assistant_required,
            )
            terminal_marker = "resolved" if scenario in {"claude-diff-accept", "claude-diff-stale"} else (
                "timed_out" if scenario == "claude-diff-timeout" else "declined"
            )
            state = "approved" if terminal_marker == "resolved" else "denied"
            decided_by = "auto" if scenario in {"claude-diff-corrupt", "claude-diff-timeout"} else "human"
            denial_code = {
                "claude-diff-corrupt": "DENIED_APPROVAL_SNAPSHOT_INVALID",
                "claude-diff-timeout": "DENIED_APPROVAL_TIMEOUT",
            }.get(scenario)
            opens = 2 if scenario == "claude-diff-stale" else 1
            revisions = (
                (int(safe["diff_revision"]), int(safe["diff_refreshed_revision"]))
                if scenario == "claude-diff-stale" else (int(safe.get("diff_revision", 1)),)
            )
            tool_terminal = "close_ok" if terminal_marker == "resolved" else "close_fail"
            tool_code = denial_code or (None if tool_terminal == "close_ok" else "DENIED_TOOL_REJECTED")
            tool_correlation = self._sole_tool_correlation(events, "kaizen_propose_changes")
            tool_open, tool_close, _tool_open_body, _tool_close_body = self._require_tool_span(
                events, tool_correlation, name="kaizen_propose_changes", terminal=tool_terminal, code=tool_code,
            )
            parsed, approval_terminal, _approval_id = self._require_approval_chain(
                events, session_id=session_id, agent_run_id=run_id, correlation_id=correlation,
                opens=opens, terminal=terminal_marker, state=state, decided_by=decided_by,
                code=denial_code, diff=True, revisions=revisions,
            )
            first_event, first_before, first_proposed = self._require_diff_open(
                parsed, 0, verify_proposed=scenario != "claude-diff-corrupt",
            )
            latest_event, latest_before, latest_proposed = self._require_diff_open(
                parsed, -1, verify_proposed=scenario != "claude-diff-corrupt",
            )
            if not (
                int(tool_open["sequence_no"]) < int(first_event["sequence_no"])
                <= int(latest_event["sequence_no"]) < int(approval_terminal["sequence_no"])
                < int(tool_close["sequence_no"]) < turn_closed
            ):
                raise self._durable_denial("diff tool/approval/result chain is out of order")
            manifest_target = self.layout.workspace / str(self._manifest_payload()["fixtures"]["diff_target"])
            if scenario == "claude-diff-accept":
                if self._sha256_path(manifest_target) != first_proposed:
                    raise self._durable_denial("accepted diff target does not match the durable proposed snapshot")
            elif scenario in {"claude-diff-reject", "claude-writer-conflict"}:
                if self._sha256_path(manifest_target) != first_before:
                    raise self._durable_denial("denied diff changed its authoritative target")
            elif scenario == "claude-diff-stale":
                if (
                    parsed[0][1].get("snapshot_set_sha256") != safe.get("diff_snapshot_set_sha256")
                    or parsed[1][1].get("snapshot_set_sha256") != safe.get("diff_refreshed_snapshot_set_sha256")
                    or first_before != safe.get("diff_before_sha256")
                    or latest_before != safe.get("diff_refreshed_before_sha256")
                    or latest_proposed != safe.get("diff_proposed_sha256")
                    or latest_before != safe.get("diff_mutated_base_sha256")
                    or self._sha256_path(self.layout.workspace / str(safe["diff_path"])) != latest_proposed
                ):
                    raise self._durable_denial("stale diff revisions do not reconcile with outer mutation truth")
            elif scenario == "claude-diff-corrupt":
                action = self._action_proof.get("corrupt")
                cache = self.layout.workspace / "AI" / "work" / "orchestration" / "ui-cache" / "diffs" / "sha256"
                if (
                    not isinstance(action, Mapping)
                    or first_before != safe.get("diff_before_sha256")
                    or first_proposed != safe.get("diff_proposed_sha256")
                    or self._sha256_path(cache / first_proposed) != action.get("corrupted")
                    or self._sha256_path(self.layout.workspace / str(safe["diff_path"])) != first_before
                ):
                    raise self._durable_denial("corrupt snapshot denial does not preserve exact bytes")
            elif scenario == "claude-diff-timeout":
                if self._sha256_path(manifest_target) != first_before:
                    raise self._durable_denial("timed-out diff changed its target")
            correlations.extend(((run_id, correlation), (run_id, tool_correlation)))

        else:
            raise self._durable_denial("scenario authoritative PASS contract is undefined")

        self._bind_scenario_runs(scenario, session_id, (run_id,), correlations)
        self._record_provider_calls(scenario, {run_id: run_calls})

    def _validate_suite_authority(self, request: SuiteRequest) -> None:
        """Post-suite reconciliation: durable id-snapshot delta equals exactly the bound runs/sessions/approvals, provider-call totals within call_ceiling AND reserved_calls, no foreign events, no open C4/nonterminal spans, runtime globally clean when cleanup-leak selected."""
        baseline = self._suite_record_baseline
        if baseline is None:
            raise self._durable_denial("suite durable-record baseline is missing")
        expected_scenarios = set(request.scenarios) - {"claude-traversal-zero-record"}
        if set(self._scenario_bound_runs) != expected_scenarios:
            raise self._durable_denial("suite scenarios do not have an exact authoritative run partition")
        expected_runs = set().union(*(set(value) for value in self._scenario_bound_runs.values()))
        expected_sessions = set(self._bound_session_owner)
        current = self._durable_id_snapshot()
        if (
            current["agent_runs"] != baseline["agent_runs"] | expected_runs
            or current["agent_sessions"] != baseline["agent_sessions"] | expected_sessions
            or current["approval_requests"] != baseline["approval_requests"] | set(self._bound_approval_ids)
            or not baseline["agent_events"].issubset(current["agent_events"])
            or current["user_instructions"] != baseline["user_instructions"]
            or set(self._provider_calls_by_run) != expected_runs
            or sum(self._provider_calls_by_run.values()) > request.call_ceiling
            or sum(self._provider_calls_by_run.values()) > request.reserved_calls
        ):
            raise self._durable_denial("suite has foreign/missing durable rows or exceeded its actual provider-call ceiling")
        database = self.layout.workspace / "AI" / "db" / "kaizen.db"
        try:
            with _durable_connection(database) as connection:
                new_events = connection.execute(
                    "SELECT id,agent_run_id FROM agent_events"
                ).fetchall()
                if any(
                    str(event_id) not in baseline["agent_events"] and str(run_id) not in expected_runs
                    for event_id, run_id in new_events
                ):
                    raise self._durable_denial("suite contains an event for an unbound run")
                if connection.execute("SELECT COUNT(*) FROM approval_requests WHERE state = 'open'").fetchone()[0] != 0:
                    raise self._durable_denial("suite cleanup left an open C4 row")
                for run_id in expected_runs:
                    state = agent_runs.reduce_run_conn(connection, run_id)
                    if not state.get("terminal") or state.get("open_children") or state.get("unresolved_approvals") \
                            or state.get("open_turns") or state.get("open_tool_calls"):
                        raise self._durable_denial("suite cleanup left a nonterminal or open durable span")
        except turso.Error as error:
            raise self._durable_denial("suite durable cleanup scan is unreadable") from error
        if "cleanup-leak-state" in request.scenarios:
            status = self._daemon_status()
            if not (
                status.get("owned_children") == 0
                and status.get("driven_sessions") == 0
                and status.get("pending_approvals") == 0
                and status.get("active_durable_runs") == 0
                and status.get("writer_claim_active") is False
            ):
                raise self._durable_denial("suite runtime cleanup is not globally clean")

    def child_env(self) -> dict[str, str]:
        env = children.compose_child_env()
        for key in tuple(env):
            if key.upper() == "ELECTRON_RUN_AS_NODE" or key.upper().startswith("VSCODE_"):
                env.pop(key, None)
        source, existing = str(self.source_root), env.get("PYTHONPATH")
        provider_runtime = require_non_system_drive(
            Path(env.get("KAIZEN_CLAUDE_PROVIDER_RUNTIME_ROOT") or source),
            label="provider runtime root",
        )
        self._provider_runtime_root = provider_runtime
        env.update(
            {
                "KAIZEN_REPO_ROOT": str(self.layout.workspace),
                "KAIZEN_TEST_EXTENSION_RUN_ROOT": str(self.layout.root),
                "KAIZEN_TEST_EXTENSION_EVIDENCE_PATH": str(self.layout.evidence),
                "KAIZEN_TEST_EXTENSION_CLIENT_EVIDENCE_PATH": str(self.layout.client_evidence),
                "KAIZEN_TEST_EXTENSION_CONTROL_PATH": str(self.layout.control),
                "KAIZEN_TEST_EXTENSION_ACTION_ROOT": str(self.layout.action_root),
                "KAIZEN_TEST_EXTENSION_EDH_READY_PATH": str(self.layout.edh_ready),
                "KAIZEN_TEST_EXTENSION_PROVIDER_RETRIES": "0",
                "KAIZEN_HTTP_RETRIES": "0",
                "KAIZEN_CLAUDE_PROVIDER_RUNTIME_ROOT": str(provider_runtime),
                "PYTHONPATH": source if not existing else source + os.pathsep + existing,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": str(self.layout.temp / "pycache"),
                "TEMP": str(self.layout.temp),
                "TMP": str(self.layout.temp),
                "TMPDIR": str(self.layout.temp),
                "SQLITE_TMPDIR": str(self.layout.temp),
            }
        )
        return env

    def _run_kaizen(self, *args: str, timeout: float = 30.0) -> dict[str, Any]:
        timeout = self._bounded_active_timeout(timeout)
        result = self.command_runner(
            [str(self.python), str(self.source_root / "kaizen.py"), *args, "--json"],
            cwd=str(self.source_root), env=self.child_env(), capture_output=True, text=True, timeout=timeout,
        )
        payload = _parse_json_output(result)
        if result.returncode != 0 or payload.get("status") in {"DENIED", "ERROR"}:
            raise TestExtensionError(str(payload.get("code") or "DENIED_TEST_EXTENSION_CHILD")[:128], "child operation failed")
        return payload

    def initialize(self) -> None:
        self._source_fingerprint = source_fingerprint(self.source_root)
        prepare_plane(self.layout, self.source_root)
        self.evidence.append(
            "run.open", "START", wall_seconds=self.wall_seconds,
            max_simultaneous_turns=MAX_SIMULTANEOUS_TURNS, provider_retries=0,
        )
        self._run_kaizen("K1", timeout=30.0)
        self.evidence.append("plane.initialized", "PASS", fake_git_boundary=True, source_files_fingerprinted=len(self._source_fingerprint))

    def verify_source_immutable(self) -> bool:
        current = source_fingerprint(self.source_root)
        unchanged = self._source_fingerprint is not None and current == self._source_fingerprint
        self.evidence.append(
            "source.immutable", "PASS" if unchanged else "FAIL",
            files_checked=len(current), mutation_detected=not unchanged,
        )
        return unchanged

    def _capture_claude_provider_baseline(self) -> None:
        try:
            disk_fingerprint = provider_target_fingerprint(self._provider_runtime_root or self.source_root)
        except (OSError, KeyError, TypeError, claude_runtime.ClaudeRuntimeError) as error:
            self.evidence.append("provider.immutable_baseline", "FAIL", reproducible=False)
            raise TestExtensionError(
                "DENIED_TEST_EXTENSION_PROVIDER_TARGET_CHANGED",
                "provider runtime target is not independently reproducible",
            ) from error
        matches = (
            isinstance(self._provider_target_fingerprint, str)
            and len(self._provider_target_fingerprint) == 64
            and disk_fingerprint == self._provider_target_fingerprint
        )
        self.evidence.append(
            "provider.immutable_baseline", "PASS" if matches else "FAIL",
            reproducible=matches, provider_target_fingerprint=disk_fingerprint,
        )
        if not matches:
            raise TestExtensionError(
                "DENIED_TEST_EXTENSION_PROVIDER_TARGET_CHANGED",
                "provider runtime target disagrees with the daemon baseline",
            )
        self._provider_disk_fingerprint = disk_fingerprint

    def _verify_claude_provider_final(self, status_payload: Mapping[str, Any]) -> bool:
        self._provider_final_verified = True
        try:
            disk_fingerprint = provider_target_fingerprint(self._provider_runtime_root or self.source_root)
        except (OSError, KeyError, TypeError, claude_runtime.ClaudeRuntimeError):
            disk_fingerprint = None
        unchanged = (
            isinstance(self._provider_target_fingerprint, str)
            and len(self._provider_target_fingerprint) == 64
            and status_payload.get("provider_target_fingerprint") == self._provider_target_fingerprint
            and self._provider_disk_fingerprint == self._provider_target_fingerprint
            and disk_fingerprint == self._provider_disk_fingerprint
        )
        self.evidence.append(
            "provider.immutable_final", "PASS" if unchanged else "FAIL",
            provider_target_unchanged=unchanged,
        )
        self._provider_final_ok = unchanged
        return unchanged

    def _ensure_claude_provider_final(self) -> bool:
        request = self._approved_request
        if request is None or request.provider != "claude":
            return True
        if self._provider_final_verified:
            return self._provider_final_ok is True
        try:
            status_payload = self._run_kaizen("daemon", "status", timeout=5.0)
        except (TestExtensionError, subprocess.TimeoutExpired, OSError):
            self._provider_final_verified = True
            self._provider_final_ok = False
            self.evidence.append(
                "provider.immutable_final", "FAIL", provider_target_unchanged=False,
            )
            return False
        return self._verify_claude_provider_final(status_payload)

    def _verify_claude_provider_post_cleanup(self, *, cleanup_proven: bool) -> bool:
        request = self._approved_request
        if request is None or request.provider != "claude":
            return True
        if self._provider_post_cleanup_verified:
            return self._provider_post_cleanup_ok is True
        self._provider_post_cleanup_verified = True
        try:
            disk_fingerprint = provider_target_fingerprint(self._provider_runtime_root or self.source_root)
        except (OSError, KeyError, TypeError, claude_runtime.ClaudeRuntimeError):
            disk_fingerprint = None
        unchanged = (
            cleanup_proven
            and isinstance(self._provider_target_fingerprint, str)
            and len(self._provider_target_fingerprint) == 64
            and self._provider_disk_fingerprint == self._provider_target_fingerprint
            and disk_fingerprint == self._provider_disk_fingerprint
        )
        self._provider_post_cleanup_ok = unchanged
        self.evidence.append(
            "provider.immutable_post_cleanup", "PASS" if unchanged else "FAIL",
            provider_target_unchanged=unchanged, cleanup_proven=cleanup_proven,
        )
        return unchanged

    def start_daemon(self) -> None:
        if self._consume_any_stop():
            raise _TestExtensionStopRequested()
        child = self.spawner(
            [str(self.python), str(self.source_root / "kaizen.py"), "daemon", "run"], cwd=str(self.source_root),
            env=self.child_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.children.append(("daemon", child))
        self.evidence.append("daemon.starting", "START")
        deadline = self._phase_deadline(DAEMON_READY_TIMEOUT_SECONDS)
        last_failed_gates: tuple[str, ...] = ("control_response",)
        while self.monotonic() < deadline:
            if self._consume_any_stop():
                raise _TestExtensionStopRequested()
            try:
                capabilities = sanitize_capabilities(
                    self._run_kaizen("daemon", "session", "capabilities", timeout=5.0)
                )
                status = self._run_kaizen("daemon", "status", timeout=5.0)
                last_failed_gates = daemon_readiness_failures(
                    status, capabilities, child, self.layout.workspace,
                )
                if last_failed_gates:
                    raise TestExtensionError(
                        "DENIED_TEST_EXTENSION_DAEMON_STARTING",
                        "isolated daemon has not completed its bound capability startup",
                    )
                self.capabilities = capabilities
                fingerprint = status.get("provider_target_fingerprint")
                candidate = fingerprint if isinstance(fingerprint, str) else None
                if self._provider_target_fingerprint is None:
                    self._provider_target_fingerprint = candidate
                elif candidate != self._provider_target_fingerprint:
                    raise TestExtensionError(
                        "DENIED_TEST_EXTENSION_PROVIDER_TARGET_CHANGED",
                        "provider runtime target changed inside the approved suite",
                    )
                self.evidence.append(
                    "daemon.ready", "PASS", capabilities=self.capabilities,
                    provider_target_fingerprint=self._provider_target_fingerprint,
                )
                return
            except TestExtensionError as error:
                if error.code == WALL_CLOCK_CODE:
                    raise
                if error.code != "DENIED_TEST_EXTENSION_DAEMON_STARTING":
                    last_failed_gates = ("control_response",)
                self._active_sleep(0.25)
            except (subprocess.TimeoutExpired, OSError):
                if self._wall_exhausted():
                    raise TestExtensionError(WALL_CLOCK_CODE, "test-extension outer wall clock is exhausted")
                last_failed_gates = ("control_response",)
                self._active_sleep(0.25)
        if self._wall_exhausted():
            raise TestExtensionError(WALL_CLOCK_CODE, "test-extension outer wall clock is exhausted")
        self.evidence.append(
            "daemon.readiness", "FAIL", code="DENIED_TEST_EXTENSION_DAEMON_TIMEOUT",
            failed_gates=list(last_failed_gates),
        )
        raise TestExtensionError("DENIED_TEST_EXTENSION_DAEMON_TIMEOUT", "isolated daemon did not become ready")

    def _validate_edh_ready(self) -> None:
        target = self.layout.edh_ready
        try:
            info = target.lstat()
            with target.open("rb") as stream:
                raw = stream.read(EDH_READY_MAX_BYTES + 1)
        except OSError as error:
            raise TestExtensionError("DENIED_TEST_EXTENSION_EDH_READY_INVALID", "EDH ready artifact is unreadable") from error
        if (
            not stat.S_ISREG(info.st_mode)
            or self._is_reparse(info)
            or len(raw) < 2
            or len(raw) > EDH_READY_MAX_BYTES
            or target.resolve().parent != self.layout.root.resolve()
        ):
            raise TestExtensionError("DENIED_TEST_EXTENSION_EDH_READY_INVALID", "EDH ready artifact is not a bounded plain file")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TestExtensionError("DENIED_TEST_EXTENSION_EDH_READY_INVALID", "EDH ready artifact is invalid JSON") from error
        manifest_raw = self.layout.manifest.read_bytes()
        manifest = self._manifest_payload()
        expected = {
            "v": 1,
            "run_id": self.layout.run_id,
            "suite_nonce": manifest["suite_nonce"],
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        }
        if not isinstance(value, Mapping) or set(value) != set(expected):
            raise TestExtensionError("DENIED_TEST_EXTENSION_EDH_READY_INVALID", "EDH ready artifact fields are not exact")
        if value != expected:
            raise TestExtensionError("DENIED_TEST_EXTENSION_EDH_READY_FORGED", "EDH ready artifact does not bind this plane")

    def _edh_exit_code(self) -> str:
        """Classify only bounded, run-local VS Code diagnostics into stable safe codes."""
        log_root = self.layout.user_data / "logs"
        try:
            root_info = log_root.lstat()
            root_resolved = log_root.resolve()
            if (
                not stat.S_ISDIR(root_info.st_mode)
                or self._is_reparse(root_info)
                or root_resolved.parent != self.layout.user_data.resolve()
            ):
                return EDH_EXIT_CODE
            sessions: list[Path] = []
            for entry in log_root.iterdir():
                if len(sessions) >= EDH_LOG_DIRECTORY_LIMIT:
                    return EDH_EXIT_CODE
                sessions.append(entry)
            for session in sessions:
                session_info = session.lstat()
                session_resolved = session.resolve()
                if (
                    not stat.S_ISDIR(session_info.st_mode)
                    or self._is_reparse(session_info)
                    or session_resolved.parent != root_resolved
                ):
                    continue
                main_log = session / "main.log"
                log_info = main_log.lstat()
                if (
                    not stat.S_ISREG(log_info.st_mode)
                    or self._is_reparse(log_info)
                    or not 0 < log_info.st_size <= EDH_MAIN_LOG_MAX_BYTES
                    or main_log.resolve().parent != session_resolved
                ):
                    continue
                with main_log.open("rb") as stream:
                    raw = stream.read(EDH_MAIN_LOG_MAX_BYTES + 1)
                if len(raw) <= EDH_MAIN_LOG_MAX_BYTES and _VSCODE_UPDATE_IN_PROGRESS_SIGNATURE in raw:
                    return VSCODE_UPDATE_IN_PROGRESS_CODE
        except OSError:
            return EDH_EXIT_CODE
        return EDH_EXIT_CODE

    def _wait_for_edh_ready(self, child: children.OwnedChild) -> None:
        deadline = self._phase_deadline(EDH_READY_TIMEOUT_SECONDS)
        while self.monotonic() < deadline:
            if self._consume_any_stop():
                raise _TestExtensionStopRequested()
            if child.poll() is not None:
                raise TestExtensionError(self._edh_exit_code(), "Extension Development Host exited before readiness")
            if self.layout.edh_ready.exists():
                self._validate_edh_ready()
                return
            self._active_sleep(0.1)
        if self._wall_exhausted():
            raise TestExtensionError(WALL_CLOCK_CODE, "test-extension outer wall clock is exhausted")
        raise TestExtensionError("DENIED_TEST_EXTENSION_EDH_READY_TIMEOUT", "Extension Development Host did not attest readiness")

    def _attest_edh_shared_storage(self) -> None:
        root = self.layout.shared_data
        shared = root / "sharedStorage"
        target = shared / "state.vscdb"
        try:
            root_info, shared_info, target_info = root.lstat(), shared.lstat(), target.lstat()
        except OSError as error:
            raise TestExtensionError(
                "DENIED_TEST_EXTENSION_EDH_SHARED_STORAGE",
                "Extension Development Host did not create isolated shared storage",
            ) from error
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or not stat.S_ISDIR(shared_info.st_mode)
            or not stat.S_ISREG(target_info.st_mode)
            or self._is_reparse(root_info)
            or self._is_reparse(shared_info)
            or self._is_reparse(target_info)
            or root.resolve() != (self.layout.root / "vscode-shared-data").resolve()
            or shared.resolve().parent != root.resolve()
            or target.resolve().parent != shared.resolve()
            or target_info.st_size == 0
        ):
            raise TestExtensionError(
                "DENIED_TEST_EXTENSION_EDH_SHARED_STORAGE",
                "Extension Development Host shared storage is not an isolated plain database",
            )

    def start_edh(self) -> None:
        if self._consume_any_stop():
            raise _TestExtensionStopRequested()
        if self.layout.edh_ready.exists():
            raise TestExtensionError("DENIED_TEST_EXTENSION_EDH_READY_STALE", "EDH ready artifact preceded child start")
        args = [
            str(self.code_path), "--new-window", "--wait",
            f"--extensionDevelopmentPath={self.source_root / 'extension'}",
            "--user-data-dir", str(self.layout.user_data), "--extensions-dir", str(self.layout.extensions),
            "--shared-data-dir", str(self.layout.shared_data),
            "--disable-updates", "--disable-crash-reporter", "--disable-telemetry",
            "--skip-welcome", "--skip-release-notes", str(self.layout.workspace),
        ]
        self.evidence.append("edh.starting", "START")
        try:
            child = self.spawner(
                args, cwd=str(self.layout.workspace), env=self.child_env(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            self.evidence.append("edh.readiness", "FAIL", code=EDH_SPAWN_CODE)
            raise TestExtensionError(EDH_SPAWN_CODE, "Extension Development Host could not start") from error
        self.children.append(("edh", child))
        try:
            self._wait_for_edh_ready(child)
            self._attest_edh_shared_storage()
        except TestExtensionError as error:
            self.evidence.append("edh.readiness", "FAIL", code=error.code)
            raise
        self.evidence.append(
            "edh.started", "READY", development_extension=True, isolated_user_data=True, isolated_shared_data=True,
            activation_attested=True,
        )

    def _read_control_file(self, target: Path) -> Mapping[str, Any] | None:
        if not target.is_file():
            return None
        try:
            raw = target.read_bytes()
            if len(raw) > 64 * 1024:
                raise TestExtensionError("DENIED_TEST_EXTENSION_CONTROL_OVERSIZE", "control request is oversized")
            encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            value = json.loads(raw.decode(encoding))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TestExtensionError("DENIED_TEST_EXTENSION_CONTROL", "control request is unreadable") from error
        return value if isinstance(value, Mapping) else None

    def _read_control(self) -> Mapping[str, Any] | None:
        return self._read_control_file(self.layout.control)

    def _read_launcher_stop(self) -> Mapping[str, Any] | None:
        return self._read_control_file(self.layout.root / "launcher-stop.json")

    def _accept_preapproval_stop(self, value: Any) -> dict[str, Any]:
        if self._approved_request is not None:
            raise TestExtensionError(
                "DENIED_TEST_EXTENSION_CONTROL", "approved suites require the bound action protocol",
            )
        manifest = self._manifest_payload()
        request = validate_preapproval_stop(
            value,
            suite_nonce=str(manifest.get("suite_nonce") or ""),
            seen_stop_ids=self._preapproval_stop_ids,
        )
        self._preapproval_stop_ids.add(str(request["stop_id"]))
        return request

    def _consume_preapproval_stop(self) -> bool:
        control = self._read_control()
        if control is None or control.get("action") != "stop":
            return False
        stopped = self._accept_preapproval_stop(control)
        self.evidence.append(
            "suite.stop_requested", "STOP", pre_approval=True,
            pending_suite_bound="request_sha256" in stopped,
        )
        return True

    def _consume_launcher_stop(self) -> bool:
        if self._launcher_stop_consumed:
            return True
        control = self._read_launcher_stop()
        if control is None:
            return False
        manifest = self._manifest_payload()
        stopped = validate_preapproval_stop(
            control,
            suite_nonce=str(manifest.get("suite_nonce") or ""),
            seen_stop_ids=self._preapproval_stop_ids,
        )
        self._preapproval_stop_ids.add(str(stopped["stop_id"]))
        self._launcher_stop_consumed = True
        self._stop_requested = True
        self.evidence.append(
            "suite.stop_requested", "STOP", launcher=True,
            pre_approval=self._approved_request is None,
            pending_suite_bound="request_sha256" in stopped,
        )
        return True

    def _consume_any_stop(self) -> bool:
        return self._consume_launcher_stop() or self._consume_preapproval_stop()

    def _accept_external_block(self, value: Any) -> dict[str, Any]:
        if self._approved_request is not None:
            raise TestExtensionError(
                "DENIED_TEST_EXTENSION_CONTROL", "approved suites require the bound action protocol",
            )
        request = validate_external_block(
            value,
            suite_nonce=str(self._manifest_payload().get("suite_nonce") or ""),
            capabilities=self.capabilities,
            seen_block_ids=self._external_block_ids,
        )
        self._external_block_ids.add(str(request["block_id"]))
        return request

    def _reload_control_capabilities(self) -> None:
        """Re-read the daemon-authoritative cache after any editor-side explicit refresh."""
        self.capabilities = sanitize_capabilities(
            self._run_kaizen("daemon", "session", "capabilities", timeout=5.0)
        )

    def _consume_client_evidence(self) -> int | None:
        if not self.layout.client_evidence.is_file():
            return None
        size = self.layout.client_evidence.stat().st_size
        if size < self._client_offset or size > 8 * 1024 * 1024:
            raise TestExtensionError("DENIED_TEST_EXTENSION_CLIENT_EVIDENCE", "client evidence boundary is invalid")
        with self.layout.client_evidence.open("r", encoding="utf-8") as stream:
            stream.seek(self._client_offset)
            lines, self._client_offset = stream.readlines(), stream.tell()
        terminal: int | None = None
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise TestExtensionError("DENIED_TEST_EXTENSION_CLIENT_EVIDENCE", "client evidence is invalid JSONL") from error
            if (
                not isinstance(value, Mapping)
                or type(value.get("v")) is not int
                or value.get("v") != 1
                or not isinstance(value.get("event"), str)
            ):
                raise TestExtensionError("DENIED_TEST_EXTENSION_CLIENT_EVIDENCE", "client evidence envelope is invalid")
            event, safe = str(value["event"]), _sanitize_client_evidence(value)
            if event == "suite.complete":
                if self._approved_request is None:
                    raise TestExtensionError("DENIED_TEST_EXTENSION_CLIENT_EVIDENCE", "suite completed before approval")
                selected = set(self._approved_request.scenarios)
                received = set(self._scenario_results)
                if received != selected:
                    raise TestExtensionError(
                        "DENIED_TEST_EXTENSION_SCENARIO_RECONCILIATION",
                        "suite completion is missing or adds selected scenario results",
                    )
                counts = {
                    "passed": sum(status == "PASS" for status in self._scenario_results.values()),
                    "failed": sum(status == "FAIL" for status in self._scenario_results.values()),
                    "not_run": sum(status == "NOT_RUN" for status in self._scenario_results.values()),
                }
                if any(safe.get(key) != count for key, count in counts.items()):
                    raise TestExtensionError(
                        "DENIED_TEST_EXTENSION_SCENARIO_RECONCILIATION", "suite totals do not match scenario results"
                    )
                if not counts["failed"] and not counts["not_run"]:
                    self._validate_suite_authority(self._approved_request)
                if counts["failed"]:
                    terminal, status = 1, "FAIL"
                elif counts["not_run"]:
                    terminal, status = 3, "NOT_RUN"
                else:
                    terminal, status = 0, "PASS"
                self._suite_counts = counts
            elif event in {"scenario.complete", "scenario.not_run", "suite.started"}:
                status = str(safe.pop("status", "INFO"))
                mapped = status if status in {"PASS", "FAIL", "NOT_RUN", "START", "INFO"} else "INFO"
                if event != "suite.started":
                    if self._approved_request is None:
                        raise TestExtensionError("DENIED_TEST_EXTENSION_CLIENT_EVIDENCE", "scenario result preceded approval")
                    scenario = safe.get("scenario")
                    if not isinstance(scenario, str) or scenario not in self._approved_request.scenarios:
                        raise TestExtensionError(
                            "DENIED_TEST_EXTENSION_SCENARIO_RECONCILIATION", "unselected scenario result was reported"
                        )
                    if scenario in self._scenario_results:
                        raise TestExtensionError(
                            "DENIED_TEST_EXTENSION_SCENARIO_RECONCILIATION", "duplicate scenario result was reported"
                        )
                    expected_statuses = {"scenario.complete": {"PASS", "FAIL"}, "scenario.not_run": {"NOT_RUN"}}
                    if mapped not in expected_statuses[event]:
                        raise TestExtensionError(
                            "DENIED_TEST_EXTENSION_SCENARIO_RECONCILIATION", "scenario event and status disagree"
                        )
                    if event == "scenario.not_run":
                        reason = safe.get("code")
                        normalized_reason = external_scenario_block_code(reason)
                        if normalized_reason is None or reason != normalized_reason:
                            raise TestExtensionError(
                                "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_CODE",
                                "NOT_RUN requires one exact external provider code",
                            )
                    if safe.get("provider") != self._approved_request.provider:
                        raise TestExtensionError(
                            "DENIED_TEST_EXTENSION_SCENARIO_RECONCILIATION", "scenario provider disagrees with approval"
                        )
                    if mapped == "PASS":
                        _validate_pass_proof(scenario, safe, self._approved_request, self.python)
                        self._validate_authoritative_pass(scenario, safe, self._approved_request)
                    self._scenario_results[scenario] = mapped
                self.evidence.append(event, mapped, **_safe_evidence_fields(safe))
        return terminal

    def wait(self) -> int:
        """Main control loop: consumes stop/external-block/suite-approval control, drives action requests + client-evidence reconciliation, runs final provider/cleanup verifications, returns suite exit code (0/1/3/124/130)."""
        deadline = self._run_deadline if self._run_deadline is not None else self.monotonic() + self.wall_seconds
        approved, prior_control = False, None
        while self.monotonic() < deadline:
            if self._consume_launcher_stop():
                return 130
            control = self._read_control()
            if control is not None:
                encoded = json.dumps(control, sort_keys=True, separators=(",", ":")).encode("utf-8")
                if encoded != prior_control:
                    prior_control = encoded
                    if control.get("action") == "stop":
                        self._consume_preapproval_stop()
                        return 130
                    self._reload_control_capabilities()
                    if control.get("action") == "external_block":
                        blocked = self._accept_external_block(control)
                        provider, code = str(blocked["provider"]), str(blocked["code"])
                        scenarios = tuple(str(scenario) for scenario in blocked["scenarios"])
                        self._scenario_results = {scenario: "NOT_RUN" for scenario in scenarios}
                        self._suite_counts = {"passed": 0, "failed": 0, "not_run": len(scenarios)}
                        for scenario in scenarios:
                            self.evidence.append(
                                "scenario.not_run", "NOT_RUN", scenario=scenario, provider=provider, code=code,
                            )
                        self.evidence.append(
                            "suite.external_block", "NOT_RUN", provider=provider, code=code,
                            scenarios=list(scenarios), provider_calls=0,
                        )
                        self.evidence.append("suite.complete", "NOT_RUN", **self._suite_counts)
                        return 3
                    request = validate_suite_request(control, self.capabilities)
                    if request.suite_nonce != self._manifest_payload()["suite_nonce"]:
                        raise TestExtensionError("DENIED_TEST_EXTENSION_SUITE_NONCE", "suite nonce is not this plane")
                    if approved:
                        raise TestExtensionError("DENIED_TEST_EXTENSION_CONTROL_DUPLICATE", "suite configuration is immutable")
                    approved = True
                    self._approved_request = request
                    if request.provider == "claude":
                        self._capture_claude_provider_baseline()
                    self._scenario_results.clear()
                    self._suite_counts = None
                    self._suite_record_baseline = self._durable_id_snapshot()
                    self._write_suite_binding()
                    self.evidence.append(
                        "suite.approved", "READY", provider=request.provider, model=request.model,
                        effort=request.effort, max_turns=request.max_turns, call_ceiling=request.call_ceiling,
                        calls_reserved=request.reserved_calls, scenarios=list(request.scenarios), provider_retries=0,
                        request_sha256=request.request_sha256,
                    )
            if approved:
                self._consume_action_requests()
                if self._stop_requested:
                    self.evidence.append("suite.stop_requested", "STOP")
                    return 130
                outcome = self._consume_client_evidence()
                if outcome is not None:
                    request = self._approved_request
                    status_payload: dict[str, Any] | None = None
                    if request and (request.provider == "claude" or "cleanup-leak-state" in request.scenarios):
                        status_payload = self._run_kaizen("daemon", "status", timeout=5.0)
                    provider_ok = True
                    if request and request.provider == "claude":
                        provider_ok = self._verify_claude_provider_final(status_payload or {})
                    cleanup_ok = True
                    if request and "cleanup-leak-state" in request.scenarios:
                        if status_payload is None:
                            status_payload = self._run_kaizen("daemon", "status", timeout=5.0)
                        cleanup_ok = (
                            status_payload.get("owned_children") == 0
                            and status_payload.get("driven_sessions") == 0
                            and status_payload.get("pending_approvals") == 0
                            and status_payload.get("active_durable_runs") == 0
                            and status_payload.get("writer_claim_active") is False
                        )
                        self.evidence.append(
                            "cleanup.leak_state", "PASS" if cleanup_ok else "FAIL",
                            zero_children=status_payload.get("owned_children") == 0,
                            zero_sessions=status_payload.get("driven_sessions") == 0,
                            zero_approvals=status_payload.get("pending_approvals") == 0,
                            zero_active_durable_runs=status_payload.get("active_durable_runs") == 0,
                            writer_released=status_payload.get("writer_claim_active") is False,
                        )
                    if not provider_ok or not cleanup_ok:
                        outcome = 1
                    final_status = "PASS" if outcome == 0 else "NOT_RUN" if outcome == 3 else "FAIL"
                    self.evidence.append("suite.complete", final_status, **(self._suite_counts or {}))
                    return outcome
            for name, child in self.children:
                if child.poll() is not None:
                    raise TestExtensionError("WORKER_DIED", f"{name} child exited before suite completion")
            self._active_sleep(0.2)
        for name, child in self.children:
            if child.poll() is not None:
                raise TestExtensionError("WORKER_DIED", f"{name} child exited before suite completion")
        self.evidence.append("suite.wall_clock", "FAIL", code=WALL_CLOCK_CODE)
        return 124

    def cleanup(self) -> bool:
        proven = True
        retained: list[tuple[str, children.OwnedChild]] = []
        present = {name for name, _child in self.children}
        if "edh" not in present:
            self.evidence.append("cleanup.edh", "PASS", termination_proven=True, not_started=True)
        for name, child in reversed(self.children):
            terminated = False
            for attempt in (1, 2):
                try:
                    child.kill_tree(timeout=5.0)
                    if child.poll() is None:
                        raise children.ChildTerminationError("child remained live after kill_tree")
                    terminated = True
                    self.evidence.append(
                        f"cleanup.{name}", "PASS", termination_proven=True, attempt=attempt,
                    )
                    break
                except children.ChildTerminationError:
                    self.evidence.append(
                        f"cleanup.{name}", "FAIL", termination_proven=False, attempt=attempt,
                        retrying=attempt == 1, preserved_for_audit=attempt == 2,
                    )
            if not terminated:
                proven = False
                retained.append((name, child))
        if "daemon" not in present:
            self.evidence.append("cleanup.daemon", "PASS", termination_proven=True, not_started=True)
        self.children = list(reversed(retained))
        return proven

    def run(self) -> int:
        """Lifecycle wrapper: initialize->daemon->EDH->wait under a run deadline; finally enforces provider-final, cleanup, provider-post-cleanup, source-immutable gates (any failure forces exit 1)."""
        exit_code = 1
        self._run_deadline = self.monotonic() + self.wall_seconds
        try:
            self.initialize(); self.start_daemon(); self.start_edh(); exit_code = self.wait()
        except _TestExtensionStopRequested:
            exit_code = 130
        except (TestExtensionError, subprocess.TimeoutExpired) as error:
            code = error.code if isinstance(error, TestExtensionError) else "DENIED_TEST_EXTENSION_CHILD_TIMEOUT"
            if code == WALL_CLOCK_CODE or self._wall_exhausted():
                exit_code = 124
                self.evidence.append("suite.wall_clock", "FAIL", code=WALL_CLOCK_CODE)
                print(f"Kaizen Test Extension failed: {WALL_CLOCK_CODE}", file=sys.stderr, flush=True)
            else:
                self.evidence.append("run.failed", "FAIL", code=code)
                print(f"Kaizen Test Extension failed: {code}", file=sys.stderr, flush=True)
        except Exception:
            exit_code = 1
            self.evidence.append("run.failed", "FAIL", code=INTERNAL_ERROR_CODE)
            print(f"Kaizen Test Extension failed: {INTERNAL_ERROR_CODE}", file=sys.stderr, flush=True)
        finally:
            self._run_deadline = None
            if not self._ensure_claude_provider_final():
                exit_code = 1
            cleanup_proven = self.cleanup()
            if not cleanup_proven:
                exit_code = 1
            if not self._verify_claude_provider_post_cleanup(cleanup_proven=cleanup_proven):
                exit_code = 1
            if not self.verify_source_immutable():
                exit_code = 1
            closed_status = "PASS" if exit_code == 0 else "NOT_RUN" if exit_code == 3 else "STOP" if exit_code == 130 else "FAIL"
            self.evidence.append("run.closed", closed_status, exit_code=exit_code)
        return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Kaizen Test Extension in a visible terminal")
    parser.add_argument("--source-root", required=True, help="Agent Kaizen source checkout (read-only for the run)")
    parser.add_argument("--plane-base", help="non-system-drive test-extension root (default: DEVROOT/test-extension)")
    parser.add_argument("--run-id", help="exact bounded run id supplied by the visible launcher")
    parser.add_argument("--python", default=sys.executable, help="shared Kaizen venv Python")
    parser.add_argument("--code-path", required=True, help="VS Code executable for the isolated EDH")
    parser.add_argument("--wall-seconds", type=int, default=MAX_WALL_SECONDS, help="outer wall clock, maximum 1800")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source = require_non_system_drive(Path(args.source_root), label="source root")
        plane_base = args.plane_base.strip()
        plane = require_non_system_drive(Path(plane_base), label="plane base") if plane_base else default_plane_base(source)
        layout = create_layout(source, plane, run_id=args.run_id)
        runner = OuterRunner(
            source_root=source, layout=layout, python=Path(args.python), code_path=Path(args.code_path),
            wall_seconds=args.wall_seconds,
        )
        print(f"Kaizen Test Extension run: {layout.run_id}", flush=True)
        print(f"Evidence: {layout.evidence}", flush=True)
        print("Keep this terminal open; use Stop in the editor tab for authenticated cleanup. Closing it is an unproven emergency abort.", flush=True)
        return runner.run()
    except TestExtensionError as error:
        print(f"Kaizen Test Extension refused: {error.code}", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print(f"Kaizen Test Extension refused: {INTERNAL_ERROR_CODE}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
