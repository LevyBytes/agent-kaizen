"""The Kaizen Improvement Lab (Layer E).

A kaizen-native, dependency-free recontextualization of DSPy's ideas (signatures,
metrics, optimizers, demonstrations, compile). It closes the SAVMI Improve loop
with evidence: assemble a case set + exemplars for a task contract, record
candidate variants as improvement proposals scored against that case set, then
rank them. The agent supplies creative variant generation; this tool supplies the
deterministic case-set assembly, record-keeping, and ranking. Proposals are never
auto-applied -- a human promotes the winner through GOTCHA -> LEARNING -> LEARNED.
"""

from __future__ import annotations

import json
from typing import Any

from .db import fetch_all, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import file_sha256, utc_text_hash
from .paths import EXPORT_ROOT, read_text_file, repo_relative
from .schemas import validate_record
from .task_records import _text_arg


def _slug(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip().lower()) or "contract"


def _require_contract(args: Any) -> str:
    contract = getattr(args, "contract", None)
    if not contract:
        raise KaizenDenied(
            "DENIED_CONTRACT_REQUIRED",
            {"required_action": "pass --contract (the task contract / signature name)"},
            exit_code=2,
        )
    return contract


def assemble_case_set(args: Any) -> dict[str, Any]:
    """O1: gather eval cases + LEARNED exemplars + active GOTCHAs for a contract."""
    contract = _require_contract(args)
    limit = int(getattr(args, "limit", None) or 50)
    category = getattr(args, "category", None)
    if category:
        cases = fetch_all(
            "SELECT id, category, title, summary FROM eval_cases WHERE category = ? ORDER BY created_at DESC LIMIT ?",
            (category, limit),
        )
    else:
        cases = fetch_all(
            "SELECT id, category, title, summary FROM eval_cases ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    exemplars = fetch_all("SELECT id, title, summary FROM learned ORDER BY created_at DESC LIMIT ?", (limit,))
    gotchas = fetch_all(
        "SELECT id, title, summary FROM gotcha WHERE status = 'active' ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )

    out_dir = EXPORT_ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"lab-caseset-{_slug(contract)}.md"
    lines = [
        f"# Lab case set: {contract}",
        "",
        f"Eval cases: {len(cases)}",
        f"LEARNED exemplars: {len(exemplars)}",
        f"Active GOTCHAs: {len(gotchas)}",
        "",
        "## Eval cases (the trainset)",
    ]
    lines.extend(f"- `{c[0]}` [{c[1]}] {c[2]} - {c[3]}" for c in cases)
    lines.append("")
    lines.append("## LEARNED exemplars (demonstrations)")
    lines.extend(f"- `{e[0]}` {e[1]} - {e[2]}" for e in exemplars)
    lines.append("")
    lines.append("## Active GOTCHAs (failure signals)")
    lines.extend(f"- `{g[0]}` {g[1]} - {g[2]}" for g in gotchas)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "status": "OK",
        "contract": contract,
        "eval_cases": len(cases),
        "exemplars": len(exemplars),
        "active_gotchas": len(gotchas),
        "path": repo_relative(path),
        "sha256": file_sha256(path),
    }


def add_proposal(args: Any) -> dict[str, Any]:
    """O2: record a candidate variant as an improvement proposal (never auto-applied).

    ``status`` is a free-text label for human triage; nothing in the lab acts on it.
    """
    contract = _require_contract(args)
    title = _text_arg(args, "title", "")
    if not title:
        raise KaizenDenied("DENIED_TITLE_REQUIRED", {"required_action": "resubmit with --title"}, exit_code=2)
    summary = _text_arg(args, "summary", "")
    body = _text_arg(args, "body", "")
    payload_json = getattr(args, "payload_json", None) or "{}"
    if getattr(args, "payload_json_file", None):
        payload_json = read_text_file(args.payload_json_file)
    if not isinstance(json.loads(payload_json), dict):
        raise KaizenDenied(
            "DENIED_PAYLOAD_TYPE",
            {"required_action": "--payload-json must be a JSON object"},
            exit_code=2,
        )

    metric = getattr(args, "metric", None)
    baseline = getattr(args, "baseline_score", None)
    candidate = getattr(args, "candidate_score", None)
    proposal = {
        "contract": contract,
        "title": title,
        "summary": summary,
        "body": body,
        "metric": metric,
        "baseline_score": baseline,
        "candidate_score": candidate,
    }
    # The registry schema is authoritative: it already enforces a 1-2 sentence summary and the
    # body word limit, so no separate validate_text_fields pass is needed.
    validate_record("improvement_proposal", {k: v for k, v in proposal.items() if v is not None})

    record_id = new_id("ip")
    created = now()
    content_hash = utc_text_hash({"id": record_id, **proposal})

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO improvement_proposals "
            "(id, created_at, contract, status, title, summary, body, baseline_score, candidate_score, "
            "metric, payload_json, content_hash, is_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                created,
                contract,
                getattr(args, "status", None) or "proposed",
                title,
                summary,
                body,
                baseline,
                candidate,
                metric,
                payload_json,
                content_hash,
                1 if getattr(args, "test", False) else 0,
            ),
        )

    write_tx(op)
    return {"status": "OK", "id": record_id, "content_hash": content_hash}


def proposal_report(args: Any) -> dict[str, Any]:
    """O3: rank a contract's proposals by candidate-minus-baseline delta and report."""
    contract = _require_contract(args)
    rows = fetch_all(
        "SELECT id, status, title, baseline_score, candidate_score, metric, summary FROM improvement_proposals "
        "WHERE contract = ? ORDER BY (candidate_score IS NOT NULL AND baseline_score IS NOT NULL) DESC, "
        "(COALESCE(candidate_score, 0) - COALESCE(baseline_score, 0)) DESC, created_at DESC",
        (contract,),
    )
    out_dir = EXPORT_ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"lab-report-{_slug(contract)}.md"
    lines = [f"# Lab report: {contract}", "", f"Proposals: {len(rows)}", "", "## Leaderboard (by candidate - baseline)"]
    for r in rows:
        base, cand = r[3], r[4]
        delta = f"{cand - base:+}" if (base is not None and cand is not None) else "n/a"
        cand_str = f"{cand}" if cand is not None else "n/a"
        lines.append(f"- `{r[0]}` [{r[1]}] delta={delta} ({r[5] or 'metric?'}={cand_str}) {r[2]} - {r[6]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    top = None
    if rows:
        r = rows[0]
        top = {"id": r[0], "title": r[2], "candidate_score": r[4], "baseline_score": r[3], "metric": r[5]}
    return {
        "status": "OK",
        "contract": contract,
        "proposals": len(rows),
        "top_proposal": top,
        "promotion_path": "human review -> promote underlying learning via L2/L3 (GOTCHA -> LEARNING -> LEARNED)",
        "path": repo_relative(path),
        "sha256": file_sha256(path),
    }


def lab_evaluate(args: Any) -> dict[str, Any]:
    """O4: advisory LLM-judge evaluation of a contract's proposals against its eval cases.

    Writes a model ``candidate_score`` + one ``source='model'`` eval_score per proposal (each linked
    to a ``kind='judge'`` trace). ADVISORY ONLY: it never changes ``status`` and never promotes --
    ``O3`` then reorders and a human promotes the winner via ``L2``/``L3``.
    """
    from .backends import get_text_backend
    from .model_ops import _judge_candidate, _record_judge_trace
    from .trace_records import write_eval_score

    backend = get_text_backend()
    if backend is None:
        raise KaizenDenied(
            "DENIED_BACKEND_UNCONFIGURED",
            {"required_action": "set KAIZEN_LLM_MODEL (or KAIZEN_TEXT_BACKEND=transformers) -- see setup/OLLAMA.md"},
            exit_code=2,
        )
    contract = _require_contract(args)
    limit = int(getattr(args, "limit", None) or 50)
    category = getattr(args, "category", None)
    if category:
        cases = fetch_all(
            "SELECT id, summary, expected_json, body FROM eval_cases WHERE category = ? ORDER BY created_at DESC LIMIT ?",
            (category, limit),
        )
    else:
        cases = fetch_all(
            "SELECT id, summary, expected_json, body FROM eval_cases ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    if not cases:
        raise KaizenDenied(
            "DENIED_NO_EVAL_CASES",
            {"required_action": "add eval cases with Q3 (eval-case-add) before running O4"},
            exit_code=2,
        )
    proposals = fetch_all(
        "SELECT id, title, body FROM improvement_proposals WHERE contract = ? ORDER BY created_at DESC",
        (contract,),
    )
    if not proposals:
        return {"status": "OK", "contract": contract, "evaluated": 0, "note": "no proposals for this contract (record them with O2)."}

    metric = getattr(args, "metric", None) or "model_judge"
    evaluated: list[dict[str, Any]] = []
    for pid, ptitle, pbody in proposals:
        candidate = pbody or ptitle or ""
        scores: list[float] = []
        agg = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "latency_ms": 0, "raw": []}
        for _cid, csummary, cexpected, cbody in cases:
            rubric = cexpected or cbody or csummary
            judged = _judge_candidate(backend, rubric, candidate)
            if judged["verdict"]["score"] is not None:
                scores.append(judged["verdict"]["score"])
            usage = judged["usage"]
            agg["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            agg["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            agg["total_tokens"] += int(usage.get("total_tokens") or 0)
            agg["latency_ms"] += judged["latency_ms"]
            agg["raw"].append(judged["raw_text"])
        mean = round(sum(scores) / len(scores), 4) if scores else None
        summary = f"O4 advisory judge of a proposal over {len(cases)} eval case(s); mean={mean}."
        judged_agg = {
            "usage": {k: agg[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
            "latency_ms": agg["latency_ms"],
            "raw_text": "\n---\n".join(agg["raw"]),
            "model": backend.model,
        }
        is_test = 1 if getattr(args, "test", False) else 0
        trace_event_id = _record_judge_trace(
            backend.name, backend.model, judged_agg, getattr(args, "task_id", None), getattr(args, "trace_id", None),
            summary, is_test
        )
        if mean is not None:
            def _update(conn: Any, _attempt: int, _pid: str = pid, _mean: float = mean) -> None:
                conn.execute("UPDATE improvement_proposals SET candidate_score = ? WHERE id = ?", (_mean, _pid))

            write_tx(_update)
        value, data_type = (mean, "numeric") if mean is not None else ("no-numeric-verdict", "categorical")
        score = write_eval_score(
            {
                "trace_event_id": trace_event_id,
                "name": metric,
                "value": value,
                "data_type": data_type,
                "source": "model",
                "comment": f"contract={contract}"[:200],
            },
            is_test=is_test,
        )
        evaluated.append({"proposal_id": pid, "mean_score": mean, "cases": len(cases), "trace_event_id": trace_event_id, "score_id": score["id"]})

    return {
        "status": "OK",
        "advisory": True,
        "contract": contract,
        "evaluated": len(evaluated),
        "cases": len(cases),
        "proposals": evaluated,
        "note": "advisory only; a human promotes the winner via L2/L3.",
    }
