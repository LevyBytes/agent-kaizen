"""Ollama-backed embedding + advisory-text backends (local default, OpenAI-compatible).

Defaults to a local Ollama server's OpenAI-compatible ``/v1`` API but works against any remote
OpenAI-compatible endpoint via env. Embeddings fall back to Ollama's native ``/api/embeddings``
when the ``/v1`` route is unavailable.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..denials import KaizenDenied
from .http_retry import http_request
from .openai_compat import OpenAICompatClient


class OllamaEmbeddingBackend:
    name = "ollama"

    def __init__(self, *, base_url: str, model: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.client = OpenAICompatClient(self.base_url, api_key=api_key)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            vectors = self.client.embeddings(texts, self.model)
            if vectors:
                return vectors
        except KaizenDenied:
            pass  # /v1 unavailable (e.g. older Ollama) -> try the native endpoint
        return self._native_embed(texts)

    def _native_embed(self, texts: list[str]) -> list[list[float]]:
        root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        vectors: list[list[float]] = []
        for text in texts:
            data = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
            req = urllib.request.Request(
                root + "/api/embeddings", data=data, headers={"Content-Type": "application/json"}
            )
            try:
                payload = json.loads(http_request(req, timeout=60).decode("utf-8"))
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
            vectors.append(payload.get("embedding", []))
        return vectors

    def probe(self) -> dict[str, Any]:
        vectors = self.embed(["probe"])
        dimension = len(vectors[0]) if vectors and vectors[0] else 0
        return {"backend": self.name, "kind": "embedding", "model": self.model, "base_url": self.base_url, "dimension": dimension}


class OllamaTextBackend:
    name = "ollama"

    def __init__(self, *, base_url: str, model: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.client = OpenAICompatClient(self.base_url, api_key=api_key)

    def chat(self, prompt: str, **opts: Any) -> dict[str, Any]:
        return self.client.chat([{"role": "user", "content": prompt}], self.model, **opts)

    def probe(self) -> dict[str, Any]:
        self.chat("ping", max_tokens=1)
        return {"backend": self.name, "kind": "text", "model": self.model, "base_url": self.base_url, "ok": True}
