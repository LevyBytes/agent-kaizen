"""Offline resilience + cross-machine orphan recovery (v8 M15, plan §B.6, deps M10b+M12).

Availability paramount (§B.6). A partition PAUSES only NEW cross-host-authoritative mutations --
``DENIED_LEASE_HUB_UNREACHABLE`` already landed at M10b (an authoritative grant/renew on a synced store
whose pull fails refuses); LOCAL work continues on held scopes and fresh ``kz/<task>/<node>/iso``
branches under the ``iso:`` epoch, with policy-gated local WIP checkpoint commits preserving work at zero
network. This module is the RECONNECT + ORPHAN-RECOVERY leg the plan calls ``D9 reconcile``: it PULLS the
coord ledger, optionally FETCHes git, DETECTS scope overlap, and either AUTO-ADOPTs (no overlap) or
surfaces-and-refuses (``DENIED_ISO_CONFLICT``); and it SWEEPs heartbeat-stale nodes, reclaiming their
leases + canceling their non-terminal dispatches over the immutable event history.

It COMPOSES the existing engine -- it introduces ZERO new coord kinds/markers and ZERO DDL:

- :mod:`fleet.coordination` -- ``_iso_sentinel`` (the §B.3 node-tagged iso marker), ``request_lease``
  (the adoption request), ``current_leases`` (fresh expiry-aware projection), and the exact
  ``lease/expired`` event shape ``sweep_expired`` emits (so the reducer frees a reclaimed scope
  identically), plus ``DEFAULT_TTL_S``. The iso-scope PRODUCER is the shipped D4 ``{"iso": true}``
  claim → D5 self-grant flow: ``grant_lease``/``renew_lease`` stamp {iso, iso_sentinel} on a SELF-grant
  under an iso coordinator (M15 audit closure), which is exactly what :func:`_my_iso_scopes` discovers.
- :mod:`fleet.mirror` -- ``node_branch`` (the branch-per-node name, sanitized), ``fetch_with_fallback``
  (hub->peer git fetch, transport-checked), ``record_fork_decision`` (the surface-and-confirm menu the
  §B.6 "surface-and-confirm fan-in" reuses), ``assert_remote_transport_safe`` (via fetch_with_fallback).
- :mod:`fleet.dispatch_remote` -- ``cancel_dispatch`` (any-side truthful abort, M14) + ``current_dispatches``.
- :mod:`fleet.reducers` -- ``reduce_nodes`` (retired / last_heartbeat), ``reduce_lease`` (expiry-aware),
  ``reduce_coordinator``, ``max_epoch``.

House patterns held: record-then-refuse (every conflict is APPENDED before the raise -- append-only
commits immediately, so the audit survives); NEVER auto-merge (the tree is untouched on conflict);
publish stays behind the HandoffEngine's ``allow_push`` discipline ("published" means an ls-remote sha
CONFIRM); redaction pass-path (only scopes/shas/short reasons/node ids enter a synced payload -- the
HandoffEngine basename-only precedent).

**kaizen.db BOUNDARY (structural).** The survivor finalizes LEDGER-visible state ONLY. A dead node's
``agent_runs`` live in ITS ``kaizen.db`` (un-synced by construction, §3.2) -- structurally out of reach.
Neither :func:`reconcile` nor :func:`sweep_stale_nodes` ever opens or touches any kaizen.db for another
node; a stale node's runs are its own immutable history, and the survivor only reclaims the SHARED
(fleet.db) coordination state (its leases, its in-flight dispatches, its coordinator role divergence).
"""

from __future__ import annotations

from typing import Any

from ..denials import KaizenDenied
from . import coordination, dispatch_remote, mirror, reducers

# The heartbeat-staleness threshold (§9 row 10). An orphan sweep reclaims a node's leases only when its
# freshest heartbeat is older than STALE + SKEW: the skew margin is the clock-skew tolerance that keeps a
# transiently-disconnected node (age inside the margin) from being falsely orphaned. Both are overridable
# per-call so a lab can compress the window.
DEFAULT_STALE_AFTER_S = 900.0
DEFAULT_SKEW_MARGIN_S = 120.0


# --- iso naming + scope discovery ---------------------------------------------------------------

def iso_branch(task: str, node_id: str, worktree: int | None = None) -> str:
    """The §B.6 isolated-work branch ``kz/<task>/<node>[/w<n>]/iso`` -- :func:`mirror.node_branch`
    (sanitized; a bad task refuses ``DENIED_MIRROR_BRANCH_INVALID``) with the ``/iso`` suffix that marks
    a branch minted under the ``iso:`` epoch (fresh, no hub reconciliation). Reconcile publishes THIS
    branch (policy-gated) after adopting its scope."""
    return f"{mirror.node_branch(task, node_id, worktree)}/iso"


def _my_iso_sentinel(store: Any) -> str:
    """This node's §B.3 iso sentinel (``iso:<last6 of node_id>``) -- the exact marker
    :func:`coordination._iso_sentinel` stamps, so a scope this node claimed isolated is matched by its
    own sentinel and NOT by another node's (node-tagged: two isolated nodes' sentinels differ)."""
    return coordination._iso_sentinel(store)


def _my_iso_scopes(store: Any) -> list[str]:
    """The scope keys THIS node holds under the ``iso:`` epoch: every ``scope_key`` carried by a coord
    event whose ``payload.iso_sentinel`` equals THIS node's sentinel (a lease/coordinator event minted
    isolated by this node). Deterministic + de-duplicated (sorted); an event with no scope_key (a bare
    isolated coordinator claim) contributes no scope. This is the set reconcile adopts-or-surfaces."""
    my = _my_iso_sentinel(store)
    scopes: set[str] = set()
    for event in store.coord_events():
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if payload.get("iso_sentinel") != my:
            continue
        scope = event.get("scope_key")
        if scope:
            scopes.add(scope)
    return sorted(scopes)


# --- offline status ------------------------------------------------------------------------------

def offline_status(store: Any) -> dict[str, Any]:
    """The isolation posture of THIS replica (§B.6). NOTE: this PULLS as its isolation probe.

    - ``synced`` -- the store is sync-configured (``store.sync.synced``). An unsynced local/lab store is
      pure-local: it CANNOT be isolated (there is no hub to lose), so ``isolated`` is False and
      ``authoritative_blocked`` is False regardless of connectivity.
    - ``isolated`` -- a SYNCED store whose probe ``pull_and_reduce()`` raises (any exception ⇒ the hub is
      unreachable ⇒ isolated True). A clean pull ⇒ isolated False.
    - ``can_work_local`` -- ALWAYS True (§B.6 availability paramount: local work never stops; only NEW
      cross-host-authoritative mutations pause under partition).
    - ``authoritative_blocked`` -- mirrors ``isolated`` (an isolated node cannot acquire authoritative
      leases -- ``DENIED_LEASE_HUB_UNREACHABLE`` -- but keeps advisory/local coordination).
    - ``iso_scopes`` -- the scopes this node holds under the iso: epoch (:func:`_my_iso_scopes`)."""
    synced = bool(getattr(getattr(store, "sync", None), "synced", False))
    isolated = False
    if synced:
        try:
            store.pull_and_reduce()
        except Exception:  # noqa: BLE001 -- ANY pull failure means the hub is unreachable => isolated
            isolated = True
    return {
        "synced": synced,
        "isolated": isolated,
        "can_work_local": True,
        "authoritative_blocked": isolated,
        "iso_scopes": _my_iso_scopes(store),
    }


# --- overlap detection ---------------------------------------------------------------------------

def _iso_claim_info(store: Any, scope_key: str) -> tuple[str, int | None]:
    """(earliest created_at, highest epoch) across THIS node's iso-sentinel events on ``scope_key`` --
    the iso-era hold's clock origin and epoch fence. ``("", None)`` when no iso event exists. Later events
    on the scope by ANOTHER node past EITHER axis are overlap (they raced the isolated hold)."""
    my = _my_iso_sentinel(store)
    stamps: list[str] = []
    epochs: list[int] = []
    for event in store.coord_events():
        if event.get("scope_key") != scope_key:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if payload.get("iso_sentinel") != my:
            continue
        stamps.append(str(event.get("created_at") or ""))
        epochs.append(int(event.get("epoch") or 0))
    return (min(stamps) if stamps else "", max(epochs) if epochs else None)


def _detect_overlap(store: Any, scope_key: str, *, now: str | None = None) -> dict[str, Any] | None:
    """Detect overlap on one iso scope against the FRESH post-pull ledger. Overlap ⇔ ANY of:

    - (a) a DIFFERENT node is the reduced winning holder of this scope -- someone took it and their
      grant out-ranks the iso hold;
    - (a2/a3) ANY other-node ``lease/granted`` on this scope minted DURING-OR-AFTER the iso era -- by
      clock (created after the iso claim) or by fence (epoch at-or-above the iso hold's epoch, including
      the legacy epoch-zero boundary, which was
      minted above everything this node could see) -- REGARDLESS of reduction rank or current state. An
      iso grant that OUT-RANKS the other side must still surface (leg (a) alone would silently absorb
      the other node's work), and a concurrent grant later released was still concurrent work;
    - (b) another node appended a handoff/dispatch event on this scope AFTER this node's iso claim.

    Deliberate boundary: an other-node grant BOTH pre-claim by clock AND below the iso epoch is M10a
    advisory coexistence (branch-per-node; it was visible before the iso claim), never flagged here.

    Returns ``{other_node, kind, detail...}`` describing the first overlap found, else None. Read-only
    (pure over the ledger); records nothing."""
    leases = coordination.current_leases(store, now=now)
    active = leases.get(scope_key)
    my = store.node_id

    # (a) a different node authoritatively holds the scope in the fresh ledger.
    if active and active.get("state") == "held" and active.get("holder") not in (None, my):
        return {
            "other_node": active.get("holder"),
            "kind": "lease_held",
            "other_epoch": active.get("epoch"),
            "other_grant_seq": active.get("grant_seq"),
            "scope_key": scope_key,
        }

    iso_at, iso_epoch = _iso_claim_info(store, scope_key)
    for event in store.coord_events():
        if event.get("scope_key") != scope_key:
            continue
        kind = event.get("event_kind")
        created = str(event.get("created_at") or "")
        # (a2)/(a3) rank-blind concurrent grant by another node (holder axis, not appender: the
        # grantor appends on behalf of the holder).
        if kind == "lease" and event.get("marker") == "granted":
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            other = payload.get("holder") or event.get("node_id")
            if other in (None, my):
                continue
            epoch = int(event.get("epoch") or 0)
            if (iso_at and created > iso_at) or (iso_epoch is not None and epoch >= iso_epoch):
                return {
                    "other_node": other,
                    "kind": "lease_granted_concurrent",
                    "other_epoch": epoch,
                    "event_id": event.get("id"),
                    "scope_key": scope_key,
                }
            continue
        # (b) a handoff/dispatch by another node created AFTER this node's iso claim.
        if kind not in ("handoff", "dispatch"):
            continue
        other = event.get("node_id")
        if other in (None, my):
            continue
        if iso_at and created > iso_at:
            return {
                "other_node": other,
                "kind": f"{kind}_{event.get('marker')}",
                "event_id": event.get("id"),
                "scope_key": scope_key,
            }
    return None


def _open_iso_conflicts(store: Any) -> dict[tuple[str, str, str], str]:
    """Map unresolved iso-overlap signatures to their existing conflict event ids."""
    events = store.coord_events()
    resolved: set[str] = set()
    detected: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_kind") != "conflict":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event.get("marker") == "resolved" and payload.get("source_conflict_id"):
            resolved.add(str(payload["source_conflict_id"]))
        elif event.get("marker") == "detected" and payload.get("iso") and event.get("id"):
            detected.append(event)
    open_by_signature: dict[tuple[str, str, str], str] = {}
    for event in detected:
        event_id = str(event["id"])
        if event_id in resolved:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        signature = (
            str(event.get("scope_key") or ""),
            str(payload.get("other_node") or ""),
            str(payload.get("overlap_kind") or ""),
        )
        open_by_signature.setdefault(signature, event_id)
    return open_by_signature


# --- reconcile (the §B.6 mermaid: pull -> fetch -> overlap -> adopt/FF or DENIED_ISO_CONFLICT) ----

def reconcile(
    store: Any,
    git_runner: Any = None,
    *,
    hub_remote: str | None = None,
    peer_remotes: list[str] | None = None,
    allow_publish: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """Reconnect + reconcile isolated work (§B.6). The mermaid, step by step:

    a. **PULL** the coord ledger (``store.pull_and_reduce()``). A pull EXCEPTION is a truthful STOP --
       reconcile without a fresh ledger is not reconcile -- ⇒ ``{status: "BLOCKED", pulled: False,
       reason}``, NOTHING appended. An UNSYNCED store pulls nothing (pure-local lab reconciling its own
       ledger) ⇒ ``pulled: "local"`` (a structured no-op) and continues.
    b. **FETCH** (optional): when ``hub_remote`` AND ``git_runner`` are given, ``mirror.fetch_with_fallback``
       (hub then peers, transport-checked). No ``hub_remote`` ⇒ a ledger-only reconcile (``fetched: None``).
    c. **OVERLAP DETECTION** over :func:`_my_iso_scopes`: detect ALL overlaps first; reuse any matching
       unresolved ``conflict/detected`` and append only missing conflicts via
       :func:`mirror.record_fork_decision`; THEN raise ONE ``DENIED_ISO_CONFLICT`` carrying every
       conflict event id + scope (record-then-refuse; NEVER merge, tree untouched).
    d. **AUTO-ADOPT** (no overlap): per iso scope, ``coordination.request_lease`` (a lease/requested at
       the current post-pull state) + a ``resolution/recorded`` {iso_adopted, scope_key, iso_sentinel}
       durable adoption record; collect ``adopted``.
    e. **PUBLISH** (policy-gated): when ``git_runner`` + ``hub_remote`` are given AND the iso branch
       exists locally: ``allow_publish=False`` (DEFAULT, incl. the CLI) ⇒ ``would_publish: [branch...]``
       (a truthful record, NO push); ``allow_publish=True`` (labs/owner) ⇒ ``git push <hub> <branch>`` +
       an ls-remote sha CONFIRM ("published" means confirmed), else the branch lands in ``would_publish``
       with a ``push_failed`` note.
    f. Returns ``{status: "OK", pulled, fetched, adopted, conflicts: [], published/would_publish}``.

    ``git_runner`` follows the injected-runner discipline of :mod:`fleet.mirror`: the public CLI passes
    None unless the operator explicitly provides a hub_remote, so this never defaults git operations onto
    the real repo. Fetch/publish target the explicit operator-provided remote only."""
    # (a) PULL -- a fresh ledger is a precondition; a pull failure is a truthful stop.
    try:
        store.pull_and_reduce()
        pulled: Any = True if bool(getattr(getattr(store, "sync", None), "synced", False)) else "local"
    except KaizenDenied as denied:
        # A structured pull refusal (e.g. DENIED_EPOCH_REGRESSION) is surfaced as the BLOCKED reason.
        return {"status": "BLOCKED", "pulled": False, "reason": denied.code, "fetched": None, "adopted": [], "conflicts": [], "published": [], "would_publish": []}
    except Exception as error:  # noqa: BLE001 -- any transport failure blocks reconcile
        return {"status": "BLOCKED", "pulled": False, "reason": type(error).__name__, "fetched": None, "adopted": [], "conflicts": [], "published": [], "would_publish": []}

    # (b) FETCH (optional; ledger-only reconcile when no hub_remote).
    fetched: dict[str, Any] | None = None
    if hub_remote and git_runner is not None:
        fetch = mirror.fetch_with_fallback(git_runner, hub_remote, peer_remotes or [])
        fetched = {"fetched": fetch.get("fetched"), "source": fetch.get("source")}

    # Idempotency: iso events persist forever in the append-only ledger, so a scope ALREADY adopted by a
    # prior reconcile (a resolution/recorded {iso_adopted} for this node's sentinel) is not re-adopted --
    # a second reconcile appends nothing new for it (and a post-adoption holder is a REAL overlap only
    # for scopes still awaiting adoption).
    already_adopted = _adopted_scopes(store)
    iso_scopes = [s for s in _my_iso_scopes(store) if s not in already_adopted]

    # (c) OVERLAP DETECTION -- detect ALL first, record a conflict per overlap, then refuse once.
    overlaps: list[dict[str, Any]] = []
    for scope_key in iso_scopes:
        overlap = _detect_overlap(store, scope_key, now=now)
        if overlap is not None:
            overlaps.append(overlap)
    if overlaps:
        conflict_ids: list[str] = []
        conflict_scopes: list[str] = []
        open_conflicts = _open_iso_conflicts(store)
        for overlap in overlaps:
            detail = {
                "iso": True,
                "other_node": overlap.get("other_node"),
                "overlap_kind": overlap.get("kind"),
            }
            scope_key = str(overlap["scope_key"])
            signature = (scope_key, str(detail["other_node"] or ""), str(detail["overlap_kind"] or ""))
            event_id = open_conflicts.get(signature)
            if event_id is None:
                event = mirror.record_fork_decision(store, scope_key, detail=detail)
                event_id = str(event.get("id") or "")
                open_conflicts[signature] = event_id
            conflict_ids.append(event_id)
            conflict_scopes.append(scope_key)
        raise KaizenDenied(
            "DENIED_ISO_CONFLICT",
            {
                "scopes": conflict_scopes,
                "conflict_event_ids": conflict_ids,
                "required_action": (
                    "iso work overlaps another node's authoritative hold; resolve the surfaced fork "
                    f"({'/'.join(mirror.FORK_OPTIONS)}, recommend {mirror.FORK_RECOMMENDATION}) -- "
                    "the tree was NOT merged"
                ),
            },
            exit_code=2,
        )

    # (d) AUTO-ADOPT (no overlap): request the lease at the fresh state + a durable adoption record.
    my_sentinel = _my_iso_sentinel(store)
    adopted: list[str] = []
    for scope_key in iso_scopes:
        coordination.request_lease(store, scope_key, summary="iso adoption request")
        store.append_coord_event(
            "resolution",
            "recorded",
            summary=f"Iso scope {scope_key} adopted after reconcile.",
            scope_key=scope_key,
            payload={"iso_adopted": True, "scope_key": scope_key, "iso_sentinel": my_sentinel},
        )
        adopted.append(scope_key)

    result: dict[str, Any] = {
        "status": "OK",
        "pulled": pulled,
        "fetched": fetched,
        "adopted": adopted,
        "conflicts": [],
    }

    # (e) PUBLISH (policy-gated) -- only with a runner + hub, and only for iso branches that exist.
    if git_runner is not None and hub_remote:
        published, would_publish = _publish_iso_branches(
            git_runner, hub_remote, adopted, allow_publish=allow_publish
        )
        if published:
            result["published"] = published
        if would_publish:
            result["would_publish"] = would_publish
    return result


def _adopted_scopes(store: Any) -> set[str]:
    """Scopes THIS node already adopted (a ``resolution/recorded`` {iso_adopted, iso_sentinel=mine})
    -- the reconcile idempotency set: an adopted scope is never re-adopted by a later reconcile."""
    my = _my_iso_sentinel(store)
    adopted: set[str] = set()
    for event in store.coord_events():
        if event.get("event_kind") != "resolution" or event.get("marker") != "recorded":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if payload.get("iso_adopted") and payload.get("iso_sentinel") == my and payload.get("scope_key"):
            adopted.add(payload["scope_key"])
    return adopted


def _branch_candidates(scope_key: str) -> list[str]:
    """The local-branch candidates for an adopted scope. A production scope is
    ``<project_id>/<branch>`` (mirror.branch_scope_key), so the PROJECT-STRIPPED tail is the real branch
    name; the raw scope is also tried (labs may scope by bare branch). Non-existent candidates are
    skipped by the publish gate (rev-parse fails), so a wrong candidate is inert."""
    candidates = [scope_key]
    if "/" in scope_key:
        tail = scope_key.split("/", 1)[1]
        if tail and tail not in candidates:
            candidates.append(tail)
    return candidates


def _local_branch_exists(git_runner: Any, branch: str) -> bool:
    """True when ``branch`` resolves locally (``git rev-parse --verify --quiet <branch>``)."""
    proc = git_runner("rev-parse", "--verify", "--quiet", branch)
    return proc.returncode == 0


def _publish_iso_branches(
    git_runner: Any, hub_remote: str, branches: list[str], *, allow_publish: bool
) -> tuple[list[str], list[dict[str, Any]]]:
    """Policy-gated publish of iso branches (HandoffEngine discipline: "published" means an ls-remote sha
    CONFIRM). ``allow_publish=False`` (default) records EVERY existing branch as ``would_publish`` with NO
    push. ``allow_publish=True`` pushes each + confirms the pushed sha via ``git ls-remote``; a
    push/confirm failure lands the branch in ``would_publish`` with a ``push_failed`` note. Non-existent
    branches are skipped (nothing to publish). ``branches`` here are the adopted scope keys' branch names
    passed straight through -- the caller decides the branch name mapping."""
    published: list[str] = []
    would_publish: list[dict[str, Any]] = []
    for scope in branches:
        # A scope is <project_id>/<branch> in production (branch_scope_key); try the project-stripped
        # tail AND the raw scope, publishing the first candidate that exists locally.
        branch = next(
            (c for c in _branch_candidates(scope) if _local_branch_exists(git_runner, c)), None
        )
        if branch is None:
            continue
        if not allow_publish:
            would_publish.append({"branch": branch, "reason": "publish not enabled (owner-gated)"})
            continue
        sha = _push_and_confirm(git_runner, hub_remote, branch)
        if sha is not None:
            published.append(branch)
        else:
            would_publish.append({"branch": branch, "push_failed": True})
    return published, would_publish


def _push_and_confirm(git_runner: Any, hub_remote: str, branch: str) -> str | None:
    """``git push <hub> <branch>`` then CONFIRM the pushed sha appears in ``git ls-remote <hub>`` (the
    HandoffEngine._push_and_confirm precedent: a push is not "published" until an ls-remote match proves
    it landed). Returns the confirmed sha, or None on any failure."""
    rev = git_runner("rev-parse", branch)
    if rev.returncode != 0 or not (rev.stdout or "").strip():
        return None
    branch_sha = (rev.stdout or "").strip()
    push = git_runner("push", hub_remote, branch)
    if push.returncode != 0:
        return None
    ls = git_runner("ls-remote", hub_remote)
    if ls.returncode != 0:
        return None
    for line in (ls.stdout or "").splitlines():
        parts = line.split()
        if parts and parts[0] == branch_sha:
            return branch_sha
    return None


# --- cross-machine orphan sweep (heartbeat-stale node reclamation) -------------------------------

def _node_last_heartbeat(store: Any, node_id: str, reduced: dict[str, Any]) -> str | None:
    """The freshest heartbeat evidence for ``node_id``: the later of the nodes-table ``last_heartbeat``
    (``store._read_all``) and the reduced ``last_heartbeat`` (``reduce_nodes`` over coord events). None
    when NEITHER exists (no heartbeat evidence at all -- unknown, never swept)."""
    table_hb: str | None = None
    reader = getattr(store, "_read_all", None)
    if callable(reader):
        try:
            rows = reader("SELECT last_heartbeat FROM nodes WHERE node_id = ?", (node_id,))
            if rows and rows[0] and rows[0][0]:
                table_hb = rows[0][0]
        except Exception:  # noqa: BLE001 -- a DB-free FakeStore has no nodes table; reduced-only is fine
            table_hb = None
    reduced_hb = reduced.get("last_heartbeat")
    candidates = [hb for hb in (table_hb, reduced_hb) if hb]
    return max(candidates) if candidates else None


def _age_s(last_heartbeat: str, now_iso: str) -> float | None:
    """Return UTC-normalized heartbeat age; accept ``Z`` and assume UTC for naive values."""
    from datetime import datetime, timezone

    try:
        then = datetime.fromisoformat(last_heartbeat.replace("Z", "+00:00"))
        current = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return (current - then).total_seconds()
    except (ValueError, TypeError):
        return None


def sweep_stale_nodes(
    store: Any,
    *,
    now: str | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    skew_margin_s: float = DEFAULT_SKEW_MARGIN_S,
    include_self: bool = False,
) -> dict[str, Any]:
    """Cross-machine orphan recovery (§B.6 / §9 row 10): reclaim a heartbeat-stale node's LEDGER-visible
    coordination state so the fleet is not blocked on a dead node.

    **Staleness.** Per node, take the freshest heartbeat evidence (:func:`_node_last_heartbeat`). STALE
    ⇔ ``age_s > stale_after_s + skew_margin_s`` -- the skew margin (§9 row 10) is the clock-skew
    tolerance: an age INSIDE the margin is NOT stale (a transient disconnect must not be falsely
    orphaned).

    **Fail-safe exclusions** (never swept): THIS node (unless ``include_self``, test-only); a node with
    NO heartbeat evidence at all (unknown != dead); a RETIRED node (already terminal).

    **Per stale node:**
    - (i) every lease it HOLDS (fresh ``current_leases``, state held) ⇒ append a ``lease/expired``
      carrying ``{scope_key, epoch, payload{grant_seq, reclaimed_from, reason: "heartbeat-stale",
      age_s}}`` -- REUSING ``sweep_expired``'s event shape (same grant_seq-scoped payload) so the reducer
      frees the scope IDENTICALLY;
    - (ii) every NON-terminal dispatch TARGETING it (fresh ``current_dispatches``, state in
      requested|accepted|started) ⇒ ``dispatch_remote.cancel_dispatch`` (any-side truthful abort, M14);
    - (iii) if it is the reduced COORDINATOR ⇒ append a ``divergence/detected`` {would_block:
      "STALE_COORDINATOR", node, age_s} -- RECORD ONLY, NEVER auto-seize the role (claiming coordinator
      is a deliberate operator act, not an automatic reclamation).

    **Idempotent.** A second sweep appends nothing new: a freed lease reduces ``free`` (not ``held``) so
    (i) skips it; a canceled dispatch is terminal so (ii) skips it; (iii) DEDUPES by checking for an
    existing STALE_COORDINATOR divergence at the same node + epoch and skipping when present.

    **kaizen.db boundary.** This finalizes SHARED (fleet.db) state ONLY. A dead node's ``agent_runs``
    live in ITS un-synced kaizen.db (structurally out of reach); this never opens or reads any kaizen.db
    for another node. Proven by a sweep over a DB-free store completing cleanly.

    Returns ``{status: "OK", stale: [{node, age_s, leases_reclaimed, dispatches_canceled,
    was_coordinator}], untouched: n}``."""
    now_iso = now if now is not None else coordination._now_iso()
    events = store.coord_events()
    reduced_nodes = reducers.reduce_nodes(events)
    coordinator = reducers.reduce_coordinator(events)
    leases = coordination.current_leases(store, now=now_iso)
    dispatches = dispatch_remote.current_dispatches(store)

    stale_summaries: list[dict[str, Any]] = []
    untouched = 0
    for node_id in sorted(reduced_nodes):
        reduced = reduced_nodes[node_id]
        # Fail-safe exclusions: self (unless test override), retired (terminal), no-heartbeat (unknown).
        if node_id == store.node_id and not include_self:
            untouched += 1
            continue
        if reduced.get("retired"):
            untouched += 1
            continue
        last_hb = _node_last_heartbeat(store, node_id, reduced)
        if not last_hb:
            untouched += 1
            continue
        age = _age_s(last_hb, now_iso)
        if age is None or age <= (float(stale_after_s) + float(skew_margin_s)):
            # Inside the skew margin (or fresh) => a transient disconnect, NOT an orphan.
            untouched += 1
            continue

        age_rounded = round(age, 3)
        leases_reclaimed = _reclaim_leases(store, node_id, leases, age_rounded)
        dispatches_canceled = _cancel_targeted_dispatches(store, node_id, dispatches)
        was_coordinator = _record_stale_coordinator(store, node_id, coordinator, age_rounded)
        stale_summaries.append(
            {
                "node": node_id,
                "age_s": age_rounded,
                "leases_reclaimed": leases_reclaimed,
                "dispatches_canceled": dispatches_canceled,
                "was_coordinator": was_coordinator,
            }
        )
    return {"status": "OK", "stale": stale_summaries, "untouched": untouched}


def _reclaim_leases(store: Any, node_id: str, leases: dict[str, dict[str, Any]], age_s: float) -> list[str]:
    """Append a ``lease/expired`` for every scope the stale ``node_id`` HOLDS (state held). The payload
    REUSES ``sweep_expired``'s grant_seq-scoped shape (so the reducer frees the scope identically) plus
    the reclamation provenance {reclaimed_from, reason, age_s}. Idempotent: a scope already freed reduces
    'free', not 'held', so it is not matched here on a re-sweep. Returns the reclaimed scope keys."""
    reclaimed: list[str] = []
    for scope_key, projection in sorted(leases.items()):
        if projection.get("state") != "held" or projection.get("holder") != node_id:
            continue
        epoch = int(projection.get("epoch") or 0)
        store.append_coord_event(
            "lease",
            "expired",
            summary=f"Lease reclaimed for {scope_key} at epoch {epoch} (holder heartbeat-stale).",
            scope_key=scope_key,
            epoch=epoch,
            payload={
                "grant_seq": int(projection.get("grant_seq") or 0),
                "reclaimed_from": node_id,
                "reason": "heartbeat-stale",
                "age_s": age_s,
            },
        )
        reclaimed.append(scope_key)
    return reclaimed


def _cancel_targeted_dispatches(store: Any, node_id: str, dispatches: dict[str, dict[str, Any]]) -> list[str]:
    """Cancel every NON-terminal dispatch TARGETING the stale ``node_id`` (state in
    requested|accepted|started) via ``dispatch_remote.cancel_dispatch`` (any-side truthful abort, M14).
    Idempotent: a canceled dispatch is terminal, so a re-sweep finds nothing non-terminal. Returns the
    canceled dispatch ids."""
    canceled: list[str] = []
    non_terminal = {"requested", "accepted", "started"}
    for dispatch_id, projection in sorted(dispatches.items()):
        if projection.get("target_node") != node_id:
            continue
        if projection.get("state") not in non_terminal:
            continue
        dispatch_remote.cancel_dispatch(store, dispatch_id, reason="target heartbeat-stale")
        canceled.append(dispatch_id)
    return canceled


def _record_stale_coordinator(store: Any, node_id: str, coordinator: dict[str, Any], age_s: float) -> bool:
    """When the stale ``node_id`` is the reduced COORDINATOR, append a ``divergence/detected``
    {would_block: "STALE_COORDINATOR", node, epoch, age_s} -- RECORD ONLY (never auto-seize the role; a
    claim is a deliberate operator act). Idempotent: DEDUPE by scanning for an existing STALE_COORDINATOR
    divergence at the same node + epoch and skipping when present. Returns True when this node WAS the
    coordinator (whether or not a fresh divergence was appended)."""
    if coordinator.get("holder") != node_id:
        return False
    epoch = coordinator.get("epoch")
    # Dedupe: a prior sweep may already have recorded this exact stale-coordinator divergence.
    for event in store.coord_events():
        if event.get("event_kind") != "divergence" or event.get("marker") != "detected":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if (
            payload.get("would_block") == "STALE_COORDINATOR"
            and payload.get("node") == node_id
            and payload.get("epoch") == epoch
        ):
            return True
    store.append_coord_event(
        "divergence",
        "detected",
        summary=f"Coordinator {node_id} heartbeat-stale at epoch {epoch}; role not auto-seized.",
        payload={
            "would_block": "STALE_COORDINATOR",
            "node": node_id,
            "epoch": epoch,
            "age_s": age_s,
        },
    )
    return True
