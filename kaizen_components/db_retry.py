"""Provide bounded retry helpers for transactional database writes and shared connection handling."""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from .denials import KaizenDenied


ATTEMPTS = 6
TOTAL_SECONDS = 4.0


def is_retryable(error: BaseException) -> bool:
    # A KaizenDenied is a deliberate, structured denial (validation, a completion gate, not-found) --
    # never a transient DB fault -- so it must propagate immediately even when raised INSIDE a
    # write_tx op. Short-circuit before the message heuristic below, whose substring match would
    # otherwise mis-catch a denial whose payload merely contains a trigger word (e.g. "blocking").
    """Returns False for KaizenDenied (deliberate denial), else True iff the lowercased message contains a transient-fault token (conflict/busy/snapshot/lock). Has inline comments but no docstring."""
    if isinstance(error, KaizenDenied):
        return False
    # "lock" covers turso's Windows file-lock contention ("Locking error: Failed locking
    # file ... (os error 33)") and SQLite's "database is locked" — both transient under
    # concurrent processes and exactly what the backoff budget exists to absorb.
    msg = str(error).lower()
    return re.search(r"\b(?:conflict|busy|snapshot|lock(?:ed|ing)?)\b", msg) is not None


def retry_delay(attempt_index: int) -> float:
    """Weighted (1..ATTEMPTS-1) backoff for `attempt_index` in [0, ATTEMPTS-2]; the schedule sums to TOTAL_SECONDS."""
    weights = [1 + i for i in range(ATTEMPTS - 1)]
    if not weights:
        return 0.0
    return TOTAL_SECONDS * weights[attempt_index] / sum(weights)


def rollback_quietly(conn: Any) -> None:
    """Best-effort ROLLBACK; swallows all exceptions (broken/closed conn)."""
    try:
        conn.execute("ROLLBACK")
    except Exception:
        pass


def with_retry(connect: Callable[[], Any], operation: Callable[[Any, int], Any]) -> Any:
    """Public contract: opens a FRESH conn per attempt via `connect`, runs `operation(conn, attempt)` inside BEGIN CONCURRENT/COMMIT, rolls back + closes on failure, retries only when `is_retryable`, sleeps `retry_delay` between non-final attempts, and on exhaustion (ATTEMPTS) raises `KaizenDenied("DENIED_DB_WRITE_RETRY_EXHAUSTED", ...)` chained from the last error. Note `operation` may run up to ATTEMPTS times and must be safe to re-run (attempt index is provided for idempotency). Currently documented only at the caller `db.py:write_tx`."""
    started = time.monotonic()
    errors: list[str] = []
    for attempt in range(1, ATTEMPTS + 1):
        conn = connect()
        try:
            conn.execute("BEGIN CONCURRENT")
            result = operation(conn, attempt)
            conn.execute("COMMIT")
            return result
        except Exception as error:
            rollback_quietly(conn)
            if not is_retryable(error):
                raise
            errors.append(str(error))
            if attempt == ATTEMPTS:
                raise KaizenDenied(
                    "DENIED_DB_WRITE_RETRY_EXHAUSTED",
                    {
                        "attempts": ATTEMPTS,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "reason": str(error),
                        "retryable": True,
                        "required_action": (
                            "standby for user or retry the same command later; do not rewrite schema"
                        ),
                        "errors": errors,
                    },
                ) from error
        finally:
            conn.close()
        # Connection is closed by the finally above before we back off, so the handle is
        # not held open across the sleep. Only reached on a retryable, non-final attempt.
        time.sleep(retry_delay(attempt - 1))

    raise AssertionError("unreachable retry loop exit")
