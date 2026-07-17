"""B5 pii-scan: advisory PII scan that AUGMENTS the deterministic regex redaction gate.

The regex ``assert_redacted`` (redaction.py) stays the SOLE thing that can deny a write. This adds an
OPTIONAL model NER (GLiNER2) for extra recall -- advisory only, never a gate. Model hits are stored as
entity label + a hashed span (never raw PII), matching the private-by-default rule. B5 writes a durable
``pii_scan`` record.
"""

from __future__ import annotations

import json
from typing import Any

from .db import new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import utc_text_hash
from .redaction import assert_redacted, scan_for_secrets
from .schemas import validate_record
from .task_records import _text_arg


def _stable_text(path: Any) -> str:
    """Read one UTF-8 file through the handle-anchored authority, rejecting swaps and oversize files."""
    from .orchestration.workspace_path_authority import MAX_AUTHORITY_BYTES, WorkspacePathAuthority, WorkspacePathError

    try:
        with WorkspacePathAuthority(path.parent) as authority:
            return authority.read(path.name, MAX_AUTHORITY_BYTES).data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise KaizenDenied("DENIED_FILE_NOT_UTF8", {"required_action": "re-encode the file as UTF-8"}, exit_code=2) from error
    except WorkspacePathError as error:
        raise KaizenDenied("DENIED_FILE_NOT_FOUND", {"required_action": "pass a stable regular file"}, exit_code=1) from error


def advisory_pii_scan(
    text: str,
    *,
    record_trace: bool = False,
    task_id: str | None = None,
    is_test: int = 0,
    backend: Any | None = None,
) -> list[dict[str, Any]]:
    """Optional model-NER hits as ``[{source:'model', label, span_hash}]``; [] when no PII backend.

    ``record_trace=True`` writes one best-effort ``model_call`` observability trace (lane ``pii``) so
    the GLiNER model use shows in the B6 monitor. ``task_id`` and ``is_test`` affect only that optional
    trace; neither changes returned hits. B2/B4/B5 all route their PII scan through here.
    """
    from .backends import get_pii_backend

    backend = backend if backend is not None else get_pii_backend()
    if backend is None:
        return []
    import time

    started = time.monotonic()
    raw_hits = list(backend.scan(text))
    if record_trace:
        from .trace_records import record_model_call

        record_model_call(
            lane="pii",
            model=getattr(backend, "model", None),
            provider=getattr(backend, "name", None),
            latency_ms=int((time.monotonic() - started) * 1000),
            count=1,
            task_id=task_id,
            is_test=is_test,
        )
    hits: list[dict[str, Any]] = []
    for hit in raw_hits:
        start, end = hit.get("start"), hit.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end <= len(text):
            continue
        span = text[start:end]
        hits.append({"source": "model", "label": hit.get("label", "pii"), "span_hash": utc_text_hash({"s": span})})
    return hits


def _regex_hits(text: str) -> list[dict[str, Any]]:
    """Regex baseline hits (label only; the enforced gate lives in redaction.assert_redacted)."""
    return [{"source": "regex", "label": name, "span_hash": None} for name in scan_for_secrets(text)]


def pii_scan(args: Any) -> dict[str, Any]:
    """B5: advisory PII scan over --prompt / --body / --path text. Regex baseline + optional model NER.
    Writes a durable pii_scan record (hashed spans only). ADVISORY ONLY -- never gates a write."""
    text = getattr(args, "prompt", None)
    source_ref = "prompt"
    if text is None and getattr(args, "body", None) is not None:
        text, source_ref = args.body, "body"
    if text is None and getattr(args, "path", None):
        from .paths import resolve_user_path

        allow_external = bool(getattr(args, "allow_external", False))
        path = resolve_user_path(
            args.path, require_file=True, repo_only=not allow_external, allow_external_hint=not allow_external
        )
        text, source_ref = _stable_text(path), "path"
    if text is None:
        raise KaizenDenied(
            "DENIED_PII_TEXT_REQUIRED",
            {"required_action": "pass --prompt, --body, or --path (the text to scan)"},
            exit_code=2,
        )

    regex_hits = _regex_hits(text)
    from .backends import get_pii_backend

    backend = get_pii_backend()
    is_test = 1 if getattr(args, "test", False) else 0
    model_hits = advisory_pii_scan(
        text, record_trace=True, task_id=getattr(args, "task_id", None), is_test=is_test, backend=backend,
    ) if backend is not None else []
    hits = regex_hits + model_hits
    summary = _text_arg(
        args, "summary", f"Advisory PII scan: {len(regex_hits)} regex, {len(model_hits)} model hit(s)."
    )
    payload = {
        "task_id": getattr(args, "task_id", None),
        "trace_id": getattr(args, "trace_id", None),
        "source_ref": source_ref,
        "regex_hit_count": len(regex_hits),
        "model_hit_count": len(model_hits),
        "hits": hits,
        "model": backend.model if backend else None,
        "provider": backend.name if backend else None,
        "summary": summary,
    }
    assert_redacted({"summary": summary})
    validate_record("pii_scan", {k: v for k, v in payload.items() if v is not None})

    record_id = new_id("pii")
    created = now()
    content_hash = utc_text_hash({"id": record_id, **{k: v for k, v in payload.items() if v is not None}})

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO pii_scan "
            "(id, created_at, task_id, trace_id, source_ref, regex_hit_count, model_hit_count, hits_json, "
            "model, provider, summary, content_hash, is_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id, created, payload.get("task_id"), payload.get("trace_id"), source_ref,
                len(regex_hits), len(model_hits), json.dumps(hits), payload.get("model"),
                payload.get("provider"), summary, content_hash, is_test,
            ),
        )

    write_tx(op)
    return {
        "status": "OK",
        "id": record_id,
        "advisory": True,
        "regex_hits": regex_hits,
        "model_hits": model_hits,
        "note": "advisory only; the regex redaction gate remains the sole enforced check.",
    }
