"""db_retry unit tests (in-process fakes) plus a concurrent-K1 initialization regression."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# db_retry.py imports only `time`/`typing` and `.denials` (no DB), so in-process import is safe.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kaizen_components import agent_runs, db, db_retry  # noqa: E402
from kaizen_components.db_retry import ATTEMPTS, TOTAL_SECONDS, is_retryable, retry_delay, rollback_quietly, with_retry  # noqa: E402
from kaizen_components.denials import KaizenDenied  # noqa: E402

from _harness import KAIZEN, REPO_ROOT, kaizen  # noqa: E402


class _FakeConn:
    """Minimal connection stand-in: records every execute() and close() into a shared log."""

    def __init__(self, log: list[str]) -> None:
        self._log = log

    def execute(self, sql: str, *params: object) -> None:
        self._log.append(sql)

    def close(self) -> None:
        self._log.append("<close>")


class IsRetryableTest(unittest.TestCase):
    def test_true_for_conflict_busy_snapshot(self):
        for message in ("write-write conflict", "database is busy", "stale snapshot", "DATABASE IS BUSY"):
            self.assertTrue(is_retryable(Exception(message)), message)

    def test_false_for_other_errors(self):
        for error in (Exception("no such table: gotcha"), ValueError("bad payload"), Exception("")):
            self.assertFalse(is_retryable(error), repr(error))


class RetryDelayTest(unittest.TestCase):
    def test_schedule_increases_and_sums_to_total_seconds(self):
        delays = [retry_delay(i) for i in range(ATTEMPTS - 1)]
        for earlier, later in zip(delays, delays[1:]):
            self.assertGreater(later, earlier, delays)
        self.assertAlmostEqual(sum(delays), TOTAL_SECONDS, delta=1e-9)
        # Weights are 1..ATTEMPTS-1, so the first backoff is TOTAL_SECONDS * 1 / sum(weights).
        weight_sum = sum(range(1, ATTEMPTS))
        self.assertAlmostEqual(delays[0], TOTAL_SECONDS / weight_sum, delta=1e-9)


class WithRetryTest(unittest.TestCase):
    def test_commits_on_first_success(self):
        log: list[str] = []
        attempts: list[int] = []
        sentinel = object()

        def operation(conn, attempt):
            attempts.append(attempt)
            return sentinel

        result = with_retry(lambda: _FakeConn(log), operation)
        self.assertIs(result, sentinel)
        self.assertEqual(attempts, [1])
        self.assertEqual(log, ["BEGIN CONCURRENT", "COMMIT", "<close>"])

    def test_reraises_non_retryable_immediately(self):
        log: list[str] = []
        attempts: list[int] = []

        def operation(conn, attempt):
            attempts.append(attempt)
            raise ValueError("schema violation, not retryable")

        with self.assertRaises(ValueError):
            with_retry(lambda: _FakeConn(log), operation)
        self.assertEqual(attempts, [1], "non-retryable error must not be retried")
        self.assertEqual(log, ["BEGIN CONCURRENT", "ROLLBACK", "<close>"])

    def test_retries_on_busy_then_succeeds_on_fresh_connection(self):
        log: list[str] = []
        connections: list[_FakeConn] = []
        attempts: list[int] = []

        def connect():
            conn = _FakeConn(log)
            connections.append(conn)
            return conn

        def operation(conn, attempt):
            attempts.append(attempt)
            if attempt == 1:
                raise Exception("database is busy")
            return "second-try"

        with mock.patch.object(db_retry.time, "sleep") as fake_sleep:
            result = with_retry(connect, operation)
        self.assertEqual(result, "second-try")
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(len(connections), 2, "each attempt must open a fresh connection")
        # First connection is rolled back AND closed before the backoff sleep runs.
        self.assertEqual(
            log,
            ["BEGIN CONCURRENT", "ROLLBACK", "<close>", "BEGIN CONCURRENT", "COMMIT", "<close>"],
        )
        fake_sleep.assert_called_once_with(retry_delay(0))

    def test_exhaustion_raises_denied_with_attempt_ledger(self):
        connections = 0

        def connect():
            nonlocal connections
            connections += 1
            return _FakeConn([])

        def operation(conn, attempt):
            raise Exception("database is busy")

        with mock.patch.object(db_retry.time, "sleep") as fake_sleep:
            with self.assertRaises(KaizenDenied) as ctx:
                with_retry(connect, operation)
        denied = ctx.exception
        self.assertEqual(denied.code, "DENIED_DB_WRITE_RETRY_EXHAUSTED")
        self.assertEqual(denied.fields["attempts"], ATTEMPTS)
        self.assertEqual(denied.fields["attempts"], 6)
        self.assertTrue(denied.fields["retryable"])
        self.assertEqual(denied.fields["reason"], "database is busy")
        self.assertEqual(denied.fields["errors"], ["database is busy"] * ATTEMPTS)
        self.assertEqual(connections, ATTEMPTS)
        # The final attempt raises instead of backing off, so only ATTEMPTS-1 sleeps happen.
        self.assertEqual(
            [call.args[0] for call in fake_sleep.call_args_list],
            [retry_delay(i) for i in range(ATTEMPTS - 1)],
        )


class RollbackQuietlyTest(unittest.TestCase):
    def test_swallows_exceptions_from_broken_connection(self):
        class ExplodingConn:
            def execute(self, sql):
                raise RuntimeError("connection already dead")

        rollback_quietly(ExplodingConn())  # must not raise

    def test_issues_rollback_on_working_connection(self):
        log: list[str] = []
        rollback_quietly(_FakeConn(log))
        self.assertEqual(log, ["ROLLBACK"])


class IntegrityScanCountTest(unittest.TestCase):
    def test_true_orphan_count_is_not_capped_by_sample(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("CREATE TABLE parent(id TEXT); CREATE TABLE child(parent_id TEXT);")
        conn.executemany("INSERT INTO child(parent_id) VALUES (?)", [(f"missing-{i}",) for i in range(9)])
        with mock.patch.object(db, "REFERENCES", [("child", "parent_id", "parent", "id")]), \
             mock.patch.object(db, "ensure_schema"), \
             mock.patch.object(db, "read_retry", side_effect=lambda op: op(conn)), \
             mock.patch.object(agent_runs, "find_child_leaks", return_value={"violations": 0}):
            result = db.integrity_scan()
        self.assertEqual(result["total_orphans"], 9)
        rel = result["orphaned_relationships"][0]
        self.assertEqual(rel["orphans"], 9)
        self.assertEqual(len(rel["sample"]), 5)
        self.assertTrue(rel["sample_truncated"])


class K1ConcurrencyRegressionTest(unittest.TestCase):
    """Four simultaneous K1 calls against one fresh shared data plane.

    initialize() uses INSERT OR IGNORE for the schema_version row, so racing K1s can no
    longer collide on the PRIMARY KEY, and is_retryable() now treats lock-contention
    errors (turso's Windows "Locking error: ... (os error 33)", SQLite "database is
    locked") as retryable, so connect() losers back off inside the 4s budget instead of
    denying on attempt 1. All four calls normally succeed; the assertion keeps a
    machine-tolerant envelope (a slow disk may still exhaust the budget): at least one
    winner reports schema_ok, any loser emits a clean structured
    DENIED_DB_CONNECT_RETRY_EXHAUSTED (no tracebacks, no UNIQUE/PK collision), and the
    surviving DB is healthy for a follow-up K1.
    """

    def test_simultaneous_k1_race_is_clean_and_db_survives(self):
        root = Path(tempfile.mkdtemp(prefix="kaizen-k1-race-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        env = dict(os.environ)
        env["KAIZEN_REPO_ROOT"] = str(root)
        procs = [
            subprocess.Popen(
                [sys.executable, str(KAIZEN), "K1", "--json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(REPO_ROOT),
                env=env,
            )
            for _ in range(4)
        ]
        outcomes = []
        drained: set[int] = set()
        try:
            for index, proc in enumerate(procs):
                try:
                    stdout, stderr = proc.communicate(timeout=120)
                except subprocess.TimeoutExpired:
                    self.fail(f"K1 #{index} did not exit within 120 seconds")
                drained.add(id(proc))
                outcomes.append((proc.returncode, stdout, stderr))
        finally:
            for proc in procs:
                if id(proc) not in drained:
                    if proc.poll() is None:
                        proc.kill()
                    proc.communicate(timeout=5)

        winners = 0
        for index, (rc, stdout, stderr) in enumerate(outcomes):
            combined = stdout + stderr
            self.assertNotIn("Traceback", combined, f"K1 #{index} crashed uncleanly: {combined!r}")
            # The INSERT OR IGNORE regression symptom: a schema_version PK collision.
            self.assertNotIn("UNIQUE", combined, f"K1 #{index} hit a PK collision: {combined!r}")
            rendered = stdout.strip() or stderr.strip()
            self.assertTrue(rendered, f"K1 #{index} exited {rc} without JSON; stdout={stdout!r}; stderr={stderr!r}")
            payload = json.loads(rendered)
            if rc == 0:
                winners += 1
                self.assertEqual(payload.get("status"), "OK", payload)
                self.assertTrue(payload.get("schema", {}).get("schema_ok"), payload)
            else:
                self.assertEqual(rc, 1, f"K1 #{index}: {payload}")
                self.assertEqual(payload.get("status"), "DENIED", payload)
                self.assertEqual(payload.get("code"), "DENIED_DB_CONNECT_RETRY_EXHAUSTED", payload)
        self.assertGreaterEqual(winners, 1, outcomes)

        # The data plane the winner initialized must remain fully usable: a sequential
        # follow-up K1 (the engine's own "retry the same command later" advice) is green.
        rc, payload = kaizen(root, "K1", timeout=60)
        self.assertEqual(rc, 0, payload)
        self.assertTrue(payload.get("schema", {}).get("schema_ok"), payload)


if __name__ == "__main__":
    unittest.main()
