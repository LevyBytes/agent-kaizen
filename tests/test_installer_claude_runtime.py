"""Windows behavior tests for the explicit Claude provider-runtime setup selection."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


INSTALLER = Path(__file__).resolve().parents[1] / "setup" / "Install-Agent-Kaizen.cmd"
MARKER = "__AK_PS_PAYLOAD__"
POWERSHELL = shutil.which("powershell")
TEST_TEMP_ROOT = Path(__file__).resolve().parents[1] / "AI" / "work" / "test-extension-offline-tests" / "windows-installer"


def _payload() -> str:
    text = INSTALLER.read_text(encoding="utf-8")
    index = text.rfind(MARKER)
    if index < 0:
        raise AssertionError("installer payload marker not found")
    return text[index + len(MARKER):]


@unittest.skipUnless(platform.system() == "Windows" and POWERSHELL, "Windows PowerShell required")
class ClaudeRuntimeSelectorTest(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.root = Path(self.temp_dir.name)
        self.payload = self.root / "installer.ps1"
        self.payload.write_text(_payload(), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run(self, args: list[str], env_value: str | None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        if env_value is None:
            env.pop("AK_WITH_CLAUDE_RUNTIME", None)
        else:
            env["AK_WITH_CLAUDE_RUNTIME"] = env_value
        env["DEVROOT"] = str(self.root / "environment-devroot")
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.payload), *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    def test_environment_true_is_visible_in_action_free_plan(self) -> None:
        plan = self.root / "plan.json"
        devroot = self.root / "requested devroot"
        result = self._run(
            [str(devroot), "-PlanOnly", "-NoDevTools", "-NoNetwork", "-NoExternalActions", "-NoUserEnvWrites", "-NoPause", "-EmitPlanJson", str(plan)],
            "enabled",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        emitted = json.loads(plan.read_text(encoding="utf-8"))
        self.assertEqual(Path(emitted["devRoot"]), devroot)
        self.assertTrue(emitted["selection"]["claudeRuntime"])
        self.assertIn("Plan-only complete; no install actions were run.", result.stdout)

    def test_explicit_cli_selection_overrides_invalid_environment(self) -> None:
        plan = self.root / "plan.json"
        result = self._run(
            [str(self.root / "devroot"), "-PlanOnly", "-NoNetwork", "-NoExternalActions", "-NoUserEnvWrites", "-NoPause", "-WithClaudeRuntime", "-EmitPlanJson", str(plan)],
            "invalid",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(plan.read_text(encoding="utf-8"))["selection"]["claudeRuntime"])

    def test_invalid_environment_is_rejected_early(self) -> None:
        result = self._run([str(self.root / "devroot"), "-PlanOnly", "-NoPause"], "sometimes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AK_WITH_CLAUDE_RUNTIME must be one of", result.stdout + result.stderr)

    def test_selected_self_test_reports_no_install_actions(self) -> None:
        result = self._run(
            [str(self.root / "devroot"), "-SelfTest", "-NoNetwork", "-NoExternalActions", "-NoUserEnvWrites", "-NoPause"],
            "true",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Self-test complete; step shape validated without install actions.", result.stdout)


@unittest.skipUnless(platform.system() == "Windows" and POWERSHELL, "Windows PowerShell required")
class ClaudeRuntimeStepTest(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.logs = self.root / "logs"
        self.state = self.root / "ready"
        self.calls = self.root / "calls.txt"
        (self.repo / "setup").mkdir(parents=True)
        self.logs.mkdir()
        helper = self.repo / "setup" / "claude_runtime_setup.py"
        helper.write_text(
            "from pathlib import Path\n"
            "import os, sys\n"
            "state = Path(os.environ['AK_TEST_STATE'])\n"
            "calls = Path(os.environ['AK_TEST_CALLS'])\n"
            "action = sys.argv[1]\n"
            "with calls.open('a', encoding='utf-8') as stream: stream.write(action + '\\n')\n"
            "if action == 'check': raise SystemExit(0 if state.is_file() else 2)\n"
            "if action == 'install': state.write_text('ready', encoding='utf-8'); raise SystemExit(0)\n"
            "raise SystemExit(3)\n",
            encoding="utf-8",
        )
        payload = _payload()
        main_index = payload.find("\n$akExitCode = 0")
        if main_index < 0:
            raise AssertionError("installer main marker not found")
        harness = r'''
$script:ClaudeRuntimeSelected = $true
$script:RepoPath = $env:AK_TEST_REPO
$script:VenvPython = $env:AK_TEST_PYTHON
$script:LogRoot = $env:AK_TEST_LOG_ROOT
$script:CurrentStep = 1
try {
    Ensure-AkClaudeRuntime
    Write-Output '__AK_RESULT__:OK'
    exit 0
} catch {
    Write-Output ('__AK_RESULT__:ERROR:' + $_.Exception.Message)
    exit 7
}
'''
        self.harness = self.root / "runtime-step.ps1"
        self.harness.write_text(payload[:main_index] + harness, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run(self, *, no_network: bool) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("AK_WITH_CLAUDE_RUNTIME", None)
        env.update(
            {
                "AK_TEST_REPO": str(self.repo),
                "AK_TEST_PYTHON": sys.executable,
                "AK_TEST_LOG_ROOT": str(self.logs),
                "AK_TEST_STATE": str(self.state),
                "AK_TEST_CALLS": str(self.calls),
            }
        )
        args = [
            POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(self.harness), "-NoPrompt",
        ]
        if no_network:
            args.append("-NoNetwork")
        return subprocess.run(args, capture_output=True, text=True, timeout=30, env=env)

    def _calls(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    def test_warm_no_network_run_checks_and_returns(self) -> None:
        self.state.write_text("ready", encoding="utf-8")
        result = self._run(no_network=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._calls(), ["check"])

    def test_cold_no_network_run_denies_before_install(self) -> None:
        result = self._run(no_network=True)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual(self._calls(), ["check"])
        self.assertIn("-NoNetwork blocks its installation", result.stdout)

    def test_cold_network_allowed_run_installs_then_rechecks(self) -> None:
        result = self._run(no_network=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._calls(), ["check", "install", "check"])


if __name__ == "__main__":
    unittest.main()
