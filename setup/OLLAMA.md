# Ollama Model Backend

Ollama is an optional local model backend for `B*` / `model-*` operations. It can provide:

- embeddings for `E3` chunk embeddings and `E4 --semantic`;
- advisory text via `B2 model-run`.

With nothing configured, the harness stays deterministic and local: recursive chunking plus lexical search, no model network calls.

## Install

1. Install Ollama from <https://ollama.com/download>.
2. Pull the default Agent Kaizen models with the helper:

```powershell
setup\install-ollama.ps1
```

```sh
bash setup/install-ollama.sh
```

The helper points `OLLAMA_MODELS` at `$DEVROOT/Ollama/models`, so weights live outside the repo.

## Dry Runs

```powershell
setup\install-ollama.ps1 -ListSteps
setup\install-ollama.ps1 -PlanOnly -NoNetwork -NoExternalActions -NoUserEnvWrites
setup\install-ollama.ps1 -SelfTest -NoNetwork -NoExternalActions -NoUserEnvWrites
```

```sh
bash setup/install-ollama.sh --list-steps
bash setup/install-ollama.sh --plan-only --no-network --no-external-actions --no-user-env-writes
bash setup/install-ollama.sh --self-test --no-network --no-external-actions --no-user-env-writes
```

Command logs and setup state live under `$DEVROOT/agent-kaizen-setup/`.

## Configure

```text
KAIZEN_EMBED_MODEL=nomic-embed-text
KAIZEN_LLM_MODEL=llama3.2
KAIZEN_EMBED_BASE_URL=http://127.0.0.1:11434/v1
KAIZEN_LLM_BASE_URL=http://127.0.0.1:11434/v1
```

Remote OpenAI-compatible endpoints can be used by setting the corresponding `*_BASE_URL` and `*_API_KEY` environment variables. API keys are read from the environment only and are never stored.

One embedding model should be used per corpus because vector dimensions must stay consistent. Switching models requires `B3 reembed`.

## Verify

```powershell
python kaizen.py B1 --json
python kaizen.py E3 --id <doc-id> --json
python kaizen.py E4 --query "..." --semantic --json
python kaizen.py B3 --json
python kaizen.py B2 --prompt "Summarize ..." --json
```
