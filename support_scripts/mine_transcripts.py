#!/usr/bin/env python3
"""Mine coding-agent session transcripts for GOTCHA candidates.

Reads Claude Code session transcripts (JSONL under ``~/.claude/projects/<slug>/``),
extracts recurring friction signals — failed tool calls and user corrections —
groups them by normalized signature, and drafts GOTCHA candidates for HUMAN review.

The script never writes to the Kaizen DB: it emits a markdown + JSON draft under
gitignored ``AI/work/transcript-mining/`` and suggests the ``kaizen.py G1`` commands.
A human (or an agent with the user's go-ahead) promotes drafts through the normal
write gate, which also enforces redaction. Standard library only; read-only on the
transcript directory.

Usage:
    python support_scripts/mine_transcripts.py                     # auto-detect this repo's transcripts
    python support_scripts/mine_transcripts.py --dir <path>        # explicit transcript dir (repeatable)
    python support_scripts/mine_transcripts.py --min-count 3 --limit 10 --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "AI" / "work" / "transcript-mining"

# Phrases (lowercased) that usually open a user correction of agent behavior.
CORRECTION_CUES = (
    "no,",
    "no ",
    "don't",
    "do not",
    "stop",
    "that's wrong",
    "that is wrong",
    "not what i",
    "instead",
    "actually,",
    "why did you",
    "undo",
    "revert",
    "you missed",
    "you forgot",
    "should not have",
    "shouldn't have",
)

_HOME_WIN = re.compile(r"(?i)[a-z]:[\\/]users[\\/][^\\/\s\"']+")
_HOME_POSIX = re.compile(r"(?<![A-Za-z0-9._/])/(?:home|Users)/[^/\s\"']+")
_HEXISH = re.compile(r"\b[0-9a-f]{8,}\b")
_NUMBERS = re.compile(r"\d+")


def project_slug(path: Path) -> str:
    """Claude Code's project-directory slug: lowercase, non-alphanumerics -> '-'."""
    return re.sub(r"[^a-z0-9]", "-", str(path).lower())


def default_transcript_dir() -> Path | None:
    candidate = Path.home() / ".claude" / "projects" / project_slug(REPO_ROOT)
    return candidate if candidate.is_dir() else None


def scrub(text: str) -> str:
    """Drop personal paths so drafts start closer to redaction-gate clean."""
    text = _HOME_WIN.sub("<home>", text)
    return _HOME_POSIX.sub("<home>", text)


def signature(text: str) -> str:
    """Normalize an error/correction line into a groupable signature."""
    first = scrub(text).strip().splitlines()[0] if text.strip() else ""
    first = _HEXISH.sub("<hex>", first)
    first = _NUMBERS.sub("#", first)
    return " ".join(first.split())[:160]


def _text_of(content) -> str:
    """Extract plain text from a message content value (string or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _walk_tool_errors(node, out: list[str]) -> None:
    """Collect error text from any nested tool_result dict with is_error true."""
    if isinstance(node, dict):
        if node.get("is_error") is True:
            text = _text_of(node.get("content"))
            if text.strip():
                out.append(text)
        for value in node.values():
            _walk_tool_errors(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_tool_errors(item, out)


def mine_file(path: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (tool_errors, corrections) as (signature, evidence 'file:line') pairs."""
    errors: list[tuple[str, str]] = []
    corrections: list[tuple[str, str]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for lineno, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            found: list[str] = []
            _walk_tool_errors(obj, found)
            for text in found:
                sig = signature(text)
                if sig:
                    errors.append((sig, f"{path.name}:{lineno}"))
            if obj.get("type") == "user":
                message = obj.get("message") or {}
                text = _text_of(message.get("content")) if isinstance(message, dict) else ""
                head = text.strip().lower()[:200]
                if head and any(cue in head for cue in CORRECTION_CUES):
                    sig = signature(text)
                    if sig:
                        corrections.append((sig, f"{path.name}:{lineno}"))
    return errors, corrections


def group(pairs: list[tuple[str, str]], min_count: int) -> list[dict]:
    counts: Counter[str] = Counter(sig for sig, _ev in pairs)
    evidence: dict[str, list[str]] = defaultdict(list)
    for sig, ev in pairs:
        if len(evidence[sig]) < 5:
            evidence[sig].append(ev)
    return [
        {"signature": sig, "count": n, "evidence": evidence[sig]}
        for sig, n in counts.most_common()
        if n >= min_count
    ]


def draft_g1(kind: str, candidate: dict) -> str:
    title = f"{kind}: {candidate['signature'][:70]}"
    summary = f"Recurred {candidate['count']}x across recent sessions; drafted by mine_transcripts.py."
    return (
        f'python kaizen.py G1 --title "{title}" --summary "{summary}" '
        f'--body-file <write the evidence + suspected cause to a file first> --json'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mine_transcripts.py",
        description="Mine agent session transcripts for GOTCHA candidates (drafts only; never writes the DB).",
    )
    parser.add_argument("--dir", action="append", help="transcript directory; repeatable (default: this repo's Claude Code project dir)")
    parser.add_argument("--min-count", type=int, default=2, help="minimum occurrences for a tool-error candidate (default 2)")
    parser.add_argument("--limit", type=int, default=20, help="max candidates per category (default 20)")
    parser.add_argument("--include-subagents", action="store_true", help="also scan subagent transcript subdirectories")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    args = parser.parse_args(argv)

    dirs = [Path(d) for d in (args.dir or [])]
    if not dirs:
        detected = default_transcript_dir()
        if detected is None:
            print("ERROR: no --dir given and no default transcript directory found", file=sys.stderr)
            return 2
        dirs = [detected]

    files: list[Path] = []
    for base in dirs:
        if not base.is_dir():
            print(f"ERROR: not a directory: {base}", file=sys.stderr)
            return 2
        files.extend(sorted(base.glob("*.jsonl")))
        if args.include_subagents:
            files.extend(sorted(base.glob("subagents/**/*.jsonl")))

    all_errors: list[tuple[str, str]] = []
    all_corrections: list[tuple[str, str]] = []
    for path in files:
        errors, corrections = mine_file(path)
        all_errors.extend(errors)
        all_corrections.extend(corrections)

    error_candidates = group(all_errors, args.min_count)[: args.limit]
    correction_candidates = group(all_corrections, 1)[: args.limit]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scanned_files": len(files),
        "tool_error_events": len(all_errors),
        "correction_events": len(all_corrections),
        "tool_error_candidates": error_candidates,
        "correction_candidates": correction_candidates,
        "next_step": "review each candidate; promote real pitfalls with kaizen.py G1 (the write gate enforces redaction)",
    }
    json_path = OUT_DIR / f"gotcha-candidates-{stamp}.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# GOTCHA candidates (drafts for human review)",
        "",
        f"Generated: {report['generated_at']}  |  Files scanned: {len(files)}  |  "
        f"Tool errors: {len(all_errors)}  |  Corrections: {len(all_corrections)}",
        "",
        "Nothing below has been written to the Kaizen DB. Promote a candidate only after",
        "confirming it is a real, recurring pitfall (not a one-off or already fixed).",
        "",
        "## Recurring tool errors",
        "",
    ]
    for c in error_candidates:
        lines.append(f"- **{c['count']}x** `{c['signature']}`")
        lines.append(f"  - evidence: {', '.join(c['evidence'])}")
        lines.append(f"  - draft: `{draft_g1('Tool failure', c)}`")
    if not error_candidates:
        lines.append("- none above threshold")
    lines.extend(["", "## User corrections", ""])
    for c in correction_candidates:
        lines.append(f"- **{c['count']}x** `{c['signature']}`")
        lines.append(f"  - evidence: {', '.join(c['evidence'])}")
        lines.append(f"  - draft: `{draft_g1('User correction', c)}`")
    if not correction_candidates:
        lines.append("- none found")
    md_path = OUT_DIR / f"gotcha-candidates-{stamp}.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps({"status": "OK", "markdown": str(md_path), "json": str(json_path), **{k: report[k] for k in ("scanned_files", "tool_error_events", "correction_events")}, "tool_error_candidates": len(error_candidates), "correction_candidates": len(correction_candidates)}, indent=2))
    else:
        print(f"OK: {len(error_candidates)} tool-error + {len(correction_candidates)} correction candidates")
        print(f"markdown: {md_path}")
        print(f"json:     {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
