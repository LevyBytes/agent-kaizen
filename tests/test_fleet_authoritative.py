"""Fleet authoritative enforcement (v8 M10b): epoch fence + authoritative mutex + partition +
watermark regression + pinned-contested divergence + iso sentinel + node fence (gate F3).

Hermetic: NO sync server, NO network. FleetStore legs run in a SUBPROCESS pinned to a fresh scratch
KAIZEN_REPO_ROOT (paths.REPO_ROOT is import-frozen, so a subprocess carries its own plane and nothing
leaks into other in-process suites -- same rationale as test_fleet_core / test_fleet_coordination).

Partition + watermark legs never need a live sync server: they FLAG a plain store as synced
(store.sync._synced = True) and either inject a raising pull() (partition) or drive a lower-epoch digest
against a pre-seeded watermark (regression). Advisory-vs-authoritative and pinned-vs-roaming are the CP/AP
and single-source-of-truth splits M10b enforces.

M10a advisory/shadow behavior (and the flipped stale-fence assertion) lives in test_fleet_coordination.py.
The C1-C4 node-fence leg (WriteTxFence) round-trips through the real CLI (IsolatedDBTest), so the existing
test_session_records suite proving off-mode inertness stays authoritative for the off path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import REPO_ROOT, IsolatedDBTest, kaizen  # noqa: E402


# Runs inside the scratch-plane subprocess: builds FleetStore(s), exposes the coordination engine +
# reducers + db (watermark seeding), execs the per-test BODY (which sets `out`), and emits it. A
# KaizenDenied that escapes the BODY is surfaced as {"_denied": code, "_exit": exit_code}; BODies that
# want to assert BOTH a refusal AND post-state catch KaizenDenied in-band and read events afterward.
_PREAMBLE = r"""
import json, os
from kaizen_components.fleet import identity, reducers, coordination as co
from kaizen_components.fleet.store import FleetStore
from kaizen_components import db
from kaizen_components.denials import KaizenDenied

def make_store(**kw):
    return FleetStore(**kw)

out = None
try:
    exec(BODY)
    print("RESULT " + json.dumps(out))
except KaizenDenied as e:
    print("RESULT " + json.dumps({"_denied": e.code, "_exit": e.exit_code}))
"""


class _ScratchSubprocess(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-auth-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        rc, payload = kaizen(self.root, "K1")
        self.assertEqual(rc, 0, f"K1 init failed: {payload}")

    def plane(self, body: str, *, env: dict | None = None, second_root: Path | None = None) -> dict:
        """Describe environment isolation, trusted BODY embedding, denial encoding, RESULT decoding, and failure behavior."""
        script = "BODY = " + repr(body) + "\n" + _PREAMBLE
        full_env = dict(os.environ)
        full_env["KAIZEN_REPO_ROOT"] = str(self.root)
        if second_root is not None:
            full_env["KZ_SECOND_ROOT"] = str(second_root)
        if env:
            full_env.update(env)
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(REPO_ROOT), env=full_env, timeout=60
        )
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        self.fail(f"no RESULT from subprocess.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


class TwoNodeContentionTest(_ScratchSubprocess):
    def test_authoritative_contention_yields_exactly_one_holder(self):
        second = Path(tempfile.mkdtemp(prefix="kaizen-auth-B-"))
        self.addCleanup(shutil.rmtree, second, ignore_errors=True)
        self.assertEqual(kaizen(second, "K1")[0], 0)
        out = self.plane(
            "import os\n"
            "dbA = os.path.join(os.environ['KAIZEN_REPO_ROOT'], 'AI', 'db', 'fleet.db')\n"
            "dbB = os.path.join(os.environ['KZ_SECOND_ROOT'], 'AI', 'db', 'fleet.db')\n"
            "sA = FleetStore(db_path=dbA); sB = FleetStore(db_path=dbB)\n"
            "co.claim_coordinator(sA, summary='claim')\n"
            "NODE_B = 'nBBBB00000000'\n"
            "# authoritative grant of scope S to A => held\n"
            "g1 = co.grant_lease(sA, 'p/S', sA.node_id, mode='authoritative', summary='g1')\n"
            "held_state = co.current_leases(sA)['p/S']['state']\n"
            "# authoritative grant of the SAME scope to B while held => DENIED_LEASE_HELD (contention)\n"
            "denied = None\n"
            "try:\n"
            "    co.grant_lease(sA, 'p/S', NODE_B, mode='authoritative', summary='g2')\n"
            "except KaizenDenied as e:\n"
            "    denied = e.code\n"
            "# release (A is the holder) then grant to B cleanly\n"
            "co.release_lease(sA, 'p/S', summary='rel')\n"
            "g3 = co.grant_lease(sA, 'p/S', NODE_B, mode='authoritative', summary='g3')\n"
            "# cross-replay ALL of A's events into B and reduce both\n"
            "for r in sA.coord_events():\n"
            "    sB.append_coord_event(r['event_kind'], r['marker'], summary='replay', scope_key=r.get('scope_key'), epoch=r.get('epoch'), payload=r.get('payload'), source_event_id=r['id'])\n"
            "la = co.current_leases(sA)['p/S']; lb = co.current_leases(sB)['p/S']\n"
            "out = {\n"
            "  'held_state': held_state, 'denied': denied,\n"
            "  'final_holder_A': la['holder'], 'final_holder_B': lb['holder'],\n"
            "  'final_state_A': la['state'], 'final_state_B': lb['state'],\n"
            "  'mode_A': la['mode'], 'g1_mode': g1['mode'], 'g3_holder': g3['holder'],\n"
            "}\n"
            "sA.close(); sB.close()\n",
            second_root=second,
        )
        self.assertEqual(out["held_state"], "held")
        self.assertEqual(out["g1_mode"], "authoritative")
        self.assertEqual(out["denied"], "DENIED_LEASE_HELD")  # contention blocked
        # After release + re-grant, EXACTLY ONE holder (node B) on both replicas -- no split holder.
        self.assertEqual(out["final_holder_A"], "nBBBB00000000")
        self.assertEqual(out["final_holder_B"], "nBBBB00000000")
        self.assertEqual(out["final_state_A"], "held")
        self.assertEqual(out["final_state_B"], "held")
        self.assertEqual(out["mode_A"], "authoritative")

    def test_authoritative_regrant_to_same_holder_also_contends(self):
        # Re-granting a HELD scope to ANY holder (even the current one) via grant (not renew) is
        # contention -- the mutex is on "held", not on identity. (renew is the holder's own path.)
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c')\n"
            "co.grant_lease(s, 'p/S', s.node_id, mode='authoritative', summary='g')\n"
            "co.grant_lease(s, 'p/S', s.node_id, mode='authoritative', summary='regrant')\n"
            "out = {}\n"
        )
        self.assertEqual(out["_denied"], "DENIED_LEASE_HELD")
        self.assertEqual(out["_exit"], 2)


class StaleEpochDeniedTest(_ScratchSubprocess):
    def test_stale_grantor_action_records_conflict_then_denies(self):
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c')\n"  # current epoch 1
            "denied = None\n"
            "try:\n"
            "    co.grant_lease(s, 'p/S', s.node_id, summary='g', assumed_epoch=0)\n"
            "except KaizenDenied as e:\n"
            "    denied = e.code\n"
            "conflicts = [c for c in s.coord_events() if c['event_kind']=='conflict' and c['marker']=='detected']\n"
            "would = conflicts[0]['payload']['would_block'] if conflicts else None\n"
            "granted = 'p/S' in co.current_leases(s)\n"
            "out = {'denied': denied, 'would': would, 'n_conflict': len(conflicts), 'granted': granted}\n"
        )
        self.assertEqual(out["denied"], "DENIED_STALE_FENCE")
        self.assertEqual(out["would"], "DENIED_STALE_FENCE")  # audit event written BEFORE the raise
        self.assertEqual(out["n_conflict"], 1)
        self.assertFalse(out["granted"])

    def test_fresh_epoch_proceeds(self):
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c')\n"
            "g = co.grant_lease(s, 'p/S', s.node_id, summary='g', assumed_epoch=1)\n"  # matches current
            "conflicts = [c for c in s.coord_events() if c['event_kind']=='conflict']\n"
            "out = {'status': g['status'], 'n_conflict': len(conflicts), 'state': co.current_leases(s)['p/S']['state']}\n"
        )
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["n_conflict"], 0)
        self.assertEqual(out["state"], "held")


class PartitionNoSplitBrainTest(_ScratchSubprocess):
    def test_authoritative_refuses_when_hub_unreachable_advisory_proceeds(self):
        # Flag a plain store synced, then inject a pull() that raises (a partition). Authoritative
        # grant must FIRST pull => DENIED_LEASE_HUB_UNREACHABLE; advisory on the SAME store proceeds (AP).
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c')\n"
            "s.sync._synced = True\n"
            "def boom():\n"
            "    raise RuntimeError('sync endpoint unreachable')\n"
            "s.sync.pull = boom\n"
            "auth_denied = None\n"
            "try:\n"
            "    co.grant_lease(s, 'p/S', s.node_id, mode='authoritative', summary='g')\n"
            "except KaizenDenied as e:\n"
            "    auth_denied = e.code\n"
            "# advisory on the SAME partitioned store proceeds (never pulls)\n"
            "adv = co.grant_lease(s, 'p/Sadv', s.node_id, mode='advisory', summary='gadv')\n"
            "out = {'auth_denied': auth_denied, 'adv_status': adv['status'], 'adv_mode': adv['mode'],\n"
            "       'auth_granted': 'p/S' in co.current_leases(s)}\n"
        )
        self.assertEqual(out["auth_denied"], "DENIED_LEASE_HUB_UNREACHABLE")
        self.assertFalse(out["auth_granted"])  # partitioned authoritative did NOT write
        self.assertEqual(out["adv_status"], "OK")  # advisory is partition-tolerant
        self.assertEqual(out["adv_mode"], "advisory")

    def test_authoritative_allowed_when_sync_off(self):
        # A sync-off store cannot partition (it never pulls a hub), so authoritative is allowed.
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c')\n"
            "g = co.grant_lease(s, 'p/S', s.node_id, mode='authoritative', summary='g')\n"
            "out = {'status': g['status'], 'synced': s.sync.synced, 'state': co.current_leases(s)['p/S']['state']}\n"
        )
        self.assertEqual(out["status"], "OK")
        self.assertFalse(out["synced"])
        self.assertEqual(out["state"], "held")


class WatermarkRegressionTest(_ScratchSubprocess):
    def test_synced_digest_below_watermark_refuses(self):
        out = self.plane(
            "s = make_store()\n"
            "pid = identity.project_id()['project_id']\n"
            "# seed the un-synced watermark HIGH on the scratch plane\n"
            "db.set_setting('fleet_max_seen_epoch_' + pid, '50')\n"
            "# a lower-epoch ledger on a SYNCED-flagged store => regression\n"
            "s.append_coord_event('coordinator', 'granted', summary='low', epoch=2)\n"
            "s.sync._synced = True\n"
            "denied = None\n"
            "try:\n"
            "    s.digest()\n"
            "except KaizenDenied as e:\n"
            "    denied = e.code\n"
            "out = {'denied': denied, 'watermark': s.watermark(pid)}\n"
            "s.close()\n"
        )
        self.assertEqual(out["denied"], "DENIED_EPOCH_REGRESSION")
        self.assertEqual(out["watermark"], 50)  # refusal did NOT lower the watermark

    def test_accept_env_resets_watermark_and_records_divergence(self):
        out = self.plane(
            "s = make_store()\n"
            "pid = identity.project_id()['project_id']\n"
            "db.set_setting('fleet_max_seen_epoch_' + pid, '50')\n"
            "s.append_coord_event('coordinator', 'granted', summary='low', epoch=2)\n"
            "s.sync._synced = True\n"
            "d = s.digest()\n"  # with the accept env set, digest proceeds (no raise)
            "divs = [x for x in s.coord_events() if x['event_kind']=='divergence' and x['marker']=='detected']\n"
            "accepted = [x['payload'] for x in divs if x['payload'].get('accepted')]\n"
            "out = {'status': d['status'], 'watermark': s.watermark(pid), 'n_accepted': len(accepted),\n"
            "       'stored': accepted[0]['stored_watermark'] if accepted else None,\n"
            "       'reduced': accepted[0]['reduced_epoch'] if accepted else None}\n"
            "s.close()\n",
            env={"KAIZEN_FLEET_ACCEPT_EPOCH_REGRESSION": "1"},
        )
        self.assertEqual(out["status"], "OK")  # accepted, not refused
        self.assertEqual(out["watermark"], 2)  # reset DOWN to the reduced epoch
        self.assertEqual(out["n_accepted"], 1)
        self.assertEqual(out["stored"], 50)
        self.assertEqual(out["reduced"], 2)

    def test_unsynced_store_below_watermark_never_refuses(self):
        # An unsynced local/lab store never regresses (it never pulls a hub) -- digest is silent.
        out = self.plane(
            "s = make_store()\n"
            "pid = identity.project_id()['project_id']\n"
            "db.set_setting('fleet_max_seen_epoch_' + pid, '50')\n"
            "s.append_coord_event('coordinator', 'granted', summary='low', epoch=2)\n"
            "d = s.digest()\n"  # sync OFF => no regression check
            "out = {'status': d['status'], 'watermark': s.watermark(pid)}\n"
            "s.close()\n"
        )
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["watermark"], 50)  # monotonic: a lower reduced epoch never lowered it


class PinnedContestedClaimTest(_ScratchSubprocess):
    def test_pinned_contested_claim_refuses_diverged(self):
        # A PINNED coordinator held by another node => a contested claim is split-brain control =>
        # DENIED_COORD_DIVERGED (M10b), not the M10a record-and-return.
        out = self.plane(
            "s = make_store()\n"
            "# inject a foreign PINNED granted holder at epoch 1\n"
            "s.append_coord_event('coordinator', 'granted', summary='foreign pinned', epoch=1, payload={'mode':'pinned','to_node':'nAAAA00000000'})\n"
            "co.claim_coordinator(s, summary='mine')\n"
            "out = {}\n"
        )
        self.assertEqual(out["_denied"], "DENIED_COORD_DIVERGED")
        self.assertEqual(out["_exit"], 2)

    def test_roaming_contested_claim_records_and_preserves_holder(self):
        # ROAMING keeps M10a semantics: record the contested claim, never displace, never raise.
        out = self.plane(
            "s = make_store()\n"
            "s.append_coord_event('coordinator', 'granted', summary='foreign roaming', epoch=1, payload={'mode':'roaming','to_node':'nAAAA00000000'})\n"
            "r = co.claim_coordinator(s, summary='mine')\n"
            "coord = co.current_coordinator(s)\n"
            "conflicts = [c for c in s.coord_events() if c['event_kind']=='conflict' and c['marker']=='detected']\n"
            "would = conflicts[0]['payload']['would_block'] if conflicts else None\n"
            "out = {'status': r['status'], 'contested': r['contested'], 'holder': coord['holder'], 'would': would}\n"
        )
        self.assertEqual(out["status"], "RECORDED")
        self.assertTrue(out["contested"])
        self.assertEqual(out["holder"], "nAAAA00000000")  # not displaced
        self.assertEqual(out["would"], "CONTESTED_CLAIM")


class IsoSentinelTest(_ScratchSubprocess):
    def test_isolated_claim_records_sentinel_and_reducer_surfaces_it(self):
        out = self.plane(
            "s = make_store()\n"
            "r = co.claim_coordinator(s, isolated=True, summary='iso claim')\n"
            "coord = co.current_coordinator(s)\n"
            "granted = [g for g in s.coord_events() if g['event_kind']=='coordinator' and g['marker']=='granted']\n"
            "pay = granted[0]['payload'] if granted else {}\n"
            "out = {\n"
            "  'result_iso': r.get('iso'), 'result_sentinel': r.get('iso_sentinel'),\n"
            "  'payload_iso': pay.get('iso'), 'payload_sentinel': pay.get('iso_sentinel'),\n"
            "  'reduced_iso': coord.get('iso'), 'reduced_sentinel': coord.get('iso_sentinel'),\n"
            "  'sentinel_prefix': (coord.get('iso_sentinel') or '').startswith('iso:'),\n"
            "}\n"
        )
        self.assertTrue(out["result_iso"])
        self.assertTrue(out["payload_iso"])
        self.assertTrue(out["reduced_iso"])
        self.assertTrue(out["sentinel_prefix"])
        # The sentinel is consistent record -> reducer.
        self.assertEqual(out["result_sentinel"], out["payload_sentinel"])
        self.assertEqual(out["payload_sentinel"], out["reduced_sentinel"])

    def test_isolated_transfer_stamps_sentinel_on_granted(self):
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c')\n"
            "t = co.transfer_coordinator(s, 'nBBBB00000000', isolated=True, summary='xfer')\n"
            "coord = co.current_coordinator(s)\n"
            "out = {'t_iso': t.get('iso'), 'reduced_iso': coord.get('iso'), 'holder': coord['holder'],\n"
            "       'sentinel_prefix': (coord.get('iso_sentinel') or '').startswith('iso:')}\n"
        )
        self.assertTrue(out["t_iso"])
        self.assertTrue(out["reduced_iso"])
        self.assertEqual(out["holder"], "nBBBB00000000")
        self.assertTrue(out["sentinel_prefix"])

    def test_non_isolated_claim_has_no_sentinel(self):
        out = self.plane(
            "s = make_store()\n"
            "r = co.claim_coordinator(s, summary='plain')\n"
            "coord = co.current_coordinator(s)\n"
            "out = {'result_iso': r.get('iso'), 'reduced_iso': coord.get('iso')}\n"
        )
        self.assertIsNone(out["result_iso"])
        self.assertIsNone(out["reduced_iso"])


class AdvisoryUnchangedTest(_ScratchSubprocess):
    def test_m10a_advisory_flows_are_byte_identical(self):
        # No mode arg => advisory default. grant/renew/release/expiry behave exactly as M10a: no pull,
        # no contention, no fence surprises.
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c')\n"
            "g = co.grant_lease(s, 'p/m', s.node_id, summary='g')\n"  # advisory default, self holds
            "r1 = co.renew_lease(s, 'p/m', summary='rn')\n"
            "held = co.current_leases(s)['p/m']\n"
            "rel = co.release_lease(s, 'p/m', summary='rel')\n"  # self is the holder => OK
            "free = co.current_leases(s)['p/m']\n"
            "# a SECOND advisory grant of a scope already held is best-effort (NOT refused) -- M10a AP:\n"
            "co.grant_lease(s, 'p/n', s.node_id, summary='n1')\n"
            "g2 = co.grant_lease(s, 'p/n', 'nOther0000000', summary='n2')\n"  # advisory never contends
            "held_n = co.current_leases(s)['p/n']\n"
            "out = {'g_mode': g['mode'], 'r1_seq': r1['grant_seq'],\n"
            "       'held_is_self': held['holder'] == s.node_id, 'held_seq': held['grant_seq'],\n"
            "       'rel_status': rel['status'], 'free_state': free['state'],\n"
            "       'g2_status': g2['status'], 'held_n_holder': held_n['holder']}\n"
        )
        self.assertEqual(out["g_mode"], "advisory")  # default is advisory
        self.assertEqual(out["r1_seq"], 2)  # renew bumps seq
        self.assertTrue(out["held_is_self"])  # self holds after grant+renew
        self.assertEqual(out["held_seq"], 2)  # renew won at seq 2
        self.assertEqual(out["rel_status"], "OK")  # self-held release succeeds
        self.assertEqual(out["free_state"], "free")
        self.assertEqual(out["g2_status"], "OK")  # advisory NEVER contends (AP best-effort)
        self.assertEqual(out["held_n_holder"], "nOther0000000")  # highest grant_seq wins

    def test_advisory_expiry_and_sweep_unchanged(self):
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c')\n"
            "co.grant_lease(s, 'p/e', s.node_id, ttl_s=1, summary='g')\n"  # advisory
            "future = '2999-01-01T00:00:00+00:00'\n"
            "reduced = reducers.reduce_lease(s.coord_events(), now=future)['p/e']\n"
            "sw1 = co.sweep_expired(s, now=future); sw2 = co.sweep_expired(s, now=future)\n"
            "out = {'state': reduced['state'], 'sw1': len(sw1['swept']), 'sw2': len(sw2['swept'])}\n"
        )
        self.assertEqual(out["state"], "expired")
        self.assertEqual(out["sw1"], 1)
        self.assertEqual(out["sw2"], 0)  # idempotent


class WriteTxFenceTest(IsolatedDBTest):
    """The node-aware write_tx fence (C1 stamps owner; a FOREIGN-owner C2/C3/C4 mutation is refused
    DENIED_STALE_FENCE) round-tripped through the real CLI. Off-mode inertness is proven by the existing
    test_session_records suite staying green unmodified; here we assert the ACTIVE path.

    "Foreign node" is simulated by creating the session OWNED BY an explicit foreign owning_node (C1
    payload) while THIS process's real fleet node id is different -- so a normal C2/C3/C4 mutation hits
    the owner-axis mismatch. That exercises the real fence without needing to mint a second identity."""

    ACTIVE = {"KAIZEN_DIST_MODE": "active"}
    FOREIGN_NODE = "nFOREIGN00000"

    def _my_node_id(self) -> str:
        # This process's fleet node id (minted into the isolated plane; the CLI reads the SAME file, so a
        # default-owner C1 stamps exactly this id).
        code = "from kaizen_components.fleet import identity; print(identity.node_id())"
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            env={**os.environ, "KAIZEN_REPO_ROOT": str(self.root)},
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"node-id child failed: stdout={proc.stdout!r}; stderr={proc.stderr!r}")
        output = proc.stdout.strip().splitlines()
        self.assertTrue(output, f"node-id child emitted no stdout; stderr={proc.stderr!r}")
        return output[-1]

    def test_active_c1_stamps_owning_node(self):
        rc, p = self.kz("C1", "--summary", "Owned session.", env=self.ACTIVE)
        self.assertEqual(rc, 0, p)
        rc, tl = self.kz("C5", "--session-id", p["id"])
        self.assertEqual(rc, 0, tl)
        self.assertEqual(tl["session"]["owning_node"], self._my_node_id(), tl)
        self.assertEqual(tl["session"]["node_epoch"], 0, tl)  # epoch-at-creation default

    def test_owner_node_mutation_ok(self):
        rc, p = self.kz("C1", "--summary", "Owned session.", env=self.ACTIVE)
        self.assertEqual(rc, 0, p)
        sid = p["id"]
        # Same process/node (default-stamped owner) mutates => all OK (owner matches).
        rc, i = self.kz("C2", "--session-id", sid, "--body", "Do X.", env=self.ACTIVE)
        self.assertEqual(rc, 0, i)
        rc, g = self.kz("C3", "--session-id", sid, "--title", "G", "--summary", "A goal.", env=self.ACTIVE)
        self.assertEqual(rc, 0, g)
        rc, a = self.kz("C4", "--session-id", sid, "--summary", "Approve.",
                        "--payload-json", '{"request_type":"tool_approval"}', env=self.ACTIVE)
        self.assertEqual(rc, 0, a)

    def test_foreign_owner_mutation_denied_stale_fence(self):
        # Session OWNED by a foreign node; THIS process's node id differs => C2 hits the owner-axis fence.
        rc, p = self.kz(
            "C1", "--summary", "Foreign-owned session.",
            "--payload-json", json.dumps({"owning_node": self.FOREIGN_NODE}), env=self.ACTIVE,
        )
        self.assertEqual(rc, 0, p)
        sid = p["id"]
        self.assertNotEqual(self._my_node_id(), self.FOREIGN_NODE)  # sanity: really foreign
        rc, d = self.kz("C2", "--session-id", sid, "--body", "Foreign-owned write.", env=self.ACTIVE)
        self.assertEqual(rc, 2, d)
        self.assertEqual(d.get("code"), "DENIED_STALE_FENCE", d)
        self.assertEqual(d.get("owning_node"), self.FOREIGN_NODE, d)
        self.assertEqual(d.get("expected_owning_node"), self._my_node_id(), d)
        # C3 and C4 are fenced identically.
        rc, d3 = self.kz("C3", "--session-id", sid, "--title", "G", "--summary", "Foreign goal.", env=self.ACTIVE)
        self.assertEqual(rc, 2, d3)
        self.assertEqual(d3.get("code"), "DENIED_STALE_FENCE", d3)
        rc, d4 = self.kz(
            "C4", "--session-id", sid, "--summary", "Foreign approval.",
            "--payload-json", '{"request_type":"tool_approval"}', env=self.ACTIVE,
        )
        self.assertEqual(rc, 2, d4)
        self.assertEqual(d4.get("code"), "DENIED_STALE_FENCE", d4)

    def test_legacy_null_owner_session_mutation_ok(self):
        # A session created in OFF mode carries no owning_node (NULL) -> a later ACTIVE mutation is NOT
        # fenced (back-compat): the fence only guards node-OWNED rows.
        rc, p = self.kz("C1", "--summary", "Legacy unowned session.")  # off mode: no owner stamped
        self.assertEqual(rc, 0, p)
        sid = p["id"]
        rc, tl = self.kz("C5", "--session-id", sid)
        self.assertEqual(rc, 0, tl)
        self.assertIsNone(tl["session"]["owning_node"], tl)  # NULL owner
        # Now mutate under ACTIVE mode => allowed (unfenced legacy row; owning_node IS NULL).
        rc, i = self.kz("C2", "--session-id", sid, "--body", "Legacy-row write.", env=self.ACTIVE)
        self.assertEqual(rc, 0, i)


if __name__ == "__main__":
    unittest.main()
