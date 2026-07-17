"""Shared helpers for the Agent Kaizen test suite.

Every test runs the real CLI (``kaizen.py``) as a subprocess against an isolated data plane: ``KAIZEN_REPO_ROOT`` points at a fresh temporary directory, so tests never read or write the project's real ``AI/db``.

Run ``tests/run_tests.py`` with the shared Kaizen virtual environment. The no-argument runner selects the fast core lane; raw unittest discovery bypasses lane safety and is never a default gate.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
KAIZEN = REPO_ROOT / "kaizen.py"
ARGS_PY = REPO_ROOT / "kaizen_components" / "args.py"
README = REPO_ROOT / "README.md"


def run(repo_root: Path, *args: str, env: dict[str, str] | None = None,
        timeout: float | None = 120.0) -> subprocess.CompletedProcess:
    """Run kaizen.py in an isolated plane with UTF-8 decoding and a bounded default timeout."""
    full_env = dict(os.environ)
    full_env["KAIZEN_REPO_ROOT"] = str(repo_root)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(KAIZEN), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        env=full_env,
        timeout=timeout,
    )


def kaizen(repo_root: Path, *args: str, env: dict[str, str] | None = None,
           timeout: float | None = 120.0) -> tuple[int, dict[str, Any]]:
    """Run an op with --json; return (returncode, parsed payload).

    Success JSON is on stdout; DENIED/ERROR JSON is on stderr, so both are considered. A decode failure returns the raw streams under _unparsed_stdout and _unparsed_stderr.
    """
    proc = run(repo_root, *args, "--json", env=env, timeout=timeout)
    raw = proc.stdout.strip() or proc.stderr.strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"_unparsed_stdout": proc.stdout, "_unparsed_stderr": proc.stderr}
    return proc.returncode, payload


def alias_codes() -> list[str]:
    """Parse the operation codes out of the ALIASES dict in args.py (no import)."""
    text = ARGS_PY.read_text(encoding="utf-8")
    if "ALIASES = {" not in text:
        raise AssertionError("args.py no longer contains the ALIASES mapping marker")
    block = text.split("ALIASES = {", 1)[1].split("\n}", 1)[0]
    return re.findall(r'^\s*"([A-Z]+\d+)":', block, re.MULTILINE)


def readme_codes() -> list[str]:
    """Parse the operation codes out of the README command-index table."""
    text = README.read_text(encoding="utf-8")
    return re.findall(r"^\|\s*`([A-Z]+\d+)`", text, re.MULTILINE)


def readme_purposes() -> dict[str, str]:
    """Parse code -> Purpose cell out of the README command-index table."""
    text = README.read_text(encoding="utf-8")
    rows = re.findall(r"^\|\s*`([A-Z]+\d+)`\s*\|\s*`[^`]+`\s*\|\s*([^|]+)\|", text, re.MULTILINE)
    return {code: purpose.strip() for code, purpose in rows}


def registry_purposes() -> dict[str, str]:
    """Parse code -> purpose out of the REGISTRY dict in args.py (no import)."""
    text = ARGS_PY.read_text(encoding="utf-8")
    marker = "REGISTRY: dict[str, tuple[str, tuple[str, ...]]] = {"
    if marker not in text:
        raise AssertionError("args.py no longer contains the REGISTRY mapping marker")
    block = text.split(marker, 1)[1].split("\n}", 1)[0]
    return dict(re.findall(r'^\s*"([A-Z]+\d+)": \("([^"]+)"', block, re.MULTILINE))


class IsolatedDBTest(unittest.TestCase):
    """Base class: a fresh temp KAIZEN_REPO_ROOT per test, initialized with K1."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        rc, payload = kaizen(self.root, "K1")
        self.assertEqual(rc, 0, f"K1 init failed: {payload}")
        self.k1_payload = payload

    def kz(self, *args: str, env: dict[str, str] | None = None,
           timeout: float | None = 120.0) -> tuple[int, dict[str, Any]]:
        """Convenience wrapper = kaizen(self.root, ...); returns (returncode, payload) bound to this test's isolated temp root."""
        return kaizen(self.root, *args, env=env, timeout=timeout)
