"""Input-hardening tests: unified path policy, UTF-8 decode denial, LIKE wildcard escaping,
and PDF ingestion guards. All exercise the real CLI against an isolated data plane."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from _harness import IsolatedDBTest

try:
    import pypdf  # noqa: F401

    HAS_PYPDF = True
except Exception:
    HAS_PYPDF = False


def _external_file(name: str, content: str) -> Path:
    """A file OUTSIDE any KAIZEN_REPO_ROOT (its own temp dir), for path-policy tests."""
    ext_dir = Path(tempfile.mkdtemp(prefix="kaizen-external-"))
    path = ext_dir / name
    path.write_text(content, encoding="utf-8")
    return path


class PathPolicyTest(IsolatedDBTest):
    def test_e1_external_denied_without_flag(self):
        ext = _external_file("note.txt", "outside-repo content")
        self.addCleanup(shutil.rmtree, ext.parent, ignore_errors=True)
        rc, payload = self.kz("E1", "--path", str(ext))
        self.assertEqual(rc, 2, payload)
        self.assertEqual(payload.get("code"), "DENIED_PATH_OUTSIDE_REPO", payload)

    def test_e1_traversal_denied(self):
        # POSIX-form traversal is a real escape on every platform (the 177a0ef lesson).
        rc, payload = self.kz("E1", "--path", "../escape.txt")
        self.assertEqual(rc, 2, payload)
        self.assertEqual(payload.get("code"), "DENIED_PATH_OUTSIDE_REPO", payload)

    def test_e1_external_allowed_stores_sanitized_origin(self):
        ext = _external_file("secret-notes.txt", "# Doc\n\nExternal but allowed.\n")
        self.addCleanup(shutil.rmtree, ext.parent, ignore_errors=True)
        rc, payload = self.kz("E1", "--path", str(ext), "--allow-external")
        self.assertEqual(rc, 0, payload)
        origin = payload.get("origin_ref", "")
        self.assertEqual(origin, "external:secret-notes.txt", payload)
        # No absolute path, drive letter, or home directory leaks into the stored origin.
        self.assertNotIn(str(ext.parent), origin)
        self.assertNotIn(":", origin.replace("external:", ""))

    def test_a2_external_denied_without_flag_then_sanitized_with_flag(self):
        ext = _external_file("hashme.txt", "content to hash")
        self.addCleanup(shutil.rmtree, ext.parent, ignore_errors=True)
        rc, denied = self.kz("A2", "--path", str(ext))
        self.assertEqual(rc, 2, denied)
        self.assertEqual(denied.get("code"), "DENIED_PATH_OUTSIDE_REPO", denied)

        rc, ok = self.kz("A2", "--path", str(ext), "--allow-external")
        self.assertEqual(rc, 0, ok)
        self.assertEqual(ok.get("path"), "external:hashme.txt", ok)
        self.assertTrue(ok.get("sha256"), ok)


class DecodeDenialTest(IsolatedDBTest):
    def test_non_utf8_file_denies_cleanly(self):
        bad = self.root / "bad.json"
        bad.write_bytes(b"\xff\xfe\x00 not valid utf-8 \x80\x81")
        rc, payload = self.kz("Q8", "--kind", "gotcha", "--payload-json-file", str(bad))
        self.assertEqual(rc, 2, payload)
        self.assertEqual(payload.get("code"), "DENIED_FILE_NOT_UTF8", payload)


class LikeEscapingTest(IsolatedDBTest):
    def _ingest_and_chunk(self, name: str, body: str) -> None:
        doc = self.root / name
        doc.write_text(body, encoding="utf-8")
        rc, ing = self.kz("E1", "--path", str(doc))
        self.assertEqual(rc, 0, ing)
        rc, ch = self.kz("E3", "--id", ing["id"])
        self.assertEqual(rc, 0, ch)

    def test_underscore_wildcard_is_escaped(self):
        # LIKE treats '_' as any single char; escaping keeps 'a_b' from matching 'axb'.
        self._ingest_and_chunk("doc_ab.md", "# Doc\n\nThe literal token a_b is the marker here.\n")
        self._ingest_and_chunk("doc_axb.md", "# Doc\n\nThe literal token axb is a decoy here.\n")

        rc, q = self.kz("E4", "--query", "a_b")
        self.assertEqual(rc, 0, q)
        self.assertEqual(q.get("mode"), "like", q)
        snippets = [r.get("snippet", "") for r in q.get("records", [])]
        self.assertTrue(snippets, q)
        self.assertTrue(all("a_b" in s for s in snippets), snippets)
        self.assertFalse(any("axb" in s for s in snippets), snippets)


class PdfGuardTest(IsolatedDBTest):
    def test_oversize_pdf_denied_without_pypdf(self):
        # Size gate runs BEFORE the pypdf import, so this needs no optional dependency.
        pdf = self.root / "big.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + b"0" * 400)
        rc, payload = self.kz("E1", "--path", str(pdf), env={"KAIZEN_MAX_PDF_BYTES": "100"})
        self.assertEqual(rc, 2, payload)
        self.assertEqual(payload.get("code"), "DENIED_FILE_TOO_LARGE", payload)

    @unittest.skipUnless(HAS_PYPDF, "pypdf not installed (opt-in docs backend)")
    def test_encrypted_pdf_denied(self):
        pdf = self.root / "locked.pdf"
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.encrypt("pw")
        with open(pdf, "wb") as handle:
            writer.write(handle)
        rc, payload = self.kz("E1", "--path", str(pdf))
        self.assertEqual(rc, 2, payload)
        self.assertEqual(payload.get("code"), "DENIED_PDF_ENCRYPTED", payload)

    @unittest.skipUnless(HAS_PYPDF, "pypdf not installed (opt-in docs backend)")
    def test_too_many_pages_denied(self):
        pdf = self.root / "many.pdf"
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.add_blank_page(width=72, height=72)
        with open(pdf, "wb") as handle:
            writer.write(handle)
        rc, payload = self.kz("E1", "--path", str(pdf), env={"KAIZEN_MAX_PDF_PAGES": "1"})
        self.assertEqual(rc, 2, payload)
        self.assertEqual(payload.get("code"), "DENIED_PDF_TOO_MANY_PAGES", payload)

    @unittest.skipUnless(HAS_PYPDF, "pypdf not installed (opt-in docs backend)")
    def test_no_text_pdf_denied(self):
        pdf = self.root / "blank.pdf"
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with open(pdf, "wb") as handle:
            writer.write(handle)
        rc, payload = self.kz("E1", "--path", str(pdf))
        self.assertEqual(rc, 2, payload)
        self.assertEqual(payload.get("code"), "DENIED_PDF_NO_TEXT", payload)

    @unittest.skipUnless(HAS_PYPDF, "pypdf not installed (opt-in docs backend)")
    def test_malformed_pdf_denied(self):
        pdf = self.root / "broken.pdf"
        pdf.write_bytes(b"%PDF-1.4\nthis is not a real pdf body\n")
        rc, payload = self.kz("E1", "--path", str(pdf))
        self.assertEqual(rc, 2, payload)
        self.assertEqual(payload.get("code"), "DENIED_PDF_UNREADABLE", payload)


if __name__ == "__main__":
    unittest.main()
