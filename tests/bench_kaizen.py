#!/usr/bin/env python3
"""Repeatable local benchmarks for the Agent Kaizen harness.

Times the real CLI code path in-process (``kaizen_components.args.main``), so results
exclude Python interpreter startup but include argparse, schema checks, fresh DB
connections, and commits — the cost an agent actually pays per operation.

Four phases, all against an isolated ``KAIZEN_REPO_ROOT`` temp root (the real
``AI/db/`` is never read or written; no network):

1. Write latency: W1, G1, Q2, X1, T2 — median/p95 over N iterations on a fresh DB.
2. Read-back at scale: R0 (session digest) and X5 (policy context) timed against
   seeded databases of increasing record counts.
3. Evidence retrieval: E1 ingest -> E3 chunk -> E4 lexical query over a deterministic
   synthetic corpus, with a top-k hit-rate correctness gate and a negative control.
4. Context recovery: the R0 digest payload versus the text content of this project's
   median local agent session transcript (aggregate sizes only — content never leaves
   the machine, and the phase skips gracefully when no transcripts exist).

Outputs: ``docs/benchmarks.json`` + ``docs/BENCHMARKS.md`` (fully regenerated), SVG
charts under ``docs/images/`` (render everywhere, including previews that cannot draw
Mermaid), and the ``## Benchmarks`` + ``## Benchmarks Preview`` sections in ``README.md``
(plus ``readme-section.md`` and ``teaser-section.md`` when ``--out`` is used)
(located by heading and replaced up to the next heading). Only derived numbers and coarse
machine info are written —
never op payloads, hostnames, or personal paths.

Usage:
    python tests/bench_kaizen.py                 # full run, updates docs/ + README
    python tests/bench_kaizen.py --quick         # tiny run, writes to a temp dir
    python tests/bench_kaizen.py --out DIR       # redirect all artifacts (dry run)
    python tests/bench_kaizen.py --from-json P   # re-render artifacts, no measuring
    python tests/bench_kaizen.py --allow-semantic  # keep embed env vars; time E4 --semantic
"""

from __future__ import annotations

import argparse
import gc
import io
import json
import math
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from support_scripts.mine_transcripts import default_transcript_dir  # noqa: E402

BENCH_WORK_ROOT = REPO_ROOT / "AI" / "work" / "test-bench"

TEASER_HEADING = "## Benchmarks Preview"
BENCHMARKS_HEADING = "## Benchmarks"

BACKEND_ENV_VARS = ("KAIZEN_EMBED_MODEL", "KAIZEN_EMBED_BACKEND", "KAIZEN_LLM_MODEL", "KAIZEN_TURSO_FTS")

# Deliberately bland corpus vocabulary: nothing email-, path-, or secret-shaped, so the
# redaction gate never has anything to find in synthetic text.
WORDS = (
    "harness ledger record verify scope adapt improve manage digest chunk corpus signal "
    "budget retry deterministic evidence artifact schema policy lesson gotcha promote "
    "review packet trace score report source lock backup manifest export cycle window "
    "buffer index query filter branch merge draft final stable robust simple clear direct "
    "local durable bounded gate lineage"
).split()

# Filled in by _bind_engine() after KAIZEN_REPO_ROOT is set; the engine binds its data
# plane at import time, so importing any earlier would target the real repo.
_KAIZEN_MAIN = None
_ALIASES: dict[str, list[str]] = {}
_TOOL_VERSION = "unknown"


def _bind_engine() -> None:
    """Imports engine AFTER KAIZEN_REPO_ROOT set; mutates 3 globals; must run exactly once (no re-bind guard — F1)."""
    global _KAIZEN_MAIN, _ALIASES, _TOOL_VERSION
    if _KAIZEN_MAIN is not None:
        raise RuntimeError("benchmark engine is already bound")
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from kaizen_components import TOOL_VERSION
    from kaizen_components.args import ALIASES, main

    _KAIZEN_MAIN = main
    _ALIASES = ALIASES
    _TOOL_VERSION = TOOL_VERSION


def alias_of(code: str) -> str:
    """Human alias for op code, else the code."""
    names = _ALIASES.get(code, [code])
    return names[1] if len(names) > 1 else code


def call_op(*argv: str) -> tuple[int, dict]:
    """Run op in-process with --json; capture stdout/stderr; return (rc, payload); stderr-fallback + JSON-decode-fallback semantics (F2)."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = _KAIZEN_MAIN([*argv, "--json"])
    except SystemExit as exc:  # argparse parse failure; ops themselves return codes
        rc = int(exc.code or 0)
    stdout = out.getvalue().strip()
    stderr = err.getvalue().strip()
    raw = stdout or (stderr if rc != 0 else "{}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {"unparsed": raw[:200]}
    return rc, payload


def must(*argv: str) -> dict:
    """Run-or-SystemExit(1); fail-closed on rc!=0."""
    rc, payload = call_op(*argv)
    if rc != 0:
        print(f"bench: required op failed rc={rc}: {' '.join(argv)} -> {payload}", file=sys.stderr)
        raise SystemExit(1)
    return payload


def timed(*argv: str) -> tuple[float, dict]:
    """As must + perf_counter ms; fail-closed on rc!=0."""
    start = time.perf_counter()
    rc, payload = call_op(*argv)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    if rc != 0:
        print(f"bench: timed op failed rc={rc}: {' '.join(argv)} -> {payload}", file=sys.stderr)
        raise SystemExit(1)
    return elapsed_ms, payload


def stats(samples: list[float]) -> dict:
    """Nearest-rank p95 (≈max at small n, F3); assumes non-empty samples."""
    ordered = sorted(samples)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    return {
        "n": len(ordered),
        "median_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(p95, 2),
        "min_ms": round(ordered[0], 2),
        "max_ms": round(ordered[-1], 2),
    }


def log(message: str) -> None:
    print(f"bench: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Op builders


def write_op_argv(op: str, i: int) -> list[str]:
    """Build argv for op/index; ValueError on unknown op."""
    if op == "W1":
        return ["W1", "--title", f"Bench task {i}", "--summary", "Benchmark task record.",
                "--body", "Deterministic benchmark body."]
    if op == "G1":
        return ["G1", "--title", f"Bench gotcha {i}", "--summary", "Benchmark gotcha record.",
                "--body", "Deterministic benchmark body."]
    if op == "Q2":
        return ["Q2", "--conclusion", "VERIFIED_ACCEPTABLE", "--summary", "Benchmark verification event."]
    if op == "X1":
        return ["X1", "--title", f"Bench policy {i}", "--summary", "Benchmark policy record.",
                "--body", "Deterministic policy body.", "--priority", "normal"]
    if op == "T2":
        payload = {"name": "bench-score", "value": 0.5, "data_type": "numeric", "source": "deterministic"}
        return ["T2", "--payload-json", json.dumps(payload)]
    raise ValueError(op)


# Seed mix per 10 ops: every session_digest() cost center grows linearly — active
# tasks + gotchas (each also auto-writes a ledger event, so the drift NOT-IN subquery
# does real work), blocking verifications, priority-ordered policies, eval scores.
SEED_PATTERN = ("W1", "W1", "W1", "G1", "G1", "G1", "Q2F", "Q2F", "X1", "T2")


def seed_op_argv(i: int) -> list[str]:
    """SEED_PATTERN dispatch incl. Q2F failed-verification variant."""
    op = SEED_PATTERN[i % len(SEED_PATTERN)]
    if op == "Q2F":
        return ["Q2", "--conclusion", "VERIFICATION_FAILED", "--summary", "Benchmark verification event."]
    return write_op_argv(op, i)


# ---------------------------------------------------------------------------
# Phases


def reset_db(bench_root: Path) -> None:
    """Delete kaizen.db* with Windows-handle retries, fail on leftovers, then run K1."""
    db_dir = bench_root / "AI" / "db"
    for _attempt in range(3):
        leftovers = list(db_dir.glob("kaizen.db*")) if db_dir.exists() else []
        if not leftovers:
            break
        try:
            for path in leftovers:
                path.unlink()
        except PermissionError:  # Windows: a just-closed handle can linger briefly
            gc.collect()
            time.sleep(0.2)
    leftovers = list(db_dir.glob("kaizen.db*")) if db_dir.exists() else []
    if leftovers:
        raise RuntimeError(f"benchmark database cleanup failed: {', '.join(str(path) for path in leftovers)}")
    must("K1")


def phase_write_latency(warmup: int, iterations: int) -> dict:
    """Measure each write operation sequentially on one growing DB and return per-op statistics."""
    results: dict[str, dict] = {}
    for op in ("W1", "G1", "Q2", "X1", "T2"):
        for i in range(warmup):
            must(*write_op_argv(op, i))
        samples = [timed(*write_op_argv(op, warmup + i))[0] for i in range(iterations)]
        results[op] = {"alias": alias_of(op), **stats(samples)}
        log(f"write latency {op} median {results[op]['median_ms']} ms")
    return results


def phase_digest_at_scale(levels: list[int], warmup: int, iterations: int) -> list[dict]:
    """One-line intent + return shape (per-scale rows incl. counts/r0_payload_bytes)."""
    rows = []
    seeded = 0
    for level in levels:
        while seeded < level:
            must(*seed_op_argv(seeded))
            seeded += 1
        log(f"seeded {seeded} records")
        entry: dict = {"scale": level}
        for op in ("R0", "X5"):
            for _ in range(warmup):
                must(op)
            samples = []
            payload: dict = {}
            for _ in range(iterations):
                elapsed_ms, payload = timed(op)
                samples.append(elapsed_ms)
            entry[op] = stats(samples)
            if op == "R0":
                entry["counts"] = payload.get("counts", {})
                entry["r0_payload_bytes"] = len(json.dumps(payload).encode("utf-8"))
        rows.append(entry)
        log(f"digest at {level}: R0 median {entry['R0']['median_ms']} ms, X5 median {entry['X5']['median_ms']} ms")
    return rows


def build_corpus(bench_root: Path, docs: int, paragraphs: int) -> list[Path]:
    """Seeded-RNG deterministic md docs, one unique kzbench-marker per doc; returns paths."""
    import random

    corpus_dir = bench_root / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(docs):
        rng = random.Random(20260703 + i)
        paras = []
        for _p in range(paragraphs):
            words = [rng.choice(WORDS) for _ in range(rng.randint(40, 70))]
            paras.append(" ".join(words) + ".")
        paras[paragraphs // 2] = f"The keystone identifier for this document is kzbench-marker-{i:04d}."
        path = corpus_dir / f"doc_{i:04d}.md"
        path.write_text(f"# Benchmark corpus document {i}\n\n" + "\n\n".join(paras) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def phase_retrieval(bench_root: Path, docs: int, paragraphs: int, repeats: int,
                    allow_semantic: bool) -> dict:
    """One-line intent + return shape (E1/E3/E4 stats, hit_rate, semantic)."""
    if docs <= 0:
        raise ValueError("retrieval benchmark requires corpus_docs > 0")
    build_corpus(bench_root, docs, paragraphs)
    e1_samples, e3_samples, e4_samples = [], [], []
    doc_ids: dict[int, str] = {}
    chunks_total = 0
    for i in range(docs):
        elapsed_ms, payload = timed("E1", "--path", f"corpus/doc_{i:04d}.md",
                                    "--summary", f"Benchmark corpus document {i}.")
        e1_samples.append(elapsed_ms)
        doc_ids[i] = payload["id"]
        elapsed_ms, payload = timed("E3", "--id", doc_ids[i])
        e3_samples.append(elapsed_ms)
        if isinstance(payload.get("chunk_count"), int):
            chunks_total += payload["chunk_count"]
    hits_top1 = hits_top5 = 0
    mode = "like"
    for i in range(docs):
        payload = {}
        for _ in range(repeats):
            elapsed_ms, payload = timed("E4", "--query", f"kzbench-marker-{i:04d}", "--limit", "5")
            e4_samples.append(elapsed_ms)
        mode = payload.get("mode", mode)
        found = [record.get("document_id") for record in payload.get("records", [])]
        if any(document_id is None for document_id in found):
            raise RuntimeError("retrieval response omitted document_id")
        hits_top1 += 1 if found[:1] == [doc_ids[i]] else 0
        hits_top5 += 1 if doc_ids[i] in found else 0
    negative = must("E4", "--query", "kzbench-marker-none", "--limit", "5")
    negative_clean = not negative.get("records")

    semantic: dict
    if not allow_semantic:
        semantic = {"skipped": True, "reason": "disabled"}
    else:
        rc, payload = call_op("E4", "--query", "kzbench-marker-0000", "--semantic", "--limit", "5")
    if allow_semantic and rc == 0 and payload.get("mode") == "semantic":
        sem_samples = []
        for i in range(docs):
            for _ in range(repeats):
                elapsed_ms, _payload = timed("E4", "--query", f"kzbench-marker-{i:04d}", "--semantic",
                                             "--limit", "5")
                sem_samples.append(elapsed_ms)
        semantic = {"skipped": False, "embedding_model": payload.get("embedding_model"), **stats(sem_samples)}
    elif allow_semantic:
        semantic = {"skipped": True, "reason": payload.get("code", "unavailable")}
    log(f"retrieval: hit@1 {hits_top1}/{docs}, hit@5 {hits_top5}/{docs}, mode {mode}")
    return {
        "mode": mode,
        "E1": {"alias": alias_of("E1"), **stats(e1_samples)},
        "E3": {"alias": alias_of("E3"), **stats(e3_samples), "chunks_total": chunks_total},
        "E4": {"alias": alias_of("E4"), **stats(e4_samples)},
        "hit_rate": {
            "top_1": round(hits_top1 / docs, 2),
            "top_5": round(hits_top5 / docs, 2),
            "queries": docs,
            "negative_control_clean": negative_clean,
        },
        "semantic": semantic,
    }


def _text_chars(node) -> int:
    """Total characters under any "text" key — the content an agent would re-read."""
    total = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "text" and isinstance(value, str):
                total += len(value)
            else:
                total += _text_chars(value)
    elif isinstance(node, list):
        for item in node:
            total += _text_chars(item)
    return total


def phase_context_recovery(digest_entries: list[dict]) -> dict:
    """Compare restoring context via R0 against replaying a raw session transcript.

    The baseline is measured from this project's own local agent session transcripts
    (same directory convention as mine_transcripts.py). Only aggregate sizes are
    recorded — no transcript content, names, or paths leave the machine.
    """
    entry = digest_entries[-1]
    r0_bytes = entry.get("r0_payload_bytes", 0)
    result: dict = {
        "r0_scale": entry["scale"],
        "r0_payload_bytes": r0_bytes,
        "r0_tokens_approx": max(1, round(r0_bytes / 4)),
        "transcripts": {"found": False},
    }
    transcript_dir = default_transcript_dir()
    if transcript_dir is None:
        log("context recovery: no local session transcripts found; baseline skipped")
        return result
    files = sorted(transcript_dir.glob("*.jsonl")) if transcript_dir.is_dir() else []
    if not files:
        log("context recovery: no local session transcripts found; baseline skipped")
        return result
    raw_sizes = sorted((path.stat().st_size, path) for path in files)
    median_size, median_path = raw_sizes[len(raw_sizes) // 2]  # Upper median when the count is even.
    chars = 0
    with open(median_path, encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                chars += _text_chars(json.loads(line))
            except json.JSONDecodeError:
                continue
    tokens = max(1, round(chars / 4))
    result["transcripts"] = {
        "found": True,
        "count": len(files),
        "median_raw_bytes": median_size,
        "median_session_text_chars": chars,
        "median_session_text_tokens_approx": tokens,
    }
    result["ratio_vs_transcript_text"] = max(1, round(tokens / result["r0_tokens_approx"]))
    log(f"context recovery: R0 ~{result['r0_tokens_approx']} tokens vs median transcript "
        f"~{tokens} tokens ({result['ratio_vs_transcript_text']}x)")
    return result


# ---------------------------------------------------------------------------
# Rendering


def machine_info() -> dict:
    """Return coarse, non-identifying runtime metadata recorded with benchmark results."""
    try:
        from importlib.metadata import version

        pyturso = version("pyturso")
    except Exception:
        pyturso = "unknown"
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "python": platform.python_version(),
        "pyturso": pyturso,
        "kaizen_tool_version": _TOOL_VERSION,
    }


def machine_line(machine: dict) -> str:
    """Format benchmark machine metadata as one human-readable line."""
    return (f"{machine['platform']}, {machine['machine']}, Python {machine['python']}, "
            f"pyturso {machine['pyturso']}")


def fmt_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a column-aligned Markdown table."""
    # Prettier-compatible: cells padded to column width, separator dashes fill the column.
    widths = [max(len(headers[c]), 3, *(len(row[c]) for row in rows)) for c in range(len(headers))]
    def line(cells: list[str]) -> str:
        return "| " + " | ".join(cell.ljust(widths[c]) for c, cell in enumerate(cells)) + " |"
    out = [line(headers), "| " + " | ".join("-" * w for w in widths) + " |"]
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def mermaid_xychart(title: str, x_labels: list[str], series: list[tuple[str, list[float]]],
                    y_label: str = "ms") -> str:
    """Render a Mermaid xychart for one or more numeric series."""
    y_max = max((value for _kind, values in series for value in values), default=0.0)
    y_top = max(1, math.ceil(y_max * 1.15))
    labels = ", ".join(f'"{label}"' for label in x_labels)
    lines = ["```mermaid", "xychart-beta", f'    title "{title}"', f"    x-axis [{labels}]",
             f'    y-axis "{y_label}" 0 --> {y_top}']
    for kind, values in series:
        lines.append(f"    {kind} [{', '.join(str(round(v, 1)) for v in values)}]")
    lines.append("```")
    return "\n".join(lines)


def write_ops_chart(results: dict, iterations: int) -> str:
    """Render the write-operation median-latency Mermaid chart."""
    ops = list(results["write_ops"])
    medians = [results["write_ops"][op]["median_ms"] for op in ops]
    return mermaid_xychart(f"Write-op latency, median ms (N={iterations}, local libSQL)", ops,
                           [("bar", medians)])


def _fmt_num(value: float) -> str:
    if value == 0:
        return "0"
    if value >= 100:
        return f"{value:,.0f}"
    if value >= 10:
        return f"{value:.0f}"
    return f"{value:.1f}"


def svg_bar_chart(title: str, labels: list[str], values: list[float], y_label: str) -> str:
    """A self-contained SVG bar chart (white background so it reads in dark mode too)."""
    if not values or len(labels) != len(values):
        raise ValueError("SVG bar chart requires one label per non-empty value")
    width, height = 760, 380
    ml, mr, mt, mb = 76, 24, 64, 56
    plot_w, plot_h = width - ml - mr, height - mt - mb
    y_top = max(values) * 1.15 or 1.0
    slot = plot_w / len(values)
    bar_w = slot * 0.6
    font = 'font-family="-apple-system, Segoe UI, Helvetica, Arial, sans-serif"'
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">',
        f'<rect width="{width}" height="{height}" rx="8" fill="#ffffff" stroke="#d0d7de"/>',
        f'<text x="{width / 2}" y="30" text-anchor="middle" {font} font-size="16" '
        f'font-weight="600" fill="#1f2328">{escape(title)}</text>',
        f'<text x="{ml}" y="{mt - 12}" {font} font-size="11" fill="#57606a">{escape(y_label)}</text>',
    ]
    for i in range(5):
        value = y_top * i / 4
        y = mt + plot_h - plot_h * i / 4
        parts.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{width - mr}" y2="{y:.1f}" '
                     f'stroke="#d0d7de" stroke-width="{1.2 if i == 0 else 0.6}"/>')
        parts.append(f'<text x="{ml - 8}" y="{y + 4:.1f}" text-anchor="end" {font} '
                     f'font-size="11" fill="#57606a">{_fmt_num(value)}</text>')
    for i, (label, value) in enumerate(zip(labels, values)):
        x = ml + i * slot + (slot - bar_w) / 2
        bar_h = plot_h * value / y_top
        y = mt + plot_h - bar_h
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
                     f'rx="3" fill="#2f81f7"/>')
        parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{max(y - 7, mt - 2):.1f}" '
                     f'text-anchor="middle" {font} font-size="12" font-weight="600" '
                     f'fill="#1f2328">{_fmt_num(value)}</text>')
        parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{mt + plot_h + 22}" text-anchor="middle" '
                     f'{font} font-size="12" fill="#1f2328">{escape(label)}</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def render_svg_charts(results: dict) -> dict[str, str]:
    """Return filename-to-content mappings for SVG artifacts embedded by the README."""
    ops = list(results["write_ops"])
    charts = {
        "bench-write-latency.svg": svg_bar_chart(
            f"Write-op latency, median ms (N={results['config']['write_iterations']}, local libSQL)",
            [f"{op} {results['write_ops'][op]['alias']}" for op in ops],
            [results["write_ops"][op]["median_ms"] for op in ops],
            "milliseconds",
        ),
    }
    ctx = results.get("context_recovery", {})
    if ctx.get("transcripts", {}).get("found"):
        charts["bench-context-recovery.svg"] = svg_bar_chart(
            "Restoring session context, approximate tokens (chars / 4)",
            [f"R0 digest ({ctx['r0_scale']:,} records)", "Replay median session transcript"],
            [ctx["r0_tokens_approx"], ctx["transcripts"]["median_session_text_tokens_approx"]],
            "approx tokens",
        )
    return charts


def render_benchmarks_md(results: dict) -> str:
    machine = results["machine"]
    config = results["config"]
    parts = [
        "<!-- Generated by tests/bench_kaizen.py; do not edit by hand. -->",
        "",
        "# Benchmarks",
        "",
        "Repeatable local benchmarks for the Agent Kaizen harness: the real CLI code path, timed in-process against an isolated scratch data plane. Anyone can regenerate this file with one command (see **Reproduce**); numbers below are the committed reference run.",
        "",
        f"Reference machine: {machine_line(machine)}. Generated {results['generated_at']} (UTC), mode `{results['mode']}`, total {results['total_seconds']}s.",
        "",
        "## Methodology",
        "",
        "- Every operation runs through `kaizen_components.args.main()` in-process, so timings exclude Python interpreter startup but include argument parsing, schema checks, fresh DB connections, and commits — the per-op cost an agent actually pays.",
        "- The whole run targets a throwaway `KAIZEN_REPO_ROOT` temp directory; the repo's own `AI/db/` is never read or written, and nothing touches the network.",
        f"- Percentiles are nearest-rank; the clock is `time.perf_counter`. Write ops run {config['warmup']} warmup + {config['write_iterations']} timed iterations each; read-back ops run {config['digest_iterations']} timed iterations per scale level.",
        "- The scale phase seeds a mix of tasks, gotchas, failed verifications, policies, and eval scores so every query in the R0 session digest does real work; the `learned` table stays empty (its digest query is a constant-cost scan).",
        "- Read-back latency can be non-monotonic across scale levels: the engine opens a fresh connection per query, and connection cost tracks the size of the MVCC write log, which the storage layer compacts on internal thresholds. A level measured right after a seeding burst (log large, not yet compacted) can read slower than a larger level measured after a compaction. Row count is not the whole story; log state matters too.",
        "- Retrieval hit rate is a correctness gate, not a ranking measure: lexical mode orders by recency, so it is meaningful only because each marker phrase is unique to one document (expected value: 1.0, plus a negative-control query that must return nothing).",
        "- Context recovery compares the `R0` digest payload against the text content of the median-sized local agent session transcript for this project. Token counts use the chars-divided-by-4 heuristic; only aggregate sizes are recorded, never content.",
        "",
        "## Limits",
        "",
        "- These numbers are **repository-local and illustrative**, not a portable benchmark: the context-recovery baseline is this project's own median session transcript, so the ratio reflects this repo's transcripts, not yours.",
        "- Token counts are a `chars / 4` approximation, not a real tokenizer count.",
        "- Retrieval hit rate is synthetic: each query targets a marker phrase unique to one seeded document, so it measures correctness (does the right doc come back), not ranking quality.",
        "- Latency is machine- and state-dependent (see the MVCC write-log note above); treat the reference run as a shape, not a guarantee.",
        "",
        "## Op Write Latency",
        "",
    ]
    write_rows = [[f"`{op}`", entry["alias"], str(entry["n"]), str(entry["median_ms"]),
                   str(entry["p95_ms"]), str(entry["min_ms"]), str(entry["max_ms"])]
                  for op, entry in results["write_ops"].items()]
    parts.append(fmt_table(["Op", "Alias", "n", "median ms", "p95 ms", "min ms", "max ms"], write_rows))
    parts += ["", write_ops_chart(results, config["write_iterations"]), ""]

    parts += ["## Session Digest At Scale", ""]
    scale_rows = [[str(entry["scale"]), str(entry["R0"]["median_ms"]), str(entry["R0"]["p95_ms"]),
                   str(entry["X5"]["median_ms"]), str(entry["X5"]["p95_ms"])]
                  for entry in results["digest_at_scale"]]
    parts.append(fmt_table(["Records seeded", "R0 median ms", "R0 p95 ms", "X5 median ms", "X5 p95 ms"],
                           scale_rows))
    scales = [str(entry["scale"]) for entry in results["digest_at_scale"]]
    r0_line = [entry["R0"]["median_ms"] for entry in results["digest_at_scale"]]
    x5_line = [entry["X5"]["median_ms"] for entry in results["digest_at_scale"]]
    parts += ["", mermaid_xychart("Read-back latency vs records seeded, median ms", scales,
                                  [("line", r0_line), ("line", x5_line)]),
              "", "Series order: `R0` session digest first, then `X5` policy context. If a mid-size level reads slower than a larger one, that may reflect the MVCC write-log effect described in **Methodology** — latency can follow log-compaction state as much as row count.", ""]

    retrieval = results["retrieval"]
    parts += ["## Evidence Retrieval", ""]
    retrieval_rows = [[f"`{op}`", retrieval[op]["alias"], str(retrieval[op]["n"]),
                       str(retrieval[op]["median_ms"]), str(retrieval[op]["p95_ms"])]
                      for op in ("E1", "E3", "E4")]
    parts.append(fmt_table(["Step", "Alias", "n", "median ms", "p95 ms"], retrieval_rows))
    hit = retrieval["hit_rate"]
    parts += [
        "",
        f"Corpus: {results['config']['corpus_docs']} documents, "
        f"{results['config']['paragraphs_per_doc']} paragraphs each, "
        f"{retrieval['E3']['chunks_total']} chunks total. Mode: `{retrieval['mode']}`.",
        "",
        f"Hit rate over {hit['queries']} marker queries: top-1 {hit['top_1']}, top-5 {hit['top_5']}; negative control clean: {str(hit['negative_control_clean']).lower()}.",
        "",
    ]
    if retrieval["semantic"].get("skipped"):
        parts.append(f"Semantic mode: skipped ({retrieval['semantic'].get('reason')}) — no embedding backend configured, which is the dependency-light default. Configure one and pass `--allow-semantic` to time vector search too.")
    else:
        sem = retrieval["semantic"]
        parts.append(f"Semantic mode ({sem.get('embedding_model')}): median {sem['median_ms']} ms, p95 {sem['p95_ms']} ms over {sem['n']} queries.")
    ctx = results.get("context_recovery")
    if ctx:
        parts += ["", "## Context Recovery", "",
                  'What it costs to restore working context at session start: reading the `R0` session digest versus replaying a raw agent session transcript (the "just scroll up" baseline).', ""]
        transcripts = ctx.get("transcripts", {})
        if transcripts.get("found"):
            r0_tok = ctx["r0_tokens_approx"]
            tr_tok = transcripts["median_session_text_tokens_approx"]
            parts.append(fmt_table(
                ["Source", "Size", "Approx tokens (chars / 4)"],
                [[f"`R0` digest at {ctx['r0_scale']:,} seeded records",
                  f"{ctx['r0_payload_bytes']:,} bytes", f"{r0_tok:,}"],
                 [f"Median-sized session transcript, text content (of {transcripts['count']} sessions)",
                  f"{transcripts['median_session_text_chars']:,} chars", f"{tr_tok:,}"]]))
            parts += ["", mermaid_xychart("Restoring session context, approx tokens",
                                          ["R0 digest", "median transcript"],
                                          [("bar", [float(r0_tok), float(tr_tok)])],
                                          y_label="approx tokens"),
                      "",
                      f"Starting from records is about **{ctx['ratio_vs_transcript_text']:,}× cheaper** than replaying the median transcript — and the digest is curated state (active policy, open GOTCHAs, blocking verifications, lessons), not a wall of chat to re-read. The transcript baseline is measured from this project's own local agent session logs; only aggregate sizes are recorded, never content."]
        else:
            parts += ["No local agent session transcripts were found for this project, so the transcript-replay baseline was skipped in this run. The committed reference numbers come from the author's machine; regenerate on a machine with session history to measure your own baseline."]
    parts += [
        "",
        "## Reproduce",
        "",
        "```powershell",
        "python tests\\bench_kaizen.py",
        "```",
        "",
        "```sh",
        "python tests/bench_kaizen.py",
        "```",
        "",
        "`--quick` runs a tiny variant into a temp directory (used by the test suite so this script cannot rot); `--out DIR` redirects all artifacts for a dry run; `--allow-semantic` keeps your embedding-backend environment variables and times `E4 --semantic` when a backend responds.",
        "",
    ]
    return "\n".join(parts)


def render_readme_section(results: dict) -> str:
    """Render the complete README Benchmarks section."""
    lines = [
        "## Benchmarks",
        "",
        "Real numbers from the real code path: `tests/bench_kaizen.py` times the CLI in-process (interpreter startup excluded) against an isolated scratch data plane — your `AI/db/` is never touched. Full methodology, tables, and charts: [docs/BENCHMARKS.md](docs/BENCHMARKS.md).",
        "",
    ]
    ctx = results.get("context_recovery", {})
    if ctx.get("transcripts", {}).get("found"):
        ratio_sentence = (
            f"Session context restored from records is about "
            f"**{ctx['ratio_vs_transcript_text']:,}× cheaper** than replaying this repo's median "
            f"agent session transcript — and it is curated state, not a wall of chat."
        )
        lines += [
            "![Restoring session context: the R0 digest vs replaying a session transcript]"
            "(docs/images/bench-context-recovery.svg)",
            "",
            ratio_sentence,
            "",
        ]
    lines += [
        "![Write-op latency, median milliseconds](docs/images/bench-write-latency.svg)",
        "",
        f"Reference run: {machine_line(results['machine'])}. Regenerate with `python tests/bench_kaizen.py`.",
    ]
    return "\n".join(lines)


def render_teaser(results: dict) -> str:
    """Render the compact README Benchmarks Preview section."""
    if not results.get("write_ops") or not results.get("digest_at_scale"):
        raise ValueError("benchmark results require non-empty write_ops and digest_at_scale")
    write_max = max(entry["median_ms"] for entry in results["write_ops"].values())
    digest_max = max(entry["R0"]["median_ms"] for entry in results["digest_at_scale"]) / 1000.0
    ctx = results.get("context_recovery", {})
    sentence = (
        f"Proof is in the pudding: record writes land in under {math.ceil(write_max / 5) * 5} ms, a "
        f"full session-start digest reads back in ~{digest_max:.2f} s at "
        f"{results['digest_at_scale'][-1]['scale']:,} records, "
    )
    if ctx.get("transcripts", {}).get("found"):
        sentence += (
            f"and restoring context from records is ~{ctx['ratio_vs_transcript_text']:,}× cheaper "
            f"than replaying a session transcript "
        )
    sentence += "— measured, repeatable, on your machine: see [Benchmarks](#benchmarks)."
    return "\n".join(["## Benchmarks Preview", "", sentence])


def _is_section_boundary(line: str) -> bool:
    """True if the line starts a new H1/H2 section (the end of the current one). A deeper
    heading (`### ` and below) is content within the section, not a boundary."""
    return line.startswith("## ") or line.startswith("# ")


def replace_heading_section(readme_path: Path, heading: str, section: str) -> None:
    """Replace the Markdown section under an exact ``## Heading`` line with ``section``.

    The region runs from the heading line through the last non-blank line before the next
    H1/H2 heading; the blank line before that next heading is preserved, so spacing stays
    clean. Heading-delimited (no BEGIN/END marker comments). The heading is matched exactly
    (`## Benchmarks` must not hit `## Benchmarks Preview`). Raises if the heading is absent.
    """
    lines = readme_path.read_text(encoding="utf-8").splitlines()
    lo = next((i for i, ln in enumerate(lines) if ln.strip() == heading), None)
    if lo is None:
        print(f"bench: heading {heading!r} not found in {readme_path}; add it or rerun with --out",
              file=sys.stderr)
        raise SystemExit(1)
    last, j = lo, lo + 1
    while j < len(lines) and not _is_section_boundary(lines[j]):
        if lines[j].strip():
            last = j
        j += 1
    new_lines = lines[:lo] + section.splitlines() + lines[last + 1:]
    readme_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# Main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse benchmark measurement and artifact-rendering options."""
    parser = argparse.ArgumentParser(description="Benchmark the Agent Kaizen harness against an "
                                                 "isolated scratch data plane.")
    parser.add_argument("--quick", action="store_true",
                        help="tiny iteration counts; artifacts go to a temp dir, never the repo")
    parser.add_argument("--out", metavar="DIR",
                        help="write benchmarks.json, BENCHMARKS.md, and readme-section.md here "
                             "instead of updating docs/ and README.md")
    parser.add_argument("--allow-semantic", action="store_true",
                        help="keep embedding-backend env vars and time E4 --semantic if available")
    parser.add_argument("--keep-root", action="store_true",
                        help="keep the scratch KAIZEN_REPO_ROOT for inspection")
    parser.add_argument("--from-json", metavar="PATH",
                        help="skip measuring; re-render all artifacts from an existing results JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Orchestrates 4 phases → results → artifacts; note --out/--quick/--from-json branches and the TEMP-repoint-into-deleted-bench-root ordering caveat (718-719)."""
    args = parse_args(argv)
    BENCH_WORK_ROOT.mkdir(parents=True, exist_ok=True)
    if args.quick:
        config = {"write_iterations": 5, "warmup": 1, "scales": [50, 150], "digest_iterations": 5,
                  "corpus_docs": 3, "paragraphs_per_doc": 8, "query_repeats": 2, "top_k": 5}
    else:
        config = {"write_iterations": 50, "warmup": 3, "scales": [100, 1000, 5000],
                  "digest_iterations": 20, "corpus_docs": 20, "paragraphs_per_doc": 40,
                  "query_repeats": 5, "top_k": 5}

    # Resolve output targets BEFORE importing the engine: engine import re-points the
    # process TEMP dirs into the bench root, which is deleted at exit.
    if args.out:
        out_dir = Path(args.out).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
    elif args.quick:
        out_dir = Path(tempfile.mkdtemp(prefix="kaizen-bench-out-", dir=BENCH_WORK_ROOT))
    else:
        out_dir = None  # full run: docs/ + README in the repo

    if args.from_json:
        results = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        required = {"machine", "config", "write_ops", "digest_at_scale", "retrieval"}
        missing = sorted(required - results.keys()) if isinstance(results, dict) else sorted(required)
        if missing:
            raise ValueError(f"benchmark results JSON is missing required keys: {', '.join(missing)}")
        log(f"re-rendering from {args.from_json} (no measurement)")
    else:
        bench_root = Path(tempfile.mkdtemp(prefix="kaizen-bench-", dir=BENCH_WORK_ROOT))
        os.environ["KAIZEN_REPO_ROOT"] = str(bench_root)
        if not args.allow_semantic:
            for var in BACKEND_ENV_VARS:
                os.environ.pop(var, None)
        _bind_engine()

        started = time.perf_counter()
        try:
            log(f"scratch data plane: {bench_root}")
            must("K1")
            log("phase 1/4: write latency")
            write_ops = phase_write_latency(config["warmup"], config["write_iterations"])
            log("phase 2/4: session digest at scale")
            reset_db(bench_root)
            digest_at_scale = phase_digest_at_scale(config["scales"], min(config["warmup"], 2),
                                                    config["digest_iterations"])
            log("phase 3/4: evidence retrieval")
            reset_db(bench_root)
            retrieval = phase_retrieval(bench_root, config["corpus_docs"],
                                        config["paragraphs_per_doc"], config["query_repeats"],
                                        args.allow_semantic)
            log("phase 4/4: context recovery")
            context_recovery = phase_context_recovery(digest_at_scale)
        finally:
            if args.keep_root:
                log(f"keeping scratch root: {bench_root}")
            else:
                shutil.rmtree(bench_root, ignore_errors=True)

        results = {
            "schema": "kaizen-bench/v1",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": "quick" if args.quick else "full",
            "machine": machine_info(),
            "config": config,
            "write_ops": write_ops,
            "digest_at_scale": digest_at_scale,
            "retrieval": retrieval,
            "context_recovery": context_recovery,
            "total_seconds": round(time.perf_counter() - started, 1),
        }

    json_text = json.dumps(results, indent=2) + "\n"
    md_text = render_benchmarks_md(results).rstrip("\n") + "\n"
    section = render_readme_section(results)
    teaser = render_teaser(results)
    svg_charts = render_svg_charts(results)
    if out_dir is not None:
        (out_dir / "benchmarks.json").write_text(json_text, encoding="utf-8", newline="\n")
        (out_dir / "BENCHMARKS.md").write_text(md_text, encoding="utf-8", newline="\n")
        (out_dir / "readme-section.md").write_text(section + "\n", encoding="utf-8", newline="\n")
        (out_dir / "teaser-section.md").write_text(teaser + "\n", encoding="utf-8", newline="\n")
        for name, svg in svg_charts.items():
            (out_dir / name).write_text(svg, encoding="utf-8", newline="\n")
        for name in ("benchmarks.json", "BENCHMARKS.md", "readme-section.md", "teaser-section.md",
                     *svg_charts):
            print(out_dir / name)
    else:
        docs_dir = REPO_ROOT / "docs"
        images_dir = docs_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        (docs_dir / "benchmarks.json").write_text(json_text, encoding="utf-8", newline="\n")
        (docs_dir / "BENCHMARKS.md").write_text(md_text, encoding="utf-8", newline="\n")
        for name, svg in svg_charts.items():
            (images_dir / name).write_text(svg, encoding="utf-8", newline="\n")
        replace_heading_section(REPO_ROOT / "README.md", BENCHMARKS_HEADING, section)
        replace_heading_section(REPO_ROOT / "README.md", TEASER_HEADING, teaser)
        print(docs_dir / "benchmarks.json")
        print(docs_dir / "BENCHMARKS.md")
        for name in svg_charts:
            print(images_dir / name)
        print(REPO_ROOT / "README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
