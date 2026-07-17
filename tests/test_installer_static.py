"""Static guards on the Windows installer's launcher, winget bootstrap, and Python install.

These lock in the Round-6 fixes proven against Windows Sandbox logs so they cannot silently
regress:
  * The cmd wrapper is a thin launcher that self-elevates into an ELEVATED PowerShell window
    (`-Verb RunAs`) rather than running the steps under cmd.
  * winget's App Installer bundle depends on the Windows App Runtime 1.8 framework, so the
    bootstrap installs `windowsappruntimeinstall` before RegisterByFamilyName / the bundle
    (a fresh sandbox otherwise fails with 0x80073CF9 / 0x80073CF3).
  * The python.org WiX-burn bootstrapper is run through the job-based `Invoke-AkInstaller`
    (Start-Job + call operator) so it returns a real exit code instead of a 1-second no-op,
    and stale banned tokens (the non-existent VCLibs framework URL, the impossible UI.Xaml
    min-version, the nuget/deps-zip detours, winget-Python) stay gone.
A Windows-only case AST-parses the embedded PowerShell payload so a syntax error in these edits
fails the suite rather than the installer.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "setup" / "Install-Agent-Kaizen.cmd"
MARKER = "__AK_PS_PAYLOAD__"


def _installer_text() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def _payload(text: str) -> str:
    # The installer extracts the payload at the LAST marker occurrence (the first is a literal
    # inside the extraction command); mirror that here.
    idx = text.rfind(MARKER)
    assert idx >= 0, "payload marker not found"
    return text[idx + len(MARKER):]


class InstallerStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _installer_text()

    def test_bogus_and_stale_tokens_absent(self):
        for token in (
            "aka.ms/Microsoft.VCLibs.$arch.14.00.appx",  # the non-existent non-Desktop framework URL
            "8.2501.31001.0",            # impossible UI.Xaml min-version assertion (removed)
            "Stop-NoWinget",             # replaced by the non-throwing Write-WingetUnavailable
            "Python.Python.3.12",        # Python is python.org-into-DEVROOT, not winget
            "v3-flatcontainer",          # nuget-CPython detour removed
            "DesktopAppInstaller_Dependencies",  # deps-zip winget bootstrap removed
        ):
            self.assertNotIn(token, self.text, f"stale token should be gone: {token}")

    def test_fix_tokens_present(self):
        for token in (
            "Test-AkDownloadedFile",
            "RegisterByFamilyName",           # registration-first winget bootstrap (SC/MS doc)
            "Ensure-WindowsAppRuntime18",     # install the WindowsAppRuntime 1.8 framework first
            "windowsappruntimeinstall",       # the arch-aware WindowsAppRuntime 1.8 redist URL
            "Verb RunAs",                     # cmd wrapper self-elevates into a PowerShell window
            ":RunElevated",                   # elevated-PowerShell relaunch path in the cmd wrapper
            "Read-AkDevRootMenu",             # DEVROOT chooser lives in the payload, not cmd
            "Invoke-AkInstaller",             # job-based runner so burn bootstrappers do not no-op
            "Start-Job",
            "python-install-",                # mirrored /log diagnostic for the Python MSI
            "Install-GitDirect",
            "Install-PythonDirect",
            "TargetDir=",                     # python.org into an explicit DEVROOT target (SC)
            "/VERYSILENT",                    # Git direct-install silent flags
            "AK_LOG_MIRROR",
            "Get-AkSmartAppControlState",     # Smart App Control preflight detection
            "Select-AkDevTools",              # tool-selection menu
            "Rustlang.Rustup",                # Rust via winget (SC)
            "Ensure-BuildTools",
            "vs_BuildTools.exe",
            "Launch-VsDevShell.ps1",          # Developer PowerShell env
            "--find-links",                   # pyturso wheel-first
            "import pyturso",                 # post-deps smoke check
            "Enter-VsDevShell",               # DevShell via the cmdlet (no exit(); no picker)
            "__AK_DEVSHELL_PREFIX__",         # workspace profile placeholder filled after Build Tools
            "already downloaded",             # Verify pillar: download idempotency (skip cached file)
            "Write-Utf8IfChanged",            # write generated files only when content differs
            "already present; skipping",      # WindowsAppRuntime pre-flight skip on warm re-run
            "Ensure-Skills",                  # skills selection + install step
            "api.github.com/users/LevyBytes", # live skill-repo enumeration
            "Set-AkStepSkipped",              # user-deselected optional tools report SKIPPED, not OK
            "SKIPPED",                        # distinct step status for a user-skipped option
            "Initialize-AkBanner",            # sticky progress banner: reserve the pinned block
            "Write-AkBanner",                 # sticky banner: single-pass in-place redraw (no flashing)
            "Update-AkBannerActivity",        # sticky banner: throttled live current-download line
            "NpmGlobalDir",                   # DEVROOT npm global packages dir
            "npm-global",                     # ...added to PATH + npm config set prefix (SC parity)
            # Rename + in-installer duplication + smart toolselect (this round)
            "$ProjectName",                   # -ProjectName rename param (folder + workspace name)
            "__AK_WS_LABEL__",                # de-hardcoded workspace label placeholder
            "Resolve-AkProjectMode",          # fresh-rename vs duplicate decision, before any tool prompt
            "Get-AkExistingProject",          # detect a git-backed agent-kaizen to duplicate from
            "Invoke-AkDuplication",           # duplication flow (link engine, copy customizable, own DB)
            "Invoke-AkSkillsInto",            # skills impl shared by normal setup + duplication
            "Test-AkTursoSatisfied",          # skip the Rust/Build-Tools prompt when already satisfied
            "New-AkLink",                     # junction dirs / symlink-or-copy files for the shared engine
            "KAIZEN_REPO_ROOT",               # kaizen.cmd wrapper isolates the duplicated project's DB
        ):
            self.assertIn(token, self.text, f"expected fix token missing: {token}")

    def test_claude_runtime_selector_contract(self):
        for token in (
            "[switch] $WithClaudeRuntime",
            "AK_WITH_CLAUDE_RUNTIME",
            "$PSBoundParameters.ContainsKey('WithClaudeRuntime')",
            "1, true, enabled, 0, false, disabled",
            "claudeRuntime = [bool]$script:ClaudeRuntimeSelected",
            "Test-AkManagedNodeTools",
            "[IO.FileAttributes]::ReparsePoint",
            "node-v$v-win-$nodeArch.zip",
            "setup\\claude_runtime_setup.py",
            "Ensure-AkClaudeRuntime",
            "'check'",
            "'install'",
        ):
            self.assertIn(token, self.text, f"Claude runtime selector contract missing: {token}")
        node_step = self.text.index("Id = 'node'")
        runtime_step = self.text.index("Id = 'claude-runtime'")
        vscode_step = self.text.index("Id = 'vscode'")
        self.assertLess(node_step, runtime_step)
        self.assertLess(runtime_step, vscode_step)
        runtime_body = self.text.index("function Ensure-AkClaudeRuntime")
        warm_check = self.text.index("if (Test-AkClaudeRuntimeReady)", runtime_body)
        no_network = self.text.index("if ($NoNetwork)", warm_check)
        install = self.text.index("'install'", no_network)
        recheck = self.text.index("if (-not (Test-AkClaudeRuntimeReady))", install)
        self.assertLess(warm_check, no_network)
        self.assertLess(no_network, install)
        self.assertLess(install, recheck)

    @unittest.skipUnless(
        platform.system() == "Windows" and shutil.which("powershell"),
        "PowerShell AST parse is Windows-only",
    )
    def test_payload_parses(self):
        payload = _payload(self.text)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".ps1", encoding="utf-8", delete=False) as fh:
                fh.write(payload)
                tmp = fh.name
            ps_tmp = tmp.replace("'", "''")
            script = (
                "$e=$null;$t=$null;"
                f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ps_tmp}',[ref]$t,[ref]$e);"
                "if($e -and $e.Count){$e|ForEach-Object{Write-Output $_.Message};exit 1}"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)


if __name__ == "__main__":
    unittest.main()
