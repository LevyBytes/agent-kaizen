#!/usr/bin/env python3
"""Check or install the pinned DEVROOT-local Claude SDK runtime.

``check`` and ``install`` emit one JSON object and return 0 when ready or 2
when denied. Tool discovery is restricted to DEVROOT; PATH is never consulted.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from kaizen_components.orchestration.claude_runtime import (  # noqa: E402
    ClaudeRuntimeError,
    RUNTIME_KIND,
    SDK_VERSION,
    WORKER_PROTOCOL,
    install_runtime,
    runtime_capability,
)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def _regular_local_tool(path: Path, devroot: Path, field: str, *, allow_symlink: bool = False) -> Path:
    """Require the exact DEVROOT-managed tool path without PATH discovery."""

    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(devroot)
        resolved_info = resolved.stat()
    except (OSError, ValueError) as error:
        raise ClaudeRuntimeError(field=field) from error
    symlink = stat.S_ISLNK(info.st_mode)
    reparse = bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    # Official POSIX Node archives expose bin/npm as an internal symlink to npm-cli.js. Permit only
    # that contained regular-file shape; Windows symlinks/junctions remain reparse-point denials.
    if (
        reparse
        or (symlink and not allow_symlink)
        or (not symlink and not stat.S_ISREG(info.st_mode))
        or not stat.S_ISREG(resolved_info.st_mode)
    ):
        raise ClaudeRuntimeError(field=field)
    return resolved


def _devroot_tools() -> tuple[Path, Path]:
    """Resolve exact DEVROOT-managed Node/npm tools without PATH fallback.

    Windows requires ``node/{node.exe,npm.cmd}``; POSIX requires
    ``node/bin/{node,npm}``. Missing, external, or unsafe paths are denied.
    """

    value = os.environ.get("DEVROOT", "").strip()
    if not value:
        raise ClaudeRuntimeError(field="devroot")
    try:
        devroot = Path(value).expanduser().resolve(strict=True)
    except OSError as error:
        raise ClaudeRuntimeError(field="devroot") from error
    node_root = devroot / "node"
    if os.name == "nt":
        node = node_root / "node.exe"
        npm = node_root / "npm.cmd"
    else:
        node = node_root / "bin" / "node"
        npm = node_root / "bin" / "npm"
    return (
        _regular_local_tool(node, devroot, "node_executable"),
        _regular_local_tool(npm, devroot, "npm_executable", allow_symlink=os.name != "nt"),
    )


def _check() -> int:
    try:
        capability = runtime_capability(REPO_ROOT)
        ready = capability.get("runtime_status") == "ready"
    except Exception:
        capability = {
            "runtime_kind": RUNTIME_KIND,
            "runtime_version": SDK_VERSION,
            "runtime_status": "unavailable",
            "worker_protocol": WORKER_PROTOCOL,
            "code": "DENIED_SDK_UNAVAILABLE",
        }
        ready = False
    _emit({
        "action": "check",
        "code": None if ready else capability.get("code", "DENIED_SDK_UNAVAILABLE"),
        "runtime_kind": capability.get("runtime_kind", RUNTIME_KIND),
        "runtime_status": capability.get("runtime_status", "unavailable"),
        "runtime_version": capability.get("runtime_version", SDK_VERSION),
        "status": "OK" if ready else "DENIED",
        "worker_protocol": capability.get("worker_protocol", WORKER_PROTOCOL),
    })
    return 0 if ready else 2


def _install() -> int:
    try:
        node, npm = _devroot_tools()
        result = install_runtime(REPO_ROOT, node_executable=node, npm_executable=npm)
        if result.get("status") != "ready":
            raise ClaudeRuntimeError(field="runtime_validation")
        _emit({
            "action": "install",
            "runtime_kind": RUNTIME_KIND,
            "runtime_status": "ready",
            "runtime_version": SDK_VERSION,
            "status": "OK",
            "warm": bool(result.get("warm")),
            "worker_protocol": WORKER_PROTOCOL,
        })
        return 0
    except ClaudeRuntimeError as error:
        _emit({
            "action": "install",
            "code": error.code,
            "field": error.field,
            "runtime_kind": RUNTIME_KIND,
            "runtime_status": "unavailable",
            "runtime_version": SDK_VERSION,
            "status": "DENIED",
            "worker_protocol": WORKER_PROTOCOL,
        })
        return 2
    except Exception:
        _emit({
            "action": "install",
            "code": "DENIED_SDK_UNAVAILABLE",
            "field": "runtime",
            "runtime_kind": RUNTIME_KIND,
            "runtime_status": "unavailable",
            "runtime_version": SDK_VERSION,
            "status": "DENIED",
            "worker_protocol": WORKER_PROTOCOL,
        })
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check or explicitly install the pinned DEVROOT-local Claude SDK runtime.",
    )
    parser.add_argument("action", choices=("check", "install"))
    args = parser.parse_args(argv)
    return _check() if args.action == "check" else _install()


if __name__ == "__main__":
    raise SystemExit(main())
