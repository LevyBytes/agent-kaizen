"""D5 exact bounded continuation builder."""

from __future__ import annotations

import unittest

from kaizen_components.session_protocol import SessionProtocolError, validate_image_refs
from kaizen_components.orchestration.continuation import (
    CONTINUATION_MAX_BYTES,
    ContinuationTooLarge,
    build_continuation_prompt,
)
from kaizen_components.orchestration.session_artifacts import RuntimeContext


class ContinuationBuilderTest(unittest.TestCase):
    def test_new_prompt_and_newest_whole_unicode_message_are_never_split(self) -> None:
        old = {"role": "user", "text": "OLD-" + "😀" * 200}
        newest = {"role": "assistant", "text": "NEWEST-" + "é" * 40}
        prompt = "CURRENT-😀-REQUEST"
        newest_only = build_continuation_prompt([newest], prompt)
        bounded = build_continuation_prompt(
            [old, newest], prompt, limit_bytes=newest_only.byte_count,
        )
        self.assertEqual(bounded.retained_message_count, 1)
        self.assertEqual(bounded.omitted_message_count, 1)
        self.assertIn("NEWEST-", bounded.adapter_prompt)
        self.assertNotIn("OLD-", bounded.adapter_prompt)
        self.assertTrue(bounded.adapter_prompt.endswith(prompt))
        self.assertEqual(bounded.byte_count, len(bounded.adapter_prompt.encode("utf-8")))

    def test_current_governed_context_is_inside_exact_limit_and_oversize_is_pre_record(self) -> None:
        context = RuntimeContext(
            id="ctx", kind="selection", source_path="src/app.py", sha256="a" * 64,
            text="context-😀", range={"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
        )
        complete = build_continuation_prompt([], "do it", [context])
        self.assertIn("KAIZEN_GOVERNED_CONTEXT_V1", complete.adapter_prompt)
        with self.assertRaises(ContinuationTooLarge):
            build_continuation_prompt([], "do it", [context], limit_bytes=complete.byte_count - 1)

    def test_long_history_stays_under_one_mib_and_carries_metadata_not_bytes(self) -> None:
        raw_attachment = {
            "id": "image", "kind": "image", "artifact_ref": "sha256:" + "b" * 64,
            "sha256": "b" * 64, "bytes": 12, "media_type": "image/png",
            "content": "raw-image-bytes",
        }
        with self.assertRaises(SessionProtocolError):
            validate_image_refs([raw_attachment])
        attachment = validate_image_refs([{key: value for key, value in raw_attachment.items() if key != "content"}])[0]
        messages = [
            {
                "role": "user", "text": f"message-{index}-" + "x" * 180_000,
                "attachments": [{**attachment, "id": f"image-{index}", "availability": "expired"}],
            }
            for index in range(8)
        ]
        result = build_continuation_prompt(messages, "always-retained")
        self.assertLessEqual(result.byte_count, CONTINUATION_MAX_BYTES)
        self.assertGreater(result.omitted_message_count, 0)
        self.assertIn("always-retained", result.adapter_prompt)
        self.assertIn("sha256:", result.adapter_prompt)
        self.assertNotIn("raw-image-bytes", result.adapter_prompt)


if __name__ == "__main__":
    unittest.main()
