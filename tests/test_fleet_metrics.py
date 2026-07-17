"""Built-in fleet metrics + health surfaces (v8 M17, plan §5.3 M17 / ledger #15).

Four legs, hermetic:

1. **Pure metric projections** (`fleet/metrics.py`) over seeded event lists -- heartbeat ages,
   replica-view sync staleness, open conflict spans, orphan-sweep reclamations (plain expiries
   excluded), dispatch requested→terminal latency (open dispatches counted, terminal-without-request
   ignored).
2. **Bundle** over a real scratch FleetStore: unsynced store has NO sync_stats key (metrics never
   invent sync state); counts/epoch present.
3. **`GET /v1/health`** on the control service (unsigned read-only, same trust as /v1/probe): status,
   node identity, uptime, event count, staleness block; endpoint advertised by /v1/probe.
4. **Daemon loopback `fleet/metrics`** (stats() via daemon only -- the §5.3 wording): the supervisor
   serves the bundle over its held handle (subprocess scratch-plane, test_reconcile convention).

Deploy assets (setup/deploy/) are validated STATICALLY here -- structure/fields only, no docker
execution (PR-lane hermeticity; the lab/dry-run legs live outside unittest).
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

from kaizen_components.fleet import metrics  # noqa: E402
from kaizen_components.fleet.store import FleetStore  # noqa: E402

from test_control_http import _ServiceHarness  # noqa: E402
from kaizen_components.fleet import control_http  # noqa: E402

_NOW = "2026-07-10T12:00:00+00:00"


def _ev(kind, marker, *, node="nA0000000000001", created="2026-07-10T11:59:00+00:00", scope=None, epoch=None, payload=None):
    # id must be UNIQUE per event (the reducers' canonical pass dedupes by id) -- bind the full
    # node + timestamp + scope into it.
    return {
        "id": f"ce_{kind}_{marker}_{node}_{created}_{scope}", "created_at": created, "node_id": node,
        "project_id": None, "event_kind": kind, "marker": marker, "scope_key": scope,
        "epoch": epoch, "payload": payload,
    }


# --- 1. pure projections ---------------------------------------------------------------------------

class HeartbeatAgesTest(unittest.TestCase):
    def test_ages_from_reduced_heartbeats_unknown_omitted(self) -> None:
        events = [
            _ev("node", "registered", node="nA0000000000001", payload={"role": "worker", "tailnet_name": "a"}),
            _ev("heartbeat", "point", node="nA0000000000001", created="2026-07-10T11:59:00+00:00", payload={"ts": "2026-07-10T11:59:00+00:00"}),
            _ev("node", "registered", node="nB0000000000002", payload={"role": "worker", "tailnet_name": "b"}),
            # nB registered but NEVER heartbeat -> omitted (unknown is not zero).
        ]
        ages = metrics.heartbeat_ages(events, _NOW)
        self.assertAlmostEqual(ages["nA0000000000001"], 60.0, places=1)
        self.assertNotIn("nB0000000000002", ages)


class SyncStalenessTest(unittest.TestCase):
    def test_foreign_and_local_ages(self) -> None:
        events = [
            _ev("heartbeat", "point", node="nSelf0000000001", created="2026-07-10T11:58:00+00:00", payload={"ts": "x"}),
            _ev("heartbeat", "point", node="nPeer0000000002", created="2026-07-10T11:50:00+00:00", payload={"ts": "x"}),
        ]
        s = metrics.sync_staleness(events, "nSelf0000000001", _NOW)
        self.assertAlmostEqual(s["newest_local_event_age_s"], 120.0, places=1)
        self.assertAlmostEqual(s["newest_foreign_event_age_s"], 600.0, places=1)

    def test_single_node_plane_has_no_foreign_age(self) -> None:
        events = [_ev("heartbeat", "point", node="nSelf0000000001", payload={"ts": "x"})]
        s = metrics.sync_staleness(events, "nSelf0000000001", _NOW)
        self.assertIsNone(s["newest_foreign_event_age_s"])


class LeaseConflictsTest(unittest.TestCase):
    def test_open_spans_counted_with_oldest_age(self) -> None:
        events = [
            _ev("conflict", "detected", created="2026-07-10T11:00:00+00:00", scope="p/a", payload={"decision": "fork on p/a"}),
            _ev("conflict", "detected", created="2026-07-10T11:30:00+00:00", scope="p/b", payload={"decision": "fork on p/b"}),
        ]
        c = metrics.lease_conflicts(events, _NOW)
        self.assertEqual(c["open_count"], 2)
        self.assertAlmostEqual(c["oldest_open_age_s"], 3600.0, places=1)

    def test_no_conflicts(self) -> None:
        c = metrics.lease_conflicts([], _NOW)
        self.assertEqual(c["open_count"], 0)
        self.assertIsNone(c["oldest_open_age_s"])


class OrphanSweepsTest(unittest.TestCase):
    def test_only_reclamations_counted_not_plain_expiries(self) -> None:
        events = [
            _ev("lease", "expired", scope="p/a", epoch=1, payload={"grant_seq": 1}),  # plain expiry sweep
            _ev("lease", "expired", scope="p/b", epoch=1, created="2026-07-10T11:10:00+00:00",
                payload={"grant_seq": 1, "reclaimed_from": "nDead0000000001", "reason": "heartbeat-stale", "age_s": 999.0}),
            _ev("lease", "expired", scope="p/c", epoch=2, created="2026-07-10T11:20:00+00:00",
                payload={"grant_seq": 1, "reclaimed_from": "nDead0000000001", "reason": "heartbeat-stale", "age_s": 999.0}),
        ]
        o = metrics.orphan_sweeps(events)
        self.assertEqual(o["reclaimed_total"], 2)
        self.assertEqual(o["by_node"], {"nDead0000000001": 2})
        self.assertEqual(o["last_at"], "2026-07-10T11:20:00+00:00")


class DispatchLatencyTest(unittest.TestCase):
    def test_terminal_latency_and_open_count(self) -> None:
        events = [
            _ev("dispatch", "requested", created="2026-07-10T11:00:00+00:00", scope="p/a", payload={"dispatch_id": "d1", "target_node": "nB"}),
            _ev("dispatch", "completed", created="2026-07-10T11:00:30+00:00", scope="p/a", payload={"dispatch_id": "d1"}),
            _ev("dispatch", "requested", created="2026-07-10T11:01:00+00:00", scope="p/b", payload={"dispatch_id": "d2", "target_node": "nB"}),
            _ev("dispatch", "failed", created="2026-07-10T11:02:30+00:00", scope="p/b", payload={"dispatch_id": "d2", "reason": "x"}),
            _ev("dispatch", "requested", created="2026-07-10T11:03:00+00:00", scope="p/c", payload={"dispatch_id": "d3", "target_node": "nB"}),  # open
            _ev("dispatch", "canceled", created="2026-07-10T11:04:00+00:00", scope="p/d", payload={"dispatch_id": "dGhost"}),  # terminal w/o request -> ignored
        ]
        d = metrics.dispatch_latency(events)
        self.assertEqual(d["terminal_count"], 2)
        self.assertAlmostEqual(d["avg_s"], 60.0, places=1)   # (30 + 90) / 2
        self.assertAlmostEqual(d["max_s"], 90.0, places=1)
        self.assertEqual(d["open_count"], 1)

    def test_empty(self) -> None:
        d = metrics.dispatch_latency([])
        self.assertEqual(d["terminal_count"], 0)
        self.assertIsNone(d["avg_s"])
        self.assertEqual(d["open_count"], 0)


# --- 2. bundle over a real scratch store -----------------------------------------------------------

class FleetMetricsBundleTest(unittest.TestCase):
    def test_bundle_over_unsynced_store_has_no_sync_stats(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="kaizen-m17-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        store = FleetStore(db_path=root / "fleet.db", node={"node_id": "nM17A00000001", "created_at": "t"})
        self.addCleanup(store.close)
        store.register_node("worker", tailnet_name="m17.tailtest.ts.net")
        store.heartbeat()
        bundle = metrics.fleet_metrics(store, now=_NOW)
        self.assertEqual(bundle["status"], "OK")
        self.assertEqual(bundle["node_id"], "nM17A00000001")
        self.assertIn("nM17A00000001", bundle["heartbeat_ages_s"])
        self.assertGreaterEqual(bundle["coord_events"], 2)
        self.assertNotIn("sync_stats", bundle)  # unsynced: metrics never invent sync state


# --- 3. GET /v1/health -----------------------------------------------------------------------------

class HealthEndpointTest(_ServiceHarness):
    def test_health_is_unsigned_readonly_and_advertised(self) -> None:
        health = control_http._get(self.base, "/v1/health", 5.0)
        self.assertEqual(health.get("status"), "OK")
        self.assertEqual(health.get("node_id"), self.server_ident["node_id"])
        self.assertGreaterEqual(health.get("uptime_s"), 0.0)
        self.assertIn("coord_events", health)
        self.assertIn("sync_staleness", health)
        probe = control_http._get(self.base, "/v1/probe", 5.0)
        self.assertIn("GET /v1/health", probe["endpoints"])

    def test_health_appends_nothing(self) -> None:
        before = len(self.store.coord_events())
        control_http._get(self.base, "/v1/health", 5.0)
        self.assertEqual(len(self.store.coord_events()), before)


# --- 4. daemon loopback fleet/metrics ---------------------------------------------------------------

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


class SupervisorMetricsLoopbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-m17-sup-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.assertEqual(kaizen(self.root, "K1")[0], 0)

    def drive(self, body: str) -> dict:
        script = "BODY = " + repr(body) + "\n" + _SUP_PREAMBLE
        env = dict(os.environ)
        env["KAIZEN_REPO_ROOT"] = str(self.root)
        env["KAIZEN_DIST_MODE"] = "observe"
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=120
        )
        for line in (proc.stdout or "").splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        raise AssertionError(f"no RESULT line: stdout={proc.stdout!r} stderr={proc.stderr[-800:]!r}")

    def test_metrics_served_over_daemon_handle(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "sup._fleet.heartbeat()\n"
            "resp = sup._handle_control({'op': 'fleet/metrics', 'args': {}})\n"
            "sup.shutdown()\n"
            "out = {'resp': resp}\n"
        )
        resp = out["resp"]
        self.assertEqual(resp["status"], "OK")
        self.assertIn("heartbeat_ages_s", resp)
        self.assertIn("dispatch_latency", resp)
        self.assertIn("lease_conflicts", resp)


# --- R0 advisory metrics key --------------------------------------------------------------------------

_ACTIVE = {"KAIZEN_DIST_MODE": "active"}


class R0MetricsAdvisoryTest(IsolatedDBTest):
    def test_r0_fleet_section_carries_metrics(self) -> None:
        # M17: the R0 fleet section gains an advisory 'metrics' block (heartbeat ages, staleness,
        # conflicts, sweeps, dispatch latency) riding the same never-break-R0 posture as the digest.
        self.assertEqual(self.kz("D1", "--payload-json", '{"role":"worker"}', "--summary", "N.", env=_ACTIVE)[0], 0)
        self.assertEqual(self.kz("D2", "--summary", "Beat.", env=_ACTIVE)[0], 0)
        rc, p = self.kz("R0", env=_ACTIVE)
        self.assertEqual(rc, 0, p)
        m = p.get("fleet", {}).get("metrics", {})
        self.assertEqual(m.get("status"), "OK", m)
        self.assertEqual(len(m.get("heartbeat_ages_s", {})), 1, m)
        self.assertIn("dispatch_latency", m)
        self.assertIn("lease_conflicts", m)
        self.assertIn("orphan_sweeps", m)

    def test_r0_off_mode_still_has_no_fleet_key(self) -> None:
        rc, p = self.kz("R0")
        self.assertEqual(rc, 0, p)
        self.assertNotIn("fleet", p)


# --- deploy assets: static validation only ----------------------------------------------------------

DEPLOY = Path(REPO_ROOT) / "setup" / "deploy"


class DeployAssetsStaticTest(unittest.TestCase):
    # Host-persistence assets (Windows scheduled task, systemd unit) were removed 2026-07-10 by owner
    # policy: no machine-level persistence ships in any scenario.
    def test_compose_hub_pair_shape(self) -> None:
        text = (DEPLOY / "docker-compose.hub.yml").read_text(encoding="utf-8")
        self.assertIn("services:", text)
        self.assertIn("sync:", text)
        self.assertIn("git:", text)
        # Self-contained builds only -- no invented registry images.
        self.assertIn("build:", text)
        self.assertNotIn("image: turso", text)
        for dockerfile in ("hub-sync.Dockerfile", "hub-git.Dockerfile"):
            self.assertTrue((DEPLOY / dockerfile).is_file(), dockerfile)

    def test_deploy_readme_present(self) -> None:
        text = (DEPLOY / "README.md").read_text(encoding="utf-8")
        self.assertIn("idempoten", text.casefold())
        self.assertIn("tailscale", text.casefold())


if __name__ == "__main__":
    unittest.main()
