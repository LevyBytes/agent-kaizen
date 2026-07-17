"""Fleet D-ops (v8 M9 / D1/D2/D8) through the real CLI + R0 fleet-section gating.

Satisfies the op-coverage matrix (self.kz("D1"/"D2"/"D8", ...)) and proves:
- register -> heartbeat -> digest round-trips under KAIZEN_DIST_MODE=active (digest shows the node with
  a fresh heartbeat age);
- mode-off refusal (DENIED_DIST_MODE_OFF, exit 2) keeps the default inert;
- R0 in off mode has NO 'fleet' key; R0 with mode=active + fleet.db present carries the fleet section;
- the _route decision (loopback vs break-glass) is driven purely by daemon liveness (fake send_request
  injected -- no daemon started, no network).

README/PURPOSES parity and dispatch-every-alias are covered by test_cli_wiring.py; op-coverage by
test_op_coverage.py. This file only exercises the ops so those matrices see D1/D2/D8.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import IsolatedDBTest  # noqa: E402

_ACTIVE = {"KAIZEN_DIST_MODE": "active"}


class FleetOpsCliTest(IsolatedDBTest):
    def test_d1_register_d2_heartbeat_d8_digest_round_trip(self):
        rc, p = self.kz("D1", "--payload-json", '{"role":"worker"}', "--summary", "This node.", env=_ACTIVE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        self.assertEqual(p.get("role"), "worker", p)
        self.assertEqual(p.get("marker"), "registered", p)

        rc, p = self.kz("D2", env=_ACTIVE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        self.assertTrue(p.get("ts"), p)

        rc, p = self.kz("D8", env=_ACTIVE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        self.assertEqual(len(p.get("nodes", [])), 1, p)
        node = p["nodes"][0]
        self.assertEqual(node["role"], "worker", p)
        self.assertIsNotNone(node["heartbeat_age_s"], p)
        self.assertGreaterEqual(p["counts"]["coord_events"], 2, p)

    def test_d1_second_call_is_update_marker(self):
        self.assertEqual(self.kz("D1", "--payload-json", '{"role":"worker"}', "--summary", "N.", env=_ACTIVE)[0], 0)
        rc, p = self.kz("D1", "--payload-json", '{"role":"coordinator"}', "--summary", "N.", env=_ACTIVE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("marker"), "updated", p)
        self.assertEqual(p.get("role"), "coordinator", p)

    def test_d1_off_mode_refused(self):
        rc, p = self.kz("D1", "--payload-json", '{"role":"worker"}', "--summary", "N.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_DIST_MODE_OFF", p)

    def test_d2_off_mode_refused(self):
        rc, p = self.kz("D2")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_DIST_MODE_OFF", p)

    def test_d8_off_mode_refused(self):
        rc, p = self.kz("D8")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_DIST_MODE_OFF", p)

    def test_d1_requires_role(self):
        rc, p = self.kz("D1", "--summary", "no role given.", env=_ACTIVE)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_NODE_ROLE_REQUIRED", p)

    def test_d1_rejects_bad_role_enum(self):
        rc, p = self.kz("D1", "--payload-json", '{"role":"overlord"}', "--summary", "N.", env=_ACTIVE)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_SCHEMA_ENUM", p)

    def test_d8_off_mode_creates_no_fleet_db(self):
        """Off-mode refusal must not create fleet.db (off-unchanged invariant)."""
        self.kz("D8")
        self.assertFalse((self.root / "AI" / "db" / "fleet.db").exists())


class CoordinationOpsCliTest(IsolatedDBTest):
    """D4/D5 CLI round-trip (M10a shadow) under KAIZEN_DIST_MODE=active. Satisfies op-coverage for
    D4/D5; the engine semantics are proven in test_fleet_coordination.py."""

    def test_d4_claim_then_d5_grant_release_round_trip(self):
        rc, p = self.kz("D4", "--action", "claim", "--summary", "Claim coordinator.", env=_ACTIVE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        self.assertEqual(p.get("epoch"), 1, p)
        holder = p.get("holder")
        self.assertTrue(holder, p)

        # Grant a lease to self (this node is the coordinator now).
        rc, p = self.kz("D5", "--action", "grant", "--scope", "p/main",
                        "--payload-json", json.dumps({"holder": holder}), "--summary", "Grant.", env=_ACTIVE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        self.assertEqual(p.get("grant_seq"), 1, p)

        # Release (holder-side).
        rc, p = self.kz("D5", "--action", "release", "--scope", "p/main", "--summary", "Release.", env=_ACTIVE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)

    def test_d5_request_is_unordered_and_needs_no_coordinator(self):
        # A lease REQUEST is append-only + unordered: any node may request without being coordinator.
        rc, p = self.kz("D5", "--action", "request", "--scope", "p/feat", "--summary", "Request.", env=_ACTIVE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        self.assertEqual(p.get("scope_key"), "p/feat", p)

    def test_d5_handoff_shadow_records_steps(self):
        # Shadow handoff runs against REPO_ROOT (the scratch plane, no git) and degrades cleanly:
        # pure-shadow, no commit/push, steps recorded, never raises.
        self.assertEqual(self.kz("D4", "--action", "claim", "--summary", "C.", env=_ACTIVE)[0], 0)
        holder = self.kz("D8", env=_ACTIVE)[1]["coordinator"]["holder"]
        self.kz("D5", "--action", "grant", "--scope", "p/main",
                "--payload-json", json.dumps({"holder": holder}), "--summary", "G.", env=_ACTIVE)
        rc, p = self.kz("D5", "--action", "handoff", "--scope", "p/main", "--summary", "Handoff.", env=_ACTIVE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        self.assertIn("started", p.get("steps", []), p)
        self.assertIn("completed", p.get("steps", []), p)

    def test_d4_grantor_only_transfer_refused_before_claim(self):
        # transfer without holding the role => DENIED_NOT_COORDINATOR (exit 2), surfaced via the CLI.
        rc, p = self.kz("D4", "--action", "transfer",
                        "--payload-json", json.dumps({"to_node": "nOther000000"}), "--summary", "T.", env=_ACTIVE)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_NOT_COORDINATOR", p)

    def test_d4_off_mode_refused(self):
        rc, p = self.kz("D4", "--action", "claim", "--summary", "C.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_DIST_MODE_OFF", p)

    def test_d5_off_mode_refused(self):
        rc, p = self.kz("D5", "--action", "request", "--scope", "p/x", "--summary", "R.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_DIST_MODE_OFF", p)

    def test_d5_requires_scope(self):
        rc, p = self.kz("D5", "--action", "request", "--summary", "no scope.", env=_ACTIVE)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_LEASE_SCOPE_REQUIRED", p)


class R0FleetSectionTest(IsolatedDBTest):
    def test_r0_off_mode_has_no_fleet_key(self):
        rc, p = self.kz("R0")
        self.assertEqual(rc, 0, p)
        self.assertNotIn("fleet", p)

    def test_r0_active_with_fleet_db_has_section(self):
        # Create fleet.db via a register, then R0 (active) carries the fleet digest section.
        self.assertEqual(self.kz("D1", "--payload-json", '{"role":"worker"}', "--summary", "N.", env=_ACTIVE)[0], 0)
        rc, p = self.kz("R0", env=_ACTIVE)
        self.assertEqual(rc, 0, p)
        self.assertIn("fleet", p)
        self.assertEqual(p["fleet"].get("status"), "OK", p["fleet"])
        self.assertEqual(len(p["fleet"].get("nodes", [])), 1, p["fleet"])

    def test_r0_off_mode_absent_even_after_fleet_db_exists(self):
        """Once fleet.db exists, an OFF-mode R0 STILL omits the key entirely (mode gates, not the file)."""
        self.assertEqual(self.kz("D1", "--payload-json", '{"role":"worker"}', "--summary", "N.", env=_ACTIVE)[0], 0)
        rc, p = self.kz("R0")  # off
        self.assertEqual(rc, 0, p)
        self.assertNotIn("fleet", p)


class RouteDecisionTest(unittest.TestCase):
    """The _route loopback-vs-break-glass decision is driven purely by daemon liveness. Injected fake
    send_request proves the loopback leg is chosen when a daemon is 'live' (no daemon actually started,
    no socket opened). Both legs run in a scratch-plane SUBPROCESS so no sys.modules surgery leaks into
    other in-process suites."""

    def setUp(self) -> None:
        import shutil
        import tempfile

        self.root = Path(tempfile.mkdtemp(prefix="kaizen-route-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        from _harness import kaizen

        self.assertEqual(kaizen(self.root, "K1")[0], 0)

    def _run(self, body: str) -> dict:
        import json
        import os
        import subprocess

        from _harness import REPO_ROOT

        script = "BODY = " + repr(body) + "\n" + _ROUTE_PREAMBLE
        env = dict(os.environ)
        env["KAIZEN_REPO_ROOT"] = str(self.root)
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=60
        )
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT "):
                result = json.loads(line[len("RESULT "):])
                self.assertIsNotNone(result, "route body did not assign out")
                return result
        self.fail(f"no RESULT.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")

    def test_route_uses_loopback_when_daemon_live(self):
        out = self._run(
            "from kaizen_components.fleet import records, store\n"
            "store.daemon_is_live = lambda: True\n"
            "captured = {}\n"
            "def fake_send(op, wire):\n"
            "    captured['op'] = op; captured['wire'] = wire\n"
            "    return {'status': 'OK', 'via': 'loopback', 'op': op}\n"
            "res = records._route('fleet/heartbeat', {'run': lambda s: {'unreached': True}, 'wire': {'k': 'v'}}, send_request=fake_send)\n"
            "out = {'captured': captured, 'via': res.get('via'), 'unreached': 'unreached' in res}\n"
        )
        self.assertEqual(out["captured"]["op"], "fleet/heartbeat")
        self.assertEqual(out["captured"]["wire"], {"k": "v"})
        self.assertEqual(out["via"], "loopback")
        self.assertFalse(out["unreached"])  # break-glass run() was NOT called

    def test_route_uses_break_glass_when_daemon_dead(self):
        out = self._run(
            "from kaizen_components.fleet import records, store\n"
            "store.daemon_is_live = lambda: False\n"
            "ran = {}\n"
            "def run(s):\n"
            "    ran['hit'] = True; return {'status': 'OK'}\n"
            "res = records._route('fleet/heartbeat', {'run': run, 'wire': {}})\n"
            "out = {'hit': ran.get('hit', False), 'via': res.get('via')}\n"
        )
        self.assertTrue(out["hit"])
        self.assertEqual(out["via"], "break-glass")


_ROUTE_PREAMBLE = r"""
import json
out = None
exec(BODY)
print("RESULT " + json.dumps(out))
"""


if __name__ == "__main__":
    unittest.main()
