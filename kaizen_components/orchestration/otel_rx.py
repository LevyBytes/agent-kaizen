r"""Stdlib OTLP receiver for the Claude Code telemetry bus (v8 M-CLAUDE / M5b, plan §5.4).

Claude Code exports its full session lifecycle over OpenTelemetry when
``CLAUDE_CODE_ENABLE_TELEMETRY=1`` + ``OTEL_EXPORTER_OTLP_PROTOCOL=http/json``. The probe
(``AI/work/orchestration-v8/probes/P-A-claude/verdict.md``) proved this is a REAL,
stdlib-ingestable bus: it POSTs **plain uncompressed JSON** to ``POST /v1/logs`` and
``POST /v1/metrics`` -- so this receiver is a plain :class:`http.server.ThreadingHTTPServer`
with NO protobuf / grpc / opentelemetry dependency.

DELTA the probe pinned (fold verbatim): the on-wire ``event.name`` attribute is **BARE**
(``user_prompt``, ``api_request``, ``tool_decision``, ``tool_result``, ``assistant_response``,
``mcp_server_connection``) -- the ``claude_code.`` namespace lives in the instrumentation
SCOPE (``com.anthropic.claude_code.events``), NOT in the event name. This receiver keys on
``event.name`` + scope, never a prefixed literal. Every event carries PII (``user.email``,
``user.account_uuid``, ``user.id``, ``organization.id``, plus prompt/response text) so a
REDACTION pass strips it BEFORE the injected ``recorder`` ever sees an event.

Invariants:
- ENRICHMENT-ONLY. The daemon must run fine without this receiver; it is never a dependency.
  Nothing here writes the DB -- it hands redacted events to an injected ``recorder`` callback
  the daemon owns (the daemon decides whether/how to funnel them).
- NEVER raises on malformed input. A real OTLP endpoint answers 200 with an
  ``{"partialSuccess":{}}`` body even on a body it could not fully parse; so does this. A
  handler that 500s would make Claude's exporter retry-storm.
- gzip + chunked transfer-encoding handled DEFENSIVELY. The probe saw plain JSON, but a future
  Claude build (or an intermediary) may compress or chunk; decode both before parsing.
- 127.0.0.1 ONLY. The bind is loopback; the wrapper points Claude's exporter at it.

Purity: stdlib + :mod:`redaction` only (for the secret-scan reuse). No subprocess/socket beyond
``http.server``; no DB. The daemon constructs :class:`OtelReceiver`, injects a recorder, and calls
``start()`` / ``stop()`` (mirrors the M11 control-http lifecycle).
"""

from __future__ import annotations

import gzip
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .. import redaction

# The instrumentation scope Claude Code stamps on its events. The receiver only trusts events
# under this scope so an unrelated OTLP producer on the same port cannot inject Claude-shaped events.
CLAUDE_SCOPE = "com.anthropic.claude_code.events"

# Bare on-wire event.name values from the provider probe. Kept as the known set for classification; an unknown name is
# still recorded (forward-compat) but flagged so a new Claude event type surfaces rather than vanishing.
KNOWN_EVENT_NAMES: frozenset[str] = frozenset({
    "user_prompt",
    "api_request",
    "tool_decision",
    "tool_result",
    "assistant_response",
    "mcp_server_connection",
})

# PII attribute keys stripped WHOLESALE before the recorder because every probed event carries these. The
# values are dropped (not hashed) -- the record plane keeps the event's SHAPE + non-PII attrs, never the
# identity. Prompt/response free-text keys are here too (they carry the user's content verbatim).
_PII_ATTR_KEYS: frozenset[str] = frozenset({
    "user.email",
    "user.account_uuid",
    "user.id",
    "organization.id",
    "user.name",
    # Free-text content fields (prompt/response bodies). Not identity per se, but user content that must
    # not durably land in a record (README "private by default").
    "prompt",
    "prompt.text",
    "message",
    "message.text",
    "response",
    "response.text",
    "content",
    "event.body",
})

# Cap a single request body so a hostile/broken client cannot stream unbounded bytes into memory.
_MAX_BODY_BYTES = 8 << 20  # 8 MiB
_MAX_DECOMPRESSED_BYTES = 64 << 20  # 64 MiB (gzip bomb guard)


# --- OTLP JSON parsing -------------------------------------------------------------------------

def _attr_value(value: Any) -> Any:
    """Unwrap an OTLP AnyValue ({"stringValue"|"intValue"|"boolValue"|"doubleValue"|...: v}) to a plain
    Python scalar. A non-dict (already-plain) value passes through. Unknown wrappers return the raw dict
    (never raises)."""
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "boolValue"):
        if key in value:
            return value[key]
    if "intValue" in value:
        raw = value["intValue"]
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if "doubleValue" in value:
        return value["doubleValue"]
    if "arrayValue" in value:
        arr = value["arrayValue"].get("values", []) if isinstance(value["arrayValue"], dict) else []
        return [_attr_value(v) for v in arr]
    if "kvlistValue" in value:
        return _attrs_to_dict(value["kvlistValue"].get("values", []) if isinstance(value["kvlistValue"], dict) else [])
    return value


def _attrs_to_dict(attributes: Any) -> dict[str, Any]:
    """OTLP attributes ([{"key":k,"value":AnyValue}, ...]) -> a flat dict. Tolerant of a malformed entry
    (skipped, never raised)."""
    out: dict[str, Any] = {}
    if not isinstance(attributes, list):
        return out
    for attr in attributes:
        if not isinstance(attr, dict):
            continue
        key = attr.get("key")
        if not isinstance(key, str):
            continue
        out[key] = _attr_value(attr.get("value"))
    return out


def redact_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Strip PII BEFORE the recorder. Drops every ``_PII_ATTR_KEYS`` key wholesale, then runs each
    remaining STRING value through the secret scanner (:func:`redaction.scan_for_secrets`) and nulls any
    that still smell like a secret/personal-path/email (belt-and-suspenders for a value we did not name
    explicitly). Returns a NEW dict (never mutates the input)."""
    clean: dict[str, Any] = {}
    for key, value in attrs.items():
        if key in _PII_ATTR_KEYS:
            continue
        clean[key] = _redact_value(value)
    return clean


def _redact_value(value: Any) -> Any:
    """Recursively redact strings and named PII fields in structured OTLP values."""
    if isinstance(value, str):
        return "[REDACTED]" if redaction.scan_for_secrets(value) else value
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items() if key not in _PII_ATTR_KEYS}
    return value


def parse_logs_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse an OTLP ExportLogsServiceRequest JSON body into a list of normalized, REDACTED events.

    Walks resourceLogs -> scopeLogs -> logRecords. Each event dict is:
      {event_name, scope, known, severity, time_unix_nano, attributes(REDACTED),
       resource_attributes(REDACTED), body}
    ``event_name`` is the BARE ``event.name`` attribute. Only records under the Claude scope
    (or a scope-less record, defensively) are emitted. NEVER raises -- a malformed sub-node is skipped."""
    events: list[dict[str, Any]] = []
    if not isinstance(body, dict):
        return events
    for resource_log in _as_list(body.get("resourceLogs")):
        resource_attrs = {}
        resource = resource_log.get("resource") if isinstance(resource_log, dict) else None
        if isinstance(resource, dict):
            resource_attrs = redact_attrs(_attrs_to_dict(resource.get("attributes")))
        for scope_log in _as_list(resource_log.get("scopeLogs") if isinstance(resource_log, dict) else None):
            scope = scope_log.get("scope") if isinstance(scope_log, dict) else None
            scope_name = str(scope.get("name")) if isinstance(scope, dict) and scope.get("name") else ""
            # Trust only the Claude scope (or a scope-less record, which we treat as unattributed but
            # still parse defensively -- an empty scope name is not a spoof of the Claude scope).
            if scope_name and scope_name != CLAUDE_SCOPE:
                continue
            for record in _as_list(scope_log.get("logRecords") if isinstance(scope_log, dict) else None):
                event = _parse_log_record(record, scope_name, resource_attrs)
                if event is not None:
                    events.append(event)
    return events


def _parse_log_record(record: Any, scope_name: str, resource_attrs: dict[str, Any]) -> dict[str, Any] | None:
    """Private; parses one OTLP logRecord -> redacted event dict or None (skips non-dict / missing/empty `event.name`). Note the load-bearing redaction steps: drops `event.name` from the attr bag (183), secret-scans `body` (185)."""
    if not isinstance(record, dict):
        return None
    attrs = _attrs_to_dict(record.get("attributes"))
    event_name = attrs.get("event.name")
    # event.name is the keying axis. A record without it is not a Claude lifecycle event; skip.
    if not isinstance(event_name, str) or not event_name:
        return None
    redacted = redact_attrs(attrs)
    redacted.pop("event.name", None)  # keyed separately; don't duplicate into the attr bag
    body_field = _attr_value(record.get("body")) if "body" in record else None
    body_field = _redact_value(body_field)
    return {
        "event_name": event_name,
        "scope": scope_name or None,
        "known": event_name in KNOWN_EVENT_NAMES,
        "severity": record.get("severityText") or record.get("severityNumber"),
        "time_unix_nano": record.get("timeUnixNano")
        if record.get("timeUnixNano") is not None else record.get("observedTimeUnixNano"),
        "attributes": redacted,
        "resource_attributes": dict(resource_attrs),
        "body": body_field,
    }


def parse_metrics_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse an OTLP ExportMetricsServiceRequest JSON body into normalized, REDACTED metric points.
    Metrics are enrichment counters (token counts, cost, tool durations); we surface {metric, scope,
    attributes(REDACTED), value} per data point. NEVER raises."""
    points: list[dict[str, Any]] = []
    if not isinstance(body, dict):
        return points
    for resource_metric in _as_list(body.get("resourceMetrics")):
        for scope_metric in _as_list(resource_metric.get("scopeMetrics") if isinstance(resource_metric, dict) else None):
            scope = scope_metric.get("scope") if isinstance(scope_metric, dict) else None
            scope_name = str(scope.get("name")) if isinstance(scope, dict) and scope.get("name") else ""
            for metric in _as_list(scope_metric.get("metrics") if isinstance(scope_metric, dict) else None):
                if not isinstance(metric, dict):
                    continue
                name = str(metric.get("name") or "")
                for dp in _metric_data_points(metric):
                    attrs = redact_attrs(_attrs_to_dict(dp.get("attributes")))
                    points.append({
                        "metric": name,
                        "scope": scope_name or None,
                        "attributes": attrs,
                        "value": _metric_value(dp),
                        "time_unix_nano": dp.get("timeUnixNano"),
                    })
    return points


def _metric_value(dp: dict[str, Any]) -> Any:
    """A data point's numeric value. OTLP/JSON encodes int64 as a STRING (``asInt``), so coerce it to
    ``int``; ``asDouble`` is already a JSON number. Missing/unparseable => None (never raises)."""
    if "asInt" in dp:
        try:
            return int(dp["asInt"])
        except (TypeError, ValueError):
            return dp["asInt"]
    return dp.get("asDouble")


def _metric_data_points(metric: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns the dataPoints list for the FIRST present metric-container kind (sum/gauge/histogram/expHistogram/summary); [] otherwise."""
    for key in ("sum", "gauge", "histogram", "exponentialHistogram", "summary"):
        container = metric.get(key)
        if isinstance(container, dict):
            return [dp for dp in _as_list(container.get("dataPoints")) if isinstance(dp, dict)]
    return []


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# --- HTTP server -------------------------------------------------------------------------------

# Recorder signature: recorder(kind, event) where kind is 'log' | 'metric' and event is a parsed,
# REDACTED dict. The daemon injects one; a test injects a list.append closure.
Recorder = Callable[[str, dict[str, Any]], None]


class OtelReceiver:
    """A loopback OTLP/HTTP-JSON receiver for the Claude telemetry bus. Constructed by the daemon with
    an injected ``recorder`` and started/stopped like the M11 control service. Enrichment-only: a start
    failure logs and degrades to no-receiver; the daemon runs regardless.

    ``address`` is ``host:port`` after :meth:`start` (port 0 binds an ephemeral port; the wrapper reads
    the chosen port from the daemon's status or an addr file the daemon writes)."""

    def __init__(
        self,
        recorder: Recorder,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.recorder = recorder
        self.host = host
        self.port = port
        self.logger = logger or (lambda _m: None)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.address: str | None = None

    def start(self) -> dict[str, Any]:
        """Binds + serves on a daemon thread; returns `{"active":True,"address":host:port}`. Ephemeral-port behavior currently only in class docstring."""
        handler = _make_handler(self.recorder, self.logger)
        server = ThreadingHTTPServer((self.host, self.port), handler)
        server.daemon_threads = True
        host, port = server.server_address[0], server.server_address[1]
        self.address = f"{host}:{port}"
        self._server = server
        thread = threading.Thread(target=server.serve_forever, name="kaizen-otel-rx", daemon=True)
        thread.start()
        self._thread = thread
        self.logger(f"otel receiver started at {self.address}")
        return {"active": True, "address": self.address}

    def stop(self) -> None:
        """Idempotent shutdown."""
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # noqa: BLE001 -- teardown must not raise past shutdown
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def _make_handler(recorder: Recorder, logger: Callable[[str], None]):
    """Build a fail-open OTLP handler class bound to the concurrent recorder and logger."""
    class _OtelHandler(BaseHTTPRequestHandler):
        """Always-200 OTLP handler that suppresses default access logs and reports partial success."""
        # Silence BaseHTTPRequestHandler's default stderr access log (keeps the daemon's stderr clean;
        # the daemon's own logger is the audit path).
        def log_message(self, _format: str, *_args: Any) -> None:  # noqa: N802
            return

        def do_POST(self) -> None:  # noqa: N802
            """Request entrypoint; always-200 (`{}` on parse, `partialSuccess` otherwise), routes `/v1/logs` and `/v1/metrics`, never 500s. Contract currently only in scattered inline comments."""
            path = (self.path or "").split("?", 1)[0].rstrip("/")
            body = self._read_body()
            parsed_ok = False
            try:
                if body is not None:
                    doc = json.loads(body.decode("utf-8"))
                    if path.endswith("/v1/logs"):
                        for event in parse_logs_body(doc):
                            _safe_record(recorder, "log", event, logger)
                        parsed_ok = True
                    elif path.endswith("/v1/metrics"):
                        for point in parse_metrics_body(doc):
                            _safe_record(recorder, "metric", point, logger)
                        parsed_ok = True
            except Exception as error:  # noqa: BLE001 -- NEVER 500; a real OTLP endpoint 200s + partialSuccess
                logger(f"otel parse error (returning 200 partialSuccess): {type(error).__name__}: {error}")
            # OTLP/HTTP success shape is an ExportServiceResponse; an empty object (or partialSuccess) is
            # a valid 200. We always 200 so Claude's exporter never retry-storms on our parse trouble.
            self._respond_200(parsed_ok)

        # Any GET is a liveness probe and returns 200; unimplemented verbs retain the base handler's 501.
        def do_GET(self) -> None:  # noqa: N802
            self._respond_200(True)

        def _read_body(self) -> bytes | None:
            try:
                length_header = self.headers.get("Content-Length")
                encoding = (self.headers.get("Content-Encoding") or "").lower()
                transfer = (self.headers.get("Transfer-Encoding") or "").lower()
                if "chunked" in transfer:
                    raw = self._read_chunked()
                elif length_header is not None:
                    length = int(length_header)
                    if length < 0 or length > _MAX_BODY_BYTES:
                        return None
                    raw = self.rfile.read(length)
                else:
                    raw = b""
                if "gzip" in encoding and raw:
                    raw = _gunzip(raw)
                return raw
            except Exception as error:  # noqa: BLE001 -- a body read/decompress failure => treat as empty, still 200
                logger(f"otel body read error (treated as empty): {type(error).__name__}")
                return None

        def _read_chunked(self) -> bytes:
            """Minimal HTTP/1.1 chunked-body reader (defensive: the probe saw Content-Length, but an
            intermediary may re-chunk). Bounded by _MAX_BODY_BYTES."""
            buf = bytearray()
            while True:
                size_line = self.rfile.readline(64)
                if not size_line:
                    break
                size_str = size_line.split(b";", 1)[0].strip()
                try:
                    size = int(size_str, 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()  # consume trailing CRLF after the last chunk
                    break
                remaining = _MAX_BODY_BYTES - len(buf)
                to_read = min(size, remaining + 1)
                chunk = self.rfile.read(to_read)
                buf.extend(chunk)
                if size > to_read or len(buf) > _MAX_BODY_BYTES:
                    break
                self.rfile.readline()  # consume the CRLF after each chunk
            return bytes(buf)

        def _respond_200(self, parsed_ok: bool) -> None:
            payload = b"{}" if parsed_ok else b'{"partialSuccess":{}}'
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception:  # noqa: BLE001 -- a broken client socket is not our problem
                pass

    return _OtelHandler


def _safe_record(recorder: Recorder, kind: str, event: dict[str, Any], logger: Callable[[str], None]) -> None:
    """Hand one parsed+redacted event to the recorder; a recorder exception is logged, never propagated
    (enrichment must not break the ingest loop)."""
    try:
        recorder(kind, event)
    except Exception as error:  # noqa: BLE001 -- a recorder bug must not 500 the endpoint
        logger(f"otel recorder error: {type(error).__name__}: {error}")


def _gunzip(raw: bytes) -> bytes:
    """Decompresses gzip bytes truncating to `_MAX_DECOMPRESSED_BYTES`; must document (and F1 fix) that it currently inflates fully before truncating."""
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
        data = compressed.read(_MAX_DECOMPRESSED_BYTES + 1)
    if len(data) > _MAX_DECOMPRESSED_BYTES:
        return data[:_MAX_DECOMPRESSED_BYTES]
    return data
