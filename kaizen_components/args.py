from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from . import TOOL_VERSION
from . import db
from .denials import KaizenDenied, remedy_example, usage_denial
from .hashing import file_sha256, utc_text_hash, validate_text_fields
from .output import emit, emit_error
from .paths import EXPORT_ROOT, REPO_ROOT, read_text_file, repo_relative
from .plan_records import add_plan, revise_plan
from .policy_records import add_policy, inspect_policy, list_policies, query_policies, session_context
from .proof_artifacts import (
    add_anti_pattern,
    add_artifact,
    add_eval_case,
    add_eval_run,
    add_verification,
    hash_file,
    inspect_artifact,
    inspect_quality,
    list_artifacts,
    query_anti_patterns,
    query_verifications,
    verify_artifact_hash,
)
from .reports import make_report, session_digest
from .task_records import (
    add_ledger_event,
    add_lifecycle_record,
    inspect_record,
    learned_context,
    list_records,
    promote_gotcha_to_learning,
    promote_learning_to_learned,
    query_records,
    update_lifecycle_record,
)
from .task_records import _text_arg
from . import migration_learning


ALIASES = {
    "K0": ["K0", "op-find"],
    "K1": ["K1", "db-check", "check-init"],
    "K2": ["K2", "schema-status"],
    "K3": ["K3", "db-backup"],
    "K6": ["K6", "db-manifest"],
    "W1": ["W1", "task-start"],
    "W2": ["W2", "task-update"],
    "W3": ["W3", "plan-create"],
    "W4": ["W4", "plan-revise"],
    "W5": ["W5", "subagent-packet-create"],
    "W6": ["W6", "subagent-packet-ingest"],
    "W7": ["W7", "diagnostic-packet-create"],
    "W8": ["W8", "diagnostic-result-ingest"],
    "G1": ["G1", "gotcha-add"],
    "G2": ["G2", "gotcha-list"],
    "G3": ["G3", "gotcha-query"],
    "G4": ["G4", "gotcha-inspect"],
    "G5": ["G5", "gotcha-update"],
    "L1": ["L1", "learning-add"],
    "L2": ["L2", "promote-gotcha-learning"],
    "L3": ["L3", "promote-learning-learned"],
    "L4": ["L4", "learning-list"],
    "L5": ["L5", "learning-query"],
    "L6": ["L6", "learning-inspect"],
    "L7": ["L7", "learned-list"],
    "L8": ["L8", "learned-query"],
    "L9": ["L9", "learned-inspect"],
    "L10": ["L10", "learned-context"],
    "Q1": ["Q1", "proof-add"],
    "Q2": ["Q2", "verification-add"],
    "Q3": ["Q3", "eval-case-add"],
    "Q4": ["Q4", "eval-run-add"],
    "Q5": ["Q5", "anti-pattern-add"],
    "Q6": ["Q6", "anti-pattern-query"],
    "Q7": ["Q7", "quality-inspect"],
    "Q8": ["Q8", "output-validate"],
    "Q9": ["Q9", "verify-query"],
    "M1": ["M1", "migration-scan"],
    "M2": ["M2", "migration-dry-run"],
    "M3": ["M3", "migration-apply"],
    "M4": ["M4", "migration-verify"],
    "M5": ["M5", "migration-report"],
    "R0": ["R0", "session-digest"],
    "R1": ["R1", "task-report"],
    "R2": ["R2", "ledger-report"],
    "R3": ["R3", "learning-report"],
    "R4": ["R4", "proof-report"],
    "R5": ["R5", "eval-report"],
    "R6": ["R6", "source-report"],
    "R7": ["R7", "anti-pattern-report"],
    "R8": ["R8", "weekly-report"],
    "R9": ["R9", "monthly-report"],
    "R10": ["R10", "yearly-report"],
    "R11": ["R11", "topic-report"],
    "S1": ["S1", "source-add"],
    "S2": ["S2", "source-query"],
    "S3": ["S3", "source-inspect"],
    "S4": ["S4", "source-export"],
    "I1": ["I1", "irl-create"],
    "I2": ["I2", "irl-prediction-add"],
    "I3": ["I3", "irl-correction-add"],
    "I4": ["I4", "irl-outcome-add"],
    "I5": ["I5", "irl-report"],
    "A1": ["A1", "artifact-add"],
    "A2": ["A2", "artifact-hash"],
    "A3": ["A3", "artifact-inspect"],
    "A4": ["A4", "artifact-list"],
    "A5": ["A5", "artifact-verify"],
    "X1": ["X1", "policy-add"],
    "X2": ["X2", "policy-list"],
    "X3": ["X3", "policy-query"],
    "X4": ["X4", "policy-inspect"],
    "X5": ["X5", "policy-session-context"],
    "T1": ["T1", "trace-add"],
    "T2": ["T2", "score-add"],
    "T3": ["T3", "trace-report"],
    "T4": ["T4", "score-query"],
    "E1": ["E1", "evidence-ingest-file"],
    "E3": ["E3", "evidence-chunk"],
    "E4": ["E4", "evidence-query"],
    "E5": ["E5", "evidence-inspect"],
    "O1": ["O1", "lab-assemble"],
    "O2": ["O2", "lab-propose"],
    "O3": ["O3", "lab-report"],
    "Y1": ["Y1", "comfy-run"],
    "Y2": ["Y2", "comfy-inspect"],
    "Y3": ["Y3", "comfy-list"],
    "Y4": ["Y4", "comfy-replay"],
    "Y5": ["Y5", "comfy-doctor"],
    "B1": ["B1", "model-doctor"],
    "B2": ["B2", "model-run"],
    "B3": ["B3", "reembed"],
}

ALIAS_TO_CODE = {alias.lower(): code for code, aliases in ALIASES.items() for alias in aliases}

# One-line purpose per op (kept byte-identical to the README command-index Purpose column —
# a parity test enforces it) plus sparse intent keywords for K0 lookup where user phrasing
# diverges from the purpose text. args.py is the single source of truth; the README follows.
REGISTRY: dict[str, tuple[str, tuple[str, ...]]] = {
    "K0": ("Find the right operation from intent", ("lookup", "discover")),
    "K1": ("Check or initialize the DB", ("init", "setup", "database", "start")),
    "K2": ("Show schema status", ("version", "migration")),
    "K3": ("Back up DB files", ("backup",)),
    "K6": ("Export a DB manifest", ("counts", "tables")),
    "W1": ("Create a task record", ("begin", "new", "start", "work")),
    "W2": ("Add a ledger/status update", ("status", "progress", "decision", "done", "note")),
    "W3": ("Create a plan record", ("plan",)),
    "W4": ("Revise a plan record", ("replan", "revision")),
    "W5": ("Create a subagent packet", ("handoff", "delegate")),
    "W6": ("Ingest a subagent packet", ("result",)),
    "W7": ("Create a diagnostic packet", ("debug",)),
    "W8": ("Ingest a diagnostic result", ()),
    "G1": ("Add a GOTCHA record", ("pitfall", "failure", "footgun", "mistake", "bug", "problem")),
    "G2": ("List GOTCHA records", ()),
    "G3": ("Query GOTCHA records", ("search",)),
    "G4": ("Inspect a GOTCHA record", ()),
    "G5": ("Update a GOTCHA record", ("resolve", "status", "close")),
    "L1": ("Add a LEARNING record", ()),
    "L2": ("Promote GOTCHA to LEARNING", ("validate", "promote")),
    "L3": ("Promote LEARNING to LEARNED", ("implemented", "promote")),
    "L4": ("List LEARNING records", ()),
    "L5": ("Query LEARNING records", ()),
    "L6": ("Inspect a LEARNING record", ()),
    "L7": ("List LEARNED records", ()),
    "L8": ("Query LEARNED records", ()),
    "L9": ("Inspect a LEARNED record", ()),
    "L10": ("Export LEARNED lessons + source chain", ("lessons", "learned", "history", "chain", "lineage")),
    "Q1": ("Record proof metadata", ("proof", "evidence")),
    "Q2": ("Add a verification result", ("passed", "failed", "verify", "check", "conclusion")),
    "Q3": ("Add an eval case", ()),
    "Q4": ("Record an eval run", ()),
    "Q5": ("Add an anti-pattern record", ()),
    "Q6": ("Query anti-pattern records", ()),
    "Q7": ("Inspect proof, eval, or quality record", ()),
    "Q8": ("Validate a payload against its schema", ("schema", "json")),
    "Q9": ("Query verification conclusions", ("blockers", "failed", "checks")),
    "M1": ("Scan learning surfaces", ()),
    "M2": ("Preview migration actions", ("dry-run",)),
    "M3": ("Apply migration actions", ()),
    "M4": ("Verify migrated surfaces", ()),
    "M5": ("Report migration state", ()),
    "R0": ("Compact session-start digest (read)", ("session", "context", "compaction", "resume", "digest")),
    "R1": ("Generate a task report", ()),
    "R2": ("Generate a ledger report", ("timeline",)),
    "R3": ("Generate a learning report", ()),
    "R4": ("Generate a proof report", ()),
    "R5": ("Generate an eval report", ()),
    "R6": ("Generate a source report", ()),
    "R7": ("Generate an anti-pattern report", ()),
    "R8": ("Generate a weekly report", ()),
    "R9": ("Generate a monthly report", ()),
    "R10": ("Generate a yearly report", ()),
    "R11": ("Generate a topic report", ("topic", "search")),
    "S1": ("Add a source lock", ("source", "provenance", "citation")),
    "S2": ("Query source locks", ()),
    "S3": ("Inspect a source lock", ()),
    "S4": ("Export source locks", ()),
    "I1": ("Create an IRL Review record", ("prediction", "forecast")),
    "I2": ("Add an IRL Review prediction", ("prediction",)),
    "I3": ("Add a user correction", ("correction",)),
    "I4": ("Add an observed outcome", ("outcome", "calibration")),
    "I5": ("Generate an IRL Review report", ()),
    "A1": ("Add an artifact reference", ("file", "output")),
    "A2": ("Hash a file", ("sha256",)),
    "A3": ("Inspect an artifact", ()),
    "A4": ("List or query artifacts", ()),
    "A5": ("Verify an artifact hash", ("integrity",)),
    "X1": ("Add private policy context", ("rule", "policy", "private", "guardrail")),
    "X2": ("List private policy records", ()),
    "X3": ("Query private policy records", ()),
    "X4": ("Inspect a private policy record", ()),
    "X5": ("Load session policy context", ("session", "context", "rules", "compaction")),
    "E1": ("Ingest a file into the evidence plane", ("ingest", "document", "spec", "pdf", "manual")),
    "E3": ("Chunk an evidence document", ("split",)),
    "E4": ("Search evidence chunks", ("retrieve", "semantic", "find")),
    "E5": ("Inspect a document, block, or chunk", ()),
    "T1": ("Record a trace event", ("telemetry",)),
    "T2": ("Record an eval score", ("score",)),
    "T3": ("Generate a trace report", ()),
    "T4": ("Query eval scores with aggregates", ("trends", "mean")),
    "O1": ("Assemble an improvement-lab case set", ("lab", "trainset")),
    "O2": ("Record an improvement proposal", ("candidate", "variant")),
    "O3": ("Rank and report improvement proposals", ("leaderboard",)),
    "Y1": ("Run + record a ComfyUI workflow", ("image", "generate", "diffusion")),
    "Y2": ("Inspect one generative run", ()),
    "Y3": ("List recent generative runs", ()),
    "Y4": ("Re-submit a prior run's workflow", ("replay",)),
    "Y5": ("Probe the configured ComfyUI endpoint", ("doctor",)),
    "B1": ("Probe configured model backends", ("doctor", "ollama")),
    "B2": ("Advisory text via the LLM backend", ("llm", "model")),
    "B3": ("Backfill evidence-chunk embeddings", ("reembed", "vectors")),
}

# Ready-to-adapt examples for the ops agents reach for most; everything else gets a generic form.
_K0_EXAMPLES = {
    "K1": 'python kaizen.py K1 --json',
    "W1": 'python kaizen.py W1 --title "Task" --summary "One sentence." --body "Plan details." --json',
    "W2": 'python kaizen.py W2 --id TASK_ID --status done --summary "One sentence." --json',
    "G1": 'python kaizen.py G1 --title "Pitfall" --summary "One sentence." --body "Evidence-driven note." --json',
    "L2": 'python kaizen.py L2 --id GOTCHA_ID --json',
    "L10": 'python kaizen.py L10 --limit 5 --json',
    "Q2": 'python kaizen.py Q2 --task-id TASK_ID --conclusion VERIFIED_ACCEPTABLE --summary "One sentence." --json',
    "Q9": 'python kaizen.py Q9 --conclusion VERIFICATION_FAILED --json',
    "R0": 'python kaizen.py R0 --json',
    "T4": 'python kaizen.py T4 --trace-id TRACE_EVENT_ID --json',
    "X1": 'python kaizen.py X1 --title "Rule" --summary "One sentence." --body "The rule." --priority high --json',
    "X5": 'python kaizen.py X5 --json',
    "E1": 'python kaizen.py E1 --path docs/spec.md --summary "Ingest the spec." --json',
}


def _code_sort_key(code: str) -> tuple[str, int]:
    match = re.match(r"([A-Z]+)(\d+)", code)
    return (match.group(1), int(match.group(2))) if match else (code, 0)


def op_lookup(args: argparse.Namespace) -> dict[str, Any]:
    """K0: map intent text to operation codes. Pure lookup -- no DB, works before K1."""
    limit = int(getattr(args, "limit", None) or 5)

    def entry(code: str) -> dict[str, Any]:
        purpose, _keywords = REGISTRY[code]
        return {
            "code": code,
            "alias": ALIASES[code][-1],
            "purpose": purpose,
            "example": _K0_EXAMPLES.get(code, f"python kaizen.py {code} --json"),
        }

    query = getattr(args, "query", None)
    if not query:
        records = [entry(code) for code in sorted(ALIASES, key=_code_sort_key)]
        return {"status": "OK", "count": len(records), "records": records}
    # Normalize trivial plurals so "records"/"checks" match "record"/"check".
    raw_tokens = re.findall(r"[a-z0-9]+", query.lower())
    tokens = [t[:-1] if len(t) > 3 and t.endswith("s") else t for t in raw_tokens]
    scored: list[tuple[int, str]] = []
    for code, aliases in ALIASES.items():
        purpose, keywords = REGISTRY[code]
        haystack = " ".join([*aliases, purpose, *keywords]).lower()
        words = set(re.findall(r"[a-z0-9]+", haystack))
        words |= {w[:-1] for w in words if len(w) > 3 and w.endswith("s")}
        score = sum(2 if token in words else (1 if token in haystack else 0) for token in tokens)
        if score:
            scored.append((score, code))
    scored.sort(key=lambda item: (-item[0], _code_sort_key(item[1])))
    records = [{**entry(code), "score": score} for score, code in scored[:limit]]
    result: dict[str, Any] = {"status": "OK", "query": query, "count": len(records), "records": records}
    if not records:
        result["required_action"] = "no match; run K0 with no --query for the full operation index"
    return result


HELP_EPILOG = """
Examples:
  python kaizen.py K0 --query "record a pitfall" --json
  python kaizen.py K1 --json
  python kaizen.py W1 --title "Task" --summary "One sentence." --body "Plan details."
  python kaizen.py G1 --title "Pitfall" --summary "One sentence." --body "Evidence-driven note."
  python kaizen.py X5 --json
  python kaizen.py R0 --json
  python kaizen.py R2 --limit 20

Command families:
  K* DB/core | W* work/tasks/plans/packets | G* GOTCHA | L* LEARNING/LEARNED
  Q* quality/evals/verification/proof | M* migration | R* reports | S* sources
  I* IRL Review | A* artifacts | X* private context/policy | T* traces/scores | E* evidence ingestion | O* improvement lab
  Y* generative runs (ComfyUI) | B* model/embedding backends

Use the code form or named alias. For example, K1 and db-check are identical.
"""


class _KaizenArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # noqa: D102 -- argparse hook
        # The classic Windows PowerShell 5.1 failure: quotes inside an inline JSON arg
        # are stripped before argv, so the JSON shatters into "unrecognized arguments"
        # fragments containing { or \. Hint the quoting-proof path before exiting.
        if "unrecognized arguments" in message and any(ch in message for ch in "{\\"):
            print(
                "hint: inline JSON quotes were likely stripped by the shell (Windows PowerShell 5.1 "
                "does this); use the matching *-file flag (e.g. --payload-json-file) or "
                'backslash-escape the quotes: --payload-json "{\\"k\\":\\"v\\"}"',
                file=sys.stderr,
            )
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _KaizenArgumentParser(
        prog="kaizen.py",
        description="Agent Kaizen data-plane CLI.",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("operation", nargs="?", help="operation code or named alias, such as K1 or db-check")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--version", action="store_true", help="show tool version")
    parser.add_argument(
        "--integrity",
        action="store_true",
        help="K1: also scan cross-table id references for orphaned rows (read-only)",
    )
    parser.add_argument(
        "--restamp-manifest",
        action="store_true",
        dest="restamp_manifest",
        help="K1: reconcile the stored DDL manifest hash to the current engine (only when schema_ok)",
    )
    parser.add_argument("--id", help="record id")
    parser.add_argument("--task-id", help="task id")
    parser.add_argument("--trace-id", help="trace id grouping related events")
    parser.add_argument("--chunker", help="chunking strategy: recursive (default) or semantic (needs an embedding backend)")
    parser.add_argument("--contract", help="improvement-lab task contract / signature name")
    parser.add_argument("--metric", help="improvement-lab metric name")
    parser.add_argument("--baseline-score", type=float, dest="baseline_score", help="baseline metric score")
    parser.add_argument("--candidate-score", type=float, dest="candidate_score", help="candidate metric score")
    parser.add_argument("--endpoint", help="backend HTTP endpoint URL (e.g. ComfyUI; default http://127.0.0.1:8188)")
    parser.add_argument("--timeout", type=float, help="max seconds to wait on a backend run")
    parser.add_argument("--template", help="generative-run output template / subdir name")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="validate + record without calling the backend")
    parser.add_argument("--prompt", help="advisory model prompt (B2 model-run)")
    parser.add_argument("--semantic", action="store_true", help="E4: vector search via the configured embedding backend")
    parser.add_argument("--proof-id", help="proof or verification id")
    parser.add_argument("--eval-case-id", help="eval case id")
    parser.add_argument("--review-id", help="IRL review id")
    parser.add_argument("--title", help="record title")
    parser.add_argument("--summary", help="required 1-2 sentence summary")
    parser.add_argument("--summary-file", help="read summary from file when inline input is unsafe")
    parser.add_argument("--body", help="record body")
    parser.add_argument("--body-file", help="read body from file when inline input is unsafe")
    parser.add_argument("--scope", help="record scope")
    parser.add_argument("--status", help="record status")
    parser.add_argument("--writer-role", help="writer role label")
    parser.add_argument("--trigger", help="policy trigger label")
    parser.add_argument("--priority", help="policy priority label")
    parser.add_argument("--query", help="query text")
    parser.add_argument("--limit", type=int, help="maximum records")
    parser.add_argument("--path", help="file or fixture path")
    parser.add_argument(
        "--allow-external",
        action="store_true",
        dest="allow_external",
        help="A2/E1: permit a path outside the repo (stored as a sanitized external: origin, never an absolute path)",
    )
    parser.add_argument("--kind", help="artifact or inspect kind")
    parser.add_argument("--category", help="eval category")
    parser.add_argument("--conclusion", help="verification conclusion")
    parser.add_argument("--severity", help="severity label")
    parser.add_argument("--scope-label", help="verification scope label")
    parser.add_argument("--actionability", help="actionability label")
    parser.add_argument("--evidence", help="JSON evidence locations")
    parser.add_argument("--evidence-file", help="read evidence JSON from file when inline quoting is unsafe")
    parser.add_argument("--findings", help="JSON findings")
    parser.add_argument("--findings-file", help="read findings JSON from file when inline quoting is unsafe")
    parser.add_argument("--remedies", help="JSON remedies")
    parser.add_argument("--remedies-file", help="read remedies JSON from file when inline quoting is unsafe")
    parser.add_argument("--artifact-ids", help="JSON artifact id list")
    parser.add_argument("--artifact-ids-file", help="read artifact id JSON from file when inline quoting is unsafe")
    parser.add_argument("--expected-json", help="expected eval JSON")
    parser.add_argument("--expected-json-file", help="read expected eval JSON from file when inline quoting is unsafe")
    parser.add_argument("--root", action="append", help="migration root; repeatable")
    parser.add_argument("--source-id", help="source lock id")
    parser.add_argument("--authority-tier", help="source authority tier")
    parser.add_argument("--url-or-repository", help="source URL or repository")
    parser.add_argument("--version-or-commit", help="source version or commit")
    parser.add_argument("--retrieved-at", help="source retrieval timestamp")
    parser.add_argument("--content-hash", help="source content hash")
    parser.add_argument("--license", help="source license")
    parser.add_argument("--supersedes", help="superseded source id")
    parser.add_argument("--decision", help="IRL Review decision")
    parser.add_argument("--payload-json", help="structured payload JSON")
    parser.add_argument("--payload-json-file", help="read structured payload JSON from file when inline quoting is unsafe")
    parser.add_argument("--symptom", help="anti-pattern symptom")
    parser.add_argument("--maintainability-harm", help="anti-pattern maintainability harm")
    parser.add_argument("--trigger-evidence", help="anti-pattern trigger evidence")
    parser.add_argument("--preferred-correction", help="anti-pattern preferred correction")
    parser.add_argument("--valid-exceptions", help="anti-pattern valid exceptions")
    parser.add_argument("--verification", help="anti-pattern verification")
    return parser


def canonical_operation(raw: str | None) -> str:
    if not raw:
        raise usage_denial("missing operation", required_action="run kaizen.py --help and choose an operation")
    code = ALIAS_TO_CODE.get(raw.lower())
    if not code:
        raise usage_denial(
            f"unknown operation {raw!r}",
            operation=raw,
            required_action="run kaizen.py --help and use an approved operation code or alias",
        )
    return code


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.version:
        return {"status": "OK", "tool_version": TOOL_VERSION}
    operation = canonical_operation(args.operation)
    args.operation = operation

    if operation == "K0":
        return op_lookup(args)
    if operation == "K1":
        if getattr(args, "restamp_manifest", False):
            return {"status": "OK", **db.restamp_manifest()}
        status = db.initialize()
        result = {"status": "OK", "message": "Kaizen DB checked/initialized.", "schema": status}
        if getattr(args, "integrity", False):
            result["integrity"] = db.integrity_scan()
        return result
    if operation == "K2":
        return {"status": "OK", "schema": db.schema_status()}
    if operation == "K3":
        return {"status": "OK", **db.backup_db()}
    if operation == "K6":
        return {"status": "OK", **db.export_manifest()}

    lifecycle_dispatch = {
        "W1": lambda: add_lifecycle_record("tasks", args),
        "W2": lambda: update_lifecycle_record("tasks", args),
        "G1": lambda: add_lifecycle_record("gotcha", args),
        "G2": lambda: list_records("gotcha", args),
        "G3": lambda: query_records("gotcha", args),
        "G4": lambda: inspect_record("gotcha", args),
        "G5": lambda: update_lifecycle_record("gotcha", args),
        "L1": lambda: add_lifecycle_record("learning", args),
        "L2": lambda: promote_gotcha_to_learning(args),
        "L3": lambda: promote_learning_to_learned(args),
        "L4": lambda: list_records("learning", args),
        "L5": lambda: query_records("learning", args),
        "L6": lambda: inspect_record("learning", args),
        "L7": lambda: list_records("learned", args),
        "L8": lambda: query_records("learned", args),
        "L9": lambda: inspect_record("learned", args),
        "L10": lambda: learned_context(args),
    }
    if operation in lifecycle_dispatch:
        result = lifecycle_dispatch[operation]()
        if operation in {"W1", "W2", "G1", "G5", "L1", "L2", "L3"}:
            try:
                add_ledger_event(args, title=f"{operation} {result.get('id', '')}", body=json.dumps(result))
            except Exception as error:
                # The primary record IS committed at this point; failing the whole op here
                # would invite a retry that duplicates it. Surface the gap loudly instead
                # so the durable timeline (the ledger) is never silently incomplete.
                result["ledger_warning"] = f"ledger event not recorded: {error}"
                result["required_action"] = (
                    "the primary record was written but its ledger event was not; "
                    "record it manually with W2 or retry the ledger write"
                )
                print(f"WARNING ledger event not recorded for {operation}: {error}", file=sys.stderr)
        return result

    if operation == "W3":
        return add_plan(args)
    if operation == "W4":
        return revise_plan(args)
    if operation == "W5":
        return add_packet(args, table="subagent_packets", packet_type="request")
    if operation == "W6":
        return add_packet(args, table="subagent_packets", packet_type="ingest")
    if operation == "W7":
        return add_packet(args, table="diagnostic_packets", packet_type="request")
    if operation == "W8":
        return add_packet(args, table="diagnostic_packets", packet_type="ingest")

    quality_dispatch = {
        "Q1": lambda: add_verification(args),
        "Q2": lambda: add_verification(args),
        "Q3": lambda: add_eval_case(args),
        "Q4": lambda: add_eval_run(args),
        "Q5": lambda: add_anti_pattern(args),
        "Q6": lambda: query_anti_patterns(args),
        "Q7": lambda: inspect_quality(args),
        "Q9": lambda: query_verifications(args),
        "A1": lambda: add_artifact(args),
        "A2": lambda: hash_file(args),
        "A3": lambda: inspect_artifact(args),
        "A4": lambda: list_artifacts(args),
        "A5": lambda: verify_artifact_hash(args),
    }
    if operation in quality_dispatch:
        if operation == "Q1" and not args.conclusion:
            args.conclusion = "PROOF_RECORDED"
        return quality_dispatch[operation]()

    if operation == "Q8":
        return output_validate(args)

    if operation in {"T1", "T2", "T3", "T4"}:
        from .trace_records import add_eval_score, add_trace_event, query_eval_scores, trace_report

        if operation == "T1":
            return add_trace_event(args)
        if operation == "T2":
            return add_eval_score(args)
        if operation == "T4":
            return query_eval_scores(args)
        return trace_report(args)

    if operation in {"E1", "E3", "E4", "E5"}:
        from .evidence import chunk_document, ingest_file, inspect_evidence, query_evidence

        if operation == "E1":
            return ingest_file(args)
        if operation == "E3":
            return chunk_document(args)
        if operation == "E4":
            return query_evidence(args)
        return inspect_evidence(args)

    if operation in {"O1", "O2", "O3"}:
        from .lab import add_proposal, assemble_case_set, proposal_report

        if operation == "O1":
            return assemble_case_set(args)
        if operation == "O2":
            return add_proposal(args)
        return proposal_report(args)

    if operation in {"Y1", "Y2", "Y3", "Y4", "Y5"}:
        from .comfyui import comfy_doctor, comfy_inspect, comfy_list, comfy_replay, run_workflow

        if operation == "Y1":
            return run_workflow(args)
        if operation == "Y2":
            return comfy_inspect(args)
        if operation == "Y3":
            return comfy_list(args)
        if operation == "Y4":
            return comfy_replay(args)
        return comfy_doctor(args)

    if operation in {"B1", "B2", "B3"}:
        from .model_ops import model_doctor, model_run, reembed

        if operation == "B1":
            return model_doctor(args)
        if operation == "B2":
            return model_run(args)
        return reembed(args)

    migration_dispatch = {
        "M1": migration_learning.scan_roots,
        "M2": migration_learning.dry_run,
        "M3": migration_learning.apply,
        "M4": migration_learning.verify,
        "M5": migration_learning.migration_report,
    }
    if operation in migration_dispatch:
        return migration_dispatch[operation](args)

    if operation == "R0":
        return session_digest(args)

    if operation in {"R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10", "R11"}:
        return make_report(args)

    if operation in {"S1", "S2", "S3", "S4"}:
        return source_dispatch(operation, args)

    if operation in {"I1", "I2", "I3", "I4", "I5"}:
        return irl_dispatch(operation, args)

    policy_dispatch = {
        "X1": lambda: add_policy(args),
        "X2": lambda: list_policies(args),
        "X3": lambda: query_policies(args),
        "X4": lambda: inspect_policy(args),
        "X5": lambda: session_context(args),
    }
    if operation in policy_dispatch:
        return policy_dispatch[operation]()

    raise usage_denial(f"operation {operation} is recognized but not implemented")


def add_packet(args: argparse.Namespace, *, table: str, packet_type: str) -> dict[str, Any]:
    title = _text_arg(args, "title", f"{packet_type} packet")
    summary = _text_arg(args, "summary", "")
    body = _text_arg(args, "body", "")
    validate_text_fields({"summary": summary, "body": body})
    payload_json = args.payload_json or "{}"
    if args.payload_json_file:
        payload_json = read_text_file(args.payload_json_file)
    if not isinstance(json.loads(payload_json), dict):
        raise KaizenDenied(
            "DENIED_PAYLOAD_TYPE",
            {"required_action": "--payload-json must be a JSON object"},
            exit_code=2,
        )
    record_id = db.new_id("sp" if table == "subagent_packets" else "dp")
    created = db.now()
    content_hash = utc_text_hash({"id": record_id, "title": title, "summary": summary, "body": body, "payload_json": payload_json})

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            f"INSERT INTO {table} "
            "(id, created_at, task_id, packet_type, status, title, summary, body, payload_json, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                created,
                args.task_id,
                packet_type,
                args.status or "recorded",
                title,
                summary,
                body,
                payload_json,
                content_hash,
            ),
        )

    db.write_tx(op)
    return {"status": "OK", "id": record_id, "content_hash": content_hash}


def output_validate(args: argparse.Namespace) -> dict[str, Any]:
    from .schemas import jsonschema_available, list_schemas, validate_record

    kind = getattr(args, "kind", None)
    if not kind:
        return {"status": "OK", "schemas": list_schemas(), "jsonschema_accelerator": jsonschema_available()}
    raw = args.payload_json
    if args.payload_json_file:
        raw = read_text_file(args.payload_json_file)
    if not raw:
        raise usage_denial("missing payload", required_action="pass --payload-json or --payload-json-file")
    payload = json.loads(raw)
    validate_record(kind, payload)
    return {"status": "OK", "record_type": kind, "valid": True, "jsonschema_accelerator": jsonschema_available()}


def source_dispatch(operation: str, args: argparse.Namespace) -> dict[str, Any]:
    if operation == "S1":
        return source_add(args)
    if operation == "S2":
        return source_query(args)
    if operation == "S3":
        return source_inspect(args)
    if operation == "S4":
        return source_export(args)
    raise AssertionError(operation)


def source_add(args: argparse.Namespace) -> dict[str, Any]:
    for field in ("source_id", "authority_tier", "url_or_repository", "summary"):
        if not getattr(args, field, None):
            raise KaizenDenied("DENIED_REQUIRED_FIELDS", {"fields": [field], "required_action": f"resubmit with --{field.replace('_', '-')}"}, exit_code=2)
    body = _text_arg(args, "body", "")
    validate_text_fields({"summary": args.summary, "body": body})
    from .schemas import validate_record

    validate_record(
        "source_lock",
        {
            k: v
            for k, v in {
                "source_id": args.source_id,
                "authority_tier": args.authority_tier,
                "url_or_repository": args.url_or_repository,
                "version_or_commit": getattr(args, "version_or_commit", None),
                "retrieved_at": getattr(args, "retrieved_at", None),
                "content_hash": getattr(args, "content_hash", None),
                "license": getattr(args, "license", None),
                "supersedes": getattr(args, "supersedes", None),
                "summary": args.summary,
                "body": body,
            }.items()
            if v not in (None, "")
        },
    )
    record_id = db.new_id("s")
    created = db.now()

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO source_locks "
            "(id, created_at, source_id, authority_tier, url_or_repository, version_or_commit, retrieved_at, "
            "content_hash, license, supersedes, summary, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                created,
                args.source_id,
                args.authority_tier,
                args.url_or_repository,
                args.version_or_commit,
                args.retrieved_at,
                args.content_hash,
                args.license,
                args.supersedes,
                args.summary,
                body,
            ),
        )

    db.write_tx(op)
    return {"status": "OK", "id": record_id, "source_id": args.source_id}


def source_query(args: argparse.Namespace) -> dict[str, Any]:
    query = args.query or ""
    pattern = f"%{query}%"
    rows = db.fetch_all(
        "SELECT id, source_id, authority_tier, url_or_repository, version_or_commit, summary FROM source_locks "
        "WHERE source_id LIKE ? OR url_or_repository LIKE ? OR summary LIKE ? ORDER BY created_at DESC LIMIT ?",
        (pattern, pattern, pattern, args.limit or 20),
    )
    return {
        "status": "OK",
        "records": [
            {"id": r[0], "source_id": r[1], "authority_tier": r[2], "url_or_repository": r[3], "version_or_commit": r[4], "summary": r[5]}
            for r in rows
        ],
    }


def source_inspect(args: argparse.Namespace) -> dict[str, Any]:
    return inspect_record("source_locks", args)


def source_export(args: argparse.Namespace) -> dict[str, Any]:
    rows = db.fetch_all(
        "SELECT source_id, authority_tier, url_or_repository, version_or_commit, retrieved_at, content_hash, license, supersedes "
        "FROM source_locks ORDER BY source_id"
    )
    data = {
        "generated_at": db.now(),
        "sources": [
            {
                "source_id": r[0],
                "authority_tier": r[1],
                "url_or_repository": r[2],
                "version_or_commit": r[3],
                "retrieved_at": r[4],
                "content_hash": r[5],
                "license": r[6],
                "supersedes": r[7],
            }
            for r in rows
        ],
    }
    path = EXPORT_ROOT / "sources.lock.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"status": "OK", "path": repo_relative(path), "count": len(rows), "sha256": file_sha256(path)}


def irl_dispatch(operation: str, args: argparse.Namespace) -> dict[str, Any]:
    if operation == "I5":
        return irl_report(args)
    event_types = {
        "I1": "review",
        "I2": "prediction",
        "I3": "user_correction",
        "I4": "observed_outcome",
    }
    event_type = event_types[operation]
    decision = args.decision or args.title or event_type
    summary = _text_arg(args, "summary", "")
    body = _text_arg(args, "body", "")
    validate_text_fields({"summary": summary, "body": body})
    record_id = db.new_id("irl")
    review_id = args.review_id or (record_id if operation == "I1" else None)
    if not review_id:
        raise KaizenDenied("DENIED_REVIEW_ID_REQUIRED", {"required_action": "resubmit with --review-id"}, exit_code=2)
    created = db.now()
    content_hash = utc_text_hash({"id": record_id, "review_id": review_id, "event_type": event_type, "summary": summary, "body": body})

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO irl_reviews (id, created_at, review_id, event_type, status, decision, summary, body, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record_id, created, review_id, event_type, args.status or "recorded", decision, summary, body, content_hash),
        )

    db.write_tx(op)
    return {"status": "OK", "id": record_id, "review_id": review_id, "event_type": event_type}


def irl_report(args: argparse.Namespace) -> dict[str, Any]:
    review_id = args.review_id or args.id
    if not review_id:
        raise KaizenDenied("DENIED_REVIEW_ID_REQUIRED", {"required_action": "resubmit with --review-id"}, exit_code=2)
    rows = db.fetch_all(
        "SELECT id, created_at, event_type, status, decision, summary FROM irl_reviews "
        "WHERE review_id = ? ORDER BY created_at",
        (review_id,),
    )
    out_dir = EXPORT_ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Slug the id for the FILENAME only (ids are user-suppliable via --review-id, and a
    # path separator in one turned into a raw ERROR_UNEXPECTED); queries keep the raw id.
    safe_review_id = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in review_id) or "review"
    path = out_dir / f"irl-review-{safe_review_id}.md"
    lines = [f"# IRL Review {review_id}", "", f"Rows: {len(rows)}", ""]
    for row in rows:
        lines.append(f"- `{row[0]}` {row[1]} [{row[2]}:{row[3]}] {row[4]} - {row[5]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"status": "OK", "review_id": review_id, "path": repo_relative(path), "rows": len(rows), "sha256": file_sha256(path)}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.operation and not args.version:
        parser.print_help()
        return 0
    try:
        result = dispatch(args)
        return emit(result, as_json=args.json)
    except KaizenDenied as denied:
        payload = denied.payload()
        payload["exit_code"] = denied.exit_code
        example = remedy_example(denied.code, getattr(args, "operation", None))
        if example:
            payload.setdefault("example", example)
        return emit_error(payload, as_json=args.json)
    except json.JSONDecodeError as error:
        payload = {
            "status": "DENIED",
            "code": "DENIED_JSON_INVALID",
            "message": str(error),
            "required_action": "resubmit valid JSON for the JSON-valued argument",
            "example": remedy_example("DENIED_JSON_INVALID", getattr(args, "operation", None)),
            "exit_code": 2,
        }
        return emit_error(payload, as_json=args.json)
    except Exception as error:
        payload = {
            "status": "ERROR",
            "code": "ERROR_UNEXPECTED",
            "message": str(error),
            "required_action": "rerun with a narrower command or inspect SETUP.md if this is a tooling failure",
            "exit_code": 1,
        }
        return emit_error(payload, as_json=args.json)
