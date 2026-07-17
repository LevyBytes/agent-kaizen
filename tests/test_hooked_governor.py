"""Hooked governor + hook shim + hooks installer (v8 M-CLAUDE / M5b): each contract asserted hermetically.

NO real ``claude`` is invoked and NO network egress happens. The governor decision logic
(``orchestration.hooked``) is driven with an in-process :class:`policy.PolicyEngine` (no DB -- test_policy.py
owns the DB legs) and synthetic hook payloads shaped exactly as the recorded payload probe. The shim
(``kaizen_components/orchestration/claude_hook_shim.py``) is run as a REAL subprocess with NO daemon up, proving it
fails CLOSED itself when the provider hook executable is missing or errors. The installer is exercised through a real :class:`Supervisor` against a
throwaway git repo, proving idempotent validate-and-skip-warm + the git-tracked refusal.

Contract -> proving test:
- PreToolUse allow/deny/ask mapping ....... PreToolUseMappingTest
- strict fail-closed on unreachable ....... FailClosedModeTest
- observe pass-through .................... FailClosedModeTest
- UserPromptSubmit @-ref block ............ UserPromptSubmitGuardTest
- shim fail-closed (real subprocess) ...... ShimFailClosedTest
- lifecycle shim always exit-0/no-output .. ShimFailClosedTest
- installer idempotent + tracked-refuse ... HooksInstallerTest
- registry disjointness (both directions) . RegistryDisjointnessTest
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _harness import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT))

from kaizen_components.orchestration import hooked, policy  # noqa: E402

SHIM = str(REPO_ROOT / "kaizen_components" / "orchestration" / "claude_hook_shim.py")


# --- helpers ----------------------------------------------------------------------------------

def engine(protected=(), rules=()) -> policy.PolicyEngine:
    # vendor=[] so no real ~/.claude / ~/.codex path is resolved in a unit test.
    return policy.PolicyEngine(list(protected), list(rules), [])


def decide_via(eng: policy.PolicyEngine, epoch: int = 0):
    return lambda action: eng.decide(action, current_epoch=epoch)


def pretool_payload(tool_name: str, tool_input: dict, *, cwd: str = "C:/w", tid: str = "t1") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": "sess-1",
        "tool_use_id": tid,
        "permission_mode": "default",
        "transcript_path": "C:/w/.transcript.jsonl",
        "cwd": cwd,
    }


def prompt_payload(prompt: str, *, cwd: str = "C:/w") -> dict:
    return {
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "session_id": "sess-1",
        "permission_mode": "default",
        "transcript_path": "C:/w/.transcript.jsonl",
        "cwd": cwd,
    }


# The shipped path is shim -> daemon _hooks_decide -> output renderers. These in-process runners are
# test-only composition helpers for the pure action/guard primitives; they intentionally do not ship.
class _TestDaemonUnreachable(Exception):
    pass


def _test_unreachable(event: str, *, strict: bool, reason: str):
    return hooked.fail_closed_output(event, reason=reason) if strict else hooked.passthrough_output(event)


def _test_run_pretooluse(payload, *, mode, decide_fn, engine_name="claude", epoch=0):
    strict = mode == hooked.MODE_STRICT
    action = hooked.action_from_pretooluse(payload, engine=engine_name, epoch=epoch)
    try:
        decision = decide_fn(action)
    except _TestDaemonUnreachable:
        return _test_unreachable(hooked.PRE_TOOL_USE, strict=strict, reason="policy daemon unreachable")
    except Exception as error:  # noqa: BLE001 -- test composition mirrors the shim's fail policy
        return _test_unreachable(hooked.PRE_TOOL_USE, strict=strict, reason=type(error).__name__)
    return hooked.pretooluse_output(decision, strict=strict)


def _test_run_userpromptsubmit(payload, *, mode, engine=None, engine_fn=None):
    strict = mode == hooked.MODE_STRICT
    try:
        resolved = engine if engine is not None else (engine_fn() if engine_fn is not None else None)
    except _TestDaemonUnreachable:
        return _test_unreachable(hooked.USER_PROMPT_SUBMIT, strict=strict, reason="policy daemon unreachable")
    except Exception as error:  # noqa: BLE001 -- test composition mirrors the shim's fail policy
        return _test_unreachable(hooked.USER_PROMPT_SUBMIT, strict=strict, reason=type(error).__name__)
    if resolved is None:
        return _test_unreachable(hooked.USER_PROMPT_SUBMIT, strict=strict, reason="no policy engine")
    block, reason = hooked.guard_prompt(
        str(payload.get("prompt") or ""), engine=resolved, cwd=str(payload.get("cwd") or "") or None,
    )
    return hooked.userpromptsubmit_output(block=block, reason=reason, strict=strict)


# Preserve existing test call sites while keeping the production module free of test-only runners.
hooked.DaemonUnreachable = _TestDaemonUnreachable
hooked.run_pretooluse = _test_run_pretooluse
hooked.run_userpromptsubmit = _test_run_userpromptsubmit


# --- PreToolUse allow/deny/ask ----------------------------------------------------------------

class PreToolUseMappingTest(unittest.TestCase):
    def test_allow_maps_to_permission_allow_exit0(self) -> None:
        # A path_prefix allow over the cwd prefix => file_write under it allows.
        eng = engine(rules=[{
            "id": "r_allow", "rule_type": "allow", "verb": "file_write",
            "match_kind": "path_prefix", "pattern": "C:/w", "engine": None, "enabled": True,
        }])
        payload = pretool_payload("Write", {"file_path": "C:/w/foo.txt"})
        out = hooked.run_pretooluse(payload, mode=hooked.MODE_STRICT, decide_fn=decide_via(eng))
        self.assertEqual(out.decision, policy.ALLOW)
        self.assertEqual(out.exit_code, 0)
        body = json.loads(hooked.render(out))
        self.assertEqual(body["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(body["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_deny_invariant_git_push_exit2_in_strict(self) -> None:
        # A git push inside a Bash command hits INV_GIT_PUSH regardless of any DB rule.
        eng = engine()
        payload = pretool_payload("Bash", {"command": "git push origin main"})
        out = hooked.run_pretooluse(payload, mode=hooked.MODE_STRICT, decide_fn=decide_via(eng))
        self.assertEqual(out.decision, policy.DENY)
        self.assertEqual(out.exit_code, 2)  # strict: block precedes the permission tool
        body = json.loads(hooked.render(out))
        self.assertEqual(body["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("INV_GIT_PUSH", body["hookSpecificOutput"]["permissionDecisionReason"])

    def test_deny_in_observe_still_json_deny_but_exit0(self) -> None:
        # Observe is PASSIVE (H0): a live deny is COMPUTED (decision retained) but NOTHING is rendered
        # to Claude -- empty body, exit 0. The governor never emits a deny on the wire in observe mode.
        eng = engine()
        payload = pretool_payload("Bash", {"command": "git push origin main"})
        out = hooked.run_pretooluse(payload, mode=hooked.MODE_OBSERVE, decide_fn=decide_via(eng))
        self.assertEqual(out.decision, policy.DENY)  # truth preserved for callers/tests
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(hooked.render(out), "")  # emits nothing to Claude

    def test_deny_protected_path_write(self) -> None:
        eng = engine(protected=["C:/protected"])
        payload = pretool_payload("Write", {"file_path": "C:/protected/secret.txt"})
        out = hooked.run_pretooluse(payload, mode=hooked.MODE_STRICT, decide_fn=decide_via(eng))
        self.assertEqual(out.decision, policy.DENY)
        body = json.loads(hooked.render(out))
        self.assertIn("INV_PROTECTED_PATH", body["hookSpecificOutput"]["permissionDecisionReason"])

    def test_unmatched_defaults_to_ask(self) -> None:
        eng = engine()
        payload = pretool_payload("Read", {"file_path": "C:/w/bar.txt"})
        out = hooked.run_pretooluse(payload, mode=hooked.MODE_STRICT, decide_fn=decide_via(eng))
        self.assertEqual(out.decision, policy.ASK)
        self.assertEqual(out.exit_code, 0)
        body = json.loads(hooked.render(out))
        self.assertEqual(body["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_bash_exact_command_allow(self) -> None:
        eng = engine(rules=[{
            "id": "r_ls", "rule_type": "allow", "verb": "exec",
            "match_kind": "exact_command", "pattern": "ls -la", "engine": None, "enabled": True,
        }])
        payload = pretool_payload("Bash", {"command": "ls -la"})
        out = hooked.run_pretooluse(payload, mode=hooked.MODE_STRICT, decide_fn=decide_via(eng))
        self.assertEqual(out.decision, policy.ALLOW)

    def test_action_builder_maps_tools_to_verbs(self) -> None:
        # The verb mapping is what lets the invariants fire; assert it directly.
        a_write = hooked.action_from_pretooluse(pretool_payload("Write", {"file_path": "C:/w/a"}), engine="claude", epoch=0)
        self.assertEqual(a_write.verb, "file_write")
        self.assertEqual(a_write.targets, ("C:/w/a",))
        a_bash = hooked.action_from_pretooluse(pretool_payload("Bash", {"command": "echo hi"}), engine="claude", epoch=0)
        self.assertEqual(a_bash.verb, "exec")
        self.assertEqual(a_bash.command, "echo hi")
        a_read = hooked.action_from_pretooluse(pretool_payload("Read", {"file_path": "C:/w/a"}), engine="claude", epoch=0)
        self.assertEqual(a_read.verb, "file_read")
        a_task = hooked.action_from_pretooluse(pretool_payload("Task", {"prompt": "do x"}), engine="claude", epoch=0)
        self.assertEqual(a_task.verb, "spawn")


# --- strict fail-closed / observe pass-through ------------------------------------------------

class FailClosedModeTest(unittest.TestCase):
    def test_strict_fail_closed_on_daemon_unreachable(self) -> None:
        def unreachable(_action):
            raise hooked.DaemonUnreachable("down")

        payload = pretool_payload("Write", {"file_path": "C:/w/x.txt"})
        out = hooked.run_pretooluse(payload, mode=hooked.MODE_STRICT, decide_fn=unreachable)
        self.assertEqual(out.decision, policy.DENY)
        self.assertEqual(out.exit_code, 2)
        body = json.loads(hooked.render(out))
        self.assertEqual(body["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_strict_fail_closed_on_arbitrary_error(self) -> None:
        # A governor bug (ANY exception) must not fail OPEN in strict mode.
        def boom(_action):
            raise RuntimeError("bug")

        payload = pretool_payload("Write", {"file_path": "C:/w/x.txt"})
        out = hooked.run_pretooluse(payload, mode=hooked.MODE_STRICT, decide_fn=boom)
        self.assertEqual(out.exit_code, 2)
        self.assertEqual(out.decision, policy.DENY)

    def test_observe_pass_through_on_daemon_unreachable(self) -> None:
        def unreachable(_action):
            raise hooked.DaemonUnreachable("down")

        payload = pretool_payload("Write", {"file_path": "C:/w/x.txt"})
        out = hooked.run_pretooluse(payload, mode=hooked.MODE_OBSERVE, decide_fn=unreachable)
        self.assertEqual(out.decision, "passthrough")
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(hooked.render(out), "")  # empty body: governor abstains, never weakens Claude


class ObservePassiveTest(unittest.TestCase):
    """H0 exit criterion: hooked-observe NEVER emits allow/ask/deny/block JSON to Claude. Every LIVE
    decision is computed (decision label retained on the outcome) but rendered as an EMPTY body + exit 0.
    On the wire an observe deny, ask, allow, and block are indistinguishable (all empty)."""

    def test_live_deny_pretooluse_renders_empty(self) -> None:
        eng = engine()  # INV_GIT_PUSH deny
        payload = pretool_payload("Bash", {"command": "git push origin main"})
        out = hooked.run_pretooluse(payload, mode=hooked.MODE_OBSERVE, decide_fn=decide_via(eng))
        self.assertEqual(out.decision, policy.DENY)
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(hooked.render(out), "")

    def test_live_ask_pretooluse_renders_empty(self) -> None:
        eng = engine()  # unmatched Read defaults to ask
        payload = pretool_payload("Read", {"file_path": "C:/w/bar.txt"})
        out = hooked.run_pretooluse(payload, mode=hooked.MODE_OBSERVE, decide_fn=decide_via(eng))
        self.assertEqual(out.decision, policy.ASK)
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(hooked.render(out), "")

    def test_live_allow_pretooluse_renders_empty(self) -> None:
        eng = engine(rules=[{
            "id": "r_allow", "rule_type": "allow", "verb": "file_write",
            "match_kind": "path_prefix", "pattern": "C:/w", "engine": None, "enabled": True,
        }])
        payload = pretool_payload("Write", {"file_path": "C:/w/foo.txt"})
        out = hooked.run_pretooluse(payload, mode=hooked.MODE_OBSERVE, decide_fn=decide_via(eng))
        self.assertEqual(out.decision, policy.ALLOW)
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(hooked.render(out), "")

    def test_live_block_userpromptsubmit_renders_empty(self) -> None:
        eng = engine(protected=["C:/secret"])
        out = hooked.run_userpromptsubmit(
            prompt_payload("read @C:/secret/keys.txt now"), mode=hooked.MODE_OBSERVE, engine=eng
        )
        self.assertEqual(out.decision, "block")  # guard truth retained
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(hooked.render(out), "")  # but nothing emitted to Claude


# --- UserPromptSubmit @-ref guard -------------------------------------------------------------

class UserPromptSubmitGuardTest(unittest.TestCase):
    def test_at_ref_to_protected_path_blocks(self) -> None:
        eng = engine(protected=["C:/secret"])
        out = hooked.run_userpromptsubmit(prompt_payload("read @C:/secret/keys.txt now"), mode=hooked.MODE_STRICT, engine=eng)
        self.assertEqual(out.decision, "block")
        self.assertEqual(out.exit_code, 2)
        body = json.loads(hooked.render(out))
        self.assertEqual(body["decision"], "block")
        self.assertIn("keys.txt", body["reason"])

    def test_at_ref_to_vendor_config_blocks(self) -> None:
        # A ~/.claude/settings.json vendor-config @-ref is blocked too.
        eng = policy.PolicyEngine([], [], [os.path.expanduser("~/.claude/settings.json")])
        ref = os.path.expanduser("~/.claude/settings.json").replace("\\", "/")
        out = hooked.run_userpromptsubmit(prompt_payload(f"show me @{ref}"), mode=hooked.MODE_STRICT, engine=eng)
        self.assertEqual(out.decision, "block")

    def test_prompt_without_protected_ref_passes(self) -> None:
        eng = engine(protected=["C:/secret"])
        out = hooked.run_userpromptsubmit(prompt_payload("hello, no refs here"), mode=hooked.MODE_STRICT, engine=eng)
        self.assertEqual(out.decision, "allow")
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(hooked.render(out), "")

    def test_non_protected_at_ref_passes(self) -> None:
        # A bare @-ref to a NON-protected path is allowed (over-blocking every @ would break normal use).
        eng = engine(protected=["C:/secret"])
        out = hooked.run_userpromptsubmit(prompt_payload("read @C:/w/notes.md"), mode=hooked.MODE_STRICT, engine=eng)
        self.assertEqual(out.decision, "allow")

    def test_strict_fail_closed_when_no_engine(self) -> None:
        # A UserPromptSubmit with no obtainable engine fails closed in strict.
        out = hooked.run_userpromptsubmit(prompt_payload("anything"), mode=hooked.MODE_STRICT, engine=None)
        self.assertEqual(out.decision, "block")
        self.assertEqual(out.exit_code, 2)

    def test_observe_pass_through_when_no_engine(self) -> None:
        out = hooked.run_userpromptsubmit(prompt_payload("anything"), mode=hooked.MODE_OBSERVE, engine=None)
        self.assertEqual(out.decision, "passthrough")
        self.assertEqual(out.exit_code, 0)


# --- shim fail-closed (real subprocess, no daemon) --------------------------------------------

class ShimFailClosedTest(unittest.TestCase):
    """A provider hook that errors fails OPEN, so the shim must fail-closed ITSELF. Run the shim as a real
    subprocess with NO daemon up + an isolated repo root (so send_control returns a clean not-running
    payload) and assert strict => exit 2 + deny JSON, observe => exit 0 + empty stdout."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-shim-"))
        self.addCleanup(_rmtree, self.root)
        self.env = dict(os.environ)
        self.env["KAIZEN_REPO_ROOT"] = str(self.root)

    def _run_shim(self, mode: str, event: str, payload: dict):
        proc = subprocess.run(
            [sys.executable, SHIM, "--mode", mode, "--event", event],
            input=json.dumps(payload), capture_output=True, text=True, env=self.env,
            cwd=str(REPO_ROOT), timeout=90,
        )
        return proc

    def test_strict_pretooluse_fails_closed(self) -> None:
        proc = self._run_shim("hooked-strict", "PreToolUse", pretool_payload("Write", {"file_path": "C:/w/x.txt"}))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        body = json.loads(proc.stdout.strip())  # stdout carries ONLY the hook JSON line
        self.assertEqual(body["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_strict_userpromptsubmit_fails_closed(self) -> None:
        proc = self._run_shim("hooked-strict", "UserPromptSubmit", prompt_payload("anything"))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        body = json.loads(proc.stdout.strip())
        self.assertEqual(body["decision"], "block")

    def test_observe_passes_through(self) -> None:
        proc = self._run_shim("hooked-observe", "PreToolUse", pretool_payload("Write", {"file_path": "C:/w/x.txt"}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")  # empty body: governor abstains

    def test_stdout_is_pristine_json_only(self) -> None:
        # Diagnostics go to stderr; stdout must be exactly one JSON object (or empty), never log noise.
        proc = self._run_shim("hooked-strict", "PreToolUse", pretool_payload("Write", {"file_path": "C:/w/x.txt"}))
        json.loads(proc.stdout.strip())  # raises if stdout carries anything but the JSON line

    def test_record_only_lifecycle_hooks_always_exit_zero_without_output(self) -> None:
        for event in ("SessionStart", "Stop", "StopFailure", "SessionEnd"):
            with self.subTest(event=event):
                proc = self._run_shim(
                    "hooked-strict", event,
                    {"hook_event_name": event, "session_id": "sess-lifecycle"},
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, "")


# --- relocation safety (in-process, no Git or daemon) -----------------------------------------

class HooksRelocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-hook-relocation-"))
        self.addCleanup(_rmtree, self.root)
        from kaizen_components.orchestration.supervisor import Supervisor

        self.sup = Supervisor(repo_root=self.root)
        self.sup._is_git_tracked = lambda _path: False

    def test_verify_rejects_stale_command_and_install_preserves_mode(self) -> None:
        path = self.root / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        old_shim = self.root / "support_scripts" / "claude_hook_shim.py"
        current_shim = self.root / "kaizen_components" / "orchestration" / "claude_hook_shim.py"
        hooks = {"PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo user"}]}]}
        for event, block in self.sup._desired_hooks_block("hooked-observe").items():
            stale = json.loads(json.dumps(block))
            stale[0]["hooks"][0]["command"] = stale[0]["hooks"][0]["command"].replace(
                str(current_shim), str(old_shim)
            )
            hooks[event] = stale
        path.write_text(json.dumps({"opaque": {"preserve": True}, "hooks": hooks}), encoding="utf-8")

        before = self.sup._handle_hooks("hooks/verify", {})
        self.assertFalse(before["installed"])
        self.assertTrue(before["stale"])
        self.assertEqual(before["mode"], "hooked-observe")
        self.assertTrue(all(before["markers"].values()))
        self.assertFalse(any(before["events"].values()))

        replaced = self.sup._handle_hooks("hooks/install", {"mode": before["mode"]})
        self.assertEqual(replaced["status"], "OK")
        self.assertFalse(replaced["skipped"])
        after = self.sup._handle_hooks("hooks/verify", {})
        self.assertTrue(after["installed"])
        self.assertFalse(after["stale"])
        self.assertEqual(after["mode"], "hooked-observe")
        written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written["opaque"], {"preserve": True})
        self.assertIn("PostToolUse", written["hooks"])
        self.assertNotIn(str(old_shim), path.read_text(encoding="utf-8"))

    def test_atomic_install_failure_preserves_original_settings(self) -> None:
        path = self.root / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"opaque": "preserve"}), encoding="utf-8")
        original = path.read_bytes()

        with mock.patch(
            "kaizen_components.orchestration.supervisor.os.replace",
            side_effect=OSError("fixture replace failure"),
        ):
            result = self.sup._handle_hooks("hooks/install", {"mode": "hooked-strict"})

        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["code"], "ERROR_HOOKS_OP")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])


# --- installer (real Supervisor against a throwaway git repo) ---------------------------------

class HooksInstallerTest(unittest.TestCase):
    """Idempotent validate-and-skip-warm + the git-tracked refusal. Drives the real Supervisor's
    hooks/* handlers directly (in-process; no backgrounded daemon)."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-hooks-"))
        self.addCleanup(_rmtree, self.root)
        # A real git repo so the ls-files tracking check has something to answer.
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        env = dict(os.environ)
        env["KAIZEN_REPO_ROOT"] = str(self.root)
        # Import inside the test env so paths resolve; construct the Supervisor bound to this root.
        from kaizen_components.orchestration.supervisor import Supervisor  # noqa: E402

        self.sup = Supervisor(repo_root=self.root)
        # Installer/decision unit tests run in-process after db.py is already imported for the real repo.
        # Suppress enrichment here so these unit checks cannot create project C1/T5 rows; the isolated
        # subprocess recording suites below own all real-ledger assertions.
        self.sup._record_hook_decision = lambda _payload, _response: None

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(self.root), capture_output=True, text=True, timeout=30)

    def test_install_verify_remove_verify_cycle(self) -> None:
        r1 = self.sup._handle_hooks("hooks/install", {"mode": "hooked-strict"})
        self.assertEqual(r1["status"], "OK")
        self.assertTrue(r1["installed"])
        self.assertFalse(r1["skipped"])  # cold install does work
        self.assertTrue(Path(r1["path"]).is_file())

        v1 = self.sup._handle_hooks("hooks/verify", {})
        self.assertTrue(v1["installed"])
        self.assertEqual(
            set(v1["events"]),
            {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop", "StopFailure", "SessionEnd"},
        )
        self.assertTrue(all(v1["events"].values()))

        rm = self.sup._handle_hooks("hooks/remove", {})
        self.assertTrue(rm["removed"])

        v2 = self.sup._handle_hooks("hooks/verify", {})
        self.assertFalse(v2["installed"])

    def test_install_twice_is_skip_warm(self) -> None:
        first = self.sup._handle_hooks("hooks/install", {"mode": "hooked-strict"})
        self.assertFalse(first["skipped"])
        second = self.sup._handle_hooks("hooks/install", {"mode": "hooked-strict"})
        self.assertTrue(second["installed"])
        self.assertTrue(second["skipped"])  # warm re-run does NO write (validate-and-skip-warm)

    def test_install_preserves_user_hooks(self) -> None:
        # A pre-existing user hook for a DIFFERENT event must survive the install/remove cycle.
        path = self.root / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "echo user"}]}]}}), encoding="utf-8")
        self.sup._handle_hooks("hooks/install", {"mode": "hooked-strict"})
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("PostToolUse", after["hooks"])  # user's event preserved
        self.assertIn("PreToolUse", after["hooks"])    # ours added
        self.sup._handle_hooks("hooks/remove", {})
        after_rm = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("PostToolUse", after_rm["hooks"])  # user's event still there after remove
        self.assertNotIn("PreToolUse", after_rm.get("hooks", {}))  # ours gone

    def test_install_preserves_user_hook_on_same_event(self) -> None:
        path = self.root / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        user_entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user"}]}
        path.write_text(json.dumps({"hooks": {"PreToolUse": [user_entry]}}), encoding="utf-8")

        self.sup._handle_hooks("hooks/install", {"mode": "hooked-strict"})
        installed = json.loads(path.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        self.assertIn(user_entry, installed)
        self.assertEqual(sum(entry.get("_kaizen") == "kaizen-governor" for entry in installed), 1)

        self.sup._handle_hooks("hooks/remove", {})
        removed = json.loads(path.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        self.assertEqual(removed, [user_entry])

    def test_install_refuses_git_tracked_settings(self) -> None:
        # Write then FORCE-track the settings file (bypass any inherited gitignore) to simulate a
        # committed settings.local.json; the installer must REFUSE rather than write a tracked file.
        self.sup._handle_hooks("hooks/install", {"mode": "hooked-strict"})
        self._git("add", "-f", ".claude/settings.local.json")
        self._git("commit", "-q", "-m", "commit settings")
        refused = self.sup._handle_hooks("hooks/install", {"mode": "hooked-strict"})
        self.assertEqual(refused["status"], "DENIED")
        self.assertEqual(refused["code"], "DENIED_HOOKS_SETTINGS_TRACKED")
        # remove also refuses to edit a tracked file.
        rm_refused = self.sup._handle_hooks("hooks/remove", {})
        self.assertEqual(rm_refused["code"], "DENIED_HOOKS_SETTINGS_TRACKED")

    def test_decide_op_denies_git_push_via_policy(self) -> None:
        # The hooks/decide loopback op routes a PreToolUse payload through the loaded policy engine.
        self.sup.policy = engine()
        resp = self.sup._handle_hooks("hooks/decide", {
            "hook_event_name": "PreToolUse",
            "payload": pretool_payload("Bash", {"command": "git push origin main"}),
        })
        self.assertEqual(resp["result"], "deny")
        self.assertEqual(resp["invariant_id"], "INV_GIT_PUSH")

    def test_decide_op_no_policy_denies(self) -> None:
        self.sup.policy = None
        resp = self.sup._handle_hooks("hooks/decide", {
            "hook_event_name": "PreToolUse",
            "payload": pretool_payload("Write", {"file_path": "C:/w/x.txt"}),
        })
        self.assertEqual(resp["status"], "DENIED")
        self.assertEqual(resp["code"], "DENIED_POLICY_NOT_LOADED")

    def test_decide_userpromptsubmit_block(self) -> None:
        self.sup.policy = engine(protected=["C:/secret"])
        resp = self.sup._handle_hooks("hooks/decide", {
            "hook_event_name": "UserPromptSubmit",
            "payload": prompt_payload("read @C:/secret/x.txt"),
        })
        self.assertTrue(resp["block"])


# --- registry disjointness (both directions) --------------------------------------------------

class RegistryDisjointnessTest(unittest.TestCase):
    """The M5b registry delta: the newly-registered Claude/OTEL kinds must not collide with the fleet
    COORD matrix, and the still-reserved names must remain disjoint too (test_fleet_core.py:379 keeps the
    reserved<->coord half; this adds the live<->coord reverse the declaration mandates)."""

    def test_new_live_kinds_registered(self) -> None:
        from kaizen_components.schemas.registry import AGENT_EVENT_KIND_MARKERS, KAIZEN_ENUMS

        for kind in ("hook_event", "permission_mode_changed", "compaction"):
            self.assertIn(kind, KAIZEN_ENUMS["agent_event_kind"])
            self.assertEqual(AGENT_EVENT_KIND_MARKERS[kind], ["point"])

    def test_new_live_kinds_disjoint_from_coord_markers(self) -> None:
        from kaizen_components.schemas.registry import AGENT_EVENT_KIND_MARKERS, COORD_EVENT_KIND_MARKERS

        new_live = {"hook_event", "permission_mode_changed", "compaction"}
        self.assertEqual(new_live & set(COORD_EVENT_KIND_MARKERS), set())
        # And the WHOLE agent-event kind space stays disjoint from the coord kind space (no name fights
        # between the T6 agent-run ledger and the M9 fleet coord ledger).
        self.assertEqual(set(AGENT_EVENT_KIND_MARKERS) & set(COORD_EVENT_KIND_MARKERS), set())

    def test_reserved_still_disjoint_from_coord(self) -> None:
        from kaizen_components.schemas.registry import COORD_EVENT_KIND_MARKERS, RESERVED_CLAUDE_EVENT_KINDS

        # The fleet_core:379 invariant restated here so this suite guards it independently.
        self.assertEqual(set(RESERVED_CLAUDE_EVENT_KINDS) & set(COORD_EVENT_KIND_MARKERS), set())

    def test_subagent_names_left_reserved(self) -> None:
        # The declaration's cleaner option: map onto the EXISTING 'subagent' kind, register nothing new.
        from kaizen_components.schemas.registry import KAIZEN_ENUMS, RESERVED_CLAUDE_EVENT_KINDS

        self.assertIn("subagent_start", RESERVED_CLAUDE_EVENT_KINDS)
        self.assertIn("subagent_stop", RESERVED_CLAUDE_EVENT_KINDS)
        self.assertNotIn("subagent_start", KAIZEN_ENUMS["agent_event_kind"])
        self.assertIn("subagent", KAIZEN_ENUMS["agent_event_kind"])  # the existing span they ride


# --- _hooks_decide ledger recording (real DB via isolated subprocess) -------------------------
# The Supervisor writes the REAL ledger through the frozen-at-import db.DB_PATH, so these tests drive it
# in a SUBPROCESS with KAIZEN_REPO_ROOT pinned to a throwaway root (the established test_supervisor.py
# idiom) -- the project's AI/db is never touched. The driver constructs a bare Supervisor (no boot: no
# single-instance claim / loopback), injects an in-process PolicyEngine, runs the caller's BODY (which
# calls _hooks_decide and sets `out`), then appends ledger counts so every test asserts on them.

_RECORDER_PREAMBLE = (
    "import json\n"
    "from pathlib import Path\n"
    "from kaizen_components import db\n"
    "from kaizen_components.orchestration import policy\n"
    "from kaizen_components.orchestration.supervisor import Supervisor\n"
    "def make_engine(protected=(), rules=()):\n"
    "    return policy.PolicyEngine(list(protected), list(rules), [])\n"
    "sup = Supervisor(repo_root=Path(ROOT))\n"
    "out = {}\n"
    "exec(BODY)\n"
    "runs = db.fetch_all(\"SELECT id, agent_type, surface FROM agent_runs\")\n"
    "hook_pts = db.fetch_all(\n"
    "    \"SELECT agent_run_id, code, correlation_id FROM agent_events \"\n"
    "    \"WHERE event_kind = 'hook_event' AND marker = 'point'\")\n"
    "out['runs'] = [{'id': r[0], 'agent_type': r[1], 'surface': r[2]} for r in runs]\n"
    "out['hook_points'] = [{'run': r[0], 'code': r[1], 'corr': r[2]} for r in hook_pts]\n"
    "print('RESULT ' + json.dumps(out))\n"
)


def _run_recorder(root: Path, body: str) -> dict:
    """Run a recorder driver in a subprocess pinned to ``root``; return the parsed RESULT payload."""
    script = "ROOT = " + repr(str(root)) + "\nBODY = " + repr(body) + "\n" + _RECORDER_PREAMBLE
    env = dict(os.environ)
    env["KAIZEN_REPO_ROOT"] = str(root)
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env,
        cwd=str(REPO_ROOT), timeout=120,
    )
    line = next((ln for ln in (proc.stdout or "").splitlines() if ln.startswith("RESULT ")), None)
    assert line is not None, f"no RESULT; stdout={proc.stdout!r} stderr={proc.stderr[-800:]!r}"
    return json.loads(line[len("RESULT "):])


class HookDecisionRecordingTest(unittest.TestCase):
    """_hooks_decide best-effort ledger recording: one synthetic claude/cli T5 run per foreign session,
    one hook_event 'point' per decide, run reuse across calls, and a hard guarantee that a ledger failure
    leaves the returned decision byte-identical."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-hookrec-"))
        self.addCleanup(_rmtree, self.root)
        from _harness import kaizen as _kaizen

        rc, payload = _kaizen(self.root, "K1")
        self.assertEqual(rc, 0, payload)

    def test_decide_lands_one_hook_point_on_synthetic_claude_cli_run(self) -> None:
        body = (
            "sup.policy = make_engine()\n"
            "resp = sup._hooks_decide({'hook_event_name': 'PreToolUse', 'payload': "
            "{'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', "
            "'tool_input': {'command': 'git push origin main'}, 'session_id': 'sess-A'}})\n"
            "out['resp'] = resp\n"
        )
        res = _run_recorder(self.root, body)
        self.assertEqual(res["resp"]["result"], "deny")  # decision itself unaffected by recording
        self.assertEqual(len(res["runs"]), 1)  # exactly one synthetic run opened
        self.assertEqual(res["runs"][0]["agent_type"], "claude")
        self.assertEqual(res["runs"][0]["surface"], "cli")
        self.assertEqual(len(res["hook_points"]), 1)  # exactly one hook_event point
        self.assertEqual(res["hook_points"][0]["code"], "deny")
        self.assertEqual(res["hook_points"][0]["corr"], "sess-A")
        self.assertEqual(res["hook_points"][0]["run"], res["runs"][0]["id"])

    def test_duplicate_delivery_reuses_run_and_deduplicates_hook_point(self) -> None:
        # Same delivery twice => one observed T5 and one source-id-deduplicated hook point.
        body = (
            "sup.policy = make_engine()\n"
            "p = {'hook_event_name': 'PreToolUse', 'tool_name': 'Read', "
            "'tool_input': {'file_path': 'C:/w/a.txt'}, 'session_id': 'sess-B'}\n"
            "sup._hooks_decide({'hook_event_name': 'PreToolUse', 'payload': dict(p)})\n"
            "sup._hooks_decide({'hook_event_name': 'PreToolUse', 'payload': dict(p)})\n"
        )
        res = _run_recorder(self.root, body)
        self.assertEqual(len(res["runs"]), 1)  # reused, not a second run
        self.assertEqual(len(res["hook_points"]), 1)
        self.assertEqual({pt["run"] for pt in res["hook_points"]}, {res["runs"][0]["id"]})

    def test_userpromptsubmit_block_records_point(self) -> None:
        body = (
            "sup.policy = make_engine(protected=['C:/secret'])\n"
            "resp = sup._hooks_decide({'hook_event_name': 'UserPromptSubmit', 'payload': "
            "{'hook_event_name': 'UserPromptSubmit', 'prompt': 'read @C:/secret/x.txt', "
            "'session_id': 'sess-C'}})\n"
            "out['resp'] = resp\n"
        )
        res = _run_recorder(self.root, body)
        self.assertTrue(res["resp"]["block"])
        self.assertEqual(len(res["runs"]), 1)
        self.assertEqual(len(res["hook_points"]), 1)
        self.assertEqual(res["hook_points"][0]["code"], "block")

    def test_ledger_failure_leaves_decision_unchanged(self) -> None:
        # Monkeypatch funnel_event to RAISE: the recording block must swallow it and return the SAME
        # decision (byte-identical), writing zero hook points.
        body = (
            "sup.policy = make_engine()\n"
            "def boom(*a, **k):\n"
            "    raise RuntimeError('ledger down')\n"
            "sup.funnel_event = boom\n"
            "resp = sup._hooks_decide({'hook_event_name': 'PreToolUse', 'payload': "
            "{'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', "
            "'tool_input': {'command': 'git push origin main'}, 'session_id': 'sess-D'}})\n"
            "out['resp'] = resp\n"
        )
        res = _run_recorder(self.root, body)
        # Decision fully intact despite the ledger raise.
        self.assertEqual(res["resp"]["status"], "OK")
        self.assertEqual(res["resp"]["result"], "deny")
        self.assertEqual(res["resp"]["invariant_id"], "INV_GIT_PUSH")
        self.assertEqual(len(res["hook_points"]), 0)  # funnel raised => no point landed

    def test_no_session_id_records_nothing(self) -> None:
        body = (
            "sup.policy = make_engine()\n"
            "resp = sup._hooks_decide({'hook_event_name': 'PreToolUse', 'payload': "
            "{'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', "
            "'tool_input': {'command': 'git push origin main'}}})\n"  # no session_id
            "out['resp'] = resp\n"
        )
        res = _run_recorder(self.root, body)
        self.assertEqual(res["resp"]["result"], "deny")  # decision still returned
        self.assertEqual(len(res["runs"]), 0)  # nothing to attribute => no run
        self.assertEqual(len(res["hook_points"]), 0)


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
