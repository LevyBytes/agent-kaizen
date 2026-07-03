# PyTorch backend (in-process embeddings + semantic chunking)

An **opt-in, heavy** extra that adds an **in-process** sentence-transformers embedding backend (no
server) and an embedder-backed **semantic chunker**. It implements the same `EmbeddingBackend`
protocol as Ollama, so it activates the exact same E3/E4 vector search — just without needing a
running model server.

**The catch is weight:** `torch` + `sentence-transformers` are ~GB. They live **only** in
`requirements-pytorch.txt`, never the dependency-light core. With the extra uninstalled the harness
degrades gracefully (deterministic recursive chunking + lexical search, or Ollama if configured).

> The `transformers` local text-generation backend is **deferred** — use Ollama's `B2 model-run`
> for local advisory text. Only the embedding backend + semantic chunker ship here.

## Install

The embeddings run **in-process**, so install into the **same venv you run `kaizen.py` with** (the
existing kaizen venv, or a dedicated `kaizen-pytorch` venv you then launch kaizen from).

```powershell
setup\install-pytorch.ps1            # CPU torch
setup\install-pytorch.ps1 -Gpu      # CUDA torch wheel
```

```bash
bash setup/install-pytorch.sh        # CPU torch
bash setup/install-pytorch.sh --gpu  # CUDA torch wheel
```

The installer redirects the HuggingFace weight cache to **`$DEVROOT/models`** via `HF_HOME` so the
multi-GB weights live outside the repo (workspace-visible, never tracked). First use of a model
downloads it from HuggingFace — an explicit, one-time network fetch.

## Configure (opt-in)

```text
KAIZEN_EMBED_BACKEND = sentence-transformers     # select the in-process embedder
KAIZEN_EMBED_MODEL   = all-MiniLM-L6-v2          # optional; this 384-dim default is used if unset
HF_HOME              = $DEVROOT/models           # keep weights out of the repo
```

Embeddings are deterministic per model+version, so the recorded `embedding_model` makes vector
search reproducible. One embedding model per corpus (consistent dimension) — switching models
requires `B3 reembed`.

## Verify + use

```bash
python kaizen.py B1 --json                              # model-doctor: reports sentence-transformers + dimension
python kaizen.py E3 --id <doc-id> --chunker semantic --json   # embedder-backed semantic chunking
python kaizen.py E4 --query "..." --semantic --json     # nearest chunks by cosine
python kaizen.py B3 --json                              # backfill embeddings for existing chunks
```
