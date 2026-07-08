from __future__ import annotations

import json
import sys
from typing import Any


def emit(payload: dict[str, Any], *, as_json: bool = False) -> int:
    if as_json:
        # ensure_ascii=True: escape non-ASCII to \uXXXX so --json never crashes writing to a
        # non-UTF-8 stream (Windows cp1252 console/pipe). Still valid JSON; parsers decode it back.
        print(json.dumps(payload, indent=2, ensure_ascii=True))
        return 0
    status = payload.get("status", "OK")
    message = payload.get("message")
    record_id = payload.get("id") or payload.get("record_id")
    if message:
        print(message)
    elif record_id:
        print(f"{status}: {record_id}")
    else:
        print(status)
    return 0


def emit_error(payload: dict[str, Any], *, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=True), file=sys.stderr)
    else:
        code = payload.get("code", "ERROR")
        action = payload.get("required_action")
        detail = payload.get("message") or payload.get("reason") or payload.get("field")
        print(f"{code}: {detail}", file=sys.stderr)
        if action:
            print(f"required_action: {action}", file=sys.stderr)
    return int(payload.get("exit_code", 1))
