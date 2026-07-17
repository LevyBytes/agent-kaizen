"""Opt-in D4 L6 Ollama proof with traversal and policy-denial zero-record controls."""

from __future__ import annotations

import json
import os
import unittest

from test_session_drive import _DrivenSubprocess


_LIVE = os.environ.get("KAIZEN_RUN_LIVE") == "1" and bool(os.environ.get("KAIZEN_LLM_MODEL"))


@unittest.skipUnless(_LIVE, "set KAIZEN_RUN_LIVE=1 and KAIZEN_LLM_MODEL for existing local Ollama")
class GovernedContextLiveL6Test(_DrivenSubprocess):
    """Live sentinel round-trip, metadata-only wire behavior, and zero-record security denials."""
    def test_file_selection_output_metadata_and_zero_record_controls(self) -> None:
        model = os.environ["KAIZEN_LLM_MODEL"]
        out = self.drive(
            "from pathlib import Path\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration import policy as P\n"
            "sup=Supervisor(); sup.boot(); sup._driven_test_records=True\n"
            "root=Path(sup.repo_root); src=root/'src'; src.mkdir(parents=True,exist_ok=True)\n"
            "file_marker='D4_FILE_SENTINEL_7A91C2'; selection_marker='D4_SELECTION_SENTINEL_4E83B6'\n"
            "file_path=src/'file.txt'; file_path.write_text(file_marker,encoding='utf-8')\n"
            "selection_source=src/'dirty.txt'; selection_source.write_text('disk-version-not-selection',encoding='utf-8')\n"
            "selected=selection_marker.encode('utf-8'); staged=sup._artifact_cache.store('context',selected,scope_id='host-live-l6',origin='selection')\n"
            "refs=[{'id':'file','kind':'file','source_path':'src/file.txt'},{'id':'selection','kind':'selection','source_path':'src/dirty.txt','range':{'start':{'line':0,'character':0},'end':{'line':0,'character':len(selection_marker)}},'snapshot_ref':staged.artifact_ref,'sha256':staged.sha256,'bytes':staged.bytes,'encoding':'utf-8'}]\n"
            "prompt='Read both governed context records. Reply with exactly one JSON object whose final string contains the complete file value, one space, then the complete selection value. Do not call tools.'\n"
            f"start=sup._handle_control({{'op':'session/start','args':{{'engine':'local_llm','model':{model!r},'max_turns':3,'prompt':prompt,'context_refs':refs}}}})\n"
            "rid=start.get('agent_run_id'); idle_session=wait_idle(sup,rid,budget=120.0) if rid else None; idle=bool(idle_session and idle_session.turn_state=='idle')\n"
            "messages=[json.loads(row[0]) for row in db.fetch_all(\"SELECT body FROM agent_events WHERE agent_run_id=? AND event_kind='chat_message' ORDER BY sequence_no\",(rid,))] if rid else []\n"
            "assistant=next((message['text'] for message in messages if message.get('role')=='assistant'),'')\n"
            "user=next((message for message in messages if message.get('role')=='user'),{})\n"
            "counts=lambda:[db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0],db.fetch_one('SELECT COUNT(*) FROM agent_events')[0]]\n"
            "before_traversal=counts(); traversal=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','model':"
            f"{model!r},'prompt':'control','context_refs':[{{'id':'bad','kind':'file','source_path':'../escape'}}]}}}}); after_traversal=counts()\n"
            "denied_path=src/'denied.txt'; denied_path.write_text('DENIED_CONTROL',encoding='utf-8'); original=sup._build_session_snapshot\n"
            "deny_rule={'id':'d4-live-deny','rule_type':'deny','verb':'file_read','match_kind':'path_prefix','pattern':str(denied_path),'engine':None,'enabled':True}\n"
            "sup._build_session_snapshot=lambda engine,mode:P.build_policy_snapshot(engine,mode,str(root),[],[deny_rule],protected_paths=[],vendor_config_paths=[])\n"
            "before_policy=counts(); policy_denied=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','model':"
            f"{model!r},'prompt':'control','context_refs':[{{'id':'denied','kind':'file','source_path':'src/denied.txt'}}]}}}}); after_policy=counts(); sup._build_session_snapshot=original\n"
            "close=sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}}) if rid and idle else {}; sup.shutdown()\n"
            "out={'start_status':start.get('status'),'start_code':start.get('code'),'idle':idle,'assistant':assistant,'user':user,'before_traversal':before_traversal,'traversal_code':traversal.get('code'),'after_traversal':after_traversal,'before_policy':before_policy,'policy_code':policy_denied.get('code'),'after_policy':after_policy,'close_status':close.get('status')}\n",
            env={
                "KAIZEN_RUN_LIVE": "1",
                "KAIZEN_LLM_MODEL": model,
                "KAIZEN_LLM_BASE_URL": os.environ.get("KAIZEN_LLM_BASE_URL", "http://127.0.0.1:11434/v1"),
            },
            timeout=180.0,
        )
        self.assertEqual(out["start_status"], "OK", out)
        self.assertTrue(out["idle"], out)
        self.assertIn("D4_FILE_SENTINEL_7A91C2", out["assistant"], out)
        self.assertIn("D4_SELECTION_SENTINEL_4E83B6", out["assistant"], out)
        self.assertEqual(len(out["user"].get("context_refs", [])), 2, out)
        self.assertNotIn("D4_FILE_SENTINEL_7A91C2", json.dumps(out["user"]))
        self.assertNotIn("D4_SELECTION_SENTINEL_4E83B6", json.dumps(out["user"]))
        self.assertNotIn("ui-cache", json.dumps(out["user"]))
        self.assertEqual(out["traversal_code"], "DENIED_CONTEXT_INVALID")
        self.assertEqual(out["before_traversal"], out["after_traversal"])
        self.assertEqual(out["policy_code"], "DENIED_CONTEXT_POLICY")
        self.assertEqual(out["before_policy"], out["after_policy"])
        self.assertEqual(out["close_status"], "OK")


if __name__ == "__main__":
    unittest.main()
