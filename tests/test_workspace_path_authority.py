"""Security regression suite for handle-anchored bounded reads, exact replacement/unlink, persistent process locks, path rejection, and root-swap resistance."""
from __future__ import annotations

import hashlib
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from kaizen_components.orchestration import workspace_path_authority as authority_module
from kaizen_components.orchestration.workspace_path_authority import (
    WorkspacePathAuthority,
    WorkspacePathError,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = REPO_ROOT / "AI" / "work" / "workspace-path-authority-tests"


def bounded_readline(child: subprocess.Popen, stage: str, timeout: float = 10.0) -> str:
    result: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            result.put(("line", child.stdout.readline()))
        except BaseException as error:  # noqa: BLE001 -- surface reader failures in the test thread
            result.put(("error", error))

    threading.Thread(target=read, daemon=True, name=f"authority-{stage}-reader").start()
    try:
        kind, value = result.get(timeout=timeout)
    except queue.Empty:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        stderr = child.stderr.read() if child.stderr is not None else ""
        raise AssertionError(f"child timed out during {stage}; stderr={stderr!r}") from None
    if kind == "error":
        raise AssertionError(f"child read failed during {stage}: {value}")
    return str(value).strip()


class WorkspacePathAuthorityTest(unittest.TestCase):
    """Exercises cross-process locking and exact handle-owned workspace operations under isolated AI/work roots."""
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\/").casefold()
            if TEST_ROOT.drive.rstrip("\\/").casefold() == system_drive:
                self.skipTest("workspace path-authority tests require non-system-drive scratch")
        self.root = Path(tempfile.mkdtemp(prefix="authority-", dir=TEST_ROOT))
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        candidate = Path(os.path.abspath(self.root))
        self.assertEqual(candidate.parent, Path(os.path.abspath(TEST_ROOT)))
        shutil.rmtree(candidate, ignore_errors=False)

    def test_bounded_stable_read_identity_and_close(self) -> None:
        target = self.workspace / "state" / "owned.json"
        target.parent.mkdir()
        body = b'{"ok":true}\n'
        target.write_bytes(body)
        authority = WorkspacePathAuthority(self.workspace)
        self.assertEqual(authority.workspace_root, Path(os.path.abspath(self.workspace)))
        with authority.exclusive() as locked:
            self.assertIs(locked, authority)
            self.assertIsNone(authority.identity("missing.json"))
            value = authority.read("state/owned.json", 1024)
        self.assertEqual(value.data, body)
        self.assertEqual(value.size, len(body))
        self.assertEqual(value.sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual(value.identity, authority.identity("state/owned.json"))
        with self.assertRaises(WorkspacePathError):
            authority.read("state/owned.json", len(body) - 1)
        authority.close()
        authority.close()
        with self.assertRaises(WorkspacePathError):
            authority.identity("state/owned.json")

    def test_process_lock_conflicts_then_reacquires_without_unlinking_lock_file(self) -> None:
        with WorkspacePathAuthority(self.workspace) as first, \
                WorkspacePathAuthority(self.workspace) as second:
            held = first.acquire_process_lock("supervisor.lock")
            self.assertIsNotNone(first.identity("supervisor.lock"))
            with self.assertRaises(OSError):
                second.acquire_process_lock("supervisor.lock")
            held.close()
            held.close()
            again = second.acquire_process_lock("supervisor.lock")
            again.close()
            self.assertIsNotNone(second.identity("supervisor.lock"))

    def test_process_lock_conflicts_across_processes(self) -> None:
        script = (
            "import sys\n"
            "from kaizen_components.orchestration.workspace_path_authority import WorkspacePathAuthority\n"
            "authority=WorkspacePathAuthority(sys.argv[1])\n"
            "held=authority.acquire_process_lock('supervisor.lock')\n"
            "print('READY', flush=True)\n"
            "sys.stdin.readline()\n"
            "held.close(); authority.close()\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(self.workspace)],
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(bounded_readline(child, "readiness"), "READY")
            with WorkspacePathAuthority(self.workspace) as authority:
                with self.assertRaises(OSError):
                    authority.acquire_process_lock("supervisor.lock")
            stdout, stderr = child.communicate("\n", timeout=10)
            self.assertEqual(child.returncode, 0, stdout + stderr)
            with WorkspacePathAuthority(self.workspace) as authority:
                held = authority.acquire_process_lock("supervisor.lock")
                held.close()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_simultaneous_missing_process_lock_file_has_exactly_one_winner(self) -> None:
        script = (
            "import sys\n"
            "from kaizen_components.orchestration.workspace_path_authority import WorkspacePathAuthority\n"
            "authority=WorkspacePathAuthority(sys.argv[1])\n"
            "original_identity=authority.identity\n"
            "captured_missing=original_identity('simultaneous.lock')\n"
            "assert captured_missing is None\n"
            "first_identity=True\n"
            "def synchronized_identity(relative):\n"
            "    global first_identity\n"
            "    if first_identity and str(relative) == 'simultaneous.lock':\n"
            "        first_identity=False; return captured_missing\n"
            "    return original_identity(relative)\n"
            "authority.identity=synchronized_identity\n"
            "print('READY', flush=True)\n"
            "sys.stdin.readline()\n"
            "try:\n"
            "    held=authority.acquire_process_lock('simultaneous.lock')\n"
            "except Exception as error:\n"
            "    print('DENIED:'+type(error).__name__, flush=True); authority.close()\n"
            "else:\n"
            "    print('ACQUIRED', flush=True); sys.stdin.readline(); held.close(); authority.close()\n"
        )
        children = [subprocess.Popen(
            [sys.executable, "-c", script, str(self.workspace)],
            cwd=REPO_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        ) for _ in range(2)]
        try:
            for child in children:
                self.assertEqual(bounded_readline(child, "readiness"), "READY")
            for child in children:
                child.stdin.write("GO\n")
                child.stdin.flush()
            results = [bounded_readline(child, "lock result") for child in children]
            self.assertEqual(results.count("ACQUIRED"), 1, results)
            self.assertEqual(sum(value.startswith("DENIED:") for value in results), 1, results)
            winner = children[results.index("ACQUIRED")]
            winner.stdin.write("RELEASE\n")
            winner.stdin.flush()
            for child in children:
                stdout, stderr = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, stdout + stderr)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    def test_atomic_replace_requires_exact_destination_identity(self) -> None:
        with WorkspacePathAuthority(self.workspace) as authority:
            first = authority.atomic_replace("registry.json", b"one", expected=None, max_bytes=1024)
            first_read = authority.read("registry.json", 1024)
            self.assertEqual(first_read.data, b"one")
            self.assertEqual(first, authority.identity("registry.json"))

            with self.assertRaises(WorkspacePathError):
                authority.atomic_replace("registry.json", b"unexpected", expected=None, max_bytes=1024)
            self.assertEqual(authority.read("registry.json", 1024).data, b"one")

            wrong = replace(first_read, identity=replace(first, inode=first.inode + 1))
            with self.assertRaises(WorkspacePathError):
                authority.atomic_replace("registry.json", b"unexpected", expected=wrong, max_bytes=1024)
            self.assertEqual(authority.read("registry.json", 1024).data, b"one")

            second = authority.atomic_replace("registry.json", b"two", expected=first_read, max_bytes=1024)
            self.assertNotEqual(second, first)
            self.assertEqual(authority.read("registry.json", 1024).data, b"two")

    def test_atomic_replace_refuses_same_identity_same_size_content_drift(self) -> None:
        target = self.workspace / "registry.json"
        with WorkspacePathAuthority(self.workspace) as authority:
            authority.atomic_replace("registry.json", b"one", expected=None, max_bytes=1024)
            expected = authority.read("registry.json", 1024)
            platform_stat = target.stat()
            target.write_bytes(b"bad")
            os.utime(target, ns=(platform_stat.st_atime_ns, platform_stat.st_mtime_ns))
            self.assertEqual(authority.identity("registry.json"), expected.identity)
            with self.assertRaises(WorkspacePathError):
                authority.atomic_replace("registry.json", b"two", expected=expected, max_bytes=1024)
            self.assertEqual(target.read_bytes(), b"bad")

    def test_private_atomic_replace_injected_rename_failure_preserves_old_file(self) -> None:
        with WorkspacePathAuthority(self.workspace) as authority:
            authority.atomic_replace("registry.json", b"old", expected=None, max_bytes=1024)
            expected = authority.read("registry.json", 1024)
            before_staging = set(self.workspace.glob(".kaizen-*.tmp"))
            if os.name == "nt":
                self.assertIsNotNone(authority._windows)
                patcher = mock.patch.object(
                    authority._windows, "rename", side_effect=WorkspacePathError("injected rename"),
                )
            else:
                patcher = mock.patch.object(
                    authority_module.os, "replace", side_effect=OSError("injected rename"),
                )
            with patcher, self.assertRaises(WorkspacePathError):
                authority.atomic_replace(
                    "registry.json", b"new", expected=expected, max_bytes=1024,
                )
            self.assertEqual(authority.read("registry.json", 1024), expected)
            self.assertEqual(set(self.workspace.glob(".kaizen-*.tmp")), before_staging)

    def test_exact_staged_create_and_promote(self) -> None:
        staged_body = b"journaled exact bytes"
        staged_digest = hashlib.sha256(staged_body).hexdigest()
        (self.workspace / "stage").mkdir()
        (self.workspace / "output").mkdir()
        with WorkspacePathAuthority(self.workspace) as authority:
            staged = authority.create_exact_exclusive(
                "stage/apply.tmp", staged_body, max_bytes=1024,
            )
            self.assertEqual(staged.data, staged_body)
            self.assertEqual(staged.sha256, staged_digest)
            self.assertEqual(staged.identity, authority.identity("stage/apply.tmp"))

            with self.assertRaises(WorkspacePathError):
                authority.create_exact_exclusive("stage/apply.tmp", b"clobber", max_bytes=1024)
            self.assertEqual(authority.read("stage/apply.tmp", 1024), staged)

            with self.assertRaises(WorkspacePathError):
                authority.replace_from_exact(
                    "stage/apply.tmp", "output/result.txt",
                    staged_sha256="0" * 64, staged_size=staged.size,
                    expected_target=None, max_bytes=1024,
                )
            with self.assertRaises(WorkspacePathError):
                authority.replace_from_exact(
                    "stage/apply.tmp", "output/result.txt",
                    staged_sha256=staged.sha256, staged_size=staged.size - 1,
                    expected_target=None, max_bytes=1024,
                )
            self.assertEqual(authority.read("stage/apply.tmp", 1024), staged)
            self.assertIsNone(authority.identity("output/result.txt"))

            first = authority.replace_from_exact(
                "stage/apply.tmp", "output/result.txt",
                staged_sha256=staged.sha256, staged_size=staged.size,
                expected_target=None, max_bytes=1024,
            )
            self.assertIsNone(authority.identity("stage/apply.tmp"))
            self.assertEqual(first, authority.identity("output/result.txt"))
            self.assertEqual(authority.read("output/result.txt", 1024).data, staged_body)

            replacement_body = b"replacement exact bytes"
            replacement = authority.create_exact_exclusive(
                "stage/replace.tmp", replacement_body, max_bytes=1024,
            )
            expected = authority.read("output/result.txt", 1024)
            with self.assertRaises(WorkspacePathError):
                authority.replace_from_exact(
                    "stage/replace.tmp", "output/result.txt",
                    staged_sha256=replacement.sha256, staged_size=replacement.size,
                    expected_target=None, max_bytes=1024,
                )
            self.assertEqual(authority.read("stage/replace.tmp", 1024), replacement)
            if os.name != "nt":
                self.assertEqual(authority.read("output/result.txt", 1024), expected)
                return
            second = authority.replace_from_exact(
                "stage/replace.tmp", "output/result.txt",
                staged_sha256=replacement.sha256, staged_size=replacement.size,
                expected_target=expected, max_bytes=1024,
            )
            self.assertIsNone(authority.identity("stage/replace.tmp"))
            self.assertEqual(second, authority.identity("output/result.txt"))
            self.assertEqual(authority.read("output/result.txt", 1024).data, replacement_body)

    @unittest.skipUnless(os.name == "nt", "Windows exact-target disposition promotion")
    def test_windows_existing_target_blocks_in_place_writer_then_promotes(self) -> None:
        target = self.workspace / "target.txt"
        target.write_bytes(b"old")
        body = b"new exact bytes"
        with WorkspacePathAuthority(self.workspace) as authority:
            staged = authority.create_exact_exclusive("stage.tmp", body, max_bytes=1024)
            expected = authority.read("target.txt", 1024)
            with target.open("r+b") as writer:
                with self.assertRaises(WorkspacePathError):
                    authority.replace_from_exact(
                        "stage.tmp", "target.txt",
                        staged_sha256=staged.sha256, staged_size=staged.size,
                        expected_target=expected, max_bytes=1024,
                    )
                writer.seek(0)
                self.assertEqual(writer.read(), b"old")
            self.assertEqual(authority.read("stage.tmp", 1024), staged)
            authority.replace_from_exact(
                "stage.tmp", "target.txt",
                staged_sha256=staged.sha256, staged_size=staged.size,
                expected_target=expected, max_bytes=1024,
            )
            self.assertEqual(authority.read("target.txt", 1024).data, body)

    @unittest.skipUnless(os.name == "nt", "Windows no-clobber recovery collision")
    def test_windows_collision_after_exact_disposition_never_overwrites(self) -> None:
        target = self.workspace / "target.txt"
        target.write_bytes(b"old")
        body = b"intended new bytes"
        with WorkspacePathAuthority(self.workspace) as authority:
            staged = authority.create_exact_exclusive("stage.tmp", body, max_bytes=1024)
            expected = authority.read("target.txt", 1024)
            windows = authority._windows
            self.assertIsNotNone(windows)
            native_rename = windows.rename

            def collide(handle: int, destination: Path, *, replace: bool) -> None:
                self.assertFalse(replace)
                destination.write_bytes(b"collision")
                native_rename(handle, destination, replace=replace)

            with mock.patch.object(windows, "rename", side_effect=collide):
                with self.assertRaises(WorkspacePathError) as raised:
                    authority.replace_from_exact(
                        "stage.tmp", "target.txt",
                        staged_sha256=staged.sha256, staged_size=staged.size,
                        expected_target=expected, max_bytes=1024,
                    )
            self.assertEqual(getattr(raised.exception, "staged_relative", None), "stage.tmp")
            self.assertEqual(authority.read("stage.tmp", 1024), staged)
            self.assertEqual(authority.read("target.txt", 1024).data, b"collision")

    @unittest.skipIf(os.name == "nt", "POSIX no-clobber promotion")
    def test_posix_expected_absent_promotion_does_not_use_replace(self) -> None:
        body = b"no clobber"
        with WorkspacePathAuthority(self.workspace) as authority:
            staged = authority.create_exact_exclusive("stage.tmp", body, max_bytes=1024)
            with mock.patch.object(
                authority_module.os, "replace", side_effect=AssertionError("replace may clobber"),
            ):
                authority.replace_from_exact(
                    "stage.tmp", "target.txt",
                    staged_sha256=staged.sha256, staged_size=staged.size,
                    expected_target=None, max_bytes=1024,
                )
            self.assertEqual(authority.read("target.txt", 1024).data, body)

    @unittest.skipIf(os.name == "nt", "POSIX existing-target refusal")
    def test_posix_existing_target_promotion_fails_before_target_mutation(self) -> None:
        target = self.workspace / "target.txt"
        target.write_bytes(b"original")
        with WorkspacePathAuthority(self.workspace) as authority:
            staged = authority.create_exact_exclusive("stage.tmp", b"new", max_bytes=1024)
            expected = authority.read("target.txt", 1024)
            with self.assertRaisesRegex(WorkspacePathError, "unsupported safely"):
                authority.replace_from_exact(
                    "stage.tmp", "target.txt",
                    staged_sha256=staged.sha256, staged_size=staged.size,
                    expected_target=expected, max_bytes=1024,
                )
            self.assertEqual(authority.read("target.txt", 1024), expected)
            self.assertEqual(authority.read("stage.tmp", 1024), staged)

    def test_exact_hash_size_unlink_refuses_mismatch(self) -> None:
        body = b"staged apply bytes"
        digest = hashlib.sha256(body).hexdigest()
        with WorkspacePathAuthority(self.workspace) as authority:
            authority.atomic_replace(".kaizen-stage.tmp", body, expected=None, max_bytes=1024)
            with self.assertRaises(WorkspacePathError):
                authority.unlink_exact(
                    ".kaizen-stage.tmp", sha256="0" * 64, size=len(body), max_bytes=1024,
                )
            self.assertIsNotNone(authority.identity(".kaizen-stage.tmp"))
            with self.assertRaises(WorkspacePathError):
                authority.unlink_exact(
                    ".kaizen-stage.tmp", sha256=digest, size=len(body) - 1, max_bytes=1024,
                )
            self.assertTrue(authority.unlink_exact(
                ".kaizen-stage.tmp", sha256=digest, size=len(body), max_bytes=1024,
            ))
            self.assertFalse(authority.unlink_exact(
                ".kaizen-stage.tmp", sha256=digest, size=len(body), max_bytes=1024,
            ))

    def test_absolute_traversal_and_windows_stream_paths_are_rejected(self) -> None:
        with WorkspacePathAuthority(self.workspace) as authority:
            for value in ("../outside", ".", str(self.root / "outside")):
                with self.subTest(value=value), self.assertRaises(WorkspacePathError):
                    authority.identity(value)
            if os.name == "nt":
                for value in (
                    "file.txt:stream", "trailing. ", "trailing.",
                    "CON", "nul.txt", "PRN.log", "AUX", "COM1.bin", "lpt9.txt",
                    "CONIN$", "conout$.txt", "CLOCK$", "COM¹.log", "LPT³",
                ):
                    with self.subTest(value=value), self.assertRaises(WorkspacePathError):
                        authority.identity(value)

    def test_real_redirected_component_and_final_link_are_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        redirect = self.workspace / "redirect"
        final_link = self.workspace / "final-link.txt"
        try:
            os.symlink(outside, redirect, target_is_directory=True)
            os.symlink(outside / "secret.txt", final_link, target_is_directory=False)
        except OSError as error:
            self.skipTest(f"real reparse/symlink fixture unavailable: {error}")
        with WorkspacePathAuthority(self.workspace) as authority:
            with self.assertRaises(WorkspacePathError):
                authority.read("redirect/secret.txt", 1024)
            with self.assertRaises(WorkspacePathError):
                authority.read("final-link.txt", 1024)

    def test_redirected_workspace_root_is_rejected_when_fixture_is_supported(self) -> None:
        real = self.root / "real-workspace"
        real.mkdir()
        redirected = self.root / "redirected-workspace"
        try:
            os.symlink(real, redirected, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"real reparse/symlink fixture unavailable: {error}")
        with self.assertRaises(WorkspacePathError):
            WorkspacePathAuthority(redirected)

    def test_root_ancestor_swap_cannot_redirect_an_operation(self) -> None:
        target = self.workspace / "target.txt"
        target.write_bytes(b"original")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "target.txt").write_bytes(b"outside")
        moved = self.root / "moved-workspace"
        with WorkspacePathAuthority(self.workspace) as authority:
            expected = authority.identity("target.txt")
            self.assertIsNotNone(expected)
            expected_read = authority.read("target.txt", 1024)
            if os.name == "nt":
                with self.assertRaises(OSError):
                    os.rename(self.workspace, moved)
                authority.atomic_replace("target.txt", b"inside", expected=expected_read, max_bytes=1024)
                self.assertEqual(target.read_bytes(), b"inside")
            else:
                os.rename(self.workspace, moved)
                os.symlink(outside, self.workspace, target_is_directory=True)
                authority.atomic_replace("target.txt", b"inside", expected=expected_read, max_bytes=1024)
                self.assertEqual((moved / "target.txt").read_bytes(), b"inside")
            self.assertEqual((outside / "target.txt").read_bytes(), b"outside")

    @unittest.skipUnless(os.name == "nt", "Windows exact-handle operations")
    def test_windows_replace_and_unlink_do_not_fall_back_to_path_mutators(self) -> None:
        body = b"handle-owned"
        digest = hashlib.sha256(body).hexdigest()
        with WorkspacePathAuthority(self.workspace) as authority, \
                mock.patch.object(authority_module.os, "replace", side_effect=AssertionError("path replace")), \
                mock.patch.object(authority_module.os, "unlink", side_effect=AssertionError("path unlink")):
            staged = authority.create_exact_exclusive("stage.bin", body, max_bytes=1024)
            identity = authority.replace_from_exact(
                "stage.bin", "target.bin",
                staged_sha256=staged.sha256, staged_size=staged.size,
                expected_target=None, max_bytes=1024,
            )
            self.assertEqual(identity, authority.identity("target.bin"))
            self.assertTrue(authority.unlink_exact(
                "target.bin", sha256=digest, size=len(body), max_bytes=1024,
            ))


if __name__ == "__main__":
    unittest.main()
