# ComfyUI backend (Y\* / `comfy-*`)

Agent Kaizen can drive a local **ComfyUI** server to generate assets (images, diagrams, proof
visuals) and record each run reproducibly. ComfyUI is an **optional, capability-activated** backend:
the external server is the only dependency (the Kaizen client is stdlib `urllib`), so when no server
is reachable the ops fail gracefully with `DENIED_BACKEND_UNAVAILABLE` instead of crashing.

## Execution contract

- **Localhost by default** (`http://127.0.0.1:8188`); override with `--endpoint` or `KAIZEN_COMFYUI_URL`.
  A non-loopback endpoint is an explicit opt-in and is flagged in the run record.
- **Never auto-fired from a skill** — `comfy-run` is an explicit agent/user command.
- The server, its venv, and its models live **outside this repo**, as a `$DEVROOT` sibling
  (`$DEVROOT/ComfyUI/`), so multi-GB weights are never tracked by git.
- Generated outputs land in `AI/generated/<template>/` (gitignored); each is registered as an
  artifact with a sha256. The workflow JSON is stored as an artifact too, so any run is replayable.

## Install (guided)

From the repo root, run the installer for your OS (it clones ComfyUI as a `$DEVROOT` sibling, makes a
venv, installs requirements with a CPU/GPU branch, and prints the start command — it never
auto-downloads model weights):

```powershell
# Windows / PowerShell
setup\install-comfyui.ps1            # CPU by default; add -Gpu for a CUDA wheel
```

```bash
# macOS / Linux
bash setup/install-comfyui.sh        # CPU by default; pass --gpu for a CUDA wheel
```

Then add `$DEVROOT/ComfyUI` to your local VS Code multi-root workspace if you want it in the Explorer
(it stays out of the repo).

## Models

Place at least one checkpoint under `$DEVROOT/ComfyUI/models/checkpoints/` (e.g. an SD/SDXL
`.safetensors`). Licensing and size make this a deliberate, manual step — the installer does not fetch
weights for you.

## Start the server

```text
$DEVROOT/ComfyUI/.venv/Scripts/python.exe  $DEVROOT/ComfyUI/main.py     # Windows
$DEVROOT/ComfyUI/.venv/bin/python          $DEVROOT/ComfyUI/main.py     # POSIX
```

It serves the API + web UI at `http://127.0.0.1:8188`.

## Verify + use

```bash
python kaizen.py Y5 --json                                   # comfy-doctor: probe the endpoint
python kaizen.py Y1 --path workflow.json --template hero --dry-run --json   # record a queued run, NO network
python kaizen.py Y1 --path workflow.json --template hero --json             # submit + record a real run
python kaizen.py Y3 --json                                   # list recent runs
python kaizen.py Y2 --id <run-id> --json                     # inspect one run
python kaizen.py Y4 --id <run-id> --json                     # replay a prior run's stored workflow
```

The workflow file is a ComfyUI **API-format prompt graph** (the JSON you get from the ComfyUI web UI
via *Save (API Format)*). `--dry-run` validates and records a `queued` run from the workflow alone
(no server needed) — useful on machines without a GPU. A real run records `status=completed`, the
graph hash, seed, checkpoints, latency, and every output as a hashed artifact under
`AI/generated/<template>/`.
