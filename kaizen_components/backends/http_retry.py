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
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone
from typing import Callable

# Deterministic backoff: ~3.5s worst case on top of the caller's timeouts, matching
# the bounded-retry posture of db_retry (never an unbounded or jittered schedule).
# A 429 with a Retry-After header may stretch a single wait to RETRY_AFTER_CAP
# seconds (~15s absolute worst case) — but only when the server explicitly asks.
RETRY_DELAYS = (0.5, 1.0, 2.0)
RETRY_AFTER_CAP = 5.0

_TRANSIENT_ERRORS = (TimeoutError, ConnectionError)


def _effective_retry_delays() -> tuple[float, ...]:
    """The isolated Test Extension child can disable HTTP retries; all other values keep defaults."""
    return () if os.environ.get("KAIZEN_HTTP_RETRIES") == "0" else RETRY_DELAYS


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
    header = (error.headers.get("Retry-After") or "").strip()
    if not header:
        return None
    if header.isdigit():
        return min(float(header), RETRY_AFTER_CAP)
    try:
        parsed = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return min(max(parsed.timestamp() - time.time(), 0.0), RETRY_AFTER_CAP)


def http_open(
    req: urllib.request.Request,
    *,
    timeout: float,
    redirect_validator: Callable[[str], None] | None = None,
):
    """Open ``req`` with transient retries and return a response the caller must close.

    Retries stop as soon as a response is returned, so streaming callers never replay a partially consumed
    body. When supplied, ``redirect_validator`` receives every resolved redirect target before it is followed
    and may reject it.
    """
    opener: Callable[..., object] = urllib.request.urlopen
    if redirect_validator is not None:
        class ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, headers, newurl):
                target = urllib.parse.urljoin(request.full_url, newurl)
                redirect_validator(target)
                return super().redirect_request(request, fp, code, msg, headers, target)

        opener = urllib.request.build_opener(ValidatingRedirectHandler()).open
    last_error: Exception | None = None
    retry_delays = _effective_retry_delays()
    for attempt in range(len(retry_delays) + 1):
        try:
            return opener(req, timeout=timeout)  # noqa: S310 -- caller or redirect_validator fixes the endpoint
        except Exception as error:  # noqa: BLE001 -- classified above
            if not _is_transient(error):
                raise
            last_error = error
        if attempt < len(retry_delays):
            requested_delay = _retry_after_seconds(last_error)
            if isinstance(last_error, urllib.error.HTTPError):
                last_error.close()
            time.sleep(retry_delays[attempt] if requested_delay is None else requested_delay)
    if last_error is None:  # Defensive invariant if the retry loop is refactored.
        raise RuntimeError("HTTP retry loop exited without a response or error")
    raise last_error


def http_request(
    req: urllib.request.Request,
    *,
    timeout: float,
    redirect_validator: Callable[[str], None] | None = None,
) -> bytes:
    """Open req, validate each redirect target when requested, and return the body with transient retries."""
    with http_open(req, timeout=timeout, redirect_validator=redirect_validator) as resp:
        return resp.read()
