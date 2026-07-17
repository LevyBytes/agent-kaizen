"""Per-node identity + deterministic project id + node-tagged coord ids (v8 M9, plan §B.1).

Machine-local, sync-excluded identity: ``node_identity.json`` lives in gitignored ``AI/db`` and is
sync-excluded BY CONSTRUCTION (only ``fleet.db`` ever syncs). ``project_id`` is a deterministic
sha256-hex-16 derivation with a strict source precedence so every mirror of one project resolves the
SAME id. ``coord_id`` folds the node id into the PK so LWW sync only ever merges DISTINCT rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .. import TOOL_VERSION
from ..paths import NODE_IDENTITY_PATH, REPO_ROOT
from . import net


def _utcstamp() -> str:
    """Second-resolution compact UTC stamp for coord_id; NOT unique alone (uuid4 tail provides uniqueness)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _sha16(text: str) -> str:
    """Sha256 hex truncated to 16 chars; shared project/marker hashing primitive."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _read_identity() -> dict | None:
    """Return a valid persisted identity, or None for an absent/corrupt file."""
    try:
        data = json.loads(NODE_IDENTITY_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("node_id"), str) and data["node_id"] else None


def _atomic_write_identity(identity: dict) -> None:
    """Atomically replace the identity file with a flushed same-directory temporary file."""
    NODE_IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=NODE_IDENTITY_PATH.name + ".", suffix=".tmp", dir=NODE_IDENTITY_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(identity, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, NODE_IDENTITY_PATH)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


@contextmanager
def _identity_lock():
    """Serialize identity read-modify-write operations across local processes."""
    NODE_IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = NODE_IDENTITY_PATH.with_name(NODE_IDENTITY_PATH.name + ".lock")
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415 -- platform-specific stdlib

            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # type: ignore[import-not-found]  # noqa: PLC0415 -- platform-specific stdlib

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_or_mint_locked() -> dict:
    """Load a valid identity or atomically mint one while the identity lock is held."""
    persisted = _read_identity()
    if persisted is not None:
        return persisted
    identity = {
        "node_id": "n" + uuid.uuid4().hex[:16],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "minted_by_tool_version": TOOL_VERSION,
    }
    _atomic_write_identity(identity)
    return identity


def load_or_mint_node_identity() -> dict:
    """Read ``node_identity.json``; mint + persist it on first use.

    Minted shape: ``{node_id: "n"+16hex, created_at, minted_by_tool_version}``. Machine-local and
    sync-excluded (only fleet.db syncs), so the node_id never leaves this node except folded into
    coord_event PKs. A corrupt/partial file is re-minted (identity is disposable and re-derivable)."""
    with _identity_lock():
        return _load_or_mint_locked()


def node_id() -> str:
    """The stable node id (mint-once, reload-thereafter)."""
    return load_or_mint_node_identity()["node_id"]


def ensure_signing_identity() -> dict:
    """Load the node identity and ensure it carries an Ed25519 signing key (v8 M11, plan §C.2).

    On first call for a node WITHOUT ``ed25519_seed_hex`` it mints a fresh
    ``nacl.signing.SigningKey`` and persists ``ed25519_seed_hex`` + ``ed25519_pub_hex`` into
    ``node_identity.json``, PRESERVING every existing field (same LF-newline, indent-2 style as the
    mint path). The SEED is a machine-local secret: node_identity.json lives in gitignored AI/db and is
    sync-excluded BY CONSTRUCTION (only fleet.db ever syncs), so the seed NEVER leaves this node -- only
    the public half rides out via nodes.pubkey. The seed is never logged or printed."""
    with _identity_lock():
        identity = _load_or_mint_locked()
        if identity.get("ed25519_seed_hex"):
            return identity
        from nacl import signing  # noqa: PLC0415 -- lazy: keep PyNaCl off the no-signing/plain-identity path

        key = signing.SigningKey.generate()
        identity["ed25519_seed_hex"] = key.encode().hex()
        identity["ed25519_pub_hex"] = key.verify_key.encode().hex()
        _atomic_write_identity(identity)
        return identity


def signing_key_from(identity: dict):
    """The ``nacl.signing.SigningKey`` for an identity carrying ``ed25519_seed_hex``, else None (a
    node with no minted seed -- e.g. an injected test node -- signs nothing). Lazy PyNaCl import."""
    seed_hex = identity.get("ed25519_seed_hex") if isinstance(identity, dict) else None
    if not seed_hex:
        return None
    from nacl import signing  # noqa: PLC0415 -- lazy: only the signing path loads PyNaCl

    return signing.SigningKey(bytes.fromhex(seed_hex))


def _git(*args: str) -> str | None:
    """Run ``git <args>`` in REPO_ROOT; return stripped stdout, or None on any failure.

    git is optional: an absent binary, a non-repo cwd, or a non-zero exit all fall through so the
    next project_id source is tried (deterministic per clone even with no git)."""
    exe = shutil.which("git")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def _normalize_remote(url: str) -> str:
    """Lowercase and remove trailing ``.git``/slashes without alias or transport canonicalization."""
    normalized = url.strip().lower()
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized.rstrip("/")


def project_id() -> dict:
    """Deterministic ``{project_id, source}`` (sha256-hex-16). First hit wins:

    1. committed ``AI/project.id`` file content (stripped) -- the explicit project marker;
    2. normalized ``git remote.origin.url`` (lowercase, strip trailing ``.git`` / ``/``);
    3. root commit sha (``git rev-list --max-parents=0 HEAD`` first line);
    4. fallback: resolved REPO_ROOT path string (still deterministic PER CLONE; source ``repo-path``).

    Sources 2-3 use git via subprocess and fall through silently when git is unavailable, so a gitless
    scratch tree resolves via source 4 (never raises)."""
    marker = REPO_ROOT / "AI" / "project.id"
    if marker.is_file():
        try:
            content = marker.read_text(encoding="utf-8").strip()
        except OSError:
            content = ""
        if content:
            return {"project_id": _sha16(content), "source": "project-id-file"}

    remote = _git("config", "--get", "remote.origin.url")
    if remote:
        return {"project_id": _sha16(_normalize_remote(remote)), "source": "git-remote"}

    root_commit = _git("rev-list", "--max-parents=0", "HEAD")
    if root_commit:
        first = sorted(line.strip() for line in root_commit.splitlines() if line.strip())[0]
        if first:
            return {"project_id": _sha16(first), "source": "root-commit"}

    return {"project_id": _sha16(str(REPO_ROOT.resolve())), "source": "repo-path"}


def coord_id(prefix: str, node_id_value: str) -> str:
    """Node-tagged, globally-unique PK for a coord_events row.

    Shape ``{prefix}_{utcstamp}_{node_id[-6:]}{uuid4hex}``: folding the node id into the PK means LWW
    sync only ever MERGES distinct rows (two nodes can never collide on a PK). The tag is a locality
    hint only; global uniqueness comes from the full UUID tail even when a caller supplies a short id."""
    tag = (node_id_value or "")[-6:]
    return f"{prefix}_{_utcstamp()}_{tag}{uuid.uuid4().hex}"


def node_display_name() -> str:
    """A human-facing node name that is NEVER a raw IP.

    Precedence: ``KAIZEN_NODE_NAME`` env override -> tailnet MagicDNS self name (when on the tailnet)
    -> ``platform.node()`` hostname. All three are names, so a coord/nodes row never leaks an IP."""
    override = os.environ.get("KAIZEN_NODE_NAME")
    if override and override.strip():
        return override.strip()
    tailnet_name = net.tailnet_self()
    if tailnet_name:
        return tailnet_name
    return platform.node() or "unknown-node"
