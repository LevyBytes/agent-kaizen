"""External-source ingestion plane (Layer A).

Turns outside material into source-locked, validated, LLM-usable records:
`source_locks` (provenance + hash) -> `evidence_documents` -> `evidence_blocks`
-> `evidence_chunks`. Tier-1 readers (txt/md/html/csv) are pure stdlib and the
always-available default; PDF/DOCX/XLSX are optional capability-activated backends
(pypdf/python-docx/openpyxl) that degrade gracefully when absent. Chunks keep
neighbor links so any retrieved chunk can reopen its surrounding source.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from .db import fetch_all, fetch_one, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import file_sha256, utc_text_hash, validate_text_fields
from .paths import path_in_repo, repo_relative, resolve_user_path
from .schemas import KAIZEN_ENUMS, validate_record
from .task_records import _text_arg


_MEDIA = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _strip_html(raw: str) -> str:
    no_scripts = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", no_scripts)
    text = html.unescape(text)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


def _backend_unavailable(ext: str, package: str) -> KaizenDenied:
    return KaizenDenied(
        "DENIED_BACKEND_UNAVAILABLE",
        {
            "ext": ext,
            "package": package,
            "required_action": f"install the opt-in doc backends: pip install -r requirements-docs.txt (provides {package})",
        },
        exit_code=2,
    )


# Guards for hostile or degenerate PDFs. Env-overridable so operators can tune them and tests
# can shrink them without writing multi-MB fixtures; the defaults are the shipped policy.
MAX_PDF_BYTES = int(os.environ.get("KAIZEN_MAX_PDF_BYTES") or 25 * 1024 * 1024)
MAX_PDF_PAGES = int(os.environ.get("KAIZEN_MAX_PDF_PAGES") or 500)


def _extract_pdf(path: Path) -> tuple[str, str, str, float]:
    # Size gate first, BEFORE the pypdf import: it needs no optional dependency and
    # stops a hostile multi-GB file before any parsing work happens.
    size = path.stat().st_size
    if size > MAX_PDF_BYTES:
        raise KaizenDenied(
            "DENIED_FILE_TOO_LARGE",
            {
                "path": str(path),
                "bytes": size,
                "max_bytes": MAX_PDF_BYTES,
                "required_action": "keep PDFs under the inline-ingestion size cap or pre-extract the text to .txt/.md",
            },
            exit_code=2,
        )
    try:
        import pypdf  # type: ignore
    except Exception as error:
        raise _backend_unavailable(".pdf", "pypdf") from error
    try:
        reader = pypdf.PdfReader(str(path))
        if reader.is_encrypted:
            raise KaizenDenied(
                "DENIED_PDF_ENCRYPTED",
                {
                    "path": str(path),
                    "required_action": "decrypt or re-export the PDF without a password before ingesting",
                },
                exit_code=2,
            )
        pages = len(reader.pages)
        if pages > MAX_PDF_PAGES:
            raise KaizenDenied(
                "DENIED_PDF_TOO_MANY_PAGES",
                {
                    "path": str(path),
                    "pages": pages,
                    "max_pages": MAX_PDF_PAGES,
                    "required_action": "split the PDF or pre-extract the text to .txt/.md",
                },
                exit_code=2,
            )
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except KaizenDenied:
        raise
    except Exception as error:
        # Malformed PDFs surface as assorted pypdf/struct errors; keep them structured.
        raise KaizenDenied(
            "DENIED_PDF_UNREADABLE",
            {
                "path": str(path),
                "reason": str(error),
                "required_action": "repair the PDF or pre-extract the text to .txt/.md",
            },
            exit_code=2,
        ) from error
    if not text.strip():
        raise KaizenDenied(
            "DENIED_PDF_NO_TEXT",
            {
                "path": str(path),
                "required_action": "the PDF has no extractable text (likely scanned); OCR or pre-extract it externally",
            },
            exit_code=2,
        )
    return text, "pypdf", "pdftext", 0.8


def _extract_docx(path: Path) -> tuple[str, str, str, float]:
    try:
        import docx  # type: ignore
    except Exception as error:
        raise _backend_unavailable(".docx", "python-docx") from error
    document = docx.Document(str(path))
    text = "\n\n".join(p.text for p in document.paragraphs)
    return text, "python-docx", "native", 0.9


def _extract_xlsx(path: Path) -> tuple[str, str, str, float]:
    try:
        import openpyxl  # type: ignore
    except Exception as error:
        raise _backend_unavailable(".xlsx", "openpyxl") from error
    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    lines: list[str] = []
    for sheet in workbook.worksheets:
        lines.append(f"# {sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                lines.append(", ".join(cells))
    return "\n".join(lines), "openpyxl", "native", 0.9


def _extract_text(path: Path) -> tuple[str, str, str, str, float]:
    """Return (text, media_type, backend, extraction_method, confidence)."""
    ext = path.suffix.lower()
    media = _MEDIA.get(ext, "application/octet-stream")
    if ext in {".txt", ".md", ".markdown", ".csv"}:
        return path.read_text(encoding="utf-8-sig", errors="replace"), media, "native", "native", 1.0
    if ext in {".html", ".htm"}:
        return _strip_html(path.read_text(encoding="utf-8-sig", errors="replace")), media, "native", "native", 0.9
    if ext == ".pdf":
        text, backend, method, conf = _extract_pdf(path)
        return text, media, backend, method, conf
    if ext == ".docx":
        text, backend, method, conf = _extract_docx(path)
        return text, media, backend, method, conf
    if ext == ".xlsx":
        text, backend, method, conf = _extract_xlsx(path)
        return text, media, backend, method, conf
    raise KaizenDenied(
        "DENIED_UNSUPPORTED_MEDIA",
        {
            "ext": ext,
            "required_action": "supported natively: .txt .md .html .csv; .pdf/.docx/.xlsx via the optional doc backends (pip install -r requirements-docs.txt)",
        },
        exit_code=2,
    )


def _split_blocks(text: str) -> list[tuple[str, str]]:
    """Split into (block_type, block_text) on blank lines; markdown headings -> SectionHeader."""
    blocks: list[tuple[str, str]] = []
    for raw in re.split(r"\n\s*\n", text):
        chunk = raw.strip()
        if not chunk:
            continue
        block_type = "SectionHeader" if chunk.lstrip().startswith("#") else "Text"
        blocks.append((block_type, chunk))
    return blocks


def _heading_before(text: str, position: int) -> str:
    head = text[:position]
    matches = re.findall(r"(?m)^#{1,6}\s+(.+)$", head)
    return matches[-1].strip() if matches else ""


def _recursive_chunk(text: str, max_chars: int = 1024) -> list[tuple[str, int, int]]:
    """Deterministic char-window chunker that prefers paragraph/sentence breaks."""
    chunks: list[tuple[str, int, int]] = []
    n = len(text)
    i = 0
    while i < n:
        end = min(i + max_chars, n)
        if end < n:
            window = text[i:end]
            cut = window.rfind("\n\n")
            if cut == -1:
                cut = window.rfind(". ")
            if cut > max_chars * 0.5:
                end = i + cut + 1
        raw = text[i:end]
        body = raw.strip()
        if body:
            # start/end bound the stored (stripped) text, not the raw window, so reopening
            # text[start:end] returns exactly the persisted chunk.
            start = i + (len(raw) - len(raw.lstrip()))
            chunks.append((body, start, start + len(body)))
        i = end
    return chunks


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _escape_like(query: str) -> str:
    """Escape LIKE wildcards so a literal % or _ in the query cannot over-match."""
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _split_segments(text: str) -> list[tuple[str, int]]:
    """Paragraph-ish segments with their start offset in ``text`` (for semantic chunking)."""
    segments: list[tuple[str, int]] = []
    for match in re.finditer(r"[^\n].*?(?=\n\s*\n|\Z)", text, re.DOTALL):
        raw = match.group()
        seg = raw.strip()
        if seg:
            start = match.start() + (len(raw) - len(raw.lstrip()))
            segments.append((seg, start))
    return segments


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - dot / (na * nb)


def _semantic_chunk(
    segments: list[tuple[str, int]],
    embeddings: list[list[float]],
    *,
    max_chars: int = 1024,
    threshold: float = 0.35,
) -> list[tuple[str, int, int]]:
    """Merge consecutive segments into chunks, breaking where the adjacent-segment embedding
    distance exceeds ``threshold`` or the running chunk would exceed ``max_chars``."""
    chunks: list[tuple[str, int, int]] = []
    cur_text = ""
    cur_start: int | None = None
    for index, (seg, start) in enumerate(segments):
        if cur_start is None:
            cur_text, cur_start = seg, start
            continue
        boundary = _cosine_distance(embeddings[index - 1], embeddings[index]) > threshold
        too_big = len(cur_text) + len(seg) + 2 > max_chars
        if boundary or too_big:
            chunks.append((cur_text, cur_start, cur_start + len(cur_text)))
            cur_text, cur_start = seg, start
        else:
            cur_text = cur_text + "\n\n" + seg
    if cur_start is not None:
        chunks.append((cur_text, cur_start, cur_start + len(cur_text)))
    return chunks


def ingest_file(args: Any) -> dict[str, Any]:
    allow_external = bool(getattr(args, "allow_external", False))
    path = resolve_user_path(
        getattr(args, "path", None),
        require_file=True,
        repo_only=not allow_external,
        allow_external_hint=not allow_external,
    )

    text, media_type, backend, extraction_method, confidence = _extract_text(path)
    sha = file_sha256(path)
    # External ingests store a sanitized origin (basename + content hash), never an
    # absolute machine path: text fields are redaction-gated, so the path column and
    # the derived summary must honor the same private-by-default rule.
    rel = repo_relative(path) if path_in_repo(path) else f"external:{path.name}"
    blocks = _split_blocks(text)

    summary = _text_arg(args, "summary", f"Ingested {rel} ({len(blocks)} blocks).")
    validate_text_fields({"summary": summary, "body": ""})

    source_id = getattr(args, "source_id", None) or path.name
    authority_tier = getattr(args, "authority_tier", None) or "implementation"
    validate_record(
        "source_lock",
        {
            "source_id": source_id,
            "authority_tier": authority_tier,
            "url_or_repository": rel,
            "content_hash": sha,
            "summary": summary,
        },
    )
    document_id = new_id("ed")
    source_lock_id = new_id("s")
    created = now()
    doc_hash = utc_text_hash({"id": document_id, "ref": rel, "sha256": sha, "blocks": len(blocks)})

    doc_payload = {
        "source_lock_id": source_lock_id,
        "task_id": getattr(args, "task_id", None),
        "origin_kind": "file",
        "origin_ref": rel,
        "media_type": media_type,
        "backend": backend,
        "extraction_method": extraction_method,
        "extraction_confidence": confidence,
        "block_count": len(blocks),
        "chunk_count": 0,
        "summary": summary,
    }
    validate_record("evidence_document", {k: v for k, v in doc_payload.items() if v is not None})

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO source_locks "
            "(id, created_at, source_id, authority_tier, url_or_repository, version_or_commit, retrieved_at, "
            "content_hash, license, supersedes, summary, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_lock_id,
                created,
                source_id,
                authority_tier,
                rel,
                getattr(args, "version_or_commit", None),
                created,
                sha,
                getattr(args, "license", None),
                getattr(args, "supersedes", None),
                summary,
                "",
            ),
        )
        conn.execute(
            "INSERT INTO evidence_documents "
            "(id, created_at, source_lock_id, task_id, origin_kind, origin_ref, media_type, backend, "
            "backend_version, extraction_method, extraction_confidence, block_count, chunk_count, summary, body, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                created,
                source_lock_id,
                getattr(args, "task_id", None),
                "file",
                rel,
                media_type,
                backend,
                None,
                extraction_method,
                confidence,
                len(blocks),
                0,
                summary,
                "",
                doc_hash,
            ),
        )
        for index, (block_type, block_text) in enumerate(blocks):
            block_id = new_id("eb")
            block_hash = utc_text_hash({"id": block_id, "text": block_text})
            conn.execute(
                "INSERT INTO evidence_blocks "
                "(id, created_at, document_id, block_index, block_type, page_no, bbox_json, section_path, "
                "text, image_ref, extraction_method, confidence, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    block_id,
                    created,
                    document_id,
                    index,
                    block_type,
                    None,
                    None,
                    None,
                    block_text,
                    None,
                    extraction_method,
                    confidence,
                    block_hash,
                ),
            )

    write_tx(op)
    return {
        "status": "OK",
        "id": document_id,
        "source_lock_id": source_lock_id,
        "origin_ref": rel,
        "sha256": sha,
        "backend": backend,
        "extraction_confidence": confidence,
        "block_count": len(blocks),
    }


def chunk_document(args: Any) -> dict[str, Any]:
    document_id = getattr(args, "id", None)
    if not document_id:
        raise KaizenDenied("DENIED_ID_REQUIRED", {"required_action": "resubmit with --id (document id)"}, exit_code=2)
    doc = fetch_one("SELECT id, source_lock_id FROM evidence_documents WHERE id = ?", (document_id,))
    if doc is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": document_id, "table": "evidence_documents"}, exit_code=1)
    source_lock_id = doc[1]
    block_rows = fetch_all(
        "SELECT text FROM evidence_blocks WHERE document_id = ? ORDER BY block_index", (document_id,)
    )
    if not block_rows:
        raise KaizenDenied("DENIED_NO_BLOCKS", {"id": document_id, "required_action": "ingest the document first"}, exit_code=1)
    full_text = "\n\n".join(r[0] for r in block_rows)

    chunker = getattr(args, "chunker", None) or "recursive"
    # `recursive` (default, deterministic) and `semantic` (embedder-backed) are implemented; the
    # remaining enum values (e.g. `neural`) are reserved. Rejecting unimplemented strategies up front
    # keeps the stored `chunker` label honest about the algorithm that actually ran.
    implemented = ("recursive", "semantic")
    if chunker not in implemented:
        raise KaizenDenied(
            "DENIED_CHUNKER_UNSUPPORTED",
            {
                "chunker": chunker,
                "implemented": list(implemented),
                "reserved": [c for c in KAIZEN_ENUMS["chunker"] if c not in implemented],
                "required_action": "use --chunker recursive (default) or semantic (needs an embedding backend)",
            },
            exit_code=2,
        )
    if chunker == "semantic":
        from .backends import embed_batched, get_embedding_backend

        sem_backend = get_embedding_backend()
        if sem_backend is None:
            raise KaizenDenied(
                "DENIED_BACKEND_UNCONFIGURED",
                {"required_action": "semantic chunking needs an embedding backend: set KAIZEN_EMBED_MODEL (Ollama) or KAIZEN_EMBED_BACKEND=sentence-transformers"},
                exit_code=2,
            )
        segments = _split_segments(full_text)
        if len(segments) > 1:
            seg_vectors = embed_batched(sem_backend, [seg for seg, _start in segments])  # raises DENIED if the backend/extra is absent
            pieces = _semantic_chunk(segments, seg_vectors)
        else:
            pieces = _recursive_chunk(full_text)
    else:
        pieces = _recursive_chunk(full_text)
    created = now()
    chunk_ids = [new_id("ec") for _ in pieces]

    # Build and schema-gate every chunk payload before writing, so the registered
    # evidence_chunk contract (chunker enum, context length) actually guards the write.
    records: list[tuple[str, dict[str, Any]]] = []
    for index, ((body, start, end), chunk_id) in enumerate(zip(pieces, chunk_ids)):
        payload = {
            "document_id": document_id,
            "source_lock_id": source_lock_id,
            "chunk_index": index,
            "text": body,
            "start_index": start,
            "end_index": end,
            "token_count": _estimate_tokens(body),
            # cap derived heading context to the schema bound so a long heading never aborts ingest
            "context": " ".join(_heading_before(full_text, start).split()[:60]),
            "chunker": chunker,
            "backend": "native",
            "neighbor_prev_id": chunk_ids[index - 1] if index > 0 else None,
            "neighbor_next_id": chunk_ids[index + 1] if index < len(chunk_ids) - 1 else None,
        }
        validate_record("evidence_chunk", {k: v for k, v in payload.items() if v is not None})
        records.append((chunk_id, payload))

    # Optional embedding payoff: if an embedding backend is configured (opt-in via
    # KAIZEN_EMBED_MODEL), embed each chunk so E4 --semantic can rank by cosine. Absent or
    # unreachable -> store chunks without embeddings (the deterministic, lexical baseline).
    embeddings: list[list[float]] | None = None
    embedding_model: str | None = None
    embedding_note: str | None = None
    from .backends import embed_batched, get_embedding_backend

    _embed_backend = get_embedding_backend()
    if _embed_backend is not None:
        try:
            # Batched: one unbatched call over a large corpus risks request-size and
            # memory failures. A wrong per-batch count is a data-integrity fault and
            # denies (same contract as B3 reembed) instead of silently storing
            # unembedded chunks.
            embeddings = embed_batched(_embed_backend, [payload["text"] for _cid, payload in records])
            embedding_model = _embed_backend.model
        except KaizenDenied as denied:
            if denied.code == "DENIED_EMBED_MISMATCH":
                raise
            # Configured but unreachable -> graceful: store chunks without embeddings,
            # but say so in the payload instead of degrading silently.
            embeddings = None
            embedding_note = f"embedding backend unreachable ({denied.code}); chunks stored without embeddings, run B3 later"

    def op(conn: Any, _attempt: int) -> None:
        for chunk_id, payload in records:
            chunk_hash = utc_text_hash({"id": chunk_id, "text": payload["text"], "doc": document_id})
            conn.execute(
                "INSERT INTO evidence_chunks "
                "(id, created_at, document_id, source_lock_id, chunk_index, text, start_index, end_index, "
                "token_count, context, chunker, backend, embedding, embedding_model, neighbor_prev_id, "
                "neighbor_next_id, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk_id,
                    created,
                    document_id,
                    source_lock_id,
                    payload["chunk_index"],
                    payload["text"],
                    payload["start_index"],
                    payload["end_index"],
                    payload["token_count"],
                    payload["context"],
                    payload["chunker"],
                    payload["backend"],
                    None,
                    None,
                    payload["neighbor_prev_id"],
                    payload["neighbor_next_id"],
                    chunk_hash,
                ),
            )
        if embeddings is not None:
            for (chunk_id, _payload), vector in zip(records, embeddings):
                conn.execute(
                    "UPDATE evidence_chunks SET embedding = vector32(?), embedding_model = ? WHERE id = ?",
                    (json.dumps(vector), embedding_model, chunk_id),
                )
        conn.execute(
            "UPDATE evidence_documents SET chunk_count = ? WHERE id = ?",
            (len(records), document_id),
        )

    write_tx(op)
    result = {
        "status": "OK",
        "document_id": document_id,
        "chunk_count": len(records),
        "chunker": chunker,
        "embedded": embeddings is not None,
        "embedding_model": embedding_model,
    }
    if embedding_note:
        result["embedding_note"] = embedding_note
    return result


def query_evidence(args: Any) -> dict[str, Any]:
    query = getattr(args, "query", None)
    if not query:
        raise KaizenDenied("DENIED_QUERY_REQUIRED", {"required_action": "resubmit with --query"}, exit_code=2)
    limit = int(getattr(args, "limit", None) or 10)
    # Semantic vector mode is OPT-IN (--semantic) so E4 never makes a silent network call.
    if getattr(args, "semantic", False):
        from .backends import get_embedding_backend

        backend = get_embedding_backend()
        if backend is None:
            raise KaizenDenied(
                "DENIED_BACKEND_UNCONFIGURED",
                {"required_action": "set KAIZEN_EMBED_MODEL for --semantic search, or drop --semantic for lexical"},
                exit_code=2,
            )
        # Pre-flight: cosine distance over mismatched embedding spaces ranks silently
        # wrong (e.g. 768-dim Ollama chunks queried with a 384-dim sentence-transformers
        # vector). Deny unless every stored embedding came from the active model.
        stored_models = [
            r[0]
            for r in fetch_all(
                "SELECT DISTINCT embedding_model FROM evidence_chunks WHERE embedding IS NOT NULL"
            )
            if r[0]
        ]
        foreign_models = [m for m in stored_models if m != backend.model]
        if foreign_models:
            raise KaizenDenied(
                "DENIED_EMBED_MODEL_MISMATCH",
                {
                    "active_model": backend.model,
                    "stored_models": stored_models,
                    "required_action": "run B3 (reembed) to re-embed the corpus with the active model, or set KAIZEN_EMBED_MODEL to a stored model",
                },
                exit_code=2,
            )
        query_vectors = backend.embed([query])  # raises DENIED if the backend is unreachable
        if not query_vectors or not query_vectors[0]:
            raise KaizenDenied(
                "DENIED_EMBED_EMPTY",
                {"required_action": "embedding backend returned no vector for the query; check the model with B1"},
                exit_code=1,
            )
        qvec = query_vectors[0]
        vrows = fetch_all(
            "SELECT id, document_id, source_lock_id, context, neighbor_prev_id, neighbor_next_id, "
            "substr(text, 1, 240), vector_distance_cos(embedding, vector32(?)) AS dist "
            "FROM evidence_chunks WHERE embedding IS NOT NULL ORDER BY dist ASC LIMIT ?",
            (json.dumps(qvec), limit),
        )
        if vrows:
            return {
                "status": "OK",
                "mode": "semantic",
                "query": query,
                "embedding_model": backend.model,
                "note": "ranked by vector_distance_cos over Turso-native vectors.",
                "records": [
                    {
                        "id": r[0],
                        "document_id": r[1],
                        "source_lock_id": r[2],
                        "context": r[3],
                        "neighbor_prev_id": r[4],
                        "neighbor_next_id": r[5],
                        "snippet": r[6],
                        "score": r[7],
                    }
                    for r in vrows
                ],
            }
        # No embeddings stored yet -> fall through to the lexical baseline below (run E3/B3 to embed).
    rows: list[tuple[Any, ...]] | None = None
    mode = "like"
    # Turso FTS (Tantivy) is an experimental engine feature in the current build; opt in only
    # when the engine is started with it enabled. Lexical LIKE is the always-available baseline.
    # The FTS index itself is created during K1 check/init (env-gated), never on this hot path.
    if os.environ.get("KAIZEN_TURSO_FTS") == "1":
        try:
            rows = fetch_all(
                "SELECT id, document_id, source_lock_id, context, neighbor_prev_id, neighbor_next_id, "
                "substr(text, 1, 240), fts_score(text, ?) AS score FROM evidence_chunks "
                "WHERE fts_match(text, ?) ORDER BY score LIMIT ?",
                (query, query, limit),
            )
            mode = "fts"
        except Exception:
            rows = None
    if rows is None:
        pattern = f"%{_escape_like(query)}%"
        rows = fetch_all(
            "SELECT id, document_id, source_lock_id, context, neighbor_prev_id, neighbor_next_id, "
            "substr(text, 1, 240), 0 FROM evidence_chunks WHERE text LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC LIMIT ?",
            (pattern, limit),
        )
    return {
        "status": "OK",
        "mode": mode,
        "query": query,
        "note": "lexical=LIKE (Turso FTS is experimental; opt in with KAIZEN_TURSO_FTS=1). Turso-native vector search activates once an embedding backend is configured.",
        "records": [
            {
                "id": r[0],
                "document_id": r[1],
                "source_lock_id": r[2],
                "context": r[3],
                "neighbor_prev_id": r[4],
                "neighbor_next_id": r[5],
                "snippet": r[6],
                "score": r[7],
            }
            for r in rows
        ],
    }


def inspect_evidence(args: Any) -> dict[str, Any]:
    record_id = getattr(args, "id", None)
    if not record_id:
        raise KaizenDenied("DENIED_ID_REQUIRED", {"required_action": "resubmit with --id"}, exit_code=2)
    kind = getattr(args, "kind", None) or "document"
    table = {"document": "evidence_documents", "block": "evidence_blocks", "chunk": "evidence_chunks"}.get(kind)
    if table is None:
        raise KaizenDenied(
            "DENIED_KIND_INVALID",
            {"kind": kind, "required_action": "use --kind document|block|chunk"},
            exit_code=2,
        )
    row = fetch_one(f"SELECT * FROM {table} WHERE id = ?", (record_id,))
    if row is None:
        raise KaizenDenied("DENIED_RECORD_NOT_FOUND", {"id": record_id, "table": table}, exit_code=1)
    columns = [r[1] for r in fetch_all(f"PRAGMA table_info({table})")]
    record = dict(zip(columns, row))
    record.pop("embedding", None)
    return {"status": "OK", "record": record}
