# user/ — durable, user-facing artifacts (gitignored, local-only)

This folder is the home for durable, user-facing artifacts. It is gitignored (local-only) but **organized**: the
folder structure and the per-folder `.gitignore` files are tracked so the layout travels with the repo, while the
contents stay on your machine.

Save durable user-facing artifacts here, **never** in `AI/work/` (which is disposable agent scratch — see
[`AI/work/README.md`](../AI/work/README.md)).

## Layout

- **`user/plans/`** — implementation plans and specs you want to keep (e.g. the plan behind a multi-step build).
- **`user/prompts-given/`** — prompts the user handed to an agent.
- **`user/prompt-results-logs/`** — logged outputs / result records from prompt runs.
- **`user/user-generated-prompts/`** — prompts produced by the prompt-builder scripts in
  `prompt-builder-scripts/` (the scripts already write here).

Each subfolder carries its own `.gitignore` (ignore contents, keep the folder + its `.gitignore`). The full
operating model is in [`SETUP.md`](../setup/SETUP.md).
