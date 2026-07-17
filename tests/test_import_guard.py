"""Record-handler modules stay pure: no process-control or network-client/server imports.

Allowlist = modules whose PURPOSE is process or network work: comfy_runtime,
comfy_mcp, model_monitor, backends/* (HTTP + socket probing), and the
orchestration/* + fleet/* planes (they own children and sockets by design).
Everything else in kaizen_components is a record handler and must not gain a
process/network capability silently."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from _harness import REPO_ROOT

BANNED_TOP_LEVEL = {"subprocess", "socket", "asyncio", "ftplib", "smtplib", "ssl", "multiprocessing"}
BANNED_DOTTED = {"http.server", "http.client", "urllib.request", "xmlrpc.client"}
ALLOWED_FILES = {"comfy_runtime.py", "comfy_mcp.py", "comfyui.py", "model_monitor.py"}
ALLOWED_PREFIXES = ("backends/", "orchestration/", "fleet/")


def _violations_in(path: Path, rel: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name.split(".")[0] in BANNED_TOP_LEVEL or name in BANNED_DOTTED:
                    found.append(f"{rel}: import {name}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module = node.module
            if module.split(".")[0] in BANNED_TOP_LEVEL or module in BANNED_DOTTED:
                found.append(f"{rel}: from {module} import ...")
            elif module == "http" and any(alias.name == "server" for alias in node.names):
                found.append(f"{rel}: from http import server")
            elif module == "urllib" and any(alias.name == "request" for alias in node.names):
                found.append(f"{rel}: from urllib import request")
    return found


class ImportGuardTest(unittest.TestCase):
    def test_record_handlers_ban_process_and_network_imports(self) -> None:
        pkg = Path(REPO_ROOT) / "kaizen_components"
        violations: list[str] = []
        for path in sorted(pkg.rglob("*.py")):
            rel = path.relative_to(pkg).as_posix()
            if path.name in ALLOWED_FILES or rel.startswith(ALLOWED_PREFIXES):
                continue
            violations.extend(_violations_in(path, rel))
        self.assertEqual(violations, [], "record handlers must stay process/network-free")

    def test_guard_actually_sees_the_allowlisted_capability(self) -> None:
        # Self-check: the guard's parser must FIND banned imports where they are known
        # to exist (backends/http_retry.py imports socket), proving the scan works.
        target = Path(REPO_ROOT) / "kaizen_components" / "backends" / "http_retry.py"
        self.assertTrue(_violations_in(target, "backends/http_retry.py"))


if __name__ == "__main__":
    unittest.main()
