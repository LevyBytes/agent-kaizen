"""Supervisor core (v8 M1): crash/restart truthful finalize, single-instance, loopback
auth, cp1252 --json regression, and env composition.

Every test drives the real CLI (``kaizen.py``) as a subprocess against an isolated
data plane (the repo idiom), except the pure loopback-auth test which exercises the
in-process server directly. The undocumented ``daemon run --exit-after-boot`` seam runs
boot + orphan-sweep + exit so a CLI test never has to background a live daemon.
"""

from __future__ import annotations

import argparse
import json
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from _harness import REPO_ROOT, kaizen, run

sys.path.insert(0, str(REPO_ROOT))

from kaizen_components.orchestration import loopback  # noqa: E402
from kaizen_components.orchestration import supervisor as supervisor_module  # noqa: E402
from kaizen_components.orchestration.writer_lease import (  # noqa: E402
    OwnershipRegistry,
    OwnershipStateError,
    WRITER_MARKER_KEY,
    WorkspaceWriterLease,
)
from test_session_drive import _DrivenSubprocess  # noqa: E402  -- driven-subprocess harness (imported, not modified)
from kaizen_components.orchestration.workspace_path_authority import (  # noqa: E402
    WorkspacePathAuthority,
    WorkspacePathError,
)
from kaizen_components.orchestration.proposal_executor import WorkspaceProposalExecutor  # noqa: E402


def _runtime_dir(root: Path) -> Path:
    """Return and create the orchestration runtime directory beneath the supplied workspace."""
    d = root / "AI" / "work" / "orchestration" / "runtime"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _open_run(root: Path, summary: str = "supervisor test run") -> str:
    """Open a T5 fixture run through the CLI and return its asserted-success identifier."""
    rc, payload = kaizen(root, "T5", "--payload-json", '{"agent_type":"other","surface":"cli"}',
                         "--summary", summary)
    assert rc == 0, payload
    return payload["id"]


class OwnershipRegistryAtomicityTest(unittest.TestCase):
    """Atomic registry RMW, writer-claim lifecycle, and exact crash-recovery invariants."""
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-p0-owner-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.path = _runtime_dir(self.root) / "owned_runs.json"
        self.registry = OwnershipRegistry(self.path, workspace_root=self.root)
        self.addCleanup(self.registry.close)

    def test_concurrent_rmw_preserves_unrelated_entries_and_valid_json(self) -> None:
        barrier = threading.Barrier(17)
        errors: list[BaseException] = []

        def record(index: int) -> None:
            try:
                barrier.wait()
                self.registry.record_run(f"ar_{index:02d}", pid=1000 + index, nonce=f"n{index}")
            except BaseException as error:  # noqa: BLE001 -- thread failures must reach the assertion
                errors.append(error)

        threads = [threading.Thread(target=record, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(set(self.registry.runs()), {f"ar_{index:02d}" for index in range(16)})
        self.assertIsInstance(json.loads(self.path.read_text(encoding="utf-8")), dict)

        clears = [threading.Thread(target=self.registry.clear_run, args=(f"ar_{index:02d}",))
                  for index in range(0, 16, 2)]
        for thread in clears:
            thread.start()
        for thread in clears:
            thread.join()
        self.assertEqual(set(self.registry.runs()), {f"ar_{index:02d}" for index in range(1, 16, 2)})

    def test_malformed_registry_is_refused_without_overwrite(self) -> None:
        original = "{malformed-json\n"
        self.path.write_text(original, encoding="utf-8")
        with self.assertRaises(OwnershipStateError):
            self.registry.record_run("ar_new", pid=1, nonce="n")
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_dead_writer_marker_recovers_but_live_marker_stays_closed(self) -> None:
        first = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, denial = first.acquire("ask")
        self.assertIsNone(denial)
        self.assertIsNotNone(claim)
        self.assertIn(WRITER_MARKER_KEY, self.registry.load())

        restarted = WorkspaceWriterLease(
            self.registry, pid=222, nonce="second", clock=lambda: "t2", workspace_root=self.root,
        )
        self.assertFalse(restarted.reconcile_after_orphan_sweep(lambda pid: pid == 111))
        self.assertTrue(restarted.recovery_required)
        self.assertEqual(restarted.acquire("ask")[1]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")

        self.assertTrue(restarted.reconcile_after_orphan_sweep(lambda _pid: False))
        self.assertNotIn(WRITER_MARKER_KEY, self.registry.load())
        recovered, denial = restarted.acquire("ask")
        self.assertIsNone(denial)
        self.assertIsNotNone(recovered)
        self.assertTrue(restarted.release(recovered.claim_id))

    def test_pending_spawn_never_auto_clears_after_owner_death(self) -> None:
        first = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = first.acquire("ask")
        self.assertIsNotNone(claim)
        first.begin_spawn(claim.claim_id)

        restarted = WorkspaceWriterLease(
            self.registry, pid=222, nonce="second", clock=lambda: "t2", workspace_root=self.root,
        )
        self.assertFalse(restarted.reconcile_after_orphan_sweep(lambda _pid: False))
        self.assertTrue(restarted.recovery_required)
        marker = self.registry.writer_marker()
        self.assertIsNotNone(marker)
        self.assertTrue(marker["spawn_pending"])

    def test_tracked_child_must_be_proven_dead_before_restart_clears(self) -> None:
        first = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = first.acquire("ask")
        self.assertIsNotNone(claim)
        first.begin_spawn(claim.claim_id)
        first.track_spawned_child(claim.claim_id, 333)

        restarted = WorkspaceWriterLease(
            self.registry, pid=222, nonce="second", clock=lambda: "t2", workspace_root=self.root,
        )
        self.assertFalse(restarted.reconcile_after_orphan_sweep(lambda pid: pid == 333))
        self.assertTrue(restarted.recovery_required)
        self.assertTrue(restarted.reconcile_after_orphan_sweep(lambda _pid: False))
        self.assertIsNone(self.registry.writer_marker())

    @staticmethod
    def _apply_state(pid: int, *, phase: str = "staging", staged: bool = True) -> dict:
        """Build the canonical staged-target recovery journal used by writer-lease reconciliation tests."""
        content = b"final"
        return {
            "phase": phase,
            "staged": [{
                "path": f".target.txt.kaizen-{pid}-0-{'a' * 24}.tmp",
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }] if staged else [],
            "targets": [{
                "path": "target.txt",
                "base_exists": False,
                "base_sha256": None,
                "final_exists": True,
                "final_sha256": hashlib.sha256(content).hexdigest(),
            }],
        }

    def test_apply_journal_accepts_only_monotonic_immutable_phase_graph(self) -> None:
        lease = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = lease.acquire("plan")
        self.assertIsNotNone(claim)
        staging = self._apply_state(111)
        mutating = {**staging, "phase": "mutating"}
        self.assertTrue(lease.set_apply_recovery(claim.claim_id, staging))
        self.assertTrue(lease.set_apply_recovery(claim.claim_id, staging))
        self.assertTrue(lease.set_apply_recovery(claim.claim_id, mutating))
        self.assertTrue(lease.set_apply_recovery(claim.claim_id, mutating))
        (self.root / "target.txt").write_bytes(b"final")
        self.assertTrue(lease.set_apply_recovery(claim.claim_id, None))
        self.assertTrue(lease.release(claim.claim_id))

    def test_deletion_only_journal_allows_zero_staged_in_both_phases(self) -> None:
        lease = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = lease.acquire("plan")
        staging = self._apply_state(111, staged=False)
        self.assertTrue(lease.set_apply_recovery(claim.claim_id, staging))
        self.assertTrue(lease.set_apply_recovery(claim.claim_id, {**staging, "phase": "mutating"}))
        (self.root / "target.txt").write_bytes(b"final")
        self.assertTrue(lease.set_apply_recovery(claim.claim_id, None))

    def test_invalid_transition_retains_last_journal_and_durably_refuses_release(self) -> None:
        lease = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = lease.acquire("plan")
        staging = self._apply_state(111)
        self.assertTrue(lease.set_apply_recovery(claim.claim_id, staging))
        drifted = json.loads(json.dumps(staging))
        drifted["targets"][0]["final_sha256"] = "b" * 64
        self.assertFalse(lease.set_apply_recovery(claim.claim_id, drifted))
        marker = self.registry.writer_marker()
        self.assertEqual(marker["apply_recovery"], staging)
        self.assertTrue(marker["recovery_required"])
        self.assertFalse(lease.release(claim.claim_id))
        self.assertIsNotNone(self.registry.writer_marker())

    def test_journal_clear_requires_phase_exact_targets_and_absent_staging(self) -> None:
        for name, phase, create_stage, create_target in (
            ("staging-temp-present", "staging", True, False),
            ("mutating-target-still-base", "mutating", False, False),
        ):
            with self.subTest(name=name):
                root = self.root / name
                root.mkdir()
                registry = OwnershipRegistry(
                    _runtime_dir(root) / "owned_runs.json", workspace_root=root,
                )
                lease = WorkspaceWriterLease(
                    registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=root,
                )
                claim, _ = lease.acquire("plan")
                staging = self._apply_state(111)
                self.assertTrue(lease.set_apply_recovery(claim.claim_id, staging))
                state = staging
                if phase == "mutating":
                    state = {**staging, "phase": "mutating"}
                    self.assertTrue(lease.set_apply_recovery(claim.claim_id, state))
                if create_stage:
                    (root / state["staged"][0]["path"]).write_bytes(b"final")
                if create_target:
                    (root / "target.txt").write_bytes(b"final")
                self.assertFalse(lease.set_apply_recovery(claim.claim_id, None))
                self.assertEqual(registry.writer_marker()["apply_recovery"], state)
                self.assertTrue(registry.writer_marker()["recovery_required"])

    def test_direct_mutating_and_wrong_temp_pid_are_rejected_before_filesystem_action(self) -> None:
        for name, state in (
            ("direct-mutating", self._apply_state(111, phase="mutating")),
            ("wrong-pid", self._apply_state(222)),
        ):
            with self.subTest(name=name):
                root = self.root / name
                root.mkdir()
                registry = OwnershipRegistry(
                    _runtime_dir(root) / "owned_runs.json", workspace_root=root,
                )
                lease = WorkspaceWriterLease(
                    registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=root,
                )
                claim, _ = lease.acquire("plan")
                self.assertFalse(lease.set_apply_recovery(claim.claim_id, state))
                self.assertTrue(lease.recovery_required)

    def test_stale_apply_callback_does_not_poison_a_later_claim(self) -> None:
        lease = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        old, _ = lease.acquire("plan")
        self.assertTrue(lease.release(old.claim_id))
        current, _ = lease.acquire("plan")
        self.assertFalse(lease.set_apply_recovery(old.claim_id, self._apply_state(111)))
        self.assertFalse(lease.recovery_required)
        self.assertEqual(self.registry.writer_marker()["claim_id"], current.claim_id)
        self.assertTrue(lease.release(current.claim_id))

    def test_dead_staging_journal_cleans_only_exact_temp_then_clears(self) -> None:
        first = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = first.acquire("plan")
        state = self._apply_state(111)
        self.assertTrue(first.set_apply_recovery(claim.claim_id, state))
        staged = self.root / state["staged"][0]["path"]
        staged.write_bytes(b"final")
        unrelated = self.root / f".target.txt.kaizen-111-9-{'b' * 24}.tmp"
        unrelated.write_bytes(b"final")

        restarted = WorkspaceWriterLease(
            self.registry, pid=222, nonce="second", clock=lambda: "t2", workspace_root=self.root,
        )
        self.assertTrue(restarted.reconcile_after_orphan_sweep(lambda _pid: False))
        self.assertFalse(staged.exists())
        self.assertEqual(unrelated.read_bytes(), b"final")
        self.assertIsNone(self.registry.writer_marker())

    def test_dead_mutating_journal_clears_when_all_targets_are_exact_final(self) -> None:
        first = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = first.acquire("plan")
        staging = self._apply_state(111)
        self.assertTrue(first.set_apply_recovery(claim.claim_id, staging))
        self.assertTrue(first.set_apply_recovery(claim.claim_id, {**staging, "phase": "mutating"}))
        (self.root / "target.txt").write_bytes(b"final")

        restarted = WorkspaceWriterLease(
            self.registry, pid=222, nonce="second", clock=lambda: "t2", workspace_root=self.root,
        )
        self.assertTrue(restarted.reconcile_after_orphan_sweep(lambda _pid: False))
        self.assertEqual((self.root / "target.txt").read_bytes(), b"final")
        self.assertIsNone(self.registry.writer_marker())

    def test_dead_journal_mismatch_stays_closed_and_untouched(self) -> None:
        first = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = first.acquire("plan")
        state = self._apply_state(111)
        self.assertTrue(first.set_apply_recovery(claim.claim_id, state))
        staged = self.root / state["staged"][0]["path"]
        staged.write_bytes(b"wrong")

        restarted = WorkspaceWriterLease(
            self.registry, pid=222, nonce="second", clock=lambda: "t2", workspace_root=self.root,
        )
        self.assertFalse(restarted.reconcile_after_orphan_sweep(lambda _pid: False))
        self.assertEqual(staged.read_bytes(), b"wrong")
        self.assertIsNotNone(self.registry.writer_marker())

    def test_apply_journal_survives_promotion_and_spawn_tracking(self) -> None:
        lease = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = lease.acquire("ask")
        state = self._apply_state(111)
        self.assertTrue(lease.set_apply_recovery(claim.claim_id, state))
        lease.promote(claim.claim_id, session_id="session-1", agent_run_id="run-1")
        lease.begin_spawn(claim.claim_id)
        lease.track_spawned_child(claim.claim_id, 333)
        marker = self.registry.writer_marker()
        self.assertEqual(marker["apply_recovery"], state)
        self.assertEqual(marker["session_id"], "session-1")
        self.assertEqual(marker["child_pids"], [333])

    def test_dead_mutating_all_base_and_missing_stage_clear_without_target_touch(self) -> None:
        first = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = first.acquire("plan")
        staging = self._apply_state(111)
        self.assertTrue(first.set_apply_recovery(claim.claim_id, staging))
        self.assertTrue(first.set_apply_recovery(claim.claim_id, {**staging, "phase": "mutating"}))

        restarted = WorkspaceWriterLease(
            self.registry, pid=222, nonce="second", clock=lambda: "t2", workspace_root=self.root,
        )
        self.assertTrue(restarted.reconcile_after_orphan_sweep(lambda _pid: False))
        self.assertFalse((self.root / "target.txt").exists())
        self.assertIsNone(self.registry.writer_marker())

    def test_dead_mixed_base_final_targets_retain_marker_and_all_files(self) -> None:
        final_hash = hashlib.sha256(b"final").hexdigest()
        state = {
            "phase": "staging", "staged": [],
            "targets": [
                {"path": name, "base_exists": False, "base_sha256": None,
                 "final_exists": True, "final_sha256": final_hash}
                for name in ("one.txt", "two.txt")
            ],
        }
        first = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = first.acquire("plan")
        self.assertTrue(first.set_apply_recovery(claim.claim_id, state))
        self.assertTrue(first.set_apply_recovery(claim.claim_id, {**state, "phase": "mutating"}))
        (self.root / "one.txt").write_bytes(b"final")

        restarted = WorkspaceWriterLease(
            self.registry, pid=222, nonce="second", clock=lambda: "t2", workspace_root=self.root,
        )
        self.assertFalse(restarted.reconcile_after_orphan_sweep(lambda _pid: False))
        self.assertEqual((self.root / "one.txt").read_bytes(), b"final")
        self.assertFalse((self.root / "two.txt").exists())
        self.assertIsNotNone(self.registry.writer_marker())

    def test_malformed_dead_apply_journal_is_never_cleared(self) -> None:
        first = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = first.acquire("plan")
        marker = self.registry.writer_marker()
        marker["apply_recovery"] = {"phase": "staging", "staged": [], "targets": []}
        self.registry.put_writer_marker(marker)

        restarted = WorkspaceWriterLease(
            self.registry, pid=222, nonce="second", clock=lambda: "t2", workspace_root=self.root,
        )
        self.assertFalse(restarted.reconcile_after_orphan_sweep(lambda _pid: False))
        self.assertEqual(self.registry.writer_marker()["claim_id"], claim.claim_id)

    def test_apply_journal_with_unrelated_cleanup_paths_is_not_auto_reconciled(self) -> None:
        first = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = first.acquire("plan")
        state = self._apply_state(111)
        self.assertTrue(first.set_apply_recovery(claim.claim_id, state))
        staged = self.root / state["staged"][0]["path"]
        staged.write_bytes(b"final")
        self.assertTrue(first.require_recovery("unrelated uncertainty", cleanup_paths=("other.tmp",)))

        restarted = WorkspaceWriterLease(
            self.registry, pid=222, nonce="second", clock=lambda: "t2", workspace_root=self.root,
        )
        self.assertFalse(restarted.reconcile_after_orphan_sweep(lambda _pid: False))
        self.assertEqual(staged.read_bytes(), b"final")
        self.assertIsNotNone(self.registry.writer_marker())

    def test_apply_journal_accepts_exact_staged_cleanup_path_subset_on_restart(self) -> None:
        first = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = first.acquire("plan")
        state = self._apply_state(111)
        self.assertTrue(first.set_apply_recovery(claim.claim_id, state))
        staged_path = state["staged"][0]["path"]
        staged = self.root / staged_path
        staged.write_bytes(b"final")
        self.assertTrue(first.require_recovery(
            "staged workspace mutation cleanup could not be proven",
            cleanup_paths=(staged_path,),
        ))

        restarted = WorkspaceWriterLease(
            self.registry, pid=222, nonce="second", clock=lambda: "t2", workspace_root=self.root,
        )
        self.assertTrue(restarted.reconcile_after_orphan_sweep(lambda _pid: False))
        self.assertFalse(staged.exists())
        self.assertIsNone(self.registry.writer_marker())

    def test_exact_sixty_four_mib_staged_journal_fits_private_bounds(self) -> None:
        lease = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = lease.acquire("plan")
        parent = "/".join(("😀" * 240, "😁" * 240, "😂" * 240))
        targets = []
        staged = []
        digest = "a" * 64
        for index in range(128):
            name = f"{'😃' * 180}-{index}.txt"
            target = f"{parent}/{name}"
            targets.append({
                "path": target, "base_exists": False, "base_sha256": None,
                "final_exists": True, "final_sha256": digest,
            })
            if index < 8:
                staged.append({
                    "path": f"{parent}/.{name}.kaizen-111-{index}-{'b' * 24}.tmp",
                    "sha256": digest, "bytes": 8 * 1024 * 1024,
                })
        state = {"phase": "staging", "staged": staged, "targets": targets}
        encoded = json.dumps(state, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), 4 * 1024 * 1024)
        self.assertTrue(lease.set_apply_recovery(claim.claim_id, state))
        self.assertLessEqual(self.path.stat().st_size, 8 * 1024 * 1024)

    def test_one_hundred_twenty_eight_staged_descriptors_over_aggregate_are_refused(self) -> None:
        lease = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = lease.acquire("plan")
        digest = "a" * 64
        targets = []
        staged = []
        for index in range(128):
            target = f"target-{index}.txt"
            targets.append({
                "path": target, "base_exists": False, "base_sha256": None,
                "final_exists": True, "final_sha256": digest,
            })
            staged.append({
                "path": f".target-{index}.txt.kaizen-111-{index}-{'b' * 24}.tmp",
                "sha256": digest, "bytes": 524_289,
            })
        self.assertFalse(lease.set_apply_recovery(claim.claim_id, {
            "phase": "staging", "staged": staged, "targets": targets,
        }))
        self.assertTrue(lease.recovery_required)

    def test_target_recovery_reads_stop_at_aggregate_bound(self) -> None:
        targets = []
        for name in ("one.txt", "two.txt"):
            payload = b"12345"
            (self.root / name).write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            targets.append({
                "path": name, "base_exists": True, "base_sha256": digest,
                "final_exists": True, "final_sha256": digest,
            })
        first = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = first.acquire("plan")
        self.assertTrue(first.set_apply_recovery(claim.claim_id, {
            "phase": "staging", "staged": [], "targets": targets,
        }))
        restarted = WorkspaceWriterLease(
            self.registry, pid=222, nonce="second", clock=lambda: "t2", workspace_root=self.root,
        )
        with mock.patch(
            "kaizen_components.orchestration.writer_lease.MAX_APPLY_STAGED_TOTAL_BYTES", 8,
        ):
            self.assertFalse(restarted.reconcile_after_orphan_sweep(lambda _pid: False))
        self.assertIsNotNone(self.registry.writer_marker())

    def test_target_descriptor_cannot_be_absent_in_both_base_and_final(self) -> None:
        lease = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = lease.acquire("plan")
        state = self._apply_state(111, staged=False)
        state["targets"][0].update({
            "base_exists": False, "base_sha256": None,
            "final_exists": False, "final_sha256": None,
        })
        self.assertFalse(lease.set_apply_recovery(claim.claim_id, state))
        self.assertTrue(lease.recovery_required)

    def test_missing_stage_that_reappears_before_marker_clear_stays_closed(self) -> None:
        first = WorkspaceWriterLease(
            self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        claim, _ = first.acquire("plan")
        state = self._apply_state(111)
        self.assertTrue(first.set_apply_recovery(claim.claim_id, state))
        restarted = WorkspaceWriterLease(
            self.registry, pid=222, nonce="second", clock=lambda: "t2", workspace_root=self.root,
        )
        staged_path = state["staged"][0]["path"]
        original = restarted._recovery_file_state
        staged_reads = 0

        def reappear(raw_path: str, *, max_bytes: int = 8 * 1024 * 1024):
            nonlocal staged_reads
            if raw_path == staged_path:
                staged_reads += 1
                if staged_reads == 2:
                    (self.root / staged_path).write_bytes(b"final")
            return original(raw_path, max_bytes=max_bytes)

        with mock.patch.object(restarted, "_recovery_file_state", side_effect=reappear):
            self.assertFalse(restarted.reconcile_after_orphan_sweep(lambda _pid: False))
        self.assertEqual((self.root / staged_path).read_bytes(), b"final")
        self.assertIsNotNone(self.registry.writer_marker())

    def test_oversize_registry_candidate_is_refused_without_overwrite(self) -> None:
        self.registry.record_run("small", pid=1, nonce="n")
        before = self.path.read_bytes()
        with mock.patch(
            "kaizen_components.orchestration.writer_lease.MAX_OWNERSHIP_REGISTRY_BYTES", 512,
        ):
            with self.assertRaises(OwnershipStateError):
                self.registry.record_run("large", pid=2, nonce="x" * 1_024)
        self.assertEqual(self.path.read_bytes(), before)

    def test_writer_lease_refuses_a_workspace_different_from_registry_authority(self) -> None:
        other = self.root / "other"
        other.mkdir()
        with self.assertRaises(OwnershipStateError):
            WorkspaceWriterLease(
                self.registry, pid=111, nonce="first", clock=lambda: "t1", workspace_root=other,
            )

    def test_plan_first_write_acquires_claim_before_recovery_factory_snapshot(self) -> None:
        from kaizen_components.orchestration import policy
        from kaizen_components.orchestration.adapters import BrokerApprovalResult
        from kaizen_components.orchestration.session_drive import DrivenSession, WriterClaimBinding
        from kaizen_components.orchestration.supervisor import Supervisor
        from kaizen_components.orchestration.tool_gateway import PROPOSE_CHANGES, ToolContext, ToolGateway
        from kaizen_components.orchestration.proposal_executor import WorkspaceProposalExecutor

        lease = WorkspaceWriterLease(
            self.registry, pid=os.getpid(), nonce="first", clock=lambda: "t1", workspace_root=self.root,
        )
        supervisor = Supervisor.__new__(Supervisor)
        supervisor._writer_lease = lease
        binding = WriterClaimBinding()
        adapter = object()
        session = DrivenSession(
            "session-plan", "run-plan", adapter, "local_llm", 30.0,
            permission_mode="plan", writer_claim_binding=binding,
        )
        snapshot = policy.build_policy_snapshot(
            "local_llm", "plan", str(self.root), [str(self.root / "plan.txt")], [],
            protected_paths=[], vendor_config_paths=[],
        )
        gateway = ToolGateway(
            self.root,
            decide=lambda _action, _epoch: policy.Decision(
                result=policy.ASK, reason="exercise Plan first-write lease", dedupe_key="plan-write",
            ),
            approval_broker=lambda request: BrokerApprovalResult(
                "approved", updated_input=request["diff_request"]["updated_input"],
                post_apply=lambda: {"status": "OK", "partial_apply": False, "mismatches": []},
            ),
            mutation_guard=lambda action: supervisor._guard_session_mutation(session, snapshot, action),
            proposal_executor=WorkspaceProposalExecutor(self.root),
            recovery_callback_factory=supervisor._writer_apply_recovery_factory(binding),
        )
        self.addCleanup(gateway.close)
        context = ToolContext(policy.Actor("local_llm", "session-plan", 0, "turn-plan"), 0, "tool-plan")

        result = gateway.execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "create", "path": "plan.txt", "content": "governed"}]},
            context,
        )

        self.assertEqual(result["status"], "OK", result)
        self.assertIsNotNone(session.writer_claim_token)
        self.assertEqual(binding.current(), session.writer_claim_token)
        self.assertFalse(lease.apply_recovery_active)
        self.assertTrue(lease.release(session.writer_claim_token))

    def test_executor_crash_journals_reconcile_only_from_exact_whole_request_state(self) -> None:
        cases = ("modify-gap", "rename-mixed", "backup-cleanup", "create-all-base")
        for case in cases:
            with self.subTest(case=case):
                root = self.root / case
                root.mkdir()
                registry = OwnershipRegistry(
                    _runtime_dir(root) / "owned_runs.json", workspace_root=root,
                )
                lease = WorkspaceWriterLease(
                    registry, pid=os.getpid(), nonce="first", clock=lambda: "t1",
                    workspace_root=root,
                )
                claim, denial = lease.acquire("plan")
                self.assertIsNone(denial)
                executor = WorkspaceProposalExecutor(
                    root,
                    recovery_callback=lambda state, token=claim.claim_id: lease.set_apply_recovery(
                        token, state,
                    ),
                    path_authority=registry.authority,
                )
                try:
                    if case == "modify-gap":
                        target = root / "target.txt"
                        target.write_text("base", encoding="utf-8")
                        proposal = {"changes": [
                            {"kind": "modify", "path": "target.txt", "content": "final"},
                        ]}
                        with mock.patch.object(
                            executor, "_promote_staged", side_effect=OSError("injected gap"),
                        ):
                            result = executor.prepare(proposal).apply(proposal)
                        self.assertEqual(result["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
                        self.assertFalse(target.exists())
                        expected_reconcile = False
                    elif case == "rename-mixed":
                        source = root / "old.txt"
                        source.write_text("base", encoding="utf-8")
                        proposal = {"changes": [
                            {"kind": "rename", "old_path": "old.txt", "path": "new.txt"},
                        ]}
                        original_unlink = executor._unlink_baseline

                        def fail_source(path, baseline):
                            if path == source:
                                raise OSError("injected mixed rename")
                            return original_unlink(path, baseline)

                        with mock.patch.object(executor, "_unlink_baseline", side_effect=fail_source):
                            result = executor.prepare(proposal).apply(proposal)
                        self.assertEqual(result["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
                        self.assertTrue(source.exists())
                        self.assertTrue((root / "new.txt").exists())
                        expected_reconcile = False
                    elif case == "backup-cleanup":
                        target = root / "target.txt"
                        target.write_text("base", encoding="utf-8")
                        proposal = {"changes": [
                            {"kind": "modify", "path": "target.txt", "content": "final"},
                        ]}
                        original_cleanup = executor._cleanup_staged
                        with mock.patch.object(
                            executor,
                            "_cleanup_staged",
                            side_effect=lambda item: False if not item.promote else original_cleanup(item),
                        ):
                            result = executor.prepare(proposal).apply(proposal)
                        self.assertEqual(result["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
                        self.assertEqual(target.read_text(encoding="utf-8"), "final")
                        expected_reconcile = True
                    else:
                        proposal = {"changes": [
                            {"kind": "create", "path": "target.txt", "content": "final"},
                        ]}
                        with mock.patch.object(
                            executor, "_promote_staged", side_effect=OSError("injected first promote"),
                        ):
                            result = executor.prepare(proposal).apply(proposal)
                        self.assertEqual(result["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
                        self.assertFalse((root / "target.txt").exists())
                        expected_reconcile = True

                    before_paths = sorted(path.name for path in root.rglob("*.kaizen-*.tmp"))
                    self.assertTrue(before_paths)
                    restarted = WorkspaceWriterLease(
                        registry, pid=222, nonce="second", clock=lambda: "t2",
                        workspace_root=root,
                    )
                    self.assertEqual(
                        restarted.reconcile_after_orphan_sweep(lambda _pid: False),
                        expected_reconcile,
                    )
                    if expected_reconcile:
                        self.assertIsNone(registry.writer_marker())
                        self.assertEqual(list(root.rglob("*.kaizen-*.tmp")), [])
                    else:
                        self.assertIsNotNone(registry.writer_marker())
                        self.assertEqual(
                            sorted(path.name for path in root.rglob("*.kaizen-*.tmp")),
                            before_paths,
                        )
                finally:
                    executor.close()
                    registry.close()

    def test_registry_and_executor_concurrency_has_one_authority_first_lock_order(self) -> None:
        root = self.root / "lock-order"
        root.mkdir()
        registry = OwnershipRegistry(_runtime_dir(root) / "owned_runs.json", workspace_root=root)
        lease = WorkspaceWriterLease(
            registry, pid=os.getpid(), nonce="first", clock=lambda: "t1", workspace_root=root,
        )
        claim, _ = lease.acquire("plan")
        executor = WorkspaceProposalExecutor(
            root,
            recovery_callback=lambda state: lease.set_apply_recovery(claim.claim_id, state),
            path_authority=registry.authority,
        )
        barrier = threading.Barrier(3)
        errors: list[BaseException] = []

        def apply() -> None:
            try:
                barrier.wait()
                proposal = {"changes": [
                    {"kind": "create", "path": "target.txt", "content": "final"},
                ]}
                result = executor.prepare(proposal).apply(proposal)
                if result.get("status") != "OK":
                    raise AssertionError(result)
            except BaseException as error:  # noqa: BLE001 -- thread failures reach the test
                errors.append(error)

        def update_registry() -> None:
            try:
                barrier.wait()
                for index in range(20):
                    registry.record_run(f"parallel-{index}", pid=9000 + index, nonce="n")
            except BaseException as error:  # noqa: BLE001 -- thread failures reach the test
                errors.append(error)

        threads = [threading.Thread(target=apply), threading.Thread(target=update_registry)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)
        try:
            self.assertTrue(all(not thread.is_alive() for thread in threads), "authority lock deadlock")
            self.assertEqual(errors, [])
        finally:
            executor.close()
            registry.close()


class BoundedResponseProducerTest(unittest.TestCase):
    """Events responses preserve gapless cursors while respecting the loopback line-byte ceiling."""

    @staticmethod
    def _event(sequence_no: int, body: str) -> dict:
        return {
            "sequence_no": sequence_no, "event_kind": "chat_message", "marker": "point",
            "summary": "message", "correlation_id": None, "code": None, "body": body,
        }

    def test_single_legacy_oversize_event_advances_with_omitted_body(self) -> None:
        from kaizen_components.orchestration.supervisor import Supervisor

        sup = Supervisor()
        payload = sup._events_payload(
            "ar_legacy", [self._event(1, "x" * loopback._MAX_LINE_BYTES)], 0,
            False, None, controller="observed",
        )
        self.assertLessEqual(sup._response_bytes(payload), loopback._MAX_LINE_BYTES - 4096)
        self.assertEqual(payload["cursor"], 1)
        self.assertTrue(payload["events"][0]["body_omitted"])
        self.assertEqual(payload["events"][0]["body_omission_code"], "PAYLOAD_TOO_LARGE")

    def test_many_events_batch_gaplessly_below_frame(self) -> None:
        from kaizen_components.orchestration.supervisor import Supervisor

        sup = Supervisor()
        events = [self._event(index, "x" * 100_000) for index in range(1, 21)]
        first = sup._events_payload("ar_many", events, 0, False, None, controller="observed")
        self.assertLessEqual(sup._response_bytes(first), loopback._MAX_LINE_BYTES - 4096)
        self.assertTrue(first["truncated"])
        self.assertEqual(
            [event["sequence_no"] for event in first["events"]],
            list(range(1, first["cursor"] + 1)),
        )
        remaining = [event for event in events if event["sequence_no"] > first["cursor"]]
        second = sup._events_payload(
            "ar_many", remaining, first["cursor"], False, None, controller="observed",
        )
        self.assertEqual(second["events"][0]["sequence_no"], first["cursor"] + 1)
        self.assertLessEqual(sup._response_bytes(second), loopback._MAX_LINE_BYTES - 4096)


class CrashSweepTest(unittest.TestCase):
    """Exit criterion (1): crash/restart => truthful finalize. Open a T5 run recording a
    fake dead pid+nonce, boot the sweep, assert T8 canceled with dangling spans closed."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-m1-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        rc, _ = kaizen(self.root, "K1")
        self.assertEqual(rc, 0)

    def test_dead_owner_run_force_finalized(self) -> None:
        run_id = _open_run(self.root)
        # A dangling child span must be force-closed over immutable history.
        rc, _ = kaizen(self.root, "T6", "--agent-run-id", run_id, "--payload-json",
                       '{"event_kind":"subagent","marker":"open","correlation_id":"child1"}',
                       "--summary", "spawn child")
        self.assertEqual(rc, 0)
        # Record a dead owner (pid absent + nonce that is not the booting daemon's).
        ownership = _runtime_dir(self.root) / "owned_runs.json"
        ownership.write_text(
            json.dumps({run_id: {"pid": 999999, "nonce": "deadnonce000"}}), encoding="utf-8"
        )
        proc = run(self.root, "daemon", "run", "--exit-after-boot", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn(run_id, payload["swept"]["finalized"])
        # T7 now reports a terminal run whose immutable history still holds the dangling child.
        rc, inspect = kaizen(self.root, "T7", "--agent-run-id", run_id)
        self.assertEqual(rc, 0)
        state = inspect["state"]
        self.assertTrue(state["terminal"])
        self.assertEqual(state["terminal_state"], "failure")  # canceled is a non-success finalize
        self.assertEqual(state["open_children"], ["child1"])

    def test_live_owner_not_swept(self) -> None:
        # False-orphan guard (ledger #10): an owner whose pid is THIS live process (and a
        # nonce that is not the daemon's) must not be swept while alive.
        run_id = _open_run(self.root)
        ownership = _runtime_dir(self.root) / "owned_runs.json"
        ownership.write_text(
            json.dumps({run_id: {"pid": os.getpid(), "nonce": "someothernonce"}}), encoding="utf-8"
        )
        proc = run(self.root, "daemon", "run", "--exit-after-boot", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertNotIn(run_id, payload["swept"]["finalized"])
        rc, inspect = kaizen(self.root, "T7", "--agent-run-id", run_id)
        self.assertFalse(inspect["state"]["terminal"])


class SingleInstanceTest(unittest.TestCase):
    """Exit criterion (3): a second `daemon run` refuses via pidfile+nonce while the
    first is live. Simulated by writing a pidfile naming THIS live process, then booting."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-m1-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        rc, _ = kaizen(self.root, "K1")
        self.assertEqual(rc, 0)

    def test_second_daemon_refuses(self) -> None:
        pidfile = _runtime_dir(self.root) / "supervisor.pid"
        pidfile.write_text(
            json.dumps({"pid": os.getpid(), "nonce": "livenonce", "started_at": "now"}),
            encoding="utf-8",
        )
        proc = run(self.root, "daemon", "run", "--exit-after-boot", "--json")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        payload = json.loads(proc.stderr)
        self.assertEqual(payload["code"], "DENIED_DAEMON_ALREADY_RUNNING")

    def test_stale_pidfile_is_reclaimed(self) -> None:
        # A pidfile naming a DEAD pid must not block a fresh boot.
        pidfile = _runtime_dir(self.root) / "supervisor.pid"
        pidfile.write_text(
            json.dumps({"pid": 999999, "nonce": "deadnonce", "started_at": "old"}),
            encoding="utf-8",
        )
        proc = run(self.root, "daemon", "run", "--exit-after-boot", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_supervisor_refuses_cross_process_while_os_lock_is_held(self) -> None:
        relative = "AI/work/orchestration/runtime/supervisor.lock"
        with WorkspacePathAuthority(self.root) as authority:
            authority.ensure_directory("AI/work/orchestration/runtime")
            held = authority.acquire_process_lock(relative)
            try:
                proc = run(self.root, "daemon", "run", "--exit-after-boot", "--json")
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertEqual(json.loads(proc.stderr)["code"], "DENIED_DAEMON_ALREADY_RUNNING")
            finally:
                held.close()
        warm = run(self.root, "daemon", "run", "--exit-after-boot", "--json")
        self.assertEqual(warm.returncode, 0, warm.stderr)

    def test_direct_boot_failure_releases_lifetime_lock_and_root_authority(self) -> None:
        first = supervisor_module.Supervisor(self.root)
        with mock.patch.object(first, "orphan_sweep", side_effect=RuntimeError("injected boot failure")):
            with self.assertRaisesRegex(RuntimeError, "injected boot failure"):
                first.boot()
        second = supervisor_module.Supervisor(self.root)
        try:
            second._claim_single_instance()
        finally:
            second.shutdown()

    def test_direct_boot_conflict_closes_the_denied_supervisor_authority(self) -> None:
        first = supervisor_module.Supervisor(self.root)
        denied = supervisor_module.Supervisor(self.root)
        first._claim_single_instance()
        try:
            with self.assertRaises(supervisor_module.SingleInstanceError):
                denied.boot()
        finally:
            first.shutdown()
        # Windows: a successful roundtrip rename proves the denied supervisor released its root handle.
        moved = self.root.with_name(self.root.name + "-moved")
        self.root.rename(moved)
        moved.rename(self.root)

    def test_constructor_failure_releases_root_authority(self) -> None:
        candidate = self.root / "constructor-failure"
        candidate.mkdir()
        with mock.patch.object(
            supervisor_module, "DiffSnapshotManager", side_effect=RuntimeError("constructor failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "constructor failure"):
                supervisor_module.Supervisor(candidate)
        # Windows: a successful roundtrip rename proves constructor failure released its root handle.
        moved = self.root / "constructor-failure-moved"
        candidate.rename(moved)
        moved.rename(candidate)

    def test_approval_reconcile_failure_preserves_writer_marker_and_denies_new_claim(self) -> None:
        supervisor = supervisor_module.Supervisor(self.root)
        prior = WorkspaceWriterLease(
            supervisor._ownership_registry,
            pid=111,
            nonce="prior",
            clock=lambda: "t0",
            workspace_root=self.root,
        )
        claim, denial = prior.acquire("ask")
        self.assertIsNone(denial)
        before = supervisor._ownership_registry.writer_marker()
        engine = mock.Mock(protected_paths=(), rules=())
        try:
            with mock.patch.object(supervisor, "orphan_sweep", return_value={}), \
                    mock.patch.object(supervisor, "_reconcile_approval_orphans", return_value=False), \
                    mock.patch.object(supervisor._artifact_cache, "cleanup"), \
                    mock.patch.object(supervisor, "_git_worktree_prune"), \
                    mock.patch.object(supervisor_module.policy, "build_engine_from_db", return_value=engine), \
                    mock.patch.object(supervisor, "_build_capabilities", return_value=[]), \
                    mock.patch.object(supervisor, "_boot_fleet"), \
                    mock.patch.object(supervisor, "_start_loopback"), \
                    mock.patch.object(supervisor, "_start_control", return_value=None), \
                    mock.patch.object(supervisor, "log"), \
                    mock.patch.object(supervisor_module.children, "pid_alive", return_value=False):
                result = supervisor._boot_claimed()
            self.assertFalse(result["approvals_reconciled"])
            self.assertFalse(result["writer_reconciled"])
            self.assertEqual(supervisor._ownership_registry.writer_marker(), before)
            new_claim, denied = supervisor._writer_lease.acquire("ask")
            self.assertIsNone(new_claim)
            self.assertEqual(denied["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
            self.assertEqual(supervisor._ownership_registry.writer_marker()["claim_id"], claim.claim_id)
        finally:
            supervisor.shutdown()

    def test_pid_token_and_log_reparse_targets_never_receive_runtime_writes(self) -> None:
        outside = self.root / "outside-runtime-target.txt"
        outside.write_text("outside", encoding="utf-8")
        probe_root = self.root / "reparse-probe"
        runtime = _runtime_dir(probe_root)
        probe = runtime / "supervisor.pid"
        try:
            os.symlink(outside, probe, target_is_directory=False)
        except OSError as error:
            self.skipTest(f"real reparse/symlink fixture unavailable: {error}")
        probe.unlink()

        for filename, action in (
            ("supervisor.pid", "claim"),
            ("control.token", "claim"),
            ("supervisor.log", "log"),
        ):
            with self.subTest(filename=filename):
                root = self.root / ("reparse-" + filename.replace(".", "-"))
                runtime = _runtime_dir(root)
                os.symlink(outside, runtime / filename, target_is_directory=False)
                supervisor = supervisor_module.Supervisor(root)
                try:
                    if action == "claim":
                        expected_error = (
                            supervisor_module.SingleInstanceError
                            if filename == "supervisor.pid"
                            else WorkspacePathError
                        )
                        with self.assertRaises(expected_error):
                            supervisor._claim_single_instance()
                    else:
                        supervisor.log("must not reach redirected log")
                    self.assertEqual(outside.read_text(encoding="utf-8"), "outside")
                    if filename == "control.token":
                        status = supervisor_module.send_control(
                            op="status", args={}, repo_root=root, timeout=0.1,
                        )
                        self.assertEqual(status["code"], "DENIED_WORKSPACE_PATH_AUTHORITY")
                        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")
                finally:
                    supervisor.shutdown()

    def test_supervisor_log_is_bounded_and_rotates_via_authority(self) -> None:
        supervisor = supervisor_module.Supervisor(self.root)
        try:
            with mock.patch.object(supervisor_module, "_LOGFILE_MAX_BYTES", 128):
                supervisor.log("x" * 200)
                supervisor.log("y" * 200)
                supervisor.log("z" * 200)
                stable = supervisor._ownership_registry.authority.read(
                    supervisor._logfile_relative, 128,
                )
            self.assertLessEqual(stable.size, 128)
            self.assertIn(supervisor_module._LOG_TRUNCATION_MARKER, stable.data)
            self.assertIn(b"z", stable.data)
        finally:
            supervisor.shutdown()


class StatusClientTest(unittest.TestCase):
    def test_status_reports_not_running_cleanly(self) -> None:
        # `daemon status` is a loopback client; with no daemon up it reports a clean
        # not-running result (never a traceback).
        root = Path(tempfile.mkdtemp(prefix="kaizen-m1-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        proc = run(root, "daemon", "status", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["running"])


class Cp1252RegressionTest(unittest.TestCase):
    """Exit criterion (4) / GOTCHA g_20260708084954: a non-ASCII --json emit under a
    forced cp1252 stdout must exit 0 with clean JSON. Drive kaizen.py in a subprocess
    with PYTHONIOENCODING=cp1252 and push a non-ASCII payload through a T6 event."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-m1-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        rc, _ = kaizen(self.root, "K1")
        self.assertEqual(rc, 0)

    def test_non_ascii_json_emit_under_cp1252(self) -> None:
        run_id = _open_run(self.root, summary="cp1252 run")
        # A non-ASCII summary (em dash, accented chars) through T6, emitted --json.
        proc = run(
            self.root, "T6", "--agent-run-id", run_id, "--payload-json",
            '{"event_kind":"turn","marker":"open","correlation_id":"t1"}',
            "--summary", "café — naïve résumé", "--json",
            env={"PYTHONIOENCODING": "cp1252"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)  # must be valid JSON despite the cp1252 stream
        self.assertEqual(payload["status"], "OK")


class EnvCompositionTest(unittest.TestCase):
    """Exit criterion (5): the supervisor spawns a child that echoes its env; ANTHROPIC_*
    absent even when set in the parent, and PYTHONUTF8=1 present."""

    def test_child_env_stripped_and_utf8_forced(self) -> None:
        # Exercise the pure composer AND the real spawn path (children.spawn_owned).
        from kaizen_components.orchestration import children

        parent = {
            "ANTHROPIC_API_KEY": "sk-should-not-propagate",
            "ANTHROPIC_AUTH_TOKEN": "tok-should-not-propagate",
            "PYTHONIOENCODING": "cp1252",
            "KEEP_ME": "yes",
        }
        composed = children.compose_child_env(parent)
        self.assertNotIn("ANTHROPIC_API_KEY", composed)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", composed)
        self.assertEqual(composed["PYTHONUTF8"], "1")
        self.assertEqual(composed["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(composed["KEEP_ME"], "yes")

    def test_spawned_child_sees_composed_env(self) -> None:
        from kaizen_components.orchestration import children

        os.environ["ANTHROPIC_API_KEY"] = "sk-leak"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "tok-leak"
        self.addCleanup(os.environ.pop, "ANTHROPIC_API_KEY", None)
        self.addCleanup(os.environ.pop, "ANTHROPIC_AUTH_TOKEN", None)
        script = (
            "import os, json, sys;"
            "sys.stdout.write(json.dumps({"
            "'api': os.environ.get('ANTHROPIC_API_KEY'),"
            "'auth': os.environ.get('ANTHROPIC_AUTH_TOKEN'),"
            "'utf8': os.environ.get('PYTHONUTF8'),"
            "}))"
        )
        child = children.spawn_owned([sys.executable, "-c", script])
        out, _ = child.process.communicate(timeout=30)
        child.release()
        echoed = json.loads(out)
        self.assertIsNone(echoed["api"])
        self.assertIsNone(echoed["auth"])
        self.assertEqual(echoed["utf8"], "1")


class LoopbackAuthTest(unittest.TestCase):
    """Exit criterion (6): wrong/missing token rejected on the loopback channel."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-m1-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.runtime = _runtime_dir(self.root)
        self.server = loopback.LoopbackServer(
            self.root, self.runtime, "secret-token", self._handler
        )
        self.server.start()
        self.addCleanup(self.server.stop)

    @staticmethod
    def _handler(request: dict) -> dict:
        return {"status": "OK", "op": request.get("op")}

    def test_correct_token_accepted(self) -> None:
        resp = loopback.send_request(
            self.root, self.runtime, {"op": "ping", "token": "secret-token"}, timeout=3
        )
        self.assertEqual(resp["status"], "OK")

    def test_wrong_token_rejected(self) -> None:
        resp = loopback.send_request(
            self.root, self.runtime, {"op": "ping", "token": "WRONG"}, timeout=3
        )
        self.assertEqual(resp["code"], "DENIED_LOOPBACK_AUTH")

    def test_fast_request_succeeds_while_long_poll_is_parked(self) -> None:
        # 2026-07-10 fix: the pipe server keeps a spare instance and serves each connection on its
        # own worker thread (socket-path parity). Before it, a parked session/events long-poll (the
        # idle chat pump long-polls back-to-back for up to 25s) held the ONLY pipe instance, so every
        # status/observed/timeline poll flapped DAEMON_UNREACHABLE ("no loopback transport present").
        slow_started = threading.Event()
        release_slow = threading.Event()
        self.addCleanup(release_slow.set)
        base_handler = self._handler

        def handler(request: dict) -> dict:
            if request.get("op") == "slow":
                slow_started.set()
                release_slow.wait(timeout=30.0)  # a parked long-poll
            return base_handler(request)

        self.server.handler = handler
        results: dict[str, dict] = {}

        def call_slow() -> None:
            results["slow"] = loopback.send_request(
                self.root, self.runtime, {"op": "slow", "token": "secret-token"}, timeout=30
            )

        slow_thread = threading.Thread(target=call_slow, daemon=True)
        slow_thread.start()
        self.assertTrue(slow_started.wait(timeout=10.0), "slow request never reached the handler")
        # While the slow handler is PARKED, a second client must still get through immediately.
        fast = loopback.send_request(
            self.root, self.runtime, {"op": "fast", "token": "secret-token"}, timeout=5
        )
        self.assertEqual(fast["status"], "OK")
        self.assertEqual(fast["op"], "fast")
        release_slow.set()
        slow_thread.join(timeout=10.0)
        self.assertFalse(slow_thread.is_alive())
        self.assertEqual(results["slow"]["status"], "OK")

    def test_missing_token_rejected(self) -> None:
        resp = loopback.send_request(
            self.root, self.runtime, {"op": "ping"}, timeout=3
        )
        self.assertEqual(resp["code"], "DENIED_LOOPBACK_AUTH")


class EngineRegistrationTest(unittest.TestCase):
    """M8 (§D deferral insurance): the UI's engine selector enumerates lanes from ADAPTER REGISTRATION.
    The list is DISCOVERED from orchestration/adapters, and the claude lane is absent until M-CLAUDE
    actually lands an adapter (the UI greys it out)."""

    def test_registered_engines_discovered_not_hardcoded(self) -> None:
        from kaizen_components.orchestration.adapters import ADAPTER_CONSTRUCTORS, get_adapter_constructor
        from kaizen_components.orchestration.supervisor import _registered_engines

        engines = _registered_engines()
        self.assertEqual(set(engines), {"local_llm", "codex", "claude_cli"})
        self.assertEqual(set(ADAPTER_CONSTRUCTORS), set(engines))
        self.assertIs(get_adapter_constructor("claude"), ADAPTER_CONSTRUCTORS["claude_cli"])
        self.assertNotIn("claude", engines)

    def test_status_payload_carries_engines_and_node_id(self) -> None:
        # The two M8 status fields ride a REAL booted supervisor's status op (subprocess, scratch plane;
        # dist off => node_id is None, truthfully).
        import shutil
        import subprocess

        root = Path(tempfile.mkdtemp(prefix="kaizen-m8-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.assertEqual(kaizen(root, "K1")[0], 0)
        body = (
            "sup = Supervisor(); sup.boot()\n"
            "resp = sup._handle_control({'op': 'status'})\n"
            "sup.shutdown()\n"
            "out = {'resp': resp}\n"
        )
        preamble = (
            "import json\n"
            "from kaizen_components.orchestration.supervisor import Supervisor\n"
            "out = None\n"
            "exec(BODY)\n"
            "print('RESULT ' + json.dumps(out))\n"
        )
        script = "BODY = " + repr(body) + "\n" + preamble
        env = dict(os.environ)
        env["KAIZEN_REPO_ROOT"] = str(root)
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env,
            cwd=str(REPO_ROOT), timeout=120,
        )
        line = next((ln for ln in (proc.stdout or "").splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, f"stdout={proc.stdout!r} stderr={proc.stderr[-500:]!r}")
        resp = json.loads(line[len("RESULT "):])["resp"]
        self.assertEqual(resp["status"], "OK")
        self.assertIn("codex", resp["engines"])
        self.assertNotIn("claude", resp["engines"])
        self.assertIsNone(resp["node_id"])  # dist off => no fleet identity, truthfully
        self.assertEqual(resp["active_durable_runs"], 0)


class SessionCliHelpTest(unittest.TestCase):
    def test_json_flag_survives_supported_subcommand_positions(self) -> None:
        from kaizen_components.orchestration.daemon_cli import build_daemon_parser

        parser = build_daemon_parser()
        for argv in (["status", "--json"], ["session", "--json", "capabilities"],
                     ["session", "capabilities", "--json"]):
            with self.subTest(argv=argv):
                self.assertTrue(parser.parse_args(argv).json)

    def test_session_help_exposes_turn_and_close(self) -> None:
        from kaizen_components.orchestration.daemon_cli import build_daemon_parser

        parser = build_daemon_parser()
        daemon_choices = next(
            action.choices for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        session_parser = daemon_choices["session"]
        session_choices = next(
            action.choices for action in session_parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        expected = {"capabilities", "start", "turn", "close", "list", "events", "steer", "interrupt", "kill"}
        self.assertTrue(expected <= set(session_choices))
        help_text = session_parser.format_help()
        self.assertIn("turn", help_text)
        self.assertIn("close", help_text)

    def test_approve_answers_file_reaches_the_exact_loopback_wire(self) -> None:
        from kaizen_components.orchestration import daemon_cli

        work = REPO_ROOT / "AI" / "work"
        with tempfile.TemporaryDirectory(prefix="daemon-answers-", dir=work) as raw:
            answers = Path(raw) / "answers.json"
            answers.write_text(json.dumps({"reason": "  exact answer  "}), encoding="utf-8")
            with mock.patch.object(
                supervisor_module, "send_control", return_value={"status": "OK"},
            ) as send, mock.patch.object(daemon_cli, "emit", return_value=0):
                rc = daemon_cli.daemon_main([
                    "approve", "--correlation-id", "corr-1", "--session", "session-1",
                    "--decision", "approve", "--answers-json-file", str(answers), "--json",
                ])
            self.assertEqual(rc, 0)
            send.assert_called_once_with(op="approve", args={
                "decision": "approve",
                "correlation_id": "corr-1",
                "session_id": "session-1",
                "answers": {"reason": "  exact answer  "},
            })

    def test_approve_answers_file_rejects_non_utf8_non_object_and_oversize_before_loopback(self) -> None:
        from kaizen_components.orchestration import daemon_cli

        work = REPO_ROOT / "AI" / "work"
        with tempfile.TemporaryDirectory(prefix="daemon-answers-invalid-", dir=work) as raw:
            root = Path(raw)
            cases = {
                "non-utf8": b"{\xff}",
                "non-object": b"[]",
                "invalid-json": b"{",
                "oversize": b"{" + b" " * (256 * 1024) + b"}",
            }
            for name, payload in cases.items():
                with self.subTest(name=name):
                    path = root / f"{name}.json"
                    path.write_bytes(payload)
                    with mock.patch.object(supervisor_module, "send_control") as send, mock.patch.object(
                        daemon_cli, "emit_error", return_value=2,
                    ) as emit_error:
                        rc = daemon_cli.daemon_main([
                            "approve", "--approval-id", "approval-1", "--decision", "approve",
                            "--answers-json-file", str(path), "--json",
                        ])
                    self.assertEqual(rc, 2)
                    send.assert_not_called()
                    self.assertEqual(emit_error.call_args.args[0]["code"], "DENIED_USER_INPUT_ANSWERS_INVALID")


class DrivenIdleOrphanSweepTest(_DrivenSubprocess):
    """Sweep truthfulness + conversation continuation (owner decision 2026-07-10): a driven leg whose
    daemon dies is force-finalized to EXACTLY ONE canceled T8 and stays replayable read-only -- but the
    CONVERSATION is a durable asset: session/turn on the orphaned LATEST leg rehydrates a NEW linked T5
    under the same open C1, seeded from durable chat events and deciding from the ORIGINAL stored
    snapshot. Closed/killed conversations stay terminal (the resume marker gates on the sweep summary)."""

    def test_swept_leg_finalizes_once_and_conversation_resumes_as_new_leg(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration import supervisor as S\n"
            "sup = Supervisor(); sup.boot()\n"
            "install_factory(sup, scripted_provider([final_reply('one')]))\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'first'}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "wait_idle(sup, rid)\n"  # turn completes to idle -- NO close, so the run stays non-terminal
            "idle_state = sup._safe_reduce(rid)\n"
            "fin_before = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
            "# Owner death: rewrite the run's ownership entry to a dead pid + foreign nonce (ledger #10\n"
            "# demands nonce inequality AND a dead pid before a sweep may fire).\n"
            "registry = json.loads(S.OWNERSHIP_FILE.read_text(encoding='utf-8'))\n"
            "registry[rid] = {'pid': 999999, 'nonce': 'deadnonce000'}\n"
            "S.OWNERSHIP_FILE.write_text(json.dumps(registry), encoding='utf-8')\n"
            "# A FRESH Supervisor (new nonce) runs the boot sweep directly. orphan_sweep (not boot) so no\n"
            "# single-instance claim collides with the still-live first daemon holding the pidfile.\n"
            "sweeper = Supervisor()\n"
            "install_factory(sweeper, scripted_provider([final_reply('resumed-two')]))\n"
            "swept = sweeper.orphan_sweep()\n"
            "swept_again = sweeper.orphan_sweep()\n"  # idempotent: the now-terminal run is not re-finalized
            "state = sweeper._safe_reduce(rid)\n"
            "fin_after = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
            "open_c4 = db.fetch_one(\"SELECT COUNT(*) FROM approval_requests WHERE session_id = ? AND state = 'open'\", (sid,))[0]\n"
            "ev = sweeper._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})\n"
            "snap_row = db.fetch_one('SELECT policy_snapshot, state FROM agent_sessions WHERE id = ?', (sid,))\n"
            "live_before_turn = len(sweeper._driven)\n"
            "resumable_before = sweeper._handle_control({'op': 'session/list', 'args': {'controller': 'driven'}})['sessions'][0]['resumable']\n"
            "# CONTINUATION: the same turn that previously refused now rehydrates a new linked leg.\n"
            "turn = sweeper._handle_control({'op': 'session/turn', 'args': {'agent_run_id': rid, 'prompt': 'again'}})\n"
            "new_rid = turn.get('agent_run_id')\n"
            "wait_idle(sweeper, new_rid)\n"
            "adapter = sweeper._driven[new_rid].adapter\n"
            "seeded = [dict(m) for m in adapter.conversation_history]\n"
            "from kaizen_components.orchestration import policy as _pol\n"
            "rehydrated = _pol.snapshot_from_json(snap_row[0])\n"
            "snap_roots = sorted(rehydrated.designated_write_roots)\n"
            "chat = [json.loads(r[0]) for r in db.fetch_all(\"SELECT e.body FROM agent_events e \"\n"
            "    \"JOIN agent_runs r ON e.agent_run_id = r.id WHERE r.session_id = ? \"\n"
            "    \"AND e.event_kind = 'chat_message' ORDER BY r.created_at, r.id, e.sequence_no\", (sid,))]\n"
            "runs = [r[0] for r in db.fetch_all('SELECT id FROM agent_runs WHERE session_id = ? ORDER BY created_at, id', (sid,))]\n"
            "# The OLD leg is no longer the latest: turning it again refuses terminal (no forking).\n"
            "old_turn = sweeper._handle_control({'op': 'session/turn', 'args': {'agent_run_id': rid, 'prompt': 'fork'}})\n"
            "close = sweeper._handle_control({'op': 'session/close', 'args': {'agent_run_id': new_rid}})\n"
            "resumable_after = sweeper._handle_control({'op': 'session/list', 'args': {'controller': 'driven'}})['sessions'][0]['resumable']\n"
            "new_state = sweeper._safe_reduce(new_rid)\n"
            "old_fin_final = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
            "sup.shutdown()\n"
            "sweeper.shutdown()\n"
            "out = {'idle_terminal': idle_state['terminal'], 'fin_before': fin_before,\n"
            "       'swept': swept, 'swept_again': swept_again,\n"
            "       'terminal': state['terminal'], 'terminal_state': state['terminal_state'],\n"
            "       'open_children': state['open_children'], 'unresolved_approvals': state['unresolved_approvals'],\n"
            "       'fin_after': fin_after, 'open_c4': open_c4,\n"
            "       'ev_terminal': ev['terminal'], 'ev_seqs': [e['sequence_no'] for e in ev['events']],\n"
            "       'session_state': snap_row[1], 'live_before_turn': live_before_turn,\n"
            "       'turn': {k: turn.get(k) for k in ('status', 'code', 'agent_run_id', 'resumed_from', 'profile_hash')},\n"
            "       'start_hash': start['profile_hash'], 'seeded': seeded, 'snap_roots': snap_roots,\n"
            "       'snap_hash': rehydrated.profile_hash,\n"
            "       'chat': [(m['role'], m['text']) for m in chat], 'runs': runs, 'rid': rid,\n"
            "       'resumable_before': resumable_before, 'resumable_after': resumable_after,\n"
            "       'old_turn_code': old_turn.get('code'), 'close': close.get('status'),\n"
            "       'new_terminal_state': new_state.get('terminal_state') if new_state else None,\n"
            "       'old_fin_final': old_fin_final}\n"
        )
        # The turn reached idle without terminalizing: the sweep has a genuine non-terminal driven run.
        self.assertFalse(out["idle_terminal"])
        self.assertEqual(out["fin_before"], 0)
        # EXACTLY the one run swept, once; the second sweep is a no-op (run already terminal).
        self.assertEqual(len(out["swept"]["finalized"]), 1)
        self.assertEqual(out["swept"]["scanned"], 1)
        self.assertEqual(out["swept_again"]["finalized"], [])
        # Terminal + canceled (a non-success finalize reduces to terminal_state 'failure'); NO double T8.
        self.assertTrue(out["terminal"])
        self.assertEqual(out["terminal_state"], "failure")
        self.assertEqual(out["fin_after"], 1)
        self.assertEqual(out["open_children"], [])
        self.assertEqual(out["unresolved_approvals"], [])
        self.assertEqual(out["open_c4"], 0)
        # The swept leg stays replayable read-only, gapless from 1; the C1 envelope stays OPEN.
        self.assertTrue(out["ev_terminal"])
        self.assertEqual(out["ev_seqs"], list(range(1, len(out["ev_seqs"]) + 1)))
        self.assertEqual(out["session_state"], "open")
        self.assertEqual(out["live_before_turn"], 0)
        # session/list carries the AUTHORITATIVE continuation verdict: true for the swept conversation
        # AND still true after an explicit close (owner 2026-07-11: no conversation dead-ends -- a
        # closed conversation continues on the next send as another linked leg).
        self.assertTrue(out["resumable_before"])
        self.assertTrue(out["resumable_after"])
        # CONTINUATION: the turn resumed as a NEW linked leg under the SAME C1 + profile hash.
        self.assertEqual(out["turn"]["status"], "OK", out["turn"])
        self.assertEqual(out["turn"]["resumed_from"], out["rid"])
        self.assertNotEqual(out["turn"]["agent_run_id"], out["rid"])
        self.assertEqual(out["turn"]["profile_hash"], out["start_hash"])
        # Restart continuation is one bounded fixed frame followed by the new durable answer; the local
        # adapter is never seeded with an unbounded historical message array.
        self.assertEqual(len(out["seeded"]), 2)
        self.assertEqual(out["seeded"][0]["role"], "user")
        self.assertIn("KAIZEN_DURABLE_HISTORY_V1", out["seeded"][0]["content"])
        self.assertIn('"role":"user","text":"first"', out["seeded"][0]["content"])
        self.assertIn('"role":"assistant","text":"one"', out["seeded"][0]["content"])
        self.assertTrue(out["seeded"][0]["content"].endswith("again"))
        self.assertEqual(out["seeded"][1], {"role": "assistant", "content": "resumed-two"})
        # The C1 stored a complete, verbatim-rehydratable snapshot: plan roots materialized, hash
        # identity intact (the factory test seam deliberately runs its own engine; the REAL local and
        # vendor paths build from this snapshot via PolicyEngine.from_snapshot -- round-trip fidelity is
        # unit-locked by SnapshotSerializationTest).
        self.assertEqual(len(out["snap_roots"]), 2)
        self.assertEqual(out["snap_hash"], out["start_hash"])
        # One continuous durable transcript across both legs; exactly two linked runs, oldest first.
        self.assertEqual(
            out["chat"],
            [["user", "first"], ["assistant", "one"], ["user", "again"], ["assistant", "resumed-two"]],
        )
        self.assertEqual(len(out["runs"]), 2)
        self.assertEqual(out["runs"][0], out["rid"])
        # No forking an old leg; explicit close ends the conversation with one success T8 on the new leg
        # while the swept leg keeps exactly its single canceled T8.
        self.assertEqual(out["old_turn_code"], "DENIED_SESSION_TERMINAL")
        self.assertEqual(out["close"], "OK")
        self.assertEqual(out["new_terminal_state"], "success")
        self.assertEqual(out["old_fin_final"], 1)


if __name__ == "__main__":
    unittest.main()
