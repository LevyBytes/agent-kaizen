# AI/work/ — agent scratch (gitignored)

This is gitignored scratch space for AI agents. Only this `README.md` and the per-folder `.gitignore` files are
tracked and published; everything else under `AI/work/` is local-only — the handoff ledger and gotcha-candidates
file persist locally but stay gitignored, while task folders, `tmp/`, and `tests/` are disposable. The rule that
decides what belongs here versus elsewhere:

## Durable vs scratch — the routing rule

- **Durable / user-facing → `user/`** (not here): plans, specs, handoffs, given/generated prompts, result logs,
  deliverables. See [`user/README.md`](../../user/README.md). Plans go to `user/plans/`.
- **Agent scratch → `AI/`** (here and its siblings): throwaway work, tests, caches, generated output, lint reports.

Save durable user-facing artifacts to `user/`, **never** to `AI/work/`.

## Layout of this folder

- **`AI/work/<task-slug>/`** — one folder per task. Everything for a task (notes, intermediate files, fixtures,
  one-off scripts) lives inside its own folder. Delete the folder when the task ships. **No loose files at the
  `AI/work/` root.**
- **`AI/work/tmp/`** — caches, downloads, throwaway temp; clear anytime. Created on demand.
- **`AI/work/tests/`** — test runs / verification artifacts; auto-cleaned. Created on demand. Python tests are
  discovered with `python -m unittest discover -s AI/work/tests`.
- **`AI/work/build-ledger.md`** — cross-agent handoff ledger; read at session start, update after each milestone
  with a `YYYY-MM-DD HH:mm:ss -` log prefix. Stays at the `AI/work/` root.
- **`AI/work/GOTCHA-candidates.md`** — local-only candidate pitfalls; log here instead of editing `GOTCHA.md`
  unless the user explicitly asks. Stays at the `AI/work/` root.

## Sibling scratch areas (under `AI/`)

- **`AI/support_scripts_work/`** — scratch for developing the tracked helper scripts in `support_scripts/` (tests, temp,
  fetched data, backups). Keep `support_scripts/` itself clean — only shipped scripts + its README.
- **`AI/generation/`** — generated output.

These conventions, and the rest of the operating model, are described in [`SETUP.md`](../../setup/SETUP.md).
