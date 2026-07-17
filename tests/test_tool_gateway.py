"""Contract tests for bounded read/list/search/process/proposal lanes, policy and approval ordering, cancellation, exact apply extent, and stale-race handling."""
from __future__ import annotations

import os
import io
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from kaizen_components.orchestration import children, policy
from kaizen_components.orchestration.adapters import BrokerApprovalResult
from kaizen_components.orchestration.proposal_executor import WorkspaceProposalExecutor
from kaizen_components.orchestration.tool_gateway import (
    LIST_FILES,
    PROPOSE_CHANGES,
    READ_FILE,
    RUN_PROCESS,
    SEARCH_TEXT,
    ToolContext,
    ToolGateway,
)


WORK_ROOT = Path(__file__).resolve().parents[1] / "AI" / "work" / "tool-gateway-tests"


def _decision(result: str, *, invariant_id: str | None = None) -> policy.Decision:
    return policy.Decision(
        result=result,
        reason=f"test {result}",
        dedupe_key=f"test-{result}",
        invariant_id=invariant_id,
    )


class ToolGatewayTest(unittest.TestCase):
    """Uses an isolated workspace plane and closes gateways in reverse creation order after each tool-policy scenario."""
    def setUp(self) -> None:
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="plane-", dir=WORK_ROOT)
        self.root = Path(self.temp.name)
        self.actions: list[policy.RequestedAction] = []
        self.gateways: list[ToolGateway] = []
        self.context = ToolContext(
            actor=policy.Actor("claude", "session-test", 7, thread_id="turn-test"),
            current_epoch=7,
            tool_call_id="tool-test",
        )

    def tearDown(self) -> None:
        for gateway in reversed(self.gateways):
            gateway.close()
        self.temp.cleanup()

    def gateway(self, result: str = policy.ALLOW, **kwargs):
        def decide(action: policy.RequestedAction, _epoch: int) -> policy.Decision:
            self.actions.append(action)
            return _decision(result)

        gateway = ToolGateway(self.root, decide=decide, **kwargs)
        self.gateways.append(gateway)
        return gateway

    def policy_gateway(self, mode: str, **kwargs) -> ToolGateway:
        snapshot = policy.build_policy_snapshot(
            "claude", mode, str(self.root), [], [], protected_paths=[], vendor_config_paths=[],
        )
        gateway = ToolGateway(self.root, decide=snapshot.decide, **kwargs)
        self.gateways.append(gateway)
        return gateway

    def proposal_baselines(self, proposal):
        executor = WorkspaceProposalExecutor(self.root)
        try:
            return executor.prepare(proposal).baselines
        finally:
            executor.close()

    def test_governed_utf8_read_is_ranged_hashed_and_policy_gated(self) -> None:
        (self.root / "note.txt").write_bytes(b"one\ntwo\nthree\n")
        result = self.gateway().execute(
            READ_FILE,
            {"path": "note.txt", "start_line": 2, "end_line": 2},
            self.context,
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["result"]["text"], "two\n")
        self.assertTrue(result["result"]["truncated"])
        self.assertEqual(self.actions[0].verb, "file_read")
        self.assertEqual(self.actions[0].targets, ("note.txt",))

    def test_large_source_allows_bounded_range_with_full_hash_and_no_source_size_rejection(self) -> None:
        content = "".join(f"line-{index:04} " + "x" * 180 + "\n" for index in range(3_000))
        encoded = content.encode("utf-8")
        self.assertGreater(len(encoded), 256 * 1024)
        (self.root / "large.txt").write_bytes(encoded)

        result = self.gateway().execute(
            READ_FILE,
            {"path": "large.txt", "start_line": 2_500, "end_line": 2_501, "max_bytes": 1_024},
            self.context,
        )

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["result"]["text"], "".join(content.splitlines(keepends=True)[2_499:2_501]))
        self.assertEqual(result["result"]["total_lines"], 3_000)
        self.assertEqual(result["result"]["sha256"], hashlib.sha256(encoded).hexdigest())
        self.assertTrue(result["result"]["truncated"])

    def test_large_unselected_line_stays_bounded_but_late_binary_still_denies(self) -> None:
        (self.root / "wide.txt").write_bytes(b"z" * (300 * 1024) + b"\nselected\n")
        ranged = self.gateway().execute(
            READ_FILE,
            {"path": "wide.txt", "start_line": 2, "end_line": 2, "max_bytes": 32},
            self.context,
        )
        self.assertEqual(ranged["status"], "OK")
        self.assertEqual(ranged["result"]["text"], "selected\n")

        (self.root / "late-binary.txt").write_bytes(b"selected\n" + b"x" * (300 * 1024) + b"\x00")
        denied = self.gateway().execute(
            READ_FILE,
            {"path": "late-binary.txt", "start_line": 1, "end_line": 1, "max_bytes": 32},
            self.context,
        )
        self.assertEqual(denied["code"], "DENIED_TOOL_BINARY")

    def test_read_rejects_binary_oversize_unknown_fields_and_escape(self) -> None:
        (self.root / "binary.bin").write_bytes(b"abc\x00def")
        gateway = self.gateway()
        self.assertEqual(
            gateway.execute(READ_FILE, {"path": "binary.bin"}, self.context)["code"],
            "DENIED_TOOL_BINARY",
        )
        (self.root / "text.txt").write_text("abcdef", encoding="utf-8")
        with mock.patch("kaizen_components.orchestration.tool_gateway.READ_MAX_BYTES", 3):
            self.assertEqual(
                gateway.execute(READ_FILE, {"path": "text.txt"}, self.context)["code"],
                "DENIED_TOOL_TOO_LARGE",
            )
        self.assertEqual(
            gateway.execute(READ_FILE, {"path": "binary.bin", "raw": True}, self.context)["code"],
            "DENIED_TOOL_INPUT_INVALID",
        )
        self.assertEqual(
            gateway.execute(READ_FILE, {"path": "../outside.txt"}, self.context)["code"],
            "DENIED_TOOL_PATH_INVALID",
        )

    def test_every_existing_reparse_component_is_rejected(self) -> None:
        target = self.root / "real"
        target.mkdir()
        (target / "value.txt").write_text("x", encoding="utf-8")
        link = self.root / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
            gateway = self.gateway()
            result = gateway.execute(READ_FILE, {"path": "link/value.txt"}, self.context)
        except OSError:
            link.mkdir()
            (link / "value.txt").write_text("x", encoding="utf-8")
            gateway = self.gateway()
            original = gateway._is_reparse
            self.assertFalse(original(link), "fallback emulates reparse detection on a real directory")
            with mock.patch.object(
                gateway,
                "_is_reparse",
                side_effect=lambda path: path == link or original(path),
            ):
                result = gateway.execute(READ_FILE, {"path": "link/value.txt"}, self.context)
        self.assertEqual(result["code"], "DENIED_TOOL_PATH_REPARSE")

    def test_list_is_sorted_bounded_and_never_descends_past_depth(self) -> None:
        (self.root / "b.txt").write_text("b", encoding="utf-8")
        (self.root / "A.txt").write_text("a", encoding="utf-8")
        (self.root / "dir").mkdir()
        (self.root / "dir" / "deep.txt").write_text("deep", encoding="utf-8")
        result = self.gateway().execute(
            LIST_FILES,
            {"path": ".", "max_depth": 0, "max_entries": 2},
            self.context,
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual([item["path"] for item in result["result"]["entries"]], ["A.txt", "b.txt"])
        self.assertTrue(result["result"]["truncated"])
        self.assertNotIn("dir/deep.txt", [item["path"] for item in result["result"]["entries"]])
        with mock.patch("kaizen_components.orchestration.tool_gateway.STRUCTURED_OUTPUT_MAX_BYTES", 4):
            byte_bounded = self.gateway().execute(LIST_FILES, {"path": "."}, self.context)
        self.assertEqual(byte_bounded["result"]["entries"], [])
        self.assertTrue(byte_bounded["result"]["truncated"])

    def test_search_supports_literal_regex_bounds_and_binary_skip(self) -> None:
        (self.root / "a.txt").write_text("Alpha one\nbeta 22\n", encoding="utf-8")
        (self.root / "b.txt").write_text("alpha two\n", encoding="utf-8")
        (self.root / "raw.bin").write_bytes(b"alpha\x00hidden")
        gateway = self.gateway()
        literal = gateway.execute(
            SEARCH_TEXT,
            {"query": "ALPHA", "mode": "literal", "glob": "*.txt"},
            self.context,
        )
        self.assertEqual([item["path"] for item in literal["result"]["matches"]], ["a.txt", "b.txt"])
        regex = gateway.execute(
            SEARCH_TEXT,
            {"query": r"\d{2}$", "mode": "regex", "case_sensitive": True},
            self.context,
        )
        self.assertEqual(regex["result"]["matches"][0]["line"], 2)
        self.assertEqual(regex["result"]["skipped_binary"], 1)
        invalid = gateway.execute(SEARCH_TEXT, {"query": "(", "mode": "regex"}, self.context)
        self.assertEqual(invalid["code"], "DENIED_TOOL_REGEX_INVALID")

    def test_list_and_search_defaults_match_the_model_schema_without_search_depth(self) -> None:
        for index in range(210):
            (self.root / f"entry-{index:03}.txt").write_text("needle", encoding="utf-8")
        default_list = self.gateway().execute(LIST_FILES, {"path": "."}, self.context)
        self.assertEqual(default_list["status"], "OK")
        self.assertEqual(len(default_list["result"]["entries"]), 210)
        self.assertFalse(default_list["result"]["truncated"])

        lines = "".join(f"needle {index}\n" for index in range(110))
        (self.root / "matches.txt").write_text(lines, encoding="utf-8")
        default_search = self.gateway().execute(
            SEARCH_TEXT, {"query": "needle", "path": "matches.txt"}, self.context,
        )
        self.assertEqual(default_search["status"], "OK")
        self.assertEqual(len(default_search["result"]["matches"]), 110)
        self.assertFalse(default_search["result"]["truncated"])

        unadvertised = self.gateway().execute(
            SEARCH_TEXT, {"query": "needle", "max_depth": 1}, self.context,
        )
        self.assertEqual(unadvertised["code"], "DENIED_TOOL_INPUT_INVALID")

    def test_list_and_search_glob_bound_matches_worker_schema(self) -> None:
        accepted = self.gateway().execute(
            LIST_FILES, {"glob": "x" * 1_024}, self.context,
        )
        self.assertEqual(accepted["status"], "OK")
        denied = self.gateway().execute(
            SEARCH_TEXT, {"query": "x", "glob": "x" * 1_025}, self.context,
        )
        self.assertEqual(denied["code"], "DENIED_TOOL_INPUT_INVALID")

    def test_process_is_direct_argv_owned_bounded_and_reports_unknown_effects(self) -> None:
        result = self.gateway().execute(
            RUN_PROCESS,
            {"executable": sys.executable, "argv": ["-c", "print('direct-ok')"], "timeout_ms": 10_000},
            self.context,
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["result"]["exit_code"], 0)
        self.assertEqual(result["result"]["stdout"].strip(), "direct-ok")
        self.assertTrue(result["result"]["effects_unknown"])
        self.assertEqual(self.actions[0].verb, "exec")

    def test_process_environment_is_allowlisted_scrubbed_and_workspace_scoped(self) -> None:
        secrets = {
            "OPENAI_API_KEY": "openai-tool-gateway-secret",
            "ANTHROPIC_API_KEY": "anthropic-tool-gateway-secret",
            "KAIZEN_TEST_SECRET": "ambient-tool-gateway-secret",
        }
        probe = "import json,os; print(json.dumps(dict(os.environ), sort_keys=True))"
        with mock.patch.dict(os.environ, secrets, clear=False):
            result = self.gateway().execute(
                RUN_PROCESS,
                {"executable": sys.executable, "argv": ["-c", probe], "timeout_ms": 10_000},
                self.context,
            )
        self.assertEqual(result["status"], "OK", result)
        observed = json.loads(result["result"]["stdout"])
        allowed = {
            "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SystemDrive",
            "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR",
            "PYTHONUTF8", "PYTHONIOENCODING",
        }
        self.assertLessEqual({key.casefold() for key in observed}, {key.casefold() for key in allowed})
        for key in secrets:
            self.assertNotIn(key, observed)
        runtime = (self.root / "AI" / "work" / "orchestration" / "tool-runtime").resolve()
        for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR"):
            self.assertEqual(Path(observed[key]).resolve(), runtime)
        self.assertEqual(observed["PYTHONUTF8"], "1")
        self.assertEqual(observed["PYTHONIOENCODING"], "utf-8")

    def test_process_rejects_every_unadvertised_side_channel(self) -> None:
        gateway = self.gateway()
        for field, value in (("env", {}), ("stdin", "x"), ("detach", True)):
            result = gateway.execute(
                RUN_PROCESS,
                {"executable": sys.executable, "argv": ["-V"], field: value},
                self.context,
            )
            self.assertEqual(result["code"], "DENIED_TOOL_INPUT_INVALID")

    def test_worker_facing_process_and_proposal_boundaries_match_gateway(self) -> None:
        spawner = mock.Mock(side_effect=RuntimeError("bounded input reached owned spawn seam"))
        gateway = self.gateway(spawner=spawner)

        accepted_count = gateway.execute(
            RUN_PROCESS,
            {"executable": sys.executable, "argv": [""] * 64},
            self.context,
        )
        self.assertEqual(accepted_count["code"], "ERROR_TOOL_PROCESS_SPAWN")
        self.assertEqual(spawner.call_count, 1)
        denied_count = gateway.execute(
            RUN_PROCESS,
            {"executable": sys.executable, "argv": [""] * 65},
            self.context,
        )
        self.assertEqual(denied_count["code"], "DENIED_TOOL_INPUT_INVALID")
        self.assertEqual(spawner.call_count, 1)

        accepted_utf8 = gateway.execute(
            RUN_PROCESS,
            {"executable": sys.executable, "argv": ["é" * 2_048]},
            self.context,
        )
        self.assertEqual(accepted_utf8["code"], "ERROR_TOOL_PROCESS_SPAWN")
        self.assertEqual(spawner.call_count, 2)
        denied_utf8 = gateway.execute(
            RUN_PROCESS,
            {"executable": sys.executable, "argv": ["é" * 2_049]},
            self.context,
        )
        self.assertEqual(denied_utf8["code"], "DENIED_TOOL_INPUT_INVALID")
        self.assertEqual(spawner.call_count, 2)

        accepted_query = gateway.execute(
            SEARCH_TEXT, {"query": "é" * 2_048}, self.context,
        )
        self.assertEqual(accepted_query["status"], "OK")
        denied_query = gateway.execute(
            SEARCH_TEXT, {"query": "é" * 2_049}, self.context,
        )
        self.assertEqual(denied_query["code"], "DENIED_TOOL_INPUT_INVALID")

        accepted_summary = gateway.execute(
            PROPOSE_CHANGES,
            {"summary": "s" * 256, "changes": [{"kind": "create", "path": "x.txt", "content": "x"}]},
            self.context,
        )
        self.assertEqual(accepted_summary["code"], "DENIED_TOOL_UNSUPPORTED")
        denied_summary = gateway.execute(
            PROPOSE_CHANGES,
            {"summary": "s" * 257, "changes": [{"kind": "create", "path": "x.txt", "content": "x"}]},
            self.context,
        )
        self.assertEqual(denied_summary["code"], "DENIED_TOOL_INPUT_INVALID")
        blank_summary = gateway.execute(
            PROPOSE_CHANGES,
            {"summary": "   ", "changes": [{"kind": "create", "path": "x.txt", "content": "x"}]},
            self.context,
        )
        self.assertEqual(blank_summary["code"], "DENIED_TOOL_INPUT_INVALID")

    def test_process_timeout_is_a_fatal_signal(self) -> None:
        result = self.gateway().execute(
            RUN_PROCESS,
            {"executable": sys.executable, "argv": ["-c", "import time;time.sleep(2)"], "timeout_ms": 1_000},
            self.context,
        )
        self.assertEqual(result["code"], "DENIED_TOOL_PROCESS_TIMEOUT")
        self.assertTrue(result["fatal"])
        self.assertTrue(result["effects_unknown"])
        self.assertTrue(result["timed_out"])

    def test_cancel_joins_a_long_running_owned_process(self) -> None:
        gateway = self.gateway()
        result: dict[str, object] = {}

        def run() -> None:
            result.update(gateway.execute(
                RUN_PROCESS,
                {"executable": sys.executable, "argv": ["-c", "import time;time.sleep(30)"],
                 "timeout_ms": 60_000},
                self.context,
            ))

        thread = threading.Thread(target=run)
        thread.start()
        for _attempt in range(100):
            with gateway._active_lock:
                if gateway._active_processes:
                    break
            time.sleep(0.01)
        canceled = gateway.cancel_active_processes(5.0)
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(canceled["termination_proven"])
        self.assertEqual(result["code"], "DENIED_TOOL_PROCESS_CANCELED")
        self.assertTrue(result["fatal"])

    def test_uncertain_process_kill_remains_registered_fail_closed(self) -> None:
        class Process:
            stdout = io.StringIO("")
            stderr = io.StringIO("")
            stdin = None
            returncode = None

            @staticmethod
            def wait(timeout=None):
                time.sleep(0.1)
                raise subprocess.TimeoutExpired("fake", timeout)

            @staticmethod
            def poll():
                return None

        class Child:
            pid = 42
            process = Process()

            @staticmethod
            def poll():
                return None

            @staticmethod
            def kill_tree(timeout=5.0):
                raise RuntimeError("termination unproven")

            @staticmethod
            def release():
                raise AssertionError("live child cannot release")

        gateway = self.gateway(spawner=lambda *_args, **_kwargs: Child())
        result: dict[str, object] = {}
        thread = threading.Thread(target=lambda: result.update(gateway.execute(
            RUN_PROCESS,
            {"executable": "fake.exe", "timeout_ms": 1_000},
            self.context,
        )))
        thread.start()
        for _attempt in range(100):
            with gateway._active_lock:
                if gateway._active_processes:
                    break
            time.sleep(0.01)
        else:
            self.fail("fake child was not registered before cancellation")
        canceled = gateway.cancel_active_processes(0.5)
        thread.join(timeout=2.0)
        self.assertFalse(canceled["termination_proven"])
        self.assertEqual(canceled["active_count"], 1)
        self.assertEqual(result["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")

    def test_process_output_flood_is_drained_with_one_total_retention_cap(self) -> None:
        script = "import os;os.write(1,b'o'*1048576);os.write(2,b'e'*1048576)"
        result = self.gateway().execute(
            RUN_PROCESS,
            {"executable": sys.executable, "argv": ["-c", script], "timeout_ms": 10_000},
            self.context,
        )
        self.assertEqual(result["status"], "OK")
        retained = len(result["result"]["stdout"].encode("utf-8")) \
            + len(result["result"]["stderr"].encode("utf-8"))
        self.assertLessEqual(retained, 256 * 1024)
        self.assertTrue(result["result"]["truncated"])

    def test_catastrophic_regex_is_killed_by_isolated_timeout(self) -> None:
        (self.root / "catastrophic.txt").write_text("a" * 200_000 + "!", encoding="utf-8")
        result = self.gateway().execute(
            SEARCH_TEXT,
            {"query": "(a+)+$", "mode": "regex", "path": "catastrophic.txt"},
            self.context,
        )
        self.assertEqual(result["code"], "DENIED_TOOL_REGEX_TIMEOUT")
        self.assertFalse(result["fatal"])

    def test_process_ask_rejection_is_recoverable_and_never_spawns(self) -> None:
        spawner = mock.Mock(side_effect=AssertionError("must not spawn"))
        result = self.gateway(policy.ASK, approval_broker=lambda _request: "denied", spawner=spawner).execute(
            RUN_PROCESS,
            {"executable": sys.executable, "argv": ["-V"]},
            self.context,
        )
        self.assertEqual(result["code"], "DENIED_TOOL_REJECTED")
        self.assertFalse(result["fatal"])
        spawner.assert_not_called()

    def test_process_ask_broker_precedes_mutation_guard(self) -> None:
        order: list[str] = []

        def broker(_request):
            order.append("broker")
            return "approved"

        def guard(_action):
            order.append("guard")
            return None

        result = self.gateway(policy.ASK, approval_broker=broker, mutation_guard=guard).execute(
            RUN_PROCESS,
            {"executable": sys.executable, "argv": ["-c", "print('ok')"]},
            self.context,
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(order, ["broker", "guard"])

    def test_real_policy_process_matrix_requires_approval_except_explicit_full(self) -> None:
        command = {
            "executable": sys.executable,
            "argv": ["-c", "print('matrix-ok')"],
            "cwd": ".",
            "timeout_ms": 10_000,
        }
        for mode in ("plan", "ask", "agent", "full"):
            with self.subTest(mode=mode):
                broker = mock.Mock(return_value="approved")
                spawned: list[list[str]] = []

                def spawn(argv, **kwargs):
                    spawned.append(list(argv))
                    return children.spawn_owned(argv, **kwargs)

                result = self.policy_gateway(mode, approval_broker=broker, spawner=spawn).execute(
                    RUN_PROCESS, command, self.context,
                )
                if mode == "plan":
                    self.assertEqual(result["code"], "MODE_CEILING:exec")
                    self.assertEqual(spawned, [])
                    broker.assert_not_called()
                    continue
                self.assertEqual(result["status"], "OK")
                self.assertEqual(result["result"]["effects_unknown"], True)
                self.assertEqual(spawned, [[sys.executable, "-c", "print('matrix-ok')"]])
                if mode in ("ask", "agent"):
                    broker.assert_called_once()
                    self.assertEqual(result["result"]["policy_decision"], "ask")
                else:
                    broker.assert_not_called()
                    self.assertEqual(result["result"]["policy_decision"], "allow")

    def test_real_policy_ask_and_agent_fail_closed_without_explicit_approval(self) -> None:
        for mode in ("ask", "agent"):
            with self.subTest(mode=mode):
                spawner = mock.Mock(side_effect=AssertionError("must not spawn"))
                result = self.policy_gateway(mode, spawner=spawner).execute(
                    RUN_PROCESS, {"executable": sys.executable, "argv": ["-V"]}, self.context,
                )
                self.assertEqual(result["code"], "ERROR_TOOL_APPROVAL_TRANSPORT")
                self.assertTrue(result["fatal"])
                spawner.assert_not_called()

    def test_real_full_policy_invariant_denies_direct_argv_git_push_before_spawn(self) -> None:
        broker = mock.Mock(side_effect=AssertionError("invariant must not broker"))
        spawner = mock.Mock(side_effect=AssertionError("invariant must not spawn"))
        result = self.policy_gateway("full", approval_broker=broker, spawner=spawner).execute(
            RUN_PROCESS,
            {"executable": "git", "argv": ["push", "origin", "main"], "cwd": ".", "timeout_ms": 5_000},
            self.context,
        )
        self.assertEqual(result["code"], policy.INV_GIT_PUSH)
        self.assertFalse(result["fatal"])
        broker.assert_not_called()
        spawner.assert_not_called()

    @staticmethod
    def apply_proposal(root: Path, value) -> dict[str, object]:
        """Minimal non-authoritative applier used only to prove whole-request broker ordering and preservation of create/modify/delete/rename kinds."""
        for change in value["changes"]:
            target = root / change["path"]
            if change["kind"] == "delete":
                target.unlink()
            elif change["kind"] == "rename":
                source = root / change["old_path"]
                source.rename(target)
                if "content" in change:
                    target.write_text(change["content"], encoding="utf-8")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(change["content"], encoding="utf-8")
        return {"status": "OK"}

    def test_whole_request_proposal_broker_precedes_apply_and_preserves_all_kinds(self) -> None:
        (self.root / "modify.txt").write_text("before", encoding="utf-8")
        (self.root / "delete.txt").write_text("delete", encoding="utf-8")
        (self.root / "old.txt").write_text("old", encoding="utf-8")
        captured: dict[str, object] = {}

        def broker(request):
            captured.update(request)
            self.assertFalse((self.root / "create.txt").exists())
            return BrokerApprovalResult(
                "approved",
                updated_input=request["diff_request"]["updated_input"],
                post_apply=lambda: {"status": "OK", "partial_apply": False, "mismatches": []},
            )

        proposal = {
            "summary": "all kinds",
            "changes": [
                {"kind": "create", "path": "create.txt", "content": "new"},
                {"kind": "modify", "path": "modify.txt", "content": "after"},
                {"kind": "delete", "path": "delete.txt"},
                {"kind": "rename", "old_path": "old.txt", "path": "renamed.txt"},
            ],
        }
        result = self.gateway(
            approval_broker=broker,
            proposal_executor=lambda value: self.apply_proposal(self.root, value),
        ).execute(PROPOSE_CHANGES, proposal, self.context)
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["result"]["applied"])
        self.assertEqual((self.root / "create.txt").read_text(encoding="utf-8"), "new")
        self.assertEqual((self.root / "modify.txt").read_text(encoding="utf-8"), "after")
        self.assertFalse((self.root / "delete.txt").exists())
        self.assertEqual((self.root / "renamed.txt").read_text(encoding="utf-8"), "old")
        self.assertTrue(captured["negotiated"])
        diff = captured["diff_request"]["changes"]
        self.assertEqual({item["kind"] for item in diff}, {"create", "modify", "delete", "rename"})
        self.assertIn("proposed_content", diff[0])

    def test_proposal_rejection_is_recoverable_and_never_mutates(self) -> None:
        executor = mock.Mock(side_effect=AssertionError("must not execute"))
        result = self.gateway(
            approval_broker=lambda _request: "denied",
            proposal_executor=executor,
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "create", "path": "never.txt", "content": "x"}]},
            self.context,
        )
        self.assertEqual(result["code"], "DENIED_TOOL_REJECTED")
        self.assertFalse(result["fatal"])
        self.assertFalse((self.root / "never.txt").exists())
        executor.assert_not_called()

    def test_shared_executor_stale_race_runs_broker_cleanup_but_reports_zero_apply_mutation(self) -> None:
        target = self.root / "race.txt"
        target.write_text("approved-base", encoding="utf-8")
        audit = mock.Mock(return_value={
            "status": "DENIED",
            "partial_apply": False,
            "mismatches": [{"path": "race.txt", "reason": "audit_expected_proposed"}],
        })

        def broker(request):
            target.write_text("changed-after-preview", encoding="utf-8")
            return BrokerApprovalResult(
                "approved",
                updated_input=request["diff_request"]["updated_input"],
                post_apply=audit,
            )

        result = self.gateway(
            approval_broker=broker,
            proposal_executor=WorkspaceProposalExecutor(self.root),
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "modify", "path": "race.txt", "content": "proposed"}]},
            self.context,
        )

        self.assertEqual(result["code"], "DENIED_APPROVAL_STALE_RERUN_REQUIRED")
        self.assertTrue(result["fatal"])
        self.assertEqual(result["apply_extent"], "none")
        self.assertFalse(result["partial_apply"])
        self.assertEqual(result["mismatches"][0]["path"], "race.txt")
        self.assertEqual(target.read_text(encoding="utf-8"), "changed-after-preview")
        audit.assert_called_once_with()

    def test_preapply_denial_remains_none_when_post_state_is_externally_uncertain(self) -> None:
        class Prepared:
            baselines = ()

            @staticmethod
            def apply(_approved):
                raise AssertionError("preapply denial must not execute")

        class Executor:
            @staticmethod
            def prepare(_proposal):
                return Prepared()

        result = self.gateway(
            approval_broker=lambda request: BrokerApprovalResult(
                "approved",
                updated_input=request["diff_request"]["updated_input"],
                post_apply=lambda: {
                    "status": "DENIED", "apply_extent": "uncertain", "partial_apply": True,
                    "mismatches": [{"path": "", "reason": "apply_outcome_uncertain"}],
                },
            ),
            mutation_guard=lambda _action: {
                "status": "DENIED", "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                "required_action": "retry after the active writer finishes",
            },
            proposal_executor=Executor(),
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "create", "path": "external.txt", "content": "x"}]},
            self.context,
        )

        self.assertEqual(result["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertEqual(result["apply_extent"], "none")
        self.assertFalse(result["partial_apply"])
        self.assertTrue(result["mismatch_evidence_uncertain"])

    def test_prepared_stale_denial_remains_none_when_post_state_is_externally_uncertain(self) -> None:
        class Prepared:
            baselines = ()

            @staticmethod
            def apply(_approved):
                return {
                    "status": "DENIED",
                    "code": "DENIED_APPROVAL_STALE_RERUN_REQUIRED",
                    "partial_apply": False,
                    "mismatches": [{"path": "stale.txt", "reason": "approved_base_changed"}],
                }

        class Executor:
            @staticmethod
            def prepare(_proposal):
                return Prepared()

        result = self.gateway(
            approval_broker=lambda request: BrokerApprovalResult(
                "approved",
                updated_input=request["diff_request"]["updated_input"],
                post_apply=lambda: {
                    "status": "DENIED", "apply_extent": "uncertain", "partial_apply": True,
                    "mismatches": [{
                        "path": "stale.txt", "reason": "final_state_mismatch",
                        "expected_exists": True, "actual_exists": True,
                        "expected_sha256": "a" * 64, "actual_sha256": "b" * 64,
                    }],
                },
            ),
            proposal_executor=Executor(),
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "create", "path": "stale.txt", "content": "x"}]},
            self.context,
        )

        self.assertEqual(result["code"], "DENIED_APPROVAL_STALE_RERUN_REQUIRED")
        self.assertEqual(result["apply_extent"], "none")
        self.assertFalse(result["partial_apply"])
        self.assertTrue(result["mismatch_evidence_uncertain"])

    def test_apply_recovery_factory_snapshots_only_after_mutation_guard(self) -> None:
        order: list[str] = []

        def guard(_action):
            order.append("guard")
            return None

        def factory():
            self.assertEqual(order, ["guard"])
            order.append("factory")

            def persist(state):
                order.append("clear" if state is None else str(state["phase"]))
                return True

            return persist

        result = self.gateway(
            approval_broker=lambda request: BrokerApprovalResult(
                "approved",
                updated_input=request["diff_request"]["updated_input"],
                post_apply=lambda: {"status": "OK", "partial_apply": False, "mismatches": []},
            ),
            mutation_guard=guard,
            proposal_executor=WorkspaceProposalExecutor(self.root),
            recovery_callback_factory=factory,
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "create", "path": "captured.txt", "content": "safe"}]},
            self.context,
        )

        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(order, ["guard", "factory", "staging", "mutating", "clear"])
        self.assertEqual((self.root / "captured.txt").read_text(encoding="utf-8"), "safe")

    def test_independent_partial_audit_dominates_executor_false(self) -> None:
        result = self.gateway(
            approval_broker=lambda request: BrokerApprovalResult(
                "approved",
                updated_input=request["diff_request"]["updated_input"],
                post_apply=lambda: {
                    "status": "DENIED",
                    "partial_apply": True,
                    "mismatches": [{"path": "partial.txt", "reason": "final_state_mismatch"}],
                },
            ),
            proposal_executor=lambda _proposal: {
                "status": "DENIED",
                "code": "DENIED_APPROVAL_STALE_RERUN_REQUIRED",
                "partial_apply": False,
                "mismatches": [],
            },
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "create", "path": "partial.txt", "content": "x"}]},
            self.context,
        )

        self.assertTrue(result["partial_apply"])
        self.assertEqual(result["apply_extent"], "partial")
        self.assertEqual(result["mismatches"][0]["path"], "partial.txt")

    def test_complete_post_apply_extent_does_not_fabricate_partial_for_recovery_denial(self) -> None:
        result = self.gateway(
            approval_broker=lambda request: BrokerApprovalResult(
                "approved",
                updated_input=request["diff_request"]["updated_input"],
                post_apply=lambda: {
                    "status": "OK", "apply_extent": "complete",
                    "partial_apply": False, "mismatches": [],
                },
            ),
            proposal_executor=lambda _proposal: {
                "status": "DENIED",
                "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                "partial_apply": False,
                "mismatches": [],
            },
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "create", "path": "complete.txt", "content": "x"}]},
            self.context,
        )

        self.assertEqual(result["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertEqual(result["apply_extent"], "complete")
        self.assertFalse(result["partial_apply"])

    def test_authoritative_none_or_complete_extent_overrides_prepared_executor_partial_claim(self) -> None:
        class Prepared:
            baselines = ()

            @staticmethod
            def apply(_approved):
                return {
                    "status": "DENIED",
                    "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                    "partial_apply": True,
                    "mismatches": [],
                }

        class Executor:
            @staticmethod
            def prepare(_proposal):
                return Prepared()

        for extent, status in (("none", "DENIED"), ("complete", "OK")):
            with self.subTest(extent=extent):
                result = self.gateway(
                    approval_broker=lambda request, value=extent, audit_status=status: BrokerApprovalResult(
                        "approved",
                        updated_input=request["diff_request"]["updated_input"],
                        post_apply=lambda: {
                            "status": audit_status, "apply_extent": value,
                            "partial_apply": False, "mismatches": [],
                        },
                    ),
                    proposal_executor=Executor(),
                ).execute(
                    PROPOSE_CHANGES,
                    {"changes": [{"kind": "create", "path": "exact.txt", "content": "x"}]},
                    self.context,
                )

                self.assertEqual(result["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
                self.assertEqual(result["apply_extent"], extent)
                self.assertFalse(result["partial_apply"])

    def test_prepared_executor_cleanup_uncertainty_does_not_override_complete_audit_extent(self) -> None:
        class Prepared:
            baselines = ()

            @staticmethod
            def apply(_approved):
                return {
                    "status": "DENIED",
                    "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                    "partial_apply": False,
                    "mismatches": [{"path": "", "reason": "staged_cleanup_unproven"}],
                }

        class Executor:
            @staticmethod
            def prepare(_proposal):
                return Prepared()

        result = self.gateway(
            approval_broker=lambda request: BrokerApprovalResult(
                "approved",
                updated_input=request["diff_request"]["updated_input"],
                post_apply=lambda: {
                    "status": "OK", "apply_extent": "complete",
                    "partial_apply": False, "mismatches": [],
                },
            ),
            proposal_executor=Executor(),
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "create", "path": "uncertain.txt", "content": "x"}]},
            self.context,
        )

        self.assertEqual(result["code"], "DENIED_WORKSPACE_RECOVERY_REQUIRED")
        self.assertEqual(result["apply_extent"], "complete")
        self.assertFalse(result["partial_apply"])
        self.assertTrue(result["mismatch_evidence_uncertain"])

    def test_revision_two_nested_content_rebinds_to_exact_approved_base_and_applies(self) -> None:
        target = self.root / "rebased.txt"
        target.write_text("a\nold\nz\n", encoding="utf-8")
        executor = WorkspaceProposalExecutor(self.root, recovery_callback=lambda _state: True)
        audit = mock.Mock(side_effect=lambda: {
            "status": "OK" if target.read_text(encoding="utf-8") == "external\na\nnew\nz\n" else "DENIED",
            "partial_apply": False,
            "mismatches": [],
        })

        def broker(request):
            target.write_text("external\na\nold\nz\n", encoding="utf-8")
            approved = {
                "summary": request["diff_request"]["updated_input"]["summary"],
                "changes": [{
                    "kind": "modify", "path": "rebased.txt", "content": "external\na\nnew\nz\n",
                }],
            }
            contract = self.proposal_baselines(approved)
            return BrokerApprovalResult(
                "approved",
                updated_input=approved,
                post_apply=audit,
                approval_revision=2,
                snapshot_set_sha256="a" * 64,
                approved_bases=contract,
            )

        result = self.gateway(
            approval_broker=broker,
            proposal_executor=executor,
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "modify", "path": "rebased.txt", "content": "a\nnew\nz\n"}]},
            self.context,
        )

        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(target.read_text(encoding="utf-8"), "external\na\nnew\nz\n")
        audit.assert_called_once_with()

    def test_revision_two_rejects_every_non_content_structural_change_and_audits(self) -> None:
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        proposal = {
            "summary": "same structure",
            "changes": [
                {"kind": "modify", "path": "first.txt", "content": "new-first"},
                {"kind": "modify", "path": "second.txt", "content": "new-second"},
            ],
        }
        cases = {
            "summary": {**proposal, "summary": "changed summary"},
            "reordered": {**proposal, "changes": list(reversed(proposal["changes"]))},
            "missing": {**proposal, "changes": proposal["changes"][:1]},
            "path": {**proposal, "changes": [
                {"kind": "modify", "path": "third.txt", "content": "new-first"},
                proposal["changes"][1],
            ]},
            "kind": {**proposal, "changes": [
                {"kind": "create", "path": "first.txt", "content": "new-first"},
                proposal["changes"][1],
            ]},
            "extra_key": {**proposal, "changes": [
                {**proposal["changes"][0], "old_path": "old.txt"},
                proposal["changes"][1],
            ]},
        }
        for name, approved in cases.items():
            with self.subTest(name=name):
                first.write_text("old-first", encoding="utf-8")
                second.write_text("old-second", encoding="utf-8")
                audit = mock.Mock(return_value={"status": "OK", "partial_apply": False, "mismatches": []})
                result = self.gateway(
                    approval_broker=lambda _request, value=approved, callback=audit: BrokerApprovalResult(
                        "approved",
                        updated_input=value,
                        post_apply=callback,
                        approval_revision=2,
                    ),
                    proposal_executor=WorkspaceProposalExecutor(self.root),
                ).execute(PROPOSE_CHANGES, proposal, self.context)
                self.assertEqual(result["code"], "DENIED_APPROVAL_BODY_INVALID", result)
                self.assertTrue(result["fatal"])
                self.assertFalse(result["partial_apply"])
                self.assertEqual(first.read_text(encoding="utf-8"), "old-first")
                self.assertEqual(second.read_text(encoding="utf-8"), "old-second")
                audit.assert_called_once_with()

    def test_approved_base_change_before_reprepare_is_denied_without_apply(self) -> None:
        target = self.root / "base-race.txt"
        target.write_text("approved-base", encoding="utf-8")
        executor = WorkspaceProposalExecutor(self.root)
        audit = mock.Mock(return_value={
            "status": "DENIED", "partial_apply": False,
            "mismatches": [{"path": "base-race.txt", "reason": "not_applied"}],
        })

        def broker(request):
            approved = request["diff_request"]["updated_input"]
            contract = self.proposal_baselines(approved)
            target.write_text("changed-after-validation", encoding="utf-8")
            return BrokerApprovalResult(
                "approved",
                updated_input=approved,
                post_apply=audit,
                approval_revision=1,
                snapshot_set_sha256="b" * 64,
                approved_bases=contract,
            )

        result = self.gateway(
            approval_broker=broker,
            proposal_executor=executor,
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "modify", "path": "base-race.txt", "content": "proposed"}]},
            self.context,
        )

        self.assertEqual(result["code"], "DENIED_APPROVAL_STALE_RERUN_REQUIRED", result)
        self.assertFalse(result["partial_apply"])
        self.assertEqual(target.read_text(encoding="utf-8"), "changed-after-validation")
        audit.assert_called_once_with()

    def test_approved_base_change_after_reprepare_is_caught_by_executor_rehash(self) -> None:
        target = self.root / "late-race.txt"
        target.write_text("approved-base", encoding="utf-8")

        class RacingExecutor(WorkspaceProposalExecutor):
            def __init__(self, root: Path) -> None:
                super().__init__(root)
                self.calls = 0

            def prepare(self, value):
                self.calls += 1
                prepared = super().prepare(value)
                if self.calls != 2:
                    return prepared

                class RacePrepared:
                    baselines = prepared.baselines

                    @staticmethod
                    def apply(approved):
                        target.write_text("changed-after-reprepare", encoding="utf-8")
                        return prepared.apply(approved)

                return RacePrepared()

        executor = RacingExecutor(self.root)
        audit = mock.Mock(return_value={
            "status": "DENIED", "partial_apply": False,
            "mismatches": [{"path": "late-race.txt", "reason": "not_applied"}],
        })

        def broker(request):
            approved = request["diff_request"]["updated_input"]
            return BrokerApprovalResult(
                "approved",
                updated_input=approved,
                post_apply=audit,
                approval_revision=1,
                snapshot_set_sha256="c" * 64,
                approved_bases=self.proposal_baselines(approved),
            )

        result = self.gateway(
            approval_broker=broker,
            proposal_executor=executor,
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "modify", "path": "late-race.txt", "content": "proposed"}]},
            self.context,
        )

        self.assertEqual(result["code"], "DENIED_APPROVAL_STALE_RERUN_REQUIRED", result)
        self.assertFalse(result["partial_apply"])
        self.assertEqual(target.read_text(encoding="utf-8"), "changed-after-reprepare")
        audit.assert_called_once_with()

    def test_proposal_policy_deny_never_reaches_broker(self) -> None:
        broker = mock.Mock(side_effect=AssertionError("must not broker"))

        def decide(_action, _epoch):
            return _decision(policy.DENY, invariant_id="INV_PROTECTED_PATH")

        result = ToolGateway(self.root, decide=decide, approval_broker=broker).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "create", "path": "x.txt", "content": "x"}]},
            self.context,
        )
        self.assertEqual(result["code"], "INV_PROTECTED_PATH")
        broker.assert_not_called()

    def test_proposal_without_authoritative_executor_fails_before_broker_or_mutation(self) -> None:
        target = self.root / "stale.txt"
        target.write_text("before", encoding="utf-8")
        broker = mock.Mock(side_effect=AssertionError("missing executor must not broker"))

        result = self.gateway(policy.ASK, approval_broker=broker).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "modify", "path": "stale.txt", "content": "after"}]},
            self.context,
        )

        self.assertEqual(result["code"], "DENIED_TOOL_UNSUPPORTED")
        self.assertTrue(result["fatal"])
        self.assertEqual(target.read_text(encoding="utf-8"), "before")
        broker.assert_not_called()

    def test_proposal_revalidates_broker_updated_input_before_executor(self) -> None:
        executor = mock.Mock(side_effect=AssertionError("must not execute"))
        result = self.gateway(
            approval_broker=lambda _request: BrokerApprovalResult(
                "approved",
                updated_input={"changes": [{"kind": "create", "path": "other.txt", "content": "x"}]},
                post_apply=lambda: {"status": "OK"},
            ),
            proposal_executor=executor,
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "create", "path": "expected.txt", "content": "x"}]},
            self.context,
        )
        self.assertEqual(result["code"], "DENIED_APPROVAL_BODY_INVALID")
        self.assertTrue(result["fatal"])
        executor.assert_not_called()

    def test_partial_apply_audit_is_fatal(self) -> None:
        mismatches = [{
            "path": "x.txt", "expected_exists": True, "actual_exists": False,
            "expected_sha256": "a" * 64, "actual_sha256": None,
        }]
        result = self.gateway(
            approval_broker=lambda request: BrokerApprovalResult(
                "approved",
                updated_input=request["diff_request"]["updated_input"],
                post_apply=lambda: {"status": "DENIED", "partial_apply": True, "mismatches": mismatches},
            ),
            proposal_executor=lambda _value: {"status": "OK"},
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "create", "path": "x.txt", "content": "x"}]},
            self.context,
        )
        self.assertEqual(result["code"], "DENIED_APPROVAL_STALE_RERUN_REQUIRED")
        self.assertTrue(result["fatal"])
        self.assertTrue(result["partial_apply"])
        self.assertEqual(result["apply_extent"], "partial")
        self.assertEqual(result["mismatches"], [{**mismatches[0], "reason": "final_state_mismatch"}])
        self.assertEqual(result["mismatch_count"], 1)
        self.assertTrue(result["mismatch_evidence_complete"])
        self.assertFalse(result["mismatch_evidence_uncertain"])

    def test_post_apply_exception_is_explicitly_uncertain(self) -> None:
        def audit():
            raise OSError("audit unavailable")

        result = self.gateway(
            approval_broker=lambda request: BrokerApprovalResult(
                "approved",
                updated_input=request["diff_request"]["updated_input"],
                post_apply=audit,
            ),
            proposal_executor=lambda _value: {"status": "OK"},
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "create", "path": "x.txt", "content": "x"}]},
            self.context,
        )

        self.assertTrue(result["fatal"])
        self.assertTrue(result["partial_apply"])
        self.assertEqual(result["apply_extent"], "uncertain")
        self.assertEqual(result["mismatches"], [{
            "path": "", "reason": "post_apply_audit_unavailable",
        }])
        self.assertFalse(result["mismatch_evidence_complete"])
        self.assertTrue(result["mismatch_evidence_uncertain"])

    def test_executor_exception_before_mutation_is_never_claimed_complete(self) -> None:
        def explode(_value):
            raise OSError("executor failed before mutation")

        result = self.gateway(
            approval_broker=lambda request: BrokerApprovalResult(
                "approved",
                updated_input=request["diff_request"]["updated_input"],
                post_apply=lambda: {"status": "OK", "partial_apply": False, "mismatches": []},
            ),
            proposal_executor=explode,
        ).execute(
            PROPOSE_CHANGES,
            {"changes": [{"kind": "create", "path": "x.txt", "content": "x"}]},
            self.context,
        )

        self.assertTrue(result["fatal"])
        self.assertTrue(result["partial_apply"])
        self.assertEqual(result["apply_extent"], "uncertain")
        self.assertEqual(result["mismatches"], [{
            "path": "", "reason": "apply_outcome_uncertain",
        }])
        self.assertFalse(result["mismatch_evidence_complete"])
        self.assertTrue(result["mismatch_evidence_uncertain"])

    def test_executor_exception_after_mutation_retains_exact_audit_rows_plus_uncertainty(self) -> None:
        proposal = {"changes": [
            {"kind": "create", "path": "one.txt", "content": "one"},
            {"kind": "create", "path": "two.txt", "content": "two"},
        ]}

        def mutate_then_explode(_value):
            (self.root / "one.txt").write_text("one", encoding="utf-8")
            raise OSError("executor failed after first mutation")

        exact = {
            "path": "two.txt", "reason": "final_state_mismatch",
            "expected_exists": True, "actual_exists": False,
            "expected_sha256": hashlib.sha256(b"two").hexdigest(), "actual_sha256": None,
        }
        result = self.gateway(
            approval_broker=lambda request: BrokerApprovalResult(
                "approved",
                updated_input=request["diff_request"]["updated_input"],
                post_apply=lambda: {
                    "status": "DENIED", "partial_apply": True, "mismatches": [exact],
                },
            ),
            proposal_executor=mutate_then_explode,
        ).execute(PROPOSE_CHANGES, proposal, self.context)

        self.assertTrue(result["fatal"])
        self.assertTrue(result["partial_apply"])
        self.assertEqual(result["apply_extent"], "uncertain")
        self.assertEqual(result["mismatches"], [
            {"path": "", "reason": "apply_outcome_uncertain"}, exact,
        ])
        self.assertFalse(result["mismatch_evidence_complete"])
        self.assertTrue(result["mismatch_evidence_uncertain"])

    def test_approval_timeout_and_transport_are_fatal_but_explicit_deny_is_not(self) -> None:
        proposal = {"changes": [{"kind": "create", "path": "x.txt", "content": "x"}]}
        executor = mock.Mock(side_effect=AssertionError("denied approval must not execute"))
        timeout = self.gateway(
            approval_broker=lambda _request: {
                "decision": "denied", "code": "DENIED_APPROVAL_TIMEOUT", "fatal": True,
            },
            proposal_executor=executor,
        ).execute(PROPOSE_CHANGES, proposal, self.context)
        self.assertTrue(timeout["fatal"])
        self.assertEqual(timeout["code"], "DENIED_APPROVAL_TIMEOUT")

        def broken(_request):
            raise OSError("transport gone")

        transport = self.gateway(
            approval_broker=broken,
            proposal_executor=executor,
        ).execute(PROPOSE_CHANGES, proposal, self.context)
        self.assertTrue(transport["fatal"])
        self.assertEqual(transport["code"], "ERROR_TOOL_APPROVAL_TRANSPORT")
        executor.assert_not_called()

    def test_proposal_count_content_and_duplicate_path_bounds_are_atomic(self) -> None:
        broker = mock.Mock(side_effect=AssertionError("invalid proposal must not broker"))
        gateway = self.gateway(approval_broker=broker)
        duplicate = gateway.execute(
            PROPOSE_CHANGES,
            {"changes": [
                {"kind": "create", "path": "x.txt", "content": "1"},
                {"kind": "modify", "path": "x.txt", "content": "2"},
            ]},
            self.context,
        )
        self.assertEqual(duplicate["code"], "DENIED_TOOL_INPUT_INVALID")
        with mock.patch("kaizen_components.orchestration.tool_gateway.PROPOSAL_MAX_SIDE_BYTES", 1):
            large = gateway.execute(
                PROPOSE_CHANGES,
                {"changes": [{"kind": "create", "path": "large.txt", "content": "xx"}]},
                self.context,
            )
        self.assertEqual(large["code"], "DENIED_TOOL_TOO_LARGE")
        broker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
