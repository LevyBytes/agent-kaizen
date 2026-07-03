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

from typing import Any

from ..denials import KaizenDenied

_DEFAULT_MODEL = "all-MiniLM-L6-v2"  # 384-dim, small/fast; fixes the corpus embedding dimension


class SentenceTransformersBackend:
    name = "sentence-transformers"

    def __init__(self, *, model: str | None = None) -> None:
        self.model = model or _DEFAULT_MODEL
        self._encoder: Any = None

    def _load(self) -> Any:
        if self._encoder is not None:
            return self._encoder
        try:
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

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        encoder = self._load()
        vectors = encoder.encode(list(texts), convert_to_numpy=True, normalize_embeddings=False)
        return [[float(value) for value in row] for row in vectors]

    def probe(self) -> dict[str, Any]:
        vectors = self.embed(["probe"])
        dimension = len(vectors[0]) if vectors and vectors[0] else 0
        return {"backend": self.name, "kind": "embedding", "model": self.model, "in_process": True, "dimension": dimension}
