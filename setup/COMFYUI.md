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
python kaizen.py Y1 --template txt2img --workflow-file workflow.json --prompt "..." --json
python kaizen.py Y2 --id <run-id> --json
python kaizen.py Y4 --id <run-id> --json
```

The workflow file is a ComfyUI API-format prompt graph. `--dry-run` validates and records a queued run from the workflow alone; a real run records completed outputs as hashed artifacts under `AI/generation/<template>/`.
