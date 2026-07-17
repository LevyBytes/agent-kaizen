"""Model-backend LIVE path against an in-process MOCK OpenAI-compatible server (no Ollama needed).
Exercises the real client code end to end: B1 reports a reachable embedder + dimension; E3 stores
embeddings; the embedder-backed `semantic` chunker splits by similarity; E4 `--semantic` ranks by
cosine over Turso-native vectors; B2 records a model_call trace; B3 backfills embeddings.

The mock embedding is a deterministic keyword vector [apple, quantum, garden, bias], so cosine
ranking is predictable (a query about apples ranks the apple chunk first)."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import IsolatedDBTest  # noqa: E402

_DOC = (
    "# Topics\n\nApples and oranges grow on fruit trees.\n\n"
    "Quantum entanglement links distant particles.\n\nGardening needs water and sunlight.\n"
)


def _vec(text: str) -> list[float]:
    """Returns the 4-dim deterministic keyword+bias vector [apple, quantum, garden, 1.0] the mock serves."""
    t = text.lower()
    return [float(t.count("apple")), float(t.count("quantum")), float(t.count("garden")), 1.0]


class _OpenAIHandler(BaseHTTPRequestHandler):
    """In-process mock serving /v1/embeddings + /v1/chat/completions OpenAI shapes."""
    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        req = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/embeddings"):
            inputs = req.get("input", [])
            if isinstance(inputs, str):
                inputs = [inputs]
            self._json({
                # Preserve the full OpenAI response shape even though the current client consumes positionally.
                "data": [{"embedding": _vec(text), "index": index} for index, text in enumerate(inputs)],
                "model": req.get("model", "fake"),
            })
        elif self.path.endswith("/chat/completions"):
            self._json({
                "choices": [{"message": {"content": "mock advisory reply"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                "model": req.get("model", "fake"),
            })
        else:
            self._json({}, 404)


class BackendsLiveTest(IsolatedDBTest):
    """SetUp stands up the mock server and pins embed/LLM env to it."""
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
        base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        # addCleanup is LIFO: register server_close first so shutdown() runs before it.
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = {
            "KAIZEN_EMBED_BACKEND": "ollama",
            "KAIZEN_EMBED_MODEL": "fake-embed",
            "KAIZEN_EMBED_BASE_URL": base,
            "KAIZEN_LLM_MODEL": "fake-chat",
            "KAIZEN_LLM_BASE_URL": base,
        }

    def _ingest(self, env: dict) -> str:
        doc = self.root / "doc.md"
        doc.write_text(_DOC, encoding="utf-8")
        rc, ing = self.kz("E1", "--path", str(doc), env=env)
        self.assertEqual(rc, 0, ing)
        return ing["id"]

    def test_doctor_reachable_with_dimension(self):
        rc, p = self.kz("B1", env=self.env)
        self.assertEqual(rc, 0, p)
        embedding = p.get("embedding")
        text = p.get("text")
        self.assertIsInstance(embedding, dict, p)
        self.assertIsInstance(text, dict, p)
        self.assertTrue(embedding.get("reachable"), p)
        self.assertEqual(embedding.get("dimension"), 4, p)
        self.assertTrue(text.get("reachable"), p)

    def test_semantic_chunk_then_vector_query_ranks_correctly(self):
        doc_id = self._ingest(self.env)
        rc, ch = self.kz("E3", "--id", doc_id, "--chunker", "semantic", env=self.env)
        self.assertEqual(rc, 0, ch)
        self.assertTrue(ch.get("embedded"), ch)
        self.assertEqual(ch.get("embedding_model"), "fake-embed", ch)
        self.assertGreaterEqual(ch.get("chunk_count"), 2, ch)  # similarity boundaries split the topics

        rc2, q = self.kz("E4", "--query", "fresh apples please", "--semantic", env=self.env)
        self.assertEqual(rc2, 0, q)
        self.assertEqual(q.get("mode"), "semantic", q)
        records = q.get("records")
        self.assertIsInstance(records, list, q)
        self.assertGreater(len(records), 0, q)
        self.assertIsInstance(records[0], dict, q)
        snippet = records[0].get("snippet")
        self.assertIsInstance(snippet, str, q)
        self.assertIn("Apple", snippet, q)  # cosine ranks the apple chunk first

    def test_model_run_records_advisory_trace(self):
        rc, p = self.kz("B2", "--prompt", "Summarize the latest report.", env=self.env)
        self.assertEqual(rc, 0, p)
        self.assertTrue(p.get("advisory"), p)
        self.assertEqual(p.get("provider"), "ollama", p)
        self.assertTrue(p.get("output_ref"), p)
        tokens = p.get("tokens")
        self.assertIsInstance(tokens, dict, p)
        self.assertEqual(tokens.get("total"), 8, p)

    def test_reembed_backfills_unembedded_chunks(self):
        no_embed = {"KAIZEN_EMBED_BACKEND": "ollama", "KAIZEN_EMBED_MODEL": ""}
        doc_id = self._ingest(no_embed)
        rc, ch = self.kz("E3", "--id", doc_id, env=no_embed)  # recursive, no embeddings
        self.assertEqual(rc, 0, ch)
        self.assertFalse(ch.get("embedded"), ch)

        rc2, r = self.kz("B3", env=self.env)  # backfill with the mock embedder
        self.assertEqual(rc2, 0, r)
        self.assertGreaterEqual(r.get("reembedded"), 1, r)
        self.assertEqual(r.get("dimension"), 4, r)


if __name__ == "__main__":
    unittest.main()
