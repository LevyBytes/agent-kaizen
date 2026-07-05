"""Static guards on the Linux/macOS installer (installer-common.sh, install-agent-kaizen.sh, setup.sh).

These lock in the core-robustness parity brought over from the Windows installer so it cannot
silently regress:
  * Turso build deps -- the bootstrap installs a C toolchain (build-essential/gcc + curl), and the
    repo-local setup ensures Rust (rustup, DEVROOT-scoped) before compiling pyturso, wheels-first,
    with an `import turso` acceptance check.
  * PATH/env persistence (ak_add_path_entry / ak_persist_env), a distinct SKIPPED status, a download
    cache, sysexits-style exit codes, DEVROOT validation, and non-interactive apt.
When bash/shellcheck are available the scripts are also parsed so an edit that breaks syntax fails
the suite rather than a user's install.
"""

from __future__ import annotations

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
        ), "installer-common.sh")

    def test_bootstrap_tokens(self):
        self._assert_all(self.boot, (
            "have_cc",                # C-compiler detection
            "ensure_build_toolchain",
            "install_build_pkgs",
            "build-essential",        # apt C toolchain for the pyturso build
            "DEBIAN_FRONTEND=noninteractive",  # no tzdata hang on bare Ubuntu
            "validate_devroot",       # reject protected roots (no silent fallback)
        ), "install-agent-kaizen.sh")

    def test_setup_tokens(self):
        self._assert_all(self.local, (
            "step_rust",              # Rust step before deps
            "ak_ensure_rust",
            "import turso",           # pre-flight skip + acceptance smoke check (pyturso imports as turso)
            "ak_have_pyturso_wheel",
            "--prefer-binary",        # wheels-first pip (mirror Windows Step-Dependencies)
            "--find-links",
            "pyturso==0.6.1",         # cache the built wheel for reuse
        ), "setup.sh")

    def test_harness_present(self):
        self.assertTrue(HARNESS.is_file(), "WSL sandbox test harness missing")
        text = HARNESS.read_text(encoding="utf-8")
        # Isolation contract: local source is staged + cloned; the run is a throwaway instance.
        self.assertIn("--repo-source", text)
        self.assertIn("wsl.exe", text)

    @staticmethod
    def _lf_bytes(p: Path) -> bytes:
        # Feed scripts on stdin (path-translation-agnostic across Git Bash / the WSL launcher) and
        # force LF so a Windows text-mode pipe cannot reintroduce CR and cause a false failure.
        return p.read_bytes().replace(b"\r\n", b"\n")

    @unittest.skipUnless(shutil.which("bash"), "bash not available")
    def test_bash_syntax(self):
        for p in (COMMON, BOOT, LOCAL):
            proc = subprocess.run(["bash", "-n"], input=self._lf_bytes(p), capture_output=True)
            self.assertEqual(proc.returncode, 0, f"{p.name}:\n{proc.stderr.decode(errors='replace')}")

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not available")
    def test_shellcheck(self):
        for p in (COMMON, BOOT, LOCAL):
            proc = subprocess.run(
                ["shellcheck", "--shell=bash", "-S", "warning", "-e", "SC1091", "-"],
                input=self._lf_bytes(p), capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, f"{p.name}:\n{proc.stdout.decode(errors='replace')}")


if __name__ == "__main__":
    unittest.main()
