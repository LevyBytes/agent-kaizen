# Embedding model benchmark — why `F2LLM-v2-1.7B` is the default

The default embedder (`E3` chunk embedding / `E4` vector search) was chosen from a **measured retrieval A-B on the actual Kaizen pipeline** — not from vendor leaderboard claims. Both candidates were run end-to-end and scored against real relevance labels.

## Method

- **Pipeline-faithful.** Each model embeds the corpus and queries through the exact Kaizen in-process embedder (the `E3` path), and retrieval is ranked by cosine distance — the same operation `E4 --semantic` runs via Turso `vector_distance_cos` (validated directly on SciFact; the larger sweeps use vectorized numpy cosine, which produces identical rankings).
- **Real labels.** Three standard [BeIR](https://github.com/beir-cellar/beir) retrieval sets with their published `qrels`, scored as graded **NDCG@10** (also MRR@10 / Recall@10): **SciFact** (scientific-claim verification, 300 test queries / 5,183 docs), **NFCorpus** (medical, 323 / 3,633), **FiQA** (financial Q&A, 648 / 57,638).
- **The query instruction.** F2LLM is instruction-tuned — it ships a `query` prompt. It is measured both **as the backend runs it today** (Kaizen applies the query instruction to queries automatically; documents stay unprompted) and **without** any prompt, to isolate how much the instruction is worth. `granite` needs no prompt.

The method above is fully specified, so the numbers are reproducible: embed each BeIR corpus + queries with the model under `KAIZEN_EMBED_BACKEND=sentence-transformers`, rank by cosine, and score `NDCG@10` against the published `qrels` (F2LLM queries carry its `config_sentence_transformers.json` `query` prompt; documents none).

## Results — NDCG@10 (higher is better)

| Model | dim | SciFact | NFCorpus | FiQA | **mean** |
|---|---|---|---|---|---|
| `granite-embedding-311m-multilingual-r2` | 768 | **0.7081** | 0.3113 | 0.3947 | 0.4714 |
| `F2LLM-v2-1.7B` — no prompt | 2048 | 0.6573 | 0.3239 | 0.4763 | 0.4858 |
| **`F2LLM-v2-1.7B` — with query instruction** | 2048 | 0.6700 | **0.3635** | **0.5218** | **0.5184** |
| `F2LLM-v2-1.7B` — instruction, truncated to 768 | 768 | 0.6468 | 0.3573 | 0.5177 | 0.5060 |

Δ NDCG@10 vs `granite` (F2LLM with its instruction): **SciFact -0.038, NFCorpus +0.052, FiQA +0.127, mean +0.047.**

## Why F2LLM was chosen

- **Better retriever on balance.** It wins two of three sets and is **+0.047 NDCG@10 on average**, decisively so on FiQA (**+0.127**). Even without its prompt it edges granite on average.
- **The query instruction earns the lead** (+0.013→+0.046 over no-prompt per set), so the backend now applies it automatically to `E4` queries — without it, F2LLM would actually *lose* on SciFact.
- **Clears the same gates as granite:** apache-2.0, fresh (F2LLM 2026-03, granite 2026-04), multilingual (~80 languages), clean `sentence-transformers` load (no `trust_remote_code`), fits the 12 GB budget (~3.4 GB).

The result is **domain-dependent**, and the doc is honest about it: `granite` wins **SciFact** (scientific-claim retrieval), where a compact ModernBERT bi-encoder is very strong. F2LLM wins the Q&A-style tasks that better match general evidence retrieval.

## Runner-up and tradeoffs

`ibm-granite/granite-embedding-311m-multilingual-r2` remains the recommended alternative and is a one-line switch:

```text
KAIZEN_EMBED_MODEL=ibm-granite/granite-embedding-311m-multilingual-r2
```

Prefer it when your corpus is scientific/claim-style, or when you want a much lighter footprint: **~0.62 GB vs ~3.4 GB**, **768-dim vs 2048-dim** (≈2.7× smaller vector store and faster ANN), and no query prompt to manage.

Switching or upgrading the embedder is a **rolling, reversible re-index**, not a blocking re-vector: embeddings are model-specific, so Kaizen keeps a separate index per model and ranks against a single active one. `B3 --model <new>` builds the new index in the background while the old one keeps serving, `B7 --activate --model <new>` flips retrieval once it fully covers the corpus, and `B7 --activate --model <old>` rolls back instantly (the previous index is retained; see [`setup/PYTORCH.md`](../setup/PYTORCH.md)). Until the active model is indexed, `E4 --semantic` denies with `DENIED_EMBED_INDEX_ABSENT`.
