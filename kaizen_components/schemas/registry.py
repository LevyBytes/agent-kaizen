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
from ..orchestration.modes import CONTROLLERS, SESSION_MODES


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
    "generative_status": ["queued", "running", "completed", "completed_no_output", "failed"],
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
        # v8 M-CLAUDE (M5b): point-in-time Claude governor/OTEL kinds, moved out of
        # RESERVED_CLAUDE_EVENT_KINDS now that the hooked governor + OTEL receiver produce them.
        # subagent_start/subagent_stop are NOT enumerated -- the governor maps Claude's SubagentStart/
        # SubagentStop onto the EXISTING 'subagent' span (open/close_*), so no new subagent kind is needed.
        "hook_event", "permission_mode_changed", "compaction",
        # v8 H2.1 durable conversation events (both 'point'). chat_message carries one complete user or
        # assistant message (per-kind body validation in the T6 add path: role/text/source, redacted +
        # size-capped). profile is emitted once at session startup: the requested + effective profile and
        # profile_hash snapshot metadata.
        "chat_message", "profile",
    ],
    "agent_event_marker": [
        "open", "close_ok", "close_fail", "close_canceled", "resolved", "declined", "timed_out", "point",
    ],
    "agent_failure_category": ["transport", "auth", "rate_limit", "context"],
    # v8 M2 orchestration session records (C1-C5). controller/mode mirror
    # orchestration.modes (parsed there; duplicated here for Q8 payload validation). auth_mode is the
    # billing lane: 'subscription'/'api-key' are a RESERVED value space for the deferred M-CLAUDE Claude
    # lanes -- accepted now with no live producer, so the record layer proves it accepts the value space.
    # controller/mode reference orchestration.modes verbatim (single parse source) rather than
    # relisting the values here, so the reserved deferred mode value stays defined in exactly one place.
    "session_controller": list(CONTROLLERS),
    "session_mode": list(SESSION_MODES),
    "session_auth_mode": ["none", "subscription", "api-key"],
    # v8 H2.1 UI permission mode (plan|ask|agent|full). The owner-selected permission ceiling for a
    # driven conversation, carried on agent_sessions/agent_runs. DISTINCT from session_mode (orchestration
    # semantics); mode semantics live in code, mode_profiles stores only designated roots + snapshot meta.
    "permission_mode": ["plan", "ask", "agent", "full"],
    "approval_request_type": ["tool_approval", "clarifying_question", "plan_exit", "requestUserInput"],
    "approval_state": ["open", "approved", "denied", "canceled", "cleared_by_engine", "deferred"],
    "approval_decided_by": ["auto", "human"],
    # v8 M3 policy authority rules (typed DB rules the PolicyEngine layers UNDER the code INVARIANTS).
    # authority_rule_type is the allow/ask/deny lattice (deny > ask > allow, default ask). policy_verb is
    # the RequestedAction verb space plus 'any' (a rule matching every verb). authority_match_kind is the
    # pattern dialect: exact_command (normal-form command equality) or path_prefix (boundary-aware
    # canonical prefix; never allows an exec command). Validated for the 'authority_rule' kind below.
    "authority_rule_type": ["allow", "ask", "deny"],
    "policy_verb": ["tool", "file_read", "file_write", "net", "git", "exec", "spawn", "any"],
    "authority_match_kind": ["exact_command", "path_prefix"],
    # v8 M9 fleet node role (fleet.db nodes.role). ROLE roams (coordinator), SERVICE does not (hub);
    # model-server advertises compute (M13). Validated for the 'fleet_node' kind below.
    "node_role": ["coordinator", "worker", "hub", "model-server"],
    # v8 M13 backend registry lanes (B8 backend_endpoints.lanes). The capability set an endpoint may
    # serve; element-enforced in backend_registry._clean_lanes (the compact 'list' type cannot bound
    # elements). embed is model-bound (the failover data-integrity invariant is keyed off it).
    "backend_lane": ["embed", "text", "judge", "rerank"],
}


# T6 cross-field rule: validate_against_spec checks each field's enum independently, so it would accept
# a nonsense (event_kind, marker) pair like (transport, resolved). This map is the authoritative set of
# allowed markers per kind; the T6 handler rejects a pair outside it (DENIED_EVENT_KIND_MARKER, exit 2).
AGENT_EVENT_KIND_MARKERS: dict[str, list[str]] = {
    "subagent": ["open", "close_ok", "close_fail", "close_canceled"],
    "approval": ["open", "resolved", "declined", "timed_out"],
    # A turn interrupted mid-flight closes canceled, not failed, so its terminal marker must reach T6.
    "turn": ["open", "close_ok", "close_fail", "close_canceled"],
    "tool_call": ["open", "close_ok", "close_fail"],
    "transport": ["point"],
    "auth": ["point"],
    "rate_limit": ["point"],
    "context": ["point"],
    "verification": ["point"],
    "finalization": ["close_ok", "close_fail", "close_canceled"],
    # v8 M-CLAUDE (M5b) point-in-time governor/OTEL kinds. All 'point': a PreToolUse/UserPromptSubmit
    # hook decision, a permission_mode flip (evasion detection, §9 row 18), and a PreCompact context
    # squeeze are instantaneous events, not spans. subagent_start/stop ride the existing 'subagent' span.
    "hook_event": ["point"],
    "permission_mode_changed": ["point"],
    "compaction": ["point"],
    # v8 H2.1 durable conversation events. Both instantaneous (point), not spans. chat_message's body is
    # further validated per-kind in the T6 add path (role in {user,assistant}, text str <=1 MiB, source in
    # {driven,observed}, optional turn_id), redacted before persistence; profile's body is non-secret JSON.
    "chat_message": ["point"],
    "profile": ["point"],
}


# RESERVED Claude/OTEL marker names still awaiting a producer (v8 §4 registry note). As of M-CLAUDE
# (M5b) the governor/OTEL lane LANDED: hook_event / permission_mode_changed / compaction are now LIVE
# (moved into KAIZEN_ENUMS['agent_event_kind'] + AGENT_EVENT_KIND_MARKERS above, all 'point'). The
# subagent-attribution names below stay RESERVED (not enumerated) BECAUSE the governor maps Claude's
# SubagentStart/SubagentStop onto the EXISTING 'subagent' span (open/close_*) rather than mint new kinds
# -- so nothing produces these literal names and registering them would only force an unused op-coverage
# handler. The M9 fleet coord_events kind x marker matrix must still NOT collide with any name here (a
# test asserts the intersection is empty), and the newly-registered live kinds must ALSO stay disjoint
# from COORD markers (asserted in test_hooked_governor.py).
RESERVED_CLAUDE_EVENT_KINDS: tuple[str, ...] = (
    "subagent_start",
    "subagent_stop",
)


# v8 M9 fleet coord_events (kind x marker) matrix. Mirrors AGENT_EVENT_KIND_MARKERS discipline: the
# registry enum can only bound event_kind and marker INDEPENDENTLY, so this map is the authoritative
# set of allowed markers per kind and the FleetStore append path rejects a pair outside it
# (DENIED_COORD_KIND_MARKER, exit 2) exactly like the T6 handler does for agent_events.
#
# These kinds MUST NOT collide with RESERVED_CLAUDE_EVENT_KINDS (a test asserts the intersection is
# empty; a collision would let the deferred M-CLAUDE OTEL lane and the fleet ledger fight over a name).
# Only 'node' and 'heartbeat' have LIVE producers in M9 (D1/D2); coordinator/lease/handoff/dispatch/
# divergence/conflict/resolution are registered now (deferral insurance) but produced at M10a+.
COORD_EVENT_KIND_MARKERS: dict[str, list[str]] = {
    "node": ["registered", "updated", "retired"],
    "heartbeat": ["point"],
    "coordinator": ["claimed", "granted", "released", "denied"],
    "lease": ["requested", "granted", "denied", "released", "revoked", "expired"],
    "handoff": ["started", "pushed", "released", "completed", "aborted"],
    "dispatch": ["requested", "accepted", "started", "completed", "failed", "canceled"],
    "divergence": ["detected", "resolved"],
    "conflict": ["detected", "resolved"],
    "resolution": ["recorded"],
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
            # v8 H2.1 conversation-profile fields (all optional; NULL on legacy rows). session_id soft-links
            # the C1 envelope; model stays the EFFECTIVE model, requested_model the owner-selected one;
            # permission_mode is the UI plan|ask|agent|full setting (distinct from the session's mode).
            "session_id": {"type": "str", "max_words": 20},
            "engine": {"type": "str", "max_words": 20},
            "auth_mode": {"type": "str", "enum": KAIZEN_ENUMS["session_auth_mode"]},
            "requested_model": {"type": "str", "max_words": 20},
            "requested_reasoning_effort": {"type": "str", "max_words": 20},
            "reasoning_effort": {"type": "str", "max_words": 20},
            "permission_mode": {"type": "str", "enum": KAIZEN_ENUMS["permission_mode"]},
            "profile_hash": {"type": "str", "max_words": 10},
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
    "agent_session": {
        "required": ["controller", "mode", "auth_mode", "summary"],
        "allow_extra": False,
        "fields": {
            "task_id": {"type": "str"},
            "controller": {"type": "str", "enum": KAIZEN_ENUMS["session_controller"]},
            "mode": {"type": "str", "enum": KAIZEN_ENUMS["session_mode"]},
            "engine": {"type": "str", "max_words": 20},
            "auth_mode": {"type": "str", "enum": KAIZEN_ENUMS["session_auth_mode"]},
            "owning_node": {"type": "str", "max_words": 20},
            "node_epoch": {"type": "int", "min": 0},
            "vendor_session_root_id": {"type": "str", "max_words": 20},
            "vendor_thread_id": {"type": "str", "max_words": 20},
            "cwd": {"type": "str"},
            "git_branch": {"type": "str", "max_words": 20},
            "state": {"type": "str", "max_words": 20},
            # v8 H2.1 conversation-profile fields (all optional; NULL on legacy rows). requested_model /
            # requested_reasoning_effort are the owner-selected values; permission_mode is the UI
            # plan|ask|agent|full setting (distinct from mode, which keeps orchestration semantics).
            "requested_model": {"type": "str", "max_words": 20},
            "requested_reasoning_effort": {"type": "str", "max_words": 20},
            "permission_mode": {"type": "str", "enum": KAIZEN_ENUMS["permission_mode"]},
            "profile_hash": {"type": "str", "max_words": 10},
            # Nullable in storage / optional on legacy C1 payloads. Canonical whitespace and the
            # 80-code-point limit are enforced by session_records (the compact schema supports words,
            # not Unicode-code-point bounds).
            "title": {"type": "str"},
            # The immutable PolicySnapshot JSON for daemon-restart continuation (policy.snapshot_to_json).
            "policy_snapshot": {"type": "str"},
            "summary": {"type": "str", "summary": True},
        },
    },
    "user_instruction": {
        "required": ["session_id", "instruction", "summary"],
        "allow_extra": False,
        "fields": {
            "session_id": {"type": "str"},
            "instruction": {"type": "str", "max_words": 1000},
            "summary": {"type": "str", "summary": True},
        },
    },
    "goal": {
        "required": ["session_id", "title", "summary"],
        "allow_extra": False,
        "fields": {
            "session_id": {"type": "str"},
            "state": {"type": "str", "max_words": 20},
            "title": {"type": "str", "max_words": 40},
            "summary": {"type": "str", "summary": True},
            "body": {"type": "str", "max_words": 1000},
        },
    },
    "approval_request": {
        "required": ["session_id", "request_type", "state", "summary"],
        "allow_extra": False,
        "fields": {
            "session_id": {"type": "str"},
            "correlation_id": {"type": "str", "max_words": 20},
            "request_type": {"type": "str", "enum": KAIZEN_ENUMS["approval_request_type"]},
            "state": {"type": "str", "enum": KAIZEN_ENUMS["approval_state"]},
            "decided_by": {"type": "str", "enum": KAIZEN_ENUMS["approval_decided_by"]},
            "rule_id": {"type": "str", "max_words": 20},
            "summary": {"type": "str", "summary": True},
        },
    },
    "mode_profile": {
        "required": ["name", "profile_json", "summary"],
        "allow_extra": False,
        "fields": {
            # profile_json is a per-engine extensible mapping serialized to a JSON string; validated
            # per-engine at consume time (M-CLAUDE drops Claude's shape in with zero migration).
            "name": {"type": "str", "max_words": 40},
            "profile_json": {"type": "str"},
            "summary": {"type": "str", "summary": True},
        },
    },
    "authority_rule": {
        # v8 M3 DB-overridable policy rule (loaded by orchestration.policy). rule_type/verb/match_kind are
        # enum-bound; pattern is the free-form normal-form command or canonical path prefix; engine is an
        # optional scoping label (NULL = any engine). Rows layer UNDER the code INVARIANTS -- an allow row
        # can never widen a hard-deny.
        "required": ["rule_type", "verb", "match_kind", "pattern", "summary"],
        "allow_extra": False,
        "fields": {
            "rule_type": {"type": "str", "enum": KAIZEN_ENUMS["authority_rule_type"]},
            "verb": {"type": "str", "enum": KAIZEN_ENUMS["policy_verb"]},
            "match_kind": {"type": "str", "enum": KAIZEN_ENUMS["authority_match_kind"]},
            "pattern": {"type": "str"},
            "engine": {"type": "str", "max_words": 20},
            "summary": {"type": "str", "summary": True},
        },
    },
    # v8 M9 fleet node registration payload (D1). role is enum-bound to node_role; tailnet_name is a
    # MagicDNS name (never a raw IP). capabilities_json is an extensible mapping (has_gpu/model_endpoints/
    # is_pinned) validated per-field at consume time (M13). os/arch are stamped from platform at write.
    "fleet_node": {
        "required": ["node_id", "role", "summary"],
        "allow_extra": False,
        "fields": {
            "node_id": {"type": "str", "max_words": 10},
            "role": {"type": "str", "enum": KAIZEN_ENUMS["node_role"]},
            "tailnet_name": {"type": "str", "max_words": 20},
            "os": {"type": "str", "max_words": 10},
            "arch": {"type": "str", "max_words": 10},
            "capabilities_json": {"type": "dict"},
            "summary": {"type": "str", "summary": True},
        },
    },
    # v8 M9 coord_events append payload (fleet.db coordination ledger). event_kind is enum-bound to the
    # COORD_EVENT_KIND_MARKERS key set; the (kind x marker) PAIR is validated in the FleetStore append
    # path (like T6) because the registry enum cannot express pairs. payload is an extensible mapping.
    "coord_event": {
        "required": ["node_id", "event_kind", "marker", "summary"],
        "allow_extra": False,
        "fields": {
            "node_id": {"type": "str", "max_words": 10},
            "project_id": {"type": "str", "max_words": 10},
            "event_kind": {"type": "str", "enum": list(COORD_EVENT_KIND_MARKERS)},
            "marker": {"type": "str", "enum": sorted({
                marker for markers in COORD_EVENT_KIND_MARKERS.values() for marker in markers
            })},
            "scope_key": {"type": "str", "max_words": 40},
            "epoch": {"type": "int", "min": 0},
            "payload": {"type": "dict"},
            "summary": {"type": "str", "summary": True},
        },
    },
    # v8 M10a D4 coordinator-role action payload (validated before coordination.py acts). action is the
    # claim|transfer|release verb; to_node is the transfer target (required by the D4 handler for
    # transfer); mode is the roaming|pinned role mode. The coord_events this drives are already in the
    # COORD_EVENT_KIND_MARKERS coordinator/conflict matrices (M10a adds producers, no new kinds).
    # M10b: iso (bool) requests an isolated claim/transfer that stamps the §B.3 iso: epoch sentinel on
    # the coord_event payload (RECORD-level; reconcile is M15).
    "coordinator_action": {
        "required": ["action", "node_id", "summary"],
        "allow_extra": False,
        "fields": {
            "action": {"type": "str", "enum": ["claim", "transfer", "release"]},
            "node_id": {"type": "str", "max_words": 10},
            "to_node": {"type": "str", "max_words": 10},
            "mode": {"type": "str", "enum": ["roaming", "pinned"]},
            "iso": {"type": "bool"},
            "summary": {"type": "str", "summary": True},
        },
    },
    # v8 M10a D5 lease action payload (validated before coordination.py acts). action is the
    # request|grant|renew|release|handoff verb; scope_key is the lease scope; holder is the grantee node
    # (grantor grants on its behalf); ttl_s is the lease lifetime; mode is advisory|authoritative
    # (authoritative is RECORDED but not enforced until M10b). Drives lease/handoff coord_events already
    # in COORD_EVENT_KIND_MARKERS.
    "lease_action": {
        "required": ["action", "scope_key", "node_id", "summary"],
        "allow_extra": False,
        "fields": {
            "action": {"type": "str", "enum": ["request", "grant", "renew", "release", "handoff"]},
            "scope_key": {"type": "str", "max_words": 40},
            "node_id": {"type": "str", "max_words": 10},
            "holder": {"type": "str", "max_words": 10},
            "ttl_s": {"type": "int", "min": 0},
            "mode": {"type": "str", "enum": ["advisory", "authoritative"]},
            "summary": {"type": "str", "summary": True},
        },
    },
    # v8 M14 D7 remote-dispatch action payload (validated before dispatch_remote.py acts). action is the
    # request|accept|start|complete|fail|cancel|apply|list verb; target_node/task/scope_key are the
    # request fields; dispatch_id addresses an existing dispatch; reason is a short failure/cancel class
    # (rides the synced payload -- never a home path). Drives dispatch coord_events already in
    # COORD_EVENT_KIND_MARKERS (M14 adds a producer, no new kinds). summary is required like every D-op.
    "remote_dispatch": {
        "required": ["action", "summary"],
        "allow_extra": False,
        "fields": {
            "action": {"type": "str", "enum": ["request", "accept", "start", "complete", "fail", "cancel", "apply", "list"]},
            "target_node": {"type": "str", "max_words": 10},
            "task": {"type": "str", "max_words": 40},
            "scope_key": {"type": "str", "max_words": 40},
            "dispatch_id": {"type": "str", "max_words": 20},
            "reason": {"type": "str", "max_words": 60},
            "summary": {"type": "str", "summary": True},
        },
    },
    # v8 M15 D9 reconcile action payload (validated before reconcile.py acts). action is the
    # reconcile|sweep|status leg; hub_remote is an optional operator-provided git remote for the
    # reconcile git-fetch/publish leg (transport-checked inside mirror.fetch_with_fallback);
    # stale_after_s/skew_margin_s tune the orphan-sweep staleness window (§9 row 10 skew tolerance);
    # allow_publish is bool (owner-gated -- the CLI records it False, never enables publication at M15).
    # Drives lease/divergence/conflict/resolution coord_events already in COORD_EVENT_KIND_MARKERS (M15
    # adds a producer, no new kinds). summary is required like every D-op.
    "fleet_reconcile": {
        "required": ["action", "summary"],
        "allow_extra": False,
        "fields": {
            "action": {"type": "str", "enum": ["reconcile", "sweep", "status"]},
            "hub_remote": {"type": "str"},
            "stale_after_s": {"type": "int", "min": 1},
            "skew_margin_s": {"type": "int", "min": 0},
            "allow_publish": {"type": "bool"},
            "summary": {"type": "str", "summary": True},
        },
    },
    # v8 M13 backend registry (B8) endpoint payload -- one remote model endpoint (plan §C.3). base_url is
    # the OpenAI-compatible endpoint; lanes is the capability list (subset of backend_lane, element-enum
    # enforced by backend_registry._clean_lanes since the compact list type cannot bound elements); model
    # is the model identity the endpoint serves (the embed-lane failover invariant is keyed off it);
    # priority orders failover (lower tried first); node_id soft-links the advertising fleet node. Mirrors
    # fleet_node's shape (required base_url/lanes/model; optional priority/node_id/summary).
    "backend_endpoint": {
        "required": ["base_url", "lanes", "model", "summary"],
        "allow_extra": False,
        "fields": {
            "base_url": {"type": "str"},
            "lanes": {"type": "list"},
            "model": {"type": "str", "max_words": 20},
            "priority": {"type": "int", "min": 0},
            "node_id": {"type": "str", "max_words": 10},
            "summary": {"type": "str", "summary": True},
        },
    },
}


def list_schemas() -> list[str]:
    """Returns known record types, sorted."""
    return sorted(SCHEMAS)


def get_schema(record_type: str) -> dict[str, Any]:
    """Returns the compact spec for record_type; raises KaizenDenied(DENIED_SCHEMA_UNKNOWN_TYPE, exit 2) when unknown."""
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
