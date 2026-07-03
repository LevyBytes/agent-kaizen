"""Bounded retry for backend HTTP calls (Ollama, OpenAI-compatible, ComfyUI).

One transient timeout used to abort a whole E3/B3/Y1 run because every backend
client called ``urlopen`` exactly once. This helper retries transient failures
(connection errors, timeouts, HTTP 5xx/429) on a short deterministic schedule and
fast-fails everything else (4xx means a wrong model/endpoint, not flakiness).
DB writes have their own retry (``db_retry``); this covers the network seam only.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request

# Deterministic backoff: ~3.5s worst case on top of the caller's timeouts, matching
# the bounded-retry posture of db_retry (never an unbounded or jittered schedule).
RETRY_DELAYS = (0.5, 1.0, 2.0)


def _is_transient(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code >= 500 or error.code == 429
    # URLError, socket.timeout, ConnectionError, OSError: all transport-level.
    return True


def http_request(req: urllib.request.Request, *, timeout: float) -> bytes:
    """Open ``req`` and return the response body, retrying transient failures."""
    last_error: Exception | None = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (loopback default)
                return resp.read()
        except Exception as error:  # noqa: BLE001 -- classified below
            if not _is_transient(error):
                raise
            last_error = error
        if attempt < len(RETRY_DELAYS):
            time.sleep(RETRY_DELAYS[attempt])
    assert last_error is not None
    raise last_error
