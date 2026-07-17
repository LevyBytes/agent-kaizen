"""Fleet D-op handlers (v8 M9 / D1/D2/D8; M10a / D4/D5; M10b enforcement passthroughs): argparse-Namespace in, dict out.

Mirrors :mod:`kaizen_components.session_records`. Each op:
- gates on ``KAIZEN_DIST_MODE != off`` (default ``off`` stays inert -- DENIED_DIST_MODE_OFF, exit 2);
- ROUTES via :func:`_route`: when a live daemon holds the pidfile, the op goes through the daemon
  loopback (the daemon owns the single fleet.db handle); otherwise it BREAK-GLASS direct-opens a
  FleetStore (refused if a daemon is live), does the work, and closes.

- D1 :func:`node_register`   -- register/update this node in the fleet.
- D2 :func:`node_heartbeat`  -- record a fleet node heartbeat.
- D8 :func:`fleet_digest`    -- the fleet digest (nodes + coordinator + leases projection).
- D4 :func:`coordinator_action` -- claim | transfer | release the coordinator role (M10a).
- D5 :func:`lease_action`    -- request | grant | renew | release | handoff a lease (M10a shadow).
- D7 :func:`remote_dispatch` -- request | accept | start | complete | fail | cancel | apply | list a
                                remote dispatch (M14, gate F4). apply gates on a local C4 approval.
- D9 :func:`reconcile`       -- reconcile after isolation or node loss (M15, plan §B.6): reconcile |
                                sweep | status over the daemon-held store.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from ..denials import KaizenDenied
from ..orchestration.modes import dist_mode
from ..paths import REPO_ROOT, read_text_file
from ..schemas import validate_record
from . import store as fleet_store


def _payload(args: Any) -> dict[str, Any]:
    """Parse args.payload_json or --payload-json-file JSON to a dict; refuse a non-dict payload with DENIED_PAYLOAD_TYPE (exit 2); empty/absent => {}."""
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


def _integer(value: Any, field: str) -> int | None:
    """Parse an optional integer payload field or raise a structured payload denial."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise KaizenDenied(
            "DENIED_PAYLOAD_VALUE",
            {"field": field, "required_action": f"{field} must be an integer"},
            exit_code=2,
        ) from error


def _require_dist_mode() -> str:
    """Refuse when distribution is off (the default). Keeps every off-mode path inert: no fleet.db is
    ever created, no coord row written, until an operator opts in via KAIZEN_DIST_MODE=observe|active."""
    mode = dist_mode()
    if mode == "off":
        raise KaizenDenied(
            "DENIED_DIST_MODE_OFF",
            {
                "dist_mode": mode,
                "required_action": "set KAIZEN_DIST_MODE=observe|active to use fleet ops",
            },
            exit_code=2,
        )
    return mode


def _route(op: str, op_args: dict[str, Any], *, send_request: Callable[..., dict] | None = None) -> dict[str, Any]:
    """Route a fleet op to the daemon loopback when a daemon is live, else break-glass direct-open.

    ``send_request`` is injectable (tests pass a fake to assert the loopback decision without a running
    daemon). When no daemon is live, the op runs against a break-glass FleetStore (opened, used via
    ``op_args['run']``, and closed). ``op_args['run']`` is a callable ``(store) -> dict``."""
    run: Callable[[fleet_store.FleetStore], dict[str, Any]] = op_args["run"]
    if fleet_store.daemon_is_live():
        client = send_request if send_request is not None else _loopback_send
        result = client(op, op_args.get("wire", {}))
        result.setdefault("via", "loopback")
        return result
    # Break-glass: no live daemon owns the handle, so it is safe to open one directly.
    store = fleet_store.open_store_breakglass()
    try:
        result = run(store)
    finally:
        store.close()
    result.setdefault("via", "break-glass")
    return result


def _loopback_send(op: str, wire_args: dict[str, Any]) -> dict[str, Any]:
    """Real loopback client: resolve the daemon control token + transport and send ``{op, args}``."""
    from ..orchestration import loopback
    from ..orchestration.supervisor import TOKENFILE, ensure_runtime_dir
    from ..paths import REPO_ROOT

    runtime = ensure_runtime_dir()
    if not TOKENFILE.is_file():
        raise KaizenDenied(
            "DENIED_FLEET_DAEMON_NO_TOKEN",
            {"required_action": "the daemon is live but no control token is present; restart the daemon"},
            exit_code=1,
        )
    token = TOKENFILE.read_text(encoding="utf-8").strip()
    response = loopback.send_request(
        REPO_ROOT, runtime, {"op": op, "token": token, "args": wire_args, "epoch": 0}
    )
    response.setdefault("via", "loopback")
    return response


# --- D1 node register ----------------------------------------------------------------------------

def node_register(args: Any) -> dict[str, Any]:
    """D1: register (or update) this node in the fleet. Payload {role, tailnet_name?, capabilities?}."""
    _require_dist_mode()
    payload = _payload(args)
    role = payload.get("role")
    if not role:
        raise KaizenDenied(
            "DENIED_NODE_ROLE_REQUIRED",
            {"required_action": "resubmit with --payload-json '{\"role\":\"worker\"}' (coordinator|worker|hub|model-server)"},
            exit_code=2,
        )
    tailnet_name = payload.get("tailnet_name")
    capabilities = payload.get("capabilities")
    summary = getattr(args, "summary", None) or payload.get("summary") or f"Register node as {role}."

    # Validate the record shape up front (mode-off never reaches here, so this validates a real intent).
    validate_record(
        "fleet_node",
        {
            k: v
            for k, v in {
                "node_id": fleet_store.identity.node_id(),
                "role": role,
                "tailnet_name": tailnet_name,
                "capabilities_json": capabilities if isinstance(capabilities, dict) else None,
                "summary": summary,
            }.items()
            if v not in (None, "")
        },
    )

    def run(store: fleet_store.FleetStore) -> dict[str, Any]:
        return store.register_node(role, tailnet_name=tailnet_name, capabilities=capabilities)

    return _route(
        "fleet/node-register",
        {"run": run, "wire": {"role": role, "tailnet_name": tailnet_name, "capabilities": capabilities, "summary": summary}},
    )


# --- D2 node heartbeat ---------------------------------------------------------------------------

def node_heartbeat(args: Any) -> dict[str, Any]:
    """D2: record a fleet node heartbeat (advance last_heartbeat + heartbeat/point coord_event)."""
    _require_dist_mode()

    def run(store: fleet_store.FleetStore) -> dict[str, Any]:
        return store.heartbeat()

    return _route("fleet/heartbeat", {"run": run, "wire": {}})


# --- D8 fleet digest -----------------------------------------------------------------------------

def fleet_digest(args: Any) -> dict[str, Any]:
    """D8: the fleet digest -- nodes (with heartbeat age) + coordinator + leases projection, computed
    fresh from coord_events. Optional --limit caps the nodes listed."""
    _require_dist_mode()
    limit = getattr(args, "limit", None)

    def run(store: fleet_store.FleetStore) -> dict[str, Any]:
        return store.digest(limit=limit)

    return _route("fleet/digest", {"run": run, "wire": {"limit": limit}})


# --- D4 coordinator action -----------------------------------------------------------------------

def coordinator_action(args: Any) -> dict[str, Any]:
    """D4: claim | transfer | release the coordinator role (M10b: fence + pinned-contested ENFORCE).

    --action selects the verb; transfer's target node comes from --payload-json {"to_node": ...}. The
    payload is validated against the ``coordinator_action`` schema before acting. M10b: --payload-json
    {"iso": true} stamps the §B.3 iso: sentinel on claim/transfer; a contested claim against a PINNED
    holder refuses DENIED_COORD_DIVERGED."""
    _require_dist_mode()
    action = getattr(args, "action", None)
    if not action:
        raise KaizenDenied(
            "DENIED_COORDINATOR_ACTION_REQUIRED",
            {"required_action": "pass --action claim|transfer|release"},
            exit_code=2,
        )
    payload = _payload(args)
    mode = payload.get("mode", "roaming")
    to_node = payload.get("to_node")
    isolated = bool(payload.get("iso", False))
    summary = getattr(args, "summary", None) or payload.get("summary") or f"Coordinator {action}."

    validate_record(
        "coordinator_action",
        {
            k: v
            for k, v in {
                "action": action,
                "node_id": fleet_store.identity.node_id(),
                "to_node": to_node,
                "mode": mode if action == "claim" else None,
                "iso": isolated if action in ("claim", "transfer") else None,
                "summary": summary,
            }.items()
            if v not in (None, "")
        },
    )

    if action == "transfer" and not to_node:
        raise KaizenDenied(
            "DENIED_TRANSFER_TARGET_REQUIRED",
            {"required_action": "resubmit transfer with --payload-json '{\"to_node\":\"<node_id>\"}'"},
            exit_code=2,
        )

    def run(store: fleet_store.FleetStore) -> dict[str, Any]:
        from . import coordination

        if action == "claim":
            return coordination.claim_coordinator(store, mode=mode, isolated=isolated, summary=summary)
        if action == "transfer":
            return coordination.transfer_coordinator(store, to_node, isolated=isolated, summary=summary)
        return coordination.release_coordinator(store, summary=summary)

    return _route(
        "fleet/coordinator",
        {"run": run, "wire": {"action": action, "mode": mode, "to_node": to_node, "iso": isolated, "summary": summary}},
    )


# --- D5 lease action -----------------------------------------------------------------------------

def lease_action(args: Any) -> dict[str, Any]:
    """D5: request | grant | renew | release | handoff a lease (M10b: authoritative mode ENFORCES).

    --scope carries the scope_key; grant's holder/ttl come from --payload-json {"holder":..., "ttl_s":
    ...}. --payload-json {"mode":"authoritative"} on grant|renew turns on the M10b CP mutex (contention
    ⇒ DENIED_LEASE_HELD; synced-store partition ⇒ DENIED_LEASE_HUB_UNREACHABLE); the default advisory
    keeps M10a AP behavior. handoff runs the HandoffEngine in PURE SHADOW (allow flags False =>
    patch-artifact transport, no commit, no push). The payload is validated against the ``lease_action``
    schema before acting."""
    _require_dist_mode()
    action = getattr(args, "action", None)
    if not action:
        raise KaizenDenied(
            "DENIED_LEASE_ACTION_REQUIRED",
            {"required_action": "pass --action request|grant|renew|release|handoff"},
            exit_code=2,
        )
    scope_key = getattr(args, "scope", None)
    if not scope_key:
        raise KaizenDenied(
            "DENIED_LEASE_SCOPE_REQUIRED",
            {"required_action": "pass --scope <scope_key> (e.g. <project_id>/kz/task/node)"},
            exit_code=2,
        )
    payload = _payload(args)
    holder = payload.get("holder")
    ttl_s = payload.get("ttl_s")
    ttl_value = _integer(ttl_s, "ttl_s")
    mode = payload.get("mode", "advisory")
    summary = getattr(args, "summary", None) or payload.get("summary") or f"Lease {action} for {scope_key}."

    validate_record(
        "lease_action",
        {
            k: v
            for k, v in {
                "action": action,
                "scope_key": scope_key,
                "node_id": fleet_store.identity.node_id(),
                "holder": holder,
                "ttl_s": ttl_value,
                # mode is the request|grant|renew CP/AP selector at M10b (was request-only at M10a).
                "mode": mode if action in ("request", "grant", "renew") else None,
                "summary": summary,
            }.items()
            if v not in (None, "")
        },
    )

    if action == "grant" and not holder:
        raise KaizenDenied(
            "DENIED_LEASE_HOLDER_REQUIRED",
            {"required_action": "resubmit grant with --payload-json '{\"holder\":\"<node_id>\"}'"},
            exit_code=2,
        )

    def run(store: fleet_store.FleetStore) -> dict[str, Any]:
        from . import coordination

        ttl = int(ttl_value) if ttl_value is not None else coordination.DEFAULT_TTL_S
        if action == "request":
            return coordination.request_lease(store, scope_key, mode=mode, ttl_s=ttl, summary=summary)
        if action == "grant":
            return coordination.grant_lease(store, scope_key, holder, mode=mode, ttl_s=ttl, summary=summary)
        if action == "renew":
            return coordination.renew_lease(store, scope_key, mode=mode, ttl_s=ttl, summary=summary)
        if action == "release":
            return coordination.release_lease(store, scope_key, summary=summary)
        # handoff: pure-shadow HandoffEngine (allow flags False => patch-artifact, no commit/push).
        from ..paths import WORK_ROOT

        artifact_dir = WORK_ROOT / "orchestration" / "runtime" / "handoff-artifacts"
        engine = coordination.HandoffEngine(
            store,
            str(REPO_ROOT),
            allow_wip_commit=False,
            allow_push=False,
            artifact_dir=str(artifact_dir),
        )
        return engine.shadow_handoff(scope_key)

    return _route(
        "fleet/lease",
        {
            "run": run,
            "wire": {"action": action, "scope_key": scope_key, "holder": holder, "ttl_s": ttl_s, "mode": mode, "summary": summary},
        },
    )


# --- D7 remote dispatch --------------------------------------------------------------------------

def remote_dispatch(args: Any) -> dict[str, Any]:
    """D7: request | accept | start | complete | fail | cancel | apply | list a remote dispatch (M14,
    plan §B.5, gate F4). Mirrors D4/D5 exactly: _require_dist_mode, validate the payload up front,
    _route("fleet/dispatch", ...) so a live daemon serves it over loopback and break-glass otherwise.

    --action selects the verb. request needs --scope + --payload-json {target_node, task}; accept/start/
    complete/fail/cancel/apply need --payload-json {dispatch_id, ...}. apply passes an approvals_lookup
    that reads the local C4 approval row (kaizen.db) -- the laptop's approval gates the apply into its
    OWN worktree (§B.5: "apply into the active worktree needs explicit approval")."""
    _require_dist_mode()
    action = getattr(args, "action", None)
    if not action:
        raise KaizenDenied(
            "DENIED_DISPATCH_ACTION_REQUIRED",
            {"required_action": "pass --action request|accept|start|complete|fail|cancel|apply|list"},
            exit_code=2,
        )
    payload = _payload(args)
    scope_key = getattr(args, "scope", None) or payload.get("scope_key")
    target_node = payload.get("target_node")
    task = payload.get("task")
    dispatch_id = payload.get("dispatch_id") or getattr(args, "id", None)
    reason = payload.get("reason")
    summary = getattr(args, "summary", None) or payload.get("summary") or f"Dispatch {action}."

    # Validate the record shape up front (mode-off never reaches here, so this validates a real intent).
    validate_record(
        "remote_dispatch",
        {
            k: v
            for k, v in {
                "action": action,
                "target_node": target_node if action == "request" else None,
                "task": task if action == "request" else None,
                "scope_key": scope_key,
                "dispatch_id": dispatch_id,
                "reason": reason if action in ("fail", "cancel") else None,
                "summary": summary,
            }.items()
            if v not in (None, "")
        },
    )

    if action == "request" and not (target_node and task and scope_key):
        raise KaizenDenied(
            "DENIED_DISPATCH_FIELDS_REQUIRED",
            {"required_action": "request needs --scope and --payload-json '{\"target_node\":..., \"task\":...}'"},
            exit_code=2,
        )
    if action in ("accept", "start", "complete", "fail", "cancel", "apply") and not dispatch_id:
        raise KaizenDenied(
            "DENIED_DISPATCH_ID_REQUIRED",
            {"required_action": "resubmit with --payload-json '{\"dispatch_id\":\"<id>\"}'"},
            exit_code=2,
        )
    if action == "apply":
        approval_id = payload.get("approval_id")
        artifact_path = payload.get("artifact_path")
        if not (approval_id and artifact_path):
            raise KaizenDenied(
                "DENIED_DISPATCH_APPLY_FIELDS_REQUIRED",
                {"required_action": "apply needs --payload-json '{\"dispatch_id\":..., \"approval_id\":..., \"artifact_path\":...}'"},
                exit_code=2,
            )

    def run(store: fleet_store.FleetStore) -> dict[str, Any]:
        from . import dispatch_remote

        if action == "request":
            return dispatch_remote.request_dispatch(
                store, target_node=target_node, task=task, scope_key=scope_key,
                required_leases=payload.get("required_leases"), summary=summary,
            )
        if action == "accept":
            return dispatch_remote.accept_dispatch(store, dispatch_id, summary=summary)
        if action == "start":
            return dispatch_remote.start_dispatch(store, dispatch_id, summary=summary)
        if action == "complete":
            return dispatch_remote.complete_dispatch(
                store, dispatch_id,
                artifact_name=payload.get("artifact_name"), artifact_sha=payload.get("artifact_sha"),
                branch=payload.get("branch"), summary=summary,
            )
        if action == "fail":
            return dispatch_remote.fail_dispatch(store, dispatch_id, reason or "unspecified", summary=summary)
        if action == "cancel":
            return dispatch_remote.cancel_dispatch(store, dispatch_id, reason=reason, summary=summary)
        if action == "apply":
            return dispatch_remote.apply_dispatch(
                store, None, dispatch_id,
                artifact_path=payload["artifact_path"],
                approval_id=payload["approval_id"],
                approvals_lookup=_local_approval_lookup,
                repo_root=str(REPO_ROOT),
                summary=summary,
            )
        return dispatch_remote.list_dispatches(store)

    return _route(
        "fleet/dispatch",
        {
            "run": run,
            "wire": {
                "action": action, "scope_key": scope_key, "target_node": target_node, "task": task,
                "dispatch_id": dispatch_id, "reason": reason,
                "artifact_name": payload.get("artifact_name"), "artifact_sha": payload.get("artifact_sha"),
                "branch": payload.get("branch"), "required_leases": payload.get("required_leases"),
                "approval_id": payload.get("approval_id"), "artifact_path": payload.get("artifact_path"),
                "summary": summary,
            },
        },
    )


def _local_approval_lookup(approval_id: str) -> dict[str, Any] | None:
    """Read a local C4 approval row (kaizen.db) as the ``{state, ...}`` shape apply_dispatch gates on.
    The dispatcher's OWN approval (on THIS laptop) authorizes applying a returned patch into THIS
    worktree -- so the lookup reads kaizen.db (never fleet.db); a missing approval returns None
    (⇒ DENIED_DISPATCH_APPLY_UNAPPROVED)."""
    from .. import db

    row = db.fetch_one(
        "SELECT id, session_id, state, decided_by FROM approval_requests WHERE id = ?", (approval_id,)
    )
    if row is None:
        return None
    return {"id": row[0], "session_id": row[1], "state": row[2], "decided_by": row[3]}


# --- D9 reconcile --------------------------------------------------------------------------------

def reconcile(args: Any) -> dict[str, Any]:
    """D9: reconcile after isolation or node loss (M15, plan §B.6). Mirrors D4/D5/D7 exactly:
    _require_dist_mode, validate the payload up front, _route("fleet/reconcile", ...) so a live daemon
    serves it over loopback and break-glass otherwise.

    --action selects the leg (default ``reconcile``):
    - ``reconcile`` -- pull the coord ledger, (optionally) git-fetch, then auto-adopt this node's iso
      scopes or refuse ``DENIED_ISO_CONFLICT`` on overlap (surface-and-confirm);
    - ``sweep`` -- reclaim heartbeat-stale nodes' leases + cancel their non-terminal dispatches;
    - ``status`` -- this replica's isolation posture (NOTE: status PULLS as its probe).

    Payload {hub_remote?, stale_after_s?, skew_margin_s?, allow_publish?}. The git_runner is passed ONLY
    when a hub_remote is EXPLICITLY provided (payload or the ``KAIZEN_GIT_HUB_REMOTE`` env) -- the public
    CLI never defaults git operations onto the real repo; with a hub_remote the runner is built on
    paths.REPO_ROOT and fetch/publish target that explicit operator remote. ``allow_publish`` stays False
    from the CLI: publication is owner-gated, no CLI flag enables it at M15."""
    _require_dist_mode()
    action = getattr(args, "action", None) or "reconcile"
    if action not in ("reconcile", "sweep", "status"):
        raise KaizenDenied(
            "DENIED_RECONCILE_ACTION_REQUIRED",
            {"action": action, "required_action": "pass --action reconcile|sweep|status"},
            exit_code=2,
        )
    payload = _payload(args)
    # hub_remote comes from the payload, falling back to the env the mirror layer reads.
    hub_remote = payload.get("hub_remote") or os.environ.get("KAIZEN_GIT_HUB_REMOTE") or None
    stale_after_s = payload.get("stale_after_s")
    skew_margin_s = payload.get("skew_margin_s")
    stale_value = _integer(stale_after_s, "stale_after_s")
    skew_value = _integer(skew_margin_s, "skew_margin_s")
    summary = getattr(args, "summary", None) or payload.get("summary") or f"Reconcile {action}."

    validate_record(
        "fleet_reconcile",
        {
            k: v
            for k, v in {
                "action": action,
                "hub_remote": hub_remote if action == "reconcile" else None,
                "stale_after_s": int(stale_value) if stale_value is not None else None,
                "skew_margin_s": int(skew_value) if skew_value is not None else None,
                # allow_publish is owner-gated -- the CLI never enables it, so it is recorded False.
                "allow_publish": False,
                "summary": summary,
            }.items()
            if v not in (None, "")
        },
    )

    def run(store: fleet_store.FleetStore) -> dict[str, Any]:
        from . import coordination, reconcile as reconcile_engine

        if action == "status":
            return {"status": "OK", **reconcile_engine.offline_status(store)}
        if action == "sweep":
            kwargs: dict[str, Any] = {}
            if stale_value is not None:
                kwargs["stale_after_s"] = float(stale_value)
            if skew_value is not None:
                kwargs["skew_margin_s"] = float(skew_value)
            return reconcile_engine.sweep_stale_nodes(store, **kwargs)
        # reconcile: a git_runner is built ONLY when the operator gave an explicit hub_remote (fetch/
        # publish target that remote); allow_publish stays False from the CLI (owner-gated).
        git_runner = coordination._default_git_runner(str(REPO_ROOT)) if hub_remote else None
        return reconcile_engine.reconcile(
            store, git_runner, hub_remote=hub_remote, allow_publish=False
        )

    return _route(
        "fleet/reconcile",
        {
            "run": run,
            "wire": {
                "action": action, "hub_remote": hub_remote,
                "stale_after_s": stale_after_s, "skew_margin_s": skew_margin_s, "summary": summary,
            },
        },
    )
