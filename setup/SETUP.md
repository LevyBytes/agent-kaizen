# SETUP.md - Agent Operating Manual For Agent Kaizen

This is the agent-facing operating manual for this repo - the agent counterpart to the human-facing
[`README.md`](../README.md). It lives in `setup/`, alongside the bootstrap and helper scripts. The public
system concept lives in [`Kaizen_System.md`](../Kaizen_System.md). `AGENTS.md` and `CLAUDE.md` (repo root) are
the compact host instructions that point here.

## 1. What This Repo Is

Agent Kaizen is a local implementation of the Kaizen System for improving AI coding-agent work in VS Code
projects.

The system loop is:

```text
SAVMI = Scope -> Adapt -> Verify -> Manage -> Improve
```

The local harness is:

- `kaizen.py`;
- the local DB under `AI/db/`;
- project and skill `evals/` surfaces;
- structured reports, proof records, hashes, source locks, packets, and policy context.

## 2. First-Time Setup

The one-file installers and their helper scripts all live in this `setup/` folder:

- Windows: `setup/Install-Agent-Kaizen.cmd` - installs git + Python via winget, sets `DEVROOT`,
  clones the repo into `DEVROOT\agent-kaizen`, builds the shared venv, generates the VS Code workspace +
  launcher, scaffolds an empty sibling `SKILLS` store, and initializes the DB.
- Linux/macOS: `setup/install-agent-kaizen.sh` - installs git + Python via the system package
  manager, clones the repo, then runs `setup/setup.sh` (which also runs standalone in an already-cloned repo).
- Skills are optional and ship empty: `setup/link-skills.ps1` / `setup/link-skills.sh` clone a skills store you
  choose and link it into `.agents/skills` + `.claude/skills`.
- `setup/SetDevRoot.cmd` sets only the `DEVROOT` user environment variable.

Human-facing install steps are in the repo [`README.md`](../README.md).

## 3. Session Start

For non-trivial work, run or request the current private policy context first - the same command for Claude Code
and Codex:

```powershell
python kaizen.py X5 --json
```

The policy DB ships empty; it returns only the rules you have added with `X1`. Reload this context after
conversation compaction. Before every major task, remind the user to compact or start a continuation when the
context window is getting heavy.

Then check the DB and load the session digest:

```powershell
python kaizen.py K1 --json
python kaizen.py R0 --json
```

`R0` is the read-back half of Manage: one small JSON payload with active policy, open GOTCHAs,
blocking verification conclusions (`VERIFICATION_FAILED`, `NEEDS_HUMAN_DECISION`), recent LEARNED
lessons, and active tasks — so a session starts from records instead of chat memory. Reload `X5`
and `R0` after compaction.

## 4. Core Rule

```text
Scripts control structure and repetitive work.
Agents control meaning, synthesis, and adaptation.
Verification controls acceptance.
Management controls durable memory and policy.
The user controls priorities and approvals.
```

Move deterministic mechanics into scripts. Keep agent context for judgment, tradeoffs, and synthesis.

## 5. Kaizen CLI

Use:

```powershell
python kaizen.py <operation> --json
```

The shared venv is `$DEVROOT/Python/venvs/kaizen` (created by the setup script); a repo-local `.venv` also
works as a fallback. Dependency pins are in `requirements-kaizen.txt`.

Command families:

- `K*` DB/core
- `W*` work/tasks/plans/packets
- `G*` GOTCHA
- `L*` LEARNING/LEARNED
- `Q*` quality/evals/verification/proof (incl. `Q8` output-validate against record schemas)
- `M*` migration
- `R*` reports
- `S*` sources
- `I*` IRL Review
- `A*` artifacts
- `X*` private policy/session context
- `T*` traces/scores
- `E*` evidence ingestion
- `O*` improvement lab
- `Y*` generative runs (ComfyUI)
- `B*` model/embedding backends (Ollama)

Use `--help` for the approved operation list. Short codes and named aliases are equivalent. Do not invent
operation codes or flags during task work.

JSON-valued args and shell quoting differ per shell. The file fallback always works; inline forms
vary:

| Shell                  | Inline JSON that survives argv                          | Safest form   |
| ---------------------- | ------------------------------------------------------- | ------------- |
| Windows PowerShell 5.1 | `--payload-json "{\"task\":\"summarize\"}"` (escaped)   | `*-file` flag |
| PowerShell 7+          | `--payload-json '{"task":"summarize"}'` (single-quoted) | either        |
| bash / zsh             | `--payload-json '{"task":"summarize"}'` (single-quoted) | either        |
| cmd.exe                | `--payload-json "{\"task\":\"summarize\"}"` (escaped)   | `*-file` flag |

Every JSON flag has a file twin for when quoting fights back: `--payload-json-file`,
`--summary-file`, `--body-file`, `--evidence-file`, `--findings-file`, `--remedies-file`,
`--artifact-ids-file`, `--expected-json-file`.

## 6. Work Locations

- Durable human-facing deliverables: `user/`.
- Generated content: `AI/generation/`.
- Task scratch: `AI/work/<task-slug>/`.
- Kaizen DB and exports: `AI/db/`.
- Setup/bootstrap scripts: `setup/`.
- Auxiliary helper scripts: `support_scripts/`.
- Helper scripts scratch: `AI/support_scripts_work/`.
- Root eval fixtures and learning command stubs: `evals/`.

For public repositories, keep `AI/db/` contents private/local unless explicitly sanitized. Private repositories
may choose to track DB data deliberately.

`AI/work/build-ledger.md` remains a local continuity export during the transition. Update it after major
milestones until DB-backed ledger reporting fully replaces it.

## 7. SAVMI Workflow

For substantial work use [`Kaizen_System.md`](../Kaizen_System.md):

1. **Scope**: research the system, interview the user when needed, define assumptions, scope, and acceptance
   criteria.
2. **Adapt**: create the execution contract, use allowed capabilities, and make bounded changes.
3. **Verify**: run deterministic checks first; use structured synthesis only where judgment is needed.
4. **Manage**: record tasks, plans, proof, artifacts, source locks, learning records, ingested evidence, activity traces, eval scores, and reports.
5. **Improve**: review managed evidence and feed better priorities into the next Scope cycle.

Use `W1`/`W3` for tasks and plans when work is substantial. Use `Q*` and `A*` for proof, verifier findings, eval
runs, anti-patterns, and artifacts. Use `G*` and `L*` for the GOTCHA -> LEARNING -> LEARNED lifecycle. Use `E*`
to ingest external evidence, `T*` to record traces and eval scores, and `O*` for the improvement lab.

Read records back, not just in: `R0` (session digest), `L10` (LEARNED lessons with their
GOTCHA -> LEARNING chain), `Q9` (verification conclusions by task/conclusion/severity), and `T4`
(eval scores with aggregates) pull past work into the current session cheaply.

### Durability triage

Not every observation deserves a durable record. Default rubric:

| Situation                                                  | Action                    |
| ---------------------------------------------------------- | ------------------------- |
| Changes scope, risk, acceptance criteria, or a decision    | `W1`/`W2` (task + ledger) |
| A failure, pitfall, or suspected issue with evidence       | `G1`                      |
| A validated root cause + correction for an existing GOTCHA | `L2` (promote)            |
| Proof that work passed or failed a check                   | `Q1`/`Q2`                 |
| Minor mechanics, typos, or chat-level back-and-forth       | chat only — no record     |

### Evidence Ingestion Walkthrough

Managed ingestion instead of pasting raw text into context:

```powershell
python kaizen.py E1 --path docs/spec.md --summary "Ingest the widget spec." --json
python kaizen.py E3 --id DOC_ID_FROM_E1 --json
python kaizen.py E4 --query "retry budget" --json
python kaizen.py E5 --id DOC_ID_FROM_E1 --json
```

`E3` chunks deterministically (add `--chunker semantic` with an embedding backend); `E4` is
lexical by default and vector-ranked with `--semantic` once chunks are embedded (`B3` backfills).
Lock external sources with `S1` first when provenance matters.

### Private Policy Walkthrough

The policy DB ships empty by design. A realistic first rule and its round-trip:

```powershell
python kaizen.py X1 --title "User-owned commits" --trigger session-start --priority high --summary "Agents prepare changes; the user commits." --body "Do not commit, push, or rewrite history unless explicitly authorized for that exact action." --json
python kaizen.py X5 --json
```

`X5` (and `R0`) return active rules sorted by priority; `--trigger` labels when a rule applies
(`session-start` rules always load).

## 8. Skills

Skills live in the external skills store and are surfaced through `.agents/skills` and `.claude/skills`. The
public repo ships with no skills; add your own store with `setup/link-skills.ps1` / `setup/link-skills.sh`. Use
the host skill loader when a task matches a skill trigger. Edit the canonical skill store, not a copied mirror.

Skills and scripts can support all SAVMI layers:

- Scope research and routing;
- Adapt execution and deterministic mechanics;
- Verify proof and review;
- Manage records and generated views;
- Improve evals, anti-patterns, and correction patterns.

Every serious skill should have:

- `SKILL.md`;
- `evals/GOTCHA.md`;
- `evals/LEARNING.md`;
- `evals/LEARNED.md`;
- `evals/` fixtures for executable or behavioral evals when needed.

The Markdown learning files are stubs or generated views. Durable learning records live in the DB.

## 9. Verification

Before reporting success:

- re-read the user request and the active Scope;
- check each acceptance criterion;
- run relevant tests, formatters, linters, validators, or smoke commands;
- record proof artifacts and hashes where practical;
- say what passed, failed, and was not run.

Known caveat: running a repo-wide formatter can hit unrelated local scratch. Prefer targeted checks on the
Markdown you changed.

## 10. Public Surfaces

Tracked public docs should explain the portable system and local harness, not private machine policy. Private
policy context belongs in the local data plane and is loaded through `X5`.

Before preparing public output, inspect:

- tracked files;
- ignored/generated files intended for export;
- local DB exports and reports;
- personal paths;
- secret-like strings.
