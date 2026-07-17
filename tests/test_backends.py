"""Model-backend ops (B*) + the embedding seam in E3/E4, all exercised WITHOUT a live model
server. Confirms the opt-in/graceful contract: no backend configured -> deterministic chunking +
lexical search still work; configured-but-unreachable -> the doctor reports it and the ops deny
cleanly rather than crashing."""

from __future__ import annotations

import email.message
import os
import socket
import ssl
import sys
import types
import unittest
import urllib.error
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import IsolatedDBTest  # noqa: E402
from kaizen_components import evidence  # noqa: E402
from kaizen_components.backends import embed_batched, http_retry  # noqa: E402
from kaizen_components.backends.ollama import OllamaEmbeddingBackend  # noqa: E402
from kaizen_components.backends.transformers_backend import TransformersTextBackend  # noqa: E402
from kaizen_components.denials import KaizenDenied  # noqa: E402


# Force "no backend configured" regardless of the outer environment.
_UNCONFIGURED = {
    "KAIZEN_EMBED_BACKEND": "",
    "KAIZEN_EMBED_MODEL": "",
    "KAIZEN_LLM_BACKEND": "",
    "KAIZEN_LLM_MODEL": "",
    "KAIZEN_TEXT_BACKEND": "",
}


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
    def test_isolated_child_override_disables_transient_retries(self):
        calls = {"n": 0}

        def fake_urlopen(req, timeout):
            calls["n"] += 1
            raise TimeoutError("one bounded attempt")

        with mock.patch.dict(os.environ, {"KAIZEN_HTTP_RETRIES": "0"}), mock.patch.object(
            http_retry.urllib.request, "urlopen", fake_urlopen
        ), mock.patch.object(http_retry.time, "sleep") as fake_sleep:
            with self.assertRaises(TimeoutError):
                http_retry.http_request(mock.Mock(), timeout=1.0)
        self.assertEqual(calls["n"], 1)
        fake_sleep.assert_not_called()

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

        with mock.patch.dict(os.environ, {"KAIZEN_HTTP_RETRIES": "1"}), mock.patch.object(http_retry.urllib.request, "urlopen", fake_urlopen), mock.patch.object(
            http_retry.time, "sleep"
        ) as fake_sleep:
            body = http_retry.http_request(mock.Mock(), timeout=1.0)
        self.assertEqual(body, b"ok")
        self.assertEqual(calls["n"], 2)
        fake_sleep.assert_called_once_with(http_retry.RETRY_DELAYS[0])

    def test_http_open_returns_caller_owned_response_after_connect_retry(self):
        response = mock.Mock()
        calls = 0

        def fake_urlopen(_req, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionRefusedError("opening")
            return response

        with mock.patch.object(http_retry.urllib.request, "urlopen", fake_urlopen), mock.patch.object(http_retry.time, "sleep"):
            opened = http_retry.http_open(mock.Mock(), timeout=1.0)
        self.assertIs(opened, response)
        self.assertEqual(calls, 2)
        response.close.assert_not_called()
        opened.close()
        response.close.assert_called_once_with()

    def test_response_read_failure_is_never_retried(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = TimeoutError("body read began")
        with mock.patch.object(http_retry.urllib.request, "urlopen", return_value=response) as opened, mock.patch.object(http_retry.time, "sleep") as slept:
            with self.assertRaises(TimeoutError):
                http_retry.http_request(mock.Mock(), timeout=1.0)
        opened.assert_called_once()
        slept.assert_not_called()

    def test_permanent_error_is_not_retried(self):
        def fake_urlopen(req, timeout):
            raise _http_error(404)

        with mock.patch.dict(os.environ, {"KAIZEN_HTTP_RETRIES": "1"}), mock.patch.object(http_retry.urllib.request, "urlopen", fake_urlopen), mock.patch.object(
            http_retry.time, "sleep"
        ) as fake_sleep:
            with self.assertRaises(urllib.error.HTTPError):
                http_retry.http_request(mock.Mock(), timeout=1.0)
        fake_sleep.assert_not_called()

    def test_429_retry_after_drives_the_sleep(self):
        def fake_urlopen(req, timeout):
            raise _http_error(429, "4")

        with mock.patch.dict(os.environ, {"KAIZEN_HTTP_RETRIES": "1"}), mock.patch.object(http_retry.urllib.request, "urlopen", fake_urlopen), mock.patch.object(
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

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
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


class OllamaFallbackTest(unittest.TestCase):
    def test_only_missing_v1_route_falls_back_to_native(self):
        backend = OllamaEmbeddingBackend(base_url="http://127.0.0.1:11434/v1", model="m")
        backend.client.embeddings = mock.Mock(side_effect=KaizenDenied(
            "DENIED_BACKEND_HTTP", {"http_status": 404}, exit_code=2,
        ))
        backend._native_embed = mock.Mock(return_value=[[1.0]])
        self.assertEqual(backend.embed(["x"]), [[1.0]])
        backend._native_embed.assert_called_once_with(["x"])

    def test_actionable_v1_denial_is_preserved(self):
        backend = OllamaEmbeddingBackend(base_url="http://127.0.0.1:11434/v1", model="m")
        denied = KaizenDenied("DENIED_BACKEND_HTTP", {"http_status": 400, "reason": "wrong model"}, exit_code=2)
        backend.client.embeddings = mock.Mock(side_effect=denied)
        backend._native_embed = mock.Mock()
        with self.assertRaises(KaizenDenied) as ctx:
            backend.embed(["x"])
        self.assertIs(ctx.exception, denied)
        backend._native_embed.assert_not_called()

    def test_native_fallback_forwards_bearer_and_rejects_malformed_json(self):
        backend = OllamaEmbeddingBackend(base_url="http://127.0.0.1:11434/v1", model="m", api_key="secret")
        requests = []

        def valid(request, *, timeout):
            requests.append((request, timeout))
            return b'{"embedding":[1,2.5]}'

        with mock.patch("kaizen_components.backends.ollama.http_request", side_effect=valid):
            self.assertEqual(backend._native_embed(["x"]), [[1.0, 2.5]])
        self.assertEqual(requests[0][0].get_header("Authorization"), "Bearer secret")
        with mock.patch("kaizen_components.backends.ollama.http_request", return_value=b"not-json"):
            with self.assertRaises(KaizenDenied) as caught:
                backend._native_embed(["x"])
        self.assertEqual(caught.exception.code, "DENIED_BACKEND_MALFORMED")


class TransformersTextBackendInputTest(unittest.TestCase):
    class Tensor:
        def __init__(self, values):
            self.values = list(values)
            self.shape = (1, len(self.values))

        def to(self, _device):
            return self

        def __getitem__(self, index):
            if index == 0:
                return TransformersTextBackendInputTest.Row(self.values)
            raise IndexError(index)

    class Row:
        def __init__(self, values):
            self.values = list(values)
            self.shape = (len(self.values),)

        def __getitem__(self, index):
            return TransformersTextBackendInputTest.Row(self.values[index]) if isinstance(index, slice) else self.values[index]

    class BatchEncoding(dict):
        pass

    def _backend(self, encoded):
        generated = self.Tensor([10, 11, 12, 20, 21])
        tokenizer = types.SimpleNamespace(
            apply_chat_template=mock.Mock(return_value=encoded),
            eos_token_id=None,
            pad_token_id=None,
            decode=mock.Mock(return_value="answer"),
        )
        model = types.SimpleNamespace(device="cpu", generate=mock.Mock(return_value=generated))
        backend = TransformersTextBackend(model="fixture", max_new_tokens=7)
        backend._load = mock.Mock()
        backend._tokenizer = tokenizer
        backend._model_obj = model
        backend._torch = types.SimpleNamespace(no_grad=lambda: nullcontext())
        return backend, model

    def test_tensor_and_batch_encoding_inputs_are_forwarded(self):
        tensor = self.Tensor([10, 11, 12])
        plain, plain_model = self._backend(tensor)
        self.assertEqual(plain.chat("x")["text"], "answer")
        self.assertEqual(set(plain_model.generate.call_args.kwargs), {"input_ids", "max_new_tokens", "do_sample"})

        batch = self.BatchEncoding(input_ids=tensor, attention_mask=self.Tensor([1, 1, 1]))
        mapped, mapped_model = self._backend(batch)
        self.assertEqual(mapped.chat("x")["usage"]["prompt_tokens"], 3)
        self.assertIn("attention_mask", mapped_model.generate.call_args.kwargs)

    def test_mapping_without_input_ids_is_denied(self):
        backend, _model = self._backend(self.BatchEncoding(attention_mask=self.Tensor([1])))
        with self.assertRaises(KaizenDenied) as caught:
            backend.chat("x")
        self.assertEqual(caught.exception.code, "DENIED_BACKEND_MALFORMED")


class RechunkReplacementTest(unittest.TestCase):
    def test_old_embeddings_and_chunks_are_deleted_before_reinsert(self):
        statements: list[str] = []

        class Conn:
            def execute(self, sql, _params=()):
                statements.append(" ".join(sql.split()))

        with mock.patch.object(evidence, "fetch_one", return_value=("ed-1", "sl-1", 0)), \
             mock.patch.object(evidence, "fetch_all", return_value=[("one paragraph",)]), \
             mock.patch.object(evidence, "write_tx", side_effect=lambda op: op(Conn(), 1)), \
             mock.patch("kaizen_components.backends.get_embedding_backend", return_value=None):
            out = evidence.chunk_document(types.SimpleNamespace(id="ed-1", chunker="recursive", test=False))
        self.assertEqual(out["chunk_count"], 1)
        self.assertIn("DELETE FROM chunk_embeddings", statements[0])
        self.assertIn("DELETE FROM evidence_chunks", statements[1])
        self.assertIn("INSERT INTO evidence_chunks", statements[2])


class BackendDoctorTest(IsolatedDBTest):
    def test_doctor_unconfigured(self):
        rc, p = self.kz("B1", env=_UNCONFIGURED)
        self.assertEqual(rc, 0, p)
        self.assertFalse(p["embedding"]["configured"], p)
        self.assertFalse(p["text"]["configured"], p)

    def test_doctor_configured_but_unreachable(self):
        env = {"KAIZEN_EMBED_MODEL": "hf.co/mradermacher/KaLM-embedding-multilingual-mini-instruct-v2.5-GGUF:Q8_0", "KAIZEN_EMBED_BASE_URL": "http://127.0.0.1:6/v1"}
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


class EmbedQueryPromptTest(unittest.TestCase):
    """The in-process embedder applies a query instruction to QUERIES (is_query=True) but NEVER to
    documents -- so instruction-tuned defaults (F2LLM, Qwen3-Embedding) reach their retrieval ceiling
    on E4 --semantic while E1/E3 document embeddings stay unprompted. Encoder is stubbed (no weights)."""

    def _backend(self, prompts):
        from kaizen_components.backends.sentence_transformers_backend import SentenceTransformersBackend

        calls: list[dict] = []

        class _Enc:
            def __init__(self):
                self.prompts = prompts

            def encode(self, texts, **kw):
                calls.append(kw)
                class _Vectors(list):
                    def tolist(self):
                        return list(self)

                return _Vectors([[0.0, 0.0, 0.0, 0.0] for _ in texts])

        b = SentenceTransformersBackend(model="fake")
        b._encoder = _Enc()  # _load() returns this -> no sentence_transformers import, no weights
        return b, calls

    def test_document_embed_never_prompts(self):
        b, calls = self._backend({"query": "Instruct: ...\nQuery: "})
        b.embed(["a chunk of a document"])  # is_query defaults False
        self.assertNotIn("prompt", calls[0])
        self.assertNotIn("prompt_name", calls[0])

    def test_query_uses_models_configured_prompt(self):
        b, calls = self._backend({"query": "Instruct: ...\nQuery: "})
        with mock.patch.dict(os.environ, {"KAIZEN_EMBED_QUERY_PROMPT": ""}):
            b.embed(["a search query"], is_query=True)
        self.assertEqual(calls[0].get("prompt_name"), "query")

    def test_env_override_beats_model_prompt(self):
        b, calls = self._backend({"query": "model prompt"})
        with mock.patch.dict(os.environ, {"KAIZEN_EMBED_QUERY_PROMPT": "OVERRIDE: "}):
            b.embed(["q"], is_query=True)
        self.assertEqual(calls[0].get("prompt"), "OVERRIDE: ")
        self.assertNotIn("prompt_name", calls[0])

    def test_model_without_query_prompt_stays_plain(self):
        b, calls = self._backend({})  # granite-like: no query prompt -> unchanged behavior
        with mock.patch.dict(os.environ, {"KAIZEN_EMBED_QUERY_PROMPT": ""}):
            b.embed(["q"], is_query=True)
        self.assertNotIn("prompt", calls[0])
        self.assertNotIn("prompt_name", calls[0])


if __name__ == "__main__":
    unittest.main()
