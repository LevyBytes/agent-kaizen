"""Offline contract tests for test-extension layout, process ownership, evidence reconciliation, authoritative Turso records, action protocol, and cleanup."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import turso

from kaizen_components import db_schema
from kaizen_components.orchestration import test_extension
from kaizen_components.orchestration import supervisor
from test_session_drive import _DrivenSubprocess


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = REPO_ROOT / "AI" / "work" / "test-extension-offline-tests"


def capabilities() -> dict:
    return {
        "status": "OK",
        "engines": [
            {
                "id": "claude", "label": "Claude", "drivable": True,
                "availability": {"state": "available"}, "features": {"test_extension": True},
                "runtime": {"kind": "claude-agent-sdk", "status": "ready"},
                "models": [{"id": "discovered-claude", "label": "Discovered Claude", "reasoning_efforts": ["low", "high"]}],
            },
            {
                "id": "local_llm", "label": "Ollama", "drivable": True,
                "availability": {"state": "available"}, "features": {"test_extension": True},
                "models": [{"id": "discovered-ollama", "label": "Discovered Ollama", "reasoning_efforts": []}],
            },
        ],
    }


def start_control(
    *,
    provider: str = "claude",
    model: str = "discovered-claude",
    effort: str | None = "high",
    max_turns: int = 4,
    call_ceiling: int = 8,
    scenarios: list[str] | None = None,
    suite_nonce: str = "a" * 64,
) -> dict:
    value = {
        "v": 1,
        "action": "start",
        "suite_nonce": suite_nonce,
        "request_sha256": "",
        "provider": provider,
        "model": model,
        "effort": effort,
        "max_turns": max_turns,
        "call_ceiling": call_ceiling,
        "scenarios": scenarios or ["claude-text-stream", "claude-governed-context"],
        "provider_retries": 0,
    }
    value["request_sha256"] = test_extension._canonical_request_sha256(value)
    return value


def preapproval_stop(*, suite_nonce: str = "a" * 64, stop_id: str = "b" * 64,
                     request_sha256: str | None = None) -> dict:
    value = {
        "v": 1,
        "action": "stop",
        "suite_nonce": suite_nonce,
        "stop_id": stop_id,
        "reason": "user",
    }
    if request_sha256 is not None:
        value["request_sha256"] = request_sha256
    return value


def external_block(
    *,
    suite_nonce: str = "a" * 64,
    block_id: str = "b" * 64,
    provider: str = "claude",
    code: str = "DENIED_AUTH_UNAVAILABLE",
    scenarios: list[str] | None = None,
) -> dict:
    return {
        "v": 1,
        "action": "external_block",
        "suite_nonce": suite_nonce,
        "block_id": block_id,
        "provider": provider,
        "code": code,
        "scenarios": scenarios or ["claude-text-stream", "claude-governed-context"],
    }


def action_request(
    phase: str,
    sequence: int,
    action_id: str,
    suite: test_extension.SuiteRequest,
    **changes,
) -> dict:
    """Build the exact scenario-bound action request shape for a phase, including phase-specific locator and proof fields."""
    scenario = {
        "traversal.arm_zero_records": "claude-traversal-zero-record",
        "traversal.verify_zero_records": "claude-traversal-zero-record",
        "stale.mutate_base": "claude-diff-stale",
        "corrupt.snapshot": "claude-diff-corrupt",
        "writer.holder_open": "claude-writer-conflict",
        "writer.loser_arm": "claude-writer-conflict",
        "writer.loser_verify": "claude-writer-conflict",
        "writer.holder_release": "claude-writer-conflict",
        "restart.daemon": "claude-interrupt-restart",
        "restart.cleanup": "claude-interrupt-restart",
        "suite.stop": "suite",
    }[phase]
    value = {
        "protocol": test_extension.ACTION_PROTOCOL,
        "suite_nonce": suite.suite_nonce,
        "request_sha256": suite.request_sha256,
        "sequence": sequence,
        "action_id": action_id,
        "scenario_id": scenario,
        "phase": phase,
    }
    if phase == "traversal.verify_zero_records":
        value["denial_code"] = "DENIED_CONTEXT_INVALID"
    if phase in {
        "stale.mutate_base", "corrupt.snapshot", "writer.holder_open", "writer.loser_arm",
        "writer.loser_verify", "writer.holder_release",
    }:
        value.update({
            "conversation_id": "conversation-holder",
            "session_id": "session-holder",
            "agent_run_id": "run-holder",
            "correlation_id": "approval-holder",
            "revision": 1,
            "snapshot_set_sha256": "1" * 64,
            "workspace_path": "te-diff-target.txt",
            "before_sha256": "2" * 64,
            "proposed_sha256": "3" * 64,
        })
    if phase in {"writer.loser_arm", "writer.loser_verify"}:
        value.update({"loser_conversation_id": "conversation-loser", "loser_request_token": "4" * 64})
    if phase == "writer.loser_verify":
        value["denial_code"] = "DENIED_WORKSPACE_WRITER_BUSY"
    if phase == "restart.daemon":
        value.update({
            "conversation_id": "conversation-restart", "session_id": "session-restart",
            "agent_run_id": "run-old",
        })
    if phase == "restart.cleanup":
        value.update({
            "conversation_id": "conversation-restart", "session_id": "session-restart",
            "agent_run_id": "run-new", "previous_agent_run_id": "run-old",
        })
    if phase == "suite.stop":
        value["reason"] = "user"
    value.update(changes)
    return value


def write_edh_ready(layout: test_extension.RunLayout, *, shared_storage: bool = True, **changes) -> dict:
    manifest_bytes = layout.manifest.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    value = {
        "v": 1,
        "run_id": layout.run_id,
        "suite_nonce": manifest["suite_nonce"],
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        **changes,
    }
    layout.edh_ready.write_text(json.dumps(value), encoding="utf-8")
    if shared_storage:
        database = layout.shared_data / "sharedStorage" / "state.vscdb"
        database.parent.mkdir(parents=True, exist_ok=True)
        database.write_bytes(b"SQLite format 3\x00")
    return value


class FakeChild:
    def __init__(self, pid: int = 1234, *, owned_pids: set[int] | None = None) -> None:
        self.killed, self.returncode = 0, None
        self.pid = pid
        self.owned_pids = set(owned_pids or {pid})

    def poll(self):
        return self.returncode

    def owns_pid(self, pid: int) -> bool:
        return type(pid) is int and pid in self.owned_pids

    def kill_tree(self, timeout: float = 5.0) -> None:
        self.killed += 1
        self.returncode = 0


class TestExtensionTest(unittest.TestCase):
    """Offline OuterRunner protocol, authority, lifecycle, fail-closed, and evidence-integrity coverage over isolated test planes."""
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(prefix="case-", dir=TEST_TMP))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.source = self.temp / "source-workspace"
        self.source.mkdir()
        (self.source / "extension").mkdir()
        (self.source / "kaizen.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        self.base = self.temp / "plane-base"

    def layout(self, run_id: str = "te-offline-001") -> test_extension.RunLayout:
        return test_extension.create_layout(self.source, self.base, run_id=run_id)

    def approved_runner(
        self,
        scenarios: tuple[str, ...],
        run_id: str = "te-actions",
    ) -> tuple[test_extension.OuterRunner, test_extension.SuiteRequest]:
        layout = self.layout(run_id)
        test_extension.prepare_plane(layout, self.source)
        nonce = json.loads(layout.manifest.read_text(encoding="utf-8"))["suite_nonce"]
        ceiling = sum(4 if scenario == "claude-interrupt-restart" else 2 for scenario in scenarios)
        control = start_control(
            effort="low", max_turns=2, call_ceiling=max(2, ceiling),
            scenarios=list(scenarios), suite_nonce=nonce,
        )
        request = test_extension.validate_suite_request(control, capabilities())
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable), code_path=self.temp / "Code.exe",
        )
        runner._approved_request = request
        runner._write_suite_binding()
        return runner, request

    def authoritative_runner(
        self,
        scenario: str,
        *,
        run_id: str | None = None,
        call_ceiling: int = 2,
        provider: str = "claude",
    ) -> tuple[test_extension.OuterRunner, test_extension.SuiteRequest]:
        layout = self.layout(run_id or f"te-authority-{os.urandom(3).hex()}")
        test_extension.prepare_plane(layout, self.source)
        # Exercise the authority reader against the exact K1 Turso/MVCC file format and current
        # v1 DDL/additive-column contract rather than a mocked row interface.
        database = layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            connection.execute("PRAGMA journal_mode = 'mvcc'").fetchone()
            for statement in db_schema.DDL:
                connection.execute(statement)
            for table, column, definition in db_schema.ADDITIVE_COLUMNS:
                existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            for statement in (*db_schema.INDEXES, *db_schema.ADDITIVE_INDEX_SQL):
                connection.execute(statement)
        nonce = json.loads(layout.manifest.read_text(encoding="utf-8"))["suite_nonce"]
        model = "discovered-claude" if provider == "claude" else "discovered-ollama"
        effort = "low" if provider == "claude" else None
        control = start_control(
            provider=provider, model=model, effort=effort, max_turns=2, call_ceiling=call_ceiling,
            scenarios=[scenario], suite_nonce=nonce,
        )
        request = test_extension.validate_suite_request(control, capabilities())
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe",
        )
        runner._approved_request = request
        runner._suite_record_baseline = runner._durable_id_snapshot()
        return runner, request

    @staticmethod
    def insert_row(connection: Any, table: str, values: dict) -> None:
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )

    def insert_session_run(
        self,
        runner: test_extension.OuterRunner,
        request: test_extension.SuiteRequest,
        scenario: str,
        *,
        session_id: str,
        agent_run_id: str,
        session_state: str = "closed",
        run_state: str = "success",
    ) -> None:
        permission = runner._scenario_permission(scenario)
        engine = "claude" if request.provider == "claude" else "local_llm"
        auth = "subscription" if request.provider == "claude" else "none"
        profile_hash = "a" * 64
        database = runner.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            self.insert_row(connection, "agent_sessions", {
                "id": session_id,
                "created_at": "2026-01-01T00:00:00.000Z",
                "controller": "kaizen",
                "mode": "orchestrate",
                "engine": engine,
                "auth_mode": auth,
                "state": session_state,
                "summary": "test-extension authoritative session",
                "content_hash": "session-content-hash",
                "requested_model": request.model,
                "requested_reasoning_effort": request.effort,
                "permission_mode": permission,
                "profile_hash": profile_hash,
                "is_test": 1,
            })
            self.insert_row(connection, "agent_runs", {
                "id": agent_run_id,
                "created_at": "2026-01-01T00:00:01.000Z",
                "agent_type": "other",
                "surface": "app-server",
                "model": request.model,
                "state": run_state,
                "summary": "test-extension authoritative run",
                "body": "{}",
                "content_hash": "run-content-hash",
                "session_id": session_id,
                "engine": engine,
                "auth_mode": auth,
                "requested_model": request.model,
                "requested_reasoning_effort": request.effort,
                "reasoning_effort": request.effort,
                "permission_mode": permission,
                "profile_hash": profile_hash,
                "is_test": 1,
            })

    def insert_event(
        self,
        connection: Any,
        agent_run_id: str,
        sequence_no: int,
        event_kind: str,
        marker: str,
        body: dict,
        *,
        correlation_id: str | None = None,
        source_event_id: str | None = None,
        code: str | None = None,
        name: str | None = None,
        summary: str | None = None,
    ) -> None:
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
        self.insert_row(connection, "agent_events", {
            "id": f"{agent_run_id}-event-{sequence_no}",
            "created_at": f"2026-01-01T00:00:{sequence_no + 1:02d}.000Z",
            "agent_run_id": agent_run_id,
            "sequence_no": sequence_no,
            "source_event_id": source_event_id,
            "correlation_id": correlation_id,
            "event_kind": event_kind,
            "marker": marker,
            "code": code,
            "name": name,
            "summary": summary or f"{event_kind} {marker}",
            "body": encoded,
            "content_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        })

    @staticmethod
    def profile_body(
        request: test_extension.SuiteRequest,
        permission: str,
        *,
        resumed: bool = False,
    ) -> dict[str, Any]:
        auth = "subscription" if request.provider == "claude" else "none"
        selected = {
            "model": request.model,
            "reasoning_effort": request.effort,
            "permission_mode": permission,
            "auth_mode": auth,
            "max_turns": request.max_turns,
        }
        profile: dict[str, Any] = {
            "requested": dict(selected), "effective": dict(selected), "profile_hash": "a" * 64,
        }
        if resumed:
            profile.update({"resume_fidelity": "reduced", "omitted_message_count": 1})
        return profile

    def seed_structured_codeword_authority(
        self,
        runner: test_extension.OuterRunner,
        request: test_extension.SuiteRequest,
        *,
        session_id: str,
        agent_run_id: str,
    ) -> dict:
        from kaizen_components.orchestration.artifact_cache import ArtifactCache

        scenario = request.scenarios[0]
        self.insert_session_run(
            runner, request, scenario, session_id=session_id, agent_run_id=agent_run_id,
        )
        manifest = json.loads(runner.layout.manifest.read_text(encoding="utf-8"))
        cache = ArtifactCache(runner.layout.workspace)
        user: dict[str, Any] = {"role": "user", "source": "driven", "text": "use exact staged references"}
        if scenario == "claude-image-codeword":
            image = (runner.layout.workspace / manifest["fixtures"]["image"]).read_bytes()
            stored = cache.store(
                "images", image, scope_id=session_id, media_type="image/png", origin="test-extension",
            )
            user["attachments"] = [{
                "id": "image-one", "kind": "image", "artifact_ref": stored.artifact_ref,
                "sha256": stored.sha256, "bytes": stored.bytes, "media_type": "image/png",
                "name": "te-image.png",
            }]
            codewords = (manifest["codewords"]["image"],)
        else:
            context_path = str(manifest["fixtures"]["context"])
            selection_path = str(manifest["fixtures"]["selection"])
            context_bytes = (runner.layout.workspace / context_path).read_bytes()
            exact_selection = f"unsaved dirty selection codeword {manifest['codewords']['selection']}"
            selection_bytes = exact_selection.encode("utf-8")
            stored = cache.store("context", selection_bytes, scope_id=session_id, origin="test-extension")
            user["context_refs"] = [
                {
                    "id": "context-file", "kind": "file", "source_path": context_path,
                    "sha256": hashlib.sha256(context_bytes).hexdigest(), "bytes": len(context_bytes),
                    "encoding": "utf-8",
                },
                {
                    "id": "context-selection", "kind": "selection", "source_path": selection_path,
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": len(exact_selection)},
                    },
                    "snapshot_ref": stored.artifact_ref, "sha256": stored.sha256,
                    "bytes": stored.bytes, "encoding": "utf-8",
                },
            ]
            codewords = (manifest["codewords"]["context"], manifest["codewords"]["selection"])
        turn_id = f"{agent_run_id}-turn"
        assistant_text = "verified " + " ".join(codewords)
        database = runner.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            self.insert_event(connection, agent_run_id, 1, "profile", "point", self.profile_body(request, "plan"))
            self.insert_event(connection, agent_run_id, 2, "chat_message", "point", user)
            self.insert_event(
                connection, agent_run_id, 3, "turn", "open", {"turn_id": turn_id},
                correlation_id=turn_id,
            )
            self.insert_event(
                connection, agent_run_id, 4, "turn", "close_ok", {"status": "OK", "num_turns": 1},
                correlation_id=turn_id,
            )
            self.insert_event(connection, agent_run_id, 5, "chat_message", "point", {
                "role": "assistant", "source": "driven", "text": assistant_text, "turn_id": turn_id,
            }, correlation_id=turn_id)
            self.insert_event(connection, agent_run_id, 6, "finalization", "close_ok", {})
        proof = {
            "scenario": scenario, "provider": request.provider, "calls_reserved": request.max_turns,
            "profile_attested": True, "attested_model": request.model,
            "attested_effort": request.effort, "codeword_seen": True,
            "session_id": session_id, "agent_run_id": agent_run_id,
        }
        if scenario == "claude-image-codeword":
            proof["image_refs"] = 1
        else:
            proof.update({"context_refs": 2, "selection_exact": True})
        return proof

    @staticmethod
    def diff_open_body(
        revision: int,
        path: str,
        before: bytes,
        proposed: bytes,
    ) -> dict[str, Any]:
        from kaizen_components.orchestration.approvals import canonical_snapshot_set_sha256

        body = {
            "approval_revision": revision,
            "expires_at": "2030-01-01T00:00:00Z",
            "snapshot_set_sha256": "0" * 64,
            "file_changes": [{
                "change_id": f"change-{revision}", "path": path, "kind": "modify", "old_path": None,
                "preview_mode": "text", "preview_reason": None,
                "before": {
                    "artifact_ref": f"sha256:{hashlib.sha256(before).hexdigest()}",
                    "sha256": hashlib.sha256(before).hexdigest(), "bytes": len(before),
                    "encoding": "utf-8", "media_type": None,
                },
                "proposed": {
                    "artifact_ref": f"sha256:{hashlib.sha256(proposed).hexdigest()}",
                    "sha256": hashlib.sha256(proposed).hexdigest(), "bytes": len(proposed),
                    "encoding": "utf-8", "media_type": None,
                },
            }],
        }
        body["snapshot_set_sha256"] = canonical_snapshot_set_sha256(body)
        return body

    def seed_diff_authority(
        self,
        runner: test_extension.OuterRunner,
        request: test_extension.SuiteRequest,
        *,
        session_id: str,
        agent_run_id: str,
        approval_id: str,
    ) -> dict:
        """Seed authoritative C4, event, cache, and final-byte state for accept, reject, stale, corrupt, timeout, and writer-conflict scenarios."""
        from kaizen_components.orchestration.artifact_cache import ArtifactCache

        scenario = request.scenarios[0]
        fatal = scenario in {"claude-diff-corrupt", "claude-diff-timeout"}
        self.insert_session_run(
            runner, request, scenario, session_id=session_id, agent_run_id=agent_run_id,
            session_state="failed" if fatal else "closed", run_state="failed" if fatal else "success",
        )
        manifest = json.loads(runner.layout.manifest.read_text(encoding="utf-8"))
        relative_path = str(manifest["fixtures"]["diff_target"])
        target = runner.layout.workspace / relative_path
        before = target.read_bytes()
        first_proposed = b"Test Extension first proposed content\n"
        mutated = b"Test Extension controlled stale-base mutation\n" + before
        final_proposed = b"Test Extension rebased proposed content\n"
        cache = ArtifactCache(runner.layout.workspace)
        for content in {before, first_proposed}:
            cache.store("diffs", content, scope_id=approval_id, origin="test-extension")
        open_bodies = [self.diff_open_body(1, relative_path, before, first_proposed)]
        if scenario == "claude-diff-stale":
            for content in {mutated, final_proposed}:
                cache.store("diffs", content, scope_id=approval_id, origin="test-extension")
            open_bodies.append(self.diff_open_body(2, relative_path, mutated, final_proposed))

        terminal_marker = "resolved" if scenario in {"claude-diff-accept", "claude-diff-stale"} else (
            "timed_out" if scenario == "claude-diff-timeout" else "declined"
        )
        state = "approved" if terminal_marker == "resolved" else "denied"
        decided_by = "auto" if scenario in {"claude-diff-corrupt", "claude-diff-timeout"} else "human"
        denial_code = {
            "claude-diff-corrupt": "DENIED_APPROVAL_SNAPSHOT_INVALID",
            "claude-diff-timeout": "DENIED_APPROVAL_TIMEOUT",
        }.get(scenario)
        tool_terminal = "close_ok" if terminal_marker == "resolved" else "close_fail"
        tool_code = denial_code or (None if tool_terminal == "close_ok" else "DENIED_TOOL_REJECTED")
        approval_correlation = f"{agent_run_id}-approval"
        tool_correlation = f"{agent_run_id}-tool"
        turn_id = f"{agent_run_id}-turn"

        if scenario in {"claude-diff-accept", "claude-diff-stale"}:
            target.write_bytes(final_proposed if scenario == "claude-diff-stale" else first_proposed)
        else:
            target.write_bytes(before)
        corrupted_sha: str | None = None
        if scenario == "claude-diff-corrupt":
            declared = hashlib.sha256(first_proposed).hexdigest()
            object_path = cache.kind_root("diffs") / declared
            object_path.write_bytes(b"TEST_EXTENSION_CONTROLLED_CORRUPTION\n")
            corrupted_sha = hashlib.sha256(object_path.read_bytes()).hexdigest()

        database = runner.layout.workspace / "AI" / "db" / "kaizen.db"
        sequence = 1
        with turso.connect(str(database)) as connection:
            self.insert_event(connection, agent_run_id, sequence, "profile", "point", self.profile_body(request, "ask"))
            sequence += 1
            self.insert_event(connection, agent_run_id, sequence, "chat_message", "point", {
                "role": "user", "source": "driven", "text": "propose the bounded fixture change",
            })
            sequence += 1
            self.insert_event(
                connection, agent_run_id, sequence, "turn", "open", {"turn_id": turn_id},
                correlation_id=turn_id,
            )
            sequence += 1
            self.insert_event(connection, agent_run_id, sequence, "tool_call", "open", {
                "name": "kaizen_propose_changes", "tool_call_id": tool_correlation,
            }, correlation_id=tool_correlation, name="kaizen_propose_changes")
            sequence += 1
            for body in open_bodies:
                self.insert_event(
                    connection, agent_run_id, sequence, "approval", "open", body,
                    correlation_id=approval_correlation,
                    source_event_id=f"approval:{approval_id}:open:{body['approval_revision']}",
                    name="diff_snapshot",
                )
                sequence += 1
            terminal_body = {"decision": state}
            if denial_code is not None:
                terminal_body = {
                    "decision": "denied", "denial_code": denial_code,
                    "presentation": "timed_out" if scenario == "claude-diff-timeout" else "unavailable",
                }
            self.insert_event(
                connection, agent_run_id, sequence, "approval", terminal_marker, terminal_body,
                correlation_id=approval_correlation, source_event_id=f"approval:{approval_id}:terminal",
                code=denial_code,
            )
            sequence += 1
            self.insert_event(connection, agent_run_id, sequence, "tool_call", tool_terminal, {
                "name": "kaizen_propose_changes", "tool_call_id": tool_correlation,
                "result": {"applied": tool_terminal == "close_ok"},
            }, correlation_id=tool_correlation, code=tool_code, name="kaizen_propose_changes")
            sequence += 1
            self.insert_event(
                connection, agent_run_id, sequence, "turn", "close_fail" if fatal else "close_ok",
                {"status": "DENIED" if fatal else "OK", "num_turns": 1}, correlation_id=turn_id,
            )
            sequence += 1
            if not fatal:
                self.insert_event(connection, agent_run_id, sequence, "chat_message", "point", {
                    "role": "assistant", "source": "driven", "text": "bounded diff complete",
                    "turn_id": turn_id,
                }, correlation_id=turn_id)
                sequence += 1
            self.insert_event(
                connection, agent_run_id, sequence, "finalization", "close_fail" if fatal else "close_ok", {},
            )
            self.insert_row(connection, "approval_requests", {
                "id": approval_id, "created_at": "2026-01-01T00:00:06.000Z",
                "updated_at": "2026-01-01T00:00:07.000Z", "session_id": session_id,
                "correlation_id": approval_correlation, "request_type": "tool_approval", "state": state,
                "decided_by": decided_by, "summary": "bounded diff approval",
                "content_hash": "approval-content-hash", "is_test": 1,
            })

        first_body, latest_body = open_bodies[0], open_bodies[-1]
        first_before = first_body["file_changes"][0]["before"]["sha256"]
        latest_before = latest_body["file_changes"][0]["before"]["sha256"]
        latest_proposed = latest_body["file_changes"][0]["proposed"]["sha256"]
        proof: dict[str, Any] = {
            "scenario": scenario, "provider": "claude", "calls_reserved": request.max_turns,
            "profile_attested": True, "attested_model": request.model, "attested_effort": request.effort,
            "approval_ui": True, "approval_status": state, "diff_card_count": 1,
            "diff_path_matches": True, "diff_before_matches": True, "diff_proposed_hashed": True,
            "diff_final_matches": True, "diff_revision": 1, "diff_path": relative_path,
            "diff_snapshot_set_sha256": first_body["snapshot_set_sha256"],
            "diff_before_sha256": first_before, "diff_proposed_sha256": latest_proposed,
            "diff_final_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "session_id": session_id, "agent_run_id": agent_run_id,
            "correlation_id": approval_correlation, "tool_correlation_id": tool_correlation,
            "approval_id": approval_id,
        }
        if scenario == "claude-diff-accept":
            proof.update({"decision": "approve"})
        elif scenario == "claude-diff-reject":
            proof.update({"decision": "deny"})
        elif scenario == "claude-diff-stale":
            proof.update({
                "decision": "approve", "diff_revision_refreshed": True, "diff_second_accept": True,
                "diff_refreshed_revision": 2,
                "diff_refreshed_snapshot_set_sha256": latest_body["snapshot_set_sha256"],
                "diff_mutated_base_sha256": latest_before,
                "diff_refreshed_before_sha256": latest_before,
            })
            runner._action_proof["stale"] = {
                "path": relative_path, "before": first_before, "mutated": latest_before,
                "binding": {
                    "session_id": session_id, "agent_run_id": agent_run_id,
                    "correlation_id": approval_correlation, "revision": 1,
                    "snapshot_set_sha256": first_body["snapshot_set_sha256"],
                },
            }
        elif scenario == "claude-diff-corrupt":
            proof.update({
                "decision": "approve", "diff_corrupt": True, "diff_authoritative_denial": True,
                "denial_code": denial_code, "diff_corrupted_artifact_sha256": corrupted_sha,
            })
            runner._action_proof["corrupt"] = {
                "digest": latest_proposed, "corrupted": corrupted_sha,
                "binding": {
                    "session_id": session_id, "agent_run_id": agent_run_id,
                    "correlation_id": approval_correlation, "revision": 1,
                    "snapshot_set_sha256": first_body["snapshot_set_sha256"],
                },
            }
        elif scenario == "claude-diff-timeout":
            proof["approval_status"] = "timed_out"
        elif scenario == "claude-writer-conflict":
            proof.update({
                "decision": "deny", "denial_code": "DENIED_WORKSPACE_WRITER_BUSY", "zero_records": True,
                "writer_conflict_seen": True, "writer_loser_zero_records": True,
                "writer_lease_retained": True, "writer_lease_released": True,
            })
            runner._scenario_bindings[scenario] = {
                "session_id": session_id, "agent_run_id": agent_run_id,
                "correlation_id": approval_correlation,
            }
            runner._action_proof["writer"] = {
                "zero_records": True, "retained": True, "released": True, "direct_denied": True,
            }
        return proof

    def seed_cleanup_authority(
        self,
        runner: test_extension.OuterRunner,
        request: test_extension.SuiteRequest,
        *,
        session_id: str,
        agent_run_id: str,
    ) -> dict:
        scenario = "cleanup-leak-state"
        self.insert_session_run(
            runner, request, scenario, session_id=session_id, agent_run_id=agent_run_id,
        )
        manifest = json.loads(runner.layout.manifest.read_text(encoding="utf-8"))
        fixture_path = str(manifest["fixtures"]["context"])
        fixture = runner.layout.workspace / fixture_path
        tool_id, turn_id = f"{agent_run_id}-tool", f"{agent_run_id}-turn"
        database = runner.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            self.insert_event(connection, agent_run_id, 1, "profile", "point", self.profile_body(request, "plan"))
            self.insert_event(connection, agent_run_id, 2, "chat_message", "point", {
                "role": "user", "source": "driven", "text": "read the bounded cleanup fixture",
            })
            self.insert_event(connection, agent_run_id, 3, "turn", "open", {"turn_id": turn_id}, correlation_id=turn_id)
            self.insert_event(connection, agent_run_id, 4, "tool_call", "open", {
                "name": "kaizen_read_file", "tool_call_id": tool_id, "path": fixture_path,
            }, correlation_id=tool_id, name="kaizen_read_file")
            self.insert_event(connection, agent_run_id, 5, "tool_call", "close_ok", {
                "name": "kaizen_read_file", "tool_call_id": tool_id,
                "result": {
                    "path": fixture_path, "sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
                    "truncated": False,
                },
            }, correlation_id=tool_id, name="kaizen_read_file")
            self.insert_event(
                connection, agent_run_id, 6, "turn", "close_ok", {"status": "OK", "num_turns": 1},
                correlation_id=turn_id,
            )
            self.insert_event(connection, agent_run_id, 7, "chat_message", "point", {
                "role": "assistant", "source": "driven", "text": "bounded cleanup read complete",
                "turn_id": turn_id,
            }, correlation_id=turn_id)
            self.insert_event(connection, agent_run_id, 8, "finalization", "close_ok", {})
        return {
            "scenario": scenario, "provider": "claude", "calls_reserved": request.max_turns,
            "profile_attested": True, "attested_model": request.model, "attested_effort": request.effort,
            "conversation_idle": True, "pending_approvals": 0, "running_cards": 0,
            "session_id": session_id, "agent_run_id": agent_run_id,
        }

    def seed_restart_authority(
        self,
        runner: test_extension.OuterRunner,
        request: test_extension.SuiteRequest,
        *,
        session_id: str = "session-restart",
        old_run_id: str = "run-restart-old",
        new_run_id: str = "run-restart-new",
    ) -> dict:
        scenario = "claude-interrupt-restart"
        self.insert_session_run(
            runner, request, scenario, session_id=session_id, agent_run_id=old_run_id,
        )
        database = runner.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            self.insert_row(connection, "agent_runs", {
                "id": new_run_id, "created_at": "2026-01-01T00:00:20.000Z",
                "agent_type": "other", "surface": "app-server", "model": request.model,
                "state": "success", "summary": "test-extension resumed run", "body": "{}",
                "content_hash": "resumed-run-content-hash", "session_id": session_id,
                "engine": "claude", "auth_mode": "subscription", "requested_model": request.model,
                "requested_reasoning_effort": request.effort, "reasoning_effort": request.effort,
                "permission_mode": "plan", "profile_hash": "a" * 64, "is_test": 1,
            })
            old_turn = f"{old_run_id}-turn"
            self.insert_event(connection, old_run_id, 1, "profile", "point", self.profile_body(request, "plan"))
            self.insert_event(connection, old_run_id, 2, "chat_message", "point", {
                "role": "user", "source": "driven", "text": "start bounded interrupted work",
            })
            self.insert_event(
                connection, old_run_id, 3, "turn", "open", {"turn_id": old_turn}, correlation_id=old_turn,
            )
            self.insert_event(
                connection, old_run_id, 4, "turn", "close_canceled", {"status": "CANCELED", "num_turns": 0},
                correlation_id=old_turn,
            )
            self.insert_event(
                connection, old_run_id, 5, "finalization", "close_canceled", {}, code="canceled",
                summary="orphan-sweep force-finalize: owning daemon dead",
            )
            new_turn = f"{new_run_id}-turn"
            self.insert_event(
                connection, new_run_id, 1, "profile", "point", self.profile_body(request, "plan", resumed=True),
            )
            self.insert_event(connection, new_run_id, 2, "chat_message", "point", {
                "role": "user", "source": "driven", "text": "continue bounded interrupted work",
            })
            self.insert_event(
                connection, new_run_id, 3, "turn", "open", {"turn_id": new_turn}, correlation_id=new_turn,
            )
            self.insert_event(
                connection, new_run_id, 4, "turn", "close_ok", {"status": "OK", "num_turns": 1},
                correlation_id=new_turn,
            )
            codeword = json.loads(runner.layout.manifest.read_text(encoding="utf-8"))["codewords"]["text"]
            self.insert_event(connection, new_run_id, 5, "chat_message", "point", {
                "role": "assistant", "source": "driven", "text": f"resumed {codeword}", "turn_id": new_turn,
            }, correlation_id=new_turn)
            self.insert_event(connection, new_run_id, 6, "finalization", "close_ok", {})
        runner._scenario_bindings[scenario] = {
            "conversation_id": "conversation-restart", "session_id": session_id, "agent_run_id": old_run_id,
        }
        runner._action_proof["restart"] = {"restarted": True, "clean": True}
        return {
            "scenario": scenario, "provider": "claude", "calls_reserved": request.max_turns * 2,
            "profile_attested": True, "attested_model": request.model, "attested_effort": request.effort,
            "interrupted": True, "restart_resumable": True, "restart_same_session": True,
            "restart_new_run": True, "restart_reduced_fidelity": True, "queue_empty": True,
            "runtime_children_clean": True, "conversation_idle": True,
            "session_id": session_id, "previous_agent_run_id": old_run_id, "agent_run_id": new_run_id,
        }

    def seed_text_authority(
        self,
        runner: test_extension.OuterRunner,
        request: test_extension.SuiteRequest,
        *,
        session_id: str = "session-text",
        agent_run_id: str = "run-text",
        provider_calls: int | bool = 1,
    ) -> dict:
        scenario = request.scenarios[0]
        self.insert_session_run(
            runner, request, scenario, session_id=session_id, agent_run_id=agent_run_id,
        )
        permission = runner._scenario_permission(scenario)
        auth = "subscription" if request.provider == "claude" else "none"
        profile = {
            "requested": {
                "model": request.model, "reasoning_effort": request.effort,
                "permission_mode": permission, "auth_mode": auth, "max_turns": request.max_turns,
            },
            "effective": {
                "model": request.model, "reasoning_effort": request.effort,
                "permission_mode": permission, "auth_mode": auth, "max_turns": request.max_turns,
            },
            "profile_hash": "a" * 64,
        }
        turn_id = f"{agent_run_id}-turn"
        codeword = json.loads(runner.layout.manifest.read_text(encoding="utf-8"))["codewords"]["text"]
        count_key = "num_turns" if request.provider == "claude" else "iterations"
        database = runner.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            self.insert_event(connection, agent_run_id, 1, "profile", "point", profile)
            self.insert_event(connection, agent_run_id, 2, "chat_message", "point", {
                "role": "user", "source": "driven", "text": "return the bounded codeword",
            })
            self.insert_event(
                connection, agent_run_id, 3, "turn", "open", {"turn_id": turn_id},
                correlation_id=turn_id,
            )
            self.insert_event(
                connection, agent_run_id, 4, "turn", "close_ok",
                {"status": "OK", count_key: provider_calls}, correlation_id=turn_id,
            )
            self.insert_event(connection, agent_run_id, 5, "chat_message", "point", {
                "role": "assistant", "source": "driven", "text": f"verified {codeword}",
                "turn_id": turn_id,
            }, correlation_id=turn_id)
            self.insert_event(connection, agent_run_id, 6, "finalization", "close_ok", {})
        return {
            "scenario": scenario,
            "provider": request.provider,
            "calls_reserved": request.max_turns,
            "profile_attested": True,
            "attested_model": request.model,
            "attested_effort": request.effort,
            "codeword_seen": True,
            "delta_count": 2,
            "ordered": True,
            "durable_replacements": 1,
            "stream_suppressed": False,
            "session_id": session_id,
            "agent_run_id": agent_run_id,
        }

    def seed_process_authority(
        self,
        runner: test_extension.OuterRunner,
        request: test_extension.SuiteRequest,
        *,
        session_id: str = "session-process",
        agent_run_id: str = "run-process",
        approval_id: str = "approval-process",
    ) -> dict:
        scenario = "claude-process-approval"
        self.insert_session_run(
            runner, request, scenario, session_id=session_id, agent_run_id=agent_run_id,
        )
        turn_id = f"{agent_run_id}-turn"
        tool_id = f"{agent_run_id}-tool"
        approval_correlation = f"{agent_run_id}-approval"
        process_body = {
            "tool": "kaizen_run_process",
            "executable": str(runner.python),
            "argv": ["-c", "print('TE_PROCESS_OK')"],
            "cwd": ".",
            "timeout_ms": 5000,
        }
        tool_open = {
            "name": "kaizen_run_process",
            "tool_call_id": tool_id,
            "executable": str(runner.python),
            "argv": ["-c", "print('TE_PROCESS_OK')"],
            "cwd": ".",
            "timeout_ms": 5000,
        }
        tool_close = {
            "name": "kaizen_run_process",
            "tool_call_id": tool_id,
            "result": {
                "exit_code": 0,
                "timed_out": False,
                "truncated": False,
                "effects_unknown": True,
                "policy_decision": "ask",
                "stdout_sha256": hashlib.sha256(b"TE_PROCESS_OK\n").hexdigest(),
                "stdout_bytes": len(b"TE_PROCESS_OK\n"),
            },
        }
        profile = {
            "requested": {
                "model": request.model, "reasoning_effort": request.effort,
                "permission_mode": "ask", "auth_mode": "subscription", "max_turns": request.max_turns,
            },
            "effective": {
                "model": request.model, "reasoning_effort": request.effort,
                "permission_mode": "ask", "auth_mode": "subscription", "max_turns": request.max_turns,
            },
            "profile_hash": "a" * 64,
        }
        database = runner.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            self.insert_event(connection, agent_run_id, 1, "profile", "point", profile)
            self.insert_event(connection, agent_run_id, 2, "chat_message", "point", {
                "role": "user", "source": "driven", "text": "run the bounded process",
            })
            self.insert_event(
                connection, agent_run_id, 3, "turn", "open", {"turn_id": turn_id},
                correlation_id=turn_id,
            )
            self.insert_event(
                connection, agent_run_id, 4, "tool_call", "open", tool_open,
                correlation_id=tool_id, name="kaizen_run_process",
            )
            self.insert_event(
                connection, agent_run_id, 5, "approval", "open", process_body,
                correlation_id=approval_correlation,
                source_event_id=f"approval:{approval_id}:open:1",
            )
            self.insert_event(
                connection, agent_run_id, 6, "approval", "resolved", {"decision": "approved"},
                correlation_id=approval_correlation,
                source_event_id=f"approval:{approval_id}:terminal",
            )
            self.insert_event(
                connection, agent_run_id, 7, "tool_call", "close_ok", tool_close,
                correlation_id=tool_id, name="kaizen_run_process",
            )
            self.insert_event(
                connection, agent_run_id, 8, "turn", "close_ok", {"status": "OK", "num_turns": 1},
                correlation_id=turn_id,
            )
            self.insert_event(connection, agent_run_id, 9, "chat_message", "point", {
                "role": "assistant", "source": "driven", "text": "bounded process complete",
                "turn_id": turn_id,
            }, correlation_id=turn_id)
            self.insert_event(connection, agent_run_id, 10, "finalization", "close_ok", {})
            self.insert_row(connection, "approval_requests", {
                "id": approval_id,
                "created_at": "2026-01-01T00:00:06.000Z",
                "updated_at": "2026-01-01T00:00:07.000Z",
                "session_id": session_id,
                "correlation_id": approval_correlation,
                "request_type": "tool_approval",
                "state": "approved",
                "decided_by": "human",
                "summary": "bounded process approval",
                "content_hash": "approval-content-hash",
                "is_test": 1,
            })
        return {
            "scenario": scenario,
            "provider": request.provider,
            "calls_reserved": request.max_turns,
            "profile_attested": True,
            "attested_model": request.model,
            "attested_effort": request.effort,
            "approval_ui": True,
            "decision": "approve",
            "approval_status": "approved",
            "tool_card_count": 1,
            "tool_status": "ok",
            "tool_request_matches": True,
            "tool_result_matches": True,
            "tool_decision_approved": True,
            "tool_output_complete": True,
            "process_exit_code": 0,
            "process_stdout_bytes": len(b"TE_PROCESS_OK\n"),
            "process_request_sha256": test_extension._process_request_sha256(
                runner.python, ("-c", "print('TE_PROCESS_OK')"), ".", 5000,
            ),
            "process_stdout_sha256": hashlib.sha256(b"TE_PROCESS_OK\n").hexdigest(),
            "session_id": session_id,
            "agent_run_id": agent_run_id,
            "correlation_id": approval_correlation,
            "tool_correlation_id": tool_id,
            "approval_id": approval_id,
        }

    def seed_plan_process_denial_authority(
        self,
        runner: test_extension.OuterRunner,
        request: test_extension.SuiteRequest,
        *,
        session_id: str = "session-plan",
        agent_run_id: str = "run-plan",
    ) -> dict:
        scenario = request.scenarios[0]
        self.insert_session_run(
            runner, request, scenario, session_id=session_id, agent_run_id=agent_run_id,
        )
        turn_id = f"{agent_run_id}-turn"
        tool_id = f"{agent_run_id}-tool"
        invariant_tool_id = f"{agent_run_id}-invariant-tool"
        auth = "subscription" if request.provider == "claude" else "none"
        profile = {
            "requested": {
                "model": request.model, "reasoning_effort": request.effort,
                "permission_mode": "plan", "auth_mode": auth, "max_turns": request.max_turns,
            },
            "effective": {
                "model": request.model, "reasoning_effort": request.effort,
                "permission_mode": "plan", "auth_mode": auth, "max_turns": request.max_turns,
            },
            "profile_hash": "a" * 64,
        }
        tool_open = {
            "name": "kaizen_run_process", "tool_call_id": tool_id,
            "executable": str(runner.python), "argv": ["-c", "print('MUST_NOT_RUN')"],
            "cwd": ".", "timeout_ms": 5000,
        }
        tool_close = {"name": "kaizen_run_process", "tool_call_id": tool_id, "result": {}}
        invariant_open = {
            "name": "kaizen_run_process", "tool_call_id": invariant_tool_id,
            "executable": "git", "argv": ["push", "origin", "main"],
            "cwd": ".", "timeout_ms": 5000,
        }
        invariant_close = {
            "name": "kaizen_run_process", "tool_call_id": invariant_tool_id, "result": {},
        }
        prove_invariant = scenario == "claude-plan-controls"
        count_key = "num_turns" if request.provider == "claude" else "iterations"
        database = runner.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            self.insert_event(connection, agent_run_id, 1, "profile", "point", profile)
            self.insert_event(connection, agent_run_id, 2, "chat_message", "point", {
                "role": "user", "source": "driven", "text": "attempt the bounded denied process",
            })
            self.insert_event(
                connection, agent_run_id, 3, "turn", "open", {"turn_id": turn_id},
                correlation_id=turn_id,
            )
            self.insert_event(
                connection, agent_run_id, 4, "tool_call", "open", tool_open,
                correlation_id=tool_id, name="kaizen_run_process",
            )
            self.insert_event(
                connection, agent_run_id, 5, "tool_call", "close_fail", tool_close,
                correlation_id=tool_id, code="MODE_CEILING:exec", name="kaizen_run_process",
            )
            sequence = 6
            if prove_invariant:
                self.insert_event(
                    connection, agent_run_id, sequence, "tool_call", "open", invariant_open,
                    correlation_id=invariant_tool_id, name="kaizen_run_process",
                )
                sequence += 1
                self.insert_event(
                    connection, agent_run_id, sequence, "tool_call", "close_fail", invariant_close,
                    correlation_id=invariant_tool_id, code="INV_GIT_PUSH", name="kaizen_run_process",
                )
                sequence += 1
            self.insert_event(
                connection, agent_run_id, sequence, "turn", "close_ok", {"status": "OK", count_key: 1},
                correlation_id=turn_id,
            )
            sequence += 1
            self.insert_event(connection, agent_run_id, sequence, "chat_message", "point", {
                "role": "assistant", "source": "driven", "text": "bounded process denied",
                "turn_id": turn_id,
            }, correlation_id=turn_id)
            sequence += 1
            self.insert_event(connection, agent_run_id, sequence, "finalization", "close_ok", {})
        proof = {
            "scenario": scenario,
            "provider": request.provider,
            "calls_reserved": request.max_turns,
            "profile_attested": True,
            "attested_model": request.model,
            "tool_card_count": 2 if prove_invariant else 1,
            "tool_status": "blocked",
            "tool_request_matches": True,
            "tool_zero_execution": True,
            "tool_zero_stdout": True,
            "process_stdout_bytes": 0,
            "denial_code": "MODE_CEILING:exec",
            "process_request_sha256": test_extension._process_request_sha256(
                runner.python, ("-c", "print('MUST_NOT_RUN')"), ".", 5000,
            ),
            "session_id": session_id,
            "agent_run_id": agent_run_id,
            "correlation_id": tool_id,
        }
        if prove_invariant:
            proof["invariant_denial_code"] = "INV_GIT_PUSH"
        if request.effort is not None:
            proof["attested_effort"] = request.effort
        return proof

    def test_layout_is_fresh_and_outside_source_workspace(self) -> None:
        layout = self.layout()
        self.assertFalse(layout.root.exists())
        self.assertTrue(layout.run_id.startswith("te-"))
        with self.assertRaisesRegex(test_extension.TestExtensionError, "outside the source workspace"):
            test_extension.create_layout(self.source, self.source / "AI" / "work" / "bad", run_id="te-bad")

    def test_prepare_plane_has_fake_git_boundary_and_deterministic_fixtures(self) -> None:
        layout = self.layout()
        words = test_extension.prepare_plane(layout, self.source)
        self.assertEqual(
            (layout.workspace / ".git" / "HEAD").read_text(encoding="utf-8"),
            "ref: refs/heads/test-extension-fixture\n",
        )
        self.assertIn(words["context"], (layout.workspace / "te-context.txt").read_text(encoding="utf-8"))
        png = (layout.workspace / "te-image.png").read_bytes()
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(png.endswith(b"IEND\xaeB`\x82"))
        manifest = json.loads(layout.manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["limits"], {"max_simultaneous_turns": 2, "max_wall_seconds": 1800, "provider_retries": 0})
        self.assertEqual(manifest["workspace_path"], str(layout.workspace.resolve()))
        self.assertNotIn(str(self.source), layout.manifest.read_text(encoding="utf-8"))

    def test_private_manifest_rejects_forged_or_out_of_plane_workspace(self) -> None:
        layout = self.layout("te-manifest-workspace")
        test_extension.prepare_plane(layout, self.source)
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe",
        )
        original = json.loads(layout.manifest.read_text(encoding="utf-8"))
        self.assertEqual(runner._manifest_payload()["workspace_path"], str(layout.workspace.resolve()))
        for forged in (
            "fixture-workspace",
            str(layout.root.parent / "fixture-workspace"),
            str(layout.root / "other-workspace"),
            str(layout.root / "nested" / ".." / "fixture-workspace"),
        ):
            with self.subTest(workspace_path=forged):
                layout.manifest.write_text(
                    json.dumps({**original, "workspace_path": forged}), encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    test_extension.TestExtensionError, "manifest workspace is invalid",
                ) as raised:
                    runner._manifest_payload()
                self.assertEqual(raised.exception.code, "DENIED_TEST_EXTENSION_MANIFEST")
        layout.manifest.write_text(json.dumps(original), encoding="utf-8")
        self.assertEqual(runner._manifest_payload()["workspace_path"], str(layout.workspace.resolve()))

    def test_authority_reader_opens_a_real_k1_turso_mvcc_plane(self) -> None:
        (self.source / "kaizen.py").write_text(
            "import runpy, sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            f"runpy.run_path({str(REPO_ROOT / 'kaizen.py')!r}, run_name='__main__')\n",
            encoding="utf-8",
        )
        layout = self.layout("te-real-k1-mvcc")
        test_extension.prepare_plane(layout, self.source)
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe",
        )
        initialized = runner._run_kaizen("K1")
        self.assertEqual(initialized["status"], "OK")
        self.assertEqual(
            runner._durable_id_snapshot(),
            {
                "agent_sessions": frozenset(),
                "user_instructions": frozenset(),
                "agent_runs": frozenset(),
                "agent_events": frozenset(),
                "approval_requests": frozenset(),
            },
        )

    def test_selection_uses_discovered_catalog_and_conservative_budget(self) -> None:
        request = test_extension.validate_suite_request(
            start_control(),
            capabilities(),
        )
        self.assertEqual(request.reserved_calls, 8)
        self.assertEqual(request.provider_retries, 0)
        for mutation, code in (
            ({"model": "hard-coded-alias"}, "DENIED_TEST_EXTENSION_MODEL"),
            ({"effort": "unsupported"}, "DENIED_TEST_EXTENSION_EFFORT"),
            ({"effort": None}, "DENIED_TEST_EXTENSION_EFFORT_REQUIRED"),
            ({"call_ceiling": 7}, "DENIED_TEST_EXTENSION_CALL_BUDGET"),
            ({"scenarios": ["ollama-text-stream"]}, "DENIED_TEST_EXTENSION_PROVIDER_SCENARIO"),
        ):
            body = start_control()
            body.update(mutation)
            body["request_sha256"] = test_extension._canonical_request_sha256(body)
            with self.assertRaises(test_extension.TestExtensionError) as caught:
                test_extension.validate_suite_request(body, capabilities())
            self.assertEqual(caught.exception.code, code)

    def test_feature_absence_fails_closed_and_ollama_never_falls_back_to_claude(self) -> None:
        caps = capabilities()
        caps["engines"][1]["features"] = {}
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            test_extension.validate_suite_request(
                start_control(
                    provider="ollama", model="discovered-ollama", effort=None, max_turns=2,
                    call_ceiling=2, scenarios=["ollama-text-stream"],
                ),
                caps,
            )
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_PROVIDER_UNAVAILABLE")

        for changes in (
            {"runtime": None},
            {"availability": {"state": "degraded"}},
            {"drivable": False},
        ):
            with self.subTest(changes=changes):
                claude = capabilities()
                claude["engines"][0].update(changes)
                with self.assertRaises(test_extension.TestExtensionError) as denied:
                    test_extension.validate_suite_request(start_control(), claude)
                self.assertEqual(denied.exception.code, "DENIED_TEST_EXTENSION_PROVIDER_UNAVAILABLE")

    def test_capability_evidence_is_sanitized(self) -> None:
        raw = capabilities()
        raw["engines"][0].update({
            "email": "private", "organization": "private", "runtime_path": "private",
            "runtime": {"status": "ready", "kind": "private", "version": "private"},
        })
        safe = test_extension.sanitize_capabilities(raw)
        self.assertNotIn("private", json.dumps(safe))
        self.assertTrue(safe["engines"][0]["features"]["test_extension"])
        self.assertEqual(safe["engines"][0]["runtime"], {"status": "ready"})

    def test_capability_sanitizer_matches_editor_fail_closed_normalization(self) -> None:
        malformed_runtime = capabilities()
        malformed_runtime["engines"][0].update({
            "drivable": False,
            "availability": {"state": "unavailable", "code": "DENIED_SDK_UNAVAILABLE"},
            "runtime": {"status": "unavailable"},
            "models": [],
        })
        safe = test_extension.sanitize_capabilities(malformed_runtime)
        self.assertNotIn("runtime", safe["engines"][0])
        self.assertIsNone(test_extension._capability_external_block_code("claude", safe))

        blank_label = capabilities()
        blank_label["engines"][0]["label"] = "   "
        self.assertEqual(
            [engine["id"] for engine in test_extension.sanitize_capabilities(blank_label)["engines"]],
            ["local_llm"],
        )

        padded_state = capabilities()
        padded_state["engines"][0]["availability"] = {
            "state": " available ", "code": "DENIED_PROVIDER_CAPACITY",
        }
        safe = test_extension.sanitize_capabilities(padded_state)
        self.assertEqual(safe["engines"][0]["availability"]["state"], "available")
        self.assertIsNone(test_extension._capability_external_block_code("claude", safe))

        blank_model = capabilities()
        blank_model["engines"][0]["models"] = [{"id": " ", "label": "Model"}]
        safe = test_extension.sanitize_capabilities(blank_model)
        self.assertEqual(safe["engines"][0]["models"], [])
        self.assertEqual(
            test_extension._capability_external_block_code("claude", safe),
            "DENIED_MODEL_UNAVAILABLE",
        )

        invalid_drivable = capabilities()
        invalid_drivable["engines"][0]["drivable"] = 1
        self.assertEqual(
            [engine["id"] for engine in test_extension.sanitize_capabilities(invalid_drivable)["engines"]],
            ["local_llm"],
        )

    def test_evidence_rejects_sensitive_field_names(self) -> None:
        layout = self.layout()
        writer = test_extension.EvidenceWriter(layout.evidence, layout.run_id, clock=lambda: "2026-01-01T00:00:00.000Z")
        writer.append("run.open", "START", provider_retries=0)
        for unsafe in ({"credential_path": "never"}, {"raw_output": "never"}):
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(ValueError, "unsafe"):
                writer.append("bad", "INFO", **unsafe)
        self.assertEqual(json.loads(layout.evidence.read_text(encoding="utf-8"))["seq"], 1)

    def test_outer_runner_owns_daemon_and_edh_as_siblings(self) -> None:
        layout = self.layout()
        spawned = []
        commands = []

        def spawn(cmd, **kwargs):
            child = FakeChild(); spawned.append((cmd, child, kwargs))
            if any(str(arg).startswith("--extensionDevelopmentPath=") for arg in cmd):
                write_edh_ready(layout)
            return child

        def command(cmd, **kwargs):
            commands.append(tuple(cmd))
            payload = capabilities() if "capabilities" in cmd else {
                "status": "OK", "running": True, "pid": 1234, "nonce": "a" * 32,
                "repo_root": str(layout.workspace), "engines": ["claude_cli", "codex", "local_llm"],
                "provider_target_fingerprint": "f" * 64,
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

        ticks = iter([0.0, 30.0, 30.0, 30.1])
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable), code_path=self.temp / "Code.exe",
            spawner=spawn, command_runner=command, monotonic=lambda: next(ticks, 0.2), sleep=lambda _seconds: None,
        )
        runner.initialize(); runner.start_daemon(); runner.start_edh()
        self.assertEqual([name for name, _ in runner.children], ["daemon", "edh"])
        self.assertEqual(len(spawned), 2)
        self.assertNotIn("--extensionDevelopmentPath", " ".join(spawned[0][0]))
        self.assertIn("--extensionDevelopmentPath", " ".join(spawned[1][0]))
        edh_command = spawned[1][0]
        self.assertEqual(edh_command[edh_command.index("--shared-data-dir") + 1], str(layout.shared_data))
        self.assertTrue(layout.shared_data.is_dir())
        self.assertTrue((layout.shared_data / "sharedStorage" / "state.vscdb").is_file())
        capability_commands = [cmd for cmd in commands if "capabilities" in cmd]
        self.assertEqual(len(capability_commands), 1)
        self.assertNotIn("--refresh", capability_commands[0])
        for _cmd, _child, kwargs in spawned:
            self.assertEqual(kwargs["env"]["KAIZEN_REPO_ROOT"], str(layout.workspace))
            self.assertEqual(kwargs["env"]["KAIZEN_TEST_EXTENSION_PROVIDER_RETRIES"], "0")
            self.assertEqual(kwargs["env"]["KAIZEN_HTTP_RETRIES"], "0")
            self.assertEqual(kwargs["env"]["KAIZEN_TEST_EXTENSION_EDH_READY_PATH"], str(layout.edh_ready))
            self.assertEqual(kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertTrue(kwargs["env"]["PYTHONPYCACHEPREFIX"].startswith(str(layout.temp)))
            self.assertEqual(kwargs["env"]["KAIZEN_CLAUDE_PROVIDER_RUNTIME_ROOT"], str(self.source))
        self.assertTrue(runner.cleanup())
        self.assertEqual([child.killed for _, child, _ in spawned], [1, 1])

    def test_daemon_start_rejects_cli_false_ready_until_bound_capabilities_exist(self) -> None:
        layout = self.layout("te-daemon-false-ready")
        status_calls = 0

        def command(cmd, **kwargs):
            nonlocal status_calls
            if "capabilities" in cmd:
                payload = capabilities()
            elif "status" in cmd:
                status_calls += 1
                payload = {"status": "OK", "running": False} if status_calls == 1 else {
                    "status": "OK", "running": True, "pid": 4321, "nonce": "b" * 32,
                    "repo_root": str(layout.workspace), "engines": ["claude_cli", "codex", "local_llm"],
                    "provider_target_fingerprint": "f" * 64,
                }
            else:
                payload = {"status": "OK"}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe", spawner=lambda _cmd, **_kwargs: FakeChild(pid=4321),
            command_runner=command, monotonic=lambda: 0.0, sleep=lambda _seconds: None,
        )
        runner.initialize()
        runner.start_daemon()
        self.assertEqual(status_calls, 2)
        self.assertEqual({item["id"] for item in runner.capabilities["engines"]}, {"claude", "local_llm"})
        events = [json.loads(line) for line in layout.evidence.read_text(encoding="utf-8").splitlines()]
        starting = [event for event in events if event["event"] == "daemon.starting"]
        ready = [event for event in events if event["event"] == "daemon.ready"]
        self.assertEqual(len(starting), 1)
        self.assertEqual(len(ready), 1)
        self.assertEqual(starting[0]["status"], "START")
        self.assertLess(starting[0]["seq"], ready[0]["seq"])
        self.assertEqual(ready[0]["provider_target_fingerprint"], "f" * 64)
        self.assertTrue(runner.cleanup())

    def test_daemon_start_rejects_foreign_pid_until_owned_root_is_reported(self) -> None:
        layout = self.layout("te-daemon-pid-mismatch")
        status_calls = 0
        child = FakeChild(pid=2468)

        def command(cmd, **kwargs):
            nonlocal status_calls
            if "capabilities" in cmd:
                payload = capabilities()
            elif "status" in cmd:
                status_calls += 1
                payload = {
                    "status": "OK", "running": True,
                    "pid": 1357 if status_calls == 1 else child.pid,
                    "nonce": "c" * 32, "repo_root": str(layout.workspace),
                    "engines": ["claude_cli", "codex", "local_llm"],
                    "provider_target_fingerprint": "f" * 64,
                }
            else:
                payload = {"status": "OK"}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe", spawner=lambda _cmd, **_kwargs: child,
            command_runner=command, monotonic=lambda: 0.0, sleep=lambda _seconds: None,
        )
        runner.initialize()
        runner.start_daemon()
        self.assertEqual(status_calls, 2)
        events = [json.loads(line) for line in layout.evidence.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len([event for event in events if event["event"] == "daemon.ready"]), 1)
        self.assertTrue(runner.cleanup())

    def test_daemon_start_accepts_owned_descendant_after_launcher_exits(self) -> None:
        layout = self.layout("te-daemon-owned-descendant")
        daemon_pid = 9753
        child = FakeChild(pid=2468, owned_pids={2468, daemon_pid})
        child.returncode = 0

        def command(cmd, **kwargs):
            if "capabilities" in cmd:
                payload = capabilities()
            elif "status" in cmd:
                payload = {
                    "status": "OK", "running": True, "pid": daemon_pid,
                    "nonce": "d" * 32, "repo_root": str(layout.workspace),
                    "engines": ["claude_cli", "codex", "local_llm"],
                    "provider_target_fingerprint": "f" * 64,
                }
            else:
                payload = {"status": "OK"}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe", spawner=lambda _cmd, **_kwargs: child,
            command_runner=command, monotonic=lambda: 0.0, sleep=lambda _seconds: None,
        )
        runner.initialize()
        runner.start_daemon()
        self.assertEqual(runner.capabilities["engines"][0]["id"], "claude")
        self.assertTrue(runner.cleanup())

    def test_daemon_timeout_records_only_bounded_failed_gate_names(self) -> None:
        layout = self.layout("te-daemon-readiness-diagnostic")
        child = FakeChild(pid=2468)
        clock = [0.0]

        def command(cmd, **kwargs):
            if "capabilities" in cmd:
                payload = capabilities()
            elif "status" in cmd:
                payload = {
                    "status": "OK", "running": True, "pid": 1357,
                    "nonce": "e" * 32, "repo_root": str(layout.workspace),
                    "engines": ["claude_cli", "codex", "local_llm"],
                    "provider_target_fingerprint": "f" * 64,
                }
            else:
                payload = {"status": "OK"}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe", spawner=lambda _cmd, **_kwargs: child,
            command_runner=command, monotonic=lambda: clock[0],
            sleep=lambda _seconds: clock.__setitem__(0, clock[0] + 60.0),
        )
        runner.initialize()
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            runner.start_daemon()
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_DAEMON_TIMEOUT")
        evidence = [json.loads(line) for line in layout.evidence.read_text(encoding="utf-8").splitlines()]
        diagnostic = [event for event in evidence if event["event"] == "daemon.readiness"]
        self.assertEqual(len(diagnostic), 1)
        self.assertEqual(diagnostic[0]["failed_gates"], ["owned_process"])
        self.assertEqual(diagnostic[0]["code"], "DENIED_TEST_EXTENSION_DAEMON_TIMEOUT")
        self.assertNotIn("pid", diagnostic[0])
        self.assertNotIn("repo_root", diagnostic[0])
        self.assertTrue(runner.cleanup())

    def test_edh_ready_requires_the_fresh_plane_shared_storage_database(self) -> None:
        for name, wrong_root in (("missing", False), ("wrong-root", True)):
            with self.subTest(name=name):
                layout = self.layout(f"te-edh-shared-{name}")
                test_extension.prepare_plane(layout, self.source)

                def spawn(_cmd, **_kwargs):
                    write_edh_ready(layout, shared_storage=False)
                    if wrong_root:
                        target = layout.user_data / "sharedStorage" / "state.vscdb"
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(b"SQLite format 3\x00")
                    return FakeChild()

                runner = test_extension.OuterRunner(
                    source_root=self.source, layout=layout, python=Path(sys.executable),
                    code_path=self.temp / "Code.exe", spawner=spawn,
                )
                with self.assertRaises(test_extension.TestExtensionError) as caught:
                    runner.start_edh()
                self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_EDH_SHARED_STORAGE")
                self.assertEqual([item[0] for item in runner.children], ["edh"])
                self.assertTrue(runner.cleanup())

    def test_edh_ready_rejects_stale_wrong_nonce_wrong_digest_and_malformed_artifacts(self) -> None:
        cases = (
            ("wrong-nonce", {"suite_nonce": "b" * 64}, "DENIED_TEST_EXTENSION_EDH_READY_FORGED"),
            ("wrong-digest", {"manifest_sha256": "c" * 64}, "DENIED_TEST_EXTENSION_EDH_READY_FORGED"),
            ("malformed", None, "DENIED_TEST_EXTENSION_EDH_READY_INVALID"),
        )
        for name, changes, code in cases:
            with self.subTest(name=name):
                layout = self.layout(f"te-edh-{name}")
                test_extension.prepare_plane(layout, self.source)

                def spawn(_cmd, **_kwargs):
                    if changes is None:
                        layout.edh_ready.write_text("{", encoding="utf-8")
                    else:
                        write_edh_ready(layout, **changes)
                    return FakeChild()

                runner = test_extension.OuterRunner(
                    source_root=self.source, layout=layout, python=Path(sys.executable),
                    code_path=self.temp / "Code.exe", spawner=spawn,
                )
                with self.assertRaises(test_extension.TestExtensionError) as caught:
                    runner.start_edh()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual([item[0] for item in runner.children], ["edh"])
                self.assertTrue(runner.cleanup())

        stale_layout = self.layout("te-edh-stale")
        test_extension.prepare_plane(stale_layout, self.source)
        write_edh_ready(stale_layout)
        stale_spawn = mock.Mock()
        stale = test_extension.OuterRunner(
            source_root=self.source, layout=stale_layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe", spawner=stale_spawn,
        )
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            stale.start_edh()
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_EDH_READY_STALE")
        stale_spawn.assert_not_called()

    def test_edh_ready_timeout_retains_child_for_runner_cleanup(self) -> None:
        layout = self.layout("te-edh-no-ready")
        test_extension.prepare_plane(layout, self.source)
        child = FakeChild()
        ticks = iter((0.0, 0.0, 61.0))
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe", spawner=lambda _cmd, **_kwargs: child,
            monotonic=lambda: next(ticks, 61.0), sleep=lambda _seconds: None,
        )
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            runner.start_edh()
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_EDH_READY_TIMEOUT")
        self.assertEqual(runner.children, [("edh", child)])
        self.assertTrue(runner.cleanup())
        self.assertEqual(child.killed, 1)

    def test_edh_exit_records_bounded_run_local_update_diagnostic(self) -> None:
        layout = self.layout("te-edh-update-lock")
        test_extension.prepare_plane(layout, self.source)
        main_log = layout.user_data / "logs" / "20260715T004539" / "main.log"
        main_log.parent.mkdir(parents=True)
        main_log.write_bytes(
            b"prefix\nCode is currently being updated. Please wait for the update to complete before launching.\n"
        )
        child = FakeChild()
        child.returncode = 1
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe", spawner=lambda _cmd, **_kwargs: child,
            monotonic=lambda: 0.0,
        )

        with self.assertRaises(test_extension.TestExtensionError) as caught:
            runner.start_edh()

        self.assertEqual(caught.exception.code, test_extension.VSCODE_UPDATE_IN_PROGRESS_CODE)
        evidence_text = layout.evidence.read_text(encoding="utf-8")
        evidence = [json.loads(line) for line in evidence_text.splitlines()]
        self.assertEqual([item["event"] for item in evidence], ["edh.starting", "edh.readiness"])
        self.assertEqual(evidence[-1]["code"], test_extension.VSCODE_UPDATE_IN_PROGRESS_CODE)
        self.assertNotIn("currently being updated", evidence_text)
        self.assertNotIn(str(main_log), evidence_text)
        self.assertEqual(runner.children, [("edh", child)])
        self.assertTrue(runner.cleanup())

    def test_edh_exit_diagnostic_falls_back_for_unknown_oversize_and_reparse_logs(self) -> None:
        cases = ("unknown", "oversize", "reparse")
        for name in cases:
            with self.subTest(name=name):
                layout = self.layout(f"te-edh-diagnostic-{name}")
                test_extension.prepare_plane(layout, self.source)
                main_log = layout.user_data / "logs" / "session" / "main.log"
                main_log.parent.mkdir(parents=True)
                if name == "oversize":
                    main_log.write_bytes(
                        test_extension._VSCODE_UPDATE_IN_PROGRESS_SIGNATURE
                        + b"x" * test_extension.EDH_MAIN_LOG_MAX_BYTES
                    )
                else:
                    main_log.write_bytes(b"unclassified child exit")
                child = FakeChild()
                child.returncode = 1
                runner = test_extension.OuterRunner(
                    source_root=self.source, layout=layout, python=Path(sys.executable),
                    code_path=self.temp / "Code.exe", spawner=lambda _cmd, **_kwargs: child,
                    monotonic=lambda: 0.0,
                )

                context = mock.patch.object(runner, "_is_reparse", return_value=True) \
                    if name == "reparse" else contextlib.nullcontext()
                with context, self.assertRaises(test_extension.TestExtensionError) as caught:
                    runner.start_edh()

                self.assertEqual(caught.exception.code, test_extension.EDH_EXIT_CODE)
                evidence_text = layout.evidence.read_text(encoding="utf-8")
                evidence = [json.loads(line) for line in evidence_text.splitlines()]
                self.assertEqual(evidence[-1]["code"], test_extension.EDH_EXIT_CODE)
                self.assertNotIn("unclassified child exit", evidence_text)
                self.assertNotIn("currently being updated", evidence_text)
                self.assertTrue(runner.cleanup())

    def test_edh_ready_allows_bounded_cold_extension_host_startup(self) -> None:
        layout = self.layout("te-edh-delayed-ready")
        test_extension.prepare_plane(layout, self.source)
        child = FakeChild()
        ticks = iter((0.0, 30.0, 59.0))

        def publish_ready(_seconds: float) -> None:
            write_edh_ready(layout)

        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe", spawner=lambda _cmd, **_kwargs: child,
            monotonic=lambda: next(ticks, 59.0), sleep=publish_ready,
        )
        runner.start_edh()
        self.assertEqual(runner.children, [("edh", child)])
        self.assertEqual(test_extension.EDH_READY_TIMEOUT_SECONDS, 60.0)
        self.assertGreaterEqual(test_extension.DAEMON_READY_TIMEOUT_SECONDS, 210.0)
        self.assertTrue(runner.cleanup())

    def test_provider_package_freezes_validated_target_while_session_plane_stays_unique(self) -> None:
        runtime_root = self.temp / "provider-runtime" / "claude-agent"
        target = runtime_root / "version-target"
        worker = target / "dist" / "worker.js"
        node = self.temp / "node.exe"
        worker.parent.mkdir(parents=True)
        worker.write_text("// immutable worker\n", encoding="utf-8")
        node.write_bytes(b"node")
        validated = {
            "target": target,
            "pointer": {"schema": 1, "active": "version-target", "previous": None},
            "integrity": {"node_executable": str(node), "worker_sha256": "a" * 64},
        }
        with mock.patch.dict(os.environ, {"KAIZEN_CLAUDE_PROVIDER_RUNTIME_ROOT": str(runtime_root)}), mock.patch(
            "kaizen_components.orchestration.claude_runtime.validate_runtime", return_value=validated
        ) as validate:
            command = supervisor._validated_claude_provider_command(self.source)
        validate.assert_called_once_with(
            runtime_root.resolve(), source_root=mock.ANY,
        )
        self.assertEqual(command, [str(node.resolve()), str(worker.resolve())])
        self.assertNotIn(str(self.layout().workspace), command)

    def test_supervisor_freezes_provider_target_and_fingerprint_once(self) -> None:
        frozen = supervisor._ClaudeProviderTarget(("node", "worker"), "f" * 64)
        instance = supervisor.Supervisor(self.source)
        self.addCleanup(instance.shutdown)
        with mock.patch(
            "kaizen_components.orchestration.supervisor._validated_claude_provider_target", return_value=frozen,
        ) as validate:
            self.assertIs(instance._frozen_claude_provider_target(), frozen)
            self.assertIs(instance._frozen_claude_provider_target(), frozen)
        validate.assert_called_once_with(self.source.resolve())
        self.assertEqual(instance.status_payload()["provider_target_fingerprint"], "f" * 64)

    def test_provider_target_fingerprint_rehashes_validated_pointer_and_integrity(self) -> None:
        runtime_root = self.temp / "provider-runtime" / "claude-agent"
        first = {
            "pointer": {"schema": 1, "active": "target-a", "previous": None},
            "integrity": {"worker_sha256": "a" * 64, "node_sha256": "b" * 64},
        }
        changed = {
            "pointer": {"schema": 1, "active": "target-b", "previous": "target-a"},
            "integrity": {"worker_sha256": "c" * 64, "node_sha256": "b" * 64},
        }
        with mock.patch(
            "kaizen_components.orchestration.test_extension.claude_runtime.validate_runtime",
            side_effect=[first, changed],
        ) as validate:
            before = test_extension.provider_target_fingerprint(runtime_root)
            after = test_extension.provider_target_fingerprint(runtime_root)
        self.assertNotEqual(before, after)
        self.assertEqual(validate.call_args_list, [
            mock.call(runtime_root.resolve(), source_root=mock.ANY),
        ] * 2)

    def test_claude_provider_fingerprint_is_unconditional_without_cleanup_scenario(self) -> None:
        def run_case(run_id: str, fingerprints: list[str]) -> tuple[int, list[dict]]:
            """Exercise both immutable-final fingerprint outcomes with a traversal-only runner."""
            runner, suite = self.authoritative_runner(
                "claude-traversal-zero-record", run_id=run_id,
            )
            control = start_control(
                effort=suite.effort, max_turns=suite.max_turns, call_ceiling=suite.call_ceiling,
                scenarios=list(suite.scenarios), suite_nonce=suite.suite_nonce,
            )
            runner.layout.control.write_text(json.dumps(control), encoding="utf-8")
            arm = action_request("traversal.arm_zero_records", 1, "traversal-arm", suite)
            verify = action_request("traversal.verify_zero_records", 2, "traversal-verify", suite)
            (runner.layout.action_requests / "00000001-traversal-arm.json").write_text(
                json.dumps(arm), encoding="utf-8",
            )
            (runner.layout.action_requests / "00000002-traversal-verify.json").write_text(
                json.dumps(verify), encoding="utf-8",
            )
            runner.layout.client_evidence.write_text(
                json.dumps({
                    "v": 1, "event": "scenario.complete", "scenario": "claude-traversal-zero-record",
                    "provider": "claude", "status": "PASS", "calls_reserved": suite.max_turns,
                    "denial_code": "DENIED_CONTEXT_INVALID", "zero_records": True,
                }) + "\n" + json.dumps({
                    "v": 1, "event": "suite.complete", "passed": 1, "failed": 0, "not_run": 0,
                }) + "\n",
                encoding="utf-8",
            )
            runner.capabilities = capabilities()
            runner._provider_target_fingerprint = "f" * 64
            runner._provider_runtime_root = self.source
            runner._run_kaizen = mock.Mock(side_effect=lambda *args, **_kwargs: (
                capabilities() if args[-1] == "capabilities"
                else {"provider_target_fingerprint": "f" * 64}
            ))
            with mock.patch(
                "kaizen_components.orchestration.test_extension.provider_target_fingerprint",
                side_effect=fingerprints,
            ):
                outcome = runner.wait()
            evidence = [json.loads(line) for line in runner.layout.evidence.read_text(encoding="utf-8").splitlines()]
            return outcome, evidence

        passed, passed_evidence = run_case("te-provider-unconditional-pass", ["f" * 64, "f" * 64])
        self.assertEqual(passed, 0)
        self.assertEqual(
            [item["status"] for item in passed_evidence if item["event"].startswith("provider.immutable_")],
            ["PASS", "PASS"],
        )
        self.assertFalse(any(item["event"] == "cleanup.leak_state" for item in passed_evidence))

        failed, failed_evidence = run_case("te-provider-unconditional-fail", ["f" * 64, "e" * 64])
        self.assertEqual(failed, 1)
        self.assertEqual(
            [item["status"] for item in failed_evidence if item["event"] == "provider.immutable_final"],
            ["FAIL"],
        )

    def test_stopped_claude_suite_still_runs_final_provider_immutability_gate(self) -> None:
        def run_case(run_id: str, final_fingerprint: str) -> tuple[int, list[dict]]:
            layout = self.layout(run_id)
            test_extension.prepare_plane(layout, self.source)
            runner = test_extension.OuterRunner(
                source_root=self.source, layout=layout, python=Path(sys.executable),
                code_path=self.temp / "Code.exe",
            )
            runner._approved_request = test_extension.SuiteRequest(
                "claude", "discovered-claude", "low", 2, 2,
                ("claude-traversal-zero-record",),
            )
            runner._provider_target_fingerprint = "f" * 64
            runner._provider_disk_fingerprint = "f" * 64
            runner._provider_runtime_root = self.source
            runner._run_kaizen = mock.Mock(return_value={"provider_target_fingerprint": "f" * 64})
            with mock.patch.object(runner, "initialize"), mock.patch.object(
                runner, "start_daemon",
            ), mock.patch.object(runner, "start_edh"), mock.patch.object(
                runner, "wait", return_value=130,
            ), mock.patch.object(
                runner, "verify_source_immutable", return_value=True,
            ), mock.patch(
                "kaizen_components.orchestration.test_extension.provider_target_fingerprint",
                return_value=final_fingerprint,
            ):
                outcome = runner.run()
            evidence = [json.loads(line) for line in layout.evidence.read_text(encoding="utf-8").splitlines()]
            return outcome, evidence

        stopped, evidence = run_case("te-provider-stop-pass", "f" * 64)
        self.assertEqual(stopped, 130)
        self.assertEqual([item["status"] for item in evidence if item["event"] == "provider.immutable_final"], ["PASS"])
        self.assertEqual(
            [item["status"] for item in evidence if item["event"] == "provider.immutable_post_cleanup"],
            ["PASS"],
        )

        changed, evidence = run_case("te-provider-stop-fail", "e" * 64)
        self.assertEqual(changed, 1)
        self.assertEqual([item["status"] for item in evidence if item["event"] == "provider.immutable_final"], ["FAIL"])
        self.assertEqual(
            [item["status"] for item in evidence if item["event"] == "provider.immutable_post_cleanup"],
            ["FAIL"],
        )

    def test_provider_mutation_during_child_kill_fails_post_cleanup_rehash(self) -> None:
        layout = self.layout("te-provider-cleanup-mutation")
        test_extension.prepare_plane(layout, self.source)
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe",
        )
        runner._approved_request = test_extension.SuiteRequest(
            "claude", "discovered-claude", "low", 2, 2,
            ("claude-traversal-zero-record",),
        )
        runner._provider_target_fingerprint = "f" * 64
        runner._provider_disk_fingerprint = "f" * 64
        runner._provider_runtime_root = self.source
        runner._run_kaizen = mock.Mock(return_value={"provider_target_fingerprint": "f" * 64})
        mutated = False

        class MutatingChild(FakeChild):
            """Simulate provider-runtime mutation during child teardown so post-cleanup fingerprint verification must fail after the final pre-cleanup pass."""
            def kill_tree(self, timeout: float = 5.0) -> None:
                nonlocal mutated
                mutated = True
                super().kill_tree(timeout)

        child = MutatingChild()
        runner.children = [("daemon", child)]
        with mock.patch.object(runner, "initialize"), mock.patch.object(
            runner, "start_daemon",
        ), mock.patch.object(runner, "start_edh"), mock.patch.object(
            runner, "wait", return_value=0,
        ), mock.patch.object(
            runner, "verify_source_immutable", return_value=True,
        ), mock.patch(
            "kaizen_components.orchestration.test_extension.provider_target_fingerprint",
            side_effect=lambda _root: ("e" if mutated else "f") * 64,
        ):
            self.assertEqual(runner.run(), 1)
        self.assertEqual(child.killed, 1)
        evidence = [json.loads(line) for line in layout.evidence.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            [(item["event"], item["status"]) for item in evidence if item["event"].startswith("provider.immutable_")],
            [("provider.immutable_final", "PASS"), ("provider.immutable_post_cleanup", "FAIL")],
        )

    def test_provider_runtime_missing_is_fail_closed_not_a_pass(self) -> None:
        with mock.patch(
            "kaizen_components.orchestration.claude_runtime.validate_runtime",
            side_effect=OSError("missing"),
        ):
            self.assertIsNone(supervisor._validated_claude_provider_command(self.source))

    def test_traversal_attestation_never_starts_a_session_and_requires_zero_durable_records(self) -> None:
        layout = self.layout(); test_extension.prepare_plane(layout, self.source)
        database = layout.workspace / "AI" / "db" / "kaizen.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        with turso.connect(str(database)) as connection:
            for table in ("agent_sessions", "user_instructions", "agent_runs", "agent_events", "approval_requests"):
                connection.execute(f"CREATE TABLE {table} (id TEXT)")
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable), code_path=self.temp / "Code.exe",
        )
        runner._approved_request = test_extension.SuiteRequest(
            "claude", "discovered-claude", "low", 2, 2, ("claude-traversal-zero-record",),
        )
        runner._suite_record_baseline = runner._durable_id_snapshot()
        with mock.patch("kaizen_components.orchestration.test_extension.loopback.send_request") as send:
            runner._arm_traversal_zero_records()
            proof = runner._attest_traversal_zero_records("DENIED_CONTEXT_INVALID")
        send.assert_not_called()
        self.assertEqual(proof, {"denial_code": "DENIED_CONTEXT_INVALID", "zero_records": True})
        self.assertFalse((layout.root / "traversal-proof.json").exists())

    def test_child_traversal_action_is_nonce_bound_and_outer_only_attests_records(self) -> None:
        runner, suite = self.authoritative_runner("claude-traversal-zero-record")
        runner._write_suite_binding()
        arm = action_request("traversal.arm_zero_records", 1, "traversal-arm", suite)
        verify = action_request("traversal.verify_zero_records", 2, "traversal-verify", suite)
        arm_target = runner.layout.action_requests / "00000001-traversal-arm.json"
        target = runner.layout.action_requests / "00000002-traversal-verify.json"
        arm_target.write_text(json.dumps(arm), encoding="utf-8")
        target.write_text(json.dumps(verify), encoding="utf-8")
        with mock.patch("kaizen_components.orchestration.test_extension.loopback.send_request") as send:
            runner._consume_action_requests()
        send.assert_not_called()
        arm_receipt = json.loads((runner.layout.action_receipts / arm_target.name).read_text(encoding="utf-8"))
        self.assertEqual(arm_receipt["proof"], {"zero_record_baseline_armed": True})
        receipt = json.loads((runner.layout.action_receipts / target.name).read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "OK")
        self.assertEqual(receipt["proof"], {
            "denial_code": "DENIED_CONTEXT_INVALID", "zero_records": True,
        })

        forged = action_request(
            "traversal.verify_zero_records", 3, "traversal-2", suite,
            denial_code="DENIED_WORKSPACE_WRITER_BUSY",
        )
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            test_extension._validate_action_request(
                forged, suite_nonce=suite.suite_nonce, request_sha256=suite.request_sha256,
                selected=suite.scenarios, expected_sequence=3,
            )
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_ACTION_BINDING")

    def test_traversal_arm_is_scenario_local_and_detects_any_intervening_row(self) -> None:
        prior, prior_suite = self.authoritative_runner("claude-traversal-zero-record")
        self.insert_session_run(
            prior, prior_suite, "claude-text-stream",
            session_id="session-prior", agent_run_id="run-prior",
        )
        self.assertNotEqual(prior._durable_id_snapshot(), prior._suite_record_baseline)
        with mock.patch("kaizen_components.orchestration.test_extension.loopback.send_request") as send:
            self.assertEqual(prior._arm_traversal_zero_records(), {"zero_record_baseline_armed": True})
            self.assertEqual(
                prior._attest_traversal_zero_records("DENIED_CONTEXT_INVALID"),
                {"denial_code": "DENIED_CONTEXT_INVALID", "zero_records": True},
            )
        send.assert_not_called()

        changed, changed_suite = self.authoritative_runner("claude-traversal-zero-record")
        changed._arm_traversal_zero_records()
        self.insert_session_run(
            changed, changed_suite, "claude-text-stream",
            session_id="session-injected", agent_run_id="run-injected",
        )
        with mock.patch("kaizen_components.orchestration.test_extension.loopback.send_request") as send:
            with self.assertRaises(test_extension.TestExtensionError) as caught:
                changed._attest_traversal_zero_records("DENIED_CONTEXT_INVALID")
        send.assert_not_called()
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_TRAVERSAL_PROOF")

    def test_traversal_verify_without_arm_replay_and_cross_suite_are_fail_closed(self) -> None:
        runner, suite = self.authoritative_runner("claude-traversal-zero-record")
        runner._write_suite_binding()
        verify = action_request("traversal.verify_zero_records", 1, "verify-without-arm", suite)
        (runner.layout.action_requests / "00000001-verify-without-arm.json").write_text(
            json.dumps(verify), encoding="utf-8",
        )
        with mock.patch("kaizen_components.orchestration.test_extension.loopback.send_request") as send:
            with self.assertRaises(test_extension.TestExtensionError) as caught:
                runner._consume_action_requests()
        send.assert_not_called()
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_ACTION_PHASE")

        replay, replay_suite = self.authoritative_runner("claude-traversal-zero-record")
        replay._write_suite_binding()
        arm = action_request("traversal.arm_zero_records", 1, "traversal-arm", replay_suite)
        target = replay.layout.action_requests / "00000001-traversal-arm.json"
        target.write_text(json.dumps(arm), encoding="utf-8")
        replay._consume_action_requests()
        target.write_text(json.dumps({**arm, "suite_nonce": "b" * 64}), encoding="utf-8")
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            replay._consume_action_requests()
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_ACTION_REPLAY")

        cross_suite = {**arm, "suite_nonce": "c" * 64}
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            test_extension._validate_action_request(
                cross_suite,
                suite_nonce=replay_suite.suite_nonce,
                request_sha256=replay_suite.request_sha256,
                selected=replay_suite.scenarios,
                expected_sequence=1,
            )
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_ACTION_FORGED")

    def test_runner_client_pass_requires_and_reconciles_authoritative_text_records(self) -> None:
        runner, request = self.authoritative_runner("claude-text-stream")
        proof = self.seed_text_authority(runner, request)
        runner.layout.client_evidence.write_text(
            json.dumps({
                "v": 1, "event": "scenario.complete", "status": "PASS", **proof,
                "raw_output": "must not mirror",
            }) + "\n" + json.dumps({
                "v": 1, "event": "suite.complete", "passed": 1, "failed": 0, "not_run": 0,
            }) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(runner._consume_client_evidence(), 0)
        self.assertEqual(runner._scenario_bound_runs, {"claude-text-stream": ("run-text",)})
        self.assertEqual(runner._provider_calls_by_run, {"run-text": 1})
        evidence = runner.layout.evidence.read_text(encoding="utf-8")
        self.assertIn('"event":"scenario.complete"', evidence)
        self.assertNotIn("must not mirror", evidence)

    def test_process_approval_pass_binds_distinct_tool_and_approval_correlations(self) -> None:
        runner, request = self.authoritative_runner("claude-process-approval")
        proof = self.seed_process_authority(runner, request)
        self.assertNotEqual(proof["correlation_id"], proof["tool_correlation_id"])
        runner.layout.client_evidence.write_text(
            json.dumps({
                "v": 1, "event": "scenario.complete", "status": "PASS", **proof,
            }) + "\n" + json.dumps({
                "v": 1, "event": "suite.complete", "passed": 1, "failed": 0, "not_run": 0,
            }) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(runner._consume_client_evidence(), 0)
        self.assertEqual(
            runner._bound_correlations,
            {
                ("run-process", "run-process-approval"): "claude-process-approval",
                ("run-process", "run-process-tool"): "claude-process-approval",
            },
        )
        self.assertEqual(runner._bound_approval_ids, {"approval-process": "run-process"})
        evidence = runner.layout.evidence.read_text(encoding="utf-8")
        self.assertIn('"tool_result_complete":true', evidence)
        self.assertNotIn("tool_output_complete", evidence)

    def test_image_and_context_passes_rehash_exact_authoritative_artifacts(self) -> None:
        cache_kind = {
            "claude-image-codeword": "images",
            "claude-governed-context": "context",
        }
        for scenario in cache_kind:
            with self.subTest(scenario=scenario, case="valid"):
                runner, request = self.authoritative_runner(scenario)
                proof = self.seed_structured_codeword_authority(
                    runner, request, session_id=f"session-{scenario}", agent_run_id=f"run-{scenario}",
                )
                test_extension._validate_pass_proof(scenario, proof, request, runner.python)
                runner._validate_authoritative_pass(scenario, proof, request)
                self.assertEqual(runner._provider_calls_by_run, {f"run-{scenario}": 1})

            with self.subTest(scenario=scenario, case="corrupt-cache"):
                corrupted, corrupted_request = self.authoritative_runner(scenario)
                corrupted_proof = self.seed_structured_codeword_authority(
                    corrupted, corrupted_request,
                    session_id=f"session-corrupt-{scenario}", agent_run_id=f"run-corrupt-{scenario}",
                )
                cache = (
                    corrupted.layout.workspace / "AI" / "work" / "orchestration" / "ui-cache"
                    / cache_kind[scenario] / "sha256"
                )
                objects = [path for path in cache.iterdir() if len(path.name) == 64]
                self.assertEqual(len(objects), 1)
                objects[0].write_bytes(b"corrupted-authoritative-artifact")
                with self.assertRaises(test_extension.TestExtensionError) as caught:
                    corrupted._validate_authoritative_pass(scenario, corrupted_proof, corrupted_request)
                self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

    def test_all_diff_and_writer_branches_bind_real_c4_snapshots_and_final_bytes(self) -> None:
        scenarios = (
            "claude-diff-accept", "claude-diff-reject", "claude-diff-stale",
            "claude-diff-corrupt", "claude-diff-timeout", "claude-writer-conflict",
        )
        for index, scenario in enumerate(scenarios, start=1):
            with self.subTest(scenario=scenario, case="valid"):
                runner, request = self.authoritative_runner(scenario)
                proof = self.seed_diff_authority(
                    runner, request, session_id=f"session-diff-{index}", agent_run_id=f"run-diff-{index}",
                    approval_id=f"approval-diff-{index}",
                )
                test_extension._validate_pass_proof(scenario, proof, request, runner.python)
                runner._validate_authoritative_pass(scenario, proof, request)
                self.assertEqual(runner._provider_calls_by_run, {f"run-diff-{index}": 1})
                self.assertEqual(runner._bound_approval_ids, {f"approval-diff-{index}": f"run-diff-{index}"})

            with self.subTest(scenario=scenario, case="wrong-final-bytes"):
                mutated, mutated_request = self.authoritative_runner(scenario)
                mutated_proof = self.seed_diff_authority(
                    mutated, mutated_request, session_id=f"session-mutated-{index}",
                    agent_run_id=f"run-mutated-{index}", approval_id=f"approval-mutated-{index}",
                )
                target = mutated.layout.workspace / "te-diff-target.txt"
                target.write_bytes(b"branch-specific unauthorized final bytes\n")
                with self.assertRaises(test_extension.TestExtensionError) as caught:
                    mutated._validate_authoritative_pass(scenario, mutated_proof, mutated_request)
                self.assertIn(caught.exception.code, {
                    "DENIED_TEST_EXTENSION_DURABLE_PROOF", "DENIED_TEST_EXTENSION_CONTROL_PROOF",
                })

    def test_restart_cleanup_and_traversal_branches_have_real_authority_and_adversaries(self) -> None:
        restart, restart_request = self.authoritative_runner(
            "claude-interrupt-restart", call_ceiling=4,
        )
        restart_proof = self.seed_restart_authority(restart, restart_request)
        test_extension._validate_pass_proof(
            "claude-interrupt-restart", restart_proof, restart_request, restart.python,
        )
        restart._validate_authoritative_pass("claude-interrupt-restart", restart_proof, restart_request)
        self.assertEqual(restart._provider_calls_by_run, {"run-restart-old": 0, "run-restart-new": 1})

        bad_restart, bad_restart_request = self.authoritative_runner(
            "claude-interrupt-restart", call_ceiling=4,
        )
        bad_restart_proof = self.seed_restart_authority(bad_restart, bad_restart_request)
        database = bad_restart.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            row = connection.execute(
                "SELECT id,body FROM agent_events WHERE agent_run_id = ? AND event_kind = 'profile'",
                ("run-restart-new",),
            ).fetchone()
            body = json.loads(row[1]); body.pop("resume_fidelity")
            encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "UPDATE agent_events SET body = ?, content_hash = ? WHERE id = ?",
                (encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), row[0]),
            )
        with self.assertRaises(test_extension.TestExtensionError) as restart_denied:
            bad_restart._validate_authoritative_pass(
                "claude-interrupt-restart", bad_restart_proof, bad_restart_request,
            )
        self.assertEqual(restart_denied.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

        cleanup, cleanup_request = self.authoritative_runner("cleanup-leak-state")
        cleanup_proof = self.seed_cleanup_authority(
            cleanup, cleanup_request, session_id="session-cleanup", agent_run_id="run-cleanup",
        )
        test_extension._validate_pass_proof("cleanup-leak-state", cleanup_proof, cleanup_request, cleanup.python)
        cleanup._validate_authoritative_pass("cleanup-leak-state", cleanup_proof, cleanup_request)

        bad_cleanup, bad_cleanup_request = self.authoritative_runner("cleanup-leak-state")
        bad_cleanup_proof = self.seed_cleanup_authority(
            bad_cleanup, bad_cleanup_request,
            session_id="session-cleanup-bad", agent_run_id="run-cleanup-bad",
        )
        database = bad_cleanup.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            row = connection.execute(
                "SELECT id,body FROM agent_events WHERE agent_run_id = ? AND event_kind = 'tool_call' "
                "AND marker = 'close_ok'",
                ("run-cleanup-bad",),
            ).fetchone()
            body = json.loads(row[1]); body["result"]["sha256"] = "f" * 64
            encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "UPDATE agent_events SET body = ?, content_hash = ? WHERE id = ?",
                (encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), row[0]),
            )
        with self.assertRaises(test_extension.TestExtensionError) as cleanup_denied:
            bad_cleanup._validate_authoritative_pass(
                "cleanup-leak-state", bad_cleanup_proof, bad_cleanup_request,
            )
        self.assertEqual(cleanup_denied.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

        traversal, traversal_request = self.authoritative_runner("claude-traversal-zero-record")
        traversal._arm_traversal_zero_records()
        traversal._attest_traversal_zero_records("DENIED_CONTEXT_INVALID")
        traversal_proof = {
            "calls_reserved": traversal_request.max_turns,
            "denial_code": "DENIED_CONTEXT_INVALID", "zero_records": True,
        }
        test_extension._validate_pass_proof(
            "claude-traversal-zero-record", traversal_proof, traversal_request, traversal.python,
        )
        traversal._validate_authoritative_pass(
            "claude-traversal-zero-record", traversal_proof, traversal_request,
        )
        traversal._validate_suite_authority(traversal_request)

        bad_traversal, bad_traversal_request = self.authoritative_runner("claude-traversal-zero-record")
        bad_traversal._arm_traversal_zero_records()
        bad_traversal._attest_traversal_zero_records("DENIED_CONTEXT_INVALID")
        bad_traversal._action_proof["traversal"]["after_ids"] = {
            **bad_traversal._action_proof["traversal"]["after_ids"],
            "agent_sessions": frozenset({"forged-session"}),
        }
        with self.assertRaises(test_extension.TestExtensionError) as traversal_denied:
            bad_traversal._validate_authoritative_pass(
                "claude-traversal-zero-record", traversal_proof, bad_traversal_request,
            )
        self.assertEqual(traversal_denied.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

    def test_ollama_tool_policy_reconciles_canonical_process_denial_and_rejects_legacy_tool(self) -> None:
        runner, request = self.authoritative_runner("ollama-tool-policy", provider="ollama")
        proof = self.seed_plan_process_denial_authority(runner, request)
        runner.layout.client_evidence.write_text(
            json.dumps({
                "v": 1, "event": "scenario.complete", "status": "PASS", **proof,
            }) + "\n" + json.dumps({
                "v": 1, "event": "suite.complete", "passed": 1, "failed": 0, "not_run": 0,
            }) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(runner._consume_client_evidence(), 0)
        self.assertEqual(runner._scenario_bound_runs, {"ollama-tool-policy": ("run-plan",)})
        self.assertEqual(runner._provider_calls_by_run, {"run-plan": 1})

        legacy, legacy_request = self.authoritative_runner("ollama-tool-policy", provider="ollama")
        legacy_proof = self.seed_plan_process_denial_authority(legacy, legacy_request)
        database = legacy.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            for marker in ("open", "close_fail"):
                row = connection.execute(
                    "SELECT id,body FROM agent_events WHERE agent_run_id = ? AND event_kind = 'tool_call' AND marker = ?",
                    ("run-plan", marker),
                ).fetchone()
                body = json.loads(row[1])
                body["name"] = "write_file"
                encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
                connection.execute(
                    "UPDATE agent_events SET name = ?, body = ?, content_hash = ? WHERE id = ?",
                    ("write_file", encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), row[0]),
                )
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            legacy._validate_authoritative_pass("ollama-tool-policy", legacy_proof, legacy_request)
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

    def test_claude_plan_controls_reconcile_plan_ceiling_and_git_push_invariant(self) -> None:
        runner, request = self.authoritative_runner("claude-plan-controls")
        proof = self.seed_plan_process_denial_authority(runner, request)
        runner.layout.client_evidence.write_text(
            json.dumps({
                "v": 1, "event": "scenario.complete", "status": "PASS", **proof,
            }) + "\n" + json.dumps({
                "v": 1, "event": "suite.complete", "passed": 1, "failed": 0, "not_run": 0,
            }) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(runner._consume_client_evidence(), 0)
        self.assertEqual(runner._scenario_bound_runs, {"claude-plan-controls": ("run-plan",)})
        self.assertEqual(len(runner._bound_correlations), 2)

        forged, forged_request = self.authoritative_runner("claude-plan-controls")
        forged_proof = self.seed_plan_process_denial_authority(forged, forged_request)
        database = forged.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            row = connection.execute(
                "SELECT id,body FROM agent_events WHERE agent_run_id = ? AND correlation_id = ? AND marker = 'open'",
                ("run-plan", "run-plan-invariant-tool"),
            ).fetchone()
            body = json.loads(row[1])
            body["argv"] = ["status"]
            encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "UPDATE agent_events SET body = ?, content_hash = ? WHERE id = ?",
                (encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), row[0]),
            )
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            forged._validate_authoritative_pass("claude-plan-controls", forged_proof, forged_request)
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

    def test_authoritative_pass_rejects_reused_and_prebaseline_runs(self) -> None:
        runner, request = self.authoritative_runner("claude-text-stream")
        proof = self.seed_text_authority(runner, request)
        runner._validate_authoritative_pass("claude-text-stream", proof, request)
        with self.assertRaises(test_extension.TestExtensionError) as reused:
            runner._validate_authoritative_pass("claude-text-stream", proof, request)
        self.assertEqual(reused.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

        prebaseline, prebaseline_request = self.authoritative_runner("claude-text-stream")
        prebaseline_proof = self.seed_text_authority(prebaseline, prebaseline_request)
        prebaseline._suite_record_baseline = prebaseline._durable_id_snapshot()
        with self.assertRaises(test_extension.TestExtensionError) as preexisting:
            prebaseline._validate_authoritative_pass(
                "claude-text-stream", prebaseline_proof, prebaseline_request,
            )
        self.assertEqual(preexisting.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

    def test_authoritative_pass_rejects_missing_or_wrong_locators_and_provider(self) -> None:
        runner, request = self.authoritative_runner("claude-text-stream")
        proof = self.seed_text_authority(runner, request)
        for mutation in (
            {"session_id": None, "agent_run_id": None},
            {"agent_run_id": "run-that-does-not-exist"},
            {"session_id": "session-that-does-not-exist"},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(test_extension.TestExtensionError) as caught:
                runner._validate_authoritative_pass("claude-text-stream", {**proof, **mutation}, request)
            self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

        runner.layout.client_evidence.write_text(json.dumps({
            "v": 1, "event": "scenario.complete", "status": "PASS", **proof, "provider": "ollama",
        }) + "\n", encoding="utf-8")
        with self.assertRaises(test_extension.TestExtensionError) as provider:
            runner._consume_client_evidence()
        self.assertEqual(provider.exception.code, "DENIED_TEST_EXTENSION_SCENARIO_RECONCILIATION")

        process, process_request = self.authoritative_runner("claude-process-approval")
        process_proof = self.seed_process_authority(process, process_request)
        with self.assertRaises(test_extension.TestExtensionError) as correlation:
            process._validate_authoritative_pass(
                "claude-process-approval",
                {**process_proof, "correlation_id": process_proof["tool_correlation_id"]},
                process_request,
            )
        self.assertEqual(correlation.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

    def test_authoritative_pass_rejects_duplicate_finalization_and_reversed_turn(self) -> None:
        duplicate, duplicate_request = self.authoritative_runner("claude-text-stream")
        duplicate_proof = self.seed_text_authority(duplicate, duplicate_request)
        database = duplicate.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            self.insert_event(connection, "run-text", 7, "finalization", "close_ok", {})
        with self.assertRaises(test_extension.TestExtensionError) as finalization:
            duplicate._validate_authoritative_pass(
                "claude-text-stream", duplicate_proof, duplicate_request,
            )
        self.assertEqual(finalization.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

        reversed_runner, reversed_request = self.authoritative_runner("claude-text-stream")
        reversed_proof = self.seed_text_authority(reversed_runner, reversed_request)
        database = reversed_runner.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            connection.execute(
                "UPDATE agent_events SET sequence_no = 30 WHERE agent_run_id = ? AND event_kind = 'turn' AND marker = 'open'",
                ("run-text",),
            )
            connection.execute(
                "UPDATE agent_events SET sequence_no = 3 WHERE agent_run_id = ? AND event_kind = 'turn' AND marker = 'close_ok'",
                ("run-text",),
            )
            connection.execute(
                "UPDATE agent_events SET sequence_no = 4 WHERE agent_run_id = ? AND event_kind = 'turn' AND marker = 'open'",
                ("run-text",),
            )
        with self.assertRaises(test_extension.TestExtensionError) as turn:
            reversed_runner._validate_authoritative_pass(
                "claude-text-stream", reversed_proof, reversed_request,
            )
        self.assertEqual(turn.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

    def test_process_approval_rejects_wrong_c4_state_and_decider(self) -> None:
        runner, request = self.authoritative_runner("claude-process-approval")
        proof = self.seed_process_authority(runner, request)
        database = runner.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            connection.execute(
                "UPDATE approval_requests SET decided_by = 'auto' WHERE id = 'approval-process'",
            )
        with self.assertRaises(test_extension.TestExtensionError) as decider:
            runner._validate_authoritative_pass("claude-process-approval", proof, request)
        self.assertEqual(decider.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

        with turso.connect(str(database)) as connection:
            connection.execute(
                "UPDATE approval_requests SET decided_by = 'human', state = 'denied' WHERE id = 'approval-process'",
            )
        with self.assertRaises(test_extension.TestExtensionError) as state:
            runner._validate_authoritative_pass("claude-process-approval", proof, request)
        self.assertEqual(state.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

    def test_authoritative_pass_rejects_malformed_and_excess_provider_counts(self) -> None:
        runner, request = self.authoritative_runner("claude-text-stream")
        proof = self.seed_text_authority(runner, request)
        database = runner.layout.workspace / "AI" / "db" / "kaizen.db"
        for value in (True, request.max_turns + 1):
            encoded = json.dumps({"status": "OK", "num_turns": value}, separators=(",", ":"))
            with turso.connect(str(database)) as connection:
                connection.execute(
                    "UPDATE agent_events SET body = ?, content_hash = ? "
                    "WHERE agent_run_id = ? AND event_kind = 'turn' AND marker = 'close_ok'",
                    (encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), "run-text"),
                )
            with self.subTest(value=value), self.assertRaises(test_extension.TestExtensionError) as caught:
                runner._validate_authoritative_pass("claude-text-stream", proof, request)
            self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

    def test_suite_authority_rejects_provider_ceiling_and_foreign_rows(self) -> None:
        runner, request = self.authoritative_runner("claude-text-stream")
        proof = self.seed_text_authority(runner, request)
        runner._validate_authoritative_pass("claude-text-stream", proof, request)
        runner._provider_calls_by_run["run-text"] = request.call_ceiling + 1
        with self.assertRaises(test_extension.TestExtensionError) as ceiling:
            runner._validate_suite_authority(request)
        self.assertEqual(ceiling.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

        runner._provider_calls_by_run["run-text"] = 1
        database = runner.layout.workspace / "AI" / "db" / "kaizen.db"
        with turso.connect(str(database)) as connection:
            self.insert_row(connection, "agent_sessions", {
                "id": "foreign-session",
                "created_at": "2026-01-01T00:01:00.000Z",
                "controller": "kaizen",
                "mode": "orchestrate",
                "auth_mode": "subscription",
                "state": "closed",
                "summary": "foreign suite row",
                "content_hash": "foreign-content-hash",
            })
        with self.assertRaises(test_extension.TestExtensionError) as foreign:
            runner._validate_suite_authority(request)
        self.assertEqual(foreign.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")

    def test_suite_complete_cannot_false_pass_missing_duplicate_unselected_or_failed_results(self) -> None:
        request = test_extension.SuiteRequest(
            "claude", "discovered-claude", "low", 2, 4,
            ("claude-text-stream", "claude-governed-context"),
        )

        def consume(lines: list[dict]) -> tuple[test_extension.OuterRunner, int | None]:
            layout = self.layout(f"te-reconcile-{os.urandom(2).hex()}")
            test_extension.prepare_plane(layout, self.source)
            runner = test_extension.OuterRunner(
                source_root=self.source, layout=layout, python=Path(sys.executable), code_path=self.temp / "Code.exe",
            )
            runner._approved_request = request
            layout.client_evidence.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
            return runner, runner._consume_client_evidence()

        with self.assertRaises(test_extension.TestExtensionError) as missing:
            consume([{"v": 1, "event": "suite.complete", "passed": 0, "failed": 0, "not_run": 0}])
        self.assertEqual(missing.exception.code, "DENIED_TEST_EXTENSION_SCENARIO_RECONCILIATION")

        base = {
            "v": 1, "event": "scenario.complete", "scenario": "claude-text-stream",
            "provider": "claude", "status": "FAIL",
        }
        with self.assertRaises(test_extension.TestExtensionError) as duplicate:
            consume([base, base])
        self.assertEqual(duplicate.exception.code, "DENIED_TEST_EXTENSION_SCENARIO_RECONCILIATION")
        with self.assertRaises(test_extension.TestExtensionError) as unselected:
            consume([{**base, "scenario": "claude-diff-accept"}])
        self.assertEqual(unselected.exception.code, "DENIED_TEST_EXTENSION_SCENARIO_RECONCILIATION")

        failed_lines = [
            {**base, "status": "FAIL"},
            {
                "v": 1, "event": "scenario.not_run", "scenario": "claude-governed-context",
                "provider": "claude", "status": "NOT_RUN", "code": "DENIED_AUTH_UNAVAILABLE",
            },
            {"v": 1, "event": "suite.complete", "passed": 0, "failed": 1, "not_run": 1},
        ]
        _runner, outcome = consume(failed_lines)
        self.assertEqual(outcome, 1)

    def test_minimal_fabricated_pass_and_inexact_plan_process_proof_are_denied(self) -> None:
        def consume(scenario: str, proof: dict) -> None:
            layout = self.layout(f"te-proof-{os.urandom(2).hex()}")
            test_extension.prepare_plane(layout, self.source)
            runner = test_extension.OuterRunner(
                source_root=self.source, layout=layout, python=Path(sys.executable), code_path=self.temp / "Code.exe",
            )
            runner._approved_request = test_extension.SuiteRequest(
                "claude", "discovered-claude", "low", 2, 2, (scenario,),
            )
            layout.client_evidence.write_text(json.dumps({
                "v": 1, "event": "scenario.complete", "scenario": scenario, "provider": "claude",
                "status": "PASS", **proof,
            }) + "\n", encoding="utf-8")
            runner._consume_client_evidence()

        with self.assertRaises(test_extension.TestExtensionError) as minimal:
            consume("claude-text-stream", {})
        self.assertEqual(minimal.exception.code, "DENIED_TEST_EXTENSION_PASS_PROOF")

        expected = {
            "calls_reserved": 2, "profile_attested": True, "attested_model": "discovered-claude",
            "attested_effort": "low", "tool_card_count": 2, "tool_status": "blocked",
            "tool_request_matches": True, "tool_zero_execution": True, "tool_zero_stdout": True,
            "process_stdout_bytes": 0, "denial_code": "MODE_CEILING:exec",
            "invariant_denial_code": "INV_GIT_PUSH",
            "process_request_sha256": test_extension._process_request_sha256(
                Path(sys.executable), ("-c", "print('MUST_NOT_RUN')"), ".", 5000,
            ),
        }
        with self.assertRaises(test_extension.TestExtensionError) as no_durable_records:
            consume("claude-plan-controls", expected)
        self.assertEqual(no_durable_records.exception.code, "DENIED_TEST_EXTENSION_DURABLE_PROOF")
        for mutation in (
            {"tool_card_count": 1}, {"tool_status": "ok"}, {"tool_request_matches": False},
            {"tool_zero_execution": False}, {"tool_zero_stdout": False}, {"process_stdout_bytes": 1},
            {"process_exit_code": 0}, {"process_stdout_sha256": "a" * 64},
            {"denial_code": "DENIED_TOOL_POLICY"},
            {"invariant_denial_code": "DENIED_TOOL_POLICY"},
        ):
            with self.assertRaises(test_extension.TestExtensionError) as caught:
                consume("claude-plan-controls", {**expected, **mutation})
            self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_PASS_PROOF")

    def test_pass_proof_contracts_cover_stream_attachments_diffs_process_traversal_and_cleanup(self) -> None:
        python = Path(sys.executable)
        common = {
            "calls_reserved": 2, "profile_attested": True, "attested_model": "discovered-claude",
            "attested_effort": "low",
        }
        process_request = test_extension._process_request_sha256(
            python, ("-c", "print('TE_PROCESS_OK')"), ".", 5000,
        )
        cases = {
            "claude-text-stream": ({
                **common, "codeword_seen": True, "delta_count": 2, "ordered": True,
                "durable_replacements": 1, "stream_suppressed": False,
            }, "durable_replacements"),
            "claude-image-codeword": ({**common, "codeword_seen": True, "image_refs": 1}, "image_refs"),
            "claude-governed-context": ({
                **common, "codeword_seen": True, "context_refs": 2, "selection_exact": True,
            }, "selection_exact"),
            "claude-diff-accept": ({
                **common, "approval_ui": True, "decision": "approve", "approval_status": "approved",
                "diff_card_count": 1, "diff_path_matches": True, "diff_before_matches": True,
                "diff_proposed_hashed": True, "diff_final_matches": True,
            }, "diff_final_matches"),
            "claude-process-approval": ({
                **common, "approval_ui": True, "decision": "approve", "approval_status": "approved",
                "tool_card_count": 1, "tool_status": "ok", "tool_request_matches": True,
                "tool_result_matches": True, "tool_decision_approved": True, "tool_output_complete": True,
                "process_exit_code": 0,
                "process_stdout_bytes": len(b"TE_PROCESS_OK\n"), "process_request_sha256": process_request,
                "process_stdout_sha256": hashlib.sha256(b"TE_PROCESS_OK\n").hexdigest(),
            }, "tool_result_matches"),
            "claude-traversal-zero-record": ({
                "calls_reserved": 2, "denial_code": "DENIED_CONTEXT_INVALID", "zero_records": True,
            }, "zero_records"),
            "claude-diff-timeout": ({
                **common, "approval_ui": True, "approval_status": "timed_out", "diff_card_count": 1,
                "diff_path_matches": True, "diff_before_matches": True, "diff_proposed_hashed": True,
                "diff_final_matches": True,
            }, "approval_status"),
            "cleanup-leak-state": ({
                **common, "conversation_idle": True, "pending_approvals": 0, "running_cards": 0,
            }, "running_cards"),
        }
        for scenario, (proof, required) in cases.items():
            request = test_extension.SuiteRequest(
                "claude", "discovered-claude", "low", 2, 2, (scenario,),
            )
            test_extension._validate_pass_proof(scenario, proof, request, python)
            incomplete = dict(proof); incomplete.pop(required)
            with self.assertRaises(test_extension.TestExtensionError, msg=scenario) as caught:
                test_extension._validate_pass_proof(scenario, incomplete, request, python)
            self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_PASS_PROOF")

    def test_not_run_is_a_nonzero_external_block(self) -> None:
        layout = self.layout("te-not-run")
        test_extension.prepare_plane(layout, self.source)
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable), code_path=self.temp / "Code.exe",
        )
        runner._approved_request = test_extension.SuiteRequest(
            "claude", "discovered-claude", "low", 2, 2, ("claude-text-stream",),
        )
        layout.client_evidence.write_text(
            json.dumps({
                "v": 1, "event": "scenario.not_run", "scenario": "claude-text-stream",
                "provider": "claude", "status": "NOT_RUN", "code": "DENIED_AUTH_UNAVAILABLE",
            }) + "\n" + json.dumps({"v": 1, "event": "suite.complete", "passed": 0, "failed": 0, "not_run": 1}) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(runner._consume_client_evidence(), 3)

    def test_not_run_rejects_missing_internal_or_noncanonical_reason_codes(self) -> None:
        for index, code in enumerate((None, "INTERNAL_PROVIDER_FAILURE", "denied_auth_unavailable")):
            with self.subTest(code=code):
                layout = self.layout(f"te-not-run-code-{index}")
                test_extension.prepare_plane(layout, self.source)
                runner = test_extension.OuterRunner(
                    source_root=self.source, layout=layout, python=Path(sys.executable),
                    code_path=self.temp / "Code.exe",
                )
                runner._approved_request = test_extension.SuiteRequest(
                    "claude", "discovered-claude", "low", 2, 2, ("claude-text-stream",),
                )
                event = {
                    "v": 1, "event": "scenario.not_run", "scenario": "claude-text-stream",
                    "provider": "claude", "status": "NOT_RUN",
                }
                if code is not None:
                    event["code"] = code
                layout.client_evidence.write_text(json.dumps(event) + "\n", encoding="utf-8")
                with self.assertRaises(test_extension.TestExtensionError) as caught:
                    runner._consume_client_evidence()
                self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_CODE")

        layout = self.layout("te-not-run-boolean-version")
        test_extension.prepare_plane(layout, self.source)
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe",
        )
        runner._approved_request = test_extension.SuiteRequest(
            "claude", "discovered-claude", "low", 2, 2, ("claude-text-stream",),
        )
        layout.client_evidence.write_text(json.dumps({
            "v": True, "event": "scenario.not_run", "scenario": "claude-text-stream",
            "provider": "claude", "status": "NOT_RUN", "code": "DENIED_AUTH_UNAVAILABLE",
        }) + "\n", encoding="utf-8")
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            runner._consume_client_evidence()
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_CLIENT_EVIDENCE")

    def test_suite_nonce_digest_and_exact_start_schema_fail_closed(self) -> None:
        body = start_control()
        validated = test_extension.validate_suite_request(body, capabilities())
        self.assertEqual(validated.suite_nonce, "a" * 64)
        self.assertEqual(validated.request_sha256, body["request_sha256"])
        for mutation, code in (
            ({"request_sha256": "b" * 64}, "DENIED_TEST_EXTENSION_REQUEST_DIGEST"),
            ({"suite_nonce": "short"}, "DENIED_TEST_EXTENSION_SUITE_NONCE"),
            ({"provider_retries": 1}, "DENIED_TEST_EXTENSION_PROVIDER_RETRIES"),
            ({"provider_retries": False}, "DENIED_TEST_EXTENSION_PROVIDER_RETRIES"),
            ({"unexpected": True}, "DENIED_TEST_EXTENSION_CONTROL"),
        ):
            forged = {**body, **mutation}
            if "provider_retries" in mutation:
                forged["request_sha256"] = test_extension._canonical_request_sha256(forged)
            with self.assertRaises(test_extension.TestExtensionError) as caught:
                test_extension.validate_suite_request(forged, capabilities())
            self.assertEqual(caught.exception.code, code)
        for mutation, code in (
            ({"v": True}, "DENIED_TEST_EXTENSION_CONTROL"),
            ({"provider": 7}, "DENIED_TEST_EXTENSION_PROVIDER"),
            ({"model": 7}, "DENIED_TEST_EXTENSION_MODEL"),
            ({"effort": 7}, "DENIED_TEST_EXTENSION_EFFORT"),
            ({"scenarios": [1]}, "DENIED_TEST_EXTENSION_SCENARIOS"),
        ):
            forged = {**body, **mutation}
            forged["request_sha256"] = test_extension._canonical_request_sha256(forged)
            with self.assertRaises(test_extension.TestExtensionError) as caught:
                test_extension.validate_suite_request(forged, capabilities())
            self.assertEqual(caught.exception.code, code)

    def test_preapproval_stop_is_exact_plane_bound_pending_bound_and_replay_safe(self) -> None:
        layout = self.layout("te-preapproval-stop-validation")
        test_extension.prepare_plane(layout, self.source)
        nonce = json.loads(layout.manifest.read_text(encoding="utf-8"))["suite_nonce"]
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe",
        )
        first = preapproval_stop(suite_nonce=nonce)
        self.assertEqual(runner._accept_preapproval_stop(first), first)
        with self.assertRaises(test_extension.TestExtensionError) as replay:
            runner._accept_preapproval_stop(first)
        self.assertEqual(replay.exception.code, "DENIED_TEST_EXTENSION_STOP_REPLAY")

        pending = preapproval_stop(
            suite_nonce=nonce, stop_id="c" * 64, request_sha256="d" * 64,
        )
        self.assertEqual(test_extension.validate_preapproval_stop(
            pending, suite_nonce=nonce,
        ), pending)
        for mutation, code in (
            ({"v": True}, "DENIED_TEST_EXTENSION_STOP_SCHEMA"),
            ({"suite_nonce": "e" * 64}, "DENIED_TEST_EXTENSION_STOP_FORGED"),
            ({"unexpected": True}, "DENIED_TEST_EXTENSION_STOP_SCHEMA"),
            ({"stop_id": "bad"}, "DENIED_TEST_EXTENSION_STOP_ID"),
            ({"request_sha256": "bad"}, "DENIED_TEST_EXTENSION_REQUEST_DIGEST"),
        ):
            forged = {**pending, **mutation}
            with self.assertRaises(test_extension.TestExtensionError) as caught:
                test_extension.validate_preapproval_stop(forged, suite_nonce=nonce)
            self.assertEqual(caught.exception.code, code)

    def test_preapproval_stop_exits_through_owned_cleanup_and_run_closed_proof(self) -> None:
        layout = self.layout("te-preapproval-stop-cleanup")
        test_extension.prepare_plane(layout, self.source)
        nonce = json.loads(layout.manifest.read_text(encoding="utf-8"))["suite_nonce"]
        layout.control.write_text(json.dumps(preapproval_stop(suite_nonce=nonce)), encoding="utf-8")
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe",
        )
        daemon, edh = FakeChild(), FakeChild()
        runner.children = [("daemon", daemon), ("edh", edh)]
        with mock.patch.object(runner, "initialize"), mock.patch.object(
            runner, "start_daemon",
        ), mock.patch.object(runner, "start_edh"), mock.patch.object(
            runner, "verify_source_immutable", return_value=True,
        ):
            self.assertEqual(runner.run(), 130)
        self.assertEqual((daemon.killed, edh.killed), (1, 1))
        evidence = [json.loads(line) for line in layout.evidence.read_text(encoding="utf-8").splitlines()]
        events = [item["event"] for item in evidence]
        self.assertLess(events.index("suite.stop_requested"), events.index("cleanup.edh"))
        self.assertLess(events.index("cleanup.edh"), events.index("cleanup.daemon"))
        self.assertLess(events.index("cleanup.daemon"), events.index("run.closed"))
        for name in ("cleanup.edh", "cleanup.daemon"):
            cleanup = next(item for item in evidence if item["event"] == name)
            self.assertEqual(cleanup["status"], "PASS")
            self.assertIs(cleanup["termination_proven"], True)
            self.assertNotIn("preserved_for_audit", cleanup)
        stop = next(item for item in evidence if item["event"] == "suite.stop_requested")
        self.assertEqual((stop["pre_approval"], stop["pending_suite_bound"]), (True, False))
        self.assertEqual(evidence[-1]["exit_code"], 130)

    def test_launcher_stop_interrupts_daemon_start_and_uses_owned_cleanup(self) -> None:
        layout = self.layout("te-launcher-stop-starting")
        spawn = mock.Mock(side_effect=AssertionError("daemon must not spawn after a queued Stop"))

        def initialize() -> None:
            test_extension.prepare_plane(layout, self.source)
            nonce = json.loads(layout.manifest.read_text(encoding="utf-8"))["suite_nonce"]
            (layout.root / "launcher-stop.json").write_text(
                json.dumps(preapproval_stop(suite_nonce=nonce)), encoding="utf-8",
            )
            runner.evidence.append("run.open", "START", wall_seconds=1800)

        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe", spawner=spawn,
            command_runner=mock.Mock(side_effect=AssertionError("daemon probe must not run after Stop")),
        )
        with mock.patch.object(runner, "initialize", side_effect=initialize), mock.patch.object(
            runner, "verify_source_immutable", return_value=True,
        ):
            self.assertEqual(runner.run(), 130)
        spawn.assert_not_called()
        evidence = [json.loads(line) for line in layout.evidence.read_text(encoding="utf-8").splitlines()]
        events = [item["event"] for item in evidence]
        self.assertIn("suite.stop_requested", events)
        self.assertNotIn("daemon.ready", events)
        self.assertNotIn("edh.started", events)
        self.assertNotIn("run.failed", events)
        self.assertLess(events.index("suite.stop_requested"), events.index("cleanup.edh"))
        self.assertLess(events.index("cleanup.edh"), events.index("cleanup.daemon"))
        self.assertLess(events.index("cleanup.daemon"), events.index("run.closed"))
        for name in ("cleanup.edh", "cleanup.daemon"):
            cleanup = next(item for item in evidence if item["event"] == name)
            self.assertEqual(cleanup["status"], "PASS")
            self.assertIs(cleanup["termination_proven"], True)
            self.assertIs(cleanup["not_started"], True)
        self.assertEqual(evidence[-1]["status"], "STOP")
        stop = next(item for item in evidence if item["event"] == "suite.stop_requested")
        self.assertIs(stop["launcher"], True)

    def test_launcher_stop_remains_authoritative_after_approval_without_overwriting_control(self) -> None:
        layout = self.layout("te-launcher-stop-approved")
        test_extension.prepare_plane(layout, self.source)
        nonce = json.loads(layout.manifest.read_text(encoding="utf-8"))["suite_nonce"]
        control = start_control(suite_nonce=nonce)
        layout.control.write_text(json.dumps(control), encoding="utf-8")
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe",
        )
        runner._approved_request = test_extension.validate_suite_request(control, capabilities())
        (layout.root / "launcher-stop.json").write_text(
            json.dumps(preapproval_stop(suite_nonce=nonce)), encoding="utf-8",
        )

        self.assertIs(runner._consume_launcher_stop(), True)
        self.assertIs(runner._consume_launcher_stop(), True, "the accepted exact-run Stop remains latched")
        self.assertEqual(json.loads(layout.control.read_text(encoding="utf-8")), control)
        evidence = [json.loads(line) for line in layout.evidence.read_text(encoding="utf-8").splitlines()]
        stops = [item for item in evidence if item["event"] == "suite.stop_requested"]
        self.assertEqual(len(stops), 1)
        self.assertEqual((stops[0]["launcher"], stops[0]["pre_approval"]), (True, False))

    def test_launcher_stop_is_checked_before_a_dead_child(self) -> None:
        layout = self.layout("te-launcher-stop-before-dead-child")
        test_extension.prepare_plane(layout, self.source)
        nonce = json.loads(layout.manifest.read_text(encoding="utf-8"))["suite_nonce"]
        (layout.root / "launcher-stop.json").write_text(
            json.dumps(preapproval_stop(suite_nonce=nonce)), encoding="utf-8",
        )
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe", monotonic=mock.Mock(side_effect=[0.0, 0.0]),
        )
        dead = FakeChild()
        dead.returncode = 1
        runner.children = [("edh", dead)]
        runner._run_deadline = 10.0
        self.assertEqual(runner.wait(), 130)

    def test_visible_runner_accepts_one_exact_launcher_run_id(self) -> None:
        parsed = test_extension.build_parser().parse_args([
            "--source-root", str(self.source), "--plane-base", str(self.base),
            "--run-id", "te-exact-launcher", "--code-path", str(self.temp / "Code.exe"),
        ])
        self.assertEqual(parsed.run_id, "te-exact-launcher")
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            test_extension.create_layout(self.source, self.base, run_id="../escape")
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_RUN_ID")

    def test_external_block_is_exact_plane_bound_capability_justified_and_replay_safe(self) -> None:
        layout = self.layout("te-external-block-validation")
        test_extension.prepare_plane(layout, self.source)
        nonce = json.loads(layout.manifest.read_text(encoding="utf-8"))["suite_nonce"]
        caps = capabilities()
        caps["engines"][0].update({
            "drivable": False,
            "availability": {"state": "auth_required", "code": "DENIED_AUTH_MODE_MISMATCH"},
            "runtime": {"status": "unavailable"},
            "models": [],
        })
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe",
        )
        runner.capabilities = test_extension.sanitize_capabilities(caps)
        first = external_block(
            suite_nonce=nonce, code="DENIED_AUTH_MODE_MISMATCH",
            scenarios=["claude-text-stream"],
        )
        self.assertEqual(runner._accept_external_block(first), first)
        with self.assertRaises(test_extension.TestExtensionError) as replay:
            runner._accept_external_block(first)
        self.assertEqual(replay.exception.code, "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_REPLAY")

        cases = (
            ({**first, "block_id": "c" * 64, "v": True}, "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_SCHEMA"),
            ({**first, "block_id": "c" * 64, "suite_nonce": "d" * 64}, "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_FORGED"),
            ({**first, "block_id": "c" * 64, "code": "DENIED_SDK_UNAVAILABLE"}, "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_UNJUSTIFIED"),
            ({**first, "block_id": "c" * 64, "scenarios": ["ollama-text-stream"]}, "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_SCENARIOS"),
            ({**first, "block_id": "c" * 64, "extra": True}, "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_SCHEMA"),
            ({**first, "block_id": "c" * 64, "code": "denied_auth_mode_mismatch"}, "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_CODE"),
        )
        for value, code in cases:
            with self.subTest(code=code), self.assertRaises(test_extension.TestExtensionError) as caught:
                test_extension.validate_external_block(
                    value, suite_nonce=nonce, capabilities=runner.capabilities,
                )
            self.assertEqual(caught.exception.code, code)

    def test_external_block_reason_is_derived_only_from_explicit_external_capability_state(self) -> None:
        def claude(**changes: Any) -> dict:
            caps = capabilities()
            caps["engines"][0].update(changes)
            return test_extension.sanitize_capabilities(caps)

        cases = (
            (
                claude(
                    drivable=False, availability={"state": "auth_required", "code": "DENIED_ENGINE_UNAVAILABLE"},
                    runtime={"status": "unavailable"}, models=[],
                ),
                "DENIED_AUTH_UNAVAILABLE",
            ),
            (
                claude(
                    drivable=False, availability={"state": "unavailable"},
                    runtime={"kind": "claude-agent-sdk", "status": "unavailable"}, models=[],
                ),
                "DENIED_SDK_UNAVAILABLE",
            ),
            (
                claude(
                    drivable=True, availability={"state": "available"},
                    runtime={"kind": "claude-agent-sdk", "status": "ready"}, models=[],
                ),
                "DENIED_MODEL_UNAVAILABLE",
            ),
            (
                claude(
                    drivable=True,
                    availability={"state": "unavailable", "code": "DENIED_PROVIDER_CAPACITY"},
                    runtime={"kind": "claude-agent-sdk", "status": "ready"}, models=[],
                ),
                "DENIED_PROVIDER_CAPACITY",
            ),
        )
        for caps, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(test_extension._capability_external_block_code("claude", caps), expected)

        self.assertIsNone(test_extension._capability_external_block_code("claude", {"engines": []}))
        internal = claude(
            drivable=False, availability={"state": "unavailable", "code": "DENIED_ENGINE_UNAVAILABLE"},
            runtime={"kind": "claude-agent-sdk", "status": "ready"}, models=[],
        )
        self.assertIsNone(test_extension._capability_external_block_code("claude", internal))
        inconsistent = claude(
            drivable=False, availability={"state": "unavailable", "code": "DENIED_PROVIDER_CAPACITY"},
            runtime={"kind": "claude-agent-sdk", "status": "ready"}, models=[],
        )
        self.assertIsNone(test_extension._capability_external_block_code("claude", inconsistent))

    def test_external_block_reloads_current_daemon_catalog_before_acceptance(self) -> None:
        def auth_required_capabilities() -> dict:
            current = capabilities()
            current["engines"][0].update({
                "drivable": False,
                "availability": {"state": "auth_required", "code": "DENIED_AUTH_UNAVAILABLE"},
                "runtime": {"kind": "claude-agent-sdk", "status": "unavailable"},
                "models": [],
            })
            return current

        layout = self.layout("te-external-block-current-auth")
        test_extension.prepare_plane(layout, self.source)
        nonce = json.loads(layout.manifest.read_text(encoding="utf-8"))["suite_nonce"]
        became_blocked = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe",
        )
        became_blocked.capabilities = test_extension.sanitize_capabilities(capabilities())
        became_blocked._run_kaizen = mock.Mock(return_value=auth_required_capabilities())
        became_blocked._reload_control_capabilities()
        became_blocked._accept_external_block(external_block(suite_nonce=nonce))

        layout = self.layout("te-external-block-current-ready")
        test_extension.prepare_plane(layout, self.source)
        nonce = json.loads(layout.manifest.read_text(encoding="utf-8"))["suite_nonce"]
        recovered = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe",
        )
        recovered.capabilities = test_extension.sanitize_capabilities(auth_required_capabilities())
        recovered._run_kaizen = mock.Mock(return_value=capabilities())
        recovered._reload_control_capabilities()
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            recovered._accept_external_block(external_block(suite_nonce=nonce))
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK_UNJUSTIFIED")

    def test_external_block_exits_not_run_with_zero_provider_calls_and_owned_cleanup(self) -> None:
        layout = self.layout("te-external-block-cleanup")
        test_extension.prepare_plane(layout, self.source)
        nonce = json.loads(layout.manifest.read_text(encoding="utf-8"))["suite_nonce"]
        caps = capabilities()
        caps["engines"][0].update({
            "drivable": False,
            "availability": {"state": "auth_required", "code": "DENIED_AUTH_UNAVAILABLE"},
            "runtime": {"status": "unavailable"},
            "models": [],
        })
        layout.control.write_text(json.dumps(external_block(suite_nonce=nonce)), encoding="utf-8")
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe",
        )
        runner.capabilities = test_extension.sanitize_capabilities(caps)
        runner._run_kaizen = mock.Mock(return_value=caps)
        daemon, edh = FakeChild(), FakeChild()
        runner.children = [("daemon", daemon), ("edh", edh)]
        with mock.patch.object(runner, "initialize"), mock.patch.object(
            runner, "start_daemon",
        ), mock.patch.object(runner, "start_edh"), mock.patch.object(
            runner, "verify_source_immutable", return_value=True,
        ), mock.patch.object(runner, "_capture_claude_provider_baseline") as provider_start:
            self.assertEqual(runner.run(), 3)
        provider_start.assert_not_called()
        runner._run_kaizen.assert_called_once_with("daemon", "session", "capabilities", timeout=5.0)
        self.assertIsNone(runner._approved_request)
        self.assertEqual(runner._provider_calls_by_run, {})
        self.assertEqual((daemon.killed, edh.killed), (1, 1))

        evidence = [json.loads(line) for line in layout.evidence.read_text(encoding="utf-8").splitlines()]
        events = [item["event"] for item in evidence]
        self.assertNotIn("suite.approved", events)
        not_run = [item for item in evidence if item["event"] == "scenario.not_run"]
        self.assertEqual([item["scenario"] for item in not_run], [
            "claude-text-stream", "claude-governed-context",
        ])
        self.assertTrue(all(
            item["status"] == "NOT_RUN" and item["provider"] == "claude"
            and item["code"] == "DENIED_AUTH_UNAVAILABLE" for item in not_run
        ))
        external = next(item for item in evidence if item["event"] == "suite.external_block")
        self.assertEqual(external["provider_calls"], 0)
        complete = next(item for item in evidence if item["event"] == "suite.complete")
        self.assertEqual(
            (complete["status"], complete["passed"], complete["failed"], complete["not_run"]),
            ("NOT_RUN", 0, 0, 2),
        )
        self.assertLess(events.index("suite.complete"), events.index("cleanup.edh"))
        self.assertEqual((evidence[-1]["event"], evidence[-1]["status"], evidence[-1]["exit_code"]), (
            "run.closed", "NOT_RUN", 3,
        ))

    def test_unproven_child_cleanup_is_retained_for_audit(self) -> None:
        class StubbornChild(FakeChild):
            def kill_tree(self, timeout: float = 5.0) -> None:
                self.killed += 1

        layout = self.layout("te-unproven-child-cleanup")
        child = StubbornChild()
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable),
            code_path=self.temp / "Code.exe",
        )
        runner.children = [("edh", child)]

        self.assertFalse(runner.cleanup())
        self.assertEqual(runner.children, [("edh", child)])
        self.assertEqual(child.killed, 2)
        evidence = [json.loads(line) for line in layout.evidence.read_text(encoding="utf-8").splitlines()]
        cleanup = [item for item in evidence if item["event"] == "cleanup.edh"]
        self.assertEqual([item["attempt"] for item in cleanup], [1, 2])
        self.assertTrue(all(item["status"] == "FAIL" and item["termination_proven"] is False for item in cleanup))
        self.assertIs(cleanup[0]["preserved_for_audit"], False)
        self.assertIs(cleanup[1]["preserved_for_audit"], True)

    def test_postapproval_control_stop_still_requires_bound_action_protocol(self) -> None:
        runner, _suite = self.approved_runner(("claude-text-stream",), "te-postapproval-control-stop")
        nonce = json.loads(runner.layout.manifest.read_text(encoding="utf-8"))["suite_nonce"]
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            runner._accept_preapproval_stop(preapproval_stop(suite_nonce=nonce))
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_CONTROL")

    def test_action_files_are_per_id_atomic_bound_and_idempotent(self) -> None:
        runner, suite = self.approved_runner(("claude-diff-stale",), "te-action-idempotent")
        request = action_request("stale.mutate_base", 1, "stale-1", suite)
        target = runner.layout.action_requests / "00000001-stale-1.json"
        target.write_text(json.dumps(request), encoding="utf-8")
        with mock.patch.object(runner, "_execute_action", return_value={"mutated_sha256": "5" * 64}) as execute:
            runner._consume_action_requests()
            runner._consume_action_requests()
        execute.assert_called_once()
        receipt_path = runner.layout.action_receipts / target.name
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual({key: receipt[key] for key in request}, request)
        self.assertEqual(receipt["status"], "OK")
        self.assertEqual(receipt["proof"], {"mutated_sha256": "5" * 64})
        self.assertEqual(
            json.loads(runner.layout.action_suite.read_text(encoding="utf-8")),
            {
                "protocol": test_extension.ACTION_PROTOCOL,
                "suite_nonce": suite.suite_nonce,
                "request_sha256": suite.request_sha256,
            },
        )
        target.write_text(json.dumps({**request, "before_sha256": "6" * 64}), encoding="utf-8")
        with self.assertRaises(test_extension.TestExtensionError) as replay:
            runner._consume_action_requests()
        self.assertEqual(replay.exception.code, "DENIED_TEST_EXTENSION_ACTION_REPLAY")

    def test_accepted_action_failure_always_has_terminal_bound_receipt(self) -> None:
        runner, suite = self.approved_runner(("claude-diff-corrupt",), "te-action-failed-receipt")
        request = action_request("corrupt.snapshot", 1, "corrupt-1", suite)
        target = runner.layout.action_requests / "00000001-corrupt-1.json"
        target.write_text(json.dumps(request), encoding="utf-8")
        with mock.patch.object(
            runner,
            "_execute_action",
            side_effect=test_extension.TestExtensionError("DENIED_TEST_EXTENSION_ACTION_SNAPSHOT", "bad"),
        ):
            runner._consume_action_requests()
        receipt = json.loads((runner.layout.action_receipts / target.name).read_text(encoding="utf-8"))
        self.assertEqual({key: receipt[key] for key in request}, request)
        self.assertEqual(receipt["status"], "FAILED")
        self.assertEqual(receipt["code"], "DENIED_TEST_EXTENSION_ACTION_SNAPSHOT")
        self.assertEqual(receipt["proof"], {})

    def test_action_sequence_schema_forgery_and_cross_scenario_are_denied(self) -> None:
        runner, suite = self.approved_runner(("claude-diff-stale",), "te-action-adversarial")
        base = action_request("stale.mutate_base", 1, "stale-1", suite)
        cases = (
            ({**base, "suite_nonce": "f" * 64}, "DENIED_TEST_EXTENSION_ACTION_FORGED"),
            ({**base, "request_sha256": "f" * 64}, "DENIED_TEST_EXTENSION_ACTION_FORGED"),
            ({**base, "workspace_path": "../escape.txt"}, "DENIED_TEST_EXTENSION_ACTION_PATH"),
            ({**base, "revision": 0}, "DENIED_TEST_EXTENSION_ACTION_REVISION"),
            ({**base, "extra": True}, "DENIED_TEST_EXTENSION_ACTION_SCHEMA"),
            (
                {**action_request("corrupt.snapshot", 1, "stale-1", suite)},
                "DENIED_TEST_EXTENSION_ACTION_SCENARIO",
            ),
        )
        for value, code in cases:
            with self.assertRaises(test_extension.TestExtensionError, msg=code) as caught:
                test_extension._validate_action_request(
                    value,
                    suite_nonce=suite.suite_nonce,
                    request_sha256=suite.request_sha256,
                    selected=suite.scenarios,
                    expected_sequence=1,
                )
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(test_extension.TestExtensionError) as reordered:
            test_extension._validate_action_request(
                {**base, "sequence": 2},
                suite_nonce=suite.suite_nonce,
                request_sha256=suite.request_sha256,
                selected=suite.scenarios,
                expected_sequence=1,
            )
        self.assertEqual(reordered.exception.code, "DENIED_TEST_EXTENSION_ACTION_SEQUENCE")

    def test_action_file_reparse_oversize_and_missing_sequence_are_denied(self) -> None:
        runner, suite = self.approved_runner(("claude-diff-stale",), "te-action-file-bounds")
        request = action_request("stale.mutate_base", 2, "stale-2", suite)
        target = runner.layout.action_requests / "00000002-stale-2.json"
        target.write_text(json.dumps(request), encoding="utf-8")
        with self.assertRaises(test_extension.TestExtensionError) as missing:
            runner._consume_action_requests()
        self.assertEqual(missing.exception.code, "DENIED_TEST_EXTENSION_ACTION_SEQUENCE")
        target.unlink()
        target = runner.layout.action_requests / "00000001-stale-1.json"
        target.write_text(" " * (test_extension.ACTION_REQUEST_MAX_BYTES + 1), encoding="utf-8")
        with self.assertRaises(test_extension.TestExtensionError) as oversized:
            runner._consume_action_requests()
        self.assertEqual(oversized.exception.code, "DENIED_TEST_EXTENSION_ACTION_OVERSIZE")
        target.write_text(json.dumps(action_request("stale.mutate_base", 1, "stale-1", suite)), encoding="utf-8")
        with mock.patch.object(runner, "_is_reparse", return_value=True):
            with self.assertRaises(test_extension.TestExtensionError) as reparse:
                runner._consume_action_requests()
        self.assertEqual(reparse.exception.code, "DENIED_TEST_EXTENSION_ACTION_REPARSE")

    def test_phase_repetition_and_cross_revision_are_denied(self) -> None:
        runner, suite = self.approved_runner(("claude-writer-conflict",), "te-action-phase-order")
        first = action_request("writer.holder_open", 1, "holder-open", suite)
        (runner.layout.action_requests / "00000001-holder-open.json").write_text(
            json.dumps(first), encoding="utf-8",
        )
        with mock.patch.object(runner, "_execute_action", return_value={"isolated_parked": True}):
            runner._consume_action_requests()
        repeated = action_request("writer.holder_open", 2, "holder-repeat", suite)
        (runner.layout.action_requests / "00000002-holder-repeat.json").write_text(
            json.dumps(repeated), encoding="utf-8",
        )
        with self.assertRaises(test_extension.TestExtensionError) as phase:
            runner._consume_action_requests()
        self.assertEqual(phase.exception.code, "DENIED_TEST_EXTENSION_ACTION_PHASE")
        (runner.layout.action_requests / "00000002-holder-repeat.json").unlink()
        cross_revision = action_request("writer.loser_arm", 2, "loser-arm", suite, revision=2)
        (runner.layout.action_requests / "00000002-loser-arm.json").write_text(
            json.dumps(cross_revision), encoding="utf-8",
        )
        with self.assertRaises(test_extension.TestExtensionError) as revision:
            runner._consume_action_requests()
        self.assertEqual(revision.exception.code, "DENIED_TEST_EXTENSION_ACTION_CROSS_REVISION")

    def test_restart_actions_match_exact_ts_shape_without_correlation(self) -> None:
        _runner, suite = self.approved_runner(("claude-interrupt-restart",), "te-restart-shape")
        daemon = action_request("restart.daemon", 1, "restart-daemon", suite)
        cleanup = action_request("restart.cleanup", 2, "restart-cleanup", suite)
        self.assertNotIn("correlation_id", daemon)
        self.assertNotIn("correlation_id", cleanup)
        self.assertEqual(
            test_extension._validate_action_request(
                daemon,
                suite_nonce=suite.suite_nonce,
                request_sha256=suite.request_sha256,
                selected=suite.scenarios,
                expected_sequence=1,
            ),
            daemon,
        )
        self.assertEqual(
            test_extension._validate_action_request(
                cleanup,
                suite_nonce=suite.suite_nonce,
                request_sha256=suite.request_sha256,
                selected=suite.scenarios,
                expected_sequence=2,
            ),
            cleanup,
        )
        for value, sequence in (({**daemon, "correlation_id": "invented"}, 1),
                                ({**cleanup, "correlation_id": "invented"}, 2)):
            with self.assertRaises(test_extension.TestExtensionError) as extra:
                test_extension._validate_action_request(
                    value,
                    suite_nonce=suite.suite_nonce,
                    request_sha256=suite.request_sha256,
                    selected=suite.scenarios,
                    expected_sequence=sequence,
                )
            self.assertEqual(extra.exception.code, "DENIED_TEST_EXTENSION_ACTION_SCHEMA")

    def test_bound_stop_gets_receipt_before_outer_stops(self) -> None:
        runner, suite = self.approved_runner(("claude-text-stream",), "te-action-stop")
        request = action_request("suite.stop", 1, "stop-1", suite)
        target = runner.layout.action_requests / "00000001-stop-1.json"
        target.write_text(json.dumps(request), encoding="utf-8")
        runner._consume_action_requests()
        receipt = json.loads((runner.layout.action_receipts / target.name).read_text(encoding="utf-8"))
        self.assertTrue(runner._stop_requested)
        self.assertEqual(receipt["status"], "OK")
        self.assertEqual(receipt["proof"], {"stop_accepted": True})

    def test_wall_clock_above_thirty_minutes_is_denied(self) -> None:
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            test_extension.OuterRunner(
                source_root=self.source, layout=self.layout(), python=Path(sys.executable),
                code_path=self.temp / "Code.exe", wall_seconds=1801,
            )
        self.assertEqual(caught.exception.code, "DENIED_TEST_EXTENSION_WALL_CLOCK")

    def test_unexpected_python_error_is_sanitized_and_still_closes(self) -> None:
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=self.layout("te-internal-error"),
            python=Path(sys.executable), code_path=self.temp / "Code.exe",
        )
        with mock.patch.object(runner, "initialize"), mock.patch.object(
            runner, "start_daemon", side_effect=RuntimeError("sensitive fixture detail"),
        ), mock.patch.object(runner, "start_edh"), mock.patch.object(
            runner, "_ensure_claude_provider_final", return_value=True,
        ), mock.patch.object(runner, "cleanup", return_value=True), mock.patch.object(
            runner, "_verify_claude_provider_post_cleanup", return_value=True,
        ), mock.patch.object(runner, "verify_source_immutable", return_value=True):
            self.assertEqual(runner.run(), 1)
        self.assertIsNone(runner._run_deadline)
        evidence = [
            json.loads(line) for line in runner.layout.evidence.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            (evidence[0]["event"], evidence[0]["code"]),
            ("run.failed", test_extension.INTERNAL_ERROR_CODE),
        )
        self.assertNotIn("sensitive fixture detail", runner.layout.evidence.read_text(encoding="utf-8"))
        self.assertEqual(
            (evidence[-1]["event"], evidence[-1]["status"], evidence[-1]["exit_code"]),
            ("run.closed", "FAIL", 1),
        )

    def test_outer_wall_is_anchored_before_preflight_and_maps_to_exit_124(self) -> None:
        clock = [0.0]
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=self.layout("te-absolute-wall"), python=Path(sys.executable),
            code_path=self.temp / "Code.exe", wall_seconds=10, monotonic=lambda: clock[0], sleep=lambda seconds: None,
        )
        runner.initialize = lambda: clock.__setitem__(0, 4.0)  # type: ignore[method-assign]
        runner.start_daemon = lambda: clock.__setitem__(0, 9.0)  # type: ignore[method-assign]
        runner.start_edh = lambda: clock.__setitem__(0, 10.0)  # type: ignore[method-assign]
        runner.wait = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
            test_extension.TestExtensionError(test_extension.WALL_CLOCK_CODE, "expired")
        )
        runner._ensure_claude_provider_final = lambda: True  # type: ignore[method-assign]
        runner.cleanup = lambda: True  # type: ignore[method-assign]
        runner._verify_claude_provider_post_cleanup = lambda **_kwargs: True  # type: ignore[method-assign]
        runner.verify_source_immutable = lambda: True  # type: ignore[method-assign]
        self.assertEqual(runner.run(), 124)
        self.assertIsNone(runner._run_deadline)
        events = [json.loads(line) for line in runner.layout.evidence.read_text(encoding="utf-8").splitlines()]
        wall = [event for event in events if event["event"] == "suite.wall_clock"]
        self.assertEqual(len(wall), 1)
        self.assertEqual(wall[0]["code"], test_extension.WALL_CLOCK_CODE)

    def test_restart_daemon_ready_wait_cannot_reset_outer_wall(self) -> None:
        clock = [9.0]
        child = FakeChild()

        def command(cmd, **kwargs):
            if "capabilities" in cmd:
                clock[0] = 10.0
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(capabilities()), stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"status": "OK"}), stderr="")

        runner = test_extension.OuterRunner(
            source_root=self.source, layout=self.layout("te-restart-wall"), python=Path(sys.executable),
            code_path=self.temp / "Code.exe", spawner=lambda _cmd, **_kwargs: child,
            command_runner=command, monotonic=lambda: clock[0], sleep=lambda _seconds: None,
        )
        runner._run_deadline = 10.0
        with self.assertRaises(test_extension.TestExtensionError) as caught:
            runner.start_daemon()
        self.assertEqual(caught.exception.code, test_extension.WALL_CLOCK_CODE)
        runner._run_deadline = None
        self.assertTrue(runner.cleanup())

    def test_terminal_copy_requires_editor_stop_and_disclaims_terminal_close(self) -> None:
        source = Path(test_extension.__file__).read_text(encoding="utf-8")
        self.assertIn("Keep this terminal open; use Stop in the editor tab", source)
        self.assertIn("Closing it is an unproven emergency abort", source)
        self.assertNotIn("Close this terminal or choose Stop", source)

    def test_source_fingerprint_detects_mutation_and_pycache_is_routed_to_plane(self) -> None:
        layout = self.layout("te-source-immutability")

        def command(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"status": "OK"}), stderr="")

        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable), code_path=self.temp / "Code.exe",
            command_runner=command,
        )
        runner.initialize()
        self.assertTrue(runner.verify_source_immutable())
        (self.source / "kaizen.py").write_text("raise SystemExit(9)\n", encoding="utf-8")
        self.assertFalse(runner.verify_source_immutable())
        env = runner.child_env()
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["PYTHONPYCACHEPREFIX"], str(layout.temp / "pycache"))

    def test_child_env_strips_host_editor_controls_but_preserves_vendor_identity(self) -> None:
        layout = self.layout("te-child-env")
        runner = test_extension.OuterRunner(
            source_root=self.source, layout=layout, python=Path(sys.executable), code_path=self.temp / "Code.exe",
        )
        inherited = {
            "ELECTRON_RUN_AS_NODE": "1",
            "VSCODE_CODE_CACHE_PATH": "protected-cache",
            "vscode_ipc_hook": "stale-ipc",
            "USERPROFILE": "vendor-profile",
            "HOME": "vendor-home",
            "CLAUDE_CONFIG_DIR": "vendor-claude",
        }
        with mock.patch.dict(os.environ, inherited, clear=False):
            env = runner.child_env()
        self.assertFalse(any(key.upper() == "ELECTRON_RUN_AS_NODE" for key in env))
        self.assertFalse(any(key.upper().startswith("VSCODE_") for key in env))
        self.assertEqual(env["USERPROFILE"], "vendor-profile")
        self.assertEqual(env["HOME"], "vendor-home")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "vendor-claude")

    def test_visible_runner_disables_bytecode_before_repository_import(self) -> None:
        source = (REPO_ROOT / "tests" / "run_test_extension.py").read_text(encoding="utf-8")
        self.assertLess(source.index("sys.dont_write_bytecode = True"), source.index("from kaizen_components"))

    def test_source_fingerprint_covers_shipped_media_and_restoration(self) -> None:
        assets = {
            "extension/media/kaizen.svg": b"<svg/>\n",
            "extension/media/webview/chat.html": b"<main>Kaizen</main>\n",
            "extension/media/webview/chat.css": b"main { display: block; }\n",
        }
        for relative, body in assets.items():
            target = self.source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)

        baseline = test_extension.source_fingerprint(self.source)
        self.assertTrue(set(assets).issubset(baseline))
        for relative, body in assets.items():
            with self.subTest(relative=relative):
                target = self.source / relative
                target.write_bytes(body + b"mutation\n")
                self.assertNotEqual(test_extension.source_fingerprint(self.source), baseline)
                target.write_bytes(body)
                self.assertEqual(test_extension.source_fingerprint(self.source), baseline)


class TestExtensionPreparedOllamaPathTest(_DrivenSubprocess):
    """Drive a prepared isolated plane through the real supervisor local-provider path and prove governed-context delivery without capability mutation."""
    def test_prepared_governed_context_reaches_local_provider_without_capability_mutation(self) -> None:
        out = self.drive(
            "import os\n"
            "from pathlib import Path\n"
            "from kaizen_components.orchestration import test_extension as TE\n"
            "root=Path(os.environ['KAIZEN_REPO_ROOT']).resolve(); plane=root/'AI'/'work'/'te-prepared-path'; actions=plane/'actions'\n"
            "layout=TE.RunLayout('te-prepared-path',plane,root,plane/'user-data',plane/'extensions',plane/'tmp',plane/'evidence.jsonl',plane/'client-evidence.jsonl',plane/'control.json',actions,actions/'requests',actions/'receipts',actions/'suite.json',plane/'manifest.json',plane/'edh-ready.json')\n"
            "source=Path(TE.__file__).resolve().parents[2]; codewords=TE.prepare_plane(layout,source)\n"
            "sup=Supervisor(); sup._probe_ollama_models=lambda:([{'id':'fixture','label':'Fixture','reasoning_efforts':[]}],[],'available'); sup.boot()\n"
            "provider=scripted_provider([final_reply(codewords['context']+' '+codewords['selection'])]); install_factory(sup,provider)\n"
            "selection=('unsaved dirty selection codeword '+codewords['selection']).encode('utf-8'); staged=sup._artifact_cache.store('context',selection,scope_id='te-prepared-path',origin='selection')\n"
            "refs=[{'id':'te-file','kind':'file','source_path':'te-context.txt'},{'id':'te-selection','kind':'selection','source_path':'te-selection.txt','range':{'start':{'line':0,'character':0},'end':{'line':0,'character':len(selection.decode('utf-8'))}},'snapshot_ref':staged.artifact_ref,'sha256':staged.sha256,'bytes':staged.bytes,'encoding':'utf-8'}]\n"
            "features=next(item['features'] for item in sup._capabilities if item['id']=='local_llm')\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'local_llm','model':'fixture','prompt':'use prepared context','profile':{'permission_mode':'plan'},'context_refs':refs}})\n"
            "rid=start.get('agent_run_id'); wait_idle(sup,rid,budget=20.0) if rid else None\n"
            "calls=provider.state['calls']; governed=next((message['content'] for message in calls[0] if message['role']=='user' and 'KAIZEN_GOVERNED_CONTEXT_V1' in message['content']),'') if calls else ''\n"
            "close=sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}}) if rid else {}; sup.shutdown()\n"
            "out={'features':features,'start':start,'provider_calls':len(calls),'context_seen':codewords['context'] in governed,'selection_seen':codewords['selection'] in governed,'close':close}\n",
            env={"PATH": str(REPO_ROOT)},
        )
        self.assertTrue(out["features"]["governed_context"], out)
        self.assertFalse(out["features"]["image_attachments"], out)
        self.assertEqual(out["start"]["status"], "OK", out)
        self.assertEqual(out["provider_calls"], 1, out)
        self.assertTrue(out["context_seen"], out)
        self.assertTrue(out["selection_seen"], out)
        self.assertEqual(out["close"]["status"], "OK", out)


if __name__ == "__main__":
    unittest.main()
