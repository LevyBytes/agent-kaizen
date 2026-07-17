"""Built-in fleet metrics (v8 M17, plan §5.3 M17 / ledger #15 -- metrics BEFORE external observability;
OTEL-for-Kaizen stays deferred owner-gated).

Pure projections over ``coord_events`` (+ the store's sync stats when synced): everything here re-derives
from the append-only ledger exactly like the reducers -- no counters table, no mutable metric rows, no
new DDL. Consumed by the daemon loopback (``fleet/metrics``), the control-service ``/v1/health``
endpoint, and the advisory R0 fleet section; never by a write path.

Metric set (the §5.3 M17 list):
- ``heartbeat_ages`` -- per-node liveness age (the staleness signal the M15 sweep thresholds read);
- ``sync_staleness`` -- age of the newest FOREIGN event in this replica (how stale my view of the rest
  of the fleet plausibly is; None single-node) + the newest local append age;
- ``lease_conflicts`` -- open conflict spans (reduce_conflicts) = contested claims/forks awaiting a
  decision;
- ``orphan_sweeps`` -- reclamations performed by M15 sweeps (lease/expired carrying ``reclaimed_from``);
- ``dispatch_latency`` -- requested→terminal wall seconds per finished dispatch (avg/max) + open count.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import reducers


def _age_s(then_iso: str, now_iso: str) -> float | None:
    """Return elapsed seconds between two ISO instants, or None when unavailable."""
    try:
        return (datetime.fromisoformat(now_iso) - datetime.fromisoformat(then_iso)).total_seconds()
    except (ValueError, TypeError):
        return None


def _now_iso() -> str:
    """Return the shared UTC ISO clock used by fleet event producers."""
    from .. import db

    return db.now()


def heartbeat_ages(events: list[dict[str, Any]], now_iso: str) -> dict[str, float]:
    """Per-node ``last_heartbeat`` age in seconds (reduce_nodes evidence; nodes with no heartbeat are
    omitted -- unknown is not zero)."""
    ages: dict[str, float] = {}
    for node_id, projection in reducers.reduce_nodes(events).items():
        last = projection.get("last_heartbeat")
        if not last:
            continue
        age = _age_s(str(last), now_iso)
        if age is not None:
            ages[node_id] = round(age, 3)
    return ages


def sync_staleness(events: list[dict[str, Any]], self_node: str, now_iso: str) -> dict[str, Any]:
    """The replica-view staleness signals: age of the newest FOREIGN-node event (None when this replica
    has never seen another node -- single-node plane) and of the newest LOCAL append."""
    newest_foreign: str | None = None
    newest_local: str | None = None
    for event in events:
        created = str(event.get("created_at") or "")
        if not created:
            continue
        if event.get("node_id") == self_node:
            if newest_local is None or created > newest_local:
                newest_local = created
        elif newest_foreign is None or created > newest_foreign:
            newest_foreign = created
    foreign_age = _age_s(newest_foreign, now_iso) if newest_foreign else None
    local_age = _age_s(newest_local, now_iso) if newest_local else None
    return {
        "newest_foreign_event_age_s": round(foreign_age, 3) if foreign_age is not None else None,
        "newest_local_event_age_s": round(local_age, 3) if local_age is not None else None,
    }


def lease_conflicts(events: list[dict[str, Any]], now_iso: str) -> dict[str, Any]:
    """Open conflict spans (the M12 blocking-span projection): count + the oldest open span's age --
    a growing oldest-age is a decision nobody is making."""
    projection = reducers.reduce_conflicts(events)
    open_spans = projection.get("open") or []
    oldest: float | None = None
    for span in open_spans:
        age = _age_s(str(span.get("created_at") or ""), now_iso)
        if age is not None and (oldest is None or age > oldest):
            oldest = age
    return {"open_count": len(open_spans), "oldest_open_age_s": round(oldest, 3) if oldest is not None else None}


def orphan_sweeps(events: list[dict[str, Any]]) -> dict[str, Any]:
    """M15 sweep reclamations: every ``lease/expired`` carrying ``payload.reclaimed_from`` (the
    provenance the sweep stamps). Totals + per-reclaimed-node counts + the latest reclamation time."""
    total = 0
    by_node: dict[str, int] = {}
    last_at: str | None = None
    for event in events:
        if event.get("event_kind") != "lease" or event.get("marker") != "expired":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        source = payload.get("reclaimed_from")
        if not source:
            continue  # a plain expiry sweep, not an orphan reclamation
        total += 1
        by_node[source] = by_node.get(source, 0) + 1
        created = str(event.get("created_at") or "")
        if last_at is None or created > last_at:
            last_at = created
    return {"reclaimed_total": total, "by_node": by_node, "last_at": last_at}


def dispatch_latency(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Return latency/counts for dispatches with a request and a non-negative terminal interval.

    The latest terminal event wins, matching :func:`reducers.reduce_dispatches`. Orphan terminal events
    and negative clock-skew pairs are counted separately instead of disappearing from the projection."""
    requested_at: dict[str, str] = {}
    terminal_at: dict[str, str] = {}
    terminal_key: dict[str, tuple[str, str]] = {}
    for event in events:
        if event.get("event_kind") != "dispatch":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        dispatch_id = payload.get("dispatch_id")
        if not dispatch_id:
            continue
        created = str(event.get("created_at") or "")
        marker = event.get("marker")
        if marker == "requested":
            prior = requested_at.get(dispatch_id)
            if prior is None or created < prior:
                requested_at[dispatch_id] = created
        elif marker in ("completed", "failed", "canceled"):
            key = (created, str(event.get("id") or ""))
            if dispatch_id not in terminal_key or key > terminal_key[dispatch_id]:
                terminal_key[dispatch_id] = key
                terminal_at[dispatch_id] = created
    latencies: list[float] = []
    terminal_no_start = 0
    skew_dropped = 0
    for dispatch_id, done in terminal_at.items():
        start = requested_at.get(dispatch_id)
        if start is None:
            terminal_no_start += 1
            continue
        delta = _age_s(start, done)
        if delta is not None and delta >= 0:
            latencies.append(delta)
        elif delta is not None:
            skew_dropped += 1
    open_count = len([d for d in requested_at if d not in terminal_at])
    return {
        "terminal_count": len(latencies),
        "avg_s": round(sum(latencies) / len(latencies), 3) if latencies else None,
        "max_s": round(max(latencies), 3) if latencies else None,
        "open_count": open_count,
        "terminal_no_start_count": terminal_no_start,
        "clock_skew_dropped_count": skew_dropped,
    }


def fleet_metrics(store: Any, *, now: str | None = None) -> dict[str, Any]:
    """The M17 metric bundle over one store's view: pure event projections + the sync handle's stats
    when synced (revision/frames from turso -- the daemon-only ``stats()`` surface re-used, never a
    second wire)."""
    now_iso = now if now is not None else _now_iso()
    events = store.coord_events()
    bundle: dict[str, Any] = {
        "status": "OK",
        "now": now_iso,
        "node_id": store.node_id,
        "heartbeat_ages_s": heartbeat_ages(events, now_iso),
        "sync_staleness": sync_staleness(events, store.node_id, now_iso),
        "lease_conflicts": lease_conflicts(events, now_iso),
        "orphan_sweeps": orphan_sweeps(events),
        "dispatch_latency": dispatch_latency(events),
        "coord_events": len(events),
        "max_epoch": reducers.max_epoch(events),
    }
    if bool(getattr(getattr(store, "sync", None), "synced", False)):
        try:
            bundle["sync_stats"] = store.stats()
        except Exception as error:  # noqa: BLE001 -- metrics are advisory; a stats fault must not break them
            bundle["sync_stats"] = {"status": "ERROR", "error": type(error).__name__}
    return bundle
