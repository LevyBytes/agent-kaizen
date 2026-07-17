"""Driven-conversation lifecycle and approval rendezvous (H2.2).

The M6 LocalLLMAdapter owns the model loop but is otherwise passive -- it emits recorder events and
parks on an on_approval callback. This module is the daemon-side glue that makes a *driven* session:
a C1 session + T5 agent-run envelope, an adapter whose recorder bridges to the supervisor's T6 funnel,
a background thread per turn, and a per-correlation approval waiter the daemon releases when a client
approves over the loopback. One C1/T5 remains open across ``running -> idle`` turn cycles. Only explicit
close (success) or a fatal/kill/shutdown path finalizes T8.

The waiter is the crux of the fail-closed approval contract. The adapter emits ``approval open`` (with
the policy decision's ``correlation_hash``) before ``record_ask`` persists the C4 row,
so an ``approval_id``-keyed client races the DB write. The client therefore approves by the RACE-FREE
``correlation_id`` from the stream event; :class:`_ApprovalWaiter` parks the adapter's approval thread
per ``correlation_hash`` with NO waiter-side timeout -- the adapter's ``approval_timeout`` is the sole
clock, so the fail-closed guarantee (a stuck approval denies) is preserved end to end.

STDOUT stays pristine: this module never prints; the supervisor's logger is injected. It writes NO DB
rows itself -- the adapter emits recorder events that the supervisor funnels to T5/T6/T8, and the
lifecycle finalize goes through the supervisor's T5 handlers. Stdlib only.
"""

from __future__ import annotations

import threading
import json
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .adapters import Adapter, TurnResult

# Driven default approval timeout (seconds). ``LocalLlmAdapter.__init__`` defaults to 30.0;
# a driven session OVERRIDES it to 300.0 so a human at the webview has a realistic window. Per-call
# session/start ``approval_timeout`` overrides this again. This is the SOLE approval clock (the waiter
# adds none) so the fail-closed contract has exactly one deadline.
DRIVEN_APPROVAL_TIMEOUT_DEFAULT = 300.0

# session/events long-poll ceiling (seconds). A waiter is capped at min(request.wait, this) so a hostile
# or buggy client cannot pin an accept-loop thread indefinitely; the webview re-polls.
SESSION_LONG_POLL_MAX_S = 25.0

# Ephemeral streamed text is bounded independently from durable T6 events. Small provider fragments
# stay intact; an unexpectedly large fragment is split before admission so one delta can always fit
# inside the 256 KiB response batch.
DELTA_RING_MAX_CHUNKS = 2_048
DELTA_RING_MAX_BYTES = 2 * 1024 * 1024
DELTA_BATCH_MAX_BYTES = 256 * 1024
DELTA_FRAGMENT_MAX_BYTES = 32 * 1024

TURN_OPEN = "open"
TURN_RUNNING = "running"
TURN_IDLE = "idle"
TURN_TERMINAL = "terminal"
TURN_STATES = (TURN_OPEN, TURN_RUNNING, TURN_IDLE, TURN_TERMINAL)


def _split_utf8(text: str, limit: int = DELTA_FRAGMENT_MAX_BYTES) -> list[str]:
    """Split text on UTF-8 boundaries without changing its content."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    raw = text.encode("utf-8")
    chunks: list[str] = []
    while raw:
        end = min(len(raw), limit)
        while True:
            try:
                chunks.append(raw[:end].decode("utf-8"))
                break
            except UnicodeDecodeError as error:
                end = error.start
                if end <= 0:  # pragma: no cover -- UTF-8 code points are at most four bytes
                    raise
        raw = raw[end:]
    return chunks


class DeltaRing:
    """Thread-safe, monotonic, non-consuming ring for ephemeral assistant text deltas."""

    def __init__(self) -> None:
        self._items: deque[tuple[dict[str, Any], int]] = deque()
        self._bytes = 0
        self._latest = 0
        self._dropped_through = 0
        self._lock = threading.RLock()

    @property
    def cursor(self) -> int:
        with self._lock:
            return self._latest

    def append(self, turn_id: str, text: str) -> int:
        """Append non-empty text, evicting oldest chunks to both hard ring limits."""

        if not text:
            return self.cursor
        with self._lock:
            for fragment in _split_utf8(text):
                self._latest += 1
                size = len(fragment.encode("utf-8"))
                item = {"seq": self._latest, "turn_id": turn_id, "text": fragment}
                self._items.append((item, size))
                self._bytes += size
                while len(self._items) > DELTA_RING_MAX_CHUNKS or self._bytes > DELTA_RING_MAX_BYTES:
                    evicted, evicted_size = self._items.popleft()
                    self._bytes -= evicted_size
                    self._dropped_through = int(evicted["seq"])
            return self._latest

    def read(self, since: int) -> tuple[list[dict[str, Any]], int, bool]:
        """Return one ordered <=256 KiB batch after ``since`` plus cursor/drop state."""

        with self._lock:
            latest = self._latest
            if since > latest:
                return [], latest, True  # cursor belongs to a prior daemon/ring
            dropped = since < self._dropped_through
            if dropped:
                return [], latest, True  # never render an incomplete retained suffix
            items = [dict(item) for item, _size in self._items if int(item["seq"]) > since]

        batch: list[dict[str, Any]] = []
        encoded_bytes = len(b'{"deltas":[') + len(b"]}")
        cursor = since
        for item in items:
            item_bytes = len(json.dumps(item, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
            candidate_bytes = encoded_bytes + item_bytes + (1 if batch else 0)
            if candidate_bytes > DELTA_BATCH_MAX_BYTES:
                break
            batch.append(item)
            encoded_bytes = candidate_bytes
            cursor = int(item["seq"])
        return batch, cursor if batch else latest, dropped


class WriterClaimBinding:
    """Thread-safe current writer claim shared by a preflight adapter and its driven session."""

    def __init__(self, claim_id: str | None = None) -> None:
        self._claim_id = claim_id
        self._lock = threading.RLock()

    def current(self) -> str | None:
        with self._lock:
            return self._claim_id

    def set(self, claim_id: str) -> None:
        with self._lock:
            self._claim_id = claim_id

    def clear(self, claim_id: str) -> bool:
        with self._lock:
            if self._claim_id != claim_id:
                return False
            self._claim_id = None
            return True


class _ApprovalWaiter:
    """A parked approval for one ``correlation_hash``. The adapter's approval thread blocks in
    :meth:`wait` (bounded by the adapter's approval_timeout, NOT here); the daemon's ``approve`` handler
    calls :meth:`release` with the decision string once the client decides by correlation_id. A single
    release wins (idempotent); a second decide is a no-op here (the C4 state machine refuses the DB
    re-decide with DENIED_APPROVAL_ALREADY_DECIDED, which the daemon surfaces)."""

    __slots__ = ("_event", "_decision", "_lock", "_released")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._decision: Any = None
        self._lock = threading.Lock()
        self._released = False

    def release(self, decision: Any) -> bool:
        """Deliver ``decision`` (``approved``/``denied``) to the parked adapter thread. Returns True iff
        THIS call did the release (a later duplicate returns False and changes nothing)."""
        with self._lock:
            if self._released:
                return False
            self._released = True
            self._decision = decision
        self._event.set()
        return True

    def deny(self) -> bool:
        """Release as denied (kill/shutdown teardown: parked waiters fail closed)."""
        from .adapters.local_llm import DECISION_DENIED

        return self.release(DECISION_DENIED)

    def wait(self, timeout: float | None) -> str:
        """Block until released or ``timeout`` (the adapter's approval_timeout) elapses. A timeout is a
        fail-closed deny -- identical to the adapter's own no-callback / timeout path."""
        from .adapters.local_llm import DECISION_DENIED

        decision, _timed_out = self.wait_result(timeout)
        value = getattr(decision, "decision", decision)
        return value if value in ("approved", "denied") else DECISION_DENIED

    def wait_result(self, timeout: float | None) -> tuple[Any, bool]:
        """Return the decision plus whether this wait consumed the sole timeout clock."""

        from .adapters.local_llm import DECISION_DENIED

        if self._event.wait(timeout):
            return self._decision if self._decision is not None else DECISION_DENIED, False
        # Close release-vs-timeout atomically. If release won after Event.wait returned, preserve it;
        # otherwise mark the waiter expired so a later approval can be stashed for reconciliation.
        with self._lock:
            if self._released:
                return self._decision if self._decision is not None else DECISION_DENIED, False
            self._released = True
            self._decision = DECISION_DENIED
        return DECISION_DENIED, True


@dataclass(frozen=True)
class TurnReservation:
    """Request-owned, reversible prelaunch transition into ``running``.

    The reservation is deliberately separate from the writer claim. A request must first reserve the
    session, then acquire its own claim; it can never borrow a token left by another request. Until the
    caller commits, every mutated lifecycle field can be restored exactly on a prelaunch denial.
    """

    reservation_id: str
    prior_state: str
    prior_result: "TurnResult | None"
    prior_turn_done: bool
    prior_terminal: bool
    prior_delta_turn_id: str | None


@dataclass
class DrivenSession:
    """One daemon-driven conversation. Owns the adapter, current turn thread, and approval
    waiters. ``waiters`` maps ``correlation_hash`` -> :class:`_ApprovalWaiter`; the on_approval callback
    installs one per ask and the daemon's approve/kill paths resolve them. ``state`` is the authoritative
    in-memory ``open|running|idle|terminal`` lifecycle; the durable reducer remains authoritative for T8.
    ``lock`` makes concurrent turn/close/kill requests deterministic."""

    session_id: str
    agent_run_id: str
    adapter: "Adapter"
    engine: str
    approval_timeout: float
    permission_mode: str = "plan"
    diff_snapshots: bool = False
    image_attachments: bool = False
    governed_context: bool = False
    streaming: bool = False
    policy_snapshot: Any = None
    thread: threading.Thread | None = None
    waiters: dict[str, _ApprovalWaiter] = field(default_factory=dict)
    approval_ids: dict[str, str] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    state: str = TURN_OPEN
    turn_done: threading.Event = field(default_factory=threading.Event)
    terminal: threading.Event = field(default_factory=threading.Event)
    result: "TurnResult | None" = None
    _writer_claim_token: str | None = None
    writer_claim_binding: WriterClaimBinding = field(default_factory=WriterClaimBinding)
    delta_ring: DeltaRing = field(default_factory=DeltaRing)
    _active_delta_turn_id: str | None = None
    _turn_reservation_id: str | None = None
    # On-park approval re-check (the race fix). The adapter emits approval/open then parks the waiter
    # slightly later; an approve landing in that gap would strand the waiter until approval_timeout.
    # This callable (session_id, correlation) -> the adapter decision string when the C4 row is ALREADY
    # decided, else None. The supervisor injects a connect-per-call DB read; DB errors return None so the
    # ask path never crashes and falls through to normal parking (fail-closed clock unchanged).
    approval_recheck: Callable[[str, str], str | None] | None = None

    # --- conversation lifecycle ---------------------------------------------------------------

    @property
    def turn_state(self) -> str:
        with self.lock:
            return self.state

    def begin_turn(self) -> str | None:
        """Atomically transition open/idle -> running. Return a protocol denial code on refusal."""

        reservation, denial = self.reserve_turn()
        if denial is not None:
            return denial
        assert reservation is not None
        if not self.commit_turn(reservation):  # pragma: no cover - same-thread invariant
            raise RuntimeError("driven turn reservation disappeared before commit")
        return None

    def reserve_turn(self) -> tuple[TurnReservation | None, str | None]:
        """Atomically reserve open/idle state for one request without committing the turn."""

        with self.lock:
            if self.state == TURN_TERMINAL:
                return None, "DENIED_SESSION_TERMINAL"
            if self.state == TURN_RUNNING:
                return None, "DENIED_TURN_IN_PROGRESS"
            if self.state not in (TURN_OPEN, TURN_IDLE):
                return None, "DENIED_SESSION_NOT_IDLE"
            reservation = TurnReservation(
                reservation_id=uuid.uuid4().hex,
                prior_state=self.state,
                prior_result=self.result,
                prior_turn_done=self.turn_done.is_set(),
                prior_terminal=self.terminal.is_set(),
                prior_delta_turn_id=self._active_delta_turn_id,
            )
            self.state = TURN_RUNNING
            self.result = None
            self._active_delta_turn_id = None
            self._turn_reservation_id = reservation.reservation_id
            self.turn_done.clear()
            self.terminal.clear()
            return reservation, None

    def commit_turn(self, reservation: TurnReservation) -> bool:
        """Commit the request-owned reservation immediately before durable/provider work."""

        with self.lock:
            if self._turn_reservation_id != reservation.reservation_id or self.state != TURN_RUNNING:
                return False
            self._turn_reservation_id = None
            return True

    def rollback_turn(self, reservation: TurnReservation) -> bool:
        """Restore the exact pre-reservation lifecycle after a prelaunch denial."""

        with self.lock:
            if self._turn_reservation_id != reservation.reservation_id or self.state != TURN_RUNNING:
                return False
            self.state = reservation.prior_state
            self.result = reservation.prior_result
            self._active_delta_turn_id = reservation.prior_delta_turn_id
            self._turn_reservation_id = None
            if reservation.prior_turn_done:
                self.turn_done.set()
            else:
                self.turn_done.clear()
            if reservation.prior_terminal:
                self.terminal.set()
            else:
                self.terminal.clear()
            return True

    def finish_turn(self, result: "TurnResult") -> None:
        """Publish a turn result and return a healthy conversation to idle."""

        with self.lock:
            if self.state != TURN_TERMINAL:
                self.result = result
                self._turn_reservation_id = None
                self.state = TURN_TERMINAL if result.fatal else TURN_IDLE
            terminal = self.state == TURN_TERMINAL
        self.turn_done.set()
        if terminal:
            self.terminal.set()

    def append_delta(self, turn_id: str, text: str) -> int:
        """Admit one normalized provider delta only while this live session is streaming a turn."""

        with self.lock:
            if not self.streaming or self.state != TURN_RUNNING or not text:
                return self.delta_ring.cursor
            if self._active_delta_turn_id is None:
                self._active_delta_turn_id = turn_id
            elif self._active_delta_turn_id != turn_id:
                return self.delta_ring.cursor  # stale or cross-turn callback: ignore fail-closed
            return self.delta_ring.append(turn_id, text)

    def read_deltas(self, since: int) -> tuple[list[dict[str, Any]], int, bool]:
        """Return the bounded delta batch, next cursor, and dropped-history flag."""
        return self.delta_ring.read(since)

    def complete_delta_turn(self) -> str | None:
        """Detach the ephemeral turn key used to correlate its durable final replacement."""

        with self.lock:
            turn_id = self._active_delta_turn_id
            self._active_delta_turn_id = None
            return turn_id

    def begin_close(self) -> str | None:
        """Atomically reserve the idle-only explicit-close path."""

        with self.lock:
            if self.state == TURN_TERMINAL:
                return "DENIED_SESSION_TERMINAL"
            if self.state == TURN_RUNNING:
                return "DENIED_SESSION_NOT_IDLE"
            if self.state not in (TURN_OPEN, TURN_IDLE):
                return "DENIED_SESSION_NOT_IDLE"
            self.state = TURN_TERMINAL
        self.turn_done.set()
        self.terminal.set()
        return None

    def mark_terminal(self) -> None:
        """Force the in-memory lifecycle terminal (kill/shutdown/fatal cleanup)."""

        with self.lock:
            self.state = TURN_TERMINAL
            self._turn_reservation_id = None
        self.turn_done.set()
        self.terminal.set()

    @property
    def writer_claim_token(self) -> str | None:
        with self.lock:
            return self._writer_claim_token

    def set_writer_claim(self, claim_id: str) -> None:
        """Bind ``claim_id`` idempotently; reject a conflicting live claim."""
        with self.lock:
            if self._writer_claim_token not in (None, claim_id):
                raise RuntimeError("driven session already owns a different writer claim")
            self._writer_claim_token = claim_id
            self.writer_claim_binding.set(claim_id)

    def clear_writer_claim(self, claim_id: str) -> bool:
        with self.lock:
            if self._writer_claim_token != claim_id:
                return False
            if not self.writer_claim_binding.clear(claim_id):
                return False
            self._writer_claim_token = None
            return True

    # --- waiter bookkeeping (the fail-closed approval spine) -----------------------------------

    def park_waiter(self, correlation_hash: str) -> _ApprovalWaiter:
        """Install (or reuse) the waiter for ``correlation_hash`` and return it. Reuse means a re-decided
        identical action (same policy correlation) parks on the SAME waiter -- so a client approve that
        raced an adapter re-ask still lands."""
        with self.lock:
            waiter = self.waiters.get(correlation_hash)
            if waiter is None:
                waiter = _ApprovalWaiter()
                self.waiters[correlation_hash] = waiter
            return waiter

    def resolve_waiter(self, correlation_hash: str, decision: Any) -> bool:
        """Release the waiter for ``correlation_hash`` (the daemon approve path). Returns True iff a live
        waiter was released; False when none is parked (the client raced ahead of the adapter's ask, or
        the correlation is unknown -- the caller surfaces that)."""
        with self.lock:
            waiter = self.waiters.get(correlation_hash)
        if waiter is None:
            return False
        return waiter.release(decision)

    def bind_broker_approval(self, correlation_hash: str, approval_id: str) -> _ApprovalWaiter:
        """Bind a committed broker C4 to its waiter before the adapter is allowed to park."""

        with self.lock:
            waiter = self.waiters.get(correlation_hash)
            if waiter is None:
                waiter = _ApprovalWaiter()
                self.waiters[correlation_hash] = waiter
            self.approval_ids[correlation_hash] = approval_id
            return waiter

    def clear_broker_approval(self, correlation_hash: str, approval_id: str) -> None:
        """Clear only the matching live broker binding; stale clears are no-ops."""
        with self.lock:
            if self.approval_ids.get(correlation_hash) == approval_id:
                self.approval_ids.pop(correlation_hash, None)
                self.waiters.pop(correlation_hash, None)

    def live_approval_ids(self) -> set[str]:
        with self.lock:
            return set(self.approval_ids.values())

    def live_approval_id(self, correlation_hash: str) -> str | None:
        with self.lock:
            return self.approval_ids.get(correlation_hash)

    def deny_all_waiters(self) -> int:
        """Fail-closed release every parked waiter as denied (kill/shutdown). Returns the count released
        (only those not already resolved)."""
        with self.lock:
            waiters = list(self.waiters.values())
            self.waiters.clear()
            self.approval_ids.clear()
            return sum(1 for waiter in waiters if waiter.deny())

    def on_approval(self, request: dict[str, Any]) -> str:
        """The adapter's :meth:`LocalLlmAdapter.on_approval` callback. Parks per ``correlation_id``
        (== the policy decision correlation_hash the stream event carries) and blocks on the waiter,
        bounded by the adapter's approval_timeout -- the SOLE clock. A missing correlation_id cannot be
        resolved race-free, so it fails closed immediately.

        On-park re-check (race fix): AFTER parking (so a concurrent approve resolves it either via the
        recheck here or via the daemon's resolve_waiter), before blocking, ask ``approval_recheck`` whether
        the C4 row is already decided; if so, resolve the waiter with that decision instantly instead of
        stranding it until approval_timeout. The decision is consumed exactly once -- release() is
        idempotent, so a daemon resolve that raced this recheck is a no-op. A recheck exception falls
        through to normal parking (fail-closed clock preserved)."""
        from .adapters.local_llm import DECISION_DENIED

        correlation = request.get("correlation_id")
        if not correlation:
            return DECISION_DENIED  # no race-free handle -> fail closed
        waiter = self.park_waiter(str(correlation))
        if self.approval_recheck is not None:
            try:
                decided = self.approval_recheck(self.session_id, str(correlation))
            except Exception:  # noqa: BLE001 -- a recheck DB error must never crash the ask path
                decided = None
            if decided is not None:
                waiter.release(decided)  # idempotent: a daemon resolve that raced is a no-op
        try:
            return waiter.wait(self.approval_timeout)
        finally:
            with self.lock:
                if str(correlation) not in self.approval_ids and self.waiters.get(str(correlation)) is waiter:
                    self.waiters.pop(str(correlation), None)


def run_driven_turn(
    session: DrivenSession,
    adapter: "Adapter",
    prompt: str,
    *,
    finalize: Callable[[str, dict[str, Any], bool], None],
    record_assistant: Callable[["TurnResult"], "TurnResult"] | None = None,
    turn_finally: Callable[[DrivenSession, "TurnResult", bool], bool] | None = None,
    logger: Callable[[str], None],
) -> None:
    """Run one turn, returning healthy adapters to idle and terminalizing only fatal results.

    The caller has already atomically accepted the turn and persisted its user message. A complete
    successful assistant message is persisted through ``record_assistant``. Unexpected adapter errors
    are represented by a safe fatal result (exception text is neither logged nor persisted).
    """

    from .adapters import TurnResult, normalize_turn_result

    result = TurnResult(status="FAILED", error_code="ERROR_ADAPTER_TURN", fatal=True)
    try:
        run = getattr(adapter, "run_turn", None)
        raw = run(prompt) if callable(run) else adapter.start_turn(prompt)  # type: ignore[attr-defined]
        result = normalize_turn_result(raw)
        if result.status == "OK" and result.final_text is None:
            result = TurnResult(
                status="FAILED",
                vendor_turn_id=result.vendor_turn_id,
                error_code="ERROR_ADAPTER_RESULT",
                fatal=True,
            )
        elif result.status == "OK" and record_assistant is not None:
            result = record_assistant(result)
    except Exception as error:  # noqa: BLE001 -- a driven turn must always finalize terminally
        logger(f"driven turn {session.agent_run_id} crashed: {type(error).__name__}")
        result = TurnResult(status="FAILED", error_code="ERROR_ADAPTER_TURN", fatal=True)
    finally:
        termination_proven = True
        if result.fatal:
            try:
                killed = adapter.kill()
                termination_proven = (
                    isinstance(killed, dict) and killed.get("status") == "OK"
                    and killed.get("killed") is True
                    and killed.get("termination_proven") is not False
                )
            except Exception as error:  # noqa: BLE001 -- fatal cleanup continues to T8
                termination_proven = False
                logger(f"driven adapter {session.agent_run_id} kill failed: {type(error).__name__}")
        cleanup_ok = True
        if turn_finally is not None:
            try:
                cleanup_ok = turn_finally(session, result, termination_proven)
            except Exception as error:  # noqa: BLE001 -- cleanup uncertainty terminalizes fail-closed
                cleanup_ok = False
                logger(f"driven turn {session.agent_run_id} cleanup failed: {type(error).__name__}")
        if not cleanup_ok and not result.fatal:
            result = TurnResult(status="FAILED", error_code="DENIED_WORKSPACE_RECOVERY_REQUIRED", fatal=True)
            try:
                killed = adapter.kill()
                termination_proven = (
                    isinstance(killed, dict) and killed.get("status") == "OK"
                    and killed.get("killed") is True
                    and killed.get("termination_proven") is not False
                )
            except Exception as error:  # noqa: BLE001 -- recovery-required remains fail-closed
                termination_proven = False
                logger(f"driven adapter {session.agent_run_id} recovery kill failed: {type(error).__name__}")
        # Writer/C4 cleanup happens before this transition; a new turn can never observe idle early.
        session.finish_turn(result)
        if result.fatal:
            # Non-fatal CANCELED is an interrupt result and returns to idle above; only a fatal
            # cancellation closes the conversation with a canceled conclusion.
            conclusion = "canceled" if result.status == "CANCELED" else "failed"
            try:
                finalize(conclusion, result.as_dict(), termination_proven)
            except Exception as error:  # noqa: BLE001 -- finalize failure must not leave the thread hung
                logger(f"driven turn {session.agent_run_id} finalize failed: {type(error).__name__}")
