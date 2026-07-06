# A Better Skill Framework

### Deterministic documentation → gold-standard agent skills

A concrete, reproducible method for turning a body of documentation into a **gold-standard agent
skill**: a focused, index-navigated, verifiable reference package an AI agent (Claude Code, Codex,
or similar) can load on demand. This document is the authoritative spec for the framework, the
_shape_ of a skill, the _pipeline_ that produces one, and the _tooling_ that enforces the standard.

It is the **Environment** layer of the
[Three-Layer Scaffolding Method](three-layer-scaffolding-method-for-LLM-LRE-FIM-integration.md)
(Spec → Verifier → Environment) made concrete: skills are the compounding, reusable artifacts that
make every future session start stronger, and the validator is their Verifier.

---

## Part 0 — Why skills, not RAG

Most "LLM + documents" setups are retrieval-augmented generation: dump files in, retrieve chunks at
query time, re-derive an answer every time. Nothing accumulates. A **skill** is the opposite: the
knowledge is **compiled once** into a structured, navigable package — a trigger-rich entry point, a
catalog, and right-sized reference files — and then reused. At the scale of one documentation source
(hundreds of pages), an **index file beats an embedding index**: the agent reads the catalog, opens
the one file it needs, and stops. No vector store, no re-ranking, no per-query rediscovery.

---

## Part 1 — The layers of a skill's "source of truth"

A shipped skill sits on top of three conceptual layers:

| Layer                          | What it is                                                                                                              | Owner    |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------- | -------- |
| **Raw upstream docs**          | The publisher's docs (manual, API reference, book). Immutable; read-only.                                               | upstream |
| **The recontextualized skill** | Our owned, compiled reference — the skill's **source of truth**; right-sized, rewritten in our words.                   | us       |
| **Navigation surface**         | `SKILL.md` + `references/INDEX.md` (+ `topics.json`): the trigger, the catalog, the "how it fits together." Read first. | us       |

Two production modes feed the middle layer:

- **Verbatim** — a faithful, deterministic reproduction of the upstream docs (light cleanup only).
  Useful for private, local reference where exact fidelity matters and licensing is not a concern.
- **Recontextualized** — the verbatim capture rewritten/restructured in our own words so it can be
  shipped publicly. The recontextualized package becomes the skill's source of truth; the original
  publisher's docs remain the raw upstream it was derived from.

---

## Part 2 — The gold-standard package

Every skill conforms to this layout. The validator
(`skill-drafting/scripts/validate_skill_package.py`) enforces it.

### 2.1 Flat skill

```
<skill-name>/
├── SKILL.md            ← entry point (always loaded as metadata; body loads on trigger)
├── GOTCHA.md           ← sibling: recurring failure modes + what to do instead
└── references/
    ├── INDEX.md        ← human catalog of every file, grouped by section
    ├── topics.json     ← machine-readable routing (array shape, below)
    └── <topic>.md …    ← one focused subject per file, ≤ ~24 KB
```

### 2.2 Router skill (large/multi-area sources)

```
<skill-name>/
├── SKILL.md            ← top-level router: routes to the sub-skills below
├── GOTCHA.md
└── <area>/             ← one sub-skill per area, each a full flat skill
    ├── SKILL.md
    ├── GOTCHA.md
    └── references/ …
```

### 2.3 `SKILL.md`

- **Frontmatter:** `name` (must equal the folder name) and a **trigger-rich `description`** — it
  carries _all_ the "when to use this" signals (name the task and the concrete phrases, contexts,
  and file types that should invoke it, including cases where the user never says the skill's name).
  The description is the trigger; agents under-trigger skills, so it should be deliberately pushy.
- **Body sections** (lean; the depth lives in `references/`): `# Title`; `## When to use this`;
  `## Workflow` (flat) or `## Task router` / `## Routes` (router); `## Gotchas` → points at the
  sibling `GOTCHA.md`; `## References`; `## Source`; `## Verification`.

### 2.4 `GOTCHA.md`

A sibling file of recurring failure modes and what to do instead — version skew, exact-identifier
discipline, format caveats, where to grep. Read alongside `SKILL.md`.

### 2.5 `references/topics.json` (array shape)

```json
{
  "schema_version": 1,
  "topics": [
    {
      "topic": "Display name",
      "file": "references/<file>.md",
      "summary": "One line.",
      "keywords": ["kebab-topic", "section"]
    }
  ]
}
```

### 2.6 Right-sizing

Every reference file is **≤ ~24 KB**. Oversize sources are split deterministically at heading (and
blank-line) boundaries; a single atomic unit that cannot split further (e.g. one large table) is
allowed as a noted outlier. Right-sizing keeps each file loadable in one read.

---

## Part 3 — The deterministic pipeline

```
source → ingest → corpus JSONL → build → finalize → validate
```

All stages are deterministic and stdlib-Python + a few external binaries (`curl`, and for PDFs
`pdftotext`/`qpdf`) — no per-document LLM calls in the verbatim path. The scripts live in
`skill-drafting/scripts/`.

### 3.1 Corpus record

One JSONL record per chunk:

```json
{
  "chunk_id": "...",
  "title": "...",
  "source_url": "...",
  "text": "<markdown>",
  "tags": [],
  "subskill": "<optional, router>",
  "section": "<optional>"
}
```

### 3.2 Ingest (source → corpus), by source type

`skill_builder.py ingest <format>` turns source docs into a corpus JSONL. Formats:

- **`ingest mdbook`** — an mdBook `print.html` (one fetch = the whole book), split by chapter.
- **`ingest html`** — crawled HTML pages / a docs bundle; one chunk per page (optional `section`
  drives a router).
- **`ingest rustdoc`** — generator-rendered API pages (e.g. rustdoc): extract the content section,
  drop auto-generated boilerplate sections, convert to Markdown. The content selector, the dropped
  section ids, and the stripped chrome label are options (defaults reproduce rustdoc), so the same
  command works for any similarly structured generator.
- **`ingest pdf`** — born-digital PDFs via `pdftotext` (text) + `qpdf` (outline/bookmarks for
  chapter structure); strips running headers/footers and printed-TOC leaders, de-hyphenates,
  promotes outline titles to headings.

All HTML formats share one deterministic HTML→Markdown converter (headings, fenced code with
language, tables, lists, links, images-as-captions; skips chrome).

### 3.3 Build, finalize, validate

These are `skill_builder.py` subcommands (the validator is a separate script):

- **`build`** — assembles the corpus into right-sized reference files using a
  size-balanced partition over the source's own hierarchy, generates `INDEX.md` + `topics.json`, and
  emits a starter `SKILL.md`. **Fence-aware** (a clean ` ``` `/`~~~` delimiter toggles code state; a
  stray `~~~`-prefixed content line such as a traceback caret does not). The **`--verbatim`** preset
  turns off link-rewriting, table compaction, identifier-backticking, and prettier, names files from
  the page slug, and drops 2+digit ordering prefixes from display titles; the default (generic)
  mode keeps all cleanup on.
- **`finalize`** — rewrites `SKILL.md` to the gold sections and writes the sibling
  `GOTCHA.md`, driven by a small per-source meta JSON (`name`, `title`, `description`, `when_to_use`,
  `source_url`, `verbatim`, `gotchas`, `router`, `subskills`). For routers it finalizes every
  sub-skill. The generated Verification command points at the skill's own path (and `--validator`
  overrides the validator path) — no mirror is assumed.
- **`split`** — splits any oversize reference file in place at heading boundaries.
- **`validate_skill_package.py`** — the quality gate (a separate script): `name` == folder,
  trigger-rich description, required sections, sibling `GOTCHA.md`, array-shape `topics.json`, file
  references resolve. Routers validate with `--package`.

### 3.4 Example (verbatim build)

```bash
python skill_builder.py ingest html --manifest m.tsv --src-dir src --skill my-skill --out corpus.jsonl
python skill_builder.py build --records corpus.jsonl --out path/to/my-skill \
    --name my-skill --description "<trigger-rich>" --verbatim --verify
python skill_builder.py finalize --skill path/to/my-skill --meta meta/my-skill.json
python validate_skill_package.py path/to/my-skill        # routers: --package
```

---

## Part 4 — Maintaining already-shipped skills (in-place, no re-render)

A shipped skill's source corpus is often gone, and its `SKILL.md` is frequently **hand-authored and
bespoke**. Such skills are therefore **not re-rendered** (rebuilt from corpus). Instead, an in-place
maintenance pass edits the _built_ artifacts directly:

- regenerate the **generated** `INDEX.md` / `topics.json` by scanning `references/*.md`, only where
  the file set changed (minimal churn);
- split oversize files in place;
- validate;
- **leave a bespoke `SKILL.md` / `GOTCHA.md` untouched.**

The same gold-standard operations (oversize split, INDEX/topics generation) are shared between the
builder (new skills) and the in-place tool (existing skills) — one concept, two delivery vehicles.

### Mirror discipline

Skills are mirrored: `.agents/skills/<s>` (Codex/Open Agent) and `.claude/skills/<s>` (Claude). Any
change is applied to **both** mirrors and verified identical (`diff -rq`).

---

## Part 5 — Quality gates

- `skill_builder.py build --verify`: file count, size distribution, residual raw-image/relative-link
  and prose-HTML checks.
- `validate_skill_package.py`: structural gold-standard conformance (PASS/FAIL).
- Spot-check: a reference file reads clean (entities/code intact, no chrome, no page litter).
- Mirror parity: `.agents` vs `.claude` identical.

---

## Part 6 — Roadmap

### Implemented

- **Internal cross-linking** — turns the flat catalog into a navigable web (a navigation/efficiency
  benefit, not a ranking one). Build-time: the builder resolves in-skill links to local files
  (`RESOLVE_REFS`, on by default). In-place: `skill_builder.py maintain --cross-link` adds a conservative
  `## See also` footer to each reference file (distinctive topic-name matches; already-linked files
  excluded). Richer inline/curated linking and a per-file `Related` column in `INDEX.md` remain a
  later pass.
- **Link/content-lint** — `skill_builder.py lint` (in `skill-drafting/scripts/`, so the tool ships with the repo)
  is a read-only health check (dangling local links, INDEX ↔ topics drift). Each user runs it to produce a
  report at `AI/lint/<skill>.md`, which is **local / gitignored** (per-user, never committed, never shipped
  inside a skill); it also prints a PASS/FAIL summary. (Orphan + routing-term checks are a later refinement.)
- **Master index + `covers:`** — `skill_builder.py index` aggregates every skill in a skills root into a
  top-level `INDEX.md`: a catalog (name + trigger), an entity → skill map (which skill documents an
  entity, and overlaps), and a related-skills list. It reads an optional `covers:` frontmatter list on each
  `SKILL.md` (seed/refresh with `--seed-covers`; routers → area names, flat → top derived entities) and
  otherwise derives covers from `topics.json`. A discovery/audit entry point — **not** auto-routing (agents
  route via descriptions) and **not** a synthesized wiki.

### Planned

- **Tiered multi-source synthesis** — for overlapping domains, synthesize entity/concept pages from
  multiple sources (e.g. official docs enriched by video-transcript/screenshot extraction), with
  **tiered provenance** (official > docs > transcript > screenshot-OCR), deferring to the higher tier
  on conflict and flagging it, with every synthesized claim linking down to its source. One-time
  ingest cost; never lets synthesis replace the authoritative reference.

---

## Appendix — Scripts

All in `skill-drafting/scripts/` (mirrored in `.agents` and `.claude`). The pipeline is one script,
`skill_builder.py`, with a subcommand per stage; the validator and the usage tools stay separate:

| Tool / subcommand                                      | Role                                                                                       |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------ |
| `skill_builder.py ingest {mdbook\|html\|rustdoc\|pdf}` | Source → corpus JSONL, by source type (shared HTML → Markdown converter).                  |
| `skill_builder.py build`                               | Corpus → right-sized references + INDEX/topics + starter SKILL.md (`--verbatim` preset).   |
| `skill_builder.py finalize`                            | Gold `SKILL.md` + `GOTCHA.md` from a per-source meta JSON.                                 |
| `skill_builder.py split`                               | In-place oversize splitting at heading boundaries.                                         |
| `skill_builder.py maintain`                            | In-place gold maintenance for a built skill (audit + split oversize + cross-link).         |
| `skill_builder.py lint`                                | Read-only link/content health check; writes a report to `AI/lint/<skill>.md`.              |
| `skill_builder.py index`                               | Cross-skill master `INDEX.md` (catalog + entity → skill map + related); `covers:` seeding. |
| `validate_skill_package.py`                            | Gold-standard validator (`--package` for routers); a separate script.                      |
