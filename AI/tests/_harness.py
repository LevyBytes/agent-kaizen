"""Shared helpers for the Agent Kaizen test suite.

Every test runs the real CLI (``kaizen.py``) as a subprocess against an
ISOLATED data plane: ``KAIZEN_REPO_ROOT`` is pointed at a fresh temp directory, so the
tests never read or write the project's real ``AI/db``. Tests invoke ``sys.executable``,
so run the suite with the project venv's Python (which has ``pyturso`` installed):

    .venv/Scripts/python.exe -m unittest discover -s AI/tests   # Windows
    ./.venv/bin/python -m unittest discover -s AI/tests         # POSIX
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

REPO_ROOT = Path(__file__).resolve().parents[2]
KAIZEN = REPO_ROOT / "kaizen.py"
ARGS_PY = REPO_ROOT / "kaizen_components" / "args.py"
README = REPO_ROOT / "README.md"


def run(repo_root: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run kaizen.py with KAIZEN_REPO_ROOT pinned to an isolated data plane."""
    full_env = dict(os.environ)
    full_env["KAIZEN_REPO_ROOT"] = str(repo_root)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(KAIZEN), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=full_env,
    )


def kaizen(repo_root: Path, *args: str, env: dict | None = None) -> tuple[int, dict]:
    """Run an op with --json; return (returncode, parsed payload).

    Success JSON is on stdout; DENIED/ERROR JSON is on stderr, so both are considered.
    """
    proc = run(repo_root, *args, "--json", env=env)
    raw = proc.stdout.strip() or proc.stderr.strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"_unparsed_stdout": proc.stdout, "_unparsed_stderr": proc.stderr}
    return proc.returncode, payload


def alias_codes() -> list[str]:
    """Parse the operation codes out of the ALIASES dict in args.py (no import)."""
    text = ARGS_PY.read_text(encoding="utf-8")
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
    block = text.split("REGISTRY: dict[str, tuple[str, tuple[str, ...]]] = {", 1)[1].split("\n}", 1)[0]
    return dict(re.findall(r'^\s*"([A-Z]+\d+)": \("([^"]+)"', block, re.MULTILINE))


class IsolatedDBTest(unittest.TestCase):
    """Base class: a fresh temp KAIZEN_REPO_ROOT per test, initialized with K1."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        rc, payload = kaizen(self.root, "K1")
        self.assertEqual(rc, 0, f"K1 init failed: {payload}")

    def kz(self, *args: str, env: dict | None = None) -> tuple[int, dict]:
        return kaizen(self.root, *args, env=env)
