"""D4 supervisor integration: zero-record preflight, metadata durability, and prompt-only context bytes."""

from __future__ import annotations

import json
import unittest

from test_session_drive import _DrivenSubprocess


class SessionArtifactSupervisorTest(_DrivenSubprocess):
    """Governed-context start and turn materialization remains metadata-only on durable chat records, with zero-record denials."""
    def test_governed_context_start_and_turn_are_pre_record_and_metadata_only(self) -> None:
        out = self.drive(
            "from pathlib import Path\n"
            "from kaizen_components import db\n"
            "sup=Supervisor(); sup.boot(); sup._driven_test_records=True\n"
            "root=Path(sup.repo_root); source=root/'src'/'context.txt'; source.parent.mkdir(parents=True,exist_ok=True); source.write_text('trusted facts only @nested.txt',encoding='utf-8')\n"
            "provider=scripted_provider([final_reply('one'),final_reply('two')]); install_factory(sup,provider)\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'review @src/raw.txt','context_refs':[{'id':'file-1','kind':'file','source_path':'src/context.txt'}]}})\n"
            "rid=start.get('agent_run_id'); wait_idle(sup,rid,budget=20.0) if rid else None\n"
            "first_row=db.fetch_one(\"SELECT body FROM agent_events WHERE agent_run_id=? AND event_kind='chat_message' AND json_extract(body,'$.role')='user' ORDER BY sequence_no LIMIT 1\",(rid,)) if rid else None\n"
            "first_body=json.loads(first_row[0]) if first_row else {}\n"
            "before=[db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0],db.fetch_one('SELECT COUNT(*) FROM agent_events')[0]]\n"
            "bad=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'bad turn','context_refs':[{'id':'bad','kind':'file','source_path':'../escape'}]}})\n"
            "after_bad=[db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0],db.fetch_one('SELECT COUNT(*) FROM agent_events')[0]]\n"
            "turn=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'second @src/raw.txt','context_refs':[{'id':'file-2','kind':'file','source_path':'src/context.txt'}]}})\n"
            "wait_idle(sup,rid,budget=20.0)\n"
            "user_bodies=[json.loads(row[0]) for row in db.fetch_all(\"SELECT body FROM agent_events WHERE agent_run_id=? AND event_kind='chat_message' AND json_extract(body,'$.role')='user' ORDER BY sequence_no\",(rid,))]\n"
            "calls=provider.state['calls']; prompts=[next((message['content'] for message in call if message['role']=='user' and 'KAIZEN_GOVERNED_CONTEXT_V1' in message['content']),None) for call in calls]\n"
            "close=sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}}); sup.shutdown()\n"
            "out={'start':start,'first_body':first_body,'before':before,'bad':bad,'after_bad':after_bad,'turn':turn,'user_bodies':user_bodies,'prompts':prompts,'close':close}\n"
        )
        self.assertEqual(out["start"]["status"], "OK", out)
        self.assertEqual(out["bad"]["code"], "DENIED_CONTEXT_INVALID")
        self.assertEqual(out["before"], out["after_bad"], out)
        self.assertEqual(out["turn"]["status"], "OK")
        self.assertEqual(len(out["user_bodies"]), 2)
        for body in out["user_bodies"]:
            self.assertEqual(body["context_refs"][0]["encoding"], "utf-8")
            self.assertNotIn("snapshot_ref", body["context_refs"][0])
            self.assertNotIn("trusted facts only", json.dumps(body))
            self.assertNotIn("ui-cache", json.dumps(body))
        self.assertEqual(out["first_body"]["text"], "review @src/raw.txt")
        for prompt in out["prompts"]:
            self.assertIsNotNone(prompt, out)
            self.assertIn("trusted facts only", prompt)
            self.assertIn("untrusted reference data", prompt)
            self.assertNotIn("@src/raw.txt", prompt)
            self.assertNotIn("@nested.txt", prompt)
        self.assertEqual(out["close"]["status"], "OK")

    def test_invalid_unsupported_and_policy_context_create_zero_records(self) -> None:
        out = self.drive(
            "from pathlib import Path\n"
            "from kaizen_components import db\n"
            "sup=Supervisor(); sup.boot(); sup._driven_test_records=True\n"
            "root=Path(sup.repo_root); source=root/'deny.txt'; source.write_text('safe',encoding='utf-8')\n"
            "image_bytes=b'\\x89PNG\\r\\n\\x1a\\nbody'; staged=sup._artifact_cache.store('images',image_bytes,scope_id='host',media_type='image/png',origin='host')\n"
            "image={'id':'img','kind':'image','artifact_ref':staged.artifact_ref,'sha256':staged.sha256,'bytes':staged.bytes,'media_type':'image/png'}\n"
            "invalid=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'x','context_refs':[{'id':'bad','kind':'file','source_path':'../escape'}]}})\n"
            "unsupported_image=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'x','attachments':[image]}})\n"
            "caps={cap['id']:dict(cap['features']) for cap in sup._capabilities}\n"
            "for cap in sup._capabilities:\n"
            "    if cap['id']=='local_llm': cap['features']['governed_context']=False\n"
            "unsupported_context=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':'x','context_refs':[{'id':'file','kind':'file','source_path':'deny.txt'}]}})\n"
            "counts=[db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0],db.fetch_one('SELECT COUNT(*) FROM agent_events')[0]]\n"
            "sup.shutdown()\n"
            "out={'invalid':invalid,'unsupported_image':unsupported_image,'unsupported_context':unsupported_context,'counts':counts,'caps':caps}\n"
        )
        self.assertEqual(out["invalid"]["code"], "DENIED_CONTEXT_INVALID")
        self.assertEqual(out["unsupported_image"]["code"], "DENIED_ATTACHMENT_UNSUPPORTED")
        self.assertEqual(out["unsupported_context"]["code"], "DENIED_CONTEXT_UNSUPPORTED")
        self.assertEqual(out["counts"], [0, 0, 0], out)
        self.assertTrue(out["caps"]["local_llm"]["governed_context"])
        self.assertFalse(out["caps"]["claude"]["governed_context"])
        self.assertFalse(out["caps"]["codex"]["governed_context"])
        for features in out["caps"].values():
            self.assertFalse(features["image_attachments"])


if __name__ == "__main__":
    unittest.main()
