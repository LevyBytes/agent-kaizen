# A Three-Layer Scaffolding Method: Spec / Verifier / Environment

### Spec → Verifier → Environment

A conceptual framework distilled by David Levy from Andrej Karpathy's approach to working with AI agents, written as **operating instructions for an AI agent** (Claude Code, Codex, or similar). Load this document into context when generating any of the following artifacts:

1. **Prompts** for complex or multi-step work
2. **CLAUDE.md / AGENTS.md** memory files
3. **Skills** (reusable task handbooks)

The framework has three layers. Every artifact you build must be traceable to one or more of them. Part 4 contains the concrete build recipes; Parts 0–3 are the principles those recipes enforce.

---

## Part 0 — First Principles

These five principles govern everything below. When a recipe in Part 4 is ambiguous, resolve the ambiguity by returning here.

**P1 — The context gap.** Models are strong wherever the signal is measurable and weak wherever the answer depends on unstated human context. The canonical failure: asked whether to walk or drive to a car wash 50 meters away, models say "walk — it's so close," because nothing in the prompt encodes that the car must arrive at the car wash. Every artifact you produce exists to close this gap: to move context out of the human's head and into a form the model can act on.

**P2 — The librarian model.** Treat any model (including yourself) as a librarian who can only answer from the books in its library — and who does not know which books are missing, so it answers confidently anyway. Two consequences:

- Emotional pressure, exhortation, and vague quality demands ("do better," "be careful," "make it good") change nothing. They are inputs the librarian has no book for.
- The only reliable levers are **what is in the library** (context, data, environment — Layers 1 and 3) and **how answers get checked** (verification — Layer 2).

**P3 — Understanding is non-delegable.** Karpathy: "You can outsource your thinking, but you can't outsource your understanding." Goals, stakes, and cost-of-error can only come from the human. Artifacts must be designed to _extract_ understanding from the human (interviews, explicit decision confirmation) — never to invent it on their behalf.

**P4 — Feedback loops multiply quality.** Per the creator of Claude Code, giving the agent a real feedback loop roughly doubles or triples final output quality. Verification is therefore not a final step bolted on at the end; it is an architectural layer designed in before any work begins.

**P5 — Cost-of-error determines control strength.** Match the enforcement mechanism to the cost of getting it wrong:

| Cost of error       | Control mechanism                                                                     |
| ------------------- | ------------------------------------------------------------------------------------- |
| Low                 | Prose guidance in the prompt or memory file                                           |
| Medium              | Explicit human confirmation before acting ("ask first")                               |
| High / irreversible | Hard, tool-level enforcement (hooks, permission rules, sandboxes) — never prose alone |

---

## Part 1 — Layer 1: The Spec

**Definition:** The spec is the artifact that transfers the human's understanding into machine-usable form. It is more detailed than a plan. Plan mode output is a useful starting point, but it operates at too high a level — the spec is co-designed with the human and resolves the decisions a plan leaves open.

### 1.1 Extract the goal, not the task

A task is what the human typed. The goal is **the decision the output drives**. "Create an end-of-month report" is a task; "give the leadership team the numbers they need to decide whether to cut the underperforming product line" is a goal. The goal can never be inferred — it must be extracted.

**Goal interview protocol.** Before producing a spec for any non-trivial work, interview the human. Minimum question set:

1. What decision will this output drive, and who makes that decision?
2. Who consumes the output, and what do they do next with it?
3. What does "done" look like, stated observably?
4. What is the cost if this is wrong? _(This answer feeds the guardrail tier in Layer 3.)_
5. What constraints are non-negotiable?
6. What existing examples define the expected shape of the output? _(This answer feeds external signal in Layer 2.)_

Stop interviewing once the goal is unambiguous. Do not interrogate the human about things already stated or inferable from the conversation.

### 1.2 Scope agile, not waterfall

Waterfall: take the whole task, build the whole thing, reveal it at the end. Agile: break the task into small buckets, show results throughout, course-correct continuously. Humans instinctively use agents in waterfall mode ("here's everything, go") because it feels efficient. It isn't — every undetected drift compounds.

**Rules:**

- Bias toward the **smallest spec that produces a reviewable result**.
- Define explicit checkpoints: what gets shown to the human, and when.
- The loop is: scope tightly → execute → show → adjust → repeat.

### 1.3 Be precise; force decision verification

Every assumption the model makes is a chance to drift from what the human actually wants. The spec must therefore:

- **Enumerate assumptions explicitly.** Each one is either confirmed by the human or eliminated by adding detail.
- **Surface key decisions for explicit confirmation.** Do not bury decisions inside prose. List them, list the options, state your recommendation, and require the human to confirm before proceeding. The operative instruction: _"Make me verify key decisions explicitly to ensure nothing is missed."_

### 1.4 Spec template

```markdown
# Spec: <name>

## Goal

<the decision this output drives, and who makes it — not the task>

## Consumers & next action

<who reads/uses this, and what they do with it>

## Scope (this iteration only)

<the smallest reviewable increment>

## Out of scope

<explicitly deferred — prevents silent scope creep>

## Inputs & references

<files, data sources, historical examples — feeds Layer 2 external signal>

## Key decisions — confirm each before building

- [ ] Decision: `___` | Options: `___` | Recommended: `___` | Human confirmed: `___`

## Assumptions — confirm or eliminate each

- [ ] `___`

## Acceptance criteria

<precise, checkable — see Layer 2. No "looks good" allowed.>

## Checkpoints

<what gets shown for review, and when>
```

---

## Part 2 — Layer 2: The Verifier

**Definition:** The verification process sits on top of every spec. It converts "looks good" into checkable criteria and gives the model the feedback loop from P4. The librarian doesn't know which books it's missing (P2), so it cannot reliably self-assess — verification must be engineered, not assumed.

### 2.1 Define evaluation criteria up front

Criteria are written **before any work begins** — into the spec, not after the output exists. Vague criteria leave the model room to be confidently wrong.

| Vague (unusable)              | Precise (checkable)                                                               |
| ----------------------------- | --------------------------------------------------------------------------------- |
| "Make this report look good"  | "Three sections; each section ends with a recommendation; total length ≤ 2 pages" |
| "Clean up the code"           | "All functions typed; existing tests pass; no function exceeds 40 lines"          |
| "Make the email professional" | "Under 150 words; one clear ask in the first paragraph; no exclamation points"    |

Properties of good criteria: **observable**, **binary where possible**, **defined before generation**, **written into the spec**.

The operative instruction: _"Before starting, outline the evaluation criteria you will use to ensure a high-quality final product. Be precise."_

### 2.2 Use a second model as critic

A different model is a different librarian with a different library. Disagreement between the two is signal — it marks exactly where assumptions, gaps, or errors hide.

- Inside Claude Code: a Codex plugin (or any second-model integration) lets you cross-check directly within the session.
- The conditional pattern, written into specs and memory files: _"If this turns into a complex build, run the final output past [second model] and reconcile any disagreement before presenting."_
- Reserve cross-checking for complex or high-stakes work. Routine increments don't need it; the cost isn't free.

### 2.3 Pull external signal — prefer ground truth over self-assessment

The strongest verification comes from outside any model entirely. Ask of every spec: _what external ground truth can confirm this output?_

- **Technical example:** Instead of trusting "the deployment succeeded," connect the session to the deployment system and read the actual status. Now success is a fact, not a claim.
- **Non-technical example:** When producing a monthly report, ingest the historical reports as format references. The output is verified against real precedent, not against the model's guess of what a report looks like.
- Other ground-truth sources: test suites, linters, type checkers, schema validators, live data queries, screenshots of rendered output.

### 2.4 Verification plan template

Embed this block in every spec and every multi-step prompt:

```markdown
## Verification plan

- Criteria: <the precise list from 2.1>
- Self-check: re-read the output against each criterion; report pass/fail per criterion
- Cross-check: <second model / human reviewer>, triggered when <condition>
- Ground truth: <tests, deploy status, reference documents, data sources>
- On failure: fix and re-verify before presenting. Never present unverified work as verified.
```

---

## Part 3 — Layer 3: The Environment

**Definition:** The workshop where Layers 1 and 2 live. The spec is the blueprint pinned to the wall; the verifier is the quality-check station by the door; the environment is the workshop itself. Most people rebuild the workshop from scratch in every chat. The point of this layer is **persistence and compounding**: an environment that improves with every use. It is the human's world, and the agent lives in it — not the other way around.

### 3.1 The memory file: CLAUDE.md / AGENTS.md

**What they are.** `CLAUDE.md` is loaded into Claude Code's context automatically at session start — it is the first thing the agent reads and shapes everything after. `AGENTS.md` is the cross-tool counterpart of the same idea, read by Codex and a growing set of other agents. Keep their content aligned; put tool-specific enforcement details wherever the relevant tool reads them.

**Anatomy — five sections, in this order:**

```markdown
# CLAUDE.md (mirror the content as AGENTS.md for Codex and other agents)

## 1. What this workspace is

<one paragraph: purpose, owner, what gets built here, for whom>

## 2. Map of the territory (knowledge architecture)

<where information lives and what is authoritative — pointers, not content>

- /docs/ ← authoritative product docs; check here first for product questions
- /data/reference/ ← reference data; treat as read-only input
- /reports/history/ ← past outputs; use as format reference (Layer 2 external signal)

## 3. Skills index & routing

<one line each: trigger condition → skill>

- monthly-report → any request for a periodic business report
- deploy-check → after any deployment, before declaring success

## 4. Working rules (always apply)

- Before any multi-step build, produce a spec AND a verification plan; show both before executing.
- Bias toward small, compartmentalized increments; checkpoint after each.
- Surface key decisions and assumptions for explicit human confirmation.
- Never present unverified work as verified.

## 5. Permission tiers

### Always do (autopilot)

- run tests, format code, read anything under /docs

### Ask first

- schema changes, new dependencies, anything touching billing or customer data

### Never do ← enforced at the tool level, not by this file (see hooks)

- edit anything under /critical/
- push to main; delete data
```

**Writing rules for memory files:**

- Every line costs context on every session. Keep it short enough to be read in full, every time. Prefer **pointers into the knowledge base** over inlined content.
- Every line must be **actionable** — a rule the agent can follow or a fact it can use. Delete aspirational lines ("be thorough," "write good code"); per P2, the librarian has no book for them.
- Every rule gets sorted into a permission tier. Any rule in **Never do** must have a corresponding tool-level enforcement (3.4); a prose-only "never" is a wish, not a rule.

### 3.2 The knowledge base

A folder system of the human's own data, organized so the agent always knows where to look. This is the compounding asset — "your data is your moat" — and the beginning of the human's own intellectual data property.

**Principles:**

- Predictable hierarchy: one concern per top-level folder; stable, descriptive names.
- An index or README at each level summarizing what lives there and what is authoritative.
- The memory file's "Map of the territory" section points into it; the knowledge base holds the actual content.
- Ingest deliberately: meeting notes, past deliverables, style references, domain documents — anything the agent will need to act with the human's context instead of generic priors (P1).

**Suggested layout:**

```
knowledge/
├── README.md            ← index of everything below
├── domain/              ← how this business/field actually works
├── examples/            ← gold-standard past outputs (Layer 2 references)
├── style/               ← voice, formatting, brand conventions
└── decisions/           ← past decisions and their reasoning (goal context)
```

### 3.3 Skills

**Rule of thumb:** anything done repeatedly becomes a skill — a handbook for one specific task. One-offs stay prompts.

**Anatomy** (Anthropic skill format):

```
skill-name/
├── SKILL.md             ← required
│   ├── YAML frontmatter: name, description (both required)
│   └── Markdown instructions
└── bundled resources    ← optional
    ├── scripts/         ← executable code for deterministic steps
    ├── references/      ← docs loaded into context only when needed
    └── assets/          ← templates, fonts, icons used in output
```

**Skill-writing rules:**

- **The description is the trigger.** All "when to use this" information lives in the description, not the body — and descriptions should be deliberately pushy, because agents under-trigger skills. Name the task _and_ the concrete phrases, contexts, and file types that should invoke it, including cases where the user doesn't use the skill's name.
- **Progressive disclosure.** Metadata (name + description) is always in context; the SKILL.md body loads on trigger (keep it under \~500 lines); bundled references load only when the skill says to read them.
- **Embed Layer 2 inside every skill.** A skill's steps end with its own verification section: criteria, ground-truth checks, and known failure modes. A skill without a verifier is half a skill.
- **Iterate by use — "run water through the hose."** The only way to find a hose's leaks is to run water through it. After every real use, note where the skill failed or was ambiguous and patch it. Skills compound exactly as fast as they get used and repaired.

### 3.4 Guardrails: matching enforcement to cost-of-error

Bucket every recurring action into one of three tiers (this is P5 made operational):

| Tier          | Meaning                       | Enforcement mechanism                                                                         |
| ------------- | ----------------------------- | --------------------------------------------------------------------------------------------- |
| **Always do** | Safe to run on autopilot      | Listed in the memory file                                                                     |
| **Ask first** | Human confirms before action  | Working rule + the agent asks                                                                 |
| **Never do**  | A line that cannot be crossed | Tool-level enforcement — hooks, permission deny rules, sandbox config. **Never prose alone.** |

**Why prose isn't enough:** a line in CLAUDE.md saying "don't touch /critical/" is a request the model can still violate — it gets you perhaps 80% of the way. For the remaining 20%, enforce at the tool level. Prefer a static deny rule when possible; the hook below is an illustrative sketch, not complete path validation.

**Claude Code enforcement — PreToolUse hook.** Hooks run shell commands at lifecycle events. A PreToolUse hook fires before a tool call; the matcher targets tool names; **exit code 2 blocks the call** and feeds stderr back to the agent as the reason. (Gotcha: exit code 1 is treated as a _non-blocking_ error and the action proceeds — policy hooks must use exit 2.)

`.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [{ "type": "command", "command": ".claude/hooks/protect-critical.sh" }]
      }
    ]
  }
}
```

`.claude/hooks/protect-critical.sh` (mark executable):

```bash
#!/bin/bash
# Illustrative only: normalize paths before production use; this forward-slash substring check does not cover traversal or Windows backslashes.
file_path=$(jq -r '.tool_input.file_path // empty')
if [[ "$file_path" == *"/critical/"* ]]; then
  echo "Blocked by policy: files under /critical/ are never edited by the agent." >&2
  exit 2
fi
exit 0
```

Simpler alternative for path-based rules: a `permissions.deny` entry in `.claude/settings.json` (e.g., `"deny": ["Edit(./critical/**)"]`). Hooks are for logic; deny rules are for static paths. On Windows, prefer the deny rule for static paths; the illustrative Bash hook also requires Bash and `jq`.

**Codex / other agents:** the equivalents are sandbox and approval-mode configuration, read-only mounts, restricted credentials, and CI checks that reject violating changes. The principle is identical: **Never-do tiers live in the tool layer, not the prompt layer.**

---

## Part 4 — Build Recipes

**Meta-rule:** apply the three layers to your _own_ generation of these artifacts. Interview before writing (Layer 1), state criteria for the artifact and check the artifact against them before presenting (Layer 2), and place the artifact correctly in the environment (Layer 3).

### 4.1 When asked to build a prompt

1. If the goal isn't explicit, run the goal interview (1.1). Extract: the driving decision, consumers, done-state, cost-of-error, constraints, reference examples.
2. Structure the prompt in this order: **goal → context \& inputs → scope (one increment) → key decisions to confirm → evaluation criteria → verification plan → checkpoint instruction**.
3. Include the relevant fragments from Appendix A verbatim where they fit.
4. **Acceptance check before presenting** — the prompt must pass all of:

   - [ ] Goal stated as the decision it drives, not as a task
   - [ ] Scope is a single reviewable increment
   - [ ] Key decisions and assumptions surfaced for explicit confirmation
   - [ ] Criteria are precise and checkable (no "looks good")
   - [ ] A verification plan with at least one ground-truth source where any exists
   - [ ] A checkpoint instruction (what to show, when)

### 4.2 When asked to build a CLAUDE.md / AGENTS.md

1. Interview the human: What is this workspace? Where does knowledge live, and what is authoritative? What gets done repeatedly here (→ candidate skills)? What is expensive or irreversible to get wrong (→ tier assignments)?
2. Generate using the five-section skeleton in 3.1.
3. Sort every rule into a permission tier. For each **Never do**, draft the corresponding tool-level enforcement (hook, deny rule, or sandbox setting) alongside the file — do not ship a prose-only "never."
4. Prefer pointers into the knowledge base over inlined content; flag any section over a few lines as a candidate for extraction into `knowledge/`.
5. **Acceptance check** — every line actionable; no aspirational filler; tiers complete; each Never-do has named enforcement; total length readable in full every session.

### 4.3 When asked to build a skill

1. Confirm repetition: expected to run ≥2–3 times? If not, deliver a prompt instead and say why.
2. Extract the workflow — from the current conversation history if the human says "turn this into a skill" (tools used, step sequence, corrections made, formats observed), otherwise by interview: trigger contexts, expected output format, edge cases, dependencies.
3. Write the SKILL.md: frontmatter `name` + a deliberately pushy `description` carrying all trigger conditions; body with steps; an embedded verification section (criteria + ground-truth checks + known failure modes); examples. Push deterministic steps into `scripts/`, bulky documentation into `references/`.
4. Register it in the memory file's skills-routing section (3.1, section 3).
5. Schedule the leak test: after the first real use, collect failures and patch. State this explicitly when delivering the skill.
6. **Acceptance check** — description contains every trigger condition; body under \~500 lines; verification embedded; routed in the memory file; iteration step communicated.

### 4.4 When executing any multi-step build

Run the loop, never the waterfall:

> **spec** (smallest reviewable increment) → **confirm decisions** with the human → **execute** the increment → **verify** against criteria + ground truth → **checkpoint** with the human → **repeat**.

If at any point you are about to proceed on an unconfirmed assumption with non-trivial cost-of-error: stop and surface it. That pause _is_ the method.

---

## Appendix A — Reusable Prompt Fragments

Literal lines, ready to embed in prompts, specs, and memory files:

- **Goal:** "Before doing anything, interview me to identify the goal of this project — the decision the output should drive, not the task."
- **Scope:** "Bias toward smaller, more compartmentalized specs. Propose the smallest increment that produces something reviewable."
- **Decisions:** "Make me verify key decisions explicitly to ensure nothing is missed."
- **Criteria:** "Before starting, outline the evaluation criteria you will use to ensure a high-quality final product. Be precise."
- **Cross-check:** "If this turns into a complex build, run the final output past [second model] and reconcile any disagreement before presenting."
- **Ground truth:** "Identify what external signal (tests, deploy status, reference documents, historical examples, live data) can verify this output, and use it before declaring success."
- **Environment audit:** "Audit my CLAUDE.md, knowledge base, skills, and guardrails against the three-layer model (spec / verifier / environment) and propose the highest-leverage fixes, ordered by cost-of-error."

---

## Appendix B — The One-Sentence Summary

The human supplies understanding; the spec encodes it; the verifier checks against it; the environment makes all three compound — and nothing in any layer is a substitute for the understanding itself.
