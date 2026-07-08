# support_scripts

Tracked **auxiliary** helper scripts for the Agent Kaizen harness. Generated output, fetched data, backups, and temporary files belong under gitignored `AI/support_scripts_work/` or a task folder under `AI/work/`.

> The Kaizen harness CLI (`kaizen.py`), its engine package (`kaizen_components/`), and the pinned dependencies (`requirements-kaizen.txt`) live at the **repository root**, not here. See the root [`README.md`](../README.md) for the command index and setup. The scripts below are standalone utilities; `bench_kaizen.py`, `model_monitor_gui.py`, and `run_backend_tests.py` import the engine in-process by design (the benchmark times it, the monitor reuses the `B6` data layer, and the backend-test runner drives the suite + reads the GPU line).

## `bench_kaizen.py` - Repeatable harness benchmarks

Times the real CLI code path in-process against an isolated `KAIZEN_REPO_ROOT` temp root — the real `AI/db/` is never read or written, and nothing touches the network. Four phases: write-op latency, `R0`/`X5` read-back at 100/1k/5k seeded records, `E1`→`E3`→`E4` retrieval with a top-k hit-rate correctness gate, and context recovery (the `R0` digest versus the text content of this project's median local session transcript — aggregate sizes only, content never leaves the machine). Regenerates [`docs/BENCHMARKS.md`](../docs/BENCHMARKS.md), `docs/benchmarks.json`, SVG charts under `docs/images/` (they render everywhere, including previews that cannot draw Mermaid), and the `BENCHMARKS` + `BENCH-TEASER` marker regions in the root README.

Usage:

```powershell
python support_scripts/bench_kaizen.py
python support_scripts/bench_kaizen.py --quick --out AI/support_scripts_work/bench
python support_scripts/bench_kaizen.py --from-json docs/benchmarks.json
python support_scripts/bench_kaizen.py --allow-semantic
```

`--quick` is the tiny variant the test suite runs (`AI/tests/test_bench_smoke.py`), and it never writes into the repo; `--from-json` re-renders all artifacts from a saved results file after a renderer change; `--allow-semantic` keeps your embedding-backend env vars and also times `E4 --semantic` when a backend responds.

## `model_monitor_gui.py` / `model-monitor.cmd` - Native backend-monitor window

A live PySide6 desktop window that monitors what is actually running on the GPU — **system-wide, no configuration**. It shows a GPU header (temp / fan / util / VRAM bar / power) and a **Models running now** panel listing every GPU compute process filtered to AI executables (`python` / `ollama` / `llama-server` / `comfyui` / …, any project) plus every Ollama-resident model with its VRAM and keep-alive, read straight from the driver — so a model launched by any project appears with zero setup. It reuses the `B6` data layer (`kaizen_components.model_monitor.collect`), so the window and `python kaizen.py B6` never diverge, and it is read-only and thermal-safe (the poll loop runs no model inference — it only observes). The terminal equivalent is `python kaizen.py B6 --watch`.

1-click: double-click **`model-monitor.cmd`** (launches via the venv `pythonw`, no console window). Or run it directly:

```powershell
support_scripts\model-monitor.cmd
python support_scripts/model_monitor_gui.py --interval 2.0 --limit 8
python support_scripts/model_monitor_gui.py --once     # one JSON snapshot, no Qt (headless self-test)
python support_scripts/model_monitor_gui.py --smoke    # build offscreen, redraw, exit 0 (Qt gate, no display)
```

A **Recent Kaizen model calls** feed reads this project's DB and shows **every** model lane — `embedding`, `rerank`, `pii`, text-gen `model_call`, and `judge` (each embedder/reranker/PII op writes a lightweight `model_call` trace tagged with its lane). It *accumulates* — it retains every trace it observes for the life of the window, so a mid-run `K7` purge of `is_test` rows (as `run-backend-tests` does) never blanks it out. On Windows WDDM, `nvidia-smi` reports per-process VRAM as `[N/A]` (shown as `VRAM n/a`); aggregate VRAM and per-Ollama-model VRAM are still exact. For a config check of *this* repo's backends specifically, use `python kaizen.py B6 --probe` / `B1` on the CLI — the window itself stays a pure observer. To force a **sustained, visible** load into this window, run `python kaizen.py B6 --probe --hold 30` (it loads every configured backend and holds them resident for 30s).

## `run_backend_tests.py` / `run-backend-tests.cmd` / `run-backend-tests.sh` - Run only the backend-model tests

Runs just the model-implementation tests (embedder, reranker, PII, and the Qwen judge, plus the default-pin guardrail and the `is_test`/`K7` machinery) — not the whole harness suite. With live tests enabled it: (1) runs a **preflight** that prints a PASS/skip line per prerequisite (torch/sentence-transformers/transformers/gliner2 extras, `HF_HOME`, GPU, and whether Ollama is serving the Qwen judge) so a no-op run explains itself; (2) **auto-launches the monitor GUI** so it is open for the whole run; (3) runs the tests — `test_model_integration` (the live models: every backend-plan op × its real model, writing `--test` rows) **first**, and `test_purge_test` **last**; (4) holds all four backends resident for ~30s — the reliable "watch it load" moment, because each per-op test load is too brief for a 2s poll to catch; and (5) runs a final **`K7`** that purges any `is_test` rows the live tests wrote to the **real DB** (each test also self-cleans in `tearDownClass`, so this is a belt-and-suspenders guarantee). The held backends free themselves on exit (Ollama evicted, torch cache emptied). It also prints a GPU line before/after so the thermal envelope is visible.

Note on what actually touches the GPU: `test_model_integration` exercises the real backends, but the embedder/reranker/PII load in-process only for a few seconds per op, and the **judge runs in the Ollama server** (it shows in the monitor's Ollama panel, not as a python process). Everything else is static/mock/deny/unit and loads no weights. The sustained end-of-run probe (or `python kaizen.py B6 --probe --hold 30`) is the dependable way to see a load.

`run_backend_tests.py` is cross-platform; the `.cmd` (Windows) and `.sh` (Linux/macOS) wrappers just locate the project venv's Python and forward args. On Linux, per-process VRAM comes straight from `nvidia-smi`, and the GUI is skipped automatically on a headless host (no `$DISPLAY`).

```powershell
support_scripts\run-backend-tests.cmd                     # Windows launcher
python support_scripts/run_backend_tests.py
python support_scripts/run_backend_tests.py --no-live    # skip the weight-loading live tests (fast, no GPU/heat)
python support_scripts/run_backend_tests.py --no-gui      # don't auto-launch the monitor GUI
python support_scripts/run_backend_tests.py --no-hold     # skip the sustained end-of-run held probe
python support_scripts/run_backend_tests.py -k judge      # only files whose name contains 'judge'
```

```sh
bash support_scripts/run-backend-tests.sh                 # Linux/macOS launcher (same flags)
$DEVROOT/Python/venvs/kaizen/bin/python support_scripts/run_backend_tests.py --no-live
```

Requires the opt-in extras (`requirements-pytorch.txt`) and `HF_HOME` set to the weight cache (see [`setup/PYTORCH.md`](../setup/PYTORCH.md)); the judge needs Ollama serving the GGUF model (see [`setup/OLLAMA.md`](../setup/OLLAMA.md)). The full harness/framework suite is separate — `python -m unittest discover -s AI/tests` — and is not needed to validate the models.

## `yt_transcript.py` - Pull YouTube-provided transcripts

Fetches the caption tracks YouTube serves to its own player and saves them as timestamped JSON. It is standard-library only and uses no official API key.

Default output:

```text
AI/support_scripts_work/transcripts/
```

Usage:

```powershell
python support_scripts/yt_transcript.py dQw4w9WgXcQ
python support_scripts/yt_transcript.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
python support_scripts/yt_transcript.py <id-or-url> --list
python support_scripts/yt_transcript.py <id-or-url> --print
python support_scripts/yt_transcript.py <id-or-url> --out-dir AI/work/yt
```

Keep transcripts in gitignored local space unless the user explicitly approves another destination.

## `mine_transcripts.py` - Draft GOTCHA candidates from session transcripts

Scans coding-agent session transcripts (Claude Code JSONL; defaults to this repo's own project directory under `~/.claude/projects/`) for recurring friction — failed tool calls grouped by normalized error signature, plus user-correction messages — and drafts GOTCHA candidates for human review. It is read-only on transcripts, scrubs personal home paths from drafts, and **never writes the Kaizen DB**: promotion happens through the normal `G1` write gate.

Default output:

```text
AI/work/transcript-mining/gotcha-candidates-<stamp>.md   (+ .json)
```

Usage:

```powershell
python support_scripts/mine_transcripts.py
python support_scripts/mine_transcripts.py --dir path\to\transcripts --min-count 3 --limit 10
python support_scripts/mine_transcripts.py --include-subagents --json
```

## `gotcha_wipe.py` - Legacy GOTCHA backup/wipe utility

This predates the Kaizen DB migration. Prefer `kaizen.py M*`, `G*`, and `L*` commands for new work. The script remains available for restoring or inspecting legacy backups.

Usage:

```powershell
python support_scripts/gotcha_wipe.py wipe --dry-run
python support_scripts/gotcha_wipe.py wipe --yes --json
python support_scripts/gotcha_wipe.py restore AI/support_scripts_work/gotcha-backups/<timestamp> --yes --json
python support_scripts/gotcha_wipe.py list
```

Backups default to:

```text
AI/support_scripts_work/gotcha-backups/
```

## `seed_model_candidates.py` - Seed the model-candidate registry

Reads `evals/model-candidates.json` (the vetted text-to-image, 3D, and backend candidates — each with license, pinned commit, disposition, and notes) and records one source lock per entry via `S1`, skipping any `source_id` already present so re-runs are idempotent. `--dry-run` prints the planned `S1` calls without writing; `--test` marks the rows `is_test` so `K7` can purge them.

Usage:

```powershell
python support_scripts/seed_model_candidates.py --dry-run
python support_scripts/seed_model_candidates.py
python support_scripts/seed_model_candidates.py --test
```

## `comfy_live_verify.py` - End-to-end ComfyUI harness check

Drives the whole `Y6`-`Y9` ComfyUI flow against an already-running local runtime and prints PASS/FAIL per step: `Y6 --action doctor`, the `Y8 --validate` approval gate, a live `Y8 --route api` run, and `Y2` inspection; with `--mcp` it also runs the `Y7` bakeoff, an `Y8 --route mcp` run, and the `Y9` api-vs-mcp parity pair. It installs nothing (start the runtime first with `Y6 --action start`), writes only `--test` records, and purges them at the end unless `--keep` is passed.

Usage:

```powershell
python support_scripts/comfy_live_verify.py
python support_scripts/comfy_live_verify.py --mcp
python support_scripts/comfy_live_verify.py --keep
```
