# support_scripts

Tracked **auxiliary** helper scripts for the Agent Kaizen harness. Generated output, fetched data,
backups, and temporary files belong under gitignored `AI/support_scripts_work/` or a task folder
under `AI/work/`.

> The Kaizen harness CLI (`kaizen.py`), its engine package (`kaizen_components/`), and the pinned
> dependencies (`requirements-kaizen.txt`) live at the **repository root**, not here. See the root
> [`README.md`](../README.md) for the command index and setup. The scripts below are standalone
> utilities; only `bench_kaizen.py` imports the engine (in-process, by design — it benchmarks it).

## `bench_kaizen.py` - Repeatable harness benchmarks

Times the real CLI code path in-process against an isolated `KAIZEN_REPO_ROOT` temp root — the
real `AI/db/` is never read or written, and nothing touches the network. Four phases: write-op
latency, `R0`/`X5` read-back at 100/1k/5k seeded records, `E1`→`E3`→`E4` retrieval with a top-k
hit-rate correctness gate, and context recovery (the `R0` digest versus the text content of this
project's median local session transcript — aggregate sizes only, content never leaves the
machine). Regenerates [`docs/BENCHMARKS.md`](../docs/BENCHMARKS.md), `docs/benchmarks.json`, SVG
charts under `docs/images/` (they render everywhere, including previews that cannot draw
Mermaid), and the `BENCHMARKS` + `BENCH-TEASER` marker regions in the root README.

Usage:

```powershell
python support_scripts/bench_kaizen.py
python support_scripts/bench_kaizen.py --quick --out AI/support_scripts_work/bench
python support_scripts/bench_kaizen.py --from-json docs/benchmarks.json
python support_scripts/bench_kaizen.py --allow-semantic
```

`--quick` is the tiny variant the test suite runs (`AI/tests/test_bench_smoke.py`), and it never
writes into the repo; `--from-json` re-renders all artifacts from a saved results file after a
renderer change; `--allow-semantic` keeps your embedding-backend env vars and also times
`E4 --semantic` when a backend responds.

## `yt_transcript.py` - Pull YouTube-provided transcripts

Fetches the caption tracks YouTube serves to its own player and saves them as timestamped JSON. It is
standard-library only and uses no official API key.

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

Scans coding-agent session transcripts (Claude Code JSONL; defaults to this repo's own project
directory under `~/.claude/projects/`) for recurring friction — failed tool calls grouped by
normalized error signature, plus user-correction messages — and drafts GOTCHA candidates for
human review. It is read-only on transcripts, scrubs personal home paths from drafts, and
**never writes the Kaizen DB**: promotion happens through the normal `G1` write gate.

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

This predates the Kaizen DB migration. Prefer `kaizen.py M*`, `G*`, and `L*` commands for new work.
The script remains available for restoring or inspecting legacy backups.

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
