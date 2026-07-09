"""Centralized record vocabularies and the compact schema registry.

`KAIZEN_ENUMS` is the single source of truth for controlled-vocabulary values so
agents cannot invent status/label/enum values. `SCHEMAS` maps a record type to a
compact spec consumed by :mod:`kaizen_components.schemas.validator`.

Enums are applied only to fields whose allowed values are verified against the
real write paths (new trace/score fields, verification conclusions). `scope` and
`status` on existing lifecycle records stay free-form for now so wiring this gate
into existing writes cannot reject records the current code accepts.
"""

from __future__ import annotations

from typing import Any

from ..denials import KaizenDenied


KAIZEN_ENUMS: dict[str, list[str]] = {
    "scope": ["project", "global", "skill", "session"],
    "lifecycle_status": ["active", "recorded", "resolved", "superseded", "draft", "noted", "promoted"],
    "verification_conclusion": [
        "VERIFIED_ACCEPTABLE",
        "ACCEPTABLE_WITH_CONCERNS",
        "NEEDS_HUMAN_DECISION",
        "STRUCTURAL_REWORK_RECOMMENDED",
        "VERIFICATION_FAILED",
        "PROOF_RECORDED",
    ],
    "trace_kind": ["model_call", "tool_call", "evidence_read", "verifier", "subagent", "generative_run", "judge"],
    "trace_level": ["debug", "default", "warning", "error"],
    "score_data_type": ["numeric", "categorical", "boolean"],
    "score_source": ["deterministic", "human", "model"],
    "eval_category": [
        "behavior",
        "should_trigger",
        "should_not_trigger",
        "routing",
        "grounding",
        "proof",
        "security",
        "freshness",
        "learning_regression",
    ],
    "extraction_method": ["native", "pdftext", "ocr", "llm", "manual"],
    # 'neural' is intentionally NOT implemented and stays a reserved, documented value. The peer-reviewed
    # evidence (Qu, Tu & Bao, "Is Semantic Chunking Worth the Computational Cost?", Findings of NAACL 2025;
    # arXiv 2410.13070) finds semantic/clustering chunking not consistently worth its cost over fixed-size
    # on real corpora, so 'recursive' (fixed-size) is the supported default.
    "chunker": ["token", "sentence", "recursive", "code", "semantic", "neural"],
    "authority_tier": ["normative", "official_docs", "implementation", "design_guidance"],
    "generative_status": ["queued", "running", "completed", "failed"],
    "generative_route": ["api", "mcp"],
    "gateway_event_type": ["comfyui_run"],
    # Authoritative agent-run ledger (T5-T8). agent_type/surface/host/sandbox_mode/approval_mode/os
    # describe the runtime ENVELOPE so a run is reproducible across VS Code local/remote/wsl/web/sandbox
    # and Claude/Codex approval+sandbox modes. event_kind x marker is the two-axis lifecycle model the
    # reducer folds (marker is the reducer axis; see the (kind x marker) matrix enforced in the T6 handler).
    "agent_type": ["codex", "claude", "other"],
    "surface": ["vscode-extension", "cli", "app-server", "cloud", "manual"],
    "host": ["local", "remote-ssh", "wsl", "dev-container", "windows-sandbox", "codespaces", "web", "cloud"],
    "sandbox_mode": ["read-only", "workspace-write", "full-access", "danger-full-access", "unknown"],
    "approval_mode": ["manual", "plan", "agent", "on-request", "on-failure", "full-access", "never"],
    "os": ["windows", "macos", "linux"],
    "agent_event_kind": [
        "turn", "subagent", "tool_call", "approval",
        "transport", "auth", "rate_limit", "context", "verification", "finalization",
    ],
    "agent_event_marker": [
        "open", "close_ok", "close_fail", "close_canceled", "resolved", "declined", "timed_out", "point",
    ],
    "agent_failure_category": ["transport", "auth", "rate_limit", "context"],
}


# T6 cross-field rule: validate_against_spec checks each field's enum independently, so it would accept
# a nonsense (event_kind, marker) pair like (transport, resolved). This map is the authoritative set of
# allowed markers per kind; the T6 handler rejects a pair outside it (DENIED_EVENT_KIND_MARKER, exit 2).
AGENT_EVENT_KIND_MARKERS: dict[str, list[str]] = {
    "subagent": ["open", "close_ok", "close_fail", "close_canceled"],
    "approval": ["open", "resolved", "declined", "timed_out"],
    "turn": ["open", "close_ok", "close_fail"],
    "tool_call": ["open", "close_ok", "close_fail"],
    "transport": ["point"],
    "auth": ["point"],
    "rate_limit": ["point"],
    "context": ["point"],
    "verification": ["point"],
    "finalization": ["close_ok", "close_fail", "close_canceled"],
}


SCHEMAS: dict[str, dict[str, Any]] = {
    "trace_event": {
        "required": ["kind", "summary"],
        "allow_extra": False,
        "fields": {
            "task_id": {"type": "str"},
            "trace_id": {"type": "str"},
            "parent_event_id": {"type": "str"},
            "kind": {"type": "str", "enum": KAIZEN_ENUMS["trace_kind"]},
            "name": {"type": "str", "max_words": 40},
            "level": {"type": "str", "enum": KAIZEN_ENUMS["trace_level"]},
            "environment": {"type": "str", "max_words": 20},
            "session_id": {"type": "str"},
            "status": {"type": "str", "max_words": 20},
            "status_message": {"type": "str", "max_words": 200},
            "model": {"type": "str", "max_words": 20},
            "provider": {"type": "str", "max_words": 20},
            "prompt_tokens": {"type": "int", "min": 0},
            "completion_tokens": {"type": "int", "min": 0},
            "total_tokens": {"type": "int", "min": 0},
            "cost": {"type": "float", "min": 0},
            "latency_ms": {"type": "int", "min": 0},
            "tags": {"type": "list"},
            "input_ref": {"type": "str"},
            "output_ref": {"type": "str"},
            "summary": {"type": "str", "summary": True},
            "body": {"type": "str", "max_words": 1000},
        },
    },
    "eval_score": {
        "required": ["name", "value", "data_type", "source"],
        "allow_extra": False,
        "fields": {
            "trace_event_id": {"type": "str"},
            "eval_run_id": {"type": "str"},
            "verification_id": {"type": "str"},
            "name": {"type": "str", "max_words": 40},
            "value": {"type": "any"},
            "data_type": {"type": "str", "enum": KAIZEN_ENUMS["score_data_type"]},
            "source": {"type": "str", "enum": KAIZEN_ENUMS["score_source"]},
            "comment": {"type": "str", "max_words": 200},
        },
    },
    "verification": {
        "required": ["summary"],
        "allow_extra": False,
        "fields": {
            "task_id": {"type": "str"},
            "proof_id": {"type": "str"},
            "conclusion": {"type": "str", "enum": KAIZEN_ENUMS["verification_conclusion"]},
            "summary": {"type": "str", "summary": True},
            "body": {"type": "str", "max_words": 1000},
            "evidence": {"type": "list"},
            "findings": {"type": "list"},
            "remedies": {"type": "list"},
            "severity": {"type": "str", "max_words": 10},
            "scope_label": {"type": "str", "max_words": 10},
            "actionability": {"type": "str", "max_words": 10},
            "artifact_ids": {"type": "list"},
        },
    },
    "gotcha": {
        "required": ["title", "summary", "body"],
        "allow_extra": False,
        "fields": {
            "title": {"type": "str", "max_words": 40},
            "summary": {"type": "str", "summary": True},
            "body": {"type": "str", "max_words": 1000},
            "scope": {"type": "str"},
            "status": {"type": "str"},
            "writer_role": {"type": "str"},
            "task_id": {"type": "str"},
        },
    },
    "source_lock": {
        "required": ["source_id", "authority_tier", "url_or_repository", "summary"],
        "allow_extra": False,
        "fields": {
            "source_id": {"type": "str", "max_words": 40},
            "authority_tier": {"type": "str", "enum": KAIZEN_ENUMS["authority_tier"]},
            "url_or_repository": {"type": "str"},
            "version_or_commit": {"type": "str"},
            "retrieved_at": {"type": "str"},
            "content_hash": {"type": "str"},
            "license": {"type": "str", "max_words": 10},
            "supersedes": {"type": "str"},
            "summary": {"type": "str", "summary": True},
            "body": {"type": "str", "max_words": 1000},
        },
    },
    "evidence_document": {
        "required": ["origin_kind", "origin_ref", "backend", "summary"],
        "allow_extra": False,
        "fields": {
            "source_lock_id": {"type": "str"},
            "task_id": {"type": "str"},
            "origin_kind": {"type": "str", "enum": ["file", "web"]},
            "origin_ref": {"type": "str"},
            "media_type": {"type": "str"},
            "backend": {"type": "str", "max_words": 10},
            "backend_version": {"type": "str"},
            "extraction_method": {"type": "str", "enum": KAIZEN_ENUMS["extraction_method"]},
            "extraction_confidence": {"type": "float", "min": 0, "max": 1},
            "block_count": {"type": "int", "min": 0},
            "chunk_count": {"type": "int", "min": 0},
            "summary": {"type": "str", "summary": True},
        },
    },
    "evidence_chunk": {
        "required": ["text", "chunk_index", "chunker"],
        "allow_extra": False,
        "fields": {
            "document_id": {"type": "str"},
            "source_lock_id": {"type": "str"},
            "chunk_index": {"type": "int", "min": 0},
            "text": {"type": "str", "max_words": 1000},
            "start_index": {"type": "int", "min": 0},
            "end_index": {"type": "int", "min": 0},
            "token_count": {"type": "int", "min": 0},
            "context": {"type": "str", "max_words": 60},
            "chunker": {"type": "str", "enum": KAIZEN_ENUMS["chunker"]},
            "backend": {"type": "str", "max_words": 10},
            "neighbor_prev_id": {"type": "str"},
            "neighbor_next_id": {"type": "str"},
        },
    },
    "improvement_proposal": {
        "required": ["contract", "title", "summary"],
        "allow_extra": False,
        "fields": {
            "contract": {"type": "str", "max_words": 20},
            "title": {"type": "str", "max_words": 40},
            "summary": {"type": "str", "summary": True},
            "body": {"type": "str", "max_words": 1000},
            "metric": {"type": "str", "max_words": 20},
            "baseline_score": {"type": "float"},
            "candidate_score": {"type": "float"},
        },
    },
    "pii_scan": {
        "required": ["regex_hit_count", "model_hit_count", "summary"],
        "allow_extra": False,
        "fields": {
            "task_id": {"type": "str"},
            "trace_id": {"type": "str"},
            "source_ref": {"type": "str", "max_words": 10},
            "regex_hit_count": {"type": "int", "min": 0},
            "model_hit_count": {"type": "int", "min": 0},
            "hits": {"type": "list"},
            "model": {"type": "str", "max_words": 20},
            "provider": {"type": "str", "max_words": 20},
            "summary": {"type": "str", "summary": True},
        },
    },
    "generative_run": {
        "required": ["backend", "template", "workflow_hash", "status", "summary"],
        "allow_extra": False,
        "fields": {
            "task_id": {"type": "str"},
            "backend": {"type": "str", "max_words": 10},
            "template": {"type": "str", "max_words": 20},
            "endpoint": {"type": "str"},
            "workflow_hash": {"type": "str"},
            "workflow_artifact_id": {"type": "str"},
            "seed": {"type": "str", "max_words": 20},
            "models": {"type": "list"},
            "status": {"type": "str", "enum": KAIZEN_ENUMS["generative_status"]},
            "prompt_id": {"type": "str"},
            "output_artifact_ids": {"type": "list"},
            "output_dir": {"type": "str"},
            "latency_ms": {"type": "int", "min": 0},
            "summary": {"type": "str", "summary": True},
        },
    },
    "generative_run_route": {
        "required": ["run_id", "route"],
        "allow_extra": False,
        "fields": {
            "run_id": {"type": "str"},
            "route": {"type": "str", "enum": KAIZEN_ENUMS["generative_route"]},
            "runtime_profile": {"type": "str", "max_words": 40},
            "mcp_candidate": {"type": "str", "max_words": 10},
            "mcp_version": {"type": "str", "max_words": 10},
            "ab_pair_id": {"type": "str"},
            "payload": {"type": "dict"},
        },
    },
    "gateway_event": {
        "required": ["event_type", "status", "summary"],
        "allow_extra": False,
        "fields": {
            "event_type": {"type": "str", "enum": KAIZEN_ENUMS["gateway_event_type"]},
            "status": {"type": "str", "max_words": 20},
            "summary": {"type": "str", "summary": True},
            "body": {"type": "str", "max_words": 1000},
            "payload": {"type": "dict"},
        },
    },
    "agent_run": {
        "required": ["agent_type", "surface", "summary"],
        "allow_extra": False,
        "fields": {
            "task_id": {"type": "str"},
            "agent_type": {"type": "str", "enum": KAIZEN_ENUMS["agent_type"]},
            "surface": {"type": "str", "enum": KAIZEN_ENUMS["surface"]},
            "host": {"type": "str", "enum": KAIZEN_ENUMS["host"]},
            "sandbox_mode": {"type": "str", "enum": KAIZEN_ENUMS["sandbox_mode"]},
            "approval_mode": {"type": "str", "enum": KAIZEN_ENUMS["approval_mode"]},
            "os": {"type": "str", "enum": KAIZEN_ENUMS["os"]},
            "extension_version": {"type": "str", "max_words": 10},
            "agent_version": {"type": "str", "max_words": 10},
            "model": {"type": "str", "max_words": 20},
            "worktree_path": {"type": "str"},
            "cwd": {"type": "str"},
            "git_branch": {"type": "str", "max_words": 20},
            "git_commit": {"type": "str", "max_words": 10},
            "summary": {"type": "str", "summary": True},
            "body": {"type": "str", "max_words": 1000},
        },
    },
    "agent_event": {
        "required": ["event_kind", "marker", "summary"],
        "allow_extra": False,
        "fields": {
            "agent_run_id": {"type": "str"},
            "source_event_id": {"type": "str", "max_words": 20},
            "correlation_id": {"type": "str", "max_words": 20},
            "event_kind": {"type": "str", "enum": KAIZEN_ENUMS["agent_event_kind"]},
            "marker": {"type": "str", "enum": KAIZEN_ENUMS["agent_event_marker"]},
            "code": {"type": "str", "max_words": 20},
            "name": {"type": "str", "max_words": 40},
            "status_message": {"type": "str", "max_words": 200},
            "summary": {"type": "str", "summary": True},
            "body": {"type": "str", "max_words": 1000},
        },
    },
}


def list_schemas() -> list[str]:
    return sorted(SCHEMAS)


def get_schema(record_type: str) -> dict[str, Any]:
    spec = SCHEMAS.get(record_type)
    if spec is None:
        raise KaizenDenied(
            "DENIED_SCHEMA_UNKNOWN_TYPE",
            {
                "record_type": record_type,
                "known_types": list_schemas(),
                "required_action": "pass a --kind from the known record types",
            },
            exit_code=2,
        )
    return spec
