# Kaizen System

The Kaizen System is a five-layer approach to continually improving workflows for agentic coding work. It can be used by humans, Codex, Claude, local models, or multi-agent workflows. Like a delicious 5 layer burrito, you'll keep coming back for more... improvements.

A memorable mnemonic because every good and bad idea has one:

```text
SAVMI = Scope -> Adapt -> Verify -> Manage -> Improve
```

## 1. Core Idea

Cyclic product and service systems need at least five things to produce continual improvement:

| Layer | Purpose | Primary output |
| --- | --- | --- |
| Scope | Turn intent and evidence into plans | Iterative Spec, assumptions, acceptance criteria |
| Adapt | Change the system through bounded work | Execution, contracts, patches, scripts, artifacts |
| Verify | Decide whether the result works | Go/no-go result, proof, structured findings |
| Manage | Keep work data usable | Records, hashes, source locks, reports, policies |
| Improve | Decide what to improve next | Retrospective, next-cycle priorities, promoted work |

The Kaizen System separates responsibilities:

```text
Scripts control structure and repetitive work.
Agents control meaning, synthesis, and adaptation.
Verification controls acceptance.
Management controls durable memory and policy.
The user controls priorities and approvals.
```

SAVMI is a cycle. Tiny tasks can run a tiny cycle: define the goal, make the change, verify it, and record only the useful trace. Increase the amount of Scope, Adapt contracts, verification, and management records as risk, ambiguity, cost, or blast radius increases.

Evidence authority applies across the whole system. Raw upstream evidence such as source code, specifications, official documentation, test output, logs, artifact hashes, and explicit user decisions outranks summaries, generated references, and model synthesis. Compiled references are useful operating layers, but when they conflict with evidence, return to the evidence and record the correction.

## 2. Scope

Scope is the planning and discovery layer. It is where the system learns what matters before it tries to change anything.

Scope includes:

- evidence-only research into the current project;
- detailed interviews that clarify priorities and tradeoffs;
- assumptions and how they will be tested;
- boundaries for the current increment;
- acceptance criteria that can be verified;
- a compact **Iterative Spec**.

When Scope needs outside material - specifications, PDFs, manuals, standards, web references - pull it through a managed ingestion path instead of pasting raw text into the context window. Each external source becomes a source-locked artifact with a content hash, a provenance tier, and a recorded extraction method, so later cycles can trust it and reopen it instead of fetching it again.

The Iterative Spec should be improved until it is ready to adapt. For substantial work, the agent should ask enough targeted questions to remove risky ambiguity. For small work, the spec can be a few sentences, but it still needs a clear goal and a way to know whether the result worked.

A concept of IRL impact and review belongs here. For major architecture, schema, migration, security, cost, workflow decisions, etc... agents can forecast likely consequences, failure modes, maintenance costs, and user friction. Those forecasts are only hypotheses and should not be used as a primary rubric for decision making. But inherently incorrect assumptions should be identified and addressed at this step before work moves forward.

Decision and approval gates prevent silent drift. If new evidence changes scope, risk, cost, data shape, security posture, user-facing behavior, or acceptance criteria, pause adaptation, return to Scope, and get the decision approved before continuing.

## 3. Adapt

Adapt is the change layer. It turns the approved Scope into bounded but adaptive execution. Just because our favorite Toy-R-Us stores did not adapt, does not mean you should not either. Change is inevitable, be flexible enough to facilitate adaptive approaches to fulfill your scope.

Adapt includes:

- an execution contract for substantial work;
- allowed and forbidden effects;
- required capabilities, tools, and credentials;
- output contracts and rollback notes;
- consistent workflow tracking through a ledger;
- small implementation increments;
- plan adaptive revisions when evidence changes.

The execution contract keeps agents from turning a narrow request into broad uncontrolled work. It should define what can be read, what can be written, what commands may run, what network access is allowed, what outputs must exist, and what terminal states are possible.

Capabilities need boundaries. Any tool, script, skill, agent, MCP server, or gateway should have an understandable contract for what it can read, write, call, and return; what credentials or approvals it requires; what schemas it accepts or emits; and what evidence proves it behaved correctly.

Deterministic work should move into maintainable coded scripts. Agents should not spend context remembering file naming rules, formatter commands, report paths, schema transitions, or ledger mechanics when a script can do those jobs perfectly repeatable. This ends up saving a substantial amount of tokens over time and reduces inherent agentic drift.

A LEDGER is the durable timeline of the work. It records decisions, adapted plans, commands run, artifacts changed, verification results, failures, and handoff notes so humans and agents can resume from evidence instead of parsing what happened from a chat history. Save on context window, save on tokens.

Suggestion until no longer an issue, compact your context window before major tasks.

## 4. Verify

Verify is the acceptance layer. It answers the operational question:

```text
Can this work move forward: yes or no?
```

Use deterministic evidence first whenever possible:

- tests;
- linters and formatters;
- type checks;
- schema validators;
- rendered screenshots;
- command output;
- artifact hashes;
- source locks.

Use human auditing or LLM synthesis when the quality question requires judgment, such as maintainability, architecture fit, boundary cleanliness, code review, or user-facing clarity. Synthesis-based review must still cite evidence and produce actionable findings.

Structured durability is part of acceptance. A durable record should pass a schema contract before it is written: required fields present, no invented fields, controlled vocabularies honored. The same contracts can steer a local model you control to emit valid structure at generation time, and they catch malformed records from any model at the write gate. But remember the rule - a perfectly valid record can still be wrong. Structure is a precondition for durability, never a substitute for evidence.

Verification records should include:

- go/no-go result;
- structured status;
- evidence locations;
- ordered findings;
- proposed remedies;
- severity, scope, and actionability labels;
- command metadata;
- artifact links and hashes where practical.

Useful status labels you could include:

- `VERIFIED_ACCEPTABLE`
- `ACCEPTABLE_WITH_CONCERNS`
- `NEEDS_HUMAN_DECISION`
- `STRUCTURAL_REWORK_RECOMMENDED`
- `VERIFICATION_FAILED`

## 5. Manage

Manage is the durable operations layer. It is the part most agentic coding approaches I think underweight: records, policy, proof, context hygiene, data management, and retrieval.

The data plane aspect of management helps improve efficiency for automation. The process saves what happens, stores it in a structured database, and makes it available to later cycles without requiring a full chat-history reread. Targetable, bite-sized records under one thousand characters when possible. Yay more saving tokens!

Rely on ledgers and validated reports instead of chat memory, pass distilled packets between agents, and avoid forcing the main context window to carry raw transcripts, long logs, or every intermediate observation.

Management has a write half and a read half, and the read half is where the value compounds. A session should start from a compact digest of current state — active policy, open pitfalls, blocking verification results, recent validated lessons, open tasks — and pull deeper records on demand: verification conclusions by task or severity, lessons with their full promotion lineage, score trends over time. Written and never read back, the data plane is a diary; read back at session start, it is an operating picture.

The managed data plane should be written through deterministic commands or scripts. Agents should not invent schemas, filenames, IDs, timestamps, hashes, revision rules, or status transitions while doing the work. The write path should own validation and deny malformed records before they become durable.

A Kaizen data plane should manage:

- tasks and plans;
- ledgers and reports;
- GOTCHA, LEARNING, and LEARNED records;
- verification and proof records;
- eval cases and run metadata;
- source locks and provenance;
- artifact references and hashes;
- subagent and diagnostic packets;
- ingested external evidence: source-locked documents, typed blocks, and retrieval chunks;
- activity traces and evaluation scores;
- IRL Review predictions, corrections, and observed outcomes;
- anti-pattern catalog entries;
- private policy or security context;
- future gateway or routing events.

Markdown can be a useful view, command stub, or export, but durable records should live in the managed data plane. This prevents agents from scattering important facts across chat transcripts, terminal scrollback, and scratch files.

The evidence plane turns outside material into bite-sized, source-locked records: ingest a file or a page, lock its source with a hash and a provenance tier, split it into typed blocks, and chunk it for retrieval. Transformed evidence is derived evidence, not raw truth - OCR and layout extraction can lie - so every record carries its extraction method, a confidence signal, and page or position anchors. Retrieval always keeps a thread back to the surrounding source: a chunk that looks clean can still be missing the neighbor that changes its meaning, so chunks link to their neighbors and back to the original document. Heavy extractors and embedders are optional, capability-activated backends; the deterministic readers and chunkers always work without them.

Telemetry is managed too. A trace is the tree of what happened in a task - model calls, tool calls, evidence reads, verifier runs - and a score is a judgment about a step or the whole task. Capture them with flags that make them findable - level, environment, session, tags - but redact by default: store a hash or a reference, never a raw secret or a personal path. Over-instrumentation is its own kind of slop, so keep traces opt-in and bite-sized and make the records earn their keep.

What are LEARNING and LEARNED records you might ask? Frontier models are great at catching GOTCHAs but that does not mean that GOTCHA was the root cause or valid. In this burrito, GOTCHAs are freely recorded by models, but are not implemented until validated or human prompted to LEARNING. And once implemented are moved to LEARNED for separate records. Why LEARNED and not a regular ledger line you may ask? Fast pattern recognition at less token usage. Inevitably, patterns will emerge from your GOTCHA fixes from agentic coding. Don't waste time and tokens, pull in your LEARNED lessons as needed.

## 6. Improve

Improve is the retrospective and next-cycle planning layer.

Improve asks:

- What failed or almost failed?
- What repeated friction appeared?
- What should become deterministic tooling?
- What should become an eval?
- What should become a quality rule?
- What should be removed because it adds complexity?
- What should enter the next Scope cycle?

LEDGER, GOTCHA, LEARNING, and LEARNED are management records, but Improve decides how to use them.

Promotion path:

1. GOTCHA: observed failure, pitfall, or suspected issue.
2. LEARNING: investigated root cause and correction pattern.
3. LEARNED: implemented improvement in scripts, evals, docs, or system behavior.

Promotion pays back at read time: a promoted lesson carries its lineage, so a later session pulls the lesson and its evidence in one cheap query instead of re-deriving it.

The Improvement Lab is how Improve compounds. It borrows the good ideas from program-not-prompt frameworks without dragging in their machinery: a task contract is a typed signature, a metric is a scorer that must cite evidence, a case set is assembled from your own eval cases and LEARNED records, and demonstrations are the examples your past successes already proved. The lab generates candidate variants of a prompt, a trigger, or a verifier, scores each against the case set, and proposes the winner with before-and-after numbers. Nothing auto-applies. A human promotes the winner through GOTCHA to LEARNING to LEARNED. Continual improvement, within reason: bounded, grounded, and gated.

Improve does not bypass Scope and Adapt. It feeds the next cycle with better priorities and stronger evidence.

## 7. Skills And Capabilities

Skills and support scripts are cross-layer capabilities. They can help Scope research, Adaptive execution, Verify, review, Manage records, and Improve learning.

A skill should not be a horde of clowns filling your model context window car. It is a small trigger surface plus optional references, scripts, fixtures, and verification behavior that loads only when relevant or preferably only on demand.

Common runtime roles for skill scripts:

- utility;
- verification;
- data enrichment;
- orchestration.

Common package shapes:

```text
Portable core:
skill-name/
`-- SKILL.md

Skill package:
skill-name/
|-- SKILL.md
|-- scripts/
|-- references/
|   |-- INDEX.md
|   |-- topics.json
|   `-- <topic>.md
|-- permissions.yaml
|-- output.schema.json
`-- evals/
    |-- GOTCHA.md
    |-- LEARNING.md
    `-- LEARNED.md
```

`SKILL.md` description text is trigger text, not a summary. It should name tasks, phrases, file types, tools, and contexts that should load the skill, including cases where the user never says the skill name.

Route bigger skills like a good handbook, not a tutorial. A hub skill triages the problem and points to leaf skills; each leaf answers when and why, not just how, and links to the authoritative source instead of inlining it. Give every skill an explicit **Use when** and a **Do NOT use when**, plus a short **What NOT to Do** list. Those three sections are the cheapest way to stop a skill from firing on the wrong task or repeating a mistake it already learned.

Apply the same evidence authority model inside skills:

| Term | Meaning |
| --- | --- |
| Evidence authority | Upstream specs, official docs, repos, test output, captured evidence |
| Compiled operational reference | Recontextualized material optimized for agent use |
| Navigation and control surface | `SKILL.md`, indexes, routing metadata, command stubs, maintenance rules |

When sources conflict, prefer higher authority tiers:

1. Normative standards and specifications.
2. Official project documentation.
3. Maintained implementation, tests, and examples.
4. Design guidance, essays, transcripts, and idea files.

## 8. Evals And Quality

Structural validation is not enough. Skills and workflows need behavioral evals.

Useful eval categories:

- should-trigger cases;
- should-not-trigger cases;
- routing cases;
- grounding cases;
- proof and verification cases;
- security and permission cases;
- freshness cases;
- learning-regression cases.

Quality review should cover more than behavior correctness:

- behavior;
- maintainability;
- testability;
- boundary cleanliness;
- architectural fit;
- complexity deletion;
- canonical helper reuse.

High-recall review mode is useful for substantial work. Missing an important structural issue is worse than raising some false positives. Findings should be labeled by evidence, severity, scope, and actionability.

Agent contracts have a density standard: plans, task specs, subagent and handoff instructions, durable records, and generated reports must be terse, technical, and signal-dense — no colloquialisms, filler, or restatement, maximizing signal per token. Human-authored documentation and commit messages keep a human voice and are out of scope. The standard is deterministically checkable (a filler and hedge wordlist, qualifier density, and near-duplicate-sentence detection), so its verdict can gate; see the contract-bloat anti-pattern in the next section.

Model freshness is a standard. Default model repos — text generation, reranker, PII, and embedder — must be current best-in-class and reviewed on a cadence, never a stale default. Using an outdated or under-performant model when a better one exists contradicts the continual improvement ethos. This standard is invoked when the framework is updated or on user inquiry, not on every agent turn. Model VRAM budget should remain adaptive to each system according to user preferences, but for public facing initial adoption the defaults models are sized to a 12 GB VRAM budget.

Research on utilization or retirement of model implementation methods for this framework should be done against a fixed source allowlist of peer reviewed research papers available from platforms such as: arXiv, OpenReview, JMLR, Nature Machine Intelligence, JAIR, HuggingFace, GitHub, official Anthropic and OpenAI reports, and ACL Anthology; and all model implementation methods for the framework should be validated against available workflows and available datasets before trusted for wide adoption.

## 9. Agent Code Anti-Patterns

The anti-pattern catalog tracks recurring failure shapes that coding agents tend to introduce.

Each anti-pattern should include:

- symptom;
- maintainability harm;
- trigger evidence;
- preferred correction pattern;
- valid exceptions;
- verification.

Keep entries operational. A reviewer should know what evidence triggers the finding and what correction to prefer.

For example, agent workspace discipline. A spawned subagent with shell and network access clones repositories into the OS temp directory or the user profile on the system drive instead of the project work area. The harm is a polluted system drive, untracked bulk, and a crawler running from a shared temp dir. The correction is to pin all scratch to the project work area, forbid cloning and network writes in spawned-agent prompts, and prefer a single parent-side fetch that read-only agents point at. The harness redirects its own temp into the work area, but agents launched outside the harness do not inherit that, which is exactly how the footgun fires.

For example, contract bloat. An agent-authored plan or record accumulates colloquialisms, hedges, and restated sentences. The harm is wasted context and degraded downstream-agent performance, since every filler token displaces signal. The correction is to write terse, technical, signal-dense contracts and cut restatement. The trigger evidence is a filler or hedge phrase, high qualifier density, or a near-duplicate sentence, which a deterministic lint flags. The valid exception is human-authored prose and commit messages, which keep a human voice.

## 10. Local Harness Example

The agent-kaizen repository implements the Kaizen System with a local harness:

- `kaizen.py` as the structured write path;
- a local Turso direct-file database under `AI/db/`;
- session read-back: `R0` (session digest), `L10` (lessons with lineage), `Q9` (verification conclusions), `T4` (score trends);
- Markdown command stubs and generated views under `evals/`;
- skill junctions for Codex and Claude;
- targeted reports and proof artifacts.

The system concept is backend-agnostic. A project can use a different database, a remote service, or a larger governance gateway as long as the managed records remain structured, queryable, and governed by deterministic write paths.

Agentgateway or similar routing gateways should be used at large scales for: centralized identity, RBAC, remote tool federation, model routing, budgets, rate limits, failover, and auditable traces across multiple agents, users, services, or machines.

A local generative-backend seam belongs to this same layer: routing cheap or private sub-tasks to a local model, with budgets and routing events recorded like any other managed data. It is best designed as its own increment. A local or cheaper model can help with triage, summarization, or first-pass drafting, but it is never the final acceptance authority unless a deterministic verifier backs the decision. The first such backend has shipped: a ComfyUI workflow runner records each generative run with a graph hash, the stored workflow, the seed, and output hashes, so the asset is reproducible and replayable. It is optional and capability-activated — the agent authors the workflow, and an unreachable server fails gracefully rather than blocking the task. A second backend has joined it: an optional OpenAI-compatible model seam (a local Ollama server by default, any remote endpoint by configuration, or an in-process sentence-transformers backend with no server at all) that supplies embeddings — turning evidence-chunk storage and vector search live over Turso-native vectors — and advisory text generation. It is opt-in (configured by environment), API keys stay in the environment and are never stored, and with nothing configured the harness stays on its deterministic, lexical baseline.

The Kaizen System performs best when every cycle starts with better context, fewer repeated mistakes, and stronger evidence than the last one.

Enjoy your continually improving burrito, now go make something amazing!
