"""Distribution and session mode seams (v8 M0).

Config seams only: nothing in the shipped data plane consumes these values yet,
so ``KAIZEN_DIST_MODE=off`` (the default) leaves every existing code path
byte-identical. Runtime consumers arrive at M1+ (supervisor) and M9+ (fleet).
"""

from __future__ import annotations

import os

from ..denials import KaizenDenied

DIST_MODES: tuple[str, ...] = ("off", "observe", "active")
CONTROLLERS: tuple[str, ...] = ("observed", "kaizen")
# "hooked" is reserved for the deferred M-CLAUDE milestone (governor over official
# Claude Code extension sessions). It parses so config and records can carry the
# value, but no runtime handler exists before M-CLAUDE.
SESSION_MODES: tuple[str, ...] = ("observe", "hooked", "orchestrate", "strict")

DIST_MODE_ENV = "KAIZEN_DIST_MODE"


def _parse(value: str | None, allowed: tuple[str, ...], field: str, default: str) -> str:
    if value is not None and not isinstance(value, str):
        raise KaizenDenied(
            "DENIED_MODE_INVALID",
            {
                "field": field,
                "value": repr(value),
                "allowed": list(allowed),
                "required_action": f"set {field} to one of: {'|'.join(allowed)}",
            },
            exit_code=2,
        )
    raw = (value if value is not None else default).strip().lower()
    if raw not in allowed:
        raise KaizenDenied(
            "DENIED_MODE_INVALID",
            {
                "field": field,
                "value": raw,
                "allowed": list(allowed),
                "required_action": f"set {field} to one of: {'|'.join(allowed)}",
            },
            exit_code=2,
        )
    return raw


def parse_dist_mode(value: str | None) -> str:
    return _parse(value, DIST_MODES, "dist_mode", "off")


def parse_controller(value: str | None) -> str:
    return _parse(value, CONTROLLERS, "controller", "observed")


def parse_session_mode(value: str | None) -> str:
    return _parse(value, SESSION_MODES, "session_mode", "observe")


def dist_mode(env: dict[str, str] | None = None) -> str:
    """Read ``KAIZEN_DIST_MODE``; default ``off``. An invalid value denies loudly
    (a config typo silently falling back to ``off`` would mask a fleet misconfig)."""
    source = os.environ if env is None else env
    return parse_dist_mode(source.get(DIST_MODE_ENV))
