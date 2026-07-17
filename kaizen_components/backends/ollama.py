"""Ollama-backed embedding + advisory-text backends (local default, OpenAI-compatible).

Defaults to a local Ollama server's OpenAI-compatible ``/v1`` API but works against any remote
OpenAI-compatible endpoint via env. Embeddings fall back to Ollama's native ``/api/embeddings``
when the ``/v1`` route is unavailable.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Callable

from ..denials import KaizenDenied
from .http_retry import http_request
from .openai_compat import OpenAICompatClient


class OllamaEmbeddingBackend:
    """Local Ollama embedder over `/v1` with native `/api/embeddings` fallback; ctor base_url/model/api_key."""
    name = "ollama"

    def __init__(self, *, base_url: str, model: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.client = OpenAICompatClient(self.base_url, api_key=api_key)

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        """Per-text vectors, empty-in→empty-out; `is_query`+`KAIZEN_EMBED_QUERY_PROMPT` prefix; `/v1`→native fallback semantics (ties to F1)."""
        if not texts:
            return []
        # Ollama embedders have no prompt config; honor an explicit query-instruction override only.
        prompt = os.environ.get("KAIZEN_EMBED_QUERY_PROMPT") if is_query else None
        if prompt:
            texts = [prompt + t for t in texts]
        try:
            vectors = self.client.embeddings(texts, self.model)
            return vectors
        except KaizenDenied as denied:
            # Older Ollama builds may lack only the OpenAI-compatible route. Preserve every other
            # denial: retrying natively would replace its actionable status/detail with a generic error.
            if denied.code != "DENIED_BACKEND_HTTP" or denied.fields.get("http_status") != 404:
                raise
        return self._native_embed(texts)

    def _native_embed(self, texts: list[str]) -> list[list[float]]:
        """Pre-`/v1` Ollama fallback; per-text `/api/embeddings` call; raises `DENIED_BACKEND_UNAVAILABLE` on transport/parse failure (see A1 gap)."""
        root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        vectors: list[list[float]] = []
        for text in texts:
            data = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            req = urllib.request.Request(
                root + "/api/embeddings", data=data, headers=headers
            )
            try:
                raw = http_request(req, timeout=60).decode("utf-8")
            except Exception as error:  # noqa: BLE001
                raise KaizenDenied(
                    "DENIED_BACKEND_UNAVAILABLE",
                    {
                        "base_url": self.base_url,
                        "reason": str(error),
                        "required_action": f"start Ollama (see setup/OLLAMA.md) and run `ollama pull {self.model}`",
                    },
                    exit_code=2,
                ) from error
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as error:
                raise KaizenDenied(
                    "DENIED_BACKEND_MALFORMED",
                    {
                        "base_url": self.base_url,
                        "reason": "native response was not JSON",
                        "required_action": "check the Ollama endpoint and model",
                    },
                    exit_code=2,
                ) from error
            vector = payload.get("embedding") if isinstance(payload, dict) else None
            if not isinstance(vector, list) or not all(
                isinstance(value, (int, float)) and not isinstance(value, bool) for value in vector
            ):
                raise KaizenDenied(
                    "DENIED_BACKEND_MALFORMED",
                    {
                        "base_url": self.base_url,
                        "reason": "native response omitted a numeric embedding list",
                        "required_action": "check the Ollama endpoint and model",
                    },
                    exit_code=2,
                )
            vectors.append([float(value) for value in vector])
        return vectors

    def probe(self) -> dict[str, Any]:
        """Capability dict incl. detected dimension via one "probe" embed."""
        vectors = self.embed(["probe"])
        dimension = len(vectors[0]) if vectors and vectors[0] else 0
        return {
            "backend": self.name,
            "kind": "embedding",
            "model": self.model,
            "base_url": self.base_url,
            "dimension": dimension,
        }


class OllamaTextBackend:
    """Advisory-text backend over OpenAI-compatible `/v1/chat/completions`."""
    name = "ollama"

    def __init__(self, *, base_url: str, model: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.client = OpenAICompatClient(self.base_url, api_key=api_key)

    def chat(self, prompt: str, **opts: Any) -> dict[str, Any]:
        return self.client.chat([{"role": "user", "content": prompt}], self.model, **opts)

    def chat_messages(self, messages: list[dict[str, str]], **opts: Any) -> dict[str, Any]:
        return self.client.chat(messages, self.model, **opts)

    def chat_messages_stream(
        self,
        messages: list[dict[str, str]],
        on_delta: Callable[[str], None],
        **opts: Any,
    ) -> dict[str, Any]:
        return self.client.chat_stream(messages, self.model, on_delta, **opts)

    def probe(self) -> dict[str, Any]:
        """Liveness: 1-token chat ping; ok-dict or underlying denial."""
        self.chat("ping", max_tokens=1)
        return {
            "backend": self.name,
            "kind": "text",
            "model": self.model,
            "base_url": self.base_url,
            "ok": True,
        }
