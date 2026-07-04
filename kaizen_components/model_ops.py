"""Model-backend ops (B*): doctor, advisory text run, and re-embed.

- ``B1 model-doctor`` probes the configured embedding + text backends (reachable? model? dim?).
- ``B2 model-run`` sends an advisory prompt to the text backend and records a ``model_call``
  trace (redaction-by-default). It is ADVISORY ONLY — never the final acceptance authority unless
  a deterministic verifier confirms; the caller, not the model, enforces that.
- ``B3 reembed`` backfills ``evidence_chunks.embedding`` (Turso ``vector32``) for the configured
  embedding model, so E4 ``--semantic`` can rank by cosine distance.

All three degrade gracefully when no backend is configured/reachable.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .backends import embed_batched, get_embedding_backend, get_text_backend
from .db import fetch_all, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import utc_text_hash, validate_text_fields
from .redaction import assert_redacted
from .schemas import validate_record
from .task_records import _text_arg


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

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO trace_events "
            "(id, created_at, task_id, trace_id, kind, level, status, model, provider, prompt_tokens, "
            "completion_tokens, total_tokens, latency_ms, output_ref, redaction_status, summary, body, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id, created, payload.get("task_id"), payload.get("trace_id"), "model_call", "default",
                "recorded", payload["model"], payload["provider"], payload.get("prompt_tokens"),
                payload.get("completion_tokens"), payload.get("total_tokens"), latency_ms, output_ref,
                "scanned_clean", summary, "", content_hash,
            ),
        )

    write_tx(op)
    return {
        "status": "OK",
        "id": record_id,
        "advisory": True,
        "model": payload["model"],
        "provider": payload["provider"],
        "output_ref": output_ref,
        "tokens": {"prompt": payload.get("prompt_tokens"), "completion": payload.get("completion_tokens"), "total": payload.get("total_tokens")},
        "latency_ms": latency_ms,
        "note": "advisory only; not an acceptance authority unless a deterministic verifier confirms.",
    }


def reembed(args: Any) -> dict[str, Any]:
    """B3: backfill evidence_chunks.embedding for the configured embedding model."""
    backend = get_embedding_backend()
    if backend is None:
        raise KaizenDenied(
            "DENIED_BACKEND_UNCONFIGURED",
            {"required_action": "set KAIZEN_EMBED_MODEL (and optionally KAIZEN_EMBED_BASE_URL) -- see setup/OLLAMA.md"},
            exit_code=2,
        )
    document_id = getattr(args, "id", None)
    where = "embedding IS NULL OR embedding_model IS NULL OR embedding_model <> ?"
    if document_id:
        rows = fetch_all(
            f"SELECT id, text FROM evidence_chunks WHERE document_id = ? AND ({where}) ORDER BY chunk_index",
            (document_id, backend.model),
        )
    else:
        rows = fetch_all(f"SELECT id, text FROM evidence_chunks WHERE {where} ORDER BY created_at", (backend.model,))
    if not rows:
        return {"status": "OK", "reembedded": 0, "model": backend.model, "note": "no chunks needed embedding"}

    # Batched network calls OUTSIDE the tx; raises DENIED if unreachable or on a
    # per-batch count mismatch (large corpora would blow request-size limits unbatched).
    vectors = embed_batched(backend, [r[1] for r in rows])
    model = backend.model

    def op(conn: Any, _attempt: int) -> None:
        for (chunk_id, _text), vector in zip(rows, vectors):
            conn.execute(
                "UPDATE evidence_chunks SET embedding = vector32(?), embedding_model = ? WHERE id = ?",
                (json.dumps(vector), model, chunk_id),
            )

    write_tx(op)
    return {"status": "OK", "reembedded": len(rows), "model": model, "dimension": len(vectors[0]) if vectors and vectors[0] else 0}
