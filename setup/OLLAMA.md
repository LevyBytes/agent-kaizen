# Ollama model backend (B\* / `model-*`)

An **optional, opt-in** model backend that speaks the **OpenAI-compatible** API — a local Ollama
server by default, or any remote OpenAI-compatible endpoint by configuration. It provides two
capabilities:

- **Embeddings** (the payoff): light up `E3` (store an embedding per chunk) and `E4 --semantic`
  (Turso-native vector search via `vector_distance_cos`). `B3 reembed` backfills existing chunks.
- **Advisory text** (`B2 model-run`): triage / summarize / classify, recorded as a `model_call`
  trace. **Advisory only** — never the final acceptance authority unless a deterministic verifier
  confirms.

With nothing configured the harness is unchanged: deterministic recursive chunking + lexical search,
no network. API keys are read from the environment **only** and are never stored.

## Install

1. Install Ollama from <https://ollama.com/download> (it runs as a local service on `:11434`).
2. Run the helper (relocates the model store under `$DEVROOT`, pulls an embed + chat model):

   ```powershell
   setup\install-ollama.ps1            # Windows
   ```

   ```bash
   bash setup/install-ollama.sh        # macOS / Linux
   ```

   It points `OLLAMA_MODELS` at `$DEVROOT/Ollama/models` so weights live outside the repo
   (workspace-visible, never tracked by git).

## Configure (opt-in)

Tell Kaizen which models to use (a backend activates **only** when its model is set):

```text
KAIZEN_EMBED_MODEL = nomic-embed-text          # enables E3 embeddings + E4 --semantic
KAIZEN_LLM_MODEL   = llama3.2                   # enables B2 model-run
# Remote OpenAI-compatible endpoint (optional; local Ollama /v1 is the default):
KAIZEN_EMBED_BASE_URL / KAIZEN_LLM_BASE_URL     # e.g. https://api.example.com/v1
KAIZEN_EMBED_API_KEY  / KAIZEN_LLM_API_KEY      # env only, never stored
```

One embedding model per corpus — `vector_distance_cos` needs a consistent dimension. Switching the
embed model requires a re-embed (`B3 reembed`).

## Verify + use

```bash
python kaizen.py B1 --json                       # model-doctor: configured? reachable? dimension?
python kaizen.py E1 --path notes.md --json       # ingest
python kaizen.py E3 --id <doc-id> --json         # chunk -> chunks now carry an embedding
python kaizen.py E4 --query "..." --semantic --json   # nearest chunks by cosine (else lexical)
python kaizen.py B3 --json                        # backfill embeddings for existing chunks
python kaizen.py B2 --prompt "Summarize ..." --json   # advisory text -> a model_call trace
```

`E4 --semantic` falls back to the lexical baseline when no embeddings are stored yet; run `E3`
(after configuring an embed model) or `B3 reembed` to populate them.
