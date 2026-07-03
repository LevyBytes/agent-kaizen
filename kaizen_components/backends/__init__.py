"""Pluggable model-backend layer (embeddings + advisory text).

Capability-activated and OPT-IN: a backend is returned only when its model is configured via
env (``KAIZEN_EMBED_MODEL`` / ``KAIZEN_LLM_MODEL``). With no configuration the harness behaves
exactly as before — lexical search, deterministic recursive chunking, no network.

One OpenAI-compatible adapter covers a local Ollama server (its ``/v1`` API) and any remote
OpenAI-compatible endpoint; API keys come from env only and are never stored. PyTorch backends
(sentence-transformers / transformers) register here in a later phase via the same protocols.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any, Optional, Protocol, runtime_checkable

from ..denials import KaizenDenied


_DEFAULT_OLLAMA_BASE = "http://127.0.0.1:11434/v1"


def _assert_endpoint_transport_safe(url: str, env_var: str) -> None:
    """Deny plain HTTP to a non-loopback endpoint: the bearer key would cross the
    network in cleartext. Loopback stays HTTP (the local-Ollama default); remote
    endpoints need https. KAIZEN_ALLOW_INSECURE_HTTP=1 opts a trusted LAN back in."""
    if os.environ.get("KAIZEN_ALLOW_INSECURE_HTTP") == "1":
        return
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" or host in ("127.0.0.1", "localhost", "::1"):
        return
    raise KaizenDenied(
        "DENIED_ENDPOINT_INSECURE",
        {
            "endpoint": url,
            "env_var": env_var,
            "required_action": (
                "use https:// for non-loopback endpoints, keep the backend on 127.0.0.1, "
                "or set KAIZEN_ALLOW_INSECURE_HTTP=1 for a trusted private network"
            ),
        },
        exit_code=2,
    )


@runtime_checkable
class EmbeddingBackend(Protocol):
    name: str
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def probe(self) -> dict[str, Any]: ...


@runtime_checkable
class TextBackend(Protocol):
    name: str
    model: str

    def chat(self, prompt: str, **opts: Any) -> dict[str, Any]: ...

    def probe(self) -> dict[str, Any]: ...


def get_embedding_backend() -> Optional[EmbeddingBackend]:
    """The configured embedding backend, or None when none is configured (opt-in).

    ``KAIZEN_EMBED_BACKEND=sentence-transformers`` selects the in-process PyTorch embedder (no
    server, opt-in extra); otherwise the default is Ollama, active only when ``KAIZEN_EMBED_MODEL``
    is set. Both implement the same ``EmbeddingBackend`` protocol, so E3/E4 are backend-agnostic.
    """
    selector = os.environ.get("KAIZEN_EMBED_BACKEND", "ollama").strip().lower()
    if selector in ("sentence-transformers", "sentence_transformers", "st"):
        from .sentence_transformers_backend import SentenceTransformersBackend

        return SentenceTransformersBackend(model=os.environ.get("KAIZEN_EMBED_MODEL") or None)

    model = os.environ.get("KAIZEN_EMBED_MODEL")
    if not model:
        return None
    from .ollama import OllamaEmbeddingBackend

    base_url = os.environ.get("KAIZEN_EMBED_BASE_URL", _DEFAULT_OLLAMA_BASE)
    _assert_endpoint_transport_safe(base_url, "KAIZEN_EMBED_BASE_URL")
    return OllamaEmbeddingBackend(
        base_url=base_url,
        model=model,
        api_key=os.environ.get("KAIZEN_EMBED_API_KEY"),
    )


def get_text_backend() -> Optional[TextBackend]:
    """The configured advisory text backend, or None when none is configured (opt-in)."""
    model = os.environ.get("KAIZEN_LLM_MODEL")
    if not model:
        return None
    from .ollama import OllamaTextBackend

    base_url = os.environ.get("KAIZEN_LLM_BASE_URL", _DEFAULT_OLLAMA_BASE)
    _assert_endpoint_transport_safe(base_url, "KAIZEN_LLM_BASE_URL")
    return OllamaTextBackend(
        base_url=base_url,
        model=model,
        api_key=os.environ.get("KAIZEN_LLM_API_KEY"),
    )


__all__ = ["EmbeddingBackend", "TextBackend", "get_embedding_backend", "get_text_backend"]
