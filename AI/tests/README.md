# AI/tests/

Automated tests for the Agent Kaizen harness — 243 tests across 27 modules. Standard-library
`unittest` only — no extra dependencies beyond the pinned runtime in `requirements-kaizen.txt`.
(A handful of PDF-guard tests skip unless the opt-in `requirements-docs.txt` backends are installed.)

## Isolation (the real DB is never touched)

Every test that exercises the database runs `kaizen.py` as a subprocess with
`KAIZEN_REPO_ROOT` pointed at a fresh temporary directory. `paths.py` anchors the whole data
plane (`kaizen.db`, exports, manifests) on that root, so the tests read and write only throwaway
temp databases and never the project's real `AI/db`. Temp roots are deleted on teardown.

Tests invoke `sys.executable`, so run them with the project venv's Python (the one that has
`pyturso` installed).

## Run

```powershell
# Windows
.\.venv\Scripts\python.exe -m unittest discover -s AI/tests
```

```sh
# Linux / macOS
./.venv/bin/python -m unittest discover -s AI/tests
```

## What is covered

- `test_op_coverage.py` — the conformance matrix: AST-scans the whole suite and **fails if any
  CLI operation is not exercised by at least one test** (`ALLOWED_UNTESTED` is empty by design).
- `test_doc_examples.py` — executes every `kaizen.py` command example in `README.md`,
  `setup/SETUP.md`, and `evals/README.md` against a scratch database (placeholder IDs threaded
  between commands), and asserts `CLAUDE.md`/`AGENTS.md` stay byte-identical below their titles.
- `test_bench_smoke.py` — `support_scripts/bench_kaizen.py --quick` runs green into a temp
  output dir (valid results JSON, perfect retrieval hit rate, semantic skipped, no personal
  paths) and never rewrites the repo's own `README.md`/`docs/BENCHMARKS.md`.
- `test_schema.py` — `K1` is idempotent, `K2` reports a healthy schema, `K3`/`K6` run, plus the
  manifest-drift write gate (fail-closed + `K1 --restamp-manifest` reconciliation) and the
  `K1 --integrity` cross-table orphan scan (clean DB and a seeded orphan).
- `test_input_hardening.py` — the unified repo-only path policy (`E1`/`A2` deny outside-repo paths;
  `--allow-external` stores a sanitized `external:` origin), the UTF-8 decode denial on `--*-file`
  inputs, LIKE wildcard escaping in lexical search, and the PDF ingestion guards
  (size/pages/encrypted/no-text; pypdf-dependent cases skip when the docs backend is absent).
- `test_db_retry.py` — retry-vocabulary/backoff unit tests plus a 4-process concurrent `K1` race
  regression against one shared data plane.
- `test_learning_ops.py` — the full L\* lifecycle: adds, `L2`/`L3` promotion linkage
  (`source_gotcha_id`/`source_learning_id`), promoted-status transitions, `G5` updates, `L10`
  lineage narratives, denials.
- `test_policy_ops.py` — `X1`–`X5` round-trips, priority-over-recency ordering (an old critical
  rule survives the LIMIT window), trigger filtering, retired-rule exclusion.
- `test_quality_ops.py` — `Q1`–`Q7`, `Q9` filters, artifact ops, `*-file` JSON fallbacks, `Q7`
  routing to eval runs/cases.
- `test_report_ops.py` — `R0` session digest (sections, counts, drift signals, limits), `R1`–`R6`
  report files, `R4` severity/actionability filters and enriched columns, `R9`/`R10` windows.
- `test_plan_packet_ops.py` — `W2`–`W8`: plan revisions, packet round-trips, object-only payload
  enforcement, revision counting via `K6`.
- `test_migration_ops.py` — `M1`–`M5`: allowlists, dry-run purity (hash-snapshot), stub content,
  backups + manifest, verify/report.
- `test_irl_ops.py` — `I1`–`I5`: review/prediction/correction/outcome lifecycle and report files.
- `test_lab_misc_ops.py` — `O1`–`O3` improvement lab, `S4` export, `E5` inspect, and the newer
  redaction pattern classes.
- `test_integration_chains.py` — end-to-end chains across families (GOTCHA→LEARNED→report,
  task→artifact→verification→digest, trace→score→query, artifact path containment).
- `test_cli_wiring.py` — `--help`/`--version`, unknown/missing-op denials, ALIASES ↔ README
  command-table parity, and that every operation reaches a real dispatch branch (OK or a
  structured `DENIED`, never `ERROR_UNEXPECTED`).
- `test_records.py` — create/inspect/query round-trips (GOTCHA, source locks, artifacts, traces),
  structured denials on missing required fields, and a clean `DENIED_FILE_NOT_FOUND` on a missing `--*-file`.
- `test_reports.py` — `R7 --query` regression (no missing-`body` column error), `R8`–`R10`
  time-windowed ledger reports (incl. a recent-event inclusion check), and `R11` topic requiring `--query`.
- `test_redaction.py` — the secret/personal-path/email scanner, plus the trace-write gate denying
  secrets in `environment`/`tags`.
- `test_output_validate.py` — `Q8` lists schemas, accepts valid payloads, rejects invalid ones.
- `test_aux.py` — the prompt-builder imports cleanly with its external-checker read guarded.
- `test_vectors.py` — Turso-native vector storage + cosine-distance nearest-neighbour search (the
  engine claim), exercised in a throwaway temp database.
- `test_comfyui.py` — the ComfyUI generative-run ops (`Y*`): `Y1 --dry-run` records a `queued` run
  with a deterministic workflow hash + extracted seed/models (no network), `Y2`/`Y3` inspect/list,
  and graceful denials (`DENIED_BACKEND_UNAVAILABLE` with no server, missing `--path`, bad workflow type).
- `test_backends.py` — the model-backend ops (`B*`) + embedding seam, with no live server: `B1`
  reports unconfigured/unreachable backends, `B2`/`B3`/`E4 --semantic` deny cleanly when unconfigured,
  and `E1`→`E3`→`E4` still chunk + lexically search with no embeddings (graceful degradation). Also
  unit-tests the HTTP retry classifier (transient vs permanent, `Retry-After` honoring) and the
  bounded `embed_batched` helper (ordered batches, count-mismatch denial).
- `test_pytorch.py` — the sentence-transformers selection + `semantic` chunker without the heavy
  extra installed: the in-process embedder is recognized, the absent extra denies cleanly, `neural`
  is reserved, and `semantic` chunking requires a configured backend (absent-extra tests skip if installed).
- `test_comfyui_live.py` — the ComfyUI **live** path against an in-process mock server: `Y5`
  reachable, `Y1` submit→wait→fetch→save→register→`completed` (output saved + hashed), and `Y4` replay.
- `test_backends_live.py` — the model-backend **live** path against an in-process mock OpenAI server:
  `B1` reachable + dimension, `E3` stores embeddings, the `semantic` chunker splits by similarity,
  `E4 --semantic` ranks by cosine, `B2` records a model_call trace, `B3` backfills embeddings.
