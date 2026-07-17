"""H2.5 observed Claude transcript capture, exercised with synthetic hook payloads only.

No real Claude process, hook installation, or network access occurs. Each case uses an isolated
KAIZEN_REPO_ROOT subprocess so C1/T5/T6/T8 assertions read the real SQLite-compatible record plane
without touching the project database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import REPO_ROOT, kaizen


_PREAMBLE = r'''
import json
from pathlib import Path
from kaizen_components import db
from kaizen_components.orchestration import policy
from kaizen_components.orchestration.supervisor import Supervisor

def make_sup():
    sup = Supervisor(repo_root=Path(ROOT))
    sup.policy = policy.PolicyEngine([], [], [])
    sup._driven_test_records = True
    return sup

def payload(event, session="vendor-session-1", **extra):
    value = {"hook_event_name": event, "session_id": session, "cwd": "D:/work"}
    value.update(extra)
    return value

def record(sup, event, session="vendor-session-1", **extra):
    p = payload(event, session=session, **extra)
    if event in ("PreToolUse", "UserPromptSubmit"):
        return sup._hooks_decide({"hook_event_name": event, "payload": p})
    return sup._hooks_record({"hook_event_name": event, "payload": p})

out = {}
exec(BODY)
print("RESULT " + json.dumps(out, ensure_ascii=True))
'''


def _run(root: Path, body: str) -> dict:
    script = "ROOT = " + repr(str(root)) + "\nBODY = " + repr(body) + "\n" + _PREAMBLE
    env = dict(os.environ)
    env["KAIZEN_REPO_ROOT"] = str(root)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    line = next((item for item in proc.stdout.splitlines() if item.startswith("RESULT ")), None)
    if line is None:
        raise AssertionError(f"driver failed rc={proc.returncode}; stdout={proc.stdout!r}; stderr={proc.stderr[-1200:]!r}")
    return json.loads(line[len("RESULT "):])


class ObservedClaudeHooksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-observed-hooks-"))
        self.addCleanup(_rmtree, self.root)
        rc, result = kaizen(self.root, "K1")
        self.assertEqual(rc, 0, result)

    def test_c1_reuse_linked_resume_replay_and_read_only_controls(self) -> None:
        body = r'''
sup = make_sup()
first = record(sup, "SessionStart", source="startup", model="claude-test")
record(sup, "UserPromptSubmit", prompt="first prompt")
record(sup, "PreToolUse", tool_name="Read", tool_input={"file_path": "D:/work/a.txt"}, tool_use_id="tool-1")

# Simulate a daemon restart: later hooks recover the active T5 from DB truth, not process memory.
sup = make_sup()
record(sup, "Stop", last_assistant_message="first answer")
end1 = record(sup, "SessionEnd", reason="prompt_input_exit")
end1_duplicate = record(sup, "SessionEnd", reason="prompt_input_exit")

second = record(sup, "SessionStart", source="resume", model="claude-test")
record(sup, "UserPromptSubmit", prompt="second prompt")
record(sup, "Stop", last_assistant_message="second answer")
record(sup, "SessionEnd", reason="prompt_input_exit")

sessions = db.fetch_all("SELECT id, controller, engine, auth_mode, vendor_session_root_id FROM agent_sessions")
runs = db.fetch_all("SELECT id, session_id, engine, auth_mode FROM agent_runs ORDER BY created_at, id")
chat = db.fetch_all(
    "SELECT agent_run_id, body FROM agent_events WHERE event_kind = 'chat_message' ORDER BY created_at, sequence_no"
)
finals = db.fetch_all("SELECT agent_run_id FROM agent_events WHERE event_kind = 'finalization'")
tool_points = db.fetch_all(
    "SELECT correlation_id FROM agent_events WHERE event_kind = 'hook_event' AND correlation_id = 'tool-1'"
)
listed = sup._handle_session_list({"controller": "observed"})
replays = [sup._handle_session_events({"agent_run_id": row[0], "since": 0}) for row in runs]
run_id = runs[-1][0]
controls = [
    sup._handle_session_turn({"agent_run_id": run_id, "prompt": "no"}),
    sup._handle_session_close({"agent_run_id": run_id}),
    sup._handle_session_steer({"agent_run_id": run_id, "instruction": "no"}),
    sup._handle_session_interrupt({"agent_run_id": run_id}),
    sup._handle_session_kill({"agent_run_id": run_id}),
]
out.update({
    "first": first, "second": second, "end1": end1, "end1_duplicate": end1_duplicate,
    "sessions": sessions, "runs": runs,
    "chat": [{"run": row[0], **json.loads(row[1])} for row in chat],
    "finals": finals, "tool_points": tool_points, "listed": listed, "replays": replays,
    "control_codes": [item.get("code") for item in controls],
})
'''
        result = _run(self.root, body)
        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(result["sessions"][0][1:], ["observed", "claude", "none", "vendor-session-1"])
        self.assertEqual(len(result["runs"]), 2)
        self.assertEqual({row[1] for row in result["runs"]}, {result["sessions"][0][0]})
        self.assertTrue(all(row[2:] == ["claude", "none"] for row in result["runs"]))
        self.assertEqual(
            [(item["role"], item["text"], item["source"]) for item in result["chat"]],
            [
                ("user", "first prompt", "observed"),
                ("assistant", "first answer", "observed"),
                ("user", "second prompt", "observed"),
                ("assistant", "second answer", "observed"),
            ],
        )
        self.assertEqual(len(result["finals"]), 2)
        self.assertTrue(result["end1_duplicate"]["deduplicated"])
        self.assertEqual(result["tool_points"], [["tool-1"]])
        self.assertEqual(len(result["listed"]["sessions"]), 1)
        self.assertEqual(len(result["listed"]["sessions"][0]["runs"]), 2)
        self.assertTrue(all(item["controller"] == "observed" for item in result["replays"]))
        self.assertTrue(all(item["terminal"] for item in result["replays"]))
        self.assertEqual(result["control_codes"], ["DENIED_OBSERVED_SESSION_READ_ONLY"] * 5)

    def test_duplicate_delivery_dedupes_but_repeated_text_after_next_role_is_kept(self) -> None:
        body = r'''
sup = make_sup()
record(sup, "SessionStart", source="startup")
record(sup, "UserPromptSubmit", prompt="same")
record(sup, "UserPromptSubmit", prompt="same")
record(sup, "Stop", last_assistant_message="same answer")
record(sup, "Stop", last_assistant_message="same answer")
record(sup, "UserPromptSubmit", prompt="same")
record(sup, "Stop", last_assistant_message="same answer")
record(sup, "PreToolUse", tool_name="Read", tool_input={"file_path": "D:/work/a"}, tool_use_id="tool-dupe")
record(sup, "PreToolUse", tool_name="Read", tool_input={"file_path": "D:/work/a"}, tool_use_id="tool-dupe")
record(sup, "SessionEnd", reason="prompt_input_exit")
chat = db.fetch_all("SELECT body FROM agent_events WHERE event_kind = 'chat_message' ORDER BY sequence_no")
tools = db.fetch_all(
    "SELECT id FROM agent_events WHERE event_kind = 'hook_event' AND correlation_id = 'tool-dupe'"
)
out["chat"] = [json.loads(row[0]) for row in chat]
out["tool_count"] = len(tools)
'''
        result = _run(self.root, body)
        self.assertEqual([item["role"] for item in result["chat"]], ["user", "assistant", "user", "assistant"])
        self.assertEqual(result["tool_count"], 1)

    def test_redaction_and_size_limit_write_explicit_placeholders(self) -> None:
        body = r'''
sup = make_sup()
record(sup, "SessionStart", source="startup")
record(sup, "UserPromptSubmit", prompt="token sk-ant-aaaaaaaaaaaaaaaaaaaaaaaa")
record(sup, "Stop", last_assistant_message="x" * ((1 << 20) + 1))
record(sup, "SessionEnd", reason="prompt_input_exit")
rows = db.fetch_all(
    "SELECT code, body FROM agent_events WHERE event_kind = 'chat_message' ORDER BY sequence_no"
)
out["messages"] = [{"code": row[0], **json.loads(row[1])} for row in rows]
out["raw_db_contains_secret"] = bool(db.fetch_one(
    "SELECT id FROM agent_events WHERE body LIKE '%sk-ant-%' LIMIT 1"
))
'''
        result = _run(self.root, body)
        self.assertEqual(
            [item["code"] for item in result["messages"]],
            ["DENIED_CHAT_MESSAGE_REDACTED", "DENIED_CHAT_MESSAGE_OVERSIZE"],
        )
        self.assertEqual(
            [item["text"] for item in result["messages"]],
            ["[message omitted: redaction policy]", "[message omitted: exceeds 1 MiB limit]"],
        )
        self.assertFalse(result["raw_db_contains_secret"])

    def test_stop_failure_is_structured_deduplicated_and_session_end_finalizes_once(self) -> None:
        body = r'''
sup = make_sup()
record(sup, "SessionStart", source="startup")
record(sup, "UserPromptSubmit", prompt="try")
record(sup, "StopFailure", error_type="rate_limit")
record(sup, "StopFailure", error_type="rate_limit")
before = db.fetch_all("SELECT id FROM agent_events WHERE event_kind = 'finalization'")
record(sup, "SessionEnd", reason="prompt_input_exit")
record(sup, "SessionEnd", reason="prompt_input_exit")
failures = db.fetch_all(
    "SELECT event_kind, code, body FROM agent_events WHERE event_kind = 'rate_limit'"
)
after = db.fetch_all("SELECT id FROM agent_events WHERE event_kind = 'finalization'")
out.update({"before": len(before), "after": len(after), "failures": failures})
'''
        result = _run(self.root, body)
        self.assertEqual(result["before"], 0)
        self.assertEqual(result["after"], 1)
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(result["failures"][0][:2], ["rate_limit", "rate_limit"])
        self.assertEqual(
            json.loads(result["failures"][0][2]),
            {"hook_event_name": "StopFailure", "error_type": "rate_limit"},
        )

    def test_recording_failure_is_swallowed_by_record_handler(self) -> None:
        body = r'''
sup = make_sup()
record(sup, "SessionStart", source="startup")
def boom(*args, **kwargs):
    raise RuntimeError("ledger unavailable")
sup.funnel_event = boom
out["response"] = record(sup, "Stop", last_assistant_message="answer")
'''
        result = _run(self.root, body)
        self.assertEqual(result["response"]["status"], "OK")
        self.assertFalse(result["response"]["recorded"])
        self.assertEqual(result["response"]["code"], "OBSERVED_RECORD_FAILED")

    def test_stop_failure_error_type_maps_to_kind(self) -> None:
        # Beyond rate_limit (covered above): billing_error -> auth, invalid_request -> context, an
        # UNKNOWN error_type -> transport (the catch-all). Each lands as the mapped event_kind carrying
        # the structured StopFailure body {hook_event_name, error_type}.
        body = r'''
sup = make_sup()
record(sup, "SessionStart", source="startup")
record(sup, "UserPromptSubmit", prompt="try")
record(sup, "StopFailure", error_type="billing_error")
record(sup, "StopFailure", error_type="invalid_request")
record(sup, "StopFailure", error_type="totally_made_up_error")
rows = db.fetch_all(
    "SELECT event_kind, code, body FROM agent_events "
    "WHERE event_kind IN ('auth', 'context', 'transport') ORDER BY sequence_no"
)
out["rows"] = [{"kind": row[0], "code": row[1], "body": json.loads(row[2])} for row in rows]
'''
        result = _run(self.root, body)
        self.assertEqual(
            [(row["kind"], row["code"]) for row in result["rows"]],
            [("auth", "billing_error"), ("context", "invalid_request"),
             ("transport", "totally_made_up_error")],
        )
        for row in result["rows"]:
            self.assertEqual(
                row["body"], {"hook_event_name": "StopFailure", "error_type": row["code"]}
            )

    def test_session_start_supersedes_open_run_reusing_the_same_c1(self) -> None:
        # A second SessionStart for the same vendor session WITHOUT an intervening SessionEnd finalizes
        # the still-open prior T5 (canceled) and opens a fresh T5 under the SAME reused C1. The two
        # SessionStarts must differ (source startup vs resume) so the second is not deduplicated as a
        # redelivery of the first. Result: one C1, two runs, the first terminal canceled.
        body = r'''
sup = make_sup()
first = record(sup, "SessionStart", source="startup", model="claude-test")
record(sup, "UserPromptSubmit", prompt="p1")
record(sup, "Stop", last_assistant_message="a1")
second = record(sup, "SessionStart", source="resume", model="claude-test")
sessions = db.fetch_all("SELECT id FROM agent_sessions")
runs = db.fetch_all("SELECT id, session_id FROM agent_runs ORDER BY created_at, id")
fins = db.fetch_all(
    "SELECT agent_run_id, marker, code FROM agent_events WHERE event_kind = 'finalization'"
)
terminals = {row[0]: sup._safe_reduce(row[0]) for row in runs}
out.update({
    "first": first, "second": second, "sessions": sessions, "runs": runs, "fins": fins,
    "terminals": {rid: [state["terminal"], state["terminal_state"]] for rid, state in terminals.items()},
})
'''
        result = _run(self.root, body)
        self.assertEqual(len(result["sessions"]), 1)  # one C1 reused across both SessionStarts
        self.assertFalse(result["first"]["deduplicated"])
        self.assertFalse(result["second"]["deduplicated"])
        self.assertEqual(result["first"]["session_id"], result["second"]["session_id"])
        self.assertNotEqual(result["first"]["agent_run_id"], result["second"]["agent_run_id"])
        run_ids = [row[0] for row in result["runs"]]
        session_ids = {row[1] for row in result["runs"]}
        self.assertEqual(len(run_ids), 2)
        self.assertEqual(session_ids, {result["sessions"][0][0]})  # both runs under the reused C1
        first_run, second_run = result["first"]["agent_run_id"], result["second"]["agent_run_id"]
        # Exactly one finalization: the superseded prior run, canceled.
        self.assertEqual(result["fins"], [[first_run, "close_canceled", "canceled"]])
        self.assertEqual(result["terminals"][first_run], [True, "failure"])  # canceled -> terminal failure
        self.assertEqual(result["terminals"][second_run], [False, None])  # fresh run stays open

    def test_decide_response_unchanged_when_recorder_raises(self) -> None:
        # A PreToolUse decide issued TWICE with an IDENTICAL payload: once normally, once with the
        # record-only path forced to raise (funnel_event injection, as the record-only case above). The
        # decide response (status/decision/reason/dedupe/rule fields) must be byte-identical: a recording
        # failure never weakens or alters the observe/strict policy decision.
        body = r'''
sup = make_sup()
record(sup, "SessionStart", source="startup")
tool_input = {"file_path": "D:/work/a.txt"}
normal = record(sup, "PreToolUse", tool_name="Read", tool_input=tool_input, tool_use_id="tool-same")
real = sup.funnel_event
def boom(*args, **kwargs):
    raise RuntimeError("ledger unavailable")
sup.funnel_event = boom
broken = record(sup, "PreToolUse", tool_name="Read", tool_input=tool_input, tool_use_id="tool-same")
sup.funnel_event = real
out.update({"normal": normal, "broken": broken})
'''
        result = _run(self.root, body)
        self.assertEqual(result["normal"]["status"], "OK")
        self.assertEqual(result["broken"]["status"], "OK")
        # The whole decision-bearing response is unchanged despite the recorder raising on the 2nd call.
        self.assertEqual(result["normal"], result["broken"])


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
