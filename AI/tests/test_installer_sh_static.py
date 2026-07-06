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
import shutil
import subprocess
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[2] / "setup"
COMMON = SETUP / "installer-common.sh"
BOOT = SETUP / "install-agent-kaizen.sh"
LOCAL = SETUP / "setup.sh"
HARNESS = Path(__file__).resolve().parent / "wsl-sandbox-test.ps1"


class InstallerShStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.common = COMMON.read_text(encoding="utf-8")
        cls.boot = BOOT.read_text(encoding="utf-8")
        cls.local = LOCAL.read_text(encoding="utf-8")

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
            "pyturso==0.6.1",         # cache the built wheel for reuse
            "AK_TOOL_RUST",           # toolchain choice threaded from the bootstrap menu
            "AK_PROJECT_NAME",        # rename: project name from the bootstrap
            "WS_LABEL",               # parameterized workspace display label
            "ak_install_skills",      # normal install now selects + installs skills (not scaffold-only)
            "-vscode.sh",             # normal install writes its own VS Code launcher (parity)
        ), "setup.sh")

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
            proc = subprocess.run(["bash", "-n"], input=self._lf_bytes(p), capture_output=True)
            self.assertEqual(proc.returncode, 0, f"{p.name}:\n{proc.stderr.decode(errors='replace')}")


if __name__ == "__main__":
    unittest.main()
