# Auxiliary Support Scripts

This directory contains only optional operator utilities that support Agent Kaizen without serving as product runtime, setup, test, verification, acceptance, or benchmark infrastructure. Generated output, fetched data, backups, and temporary files belong under gitignored `AI/support_scripts_work/` or a task folder under `AI/work/`.

Tests, benchmarks, verification tools, and acceptance launchers belong under [`tests/`](../tests/). Product runtime belongs under [`kaizen_components/`](../kaizen_components/), and setup or installer helpers belong under [`setup/`](../setup/). Do not add those source classes here.

## Inventory

- `README.md` — this auxiliary inventory and placement contract.
- `gotcha_wipe.py` — explicit legacy backup-and-wipe utility for GOTCHA records.
- `mine_transcripts.py` — read-only transcript miner that drafts human-reviewed GOTCHA candidates.
- `model-monitor.cmd` — one-click Windows launcher for the native backend monitor.
- `model_monitor_gui.py` — operational GPU and model monitor with read-only automatic polling and a noninteractive `--once` probe.
- `seed_model_candidates.py` — explicit model-candidate source-lock seeder.
- `yt_transcript.py` — opt-in network utility for downloading YouTube-provided caption tracks.

## GOTCHA backup and wipe

Run only when intentionally exporting and clearing GOTCHA records:

```powershell
python support_scripts/gotcha_wipe.py --help
```

## Transcript mining

The miner reads local agent transcripts, writes drafts under auxiliary scratch, and never promotes a candidate automatically:

```powershell
python support_scripts/mine_transcripts.py --help
```

## Backend monitor

Double-click `support_scripts\model-monitor.cmd` for the native PySide6 window, or invoke the GUI directly:

```powershell
support_scripts\model-monitor.cmd
python support_scripts/model_monitor_gui.py --interval 2.0 --limit 8
python support_scripts/model_monitor_gui.py --once
```

Automatic monitoring is read-only and system-wide. It reuses the `B6` data layer, observes GPU processes and Ollama residency, and does not run inference. The explicit emergency-stop action can unload models and terminate displayed GPU processes after confirmation. Synthetic GUI verification belongs in `tests/test_model_monitor_gui.py`; backend test launchers belong under `tests/`.

## Model-candidate seeding

The seeder writes explicit source-lock candidate records; `--test` marks test records but does not make this utility a test runner:

```powershell
python support_scripts/seed_model_candidates.py --help
```

## YouTube transcripts

The transcript utility performs opt-in network access and stores fetched output in the path selected by the operator:

```powershell
python support_scripts/yt_transcript.py --help
```
