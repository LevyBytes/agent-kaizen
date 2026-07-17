"""Stdlib OpenAI-compatible HTTP client (no ``openai`` SDK, no ``requests``).

Targets ``/v1/embeddings`` and ``/v1/chat/completions``, so it works against a local Ollama
server (``/v1``) and any remote OpenAI-compatible endpoint. The optional bearer key is passed
from env per request and never stored.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

from ..denials import KaizenDenied
from .http_retry import http_open, http_request


class OpenAICompatClient:
    """Stdlib OpenAI-compatible client; /v1/embeddings + /v1/chat/completions; key per-request/unstored; raises KaizenDenied (HTTP/UNAVAILABLE/MALFORMED)."""
    _STREAM_LINE_MAX_BYTES = 1024 * 1024

    def __init__(self, base_url: str, *, api_key: str | None = None, timeout: float = 60.0) -> None:
        """Base_url trailing slash stripped; timeout seconds."""
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Returns parsed JSON dict; DENIED_BACKEND_HTTP on 4xx/5xx post-retry, DENIED_BACKEND_UNAVAILABLE on transport failure."""
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
        """Order-preserving one-vector-per-text; raises _malformed on wrong data/embedding shape; element type unchecked (A/F2)."""
        resp = self._post("/embeddings", {"model": model, "input": texts})
        data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(data, list):
            raise self._malformed("/embeddings", "missing 'data' list in response")
        vectors: list[list[float]] = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise self._malformed("/embeddings", "item without an 'embedding' list in response data")
            vector = item["embedding"]
            if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in vector):
                raise self._malformed("/embeddings", "embedding values must be numeric")
            vectors.append([float(value) for value in vector])
        return vectors

    def chat(self, messages: list[dict[str, str]], model: str, **opts: Any) -> dict[str, Any]:
        """Non-stream single-turn; returns {text,usage,model}; normalizes to choices[0].message.content, "" if absent (F6); content not str-guarded (A1)."""
        payload: dict[str, Any] = {**opts, "model": model, "messages": messages, "stream": False}
        resp = self._post("/chat/completions", payload)
        if not isinstance(resp, dict):
            raise self._malformed("/chat/completions", "non-object response")
        choices = resp.get("choices", [])
        if isinstance(choices, list) and len(choices) > 1:
            raise self._malformed("/chat/completions", "multiple choices are unsupported")
        text = ""
        if choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message", {})
            content = message.get("content", "") if isinstance(message, dict) else ""
            text = content if isinstance(content, str) else ""
        usage = resp.get("usage", {})
        return {
            "text": text,
            "usage": usage if isinstance(usage, dict) else {},
            "model": resp.get("model", model),
        }

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        model: str,
        on_delta: Callable[[str], None],
        **opts: Any,
    ) -> dict[str, Any]:
        """Stream OpenAI SSE or Ollama-style JSONL and return the normalized complete reply.

        Connection/open failures may retry only before response-body bytes are consumed. Once streaming begins,
        failures are not replayed; exceptions from ``on_delta`` propagate immediately and stop the stream.
        """

        path = "/chat/completions"
        payload: dict[str, Any] = {"model": model, "messages": messages, **opts, "stream": True}
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        response_model = model
        saw_payload = False
        try:
            # Retry only the connect/open phase; once any response body is available it is never replayed.
            with http_open(req, timeout=self.timeout) as response:
                first_line = True
                while True:
                    raw = response.readline(self._STREAM_LINE_MAX_BYTES + 1)
                    if not raw:
                        break
                    if len(raw) > self._STREAM_LINE_MAX_BYTES:
                        raise self._malformed(path, "stream line exceeds 1 MiB")
                    try:
                        line = raw.decode("utf-8-sig" if first_line else "utf-8", "strict").strip()
                    except UnicodeDecodeError as error:
                        raise self._malformed(path, "invalid UTF-8 in streamed response") from error
                    first_line = False
                    if not line or line.startswith(":") or line.startswith("event:") or line.startswith("id:"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                        if not line:
                            continue
                    if line == "[DONE]":
                        break
                    try:
                        event = json.loads(line)
                    except (TypeError, ValueError) as error:
                        raise self._malformed(path, "invalid JSON in streamed response") from error
                    if not isinstance(event, dict):
                        raise self._malformed(path, "non-object item in streamed response")
                    choices = event.get("choices")
                    if isinstance(choices, list) and len(choices) > 1:
                        raise self._malformed(path, "multiple choices are unsupported")
                    saw_payload = True
                    fragment = self._stream_text(event)
                    if fragment:
                        text_parts.append(fragment)
                        try:
                            on_delta(fragment)
                        except Exception:  # noqa: BLE001 -- UI streaming is ephemeral, never fail the turn
                            pass
                    event_usage = event.get("usage")
                    if isinstance(event_usage, dict):
                        usage = event_usage
                    if isinstance(event.get("model"), str):
                        response_model = event["model"]
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
        except KaizenDenied:
            raise
        except Exception as error:  # noqa: BLE001 -- any transport failure means unreachable
            raise KaizenDenied(
                "DENIED_BACKEND_UNAVAILABLE",
                {
                    "base_url": self.base_url,
                    "reason": str(error),
                    "required_action": "start the model server (see setup/OLLAMA.md) or set KAIZEN_LLM_BASE_URL",
                },
                exit_code=2,
            ) from error
        if not saw_payload:
            raise self._malformed(path, "empty streamed response")
        return {"text": "".join(text_parts), "usage": usage, "model": response_model}

    @staticmethod
    def _stream_text(event: dict[str, Any]) -> str:
        """Extract one incremental fragment from OpenAI SSE or Ollama JSONL shapes."""

        choices = event.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            first = choices[0]
            delta = first.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                return delta["content"]
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
        message = event.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
        return event["response"] if isinstance(event.get("response"), str) else ""
