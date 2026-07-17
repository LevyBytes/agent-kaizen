"""Build paste-ready prompts for creating new prompt-generation scripts.

Deterministic core plus an optional copy-paste LLM-assist loop: the script
composes an assist request you paste into any LLM (ChatGPT, Claude, Codex, ...),
you paste the reply back, and the script turns suggestions into editable
defaults for your interview answers. The final build-prompt is always assembled
deterministically from those answers, so `last` stays byte-reproducible.

The generated prompt is tailored to where it will run: a repo-aware VS Code AI
extension (Claude / Codex) or an online LLM that cannot see your files.
"""

from __future__ import annotations

import ctypes
import difflib
import importlib
import json
import os
import re
import subprocess
import sys
from collections import OrderedDict
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    # Optional: salvages the slightly-broken JSON LLMs commonly emit. The
    # script runs fine without it (stdlib parsing + advisory fallback) and can
    # offer to install it when the assist loop is on.
    import json_repair
except ImportError:  # pragma: no cover - exercised only when the package is absent
    json_repair = None

# (import name, pip name, why it helps) for optional packages the assist loop
# can use. Kept generic so more can be added without reshaping the code.
OPTIONAL_DEPENDENCIES = (
    ("json_repair", "json-repair", "more robust parsing of pasted LLM replies"),
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
if hasattr(sys.stdin, "reconfigure"):
    # utf-8-sig strips the BOM that Windows shells can prepend to piped input
    sys.stdin.reconfigure(encoding="utf-8-sig")


LOG_PATH = Path(__file__).resolve().with_suffix(".log")
SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT_STEM = Path(__file__).stem


class QuitRequested(Exception):
    """Raised when the user types q/quit at any prompt to exit cleanly."""


# Sentinel returned by interview prompts when the user types b/back to go to
# the previous question (only when the prompt was called with allow_back=True).
BACK = object()
MULTILINE_SENTINEL = "END"


def find_repo_root(start: Path) -> Path:
    """Find the repository root by walking upward from a starting folder.

    The repo root gives the script stable defaults without hard-coding one
    person's machine path.
    """
    for candidate in (start, *start.parents):
        if (candidate / "AGENTS.md").is_file() or (candidate / ".git").exists():
            return candidate
    return start


REPO_ROOT = find_repo_root(SCRIPT_DIR)

SETTINGS_MARKER = "# ==== SETTINGS (current defaults; rewritten in place by the script) ===="
SESSIONS_MARKER = "# ==== SESSIONS (root-hash working contexts; newest last) ===="
DRAFTS_MARKER = "# ==== SESSION DRAFTS (latest answers per session; rewritten in place) ===="
HISTORY_MARKER = "# ==== RUN HISTORY (append-only; newest last) ===="
DEVLOG_MARKER = "# ==== DEVELOPMENT LOG (append-only; decisions and actions per run) ===="

MODES = ("new script prompt", "meta prompt")
BACKUP_MODES = ("ask", "always", "never")
RISK_LEVELS = ("low", "medium", "high")
ASSIST_MODES = ("on", "off")
LLM_TARGETS = ("extension", "online")
EXTENSION_NAMES = ("Claude", "Codex")

# Each risk level maps to a concrete control strength for the receiving LLM,
# so the answer changes behavior instead of just being echoed back.
RISK_GUIDANCE = OrderedDict(
    (
        (
            "low",
            "proceed through the agreed spec on your own, checkpointing after "
            "each reviewable increment.",
        ),
        (
            "medium",
            "confirm key decisions and assumptions with the user before "
            "editing; do not proceed on unconfirmed assumptions.",
        ),
        (
            "high",
            "require explicit user confirmation for every key decision, and "
            "propose tool-level guardrails (permission rules, hooks, or "
            "sandboxing) before touching anything hard to reverse.",
        ),
    )
)

SETTING_KEYS = (
    "mode",
    "assist_mode",
    "copy_to_clipboard",
    "backup_mode",
    "backup_folder",
    "prompt_archive_folder",
    "result_log_folder",
)

DEFAULT_SETTINGS = OrderedDict(
    (
        ("mode", "new script prompt"),
        ("assist_mode", "on"),
        ("copy_to_clipboard", "yes"),
        ("backup_mode", "ask"),
        ("backup_folder", str(REPO_ROOT / "user" / "user-generated-prompts")),
        ("prompt_archive_folder", str(REPO_ROOT / "user" / "prompts-given")),
        ("result_log_folder", str(REPO_ROOT / "user" / "prompt-results-logs")),
    )
)

FOLDER_EXPLANATIONS = {
    "backup_folder": "Where optional Markdown backups of generated prompts are saved.",
    "prompt_archive_folder": "Repo folder where the extension saves the exact prompt it received.",
    "result_log_folder": "Repo folder where the extension writes its work log after building.",
}
# Folder settings should never be a bare confirm word - guard against the common
# habit of typing y/n at the one-step prompt (which would set the folder to "y").
PATH_SETTINGS = ("backup_folder", "prompt_archive_folder", "result_log_folder")
CONFIRM_TOKENS = {"y", "yes", "n", "no"}

DEFAULT_DECISION = "doing this by hand is slow and inconsistent; I want reliable, repeatable prompts"
DEFAULT_TASK = "build a new interactive prompt-generation script"
DEFAULT_AUDIENCE = "a developer who runs the script and pastes its prompts into an LLM"
DEFAULT_CONSUMERS_EXTENSION = "the VS Code extension writes the script into the repo, then I run it"
DEFAULT_CONSUMERS_ONLINE = "I paste the LLM's output, save the script myself, then run it"
DEFAULT_PROMPT_OUTPUT = "a complete prompt the user can paste into an LLM"
DEFAULT_SOURCE_REFS = (
    "the Kaizen System (SAVMI) and an existing "
    "prompt-builder script as a reference implementation"
)
DEFAULT_INPUTS = "interactive terminal questions with clear prompts and defaults"
DEFAULT_LOG_BEHAVIOR = "sibling .log with SETTINGS and RUN HISTORY zones; prompts are not stored in the log"
DEFAULT_LOCATION = "ask the user after inspecting the existing script categories"
DEFAULT_CONSTRAINTS = "Python 3.10+, standard library only, UTF-8 file I/O, pathlib paths"
DEFAULT_DONE = (
    "first run ends in a complete prompt with zero unresolved placeholders; "
    "the sibling log has both zones; the unittest suite passes"
)
DEFAULT_VERIFICATION = "real script runs, log inspection, and unittest coverage"
DEFAULT_RISK = "medium"
DEFAULT_ONLINE_NAME = "ChatGPT"

# (attribute, friendly label, one-line explanation, default) for every
# free-text interview field, in question order.
INTERVIEW_FIELDS = (
    (
        "task",
        "What should the new script help you build prompts for?",
        "The kind of prompt-builder you want to create.",
        DEFAULT_TASK,
    ),
    (
        "driving_decision",
        "What problem does that solve for you?",
        "The recurring problem the prompts above should fix - the reason this script is worth building.\n"
        "Describe the pain or goal, not how the script works. A clear objective is fine.\n"
        "Example: getting scattered online and downloaded docs into LLM-optimized markdown an agent can actually use, which is hard to do well by hand.",
        DEFAULT_DECISION,
    ),
    (
        "audience",
        "Who will run this script and use its prompts?",
        "The person who runs the script day to day.",
        DEFAULT_AUDIENCE,
    ),
    (
        "consumers_next_action",
        "After a prompt is generated, what happens next?",
        "Who or what consumes the prompt and the next step they take.",
        DEFAULT_CONSUMERS_EXTENSION,
    ),
    (
        "prompt_output",
        "What should each generated prompt make the LLM produce?",
        "The artifact the LLM should output when given the prompt.",
        DEFAULT_PROMPT_OUTPUT,
    ),
    (
        "source_refs",
        "Any reference material the LLM should follow?",
        "Docs or examples that define the expected output (describe them for online LLMs).",
        DEFAULT_SOURCE_REFS,
    ),
    (
        "inputs",
        "How should the script ask its questions?",
        "How the user interacts with the script.",
        DEFAULT_INPUTS,
    ),
    (
        "log_behavior",
        "How should the script remember settings and history?",
        "Whether and how it keeps a sibling log.",
        DEFAULT_LOG_BEHAVIOR,
    ),
    (
        "target_location",
        "Where should the new script live?",
        "Folder for the new script (used only for a repo-aware extension).",
        DEFAULT_LOCATION,
    ),
    (
        "constraints",
        "Any hard limits?",
        "Language, runtime, or library constraints.",
        DEFAULT_CONSTRAINTS,
    ),
    (
        "done_looks_like",
        "How will you know it's finished and correct?",
        "Your definition of done.",
        DEFAULT_DONE,
    ),
    (
        "verification_signal",
        "How will you verify it actually works?",
        "The real signal you'll check.",
        DEFAULT_VERIFICATION,
    ),
)
LABEL_BY_ATTR = {attr: label for attr, label, _explain, _default in INTERVIEW_FIELDS}
LABEL_BY_ATTR["risk_level"] = "risk level"
# attr -> (label, explanation, default) for the navigable interview steps.
FIELD_META = {attr: (label, explain, default) for attr, label, explain, default in INTERVIEW_FIELDS}
# Field attributes the assist loop may suggest values for (risk_level last).
KNOWN_FIELDS = tuple(attr for attr, _l, _e, _d in INTERVIEW_FIELDS) + ("risk_level",)

OPENING_MARKER = "==== GENERATED PROMPT — paste into your LLM ===="
CLOSING_MARKER = "==== END GENERATED PROMPT ===="
ASSIST_OPEN = "==== ASSIST REQUEST — paste into any LLM, then paste the reply back ===="
ASSIST_CLOSE = "==== END ASSIST REQUEST ===="
PASTE_SENTINEL = "<<END>>"
PASTE_CAP = 100_000
# Tokens an online prompt must never contain (it cannot see repo files).
ONLINE_BANNED_TOKENS = (
    "claude.md",
    "agents.md",
    ".agents",
    "ai/work",
    "user-generated-prompts",
    "repo-relative",
)

KAIZEN_SUMMARY = """Use the Kaizen System (SAVMI) as the operating model. Scale the rigor of each layer to the risk, ambiguity, cost, and blast radius of the work:
- Scope: turn intent and evidence into a compact Iterative Spec before changing anything - goal, consumers, inputs, assumptions and how they will be tested, explicit boundaries, and verifiable acceptance criteria. Do evidence-only research, interview the user to remove risky ambiguity, and pause at decision and approval gates instead of drifting when scope, risk, cost, or behavior changes.
- Adapt: turn the approved scope into bounded execution under a contract that states what may be read, written, and run, what is forbidden, the required capabilities, the expected outputs, and rollback. Work in small increments, push deterministic mechanics (naming, formatting, paths, schemas, ledger writes) into scripts, and revise the plan when evidence changes.
- Verify: answer go or no-go with deterministic evidence first - tests, linters, type checks, schema validators, command output, rendered screenshots, and hashes - and use human or model synthesis only where judgment is needed. Record the result with ordered, evidence-cited findings labeled by severity, scope, and actionability.
- Manage: keep durable, structured records (tasks, plans, ledgers, proof, source locks, artifacts, and GOTCHA/LEARNING/LEARNED lessons) through deterministic write paths instead of relying on chat memory; keep records small and queryable.
- Improve: retrospect on what failed or repeated, what should become a script, an eval, or a quality rule, and what to delete; promote GOTCHA -> LEARNING -> LEARNED and feed better priorities into the next Scope cycle.
Evidence authority: raw upstream evidence - source code, specifications, official docs, test output, logs, hashes, and explicit user decisions - outranks summaries and model synthesis; when they conflict, return to the evidence and record the correction."""

NEW_SCRIPT_EXTENSION_TEMPLATE = """You are working in a VS Code repository as my chosen AI extension: {llm_name}. You can read files in this repository.

Goal
Build or plan a new prompt-generation script for this request: {task}.
Problem it solves: {driving_decision}.

Consumers and next action
- Primary user: {audience}.
- After a prompt is generated: {consumers_next_action}.
- The script should produce: {prompt_output}.

Context and required repo inspection
- Before implementing, inspect the repository structure, existing prompt scripts, README files, tests, and gitignore rules.
- Follow the repo conventions in {memory_file_abs} and the .agents folder if present.
- Review the current user-generated-prompts subfolder layout if it exists. If it does not exist, say that clearly.
- Ask the user where they want the new script placed after inspection, and suggest likely destination folders based on existing script categories.
- Do not invent missing folders silently; only create new repo folders when explicitly asked by the user.
- Treat these source references as starting points: {source_refs}.

Kaizen operating method (SAVMI)
{kaizen_summary}

Scope for the receiving LLM
- Produce the smallest reviewable increment that creates the requested prompt-generation script.
- Keep the script interactive in the terminal: {inputs}.
- Use this log behavior unless the user changes it: {log_behavior}.
- Default placement preference from the user: {target_location}.
- Constraints: {constraints}.
- Cost of error is {risk_level}: {risk_guidance}

Acceptance criteria
- The user's definition of done: {done_looks_like}.
- Before starting, outline the precise evaluation criteria you will use; make each criterion observable and binary where possible.
- Make the user verify key decisions explicitly to ensure nothing is missed.

Implementation requirements
- Use Python 3.10+ and the standard library only.
- Keep the script fully self-contained: one file with all prompt templates embedded as string constants — never read template text from another file at runtime, and never import from sibling scripts.
- Use single number or letter shortcuts for modes and commands; reserve free typing for descriptive values; allow q to quit at any question.
- Copy generated prompts to the clipboard when supported, but always print the prompt between unmistakable markers.
- Protect one-click launches: keep the terminal open at the end when running interactively outside VS Code so the user cannot lose the prompt.
- Add docstrings to every function and helper class so a Python learner can understand the script.
- Never write generated prompt text to a sibling log.
- Keep tests and any other working documents under the repo's gitignored work area (AI/work/); do not add new repo folders for them.
- The script may optionally include the same copy-paste LLM-assist loop this generator uses, but it must stay fully functional without it.

Build in two passes
- Pass 1: draft the script to the spec and the capability decisions above.
- Pass 2: self-review the draft against this rubric before showing it, and fix every gap:
  - input edge cases handled (bracketed, empty, or oversized answers do not corrupt output);
  - interrupted runs (EOF / Ctrl-C / quit) leave the log and any written files valid;
  - the same inputs reproduce the same output;
  - persistence and recall of prior runs where the goal benefits;
  - single number/letter controls, with descriptive values typed in full;
  - the emitted artifact prints between clear markers and is self-contained;
  - tests live under AI/work and pass; every function and class has a docstring.
- Only present the script after this self-review pass.

Prompt/result logging instructions for this run
- Save the exact prompt you received to this repo-relative path: {prompt_archive_path}.
- After completing the work, write a Markdown work log to this repo-relative path: {result_log_path}.
- Use relative repo links or paths when referring to files.

Verification plan
- External signal to use: {verification_signal}.
- Add or update standard-library unittest coverage and run it before claiming success.
- Report what passed, what was not run, and any residual risk.

Checkpoint instruction
Before making non-trivial edits, show the user the small spec, key decisions, assumptions, acceptance criteria, and verification plan. If a required decision is missing, ask a precise question instead of guessing."""

NEW_SCRIPT_ONLINE_TEMPLATE = """You are {llm_name}, an online assistant. You cannot access my files, repository, or tools — everything you need is in this prompt. Do not reference or assume any local files; I will save your output myself.

Goal
Help me build a new prompt-generation script for this request: {task}.
Problem it solves: {driving_decision}.

Consumers and next action
- Primary user: {audience}.
- After a prompt is generated: {consumers_next_action}.
- The script should produce: {prompt_output}.

Context (everything is described here; do not look for files)
- Treat these described references as the only context you have: {source_refs}.

Kaizen operating method (SAVMI)
{kaizen_summary}

Scope
- Produce the smallest reviewable increment that creates the requested prompt-generation script.
- Keep the script interactive in the terminal: {inputs}.
- Use this log behavior unless I change it: {log_behavior}.
- Constraints: {constraints}.
- Cost of error is {risk_level}: {risk_guidance}

Acceptance criteria
- My definition of done: {done_looks_like}.
- Before starting, outline the precise evaluation criteria you will use; make each criterion observable and binary where possible.
- Surface key decisions and assumptions for me to confirm before you finalize.

Implementation requirements
- Use Python 3.10+ and the standard library only.
- Keep the script fully self-contained: one file with all prompt templates embedded as string constants.
- Use single number or letter shortcuts for modes and commands; reserve free typing for descriptive values; allow q to quit at any question.
- Copy generated prompts to the clipboard when supported, but always print the prompt between unmistakable markers.
- Add docstrings to every function and helper class so a Python learner can understand the script.
- Never write generated prompt text into a sibling log.
- Include a standard-library unittest, in or beside the file, that I can run.

Build in two passes
- Pass 1: draft the script to the spec and the capability decisions above.
- Pass 2: self-review the draft against this rubric before showing it, and fix every gap:
  - input edge cases handled (bracketed, empty, or oversized answers do not corrupt output);
  - interrupted runs (EOF / Ctrl-C / quit) leave any log and files valid;
  - the same inputs reproduce the same output;
  - single number/letter controls, with descriptive values typed in full;
  - the emitted artifact prints between clear markers and is self-contained;
  - a standard-library unittest is included; every function and class has a docstring.
- Only present the script after this self-review pass.

Output
- Output the complete script as one code block I can copy in full.
- Then add a short note on where to save it and how to run it. Do not assume any folder layout.

Verification plan
- External signal to use: {verification_signal}.
- I will run the script and its unittest myself; tell me exactly what to check.

Checkpoint
Before finalizing, show me the small spec, key decisions, assumptions, acceptance criteria, and verification plan. If a required decision is missing, ask a precise question instead of guessing."""

META_EXTENSION_TEMPLATE = """You are working in a VS Code repository as my chosen AI extension: {llm_name}. You can read files in this repository.

Use this reusable operating prompt whenever I ask you to create or change a prompt-generation script.

Kaizen operating method (SAVMI)
{kaizen_summary}

Required workflow
1. Inspect the repository before asking questions. Read local README files, existing prompt scripts, tests, gitignore rules, and the conventions in {memory_file_abs} and the .agents folder.
2. Identify the actual goal: what decision the generated prompt supports, who uses it, and what done looks like.
3. Review the current user-generated-prompts subfolder layout if it exists. If it does not exist, say that clearly.
4. Ask the user where the new script should be placed, and suggest likely destination folders based on existing script categories.
5. Produce a small spec before editing: goal, consumers, scope, out of scope, inputs, constraints, assumptions, acceptance criteria, and verification.
6. Implement with existing repo conventions: interactive terminal flow, sibling .log with SETTINGS and RUN HISTORY zones, no prompt text in logs, UTF-8, pathlib, and standard library only unless explicitly approved. Keep each script fully self-contained: one file with embedded templates. Use single number or letter shortcuts for modes and commands, free typing only for descriptive values, and allow q to quit.
7. Add function and class docstrings so someone learning Python can understand the script.
8. Copy generated prompts to the clipboard when supported, print them between unmistakable markers, and protect one-click launches.
9. Keep tests and working documents under the repo's gitignored work area (AI/work/); only create new repo folders when explicitly asked.
10. Verify using external signal: real script runs, log snapshots, and unittest coverage.

Quality bar
- Define precise acceptance criteria with the user before building, and make the user verify key decisions explicitly.
- Never present unverified work as verified.
- Keep generated scripts readable, deterministic, and safe for repeated local use."""

META_ONLINE_TEMPLATE = """You are {llm_name}, an online assistant. You cannot access my files or repository — everything you need is in this prompt. Do not reference or assume any local files.

Use this reusable operating prompt whenever I ask you to help create or change a prompt-generation script.

Kaizen operating method (SAVMI)
{kaizen_summary}

Required workflow
1. Identify the actual goal: what decision the generated prompt supports, who uses it, and what done looks like.
2. Produce a small spec before drafting: goal, consumers, scope, out of scope, inputs, constraints, assumptions, acceptance criteria, and verification.
3. Implement as a fully self-contained single-file Python script (standard library only unless I approve otherwise): interactive terminal flow, optional sibling log for settings and history but never the prompts, UTF-8, single number or letter shortcuts for controls, free typing only for descriptive values, and q to quit.
4. Add function and class docstrings so someone learning Python can understand the script.
5. Copy generated prompts to the clipboard when supported, and print them between unmistakable markers.
6. Include a standard-library unittest I can run.
7. Output the complete script as one code block, then a short note on where to save it and how to run it.

Quality bar
- Define precise acceptance criteria with me before drafting, and surface key decisions for me to verify.
- Never present unverified work as verified; I will run the verification myself.
- Keep generated scripts readable, deterministic, and safe for repeated local use."""


@dataclass
class RunEntry:
    """Store every per-run answer from one prompt-generation run.

    All answers are kept in the sibling log's RUN HISTORY zone so the `last`
    command can reproduce the previous prompt exactly, without ever storing
    the generated prompt text itself.
    """

    timestamp: str
    mode: str
    session: str
    llm_target: str
    llm_name: str
    driving_decision: str
    task: str
    audience: str
    consumers_next_action: str
    prompt_output: str
    source_refs: str
    inputs: str
    log_behavior: str
    target_location: str
    constraints: str
    done_looks_like: str
    verification_signal: str
    risk_level: str


@dataclass
class SessionEntry:
    """One named working context, identified by a short root hash.

    Recorded in the log's SESSIONS zone the moment a New session is chosen, so
    Continue can list and resume it later.
    """

    created: str
    root: str
    name: str


# History line keys, in write order, mapped to RunEntry attribute names.
HISTORY_FIELDS = OrderedDict(
    (
        ("mode", "mode"),
        ("session", "session"),
        ("target", "llm_target"),
        ("llm", "llm_name"),
        ("decision", "driving_decision"),
        ("task", "task"),
        ("audience", "audience"),
        ("consumers", "consumers_next_action"),
        ("output", "prompt_output"),
        ("refs", "source_refs"),
        ("inputs", "inputs"),
        ("log", "log_behavior"),
        ("location", "target_location"),
        ("constraints", "constraints"),
        ("done", "done_looks_like"),
        ("verify", "verification_signal"),
        ("risk", "risk_level"),
    )
)


def memory_file_for(llm_name: str) -> str:
    """Return the repo memory file an extension follows (Claude vs Codex)."""
    return "CLAUDE.md" if llm_name.strip().lower().startswith("claude") else "AGENTS.md"


def normalize_spaces(value: str) -> str:
    """Collapse repeated whitespace into single spaces."""
    return " ".join(value.split())


def clean_for_log(value: str) -> str:
    """Make user-entered text safe for the simple pipe-delimited log format.

    " | " is the history field separator and square brackets would look like
    unresolved placeholders in the finished prompt, so both are neutralized. Filesystem paths must
    bypass this destructive normalization because brackets are legal path characters.
    """
    return normalize_spaces(value).replace(" | ", " / ").replace("[", "(").replace("]", ")")


def is_enabled(value: str) -> bool:
    """Interpret a yes/no style setting as a Python boolean."""
    return value.strip().lower() in {"yes", "y", "true", "1", "on"}


def is_yes_no(value: str) -> bool:
    """Return True when text is an accepted yes/no setting value."""
    return value.strip().lower() in {"yes", "y", "no", "n", "true", "false", "1", "0", "on", "off"}


def read_line(prompt: str) -> str:
    """Read one input line, raising QuitRequested when the user types q/quit."""
    answer = input(prompt)
    if answer.strip().lower() in {"q", "quit"}:
        raise QuitRequested
    return answer


def make_root_hash() -> str:
    """Generate a short, unique session root hash (8 hex characters)."""
    return os.urandom(4).hex()


def format_session_line(session: SessionEntry) -> str:
    """Format one SessionEntry for the SESSIONS zone."""
    return f"[{session.created}] root={session.root} | name={session.name}"


def parse_session_line(line: str) -> SessionEntry | None:
    """Parse one SESSIONS line into a SessionEntry, or None if malformed."""
    match = re.match(r"^\[(?P<created>[^\]]+)\]\s+(?P<body>.+)$", line.strip())
    if not match:
        return None
    values: dict[str, str] = {}
    for part in match.group("body").split(" | "):
        if "=" not in part:
            return None
        key, value = part.split("=", 1)
        values[key.strip()] = value.strip()
    if not values.get("root"):
        return None
    return SessionEntry(created=match.group("created"), root=values["root"], name=values.get("name", ""))


def read_log() -> tuple[
    OrderedDict[str, str],
    list[RunEntry],
    list[SessionEntry],
    "dict[str, RunEntry]",
    list[str],
    list[str],
    bool,
]:
    """Read settings, sessions, drafts, run history, and the development log.

    Returns settings, parsed history, parsed sessions, the per-session drafts
    (latest answers keyed by session root), the raw development-log lines
    (preserved verbatim so they round-trip), recoverable parse issues, and a
    first-run flag that tells main whether to create an initial log.
    """
    settings = OrderedDict(DEFAULT_SETTINGS)
    history: list[RunEntry] = []
    sessions: list[SessionEntry] = []
    drafts: dict[str, RunEntry] = {}
    devlog: list[str] = []
    issues: list[str] = []

    if not LOG_PATH.exists():
        return settings, history, sessions, drafts, devlog, issues, True

    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        issues.append(f"could not read log ({exc}); using built-in defaults")
        return settings, history, sessions, drafts, devlog, issues, False

    zone = None
    seen_settings: set[str] = set()
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if line == SETTINGS_MARKER:
            zone = "settings"
            continue
        if line == SESSIONS_MARKER:
            zone = "sessions"
            continue
        if line == DRAFTS_MARKER:
            zone = "drafts"
            continue
        if line == HISTORY_MARKER:
            zone = "history"
            continue
        if line == DEVLOG_MARKER:
            zone = "devlog"
            continue
        if zone == "devlog":
            devlog.append(raw_line)
            continue
        if not line or line.startswith("#"):
            continue
        if zone == "sessions":
            parsed_session = parse_session_line(raw_line)
            if parsed_session is None:
                issues.append(f"session line {line_number} ignored")
            else:
                sessions.append(parsed_session)
            continue
        if zone == "drafts":
            parsed_draft = parse_history_line(raw_line)
            if parsed_draft is None or not parsed_draft.session:
                issues.append(f"draft line {line_number} ignored")
            else:
                drafts[parsed_draft.session] = parsed_draft
            continue

        if zone == "settings":
            if "=" not in raw_line:
                issues.append(f"line {line_number} ignored")
                continue
            key, value = raw_line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key not in DEFAULT_SETTINGS:
                issues.append(f"unknown setting on line {line_number} ignored")
                continue
            if key == "mode" and value not in MODES:
                issues.append(f"mode on line {line_number} reset to default")
                continue
            if key == "assist_mode" and value not in ASSIST_MODES:
                issues.append(f"assist_mode on line {line_number} reset to default")
                continue
            if key == "copy_to_clipboard" and not is_yes_no(value):
                issues.append(f"copy_to_clipboard on line {line_number} reset to default")
                continue
            if key == "backup_mode" and value not in BACKUP_MODES:
                issues.append(f"backup_mode on line {line_number} reset to default")
                continue
            if not value:
                issues.append(f"{key} on line {line_number} reset to default")
                continue
            settings[key] = value
            seen_settings.add(key)
        elif zone == "history":
            parsed = parse_history_line(raw_line)
            if parsed is None:
                issues.append(f"history line {line_number} ignored")
            else:
                history.append(parsed)
        else:
            issues.append(f"line {line_number} outside a known zone ignored")

    for key in SETTING_KEYS:
        if key not in seen_settings:
            issues.append(f"{key} missing; using built-in default")

    return settings, history, sessions, drafts, devlog, issues, False


def parse_history_line(line: str) -> RunEntry | None:
    """Convert one RUN HISTORY line into a RunEntry object.

    Tolerant by design: any line whose keys are a subset of the known field
    keys parses, with missing fields backfilled to empty. This keeps older
    logs (written before newer fields existed) working. Lines with unknown
    keys, or an invalid mode/risk, are rejected.
    """
    match = re.match(r"^\[(?P<timestamp>[^\]]+)\]\s+(?P<body>.+)$", line.strip())
    if not match:
        return None

    values: dict[str, str] = {}
    for part in match.group("body").split(" | "):
        if "=" not in part:
            return None
        key, value = part.split("=", 1)
        values[key.strip()] = value.strip()

    if not set(values).issubset(set(HISTORY_FIELDS)):
        return None
    if values.get("mode") not in MODES:
        return None
    if values.get("risk") not in RISK_LEVELS:
        return None

    fields = {attr: values.get(key, "") for key, attr in HISTORY_FIELDS.items()}
    return RunEntry(timestamp=match.group("timestamp"), **fields)


def write_log(
    settings: OrderedDict[str, str],
    history: list[RunEntry],
    sessions: list[SessionEntry],
    drafts: "dict[str, RunEntry]",
    devlog: list[str],
) -> None:
    """Write settings, sessions, drafts, history, and the dev log atomically."""
    lines = [SETTINGS_MARKER]
    for key in SETTING_KEYS:
        lines.append(f"{key} = {settings[key]}")
    lines.append(SESSIONS_MARKER)
    for session in sessions:
        lines.append(format_session_line(session))
    lines.append(DRAFTS_MARKER)
    for entry in drafts.values():
        lines.append(format_history_entry(entry))
    lines.append(HISTORY_MARKER)
    for entry in history:
        lines.append(format_history_entry(entry))
    lines.append(DEVLOG_MARKER)
    lines.extend(devlog)

    tmp_path = LOG_PATH.with_name(LOG_PATH.name + ".tmp")
    try:
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp_path, LOG_PATH)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def format_history_entry(entry: RunEntry) -> str:
    """Format one RunEntry for the append-only history zone."""
    body = " | ".join(
        f"{key}={getattr(entry, attr)}" for key, attr in HISTORY_FIELDS.items()
    )
    return f"[{entry.timestamp}] {body}"


def format_devlog_lines(
    run_number: int, run: RunEntry, assist_on: bool, target_tag: str, events: list[str]
) -> list[str]:
    """Format one run's development-log block (header + indented event bullets).

    Records decisions and actions only; never prompt text. It references the
    archive path (extension) or notes the run was not archived (online).
    """
    header = (
        f"[{run.timestamp}] run {run_number} | session={run.session} | mode={run.mode} | "
        f"target={target_tag} | assist={'on' if assist_on else 'off'}"
    )
    return [header] + [f"  - {event}" for event in events]


def ask_yes_no(prompt: str, default: str = "n") -> bool:
    """Ask a yes/no question and return True for yes or False for no."""
    default = default.lower()
    while True:
        answer = read_line(f"{prompt} [{default}]: ").strip().lower()
        value = default if answer == "" else answer
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Enter y or n (or q to quit).")


def prompt_text(prompt: str, default: str) -> str:
    """Ask a plain text question and return the answer or the shown default."""
    answer = read_line(f"{prompt} [{default}]: ").strip()
    return default if answer == "" else clean_for_log(answer)


def read_multiline_answer(sentinel: str = MULTILINE_SENTINEL) -> str:
    """Read a multi-line answer until a line equal to the sentinel, or EOF.

    Reads literal text via raw input(), so q/b are not special inside it; caps
    total size so a runaway paste cannot hang or exhaust memory.
    """
    print(f"  Paste or type your answer; finish with a line containing only {sentinel}:")
    collected: list[str] = []
    size = 0
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().upper() == sentinel:
            break
        line_size = len(line) + 1
        if size + line_size > PASTE_CAP:
            remaining = max(0, PASTE_CAP - size - 1)
            if remaining:
                collected.append(line[:remaining])
            print("  Too long; using what was captured so far.")
            break
        size += line_size
        collected.append(line)
    return "\n".join(collected)


def confirm_or_enter(label: str, explanation: str, default: str, allow_back: bool = False) -> str | object:
    """Ask a free-text question in one step: keep the default or replace it.

    Enter keeps the shown default; typing anything makes that text the new
    value (always saved); `m` opens a multi-line capture; `b` (when allow_back)
    returns the BACK sentinel; `q` quits. Returns the value, or BACK.
    """
    print(f"\n{label}")
    for line in explanation.splitlines():
        print(f"  {line}")
    print(f"  current default: {default}")
    help_text = "Enter=keep | type a value | m=multi-line"
    if allow_back:
        help_text += " | b=back"
    help_text += " | q=quit"
    answer = read_line(f"  {help_text}: ").strip()
    low = answer.lower()
    if allow_back and low in {"b", "back"}:
        return BACK
    if low in {"m", "ml"}:
        text = read_multiline_answer()
        return clean_for_log(text) if text.strip() else default
    return clean_for_log(answer) if answer else default


def validate_path(value: str) -> tuple[bool, str]:
    """Validate a folder-path setting; return (ok, feedback message).

    Rejects empty input, control characters, and ``<>"|?*`` while permitting ``:`` for Windows
    drive letters; resolves relative paths against the repo root and reports whether the folder exists.
    """
    raw = value.strip()
    if not raw:
        return False, "  Please type a folder path."
    if any(ch in raw for ch in '<>"|?*') or any(ord(ch) < 32 for ch in raw):
        return False, "  That folder path has invalid characters; please type a valid path."
    try:
        path = Path(raw)
        resolved = (path if path.is_absolute() else REPO_ROOT / path).resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        return False, f"  That is not a usable path ({exc}); please type a valid folder path."
    if resolved.exists() and not resolved.is_dir():
        return False, f"  {resolved} is a file, not a folder; choose another path."
    state = "exists" if resolved.exists() else "will be created when needed"
    return True, f"  Saved folder: {resolved} ({state})."


def prompt_path(label: str, explanation: str, default: str) -> str:
    """Ask for a folder-path setting, validating it before moving on.

    Rejects a bare y/n (a confirm habit), validates the path, and reports the
    resolved folder and whether it exists, re-asking until the value is a usable
    path. Enter keeps the default; q quits.
    """
    print(f"\n{label}")
    for line in explanation.splitlines():
        print(f"  {line}")
    print(f"  current default: {default}")
    while True:
        answer = read_line("  Enter=keep | type a folder path | q=quit: ").strip()
        if answer.lower() in CONFIRM_TOKENS:
            print(
                "  That looks like a yes/no. Type the actual folder path, "
                "or press Enter to keep the default."
            )
            continue
        value = default if answer == "" else answer.strip()
        ok, feedback = validate_path(value)
        print(feedback)
        if ok:
            return value


def prompt_menu(
    label: str,
    explanation: str,
    options: tuple[tuple[str, tuple[str, ...], str], ...],
    default: str,
    allow_back: bool = False,
) -> str | object:
    """Ask a choice question selectable by number, letter, or full value.

    Returns the chosen value, or BACK when allow_back and the user types b.
    """
    print(f"\n{label}")
    if explanation:
        print(f"  {explanation}")
    for index, (value, keys, desc) in enumerate(options, start=1):
        keylabel = " / ".join((str(index),) + keys)
        print(f"  {keylabel} - {value}: {desc}")
    suffix = " | b=back" if allow_back else ""
    while True:
        answer = read_line(f"  {label} [{default}] (q=quit{suffix}): ").strip().lower()
        if allow_back and answer in {"b", "back"}:
            return BACK
        if answer == "":
            return default
        for index, (value, keys, _desc) in enumerate(options, start=1):
            if answer == str(index) or answer in keys or answer == value.lower():
                return value
        print("  Choose one of the listed options.")


def prompt_toggle(label: str, explanation: str, default: str) -> str:
    """Ask an on/off setting, accepting on/off or y/n; return 'on' or 'off'."""
    print(f"\n{label}")
    if explanation:
        print(f"  {explanation}")
    shown = "on" if default == "on" else "off"
    while True:
        answer = read_line(f"  {label} [{shown}] (on/off or y/n, q=quit): ").strip().lower()
        if answer == "":
            return shown
        if answer in {"on", "y", "yes", "1", "true"}:
            return "on"
        if answer in {"off", "n", "no", "0", "false"}:
            return "off"
        print("  Enter on/off or y/n.")


def prompt_copy_setting(default: str) -> str:
    """Ask whether to copy prompts to the clipboard; return 'yes' or 'no'."""
    toggled = prompt_toggle(
        "copy_to_clipboard",
        "Copy each generated prompt to the clipboard automatically.",
        "on" if is_enabled(default) else "off",
    )
    return "yes" if toggled == "on" else "no"


def prompt_backup_mode(default: str) -> str:
    """Ask the backup mode, accepting ask/always/never or y(=always)/n(=never)."""
    print("\nbackup_mode")
    print("  Backups save a Markdown copy of each generated prompt so you can reread it later.")
    while True:
        answer = read_line(
            f"  backup_mode [{default}] (ask / always / never, or y=always / n=never, q=quit): "
        ).strip().lower()
        if answer == "":
            return default
        if answer in {"always", "y", "yes"}:
            return "always"
        if answer in {"never", "n", "no"}:
            return "never"
        if answer == "ask":
            return "ask"
        print("  Enter ask/always/never or y/n.")


def walk_settings(settings: OrderedDict[str, str]) -> tuple[OrderedDict[str, str], bool]:
    """Let the user review and change persistent settings (mode is skipped)."""
    updated = OrderedDict(settings)
    print("Settings: answer each question; type q at any prompt to quit.")
    for key in SETTING_KEYS:
        if key == "mode":
            continue
        if key == "assist_mode":
            updated[key] = prompt_toggle(
                "assist_mode",
                "Optional copy-paste LLM help during the interview (default on).",
                updated[key],
            )
        elif key == "copy_to_clipboard":
            updated[key] = prompt_copy_setting(updated[key])
        elif key == "backup_mode":
            updated[key] = prompt_backup_mode(updated[key])
        elif key in PATH_SETTINGS:
            updated[key] = prompt_path(key, FOLDER_EXPLANATIONS.get(key, ""), updated[key])
        else:
            raise RuntimeError(f"unsupported setting key: {key}")
    return updated, updated != settings


def resolve_mode(answer: str) -> str | None:
    """Translate a mode answer (digit, short key, or full name) into a mode."""
    table = {"1": "new script prompt", "n": "new script prompt", "2": "meta prompt", "m": "meta prompt"}
    if answer in MODES:
        return answer
    return table.get(answer)


def print_mode_menu() -> None:
    """Show the mode menu with explanations and command keys."""
    print("\nWhat should this run generate?")
    print(
        "  1 / n - new script prompt: a prompt that instructs an LLM to BUILD a new "
        "prompt-builder script for you."
    )
    print(
        "  2 / m - meta prompt: a reusable prompt that teaches an LLM HOW to build "
        "prompt-builder scripts in general (no specific script)."
    )
    print("  commands: r = reprint last prompt | s = settings | q = quit")


def ask_mode(
    settings: OrderedDict[str, str],
    history: list[RunEntry],
    sessions: list[SessionEntry],
    drafts: "dict[str, RunEntry]",
    devlog: list[str],
    reprint_source: RunEntry | None,
) -> str:
    """Ask for the run mode or handle the r/s commands (q quits via read_line).

    The r/last reprint reproduces the active session's most recent completed
    prompt.
    """
    print_mode_menu()
    while True:
        answer = read_line(f"  mode [{settings['mode']}]: ").strip().lower()
        value = settings["mode"] if answer == "" else answer
        if value in {"s", "settings"}:
            new_settings, _changed = walk_settings(settings)
            settings.clear()
            settings.update(new_settings)
            write_log(settings, history, sessions, drafts, devlog)
            print_mode_menu()
            continue
        if value in {"r", "last"}:
            if reprint_source is None:
                print("No previous run in this session to reprint.")
                continue
            output = build_output_safely(settings, reprint_source)
            if output is None:
                raise SystemExit(1)
            emit_output(settings, output)
            raise SystemExit(0)
        resolved = resolve_mode(value)
        if resolved is not None:
            settings["mode"] = resolved
            return resolved
        print("  Choose 1/n or 2/m, or r / s / q.")


def pick_session(sessions: list[SessionEntry]) -> SessionEntry:
    """List saved sessions newest-first and let the user pick one by number."""
    ordered = list(reversed(sessions))
    print("\nSaved sessions (newest first):")
    for number, session in enumerate(ordered, start=1):
        print(f"  {number}. {session.name} [{session.root}] (created {session.created})")
    while True:
        answer = read_line(f"  pick a session 1-{len(ordered)} [1] (q=quit): ").strip().lower()
        if answer == "":
            return ordered[0]
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(ordered):
                return ordered[index - 1]
        print("  Enter a listed number.")


def choose_session(
    settings: OrderedDict[str, str],
    history: list[RunEntry],
    sessions: list[SessionEntry],
    drafts: "dict[str, RunEntry]",
    devlog: list[str],
) -> SessionEntry:
    """Choose the active session: Continue a saved one, or start a named New one.

    A New session generates a root hash and is written to the log immediately,
    so it persists even if the user quits before completing a run.
    """
    if sessions:
        choice = prompt_menu(
            "Start",
            "Continue a saved session or start a new one.",
            (
                ("continue", ("c",), "resume a saved session and its answers"),
                ("new", ("n",), "start a fresh session"),
            ),
            "continue",
        )
        if choice == "continue":
            return pick_session(sessions)
    name = confirm_or_enter(
        "Name this session",
        "A short label so you can find it later in Continue.",
        "untitled session",
    )
    session = SessionEntry(
        created=datetime.now().strftime("%Y-%m-%d %H:%M"), root=make_root_hash(), name=name
    )
    sessions.append(session)
    write_log(settings, history, sessions, drafts, devlog)
    print(f"Started session '{session.name}' [{session.root}].")
    return session


def ask_llm_target(default: str, allow_back: bool = False) -> str | object:
    """Ask where the generated prompt will run: extension or online LLM."""
    return prompt_menu(
        "Where will you run the generated prompt?",
        "This changes whether the prompt may reference your repo files.",
        (
            (
                "extension",
                ("e",),
                "a VS Code AI extension (Claude or Codex) that can read repo files",
            ),
            (
                "online",
                ("o",),
                "an online LLM (ChatGPT, Claude web, ...) that cannot see your files",
            ),
        ),
        default,
        allow_back=allow_back,
    )


def ask_llm_name(
    llm_target: str, current_name: str, session_last: RunEntry | None, allow_back: bool = False
) -> str | object:
    """Ask which extension (Claude/Codex) or the online LLM's name."""
    if llm_target == "extension":
        default = current_name if current_name in EXTENSION_NAMES else (
            session_last.llm_name if session_last and session_last.llm_name in EXTENSION_NAMES else "Claude"
        )
        return prompt_menu(
            "Which VS Code AI extension?",
            "Only Claude and Codex-like extensions are supported; this selects the memory file the prompt follows.",
            (
                ("Claude", ("c",), "follows CLAUDE.md by absolute path"),
                ("Codex", ("x",), "follows AGENTS.md and the .agents folder"),
            ),
            default,
            allow_back=allow_back,
        )
    default = current_name or (session_last.llm_name if session_last and session_last.llm_name else DEFAULT_ONLINE_NAME)
    return confirm_or_enter(
        "Name of the online LLM", "Only used to address the prompt.", default, allow_back=allow_back
    )


def default_for_field(attr: str, llm_target: str, default: str) -> str:
    """Pick a field default, choosing the target-specific consumers default."""
    if attr == "consumers_next_action":
        return DEFAULT_CONSUMERS_ONLINE if llm_target == "online" else DEFAULT_CONSUMERS_EXTENSION
    return default


def risk_menu(default: str, allow_back: bool = False) -> str | object:
    """Ask the cost-of-error level via a friendly menu."""
    return prompt_menu(
        "How costly is a mistake here?",
        "Controls how much the receiving LLM checks with you before acting.",
        (
            ("low", ("l",), "proceed with checkpoints"),
            ("medium", ("m",), "confirm key decisions first"),
            ("high", ("h",), "confirm every decision and propose guardrails"),
        ),
        default,
        allow_back=allow_back,
    )


def print_interview_warning() -> None:
    """Warn about multi-line pastes and remind the user of m/b/q."""
    print(
        "\nAnswering tips: type m for a multi-line answer (paste, then a line with only END). "
        "Type b to go back to the previous question, q to quit."
    )
    print(
        "Note: pasting several lines directly will spill into the next questions - use m for that."
    )


def ask_interview_step(
    key: str, answers: dict[str, str], session_last: RunEntry | None
) -> str | object:
    """Ask one interview step by key; returns the value or the BACK sentinel."""
    if key == "llm_target":
        default = answers.get("llm_target") or (
            session_last.llm_target if session_last and session_last.llm_target in LLM_TARGETS else "extension"
        )
        return ask_llm_target(default, allow_back=True)
    if key == "llm_name":
        return ask_llm_name(answers["llm_target"], answers.get("llm_name", ""), session_last, allow_back=True)
    if key == "risk_level":
        default = answers.get("risk_level") or (
            session_last.risk_level if session_last and session_last.risk_level in RISK_LEVELS else DEFAULT_RISK
        )
        return risk_menu(default, allow_back=True)
    label, explanation, field_default = FIELD_META[key]
    if answers.get(key):
        current = answers[key]
    elif session_last is not None and getattr(session_last, key):
        current = getattr(session_last, key)
    else:
        current = default_for_field(key, answers["llm_target"], field_default)
    return confirm_or_enter(label, explanation, current, allow_back=True)


def run_interview(mode: str, session_last: RunEntry | None, persist) -> dict[str, str]:
    """Run the navigable interview (b goes back) and return the answers dict.

    Steps: llm_target, llm_name, then (for new-script mode) each descriptor and
    the risk level. Going forward through already-answered steps shows the
    prior answer as the default, so corrections are quick. `persist(answers)`
    is called after every answer so the session's draft is always current.
    """
    print_interview_warning()
    steps = ["llm_target", "llm_name"]
    if mode == "new script prompt":
        steps += [attr for attr, _l, _e, _d in INTERVIEW_FIELDS] + ["risk_level"]
    answers: dict[str, str] = {}
    index = 0
    while index < len(steps):
        result = ask_interview_step(steps[index], answers, session_last)
        if result is BACK:
            if index == 0:
                print("Already at the first question.")
                continue
            index -= 1
            continue
        answers[steps[index]] = result
        index += 1
        persist(answers)
    return answers


def entry_from_answers(answers: dict[str, str], mode: str, timestamp: str, session: str) -> RunEntry:
    """Build an entry from a complete interview containing target, name, and every known field."""
    return RunEntry(
        timestamp=timestamp,
        mode=mode,
        session=session,
        llm_target=answers["llm_target"],
        llm_name=answers["llm_name"],
        **{attr: answers[attr] for attr in KNOWN_FIELDS},
    )


def draft_from_answers(
    answers: dict[str, str], mode: str, session: str, fallback: RunEntry | None
) -> RunEntry:
    """Build a draft RunEntry from possibly-partial answers.

    Not-yet-answered fields are filled from the prior draft/run (fallback) or
    the built-in defaults, so the draft is always a valid, complete entry that
    can be saved and later reproduced.
    """

    def pick(attr: str, default: str) -> str:
        """Return the answer for attr, else the fallback's value, else default."""
        value = answers.get(attr)
        if value:
            return value
        if fallback is not None and getattr(fallback, attr, ""):
            return getattr(fallback, attr)
        return default

    return RunEntry(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
        mode=mode,
        session=session,
        llm_target=pick("llm_target", "extension"),
        llm_name=pick("llm_name", "Claude"),
        driving_decision=pick("driving_decision", DEFAULT_DECISION),
        task=pick("task", DEFAULT_TASK),
        audience=pick("audience", DEFAULT_AUDIENCE),
        consumers_next_action=pick("consumers_next_action", DEFAULT_CONSUMERS_EXTENSION),
        prompt_output=pick("prompt_output", DEFAULT_PROMPT_OUTPUT),
        source_refs=pick("source_refs", DEFAULT_SOURCE_REFS),
        inputs=pick("inputs", DEFAULT_INPUTS),
        log_behavior=pick("log_behavior", DEFAULT_LOG_BEHAVIOR),
        target_location=pick("target_location", DEFAULT_LOCATION),
        constraints=pick("constraints", DEFAULT_CONSTRAINTS),
        done_looks_like=pick("done_looks_like", DEFAULT_DONE),
        verification_signal=pick("verification_signal", DEFAULT_VERIFICATION),
        risk_level=pick("risk_level", DEFAULT_RISK),
    )


def meta_run_entry(timestamp: str, session: str, llm_target: str, llm_name: str) -> RunEntry:
    """Build the fixed RunEntry used by meta prompt mode."""
    consumers = (
        DEFAULT_CONSUMERS_ONLINE if llm_target == "online" else DEFAULT_CONSUMERS_EXTENSION
    )
    return RunEntry(
        timestamp=timestamp,
        mode="meta prompt",
        session=session,
        llm_target=llm_target,
        llm_name=llm_name,
        driving_decision="give any LLM a reusable workflow for building prompt-builder scripts",
        task="create a reusable Kaizen (SAVMI) prompt-script workflow prompt",
        audience="an LLM helping build prompt-generation scripts",
        consumers_next_action=consumers,
        prompt_output="a reusable operating prompt",
        source_refs=DEFAULT_SOURCE_REFS,
        inputs=DEFAULT_INPUTS,
        log_behavior=DEFAULT_LOG_BEHAVIOR,
        target_location="ask after repo inspection",
        constraints=DEFAULT_CONSTRAINTS,
        done_looks_like=DEFAULT_DONE,
        verification_signal=DEFAULT_VERIFICATION,
        risk_level=DEFAULT_RISK,
    )


def read_pasted_block(expect_json: bool = False) -> str:
    """Read a pasted reply and return its text, finishing the way users paste.

    When expect_json is True (the sharpen reply is a JSON object), the block
    finishes the instant the captured text parses as JSON, so pasting it on one line or many
    completes with no terminator; an initial blank line also finishes and internal blank lines are
    preserved. Otherwise (a free-text critique that may contain blank lines)
    it finishes only on a terminator line. In every mode a line equal to END
    (case-insensitive) or the legacy <<END>>, or EOF, also finishes. Caps total
    size; reads literal text, so q is not treated as quit.
    """
    if expect_json:
        print(
            "Paste the LLM reply; it finishes automatically once the JSON is complete "
            "(or type END)."
        )
    else:
        print(f"Paste the LLM reply, then a line containing only {MULTILINE_SENTINEL} to finish:")
    collected: list[str] = []
    size = 0
    while True:
        try:
            line = input()
        except EOFError:
            break
        stripped = line.strip()
        if stripped.upper() == MULTILINE_SENTINEL or stripped == PASTE_SENTINEL:
            break
        if expect_json and stripped == "" and not collected:
            break
        line_size = len(line) + 1
        if size + line_size > PASTE_CAP:
            remaining = max(0, PASTE_CAP - size - 1)
            if remaining:
                collected.append(line[:remaining])
            print("Reply too large; using what was captured so far.")
            break
        size += line_size
        collected.append(line)
        if expect_json and stripped.endswith("}") and captured_json_is_complete("\n".join(collected)):
            break
    return "\n".join(collected)


def extract_json_text(block: str) -> str | None:
    """Pull the most likely JSON object text out of a pasted reply."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", block, re.DOTALL)
    if fence:
        return fence.group(1)
    start = block.find("{")
    if start == -1:
        return None
    depth = 0
    for index in range(start, len(block)):
        char = block[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return block[start : index + 1]
    return block[start:]  # unbalanced; let the repair layer try


def captured_json_is_complete(block: str) -> bool:
    """Return True only when the captured object is complete strict JSON, before repair is attempted."""
    raw = extract_json_text(block)
    if raw is None:
        return False
    try:
        return isinstance(json.loads(raw), dict)
    except json.JSONDecodeError:
        return False


def preclean_json(text: str) -> str:
    """Best-effort stdlib cleanup of common LLM JSON quirks."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    cleaned = (
        cleaned.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    )
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)  # trailing commas
    return cleaned


def parse_assist_response(block: str) -> dict | None:
    """Parse a pasted reply into a dict, or None when no JSON can be recovered.

    Pipeline: extract JSON -> stdlib json -> tolerant pre-clean -> json_repair
    salvage (if installed). Returns None when every layer fails.
    """
    raw = extract_json_text(block)
    if raw is None:
        return None
    for candidate in (raw, preclean_json(raw)):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            obj = None
        if isinstance(obj, dict):
            return obj
    if json_repair is not None:
        try:
            obj = json_repair.loads(raw)
        except Exception:  # pragma: no cover - json_repair is intentionally permissive
            obj = None
        if isinstance(obj, dict) and obj:
            return obj
    return None


def normalize_field_key(key: str) -> str | None:
    """Map a suggested key to a known field by exact or close fuzzy match."""
    candidate = key.strip().lower().replace(" ", "_")
    if candidate in KNOWN_FIELDS:
        return candidate
    match = difflib.get_close_matches(candidate, KNOWN_FIELDS, n=1, cutoff=0.8)
    return match[0] if match else None


def map_suggestions(raw_suggestions: object) -> "OrderedDict[str, str]":
    """Turn a suggested_answers object into known-field -> cleaned value."""
    mapped: "OrderedDict[str, str]" = OrderedDict()
    if not isinstance(raw_suggestions, dict):
        return mapped
    for key, value in raw_suggestions.items():
        attr = normalize_field_key(str(key))
        if attr is None:
            print(f"(ignored unrecognized suggestion key: {key})")
            continue
        mapped[attr] = clean_for_log(str(value))
    return mapped


def compose_interview_assist_request(answers: dict[str, str]) -> str:
    """Compose the sharpen-answers request body for any external LLM."""
    current = "\n".join(f"- {attr}: {answers[attr]}" for attr in KNOWN_FIELDS)
    schema_fields = ",\n    ".join(f'"{attr}": "..."' for attr in KNOWN_FIELDS)
    return (
        "You are helping refine the inputs to a prompt-builder generator.\n\n"
        f"{KAIZEN_SUMMARY}\n\n"
        "The user wants to build a new interactive prompt-builder script. Current draft answers:\n"
        f"{current}\n\n"
        "Reply with ONLY one fenced json block in exactly this shape:\n"
        "```json\n"
        "{\n"
        '  "suggested_answers": {\n    ' + schema_fields + "\n  },\n"
        '  "decisions_to_confirm": ["..."],\n'
        '  "recommended_capabilities": ["..."]\n'
        "}\n"
        "```\n"
        "1. In suggested_answers, give sharper, more complete values ONLY for fields you would change (omit the others).\n"
        "2. In decisions_to_confirm, list key decisions or assumptions the user should confirm.\n"
        "3. In recommended_capabilities, list robustness and UX capabilities this prompt-builder should include.\n"
        "Return only the json block, nothing else."
    )


def compose_critique_assist_request(draft_prompt: str) -> str:
    """Compose the critique request: review the draft, return JSON field edits.

    Uses the same JSON contract as the sharpen request so the reply can be
    parsed and applied identically (and auto-finishes on the JSON close).
    """
    schema_fields = ",\n    ".join(f'"{attr}": "..."' for attr in KNOWN_FIELDS)
    return (
        "You are reviewing a draft build-prompt that will instruct an LLM to create an "
        "interactive prompt-builder script.\n\n"
        f"{KAIZEN_SUMMARY}\n\n"
        "Critique the draft below for gaps, missing robustness or UX, unclear acceptance "
        "criteria, or anything that would force the user into many follow-up iterations. "
        "Then return improved input values that address your critique.\n\n"
        "=== DRAFT START ===\n"
        f"{draft_prompt}\n"
        "=== DRAFT END ===\n\n"
        "Reply with ONLY one fenced json block in exactly this shape:\n"
        "```json\n"
        "{\n"
        '  "suggested_answers": {\n    ' + schema_fields + "\n  },\n"
        '  "decisions_to_confirm": ["..."],\n'
        '  "recommended_capabilities": ["..."]\n'
        "}\n"
        "```\n"
        "1. In suggested_answers, give improved values ONLY for fields your critique would change (omit the others).\n"
        "2. In decisions_to_confirm, list your critique points and key decisions the user should confirm.\n"
        "3. In recommended_capabilities, list robustness and UX capabilities the script should include.\n"
        "Return only the json block, nothing else."
    )


def emit_assist_request(settings: OrderedDict[str, str], request_body: str) -> None:
    """Copy the assist request to the clipboard (when enabled) and print it."""
    if is_enabled(settings["copy_to_clipboard"]):
        copied, message = copy_to_clipboard(request_body)
        if copied:
            print("Copied assist request to clipboard.")
        else:
            print(f"Clipboard copy skipped: {message}.")
    print(f"\n{ASSIST_OPEN}\n{request_body}\n{ASSIST_CLOSE}")


def apply_sharpen_response(
    answers: dict[str, str], block: str, events: list[str], kind: str = "sharpen"
) -> dict[str, str]:
    """Ingest a sharpen/critique reply and let the user accept/edit each suggestion."""
    if not block.strip():
        print("No reply captured.")
        events.append(f"{kind} round: empty reply")
        return answers
    if ASSIST_OPEN in block:
        print("That looks like the request, not the reply. Try again.")
        events.append(f"{kind} round: request pasted back")
        return answers

    parsed = parse_assist_response(block)
    if parsed is None:
        print("\nCould not read JSON from the reply; showing it as advice only:\n")
        print(block)
        events.append(f"{kind} round: unparseable reply -> advisory only")
        return answers

    for decision in parsed.get("decisions_to_confirm", []) or []:
        print(f"decision to confirm: {decision}")
    for capability in parsed.get("recommended_capabilities", []) or []:
        print(f"recommended capability: {capability}")

    suggestions = map_suggestions(parsed.get("suggested_answers"))
    if not suggestions:
        print("No recognized field suggestions in the reply.")
        events.append(f"{kind} round: no field suggestions (advisory only)")
        return answers

    accepted: list[str] = []
    edited: list[str] = []
    for attr, suggested in suggestions.items():
        if attr == "risk_level":
            default = suggested if suggested in RISK_LEVELS else answers.get("risk_level", DEFAULT_RISK)
            new_value = risk_menu(default)
        else:
            new_value = confirm_or_enter(
                f"{LABEL_BY_ATTR[attr]} (LLM-suggested)", "", suggested
            )
        answers[attr] = new_value
        (accepted if new_value == suggested else edited).append(attr)
    events.append(
        f"{kind} round: suggested {list(suggestions)}; accepted {accepted}; edited {edited}"
    )
    return answers


def record_assist_pass(
    devlog: list[str], session_root: str, kind: str, pass_no: int, block: str
) -> None:
    """Append a one-line record of one assist pass reply to the development log.

    The reply is stored as compact JSON (field values / critique notes only -
    never the build-prompt) so each pass can be reviewed later, even if the run
    is later interrupted. Single-line so it round-trips in the line-based zone.
    """
    parsed = parse_assist_response(block)
    if isinstance(parsed, dict):
        reply = json.dumps(parsed, ensure_ascii=False)
    else:
        reply = f"reply-unparsed ({len(block)} chars)"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    devlog.append(
        f"[{timestamp}] session={session_root} | assist {kind} pass {pass_no} | reply={reply}"
    )


def run_assist_pass(
    settings: OrderedDict[str, str],
    answers: dict[str, str],
    kind: str,
    history: list[RunEntry],
    events: list[str],
    devlog: list[str],
    active: SessionEntry,
    persist,
    pass_no: int,
) -> dict[str, str]:
    """Run one sharpen/critique pass: emit request, read JSON, apply, log, persist."""
    if kind == "sharpen":
        request = compose_interview_assist_request(answers)
    else:
        draft_entry = entry_from_answers(
            answers, "new script prompt", datetime.now().strftime("%Y-%m-%d %H:%M"), ""
        )
        request = compose_critique_assist_request(
            build_prompt(settings, draft_entry)
        )
    emit_assist_request(settings, request)
    block = read_pasted_block(expect_json=True)
    answers = apply_sharpen_response(answers, block, events, kind=kind)
    record_assist_pass(devlog, active.root, kind, pass_no, block)
    persist(answers)
    return answers


def run_assist_loop(
    settings: OrderedDict[str, str],
    answers: dict[str, str],
    history: list[RunEntry],
    events: list[str],
    devlog: list[str],
    active: SessionEntry,
    persist,
) -> dict[str, str]:
    """Run the optional copy-paste LLM-assist loop, returning final answers.

    Both sharpen and critique passes return JSON field edits; after each, the
    user is offered another pass with a different LLM, carrying improvements
    forward. Every pass is saved to the development log and to the session draft
    (via persist), so multi-LLM work is reviewable and never lost.
    """
    print(
        "\nLLM assist (optional): copy a request, paste it into any LLM, paste the reply back."
    )
    events.append("assist loop entered")
    pass_no = 0
    while True:
        choice = prompt_menu(
            "assist",
            "Get LLM help refining your answers, or skip to generate.",
            (
                ("sharpen", ("a",), "ask an LLM to sharpen your answers"),
                ("critique", ("c",), "ask an LLM to critique the draft and return edits"),
                ("skip", ("s",), "skip and generate now"),
            ),
            "skip",
        )
        if choice == "skip":
            events.append("assist: skipped to generate")
            return answers
        kind = "sharpen" if choice == "sharpen" else "critique"
        pass_no += 1
        answers = run_assist_pass(
            settings, answers, kind, history, events, devlog, active, persist, pass_no
        )
        while ask_yes_no(
            f"Run another {kind} pass with a different LLM for more development?", "n"
        ):
            pass_no += 1
            events.append(f"assist: another {kind} pass (different LLM)")
            answers = run_assist_pass(
                settings, answers, kind, history, events, devlog, active, persist, pass_no
            )


def ask_run(
    settings: OrderedDict[str, str],
    history: list[RunEntry],
    sessions: list[SessionEntry],
    drafts: "dict[str, RunEntry]",
    devlog: list[str],
    events: list[str],
    active: SessionEntry,
    defaults_source: RunEntry | None,
    reprint_source: RunEntry | None,
) -> RunEntry:
    """Ask the run questions (navigable, target-aware, optional assist) -> RunEntry.

    Every answer is saved to the active session's draft as it is entered, via a
    persist callback, so nothing is lost if the run is interrupted.
    """
    mode = ask_mode(settings, history, sessions, drafts, devlog, reprint_source)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    def persist(current_answers: dict[str, str]) -> None:
        """Save the current answers as the active session's draft, immediately."""
        drafts[active.root] = draft_from_answers(current_answers, mode, active.root, defaults_source)
        write_log(settings, history, sessions, drafts, devlog)

    persist({})  # record the chosen mode immediately under the session

    answers = run_interview(mode, defaults_source, persist)

    if mode == "meta prompt":
        run = meta_run_entry(timestamp, active.root, answers["llm_target"], answers["llm_name"])
        events.append("meta prompt generated (assist not applied)")
    else:
        if is_enabled(settings["assist_mode"]):
            answers = run_assist_loop(
                settings, answers, history, events, devlog, active, persist
            )
        run = entry_from_answers(answers, mode, timestamp, active.root)

    print(
        f"using: session {active.name} [{active.root}] | target {run.llm_target} ({run.llm_name}) | "
        f"assist {settings['assist_mode']} | clipboard {settings['copy_to_clipboard']} | "
        f"backup {settings['backup_mode']}"
    )
    return run


def to_repo_relative_path(value: str) -> str:
    """Convert an absolute or relative path setting into a repo-relative path."""
    raw = value.strip()
    if not raw:
        return "."
    path = Path(raw)
    if path.is_absolute():
        try:
            relative = path.resolve().relative_to(REPO_ROOT)
        except ValueError:
            relative = Path(os.path.relpath(path, REPO_ROOT))
    else:
        relative = path
    return relative.as_posix()


def join_repo_relative_path(folder: str, filename: str) -> str:
    """Join a configured folder and file name as a repo-relative path."""
    base = to_repo_relative_path(folder)
    parts = [part for part in base.replace("\\", "/").split("/") if part and part != "."]
    return "/".join((parts or ["."]) + [filename])


def filename_stamp(timestamp: str) -> str:
    """Convert a history timestamp into a filename-safe stamp.

    "2026-06-13 14:10" becomes "20260613-1410". Deriving the stamp from the
    stored run timestamp (not the wall clock) keeps the archive/result-log
    filenames reproducible when `last` rebuilds a previous prompt.
    """
    return timestamp.replace("-", "").replace(":", "").replace(" ", "-")


def build_workflow_paths(settings: OrderedDict[str, str], run: RunEntry) -> tuple[str, str]:
    """Build repo-relative prompt archive and result-log paths (extension mode)."""
    stem = f"{filename_stamp(run.timestamp)}-{SCRIPT_STEM}"
    prompt_path = join_repo_relative_path(settings["prompt_archive_folder"], f"{stem}.md")
    result_path = join_repo_relative_path(settings["result_log_folder"], f"{stem}-results.md")
    return prompt_path, result_path


def build_prompt(settings: OrderedDict[str, str], run: RunEntry) -> str:
    """Render the paste-ready prompt for the run's mode and LLM target."""
    if run.mode == "meta prompt":
        if run.llm_target == "online":
            return META_ONLINE_TEMPLATE.format(
                llm_name=run.llm_name, kaizen_summary=KAIZEN_SUMMARY
            )
        memory_file_abs = str(REPO_ROOT / memory_file_for(run.llm_name))
        return META_EXTENSION_TEMPLATE.format(
            llm_name=run.llm_name,
            memory_file_abs=memory_file_abs,
            kaizen_summary=KAIZEN_SUMMARY,
        )

    fields = {
        "llm_name": run.llm_name,
        "driving_decision": run.driving_decision,
        "task": run.task,
        "audience": run.audience,
        "consumers_next_action": run.consumers_next_action,
        "prompt_output": run.prompt_output,
        "source_refs": run.source_refs,
        "inputs": run.inputs,
        "log_behavior": run.log_behavior,
        "constraints": run.constraints,
        "risk_level": run.risk_level,
        "risk_guidance": RISK_GUIDANCE.get(run.risk_level, RISK_GUIDANCE[DEFAULT_RISK]),
        "done_looks_like": run.done_looks_like,
        "verification_signal": run.verification_signal,
        "kaizen_summary": KAIZEN_SUMMARY,
    }
    if run.llm_target == "online":
        return NEW_SCRIPT_ONLINE_TEMPLATE.format(**fields)
    memory_file_abs = str(REPO_ROOT / memory_file_for(run.llm_name))
    prompt_archive_path, result_log_path = build_workflow_paths(settings, run)
    return NEW_SCRIPT_EXTENSION_TEMPLATE.format(
        memory_file_abs=memory_file_abs,
        target_location=run.target_location,
        prompt_archive_path=prompt_archive_path,
        result_log_path=result_log_path,
        **fields,
    )


def build_output(settings: OrderedDict[str, str], run: RunEntry) -> str:
    """Wrap the generated prompt in terminal markers and verify it."""
    prompt = build_prompt(settings, run)
    verify_prompt(prompt, run.mode, run.llm_target, run.llm_name)
    return "\n".join((OPENING_MARKER, prompt, CLOSING_MARKER))


def build_output_safely(settings: OrderedDict[str, str], run: RunEntry) -> str | None:
    """Build the output, or report a verification failure without crashing."""
    try:
        return build_output(settings, run)
    except RuntimeError as exc:
        print(f"Prompt failed verification and was not emitted: {exc}")
        return None


def verify_prompt(prompt: str, mode: str, llm_target: str, llm_name: str) -> None:
    """Raise RuntimeError if the generated prompt is wrong for its target.

    Online prompts must be self-contained (no repo-file references); extension
    prompts must name the memory file they follow.
    """
    lower_prompt = prompt.lower()
    required = [
        "savmi",
        "scope:",
        "adapt:",
        "verify:",
        "manage:",
        "improve:",
        "acceptance criteria",
        "self-contained",
        "docstrings",
        "verification",
    ]
    if mode == "new script prompt":
        required += ["self-review", "problem it solves", "cost of error is"]
    if llm_target == "extension":
        required.append(memory_file_for(llm_name).lower())
        if mode == "new script prompt":
            required += ["inspect the repository", "ask the user where"]
    else:
        required.append("cannot access")

    missing = [item for item in required if item not in lower_prompt]
    if missing:
        raise RuntimeError("generated prompt missing required text: " + ", ".join(missing))

    if llm_target == "online":
        banned = [token for token in ONLINE_BANNED_TOKENS if token in lower_prompt]
        if banned:
            raise RuntimeError(
                "online prompt references repo-only items: " + ", ".join(banned)
            )
    if "[" in prompt or "]" in prompt:
        raise RuntimeError("generated prompt still contains bracket placeholders")


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    """Copy text on Windows; SetClipboardData success transfers HGLOBAL ownership to the clipboard."""
    if os.name != "nt":
        return False, "clipboard copy is only implemented for Windows"

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.restype = wintypes.HANDLE

    cf_unicode_text = 13
    gmem_moveable = 0x0002
    data = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(gmem_moveable, len(data))
    if not handle:
        return False, "GlobalAlloc failed"
    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        return False, "GlobalLock failed"
    ctypes.memmove(locked, data, len(data))
    kernel32.GlobalUnlock(handle)
    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        return False, "OpenClipboard failed"
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(cf_unicode_text, handle):
            kernel32.GlobalFree(handle)
            return False, "SetClipboardData failed"
    finally:
        user32.CloseClipboard()
    return True, ""


def clipboard_payload(output: str) -> str:
    """Return only the text between the terminal markers."""
    start = output.find(OPENING_MARKER)
    end = output.find(CLOSING_MARKER)
    if start == -1 or end == -1 or end <= start:
        return output
    return output[start + len(OPENING_MARKER):end].strip("\n")


def backup_filename(mode: str, timestamp: datetime | None = None) -> str:
    """Build the timestamped Markdown backup file name from the run mode."""
    stamp = (timestamp or datetime.now()).strftime("%Y%m%d-%H%M%S")
    slug = mode.replace(",", "").replace(" ", "-")
    return f"{stamp}-{slug}.md"


def save_prompt_backup(
    settings: OrderedDict[str, str], prompt_text_value: str, mode: str
) -> Path | None:
    """Optionally save the prompt backup based on the backup_mode setting."""
    backup_mode = settings["backup_mode"]
    if backup_mode == "never":
        return None
    folder = Path(settings["backup_folder"])
    should_save = backup_mode == "always"
    try:
        if backup_mode == "ask":
            should_save = ask_yes_no(f"Save a Markdown backup to {folder}?", "n")
        if not should_save:
            return None
        if not folder.exists():
            if not ask_yes_no(f"Backup folder does not exist: {folder}. Create it?", "n"):
                print("Backup not saved; folder was not created.")
                return None
            folder.mkdir(parents=True, exist_ok=True)
        path = folder / backup_filename(mode)
        path.write_text(prompt_text_value + "\n", encoding="utf-8")
    except (EOFError, QuitRequested):
        print("\nBackup skipped.")
        return None
    except OSError as exc:
        print(f"Backup not saved: {exc}")
        return None
    print(f"Saved prompt backup: {path}")
    return path


def pip_install(package: str) -> bool:
    """Install one package into the active interpreter with pip."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", package],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Could not run pip: {exc}")
        return False
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        print("pip install failed:")
        for line in tail:
            print(f"  {line}")
    return result.returncode == 0


def offer_optional_dependencies(settings: OrderedDict[str, str]) -> None:
    """Offer to install missing optional packages the assist loop would use.

    Only acts when the assist loop is on and a package is missing. On a
    non-interactive run it prints a one-line hint instead of prompting.
    """
    if not is_enabled(settings["assist_mode"]):
        return
    for import_name, pip_name, reason in OPTIONAL_DEPENDENCIES:
        if globals().get(import_name) is not None:
            continue
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print(f"Tip: 'pip install {pip_name}' for {reason}.")
            continue
        if not ask_yes_no(
            f"Optional package '{pip_name}' ({reason}) is not installed. Install it now?", "n"
        ):
            print(f"Continuing without {pip_name}; standard-library parsing will be used.")
            continue
        print(f"Installing {pip_name} ...")
        if pip_install(pip_name):
            try:
                globals()[import_name] = importlib.import_module(import_name)
                print(f"Installed {pip_name}.")
            except ImportError:
                print(f"Installed {pip_name}, but importing it still failed; using stdlib parsing.")
        else:
            print(f"Could not install {pip_name}; using standard-library parsing.")


def console_will_close_on_exit() -> bool:
    """Treat probe errors or at most two console processes (Python plus launcher) as one-click exit."""
    if os.name != "nt":
        return False
    try:
        process_list = (wintypes.DWORD * 8)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(process_list, 8)
    except (AttributeError, OSError):
        return True
    if count == 0:
        return True
    return count <= 2


def should_pause_before_exit() -> bool:
    """Return True when an interactive one-click launch should pause at exit."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if "vscode" in term_program:
        return False
    if os.environ.get("VSCODE_INJECTION") or os.environ.get("VSCODE_PID"):
        return False
    return console_will_close_on_exit()


def pause_before_exit_if_needed() -> None:
    """Keep one-click terminal windows open long enough for manual copying."""
    if should_pause_before_exit():
        try:
            input("Press Enter to exit.")
        except (EOFError, KeyboardInterrupt):
            pass


def emit_output(settings: OrderedDict[str, str], output: str) -> str:
    """Copy and print the prompt, returning the exact reusable payload."""
    payload = clipboard_payload(output)
    if is_enabled(settings["copy_to_clipboard"]):
        copied, message = copy_to_clipboard(payload)
        if copied:
            print("Copied generated prompt to clipboard.")
        else:
            print(f"Clipboard copy skipped: {message}.")
    print(output)
    return payload


def main() -> int:
    """Run the interactive script and return a process exit code."""
    settings, history, sessions, drafts, devlog, issues, first_run = read_log()
    events: list[str] = []
    if issues:
        print("Log had unparseable or missing values; built-in defaults were used where needed.")
    try:
        if first_run:
            print(f"No {LOG_PATH.name} found; first run setup will create it.")
            write_log(settings, history, sessions, drafts, devlog)
            settings, _changed = walk_settings(settings)
            write_log(settings, history, sessions, drafts, devlog)

        offer_optional_dependencies(settings)
        active = choose_session(settings, history, sessions, drafts, devlog)
        last_completed = next((e for e in reversed(history) if e.session == active.root), None)
        # Resume the session's latest answers: prefer the saved draft, else the
        # last completed run; reprint always uses a completed run.
        defaults_source = drafts.get(active.root) or last_completed
        run = ask_run(
            settings, history, sessions, drafts, devlog, events, active, defaults_source, last_completed
        )

        # Build (and verify) before logging so a failed build never records a
        # history line that would poison later `last` runs.
        output = build_output_safely(settings, run)
        if output is None:
            return 1

        history.append(run)
        drafts[active.root] = run  # the draft now reflects the completed run
        assist_on = is_enabled(settings["assist_mode"]) and run.mode == "new script prompt"
        if run.llm_target == "online":
            target_tag = "online"
            events.append("emitted (online; not archived to repo)")
        else:
            target_tag = f"extension:{run.llm_name}"
            prompt_archive_path, _ = build_workflow_paths(settings, run)
            events.append(f"emitted build-prompt archived at {prompt_archive_path}")
        devlog.extend(format_devlog_lines(len(history), run, assist_on, target_tag, events))
        write_log(settings, history, sessions, drafts, devlog)
        payload = emit_output(settings, output)
        save_prompt_backup(settings, payload, run.mode)
        return 0
    except QuitRequested:
        print("\nQuit; the log remains valid.")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted; log remains valid.")
        return 130
    except EOFError:
        print("\nInput ended unexpectedly; no run was recorded and the log remains valid.")
        return 1
    finally:
        pause_before_exit_if_needed()


if __name__ == "__main__":
    raise SystemExit(main())
