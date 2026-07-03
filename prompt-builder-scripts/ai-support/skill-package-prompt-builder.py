"""Build paste-ready prompts that make a VS Code extension LLM author a deeply-digested skill package.

This interactive, standard-library-only script interviews you and assembles a
single build-prompt you paste into Codex or Claude. That prompt drives the
extension LLM to research the sources you registered and rewrite them into
concise, attributed, LLM-optimized Markdown under a skill's ``references/``
area — not raw dumps. The script never calls an LLM and never touches the
network; it only interviews, records, validates, and assembles the prompt.

Design mirrors the sibling ``generate-prompt-for-new-script.py``: a 5-zone
sibling ``.log`` (SETTINGS, SESSIONS, SESSION DRAFTS, RUN HISTORY, DEVELOPMENT
LOG), named sessions with New/Continue, draft persistence so a quit-partway
interview resumes exactly, an optional copy-paste LLM-assist loop, clipboard
copy with a printed fallback, and verify-before-emit. The genuinely new piece
is an editable source registry whose contents survive quit/Continue.
"""

from __future__ import annotations

import argparse
import base64
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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

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


LOG_PATH = Path(__file__).with_suffix(".log")
SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT_STEM = Path(__file__).stem
SCRIPT_VERSION = "0.1.0"


class QuitRequested(Exception):
    """Raised when the user types q/quit at any prompt to exit cleanly."""


class CliError(Exception):
    """Raised for noninteractive CLI errors that should print cleanly and exit non-zero."""


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

# Single generate mode kept so the mode menu can still host the r/s/q commands
# and so RUN HISTORY lines stay forward-compatible with the sibling format.
MODES = ("skill build prompt",)
BACKUP_MODES = ("ask", "always", "never")
RISK_LEVELS = ("low", "medium", "high")
ASSIST_MODES = ("on", "off")
TARGET_AGENTS = ("Codex", "Claude", "both")
SOURCE_TYPES = ("official-docs", "local-file", "url", "example", "api", "tutorial", "note")
PRIORITIES = ("high", "medium", "low")
TRUST_LEVELS = ("official", "verified", "community", "unverified")
# License posture of the registered sources, taken collectively. Drives whether the
# generated prompt uses a digest-and-attribute model (safe for permissive/owned text)
# or an inspiration-only original-authoring model (required for copyleft/unknown text,
# the lesson from recontextualizing the copyleft blender/lumberyard mirrors).
SOURCE_LICENSES = ("permissive-or-owned", "copyleft-or-restricted", "unknown")
BUILD_INTENTS = ("new", "update")
# Agent-parallelism mode for the generated prompt. Offered only when a registered source is large
# (a URL doc-site, a big/unknown-page PDF, or a big local file). "none" = sequential build;
# "generic" = model-agnostic parallel subagents (used for Codex / non-Claude executors);
# "haiku"/"opus" = Claude subagents launched with that model (Haiku = fast/cheap bulk fetch+digest,
# Opus = strongest reasoning). The orchestrator always owns writes/ledger/verify (no write races).
PARALLELISM_MODES = ("none", "generic", "haiku", "opus")

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
            "writing files; do not proceed on unconfirmed assumptions.",
        ),
        (
            "high",
            "require explicit user confirmation for every key decision, and "
            "propose tool-level guardrails (permission rules, hooks, or "
            "sandboxing) before touching anything hard to reverse.",
        ),
    )
)

# Each license posture switches the attribution model the generated prompt enforces.
# "digest" keeps source-map.json + per-file license_note (fine for permissive/owned text);
# "inspiration" forbids reproducing source text, drops source-map.json, and requires an
# `## Inspired by` originality section (the copyleft-safe posture). Unknown is treated as
# inspiration because it is the safe default.
LICENSE_MODE_BY_LICENSE = OrderedDict(
    (
        ("permissive-or-owned", "digest"),
        ("copyleft-or-restricted", "inspiration"),
        ("unknown", "inspiration"),
    )
)
LICENSE_GUIDANCE = OrderedDict(
    (
        (
            "permissive-or-owned",
            "Sources are permissively licensed or owned by the user: you MAY quote or closely "
            "paraphrase, but still digest rather than dump. Produce references/source-map.json "
            "and a per-file license_note.",
        ),
        (
            "copyleft-or-restricted",
            "Sources are copyleft or otherwise restricted: write ORIGINAL content informed by them, "
            "reproduce NO source sentences or structure, OMIT source-map.json/license_note, and add "
            "an `## Inspired by` section listing the sources as inspiration only.",
        ),
        (
            "unknown",
            "Source licensing is unknown: treat it as restricted — write ORIGINAL content, reproduce "
            "NO source text, OMIT source-map.json/license_note, and add an `## Inspired by` section.",
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
        ("mode", "skill build prompt"),
        ("assist_mode", "on"),
        ("copy_to_clipboard", "yes"),
        ("backup_mode", "ask"),
        # Declared repo-relative (portable); resolved to absolute when written or emitted.
        ("backup_folder", "user/user-generated-prompts"),
        ("prompt_archive_folder", "user/prompts-given"),
        ("result_log_folder", "user/prompt-results-logs"),
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

# ---- Interview default values (real text, never placeholders) ----------------
DEFAULT_SKILL_NAME = "Example Skill"
DEFAULT_SKILL_SLUG = "example-skill"
DEFAULT_SKILL_PURPOSE = "give an agent focused, source-backed guidance for one specific task"
DEFAULT_TARGET_AGENTS = "both"
DEFAULT_TRIGGERS = (
    "when the user asks about the skill's topic by name or by describing the task, "
    "or opens files related to it"
)
DEFAULT_WORKFLOW = (
    "the agent loads SKILL.md, follows its steps, and reads only the reference files it needs"
)
DEFAULT_FILES = (
    "SKILL.md plus a references/ folder organized by topic (INDEX.md, topics.json, and one "
    "Markdown file per concern), and a ground-truth verifier script"
)
DEFAULT_DOC_DEPTH = (
    "each reference file at most about 600 words, chunked under H2/H3 headings, "
    "one example per concept, no raw dumps"
)
DEFAULT_VERIFICATION = (
    "every reference is listed in INDEX.md and topics.json and reachable from the SKILL.md task-router; "
    "the preserve-glossary is verified against the references; no unresolved placeholders; no invented facts"
)
DEFAULT_PRIVACY = (
    "do not copy secrets, tokens, credentials, or personal data into any file; "
    "redact sensitive values and note the redaction"
)
DEFAULT_OUT_OF_SCOPE = "anything not directly needed to perform the skill's one task"
DEFAULT_RISK = "high"
DEFAULT_SOURCE_LICENSE = "unknown"
DEFAULT_BUILD_INTENT = "new"
DEFAULT_PARALLELISM = "none"
SKILL_CATEGORIES = OrderedDict(
    (
        ("library-api-reference", "Library and API reference"),
        ("product-verification", "Product verification"),
        ("data-fetching-analysis", "Data fetching and analysis"),
        ("business-process-automation", "Business process and team automation"),
        ("code-scaffolding-templates", "Code scaffolding and templates"),
        ("code-quality-review", "Code quality and review"),
        ("ci-cd-deployment", "CI/CD and deployment"),
        ("runbook", "Runbook"),
        ("infrastructure-operations", "Infrastructure operations"),
    )
)
SKILL_CATEGORY_VALUES = tuple(SKILL_CATEGORIES)
MEMORY_FILE_POLICIES = ("assess-then-ask", "read-only", "edit-with-explicit-approval")
SUBAGENT_REVIEW_POLICIES = ("nontrivial", "high-risk-only", "optional", "none")
DEFAULT_SKILL_CATEGORY = "library-api-reference"
DEFAULT_EXAMPLE_TASKS = (
    "include one direct trigger task, one paraphrased trigger task that omits the skill name, "
    "and one adjacent negative-trigger task"
)
DEFAULT_NEGATIVE_TRIGGERS = "adjacent tasks that should use another skill or no skill at all"
DEFAULT_KNOWN_GOTCHAS = (
    "specific recurring failure modes the skill must prevent; avoid generic cautions"
)
DEFAULT_QUALITY_RUBRIC = (
    "good output triggers correctly, follows the workflow, uses the right references, "
    "asks missing setup questions, verifies with evidence, and rejects shallow or invented output"
)
DEFAULT_OBSERVATION_METHOD = "category-default"
DEFAULT_MEMORY_FILE_POLICY = "assess-then-ask"
DEFAULT_SUBAGENT_REVIEW_POLICY = "nontrivial"


def category_observation_default(category: str) -> str:
    """Return the default observation artifact for a skill category."""
    return {
        "library-api-reference": "runnable examples, import checks, command checks, or minimal API smoke tests",
        "product-verification": "Playwright runs, screenshots, traces, console logs, or rendered UI inspection",
        "data-fetching-analysis": "sample query results, dashboard snapshots, row counts, or reconciled totals",
        "business-process-automation": "completed checklist, generated artifact, approval record, or process log",
        "code-scaffolding-templates": "generated files plus compile/test output, snapshot comparison, or smoke run",
        "code-quality-review": "seeded bad examples, review findings, tests, and before/after diffs",
        "ci-cd-deployment": "dry-run output, CI status, deployment status, logs, or rollback evidence",
        "runbook": "collected evidence, command traces, timeline, or structured incident report",
        "infrastructure-operations": "dry-run plan, resource diff, metrics snapshot, or post-action verification",
    }.get(category, "task-specific tests, logs, traces, screenshots, sample runs, or fixtures")


def category_label(category: str) -> str:
    """Return a human-readable label for a skill category value."""
    return SKILL_CATEGORIES.get(category, category)


def resolved_observation_method(run: "RunEntry") -> str:
    """Return the observation method after applying category defaults."""
    raw = (run.observation_method or "").strip()
    if not raw or raw == DEFAULT_OBSERVATION_METHOD:
        return category_observation_default(run.skill_category)
    return raw

# (attribute, friendly label, one-line explanation, default) for every free-text
# interview field, in question order. Special steps (slug, target_agents,
# sources, risk_level) are handled separately by ask_interview_step.
INTERVIEW_FIELDS = (
    (
        "skill_name",
        "What is the skill's human-readable name?",
        "A short title for the skill (the YAML 'name' is derived from the slug, not this).",
        DEFAULT_SKILL_NAME,
    ),
    (
        "skill_purpose",
        "What does this skill do?",
        "The one task the skill exists to support, in a sentence.",
        DEFAULT_SKILL_PURPOSE,
    ),
    (
        "trigger_conditions",
        "When should an agent reach for this skill?",
        "Trigger-rich 'when to use' text, including phrases and file types that should invoke it.\n"
        "This becomes the pushy SKILL.md description, so be concrete.",
        DEFAULT_TRIGGERS,
    ),
    (
        "example_tasks",
        "What example tasks should prove the trigger behavior?",
        "Include one direct trigger, one paraphrased trigger, and one negative-trigger example.",
        DEFAULT_EXAMPLE_TASKS,
    ),
    (
        "negative_triggers",
        "What adjacent tasks should NOT use this skill?",
        "Names overlap boundaries and nearby work that belongs to another skill or a generic answer.",
        DEFAULT_NEGATIVE_TRIGGERS,
    ),
    (
        "user_workflow",
        "How will the agent use the skill at run time?",
        "The expected workflow the skill supports.",
        DEFAULT_WORKFLOW,
    ),
    (
        "files_to_generate",
        "Which files should the LLM create?",
        "Defaults to the mandated skill-package set; edit only if you need more.",
        DEFAULT_FILES,
    ),
    (
        "doc_depth",
        "How deep should each reference file be?",
        "Sets the per-file word/token budget and digestion style the prompt enforces.",
        DEFAULT_DOC_DEPTH,
    ),
    (
        "verification_requirements",
        "What must be true for the skill to count as done?",
        "The verification requirements the LLM must self-check.",
        DEFAULT_VERIFICATION,
    ),
    (
        "known_gotchas",
        "What recurring gotchas must the skill prevent?",
        "Concrete failure modes and how the skill should steer around them.",
        DEFAULT_KNOWN_GOTCHAS,
    ),
    (
        "quality_rubric",
        "How should good vs bad skill output be judged?",
        "The task-specific rubric the receiving agent must use before claiming success.",
        DEFAULT_QUALITY_RUBRIC,
    ),
    (
        "observation_method",
        "How should the agent observe output while building?",
        "Use category-default, or name tests/logs/screenshots/traces/sample runs/fixtures to inspect.",
        DEFAULT_OBSERVATION_METHOD,
    ),
    (
        "privacy_constraints",
        "Any privacy or secret-handling rules?",
        "How the LLM must handle secrets, credentials, and personal data.",
        DEFAULT_PRIVACY,
    ),
    (
        "out_of_scope",
        "What is explicitly out of scope?",
        "The EXCLUDE boundary - what the skill must NOT cover.",
        DEFAULT_OUT_OF_SCOPE,
    ),
)
# attr -> (label, explanation, default) for the navigable interview steps.
FIELD_META = {attr: (label, explain, default) for attr, label, explain, default in INTERVIEW_FIELDS}
LABEL_BY_ATTR = {attr: label for attr, label, _explain, _default in INTERVIEW_FIELDS}
LABEL_BY_ATTR["skill_slug"] = "skill slug"
LABEL_BY_ATTR["target_agents"] = "target agent(s)"
LABEL_BY_ATTR["risk_level"] = "risk level"
LABEL_BY_ATTR["source_license"] = "source license posture"
LABEL_BY_ATTR["build_intent"] = "build intent"
LABEL_BY_ATTR["parallelism"] = "agent parallelism"
LABEL_BY_ATTR["skill_category"] = "skill category"
LABEL_BY_ATTR["memory_file_policy"] = "memory-file policy"
LABEL_BY_ATTR["subagent_review_policy"] = "subagent review policy"
# Field attributes the assist loop may suggest values for. Slug and sources are
# excluded: the slug is validated separately, and sources are a structured
# registry, not free text.
KNOWN_FIELDS = tuple(attr for attr, _l, _e, _d in INTERVIEW_FIELDS) + (
    "target_agents", "risk_level", "source_license", "parallelism", "skill_category",
    "memory_file_policy", "subagent_review_policy",
)

OPENING_MARKER = "==== GENERATED PROMPT — paste into your LLM ===="
CLOSING_MARKER = "==== END GENERATED PROMPT ===="
ASSIST_OPEN = "==== ASSIST REQUEST — paste into any LLM, then paste the reply back ===="
ASSIST_CLOSE = "==== END ASSIST REQUEST ===="
PASTE_SENTINEL = "<<END>>"
PASTE_CAP = 100_000

KAIZEN_SYSTEM_SUMMARY = """Use the Kaizen System SAVMI loop as the operating model:
- Scope: research evidence, clarify user intent, name assumptions, and define acceptance criteria before editing.
- Adapt: make bounded changes through explicit execution contracts and deterministic scripts where practical.
- Verify: use deterministic checks first, then structured synthesis where judgment is required, and produce a clear go/no-go result.
- Manage: record tasks, plans, proof, artifacts, source locks, evals, and learning records through the local harness.
- Improve: use managed evidence to decide what should feed the next Scope cycle."""

# Fixed JSON shapes, stated verbatim in the generated prompt. Kept as plain
# (non f-string) constants so their braces never collide with str.format.
SOURCE_MAP_SCHEMA = """`references/source-map.json` is a JSON object: {"schema_version": 1, "entries": [ one entry per reference file ]}.
Because references are organized by topic, a single file may draw on MORE THAN ONE source, so each entry lists all sources it used.
Each entry MUST have exactly these keys and nothing else:
{
  "file": "references/<reference-file>.md",
  "sources": [
    {
      "source_label": "the human-readable label from the source registry",
      "source_type": "official-docs | local-file | url | example | api | tutorial | note",
      "source_location": "the URL or repo-relative/local path of the source",
      "retrieved_or_commit": "retrieval date YYYY-MM-DD for URLs, or a commit/path note for local files",
      "attribution": "who authored the source and any required credit",
      "license_note": "license or usage note; if you keep any raw excerpt, justify it here"
    }
  ]
}"""

TOPICS_SCHEMA = """`references/topics.json` is a JSON object: {"schema_version": 1, "topics": [ one entry per reference file ]}.
Each entry MUST have exactly these keys and nothing else:
{
  "topic": "short topic title",
  "file": "references/<reference-file>.md",
  "summary": "one or two sentences on what the file covers",
  "keywords": ["keyword-one", "keyword-two", "keyword-three"]
}"""

def _read_canonical_checker(filename: str) -> str:
    path = REPO_ROOT / ".agents" / "skills" / "skill-drafting" / "scripts" / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return "# bundled checker not available in this clone; write the fallback described above."


CANONICAL_CLAUDE_USAGE_CHECKER = _read_canonical_checker("claude_usage_check.py")
CANONICAL_CODEX_USAGE_CHECKER = _read_canonical_checker("codex_usage_check.py")


@dataclass
class SourceEntry:
    """One registered documentation source the LLM must digest into a reference.

    Stored inside RunEntry and serialized (base64-encoded JSON) into the log so
    the registry survives quit/Continue. ``content_path`` (for URL sources)
    optionally points at a pre-fetched local copy; otherwise the generated prompt
    instructs the executing agent to fetch the URL and cache it under AI/work.
    ``supply_at_draft`` is retained only for log back-compat and no longer gates
    anything. Local sources must exist on disk; URL sources are always usable.
    """

    label: str
    stype: str
    location: str
    priority: str = "medium"
    trust: str = "unverified"
    intended_use: str = ""
    content_path: str = ""
    supply_at_draft: bool = False
    approx_pages: int = 0

    def is_url(self) -> bool:
        """Return True when the source location is an http/https URL."""
        try:
            parsed = urlparse(self.location.strip())
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def is_pdf(self) -> bool:
        """Return True when the location points at a PDF (URL or local path)."""
        loc = self.location.strip().lower()
        for sep in ("?", "#"):
            if sep in loc:
                loc = loc.split(sep, 1)[0]
        return loc.endswith(".pdf")

    def is_usable(self) -> bool:
        """Return True when the LLM will have real content to digest.

        URL sources are always usable: the agent that runs the prompt fetches them. Local/path
        sources must exist on disk.
        """
        if self.is_url():
            return True
        return local_path_exists(self.location)

    def content_instruction(self, fetch_dir_abs: str = "") -> str:
        """Return the per-source content-handling line for the generated prompt.

        ``fetch_dir_abs`` is the absolute per-skill fetch cache (``AI/work/<slug>-docs``) where the
        agent saves fetched URL content before digesting.
        """
        if self.is_url():
            if self.content_path and local_path_exists(self.content_path):
                return (
                    f"pre-fetched content is at {abs_repo_path(self.content_path)}; digest from there, "
                    "and re-fetch the live URL only to fill gaps"
                )
            target = fetch_dir_abs or "the build's AI/work/<slug>-docs fetch cache"
            return (
                "fetch this URL and follow its relevant in-domain links deeply, save the extracted "
                f"notes under {target}/, then digest from your fetch; never invent content"
            )
        return f"read and digest the local file at {abs_repo_path(self.location)}"

    def to_dict(self) -> "OrderedDict[str, object]":
        """Return an ordered dict of this source for JSON serialization."""
        return OrderedDict(
            (
                ("label", self.label),
                ("stype", self.stype),
                ("location", self.location),
                ("priority", self.priority),
                ("trust", self.trust),
                ("intended_use", self.intended_use),
                ("content_path", self.content_path),
                ("supply_at_draft", self.supply_at_draft),
                ("approx_pages", self.approx_pages),
            )
        )

    @classmethod
    def from_dict(cls, data: dict) -> "SourceEntry":
        """Build a SourceEntry from a parsed dict, tolerating missing keys."""
        return cls(
            label=str(data.get("label", "")).strip(),
            stype=str(data.get("stype", "note")).strip() or "note",
            location=str(data.get("location", "")).strip(),
            priority=str(data.get("priority", "medium")).strip() or "medium",
            trust=str(data.get("trust", "unverified")).strip() or "unverified",
            intended_use=str(data.get("intended_use", "")).strip(),
            content_path=str(data.get("content_path", "")).strip(),
            supply_at_draft=bool(data.get("supply_at_draft", False)),
            approx_pages=_coerce_page_count(data.get("approx_pages", 0)),
        )


@dataclass
class RunEntry:
    """Every per-run answer from one prompt-generation run.

    Kept in the sibling log's RUN HISTORY zone so the r/last command can
    reproduce a previous prompt exactly, without ever storing the generated
    prompt text. The source registry travels with the run as a list of
    SourceEntry objects.
    """

    timestamp: str
    mode: str
    session: str
    target_agents: str
    skill_name: str
    skill_slug: str
    skill_purpose: str
    trigger_conditions: str
    user_workflow: str
    files_to_generate: str
    doc_depth: str
    verification_requirements: str
    privacy_constraints: str
    out_of_scope: str
    risk_level: str
    skill_category: str = DEFAULT_SKILL_CATEGORY
    example_tasks: str = DEFAULT_EXAMPLE_TASKS
    negative_triggers: str = DEFAULT_NEGATIVE_TRIGGERS
    known_gotchas: str = DEFAULT_KNOWN_GOTCHAS
    quality_rubric: str = DEFAULT_QUALITY_RUBRIC
    observation_method: str = DEFAULT_OBSERVATION_METHOD
    source_license: str = DEFAULT_SOURCE_LICENSE
    build_intent: str = DEFAULT_BUILD_INTENT
    parallelism: str = DEFAULT_PARALLELISM
    memory_file_policy: str = DEFAULT_MEMORY_FILE_POLICY
    subagent_review_policy: str = DEFAULT_SUBAGENT_REVIEW_POLICY
    sources: list = field(default_factory=list)


@dataclass
class SessionEntry:
    """One named working context, identified by a short root hash.

    Recorded in the log's SESSIONS zone the moment a New session is chosen, so
    Continue can list and resume it later.
    """

    created: str
    root: str
    name: str


# History line scalar keys, in write order, mapped to RunEntry attributes. The
# structured ``sources`` field is handled separately (base64-encoded JSON).
SCALAR_FIELDS = OrderedDict(
    (
        ("mode", "mode"),
        ("session", "session"),
        ("target", "target_agents"),
        ("name", "skill_name"),
        ("slug", "skill_slug"),
        ("purpose", "skill_purpose"),
        ("category", "skill_category"),
        ("triggers", "trigger_conditions"),
        ("examples", "example_tasks"),
        ("negatives", "negative_triggers"),
        ("workflow", "user_workflow"),
        ("files", "files_to_generate"),
        ("depth", "doc_depth"),
        ("verify", "verification_requirements"),
        ("gotchas", "known_gotchas"),
        ("quality", "quality_rubric"),
        ("observe", "observation_method"),
        ("privacy", "privacy_constraints"),
        ("exclude", "out_of_scope"),
        ("risk", "risk_level"),
        ("license", "source_license"),
        ("intent", "build_intent"),
        ("parallel", "parallelism"),
        ("memory_policy", "memory_file_policy"),
        ("subagent_review", "subagent_review_policy"),
    )
)
ALLOWED_HISTORY_KEYS = set(SCALAR_FIELDS) | {"sources"}

RUN_DEFAULTS = {
    "target_agents": DEFAULT_TARGET_AGENTS,
    "skill_name": DEFAULT_SKILL_NAME,
    "skill_slug": DEFAULT_SKILL_SLUG,
    "skill_purpose": DEFAULT_SKILL_PURPOSE,
    "skill_category": DEFAULT_SKILL_CATEGORY,
    "trigger_conditions": DEFAULT_TRIGGERS,
    "example_tasks": DEFAULT_EXAMPLE_TASKS,
    "negative_triggers": DEFAULT_NEGATIVE_TRIGGERS,
    "user_workflow": DEFAULT_WORKFLOW,
    "files_to_generate": DEFAULT_FILES,
    "doc_depth": DEFAULT_DOC_DEPTH,
    "verification_requirements": DEFAULT_VERIFICATION,
    "known_gotchas": DEFAULT_KNOWN_GOTCHAS,
    "quality_rubric": DEFAULT_QUALITY_RUBRIC,
    "observation_method": DEFAULT_OBSERVATION_METHOD,
    "privacy_constraints": DEFAULT_PRIVACY,
    "out_of_scope": DEFAULT_OUT_OF_SCOPE,
    "risk_level": DEFAULT_RISK,
    "source_license": DEFAULT_SOURCE_LICENSE,
    "build_intent": DEFAULT_BUILD_INTENT,
    "parallelism": DEFAULT_PARALLELISM,
    "memory_file_policy": DEFAULT_MEMORY_FILE_POLICY,
    "subagent_review_policy": DEFAULT_SUBAGENT_REVIEW_POLICY,
}

# Interview fields that must be non-empty before a prompt may be emitted.
REQUIRED_FIELDS = (
    "skill_name",
    "skill_slug",
    "skill_purpose",
    "skill_category",
    "trigger_conditions",
    "example_tasks",
    "negative_triggers",
    "user_workflow",
    "files_to_generate",
    "doc_depth",
    "verification_requirements",
    "known_gotchas",
    "quality_rubric",
    "observation_method",
    "privacy_constraints",
    "out_of_scope",
    "target_agents",
    "risk_level",
    "source_license",
    "build_intent",
    "memory_file_policy",
    "subagent_review_policy",
)

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


# ---- Small text and IO helpers ----------------------------------------------
def normalize_spaces(value: str) -> str:
    """Collapse repeated whitespace into single spaces."""
    return " ".join(value.split())


def clean_for_log(value: str) -> str:
    """Make user-entered text safe for the pipe-delimited scalar log format.

    " | " is the field separator and square brackets would look like unresolved
    placeholders in the finished prompt, so both are neutralized.
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


def slugify(value: str) -> str:
    """Turn arbitrary text into a kebab-case slug candidate."""
    lowered = value.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return cleaned or DEFAULT_SKILL_SLUG


def resolve_local_path(value: str) -> Path:
    """Resolve a possibly-relative path against the repo root."""
    path = Path(value.strip())
    return path if path.is_absolute() else REPO_ROOT / path


def abs_repo_path(rel: str) -> str:
    """Resolve a repo-relative location to an absolute, POSIX-style string for the prompt.

    Paths are DECLARED relative in the script; the generated prompt always shows them resolved
    to absolute. POSIX separators (forward slashes) keep the output cross-platform and keep the
    verifier's path substrings valid on every OS.
    """
    return resolve_local_path(rel).resolve().as_posix()


def local_path_exists(value: str) -> bool:
    """Return True when a local/relative path resolves to something on disk."""
    if not value.strip():
        return False
    try:
        return resolve_local_path(value).exists()
    except (OSError, ValueError, RuntimeError):
        return False


# A source bigger than this likely cannot be digested in one reference without
# straining the agent's context; warn and nudge the split-by-topic behavior.
SOURCE_SIZE_WARN_BYTES = 200_000

# A local PDF with more pages than this (or with an unknown page count) is treated
# as a large, multi-batch ingestion that warrants a resumable progress ledger in
# the generated prompt. URL sources always warrant the ledger regardless of size.
LEDGER_PAGE_THRESHOLD = 10


def _coerce_page_count(value: object) -> int:
    """Return a non-negative int page count, tolerating strings/None/garbage (0 = unknown)."""
    try:
        pages = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return pages if pages > 0 else 0


def warn_if_large_source(value: str) -> None:
    """Print a non-blocking warning when a local/pre-fetched source file is large."""
    try:
        size = resolve_local_path(value).stat().st_size
    except (OSError, ValueError, RuntimeError):
        return
    if size > SOURCE_SIZE_WARN_BYTES:
        print(
            f"  Note: this source is large (~{size // 1024} KB). The generated prompt organizes "
            "references by topic, so expect it to be split across several focused files, not one."
        )


def source_is_large(src: "SourceEntry") -> bool:
    """Return True when a source is big enough to warrant agent parallelism.

    URL doc-sites, PDFs over LEDGER_PAGE_THRESHOLD pages (or with an unknown count), and local files
    larger than SOURCE_SIZE_WARN_BYTES all qualify.
    """
    if src.is_url():
        return True
    if src.is_pdf():
        return src.approx_pages == 0 or src.approx_pages > LEDGER_PAGE_THRESHOLD
    try:
        return resolve_local_path(src.location).stat().st_size > SOURCE_SIZE_WARN_BYTES
    except (OSError, ValueError, RuntimeError):
        return False


def any_large_source(sources: list) -> bool:
    """Return True when at least one registered source is large (see source_is_large)."""
    return any(source_is_large(s) for s in sources)


def skill_dir_exists(slug: str) -> bool:
    """Return True when `.agents/skills/<slug>/` already exists in the repo."""
    try:
        return (REPO_ROOT / ".agents" / "skills" / slug).is_dir()
    except (OSError, ValueError):
        return False


def memory_paths() -> tuple[str, str]:
    """Return absolute, POSIX-style paths to the repo's AGENTS.md and CLAUDE.md."""
    return abs_repo_path("AGENTS.md"), abs_repo_path("CLAUDE.md")


# ---- Source registry serialization ------------------------------------------
def encode_sources(sources: list) -> str:
    """Encode a list of SourceEntry objects as a base64 JSON string.

    Base64 keeps the value on one line with no " | " or brackets, so it never
    corrupts the pipe-delimited log format and round-trips exactly.
    """
    payload = [src.to_dict() for src in sources]
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def decode_sources(value: str) -> list:
    """Decode a base64 JSON sources string into SourceEntry objects.

    Tolerant: returns an empty list on any decode/parse error so an older or
    corrupt line never crashes the reader.
    """
    text = value.strip()
    if not text:
        return []
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [SourceEntry.from_dict(item) for item in data if isinstance(item, dict)]


def default_seed_sources() -> list:
    """Return the editable default source registry.

    The repo-root Kaizen System document (a local file that exists) plus the two
    official provider skill docs (URLs the executing agent fetches). All three
    are usable, so a first run can emit immediately.
    """
    return [
        SourceEntry(
            label="Kaizen System",
            stype="local-file",
            location="Kaizen_System.md",
            priority="high",
            trust="official",
            intended_use="the SAVMI operating model the skill and its docs should follow",
        ),
        SourceEntry(
            label="Claude skills documentation",
            stype="official-docs",
            location="https://code.claude.com/docs/en/skills",
            priority="high",
            trust="official",
            intended_use="skill format, frontmatter, and authoring rules for Claude",
            supply_at_draft=True,
        ),
        SourceEntry(
            label="Codex skills documentation",
            stype="official-docs",
            location="https://developers.openai.com/codex/skills",
            priority="high",
            trust="official",
            intended_use="skill discovery and format for Codex",
            supply_at_draft=True,
        ),
    ]


# ---- Session line format/parse ----------------------------------------------
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


# ---- Log read / write -------------------------------------------------------
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

    Returns settings, parsed history, parsed sessions, per-session drafts
    (latest answers keyed by session root), the raw development-log lines
    (preserved verbatim so they round-trip), recoverable parse issues, and a
    first-run flag telling main whether to create an initial log. Parsing is
    lenient by design and never raises on older or foreign lines.
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
    """Convert one RUN HISTORY/DRAFT line into a RunEntry, or None if malformed.

    Tolerant by design: any line whose keys are a subset of the known keys
    parses, with missing scalar fields backfilled to empty and sources backfilled
    to an empty list. Lines with unknown keys, or an invalid mode/risk, are
    rejected so they can be re-recorded cleanly.
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

    if not set(values).issubset(ALLOWED_HISTORY_KEYS):
        return None
    if values.get("mode") not in MODES:
        return None
    if values.get("risk") not in RISK_LEVELS:
        return None

    scalar = {attr: values.get(key, "") for key, attr in SCALAR_FIELDS.items()}
    for attr, default in RUN_DEFAULTS.items():
        if not scalar.get(attr):
            scalar[attr] = default
    if scalar["target_agents"] not in TARGET_AGENTS:
        scalar["target_agents"] = DEFAULT_TARGET_AGENTS
    if scalar["skill_category"] not in SKILL_CATEGORY_VALUES:
        scalar["skill_category"] = DEFAULT_SKILL_CATEGORY
    if scalar["source_license"] not in SOURCE_LICENSES:
        scalar["source_license"] = DEFAULT_SOURCE_LICENSE
    if scalar["build_intent"] not in BUILD_INTENTS:
        scalar["build_intent"] = DEFAULT_BUILD_INTENT
    if scalar["parallelism"] not in PARALLELISM_MODES:
        scalar["parallelism"] = DEFAULT_PARALLELISM
    if scalar["memory_file_policy"] not in MEMORY_FILE_POLICIES:
        scalar["memory_file_policy"] = DEFAULT_MEMORY_FILE_POLICY
    if scalar["subagent_review_policy"] not in SUBAGENT_REVIEW_POLICIES:
        scalar["subagent_review_policy"] = DEFAULT_SUBAGENT_REVIEW_POLICY
    sources = decode_sources(values.get("sources", ""))
    return RunEntry(timestamp=match.group("timestamp"), sources=sources, **scalar)


def format_history_entry(entry: RunEntry) -> str:
    """Format one RunEntry for the history/drafts zone (scalars + base64 sources)."""
    parts = [f"{key}={getattr(entry, attr)}" for key, attr in SCALAR_FIELDS.items()]
    parts.append(f"sources={encode_sources(entry.sources)}")
    return f"[{entry.timestamp}] " + " | ".join(parts)


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
    tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp_path, LOG_PATH)


def sources_log_summary(sources: list) -> str:
    """Return a compact, single-line JSON summary of sources for the dev log."""
    compact = [
        {
            "label": src.label,
            "type": src.stype,
            "location": src.location,
            "usable": src.is_usable(),
        }
        for src in sources
    ]
    return json.dumps(compact, ensure_ascii=False)


def format_devlog_lines(
    run_number: int, run: RunEntry, assist_on: bool, events: list[str]
) -> list[str]:
    """Format one run's development-log block (header + indented event bullets).

    Records decisions, actions, and the source registry only - never prompt
    text. The source registry is user data and safe to log.
    """
    header = (
        f"[{run.timestamp}] run {run_number} | session={run.session} | mode={run.mode} | "
        f"target={run.target_agents} | assist={'on' if assist_on else 'off'}"
    )
    bullets = [f"  - {event}" for event in events]
    bullets.append(f"  - sources={sources_log_summary(run.sources)}")
    return [header] + bullets


# ---- Interview prompt primitives --------------------------------------------
def ask_yes_no(prompt: str, default: str = "n", allow_back: bool = False):
    """Ask a yes/no question; return True/False, or BACK when allow_back and the user types b."""
    default = default.lower()
    suffix = " | b=back" if allow_back else ""
    while True:
        answer = read_line(f"{prompt} [{default}]{suffix}: ").strip().lower()
        if allow_back and answer in {"b", "back"}:
            return BACK
        value = default if answer == "" else answer
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Enter y or n (or q to quit).")


def read_multiline_answer(sentinel: str = MULTILINE_SENTINEL) -> str:
    """Read a multi-line answer until a line equal to the sentinel, or EOF.

    Reads literal text via input(), so q/b are not special inside it; caps total
    size so a runaway paste cannot hang or exhaust memory; auto-closes on EOF so
    a missing terminator never blocks.
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
        size += len(line) + 1
        if size > PASTE_CAP:
            print("  Too long; using what was captured so far.")
            break
        collected.append(line)
    return "\n".join(collected)


def confirm_or_enter(label: str, explanation: str, default: str, allow_back: bool = False):
    """Ask a free-text question in one step: keep the default or replace it.

    Enter keeps the shown default; typing anything makes that text the new value;
    ``m`` opens a multi-line capture; ``b`` (when allow_back) returns BACK; ``q``
    quits. Returns the value, or BACK.
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


def prompt_slug(default: str, name_hint: str, allow_back: bool = False):
    """Ask for a kebab-case skill slug, validating the charset before moving on.

    Enter keeps the default; ``b`` (when allow_back) returns BACK; ``q`` quits.
    Re-asks until the value is a valid slug (lowercase letters, digits, hyphens).
    """
    print("\nSkill slug (folder name under .agents/skills/)")
    print("  Lowercase letters, digits, and single hyphens only, e.g. my-skill.")
    suggested = default if SLUG_RE.match(default) else slugify(name_hint or default)
    print(f"  current default: {suggested}")
    suffix = " | b=back" if allow_back else ""
    while True:
        answer = read_line(f"  Enter=keep | type a slug{suffix} | q=quit: ").strip()
        if allow_back and answer.lower() in {"b", "back"}:
            return BACK
        candidate = suggested if answer == "" else answer.strip().lower()
        if SLUG_RE.match(candidate):
            return candidate
        derived = slugify(candidate)
        print(f"  Not a valid slug. Try: {derived}")
        suggested = derived


def validate_path(value: str) -> tuple[bool, str]:
    """Validate a folder-path setting; return (ok, feedback message).

    Rejects empty input and paths with characters invalid in a folder name;
    otherwise resolves the path (relative paths against the repo root) and
    reports the resolved folder and whether it already exists.
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


def prompt_path(label: str, explanation: str, default: str, allow_back: bool = False):
    """Ask for a folder-path setting, validating it before moving on.

    Rejects a bare y/n (a confirm habit), validates the path, and reports the
    resolved folder and whether it exists, re-asking until the value is usable.
    Enter keeps the default; b (when allow_back) returns BACK; q quits.
    """
    print(f"\n{label}")
    for line in explanation.splitlines():
        print(f"  {line}")
    print(f"  current default: {default}")
    suffix = " | b=back" if allow_back else ""
    while True:
        answer = read_line(f"  Enter=keep | type a folder path{suffix} | q=quit: ").strip()
        if allow_back and answer.lower() in {"b", "back"}:
            return BACK
        if answer.lower() in CONFIRM_TOKENS:
            print(
                "  That looks like a yes/no. Type the actual folder path, "
                "or press Enter to keep the default."
            )
            continue
        value = default if answer == "" else clean_for_log(answer)
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
):
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


def prompt_toggle(label: str, explanation: str, default: str, allow_back: bool = False):
    """Ask an on/off setting; return 'on'/'off', or BACK when allow_back and the user types b."""
    print(f"\n{label}")
    if explanation:
        print(f"  {explanation}")
    shown = "on" if default == "on" else "off"
    suffix = " | b=back" if allow_back else ""
    while True:
        answer = read_line(f"  {label} [{shown}] (on/off or y/n{suffix}, q=quit): ").strip().lower()
        if allow_back and answer in {"b", "back"}:
            return BACK
        if answer == "":
            return default
        if answer in {"on", "y", "yes", "1", "true"}:
            return "on"
        if answer in {"off", "n", "no", "0", "false"}:
            return "off"
        print("  Enter on/off or y/n.")


def prompt_copy_setting(default: str, allow_back: bool = False):
    """Ask whether to copy prompts to the clipboard; return 'yes'/'no', or BACK."""
    toggled = prompt_toggle(
        "copy_to_clipboard",
        "Copy each generated prompt to the clipboard automatically.",
        "on" if is_enabled(default) else "off",
        allow_back=allow_back,
    )
    if toggled is BACK:
        return BACK
    return "yes" if toggled == "on" else "no"


def prompt_backup_mode(default: str, allow_back: bool = False):
    """Ask the backup mode (ask/always/never or y/n); return the mode, or BACK."""
    print("\nbackup_mode")
    print("  Backups save a Markdown copy of each generated prompt so you can reread it later.")
    suffix = " | b=back" if allow_back else ""
    while True:
        answer = read_line(
            f"  backup_mode [{default}] (ask / always / never, or y=always / n=never{suffix}, q=quit): "
        ).strip().lower()
        if allow_back and answer in {"b", "back"}:
            return BACK
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
    """Let the user review and change persistent settings (mode is skipped).

    Navigable: b steps back to the previous setting; b at the first setting exits settings.
    """
    updated = OrderedDict(settings)
    print("Settings: answer each question; b=back, q=quit.")
    keys = [k for k in SETTING_KEYS if k != "mode"]
    i = 0
    while i < len(keys):
        key = keys[i]
        if key == "assist_mode":
            val = prompt_toggle(
                "assist_mode",
                "Optional copy-paste LLM help during the interview (default on).",
                updated[key],
                allow_back=True,
            )
        elif key == "copy_to_clipboard":
            val = prompt_copy_setting(updated[key], allow_back=True)
        elif key == "backup_mode":
            val = prompt_backup_mode(updated[key], allow_back=True)
        elif key in PATH_SETTINGS:
            val = prompt_path(key, FOLDER_EXPLANATIONS.get(key, ""), updated[key], allow_back=True)
        else:
            val = confirm_or_enter(key, FOLDER_EXPLANATIONS.get(key, ""), updated[key], allow_back=True)
        if val is BACK:
            if i == 0:
                break  # back at the first setting exits the settings walk
            i -= 1
            continue
        updated[key] = val
        i += 1
    return updated, updated != settings


# ---- Menus for special interview fields -------------------------------------
def target_agents_menu(default: str, allow_back: bool = False):
    """Ask which agent(s) the skill targets: Codex, Claude, or both."""
    return prompt_menu(
        "Which agent(s) is this skill for?",
        "Codex reads .agents/skills; Claude reads .claude/skills — both are per-skill junctions to one store copy.",
        (
            ("Codex", ("x",), "tailor the build prompt to Codex"),
            ("Claude", ("c",), "tailor the build prompt to Claude"),
            ("both", (), "tailor to either; the one copy serves both via junctions"),
        ),
        default,
        allow_back=allow_back,
    )


def skill_category_menu(default: str, allow_back: bool = False):
    """Ask which skill category best describes the target skill."""
    return prompt_menu(
        "Skill category",
        "Choose the primary category; if the skill spans categories, the generated prompt asks the agent to explain why.",
        tuple((value, (), label) for value, label in SKILL_CATEGORIES.items()),
        default if default in SKILL_CATEGORY_VALUES else DEFAULT_SKILL_CATEGORY,
        allow_back=allow_back,
    )


def risk_menu(default: str, allow_back: bool = False):
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


def memory_file_policy_menu(default: str, allow_back: bool = False):
    """Ask how generated prompts should handle AGENTS.md / CLAUDE.md changes."""
    return prompt_menu(
        "Memory-file policy",
        "Controls whether the receiving agent only reads memory files or proposes/edits routing changes.",
        (
            ("assess-then-ask", ("a",), "inspect AGENTS.md/CLAUDE.md and propose changes, but ask before editing"),
            ("read-only", ("r",), "read memory files as context only"),
            ("edit-with-explicit-approval", ("e",), "may edit memory files only after explicit approval"),
        ),
        default if default in MEMORY_FILE_POLICIES else DEFAULT_MEMORY_FILE_POLICY,
        allow_back=allow_back,
    )


def subagent_review_policy_menu(default: str, allow_back: bool = False):
    """Ask when generated prompts should require independent subagent review."""
    return prompt_menu(
        "Subagent review policy",
        "Controls independent architecture/quality review beyond source digestion.",
        (
            ("nontrivial", ("n",), "require independent review for nontrivial skills"),
            ("high-risk-only", ("h",), "require it only for high-risk, public, team, or large-source skills"),
            ("optional", ("o",), "suggest it as an optional quality pass"),
            ("none", (), "do not request subagent review"),
        ),
        default if default in SUBAGENT_REVIEW_POLICIES else DEFAULT_SUBAGENT_REVIEW_POLICY,
        allow_back=allow_back,
    )


def source_license_menu(default: str, allow_back: bool = False):
    """Ask the license posture of the registered sources, taken collectively."""
    return prompt_menu(
        "What is the license posture of your sources?",
        "Decides whether the prompt digests+attributes the sources or writes original "
        "'inspired-by' content. When unsure, choose unknown (treated as restricted).",
        (
            ("permissive-or-owned", ("p",),
             "public-domain, permissive, or your own: may quote/paraphrase"),
            ("copyleft-or-restricted", ("c",),
             "copyleft or otherwise restricted: original 'inspired-by' content only"),
            ("unknown", ("u",), "not sure: safest path, original 'inspired-by' content only"),
        ),
        default,
        allow_back=allow_back,
    )


def build_intent_menu(default: str, allow_back: bool = False):
    """Ask whether this run authors a NEW skill or updates an EXISTING one."""
    return prompt_menu(
        "Is this a new skill or an update to an existing one?",
        "Update mode tells the agent to extend/refresh the existing skill in place and to "
        "preserve unrelated content, instead of authoring from scratch.",
        (
            ("new", ("n",), "author a brand-new skill package"),
            ("update", ("u",), "extend/refresh an existing skill at the same slug"),
        ),
        default,
        allow_back=allow_back,
    )


def parallelism_menu(executor: str, default: str = "none", allow_back: bool = False):
    """Offer agent parallelism for a large build; return a PARALLELISM_MODES value or BACK.

    Asked only when a registered source is large. A Codex (non-Claude) executor gets model-agnostic
    'generic' parallelism; a Claude executor chooses Haiku or Opus subagents.
    """
    print("\nAgent parallelism")
    print("  A registered source is large; parallel subagents can fetch/digest disjoint batches faster.")
    print("  (The orchestrator still owns all writes, the ledger, and verification.)")
    want = ask_yes_no(
        "  Include agent-parallelism instructions in the prompt?",
        "y" if default != "none" else "n",
        allow_back=allow_back,
    )
    if want is BACK:
        return BACK
    if not want:
        return "none"
    if executor != "Claude":
        return "generic"
    return prompt_menu(
        "Subagent model (Claude)",
        "Which Claude subagents should fan out the fetch/digest work?",
        (
            ("opus", ("o",), "Opus subagents - strongest reasoning, higher cost"),
            ("haiku", ("h",), "Haiku subagents - fast and cheap for bulk fetch/digest"),
        ),
        default if default in ("opus", "haiku") else "opus",
        allow_back=allow_back,
    )


# ---- Source registry sub-menu -----------------------------------------------
def source_summary_line(index: int, src: SourceEntry) -> str:
    """Return a one-line listing of a source with its usability flag."""
    flag = "usable" if src.is_usable() else "NEEDS CONTENT"
    return (
        f"  {index}. [{flag}] {src.label} ({src.stype}, priority {src.priority}, "
        f"trust {src.trust})\n       location: {src.location}"
    )


def list_sources(sources: list) -> None:
    """Print the current source registry, or note that it is empty."""
    if not sources:
        print("  (no sources yet)")
        return
    for index, src in enumerate(sources, start=1):
        print(source_summary_line(index, src))


def ask_source_location_for_url(src: SourceEntry):
    """Resolve content handling for a URL source; return True when done, or BACK.

    The agent that runs the generated prompt fetches the URL deeply and caches it under
    AI/work/<slug>-docs. A pre-fetched local copy is optional.
    """
    print(
        "  This is a URL. The agent that runs the prompt will fetch it (and relevant links) "
        "and cache it under AI/work. You may optionally point to a pre-fetched local copy."
    )
    content = confirm_or_enter(
        "Pre-fetched local copy (optional)",
        "Path to an already-downloaded copy of the page. Leave as the default to skip.",
        src.content_path or "",
        allow_back=True,
    )
    if content is BACK:
        return BACK
    if content and local_path_exists(content):
        src.content_path = content
        print(f"  Recorded pre-fetched content at {content}.")
        warn_if_large_source(content)
    else:
        if content:
            print(f"  No file found at {content}; not recorded — the agent will fetch the URL.")
        src.content_path = ""
    src.supply_at_draft = True  # retained for log back-compat; no longer gates anything
    return True


def ask_pdf_pages(src: SourceEntry) -> None:
    """For a PDF source, capture an approximate page count (drives the resumable-ledger trigger).

    A local PDF over ``LEDGER_PAGE_THRESHOLD`` pages, or with an unknown count, is
    treated as a large multi-batch ingestion. Non-blocking: Enter keeps the current
    value and unparseable input is recorded as unknown (0). URL PDFs already warrant
    the ledger via ``is_url`` and are not asked here.
    """
    if not src.is_pdf():
        return
    raw = confirm_or_enter(
        "Approximate page count for this PDF",
        (
            "Decides whether the prompt mandates a resumable ingestion ledger "
            f"(on above {LEDGER_PAGE_THRESHOLD} pages, or when unknown). "
            "Enter a number, or keep the default if you don't know."
        ),
        str(src.approx_pages) if src.approx_pages else "",
    )
    src.approx_pages = _coerce_page_count(raw)
    if src.approx_pages == 0:
        print("  Page count unknown - treating this PDF as a large ingestion (ledger on).")
    elif src.approx_pages > LEDGER_PAGE_THRESHOLD:
        print(f"  {src.approx_pages} pages (> {LEDGER_PAGE_THRESHOLD}) - resumable ledger on.")
    else:
        print(f"  {src.approx_pages} pages (<= {LEDGER_PAGE_THRESHOLD}) - no ledger needed for this source.")


def collect_source_location(src: SourceEntry):
    """Prompt for the location and resolve URL/path content handling.

    Sets ``src.location`` (and URL content fields). Returns True when done, BACK to step to the
    previous field, or "cancel" to abort adding the source.
    """
    while True:
        location = confirm_or_enter(
            "Source location",
            "A URL (http/https) or a repo-relative / local path.",
            src.location or "",
            allow_back=True,
        )
        if location is BACK:
            return BACK
        if not location.strip():
            print("  A location is required.")
            continue
        src.location = location.strip()
        if src.is_url():
            if ask_source_location_for_url(src) is BACK:
                continue
            return True
        if local_path_exists(src.location):
            print(f"  Found local path: {resolve_local_path(src.location)}")
            warn_if_large_source(src.location)
            ask_pdf_pages(src)
            return True
        print(f"  No file found at {resolve_local_path(src.location)}.")
        anyway = ask_yes_no("  Add it anyway and resolve the path later?", "n", allow_back=True)
        if anyway is BACK:
            continue
        if anyway:
            src.content_path = ""
            src.supply_at_draft = False
            ask_pdf_pages(src)
            return True
        reenter = ask_yes_no("  Re-enter the location?", "y", allow_back=True)
        if reenter is BACK or reenter:
            continue
        print("  Source not added.")
        return "cancel"


def build_source_interactively(existing: SourceEntry | None = None) -> SourceEntry | None:
    """Collect one source step by step; b steps back, b at the first step cancels (returns None)."""
    base = existing or SourceEntry(label="", stype="note", location="")
    src = SourceEntry(
        label=base.label, stype=base.stype, location=base.location,
        priority=base.priority, trust=base.trust, intended_use=base.intended_use,
        content_path=base.content_path, supply_at_draft=base.supply_at_draft,
        approx_pages=base.approx_pages,
    )
    steps = ("label", "stype", "location", "priority", "trust", "intended_use")
    i = 0
    while i < len(steps):
        step = steps[i]
        if step == "label":
            r = confirm_or_enter("Source label", "A short human-readable name.",
                                 src.label or "New source", allow_back=True)
            if r is BACK:
                return None  # back at the first step cancels the add
            src.label = r
        elif step == "stype":
            r = prompt_menu(
                "Source type",
                "Used for attribution metadata.",
                (
                    ("official-docs", ("o",), "official documentation"),
                    ("local-file", ("l",), "a file already in the repo or on disk"),
                    ("url", ("u",), "a web page (the executing agent fetches it; local copy optional)"),
                    ("example", ("e",), "a worked example"),
                    ("api", ("a",), "an API reference"),
                    ("tutorial", ("t",), "a tutorial or guide"),
                    ("note", ("n",), "a freeform note"),
                ),
                src.stype if src.stype in SOURCE_TYPES else "note",
                allow_back=True,
            )
            if r is BACK:
                i -= 1
                continue
            src.stype = r
        elif step == "location":
            r = collect_source_location(src)
            if r == "cancel":
                return None
            if r is BACK:
                i -= 1
                continue
        elif step == "priority":
            r = prompt_menu(
                "Priority",
                "How important this source is to the skill.",
                (("high", ("h",), "core"), ("medium", ("m",), "useful"), ("low", ("l",), "supporting")),
                src.priority if src.priority in PRIORITIES else "medium",
                allow_back=True,
            )
            if r is BACK:
                i -= 1
                continue
            src.priority = r
        elif step == "trust":
            r = prompt_menu(
                "Trust level",
                "How authoritative this source is.",
                (
                    ("official", ("o",), "first-party/official"),
                    ("verified", ("v",), "checked and reliable"),
                    ("community", ("c",), "community-sourced"),
                    ("unverified", ("u",), "unverified"),
                ),
                src.trust if src.trust in TRUST_LEVELS else "unverified",
                allow_back=True,
            )
            if r is BACK:
                i -= 1
                continue
            src.trust = r
        else:  # intended_use
            r = confirm_or_enter("Intended use", "What the LLM should extract from this source.",
                                 src.intended_use or "", allow_back=True)
            if r is BACK:
                i -= 1
                continue
            src.intended_use = r
        i += 1
    return src


def manage_source_registry(sources: list, persist):
    """Run the add/edit/remove/list source sub-menu; return the list or BACK.

    ``persist`` is called after every change so the registry is saved to the
    session draft immediately and survives quit/Continue. Enter or 'd' proceeds
    to the next interview question; 'b' goes back; 'q' quits.
    """
    print("\nSource registry")
    print("  Register every source the LLM must digest. These survive quit/Continue.")
    while True:
        list_sources(sources)
        choice = prompt_menu(
            "sources",
            "Add, edit, remove, or list sources; then continue.",
            (
                ("add", ("a",), "register a new source"),
                ("edit", ("e",), "change an existing source"),
                ("remove", ("r",), "delete a source"),
                ("list", ("l",), "show the registry again"),
                ("done", ("d",), "continue to the next question"),
            ),
            "done",
            allow_back=True,
        )
        if choice is BACK:
            return BACK
        if choice == "done":
            return sources
        if choice == "list":
            continue
        if choice == "add":
            new_source = build_source_interactively()
            if new_source is not None:
                sources.append(new_source)
                persist()
            continue
        if not sources:
            print("  No sources to edit or remove yet.")
            continue
        index = pick_source_index(sources, choice)
        if index is None:
            continue
        if choice == "remove":
            removed = sources.pop(index)
            print(f"  Removed: {removed.label}")
            persist()
        elif choice == "edit":
            edited = build_source_interactively(sources[index])
            if edited is not None:
                sources[index] = edited
                persist()


def pick_source_index(sources: list, action: str) -> int | None:
    """Ask the user to pick a source by number for edit/remove; Enter or b cancels."""
    while True:
        answer = read_line(f"  pick a source to {action} 1-{len(sources)} (Enter=cancel, b=back, q=quit): ").strip().lower()
        if answer in {"", "b", "back"}:
            return None
        if answer.isdigit():
            number = int(answer)
            if 1 <= number <= len(sources):
                return number - 1
        print("  Enter a listed number, or Enter to cancel.")


# ---- Mode menu + sessions ---------------------------------------------------
def resolve_mode(answer: str) -> str | None:
    """Translate a mode answer (blank, digit, or name) into the single mode."""
    if answer in {"", "1", "skill", "skill build prompt"}:
        return "skill build prompt"
    return None


def print_mode_menu() -> None:
    """Show the run header with the available commands."""
    print("\nThis run builds a skill-package build-prompt.")
    print("  Press Enter (or 1) to start.")
    print("  commands: r = reprint last prompt | s = settings | q = quit")


def ask_mode(
    settings: OrderedDict[str, str],
    history: list[RunEntry],
    sessions: list[SessionEntry],
    drafts: "dict[str, RunEntry]",
    devlog: list[str],
    reprint_source: RunEntry | None,
) -> str:
    """Ask for the run mode or handle the r/s commands (q quits via read_line)."""
    print_mode_menu()
    while True:
        answer = read_line(f"  start [{settings['mode']}]: ").strip().lower()
        if answer in {"b", "back"}:
            print("  Already at the start; nothing to go back to.")
            continue
        if answer in {"s", "settings"}:
            new_settings, _changed = walk_settings(settings)
            settings.clear()
            settings.update(new_settings)
            write_log(settings, history, sessions, drafts, devlog)
            print_mode_menu()
            continue
        if answer in {"r", "last"}:
            if reprint_source is None:
                print("No previous run in this session to reprint.")
                continue
            output = build_output_safely(settings, reprint_source, len(history))
            if output is None:
                raise SystemExit(1)
            emit_output(settings, output)
            raise SystemExit(0)
        resolved = resolve_mode(answer)
        if resolved is not None:
            settings["mode"] = resolved
            return resolved
        print("  Press Enter to start, or r / s / q.")


def pick_session(sessions: list[SessionEntry], allow_back: bool = False):
    """List saved sessions newest-first and let the user pick one; BACK on b when allow_back."""
    ordered = list(reversed(sessions))
    print("\nSaved sessions (newest first):")
    for number, session in enumerate(ordered, start=1):
        print(f"  {number}. {session.name} [{session.root}] (created {session.created})")
    suffix = " | b=back" if allow_back else ""
    while True:
        answer = read_line(f"  pick a session 1-{len(ordered)} [1]{suffix} (q=quit): ").strip().lower()
        if allow_back and answer in {"b", "back"}:
            return BACK
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
    while True:
        if sessions:
            choice = prompt_menu(
                "Start",
                "Continue a saved session or start a new one.",
                (
                    ("continue", ("c",), "resume a saved session and its answers"),
                    ("new", ("n",), "start a fresh session"),
                ),
                "continue",
                allow_back=True,
            )
            if choice is BACK:
                continue  # nothing precedes the Start menu; re-show it
            if choice == "continue":
                picked = pick_session(sessions, allow_back=True)
                if picked is BACK:
                    continue  # back to the Start menu
                return picked
        name = confirm_or_enter(
            "Name this session",
            "A short label so you can find it later in Continue.",
            "untitled session",
            allow_back=bool(sessions),
        )
        if name is BACK:
            continue  # back to the Start menu
        session = SessionEntry(
            created=datetime.now().strftime("%Y-%m-%d %H:%M"), root=make_root_hash(), name=name
        )
        sessions.append(session)
        write_log(settings, history, sessions, drafts, devlog)
        print(f"Started session '{session.name}' [{session.root}].")
        return session


# ---- Interview engine -------------------------------------------------------
def print_interview_warning() -> None:
    """Warn about multi-line pastes and remind the user of m/b/q."""
    print(
        "\nAnswering tips: type m for a multi-line answer (paste, then a line with only END). "
        "Type b to go back to the previous question, q to quit."
    )
    print(
        "Note: pasting several lines directly will spill into the next questions - use m for that."
    )


def ask_interview_step(key: str, answers: dict[str, str], sources: list, session_last: RunEntry | None, persist):
    """Ask one interview step by key; returns the value, the source list, or BACK."""
    if key == "skill_slug":
        default = answers.get("skill_slug") or (
            session_last.skill_slug if session_last and session_last.skill_slug else DEFAULT_SKILL_SLUG
        )
        return prompt_slug(default, answers.get("skill_name", ""), allow_back=True)
    if key == "target_agents":
        default = answers.get("target_agents") or (
            session_last.target_agents if session_last and session_last.target_agents in TARGET_AGENTS
            else DEFAULT_TARGET_AGENTS
        )
        return target_agents_menu(default, allow_back=True)
    if key == "skill_category":
        default = answers.get("skill_category") or (
            session_last.skill_category
            if session_last and getattr(session_last, "skill_category", "") in SKILL_CATEGORY_VALUES
            else DEFAULT_SKILL_CATEGORY
        )
        return skill_category_menu(default, allow_back=True)
    if key == "sources":
        return manage_source_registry(sources, persist)
    if key == "risk_level":
        default = answers.get("risk_level") or (
            session_last.risk_level if session_last and session_last.risk_level in RISK_LEVELS else DEFAULT_RISK
        )
        return risk_menu(default, allow_back=True)
    if key == "source_license":
        default = answers.get("source_license") or (
            session_last.source_license
            if session_last and session_last.source_license in SOURCE_LICENSES else DEFAULT_SOURCE_LICENSE
        )
        return source_license_menu(default, allow_back=True)
    if key == "memory_file_policy":
        default = answers.get("memory_file_policy") or (
            session_last.memory_file_policy
            if session_last and getattr(session_last, "memory_file_policy", "") in MEMORY_FILE_POLICIES
            else DEFAULT_MEMORY_FILE_POLICY
        )
        return memory_file_policy_menu(default, allow_back=True)
    if key == "subagent_review_policy":
        default = answers.get("subagent_review_policy") or (
            session_last.subagent_review_policy
            if session_last and getattr(session_last, "subagent_review_policy", "") in SUBAGENT_REVIEW_POLICIES
            else DEFAULT_SUBAGENT_REVIEW_POLICY
        )
        return subagent_review_policy_menu(default, allow_back=True)
    if key == "build_intent":
        default = answers.get("build_intent") or (
            session_last.build_intent
            if session_last and session_last.build_intent in BUILD_INTENTS else DEFAULT_BUILD_INTENT
        )
        return build_intent_menu(default, allow_back=True)
    if key == "parallelism":
        # Only offered when a registered source is large; otherwise transparently 'none'.
        if not any_large_source(sources):
            return DEFAULT_PARALLELISM
        target = answers.get("target_agents") or (
            session_last.target_agents if session_last and session_last.target_agents in TARGET_AGENTS
            else DEFAULT_TARGET_AGENTS
        )
        executor = "Claude" if target in ("Claude", "both") else "Codex"
        default = answers.get("parallelism") or (
            session_last.parallelism
            if session_last and getattr(session_last, "parallelism", "") in PARALLELISM_MODES
            else DEFAULT_PARALLELISM
        )
        return parallelism_menu(executor, default, allow_back=True)
    label, explanation, field_default = FIELD_META[key]
    if key == "observation_method":
        category = answers.get("skill_category") or (
            session_last.skill_category
            if session_last and getattr(session_last, "skill_category", "") in SKILL_CATEGORY_VALUES
            else DEFAULT_SKILL_CATEGORY
        )
        field_default = category_observation_default(category)
    if answers.get(key):
        current = answers[key]
    elif session_last is not None and getattr(session_last, key, ""):
        current = getattr(session_last, key)
    else:
        current = field_default
    return confirm_or_enter(label, explanation, current, allow_back=True)


INTERVIEW_STEPS = (
    "skill_name",
    "skill_slug",
    "build_intent",
    "skill_purpose",
    "target_agents",
    "skill_category",
    "trigger_conditions",
    "example_tasks",
    "negative_triggers",
    "sources",
    "parallelism",
    "source_license",
    "memory_file_policy",
    "subagent_review_policy",
    "user_workflow",
    "files_to_generate",
    "doc_depth",
    "verification_requirements",
    "known_gotchas",
    "quality_rubric",
    "observation_method",
    "privacy_constraints",
    "out_of_scope",
    "risk_level",
)


def run_interview(
    session_last: RunEntry | None, sources: list, persist, answers: dict[str, str] | None = None,
    start_index: int = 0,
) -> dict[str, str]:
    """Run the navigable interview (b goes back) and return the scalar answers.

    The source registry is edited in place in ``sources``. ``persist(answers)``
    is called after every answer so the session's draft is always current and a
    quit-partway interview resumes exactly (sources included). Re-entrant: pass prior
    ``answers`` and a ``start_index`` to resume (e.g. when a later stage steps back),
    landing on a chosen question with all prior values prefilled as defaults.
    """
    print_interview_warning()
    if answers is None:
        answers = {}
    index = max(0, min(start_index, len(INTERVIEW_STEPS) - 1))
    while index < len(INTERVIEW_STEPS):
        key = INTERVIEW_STEPS[index]
        result = ask_interview_step(key, answers, sources, session_last, lambda: persist(answers))
        if result is BACK:
            if index == 0:
                print("Already at the first question.")
                continue
            index -= 1
            continue
        if key != "sources":
            answers[key] = result
        index += 1
        persist(answers)
    return answers


def entry_from_answers(
    answers: dict[str, str], sources: list, mode: str, timestamp: str, session: str
) -> RunEntry:
    """Build a RunEntry from an answers dict plus the source registry."""
    scalar = {
        attr: answers.get(attr, RUN_DEFAULTS.get(attr, ""))
        for _key, attr in SCALAR_FIELDS.items()
        if attr not in {"mode", "session"}
    }
    return RunEntry(
        timestamp=timestamp,
        mode=mode,
        session=session,
        sources=list(sources),
        **scalar,
    )


def draft_from_answers(
    answers: dict[str, str], sources: list, mode: str, session: str, fallback: RunEntry | None
) -> RunEntry:
    """Build a draft RunEntry from possibly-partial answers plus sources.

    Not-yet-answered scalar fields are filled from the prior draft/run (fallback)
    or built-in defaults, so the draft is always a valid, complete entry that can
    be saved and later reproduced.
    """
    defaults = dict(RUN_DEFAULTS)

    def pick(attr: str) -> str:
        """Return the answer for attr, else the fallback's value, else default."""
        value = answers.get(attr)
        if value:
            return value
        if fallback is not None and getattr(fallback, attr, ""):
            return getattr(fallback, attr)
        return defaults[attr]

    return RunEntry(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
        mode=mode,
        session=session,
        target_agents=pick("target_agents"),
        skill_name=pick("skill_name"),
        skill_slug=pick("skill_slug"),
        skill_purpose=pick("skill_purpose"),
        skill_category=pick("skill_category"),
        trigger_conditions=pick("trigger_conditions"),
        example_tasks=pick("example_tasks"),
        negative_triggers=pick("negative_triggers"),
        user_workflow=pick("user_workflow"),
        files_to_generate=pick("files_to_generate"),
        doc_depth=pick("doc_depth"),
        verification_requirements=pick("verification_requirements"),
        known_gotchas=pick("known_gotchas"),
        quality_rubric=pick("quality_rubric"),
        observation_method=pick("observation_method"),
        privacy_constraints=pick("privacy_constraints"),
        out_of_scope=pick("out_of_scope"),
        risk_level=pick("risk_level"),
        source_license=pick("source_license"),
        build_intent=pick("build_intent"),
        parallelism=pick("parallelism"),
        memory_file_policy=pick("memory_file_policy"),
        subagent_review_policy=pick("subagent_review_policy"),
        sources=list(sources) if sources else (list(fallback.sources) if fallback else []),
    )


# ---- Optional copy-paste LLM-assist loop ------------------------------------
def read_pasted_block(expect_json: bool = False) -> str:
    """Read a pasted reply and return its text, finishing the way users paste.

    When expect_json is True, the block finishes the instant the captured text
    parses as JSON (so pasting and pressing Enter completes with no terminator);
    a blank line also finishes. A line equal to END or <<END>>, or EOF, always
    finishes. Caps total size; reads literal text, so q is not treated as quit.
    """
    if expect_json:
        print(
            "Paste the LLM reply; it finishes automatically once the JSON is complete "
            "(or press Enter on a blank line, or type END)."
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
        if expect_json and stripped == "":
            break
        size += len(line) + 1
        if size > PASTE_CAP:
            print("Reply too large; using what was captured so far.")
            break
        collected.append(line)
        if expect_json and parse_assist_response("\n".join(collected)) is not None:
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


def preclean_json(text: str) -> str:
    """Best-effort stdlib cleanup of common LLM JSON quirks."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    cleaned = cleaned.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
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
    current = "\n".join(f"- {attr}: {answers.get(attr, '')}" for attr in KNOWN_FIELDS)
    schema_fields = ",\n    ".join(f'"{attr}": "..."' for attr in KNOWN_FIELDS)
    return (
        "You are helping refine the inputs to a skill-package build-prompt generator.\n\n"
        f"{KAIZEN_SYSTEM_SUMMARY}\n\n"
        "The user wants a build-prompt that makes a coding agent author a skill package with "
        "deeply digested, attributed reference docs. Current draft answers:\n"
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
        "3. In recommended_capabilities, list quality and discoverability capabilities the skill should include.\n"
        "Return only the json block, nothing else."
    )


def compose_critique_assist_request(draft_prompt: str) -> str:
    """Compose the critique request: review the draft, return JSON field edits."""
    schema_fields = ",\n    ".join(f'"{attr}": "..."' for attr in KNOWN_FIELDS)
    return (
        "You are reviewing a draft build-prompt that will instruct a coding agent to author a "
        "skill package with digested reference docs.\n\n"
        f"{KAIZEN_SYSTEM_SUMMARY}\n\n"
        "Critique the draft below for gaps, weak digestion rules, unclear acceptance criteria, or "
        "anything that would leave the resulting skill shallow. Then return improved input values.\n\n"
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
        "3. In recommended_capabilities, list quality and discoverability capabilities the skill should include.\n"
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


def apply_assist_response(
    answers: dict[str, str], block: str, events: list[str], kind: str = "sharpen"
) -> dict[str, str]:
    """Ingest a sharpen/critique reply and AUTO-APPLY its field suggestions (no per-field prompts).

    Decisions and recommended capabilities are printed for awareness; recognized field suggestions
    are applied directly to ``answers`` (risk_level/target_agents validated against their allowed
    values, invalid ones skipped), then a one-line summary lists what changed. The final review
    screen still shows every value before generation, so the user keeps a last look.
    """
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
    applied: list[str] = []
    skipped: list[str] = []
    for attr, suggested in suggestions.items():
        if attr == "risk_level" and suggested not in RISK_LEVELS:
            skipped.append(attr)
            continue
        if attr == "target_agents" and suggested not in TARGET_AGENTS:
            skipped.append(attr)
            continue
        if attr == "skill_category" and suggested not in SKILL_CATEGORY_VALUES:
            skipped.append(attr)
            continue
        if attr == "source_license" and suggested not in SOURCE_LICENSES:
            skipped.append(attr)
            continue
        if attr == "parallelism" and suggested not in PARALLELISM_MODES:
            skipped.append(attr)
            continue
        if attr == "memory_file_policy" and suggested not in MEMORY_FILE_POLICIES:
            skipped.append(attr)
            continue
        if attr == "subagent_review_policy" and suggested not in SUBAGENT_REVIEW_POLICIES:
            skipped.append(attr)
            continue
        answers[attr] = suggested
        applied.append(attr)
    if applied:
        print(f"Applied {len(applied)} suggestion(s): {', '.join(applied)}.")
    else:
        print("No recognized field suggestions to apply.")
    if skipped:
        print(f"Skipped invalid suggestion(s): {', '.join(skipped)}.")
    events.append(f"{kind} round: applied {applied}; skipped {skipped}")
    return answers


def record_assist_pass(devlog: list[str], session_root: str, kind: str, pass_no: int, block: str) -> None:
    """Append a one-line record of one assist pass reply to the development log.

    The reply is stored as compact JSON (field values / critique notes only -
    never the build-prompt) so each pass can be reviewed later.
    """
    parsed = parse_assist_response(block)
    if isinstance(parsed, dict):
        reply = json.dumps(parsed, ensure_ascii=False)
    else:
        reply = f"reply-unparsed ({len(block)} chars)"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    devlog.append(f"[{timestamp}] session={session_root} | assist {kind} pass {pass_no} | reply={reply}")


def run_assist_pass(
    settings: OrderedDict[str, str],
    answers: dict[str, str],
    sources: list,
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
            answers, sources, "skill build prompt", datetime.now().strftime("%Y-%m-%d %H:%M"), active.root
        )
        request = compose_critique_assist_request(build_prompt(settings, draft_entry, len(history) + 1))
    emit_assist_request(settings, request)
    block = read_pasted_block(expect_json=True)
    answers = apply_assist_response(answers, block, events, kind=kind)
    record_assist_pass(devlog, active.root, kind, pass_no, block)
    persist(answers)
    return answers


def run_assist_loop(
    settings: OrderedDict[str, str],
    answers: dict[str, str],
    sources: list,
    history: list[RunEntry],
    events: list[str],
    devlog: list[str],
    active: SessionEntry,
    persist,
) -> dict[str, str]:
    """Run the optional copy-paste LLM-assist loop, returning final answers.

    Both sharpen and critique passes return JSON field edits; after each, the
    user is offered another pass with a different LLM. Sources are not changed by
    assist. Every pass is saved to the dev log and the session draft.
    """
    print("\nLLM assist (optional): copy a request, paste it into any LLM, paste the reply back.")
    events.append("assist loop entered")
    pass_no = 0
    while True:
        choice = prompt_menu(
            "assist",
            "Get LLM help refining your answers, or skip to generate (b = back to the interview).",
            (
                ("sharpen", ("a",), "ask an LLM to sharpen your answers"),
                ("critique", ("c",), "ask an LLM to critique the draft and return edits"),
                ("skip", ("s",), "skip and generate now"),
            ),
            "skip",
            allow_back=True,
        )
        if choice is BACK:
            events.append("assist: back to interview")
            return BACK
        if choice == "skip":
            events.append("assist: skipped to generate")
            return answers
        kind = "sharpen" if choice == "sharpen" else "critique"
        pass_no += 1
        answers = run_assist_pass(
            settings, answers, sources, kind, history, events, devlog, active, persist, pass_no
        )
        # After applying a reply, only ask whether to refine again with another LLM; on no/back,
        # fall through to the assist menu (refine answers / refine prompt / skip).
        while True:
            again = ask_yes_no(
                f"Refine with another LLM (paste another {kind} reply)?", "n", allow_back=True
            )
            if again is True:
                pass_no += 1
                events.append(f"assist: another {kind} pass (different LLM)")
                answers = run_assist_pass(
                    settings, answers, sources, kind, history, events, devlog, active, persist, pass_no
                )
                continue
            break


# ---- Run orchestration ------------------------------------------------------
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
    """Ask the run questions (navigable, with source registry and optional assist).

    Every answer and every registry change is saved to the active session's draft
    as it happens, via a persist callback, so nothing is lost if the run is
    interrupted. ``reprint_source`` is the last completed run used by r/last.
    """
    mode = ask_mode(settings, history, sessions, drafts, devlog, reprint_source=reprint_source)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    if defaults_source is not None and defaults_source.sources:
        sources = [SourceEntry.from_dict(src.to_dict()) for src in defaults_source.sources]
    else:
        sources = default_seed_sources()

    def persist(current_answers: dict[str, str]) -> None:
        """Save the current answers and sources as the active session's draft."""
        drafts[active.root] = draft_from_answers(current_answers, sources, mode, active.root, defaults_source)
        write_log(settings, history, sessions, drafts, devlog)

    persist({})  # record the mode + seeded sources immediately under the session

    assist_on = is_enabled(settings["assist_mode"])
    answers: dict[str, str] = {}
    stage = "interview"
    # Back-chain: interview <-> assist <-> confirm. `b` at any stage steps back to the
    # previous stage; re-entering the interview lands on the last question with prior
    # answers prefilled, so `b` walks back to edit anything, then Enter moves forward.
    while True:
        if stage == "interview":
            start_index = 0 if not answers else len(INTERVIEW_STEPS) - 1
            answers = run_interview(defaults_source, sources, persist, answers, start_index)
            stage = "assist" if assist_on else "confirm"
        elif stage == "assist":
            result = run_assist_loop(settings, answers, sources, history, events, devlog, active, persist)
            if result is BACK:
                stage = "interview"
                continue
            answers = result
            stage = "confirm"
        else:  # confirm
            run = entry_from_answers(answers, sources, mode, timestamp, active.root)
            usable = sum(1 for src in run.sources if src.is_usable())
            print(
                f"using: session {active.name} [{active.root}] | target {run.target_agents} | "
                f"sources {usable}/{len(run.sources)} usable | assist {settings['assist_mode']} | "
                f"clipboard {settings['copy_to_clipboard']} | backup {settings['backup_mode']}"
            )
            show_review(run)
            exists = skill_dir_exists(run.skill_slug)
            if run.build_intent == "new" and exists:
                print(
                    f"  Warning: .agents/skills/{run.skill_slug}/ already EXISTS but build intent is "
                    "'new'. The prompt will tell the agent to stop and ask before overwriting; consider "
                    "build intent 'update' or a different slug."
                )
            elif run.build_intent == "update" and not exists:
                print(
                    f"  Warning: build intent is 'update' but .agents/skills/{run.skill_slug}/ does not "
                    "exist yet; the agent will have nothing to update."
                )
            decision = ask_yes_no("Generate the prompt with these answers?", "y", allow_back=True)
            if decision is BACK:
                stage = "assist" if assist_on else "interview"
                continue
            if not decision:
                raise QuitRequested
            return run


def show_review(run: RunEntry) -> None:
    """Print the final review screen: every answer plus the full source list."""
    print("\n==== REVIEW - confirm before generating ====")
    print(f"  skill name:   {run.skill_name}")
    print(f"  skill slug:   {run.skill_slug}")
    print(f"  build intent: {run.build_intent}")
    print(f"  purpose:      {run.skill_purpose}")
    print(f"  target:       {run.target_agents}")
    print(f"  category:     {run.skill_category}")
    print(f"  triggers:     {run.trigger_conditions}")
    print(f"  examples:     {run.example_tasks}")
    print(f"  negatives:    {run.negative_triggers}")
    print(f"  workflow:     {run.user_workflow}")
    print(f"  files:        {run.files_to_generate}")
    print(f"  doc depth:    {run.doc_depth}")
    print(f"  verification: {run.verification_requirements}")
    print(f"  gotchas:      {run.known_gotchas}")
    print(f"  quality:      {run.quality_rubric}")
    print(f"  observation:  {resolved_observation_method(run)}")
    print(f"  privacy:      {run.privacy_constraints}")
    print(f"  out of scope: {run.out_of_scope}")
    print(f"  risk level:   {run.risk_level}")
    print(f"  src license:  {run.source_license}")
    print(f"  parallelism:  {run.parallelism}")
    print(f"  memory policy:{run.memory_file_policy}")
    print(f"  subagent rev: {run.subagent_review_policy}")
    print("  sources:")
    if run.sources:
        for index, src in enumerate(run.sources, start=1):
            print(source_summary_line(index, src))
    else:
        print("    (none)")
    print("============================================")


# ---- Paths for the generated prompt -----------------------------------------
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
    stored run timestamp keeps the archive/result-log filenames reproducible
    when r/last rebuilds a previous prompt.
    """
    return timestamp.replace("-", "").replace(":", "").replace(" ", "-")


def build_workflow_paths(settings: OrderedDict[str, str], run: RunEntry) -> tuple[str, str]:
    """Build ABSOLUTE (POSIX) prompt-archive and result-log paths for the run.

    Folder settings are declared repo-relative; they are resolved to absolute here so the
    generated prompt shows fully-qualified destinations.
    """
    stem = f"{filename_stamp(run.timestamp)}-{SCRIPT_STEM}-{run.skill_slug}"
    archive_dir = resolve_local_path(settings["prompt_archive_folder"]).resolve()
    result_dir = resolve_local_path(settings["result_log_folder"]).resolve()
    return (archive_dir / f"{stem}.md").as_posix(), (result_dir / f"{stem}-results.md").as_posix()


# ---- Build prompt + verify --------------------------------------------------
def render_sources_block(sources: list, fetch_dir_abs: str = "") -> str:
    """Render the registered sources as a numbered block for the prompt."""
    if not sources:
        return "(no sources registered)"
    lines = []
    for index, src in enumerate(sources, start=1):
        lines.append(
            f"{index}. {src.label} — type: {src.stype}; priority: {src.priority}; trust: {src.trust}\n"
            f"   location: {src.location}\n"
            f"   intended use: {src.intended_use or 'not specified'}\n"
            f"   content handling: {src.content_instruction(fetch_dir_abs)}"
        )
    return "\n".join(lines)


def claude_adaptation_note(target_agents: str, slug: str) -> str:
    """Note the single-copy/junction reality when Claude is a target (no second copy to create)."""
    if target_agents not in {"Claude", "both"}:
        return ""
    skill_dir = abs_repo_path(f".agents/skills/{slug}")
    claude_dir = abs_repo_path(f".claude/skills/{slug}")
    return (
        "\n## One copy, both agents (no mirror)\n"
        f"This skill is a single package. {skill_dir} (Codex) and {claude_dir} (Claude) are "
        "**per-skill directory junctions to the same store copy** — build it once and both agents see it "
        "automatically. Do NOT create a second copy or 'mirror'; if any runtime wording is agent-specific, "
        "edit the one copy.\n"
    )


def ledger_reasons(run: RunEntry) -> list:
    """Return human-readable reasons this build warrants a resumable ledger.

    A ledger is mandated for large, multi-batch ingestions: any URL source (web
    documentation preparation, which can span many pages) or any local PDF whose
    page count exceeds ``LEDGER_PAGE_THRESHOLD`` or is unknown. Returns an empty
    list when no source warrants one.
    """
    reasons = []
    for src in run.sources:
        name = src.label or src.location
        if src.is_url():
            reasons.append(f"{name} (URL documentation)")
        elif src.is_pdf():
            if src.approx_pages == 0:
                reasons.append(f"{name} (PDF, page count unknown)")
            elif src.approx_pages > LEDGER_PAGE_THRESHOLD:
                reasons.append(f"{name} (PDF, ~{src.approx_pages} pages)")
    return reasons


def build_needs_ledger(run: RunEntry) -> bool:
    """Return True when the build is large enough to require a resumable ledger."""
    return bool(ledger_reasons(run))


def build_prompt(settings: OrderedDict[str, str], run: RunEntry, run_number: int) -> str:
    """Render the paste-ready skill-package build-prompt for a run.

    Assembled by concatenating field-interpolated prose sections with the fixed
    JSON-schema constants, so the schemas' braces never collide with formatting.
    The attribution model and several clauses switch on the source license posture
    (digest-and-attribute vs inspiration-only) and on the build intent (new vs update).
    Deterministic: identical answers produce an identical body (timestamps only
    affect the archive/result filenames).
    """
    slug = run.skill_slug
    slug_us = slug.replace('-', '_')
    agents_abs, claude_abs = memory_paths()
    # The single agent that will EXECUTE this prompt, derived from target_agents:
    # Codex-only -> Codex; Claude-only or both -> Claude. The prompt is tailored to it
    # (identity, memory file, native tools, usage-monitoring method).
    executor = "Claude" if run.target_agents in ("Claude", "both") else "Codex"
    if executor == "Codex":
        memory_line = (
            f"- Follow the repo conventions in {agents_abs} (Codex's memory file) and the aligned "
            f"{claude_abs}, plus the .agents folder."
        )
    else:
        memory_line = (
            f"- Follow the repo conventions in {claude_abs} (Claude's memory file) and the aligned "
            f"{agents_abs}, plus the .agents folder."
        )
    native_tools = "Codex's `apply_patch`" if executor == "Codex" else "your native Write/Edit tools"
    # Repo-anchored locations are DECLARED relative and emitted absolute (POSIX) in the prompt.
    skill_dir = abs_repo_path(f".agents/skills/{slug}")
    claude_skill_dir = abs_repo_path(f".claude/skills/{slug}")
    agents_skills_dir = abs_repo_path(".agents/skills")
    claude_skills_dir = abs_repo_path(".claude/skills")
    skill_drafting_dir = abs_repo_path(".agents/skills/skill-drafting")
    claude_skill_drafting_dir = abs_repo_path(".claude/skills/skill-drafting")
    work_dir = abs_repo_path("AI/work")
    fetch_dir = abs_repo_path(f"AI/work/{slug}-docs")
    # Point at the full Kaizen System file for complete context, but only when it exists at the
    # repo root AND it is not already a registered source (avoid duplicate declarations).
    method_file = "Kaizen_System.md"
    method_in_sources = any(method_file in (src.location or "") for src in run.sources)
    if local_path_exists(method_file) and not method_in_sources:
        kaizen_system_ref = (
            "\n- Full system: READ THIS FILE for the complete Kaizen System (the lines above are only a "
            f"summary) — {abs_repo_path(method_file)}"
        )
    else:
        kaizen_system_ref = ""
    prompt_archive_path, result_log_path = build_workflow_paths(settings, run)
    sources_block = render_sources_block(run.sources, fetch_dir)
    risk_guidance = RISK_GUIDANCE.get(run.risk_level, RISK_GUIDANCE[DEFAULT_RISK])
    license_mode = LICENSE_MODE_BY_LICENSE.get(run.source_license, "inspiration")
    license_guidance = LICENSE_GUIDANCE.get(run.source_license, LICENSE_GUIDANCE[DEFAULT_SOURCE_LICENSE])
    verifier_path = abs_repo_path(f"AI/work/verify_{slug_us}.py")
    ledger_path = abs_repo_path(f"AI/work/{slug_us}_build_ledger.md")
    observation_method = resolved_observation_method(run)
    category = category_label(run.skill_category)

    memory_policy_guidance = {
        "assess-then-ask": (
            "Inspect AGENTS.md and CLAUDE.md, decide whether tool, hook, guardrail, or skill-routing "
            "updates are needed, and PROPOSE those edits only. Do not edit memory files unless I explicitly approve."
        ),
        "read-only": (
            "Read AGENTS.md and CLAUDE.md as context only. Do not propose or make memory-file edits unless a blocker appears."
        ),
        "edit-with-explicit-approval": (
            "Assess AGENTS.md and CLAUDE.md and ask for explicit approval before any memory-file edit."
        ),
    }.get(run.memory_file_policy, "")

    if run.subagent_review_policy == "nontrivial":
        subagent_review_block = (
            "\n## Independent subagent review and forward-testing\n"
            "- This is a nontrivial skill build. If your runtime exposes an explicit subagent/worker mechanism, "
            "use independent reviewers before finalizing: one for skill architecture/trigger boundaries and one "
            "for the quality harness/verification risks. Give them raw artifacts, not your intended fix.\n"
            "- Treat subagents as reviewers or testers. They may produce notes, critique, source digests, or "
            "forward-test results, but the orchestrator owns all shipped file writes.\n"
            "- If no subagent mechanism is available, say so and run separated self-review passes for architecture "
            "and quality. Do not imply that independent review happened.\n"
        )
    elif run.subagent_review_policy == "high-risk-only":
        subagent_review_block = (
            "\n## Independent subagent review and forward-testing\n"
            "- Use independent subagent review when the skill is high-risk, source-heavy, public/team-shared, "
            "or overlaps other skills. Otherwise run a separated self-review pass and report why that was enough.\n"
        )
    elif run.subagent_review_policy == "optional":
        subagent_review_block = (
            "\n## Independent review option\n"
            "- Consider subagent review for architecture or quality risks. If you skip it, state the reason and "
            "compensating self-checks.\n"
        )
    else:
        subagent_review_block = ""

    if build_needs_ledger(run):
        ledger_reasons_text = "; ".join(ledger_reasons(run))
        ledger_block = (
            "\n## Resumable ledger (large / multi-batch ingestion — MANDATORY here)\n"
            f"- This build ingests large sources ({ledger_reasons_text}), so it will not fit in one pass. "
            f"Keep a resumable progress ledger at {ledger_path} (under {work_dir}, gitignored, build-time "
            "only — NOT part of the shipped skill package).\n"
            "- Seed it during Pass 1 with one row per planned reference/topic, then UPDATE it as you go. Each "
            "row records: topic, target reference file, which source section(s) / page-range it covers, and a "
            "status of `pending` -> `drafting` -> `drafted` -> `verified`.\n"
            "- Update the ledger immediately after finishing each reference (and after its fidelity check), "
            "before starting the next, so progress is never held only in your context.\n"
            "- On resume (a new session, or after a usage/context limit) re-read the ledger FIRST and continue "
            "from the first row that is not `verified`; never restart completed work or silently skip a row.\n"
            "- Tie this to Usage monitoring below: as you near a limit, finish the current reference, update its "
            "ledger row, checkpoint, and pause at a clean row boundary.\n"
        )
    else:
        ledger_block = ""

    if run.parallelism == "opus":
        worker = "parallel Opus subagents (launch each with model: opus)"
        worker_note = "Opus subagents give the strongest reasoning for faithful digestion."
    elif run.parallelism == "haiku":
        worker = "parallel Haiku subagents (launch each with model: haiku)"
        worker_note = ("Haiku subagents are fast and cheap for bulk fetch+digest; keep hard "
                       "reasoning and reconciliation for yourself.")
    elif run.parallelism == "generic":
        worker = "parallel subagents/workers"
        worker_note = "Use whatever subagent/worker mechanism your runtime provides."
    else:
        worker = ""
    if worker:
        parallelism_block = (
            "\n## Agent parallelism (a registered source is large — partition and parallelize)\n"
            "- A registered source is large; do not ingest it all yourself in one sequential pass. Act as the "
            "ORCHESTRATOR.\n"
            "- First enumerate the source's table of contents / index, then partition it into DISJOINT topic "
            "batches (roughly one per planned reference file); record the partition in the ledger before dispatching.\n"
            f"- Dispatch {worker}, one per disjoint batch. {worker_note} Each subagent: fetches ONLY its assigned "
            f"pages/sections (and relevant in-domain links) deeply, saves its extracted notes under {fetch_dir} (its "
            "own files, no overlap), obeys the Fidelity + Originality + per-file budget rules above, and RETURNS a "
            "digested, ORIGINAL-prose draft plus its preserve-glossary terms and any TODO gaps. Subagents do NOT "
            "write into the shipped skill package.\n"
            "- The ORCHESTRATOR owns ALL writes: review each draft, write/normalize the reference files, maintain "
            "the ledger, assemble SKILL.md / INDEX.md / topics.json, reconcile cross-references, and run the "
            "verifier — this avoids write races and keeps one consistent voice.\n"
            "- Keep batches disjoint; after each wave, update the ledger, run the verifier on what exists, and "
            "check usage before dispatching the next wave.\n"
        )
    else:
        parallelism_block = ""

    if run.build_intent == "update":
        intent_line = (
            f"Build intent: UPDATE the existing skill at {skill_dir}. Read what is "
            "already there first; extend or refresh only what the sources/changes require; PRESERVE "
            "unrelated existing content, structure, and file names; do not regress working material."
        )
    else:
        intent_line = f"Build intent: author a NEW skill package at {skill_dir}."

    if license_mode == "digest":
        attribution_files_line = (
            "- `references/source-map.json` — attribution metadata (schema below): one entry per "
            "reference file mapping it to the source(s) it draws from."
        )
        attribution_self_check = (
            "Attribution: every reference file maps to its source(s) in `references/source-map.json`; "
            "any retained excerpt is justified in that entry's `license_note`."
        )
        no_dump_clause = "No raw dumps unless you justify the excerpt in that file's `license_note`."
        originality_block = ""
        pass1_sourcemap = ", `references/source-map.json`"
    else:
        attribution_files_line = (
            "- Do NOT create `references/source-map.json` and do NOT add `license_note` fields. "
            "Instead add an `## Inspired by` section to SKILL.md (see Originality below)."
        )
        attribution_self_check = (
            "Originality: no reference reproduces source sentences, headings, ordering, tables, or "
            "example wording; SKILL.md has an `## Inspired by` section listing sources as inspiration "
            "only; there is NO source-map.json and NO license_note anywhere in the package."
        )
        no_dump_clause = "Reproduce no source text at all (inspiration-only mode); write original prose."
        pass1_sourcemap = ""
        originality_block = (
            "\n## Originality (inspiration-only mode — write original content informed by the sources)\n"
            "- Treat the registered sources as inspiration, not copy material (see the license posture "
            "stated above).\n"
            "- This is a HOW-TO-WRITE rule, not a stop: still build the full skill — just express it in "
            "your own words and structure instead of copying the source text. Do not refuse, water down, "
            "or add disclaimers; produce complete, useful references.\n"
            "- Write every reference from understanding, in your own words; reproduce no sentences, "
            "section orderings, tables, or example phrasings from the sources.\n"
            "- This is NOT a license to change facts: re-express PROSE freely, but keep NAMES, "
            "COMMANDS, SIGNATURES, MANIFEST KEYS, PERMISSION STRINGS, ENUM/CONSTANT VALUES, "
            "CONFIG KEYS, and NUMBERS exactly as the source has them, and reproduce runnable "
            "code blocks verbatim. Originality applies to prose, never to code (see Fidelity).\n"
            "- Add an `## Inspired by` section near the end of SKILL.md: a one-line statement that the "
            "content is original, then one bullet per source naming it as inspiration only.\n"
        )

    if executor == "Claude":
        usage_block = (
            "\n## Usage monitoring (for a long or batched build)\n"
            "- This can be a multi-batch job; watch your remaining model usage and checkpoint before you "
            "run out, rather than stopping mid-write.\n"
            "- Prefer a bundled read-only checker if present. Run the first existing script after each "
            "batch/pass: "
            f"`{skill_drafting_dir}/scripts/claude_usage_check.py` or "
            f"`{claude_skill_drafting_dir}/scripts/claude_usage_check.py`.\n"
            f"- If neither bundled script exists, write the fallback below to `{work_dir}/claude_usage_check.py` "
            "and run it after each batch/pass. It calls `https://api.anthropic.com/api/oauth/usage` with "
            "`anthropic-beta: oauth-2025-04-20` and prints only known utilization windows such as "
            "`five_hour` and `seven_day`.\n"
            "- SECURITY (mandatory): the credentials file and token are SECRET. Read them ONLY to call "
            "the usage endpoint. NEVER print, echo, log, commit, or write the token or the raw "
            "credentials into any file or the work log; surface only the resulting percentages. Treat "
            "`~/.claude/.credentials.json` as read-only and follow loaded private policy context.\n"
            "```python\n"
            f"{CANONICAL_CLAUDE_USAGE_CHECKER}\n"
            "```\n"
        )
    else:
        usage_block = (
            "\n## Usage monitoring (for a long or batched build)\n"
            "- This can be a multi-batch job; watch your remaining model usage and checkpoint before you "
            "run out, rather than stopping mid-write.\n"
            "- Prefer a bundled read-only checker if present. Run the first existing script after each "
            "batch/pass: "
            f"`{skill_drafting_dir}/scripts/codex_usage_check.py` or "
            f"`{claude_skill_drafting_dir}/scripts/codex_usage_check.py`.\n"
            f"- If neither bundled script exists, write the fallback below to `{work_dir}/codex_usage_check.py` "
            "and run it after each batch/pass. It launches `codex app-server --listen stdio://`, sends "
            "the JSON-RPC method `account/rateLimits/read`, and prints `usedPercent`, "
            "`windowDurationMins`, `resetsAt`, optional `credits`, and plan metadata from the returned "
            "rate-limit buckets. Ref: https://developers.openai.com/codex/app-server\n"
            "- Default behavior is read-only. Use `--login` only when the user explicitly asks to "
            "authenticate; it runs `codex login --device-auth` first, then retries the read. Device-code "
            "login requires ChatGPT Settings -> Codex -> `Enable device code authorization for Codex` "
            "to be enabled. Use `--login-new-window` only when the user needs a separate Windows Command "
            "Prompt for the login ceremony.\n"
            "- The Codex agent sandbox blocks direct egress to `https://chatgpt.com/backend-api/wham/usage` by default, so an in-sandbox agent cannot read Codex usage on its own; ask the "
            "user to run the sibling `codex_usage_bridge.cmd` launcher or "
            "`python codex_usage_check.py --run-bridge --login-if-needed`. The one-click launcher "
            "uses `--login-if-needed`, so a first-time user gets the device-code login before the "
            "bridge starts. The bridge starts `codex app-server --listen ws://127.0.0.1:17342`, "
            "writes `AI/work/codex-usage-bridge.json`, stays visible so the user can minimize it, "
            "and stops when the user presses any key in that window. Normal checker runs auto-detect "
            "that status file and query the user's authenticated local app-server over loopback; "
            "callers can also pass `--app-server-url ws://127.0.0.1:17342` explicitly.\n"
            "- For a shared repo-local login, the user can run the checker with `--repo-local-home "
            "--login`; the agent can then retry with `--repo-local-home`. This persists Codex state "
            "under `AI/work/codex-home-agent`. Before writing local Codex state, the checker creates "
            "or repairs `AI/work/.gitignore` so `AI/work` contents stay untracked by default.\n"
            "- SECURITY (mandatory): never print, echo, log, commit, or write tokens, raw credentials, "
            "or raw app-server responses. Surface only the formatted utilization fields, or use "
            "`--show-response-shape` for a sanitized shape tree while validating response changes.\n"
            "```python\n"
            f"{CANONICAL_CODEX_USAGE_CHECKER}\n"
            "```\n"
        )

    if run.target_agents == "Codex":
        discoverability_line = f"- Confirm the skill is discoverable by Codex under {agents_skills_dir}."
    elif run.target_agents == "Claude":
        discoverability_line = (
            f"- Confirm the skill is discoverable under {agents_skills_dir}; the same copy is surfaced to "
            f"Claude under {claude_skills_dir} via its per-skill junction."
        )
    else:
        discoverability_line = (
            f"- Confirm the skill is discoverable by Codex (under {agents_skills_dir}) and by Claude "
            f"(under {claude_skills_dir}) — the same copy via per-skill junctions."
        )

    header = f"""You are working in this VS Code repository as {executor}. You can read and write files in this repository.

## Goal
Author a high-quality skill package for "{run.skill_name}" at {skill_dir}. The skill must give an agent {run.skill_purpose}. The core of the work is DEEPLY DIGESTED, TASK-ORGANIZED documentation: study the registered sources and rewrite them into concise, LLM-optimized Markdown references organized by the skill's concerns — never raw dumps, never one flat file per source, never padding.

{intent_line}

## Problem this solves
Generic "make me a skill" output is shallow: a thin SKILL.md and un-digested references that waste tokens and under-inform the agent. Worse, a careless rewrite silently RENAMES real identifiers, invents facts, or flips directions — the failure that ruined earlier documentation skills in this repo. This prompt forces research, faithful re-expression, task organization, and verification instead of copy-paste.

## Context and required repo inspection
{memory_line}
- Inspect the existing skills under {agents_skills_dir} first (for example powershell-vsdevshell and cli-design) to match structure and quality, but do not copy any other skill's internal details into this one.
- Use the skill-drafting contract as the quality bar: read {skill_drafting_dir}/SKILL.md and its relevant references ({claude_skill_drafting_dir} is the same file via Claude's per-skill junction).
- For each URL source below, FETCH it and follow its relevant in-domain links deeply, saving the extracted notes under {fetch_dir}/ before you digest. Use only real content (fetched pages or local files) — do not invent facts, flags, names, or numbers.

## Kaizen System operating model
{KAIZEN_SYSTEM_SUMMARY}{kaizen_system_ref}

You (the agent executing this prompt) are {executor}; ship-to target(s): {run.target_agents}.
Cost of error is {run.risk_level}: {risk_guidance}
Source license posture is {run.source_license}: {license_guidance}
Skill category is {run.skill_category} ({category}). If the skill spans categories, state the primary category and why in the spec.
Memory-file policy is {run.memory_file_policy}: {memory_policy_guidance}
Subagent review policy is {run.subagent_review_policy}.

## Staged collaboration gates (mandatory before nontrivial writes)
- Gate 1: show goal, consumers, scope, out of scope, examples, and cost of error.
- Gate 2: show skill category, positive triggers, paraphrased triggers, negative triggers, and overlap boundaries.
- Gate 3: show package anatomy, reference split, scripts/assets, setup/config, and memory-file impact.
- Gate 4: show quality harness, observation artifact, verifier design, and forward-test/subagent plan.
- Gate 5: show final implementation plan, exact files, acceptance criteria, and preservation/rollback rules.
- Ask precise questions whenever a gate has a real decision. Do not ask me to decide details already answered by repo conventions.

## Registered sources (study each; organize the OUTPUT by topic, not by source)
{sources_block}

## Fidelity — re-express prose, preserve facts verbatim (READ TWICE)
- ABSOLUTE RULE: code is never recontextualized. Recontextualization and condensation apply ONLY to explanatory prose. Reproduce VERBATIM, character-for-character, never paraphrased, modernized, reformatted, re-indented, or pluralized: API/class/method/property/function names; function signatures, parameter names, and parameter order; manifest keys; permission strings; enum/constant/config-key/schema-key values; CLI commands/subcommands/flags; environment variables; file paths and extensions; version values; numeric values WITH their units; UI labels; keyboard shortcuts; exit codes; formulas; and ENTIRE runnable code blocks (JS/TS/JSON/HTML/shell/Python or similar). Copy code blocks; do not rewrite them. One altered token is a real coding error.
- You MAY freely rewrite sentences and reorganize. You MUST carry these across UNCHANGED — never renamed, "modernized", pluralized, abbreviated, or paraphrased into a near-synonym: proper names; API/class/method/property names; command names, subcommands, and flags; UI/menu/option/mode labels; keyboard shortcuts; file paths and extensions; environment variables; formulas; numeric values WITH their units; enum/constant values; exit codes; version numbers.
- Never invert or alter a direction, comparison, ordering, default, unit, or cause-and-effect relationship.
- Apply the STRICTEST fidelity to `api` and `official-docs` sources (their names, signatures, and numbers are load-bearing); `tutorial`/`note` sources may be summarized more loosely but still must not rename or fabricate.
- If a source lacks something, write a `TODO:` note — never fill the gap with a plausible-sounding invention.

## Pass 0 — build a preserve-glossary first
Before writing any reference, extract from the sources a "preserve list": every identifier, command/flag, signature, parameter name/order, runnable code block, manifest key, permission string, config key, labelled UI element, enum/constant value, schema key, formula, path, version, and number-with-unit that must appear verbatim. Keep it as a working artifact (record it in the work log). Author the references, then in Pass 2 confirm each glossary term still appears unchanged. This is your own ground truth against accidental renaming.

## Files to create under {skill_dir}
- `SKILL.md` — YAML front matter with `name` and a trigger-rich `description`; a body with when-to-use triggers, a task-router table, the runtime workflow, and Verification notes. Keep the body focused and short (aim for ≤ ~500 lines). Respect progressive disclosure: the YAML name+description always load, the SKILL.md body loads when the skill triggers, and references load only when the router points to them — so put routing in SKILL.md and depth in references.
- `references/` — the documentation folder, organized BY TOPIC/CONCERN (one file per coherent concern). A reference may synthesize several sources, and one source may feed several references; do NOT mechanically emit one file per source.
- `references/INDEX.md` — a start-here index listing every reference file with a one-line summary.
- `references/topics.json` — topic metadata (schema below).
{attribution_files_line}
- `{verifier_path}` — a small stdlib-only ground-truth verifier that checks structural invariants (every reference is in INDEX.md and has exactly one topics.json entry; cross-links resolve; each reference has its required sections) and greps the Pass-0 preserve-glossary terms against the shipped references; reference it from SKILL.md "Verification notes".
Requested file set from the user (advisory; the set above is mandatory): {run.files_to_generate}.

## SKILL.md description and triggers
- Make the `description` deliberately pushy and trigger-rich so the agent does not under-trigger the skill. Name the task AND concrete trigger phrases, tool/library names, and file types that should fire it EVEN WHEN the user never says the skill's name.
- Frontmatter must be valid YAML for strict skill loaders. Use `name` as kebab-case matching the folder. Keep the rendered `description` at or below 1024 characters unless the target runtime documents a different limit. Use folded scalar YAML (`description: >-`) for multi-clause descriptions, especially descriptions containing colons, quotes, commas, paths, API names, file names, or negative-trigger boundaries. Verify the frontmatter parses cleanly before claiming the skill package is loadable; move excess trigger detail into the body or references instead of overloading `description`.
- Example shape (adapt, do not copy): "Use when <doing X> — <subtask a>, <subtask b>, ...; also fires when working with <concrete tool/library/file-type names>, even when the user never says \\"{run.skill_name}\\"."
- Triggers to encode: {run.trigger_conditions}.
- Trigger examples to use in tests: {run.example_tasks}.
- Negative triggers / overlap boundaries to encode: {run.negative_triggers}.
- Add explicit "Do not use when" or equivalent boundary wording when overlap is likely.
- Add a task-router table to SKILL.md mapping each concern to the reference file to read, so the agent loads only what it needs. Shape:
  | If the task is about... | Read |
  | --- | --- |
  | <a concern> | references/<file>.md |
- Expected runtime workflow: {run.user_workflow}.

## Gotchas and quality harness
- Add a `## Gotchas` section to SKILL.md. It must name concrete recurring failure modes and what the agent should do instead: {run.known_gotchas}.
- Before implementation, define a task-specific quality harness that can reject bad output, not just praise good output.
- Good-vs-bad rubric to apply: {run.quality_rubric}.
- Observation artifact while building: {observation_method}. Capture or describe the artifact in the work log and use it to adjust the skill before finalizing.
- Include trigger tests for direct trigger, paraphrased trigger, and negative trigger; include at least one workflow/output test that exercises the skill's main job.

## Documentation depth and digestion rules (per-file budget)
- {run.doc_depth}.
- Chunk each reference under H2/H3 headings; keep one example per concept; remove boilerplate and repetition.
- {no_dump_clause}
- Stay within this per-file budget; if a topic is large, split it across focused files rather than padding one.
{originality_block}
"""

    if license_mode == "digest":
        schema_block = (
            "## JSON schemas — use these shapes verbatim\n"
            + SOURCE_MAP_SCHEMA
            + "\n\n"
            + TOPICS_SCHEMA
            + "\n"
        )
    else:
        schema_block = (
            "## JSON schema — use this shape verbatim (inspiration mode ships NO source-map.json)\n"
            + TOPICS_SCHEMA
            + "\n"
        )

    checks_block = f"""
## Binary self-checks you MUST perform before finishing
1. Structure: every reference file is listed in `references/INDEX.md` and has exactly one `references/topics.json` entry; references are organized by topic (not one flat file per source); SKILL.md has a task-router table and stays within its length budget.
2. {attribution_self_check}
3. Fidelity diff: re-read each reference against its source AND your Pass-0 preserve-glossary; confirm every glossary term (names, commands, signatures, parameter names/order, runnable code blocks, manifest keys, permission strings, enum/constant/config/schema keys, numbers-with-units, formulas, shortcuts, paths, versions) appears UNCHANGED and with the same meaning, and that no direction, comparison, or cause-and-effect was flipped. List any altered token and either revert it or justify it in the work log.
4. Each reference stays within the per-file budget above (chunked H2/H3, one example per concept, no padding).
5. No invention: all content derives ONLY from the supplied sources; gaps are marked `TODO`, never filled with guesses. Zero unresolved placeholders remain in any file.
6. Stop and ask before any overwrite or delete: check whether {skill_dir} already exists; if it does, STOP AND ASK for explicit confirmation before you overwrite, modify, or delete any existing file. Never overwrite or delete user files without confirmation.
7. The verifier script (`{verifier_path}`) exists, runs clean, and its checks are described in SKILL.md "Verification notes".
8. Verification requirements from the user are met: {run.verification_requirements}.
9. Skill-drafting gates: the spec includes staged gates, skill category, positive trigger, paraphrased trigger, negative trigger, overlap boundary, and memory-file assessment.
10. Quality harness: the package includes Gotchas, good-vs-bad rubric, observation artifact, trigger tests, negative trigger test, workflow/output test, and a clear bad-output rejection rule.
11. Independent review: subagent review or the explicit unavailable-subagent self-review fallback is reported according to the policy above.

## EXCLUDE / Out of scope
- Out of scope for this skill: {run.out_of_scope}.
- Do not add anything outside the skill's one task; keep the package focused.

## Privacy and secret handling
- {run.privacy_constraints}.
- Memory-file assessment: inspect AGENTS.md and CLAUDE.md for skill routing, tool enablement, hooks, guardrails, and permission rules. Apply the memory-file policy above; unless explicitly approved, propose memory-file changes in the spec or final report instead of editing them.
- This applies to EVERY file you create — references, metadata, examples, and the work log; never paste a real secret/token/credential into an example, and note any redaction.

## Final discoverability self-check
- Confirm the SKILL.md `description` would trigger for the listed conditions even when the user does not name the skill.
{discoverability_line}
- Confirm `references/INDEX.md` and the SKILL.md task-router table give a clear start-here path into the docs.
{claude_adaptation_note(run.target_agents, slug)}
## Tooling discipline (avoid needless approval prompts)
- Create and edit EVERY file through {native_tools} — NOT by shelling out. Do not pipe or redirect into files or use ad-hoc shell utilities (`sed`, `awk`, `tee`, `echo >`, `Set-Content`, `>`/`>>`) to write or mutate files; those trigger repeated permission prompts and are error-prone.
- Prefer your native Read/search tools over shell `cat`/`grep` for inspection. Use the shell only when genuinely required (e.g., running the verifier script), and batch such commands.
- This keeps edits reviewable and avoids interrupting me for approval on every file operation.

## Build in passes
- Pass 0: extract the preserve-glossary from the sources (see Fidelity above).
- Pass 1: organize references by topic, then draft SKILL.md (with the task-router table), every reference file, `references/topics.json`{pass1_sourcemap}, and the verifier script.
- Pass 2: run the binary self-checks above — including the fidelity diff and the verifier script — and fix every gap before presenting. Report pass/fail per check.
- Pass 3 (for a complex or high-stakes skill): run the finished package past a second model (or this repo's Codex plugin) and reconcile any disagreement before presenting.
{subagent_review_block}{parallelism_block}{ledger_block}{usage_block}
## Prompt/result logging for this run
- Save the exact prompt you received to {prompt_archive_path}.
- After completing the work, write a Markdown work log to {result_log_path} (include the preserve-glossary and the fidelity-diff results).
- When naming repo locations use absolute paths; for links INSIDE the skill package (e.g. references/INDEX.md) use relative links so the shipped skill stays portable.

## Acceptance criteria
- The skill package is complete, concise, discoverable, task-organized, and faithful: every fact traces to a supplied source and every preserved name/number/command is verbatim.
- Before writing files, outline the precise evaluation criteria you will use; make each observable and binary.
- Make me verify key decisions explicitly to ensure nothing is missed.

## Checkpoint
Before non-trivial writes, show me the spec, key decisions, assumptions, acceptance criteria, and verification plan. If a required decision is missing, ask a precise question instead of guessing."""

    return header + schema_block + checks_block


def build_output(settings: OrderedDict[str, str], run: RunEntry, run_number: int) -> str:
    """Wrap the generated prompt in terminal markers and verify it."""
    prompt = build_prompt(settings, run, run_number)
    verify_prompt(run, prompt)
    return "\n".join((OPENING_MARKER, prompt, CLOSING_MARKER))


def build_output_safely(settings: OrderedDict[str, str], run: RunEntry, run_number: int) -> str | None:
    """Build the output, or report a verification failure without crashing."""
    try:
        return build_output(settings, run, run_number)
    except RuntimeError as exc:
        print(f"Prompt failed verification and was not emitted: {exc}")
        return None


# Clauses that must appear in EVERY generated prompt regardless of license mode.
REQUIRED_PROMPT_SUBSTRINGS = (
    "savmi",
    "scope:",
    "adapt:",
    "verify:",
    "manage:",
    "improve:",
    "acceptance criteria",
    ".agents/skills/",
    "skill.md",
    "references/index.md",
    "references/topics.json",
    "keywords",
    "stop and ask",
    "exclude",
    "out of scope",
    "privacy",
    "secret",
    "do not invent",
    "todo",
    "discoverab",
    "codex",
    "claude",
    "budget",
    "fidelity",          # the verbatim-preserve block (#1)
    "absolute rule",
    "code is never recontextualized",
    "glossary",          # the Pass-0 preserve-glossary (#7)
    "router",            # the SKILL.md task-router table (#8)
    "frontmatter",
    "description: >-",
    "1024",
    "by topic",          # organize references by topic, not by source (#6)
    "verify_",           # the mandated ground-truth verifier script (#4)
    "second model",      # the output cross-check fragment (#10)
    "usage monitoring",  # the usage-monitoring section (#17)
    "tooling discipline",  # native-tools/no-ad-hoc-shell editing rule (#18)
    "skill-drafting",
    "staged collaboration gates",
    "skill category",
    "paraphrased trigger",
    "negative trigger",
    "overlap boundary",
    "gotchas",
    "quality harness",
    "good-vs-bad",
    "observation artifact",
    "memory-file assessment",
    "subagent review",
    "forward-test",
)
# Clauses required only in digest-and-attribute mode (permissive/owned sources).
DIGEST_ONLY_SUBSTRINGS = (
    "references/source-map.json",
    "source_label",
    "retrieved_or_commit",
    "license_note",
)
# Clauses required only in inspiration-only mode (copyleft/unknown sources).
INSPIRATION_ONLY_SUBSTRINGS = (
    "inspired by",
)

PLACEHOLDER_RE = re.compile(r"\(\s*\)|\{\s*\}|\[\s*\]")
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)


def verify_prompt(run: RunEntry, prompt: str) -> None:
    """Raise RuntimeError if the run or generated prompt fails a binary check.

    Refuses to emit when a required field is empty, the slug is invalid, the
    registry is empty or has an unusable source, an empty placeholder remains,
    an overwrite/delete instruction lacks a stop-and-ask guard, or any required
    clause/schema substring is missing.
    """
    empty = [attr for attr in REQUIRED_FIELDS if not str(getattr(run, attr, "")).strip()]
    if empty:
        raise RuntimeError("required fields are empty: " + ", ".join(empty))
    if not SLUG_RE.match(run.skill_slug):
        raise RuntimeError(f"skill slug is not valid kebab-case: {run.skill_slug!r}")
    if run.target_agents not in TARGET_AGENTS:
        raise RuntimeError(f"target agent(s) invalid: {run.target_agents!r}")
    if run.skill_category not in SKILL_CATEGORY_VALUES:
        raise RuntimeError(f"skill category invalid: {run.skill_category!r}")
    if run.source_license not in SOURCE_LICENSES:
        raise RuntimeError(f"source license posture invalid: {run.source_license!r}")
    if run.build_intent not in BUILD_INTENTS:
        raise RuntimeError(f"build intent invalid: {run.build_intent!r}")
    if run.parallelism not in PARALLELISM_MODES:
        raise RuntimeError(f"parallelism mode invalid: {run.parallelism!r}")
    if run.memory_file_policy not in MEMORY_FILE_POLICIES:
        raise RuntimeError(f"memory-file policy invalid: {run.memory_file_policy!r}")
    if run.subagent_review_policy not in SUBAGENT_REVIEW_POLICIES:
        raise RuntimeError(f"subagent review policy invalid: {run.subagent_review_policy!r}")

    if not run.sources:
        raise RuntimeError("no sources registered; add at least one usable source")
    unusable = [src.label or src.location for src in run.sources if not src.is_usable()]
    if unusable:
        raise RuntimeError("local source(s) not found on disk: " + ", ".join(unusable))

    placeholder_scan_text = FENCED_CODE_RE.sub("", prompt)
    if PLACEHOLDER_RE.search(placeholder_scan_text):
        raise RuntimeError("generated prompt still contains an empty () / {} / [] placeholder")

    lower = prompt.lower()
    license_mode = LICENSE_MODE_BY_LICENSE.get(run.source_license, "inspiration")
    required = list(REQUIRED_PROMPT_SUBSTRINGS) + list(
        DIGEST_ONLY_SUBSTRINGS if license_mode == "digest" else INSPIRATION_ONLY_SUBSTRINGS
    )
    # Usage method follows the EXECUTING agent (derived from target_agents), not the ship-to set.
    executor = "Claude" if run.target_agents in ("Claude", "both") else "Codex"
    if executor == "Claude":
        required.append("api/oauth/usage")   # Claude usage endpoint (#17)
        required.append("oauth-2025-04-20")  # Claude OAuth beta header for usage endpoint (#17)
        required.append("five_hour")         # known Claude usage window (#17)
    else:
        required.append("ratelimits/read")                    # Codex app-server usage method (#17)
        required.append("codex_usage_check.py")               # bundled Codex usage checker (#17)
        required.append("codex_usage_bridge.cmd")             # one-click user bridge launcher (#17)
        required.append("--login")                            # explicit Codex authentication option (#17)
        required.append("--app-server-url")                   # sandbox-compatible user app-server bridge (#17)
        required.append("--repo-local-home")                  # repo-local credential persistence option (#17)
        required.append("--run-bridge")                       # visible bridge lifecycle mode (#17)
        required.append("--login-if-needed")                  # bridge first-run auth preflight (#17)
        required.append("ai/work/.gitignore")                 # repo-local credential safety guard (#17)
        required.append("usedpercent")                        # Codex usage field (#17)
        required.append("enable device code authorization")   # Codex device-code prerequisite (#17)
    if build_needs_ledger(run):
        required.append("resumable ledger")  # large/multi-batch build progress ledger (#19)
    if any(src.is_url() for src in run.sources):
        required.append("fetch")             # URL sources must be fetched, not invented (#20)
    if run.parallelism != "none":
        required.append("agent parallelism")  # parallel-subagent section for large builds (#21)
        if run.parallelism in ("opus", "haiku"):
            required.append(run.parallelism)  # the chosen Claude subagent model is named
    missing = [token for token in required if token not in lower]
    if missing:
        raise RuntimeError("generated prompt missing required text: " + ", ".join(missing))

    if ("overwrite" in lower or "delete" in lower) and "stop and ask" not in lower:
        raise RuntimeError("overwrite/delete instruction lacks a stop-and-ask guard")


# ---- Argparse CLI -----------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for interactive and noninteractive use."""
    parser = argparse.ArgumentParser(
        prog="skill-package-prompt-builder.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Build paste-ready prompts that make Codex or Claude author deeply "
            "digested skill packages."
        ),
        epilog=(
            "Examples:\n"
            "  python .\\prompt-builder-scripts\\ai-support\\skill-package-prompt-builder.py\n"
            "  python .\\prompt-builder-scripts\\ai-support\\skill-package-prompt-builder.py run\n"
            "  python .\\prompt-builder-scripts\\ai-support\\skill-package-prompt-builder.py generate "
            "--skill-name \"Demo Skill\" --skill-slug demo-skill --source \"label=Docs;type=url;"
            "location=https://example.com;priority=high;trust=verified;use=skill rules\" --no-clipboard\n"
            "  python .\\prompt-builder-scripts\\ai-support\\skill-package-prompt-builder.py generate "
            "--skill-name \"Review Skill\" --skill-slug review-skill --skill-category code-quality-review "
            "--quality-rubric \"rejects generic findings without evidence\" --no-log --no-clipboard\n"
            "  python .\\prompt-builder-scripts\\ai-support\\skill-package-prompt-builder.py "
            "sources schema\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {SCRIPT_VERSION}")
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    run_parser = subparsers.add_parser("run", help="start the interactive interview")
    run_parser.set_defaults(handler=handle_run_command)

    generate_parser = subparsers.add_parser(
        "generate",
        help="build a prompt noninteractively from flags and source registry input",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_generate_arguments(generate_parser)
    generate_parser.set_defaults(handler=handle_generate_command)

    last_parser = subparsers.add_parser("last", help="reprint the last generated prompt")
    last_parser.add_argument("--session", help="session root or exact session name to select")
    add_output_arguments(last_parser)
    last_parser.set_defaults(handler=handle_last_command)

    settings_parser = subparsers.add_parser("settings", help="show or change sibling-log settings")
    settings_sub = settings_parser.add_subparsers(dest="settings_command", metavar="action", required=True)
    settings_show = settings_sub.add_parser("show", help="print current settings")
    settings_show.set_defaults(handler=handle_settings_command)
    settings_set = settings_sub.add_parser("set", help="set one setting value")
    settings_set.add_argument("key", choices=SETTING_KEYS)
    settings_set.add_argument("value")
    settings_set.set_defaults(handler=handle_settings_command)
    settings_reset = settings_sub.add_parser("reset", help="reset one setting or all settings")
    settings_reset.add_argument("key", nargs="?", choices=SETTING_KEYS)
    settings_reset.set_defaults(handler=handle_settings_command)

    sessions_parser = subparsers.add_parser("sessions", help="inspect saved sessions")
    sessions_sub = sessions_parser.add_subparsers(dest="sessions_command", metavar="action", required=True)
    sessions_list = sessions_sub.add_parser("list", help="list saved sessions")
    sessions_list.set_defaults(handler=handle_sessions_command)
    sessions_show = sessions_sub.add_parser("show", help="show one session")
    sessions_show.add_argument("session", help="session root or exact session name")
    sessions_show.set_defaults(handler=handle_sessions_command)

    sources_parser = subparsers.add_parser("sources", help="inspect or validate source registries")
    sources_sub = sources_parser.add_subparsers(dest="sources_command", metavar="action", required=True)
    sources_schema = sources_sub.add_parser("schema", help="print source-registry JSON schema")
    sources_schema.set_defaults(handler=handle_sources_command)
    sources_validate = sources_sub.add_parser("validate", help="validate source registry input")
    add_source_arguments(sources_validate)
    sources_validate.add_argument("--session", help="session root or exact session name")
    sources_validate.set_defaults(handler=handle_sources_command)
    sources_list = sources_sub.add_parser("list", help="list source registry entries")
    add_source_arguments(sources_list)
    sources_list.add_argument("--session", help="session root or exact session name")
    sources_list.set_defaults(handler=handle_sources_command)
    return parser


def add_generate_arguments(parser: argparse.ArgumentParser) -> None:
    """Add noninteractive prompt-generation options to a parser."""
    parser.add_argument("--session", help="session root or exact session name to use as defaults")
    parser.add_argument("--session-name", help="name for a new CLI-generated session")
    parser.add_argument("--skill-name")
    parser.add_argument("--skill-slug")
    parser.add_argument("--purpose", dest="skill_purpose")
    parser.add_argument("--target-agents", choices=TARGET_AGENTS)
    parser.add_argument("--skill-category", choices=SKILL_CATEGORY_VALUES)
    parser.add_argument("--triggers", dest="trigger_conditions")
    parser.add_argument("--example-tasks")
    parser.add_argument("--negative-triggers")
    parser.add_argument("--workflow", dest="user_workflow")
    parser.add_argument("--files", dest="files_to_generate")
    parser.add_argument("--doc-depth", dest="doc_depth")
    parser.add_argument("--verification", dest="verification_requirements")
    parser.add_argument("--known-gotchas")
    parser.add_argument("--quality-rubric")
    parser.add_argument("--observation-method")
    parser.add_argument("--privacy", dest="privacy_constraints")
    parser.add_argument("--out-of-scope", dest="out_of_scope")
    parser.add_argument("--risk", dest="risk_level", choices=RISK_LEVELS)
    parser.add_argument("--source-license", choices=SOURCE_LICENSES)
    parser.add_argument("--build-intent", choices=BUILD_INTENTS)
    parser.add_argument("--parallelism", choices=PARALLELISM_MODES)
    parser.add_argument("--memory-file-policy", choices=MEMORY_FILE_POLICIES)
    parser.add_argument("--subagent-review-policy", choices=SUBAGENT_REVIEW_POLICIES)
    add_source_arguments(parser)
    add_output_arguments(parser)
    parser.add_argument("--backup-mode", choices=BACKUP_MODES, help="backup behavior for this run")
    parser.add_argument("--no-log", action="store_true", help="do not append this run to the sibling log")


def add_source_arguments(parser: argparse.ArgumentParser) -> None:
    """Add source-registry input options to a parser."""
    parser.add_argument("--sources-file", help="JSON array or object with a 'sources' array")
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help=(
            "repeatable semicolon-separated source spec, e.g. "
            "\"label=Docs;type=url;location=https://example.com;priority=high;trust=verified;use=rules\""
        ),
    )


def add_output_arguments(parser: argparse.ArgumentParser) -> None:
    """Add output-shaping options shared by prompt-emitting commands."""
    parser.add_argument("--raw", action="store_true", help="print only the prompt body without markers")
    parser.add_argument("--output", help="also write emitted text to this path")
    parser.add_argument("--no-clipboard", action="store_true", help="disable clipboard copy for this run")


def handle_run_command(_args: argparse.Namespace) -> int:
    """Run the original interactive workflow from an explicit subcommand."""
    return run_interactive()


def handle_generate_command(args: argparse.Namespace) -> int:
    """Generate a prompt from CLI flags without prompting."""
    settings, history, sessions, drafts, devlog, issues, first_run = read_log()
    report_log_issues(issues)
    settings = effective_cli_settings(settings, args)
    session, fallback, is_new_session = resolve_cli_session(args, sessions, drafts, history)
    run = build_run_from_cli_args(args, session, fallback)
    try:
        output = build_output(settings, run, len(history) + 1)
    except RuntimeError as exc:
        raise CliError(str(exc)) from exc
    emit_cli_output(settings, output, args)
    save_cli_backup(settings, output, run)
    if not args.no_log:
        if first_run and not LOG_PATH.exists():
            write_log(settings, history, sessions, drafts, devlog)
        if is_new_session:
            sessions.append(session)
        history.append(run)
        drafts[session.root] = run
        events = ["emitted noninteractive skill build-prompt"]
        prompt_archive_path, _ = build_workflow_paths(settings, run)
        events.append(f"archive target {prompt_archive_path}")
        devlog.extend(format_devlog_lines(len(history), run, is_enabled(settings["assist_mode"]), events))
        write_log(settings, history, sessions, drafts, devlog)
    return 0


def handle_last_command(args: argparse.Namespace) -> int:
    """Reprint the last prompt from history without changing the log."""
    settings, history, sessions, drafts, _devlog, issues, _first_run = read_log()
    report_log_issues(issues)
    settings = effective_cli_settings(settings, args)
    selected = select_history_run(args.session, sessions, history) if args.session else (history[-1] if history else None)
    if selected is None:
        raise CliError("no completed run found")
    output = build_output(settings, selected, len(history))
    emit_cli_output(settings, output, args)
    return 0


def handle_settings_command(args: argparse.Namespace) -> int:
    """Show, set, or reset settings in the sibling log."""
    settings, history, sessions, drafts, devlog, issues, _first_run = read_log()
    report_log_issues(issues)
    if args.settings_command == "show":
        for key in SETTING_KEYS:
            print(f"{key} = {settings[key]}")
        return 0
    if args.settings_command == "set":
        settings[args.key] = validate_setting_value(args.key, args.value)
        write_log(settings, history, sessions, drafts, devlog)
        print(f"{args.key} = {settings[args.key]}")
        return 0
    if args.settings_command == "reset":
        if args.key:
            settings[args.key] = DEFAULT_SETTINGS[args.key]
            print(f"{args.key} = {settings[args.key]}")
        else:
            settings = OrderedDict(DEFAULT_SETTINGS)
            print("settings reset to built-in defaults")
        write_log(settings, history, sessions, drafts, devlog)
        return 0
    raise CliError(f"unknown settings action: {args.settings_command}")


def handle_sessions_command(args: argparse.Namespace) -> int:
    """List or show saved sessions."""
    _settings, history, sessions, drafts, _devlog, issues, _first_run = read_log()
    report_log_issues(issues)
    if args.sessions_command == "list":
        if not sessions:
            print("(no sessions)")
            return 0
        for session in reversed(sessions):
            runs = [run for run in history if run.session == session.root]
            draft_flag = " draft" if session.root in drafts else ""
            print(f"{session.root} | {session.name} | created {session.created} | runs {len(runs)}{draft_flag}")
        return 0
    if args.sessions_command == "show":
        session = resolve_existing_session(args.session, sessions)
        runs = [run for run in history if run.session == session.root]
        latest = drafts.get(session.root) or (runs[-1] if runs else None)
        print(format_session_line(session))
        print(f"runs = {len(runs)}")
        print(f"has_draft = {'yes' if session.root in drafts else 'no'}")
        if latest:
            print(f"skill = {latest.skill_name}")
            print(f"slug = {latest.skill_slug}")
            print(f"sources = {len(latest.sources)}")
        return 0
    raise CliError(f"unknown sessions action: {args.sessions_command}")


def handle_sources_command(args: argparse.Namespace) -> int:
    """Print source schema, validate source input, or list source entries."""
    if args.sources_command == "schema":
        print(source_schema_text())
        return 0
    _settings, history, sessions, drafts, _devlog, issues, _first_run = read_log()
    report_log_issues(issues)
    sources = resolve_sources_for_cli(args, sessions, drafts, history)
    if args.sources_command == "validate":
        validate_sources(sources)
        print(f"valid sources: {len(sources)}")
        return 0
    if args.sources_command == "list":
        validate_sources(sources)
        list_sources(sources)
        return 0
    raise CliError(f"unknown sources action: {args.sources_command}")


def report_log_issues(issues: list[str]) -> None:
    """Print recoverable log parse issues to stderr for noninteractive commands."""
    for issue in issues:
        print(f"warning: {issue}", file=sys.stderr)


def effective_cli_settings(settings: OrderedDict[str, str], args: argparse.Namespace) -> OrderedDict[str, str]:
    """Return settings with one-run CLI output overrides applied."""
    effective = OrderedDict(settings)
    if getattr(args, "no_clipboard", False):
        effective["copy_to_clipboard"] = "no"
    backup_mode = getattr(args, "backup_mode", None)
    if backup_mode:
        effective["backup_mode"] = backup_mode
    return effective


def validate_setting_value(key: str, value: str) -> str:
    """Validate and normalize one setting value from the CLI."""
    cleaned = value.strip()
    if key == "mode":
        if cleaned not in MODES:
            raise CliError(f"mode must be one of: {', '.join(MODES)}")
    elif key == "assist_mode":
        if cleaned not in ASSIST_MODES:
            raise CliError(f"assist_mode must be one of: {', '.join(ASSIST_MODES)}")
    elif key == "copy_to_clipboard":
        if not is_yes_no(cleaned):
            raise CliError("copy_to_clipboard must be a yes/no style value")
    elif key == "backup_mode":
        if cleaned not in BACKUP_MODES:
            raise CliError(f"backup_mode must be one of: {', '.join(BACKUP_MODES)}")
    elif key in PATH_SETTINGS:
        if cleaned.lower() in CONFIRM_TOKENS:
            raise CliError(f"{key} must be a folder path, not a yes/no answer")
        ok, message = validate_path(cleaned)
        if not ok:
            raise CliError(message)
    if not cleaned:
        raise CliError(f"{key} may not be empty")
    return cleaned


def resolve_existing_session(selector: str, sessions: list[SessionEntry]) -> SessionEntry:
    """Find exactly one saved session by root hash or exact name."""
    matches = [session for session in sessions if session.root == selector or session.name == selector]
    if not matches:
        raise CliError(f"session not found: {selector}")
    if len(matches) > 1:
        roots = ", ".join(session.root for session in matches)
        raise CliError(f"session name is ambiguous: {selector} ({roots})")
    return matches[0]


def latest_run_for_session(root: str, history: list[RunEntry]) -> RunEntry | None:
    """Return the newest completed run for a session root, or None."""
    return next((run for run in reversed(history) if run.session == root), None)


def resolve_cli_session(
    args: argparse.Namespace,
    sessions: list[SessionEntry],
    drafts: dict[str, RunEntry],
    history: list[RunEntry],
) -> tuple[SessionEntry, RunEntry | None, bool]:
    """Resolve the session and defaults for a noninteractive generate command."""
    if getattr(args, "session", None):
        if getattr(args, "session_name", None):
            raise CliError("--session-name cannot be combined with --session")
        session = resolve_existing_session(args.session, sessions)
        fallback = drafts.get(session.root) or latest_run_for_session(session.root, history)
        return session, fallback, False
    name = args.session_name or args.skill_name or "cli generate"
    session = SessionEntry(
        created=datetime.now().strftime("%Y-%m-%d %H:%M"),
        root=make_root_hash(),
        name=clean_for_log(name),
    )
    return session, None, True


def select_history_run(selector: str, sessions: list[SessionEntry], history: list[RunEntry]) -> RunEntry | None:
    """Return the latest completed run for a selected saved session."""
    session = resolve_existing_session(selector, sessions)
    return latest_run_for_session(session.root, history)


def build_run_from_cli_args(
    args: argparse.Namespace,
    session: SessionEntry,
    fallback: RunEntry | None,
) -> RunEntry:
    """Build a RunEntry from CLI flags, defaults, and optional session state."""
    values = cli_default_values(fallback)
    applied_name = False
    for arg_name, attr in CLI_RUN_FIELD_MAP.items():
        value = getattr(args, arg_name, None)
        if value is not None:
            values[attr] = clean_for_log(value)
            if attr == "skill_name":
                applied_name = True
    if applied_name and getattr(args, "skill_slug", None) is None and fallback is None:
        values["skill_slug"] = slugify(values["skill_name"])
    if values["target_agents"] == "Codex" and values["parallelism"] in {"haiku", "opus"}:
        raise CliError("Codex-only runs must use --parallelism none or generic")
    sources = resolve_sources_for_cli(args, [], {}, []) if cli_sources_supplied(args) else (
        [SourceEntry.from_dict(src.to_dict()) for src in fallback.sources]
        if fallback and fallback.sources else default_seed_sources()
    )
    validate_sources(sources)
    return RunEntry(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
        mode="skill build prompt",
        session=session.root,
        target_agents=values["target_agents"],
        skill_name=values["skill_name"],
        skill_slug=values["skill_slug"],
        skill_purpose=values["skill_purpose"],
        skill_category=values["skill_category"],
        trigger_conditions=values["trigger_conditions"],
        example_tasks=values["example_tasks"],
        negative_triggers=values["negative_triggers"],
        user_workflow=values["user_workflow"],
        files_to_generate=values["files_to_generate"],
        doc_depth=values["doc_depth"],
        verification_requirements=values["verification_requirements"],
        known_gotchas=values["known_gotchas"],
        quality_rubric=values["quality_rubric"],
        observation_method=values["observation_method"],
        privacy_constraints=values["privacy_constraints"],
        out_of_scope=values["out_of_scope"],
        risk_level=values["risk_level"],
        source_license=values["source_license"],
        build_intent=values["build_intent"],
        parallelism=values["parallelism"],
        memory_file_policy=values["memory_file_policy"],
        subagent_review_policy=values["subagent_review_policy"],
        sources=sources,
    )


CLI_RUN_FIELD_MAP = OrderedDict(
    (
        ("skill_name", "skill_name"),
        ("skill_slug", "skill_slug"),
        ("skill_purpose", "skill_purpose"),
        ("skill_category", "skill_category"),
        ("target_agents", "target_agents"),
        ("trigger_conditions", "trigger_conditions"),
        ("example_tasks", "example_tasks"),
        ("negative_triggers", "negative_triggers"),
        ("user_workflow", "user_workflow"),
        ("files_to_generate", "files_to_generate"),
        ("doc_depth", "doc_depth"),
        ("verification_requirements", "verification_requirements"),
        ("known_gotchas", "known_gotchas"),
        ("quality_rubric", "quality_rubric"),
        ("observation_method", "observation_method"),
        ("privacy_constraints", "privacy_constraints"),
        ("out_of_scope", "out_of_scope"),
        ("risk_level", "risk_level"),
        ("source_license", "source_license"),
        ("build_intent", "build_intent"),
        ("parallelism", "parallelism"),
        ("memory_file_policy", "memory_file_policy"),
        ("subagent_review_policy", "subagent_review_policy"),
    )
)


def cli_default_values(fallback: RunEntry | None) -> dict[str, str]:
    """Return run-field defaults for noninteractive generation."""
    defaults = dict(RUN_DEFAULTS)
    if fallback is None:
        return defaults
    for attr in defaults:
        value = getattr(fallback, attr, "")
        if value:
            defaults[attr] = value
    return defaults


def cli_sources_supplied(args: argparse.Namespace) -> bool:
    """Return True when CLI source inputs were supplied."""
    return bool(getattr(args, "sources_file", None) or getattr(args, "source", []))


def resolve_sources_for_cli(
    args: argparse.Namespace,
    sessions: list[SessionEntry],
    drafts: dict[str, RunEntry],
    history: list[RunEntry],
) -> list[SourceEntry]:
    """Resolve source registry input for CLI commands."""
    if cli_sources_supplied(args):
        sources = []
        if getattr(args, "sources_file", None):
            sources.extend(load_sources_file(args.sources_file))
        for spec in getattr(args, "source", []):
            sources.append(parse_source_spec(spec))
        return sources
    if getattr(args, "session", None):
        session = resolve_existing_session(args.session, sessions)
        run = drafts.get(session.root) or latest_run_for_session(session.root, history)
        if run and run.sources:
            return [SourceEntry.from_dict(src.to_dict()) for src in run.sources]
    return default_seed_sources()


def load_sources_file(path_value: str) -> list[SourceEntry]:
    """Load SourceEntry objects from a JSON file."""
    path = resolve_local_path(path_value)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise CliError(f"could not read sources file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CliError(f"sources file is not valid JSON: {exc}") from exc
    if isinstance(data, dict) and "sources" in data:
        data = data["sources"]
    if not isinstance(data, list):
        raise CliError("sources file must contain a JSON array or an object with a 'sources' array")
    return [SourceEntry.from_dict(item) if isinstance(item, dict) else bad_source_item(item) for item in data]


def bad_source_item(item: object) -> SourceEntry:
    """Raise a clean error for a non-object source item."""
    raise CliError(f"source entries must be JSON objects, got {type(item).__name__}")


def parse_source_spec(spec: str) -> SourceEntry:
    """Parse one semicolon-separated key=value source spec."""
    values: dict[str, str] = {}
    for chunk in spec.split(";"):
        part = chunk.strip()
        if not part:
            continue
        if "=" not in part:
            raise CliError(f"source part lacks '=': {part}")
        key, value = part.split("=", 1)
        canonical = normalize_source_key(key)
        if canonical in values:
            raise CliError(f"duplicate source key: {key}")
        values[canonical] = value.strip()
    if "stype" not in values:
        values["stype"] = "note"
    return SourceEntry.from_dict(values)


def normalize_source_key(key: str) -> str:
    """Normalize a CLI source key to a SourceEntry field name."""
    normalized = key.strip().lower().replace("-", "_")
    aliases = {
        "type": "stype",
        "source_type": "stype",
        "use": "intended_use",
        "intended": "intended_use",
        "content": "content_path",
        "content_file": "content_path",
        "pages": "approx_pages",
    }
    normalized = aliases.get(normalized, normalized)
    allowed = set(SourceEntry.__dataclass_fields__)
    if normalized not in allowed:
        raise CliError(f"unknown source key: {key}")
    return normalized


def validate_sources(sources: list[SourceEntry]) -> None:
    """Validate a source registry for CLI use."""
    if not sources:
        raise CliError("source registry is empty")
    errors: list[str] = []
    for index, src in enumerate(sources, start=1):
        label = src.label or f"source {index}"
        if not src.label:
            errors.append(f"{label}: label is required")
        if not src.location:
            errors.append(f"{label}: location is required")
        if src.stype not in SOURCE_TYPES:
            errors.append(f"{label}: type must be one of {', '.join(SOURCE_TYPES)}")
        if src.priority not in PRIORITIES:
            errors.append(f"{label}: priority must be one of {', '.join(PRIORITIES)}")
        if src.trust not in TRUST_LEVELS:
            errors.append(f"{label}: trust must be one of {', '.join(TRUST_LEVELS)}")
        if src.content_path and not local_path_exists(src.content_path):
            errors.append(f"{label}: content_path not found: {src.content_path}")
        if not src.is_usable():
            errors.append(f"{label}: local source path not found: {src.location}")
    if errors:
        raise CliError("; ".join(errors))


def source_schema_text() -> str:
    """Return the JSON source-registry schema and an example."""
    example = [
        OrderedDict(
            (
                ("label", "Claude skills documentation"),
                ("stype", "official-docs"),
                ("location", "https://code.claude.com/docs/en/skills"),
                ("priority", "high"),
                ("trust", "official"),
                ("intended_use", "skill format and authoring rules"),
                ("content_path", ""),
                ("supply_at_draft", True),
                ("approx_pages", 0),
            )
        )
    ]
    return json.dumps({"sources": example}, indent=2)


def emit_cli_output(settings: OrderedDict[str, str], output: str, args: argparse.Namespace) -> None:
    """Emit prompt output for noninteractive commands without prompting."""
    payload = clipboard_payload(output)
    rendered = payload if getattr(args, "raw", False) else output
    if is_enabled(settings["copy_to_clipboard"]):
        copied, message = copy_to_clipboard(payload)
        if copied:
            print("Copied generated prompt to clipboard.")
        else:
            print(f"Clipboard copy skipped: {message}.")
    if getattr(args, "output", None):
        path = resolve_local_path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
        print(f"Wrote output: {path}")
    print(rendered)


def save_cli_backup(settings: OrderedDict[str, str], output: str, run: RunEntry) -> Path | None:
    """Save a noninteractive backup only when backup_mode is always."""
    if settings["backup_mode"] != "always":
        return None
    folder = resolve_local_path(settings["backup_folder"])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / backup_filename(run)
    path.write_text(clipboard_payload(output) + "\n", encoding="utf-8")
    print(f"Saved prompt backup: {path}")
    return path


# ---- Clipboard, backup, deps, exit, emit ------------------------------------
def _copy_to_clipboard_posix(text: str) -> tuple[bool, str]:
    """Copy text via the first available CLI clipboard tool on macOS/Linux.

    Tries pbcopy on macOS; wl-copy / xclip / xsel on other Unixes. Returns
    (True, "") on success, else (False, reason) so the caller's printed fallback
    still lets the user copy manually.
    """
    if sys.platform == "darwin":
        candidates = (["pbcopy"],)
    else:
        candidates = (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        )
    last = "no clipboard tool found (tried pbcopy/wl-copy/xclip/xsel)"
    for cmd in candidates:
        try:
            proc = subprocess.run(cmd, input=text.encode("utf-8"), timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            last = f"{cmd[0]} unavailable ({exc})"
            continue
        if proc.returncode == 0:
            return True, ""
        last = f"{cmd[0]} exited {proc.returncode}"
    return False, last


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    """Copy text to the OS clipboard (Windows ctypes; pbcopy/wl-copy/xclip/xsel elsewhere)."""
    if os.name != "nt":
        return _copy_to_clipboard_posix(text)

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


def backup_filename(run: RunEntry, timestamp: datetime | None = None) -> str:
    """Build the timestamped Markdown backup file name from the run."""
    stamp = (timestamp or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-skill-{run.skill_slug}.md"


def save_prompt_backup(settings: OrderedDict[str, str], prompt_text_value: str, run: RunEntry) -> Path | None:
    """Optionally save the prompt backup based on the backup_mode setting."""
    backup_mode = settings["backup_mode"]
    if backup_mode == "never":
        return None
    folder = resolve_local_path(settings["backup_folder"])
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
    except (EOFError, QuitRequested):
        print("\nBackup skipped.")
        return None
    path = folder / backup_filename(run)
    path.write_text(prompt_text_value + "\n", encoding="utf-8")
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
    """Detect a one-click launch whose console window dies with this process."""
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


def emit_output(settings: OrderedDict[str, str], output: str) -> None:
    """Copy the prompt to the clipboard when enabled, then print the output."""
    payload = clipboard_payload(output)
    if is_enabled(settings["copy_to_clipboard"]):
        copied, message = copy_to_clipboard(payload)
        if copied:
            print("Copied generated prompt to clipboard.")
        else:
            print(f"Clipboard copy skipped: {message}.")
    print(output)


def run_interactive() -> int:
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
        # last completed run.
        defaults_source = drafts.get(active.root) or last_completed
        run = ask_run(
            settings, history, sessions, drafts, devlog, events, active, defaults_source, last_completed
        )

        # Build (and verify) before logging so a failed build never records a
        # history line that would poison later r/last runs.
        output = build_output_safely(settings, run, len(history) + 1)
        if output is None:
            return 1

        history.append(run)
        drafts[active.root] = run  # the draft now reflects the completed run
        assist_on = is_enabled(settings["assist_mode"])
        prompt_archive_path, _ = build_workflow_paths(settings, run)
        events.append(f"emitted skill build-prompt; archive target {prompt_archive_path}")
        devlog.extend(format_devlog_lines(len(history), run, assist_on, events))
        write_log(settings, history, sessions, drafts, devlog)
        emit_output(settings, output)
        save_prompt_backup(settings, clipboard_payload(output), run)
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


def main(argv: list[str] | None = None) -> int:
    """Run the script; no argv preserves the original interactive behavior."""
    args = [] if argv is None else list(argv)
    if not args:
        return run_interactive()
    parser = build_arg_parser()
    namespace = parser.parse_args(args)
    handler = getattr(namespace, "handler", None)
    if handler is None:
        return run_interactive()
    try:
        return handler(namespace)
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
