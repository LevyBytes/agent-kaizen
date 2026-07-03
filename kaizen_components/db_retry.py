from __future__ import annotations

import time
from typing import Any, Callable

from .denials import KaizenDenied


ATTEMPTS = 6
TOTAL_SECONDS = 4.0


def is_retryable(error: BaseException) -> bool:
    # "lock" covers turso's Windows file-lock contention ("Locking error: Failed locking
    # file ... (os error 33)") and SQLite's "database is locked" — both transient under
    # concurrent processes and exactly what the backoff budget exists to absorb.
    msg = str(error).lower()
    return "conflict" in msg or "busy" in msg or "snapshot" in msg or "lock" in msg


def retry_delay(attempt_index: int) -> float:
    weights = [1 + i for i in range(ATTEMPTS - 1)]
    return TOTAL_SECONDS * weights[attempt_index] / sum(weights)


def rollback_quietly(conn: Any) -> None:
    try:
        conn.execute("ROLLBACK")
    except Exception:
        pass


def with_retry(connect: Callable[[], Any], operation: Callable[[Any, int], Any]) -> Any:
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
