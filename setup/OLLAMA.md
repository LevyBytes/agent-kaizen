# Ollama Model Backend

Ollama is an optional local model backend for `B*` / `model-*` operations. It can provide:

- embeddings for `E3` chunk embeddings and `E4 --semantic`;
- advisory text via `B2 model-run`;
- advisory LLM-as-judge scoring against a rubric via `B4 model-judge` / `O4 lab-evaluate` (a signal, never a gate).

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
KAIZEN_EMBED_MODEL=hf.co/mradermacher/KaLM-embedding-multilingual-mini-instruct-v2.5-GGUF:Q8_0
KAIZEN_LLM_MODEL=hf.co/unsloth/Qwen3.5-9B-GGUF:Q4_K_M
KAIZEN_EMBED_BASE_URL=http://127.0.0.1:11434/v1
KAIZEN_LLM_BASE_URL=http://127.0.0.1:11434/v1
```

Remote OpenAI-compatible endpoints can be used by setting the corresponding `*_BASE_URL` and `*_API_KEY` environment variables. API keys are read from the environment only and are never stored.

Each model embeds into its own vector space, so vectors from different models are not comparable and a mixed query returns meaningless matches (not slightly-worse ones). Kaizen keeps a separate index per model and ranks each query against a single active model, so switching embedders is a rolling, reversible re-index — `B3 --model <new>` builds the new index while the old one serves, `B7 --activate --model <new>` flips retrieval, and `B7 --activate --model <old>` rolls back. Until the active model is indexed, `E4 --semantic` denies with `DENIED_EMBED_INDEX_ABSENT`. See [`setup/PYTORCH.md`](PYTORCH.md) for why and for the full upgrade flow.

For the best current embedder, prefer the in-process `sentence-transformers` default (`codefuse-ai/F2LLM-v2-1.7B`, apache-2.0, instruction-tuned; see [`setup/PYTORCH.md`](PYTORCH.md) and the retrieval benchmark in [`docs/EMBEDDING-BENCHMARK.md`](../docs/EMBEDDING-BENCHMARK.md)). Ollama does not apply the model's query instruction (which the in-process path does automatically), so the in-process backend is preferred for retrieval quality; the example above uses the closest fresh GGUF for Ollama-only setups. The chat model (`Qwen/Qwen3.5-9B`, apache-2.0, GGUF Q4_K_M ~5.7 GB, ~8–11 GB VRAM) doubles as the LLM-as-judge model (`B4` / `O4`); a runner-up is `allenai/Olmo-3-7B-Instruct`. The judge is advisory only — it never gates acceptance.

## Verify

```powershell
python kaizen.py B1 --json
python kaizen.py E3 --id <doc-id> --json
python kaizen.py E4 --query "..." --semantic --json
python kaizen.py B3 --json
python kaizen.py B2 --prompt "Summarize ..." --json
python kaizen.py B4 --prompt "candidate output" --query "must be terse" --json
```
