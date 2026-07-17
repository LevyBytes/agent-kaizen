"""M0 seams: mode enums parse, KAIZEN_DIST_MODE is inert for off/observe/active while invalid values deny, the
`daemon` subcommand is a parse-only skeleton, and the tailnet probe degrades to
pure-local without raising."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _harness import REPO_ROOT, run

sys.path.insert(0, str(REPO_ROOT))

from kaizen_components.denials import KaizenDenied  # noqa: E402
from kaizen_components.fleet import net  # noqa: E402
from kaizen_components.orchestration import modes  # noqa: E402


def _scratch(test: unittest.TestCase, prefix: str) -> Path:
    """Create one auto-cleaned isolated CLI plane for a mode/daemon test."""
    root = Path(tempfile.mkdtemp(prefix=prefix))
    test.addCleanup(shutil.rmtree, root, ignore_errors=True)
    return root


class ModeParseTest(unittest.TestCase):
    def test_parse_matrix_accepts_every_declared_value(self) -> None:
        for value in modes.DIST_MODES:
            self.assertEqual(modes.parse_dist_mode(value), value)
        for value in modes.CONTROLLERS:
            self.assertEqual(modes.parse_controller(value), value)
        for value in modes.SESSION_MODES:
            self.assertEqual(modes.parse_session_mode(value), value)

    def test_defaults(self) -> None:
        self.assertEqual(modes.parse_dist_mode(None), "off")
        self.assertEqual(modes.parse_controller(None), "observed")
        self.assertEqual(modes.parse_session_mode(None), "observe")

    def test_case_and_whitespace_normalized(self) -> None:
        self.assertEqual(modes.parse_dist_mode(" Active "), "active")
        self.assertEqual(modes.parse_session_mode("HOOKED"), "hooked")

    def test_invalid_values_deny(self) -> None:
        for fn in (modes.parse_dist_mode, modes.parse_controller, modes.parse_session_mode):
            with self.assertRaises(KaizenDenied) as ctx:
                fn("bogus")
            self.assertEqual(ctx.exception.code, "DENIED_MODE_INVALID")
            self.assertEqual(ctx.exception.exit_code, 2)

    def test_dist_mode_env(self) -> None:
        self.assertEqual(modes.dist_mode(env={}), "off")
        self.assertEqual(modes.dist_mode(env={"KAIZEN_DIST_MODE": "observe"}), "observe")
        with self.assertRaises(KaizenDenied):
            modes.dist_mode(env={"KAIZEN_DIST_MODE": "on"})

    def test_hooked_lane_consumed_only_by_mclaude(self) -> None:
        # M-CLAUDE LANDED: the reserved "hooked" session mode now has a runtime governor lane. This
        # inverts the pre-landing deferral guard -- it asserts the lane EXISTS and that its consumers are
        # EXACTLY the M-CLAUDE governor + its wiring, so a stray future 'hooked' reference elsewhere in
        # the package still trips (the guard survives the landing, it does not just get deleted).
        pkg = Path(REPO_ROOT) / "kaizen_components"
        allowed = {
            Path("orchestration") / "hooked.py",                  # the governor (M5b)
            Path("orchestration") / "claude_hook_shim.py",       # relocated hook executable
            Path("orchestration") / "supervisor.py",              # hooks/* loopback wiring
            Path("orchestration") / "daemon_cli.py",              # daemon hooks install|verify|remove
            Path("schemas") / "registry.py",                      # Claude/OTEL kind registration
        }
        hits = {
            path.relative_to(pkg)
            for path in pkg.rglob("*.py")
            if "hooked" in path.read_text(encoding="utf-8")
            and path.relative_to(pkg) != Path("orchestration") / "modes.py"
        }
        self.assertIn(Path("orchestration") / "hooked.py", hits, "M-CLAUDE hooked governor must exist")
        self.assertIn(
            Path("orchestration") / "claude_hook_shim.py",
            hits,
            "relocated Claude hook shim must remain an explicit hooked-lane consumer",
        )
        self.assertEqual(hits - allowed, set(), f"unexpected 'hooked' consumer(s): {hits - allowed}")


class DistModeInertTest(unittest.TestCase):
    def test_dist_mode_env_changes_nothing_today(self) -> None:
        # off => byte-identical; and since no runtime consumes the seam yet, observe
        # and active must also be byte-identical for the shipped surface (K0).
        root = _scratch(self, "kaizen-m0-")
        outputs = set()
        for value in ("off", "observe", "active"):
            proc = run(root, "K0", "--json", env={"KAIZEN_DIST_MODE": value}, timeout=30)
            self.assertEqual(proc.returncode, 0)
            outputs.add(proc.stdout)
        self.assertEqual(len(outputs), 1, "KAIZEN_DIST_MODE must be inert at M0")


class DaemonSkeletonTest(unittest.TestCase):
    def test_daemon_bare_prints_help(self) -> None:
        root = _scratch(self, "kaizen-m1-")
        proc = run(root, "daemon", timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("supervisor daemon", proc.stdout)
        # A bare `daemon` (help) must not initialize the DB.
        self.assertFalse((root / "AI" / "db" / "kaizen.db").exists(), "bare daemon must not create the DB")

    def test_daemon_help_flag(self) -> None:
        root = _scratch(self, "kaizen-m1-")
        proc = run(root, "daemon", "--help", timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("run the per-workspace supervisor daemon", proc.stdout)
        self.assertIn("status", proc.stdout)

    def test_daemon_run_boots_and_exits(self) -> None:
        # M1: `daemon run --exit-after-boot` boots (single-instance + orphan-sweep) then
        # exits 0. The undocumented seam keeps this test off a backgrounded live daemon.
        root = _scratch(self, "kaizen-m1-")
        proc = run(root, "daemon", "run", "--exit-after-boot", "--json", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["booted_and_exited"])
        # An empty-DB boot sweeps nothing; the sweep still ran.
        self.assertEqual(payload["swept"]["finalized"], [])

    def test_daemon_status_reports_not_running(self) -> None:
        root = _scratch(self, "kaizen-m1-")
        proc = run(root, "daemon", "status", "--json", timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(json.loads(proc.stdout)["running"])

    def test_daemon_is_not_a_registered_op(self) -> None:
        from _harness import alias_codes

        self.assertNotIn("daemon", [c.lower() for c in alias_codes()])


class TailnetProbeTest(unittest.TestCase):
    def test_absent_binary_is_pure_local(self) -> None:
        with mock.patch.object(net.shutil, "which", return_value=None):
            self.assertEqual(net.tailnet_probe(), (False, None))
            self.assertFalse(net.on_tailnet())
            self.assertIsNone(net.tailnet_self())

    def test_probe_failure_never_raises(self) -> None:
        with mock.patch.object(net.shutil, "which", return_value="tailscale"):
            with mock.patch.object(net.subprocess, "run", side_effect=OSError("boom")):
                self.assertEqual(net.tailnet_probe(), (False, None))
            timeout = net.subprocess.TimeoutExpired(cmd="tailscale", timeout=1)
            with mock.patch.object(net.subprocess, "run", side_effect=timeout):
                self.assertEqual(net.tailnet_probe(), (False, None))
            bad_json = mock.Mock(returncode=0, stdout="not json")
            with mock.patch.object(net.subprocess, "run", return_value=bad_json):
                self.assertEqual(net.tailnet_probe(), (False, None))
            nonzero = mock.Mock(returncode=1, stdout="{}")
            with mock.patch.object(net.subprocess, "run", return_value=nonzero):
                self.assertEqual(net.tailnet_probe(), (False, None))

    def test_running_backend_reports_name(self) -> None:
        payload = '{"BackendState": "Running", "Self": {"DNSName": "devpc.tail1234.ts.net."}}'
        ok = mock.Mock(returncode=0, stdout=payload)
        with mock.patch.object(net.shutil, "which", return_value="tailscale"):
            with mock.patch.object(net.subprocess, "run", return_value=ok):
                self.assertEqual(net.tailnet_probe(), (True, "devpc.tail1234.ts.net"))

    def test_stopped_backend_is_offline_but_named(self) -> None:
        payload = '{"BackendState": "Stopped", "Self": {"DNSName": "devpc.tail1234.ts.net."}}'
        ok = mock.Mock(returncode=0, stdout=payload)
        with mock.patch.object(net.shutil, "which", return_value="tailscale"):
            with mock.patch.object(net.subprocess, "run", return_value=ok):
                self.assertEqual(net.tailnet_probe(), (False, "devpc.tail1234.ts.net"))


if __name__ == "__main__":
    unittest.main()
