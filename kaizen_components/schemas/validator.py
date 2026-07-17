"""Dependency-light structural validator for durable Kaizen record payloads.

This is the authoritative gate: it enforces required fields, rejects invented
fields, checks field types/enums/ranges, and defers text-length checks to the
existing :mod:`kaizen_components.hashing` helpers so word/sentence limits have a
single source of truth. A JSON Schema accelerator (``jsonschema``) is optional
and layered on top in :mod:`kaizen_components.schemas` -- it never replaces this.
"""

from __future__ import annotations

from typing import Any

from ..denials import KaizenDenied
from ..hashing import validate_summary, validate_word_limit


_PY_TYPES: dict[str, type | tuple[type, ...]] = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "list": list,
    "dict": dict,
}


def _type_ok(value: Any, type_name: str) -> bool:
    """Accept six declared types or explicit ``any``; reject unknown names and bool-as-number."""
    if type_name == "any":
        return True
    expected = _PY_TYPES.get(type_name)
    if expected is None:
        return False
    if type_name in ("int", "float") and isinstance(value, bool):
        # bool is a subclass of int; a numeric field should not accept True/False.
        return False
    return isinstance(value, expected)


def validate_against_spec(
    payload: dict[str, Any], spec: dict[str, Any], *, record_type: str,
) -> dict[str, Any]:
    """Validate ``payload`` against a compact Kaizen schema ``spec``.

    Raises :class:`KaizenDenied` (exit code 2) on the first violation; returns the
    payload unchanged on success.

    ``required`` means present and neither ``None`` nor an empty string; falsy-but-valid
    values (``0``, ``False``, ``[]``) count as present.
    """
    if not isinstance(payload, dict):
        raise KaizenDenied(
            "DENIED_SCHEMA_TYPE",
            {
                "record_type": record_type,
                "reason": "payload must be a JSON object",
                "required_action": "resubmit the payload as a JSON object",
            },
            exit_code=2,
        )

    fields: dict[str, Any] = spec.get("fields", {})
    required: list[str] = spec.get("required", [])
    allow_extra: bool = spec.get("allow_extra", False)

    missing = [
        name for name in required
        if payload.get(name) is None or (isinstance(payload.get(name), str) and not payload.get(name).strip())
    ]
    if missing:
        raise KaizenDenied(
            "DENIED_SCHEMA_REQUIRED",
            {
                "record_type": record_type,
                "fields": missing,
                "required_action": f"resubmit with {', '.join('--' + m.replace('_', '-') for m in missing)}",
            },
            exit_code=2,
        )

    if not allow_extra:
        unknown = [name for name in payload if name not in fields]
        if unknown:
            raise KaizenDenied(
                "DENIED_SCHEMA_UNKNOWN_FIELDS",
                {
                    "record_type": record_type,
                    "fields": unknown,
                    "required_action": "remove fields not in the schema; do not invent fields",
                },
                exit_code=2,
            )

    for name, rule in fields.items():
        type_name = rule.get("type", "any")
        if type_name != "any" and type_name not in _PY_TYPES:
            raise KaizenDenied(
                "DENIED_SCHEMA_FIELD_TYPE",
                {
                    "record_type": record_type,
                    "field": name,
                    "expected": type_name,
                    "required_action": "declare a supported schema field type",
                },
                exit_code=2,
            )
        if name not in payload or payload[name] is None:
            continue
        value = payload[name]
        if not _type_ok(value, type_name):
            raise KaizenDenied(
                "DENIED_SCHEMA_FIELD_TYPE",
                {
                    "record_type": record_type,
                    "field": name,
                    "expected": type_name,
                    "required_action": f"resubmit {name} as type {type_name}",
                },
                exit_code=2,
            )

        enum = rule.get("enum")
        if enum is not None and not any(type(value) is type(option) and value == option for option in enum):
            raise KaizenDenied(
                "DENIED_SCHEMA_ENUM",
                {
                    "record_type": record_type,
                    "field": name,
                    "value": value,
                    "allowed": list(enum),
                    "required_action": f"use an allowed value for {name}",
                },
                exit_code=2,
            )

        if isinstance(value, str):
            if value == "" and name not in required:
                continue
            if rule.get("summary"):
                validate_summary(value, required=name in required)
            elif "max_words" in rule:
                maximum_words = rule["max_words"]
                if isinstance(maximum_words, bool) or not isinstance(maximum_words, int) or maximum_words < 0:
                    raise KaizenDenied(
                        "DENIED_SCHEMA_SPEC_INVALID",
                        {"record_type": record_type, "field": name, "reason": "max_words must be a non-negative integer"},
                        exit_code=2,
                    )
                validate_word_limit(name, value, limit=maximum_words)

        if type_name in ("int", "float"):
            for bound_name in ("min", "max"):
                bound = rule.get(bound_name)
                if bound_name in rule and (isinstance(bound, bool) or not isinstance(bound, (int, float))):
                    raise KaizenDenied(
                        "DENIED_SCHEMA_SPEC_INVALID",
                        {"record_type": record_type, "field": name, "reason": f"{bound_name} must be numeric"},
                        exit_code=2,
                    )
            if "min" in rule and value < rule["min"]:
                raise KaizenDenied(
                    "DENIED_SCHEMA_RANGE",
                    {
                        "record_type": record_type,
                        "field": name,
                        "value": value,
                        "min": rule["min"],
                        "required_action": f"{name} must be >= {rule['min']}",
                    },
                    exit_code=2,
                )
            if "max" in rule and value > rule["max"]:
                raise KaizenDenied(
                    "DENIED_SCHEMA_RANGE",
                    {
                        "record_type": record_type,
                        "field": name,
                        "value": value,
                        "max": rule["max"],
                        "required_action": f"{name} must be <= {rule['max']}",
                    },
                    exit_code=2,
                )

    return payload


def to_json_schema(spec: dict[str, Any]) -> dict[str, Any]:
    """Project a compact Kaizen spec onto a Draft 2020-12 JSON Schema.

    Used only by the optional ``jsonschema`` accelerator; the compact spec stays
    authoritative. Only structural rules (type/enum/min/max) are projected -- text-length
    limits (``summary``/``max_words``) remain enforced solely by the stdlib validator.
    """
    json_types = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "list": "array",
        "dict": "object",
    }
    properties: dict[str, Any] = {}
    for name, rule in spec.get("fields", {}).items():
        prop: dict[str, Any] = {}
        mapped = json_types.get(rule.get("type", "any"))
        if mapped is not None:
            prop["type"] = mapped
        if "enum" in rule:
            prop["enum"] = list(rule["enum"])
        if "min" in rule:
            prop["minimum"] = rule["min"]
        if "max" in rule:
            prop["maximum"] = rule["max"]
        properties[name] = prop
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": list(spec.get("required", [])),
        "additionalProperties": bool(spec.get("allow_extra", False)),
    }
