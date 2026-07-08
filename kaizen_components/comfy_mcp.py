"""Local ComfyUI MCP lane (Y7): candidate bakeoff + pinned-winner generation.

Local-only, stdio transport, explicit loopback ``COMFYUI_URL`` always (candidate auto-detect can
probe C-drive paths). The MCP client SDK (``mcp>=1.0,<2``) is an OPTIONAL dependency, imported at
call time; when absent every live action denies with a structured install hint. The bakeoff scores
candidates against deterministic hard gates, pins the winner in ``db_settings.active_comfy_mcp``, and
records one ``source_locks`` row per candidate plus a markdown report artifact.

Only the winner gets a full generation adapter (Y8 ``--route mcp``): it submits the workflow through
the MCP submit tool, then reuses the proven direct-HTTP ``wait``/``fetch_outputs`` against the same
local runtime, so api-vs-mcp parity (Y9) compares like for like.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from typing import Any

from . import comfyui
from .comfyui import _endpoint, _insert_artifact, probe
from .db import get_setting, new_id, now, set_setting, write_tx
from .denials import KaizenDenied
from .hashing import utc_text_hash
from .paths import GENERATED_ROOT, repo_relative
from .schemas import validate_record


def _import_mcp():
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as error:
        raise KaizenDenied(
            "DENIED_BACKEND_UNAVAILABLE",
            {"backend": "mcp-client", "reason": str(error),
             "required_action": 'pip install "mcp>=1.0,<2" into the kaizen venv (optional dependency; v2 is beta)'},
            exit_code=2,
        ) from error
    return ClientSession, StdioServerParameters, stdio_client


# Candidate table. Only artokun has a reproducible local STDIO launch (npm package, Windows-supported).
# joenorton serves HTTP (streamable-http at :9000), not stdio; shawnrushefsky is not on npm (Docker /
# build-from-source). Both keep command=None, which auto-fails the stdio + reproducible-install gates
# with the reason recorded in ``install``. Verified 2026-07-08.
CANDIDATES: dict[str, dict[str, Any]] = {
    "artokun": {
        "repo": "artokun/comfyui-mcp", "license": "MIT", "runtime": "node>=22", "transport": "stdio",
        "command": "npx.cmd" if os.name == "nt" else "npx",
        "args": ["-y", "comfyui-mcp@latest"],
        "env": {"COMFYUI_URL": "<endpoint>"},  # explicit URL: its auto-detect probes C-drive paths
        "install": "npx -y comfyui-mcp@latest",
    },
    "joenorton": {
        "repo": "joenorton/comfyui-mcp-server", "license": "Apache-2.0", "runtime": "python", "transport": "http",
        "command": None, "args": [], "env": {"COMFYUI_URL": "<endpoint>"},
        "install": "python server.py (HTTP/streamable-http at 127.0.0.1:9000; not a stdio server)",
    },
    "shawnrushefsky": {
        "repo": "shawnrushefsky/comfyui-mcp", "license": "MIT", "runtime": "node>=18", "transport": "stdio",
        "command": None, "args": [], "env": {"COMFYUI_URL": "<endpoint>"},
        "install": "node dist/index.js after git clone + npm build (not published to npm); or Docker",
    },
}

# Canonical tool-name map per pinned winner. Confirmed against artokun's live list_tools (Stage 9):
# submit=enqueue_workflow (arg `workflow`), status=get_job_status, outputs=list_output_images. artokun
# randomizes seeds on submit by default -- submit_args disable that so the workflow's seed is honored,
# which is required for Y9 api-vs-mcp parity to compare the same generation.
_ADAPTERS: dict[str, dict[str, Any]] = {
    "artokun": {
        "submit": "enqueue_workflow", "status": "get_job_status", "outputs": "list_output_images",
        "submit_args": {"disable_random_seed": True},
    },
}

_CAPABILITY_KEYWORDS = {
    "submit": ("enqueue", "submit", "run_workflow", "execute", "generate"),
    "status": ("status", "job", "history", "progress", "queue"),
    "fetch_outputs": ("output", "image", "result", "download", "view"),
    "validate": ("validate", "lint", "check"),
    "list_models": ("model", "checkpoint", "lora"),
    "lifecycle": ("start", "stop", "restart", "launch"),
}


def classify_tools(tools: list[dict[str, str]]) -> dict[str, list[str]]:
    """Map ``[{name, description}]`` to ``capability -> matching tool names`` by keyword."""
    caps: dict[str, list[str]] = {}
    for cap, keywords in _CAPABILITY_KEYWORDS.items():
        hits = []
        for tool in tools:
            haystack = f"{tool.get('name', '')} {tool.get('description', '')}".lower()
            if any(kw in haystack for kw in keywords):
                hits.append(tool.get("name", ""))
        if hits:
            caps[cap] = hits
    return caps


GATES = (
    "prereq_present", "spawns_stdio", "initializes", "local_endpoint_configurable",
    "no_cloud_required", "no_c_drive_writes", "submit", "status", "fetch_outputs",
    "structured_errors", "license_known", "install_reproducible",
)
_SOFT_GATES = ("no_c_drive_writes", "structured_errors", "license_known")
HARD_GATES = tuple(g for g in GATES if g not in _SOFT_GATES)


def score_candidate(doctor: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Deterministic gate scoring (no network). Winner = all HARD_GATES pass; ties break on score."""
    caps = doctor.get("capabilities", {}) or {}
    command_ok = spec.get("command") is not None
    stdio = spec.get("transport", "stdio") == "stdio"
    env = spec.get("env") or {}
    results = {
        "prereq_present": "pass" if doctor.get("prereq_ok") else "fail",
        "spawns_stdio": "pass" if (command_ok and stdio) else "fail",
        "initializes": "pass" if doctor.get("probe_ok") else "fail",
        "local_endpoint_configurable": "pass" if "COMFYUI_URL" in env else "fail",
        "no_cloud_required": "fail" if spec.get("cloud_required") else "pass",
        "no_c_drive_writes": "unverified",
        "submit": "pass" if caps.get("submit") else "fail",
        "status": "pass" if caps.get("status") else "fail",
        "fetch_outputs": "pass" if caps.get("fetch_outputs") else "fail",
        "structured_errors": "unverified",
        "license_known": "pass" if spec.get("license") else "fail",
        "install_reproducible": "pass" if command_ok else "fail",
    }
    hard_pass = all(results[g] == "pass" for g in HARD_GATES)
    score = sum(1 for value in results.values() if value == "pass")
    return {"gates": results, "hard_pass": hard_pass, "score": score}


async def _probe_candidate(spec: dict[str, Any], endpoint: str, timeout: float = 60.0) -> dict[str, Any]:
    ClientSession, StdioServerParameters, stdio_client = _import_mcp()
    env = {**os.environ, **{k: v.replace("<endpoint>", endpoint) for k, v in (spec.get("env") or {}).items()}}
    params = StdioServerParameters(command=spec["command"], args=spec.get("args", []), env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await asyncio.wait_for(session.initialize(), timeout)
            listed = await asyncio.wait_for(session.list_tools(), timeout)
    server = getattr(init, "serverInfo", None)
    return {
        "server_version": getattr(server, "version", None),
        "tools": [{"name": t.name, "description": t.description or ""} for t in listed.tools],
    }


def _doctor_candidate(slug: str, spec: dict[str, Any], endpoint: str, timeout: float = 60.0) -> dict[str, Any]:
    """Probe one candidate; never raises for a candidate-level failure (captured in the result)."""
    result: dict[str, Any] = {
        "candidate": slug, "repo": spec["repo"], "license": spec["license"], "transport": spec.get("transport"),
        "prereq_ok": False, "prereq_reason": "", "probe_ok": False, "error": None,
        "server_version": None, "tools": [], "capabilities": {},
    }
    runtime = spec.get("runtime", "")
    if runtime.startswith("node"):
        node = shutil.which("node")
        if not node:
            result["prereq_reason"] = "node not found on PATH"
        else:
            try:
                ver = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
                major = int(ver.lstrip("v").split(".")[0]) if ver else 0
                result["node_version"] = ver
                minimum = int("".join(ch for ch in runtime if ch.isdigit()) or "0")
                if major >= minimum:
                    result["prereq_ok"] = True
                else:
                    result["prereq_reason"] = f"node {ver} < {minimum}"
            except Exception as error:  # noqa: BLE001
                result["prereq_reason"] = f"node --version failed: {error}"
    else:
        result["prereq_ok"] = True  # python runtime is already present
    if spec.get("command") is None:
        result["error"] = spec.get("install", "no reproducible local stdio launch")
        return result
    if not result["prereq_ok"]:
        return result
    try:
        probed = asyncio.run(_probe_candidate(spec, endpoint, timeout=timeout))
        result["probe_ok"] = True
        result["server_version"] = probed.get("server_version")
        result["tools"] = probed.get("tools", [])
        result["capabilities"] = classify_tools(result["tools"])
    except Exception as error:  # noqa: BLE001 -- a candidate crash is captured, not fatal to the bakeoff
        result["error"] = str(error)
    return result


def mcp_doctor(args: Any) -> dict[str, Any]:
    slug = getattr(args, "candidate", None)
    if not slug:
        raise KaizenDenied("DENIED_CANDIDATE_REQUIRED", {"candidates": sorted(CANDIDATES), "required_action": "resubmit with --candidate"}, exit_code=2)
    if slug not in CANDIDATES:
        raise KaizenDenied("DENIED_CANDIDATE_UNKNOWN", {"candidate": slug, "candidates": sorted(CANDIDATES), "required_action": "use a known --candidate"}, exit_code=2)
    _import_mcp()  # fail fast + clearly when the optional client is absent
    endpoint = _endpoint(args)
    doctor = _doctor_candidate(slug, CANDIDATES[slug], endpoint, timeout=float(getattr(args, "timeout", None) or 60))
    scored = score_candidate(doctor, CANDIDATES[slug])
    return {"status": "OK", "action": "doctor", "endpoint": endpoint, "report": doctor, "score": scored}


def _bakeoff_report_md(endpoint: str, rows: list[dict[str, Any]], winner: str | None) -> str:
    lines = [f"# ComfyUI MCP Bakeoff", "", f"Endpoint: {endpoint}", f"Winner: {winner or 'none'}", ""]
    for row in rows:
        d, s = row["doctor"], row["score"]
        lines.append(f"## {row['slug']} ({d['repo']}, {d['license']}) -- score {s['score']}, hard_pass {s['hard_pass']}")
        if d.get("error"):
            lines.append(f"- error: {d['error']}")
        lines.append("- gates: " + ", ".join(f"{g}={v}" for g, v in s["gates"].items()))
        lines.append("")
    return "\n".join(lines) + "\n"


def mcp_bakeoff(args: Any) -> dict[str, Any]:
    _import_mcp()  # fail fast when the optional client is absent
    endpoint = _endpoint(args)
    is_test = 1 if getattr(args, "test", False) else 0
    rows: list[dict[str, Any]] = []
    for slug, spec in CANDIDATES.items():
        doctor = _doctor_candidate(slug, spec, endpoint, timeout=float(getattr(args, "timeout", None) or 60))
        rows.append({"slug": slug, "doctor": doctor, "score": score_candidate(doctor, spec)})
    passing = [r for r in rows if r["score"]["hard_pass"]]
    winner = max(passing, key=lambda r: r["score"]["score"])["slug"] if passing else None
    winner_version = next((r["doctor"].get("server_version") for r in rows if r["slug"] == winner), None) if winner else None

    report_dir = GENERATED_ROOT / "comfy-mcp"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = "".join(ch for ch in now() if ch.isdigit())
    report_path = report_dir / f"bakeoff-{stamp}.md"
    report_path.write_text(_bakeoff_report_md(endpoint, rows, winner), encoding="utf-8")

    report_artifact_id = new_id("a")
    created = now()
    source_lock_rows = []
    for row in rows:
        slug, doctor = row["slug"], row["doctor"]
        spec = CANDIDATES[slug]
        source_id = f"comfy-mcp-{slug}"
        payload = {
            "source_id": source_id,
            "authority_tier": "implementation",
            "url_or_repository": f"https://github.com/{spec['repo']}",
            "version_or_commit": doctor.get("server_version") or "unprobed",
            "license": spec["license"],
            "summary": f"ComfyUI MCP bakeoff candidate {slug}; hard_pass={row['score']['hard_pass']}.",
        }
        validate_record("source_lock", payload)
        source_lock_rows.append((new_id("s"), payload, utc_text_hash({"id": source_id, **payload})))

    def op(conn: Any, _attempt: int) -> None:
        _insert_artifact(conn, artifact_id=report_artifact_id, created=created, task_id=getattr(args, "task_id", None),
                         kind="comfy_bakeoff_report", path=report_path, summary="ComfyUI MCP bakeoff report.", is_test=is_test)
        for rec_id, payload, content_hash in source_lock_rows:
            conn.execute(
                "INSERT INTO source_locks "
                "(id, created_at, source_id, authority_tier, url_or_repository, version_or_commit, retrieved_at, "
                "content_hash, license, supersedes, summary, body, is_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rec_id, created, payload["source_id"], payload["authority_tier"], payload["url_or_repository"],
                 payload["version_or_commit"], created, content_hash, payload["license"], None, payload["summary"], "", is_test),
            )

    write_tx(op)
    if winner:
        set_setting("active_comfy_mcp", winner)
        if winner_version:
            set_setting("active_comfy_mcp_version", winner_version)
    return {
        "status": "OK",
        "action": "bakeoff",
        "endpoint": endpoint,
        "winner": winner,
        "winner_version": winner_version,
        "candidates": {r["slug"]: {"hard_pass": r["score"]["hard_pass"], "score": r["score"]["score"]} for r in rows},
        "report": repo_relative(report_path),
        "report_artifact_id": report_artifact_id,
    }


def _mcp_submit(spec: dict[str, Any], endpoint: str, adapter: dict[str, str], workflow: dict[str, Any], timeout: float) -> str:
    """Submit the workflow through the winner's MCP submit tool; return the ComfyUI prompt_id."""
    ClientSession, StdioServerParameters, stdio_client = _import_mcp()
    env = {**os.environ, **{k: v.replace("<endpoint>", endpoint) for k, v in (spec.get("env") or {}).items()}}
    params = StdioServerParameters(command=spec["command"], args=spec.get("args", []), env=env)

    async def _run() -> str:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout)
                call_args = {"workflow": workflow, **adapter.get("submit_args", {})}
                result = await asyncio.wait_for(
                    session.call_tool(adapter["submit"], call_args), timeout
                )
        text = ""
        for block in getattr(result, "content", []) or []:
            text += getattr(block, "text", "") or ""
        if getattr(result, "isError", False):
            raise KaizenDenied("DENIED_WORKFLOW_REJECTED", {"reason": "MCP submit tool returned an error", "raw": text[:300]}, exit_code=2)
        try:
            parsed = json.loads(text) if text.strip().startswith("{") else {}
        except json.JSONDecodeError:
            parsed = {}
        prompt_id = parsed.get("prompt_id") or text.strip()
        if not prompt_id:
            raise KaizenDenied("DENIED_WORKFLOW_REJECTED", {"reason": "MCP submit returned no prompt_id", "raw": text[:300]}, exit_code=2)
        return str(prompt_id)

    return asyncio.run(_run())


def mcp_generate(args: Any, workflow: dict[str, Any]) -> dict[str, Any]:
    """Y8 --route mcp: submit via the pinned winner's MCP tool, then wait/fetch via direct HTTP
    against the SAME local runtime (so api-vs-mcp parity compares like for like)."""
    slug = getattr(args, "candidate", None) or get_setting("active_comfy_mcp")
    if not slug:
        raise KaizenDenied(
            "DENIED_MCP_NOT_PINNED",
            {"required_action": "run Y7 --action bakeoff first (or pass --candidate to force one)"},
            exit_code=2,
        )
    if slug not in CANDIDATES:
        raise KaizenDenied("DENIED_CANDIDATE_UNKNOWN", {"candidate": slug, "candidates": sorted(CANDIDATES), "required_action": "use a known --candidate"}, exit_code=2)
    adapter = _ADAPTERS.get(slug)
    if adapter is None:
        raise KaizenDenied(
            "DENIED_MCP_ADAPTER_UNCONFIRMED",
            {"candidate": slug, "required_action": "confirm the winner's list_tools and pin its tool-map in comfy_mcp._ADAPTERS (Stage 9 runbook)"},
            exit_code=2,
        )
    url = _endpoint(args)
    stats = probe(url, timeout=float(getattr(args, "timeout", None) or 5))  # capability gate
    system = stats.get("system", {}) if isinstance(stats, dict) else {}
    runtime_profile = f"comfyui={system.get('comfyui_version')};python={system.get('python_version')}"

    template = comfyui._slug(getattr(args, "template", None) or "default")
    workflow_hash = utc_text_hash(workflow)
    seed, models = comfyui._extract_run_meta(workflow)
    summary = comfyui._text_arg(args, "summary", f"ComfyUI run ({template}).")
    run_id = new_id("gr")
    created = now()
    dest_dir = GENERATED_ROOT / template
    dest_dir.mkdir(parents=True, exist_ok=True)
    workflow_artifact_id = new_id("a")
    workflow_path = dest_dir / f"{run_id}-workflow.json"
    workflow_path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    started = time.monotonic()
    prompt_id = _mcp_submit(CANDIDATES[slug], url, adapter, workflow, timeout=float(getattr(args, "timeout", None) or 60))
    entry = comfyui.wait(url, prompt_id, timeout=float(getattr(args, "timeout", None) or 300))
    output_paths = comfyui.fetch_outputs(url, entry, dest_dir, timeout=120)
    latency_ms = int((time.monotonic() - started) * 1000)
    run_status = (entry.get("status") or {}).get("status_str") if isinstance(entry.get("status"), dict) else None
    status = "failed" if run_status == "error" else "completed"

    return comfyui._record_run(
        args=args, url=url, workflow_hash=workflow_hash, template=template, status=status,
        prompt_id=prompt_id, output_paths=output_paths, latency_ms=latency_ms, seed=seed, models=models,
        dest_dir=dest_dir, workflow_path=workflow_path, workflow_artifact_id=workflow_artifact_id,
        run_id=run_id, created=created, summary=summary, route="mcp", runtime_profile=runtime_profile,
        mcp_candidate=slug, mcp_version=get_setting("active_comfy_mcp_version"),
    )


def comfy_mcp(args: Any) -> dict[str, Any]:
    """Y7: probe or bake off local MCP servers, or run the pinned winner."""
    action = getattr(args, "action", None)
    if not action:
        raise KaizenDenied(
            "DENIED_ACTION_REQUIRED",
            {"allowed": ["doctor", "bakeoff", "run"], "required_action": "resubmit with --action"},
            exit_code=2,
        )
    action = action.lower()
    if action == "doctor":
        return mcp_doctor(args)
    if action == "bakeoff":
        return mcp_bakeoff(args)
    if action == "run":
        workflow = comfyui._load_workflow(args)
        return mcp_generate(args, workflow)
    raise KaizenDenied(
        "DENIED_ACTION_UNKNOWN",
        {"action": action, "allowed": ["doctor", "bakeoff", "run"], "required_action": "resubmit with a valid --action"},
        exit_code=2,
    )
