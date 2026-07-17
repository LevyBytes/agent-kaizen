"""Fleet core (v8 M9): identity, FleetStore, redaction pass-path, watermark, guards, zero-drift.

Every leg runs in a SUBPROCESS with KAIZEN_REPO_ROOT pinned to a fresh scratch plane (no network, no
tursodb, sync_url=None everywhere). Subprocess isolation is deliberate: constructing FleetStore
in-process would require reloading kaizen_components (paths.REPO_ROOT is import-frozen), and that
sys.modules surgery pollutes other in-process suites (trace/backends monkeypatches). A subprocess
carries its own module state and its own REPO_ROOT, so nothing leaks. The manifest legs run the real
CLI directly. Pure-reducer convergence lives in test_fleet_reducers.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import REPO_ROOT, kaizen  # noqa: E402

_HEX16 = re.compile(r"^[0-9a-f]{16}$")

# Runs inside the scratch-plane subprocess: exposes a FleetStore builder + a JSON emitter, then
# execs the per-test BODY, which must set `out` (a JSON-able dict). Denials are caught and surfaced as
# {"_denied": code, "_exit": exit_code} so tests can assert on the refusal without a nonzero exit muddying the channel.
_PREAMBLE = r"""
import json
from kaizen_components.fleet import identity, reducers
from kaizen_components.fleet import sync as fleet_sync
from kaizen_components.fleet.store import FleetStore, daemon_is_live, open_store_breakglass
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
    """A fresh temp KAIZEN_REPO_ROOT with kaizen.db initialized (K1). Test bodies run in a subprocess
    pinned to that plane via :meth:`plane`."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-fleet-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        rc, payload = kaizen(self.root, "K1")
        self.assertEqual(rc, 0, f"K1 init failed: {payload}")

    def plane(self, body: str, *, env: dict | None = None) -> dict:
        """Run ``body`` (which sets ``out``) in a subprocess against the scratch plane; return the
        parsed ``out`` dict (or ``{"_denied": ...}`` when the body raised KaizenDenied)."""
        script = "BODY = " + repr(body) + "\n" + _PREAMBLE
        full_env = dict(_base_env())
        full_env["KAIZEN_REPO_ROOT"] = str(self.root)
        if env:
            full_env.update(env)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, cwd=str(REPO_ROOT), env=full_env, timeout=60,
        )
        marker = "RESULT "
        for line in proc.stdout.splitlines():
            if line.startswith(marker):
                return json.loads(line[len(marker):])
        self.fail(f"no RESULT from subprocess.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


def _base_env() -> dict:
    import os

    return dict(os.environ)


class IdentityTest(_ScratchSubprocess):
    def test_node_identity_mint_once_and_reload(self):
        out = self.plane(
            "a = identity.load_or_mint_node_identity();"
            "b = identity.load_or_mint_node_identity();"
            "from kaizen_components.paths import NODE_IDENTITY_PATH;"
            "out = {'a': a, 'same': a['node_id'] == b['node_id'], 'file': NODE_IDENTITY_PATH.is_file()}"
        )
        self.assertTrue(out["a"]["node_id"].startswith("n"))
        self.assertIn("created_at", out["a"])
        self.assertIn("minted_by_tool_version", out["a"])
        self.assertTrue(out["same"])
        self.assertTrue(out["file"])

    def test_project_id_file_beats_git_and_is_16_hex(self):
        (self.root / "AI").mkdir(parents=True, exist_ok=True)
        (self.root / "AI" / "project.id").write_text("my-canonical-project\n", encoding="utf-8")
        out = self.plane("r = identity.project_id(); out = {'r': r, 'again': identity.project_id()['project_id']}")
        self.assertEqual(out["r"]["source"], "project-id-file")
        self.assertTrue(_HEX16.match(out["r"]["project_id"]))
        self.assertEqual(out["again"], out["r"]["project_id"])  # deterministic

    def test_project_id_repo_path_fallback_in_gitless_scratch(self):
        # Block git from walking up into the real repo tree so a bare scratch resolves via repo-path.
        out = self.plane(
            "out = identity.project_id()",
            env={"GIT_CEILING_DIRECTORIES": str(self.root.parent)},
        )
        self.assertEqual(out["source"], "repo-path")
        self.assertTrue(_HEX16.match(out["project_id"]))

    def test_project_id_prefers_committed_marker_over_git_remote(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git not available")
        subprocess.run([git, "init", "-q"], cwd=str(self.root), check=True, capture_output=True, timeout=30)
        subprocess.run([git, "remote", "add", "origin", "https://example.com/acme/widgets.git"],
                       cwd=str(self.root), check=True, capture_output=True, timeout=30)
        out = self.plane(
            "remote = identity.project_id();"
            "out = {'remote': remote, 'expected': identity._sha16('https://example.com/acme/widgets')}",
            env={"GIT_CEILING_DIRECTORIES": str(self.root.parent)},
        )
        self.assertEqual(out["remote"]["source"], "git-remote")
        self.assertEqual(out["remote"]["project_id"], out["expected"])
        # Now drop the marker file: it must win over the remote.
        (self.root / "AI").mkdir(exist_ok=True)
        (self.root / "AI" / "project.id").write_text("pinned-id", encoding="utf-8")
        out2 = self.plane("out = identity.project_id()", env={"GIT_CEILING_DIRECTORIES": str(self.root.parent)})
        self.assertEqual(out2["source"], "project-id-file")
        self.assertNotEqual(out2["project_id"], out["remote"]["project_id"])

    def test_coord_id_is_node_tagged_and_unique(self):
        out = self.plane(
            "nid = 'nabc123def456ghi7';"
            "ids = sorted({identity.coord_id('ce', nid) for _ in range(500)});"
            "out = {'unique': len(ids) == 500, 'tagged': all(nid[-6:] in c and c.startswith('ce_') for c in ids)}"
        )
        self.assertTrue(out["unique"], "coord_id collided")
        self.assertTrue(out["tagged"])

    def test_coord_id_uses_full_uuid_tail(self):
        out = self.plane(
            "from types import SimpleNamespace;"
            "nid = 'nabc123def456ghi7';"
            "tails = iter(['abcdef' + '0' * 26, 'abcdef' + '1' * 26]);"
            "original = identity.uuid.uuid4;"
            "identity.uuid.uuid4 = lambda: SimpleNamespace(hex=next(tails));"
            "a = identity.coord_id('ce', nid);"
            "b = identity.coord_id('ce', nid);"
            "identity.uuid.uuid4 = original;"
            "parts = [value.split('_') for value in (a, b)];"
            "out = {'different': a != b,"
            "       'format': all(len(p) == 3 and p[0] == 'ce' and len(p[1]) == 14 and p[1].isdigit() for p in parts),"
            "       'tails': [p[2] for p in parts], 'tag': nid[-6:]}"
        )
        self.assertTrue(out["different"])
        self.assertTrue(out["format"])
        self.assertEqual(
            out["tails"],
            [out["tag"] + "abcdef" + "0" * 26, out["tag"] + "abcdef" + "1" * 26],
        )


class FleetStoreRoundTripTest(_ScratchSubprocess):
    def test_create_applies_fleet_ddl_and_stamps_version(self):
        out = self.plane(
            "s = make_store();"
            "ver = s._read_all(\"SELECT fleet_schema_version FROM fleet_schema WHERE id='current'\")[0][0];"
            "names = {r[0] for r in s._read_all(\"SELECT name FROM sqlite_master WHERE type='table'\")};"
            "s.close();"
            "out = {'ver': ver, 'names': sorted(names)}"
        )
        self.assertEqual(out["ver"], 1)
        for table in ("nodes", "projects", "coord_events", "remote_services", "remote_dispatches", "fleet_schema"):
            self.assertIn(table, out["names"])

    def test_register_heartbeat_digest_round_trip(self):
        out = self.plane(
            "s = make_store();"
            "m1 = s.register_node('worker', tailnet_name='gb10.tailxxxx.ts.net')['marker'];"
            "m2 = s.register_node('worker')['marker'];"
            "hb = s.heartbeat()['status'];"
            "d = s.digest();"
            "s.close();"
            "out = {'m1': m1, 'm2': m2, 'hb': hb, 'nodes': d['nodes'], 'counts': d['counts'], 'sync': d['sync']}"
        )
        self.assertEqual(out["m1"], "registered")
        self.assertEqual(out["m2"], "updated")
        self.assertEqual(out["hb"], "OK")
        self.assertEqual(len(out["nodes"]), 1)
        self.assertEqual(out["nodes"][0]["role"], "worker")
        self.assertIsNotNone(out["nodes"][0]["heartbeat_age_s"])
        self.assertEqual(out["counts"]["nodes"], 1)
        self.assertGreaterEqual(out["counts"]["coord_events"], 3)
        self.assertFalse(out["sync"])

    def test_close_is_idempotent(self):
        out = self.plane("s = make_store(); s.close(); s.close(); out = {'ok': True}")
        self.assertTrue(out["ok"])


class CoordAppendGuardTest(_ScratchSubprocess):
    def test_kind_marker_pair_refusal(self):
        out = self.plane("s = make_store(); s.append_coord_event('node', 'point', summary='wrong'); out={}")
        self.assertEqual(out["_denied"], "DENIED_COORD_KIND_MARKER")
        self.assertEqual(out["_exit"], 2)

    def test_source_event_id_dedupe(self):
        out = self.plane(
            "s = make_store();"
            "a = s.append_coord_event('heartbeat', 'point', summary='hb', source_event_id='dup-1');"
            "b = s.append_coord_event('heartbeat', 'point', summary='hb', source_event_id='dup-1');"
            "n = s._read_all(\"SELECT COUNT(*) FROM coord_events WHERE source_event_id='dup-1'\")[0][0];"
            "s.close();"
            "out = {'a_dup': a['deduped'], 'b_dup': b['deduped'], 'n': n}"
        )
        self.assertFalse(out["a_dup"])
        self.assertTrue(out["b_dup"])
        self.assertEqual(out["n"], 1)

    def test_redaction_pass_path_legit_coord_fields_pass(self):
        out = self.plane(
            "s = make_store();"
            "r = s.append_coord_event('lease', 'granted', summary='Lease granted to node.',"
            " scope_key='abc123/kz/task/node', epoch=4,"
            " payload={'holder': 'gb10.tailxxxx.ts.net', 'sha': 'a'*40, 'mode': 'advisory'});"
            "s.close();"
            "out = {'status': r['status']}"
        )
        self.assertEqual(out["status"], "OK")

    def test_redaction_denies_secret_in_payload(self):
        out = self.plane(
            "s = make_store();"
            "s.append_coord_event('node', 'registered', summary='ok', payload={'password': 'hunter2secret'});"
            "out = {}"
        )
        self.assertEqual(out["_denied"], "DENIED_TRACE_REDACTION")

    def test_redaction_denies_personal_home_path(self):
        # chr(92) is a backslash; build C:\Users\someone\wt without escaping headaches in the snippet.
        out = self.plane(
            "home = 'C:' + chr(92) + 'Users' + chr(92) + 'someone' + chr(92) + 'wt';"
            "s = make_store();"
            "s.append_coord_event('node', 'registered', summary=home);"
            "out = {}"
        )
        self.assertEqual(out["_denied"], "DENIED_TRACE_REDACTION")


class SyncSurfaceTest(_ScratchSubprocess):
    def test_pull_in_tx_structural_guard(self):
        out = self.plane("s = make_store(); s._in_tx = True; s.sync.pull(); out = {}")
        self.assertEqual(out["_denied"], "DENIED_SYNC_IN_TX")

    def test_sync_ops_are_structured_noops_when_off(self):
        out = self.plane(
            "s = make_store();"
            "res = {'pull': s.sync.pull(), 'push': s.push(), 'ckpt': s.checkpoint(), 'stats': s.stats()};"
            "par = s.pull_and_reduce();"
            "s.close();"
            "out = {'res': res, 'par_pull': par['pull'], 'par_status': par['digest']['status']}"
        )
        for key in ("pull", "push", "ckpt", "stats"):
            self.assertEqual(out["res"][key], {"sync": "off"})
        self.assertEqual(out["par_pull"], {"sync": "off"})
        self.assertEqual(out["par_status"], "OK")

    def test_auth_token_never_persisted(self):
        out = self.plane(
            "import inspect\n"
            "from pathlib import Path\n"
            "canary = 'kaizen-fleet-auth-canary-7f6ec6b8'\n"
            "sig = 'auth_token' in inspect.signature(fleet_sync.open_connection).parameters\n"
            "seen = []\n"
            "original_open = fleet_sync.open_connection\n"
            "def observed_open(db_path, sync_url, auth_token):\n"
            "    seen.append(auth_token)\n"
            "    return original_open(db_path, None, auth_token)\n"
            "fleet_sync.open_connection = observed_open\n"
            "s = make_store(auth_token=canary)\n"
            "s.register_node('worker')\n"
            "s.append_coord_event('heartbeat', 'point', summary='hb')\n"
            "bad = {}\n"
            "for t in ('projects', 'coord_events', 'remote_services', 'nodes', 'remote_dispatches'):\n"
            "    cs = {r[1].lower() for r in s._read_all('PRAGMA table_info(' + t + ')')}\n"
            "    hits = sorted(c for c in cs if 'auth' in c or 'token' in c or 'secret' in c)\n"
            "    if hits:\n"
            "        bad[t] = hits\n"
            "db_path = Path(s.db_path)\n"
            "s.close()\n"
            "persisted = any(canary.encode() in path.read_bytes() for path in db_path.parent.glob(db_path.name + '*'))\n"
            "out = {'sig': sig, 'bad': bad, 'seen': seen == [canary], 'persisted': persisted}\n"
        )
        self.assertTrue(out["sig"])
        self.assertTrue(out["seen"])
        self.assertFalse(out["persisted"])
        self.assertEqual(out["bad"], {})


class WatermarkTest(_ScratchSubprocess):
    def test_watermark_recorded_and_monotonic(self):
        out = self.plane(
            "s = make_store();"
            "s.append_coord_event('coordinator','granted',summary='claim',epoch=7); s.digest();"
            "pid = identity.project_id()['project_id'];"
            "w1 = s.watermark(pid);"
            "s.append_coord_event('coordinator','granted',summary='lower',epoch=3); s.digest();"
            "w2 = s.watermark(pid);"
            "s.close();"
            "out = {'w1': w1, 'w2': w2}"
        )
        self.assertEqual(out["w1"], 7)
        self.assertEqual(out["w2"], 7)  # monotonic; a lower epoch never lowers it

    def test_watermark_lives_in_kaizen_db_not_fleet_db(self):
        out = self.plane(
            "s = make_store();"
            "s.append_coord_event('coordinator','granted',summary='claim',epoch=2); s.digest();"
            "pid = identity.project_id()['project_id'];"
            "fleet_tables = {r[0] for r in s._read_all(\"SELECT name FROM sqlite_master WHERE type='table'\")};"
            "s.close();"
            "out = {'in_fleet': 'db_settings' in fleet_tables, 'kaizen_val': db.get_setting('fleet_max_seen_epoch_' + pid)}"
        )
        self.assertFalse(out["in_fleet"])  # watermark is NOT a fleet.db table
        self.assertEqual(out["kaizen_val"], "2")  # it is a kaizen.db db_settings row


class BreakGlassTest(_ScratchSubprocess):
    def test_break_glass_refused_when_daemon_live(self):
        out = self.plane(
            "import os, json as J;"
            "from kaizen_components.orchestration.supervisor import PIDFILE, ensure_runtime_dir;"
            "ensure_runtime_dir();"
            "PIDFILE.write_text(J.dumps({'pid': os.getpid(), 'nonce': 'x', 'started_at': 'now'}), encoding='utf-8');"
            "open_store_breakglass();"
            "out = {}"
        )
        self.assertEqual(out["_denied"], "DENIED_FLEET_DAEMON_LIVE")

    def test_break_glass_allowed_when_pid_dead(self):
        out = self.plane(
            "import json as J;"
            "from kaizen_components.orchestration.supervisor import PIDFILE, ensure_runtime_dir;"
            "ensure_runtime_dir();"
            "PIDFILE.write_text(J.dumps({'pid': 999999, 'nonce': 'x', 'started_at': 'now'}), encoding='utf-8');"
            "live = daemon_is_live();"
            "s = open_store_breakglass(); ok = s is not None; s.close();"
            "out = {'live': live, 'ok': ok}"
        )
        self.assertFalse(out["live"])
        self.assertTrue(out["ok"])


class ManifestZeroDriftTest(unittest.TestCase):
    """The critical invariant: fleet.db (FLEET_DDL/FLEET_INDEXES + additive columns) has ZERO impact on
    kaizen.db's schema manifest. Run through the REAL CLI on a fresh scratch plane."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-fleet-manifest-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_k1_manifest_unchanged_on_fresh_plane(self):
        rc, p = kaizen(self.root, "K1")
        self.assertEqual(rc, 0, p)
        schema = p["schema"]
        self.assertTrue(schema["schema_ok"], schema)
        self.assertTrue(schema["manifest_match"], schema)
        self.assertEqual(schema["schema_version"], 1)
        self.assertEqual(schema["migration_id"], "kaizen-system-foundation-v1")

    def test_manifest_stays_clean_after_fleet_and_agent_run_writes(self):
        self.assertEqual(kaizen(self.root, "K1")[0], 0)
        rc_d, p_d = kaizen(self.root, "D1", "--payload-json", '{"role":"worker"}', "--summary", "N.",
                           env={"KAIZEN_DIST_MODE": "active"})
        self.assertEqual(rc_d, 0, p_d)
        self.assertEqual(p_d.get("status"), "OK", p_d)
        rc_t, p_t = kaizen(self.root, "T5", "--summary", "Run.",
                           "--payload-json", '{"agent_type":"claude","surface":"cli"}')
        self.assertEqual(rc_t, 0, p_t)
        self.assertEqual(p_t.get("status"), "OK", p_t)
        rc, p = kaizen(self.root, "K1")
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["schema"]["schema_ok"], p["schema"])
        self.assertTrue(p["schema"]["manifest_match"], p["schema"])

    def test_kaizen_db_has_additive_fleet_columns(self):
        self.assertEqual(kaizen(self.root, "K1")[0], 0)
        db_path = self.root / "AI" / "db" / "kaizen.db"
        import turso

        conn = turso.connect(str(db_path))
        try:
            ar = {r[1] for r in conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
            ases = {r[1] for r in conn.execute("PRAGMA table_info(agent_sessions)").fetchall()}
        finally:
            conn.close()
        self.assertIn("node_id", ar)
        self.assertIn("backend_endpoint_id", ar)
        self.assertIn("owning_node", ases)
        self.assertIn("node_epoch", ases)


class RegistryHygieneTest(unittest.TestCase):
    def test_reserved_claude_kinds_do_not_collide_with_coord_kinds(self):
        from kaizen_components.schemas.registry import COORD_EVENT_KIND_MARKERS, RESERVED_CLAUDE_EVENT_KINDS

        self.assertEqual(set(RESERVED_CLAUDE_EVENT_KINDS) & set(COORD_EVENT_KIND_MARKERS), set())

    def test_coord_event_and_fleet_node_schemas_registered(self):
        from kaizen_components.schemas import list_schemas

        schemas = set(list_schemas())
        self.assertIn("coord_event", schemas)
        self.assertIn("fleet_node", schemas)


if __name__ == "__main__":
    unittest.main()
