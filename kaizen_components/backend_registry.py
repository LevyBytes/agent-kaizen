"""B8 backend registry (v8 M13, plan §C.3): productionize remote model endpoints.

Normalizes remote model endpoints into the ``backend_endpoints`` table (relational > JSON blob) and
routes lane traffic to them with a circuit-breaker failover engine. Environment variables remain the
zero-config single-endpoint path: an EMPTY registry means :func:`resolve_endpoint` returns ``None`` and
every caller falls back to the env-var backend UNCHANGED -- this module adds capability, it never
changes default behavior.

CLI (``B8 --action add|list|probe|remove``):
- ``add``     -- register an endpoint from ``--payload-json {base_url, lanes[], model, priority?, node_id?}``.
- ``list``    -- list endpoints ordered (priority, created_at); ``--limit`` caps the rows.
- ``probe``   -- health/capability probe every enabled endpoint (or ``--id`` one) via the SHIPPED
                 backend ``probe()`` shape; UPDATE health + last_probe; NEVER raises on a dead endpoint.
- ``remove``  -- ``--id`` hard-delete (registry rows are config, not history).

Resolver / failover (availability only):
- :func:`resolve_endpoint(lane, model=None)` returns the highest-priority (lowest ``priority`` int)
  enabled endpoint serving ``lane`` (and matching ``model`` when pinned), skipping breaker-tripped
  endpoints, else ``None``.
- The in-process circuit breaker trips an endpoint on a TRANSIENT failure (``http_retry`` classification)
  for ``BREAKER_COOLDOWN_S``; :func:`resolve_with_failover` walks the priority list, bounded to the
  enabled-row count, recording each hop in a ``failover`` trail.
- **DENIED_EMBED_MISMATCH invariant (data integrity, plan §C.3):** the embed lane NEVER fails over
  across a DIFFERENT model -- an embedding index is bound to one model identity, so a same-model
  different-endpoint hop is allowed but a cross-model hop is NOT (the resolver returns ``None`` rather
  than silently serving vectors from the wrong model space). Text/judge lanes may cross models ONLY when
  the caller passed no model pin.

Provenance boundary (plan §C.3): :func:`resolve_endpoint` returns the endpoint ``id`` and the
``agent_runs.backend_endpoint_id`` column now exists for M14's dispatch lane to stamp. **M13 does NOT
wire any agent_runs write** -- stamping run provenance is M14's job; this module only resolves and
records endpoint health.

Purity: this is a record handler (no ``subprocess``/``socket``/``asyncio``/``http.server`` import). The
probe HTTP goes through the allowlisted ``backends`` adapters; the tailnet check goes through the
allowlisted ``fleet.net`` (both lazily imported), so the import-guard purity boundary holds.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

from .db import fetch_all, fetch_one, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import utc_text_hash, validate_text_fields
from .paths import read_text_file
from .redaction import assert_redacted
from .schemas import validate_record
from .schemas.registry import KAIZEN_ENUMS

# The lane vocabulary an endpoint may serve (subset stored per row). Single source of truth is the
# 'backend_lane' enum in schemas/registry.py (element-enforced here since the compact 'list' field
# type cannot bound elements).
LANES: tuple[str, ...] = tuple(KAIZEN_ENUMS["backend_lane"])

# Circuit-breaker cooldown: how long a transient-failed endpoint stays tripped (skipped by the
# resolver) before it is eligible again. Availability knob only -- never affects data integrity.
BREAKER_COOLDOWN_S = 30.0

# Short in-process health memo TTL: resolve_endpoint reads rows, never probes (no hot loop); this
# bounds how often the same process re-reads the endpoint rows within one burst of resolves.
HEALTH_MEMO_TTL_S = 15.0

# In-process circuit breaker: {endpoint_id: trip_until_monotonic}. Availability-only, per-process
# (never persisted -- a fresh process starts with a clean breaker, and health lives in the row).
_BREAKER: dict[str, float] = {}

# In-process health memo: (rows_snapshot, fetched_at_monotonic) so a burst of resolves in one process
# does not re-read the table every call. Invalidated by add/remove/probe (they bump _memo_dirty).
_MEMO: dict[str, Any] = {"rows": None, "at": 0.0}


def _payload(args: Any) -> dict[str, Any]:
    """Reads --payload-json / --payload-json-file, returns dict, raises DENIED_PAYLOAD_TYPE on non-object."""
    raw = getattr(args, "payload_json", None)
    if getattr(args, "payload_json_file", None):
        raw = read_text_file(args.payload_json_file)
    payload = json.loads(raw) if raw else {}
    if not isinstance(payload, dict):
        raise KaizenDenied(
            "DENIED_PAYLOAD_TYPE",
            {"required_action": "--payload-json must be a JSON object"},
            exit_code=2,
        )
    return payload


def _clean_lanes(raw: Any) -> list[str]:
    """Validate + normalize the lanes list: a non-empty subset of LANES, de-duplicated, order-stable.

    An embedding index is model-bound, so lanes are the endpoint's advertised capability set; an
    invalid lane is a config error (DENIED_BACKEND_LANE_INVALID), not something to silently drop."""
    if not isinstance(raw, list) or not raw:
        raise KaizenDenied(
            "DENIED_BACKEND_LANE_INVALID",
            {"lanes": raw, "allowed": list(LANES),
             "required_action": "pass a non-empty lanes list, a subset of " + "|".join(LANES)},
            exit_code=2,
        )
    seen: list[str] = []
    for lane in raw:
        if lane not in LANES:
            raise KaizenDenied(
                "DENIED_BACKEND_LANE_INVALID",
                {"lane": lane, "allowed": list(LANES),
                 "required_action": "every lane must be one of " + "|".join(LANES)},
                exit_code=2,
            )
        if lane not in seen:
            seen.append(lane)
    return seen


def _memo_dirty() -> None:
    _MEMO["rows"] = None
    _MEMO["at"] = 0.0


# --- transport safety (reuse the backends gate; do NOT duplicate) --------------------------------

def _assert_transport(base_url: str, *, tailnet_probe=None) -> None:
    """Reuse the SHIPPED backends transport gate (loopback http / https / KAIZEN_ALLOW_INSECURE_HTTP /
    v8 M13 tailnet-suffixed http while on_tailnet). ``tailnet_probe`` is injectable for tests."""
    from .backends import _assert_endpoint_transport_safe

    _assert_endpoint_transport_safe(base_url, "backend_endpoints", tailnet_probe=tailnet_probe)


# --- add -----------------------------------------------------------------------------------------

def _add(args: Any, *, tailnet_probe=None) -> dict[str, Any]:
    """Registers an endpoint; gates transport before persist; `tailnet_probe` is a test-injection hook (undocumented in signature); returns the row summary."""
    payload = _payload(args)
    base_url = payload.get("base_url")
    if not base_url:
        raise KaizenDenied(
            "DENIED_BACKEND_BASE_URL_REQUIRED",
            {"required_action": "resubmit with --payload-json '{\"base_url\":\"https://host/v1\", \"lanes\":[\"embed\"], \"model\":\"...\"}'"},
            exit_code=2,
        )
    model = payload.get("model")
    if not model:
        raise KaizenDenied(
            "DENIED_BACKEND_MODEL_REQUIRED",
            {"required_action": "resubmit with a \"model\" in --payload-json (the model the endpoint serves)"},
            exit_code=2,
        )
    lanes = _clean_lanes(payload.get("lanes"))
    priority = payload.get("priority", 100)
    if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
        raise KaizenDenied(
            "DENIED_BACKEND_PRIORITY_INVALID",
            {"priority": priority, "required_action": "priority must be a non-negative integer (lower = tried first)"},
            exit_code=2,
        )
    node_id = payload.get("node_id")
    summary = getattr(args, "summary", None) or payload.get("summary") or f"Backend endpoint {base_url} ({','.join(lanes)})."

    # Transport gate BEFORE persisting (a plain-http non-tailnet endpoint must never reach the table).
    _assert_transport(base_url, tailnet_probe=tailnet_probe)

    clean = {
        k: v
        for k, v in {
            "base_url": base_url,
            "lanes": lanes,
            "model": model,
            "priority": priority,
            "node_id": node_id,
            "summary": summary,
        }.items()
        if v not in (None, "")
    }
    validate_record("backend_endpoint", clean)
    validate_text_fields({"summary": summary})
    # A bearer token pasted into base_url (or the summary) must never durably land in the table.
    assert_redacted({"base_url": base_url, "model": model, "summary": summary, "node_id": node_id or ""})

    record_id = new_id("be")
    created = now()
    lanes_json = json.dumps(lanes)
    content_hash = utc_text_hash({"id": record_id, "base_url": base_url, "lanes": lanes, "model": model, "priority": priority})
    is_test = 1 if getattr(args, "test", False) else 0

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO backend_endpoints "
            "(id, created_at, node_id, base_url, lanes, model, priority, health, last_probe, enabled, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1, ?, ?)",
            (record_id, created, node_id, base_url, lanes_json, model, priority, content_hash, is_test),
        )

    write_tx(op)
    _memo_dirty()
    return {
        "status": "OK",
        "id": record_id,
        "base_url": base_url,
        "lanes": lanes,
        "model": model,
        "priority": priority,
        "content_hash": content_hash,
    }


# --- list ----------------------------------------------------------------------------------------

def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    """Maps a _SELECT_COLS-ordered row tuple to a dict; tolerant JSON-decode of lanes."""
    (rid, created, node_id, base_url, lanes_json, model, priority, health, last_probe, enabled) = row
    try:
        lanes = json.loads(lanes_json) if lanes_json else []
    except (TypeError, ValueError):
        lanes = []
    return {
        "id": rid,
        "created_at": created,
        "node_id": node_id,
        "base_url": base_url,
        "lanes": lanes,
        "model": model,
        "priority": int(priority) if priority is not None else None,
        "health": health,
        "last_probe": last_probe,
        "enabled": bool(enabled),
    }


_SELECT_COLS = (
    "id, created_at, node_id, base_url, lanes, model, priority, health, last_probe, enabled"
)


def list_endpoints(*, limit: int | None = None, enabled_only: bool = False) -> list[dict[str, Any]]:
    """Endpoint rows ordered (priority ASC, created_at ASC) -- lowest priority int is tried first."""
    where = "WHERE enabled = 1 " if enabled_only else ""
    sql = f"SELECT {_SELECT_COLS} FROM backend_endpoints {where}ORDER BY priority ASC, created_at ASC"
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise KaizenDenied(
                "DENIED_BACKEND_LIMIT_INVALID",
                {"limit": limit, "required_action": "limit must be a nonnegative integer"},
                exit_code=2,
            )
        sql += f" LIMIT {limit}"
    return [_row_to_dict(r) for r in fetch_all(sql)]


def _list(args: Any) -> dict[str, Any]:
    """B8 list action wrapper (see R2: --limit handling)."""
    limit = getattr(args, "limit", None)
    endpoints = list_endpoints(limit=limit)
    return {
        "status": "OK",
        "count": len(endpoints),
        "endpoints": endpoints,
        "note": "env vars remain the zero-config single-endpoint path; an empty registry falls back to them.",
    }


# --- remove --------------------------------------------------------------------------------------

def _remove(args: Any) -> dict[str, Any]:
    """Hard-deletes the row, clears its breaker + memo; raises DENIED_BACKEND_NOT_FOUND."""
    endpoint_id = getattr(args, "id", None)
    if not endpoint_id:
        raise KaizenDenied(
            "DENIED_BACKEND_ID_REQUIRED",
            {"required_action": "pass --id <endpoint_id> (list them with B8 --action list)"},
            exit_code=2,
        )
    existing = fetch_one("SELECT id FROM backend_endpoints WHERE id = ?", (endpoint_id,))
    if existing is None:
        raise KaizenDenied(
            "DENIED_BACKEND_NOT_FOUND",
            {"id": endpoint_id, "required_action": "check the id with B8 --action list"},
            exit_code=1,
        )

    def op(conn: Any, _attempt: int) -> None:
        conn.execute("DELETE FROM backend_endpoints WHERE id = ?", (endpoint_id,))

    write_tx(op)
    _BREAKER.pop(endpoint_id, None)
    _memo_dirty()
    return {"status": "OK", "id": endpoint_id, "removed": True}


# --- probe ---------------------------------------------------------------------------------------

def _probe_endpoint(row: dict[str, Any]) -> dict[str, Any]:
    """Health/capability probe one endpoint via the SHIPPED backend probe() shape. Returns
    {id, health, lanes_probed[]} and NEVER raises -- a dead endpoint records health truthfully.

    Per lane: embed -> OllamaEmbeddingBackend.probe() (embeddings); text|judge -> OllamaTextBackend.probe()
    (chat); rerank -> no shipped Ollama rerank adapter, so it records 'unsupported' truthfully rather
    than faking a capability. health is "ok" when EVERY probed lane succeeds, else "error: <class>"."""
    from .backends.ollama import OllamaEmbeddingBackend, OllamaTextBackend

    base_url = row["base_url"]
    model = row["model"]
    lane_results: list[dict[str, Any]] = []
    first_error: str | None = None
    for lane in row["lanes"]:
        try:
            if lane == "embed":
                OllamaEmbeddingBackend(base_url=base_url, model=model).probe()
            elif lane in ("text", "judge"):
                OllamaTextBackend(base_url=base_url, model=model).probe()
            elif lane == "rerank":
                # No shipped Ollama cross-encoder rerank adapter -> record the truth, do not fake it.
                lane_results.append({"lane": lane, "ok": False, "detail": "unsupported"})
                continue
            else:  # pragma: no cover -- lanes are enum-validated on add
                lane_results.append({"lane": lane, "ok": False, "detail": "unknown-lane"})
                continue
            lane_results.append({"lane": lane, "ok": True})
        except Exception as error:  # noqa: BLE001 -- a probe never raises; it records health truthfully
            cls = type(error).__name__
            if first_error is None:
                first_error = cls
            lane_results.append({"lane": lane, "ok": False, "detail": cls})
    all_ok = bool(lane_results) and all(item["ok"] for item in lane_results)
    # The final branch is the all-unsupported-lanes case (currently rerank-only endpoints).
    health = "ok" if all_ok else (f"error: {first_error}" if first_error else "error: unsupported")
    return {"id": row["id"], "health": health, "lanes": lane_results}


def _probe(args: Any) -> dict[str, Any]:
    """Probes --id one (incl. disabled) or all enabled endpoints; UPDATEs health+last_probe; never raises per-endpoint."""
    endpoint_id = getattr(args, "id", None)
    if endpoint_id:
        row = fetch_one(f"SELECT {_SELECT_COLS} FROM backend_endpoints WHERE id = ?", (endpoint_id,))
        if row is None:
            raise KaizenDenied(
                "DENIED_BACKEND_NOT_FOUND",
                {"id": endpoint_id, "required_action": "check the id with B8 --action list"},
                exit_code=1,
            )
        targets = [_row_to_dict(row)]
    else:
        targets = list_endpoints(enabled_only=True)

    results: list[dict[str, Any]] = []
    for row in targets:
        outcome = _probe_endpoint(row)
        probed_at = now()
        health = outcome["health"]

        def op(conn: Any, _attempt: int, _rid=row["id"], _h=health, _at=probed_at) -> None:
            conn.execute(
                "UPDATE backend_endpoints SET health = ?, last_probe = ? WHERE id = ?",
                (_h, _at, _rid),
            )

        write_tx(op)
        results.append({**outcome, "last_probe": probed_at})
    _memo_dirty()
    return {"status": "OK", "probed": len(results), "results": results}


# --- resolver + circuit-breaker failover ---------------------------------------------------------

def _breaker_tripped(endpoint_id: str, *, clock: Callable[[], float] = time.monotonic) -> bool:
    until = _BREAKER.get(endpoint_id)
    if until is None:
        return False
    if clock() >= until:
        _BREAKER.pop(endpoint_id, None)  # cooldown elapsed -> eligible again
        return False
    return True


def trip_breaker(endpoint_id: str, *, cooldown_s: float = BREAKER_COOLDOWN_S, clock: Callable[[], float] = time.monotonic) -> None:
    """Trip an endpoint's breaker for ``cooldown_s`` (availability only). Callers trip on a TRANSIENT
    failure classified by ``http_retry`` -- a 4xx / config error is NOT transient and must not trip."""
    _BREAKER[endpoint_id] = clock() + cooldown_s


def reset_breakers() -> None:
    """Clear the in-process breaker (test hook; a fresh process already starts clean)."""
    _BREAKER.clear()


def is_transient_failure(error: Exception) -> bool:
    """Reuse the shipped ``http_retry`` transient classification (5xx/429/timeout/connection = transient;
    4xx/SSL/DNS = permanent). A KaizenDenied wrapping an unreachable backend is treated as transient."""
    from .backends.http_retry import _is_transient
    from .denials import KaizenDenied as _KD

    if isinstance(error, _KD):
        # DENIED_BACKEND_UNAVAILABLE == unreachable (transient); DENIED_BACKEND_HTTP == a real HTTP
        # status the endpoint returned (a 4xx wrong-model is permanent; only 5xx/429 are transient).
        code = getattr(error, "code", "")
        if code == "DENIED_BACKEND_UNAVAILABLE":
            return True
        fields = error.fields if isinstance(getattr(error, "fields", None), dict) else {}
        status = fields.get("http_status")
        if isinstance(status, int):
            return status >= 500 or status == 429
        return False
    return _is_transient(error)


def _enabled_rows_for_lane(lane: str, model: str | None) -> list[dict[str, Any]]:
    """Enabled endpoints serving ``lane`` (and matching ``model`` when pinned), priority order.

    Uses the in-process health memo to avoid re-reading the table on every resolve within one burst;
    the memo is invalidated by add/remove/probe."""
    memo_rows = _MEMO.get("rows")
    if memo_rows is None or (time.monotonic() - float(_MEMO.get("at", 0.0))) > HEALTH_MEMO_TTL_S:
        memo_rows = list_endpoints(enabled_only=True)
        _MEMO["rows"] = memo_rows
        _MEMO["at"] = time.monotonic()
    out = []
    for row in memo_rows:
        if lane not in row["lanes"]:
            continue
        if model is not None and row["model"] != model:
            continue
        out.append({**row, "lanes": list(row["lanes"])})
    return out


def _validate_resolve_request(lane: str, model: str | None) -> None:
    """Reject invalid lanes and unpinned embedding resolves before consulting the registry."""
    if lane not in LANES:
        raise KaizenDenied(
            "DENIED_BACKEND_LANE_INVALID",
            {"lane": lane, "allowed": list(LANES), "required_action": "resolve a known lane"},
            exit_code=2,
        )
    if lane == "embed" and model is None:
        raise KaizenDenied(
            "DENIED_BACKEND_MODEL_REQUIRED",
            {
                "lane": lane,
                "required_action": "pin the embedding model so failover cannot cross model spaces",
            },
            exit_code=2,
        )


def resolve_endpoint(
    lane: str,
    *,
    model: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Optional[dict[str, Any]]:
    """The highest-priority (lowest ``priority`` int) enabled endpoint serving ``lane``, skipping
    breaker-tripped rows; ``None`` when none qualifies (callers then fall back to the env-var path --
    UNCHANGED default behavior).

    ``model`` pins the model identity: for the embed lane a pin is MANDATORY-in-spirit -- an embedding
    index is bound to one model, so this NEVER returns a row whose model differs from ``model`` (the
    DENIED_EMBED_MISMATCH data-integrity invariant). Text/judge callers may leave ``model`` unpinned to
    accept any model on the lane. Never probes (no hot loop) -- it reads the health cache the B8
    --probe path wrote."""
    _validate_resolve_request(lane, model)
    for row in _enabled_rows_for_lane(lane, model):
        if _breaker_tripped(row["id"], clock=clock):
            continue
        return row
    return None


def resolve_with_failover(
    lane: str,
    attempt: Callable[[dict[str, Any]], Any],
    *,
    model: str | None = None,
    cooldown_s: float = BREAKER_COOLDOWN_S,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Resolve + drive ``attempt(endpoint)`` with circuit-breaker failover (availability only).

    Walks the enabled endpoints for ``lane`` in priority order (bounded to the enabled-row count, so a
    fully-tripped fleet terminates rather than looping). On a TRANSIENT failure (``http_retry``
    classification) the endpoint's breaker trips for ``cooldown_s`` and the next endpoint is tried; a
    PERMANENT failure (4xx/config) re-raises immediately (failover cannot fix a wrong model). Each hop is
    recorded in the returned ``{failover: [...]}`` trail.

    embed-lane invariant: with a pinned ``model``, only same-model endpoints are candidates
    (:func:`resolve_endpoint` already filters), so failover is same-model-different-endpoint ONLY -- it
    NEVER crosses to a different embedding model. A caller that reaches the end of the same-model list
    gets DENIED_BACKEND_NO_ENDPOINT, never a silent cross-model vector.
    """
    _validate_resolve_request(lane, model)
    candidates = _enabled_rows_for_lane(lane, model)
    trail: list[dict[str, Any]] = []
    last_error: Exception | None = None
    for row in candidates:  # bounded: at most len(candidates) hops
        if _breaker_tripped(row["id"], clock=clock):
            trail.append({"id": row["id"], "skipped": "breaker_tripped"})
            continue
        try:
            result = attempt(row)
            return {"status": "OK", "endpoint": row, "result": result, "failover": trail}
        except Exception as error:  # noqa: BLE001 -- classified below
            last_error = error
            transient = is_transient_failure(error)
            trail.append({"id": row["id"], "error": type(error).__name__, "transient": transient})
            if not transient:
                raise  # a permanent (4xx/config) failure is not an availability gap; do not fail over
            trip_breaker(row["id"], cooldown_s=cooldown_s, clock=clock)
    # Every candidate was tripped or transiently failed (or there were none) -> availability refusal.
    raise KaizenDenied(
        "DENIED_BACKEND_NO_ENDPOINT",
        {
            "lane": lane,
            "model": model,
            "attempted": [t.get("id") for t in trail],
            "failover": trail,
            "last_error": type(last_error).__name__ if last_error is not None else None,
            "required_action": (
                "no enabled endpoint on this lane is currently reachable (all tripped/failed); "
                "check B8 --action probe, add another endpoint, or rely on the env-var backend"
            ),
        },
        exit_code=2,
    ) from last_error


# --- B8 dispatch ---------------------------------------------------------------------------------

def backend_registry(args: Any) -> dict[str, Any]:
    """B8 dispatch: --action add|list|probe|remove (default list)."""
    action = (getattr(args, "action", None) or "list").strip().lower()
    if action == "add":
        return _add(args)
    if action == "list":
        return _list(args)
    if action == "probe":
        return _probe(args)
    if action == "remove":
        return _remove(args)
    raise KaizenDenied(
        "DENIED_BACKEND_ACTION_INVALID",
        {"action": action, "allowed": ["add", "list", "probe", "remove"],
         "required_action": "pass --action add|list|probe|remove (default list)"},
        exit_code=2,
    )
