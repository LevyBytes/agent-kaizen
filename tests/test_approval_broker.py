"""P0 durable approval broker: atomic C4+T6 transitions and post-commit waiter release."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from _harness import IsolatedDBTest, REPO_ROOT


class ApprovalBrokerTest(IsolatedDBTest):
    """In-process ApprovalBroker driven against a real isolated DB via _run subprocesses."""
    def _session_run(self) -> tuple[str, str]:
        """Open a C1 session + T5 driven run; return (session_id, run_id)."""
        rc, session = self.kz(
            "C1", "--controller", "kaizen", "--mode", "orchestrate", "--engine", "local_llm",
            "--summary", "Broker test session.",
        )
        self.assertEqual(rc, 0, session)
        sid = session["id"]
        rc, run = self.kz(
            "T5", "--summary", "Broker test run.", "--payload-json",
            json.dumps({
                "agent_type": "other", "surface": "vscode-extension", "session_id": sid,
                "engine": "local_llm",
            }),
        )
        self.assertEqual(rc, 0, run)
        return sid, run["id"]

    def _run(self, body: str) -> dict:
        """Exec a Python body in a subprocess pinned to this test's KAIZEN_REPO_ROOT (cwd=REPO_ROOT); assert rc==0 and parse the LAST RESULT <json> line." The RESULT-prefix / last-line / env-pinning contract is non-obvious."""
        env = dict(os.environ)
        env["KAIZEN_REPO_ROOT"] = str(self.root)
        proc = subprocess.run(
            [sys.executable, "-c", body], capture_output=True, text=True,
            cwd=str(REPO_ROOT), env=env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
        self.assertTrue(lines, proc.stdout)
        return json.loads(lines[-1][len("RESULT "):])

    def test_negotiated_confirmation_and_post_commit_release(self) -> None:
        """Require negotiated confirmation and release only after the durable decision commits."""
        sid, run_id = self._session_run()
        out = self._run(
            "import json\n"
            "from datetime import datetime, timedelta, timezone\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.approvals import ApprovalBroker, canonical_snapshot_set_sha256\n"
            f"sid={sid!r}; rid={run_id!r}\n"
            "side=lambda h: {'artifact_ref':'sha256:'+h,'sha256':h,'bytes':4,'encoding':None,'media_type':'application/octet-stream'}\n"
            "h1='1'*64; h2='2'*64\n"
            "body={'approval_revision':1,'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat().replace('+00:00','Z'),'snapshot_set_sha256':'0'*64,'file_changes':[{'change_id':'c1','path':'src/a.bin','kind':'modify','old_path':None,'preview_mode':'metadata','preview_reason':'binary','before':side(h1),'proposed':side(h2)}]}\n"
            "body['snapshot_set_sha256']=canonical_snapshot_set_sha256(body)\n"
            "seen=[]\n"
            "def release(session_id, run_id, corr, decision):\n"
            "    row=db.fetch_one('SELECT state FROM approval_requests WHERE session_id=? AND correlation_id=?',(session_id,corr))\n"
            "    terminal=db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id=? AND source_event_id LIKE 'approval:%:terminal'\",(run_id,))[0]\n"
            "    seen.append([row[0],terminal,decision]); return True\n"
            "broker=ApprovalBroker(release)\n"
            "opened=broker.open(session_id=sid,agent_run_id=rid,correlation_id='corr-confirm',body=body,negotiated=True,is_test=True)\n"
            "queried=broker.get(opened['id'])\n"
            "expiry_delta=(datetime.fromisoformat(queried['active']['body']['expires_at'].replace('Z','+00:00'))-datetime.fromisoformat(queried['created_at'])).total_seconds()\n"
            "missing=broker.resolve(opened['id'],rid,'approve')\n"
            "mismatch=broker.resolve(opened['id'],rid,'approve',expected_revision=2,snapshot_set_sha256=body['snapshot_set_sha256'],metadata_confirmed=True)\n"
            "metadata=broker.resolve(opened['id'],rid,'approve',expected_revision=1,snapshot_set_sha256=body['snapshot_set_sha256'])\n"
            "resolved=broker.resolve(opened['id'],rid,'approve',expected_revision=1,snapshot_set_sha256=body['snapshot_set_sha256'],metadata_confirmed=True)\n"
            "duplicate=broker.resolve(opened['id'],rid,'deny')\n"
            "state=db.fetch_one('SELECT state FROM approval_requests WHERE id=?',(opened['id'],))[0]\n"
            "events=db.fetch_all('SELECT marker,source_event_id FROM agent_events WHERE agent_run_id=? AND correlation_id=? ORDER BY sequence_no',(rid,'corr-confirm'))\n"
            "print('RESULT '+json.dumps({'opened':opened,'queried':queried,'expiry_delta':expiry_delta,'missing':missing,'mismatch':mismatch,'metadata':metadata,'resolved':resolved,'duplicate':duplicate,'state':state,'events':events,'seen':seen,'hash':body['snapshot_set_sha256']}))\n"
        )
        self.assertEqual(out["opened"]["state"], "open")
        self.assertTrue(out["queried"]["broker_managed"])
        self.assertTrue(out["queried"]["active"]["negotiated"])
        self.assertEqual(out["queried"]["active"]["revision"], 1)
        self.assertAlmostEqual(out["expiry_delta"], 300.0, places=3)
        self.assertEqual(out["missing"]["code"], "DENIED_APPROVAL_CONFIRMATION_REQUIRED")
        self.assertEqual(out["missing"]["required_action"], "refresh_preview")
        self.assertEqual(out["mismatch"]["code"], "DENIED_APPROVAL_REVISION_MISMATCH")
        self.assertEqual(out["metadata"]["required_action"], "confirm_metadata")
        self.assertTrue(out["metadata"]["metadata_confirmation_required"])
        self.assertEqual(out["resolved"]["status"], "OK")
        self.assertTrue(out["resolved"]["waiter_released"])
        self.assertEqual(out["duplicate"]["code"], "DENIED_APPROVAL_ALREADY_DECIDED")
        self.assertEqual(out["state"], "approved")
        self.assertEqual([event[0] for event in out["events"]], ["open", "resolved"])
        self.assertRegex(out["events"][0][1], r"^approval:apr_.+:open:1$")
        self.assertRegex(out["events"][1][1], r"^approval:apr_.+:terminal$")
        self.assertEqual(out["seen"], [["approved", 1, "approved"]])

    def test_invalid_body_and_snapshot_are_sanitized_terminal_denials(self) -> None:
        """Sanitize invalid approval inputs into terminal structured denials."""
        sid, run_id = self._session_run()
        out = self._run(
            "import json\n"
            "from datetime import datetime, timedelta, timezone\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.approvals import ApprovalBroker\n"
            f"sid={sid!r}; rid={run_id!r}\n"
            "seen=[]\n"
            "def release(s,r,c,d):\n"
            "    state=db.fetch_one('SELECT state FROM approval_requests WHERE session_id=? AND correlation_id=?',(s,c))[0]\n"
            "    terminal=db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id=? AND correlation_id=? AND marker='declined'\",(r,c))[0]\n"
            "    seen.append([c,state,terminal,d]); return True\n"
            "broker=ApprovalBroker(release)\n"
            "bad_body=broker.open(session_id=sid,agent_run_id=rid,correlation_id='bad-body',body={'unexpected':True},negotiated=True,is_test=True)\n"
            "h='1'*64; side={'artifact_ref':'sha256:'+h,'sha256':h,'bytes':1,'encoding':'utf-8','media_type':None}\n"
            "wrong_hash={'approval_revision':1,'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat().replace('+00:00','Z'),'snapshot_set_sha256':'0'*64,'file_changes':[{'change_id':'c1','path':'a.txt','kind':'modify','old_path':None,'preview_mode':'text','preview_reason':None,'before':side,'proposed':side}]}\n"
            "bad_snapshot=broker.open(session_id=sid,agent_run_id=rid,correlation_id='bad-snapshot',body=wrong_hash,negotiated=True,is_test=True)\n"
            "rows=db.fetch_all(\"SELECT correlation_id,state,decided_by,summary FROM approval_requests WHERE session_id=? ORDER BY correlation_id\",(sid,))\n"
            "events=[]\n"
            "for corr in ('bad-body','bad-snapshot'):\n"
            "    events.append(db.fetch_one('SELECT marker,code,summary,body,source_event_id FROM agent_events WHERE agent_run_id=? AND correlation_id=?',(rid,corr)))\n"
            "print('RESULT '+json.dumps({'bad_body':bad_body,'bad_snapshot':bad_snapshot,'rows':rows,'events':events,'seen':seen}))\n"
        )
        self.assertEqual(out["bad_body"]["code"], "DENIED_APPROVAL_BODY_INVALID")
        self.assertEqual(out["bad_snapshot"]["code"], "DENIED_APPROVAL_SNAPSHOT_INVALID")
        self.assertTrue(out["bad_body"]["waiter_released"])
        self.assertEqual([row[1] for row in out["rows"]], ["denied", "denied"])
        self.assertTrue(all(row[2] == "auto" for row in out["rows"]))
        self.assertTrue(all(row[3] == "approval preview unavailable; denied fail-closed" for row in out["rows"]))
        self.assertEqual([event[0] for event in out["events"]], ["declined", "declined"])
        self.assertEqual([event[1] for event in out["events"]], [
            "DENIED_APPROVAL_BODY_INVALID", "DENIED_APPROVAL_SNAPSHOT_INVALID",
        ])
        self.assertTrue(all(event[2] == "approval preview unavailable; denied fail-closed" for event in out["events"]))
        for event in out["events"]:
            self.assertEqual(json.loads(event[3])["presentation"], "unavailable")
            self.assertRegex(event[4], r"^approval:apr_.+:terminal$")
        self.assertEqual(len(out["seen"]), 2)

    def test_refreshed_revision_is_durable_keeps_deadline_and_requires_second_accept(self) -> None:
        """Persist refreshed revisions without extending deadlines and require renewed acceptance."""
        sid, run_id = self._session_run()
        out = self._run(
            "import json\n"
            "from datetime import datetime, timedelta, timezone\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.approvals import ApprovalBroker, canonical_snapshot_set_sha256\n"
            f"sid={sid!r}; rid={run_id!r}\n"
            "def make(rev,digest):\n"
            "    side={'artifact_ref':'sha256:'+digest,'sha256':digest,'bytes':3,'encoding':'utf-8','media_type':None}\n"
            "    value={'approval_revision':rev,'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat().replace('+00:00','Z'),'snapshot_set_sha256':'0'*64,'file_changes':[{'change_id':'c1','path':'src/a.txt','kind':'modify','old_path':None,'preview_mode':'text','preview_reason':None,'before':side,'proposed':side}]}\n"
            "    value['snapshot_set_sha256']=canonical_snapshot_set_sha256(value); return value\n"
            "released=[]; broker=ApprovalBroker(lambda s,r,c,d: released.append(d) is None)\n"
            "bad_initial=ApprovalBroker().open(session_id=sid,agent_run_id=rid,correlation_id='bad-initial-revision',body=make(2,'9'*64),negotiated=True,is_test=True)\n"
            "v1=make(1,'1'*64); opened=broker.open(session_id=sid,agent_run_id=rid,correlation_id='refresh',body=v1,negotiated=True,is_test=True)\n"
            "first=broker.get(opened['id']); wrong=broker.refresh(opened['id'],rid,make(3,'3'*64))\n"
            "v2=make(2,'2'*64); refreshed=broker.refresh(opened['id'],rid,v2); replayed=broker.refresh(opened['id'],rid,v2); recovered=broker.get(opened['id'])\n"
            "old_accept=broker.resolve(opened['id'],rid,'approve',expected_revision=1,snapshot_set_sha256=v1['snapshot_set_sha256'])\n"
            "accepted=broker.resolve(opened['id'],rid,'approve',expected_revision=2,snapshot_set_sha256=v2['snapshot_set_sha256'])\n"
            "events=db.fetch_all('SELECT marker,source_event_id FROM agent_events WHERE agent_run_id=? AND correlation_id=? ORDER BY sequence_no',(rid,'refresh'))\n"
            "open_bodies=[json.loads(row[0]) for row in db.fetch_all(\"SELECT body FROM agent_events WHERE agent_run_id=? AND correlation_id=? AND marker='open' ORDER BY sequence_no\",(rid,'refresh'))]\n"
            "rows=db.fetch_one('SELECT COUNT(*) FROM approval_requests WHERE id=?',(opened['id'],))[0]\n"
            "print('RESULT '+json.dumps({'bad_initial':bad_initial,'first':first,'wrong':wrong,'refreshed':refreshed,'replayed':replayed,'recovered':recovered,'old_accept':old_accept,'accepted':accepted,'events':events,'open_bodies':open_bodies,'rows':rows,'released':released}))\n"
        )
        self.assertEqual(out["bad_initial"]["code"], "DENIED_APPROVAL_BODY_INVALID")
        self.assertEqual(out["wrong"]["code"], "DENIED_APPROVAL_REVISION_MISMATCH")
        self.assertEqual(out["wrong"]["current_revision"], 1)
        self.assertEqual(out["refreshed"]["current_revision"], 2)
        self.assertFalse(out["refreshed"]["deduplicated"])
        self.assertTrue(out["replayed"]["deduplicated"])
        self.assertEqual(out["recovered"]["active"]["revision"], 2)
        self.assertEqual(
            out["recovered"]["active"]["body"]["expires_at"],
            out["first"]["active"]["body"]["expires_at"],
        )
        self.assertEqual(out["old_accept"]["code"], "DENIED_APPROVAL_REVISION_MISMATCH")
        self.assertEqual(out["old_accept"]["current_revision"], 2)
        self.assertEqual(out["accepted"]["status"], "OK")
        self.assertEqual(out["rows"], 1)
        self.assertEqual([event[0] for event in out["events"]], ["open", "open", "resolved"])
        self.assertRegex(out["events"][1][1], r"^approval:apr_.+:open:2$")
        self.assertEqual(len(out["open_bodies"]), 2)
        self.assertEqual(out["open_bodies"][0]["approval_revision"], 1)
        self.assertEqual(out["open_bodies"][1]["approval_revision"], 2)
        self.assertEqual(out["open_bodies"][0]["expires_at"], out["open_bodies"][1]["expires_at"])
        self.assertEqual(out["released"], ["approved"])

    def test_concurrent_identical_refresh_converges_to_one_next_revision(self) -> None:
        """Converge identical concurrent refreshes onto one next revision."""
        sid, run_id = self._session_run()
        out = self._run(
            "import json, threading\n"
            "from datetime import datetime, timedelta, timezone\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.approvals import ApprovalBroker, canonical_snapshot_set_sha256\n"
            f"sid={sid!r}; rid={run_id!r}\n"
            "expires=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat().replace('+00:00','Z')\n"
            "def make(rev,digest):\n"
            "    side={'artifact_ref':'sha256:'+digest,'sha256':digest,'bytes':3,'encoding':'utf-8','media_type':None}\n"
            "    value={'approval_revision':rev,'expires_at':expires,'snapshot_set_sha256':'0'*64,'file_changes':[{'change_id':'c1','path':'src/a.txt','kind':'modify','old_path':None,'preview_mode':'text','preview_reason':None,'before':side,'proposed':side}]}\n"
            "    value['snapshot_set_sha256']=canonical_snapshot_set_sha256(value); return value\n"
            "released=[]; broker=ApprovalBroker(lambda s,r,c,d: released.append(d) is None)\n"
            "v1=make(1,'1'*64); opened=broker.open(session_id=sid,agent_run_id=rid,correlation_id='same-refresh',body=v1,negotiated=True,is_test=True)\n"
            "v2=make(2,'2'*64); gate=threading.Barrier(3); results=[]; lock=threading.Lock()\n"
            "def refresh():\n"
            "    gate.wait(); result=broker.refresh(opened['id'],rid,v2)\n"
            "    with lock: results.append(result)\n"
            "threads=[threading.Thread(target=refresh),threading.Thread(target=refresh)]\n"
            "[thread.start() for thread in threads]; gate.wait(); [thread.join() for thread in threads]\n"
            "active=broker.get(opened['id'])['active']; old=broker.resolve(opened['id'],rid,'approve',expected_revision=1,snapshot_set_sha256=v1['snapshot_set_sha256'])\n"
            "accepted=broker.resolve(opened['id'],rid,'approve',expected_revision=2,snapshot_set_sha256=v2['snapshot_set_sha256'])\n"
            "events=db.fetch_all('SELECT marker,source_event_id,body FROM agent_events WHERE agent_run_id=? AND correlation_id=? ORDER BY sequence_no',(rid,'same-refresh'))\n"
            "print('RESULT '+json.dumps({'results':results,'active':active,'old':old,'accepted':accepted,'events':events,'released':released,'v2_hash':v2['snapshot_set_sha256']}))\n"
        )
        self.assertEqual([result["status"] for result in out["results"]], ["OK", "OK"])
        self.assertEqual(sorted(result["deduplicated"] for result in out["results"]), [False, True])
        self.assertEqual({result["current_revision"] for result in out["results"]}, {2})
        self.assertEqual(
            {result["current_snapshot_set_sha256"] for result in out["results"]},
            {out["v2_hash"]},
        )
        self.assertEqual(out["active"]["revision"], 2)
        self.assertEqual(out["active"]["body"]["snapshot_set_sha256"], out["v2_hash"])
        self.assertEqual(out["old"]["code"], "DENIED_APPROVAL_REVISION_MISMATCH")
        self.assertEqual(out["accepted"]["status"], "OK")
        self.assertEqual([event[0] for event in out["events"]], ["open", "open", "resolved"])
        self.assertEqual(sum(event[1].endswith(":open:2") for event in out["events"]), 1)
        self.assertEqual(out["released"], ["approved"])

    def test_concurrent_competing_refresh_keeps_one_current_second_revision(self) -> None:
        """Keep one current revision when competing refreshes race."""
        sid, run_id = self._session_run()
        out = self._run(
            "import json, threading\n"
            "from datetime import datetime, timedelta, timezone\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.approvals import ApprovalBroker, canonical_snapshot_set_sha256\n"
            f"sid={sid!r}; rid={run_id!r}\n"
            "expires=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat().replace('+00:00','Z')\n"
            "def make(rev,digest):\n"
            "    side={'artifact_ref':'sha256:'+digest,'sha256':digest,'bytes':3,'encoding':'utf-8','media_type':None}\n"
            "    value={'approval_revision':rev,'expires_at':expires,'snapshot_set_sha256':'0'*64,'file_changes':[{'change_id':'c1','path':'src/a.txt','kind':'modify','old_path':None,'preview_mode':'text','preview_reason':None,'before':side,'proposed':side}]}\n"
            "    value['snapshot_set_sha256']=canonical_snapshot_set_sha256(value); return value\n"
            "released=[]; broker=ApprovalBroker(lambda s,r,c,d: released.append(d) is None)\n"
            "v1=make(1,'1'*64); a=make(2,'2'*64); b=make(2,'3'*64)\n"
            "opened=broker.open(session_id=sid,agent_run_id=rid,correlation_id='competing-refresh',body=v1,negotiated=True,is_test=True)\n"
            "gate=threading.Barrier(3); results=[]; lock=threading.Lock()\n"
            "def refresh(label,body):\n"
            "    gate.wait(); result=broker.refresh(opened['id'],rid,body)\n"
            "    with lock: results.append([label,result])\n"
            "threads=[threading.Thread(target=refresh,args=('a',a)),threading.Thread(target=refresh,args=('b',b))]\n"
            "[thread.start() for thread in threads]; gate.wait(); [thread.join() for thread in threads]\n"
            "active=broker.get(opened['id'])['active']; current_hash=active['body']['snapshot_set_sha256']; losing_hash=b['snapshot_set_sha256'] if current_hash==a['snapshot_set_sha256'] else a['snapshot_set_sha256']\n"
            "losing_accept=broker.resolve(opened['id'],rid,'approve',expected_revision=2,snapshot_set_sha256=losing_hash)\n"
            "accepted=broker.resolve(opened['id'],rid,'approve',expected_revision=2,snapshot_set_sha256=current_hash)\n"
            "events=db.fetch_all('SELECT marker,source_event_id FROM agent_events WHERE agent_run_id=? AND correlation_id=? ORDER BY sequence_no',(rid,'competing-refresh'))\n"
            "row=db.fetch_one('SELECT state FROM approval_requests WHERE id=?',(opened['id'],))[0]\n"
            "print('RESULT '+json.dumps({'results':results,'active':active,'current_hash':current_hash,'losing_hash':losing_hash,'losing_accept':losing_accept,'accepted':accepted,'events':events,'state':row,'released':released}))\n"
        )
        statuses = sorted(result[1]["status"] for result in out["results"])
        self.assertEqual(statuses, ["DENIED", "OK"])
        denied = next(result[1] for result in out["results"] if result[1]["status"] == "DENIED")
        self.assertEqual(denied["code"], "DENIED_APPROVAL_REVISION_MISMATCH")
        self.assertEqual(denied["current_revision"], 2)
        self.assertEqual(denied["current_snapshot_set_sha256"], out["current_hash"])
        self.assertEqual(out["active"]["revision"], 2)
        self.assertEqual(out["losing_accept"]["code"], "DENIED_APPROVAL_REVISION_MISMATCH")
        self.assertEqual(out["accepted"]["status"], "OK")
        self.assertEqual(out["state"], "approved")
        self.assertEqual([event[0] for event in out["events"]], ["open", "open", "resolved"])
        self.assertEqual(sum(event[1].endswith(":open:2") for event in out["events"]), 1)
        self.assertEqual(out["released"], ["approved"])

    def test_timeout_race_has_one_terminal_winner_and_one_release(self) -> None:
        """Allow exactly one terminal winner and release in a decision-timeout race."""
        sid, run_id = self._session_run()
        out = self._run(
            "import json, threading\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.approvals import ApprovalBroker\n"
            f"sid={sid!r}; rid={run_id!r}\n"
            "released=[]; lock=threading.Lock(); gate=threading.Barrier(2)\n"
            "def release(s,r,c,d):\n"
            "    with lock: released.append(d)\n"
            "    return True\n"
            "broker=ApprovalBroker(release)\n"
            "opened=broker.open(session_id=sid,agent_run_id=rid,correlation_id='race',is_test=True)\n"
            "results=[]\n"
            "def approve():\n"
            "    gate.wait(); results.append(broker.resolve(opened['id'],rid,'approve'))\n"
            "def timeout():\n"
            "    gate.wait(); results.append(broker.timeout(opened['id'],rid))\n"
            "threads=[threading.Thread(target=approve),threading.Thread(target=timeout)]\n"
            "[t.start() for t in threads]; [t.join() for t in threads]\n"
            "row=db.fetch_one('SELECT state FROM approval_requests WHERE id=?',(opened['id'],))\n"
            "events=db.fetch_all(\"SELECT marker FROM agent_events WHERE agent_run_id=? AND correlation_id=? AND source_event_id LIKE '%:terminal'\",(rid,'race'))\n"
            "print('RESULT '+json.dumps({'results':results,'state':row[0],'events':events,'released':released}))\n"
        )
        self.assertIn(out["state"], ("approved", "denied"))
        self.assertEqual(len(out["events"]), 1)
        self.assertIn(out["events"][0][0], ("resolved", "timed_out"))
        self.assertEqual(len(out["released"]), 1)
        expected_approvals = 1 if out["state"] == "approved" else 0
        self.assertEqual(sum(result["status"] == "OK" for result in out["results"]), expected_approvals)
        self.assertEqual(sum(result.get("code") == "DENIED_APPROVAL_ALREADY_DECIDED" for result in out["results"]), 1)

    def test_path_text_side_and_pre_release_fail_closed(self) -> None:
        """Fail closed on path, text, side, and premature-release mismatches."""
        sid, run_id = self._session_run()
        out = self._run(
            "import json\n"
            "from datetime import datetime, timedelta, timezone\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.approvals import ApprovalBroker, canonical_snapshot_set_sha256\n"
            f"sid={sid!r}; rid={run_id!r}\n"
            "expires=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat().replace('+00:00','Z')\n"
            "def body(path,side):\n"
            "    value={'approval_revision':1,'expires_at':expires,'snapshot_set_sha256':'0'*64,'file_changes':[{'change_id':'c1','path':path,'kind':'modify','old_path':None,'preview_mode':'text','preview_reason':None,'before':side,'proposed':side}]}\n"
            "    value['snapshot_set_sha256']=canonical_snapshot_set_sha256(value); return value\n"
            "h='1'*64\n"
            "text_side={'artifact_ref':'sha256:'+h,'sha256':h,'bytes':1,'encoding':'utf-8','media_type':None}\n"
            "media_side={'artifact_ref':'sha256:'+h,'sha256':h,'bytes':1,'encoding':None,'media_type':'text/plain'}\n"
            "released=[]\n"
            "colon=ApprovalBroker(lambda s,r,c,d: released.append(c) is None).open(session_id=sid,agent_run_id=rid,correlation_id='colon',body=body('src/a:b.txt',text_side),negotiated=True,is_test=True)\n"
            "media=ApprovalBroker(lambda s,r,c,d: released.append(c) is None).open(session_id=sid,agent_run_id=rid,correlation_id='media',body=body('src/a.txt',media_side),negotiated=True,is_test=True)\n"
            "calls=[]\n"
            "def explode(payload): calls.append(payload['approval_id']); raise RuntimeError('snapshot read failed')\n"
            "broker=ApprovalBroker(lambda s,r,c,d: released.append(c) is None,explode)\n"
            "valid=body('src/good.txt',text_side)\n"
            "opened=broker.open(session_id=sid,agent_run_id=rid,correlation_id='pre-release',body=valid,negotiated=True,is_test=True)\n"
            "resolved=broker.resolve(opened['id'],rid,'approve',expected_revision=1,snapshot_set_sha256=valid['snapshot_set_sha256'])\n"
            "malformed_broker=ApprovalBroker(lambda s,r,c,d: released.append(c) is None,lambda payload: {'status':'OK'})\n"
            "malformed=malformed_broker.open(session_id=sid,agent_run_id=rid,correlation_id='malformed-validator',body=valid,negotiated=True,is_test=True)\n"
            "malformed_result=malformed_broker.resolve(malformed['id'],rid,'approve',expected_revision=1,snapshot_set_sha256=valid['snapshot_set_sha256'])\n"
            "state=db.fetch_one('SELECT state FROM approval_requests WHERE id=?',(opened['id'],))[0]\n"
            "event=db.fetch_one('SELECT marker,code FROM agent_events WHERE agent_run_id=? AND correlation_id=? AND source_event_id=?',(rid,'pre-release','approval:'+opened['id']+':terminal'))\n"
            "print('RESULT '+json.dumps({'colon':colon,'media':media,'resolved':resolved,'malformed_result':malformed_result,'state':state,'event':event,'released':released,'calls':calls}))\n"
        )
        self.assertEqual(out["colon"]["code"], "DENIED_APPROVAL_SNAPSHOT_INVALID")
        self.assertEqual(out["media"]["code"], "DENIED_APPROVAL_SNAPSHOT_INVALID")
        self.assertEqual(out["resolved"]["status"], "DENIED")
        self.assertEqual(out["resolved"]["code"], "DENIED_APPROVAL_SNAPSHOT_INVALID")
        self.assertFalse(out["resolved"]["retryable"])
        self.assertEqual(out["resolved"]["required_action"], "rerun_turn")
        self.assertEqual(out["malformed_result"]["code"], "DENIED_APPROVAL_SNAPSHOT_INVALID")
        self.assertEqual(out["state"], "denied")
        self.assertEqual(out["event"], ["declined", "DENIED_APPROVAL_SNAPSHOT_INVALID"])
        self.assertEqual(len(out["calls"]), 1)
        self.assertEqual(sorted(out["released"]), ["colon", "malformed-validator", "media", "pre-release"])

    def test_transaction_deduplicates_and_warm_schema_preserves_legacy_duplicates(self) -> None:
        """Deduplicate transactional requests while preserving legacy warm-schema rows."""
        sid, run_id = self._session_run()
        out = self._run(
            "import json\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.approvals import ApprovalBroker\n"
            f"sid={sid!r}; rid={run_id!r}\n"
            "broker=ApprovalBroker()\n"
            "one=broker.open(session_id=sid,agent_run_id=rid,correlation_id='same',is_test=True)\n"
            "two=broker.open(session_id=sid,agent_run_id=rid,correlation_id='same',is_test=True)\n"
            "count=db.fetch_one(\"SELECT COUNT(*) FROM approval_requests WHERE session_id=? AND correlation_id=? AND state='open'\",(sid,'same'))[0]\n"
            "open_events=db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id=? AND correlation_id=? AND marker='open'\",(rid,'same'))[0]\n"
            "stamp=db.now()\n"
            "def seed(conn,attempt):\n"
            "    for aid in ('apr_legacy_dup_1','apr_legacy_dup_2'):\n"
            "        conn.execute(\"INSERT INTO approval_requests (id,created_at,updated_at,session_id,correlation_id,request_type,state,decided_by,rule_id,summary,content_hash,is_test) VALUES (?,?,?,?,?,'tool_approval','open',NULL,NULL,'legacy duplicate',?,1)\",(aid,stamp,stamp,sid,'legacy-dup','hash-'+aid))\n"
            "db.write_tx(seed)\n"
            "warm=db.initialize()\n"
            "legacy_count=db.fetch_one(\"SELECT COUNT(*) FROM approval_requests WHERE session_id=? AND correlation_id=? AND state='open'\",(sid,'legacy-dup'))[0]\n"
            "conn=db.connect(); index_row=conn.execute(\"SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_approval_requests_open_correlation'\").fetchone(); conn.close()\n"
            "print('RESULT '+json.dumps({'one':one,'two':two,'count':count,'open_events':open_events,'warm':warm,'legacy_count':legacy_count,'index_row':index_row}))\n"
        )
        self.assertEqual(out["one"]["id"], out["two"]["id"])
        self.assertFalse(out["one"]["deduplicated"])
        self.assertTrue(out["two"]["deduplicated"])
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["open_events"], 1)
        self.assertTrue(out["warm"]["schema_ok"])
        self.assertEqual(out["legacy_count"], 2)
        self.assertIsNone(out["index_row"])

    def test_negotiated_dedup_requires_same_immutable_manifest(self) -> None:
        """Deduplicate negotiated requests only when immutable manifests match."""
        sid, run_id = self._session_run()
        out = self._run(
            "import json\n"
            "from datetime import datetime, timedelta, timezone\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.approvals import ApprovalBroker, canonical_snapshot_set_sha256\n"
            f"sid={sid!r}; rid={run_id!r}\n"
            "def body(proposed):\n"
            "    before='1'*64; side=lambda h: {'artifact_ref':'sha256:'+h,'sha256':h,'bytes':1,'encoding':'utf-8','media_type':None}\n"
            "    value={'approval_revision':1,'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat().replace('+00:00','Z'),'snapshot_set_sha256':'0'*64,'file_changes':[{'change_id':'c1','path':'a.txt','kind':'modify','old_path':None,'preview_mode':'text','preview_reason':None,'before':side(before),'proposed':side(proposed)}]}\n"
            "    value['snapshot_set_sha256']=canonical_snapshot_set_sha256(value); return value\n"
            "broker=ApprovalBroker()\n"
            "one=broker.open(session_id=sid,agent_run_id=rid,correlation_id='same-negotiated',body=body('2'*64),negotiated=True,is_test=True)\n"
            "same=broker.open(session_id=sid,agent_run_id=rid,correlation_id='same-negotiated',body=body('2'*64),negotiated=True,is_test=True)\n"
            "conflict=broker.open(session_id=sid,agent_run_id=rid,correlation_id='same-negotiated',body=body('3'*64),negotiated=True,is_test=True)\n"
            "state=db.fetch_one('SELECT state FROM approval_requests WHERE id=?',(one['id'],))[0]\n"
            "rows=db.fetch_one(\"SELECT COUNT(*) FROM approval_requests WHERE session_id=? AND correlation_id='same-negotiated'\",(sid,))[0]\n"
            "events=db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id=? AND correlation_id='same-negotiated'\",(rid,))[0]\n"
            "print('RESULT '+json.dumps({'one':one,'same':same,'conflict':conflict,'state':state,'rows':rows,'events':events}))\n"
        )
        self.assertEqual(out["same"]["id"], out["one"]["id"])
        self.assertTrue(out["same"]["deduplicated"])
        self.assertEqual(out["conflict"]["code"], "DENIED_APPROVAL_CONFLICT")
        self.assertFalse(out["conflict"]["retryable"])
        self.assertEqual(out["conflict"]["required_action"], "rerun_turn")
        self.assertFalse(out["conflict"]["waiter_should_park"])
        self.assertEqual(out["state"], "open")
        self.assertEqual(out["rows"], 1)
        self.assertEqual(out["events"], 1)

    def test_terminal_body_presentation_matches_the_actual_decision(self) -> None:
        sid, run_id = self._session_run()
        out = self._run(
            "import json\n"
            "from kaizen_components import db\n"
            "from kaizen_components.denials import KaizenDenied\n"
            "from kaizen_components.orchestration.approvals import ApprovalBroker\n"
            f"sid={sid!r}; rid={run_id!r}; broker=ApprovalBroker()\n"
            "plain=broker.open(session_id=sid,agent_run_id=rid,correlation_id='plain-deny',is_test=True)\n"
            "coded=broker.open(session_id=sid,agent_run_id=rid,correlation_id='coded-deny',is_test=True)\n"
            "approve=broker.open(session_id=sid,agent_run_id=rid,correlation_id='invalid-approve',is_test=True)\n"
            "plain_result=broker.resolve(plain['id'],rid,'deny')\n"
            "coded_result=broker.resolve(coded['id'],rid,'deny',denial_code='DENIED_APPROVAL_TIMEOUT')\n"
            "try:\n"
            "    broker.resolve(approve['id'],rid,'approve',denial_code='DENIED_APPROVAL_TIMEOUT')\n"
            "except KaizenDenied as error:\n"
            "    invalid={'code':error.code,'exit_code':error.exit_code}\n"
            "rows=db.fetch_all(\"SELECT correlation_id,marker,body FROM agent_events WHERE agent_run_id=? AND source_event_id LIKE 'approval:%:terminal' ORDER BY correlation_id\",(rid,))\n"
            "print('RESULT '+json.dumps({'plain':plain_result,'coded':coded_result,'invalid':invalid,'rows':rows}))\n"
        )
        bodies = {correlation: (marker, json.loads(body)) for correlation, marker, body in out["rows"]}
        self.assertEqual(bodies["plain-deny"], ("declined", {"decision": "denied"}))
        self.assertEqual(bodies["coded-deny"], (
            "declined",
            {"decision": "denied", "denial_code": "DENIED_APPROVAL_TIMEOUT", "presentation": "timed_out"},
        ))
        self.assertEqual(out["invalid"], {"code": "DENIED_APPROVAL_DECISION_INVALID", "exit_code": 2})

    def test_same_session_correlation_is_managed_independently_per_run(self) -> None:
        """Manage identical session correlations independently for each run."""
        sid, first_run = self._session_run()
        rc, second = self.kz(
            "T5", "--summary", "Second broker test run.", "--payload-json",
            json.dumps({
                "agent_type": "other", "surface": "vscode-extension", "session_id": sid,
                "engine": "local_llm",
            }),
        )
        self.assertEqual(rc, 0, second)
        second_run = second["id"]
        out = self._run(
            "import json\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.approvals import ApprovalBroker\n"
            f"sid={sid!r}; first={first_run!r}; second={second_run!r}\n"
            "broker=ApprovalBroker()\n"
            "one=broker.open(session_id=sid,agent_run_id=first,correlation_id='cross-run',is_test=True)\n"
            "two=broker.open(session_id=sid,agent_run_id=second,correlation_id='cross-run',is_test=True)\n"
            "one_again=broker.open(session_id=sid,agent_run_id=first,correlation_id='cross-run',is_test=True)\n"
            "two_again=broker.open(session_id=sid,agent_run_id=second,correlation_id='cross-run',is_test=True)\n"
            "rows=db.fetch_all(\"SELECT id,state FROM approval_requests WHERE session_id=? AND correlation_id='cross-run' ORDER BY id\",(sid,))\n"
            "events=[db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id=? AND correlation_id='cross-run' AND marker='open'\",(rid,))[0] for rid in (first,second)]\n"
            "managed=[broker.get(one['id'])['active']['agent_run_id'],broker.get(two['id'])['active']['agent_run_id']]\n"
            "resolved=broker.resolve(one['id'],first,'approve'); states=db.fetch_all(\"SELECT id,state FROM approval_requests WHERE session_id=? AND correlation_id='cross-run' ORDER BY id\",(sid,))\n"
            "print('RESULT '+json.dumps({'one':one,'two':two,'one_again':one_again,'two_again':two_again,'rows':rows,'events':events,'managed':managed,'resolved':resolved,'states':states}))\n"
        )
        self.assertNotEqual(out["one"]["id"], out["two"]["id"])
        self.assertFalse(out["one"]["deduplicated"])
        self.assertFalse(out["two"]["deduplicated"])
        self.assertEqual(out["one_again"]["id"], out["one"]["id"])
        self.assertEqual(out["two_again"]["id"], out["two"]["id"])
        self.assertTrue(out["one_again"]["deduplicated"])
        self.assertTrue(out["two_again"]["deduplicated"])
        self.assertEqual(len(out["rows"]), 2)
        self.assertEqual(out["events"], [1, 1])
        self.assertEqual(out["managed"], [first_run, second_run])
        self.assertEqual(out["resolved"]["status"], "OK")
        states = dict(out["states"])
        self.assertEqual(states[out["one"]["id"]], "approved")
        self.assertEqual(states[out["two"]["id"]], "open")

    def test_unmanaged_same_correlation_never_captures_broker_waiter(self) -> None:
        """Keep unmanaged matching correlations from capturing broker waiters."""
        sid, run_id = self._session_run()
        rc, legacy = self.kz(
            "C4", "--session-id", sid, "--summary", "Legacy direct approval.",
            "--payload-json", json.dumps({"request_type": "tool_approval", "correlation_id": "shared"}),
        )
        self.assertEqual(rc, 0, legacy)
        out = self._run(
            "import json\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.approvals import ApprovalBroker\n"
            f"sid={sid!r}; rid={run_id!r}; legacy_id={legacy['id']!r}\n"
            "released=[]; broker=ApprovalBroker(lambda s,r,c,d: released.append([c,d]) is None)\n"
            "opened=broker.open(session_id=sid,agent_run_id=rid,correlation_id='shared',is_test=True)\n"
            "before=db.fetch_one(\"SELECT COUNT(*) FROM approval_requests WHERE session_id=? AND correlation_id='shared' AND state='open'\",(sid,))[0]\n"
            "legacy_info=broker.get(legacy_id); managed_info=broker.get(opened['id'])\n"
            "resolved=broker.resolve(opened['id'],rid,'approve')\n"
            "states=db.fetch_all(\"SELECT id,state FROM approval_requests WHERE session_id=? AND correlation_id='shared' ORDER BY id\",(sid,))\n"
            "events=db.fetch_all(\"SELECT source_event_id,marker FROM agent_events WHERE agent_run_id=? AND correlation_id='shared' ORDER BY sequence_no\",(rid,))\n"
            "print('RESULT '+json.dumps({'opened':opened,'before':before,'legacy_info':legacy_info,'managed_info':managed_info,'resolved':resolved,'states':states,'events':events,'released':released}))\n"
        )
        self.assertNotEqual(out["opened"]["id"], legacy["id"])
        self.assertFalse(out["opened"]["deduplicated"])
        self.assertEqual(out["before"], 2)
        self.assertFalse(out["legacy_info"]["broker_managed"])
        self.assertTrue(out["managed_info"]["broker_managed"])
        self.assertEqual(out["resolved"]["status"], "OK")
        self.assertEqual(dict(out["states"])[legacy["id"]], "open")
        self.assertEqual(dict(out["states"])[out["opened"]["id"]], "approved")
        self.assertEqual([event[1] for event in out["events"]], ["open", "resolved"])
        self.assertTrue(all(event[0].startswith(f"approval:{out['opened']['id']}:") for event in out["events"]))
        self.assertEqual(out["released"], [["shared", "approved"]])

    def test_reconcile_closes_only_broker_managed_orphans(self) -> None:
        """Close only broker-managed orphan approvals during reconciliation."""
        sid, run_id = self._session_run()
        rc, unmanaged = self.kz(
            "C4", "--session-id", sid, "--summary", "Legacy unmanaged.",
            "--payload-json", json.dumps({"request_type": "tool_approval", "correlation_id": "unmanaged"}),
        )
        self.assertEqual(rc, 0, unmanaged)
        out = self._run(
            "import json\n"
            "from datetime import datetime, timedelta, timezone\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.approvals import ApprovalBroker, canonical_snapshot_set_sha256\n"
            f"sid={sid!r}; rid={run_id!r}; unmanaged={unmanaged['id']!r}\n"
            "released=[]\n"
            "broker=ApprovalBroker(lambda s,r,c,d: released.append([c,d]) is None)\n"
            "h='1'*64; side={'artifact_ref':'sha256:'+h,'sha256':h,'bytes':1,'encoding':'utf-8','media_type':None}\n"
            "expired={'approval_revision':1,'expires_at':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat().replace('+00:00','Z'),'snapshot_set_sha256':'0'*64,'file_changes':[{'change_id':'c1','path':'a.txt','kind':'modify','old_path':None,'preview_mode':'text','preview_reason':None,'before':side,'proposed':side}]}\n"
            "expired['snapshot_set_sha256']=canonical_snapshot_set_sha256(expired)\n"
            "old=broker.open(session_id=sid,agent_run_id=rid,correlation_id='expired',body=expired,negotiated=True,is_test=True)\n"
            "fresh=broker.open(session_id=sid,agent_run_id=rid,correlation_id='fresh',is_test=True)\n"
            "active=broker.get(old['id'])['active']; active['body']['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat().replace('+00:00','Z')\n"
            "db.write_tx(lambda conn,attempt: conn.execute('UPDATE agent_events SET body=? WHERE agent_run_id=? AND source_event_id=?',(json.dumps(active['body'],sort_keys=True,separators=(',',':')),rid,active['source_event_id'])))\n"
            "result=broker.reconcile_orphans(set())\n"
            "states=db.fetch_all('SELECT correlation_id,state FROM approval_requests WHERE session_id=? ORDER BY correlation_id',(sid,))\n"
            "markers=db.fetch_all(\"SELECT correlation_id,marker,code FROM agent_events WHERE agent_run_id=? AND source_event_id LIKE 'approval:%:terminal' ORDER BY correlation_id\",(rid,))\n"
            "print('RESULT '+json.dumps({'result':result,'states':states,'markers':markers,'released':released,'unmanaged':unmanaged,'old':old['id'],'fresh':fresh['id']}))\n"
        )
        self.assertEqual(out["result"]["managed"], 2)
        self.assertEqual(dict(out["states"]), {"expired": "denied", "fresh": "denied", "unmanaged": "open"})
        marker_map = {row[0]: row[1:] for row in out["markers"]}
        self.assertEqual(marker_map["expired"], ["timed_out", "DENIED_APPROVAL_TIMEOUT"])
        self.assertEqual(marker_map["fresh"], ["declined", "DENIED_APPROVAL_STALE_RERUN_REQUIRED"])
        stale = next(item for item in out["result"]["outcomes"] if item["id"] == out["fresh"])
        self.assertEqual(stale["result"]["status"], "DENIED")
        self.assertEqual(stale["result"]["code"], "DENIED_APPROVAL_STALE_RERUN_REQUIRED")
        self.assertFalse(stale["result"]["retryable"])
        self.assertNotIn("unmanaged", marker_map)
        self.assertEqual(sorted(out["released"]), [["expired", "denied"], ["fresh", "denied"]])


if __name__ == "__main__":
    import unittest

    unittest.main()
