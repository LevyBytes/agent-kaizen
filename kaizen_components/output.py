"""Emit human or ASCII-safe JSON CLI success output to stdout and error output to stderr."""

from __future__ import annotations

import json
import sys
from typing import Any


def _safe_print(value: Any, *, file: Any = None) -> None:
    """Print human text with backslash escapes for characters the destination cannot encode."""
    stream = sys.stdout if file is None else file
    encoding = getattr(stream, "encoding", None) or "utf-8"
    text = str(value).encode(encoding, errors="backslashreplace").decode(encoding)
    print(text, file=stream)


def emit(payload: dict[str, Any], *, as_json: bool = False) -> int:
    """Renders a success payload — JSON verbatim, or human line (message > status:record_id > status); always returns 0 (callers may ignore); not for error payloads."""
    if as_json:
        # ensure_ascii=True: escape non-ASCII to \uXXXX so --json never crashes writing to a
        # non-UTF-8 stream (Windows cp1252 console/pipe). Still valid JSON; parsers decode it back.
        print(json.dumps(payload, indent=2, ensure_ascii=True))
        return 0
    status = payload.get("status", "OK")
    message = payload.get("message")
    record_id = payload.get("id") or payload.get("record_id")
    if message:
        _safe_print(message)
    elif record_id:
        _safe_print(f"{status}: {record_id}")
    else:
        _safe_print(status)
    return 0


def emit_error(payload: dict[str, Any], *, as_json: bool = False) -> int:
    """Renders an error payload to stderr — JSON verbatim, or "code: detail" + optional required_action; returns exit_code (default 1)."""
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=True), file=sys.stderr)
    else:
        code = payload.get("code", "ERROR")
        action = payload.get("required_action")
        detail = payload.get("message") or payload.get("reason") or payload.get("field")
        _safe_print(f"{code}: {detail}", file=sys.stderr)
        if action:
            _safe_print(f"required_action: {action}", file=sys.stderr)
    exit_code = payload.get("exit_code", 1)
    return exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else 1
