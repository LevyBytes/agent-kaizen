"""Child ownership lab (v8 M1, exit criterion (2), v7 open probe #5).

The Job-Object grandchild-reaping lab: spawn a python child that spawns a grandchild,
kill the owner (close the Job Object handle on Windows / signal the process group on
POSIX), assert BOTH processes are gone. The Job Object is an optimization; the boot
orphan-sweep (test_supervisor.py) is the invariant. This test proves the optimization
actually reaps the tree.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from _harness import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT))

from kaizen_components.orchestration import children  # noqa: E402

# The child writes the grandchild's pid to a file, then both sleep long enough that the
# reap must be what ends them (not a natural exit). stdlib only; UTF-8 file I/O.
_CHILD_SRC = r"""
import subprocess, sys, time
pidfile = sys.argv[1]
gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(pidfile, "w", encoding="utf-8").write(str(gc.pid))
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    return children.pid_alive(pid)


class _FakeProcess:
    """A subprocess.Popen test double modeling poll/wait/kill with a dies_on_kill switch (kill sets returncode -9)."""
    def __init__(self, *, dies_on_kill: bool) -> None:
        self.pid = 424242
        self.returncode: int | None = None
        self.dies_on_kill = dies_on_kill
        self.wait_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float) -> int:
        """Raises subprocess.TimeoutExpired while returncode is None, mirroring real Popen.wait(timeout)."""
        self.wait_calls += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-child", timeout)
        return self.returncode

    def kill(self) -> None:
        self.kill_calls += 1
        if self.dies_on_kill:
            self.returncode = -9


class _DelayedDeathProcess(_FakeProcess):
    """Model asynchronous kill: the process exits only when the post-kill wait observes it."""

    def __init__(self) -> None:
        super().__init__(dies_on_kill=False)

    def wait(self, timeout: float) -> int:
        self.wait_calls += 1
        if self.kill_calls:
            self.returncode = -9
            return self.returncode
        raise subprocess.TimeoutExpired("fake-child", timeout)


class TerminationProofTest(unittest.TestCase):
    def test_forced_termination_waits_again_and_raises_if_child_remains_alive(self) -> None:
        process = _FakeProcess(dies_on_kill=False)
        with mock.patch.object(children, "IS_WINDOWS", True):
            child = children.OwnedChild(process, None)
            with self.assertRaises(children.ChildTerminationError):
                child.kill_tree(timeout=0.01)
        self.assertEqual(process.wait_calls, 2)
        self.assertEqual(process.kill_calls, 1)

    def test_forced_termination_returns_only_after_second_wait_proves_exit(self) -> None:
        process = _FakeProcess(dies_on_kill=True)
        with mock.patch.object(children, "IS_WINDOWS", True):
            child = children.OwnedChild(process, None)
            child.kill_tree(timeout=0.01)
        self.assertEqual(process.wait_calls, 2)
        self.assertEqual(process.kill_calls, 1)
        self.assertIsNotNone(process.poll())

    def test_forced_termination_handles_asynchronous_post_kill_exit(self) -> None:
        process = _DelayedDeathProcess()
        with mock.patch.object(children, "IS_WINDOWS", True):
            child = children.OwnedChild(process, None)
            child.kill_tree(timeout=0.01)
        self.assertEqual(process.wait_calls, 2)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.poll(), -9)


@unittest.skipUnless(children.IS_WINDOWS, "Windows Job Object rollback")
class WindowsSpawnRollbackTest(unittest.TestCase):
    def test_failed_spawn_cleanup_kills_waits_and_closes_job(self) -> None:
        process = _FakeProcess(dies_on_kill=True)
        job = 123
        with mock.patch.object(children._kernel32, "CloseHandle", return_value=True) as close:
            children._cleanup_failed_windows_spawn(process, job, timeout=0.01)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.wait_calls, 1)
        self.assertIsNotNone(process.poll())
        close.assert_called_once_with(job)

    def test_assignment_or_resume_failure_rolls_back_before_reraising(self) -> None:
        for stage in ("assign", "resume"):
            with self.subTest(stage=stage):
                process = _FakeProcess(dies_on_kill=True)
                job = 456
                assign_error = OSError("fixture assignment failure") if stage == "assign" else None
                resume_error = OSError("fixture resume failure") if stage == "resume" else None
                with mock.patch.object(children, "_create_kill_on_close_job", return_value=job), \
                     mock.patch.object(children.subprocess, "Popen", return_value=process), \
                     mock.patch.object(children, "_assign_to_job", side_effect=assign_error) as assign, \
                     mock.patch.object(children, "_resume_main_thread", side_effect=resume_error) as resume, \
                     mock.patch.object(children, "_cleanup_failed_windows_spawn") as cleanup:
                    with self.assertRaises(OSError):
                        children._spawn_windows(["fixture"], None, {}, None, None, None)
                assign.assert_called_once_with(job, process.pid)
                if stage == "assign":
                    resume.assert_not_called()
                else:
                    resume.assert_called_once_with(process)
                cleanup.assert_called_once_with(process, job)


class GrandchildReapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = REPO_ROOT / "AI" / "work" / f"child-lab-{id(self)}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.pidfile = self.tmp / "grandchild.pid"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wait_for_pidfile(self, timeout: float = 15.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.pidfile.is_file():
                text = self.pidfile.read_text(encoding="utf-8").strip()
                if text:
                    return int(text)
            time.sleep(0.05)
        self.fail("grandchild pid never reported")

    def test_child_and_grandchild_reaped_on_owner_close(self) -> None:
        child = children.spawn_owned(
            [sys.executable, "-c", _CHILD_SRC, str(self.pidfile)]
        )
        self.addCleanup(lambda: child.kill_tree(timeout=1.0) if child.process.poll() is None else None)
        grandchild_pid = self._wait_for_pidfile()
        child_pid = child.pid
        self.assertTrue(_alive(child_pid), "child should be alive before reap")
        self.assertTrue(_alive(grandchild_pid), "grandchild should be alive before reap")
        self.assertTrue(child.owns_pid(grandchild_pid), "grandchild must belong to the retained owner tree")
        self.assertFalse(child.owns_pid(os.getpid()), "unrelated process must fail the ownership proof")

        child.kill_tree(timeout=10.0)
        for stream in (child.process.stdout, child.process.stderr):
            if stream is not None:
                stream.close()

        # Give the kernel a moment to tear down the tree.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and (_alive(child_pid) or _alive(grandchild_pid)):
            time.sleep(0.1)
        self.assertFalse(_alive(child_pid), "child must be reaped")
        self.assertFalse(_alive(grandchild_pid), "grandchild must be reaped (Job Object / process group)")


class PidAliveTest(unittest.TestCase):
    def test_current_process_is_alive(self) -> None:
        self.assertTrue(children.pid_alive(os.getpid()))

    def test_impossible_pid_is_dead(self) -> None:
        self.assertFalse(children.pid_alive(0))
        self.assertFalse(children.pid_alive(999999))

    @unittest.skipUnless(children.IS_WINDOWS, "Win32 exit-code ambiguity regression")
    def test_process_exited_with_still_active_code_is_dead(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import os; os._exit(259)"])
        process.wait(timeout=10)
        self.assertEqual(process.returncode, 259)
        self.assertFalse(children.pid_alive(process.pid))


if __name__ == "__main__":
    unittest.main()
