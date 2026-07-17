"""WorkspaceProposalExecutor stage, mutation, recovery, and failure-injection suite, including the Windows-only exact-removal lane."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from kaizen_components.orchestration.proposal_executor import (
    DENIED_APPROVAL_STALE_RERUN_REQUIRED,
    DENIED_WORKSPACE_PROPOSAL_ALREADY_APPLIED,
    DENIED_WORKSPACE_RECOVERY_PERSIST_FAILED,
    DENIED_WORKSPACE_RECOVERY_REQUIRED,
    ProposalExecutionError,
    WorkspaceProposalExecutor,
)
from kaizen_components.orchestration.workspace_path_authority import WorkspacePathError


WORK_ROOT = Path(__file__).resolve().parents[1] / "AI" / "work" / "proposal-executor-tests"


class WorkspaceProposalExecutorTest(unittest.TestCase):
    """State-machine coverage centered on recovery callback observation and prepare/commit/abort invariants."""
    def setUp(self) -> None:
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="plane-", dir=WORK_ROOT)
        self.root = Path(self.temp.name)
        self.recovery_states = []

        def persist_recovery(state):
            self.recovery_states.append(state)
            return True

        self.executor = WorkspaceProposalExecutor(self.root, recovery_callback=persist_recovery)
        self.executors = [self.executor]

    def tearDown(self) -> None:
        for executor in reversed(self.executors):
            executor.close()
        self.temp.cleanup()

    def assert_no_staged_files(self) -> None:
        self.assertEqual(list(self.root.rglob("*.kaizen-*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "exact removal/rename lane is Windows-only")
    def test_create_modify_delete_and_byte_preserving_rename_apply_in_request_order(self) -> None:
        (self.root / "modify.txt").write_text("before", encoding="utf-8")
        (self.root / "delete.txt").write_text("delete", encoding="utf-8")
        rename_bytes = b"\x00byte-preserving\xff"
        (self.root / "old.bin").write_bytes(rename_bytes)
        proposal = {
            "summary": "all kinds",
            "changes": [
                {"kind": "create", "path": "create.txt", "content": "new"},
                {"kind": "modify", "path": "modify.txt", "content": "after"},
                {"kind": "delete", "path": "delete.txt"},
                {"kind": "rename", "old_path": "old.bin", "path": "renamed.bin"},
            ],
        }

        result = self.executor.prepare(proposal).apply(proposal)

        self.assertEqual(result, {"status": "OK", "partial_apply": False, "mismatches": []})
        self.assertEqual((self.root / "create.txt").read_text(encoding="utf-8"), "new")
        self.assertEqual((self.root / "modify.txt").read_text(encoding="utf-8"), "after")
        self.assertFalse((self.root / "delete.txt").exists())
        self.assertFalse((self.root / "old.bin").exists())
        self.assertEqual((self.root / "renamed.bin").read_bytes(), rename_bytes)
        self.assert_no_staged_files()

    @unittest.skipUnless(os.name == "nt", "exact removal/rename lane is Windows-only")
    def test_phase_markers_precede_staging_and_mutation(self) -> None:
        target = self.root / "phase.txt"
        target.write_text("base", encoding="utf-8")
        observations = []

        def persist(state):
            observations.append((state and state["phase"], len(list(self.root.rglob("*.kaizen-*.tmp")))))
            return True

        executor = WorkspaceProposalExecutor(self.root, recovery_callback=persist)
        self.executors.append(executor)
        proposal = {"changes": [{"kind": "modify", "path": "phase.txt", "content": "final"}]}

        self.assertEqual(executor.prepare(proposal).apply(proposal)["status"], "OK")
        self.assertEqual(observations, [("staging", 0), ("mutating", 2), (None, 0)])

    @unittest.skipUnless(os.name == "nt", "exact removal/rename lane is Windows-only")
    def test_valid_sixty_four_modifies_stage_128_descriptors(self) -> None:
        changes = []
        for index in range(64):
            path = f"item-{index:02d}.txt"
            (self.root / path).write_text("b", encoding="utf-8")
            changes.append({"kind": "modify", "path": path, "content": "f"})
        proposal = {"changes": changes}

        result = self.executor.prepare(proposal).apply(proposal)

        self.assertEqual(result["status"], "OK")
        mutating = next(state for state in self.recovery_states if state and state["phase"] == "mutating")
        self.assertEqual(len(mutating["staged"]), 128)

    def test_sixty_five_changes_are_denied_before_staging(self) -> None:
        proposal = {"changes": [
            {"kind": "create", "path": f"item-{index:02d}.txt", "content": "x"}
            for index in range(65)
        ]}
        with self.assertRaises(ProposalExecutionError):
            self.executor.prepare(proposal)
        self.assertEqual(self.recovery_states, [])
        self.assert_no_staged_files()

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive alias rejection")
    def test_case_aliases_are_denied_before_marker_or_mutation(self) -> None:
        target = self.root / "Alias.txt"
        target.write_text("base", encoding="utf-8")
        for proposal in (
            {"changes": [
                {"kind": "modify", "path": "Alias.txt", "content": "one"},
                {"kind": "delete", "path": "alias.txt"},
            ]},
            {"changes": [
                {"kind": "rename", "old_path": "Alias.txt", "path": "moved.txt"},
                {"kind": "create", "path": "ALIAS.TXT", "content": "new"},
            ]},
        ):
            with self.subTest(proposal=proposal), self.assertRaises(ProposalExecutionError):
                self.executor.prepare(proposal)
            self.assertEqual(target.read_text(encoding="utf-8"), "base")
            self.assertEqual(self.recovery_states, [])
            self.assert_no_staged_files()

    @unittest.skipUnless(os.name == "nt", "Windows reserved device names")
    def test_reserved_device_path_is_denied_before_marker(self) -> None:
        with self.assertRaises(ProposalExecutionError):
            self.executor.prepare({"changes": [
                {"kind": "create", "path": "NUL.txt", "content": "never"},
            ]})
        self.assertEqual(self.recovery_states, [])
        self.assert_no_staged_files()

    @unittest.skipUnless(os.name == "nt", "exact removal/rename lane is Windows-only")
    def test_staged_aggregate_boundary_counts_backups(self) -> None:
        accepted = self.root / "accepted.txt"
        accepted.write_text("base", encoding="utf-8")
        accepted_proposal = {"changes": [
            {"kind": "modify", "path": "accepted.txt", "content": "done"},
        ]}
        with mock.patch(
            "kaizen_components.orchestration.proposal_executor._MAX_STAGED_TOTAL_BYTES", 8,
        ):
            self.assertEqual(self.executor.prepare(accepted_proposal).apply(accepted_proposal)["status"], "OK")

        denied = self.root / "denied.txt"
        denied.write_text("base", encoding="utf-8")
        denied_proposal = {"changes": [
            {"kind": "modify", "path": "denied.txt", "content": "done"},
        ]}
        with mock.patch(
            "kaizen_components.orchestration.proposal_executor._MAX_STAGED_TOTAL_BYTES", 7,
        ):
            with self.assertRaises(ProposalExecutionError) as refused:
                self.executor.prepare(denied_proposal)
        self.assertEqual(refused.exception.code, "DENIED_APPROVAL_SNAPSHOT_INVALID")
        self.assertEqual(denied.read_text(encoding="utf-8"), "base")
        self.assert_no_staged_files()

    @unittest.skipUnless(os.name == "nt", "exact removal/rename lane is Windows-only")
    def test_aggregate_overflow_stops_before_reading_the_next_base(self) -> None:
        for name in ("one.txt", "two.txt"):
            (self.root / name).write_text("12345", encoding="utf-8")
        proposal = {"changes": [
            {"kind": "delete", "path": "one.txt"},
            {"kind": "delete", "path": "two.txt"},
        ]}
        with mock.patch(
            "kaizen_components.orchestration.proposal_executor._MAX_STAGED_TOTAL_BYTES", 8,
        ), mock.patch.object(
            self.executor._authority, "read", wraps=self.executor._authority.read,
        ) as bounded_read:
            with self.assertRaises(ProposalExecutionError) as refused:
                self.executor.prepare(proposal)
        self.assertEqual(refused.exception.code, "DENIED_APPROVAL_SNAPSHOT_INVALID")
        self.assertEqual(bounded_read.call_count, 1)
        self.assertEqual((self.root / "one.txt").read_text(encoding="utf-8"), "12345")
        self.assertEqual((self.root / "two.txt").read_text(encoding="utf-8"), "12345")

    @unittest.skipUnless(os.name == "nt", "exact removal/rename lane is Windows-only")
    def test_stale_second_base_denies_with_zero_target_mutation(self) -> None:
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        first.write_text("base-one", encoding="utf-8")
        second.write_text("base-two", encoding="utf-8")
        proposal = {"changes": [
            {"kind": "modify", "path": "first.txt", "content": "new-one"},
            {"kind": "modify", "path": "second.txt", "content": "new-two"},
        ]}
        prepared = self.executor.prepare(proposal)
        second.write_text("changed-after-preview", encoding="utf-8")

        result = prepared.apply(proposal)

        self.assertEqual(result["status"], "DENIED")
        self.assertEqual(result["code"], DENIED_APPROVAL_STALE_RERUN_REQUIRED)
        self.assertFalse(result["partial_apply"])
        self.assertEqual(first.read_text(encoding="utf-8"), "base-one")
        self.assertEqual(second.read_text(encoding="utf-8"), "changed-after-preview")
        self.assertEqual([item["path"] for item in result["mismatches"]], ["second.txt"])
        self.assert_no_staged_files()

    def test_create_collision_after_prepare_denies_without_overwrite(self) -> None:
        proposal = {"changes": [{"kind": "create", "path": "new.txt", "content": "approved"}]}
        prepared = self.executor.prepare(proposal)
        (self.root / "new.txt").write_text("competing", encoding="utf-8")

        result = prepared.apply(proposal)

        self.assertEqual(result["code"], DENIED_APPROVAL_STALE_RERUN_REQUIRED)
        self.assertFalse(result["partial_apply"])
        self.assertEqual((self.root / "new.txt").read_text(encoding="utf-8"), "competing")
        self.assertEqual(result["mismatches"][0]["actual_sha256"], hashlib.sha256(b"competing").hexdigest())
        self.assert_no_staged_files()

    def test_prepared_proposal_is_consumed_after_one_apply(self) -> None:
        proposal = {"changes": [{"kind": "create", "path": "once.txt", "content": "once"}]}
        prepared = self.executor.prepare(proposal)

        first = prepared.apply(proposal)
        second = prepared.apply(proposal)

        self.assertEqual(first["status"], "OK")
        self.assertEqual(second, {
            "status": "DENIED",
            "code": DENIED_WORKSPACE_PROPOSAL_ALREADY_APPLIED,
            "partial_apply": False,
            "mismatches": [{"path": "", "reason": "prepared_proposal_already_applied"}],
        })
        self.assertEqual((self.root / "once.txt").read_text(encoding="utf-8"), "once")

    def test_concurrent_apply_allows_exactly_one_attempt(self) -> None:
        proposal = {"changes": [{"kind": "create", "path": "winner.txt", "content": "winner"}]}
        prepared = self.executor.prepare(proposal)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: prepared.apply(proposal), range(2)))

        self.assertEqual(sum(result["status"] == "OK" for result in results), 1)
        denied = [result for result in results if result["status"] == "DENIED"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["code"], DENIED_WORKSPACE_PROPOSAL_ALREADY_APPLIED)
        self.assertFalse(denied[0]["partial_apply"])
        self.assertEqual((self.root / "winner.txt").read_text(encoding="utf-8"), "winner")

    def test_initial_recovery_marker_failure_denies_before_staging_or_mutation(self) -> None:
        for behavior in ("false", "raise"):
            with self.subTest(behavior=behavior):
                root = self.root / behavior
                root.mkdir()

                def persist(_state, selected=behavior):
                    if selected == "raise":
                        raise OSError("simulated recovery store failure")
                    return False

                executor = WorkspaceProposalExecutor(root, recovery_callback=persist)
                self.executors.append(executor)
                proposal = {"changes": [{"kind": "create", "path": "never.txt", "content": "never"}]}
                result = executor.prepare(proposal).apply(proposal)

                self.assertEqual(result["code"], DENIED_WORKSPACE_RECOVERY_PERSIST_FAILED)
                self.assertFalse(result["partial_apply"])
                self.assertEqual(result["mismatches"], [{"path": "", "reason": "recovery_state_persist_failed"}])
                self.assertFalse((root / "never.txt").exists())
                self.assertEqual(list(root.rglob("*.kaizen-*.tmp")), [])

    def test_post_mutation_recovery_clear_exception_retains_recovery_required(self) -> None:
        states = []

        def persist(state):
            states.append(state)
            if state is None:
                raise OSError("simulated recovery clear failure")
            return True

        executor = WorkspaceProposalExecutor(self.root, recovery_callback=persist)
        self.executors.append(executor)
        proposal = {"changes": [{"kind": "create", "path": "done-raise.txt", "content": "done"}]}

        result = executor.prepare(proposal).apply(proposal)

        self.assertEqual(result["code"], DENIED_WORKSPACE_RECOVERY_REQUIRED)
        self.assertFalse(result["partial_apply"])
        self.assertEqual(result["mismatches"], [])
        self.assertEqual((self.root / "done-raise.txt").read_text(encoding="utf-8"), "done")
        self.assertEqual([state and state["phase"] for state in states], ["staging", "mutating", None])

    def test_existing_create_target_is_rejected_during_prepare(self) -> None:
        (self.root / "exists.txt").write_text("existing", encoding="utf-8")
        with self.assertRaises(ProposalExecutionError) as raised:
            self.executor.prepare({
                "changes": [{"kind": "create", "path": "exists.txt", "content": "replace"}],
            })
        self.assertEqual(raised.exception.code, DENIED_APPROVAL_STALE_RERUN_REQUIRED)
        self.assertEqual((self.root / "exists.txt").read_text(encoding="utf-8"), "existing")

    def test_second_promotion_failure_retains_unused_stage_and_exact_partial_truth(self) -> None:
        proposal = {"changes": [
            {"kind": "create", "path": "one.txt", "content": "one"},
            {"kind": "create", "path": "two.txt", "content": "two"},
        ]}
        prepared = self.executor.prepare(proposal)
        original_promote = self.executor._promote_staged
        calls = 0

        def fail_second(item):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated second-operation failure")
            return original_promote(item)

        with mock.patch.object(self.executor, "_promote_staged", side_effect=fail_second):
            result = prepared.apply(proposal)

        self.assertEqual(result["status"], "DENIED")
        self.assertTrue(result["partial_apply"])
        self.assertEqual((self.root / "one.txt").read_text(encoding="utf-8"), "one")
        self.assertFalse((self.root / "two.txt").exists())
        self.assertEqual(result["mismatches"], [{
            "path": "two.txt", "reason": "final_state_mismatch",
            "expected_exists": True, "actual_exists": False,
            "expected_sha256": hashlib.sha256(b"two").hexdigest(), "actual_sha256": None,
        }])
        leftovers = list(self.root.rglob("*.kaizen-*.tmp"))
        self.assertEqual(len(leftovers), 1)
        self.assertEqual(self.recovery_states[-1]["phase"], "mutating")

    def test_first_promotion_failure_retains_all_stages_without_claiming_partial_apply(self) -> None:
        proposal = {"changes": [
            {"kind": "create", "path": "one.txt", "content": "one"},
            {"kind": "create", "path": "two.txt", "content": "two"},
        ]}
        prepared = self.executor.prepare(proposal)

        with mock.patch.object(
            self.executor, "_promote_staged", side_effect=OSError("simulated first-operation failure"),
        ):
            result = prepared.apply(proposal)

        self.assertEqual(result["code"], DENIED_WORKSPACE_RECOVERY_REQUIRED)
        self.assertFalse(result["partial_apply"])
        self.assertFalse((self.root / "one.txt").exists())
        self.assertFalse((self.root / "two.txt").exists())
        self.assertEqual(
            [item["path"] for item in result["mismatches"]], ["one.txt", "two.txt"],
        )
        self.assertTrue(all(
            item["reason"] == "final_state_mismatch" for item in result["mismatches"]
        ))
        self.assertEqual(len(list(self.root.rglob("*.kaizen-*.tmp"))), 2)
        self.assertEqual(self.recovery_states[-1]["phase"], "mutating")

    @unittest.skipUnless(os.name == "nt", "exact removal/rename lane is Windows-only")
    def test_modify_failure_after_exact_unlink_retains_base_backup_and_final_stage(self) -> None:
        target = self.root / "modify.txt"
        target.write_text("base", encoding="utf-8")
        proposal = {"changes": [{"kind": "modify", "path": "modify.txt", "content": "final"}]}
        prepared = self.executor.prepare(proposal)

        with mock.patch.object(
            self.executor, "_promote_staged", side_effect=OSError("crash after exact unlink"),
        ):
            result = prepared.apply(proposal)

        self.assertEqual(result["code"], DENIED_WORKSPACE_RECOVERY_REQUIRED)
        self.assertTrue(result["partial_apply"])
        self.assertFalse(target.exists())
        leftovers = list(self.root.rglob("*.kaizen-*.tmp"))
        self.assertEqual(len(leftovers), 2)
        self.assertEqual(sorted(path.read_text(encoding="utf-8") for path in leftovers), ["base", "final"])
        self.assertEqual(self.recovery_states[-1]["phase"], "mutating")

    @unittest.skipUnless(os.name == "nt", "exact removal/rename lane is Windows-only")
    def test_rename_failure_after_destination_promotion_retains_source_backup(self) -> None:
        source = self.root / "old.txt"
        destination = self.root / "new.txt"
        source.write_text("base", encoding="utf-8")
        proposal = {"changes": [{"kind": "rename", "old_path": "old.txt", "path": "new.txt"}]}
        prepared = self.executor.prepare(proposal)
        original_unlink = self.executor._unlink_baseline

        def fail_source(path, baseline):
            if path == source:
                raise OSError("crash before source unlink")
            return original_unlink(path, baseline)

        with mock.patch.object(self.executor, "_unlink_baseline", side_effect=fail_source):
            result = prepared.apply(proposal)

        self.assertEqual(result["code"], DENIED_WORKSPACE_RECOVERY_REQUIRED)
        self.assertTrue(result["partial_apply"])
        self.assertEqual(source.read_text(encoding="utf-8"), "base")
        self.assertEqual(destination.read_text(encoding="utf-8"), "base")
        leftovers = list(self.root.rglob("*.kaizen-*.tmp"))
        self.assertEqual(len(leftovers), 1)
        self.assertEqual(leftovers[0].read_text(encoding="utf-8"), "base")

    @unittest.skipUnless(os.name == "nt", "exact removal/rename lane is Windows-only")
    def test_backup_cleanup_failure_retains_mutating_journal_after_exact_final(self) -> None:
        target = self.root / "modify.txt"
        target.write_text("base", encoding="utf-8")
        proposal = {"changes": [{"kind": "modify", "path": "modify.txt", "content": "final"}]}
        prepared = self.executor.prepare(proposal)
        original_cleanup = self.executor._cleanup_staged

        def fail_backup(item):
            return False if not item.promote else original_cleanup(item)

        with mock.patch.object(self.executor, "_cleanup_staged", side_effect=fail_backup):
            result = prepared.apply(proposal)

        self.assertEqual(result["code"], DENIED_WORKSPACE_RECOVERY_REQUIRED)
        self.assertFalse(result["partial_apply"])
        self.assertEqual(target.read_text(encoding="utf-8"), "final")
        leftovers = list(self.root.rglob("*.kaizen-*.tmp"))
        self.assertEqual(len(leftovers), 1)
        self.assertEqual(leftovers[0].read_text(encoding="utf-8"), "base")
        self.assertEqual(self.recovery_states[-1]["phase"], "mutating")

    def test_exact_all_final_with_journal_clear_failure_is_not_partial_or_uncertain(self) -> None:
        states = []

        def persist(state):
            states.append(state)
            return state is not None

        executor = WorkspaceProposalExecutor(self.root, recovery_callback=persist)
        self.executors.append(executor)
        proposal = {"changes": [{"kind": "create", "path": "done.txt", "content": "done"}]}

        result = executor.prepare(proposal).apply(proposal)

        self.assertEqual(result["code"], DENIED_WORKSPACE_RECOVERY_REQUIRED)
        self.assertFalse(result["partial_apply"])
        self.assertEqual(result["mismatches"], [])
        self.assertEqual((self.root / "done.txt").read_text(encoding="utf-8"), "done")
        self.assertEqual([state and state["phase"] for state in states], ["staging", "mutating", None])

    def test_directory_collision_is_unreadable_not_fabricated_missing(self) -> None:
        proposal = {"changes": [{"kind": "create", "path": "new.txt", "content": "approved"}]}
        prepared = self.executor.prepare(proposal)
        (self.root / "new.txt").mkdir()

        result = prepared.apply(proposal)

        self.assertFalse(result["partial_apply"])
        self.assertEqual(result["mismatches"], [{
            "path": "new.txt", "reason": "target_state_unreadable",
        }])

    def test_post_apply_hash_read_failure_is_explicitly_unreadable(self) -> None:
        proposal = {"changes": [{"kind": "create", "path": "new.txt", "content": "approved"}]}
        prepared = self.executor.prepare(proposal)
        original_promote = self.executor._promote_staged
        original_read = self.executor._authority.read
        mutation_complete = False

        def promote(item):
            nonlocal mutation_complete
            original_promote(item)
            mutation_complete = True

        def fail_target_read(relative, maximum):
            if mutation_complete and str(relative) == "new.txt":
                raise WorkspacePathError("simulated final hash read failure")
            return original_read(relative, maximum)

        with mock.patch.object(self.executor, "_promote_staged", side_effect=promote), \
                mock.patch.object(self.executor._authority, "read", side_effect=fail_target_read):
            result = prepared.apply(proposal)

        self.assertTrue(result["partial_apply"])
        self.assertEqual(result["mismatches"], [{
            "path": "new.txt", "reason": "target_state_unreadable",
        }])

    def test_post_apply_reparse_detection_is_explicitly_unreadable(self) -> None:
        proposal = {"changes": [{"kind": "create", "path": "new.txt", "content": "approved"}]}
        prepared = self.executor.prepare(proposal)
        original_promote = self.executor._promote_staged
        original_identity = self.executor._authority.identity
        mutation_complete = False

        def promote(item):
            nonlocal mutation_complete
            original_promote(item)
            mutation_complete = True

        def redirected(relative):
            if mutation_complete and str(relative) == "new.txt":
                raise WorkspacePathError("simulated redirected final target")
            return original_identity(relative)

        with mock.patch.object(self.executor, "_promote_staged", side_effect=promote), \
                mock.patch.object(self.executor._authority, "identity", side_effect=redirected):
            result = prepared.apply(proposal)

        self.assertTrue(result["partial_apply"])
        self.assertEqual(result["mismatches"], [{
            "path": "new.txt", "reason": "target_state_unreadable",
        }])

    def test_final_audit_failure_returns_structured_uncertainty_instead_of_rethrowing(self) -> None:
        proposal = {"changes": [{"kind": "create", "path": "new.txt", "content": "approved"}]}
        prepared = self.executor.prepare(proposal)
        with mock.patch.object(self.executor, "_write_staged", side_effect=OSError("stage failure")), \
                mock.patch.object(self.executor, "_final_mismatches", side_effect=OSError("audit failure")):
            result = prepared.apply(proposal)

        self.assertEqual(result["status"], "DENIED")
        self.assertFalse(result["partial_apply"])
        self.assertEqual(result["mismatches"], [{
            "path": "", "reason": "apply_outcome_uncertain",
        }])

    def test_exclusive_stage_failure_clears_staging_journal_with_zero_target_change(self) -> None:
        proposal = {"changes": [{"kind": "create", "path": "new.txt", "content": "approved"}]}
        prepared = self.executor.prepare(proposal)

        with mock.patch.object(
            self.executor._authority,
            "create_exact_exclusive",
            side_effect=WorkspacePathError("simulated durable stage failure"),
        ):
            result = prepared.apply(proposal)

        self.assertEqual(result["code"], DENIED_APPROVAL_STALE_RERUN_REQUIRED)
        self.assertFalse(result["partial_apply"])
        self.assertEqual(result["mismatches"][0], {
            "path": "", "reason": "apply_outcome_uncertain",
        })
        self.assert_no_staged_files()

    def test_unproven_self_cleanup_requires_workspace_recovery(self) -> None:
        proposal = {"changes": [{"kind": "create", "path": "new.txt", "content": "approved"}]}
        prepared = self.executor.prepare(proposal)
        original_create = self.executor._authority.create_exact_exclusive

        def create_then_fail(relative, data, *, max_bytes):
            original_create(relative, data, max_bytes=max_bytes)
            raise WorkspacePathError("simulated post-create durability failure")

        with mock.patch.object(
            self.executor._authority, "create_exact_exclusive", side_effect=create_then_fail,
        ), mock.patch.object(self.executor, "_cleanup_staged", return_value=False):
            result = prepared.apply(proposal)

        leftovers = list(self.root.rglob("*.kaizen-*.tmp"))
        self.assertEqual(result["code"], DENIED_WORKSPACE_RECOVERY_REQUIRED)
        self.assertFalse(result["partial_apply"])
        self.assertEqual(len(leftovers), 1)
        self.assertEqual(result["mismatches"][0], {
            "reason": "staged_cleanup_unproven",
            "path": leftovers[0].relative_to(self.root).as_posix(),
        })
        self.assertEqual(result["mismatches"][1]["reason"], "final_state_mismatch")
        self.assertEqual(result["mismatches"][1]["path"], "new.txt")


if __name__ == "__main__":
    unittest.main()
