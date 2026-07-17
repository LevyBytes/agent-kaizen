"""Coordination engine over a FleetStore (v8 M10b, plan §B.2 / §B.3 / §B.4, gate F3).

Enforcement ON (F3; M10a was shadow/F2). The epoch fence and the authoritative lease mutex now REFUSE
instead of merely RECORDING. Every action still APPENDS append-only coord_events through the store's
single write path (:meth:`FleetStore.append_coord_event` -- kind x marker + redaction + immediate
commit) or READS fresh reducer projections; nothing mutates a mutable row. What changed at M10b:

- **epoch fence (:func:`_fence_check`).** A stale ``assumed_epoch`` on a grantor action STILL appends
  the ``conflict/detected`` {would_block: DENIED_STALE_FENCE} audit event, THEN raises
  ``DENIED_STALE_FENCE`` (exit 2) -- a stale controller epoch is split-brain control, always wrong, in
  BOTH lease modes. ``assumed_epoch=None`` keeps meaning "no fence data provided" (no check).
- **authoritative lease mutex (:func:`grant_lease` / :func:`renew_lease`, ``mode=authoritative``).**
  Granting a scope whose reduced state is "held" refuses ``DENIED_LEASE_HELD`` (contention blocks;
  renewing the CURRENT holder's own grant is the renewal path, allowed). On a sync-configured store an
  authoritative grant/renew FIRST ``pull_and_reduce()``s; any pull exception ⇒ ``DENIED_LEASE_HUB_UNREACHABLE``
  (freshness under partition). Advisory keeps M10a behavior byte-for-byte (AP; never pulls, never
  contends).
- **contested claim under a pinned coordinator (:func:`claim_coordinator`).** A contested claim while
  the current coordinator's mode is "pinned" refuses ``DENIED_COORD_DIVERGED`` instead of recording;
  roaming keeps M10a record-and-return contested semantics.
- **isolated sentinel (§B.3).** ``claim_coordinator`` / ``transfer_coordinator`` with ``isolated=True``
  stamp {iso: true, iso_sentinel} on the payload (RECORD-level; reconcile is M15).

Grantor model (§B.2): the coordinator is the SOLE grantor. Lease REQUESTS are unordered append-only
(``lease/requested``, no epoch); GRANTS carry ``(scope_key, lease_epoch, grant_seq)`` where lease_epoch
= the coordinator's current epoch and grant_seq is grantor-local monotonic (single grantor => no gapless
cross-node sequence exists or is needed). Grantor-only actions refuse (KaizenDenied
``DENIED_NOT_COORDINATOR``, exit 2) unless ``store.node_id`` == the reduced coordinator holder;
holder-only ``release_lease`` refuses ``DENIED_NOT_HOLDER`` for a stranger.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..denials import KaizenDenied
from . import reducers

DEFAULT_TTL_S = 900

GitRunner = Callable[..., "subprocess.CompletedProcess[str]"]


# --- time helpers (ISO, aligned with db.now()) ---------------------------------------------------

def _now_iso() -> str:
    """Return the shared UTC ISO clock used for grant timestamps and expiry math."""
    from .. import db

    return db.now()


def _expires_at(now_iso: str, ttl_s: int) -> str:
    """now + ttl as a UTC ISO string that string-compares correctly against other db.now() values."""
    try:
        base = datetime.fromisoformat(now_iso)
    except (ValueError, TypeError):
        base = datetime.now(timezone.utc)
    return (base + timedelta(seconds=max(0, int(ttl_s)))).isoformat()


# --- fresh reducer wrappers (never cached) -------------------------------------------------------

def current_coordinator(store: Any) -> dict[str, Any]:
    """The fresh coordinator projection ``{holder, epoch, mode, id}`` (always re-reduced)."""
    return reducers.reduce_coordinator(store.coord_events())


def current_leases(store: Any, now: str | None = None) -> dict[str, dict[str, Any]]:
    """The fresh per-scope lease projection (expiry-aware when ``now`` is given; defaults to the
    store clock so a plain call reflects real-time expiry)."""
    if now is None:
        now = _now_iso()
    return reducers.reduce_lease(store.coord_events(), now=now)


def _max_epoch(store: Any) -> int:
    """Return the highest coordinator or lease epoch across coordination events."""
    return reducers.max_epoch(store.coord_events())


# --- epoch-fence ENFORCEMENT (M10b flips M10a's record-and-proceed to record-then-refuse) ---------

def _fence_check(store: Any, assumed_epoch: int | None, reduced_epoch: int, *, scope_key: str | None = None) -> None:
    """The epoch fence, ENFORCING (M10b). When ``assumed_epoch`` is provided and disagrees with the
    reduced current epoch, FIRST append the ``conflict/detected`` {would_block: DENIED_STALE_FENCE}
    event (the audit trail M10a already wrote), THEN raise ``DENIED_STALE_FENCE`` (exit 2). A stale
    controller epoch is split-brain control -- always wrong -- so this fires for EVERY grantor action
    (transfer/grant/renew/revoke) in BOTH lease modes. ``assumed_epoch=None`` = "no fence data
    provided" (no check), the pre-M10b default for callers that pass nothing.

    This is the exact seam M10b flipped: at M10a a mismatch RECORDED and returned True; now it records
    then denies."""
    if assumed_epoch is None or int(assumed_epoch) == int(reduced_epoch):
        return
    payload: dict[str, Any] = {
        "would_block": "DENIED_STALE_FENCE",
        "assumed_epoch": int(assumed_epoch),
        "current_epoch": int(reduced_epoch),
    }
    if scope_key:
        payload["scope_key"] = scope_key
    # Record the conflict for the audit trail BEFORE refusing (append-only commits immediately, so the
    # event survives the raise) -- a denied split-brain attempt is still ledger-visible.
    store.append_coord_event(
        "conflict",
        "detected",
        summary="Epoch-fence denied (stale assumed epoch); split-brain control refused.",
        scope_key=scope_key,
        payload=payload,
    )
    fields: dict[str, Any] = {
        "assumed_epoch": int(assumed_epoch),
        "current_epoch": int(reduced_epoch),
        "required_action": "re-read the current coordinator epoch (fleet digest) and retry; a stale controller epoch is split-brain",
    }
    if scope_key:
        fields["scope_key"] = scope_key
    raise KaizenDenied("DENIED_STALE_FENCE", fields, exit_code=2)


def _require_coordinator(store: Any) -> dict[str, Any]:
    """Grantor-only gate: refuse DENIED_NOT_COORDINATOR (exit 2) unless this node is the reduced
    coordinator holder. Returns the coordinator projection when allowed."""
    coord = current_coordinator(store)
    if coord.get("holder") != store.node_id:
        raise KaizenDenied(
            "DENIED_NOT_COORDINATOR",
            {
                "node_id": store.node_id,
                "current_holder": coord.get("holder"),
                "current_epoch": coord.get("epoch"),
                "required_action": "only the coordinator (sole grantor) may grant/renew/revoke/transfer; claim the role first",
            },
            exit_code=2,
        )
    return coord


# --- coordinator role ----------------------------------------------------------------------------

def _iso_sentinel(store: Any) -> str:
    """The §B.3 ``iso:`` epoch sentinel for an isolated claim/transfer -- a RECORD-level marker that
    this authority was minted without hub reconciliation (reconcile is M15). Node-tagged so two
    isolated nodes' sentinels differ."""
    return f"iso:{(store.node_id or '')[-6:]}"


def _assert_isolated(store: Any) -> None:
    """Verify a caller-asserted isolation on a SYNCED store by the probe ``reconcile.offline_status``
    uses (a pull): a SUCCESSFUL pull means the hub is reachable ⇒ ``DENIED_NOT_ISOLATED`` -- an iso
    claim asserts a partition and must not mint iso authority on a reachable fleet. Transport failures
    confirm the partition; structured denials from a reachable hub propagate. Unsynced stores skip."""
    if not bool(getattr(getattr(store, "sync", None), "synced", False)):
        return
    try:
        store.pull_and_reduce()
    except KaizenDenied:
        # A reachable hub may return a structured refusal (for example an epoch regression); that is not
        # evidence of a network partition and must remain visible to the caller.
        raise
    except Exception:  # noqa: BLE001 -- transport/sync failure confirms isolation
        return
    raise KaizenDenied(
        "DENIED_NOT_ISOLATED",
        {
            "node_id": store.node_id,
            "required_action": "the hub is reachable; claim without isolated=True (contested rules apply) or transfer the role",
        },
        exit_code=2,
    )


def _coordinator_payload(store: Any, mode: str, *, isolated: bool = False, to_node: str | None = None) -> dict[str, Any]:
    """Build a coordinator claimed/granted payload: {mode[, to_node][, iso, iso_sentinel]}. ``isolated``
    stamps the §B.3 iso: sentinel (RECORD-level)."""
    payload: dict[str, Any] = {"mode": mode}
    if to_node:
        payload["to_node"] = to_node
    if isolated:
        payload["iso"] = True
        payload["iso_sentinel"] = _iso_sentinel(store)
    return payload


def claim_coordinator(store: Any, *, mode: str = "roaming", isolated: bool = False, summary: str | None = None) -> dict[str, Any]:
    """Claim the coordinator role. On a VACANCY (no holder) => self-grant: append claimed + granted at
    epoch = max_epoch+1 (min 1) carrying ``payload.mode`` (the §B.3 serialized flow degenerates to a
    self-grant with no live holder). Re-claiming when THIS node already holds is a no-op re-affirm.

    CONTESTED (held by ANOTHER node) is mode-sensitive at M10b:
    - the current coordinator is ROAMING => M10a semantics: append claimed {epoch} + ``conflict/detected``
      {would_block: CONTESTED_CLAIM} and return ``{status: "RECORDED", contested: True}`` -- never
      displaces (granted stays authoritative), never raises;
    - the current coordinator is PINNED => split-brain control: refuse ``DENIED_COORD_DIVERGED`` (exit 2)
      -- a pinned holder is the single source of truth and must not be contested by a bare claim,
      ISOLATED OR NOT (a pin survives partitions; iso work under a pinned fleet = held scopes only).

    ``isolated=True`` stamps the §B.3 iso: sentinel {iso, iso_sentinel} on the claimed/granted payload
    (RECORD-level; reconcile is M15) and asserts a PARTITION: on a synced store the assertion is
    VERIFIED by a pull probe (hub reachable ⇒ ``DENIED_NOT_ISOLATED``; unsynced stores skip -- the
    pure-local plane). §B.3 ISOLATED FALLBACK (M15): a VERIFIED-isolated claim against a ROAMING holder
    ASSUMES the role -- claimed + granted under the iso sentinel at epoch max+1, PLUS the
    ``conflict/detected`` {would_block: CONTESTED_CLAIM, iso_assumed} audit record -- so a partitioned
    node can mint fresh iso scope grants (D9 reconcile surfaces the contest on reconnect; the winning
    coordinator projection carries {iso, iso_sentinel} for everyone to see)."""
    if isolated:
        _assert_isolated(store)
    coord = current_coordinator(store)
    holder = coord.get("holder")
    epoch = max(1, _max_epoch(store) + 1)
    payload_mode = mode if mode in ("roaming", "pinned") else "roaming"

    if holder is not None and holder != store.node_id:
        current_mode = coord.get("mode") or "roaming"
        if current_mode == "pinned":
            # A pinned coordinator is the single source of truth; a contested claim is divergent control.
            raise KaizenDenied(
                "DENIED_COORD_DIVERGED",
                {
                    "node_id": store.node_id,
                    "current_holder": holder,
                    "current_epoch": coord.get("epoch"),
                    "current_mode": "pinned",
                    "required_action": "transfer the role from the pinned holder, or have it unpin (claim roaming) first",
                },
                exit_code=2,
            )
        if isolated:
            # §B.3 isolated fallback: verified-isolated ⇒ ASSUME the role under the iso sentinel.
            claim_payload = _coordinator_payload(store, payload_mode, isolated=True)
            claimed = store.append_coord_event(
                "coordinator",
                "claimed",
                summary=summary or f"Isolated coordinator claim at epoch {epoch}.",
                epoch=epoch,
                payload=claim_payload,
            )
            granted = store.append_coord_event(
                "coordinator",
                "granted",
                summary=summary or f"Coordinator role assumed isolated at epoch {epoch}.",
                epoch=epoch,
                payload=claim_payload,
            )
            store.append_coord_event(
                "conflict",
                "detected",
                summary="Isolated claim assumed the coordinator role over an unreachable roaming holder.",
                payload={
                    "would_block": "CONTESTED_CLAIM",
                    "iso_assumed": True,
                    "current_holder": holder,
                    "current_epoch": coord.get("epoch"),
                    "claim_epoch": epoch,
                },
            )
            return {
                "status": "OK",
                "contested": True,
                "iso": True,
                "iso_sentinel": claim_payload["iso_sentinel"],
                "node_id": store.node_id,
                "holder": store.node_id,
                "epoch": epoch,
                "mode": payload_mode,
                "claimed_id": claimed["id"],
                "granted_id": granted["id"],
            }
        # Roaming: contested claim records the claim + the conflict, does not displace (M10a).
        claimed = store.append_coord_event(
            "coordinator",
            "claimed",
            summary=summary or f"Coordinator claim (contested) at epoch {epoch}.",
            epoch=epoch,
            payload=_coordinator_payload(store, payload_mode, isolated=isolated),
        )
        store.append_coord_event(
            "conflict",
            "detected",
            summary="Contested coordinator claim recorded; current holder unchanged (roaming).",
            payload={
                "would_block": "CONTESTED_CLAIM",
                "current_holder": holder,
                "current_epoch": coord.get("epoch"),
                "claim_epoch": epoch,
            },
        )
        return {
            "status": "RECORDED",
            "contested": True,
            "node_id": store.node_id,
            "current_holder": holder,
            "current_epoch": coord.get("epoch"),
            "claim_epoch": epoch,
            "claimed_id": claimed["id"],
        }

    # Vacancy (or self): serialized claim -> grant degenerates to a self-grant.
    claim_payload = _coordinator_payload(store, payload_mode, isolated=isolated)
    claimed = store.append_coord_event(
        "coordinator",
        "claimed",
        summary=summary or f"Coordinator claim at epoch {epoch}.",
        epoch=epoch,
        payload=claim_payload,
    )
    granted = store.append_coord_event(
        "coordinator",
        "granted",
        summary=summary or f"Coordinator self-granted at epoch {epoch}.",
        epoch=epoch,
        payload=claim_payload,
    )
    result = {
        "status": "OK",
        "contested": False,
        "node_id": store.node_id,
        "holder": store.node_id,
        "epoch": epoch,
        "mode": payload_mode,
        "claimed_id": claimed["id"],
        "granted_id": granted["id"],
    }
    if isolated:
        result["iso"] = True
        result["iso_sentinel"] = claim_payload["iso_sentinel"]
    return result


def transfer_coordinator(store: Any, to_node_id: str, *, isolated: bool = False, summary: str | None = None, assumed_epoch: int | None = None) -> dict[str, Any]:
    """GRANTOR-ONLY role transfer (the serialized detach->grant->epoch-bump of §B.3). Refuse
    DENIED_NOT_COORDINATOR unless this node is the holder. Appends ``coordinator/released`` {epoch=cur}
    then ``coordinator/granted`` at one above the ledger-wide max epoch, carrying
    ``payload{mode, to_node[, iso, iso_sentinel]}`` -- the
    granted's node_id is the APPENDING (old) node, so the reducer reads the new holder from
    ``payload.to_node``. Fence ENFORCED via ``assumed_epoch`` (M10b: a stale epoch records the conflict
    then refuses DENIED_STALE_FENCE). ``isolated=True`` stamps the §B.3 iso: sentinel on the granted
    payload."""
    coord = _require_coordinator(store)
    cur = int(coord.get("epoch") or 0)
    _fence_check(store, assumed_epoch, cur)
    next_epoch = max(cur, _max_epoch(store)) + 1
    mode = coord.get("mode") or "roaming"
    released = store.append_coord_event(
        "coordinator",
        "released",
        summary=summary or f"Coordinator role released at epoch {cur} for transfer.",
        epoch=cur,
        payload={"to_node": to_node_id},
    )
    grant_payload = _coordinator_payload(store, mode, isolated=isolated, to_node=to_node_id)
    granted = store.append_coord_event(
        "coordinator",
        "granted",
        summary=summary or f"Coordinator role transferred to {to_node_id} at epoch {next_epoch}.",
        epoch=next_epoch,
        payload=grant_payload,
    )
    result = {
        "status": "OK",
        "from_node": store.node_id,
        "to_node": to_node_id,
        "epoch": next_epoch,
        "mode": mode,
        "released_id": released["id"],
        "granted_id": granted["id"],
    }
    if isolated:
        result["iso"] = True
        result["iso_sentinel"] = grant_payload["iso_sentinel"]
    return result


def release_coordinator(store: Any, *, summary: str | None = None) -> dict[str, Any]:
    """HOLDER-ONLY: append ``coordinator/released`` {epoch=cur}. Refuse DENIED_NOT_COORDINATOR for a
    non-holder (only the holder can vacate its own role)."""
    coord = _require_coordinator(store)
    cur = int(coord.get("epoch") or 0)
    released = store.append_coord_event(
        "coordinator",
        "released",
        summary=summary or f"Coordinator role released at epoch {cur}.",
        epoch=cur,
    )
    return {"status": "OK", "node_id": store.node_id, "epoch": cur, "released_id": released["id"]}


# --- leases --------------------------------------------------------------------------------------

def request_lease(store: Any, scope_key: str, *, mode: str = "advisory", ttl_s: int = DEFAULT_TTL_S, summary: str | None = None, origin_node: str | None = None) -> dict[str, Any]:
    """Append ``lease/requested`` {scope_key, payload{mode, ttl_s}} -- UNORDERED, no epoch (any node may
    request; the coordinator grants). A request never confers a hold.

    ``origin_node`` (M11, default None = byte-identical pre-M11 behavior): when set, the requesting node
    is stamped into the payload as ``origin_node``. It may ONLY be passed by a caller that
    CRYPTOGRAPHICALLY verified the origin (the control_http Ed25519 envelope), so a remote peer's request
    is attributed to the peer even though the appender column is truthfully the receiving (daemon) node."""
    lease_mode = _normalize_lease_mode(mode)
    payload: dict[str, Any] = {"mode": lease_mode, "ttl_s": int(ttl_s)}
    if origin_node:
        payload["origin_node"] = origin_node
    event = store.append_coord_event(
        "lease",
        "requested",
        summary=summary or f"Lease requested for {scope_key} ({lease_mode}).",
        scope_key=scope_key,
        payload=payload,
    )
    return {"status": "OK", "scope_key": scope_key, "mode": lease_mode, "ttl_s": int(ttl_s), "id": event["id"]}


def _next_grant_seq(store: Any, scope_key: str, epoch: int) -> int:
    """1 + max grant_seq already granted for ``scope_key`` at ``epoch`` (grantor-local monotonic; the
    grantor is single by construction). Zero grants so far => 1."""
    highest = 0
    for event in store.coord_events():
        if event.get("event_kind") != "lease" or event.get("marker") != "granted":
            continue
        if event.get("scope_key") != scope_key:
            continue
        if reducers._epoch(event) != int(epoch):
            continue
        highest = max(highest, reducers._grant_seq(event))
    return highest + 1


def _normalize_lease_mode(mode: str) -> str:
    """Coerce mode to advisory or authoritative, defaulting to advisory."""
    return mode if mode in ("advisory", "authoritative") else "advisory"


def _authoritative_freshness(store: Any, scope_key: str) -> None:
    """Authoritative freshness gate (M10b, plan §B.6). On a SYNC-CONFIGURED store, an authoritative
    grant/renew must reflect the hub's latest state, so pull_and_reduce() FIRST; ANY pull exception ⇒
    DENIED_LEASE_HUB_UNREACHABLE (a partition means no authoritative mutation -- CP). Sync OFF
    (local/lab, no partition possible) ⇒ no pull needed. Advisory never calls this (AP: partition-tolerant)."""
    sync = getattr(store, "sync", None)
    if sync is None or not getattr(sync, "synced", False):
        return
    try:
        store.pull_and_reduce()
    except KaizenDenied:
        # A structured denial from the pull path (e.g. DENIED_SYNC_IN_TX, DENIED_EPOCH_REGRESSION) is a
        # real refusal in its own right -- surface it unchanged, do not mask it as UNREACHABLE.
        raise
    except Exception as error:  # noqa: BLE001 -- any transport/sync failure = the hub is unreachable
        raise KaizenDenied(
            "DENIED_LEASE_HUB_UNREACHABLE",
            {
                "scope_key": scope_key,
                "reason": str(error)[:180],
                "required_action": "reconnect to the hub and retry, or use advisory mode (AP) for local-only coordination",
            },
            exit_code=2,
        ) from error


def _assert_authoritative_free(store: Any, scope_key: str, *, allow_holder: str | None = None) -> None:
    """Authoritative contention gate (M10b, plan §B.2 CP mutex). When the scope's reduced state is
    "held", refuse DENIED_LEASE_HELD -- granting a held scope to ANY holder (including a re-grant) is
    contention. ``allow_holder`` (set on renew) exempts the current holder's OWN grant: renewing your
    own held lease is the renewal path, not contention."""
    active = current_leases(store).get(scope_key)
    if not active or active.get("state") != "held":
        return
    holder = active.get("holder")
    if allow_holder is not None and holder == allow_holder:
        return
    raise KaizenDenied(
        "DENIED_LEASE_HELD",
        {
            "scope_key": scope_key,
            "current_holder": holder,
            "epoch": active.get("epoch"),
            "grant_seq": active.get("grant_seq"),
            "required_action": "release or revoke the held authoritative lease before granting it to another holder",
        },
        exit_code=2,
    )


def grant_lease(store: Any, scope_key: str, holder_node_id: str, *, mode: str = "advisory", ttl_s: int = DEFAULT_TTL_S, summary: str | None = None, assumed_epoch: int | None = None) -> dict[str, Any]:
    """GRANTOR-ONLY. lease_epoch = the coordinator's current epoch; grant_seq = 1 + max grant_seq already
    granted for that scope at that epoch. Append ``lease/granted`` {scope_key, epoch, payload{grant_seq,
    holder, mode, ttl_s, expires_at}} (holder in payload -- the appender is the grantor). Fence ENFORCED
    via ``assumed_epoch`` (M10b).

    ``mode`` (advisory|authoritative) is M10b's CP/AP split. ADVISORY keeps M10a behavior byte-for-byte
    (best-effort; never pulls, never contends). AUTHORITATIVE first runs the freshness gate (pull on a
    synced store; DENIED_LEASE_HUB_UNREACHABLE on a partition) and then the contention gate (a scope
    reduced "held" ⇒ DENIED_LEASE_HELD -- a single-grantor hard mutex).

    §B.6 iso propagation (M15): a SELF-grant minted while THIS node's coordinator role is iso
    (claimed/transferred ``isolated=True``) inherits {iso, iso_sentinel} on the lease payload -- the
    scope-keyed marker ``reconcile._my_iso_scopes`` discovers on reconnect. Grants to OTHER nodes are
    never stamped (iso scope work belongs to the isolated holder, and an isolated node has no reachable
    peers to grant to)."""
    coord = _require_coordinator(store)
    lease_epoch = int(coord.get("epoch") or 0)
    _fence_check(store, assumed_epoch, lease_epoch, scope_key=scope_key)
    lease_mode = _normalize_lease_mode(mode)
    if lease_mode == "authoritative":
        _authoritative_freshness(store, scope_key)
        _assert_authoritative_free(store, scope_key)
    grant_seq = _next_grant_seq(store, scope_key, lease_epoch)
    now_iso = _now_iso()
    expires_at = _expires_at(now_iso, ttl_s)
    payload: dict[str, Any] = {"grant_seq": grant_seq, "holder": holder_node_id, "mode": lease_mode, "ttl_s": int(ttl_s), "expires_at": expires_at}
    if coord.get("iso") and coord.get("iso_sentinel") and holder_node_id == store.node_id:
        payload["iso"] = True
        payload["iso_sentinel"] = coord["iso_sentinel"]
    event = store.append_coord_event(
        "lease",
        "granted",
        summary=summary or f"Lease granted to {holder_node_id} for {scope_key} at epoch {lease_epoch}.",
        scope_key=scope_key,
        epoch=lease_epoch,
        payload=payload,
    )
    return {
        "status": "OK",
        "scope_key": scope_key,
        "holder": holder_node_id,
        "epoch": lease_epoch,
        "grant_seq": grant_seq,
        "mode": lease_mode,
        "ttl_s": int(ttl_s),
        "expires_at": expires_at,
        "id": event["id"],
    }


def renew_lease(store: Any, scope_key: str, *, mode: str = "advisory", ttl_s: int = DEFAULT_TTL_S, summary: str | None = None, assumed_epoch: int | None = None) -> dict[str, Any]:
    """GRANTOR-ONLY renewal at the coordinator's current epoch with the next grant sequence.

    The current epoch normally equals the held grant's epoch; after a coordinator epoch advance the
    renewal moves to that higher epoch. The holder is preserved and ``DENIED_LEASE_NOT_HELD`` is raised
    when no active grant exists. The fence is enforced through ``assumed_epoch`` (M10b).

    ``mode`` (advisory|authoritative): advisory keeps M10a behavior. AUTHORITATIVE runs the freshness
    gate FIRST (DENIED_LEASE_HUB_UNREACHABLE on a synced-store partition) -- renewing the holder's OWN
    grant is the renewal path (never DENIED_LEASE_HELD), so contention against self is exempt.

    §B.6 iso propagation (M15): same rule as :func:`grant_lease` -- a renewal whose holder is THIS node
    under THIS node's iso coordinator role keeps {iso, iso_sentinel} on the re-grant, so an iso scope
    stays discoverable across renewals."""
    coord = _require_coordinator(store)
    lease_epoch = int(coord.get("epoch") or 0)
    _fence_check(store, assumed_epoch, lease_epoch, scope_key=scope_key)
    lease_mode = _normalize_lease_mode(mode)
    if lease_mode == "authoritative":
        _authoritative_freshness(store, scope_key)
    leases = current_leases(store)
    active = leases.get(scope_key)
    if not active or active.get("state") == "free":
        raise KaizenDenied(
            "DENIED_LEASE_NOT_HELD",
            {
                "scope_key": scope_key,
                "required_action": "grant the lease before renewing it",
            },
            exit_code=2,
        )
    holder = active.get("holder") or store.node_id
    if lease_mode == "authoritative":
        # Renew is the current holder's own re-grant path; only a DIFFERENT holder would be contention.
        _assert_authoritative_free(store, scope_key, allow_holder=holder)
    grant_seq = _next_grant_seq(store, scope_key, lease_epoch)
    now_iso = _now_iso()
    expires_at = _expires_at(now_iso, ttl_s)
    payload: dict[str, Any] = {"grant_seq": grant_seq, "holder": holder, "mode": lease_mode, "ttl_s": int(ttl_s), "expires_at": expires_at}
    if coord.get("iso") and coord.get("iso_sentinel") and holder == store.node_id:
        payload["iso"] = True
        payload["iso_sentinel"] = coord["iso_sentinel"]
    event = store.append_coord_event(
        "lease",
        "granted",
        summary=summary or f"Lease renewed for {scope_key} at epoch {lease_epoch} seq {grant_seq}.",
        scope_key=scope_key,
        epoch=lease_epoch,
        payload=payload,
    )
    return {
        "status": "OK",
        "scope_key": scope_key,
        "holder": holder,
        "epoch": lease_epoch,
        "grant_seq": grant_seq,
        "mode": lease_mode,
        "ttl_s": int(ttl_s),
        "expires_at": expires_at,
        "id": event["id"],
    }


def release_lease(store: Any, scope_key: str, *, summary: str | None = None, origin_node: str | None = None) -> dict[str, Any]:
    """HOLDER-SIDE: append ``lease/released`` {scope_key, epoch of the held grant}. Refuse
    DENIED_NOT_HOLDER when the releasing node is not the reduced holder of an active grant (a stranger
    cannot release someone else's lease). No active grant => DENIED_LEASE_NOT_HELD.

    ``origin_node`` (M11, default None = byte-identical pre-M11 behavior): the holder check compares the
    active holder against ``(origin_node or store.node_id)``. origin_node may ONLY be passed by a caller
    that CRYPTOGRAPHICALLY verified the origin (the control_http Ed25519 envelope) -- the signature check
    on the envelope substitutes for the self-node check, so a remote holder can release its OWN lease
    through the daemon (which is not itself the holder). The payload gains origin_node when set."""
    releasing_node = origin_node or store.node_id
    leases = current_leases(store)
    active = leases.get(scope_key)
    if not active or active.get("state") == "free":
        raise KaizenDenied(
            "DENIED_LEASE_NOT_HELD",
            {"scope_key": scope_key, "required_action": "there is no active lease on this scope to release"},
            exit_code=2,
        )
    if active.get("holder") != releasing_node:
        raise KaizenDenied(
            "DENIED_NOT_HOLDER",
            {
                "scope_key": scope_key,
                "node_id": releasing_node,
                "current_holder": active.get("holder"),
                "required_action": "only the lease holder may release it",
            },
            exit_code=2,
        )
    epoch = int(active.get("epoch") or 0)
    # Carry the released grant_seq so a NEW grant at the same epoch (higher seq) is not freed by this
    # release (grant_seq-aware release; expiry-releases semantics).
    payload: dict[str, Any] = {"grant_seq": int(active.get("grant_seq") or 0)}
    if origin_node:
        payload["origin_node"] = origin_node
    event = store.append_coord_event(
        "lease",
        "released",
        summary=summary or f"Lease released for {scope_key} at epoch {epoch}.",
        scope_key=scope_key,
        epoch=epoch,
        payload=payload,
    )
    return {"status": "OK", "scope_key": scope_key, "epoch": epoch, "id": event["id"]}


def revoke_lease(store: Any, scope_key: str, *, summary: str | None = None, assumed_epoch: int | None = None) -> dict[str, Any]:
    """GRANTOR-ONLY: append ``lease/revoked`` {scope_key, epoch of the active grant}. Fence ENFORCED
    (M10b). No active grant => DENIED_LEASE_NOT_HELD."""
    coord = _require_coordinator(store)
    _fence_check(store, assumed_epoch, int(coord.get("epoch") or 0), scope_key=scope_key)
    leases = current_leases(store)
    active = leases.get(scope_key)
    if not active or active.get("state") == "free":
        raise KaizenDenied(
            "DENIED_LEASE_NOT_HELD",
            {"scope_key": scope_key, "required_action": "there is no active lease on this scope to revoke"},
            exit_code=2,
        )
    epoch = int(active.get("epoch") or 0)
    event = store.append_coord_event(
        "lease",
        "revoked",
        summary=summary or f"Lease revoked for {scope_key} at epoch {epoch}.",
        scope_key=scope_key,
        epoch=epoch,
        payload={"grant_seq": int(active.get("grant_seq") or 0)},
    )
    return {"status": "OK", "scope_key": scope_key, "epoch": epoch, "id": event["id"]}


def sweep_expired(store: Any, *, now: str | None = None) -> dict[str, Any]:
    """For every reduced lease whose state == "expired", append an explicit ``lease/expired`` {scope_key,
    epoch}. IDEMPOTENT: a scope already reduced free (an explicit expired/released event exists at its
    winning epoch) is skipped, so a second sweep appends nothing. Returns ``{swept: [...]}``.

    Expiry is a pure clock function in the reducer; the explicit event materializes it so downstream
    (a next grant, an audit) sees the release without needing the same ``now``."""
    if now is None:
        now = _now_iso()
    leases = reducers.reduce_lease(store.coord_events(), now=now)
    swept: list[dict[str, Any]] = []
    for scope_key, projection in sorted(leases.items()):
        if projection.get("state") != "expired":
            continue
        epoch = int(projection.get("epoch") or 0)
        # Carry the expired grant_seq so the release is grant_seq-scoped; a second sweep sees the scope
        # reduced 'free' (not 'expired') and skips it -- idempotent.
        event = store.append_coord_event(
            "lease",
            "expired",
            summary=f"Lease expired for {scope_key} at epoch {epoch} (ttl elapsed).",
            scope_key=scope_key,
            epoch=epoch,
            payload={"grant_seq": int(projection.get("grant_seq") or 0)},
        )
        swept.append({"scope_key": scope_key, "epoch": epoch, "id": event["id"]})
    return {"status": "OK", "swept": swept}


# --- shadow handoff (§B.4 sequence, F2 shadow mode) ----------------------------------------------

def _default_git_runner(repo_root: str) -> GitRunner:
    """Subprocess git runner (shutil.which, cwd=repo_root, utf-8, never raises on non-zero). Tests
    inject their own (a scripted stub or a runner against a SCRATCH repo)."""
    exe = shutil.which("git")

    def run(*args: str, check: bool = False) -> "subprocess.CompletedProcess[str]":
        if not exe:
            return subprocess.CompletedProcess(list(args), 127, "", "git not found")
        return subprocess.run(
            [exe, *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=check,
        )

    return run


class HandoffEngine:
    """The §B.4 handoff sequence in SHADOW mode (F2). Executes quiesce->snapshot->WIP->push->release
    over an injected FleetStore + a git repo, but every divergence/conflict outcome is only RECORDED (a
    ``divergence/detected`` coord_event) and NOTHING blocks. Each step appends a ``handoff/<marker>``
    coord_event; an exception past any step becomes ``handoff/aborted`` + a truthful return (never
    raises past a step).

    Policy gates (ledger #24): WIP commits require ``allow_wip_commit=True`` (labs / an owner-granted
    ``kz/*`` profile); absent that, the DEFAULT is a patch artifact (``git diff HEAD`` written to
    ``artifact_dir``) -- silent commits NEVER happen. Push requires ``allow_push=True`` AND a
    ``hub_remote``; "pushed" means pushed (a ``handoff/pushed`` event is emitted ONLY after a real push
    is CONFIRMED by an ls-remote sha match), else the intent is recorded as ``would_push`` on the
    released payload with NO pushed event.
    """

    def __init__(
        self,
        store: Any,
        repo_root: str,
        *,
        git_runner: GitRunner | None = None,
        hub_remote: str | None = None,
        allow_wip_commit: bool = False,
        allow_push: bool = False,
        artifact_dir: str | None = None,
    ) -> None:
        self.store = store
        self.repo_root = str(repo_root)
        self.git = git_runner or _default_git_runner(self.repo_root)
        self.hub_remote = hub_remote
        self.allow_wip_commit = bool(allow_wip_commit)
        self.allow_push = bool(allow_push)
        self.artifact_dir = artifact_dir

    def _git_out(self, *args: str) -> tuple[int, str]:
        """Run Git arguments via the injected runner and return code plus stripped stdout."""
        proc = self.git(*args)
        return proc.returncode, (proc.stdout or "").strip()

    def _record_divergence(self, reason: str, scope_key: str, detail: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"would_block": reason, "scope_key": scope_key}
        if detail:
            payload.update(detail)
        self.store.append_coord_event(
            "divergence",
            "detected",
            summary=f"Handoff divergence recorded ({reason}); not blocking (shadow).",
            scope_key=scope_key,
            payload=payload,
        )

    def shadow_handoff(self, scope_key: str, *, quiesce: Callable[[], None] | None = None) -> dict[str, Any]:
        """Run the full §B.4 sequence for ``scope_key`` in shadow mode. Returns the step record
        ``{status, scope_key, steps[...], dirty, head_sha, artifact, pushed_sha, divergences[...],
        released_id, completed_id}``. Never raises: any step exception => ``handoff/aborted`` + a
        truthful record with ``status: "ABORTED"`` and the failing step."""
        steps: list[str] = []
        divergences: list[str] = []
        record: dict[str, Any] = {
            "status": "OK",
            "scope_key": scope_key,
            "shadow": True,
            "steps": steps,
            "dirty": None,
            "head_sha": None,
            "artifact": None,
            "wip_commit": None,
            "pushed_sha": None,
            "would_push": None,
            "divergences": divergences,
        }
        current_step = "started"
        try:
            # (1) started
            started = self.store.append_coord_event(
                "handoff", "started", summary=f"Shadow handoff started for {scope_key}.", scope_key=scope_key
            )
            record["started_id"] = started["id"]
            steps.append("started")

            # (2) quiesce callback (default no-op)
            current_step = "quiesce"
            if quiesce is not None:
                quiesce()
            steps.append("quiesce")

            # (3) snapshot: dirty? + HEAD sha
            current_step = "snapshot"
            status_rc, status_out = self._git_out("status", "--porcelain")
            dirty = bool(status_out) if status_rc == 0 else None
            head_rc, head_sha = self._git_out("rev-parse", "HEAD")
            head_sha = head_sha if head_rc == 0 else None
            record["dirty"] = dirty
            record["head_sha"] = head_sha
            steps.append("snapshot")

            # (4) WIP: commit (labs) or patch artifact (default, ledger #24)
            current_step = "wip"
            if self.allow_wip_commit:
                self._git_out("add", "-A")
                # Hooks RUN (never --no-verify): a hook failure surfaces as wip_commit=None and is
                # recorded truthfully; silently bypassing the owner's hooks is not a default this
                # engine gets to make (the F2 standing-profile decision may revisit explicitly).
                self._git_out("commit", "-m", f"kz-wip: shadow handoff {scope_key}")
                commit_rc, commit_sha = self._git_out("rev-parse", "HEAD")
                record["wip_commit"] = commit_sha if commit_rc == 0 else None
                steps.append("wip-commit")
            else:
                artifact = self._write_patch_artifact(scope_key)
                record["artifact"] = artifact
                steps.append("wip-artifact")

            # (5) push: real push + CONFIRM (labs), else record would_push
            current_step = "push"
            if self.allow_push and self.hub_remote:
                pushed_sha = self._push_and_confirm()
                if pushed_sha is not None:
                    # sha only in the synced event -- hub_remote can be a machine-local path/URL (would
                    # trip the redaction pass-path); it stays in the returned record.
                    pushed = self.store.append_coord_event(
                        "handoff", "pushed", summary=f"Shadow handoff pushed {scope_key} to hub.",
                        scope_key=scope_key, payload={"sha": pushed_sha},
                    )
                    record["pushed_sha"] = pushed_sha
                    record["pushed_id"] = pushed["id"]
                    steps.append("pushed")
                else:
                    # Push attempted but not confirmed => a divergence, not a pushed event.
                    self._record_divergence("PUSH_NOT_CONFIRMED", scope_key)
                    divergences.append("PUSH_NOT_CONFIRMED")
                    steps.append("push-unconfirmed")
            else:
                # Machine-local hub_remote stays OUT of the synced payload; record the intent as a flag.
                record["would_push"] = {"enabled": bool(self.hub_remote), "reason": "push not enabled (shadow default)"}
                steps.append("would-push")

            # (6) divergence checks (recorded, never blocking) -- computed before release payload
            current_step = "divergence"
            self._divergence_checks(scope_key, dirty, head_sha, divergences)
            steps.append("divergence-check")

            # (7) release the lease (holder-side) + handoff/released
            current_step = "release"
            release_payload: dict[str, Any] = {}
            if record["would_push"] is not None:
                release_payload["would_push"] = record["would_push"]
            self._release_lease_best_effort(scope_key)
            released = self.store.append_coord_event(
                "handoff", "released", summary=f"Shadow handoff released lease for {scope_key}.",
                scope_key=scope_key, payload=release_payload or None,
            )
            record["released_id"] = released["id"]
            steps.append("released")

            # (8) completed
            current_step = "completed"
            completed_payload: dict[str, Any] = {"shadow": True}
            if record["artifact"]:
                # Only the BASENAME goes in the synced coord_event -- the absolute path is machine-local
                # (may be a home path) and would trip the redaction pass-path; the full path stays in the
                # returned record (never synced).
                completed_payload["artifact"] = Path(record["artifact"]).name
            if record["pushed_sha"]:
                completed_payload["sha"] = record["pushed_sha"]
            completed = self.store.append_coord_event(
                "handoff", "completed", summary=f"Shadow handoff completed for {scope_key}.",
                scope_key=scope_key, payload=completed_payload,
            )
            record["completed_id"] = completed["id"]
            steps.append("completed")
            return record
        except Exception as error:  # noqa: BLE001 -- a step exception => aborted + truthful return
            record["status"] = "ABORTED"
            record["failed_step"] = current_step
            record["error"] = str(error)
            try:
                aborted = self.store.append_coord_event(
                    "handoff", "aborted", summary=f"Shadow handoff aborted at {current_step} for {scope_key}.",
                    scope_key=scope_key, payload={"failed_step": current_step, "reason": str(error)[:180]},
                )
                record["aborted_id"] = aborted["id"]
            except Exception:  # noqa: BLE001 -- even the abort record is best-effort
                pass
            return record

    def _write_patch_artifact(self, scope_key: str) -> str | None:
        """Write ``git diff HEAD`` to ``artifact_dir/handoff-<ts>.patch`` (the policy-gated-commit
        fallback, ledger #24). Returns the path, or None when no artifact_dir is configured."""
        if not self.artifact_dir:
            return None
        proc = self.git("diff", "HEAD")
        diff_text = proc.stdout or ""
        directory = Path(self.artifact_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        path = directory / f"handoff-{stamp}.patch"
        path.write_text(diff_text, encoding="utf-8")
        return str(path)

    def _push_and_confirm(self) -> str | None:
        """``git push <hub> HEAD`` then CONFIRM by matching the local HEAD sha against
        ``git ls-remote <hub>``. Returns the confirmed sha, or None if push/confirm failed (=> a
        divergence, never a pushed event -- "pushed" means pushed)."""
        head_rc, head_sha = self._git_out("rev-parse", "HEAD")
        if head_rc != 0 or not head_sha:
            return None
        push_rc, _ = self._git_out("push", self.hub_remote, "HEAD")
        if push_rc != 0:
            return None
        ls_rc, ls_out = self._git_out("ls-remote", self.hub_remote)
        if ls_rc != 0:
            return None
        # Confirm the head sha appears in the remote's advertised refs.
        for line in ls_out.splitlines():
            sha_field = line.split("\t", 1)[0].strip()
            sha = sha_field.split()[0] if sha_field else ""
            if sha == head_sha:
                return head_sha
        return None

    def _divergence_checks(self, scope_key: str, dirty: bool | None, head_sha: str | None, divergences: list[str]) -> None:
        """Record (never block) the §B.4 divergence conditions: dirty-non-holder, and HEAD != a prior
        grant's ``watermark_sha`` when one was carried."""
        # dirty-non-holder: the workspace is dirty but this node is not the lease holder.
        if dirty:
            leases = current_leases(self.store)
            active = leases.get(scope_key)
            holder = active.get("holder") if active else None
            if holder is not None and holder != self.store.node_id:
                self._record_divergence("DIRTY_NON_HOLDER", scope_key, {"holder": holder})
                divergences.append("DIRTY_NON_HOLDER")

        # HEAD != recorded watermark_sha (out-of-band edit) when a prior grant carried one.
        watermark = self._prior_watermark_sha(scope_key)
        if watermark is not None and head_sha is not None and watermark != head_sha:
            self._record_divergence(
                "HEAD_NOT_WATERMARK", scope_key, {"head_sha": head_sha, "watermark_sha": watermark}
            )
            divergences.append("HEAD_NOT_WATERMARK")

    def _prior_watermark_sha(self, scope_key: str) -> str | None:
        """The most recent ``watermark_sha`` carried by a prior lease/granted for this scope, if any."""
        latest: str | None = None
        for event in self.store.coord_events():
            if event.get("event_kind") != "lease" or event.get("marker") != "granted":
                continue
            if event.get("scope_key") != scope_key:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if payload.get("watermark_sha"):
                latest = payload["watermark_sha"]
        return latest

    def _release_lease_best_effort(self, scope_key: str) -> None:
        """Release the lease if this node holds it; a non-holder/no-lease is silently fine (the handoff
        still records its release step -- shadow mode never blocks)."""
        try:
            release_lease(self.store, scope_key, summary=f"Handoff release for {scope_key}.")
        except KaizenDenied:
            pass
