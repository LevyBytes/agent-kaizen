"""D3 negotiated approval integration: supervisor refresh/release and local post-apply audit."""

from __future__ import annotations

import json
import unittest

from test_session_drive import _DrivenSubprocess
from kaizen_components.orchestration.apply_evidence import decode_mismatch_path


class NegotiatedDiffSupervisorTest(_DrivenSubprocess):
    def test_negotiated_accept_temporal_order_is_writer_validate_persist_release_apply_audit(self) -> None:
        out = self.drive(
            "import threading\n"
            "from pathlib import Path\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration import proposal_executor as PE\n"
            "timeline=[]; timeline_lock=threading.RLock(); state={}\n"
            "def mark(value):\n"
            "    with timeline_lock: timeline.append(value)\n"
            "BaseExecutor=L.WorkspaceProposalExecutor\n"
            "class TracingExecutor(BaseExecutor):\n"
            "    def _apply(self, approved, proposal_sha256, baselines, **kwargs):\n"
            "        self._trace_base_checks=0; mark('apply_enter')\n"
            "        result=super()._apply(approved, proposal_sha256, baselines, **kwargs)\n"
            "        mark('apply_complete'); return result\n"
            "    def _base_mismatches(self, changes, baselines):\n"
            "        self._trace_base_checks += 1\n"
            "        mark('apply_base_rehash_' + str(self._trace_base_checks))\n"
            "        return super()._base_mismatches(changes, baselines)\n"
            "    def _final_mismatches(self, changes, baselines):\n"
            "        mark('executor_target_audit')\n"
            "        return super()._final_mismatches(changes, baselines)\n"
            "    def _write_staged(self, item, content):\n"
            "        mark('stage_proposed' if item.promote else 'stage_backup')\n"
            "        return super()._write_staged(item, content)\n"
            "L.WorkspaceProposalExecutor=TracingExecutor\n"
            "root=None; target=None\n"
            "provider=scripted_provider([tool_reply('kaizen_propose_changes', changes=[{'kind':'modify',"
            "'path':'AI/work/accept-order.txt','content':'new'}]), final_reply('done')])\n"
            "L.default_chat_provider=lambda: provider\n"
            "sup=Supervisor(); sup.boot(); root=Path(sup.repo_root); target=root/'AI'/'work'/'accept-order.txt'\n"
            "target.parent.mkdir(parents=True,exist_ok=True); target.write_text('old',encoding='utf-8')\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'edit',"
            "'profile':{'model':'fixture','permission_mode':'ask'},'client_features':{'diff_snapshots':True}}})\n"
            "rid,sid=start['agent_run_id'],start['session_id']; corr=wait_open_approval(sup,rid,budget=20.0)\n"
            "row=db.fetch_one('SELECT id FROM approval_requests WHERE session_id=? AND correlation_id=? ORDER BY created_at LIMIT 1',(sid,corr)) if corr else None\n"
            "aid=row[0] if row else None; active=sup._approval_broker.get(aid)['active']['body'] if aid else None\n"
            "session=sup._get_driven(rid)\n"
            "if session is None:\n"
            "    events=db.fetch_all('SELECT event_kind,marker,code,summary FROM agent_events WHERE agent_run_id=? ORDER BY sequence_no',(rid,))\n"
            "    raise AssertionError(json.dumps({'start':start,'corr':corr,'events':events}))\n"
            "parked=wait_waiter_parked(sup,rid,corr,budget=20.0)\n"
            "original_admit=sup._ensure_diff_writer_claim\n"
            "def traced_admit(current):\n"
            "    result=original_admit(current)\n"
            "    state['writer_admitted']=result is None and current.writer_claim_token is not None\n"
            "    mark('writer_admitted'); return result\n"
            "sup._ensure_diff_writer_claim=traced_admit\n"
            "original_validate=sup._diff_snapshots.validate_release\n"
            "def traced_validate(payload):\n"
            "    current=sup._get_driven(rid); token=current.writer_claim_token if current else None\n"
            "    state['writer_verified_before_validate']=token is not None and sup._writer_lease.verify(token,agent_run_id=rid) is None\n"
            "    mark('snapshot_base_validate'); return original_validate(payload)\n"
            "sup._diff_snapshots.validate_release=traced_validate\n"
            "original_audit=sup._diff_snapshots.audit\n"
            "def traced_audit(current_approval_id):\n"
            "    mark('snapshot_post_apply_audit'); return original_audit(current_approval_id)\n"
            "sup._diff_snapshots.audit=traced_audit\n"
            "original_release=session.resolve_waiter\n"
            "def traced_release(current_corr, decision):\n"
            "    with timeline_lock:\n"
            "        approval_state=db.fetch_one('SELECT state FROM approval_requests WHERE id=?',(aid,))[0]\n"
            "        resolved_events=db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id=? AND event_kind='approval' AND marker='resolved'\",(rid,))[0]\n"
            "        state['approval_state_before_release']=approval_state\n"
            "        state['resolved_events_before_release']=resolved_events\n"
            "        timeline.append('persistence_observed'); timeline.append('waiter_release')\n"
            "        released=original_release(current_corr,decision)\n"
            "        state['exact_waiter_released']=released; timeline.append('waiter_released')\n"
            "        return released\n"
            "session.resolve_waiter=traced_release\n"
            "approved=sup._handle_control({'op':'approve','args':{'approval_id':aid,'decision':'approve',"
            "'expected_revision':1,'snapshot_set_sha256':active['snapshot_set_sha256']}}) if aid else {}\n"
            "wait_idle(sup,rid,budget=20.0)\n"
            "written=target.read_text(encoding='utf-8'); close=sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}})\n"
            "sup.shutdown(); out={'start':start,'corr':corr,'parked':parked,'approved':approved,'state':state,"
            "'timeline':timeline,'written':written,'close':close}\n"
        )
        self.assertEqual(out["start"]["status"], "OK", out)
        self.assertIsNotNone(out["corr"], out)
        self.assertTrue(out["parked"], out)
        self.assertEqual(out["approved"]["status"], "OK", out)
        self.assertTrue(out["approved"]["waiter_released"], out)
        self.assertTrue(out["state"]["writer_admitted"], out)
        self.assertTrue(out["state"]["writer_verified_before_validate"], out)
        self.assertEqual(out["state"]["approval_state_before_release"], "approved", out)
        self.assertEqual(out["state"]["resolved_events_before_release"], 1, out)
        self.assertTrue(out["state"]["exact_waiter_released"], out)
        self.assertEqual(out["timeline"], [
            "writer_admitted",
            "snapshot_base_validate",
            "persistence_observed",
            "waiter_release",
            "waiter_released",
            "apply_enter",
            "apply_base_rehash_1",
            "stage_proposed",
            "stage_backup",
            "apply_base_rehash_2",
            "executor_target_audit",
            "apply_complete",
            "snapshot_post_apply_audit",
        ], out)
        self.assertEqual(out["written"], "new", out)
        self.assertEqual(out["close"]["status"], "OK", out)

    def test_clean_stale_refresh_second_accept_releases_rebased_input(self) -> None:
        out = self.drive(
            "from pathlib import Path\n"
            "from kaizen_components import db\n"
            "sup=Supervisor(); sup.boot(); sup._driven_test_records=True\n"
            "root=Path(sup.repo_root); target=root/'AI'/'work'/'a.txt'; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(b'a\\nold\\nz\\n')\n"
            "def write_file(args):\n"
            "    path=root/str(args['path']); path.write_bytes(str(args['content']).encode('utf-8')); return 'wrote'\n"
            "tools={'write_file':L.ToolSpec('write_file','file_write','write exact UTF-8',write_file,arg_hints=('path','content'))}\n"
            "provider=scripted_provider([tool_reply('write_file',path='AI/work/a.txt',content='a\\nnew\\nz\\n'),final_reply('done')])\n"
            "install_factory(sup,provider,tools=tools)\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'edit',"
            "'client_features':{'diff_snapshots':True}}})\n"
            "rid,sid=start['agent_run_id'],start['session_id']; corr=wait_open_approval(sup,rid,budget=20.0)\n"
            "row=db.fetch_one('SELECT id FROM approval_requests WHERE session_id=? AND correlation_id=? ORDER BY created_at LIMIT 1',(sid,corr)) if corr else None; aid=row[0] if row else None\n"
            "v1=sup._approval_broker.get(aid)['active']['body'] if aid else None\n"
            "target.write_bytes(b'external\\na\\nold\\nz\\n')\n"
            "first=sup._handle_control({'op':'approve','args':{'approval_id':aid,'decision':'approve',"
            "'expected_revision':1,'snapshot_set_sha256':v1['snapshot_set_sha256']}}) if aid else {}\n"
            "v2=sup._approval_broker.get(aid)['active']['body'] if aid else None\n"
            "session=sup._get_driven(rid); release_state={}\n"
            "original_release=session.resolve_waiter if session else None\n"
            "def traced_release(current_corr, decision):\n"
            "    release_state['approval_state_before_release']=db.fetch_one('SELECT state FROM approval_requests WHERE id=?',(aid,))[0]\n"
            "    release_state['resolved_events_before_release']=db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id=? AND event_kind='approval' AND marker='resolved'\",(rid,))[0]\n"
            "    release_state['released_revision']=getattr(decision,'approval_revision',None)\n"
            "    return original_release(current_corr,decision)\n"
            "if session: session.resolve_waiter=traced_release\n"
            "second=sup._handle_control({'op':'approve','args':{'approval_id':aid,'decision':'approve',"
            "'expected_revision':2,'snapshot_set_sha256':v2['snapshot_set_sha256']}}) if aid else {}\n"
            "wait_idle(sup,rid,budget=20.0)\n"
            "events=db.fetch_all('SELECT event_kind,marker,code,body FROM agent_events WHERE agent_run_id=? ORDER BY sequence_no',(rid,))\n"
            "approval_state=db.fetch_one('SELECT state FROM approval_requests WHERE id=?',(aid,))[0] if aid else None\n"
            "written=target.read_text(encoding='utf-8') if target.exists() else None\n"
            "close=sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}})\n"
            "sup.shutdown()\n"
            "out={'start':start,'corr':corr,'aid':aid,'v1':v1,'first':first,'v2':v2,'second':second,"
            "'state':approval_state,'release_state':release_state,'written':written,'close':close,'events':events}\n"
        )
        self.assertEqual(out["start"]["status"], "OK", out)
        self.assertIsNotNone(out["corr"], out)
        self.assertEqual(out["first"].get("code"), "DENIED_APPROVAL_REVISION_MISMATCH", out)
        self.assertEqual(out["first"]["current_revision"], 2)
        self.assertEqual(out["v2"]["approval_revision"], 2)
        self.assertEqual(out["v1"]["expires_at"], out["v2"]["expires_at"])
        self.assertEqual(out["second"]["status"], "OK", out)
        self.assertTrue(out["second"]["waiter_released"])
        self.assertEqual(out["state"], "approved")
        self.assertEqual(out["release_state"], {
            "approval_state_before_release": "approved",
            "resolved_events_before_release": 1,
            "released_revision": 2,
        }, out)
        self.assertEqual(out["written"], "external\na\nnew\nz\n", out)
        approval_markers = [event[1] for event in out["events"] if event[0] == "approval"]
        self.assertEqual(approval_markers, ["open", "open", "resolved"])
        verification = [event for event in out["events"] if event[0] == "verification"]
        self.assertEqual(len(verification), 1, out)
        self.assertFalse(json.loads(verification[0][3])["partial_apply"])
        self.assertEqual(out["close"]["status"], "OK")

    def test_gateway_proposal_clean_stale_revision_two_applies_the_approved_rebased_bytes(self) -> None:
        out = self.drive(
            "from pathlib import Path\n"
            "from kaizen_components import db\n"
            "provider=scripted_provider([tool_reply('kaizen_propose_changes',summary='rebased request',"
            "changes=[{'kind':'modify','path':'AI/work/gateway-rebase.txt','content':'a\\nnew\\nz\\n'}]),"
            "final_reply('done')])\n"
            "L.default_chat_provider=lambda: provider\n"
            "sup=Supervisor(); sup.boot(); root=Path(sup.repo_root); target=root/'AI'/'work'/'gateway-rebase.txt'\n"
            "target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(b'a\\nold\\nz\\n')\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'edit',"
            "'profile':{'model':'fixture','permission_mode':'ask'},'client_features':{'diff_snapshots':True}}})\n"
            "rid,sid=start['agent_run_id'],start['session_id']; corr=wait_open_approval(sup,rid,budget=20.0)\n"
            "row=db.fetch_one('SELECT id FROM approval_requests WHERE session_id=? AND correlation_id=? ORDER BY created_at LIMIT 1',(sid,corr)) if corr else None; aid=row[0] if row else None\n"
            "v1=sup._approval_broker.get(aid)['active']['body'] if aid else None\n"
            "target.write_bytes(b'external\\na\\nold\\nz\\n')\n"
            "first=sup._handle_control({'op':'approve','args':{'approval_id':aid,'decision':'approve',"
            "'expected_revision':1,'snapshot_set_sha256':v1['snapshot_set_sha256']}}) if aid else {}\n"
            "v2=sup._approval_broker.get(aid)['active']['body'] if aid else None\n"
            "v2_text=sup._diff_snapshots.read_artifact(v2['file_changes'][0]['proposed']).decode('utf-8') if v2 else None\n"
            "second=sup._handle_control({'op':'approve','args':{'approval_id':aid,'decision':'approve',"
            "'expected_revision':2,'snapshot_set_sha256':v2['snapshot_set_sha256']}}) if aid else {}\n"
            "wait_idle(sup,rid,budget=20.0)\n"
            "events=db.fetch_all('SELECT event_kind,marker,code,body FROM agent_events WHERE agent_run_id=? ORDER BY sequence_no',(rid,))\n"
            "state=db.fetch_one('SELECT state FROM approval_requests WHERE id=?',(aid,))[0] if aid else None\n"
            "written=target.read_text(encoding='utf-8') if target.exists() else None\n"
            "close=sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}}); sup.shutdown()\n"
            "out={'start':start,'corr':corr,'aid':aid,'v1':v1,'first':first,'v2':v2,'v2_text':v2_text,"
            "'second':second,'state':state,'written':written,'close':close,'events':events}\n"
        )
        self.assertEqual(out["start"]["status"], "OK", out)
        self.assertIsNotNone(out["corr"], out)
        self.assertEqual(out["first"].get("code"), "DENIED_APPROVAL_REVISION_MISMATCH", out)
        self.assertEqual(out["v2"]["approval_revision"], 2, out)
        self.assertEqual(out["v2_text"], "external\na\nnew\nz\n", out)
        self.assertEqual(out["second"]["status"], "OK", out)
        self.assertTrue(out["second"]["waiter_released"], out)
        self.assertEqual(out["state"], "approved", out)
        self.assertEqual(out["written"], "external\na\nnew\nz\n", out)
        self.assertEqual(
            [event[1] for event in out["events"] if event[0] == "approval"],
            ["open", "open", "resolved"],
            out,
        )
        tool_closes = [event for event in out["events"] if event[0] == "tool_call" and event[1] == "close_ok"]
        self.assertEqual(len(tool_closes), 1, out)
        tool_result = json.loads(tool_closes[0][3])["result"]
        self.assertTrue(tool_result["applied"], out)
        self.assertFalse(tool_result["partial_apply"], out)
        self.assertEqual(tool_result["executor_status"], "OK", out)
        self.assertEqual(out["close"]["status"], "OK")

    def test_supervisor_persists_exact_partial_apply_truth_and_terminalizes_without_continuation(self) -> None:
        out = self.drive(
            "from pathlib import Path\n"
            "from unittest import mock\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration import proposal_executor as PE\n"
            "provider=scripted_provider([tool_reply('kaizen_propose_changes',summary='partial request',"
            "changes=[{'kind':'create','path':'AI/work/partial-one.txt','content':'one'},"
            "{'kind':'create','path':'AI/work/partial two.txt','content':'two'}]),final_reply('must not run')])\n"
            "L.default_chat_provider=lambda: provider\n"
            "sup=Supervisor(); sup.boot(); root=Path(sup.repo_root)\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'edit',"
            "'profile':{'model':'fixture','permission_mode':'ask'},'client_features':{'diff_snapshots':True}}})\n"
            "rid,sid=start['agent_run_id'],start['session_id']; corr=wait_open_approval(sup,rid,budget=20.0)\n"
            "row=db.fetch_one('SELECT id FROM approval_requests WHERE session_id=? AND correlation_id=? ORDER BY created_at LIMIT 1',(sid,corr)) if corr else None; aid=row[0] if row else None\n"
            "active=sup._approval_broker.get(aid)['active']['body'] if aid else None\n"
            "original_replace=PE.WorkspacePathAuthority.replace_from_exact\n"
            "def fail_second(authority,staged_relative,target_relative,**kwargs):\n"
            "    if target_relative=='AI/work/partial two.txt': raise PE.WorkspacePathError('simulated second-operation failure')\n"
            "    return original_replace(authority,staged_relative,target_relative,**kwargs)\n"
            "with mock.patch.object(PE.WorkspacePathAuthority,'replace_from_exact',new=fail_second):\n"
            "    approved=sup._handle_control({'op':'approve','args':{'approval_id':aid,'decision':'approve',"
            "'expected_revision':1,'snapshot_set_sha256':active['snapshot_set_sha256']}}) if aid else {}\n"
            "    terminal=wait_terminal(sup,rid,budget=20.0)\n"
            "events=db.fetch_all('SELECT event_kind,marker,code,body FROM agent_events WHERE agent_run_id=? ORDER BY sequence_no',(rid,))\n"
            "tool_closes=[event for event in events if event[0]=='tool_call' and event[1]=='close_fail']\n"
            "result=json.loads(tool_closes[0][3])['result'] if tool_closes else None\n"
            "out={'start':start,'approved':approved,'terminal':terminal,'events':events,'result':result,"
            "'one':(root/'AI/work/partial-one.txt').read_text(encoding='utf-8') if (root/'AI/work/partial-one.txt').exists() else None,"
            "'two_exists':(root/'AI/work/partial two.txt').exists(),'provider_calls':provider.state['i'],"
            "'recovery_required':sup._writer_lease.recovery_required}\n"
            "sup.shutdown()\n"
        )

        self.assertEqual(out["start"]["status"], "OK", out)
        self.assertEqual(out["approved"]["status"], "OK", out)
        self.assertTrue(out["terminal"]["terminal"], out)
        self.assertEqual(out["one"], "one", out)
        self.assertFalse(out["two_exists"], out)
        self.assertEqual(out["provider_calls"], 1, out)
        self.assertTrue(out["recovery_required"], out)
        result = out["result"]
        self.assertTrue(result["partial_apply"], out)
        self.assertEqual(result["mismatch_count"], 1, out)
        self.assertTrue(result["mismatch_evidence_complete"], out)
        self.assertFalse(result["mismatch_evidence_uncertain"], out)
        self.assertEqual(result["mismatches"][0]["reason"], "final_state_mismatch", out)
        self.assertEqual(
            decode_mismatch_path(result["mismatches"][0]["path"]),
            "AI/work/partial two.txt",
            out,
        )

    def test_partial_apply_recorder_rejection_enters_recovery_and_never_continues(self) -> None:
        out = self.drive(
            "from pathlib import Path\n"
            "from unittest import mock\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration import proposal_executor as PE\n"
            "provider=scripted_provider([tool_reply('kaizen_propose_changes',summary='partial request',"
            "changes=[{'kind':'create','path':'AI/work/reject-one.txt','content':'one'},"
            "{'kind':'create','path':'AI/work/reject-two.txt','content':'two'}]),final_reply('must not run')])\n"
            "L.default_chat_provider=lambda: provider\n"
            "sup=Supervisor(); sup.boot(); root=Path(sup.repo_root)\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'edit',"
            "'profile':{'model':'fixture','permission_mode':'ask'},'client_features':{'diff_snapshots':True}}})\n"
            "rid,sid=start['agent_run_id'],start['session_id']; corr=wait_open_approval(sup,rid,budget=20.0)\n"
            "row=db.fetch_one('SELECT id FROM approval_requests WHERE session_id=? AND correlation_id=? ORDER BY created_at LIMIT 1',(sid,corr)) if corr else None; aid=row[0] if row else None\n"
            "active=sup._approval_broker.get(aid)['active']['body'] if aid else None\n"
            "original_funnel=sup.funnel_event\n"
            "def reject_close(run_id,event_kind,marker,**kwargs):\n"
            "    if event_kind=='tool_call' and marker=='close_fail': raise RuntimeError('durable close rejected')\n"
            "    return original_funnel(run_id,event_kind,marker,**kwargs)\n"
            "sup.funnel_event=reject_close\n"
            "original_replace=PE.WorkspacePathAuthority.replace_from_exact\n"
            "def fail_second(authority,staged_relative,target_relative,**kwargs):\n"
            "    if target_relative=='AI/work/reject-two.txt': raise PE.WorkspacePathError('simulated second-operation failure')\n"
            "    return original_replace(authority,staged_relative,target_relative,**kwargs)\n"
            "with mock.patch.object(PE.WorkspacePathAuthority,'replace_from_exact',new=fail_second):\n"
            "    approved=sup._handle_control({'op':'approve','args':{'approval_id':aid,'decision':'approve',"
            "'expected_revision':1,'snapshot_set_sha256':active['snapshot_set_sha256']}}) if aid else {}\n"
            "    terminal=wait_terminal(sup,rid,budget=20.0)\n"
            "events=db.fetch_all('SELECT event_kind,marker,code,body FROM agent_events WHERE agent_run_id=? ORDER BY sequence_no',(rid,))\n"
            "out={'start':start,'approved':approved,'terminal':terminal,'events':events,"
            "'one':(root/'AI/work/reject-one.txt').read_text(encoding='utf-8') if (root/'AI/work/reject-one.txt').exists() else None,"
            "'two_exists':(root/'AI/work/reject-two.txt').exists(),'provider_calls':provider.state['i'],"
            "'recovery_required':sup._writer_lease.recovery_required}\n"
            "sup.shutdown()\n"
        )

        self.assertEqual(out["start"]["status"], "OK", out)
        self.assertEqual(out["approved"]["status"], "OK", out)
        self.assertTrue(out["terminal"]["terminal"], out)
        self.assertEqual(out["one"], "one", out)
        self.assertFalse(out["two_exists"], out)
        self.assertEqual(out["provider_calls"], 1, out)
        self.assertTrue(out["recovery_required"], out)
        self.assertFalse(any(
            event[0] == "tool_call" and event[1] == "close_fail" for event in out["events"]
        ), out)
        self.assertTrue(any(
            event[0] == "finalization" and "DENIED_WORKSPACE_RECOVERY_REQUIRED" in event[3]
            for event in out["events"]
        ), out)

    def test_local_diff_is_code_proven_while_dark_vendors_remain_legacy(self) -> None:
        out = self.drive(
            "sup=Supervisor(); sup.boot()\n"
            "caps={item['id']:item for item in sup._capabilities}\n"
            "out={'local':caps['local_llm']['features']['diff_snapshots'],"
            "'claude':caps['claude']['features']['diff_snapshots'],"
            "'codex':caps['codex']['features']['diff_snapshots']}\n"
            "sup.shutdown()\n"
        )
        self.assertTrue(out["local"])
        self.assertFalse(out["claude"])
        self.assertFalse(out["codex"])


if __name__ == "__main__":
    unittest.main()
