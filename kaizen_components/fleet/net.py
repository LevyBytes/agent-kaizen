"""Read-only tailnet probes (v8 M0).

Local ``tailscale status --json`` only -- no network round-trip beyond the local
tailscaled socket. Absent binary, timeout, non-zero exit, or unparsable output
all degrade to pure-local ``(False, None)``; these probes never raise.
"""

from __future__ import annotations

import json
import shutil
import subprocess

_PROBE_TIMEOUT_SECONDS = 3.0


def tailnet_probe() -> tuple[bool, str | None]:
    """Return ``(on_tailnet, magicdns_self_name)``; ``(False, None)`` on any failure."""
    exe = shutil.which("tailscale")
    if not exe:
        return (False, None)
    try:
        proc = subprocess.run(
            [exe, "status", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return (False, None)
    if proc.returncode != 0:
        return (False, None)
    try:
        status = json.loads(proc.stdout)
    except ValueError:
        return (False, None)
    if not isinstance(status, dict):
        return (False, None)
    self_info = status.get("Self") or {}
    if not isinstance(self_info, dict):
        return (False, None)
    name = str(self_info.get("DNSName") or "").rstrip(".") or None
    online = status.get("BackendState") == "Running" and name is not None
    return (online, name)


def on_tailnet() -> bool:
    """Fail-closed accessor for tuple[0]: True only when tailscaled BackendState=="Running" and a self DNSName exists; False on any probe failure. (low sev — module docstring + self-documenting name/signature largely cover it)."""
    return tailnet_probe()[0]


def tailnet_self() -> str | None:
    """Accessor for tuple[1]: MagicDNS self name (trailing dot stripped) or None when off-tailnet/unavailable. (low sev — same mitigation)."""
    return tailnet_probe()[1]
