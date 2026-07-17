"""ComfyUI workflow backend (generative runs, Layer: assets/proof).

Drives a local ComfyUI server over its stdlib-HTTP API to produce reproducible,
recorded assets. The agent authors the workflow JSON; durable runs go through
``run_workflow`` (Y1), which captures a replayable record (graph hash + stored
workflow artifact + seed + checkpoints + output hashes) in ``generative_runs``,
registers each output in ``artifacts``, and writes a gateway + trace event.

ComfyUI is an OPTIONAL, capability-activated backend: the external server is the
dependency (this client is stdlib ``urllib`` only), so an unreachable server fails
gracefully with ``DENIED_BACKEND_UNAVAILABLE``. ``--dry-run`` records a ``queued``
run from the workflow alone, with no network call (testing without a GPU/server).
"""

from __future__ import annotations

import json
import ipaddress
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .backends.http_retry import http_request
from .db import fetch_all, fetch_one, get_setting, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import file_sha256, utc_text_hash, validate_text_fields
from .paths import GENERATED_ROOT, REPO_ROOT, repo_relative, resolve_user_path
from .redaction import assert_redacted
from .schemas import validate_record
from .task_records import _text_arg


_DEFAULT_URL = "http://127.0.0.1:8188"
_PROBE_TIMEOUT = 5.0
_POLL_TIMEOUT = 15.0
_SUBMIT_TIMEOUT = 30.0
_FETCH_TIMEOUT = 120.0


def _slug(name: str) -> str:
    """Return a lowercase filesystem-safe label with a default fallback."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip().lower()) or "default"


def _endpoint(args: Any) -> str:
    """Resolve endpoint precedence from argument, environment, then loopback default."""
    raw = getattr(args, "endpoint", None) or os.environ.get("KAIZEN_COMFYUI_URL") or _DEFAULT_URL
    url = raw.rstrip("/")
    _require_loopback(url)
    return url


def _is_loopback(url: str) -> bool:
    """Return whether the parsed hostname is a recognized loopback name or address."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_loopback(url: str) -> None:
    """Deny non-HTTP or non-loopback ComfyUI endpoints before any network access."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or not _is_loopback(url):
        raise KaizenDenied(
            "DENIED_COMFYUI_ENDPOINT_NON_LOOPBACK",
            {
                "endpoint": url,
                "required_action": "use an http://localhost, http://127.0.0.0/8, or http://[::1] ComfyUI endpoint",
            },
            exit_code=2,
        )


def _timeout_arg(args: Any, name: str, default: float) -> float:
    """Return a positive timeout argument without treating explicit zero as absent."""
    raw = getattr(args, name, None)
    value = default if raw is None else float(raw)
    if value <= 0:
        raise KaizenDenied(
            "DENIED_TIMEOUT_INVALID",
            {"argument": name.replace("_", "-"), "value": value, "required_action": "pass a positive timeout in seconds"},
            exit_code=2,
        )
    return value


def _backend_unreachable(url: str, error: Exception) -> KaizenDenied:
    return KaizenDenied(
        "DENIED_BACKEND_UNAVAILABLE",
        {
            "backend": "comfyui",
            "endpoint": url,
            "reason": str(error),
            "required_action": "start ComfyUI (see setup/COMFYUI.md) or pass --endpoint; default is http://127.0.0.1:8188",
        },
        exit_code=2,
    )


def _http_json(url: str, *, data: bytes | None = None, timeout: float = 30.0) -> Any:
    """Returns parsed JSON (dict/list/scalar) or `{}` on empty body; raises via `http_request` on transient-exhausted/non-transient network error; POSTs when `data` is given."""
    _require_loopback(url)
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers)
    raw = http_request(req, timeout=timeout, redirect_validator=_require_loopback)
    return json.loads(raw.decode("utf-8")) if raw else {}


def _http_bytes(url: str, *, timeout: float = 120.0) -> bytes:
    """Returns raw response bytes (used for `/view` binary outputs); no JSON parsing; propagates network errors."""
    _require_loopback(url)
    return http_request(
        urllib.request.Request(url), timeout=timeout, redirect_validator=_require_loopback,
    )


def probe(url: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Hit `/system_stats`; preserve structured validation denials, map transport failures to DENIED_BACKEND_UNAVAILABLE, and return `{}` for a non-dict response."""
    try:
        stats = _http_json(f"{url}/system_stats", timeout=timeout)
    except KaizenDenied:
        raise
    except Exception as error:  # noqa: BLE001 -- any failure means "unreachable"
        raise _backend_unreachable(url, error) from error
    return stats if isinstance(stats, dict) else {}


def fetch_object_info(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Hit `/object_info` with probe's validation/transport denial split for workflow validation."""
    try:
        info = _http_json(f"{url}/object_info", timeout=timeout)
    except KaizenDenied:
        raise
    except Exception as error:  # noqa: BLE001 -- any failure means "unreachable"
        raise _backend_unreachable(url, error) from error
    return info if isinstance(info, dict) else {}


_ASSET_INPUT_KEYS = ("ckpt_name", "unet_name", "vae_name", "lora_name", "model_name")


def validate_workflow(url: str, workflow: dict[str, Any]) -> dict[str, Any]:
    """Validate node class types + referenced model assets against live ``/object_info``.

    Unknown node types deny before submit (missing custom nodes); a model input whose value
    is absent from that loader's choice list denies as a missing, approval-gated asset."""
    info = fetch_object_info(url)
    unknown = sorted(
        {str(node.get("class_type")) for node in workflow.values()
         if isinstance(node, dict) and node.get("class_type") not in info}
    )
    if unknown:
        raise KaizenDenied(
            "DENIED_WORKFLOW_NODES_UNKNOWN",
            {"unknown_class_types": unknown,
             "required_action": "install the missing custom nodes in the managed runtime or fix the workflow graph"},
            exit_code=2,
        )
    missing: list[dict[str, str]] = []
    for node in workflow.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        class_type = str(node.get("class_type"))
        node_spec = info.get(class_type)
        spec = node_spec.get("input", {}) if isinstance(node_spec, dict) else {}
        merged = {**(spec.get("required") or {}), **(spec.get("optional") or {})}
        for key in _ASSET_INPUT_KEYS:
            value = node["inputs"].get(key)
            choice_spec = merged.get(key)
            if not isinstance(value, str) or not value or not isinstance(choice_spec, list) or not choice_spec:
                continue
            choices = choice_spec[0]
            if isinstance(choices, list) and value not in choices:
                missing.append({"input": key, "asset": value, "class_type": class_type})
    if missing:
        raise KaizenDenied(
            "DENIED_ASSET_MISSING",
            {"missing_assets": missing,
             "expected_dir": "the managed runtime's models/ subdir matching the input (e.g. models/checkpoints)",
             "required_action": "owner approval required before any model download; consult the model-candidate "
                                "registry (S2/S3) for source URL + license, place the approved file, then restart the runtime"},
            exit_code=2,
        )
    return {"nodes": len(workflow), "unknown_class_types": [], "missing_assets": []}


def comfy_doctor(args: Any) -> dict[str, Any]:
    """Y5: probe the configured ComfyUI endpoint; the capability gate."""
    url = _endpoint(args)
    stats = probe(url, timeout=_timeout_arg(args, "probe_timeout", _PROBE_TIMEOUT))
    system = stats.get("system", {}) if isinstance(stats, dict) else {}
    return {
        "status": "OK",
        "backend": "comfyui",
        "endpoint": url,
        "loopback": _is_loopback(url),
        "reachable": True,
        "comfyui_version": system.get("comfyui_version"),
        "python_version": system.get("python_version"),
    }


def _load_workflow(args: Any) -> dict[str, Any]:
    src = getattr(args, "payload_json_file", None) or getattr(args, "path", None)
    if not src:
        raise KaizenDenied(
            "DENIED_PATH_REQUIRED",
            {"required_action": "pass --path (ComfyUI prompt JSON) or --payload-json-file"},
            exit_code=2,
        )
    path = resolve_user_path(str(src), require_file=True, repo_only=True)
    try:
        workflow = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise KaizenDenied(
            "DENIED_JSON_INVALID",
            {"path": str(path), "reason": str(error), "required_action": "supply valid ComfyUI prompt JSON"},
            exit_code=2,
        ) from error
    if not isinstance(workflow, dict) or not workflow:
        raise KaizenDenied(
            "DENIED_WORKFLOW_TYPE",
            {"required_action": "ComfyUI prompt JSON must be a non-empty JSON object (the API 'prompt' graph)"},
            exit_code=2,
        )
    return workflow


def _extract_run_meta(workflow: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Pull the primary seed + checkpoint/model names out of a ComfyUI prompt graph."""
    seeds: list[str] = []
    models: list[str] = []
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for seed_key in ("seed", "noise_seed"):
            value = inputs.get(seed_key)
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                or isinstance(value, float)
                and value.is_integer()
            ):
                seeds.append(str(int(value)))
        for model_key in ("ckpt_name", "model_name", "unet_name", "vae_name", "lora_name"):
            value = inputs.get(model_key)
            if isinstance(value, str) and value:
                models.append(value)
    return (seeds[0] if seeds else None), sorted(set(models))


def submit(url: str, workflow: dict[str, Any], client_id: str, *, timeout: float = 30.0) -> str:
    """POST the prompt graph and return its id; map rejection separately from unreachability."""
    body = json.dumps({"prompt": workflow, "client_id": client_id}).encode("utf-8")
    try:
        resp = _http_json(f"{url}/prompt", data=body, timeout=timeout)
    except KaizenDenied:
        raise
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace") if error.fp else ""
        raise KaizenDenied(
            "DENIED_WORKFLOW_REJECTED",
            {"endpoint": url, "http_status": error.code, "reason": detail[:500], "required_action": "fix the workflow graph; ComfyUI rejected it"},
            exit_code=2,
        ) from error
    except Exception as error:  # noqa: BLE001
        raise _backend_unreachable(url, error) from error
    prompt_id = resp.get("prompt_id") if isinstance(resp, dict) else None
    if not prompt_id:
        raise KaizenDenied(
            "DENIED_WORKFLOW_REJECTED",
            {"endpoint": url, "response": resp, "required_action": "ComfyUI did not return a prompt_id"},
            exit_code=2,
        )
    return str(prompt_id)


def wait(url: str, prompt_id: str, *, timeout: float = 300.0) -> dict[str, Any]:
    """Polls `/history/{prompt_id}` every ~1s until an entry appears or `timeout`; raises DENIED_RUN_TIMEOUT on deadline; per-poll HTTP timeout is 15s (independent of the outer deadline)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            history = _http_json(f"{url}/history/{prompt_id}", timeout=_POLL_TIMEOUT)
        except KaizenDenied:
            raise
        except Exception as error:  # noqa: BLE001
            raise _backend_unreachable(url, error) from error
        entry = history.get(prompt_id) if isinstance(history, dict) else None
        if entry:
            return entry
        time.sleep(1.0)
    raise KaizenDenied(
        "DENIED_RUN_TIMEOUT",
        {"prompt_id": prompt_id, "timeout_s": timeout, "required_action": "raise --timeout or check the ComfyUI queue"},
        exit_code=1,
    )


def fetch_outputs(url: str, entry: dict[str, Any], dest_dir: Path, *, timeout: float = 120.0) -> list[Path]:
    """Fetch every file reference in the run's outputs, whatever the output key.

    Node packs emit outputs under arbitrary keys ("images", "gifs", "audio", "video",
    "3d", ...); anything shaped ``{filename, subfolder?, type?}`` is fetchable via ``/view``,
    so collect by shape, not by key name."""
    saved: list[Path] = []
    used_names: set[str] = set()
    outputs = entry.get("outputs", {}) if isinstance(entry, dict) else {}
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        for output_key, refs in node_output.items():
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if not isinstance(ref, dict) or not ref.get("filename"):
                    continue
                filename = str(ref["filename"])
                query = urllib.parse.urlencode(
                    {"filename": filename, "subfolder": ref.get("subfolder", ""), "type": ref.get("type", "output")}
                )
                data = _http_bytes(f"{url}/view?{query}", timeout=timeout)
                basename = Path(filename).name
                candidate = basename
                suffix = 1
                while candidate.casefold() in used_names:
                    candidate = f"{output_key}-{suffix}-{basename}"
                    suffix += 1
                used_names.add(candidate.casefold())
                target = dest_dir / candidate
                target.write_bytes(data)
                saved.append(target)
    return saved


def _insert_artifact(conn: Any, *, artifact_id: str, created: str, task_id: str | None, kind: str, path: Path, summary: str, is_test: int = 0) -> None:
    """Inserts one `artifacts` row; hashes the file if present (sha256+bytes), else nulls; stores repo-relative path; empty body. Idempotency/caller-transaction expectation (must run inside `write_tx`)."""
    sha = file_sha256(path) if path.is_file() else None
    size = path.stat().st_size if path.is_file() else None
    rel = repo_relative(path)
    content_hash = utc_text_hash({"id": artifact_id, "path": rel, "sha256": sha})
    conn.execute(
        "INSERT INTO artifacts (id, created_at, task_id, kind, path, sha256, bytes, summary, body, content_hash, is_test) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (artifact_id, created, task_id, kind, rel, sha, size, summary, "", content_hash, is_test),
    )


def run_workflow(args: Any) -> dict[str, Any]:
    """Y1: submit + record a reproducible ComfyUI run (or `--dry-run` to record without a server)."""
    url = _endpoint(args)
    dry_run = bool(getattr(args, "dry_run", False))
    validate = bool(getattr(args, "validate", False))
    if dry_run and validate:
        raise KaizenDenied("DENIED_FLAG_CONFLICT", {"required_action": "--validate needs a live server; drop --dry-run"}, exit_code=2)
    workflow = _load_workflow(args)
    workflow_hash = utc_text_hash(workflow)
    template = _slug(getattr(args, "template", None) or "default")
    seed, models = _extract_run_meta(workflow)

    summary = _text_arg(args, "summary", f"ComfyUI run ({template}).")
    assert_redacted({"summary": summary, "template": template})
    validate_text_fields({"summary": summary, "body": ""})

    run_id = new_id("gr")
    created = now()
    dest_dir = GENERATED_ROOT / template
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Store the workflow itself as a replayable artifact (so Y4 can re-submit it).
    workflow_artifact_id = new_id("a")
    workflow_path = dest_dir / f"{run_id}-workflow.json"
    workflow_path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    prompt_id: str | None = None
    status = "queued"
    latency_ms: int | None = None
    output_paths: list[Path] = []
    runtime_profile: str | None = None

    if not dry_run:
        stats = probe(url, timeout=_timeout_arg(args, "probe_timeout", _PROBE_TIMEOUT))
        system = stats.get("system", {}) if isinstance(stats, dict) else {}
        runtime_profile = f"comfyui={system.get('comfyui_version')};python={system.get('python_version')}"
        if validate:
            validate_workflow(url, workflow)
        started = time.monotonic()
        prompt_id = submit(url, workflow, uuid.uuid4().hex, timeout=_SUBMIT_TIMEOUT)
        entry = wait(url, prompt_id, timeout=_timeout_arg(args, "timeout", 300.0))
        output_paths = fetch_outputs(url, entry, dest_dir, timeout=_FETCH_TIMEOUT)
        latency_ms = int((time.monotonic() - started) * 1000)
        run_status = (entry.get("status") or {}).get("status_str") if isinstance(entry.get("status"), dict) else None
        status = "failed" if run_status == "error" else ("completed" if output_paths else "completed_no_output")

    route = getattr(args, "route", None) or "api"
    return _record_run(
        args=args, url=url, workflow_hash=workflow_hash, template=template, status=status,
        prompt_id=prompt_id, output_paths=output_paths, latency_ms=latency_ms, seed=seed, models=models,
        dest_dir=dest_dir, workflow_path=workflow_path, workflow_artifact_id=workflow_artifact_id,
        run_id=run_id, created=created, summary=summary, route=route, runtime_profile=runtime_profile,
    )


def _record_run(*, args: Any, url: str, workflow_hash: str, template: str, status: str,
                prompt_id: str | None, output_paths: list[Path], latency_ms: int | None,
                seed: str | None, models: list[str], dest_dir: Path, workflow_path: Path,
                workflow_artifact_id: str, run_id: str, created: str, summary: str,
                route: str = "api", runtime_profile: str | None = None,
                mcp_candidate: str | None = None, mcp_version: str | None = None) -> dict[str, Any]:
    """Persist a generative run + its route row + artifacts + gateway/trace events.

    Shared by the api route (``run_workflow``) and the mcp route (``comfy_mcp.mcp_generate``)
    so both lanes produce shape-identical records; only the route row differs."""
    task_id = getattr(args, "task_id", None)
    ab_pair_id = getattr(args, "ab_pair_id", None)
    is_test = 1 if getattr(args, "test", False) else 0
    output_artifacts = [(new_id("a"), path) for path in output_paths]
    output_artifact_ids = [aid for aid, _ in output_artifacts]

    payload = {
        "task_id": task_id,
        "backend": "comfyui",
        "template": template,
        "endpoint": url,
        "workflow_hash": workflow_hash,
        "workflow_artifact_id": workflow_artifact_id,
        "seed": seed,
        "models": models,
        "status": status,
        "prompt_id": prompt_id,
        "output_artifact_ids": output_artifact_ids,
        "output_dir": repo_relative(dest_dir),
        "latency_ms": latency_ms,
        "summary": summary,
    }
    validate_record("generative_run", {k: v for k, v in payload.items() if v is not None})

    route_payload: dict[str, Any] = {"run_id": run_id, "route": route}
    for key, value in (("runtime_profile", runtime_profile), ("mcp_candidate", mcp_candidate),
                       ("mcp_version", mcp_version), ("ab_pair_id", ab_pair_id)):
        if value is not None:
            route_payload[key] = value
    validate_record("generative_run_route", route_payload)

    gateway_payload = {
        "event_type": "comfyui_run",
        "status": status,
        "summary": summary,
        "payload": {"run_id": run_id, "endpoint": url, "workflow_hash": workflow_hash, "prompt_id": prompt_id, "output_count": len(output_paths)},
    }
    validate_record("gateway_event", gateway_payload)

    content_hash = utc_text_hash({"id": run_id, "workflow_hash": workflow_hash, "status": status, "outputs": output_artifact_ids})
    gateway_id = new_id("age")
    gateway_hash = utc_text_hash({"id": gateway_id, "event_type": "comfyui_run", "run": run_id, "status": status})
    trace_id = new_id("te")
    trace_hash = utc_text_hash({"id": trace_id, "run": run_id, "kind": "generative_run"})
    models_json = json.dumps(models)
    output_ids_json = json.dumps(output_artifact_ids)
    rel_dir = repo_relative(dest_dir)

    def op(conn: Any, _attempt: int) -> None:
        _insert_artifact(conn, artifact_id=workflow_artifact_id, created=created, task_id=task_id, kind="comfyui_workflow", path=workflow_path, summary=f"ComfyUI workflow for run {run_id}.", is_test=is_test)
        for aid, path in output_artifacts:
            _insert_artifact(conn, artifact_id=aid, created=created, task_id=task_id, kind="comfyui_output", path=path, summary=f"ComfyUI output {path.name} ({template}).", is_test=is_test)
        conn.execute(
            "INSERT INTO generative_runs "
            "(id, created_at, task_id, backend, template, endpoint, workflow_hash, workflow_artifact_id, seed, "
            "models_json, status, prompt_id, output_artifact_ids_json, output_dir, latency_ms, summary, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, created, task_id, "comfyui", template, url, workflow_hash, workflow_artifact_id, seed,
             models_json, status, prompt_id, output_ids_json, rel_dir, latency_ms, summary, content_hash, is_test),
        )
        conn.execute(
            "INSERT INTO generative_run_routes "
            "(run_id, created_at, route, runtime_profile, mcp_candidate, mcp_version, ab_pair_id, payload_json, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, created, route, runtime_profile, mcp_candidate, mcp_version, ab_pair_id, None, is_test),
        )
        conn.execute(
            "INSERT INTO agentgateway_events (id, created_at, event_type, status, summary, body, payload_json, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gateway_id, created, "comfyui_run", status, summary, "", json.dumps(gateway_payload["payload"]), gateway_hash, is_test),
        )
        conn.execute(
            "INSERT INTO trace_events "
            "(id, created_at, task_id, trace_id, kind, level, status, redaction_status, summary, body, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trace_id, created, task_id, run_id, "generative_run", "default", status, "scanned_clean", summary, "", trace_hash, is_test),
        )

    write_tx(op)
    return {
        "status": "OK",
        "id": run_id,
        "run_status": status,
        "dry_run": bool(getattr(args, "dry_run", False)),
        "backend": "comfyui",
        "endpoint": url,
        "workflow_hash": workflow_hash,
        "workflow_artifact_id": workflow_artifact_id,
        "seed": seed,
        "models": models,
        "prompt_id": prompt_id,
        "outputs": [repo_relative(path) for path in output_paths],
        "output_artifact_ids": output_artifact_ids,
        "output_dir": rel_dir,
        "latency_ms": latency_ms,
        "route": route,
        "ab_pair_id": ab_pair_id,
    }


def comfy_inspect(args: Any) -> dict[str, Any]:
    """Y2: full record for one generative run."""
    run_id = getattr(args, "id", None)
    if not run_id:
        raise KaizenDenied("DENIED_ID_REQUIRED", {"required_action": "resubmit with --id (run id)"}, exit_code=2)
    row = fetch_one("SELECT * FROM generative_runs WHERE id = ?", (run_id,))
    if row is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": run_id, "table": "generative_runs"}, exit_code=1)
    columns = [r[1] for r in fetch_all("PRAGMA table_info(generative_runs)")]
    record = dict(zip(columns, row))
    route_row = fetch_one(
        "SELECT route, runtime_profile, mcp_candidate, mcp_version, ab_pair_id FROM generative_run_routes WHERE run_id = ?",
        (run_id,),
    )
    if route_row is not None:
        record["route_info"] = {
            "route": route_row[0],
            "runtime_profile": route_row[1],
            "mcp_candidate": route_row[2],
            "mcp_version": route_row[3],
            "ab_pair_id": route_row[4],
        }
    return {"status": "OK", "record": record}


def comfy_list(args: Any) -> dict[str, Any]:
    """Y3: recent generative runs (optionally filtered by --task-id)."""
    limit = int(getattr(args, "limit", None) or 20)
    task_id = getattr(args, "task_id", None)
    cols = "id, created_at, template, status, workflow_hash, output_dir, summary"
    if task_id:
        rows = fetch_all(
            f"SELECT {cols} FROM generative_runs WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
            (task_id, limit),
        )
    else:
        rows = fetch_all(f"SELECT {cols} FROM generative_runs ORDER BY created_at DESC LIMIT ?", (limit,))
    return {
        "status": "OK",
        "records": [
            {"id": r[0], "created_at": r[1], "template": r[2], "status": r[3], "workflow_hash": r[4], "output_dir": r[5], "summary": r[6]}
            for r in rows
        ],
    }


def comfy_replay(args: Any) -> dict[str, Any]:
    """Y4: re-submit a prior run's stored workflow (reproducible via identical workflow_hash)."""
    run_id = getattr(args, "id", None)
    if not run_id:
        raise KaizenDenied("DENIED_ID_REQUIRED", {"required_action": "resubmit with --id (run to replay)"}, exit_code=2)
    row = fetch_one("SELECT workflow_artifact_id, template FROM generative_runs WHERE id = ?", (run_id,))
    if row is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": run_id, "table": "generative_runs"}, exit_code=1)
    artifact = fetch_one("SELECT path FROM artifacts WHERE id = ?", (row[0],))
    if artifact is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": row[0], "table": "artifacts", "required_action": "the stored workflow artifact is missing"}, exit_code=1)
    workflow_path = REPO_ROOT / artifact[0]
    args.path = str(workflow_path)
    args.payload_json_file = None
    if not getattr(args, "template", None):
        args.template = row[1]
    result = run_workflow(args)
    result["replay_of"] = run_id
    return result


_PROMPT_TOKEN = "{{PROMPT}}"


def _substitute_prompt(workflow: dict[str, Any], prompt: str) -> int:
    """Replace the placeholder in every string node input; return the replacement count."""
    count = 0
    for node in workflow.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for key, value in inputs.items():
            if isinstance(value, str) and _PROMPT_TOKEN in value:
                inputs[key] = value.replace(_PROMPT_TOKEN, prompt)
                count += 1
    return count


def _override_seed(workflow: dict[str, Any], seed: int) -> int:
    count = 0
    for node in workflow.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for key in ("seed", "noise_seed"):
            if key in inputs and isinstance(inputs[key], (int, float)) and not isinstance(inputs[key], bool):
                inputs[key] = seed
                count += 1
    return count


def comfy_generate(args: Any) -> dict[str, Any]:
    """Y8: generate via the api or mcp route, with optional ``{{PROMPT}}`` + seed override.

    Resolves the workflow (placeholder + seed), stores the RESOLVED graph so replay/hash see it,
    then dispatches: api -> run_workflow; mcp -> comfy_mcp.mcp_generate (same recording path)."""
    workflow = _load_workflow(args)
    prompt = getattr(args, "prompt", None)
    if prompt:
        if _substitute_prompt(workflow, prompt) == 0:
            raise KaizenDenied(
                "DENIED_PROMPT_PLACEHOLDER_MISSING",
                {"placeholder": _PROMPT_TOKEN, "required_action": "put {{PROMPT}} in a string input of the workflow, or drop --prompt"},
                exit_code=2,
            )
    seed = getattr(args, "seed", None)
    if seed is not None:
        _override_seed(workflow, int(seed))

    template = _slug(getattr(args, "template", None) or "default")
    dest_dir = GENERATED_ROOT / template
    dest_dir.mkdir(parents=True, exist_ok=True)
    resolved_path = dest_dir / f"y8-{new_id('wf')}.json"
    resolved_path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.path = str(resolved_path)
    args.payload_json_file = None

    route = getattr(args, "route", None) or "api"
    if route == "api":
        return run_workflow(args)
    from .comfy_mcp import mcp_generate

    return mcp_generate(args, workflow)


def _parity(api_result: dict[str, Any], mcp_result: dict[str, Any]) -> dict[str, Any]:
    def hashes(result: dict[str, Any]) -> list[str]:
        out = []
        for rel in result.get("outputs", []) or []:
            path = REPO_ROOT / rel
            if path.is_file():
                out.append(file_sha256(path))
        return sorted(out)

    api_hashes, mcp_hashes = hashes(api_result), hashes(mcp_result)
    api_latency, mcp_latency = api_result.get("latency_ms"), mcp_result.get("latency_ms")
    delta = (mcp_latency - api_latency) if (api_latency is not None and mcp_latency is not None) else None
    return {
        "both_completed": api_result.get("run_status") == "completed" and mcp_result.get("run_status") == "completed",
        "output_counts": [len(api_result.get("outputs", []) or []), len(mcp_result.get("outputs", []) or [])],
        "identical_hashes": bool(api_hashes) and api_hashes == mcp_hashes,
        "latency_ms": {"api": api_latency, "mcp": mcp_latency, "delta": delta},
    }


def _write_parity_report(args: Any, ab_pair_id: str, api_result: dict[str, Any], mcp_result: dict[str, Any], parity: dict[str, Any]) -> Path:
    report_dir = GENERATED_ROOT / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"parity-{ab_pair_id}.md"
    lines = [
        f"# ComfyUI api-vs-mcp Parity {ab_pair_id}",
        "",
        f"- api run: {api_result['id']} status={api_result.get('run_status')} latency_ms={api_result.get('latency_ms')}",
        f"- mcp run: {mcp_result['id']} status={mcp_result.get('run_status')} latency_ms={mcp_result.get('latency_ms')}",
        f"- both_completed: {parity['both_completed']}",
        f"- output_counts (api, mcp): {parity['output_counts']}",
        f"- identical_hashes: {parity['identical_hashes']}",
        f"- latency delta (mcp - api) ms: {parity['latency_ms']['delta']}",
        "",
        "Identical workflow + seed against the same runtime should yield identical output hashes; a mismatch is a recorded finding, not a failure.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    artifact_id = new_id("a")
    created = now()
    is_test = 1 if getattr(args, "test", False) else 0

    def op(conn: Any, _attempt: int) -> None:
        _insert_artifact(conn, artifact_id=artifact_id, created=created, task_id=getattr(args, "task_id", None),
                         kind="comfy_parity_report", path=path, summary=f"api-vs-mcp parity report {ab_pair_id}.", is_test=is_test)

    write_tx(op)
    return path


def comfy_ab_run(args: Any) -> dict[str, Any]:
    """Y9: identical workflow + seed through the api then mcp route; write an operational parity report."""
    import copy

    workflow = _load_workflow(args)  # DENIED_PATH_REQUIRED first -> bare Y9 stays structured
    seed = getattr(args, "seed", None)
    if seed is None:
        seed, _models = _extract_run_meta(workflow)
        if seed is None:
            raise KaizenDenied(
                "DENIED_SEED_REQUIRED",
                {"required_action": "pass --seed or put a fixed seed in the workflow; A/B needs identical seeds"},
                exit_code=2,
            )
    # Pin check BEFORE running the api lane so a missing pin never half-records.
    if not (getattr(args, "candidate", None) or get_setting("active_comfy_mcp")):
        raise KaizenDenied(
            "DENIED_MCP_NOT_PINNED",
            {"required_action": "run Y7 --action bakeoff first (or pass --candidate to force one)"},
            exit_code=2,
        )
    ab_pair_id = new_id("ab")
    api_args = copy.copy(args)
    api_args.route = "api"
    api_args.ab_pair_id = ab_pair_id
    api_args.seed = int(seed)
    mcp_args = copy.copy(args)
    mcp_args.route = "mcp"
    mcp_args.ab_pair_id = ab_pair_id
    mcp_args.seed = int(seed)
    api_result = comfy_generate(api_args)
    mcp_result = comfy_generate(mcp_args)
    parity = _parity(api_result, mcp_result)
    report_path = _write_parity_report(args, ab_pair_id, api_result, mcp_result, parity)
    return {
        "status": "OK",
        "ab_pair_id": ab_pair_id,
        "api_run": api_result["id"],
        "mcp_run": mcp_result["id"],
        "parity": parity,
        "report": repo_relative(report_path),
    }
