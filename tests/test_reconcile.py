"""Offline resilience + cross-machine orphan recovery (v8 M15, plan §B.6, deps M10b+M12).

Layered like the fleet suite (test_mirror / test_remote_dispatch conventions):

1. **In-process, DB-free FakeStore over the REAL reducers** (the bulk). ``reconcile.py`` takes an
   injected store + git_runner, so a lightweight ``FakeStore`` (an in-memory coord_event list feeding
   the real :mod:`fleet.reducers` and :mod:`fleet.coordination.current_leases`) exercises iso-scope
   discovery, offline_status, overlap detection, auto-adopt, and the orphan sweep with ZERO DB and ZERO
   network. The FakeStore has NO kaizen.db access at all, so a sweep completing over it is the structural
   proof that the survivor never needs a dead node's kaizen.db (the §3.2 boundary).
2. **In-process scratch FleetStore + REAL git** (publish + a couple of ledger legs). A temp fleet.db +
   an injected node identity (the test_remote_dispatch idiom) with a real ``git`` runner bound to SCRATCH
   repos exercises the policy-gated publish (push + ls-remote confirm to a scratch bare hub).
3. **Subprocess scratch-plane** (IsolatedDBTest for the D9 CLI; a real in-process Supervisor for the
   loopback) -- a fresh KAIZEN_REPO_ROOT per test so no real AI/db is touched.

OWNER DECISION D7 (locked): SINGLE-MACHINE labs only -- "multi-node" is multiple node-identity dicts over
a scratch store (the established stand-in); no second physical machine, no live sync server in unittest.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import REPO_ROOT, IsolatedDBTest, kaizen  # noqa: E402

from kaizen_components.denials import KaizenDenied  # noqa: E402
from kaizen_components.fleet import coordination, reconcile, reducers  # noqa: E402
from kaizen_components.fleet.store import FleetStore  # noqa: E402

GIT = shutil.which("git")

_OLD = "2026-07-09T00:00:00+00:00"       # ancient heartbeat
_NOW = "2026-07-09T12:00:00+00:00"       # 12h later -> far past any threshold
_EVENT_BASE = datetime(2026, 7, 9, tzinfo=timezone.utc)


def _event_time(sequence: int) -> str:
    """Return a valid monotonic ISO timestamp for an arbitrary synthetic event sequence."""
    return (_EVENT_BASE + timedelta(seconds=sequence)).isoformat(timespec="seconds")


def _git_checked(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run one bounded scratch Git command and fail at the command that broke fixture setup."""
    result = subprocess.run([GIT, *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise AssertionError(f"scratch git failed: argv={args!r}; rc={result.returncode}; stdout={result.stdout!r}; stderr={result.stderr!r}")
    return result


def _init_scratch_repo(test: unittest.TestCase, prefix: str, *, branch: str | None = None) -> str:
    """Create, configure, and commit one auto-cleaned scratch repository, optionally adding a branch."""
    repo = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(shutil.rmtree, repo, ignore_errors=True)
    _git_checked("init", "-q", "-b", "main", repo)
    for key, value in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git_checked("-C", repo, "config", key, value)
    Path(repo, "a.txt").write_text("one\n", encoding="utf-8")
    _git_checked("-C", repo, "add", "-A")
    _git_checked("-C", repo, "commit", "-qm", "init")
    if branch:
        _git_checked("-C", repo, "branch", branch)
    return repo


# --- a fleet.db-only fake store: an in-memory coord ledger + remote_dispatches over the REAL reducers

class _FakeSync:
    def __init__(self, synced: bool) -> None:
        self.synced = synced


class FakeStore:
    """A minimal FleetStore stand-in for reconcile.py: records appended coord_events in a list and reads
    them back so the real reducers / coordination.current_leases run over them. NO DB, NO network, NO
    kaizen.db. ``append_coord_event`` validates (kind x marker) against the registry so a bad pair fails
    here exactly as in the store. ``pull_and_reduce`` is a monkeypatchable no-op (an injected raiser
    simulates an unreachable hub). NO ``_read_all`` -> reconcile's sweep must work off reduced state
    alone (the structural kaizen.db-free proof)."""

    def __init__(self, node_id: str = "nSurvivor0001", *, synced: bool = False) -> None:
        self._node_id = node_id
        self._events: list[dict] = []
        self._seq = 0
        self.sync = _FakeSync(synced)
        # A minimal in-memory model of fleet.db's remote_dispatches (SHARED/synced coordination state --
        # NOT kaizen.db). dispatch_remote.cancel_dispatch reads/updates this row; the kaizen.db boundary
        # is about a DEAD NODE's agent_runs (a DIFFERENT, un-synced DB), never this fleet.db table.
        self._dispatch_rows: dict[str, dict] = {}
        self.reads: list[str] = []  # every _read_all SQL, so a test can assert no kaizen.db table is touched

    @property
    def node_id(self) -> str:
        return self._node_id

    def pull_and_reduce(self, limit=None):  # noqa: ANN001, ARG002 -- monkeypatched per test
        return {"status": "OK", "pull": {"synced": self.sync.synced}}

    # --- minimal fleet.db remote_dispatches shim (only what dispatch_remote needs) ----------------
    def _read_all(self, sql, params=()):  # noqa: ANN001
        self.reads.append(sql)
        if "FROM remote_dispatches WHERE id" in sql:
            row = self._dispatch_rows.get(params[0])
            if row is None:
                return []
            return [(row["id"], row["created_at"], row["target_node_id"], row["required_leases_json"], row["status"], row["payload_json"])]
        if "FROM nodes" in sql:
            # No nodes table in this fleet.db-only fake -> the sweep falls back to the reduced heartbeat.
            return []
        raise AssertionError(f"unexpected _read_all: {sql}")

    def _write(self, operation):  # noqa: ANN001
        """Minimal fleet write shim supporting the dispatch-cancellation status update used by reconciliation."""
        class _Conn:
            def __init__(self, outer):
                self.outer = outer

            def execute(self, sql, params=()):  # noqa: ANN001
                if sql.startswith("UPDATE remote_dispatches SET status"):
                    self.outer._dispatch_rows[params[1]]["status"] = params[0]
                    return self
                raise AssertionError(f"unexpected _write execute: {sql}")

        return operation(_Conn(self))

    def seed_dispatch(self, dispatch_id: str, *, target_node: str, scope_key: str, status: str = "requested") -> None:
        """Seed a fleet.db remote_dispatches row + its dispatch/requested coord_event so a non-terminal
        dispatch targeting a (soon-stale) node exists for the sweep to cancel."""
        self._dispatch_rows[dispatch_id] = {
            "id": dispatch_id, "created_at": _OLD, "target_node_id": target_node,
            "required_leases_json": json.dumps([scope_key]), "status": status,
            "payload_json": json.dumps({"task": "t", "scope_key": scope_key}),
        }
        self.inject("dispatch", "requested", node_id=self._node_id, scope_key=scope_key,
                    payload={"dispatch_id": dispatch_id, "target_node": target_node}, created_at=_OLD)

    def append_coord_event(self, event_kind, marker, *, summary, scope_key=None, epoch=None, payload=None, project_id=None, source_event_id=None):  # noqa: ANN001
        from kaizen_components.schemas.registry import COORD_EVENT_KIND_MARKERS

        assert event_kind in COORD_EVENT_KIND_MARKERS and marker in COORD_EVENT_KIND_MARKERS[event_kind], (
            f"unregistered coord pair ({event_kind}, {marker})"
        )
        self._seq += 1
        eid = f"ce_{self._seq:04d}_{self._node_id[-6:]}"
        created = _event_time(self._seq)
        row = {
            "id": eid, "created_at": created, "node_id": self._node_id, "project_id": project_id,
            "event_kind": event_kind, "marker": marker, "scope_key": scope_key, "epoch": epoch,
            "payload": payload if isinstance(payload, dict) else None,
        }
        self._events.append(row)
        return {"status": "OK", "id": eid, "event_kind": event_kind, "marker": marker}

    def coord_events(self) -> list[dict]:
        return list(self._events)

    # --- test injection helpers (bypass the append clock so timestamps are controllable) ---------
    def inject(self, event_kind, marker, *, scope_key=None, epoch=None, payload=None, node_id=None, created_at=None):  # noqa: ANN001
        """Append a raw event with a caller-chosen node_id/created_at (to seed ANOTHER node's events or a
        specific heartbeat age) -- straight into the list, no validation clock."""
        self._seq += 1
        eid = f"inj_{self._seq:04d}"
        row = {
            "id": eid, "created_at": created_at or _event_time(self._seq),
            "node_id": node_id or self._node_id, "project_id": None,
            "event_kind": event_kind, "marker": marker, "scope_key": scope_key, "epoch": epoch,
            "payload": payload if isinstance(payload, dict) else None,
        }
        self._events.append(row)
        return eid

    def events_of(self, kind, marker=None):
        return [e for e in self._events if e["event_kind"] == kind and (marker is None or e["marker"] == marker)]


def _iso_sentinel(node_id: str) -> str:
    return f"iso:{node_id[-6:]}"


def _heartbeat_at(store: FakeStore, node_id: str, ts: str) -> None:
    """Seed a node registration + a heartbeat/point at ``ts`` for ``node_id`` so reduce_nodes gives it a
    last_heartbeat (and it is not 'unknown')."""
    store.inject("node", "registered", node_id=node_id, payload={"role": "worker", "tailnet_name": "n"}, created_at="2026-07-09T00:00:00+00:00")
    store.inject("heartbeat", "point", node_id=node_id, payload={"ts": ts}, created_at=ts)


def _grant_to(store: FakeStore, scope_key: str, holder: str, *, grantor: str, epoch: int = 1, seq: int = 1) -> None:
    """Seed a coordinator/granted (so ``grantor`` is the coordinator at ``epoch``) + a lease/granted so
    ``holder`` is the reduced active holder of ``scope_key`` (no expiry: a far-future expires_at)."""
    store.inject("coordinator", "granted", node_id=grantor, epoch=epoch, payload={"mode": "roaming"})
    store.inject(
        "lease", "granted", node_id=grantor, scope_key=scope_key, epoch=epoch,
        payload={"grant_seq": seq, "holder": holder, "mode": "authoritative", "ttl_s": 900, "expires_at": "2099-01-01T00:00:00+00:00"},
    )


def _iso_hold(store: FakeStore, scope_key: str, *, epoch: int = 5) -> None:
    """Seed THIS node's iso claim + iso lease on ``scope_key`` (both carry this node's iso_sentinel)."""
    sentinel = _iso_sentinel(store.node_id)
    store.inject("coordinator", "granted", node_id=store.node_id, epoch=epoch, payload={"mode": "roaming", "iso": True, "iso_sentinel": sentinel})
    store.inject(
        "lease", "granted", node_id=store.node_id, scope_key=scope_key, epoch=epoch,
        payload={"grant_seq": 1, "holder": store.node_id, "mode": "advisory", "ttl_s": 900, "expires_at": "2099-01-01T00:00:00+00:00", "iso": True, "iso_sentinel": sentinel},
    )


# --- iso naming + scope discovery ----------------------------------------------------------------

class IsoBranchTest(unittest.TestCase):
    def test_iso_branch_suffix(self) -> None:
        self.assertEqual(reconcile.iso_branch("taskA", "nNODE00000001"), "kz/taskA/nNODE00000001/iso")
        self.assertEqual(reconcile.iso_branch("taskA", "nNODE00000001", worktree=2), "kz/taskA/nNODE00000001/w2/iso")

    def test_iso_branch_rejects_bad_task(self) -> None:
        with self.assertRaises(KaizenDenied) as ctx:
            reconcile.iso_branch("bad/task", "nNODE")
        self.assertEqual(ctx.exception.code, "DENIED_MIRROR_BRANCH_INVALID")


class MyIsoScopesTest(unittest.TestCase):
    def test_finds_only_this_nodes_iso_scopes(self) -> None:
        store = FakeStore("nSurvivor0001")
        _iso_hold(store, "p/alpha")
        _iso_hold(store, "p/beta")
        # A DIFFERENT node's iso hold (its own sentinel) must NOT be picked up.
        other_sentinel = _iso_sentinel("nOther00000002")
        store.inject("lease", "granted", node_id="nOther00000002", scope_key="p/gamma", epoch=5,
                     payload={"grant_seq": 1, "holder": "nOther00000002", "iso": True, "iso_sentinel": other_sentinel})
        # A plain (non-iso) lease is not an iso scope.
        store.inject("lease", "granted", node_id="nSurvivor0001", scope_key="p/delta", epoch=1,
                     payload={"grant_seq": 1, "holder": "nSurvivor0001"})
        self.assertEqual(reconcile._my_iso_scopes(store), ["p/alpha", "p/beta"])


# --- offline_status ------------------------------------------------------------------------------

class OfflineStatusTest(unittest.TestCase):
    def test_unsynced_store_is_pure_local_not_isolated(self) -> None:
        store = FakeStore("nA000000000001", synced=False)
        status = reconcile.offline_status(store)
        self.assertFalse(status["synced"])
        self.assertFalse(status["isolated"])
        self.assertTrue(status["can_work_local"])
        self.assertFalse(status["authoritative_blocked"])

    def test_synced_clean_pull_not_isolated(self) -> None:
        store = FakeStore("nA000000000001", synced=True)  # pull_and_reduce succeeds (no-op)
        status = reconcile.offline_status(store)
        self.assertTrue(status["synced"])
        self.assertFalse(status["isolated"])

    def test_synced_pull_failure_is_isolated_and_authoritative_blocked(self) -> None:
        store = FakeStore("nA000000000001", synced=True)

        def boom(limit=None):  # noqa: ANN001, ARG001
            raise RuntimeError("hub unreachable")

        store.pull_and_reduce = boom  # type: ignore[assignment]
        status = reconcile.offline_status(store)
        self.assertTrue(status["isolated"])
        self.assertTrue(status["authoritative_blocked"])
        self.assertTrue(status["can_work_local"])  # local work never stops

    def test_iso_scopes_surfaced(self) -> None:
        store = FakeStore("nSurvivor0001", synced=False)
        _iso_hold(store, "p/alpha")
        self.assertEqual(reconcile.offline_status(store)["iso_scopes"], ["p/alpha"])


# --- reconcile: pull-failure truthful stop -------------------------------------------------------

class ReconcilePullFailureTest(unittest.TestCase):
    def test_pull_exception_blocks_and_records_nothing(self) -> None:
        store = FakeStore("nSurvivor0001", synced=True)
        _iso_hold(store, "p/alpha")
        before = len(store.coord_events())

        def boom(limit=None):  # noqa: ANN001, ARG001
            raise RuntimeError("partition")

        store.pull_and_reduce = boom  # type: ignore[assignment]
        result = reconcile.reconcile(store)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["pulled"])
        self.assertEqual(result["reason"], "RuntimeError")
        # Nothing appended -- reconcile without a fresh ledger is not reconcile.
        self.assertEqual(len(store.coord_events()), before)

    def test_structured_pull_denial_surfaces_its_code(self) -> None:
        store = FakeStore("nSurvivor0001", synced=True)

        def denied(limit=None):  # noqa: ANN001, ARG001
            raise KaizenDenied("DENIED_EPOCH_REGRESSION", {"required_action": "rebootstrap"}, exit_code=2)

        store.pull_and_reduce = denied  # type: ignore[assignment]
        result = reconcile.reconcile(store)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "DENIED_EPOCH_REGRESSION")


# --- reconcile: AUTO-ADOPT (no overlap) ----------------------------------------------------------

class ReconcileAutoAdoptTest(unittest.TestCase):
    def test_no_overlap_adopts_scope_records_request_and_resolution(self) -> None:
        # EXIT (plan §5.3 M15): "offline work reconciles".
        store = FakeStore("nSurvivor0001", synced=False)  # pure-local lab reconciles its own ledger
        _iso_hold(store, "p/alpha")
        result = reconcile.reconcile(store)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["pulled"], "local")  # unsynced structured no-op
        self.assertEqual(result["adopted"], ["p/alpha"])
        self.assertEqual(result["conflicts"], [])
        # A lease/requested (the adoption request) was appended.
        requests = [e for e in store.coord_events() if e["event_kind"] == "lease" and e["marker"] == "requested" and e["scope_key"] == "p/alpha"]
        self.assertEqual(len(requests), 1)
        # A resolution/recorded {iso_adopted} durable record was appended.
        adopts = [e for e in store.coord_events() if e["event_kind"] == "resolution" and e["marker"] == "recorded" and (e.get("payload") or {}).get("iso_adopted")]
        self.assertEqual(len(adopts), 1)
        self.assertEqual(adopts[0]["payload"]["scope_key"], "p/alpha")
        self.assertEqual(adopts[0]["payload"]["iso_sentinel"], _iso_sentinel("nSurvivor0001"))

    def test_synced_clean_pull_marks_pulled_true(self) -> None:
        store = FakeStore("nSurvivor0001", synced=True)
        _iso_hold(store, "p/alpha")
        result = reconcile.reconcile(store)
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["pulled"] is True)
        self.assertEqual(result["adopted"], ["p/alpha"])

    def test_no_iso_scopes_adopts_nothing(self) -> None:
        store = FakeStore("nSurvivor0001", synced=False)
        result = reconcile.reconcile(store)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["adopted"], [])
        self.assertEqual(result["conflicts"], [])


# --- reconcile: OVERLAP -> DENIED_ISO_CONFLICT (surface, never merge) ----------------------------

class ReconcileOverlapTest(unittest.TestCase):
    def test_other_node_holds_scope_surfaces_conflict_then_refuses(self) -> None:
        # EXIT (plan §5.3 M15): "overlap => surfaced never merged".
        store = FakeStore("nSurvivor0001", synced=False)
        _iso_hold(store, "p/alpha", epoch=5)
        # A DIFFERENT node authoritatively holds p/alpha in the fresh ledger at a WINNING higher epoch.
        _grant_to(store, "p/alpha", "nOther00000002", grantor="nOther00000002", epoch=9, seq=1)
        before = len(store.coord_events())
        with self.assertRaises(KaizenDenied) as ctx:
            reconcile.reconcile(store)
        self.assertEqual(ctx.exception.code, "DENIED_ISO_CONFLICT")
        self.assertEqual(ctx.exception.exit_code, 2)
        self.assertIn("p/alpha", ctx.exception.fields["scopes"])
        self.assertEqual(len(ctx.exception.fields["conflict_event_ids"]), 1)
        # A conflict/detected (the surface-and-confirm fork menu) was recorded PER overlap.
        conflicts = [e for e in store.coord_events() if e["event_kind"] == "conflict" and e["marker"] == "detected"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["payload"]["iso"], True)
        self.assertEqual(conflicts[0]["payload"]["other_node"], "nOther00000002")
        self.assertIn("options", conflicts[0]["payload"])  # the fork menu
        # NEVER adopts on conflict: no lease/requested, no resolution/recorded were appended.
        self.assertFalse([e for e in store.coord_events() if e["event_kind"] == "lease" and e["marker"] == "requested"])
        self.assertFalse([e for e in store.coord_events() if e["event_kind"] == "resolution"])
        # Exactly the conflict event was added (nothing else).
        self.assertEqual(len(store.coord_events()), before + 1)

    def test_repeated_unresolved_overlap_reuses_one_conflict_record(self) -> None:
        store = FakeStore("nSurvivor0001", synced=False)
        _iso_hold(store, "p/alpha", epoch=5)
        _grant_to(store, "p/alpha", "nOther00000002", grantor="nOther00000002", epoch=9)
        conflict_ids: list[str] = []
        for _ in range(2):
            with self.assertRaises(KaizenDenied) as raised:
                reconcile.reconcile(store)
            self.assertEqual(raised.exception.code, "DENIED_ISO_CONFLICT")
            self.assertEqual(len(raised.exception.fields["conflict_event_ids"]), 1)
            conflict_ids.extend(raised.exception.fields["conflict_event_ids"])
        conflicts = [event for event in store.coord_events() if event["event_kind"] == "conflict" and event["marker"] == "detected"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflict_ids, [conflicts[0]["id"], conflicts[0]["id"]])

    def test_multiple_overlaps_each_recorded_then_one_refusal(self) -> None:
        store = FakeStore("nSurvivor0001", synced=False)
        _iso_hold(store, "p/alpha", epoch=5)
        _iso_hold(store, "p/beta", epoch=5)
        _grant_to(store, "p/alpha", "nOther00000002", grantor="nOther00000002", epoch=9)
        _grant_to(store, "p/beta", "nThird000000003", grantor="nThird000000003", epoch=9)
        with self.assertRaises(KaizenDenied) as ctx:
            reconcile.reconcile(store)
        self.assertEqual(ctx.exception.code, "DENIED_ISO_CONFLICT")
        self.assertEqual(sorted(ctx.exception.fields["scopes"]), ["p/alpha", "p/beta"])
        self.assertEqual(len(ctx.exception.fields["conflict_event_ids"]), 2)
        conflicts = [e for e in store.coord_events() if e["event_kind"] == "conflict" and e["marker"] == "detected"]
        self.assertEqual(len(conflicts), 2)

    def test_concurrent_dispatch_after_iso_claim_is_overlap(self) -> None:
        store = FakeStore("nSurvivor0001", synced=False)
        _iso_hold(store, "p/alpha", epoch=5)  # iso events at created_at ~ :NN
        # Another node's dispatch on the scope created LATER (a far-future timestamp) => overlap.
        store.inject("dispatch", "requested", node_id="nOther00000002", scope_key="p/alpha",
                     payload={"dispatch_id": "d1", "target_node": "nX"}, created_at="2099-01-01T00:00:00+00:00")
        with self.assertRaises(KaizenDenied) as ctx:
            reconcile.reconcile(store)
        self.assertEqual(ctx.exception.code, "DENIED_ISO_CONFLICT")


class OverlapRankBlindRegressionTest(unittest.TestCase):
    """M15 audit F1: _detect_overlap must be RANK-BLIND for other-node grants minted during-or-after the
    iso era. Leg (a) (reduced winner) alone misses every case where the iso grant OUT-RANKS the other
    side -- the iso epoch is minted at local-max+1, so it usually does -- and reconcile would silently
    adopt over another node's work."""

    def test_iso_out_ranking_other_grant_still_surfaces_conflict(self) -> None:
        # THE hole: iso hold at epoch 9 WINS the reduction over the other node's later grant at epoch 3.
        # Pre-fix: leg (a) sees self as winner, no handoff/dispatch => silent auto-adopt.
        store = FakeStore("nSurvivor0001", synced=False)
        _iso_hold(store, "p/alpha", epoch=9)
        _grant_to(store, "p/alpha", "nOther00000002", grantor="nOther00000002", epoch=3)  # later clock
        with self.assertRaises(KaizenDenied) as ctx:
            reconcile.reconcile(store)
        self.assertEqual(ctx.exception.code, "DENIED_ISO_CONFLICT")
        conflicts = [e for e in store.coord_events() if e["event_kind"] == "conflict" and e["marker"] == "detected"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["payload"]["other_node"], "nOther00000002")
        self.assertEqual(conflicts[0]["payload"]["overlap_kind"], "lease_granted_concurrent")
        # NEVER adopted.
        self.assertFalse([e for e in store.coord_events() if e["event_kind"] == "resolution"])

    def test_same_epoch_grant_flagged_despite_earlier_clock(self) -> None:
        # Fence leg (a3): a split-brain grant at the SAME epoch as the iso hold whose clock is EARLIER
        # (skewed) than the iso claim. Clock leg (a2) misses it; the epoch fence catches it.
        store = FakeStore("nSurvivor0001", synced=False)
        _iso_hold(store, "p/alpha", epoch=9)  # seq clock 2026-07-09T00:00:NN
        store.inject(
            "lease", "granted", node_id="nOther00000002", scope_key="p/alpha", epoch=9,
            payload={"grant_seq": 1, "holder": "nOther00000002", "expires_at": "2099-01-01T00:00:00+00:00"},
            created_at="2026-07-08T00:00:00+00:00",  # BEFORE the iso claim by clock
        )
        with self.assertRaises(KaizenDenied) as ctx:
            reconcile.reconcile(store)
        self.assertEqual(ctx.exception.code, "DENIED_ISO_CONFLICT")

    def test_concurrent_grant_later_released_still_surfaces(self) -> None:
        # A grant taken while we were isolated and RELEASED before we reconciled was still concurrent
        # work -- surfaced, never silently absorbed.
        store = FakeStore("nSurvivor0001", synced=False)
        _iso_hold(store, "p/alpha", epoch=5)
        store.inject("lease", "granted", node_id="nOther00000002", scope_key="p/alpha", epoch=1,
                     payload={"grant_seq": 1, "holder": "nOther00000002"}, created_at="2099-01-01T00:00:00+00:00")
        store.inject("lease", "released", node_id="nOther00000002", scope_key="p/alpha", epoch=1,
                     payload={"grant_seq": 1}, created_at="2099-01-01T00:00:01+00:00")
        with self.assertRaises(KaizenDenied) as ctx:
            reconcile.reconcile(store)
        self.assertEqual(ctx.exception.code, "DENIED_ISO_CONFLICT")

    def test_pre_claim_lower_epoch_hold_is_advisory_coexistence(self) -> None:
        # Deliberate boundary: an other-node hold BOTH pre-claim by clock AND below the iso epoch was
        # VISIBLE before the iso claim -- M10a advisory coexistence (branch-per-node), not a partition
        # race. Reconcile adopts.
        store = FakeStore("nSurvivor0001", synced=False)
        store.inject(  # other node's old, still-held advisory grant -- injected FIRST (earlier clock)
            "lease", "granted", node_id="nOther00000002", scope_key="p/alpha", epoch=1,
            payload={"grant_seq": 1, "holder": "nOther00000002", "expires_at": "2099-01-01T00:00:00+00:00"},
        )
        _iso_hold(store, "p/alpha", epoch=5)
        result = reconcile.reconcile(store)
        self.assertEqual(result["adopted"], ["p/alpha"])
        self.assertEqual(result["conflicts"], [])


@unittest.skipIf(GIT is None, "git not available")
class ReconcileOverlapTreeUntouchedTest(unittest.TestCase):
    def test_overlap_with_git_runner_never_touches_tree(self) -> None:
        # With a git_runner + hub attached, an overlap still records-then-refuses and NEVER merges: the
        # scratch tree/HEAD is unchanged.
        repo = _init_scratch_repo(self, "kz-recon-repo-")
        head_before = _git_checked("-C", repo, "rev-parse", "HEAD").stdout.strip()
        # A real bare hub cloned from repo so the reconcile git-fetch leg SUCCEEDS (the fetch runs before
        # overlap detection); the point of this test is that overlap still record-then-refuses without
        # touching the tree.
        hub = tempfile.mkdtemp(prefix="kz-recon-ovl-hub-")
        self.addCleanup(shutil.rmtree, hub, ignore_errors=True)
        shutil.rmtree(hub)
        _git_checked("clone", "--bare", "-q", repo, hub)

        store = FakeStore("nSurvivor0001", synced=False)
        _iso_hold(store, "p/alpha", epoch=5)
        _grant_to(store, "p/alpha", "nOther00000002", grantor="nOther00000002", epoch=9)
        runner = coordination._default_git_runner(repo)
        with self.assertRaises(KaizenDenied) as ctx:
            reconcile.reconcile(store, runner, hub_remote=hub)
        self.assertEqual(ctx.exception.code, "DENIED_ISO_CONFLICT")
        head_after = _git_checked("-C", repo, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(head_before, head_after)  # tree/HEAD untouched
        status = _git_checked("-C", repo, "status", "--porcelain").stdout
        self.assertEqual(status.strip(), "")  # clean


# --- reconcile: PUBLISH (policy-gated) -----------------------------------------------------------

@unittest.skipIf(GIT is None, "git not available")
class ReconcilePublishTest(unittest.TestCase):
    """The reconcile-level publish legs run against a LIVE hub (a bare clone of repo, so the fetch leg --
    which runs BEFORE publish -- succeeds). The push_failed / skipped-branch branches are covered at the
    _publish_iso_branches unit level below (a dead hub blocks at fetch, so it cannot reach publish
    through reconcile())."""

    def _repo_and_hub(self, branch: str) -> tuple[str, str]:
        repo = _init_scratch_repo(self, "kz-recon-pub-", branch=branch)
        # A LIVE bare hub cloned from repo -> git fetch <hub> succeeds. Delete the iso branch off the hub
        # so the allow_publish push is a real new-ref push (confirmable), not a no-op.
        hub = tempfile.mkdtemp(prefix="kz-recon-hub-")
        self.addCleanup(shutil.rmtree, hub, ignore_errors=True)
        shutil.rmtree(hub)
        _git_checked("clone", "--bare", "-q", repo, hub)
        _git_checked("-C", hub, "branch", "-D", branch)
        return repo, hub

    def test_default_would_publish_no_push(self) -> None:
        branch = reconcile.iso_branch("alpha", "nSurvivor0001")
        repo, hub = self._repo_and_hub(branch)
        store = FakeStore("nSurvivor0001", synced=False)
        # iso hold on a scope whose branch name matches the branch we adopt (adopted scope == branch).
        _iso_hold(store, branch, epoch=5)
        runner = coordination._default_git_runner(repo)
        result = reconcile.reconcile(store, runner, hub_remote=hub, allow_publish=False)
        self.assertEqual(result["status"], "OK")
        self.assertNotIn("published", result)
        self.assertEqual(result["would_publish"], [{"branch": branch, "reason": "publish not enabled (owner-gated)"}])
        # The iso branch is NOT on the hub (nothing pushed).
        ls = _git_checked("ls-remote", hub, branch).stdout.strip()
        self.assertEqual(ls, "")

    def test_allow_publish_pushes_and_confirms(self) -> None:
        branch = reconcile.iso_branch("alpha", "nSurvivor0001")
        repo, hub = self._repo_and_hub(branch)
        store = FakeStore("nSurvivor0001", synced=False)
        _iso_hold(store, branch, epoch=5)
        runner = coordination._default_git_runner(repo)
        result = reconcile.reconcile(store, runner, hub_remote=hub, allow_publish=True)
        self.assertEqual(result.get("published"), [branch])
        self.assertNotIn("would_publish", result)
        # The branch now exists on the hub (confirmed by ls-remote -- "published" means confirmed).
        ls = _git_checked("ls-remote", hub, branch).stdout.strip()
        self.assertIn(branch, ls)


class ReconcileIdempotencyRegressionTest(unittest.TestCase):
    def test_second_reconcile_does_not_readopt(self) -> None:
        # Audit regression: iso events persist forever in the append-only ledger, so WITHOUT the
        # adopted-scopes guard a second reconcile would re-adopt (duplicate lease/requested +
        # resolution/recorded). The guard makes reconcile idempotent per scope.
        store = FakeStore("nSurvivor0001", synced=False)
        _iso_hold(store, "p/alpha")
        first = reconcile.reconcile(store)
        self.assertEqual(first["adopted"], ["p/alpha"])
        before = len(store.coord_events())
        second = reconcile.reconcile(store)
        self.assertEqual(second["status"], "OK")
        self.assertEqual(second["adopted"], [])  # already adopted -> not re-adopted
        self.assertEqual(len(store.coord_events()), before)  # zero new events


@unittest.skipIf(GIT is None, "git not available")
class PublishScopeKeyDerivationRegressionTest(ReconcilePublishTest):
    def test_project_prefixed_scope_publishes_the_stripped_branch(self) -> None:
        # Audit regression: a PRODUCTION scope is <project_id>/<branch> (mirror.branch_scope_key), so the
        # branch name is the project-stripped tail -- publish must derive it, not rev-parse the raw scope.
        branch = reconcile.iso_branch("alpha", "nSurvivor0001")
        repo, hub = self._repo_and_hub(branch)
        store = FakeStore("nSurvivor0001", synced=False)
        scope = f"projdeadbeef1234/{branch}"  # branch_scope_key shape
        _iso_hold(store, scope, epoch=5)
        runner = coordination._default_git_runner(repo)
        result = reconcile.reconcile(store, runner, hub_remote=hub, allow_publish=True)
        self.assertEqual(result.get("published"), [branch])  # the STRIPPED tail was pushed + confirmed
        ls = _git_checked("ls-remote", hub, branch).stdout.strip()
        self.assertIn(branch, ls)


@unittest.skipIf(GIT is None, "git not available")
class PublishIsoBranchesUnitTest(unittest.TestCase):
    """_publish_iso_branches directly (the would_publish/push_failed/skip logic the reconcile publish leg
    delegates to)."""

    def _repo_with_branch(self, branch: str) -> str:
        return _init_scratch_repo(self, "kz-recon-pub-u-", branch=branch)

    def test_push_to_dead_hub_is_push_failed(self) -> None:
        branch = reconcile.iso_branch("alpha", "nX00000000001")
        repo = self._repo_with_branch(branch)
        dead_hub = str(Path(tempfile.mkdtemp(prefix="kz-recon-dead-")) / "no-such-bare")  # nonexistent
        self.addCleanup(shutil.rmtree, str(Path(dead_hub).parent), ignore_errors=True)
        runner = coordination._default_git_runner(repo)
        published, would = reconcile._publish_iso_branches(runner, dead_hub, [branch], allow_publish=True)
        self.assertEqual(published, [])
        self.assertEqual(would, [{"branch": branch, "push_failed": True}])

    def test_nonexistent_branch_is_skipped(self) -> None:
        repo = self._repo_with_branch("kz/other/nX/iso")  # a DIFFERENT branch exists
        runner = coordination._default_git_runner(repo)
        published, would = reconcile._publish_iso_branches(runner, "irrelevant-hub", ["p/alpha"], allow_publish=False)
        # "p/alpha" has no matching local branch -> skipped entirely (nothing to publish).
        self.assertEqual(published, [])
        self.assertEqual(would, [])


# --- sweep: stale node reclamation ---------------------------------------------------------------

class SweepStaleNodeTest(unittest.TestCase):
    def test_stale_node_lease_reclaimed_and_dispatch_canceled(self) -> None:
        # EXIT (plan §5.3 M15): "dead node's runs finalized by survivor" -- the LEDGER-visible state (its
        # lease + its targeted dispatch) is finalized by the survivor.
        store = FakeStore("nSurvivor0001")
        _heartbeat_at(store, "nDead00000002", _OLD)
        _heartbeat_at(store, "nSurvivor0001", _NOW)  # survivor is fresh
        # The dead node HOLDS a lease.
        _grant_to(store, "p/alpha", "nDead00000002", grantor="nSurvivor0001", epoch=1)
        # A non-terminal dispatch TARGETS the dead node (fleet.db remote_dispatches row + coord event).
        store.seed_dispatch("d1", target_node="nDead00000002", scope_key="p/alpha")

        result = reconcile.sweep_stale_nodes(store, now=_NOW)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(result["stale"]), 1)
        entry = result["stale"][0]
        self.assertEqual(entry["node"], "nDead00000002")
        self.assertEqual(entry["leases_reclaimed"], ["p/alpha"])
        self.assertEqual(entry["dispatches_canceled"], ["d1"])
        self.assertGreater(entry["age_s"], 0)
        # The lease/expired frees the scope: reduce_lease now shows it FREE.
        leases = coordination.current_leases(store, now=_NOW)
        self.assertEqual(leases["p/alpha"]["state"], "free")
        # The lease/expired carries the reclamation provenance + reuses the sweep_expired grant_seq shape.
        expired = [e for e in store.coord_events() if e["event_kind"] == "lease" and e["marker"] == "expired"]
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["payload"]["reclaimed_from"], "nDead00000002")
        self.assertEqual(expired[0]["payload"]["reason"], "heartbeat-stale")
        self.assertIn("grant_seq", expired[0]["payload"])
        # The dispatch is canceled (terminal).
        canceled = [e for e in store.coord_events() if e["event_kind"] == "dispatch" and e["marker"] == "canceled"]
        self.assertEqual(len(canceled), 1)


class SweepFalseOrphanGuardTest(unittest.TestCase):
    """§9 row 10: a transient disconnect (age < threshold, or inside the skew margin) must NOT be swept."""

    def test_age_below_stale_after_untouched(self) -> None:
        store = FakeStore("nSurvivor0001")
        # heartbeat 100s ago; stale_after=900 -> not stale.
        recent = "2026-07-09T11:58:20+00:00"  # 100s before _NOW
        _heartbeat_at(store, "nMaybe00000002", recent)
        _grant_to(store, "p/alpha", "nMaybe00000002", grantor="nSurvivor0001", epoch=1)
        result = reconcile.sweep_stale_nodes(store, now=_NOW, stale_after_s=900, skew_margin_s=120)
        self.assertEqual(result["stale"], [])
        self.assertFalse([e for e in store.coord_events() if e["event_kind"] == "lease" and e["marker"] == "expired"])

    def test_age_inside_skew_margin_untouched(self) -> None:
        # THE MARGIN CASE (explicit): age is PAST stale_after but INSIDE stale_after + skew_margin -> NOT
        # stale (the skew tolerance).
        store = FakeStore("nSurvivor0001")
        # age = 960s: > stale_after(900) but < 900+120=1020 -> inside the margin, untouched.
        margin_hb = "2026-07-09T11:44:00+00:00"  # 960s before _NOW
        _heartbeat_at(store, "nSkew00000002", margin_hb)
        _grant_to(store, "p/alpha", "nSkew00000002", grantor="nSurvivor0001", epoch=1)
        result = reconcile.sweep_stale_nodes(store, now=_NOW, stale_after_s=900, skew_margin_s=120)
        self.assertEqual(result["stale"], [])

    def test_age_past_margin_is_stale(self) -> None:
        # Just past the margin (age = 1080s > 1020) -> stale (the boundary is exclusive-above).
        store = FakeStore("nSurvivor0001")
        past_hb = "2026-07-09T11:42:00+00:00"  # 1080s before _NOW
        _heartbeat_at(store, "nGone00000002", past_hb)
        _grant_to(store, "p/alpha", "nGone00000002", grantor="nSurvivor0001", epoch=1)
        result = reconcile.sweep_stale_nodes(store, now=_NOW, stale_after_s=900, skew_margin_s=120)
        self.assertEqual([s["node"] for s in result["stale"]], ["nGone00000002"])

    def test_self_never_swept(self) -> None:
        store = FakeStore("nSurvivor0001")
        _heartbeat_at(store, "nSurvivor0001", _OLD)  # even a STALE self is not swept (fail-safe)
        _grant_to(store, "p/self", "nSurvivor0001", grantor="nSurvivor0001", epoch=1)
        result = reconcile.sweep_stale_nodes(store, now=_NOW, stale_after_s=900, skew_margin_s=120)
        self.assertEqual(result["stale"], [])

    def test_no_heartbeat_ever_untouched(self) -> None:
        # A node registered but NEVER heartbeat -> unknown, not dead -> untouched.
        store = FakeStore("nSurvivor0001")
        store.inject("node", "registered", node_id="nSilent00000002", payload={"role": "worker", "tailnet_name": "n"}, created_at=_OLD)
        _grant_to(store, "p/alpha", "nSilent00000002", grantor="nSurvivor0001", epoch=1)
        result = reconcile.sweep_stale_nodes(store, now=_NOW, stale_after_s=900, skew_margin_s=120)
        self.assertEqual(result["stale"], [])
        self.assertFalse([e for e in store.coord_events() if e["event_kind"] == "lease" and e["marker"] == "expired"])

    def test_retired_node_untouched(self) -> None:
        store = FakeStore("nSurvivor0001")
        _heartbeat_at(store, "nRetired00002", _OLD)
        store.inject("node", "retired", node_id="nRetired00002", created_at=_OLD)  # already terminal
        _grant_to(store, "p/alpha", "nRetired00002", grantor="nSurvivor0001", epoch=1)
        result = reconcile.sweep_stale_nodes(store, now=_NOW, stale_after_s=900, skew_margin_s=120)
        self.assertEqual(result["stale"], [])


class SweepIdempotentTest(unittest.TestCase):
    def test_second_sweep_appends_nothing(self) -> None:
        store = FakeStore("nSurvivor0001")
        _heartbeat_at(store, "nDead00000002", _OLD)
        _grant_to(store, "p/alpha", "nDead00000002", grantor="nSurvivor0001", epoch=1)
        store.seed_dispatch("d1", target_node="nDead00000002", scope_key="p/alpha")
        reconcile.sweep_stale_nodes(store, now=_NOW)
        count_after_first = len(store.coord_events())
        # A second sweep: the lease is now free, the dispatch terminal -> nothing new appended.
        result2 = reconcile.sweep_stale_nodes(store, now=_NOW)
        self.assertEqual(len(store.coord_events()), count_after_first)
        # The node still reports as stale, but with nothing left to reclaim.
        entry = [s for s in result2["stale"] if s["node"] == "nDead00000002"][0]
        self.assertEqual(entry["leases_reclaimed"], [])
        self.assertEqual(entry["dispatches_canceled"], [])


class SweepStaleCoordinatorTest(unittest.TestCase):
    def test_stale_coordinator_records_divergence_but_role_not_displaced(self) -> None:
        store = FakeStore("nSurvivor0001")
        _heartbeat_at(store, "nCoord00000002", _OLD)
        _heartbeat_at(store, "nSurvivor0001", _NOW)
        # The stale node is the reduced coordinator.
        store.inject("coordinator", "claimed", node_id="nCoord00000002", epoch=3, payload={"mode": "roaming"})
        store.inject("coordinator", "granted", node_id="nCoord00000002", epoch=3, payload={"mode": "roaming"})
        coord_before = reducers.reduce_coordinator(store.coord_events())
        self.assertEqual(coord_before["holder"], "nCoord00000002")

        result = reconcile.sweep_stale_nodes(store, now=_NOW)
        entry = [s for s in result["stale"] if s["node"] == "nCoord00000002"][0]
        self.assertTrue(entry["was_coordinator"])
        # A STALE_COORDINATOR divergence was recorded...
        divs = [e for e in store.coord_events() if e["event_kind"] == "divergence" and (e.get("payload") or {}).get("would_block") == "STALE_COORDINATOR"]
        self.assertEqual(len(divs), 1)
        self.assertEqual(divs[0]["payload"]["node"], "nCoord00000002")
        # ...but the role was NEVER auto-seized: the reduced coordinator is unchanged (still the stale node).
        coord_after = reducers.reduce_coordinator(store.coord_events())
        self.assertEqual(coord_after["holder"], "nCoord00000002")
        self.assertEqual(coord_after["epoch"], 3)

    def test_stale_coordinator_divergence_deduped_on_resweep(self) -> None:
        store = FakeStore("nSurvivor0001")
        _heartbeat_at(store, "nCoord00000002", _OLD)
        store.inject("coordinator", "claimed", node_id="nCoord00000002", epoch=3, payload={"mode": "roaming"})
        store.inject("coordinator", "granted", node_id="nCoord00000002", epoch=3, payload={"mode": "roaming"})
        reconcile.sweep_stale_nodes(store, now=_NOW)
        divs1 = [e for e in store.coord_events() if (e.get("payload") or {}).get("would_block") == "STALE_COORDINATOR"]
        reconcile.sweep_stale_nodes(store, now=_NOW)
        divs2 = [e for e in store.coord_events() if (e.get("payload") or {}).get("would_block") == "STALE_COORDINATOR"]
        self.assertEqual(len(divs1), 1)
        self.assertEqual(len(divs2), 1)  # deduped: no second divergence at the same node+epoch


class SweepKaizenDbBoundaryTest(unittest.TestCase):
    def test_sweep_touches_only_fleet_db_never_a_kaizen_db_table(self) -> None:
        # STRUCTURAL PROOF (§3.2 boundary): the sweep finalizes SHARED (fleet.db) coordination state only.
        # The FakeStore models ONLY fleet.db (coord_events + remote_dispatches); it has NO kaizen.db handle
        # and NO agent_runs/approval_requests table. A full sweep -- reclaiming a lease + canceling a
        # targeted dispatch + recording a stale-coordinator divergence -- completes over it, and every
        # _read_all the sweep issued hit ONLY remote_dispatches (never agent_runs / approval_requests /
        # any kaizen.db table). A dead node's agent_runs live in ITS un-synced kaizen.db, out of reach.
        store = FakeStore("nSurvivor0001")
        _heartbeat_at(store, "nDead00000002", _OLD)
        _grant_to(store, "p/alpha", "nDead00000002", grantor="nSurvivor0001", epoch=1)
        store.seed_dispatch("d1", target_node="nDead00000002", scope_key="p/alpha")
        # Make the dead node the coordinator too, so all three reclamation legs fire.
        store.inject("coordinator", "granted", node_id="nDead00000002", epoch=2, payload={"mode": "roaming"})
        result = reconcile.sweep_stale_nodes(store, now=_NOW)
        self.assertEqual(result["status"], "OK")
        self.assertEqual([s["node"] for s in result["stale"]], ["nDead00000002"])
        entry = result["stale"][0]
        self.assertEqual(entry["leases_reclaimed"], ["p/alpha"])
        self.assertEqual(entry["dispatches_canceled"], ["d1"])
        self.assertTrue(entry["was_coordinator"])
        # Every DB read the sweep made hit ONLY fleet.db tables (nodes, remote_dispatches) -- NEVER a
        # kaizen.db table (agent_runs / approval_requests / agent_sessions). A dead node's agent_runs are
        # structurally out of reach (its own un-synced kaizen.db).
        self.assertTrue(store.reads)  # the sweep did read fleet.db
        for sql in store.reads:
            self.assertTrue(("remote_dispatches" in sql) or ("FROM nodes" in sql), sql)
            self.assertNotIn("agent_runs", sql)
            self.assertNotIn("approval_requests", sql)
            self.assertNotIn("agent_sessions", sql)


# --- sweep over a REAL scratch FleetStore (nodes-table last_heartbeat leg) ------------------------

class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-recon-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def store(self, node_id: str, *, db_path: Path | None = None) -> FleetStore:
        s = FleetStore(db_path=db_path or (self.root / "fleet.db"), node={"node_id": node_id, "created_at": "2026-01-01T00:00:00+00:00"})
        self.addCleanup(s.close)
        return s


class SweepRealStoreTest(_StoreCase):
    def test_stale_node_reclaimed_via_real_reduced_heartbeat(self) -> None:
        # A REAL FleetStore: the survivor grants a lease to a second node identity, seeds that node an OLD
        # heartbeat via the coord ledger (reduce_nodes reads it), and sweeps it stale. Uses now= far in
        # the future so the seeded heartbeat is unambiguously stale.
        survivor = self.store("nSurvivor00001")
        coordination.claim_coordinator(survivor, summary="claim")
        # A far-future TTL so the lease is still HELD at now=2030 (staleness is about the HEARTBEAT, not
        # the lease expiry -- a huge ttl decouples the two so the reclaim path sees a held lease).
        coordination.grant_lease(survivor, "p/alpha", "nDead000000002", ttl_s=10**9, summary="grant")
        # Seed the dead node's registration + an OLD heartbeat (a distinct identity over the same db file).
        dead = FleetStore(db_path=self.root / "fleet.db", node={"node_id": "nDead000000002", "created_at": "t"})
        self.addCleanup(dead.close)
        dead.append_coord_event("node", "registered", summary="Dead node.", payload={"role": "worker", "tailnet_name": "n"})
        dead.append_coord_event("heartbeat", "point", summary="Old heartbeat.", payload={"ts": _OLD})

        result = reconcile.sweep_stale_nodes(survivor, now="2030-01-01T00:00:00+00:00", stale_after_s=900, skew_margin_s=120)
        stale_nodes = [s["node"] for s in result["stale"]]
        self.assertIn("nDead000000002", stale_nodes)
        entry = [s for s in result["stale"] if s["node"] == "nDead000000002"][0]
        self.assertEqual(entry["leases_reclaimed"], ["p/alpha"])
        # The scope reduces free after the reclamation.
        self.assertEqual(coordination.current_leases(survivor, now="2030-01-01T00:00:00+00:00")["p/alpha"]["state"], "free")


class IsoShippedFlowTest(_StoreCase):
    """M15 audit F2: the SHIPPED D4 {"iso": true} claim -> D5 self-grant flow must PRODUCE the iso-marked
    scope-keyed events reconcile discovers. Pre-fix, only coordinator events (no scope_key) carried the
    sentinel, so production D9 could never find an iso scope."""

    SCOPE = "p/kz/taskA/nIso000000001/iso"

    def test_iso_self_grant_via_shipped_flow_is_discoverable_and_adopts(self) -> None:
        s = self.store("nIso000000001")
        coordination.claim_coordinator(s, isolated=True, summary="Iso role claim.")
        coordination.grant_lease(s, self.SCOPE, "nIso000000001", ttl_s=10**9, summary="Iso self grant.")
        self.assertEqual(reconcile._my_iso_scopes(s), [self.SCOPE])
        result = reconcile.reconcile(s)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["adopted"], [self.SCOPE])

    def test_grant_to_other_node_under_iso_coordinator_not_stamped(self) -> None:
        s = self.store("nIso000000001")
        coordination.claim_coordinator(s, isolated=True, summary="Iso role claim.")
        coordination.grant_lease(s, self.SCOPE, "nPeer00000002", ttl_s=900, summary="Grant to peer.")
        self.assertEqual(reconcile._my_iso_scopes(s), [])

    def test_grant_under_normal_coordinator_not_stamped(self) -> None:
        s = self.store("nIso000000001")
        coordination.claim_coordinator(s, summary="Normal role claim.")
        coordination.grant_lease(s, self.SCOPE, "nIso000000001", ttl_s=900, summary="Normal self grant.")
        self.assertEqual(reconcile._my_iso_scopes(s), [])

    def test_renew_under_iso_coordinator_keeps_scope_discoverable(self) -> None:
        s = self.store("nIso000000001")
        coordination.claim_coordinator(s, isolated=True, summary="Iso role claim.")
        coordination.grant_lease(s, self.SCOPE, "nIso000000001", ttl_s=10**9, summary="Iso self grant.")
        coordination.renew_lease(s, self.SCOPE, ttl_s=10**9, summary="Iso renew.")
        self.assertEqual(reconcile._my_iso_scopes(s), [self.SCOPE])
        # The WINNING (renewal, grant_seq 2) grant itself carries the sentinel.
        grants = [e for e in s.coord_events() if e["event_kind"] == "lease" and e["marker"] == "granted"]
        winning = max(grants, key=lambda e: int((e.get("payload") or {}).get("grant_seq") or 0))
        self.assertEqual(int(winning["payload"]["grant_seq"]), 2)
        self.assertTrue(winning["payload"]["iso"])

    def test_coordinator_going_isolated_reclaims_into_iso_epoch(self) -> None:
        # Case (i): the roaming coordinator IS the node that loses the hub (default holder = interacted
        # node). Re-claiming itself isolated mints a FRESH iso epoch so subsequent self-grants stamp.
        s = self.store("nIso000000001")
        first = coordination.claim_coordinator(s, summary="Normal claim.")
        r = coordination.claim_coordinator(s, isolated=True, summary="Going isolated.")
        self.assertTrue(r["iso"])
        self.assertGreater(r["epoch"], first["epoch"])
        coordination.grant_lease(s, self.SCOPE, "nIso000000001", ttl_s=10**9, summary="Iso self grant.")
        self.assertEqual(reconcile._my_iso_scopes(s), [self.SCOPE])


class IsolatedRoleFallbackTest(_StoreCase):
    """M15 audit F3: §B.3 "isolated node ASSUMES role under an iso: epoch sentinel" / v8 "isolated
    fallback". Pre-fix, a contested-roaming claim returned RECORDED without granting, so a node
    partitioned away from the coordinator could never mint fresh iso scopes (grant_lease ⇒
    DENIED_NOT_COORDINATOR). Verified-isolated ⇒ assume; reachable hub ⇒ DENIED_NOT_ISOLATED; pinned ⇒
    DENIED_COORD_DIVERGED even isolated."""

    def test_isolated_claim_assumes_role_over_roaming_holder(self) -> None:
        s = self.store("nIso000000001")
        away = FleetStore(db_path=self.root / "fleet.db", node={"node_id": "nAway00000002", "created_at": "t"})
        self.addCleanup(away.close)
        coordination.claim_coordinator(away, summary="Away node claims first.")
        r = coordination.claim_coordinator(s, isolated=True, summary="Iso fallback claim.")
        self.assertEqual(r["status"], "OK")
        self.assertTrue(r["contested"])
        self.assertTrue(r["iso"])
        self.assertEqual(r["holder"], "nIso000000001")
        coord = coordination.current_coordinator(s)
        self.assertEqual(coord["holder"], "nIso000000001")
        self.assertTrue(coord.get("iso"))
        # The contest is a durable audit record.
        assumed = [
            e for e in s.coord_events()
            if e["event_kind"] == "conflict" and (e.get("payload") or {}).get("iso_assumed")
        ]
        self.assertEqual(len(assumed), 1)
        self.assertEqual(assumed[0]["payload"]["current_holder"], "nAway00000002")
        # And the point of the fallback: fresh iso scope grants now work + are discoverable.
        coordination.grant_lease(s, "p/kz/t/nIso/iso", "nIso000000001", ttl_s=10**9, summary="Iso grant.")
        self.assertEqual(reconcile._my_iso_scopes(s), ["p/kz/t/nIso/iso"])

    def test_isolated_claim_on_reachable_hub_refused(self) -> None:
        # A SYNCED store whose pull SUCCEEDS is not isolated: the iso claim must not mint iso authority.
        store = FakeStore("nIso000000001", synced=True)  # default pull_and_reduce succeeds
        store.inject("coordinator", "granted", node_id="nAway00000002", epoch=1, payload={"mode": "roaming"})
        with self.assertRaises(KaizenDenied) as ctx:
            coordination.claim_coordinator(store, isolated=True, summary="Not actually isolated.")
        self.assertEqual(ctx.exception.code, "DENIED_NOT_ISOLATED")

    def test_isolated_claim_verified_by_failing_pull_assumes(self) -> None:
        store = FakeStore("nIso000000001", synced=True)
        store.inject("coordinator", "granted", node_id="nAway00000002", epoch=1, payload={"mode": "roaming"})

        def boom(limit=None):  # noqa: ANN001, ARG001
            raise RuntimeError("partition")

        store.pull_and_reduce = boom  # type: ignore[assignment]
        r = coordination.claim_coordinator(store, isolated=True, summary="Verified isolated.")
        self.assertEqual(r["status"], "OK")
        self.assertTrue(r["iso"])

    def test_isolated_claim_against_pinned_holder_refused(self) -> None:
        store = FakeStore("nIso000000001", synced=False)  # probe skips; the PIN is the refusal
        store.inject("coordinator", "granted", node_id="nAway00000002", epoch=1, payload={"mode": "pinned"})
        with self.assertRaises(KaizenDenied) as ctx:
            coordination.claim_coordinator(store, isolated=True, summary="Iso vs pin.")
        self.assertEqual(ctx.exception.code, "DENIED_COORD_DIVERGED")


# --- D9 via the real CLI (op-coverage sees D9) ---------------------------------------------------

_ACTIVE = {"KAIZEN_DIST_MODE": "active"}
_OBSERVE = {"KAIZEN_DIST_MODE": "observe"}


class D9CliTest(IsolatedDBTest):
    """D9 through the real CLI (IsolatedDBTest). Satisfies op-coverage (self.kz("D9", ...)); the engine
    semantics are proven in the in-process legs above."""

    def test_status_round_trips(self) -> None:
        rc, p = self.kz("D9", "--action", "status", "--summary", "Status.", env=_OBSERVE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        self.assertIn("can_work_local", p)
        self.assertTrue(p["can_work_local"])
        self.assertIn("iso_scopes", p)

    def test_sweep_round_trips(self) -> None:
        rc, p = self.kz("D9", "--action", "sweep", "--summary", "Sweep.", env=_OBSERVE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        self.assertIn("stale", p)
        self.assertIn("untouched", p)

    def test_reconcile_default_action_round_trips(self) -> None:
        # Default action is reconcile; a fresh plane has no iso scopes -> adopts nothing, no conflict.
        rc, p = self.kz("D9", "--summary", "Reconcile.", env=_OBSERVE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        self.assertEqual(p.get("adopted"), [], p)
        self.assertEqual(p.get("conflicts"), [], p)

    def test_off_mode_inert(self) -> None:
        rc, p = self.kz("D9", "--action", "status")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_DIST_MODE_OFF", p)

    def test_off_mode_creates_no_fleet_db(self) -> None:
        self.kz("D9", "--action", "sweep")
        self.assertFalse((self.root / "AI" / "db" / "fleet.db").exists())

    def test_bad_action_refused(self) -> None:
        rc, p = self.kz("D9", "--action", "bogus", "--summary", "x.", env=_ACTIVE)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_RECONCILE_ACTION_REQUIRED", p)


# --- supervisor loopback (scratch plane subprocess) ----------------------------------------------

_SUP_PREAMBLE = r"""
import json, sys
from kaizen_components import db
from kaizen_components.orchestration.supervisor import Supervisor
from kaizen_components.denials import KaizenDenied

out = None
try:
    exec(BODY)
    print("RESULT " + json.dumps(out))
except KaizenDenied as e:
    print("RESULT " + json.dumps({"_denied": e.code, "_exit": e.exit_code}))
"""


class _SupSubprocess(unittest.TestCase):
    """Drives a REAL in-process Supervisor pinned to a fresh scratch KAIZEN_REPO_ROOT in a subprocess
    (test_remote_dispatch convention). Distribution is on so the daemon holds a fleet.db handle."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-recon-sup-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.assertEqual(kaizen(self.root, "K1")[0], 0)

    def drive(self, body: str, *, env: dict | None = None) -> dict:
        script = "BODY = " + repr(body) + "\n" + _SUP_PREAMBLE
        full_env = dict(os.environ)
        full_env["KAIZEN_REPO_ROOT"] = str(self.root)
        full_env["KAIZEN_DIST_MODE"] = "observe"
        if env:
            full_env.update(env)
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=str(REPO_ROOT), env=full_env)
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        self.fail(f"no RESULT.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


class SupervisorReconcileLoopbackTest(_SupSubprocess):
    def test_status_routes_through_daemon(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "resp = sup._handle_control({'op': 'fleet/reconcile', 'args': {'action': 'status'}})\n"
            "sup.shutdown()\n"
            "out = {'resp': resp}\n"
        )
        self.assertEqual(out["resp"]["status"], "OK")
        self.assertTrue(out["resp"]["can_work_local"])

    def test_sweep_routes_through_daemon(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "resp = sup._handle_control({'op': 'fleet/reconcile', 'args': {'action': 'sweep'}})\n"
            "sup.shutdown()\n"
            "out = {'resp': resp}\n"
        )
        self.assertEqual(out["resp"]["status"], "OK")
        self.assertIn("stale", out["resp"])

    def test_reconcile_adopts_iso_scope_over_the_daemon_store(self) -> None:
        # Seed an iso hold on the daemon's fleet.db (via the daemon's node identity + sentinel), then
        # reconcile over loopback: the scope is adopted (no overlap on a single-node plane).
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "from kaizen_components.fleet import coordination\n"
            "store = sup._fleet\n"
            "node = store.node_id\n"
            "sentinel = coordination._iso_sentinel(store)\n"
            "coordination.claim_coordinator(store, isolated=True, summary='iso claim')\n"
            "store.append_coord_event('lease', 'granted', summary='iso lease', scope_key='p/iso', epoch=9, payload={'grant_seq': 1, 'holder': node, 'mode': 'advisory', 'ttl_s': 900, 'expires_at': '2099-01-01T00:00:00+00:00', 'iso': True, 'iso_sentinel': sentinel})\n"
            "resp = sup._handle_control({'op': 'fleet/reconcile', 'args': {'action': 'reconcile'}})\n"
            "sup.shutdown()\n"
            "out = {'resp': resp}\n"
        )
        self.assertEqual(out["resp"]["status"], "OK")
        self.assertEqual(out["resp"]["adopted"], ["p/iso"])
        self.assertEqual(out["resp"]["conflicts"], [])

    def test_bad_action_refused_over_loopback(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "resp = sup._handle_control({'op': 'fleet/reconcile', 'args': {'action': 'bogus'}})\n"
            "sup.shutdown()\n"
            "out = {'resp': resp}\n"
        )
        self.assertEqual(out["resp"]["code"], "DENIED_RECONCILE_ACTION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
