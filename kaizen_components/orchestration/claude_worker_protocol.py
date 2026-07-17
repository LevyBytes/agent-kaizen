"""Pure v1 contract for the private Claude Agent SDK worker channel."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1 << 20
MAX_DELTA_BYTES = 32 * 1024
MAX_TURNS_MIN = 1
MAX_TURNS_DEFAULT = 8
MAX_TURNS_MAX = 32
MODEL_CATALOG_TTL_SECONDS = 300

HOST_OPERATIONS = frozenset({
    "initialize", "capability.probe", "turn.start", "turn.steer", "turn.interrupt", "session.close",
    "tool.result",
})
WORKER_EVENTS = frozenset({
    "initialized", "turn.open", "delta", "status", "rate_limit", "tool.invoke", "tool.open",
    "tool.close", "turn.result", "fatal",
})
TURN_EVENTS = frozenset({
    "turn.open", "delta", "status", "rate_limit", "tool.invoke", "tool.open", "tool.close",
    "turn.result",
})
TOOL_NAMES = (
    "kaizen_read_file",
    "kaizen_list_files",
    "kaizen_search_text",
    "kaizen_run_process",
    "kaizen_propose_changes",
)
CAPABILITY_PROBE_VERSION = 1
CAPABILITY_PROBE_EVIDENCE = {
    "streaming": "sdk-delta-parser-and-32k-fragmentation",
    "image_attachments": "verified-reference-to-sdk-image-block",
    "governed_context": "verified-reference-to-governed-prompt",
    "diff_snapshots": "proposal-outbox-reference-roundtrip",
    "controlled_tools": "exact-kaizen-tool-schema-set",
    "process_execution": "direct-argv-process-schema",
}
CAPABILITY_PROBE_FEATURES = tuple(CAPABILITY_PROBE_EVIDENCE)
DENIAL_CODES = frozenset({
    "DENIED_SDK_UNAVAILABLE",
    "DENIED_AUTH_UNAVAILABLE",
    "DENIED_AUTH_MODE_MISMATCH",
    "DENIED_MODEL_UNAVAILABLE",
    "DENIED_EFFORT_UNSUPPORTED",
    "DENIED_WORKER_PROTOCOL",
    "DENIED_WORKER_OVERSIZE",
    "DENIED_TOOL_UNSUPPORTED",
    "DENIED_TOOL_CONCURRENCY",
    "DENIED_PROVIDER_CAPACITY",
    "MODEL_CALL_BUDGET_EXHAUSTED",
    "WORKER_DIED",
})

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


class WorkerProtocolError(ValueError):
    """Stable fail-closed worker protocol denial exposing code (default DENIED_WORKER_PROTOCOL) and field attributes."""

    def __init__(self, code: str = "DENIED_WORKER_PROTOCOL", field: str = "frame") -> None:
        super().__init__(f"{code}: {field}")
        self.code = code
        self.field = field


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise WorkerProtocolError(field=field)
    return value


def _frame_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (json.dumps(dict(value), ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError) as error:
        raise WorkerProtocolError(field="frame") from error


def encode_frame(value: Mapping[str, Any]) -> bytes:
    """Validate and encode one newline-terminated frame within the loopback ceiling."""

    clean = validate_frame(value)
    payload = _frame_bytes(clean)
    if len(payload) > MAX_FRAME_BYTES:
        raise WorkerProtocolError("DENIED_WORKER_OVERSIZE", "frame")
    return payload


def decode_frame(value: bytes | str) -> dict[str, Any]:
    """Decode exactly one JSON object; trailing data and over-limit input are refused."""

    raw = value.encode("utf-8") if isinstance(value, str) else value
    if not isinstance(raw, bytes):
        raise WorkerProtocolError(field="frame")
    if len(raw) > MAX_FRAME_BYTES:
        raise WorkerProtocolError("DENIED_WORKER_OVERSIZE", "frame")
    if b"\n" in raw.rstrip(b"\r\n"):
        raise WorkerProtocolError(field="frame")
    try:
        decoded = raw.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkerProtocolError(field="frame") from error
    return validate_frame(payload)


def validate_frame(value: Any) -> dict[str, Any]:
    """Public (in `__all__`) and undocumented: state it returns a shallow-copied dict, enforces `v == PROTOCOL_VERSION`, and applies the per-type (request/response/event) required/allowed-key plus `turn_id`/`seq` rules, raising `WorkerProtocolError`. Highest-value gap."""
    if not isinstance(value, Mapping) or value.get("v") != PROTOCOL_VERSION:
        raise WorkerProtocolError(field="v")
    frame = dict(value)
    frame_type = frame.get("type")
    if frame_type == "request":
        required, allowed = {"v", "type", "id", "op", "body"}, {
            "v", "type", "id", "op", "session_id", "turn_id", "body",
        }
        if not required.issubset(frame) or set(frame) - allowed:
            raise WorkerProtocolError(field="request")
        _identifier(frame.get("id"), "id")
        operation = frame.get("op")
        if operation not in HOST_OPERATIONS:
            raise WorkerProtocolError(field="op")
        if "session_id" not in frame:
            raise WorkerProtocolError(field="session_id")
        if operation in {"turn.start", "turn.steer", "turn.interrupt", "tool.result"}:
            if "turn_id" not in frame:
                raise WorkerProtocolError(field="turn_id")
        elif "turn_id" in frame:
            raise WorkerProtocolError(field="turn_id")
    elif frame_type == "response":
        required, allowed = {"v", "type", "id", "ok"}, {"v", "type", "id", "ok", "body", "error"}
        if not required.issubset(frame) or set(frame) - allowed or not isinstance(frame.get("ok"), bool):
            raise WorkerProtocolError(field="response")
        _identifier(frame.get("id"), "id")
        if frame["ok"] and "error" in frame or not frame["ok"] and "body" in frame:
            raise WorkerProtocolError(field="response")
    elif frame_type == "event":
        required, allowed = {"v", "type", "event", "session_id", "seq", "body"}, {
            "v", "type", "event", "session_id", "turn_id", "seq", "body",
        }
        if not required.issubset(frame) or set(frame) - allowed:
            raise WorkerProtocolError(field="event")
        if frame.get("event") not in WORKER_EVENTS:
            raise WorkerProtocolError(field="event")
        event = frame.get("event")
        if event in TURN_EVENTS and "turn_id" not in frame:
            raise WorkerProtocolError(field="turn_id")
        if event == "initialized" and "turn_id" in frame:
            raise WorkerProtocolError(field="turn_id")
        seq = frame.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise WorkerProtocolError(field="seq")
    else:
        raise WorkerProtocolError(field="type")
    for field in ("session_id", "turn_id"):
        if field in frame:
            _identifier(frame[field], field)
    if "body" in frame and not isinstance(frame["body"], Mapping):
        raise WorkerProtocolError(field="body")
    if "error" in frame and not isinstance(frame["error"], Mapping):
        raise WorkerProtocolError(field="error")
    return frame


def capability_probe_digest(feature: str, challenge: str) -> str:
    """Bind one probe response to its requested feature and one-use host challenge."""

    import hashlib

    if feature not in CAPABILITY_PROBE_EVIDENCE:
        raise WorkerProtocolError(field="capability.feature")
    _identifier(challenge, "capability.challenge")
    material = (
        f"kaizen-capability-probe-v{CAPABILITY_PROBE_VERSION}\0{feature}\0{challenge}\0"
        f"{CAPABILITY_PROBE_EVIDENCE[feature]}"
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def validate_capability_probe_result(value: Any, *, feature: str, challenge: str) -> dict[str, Any]:
    """Validate exact per-feature evidence; malformed or cross-feature claims fail closed."""

    if feature not in CAPABILITY_PROBE_FEATURES:
        raise WorkerProtocolError(field="capability.feature")
    _identifier(challenge, "capability.challenge")
    if not isinstance(value, Mapping):
        raise WorkerProtocolError(field="capability.result")
    required = {"probe_version", "feature", "challenge", "status", "evidence_sha256"}
    if feature == "diff_snapshots":
        required.add("artifact_ref")
    if set(value) != required:
        raise WorkerProtocolError(field="capability.result")
    if value.get("probe_version") != CAPABILITY_PROBE_VERSION \
            or value.get("feature") != feature or value.get("challenge") != challenge \
            or value.get("status") != "proven":
        raise WorkerProtocolError(field="capability.result")
    expected = capability_probe_digest(feature, challenge)
    if value.get("evidence_sha256") != expected:
        raise WorkerProtocolError(field="capability.evidence_sha256")
    clean = dict(value)
    if feature == "diff_snapshots":
        clean["artifact_ref"] = validate_reference(value.get("artifact_ref"))
    return clean


def validate_reference(value: Any) -> dict[str, Any]:
    """Validate the metadata-only reference shape; filesystem containment is checked by each reader."""

    if not isinstance(value, Mapping):
        raise WorkerProtocolError(field="reference")
    allowed = {"root", "path", "sha256", "bytes", "encoding", "media_type"}
    required = {"root", "path", "sha256", "bytes"}
    if not required.issubset(value) or set(value) - allowed:
        raise WorkerProtocolError(field="reference")
    root, path = value.get("root"), value.get("path")
    if root not in ("runtime", "cache"):
        raise WorkerProtocolError(field="reference.root")
    if not isinstance(path, str) or not path or len(path) > 4096 or "\\" in path or ":" in path or path.startswith("/"):
        raise WorkerProtocolError(field="reference.path")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise WorkerProtocolError(field="reference.path")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise WorkerProtocolError(field="reference.path")
    digest = value.get("sha256")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise WorkerProtocolError(field="reference.sha256")
    byte_count = value.get("bytes")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        raise WorkerProtocolError(field="reference.bytes")
    encoding = value.get("encoding")
    if encoding is not None and encoding != "utf-8":
        raise WorkerProtocolError(field="reference.encoding")
    media_type = value.get("media_type")
    if media_type is not None and media_type not in _MEDIA_TYPES:
        raise WorkerProtocolError(field="reference.media_type")
    return dict(value)


def sanitize_model_catalog(value: Any) -> list[dict[str, Any]]:
    """Return the account-specific public catalog or fail closed on malformed/duplicate entries."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise WorkerProtocolError("DENIED_MODEL_UNAVAILABLE", "models")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise WorkerProtocolError("DENIED_MODEL_UNAVAILABLE", f"models[{index}]")
        model_id = raw["value"] if "value" in raw else raw.get("id")
        display = raw["displayName"] if "displayName" in raw else raw.get("label")
        description = raw.get("description", "")
        efforts = raw["supportedEffortLevels"] if "supportedEffortLevels" in raw else raw.get("reasoning_efforts", [])
        if not isinstance(model_id, str) or not model_id or len(model_id) > 256 or model_id in seen:
            raise WorkerProtocolError("DENIED_MODEL_UNAVAILABLE", f"models[{index}].id")
        if not isinstance(display, str) or not display or len(display) > 256 or not isinstance(description, str):
            raise WorkerProtocolError("DENIED_MODEL_UNAVAILABLE", f"models[{index}]")
        if not isinstance(efforts, list) or any(item not in _EFFORTS for item in efforts) or len(set(efforts)) != len(efforts):
            raise WorkerProtocolError("DENIED_MODEL_UNAVAILABLE", f"models[{index}].reasoning_efforts")
        seen.add(model_id)
        result.append({
            "id": model_id,
            "label": display,
            "description": description[:1024],
            "reasoning_efforts": list(efforts),
            "supports_adaptive_thinking": raw.get("supportsAdaptiveThinking") is True,
            "supports_fast_mode": raw.get("supportsFastMode") is True,
        })
    if not result:
        raise WorkerProtocolError("DENIED_MODEL_UNAVAILABLE", "models")
    return result


def validate_profile(models: Sequence[Mapping[str, Any]], model: Any, effort: Any, max_turns: Any,
                     auth_mode: Any) -> dict[str, Any]:
    """Validate the immutable Claude conversation profile before records or provider calls."""

    if auth_mode != "subscription":
        raise WorkerProtocolError("DENIED_AUTH_MODE_MISMATCH", "auth_mode")
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or not (MAX_TURNS_MIN <= max_turns <= MAX_TURNS_MAX):
        raise WorkerProtocolError("MODEL_CALL_BUDGET_EXHAUSTED", "max_turns")
    selected = next((entry for entry in models if entry.get("id") == model), None)
    if selected is None:
        raise WorkerProtocolError("DENIED_MODEL_UNAVAILABLE", "model")
    allowed_efforts = selected.get("reasoning_efforts")
    if not isinstance(allowed_efforts, list):
        raise WorkerProtocolError("DENIED_EFFORT_UNSUPPORTED", "reasoning_effort")
    if (allowed_efforts and effort not in allowed_efforts) or (not allowed_efforts and effort is not None):
        raise WorkerProtocolError("DENIED_EFFORT_UNSUPPORTED", "reasoning_effort")
    return {
        "model": model,
        **({"reasoning_effort": effort} if effort is not None else {}),
        "max_turns": max_turns,
        "auth_mode": auth_mode,
    }


__all__ = [
    "CAPABILITY_PROBE_EVIDENCE", "CAPABILITY_PROBE_FEATURES", "CAPABILITY_PROBE_VERSION", "DENIAL_CODES",
    "HOST_OPERATIONS", "MAX_DELTA_BYTES", "MAX_FRAME_BYTES", "MAX_TURNS_DEFAULT", "MAX_TURNS_MAX",
    "MAX_TURNS_MIN", "MODEL_CATALOG_TTL_SECONDS", "PROTOCOL_VERSION", "TOOL_NAMES", "TURN_EVENTS",
    "WORKER_EVENTS", "WorkerProtocolError", "capability_probe_digest", "decode_frame", "encode_frame",
    "sanitize_model_catalog", "validate_capability_probe_result", "validate_frame", "validate_profile",
    "validate_reference",
]
