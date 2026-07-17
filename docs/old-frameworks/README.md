# Old Frameworks

An archive of the frameworks that came before Agent Kaizen's current [Kaizen System](../../Kaizen_System.md) (`SAVMI = Scope -> Adapt -> Verify -> Manage -> Improve`). They are kept here for lineage and reference: this is where the ideas the current system grew out of are recorded, and where retired versions of the framework are set aside as it keeps evolving.

Each document below was written as operating instructions loaded into an AI coding agent's context (Claude Code, Codex, or similar) — the operating model the agent worked to before the Kaizen System took its place. They are superseded, not deleted: the current method carries their core ideas forward in a single managed loop backed by a local record store.

## What is archived here

### `three-layer-scaffolding-method-for-LLM-LRE-FIM-integration.md`

**A Three-Layer Scaffolding Method — Spec → Verifier → Environment.**

A conceptual framework distilled by David Levy from Andrej Karpathy's approach to working with AI agents. It was loaded into an agent's context as the operating model whenever the work was to generate one of three artifacts: prompts for complex or multi-step work, `CLAUDE.md` / `AGENTS.md` memory files, or skills. Every artifact the agent produced had to trace back to one or more of its three layers:

- **Spec** — the artifact that transfers the human's understanding into machine-usable form (goal over task, agile scope, explicit decision confirmation).
- **Verifier** — verification designed in on top of every spec, turning "looks good" into checkable criteria and a real feedback loop.
- **Environment** — the persistent workshop where the spec and verifier live and compound: the memory file, the knowledge base, skills, and tool-level guardrails.

Its one-sentence summary: the human supplies understanding, the spec encodes it, the verifier checks against it, and the environment makes all three compound. This is the conceptual predecessor the current SAVMI method grew out of: Scope and Verify carry the Spec and Verifier forward, and Manage plus a durable record store take the place of the Environment's "make it compound."

### `better-skill-framework.md`

**A Better Skill Framework — deterministic documentation → gold-standard agent skills.**

A concrete, reproducible method for turning a body of documentation into a quality agent skill: a focused, index-navigated, verifiable reference package an agent can load on demand. It was the authoritative spec for what a skill should look like and the deterministic pipeline that produced one (`source → ingest → corpus JSONL → build → finalize → validate`), with a validator as the quality gate.

The framework described itself as the **Environment layer of the Three-Layer Scaffolding Method made concrete**: skills are the compounding, reusable artifacts that make every future session start stronger, and the validator is their Verifier. It is the predecessor to the current skill-drafting workflow.

## Why they are here, not gone

These frameworks did real work; the current [Kaizen System](../../Kaizen_System.md) supersedes them by folding their strongest ideas — spec-first scoping, verification as an architectural layer, and compounding reusable artifacts — into one managed SAVMI loop whose outputs are structured records rather than prose alone.

As the framework continues to evolve, retired versions of `Kaizen_System.md` will be archived alongside these documents, so the lineage of the method stays readable end to end.
