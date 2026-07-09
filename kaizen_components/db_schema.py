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
)
ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [(t, "is_test", _IS_TEST) for t in TEST_ROOT_TABLES]


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
]
# Count query per root table so K7 can report what it removed (captured before the deletes run).
TEST_COUNT_SQL: dict[str, str] = {t: f"SELECT COUNT(*) FROM {t} WHERE is_test = 1" for t in TEST_ROOT_TABLES}


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
]


def ddl_sha256() -> str:
    """Hash of the actual DDL + index text, so manifest comparisons catch edits that
    change table shapes without a SCHEMA_VERSION/MIGRATION_ID bump."""
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
    ("eval_runs", "eval_case_id", "eval_cases", "id"),
    ("eval_scores", "trace_event_id", "trace_events", "id"),
    ("eval_scores", "eval_run_id", "eval_runs", "id"),
    ("eval_scores", "verification_id", "verification_events", "id"),
    ("generative_runs", "workflow_artifact_id", "artifacts", "id"),
    ("generative_run_routes", "run_id", "generative_runs", "id"),
    ("agent_events", "agent_run_id", "agent_runs", "id"),
]


def schema_manifest() -> dict[str, object]:
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
        ],
    }
