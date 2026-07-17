"""Normalize bounded, content-free evidence for approved workspace application outcomes.

No evidence keys returns ``{}``; malformed, incomplete, or oversized input returns one explicit uncertainty
sentinel; valid input returns the strict evidence envelope. Every emitted row path is decoded, validated, and
re-encoded into canonical ``PATH_ENCODING`` printable ASCII rather than trusting caller-provided spelling.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


MAX_MISMATCHES = 128  # 64 renames can each expose a target and source mismatch.
MAX_PATH_CHARS = 1_024
MAX_EVENT_BYTES = 1 << 20
MAX_EVENT_RESERVE_BYTES = 4 * 1_024
MAX_EVIDENCE_BYTES = MAX_EVENT_BYTES - MAX_EVENT_RESERVE_BYTES
PATH_ENCODING = "tilde-codepoint-ascii-v1"

_ALLOWED_KEYS = frozenset({
    "path", "reason", "expected_exists", "actual_exists", "expected_sha256", "actual_sha256",
})
_STATE_KEYS = frozenset({"expected_exists", "actual_exists", "expected_sha256", "actual_sha256"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")
_HEX_ESCAPE = re.compile(r"[0-9a-f]{6}")
_UNCERTAIN_REASONS = frozenset({
    "apply_outcome_uncertain",
    "mismatch_evidence_invalid",
    "mismatch_evidence_missing",
    "mismatch_evidence_overflow",
    "post_apply_audit_unavailable",
    "staged_cleanup_unproven",
    "target_state_unreadable",
})

# Exact serialized upper bound for 128 full-state rows whose paths and reasons each take their maximum
# representation. Replacing the empty ``[]`` in the envelope adds N rows plus N-1 commas.
_WORST_ROW = {
    "path": "~10ffff" * MAX_PATH_CHARS,
    "reason": "r" * 64,
    "expected_exists": True,
    "actual_exists": True,
    "expected_sha256": "a" * 64,
    "actual_sha256": "b" * 64,
}
_WORST_ENVELOPE = {
    "partial_apply": True,
    "mismatches": [],
    "mismatch_count": MAX_MISMATCHES,
    "mismatch_evidence_complete": False,
    "mismatch_evidence_uncertain": True,
    "mismatch_path_encoding": PATH_ENCODING,
}
_json_size = lambda value: len(  # noqa: E731 -- compact one-use bound expression
    json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
)
WORST_CASE_EVIDENCE_BYTES = (
    _json_size(_WORST_ENVELOPE)
    + MAX_MISMATCHES * _json_size(_WORST_ROW)
    + MAX_MISMATCHES - 1
)
assert WORST_CASE_EVIDENCE_BYTES <= MAX_EVIDENCE_BYTES


def encode_mismatch_path(value: str) -> str:
    """Encode an exact path as printable, whitespace-free ASCII without base64.

    Common printable ASCII stays one byte. Tilde/backslash/space use two-byte escapes; every other
    codepoint uses ``~`` plus six lowercase hex digits, so the absolute expansion is seven bytes per
    input character. This bound keeps 128 maximum-length paths beneath the 1 MiB event envelope.
    """

    out: list[str] = []
    for character in value:
        if character == "~":
            out.append("~~")
        elif character == "\\":
            out.append("~b")
        elif character == " ":
            out.append("~s")
        elif 0x21 <= ord(character) <= 0x7E:
            out.append(character)
        else:
            out.append(f"~{ord(character):06x}")
    return "".join(out)


def decode_mismatch_path(value: str) -> str:
    """Reverse :func:`encode_mismatch_path`; reject malformed escape sequences."""

    out: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "~":
            if not 0x21 <= ord(character) <= 0x7E or character == "\\":
                raise ValueError("invalid encoded mismatch path")
            out.append(character)
            index += 1
            continue
        if index + 1 >= len(value):
            raise ValueError("invalid encoded mismatch path")
        marker = value[index + 1]
        if marker == "~":
            out.append("~")
            index += 2
            continue
        if marker == "b":
            out.append("\\")
            index += 2
            continue
        if marker == "s":
            out.append(" ")
            index += 2
            continue
        if index + 7 > len(value):
            raise ValueError("invalid encoded mismatch path")
        digits = value[index + 1:index + 7]
        if _HEX_ESCAPE.fullmatch(digits) is None:
            raise ValueError("invalid encoded mismatch path")
        codepoint = int(digits, 16)
        if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("invalid encoded mismatch path")
        out.append(chr(codepoint))
        index += 7
    decoded = "".join(out)
    if encode_mismatch_path(decoded) != value:
        raise ValueError("non-canonical encoded mismatch path")
    return decoded


def _relative_path(value: Any, *, encoded: bool) -> str:
    """Return canonical encoded relative-path evidence after traversal and platform checks."""
    if not isinstance(value, str):
        raise ValueError("mismatch path must be text")
    raw = decode_mismatch_path(value) if encoded else value
    if len(raw) > MAX_PATH_CHARS or "\x00" in raw \
            or any(0xD800 <= ord(character) <= 0xDFFF for character in raw):
        raise ValueError("mismatch path is invalid")
    if raw:
        # Validate both separator spellings identically without changing the exact path
        # re-emitted as evidence. This catches backslash-joined traversal on every host.
        relative = PurePosixPath(raw.replace("\\", "/"))
        if relative.is_absolute() or any(
            part in ("", ".", "..") or (os.name == "nt" and ":" in part)
            for part in relative.parts
        ):
            raise ValueError("mismatch path is invalid")
    return encode_mismatch_path(raw)


def _hash(value: Any, exists: bool) -> str | None:
    """Validate that existing states carry a digest and missing states do not."""
    if value is None:
        if exists:
            raise ValueError("existing mismatch state requires a hash")
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("mismatch hash is invalid")
    if not exists:
        raise ValueError("missing mismatch state cannot carry a hash")
    return value


def _row(value: Any, *, encoded: bool) -> dict[str, Any]:
    """Normalize one row with all-or-none state keys and coupled existence/hash fields."""
    if not isinstance(value, Mapping):
        raise ValueError("mismatch row must be an object")
    raw = dict(value)
    if set(raw) - _ALLOWED_KEYS or "path" not in raw:
        raise ValueError("mismatch row shape is invalid")
    state_keys = set(raw) & _STATE_KEYS
    if state_keys and state_keys != _STATE_KEYS:
        raise ValueError("mismatch state is incomplete")
    reason = raw.get("reason", "final_state_mismatch" if state_keys == _STATE_KEYS else None)
    if not isinstance(reason, str) or _REASON.fullmatch(reason) is None:
        raise ValueError("mismatch reason is invalid")
    clean: dict[str, Any] = {
        "path": _relative_path(raw["path"], encoded=encoded),
        "reason": reason,
    }
    if state_keys:
        expected_exists = raw["expected_exists"]
        actual_exists = raw["actual_exists"]
        if not isinstance(expected_exists, bool) or not isinstance(actual_exists, bool):
            raise ValueError("mismatch existence state is invalid")
        clean.update({
            "expected_exists": expected_exists,
            "actual_exists": actual_exists,
            "expected_sha256": _hash(raw["expected_sha256"], expected_exists),
            "actual_sha256": _hash(raw["actual_sha256"], actual_exists),
        })
    return clean


def _sentinel(reason: str, *, partial_apply: bool) -> dict[str, Any]:
    """Return the canonical encoded one-row empty-path uncertainty envelope."""
    return {
        "partial_apply": partial_apply,
        "mismatches": [{"path": "", "reason": reason}],
        "mismatch_count": 1,
        "mismatch_evidence_complete": False,
        "mismatch_evidence_uncertain": True,
        "mismatch_path_encoding": PATH_ENCODING,
    }


def normalize_apply_evidence(source: Mapping[str, Any]) -> dict[str, Any]:
    """Return normalized evidence, recomputing ``mismatch_count`` from rows.

    No evidence keys produce ``{}``; malformed, incomplete, or oversized input produces one
    explicit uncertainty sentinel. Strict-envelope paths are re-emitted as canonical
    ``PATH_ENCODING`` printable ASCII, and any caller-supplied count is ignored.
    """

    evidence_keys = {
        "partial_apply", "mismatches", "mismatch_count", "mismatch_evidence_complete",
        "mismatch_evidence_uncertain", "mismatch_path_encoding",
    }
    if not any(key in source for key in evidence_keys):
        return {}
    partial_apply = source.get("partial_apply", False)
    if not isinstance(partial_apply, bool):
        return _sentinel("mismatch_evidence_invalid", partial_apply=True)
    raw_rows = source.get("mismatches", [])
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes, bytearray)):
        return _sentinel("mismatch_evidence_invalid", partial_apply=partial_apply)
    if len(raw_rows) > MAX_MISMATCHES:
        return _sentinel("mismatch_evidence_overflow", partial_apply=partial_apply)
    encoded_marker = source.get("mismatch_path_encoding")
    if encoded_marker not in (None, PATH_ENCODING):
        return _sentinel("mismatch_evidence_invalid", partial_apply=partial_apply)
    try:
        rows = [_row(item, encoded=encoded_marker == PATH_ENCODING) for item in raw_rows]
    except (TypeError, ValueError):
        return _sentinel("mismatch_evidence_invalid", partial_apply=partial_apply)
    if partial_apply and not rows:
        return _sentinel("mismatch_evidence_missing", partial_apply=True)
    uncertain = any(not item["path"] or item["reason"] in _UNCERTAIN_REASONS for item in rows)
    result = {
        "partial_apply": partial_apply,
        "mismatches": rows,
        "mismatch_count": len(rows),
        "mismatch_evidence_complete": not uncertain,
        "mismatch_evidence_uncertain": uncertain,
        "mismatch_path_encoding": PATH_ENCODING,
    }
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_BYTES:
        return _sentinel("mismatch_evidence_overflow", partial_apply=partial_apply)
    return result


def staged_cleanup_paths(source: Mapping[str, Any]) -> tuple[str, ...]:
    """Return exact decoded recovery paths only from strict normalized cleanup evidence."""

    evidence = normalize_apply_evidence(source)
    if evidence.get("mismatch_path_encoding") != PATH_ENCODING:
        return ()
    rows = evidence.get("mismatches", [])
    # Every normalization sentinel has an empty path. Reject the whole envelope explicitly so a
    # future sentinel-reason rename cannot accidentally turn uncertainty into a cleanup target.
    if any(not row.get("path") for row in rows if isinstance(row, Mapping)):
        return ()
    paths: list[str] = []
    for row in rows:
        if row.get("reason") != "staged_cleanup_unproven" or not row.get("path"):
            continue
        # normalize_apply_evidence already decoded and canonically re-encoded every row.
        paths.append(decode_mismatch_path(str(row["path"])))
    return tuple(dict.fromkeys(paths))


__all__ = [
    "MAX_EVIDENCE_BYTES",
    "MAX_EVENT_BYTES",
    "MAX_MISMATCHES",
    "MAX_PATH_CHARS",
    "PATH_ENCODING",
    "WORST_CASE_EVIDENCE_BYTES",
    "decode_mismatch_path",
    "encode_mismatch_path",
    "normalize_apply_evidence",
    "staged_cleanup_paths",
]
