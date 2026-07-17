"""Declare the additive Kaizen and fleet schemas, indexes, purge rules, integrity references, and manifest hashes."""

from __future__ import annotations

import hashlib

from . import TOOL_VERSION


SCHEMA_VERSION = 1
MIGRATION_ID = "kaizen-system-foundation-v1"


DDL = [
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        id TEXT PRIMARY KEY,
        schema_version INTEGER NOT NULL,
        migration_id TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        tool_version TEXT NOT NULL,
        manifest_hash TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gotcha (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        scope TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        source_task_id TEXT,
        source_command TEXT,
        writer_role TEXT,
        artifact_ids_json TEXT,
        content_hash TEXT NOT NULL,
        current_revision_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gotcha_revision (
        id TEXT PRIMARY KEY,
        gotcha_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revision_number INTEGER NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        previous_hash TEXT,
        source_command TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS learning (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        scope TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        source_task_id TEXT,
        source_command TEXT,
        writer_role TEXT,
        artifact_ids_json TEXT,
        source_gotcha_id TEXT,
        content_hash TEXT NOT NULL,
        current_revision_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS learning_revision (
        id TEXT PRIMARY KEY,
        learning_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revision_number INTEGER NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        previous_hash TEXT,
        source_command TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS learned (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        scope TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        source_task_id TEXT,
        source_command TEXT,
        writer_role TEXT,
        artifact_ids_json TEXT,
        source_learning_id TEXT,
        content_hash TEXT NOT NULL,
        current_revision_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS learned_revision (
        id TEXT PRIMARY KEY,
        learned_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revision_number INTEGER NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        previous_hash TEXT,
        source_command TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        scope TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        source_command TEXT,
        writer_role TEXT,
        content_hash TEXT NOT NULL,
        current_revision_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_revision (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revision_number INTEGER NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        previous_hash TEXT,
        source_command TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plans (
        id TEXT PRIMARY KEY,
        task_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        source_command TEXT,
        writer_role TEXT,
        content_hash TEXT NOT NULL,
        current_revision_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_revision (
        id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revision_number INTEGER NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        previous_hash TEXT,
        source_command TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_events (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        task_id TEXT,
        scope TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        source_command TEXT,
        writer_role TEXT,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS verification_events (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        task_id TEXT,
        proof_id TEXT,
        conclusion TEXT NOT NULL,
        evidence_locations_json TEXT NOT NULL,
        findings_json TEXT NOT NULL,
        remedies_json TEXT NOT NULL,
        severity TEXT,
        scope_label TEXT,
        actionability TEXT,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        artifact_ids_json TEXT,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        task_id TEXT,
        kind TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT,
        bytes INTEGER,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS eval_cases (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        category TEXT NOT NULL,
        scope TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        fixture_path TEXT,
        expected_json TEXT,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS eval_runs (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        eval_case_id TEXT,
        status TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        artifact_ids_json TEXT,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_locks (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        source_id TEXT NOT NULL,
        authority_tier TEXT NOT NULL,
        url_or_repository TEXT NOT NULL,
        version_or_commit TEXT,
        retrieved_at TEXT,
        content_hash TEXT,
        license TEXT,
        supersedes TEXT,
        summary TEXT NOT NULL,
        body TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS irl_reviews (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        review_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        status TEXT NOT NULL,
        decision TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subagent_packets (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        task_id TEXT,
        packet_type TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS diagnostic_packets (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        task_id TEXT,
        packet_type TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS anti_patterns (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        scope TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        symptom TEXT NOT NULL,
        maintainability_harm TEXT NOT NULL,
        trigger_evidence TEXT NOT NULL,
        preferred_correction TEXT NOT NULL,
        valid_exceptions TEXT NOT NULL,
        verification TEXT NOT NULL,
        summary TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agentgateway_events (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        event_type TEXT NOT NULL,
        status TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reports (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        report_type TEXT NOT NULL,
        scope TEXT NOT NULL,
        path TEXT NOT NULL,
        summary TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS private_policy (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        scope TEXT NOT NULL,
        trigger TEXT NOT NULL,
        priority TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        source_command TEXT,
        writer_role TEXT,
        content_hash TEXT NOT NULL,
        current_revision_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS private_policy_revision (
        id TEXT PRIMARY KEY,
        policy_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revision_number INTEGER NOT NULL,
        trigger TEXT NOT NULL,
        priority TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        previous_hash TEXT,
        source_command TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_documents (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        source_lock_id TEXT,
        task_id TEXT,
        origin_kind TEXT NOT NULL,
        origin_ref TEXT NOT NULL,
        media_type TEXT,
        backend TEXT NOT NULL,
        backend_version TEXT,
        extraction_method TEXT,
        extraction_confidence REAL,
        block_count INTEGER,
        chunk_count INTEGER,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        is_test INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_blocks (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        document_id TEXT NOT NULL,
        block_index INTEGER NOT NULL,
        block_type TEXT NOT NULL,
        page_no INTEGER,
        bbox_json TEXT,
        section_path TEXT,
        text TEXT NOT NULL,
        image_ref TEXT,
        extraction_method TEXT,
        confidence REAL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_chunks (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        document_id TEXT NOT NULL,
        source_lock_id TEXT,
        chunk_index INTEGER NOT NULL,
        text TEXT NOT NULL,
        start_index INTEGER,
        end_index INTEGER,
        token_count INTEGER,
        context TEXT,
        chunker TEXT NOT NULL,
        backend TEXT,
        neighbor_prev_id TEXT,
        neighbor_next_id TEXT,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trace_events (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        task_id TEXT,
        trace_id TEXT,
        parent_event_id TEXT,
        kind TEXT NOT NULL,
        name TEXT,
        level TEXT NOT NULL,
        environment TEXT,
        session_id TEXT,
        status TEXT NOT NULL,
        status_message TEXT,
        model TEXT,
        provider TEXT,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        total_tokens INTEGER,
        cost REAL,
        latency_ms INTEGER,
        tags_json TEXT,
        input_ref TEXT,
        output_ref TEXT,
        redaction_status TEXT,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        is_test INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS eval_scores (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        trace_event_id TEXT,
        eval_run_id TEXT,
        verification_id TEXT,
        name TEXT NOT NULL,
        value_num REAL,
        value_label TEXT,
        data_type TEXT NOT NULL,
        source TEXT NOT NULL,
        comment TEXT,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS improvement_proposals (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        contract TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        baseline_score REAL,
        candidate_score REAL,
        metric TEXT,
        payload_json TEXT,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS generative_runs (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        task_id TEXT,
        backend TEXT NOT NULL,
        template TEXT NOT NULL,
        endpoint TEXT,
        workflow_hash TEXT NOT NULL,
        workflow_artifact_id TEXT,
        seed TEXT,
        models_json TEXT,
        status TEXT NOT NULL,
        prompt_id TEXT,
        output_artifact_ids_json TEXT,
        output_dir TEXT,
        latency_ms INTEGER,
        summary TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pii_scan (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        task_id TEXT,
        trace_id TEXT,
        source_ref TEXT,
        regex_hit_count INTEGER NOT NULL,
        model_hit_count INTEGER NOT NULL,
        hits_json TEXT NOT NULL,
        model TEXT,
        provider TEXT,
        summary TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        is_test INTEGER NOT NULL DEFAULT 0
    )
    """,
    # Per-(chunk, model) embedding index (additive; SCHEMA_VERSION stays 1). Replaces the single inline
    # evidence_chunks.embedding/embedding_model: one row per model lets several embedders index the
    # same corpus at once, so an embedder swap is a background/reversible re-index, not a blocking
    # full re-vector. evidence_chunks.text stays the source of truth; these rows are a rebuildable
    # derived index. `embedding` is a Turso vector32() BLOB. is_test mirrors the parent document so
    # K7 purge cascades (by chunk -> document is_test) and its own is_test=1 rows are removable.
    """
    CREATE TABLE IF NOT EXISTS chunk_embeddings (
        chunk_id        TEXT NOT NULL,
        embedding_model TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        embedding       BLOB NOT NULL,
        created_at      TEXT NOT NULL,
        is_test         INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chunk_id, embedding_model)
    )
    """,
    # Durable key/value settings (additive; SCHEMA_VERSION stays 1). Holds `active_embedding_model` (the model E4/O5 read
    # for retrieval), so the active index is a stored fact, not an env-var guess. Config, NOT
    # is_test-scoped: excluded from the K7 purge.
    """
    CREATE TABLE IF NOT EXISTS db_settings (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    # Route/A-B execution metadata for generative runs (one row per run). Child of
    # generative_runs: route lane (api|mcp), managed-runtime profile, MCP candidate identity,
    # and the ab_pair_id grouping the two runs of a Y9 pair. payload_json reserves room for
    # future route fields without another DDL change. is_test mirrors the parent run so K7
    # purge removes it with the run. Additive-only: no SCHEMA_VERSION bump (owner freeze).
    """
    CREATE TABLE IF NOT EXISTS generative_run_routes (
        run_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        route TEXT NOT NULL,
        runtime_profile TEXT,
        mcp_candidate TEXT,
        mcp_version TEXT,
        ab_pair_id TEXT,
        payload_json TEXT,
        is_test INTEGER NOT NULL DEFAULT 0
    )
    """,
    # Authoritative orchestration ledger (additive; SCHEMA_VERSION stays 1). agent_runs is one
    # execution ENVELOPE per tracked agent run (runtime host/sandbox/approval reproducibility fields,
    # SOFT-linked to task_id like the other annotational task_id columns). state/failure_category are a
    # DENORMALIZED cache for R0/T7 display ONLY -- never authoritative: gates and the K1 child-leak
    # invariant recompute run state from agent_events (the source of truth). is_test comes via the
    # ADDITIVE path (root table -> TEST_ROOT_TABLES), so it is not in this CREATE DDL.
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        task_id TEXT,
        agent_type TEXT NOT NULL,
        surface TEXT NOT NULL,
        host TEXT,
        sandbox_mode TEXT,
        approval_mode TEXT,
        os TEXT,
        extension_version TEXT,
        agent_version TEXT,
        model TEXT,
        worktree_path TEXT,
        cwd TEXT,
        git_branch TEXT,
        git_commit TEXT,
        state TEXT,
        failure_category TEXT,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    # Append-only lifecycle events for one run (child of agent_runs via the HARD agent_run_id link,
    # K1-orphan-scanned). This is the AUTHORITATIVE input the reducer folds into run state. Each row is
    # (event_kind x marker) with an optional correlation_id span key (child/approval/turn/tool id).
    # sequence_no is a harness-assigned gapless MAX+1 per run (never emitter-supplied). source_event_id
    # is a nullable vendor dedup key; the partial UNIQUE index + INSERT OR IGNORE make replay idempotent.
    # No is_test column: purged via the parent run's is_test (TEST_PURGE_SQL cascade), like revisions.
    """
    CREATE TABLE IF NOT EXISTS agent_events (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        agent_run_id TEXT NOT NULL,
        sequence_no INTEGER NOT NULL,
        source_event_id TEXT,
        correlation_id TEXT,
        event_kind TEXT NOT NULL,
        marker TEXT NOT NULL,
        code TEXT,
        name TEXT,
        status_message TEXT,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    # Orchestration session records (v8 M2 / C1-C5, additive; SCHEMA_VERSION stays 1). agent_sessions
    # is one controlled session envelope: controller/mode carry the v8 workflow enums (see
    # orchestration.modes CONTROLLERS / SESSION_MODES), auth_mode the billing lane (none|subscription|api-key -- the
    # subscription/api-key value space is RESERVED for the deferred M-CLAUDE Claude lanes, accepted now
    # with no live producer). owning_node/node_epoch are the fleet fence columns (NULL until the daemon
    # writes them); vendor_session_root_id/vendor_thread_id key back to the vendor session (forks keep
    # the root). is_test via the ADDITIVE path (root table -> TEST_ROOT_TABLES), not this CREATE DDL.
    """
    CREATE TABLE IF NOT EXISTS agent_sessions (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        task_id TEXT,
        controller TEXT NOT NULL,
        mode TEXT NOT NULL,
        engine TEXT,
        auth_mode TEXT NOT NULL,
        owning_node TEXT,
        node_epoch INTEGER,
        vendor_session_root_id TEXT,
        vendor_thread_id TEXT,
        cwd TEXT,
        git_branch TEXT,
        state TEXT NOT NULL,
        summary TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    # Session-scoped user instruction records (child of agent_sessions via session_id). Append-only:
    # one row per instruction the controller injects into a session. seq is a per-session gapless
    # ordinal so C5 renders instructions in submission order. is_test via the ADDITIVE path.
    """
    CREATE TABLE IF NOT EXISTS user_instructions (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        session_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        instruction TEXT NOT NULL,
        summary TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    # Goal create/update lifecycle (child of agent_sessions). The Kaizen goal is the canonical
    # cross-vendor goal projection (engines with no native goal store map to C3). state tracks the
    # goal lifecycle; C3 update rewrites the row in place (no revision table -- goals are lightweight
    # and their history is the session timeline). is_test via the ADDITIVE path.
    """
    CREATE TABLE IF NOT EXISTS goals (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        session_id TEXT NOT NULL,
        state TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        body TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    # Approval-request view projection (child of agent_sessions). correlation_id keys back to an
    # agent_events span (the supervisor's approval span); request_type/state are the C4 state machine
    # (state {open, approved, denied, canceled, cleared_by_engine, deferred}). decided_by {auto|human}
    # and rule_id stay NULL until a decision lands. C4 update is the state machine: an already-decided
    # request refuses re-decision. is_test via the ADDITIVE path.
    """
    CREATE TABLE IF NOT EXISTS approval_requests (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        session_id TEXT NOT NULL,
        correlation_id TEXT,
        request_type TEXT NOT NULL,
        state TEXT NOT NULL,
        decided_by TEXT,
        rule_id TEXT,
        summary TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    # Named per-engine mode profiles (v8 M2 exit criterion). profile_json is an EXTENSIBLE per-engine
    # JSON mapping keyed by engine (validated per-engine at consume time): Claude's permission-mode /
    # allow-rules shape drops in additively at M-CLAUDE with ZERO migration -- this shape is the hard
    # contract. Config-like root table; carries is_test via the ADDITIVE path so live-test profiles purge.
    """
    CREATE TABLE IF NOT EXISTS mode_profiles (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        name TEXT NOT NULL,
        profile_json TEXT NOT NULL,
        summary TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    # Typed policy-authority rules for the M3 decision chokepoint (additive; SCHEMA_VERSION stays 1).
    # Each row is one DB-overridable allow/ask/deny rule the PolicyEngine layers UNDER the code
    # INVARIANTS (a rule can never widen a hard-deny). rule_type {allow,ask,deny}; verb the policy verb
    # ('any' = every verb); match_kind {exact_command, path_prefix}; pattern the normal-form command or
    # canonical path prefix; engine NULL matches any engine, else must equal the actor engine; enabled
    # gates the row (disabled = ignored). SCHEMA-ONLY until a live producer -- validated via the
    # 'authority_rule' kind and stored/loaded by orchestration.policy (store_rule/load_rules); no CLI op
    # or args.py wiring lands here (the mode_profiles precedent). is_test via the ADDITIVE path (root
    # table -> TEST_ROOT_TABLES), so it is not in this CREATE DDL.
    """
    CREATE TABLE IF NOT EXISTS authority_rules (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        rule_type TEXT NOT NULL,
        verb TEXT NOT NULL,
        match_kind TEXT NOT NULL,
        pattern TEXT NOT NULL,
        engine TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        summary TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    # v8 M13 backend registry (B8) -- remote model endpoints normalized into kaizen.db (relational >
    # JSON blob, plan §C.3). One row per advertised endpoint: base_url + the lanes it serves
    # (JSON list subset of ["embed","text","judge","rerank"]) + the model it exposes. priority orders
    # failover (lower = tried first, matching how a preference list reads); health/last_probe are the
    # B8 --probe health cache the resolver reads WITHOUT re-probing (no hot loop); enabled gates a row
    # out of resolution without deleting it. node_id soft-links a fleet node (the model-server that
    # advertises it; NULL for a plain local/env endpoint). Env vars remain the zero-config single-endpoint
    # path -- an empty table means resolve_endpoint() returns None and callers fall back to the env-var
    # backend UNCHANGED. is_test via the ADDITIVE path (root table -> TEST_ROOT_TABLES), not this CREATE DDL.
    # content_hash remains nullable for legacy schema compatibility; backend_registry always writes it.
    """
    CREATE TABLE IF NOT EXISTS backend_endpoints (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        node_id TEXT,
        base_url TEXT NOT NULL,
        lanes TEXT NOT NULL,
        model TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 100,
        health TEXT,
        last_probe TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        content_hash TEXT
    )
    """,
    # Validated skill-context snapshots (additive; SCHEMA_VERSION stays 1). Python skill-management
    # code is authoritative for discovery, validation, links, indexes, and host policy. These tables
    # retain only portable metadata, complete snapshot provenance, and reconciliation events; skill
    # prose and machine-absolute paths remain on disk and are read only after a live hash check.
    """
    CREATE TABLE IF NOT EXISTS skill_context_syncs (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('complete')),
        plan_hash TEXT NOT NULL,
        inventory_hash TEXT NOT NULL,
        skill_count INTEGER NOT NULL CHECK (skill_count >= 0),
        surface_count INTEGER NOT NULL CHECK (surface_count >= 0),
        hosts_json TEXT NOT NULL,
        is_current INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
        summary TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_contexts (
        id TEXT PRIMARY KEY,
        sync_id TEXT NOT NULL,
        skill_name TEXT NOT NULL,
        description TEXT NOT NULL,
        covers_json TEXT NOT NULL,
        skill_md_relpath TEXT NOT NULL,
        skill_sha256 TEXT NOT NULL,
        package_sha256 TEXT NOT NULL,
        validation_status TEXT NOT NULL CHECK (validation_status IN ('valid', 'invalid')),
        validation_errors_json TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_context_surfaces (
        id TEXT PRIMARY KEY,
        context_id TEXT NOT NULL,
        host TEXT NOT NULL CHECK (host IN ('codex', 'claude')),
        surface_relpath TEXT NOT NULL,
        path_kind TEXT NOT NULL CHECK (path_kind IN ('link', 'real_directory', 'missing')),
        validation_status TEXT NOT NULL CHECK (
            validation_status IN ('missing', 'correct', 'wrong_target', 'dangling_link', 'real_directory')
        ),
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_context_events (
        id TEXT PRIMARY KEY,
        sync_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (event_type IN ('added', 'updated', 'retired', 'snapshot_applied')),
        skill_name TEXT,
        summary TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
]


# Additive columns applied to EXISTING DBs (CREATE TABLE IF NOT EXISTS never alters a live table).
# Idempotent: applied only when PRAGMA table_info shows the column absent. (table, column, type-def).
# These carry the is_test marker so live-integration test rows can be purged safely (K7 purge-test).
# is_test marks a row as a removable live-test record (K7 purge-test). Every ROOT record table an op
# writes carries it; revision/child tables inherit removal via their parent link (below), so they need
# no column. Added via the ADDITIVE path (not the CREATE TABLE DDL), so ddl_sha256 / the manifest is
# UNCHANGED -- no SCHEMA_VERSION bump and no K1 --restamp-manifest required.
_IS_TEST = "INTEGER NOT NULL DEFAULT 0"
# Root record tables that carry is_test (evidence_documents/trace_events/pii_scan already had it).
TEST_ROOT_TABLES: tuple[str, ...] = (
    "gotcha", "learning", "learned", "tasks", "plans", "ledger_events", "verification_events",
    "artifacts", "eval_cases", "eval_runs", "eval_scores", "source_locks", "irl_reviews",
    "subagent_packets", "diagnostic_packets", "anti_patterns", "agentgateway_events", "reports",
    "private_policy", "improvement_proposals", "generative_runs",
    "evidence_documents", "trace_events", "pii_scan", "agent_runs",
    # v8 M2 session records (each is a root that carries is_test via the ADDITIVE path).
    "agent_sessions", "user_instructions", "goals", "approval_requests", "mode_profiles",
    # v8 M3 policy authority rules (root; is_test via the ADDITIVE path).
    "authority_rules",
    # v8 M13 backend registry endpoints (root; is_test via the ADDITIVE path so live-test rows purge).
    "backend_endpoints",
    # Skill context snapshot root; contexts/surfaces/events purge through their owning sync.
    "skill_context_syncs",
)
ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [(t, "is_test", _IS_TEST) for t in TEST_ROOT_TABLES]
# v8 M9 fleet fence columns on kaizen.db root tables (NULL-default; applied via the ADDITIVE path so
# ddl_sha256 / the manifest is UNCHANGED -- no SCHEMA_VERSION bump, no K1 --restamp-manifest). SCHEMA-ONLY
# in M9: no producer writes them yet (the daemon fills node_id/owning_node/node_epoch at M10a+).
# owning_node/node_epoch also appear in current CREATE DDL for fresh databases; retaining them here
# migrates older live databases that predate those inline columns.
ADDITIVE_COLUMNS += [
    ("agent_runs", "node_id", "TEXT"),
    ("agent_runs", "backend_endpoint_id", "TEXT"),
    ("agent_sessions", "owning_node", "TEXT"),
    ("agent_sessions", "node_epoch", "INTEGER"),
]
# v8 H2.1 conversation-profile columns (all nullable; applied via the ADDITIVE path so ddl_sha256 / the
# manifest is UNCHANGED -- no SCHEMA_VERSION bump). agent_sessions carries the REQUESTED (owner-selected)
# model/effort + the UI permission_mode (plan|ask|agent|full -- distinct from agent_sessions.mode, which
# keeps orchestration semantics) + the immutable profile_hash. agent_runs carries the same per-run plus
# session_id (soft link to the C1 envelope, no cascade -- runs are audit evidence), engine, auth_mode, and
# reasoning_effort (the EFFECTIVE effort; agent_runs.model stays the EFFECTIVE model). Legacy rows with
# NULL H2 metadata stay readable everywhere.
ADDITIVE_COLUMNS += [
    ("agent_sessions", "requested_model", "TEXT"),
    ("agent_sessions", "requested_reasoning_effort", "TEXT"),
    ("agent_sessions", "permission_mode", "TEXT"),
    ("agent_sessions", "profile_hash", "TEXT"),
    # Harness UI v4 P0: the sole additive schema column for this milestone. Nullable keeps every
    # pre-v4/observed session readable; driven starts derive the canonical value before C1.
    ("agent_sessions", "title", "TEXT"),
    # Conversation continuation (owner ask 2026-07-10): the C1 stores its immutable policy snapshot
    # JSON so a daemon restart rehydrates the ORIGINAL captured inputs (never live rules); NULL on
    # legacy rows (simply not resumable). vendor_session_root_id (base column) carries the driven
    # vendor resume key, mirroring its observed-session use.
    ("agent_sessions", "policy_snapshot", "TEXT"),
    ("agent_runs", "session_id", "TEXT"),
    ("agent_runs", "engine", "TEXT"),
    ("agent_runs", "auth_mode", "TEXT"),
    ("agent_runs", "requested_model", "TEXT"),
    ("agent_runs", "requested_reasoning_effort", "TEXT"),
    ("agent_runs", "reasoning_effort", "TEXT"),
    ("agent_runs", "permission_mode", "TEXT"),
    ("agent_runs", "profile_hash", "TEXT"),
]
# Skill-context lifecycle dimensions remain schema-v1 additive metadata. Existing snapshot rows receive
# NULL and therefore fail closed in SK7/SK8 integrity checks until a freshly confirmed SK6 apply writes
# the finite values and recomputed content hashes.
ADDITIVE_COLUMNS += [
    (
        "skill_contexts",
        "publication_status",
        "TEXT CHECK (publication_status IN ('published', 'staged'))",
    ),
    ("skill_contexts", "publication_reason", "TEXT"),
    (
        "skill_context_surfaces",
        "policy_state",
        "TEXT CHECK (policy_state IN ('on', 'name-only', 'user-invocable-only', 'off'))",
    ),
]
# v8 H2.1 additive indexes: applied alongside ADDITIVE_COLUMNS at connect, deliberately NOT in the
# hashed INDEXES list (the FTS_INDEX_SQL precedent) -- folding them in would shift
# expected_manifest_hash for every existing DB and force K1 --restamp-manifest.
ADDITIVE_INDEX_SQL: list[str] = [
    # Linked-run lookup (session/list aggregates a C1 session's ordered T5 runs by session_id).
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_session_id ON agent_runs(session_id)",
]


# Test-record purge cascade (K7): delete children by their parent link first, then every flagged root.
# The schema ships no FK constraints, so order is logical-only, but children-first keeps the intent clear.
# Every row removed is either is_test=1 or a descendant of an is_test=1 root.
TEST_PURGE_SQL: list[str] = [
    # revision tables -> by their owning record's is_test
    "DELETE FROM gotcha_revision WHERE gotcha_id IN (SELECT id FROM gotcha WHERE is_test = 1)",
    "DELETE FROM learning_revision WHERE learning_id IN (SELECT id FROM learning WHERE is_test = 1)",
    "DELETE FROM learned_revision WHERE learned_id IN (SELECT id FROM learned WHERE is_test = 1)",
    "DELETE FROM task_revision WHERE task_id IN (SELECT id FROM tasks WHERE is_test = 1)",
    "DELETE FROM plan_revision WHERE plan_id IN (SELECT id FROM plans WHERE is_test = 1)",
    "DELETE FROM private_policy_revision WHERE policy_id IN (SELECT id FROM private_policy WHERE is_test = 1)",
    # evidence children -> by their document's is_test (chunk_embeddings FIRST: it maps chunk->document
    # through evidence_chunks, which the next statement deletes -- reverse the order and the mapping is gone).
    "DELETE FROM chunk_embeddings WHERE is_test = 1 "
    "OR chunk_id IN (SELECT id FROM evidence_chunks WHERE document_id IN "
    "(SELECT id FROM evidence_documents WHERE is_test = 1))",
    "DELETE FROM evidence_chunks WHERE document_id IN (SELECT id FROM evidence_documents WHERE is_test = 1)",
    "DELETE FROM evidence_blocks WHERE document_id IN (SELECT id FROM evidence_documents WHERE is_test = 1)",
    # eval_scores/eval_runs -> own flag OR any is_test parent link
    "DELETE FROM eval_scores WHERE is_test = 1 "
    "OR trace_event_id IN (SELECT id FROM trace_events WHERE is_test = 1) "
    "OR eval_run_id IN (SELECT id FROM eval_runs WHERE is_test = 1) "
    "OR verification_id IN (SELECT id FROM verification_events WHERE is_test = 1)",
    "DELETE FROM eval_runs WHERE is_test = 1 OR eval_case_id IN (SELECT id FROM eval_cases WHERE is_test = 1)",
    # roots
    "DELETE FROM gotcha WHERE is_test = 1",
    "DELETE FROM learning WHERE is_test = 1",
    "DELETE FROM learned WHERE is_test = 1",
    "DELETE FROM tasks WHERE is_test = 1",
    "DELETE FROM plans WHERE is_test = 1",
    "DELETE FROM ledger_events WHERE is_test = 1",
    "DELETE FROM verification_events WHERE is_test = 1",
    "DELETE FROM artifacts WHERE is_test = 1",
    "DELETE FROM eval_cases WHERE is_test = 1",
    "DELETE FROM source_locks WHERE is_test = 1",
    "DELETE FROM irl_reviews WHERE is_test = 1",
    "DELETE FROM subagent_packets WHERE is_test = 1",
    "DELETE FROM diagnostic_packets WHERE is_test = 1",
    "DELETE FROM anti_patterns WHERE is_test = 1",
    "DELETE FROM agentgateway_events WHERE is_test = 1",
    "DELETE FROM reports WHERE is_test = 1",
    "DELETE FROM private_policy WHERE is_test = 1",
    "DELETE FROM improvement_proposals WHERE is_test = 1",
    "DELETE FROM generative_run_routes WHERE is_test = 1 OR run_id IN (SELECT id FROM generative_runs WHERE is_test = 1)",
    "DELETE FROM generative_runs WHERE is_test = 1",
    "DELETE FROM evidence_documents WHERE is_test = 1",
    "DELETE FROM trace_events WHERE is_test = 1",
    "DELETE FROM pii_scan WHERE is_test = 1",
    # agent_events (child) first by its run's is_test, then the flagged runs.
    "DELETE FROM agent_events WHERE agent_run_id IN (SELECT id FROM agent_runs WHERE is_test = 1)",
    "DELETE FROM agent_runs WHERE is_test = 1",
    # v8 M2 session records: session-scoped children (own is_test OR parent session is_test) first,
    # then the flagged session/profile roots. mode_profiles is standalone (no session link).
    "DELETE FROM user_instructions WHERE is_test = 1 "
    "OR session_id IN (SELECT id FROM agent_sessions WHERE is_test = 1)",
    "DELETE FROM goals WHERE is_test = 1 "
    "OR session_id IN (SELECT id FROM agent_sessions WHERE is_test = 1)",
    "DELETE FROM approval_requests WHERE is_test = 1 "
    "OR session_id IN (SELECT id FROM agent_sessions WHERE is_test = 1)",
    "DELETE FROM agent_sessions WHERE is_test = 1",
    "DELETE FROM mode_profiles WHERE is_test = 1",
    # v8 M3 policy authority rules (standalone root; no session link).
    "DELETE FROM authority_rules WHERE is_test = 1",
    # v8 M13 backend registry (standalone config root; no children).
    "DELETE FROM backend_endpoints WHERE is_test = 1",
    # Skill context snapshot children first, then their flagged sync root.
    "DELETE FROM skill_context_surfaces WHERE context_id IN "
    "(SELECT id FROM skill_contexts WHERE sync_id IN (SELECT id FROM skill_context_syncs WHERE is_test = 1))",
    "DELETE FROM skill_context_events WHERE sync_id IN (SELECT id FROM skill_context_syncs WHERE is_test = 1)",
    "DELETE FROM skill_contexts WHERE sync_id IN (SELECT id FROM skill_context_syncs WHERE is_test = 1)",
    "DELETE FROM skill_context_syncs WHERE is_test = 1",
]
# Count query per root table so K7 can report what it removed (captured before the deletes run).
TEST_COUNT_SQL: dict[str, str] = {t: f"SELECT COUNT(*) FROM {t} WHERE is_test = 1" for t in TEST_ROOT_TABLES}
TEST_COUNT_SQL.update(
    {
        "chunk_embeddings": "SELECT COUNT(*) FROM chunk_embeddings WHERE is_test = 1",
        "generative_run_routes": "SELECT COUNT(*) FROM generative_run_routes WHERE is_test = 1",
    }
)


INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_gotcha_scope ON gotcha(scope)",
    "CREATE INDEX IF NOT EXISTS idx_learning_scope ON learning(scope)",
    "CREATE INDEX IF NOT EXISTS idx_learned_scope ON learned(scope)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_plans_task ON plans(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_task ON ledger_events(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_path ON artifacts(path)",
    "CREATE INDEX IF NOT EXISTS idx_eval_cases_category ON eval_cases(category)",
    "CREATE INDEX IF NOT EXISTS idx_sources_source_id ON source_locks(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_irl_review ON irl_reviews(review_id)",
    "CREATE INDEX IF NOT EXISTS idx_anti_patterns_scope ON anti_patterns(scope)",
    "CREATE INDEX IF NOT EXISTS idx_private_policy_scope ON private_policy(scope)",
    "CREATE INDEX IF NOT EXISTS idx_private_policy_trigger ON private_policy(trigger)",
    "CREATE INDEX IF NOT EXISTS idx_private_policy_status ON private_policy(status)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_documents_source ON evidence_documents(source_lock_id)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_blocks_document ON evidence_blocks(document_id)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_chunks_document ON evidence_chunks(document_id)",
    "CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_model ON chunk_embeddings(embedding_model)",
    "CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_chunk ON chunk_embeddings(chunk_id)",
    "CREATE INDEX IF NOT EXISTS idx_trace_events_task ON trace_events(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_trace_events_trace ON trace_events(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_eval_scores_trace ON eval_scores(trace_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_improvement_proposals_contract ON improvement_proposals(contract)",
    "CREATE INDEX IF NOT EXISTS idx_generative_runs_backend ON generative_runs(backend)",
    "CREATE INDEX IF NOT EXISTS idx_generative_runs_task ON generative_runs(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_gen_routes_pair ON generative_run_routes(ab_pair_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(agent_run_id)",
    # Idempotent replay: a repeated (run, source_event_id) is a no-op under T6's INSERT OR IGNORE.
    # Partial so locally-minted events (source_event_id NULL) never collide.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_events_source ON agent_events(agent_run_id, source_event_id) "
    "WHERE source_event_id IS NOT NULL",
    # v8 M2 session records (C5 timeline joins children by session_id).
    "CREATE INDEX IF NOT EXISTS idx_agent_sessions_task ON agent_sessions(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_user_instructions_session ON user_instructions(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_goals_session ON goals(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_approval_requests_session ON approval_requests(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_approval_requests_corr ON approval_requests(correlation_id)",
    "CREATE INDEX IF NOT EXISTS idx_mode_profiles_name ON mode_profiles(name)",
    # v8 M3 policy authority rules (load_rules() scans by verb).
    "CREATE INDEX IF NOT EXISTS idx_authority_rules_verb ON authority_rules(verb)",
    # v8 M13 backend registry (resolve_endpoint orders enabled rows by priority).
    "CREATE INDEX IF NOT EXISTS idx_backend_endpoints_priority ON backend_endpoints(enabled, priority)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_skill_context_sync_current ON skill_context_syncs(is_current) "
    "WHERE is_current = 1",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_skill_context_sync_name ON skill_contexts(sync_id, skill_name)",
    "CREATE INDEX IF NOT EXISTS idx_skill_contexts_sync ON skill_contexts(sync_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_skill_context_surface_host ON skill_context_surfaces(context_id, host)",
    "CREATE INDEX IF NOT EXISTS idx_skill_context_events_sync ON skill_context_events(sync_id)",
]


# --- v8 M9 fleet.db schema (SEPARATE synced coordination DB) ---------------------------------------
# FLEET_DDL/FLEET_INDEXES describe the DAEMON-OWNED fleet.db, applied by FleetStore -- NOT kaizen.db.
# They are DELIBERATELY not added to DDL, INDEXES, the schema_manifest tables list, or anything hashed
# by ddl_sha256: fleet.db has its own version stamp (fleet_schema, version 1) and is disposable
# (re-bootstrappable from the hub), so it must have ZERO impact on kaizen.db's manifest (a K1
# manifest_match invariant proves this). Tables per plan v8 §B.1/§4:
#   nodes/projects            -- fleet membership + project registry
#   coord_events              -- append-only coordination ledger (PK node-tagged; the reducer input)
#   remote_services/dispatches-- advertised endpoints + dispatched runs (M11+/M14 producers)
# leases are a REDUCER PROJECTION of coord_events (never a mutable mutex row) -- no leases table.
FLEET_SCHEMA_VERSION = 1
FLEET_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS fleet_schema (
        id TEXT PRIMARY KEY,
        fleet_schema_version INTEGER NOT NULL,
        applied_at TEXT NOT NULL
    )
    """,
    # One row per fleet node. tailnet_name is a MagicDNS name (never a raw IP/username); pubkey is
    # NULL until M11 (Ed25519 signing). capabilities_json carries has_gpu/model_endpoints/is_pinned.
    """
    CREATE TABLE IF NOT EXISTS nodes (
        node_id TEXT PRIMARY KEY,
        role TEXT,
        tailnet_name TEXT,
        os TEXT,
        arch TEXT,
        capabilities_json TEXT,
        pubkey TEXT,
        last_heartbeat TEXT,
        registered_at TEXT,
        updated_at TEXT
    )
    """,
    # Deterministic project registry (project_id derived per fleet.identity.project_id -> same id on
    # every mirror). hub_remote/sync_url point at the hub's two services (git bare + turso sync-server).
    """
    CREATE TABLE IF NOT EXISTS projects (
        project_id TEXT PRIMARY KEY,
        name TEXT,
        hub_remote TEXT,
        sync_url TEXT,
        registered_at TEXT
    )
    """,
    # Append-only coordination ledger -- the AUTHORITATIVE input the pure reducers fold (leases,
    # coordinator role). PK is node-tagged (fleet.identity.coord_id) so LWW sync only ever merges
    # DISTINCT rows. source_event_id is a nullable dedupe key (partial UNIQUE index below + INSERT OR
    # IGNORE => idempotent replay). sig is NULL until M11. content_hash covers the row core.
    """
    CREATE TABLE IF NOT EXISTS coord_events (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        node_id TEXT NOT NULL,
        project_id TEXT,
        event_kind TEXT NOT NULL,
        marker TEXT NOT NULL,
        scope_key TEXT,
        epoch INTEGER,
        payload_json TEXT,
        source_event_id TEXT,
        content_hash TEXT,
        sig TEXT
    )
    """,
    # Advertised model endpoints / compute / workspace workers (M11+/M13 producers).
    """
    CREATE TABLE IF NOT EXISTS remote_services (
        id TEXT PRIMARY KEY,
        node_id TEXT,
        kind TEXT,
        base_url TEXT,
        lanes_json TEXT,
        registered_at TEXT,
        updated_at TEXT
    )
    """,
    # Dispatched runs (target node, required leases, status) -- M14 remote-dispatch producer.
    """
    CREATE TABLE IF NOT EXISTS remote_dispatches (
        id TEXT PRIMARY KEY,
        created_at TEXT,
        target_node_id TEXT,
        required_leases_json TEXT,
        status TEXT,
        payload_json TEXT
    )
    """,
]
FLEET_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_coord_events_created ON coord_events(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_coord_events_kind ON coord_events(event_kind)",
    "CREATE INDEX IF NOT EXISTS idx_coord_events_node ON coord_events(node_id)",
    # Idempotent replay: a repeated source_event_id is a no-op under INSERT OR IGNORE. Partial so
    # locally-minted events (source_event_id NULL) never collide.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_coord_events_source ON coord_events(source_event_id) "
    "WHERE source_event_id IS NOT NULL",
]


def ddl_sha256() -> str:
    """Hash of the actual DDL + index text, so manifest comparisons catch edits that
    change table shapes without a SCHEMA_VERSION/MIGRATION_ID bump.

    Deliberately covers ONLY kaizen.db's DDL/INDEXES -- fleet.db's FLEET_DDL/FLEET_INDEXES are a
    separate, disposable, hub-syncable schema and must NOT shift kaizen.db's manifest hash."""
    text = "\n".join(stmt.strip() for stmt in (*DDL, *INDEXES))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Opt-in experimental Turso FTS index. Deliberately NOT part of DDL/INDEXES (and so not in
# ddl_sha256): it is created only when KAIZEN_TURSO_FTS=1, and folding it into the hashed
# manifest would shift expected_manifest_hash for every existing DB. Applied during K1.
FTS_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_evidence_chunks_fts ON evidence_chunks USING fts (text)"


# Referential-integrity map for the read-only K1 --integrity scan (the schema ships no FK
# constraints, so this is application-level). Each entry is child (table, column) -> parent
# (table, column); a non-NULL child value with no matching parent row is an orphan. Kept
# CONSERVATIVE on purpose: only clear structural-ownership and promotion-lineage id links.
# Deliberately EXCLUDED as soft/annotational references: the optional *task_id* columns
# (ledger/gotcha/learning/tasks annotations that may point across sessions), irl_reviews.review_id
# (a grouping key, not a row id), verification proof_id, and the self-referential neighbor/parent
# links (which can transiently dangle mid-write). This is a plain dict, NOT hashed DDL.
REFERENCES: list[tuple[str, str, str, str]] = [
    ("gotcha_revision", "gotcha_id", "gotcha", "id"),
    ("learning_revision", "learning_id", "learning", "id"),
    ("learned_revision", "learned_id", "learned", "id"),
    ("task_revision", "task_id", "tasks", "id"),
    ("plan_revision", "plan_id", "plans", "id"),
    ("private_policy_revision", "policy_id", "private_policy", "id"),
    ("learning", "source_gotcha_id", "gotcha", "id"),
    ("learned", "source_learning_id", "learning", "id"),
    ("evidence_documents", "source_lock_id", "source_locks", "id"),
    ("evidence_blocks", "document_id", "evidence_documents", "id"),
    ("evidence_chunks", "document_id", "evidence_documents", "id"),
    ("evidence_chunks", "source_lock_id", "source_locks", "id"),
    ("chunk_embeddings", "chunk_id", "evidence_chunks", "id"),
    ("eval_runs", "eval_case_id", "eval_cases", "id"),
    ("eval_scores", "trace_event_id", "trace_events", "id"),
    ("eval_scores", "eval_run_id", "eval_runs", "id"),
    ("eval_scores", "verification_id", "verification_events", "id"),
    ("generative_runs", "workflow_artifact_id", "artifacts", "id"),
    ("generative_run_routes", "run_id", "generative_runs", "id"),
    ("agent_events", "agent_run_id", "agent_runs", "id"),
    # v8 M2 session records: session-scoped children -> their session.
    ("user_instructions", "session_id", "agent_sessions", "id"),
    ("goals", "session_id", "agent_sessions", "id"),
    ("approval_requests", "session_id", "agent_sessions", "id"),
    ("skill_contexts", "sync_id", "skill_context_syncs", "id"),
    ("skill_context_surfaces", "context_id", "skill_contexts", "id"),
    ("skill_context_events", "sync_id", "skill_context_syncs", "id"),
]


def schema_manifest() -> dict[str, object]:
    """Returns the version/migration/tool/ddl-hash + ordered kaizen.db `tables` list; ordering & membership are load-bearing (hashed into expected_manifest_hash) so any table add here shifts the manifest and requires K1 --restamp-manifest."""
    return {
        "schema_version": SCHEMA_VERSION,
        "migration_id": MIGRATION_ID,
        "tool_version": TOOL_VERSION,
        "ddl_sha256": ddl_sha256(),
        "tables": [
            "schema_version",
            "gotcha",
            "gotcha_revision",
            "learning",
            "learning_revision",
            "learned",
            "learned_revision",
            "tasks",
            "task_revision",
            "plans",
            "plan_revision",
            "ledger_events",
            "verification_events",
            "artifacts",
            "eval_cases",
            "eval_runs",
            "source_locks",
            "irl_reviews",
            "subagent_packets",
            "diagnostic_packets",
            "anti_patterns",
            "agentgateway_events",
            "reports",
            "private_policy",
            "private_policy_revision",
            "evidence_documents",
            "evidence_blocks",
            "evidence_chunks",
            "chunk_embeddings",
            "db_settings",
            "trace_events",
            "eval_scores",
            "improvement_proposals",
            "generative_runs",
            "generative_run_routes",
            "pii_scan",
            "agent_runs",
            "agent_events",
            "agent_sessions",
            "user_instructions",
            "goals",
            "approval_requests",
            "mode_profiles",
            "authority_rules",
            "backend_endpoints",
            "skill_context_syncs",
            "skill_contexts",
            "skill_context_surfaces",
            "skill_context_events",
        ],
    }
