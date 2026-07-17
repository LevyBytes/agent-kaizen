"""P0 loopback response bounds and concurrent long-poll transport regressions."""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from _harness import REPO_ROOT

from kaizen_components import db
from kaizen_components.orchestration import loopback
from kaizen_components.orchestration.supervisor import Supervisor


TOKEN = "p0-transport-token"


def _tcp_exchange(
    server: loopback.LoopbackServer,
    op: str,
    args: dict[str, object],
    *,
    timeout: float = 10.0,
) -> tuple[dict[str, object], bytes]:
    """Send one authenticated JSON-lines request over the server's real TCP socket."""
    if server.transport != "tcp" or not server.address:
        raise AssertionError("fixture requires the TCP socket fallback")
    host, raw_port = server.address.rsplit(":", 1)
    payload = (json.dumps({"op": op, "args": args, "token": TOKEN}) + "\n").encode("utf-8")
    with socket.create_connection((host, int(raw_port)), timeout=timeout) as client:
        client.settimeout(timeout)
        client.sendall(payload)
        received = bytearray()
        while b"\n" not in received:
            chunk = client.recv(64 * 1024)
            if not chunk:
                break
            received.extend(chunk)
            if len(received) > loopback._MAX_LINE_BYTES + 1:
                break
    newline = received.find(b"\n")
    if newline < 0:
        raise AssertionError("loopback response was not newline-terminated")
    frame = bytes(received[: newline + 1])
    return json.loads(frame[:-1].decode("utf-8")), frame


class P0TransportIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        work = Path(REPO_ROOT) / "AI" / "work"
        work.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="p0-transport-", dir=work))
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def start_tcp(self, handler) -> loopback.LoopbackServer:
        server = loopback.LoopbackServer(self.root, self.runtime, TOKEN, handler)
        server._start_tcp()  # force the portable socket fallback; named-pipe coverage is separate
        self.addCleanup(server.stop)
        return server

    def test_session_list_trims_an_oversized_result_with_honest_counts(self) -> None:
        supervisor = Supervisor()
        session_total = 100
        runs_per_session = 50
        session_rows = [
            (
                f"as_list_{index:04d}", f"2026-07-11T00:{index % 60:02d}:00Z", None,
                "kaizen", "governed", "local_llm", "none", "active", "model", "medium",
                "plan", f"profile-{index}", "s" * 40_000, f"Conversation {index}",
            )
            for index in range(session_total)
        ]

        def run_rows(session_id: str) -> list[tuple[object, ...]]:
            return [
                (
                    f"ar_{session_id}_{index:04d}", f"2026-07-11T00:{index % 60:02d}:01Z",
                    "other", "vscode-extension", "success", "local_llm", "none", "model",
                    "model", "medium", "medium", "plan", "profile", "r" * 32_000,
                )
                for index in range(runs_per_session)
            ]

        def fetch_one(sql: str, params=()):
            if "COUNT(*) FROM agent_sessions" in sql:
                return (session_total,)
            raise AssertionError(f"unexpected fetch_one query: {sql}")

        def fetch_all(sql: str, params=()):
            if "FROM agent_sessions" in sql:
                return list(session_rows)
            if "FROM agent_runs r" in sql:
                return [
                    (session_id, *row, runs_per_session)
                    for session_id in params[:-1]
                    for row in run_rows(str(session_id))
                ]
            if "FROM agent_events WHERE agent_run_id IN" in sql:
                return []
            if "message_rank = 1" in sql:
                return [(str(session_id), "first durable prompt") for session_id in params]
            raise AssertionError(f"unexpected fetch_all query: {sql}")

        server = self.start_tcp(supervisor._handle_control)
        with mock.patch.object(db, "fetch_one", side_effect=fetch_one), \
             mock.patch.object(db, "fetch_all", side_effect=fetch_all), \
             mock.patch.object(supervisor, "_safe_reduce", return_value={
                 "terminal": True, "terminal_state": "success",
             }), \
             mock.patch.object(supervisor, "_driven_turn_state", return_value="terminal"), \
             mock.patch.object(supervisor, "_session_has_live_leg", return_value=False):
            response, frame = _tcp_exchange(
                server, "session/list", {"controller": "driven", "limit": session_total},
            )

        self.assertEqual(response["status"], "OK", response)
        self.assertLessEqual(len(frame), loopback._MAX_LINE_BYTES)
        self.assertLessEqual(len(frame), loopback._MAX_LINE_BYTES - 4096)
        sessions = response["sessions"]
        self.assertIsInstance(sessions, list)
        self.assertGreater(len(sessions), 0)
        self.assertLess(len(sessions), session_total)
        self.assertTrue(response["truncated"])
        self.assertEqual(response["sessions_total"], session_total)
        self.assertEqual(response["sessions_returned"], len(sessions))
        self.assertEqual(response["sessions_omitted"], session_total - len(sessions))
        self.assertTrue(any(entry["runs_truncated"] for entry in sessions))
        for entry in sessions:
            self.assertEqual(entry["runs_returned"], len(entry["runs"]))
            self.assertEqual(entry["runs_truncated"], entry["runs_returned"] < entry["runs_total"])

    def test_oversized_legacy_event_omits_body_but_advances_cursor(self) -> None:
        supervisor = Supervisor()
        legacy_event = {
            "sequence_no": 7,
            "event_kind": "tool_call",
            "marker": "close_fail",
            "summary": "legacy oversized tool event",
            "correlation_id": "corr-safe-legacy",
            "code": "DENIED_LEGACY_SAFE",
            "body": "x" * loopback._MAX_LINE_BYTES,
        }

        def handler(request: dict[str, object]) -> dict[str, object]:
            if request.get("op") != "session/events":
                return {"status": "DENIED", "code": "DENIED_UNKNOWN_OP"}
            return supervisor._events_payload(
                "ar_legacy", [legacy_event], 6, False, None, controller="observed",
            )

        response, frame = _tcp_exchange(
            self.start_tcp(handler), "session/events", {"agent_run_id": "ar_legacy", "since": 6},
        )

        self.assertEqual(response["status"], "OK", response)
        self.assertLessEqual(len(frame), loopback._MAX_LINE_BYTES)
        self.assertEqual(response["cursor"], 7)
        self.assertTrue(response["body_omitted"])
        self.assertEqual(response["events_remaining"], 0)
        event = response["events"][0]
        self.assertEqual(event["sequence_no"], 7)
        self.assertEqual(event["correlation_id"], "corr-safe-legacy")
        self.assertEqual(event["code"], "DENIED_LEGACY_SAFE")
        self.assertIsNone(event["body"])
        self.assertTrue(event["body_omitted"])
        self.assertEqual(event["body_omission_code"], "PAYLOAD_TOO_LARGE")

    def test_four_parked_event_polls_do_not_starve_fast_control_request(self) -> None:
        poll_count = 4
        lock = threading.Lock()
        all_parked = threading.Event()
        release_polls = threading.Event()
        parked = 0

        def handler(request: dict[str, object]) -> dict[str, object]:
            nonlocal parked
            if request.get("op") == "session/events":
                with lock:
                    parked += 1
                    if parked == poll_count:
                        all_parked.set()
                if not release_polls.wait(timeout=10.0):
                    return {"status": "ERROR", "code": "ERROR_TEST_POLL_TIMEOUT"}
                args = request.get("args") if isinstance(request.get("args"), dict) else {}
                return {
                    "status": "OK", "events": [], "cursor": args.get("since", 0),
                    "terminal": False, "truncated": False,
                }
            if request.get("op") == "ping":
                return {"status": "OK", "pong": True}
            return {"status": "DENIED", "code": "DENIED_UNKNOWN_OP"}

        server = self.start_tcp(handler)
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def poll(index: int) -> None:
            try:
                result, _frame = _tcp_exchange(
                    server, "session/events", {"agent_run_id": f"ar_poll_{index}", "since": index},
                    timeout=15.0,
                )
                results.append(result)
            except BaseException as error:  # noqa: BLE001 -- retain worker failures for the assertion
                errors.append(error)

        threads = [threading.Thread(target=poll, args=(index,), daemon=True) for index in range(poll_count)]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(all_parked.wait(timeout=5.0), "all long-polls must reach the handler")
            self.assertTrue(all(thread.is_alive() for thread in threads))
            started = time.monotonic()
            fast, frame = _tcp_exchange(server, "ping", {}, timeout=2.0)
            elapsed = time.monotonic() - started
            self.assertEqual(fast, {"status": "OK", "pong": True})
            self.assertLessEqual(len(frame), loopback._MAX_LINE_BYTES)
            self.assertLess(elapsed, 1.5, f"fast control request was starved for {elapsed:.3f}s")
            self.assertTrue(all(thread.is_alive() for thread in threads))
        finally:
            release_polls.set()
            for thread in threads:
                thread.join(timeout=5.0)

        self.assertFalse(errors, errors)
        self.assertEqual(len(results), poll_count)
        self.assertTrue(all(result.get("status") == "OK" for result in results))
        self.assertTrue(all(not thread.is_alive() for thread in threads))


if __name__ == "__main__":
    unittest.main()
