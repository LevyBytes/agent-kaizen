"""Fleet coordination (v8 M10a): advisory leases + roaming coordinator + shadow handoff (gate F2).

Hermetic: NO sync server, NO network. FleetStore legs run in a SUBPROCESS pinned to a fresh scratch
KAIZEN_REPO_ROOT (same rationale as test_fleet_core: paths.REPO_ROOT is import-frozen, so a subprocess
carries its own plane and nothing leaks into other in-process suites). Shadow-handoff legs use SCRATCH
git repos + a scratch bare "hub" remote (M12 precedent: two scratch clones + scratch bare remote).

Coverage map (plan §B.2/§B.3/§B.4). M10a advisory/shadow behavior lives here; M10b enforcement (the
authoritative mutex, partition refusal, pinned-contested divergence, watermark regression, node fence)
lives in test_fleet_authoritative.py:
1. two-node convergence WITHOUT sync (cross-replay => identical lease/coordinator projections);
2. grantor-only refusals (DENIED_NOT_COORDINATOR) + holder-only release (DENIED_NOT_HOLDER);
3. grant_seq monotonic (renew bumps seq; highest wins) -- reducer detail also in test_fleet_reducers;
4. expiry (ttl grant + now beyond expires_at => expired; sweep_expired idempotent; expiry releases);
5. contested claim under a ROAMING coordinator (A granted, B claims => conflict/detected recorded,
   reducer still shows A) -- M10a semantics preserved; the PINNED-contested REFUSAL is M10b;
6. transfer (A->B: released+granted to_node B at epoch+1; replayed old-epoch grant does not displace);
7. epoch-fence ENFORCEMENT (M10b flipped the M10a record-and-proceed): a stale assumed_epoch STILL
   records the conflict/detected {DENIED_STALE_FENCE} audit event, THEN raises DENIED_STALE_FENCE;
8. shadow handoff on scratch repo+bare hub (default artifact; allow commit+push; dirty divergence; abort);
9. vocabulary discipline (every emitted (kind,marker) already in COORD_EVENT_KIND_MARKERS).

D4/D5 CLI round-trip + op-coverage live in test_fleet_ops.py; README/REGISTRY parity in
test_cli_wiring.py. This file drives the engine directly.
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

from _harness import REPO_ROOT, kaizen  # noqa: E402


# Runs inside the scratch-plane subprocess: builds FleetStore(s), exposes the coordination engine and a
# git-scratch helper, execs the per-test BODY (which sets `out`), and emits it. KaizenDenied is caught
# and surfaced as {"_denied": code, "_exit": exit_code}.
_PREAMBLE = r"""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
from kaizen_components.fleet import identity, reducers, coordination as co
from kaizen_components.fleet.store import FleetStore
from kaizen_components.denials import KaizenDenied

GIT = shutil.which("git")

def make_store(**kw):
    return FleetStore(**kw)

def make_repo():
    repo = tempfile.mkdtemp(prefix="kz-hs-repo-")
    subprocess.run([GIT, "init", "-q", repo], capture_output=True, text=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        subprocess.run([GIT, "-C", repo, "config", k, v], capture_output=True, text=True)
    Path(repo, "a.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run([GIT, "-C", repo, "add", "-A"], capture_output=True, text=True)
    subprocess.run([GIT, "-C", repo, "commit", "-qm", "init"], capture_output=True, text=True)
    return repo

def make_hub():
    hub = tempfile.mkdtemp(prefix="kz-hs-hub-")
    subprocess.run([GIT, "init", "--bare", "-q", hub], capture_output=True, text=True)
    return hub

def git_loglines(repo):
    r = subprocess.run([GIT, "-C", repo, "log", "--oneline"], capture_output=True, text=True)
    return len([l for l in r.stdout.splitlines() if l.strip()])

out = None
try:
    exec(BODY)
    print("RESULT " + json.dumps(out))
except KaizenDenied as e:
    print("RESULT " + json.dumps({"_denied": e.code, "_exit": e.exit_code}))
"""


class _ScratchSubprocess(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-coord-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        rc, payload = kaizen(self.root, "K1")
        self.assertEqual(rc, 0, f"K1 init failed: {payload}")

    def plane(self, body: str, *, env: dict | None = None, second_root: Path | None = None) -> dict:
        """Describe environment isolation, optional second root, trusted BODY execution, denial encoding, RESULT decoding, and failure behavior."""
        script = "BODY = " + repr(body) + "\n" + _PREAMBLE
        full_env = dict(os.environ)
        full_env["KAIZEN_REPO_ROOT"] = str(self.root)
        if second_root is not None:
            full_env["KZ_SECOND_ROOT"] = str(second_root)
        if env:
            full_env.update(env)
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(REPO_ROOT), env=full_env,
            timeout=60,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        self.fail(f"no RESULT from subprocess.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


class TwoReplicaConvergenceTest(_ScratchSubprocess):
    def test_local_event_timestamps_advance_when_wall_clock_stalls_or_regresses(self):
        out = self.plane(
            "from kaizen_components.fleet import store as store_module\n"
            "s = make_store()\n"
            "times = iter([\n"
            "  '2026-01-01T00:00:00.123456+00:00',\n"
            "  '2026-01-01T00:00:00.123456+00:00',\n"
            "  '2025-12-31T23:59:59.999999+00:00',\n"
            "  '2027-01-01T00:00:00+00:00',\n"
            "])\n"
            "store_module._now = lambda: next(times)\n"
            "returned = [\n"
            "  s.append_coord_event('heartbeat', 'point', summary=f'event {i}')['created_at']\n"
            "  for i in range(4)\n"
            "]\n"
            "out = {'returned': returned, 'persisted': [row['created_at'] for row in s.coord_events()]}\n"
            "s.close()\n"
        )
        expected = [
            "2026-01-01T00:00:00.123456+00:00",
            "2026-01-01T00:00:00.123457+00:00",
            "2026-01-01T00:00:00.123458+00:00",
            "2027-01-01T00:00:00+00:00",
        ]
        self.assertEqual(out["returned"], expected)
        self.assertEqual(out["persisted"], expected)

    def test_cross_replay_reduces_identically_on_both_replicas(self):
        second = Path(tempfile.mkdtemp(prefix="kaizen-coord-B-"))
        self.addCleanup(shutil.rmtree, second, ignore_errors=True)
        rc, payload = kaizen(second, "K1")
        self.assertEqual(rc, 0, f"K1(second) failed: {payload}")
        out = self.plane(
            "import os\n"
            "dbA = os.path.join(os.environ['KAIZEN_REPO_ROOT'], 'AI', 'db', 'fleet.db')\n"
            "dbB = os.path.join(os.environ['KZ_SECOND_ROOT'], 'AI', 'db', 'fleet.db')\n"
            "sA = FleetStore(db_path=dbA); sB = FleetStore(db_path=dbB)\n"
            "co.claim_coordinator(sA, summary='claim')\n"
            "co.grant_lease(sA, 'p/main', sA.node_id, summary='g')\n"
            "co.request_lease(sA, 'p/feat', summary='r')\n"
            "co.release_lease(sA, 'p/main', summary='rel')\n"
            "for r in sA.coord_events():\n"
            "    sB.append_coord_event(r['event_kind'], r['marker'], summary='replay', scope_key=r.get('scope_key'), epoch=r.get('epoch'), payload=r.get('payload'), source_event_id=r['id'])\n"
            "dA = sA.digest(); dB = sB.digest()\n"
            "# Drop the local row 'id' (a node-tagged PK, distinct per replica by construction) -- the\n"
            "# CONVERGENCE claim is on holder/epoch/state/grant_seq, not the internal winner row id.\n"
            "def strip_ids(leases):\n"
            "    return {k: {kk: vv for kk, vv in v.items() if kk != 'id'} for k, v in leases.items()}\n"
            "cA = {k: v for k, v in dA['coordinator'].items() if k != 'id'}\n"
            "cB = {k: v for k, v in dB['coordinator'].items() if k != 'id'}\n"
            "out = {\n"
            "  'coordA': cA, 'coordB': cB,\n"
            "  'leasesA': strip_ids(dA['leases']), 'leasesB': strip_ids(dB['leases']),\n"
            "  'a_is_holder': dA['coordinator']['holder'] == sA.node_id,\n"
            "}\n"
            "sA.close(); sB.close()\n",
            second_root=second,
        )
        # Coordinator + lease projections are node-identity-independent (holder rides the payload) => the
        # two replicas reduce to IDENTICAL structure/state/epoch after cross-replay (modulo local row id).
        self.assertEqual(out["coordA"], out["coordB"])
        self.assertEqual(out["leasesA"], out["leasesB"])
        self.assertTrue(out["a_is_holder"])
        self.assertEqual(out["leasesA"]["p/main"]["state"], "free")  # released
        self.assertEqual(out["coordA"]["epoch"], 1)


class GrantorAndHolderGateTest(_ScratchSubprocess):
    def test_non_coordinator_grant_refused(self):
        out = self.plane("s = make_store(); co.grant_lease(s, 'p/s', s.node_id, summary='g'); out = {}")
        self.assertEqual(out["_denied"], "DENIED_NOT_COORDINATOR")
        self.assertEqual(out["_exit"], 2)

    def test_non_coordinator_transfer_and_revoke_refused(self):
        out = self.plane("s = make_store(); co.transfer_coordinator(s, 'nOther', summary='t'); out = {}")
        self.assertEqual(out["_denied"], "DENIED_NOT_COORDINATOR")
        out2 = self.plane("s = make_store(); co.revoke_lease(s, 'p/s', summary='rv'); out = {}")
        self.assertEqual(out2["_denied"], "DENIED_NOT_COORDINATOR")

    def test_grant_allowed_after_claim(self):
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c');"
            "g = co.grant_lease(s, 'p/s', s.node_id, summary='g');"
            "out = {'status': g['status'], 'epoch': g['epoch'], 'seq': g['grant_seq']}"
        )
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["epoch"], 1)
        self.assertEqual(out["seq"], 1)

    def test_release_by_stranger_refused(self):
        # A holds the lease (payload.holder = a foreign node); THIS node is not the holder => release
        # refuses DENIED_NOT_HOLDER.
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c');"
            "co.grant_lease(s, 'p/s', 'nSomeoneElse00', summary='g');"
            "co.release_lease(s, 'p/s'); out = {}"
        )
        self.assertEqual(out["_denied"], "DENIED_NOT_HOLDER")
        self.assertEqual(out["_exit"], 2)

    def test_release_nonexistent_lease_refused(self):
        out = self.plane("s = make_store(); co.release_lease(s, 'p/nope'); out = {}")
        self.assertEqual(out["_denied"], "DENIED_LEASE_NOT_HELD")


class GrantSeqTest(_ScratchSubprocess):
    def test_renew_bumps_seq_and_reducer_picks_highest(self):
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c');"
            "g1 = co.grant_lease(s, 'p/s', s.node_id, summary='g');"
            "r1 = co.renew_lease(s, 'p/s', summary='rn');"
            "r2 = co.renew_lease(s, 'p/s', summary='rn2');"
            "lease = co.current_leases(s)['p/s'];"
            "out = {'g1_seq': g1['grant_seq'], 'r1_seq': r1['grant_seq'], 'r2_seq': r2['grant_seq'], 'winner_seq': lease['grant_seq'], 'state': lease['state']}"
        )
        self.assertEqual(out["g1_seq"], 1)
        self.assertEqual(out["r1_seq"], 2)
        self.assertEqual(out["r2_seq"], 3)
        self.assertEqual(out["winner_seq"], 3)  # reducer picks the highest grant_seq at that epoch
        self.assertEqual(out["state"], "held")

    def test_renew_without_active_grant_refused(self):
        out = self.plane("s = make_store(); co.claim_coordinator(s, summary='c'); co.renew_lease(s, 'p/none'); out = {}")
        self.assertEqual(out["_denied"], "DENIED_LEASE_NOT_HELD")


class ExpiryTest(_ScratchSubprocess):
    def test_ttl_grant_expires_and_sweep_is_idempotent(self):
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c');"
            "g = co.grant_lease(s, 'p/s', s.node_id, ttl_s=1, summary='g');"
            "past = g['expires_at'];"
            "future = '2999-01-01T00:00:00+00:00';"
            "reduced = reducers.reduce_lease(s.coord_events(), now=future)['p/s'];"
            "sw1 = co.sweep_expired(s, now=future);"
            "sw2 = co.sweep_expired(s, now=future);"
            "expired_events = [r for r in s.coord_events() if r['event_kind']=='lease' and r['marker']=='expired'];"
            "out = {'state': reduced['state'], 'holder': reduced['holder'], 'sw1': len(sw1['swept']), 'sw2': len(sw2['swept']), 'n_expired': len(expired_events)}"
        )
        self.assertEqual(out["state"], "expired")
        self.assertIsNone(out["holder"])
        self.assertEqual(out["sw1"], 1)  # first sweep materializes one lease/expired
        self.assertEqual(out["sw2"], 0)  # idempotent: second sweep appends nothing
        self.assertEqual(out["n_expired"], 1)

    def test_expiry_releases_scope_for_new_grant(self):
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c');"
            "co.grant_lease(s, 'p/s', s.node_id, ttl_s=1, summary='g');"
            "future = '2999-01-01T00:00:00+00:00';"
            "co.sweep_expired(s, now=future);"
            "# after expiry+sweep the scope is free; a new grant takes it cleanly\n"
            "g2 = co.grant_lease(s, 'p/s', s.node_id, summary='g2');"
            "lease = co.current_leases(s)['p/s'];"
            "out = {'state': lease['state'], 'seq': lease['grant_seq'], 'holder_is_self': lease['holder'] == s.node_id}"
        )
        self.assertEqual(out["state"], "held")
        self.assertTrue(out["holder_is_self"])


class ContestedClaimTest(_ScratchSubprocess):
    def test_contested_claim_records_conflict_and_does_not_displace(self):
        # A (a foreign node, injected as a granted event) holds; THIS node claims => conflict recorded,
        # reducer still shows A, and the return says contested.
        out = self.plane(
            "s = make_store();"
            "# inject a foreign granted holder 'nAAAA' at epoch 1\n"
            "s.append_coord_event('coordinator', 'granted', summary='foreign grant', epoch=1, payload={'mode':'roaming','to_node':'nAAAA00000000'});"
            "r = co.claim_coordinator(s, summary='mine');"
            "coord = co.current_coordinator(s);"
            "conflicts = [c for c in s.coord_events() if c['event_kind']=='conflict' and c['marker']=='detected'];"
            "would = conflicts[0]['payload']['would_block'] if conflicts else None;"
            "out = {'contested': r['contested'], 'status': r['status'], 'holder': coord['holder'], 'would': would, 'n_conflict': len(conflicts)}"
        )
        self.assertTrue(out["contested"])
        self.assertEqual(out["status"], "RECORDED")
        self.assertEqual(out["holder"], "nAAAA00000000")  # reducer keeps the granted holder
        self.assertEqual(out["would"], "CONTESTED_CLAIM")
        self.assertEqual(out["n_conflict"], 1)


class TransferTest(_ScratchSubprocess):
    def test_transfer_moves_holder_and_old_epoch_replay_does_not_displace(self):
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c');"  # self holds epoch 1
            "t = co.transfer_coordinator(s, 'nBBBB00000000', summary='xfer');"
            "coord = co.current_coordinator(s);"
            "# replay an OLD-epoch self-grant (epoch 1) far in the future -- must NOT win over epoch 2\n"
            "s.append_coord_event('coordinator', 'granted', summary='old replay', epoch=1, payload={'mode':'roaming'});"
            "coord2 = co.current_coordinator(s);"
            "out = {'to': t['to_node'], 'epoch': t['epoch'], 'holder': coord['holder'], 'holder2': coord2['holder'], 'epoch2': coord2['epoch']}"
        )
        self.assertEqual(out["to"], "nBBBB00000000")
        self.assertEqual(out["epoch"], 2)
        self.assertEqual(out["holder"], "nBBBB00000000")  # read from payload.to_node
        self.assertEqual(out["holder2"], "nBBBB00000000")  # old-epoch replay did not displace
        self.assertEqual(out["epoch2"], 2)

    def test_transfer_advances_past_the_ledger_high_watermark(self):
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c');"
            "# A higher bare claim is non-authoritative, but still advances the durable epoch watermark.\n"
            "s.append_coord_event('coordinator', 'claimed', summary='contested', epoch=7, payload={'mode':'roaming','to_node':'nOther000000'});"
            "before = co.current_coordinator(s);"
            "t = co.transfer_coordinator(s, 'nBBBB00000000', summary='xfer');"
            "after = co.current_coordinator(s);"
            "out = {'before_holder': before['holder'], 'before_epoch': before['epoch'], 'transfer_epoch': t['epoch'], 'after_holder': after['holder'], 'after_epoch': after['epoch']}"
        )
        self.assertEqual(out["before_epoch"], 1)
        self.assertEqual(out["transfer_epoch"], 8)
        self.assertEqual(out["after_epoch"], 8)
        self.assertEqual(out["after_holder"], "nBBBB00000000")


class EpochFenceEnforcementTest(_ScratchSubprocess):
    def test_stale_assumed_epoch_records_conflict_then_refuses(self):
        # M10b flipped the M10a record-and-proceed: a stale assumed_epoch STILL records the
        # conflict/detected audit event, THEN raises DENIED_STALE_FENCE, and the lease is NOT granted.
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c');"  # current epoch 1
            "denied = None\n"
            "try:\n"
            "    co.grant_lease(s, 'p/s', s.node_id, summary='g', assumed_epoch=0)\n"
            "except KaizenDenied as e:\n"
            "    denied = e.code\n"
            "conflicts = [c for c in s.coord_events() if c['event_kind']=='conflict' and c['marker']=='detected']\n"
            "would = conflicts[0]['payload']['would_block'] if conflicts else None\n"
            "leases = co.current_leases(s)\n"
            "out = {'denied': denied, 'would': would, 'n_conflict': len(conflicts), 'granted': 'p/s' in leases}\n"
        )
        self.assertEqual(out["denied"], "DENIED_STALE_FENCE")  # M10b: enforcement REFUSES
        self.assertEqual(out["would"], "DENIED_STALE_FENCE")  # the audit event was still written first
        self.assertEqual(out["n_conflict"], 1)
        self.assertFalse(out["granted"])  # the grant did NOT happen (refused before the append)

    def test_matching_assumed_epoch_records_no_conflict(self):
        out = self.plane(
            "s = make_store(); co.claim_coordinator(s, summary='c');"
            "co.grant_lease(s, 'p/s', s.node_id, summary='g', assumed_epoch=1);"  # matches current
            "conflicts = [c for c in s.coord_events() if c['event_kind']=='conflict'];"
            "out = {'n_conflict': len(conflicts)}"
        )
        self.assertEqual(out["n_conflict"], 0)


class ShadowHandoffTest(_ScratchSubprocess):
    def test_default_flags_write_patch_artifact_no_push_no_commit(self):
        out = self.plane(
            "repo = make_repo(); art = tempfile.mkdtemp(prefix='kz-art-');"
            "s = make_store(); co.claim_coordinator(s, summary='c'); co.grant_lease(s, 'p/main', s.node_id, summary='g');"
            "rec = co.HandoffEngine(s, repo, artifact_dir=art).shadow_handoff('p/main');"
            "markers = [r['marker'] for r in s.coord_events() if r['event_kind']=='handoff'];"
            "out = {'status': rec['status'], 'markers': markers, 'artifact': bool(rec['artifact']), 'pushed_sha': rec['pushed_sha'], 'loglines': git_loglines(repo)};"
            "shutil.rmtree(repo, ignore_errors=True); shutil.rmtree(art, ignore_errors=True)"
        )
        self.assertEqual(out["status"], "OK")
        # started -> released -> completed in order, NO pushed event.
        self.assertEqual(out["markers"], ["started", "released", "completed"])
        self.assertTrue(out["artifact"])
        self.assertIsNone(out["pushed_sha"])
        self.assertEqual(out["loglines"], 1)  # no commit made (git log unchanged)

    def test_allow_flags_commit_and_push_confirmed(self):
        out = self.plane(
            "repo = make_repo(); hub = make_hub();"
            "s = make_store(); co.claim_coordinator(s, summary='c'); co.grant_lease(s, 'p/feat', s.node_id, summary='g');"
            "Path(repo, 'b.txt').write_text('change', encoding='utf-8');"
            "rec = co.HandoffEngine(s, repo, hub_remote=hub, allow_wip_commit=True, allow_push=True).shadow_handoff('p/feat');"
            "markers = [r['marker'] for r in s.coord_events() if r['event_kind']=='handoff'];"
            "out = {'status': rec['status'], 'markers': markers, 'wip_commit': bool(rec['wip_commit']), 'pushed_sha': bool(rec['pushed_sha']), 'loglines': git_loglines(repo)};"
            "shutil.rmtree(repo, ignore_errors=True); shutil.rmtree(hub, ignore_errors=True)"
        )
        self.assertEqual(out["status"], "OK")
        self.assertIn("pushed", out["markers"])  # a real, confirmed push emitted handoff/pushed
        self.assertTrue(out["wip_commit"])
        self.assertTrue(out["pushed_sha"])
        self.assertEqual(out["loglines"], 2)  # WIP commit exists on the branch

    def test_dirty_non_holder_records_divergence_without_raising(self):
        out = self.plane(
            "repo = make_repo(); art = tempfile.mkdtemp(prefix='kz-art-');"
            "s = make_store(); co.claim_coordinator(s, summary='c');"
            "# grant to a FOREIGN holder so THIS node is a non-holder; then dirty the tree\n"
            "co.grant_lease(s, 'p/main', 'nForeign00000', summary='g');"
            "Path(repo, 'dirty.txt').write_text('uncommitted', encoding='utf-8');"
            "rec = co.HandoffEngine(s, repo, artifact_dir=art).shadow_handoff('p/main');"
            "divs = [d for d in s.coord_events() if d['event_kind']=='divergence' and d['marker']=='detected'];"
            "reasons = [d['payload']['would_block'] for d in divs];"
            "out = {'status': rec['status'], 'reasons': reasons, 'divergences': rec['divergences']};"
            "shutil.rmtree(repo, ignore_errors=True); shutil.rmtree(art, ignore_errors=True)"
        )
        self.assertEqual(out["status"], "OK")  # sequence completed; nothing raised
        self.assertIn("DIRTY_NON_HOLDER", out["reasons"])
        self.assertIn("DIRTY_NON_HOLDER", out["divergences"])

    def test_step_exception_aborts_truthfully(self):
        out = self.plane(
            "repo = tempfile.mkdtemp(prefix='kz-x-')\n"
            "s = make_store()\n"
            "co.claim_coordinator(s, summary='c')\n"
            "co.grant_lease(s, 'p/x', s.node_id, summary='g')\n"
            "def bad(*a, **k):\n"
            "    raise RuntimeError('injected git failure')\n"
            "rec = co.HandoffEngine(s, repo, git_runner=bad).shadow_handoff('p/x')\n"
            "markers = [r['marker'] for r in s.coord_events() if r['event_kind']=='handoff']\n"
            "out = {'status': rec['status'], 'failed_step': rec.get('failed_step'), 'markers': markers, 'has_aborted_id': 'aborted_id' in rec}\n"
            "shutil.rmtree(repo, ignore_errors=True)\n"
        )
        self.assertEqual(out["status"], "ABORTED")
        self.assertEqual(out["failed_step"], "snapshot")  # dies at the first git call
        self.assertEqual(out["markers"], ["started", "aborted"])
        self.assertTrue(out["has_aborted_id"])


class VocabularyDisciplineTest(_ScratchSubprocess):
    def test_every_emitted_kind_marker_is_registered(self):
        # Drive the full engine surface, then assert every emitted (kind, marker) is in the registry
        # matrix -- M10a adds PRODUCERS only, never a new (kind, marker) pair.
        out = self.plane(
            "from kaizen_components.schemas.registry import COORD_EVENT_KIND_MARKERS as M\n"
            "repo = make_repo(); art = tempfile.mkdtemp(prefix='kz-art-')\n"
            "s = make_store()\n"
            "co.claim_coordinator(s, summary='c')\n"
            "co.request_lease(s, 'p/main', summary='r')\n"
            "co.grant_lease(s, 'p/main', s.node_id, summary='g')\n"
            "co.renew_lease(s, 'p/main', summary='rn')\n"
            "# A stale-fence attempt RECORDS the conflict/detected then REFUSES (M10b); catch it so the\n"
            "# drive continues -- the conflict family is still produced by the pre-raise audit append.\n"
            "try:\n"
            "    co.grant_lease(s, 'p/other', s.node_id, summary='g2', assumed_epoch=0)\n"
            "except KaizenDenied:\n"
            "    pass\n"
            "co.grant_lease(s, 'p/other', s.node_id, summary='g2b')\n"  # a real grant (fresh fence)
            "co.revoke_lease(s, 'p/other', summary='rv')\n"
            "co.transfer_coordinator(s, 'nZZZZ00000000', summary='xfer')\n"
            "co.HandoffEngine(s, repo, artifact_dir=art).shadow_handoff('p/main')\n"
            "pairs = sorted({(r['event_kind'], r['marker']) for r in s.coord_events()})\n"
            "bad = [p for p in pairs if p[0] not in M or p[1] not in M[p[0]]]\n"
            "out = {'pairs': pairs, 'bad': bad}\n"
            "shutil.rmtree(repo, ignore_errors=True); shutil.rmtree(art, ignore_errors=True)\n"
        )
        self.assertEqual(out["bad"], [], f"unregistered (kind, marker) pairs emitted: {out['bad']}")
        # sanity: the drive really produced coordinator + lease + handoff + conflict families.
        kinds = {p[0] for p in out["pairs"]}
        for kind in ("coordinator", "lease", "handoff", "conflict"):
            self.assertIn(kind, kinds)


if __name__ == "__main__":
    unittest.main()
