"""O5 lab-dedup: cluster near-duplicate GOTCHA/LEARNED/LEARNING/evidence records by cosine over
embeddings, for HUMAN triage. It NEVER merges or mutates records -- it writes a markdown report + a
``reports`` row (reusing the O1/O3 report-write pattern) that a human reviews.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any

from .backends import embed_batched, get_embedding_backend
from .db import fetch_all, new_id, now, write_tx
from .denials import KaizenDenied
from .hashing import file_sha256, utc_text_hash
from .paths import EXPORT_ROOT, repo_relative

_LIFECYCLE_KINDS = {
    "gotcha": "SELECT id, title, summary FROM gotcha WHERE status = 'active' ORDER BY created_at DESC LIMIT ?",
    "learned": "SELECT id, title, summary FROM learned ORDER BY created_at DESC LIMIT ?",
    "learning": "SELECT id, title, summary FROM learning ORDER BY created_at DESC LIMIT ?",
}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _single_link_clusters(vectors: list[list[float]], threshold: float) -> list[list[int]]:
    """Union-find single-link clustering on cosine >= threshold. Returns lists of member indices."""
    n = len(vectors)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _cosine(vectors[i], vectors[j]) >= threshold:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [sorted(members) for members in groups.values()]


def _same_dim(ids: list, texts: list, vectors: list[list[float]]) -> tuple[list, list, list]:
    """Keep only the most common vector dimension so cosine never zip-truncates across mixed
    embedding models (stored evidence chunks may span embedder revisions)."""
    if not vectors:
        return ids, texts, vectors
    dim = Counter(len(v) for v in vectors).most_common(1)[0][0]
    keep = [i for i, v in enumerate(vectors) if len(v) == dim]
    return [ids[i] for i in keep], [texts[i] for i in keep], [vectors[i] for i in keep]


def cluster_records(args: Any) -> dict[str, Any]:
    kind = (getattr(args, "kind", None) or "gotcha").strip().lower()
    if kind not in _LIFECYCLE_KINDS and kind != "evidence":
        raise KaizenDenied(
            "DENIED_DEDUP_KIND",
            {"kinds": [*_LIFECYCLE_KINDS, "evidence"], "required_action": "pass --kind gotcha|learned|learning|evidence"},
            exit_code=2,
        )
    limit = int(getattr(args, "limit", None) or 200)
    threshold = float(getattr(args, "threshold", None) or 0.85)  # cosine SIMILARITY threshold

    backend = get_embedding_backend()
    vectors: list[list[float]] | None = None
    vector_source = "embedded"

    if kind == "evidence":
        from .db import get_active_embedding_model

        # Offline-first: cluster over the stored chunk_embeddings index (Turso vector32) -- no backend,
        # no network. Cluster ONE model's vectors (cross-model vectors are incomparable): the active
        # model, or the most-indexed model when none is active. Fall back to embedding from text only
        # when no model has any stored vectors.
        active_model = get_active_embedding_model()
        if not active_model:
            dominant = fetch_all(
                "SELECT embedding_model FROM chunk_embeddings GROUP BY embedding_model "
                "ORDER BY COUNT(*) DESC, embedding_model LIMIT 1"
            )
            active_model = dominant[0][0] if dominant else None
        rows = fetch_all(
            "SELECT c.id, substr(c.text, 1, 240), vector_extract(e.embedding) FROM chunk_embeddings e "
            "JOIN evidence_chunks c ON c.id = e.chunk_id "
            "WHERE e.embedding_model = ? ORDER BY e.created_at DESC LIMIT ?",
            (active_model, limit),
        ) if active_model else []
        if rows:
            ids = [r[0] for r in rows]
            texts = [r[1] or "" for r in rows]
            vectors = [json.loads(r[2]) for r in rows]
            vector_source = "stored-embeddings"
            ids, texts, vectors = _same_dim(ids, texts, vectors)
        else:
            if backend is None:
                raise KaizenDenied(
                    "DENIED_BACKEND_UNCONFIGURED",
                    {"required_action": "no stored evidence embeddings -- run B3 reembed, or set "
                                        "KAIZEN_EMBED_MODEL / KAIZEN_EMBED_BACKEND=sentence-transformers"},
                    exit_code=2,
                )
            rows = fetch_all("SELECT id, substr(text, 1, 240) FROM evidence_chunks ORDER BY created_at DESC LIMIT ?", (limit,))
            ids = [r[0] for r in rows]
            texts = [r[1] or "" for r in rows]
    else:
        if backend is None:
            raise KaizenDenied(
                "DENIED_BACKEND_UNCONFIGURED",
                {"required_action": "set KAIZEN_EMBED_MODEL (or KAIZEN_EMBED_BACKEND=sentence-transformers) for O5 dedup"},
                exit_code=2,
            )
        rows = fetch_all(_LIFECYCLE_KINDS[kind], (limit,))
        ids = [r[0] for r in rows]
        texts = [f"{r[1]} {r[2]}".strip() for r in rows]

    if len(ids) < 2:
        return {"status": "OK", "kind": kind, "records": len(ids), "clusters": 0,
                "source": vector_source, "note": "fewer than 2 records; nothing to cluster."}

    if vectors is None:
        # freshly embed (the stored-evidence path skips this, so no model call to trace there)
        vectors = embed_batched(
            backend, texts, record_trace=True,
            task_id=getattr(args, "task_id", None), is_test=1 if getattr(args, "test", False) else 0,
        )  # raises DENIED if the backend is unreachable
    clusters = [c for c in _single_link_clusters(vectors, threshold) if len(c) > 1]

    out_dir = EXPORT_ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"dedup-{kind}.md"
    lines = [
        f"# Near-duplicate {kind} records",
        "",
        f"Records: {len(ids)}  |  Near-duplicate clusters: {len(clusters)}  |  cosine >= {threshold}  |  vectors: {vector_source}",
        "",
        "Human triage only -- O5 never merges or mutates records.",
        "",
    ]
    for cluster in clusters:
        lines.append(f"## Cluster of {len(cluster)}")
        for i in cluster:
            lines.append(f"- `{ids[i]}` {texts[i][:120]}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    record_id = new_id("rp")
    created = now()
    summary = f"Near-duplicate {kind}: {len(clusters)} cluster(s) over {len(ids)} records (human triage)."
    content_hash = utc_text_hash({"id": record_id, "kind": kind, "clusters": len(clusters), "records": len(ids)})

    is_test = 1 if getattr(args, "test", False) else 0

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO reports (id, created_at, report_type, scope, path, summary, content_hash, is_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (record_id, created, "dedup", kind, repo_relative(path), summary, content_hash, is_test),
        )

    write_tx(op)
    return {
        "status": "OK",
        "kind": kind,
        "records": len(ids),
        "clusters": len(clusters),
        "source": vector_source,
        "path": repo_relative(path),
        "sha256": file_sha256(path),
        "top_clusters": [[ids[i] for i in c] for c in clusters[:5]],
        "note": "human triage only; O5 never merges or mutates records.",
    }
