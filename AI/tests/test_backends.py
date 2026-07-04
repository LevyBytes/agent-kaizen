"""Model-backend ops (B*) + the embedding seam in E3/E4, all exercised WITHOUT a live model
server. Confirms the opt-in/graceful contract: no backend configured -> deterministic chunking +
lexical search still work; configured-but-unreachable -> the doctor reports it and the ops deny
cleanly rather than crashing."""

from __future__ import annotations

import email.message
import socket
import ssl
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _harness import IsolatedDBTest  # noqa: E402
from kaizen_components.backends import embed_batched, http_retry  # noqa: E402
from kaizen_components.denials import KaizenDenied  # noqa: E402


# Force "no backend configured" regardless of the outer environment.
_UNCONFIGURED = {"KAIZEN_EMBED_MODEL": "", "KAIZEN_LLM_MODEL": ""}


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("http://x", code, "err", headers, None)


class HttpRetryClassifierTest(unittest.TestCase):
    def test_transient_true_for_5xx_429_timeout_connection(self):
        for error in (
            _http_error(500),
            _http_error(503),
            _http_error(429),
            TimeoutError("slow"),
            socket.timeout("slow"),
            ConnectionResetError("reset"),
            ConnectionRefusedError("refused"),
            urllib.error.URLError(ConnectionRefusedError("refused")),
            urllib.error.URLError(TimeoutError("slow")),
        ):
            self.assertTrue(http_retry._is_transient(error), repr(error))

    def test_transient_false_for_permanent_failures(self):
        for error in (
            _http_error(404),
            _http_error(400),
            ssl.SSLError("bad cert"),
            socket.gaierror("name resolution failed"),
            urllib.error.URLError(ssl.SSLError("bad cert")),
            urllib.error.URLError(socket.gaierror("dns")),
            ValueError("unknown url type"),
        ):
            self.assertFalse(http_retry._is_transient(error), repr(error))

    def test_retry_after_honored_and_clamped(self):
        self.assertEqual(http_retry._retry_after_seconds(_http_error(429, "3")), 3.0)
        self.assertEqual(
            http_retry._retry_after_seconds(_http_error(429, "999")), http_retry.RETRY_AFTER_CAP
        )
        self.assertIsNone(http_retry._retry_after_seconds(_http_error(429)))
        self.assertIsNone(http_retry._retry_after_seconds(_http_error(500, "5")))


class HttpRetryLoopTest(unittest.TestCase):
    def test_retries_transient_then_succeeds(self):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"ok"

        calls = {"n": 0}

        def fake_urlopen(req, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("first attempt times out")
            return _Resp()

        with mock.patch.object(http_retry.urllib.request, "urlopen", fake_urlopen), mock.patch.object(
            http_retry.time, "sleep"
        ) as fake_sleep:
            body = http_retry.http_request(mock.Mock(), timeout=1.0)
        self.assertEqual(body, b"ok")
        self.assertEqual(calls["n"], 2)
        fake_sleep.assert_called_once_with(http_retry.RETRY_DELAYS[0])

    def test_permanent_error_is_not_retried(self):
        def fake_urlopen(req, timeout):
            raise _http_error(404)

        with mock.patch.object(http_retry.urllib.request, "urlopen", fake_urlopen), mock.patch.object(
            http_retry.time, "sleep"
        ) as fake_sleep:
            with self.assertRaises(urllib.error.HTTPError):
                http_retry.http_request(mock.Mock(), timeout=1.0)
        fake_sleep.assert_not_called()

    def test_429_retry_after_drives_the_sleep(self):
        def fake_urlopen(req, timeout):
            raise _http_error(429, "4")

        with mock.patch.object(http_retry.urllib.request, "urlopen", fake_urlopen), mock.patch.object(
            http_retry.time, "sleep"
        ) as fake_sleep:
            with self.assertRaises(urllib.error.HTTPError):
                http_retry.http_request(mock.Mock(), timeout=1.0)
        # Every backoff uses the server-requested 4s (clamped under the 5s cap), not the schedule.
        self.assertTrue(fake_sleep.call_args_list)
        for call in fake_sleep.call_args_list:
            self.assertEqual(call.args[0], 4.0)


class _FakeEmbedBackend:
    name = "fake"
    model = "fake-model"

    def __init__(self, *, dim: int = 3, drop_one: bool = False) -> None:
        self.dim = dim
        self.drop_one = drop_one
        self.batch_sizes: list[int] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        count = len(texts) - 1 if self.drop_one else len(texts)
        # Encode the index so order can be asserted after concatenation.
        return [[float(i)] * self.dim for i in range(count)]


class EmbedBatchedTest(unittest.TestCase):
    def test_splits_into_ordered_bounded_batches(self):
        backend = _FakeEmbedBackend()
        texts = [f"t{i}" for i in range(5)]
        vectors = embed_batched(backend, texts, batch_size=2)
        self.assertEqual(backend.batch_sizes, [2, 2, 1])
        self.assertEqual(len(vectors), 5)
        # Concatenation preserves batch-local order (each batch re-indexes from 0).
        self.assertEqual([v[0] for v in vectors], [0.0, 1.0, 0.0, 1.0, 0.0])

    def test_count_mismatch_denies(self):
        backend = _FakeEmbedBackend(drop_one=True)
        with self.assertRaises(KaizenDenied) as ctx:
            embed_batched(backend, ["a", "b", "c"], batch_size=8)
        self.assertEqual(ctx.exception.code, "DENIED_EMBED_MISMATCH")


class BackendDoctorTest(IsolatedDBTest):
    def test_doctor_unconfigured(self):
        rc, p = self.kz("B1", env=_UNCONFIGURED)
        self.assertEqual(rc, 0, p)
        self.assertFalse(p["embedding"]["configured"], p)
        self.assertFalse(p["text"]["configured"], p)

    def test_doctor_configured_but_unreachable(self):
        env = {"KAIZEN_EMBED_MODEL": "nomic-embed-text", "KAIZEN_EMBED_BASE_URL": "http://127.0.0.1:6/v1"}
        rc, p = self.kz("B1", env=env)
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["embedding"]["configured"], p)
        self.assertFalse(p["embedding"].get("reachable", True), p)


class BackendUnconfiguredDenialTest(IsolatedDBTest):
    def test_model_run_requires_backend(self):
        rc, p = self.kz("B2", "--prompt", "hello", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNCONFIGURED", p)

    def test_reembed_requires_backend(self):
        rc, p = self.kz("B3", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNCONFIGURED", p)

    def test_semantic_query_requires_backend(self):
        rc, p = self.kz("E4", "--query", "anything", "--semantic", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNCONFIGURED", p)


class EvidenceNoBackendTest(IsolatedDBTest):
    def test_chunk_without_backend_then_lexical_query(self):
        doc = self.root / "doc.md"
        doc.write_text("# Title\n\nAlpha bravo charlie.\n\nDelta echo foxtrot.\n", encoding="utf-8")
        rc, ing = self.kz("E1", "--path", str(doc), env=_UNCONFIGURED)
        self.assertEqual(rc, 0, ing)
        doc_id = ing["id"]

        rc2, ch = self.kz("E3", "--id", doc_id, env=_UNCONFIGURED)
        self.assertEqual(rc2, 0, ch)
        self.assertFalse(ch.get("embedded"), ch)          # no backend -> no embeddings stored
        self.assertIsNone(ch.get("embedding_model"), ch)

        rc3, q = self.kz("E4", "--query", "bravo", env=_UNCONFIGURED)
        self.assertEqual(rc3, 0, q)
        self.assertEqual(q.get("mode"), "like", q)         # graceful lexical baseline
        self.assertTrue(any("bravo" in (r.get("snippet") or "") for r in q.get("records", [])), q)


if __name__ == "__main__":
    unittest.main()
