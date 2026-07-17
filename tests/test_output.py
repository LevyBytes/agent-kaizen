"""--json Unicode safety (H2.0 regression). emit/emit_error must serialize non-ASCII payloads (CJK,
emoji) without raising when the underlying text stream is cp1252-encoded (the Windows console/pipe
default), and the emitted text must round-trip back through json.loads. Guards output.py's explicit
ensure_ascii=True: with it, non-ASCII escapes to \\uXXXX so the write is pure ASCII and never trips a
cp1252 UnicodeEncodeError. A verify-only item -- if it exposes a real hole, output.py is the fix.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

from _harness import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT))

from kaizen_components import output as O  # noqa: E402


# A payload whose values are outside cp1252 (CJK + emoji): a naive write to a cp1252 stream raises.
CJK_EMOJI = "モデル-\U0001f916"  # "モデル-🤖"
PAYLOAD = {"status": "OK", "id": "rec_1", "model": CJK_EMOJI, "note": "你好"}


class _Cp1252Stream(io.TextIOWrapper):
    """A cp1252-encoded text stream over an in-memory buffer -- stands in for a Windows cp1252 console
    so a non-ASCII byte would raise UnicodeEncodeError on write unless it was escaped first."""

    def __init__(self) -> None:
        self._buffer = io.BytesIO()
        super().__init__(self._buffer, encoding="cp1252", newline="")

    def getvalue(self) -> str:
        self.flush()
        return self._buffer.getvalue().decode("cp1252")


class _redirect:
    """Swap sys.stdout/sys.stderr to cp1252 streams for the duration of the block."""

    def __enter__(self) -> tuple[_Cp1252Stream, _Cp1252Stream]:
        self._out, self._err = sys.stdout, sys.stderr
        self.out, self.err = _Cp1252Stream(), _Cp1252Stream()
        sys.stdout, sys.stderr = self.out, self.err
        return self.out, self.err

    def __exit__(self, *exc: object) -> None:
        sys.stdout, sys.stderr = self._out, self._err


class JsonUnicodeSafetyTest(unittest.TestCase):
    def test_emit_json_non_ascii_does_not_raise_on_cp1252_stream(self):
        with _redirect() as (out, _err):
            rc = O.emit(PAYLOAD, as_json=True)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        # Round-trips: the escaped ASCII decodes back to the original non-ASCII values.
        parsed = json.loads(text)
        self.assertEqual(parsed["model"], CJK_EMOJI)
        self.assertEqual(parsed["note"], "你好")
        # The wire form is pure ASCII (that is WHY the cp1252 write is safe).
        self.assertTrue(text.isascii(), text)

    def test_emit_error_json_non_ascii_does_not_raise_on_cp1252_stream(self):
        err_payload = {"code": "ERROR", "message": CJK_EMOJI, "exit_code": 2}
        with _redirect() as (_out, err):
            rc = O.emit_error(err_payload, as_json=True)
        self.assertEqual(rc, 2)
        text = err.getvalue()
        parsed = json.loads(text)
        self.assertEqual(parsed["message"], CJK_EMOJI)
        self.assertTrue(text.isascii(), text)

    def test_emit_non_json_message_with_non_ascii_is_safe(self):
        # The human path (message pre-rendered by a caller) also writes via print -> cp1252 stream.
        # A non-ASCII message here is written directly; cp1252 covers Latin-1 range so keep it in-range
        # for the human path, and assert no raise.
        with _redirect() as (out, _err):
            rc = O.emit({"status": "OK", "message": "done é"}, as_json=False)  # 'é' is cp1252-safe
        self.assertEqual(rc, 0)
        self.assertIn("done é", out.getvalue())


if __name__ == "__main__":
    unittest.main()
