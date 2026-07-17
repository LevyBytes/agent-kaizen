"""The §6 fleet CI invariant: core coordinator, lease, and node reducers converge regardless of order/dupes.

Feeds interleaved / reordered / duplicated coord_events from two synthetic node_ids into those reducers
and asserts IDENTICAL projections every time -- the F1 exit criterion ("two replicas converge on an
identical fleet digest") proven at the reducer level, in the single-process harness, with zero network.
Also pins MAX-EPOCH-WINS including a hub-rewind shape (a later-created lower-epoch batch must NOT
displace a higher epoch already seen) and the deterministic tiebreaks.

Pure-unit legs: they import the reducer module directly (no CLI, no DB, no isolated plane needed).
"""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kaizen_components.fleet import reducers  # noqa: E402


def _reorder_and_duplicate(events, seed):
    """A reordered copy of ``events`` with a random subset duplicated -- the adversarial input the
    sync path can produce (out-of-order replay + at-least-once delivery)."""
    rng = random.Random(seed)
    doubled = list(events) + [e for e in events if rng.random() < 0.5]
    rng.shuffle(doubled)
    return doubled


# Two synthetic node_ids, coordinator + lease + heartbeat + node events interleaved. Includes a
# hub-rewind: e_rewind is CREATED later (00:99) but carries a LOWER epoch than e_c3 -- max-epoch-wins
# means it must NEVER win the coordinator role.
_EVENTS = [
    {"id": "n_a", "created_at": "2026-01-01T00:00:01", "node_id": "nA", "event_kind": "node", "marker": "registered", "payload": {"role": "coordinator", "tailnet_name": "a.ts.net"}},
    {"id": "n_b", "created_at": "2026-01-01T00:00:01", "node_id": "nB", "event_kind": "node", "marker": "registered", "payload": {"role": "worker", "tailnet_name": "b.ts.net"}},
    {"id": "c1", "created_at": "2026-01-01T00:00:02", "node_id": "nA", "event_kind": "coordinator", "marker": "granted", "epoch": 1},
    {"id": "c2", "created_at": "2026-01-01T00:00:03", "node_id": "nB", "event_kind": "coordinator", "marker": "granted", "epoch": 2},
    {"id": "c3", "created_at": "2026-01-01T00:00:04", "node_id": "nA", "event_kind": "coordinator", "marker": "granted", "epoch": 3},
    {"id": "c_rewind", "created_at": "2026-01-01T00:00:59", "node_id": "nB", "event_kind": "coordinator", "marker": "granted", "epoch": 2},
    {"id": "hb_a", "created_at": "2026-01-01T00:00:05", "node_id": "nA", "event_kind": "heartbeat", "marker": "point"},
    {"id": "hb_b", "created_at": "2026-01-01T00:00:06", "node_id": "nB", "event_kind": "heartbeat", "marker": "point"},
    {"id": "l1", "created_at": "2026-01-01T00:00:07", "node_id": "nA", "event_kind": "lease", "marker": "granted", "scope_key": "proj/main", "epoch": 5},
    {"id": "l2", "created_at": "2026-01-01T00:00:08", "node_id": "nB", "event_kind": "lease", "marker": "granted", "scope_key": "proj/main", "epoch": 6},
    {"id": "l3", "created_at": "2026-01-01T00:00:09", "node_id": "nA", "event_kind": "lease", "marker": "granted", "scope_key": "proj/feat", "epoch": 7},
]


class ReducerConvergenceTest(unittest.TestCase):
    def setUp(self):
        self.ref_coord = reducers.reduce_coordinator(_EVENTS)
        self.ref_lease = reducers.reduce_lease(_EVENTS)
        self.ref_nodes = reducers.reduce_nodes(_EVENTS)
        self.ref_epoch = reducers.max_epoch(_EVENTS)

    def test_reducers_are_deterministic_under_reorder_and_duplication(self):
        """Every reducer yields the SAME projection across 250 reordered/duplicated inputs -- the
        two-node-interleaving convergence invariant."""
        for seed in range(250):
            shuffled = _reorder_and_duplicate(_EVENTS, seed)
            self.assertEqual(reducers.reduce_coordinator(shuffled), self.ref_coord, f"coord drift @ seed {seed}")
            self.assertEqual(reducers.reduce_lease(shuffled), self.ref_lease, f"lease drift @ seed {seed}")
            self.assertEqual(reducers.reduce_nodes(shuffled), self.ref_nodes, f"nodes drift @ seed {seed}")
            self.assertEqual(reducers.max_epoch(shuffled), self.ref_epoch, f"epoch drift @ seed {seed}")

    def test_coordinator_is_max_epoch_wins_not_last_seen(self):
        """The highest epoch wins; the later-CREATED lower-epoch rewind (c_rewind @ epoch 2) does NOT
        displace c3 @ epoch 3 (ledger #20 hub-restore-regression shape)."""
        self.assertEqual(self.ref_coord["holder"], "nA")
        self.assertEqual(self.ref_coord["epoch"], 3)
        self.assertEqual(self.ref_coord["id"], "c3")
        self.assertEqual(self.ref_coord["mode"], "roaming")  # default when no payload.mode

    def test_hub_rewind_batch_cannot_displace_higher_epoch(self):
        """Explicit hub-restore simulation: appending an OLD-snapshot batch (all lower epochs, created
        far in the future) after the current state still reduces to the higher epoch."""
        rewind_batch = [
            {"id": "old1", "created_at": "2026-12-31T23:59:58", "node_id": "nB", "event_kind": "coordinator", "marker": "granted", "epoch": 1},
            {"id": "old2", "created_at": "2026-12-31T23:59:59", "node_id": "nB", "event_kind": "coordinator", "marker": "granted", "epoch": 2},
        ]
        combined = _EVENTS + rewind_batch
        result = reducers.reduce_coordinator(combined)
        self.assertEqual(result["epoch"], 3, "a lower-epoch hub-restore batch must not win")
        self.assertEqual(result["holder"], "nA")
        # max_epoch (the watermark input) still reflects the true high-water mark.
        self.assertEqual(reducers.max_epoch(combined), 7)

    def test_lease_projection_max_epoch_and_release(self):
        """proj/main: the top grant is l2 @ epoch 6 (beats l1 @ 5); proj/feat: l3 @ 7 held."""
        self.assertEqual(self.ref_lease["proj/main"]["epoch"], 6)
        self.assertEqual(self.ref_lease["proj/main"]["holder"], "nB")
        self.assertEqual(self.ref_lease["proj/main"]["state"], "held")
        self.assertEqual(self.ref_lease["proj/feat"]["epoch"], 7)
        self.assertEqual(self.ref_lease["proj/feat"]["state"], "held")

    def test_lease_release_frees_the_scope(self):
        """A release at the winning epoch frees the scope (no holder, state 'free')."""
        events = _EVENTS + [
            {"id": "rel", "created_at": "2026-01-01T00:00:20", "node_id": "nB", "event_kind": "lease", "marker": "released", "scope_key": "proj/main", "epoch": 6},
        ]
        result = reducers.reduce_lease(events)
        self.assertEqual(result["proj/main"]["state"], "free")
        self.assertIsNone(result["proj/main"]["holder"])

    def test_nodes_projection_folds_registration_and_heartbeat(self):
        """Per-node role/tailnet_name come from registration; last_heartbeat from the heartbeat point."""
        self.assertEqual(self.ref_nodes["nA"]["role"], "coordinator")
        self.assertEqual(self.ref_nodes["nA"]["tailnet_name"], "a.ts.net")
        self.assertEqual(self.ref_nodes["nA"]["last_heartbeat"], "2026-01-01T00:00:05")
        self.assertEqual(self.ref_nodes["nB"]["role"], "worker")
        self.assertFalse(self.ref_nodes["nA"]["retired"])

    # --- M10a extensions: grant_seq, expiry, claimed-vs-granted, mode/to_node, convergence ---------

    def test_grant_seq_breaks_same_epoch_ties_highest_wins(self):
        """A renew re-grants at the SAME epoch with grant_seq+1; the highest grant_seq wins the scope
        (grantor-local monotonic secondary key), regardless of created_at/id ordering."""
        events = [
            {"id": "g0", "created_at": "2026-02-01T00:00:01", "node_id": "nA", "event_kind": "lease", "marker": "granted", "scope_key": "p/s", "epoch": 4, "payload": {"grant_seq": 0, "holder": "nA"}},
            {"id": "g1", "created_at": "2026-02-01T00:00:02", "node_id": "nA", "event_kind": "lease", "marker": "granted", "scope_key": "p/s", "epoch": 4, "payload": {"grant_seq": 1, "holder": "nB"}},
            {"id": "g2", "created_at": "2026-02-01T00:00:03", "node_id": "nA", "event_kind": "lease", "marker": "granted", "scope_key": "p/s", "epoch": 4, "payload": {"grant_seq": 2, "holder": "nC"}},
        ]
        result = reducers.reduce_lease(events)["p/s"]
        self.assertEqual(result["grant_seq"], 2)
        self.assertEqual(result["holder"], "nC")  # payload.holder read, not the grantor node_id nA
        self.assertEqual(result["state"], "held")
        # A LOWER grant_seq created LATER (out-of-order replay) still does not win.
        replayed = events + [
            {"id": "g_old", "created_at": "2026-02-01T00:09:59", "node_id": "nA", "event_kind": "lease", "marker": "granted", "scope_key": "p/s", "epoch": 4, "payload": {"grant_seq": 1, "holder": "nB"}},
        ]
        self.assertEqual(reducers.reduce_lease(replayed)["p/s"]["holder"], "nC")

    def test_grant_seq_convergence_under_reorder_and_dupe(self):
        """Same-epoch grant_seq ordering reduces IDENTICALLY across reordered/duplicated streams."""
        events = [
            {"id": "s0", "created_at": "2026-02-01T00:00:01", "node_id": "nA", "event_kind": "lease", "marker": "granted", "scope_key": "p/s", "epoch": 3, "payload": {"grant_seq": 0, "holder": "nA"}},
            {"id": "s1", "created_at": "2026-02-01T00:00:02", "node_id": "nA", "event_kind": "lease", "marker": "granted", "scope_key": "p/s", "epoch": 3, "payload": {"grant_seq": 1, "holder": "nA"}},
            {"id": "s2", "created_at": "2026-02-01T00:00:03", "node_id": "nA", "event_kind": "lease", "marker": "granted", "scope_key": "p/t", "epoch": 3, "payload": {"grant_seq": 0, "holder": "nB"}},
        ]
        ref = reducers.reduce_lease(events)
        for seed in range(120):
            self.assertEqual(reducers.reduce_lease(_reorder_and_duplicate(events, seed)), ref, f"grant_seq drift @ {seed}")
        self.assertEqual(ref["p/s"]["grant_seq"], 1)

    def test_expiry_is_deterministic_for_a_given_now(self):
        """A ttl grant with now beyond expires_at reduces to 'expired' (holder None); before, 'held'.
        Deterministic per now => both replicas reduce identically."""
        grant = [
            {"id": "e1", "created_at": "2026-03-01T00:00:00", "node_id": "nA", "event_kind": "lease", "marker": "granted", "scope_key": "p/x", "epoch": 5, "payload": {"grant_seq": 1, "holder": "nA", "expires_at": "2026-03-01T00:15:00"}},
        ]
        before = reducers.reduce_lease(grant, now="2026-03-01T00:10:00")["p/x"]
        self.assertEqual(before["state"], "held")
        self.assertEqual(before["holder"], "nA")
        after = reducers.reduce_lease(grant, now="2026-03-01T00:20:00")["p/x"]
        self.assertEqual(after["state"], "expired")
        self.assertIsNone(after["holder"])
        self.assertEqual(after["expires_at"], "2026-03-01T00:15:00")
        # No now => never expires by clock (state held).
        self.assertEqual(reducers.reduce_lease(grant)["p/x"]["state"], "held")
        # Convergence under reorder for the expired now.
        for seed in range(60):
            self.assertEqual(reducers.reduce_lease(_reorder_and_duplicate(grant, seed), now="2026-03-01T00:20:00"), reducers.reduce_lease(grant, now="2026-03-01T00:20:00"), f"expiry drift @ {seed}")

    def test_expiry_releases_scope_for_a_new_grant(self):
        """After expiry, a new grant at the same/later epoch takes the scope cleanly (expiry releases)."""
        events = [
            {"id": "x1", "created_at": "2026-03-02T00:00:00", "node_id": "nA", "event_kind": "lease", "marker": "granted", "scope_key": "p/y", "epoch": 5, "payload": {"grant_seq": 1, "holder": "nA", "expires_at": "2026-03-02T00:05:00"}},
            {"id": "x2", "created_at": "2026-03-02T01:00:00", "node_id": "nA", "event_kind": "lease", "marker": "granted", "scope_key": "p/y", "epoch": 6, "payload": {"grant_seq": 1, "holder": "nB", "expires_at": "2026-03-02T02:00:00"}},
        ]
        result = reducers.reduce_lease(events, now="2026-03-02T01:30:00")["p/y"]
        self.assertEqual(result["state"], "held")
        self.assertEqual(result["holder"], "nB")
        self.assertEqual(result["epoch"], 6)

    def test_coordinator_mode_passthrough(self):
        """The winning coordinator event's payload.mode (roaming|pinned) is surfaced; default roaming."""
        pinned = [
            {"id": "cm", "created_at": "2026-04-01T00:00:01", "node_id": "nA", "event_kind": "coordinator", "marker": "granted", "epoch": 2, "payload": {"mode": "pinned"}},
        ]
        self.assertEqual(reducers.reduce_coordinator(pinned)["mode"], "pinned")
        # Convergence: mode is stable under reorder/dupe.
        for seed in range(60):
            self.assertEqual(reducers.reduce_coordinator(_reorder_and_duplicate(pinned, seed)), reducers.reduce_coordinator(pinned), f"mode drift @ {seed}")

    def test_contested_claim_does_not_displace_granted_holder(self):
        """claimed-vs-granted: a higher-epoch bare `claimed` against a live granted holder does NOT win.
        granted is the sole authority; claimed is a request (§B.3)."""
        events = [
            {"id": "gh", "created_at": "2026-04-02T00:00:01", "node_id": "nA", "event_kind": "coordinator", "marker": "granted", "epoch": 3, "payload": {"mode": "roaming"}},
            {"id": "cc", "created_at": "2026-04-02T00:00:02", "node_id": "nB", "event_kind": "coordinator", "marker": "claimed", "epoch": 4},
        ]
        result = reducers.reduce_coordinator(events)
        self.assertEqual(result["holder"], "nA")  # the granted holder, not the higher-epoch claimant
        self.assertEqual(result["epoch"], 3)
        # Convergence: the claimed-vs-granted resolution is order-independent.
        for seed in range(60):
            self.assertEqual(reducers.reduce_coordinator(_reorder_and_duplicate(events, seed)), result, f"contest drift @ {seed}")

    def test_bare_claim_wins_only_as_bootstrap(self):
        """With NO granted at any epoch, the highest-epoch `claimed` wins (bootstrap fallback)."""
        events = [
            {"id": "b1", "created_at": "2026-04-03T00:00:01", "node_id": "nA", "event_kind": "coordinator", "marker": "claimed", "epoch": 1},
            {"id": "b2", "created_at": "2026-04-03T00:00:02", "node_id": "nB", "event_kind": "coordinator", "marker": "claimed", "epoch": 2},
        ]
        result = reducers.reduce_coordinator(events)
        self.assertEqual(result["holder"], "nB")
        self.assertEqual(result["epoch"], 2)

    def test_coordinator_transfer_reads_to_node(self):
        """A transfer grant's node_id is the appending (old) holder; the new holder is payload.to_node."""
        events = [
            {"id": "t0", "created_at": "2026-04-04T00:00:01", "node_id": "nA", "event_kind": "coordinator", "marker": "granted", "epoch": 1, "payload": {"mode": "roaming"}},
            {"id": "t1", "created_at": "2026-04-04T00:00:02", "node_id": "nA", "event_kind": "coordinator", "marker": "released", "epoch": 1},
            {"id": "t2", "created_at": "2026-04-04T00:00:03", "node_id": "nA", "event_kind": "coordinator", "marker": "granted", "epoch": 2, "payload": {"mode": "roaming", "to_node": "nB"}},
        ]
        result = reducers.reduce_coordinator(events)
        self.assertEqual(result["holder"], "nB")  # read from payload.to_node, not the appender nA
        self.assertEqual(result["epoch"], 2)
        # A replayed OLD-epoch grant does not displace the new holder (max-epoch-wins + to_node shape).
        replayed = events + [
            {"id": "t_old", "created_at": "2026-04-04T09:59:59", "node_id": "nA", "event_kind": "coordinator", "marker": "granted", "epoch": 1, "payload": {"mode": "roaming"}},
        ]
        self.assertEqual(reducers.reduce_coordinator(replayed)["holder"], "nB")

    def test_coordinator_release_permanently_fences_same_epoch_regrant(self):
        """Coordinator epochs are one-shot: granted@N, released@N, granted@N remains unheld."""
        events = [
            {"id": "g1", "created_at": "2026-04-05T00:00:01", "node_id": "nA", "event_kind": "coordinator", "marker": "granted", "epoch": 4},
            {"id": "r1", "created_at": "2026-04-05T00:00:02", "node_id": "nA", "event_kind": "coordinator", "marker": "released", "epoch": 4},
            {"id": "g2", "created_at": "2026-04-05T00:00:03", "node_id": "nB", "event_kind": "coordinator", "marker": "granted", "epoch": 4},
        ]
        self.assertEqual(reducers.reduce_coordinator(events), {"holder": None, "epoch": -1, "mode": "roaming"})

    def test_lease_release_allows_higher_sequence_regrant_at_same_epoch(self):
        """Lease renewal keeps its distinct grant-sequence rule: a newer sequence may reuse the epoch."""
        events = [
            {"id": "g1", "created_at": "2026-04-06T00:00:01", "node_id": "nA", "event_kind": "lease", "marker": "granted", "scope_key": "p/s", "epoch": 4, "payload": {"grant_seq": 1, "holder": "nA"}},
            {"id": "r1", "created_at": "2026-04-06T00:00:02", "node_id": "nA", "event_kind": "lease", "marker": "released", "scope_key": "p/s", "epoch": 4, "payload": {"grant_seq": 1}},
            {"id": "g2", "created_at": "2026-04-06T00:00:03", "node_id": "nA", "event_kind": "lease", "marker": "granted", "scope_key": "p/s", "epoch": 4, "payload": {"grant_seq": 2, "holder": "nB"}},
        ]
        result = reducers.reduce_lease(events)["p/s"]
        self.assertEqual((result["state"], result["epoch"], result["grant_seq"], result["holder"]), ("held", 4, 2, "nB"))

    def test_empty_and_epochless_inputs_are_safe(self):
        """No events => empty/-1 projections; epochless events => max_epoch -1 (watermark not advanced)."""
        self.assertEqual(reducers.reduce_coordinator([]), {"holder": None, "epoch": -1, "mode": "roaming"})
        self.assertEqual(reducers.reduce_lease([]), {})
        self.assertEqual(reducers.reduce_nodes([]), {})
        self.assertEqual(reducers.max_epoch([]), -1)
        epochless = [{"id": "x", "created_at": "t", "node_id": "nA", "event_kind": "heartbeat", "marker": "point"}]
        self.assertEqual(reducers.max_epoch(epochless), -1)
        idless = [{"created_at": "t", "node_id": "nA", "event_kind": "heartbeat", "marker": "point"}]
        self.assertEqual(reducers.reduce_nodes(idless)["nA"]["last_heartbeat"], "t")
        self.assertEqual(reducers.max_epoch(idless), -1)


if __name__ == "__main__":
    unittest.main()
