"""Pure helpers for the additive VS Code conversation protocol.

No DB, filesystem, adapter, or UI work belongs here.  The supervisor and record
plane share these validators so the frozen wire shapes cannot drift between
preflight and durable-body validation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from .redaction import scan_for_secrets


TITLE_MAX_CODEPOINTS = 80
SNIPPET_MAX_CODEPOINTS = 160
REFERENCE_TEXT_MAX = 128
WORKSPACE_PATH_MAX = 4096
IMAGE_MAX_ITEMS = 4
IMAGE_MAX_BYTES = 4 * 1024 * 1024
CONTEXT_MAX_ITEMS = 8
CONTEXT_MAX_BYTES = 256 * 1024
CONTEXT_MAX_TOTAL_BYTES = 1024 * 1024
RESUME_MAX_EXPIRED_ARTIFACTS = 8

FEATURE_NAMES: tuple[str, ...] = (
    "streaming",
    "image_attachments",
    "governed_context",
    "diff_snapshots",
    "writer_leasing",
)
RESUME_FIDELITIES = ("full", "reduced")
ARTIFACT_UNAVAILABLE_STATES = ("expired", "unavailable")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_REF_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_CACHE_SOURCE_ROOT = "ai/work/orchestration/ui-cache"


class SessionProtocolError(ValueError):
    """One stable structural denial from the additive session protocol."""

    def __init__(self, code: str, field: str) -> None:
        """Message `"{code}: {field}"`; exposes `.code`/`.field`. (class docstring at 45 mostly suffices)."""
        super().__init__(f"{code}: {field}")
        self.code = code
        self.field = field


def _canonical_text(value: Any, limit: int) -> str | None:
    """Non-str→None; collapse `str.split()` whitespace, truncate to `limit` code points, empty→None. (low priority, self-evident)."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())[:limit]
    return text or None


def canonical_title(value: Any) -> str | None:
    """Collapse Unicode whitespace and cap at 80 Python Unicode code points."""

    return _canonical_text(value, TITLE_MAX_CODEPOINTS)


def canonical_snippet(value: Any) -> str | None:
    """Collapse Unicode whitespace and cap at 160 Python Unicode code points."""

    return _canonical_text(value, SNIPPET_MAX_CODEPOINTS)


def parse_feature_flags(value: Any) -> dict[str, bool]:
    """Return the five direct feature booleans; only the JSON literal true enables."""

    source = value if isinstance(value, Mapping) else {}
    return {name: source.get(name) is True for name in FEATURE_NAMES}


def pack_session_policy_snapshot(snapshot_json: str, client_features: Any = None) -> str:
    """Parse a policy snapshot object and overwrite its client_features.diff_snapshots flag from the literal-true input while preserving other existing client feature keys and policy/hash fields."""

    try:
        payload = json.loads(snapshot_json)
    except (TypeError, ValueError) as error:
        raise ValueError("policy snapshot must be a JSON object") from error
    if not isinstance(payload, dict):
        raise ValueError("policy snapshot must be a JSON object")
    source = client_features if isinstance(client_features, Mapping) else {}
    existing = payload.get("client_features")
    merged = dict(existing) if isinstance(existing, Mapping) else {}
    merged["diff_snapshots"] = source.get("diff_snapshots") is True
    payload["client_features"] = merged
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def read_session_client_features(snapshot_json: Any) -> dict[str, bool]:
    """Read C1 client negotiation fail-closed; legacy/malformed snapshots are false."""

    try:
        payload = json.loads(snapshot_json) if isinstance(snapshot_json, str) and snapshot_json else {}
    except (TypeError, ValueError):
        payload = {}
    source = payload.get("client_features") if isinstance(payload, dict) else None
    return {"diff_snapshots": bool(isinstance(source, Mapping) and source.get("diff_snapshots") is True)}


def validate_resume_metadata(value: Any) -> dict[str, Any]:
    """Validate optional resumed-run profile/list metadata; absent legacy fields remain readable."""

    if not isinstance(value, Mapping):
        raise SessionProtocolError("DENIED_PROFILE_BODY", "body")
    fidelity_present = "resume_fidelity" in value
    omitted_present = "omitted_message_count" in value
    if not fidelity_present and not omitted_present:
        return {}
    if not fidelity_present or not omitted_present:
        raise SessionProtocolError("DENIED_PROFILE_BODY", "resume_fidelity")
    fidelity = value.get("resume_fidelity")
    omitted = value.get("omitted_message_count")
    if fidelity not in RESUME_FIDELITIES:
        raise SessionProtocolError("DENIED_PROFILE_BODY", "resume_fidelity")
    omitted = _integer(
        omitted, "omitted_message_count", "DENIED_PROFILE_BODY", minimum=0, maximum=None,
    )
    raw_artifacts = value.get("expired_artifacts", [])
    if not isinstance(raw_artifacts, list) or len(raw_artifacts) > RESUME_MAX_EXPIRED_ARTIFACTS:
        raise SessionProtocolError("DENIED_PROFILE_BODY", "expired_artifacts")
    artifacts: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_artifacts):
        field = f"expired_artifacts[{index}]"
        if not isinstance(raw, Mapping):
            raise SessionProtocolError("DENIED_PROFILE_BODY", field)
        required = {"kind", "sha256", "bytes", "availability"}
        keys = set(raw)
        if not required.issubset(keys) or not keys.issubset(required | {"name"}):
            raise SessionProtocolError("DENIED_PROFILE_BODY", field)
        kind = raw.get("kind")
        if kind not in ("image", "file", "selection"):
            raise SessionProtocolError("DENIED_PROFILE_BODY", f"{field}.kind")
        digest = raw.get("sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise SessionProtocolError("DENIED_PROFILE_BODY", f"{field}.sha256")
        byte_count = _integer(
            raw.get("bytes"), f"{field}.bytes", "DENIED_PROFILE_BODY",
            minimum=0, maximum=IMAGE_MAX_BYTES,
        )
        availability = raw.get("availability")
        if availability not in ARTIFACT_UNAVAILABLE_STATES:
            raise SessionProtocolError("DENIED_PROFILE_BODY", f"{field}.availability")
        name = _bounded_text(
            raw.get("name"), f"{field}.name", "DENIED_PROFILE_BODY",
            optional=True, redaction_safe=True,
        )
        artifact = {
            "kind": kind, "sha256": digest, "bytes": byte_count,
            "availability": availability,
        }
        if name is not None:
            artifact["name"] = name
        artifacts.append(artifact)
    result: dict[str, Any] = {
        "resume_fidelity": fidelity,
        "omitted_message_count": omitted,
    }
    if artifacts:
        result["expired_artifacts"] = artifacts
    return result


def read_resume_metadata(value: Any) -> dict[str, Any]:
    """Fail closed when legacy/corrupt stored profile metadata is exposed over loopback."""

    try:
        return validate_resume_metadata(value)
    except SessionProtocolError:
        return {}


def _bounded_text(
    value: Any,
    field: str,
    code: str,
    *,
    optional: bool = False,
    redaction_safe: bool = False,
) -> str | None:
    """1..REFERENCE_TEXT_MAX str; `optional=True`→None allowed; `redaction_safe=True` rejects secret-bearing text; raises `SessionProtocolError(code, field)`."""
    if optional and value is None:
        return None
    if not isinstance(value, str) or not (1 <= len(value) <= REFERENCE_TEXT_MAX):
        raise SessionProtocolError(code, field)
    if redaction_safe and scan_for_secrets(value):
        raise SessionProtocolError(code, field)
    return value


def _artifact_hash(reference: Any, field: str, code: str) -> str:
    """Extract 64-hex from `sha256:<hex>` ref or raise; returns bare hex."""
    match = _ARTIFACT_REF_RE.fullmatch(reference) if isinstance(reference, str) else None
    if match is None:
        raise SessionProtocolError(code, field)
    return match.group(1)


def _integer(value: Any, field: str, code: str, *, minimum: int, maximum: int | None) -> int:
    """Validate an integer range; ``maximum=None`` leaves the upper bound open."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SessionProtocolError(code, field)
    if value < minimum:
        raise SessionProtocolError(code, field)
    if maximum is not None and value > maximum:
        raise SessionProtocolError(code.replace("_INVALID", "_TOO_LARGE"), field)
    return value


def validate_image_refs(value: Any) -> list[dict[str, Any]]:
    """Validate and detach the exact ImageRef request array."""

    if value is None:
        return []
    if not isinstance(value, list):
        raise SessionProtocolError("DENIED_ATTACHMENT_INVALID", "attachments")
    if len(value) > IMAGE_MAX_ITEMS:
        raise SessionProtocolError("DENIED_ATTACHMENT_TOO_LARGE", "attachments")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = {"id", "kind", "artifact_ref", "sha256", "bytes", "media_type"}
    allowed = required | {"name"}
    for index, item in enumerate(value):
        field = f"attachments[{index}]"
        if not isinstance(item, Mapping) or set(item) - allowed or not required.issubset(item):
            raise SessionProtocolError("DENIED_ATTACHMENT_INVALID", field)
        item_id = _bounded_text(item.get("id"), f"{field}.id", "DENIED_ATTACHMENT_INVALID")
        if item_id in seen:
            raise SessionProtocolError("DENIED_ATTACHMENT_INVALID", f"{field}.id")
        seen.add(item_id)
        if item.get("kind") != "image":
            raise SessionProtocolError("DENIED_ATTACHMENT_INVALID", f"{field}.kind")
        artifact_hash = _artifact_hash(item.get("artifact_ref"), f"{field}.artifact_ref", "DENIED_ATTACHMENT_INVALID")
        sha256 = item.get("sha256")
        if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None or sha256 != artifact_hash:
            raise SessionProtocolError("DENIED_ATTACHMENT_INVALID", f"{field}.sha256")
        byte_count = _integer(
            item.get("bytes"), f"{field}.bytes", "DENIED_ATTACHMENT_INVALID",
            minimum=1, maximum=IMAGE_MAX_BYTES,
        )
        media_type = item.get("media_type")
        if media_type not in _IMAGE_MEDIA_TYPES:
            raise SessionProtocolError("DENIED_ATTACHMENT_INVALID", f"{field}.media_type")
        name = _bounded_text(
            item.get("name"), f"{field}.name", "DENIED_ATTACHMENT_INVALID",
            optional=True, redaction_safe=True,
        )
        clean: dict[str, Any] = {
            "id": item_id,
            "kind": "image",
            "artifact_ref": item["artifact_ref"],
            "sha256": sha256,
            "bytes": byte_count,
            "media_type": media_type,
        }
        if name is not None:
            clean["name"] = name
        result.append(clean)
    return result


def validate_workspace_relative_path(
    value: Any,
    field: str = "source_path",
    code: str = "DENIED_CONTEXT_INVALID",
) -> str:
    """Return one normalized non-cache workspace-relative ``/`` path."""
    if (
        not isinstance(value, str)
        or not (1 <= len(value) <= WORKSPACE_PATH_MAX)
        or "\x00" in value
        or "\\" in value
        or ":" in value
        or value.startswith("/")
    ):
        raise SessionProtocolError(code, field)
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SessionProtocolError(code, field)
    lowered = value.casefold()
    if lowered == _CACHE_SOURCE_ROOT or lowered.startswith(_CACHE_SOURCE_ROOT + "/"):
        raise SessionProtocolError(code, field)
    return value


def _position(value: Any, field: str) -> dict[str, int]:
    """Require exact `{line,character}` non-negative int (bool-rejecting) map; returns normalized dict."""
    if not isinstance(value, Mapping) or set(value) != {"line", "character"}:
        raise SessionProtocolError("DENIED_CONTEXT_INVALID", field)
    line = value.get("line")
    character = value.get("character")
    if isinstance(line, bool) or not isinstance(line, int) or line < 0:
        raise SessionProtocolError("DENIED_CONTEXT_INVALID", f"{field}.line")
    if isinstance(character, bool) or not isinstance(character, int) or character < 0:
        raise SessionProtocolError("DENIED_CONTEXT_INVALID", f"{field}.character")
    return {"line": line, "character": character}


def _validate_context_refs(value: Any, *, resolved_files: bool) -> list[dict[str, Any]]:
    """Validate bounded, unique file/selection refs and return their normalized wire form."""

    if value is None:
        return []
    if not isinstance(value, list):
        raise SessionProtocolError("DENIED_CONTEXT_INVALID", "context_refs")
    if len(value) > CONTEXT_MAX_ITEMS:
        raise SessionProtocolError("DENIED_CONTEXT_TOO_LARGE", "context_refs")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_bytes = 0
    for index, item in enumerate(value):
        field = f"context_refs[{index}]"
        if not isinstance(item, Mapping):
            raise SessionProtocolError("DENIED_CONTEXT_INVALID", field)
        kind = item.get("kind")
        common = {"id", "kind", "source_path"}
        if kind == "file":
            required = common | ({"sha256", "bytes", "encoding"} if resolved_files else set())
        elif kind == "selection":
            required = common | {"range", "snapshot_ref", "sha256", "bytes", "encoding"}
        else:
            raise SessionProtocolError("DENIED_CONTEXT_INVALID", field)
        if set(item) != required:
            raise SessionProtocolError("DENIED_CONTEXT_INVALID", field)
        item_id = _bounded_text(item.get("id"), f"{field}.id", "DENIED_CONTEXT_INVALID")
        if item_id in seen:
            raise SessionProtocolError("DENIED_CONTEXT_INVALID", f"{field}.id")
        seen.add(item_id)
        clean: dict[str, Any] = {
            "id": item_id,
            "kind": kind,
            "source_path": validate_workspace_relative_path(item.get("source_path"), f"{field}.source_path"),
        }
        if kind == "file" and resolved_files:
            sha256 = item.get("sha256")
            if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
                raise SessionProtocolError("DENIED_CONTEXT_INVALID", f"{field}.sha256")
            byte_count = _integer(
                item.get("bytes"), f"{field}.bytes", "DENIED_CONTEXT_INVALID",
                minimum=0, maximum=CONTEXT_MAX_BYTES,
            )
            if item.get("encoding") != "utf-8":
                raise SessionProtocolError("DENIED_CONTEXT_INVALID", f"{field}.encoding")
            total_bytes += byte_count
            clean.update({"sha256": sha256, "bytes": byte_count, "encoding": "utf-8"})
        elif kind == "selection":
            range_value = item.get("range")
            if not isinstance(range_value, Mapping) or set(range_value) != {"start", "end"}:
                raise SessionProtocolError("DENIED_CONTEXT_INVALID", f"{field}.range")
            start = _position(range_value.get("start"), f"{field}.range.start")
            end = _position(range_value.get("end"), f"{field}.range.end")
            # Empty selections are valid; only a strictly reversed end is rejected.
            if (end["line"], end["character"]) < (start["line"], start["character"]):
                raise SessionProtocolError("DENIED_CONTEXT_INVALID", f"{field}.range")
            artifact_hash = _artifact_hash(
                item.get("snapshot_ref"), f"{field}.snapshot_ref", "DENIED_CONTEXT_INVALID",
            )
            sha256 = item.get("sha256")
            if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None or sha256 != artifact_hash:
                raise SessionProtocolError("DENIED_CONTEXT_INVALID", f"{field}.sha256")
            byte_count = _integer(
                item.get("bytes"), f"{field}.bytes", "DENIED_CONTEXT_INVALID",
                minimum=0, maximum=CONTEXT_MAX_BYTES,
            )
            if item.get("encoding") != "utf-8":
                raise SessionProtocolError("DENIED_CONTEXT_INVALID", f"{field}.encoding")
            total_bytes += byte_count
            clean.update({
                "range": {"start": start, "end": end},
                "snapshot_ref": item["snapshot_ref"],
                "sha256": sha256,
                "bytes": byte_count,
                "encoding": "utf-8",
            })
        result.append(clean)
    if total_bytes > CONTEXT_MAX_TOTAL_BYTES:
        raise SessionProtocolError("DENIED_CONTEXT_TOO_LARGE", "context_refs")
    return result


def validate_context_refs(value: Any) -> list[dict[str, Any]]:
    """Validate unique unresolved file refs and exact selection snapshots with per-item/count/aggregate byte bounds; file refs omit durable sha256/bytes/encoding until materialization."""

    return _validate_context_refs(value, resolved_files=False)


def validate_durable_context_refs(value: Any) -> list[dict[str, Any]]:
    """Validate durable resolved context metadata used by chat-message event bodies."""

    return _validate_context_refs(value, resolved_files=True)
