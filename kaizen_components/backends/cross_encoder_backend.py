"""In-process sentence-transformers CrossEncoder reranker (opt-in, no server).

Activated via ``KAIZEN_RERANK_BACKEND=sentence-transformers``. Re-scores retrieved evidence
chunks against the query on the E4 read path; it orders already-retrieved evidence and is never
an acceptance authority. The heavy import is deferred to first use; when the opt-in extra is not
installed the backend denies cleanly (pointing at ``requirements-pytorch.txt``). The default is a
fresh, permissive ModernBERT cross-encoder; scoring is deterministic per model+version.
"""

from __future__ import annotations

from typing import Any

from ..denials import KaizenDenied

_DEFAULT_MODEL = "cross-encoder/ettin-reranker-150m-v1"  # apache-2.0, ModernBERT, ~7999-token ctx


class CrossEncoderRerankBackend:
    name = "sentence-transformers"

    def __init__(self, *, model: str | None = None) -> None:
        self.model = model or _DEFAULT_MODEL
        self._encoder: Any = None

    def _load(self) -> Any:
        if self._encoder is not None:
            return self._encoder
        from ..quiet import quiet_stderr

        try:
            with quiet_stderr("sentence_transformers", "transformers"):
                from sentence_transformers import CrossEncoder
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
                self._encoder = CrossEncoder(self.model)
        except Exception as error:  # noqa: BLE001 -- bad model name / download failure / old sentence-transformers
            raise KaizenDenied(
                "DENIED_BACKEND_MODEL",
                {
                    "backend": self.name,
                    "model": self.model,
                    "reason": str(error),
                    "required_action": "check the model name and that sentence-transformers supports ModernBERT; first use downloads weights (set HF_HOME -- see setup/PYTORCH.md)",
                },
                exit_code=2,
            ) from error
        return self._encoder

    def rank(self, query: str, passages: list[str]) -> list[float]:
        """Return one relevance score per passage (higher = more relevant), order preserved."""
        if not passages:
            return []
        encoder = self._load()
        scores = encoder.predict([[query, passage] for passage in passages])
        return [float(score) for score in scores]

    def probe(self) -> dict[str, Any]:
        self.rank("probe", ["hello world"])
        return {"backend": self.name, "kind": "rerank", "model": self.model, "in_process": True}
