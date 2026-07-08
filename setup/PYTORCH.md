# PyTorch Backend

PyTorch is an opt-in extra for in-process `sentence-transformers` embeddings and semantic chunking. It is intentionally outside the lightweight core install because `torch` plus model weights can be large.

The installer uses the Agent Kaizen shared venv by default:

```text
$DEVROOT/Python/venvs/kaizen
```

Pass `-PythonExe` / `--python-exe` only when you deliberately want to install into a different interpreter.

## Install

```powershell
setup\install-pytorch.ps1            # CUDA torch wheel (GPU-first default)
setup\install-pytorch.ps1 -Cpu       # CPU-only torch
```

```sh
bash setup/install-pytorch.sh        # CUDA torch wheel (GPU-first default)
bash setup/install-pytorch.sh --cpu  # CPU-only torch
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
KAIZEN_EMBED_MODEL=codefuse-ai/F2LLM-v2-1.7B
HF_HOME=$DEVROOT/models
```

The default embedder is instruction-tuned: `E4 --semantic`/`--hybrid` automatically applies the model's query prompt to the query (documents are embedded plain), which is what earns its retrieval lead — see [`docs/EMBEDDING-BENCHMARK.md`](../docs/EMBEDDING-BENCHMARK.md). Override the query instruction with `KAIZEN_EMBED_QUERY_PROMPT` if needed. A lighter 768-dim alternative that wins scientific-claim retrieval is `ibm-granite/granite-embedding-311m-multilingual-r2`.

### Multiple embedding models, one active index

Each embedding model maps text into its **own** learned vector space, so vectors from different models are not comparable — not merely because the length differs (granite is 768-dim, F2LLM 2048-dim), but because even at equal dimension the coordinate systems are unrelated: a query embedded by one model, compared against documents embedded by another, returns meaningless "nearest" matches, not slightly-worse ones. Kaizen therefore stores a **separate index per model** in the `chunk_embeddings` side table and ranks every query against a **single active model's** index, never a mix. The chunk text is the source of truth; each embedding index is a derived, rebuildable artifact over it.

Because indexes coexist, upgrading the embedder is a **rolling, reversible** operation instead of a blocking re-vector:

- `python kaizen.py B3 --model <new>` builds the new model's index in the background while the current active model keeps serving `E4 --semantic` (non-blocking; it only embeds the chunks the new model is missing).
- `python kaizen.py B7 --activate --model <new>` flips retrieval to it once it fully covers the corpus.
- Roll back instantly with `python kaizen.py B7 --activate --model <old>` — the previous index is retained. Retention keeps the active model plus the previous one by default (`KAIZEN_EMBED_KEEP`, default 2); `B7 --prune` frees older indexes on demand, and `--keep-all` disables auto-pruning.

`python kaizen.py B7` (`--list`) shows every indexed model, its chunk coverage and approximate size, and which is active. If the active model has no index yet, `E4 --semantic` denies with `DENIED_EMBED_INDEX_ABSENT` (pointing you at `B3 --model <active>`) rather than silently returning cross-model matches — the failure mode that matters most for an evidence layer.

## Text generation, reranking, and PII (opt-in, GPU-first)

These in-process backends are advisory and default-sized for a 12 GB GPU. `KAIZEN_TORCH_DEVICE=auto` prefers CUDA and falls back to CPU.

```text
# Local advisory text (B2) + LLM-as-judge (B4 / O4), no server:
KAIZEN_TEXT_BACKEND=transformers
KAIZEN_LLM_MODEL=Qwen/Qwen3.5-9B        # runner-up: allenai/Olmo-3-7B-Instruct
KAIZEN_TORCH_DEVICE=auto                # auto | cuda | cpu

# Cross-encoder reranker for E4 --rerank / --hybrid:
KAIZEN_RERANK_BACKEND=sentence-transformers
KAIZEN_RERANK_MODEL=cross-encoder/ettin-reranker-150m-v1

# Advisory PII scanner (B5; also attached to B2/B4 traces when set); augments, never replaces, the regex redaction gate:
KAIZEN_PII_MODEL=fastino/gliner2-privacy-filter-PII-multi
```

The 9B judge model is lighter served as GGUF Q4_K_M via Ollama; the reranker and PII scanner are sub-1 GB. All model output stays advisory — never the acceptance authority unless a deterministic verifier confirms. Default model repos are current best-in-class and reviewed on a cadence (see the model-freshness standard in `Kaizen_System.md`).

## Verify

```powershell
python kaizen.py B1 --json
python kaizen.py E3 --id <doc-id> --chunker semantic --json
python kaizen.py E4 --query "..." --semantic --json
python kaizen.py E4 --query "..." --rerank --json
python kaizen.py B3 --json
python kaizen.py B4 --prompt "candidate" --query "rubric" --json
python kaizen.py B5 --prompt "scan this text" --json
```

## Watch the models load (B6 monitor)

The per-op backends load transiently (a few seconds each) and the judge runs in the Ollama server, so a live test run barely registers on a polling monitor. For a sustained, clearly-visible load of every configured backend in one process, use `--probe --hold`:

```powershell
python kaizen.py B6 --probe --hold 30     # load embed/rerank/pii + the Ollama judge, hold 30s
python kaizen.py B6 --watch               # live-refresh view (loads nothing)
support_scripts\model-monitor.cmd         # native GUI (system-wide GPU + Ollama panels)
```

`support_scripts\run-backend-tests.cmd` runs a preflight, auto-launches the GUI, runs the live backend tests, then holds all four backends resident so the load is visible end-to-end.
