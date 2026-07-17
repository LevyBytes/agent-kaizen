"""P0 bounded JSON-Lines loopback contracts."""

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

from kaizen_components.orchestration import loopback


class LoopbackFrameLimitTest(unittest.TestCase):
    def test_client_refuses_oversize_before_transport_lookup(self) -> None:
        result = loopback.send_request(
            Path("D:/does-not-need-to-exist"),
            Path("D:/does-not-need-to-exist/runtime"),
            {"op": "test", "args": {"text": "é" * loopback._MAX_LINE_BYTES}},
        )
        self.assertEqual(result, {
            "status": "DENIED",
            "code": "PAYLOAD_TOO_LARGE",
            "retryable": False,
            "limit_bytes": loopback._MAX_LINE_BYTES,
        })

    def test_oversize_response_is_replaced_by_minimal_denial(self) -> None:
        payload = loopback._encode_response({"status": "OK", "body": "x" * loopback._MAX_LINE_BYTES})
        self.assertLessEqual(len(payload), loopback._MAX_LINE_BYTES)
        self.assertEqual(json.loads(payload), loopback._payload_too_large())

    def test_exact_limit_line_is_accepted_and_plus_one_is_oversize(self) -> None:
        def read(payload: bytes):
            server, client = socket.socketpair()
            try:
                sender = threading.Thread(target=client.sendall, args=(payload,))
                sender.start()
                value = loopback._read_line_socket(server)
                sender.join(timeout=2.0)
                self.assertFalse(sender.is_alive(), "socket sender did not terminate")
                return value
            finally:
                server.close()
                client.close()

        exact = b"x" * (loopback._MAX_LINE_BYTES - 1) + b"\n"
        self.assertEqual(len(read(exact)), loopback._MAX_LINE_BYTES - 1)
        too_large = b"x" * loopback._MAX_LINE_BYTES + b"\n"
        self.assertIs(read(too_large), loopback._OVERSIZE_FRAME)

    def test_oversize_server_request_never_reaches_handler(self) -> None:
        calls: list[dict] = []
        server = loopback.LoopbackServer(Path("D:/repo"), Path("D:/runtime"), "token", calls.append)
        accepted, client = socket.socketpair()
        worker = threading.Thread(target=server._serve_socket, args=(accepted,))
        worker.start()
        try:
            client.settimeout(5.0)
            client.sendall(b"x" * loopback._MAX_LINE_BYTES + b"\n")
            response = b""
            while b"\n" not in response:
                response += client.recv(4096)
            self.assertEqual(json.loads(response), loopback._payload_too_large())
            self.assertEqual(calls, [])
        finally:
            client.close()
            worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive(), "loopback worker did not terminate")


@unittest.skipUnless(loopback.IS_WINDOWS, "Windows named-pipe liveness")
class PipeExistsTest(unittest.TestCase):
    def test_wait_failure_is_not_reclassified_by_stale_last_error(self) -> None:
        with mock.patch.object(loopback._kernel32, "WaitNamedPipeW", return_value=False), \
             mock.patch.object(loopback.ctypes, "get_last_error", return_value=0):
            self.assertFalse(loopback._pipe_exists(r"\\.\pipe\missing"))


class LoopbackFairnessTest(unittest.TestCase):
    def test_two_parked_long_polls_do_not_starve_control_request(self) -> None:
        scratch_root = Path(tempfile.mkdtemp(prefix="loopback-p0-", dir=Path("AI/work")))
        runtime = scratch_root / "AI" / "work" / "orchestration" / "runtime"
        runtime.mkdir(parents=True)
        release = threading.Event()
        two_parked = threading.Event()
        lock = threading.Lock()
        parked = 0

        def handler(request: dict) -> dict:
            nonlocal parked
            if request.get("op") != "session/events":
                return {"status": "OK", "control": True}
            with lock:
                parked += 1
                if parked == 2:
                    two_parked.set()
            release.wait(3.0)
            return {"status": "OK", "events": []}

        server = loopback.LoopbackServer(scratch_root, runtime, "token", handler)
        poll_results: list[dict] = []
        poll_threads = [
            threading.Thread(
                target=lambda: poll_results.append(loopback.send_request(
                    scratch_root,
                    runtime,
                    {"op": "session/events", "args": {}, "token": "token"},
                    timeout=5.0,
                )),
            )
            for _ in range(2)
        ]
        try:
            server.start()
            for thread in poll_threads:
                thread.start()
            self.assertTrue(two_parked.wait(2.0), "two long polls did not park concurrently")
            started = time.perf_counter()
            control = loopback.send_request(
                scratch_root,
                runtime,
                {"op": "status", "args": {}, "token": "token"},
                timeout=2.0,
            )
            elapsed = time.perf_counter() - started
            self.assertEqual(control, {"status": "OK", "control": True})
            self.assertLess(elapsed, 1.5, "control request was serialized behind long polls")
        finally:
            release.set()
            for thread in poll_threads:
                thread.join(timeout=5.0)
            server.stop()
            shutil.rmtree(scratch_root, ignore_errors=True)
        self.assertEqual(len(poll_results), 2)


if __name__ == "__main__":
    unittest.main()
