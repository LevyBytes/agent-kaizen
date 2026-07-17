"""Explicit engine-adapter registry and the frozen H2 turn contract.

This is deliberately a three-entry constructor map, not a plugin framework. Public ``claude`` is
normalized to the existing internal ``claude_cli`` lane at this boundary; imports stay lazy so asking
for daemon help or local-LLM capabilities never imports vendor process adapters.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable


DENIED_WORKSPACE_RECOVERY_REQUIRED = "DENIED_WORKSPACE_RECOVERY_REQUIRED"

MutationGuard = Callable[[Any], Mapping[str, Any] | None]


@dataclass(frozen=True)
class BrokerApprovalResult:
    """Typed decision; approved results may carry exact updated_input, a post_apply proof callback, approval revision/snapshot identity, and immutable approved bases, while denial normalization strips all optional data."""

    decision: str
    updated_input: Mapping[str, Any] | None = None
    post_apply: Callable[[], Mapping[str, Any]] | None = None
    approval_revision: int | None = None
    snapshot_set_sha256: str | None = None
    approved_bases: tuple[Mapping[str, Any], ...] | None = None


ApprovalBrokerCallback = Callable[[Mapping[str, Any]], str | BrokerApprovalResult]
_WORKSPACE_MUTATION_VERBS = frozenset({"file_write", "exec", "git", "spawn", "tool"})


def mutation_guard_required(action: Any) -> bool:
    """Return whether an approved action can mutate the workspace or launch an opaque mutator."""

    return getattr(action, "verb", None) in _WORKSPACE_MUTATION_VERBS


def check_mutation_guard(callback: MutationGuard | None, action: Any) -> dict[str, Any] | None:
    """Run the optional final mutation gate and normalize failures to a safe workspace denial.

    The policy engine and any human approval have already allowed ``action`` when this helper is
    reached. ``None`` means the action may proceed. A guard exception or malformed response fails
    closed without exposing exception text; valid denials must use the workspace denial namespace.
    """

    if callback is None:
        return None
    try:
        result = callback(action)
    except Exception:  # noqa: BLE001 -- a lease/guard defect must fail closed without leaking details
        result = False
    if result is None:
        return None
    if isinstance(result, Mapping):
        denial = dict(result)
        code = denial.get("code")
        if (
            denial.get("status") == "DENIED"
            and isinstance(code, str)
            and code.startswith("DENIED_WORKSPACE_")
        ):
            denial.setdefault("retryable", False)
            denial.setdefault(
                "required_action",
                "reconcile the workspace writer state before retrying the mutation",
            )
            return denial
    return {
        "status": "DENIED",
        "code": DENIED_WORKSPACE_RECOVERY_REQUIRED,
        "retryable": False,
        "required_action": "reconcile the workspace writer state before retrying the mutation",
    }


def resolve_broker_approval(
    callback: ApprovalBrokerCallback,
    request: Mapping[str, Any],
) -> str:
    """Synchronously delegate one ASK; exceptions and malformed results deny fail-closed."""

    return resolve_broker_result(callback, request).decision


def resolve_broker_result(
    callback: ApprovalBrokerCallback,
    request: Mapping[str, Any],
) -> BrokerApprovalResult:
    """Normalize legacy strings or an exact typed result; callback errors and every other shape deny."""

    try:
        result = callback(dict(request))
    except Exception:  # noqa: BLE001 -- the durable broker seam is fail-closed
        return BrokerApprovalResult("denied")
    # Typed results preserve negotiated input/evidence only on an exact approval; every other typed
    # decision is replaced with a data-free denial so an unknown vocabulary cannot smuggle fields.
    if isinstance(result, BrokerApprovalResult) and result.decision in ("approved", "denied"):
        return result if result.decision == "approved" else BrokerApprovalResult("denied")
    return BrokerApprovalResult("approved" if result == "approved" else "denied")


@dataclass(frozen=True)
class TurnResult:
    """Wire-neutral result of one adapter turn.

    ``status`` uses the existing adapter vocabulary (``OK``, ``FAILED``, ``CANCELED``). ``fatal`` says
    whether the logical conversation can safely accept another turn; it, not the status string alone,
    controls idle-versus-terminal lifecycle behavior.
    """

    status: str
    vendor_turn_id: str | None = None
    final_text: str | None = None
    error_code: str | None = None
    fatal: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return the wire-neutral mapping for this immutable result."""
        return {
            "status": self.status,
            "vendor_turn_id": self.vendor_turn_id,
            "final_text": self.final_text,
            "error_code": self.error_code,
            "fatal": self.fatal,
        }


def normalize_turn_result(value: TurnResult | Mapping[str, Any]) -> TurnResult:
    """Normalize a frozen result or a temporary legacy adapter result mapping.

    H2 adapters return :class:`TurnResult`. Mapping support is a bounded compatibility bridge for the
    pre-H2 ``start_turn`` test seam (``turn_id``/``final``/``code``); malformed legacy results fail
    closed as fatal rather than leaving a run falsely idle.
    """

    if isinstance(value, TurnResult):
        return value
    if not isinstance(value, Mapping):
        return TurnResult(status="FAILED", error_code="ERROR_ADAPTER_RESULT", fatal=True)
    raw_status = value.get("status")
    status = str(raw_status).upper() if raw_status else "FAILED"
    vendor_turn_id = value.get("vendor_turn_id", value.get("turn_id"))
    final_text = value.get("final_text", value.get("final"))
    error_code = value.get("error_code", value.get("code"))
    # Legacy mappings did not carry fatal. Conservatively terminalize an unknown/failed legacy result;
    # every H2 adapter specifies fatal explicitly.
    raw_fatal = value.get("fatal")
    fatal = status not in ("OK", "CANCELED") if raw_fatal is None else bool(raw_fatal)
    return TurnResult(
        status=status,
        vendor_turn_id=str(vendor_turn_id) if vendor_turn_id is not None else None,
        final_text=str(final_text) if final_text is not None else None,
        error_code=str(error_code) if error_code is not None else None,
        fatal=fatal,
    )


@runtime_checkable
class Adapter(Protocol):
    """Common H2 lifecycle surface implemented by all three driven adapters."""

    @property
    def active_turn_id(self) -> str | None: ...

    def open(self, profile: Mapping[str, Any], policy_snapshot: Any) -> Mapping[str, Any]: ...

    def run_turn(self, prompt: str) -> TurnResult: ...

    def set_mutation_guard(self, callback: MutationGuard | None) -> None: ...

    def set_approval_broker(self, callback: ApprovalBrokerCallback | None) -> None: ...

    def steer(self, text: str) -> Mapping[str, Any]: ...

    def interrupt(self) -> Mapping[str, Any]: ...

    def close(self) -> Mapping[str, Any]: ...

    def kill(self) -> Mapping[str, Any]: ...


def _construct_local_llm(*args: Any, **kwargs: Any) -> Adapter:
    """Lazily construct the local-LLM adapter without importing vendor lanes."""
    from .local_llm import LocalLLMAdapter

    return LocalLLMAdapter(*args, **kwargs)


def _construct_codex(*args: Any, **kwargs: Any) -> Adapter:
    """Lazily construct the Codex adapter without importing unrelated vendor lanes."""
    from .codex import CodexAdapter

    return CodexAdapter(*args, **kwargs)


def _construct_claude(*args: Any, **kwargs: Any) -> Adapter:
    """Lazily construct the Claude adapter without importing unrelated vendor lanes."""
    from .claude_sdk import ClaudeSdkAdapter

    return ClaudeSdkAdapter(*args, **kwargs)


# Internal lane keys remain unchanged. Public ``claude`` is an input alias only.
ADAPTER_CONSTRUCTORS: Mapping[str, Callable[..., Adapter]] = MappingProxyType({
    "local_llm": _construct_local_llm,
    "codex": _construct_codex,
    "claude_cli": _construct_claude,
})

_ADAPTER_ALIASES = {"claude": "claude_cli"}


def get_adapter_constructor(engine: str) -> Callable[..., Adapter]:
    """Return the explicit constructor for a public or internal engine id, raising KeyError when unknown."""

    key = _ADAPTER_ALIASES.get(str(engine), str(engine))
    return ADAPTER_CONSTRUCTORS[key]


def create_adapter(engine: str, *args: Any, **kwargs: Any) -> Adapter:
    """Construct one adapter through the explicit map."""

    return get_adapter_constructor(engine)(*args, **kwargs)


__all__ = [
    "ADAPTER_CONSTRUCTORS",
    "Adapter",
    "ApprovalBrokerCallback",
    "BrokerApprovalResult",
    "DENIED_WORKSPACE_RECOVERY_REQUIRED",
    "MutationGuard",
    "TurnResult",
    "check_mutation_guard",
    "create_adapter",
    "get_adapter_constructor",
    "mutation_guard_required",
    "normalize_turn_result",
    "resolve_broker_approval",
    "resolve_broker_result",
]
