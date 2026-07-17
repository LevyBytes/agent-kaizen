"""D3 immutable diff snapshot cache, rebase, and post-apply evidence."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kaizen_components.orchestration.approvals import (
    DENIED_SNAPSHOT_INVALID,
    DENIED_STALE,
    canonical_snapshot_set_sha256,
)
from kaizen_components.orchestration.diff_snapshots import (
    DENIED_PARTIAL_APPLY,
    DiffSnapshotError,
    DiffSnapshotManager,
)
from kaizen_components.orchestration.apply_evidence import normalize_apply_evidence
from kaizen_components.orchestration.workspace_path_authority import WorkspacePathError


class DiffSnapshotManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-d3-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.manager = DiffSnapshotManager(self.root)

    def write(self, relative: str, content: bytes | str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def test_create_modify_delete_rename_exact_bodies_and_artifacts(self) -> None:
        self.write("modify.txt", "before")
        self.write("delete.txt", "remove")
        self.write("old.txt", "rename")
        prepared = self.manager.prepare_changes("local_llm", [
            {"path": "create.txt", "kind": "create", "proposed_content": "created"},
            {"path": "modify.txt", "kind": "modify", "proposed_content": "after"},
            {"path": "delete.txt", "kind": "delete"},
            {"path": "new.txt", "old_path": "old.txt", "kind": "rename"},
        ])
        changes = {change["kind"]: change for change in prepared.body["file_changes"]}
        self.assertIsNone(changes["create"]["before"])
        self.assertIsNone(changes["delete"]["proposed"])
        self.assertEqual(changes["rename"]["old_path"], "old.txt")
        self.assertTrue(all(change["preview_mode"] == "text" for change in changes.values()))
        self.assertEqual(
            prepared.body["snapshot_set_sha256"], canonical_snapshot_set_sha256(prepared.body),
        )
        for change in changes.values():
            for side in (change["before"], change["proposed"]):
                if side is not None:
                    artifact = self.manager.cache_root / side["sha256"]
                    self.assertTrue(artifact.is_file())
                    self.assertEqual(self.manager.read_artifact(side), artifact.read_bytes())

    def test_canonical_hash_is_order_independent(self) -> None:
        self.write("a.txt", "a")
        self.write("b.txt", "b")
        first = self.manager.prepare_changes("local_llm", [
            {"path": "a.txt", "kind": "modify", "proposed_content": "A"},
            {"path": "b.txt", "kind": "modify", "proposed_content": "B"},
        ])
        reversed_body = dict(first.body)
        reversed_body["file_changes"] = list(reversed(first.body["file_changes"]))
        self.assertEqual(
            canonical_snapshot_set_sha256(first.body), canonical_snapshot_set_sha256(reversed_body),
        )

    def test_binary_unsupported_oversize_and_mixed_use_metadata(self) -> None:
        self.write("text.txt", "before")
        self.write("binary.bin", b"old\x00data")
        self.write("bad.bin", b"\xff")
        prepared = self.manager.prepare_changes("local_llm", [
            {"path": "text.txt", "kind": "modify", "proposed_content": "after"},
            {"path": "binary.bin", "kind": "modify", "proposed_content": b"new\x00data"},
            {"path": "bad.bin", "kind": "modify", "proposed_content": b"\xfe"},
            {"path": "large.txt", "kind": "create", "proposed_content": b"x" * (8 * 1024 * 1024 + 1)},
        ])
        by_path = {change["path"]: change for change in prepared.body["file_changes"]}
        self.assertEqual(by_path["text.txt"]["preview_mode"], "text")
        self.assertEqual(by_path["binary.bin"]["preview_reason"], "binary")
        self.assertEqual(by_path["bad.bin"]["preview_reason"], "unsupported_encoding")
        self.assertEqual(by_path["large.txt"]["preview_reason"], "oversize")

    def test_invalid_paths_collisions_and_atomic_failure_are_fail_closed(self) -> None:
        self.write("exists.txt", "x")
        cases = [
            [{"path": "../escape.txt", "kind": "create", "proposed_content": "x"}],
            [{"path": "exists.txt", "kind": "create", "proposed_content": "x"}],
            [{"path": "missing.txt", "kind": "modify", "proposed_content": "x"}],
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(DiffSnapshotError):
                self.manager.prepare_changes("local_llm", changes)

        isolated = DiffSnapshotManager(self.root / "atomic")
        with mock.patch("os.replace", side_effect=OSError("fixture")), self.assertRaises(DiffSnapshotError) as caught:
            isolated.prepare_changes("local_llm", [
                {"path": "a.txt", "kind": "create", "proposed_content": "x"},
            ])
        self.assertEqual(caught.exception.code, DENIED_SNAPSHOT_INVALID)
        self.assertEqual(list(isolated.cache_root.glob("*.part")), [])

    def test_clean_stale_rebase_requires_second_accept_and_updates_input(self) -> None:
        target = self.write("a.txt", "a\nold\nz\n")
        prepared = self.manager.prepare_request("local_llm", {
            "tool_name": "write_file",
            "tool_input": {"path": "a.txt", "content": "a\nnew\nz\n"},
        })
        self.manager.register("apr", prepared)
        target.write_bytes(b"external\na\nold\nz\n")
        refresh = self.manager.validate_release({"approval_id": "apr", "approval": prepared.body})
        self.assertEqual(refresh["status"], "REFRESH", refresh)
        self.assertEqual(refresh["body"]["approval_revision"], 2)
        refresh["commit"]()
        current = refresh["prepared"]
        self.assertEqual(current.updated_input["content"], "external\na\nnew\nz\n")
        accepted = self.manager.validate_release({"approval_id": "apr", "approval": current.body})
        self.assertEqual(accepted["status"], "OK")
        self.assertEqual(accepted["updated_input"]["content"], "external\na\nnew\nz\n")

    def test_nested_multi_file_rebase_preserves_request_order_and_attests_exact_bases(self) -> None:
        first = self.write("a.txt", "a\nold-a\nz\n")
        second = self.write("b.txt", "b\nold-b\nz\n")
        proposal = {
            "summary": "ordered pair",
            "changes": [
                {"kind": "modify", "path": "b.txt", "content": "b\nnew-b\nz\n"},
                {"kind": "modify", "path": "a.txt", "content": "a\nnew-a\nz\n"},
            ],
        }
        prepared = self.manager.prepare_request("claude", {
            "tool_name": "kaizen_propose_changes",
            "tool_input": proposal,
            "changes": [
                {"kind": "modify", "path": "b.txt", "proposed_content": "b\nnew-b\nz\n"},
                {"kind": "modify", "path": "a.txt", "proposed_content": "a\nnew-a\nz\n"},
            ],
            "updated_input": proposal,
        })
        self.manager.register("nested", prepared)
        first.write_bytes(b"external-a\na\nold-a\nz\n")
        second.write_bytes(b"external-b\nb\nold-b\nz\n")

        refresh = self.manager.validate_release({"approval_id": "nested", "approval": prepared.body})
        self.assertEqual(refresh["status"], "REFRESH", refresh)
        refresh["commit"]()
        current = refresh["prepared"]
        self.assertEqual(current.updated_input, {
            "summary": "ordered pair",
            "changes": [
                {"kind": "modify", "path": "b.txt", "content": "external-b\nb\nnew-b\nz\n"},
                {"kind": "modify", "path": "a.txt", "content": "external-a\na\nnew-a\nz\n"},
            ],
        })

        accepted = self.manager.validate_release({"approval_id": "nested", "approval": current.body})
        self.assertEqual(accepted["status"], "OK", accepted)
        self.assertEqual(accepted["approval_revision"], 2)
        self.assertEqual(accepted["snapshot_set_sha256"], current.body["snapshot_set_sha256"])
        self.assertEqual([item["path"] for item in accepted["approved_bases"]], ["b.txt", "a.txt"])
        self.assertEqual(
            [item["target_bytes"] for item in accepted["approved_bases"]],
            [len(second.read_bytes()), len(first.read_bytes())],
        )
        self.assertEqual(
            [item["final_sha256"] for item in accepted["approved_bases"]],
            [
                current.body["file_changes"][1]["proposed"]["sha256"],
                current.body["file_changes"][0]["proposed"]["sha256"],
            ],
        )

    def test_repeated_clean_staleness_can_create_next_revision(self) -> None:
        target = self.write("a.txt", "a\nold\nz\n")
        v1 = self.manager.prepare_changes("claude", [
            {"path": "a.txt", "kind": "modify", "proposed_content": "a\nnew\nz\n"},
        ])
        self.manager.register("apr", v1)
        target.write_bytes(b"one\na\nold\nz\n")
        first = self.manager.validate_release({"approval_id": "apr", "approval": v1.body})
        self.assertEqual(first["status"], "REFRESH", first)
        first["commit"]()
        target.write_bytes(b"one\na\nold\nz\ntwo\n")
        second = self.manager.validate_release({"approval_id": "apr", "approval": first["prepared"].body})
        self.assertEqual(second["status"], "REFRESH")
        self.assertEqual(second["body"]["approval_revision"], 3)

    def test_conflict_codex_metadata_and_create_collision_require_rerun(self) -> None:
        target = self.write("a.txt", "a\nold\nz\n")
        local = self.manager.prepare_changes("local_llm", [
            {"path": "a.txt", "kind": "modify", "proposed_content": "a\nnew\nz\n"},
        ])
        self.manager.register("local", local)
        target.write_bytes(b"a\nold\nz\na\nold\nz\n")
        self.assertEqual(
            self.manager.validate_release({"approval_id": "local", "approval": local.body})["code"],
            DENIED_STALE,
        )

        target.write_bytes(b"a\nold\nz\n")
        codex = self.manager.prepare_changes("codex", [
            {"path": "a.txt", "kind": "modify", "proposed_content": "a\nnew\nz\n"},
        ])
        self.manager.register("codex", codex)
        target.write_bytes(b"external\na\nold\nz\n")
        self.assertEqual(
            self.manager.validate_release({"approval_id": "codex", "approval": codex.body})["code"],
            DENIED_STALE,
        )

        binary = self.write("binary.bin", b"old\x00")
        metadata = self.manager.prepare_changes("local_llm", [
            {"path": "binary.bin", "kind": "modify", "proposed_content": b"new\x00"},
        ])
        self.manager.register("metadata", metadata)
        binary.write_bytes(b"external\x00")
        self.assertEqual(
            self.manager.validate_release({"approval_id": "metadata", "approval": metadata.body})["code"],
            DENIED_STALE,
        )

        create = self.manager.prepare_changes("local_llm", [
            {"path": "new.txt", "kind": "create", "proposed_content": "new"},
        ])
        self.manager.register("create", create)
        self.write("new.txt", "collision")
        self.assertEqual(
            self.manager.validate_release({"approval_id": "create", "approval": create.body})["code"],
            DENIED_STALE,
        )

        target.write_bytes(b"a\nold\nz\n")
        mixed = self.manager.prepare_changes("local_llm", [
            {"path": "a.txt", "kind": "modify", "proposed_content": "a\nnew\nz\n"},
            {"path": "mixed-new.txt", "kind": "create", "proposed_content": "new"},
        ])
        self.manager.register("mixed", mixed)
        target.write_bytes(b"external\na\nold\nz\n")
        self.assertEqual(
            self.manager.validate_release({"approval_id": "mixed", "approval": mixed.body})["code"],
            DENIED_STALE,
        )

        delete_target = self.write("delete.txt", "delete-base")
        delete = self.manager.prepare_changes("claude", [
            {"path": "delete.txt", "kind": "delete"},
        ])
        self.manager.register("delete", delete)
        delete_target.write_text("delete-changed", encoding="utf-8")
        self.assertEqual(
            self.manager.validate_release({"approval_id": "delete", "approval": delete.body})["code"],
            DENIED_STALE,
        )

        rename_source = self.write("rename-source.txt", "rename-base")
        rename = self.manager.prepare_changes("claude", [
            {"path": "rename-target.txt", "old_path": "rename-source.txt", "kind": "rename"},
        ])
        self.manager.register("rename", rename)
        rename_source.write_text("rename-changed", encoding="utf-8")
        self.assertEqual(
            self.manager.validate_release({"approval_id": "rename", "approval": rename.body})["code"],
            DENIED_STALE,
        )

    def test_missing_or_corrupt_artifact_is_snapshot_invalid(self) -> None:
        target = self.write("a.txt", "before")
        prepared = self.manager.prepare_changes("local_llm", [
            {"path": "a.txt", "kind": "modify", "proposed_content": "after"},
        ])
        self.manager.register("missing", prepared)
        side = prepared.body["file_changes"][0]["before"]
        (self.manager.cache_root / side["sha256"]).unlink()
        result = self.manager.validate_release({"approval_id": "missing", "approval": prepared.body})
        self.assertEqual(result["code"], DENIED_SNAPSHOT_INVALID)

        prepared = self.manager.prepare_changes("local_llm", [
            {"path": "a.txt", "kind": "modify", "proposed_content": "after"},
        ])
        self.manager.register("corrupt", prepared)
        side = prepared.body["file_changes"][0]["proposed"]
        (self.manager.cache_root / side["sha256"]).write_bytes(b"corrupt")
        result = self.manager.validate_release({"approval_id": "corrupt", "approval": prepared.body})
        self.assertEqual(result["code"], DENIED_SNAPSHOT_INVALID)
        self.assertEqual(target.read_text(encoding="utf-8"), "before")

    def test_reparse_cache_component_or_artifact_is_snapshot_invalid(self) -> None:
        prepared = self.manager.prepare_changes("local_llm", [
            {"path": "a.txt", "kind": "create", "proposed_content": "after"},
        ])
        side = prepared.body["file_changes"][0]["proposed"]
        digest = side["sha256"]

        with mock.patch.object(
            self.manager._artifact_cache, "_is_reparse", side_effect=lambda path: path == self.manager.cache_root,
        ), self.assertRaises(DiffSnapshotError) as cache_error:
            self.manager.read_artifact(side)
        self.assertEqual(cache_error.exception.code, DENIED_SNAPSHOT_INVALID)

        with mock.patch.object(
            self.manager._artifact_cache, "_is_reparse", side_effect=lambda path: path.name == digest,
        ), self.assertRaises(DiffSnapshotError) as artifact_error:
            self.manager.read_artifact(side)
        self.assertEqual(artifact_error.exception.code, DENIED_SNAPSHOT_INVALID)

    def test_discard_run_clears_pending_and_staged_state(self) -> None:
        prepared = self.manager.prepare_changes("local_llm", [
            {"path": "a.txt", "kind": "create", "proposed_content": "after"},
        ])
        self.manager.stage("run-1", "corr", prepared)
        self.manager.register("apr", prepared, agent_run_id="run-1")
        self.manager.discard_run("run-1")
        result = self.manager.validate_release({"approval_id": "apr", "approval": prepared.body})
        self.assertEqual(result["code"], DENIED_SNAPSHOT_INVALID)

    def test_post_apply_audit_reports_exact_mismatches_without_rollback_claim(self) -> None:
        target = self.write("a.txt", "before")
        prepared = self.manager.prepare_changes("local_llm", [
            {"path": "a.txt", "kind": "modify", "proposed_content": "after"},
        ])
        self.manager.register("apr", prepared)
        accepted = self.manager.validate_release({"approval_id": "apr", "approval": prepared.body})
        self.assertEqual(accepted["status"], "OK")
        target.write_text("wrong", encoding="utf-8")
        audit = accepted["post_apply"]()
        self.assertEqual(audit["code"], DENIED_PARTIAL_APPLY)
        self.assertEqual(audit["apply_extent"], "uncertain")
        self.assertTrue(audit["partial_apply"])
        self.assertEqual(audit["mismatches"][0], {
            "path": "", "reason": "apply_outcome_uncertain",
        })
        self.assertEqual(audit["mismatches"][1]["path"], "a.txt")
        evidence = normalize_apply_evidence(audit)
        self.assertFalse(evidence["mismatch_evidence_complete"])
        self.assertTrue(evidence["mismatch_evidence_uncertain"])
        self.assertNotIn("rollback", json.dumps(audit).casefold())

    def test_post_apply_extent_none_for_all_bases_is_not_partial_and_discards(self) -> None:
        self.write("modify.txt", "before")
        prepared = self.manager.prepare_changes("local_llm", [
            {"path": "modify.txt", "kind": "modify", "proposed_content": "after"},
            {"path": "create.txt", "kind": "create", "proposed_content": "created"},
        ])
        self.manager.register("none", prepared)
        accepted = self.manager.validate_release({"approval_id": "none", "approval": prepared.body})

        audit = accepted["post_apply"]()

        self.assertEqual(audit["status"], "DENIED")
        self.assertEqual(audit["apply_extent"], "none")
        self.assertFalse(audit["partial_apply"])
        self.assertEqual(len(audit["mismatches"]), 2)
        discarded = self.manager.audit("none")
        self.assertEqual(discarded["apply_extent"], "uncertain")

    def test_post_apply_extent_complete_for_create_modify_delete_and_rename(self) -> None:
        self.write("modify.txt", "before")
        self.write("delete.txt", "remove")
        self.write("old.txt", "rename")
        prepared = self.manager.prepare_changes("local_llm", [
            {"path": "create.txt", "kind": "create", "proposed_content": "created"},
            {"path": "modify.txt", "kind": "modify", "proposed_content": "after"},
            {"path": "delete.txt", "kind": "delete"},
            {"path": "new.txt", "old_path": "old.txt", "kind": "rename"},
        ])
        self.manager.register("complete", prepared)
        accepted = self.manager.validate_release({
            "approval_id": "complete", "approval": prepared.body,
        })
        self.write("create.txt", "created")
        self.write("modify.txt", "after")
        (self.root / "delete.txt").unlink()
        (self.root / "old.txt").replace(self.root / "new.txt")

        audit = accepted["post_apply"]()

        self.assertEqual(audit, {
            "status": "OK", "apply_extent": "complete", "partial_apply": False, "mismatches": [],
        })

    def test_post_apply_extent_partial_for_exact_base_and_final_mix(self) -> None:
        first = self.write("first.txt", "before-one")
        self.write("second.txt", "before-two")
        prepared = self.manager.prepare_changes("local_llm", [
            {"path": "first.txt", "kind": "modify", "proposed_content": "after-one"},
            {"path": "second.txt", "kind": "modify", "proposed_content": "after-two"},
        ])
        self.manager.register("partial", prepared)
        accepted = self.manager.validate_release({
            "approval_id": "partial", "approval": prepared.body,
        })
        first.write_text("after-one", encoding="utf-8")

        audit = accepted["post_apply"]()

        self.assertEqual(audit["apply_extent"], "partial")
        self.assertTrue(audit["partial_apply"])
        self.assertEqual([item["path"] for item in audit["mismatches"]], ["second.txt"])

    def test_post_apply_none_extent_rows_are_bounded_at_all_rename_paths(self) -> None:
        changes = []
        for index in range(64):
            self.write(f"old-{index}.txt", str(index))
            changes.append({
                "path": f"new-{index}.txt", "old_path": f"old-{index}.txt", "kind": "rename",
            })
        prepared = self.manager.prepare_changes("local_llm", changes)
        self.manager.register("bounded", prepared)
        accepted = self.manager.validate_release({
            "approval_id": "bounded", "approval": prepared.body,
        })

        audit = accepted["post_apply"]()

        self.assertEqual(audit["apply_extent"], "none")
        self.assertFalse(audit["partial_apply"])
        self.assertEqual(len(audit["mismatches"]), 128)

    def test_post_apply_directory_is_unreadable_not_fabricated_missing(self) -> None:
        target = self.write("a.txt", "before")
        prepared = self.manager.prepare_changes("local_llm", [
            {"path": "a.txt", "kind": "modify", "proposed_content": "after"},
        ])
        self.manager.register("directory", prepared)
        accepted = self.manager.validate_release({"approval_id": "directory", "approval": prepared.body})
        target.unlink()
        target.mkdir()

        audit = accepted["post_apply"]()
        evidence = normalize_apply_evidence(audit)

        self.assertEqual(audit["mismatches"], [{
            "path": "a.txt", "reason": "target_state_unreadable",
        }])
        self.assertFalse(evidence["mismatch_evidence_complete"])
        self.assertTrue(evidence["mismatch_evidence_uncertain"])

    def test_post_apply_hash_read_failure_is_explicitly_unreadable(self) -> None:
        self.write("a.txt", "before")
        authority = mock.Mock()
        authority.workspace_root = self.root
        authority.identity.return_value = mock.Mock()
        authority.read.side_effect = WorkspacePathError("simulated final hash read failure")
        manager = DiffSnapshotManager(self.root, workspace_path_authority=authority)
        prepared = manager.prepare_changes("local_llm", [
            {"path": "a.txt", "kind": "modify", "proposed_content": "after"},
        ])
        manager.register("read-failure", prepared)
        accepted = manager.validate_release({
            "approval_id": "read-failure", "approval": prepared.body,
        })

        audit = accepted["post_apply"]()

        self.assertEqual(audit["mismatches"], [{
            "path": "a.txt", "reason": "target_state_unreadable",
        }])
        self.assertEqual(audit["apply_extent"], "uncertain")
        authority.read.assert_called_once_with("a.txt", 64 * 1024 * 1024)

    def test_post_apply_reparse_is_explicitly_unreadable(self) -> None:
        self.write("a.txt", "before")
        authority = mock.Mock()
        authority.workspace_root = self.root
        authority.identity.side_effect = WorkspacePathError("simulated reparse path")
        manager = DiffSnapshotManager(self.root, workspace_path_authority=authority)
        prepared = manager.prepare_changes("local_llm", [
            {"path": "a.txt", "kind": "modify", "proposed_content": "after"},
        ])
        manager.register("reparse", prepared)
        accepted = manager.validate_release({"approval_id": "reparse", "approval": prepared.body})

        audit = accepted["post_apply"]()

        self.assertEqual(audit["mismatches"], [{
            "path": "a.txt", "reason": "target_state_unreadable",
        }])
        self.assertEqual(audit["apply_extent"], "uncertain")


if __name__ == "__main__":
    unittest.main()
