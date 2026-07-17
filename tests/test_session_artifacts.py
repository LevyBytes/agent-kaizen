"""D4 image/context materialization, policy, and prompt-injection contract."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kaizen_components.orchestration import policy
from kaizen_components.orchestration.artifact_cache import ARTIFACT_ORIGINS, ArtifactCache
from kaizen_components.orchestration.session_artifacts import (
    SessionArtifactError,
    SessionArtifactMaterializer,
    compose_governed_prompt,
)


class SessionArtifactMaterializerTest(unittest.TestCase):
    """Materialization and governed-prompt composition contracts, including denial codes and cache publication atomicity."""
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-d4-materialize-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cache = ArtifactCache(self.root)
        self.materializer = SessionArtifactMaterializer(self.root, self.cache)
        self.snapshot = self.make_snapshot()

    def make_snapshot(self, rules=()):
        """Build a frozen policy snapshot over the temporary workspace using the supplied authority rules."""
        return policy.build_policy_snapshot(
            "local_llm", "plan", str(self.root), [], list(rules),
            protected_paths=[], vendor_config_paths=[],
        )

    def image_ref(self, content: bytes, media_type: str, item_id: str) -> dict:
        """Store image bytes in cache and return the complete wire ImageRef mapping."""
        stored = self.cache.store(
            "images", content, scope_id="host", media_type=media_type, origin="host",
        )
        return {
            "id": item_id, "kind": "image", "artifact_ref": stored.artifact_ref,
            "sha256": stored.sha256, "bytes": stored.bytes, "media_type": media_type,
        }

    def materialize(self, *, attachments=None, context_refs=None, image=True, context=True):
        """Invoke the fixture-bound materializer with snapshot and feature-support defaults."""
        return self.materializer.materialize(
            engine="local_llm", snapshot=self.snapshot, scope_id="session",
            attachments=attachments, context_refs=context_refs,
            image_supported=image, context_supported=context,
        )

    def test_image_magic_declaration_and_content_deduplication(self) -> None:
        fixtures = [
            (b"\x89PNG\r\n\x1a\nbody", "image/png"),
            (b"\xff\xd8\xffbody", "image/jpeg"),
            (b"RIFF\x00\x00\x00\x00WEBPbody", "image/webp"),
            (b"GIF89abody", "image/gif"),
        ]
        refs = [self.image_ref(content, media_type, f"img-{index}")
                for index, (content, media_type) in enumerate(fixtures)]
        out = self.materialize(attachments=refs)
        self.assertEqual(list(out.attachments), refs)
        duplicate = [dict(refs[0]), {**refs[0], "id": "duplicate"}]
        with mock.patch.object(self.cache, "read", wraps=self.cache.read) as read:
            self.materialize(attachments=duplicate)
        self.assertEqual(read.call_count, 1)

    def test_image_count_size_type_hash_magic_and_unsupported_denials(self) -> None:
        valid = self.image_ref(b"\x89PNG\r\n\x1a\nbody", "image/png", "img")
        cases = [
            ([{**valid, "media_type": "image/jpeg"}], "DENIED_ATTACHMENT_INVALID"),
            ([{**valid, "sha256": "1" * 64, "artifact_ref": "sha256:" + "1" * 64}], "DENIED_ATTACHMENT_INVALID"),
            ([{**valid, "bytes": 4 * 1024 * 1024 + 1}], "DENIED_ATTACHMENT_TOO_LARGE"),
            ([{**valid, "media_type": "image/bmp"}], "DENIED_ATTACHMENT_INVALID"),
            ([{**valid, "id": f"img-{index}"} for index in range(5)], "DENIED_ATTACHMENT_TOO_LARGE"),
        ]
        for refs, code in cases:
            with self.subTest(code=code), self.assertRaises(SessionArtifactError) as caught:
                self.materialize(attachments=refs)
            self.assertEqual(caught.exception.code, code)
        bad_magic = self.image_ref(b"not-an-image", "image/png", "bad")
        with self.assertRaises(SessionArtifactError) as caught:
            self.materialize(attachments=[bad_magic])
        self.assertEqual(caught.exception.code, "DENIED_ATTACHMENT_INVALID")
        with self.assertRaises(SessionArtifactError) as caught:
            self.materialize(attachments=[valid], image=False)
        self.assertEqual(caught.exception.code, "DENIED_ATTACHMENT_UNSUPPORTED")

    def test_duplicate_image_reference_revalidates_each_declaration_without_mutation(self) -> None:
        valid = self.image_ref(b"\x89PNG\r\n\x1a\nbody", "image/png", "first")
        duplicate = {**valid, "id": "second"}

        def snapshot() -> dict[str, bytes]:
            return {
                path.relative_to(self.cache.cache_root).as_posix(): path.read_bytes()
                for path in self.cache.cache_root.rglob("*") if path.is_file()
            }

        before = snapshot()
        mismatched_bytes = {**duplicate, "bytes": int(valid["bytes"]) + 1}
        with mock.patch.object(self.cache, "read", wraps=self.cache.read) as read, \
                self.assertRaises(SessionArtifactError) as size_error:
            self.materialize(attachments=[valid, mismatched_bytes])
        self.assertEqual(size_error.exception.code, "DENIED_ATTACHMENT_INVALID")
        self.assertEqual(size_error.exception.field, "attachments[1].bytes")
        self.assertEqual(read.call_count, 1)
        self.assertEqual(snapshot(), before)

        cases = [
            ({**duplicate, "media_type": "image/jpeg"}, "attachments[1].media_type"),
            ({**duplicate, "sha256": "1" * 64}, "attachments[1].sha256"),
            ({**duplicate, "artifact_ref": "sha256:" + "1" * 64, "sha256": "1" * 64},
             "attachments[1]"),
        ]
        for declaration, field in cases:
            with self.subTest(field=field), self.assertRaises(SessionArtifactError) as caught:
                self.materialize(attachments=[valid, declaration])
            self.assertEqual(caught.exception.code, "DENIED_ATTACHMENT_INVALID")
            self.assertTrue(caught.exception.field.startswith(field))
            self.assertEqual(snapshot(), before)

    def test_file_and_exact_selection_materialize_metadata_and_untrusted_prompt(self) -> None:
        source = self.root / "src" / "a.txt"
        source.parent.mkdir()
        source.write_bytes(b"file @secret.txt\n")
        selection = b"selected @other.txt"
        staged = self.cache.store("context", selection, scope_id="host", origin="selection")
        refs = [
            {"id": "file", "kind": "file", "source_path": "src/a.txt"},
            {
                "id": "sel", "kind": "selection", "source_path": "src/a.txt",
                "range": {"start": {"line": 2, "character": 1}, "end": {"line": 2, "character": 19}},
                "snapshot_ref": staged.artifact_ref, "sha256": staged.sha256,
                "bytes": staged.bytes, "encoding": "utf-8",
            },
        ]
        out = self.materialize(context_refs=refs)
        durable = list(out.context_refs)
        self.assertEqual(durable[0]["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(durable[1], refs[1])
        self.assertNotIn("snapshot_ref", durable[0])
        self.assertNotIn(str(self.cache.cache_root), json.dumps(durable))
        prompt = compose_governed_prompt("review @src/a.txt", out.runtime_context)
        self.assertIn("KAIZEN_GOVERNED_CONTEXT_V1", prompt)
        self.assertIn("untrusted reference data", prompt)
        self.assertNotIn("@src/a.txt", prompt)
        self.assertNotIn("@secret.txt", prompt)
        self.assertNotIn("@other.txt", prompt)

    def test_extension_created_at_sidecar_selection_is_compatible(self) -> None:
        source = self.root / "dirty.txt"
        source.write_text("disk text", encoding="utf-8")
        content = b"unsaved selection"
        digest = hashlib.sha256(content).hexdigest()
        root = self.cache.kind_root("context", create=True)
        (root / digest).write_bytes(content)
        (root / f"{digest}.meta.json").write_text(json.dumps({
            "origin": "selection", "version": 1, "kind": "context", "sha256": digest,
            "bytes": len(content), "created_at": "2026-07-12T08:00:00.123Z",
        }), encoding="utf-8")
        ref = {
            "id": "u4", "kind": "selection", "source_path": "dirty.txt",
            "range": {"start": {"line": 4, "character": 2}, "end": {"line": 4, "character": 19}},
            "snapshot_ref": f"sha256:{digest}", "sha256": digest,
            "bytes": len(content), "encoding": "utf-8",
        }
        out = self.materialize(context_refs=[ref])
        self.assertEqual(out.context_refs, (ref,))
        self.assertEqual(out.runtime_context[0].text, "unsaved selection")

    def test_selection_rejects_malformed_origin_provenance(self) -> None:
        source = self.root / "selection.txt"
        source.write_text("disk", encoding="utf-8")
        stored = self.cache.store("context", b"selected", scope_id="host", origin="selection")
        ref = {
            "id": "sel", "kind": "selection", "source_path": source.name,
            "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 8}},
            "snapshot_ref": stored.artifact_ref, "sha256": stored.sha256,
            "bytes": stored.bytes, "encoding": "utf-8",
        }
        sidecar = self.cache.kind_root("context") / f"{stored.sha256}.meta.json"
        base = json.loads(sidecar.read_text(encoding="utf-8"))
        for malformed in (
            None,
            ["selection", 7],
            ["selection", "selection"],
            ["host"],
            ["approval_diff"],
            ["test-extension"],
            ["unknown"],
        ):
            with self.subTest(origins=malformed):
                sidecar.write_text(json.dumps({**base, "origins": malformed}), encoding="utf-8")
                with self.assertRaises(SessionArtifactError) as caught:
                    self.materialize(context_refs=[ref])
                self.assertEqual(caught.exception.code, "DENIED_CONTEXT_STALE")
        self.assertTrue({"host", "approval_diff", "test-extension"} < ARTIFACT_ORIGINS,
                        "the general artifact cache keeps its broader provenance vocabulary")

    def test_context_count_size_total_binary_utf8_range_stale_and_traversal(self) -> None:
        source = self.root / "a.txt"
        source.write_text("safe", encoding="utf-8")
        valid = {"id": "file", "kind": "file", "source_path": "a.txt"}
        cases: list[tuple[list[dict], str]] = [
            ([{**valid, "id": f"f-{index}"} for index in range(9)], "DENIED_CONTEXT_TOO_LARGE"),
            ([{**valid, "source_path": "../escape"}], "DENIED_CONTEXT_INVALID"),
        ]
        for refs, code in cases:
            with self.subTest(code=code), self.assertRaises(SessionArtifactError) as caught:
                self.materialize(context_refs=refs)
            self.assertEqual(caught.exception.code, code)

        source.write_bytes(b"x" * (256 * 1024 + 1))
        with self.assertRaises(SessionArtifactError) as caught:
            self.materialize(context_refs=[valid])
        self.assertEqual(caught.exception.code, "DENIED_CONTEXT_TOO_LARGE")
        for content in (b"bad\x00text", b"\xff"):
            source.write_bytes(content)
            with self.assertRaises(SessionArtifactError) as caught:
                self.materialize(context_refs=[valid])
            self.assertEqual(caught.exception.code, "DENIED_CONTEXT_INVALID")

        source.write_text("safe", encoding="utf-8")
        staged = self.cache.store("context", b"selected", scope_id="host", origin="selection")
        selection = {
            "id": "sel", "kind": "selection", "source_path": "a.txt",
            "range": {"start": {"line": 1, "character": 1}, "end": {"line": 1, "character": 1}},
            "snapshot_ref": staged.artifact_ref, "sha256": staged.sha256,
            "bytes": staged.bytes, "encoding": "utf-8",
        }
        with self.assertRaises(SessionArtifactError) as caught:
            self.materialize(context_refs=[selection])
        self.assertEqual(caught.exception.code, "DENIED_CONTEXT_INVALID")
        (self.cache.kind_root("context") / staged.sha256).unlink()
        selection["range"]["end"]["character"] = 9
        with self.assertRaises(SessionArtifactError) as caught:
            self.materialize(context_refs=[selection])
        self.assertEqual(caught.exception.code, "DENIED_CONTEXT_STALE")

    def test_valid_first_invalid_second_context_publishes_no_cache_artifact(self) -> None:
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        first.write_text("valid first", encoding="utf-8")
        second.write_bytes(b"invalid\x00second")
        refs = [
            {"id": "first", "kind": "file", "source_path": first.name},
            {"id": "second", "kind": "file", "source_path": second.name},
        ]

        def snapshot() -> dict[str, bytes]:
            if not self.cache.cache_root.exists():
                return {}
            return {
                path.relative_to(self.cache.cache_root).as_posix(): path.read_bytes()
                for path in self.cache.cache_root.rglob("*") if path.is_file()
            }

        before = snapshot()
        with self.assertRaises(SessionArtifactError) as caught:
            self.materialize(context_refs=refs)
        self.assertEqual(caught.exception.code, "DENIED_CONTEXT_INVALID")
        self.assertEqual(snapshot(), before)

    def test_file_and_selection_same_bytes_preserve_both_provenances_in_either_order(self) -> None:
        def selection(reference, digest, size, item_id):
            return {
                "id": item_id, "kind": "selection", "source_path": f"{item_id}.txt",
                "range": {"start": {"line": 1, "character": 0},
                          "end": {"line": 1, "character": size}},
                "snapshot_ref": reference, "sha256": digest, "bytes": size, "encoding": "utf-8",
            }

        first_content = b"selection first"
        first_file = self.root / "selection-first.txt"
        first_file.write_bytes(first_content)
        first_staged = self.cache.store("context", first_content, scope_id="host", origin="selection")
        self.materialize(context_refs=[{
            "id": "file-after-selection", "kind": "file", "source_path": first_file.name,
        }])
        first_meta = self.cache.metadata("context", first_staged.artifact_ref)
        self.assertEqual(first_meta["origin"], "selection")
        self.assertEqual(first_meta["origins"], ["file", "selection"])
        self.materialize(context_refs=[selection(
            first_staged.artifact_ref, first_staged.sha256, first_staged.bytes, "selection-first",
        )])

        second_content = b"file first"
        second_file = self.root / "file-first.txt"
        second_file.write_bytes(second_content)
        file_turn = self.materialize(context_refs=[{
            "id": "file-first", "kind": "file", "source_path": second_file.name,
        }])
        digest = file_turn.context_refs[0]["sha256"]
        second_staged = self.cache.store("context", second_content, scope_id="host", origin="selection")
        second_meta = self.cache.metadata("context", second_staged.artifact_ref)
        self.assertEqual(second_meta["origin"], "file")
        self.assertEqual(second_meta["origins"], ["file", "selection"])
        self.assertEqual(second_staged.sha256, digest)
        self.materialize(context_refs=[selection(
            second_staged.artifact_ref, second_staged.sha256, second_staged.bytes, "file-first",
        )])

    def test_context_total_dedupe_reparse_policy_and_unsupported(self) -> None:
        refs = []
        for index in range(5):
            path = self.root / f"large-{index}.txt"
            path.write_bytes(bytes([65 + index]) * (256 * 1024))
            refs.append({"id": f"f-{index}", "kind": "file", "source_path": path.name})
        with self.assertRaises(SessionArtifactError) as caught:
            self.materialize(context_refs=refs)
        self.assertEqual(caught.exception.code, "DENIED_CONTEXT_TOO_LARGE")

        shared = self.root / "shared.txt"
        shared.write_text("same", encoding="utf-8")
        duplicate = [
            {"id": "one", "kind": "file", "source_path": "shared.txt"},
            {"id": "two", "kind": "file", "source_path": "shared.txt"},
        ]
        out = self.materialize(context_refs=duplicate)
        self.assertEqual(out.context_refs[0]["sha256"], out.context_refs[1]["sha256"])
        self.assertEqual(
            len([path for path in self.cache.kind_root("context").iterdir()
                 if len(path.name) == 64 and path.name == out.context_refs[0]["sha256"]]),
            1,
        )

        with mock.patch.object(self.materializer, "_is_reparse", side_effect=lambda path: path == shared), \
                self.assertRaises(SessionArtifactError) as reparse:
            self.materialize(context_refs=[duplicate[0]])
        self.assertEqual(reparse.exception.code, "DENIED_CONTEXT_INVALID")

        deny = {
            "id": "deny-read", "rule_type": "deny", "verb": "file_read",
            "match_kind": "path_prefix", "pattern": str(shared), "engine": None, "enabled": True,
        }
        self.snapshot = self.make_snapshot([deny])
        with self.assertRaises(SessionArtifactError) as policy_error:
            self.materialize(context_refs=[duplicate[0]])
        self.assertEqual(policy_error.exception.code, "DENIED_CONTEXT_POLICY")
        with self.assertRaises(SessionArtifactError) as unsupported:
            self.materialize(context_refs=[duplicate[0]], context=False)
        self.assertEqual(unsupported.exception.code, "DENIED_CONTEXT_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
