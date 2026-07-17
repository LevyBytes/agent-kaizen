"""turso.sync connection wrapper + transaction guards (v8 M9, plan §3.2, ledger #24 / #25).

P-C probe facts (pyturso==0.6.1) this module encodes:
- sync connect signature = ``turso.sync.connect(path, remote_url=..., auth_token=..., bootstrap_if_empty=True)``
  exposing ``push()/pull()/checkpoint()/stats()`` (+ ``commit/rollback/execute/cursor/close``);
- **ONE ordering rule: never run a sync mutation while a local write tx is open** -- always commit/rollback
  first. Pull, push, and checkpoint assert ``not in_tx`` and raise
  ``DENIED_SYNC_IN_TX`` otherwise, and the store flips ``in_tx`` only inside its own tx blocks;
- ``stats().revision`` is per-handle, never a global epoch (surfaced via loopback only, never treated
  as a fleet-wide clock);
- the auth token is read from the caller/env TRANSIENTLY and is NEVER persisted to any row (it is only
  ever passed to ``connect``; no code path writes it to fleet.db).

``turso`` (and ``turso.sync``) are imported LAZILY inside :func:`open_connection` so a plain (no sync_url)
FleetStore -- and every hermetic test -- never touches the sync path.
"""

from __future__ import annotations

import os
from typing import Any

from ..denials import KaizenDenied

SYNC_URL_ENV = "KAIZEN_DB_SYNC_URL"
SYNC_AUTH_TOKEN_ENV = "KAIZEN_DB_SYNC_AUTH_TOKEN"


def sync_url_from_env() -> str | None:
    value = os.environ.get(SYNC_URL_ENV)
    return value.strip() if value and value.strip() else None


def sync_auth_token_from_env() -> str | None:
    """The sync auth token from env, used TRANSIENTLY (passed to connect, never stored)."""
    value = os.environ.get(SYNC_AUTH_TOKEN_ENV)
    return value.strip() if value and value.strip() else None


def open_connection(db_path: str, sync_url: str | None, auth_token: str | None):
    """Open the fleet.db handle: ``turso.sync.connect`` when a ``sync_url`` is configured, else a plain
    ``turso.connect`` (local-only mode). Returns ``(connection, synced: bool)``.

    ``auth_token`` is passed straight to ``connect`` and NOWHERE else -- it is never returned, logged,
    or written to a row. ``turso`` is imported here (not at module top) so the no-sync path never loads
    the sync submodule."""
    import turso  # noqa: PLC0415 -- lazy: keep the no-sync/hermetic path off the sync module

    if sync_url:
        import turso.sync  # noqa: PLC0415

        conn = turso.sync.connect(
            db_path,
            remote_url=sync_url,
            auth_token=auth_token,
            bootstrap_if_empty=True,
        )
        return conn, True
    conn = turso.connect(db_path)
    return conn, False


class SyncHandle:
    """Thin wrapper over a ``turso.sync`` connection exposing push/pull/checkpoint/stats, with the
    tx_guard the store relies on so sync mutations cannot run inside an open write tx.

    The store passes a ``in_tx_getter`` (a zero-arg callable returning its current tx state). Pull,
    push, and checkpoint assert ``not in_tx_getter()`` and raise ``DENIED_SYNC_IN_TX`` otherwise -- the ordering
    rule. All four sync ops are structured no-ops (never call the underlying connection) when the
    handle was opened without sync, so a caller can invoke them unconditionally."""

    def __init__(self, connection: Any, *, synced: bool, in_tx_getter: Any) -> None:
        self._conn = connection
        self._synced = bool(synced)
        self._in_tx = in_tx_getter

    @property
    def synced(self) -> bool:
        return self._synced

    def _assert_not_in_tx(self, op: str) -> None:
        """Refuse any sync operation while the shared connection has an open local transaction."""
        if self._in_tx():
            raise KaizenDenied(
                "DENIED_SYNC_IN_TX",
                {
                    "op": op,
                    "reason": "a local write tx is open; commit/rollback before syncing (ledger #24)",
                    "required_action": f"close the transaction before calling {op}()",
                },
                exit_code=2,
            )

    def pull(self) -> dict[str, Any]:
        """Pull remote coord_events. REFUSES when a write tx is open (the one ordering rule). No-op
        when sync is off. The caller MUST re-run the pure reducers after this (never cache across pull)."""
        self._assert_not_in_tx("pull")
        if not self._synced:
            return {"sync": "off"}
        self._conn.pull()
        return {"sync": "pulled"}

    def push(self) -> dict[str, Any]:
        """Push locally-appended coord_events to the hub. No-op when sync is off. Push-after-commit:
        the store calls this only after a commit, so no tx is open, but guard it for symmetry."""
        self._assert_not_in_tx("push")
        if not self._synced:
            return {"sync": "off"}
        self._conn.push()
        return {"sync": "pushed"}

    def checkpoint(self) -> dict[str, Any]:
        """Checkpoint the sync WAL (sidecar-growth control, ledger #13). No-op when sync is off."""
        self._assert_not_in_tx("checkpoint")
        if not self._synced:
            return {"sync": "off"}
        self._conn.checkpoint()
        return {"sync": "checkpointed"}

    def stats(self) -> dict[str, Any]:
        """Sync stats for loopback/heartbeat surfacing ONLY. ``revision`` is per-handle -- never a
        global epoch. No-op ``{sync: "off"}`` when sync is off."""
        if not self._synced:
            return {"sync": "off"}
        raw = self._conn.stats()
        # Normalize to a plain dict regardless of the native return shape.
        out: dict[str, Any] = {"sync": "on"}
        for attr in ("revision", "main_wal_size", "revert_wal_size", "last_pushed", "last_pulled"):
            if isinstance(raw, dict) and attr in raw:
                out[attr] = raw[attr]
            elif hasattr(raw, attr):
                out[attr] = getattr(raw, attr)
        return out
