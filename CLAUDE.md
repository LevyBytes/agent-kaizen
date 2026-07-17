# CLAUDE.md

`CLAUDE.md` and `AGENTS.md` are intentionally identical below this line (one set of harness rules for every agent host); edit both together.

## 1. Workspace

Agent Kaizen is a local implementation of the Kaizen System for AI coding-agent work in VS Code projects.

**Run everything through the shared Python venv, not a repo-local `.venv`.** The interpreter is `$DEVROOT/Python/venvs/kaizen/Scripts/python.exe` (Windows) or `$DEVROOT/Python/venvs/kaizen/bin/python` (Linux/macOS), built by `setup/SETUP.md` from `requirements-kaizen.txt`. Activate it (or call it by full path) before any `python kaizen.py ...` or `python tests/run_tests.py` in this file; the test runner pins all scratch and bytecode beneath `AI/work`, and a repo-local `.venv` is only a fallback and usually absent.

Use [`Kaizen_System.md`](Kaizen_System.md) for the full portable method:

```text
SAVMI = Scope -> Adapt -> Verify -> Manage -> Improve
```

Use `setup/SETUP.md` when broader setup, workflow diagnosis, or harness behavior is needed.

## 2. First Action

For non-trivial work, load private policy context and the session digest (active GOTCHAs, blocking verifications, recent LEARNED, active tasks) at session start and after compaction:

```powershell
python kaizen.py X5 --json
python kaizen.py R0 --json
```

Then check the DB when work will create records:

```powershell
python kaizen.py K1 --json
```

Before major tasks, remind the user to compact context or start a continuation when useful.

## 3. Kaizen Harness

Structured work records are written through:

```powershell
python kaizen.py <operation> --json
```

Common command families:

- `K*` DB/core; `W*` tasks/plans/packets; `G*` GOTCHA; `L*` LEARNING/LEARNED.
- `Q*` quality/evals/verification/proof (incl. `Q8` output-validate against record schemas); `A*` artifacts; `M*` migration.
- `R*` reports; `S*` source locks; `I*` IRL Review; `X*` private policy context.
- `E*` evidence ingestion (ingest-file/chunk/query/inspect); `T*` traces and eval scores; `O*` improvement lab; `Y*` generative runs (ComfyUI); `B*` model/embedding backends (Ollama, local PyTorch, embedding, reranking, and PII).

Run `python kaizen.py --help` for approved operations, or `python kaizen.py K0 --query "<intent>"` to find the right operation from intent. Do not invent operation codes or flags.

- Windows PowerShell 5.1 strips quotes inside JSON-valued args: prefer `--payload-json-file`, `--summary-file`, or `--body-file`; inline escaping (`--payload-json "{\"k\":\"v\"}"`) is a fragile last resort.

Markdown files such as `evals/GOTCHA.md`, `evals/LEARNING.md`, and `evals/LEARNED.md` are command stubs or generated views. Durable records live in the DB.

## 4. Map

- `Kaizen_System.md` - portable system document.
- `README.md` - detailed human-facing manual.
- `setup/SETUP.md` - agent operating manual.
- `setup/` - install/bootstrap scripts (installer, `SetDevRoot.cmd`, `link-skills`).
- `kaizen.py` - data-plane CLI.
- `kaizen_components/` - engine package behind the CLI.
- `extension/` - VS Code sidebar/popout controller over the local supervisor daemon.
- `requirements-kaizen.txt` - pinned Python deps.
- `tests/` - canonical test, benchmark, verification, and acceptance sources.
- `support_scripts/` - auxiliary helper scripts.
- `AI/db/` - local/private DB, manifests, exports, reports.
- `AI/work/` - task scratch and transition ledger.
- `AI/generation/` - generated draft plans.
- `evals/` - project eval fixtures and learning command stubs.
- `.claude/skills/` and `.agents/skills/` - junctions to the external skills store.

## 5. Skills

Use skills when the task matches the trigger.

- PowerShell/native Windows commands -> `powershell-vsdevshell`.
- Git -> `git`.
- GitHub -> `github`.
- CLI UX/argparse/help/output/errors -> `cli-design`.
- Skill creation/review/validation -> `skill-drafting`.
- Chrome extensions -> `chrome-extensions`.
- Blender -> `blender`.
- GIMP -> `gimp`.
- Lumberyard/CryEngine-family -> `lumberyard`.
- Turso/SQLite-compatible DB work -> `turso-db`.

## 6. Working Pattern

- Scope before Adapt: inspect, research, and clarify acceptance criteria.
- Keep changes bounded to the active request.
- Prefer deterministic scripts for repetitive mechanics.
- Verify with ground truth before synthesis-only review.
- Installer/tooling steps must be idempotent: pre-flight validate (detect already-present, valid results) and skip their download/install work on a warm re-run; do work only when validation fails, then re-validate. See `setup/SETUP.md`.
- Markdown/docs use Prettier settings `proseWrap: never` / `printWidth: 100` (config kept local, not shipped) — one clean line per paragraph/bullet, never hard-wrapped at a column; optionally `npx prettier --check <files>` if prettier is installed — a convenience, not a required gate.
- Agent contracts (plans, task specs, subagent instructions, durable DB records, generated reports) are terse, technical, and signal-dense: no colloquialisms, filler, or restatement; maximize signal per token. Human-authored docs and commit/PR text are out of scope. Lint with `Q10 contract-lint`.
- Record meaningful proof, artifacts, decisions, and learning records through the harness.
- Update `AI/work/build-ledger.md` after major milestones until DB ledger reporting fully replaces it.
- Keep private policy details out of public tracked docs; load them through `X5`.

If a non-trivial choice is not already locked by the user or active plan, present:

```text
Decision | Options | Recommendation | Confirmation
```

Before success, report what passed, failed, and was not run.
