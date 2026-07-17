"""In-process sentence-transformers embedding backend (opt-in, no server).

Activated via ``KAIZEN_EMBED_BACKEND=sentence-transformers``. The heavy import is deferred to first
use; when the opt-in extra is not installed the backend denies cleanly (pointing at
``requirements-pytorch.txt``). Embeddings are deterministic per model+version, so the recorded
``embedding_model`` keeps Turso-native vector search reproducible. Interchangeable with the Ollama
embedder via the shared ``EmbeddingBackend`` protocol.

(The module is named ``sentence_transformers_backend`` to avoid shadowing the real top-level
``sentence_transformers`` package it imports.)
"""

from __future__ import annotations

import os
import threading
from typing import Any

from ..denials import KaizenDenied

# apache-2.0, 2026-03, Qwen3-1.7B backbone, 2048-dim, ~80 langs, instruction-tuned. Chosen over
# granite-embedding-311m on a Kaizen-pipeline NDCG@10 A-B (see docs/EMBEDDING-BENCHMARK.md): +4.7 mean
# / +12.7 on FiQA with its query instruction. Runner-up (lighter, 768-dim, wins scientific-claim
# retrieval): ibm-granite/granite-embedding-311m-multilingual-r2.
_DEFAULT_MODEL = "codefuse-ai/F2LLM-v2-1.7B"


class SentenceTransformersBackend:
    name = "sentence-transformers"

    def __init__(self, *, model: str | None = None) -> None:
        self.model = model or _DEFAULT_MODEL
        self._encoder: Any = None
        self._load_lock = threading.Lock()

    def _load(self) -> Any:
        if self._encoder is not None:
            return self._encoder
        with self._load_lock:
            return self._load_locked()

    def _load_locked(self) -> Any:
        """Load exactly once while _load_lock is held."""
        if self._encoder is not None:
            return self._encoder
        from ..quiet import quiet_stderr

        try:
            with quiet_stderr("sentence_transformers", "transformers"):
                from sentence_transformers import SentenceTransformer
        except Exception as error:  # noqa: BLE001 -- extra not installed
            raise KaizenDenied(
                "DENIED_BACKEND_UNAVAILABLE",
                {
                    "backend": self.name,
                    "reason": str(error),
                    "required_action": "install the opt-in extra: pip install -r requirements-pytorch.txt (see setup/PYTORCH.md)",
                },
                exit_code=2,
            ) from error
        try:
            with quiet_stderr("sentence_transformers", "transformers"):
                self._encoder = SentenceTransformer(self.model)
        except Exception as error:  # noqa: BLE001 -- bad model name / download failure
            raise KaizenDenied(
                "DENIED_BACKEND_MODEL",
                {
                    "backend": self.name,
                    "model": self.model,
                    "reason": str(error),
                    "required_action": "check the model name; first use downloads it from HuggingFace (set HF_HOME -- see setup/PYTORCH.md)",
                },
                exit_code=2,
            ) from error
        return self._encoder

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        encoder = self._load()
        kw: dict[str, Any] = {}
        if is_query:
            # Instruction-tuned embedders (F2LLM, Qwen3-Embedding, ...) need a query prompt to reach
            # their retrieval ceiling; documents get none. Prefer an explicit env override, else the
            # model's own configured "query" prompt (config_sentence_transformers.json). A model with
            # no query prompt (e.g. granite) falls through to plain encoding -- behavior unchanged.
            override = os.environ.get("KAIZEN_EMBED_QUERY_PROMPT")
            if override:
                kw["prompt"] = override
            elif (getattr(encoder, "prompts", None) or {}).get("query"):
                kw["prompt_name"] = "query"
        try:
            vectors = encoder.encode(texts, convert_to_numpy=True, normalize_embeddings=False, **kw)
        except Exception as error:  # noqa: BLE001 -- backend failures become stable denials
            raise KaizenDenied(
                "DENIED_BACKEND_MODEL",
                {
                    "backend": self.name,
                    "model": self.model,
                    "reason": str(error),
                    "required_action": "reduce the batch or check the configured device and model",
                },
                exit_code=2,
            ) from error
        return vectors.tolist()

    def probe(self) -> dict[str, Any]:
        vectors = self.embed(["probe"])
        dimension = len(vectors[0]) if vectors and vectors[0] else 0
        return {"backend": self.name, "kind": "embedding", "model": self.model, "in_process": True, "dimension": dimension}
