"""Bounded retry for backend HTTP calls (Ollama, OpenAI-compatible, ComfyUI).

One transient timeout used to abort a whole E3/B3/Y1 run because every backend
client called ``urlopen`` exactly once. This helper retries transient failures
(connection errors, timeouts, HTTP 5xx/429) on a short deterministic schedule and
fast-fails everything else: 4xx means a wrong model/endpoint, SSL and DNS failures
mean a wrong configuration, and neither gets better by retrying.
DB writes have their own retry (``db_retry``); this covers the network seam only.
"""

from __future__ import annotations

import email.utils
import socket
import ssl
import time
import urllib.error
import urllib.request

# Deterministic backoff: ~3.5s worst case on top of the caller's timeouts, matching
# the bounded-retry posture of db_retry (never an unbounded or jittered schedule).
# A 429 with a Retry-After header may stretch a single wait to RETRY_AFTER_CAP
# seconds (~15s absolute worst case) — but only when the server explicitly asks.
RETRY_DELAYS = (0.5, 1.0, 2.0)
RETRY_AFTER_CAP = 5.0

_TRANSIENT_ERRORS = (TimeoutError, ConnectionError, socket.timeout)


def _is_transient(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code >= 500 or error.code == 429
    if isinstance(error, (ssl.SSLError, socket.gaierror)):
        # TLS negotiation and DNS resolution failures are configuration problems.
        return False
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, (ssl.SSLError, socket.gaierror)):
            return False
        return isinstance(reason, _TRANSIENT_ERRORS)
    return isinstance(error, _TRANSIENT_ERRORS)


def _retry_after_seconds(error: Exception | None) -> float | None:
    """Server-requested wait for a 429, clamped to RETRY_AFTER_CAP; None otherwise."""
    if not (isinstance(error, urllib.error.HTTPError) and error.code == 429):
        return None
    header = (error.headers.get("Retry-After") or "").strip() if error.headers else ""
    if not header:
        return None
    if header.isdigit():
        return min(float(header), RETRY_AFTER_CAP)
    try:
        parsed = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return min(max(parsed.timestamp() - time.time(), 0.0), RETRY_AFTER_CAP)


def http_request(req: urllib.request.Request, *, timeout: float) -> bytes:
    """Open ``req`` and return the response body, retrying transient failures."""
    last_error: Exception | None = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (loopback default)
                return resp.read()
        except Exception as error:  # noqa: BLE001 -- classified above
            if not _is_transient(error):
                raise
            last_error = error
        if attempt < len(RETRY_DELAYS):
            time.sleep(_retry_after_seconds(last_error) or RETRY_DELAYS[attempt])
    assert last_error is not None
    raise last_error
