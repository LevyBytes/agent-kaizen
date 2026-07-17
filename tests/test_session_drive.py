"""Driven-session spine (H0): the daemon DRIVES a local-LLM turn under the authoritative ledger.

OFFLINE tests drive a REAL in-process Supervisor pinned to a fresh scratch KAIZEN_REPO_ROOT in a
SUBPROCESS -- the suite idiom for touching the isolated DB in-process (turso Windows single-writer;
cf. test_supervisor / test_remote_dispatch / test_policy._run_policy_snippet). The scenario body runs
in the subprocess with the daemon's _adapter_factory seam injected (mirroring _dispatch_runner): the
factory returns a LocalLLMAdapter over a HERMETIC scripted provider, so the whole loop (prompt ->
parse -> decide() -> executor -> T6 funnel) runs with no network, no model, no child process. A fake
clock/sleep makes the session/events long-poll deterministic. Every scenario asserts on a RESULT json
line the subprocess prints.

Coverage (the H0 exit matrix):
- start -> events -> final .................... StartEventsFinalTest
- ask -> approve-by-correlation_id -> tool ... ApproveByCorrelationTest
- ask -> deny ................................ DenyTest
- ask timeout fail-closed ................... AskTimeoutTest
- steer mid-turn ............................ SteerTest
- interrupt ................................. InterruptTest
- kill (waiters denied, adapter dead) ...... KillTest
- cursor pagination gapless ................ CursorPaginationTest
- long-poll mid-poll delivery .............. LongPollDeliveryTest
- long-poll timeout returns empty .......... LongPollTimeoutTest
- codex/claude capability-denied, unknown UNKNOWN,
  with NO C1/T5 rows written ............... EngineGateTest
- bad loopback token refused ............... LoopbackTokenTest
- shutdown-with-open-turn .................. ShutdownOpenTurnTest
- double-approve ALREADY_DECIDED ........... DoubleApproveTest
- capabilities shape (3 wire engines) ...... CapabilitiesShapeTest
- capabilities degraded probe (no Ollama) .. CapabilitiesDegradedTest
- start w/ explicit profile persists all
  C1/T5 fields + profile/point FIRST ....... ProfileStartTest
- Full without opt-in DENIED, zero rows .... FullOptInTest
- reasoning_effort on local_llm DENIED ..... ProfileUnsupportedTest
- unknown profile field DENIED, zero rows .. ProfileUnknownFieldTest
- legacy model vs profile.model conflict ... ModelConflictTest
- claude_cli alias -> claude on the wire ... EngineAliasTest
- profile_hash differs plan vs ask ......... ProfileHashDiffersTest

Plus a gated live-smoke class (KAIZEN_RUN_LIVE=1 + KAIZEN_LLM_MODEL/KAIZEN_LLM_BASE_URL): a real Ollama
driven turn incl. an approval round-trip, asserting C1/C4/T5/T6 rows, is_test-marked + K7-purged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import KAIZEN, REPO_ROOT, kaizen, run  # noqa: E402

_LIVE = os.environ.get("KAIZEN_RUN_LIVE") == "1"


class ApprovalWaiterRaceTest(unittest.TestCase):
    def test_release_winning_after_event_wait_is_not_misreported_as_timeout(self) -> None:
        from kaizen_components.orchestration.session_drive import _ApprovalWaiter

        waiter = _ApprovalWaiter()
        rich = {"decision": "approved", "updated_input": {"x": 1}}

        class RacingEvent:
            def wait(self, _timeout):
                waiter.release(rich)
                return False

            def set(self):
                pass

        waiter._event = RacingEvent()
        value, timed_out = waiter.wait_result(0.0)
        self.assertIs(value, rich)
        self.assertFalse(timed_out)

    def test_timeout_expires_waiter_so_late_rich_approval_can_be_stashed(self) -> None:
        from kaizen_components.orchestration.session_drive import _ApprovalWaiter

        waiter = _ApprovalWaiter()
        _value, timed_out = waiter.wait_result(0.0)
        self.assertTrue(timed_out)
        self.assertFalse(waiter.release({"decision": "approved", "updated_input": {"x": 1}}))


# --- subprocess driver harness (scratch plane; the daemon runs in-process in the child) ----------

# The child preamble defines: a scripted chat provider (canned reply list), a driven _adapter_factory
# that wires it into a LocalLLMAdapter with the daemon-supplied recorder (so events funnel to T6), a
# fake clock/sleep pair, and helpers to poll events. BODY is the scenario; it must set ``out``.
_PREAMBLE = r"""
import json, sys, time
from kaizen_components.orchestration.supervisor import Supervisor
from kaizen_components.orchestration.adapters import local_llm as L
from kaizen_components.orchestration import policy


def scripted_provider(replies):
    # Returns canned dicts/strings in order (repeating the last), recording each call's message list.
    state = {"i": 0, "calls": []}
    def provider(messages, **opts):
        state["calls"].append([dict(m) for m in messages])
        idx = min(state["i"], len(replies) - 1)
        state["i"] += 1
        reply = replies[idx]
        return {"text": reply} if isinstance(reply, str) else dict(reply)
    provider.state = state
    return provider


def deterministic_ids():
    counters = {}
    def factory(prefix):
        counters[prefix] = counters.get(prefix, 0) + 1
        return prefix + "-" + str(counters[prefix])
    return factory


def tool_reply(name, **args):
    return json.dumps({"tool": name, "args": args})


def final_reply(answer):
    return json.dumps({"final": answer})


def allow_rule(verb, prefix, rid):
    return {"id": rid, "rule_type": "allow", "verb": verb, "match_kind": "path_prefix",
            "pattern": prefix, "engine": None, "enabled": True}


def make_engine(rules=(), protected=()):
    # vendor=[] so no real ~/.claude/~/.codex resolution in a unit test.
    return policy.PolicyEngine(list(protected), list(rules), [])


def echo_tools():
    # A single ALLOW-scoped no-op tool so a tool intent reaches decide() and runs.
    def _run(args):
        return "ran echo " + str(args.get("path", ""))
    return {"echo": L.ToolSpec("echo", "file_read", "echo a path", _run, arg_hints=("path",))}


def install_factory(sup, provider, *, engine=None, tools=None, timeout_holder=None):
    '''Install a scripted adapter factory while preserving the supervisor's driven-turn options.'''
    # The _adapter_factory seam: build a LocalLLMAdapter over the scripted provider with the daemon's
    # recorder (so events funnel to T6). Honors the daemon-passed kwargs (engine_name/model/
    # approval_timeout/max_turns). A fresh id_factory per adapter keeps ids deterministic per run.
    eng = engine if engine is not None else make_engine()
    def factory(agent_run_id, recorder, kwargs):
        adapter = L.LocalLLMAdapter(
            eng, chat_provider=provider, tools=(tools if tools is not None else {}),
            recorder=recorder, logger=(lambda _m: None), id_factory=deterministic_ids(), **kwargs,
        )
        return adapter
    sup._adapter_factory = factory


class FakeClock:
    # A monotonic fake: clock() advances only when sleep() is called (deterministic long-poll).
    def __init__(self):
        self.t = 0.0
    def clock(self):
        return self.t
    def sleep(self, dt):
        self.t += dt


def wait_idle(sup, run_id, budget=8.0):
    # Block until the current turn completes without implicitly finalizing the conversation.
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        sess = sup._get_driven(run_id)
        if sess is not None and sess.turn_state == "idle":
            return sess
        state = sup._safe_reduce(run_id)
        if state is not None and state["terminal"]:
            return None
        time.sleep(0.02)
    return sup._get_driven(run_id)


def wait_terminal(sup, run_id, budget=8.0):
    # Compatibility helper for pre-H2 scenarios: wait for idle, then EXPLICITLY close. Fatal/kill paths
    # may terminalize first. H2-specific tests below use wait_idle directly to prove T8 stays absent.
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        state = sup._safe_reduce(run_id)
        if state is not None and state["terminal"]:
            return state
        sess = sup._get_driven(run_id)
        if sess is not None and sess.turn_state == "idle":
            sup._handle_control({"op": "session/close", "args": {"agent_run_id": run_id}})
            continue
        time.sleep(0.02)
    return sup._safe_reduce(run_id)


def wait_open_approval(sup, run_id, budget=8.0):
    # Block until (a) an approval/open event lands on the run's stream AND (b) the C4 approval row for
    # that correlation is persisted (record_ask runs AFTER the event emit -- local_llm.py:687 vs :693 --
    # so an approve keyed by the event's correlation would race the row). Return the correlation_id (the
    # race-free approve handle the webview uses). None if none appears in budget.
    from kaizen_components import db
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        rows = db.fetch_all(
            "SELECT correlation_id FROM agent_events WHERE agent_run_id = ? AND event_kind = 'approval' "
            "AND marker = 'open' ORDER BY sequence_no LIMIT 1", (run_id,))
        if rows and rows[0][0]:
            corr = rows[0][0]
            # Confirm the C4 row exists (open OR decided) so the alt-key approve resolves deterministically.
            c4 = db.fetch_all(
                "SELECT a.id FROM approval_requests a JOIN agent_runs r ON r.session_id = a.session_id "
                "WHERE r.id = ? AND a.correlation_id = ? LIMIT 1", (run_id, corr))
            if c4:
                return corr
        time.sleep(0.02)
    return None


def wait_waiter_parked(sup, run_id, correlation, budget=8.0):
    # Block until the driven session has PARKED the approval waiter for `correlation` (the adapter's
    # on_approval resolver thread runs slightly after the approval/open event, so kill/approve must wait
    # for the parked waiter to observe it deterministically). Returns True once parked.
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        sess = sup._get_driven(run_id)
        if sess is not None:
            with sess.lock:
                if correlation in sess.waiters:
                    return True
        time.sleep(0.02)
    return False


out = None
exec(BODY)
print("RESULT " + json.dumps(out))
"""


class _DrivenSubprocess(unittest.TestCase):
    """A fresh scratch KAIZEN_REPO_ROOT, K1-initialized; ``drive(body)`` runs the scenario in a child
    process pinned to that plane and returns the parsed RESULT."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-h0-"))
        self.addCleanup(_rmtree, self.root)
        self.assertEqual(kaizen(self.root, "K1")[0], 0)

    def drive(self, body: str, *, env: dict | None = None, timeout: float = 120.0) -> dict:
        script = "BODY = " + repr(body) + "\n" + _PREAMBLE
        full_env = dict(os.environ)
        full_env["KAIZEN_REPO_ROOT"] = str(self.root)
        if env:
            full_env.update(env)
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            cwd=str(REPO_ROOT), env=full_env, timeout=timeout,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        self.fail(f"no RESULT.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-2000:]}")


def _rmtree(path: Path) -> None:
    """Best-effort removal of a scratch session-drive plane, tolerating residual Windows handles during cleanup."""
    import shutil
    shutil.rmtree(path, ignore_errors=True)


# --- 1. start -> events -> final ---------------------------------------------------------------

class StartEventsFinalTest(_DrivenSubprocess):
    def test_driven_turn_starts_streams_and_finalizes_ok(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "install_factory(sup, scripted_provider([final_reply('done')]))\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'hi'}})\n"
            "rid = start['agent_run_id']\n"
            "state = wait_terminal(sup, rid)\n"
            "ev = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})\n"
            "sup.shutdown()\n"
            "seqs = [e['sequence_no'] for e in ev['events']]\n"
            "kinds = [(e['event_kind'], e['marker']) for e in ev['events']]\n"
            "fin_bodies = [e['body'] for e in ev['events'] if e['event_kind'] == 'finalization']\n"
            "out = {'start': start, 'terminal': state['terminal'], 'terminal_state': state['terminal_state'],\n"
            "       'seqs': seqs, 'kinds': kinds, 'cursor': ev['cursor'], 'ev_terminal': ev['terminal'],\n"
            "       'fin_bodies': fin_bodies}\n"
        )
        self.assertEqual(out["start"]["status"], "OK")
        self.assertTrue(out["start"]["session_id"].startswith("as_"))
        self.assertTrue(out["start"]["agent_run_id"].startswith("ar_"))
        self.assertTrue(out["terminal"])
        self.assertEqual(out["terminal_state"], "success")
        # since=0 gapless-from-1 replay (the webview depends on this).
        self.assertEqual(out["seqs"], list(range(1, len(out["seqs"]) + 1)))
        # The T6 stream carries turn/tool_call/approval/finalization spans; the C1 session envelope is
        # NOT a T6 event kind (it is the agent_sessions row), so `session open` is not on the stream.
        self.assertIn(["turn", "open"], out["kinds"])
        self.assertIn(["turn", "close_ok"], out["kinds"])
        self.assertIn(["finalization", "close_ok"], out["kinds"])
        # The success finalization body carries the assistant's final answer -- the only stream event
        # that can (turn close_ok payload is status-only); the webview transcript reads it from here.
        self.assertEqual(out["fin_bodies"], ["done"])
        self.assertEqual(out["cursor"], out["seqs"][-1])
        self.assertTrue(out["ev_terminal"])


class LegacyContinuationTest(_DrivenSubprocess):
    """Owner 2026-07-11: no conversation dead-ends. A LEGACY conversation (no stored snapshot -- rows
    from before continuation shipped) still continues: the resume cuts a FRESH snapshot from current
    policy and HEALS it onto the C1 (new profile_hash recorded), and the transcript preamble builder
    renders the durable history for engines whose vendor context is unrecoverable."""

    def test_legacy_row_without_snapshot_continues_and_heals(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "sup = Supervisor(); sup.boot()\n"
            "install_factory(sup, scripted_provider([final_reply('one'), final_reply('healed')]))\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'first'}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "wait_idle(sup, rid)\n"
            "close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "# Simulate a pre-continuation legacy row: strip the stored snapshot.\n"
            "conn = db.connect()\n"
            "conn.execute('UPDATE agent_sessions SET policy_snapshot = NULL WHERE id = ?', (sid,))\n"
            "conn.commit()\n"
            "turn = sup._handle_control({'op': 'session/turn', 'args': {'agent_run_id': rid, 'prompt': 'again'}})\n"
            "new_rid = turn.get('agent_run_id')\n"
            "wait_idle(sup, new_rid)\n"
            "adapter_history = [dict(item) for item in sup._driven[new_rid].adapter.conversation_history]\n"
            "healed = db.fetch_one('SELECT policy_snapshot IS NOT NULL FROM agent_sessions WHERE id = ?', (sid,))[0]\n"
            "profile_events = [json.loads(r[0]) for r in db.fetch_all(\n"
            "    \"SELECT body FROM agent_events WHERE agent_run_id = ? AND event_kind = 'profile'\", (new_rid,))]\n"
            "sup.shutdown()\n"
            "out = {'close': close.get('status'), 'adapter_history': adapter_history,\n"
            "       'turn': {k: turn.get(k) for k in ('status', 'agent_run_id', 'resumed_from', 'resume_fidelity', 'omitted_message_count')},\n"
            "       'rid': rid, 'healed': bool(healed),\n"
            "       'profile_hashes': [e.get('profile_hash') for e in profile_events],\n"
            "       'profile_fidelity': [e.get('resume_fidelity') for e in profile_events],\n"
            "       'profile_omitted': [e.get('omitted_message_count') for e in profile_events],\n"
            "       'start_hash': start['profile_hash']}\n"
        )
        self.assertEqual(out["close"], "OK")
        # The fresh local adapter receives one fixed, metadata-only continuation frame rather than an
        # unbounded per-message RAM seed; complete durable messages precede the retained current prompt.
        framed = out["adapter_history"][0]["content"]
        self.assertIn("KAIZEN_DURABLE_HISTORY_V1", framed)
        self.assertIn('"role":"user","text":"first"', framed)
        self.assertIn('"role":"assistant","text":"one"', framed)
        self.assertTrue(framed.endswith("again"))
        # The legacy conversation continues; the C1 is healed with a fresh stored snapshot whose hash
        # rides the resumed leg's profile event.
        self.assertEqual(out["turn"]["status"], "OK", out["turn"])
        self.assertEqual(out["turn"]["resumed_from"], out["rid"])
        self.assertTrue(out["healed"])
        self.assertEqual(len(out["profile_hashes"]), 1)
        self.assertTrue(out["profile_hashes"][0])
        self.assertEqual(out["turn"]["resume_fidelity"], "reduced")
        self.assertEqual(out["turn"]["omitted_message_count"], 0)
        self.assertEqual(out["profile_fidelity"], ["reduced"])
        self.assertEqual(out["profile_omitted"], [0])


class BoundedContinuationMetadataTest(_DrivenSubprocess):
    def test_oversize_current_prompt_denies_before_new_run_or_event(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "sup = Supervisor(); sup.boot()\n"
            "install_factory(sup, scripted_provider([final_reply('one')]))\n"
            "start = sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'first'}})\n"
            "rid = start['agent_run_id']\n"
            "wait_idle(sup, rid)\n"
            "sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}})\n"
            "before = [db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0], db.fetch_one('SELECT COUNT(*) FROM agent_events')[0]]\n"
            "denied = sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'😀' * 300000}})\n"
            "after = [db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0], db.fetch_one('SELECT COUNT(*) FROM agent_events')[0]]\n"
            "sup.shutdown()\n"
            "out={'code':denied.get('code'),'field':denied.get('field'),'before':before,'after':after}\n"
        )
        self.assertEqual(out["code"], "DENIED_CONTEXT_TOO_LARGE")
        self.assertEqual(out["field"], "prompt")
        self.assertEqual(out["after"], out["before"])

    def test_reduced_profile_and_list_restore_only_expired_reference_metadata(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "sup = Supervisor(); sup.boot()\n"
            "install_factory(sup, scripted_provider([final_reply('one'), final_reply('two')]))\n"
            "start = sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'first prompt'}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "wait_idle(sup, rid)\n"
            "digest='a' * 64\n"
            "selection={'id':'selection-1','kind':'selection','source_path':'src/app.py',"
            "'range':{'start':{'line':0,'character':0},'end':{'line':0,'character':3}},"
            "'snapshot_ref':'sha256:'+digest,'sha256':digest,'bytes':3,'encoding':'utf-8'}\n"
            "sup._record_chat_message(rid,'user','metadata marker',context_refs=(selection,))\n"
            "sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}})\n"
            "turn=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'continue'}})\n"
            "new_rid=turn['agent_run_id']\n"
            "wait_idle(sup,new_rid)\n"
            "profile=json.loads(db.fetch_one(\"SELECT body FROM agent_events WHERE agent_run_id=? AND event_kind='profile'\",(new_rid,))[0])\n"
            "listed=sup._handle_control({'op':'session/list','args':{'controller':'driven','limit':1000}})\n"
            "entry=listed['sessions'][0]\n"
            "sup.shutdown()\n"
            "out={'turn':turn,'profile':profile,'entry':entry,'wire_bytes':len((json.dumps(listed,ensure_ascii=True)+'\\n').encode('utf-8'))}\n"
        )
        self.assertEqual(out["turn"]["resume_fidelity"], "reduced")
        self.assertEqual(out["profile"]["resume_fidelity"], "reduced")
        self.assertEqual(out["entry"]["resume_fidelity"], "reduced")
        self.assertEqual(out["profile"]["omitted_message_count"], 0)
        self.assertEqual(out["entry"]["omitted_message_count"], 0)
        self.assertEqual(out["profile"]["expired_artifacts"][0]["availability"], "expired")
        self.assertEqual(out["entry"]["expired_artifacts"], out["profile"]["expired_artifacts"])
        self.assertNotIn("snapshot_ref", json.dumps(out["entry"]["expired_artifacts"]))
        self.assertLess(out["wire_bytes"], 1 << 20)


class SessionListCostTest(_DrivenSubprocess):
    def test_limit_1000_is_batched_bounded_and_keeps_daemon_title_snippet(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "import time\n"
            "count=1000\n"
            "sessions=[]; runs=[]; events=[]\n"
            "for index in range(count):\n"
            " sid=f'as_{index:04d}'; rid=f'ar_{index:04d}'; stamp=f'2026-01-01T00:00:{index % 60:02d}Z'; prompt=f'Prompt {index}'\n"
            " sessions.append((sid,stamp,'kaizen','orchestrate','none','closed','session','h',prompt,'local_llm'))\n"
            " runs.append((rid,stamp,'other','app-server','completed','run','', 'h',sid,'local_llm','none','plan'))\n"
            " events.append((f'e_chat_{index}',stamp,rid,1,'chat_message','point','chat',json.dumps({'role':'user','text':prompt,'source':'driven'}),'h'))\n"
            " events.append((f'e_fin_{index}',stamp,rid,2,'finalization','close_ok','done','', 'h'))\n"
            "def seed(conn,attempt):\n"
            " conn.executemany(\"INSERT INTO agent_sessions (id,created_at,controller,mode,auth_mode,state,summary,content_hash,title,engine) VALUES (?,?,?,?,?,?,?,?,?,?)\",sessions)\n"
            " conn.executemany(\"INSERT INTO agent_runs (id,created_at,agent_type,surface,state,summary,body,content_hash,session_id,engine,auth_mode,permission_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)\",runs)\n"
            " conn.executemany(\"INSERT INTO agent_events (id,created_at,agent_run_id,sequence_no,event_kind,marker,summary,body,content_hash) VALUES (?,?,?,?,?,?,?,?,?)\",events)\n"
            "db.write_tx(seed)\n"
            "sup=Supervisor()\n"
            "calls=[]; one=db.fetch_one; many=db.fetch_all\n"
            "def counted_one(*args,**kwargs): calls.append('one'); return one(*args,**kwargs)\n"
            "def counted_many(*args,**kwargs): calls.append('many'); return many(*args,**kwargs)\n"
            "db.fetch_one=counted_one; db.fetch_all=counted_many\n"
            "started=time.perf_counter()\n"
            "try: listed=sup._handle_control({'op':'session/list','args':{'controller':'driven','limit':1000}})\n"
            "finally: db.fetch_one=one; db.fetch_all=many\n"
            "elapsed=time.perf_counter()-started\n"
            "wire=len((json.dumps(listed,ensure_ascii=True)+'\\n').encode('utf-8'))\n"
            "entry=listed['sessions'][0]\n"
            "sup.shutdown()\n"
            "out={'status':listed['status'],'calls':len(calls),'wire':wire,'elapsed':elapsed,\n"
            " 'returned':listed['sessions_returned'],'total':listed['sessions_total'],'truncated':listed['truncated'],\n"
            " 'title':entry['title'],'snippet':entry['snippet']}\n"
        )
        self.assertEqual(out["status"], "OK", out)
        self.assertLessEqual(out["calls"], 11, out)
        self.assertLess(out["wire"], (1 << 20) - 4096, out)
        self.assertLess(out["elapsed"], 5.0, out)
        self.assertGreater(out["returned"], 0, out)
        self.assertEqual(out["total"], 1000, out)
        self.assertEqual(out["title"], out["snippet"])


class MultiTurnLifecycleTest(_DrivenSubprocess):
    def test_two_turns_share_c1_t5_history_and_finalize_only_on_close(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "prov = scripted_provider([final_reply('first answer'), final_reply('second answer')])\n"
            "install_factory(sup, prov)\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'first prompt'}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "wait_idle(sup, rid)\n"
            "state1 = sup._safe_reduce(rid)\n"
            "ev1 = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})\n"
            "turn2 = sup._handle_control({'op': 'session/turn', 'args': {'agent_run_id': rid, 'prompt': 'second prompt'}})\n"
            "wait_idle(sup, rid)\n"
            "state2 = sup._safe_reduce(rid)\n"
            "ev2 = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})\n"
            "listed_idle = sup._handle_control({'op': 'session/list', 'args': {'controller': 'driven', 'limit': 1}})['sessions'][0]\n"
            "from kaizen_components import db\n"
            "counts = [db.fetch_one('SELECT COUNT(*) FROM agent_sessions WHERE id = ?', (sid,))[0],\n"
            "          db.fetch_one('SELECT COUNT(*) FROM agent_runs WHERE id = ?', (rid,))[0]]\n"
            "t8_before = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
            "chat = [json.loads(e['body']) for e in ev2['events'] if e['event_kind'] == 'chat_message']\n"
            "second_call = prov.state['calls'][1]\n"
            "close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "state3 = sup._safe_reduce(rid)\n"
            "ev3 = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})\n"
            "listed_closed = sup._handle_control({'op': 'session/list', 'args': {'controller': 'driven', 'limit': 1}})['sessions'][0]\n"
            "t8_after = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
            "c1_state = db.fetch_one('SELECT state FROM agent_sessions WHERE id = ?', (sid,))[0]\n"
            "sup.shutdown()\n"
            "out = {'start': start, 'turn2': turn2, 'close': close, 'counts': counts,\n"
            "       'terminal1': state1['terminal'], 'terminal2': state2['terminal'],\n"
            "       'terminal3': state3['terminal'], 'terminal_state3': state3['terminal_state'],\n"
            "       'event_state1': ev1['turn_state'], 'event_state2': ev2['turn_state'],\n"
            "       't8_before': t8_before, 't8_after': t8_after, 'chat': chat,\n"
            "       'second_call': second_call, 'seqs': [e['sequence_no'] for e in ev3['events']],\n"
            "       'c1_state': c1_state, 'listed_idle': listed_idle, 'listed_closed': listed_closed}\n"
        )
        self.assertEqual(out["start"]["status"], "OK")
        self.assertEqual(out["turn2"]["status"], "OK")
        self.assertEqual(out["counts"], [1, 1])
        self.assertFalse(out["terminal1"])
        self.assertFalse(out["terminal2"])
        self.assertEqual(out["event_state1"], "idle")
        self.assertEqual(out["event_state2"], "idle")
        self.assertEqual(out["listed_idle"]["session_id"], out["start"]["session_id"])
        self.assertEqual(out["listed_idle"]["controller"], "driven")
        self.assertEqual(out["listed_idle"]["latest_run_id"], out["start"]["agent_run_id"])
        self.assertEqual(out["listed_idle"]["latest_run_state"], "idle")
        self.assertEqual(len(out["listed_idle"]["runs"]), 1)
        self.assertEqual(out["listed_idle"]["runs"][0]["engine"], "local_llm")
        self.assertEqual(out["listed_idle"]["runs"][0]["profile"]["permission_mode"], "plan")
        self.assertEqual(out["t8_before"], 0)
        self.assertEqual(
            [(message["role"], message["text"]) for message in out["chat"]],
            [
                ("user", "first prompt"),
                ("assistant", "first answer"),
                ("user", "second prompt"),
                ("assistant", "second answer"),
            ],
        )
        # The second provider request carries the first complete exchange plus the new prompt.
        self.assertEqual(
            [(message["role"], message["content"]) for message in out["second_call"][1:]],
            [
                ("user", "first prompt"),
                ("assistant", "first answer"),
                ("user", "second prompt"),
            ],
        )
        self.assertEqual(out["close"]["status"], "OK")
        self.assertTrue(out["terminal3"])
        self.assertEqual(out["terminal_state3"], "success")
        self.assertEqual(out["t8_after"], 1)
        self.assertEqual(out["c1_state"], "closed")
        self.assertEqual(out["listed_closed"]["state"], "closed")
        self.assertEqual(out["listed_closed"]["latest_run_state"], "terminal")
        self.assertEqual(out["listed_closed"]["latest_terminal_state"], "success")
        self.assertEqual(out["seqs"], list(range(1, len(out["seqs"]) + 1)))

    def test_running_refuses_concurrent_turn_and_closed_conversation_continues(self) -> None:
        # Owner 2026-07-11: no conversation dead-ends. Mid-turn rules are unchanged (concurrent turn
        # refused, mid-turn close refused); an explicit close still writes the leg's single success T8
        # -- but a LATER turn CONTINUES the conversation as a new linked leg (the C1 reopens), and
        # close/turn against the OLD leg id refuse (no forking, one live leg).
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "import threading, time\n"
            "gate = threading.Event()\n"
            "base = scripted_provider([final_reply('done'), final_reply('continued')])\n"
            "def prov(messages, **opts):\n"
            "    gate.wait(5.0)\n"
            "    return base(messages, **opts)\n"
            "install_factory(sup, prov)\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'one'}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "deadline = time.monotonic() + 5.0\n"
            "sess = sup._get_driven(rid)\n"
            "while time.monotonic() < deadline and sess.adapter.active_turn_id is None:\n"
            "    time.sleep(0.01)\n"
            "concurrent = sup._handle_control({'op': 'session/turn', 'args': {'agent_run_id': rid, 'prompt': 'two'}})\n"
            "mid_close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "gate.set()\n"
            "wait_idle(sup, rid)\n"
            "closed = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "from kaizen_components import db\n"
            "state_closed = db.fetch_one('SELECT state FROM agent_sessions WHERE id = ?', (sid,))[0]\n"
            "after_turn = sup._handle_control({'op': 'session/turn', 'args': {'agent_run_id': rid, 'prompt': 'three'}})\n"
            "new_rid = after_turn.get('agent_run_id')\n"
            "wait_idle(sup, new_rid)\n"
            "state_reopened = db.fetch_one('SELECT state FROM agent_sessions WHERE id = ?', (sid,))[0]\n"
            "old_close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "new_close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': new_rid}})\n"
            "old_t8 = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
            "sup.shutdown()\n"
            "out = {'concurrent': concurrent, 'mid_close': mid_close, 'closed': closed,\n"
            "       'state_closed': state_closed, 'state_reopened': state_reopened,\n"
            "       'after_turn': {k: after_turn.get(k) for k in ('status', 'agent_run_id', 'resumed_from')},\n"
            "       'rid': rid, 'old_close': old_close, 'new_close': new_close, 'old_t8': old_t8}\n"
        )
        self.assertEqual(out["concurrent"]["code"], "DENIED_TURN_IN_PROGRESS")
        self.assertEqual(out["mid_close"]["code"], "DENIED_SESSION_NOT_IDLE")
        self.assertEqual(out["closed"]["status"], "OK")
        self.assertEqual(out["state_closed"], "closed")
        # The closed conversation CONTINUES: a new linked leg under the reopened C1.
        self.assertEqual(out["after_turn"]["status"], "OK")
        self.assertEqual(out["after_turn"]["resumed_from"], out["rid"])
        self.assertNotEqual(out["after_turn"]["agent_run_id"], out["rid"])
        self.assertEqual(out["state_reopened"], "open")
        # The OLD leg stays immutable (single T8, no re-close); the NEW leg closes normally.
        self.assertEqual(out["old_close"]["code"], "DENIED_SESSION_TERMINAL")
        self.assertEqual(out["old_t8"], 1)
        self.assertEqual(out["new_close"]["status"], "OK")

    def test_running_refuses_artifact_turn_before_cache_or_record_mutation(self) -> None:
        out = self.drive(
            "from pathlib import Path\n"
            "from kaizen_components import db\n"
            "import threading, time\n"
            "sup=Supervisor(); sup.boot(); gate=threading.Event()\n"
            "base=scripted_provider([final_reply('done')])\n"
            "def prov(messages,**opts): gate.wait(5.0); return base(messages,**opts)\n"
            "install_factory(sup,prov)\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','profile':{'permission_mode':'ask'}}})\n"
            "rid=start['agent_run_id']; sess=sup._get_driven(rid); deadline=time.monotonic()+5.0\n"
            "while time.monotonic()<deadline and sess.adapter.active_turn_id is None: time.sleep(0.01)\n"
            "sess.image_attachments=True; sess.governed_context=True\n"
            "root=Path(sup.repo_root); source=root/'src'/'concurrent-context.txt'; source.parent.mkdir(parents=True,exist_ok=True); source.write_text('bounded context',encoding='utf-8')\n"
            "image_bytes=b'\\x89PNG\\r\\n\\x1a\\nbody'; staged=sup._artifact_cache.store('images',image_bytes,scope_id='host',media_type='image/png',origin='host')\n"
            "image={'id':'img','kind':'image','artifact_ref':staged.artifact_ref,'sha256':staged.sha256,'bytes':staged.bytes,'media_type':'image/png'}\n"
            "context={'id':'ctx','kind':'file','source_path':'src/concurrent-context.txt'}\n"
            "cache_root=sup._artifact_cache.cache_root\n"
            "def cache_state(): return {p.relative_to(cache_root).as_posix():p.read_bytes().hex() for p in cache_root.rglob('*') if p.is_file()}\n"
            "def counts(): return [db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0],db.fetch_one('SELECT COUNT(*) FROM agent_events')[0],db.fetch_one('SELECT COUNT(*) FROM approval_requests')[0]]\n"
            "before_cache=cache_state(); before_counts=counts(); before_claim=sess.writer_claim_token\n"
            "concurrent=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'two','attachments':[image],'context_refs':[context]}})\n"
            "after_cache=cache_state(); after_counts=counts(); after_claim=sess.writer_claim_token\n"
            "gate.set(); wait_idle(sup,rid)\n"
            "before_failure_cache=cache_state(); before_failure_counts=counts()\n"
            "staging=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'stage','attachments':[image]}})\n"
            "materialization=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'materialize','context_refs':[{'id':'missing','kind':'file','source_path':'src/missing-context.txt'}]}})\n"
            "after_failure_cache=cache_state(); after_failure_counts=counts(); failure_state=sess.turn_state; failure_claim=sess.writer_claim_token\n"
            "sup.shutdown()\n"
            "out={'start':start['status'],'concurrent':concurrent,'cache_equal':before_cache==after_cache,'counts':[before_counts,after_counts],'claims':[before_claim,after_claim],'staging':staging,'materialization':materialization,'failure_cache_equal':before_failure_cache==after_failure_cache,'failure_counts':[before_failure_counts,after_failure_counts],'failure_state':failure_state,'failure_claim':failure_claim}\n"
        )
        self.assertEqual(out["start"], "OK")
        self.assertEqual(out["concurrent"]["code"], "DENIED_TURN_IN_PROGRESS")
        self.assertTrue(out["cache_equal"])
        self.assertEqual(out["counts"][0], out["counts"][1])
        self.assertEqual(out["claims"][0], out["claims"][1])
        self.assertEqual(out["staging"]["code"], "DENIED_ATTACHMENT_UNSUPPORTED")
        self.assertEqual(out["materialization"]["code"], "DENIED_CONTEXT_STALE")
        self.assertTrue(out["failure_cache_equal"])
        self.assertEqual(out["failure_counts"][0], out["failure_counts"][1])
        self.assertEqual(out["failure_state"], "idle")
        self.assertIsNone(out["failure_claim"])

    def test_redacted_and_oversize_messages_persist_explicit_placeholders(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "secret = 'sk-' + ('A' * 24)\n"
            "oversize = 'z' * ((1 << 20) + 1)\n"
            "prov = scripted_provider([final_reply(secret), final_reply('safe answer')])\n"
            "install_factory(sup, prov)\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': secret}})\n"
            "rid = start['agent_run_id']\n"
            "wait_idle(sup, rid)\n"
            "sup._handle_control({'op': 'session/turn', 'args': {'agent_run_id': rid, 'prompt': oversize}})\n"
            "wait_idle(sup, rid)\n"
            "sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "ev = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})\n"
            "chat_events = [e for e in ev['events'] if e['event_kind'] == 'chat_message']\n"
            "chat = [json.loads(e['body']) for e in chat_events]\n"
            "all_bodies = '\\n'.join(e['body'] or '' for e in ev['events'])\n"
            "sup.shutdown()\n"
            "out = {'chat': chat, 'codes': [e['code'] for e in chat_events],\n"
            "       'secret_present': secret in all_bodies, 'oversize_present': oversize in all_bodies}\n"
        )
        self.assertEqual(
            [message["text"] for message in out["chat"]],
            [
                "[message omitted: redaction policy]",
                "[message omitted: redaction policy]",
                "[message omitted: exceeds 1 MiB limit]",
                "safe answer",
            ],
        )
        self.assertEqual(
            out["codes"],
            ["DENIED_CHAT_MESSAGE_REDACTED", "DENIED_CHAT_MESSAGE_REDACTED",
             "DENIED_CHAT_MESSAGE_OVERSIZE", None],
        )
        self.assertFalse(out["secret_present"])
        self.assertFalse(out["oversize_present"])


# --- 2. ask -> approve-by-correlation_id -> tool runs ------------------------------------------

class ApproveByCorrelationTest(_DrivenSubprocess):
    def test_ask_approved_by_correlation_id_runs_the_tool(self) -> None:
        # No allow rule -> the echo tool intent decides ASK; the driver approves by the stream
        # correlation_id (race-free); the adapter waiter releases and the tool runs; then final.
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "prov = scripted_provider([tool_reply('echo', path='x'), final_reply('done')])\n"
            "install_factory(sup, prov, engine=make_engine(), tools=echo_tools())\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'go'}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "corr = wait_open_approval(sup, rid)\n"
            "appr = sup._handle_control({'op': 'approve', 'args': {'correlation_id': corr, 'session_id': sid, 'decision': 'approve'}})\n"
            "state = wait_terminal(sup, rid)\n"
            "ev = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})\n"
            "sup.shutdown()\n"
            "kinds = [(e['event_kind'], e['marker']) for e in ev['events']]\n"
            "out = {'corr': corr, 'appr': appr, 'terminal_state': state['terminal_state'], 'kinds': kinds}\n"
        )
        self.assertTrue(out["corr"])  # an approval/open surfaced with a correlation_id
        self.assertEqual(out["appr"]["status"], "OK")
        self.assertEqual(out["appr"]["state"], "approved")
        self.assertTrue(out["appr"]["waiter_released"])  # the LIVE adapter waiter was released
        self.assertEqual(out["terminal_state"], "success")
        self.assertIn(["approval", "open"], out["kinds"])
        self.assertIn(["approval", "resolved"], out["kinds"])
        self.assertIn(["tool_call", "close_ok"], out["kinds"])  # the tool actually ran post-approval

    def test_live_broker_waiter_wins_over_legacy_alt_key_collision(self) -> None:
        out = self.drive(
            "from kaizen_components import db, session_records\n"
            "sup=Supervisor(); sup.boot()\n"
            "install_factory(sup,scripted_provider([tool_reply('echo',path='x'),final_reply('done')]),engine=make_engine(),tools=echo_tools())\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'go'}}); rid,sid=start['agent_run_id'],start['session_id']\n"
            "corr=wait_open_approval(sup,rid); wait_waiter_parked(sup,rid,corr); managed=sup._get_driven(rid).live_approval_id(corr)\n"
            "legacy=db.new_id('apr')\n"
            "db.write_tx(lambda conn,attempt: session_records.insert_approval_conn(conn,approval_id=legacy,session_id=sid,correlation_id=corr,request_type='tool_approval',state='open',summary='legacy collision',created_at='2000-01-01T00:00:00+00:00',is_test=True))\n"
            "appr=sup._handle_control({'op':'approve','args':{'session_id':sid,'correlation_id':corr,'agent_run_id':rid,'decision':'approve'}})\n"
            "states=dict(db.fetch_all('SELECT id,state FROM approval_requests WHERE id IN (?,?)',(managed,legacy)))\n"
            "wait_idle(sup,rid); sup.shutdown(); out={'appr':appr,'managed':managed,'legacy':legacy,'states':states}\n"
        )
        self.assertEqual(out["appr"]["id"], out["managed"])
        self.assertEqual(out["states"][out["managed"]], "approved")
        self.assertEqual(out["states"][out["legacy"]], "open")

    def test_same_correlation_two_live_sessions_resolves_only_exact_session_run(self) -> None:
        out = self.drive(
            "sup=Supervisor(); sup.boot(); install_factory(sup,scripted_provider([final_reply('a'),final_reply('b')]))\n"
            "a=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'a'}}); b=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'b'}})\n"
            "wait_idle(sup,a['agent_run_id']); wait_idle(sup,b['agent_run_id']); corr='same-correlation'\n"
            "oa=sup._approval_broker.open(session_id=a['session_id'],agent_run_id=a['agent_run_id'],correlation_id=corr,is_test=True)\n"
            "ob=sup._approval_broker.open(session_id=b['session_id'],agent_run_id=b['agent_run_id'],correlation_id=corr,is_test=True)\n"
            "sa=sup._get_driven(a['agent_run_id']); sb=sup._get_driven(b['agent_run_id']); wa=sa.bind_broker_approval(corr,oa['id']); wb=sb.bind_broker_approval(corr,ob['id'])\n"
            "mismatch=sup._handle_control({'op':'approve','args':{'session_id':b['session_id'],'correlation_id':corr,'agent_run_id':a['agent_run_id'],'decision':'approve'}})\n"
            "dec=sup._handle_control({'op':'approve','args':{'session_id':a['session_id'],'correlation_id':corr,'agent_run_id':a['agent_run_id'],'decision':'approve'}})\n"
            "states=[sup._approval_broker.get(oa['id'])['state'],sup._approval_broker.get(ob['id'])['state']]; released=[wa._released,wb._released]\n"
            "sup._handle_control({'op':'approve','args':{'session_id':b['session_id'],'correlation_id':corr,'agent_run_id':b['agent_run_id'],'decision':'deny'}})\n"
            "sa.clear_broker_approval(corr,oa['id']); sb.clear_broker_approval(corr,ob['id']); sup.shutdown()\n"
            "out={'mismatch':mismatch,'dec':dec,'states':states,'released':released,'ids':[oa['id'],ob['id']]}\n"
        )
        self.assertEqual(out["mismatch"]["code"], "DENIED_APPROVAL_SCOPE_MISMATCH")
        self.assertEqual(out["dec"]["id"], out["ids"][0])
        self.assertEqual(out["states"], ["approved", "open"])
        self.assertEqual(out["released"], [True, False])


# --- 2b. approve BEFORE the waiter parks (the on-park re-check race fix) -----------------------

class ApproveBeforeParkRaceTest(_DrivenSubprocess):
    def test_approve_landing_before_park_still_runs_the_tool(self) -> None:
        # THE race: approve lands after the broker's atomic C4+open commit but before waiter binding.
        # The supervisor's narrow failure-injection seam blocks at that exact boundary; the post-bind
        # authoritative broker recheck must then deliver the already-committed decision.
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "import threading, time\n"
            "opened, release = threading.Event(), threading.Event()\n"
            "def before_park(info): opened.set(); release.wait(10.0)\n"
            "sup._approval_before_park = before_park\n"
            "prov = scripted_provider([tool_reply('echo', path='x'), final_reply('done')])\n"
            "install_factory(sup, prov, engine=make_engine(), tools=echo_tools())\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'go', 'approval_timeout': 30.0}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "opened.wait(10.0)\n"
            "corr = wait_open_approval(sup, rid)\n"  # C4 row persisted; the waiter is NOT parked yet
            "appr = sup._handle_control({'op': 'approve', 'args': {'correlation_id': corr, 'session_id': sid, 'decision': 'approve'}})\n"
            "release.set()\n"  # now let on_approval park -> the on-park re-check resolves it
            "state = wait_terminal(sup, rid)\n"
            "ev = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})\n"
            "sup.shutdown()\n"
            "kinds = [(e['event_kind'], e['marker']) for e in ev['events']]\n"
            "out = {'corr': corr, 'appr': appr, 'terminal_state': state['terminal_state'], 'kinds': kinds}\n"
        )
        self.assertTrue(out["corr"])
        self.assertEqual(out["appr"]["status"], "OK")
        self.assertEqual(out["appr"]["state"], "approved")
        # The daemon resolve found NO parked waiter (the approve raced ahead of the park) -- the on-park
        # re-check is what unblocked the turn.
        self.assertFalse(out["appr"]["waiter_released"])
        self.assertEqual(out["terminal_state"], "success")
        self.assertIn(["tool_call", "close_ok"], out["kinds"])  # the tool ran despite the race


class FreeTextApprovalTransportTest(_DrivenSubprocess):
    def test_exact_answers_release_a_typed_result_and_invalid_maps_leave_the_waiter_parked(self) -> None:
        out = self.drive(
            "import threading\n"
            "sup=Supervisor(); sup.boot(); install_factory(sup,scripted_provider([final_reply('idle')]))\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'start','approval_timeout':30.0}}); rid,sid=start['agent_run_id'],start['session_id']; sess=wait_idle(sup,rid)\n"
            "box={}\n"
            "request={'correlation_id':'free-text','request_type':'requestUserInput','summary':'Answer required','questions':[{'id':'reason','header':'Decision','question':'Why continue?','options':[],'ignored':'drop'},{'id':'mode','question':'Choose mode','options':[{'label':'Safe','description':'Use checks','ignored':True}]}],'ignored':'drop'}\n"
            "thread=threading.Thread(target=lambda: box.setdefault('result',sup._broker_adapter_approval(sess,request))); thread.start()\n"
            "corr=wait_open_approval(sup,rid); parked=wait_waiter_parked(sup,rid,corr)\n"
            "events=sup._handle_control({'op':'session/events','args':{'agent_run_id':rid,'since':0}})['events']; card=json.loads(next(e['body'] for e in events if e['event_kind']=='approval' and e['marker']=='open'))\n"
            "base={'correlation_id':corr,'session_id':sid,'decision':'approve'}; invalid=[]\n"
            "cases=[('missing',None),('wrong',{'other':'x'}),('extra',{'reason':'x','other':'y'}),('blank',{'reason':'   '}),('nul',{'reason':'bad\\x00value'}),('surrogate',{'reason':'\\ud800'}),('oversize',{'reason':'x'*(64*1024+1)})]\n"
            "for name,answers in cases:\n"
            " args=dict(base)\n"
            " if name!='missing': args['answers']=answers\n"
            " invalid.append([name,sup._handle_control({'op':'approve','args':args}).get('code')])\n"
            "approved=sup._handle_control({'op':'approve','args':{**base,'answers':{'reason':'  exact answer  '}}}); thread.join(5.0); result=box.get('result'); alive=thread.is_alive()\n"
            "sup.shutdown(); out={'corr':corr,'parked':parked,'card':card,'invalid':invalid,'approved':approved,'alive':alive,'type':type(result).__name__,'decision':getattr(result,'decision',None),'updated_input':getattr(result,'updated_input',None)}\n"
        )
        self.assertEqual(out["corr"], "free-text")
        self.assertTrue(out["parked"])
        self.assertEqual(out["card"], {"questions": [
            {"id": "reason", "question": "Why continue?", "header": "Decision"},
            {"id": "mode", "question": "Choose mode", "options": [{"label": "Safe", "description": "Use checks"}]},
        ]})
        self.assertEqual({code for _name, code in out["invalid"]}, {"DENIED_USER_INPUT_ANSWER_REQUIRED"}, out["invalid"])
        self.assertEqual(out["approved"]["status"], "OK")
        self.assertTrue(out["approved"]["waiter_released"])
        self.assertFalse(out["alive"])
        self.assertEqual(out["type"], "BrokerApprovalResult")
        self.assertEqual(out["decision"], "approved")
        self.assertEqual(out["updated_input"], {"answers": {"reason": "  exact answer  "}})

    def test_exact_answers_survive_approve_before_park(self) -> None:
        out = self.drive(
            "import threading\n"
            "sup=Supervisor(); sup.boot(); install_factory(sup,scripted_provider([final_reply('idle')]))\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'start','approval_timeout':30.0}}); rid,sid=start['agent_run_id'],start['session_id']; sess=wait_idle(sup,rid)\n"
            "opened,release=threading.Event(),threading.Event(); sup._approval_before_park=lambda _info:(opened.set(),release.wait(10.0))\n"
            "box={}; request={'correlation_id':'free-race','request_type':'requestUserInput','questions':[{'id':'reason','question':'Why?'}]}\n"
            "thread=threading.Thread(target=lambda: box.setdefault('result',sup._broker_adapter_approval(sess,request))); thread.start(); opened.wait(10.0)\n"
            "approved=sup._handle_control({'op':'approve','args':{'correlation_id':'free-race','session_id':sid,'decision':'approve','answers':{'reason':'race answer'}}}); release.set(); thread.join(5.0); result=box.get('result'); alive=thread.is_alive()\n"
            "sup.shutdown(); out={'approved':approved,'alive':alive,'type':type(result).__name__,'decision':getattr(result,'decision',None),'updated_input':getattr(result,'updated_input',None)}\n"
        )
        self.assertEqual(out["approved"]["status"], "OK")
        self.assertFalse(out["approved"]["waiter_released"])
        self.assertFalse(out["alive"])
        self.assertEqual(out["type"], "BrokerApprovalResult")
        self.assertEqual(out["decision"], "approved")
        self.assertEqual(out["updated_input"], {"answers": {"reason": "race answer"}})


# --- 3. ask -> deny ---------------------------------------------------------------------------

class DenyTest(_DrivenSubprocess):
    def test_ask_denied_by_correlation_id_declines_the_tool(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "prov = scripted_provider([tool_reply('echo', path='x'), final_reply('after-deny')])\n"
            "install_factory(sup, prov, engine=make_engine(), tools=echo_tools())\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'go'}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "corr = wait_open_approval(sup, rid)\n"
            "deny = sup._handle_control({'op': 'approve', 'args': {'correlation_id': corr, 'session_id': sid, 'decision': 'deny'}})\n"
            "state = wait_terminal(sup, rid)\n"
            "ev = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})\n"
            "sup.shutdown()\n"
            "kinds = [(e['event_kind'], e['marker']) for e in ev['events']]\n"
            "out = {'deny': deny, 'terminal_state': state['terminal_state'], 'kinds': kinds}\n"
        )
        self.assertEqual(out["deny"]["status"], "OK")
        self.assertEqual(out["deny"]["state"], "denied")
        self.assertIn(["approval", "declined"], out["kinds"])
        self.assertIn(["tool_call", "close_fail"], out["kinds"])  # denied -> tool_call closed fail
        self.assertEqual(out["terminal_state"], "success")  # the turn recovers and still finals OK


# --- 4. ask timeout fail-closed ---------------------------------------------------------------

class AskTimeoutTest(_DrivenSubprocess):
    def test_ask_times_out_fail_closed_no_client_decision(self) -> None:
        # approval_timeout tiny; the driver NEVER approves. The waiter's sole clock (the adapter
        # approval_timeout) fires -> deny (fail-closed); the tool is declined; the turn still finals.
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "prov = scripted_provider([tool_reply('echo', path='x'), final_reply('after-timeout')])\n"
            "install_factory(sup, prov, engine=make_engine(), tools=echo_tools())\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'go', 'approval_timeout': 0.2}})\n"
            "rid = start['agent_run_id']\n"
            "state = wait_terminal(sup, rid)\n"
            "ev = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})\n"
            "sup.shutdown()\n"
            "kinds = [(e['event_kind'], e['marker']) for e in ev['events']]\n"
            "out = {'terminal_state': state['terminal_state'], 'kinds': kinds}\n"
        )
        self.assertIn(["approval", "open"], out["kinds"])
        self.assertIn(["approval", "timed_out"], out["kinds"])  # broker commits timeout before release
        self.assertIn(["tool_call", "close_fail"], out["kinds"])
        self.assertEqual(out["terminal_state"], "success")


# --- 5. steer mid-turn ------------------------------------------------------------------------

class SteerTest(_DrivenSubprocess):
    def test_steer_injects_into_next_iteration(self) -> None:
        # A slow first reply (blocks on a gate the driver releases after steering) would be ideal, but
        # the scripted provider returns instantly. Instead: the provider's FIRST reply is a parse-error
        # (keeps the turn looping without a final), the driver steers, and we assert the steer message
        # landed in a later provider call's message list (inject-next-iteration), then a final ends it.
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "import threading, time\n"
            "gate = threading.Event()\n"
            "seen = {'steered': False}\n"
            "# A provider that parks on the first call until the driver has steered, so the steer is\n"
            "# guaranteed queued before the 2nd iteration drains it.\n"
            "base = scripted_provider(['not json (parse error)', final_reply('done')])\n"
            "def prov(messages, **opts):\n"
            "    if base.state['i'] == 0:\n"
            "        gate.wait(5.0)\n"
            "    else:\n"
            "        seen['steered'] = any('STEER-MARK' in (m.get('content') or '') for m in messages)\n"
            "    return base(messages, **opts)\n"
            "prov.state = base.state\n"
            "install_factory(sup, prov)\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'go'}})\n"
            "rid = start['agent_run_id']\n"
            "# Wait for the turn to be in-flight (active turn set), then steer, then release the gate.\n"
            "deadline = time.monotonic() + 5.0\n"
            "while time.monotonic() < deadline and sup._get_driven(rid) is None:\n"
            "    time.sleep(0.01)\n"
            "sess = sup._get_driven(rid)\n"
            "while time.monotonic() < deadline and sess.adapter._active_turn is None:\n"
            "    time.sleep(0.01)\n"
            "steer = sup._handle_control({'op': 'session/steer', 'args': {'agent_run_id': rid, 'instruction': 'STEER-MARK focus'}})\n"
            "gate.set()\n"
            "state = wait_terminal(sup, rid)\n"
            "sup.shutdown()\n"
            "out = {'steer': steer, 'steered_seen': seen['steered'], 'terminal_state': state['terminal_state']}\n"
        )
        self.assertEqual(out["steer"]["status"], "OK")
        self.assertTrue(out["steer"]["queued"])
        self.assertTrue(out["steered_seen"])  # the steer message reached a later iteration's messages
        self.assertEqual(out["terminal_state"], "success")

    def test_steer_unknown_run_denied(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "resp = sup._handle_control({'op': 'session/steer', 'args': {'agent_run_id': 'nope', 'instruction': 'x'}})\n"
            "sup.shutdown()\n"
            "out = {'resp': resp}\n"
        )
        self.assertEqual(out["resp"]["code"], "DENIED_AGENT_RUN_NOT_DRIVEN")


# --- 6. interrupt -----------------------------------------------------------------------------

class InterruptTest(_DrivenSubprocess):
    def test_interrupt_cancels_the_turn_at_next_iteration(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "import threading, time\n"
            "gate = threading.Event()\n"
            "base = scripted_provider(['not json', 'not json', final_reply('unreached')])\n"
            "def prov(messages, **opts):\n"
            "    if base.state['i'] == 0:\n"
            "        gate.wait(5.0)\n"
            "    return base(messages, **opts)\n"
            "prov.state = base.state\n"
            "install_factory(sup, prov)\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'go'}})\n"
            "rid = start['agent_run_id']\n"
            "deadline = time.monotonic() + 5.0\n"
            "while time.monotonic() < deadline and sup._get_driven(rid) is None:\n"
            "    time.sleep(0.01)\n"
            "sess = sup._get_driven(rid)\n"
            "while time.monotonic() < deadline and sess.adapter._active_turn is None:\n"
            "    time.sleep(0.01)\n"
            "resp = sup._handle_control({'op': 'session/interrupt', 'args': {'agent_run_id': rid}})\n"
            "gate.set()\n"
            "state = wait_terminal(sup, rid)\n"
            "ev = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})\n"
            "sup.shutdown()\n"
            "kinds = [(e['event_kind'], e['marker']) for e in ev['events']]\n"
            "out = {'resp': resp, 'terminal': state['terminal'], 'terminal_state': state['terminal_state'],\n"
            "       'kinds': kinds}\n"
        )
        self.assertEqual(out["resp"]["status"], "OK")
        self.assertTrue(out["resp"]["interrupted"])
        self.assertTrue(out["terminal"])
        # A healthy interrupt returns the conversation to idle; wait_terminal then performs the explicit
        # close, so the one T8 is successful.
        self.assertEqual(out["terminal_state"], "success")
        # The interrupted turn's terminal marker (turn/close_canceled) now reaches the T6 stream: it is a
        # registered turn marker, so the recorder guard passes it (previously silently dropped).
        self.assertIn(["turn", "close_canceled"], out["kinds"])


# --- 7. kill (waiters denied, adapter dead) ---------------------------------------------------

class KillTest(_DrivenSubprocess):
    def test_kill_denies_waiters_kills_adapter_and_finalizes(self) -> None:
        # A turn parked on an approval (no allow rule, long timeout). kill denies the parked waiter,
        # kills the adapter, and finalizes canceled. The turn thread thereafter finds the run terminal.
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "import time\n"
            "prov = scripted_provider([tool_reply('echo', path='x'), final_reply('unreached')])\n"
            "install_factory(sup, prov, engine=make_engine(), tools=echo_tools())\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'go', 'approval_timeout': 30.0}})\n"
            "rid = start['agent_run_id']\n"
            "corr = wait_open_approval(sup, rid)\n"  # ensure the ask surfaced + C4 row exists
            "parked = wait_waiter_parked(sup, rid, corr)\n"  # ensure the adapter waiter is actually parked
            "kill = sup._handle_control({'op': 'session/kill', 'args': {'agent_run_id': rid}})\n"
            "state = wait_terminal(sup, rid)\n"
            "# The adapter is dead: a fresh start_turn on it refuses DENIED_KILLED.\n"
            "sess_gone = sup._get_driven(rid) is None\n"
            "sup.shutdown()\n"
            "out = {'corr_seen': bool(corr), 'parked': parked, 'kill': kill, 'terminal': state['terminal'],\n"
            "       'terminal_state': state['terminal_state'], 'sess_gone': sess_gone}\n"
        )
        self.assertTrue(out["corr_seen"])
        self.assertTrue(out["parked"])  # the adapter waiter was parked before kill
        self.assertEqual(out["kill"]["status"], "OK")
        self.assertTrue(out["kill"]["killed"])
        self.assertGreaterEqual(out["kill"]["waiters_denied"], 1)  # the parked ask failed closed
        self.assertTrue(out["terminal"])
        self.assertEqual(out["terminal_state"], "failure")  # canceled
        self.assertTrue(out["sess_gone"])  # deregistered

    def test_kill_unknown_run_denied(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "resp = sup._handle_control({'op': 'session/kill', 'args': {'agent_run_id': 'nope'}})\n"
            "sup.shutdown()\n"
            "out = {'resp': resp}\n"
        )
        self.assertEqual(out["resp"]["code"], "DENIED_AGENT_RUN_NOT_DRIVEN")


# --- 8. cursor pagination gapless -------------------------------------------------------------

class CursorPaginationTest(_DrivenSubprocess):
    def test_cursor_paginates_gaplessly(self) -> None:
        # Page the run's events with limit=2 following the cursor; assert the concatenation is the full
        # gapless-from-1 sequence with no gaps or overlaps.
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "install_factory(sup, scripted_provider([final_reply('done')]))\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'hi'}})\n"
            "rid = start['agent_run_id']\n"
            "wait_terminal(sup, rid)\n"
            "cursor = 0\n"
            "collected = []\n"
            "for _ in range(50):\n"
            "    ev = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': cursor, 'limit': 2}})\n"
            "    batch = [e['sequence_no'] for e in ev['events']]\n"
            "    if not batch:\n"
            "        break\n"
            "    collected += batch\n"
            "    cursor = ev['cursor']\n"
            "full = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})\n"
            "sup.shutdown()\n"
            "out = {'paged': collected, 'full': [e['sequence_no'] for e in full['events']]}\n"
        )
        self.assertEqual(out["paged"], out["full"])
        self.assertEqual(out["paged"], list(range(1, len(out["paged"]) + 1)))  # gapless from 1


# --- 9/10. long-poll (mid-poll delivery + timeout returns empty) ------------------------------

class LongPollDeliveryTest(_DrivenSubprocess):
    def test_long_poll_returns_when_events_arrive_mid_poll(self) -> None:
        # A fake clock/sleep drives the poll loop deterministically. The _sleep hook itself appends one
        # event on its 2nd tick (no cross-thread race): the poll's next read sees it and returns non-empty
        # before the wait cap. This proves the long-poll delivers mid-poll (not just on the first read).
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "install_factory(sup, scripted_provider([final_reply('done')]))\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'x'}})\n"
            "rid = start['agent_run_id']\n"
            "wait_terminal(sup, rid)\n"  # let the real turn finish so the ledger is stable
            "top = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})['cursor']\n"
            "# NOTE: the run is now terminal; force the events handler to long-poll by making _driven_terminal\n"
            "# report non-terminal until the event lands, via a poll_state flag the _sleep hook drives.\n"
            "fake = FakeClock()\n"
            "sup._clock = fake.clock\n"
            "ticks = {'n': 0}\n"
            "real_reduce = sup._safe_reduce\n"
            "def fake_reduce(arg):\n"
            "    st = real_reduce(arg)\n"
            "    if st is not None and not ticks.get('appended'):\n"
            "        st = dict(st); st['terminal'] = False; st['terminal_state'] = None\n"  # pretend live until the event lands
            "    return st\n"
            "sup._safe_reduce = fake_reduce\n"
            "def slp(dt):\n"
            "    fake.sleep(dt)\n"
            "    ticks['n'] += 1\n"
            "    if ticks['n'] == 2 and not ticks.get('appended'):\n"
            "        sup.funnel_event(rid, 'transport', 'point', summary='late event')\n"
            "        ticks['appended'] = True\n"
            "sup._sleep = slp\n"
            "ev = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': top, 'wait': 10.0}})\n"
            "sup._safe_reduce = real_reduce\n"
            "sup.shutdown()\n"
            "out = {'top': top, 'delivered': [[e['event_kind'], e['marker']] for e in ev['events']], 'cursor': ev['cursor'], 'ticks': ticks['n']}\n"
        )
        self.assertTrue(len(out["delivered"]) >= 1)
        self.assertIn(["transport", "point"], out["delivered"])
        self.assertEqual(out["cursor"], out["top"] + 1)
        self.assertEqual(out["ticks"], 2)  # delivered on the 2nd poll tick (mid-poll), not the first read

    def test_long_poll_terminal_run_returns_immediately(self) -> None:
        # A terminal run never waits: a long-poll on it (no new events) returns at once, terminal=True.
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "install_factory(sup, scripted_provider([final_reply('done')]))\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'x'}})\n"
            "rid = start['agent_run_id']\n"
            "state = wait_terminal(sup, rid)\n"
            "top = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})['cursor']\n"
            "# wait>0 but the run is terminal -> immediate empty tail, terminal True.\n"
            "ev = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': top, 'wait': 5.0}})\n"
            "sup.shutdown()\n"
            "out = {'events': ev['events'], 'terminal': ev['terminal'], 'terminal_state': ev.get('terminal_state')}\n"
        )
        self.assertEqual(out["events"], [])
        self.assertTrue(out["terminal"])
        self.assertEqual(out["terminal_state"], "success")


class LongPollTimeoutTest(_DrivenSubprocess):
    def test_long_poll_times_out_returns_empty(self) -> None:
        # A NON-terminal driven run with no new events: the fake clock exhausts the capped wait and the
        # poll returns empty with the unchanged cursor (never blocks the driver on real time).
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "import threading, time\n"
            "# A provider that parks forever on the first call -> the run stays non-terminal for the poll.\n"
            "hold = threading.Event()\n"
            "base = scripted_provider([final_reply('unreached')])\n"
            "def prov(messages, **opts):\n"
            "    hold.wait(20.0)\n"
            "    return base(messages, **opts)\n"
            "prov.state = base.state\n"
            "install_factory(sup, prov)\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'x'}})\n"
            "rid = start['agent_run_id']\n"
            "# Wait for the run's stream to have its session/turn open events (turn in flight).\n"
            "deadline = time.monotonic() + 5.0\n"
            "sess = None\n"
            "while time.monotonic() < deadline and sess is None:\n"
            "    sess = sup._get_driven(rid); time.sleep(0.01)\n"
            "while time.monotonic() < deadline and sess.adapter._active_turn is None:\n"
            "    time.sleep(0.01)\n"
            "top = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})['cursor']\n"
            "fake = FakeClock()\n"
            "sup._clock = fake.clock\n"
            "sup._sleep = fake.sleep\n"
            "ev = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': top, 'wait': 10.0}})\n"
            "elapsed = fake.t\n"
            "hold.set()\n"  # let the turn finish so shutdown joins cleanly
            "wait_terminal(sup, rid)\n"
            "sup.shutdown()\n"
            "out = {'events': ev['events'], 'cursor': ev['cursor'], 'top': top, 'terminal': ev['terminal'], 'elapsed': elapsed}\n"
        )
        self.assertEqual(out["events"], [])
        self.assertEqual(out["cursor"], out["top"])  # cursor unchanged on empty
        self.assertFalse(out["terminal"])
        # The poll respected the 25s server cap (min(wait=10, 25)=10), advanced by 0.5s sleeps.
        self.assertLessEqual(out["elapsed"], 25.0)
        self.assertGreaterEqual(out["elapsed"], 10.0)


# --- 11. engine gate: NO C1/T5 rows on a denied engine ----------------------------------------

class EngineGateTest(_DrivenSubprocess):
    def test_codex_claude_fail_closed_unknown_unknown_no_rows(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "from kaizen_components import db\n"
            "def counts():\n"
            "    s = db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0]\n"
            "    r = db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0]\n"
            "    return s, r\n"
            "before = counts()\n"
            "codex = sup._handle_control({'op': 'session/start', 'args': {'engine': 'codex', 'prompt': 'x'}})\n"
            "claude = sup._handle_control({'op': 'session/start', 'args': {'engine': 'claude', 'prompt': 'x'}})\n"
            "claude_cli = sup._handle_control({'op': 'session/start', 'args': {'engine': 'claude_cli', 'prompt': 'x'}})\n"
            "unknown = sup._handle_control({'op': 'session/start', 'args': {'engine': 'gpt-9000', 'prompt': 'x'}})\n"
            "after = counts()\n"
            "sup.shutdown()\n"
            "out = {'codex': codex['code'], 'claude': claude['code'], 'claude_cli': claude_cli['code'],\n"
            "       'unknown': unknown['code'], 'before': before, 'after': after}\n",
            env=_NO_VENDOR_BINARIES,
        )
        # Hermetic gate check (_NO_VENDOR_BINARIES): both vendor probes report unavailable, so the
        # capability gate returns the probe's exact structured code before any row write.
        self.assertEqual(out["codex"], "DENIED_ENGINE_UNAVAILABLE")
        self.assertEqual(out["claude"], "DENIED_SDK_UNAVAILABLE")
        self.assertEqual(out["claude_cli"], "DENIED_SDK_UNAVAILABLE")
        self.assertEqual(out["unknown"], "DENIED_UNKNOWN_ENGINE")
        # THE gate invariant: a denied engine writes NO C1/T5 rows (the gate precedes any write).
        self.assertEqual(out["before"], out["after"])


# --- 11b. deterministic arg validation: NO C1/T5 rows on a malformed arg -----------------------

class ArgValidationTest(_DrivenSubprocess):
    def test_invalid_args_denied_with_no_rows(self) -> None:
        # Each malformed arg (past a valid engine) is DENIED before any C1/T5 write, so the ledger stays
        # empty. Covers: non-string/blank prompt, non-string model, non-int/<1 max_turns, non-positive/
        # non-finite approval_timeout.
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "from kaizen_components import db\n"
            "def counts():\n"
            "    s = db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0]\n"
            "    r = db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0]\n"
            "    return s, r\n"
            "before = counts()\n"
            "def start(**extra):\n"
            "    a = {'engine': 'local_llm', 'prompt': 'go'}; a.update(extra)\n"
            "    return sup._handle_control({'op': 'session/start', 'args': a})\n"
            "blank_prompt = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': '   '}})\n"
            "nonstr_prompt = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 123}})\n"
            "bad_model = start(model=5)\n"
            "bad_mt_zero = start(max_turns=0)\n"
            "bad_mt_type = start(max_turns='two')\n"
            "bad_mt_bool = start(max_turns=True)\n"
            "bad_to_zero = start(approval_timeout=0)\n"
            "bad_to_neg = start(approval_timeout=-1.0)\n"
            "bad_to_type = start(approval_timeout='soon')\n"
            "bad_to_inf = start(approval_timeout=float('inf'))\n"
            "after = counts()\n"
            "sup.shutdown()\n"
            "out = {'blank_prompt': blank_prompt['code'], 'nonstr_prompt': nonstr_prompt['code'],\n"
            "       'bad_model': bad_model['code'], 'bad_mt_zero': bad_mt_zero['code'],\n"
            "       'bad_mt_type': bad_mt_type['code'], 'bad_mt_bool': bad_mt_bool['code'],\n"
            "       'bad_to_zero': bad_to_zero['code'], 'bad_to_neg': bad_to_neg['code'],\n"
            "       'bad_to_type': bad_to_type['code'], 'bad_to_inf': bad_to_inf['code'],\n"
            "       'before': before, 'after': after}\n"
        )
        # Blank/whitespace and non-string prompts are rejected as DENIED_PROMPT_INVALID.
        self.assertEqual(out["blank_prompt"], "DENIED_PROMPT_INVALID")
        self.assertEqual(out["nonstr_prompt"], "DENIED_PROMPT_INVALID")
        self.assertEqual(out["bad_model"], "DENIED_MODEL_INVALID")
        self.assertEqual(out["bad_mt_zero"], "DENIED_MAX_TURNS_INVALID")
        self.assertEqual(out["bad_mt_type"], "DENIED_MAX_TURNS_INVALID")
        self.assertEqual(out["bad_mt_bool"], "DENIED_MAX_TURNS_INVALID")
        self.assertEqual(out["bad_to_zero"], "DENIED_APPROVAL_TIMEOUT_INVALID")
        self.assertEqual(out["bad_to_neg"], "DENIED_APPROVAL_TIMEOUT_INVALID")
        self.assertEqual(out["bad_to_type"], "DENIED_APPROVAL_TIMEOUT_INVALID")
        self.assertEqual(out["bad_to_inf"], "DENIED_APPROVAL_TIMEOUT_INVALID")
        # No rows for any invalid input (validation precedes C1/T5).
        self.assertEqual(out["before"], out["after"])


# --- 11c. compensating finalization: a post-insert startup failure never dangles the run -------

class StartupFailureCompensationTest(_DrivenSubprocess):
    def test_start_session_failure_finalizes_failed_no_dangle(self) -> None:
        # Inject an _adapter_factory whose adapter.start_session raises AFTER the C1/T5 inserts. The
        # post-insert sequence is wrapped: the run is finalized 'failed' (not left dangling), no turn
        # thread is leaked, and the response is structured ERROR_SESSION_START. A subsequent session/events
        # on the run reports terminal.
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "import threading\n"
            "from kaizen_components.orchestration.adapters import local_llm as L\n"
            "class BoomAdapter:\n"
            "    def __init__(self):\n"
            "        self._tools = {}\n"
            "    def bind_session(self, sid): pass\n"
            "    def on_approval(self, cb): pass\n"
            "    def start_session(self, cwd=None):\n"
            "        raise RuntimeError('boom during start_session')\n"
            "    def kill(self): return {'status': 'OK', 'killed': True}\n"
            "    def deny_all_waiters(self): return 0\n"
            "sup._adapter_factory = lambda arid, rec, kw: BoomAdapter()\n"
            "threads_before = set(t.name for t in threading.enumerate())\n"
            "resp = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'go'}})\n"
            "rid = resp.get('agent_run_id')\n"
            "state = sup._safe_reduce(rid) if rid else None\n"
            "ev = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}}) if rid else None\n"
            "leaked = [t.name for t in threading.enumerate() if t.name.startswith('driven-turn-') and t.name not in threads_before]\n"
            "sess_live = sup._get_driven(rid) is not None if rid else None\n"
            "sup.shutdown()\n"
            "out = {'resp': resp, 'terminal': state['terminal'] if state else None,\n"
            "       'terminal_state': state['terminal_state'] if state else None,\n"
            "       'ev_terminal': ev['terminal'] if ev else None, 'leaked': leaked, 'sess_live': sess_live}\n"
        )
        self.assertEqual(out["resp"]["status"], "ERROR")
        self.assertEqual(out["resp"]["code"], "ERROR_SESSION_START")
        self.assertTrue(out["resp"]["agent_run_id"])  # the C1/T5 rows existed; the run id is returned
        self.assertTrue(out["terminal"])              # finalized, not dangling
        self.assertEqual(out["terminal_state"], "failure")
        self.assertFalse(out["sess_live"])            # deregistered
        self.assertEqual(out["leaked"], [])           # no turn thread leaked
        self.assertTrue(out["ev_terminal"])           # session/events on it reports terminal


class MutationInterceptionProofTest(_DrivenSubprocess):
    def test_production_missing_or_raising_guard_setter_denies(self) -> None:
        out = self.drive(
            "class MissingGuard:\n"
            "    def bind_session(self,_sid): pass\n"
            "    def on_approval(self,_cb): pass\n"
            "    def set_approval_broker(self,_cb): pass\n"
            "    def kill(self): return {'status':'OK','killed':True}\n"
            "class RaisingGuard(MissingGuard):\n"
            "    def set_mutation_guard(self,_cb): raise RuntimeError('no interception')\n"
            "sup=Supervisor(); sup.boot(); sup._build_driven_adapter=lambda *_a,**_k: MissingGuard()\n"
            "missing=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one'}})\n"
            "sup._build_driven_adapter=lambda *_a,**_k: RaisingGuard()\n"
            "raising=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'two'}})\n"
            "sup.shutdown(); out={'missing':missing,'raising':raising}\n"
        )
        self.assertEqual(out["missing"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertEqual(out["raising"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")


class ApprovalBrokerBindingProofTest(_DrivenSubprocess):
    def test_production_missing_or_raising_broker_setter_denies(self) -> None:
        out = self.drive(
            "class MissingBroker:\n"
            "    def bind_session(self,_sid): pass\n"
            "    def on_approval(self,_cb): pass\n"
            "    def set_mutation_guard(self,_cb): pass\n"
            "    def kill(self): return {'status':'OK','killed':True}\n"
            "class RaisingBroker(MissingBroker):\n"
            "    def set_approval_broker(self,_cb): raise RuntimeError('no broker')\n"
            "sup=Supervisor(); sup.boot(); sup._build_driven_adapter=lambda *_a,**_k: MissingBroker()\n"
            "missing=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one'}})\n"
            "sup._build_driven_adapter=lambda *_a,**_k: RaisingBroker()\n"
            "raising=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'two'}})\n"
            "sup.shutdown(); out={'missing':missing,'raising':raising}\n"
        )
        for result in (out["missing"], out["raising"]):
            self.assertEqual(result["status"], "DENIED")
            self.assertEqual(result["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
            self.assertIn("approval broker", result["required_action"])


# --- 12. bad loopback token refused -----------------------------------------------------------

class LoopbackTokenTest(_DrivenSubprocess):
    def test_session_start_over_loopback_bad_token_refused(self) -> None:
        # Drive the REAL loopback transport (not the in-process handler) with a WRONG token: the server
        # refuses DENIED_LOOPBACK_AUTH before the handler sees session/start.
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "from kaizen_components.orchestration import loopback\n"
            "bad = loopback.send_request(sup.repo_root, sup.runtime_dir,\n"
            "    {'op': 'session/start', 'token': 'WRONG', 'args': {'engine': 'local_llm', 'prompt': 'x'}}, timeout=3)\n"
            "good = loopback.send_request(sup.repo_root, sup.runtime_dir,\n"
            "    {'op': 'ping', 'token': sup.token, 'args': {}}, timeout=3)\n"
            "sup.shutdown()\n"
            "out = {'bad': bad, 'good': good}\n"
        )
        self.assertEqual(out["bad"]["code"], "DENIED_LOOPBACK_AUTH")
        self.assertEqual(out["good"]["status"], "OK")


# --- 13. shutdown with an open turn -----------------------------------------------------------

class ShutdownOpenTurnTest(_DrivenSubprocess):
    def test_shutdown_finalizes_an_open_driven_turn(self) -> None:
        # A turn parked on an approval; shutdown() denies the waiter, kills the adapter, joins, and
        # force-finalizes the non-terminal run. After shutdown the run is terminal (never left dangling).
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "prov = scripted_provider([tool_reply('echo', path='x'), final_reply('unreached')])\n"
            "install_factory(sup, prov, engine=make_engine(), tools=echo_tools())\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'go', 'approval_timeout': 30.0}})\n"
            "rid = start['agent_run_id']\n"
            "corr = wait_open_approval(sup, rid)\n"  # the ask surfaced
            "parked = wait_waiter_parked(sup, rid, corr)\n"  # the turn is parked on the ask
            "sup.shutdown()\n"  # teardown-with-open-turn
            "state = sup._safe_reduce(rid)\n"
            "out = {'corr_seen': bool(corr), 'parked': parked, 'terminal': state['terminal'], 'terminal_state': state['terminal_state']}\n"
        )
        self.assertTrue(out["corr_seen"])
        self.assertTrue(out["parked"])
        self.assertTrue(out["terminal"])
        self.assertEqual(out["terminal_state"], "failure")  # canceled by the shutdown force-finalize


# --- 14. double-approve -> ALREADY_DECIDED ----------------------------------------------------

class DoubleApproveTest(_DrivenSubprocess):
    def test_second_approve_is_already_decided(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "prov = scripted_provider([tool_reply('echo', path='x'), final_reply('done')])\n"
            "install_factory(sup, prov, engine=make_engine(), tools=echo_tools())\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'go'}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "corr = wait_open_approval(sup, rid)\n"
            "first = sup._handle_control({'op': 'approve', 'args': {'correlation_id': corr, 'session_id': sid, 'decision': 'approve'}})\n"
            "wait_terminal(sup, rid)\n"
            "second = sup._handle_control({'op': 'approve', 'args': {'correlation_id': corr, 'session_id': sid, 'decision': 'approve'}})\n"
            "sup.shutdown()\n"
            "out = {'first': first['status'], 'second': second}\n"
        )
        self.assertEqual(out["first"], "OK")
        self.assertEqual(out["second"]["code"], "DENIED_APPROVAL_ALREADY_DECIDED")


class ApprovalRestartOrphanTest(_DrivenSubprocess):
    def test_boot_reconciles_committed_open_without_live_waiter_once(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "sup=Supervisor(); sup.boot(); install_factory(sup, scripted_provider([final_reply('idle')]))\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'go'}}); wait_idle(sup,start['agent_run_id'])\n"
            "opened=sup._approval_broker.open(session_id=start['session_id'],agent_run_id=start['agent_run_id'],correlation_id='restart-orphan',is_test=True)\n"
            "sup._loopback.stop(); sup._loopback=None; sup._release_single_instance()\n"
            "sweeper=Supervisor(); boot=sweeper.boot()\n"
            "row=db.fetch_one('SELECT state FROM approval_requests WHERE id=?',(opened['id'],))\n"
            "events=db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id=? AND source_event_id=?\",(start['agent_run_id'],'approval:'+opened['id']+':terminal'))[0]\n"
            "again=sweeper._approval_broker.reconcile_orphans(set())\n"
            "events_again=db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id=? AND source_event_id=?\",(start['agent_run_id'],'approval:'+opened['id']+':terminal'))[0]\n"
            "sweeper.shutdown(); out={'boot':boot['approvals_reconciled'],'state':row[0],'events':[events,events_again],'managed_again':again['managed']}\n"
        )
        self.assertTrue(out["boot"])
        self.assertEqual(out["state"], "denied")
        self.assertEqual(out["events"], [1, 1])
        self.assertEqual(out["managed_again"], 0)


# --- canonical gateway on the real (no-factory) driven lane ------------------------------------

class DrivenDefaultToolsTest(_DrivenSubprocess):
    def test_no_factory_driven_adapter_registers_default_tools(self) -> None:
        # The real driven lane advertises the exact provider-neutral five-tool surface and carries no
        # legacy ToolSpec runners (which would duplicate the gateway policy/broker path).
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "adapter = sup._build_driven_adapter('ar_probe', {})\n"
            "sup.shutdown()\n"
            "out = {'gateway_mode': adapter._gateway_mode, 'tools': list(L.CANONICAL_TOOL_NAMES),\n"
            "       'legacy_tools': sorted(adapter._tools)}\n"
        )
        self.assertTrue(out["gateway_mode"])
        self.assertEqual(out["tools"], [
            "kaizen_read_file", "kaizen_list_files", "kaizen_search_text",
            "kaizen_run_process", "kaizen_propose_changes",
        ])
        self.assertEqual(out["legacy_tools"], [])


class DrivenCanonicalGatewayTest(_DrivenSubprocess):
    def test_plan_process_denial_is_durable_and_zero_approval_on_normal_local_lane(self) -> None:
        out = self.drive(
            "from pathlib import Path\n"
            "from kaizen_components import db\n"
            "prov = scripted_provider([tool_reply('kaizen_run_process', executable=sys.executable, "
            "argv=['-c', \"print('MUST_NOT_RUN')\"], cwd='.', timeout_ms=5000), "
            "final_reply('structured denial observed')])\n"
            "L.default_chat_provider = lambda: prov\n"
            "sup = Supervisor(); sup.boot()\n"
            "start = sup._handle_control({'op':'session/start','args':{'engine':'local_llm',"
            "'prompt':'attempt the bounded control process','profile':{'model':'fixture',"
            "'permission_mode':'plan'}}})\n"
            "rid = start['agent_run_id']; sid = start['session_id']; wait_idle(sup, rid)\n"
            "events = db.fetch_all(\"SELECT marker, correlation_id, code, body FROM agent_events "
            "WHERE agent_run_id = ? AND event_kind = 'tool_call' ORDER BY sequence_no\", (rid,))\n"
            "approvals = db.fetch_one('SELECT COUNT(*) FROM approval_requests WHERE session_id = ?', "
            "(sid,))[0]\n"
            "runtime_created = (Path(sup.repo_root) / 'AI' / 'work' / 'orchestration' / "
            "'tool-runtime').exists()\n"
            "sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}}); sup.shutdown()\n"
            "out = {'start':start,'events':[[r[0],r[1],r[2],json.loads(r[3])] for r in events],"
            "'approvals':approvals,'runtime_created':runtime_created}\n"
        )
        self.assertEqual(out["start"]["status"], "OK", out)
        self.assertEqual([event[0] for event in out["events"]], ["open", "close_fail"])
        self.assertEqual(out["events"][0][1], out["events"][1][1])
        self.assertEqual(out["events"][0][3]["name"], "kaizen_run_process")
        self.assertEqual(out["events"][0][3]["argv"], ["-c", "print('MUST_NOT_RUN')"])
        self.assertEqual(out["events"][0][3]["cwd"], ".")
        self.assertEqual(out["events"][0][3]["timeout_ms"], 5000)
        self.assertEqual(out["events"][1][2], "MODE_CEILING:exec")
        self.assertEqual(out["events"][1][3]["name"], "kaizen_run_process")
        self.assertEqual(out["approvals"], 0)
        self.assertFalse(out["runtime_created"])


# --- H2.1 capabilities + profile ---------------------------------------------------------------

# A deterministically-unreachable Ollama endpoint so the capabilities model probe DEGRADES in the
# scratch subprocess regardless of whether the dev box has a live Ollama on the default port.
_DEAD_OLLAMA = {"KAIZEN_LLM_BASE_URL": "http://127.0.0.1:1/v1"}

# Hermetic vendor-gate env: a PATH with no codex/claude so installed-binary probes fail closed
# deterministically (unavailable -> DENIED_ENGINE_UNAVAILABLE) on every machine, capability calls never
# spawn a real vendor binary, and an offline session/start can never launch a billed vendor child.
# Installed-binary truth lives in test_codex_live.py / test_claude_live.py.
_NO_VENDOR_BINARIES = {"PATH": str(REPO_ROOT)}


class CapabilitiesShapeTest(_DrivenSubprocess):
    """Capability descriptors, profile support, and feature-shape contracts exposed by the driven supervisor."""
    def test_capabilities_reports_three_wire_engines(self) -> None:
        # Three engines in canonical wire order; no claude_cli on the wire; local_llm is drivable; installed
        # vendor lanes remain fail-closed; only modes proven by each adapter are advertised.
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "caps = sup._handle_control({'op': 'session/capabilities', 'args': {}})\n"
            "sup.shutdown()\n"
            "eng = {e['id']: e for e in caps['engines']}\n"
            "out = {'status': caps['status'], 'ids': [e['id'] for e in caps['engines']],\n"
            "       'local_drivable': eng['local_llm']['drivable'],\n"
            "       'codex': [eng['codex']['drivable'], eng['codex']['availability']['state'], eng['codex']['availability']['code'], eng['codex']['auth_modes']],\n"
            "       'claude': [eng['claude']['drivable'], eng['claude']['availability']['state']],\n"
            "       'perm_modes': eng['local_llm']['permission_modes'],\n"
            "       'features': [e['features'] for e in caps['engines']]}\n",
            env={**_DEAD_OLLAMA, **_NO_VENDOR_BINARIES},
        )
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["ids"], ["local_llm", "codex", "claude"])  # wire ids only, no claude_cli
        self.assertTrue(out["local_drivable"])
        # Hermetic shape check (_NO_VENDOR_BINARIES): both vendor lanes report the missing-binary
        # fail-closed shape. Installed-binary behavior is proven in test_codex_live/test_claude_live.
        self.assertEqual(out["codex"][0], False)
        self.assertEqual(out["codex"][1], "unavailable")
        self.assertEqual(out["codex"][2], "DENIED_ENGINE_UNAVAILABLE")
        self.assertEqual(out["codex"][3], ["subscription", "api-key"])
        self.assertEqual(out["claude"], [False, "unavailable"])
        self.assertEqual(out["perm_modes"], ["plan", "ask", "agent", "full"])
        expected_features = {
            "streaming": False,
            "image_attachments": False,
            "governed_context": False,
            "diff_snapshots": False,
            "writer_leasing": True,
            "subscription_auth": False,
            "controlled_tools": False,
            "process_execution": False,
            "test_extension": False,
        }
        local_features = {
            **expected_features,
            "streaming": True,
            "governed_context": True,
            "diff_snapshots": True,
            "controlled_tools": True,
            "process_execution": True,
            "test_extension": True,
        }
        claude_features = {**expected_features, "test_extension": True}
        self.assertEqual(out["features"], [local_features, expected_features, claude_features])


class CapabilitiesDegradedTest(_DrivenSubprocess):
    def test_probe_failure_degrades_but_stays_drivable(self) -> None:
        # No reachable Ollama -> local_llm state 'degraded', models [], a warning, but drivable STAYS true
        # (a model can still be validated at start via the editable field).
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "caps = sup._handle_control({'op': 'session/capabilities', 'args': {'refresh': True}})\n"
            "sup.shutdown()\n"
            "loc = [e for e in caps['engines'] if e['id'] == 'local_llm'][0]\n"
            "out = {'state': loc['availability']['state'], 'drivable': loc['drivable'],\n"
            "       'models': loc['models'], 'warnings': len(loc['warnings'])}\n",
            env=_DEAD_OLLAMA,
        )
        self.assertEqual(out["state"], "degraded")
        self.assertTrue(out["drivable"])
        self.assertEqual(out["models"], [])
        self.assertGreaterEqual(out["warnings"], 1)


class ProfileStartTest(_DrivenSubprocess):
    """Session-start profile validation, capability gates, model conflicts, aliases, and snapshot hashing."""
    def test_explicit_profile_persists_fields_and_emits_profile_first(self) -> None:
        # An explicit profile (model + permission_mode ask) persists to C1 + T5, the response carries the
        # effective profile + profile_hash, and profile/point is the FIRST stream event with a valid
        # JSON body carrying the profile_hash.
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "install_factory(sup, scripted_provider([final_reply('done')]))\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'hi',\n"
            "    'profile': {'model': 'qwen-test', 'permission_mode': 'ask'}}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "wait_terminal(sup, rid)\n"
            "from kaizen_components import db, session_records\n"
            "srow = db.fetch_one('SELECT permission_mode, requested_model, profile_hash, auth_mode FROM agent_sessions WHERE id = ?', (sid,))\n"
            "rrow = db.fetch_one('SELECT session_id, engine, auth_mode, permission_mode, requested_model, profile_hash, model FROM agent_runs WHERE id = ?', (rid,))\n"
            "ev = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})\n"
            "sup.shutdown()\n"
            "first = ev['events'][0]\n"
            "import json as _j\n"
            "body = _j.loads(first['body'])\n"
            "out = {'start': start, 'srow': list(srow), 'rrow': list(rrow),\n"
            "       'first_kind': [first['event_kind'], first['marker']], 'body_hash': body.get('profile_hash'),\n"
            "       'body_eff': body.get('effective'), 'body_ver': body.get('permission_mode_version')}\n",
            env=_DEAD_OLLAMA,
        )
        self.assertEqual(out["start"]["status"], "OK")
        self.assertEqual(out["start"]["engine"], "local_llm")
        self.assertEqual(out["start"]["profile"]["permission_mode"], "ask")
        self.assertEqual(out["start"]["profile"]["model"], "qwen-test")
        self.assertTrue(out["start"]["profile_hash"])
        # C1 row: permission_mode, requested_model, profile_hash, auth_mode.
        self.assertEqual(out["srow"], ["ask", "qwen-test", out["start"]["profile_hash"], "none"])
        # T5 row: session_id link, wire engine, auth_mode, permission_mode, requested_model, profile_hash, effective model.
        self.assertEqual(out["rrow"][0], out["start"]["session_id"])
        self.assertEqual(out["rrow"][1], "local_llm")
        self.assertEqual(out["rrow"][2], "none")
        self.assertEqual(out["rrow"][3], "ask")
        self.assertEqual(out["rrow"][4], "qwen-test")
        self.assertEqual(out["rrow"][5], out["start"]["profile_hash"])
        self.assertEqual(out["rrow"][6], "qwen-test")
        # profile/point is the FIRST stream event, with a JSON body carrying the profile_hash + version.
        self.assertEqual(out["first_kind"], ["profile", "point"])
        self.assertEqual(out["body_hash"], out["start"]["profile_hash"])
        self.assertEqual(out["body_eff"]["permission_mode"], "ask")
        self.assertEqual(out["body_ver"], 1)


class FullOptInTest(_DrivenSubprocess):
    def test_full_without_opt_in_denied_zero_rows(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "from kaizen_components import db\n"
            "def counts():\n"
            "    return (db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0], db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0])\n"
            "before = counts()\n"
            "resp = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'x',\n"
            "    'profile': {'permission_mode': 'full'}}})\n"
            "after = counts()\n"
            "sup.shutdown()\n"
            "out = {'code': resp['code'], 'before': before, 'after': after}\n",
            env=_DEAD_OLLAMA,
        )
        self.assertEqual(out["code"], "DENIED_FULL_CONFIRMATION_REQUIRED")
        self.assertEqual(out["before"], out["after"])  # zero rows on denial


class ProfileUnsupportedTest(_DrivenSubprocess):
    def test_reasoning_effort_on_local_llm_denied_zero_rows(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "from kaizen_components import db\n"
            "def counts():\n"
            "    return (db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0], db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0])\n"
            "before = counts()\n"
            "resp = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'x',\n"
            "    'profile': {'reasoning_effort': 'high'}}})\n"
            "after = counts()\n"
            "sup.shutdown()\n"
            "out = {'code': resp['code'], 'before': before, 'after': after}\n",
            env=_DEAD_OLLAMA,
        )
        self.assertEqual(out["code"], "DENIED_PROFILE_UNSUPPORTED")
        self.assertEqual(out["before"], out["after"])


class ProfileUnknownFieldTest(_DrivenSubprocess):
    def test_unknown_profile_field_denied_zero_rows(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "from kaizen_components import db\n"
            "def counts():\n"
            "    return (db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0], db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0])\n"
            "before = counts()\n"
            "resp = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'x',\n"
            "    'profile': {'temperature': 0.5}}})\n"
            "after = counts()\n"
            "sup.shutdown()\n"
            "out = {'code': resp['code'], 'unknown': resp.get('unknown'), 'before': before, 'after': after}\n",
            env=_DEAD_OLLAMA,
        )
        self.assertEqual(out["code"], "DENIED_PROFILE_FIELD_UNKNOWN")
        self.assertEqual(out["unknown"], ["temperature"])
        self.assertEqual(out["before"], out["after"])


class ModelConflictTest(_DrivenSubprocess):
    def test_legacy_model_vs_profile_model_conflict_denied(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "resp = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'x',\n"
            "    'model': 'legacy-m', 'profile': {'model': 'profile-m'}}})\n"
            "# Matching legacy + profile model is NOT a conflict (same value).\n"
            "install_factory(sup, scripted_provider([final_reply('done')]))\n"
            "same = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'x',\n"
            "    'model': 'same-m', 'profile': {'model': 'same-m'}}})\n"
            "wait_terminal(sup, same['agent_run_id'])\n"
            "sup.shutdown()\n"
            "out = {'code': resp['code'], 'same_status': same['status'], 'same_model': same['profile']['model']}\n",
            env=_DEAD_OLLAMA,
        )
        self.assertEqual(out["code"], "DENIED_MODEL_CONFLICT")
        self.assertEqual(out["same_status"], "OK")
        self.assertEqual(out["same_model"], "same-m")


class EngineAliasTest(_DrivenSubprocess):
    def test_claude_cli_alias_normalizes_on_the_wire(self) -> None:
        # claude_cli is accepted as an INPUT alias (normalized to claude). The alias never appears as a
        # duplicate wire engine. Hermetic (_NO_VENDOR_BINARIES): the probe reports unavailable, so the
        # denial code is the exact missing-binary gate code and no vendor child can spawn.
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "resp = sup._handle_control({'op': 'session/start', 'args': {'engine': 'claude_cli', 'prompt': 'x'}})\n"
            "caps = sup._handle_control({'op': 'session/capabilities', 'args': {}})\n"
            "sup.shutdown()\n"
            "out = {'status': resp['status'], 'code': resp.get('code'), 'engine': resp.get('engine'), 'ids': [e['id'] for e in caps['engines']]}\n",
            env={**_DEAD_OLLAMA, **_NO_VENDOR_BINARIES},
        )
        self.assertEqual(out["status"], "DENIED")
        self.assertEqual(out["code"], "DENIED_SDK_UNAVAILABLE")
        self.assertEqual(out["engine"], "claude")  # normalized on the wire, never claude_cli
        self.assertNotIn("claude_cli", out["ids"])


class ProfileHashDiffersTest(_DrivenSubprocess):
    def test_profile_hash_differs_plan_vs_ask(self) -> None:
        # Two starts differing only in permission_mode (plan vs ask) produce distinct snapshot inputs, so
        # distinct profile_hashes (plan seeds designated roots + a different mode).
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "install_factory(sup, scripted_provider([final_reply('a'), final_reply('b')]))\n"
            "plan = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'x',\n"
            "    'profile': {'permission_mode': 'plan'}}})\n"
            "wait_terminal(sup, plan['agent_run_id'])\n"
            "ask = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': 'x',\n"
            "    'profile': {'permission_mode': 'ask'}}})\n"
            "wait_terminal(sup, ask['agent_run_id'])\n"
            "sup.shutdown()\n"
            "out = {'plan': plan['profile_hash'], 'ask': ask['profile_hash']}\n",
            env=_DEAD_OLLAMA,
        )
        self.assertTrue(out["plan"])
        self.assertTrue(out["ask"])
        self.assertNotEqual(out["plan"], out["ask"])


# --- P0 workspace-writer lease ---------------------------------------------------------------

class WriterLeaseConcurrencyTest(_DrivenSubprocess):
    """Concurrent writer-claim isolation and non-borrowing across cancellation and replacement turns."""
    def test_same_session_request_cannot_borrow_canceling_turn_claim(self) -> None:
        out = self.drive(
            "import threading\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration import supervisor as S\n"
            "sup=Supervisor(); sup.boot(); install_factory(sup,scripted_provider([final_reply('done')]))\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','profile':{'permission_mode':'ask'}}}); rid=start['agent_run_id']; wait_idle(sup,rid)\n"
            "sess=sup._get_driven(rid); prior_result=sess.result.as_dict(); before_events=db.fetch_one('SELECT COUNT(*) FROM agent_events WHERE agent_run_id=?',(rid,))[0]\n"
            "acquired,verified=[],[]; original_acquire=sup._acquire_writer_claim; original_verify=sup._writer_lease.verify\n"
            "def tracked_acquire(mode,**kwargs):\n"
            "    claim,denial=original_acquire(mode,**kwargs)\n"
            "    if claim is not None: acquired.append(claim.claim_id)\n"
            "    return claim,denial\n"
            "def tracked_verify(token,**kwargs): verified.append(token); return original_verify(token,**kwargs)\n"
            "sup._acquire_writer_claim=tracked_acquire; sup._writer_lease.verify=tracked_verify\n"
            "entered,release=threading.Event(),threading.Event(); original=sup._materialize_request_artifacts; result={}\n"
            "def blocked(**_kwargs):\n"
            "    entered.set(); release.wait(5); return None,{'status':'DENIED','code':'DENIED_CONTEXT_INVALID','required_action':'retry'}\n"
            "sup._materialize_request_artifacts=blocked\n"
            "def request_a(): result['a']=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'a'}})\n"
            "thread=threading.Thread(target=request_a); thread.start(); barrier=entered.wait(3)\n"
            "b=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'b'}})\n"
            "release.set(); thread.join(5); sup._materialize_request_artifacts=original\n"
            "after_events=db.fetch_one('SELECT COUNT(*) FROM agent_events WHERE agent_run_id=?',(rid,))[0]; restored=sess.result.as_dict(); restored_state=sess.turn_state; restored_done=sess.turn_done.is_set(); marker_after='__workspace_writer__' in json.loads(S.OWNERSHIP_FILE.read_text(encoding='utf-8'))\n"
            "tokens=[]; original_run=sess.adapter.run_turn\n"
            "def checked(prompt): tokens.append(sess.writer_claim_token); return original_run(prompt)\n"
            "sess.adapter.run_turn=checked; c=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'c'}}); wait_idle(sup,rid); sup.shutdown()\n"
            "out={'barrier':barrier,'a':result.get('a'),'b':b,'state_after':restored_state,'result_restored':prior_result==restored,'turn_done':restored_done,'events_equal':before_events==after_events,'marker_after':marker_after,'c':c,'tokens':tokens,'acquired':acquired,'verified':verified}\n"
        )
        self.assertTrue(out["barrier"])
        self.assertEqual(out["a"]["code"], "DENIED_CONTEXT_INVALID")
        self.assertEqual(out["b"]["code"], "DENIED_TURN_IN_PROGRESS")
        self.assertEqual(out["state_after"], "idle")
        self.assertTrue(out["result_restored"])
        self.assertTrue(out["turn_done"])
        self.assertTrue(out["events_equal"])
        self.assertFalse(out["marker_after"])
        self.assertEqual(out["c"]["status"], "OK")
        self.assertEqual(len(out["tokens"]), 1)
        self.assertEqual(len(out["acquired"]), 2)
        self.assertNotEqual(out["acquired"][0], out["acquired"][1])
        self.assertEqual(out["verified"], out["acquired"])
        self.assertEqual(out["tokens"], [out["acquired"][1]])

    def test_source_lease_remains_held_while_approval_is_parked(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration import supervisor as S\n"
            "sup=Supervisor(); sup.boot()\n"
            "install_factory(sup,scripted_provider([tool_reply('echo',path='x'),final_reply('done')]),engine=make_engine(),tools=echo_tools())\n"
            "first=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','approval_timeout':30.0,'profile':{'permission_mode':'ask'}}}); rid,sid=first['agent_run_id'],first['session_id']\n"
            "corr=wait_open_approval(sup,rid); parked=wait_waiter_parked(sup,rid,corr)\n"
            "def counts(): return [db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0],db.fetch_one('SELECT COUNT(*) FROM agent_events')[0],db.fetch_one('SELECT COUNT(*) FROM approval_requests')[0]]\n"
            "before=counts(); c4_before=db.fetch_one('SELECT state FROM approval_requests WHERE session_id=? AND correlation_id=?',(sid,corr))[0]\n"
            "marker_before=json.loads(S.OWNERSHIP_FILE.read_text(encoding='utf-8'))['__workspace_writer__']; token_before=sup._get_driven(rid).writer_claim_token\n"
            "second_args={'engine':'local_llm','prompt':'two\\n exact  spacing','profile':{'permission_mode':'agent'}}; envelope_before=json.dumps(second_args,sort_keys=True)\n"
            "second=sup._handle_control({'op':'session/start','args':second_args}); after=counts(); envelope_after=json.dumps(second_args,sort_keys=True)\n"
            "c4_after=db.fetch_one('SELECT state FROM approval_requests WHERE session_id=? AND correlation_id=?',(sid,corr))[0]\n"
            "marker_after=json.loads(S.OWNERSHIP_FILE.read_text(encoding='utf-8'))['__workspace_writer__']; token_after=sup._get_driven(rid).writer_claim_token\n"
            "approved=sup._handle_control({'op':'approve','args':{'session_id':sid,'agent_run_id':rid,'correlation_id':corr,'decision':'approve'}}); wait_idle(sup,rid)\n"
            "registry_idle=json.loads(S.OWNERSHIP_FILE.read_text(encoding='utf-8')); token_idle=sup._get_driven(rid).writer_claim_token\n"
            "third=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'three','profile':{'permission_mode':'ask'}}}); wait_idle(sup,third['agent_run_id']); sup.shutdown()\n"
            "out={'first':first['status'],'parked':parked,'corr':corr,'before':before,'after':after,'second':second,'envelope_same':envelope_before==envelope_after,'c4':[c4_before,c4_after],'marker_same':marker_before==marker_after,'marker':marker_before,'tokens':[token_before,token_after,token_idle],'approved':approved,'marker_idle':'__workspace_writer__' in registry_idle,'third':third['status']}\n"
        )
        self.assertEqual(out["first"], "OK")
        self.assertTrue(out["corr"])
        self.assertTrue(out["parked"])
        self.assertEqual(out["second"]["code"], "DENIED_WORKSPACE_WRITER_BUSY")
        self.assertEqual(
            set(out["second"]["holder"]),
            {"session_id", "agent_run_id", "permission_mode", "acquired_at"},
        )
        self.assertEqual(out["second"]["holder"]["session_id"], out["marker"]["session_id"])
        self.assertEqual(out["second"]["holder"]["agent_run_id"], out["marker"]["agent_run_id"])
        self.assertEqual(out["second"]["holder"]["permission_mode"], "ask")
        self.assertEqual(out["before"], out["after"])
        self.assertTrue(out["envelope_same"])
        self.assertEqual(out["c4"], ["open", "open"])
        self.assertTrue(out["marker_same"])
        self.assertEqual(out["tokens"][0], out["marker"]["claim_id"])
        self.assertEqual(out["tokens"][1], out["tokens"][0])
        self.assertIsNone(out["tokens"][2])
        self.assertEqual(out["approved"]["state"], "approved")
        self.assertTrue(out["approved"]["waiter_released"])
        self.assertFalse(out["marker_idle"])
        self.assertEqual(out["third"], "OK")

    def test_start_release_failure_is_recovery_denial_with_zero_records(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "sup=Supervisor(); sup.boot()\n"
            "def counts(): return [db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0],db.fetch_one('SELECT COUNT(*) FROM agent_events')[0]]\n"
            "before=counts(); sup._materialize_request_artifacts=lambda **_kw:(None,{'status':'DENIED','code':'DENIED_CONTEXT_INVALID','required_action':'retry'}); sup._writer_lease.release=lambda *_a,**_kw:False\n"
            "result=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'x','profile':{'permission_mode':'ask'}}}); after=counts()\n"
            "out={'result':result,'counts':[before,after],'recovery':sup._writer_lease.recovery_required}\n"
        )
        self.assertEqual(out["result"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertEqual(out["counts"][0], out["counts"][1])
        self.assertTrue(out["recovery"])

    def test_live_prelaunch_release_failure_terminalizes_and_retains_recovery(self) -> None:
        out = self.drive(
            "sup=Supervisor(); sup.boot(); install_factory(sup,scripted_provider([final_reply('done')]))\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','profile':{'permission_mode':'ask'}}}); rid=start['agent_run_id']; wait_idle(sup,rid)\n"
            "sup._materialize_request_artifacts=lambda **_kw:(None,{'status':'DENIED','code':'DENIED_CONTEXT_INVALID','required_action':'retry'}); sup._writer_lease.release=lambda *_a,**_kw:False\n"
            "result=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'two'}}); reduced=sup._safe_reduce(rid)\n"
            "out={'result':result,'recovery':sup._writer_lease.recovery_required,'live':sup._get_driven(rid) is not None,'terminal':reduced['terminal']}\n"
        )
        self.assertEqual(out["result"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertTrue(out["recovery"])
        self.assertFalse(out["live"])
        self.assertTrue(out["terminal"])

    def test_resume_release_failure_is_recovery_denial_without_new_leg(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "sup=Supervisor(); sup.boot(); install_factory(sup,scripted_provider([final_reply('done')]))\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','profile':{'permission_mode':'ask'}}}); rid=start['agent_run_id']; wait_idle(sup,rid); sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}})\n"
            "before=db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0]; sup._materialize_request_artifacts=lambda **_kw:(None,{'status':'DENIED','code':'DENIED_CONTEXT_INVALID','required_action':'retry'}); sup._writer_lease.release=lambda *_a,**_kw:False\n"
            "result=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'resume'}}); after=db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0]\n"
            "out={'result':result,'runs':[before,after],'recovery':sup._writer_lease.recovery_required}\n"
        )
        self.assertEqual(out["result"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertEqual(out["runs"][0], out["runs"][1])
        self.assertTrue(out["recovery"])

    def test_start_context_rollback_uncertainty_is_recovery_with_zero_records(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.session_artifacts import SessionArtifactError\n"
            "sup=Supervisor(); sup.boot()\n"
            "def uncertain(**_kw): raise SessionArtifactError('DENIED_CONTEXT_INVALID','context_refs','retry',rollback_unproven=True)\n"
            "sup._session_artifacts.materialize=uncertain; before=[db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0],db.fetch_one('SELECT COUNT(*) FROM agent_events')[0]]\n"
            "result=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'x','profile':{'permission_mode':'ask'}}}); after=[db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0],db.fetch_one('SELECT COUNT(*) FROM agent_events')[0]]\n"
            "out={'result':result,'counts':[before,after],'recovery':sup._writer_lease.recovery_required}\n"
        )
        self.assertEqual(out["result"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertEqual(out["counts"][0], out["counts"][1])
        self.assertTrue(out["recovery"])

    def test_live_context_rollback_uncertainty_terminalizes_without_provider_call(self) -> None:
        out = self.drive(
            "from kaizen_components.orchestration.session_artifacts import SessionArtifactError\n"
            "provider=scripted_provider([final_reply('done')]); sup=Supervisor(); sup.boot(); install_factory(sup,provider)\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','profile':{'permission_mode':'ask'}}}); rid=start['agent_run_id']; wait_idle(sup,rid)\n"
            "def uncertain(**_kw): raise SessionArtifactError('DENIED_CONTEXT_INVALID','context_refs','retry',rollback_unproven=True)\n"
            "sup._session_artifacts.materialize=uncertain; before=len(provider.state['calls']); result=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'two'}}); reduced=sup._safe_reduce(rid)\n"
            "out={'result':result,'calls':[before,len(provider.state['calls'])],'terminal':reduced['terminal'],'recovery':sup._writer_lease.recovery_required}\n"
        )
        self.assertEqual(out["result"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertEqual(out["calls"][0], out["calls"][1])
        self.assertTrue(out["terminal"])
        self.assertTrue(out["recovery"])

    def test_resume_context_rollback_uncertainty_creates_no_new_leg_or_provider_call(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.session_artifacts import SessionArtifactError\n"
            "provider=scripted_provider([final_reply('done')]); sup=Supervisor(); sup.boot(); install_factory(sup,provider)\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','profile':{'permission_mode':'ask'}}}); rid,sid=start['agent_run_id'],start['session_id']; wait_idle(sup,rid); sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}})\n"
            "def uncertain(**_kw): raise SessionArtifactError('DENIED_CONTEXT_INVALID','context_refs','retry',rollback_unproven=True)\n"
            "sup._session_artifacts.materialize=uncertain; before=db.fetch_one('SELECT COUNT(*) FROM agent_runs WHERE session_id=?',(sid,))[0]; calls=len(provider.state['calls']); result=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'resume'}}); after=db.fetch_one('SELECT COUNT(*) FROM agent_runs WHERE session_id=?',(sid,))[0]\n"
            "out={'result':result,'runs':[before,after],'calls':[calls,len(provider.state['calls'])],'recovery':sup._writer_lease.recovery_required}\n"
        )
        self.assertEqual(out["result"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertEqual(out["runs"][0], out["runs"][1])
        self.assertEqual(out["calls"][0], out["calls"][1])
        self.assertTrue(out["recovery"])

    def test_instant_fatal_resume_leaves_terminal_leg_and_failed_conversation(self) -> None:
        out = self.drive(
            "import threading\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.adapters import TurnResult\n"
            "created=[]\n"
            "class InstantAdapter:\n"
            "    def __init__(self,fatal): self.fatal=fatal\n"
            "    def bind_session(self,_sid): pass\n"
            "    def on_approval(self,_callback): pass\n"
            "    def start_session(self,**_kwargs): return {'status':'OK'}\n"
            "    def run_turn(self,_prompt):\n"
            "        return TurnResult(status='FAILED',error_code='FATAL_RESUME',fatal=True) if self.fatal else TurnResult(status='OK',final_text='done')\n"
            "    def close(self): return {'status':'OK','closed':True}\n"
            "    def kill(self): return {'status':'OK','killed':True,'termination_proven':True}\n"
            "def factory(_rid,_recorder,_kwargs):\n"
            "    adapter=InstantAdapter(bool(created)); created.append(adapter); return adapter\n"
            "sup=Supervisor(); sup.boot(); sup._adapter_factory=factory\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','profile':{'permission_mode':'ask'}}}); rid,sid=start['agent_run_id'],start['session_id']; wait_idle(sup,rid); sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}})\n"
            "real_start=threading.Thread.start\n"
            "def synchronous_start(thread): real_start(thread); thread.join(5)\n"
            "threading.Thread.start=synchronous_start\n"
            "try:\n"
            "    result=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'resume'}})\n"
            "finally:\n"
            "    threading.Thread.start=real_start\n"
            "new_rid=result['agent_run_id']; reduced=sup._safe_reduce(new_rid); c1=db.fetch_one('SELECT state FROM agent_sessions WHERE id=?',(sid,))[0]; final=db.fetch_one(\"SELECT body FROM agent_events WHERE agent_run_id=? AND event_kind='finalization' ORDER BY sequence_no DESC LIMIT 1\",(new_rid,)); live=sup._get_driven(new_rid) is not None; recovery=sup._writer_lease.recovery_required; writer_active=sup._writer_lease.active; sup.shutdown()\n"
            "out={'start':start,'result':result,'old':rid,'new':new_rid,'terminal':reduced['terminal'],'terminal_state':reduced['terminal_state'],'c1':c1,'live':live,'final_body':final[0] if final else None,'recovery':recovery,'writer_active':writer_active}\n"
        )
        self.assertEqual(out["start"]["status"], "OK")
        self.assertEqual(out["result"]["status"], "ERROR")
        self.assertEqual(out["result"]["code"], "ERROR_SESSION_RESUME")
        self.assertEqual(out["result"]["agent_run_id"], out["new"])
        self.assertNotEqual(out["new"], out["old"])
        self.assertEqual(out["result"]["resumed_from"], out["old"])
        self.assertEqual(out["result"]["turn_state"], "terminal")
        self.assertTrue(out["terminal"])
        self.assertEqual(out["terminal_state"], "failure")
        self.assertEqual(out["final_body"], "FATAL_RESUME")
        self.assertFalse(out["live"])
        self.assertEqual(out["c1"], "failed")
        self.assertFalse(out["recovery"])
        self.assertFalse(out["writer_active"])

    def test_concurrent_plan_and_source_resume_serialize_to_one_new_leg_each(self) -> None:
        out = self.drive(
            "import threading\n"
            "from kaizen_components import db\n"
            "sup=Supervisor(); sup.boot(); install_factory(sup,scripted_provider([final_reply('done')]))\n"
            "def exercise(mode,label):\n"
            "    start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':label,'profile':{'permission_mode':mode}}}); rid,sid=start['agent_run_id'],start['session_id']; wait_idle(sup,rid); sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}})\n"
            "    before=db.fetch_one('SELECT COUNT(*) FROM agent_runs WHERE session_id=?',(sid,))[0]; entered,release=threading.Event(),threading.Event(); original=sup._materialize_request_artifacts; calls={'n':0}; results={}\n"
            "    def blocked(**kwargs):\n"
            "        calls['n']+=1\n"
            "        if calls['n']==1: entered.set(); release.wait(5)\n"
            "        return original(**kwargs)\n"
            "    sup._materialize_request_artifacts=blocked\n"
            "    a=threading.Thread(target=lambda:results.setdefault('a',sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'a'}}))); a.start(); barrier=entered.wait(3)\n"
            "    b=threading.Thread(target=lambda:results.setdefault('b',sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'b'}}))); b.start(); time.sleep(.1); waited=b.is_alive(); release.set(); a.join(5); b.join(5); sup._materialize_request_artifacts=original\n"
            "    after=db.fetch_one('SELECT COUNT(*) FROM agent_runs WHERE session_id=?',(sid,))[0]; ok=next((value for value in results.values() if value.get('status')=='OK'),None)\n"
            "    if ok is not None: wait_idle(sup,ok['agent_run_id']); sup._handle_control({'op':'session/close','args':{'agent_run_id':ok['agent_run_id']}})\n"
            "    return {'barrier':barrier,'waited':waited,'results':results,'runs':[before,after],'materialize_calls':calls['n']}\n"
            "plan=exercise('plan','plan'); source=exercise('ask','source'); sup.shutdown(); out={'plan':plan,'source':source}\n"
        )
        for mode in ("plan", "source"):
            with self.subTest(mode=mode):
                result = out[mode]
                self.assertTrue(result["barrier"])
                self.assertTrue(result["waited"])
                self.assertEqual(result["runs"][1] - result["runs"][0], 1)
                self.assertEqual(result["materialize_calls"], 1)
                statuses = sorted(value["status"] for value in result["results"].values())
                self.assertEqual(statuses, ["DENIED", "OK"])

    def test_source_conflict_is_zero_record_and_normal_completion_releases(self) -> None:
        out = self.drive(
            "import threading\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration import supervisor as S\n"
            "entered, release = threading.Event(), threading.Event()\n"
            "def provider(messages, **_opts):\n"
            "    entered.set(); release.wait(5); return {'text': final_reply('done')}\n"
            "sup = Supervisor(); sup.boot(); install_factory(sup, provider)\n"
            "first = sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','profile':{'permission_mode':'ask'}}})\n"
            "entered.wait(3)\n"
            "def counts(): return [db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0], db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0], db.fetch_one('SELECT COUNT(*) FROM agent_events')[0]]\n"
            "before = counts()\n"
            "second = sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'two','profile':{'permission_mode':'agent'}}})\n"
            "after = counts()\n"
            "release.set(); wait_idle(sup, first['agent_run_id'])\n"
            "registry = json.loads(S.OWNERSHIP_FILE.read_text(encoding='utf-8'))\n"
            "third = sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'three','profile':{'permission_mode':'ask'}}})\n"
            "wait_idle(sup, third['agent_run_id'])\n"
            "sup.shutdown()\n"
            "out={'first':first['status'],'second':second,'before':before,'after':after,'third':third['status'],'marker_after_idle':'__workspace_writer__' in registry}\n"
        )
        self.assertEqual(out["first"], "OK")
        self.assertEqual(out["second"]["code"], "DENIED_WORKSPACE_WRITER_BUSY")
        self.assertEqual(out["second"]["holder"]["permission_mode"], "ask")
        self.assertEqual(out["before"], out["after"])
        self.assertFalse(out["marker_after_idle"])
        self.assertEqual(out["third"], "OK")

    def test_two_read_only_plan_turns_run_concurrently_without_a_claim(self) -> None:
        out = self.drive(
            "import threading\n"
            "from kaizen_components.orchestration import supervisor as S\n"
            "lock, ready, release = threading.Lock(), threading.Event(), threading.Event(); state={'n':0}\n"
            "def provider(messages, **_opts):\n"
            "    with lock:\n"
            "        state['n'] += 1\n"
            "        if state['n'] == 2: ready.set()\n"
            "    release.wait(5); return {'text': final_reply('read only')}\n"
            "sup=Supervisor(); sup.boot(); install_factory(sup, provider)\n"
            "a=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'a','profile':{'permission_mode':'plan'}}})\n"
            "b=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'b','profile':{'permission_mode':'plan'}}})\n"
            "both=ready.wait(3); marker_during='__workspace_writer__' in json.loads(S.OWNERSHIP_FILE.read_text(encoding='utf-8'))\n"
            "release.set(); wait_idle(sup,a['agent_run_id']); wait_idle(sup,b['agent_run_id']); sup.shutdown()\n"
            "out={'a':a['status'],'b':b['status'],'both':both,'marker':marker_during}\n"
        )
        self.assertEqual((out["a"], out["b"]), ("OK", "OK"))
        self.assertTrue(out["both"])
        self.assertFalse(out["marker"])

    def test_plan_first_write_acquires_late_and_blocks_source_before_records(self) -> None:
        out = self.drive(
            "import threading\n"
            "from pathlib import Path\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration import supervisor as S\n"
            "from kaizen_components.orchestration.adapters import TurnResult\n"
            "entered, release = threading.Event(), threading.Event(); denials=[]; tracked=[]\n"
            "target=Path('.')\n"
            "class GuardAdapter:\n"
            "    def __init__(self): self.guard=None; self.session_id=None; self._child=type('Child',(),{'pid':7331})()\n"
            "    @property\n"
            "    def active_turn_id(self): return None\n"
            "    def set_mutation_guard(self, cb): self.guard=cb\n"
            "    def bind_session(self, sid): self.session_id=sid\n"
            "    def on_approval(self, _cb): pass\n"
            "    def start_session(self, **_kw): return {'status':'OK'}\n"
            "    def run_turn(self, _prompt):\n"
            "        action=policy.RequestedAction(actor=policy.Actor('local_llm',self.session_id or 's',0),verb='file_write',targets=(str(target),),raw={'cwd':str(target.parent.parent.parent)})\n"
            "        denial=self.guard(action) if self.guard else {'code':'NO_GUARD'}; denials.append(denial)\n"
            "        tracked.append(json.loads(S.OWNERSHIP_FILE.read_text(encoding='utf-8')).get('__workspace_writer__'))\n"
            "        if denial is None: target.parent.mkdir(parents=True,exist_ok=True); target.write_text('written',encoding='utf-8')\n"
            "        entered.set(); release.wait(5); return TurnResult(status='OK',final_text='done')\n"
            "    def kill(self): release.set(); return {'status':'OK','killed':True}\n"
            "    def close(self): return {'status':'OK','closed':True}\n"
            "sup=Supervisor(); sup.boot(); target=sup.repo_root/'AI'/'work'/'late.txt'; sup._adapter_factory=lambda *_a,**_k: GuardAdapter()\n"
            "plan=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'write','profile':{'permission_mode':'plan'}}}); entered.wait(3)\n"
            "before=[db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0]]\n"
            "source=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'source','profile':{'permission_mode':'ask'}}})\n"
            "after=[db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0]]\n"
            "release.set(); wait_idle(sup,plan['agent_run_id']); marker=target.is_file(); sup.shutdown()\n"
            "out={'plan':plan['status'],'guard':denials[0] if denials else 'missing','source':source,'before':before,'after':after,'marker':marker,'tracked':tracked[0]}\n"
        )
        self.assertEqual(out["plan"], "OK")
        self.assertIsNone(out["guard"])
        self.assertTrue(out["marker"])
        self.assertEqual(out["tracked"]["child_pids"], [7331])
        self.assertFalse(out["tracked"]["spawn_pending"])
        self.assertEqual(out["source"]["code"], "DENIED_WORKSPACE_WRITER_BUSY")
        self.assertEqual(out["before"], out["after"])

    def test_persistent_child_reattaches_to_each_source_turn_claim(self) -> None:
        out = self.drive(
            "from kaizen_components.orchestration import supervisor as S\n"
            "from kaizen_components.orchestration.adapters import TurnResult\n"
            "markers=[]\n"
            "class PersistentAdapter:\n"
            "    def __init__(self): self._child=type('Child',(),{'pid':7441})()\n"
            "    def bind_session(self,_sid): pass\n"
            "    def on_approval(self,_cb): pass\n"
            "    def start_session(self,**_kw): return {'status':'OK'}\n"
            "    def run_turn(self,_prompt): markers.append(json.loads(S.OWNERSHIP_FILE.read_text(encoding='utf-8'))['__workspace_writer__']); return TurnResult(status='OK',final_text='done')\n"
            "    def close(self): return {'status':'OK','closed':True}\n"
            "    def kill(self): return {'status':'OK','killed':True}\n"
            "sup=Supervisor(); sup.boot(); sup._adapter_factory=lambda *_a,**_k: PersistentAdapter()\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','profile':{'permission_mode':'ask'}}}); wait_idle(sup,start['agent_run_id'])\n"
            "turn=sup._handle_control({'op':'session/turn','args':{'agent_run_id':start['agent_run_id'],'prompt':'two'}}); wait_idle(sup,start['agent_run_id'])\n"
            "sup._handle_control({'op':'session/close','args':{'agent_run_id':start['agent_run_id']}}); out={'turn':turn,'markers':markers}\n"
        )
        self.assertEqual(out["turn"]["status"], "OK")
        self.assertEqual([marker["child_pids"] for marker in out["markers"]], [[7441], [7441]])
        self.assertNotEqual(out["markers"][0]["claim_id"], out["markers"][1]["claim_id"])

    def test_unproven_kill_retains_recovery_gate(self) -> None:
        out = self.drive(
            "import threading\n"
            "from kaizen_components.orchestration.adapters import TurnResult\n"
            "entered, release = threading.Event(), threading.Event()\n"
            "class StuckAdapter:\n"
            "    @property\n"
            "    def active_turn_id(self): return 'stuck'\n"
            "    def bind_session(self,_sid): pass\n"
            "    def on_approval(self,_cb): pass\n"
            "    def start_session(self,**_kw): return {'status':'OK'}\n"
            "    def run_turn(self,_prompt): entered.set(); release.wait(6); return TurnResult(status='OK',final_text='late')\n"
            "    def kill(self): return {'status':'ERROR','killed':True}\n"
            "sup=Supervisor(); sup.boot(); sup._adapter_factory=lambda *_a,**_k: StuckAdapter()\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'hold','profile':{'permission_mode':'ask'}}}); entered.wait(3)\n"
            "killed=sup._handle_control({'op':'session/kill','args':{'agent_run_id':start['agent_run_id']}})\n"
            "retry=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'retry','profile':{'permission_mode':'ask'}}})\n"
            "release.set(); time.sleep(.1)\n"
            "out={'killed':killed,'retry':retry}\n"
        )
        self.assertEqual(out["killed"]["status"], "DENIED")
        self.assertEqual(out["killed"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertFalse(out["killed"]["killed"])
        self.assertEqual(out["retry"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")

    def test_missing_kill_proof_after_false_close_requires_recovery(self) -> None:
        out = self.drive(
            "from kaizen_components.orchestration.adapters import TurnResult\n"
            "class UnprovenClose:\n"
            "    def bind_session(self,_sid): pass\n"
            "    def on_approval(self,_cb): pass\n"
            "    def start_session(self,**_kw): return {'status':'OK'}\n"
            "    def run_turn(self,_prompt): return TurnResult(status='OK',final_text='done')\n"
            "    def close(self): return {'status':'OK','closed':False}\n"
            "    def kill(self): return {'status':'OK'}\n"
            "sup=Supervisor(); sup.boot(); sup._adapter_factory=lambda *_a,**_k: UnprovenClose()\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','profile':{'permission_mode':'ask'}}}); wait_idle(sup,start['agent_run_id'])\n"
            "closed=sup._handle_control({'op':'session/close','args':{'agent_run_id':start['agent_run_id']}}); state=sup._safe_reduce(start['agent_run_id'])\n"
            "retry=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'retry','profile':{'permission_mode':'ask'}}})\n"
            "out={'closed':closed,'terminal_state':state['terminal_state'],'retry':retry}\n"
        )
        self.assertEqual(out["closed"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertFalse(out["closed"]["closed"])
        self.assertFalse(out["closed"]["killed"])
        self.assertEqual(out["terminal_state"], "failure")
        self.assertEqual(out["retry"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")


class AdditiveSessionPreflightTest(_DrivenSubprocess):
    """Additive image/context preflight rollback, stale-artifact, and zero-record atomicity behavior."""
    _UNCERTAIN_IMAGE_ADAPTER = (
        "from kaizen_components.orchestration.adapters import TurnResult\n"
        "from kaizen_components.orchestration.artifact_cache import ArtifactCacheError\n"
        "calls=[]\n"
        "class ImageAdapter:\n"
        "    def __init__(self): self.pending=[]\n"
        "    def bind_session(self,_sid): pass\n"
        "    def on_approval(self,_cb): pass\n"
        "    def start_session(self,**_kw): return {'status':'OK'}\n"
        "    def clear_next_turn_artifacts(self): self.pending=[]\n"
        "    def stage_attachment(self,content,**_kw): return {'bytes':len(content)}\n"
        "    def set_next_turn_artifacts(self,items): self.pending=list(items)\n"
        "    def run_turn(self,_prompt): calls.append(len(self.pending)); self.pending=[]; return TurnResult(status='OK',final_text='done')\n"
        "    def close(self): return {'status':'OK','closed':True}\n"
        "    def kill(self): self.pending=[]; return {'status':'OK','killed':True}\n"
        "adapter=ImageAdapter(); sup=Supervisor(); sup.boot(); sup._adapter_factory=lambda *_a,**_k:adapter; original_feature=sup._engine_feature; sup._engine_feature=lambda engine,feature: True if feature=='image_attachments' else original_feature(engine,feature)\n"
        "stored=sup._artifact_cache.store('images',b'\\x89PNG\\r\\n\\x1a\\nimage',scope_id='host',media_type='image/png',origin='host'); image={'id':'img','kind':'image','artifact_ref':stored.artifact_ref,'sha256':stored.sha256,'bytes':stored.bytes,'media_type':'image/png'}\n"
        "def uncertain_refs(*_a,**_kw): raise ArtifactCacheError('DENIED_ARTIFACT_WRITE','fixture',rollback_unproven=True)\n"
    )
    """Adapter fixture whose image-reference mutation reports unproven rollback recovery."""

    def test_start_image_rollback_uncertainty_terminalizes_before_provider_call(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n" + self._UNCERTAIN_IMAGE_ADAPTER +
            "before=[db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0]]; sup._artifact_cache.add_image_reference=uncertain_refs\n"
            "result=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'x','attachments':[image],'profile':{'permission_mode':'plan'}}}); after=[db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0]]; reduced=sup._safe_reduce(result['agent_run_id'])\n"
            "out={'result':result,'records':[before,after],'calls':calls,'pending':adapter.pending,'terminal':reduced['terminal'],'recovery':sup._writer_lease.recovery_required}\n"
        )
        self.assertEqual(out["result"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        before, after = out["records"]
        self.assertEqual([after_count - before_count for before_count, after_count in zip(before, after)], [1, 1])
        self.assertEqual(out["calls"], [])
        self.assertEqual(out["pending"], [])
        self.assertTrue(out["terminal"])
        self.assertTrue(out["recovery"])

    def test_live_image_rollback_uncertainty_terminalizes_before_next_provider_call(self) -> None:
        out = self.drive(
            self._UNCERTAIN_IMAGE_ADAPTER +
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','profile':{'permission_mode':'plan'}}}); rid=start['agent_run_id']; wait_idle(sup,rid); before=len(calls); sup._artifact_cache.add_image_reference=uncertain_refs\n"
            "result=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'two','attachments':[image]}}); reduced=sup._safe_reduce(rid)\n"
            "out={'result':result,'calls':[before,len(calls)],'pending':adapter.pending,'terminal':reduced['terminal'],'recovery':sup._writer_lease.recovery_required}\n"
        )
        self.assertEqual(out["result"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertEqual(out["calls"][0], out["calls"][1])
        self.assertEqual(out["pending"], [])
        self.assertTrue(out["terminal"])
        self.assertTrue(out["recovery"])

    def test_resume_image_rollback_uncertainty_terminalizes_new_leg_before_provider_call(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n" + self._UNCERTAIN_IMAGE_ADAPTER +
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','profile':{'permission_mode':'plan'}}}); rid,sid=start['agent_run_id'],start['session_id']; wait_idle(sup,rid); sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}}); before=db.fetch_one('SELECT COUNT(*) FROM agent_runs WHERE session_id=?',(sid,))[0]; before_calls=len(calls); sup._artifact_cache.add_image_reference=uncertain_refs\n"
            "result=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'resume','attachments':[image]}}); after=db.fetch_one('SELECT COUNT(*) FROM agent_runs WHERE session_id=?',(sid,))[0]; reduced=sup._safe_reduce(result['agent_run_id'])\n"
            "out={'result':result,'runs':[before,after],'calls':[before_calls,len(calls)],'pending':adapter.pending,'terminal':reduced['terminal'],'recovery':sup._writer_lease.recovery_required}\n"
        )
        self.assertEqual(out["result"]["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertEqual(out["runs"][1] - out["runs"][0], 1)
        self.assertEqual(out["calls"][0], out["calls"][1])
        self.assertEqual(out["pending"], [])
        self.assertTrue(out["terminal"])
        self.assertTrue(out["recovery"])

    def test_attachment_setter_and_second_stage_failures_clear_before_image_free_retry(self) -> None:
        out = self.drive(
            "from kaizen_components.orchestration.adapters import TurnResult\n"
            "seen=[]\n"
            "class ArtifactAdapter:\n"
            "    def __init__(self): self.pending=[]; self.mode='normal'; self.stage_count=0\n"
            "    def bind_session(self,_sid): pass\n"
            "    def on_approval(self,_cb): pass\n"
            "    def start_session(self,**_kw): return {'status':'OK'}\n"
            "    def clear_next_turn_artifacts(self): self.pending=[]\n"
            "    def stage_attachment(self,content,**_kw):\n"
            "        self.stage_count+=1\n"
            "        if self.mode=='fail_second' and self.stage_count==2: raise ValueError('second')\n"
            "        return {'sha256':str(len(content))}\n"
            "    def set_next_turn_artifacts(self,items):\n"
            "        self.pending=list(items)\n"
            "        if self.mode=='fail_set': raise ValueError('after set')\n"
            "    def run_turn(self,_prompt): seen.append(len(self.pending)); self.pending=[]; return TurnResult(status='OK',final_text='done')\n"
            "    def close(self): return {'status':'OK','closed':True}\n"
            "    def kill(self): self.pending=[]; return {'status':'OK','killed':True}\n"
            "adapter=ArtifactAdapter(); sup=Supervisor(); sup.boot(); sup._adapter_factory=lambda *_a,**_k: adapter\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','profile':{'permission_mode':'plan'}}}); rid=start['agent_run_id']; wait_idle(sup,rid); sess=sup._get_driven(rid); sess.image_attachments=True\n"
            "def image(content,item):\n"
            "    stored=sup._artifact_cache.store('images',content,scope_id='host',media_type='image/png',origin='host')\n"
            "    return {'id':item,'kind':'image','artifact_ref':stored.artifact_ref,'sha256':stored.sha256,'bytes':stored.bytes,'media_type':'image/png'}\n"
            "one=image(b'\\x89PNG\\r\\n\\x1a\\none','one'); two=image(b'\\x89PNG\\r\\n\\x1a\\ntwo','two')\n"
            "adapter.mode='fail_set'; first=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'image','attachments':[one]}}); pending_after_set=list(adapter.pending)\n"
            "adapter.mode='normal'; retry_one=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'clean-one'}}); wait_idle(sup,rid)\n"
            "adapter.mode='fail_second'; adapter.stage_count=0; second=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'images','attachments':[one,two]}}); pending_after_second=list(adapter.pending)\n"
            "adapter.mode='normal'; retry_two=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'clean-two'}}); wait_idle(sup,rid); sup.shutdown()\n"
            "out={'first':first,'second':second,'pending':[pending_after_set,pending_after_second],'retries':[retry_one['status'],retry_two['status']],'seen':seen}\n"
        )
        self.assertEqual(out["first"]["code"], "DENIED_ATTACHMENT_INVALID")
        self.assertEqual(out["second"]["code"], "DENIED_ATTACHMENT_INVALID")
        self.assertEqual(out["pending"], [[], []])
        self.assertEqual(out["retries"], ["OK", "OK"])
        self.assertEqual(out["seen"], [0, 0, 0])

    def test_invalid_and_unsupported_refs_are_zero_record_and_title_is_canonical(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "sup=Supervisor(); sup.boot(); install_factory(sup, scripted_provider([final_reply('ok')]))\n"
            "def counts(): return [db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0],db.fetch_one('SELECT COUNT(*) FROM agent_events')[0]]\n"
            "before=counts()\n"
            "bad=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'x','attachments':[{}]}})\n"
            "valid_image={'id':'i','kind':'image','artifact_ref':'sha256:'+'a'*64,'sha256':'a'*64,'bytes':1,'media_type':'image/png'}\n"
            "dark=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'x','attachments':[valid_image]}})\n"
            "after=counts()\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'  Alpha   beta  ','title':'Alpha beta','client_features':{'diff_snapshots':True}}})\n"
            "wait_idle(sup,start['agent_run_id'])\n"
            "row=db.fetch_one('SELECT title,policy_snapshot FROM agent_sessions WHERE id=?',(start['session_id'],))\n"
            "event_before=db.fetch_one('SELECT COUNT(*) FROM agent_events WHERE agent_run_id=?',(start['agent_run_id'],))[0]\n"
            "bad_turn=sup._handle_control({'op':'session/turn','args':{'agent_run_id':start['agent_run_id'],'prompt':'next','context_refs':[{}]}})\n"
            "event_after=db.fetch_one('SELECT COUNT(*) FROM agent_events WHERE agent_run_id=?',(start['agent_run_id'],))[0]\n"
            "sup.shutdown(); out={'bad':bad['code'],'dark':dark['code'],'before':before,'after':after,'title':row[0],'features':json.loads(row[1]).get('client_features'),'bad_turn':bad_turn['code'],'events':[event_before,event_after]}\n"
        )
        self.assertEqual(out["bad"], "DENIED_ATTACHMENT_INVALID")
        self.assertEqual(out["dark"], "DENIED_ATTACHMENT_UNSUPPORTED")
        self.assertEqual(out["before"], out["after"])
        self.assertEqual(out["title"], "Alpha beta")
        self.assertEqual(out["features"], {"diff_snapshots": True})
        self.assertEqual(out["bad_turn"], "DENIED_CONTEXT_INVALID")
        self.assertEqual(out["events"][0], out["events"][1])

    def test_start_and_resume_multi_context_denial_publish_no_records_or_cache(self) -> None:
        out = self.drive(
            "from pathlib import Path\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration import supervisor as S\n"
            "sup=Supervisor(); sup.boot(); install_factory(sup,scripted_provider([final_reply('done')]))\n"
            "root=Path(sup.repo_root); (root/'valid.txt').write_text('valid',encoding='utf-8'); (root/'invalid.txt').write_bytes(b'invalid\\x00binary'); refs=[{'id':'valid','kind':'file','source_path':'valid.txt'},{'id':'invalid','kind':'file','source_path':'invalid.txt'}]\n"
            "def counts(): return [db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0],db.fetch_one('SELECT COUNT(*) FROM agent_events')[0]]\n"
            "def cache_state():\n"
            "    base=sup._artifact_cache.cache_root\n"
            "    return {str(path.relative_to(base)):path.read_bytes().hex() for path in base.rglob('*') if path.is_file()} if base.exists() else {}\n"
            "before_start,cache_before=counts(),cache_state(); denied_start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'bad','profile':{'permission_mode':'ask'},'context_refs':refs}}); after_start,cache_after=counts(),cache_state(); marker_start='__workspace_writer__' in json.loads(S.OWNERSHIP_FILE.read_text(encoding='utf-8'))\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'one','profile':{'permission_mode':'ask'}}}); rid,sid=start['agent_run_id'],start['session_id']; wait_idle(sup,rid); sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}})\n"
            "before_resume=db.fetch_one('SELECT COUNT(*) FROM agent_runs WHERE session_id=?',(sid,))[0]; resume_cache_before=cache_state(); denied_resume=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'bad resume','context_refs':refs}}); after_resume=db.fetch_one('SELECT COUNT(*) FROM agent_runs WHERE session_id=?',(sid,))[0]; resume_cache_after=cache_state(); marker_resume='__workspace_writer__' in json.loads(S.OWNERSHIP_FILE.read_text(encoding='utf-8')); sup.shutdown()\n"
            "out={'start':denied_start,'start_counts':[before_start,after_start],'start_cache':cache_before==cache_after,'start_marker':marker_start,'resume':denied_resume,'resume_runs':[before_resume,after_resume],'resume_cache':resume_cache_before==resume_cache_after,'resume_marker':marker_resume}\n"
        )
        self.assertEqual(out["start"]["code"], "DENIED_CONTEXT_INVALID")
        self.assertEqual(out["start_counts"][0], out["start_counts"][1])
        self.assertTrue(out["start_cache"])
        self.assertFalse(out["start_marker"])
        self.assertEqual(out["resume"]["code"], "DENIED_CONTEXT_INVALID")
        self.assertEqual(out["resume_runs"][0], out["resume_runs"][1])
        self.assertTrue(out["resume_cache"])
        self.assertFalse(out["resume_marker"])

    def test_session_list_emits_title_snippet_and_legacy_null(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "sup=Supervisor(); sup.boot(); install_factory(sup, scripted_provider([final_reply('ok')]))\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'  Alpha   beta  '}})\n"
            "wait_idle(sup,start['agent_run_id'])\n"
            "listed=sup._handle_control({'op':'session/list','args':{'controller':'driven'}})\n"
            "entry=next(item for item in listed['sessions'] if item['session_id']==start['session_id'])\n"
            "db.write_tx(lambda conn,attempt: conn.execute('UPDATE agent_sessions SET title=NULL WHERE id=?',(start['session_id'],)))\n"
            "legacy=sup._handle_control({'op':'session/list','args':{'controller':'driven'}})\n"
            "legacy_entry=next(item for item in legacy['sessions'] if item['session_id']==start['session_id'])\n"
            "sup.shutdown(); out={'title':entry['title'],'snippet':entry['snippet'],'legacy_title':legacy_entry['title'],'truncated':listed['truncated']}\n"
        )
        self.assertEqual(out["title"], "Alpha beta")
        self.assertEqual(out["snippet"], "Alpha beta")
        self.assertIsNone(out["legacy_title"])
        self.assertFalse(out["truncated"])


# --- gated live smoke (real Ollama) -----------------------------------------------------------

@unittest.skipUnless(
    _LIVE and os.environ.get("KAIZEN_LLM_MODEL") and os.environ.get("KAIZEN_LLM_BASE_URL"),
    "live driven smoke -- set KAIZEN_RUN_LIVE=1 + KAIZEN_LLM_MODEL + KAIZEN_LLM_BASE_URL (real Ollama)",
)
class LiveDrivenSmokeTest(unittest.TestCase):
    """Real Ollama-backed turns in an isolated D-drive workspace using the production adapter/tools."""

    # Id-scoped cleanup of one driven run's rows (events -> approvals -> run -> session).
    _CLEANUP = (
        "conn = db.connect()\n"
        "conn.execute('DELETE FROM agent_events WHERE agent_run_id = ?', (rid,))\n"
        "conn.execute('DELETE FROM approval_requests WHERE session_id = ?', (sid,))\n"
        "conn.execute('DELETE FROM agent_runs WHERE id = ?', (rid,))\n"
        "conn.execute('DELETE FROM agent_sessions WHERE id = ?', (sid,))\n"
        "conn.commit()\n"
    )

    def test_real_ollama_l7_restart_continuation_title_snippet_and_fidelity(self) -> None:
        marker = "KZL7-7319"
        first_prompt = f'Remember marker {marker}. Reply exactly {{"final":"remembered {marker}"}}.'
        second_prompt = 'Reply with JSON whose "final" contains the marker from the prior conversation.'
        body = (
            "from kaizen_components import db\n"
            "sup=Supervisor(); sup.boot(); sup._driven_test_records=True\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':"
            + repr(first_prompt) + ",'max_turns':3,'model':" + repr(os.environ["KAIZEN_LLM_MODEL"]) + "}})\n"
            "rid,sid=start['agent_run_id'],start['session_id']; wait_idle(sup,rid,budget=120.0)\n"
            "sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}})\n"
            "before=sup._handle_control({'op':'session/list','args':{'controller':'driven'}})['sessions'][0]\n"
            "turn=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':"
            + repr(second_prompt) + "}})\n"
            "new_rid=turn.get('agent_run_id'); wait_idle(sup,new_rid,budget=120.0)\n"
            "profile=json.loads(db.fetch_one(\"SELECT body FROM agent_events WHERE agent_run_id=? AND event_kind='profile'\",(new_rid,))[0])\n"
            "assistant=json.loads(db.fetch_one(\"SELECT body FROM agent_events WHERE agent_run_id=? AND event_kind='chat_message' AND json_extract(body,'$.role')='assistant' ORDER BY sequence_no DESC LIMIT 1\",(new_rid,))[0])\n"
            "after=sup._handle_control({'op':'session/list','args':{'controller':'driven'}})['sessions'][0]\n"
            "sup._handle_control({'op':'session/close','args':{'agent_run_id':new_rid}}); sup.shutdown()\n"
            "conn=db.connect(); conn.execute('DELETE FROM agent_events WHERE agent_run_id IN (?,?)',(rid,new_rid)); conn.execute('DELETE FROM approval_requests WHERE session_id=?',(sid,)); conn.execute('DELETE FROM agent_runs WHERE id IN (?,?)',(rid,new_rid)); conn.execute('DELETE FROM agent_sessions WHERE id=?',(sid,)); conn.commit()\n"
            "out={'start':start,'turn':turn,'before':before,'after':after,'profile':profile,'assistant':assistant}\n"
        )
        out = self._drive_real(body)
        self.assertEqual(out["start"]["status"], "OK", out)
        self.assertEqual(out["turn"]["status"], "OK", out)
        self.assertEqual(out["turn"]["resume_fidelity"], "reduced", out)
        self.assertEqual(out["profile"]["resume_fidelity"], "reduced", out)
        self.assertEqual(out["after"]["resume_fidelity"], "reduced", out)
        self.assertEqual(out["before"]["title"], first_prompt[:80], out)
        self.assertEqual(out["before"]["snippet"], first_prompt, out)
        self.assertIn(marker, out["assistant"]["text"], out)

    def test_real_ollama_driven_two_turns_share_context_and_close_once(self) -> None:
        # Bounded H2.2 live proof: two context-linked turns share one C1/T5, remain non-terminal while
        # idle, then explicit close writes exactly one successful T8.
        marker = "KZCTX7421"
        target_rel = "AI/work/harness-ui-v1/h2.2-live-two-turn.txt"
        first_prompt = (
            f'Remember marker {marker}. Reply with exactly {{"final": "remembered {marker}"}} and nothing else.'
        )
        second_prompt = (
            'Reply with exactly one JSON object whose "final" value contains the marker from my previous prompt.'
        )
        body = (
            "sup = Supervisor(); sup.boot()\n"
            "sup._driven_test_records = True\n"
            "from pathlib import Path as _P\n"
            "target = _P(str(sup.repo_root)) / " + repr(target_rel) + "\n"
            "if target.exists(): target.unlink()\n"
            "children_before = len(sup._children)\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': "
            + repr(first_prompt) + ", 'max_turns': 3, 'model': "
            + repr(os.environ["KAIZEN_LLM_MODEL"]) + "}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "wait_idle(sup, rid, budget=120.0)\n"
            "pre1 = sup._safe_reduce(rid)\n"
            "turn2 = sup._handle_control({'op': 'session/turn', 'args': {'agent_run_id': rid, 'prompt': "
            + repr(second_prompt) + "}})\n"
            "wait_idle(sup, rid, budget=120.0)\n"
            "from kaizen_components import db\n"
            "pre2 = sup._safe_reduce(rid)\n"
            "t8_before = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
            "close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "state = sup._safe_reduce(rid)\n"
            "sess = db.fetch_one('SELECT id, is_test FROM agent_sessions WHERE id = ?', (sid,))\n"
            "runrow = db.fetch_one('SELECT id, surface, is_test FROM agent_runs WHERE id = ?', (rid,))\n"
            "evc = db.fetch_one('SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ?', (rid,))[0]\n"
            "t8_after = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
            "seqs = [r[0] for r in db.fetch_all('SELECT sequence_no FROM agent_events WHERE agent_run_id = ? ORDER BY sequence_no', (rid,))]\n"
            "chat = [json.loads(r[0]) for r in db.fetch_all(\"SELECT body FROM agent_events WHERE agent_run_id = ? AND event_kind = 'chat_message' ORDER BY sequence_no\", (rid,))]\n"
            "written = target.read_text(encoding='utf-8') if target.exists() else None\n"
            "if target.exists(): target.unlink()\n"
            "file_clean = not target.exists()\n"
            "children_after = len(sup._children)\n"
            "sup.shutdown()\n"
            + self._CLEANUP +
            "out = {'session': bool(sess), 'session_is_test': sess[1] if sess else None,\n"
            "       'run_surface': runrow[1] if runrow else None, 'run_is_test': runrow[2] if runrow else None,\n"
            "       'events': evc, 'turn2': turn2, 'close': close, 'pre1': pre1['terminal'],\n"
            "       'pre2': pre2['terminal'], 't8_before': t8_before, 't8_after': t8_after,\n"
            "       'chat': chat, 'seqs': seqs, 'written': written, 'file_clean': file_clean,\n"
            "       'children_before': children_before, 'children_after': children_after,\n"
            "       'terminal': state['terminal'] if state else None,\n"
            "       'terminal_state': state.get('terminal_state') if state else None,\n"
            "       'run_id': rid, 'session_id': sid}\n"
        )
        out = self._drive_real(body)
        self.assertTrue(out["session"])
        self.assertEqual(out["session_is_test"], 1)
        self.assertEqual(out["run_surface"], "app-server")
        self.assertEqual(out["run_is_test"], 1)
        self.assertEqual(out["turn2"]["status"], "OK")
        self.assertFalse(out["pre1"])
        self.assertFalse(out["pre2"])
        self.assertEqual(out["t8_before"], 0)
        self.assertEqual(
            [(m["role"]) for m in out["chat"]],
            ["user", "assistant", "user", "assistant"],
            out,
        )
        self.assertIn(marker, out["chat"][-1]["text"])
        self.assertIsNone(out["written"])
        self.assertTrue(out["file_clean"])
        self.assertEqual(out["children_before"], 0)
        self.assertEqual(out["children_after"], 0)
        self.assertEqual(out["close"]["status"], "OK")
        self.assertEqual(out["t8_after"], 1)
        self.assertEqual(out["seqs"], list(range(1, len(out["seqs"]) + 1)))
        self.assertGreaterEqual(out["events"], 8)
        self.assertTrue(out["terminal"])
        self.assertEqual(out["terminal_state"], "success")

    def test_real_ollama_approval_round_trip_by_correlation_id(self) -> None:
        # The governed lane (H0 exit criterion): the model calls the default write_file tool, the
        # rule-less policy lattice decides ASK, the driver approves by the STREAM correlation_id
        # (race-free alt-key), the parked waiter releases, the tool runs (ground truth: the file's
        # content), and the turn finals. All rows + the written file are deleted by id after.
        target_rel = "AI/work/harness-ui-v1/live-smoke.txt"
        marker = "kaizen-live-smoke"
        prompt = (
            'Call the write_file tool with path "' + target_rel + '" and content "' + marker
            + '". After the tool result arrives, reply with {"final": "written"}.'
        )
        body = (
            "sup = Supervisor(); sup.boot()\n"
            "sup._driven_test_records = True\n"
            # permission_mode 'ask' explicitly: under the new default 'plan', AI/work is a DESIGNATED root
            # (allow, no ask) so the write would not gate -- 'ask' keeps the approval round-trip exercised.
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': "
            + repr(prompt) + ", 'max_turns': 4, 'approval_timeout': 120.0, 'profile': {'permission_mode': 'ask'}, 'model': "
            + repr(os.environ["KAIZEN_LLM_MODEL"]) + "}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "corr = wait_open_approval(sup, rid, budget=120.0)\n"
            "parked = wait_waiter_parked(sup, rid, corr, budget=30.0) if corr else False\n"
            "dec = sup._handle_control({'op': 'approve', 'args': {'correlation_id': corr, 'session_id': sid, "
            "'decision': 'approve'}}) if corr else {'status': 'NO_ASK'}\n"
            "state = wait_terminal(sup, rid, budget=120.0)\n"
            "from kaizen_components import db\n"
            "c4 = db.fetch_one('SELECT state, decided_by FROM approval_requests WHERE session_id = ? AND "
            "correlation_id = ?', (sid, corr)) if corr else None\n"
            "tool_ok = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND "
            "event_kind = 'tool_call' AND marker = 'close_ok'\", (rid,))[0]\n"
            "from pathlib import Path as _P\n"
            "target = _P(str(sup.repo_root)) / " + repr(target_rel) + "\n"
            "written = target.read_text(encoding='utf-8') if target.exists() else None\n"
            "if target.exists(): target.unlink()\n"
            "sup.shutdown()\n"
            + self._CLEANUP +
            "out = {'corr': corr, 'parked': parked, 'decide_status': dec.get('status'),\n"
            "       'waiter_released': dec.get('waiter_released'), 'c4': list(c4) if c4 else None,\n"
            "       'tool_ok': tool_ok, 'written': written,\n"
            "       'terminal': state['terminal'] if state else None,\n"
            "       'terminal_state': state.get('terminal_state') if state else None,\n"
            "       'run_id': rid, 'session_id': sid}\n"
        )
        out = self._drive_real(body)
        self.assertIsNotNone(out["corr"], "no approval/open reached the stream (policy did not ASK?)")
        self.assertTrue(out["parked"])
        self.assertEqual(out["decide_status"], "OK")
        self.assertTrue(out["waiter_released"])
        self.assertEqual(out["c4"], ["approved", "human"])
        self.assertGreaterEqual(out["tool_ok"], 1)
        self.assertEqual(out["written"], marker)
        self.assertTrue(out["terminal"])
        self.assertEqual(out["terminal_state"], "success")

    def test_real_ollama_plan_allows_designated_root_write_without_ask(self) -> None:
        # H2.7 positive live proof (plan v3 live acceptance: "one allowed designated write" per drivable
        # engine).  Plan mode seeds AI/work + AI/generation as designated write roots, so a write_file
        # into the plane's AI/work must run WITHOUT any approval event (allow, not ask) and land on disk.
        target_rel = "AI/work/h2.7-designated-write.txt"
        marker = "kaizen-h2.7-designated-allow"
        prompt = (
            'Call the write_file tool with path "' + target_rel + '" and content "' + marker
            + '". After the tool result arrives, reply with {"final": "written"}.'
        )
        body = (
            "sup = Supervisor(); sup.boot()\n"
            "sup._driven_test_records = True\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': "
            + repr(prompt) + ", 'max_turns': 4, 'profile': {'permission_mode': 'plan'}, 'model': "
            + repr(os.environ["KAIZEN_LLM_MODEL"]) + "}})\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "wait_idle(sup, rid, budget=120.0)\n"
            "from kaizen_components import db\n"
            "markers = [r[0] for r in db.fetch_all(\"SELECT marker FROM agent_events WHERE agent_run_id = ? AND event_kind = 'approval' ORDER BY sequence_no\", (rid,))]\n"
            "c4_rows = db.fetch_one('SELECT COUNT(*) FROM approval_requests WHERE session_id = ?', (sid,))[0]\n"
            "tool_ok = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND "
            "event_kind = 'tool_call' AND marker = 'close_ok'\", (rid,))[0]\n"
            "from pathlib import Path as _P\n"
            "target = _P(str(sup.repo_root)) / " + repr(target_rel) + "\n"
            "written = target.read_text(encoding='utf-8') if target.exists() else None\n"
            "close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "state = sup._safe_reduce(rid)\n"
            "t8_after = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
            "children_after = len(sup._children)\n"
            "sup.shutdown()\n"
            + self._CLEANUP +
            "out = {'markers': markers, 'c4_rows': c4_rows, 'tool_ok': tool_ok, 'written': written,\n"
            "       'close': close, 't8_after': t8_after, 'children_after': children_after,\n"
            "       'terminal': state['terminal'] if state else None,\n"
            "       'terminal_state': state.get('terminal_state') if state else None,\n"
            "       'run_id': rid, 'session_id': sid}\n"
        )
        out = self._drive_real(body)
        # Allowed WITHOUT asking: no approval/open, no decline, no C4 row -- only the policy-allow
        # audit event (approval/resolved) the adapter emits for every gate-terminal ALLOW.
        self.assertNotIn("open", out["markers"], out)
        self.assertNotIn("declined", out["markers"], out)
        self.assertIn("resolved", out["markers"], out)
        self.assertEqual(out["c4_rows"], 0, out)
        self.assertGreaterEqual(out["tool_ok"], 1, out)
        self.assertEqual(out["written"], marker, out)
        self.assertEqual(out["close"]["status"], "OK", out)
        self.assertEqual(out["t8_after"], 1, out)
        self.assertEqual(out["children_after"], 0, out)
        self.assertTrue(out["terminal"], out)
        self.assertEqual(out["terminal_state"], "success", out)

    def test_real_ollama_plan_denies_out_of_profile_write_and_closes_once(self) -> None:
        # H2.7 negative live proof.  The target is deliberately outside the isolated KAIZEN_REPO_ROOT
        # but remains a harmless D-drive scratch file owned by this test.  Plan's write_out ceiling must
        # deny before tool execution; the conversation stays idle/non-terminal until explicit close.
        scratch_parent = REPO_ROOT / "AI/work/harness-ui-v1"
        scratch_parent.mkdir(parents=True, exist_ok=True)
        target_dir = Path(tempfile.mkdtemp(prefix="h2.7-denied-", dir=str(scratch_parent)))
        target = target_dir / "outside.txt"
        marker = "kaizen-h2.7-must-not-exist"
        prompt = (
            'Call the write_file tool with path "' + str(target) + '" and content "' + marker
            + '". If the tool is denied, reply with exactly {"final": "denied"}.'
        )
        if target.exists():
            target.unlink()
        try:
            body = (
                "sup = Supervisor(); sup.boot()\n"
                "sup._driven_test_records = True\n"
                "from pathlib import Path as _P\n"
                "target = _P(" + repr(str(target)) + ")\n"
                "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'local_llm', 'prompt': "
                + repr(prompt) + ", 'max_turns': 4, 'profile': {'permission_mode': 'plan'}, 'model': "
                + repr(os.environ["KAIZEN_LLM_MODEL"]) + "}})\n"
                "rid, sid = start['agent_run_id'], start['session_id']\n"
                "wait_idle(sup, rid, budget=120.0)\n"
                "from kaizen_components import db\n"
                "pre = sup._safe_reduce(rid)\n"
                "t8_before = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
                "denied_codes = [r[0] for r in db.fetch_all(\"SELECT code FROM agent_events WHERE agent_run_id = ? AND event_kind = 'approval' AND marker = 'declined' ORDER BY sequence_no\", (rid,))]\n"
                "exists_before_close = target.exists()\n"
                "close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
                "state = sup._safe_reduce(rid)\n"
                "t8_after = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
                "if target.exists(): target.unlink()\n"
                "absent_after = not target.exists()\n"
                "sup.shutdown()\n"
                + self._CLEANUP +
                "out = {'denied_codes': denied_codes, 'exists_before_close': exists_before_close,\n"
                "       'absent_after': absent_after, 'pre_terminal': pre['terminal'],\n"
                "       't8_before': t8_before, 't8_after': t8_after, 'close': close,\n"
                "       'terminal': state['terminal'] if state else None,\n"
                "       'terminal_state': state.get('terminal_state') if state else None,\n"
                "       'run_id': rid, 'session_id': sid}\n"
            )
            out = self._drive_real(body)
        finally:
            if target.exists():
                target.unlink()
            _rmtree(target_dir)
        self.assertIn("MODE_CEILING:write_out", out["denied_codes"], out)
        self.assertFalse(out["exists_before_close"], out)
        self.assertTrue(out["absent_after"], out)
        self.assertFalse(out["pre_terminal"], out)
        self.assertEqual(out["t8_before"], 0, out)
        self.assertEqual(out["close"]["status"], "OK", out)
        self.assertEqual(out["t8_after"], 1, out)
        self.assertTrue(out["terminal"], out)
        self.assertEqual(out["terminal_state"], "success", out)

    def _drive_real(self, body: str) -> dict:
        """Initialize a fresh live-test scratch plane under AI/work, execute BODY with the configured local model, and parse its RESULT within the extended timeout."""
        script = "BODY = " + repr(body) + "\n" + _PREAMBLE
        full = dict(os.environ)
        scratch_parent = REPO_ROOT / "AI/work/harness-ui-v1"
        scratch_parent.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="driven-live-", dir=str(scratch_parent)))
        try:
            rc, payload = kaizen(root, "K1")
            self.assertEqual(rc, 0, payload)
            full["KAIZEN_REPO_ROOT"] = str(root)
            proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                  cwd=str(REPO_ROOT), env=full, timeout=420)
            for line in proc.stdout.splitlines():
                if line.startswith("RESULT "):
                    return json.loads(line[len("RESULT "):])
            self.fail(f"no RESULT.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-2000:]}")
        finally:
            _rmtree(root)
if __name__ == "__main__":
    unittest.main()
