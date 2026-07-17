"""Secret / personal-path scanning for records that capture activity.

Trace and evidence records must not durably store raw secrets or machine-specific
paths (see the README "private by default" rule). `assert_redacted` denies a write
whose text fields contain secret-like or personal-path content, steering callers
to store a hash or a chunk/source-lock reference instead.
"""

from __future__ import annotations

import re
from typing import Any

from .denials import KaizenDenied


_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # The OpenAI pattern explicitly excludes sk-ant-, so each provider reports under its own label.
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai_key", re.compile(r"\bsk-(?!ant-)(?:(?:proj|svcacct|admin)-)?[A-Za-z0-9_\-]{20,}\b")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")),
    ("stripe_key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("azure_account_key", re.compile(r"(?i)\bAccountKey=[A-Za-z0-9+/=]{40,}")),
    # Connection URLs with inline credentials (postgres://user:pass@host, mongodb+srv://...).
    ("db_url_credentials", re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?)://[^/\s:@]+:[^@\s]+@")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")),
    ("assigned_secret", re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|bearer)\b\s*[:=]\s*\S{6,}")),
    ("bearer_header", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}")),
    # Personal home paths: both separators (C:\Users\, C:/Users/) plus POSIX (/home/, /Users/).
    ("user_home_path", re.compile(r"(?i)[a-z]:[\\/]users[\\/](?!(?:public|default)\b)[^\\/\s\"']+")),
    ("posix_home_path", re.compile(r"(?<![A-Za-z0-9._/])/(?:home|Users)/[^/\s\"']+")),
]

# Email is scanned separately so non-personal addresses (project contact, doc placeholders)
# can be allowlisted rather than dropping the whole class.
_EMAIL_RE = re.compile(r"(?i)\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_EMAIL_ALLOW_SUFFIXES = ("@example.com", "@example.org", "@anthropic.com")
_EMAIL_ALLOW_LOCALPARTS = ("noreply",)


def _has_personal_email(text: str) -> bool:
    """Returns True iff some email in text falls outside the domain-suffix and local-part allowlists."""
    for match in _EMAIL_RE.finditer(text):
        addr = match.group(0).lower()
        if any(addr.endswith(suffix) for suffix in _EMAIL_ALLOW_SUFFIXES):
            continue
        if addr.split("@", 1)[0] in _EMAIL_ALLOW_LOCALPARTS:
            continue
        return True
    return False


def scan_for_secrets(text: str) -> list[str]:
    """Return matched secret-class labels (plus ``email``); reject non-string input."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    if not text:
        return []
    hits = [name for name, pattern in _SECRET_PATTERNS if pattern.search(text)]
    if _has_personal_email(text):
        hits.append("email")
    return hits


def assert_redacted(fields: dict[str, Any]) -> None:
    """Raise :class:`KaizenDenied` if any nested string field contains secret-like content."""

    def nested_strings(value: Any, seen: set[int]) -> list[str]:
        if isinstance(value, str):
            return [value]
        if not isinstance(value, (dict, list, tuple, set)):
            return []
        identity = id(value)
        if identity in seen:
            return []
        seen.add(identity)
        values = value.values() if isinstance(value, dict) else value
        return [text for item in values for text in nested_strings(item, seen)]

    hits: dict[str, list[str]] = {}
    for field, value in fields.items():
        matches = sorted({label for text in nested_strings(value, set()) for label in scan_for_secrets(text)})
        if matches:
            hits[field] = matches
    if hits:
        raise KaizenDenied(
            "DENIED_TRACE_REDACTION",
            {
                "fields": hits,
                "required_action": "remove secrets/personal paths; store a hash or a chunk/source-lock reference instead of raw content",
            },
            exit_code=2,
        )
