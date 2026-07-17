"""Static guards on the Linux/macOS installer (installer-common.sh, install-agent-kaizen.sh, setup.sh).

These lock in the core-robustness parity brought over from the Windows installer so it cannot
silently regress:
  * Turso build deps -- the bootstrap installs a C toolchain (build-essential/gcc + curl), and the
    repo-local setup ensures Rust (rustup, DEVROOT-scoped) before compiling pyturso, wheels-first,
    with an `import turso` acceptance check.
  * PATH/env persistence (ak_add_path_entry / ak_persist_env), a distinct SKIPPED status, a download
    cache, sysexits-style exit codes, DEVROOT validation, and non-interactive apt.
On a POSIX host with native bash the scripts are also parsed (`bash -n`) so an edit that breaks
syntax fails the suite rather than a user's install.
"""

from __future__ import annotations

import platform
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1] / "setup"
WORK_ROOT = SETUP.parent / "AI" / "work"
COMMON = SETUP / "installer-common.sh"
BOOT = SETUP / "install-agent-kaizen.sh"
LOCAL = SETUP / "setup.sh"
REQUIREMENTS = SETUP.parent / "requirements-kaizen.txt"
HARNESS = Path(__file__).resolve().parent / "wsl-sandbox-test.ps1"


class InstallerShStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.common = COMMON.read_text(encoding="utf-8")
        cls.boot = BOOT.read_text(encoding="utf-8")
        cls.local = LOCAL.read_text(encoding="utf-8")
        cls.requirements = REQUIREMENTS.read_text(encoding="utf-8")

    def _assert_all(self, text: str, tokens, where: str):
        for tok in tokens:
            self.assertIn(tok, text, f"expected token missing in {where}: {tok}")

    def test_common_library_tokens(self):
        self._assert_all(self.common, (
            "ak_step_skipped",        # distinct SKIPPED status
            "SKIPPED",
            "ak_add_path_entry",      # PATH persistence (mirror Add-AkPathEntry)
            "ak_persist_env",         # env persistence (CARGO_HOME/RUSTUP_HOME)
            "ak_ensure_rust",         # rustup into DEVROOT-scoped CARGO_HOME/RUSTUP_HOME
            "ak_failure_report",      # failure report (mirror Write-AkFailureReport)
            "NO_COLOR",               # TTY/NO_COLOR-aware color (cli-design)
            "AK_EX_UNAVAILABLE",      # sysexits-style exit codes
            "already downloaded",     # download cache-skip (Verify pillar)
            "ak_install_skills",      # shared skills installer (normal install parity with Windows)
            "api.github.com/users/LevyBytes",  # live skill-repo enumeration (mirror Ensure-Skills)
        ), "installer-common.sh")

    def test_bootstrap_tokens(self):
        self._assert_all(self.boot, (
            "have_cc",                # C-compiler detection
            "ensure_build_toolchain",
            "install_build_pkgs",
            "build-essential",        # apt C toolchain for the pyturso build
            "DEBIAN_FRONTEND=noninteractive",  # no tzdata hang on bare Ubuntu
            "validate_devroot",       # reject protected roots (no silent fallback)
            "select_dev_tools",       # interactive optional-dependency menu
            "ensure_ca_certs",        # HTTPS CA bundle for pip even without the toolchain
            "install_optional_tools", # apt-install selected Node/CMake/.NET
            "--with-rust",            # opt into the Rust + C toolchain (else Turso uses its wheel)
            "--with-claude-runtime",  # approved opt-in Claude subscription runtime selector
            "AK_WITH_CLAUDE_RUNTIME", # approved environment selector
            "--project-name",         # rename: clone folder + workspace name
            "--duplicate",            # in-installer duplication of an existing agent-kaizen
            "--new-venv",             # duplicate with a separate venv instead of sharing kaizen
            "detect_source_project",  # find a git-backed agent-kaizen to duplicate from
            "maybe_offer_duplication",# interactive offer when an existing project is detected
            "read_project_name",      # fresh-install rename prompt (parity with Windows)
            "Choose [1/2/3]",         # 3-option duplication menu (same venv / separate venv / skip)
            "AK_NAME_EXPLICIT",       # only prompt for a fresh-install name when not set by flag/env
            "step_dup_create",        # duplication: link engine + copy customizable files
            "KAIZEN_REPO_ROOT",       # kaizen.sh wrapper isolates the new project's DB
            "AK_PROJECT_NAME",        # rename threaded from the bootstrap to the setup engine
        ), "install-agent-kaizen.sh")

    def test_setup_tokens(self):
        self._assert_all(self.local, (
            "step_rust",              # Rust step before deps
            "ak_ensure_rust",
            "import turso",           # pre-flight skip + acceptance smoke check (pyturso imports as turso)
            "ak_have_pyturso_wheel",
            "--prefer-binary",        # wheels-first pip when the toolchain is selected
            "--only-binary=:all:",    # wheel-only when no toolchain (clean error if no wheel)
            "--find-links",
            'version("pyturso")',     # derive the installed version used for wheel caching
            '"pyturso==$pyturso_version"', # cache exactly the installed build for reuse
            "AK_TOOL_RUST",           # toolchain choice threaded from the bootstrap menu
            "AK_WITH_CLAUDE_RUNTIME", # direct setup supports the approved environment selector
            "step_claude_runtime",    # warm-check/install/recheck runtime step
            "$REPO_ROOT/setup/claude_runtime_setup.py", # one audited runtime setup helper
            "/node/bin/node",         # exact DEVROOT-managed Node; never a PATH/system fallback
            "/node/bin/npm",          # exact DEVROOT-managed npm
            "AK_PROJECT_NAME",        # rename: project name from the bootstrap
            "WS_LABEL",               # parameterized workspace display label
            "ak_install_skills",      # normal install now selects + installs skills (not scaffold-only)
            "-vscode.sh",             # normal install writes its own VS Code launcher (parity)
        ), "setup.sh")
        self.assertIn("pyturso==0.6.1", self.requirements)  # retain the dependency-pin guard

    def test_claude_runtime_selector_contract(self):
        self._assert_all(self.boot, (
            'AK_WITH_CLAUDE_RUNTIME="${AK_WITH_CLAUDE_RUNTIME:-0}"',
            '--with-claude-runtime) AK_WITH_CLAUDE_RUNTIME=1',
            '1|true|enabled',
            '0|false|disabled|""',
            'args+=("--with-claude-runtime")',
            '"selection": {"claudeRuntime": %s}',
        ), "install-agent-kaizen.sh")
        self._assert_all(self.local, (
            'AK_WITH_CLAUDE_RUNTIME="${AK_WITH_CLAUDE_RUNTIME:-0}"',
            '--with-claude-runtime) AK_WITH_CLAUDE_RUNTIME=1',
            'ak_step_obj claude-runtime',
            "ak_assert_network_allowed 'Claude Agent SDK runtime installation'",
            '"$VENV_PY" "$helper" install',
        ), "setup.sh")

        # CLI parsing happens before normalization, so the approved flag overrides an environment
        # value of false/disabled. The provider selection is not reset by --no-dev-tools.
        self.assertLess(
            self.boot.index('--with-claude-runtime) AK_WITH_CLAUDE_RUNTIME=1'),
            self.boot.index('AK_WITH_CLAUDE_RUNTIME="$(normalize_bool'),
        )
        no_dev_start = self.boot.index('if [ "$AK_NO_DEV_TOOLS" -eq 1 ]')
        no_dev_end = self.boot.index('\n  fi', no_dev_start) + len('\n  fi')
        no_dev_block = self.boot[no_dev_start:no_dev_end]
        self.assertTrue(no_dev_block.strip())
        self.assertNotIn('AK_WITH_CLAUDE_RUNTIME=', no_dev_block)

        deps_step = self.local.index("ak_run_step deps '")
        runtime_step = self.local.index("ak_run_step claude-runtime '")
        skills_step = self.local.index("ak_run_step skills '")
        self.assertLess(deps_step, runtime_step)
        self.assertLess(runtime_step, skills_step)
        runtime_body = self.local[
            self.local.index('step_claude_runtime() {'):
            self.local.index('step_skills() {')
        ]
        self.assertNotIn('command -v node', runtime_body)
        self.assertNotIn('command -v npm', runtime_body)
        self.assertLess(runtime_body.index('"$VENV_PY" "$helper" check'), runtime_body.index('ak_assert_network_allowed'))
        self.assertLess(runtime_body.index('ak_assert_network_allowed'), runtime_body.index('"$VENV_PY" "$helper" install'))

    @unittest.skipUnless(platform.system() != "Windows" and shutil.which("bash"), "native bash not available")
    def test_claude_runtime_cli_overrides_environment_in_plan(self):
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ak-posix-selector-", dir=WORK_ROOT) as temporary:
            root = Path(temporary)
            plan = root / "plan.json"
            environment = dict(os.environ)
            environment["AK_WITH_CLAUDE_RUNTIME"] = "disabled"
            proc = subprocess.run(
                [
                    "bash", str(BOOT), "--devroot", str(root / "dev"), "--plan-only",
                    "--no-input", "--no-dev-tools", "--with-claude-runtime",
                    "--emit-plan-json", str(plan),
                ],
                capture_output=True,
                env=environment,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(json.loads(plan.read_text(encoding="utf-8"))["selection"]["claudeRuntime"])

            environment["AK_WITH_CLAUDE_RUNTIME"] = "maybe"
            invalid = subprocess.run(
                ["bash", str(BOOT), "--devroot", str(root / "dev"), "--plan-only", "--no-input"],
                capture_output=True,
                env=environment,
                text=True,
                timeout=120,
            )
            self.assertEqual(invalid.returncode, 64)
            self.assertIn("invalid AK_WITH_CLAUDE_RUNTIME boolean", invalid.stderr)

    # The WSL harness is a Windows-only maintainer tool that may not be part of every checkout; only
    # validate its contract when it is present rather than forcing it into the shipped tree.
    @unittest.skipUnless(HARNESS.is_file(), "WSL sandbox harness not in this checkout (maintainer tool)")
    def test_harness_present(self):
        text = HARNESS.read_text(encoding="utf-8")
        # Isolation contract: local source is staged + cloned; the run is a throwaway instance.
        self.assertIn("--repo-source", text)
        self.assertIn("wsl.exe", text)

    @staticmethod
    def _lf_bytes(p: Path) -> bytes:
        # Feed scripts on stdin and force LF so a Windows text-mode pipe cannot reintroduce CR.
        return p.read_bytes().replace(b"\r\n", b"\n")

    # Only run on a POSIX host: on Windows CI `bash` resolves to Git Bash / the WSL launcher whose
    # stdin handling is unreliable, giving false failures. The Ubuntu CI job runs native bash, which
    # is the meaningful platform for a Linux installer's syntax check. (shellcheck is intentionally
    # not gated here -- at warning level it flags intentional patterns like unquoted $SUDO, and at
    # error level it only duplicates this parse check.)
    @unittest.skipUnless(platform.system() != "Windows" and shutil.which("bash"), "native bash not available")
    def test_bash_syntax(self):
        for p in (COMMON, BOOT, LOCAL):
            proc = subprocess.run(["bash", "-n"], input=self._lf_bytes(p), capture_output=True, timeout=30)
            self.assertEqual(proc.returncode, 0, f"{p.name}:\n{proc.stderr.decode(errors='replace')}")


if __name__ == "__main__":
    unittest.main()
