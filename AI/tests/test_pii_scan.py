"""B5 pii-scan (advisory, augments the regex gate) + O5 lab-dedup. Regex baseline, deny/absent paths,
the pure clustering helper, and schema validity. The heavy gliner2 / embedding extras are NOT required;
tests that need them absent are skipped when present."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import IsolatedDBTest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import kaizen_components.backends as bk  # noqa: E402
import kaizen_components.model_ops as mo  # noqa: E402
import kaizen_components.pii_scan as ps  # noqa: E402
import kaizen_components.trace_records as tr  # noqa: E402
from kaizen_components.dedup import _single_link_clusters  # noqa: E402

_GLINER_PRESENT = importlib.util.find_spec("gliner2") is not None
_UNCONFIGURED = {"KAIZEN_EMBED_BACKEND": "ollama", "KAIZEN_EMBED_MODEL": "", "KAIZEN_PII_MODEL": "", "KAIZEN_PII_BACKEND": ""}
_PII_GLINER = {"KAIZEN_PII_BACKEND": "gliner2", "KAIZEN_PII_MODEL": "fastino/gliner2-privacy-filter-PII-multi"}
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # AKIA + 16 -> matches the aws_access_key regex


class ClusterUnitTest(unittest.TestCase):
    def test_single_link_clusters(self):
        vectors = [[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]]  # first two near-identical
        multi = [c for c in _single_link_clusters(vectors, 0.9) if len(c) > 1]
        self.assertEqual(len(multi), 1, multi)
        self.assertEqual(set(multi[0]), {0, 1}, multi)


class PiiScanCliTest(IsolatedDBTest):
    def test_regex_baseline_detects_secret(self):
        rc, p = self.kz("B5", "--prompt", f"key is {_AWS_KEY} ok", env=_UNCONFIGURED)
        self.assertEqual(rc, 0, p)
        self.assertTrue(any(h["label"] == "aws_access_key" for h in p["regex_hits"]), p)
        self.assertEqual(p["model_hits"], [], p)

    def test_clean_text_no_hits(self):
        rc, p = self.kz("B5", "--prompt", "add a terse judge op", env=_UNCONFIGURED)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["regex_hits"], [], p)

    def test_requires_text(self):
        rc, p = self.kz("B5", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_PII_TEXT_REQUIRED", p)

    @unittest.skipIf(_GLINER_PRESENT, "extra installed: graceful-absent path not applicable")
    def test_model_backend_absent_denies(self):
        rc, p = self.kz("B5", "--prompt", "some text here", env=_PII_GLINER)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNAVAILABLE", p)

    def test_pii_scan_schema_valid(self):
        rc, p = self.kz(
            "Q8", "--kind", "pii_scan",
            "--payload-json", '{"regex_hit_count": 1, "model_hit_count": 0, "summary": "advisory scan.", "hits": []}',
        )
        self.assertEqual(rc, 0, p)
        self.assertTrue(p.get("valid"), p)


class DedupCliTest(IsolatedDBTest):
    def test_o5_without_backend_denied(self):
        rc, p = self.kz("O5", "--kind", "gotcha", env=_UNCONFIGURED)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_UNCONFIGURED", p)


class _FakeText:
    """Minimal TextBackend stub: chat() returns a STRICT-JSON verdict so B4 parses a 'pass'."""

    name = "fake-provider"
    model = "fake-model"

    def chat(self, prompt, **opts):
        return {
            "text": '{"verdict": "pass", "score": 1, "rationale": "looks fine"}',
            "model": "fake-model",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


_FAKE_PII = [{"source": "model", "label": "person", "span_hash": "deadbeefcafe"}]


class PiiAttachWiringTest(unittest.TestCase):
    """B2/B4 attach advisory PII hits (label + span_hash, never raw) to trace_events.tags_json when a
    PII backend is configured, and leave tags_json null otherwise. In-process: write_tx + the text
    backend + write_eval_score are stubbed, so nothing touches a real DB or loads a model."""

    def setUp(self):
        self._orig = (mo.get_text_backend, mo.advisory_pii_scan, mo.write_tx, tr.write_eval_score)
        self.captured: list[tuple[str, tuple]] = []

        def fake_write_tx(op):
            class _Conn:
                def __init__(s):
                    s.rows: list[tuple[str, tuple]] = []

                def execute(s, sql, params):
                    s.rows.append((sql, params))

            conn = _Conn()
            op(conn, 1)
            self.captured.extend(conn.rows)

        mo.write_tx = fake_write_tx
        mo.get_text_backend = lambda: _FakeText()
        tr.write_eval_score = lambda payload, **kw: {"id": "score_x"}  # tolerate the is_test kwarg

    def tearDown(self):
        mo.get_text_backend, mo.advisory_pii_scan, mo.write_tx, tr.write_eval_score = self._orig

    @staticmethod
    def _args(**kw):
        return types.SimpleNamespace(**kw)

    def _trace_tags_json(self):
        for sql, params in self.captured:
            if "INSERT INTO trace_events" in sql:
                cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
                return dict(zip(cols, params)).get("tags_json")
        return "NO_TRACE_INSERT"

    def test_b2_attaches_pii_when_configured(self):
        mo.advisory_pii_scan = lambda text, **kw: list(_FAKE_PII)
        res = mo.model_run(self._args(prompt="advisory prompt", summary=None, task_id=None, trace_id=None, test=False))
        self.assertEqual(res.get("pii_advisory"), _FAKE_PII, res)
        tags = self._trace_tags_json()
        self.assertEqual(json.loads(tags)["pii_advisory"], _FAKE_PII)
        self.assertIn("deadbeefcafe", tags)  # span_hash, not raw span

    def test_b2_tags_null_when_unconfigured(self):
        mo.advisory_pii_scan = lambda text, **kw: []
        res = mo.model_run(self._args(prompt="advisory prompt", summary=None, task_id=None, trace_id=None, test=False))
        self.assertNotIn("pii_advisory", res)  # default output byte-identical
        self.assertIsNone(self._trace_tags_json())

    def test_b4_attaches_pii_to_judge_trace(self):
        mo.advisory_pii_scan = lambda text, **kw: list(_FAKE_PII)
        res = mo.model_judge(self._args(
            prompt="the candidate output", body=None, query="score it 0-1", summary=None,
            task_id=None, trace_id=None, test=False, dry_run=False,
            expected_json=None, expected_json_file=None, eval_case_id=None, proof_id=None, metric=None,
        ))
        self.assertEqual(res.get("pii_advisory"), _FAKE_PII, res)
        self.assertEqual(json.loads(self._trace_tags_json())["pii_advisory"], _FAKE_PII)


class _CaptureTx:
    """Patch trace_records.write_tx to capture the trace_events INSERT without a DB, so the model-call
    observability path is tested deterministically (no real DB, no live model)."""

    def __init__(self):
        self.rows: list[tuple[str, tuple]] = []

    def __call__(self, op):
        cap = self

        class _Conn:
            def execute(s, sql, params):
                cap.rows.append((sql, params))

        op(_Conn(), 1)

    def trace_row(self):
        for sql, params in self.rows:
            if "INSERT INTO trace_events" in sql:
                cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
                return dict(zip(cols, params))
        return None


class _FakeEmbed:
    name = "fake-embed"
    model = "fake-embed-model"

    def embed(self, texts, *, is_query=False):
        return [[0.1, 0.2, 0.3] for _ in texts]


class _FakePii:
    name = "fake-pii"
    model = "fake-pii-model"

    def scan(self, text):
        return [{"label": "person", "start": 0, "end": 4}]


class ModelCallTraceTest(unittest.TestCase):
    """The embedder / reranker / PII lanes emit a kind='model_call' observability trace (name=<lane>)
    so ALL model usage shows in the B6 monitor -- not just text-gen (B2) and judge (B4/O4)."""

    def setUp(self):
        self._orig_tx = tr.write_tx
        self.cap = _CaptureTx()
        tr.write_tx = self.cap

    def tearDown(self):
        tr.write_tx = self._orig_tx

    def test_record_model_call_writes_lane_trace(self):
        tid = tr.record_model_call(lane="embedding", model="m", provider="p", latency_ms=12,
                                   count=3, dimension=768, is_test=1)
        self.assertIsNotNone(tid)
        row = self.cap.trace_row()
        self.assertEqual(row["kind"], "model_call")
        self.assertEqual(row["name"], "embedding")
        self.assertEqual(row["model"], "m")
        self.assertEqual(row["is_test"], 1)

    def test_record_model_call_rejects_unknown_lane(self):
        self.assertIsNone(tr.record_model_call(lane="bogus", model="m", provider="p", latency_ms=1))
        self.assertIsNone(self.cap.trace_row())

    def test_record_model_call_swallows_write_error(self):
        def boom(op):
            raise RuntimeError("db down")

        tr.write_tx = boom  # observability is best-effort: a write failure must NOT propagate
        self.assertIsNone(tr.record_model_call(lane="pii", model="m", provider="p", latency_ms=1))

    def test_embed_batched_records_trace_when_asked(self):
        vecs = bk.embed_batched(_FakeEmbed(), ["a", "b"], record_trace=True, is_test=1)
        self.assertEqual(len(vecs), 2)
        row = self.cap.trace_row()
        self.assertEqual(row["name"], "embedding")
        self.assertEqual(row["model"], "fake-embed-model")
        self.assertEqual(row["is_test"], 1)

    def test_embed_batched_no_trace_by_default(self):
        bk.embed_batched(_FakeEmbed(), ["a"])  # record_trace defaults False
        self.assertIsNone(self.cap.trace_row())

    def test_advisory_pii_scan_records_trace_when_asked(self):
        orig = bk.get_pii_backend
        bk.get_pii_backend = lambda: _FakePii()
        try:
            hits = ps.advisory_pii_scan("Jane lives here", record_trace=True, is_test=1)
        finally:
            bk.get_pii_backend = orig
        self.assertTrue(hits)
        row = self.cap.trace_row()
        self.assertEqual(row["name"], "pii")
        self.assertEqual(row["model"], "fake-pii-model")


if __name__ == "__main__":
    unittest.main()
