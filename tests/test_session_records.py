"""Orchestration session records (v8 M2 / C1-C5) round-trip through the real CLI.

Exit criteria proven here:
- all five ops (C1-C5) round-trip through the CLI (IsolatedDBTest);
- C5 timeline joins the session's instructions/goals/approvals in one read;
- synthetic RESERVED value-space rows are accepted (controller=observed, mode=hooked,
  auth_mode in {subscription, api-key}) -- the deferred M-CLAUDE Claude lane's value space;
- the C4 state machine rejects re-deciding an already-decided approval;
- Q8 validates a sample payload for each new record kind;
- (K7 purge + op-coverage + Purpose parity are proven by their own suite files, which now
  cover the C-family tables/ops via TEST_ROOT_TABLES / ALIASES / README.)
"""

from __future__ import annotations

import json
import re

import turso

from _harness import IsolatedDBTest
from kaizen_components.session_protocol import (
    SessionProtocolError,
    canonical_snippet,
    canonical_title,
    pack_session_policy_snapshot,
    parse_feature_flags,
    read_session_client_features,
    validate_context_refs,
    validate_durable_context_refs,
    validate_image_refs,
    validate_workspace_relative_path,
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class SessionRecordsTest(IsolatedDBTest):
    """CLI round-trip coverage for C1-C5 records, conversation-profile metadata, and durable chat/profile events."""
    def _start_session(self, **extra):
        """Create and validate a C1 fixture session from string-valued CLI flag arguments, returning its identifier."""
        args = ["C1", "--summary", "Governed session for tests."]
        for flag, value in extra.items():
            args += [f"--{flag.replace('_', '-')}", value]
        rc, p = self.kz(*args)
        self.assertEqual(rc, 0, p)
        self.assertTrue(_HASH_RE.match(p.get("content_hash", "")), p)
        return p["id"]

    def test_c1_create_and_resume(self):
        sid = self._start_session(controller="kaizen", mode="orchestrate", auth_mode="none", engine="codex")
        # Resume returns the existing envelope without minting a new row.
        rc, p = self.kz("C1", "--id", sid)
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["resumed"], p)
        self.assertEqual(p["session"]["id"], sid, p)
        self.assertEqual(p["session"]["controller"], "kaizen", p)
        self.assertEqual(p["session"]["mode"], "orchestrate", p)

    def test_c1_defaults_to_observed_observe_none(self):
        sid = self._start_session()
        rc, p = self.kz("C5", "--session-id", sid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["session"]["controller"], "observed", p)
        self.assertEqual(p["session"]["mode"], "observe", p)
        self.assertEqual(p["session"]["auth_mode"], "none", p)

    def test_c1_rejects_invalid_controller(self):
        rc, p = self.kz("C1", "--summary", "bad.", "--controller", "nonsense")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_MODE_INVALID", p)

    def test_c1_rejects_invalid_auth_mode(self):
        rc, p = self.kz("C1", "--summary", "bad.", "--auth-mode", "oauth")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_AUTH_MODE_INVALID", p)

    def test_synthetic_reserved_claude_value_space_accepted(self):
        # controller=observed + mode=hooked + auth_mode in {subscription, api-key}: the deferred
        # M-CLAUDE value space, accepted by the record layer now with no live producer.
        for auth in ("subscription", "api-key"):
            sid = self._start_session(controller="observed", mode="hooked", auth_mode=auth)
            rc, p = self.kz("C5", "--session-id", sid)
            self.assertEqual(rc, 0, p)
            self.assertEqual(p["session"]["mode"], "hooked", p)
            self.assertEqual(p["session"]["auth_mode"], auth, p)

    def test_c2_instruction_add_and_ordering(self):
        sid = self._start_session()
        rc, p1 = self.kz("C2", "--session-id", sid, "--body", "First instruction.")
        self.assertEqual(rc, 0, p1)
        self.assertEqual(p1["seq"], 1, p1)
        rc, p2 = self.kz("C2", "--session-id", sid, "--body", "Second instruction.")
        self.assertEqual(rc, 0, p2)
        self.assertEqual(p2["seq"], 2, p2)

    def test_c2_unknown_session_denied(self):
        rc, p = self.kz("C2", "--session-id", "as_nope", "--body", "orphan instruction")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_SESSION_NOT_FOUND", p)

    def test_c3_goal_create_and_update(self):
        sid = self._start_session()
        rc, p = self.kz("C3", "--session-id", sid, "--title", "Ship M2", "--summary", "Land session records.")
        self.assertEqual(rc, 0, p)
        gid = p["id"]
        self.assertFalse(p["updated"], p)
        rc, p = self.kz("C3", "--id", gid, "--status", "done", "--summary", "M2 landed.")
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["updated"], p)
        self.assertEqual(p["state"], "done", p)

    def test_c4_approval_create_and_decide(self):
        sid = self._start_session()
        rc, p = self.kz(
            "C4", "--session-id", sid, "--summary", "Approve tool call.",
            "--payload-json", '{"request_type":"tool_approval","correlation_id":"c1"}',
        )
        self.assertEqual(rc, 0, p)
        aid = p["id"]
        self.assertEqual(p["state"], "open", p)
        rc, p = self.kz(
            "C4", "--id", aid, "--status", "approved",
            "--payload-json", '{"decided_by":"human","rule_id":"r1"}',
        )
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["state"], "approved", p)

    def test_c4_state_machine_rejects_redeciding(self):
        sid = self._start_session()
        rc, p = self.kz("C4", "--session-id", sid, "--summary", "Approve.", "--payload-json", '{"request_type":"tool_approval"}')
        self.assertEqual(rc, 0, p)
        aid = p["id"]
        rc, p = self.kz("C4", "--id", aid, "--status", "denied", "--payload-json", '{"decided_by":"auto"}')
        self.assertEqual(rc, 0, p)
        # Already denied (a decided terminal state) -> re-decision refused.
        rc, p = self.kz("C4", "--id", aid, "--status", "approved", "--payload-json", '{"decided_by":"human"}')
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_APPROVAL_ALREADY_DECIDED", p)

    def test_c5_timeline_joins_children(self):
        sid = self._start_session(controller="kaizen", mode="orchestrate")
        for args in (
            ("C2", "--session-id", sid, "--body", "Do the thing."),
            ("C3", "--session-id", sid, "--title", "Goal one", "--summary", "First goal."),
            ("C4", "--session-id", sid, "--summary", "Approve.", "--payload-json", '{"request_type":"plan_exit"}'),
        ):
            seed_rc, seed_payload = self.kz(*args)
            self.assertEqual(seed_rc, 0, seed_payload)
        rc, p = self.kz("C5", "--session-id", sid)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["counts"], {"instructions": 1, "goals": 1, "approvals": 1}, p)
        self.assertEqual(p["instructions"][0]["instruction"], "Do the thing.", p)
        self.assertEqual(p["goals"][0]["title"], "Goal one", p)
        self.assertEqual(p["approvals"][0]["request_type"], "plan_exit", p)

    def test_c5_unknown_session_denied(self):
        rc, p = self.kz("C5", "--session-id", "as_missing")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_SESSION_NOT_FOUND", p)

    def test_q8_validates_each_new_kind(self):
        samples = {
            "agent_session": '{"controller":"kaizen","mode":"orchestrate","auth_mode":"subscription","title":"Canonical title","summary":"A governed session."}',
            "user_instruction": '{"session_id":"as_1","instruction":"Do X.","summary":"Do X."}',
            "goal": '{"session_id":"as_1","title":"Ship it","summary":"Ship the milestone."}',
            "approval_request": '{"session_id":"as_1","request_type":"tool_approval","state":"open","summary":"Approve the call."}',
            "mode_profile": '{"name":"default","profile_json":"{}","summary":"Default per-engine profile."}',
        }
        for kind, payload in samples.items():
            rc, p = self.kz("Q8", "--kind", kind, "--payload-json", payload)
            self.assertEqual(rc, 0, f"{kind}: {p}")
            self.assertTrue(p.get("valid"), f"{kind}: {p}")

    # --- v8 H2.1 conversation-profile columns + durable conversation events ----------------------

    def test_c1_title_persists_resumes_and_legacy_null_stays_readable(self):
        rc, created = self.kz(
            "C1", "--summary", "Titled session.",
            "--payload-json", json.dumps({"title": "Canonical title"}),
        )
        self.assertEqual(rc, 0, created)
        sid = created["id"]
        rc, resumed = self.kz("C1", "--id", sid)
        self.assertEqual(rc, 0, resumed)
        self.assertEqual(resumed["session"]["title"], "Canonical title")
        rc, timeline = self.kz("C5", "--session-id", sid)
        self.assertEqual(rc, 0, timeline)
        self.assertEqual(timeline["session"]["title"], "Canonical title")

        legacy = self._start_session()
        rc, legacy_timeline = self.kz("C5", "--session-id", legacy)
        self.assertEqual(rc, 0, legacy_timeline)
        self.assertIsNone(legacy_timeline["session"]["title"])

    def test_c1_title_rejects_noncanonical_or_unsafe_values(self):
        for title in ("  not canonical  ", "", 7):
            rc, denied = self.kz(
                "C1", "--summary", "Bad title.", "--payload-json", json.dumps({"title": title}),
            )
            self.assertEqual(rc, 2, denied)
            self.assertEqual(denied.get("code"), "DENIED_TITLE_INVALID", denied)
        secret = "sk-ant-api03-" + "A" * 40
        rc, denied = self.kz(
            "C1", "--summary", "Unsafe title.", "--payload-json", json.dumps({"title": secret}),
        )
        self.assertEqual(rc, 2, denied)
        self.assertEqual(denied.get("code"), "DENIED_TITLE_INVALID", denied)
        self.assertNotIn(secret, json.dumps(denied))

    def test_session_protocol_canonical_text_and_direct_features(self):
        self.assertEqual(canonical_title("  Alpha\n Beta  "), "Alpha Beta")
        self.assertEqual(len(canonical_title("😀" * 81) or ""), 80)
        self.assertEqual(len(canonical_snippet("z" * 161) or ""), 160)
        self.assertIsNone(canonical_title(" \t\n "))
        flags = parse_feature_flags({
            "streaming": True,
            "image_attachments": 1,
            "governed_context": "true",
            "diff_snapshots": [],
            "writer_leasing": False,
            "unknown": True,
        })
        self.assertEqual(flags, {
            "streaming": True,
            "image_attachments": False,
            "governed_context": False,
            "diff_snapshots": False,
            "writer_leasing": False,
        })
        self.assertFalse(any(parse_feature_flags(None).values()))

    def test_session_policy_snapshot_metadata_is_fail_closed_and_hash_neutral(self):
        from kaizen_components.orchestration import policy

        snapshot = policy.PolicySnapshot(
            engine="claude",
            permission_mode="ask",
            workspace_root="D:/workspace",
            designated_write_roots=(),
            rules=(),
            protected_paths=(),
            vendor_config_paths=(),
            permission_mode_version=policy.PERMISSION_MODE_VERSION,
            protected_path_version=policy.PROTECTED_PATH_VERSION,
            profile_hash="b" * 64,
        )
        packed = pack_session_policy_snapshot(
            policy.snapshot_to_json(snapshot), {"diff_snapshots": True},
        )
        restored = policy.snapshot_from_json(packed)
        self.assertEqual(restored, snapshot)
        self.assertEqual(restored.profile_hash, snapshot.profile_hash)
        self.assertEqual(read_session_client_features(packed), {"diff_snapshots": True})
        self.assertEqual(
            read_session_client_features(pack_session_policy_snapshot(policy.snapshot_to_json(snapshot), {"diff_snapshots": 1})),
            {"diff_snapshots": False},
        )
        self.assertEqual(read_session_client_features("{bad"), {"diff_snapshots": False})
        self.assertEqual(read_session_client_features(None), {"diff_snapshots": False})

    def test_image_and_context_ref_structural_validators(self):
        digest = "a" * 64
        image = {
            "id": "image-1",
            "kind": "image",
            "artifact_ref": f"sha256:{digest}",
            "sha256": digest,
            "bytes": 1024,
            "media_type": "image/png",
            "name": "diagram.png",
        }
        self.assertEqual(validate_image_refs([image]), [image])
        with self.assertRaisesRegex(SessionProtocolError, "DENIED_ATTACHMENT_TOO_LARGE"):
            validate_image_refs([{**image, "id": f"i-{index}"} for index in range(5)])
        with self.assertRaisesRegex(SessionProtocolError, "DENIED_ATTACHMENT_INVALID"):
            validate_image_refs([{**image, "sha256": "c" * 64}])
        with self.assertRaisesRegex(SessionProtocolError, "DENIED_ATTACHMENT_INVALID"):
            validate_image_refs([{**image, "bytes": 0}])

        file_ref = {"id": "file-1", "kind": "file", "source_path": "src/main.py"}
        selection = {
            "id": "selection-1",
            "kind": "selection",
            "source_path": "src/main.py",
            "range": {
                "start": {"line": 1, "character": 2},
                "end": {"line": 2, "character": 0},
            },
            "snapshot_ref": f"sha256:{digest}",
            "sha256": digest,
            "bytes": 128,
            "encoding": "utf-8",
        }
        self.assertEqual(validate_context_refs([file_ref, selection]), [file_ref, selection])
        with self.assertRaisesRegex(SessionProtocolError, "DENIED_CONTEXT_INVALID"):
            validate_context_refs([{**file_ref, "source_path": "../escape.py"}])
        with self.assertRaisesRegex(SessionProtocolError, "DENIED_CONTEXT_TOO_LARGE"):
            validate_context_refs([
                {**selection, "id": f"selection-{index}", "bytes": 256 * 1024}
                for index in range(5)
            ])

    def test_reference_validator_shared_request_and_durable_contract(self):
        digest = "d" * 64
        image = {
            "id": "i" * 128,
            "kind": "image",
            "artifact_ref": f"sha256:{digest}",
            "sha256": digest,
            "bytes": 4 * 1024 * 1024,
            "media_type": "image/webp",
            # A display name is not a filesystem basename; separators/colon are valid when redaction-safe.
            "name": "captures/ui:variant/screenshot.webp",
        }
        self.assertEqual(validate_image_refs([image]), [image])
        with self.assertRaisesRegex(SessionProtocolError, "DENIED_ATTACHMENT_INVALID"):
            validate_image_refs([{**image, "id": "i" * 129}])
        with self.assertRaisesRegex(SessionProtocolError, "DENIED_ATTACHMENT_INVALID"):
            validate_image_refs([{**image, "name": "owner@private-domain.test"}])

        path_at_limit = "p" * 4096
        self.assertEqual(validate_workspace_relative_path(path_at_limit), path_at_limit)
        for path in (
            "p" * 4097,
            "/absolute.py",
            "C:/drive.py",
            "dir\\file.py",
            "./dot.py",
            "dir/../parent.py",
            "dir//empty.py",
            "bad\x00path.py",
            "AI/work/orchestration/ui-cache",
            "ai/work/orchestration/ui-cache/context/object",
        ):
            with self.subTest(path=repr(path)):
                with self.assertRaisesRegex(SessionProtocolError, "DENIED_CONTEXT_INVALID"):
                    validate_workspace_relative_path(path)

        request_file = {"id": "f" * 128, "kind": "file", "source_path": "src/main.py"}
        durable_file = {
            **request_file,
            "sha256": digest,
            "bytes": 256 * 1024,
            "encoding": "utf-8",
        }
        selection = {
            "id": "selection",
            "kind": "selection",
            "source_path": "src/main.py",
            "range": {
                "start": {"line": 2**40, "character": 0},
                "end": {"line": 2**40, "character": 1},
            },
            "snapshot_ref": f"sha256:{digest}",
            "sha256": digest,
            "bytes": 0,
            "encoding": "utf-8",
        }
        self.assertEqual(validate_context_refs([request_file, selection]), [request_file, selection])
        self.assertEqual(validate_durable_context_refs([durable_file, selection]), [durable_file, selection])
        with self.assertRaisesRegex(SessionProtocolError, "DENIED_CONTEXT_INVALID"):
            validate_context_refs([durable_file])
        with self.assertRaisesRegex(SessionProtocolError, "DENIED_CONTEXT_INVALID"):
            validate_durable_context_refs([request_file])

    def test_c1_persists_conversation_profile(self):
        sid = self._start_session(
            controller="kaizen", mode="orchestrate",
            permission_mode="agent", requested_model="gpt-5", requested_reasoning_effort="high",
            profile_hash="abc123",
        )
        rc, p = self.kz("C5", "--session-id", sid)
        self.assertEqual(rc, 0, p)
        sess = p["session"]
        self.assertEqual(sess["permission_mode"], "agent", p)
        self.assertEqual(sess["requested_model"], "gpt-5", p)
        self.assertEqual(sess["requested_reasoning_effort"], "high", p)
        self.assertEqual(sess["profile_hash"], "abc123", p)

    def test_c1_rejects_invalid_permission_mode(self):
        rc, p = self.kz("C1", "--summary", "bad.", "--permission-mode", "nonsense")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_PERMISSION_MODE_INVALID", p)

    def test_legacy_null_h2_metadata_readable(self):
        # A session created WITHOUT any H2 fields (legacy shape) reads with NULL profile fields.
        sid = self._start_session()
        rc, p = self.kz("C5", "--session-id", sid)
        self.assertEqual(rc, 0, p)
        sess = p["session"]
        self.assertIsNone(sess["permission_mode"], p)
        self.assertIsNone(sess["requested_model"], p)
        self.assertIsNone(sess["profile_hash"], p)

    def _start_run(self, **payload_extra):
        """Create a T5 fixture run from default metadata plus JSON-serializable overrides, returning its identifier."""
        payload = {"agent_type": "codex", "surface": "cli"}
        payload.update(payload_extra)
        rc, p = self.kz("T5", "--summary", "Run for chat events.", "--payload-json", json.dumps(payload))
        self.assertEqual(rc, 0, p)
        return p["id"]

    def test_t5_persists_conversation_profile(self):
        sid = self._start_session()
        rid = self._start_run(
            session_id=sid, engine="codex", auth_mode="subscription",
            requested_model="gpt-5", reasoning_effort="high", permission_mode="agent", profile_hash="h1",
        )
        rc, p = self.kz("T7", "--agent-run-id", rid)
        self.assertEqual(rc, 0, p)
        env = p["envelope"]
        self.assertEqual(env["session_id"], sid, p)
        self.assertEqual(env["engine"], "codex", p)
        self.assertEqual(env["auth_mode"], "subscription", p)
        self.assertEqual(env["requested_model"], "gpt-5", p)
        self.assertEqual(env["reasoning_effort"], "high", p)
        self.assertEqual(env["permission_mode"], "agent", p)
        self.assertEqual(env["profile_hash"], "h1", p)

    def test_chat_message_event_accepted(self):
        rid = self._start_run()
        body = json.dumps({"role": "user", "text": "Refactor the parser.", "source": "driven", "turn_id": "t1"})
        rc, p = self.kz(
            "T6", "--agent-run-id", rid,
            "--payload-json", json.dumps({"event_kind": "chat_message", "marker": "point", "summary": "user msg", "body": body}),
        )
        self.assertEqual(rc, 0, p)

    def test_profile_event_accepted(self):
        rid = self._start_run()
        body = json.dumps({"requested": {"permission_mode": "plan"}, "effective": {"permission_mode": "plan"}, "profile_hash": "h1"})
        rc, p = self.kz(
            "T6", "--agent-run-id", rid,
            "--payload-json", json.dumps({"event_kind": "profile", "marker": "point", "summary": "profile snapshot", "body": body}),
        )
        self.assertEqual(rc, 0, p)

    def test_resume_profile_metadata_is_strict_and_legacy_profile_stays_compatible(self):
        rid = self._start_run()
        valid = {
            "requested": {}, "effective": {}, "profile_hash": "h1",
            "resume_fidelity": "reduced", "omitted_message_count": 2,
            "expired_artifacts": [{
                "kind": "selection", "sha256": "a" * 64, "bytes": 12,
                "availability": "expired", "name": "selection",
            }],
        }
        rc, p = self.kz(
            "T6", "--agent-run-id", rid,
            "--payload-json", json.dumps({
                "event_kind": "profile", "marker": "point", "summary": "resume profile",
                "body": json.dumps(valid),
            }),
        )
        self.assertEqual(rc, 0, p)
        invalid = [
            {**valid, "resume_fidelity": "transcript_seeded"},
            {**valid, "omitted_message_count": -1},
            {**valid, "omitted_message_count": True},
            {key: value for key, value in valid.items() if key != "omitted_message_count"},
            {**valid, "expired_artifacts": [{
                **valid["expired_artifacts"][0], "content": "historical bytes forbidden",
            }]},
        ]
        for body in invalid:
            rc, denied = self.kz(
                "T6", "--agent-run-id", rid,
                "--payload-json", json.dumps({
                    "event_kind": "profile", "marker": "point", "summary": "bad resume profile",
                    "body": json.dumps(body),
                }),
            )
            self.assertEqual(rc, 2, denied)
            self.assertEqual(denied.get("code"), "DENIED_PROFILE_BODY", denied)

    def test_chat_message_bad_role_rejected(self):
        rid = self._start_run()
        body = json.dumps({"role": "system", "text": "x", "source": "driven"})
        rc, p = self.kz(
            "T6", "--agent-run-id", rid,
            "--payload-json", json.dumps({"event_kind": "chat_message", "marker": "point", "summary": "bad role", "body": body}),
        )
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_CHAT_MESSAGE_ROLE", p)

    def test_chat_message_oversize_rejected(self):
        # 1 MiB+ text: write via --payload-json-file (a 1 MiB inline arg exceeds the Windows arg limit).
        rid = self._start_run()
        big = "a" * (1024 * 1024 + 1)  # > 1 MiB
        body = json.dumps({"role": "assistant", "text": big, "source": "driven"})
        payload_path = self.root / "oversize_event.json"
        payload_path.write_text(
            json.dumps({"event_kind": "chat_message", "marker": "point", "summary": "oversize", "body": body}),
            encoding="utf-8",
        )
        rc, p = self.kz("T6", "--agent-run-id", rid, "--payload-json-file", str(payload_path))
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_CHAT_MESSAGE_OVERSIZE", p)

    def test_chat_message_redaction_applied(self):
        # A secret in the message text must surface as DENIED_CHAT_MESSAGE_REDACTED (so the caller writes
        # the placeholder), proving the redaction path ran on the chat_message body BEFORE persistence.
        rid = self._start_run()
        secret = "sk-ant-api03-" + "A" * 40
        body = json.dumps({"role": "user", "text": f"my key is {secret}", "source": "driven"})
        rc, p = self.kz(
            "T6", "--agent-run-id", rid,
            "--payload-json", json.dumps({"event_kind": "chat_message", "marker": "point", "summary": "secret leak", "body": body}),
        )
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_CHAT_MESSAGE_REDACTED", p)
        self.assertNotIn(secret, json.dumps(p, sort_keys=True))
        database = self.root / "AI" / "db" / "kaizen.db"
        connection = turso.connect(str(database))
        try:
            rows = connection.execute(
                "SELECT summary, body, status_message, code, name FROM agent_events "
                "WHERE agent_run_id = ? AND event_kind = 'chat_message'",
                (rid,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(rows, [])
        self.assertNotIn(secret, json.dumps(rows, default=str))

    def test_q8_validates_chat_message_and_profile_events(self):
        # agent_event with the new kinds validates through Q8 (kind x marker registered).
        for kind in ("chat_message", "profile"):
            payload = json.dumps({"event_kind": kind, "marker": "point", "summary": f"{kind} event."})
            rc, p = self.kz("Q8", "--kind", "agent_event", "--payload-json", payload)
            self.assertEqual(rc, 0, f"{kind}: {p}")
            self.assertTrue(p.get("valid"), f"{kind}: {p}")

    def test_test_flag_purges(self):
        # A --test session and its --test children are all is_test=1 -> K7 removes them.
        sid = self._start_session()  # non-test session (kept)
        rc, t = self.kz("C1", "--summary", "Throwaway session.", "--test")
        self.assertEqual(rc, 0, t)
        tid = t["id"]
        for args in (
            ("C2", "--session-id", tid, "--body", "throwaway", "--test"),
            ("C3", "--session-id", tid, "--title", "tmp", "--summary", "throwaway goal.", "--test"),
            ("C4", "--session-id", tid, "--summary", "throwaway approval.", "--test"),
        ):
            seed_rc, seed_payload = self.kz(*args)
            self.assertEqual(seed_rc, 0, seed_payload)
        rc, purged = self.kz("K7")
        self.assertEqual(rc, 0, purged)
        self.assertEqual(purged["purged"].get("agent_sessions"), 1, purged)
        # The kept session still reads.
        rc, p = self.kz("C5", "--session-id", sid)
        self.assertEqual(rc, 0, p)
