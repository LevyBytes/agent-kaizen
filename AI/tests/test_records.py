"""Record round-trips across representative command families."""

from __future__ import annotations

import re

from _harness import IsolatedDBTest

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class RecordsTest(IsolatedDBTest):
    def test_gotcha_add_inspect_query(self):
        rc, p = self.kz(
            "G1",
            "--title", "Cache drift pitfall",
            "--summary", "Caches drift from source.",
            "--body", "Evidence-driven note about cache drift.",
        )
        self.assertEqual(rc, 0, p)
        gid = p["id"]
        self.assertTrue(_HASH_RE.match(p.get("content_hash", "")), p)

        rc, p = self.kz("G4", "--id", gid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["record"]["id"], gid)

        rc, p = self.kz("G3", "--query", "Cache drift")
        self.assertEqual(rc, 0, p)
        self.assertIn(gid, [r["id"] for r in p["records"]])

    def test_source_lock_add_inspect_query(self):
        rc, p = self.kz(
            "S1",
            "--source-id", "turso-docs",
            "--authority-tier", "official_docs",
            "--url-or-repository", "https://docs.turso.tech",
            "--summary", "Turso documentation root.",
        )
        self.assertEqual(rc, 0, p)
        sid = p["id"]
        rc, p = self.kz("S3", "--id", sid)
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("S2", "--query", "turso")
        self.assertEqual(rc, 0, p)
        self.assertTrue(any(r["id"] == sid for r in p["records"]), p)

    def test_artifact_add_and_verify(self):
        f = self.root / "artifact.txt"
        f.write_text("hello kaizen", encoding="utf-8")
        rc, p = self.kz("A1", "--path", str(f))
        self.assertEqual(rc, 0, p)
        aid = p["id"]
        self.assertTrue(_HASH_RE.match(p.get("sha256", "")), p)
        rc, p = self.kz("A5", "--id", aid)
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["match"], p)

    def test_trace_add_and_report(self):
        rc, p = self.kz("T1", "--payload-json", '{"kind":"tool_call","summary":"unit trace","task_id":"task-1"}')
        self.assertEqual(rc, 0, p)
        self.assertTrue(_HASH_RE.match(p.get("content_hash", "")), p)
        rc, p = self.kz("T3", "--task-id", "task-1")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("events"), 1, p)

    def test_missing_input_file_is_denied(self):
        # read_text_file regression: a bad --payload-json-file (or --summary-file/--body-file)
        # must give a clean DENIED_FILE_NOT_FOUND, never a raw traceback / ERROR_UNEXPECTED.
        rc, p = self.kz("Q8", "--kind", "gotcha", "--payload-json-file", str(self.root / "does-not-exist.json"))
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_FILE_NOT_FOUND")

    def test_missing_required_field_is_structured_denial(self):
        # G1 without --title must deny cleanly (exit 2), not raise an unexpected error.
        rc, p = self.kz("G1", "--summary", "no title here", "--body", "b")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("status"), "DENIED")
