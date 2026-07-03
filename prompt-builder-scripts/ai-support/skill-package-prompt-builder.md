# skill-package-prompt-builder.py

`skill-package-prompt-builder.py` is an interactive prompt builder for creating or updating high-quality agent skill packages. It interviews you, records your decisions, verifies that the generated prompt includes the required safety and quality clauses, then prints a paste-ready prompt for Codex or Claude.

The script does not call an LLM, fetch web pages, inspect remote sources, or build the skill package itself. It prepares the prompt that a VS Code agent will execute later.

## Quick Start

Run from the repository root:

```powershell
python .\prompt-builder-scripts\ai-support\skill-package-prompt-builder.py
```

Use Python 3.10 or newer. The script is standard-library-only by default. If the optional LLM assist loop is enabled, it can use `json-repair` to parse imperfect pasted JSON replies more reliably:

```powershell
pip install json-repair
```

The script tries to copy the generated prompt to the clipboard. It always prints the generated prompt between clear terminal markers, so manual copy still works if clipboard access fails.

## Flag-Driven CLI

Running the script with no arguments still starts the interactive interview. The argparse surface adds explicit help, version, management commands, and noninteractive generation:

```powershell
python .\prompt-builder-scripts\ai-support\skill-package-prompt-builder.py --help
python .\prompt-builder-scripts\ai-support\skill-package-prompt-builder.py --version
python .\prompt-builder-scripts\ai-support\skill-package-prompt-builder.py run
```

Use `generate` when you want the prompt built from flags without prompts:

```powershell
python .\prompt-builder-scripts\ai-support\skill-package-prompt-builder.py generate `
  --skill-name "Demo Skill" `
  --skill-slug demo-skill `
  --purpose "give an agent focused guidance for demo workflows" `
  --triggers "when the user asks about demo workflows" `
  --source "label=Demo docs;type=url;location=https://example.com/docs;priority=high;trust=verified;use=workflow rules" `
  --source-license unknown `
  --risk high `
  --no-clipboard
```

`generate` starts from the selected session's latest draft or run when `--session` is supplied. Without `--session`, it starts from built-in defaults. CLI flags override those defaults for that run. Generated runs are logged by default; use `--no-log` for a stateless one-off.

Common `generate` flags:

- `--session`: session root or exact session name to use as defaults.
- `--session-name`: name for a new CLI-created session.
- `--skill-name`, `--skill-slug`, `--purpose`, `--target-agents`.
- `--skill-category`: primary category from the 9-category skill taxonomy.
- `--triggers`, `--example-tasks`, `--negative-triggers`.
- `--workflow`, `--files`, `--doc-depth`, `--verification`.
- `--known-gotchas`, `--quality-rubric`, `--observation-method`.
- `--privacy`, `--out-of-scope`, `--risk`.
- `--source-license`, `--build-intent`, `--parallelism`.
- `--memory-file-policy`: `assess-then-ask`, `read-only`, or `edit-with-explicit-approval`.
- `--subagent-review-policy`: `nontrivial`, `high-risk-only`, `optional`, or `none`.
- `--sources-file`: JSON source registry file.
- `--source`: repeatable semicolon-separated source spec.
- `--raw`: print only the prompt body, without terminal markers.
- `--output`: also write the emitted text to a file.
- `--no-clipboard`: disable clipboard copy for this run.
- `--backup-mode`: one-run backup behavior.
- `--no-log`: do not append the run to the sibling log.

Source specs use key/value pairs:

```powershell
--source "label=Docs;type=official-docs;location=https://example.com;priority=high;trust=official;use=format rules"
```

Use `--sources-file` when a source value needs semicolons or when you want a reviewable registry:

```json
{
  "sources": [
    {
      "label": "Claude skills documentation",
      "stype": "official-docs",
      "location": "https://code.claude.com/docs/en/skills",
      "priority": "high",
      "trust": "official",
      "intended_use": "skill format and authoring rules"
    }
  ]
}
```

If `--sources-file` or `--source` is supplied, that source registry replaces session/default sources for the run. If neither is supplied, `generate` uses session sources when `--session` is supplied, otherwise the built-in seed sources.

Management commands are noninteractive:

```powershell
python .\prompt-builder-scripts\ai-support\skill-package-prompt-builder.py settings show
python .\prompt-builder-scripts\ai-support\skill-package-prompt-builder.py settings set assist_mode off
python .\prompt-builder-scripts\ai-support\skill-package-prompt-builder.py settings reset
python .\prompt-builder-scripts\ai-support\skill-package-prompt-builder.py sessions list
python .\prompt-builder-scripts\ai-support\skill-package-prompt-builder.py sessions show abc12345
python .\prompt-builder-scripts\ai-support\skill-package-prompt-builder.py sources schema
python .\prompt-builder-scripts\ai-support\skill-package-prompt-builder.py sources validate --sources-file .\AI\work\sources.json
python .\prompt-builder-scripts\ai-support\skill-package-prompt-builder.py sources list
python .\prompt-builder-scripts\ai-support\skill-package-prompt-builder.py last --raw --no-clipboard
```

## What It Produces

The output is a single prompt for a receiving Codex or Claude agent. That prompt tells the receiving agent to create a complete skill package under `.agents/skills/{skill-slug}` and, when Claude support is requested, to maintain the matching `.claude/skills/{skill-slug}` copy.

The receiving agent is instructed to create:

- `SKILL.md` with YAML frontmatter and a trigger-rich `description`.
- Topic-organized `references/` files, not one raw dump per source.
- `references/INDEX.md` as the start-here map.
- `references/topics.json` as machine-readable topic metadata.
- `references/source-map.json` when source licensing allows digest-and-attribute handling.
- An `Inspired by` section instead of source-map metadata when licensing is restricted or unknown.
- A stdlib verifier script under `AI/work/verify_{skill_slug}.py`.
- A prompt archive under `user/prompts-given/`.
- A result log under `user/prompt-results-logs/`.
- A resumable build ledger under `AI/work/` when sources are large or multi-batch.

The generated prompt requires the receiving agent to work in passes: preserve-glossary extraction, topic organization, skill and reference drafting, verifier creation, fidelity checks, and final reporting.

It also requires the receiving agent to use the repo-local `skill-drafting` skill as the quality contract. That adds staged collaboration gates, category classification, trigger examples, negative triggers, a mandatory `Gotchas` section, a good-vs-bad quality harness, an observation artifact, memory-file assessment, and independent subagent review or an explicit unavailable-subagent fallback.

## What It Does Not Do

- It does not create or edit a skill package.
- It does not call Claude, Codex, OpenAI, Anthropic, or any other AI API.
- It does not fetch URLs or download source documents.
- It does not validate the truth of your registered sources.
- It does not replace the final receiving-agent verification step.

Your job as operator is to make the interview answers precise enough that the receiving agent can build the right package without guessing.

## First Run And Settings

On first run, the script creates a sibling log file:

```text
prompt-builder-scripts/ai-support/skill-package-prompt-builder.log
```

The log is divided into five zones:

- Settings: current defaults such as assist mode, clipboard behavior, backup mode, and output folders.
- Sessions: named working contexts.
- Session drafts: latest answers for each session, saved as you work.
- Run history: completed runs.
- Development log: compact records of decisions and assist-loop activity.

Generated prompt text is not stored in the sibling log. Prompt backups, when enabled, are saved separately under the configured ignored user folder.

At startup, use:

- Enter or `1` to start the prompt builder.
- `r` to reprint the last completed prompt for the current session.
- `s` to review or change settings.
- `q` to quit cleanly.

Settings include:

- `assist_mode`: whether the optional copy-paste LLM assist loop runs.
- `copy_to_clipboard`: whether the final prompt is copied automatically.
- `backup_mode`: `ask`, `always`, or `never` for saving Markdown prompt backups.
- `backup_folder`: default local prompt-backup destination.
- `prompt_archive_folder`: where the receiving agent should save the prompt it receives.
- `result_log_folder`: where the receiving agent should save its work log.

## Sessions And Navigation

The script supports named sessions so you can keep separate skill-building efforts apart.

- Choose Continue to resume a saved session.
- Choose New to start a named session with a new root hash.
- Draft answers are persisted after each major step, including source-registry edits.
- Quitting partway through leaves the log valid and lets Continue resume from saved answers.

At most prompts:

- Enter accepts the shown default.
- `b` goes back to the previous question when that prompt supports backtracking.
- `q` quits cleanly.
- `m` starts multiline entry; finish with a line containing only `END`.

Do not paste several raw lines into a normal one-line prompt. Use multiline mode for pasted blocks so lines do not spill into later questions.

## Optional LLM Assist

The assist loop is optional and works by copy-paste. It does not use an API key.

When enabled, the script prints an assist request. Paste that request into any LLM, paste the reply back into the script, and end the pasted reply with a line containing only `<<END>>`.

The assist loop can:

- Suggest stronger interview answers.
- Critique a draft generated prompt before final emission.
- Apply recognized field suggestions as editable defaults.
- Record compact assist decisions in the development-log zone.

You remain responsible for the final answers. Treat assist output as advice, not authority.

## Source Registry

The source registry is the most important part of the run. Register every source the receiving agent must study.

Each source has:

- Label: short human-readable name.
- Source type: `official-docs`, `local-file`, `url`, `example`, `api`, `tutorial`, or `note`.
- Location: URL, repo-relative path, or local path.
- Priority: `high`, `medium`, or `low`.
- Trust level: `official`, `verified`, `community`, or `unverified`.
- Intended use: what the receiving agent should extract from the source.

Use source type intentionally:

- `official-docs` and `api` sources receive the strictest fidelity treatment. Names, flags, signatures, UI labels, numbers, and paths must survive unchanged.
- `tutorial`, `example`, and `note` sources can be summarized more loosely, but the receiving agent still must not invent facts.
- `local-file` sources must exist unless you deliberately add them for later resolution.
- `url` sources are fetched by the receiving agent, not by this script.

For URL sources, you may optionally point to a pre-fetched local copy. If no local copy is recorded, the generated prompt tells the receiving agent to fetch the URL and relevant in-domain links into `AI/work/{skill-slug}-docs/`.

For local PDF sources, the script asks for approximate page count. Unknown or large PDFs trigger ledger-oriented ingestion instructions so the receiving agent can work in batches. URL sources also trigger resumable ingestion because fetching and link-following may exceed one pass.

## Key Interview Decisions

The script turns your answers into operational requirements. Strong answers are concrete, observable, and scoped to one skill.

### Skill Name And Slug

Use the human-readable name for people. Use the slug for folder and YAML naming. The slug should be lowercase kebab-case with letters, digits, and single hyphens.

Good slugs are narrow: `github-actions-review`, `gimp-layer-workflows`, `billing-api-reference`.

### Purpose

State the one job the skill exists to support. Avoid combining unrelated jobs because over-broad skills trigger poorly and waste context.

### Trigger Conditions

Write trigger text for model routing, not human marketing. Include:

- Phrases users actually say.
- Tools, services, libraries, or file types involved.
- Adjacent cases where the skill should not trigger.

The generated prompt pushes the receiving agent to make `description` specific and trigger-rich so the skill does not under-trigger.

### Skill Category

Choose the primary category before drafting. The script uses the same 9-category taxonomy as the `skill-drafting` skill: library/API reference, product verification, data fetching and analysis, business process automation, code scaffolding/templates, code quality/review, CI/CD and deployment, runbook, and infrastructure operations.

### Example Tasks And Negative Triggers

Provide one direct trigger task, one paraphrased trigger task that omits the skill name, and one negative-trigger example. These become trigger tests in the generated prompt and help the receiving agent avoid over-triggering.

### Runtime Workflow

Describe how an agent should use the finished skill:

- Load `SKILL.md`.
- Use the task-router table to choose references.
- Read only the reference files needed for the current task.
- Run scripts or checks when correctness depends on them.
- Report verification honestly.

### Files To Generate

The default file set is intentionally opinionated:

- `SKILL.md`.
- `references/INDEX.md`.
- `references/topics.json`.
- Topic-specific reference files.
- Attribution or inspiration handling based on license posture.
- A ground-truth verifier script.

Edit the file set only when a skill truly needs additional assets, scripts, or references.

### Documentation Depth

The generated prompt favors deeply digested, task-organized references:

- One coherent concern per reference file.
- H2/H3 headings.
- One example per concept.
- Concise prose.
- No raw source dumps.
- Split large topics instead of padding one file.

### Verification Requirements

Define what must be true before the skill counts as done. Prefer binary checks:

- Every reference is listed in `INDEX.md`.
- Every reference has one `topics.json` entry.
- Cross-links resolve.
- Preserve-glossary terms still appear unchanged.
- The verifier script runs clean.
- No invented facts or unresolved gaps remain.

### Gotchas, Quality, And Observation

`--known-gotchas` captures recurring failure modes the skill must prevent. `--quality-rubric` states how to reject bad output, not just recognize good output. `--observation-method` gives the receiving agent a way to see the output while building, such as tests, logs, screenshots, traces, sample runs, seeded bad examples, or fixtures. If you leave it as `category-default`, the generated prompt selects the default artifact for the chosen skill category.

### Privacy Constraints

State how secrets, credentials, tokens, personal data, internal paths, customer data, and private examples must be handled. The generated prompt applies the privacy rule to every file the receiving agent creates.

### Out Of Scope

Set an explicit boundary. The best skill packages do one job well. Anything not needed for that job should be excluded or moved to a separate skill.

### Risk Level

Risk controls how much human confirmation the generated prompt requires:

- `low`: proceed through the agreed spec with checkpoints.
- `medium`: confirm key decisions and assumptions before writing files.
- `high`: confirm every key decision and propose guardrails for hard-to-reverse work.

Choose higher risk when the skill can affect production systems, secrets, destructive operations, compliance, billing, deployments, or critical infrastructure.

### Source License Posture

License posture controls how the receiving agent may use source material:

- `permissive-or-owned`: digest and attribute. The generated package may include `references/source-map.json` and per-file license notes.
- `copyleft-or-restricted`: inspiration-only. The generated package should use original prose and an `Inspired by` section instead of source-map metadata.
- `unknown`: treated as restricted. This is the safest choice when you are unsure.

This is a writing-mode decision, not a reason to skip the skill. Restricted or unknown sources can still inform a useful package; the receiving agent must express the content in original structure and wording while preserving factual identifiers exactly.

### Build Intent

Choose:

- `new` when authoring a new skill package.
- `update` when refreshing an existing skill. Update mode tells the receiving agent to read existing files first and preserve unrelated content.

### Parallelism

Parallelism is offered only when registered sources are large. The generated prompt keeps the receiving agent as orchestrator and prevents write races:

- `none`: one sequential build.
- `generic`: model-agnostic subagents or workers.
- `haiku`: Claude Haiku subagents for fast bulk fetch and digest.
- `opus`: Claude Opus subagents for stronger reasoning.

Subagents, when used, fetch or digest disjoint source batches and return notes. The orchestrator owns all shipped-file writes, ledger updates, normalization, and verification.

### Memory And Subagent Policies

`memory_file_policy` controls how the generated prompt handles `AGENTS.md` and `CLAUDE.md`. The default, `assess-then-ask`, requires inspection and proposed edits but no memory-file mutation without explicit approval.

`subagent_review_policy` controls independent review beyond large-source digestion. The default, `nontrivial`, asks the receiving agent to use subagents for architecture and quality review when the runtime supports them, or to report an explicit separated self-review fallback.

## Skill-Drafting Principles Encoded By The Script

The script's generated prompt incorporates these skill-authoring rules.

### Skills Are Folder-Based Context Packages

A skill is not just a Markdown file. It is a portable folder that can contain:

- `SKILL.md` for instructions and routing.
- `references/` for detailed docs loaded on demand.
- `scripts/` for deterministic operations.
- `assets/` for templates, examples, fonts, media, or reusable output material.

Use optional folders only when they reduce repeated work, improve reliability, or keep large context out of `SKILL.md`.

### Frontmatter Is The Trigger Surface

Frontmatter is always visible to the model. Keep it selective and useful:

- `name`: kebab-case, matching the folder.
- `description`: what the skill does and when to use it.
- Optional metadata only when it reduces ambiguity.

The description should include trigger phrases, concrete task names, tools, file types, and negative triggers where overlap is likely.

Generated or updated `SKILL.md` frontmatter must remain valid for strict skill loaders:

- Keep the rendered `description` at or below 1024 characters.
- Use folded scalar YAML (`description: >-`) for multi-clause descriptions, especially descriptions containing colons, quotes, commas, paths, API names, or negative-trigger boundaries.
- Verify the frontmatter parses cleanly before claiming the skill package is loadable.

### Progressive Disclosure Is The Default Architecture

Put routing in `SKILL.md`, depth in references, and deterministic checks in scripts.

This keeps the model from loading every detail on every task while still making specialized knowledge discoverable when needed.

### Gotchas Matter

Gotchas are often the highest-signal content in a skill. Capture real failure modes:

- Common model mistakes.
- Fragile command or API usage.
- Local conventions the model would not infer.
- Validation steps that are easy to skip.
- Recovery paths for common failures.

### Do Not Railroad The Agent

Write goals, constraints, and decision criteria. Use strict step order only when order, safety, or correctness requires it.

### Setup And Config Need A Home

If a skill needs account IDs, repo conventions, preferred tools, paths, or user preferences, document where that setup lives and what to ask when missing. For durable memory, prefer a stable plugin data location when the host platform provides one.

### Scripts Are Reliability Tools

Use scripts for repeated transformations, validations, scaffolding, parsing, and fragile checks. Prose can describe judgment; code should handle deterministic correctness where possible.

### MCP Skills Add Workflow Knowledge

MCP gives tool access. The skill should provide:

- Setup and connection checks.
- Authentication and scope expectations.
- Tool names and case-sensitive identifiers.
- Tool-call order.
- Data passed between services.
- Validation before phase transitions.
- Error handling, rollback, and escalation guidance.

For multi-MCP workflows, keep phases disjoint and centralize error handling.

### Hooks And Guardrails

Where the host supports hooks, allowed-tool restrictions, or session-scoped guardrails, use them for temporary strictness. Good guardrails include:

- Preflight checks before risky operations.
- Schema and artifact validation.
- MCP health checks.
- Write-scope freezes.
- Blocks around destructive commands.
- Clear stop conditions before high-cost actions.

### Testing And Iteration

A finished skill should be tested across:

- Obvious trigger prompts.
- Paraphrased trigger prompts.
- Adjacent non-trigger prompts.
- Workflow completion.
- Tool or MCP success.
- Error handling.
- Baseline improvement against doing the task without the skill.

Use failures to revise descriptions, gotchas, scripts, references, and verification.

### Distribution, Composition, And Measurement

Choose distribution based on audience:

- Personal or small team: repo-local skill folder.
- Larger organization: plugin or marketplace model.
- Public use: repository with human README, install steps, examples, and screenshots.
- API or agent system: package for the runtime and document requirements.

Use composition when one skill naturally hands off to another. Name dependencies, describe the handoff artifact, and define behavior when the other skill is unavailable.

Measure skills by usage and quality:

- Under-triggering.
- Over-triggering.
- User corrections.
- Failed tool calls.
- Repeated gotchas.
- Popularity and promotion candidates.

## 9 Skill Categories To Consider

Use these categories when deciding whether your source material should become one skill or several:

1. Library and API reference: exact use of libraries, CLIs, SDKs, internal APIs, and gotchas.
2. Product verification: repeatable proof that product behavior works.
3. Data fetching and analysis: standard queries, dashboards, monitoring, and interpretation.
4. Business process and team automation: recurring team workflows and status outputs.
5. Code scaffolding and templates: local boilerplate with natural-language constraints.
6. Code quality and review: organization review standards and deterministic checks.
7. CI/CD and deployment: release, deploy, PR, cherry-pick, and environment procedures.
8. Runbooks: symptom-driven investigations and structured reports.
9. Infrastructure operations: maintenance, cleanup, dependency, cost, and guarded operational work.

If your idea spans several categories, either state the dominant category clearly or split the work into multiple focused skills.

## Generated Prompt Contract

Before the receiving agent writes non-trivial files, the generated prompt tells it to present:

- Goal.
- Key decisions.
- Assumptions.
- Acceptance criteria.
- Verification plan.

During implementation, the receiving agent must:

- Inspect existing local conventions.
- Build a preserve-glossary before drafting references.
- Organize reference files by topic, not source.
- Preserve names, commands, flags, paths, signatures, UI labels, numbers, units, and version values exactly.
- Mark source gaps explicitly instead of inventing facts.
- Keep `SKILL.md` concise and route to references.
- Use native editing tools for shipped files.
- Ask before overwriting or deleting existing skill files.
- Run the verifier script and fix failures.
- Save the exact prompt and final work log to the configured user folders.

The generated prompt verifies itself before emission. If required clauses are missing, the script refuses to emit the prompt and reports the failure.

## Operator Checklist

Before running:

- Know whether this is a new skill or an update.
- Gather authoritative sources.
- Decide the target agent surface: Codex, Claude, or both.
- Decide the license posture of all sources.
- Decide whether the skill is low, medium, or high risk.

During the interview:

- Keep the skill purpose narrow.
- Write trigger language for routing accuracy.
- Register every source with label, type, location, priority, trust, and intended use.
- Use multiline mode for long answers.
- Make verification requirements observable.
- Put secrets and privacy limits in the privacy answer.

Before pasting the generated prompt:

- Read the review screen.
- Confirm the source count and usability.
- Confirm license posture and risk level.
- Confirm generated files and output folders.
- Confirm any parallelism choice for large sources.
- Copy the prompt from the terminal markers if clipboard copy failed.

After the receiving agent finishes:

- Check the prompt archive and result log exist.
- Run the generated verifier script.
- Inspect `SKILL.md` frontmatter and trigger description.
- Confirm references are topic-organized and indexed.
- Confirm source attribution or inspiration handling matches license posture.
- Confirm gotchas, setup, scripts, MCP guidance, hooks, tests, distribution, composition, and measurement guidance are covered where relevant.
- Test positive triggers, paraphrased triggers, and negative triggers in a fresh agent session.

## Practical Defaults

For most skill-building runs:

- Use `high` risk when unsure; it forces explicit decisions and guardrails.
- Use `unknown` license posture when unsure; it forces original wording.
- Keep parallelism off unless sources are large.
- Prefer one focused skill over one broad skill.
- Add references only when the model needs more than `SKILL.md`.
- Add scripts when correctness should be deterministic.
- Treat the final generated prompt as a spec that the receiving agent must verify before building.
