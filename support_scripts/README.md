# Auxiliary Support Scripts

This directory contains optional operator utilities plus one explicit canonical library exception: `skill_management/` is the stdlib-only filesystem authority used by the public `SK1`-`SK5` operations. Generated output, fetched data, backups, and temporary files belong under gitignored `AI/support_scripts_work/` or a task folder under `AI/work/`.

Tests, benchmarks, verification tools, and acceptance launchers belong under [`tests/`](../tests/). Product runtime other than the narrowly scoped skill-management library belongs under [`kaizen_components/`](../kaizen_components/), and setup or installer helpers belong under [`setup/`](../setup/). Do not add other source classes here.

## Inventory

- `README.md` — this auxiliary inventory and placement contract.
- `gotcha_wipe.py` — explicit legacy backup-and-wipe utility for GOTCHA records.
- `mine_transcripts.py` — read-only transcript miner that drafts human-reviewed GOTCHA candidates.
- `model-monitor.cmd` — one-click Windows launcher for the native backend monitor.
- `model_monitor_gui.py` — operational GPU and model monitor with read-only automatic polling and a noninteractive `--once` probe.
- `seed_model_candidates.py` — explicit model-candidate source-lock seeder.
- `skill_manager.py` and `skill_management/` — stdlib-only project/store skill inventory, package validation, link reconciliation, rich indexes, and project-local Claude policy with hash-confirmed writes.
- `yt_transcript.py` — opt-in network utility for downloading YouTube-provided caption tracks.

## Skill management

Every inventory, validation, status, and plan operation is read-only. Mutating operations recompute the deterministic plan and require its exact `plan_sha256`; the manager never searches a user home directory. Link operations require one or more explicit `--skill` values, reject real-directory and wrong-target collisions, and never prune implicitly.

Discovery classifies each package as `published` only when its contained local Git configuration has a credential-free standard GitHub HTTPS or SSH remote; every other package is `staged`. Remote values are never returned, no network lookup occurs, and publication does not control local activation. Surface validity and host policy remain independent: Claude has writable `on`, `name-only`, `user-invocable-only`, and `off` project policy, while Codex is audit-only/default-on because it has no supported project-local policy writer.

```powershell
python support_scripts/skill_manager.py --json inventory --store-root D:\dev\SKILLS\skills
python support_scripts/skill_manager.py --json validate D:\dev\SKILLS\skills\skill-drafting
python support_scripts/skill_manager.py --json links plan --store-root D:\dev\SKILLS\skills --skill skill-drafting
python support_scripts/skill_manager.py --json links apply --store-root D:\dev\SKILLS\skills --skill skill-drafting --confirm-plan PLAN_SHA256
python support_scripts/skill_manager.py --json index plan
python support_scripts/skill_manager.py --json policy plan --policy skill-drafting=user-invocable-only
python support_scripts/skill_manager.py --json store-index plan --skills-root D:\dev\SKILLS\skills --seed-covers
```

Project policy applies only to `.claude/settings.local.json`. Codex policy inspection is explicitly audit-only because no supported project-local per-skill invocation-policy writer exists. Public plans expose hashes and operations, never unrelated settings values. A successful policy apply writes its explicit rollback record under `AI/work/skill-management/`; plan and status operations create no reports or scratch files.

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
