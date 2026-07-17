"""Stdlib OTLP receiver (v8 M-CLAUDE / M5b): synthetic OTLP JSON over a REAL loopback socket.

NO real ``claude`` and NO external network: the tests POST synthetic OTLP/HTTP-JSON bodies (shaped as
P-A L7 recorded -- BARE ``event.name``, the ``com.anthropic.claude_code.events`` scope, PII attributes)
to a live :class:`OtelReceiver` bound on an ephemeral 127.0.0.1 port, then assert the injected recorder
saw parsed + keyed events with PII stripped BEFORE it. Malformed and gzip bodies prove the never-raise +
robust-decode contract.

Contract -> proving test:
- bare event.name parsed + keyed .......... ParseLogsTest
- PII redacted before the recorder ........ RedactionTest
- live loopback POST /v1/logs ............. LiveServerTest
- metrics /v1/metrics parsed .............. LiveServerTest
- malformed body => 200, no raise ......... LiveServerTest
- gzip body handled ....................... LiveServerTest
- scope isolation (non-Claude dropped) .... ParseLogsTest
"""

from __future__ import annotations

import gzip
import http.client
import io
import json
import sys
import unittest
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path
from unittest import mock

from _harness import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT))

from kaizen_components.orchestration import otel_rx  # noqa: E402


# --- synthetic OTLP bodies --------------------------------------------------------------------

def log_record(event_name: str, extra_attrs: dict | None = None) -> dict:
    attrs = [
        {"key": "event.name", "value": {"stringValue": event_name}},
        {"key": "session.id", "value": {"stringValue": "sess-9"}},
        {"key": "prompt.id", "value": {"stringValue": "p-1"}},
        # PII that MUST be stripped before the recorder (P-A L7: every event carries these).
        {"key": "user.email", "value": {"stringValue": "levybytes01@gmail.com"}},
        {"key": "user.account_uuid", "value": {"stringValue": "uuid-abc-123"}},
        {"key": "organization.id", "value": {"stringValue": "org-777"}},
    ]
    for key, value in (extra_attrs or {}).items():
        attrs.append({"key": key, "value": {"stringValue": str(value)}})
    return {"timeUnixNano": "1700000000000000000", "attributes": attrs}


def logs_body(*records: dict, scope: str = otel_rx.CLAUDE_SCOPE) -> dict:
    return {
        "resourceLogs": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "claude-code"}}]},
            "scopeLogs": [{
                "scope": {"name": scope, "version": "2.1.195"},
                "logRecords": list(records),
            }],
        }],
    }


def metrics_body(*, scope: str = otel_rx.CLAUDE_SCOPE) -> dict:
    return {
        "resourceMetrics": [{
            "resource": {"attributes": []},
            "scopeMetrics": [{
                "scope": {"name": scope, "version": "2.1.195"},
                "metrics": [{
                    "name": "claude_code.token.usage",
                    "sum": {"dataPoints": [{
                        "asInt": "1234",
                        "timeUnixNano": "1700000000000000000",
                        "attributes": [
                            {"key": "type", "value": {"stringValue": "input"}},
                            {"key": "user.email", "value": {"stringValue": "levybytes01@gmail.com"}},
                        ],
                    }]},
                }],
            }],
        }],
    }


# --- pure parsing ------------------------------------------------------------------------------

class ParseLogsTest(unittest.TestCase):
    def test_bare_event_name_parsed_and_keyed(self) -> None:
        events = otel_rx.parse_logs_body(logs_body(log_record("tool_decision", {"decision": "accept", "tool_name": "Write"})))
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["event_name"], "tool_decision")  # BARE, not claude_code.tool_decision
        self.assertTrue(e["known"])
        self.assertEqual(e["scope"], otel_rx.CLAUDE_SCOPE)
        self.assertEqual(e["attributes"].get("decision"), "accept")
        self.assertEqual(e["attributes"].get("session.id"), "sess-9")

    def test_all_known_event_names(self) -> None:
        for name in ("user_prompt", "api_request", "tool_decision", "tool_result", "assistant_response", "mcp_server_connection"):
            events = otel_rx.parse_logs_body(logs_body(log_record(name)))
            self.assertEqual(events[0]["event_name"], name)
            self.assertTrue(events[0]["known"], name)

    def test_unknown_event_name_still_recorded_flagged(self) -> None:
        events = otel_rx.parse_logs_body(logs_body(log_record("some_future_event")))
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["known"])  # forward-compat: recorded but flagged

    def test_non_claude_scope_dropped(self) -> None:
        # An event under a DIFFERENT scope is not trusted (spoof guard).
        events = otel_rx.parse_logs_body(logs_body(log_record("tool_decision"), scope="com.someone.else"))
        self.assertEqual(events, [])

    def test_record_without_event_name_skipped(self) -> None:
        body = logs_body({"attributes": [{"key": "session.id", "value": {"stringValue": "s"}}]})
        self.assertEqual(otel_rx.parse_logs_body(body), [])

    def test_malformed_body_never_raises(self) -> None:
        # Every shape of garbage returns [] without raising.
        for junk in (None, {}, {"resourceLogs": "nope"}, {"resourceLogs": [None]},
                     {"resourceLogs": [{"scopeLogs": [{"logRecords": [42]}]}]}):
            self.assertEqual(otel_rx.parse_logs_body(junk), [])

    def test_metrics_parsed(self) -> None:
        points = otel_rx.parse_metrics_body(metrics_body())
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["metric"], "claude_code.token.usage")
        self.assertEqual(points[0]["value"], 1234)

    def test_non_claude_scope_metrics_remain_admitted_and_attributed(self) -> None:
        points = otel_rx.parse_metrics_body(metrics_body(scope="com.someone.else"))
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["scope"], "com.someone.else")


# --- redaction ---------------------------------------------------------------------------------

class RedactionTest(unittest.TestCase):
    def test_pii_stripped_before_recorder(self) -> None:
        events = otel_rx.parse_logs_body(logs_body(log_record("user_prompt")))
        attrs = events[0]["attributes"]
        self.assertNotIn("user.email", attrs)
        self.assertNotIn("user.account_uuid", attrs)
        self.assertNotIn("organization.id", attrs)
        # Non-PII correlators are KEPT (they are what make the event useful).
        self.assertEqual(attrs.get("session.id"), "sess-9")
        self.assertEqual(attrs.get("prompt.id"), "p-1")

    def test_free_text_fields_are_dropped(self) -> None:
        for key in ("prompt", "prompt.text", "message", "response", "content", "event.body"):
            with self.subTest(key=key):
                events = otel_rx.parse_logs_body(logs_body(log_record("user_prompt", {key: "my secret prompt text"})))
                self.assertNotIn(key, events[0]["attributes"])

    def test_secretlike_value_redacted_even_if_unnamed(self) -> None:
        # A value that smells like a secret is nulled even under an attr key we did not name.
        events = otel_rx.parse_logs_body(logs_body(log_record("api_request", {"some_field": "sk-ant-aaaaaaaaaaaaaaaaaaaa"})))
        self.assertEqual(events[0]["attributes"].get("some_field"), "[REDACTED]")

    def test_redact_attrs_does_not_mutate_input(self) -> None:
        src = {"user.email": "x@y.com", "keep": "v"}
        out = otel_rx.redact_attrs(src)
        self.assertIn("user.email", src)  # input untouched
        self.assertNotIn("user.email", out)
        self.assertEqual(out["keep"], "v")

    def test_metrics_attrs_redacted(self) -> None:
        points = otel_rx.parse_metrics_body(metrics_body())
        self.assertNotIn("user.email", points[0]["attributes"])
        self.assertEqual(points[0]["attributes"].get("type"), "input")

    def test_structured_values_and_body_are_redacted_recursively(self) -> None:
        attrs = {"nested": {"user.email": "person@gmail.com", "token": "AKIAIOSFODNN7EXAMPLE"}}
        clean = otel_rx.redact_attrs(attrs)
        self.assertNotIn("user.email", clean["nested"])
        self.assertEqual(clean["nested"]["token"], "[REDACTED]")
        record = log_record("tool_result")
        record["body"] = {"kvlistValue": {"values": [
            {"key": "secret", "value": {"stringValue": "AKIAIOSFODNN7EXAMPLE"}},
        ]}}
        event = otel_rx.parse_logs_body(logs_body(record))[0]
        self.assertEqual(event["body"]["secret"], "[REDACTED]")


class BodyBoundTest(unittest.TestCase):
    def test_gunzip_requests_only_cap_plus_one_bytes(self) -> None:
        stream = mock.MagicMock()
        stream.__enter__.return_value.read.return_value = b"x" * 9
        with mock.patch.object(otel_rx, "_MAX_DECOMPRESSED_BYTES", 8), \
             mock.patch.object(otel_rx.gzip, "GzipFile", return_value=stream):
            self.assertEqual(otel_rx._gunzip(b"compressed"), b"x" * 8)
        stream.__enter__.return_value.read.assert_called_once_with(9)

    def test_declared_chunk_size_is_clamped_before_read(self) -> None:
        handler_type = otel_rx._make_handler(lambda _kind, _event: None, lambda _message: None)
        handler = object.__new__(handler_type)
        handler.rfile = io.BytesIO(b"ffffffff\r\n" + b"x" * 9)
        with mock.patch.object(otel_rx, "_MAX_BODY_BYTES", 8):
            body = handler._read_chunked()
        self.assertEqual(body, b"x" * 9)


# --- live loopback server ----------------------------------------------------------------------

class LiveServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.seen: list = []
        self.rx = otel_rx.OtelReceiver(lambda kind, ev: self.seen.append((kind, ev)), port=0)
        info = self.rx.start()
        self.addCleanup(self.rx.stop)
        self.base = "http://" + info["address"]

    def _post(self, path: str, data: bytes, headers: dict) -> tuple[int, str]:
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")

    def _connection(self) -> http.client.HTTPConnection:
        parsed = urlsplit(self.base)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        self.addCleanup(connection.close)
        return connection

    def test_logs_post_records_redacted_event(self) -> None:
        status, _ = self._post("/v1/logs", json.dumps(logs_body(log_record("tool_decision"))).encode("utf-8"),
                               {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        logs = [e for kind, e in self.seen if kind == "log"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["event_name"], "tool_decision")
        self.assertNotIn("user.email", logs[0]["attributes"])  # redacted BEFORE the recorder

    def test_metrics_post_records_point(self) -> None:
        status, _ = self._post("/v1/metrics", json.dumps(metrics_body()).encode("utf-8"),
                               {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        metrics = [e for kind, e in self.seen if kind == "metric"]
        self.assertEqual(len(metrics), 1)
        self.assertNotIn("user.email", metrics[0]["attributes"])

    def test_malformed_body_returns_200_no_raise(self) -> None:
        status, resp = self._post("/v1/logs", b"{not valid json", {"Content-Type": "application/json"})
        self.assertEqual(status, 200)  # a real OTLP endpoint 200s + partialSuccess, never 500
        self.assertIn("partialSuccess", resp)
        self.assertEqual(self.seen, [])  # nothing recorded from garbage

    def test_gzip_body_handled(self) -> None:
        raw = json.dumps(logs_body(log_record("assistant_response"))).encode("utf-8")
        status, _ = self._post("/v1/logs", gzip.compress(raw),
                               {"Content-Type": "application/json", "Content-Encoding": "gzip"})
        self.assertEqual(status, 200)
        logs = [e for kind, e in self.seen if kind == "log"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["event_name"], "assistant_response")

    def test_chunked_body_is_parsed_live(self) -> None:
        raw = json.dumps(logs_body(log_record("tool_result"))).encode("utf-8")
        connection = self._connection()
        connection.request("POST", "/v1/logs", body=[raw], headers={"Content-Type": "application/json"}, encode_chunked=True)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        self.assertEqual([event[1]["event_name"] for event in self.seen], ["tool_result"])

    def test_oversized_content_length_is_rejected_before_body_read(self) -> None:
        connection = self._connection()
        connection.putrequest("POST", "/v1/logs")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(otel_rx._MAX_BODY_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        self.assertEqual(self.seen, [])

    def test_recorder_exception_does_not_500(self) -> None:
        # A recorder that raises must not break the endpoint (enrichment must not fail the ingest).
        def boom(_kind, _ev):
            raise RuntimeError("recorder bug")

        rx = otel_rx.OtelReceiver(boom, port=0)
        info = rx.start()
        self.addCleanup(rx.stop)
        req = urllib.request.Request(
            "http://" + info["address"] + "/v1/logs",
            data=json.dumps(logs_body(log_record("tool_result"))).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)  # recorder blew up, endpoint still 200s

    def test_get_health_returns_200(self) -> None:
        with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
            self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
