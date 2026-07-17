"""Pluggable model-backend layer (embeddings + advisory text).

Capability-activated and OPT-IN: a backend is returned only when its model is configured via
env (``KAIZEN_EMBED_MODEL`` / ``KAIZEN_LLM_MODEL``). With no configuration the harness behaves
exactly as before — lexical search, deterministic recursive chunking, no network.

One OpenAI-compatible adapter covers a local Ollama server (its ``/v1`` API) and any remote
OpenAI-compatible endpoint; API keys come from env only and are never stored. PyTorch backends
(sentence-transformers / transformers) register here in a later phase via the same protocols.
"""

from __future__ import annotations

import ipaddress
import os
import urllib.parse
from typing import Any, Optional, Protocol, runtime_checkable

from ..denials import KaizenDenied


_DEFAULT_OLLAMA_BASE = "http://127.0.0.1:11434/v1"


def _env_text(name: str) -> str | None:
    """Return a stripped, non-empty environment value."""
    value = os.environ.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _assert_endpoint_transport_safe(url: str, env_var: str, *, tailnet_probe=None) -> None:
    """Deny plain HTTP to a non-loopback endpoint: the bearer key would cross the
    network in cleartext. Loopback stays HTTP (the local-Ollama default); remote
    endpoints need https. KAIZEN_ALLOW_INSECURE_HTTP=1 opts a trusted LAN back in.

    v8 M13 (plan §C.1): plain HTTP to a host ending in the configured tailnet suffix
    (``KAIZEN_TAILNET_SUFFIX``, default ``.ts.net``) is ALSO allowed WHILE ``on_tailnet()`` is
    True -- WireGuard end-to-end encryption covers that hop, exactly like the git-mirror transport
    gate. ``tailnet_probe`` is injectable for tests; None ⇒ ``fleet.net.on_tailnet`` is imported
    LAZILY inside the branch, so the no-fleet / loopback / https paths never load the fleet package."""
    if os.environ.get("KAIZEN_ALLOW_INSECURE_HTTP") == "1":
        return
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if parsed.scheme == "https" or loopback:
        return
    if parsed.scheme == "http" and host:
        suffix = (os.environ.get("KAIZEN_TAILNET_SUFFIX") or ".ts.net").strip().lower()
        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        if host.endswith(suffix):
            probe = tailnet_probe
            if probe is None:
                from ..fleet.net import on_tailnet as probe  # lazy: off-tailnet paths never load fleet
            if probe():
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
    """Embedder contract with stable name/model attributes and a capability probe mapping."""
    name: str
    model: str

    # is_query=True embeds a SEARCH QUERY (instruction-tuned models apply their query prompt);
    # is_query=False (default) embeds DOCUMENTS/chunks. Backends without a query prompt ignore it.
    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]: ...

    def probe(self) -> dict[str, Any]: ...


@runtime_checkable
class TextBackend(Protocol):
    """Advisory text contract with stable name/model attributes and a capability probe mapping."""
    name: str
    model: str

    def chat(self, prompt: str, **opts: Any) -> dict[str, Any]: ...

    def probe(self) -> dict[str, Any]: ...


@runtime_checkable
class RerankBackend(Protocol):
    """Reranker contract with stable name/model attributes and a capability probe mapping."""
    name: str
    model: str

    def rank(self, query: str, passages: list[str]) -> list[float]: ...

    def probe(self) -> dict[str, Any]: ...


@runtime_checkable
class PiiBackend(Protocol):
    """Advisory PII scanner contract with stable name/model attributes and a capability probe mapping."""
    name: str
    model: str

    def scan(self, text: str) -> list[dict[str, Any]]: ...

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

        return SentenceTransformersBackend(model=_env_text("KAIZEN_EMBED_MODEL"))

    model = _env_text("KAIZEN_EMBED_MODEL")
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


def get_embedding_backend_for(model: str) -> EmbeddingBackend:
    """An embedding backend pinned to ``model`` (B3 --model / B7 rolling upgrade builds a specific
    index while the active one keeps serving). Mirrors get_embedding_backend()'s backend SELECTION but
    forces the model, and never returns None -- passing --model is an explicit request to embed with it
    (a missing extra/server surfaces as a DENIED at embed time, not a silent no-backend)."""
    selector = os.environ.get("KAIZEN_EMBED_BACKEND", "ollama").strip().lower()
    if selector in ("sentence-transformers", "sentence_transformers", "st"):
        from .sentence_transformers_backend import SentenceTransformersBackend

        return SentenceTransformersBackend(model=model)
    from .ollama import OllamaEmbeddingBackend

    base_url = os.environ.get("KAIZEN_EMBED_BASE_URL", _DEFAULT_OLLAMA_BASE)
    _assert_endpoint_transport_safe(base_url, "KAIZEN_EMBED_BASE_URL")
    return OllamaEmbeddingBackend(
        base_url=base_url,
        model=model,
        api_key=os.environ.get("KAIZEN_EMBED_API_KEY"),
    )


EMBED_BATCH_SIZE = 128


def embed_batched(
    backend: EmbeddingBackend,
    texts: list[str],
    *,
    batch_size: int = EMBED_BATCH_SIZE,
    is_query: bool = False,
    record_trace: bool = False,
    task_id: str | None = None,
    is_test: int = 0,
) -> list[list[float]]:
    """Embed ``texts`` in bounded batches, preserving order.

    One unbatched call over a large corpus risks backend request-size limits and memory
    spikes, so E3 (chunk auto-embed), semantic chunking, and B3 (reembed) all route
    through here. A responding backend that returns the wrong count is a data-integrity
    fault, not an availability gap: deny — never zip-truncate.

    ``record_trace=True`` writes one best-effort ``model_call`` observability trace (lane
    ``embedding``) so this model use shows in the B6 monitor; it never affects the return value.
    """
    import time

    if isinstance(batch_size, bool) or batch_size <= 0:
        raise KaizenDenied(
            "DENIED_EMBED_BATCH_SIZE",
            {"batch_size": batch_size, "required_action": "batch_size must be a positive integer"},
            exit_code=2,
        )
    started = time.monotonic()
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        out = backend.embed(batch, is_query=is_query)
        if len(out) != len(batch):
            raise KaizenDenied(
                "DENIED_EMBED_MISMATCH",
                {
                    "expected": len(batch),
                    "got": len(out),
                    "batch_start": start,
                    "required_action": "embedding backend returned a mismatched vector count",
                },
                exit_code=1,
            )
        vectors.extend(out)
    if record_trace:
        from ..trace_records import record_model_call

        record_model_call(
            lane="embedding",
            model=getattr(backend, "model", None),
            provider=getattr(backend, "name", None),
            latency_ms=int((time.monotonic() - started) * 1000),
            count=len(texts),
            dimension=next((len(vector) for vector in vectors if vector), None),
            task_id=task_id,
            is_test=is_test,
        )
    return vectors


def get_text_backend() -> Optional[TextBackend]:
    """The configured advisory text backend, or None when none is configured (opt-in).

    ``KAIZEN_TEXT_BACKEND`` (alias ``KAIZEN_LLM_BACKEND``) selects ``ollama`` (default, network) or
    ``transformers`` (in-process, GPU-first). The transformers branch needs no ``KAIZEN_LLM_MODEL``
    (it has a default); the ollama branch stays gated on it, so ``B1``/``B2``/``B4``/``O4`` behave
    unchanged. Both satisfy the ``TextBackend`` protocol.
    """
    selector = (
        os.environ.get("KAIZEN_TEXT_BACKEND") or os.environ.get("KAIZEN_LLM_BACKEND") or "ollama"
    ).strip().lower()
    if selector in ("transformers", "hf", "huggingface"):
        from .transformers_backend import TransformersTextBackend

        return TransformersTextBackend(model=_env_text("KAIZEN_LLM_MODEL"))

    model = _env_text("KAIZEN_LLM_MODEL")
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


def get_rerank_backend() -> Optional[RerankBackend]:
    """The configured cross-encoder reranker, or None when none is configured (opt-in).

    ``KAIZEN_RERANK_BACKEND=sentence-transformers`` selects the in-process CrossEncoder;
    ``KAIZEN_RERANK_MODEL`` overrides the default model. Unset -> None, so E4 ``--rerank``
    denies cleanly rather than silently importing torch.
    """
    selector = os.environ.get("KAIZEN_RERANK_BACKEND", "").strip().lower()
    if selector in ("sentence-transformers", "sentence_transformers", "st"):
        from .cross_encoder_backend import CrossEncoderRerankBackend

        return CrossEncoderRerankBackend(model=_env_text("KAIZEN_RERANK_MODEL"))
    return None


def get_pii_backend() -> Optional[PiiBackend]:
    """The configured advisory PII NER, or None when none is configured (opt-in).

    Activated by ``KAIZEN_PII_MODEL`` or ``KAIZEN_PII_BACKEND``; unset -> None, so the deterministic
    regex gate stays the sole enforced redaction check and no torch import happens. The model is
    ADVISORY: it augments, never replaces, the regex.
    """
    selector = os.environ.get("KAIZEN_PII_BACKEND", "").strip().lower()
    model = _env_text("KAIZEN_PII_MODEL")
    if not selector and not model:
        return None
    from .gliner_pii_backend import Gliner2PiiBackend

    return Gliner2PiiBackend(model=model or None)


def describe_backends() -> list[dict[str, Any]]:
    """Config-only reflection of the four model lanes: what WOULD run on the next op.

    Reads env + each backend module's ``_DEFAULT_MODEL`` WITHOUT importing torch or loading any
    weights, so it is cheap enough for a ``--watch`` refresh loop (B6). Mirrors the selection logic
    of the ``get_*_backend`` factories; ``device`` is the REQUESTED device (``KAIZEN_TORCH_DEVICE``),
    not the resolved one (resolving needs a torch import) -- B6 ``--probe`` reports the real device.
    """
    torch_device = (os.environ.get("KAIZEN_TORCH_DEVICE", "auto").strip().lower() or "auto")
    lanes: list[dict[str, Any]] = []

    def off(lane: str) -> dict[str, Any]:
        return {
            "lane": lane, "configured": False, "backend": None, "model": None,
            "device": None, "in_process": None, "transport": None,
        }

    # embed: KAIZEN_EMBED_BACKEND=sentence-transformers (in-process) else Ollama (needs a model).
    embed_sel = os.environ.get("KAIZEN_EMBED_BACKEND", "ollama").strip().lower()
    embed_model = _env_text("KAIZEN_EMBED_MODEL")
    if embed_sel in ("sentence-transformers", "sentence_transformers", "st"):
        from .sentence_transformers_backend import _DEFAULT_MODEL as _ST_EMBED

        lanes.append({"lane": "embed", "configured": True, "backend": "sentence-transformers",
                      "model": embed_model or _ST_EMBED, "device": torch_device, "in_process": True})
    elif embed_model:
        lanes.append({"lane": "embed", "configured": True, "backend": "ollama", "model": embed_model,
                      "device": "server", "transport": os.environ.get("KAIZEN_EMBED_BASE_URL", _DEFAULT_OLLAMA_BASE)})
    else:
        lanes.append(off("embed"))

    # text: KAIZEN_TEXT_BACKEND/KAIZEN_LLM_BACKEND=transformers (in-process, has a default) else Ollama.
    text_sel = (os.environ.get("KAIZEN_TEXT_BACKEND") or os.environ.get("KAIZEN_LLM_BACKEND") or "ollama").strip().lower()
    text_model = _env_text("KAIZEN_LLM_MODEL")
    if text_sel in ("transformers", "hf", "huggingface"):
        from .transformers_backend import _DEFAULT_MODEL as _HF_TEXT

        lanes.append({"lane": "text", "configured": True, "backend": "transformers",
                      "model": text_model or _HF_TEXT, "device": torch_device, "in_process": True})
    elif text_model:
        lanes.append({"lane": "text", "configured": True, "backend": "ollama", "model": text_model,
                      "device": "server", "transport": os.environ.get("KAIZEN_LLM_BASE_URL", _DEFAULT_OLLAMA_BASE)})
    else:
        lanes.append(off("text"))

    # rerank: opt-in via KAIZEN_RERANK_BACKEND only (no default-on).
    rerank_sel = os.environ.get("KAIZEN_RERANK_BACKEND", "").strip().lower()
    if rerank_sel in ("sentence-transformers", "sentence_transformers", "st"):
        from .cross_encoder_backend import _DEFAULT_MODEL as _CE_RERANK

        lanes.append({"lane": "rerank", "configured": True, "backend": "sentence-transformers",
                      "model": _env_text("KAIZEN_RERANK_MODEL") or _CE_RERANK,
                      "device": torch_device, "in_process": True})
    else:
        lanes.append(off("rerank"))

    # pii: opt-in via KAIZEN_PII_MODEL or KAIZEN_PII_BACKEND.
    pii_sel = os.environ.get("KAIZEN_PII_BACKEND", "").strip().lower()
    pii_model = _env_text("KAIZEN_PII_MODEL")
    if pii_sel or pii_model:
        from .gliner_pii_backend import _DEFAULT_MODEL as _GL_PII

        lanes.append({"lane": "pii", "configured": True, "backend": "gliner2",
                      "model": pii_model or _GL_PII, "device": torch_device, "in_process": True})
    else:
        lanes.append(off("pii"))

    return lanes


__all__ = [
    "EmbeddingBackend",
    "TextBackend",
    "RerankBackend",
    "PiiBackend",
    "embed_batched",
    "get_embedding_backend",
    "get_embedding_backend_for",
    "get_text_backend",
    "get_rerank_backend",
    "get_pii_backend",
    "describe_backends",
]
