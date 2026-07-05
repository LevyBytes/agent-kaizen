# PyTorch Backend

PyTorch is an opt-in extra for in-process `sentence-transformers` embeddings and semantic chunking. It is intentionally outside the lightweight core install because `torch` plus model weights can be large.

The installer uses the Agent Kaizen shared venv by default:

```text
$DEVROOT/Python/venvs/kaizen
```

Pass `-PythonExe` / `--python-exe` only when you deliberately want to install into a different interpreter.

## Install

```powershell
setup\install-pytorch.ps1            # CPU torch
setup\install-pytorch.ps1 -Gpu       # CUDA torch wheel
```

```sh
bash setup/install-pytorch.sh        # CPU torch
bash setup/install-pytorch.sh --gpu  # CUDA torch wheel
```

The installer redirects the HuggingFace cache to `$DEVROOT/models` via `HF_HOME`, so multi-GB model weights stay outside this repo. First use of a model may still download weights from HuggingFace.

## Dry Runs

```powershell
setup\install-pytorch.ps1 -ListSteps
setup\install-pytorch.ps1 -PlanOnly -NoNetwork -NoExternalActions -NoUserEnvWrites
setup\install-pytorch.ps1 -SelfTest -NoNetwork -NoExternalActions -NoUserEnvWrites
```

```sh
bash setup/install-pytorch.sh --list-steps
bash setup/install-pytorch.sh --plan-only --no-network --no-external-actions --no-user-env-writes
bash setup/install-pytorch.sh --self-test --no-network --no-external-actions --no-user-env-writes
```

Command logs and setup state live under `$DEVROOT/agent-kaizen-setup/`.

## Configure

```text
KAIZEN_EMBED_BACKEND=sentence-transformers
KAIZEN_EMBED_MODEL=all-MiniLM-L6-v2
HF_HOME=$DEVROOT/models
```

One embedding model should be used per corpus because vector dimensions must stay consistent. Switching models requires `B3 reembed`.

## Verify

```powershell
python kaizen.py B1 --json
python kaizen.py E3 --id <doc-id> --chunker semantic --json
python kaizen.py E4 --query "..." --semantic --json
python kaizen.py B3 --json
```
