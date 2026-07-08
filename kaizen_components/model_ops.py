"""Model-backend ops (B*): doctor, advisory text run, and re-embed.

- ``B1 model-doctor`` probes the configured embedding + text backends (reachable? model? dim?).
- ``B2 model-run`` sends an advisory prompt to the text backend and records a ``model_call``
  trace (redaction-by-default). It is ADVISORY ONLY — never the final acceptance authority unless
  a deterministic verifier confirms; the caller, not the model, enforces that.
- ``B3 reembed`` backfills the per-model ``chunk_embeddings`` index (Turso ``vector32``) for the
  target model, so E4 ``--semantic`` can rank by cosine distance.

All three degrade gracefully when no backend is configured/reachable.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .backends import embed_batched, get_embedding_backend, get_embedding_backend_for, get_text_backend
from .db import fetch_all, get_active_embedding_model, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import utc_text_hash, validate_text_fields
from .pii_scan import advisory_pii_scan
from .redaction import assert_redacted, scan_for_secrets
from .schemas import validate_record
from .task_records import _text_arg


def _pii_tags_json(hits: list[dict[str, Any]]) -> str | None:
    """Serialize advisory PII hits (label + span_hash, never raw) for the trace_events.tags_json
    column, or None when there are none (default behavior stays byte-identical)."""
    return json.dumps({"pii_advisory": hits}) if hits else None


def model_doctor(args: Any) -> dict[str, Any]:
    """B1: report configured embedding + text backends and whether they are reachable."""
    embed = get_embedding_backend()
    text = get_text_backend()
    report: dict[str, Any] = {
        "status": "OK",
        "embedding": {"configured": embed is not None},
        "text": {"configured": text is not None},
    }
    if embed is not None:
        try:
            report["embedding"].update(embed.probe())
            report["embedding"]["reachable"] = True
        except KaizenDenied as denied:
            report["embedding"]["reachable"] = False
            report["embedding"]["reason"] = denied.payload().get("reason")
    if text is not None:
        try:
            report["text"].update(text.probe())
            report["text"]["reachable"] = True
        except KaizenDenied as denied:
            report["text"]["reachable"] = False
            report["text"]["reason"] = denied.payload().get("reason")
    return report


def model_run(args: Any) -> dict[str, Any]:
    """B2: advisory text generation, recorded as a model_call trace (never final authority)."""
    backend = get_text_backend()
    if backend is None:
        raise KaizenDenied(
            "DENIED_BACKEND_UNCONFIGURED",
            {"required_action": "set KAIZEN_LLM_MODEL (and optionally KAIZEN_LLM_BASE_URL) -- see setup/OLLAMA.md"},
            exit_code=2,
        )
    prompt = getattr(args, "prompt", None)
    if not prompt:
        raise KaizenDenied("DENIED_PROMPT_REQUIRED", {"required_action": "pass --prompt"}, exit_code=2)
    assert_redacted({"prompt": prompt})
    # Advisory PII NER runs AFTER the enforced regex gate; [] unless a PII backend is configured, so
    # this is a no-op by default. Hits carry label + span_hash only (never raw) -- augment, never gate.
    pii_advisory = advisory_pii_scan(
        prompt,
        record_trace=True,
        task_id=getattr(args, "task_id", None),
        is_test=1 if getattr(args, "test", False) else 0,
    )

    started = time.monotonic()
    result = backend.chat(prompt)
    latency_ms = int((time.monotonic() - started) * 1000)
    usage = result.get("usage", {}) if isinstance(result.get("usage"), dict) else {}

    # The raw model output is stored only as a hash ref; a human-authored summary is the durable text.
    output_ref = utc_text_hash({"text": result.get("text", "")})
    summary = _text_arg(args, "summary", "Advisory model-run (kind=model_call); output stored as a hash ref.")
    validate_text_fields({"summary": summary, "body": ""})
    assert_redacted({"summary": summary})

    payload = {
        "kind": "model_call",
        "task_id": getattr(args, "task_id", None),
        "trace_id": getattr(args, "trace_id", None),
        "model": result.get("model", backend.model),
        "provider": backend.name,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "latency_ms": latency_ms,
        "output_ref": output_ref,
        "status": "recorded",
        "level": "default",
        "summary": summary,
    }
    validate_record("trace_event", {k: v for k, v in payload.items() if v is not None})

    record_id = new_id("te")
    created = now()
    content_hash = utc_text_hash({"id": record_id, **{k: v for k, v in payload.items() if v is not None}})

    is_test = 1 if getattr(args, "test", False) else 0
    tags_json = _pii_tags_json(pii_advisory)

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO trace_events "
            "(id, created_at, task_id, trace_id, kind, level, status, model, provider, prompt_tokens, "
            "completion_tokens, total_tokens, latency_ms, tags_json, output_ref, redaction_status, summary, body, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id, created, payload.get("task_id"), payload.get("trace_id"), "model_call", "default",
                "recorded", payload["model"], payload["provider"], payload.get("prompt_tokens"),
                payload.get("completion_tokens"), payload.get("total_tokens"), latency_ms, tags_json, output_ref,
                "scanned_clean", summary, "", content_hash, is_test,
            ),
        )

    write_tx(op)
    result = {
        "status": "OK",
        "id": record_id,
        "advisory": True,
        "is_test": bool(is_test),
        "model": payload["model"],
        "provider": payload["provider"],
        "output_ref": output_ref,
        "tokens": {"prompt": payload.get("prompt_tokens"), "completion": payload.get("completion_tokens"), "total": payload.get("total_tokens")},
        "latency_ms": latency_ms,
        "note": "advisory only; not an acceptance authority unless a deterministic verifier confirms.",
    }
    if pii_advisory:
        result["pii_advisory"] = pii_advisory
    return result


def reembed(args: Any) -> dict[str, Any]:
    """B3: build/refresh a model's chunk_embeddings index by backfilling the rows it is missing.

    ``--model <id>`` targets a specific model (default: the active model, else the configured
    backend's). Backfill-only + per-model: other models' rows are untouched, so E4 keeps serving the
    active index throughout a rolling upgrade. ``--activate`` then flips retrieval to this model
    (denies unless it fully indexes the corpus) and prunes per retention.
    """
    requested = getattr(args, "model", None)
    if requested:
        model = requested
    else:
        cfg = get_embedding_backend()
        model = get_active_embedding_model(default=(cfg.model if cfg else None))
        if not model:
            raise KaizenDenied(
                "DENIED_BACKEND_UNCONFIGURED",
                {"required_action": "set KAIZEN_EMBED_MODEL (and optionally KAIZEN_EMBED_BASE_URL), or pass --model <id> -- see setup/OLLAMA.md"},
                exit_code=2,
            )
    backend = get_embedding_backend_for(model)

    # Backfill only the chunks this model does not yet index; carry each chunk's parent-document
    # is_test so the K7 purge cascade stays exact.
    document_id = getattr(args, "id", None)
    missing = "ec.id NOT IN (SELECT chunk_id FROM chunk_embeddings WHERE embedding_model = ?)"
    base = (
        "SELECT ec.id, ec.text, COALESCE(ed.is_test, 0) FROM evidence_chunks ec "
        "LEFT JOIN evidence_documents ed ON ec.document_id = ed.id WHERE "
    )
    if document_id:
        rows = fetch_all(base + f"ec.document_id = ? AND {missing} ORDER BY ec.chunk_index", (document_id, model))
    else:
        rows = fetch_all(base + f"{missing} ORDER BY ec.created_at", (model,))

    built = 0
    dimension = 0
    if rows:
        # Batched network calls OUTSIDE the tx; raises DENIED if unreachable or on a per-batch count
        # mismatch (large corpora would blow request-size limits unbatched).
        vectors = embed_batched(
            backend, [r[1] for r in rows], record_trace=True,
            task_id=getattr(args, "task_id", None), is_test=1 if getattr(args, "test", False) else 0,
        )
        created = now()
        dimension = len(vectors[0]) if vectors and vectors[0] else 0

        def op(conn: Any, _attempt: int) -> None:
            for (chunk_id, _text, chunk_is_test), vector in zip(rows, vectors):
                conn.execute(
                    "INSERT OR IGNORE INTO chunk_embeddings "
                    "(chunk_id, embedding_model, dim, embedding, created_at, is_test) VALUES (?, ?, ?, vector32(?), ?, ?)",
                    (chunk_id, model, len(vector), json.dumps(vector), created, int(chunk_is_test or 0)),
                )

        write_tx(op)
        built = len(rows)

    result: dict[str, Any] = {"status": "OK", "model": model, "reembedded": built}
    if built:
        result["dimension"] = dimension
    else:
        result["note"] = "no chunks needed this model's index"
    if getattr(args, "activate", False):
        from .model_index import activate_model

        result["activation"] = activate_model(model, keep_all=bool(getattr(args, "keep_all", False)))
    return result


_JUDGE_SYSTEM = (
    "You are a strict evaluation judge. Score the CANDIDATE against the RUBRIC. "
    'Respond with ONLY a JSON object: {"verdict": "pass"|"fail", "score": <0..1>, "rationale": "<one sentence>"}. '
    "Output no prose outside the JSON."
)


def _build_judge_prompt(rubric: str, candidate: str) -> str:
    return f"{_JUDGE_SYSTEM}\n\nRUBRIC:\n{rubric}\n\nCANDIDATE:\n{candidate}\n\nJSON:"


def _parse_verdict(text: str) -> dict[str, Any]:
    """Tolerant parse of the model's JSON verdict; never raises (falls back to 'unparseable')."""
    import re

    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        data = json.loads(match.group(0) if match else (text or ""))
        if not isinstance(data, dict):
            raise ValueError("verdict is not an object")
    except Exception:  # noqa: BLE001 -- any malformed model output degrades to a categorical verdict
        return {"verdict": "unparseable", "score": None, "rationale": ""}
    verdict = str(data.get("verdict", "")).lower().strip() or "unparseable"
    score = data.get("score")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return {"verdict": verdict, "score": score, "rationale": str(data.get("rationale", ""))[:200]}


def _resolve_rubric(args: Any) -> str:
    """Rubric from --query, --expected-json[-file], or an --eval-case-id lookup."""
    rubric = getattr(args, "query", None) or getattr(args, "expected_json", None)
    if not rubric and getattr(args, "expected_json_file", None):
        from .paths import read_text_file

        rubric = read_text_file(args.expected_json_file)
    if not rubric and getattr(args, "eval_case_id", None):
        rows = fetch_all(
            "SELECT summary, expected_json, body FROM eval_cases WHERE id = ?", (args.eval_case_id,)
        )
        if not rows:
            raise KaizenDenied(
                "DENIED_EVAL_CASE_NOT_FOUND",
                {"eval_case_id": args.eval_case_id, "required_action": "pass an existing --eval-case-id, or use --query / --expected-json"},
                exit_code=2,
            )
        summary, expected_json, body = rows[0]
        rubric = expected_json or body or summary
    if not rubric:
        raise KaizenDenied(
            "DENIED_RUBRIC_REQUIRED",
            {"required_action": "pass --query, --expected-json[-file], or --eval-case-id (the rubric to judge against)"},
            exit_code=2,
        )
    return rubric


def _judge_candidate(backend: Any, rubric: str, candidate: str) -> dict[str, Any]:
    """Run one judge call (deterministic); returns the parsed verdict + raw text + usage + latency."""
    prompt = _build_judge_prompt(rubric, candidate)
    started = time.monotonic()
    result = backend.chat(prompt, temperature=0, max_tokens=512)
    latency_ms = int((time.monotonic() - started) * 1000)
    usage = result.get("usage", {}) if isinstance(result.get("usage"), dict) else {}
    return {
        "verdict": _parse_verdict(result.get("text", "")),
        "raw_text": result.get("text", ""),
        "usage": usage,
        "latency_ms": latency_ms,
        "model": result.get("model", backend.model),
    }


def _record_judge_trace(provider: str, model: str, judged: dict[str, Any], task_id, trace_id, summary: str,
                        is_test: int = 0, pii_advisory: list[dict[str, Any]] | None = None) -> str:
    """Insert a kind='judge' trace_event (raw output stored only as a hash ref). Returns its id.
    is_test marks the row (and its cascade-linked eval_score) for K7 purge-test removal.
    pii_advisory (label + span_hash, never raw) lands in tags_json when non-empty."""
    usage = judged["usage"]
    output_ref = utc_text_hash({"text": judged["raw_text"]})
    validate_text_fields({"summary": summary, "body": ""})
    assert_redacted({"summary": summary})
    payload = {
        "kind": "judge",
        "task_id": task_id,
        "trace_id": trace_id,
        "model": model,
        "provider": provider,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "latency_ms": judged["latency_ms"],
        "output_ref": output_ref,
        "status": "recorded",
        "level": "default",
        "summary": summary,
    }
    validate_record("trace_event", {k: v for k, v in payload.items() if v is not None})
    record_id = new_id("te")
    created = now()
    content_hash = utc_text_hash({"id": record_id, **{k: v for k, v in payload.items() if v is not None}})
    tags_json = _pii_tags_json(pii_advisory or [])

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO trace_events "
            "(id, created_at, task_id, trace_id, kind, level, status, model, provider, prompt_tokens, "
            "completion_tokens, total_tokens, latency_ms, tags_json, output_ref, redaction_status, summary, body, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id, created, task_id, trace_id, "judge", "default", "recorded", model, provider,
                payload.get("prompt_tokens"), payload.get("completion_tokens"), payload.get("total_tokens"),
                judged["latency_ms"], tags_json, output_ref, "scanned_clean", summary, "", content_hash, is_test,
            ),
        )

    write_tx(op)
    return record_id


def model_judge(args: Any) -> dict[str, Any]:
    """B4: LLM-as-judge. ADVISORY ONLY -- records a kind='judge' trace + a source='model' eval_score,
    never a verification_events conclusion and never a status change. A human or a deterministic
    verifier remains the acceptance gate."""
    backend = get_text_backend()
    if backend is None:
        raise KaizenDenied(
            "DENIED_BACKEND_UNCONFIGURED",
            {"required_action": "set KAIZEN_LLM_MODEL (and optionally KAIZEN_LLM_BASE_URL), or KAIZEN_TEXT_BACKEND=transformers -- see setup/OLLAMA.md"},
            exit_code=2,
        )
    candidate = _text_arg(args, "body", "") or (getattr(args, "prompt", None) or "")
    if not candidate:
        raise KaizenDenied(
            "DENIED_CANDIDATE_REQUIRED",
            {"required_action": "pass --body/--body-file or --prompt (the candidate output to judge)"},
            exit_code=2,
        )
    rubric = _resolve_rubric(args)
    assert_redacted({"candidate": candidate, "rubric": rubric})

    if getattr(args, "dry_run", False):
        prompt = _build_judge_prompt(rubric, candidate)
        return {"status": "OK", "dry_run": True, "advisory": True, "prompt_sha256": utc_text_hash({"p": prompt})}

    # Advisory PII NER over the candidate (after the enforced regex gate); [] unless a PII backend is set.
    pii_advisory = advisory_pii_scan(
        candidate,
        record_trace=True,
        task_id=getattr(args, "task_id", None),
        is_test=1 if getattr(args, "test", False) else 0,
    )
    judged = _judge_candidate(backend, rubric, candidate)
    verdict = judged["verdict"]
    summary = _text_arg(
        args, "summary", f"Advisory judge verdict={verdict['verdict']} (kind=judge); output stored as a hash ref."
    )
    trace_event_id = _record_judge_trace(
        backend.name, judged["model"], judged, getattr(args, "task_id", None), getattr(args, "trace_id", None),
        summary, 1 if getattr(args, "test", False) else 0, pii_advisory,
    )

    if verdict["score"] is not None:
        value, data_type = verdict["score"], "numeric"
    else:
        value, data_type = verdict["verdict"], "categorical"
    # Drop the rationale from the durable comment if it would trip redaction (never fail the op on it).
    comment = verdict["rationale"] if verdict["rationale"] and not scan_for_secrets(verdict["rationale"]) else None
    score_payload = {
        "trace_event_id": trace_event_id,
        "verification_id": getattr(args, "proof_id", None),
        "name": getattr(args, "metric", None) or "model_judge",
        "value": value,
        "data_type": data_type,
        "source": "model",
        "comment": comment,
    }
    from .trace_records import write_eval_score

    score = write_eval_score({k: v for k, v in score_payload.items() if v is not None},
                             is_test=1 if getattr(args, "test", False) else 0)

    result = {
        "status": "OK",
        "advisory": True,
        "trace_event_id": trace_event_id,
        "score_id": score["id"],
        "verdict": verdict["verdict"],
        "score": verdict["score"],
        "rationale": verdict["rationale"],
        "model": judged["model"],
        "provider": backend.name,
        "latency_ms": judged["latency_ms"],
        "note": "advisory only; not an acceptance authority unless a deterministic verifier confirms.",
    }
    if pii_advisory:
        result["pii_advisory"] = pii_advisory
    return result
