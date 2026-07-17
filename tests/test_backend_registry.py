"""B8 backend registry (v8 M13): the backend_endpoints table + CLI round-trips (add/list/probe/remove),
the tailnet-aware transport gate, the resolver, the circuit-breaker failover engine, the embed-lane
model-integrity invariant, the agent_runs.backend_endpoint_id provenance column, and the fresh-plane DDL.

All on isolated KAIZEN_REPO_ROOT planes (never the real AI/db). Two exercise styles:
- CLI round-trips via ``self.kz`` (so test_op_coverage sees B8 invoked);
- in-process resolver/breaker/gate checks via a subprocess Python snippet pinned to the SAME isolated
  plane (the resolver reads DB rows and the breaker is in-process, so it must run inside the engine),
  and a REAL local HTTP stub for the probe (reusing the suite's _OpenAIHandler).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import REPO_ROOT, IsolatedDBTest, kaizen  # noqa: E402
from test_backends_live import _OpenAIHandler  # noqa: E402  # reuse the mock OpenAI-compatible server

VALID_TS_URL = "https://gb10.tail1234.ts.net/v1"  # https always passes the transport gate


def _db_path(root: Path) -> Path:
    return root / "AI" / "db" / "kaizen.db"


def _read_sql(db_path: Path, sql: str) -> list:
    """Run one read-only query in a SEPARATE process (turso releases the Windows file lock only on
    process exit) and return the rows as JSON."""
    script = (
        "import sys, json, turso\n"
        "conn = turso.connect(sys.argv[1])\n"
        "rows = [list(r) for r in conn.execute(sys.argv[2]).fetchall()]\n"
        "conn.close()\n"
        "print(json.dumps(rows))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(db_path), sql], capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise AssertionError(f"read SQL failed: {proc.stderr or proc.stdout}")
    return json.loads(proc.stdout.strip() or "[]")


def _in_engine(root: Path, body: str, *, env: dict | None = None) -> dict:
    """Run ``body`` inside the kaizen engine, pinned to the isolated plane ``root``. ``body`` sets a
    module-level ``RESULT`` dict which is printed as JSON. Runs in a fresh process so the in-process
    circuit breaker starts clean and the turso lock releases on exit."""
    script = (
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from kaizen_components import backend_registry as br\n"
        "RESULT = {}\n"
        + body
        + "\nprint(json.dumps(RESULT))\n"
    )
    full_env = dict(os.environ)
    full_env["KAIZEN_REPO_ROOT"] = str(root)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        cwd=str(REPO_ROOT), env=full_env, timeout=30,
    )
    if proc.returncode != 0:
        raise AssertionError(f"in-engine snippet failed: {proc.stderr or proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class B8AddListRemoveTest(IsolatedDBTest):
    def _add(self, base_url=VALID_TS_URL, lanes=("text",), model="qwen2.5", priority=None, extra=None):
        payload = {"base_url": base_url, "lanes": list(lanes), "model": model}
        if priority is not None:
            payload["priority"] = priority
        if extra:
            payload.update(extra)
        return self.kz("B8", "--action", "add", "--payload-json", json.dumps(payload))

    def test_add_then_list_then_remove_round_trip(self):
        rc, p = self._add(model="qwen2.5", priority=10)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        endpoint_id = p["id"]
        self.assertEqual(p["lanes"], ["text"], p)

        rc, lst = self.kz("B8", "--action", "list")
        self.assertEqual(rc, 0, lst)
        self.assertEqual(lst["count"], 1, lst)
        self.assertEqual(lst["endpoints"][0]["id"], endpoint_id, lst)
        self.assertEqual(lst["endpoints"][0]["base_url"], VALID_TS_URL, lst)
        self.assertTrue(lst["endpoints"][0]["enabled"], lst)

        rc, rm = self.kz("B8", "--action", "remove", "--id", endpoint_id)
        self.assertEqual(rc, 0, rm)
        self.assertTrue(rm["removed"], rm)

        rc, lst2 = self.kz("B8", "--action", "list")
        self.assertEqual(rc, 0, lst2)
        self.assertEqual(lst2["count"], 0, lst2)

    def test_list_is_default_action(self):
        # Bare B8 (no --action) must list -- this is also the op-coverage invocation shape.
        rc, p = self.kz("B8")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        self.assertEqual(p["count"], 0, p)

    def test_list_orders_by_priority(self):
        self.assertEqual(self._add(model="m-a", priority=50)[0], 0)
        self.assertEqual(self._add(model="m-b", priority=5)[0], 0)
        self.assertEqual(self._add(model="m-c", priority=20)[0], 0)
        rc, lst = self.kz("B8", "--action", "list")
        self.assertEqual(rc, 0, lst)
        self.assertEqual([e["model"] for e in lst["endpoints"]], ["m-b", "m-c", "m-a"], lst)

    def test_list_limit_caps_rows(self):
        for i in range(3):
            self.assertEqual(self._add(model=f"m{i}", priority=i)[0], 0)
        rc, lst = self.kz("B8", "--action", "list", "--limit", "2")
        self.assertEqual(rc, 0, lst)
        self.assertEqual(lst["count"], 2, lst)

    def test_list_zero_is_empty_and_negative_limit_is_denied(self):
        self.assertEqual(self._add(model="m0")[0], 0)
        rc, empty = self.kz("B8", "--action", "list", "--limit", "0")
        self.assertEqual(rc, 0, empty)
        self.assertEqual(empty["endpoints"], [])
        rc, denied = self.kz("B8", "--action", "list", "--limit", "-1")
        self.assertEqual(rc, 2, denied)
        self.assertEqual(denied.get("code"), "DENIED_BACKEND_LIMIT_INVALID", denied)

    def test_add_missing_base_url_refused(self):
        rc, p = self.kz("B8", "--action", "add", "--payload-json", json.dumps({"lanes": ["text"], "model": "m"}))
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_BASE_URL_REQUIRED", p)

    def test_add_missing_model_refused(self):
        rc, p = self.kz("B8", "--action", "add", "--payload-json", json.dumps({"base_url": VALID_TS_URL, "lanes": ["text"]}))
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_MODEL_REQUIRED", p)

    def test_add_bad_lane_refused(self):
        rc, p = self._add(lanes=("text", "bogus"))
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_LANE_INVALID", p)

    def test_add_empty_lanes_refused(self):
        rc, p = self.kz("B8", "--action", "add", "--payload-json", json.dumps({"base_url": VALID_TS_URL, "lanes": [], "model": "m"}))
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_LANE_INVALID", p)

    def test_add_token_in_base_url_denied_by_redaction(self):
        # A bearer-looking secret embedded in the URL must never durably land in the table.
        bad = "https://user:sk-ant-abcdefghijklmnopqrstuvwx@gb10.tail1234.ts.net/v1"
        rc, p = self._add(base_url=bad)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_TRACE_REDACTION", p)

    def test_remove_unknown_id_refused(self):
        rc, p = self.kz("B8", "--action", "remove", "--id", "be_nope")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_NOT_FOUND", p)

    def test_unknown_action_refused(self):
        rc, p = self.kz("B8", "--action", "frobnicate")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_BACKEND_ACTION_INVALID", p)

    def test_q8_validates_backend_endpoint_schema(self):
        # Q8 record-schema registration: the backend_endpoint kind is Q8-validatable.
        rc, p = self.kz("Q8", "--kind", "backend_endpoint", "--payload-json",
                        json.dumps({"base_url": VALID_TS_URL, "lanes": ["text"], "model": "m", "summary": "An endpoint."}))
        self.assertEqual(rc, 0, p)
        self.assertTrue(p.get("valid"), p)
        # A missing required field denies through the same gate.
        rc, bad = self.kz("Q8", "--kind", "backend_endpoint", "--payload-json", json.dumps({"lanes": ["text"]}))
        self.assertEqual(rc, 2, bad)
        self.assertEqual(bad.get("status"), "DENIED", bad)

    def test_add_marks_is_test_and_k7_purges(self):
        rc, p = self.kz("B8", "--action", "add", "--test", "--payload-json",
                         json.dumps({"base_url": VALID_TS_URL, "lanes": ["text"], "model": "m"}))
        self.assertEqual(rc, 0, p)
        rows = _read_sql(_db_path(self.root), "SELECT is_test FROM backend_endpoints")
        self.assertEqual(rows, [[1]], rows)
        rc, purge = self.kz("K7")
        self.assertEqual(rc, 0, purge)
        self.assertEqual(purge["purged"].get("backend_endpoints"), 1, purge)
        self.assertEqual(_read_sql(_db_path(self.root), "SELECT COUNT(*) FROM backend_endpoints"), [[0]])


class B8TransportGateTest(IsolatedDBTest):
    """The B8 add path runs the SHIPPED backends transport gate on base_url."""

    def _add(self, base_url, env=None):
        return self.kz("B8", "--action", "add", "--payload-json",
                       json.dumps({"base_url": base_url, "lanes": ["text"], "model": "m"}), env=env)

    def test_https_remote_ok(self):
        rc, p = self._add("https://api.example.com/v1")
        self.assertEqual(rc, 0, p)

    def test_loopback_http_ok(self):
        rc, p = self._add("http://127.0.0.1:11434/v1")
        self.assertEqual(rc, 0, p)

    def test_plain_http_non_loopback_non_tailnet_refused(self):
        rc, p = self._add("http://192.168.1.50:11434/v1")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_ENDPOINT_INSECURE", p)

    def test_insecure_http_override_bypasses(self):
        rc, p = self._add("http://192.168.1.50:11434/v1", env={"KAIZEN_ALLOW_INSECURE_HTTP": "1"})
        self.assertEqual(rc, 0, p)

    def test_plain_http_tailnet_host_on_tailnet_allowed(self):
        # on_tailnet monkeypatched True => a tailnet-suffixed plain-http host is allowed (WireGuard E2E).
        out = _in_engine(
            self.root,
            "from kaizen_components.backends import _assert_endpoint_transport_safe\n"
            "try:\n"
            "    _assert_endpoint_transport_safe('http://gb10.tail1234.ts.net/v1', 'backend_endpoints', tailnet_probe=lambda: True)\n"
            "    RESULT['ok'] = True\n"
            "except Exception as e:\n"
            "    RESULT['ok'] = False; RESULT['err'] = type(e).__name__\n",
        )
        self.assertTrue(out.get("ok"), out)

    def test_plain_http_tailnet_host_off_tailnet_refused(self):
        out = _in_engine(
            self.root,
            "from kaizen_components.backends import _assert_endpoint_transport_safe\n"
            "from kaizen_components.denials import KaizenDenied\n"
            "try:\n"
            "    _assert_endpoint_transport_safe('http://gb10.tail1234.ts.net/v1', 'backend_endpoints', tailnet_probe=lambda: False)\n"
            "    RESULT['code'] = None\n"
            "except KaizenDenied as e:\n"
            "    RESULT['code'] = e.code\n",
        )
        self.assertEqual(out.get("code"), "DENIED_ENDPOINT_INSECURE", out)


class B8ProbeTest(IsolatedDBTest):
    """B8 --action probe against a REAL local HTTP stub: health flips truthfully, last_probe advances,
    a dead endpoint records error and never raises."""

    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _add(self, base_url, lanes=("text",), model="m"):
        rc, p = self.kz("B8", "--action", "add", "--payload-json",
                        json.dumps({"base_url": base_url, "lanes": list(lanes), "model": model}))
        self.assertEqual(rc, 0, p)
        return p["id"]

    def test_probe_live_stub_health_ok(self):
        endpoint_id = self._add(self.base, lanes=("text",))
        rc, p = self.kz("B8", "--action", "probe")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["probed"], 1, p)
        self.assertEqual(p["results"][0]["health"], "ok", p)
        self.assertTrue(p["results"][0]["last_probe"], p)
        rows = _read_sql(_db_path(self.root), "SELECT health, last_probe FROM backend_endpoints")
        self.assertEqual(rows[0][0], "ok", rows)
        self.assertIsNotNone(rows[0][1], rows)

    def test_probe_embed_lane_live_stub_ok(self):
        self._add(self.base, lanes=("embed",))
        rc, p = self.kz("B8", "--action", "probe")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["results"][0]["health"], "ok", p)

    def test_probe_dead_endpoint_records_error_never_raises(self):
        # Port 6 is a reserved unreachable port -> a fast connection failure, recorded not raised.
        self._add("http://127.0.0.1:6/v1", lanes=("text",))
        rc, p = self.kz("B8", "--action", "probe")
        self.assertEqual(rc, 0, p)  # never raises on a dead endpoint
        self.assertTrue(p["results"][0]["health"].startswith("error:"), p)

    def test_probe_single_id(self):
        good = self._add(self.base, lanes=("text",))
        added_rc, added = self.kz("B8", "--action", "add", "--payload-json",
                                  json.dumps({"base_url": "http://127.0.0.1:6/v1", "lanes": ["text"], "model": "m2"}))
        self.assertEqual(added_rc, 0, added)
        rc, p = self.kz("B8", "--action", "probe", "--id", good)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["probed"], 1, p)
        self.assertEqual(p["results"][0]["id"], good, p)


class B8ResolverTest(IsolatedDBTest):
    """resolve_endpoint: priority order, lane filtering, disabled skip, None on empty (env fallback)."""

    def _seed(self, endpoints):
        for ep in endpoints:
            rc, p = self.kz("B8", "--action", "add", "--payload-json", json.dumps(ep))
            self.assertEqual(rc, 0, p)

    def test_resolves_highest_priority_and_lane(self):
        self._seed([
            {"base_url": "https://a.example.com/v1", "lanes": ["text"], "model": "m-a", "priority": 50},
            {"base_url": "https://b.example.com/v1", "lanes": ["text", "judge"], "model": "m-b", "priority": 5},
        ])
        out = _in_engine(self.root, "RESULT = br.resolve_endpoint('text') or {}\n")
        self.assertEqual(out.get("model"), "m-b", out)
        out2 = _in_engine(self.root, "RESULT = br.resolve_endpoint('judge') or {}\n")
        self.assertEqual(out2.get("model"), "m-b", out2)

    def test_lane_filtering_returns_none_for_absent_lane(self):
        self._seed([{"base_url": "https://a.example.com/v1", "lanes": ["text"], "model": "m-a"}])
        out = _in_engine(self.root, "RESULT = {'r': br.resolve_endpoint('rerank')}\n")
        self.assertIsNone(out["r"], out)

    def test_empty_registry_resolves_none(self):
        out = _in_engine(self.root, "RESULT = {'r': br.resolve_endpoint('text')}\n")
        self.assertIsNone(out["r"], out)

    def test_mutating_resolved_lanes_does_not_mutate_memoized_rows(self):
        self._seed([{"base_url": "https://a.example.com/v1", "lanes": ["text"], "model": "m-a"}])
        out = _in_engine(
            self.root,
            "first = br.resolve_endpoint('text')\n"
            "first['lanes'].append('judge')\n"
            "second = br.resolve_endpoint('text')\n"
            "RESULT = {'first': first['lanes'], 'second': second['lanes']}\n",
        )
        self.assertEqual(out["first"], ["text", "judge"])
        self.assertEqual(out["second"], ["text"])

    def test_disabled_row_skipped(self):
        self._seed([
            {"base_url": "https://a.example.com/v1", "lanes": ["text"], "model": "m-a", "priority": 5},
            {"base_url": "https://b.example.com/v1", "lanes": ["text"], "model": "m-b", "priority": 50},
        ])
        # Disable the top-priority row directly, then the resolver must fall to m-b.
        rows = _read_sql(_db_path(self.root), "SELECT id FROM backend_endpoints WHERE model = 'm-a'")
        top_id = rows[0][0]
        _direct = (
            "import sys, turso\n"
            "conn = turso.connect(sys.argv[1])\n"
            "conn.execute(\"UPDATE backend_endpoints SET enabled = 0 WHERE id = ?\", (sys.argv[2],))\n"
            "conn.commit(); conn.close()\n"
        )
        proc = subprocess.run([sys.executable, "-c", _direct, str(_db_path(self.root)), top_id], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = _in_engine(self.root, "RESULT = br.resolve_endpoint('text') or {}\n")
        self.assertEqual(out.get("model"), "m-b", out)

    def test_env_fallback_path_still_resolves_with_zero_rows(self):
        # With zero registry rows, the env-var backend factory is UNCHANGED (opt-in): configuring
        # KAIZEN_LLM_MODEL yields a real text backend while resolve_endpoint returns None.
        out = _in_engine(
            self.root,
            "from kaizen_components.backends import get_text_backend\n"
            "RESULT['resolver'] = br.resolve_endpoint('text')\n"
            "RESULT['env_backend'] = get_text_backend() is not None\n",
            env={"KAIZEN_LLM_MODEL": "fake-chat", "KAIZEN_LLM_BASE_URL": "https://api.example.com/v1"},
        )
        self.assertIsNone(out["resolver"], out)
        self.assertTrue(out["env_backend"], out)


class B8CircuitBreakerTest(IsolatedDBTest):
    """resolve_with_failover: transient failure -> next endpoint + breaker trip + hop trail; cooldown
    re-enables; all-tripped -> bounded refusal (no loop)."""

    def _seed(self, endpoints):
        for ep in endpoints:
            rc, p = self.kz("B8", "--action", "add", "--payload-json", json.dumps(ep))
            self.assertEqual(rc, 0, p)

    def test_transient_failover_moves_to_next_and_trips_breaker(self):
        self._seed([
            {"base_url": "https://a.example.com/v1", "lanes": ["text"], "model": "m", "priority": 5},
            {"base_url": "https://b.example.com/v1", "lanes": ["text"], "model": "m", "priority": 50},
        ])
        # Endpoint 1 (priority 5) raises a transient KaizenDenied; endpoint 2 succeeds.
        body = (
            "from kaizen_components.denials import KaizenDenied\n"
            "calls = []\n"
            "def attempt(ep):\n"
            "    calls.append(ep['base_url'])\n"
            "    if ep['base_url'].startswith('https://a.'):\n"
            "        raise KaizenDenied('DENIED_BACKEND_UNAVAILABLE', {'required_action': 'x'}, exit_code=2)\n"
            "    return 'served'\n"
            "res = br.resolve_with_failover('text', attempt, model='m')\n"
            "RESULT['served_by'] = res['endpoint']['base_url']\n"
            "RESULT['failover'] = res['failover']\n"
            "RESULT['calls'] = calls\n"
            "RESULT['tripped_a'] = br._breaker_tripped(res['failover'][0]['id'])\n"
        )
        out = _in_engine(self.root, body)
        self.assertTrue(out["served_by"].startswith("https://b."), out)
        self.assertEqual(len(out["failover"]), 1, out)
        self.assertEqual(out["failover"][0]["error"], "KaizenDenied", out)
        self.assertTrue(out["failover"][0]["transient"], out)
        self.assertTrue(out["tripped_a"], out)

    def test_permanent_failure_does_not_fail_over(self):
        self._seed([
            {"base_url": "https://a.example.com/v1", "lanes": ["text"], "model": "m", "priority": 5},
            {"base_url": "https://b.example.com/v1", "lanes": ["text"], "model": "m", "priority": 50},
        ])
        # A 4xx (wrong model) is permanent: re-raise immediately, endpoint 2 is NOT tried.
        body = (
            "from kaizen_components.denials import KaizenDenied\n"
            "calls = []\n"
            "def attempt(ep):\n"
            "    calls.append(ep['base_url'])\n"
            "    raise KaizenDenied('DENIED_BACKEND_HTTP', {'http_status': 404, 'required_action': 'x'}, exit_code=2)\n"
            "try:\n"
            "    br.resolve_with_failover('text', attempt, model='m')\n"
            "    RESULT['raised'] = None\n"
            "except KaizenDenied as e:\n"
            "    RESULT['raised'] = e.code\n"
            "RESULT['calls'] = calls\n"
        )
        out = _in_engine(self.root, body)
        self.assertEqual(out["raised"], "DENIED_BACKEND_HTTP", out)
        self.assertEqual(len(out["calls"]), 1, out)  # only the first endpoint attempted

    def test_cooldown_re_enables_endpoint(self):
        self._seed([{"base_url": "https://a.example.com/v1", "lanes": ["text"], "model": "m", "priority": 5}])
        # Inject a tiny cooldown and a controllable clock: trip then step past the cooldown.
        body = (
            "clock = {'t': 1000.0}\n"
            "def now(): return clock['t']\n"
            "br.trip_breaker('e1', cooldown_s=5.0, clock=now)\n"
            "RESULT['tripped_before'] = br._breaker_tripped('e1', clock=now)\n"
            "clock['t'] = 1006.0\n"
            "RESULT['tripped_after'] = br._breaker_tripped('e1', clock=now)\n"
        )
        out = _in_engine(self.root, body)
        self.assertTrue(out["tripped_before"], out)
        self.assertFalse(out["tripped_after"], out)

    def test_all_tripped_bounded_refusal_no_loop(self):
        self._seed([
            {"base_url": "https://a.example.com/v1", "lanes": ["text"], "model": "m", "priority": 5},
            {"base_url": "https://b.example.com/v1", "lanes": ["text"], "model": "m", "priority": 50},
        ])
        # Every endpoint transient-fails: bounded to the row count, then a single refusal (never a loop).
        body = (
            "from kaizen_components.denials import KaizenDenied\n"
            "n = {'calls': 0}\n"
            "def attempt(ep):\n"
            "    n['calls'] += 1\n"
            "    raise KaizenDenied('DENIED_BACKEND_UNAVAILABLE', {'required_action': 'x'}, exit_code=2)\n"
            "try:\n"
            "    br.resolve_with_failover('text', attempt, model='m')\n"
            "    RESULT['code'] = None\n"
            "except KaizenDenied as e:\n"
            "    RESULT['code'] = e.code\n"
            "RESULT['calls'] = n['calls']\n"
        )
        out = _in_engine(self.root, body)
        self.assertEqual(out["code"], "DENIED_BACKEND_NO_ENDPOINT", out)
        self.assertEqual(out["calls"], 2, out)  # exactly one attempt per enabled row -- bounded


class B8EmbedInvariantTest(IsolatedDBTest):
    """The embed lane NEVER fails over across a DIFFERENT model (DENIED_EMBED_MISMATCH invariant);
    text lane unpinned MAY cross models."""

    def _seed(self, endpoints):
        for ep in endpoints:
            rc, p = self.kz("B8", "--action", "add", "--payload-json", json.dumps(ep))
            self.assertEqual(rc, 0, p)

    def test_embed_pinned_model_never_returns_a_different_model(self):
        self._seed([
            {"base_url": "https://a.example.com/v1", "lanes": ["embed"], "model": "model-A", "priority": 5},
            {"base_url": "https://b.example.com/v1", "lanes": ["embed"], "model": "model-B", "priority": 50},
        ])
        # Pinned to model-A; even after model-A's only endpoint is tripped, the resolver returns None
        # rather than crossing to model-B (an embedding index is bound to one model identity).
        body = (
            "rows = br.list_endpoints(enabled_only=True)\n"
            "a = [r for r in rows if r['model'] == 'model-A'][0]\n"
            "br.trip_breaker(a['id'])\n"
            "RESULT['resolved'] = br.resolve_endpoint('embed', model='model-A')\n"
        )
        out = _in_engine(self.root, body)
        self.assertIsNone(out["resolved"], out)

    def test_embed_resolve_requires_a_model_pin(self):
        body = (
            "for resolver in (br.resolve_endpoint, lambda lane: br.resolve_with_failover(lane, lambda _row: None)):\n"
            "    try:\n"
            "        resolver('embed')\n"
            "    except Exception as error:\n"
            "        RESULT.setdefault('codes', []).append(getattr(error, 'code', None))\n"
        )
        out = _in_engine(self.root, body)
        self.assertEqual(out["codes"], ["DENIED_BACKEND_MODEL_REQUIRED"] * 2, out)

    def test_embed_same_model_different_endpoint_failover_allowed(self):
        self._seed([
            {"base_url": "https://a.example.com/v1", "lanes": ["embed"], "model": "model-A", "priority": 5},
            {"base_url": "https://b.example.com/v1", "lanes": ["embed"], "model": "model-A", "priority": 50},
        ])
        # Same model on two endpoints: tripping the first still resolves the second (availability).
        body = (
            "rows = br.list_endpoints(enabled_only=True)\n"
            "first = sorted(rows, key=lambda r: r['priority'])[0]\n"
            "br.trip_breaker(first['id'])\n"
            "r = br.resolve_endpoint('embed', model='model-A')\n"
            "RESULT['base_url'] = r['base_url'] if r else None\n"
        )
        out = _in_engine(self.root, body)
        self.assertTrue(out["base_url"].startswith("https://b."), out)

    def test_text_unpinned_may_cross_models(self):
        self._seed([
            {"base_url": "https://a.example.com/v1", "lanes": ["text"], "model": "model-A", "priority": 5},
            {"base_url": "https://b.example.com/v1", "lanes": ["text"], "model": "model-B", "priority": 50},
        ])
        # Unpinned text: tripping model-A's endpoint crosses to model-B (availability, no index binding).
        body = (
            "rows = br.list_endpoints(enabled_only=True)\n"
            "a = [r for r in rows if r['model'] == 'model-A'][0]\n"
            "br.trip_breaker(a['id'])\n"
            "r = br.resolve_endpoint('text')\n"
            "RESULT['model'] = r['model'] if r else None\n"
        )
        out = _in_engine(self.root, body)
        self.assertEqual(out["model"], "model-B", out)


class B8ProvenanceColumnTest(IsolatedDBTest):
    """agent_runs.backend_endpoint_id exists (M14 stamps it; M13 only ensures the column)."""

    def test_column_exists_and_accepts_value(self):
        cols = [r[1] for r in _read_sql(_db_path(self.root), "PRAGMA table_info(agent_runs)")]
        self.assertIn("backend_endpoint_id", cols, cols)
        # Seed a minimal run row and UPDATE the provenance column (proves it accepts a value).
        script = (
            "import sys, turso\n"
            "conn = turso.connect(sys.argv[1])\n"
            "conn.execute(\"INSERT INTO agent_runs (id, created_at, agent_type, surface, summary, body, content_hash) \"\n"
            "             \"VALUES ('ar_x', '2026-01-01T00:00:00+00:00', 'other', 'cli', 's', 'b', 'h')\")\n"
            "conn.execute(\"UPDATE agent_runs SET backend_endpoint_id = 'be_x' WHERE id = 'ar_x'\")\n"
            "conn.commit(); conn.close()\n"
        )
        proc = subprocess.run([sys.executable, "-c", script, str(_db_path(self.root))], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = _read_sql(_db_path(self.root), "SELECT backend_endpoint_id FROM agent_runs WHERE id = 'ar_x'")
        self.assertEqual(rows, [["be_x"]], rows)


class B8FreshPlaneSchemaTest(IsolatedDBTest):
    """K1 on a fresh plane applies the new DDL: schema_ok + manifest_match true on the fresh stamp."""

    def test_fresh_plane_schema_ok_and_manifest_match(self):
        rc, p = self.kz("K2")
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["schema"]["schema_ok"], p)
        self.assertTrue(p["schema"]["manifest_match"], p)
        self.assertEqual(p["schema"]["schema_version"], 1, p)
        # The new table is present in the fresh plane.
        names = [r[0] for r in _read_sql(_db_path(self.root),
                                         "SELECT name FROM sqlite_master WHERE type='table' AND name='backend_endpoints'")]
        self.assertEqual(names, ["backend_endpoints"], names)


if __name__ == "__main__":
    unittest.main()
