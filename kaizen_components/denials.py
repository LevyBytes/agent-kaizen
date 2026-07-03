from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class KaizenDenied(Exception):
    code: str
    fields: dict[str, Any]
    exit_code: int = 1

    def payload(self) -> dict[str, Any]:
        return {"status": "DENIED", "code": self.code, **self.fields}


def usage_denial(message: str, **fields: Any) -> KaizenDenied:
    return KaizenDenied(
        "DENIED_USAGE",
        {"message": message, "required_action": fields.pop("required_action", "run --help and resubmit"), **fields},
        exit_code=2,
    )


# Copy-paste remedy templates for the most common denials; {op} is the canonical operation.
# Codes without an entry pass through unchanged. Keyed so main() can enrich any denial payload
# with a working example instead of leaving the caller to reconstruct the command shape.
REMEDY_EXAMPLES: dict[str, str] = {
    "DENIED_TITLE_REQUIRED": 'python kaizen.py {op} --title "Short title" --summary "One sentence." --json',
    "DENIED_SUMMARY_REQUIRED": 'python kaizen.py {op} --summary "One sentence." --json',
    "DENIED_SUMMARY_TOO_LONG": 'python kaizen.py {op} --summary "One or two sentences only." --body "Move the detail here." --json',
    "DENIED_ID_REQUIRED": "python kaizen.py {op} --id RECORD_ID --json",
    "DENIED_QUERY_REQUIRED": 'python kaizen.py {op} --query "topic text" --json',
    "DENIED_JSON_INVALID": "python kaizen.py {op} --payload-json-file path/to/payload.json --json  (PowerShell 5.1 strips inline JSON quotes; every JSON flag has a *-file twin)",
    "DENIED_PAYLOAD_TYPE": 'python kaizen.py {op} --payload-json "{{\\"key\\":\\"value\\"}}" --json  (must be a JSON object)',
    "DENIED_PATH_REQUIRED": "python kaizen.py {op} --path path/inside/repo --json",
    "DENIED_FILE_NOT_FOUND": "python kaizen.py {op} --path an/existing/file --json",
    "DENIED_CONTRACT_REQUIRED": 'python kaizen.py {op} --contract "contract-name" --json',
    "DENIED_REVIEW_ID_REQUIRED": "python kaizen.py {op} --review-id IRL_REVIEW_ID --summary \"One sentence.\" --json",
    "DENIED_FIELD_WORD_LIMIT": 'python kaizen.py {op} --summary "One sentence." --body-file path/to/longer-text.md --json',
}


def remedy_example(code: str, operation: str | None) -> str | None:
    template = REMEDY_EXAMPLES.get(code)
    if not template:
        return None
    return template.replace("{op}", operation or "<operation>")
