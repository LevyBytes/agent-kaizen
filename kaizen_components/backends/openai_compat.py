"""Stdlib OpenAI-compatible HTTP client (no ``openai`` SDK, no ``requests``).

Targets ``/v1/embeddings`` and ``/v1/chat/completions``, so it works against a local Ollama
server (``/v1``) and any remote OpenAI-compatible endpoint. The optional bearer key is passed
from env per request and never stored.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..denials import KaizenDenied
from .http_retry import http_request


class OpenAICompatClient:
    def __init__(self, base_url: str, *, api_key: str | None = None, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        try:
            return json.loads(http_request(req, timeout=self.timeout).decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace") if error.fp else ""
            raise KaizenDenied(
                "DENIED_BACKEND_HTTP",
                {
                    "base_url": self.base_url,
                    "path": path,
                    "http_status": error.code,
                    "reason": detail[:500],
                    "required_action": "check the model name + endpoint (see setup/OLLAMA.md)",
                },
                exit_code=2,
            ) from error
        except Exception as error:  # noqa: BLE001 -- any transport failure means "unreachable"
            raise KaizenDenied(
                "DENIED_BACKEND_UNAVAILABLE",
                {
                    "base_url": self.base_url,
                    "reason": str(error),
                    "required_action": "start the model server (see setup/OLLAMA.md) or set KAIZEN_EMBED_BASE_URL / KAIZEN_LLM_BASE_URL",
                },
                exit_code=2,
            ) from error

    def _malformed(self, path: str, detail: str) -> KaizenDenied:
        return KaizenDenied(
            "DENIED_BACKEND_MALFORMED",
            {
                "base_url": self.base_url,
                "path": path,
                "reason": detail,
                "required_action": "the endpoint responded but not with the OpenAI-compatible shape; check the URL and model",
            },
            exit_code=2,
        )

    def embeddings(self, texts: list[str], model: str) -> list[list[float]]:
        resp = self._post("/embeddings", {"model": model, "input": texts})
        data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(data, list):
            raise self._malformed("/embeddings", "missing 'data' list in response")
        vectors: list[list[float]] = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise self._malformed("/embeddings", "item without an 'embedding' list in response data")
            vectors.append(item["embedding"])
        return vectors

    def chat(self, messages: list[dict[str, str]], model: str, **opts: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        payload.update(opts)
        resp = self._post("/chat/completions", payload)
        if not isinstance(resp, dict):
            raise self._malformed("/chat/completions", "non-object response")
        choices = resp.get("choices", [])
        text = ""
        if choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message", {})
            text = message.get("content", "") if isinstance(message, dict) else ""
        usage = resp.get("usage", {})
        return {
            "text": text,
            "usage": usage if isinstance(usage, dict) else {},
            "model": resp.get("model", model),
        }
