# ComfyUI Backend

Agent Kaizen can drive a local ComfyUI server for `Y*` / `comfy-*` workflows and record each run with the workflow graph hash, seed, latency, and generated artifacts.

ComfyUI is optional. The server, its venv, and its models live outside this repo as a `$DEVROOT` sibling:

```text
$DEVROOT/ComfyUI/
```

The installer uses the Agent Kaizen shared Python as the bootstrap interpreter when available, then creates a dedicated ComfyUI venv under `$DEVROOT/ComfyUI/.venv`.

## Install

```powershell
setup\install-comfyui.ps1       # CPU torch
setup\install-comfyui.ps1 -Gpu  # CUDA torch wheel
```

```sh
bash setup/install-comfyui.sh        # CPU torch
bash setup/install-comfyui.sh --gpu  # CUDA torch wheel
```

The installer clones ComfyUI, creates its venv, installs requirements, and prints start commands. It does not download model weights.

## Dry Runs

```powershell
setup\install-comfyui.ps1 -ListSteps
setup\install-comfyui.ps1 -PlanOnly -NoNetwork -NoExternalActions -NoUserEnvWrites
setup\install-comfyui.ps1 -SelfTest -NoNetwork -NoExternalActions -NoUserEnvWrites
```

```sh
bash setup/install-comfyui.sh --list-steps
bash setup/install-comfyui.sh --plan-only --no-network --no-external-actions --no-user-env-writes
bash setup/install-comfyui.sh --self-test --no-network --no-external-actions --no-user-env-writes
```

Command logs and setup state live under `$DEVROOT/agent-kaizen-setup/`.

## Models

Place at least one checkpoint under:

```text
$DEVROOT/ComfyUI/models/checkpoints/
```

Licensing and size make model weights a deliberate manual step.

## Start

```text
$DEVROOT/ComfyUI/.venv/Scripts/python.exe  $DEVROOT/ComfyUI/main.py
$DEVROOT/ComfyUI/.venv/bin/python          $DEVROOT/ComfyUI/main.py
```

## Use

```powershell
python kaizen.py Y1 --template txt2img --workflow-file workflow.json --dry-run --json
python kaizen.py Y1 --template txt2img --workflow-file workflow.json --json
python kaizen.py Y2 --id <run-id> --json
python kaizen.py Y4 --id <run-id> --json
```

The workflow file is a ComfyUI API-format prompt graph. `--dry-run` validates and records a queued run from the workflow alone; a real run records completed outputs as hashed artifacts under `AI/generation/<template>/`.

## Runtime Ops

`Y6` / `comfy-runtime` manages the local server. It never installs anything: `provision` validates the runtime and prints the exact installer command for you to run.

```powershell
python kaizen.py Y6 --action status --json      # runtime + reachability snapshot
python kaizen.py Y6 --action provision --json   # emits the installer command if missing
python kaizen.py Y6 --action start --json       # launch the server (detached)
python kaizen.py Y6 --action doctor --json      # versions, torch/cuda, GPU, checkpoints, disk
python kaizen.py Y6 --action stop --json        # stop the server + remove the pidfile
```

The server pidfile and logs live under `AI/work/comfyui/` (gitignored). On an RTX 5080 (Blackwell) the ComfyUI venv needs a cu128+ torch wheel; `Y6 --action doctor` reports whether CUDA is available inside that venv.

By default the ComfyUI venv sits at `$DEVROOT/ComfyUI/.venv`. To place it elsewhere (e.g. alongside your other venvs at `$DEVROOT/Python/venvs/comfyui`), pass `-VenvPath` to the installer and set `KAIZEN_COMFYUI_VENV` to the same path so the `Y6` ops find it:

```powershell
setup\install-comfyui.ps1 -Gpu -CudaIndex https://download.pytorch.org/whl/cu128 -VenvPath $env:DEVROOT\Python\venvs\comfyui
setx KAIZEN_COMFYUI_VENV $env:DEVROOT\Python\venvs\comfyui
```

The runtime home (the cloned repo + `models/`) stays at `$DEVROOT/ComfyUI`; only the venv moves.

## MCP Lane

`Y7` / `comfy-mcp` treats a local MCP server as a second generation lane, A/B'd against the direct HTTP API. The MCP client is an optional dependency:

```powershell
pip install "mcp>=1.0,<2"     # optional; v2 is a beta and excluded
```

```powershell
python kaizen.py Y7 --action doctor --candidate artokun --json
python kaizen.py Y7 --action bakeoff --json      # pins a winner or records why none passed
```

The bakeoff scores candidates against local-only, loopback, Windows, stdio, submit/status/fetch, and reproducible-install gates, writes a source-lock per candidate plus a markdown report, and pins the winner in `db_settings` (`active_comfy_mcp`). Verified candidates: `artokun/comfyui-mcp` (npm, stdio, Windows), `joenorton/comfyui-mcp-server` (HTTP transport, not stdio), `shawnrushefsky/comfyui-mcp` (not published to npm). The endpoint is always passed explicitly so a candidate's auto-detection never probes off-drive paths.

## Routed Generation And A/B

```powershell
python kaizen.py Y8 --template txt2img --workflow-file wf.json --prompt "a red bicycle" --seed 42 --route api --json
python kaizen.py Y8 --template txt2img --workflow-file wf.json --route mcp --json
python kaizen.py Y9 --template txt2img --workflow-file wf.json --seed 42 --json
```

`Y8` replaces a `{{PROMPT}}` placeholder in the workflow with `--prompt`, overrides every seed with `--seed`, and routes to the API lane or the pinned MCP lane. `Y9` runs the same workflow and seed through both lanes and writes an operational parity report (both completed, output hashes, latency delta) under `AI/generation/reports/`.

## Model Approval Flow

Model weights are never downloaded automatically. Consult the registry, get owner approval, place the file, then validate:

```powershell
python kaizen.py S2 --query model- --json        # registry: candidates + dispositions + license
python kaizen.py S3 --id <lock-id> --json        # full detail for one lock (the id comes from the S2 list)
python kaizen.py Y8 --workflow-file wf.json --validate --json   # denies with the exact missing asset
```

`Y8 --validate` checks node types and referenced checkpoints against the live `/object_info` and denies with the exact missing asset and its expected `models/` subdir before any submit. First live target: `krea/Krea-2-Turbo`; public 12GB-budget fallback for pre-push verification: `Comfy-Org/z_image_turbo`. Place approved checkpoints under:

```text
$DEVROOT/ComfyUI/models/checkpoints/
```
