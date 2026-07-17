"""Provide deterministic content hashes and text-field validation gates."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .denials import KaizenDenied


DEFAULT_WORD_LIMIT = 1000


def utc_text_hash(payload: dict[str, Any]) -> str:
    """Canonical-JSON (sort_keys, compact, UTF-8) SHA-256 of payload dict; deterministic. "utc_" prefix carries no time semantics."""
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    """Streaming 1 MiB-chunk SHA-256 of file at path; returns hex digest."""
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def word_count(text: str) -> int:
    """Count whitespace-delimited tokens; return zero for empty input."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return len(text.split())


def sentence_count(text: str) -> int:
    """Heuristic count: split on [.!?]+trailing-whitespace after collapsing; min 1 for non-empty. Assumes a space follows each terminator (else under-counts)."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    stripped = " ".join(text.split())
    if not stripped:
        return 0
    parts = [p for p in re.split(r"(?<=[.!?])\s+", stripped) if p]
    return max(1, len(parts))


def validate_summary(summary: str, *, required: bool = True) -> None:
    """Raises KaizenDenied if required-and-empty (SUMMARY_REQUIRED), >2 sentences (SUMMARY_TOO_LONG), or >80 words; else None."""
    if not summary and required:
        raise KaizenDenied(
            "DENIED_SUMMARY_REQUIRED",
            {"field": "summary", "required_action": "resubmit with a 1-2 sentence summary"},
            exit_code=2,
        )
    if summary:
        count = sentence_count(summary)
        if count > 2:
            raise KaizenDenied(
                "DENIED_SUMMARY_TOO_LONG",
                {
                    "field": "summary",
                    "sentences": count,
                    "limit": 2,
                    "required_action": "resubmit with a 1-2 sentence summary",
                },
                exit_code=2,
            )
        validate_word_limit("summary", summary, limit=80)


def validate_word_limit(field: str, value: str, *, limit: int = DEFAULT_WORD_LIMIT) -> None:
    """Deny over-limit atomic fields; suggest child splitting only for body/text fields over 130%."""
    words = word_count(value or "")
    if words <= limit:
        return
    split_threshold = int(limit * 1.3)
    splittable = field == "body" or field == "text" or field.endswith("_body")
    if words <= split_threshold or not splittable:
        raise KaizenDenied(
            "DENIED_FIELD_WORD_LIMIT",
            {
                "field": field,
                "words": words,
                "limit": limit,
                "required_action": f"resubmit a shorter {field} under {limit} words",
            },
            exit_code=2,
        )
    chunks = math.ceil(words / limit)
    raise KaizenDenied(
        "DENIED_FIELD_SPLIT_REQUIRED",
        {
            "field": field,
            "words": words,
            "limit": limit,
            "required_action": f"split into {chunks} child entries",
        },
        exit_code=2,
    )


def validate_text_fields(fields: dict[str, str], *, summary_required: bool = True) -> None:
    """Validates summary (via validate_summary) then every other field at DEFAULT_WORD_LIMIT; summary_required toggles the empty-summary gate."""
    validate_summary(fields.get("summary", ""), required=summary_required)
    for name, value in fields.items():
        if name == "summary":
            continue
        validate_word_limit(name, value or "")
