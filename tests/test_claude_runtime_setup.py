from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = REPO_ROOT / "AI" / "work"
SETUP_PATH = REPO_ROOT / "setup" / "claude_runtime_setup.py"
SETUP_SPEC = importlib.util.spec_from_file_location("kaizen_claude_runtime_setup", SETUP_PATH)
if SETUP_SPEC is None or SETUP_SPEC.loader is None:
    raise RuntimeError(f"cannot load Claude runtime setup helper: {SETUP_PATH}")
setup = importlib.util.module_from_spec(SETUP_SPEC)
SETUP_SPEC.loader.exec_module(setup)


def invoke(*args: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with redirect_stdout(output):
        result = setup.main(list(args))
    return result, json.loads(output.getvalue())


class ClaudeRuntimeSetupTest(unittest.TestCase):
    def test_check_is_offline_path_free_and_sanitized(self) -> None:
        capability = {
            "runtime_kind": "claude-agent-sdk",
            "runtime_version": "0.3.207",
            "runtime_status": "unavailable",
            "worker_protocol": 1,
            "code": "DENIED_SDK_UNAVAILABLE",
        }
        with mock.patch.object(setup, "runtime_capability", return_value=capability):
            rc, payload = invoke("check")
        self.assertEqual(rc, 2)
        self.assertEqual(payload["status"], "DENIED")
        self.assertEqual(payload["code"], "DENIED_SDK_UNAVAILABLE")
        encoded = json.dumps(payload)
        self.assertNotIn(str(REPO_ROOT), encoded)
        self.assertNotIn("path", encoded.casefold())

    def test_unexpected_check_failure_is_redacted(self) -> None:
        with mock.patch.object(setup, "runtime_capability", side_effect=RuntimeError("secret path")):
            rc, payload = invoke("check")
        self.assertEqual(rc, 2)
        self.assertEqual(payload["code"], "DENIED_SDK_UNAVAILABLE")
        self.assertNotIn("secret", json.dumps(payload))
        self.assertNotIn("path", json.dumps(payload).casefold())

    def test_install_uses_only_exact_devroot_node_and_npm(self) -> None:
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="claude-setup-test-", dir=WORK_ROOT) as temporary:
            devroot = Path(temporary)
            node_root = devroot / "node"
            node_root.mkdir()
            node = node_root / ("node.exe" if os.name == "nt" else "bin/node")
            npm = node_root / ("npm.cmd" if os.name == "nt" else "bin/npm")
            node.parent.mkdir(parents=True, exist_ok=True)
            npm.parent.mkdir(parents=True, exist_ok=True)
            node.write_bytes(b"node")
            npm.write_bytes(b"npm")
            with (
                mock.patch.dict(os.environ, {"DEVROOT": str(devroot)}),
                mock.patch.object(setup, "install_runtime", return_value={"status": "ready", "warm": False}) as install,
            ):
                rc, payload = invoke("install")
            self.assertEqual(rc, 0)
            self.assertEqual(payload["status"], "OK")
            self.assertFalse(payload["warm"])
            _, kwargs = install.call_args
            self.assertEqual(kwargs["node_executable"], node.resolve())
            self.assertEqual(kwargs["npm_executable"], npm.resolve())
            self.assertNotIn("path", json.dumps(payload).casefold())

    def test_install_reports_warm_reuse_without_running_its_own_commands(self) -> None:
        with (
            mock.patch.object(setup, "_devroot_tools", return_value=(Path("node"), Path("npm"))),
            mock.patch.object(setup, "install_runtime", return_value={"status": "ready", "warm": True}),
        ):
            rc, payload = invoke("install")
        self.assertEqual(rc, 0)
        self.assertTrue(payload["warm"])

    @unittest.skipIf(os.name == "nt", "POSIX Node archives use the internal npm symlink")
    def test_install_accepts_only_a_devroot_internal_posix_npm_symlink(self) -> None:
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="claude-setup-posix-link-", dir=WORK_ROOT) as temporary:
            devroot = Path(temporary)
            node_root = devroot / "node"
            node = node_root / "bin/node"
            npm = node_root / "bin/npm"
            npm_target = node_root / "lib/node_modules/npm/bin/npm-cli.js"
            node.parent.mkdir(parents=True)
            npm_target.parent.mkdir(parents=True)
            node.write_bytes(b"node")
            npm_target.write_bytes(b"npm")
            npm.symlink_to(Path("../lib/node_modules/npm/bin/npm-cli.js"))
            with (
                mock.patch.dict(os.environ, {"DEVROOT": str(devroot)}),
                mock.patch.object(setup, "install_runtime", return_value={"status": "ready", "warm": False}) as install,
            ):
                rc, _payload = invoke("install")
            self.assertEqual(rc, 0)
            _, kwargs = install.call_args
            self.assertEqual(kwargs["node_executable"], node.resolve())
            self.assertEqual(kwargs["npm_executable"], npm_target.resolve())

    @unittest.skipIf(os.name == "nt", "POSIX symlink policy requires native POSIX semantics")
    def test_install_denies_posix_node_symlink_and_npm_escape(self) -> None:
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="claude-setup-posix-deny-", dir=WORK_ROOT) as temporary:
            root = Path(temporary)
            devroot = root / "devroot"
            outside = root / "outside"
            node_root = devroot / "node"
            node = node_root / "bin/node"
            npm = node_root / "bin/npm"
            node_target = node_root / "lib/node-real"
            outside.mkdir()
            (outside / "npm").write_bytes(b"npm")
            node.parent.mkdir(parents=True)
            node_target.parent.mkdir(parents=True)
            node_target.write_bytes(b"node")
            node.symlink_to(Path("../lib/node-real"))
            npm.symlink_to(outside / "npm")
            with mock.patch.dict(os.environ, {"DEVROOT": str(devroot)}), mock.patch.object(
                setup, "install_runtime",
            ) as install:
                rc, payload = invoke("install")
            self.assertEqual(rc, 2)
            self.assertEqual(payload["field"], "node_executable")
            install.assert_not_called()

            node.unlink()
            node.write_bytes(b"node")
            with mock.patch.dict(os.environ, {"DEVROOT": str(devroot)}), mock.patch.object(
                setup, "install_runtime",
            ) as install:
                rc, payload = invoke("install")
            self.assertEqual(rc, 2)
            self.assertEqual(payload["field"], "npm_executable")
            install.assert_not_called()

    def test_missing_devroot_fails_closed_without_path_or_path_fallback(self) -> None:
        environment = dict(os.environ)
        environment.pop("DEVROOT", None)
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
            setup, "install_runtime",
        ) as install:
            rc, payload = invoke("install")
        self.assertEqual(rc, 2)
        self.assertEqual(payload["field"], "devroot")
        install.assert_not_called()
        self.assertNotIn("path", json.dumps(payload).casefold())

    def test_unexpected_install_failure_is_redacted(self) -> None:
        with (
            mock.patch.object(setup, "_devroot_tools", return_value=(Path("node"), Path("npm"))),
            mock.patch.object(setup, "install_runtime", side_effect=RuntimeError("secret path")),
        ):
            rc, payload = invoke("install")
        self.assertEqual(rc, 2)
        self.assertEqual(payload["field"], "runtime")
        self.assertNotIn("secret", json.dumps(payload))
        self.assertNotIn("path", json.dumps(payload).casefold())


if __name__ == "__main__":
    unittest.main()
