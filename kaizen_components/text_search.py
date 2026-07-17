"""Shared lexical-search helper.

Every record/report/evidence text query is a substring `LIKE` search. Without an escape
character, a literal ``%`` or ``_`` in the user's query is treated as a wildcard, so the
match silently becomes over-broad and non-deterministic. `like_pattern` centralizes the
escaping; callers pair it with ``LIKE ? ESCAPE '\\'`` on every ``LIKE`` clause so the
behavior is identical across the whole CLI.

Note on scaling: ``LIKE '%q%'`` has a leading wildcard and cannot use a B-tree index, so
these queries scan (bounded by each query's ``LIMIT``). That is fine at per-project record
scale. A full-text path is deferred until Turso's native FTS graduates from experimental
(see the README database section); this helper is the single seam it would plug into.
"""

from __future__ import annotations

def like_pattern(query: str) -> str:
    """Return a ``%``-wrapped LIKE pattern with ``\\``, ``%`` and ``_`` escaped literally.

    Pair with ``LIKE ? ESCAPE '\\'`` in the SQL (escape ``\\`` first so it does not
    double-escape the wildcards added after it).
    """
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
