"""Focused URL-integrity tests for the YouTube transcript helper."""

from __future__ import annotations

import unittest

from support_scripts.yt_transcript import _strip_srv3_format


class StripSrv3FormatTest(unittest.TestCase):
    def test_removes_exact_item_in_every_query_position(self) -> None:
        cases = {
            "https://example.test/timedtext?fmt=srv3&lang=en": "https://example.test/timedtext?lang=en",
            "https://example.test/timedtext?lang=en&fmt=srv3&kind=asr": "https://example.test/timedtext?lang=en&kind=asr",
            "https://example.test/timedtext?lang=en&fmt=srv3": "https://example.test/timedtext?lang=en",
            "https://example.test/timedtext?lang=en&fmt=json3": "https://example.test/timedtext?lang=en&fmt=json3",
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(_strip_srv3_format(original), expected)

    def test_preserves_signed_query_bytes_and_order(self) -> None:
        original = "https://example.test/timedtext?expire=1&sig=A%2FB%2Bc%3D&fmt=srv3&key=yt8&caps=asr"
        self.assertEqual(
            _strip_srv3_format(original),
            "https://example.test/timedtext?expire=1&sig=A%2FB%2Bc%3D&key=yt8&caps=asr",
        )


if __name__ == "__main__":
    unittest.main()
