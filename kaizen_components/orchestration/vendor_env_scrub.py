#!/usr/bin/env python3
"""Launch one trusted vendor hook/helper after removing inherited credentials.

Vendor CLIs receive the selected API key when API-key authentication is active. Their
hook and MCP subprocesses must not inherit that credential. This small stdlib launcher
is the trust boundary between the vendor process and Kaizen-owned helper code: it
removes every vendor credential and credential-file locator before spawning the real
helper, preserves stdio, and returns the helper's exact exit code.
"""

from __future__ import annotations

import os
import subprocess
import sys


SENSITIVE_ENV = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "KAIZEN_CODEX_API_KEY_FILE",
    "KAIZEN_CLAUDE_API_KEY_FILE",
)


def scrub_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy without vendor credentials or credential-file locators."""
    env = dict(os.environ if source is None else source)
    for name in SENSITIVE_ENV:
        env.pop(name, None)
    return env


def main(argv: list[str] | None = None) -> int:
    """Run a scrubbed child with inherited stdio.

    One leading ``--`` is stripped. Exit codes are 64 for no command, 70 for a spawn failure, the
    child's non-negative code, or ``128 + signal`` when a POSIX child terminates by signal.
    """
    command = list(sys.argv[1:] if argv is None else argv)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        return 64
    env = scrub_environment()
    # Drop secrets from this trusted launcher's mutable environment before the
    # untrusted helper starts. No command shell or string interpolation is involved.
    for name in SENSITIVE_ENV:
        os.environ.pop(name, None)
    try:
        process = subprocess.Popen(command, env=env)  # noqa: S603 -- argv is adapter-generated
        return_code = int(process.wait())
        return return_code if return_code >= 0 else 128 + abs(return_code)
    except (OSError, ValueError):
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
