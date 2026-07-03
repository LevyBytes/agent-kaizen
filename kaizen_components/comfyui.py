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
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .backends.http_retry import http_request
from .db import fetch_all, fetch_one, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import file_sha256, utc_text_hash, validate_text_fields
from .paths import GENERATED_ROOT, REPO_ROOT, repo_relative
from .redaction import assert_redacted
from .schemas import validate_record
from .task_records import _text_arg


_DEFAULT_URL = "http://127.0.0.1:8188"


def _slug(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip().lower()) or "default"


def _endpoint(args: Any) -> str:
    raw = getattr(args, "endpoint", None) or os.environ.get("KAIZEN_COMFYUI_URL") or _DEFAULT_URL
    return raw.rstrip("/")


def _is_loopback(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


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
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers)
    raw = http_request(req, timeout=timeout)
    return json.loads(raw.decode("utf-8")) if raw else {}


def _http_bytes(url: str, *, timeout: float = 120.0) -> bytes:
    return http_request(urllib.request.Request(url), timeout=timeout)


def probe(url: str, *, timeout: float = 5.0) -> dict[str, Any]:
    try:
        stats = _http_json(f"{url}/system_stats", timeout=timeout)
    except Exception as error:  # noqa: BLE001 -- any failure means "unreachable"
        raise _backend_unreachable(url, error) from error
    return stats if isinstance(stats, dict) else {}


def comfy_doctor(args: Any) -> dict[str, Any]:
    """Y5: probe the configured ComfyUI endpoint; the capability gate."""
    url = _endpoint(args)
    stats = probe(url, timeout=float(getattr(args, "timeout", None) or 5))
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
    path = Path(src)
    if not path.is_file():
        raise KaizenDenied("DENIED_FILE_NOT_FOUND", {"path": str(path)}, exit_code=1)
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
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                seeds.append(str(int(value)))
        for model_key in ("ckpt_name", "model_name", "unet_name", "vae_name", "lora_name"):
            value = inputs.get(model_key)
            if isinstance(value, str) and value:
                models.append(value)
    return (seeds[0] if seeds else None), sorted(set(models))


def submit(url: str, workflow: dict[str, Any], client_id: str, *, timeout: float = 30.0) -> str:
    body = json.dumps({"prompt": workflow, "client_id": client_id}).encode("utf-8")
    try:
        resp = _http_json(f"{url}/prompt", data=body, timeout=timeout)
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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            history = _http_json(f"{url}/history/{prompt_id}", timeout=15)
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
    saved: list[Path] = []
    outputs = entry.get("outputs", {}) if isinstance(entry, dict) else {}
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        for image in node_output.get("images", []) or []:
            filename = image.get("filename")
            if not filename:
                continue
            query = urllib.parse.urlencode(
                {"filename": filename, "subfolder": image.get("subfolder", ""), "type": image.get("type", "output")}
            )
            data = _http_bytes(f"{url}/view?{query}", timeout=timeout)
            target = dest_dir / Path(filename).name
            target.write_bytes(data)
            saved.append(target)
    return saved


def _insert_artifact(conn: Any, *, artifact_id: str, created: str, task_id: str | None, kind: str, path: Path, summary: str) -> None:
    sha = file_sha256(path) if path.is_file() else None
    size = path.stat().st_size if path.is_file() else None
    rel = repo_relative(path)
    content_hash = utc_text_hash({"id": artifact_id, "path": rel, "sha256": sha})
    conn.execute(
        "INSERT INTO artifacts (id, created_at, task_id, kind, path, sha256, bytes, summary, body, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (artifact_id, created, task_id, kind, rel, sha, size, summary, "", content_hash),
    )


def run_workflow(args: Any) -> dict[str, Any]:
    """Y1: submit + record a reproducible ComfyUI run (or `--dry-run` to record without a server)."""
    url = _endpoint(args)
    dry_run = bool(getattr(args, "dry_run", False))
    workflow = _load_workflow(args)
    workflow_hash = utc_text_hash(workflow)
    template = _slug(getattr(args, "template", None) or "default")
    seed, models = _extract_run_meta(workflow)

    summary = _text_arg(args, "summary", f"ComfyUI run ({template}).")
    assert_redacted({"summary": summary, "template": template})
    validate_text_fields({"summary": summary, "body": ""})

    task_id = getattr(args, "task_id", None)
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

    if not dry_run:
        probe(url, timeout=float(getattr(args, "timeout", None) or 5))  # capability gate
        started = time.monotonic()
        prompt_id = submit(url, workflow, uuid.uuid4().hex, timeout=30)
        entry = wait(url, prompt_id, timeout=float(getattr(args, "timeout", None) or 300))
        output_paths = fetch_outputs(url, entry, dest_dir, timeout=120)
        latency_ms = int((time.monotonic() - started) * 1000)
        run_status = (entry.get("status") or {}).get("status_str") if isinstance(entry.get("status"), dict) else None
        status = "failed" if run_status == "error" else "completed"

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
        _insert_artifact(conn, artifact_id=workflow_artifact_id, created=created, task_id=task_id, kind="comfyui_workflow", path=workflow_path, summary=f"ComfyUI workflow for run {run_id}.")
        for aid, path in output_artifacts:
            _insert_artifact(conn, artifact_id=aid, created=created, task_id=task_id, kind="comfyui_output", path=path, summary=f"ComfyUI output {path.name} ({template}).")
        conn.execute(
            "INSERT INTO generative_runs "
            "(id, created_at, task_id, backend, template, endpoint, workflow_hash, workflow_artifact_id, seed, "
            "models_json, status, prompt_id, output_artifact_ids_json, output_dir, latency_ms, summary, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, created, task_id, "comfyui", template, url, workflow_hash, workflow_artifact_id, seed,
             models_json, status, prompt_id, output_ids_json, rel_dir, latency_ms, summary, content_hash),
        )
        conn.execute(
            "INSERT INTO agentgateway_events (id, created_at, event_type, status, summary, body, payload_json, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (gateway_id, created, "comfyui_run", status, summary, "", json.dumps(gateway_payload["payload"]), gateway_hash),
        )
        conn.execute(
            "INSERT INTO trace_events "
            "(id, created_at, task_id, trace_id, kind, level, status, redaction_status, summary, body, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trace_id, created, task_id, run_id, "generative_run", "default", status, "scanned_clean", summary, "", trace_hash),
        )

    write_tx(op)
    return {
        "status": "OK",
        "id": run_id,
        "run_status": status,
        "dry_run": dry_run,
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
    return {"status": "OK", "record": dict(zip(columns, row))}


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
    if not getattr(args, "template", None):
        args.template = row[1]
    result = run_workflow(args)
    result["replay_of"] = run_id
    return result
