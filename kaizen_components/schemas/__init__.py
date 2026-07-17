"""Schema-first output contracts for durable Kaizen records.

`validate_record(record_type, payload)` is the gate every durable write should
call before insert. The compact stdlib validator is authoritative; when the
optional `jsonschema` accelerator is installed it runs as an extra strict pass
(capability-activated, never required).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from ..denials import KaizenDenied
from .registry import KAIZEN_ENUMS, SCHEMAS, get_schema, list_schemas
from .validator import to_json_schema, validate_against_spec


def _jsonschema_module() -> Any | None:
    try:
        import jsonschema
    except Exception:
        return None
    return jsonschema


def jsonschema_available() -> bool:
    """Return whether the optional ``jsonschema`` accelerator imports; never raise."""
    return _jsonschema_module() is not None


@lru_cache(maxsize=None)
def _accelerated_schema(record_type: str) -> dict[str, Any]:
    """Project one registered compact schema once per record type."""
    return to_json_schema(get_schema(record_type))


def _accelerate(record_type: str, payload: dict[str, Any]) -> None:
    """Private extra-strict jsonschema pass; PRECONDITION that `validate_against_spec` already passed (parity depends on it); no-op when jsonschema absent; raises `KaizenDenied("DENIED_SCHEMA_JSONSCHEMA", exit_code=2)` on the first violation. Inline comment (31-33) covers only the None-drop rationale, not the no-op/raise contract."""
    jsonschema = _jsonschema_module()
    if jsonschema is None:
        return
    # The stdlib gate skips None-valued fields (None == omitted). Drop null keys here so the
    # accelerator makes the SAME accept/reject decision whether or not jsonschema is installed;
    # required fields are already non-None (validate_against_spec ran first).
    instance = {key: value for key, value in payload.items() if value is not None}
    try:
        jsonschema.validate(instance=instance, schema=_accelerated_schema(record_type))
    except (jsonschema.ValidationError, jsonschema.SchemaError) as error:  # type: ignore[attr-defined]
        raise KaizenDenied(
            "DENIED_SCHEMA_JSONSCHEMA",
            {
                "record_type": record_type,
                "field": ".".join(str(p) for p in error.absolute_path) or "(root)",
                "message": error.message,
                "required_action": "fix the payload to satisfy the record schema",
            },
            exit_code=2,
        ) from error


def has_schema(record_type: str) -> bool:
    """True when a schema is registered for ``record_type`` (so it can be gated)."""
    return record_type in SCHEMAS


def validate_record(record_type: str, payload: dict[str, Any], *, accelerate: bool = True) -> dict[str, Any]:
    """Validate a durable-record payload against its named schema.

    Raises :class:`KaizenDenied` on the first violation; returns the payload on
    success. Set ``accelerate=False`` to skip the optional jsonschema pass.
    """
    spec = get_schema(record_type)
    validate_against_spec(payload, spec, record_type=record_type)
    if accelerate:
        _accelerate(record_type, payload)
    return payload


__all__ = [
    "KAIZEN_ENUMS",
    "SCHEMAS",
    "get_schema",
    "has_schema",
    "list_schemas",
    "validate_record",
    "validate_against_spec",
    "to_json_schema",
    "jsonschema_available",
]
