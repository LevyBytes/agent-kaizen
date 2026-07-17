"""Q10 contract-lint: deterministic density check for agent-authored contracts.

Enforces the SAVMI contract-density standard -- "Agent contracts: terse, technical,
signal-dense. No colloquialisms, filler, or restatement. Maximize signal per token." --
as a stdlib-only, deterministic lint (no model, no network, no DB write), so its verdict
is eligible to gate. Scope: plans, task specs, subagent/handoff instructions, durable DB
records, generated reports. It does NOT lint human-authored docs or commit/PR text.

The result is advisory input a caller can record via Q2 (verification) or T2 (eval score,
source=deterministic); a human or a deterministic check remains the acceptance gate.
"""

from __future__ import annotations

import re
from typing import Any

from .denials import KaizenDenied
from .paths import read_text_file, resolve_user_path

# Wordy / redundant / hedging / colloquial phrases: (label, regex, suggestion). Case-insensitive.
_FILLER_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("wordy", re.compile(r"\bin order to\b", re.I), "to"),
    ("wordy", re.compile(r"\bdue to the fact that\b", re.I), "because"),
    ("wordy", re.compile(r"\bin the event that\b", re.I), "if"),
    ("wordy", re.compile(r"\bat this point in time\b", re.I), "now"),
    ("wordy", re.compile(r"\bat the present time\b", re.I), "now"),
    ("wordy", re.compile(r"\ba (?:large|great) number of\b", re.I), "many"),
    ("wordy", re.compile(r"\bfor the purpose of\b", re.I), "to"),
    ("wordy", re.compile(r"\bwith regard to\b", re.I), "about"),
    ("hedge_phrase", re.compile(r"\bit (?:is|should be) (?:important|worth) (?:to note|noting)(?: that)?\b", re.I), "(cut)"),
    ("hedge_phrase", re.compile(r"\bit should be noted that\b", re.I), "(cut)"),
    ("hedge_phrase", re.compile(r"\bas (?:we|you) can see\b", re.I), "(cut)"),
    ("hedge_phrase", re.compile(r"\bneedless to say\b", re.I), "(cut)"),
    ("hedge_phrase", re.compile(r"\bit goes without saying\b", re.I), "(cut)"),
    ("hedge_phrase", re.compile(r"\bfor all intents and purposes\b", re.I), "(cut)"),
    ("hedge_phrase", re.compile(r"\bat the end of the day\b", re.I), "(cut)"),
    ("hedge_phrase", re.compile(r"\bthe fact of the matter is\b", re.I), "(cut)"),
    ("colloquial", re.compile(r"\bgo for broke\b", re.I), "state the scope plainly"),
    ("colloquial", re.compile(r"\bdogfood(?:s|ing|ed)?\b", re.I), "name the concrete action"),
    ("colloquial", re.compile(r"\bzero to hero\b", re.I), "(cut)"),
    ("colloquial", re.compile(r"\b(?:kind|sort) of\b", re.I), "cut or be precise"),
]

# Single-word hedges / intensifiers (soft signal; density-scored, not a hard hit).
_HEDGE_WORDS = frozenset(
    {
        "really", "very", "just", "actually", "basically", "simply", "quite",
        "rather", "somewhat", "essentially", "literally", "obviously", "clearly",
        "arguably", "fairly", "pretty", "totally", "definitely", "certainly",
        "honestly", "truly", "extremely", "incredibly", "highly",
    }
)

_WORD_RE = re.compile(r"[A-Za-z0-9']+")
# Advisory sentence heuristic: avoid common abbreviations and split only before a capitalized start.
_SENT_SPLIT_RE = re.compile(r"(?<!e\.g\.)(?<!i\.e\.)(?<=[.!?])\s+(?=[A-Z])", re.I)

DEFAULT_HEDGE_DENSITY_MAX = 0.05
DEFAULT_DUP_JACCARD = 0.6


def _shingles(words: list[str], n: int = 3) -> set[tuple[str, ...]]:
    """Return n-gram shingles, treating a shorter non-empty sentence as one shingle."""
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def lint_text(
    text: str,
    *,
    hedge_density_max: float = DEFAULT_HEDGE_DENSITY_MAX,
    dup_jaccard: float = DEFAULT_DUP_JACCARD,
) -> dict[str, Any]:
    """Return verdict, word/sentence counts, signal and hedge densities, filler/hedge hits, duplicate sentence pairs, and reasons; hedge_density_max and dup_jaccard are the failure thresholds."""
    words = _WORD_RE.findall(text)
    word_count = len(words)

    filler_hits = [
        {"label": label, "text": m.group(0), "span": [m.start(), m.end()], "suggestion": suggestion}
        for label, pattern, suggestion in _FILLER_PATTERNS
        for m in pattern.finditer(text)
    ]

    hedge_hits = [
        {"word": m.group(0).lower(), "span": [m.start(), m.end()]}
        for m in _WORD_RE.finditer(text)
        if m.group(0).lower() in _HEDGE_WORDS
    ]
    hedge_density = round(len(hedge_hits) / word_count, 4) if word_count else 0.0

    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(text.strip()) if s.strip()]
    shingle_sets = [_shingles(_WORD_RE.findall(s.lower())) for s in sentences]
    duplicates: list[dict[str, Any]] = []
    for i in range(len(sentences)):
        for j in range(i + 1, len(sentences)):
            a, b = shingle_sets[i], shingle_sets[j]
            if not a or not b:
                continue
            jaccard = len(a & b) / len(a | b)
            if jaccard >= dup_jaccard:
                duplicates.append({"a_index": i, "b_index": j, "jaccard": round(jaccard, 3)})

    penalty_spans = [tuple(hit["span"]) for hit in filler_hits] + [tuple(hit["span"]) for hit in hedge_hits]
    penalized_words = sum(
        any(start < word.end() and word.start() < end for start, end in penalty_spans)
        for word in _WORD_RE.finditer(text)
    )
    signal_per_token = round(1 - penalized_words / word_count, 4) if word_count else 1.0

    reasons: list[str] = []
    if filler_hits:
        reasons.append(f"{len(filler_hits)} filler/wordy/colloquial phrase(s)")
    if hedge_density > hedge_density_max:
        reasons.append(f"hedge density {hedge_density} > {hedge_density_max}")
    if duplicates:
        reasons.append(f"{len(duplicates)} near-duplicate sentence pair(s)")

    return {
        "verdict": "pass" if not reasons else "fail",
        "word_count": word_count,
        "sentence_count": len(sentences),
        "signal_per_token": signal_per_token,
        "hedge_density": hedge_density,
        "filler_hits": filler_hits,
        "hedge_hits": hedge_hits,
        "duplicate_sentences": duplicates,
        "reasons": reasons,
    }


def contract_lint(args: Any) -> dict[str, Any]:
    """Q10: read text in body, body-file, then path precedence, deny when absent, and call lint_text with the fixed CLI thresholds; library callers may tune lint_text directly."""
    text = getattr(args, "body", None)
    if not text and getattr(args, "body_file", None):
        text = read_text_file(args.body_file)
    if not text and getattr(args, "path", None):
        allow_external = bool(getattr(args, "allow_external", False))
        path = resolve_user_path(
            args.path,
            require_file=True,
            repo_only=not allow_external,
            allow_external_hint=not allow_external,
        )
        text = read_text_file(str(path))
    if not text:
        raise KaizenDenied(
            "DENIED_CONTRACT_TEXT_REQUIRED",
            {"required_action": "pass --body, --body-file, or --path (the contract text to lint)"},
            exit_code=2,
        )

    result = lint_text(text)
    return {
        "status": "OK",
        "op": "contract-lint",
        **result,
        "note": (
            "deterministic contract-density lint; advisory input to Q2/T2 -- "
            "a human or deterministic check remains the acceptance gate."
        ),
    }
