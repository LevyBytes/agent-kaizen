"""D4 shared artifact cache security, deduplication, sidecars, and retention."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kaizen_components.orchestration.artifact_cache import ArtifactCache, ArtifactCacheError


class ArtifactCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-d4-cache-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.logs: list[str] = []
        self.cache = ArtifactCache(self.root, logger=self.logs.append, clock=lambda: self.now)

    def test_exact_roots_atomic_scoped_parts_dedupe_and_metadata_only_sidecar(self) -> None:
        seen: list[Path] = []
        real_replace = os.replace

        def replace(source, target):
            seen.append(Path(source))
            return real_replace(source, target)

        with mock.patch("os.replace", side_effect=replace):
            one = self.cache.store(
                "images", b"image-bytes", scope_id="session-one", media_type="image/png", origin="host",
            )
            two = self.cache.store(
                "images", b"image-bytes", scope_id="session-two", media_type="image/png", origin="host",
            )
        self.assertEqual(one.sha256, two.sha256)
        self.assertEqual(
            self.cache.kind_root("images"),
            self.root / "AI" / "work" / "orchestration" / "ui-cache" / "images" / "sha256",
        )
        self.assertTrue(any(path.name.endswith(".part") for path in seen))
        self.assertTrue(any(hashlib.sha256(b"session-one").hexdigest()[:16] in path.name for path in seen))
        self.assertEqual(self.cache.read("images", one.artifact_ref, expected_bytes=11), b"image-bytes")
        sidecar = self.cache.metadata("images", one.artifact_ref)
        self.assertEqual(sidecar["created_at"], "2026-01-01T00:00:00Z")
        self.assertNotIn("image-bytes", json.dumps(sidecar))
        self.assertNotIn(str(self.root), json.dumps(sidecar))
        self.assertEqual(list(self.cache.kind_root("images").glob("*.part")), [])

    def test_hash_size_missing_corrupt_and_reparse_fail_closed(self) -> None:
        stored = self.cache.store("context", b"safe", scope_id="session")
        with self.assertRaises(ArtifactCacheError) as wrong_hash:
            self.cache.store("context", b"safe", scope_id="session", expected_sha256="1" * 64)
        self.assertEqual(wrong_hash.exception.code, "DENIED_ARTIFACT_HASH")
        with self.assertRaises(ArtifactCacheError) as wrong_size:
            self.cache.read("context", stored.artifact_ref, expected_bytes=9)
        self.assertEqual(wrong_size.exception.code, "DENIED_ARTIFACT_SIZE")
        target = self.cache.kind_root("context") / stored.sha256
        target.write_bytes(b"bad")
        with self.assertRaises(ArtifactCacheError) as corrupt:
            self.cache.read("context", stored.artifact_ref)
        self.assertEqual(corrupt.exception.code, "DENIED_ARTIFACT_CORRUPT")
        target.write_bytes(b"safe")
        with mock.patch.object(self.cache, "_is_reparse", side_effect=lambda path: path == target), \
                self.assertRaises(ArtifactCacheError) as reparse:
            self.cache.read("context", stored.artifact_ref)
        self.assertEqual(reparse.exception.code, "DENIED_ARTIFACT_PATH")

    def test_store_many_restores_exact_artifacts_and_sidecars_after_second_write_failure(self) -> None:
        self.cache.store("context", b"existing", scope_id="before", origin="file")

        def snapshot() -> dict[str, bytes]:
            return {
                path.relative_to(self.cache.cache_root).as_posix(): path.read_bytes()
                for path in self.cache.cache_root.rglob("*") if path.is_file()
            }

        before = snapshot()
        original = self.cache.store
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ArtifactCacheError("DENIED_ARTIFACT_WRITE", "fixture failure")
            return original(*args, **kwargs)

        with mock.patch.object(self.cache, "store", side_effect=fail_second), \
                self.assertRaises(ArtifactCacheError) as caught:
            self.cache.store_many(
                "context",
                [{"content": b"new-one", "origin": "file"},
                 {"content": b"new-two", "origin": "file"}],
                scope_id="request",
            )
        self.assertEqual(caught.exception.code, "DENIED_ARTIFACT_WRITE")
        self.assertEqual(snapshot(), before)

    def test_store_many_marks_rollback_unproven_when_exact_restoration_fails(self) -> None:
        self.cache.store("context", b"shared", scope_id="host", origin="selection")
        original_store = self.cache.store
        original_atomic = self.cache._atomic_bytes
        state = {"stores": 0, "rollback": False}

        def fail_second_store(*args, **kwargs):
            state["stores"] += 1
            if state["stores"] == 2:
                state["rollback"] = True
                raise ArtifactCacheError("DENIED_ARTIFACT_WRITE", "fixture forward failure")
            return original_store(*args, **kwargs)

        def fail_restoration(*args, **kwargs):
            if state["rollback"]:
                raise ArtifactCacheError("DENIED_ARTIFACT_WRITE", "fixture rollback failure")
            return original_atomic(*args, **kwargs)

        with mock.patch.object(self.cache, "store", side_effect=fail_second_store), \
                mock.patch.object(self.cache, "_atomic_bytes", side_effect=fail_restoration), \
                self.assertRaises(ArtifactCacheError) as caught:
            self.cache.store_many(
                "context",
                [{"content": b"shared", "origin": "file"},
                 {"content": b"new", "origin": "file"}],
                scope_id="request",
            )
        self.assertEqual(caught.exception.code, "DENIED_ARTIFACT_WRITE")
        self.assertTrue(caught.exception.rollback_unproven)

    def test_partial_cleanup_failure_does_not_mask_primary_atomic_write_code(self) -> None:
        with mock.patch("os.replace", side_effect=OSError("replace")), \
                mock.patch.object(Path, "unlink", side_effect=OSError("cleanup")), \
                self.assertRaises(ArtifactCacheError) as caught:
            self.cache.store("context", b"new", scope_id="request", origin="file")
        self.assertEqual(caught.exception.code, "DENIED_ARTIFACT_WRITE")
        self.assertFalse(caught.exception.rollback_unproven)

    def test_image_reference_batch_restores_all_preexisting_sidecars_on_second_failure(self) -> None:
        first = self.cache.store("images", b"first", scope_id="host", origin="host")
        second = self.cache.store("images", b"second", scope_id="host", origin="host")
        self.cache.add_image_reference([first.artifact_ref], "existing-session")
        before = {
            first.artifact_ref: self.cache.metadata("images", first.artifact_ref),
            second.artifact_ref: self.cache.metadata("images", second.artifact_ref),
        }
        original = self.cache._write_sidecar
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ArtifactCacheError("DENIED_ARTIFACT_WRITE", "fixture failure")
            return original(*args, **kwargs)

        with mock.patch.object(self.cache, "_write_sidecar", side_effect=fail_second), \
                self.assertRaises(ArtifactCacheError):
            self.cache.add_image_reference(
                [first.artifact_ref, second.artifact_ref, first.artifact_ref], "new-session",
            )
        self.assertEqual(self.cache.metadata("images", first.artifact_ref), before[first.artifact_ref])
        self.assertEqual(self.cache.metadata("images", second.artifact_ref), before[second.artifact_ref])

    def test_store_rejects_malformed_existing_and_new_origin_without_mutation(self) -> None:
        stored = self.cache.store("context", b"shared", scope_id="host", origin="selection")
        root = self.cache.kind_root("context")
        sidecar = root / f"{stored.sha256}.meta.json"
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        metadata["origin"] = 7
        sidecar.write_text(json.dumps(metadata), encoding="utf-8")
        before = sidecar.read_bytes()
        with self.assertRaises(ArtifactCacheError) as malformed:
            self.cache.store("context", b"shared", scope_id="file", origin="file")
        self.assertEqual(malformed.exception.code, "DENIED_ARTIFACT_METADATA")
        self.assertEqual(sidecar.read_bytes(), before)

        cache_before = {
            path.relative_to(self.cache.cache_root).as_posix(): path.read_bytes()
            for path in self.cache.cache_root.rglob("*") if path.is_file()
        }
        with self.assertRaises(ArtifactCacheError) as invalid:
            self.cache.store("context", b"new", scope_id="file", origin="")
        self.assertEqual(invalid.exception.code, "DENIED_ARTIFACT_INVALID")
        self.assertEqual({
            path.relative_to(self.cache.cache_root).as_posix(): path.read_bytes()
            for path in self.cache.cache_root.rglob("*") if path.is_file()
        }, cache_before)

    def test_availability_uses_metadata_and_stat_without_reading_historical_bytes(self) -> None:
        stored = self.cache.store("context", b"safe", scope_id="session")
        target = self.cache.kind_root("context") / stored.sha256
        sidecar = self.cache.kind_root("context") / f"{stored.sha256}.meta.json"
        with mock.patch.object(self.cache, "_read_bytes", side_effect=AssertionError("old bytes read")):
            self.assertEqual(
                self.cache.availability(
                    "context", stored.artifact_ref,
                    expected_sha256=stored.sha256, expected_bytes=stored.bytes,
                ),
                "available",
            )
        target.unlink()
        self.assertEqual(self.cache.availability("context", stored.artifact_ref), "expired")
        target.write_bytes(b"safe")
        sidecar.unlink()
        self.assertEqual(self.cache.availability("context", stored.artifact_ref), "unavailable")
        self.assertEqual(
            self.cache.availability("context", stored.artifact_ref, expected_sha256="f" * 64),
            "unavailable",
        )

    def test_cleanup_lifetimes_and_at_most_once_gate(self) -> None:
        image = self.cache.store("images", b"img", scope_id="session")
        orphan = self.cache.store("images", b"orphan", scope_id="staged")
        context = self.cache.store("context", b"ctx", scope_id="session")
        diff = self.cache.store("diffs", b"diff", scope_id="approval")
        self.cache.add_image_reference([image.artifact_ref], "session")
        self.cache.mark_diff_open([diff.artifact_ref], "approval")
        partial = self.cache.kind_root("context") / "fixture.session.part"
        partial.write_bytes(b"partial")
        old = (self.now - timedelta(hours=25)).timestamp()
        os.utime(partial, (old, old))

        self.now += timedelta(hours=25)
        first = self.cache.cleanup(is_session_resumable=lambda session_id: session_id == "session", force=True)
        self.assertEqual(first["status"], "OK")
        self.assertFalse(partial.exists())
        self.assertFalse((self.cache.kind_root("images") / orphan.sha256).exists())
        self.assertTrue((self.cache.kind_root("images") / image.sha256).exists())
        self.assertTrue((self.cache.kind_root("context") / context.sha256).exists())
        self.assertTrue((self.cache.kind_root("diffs") / diff.sha256).exists())
        self.assertEqual(
            self.cache.cleanup(is_session_resumable=lambda _session_id: True)["reason"], "cleanup_interval",
        )

        self.now += timedelta(days=30)
        second = self.cache.cleanup(is_session_resumable=lambda _session_id: False, force=True)
        self.assertEqual(second["status"], "OK")
        self.assertIsNotNone(self.cache.metadata("images", image.artifact_ref).get("unreferenced_at"))
        self.assertFalse((self.cache.kind_root("context") / context.sha256).exists())
        self.assertTrue((self.cache.kind_root("images") / image.sha256).exists())
        self.cache.mark_diff_terminal([diff.artifact_ref], "approval")

        self.now += timedelta(days=31)
        self.cache.cleanup(is_session_resumable=lambda _session_id: False, force=True)
        self.assertFalse((self.cache.kind_root("images") / image.sha256).exists())
        self.assertFalse((self.cache.kind_root("diffs") / diff.sha256).exists())

    def test_cleanup_logs_and_skips_bad_object_without_promoting_artifacts(self) -> None:
        stored = self.cache.store("context", b"ctx", scope_id="session")
        sidecar = self.cache.kind_root("context") / f"{stored.sha256}.meta.json"
        sidecar.write_text("not-json", encoding="utf-8")
        self.now += timedelta(days=31)
        result = self.cache.cleanup(force=True)
        self.assertEqual(result["status"], "OK")
        self.assertGreaterEqual(result["skipped"], 1)
        self.assertTrue(self.logs)
        self.assertEqual(
            {path.name for path in self.cache.cache_root.iterdir()},
            {"images", "context", "diffs", "cleanup-state.json"},
        )


if __name__ == "__main__":
    unittest.main()
