"""B7 embed-index: manage the per-model embedding indexes in ``chunk_embeddings`` (additive; SCHEMA_VERSION stays 1).

An embedder upgrade is a rolling, reversible re-index rather than a blocking full re-vector:
``B3 --model NEW`` pre-stages NEW's index while the active one keeps serving E4, then ``B7 --activate
NEW`` flips retrieval to it (old index retained for rollback), and retention prunes stale models.

- ``--list`` (default): per-model coverage {model, chunks_indexed, dim, approx_bytes, is_active}.
- ``--activate --model X``: make X the retrieval model (denies unless X fully indexes the corpus),
  then prune per retention (``KAIZEN_EMBED_KEEP``, default 2 = active + previous).
- ``--prune [--previous | --all-but-active | --model X]``: drop a model's index; never the active one.

Embeddings are a rebuildable index over ``evidence_chunks.text`` (the source of truth), so pruning
frees storage without risking data -- ``B3 --model X`` rebuilds any dropped index.
"""

from __future__ import annotations

import os
from typing import Any

from .db import (
    fetch_all,
    get_active_embedding_model,
    now,
    write_tx,
)
from .denials import KaizenDenied


def keep_window() -> int:
    """Number of models to retain (active + previous by default). 0 / negative = keep-all (no prune)."""
    try:
        return int(os.environ.get("KAIZEN_EMBED_KEEP", "2"))
    except ValueError:
        return 2


def list_models() -> list[dict[str, Any]]:
    """Per-model index coverage, most-recently-built first. approx_bytes = SUM(dim) * 4 (float32)."""
    rows = fetch_all(
        "SELECT embedding_model, COUNT(*), MIN(dim), MAX(dim), SUM(dim), MAX(created_at) "
        "FROM chunk_embeddings GROUP BY embedding_model ORDER BY MAX(created_at) DESC, embedding_model"
    )
    active = get_active_embedding_model()
    return [
        {
            "model": model,
            "chunks_indexed": int(count),
            "dim": int(max_dim or 0),
            "dimension_consistent": min_dim == max_dim,
            "approx_bytes": int((sum_dim or 0) * 4),
            "created_at": latest,
            "is_active": model == active,
        }
        for model, count, min_dim, max_dim, sum_dim, latest in rows
    ]


def _delete_models(models: list[str]) -> None:
    if not models:
        return

    def op(conn: Any, _attempt: int) -> None:
        for model in models:
            conn.execute("DELETE FROM chunk_embeddings WHERE embedding_model = ?", (model,))

    write_tx(op)


def prune_to_keep(active_model: str, keep: int) -> list[str]:
    """Retain the newest ``keep`` models (the active one always kept); delete the rest. Returns the
    dropped models. ``keep`` <= 0 (keep-all) is a no-op."""
    if keep <= 0:
        return []
    names = [m["model"] for m in list_models()]  # most-recent first
    kept = [active_model] + [n for n in names if n != active_model][: max(0, keep - 1)]
    drop = [n for n in names if n not in kept]
    _delete_models(drop)
    return drop


def activate_model(model: str, *, keep_all: bool = False) -> dict[str, Any]:
    """Point retrieval (E4/O5) at ``model``'s index, then prune per retention (unless ``keep_all``).
    Denies unless ``model`` indexes EVERY chunk -- a partial index would silently drop chunks from
    retrieval."""
    def activate(conn: Any, _attempt: int) -> tuple[int, int]:
        total = int(conn.execute("SELECT COUNT(*) FROM evidence_chunks").fetchone()[0])
        indexed = int(
            conn.execute(
                "SELECT COUNT(*) FROM chunk_embeddings WHERE embedding_model = ?", (model,)
            ).fetchone()[0]
        )
        if total and indexed == 0:
            raise KaizenDenied(
                "DENIED_EMBED_INDEX_ABSENT",
                {"model": model, "required_action": f"build the index first: python kaizen.py B3 --model {model}"},
                exit_code=2,
            )
        if indexed < total:
            raise KaizenDenied(
                "DENIED_EMBED_INDEX_INCOMPLETE",
                {
                    "model": model,
                    "indexed_chunks": indexed,
                    "total_chunks": total,
                    "required_action": f"finish indexing before activating: python kaizen.py B3 --model {model}",
                },
                exit_code=2,
            )
        conn.execute(
            "INSERT OR REPLACE INTO db_settings (key, value, updated_at) VALUES ('active_embedding_model', ?, ?)",
            (model, now()),
        )
        return total, indexed

    total, indexed = write_tx(activate)
    if total == 0:
        # Empty corpus: full coverage is vacuous, so record the model as active (a preference the next
        # ingest/B3 will honor) rather than denying. Nothing to prune yet.
        return {"active_embedding_model": model, "indexed_chunks": 0, "total_chunks": 0, "pruned": []}
    pruned = prune_to_keep(model, 0 if keep_all else keep_window())
    return {"active_embedding_model": model, "indexed_chunks": indexed, "total_chunks": total, "pruned": pruned}


def _prune(args: Any) -> dict[str, Any]:
    """Non-trivial target-selection: precedence (--previous -> single most-recent non-active; --all_but_active -> all non-active; --model -> that model, denies DENIED_PRUNE_ACTIVE if it is active; else DENIED_PRUNE_TARGET) and that the active index is never prunable (L140). One line would aid the reader; borderline given density standard."""
    active = get_active_embedding_model()
    names = [m["model"] for m in list_models()]  # most-recent first
    non_active = [n for n in names if n != active]
    if getattr(args, "previous", False):
        targets = non_active[:1]  # the single most-recent non-active model
    elif getattr(args, "all_but_active", False):
        targets = list(non_active)
    elif getattr(args, "model", None):
        model = args.model
        if model == active:
            raise KaizenDenied(
                "DENIED_PRUNE_ACTIVE",
                {"model": model, "required_action": "cannot prune the active model; B7 --activate another model first"},
                exit_code=2,
            )
        targets = [model]
    else:
        raise KaizenDenied(
            "DENIED_PRUNE_TARGET",
            {"required_action": "pass one of --previous, --all-but-active, or --model <id>"},
            exit_code=2,
        )
    targets = [t for t in targets if t in names and t != active]  # report only rows actually pruned
    _delete_models(targets)
    remaining = [n for n in names if n not in targets]
    return {"pruned": targets, "active_embedding_model": active, "remaining": remaining}


def embed_index(args: Any) -> dict[str, Any]:
    """B7 dispatch: --activate / --prune / --list (default)."""
    if getattr(args, "activate", False):
        model = getattr(args, "model", None)
        if not model:
            raise KaizenDenied(
                "DENIED_MODEL_REQUIRED",
                {"required_action": "pass --model <id> with --activate (the model to make active)"},
                exit_code=2,
            )
        return {"status": "OK", **activate_model(model, keep_all=bool(getattr(args, "keep_all", False)))}
    if getattr(args, "prune", False):
        return {"status": "OK", **_prune(args)}
    return {
        "status": "OK",
        "active_embedding_model": get_active_embedding_model(),
        "keep": keep_window(),
        "models": list_models(),
        "note": "embeddings are a rebuildable index over evidence_chunks.text; B3 --model <id> (re)builds one.",
    }
