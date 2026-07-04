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
        content_hash TEXT NOT NULL
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
        embedding BLOB,
        embedding_model TEXT,
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
        content_hash TEXT NOT NULL
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
]


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
    "CREATE INDEX IF NOT EXISTS idx_trace_events_task ON trace_events(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_trace_events_trace ON trace_events(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_eval_scores_trace ON eval_scores(trace_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_improvement_proposals_contract ON improvement_proposals(contract)",
    "CREATE INDEX IF NOT EXISTS idx_generative_runs_backend ON generative_runs(backend)",
    "CREATE INDEX IF NOT EXISTS idx_generative_runs_task ON generative_runs(task_id)",
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


# Referential-integrity map for the read-only K1 --integrity scan (SCHEMA v1 ships no FK
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
            "trace_events",
            "eval_scores",
            "improvement_proposals",
            "generative_runs",
        ],
    }
