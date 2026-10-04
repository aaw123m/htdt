"""Minimal JSON Schema (draft 2020-12 subset) checker for ingestion plans.

Stdlib-only validator covering exactly the keyword surface used by
``capture_contract/htdt-ingestion-plan-v1.schema.json`` so produced
ingestion plans can be pinned to the published wire contract. It is
intentionally not a general JSON Schema implementation.

Ported from HTDT-Capture ``tools/htdt_ingestion/plan_schema.py`` — keep the
checker and the vendored schema in lockstep with the emitter (the
``sync_capture_contract.py`` drift audit covers the schema file).
"""

from __future__ import annotations

import json
from pathlib import Path
import re

DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "capture_contract"
    / "htdt-ingestion-plan-v1.schema.json"
)

_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: (
        isinstance(value, int) and not isinstance(value, bool)
    ),
    "number": lambda value: (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    ),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}

# Keywords that never constrain instances directly.
_IGNORED_KEYWORDS = {
    "$schema",
    "$id",
    "$defs",
    "title",
    "description",
    "format",
    "if",
    "then",
    "else",
}


def load_schema(path: Path | None = None) -> dict:
    schema_path = path or DEFAULT_SCHEMA_PATH
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _json_equal(left, right) -> bool:
    """Type-aware JSON equality (``1`` does not equal ``true``)."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if set(left) != set(right):
            return False
        return all(_json_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


class _Checker:
    def __init__(self, root: dict):
        self.root = root
        self.errors: list[str] = []

    def _probe_valid(self, value, schema) -> bool:
        probe = _Checker(self.root)
        probe.check(value, schema, "$")
        return not probe.errors

    def check(self, value, schema, path: str) -> None:
        if isinstance(schema, bool):
            if not schema:
                self.errors.append(f"{path}: value is disallowed")
            return
        if not isinstance(schema, dict):
            self.errors.append(f"{path}: invalid schema node")
            return

        reference = schema.get("$ref")
        if reference is not None:
            prefix = "#/$defs/"
            if not reference.startswith(prefix):
                self.errors.append(
                    f"{path}: unsupported $ref {reference!r}"
                )
                return
            target = self.root.get("$defs", {}).get(reference[len(prefix):])
            if target is None:
                self.errors.append(
                    f"{path}: unresolved $ref {reference!r}"
                )
                return
            self.check(value, target, path)

        if "type" in schema:
            declared = schema["type"]
            alternatives = (
                [declared] if isinstance(declared, str) else declared
            )
            if not any(
                _TYPE_CHECKS[alternative](value)
                for alternative in alternatives
            ):
                self.errors.append(
                    f"{path}: expected type {declared!r}, got "
                    f"{type(value).__name__}"
                )
                # Type failure makes value-level keywords meaningless.
                return

        if "const" in schema and not _json_equal(value, schema["const"]):
            self.errors.append(
                f"{path}: expected constant {schema['const']!r}"
            )
        if "enum" in schema and not any(
            _json_equal(value, option) for option in schema["enum"]
        ):
            self.errors.append(
                f"{path}: {value!r} is not one of {schema['enum']!r}"
            )

        if isinstance(value, str):
            if (
                "minLength" in schema
                and len(value) < schema["minLength"]
            ):
                self.errors.append(f"{path}: string too short")
            if "pattern" in schema:
                if re.search(schema["pattern"], value) is None:
                    self.errors.append(
                        f"{path}: {value!r} does not match "
                        f"{schema['pattern']!r}"
                    )

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                self.errors.append(
                    f"{path}: {value!r} below minimum {schema['minimum']}"
                )
            if "maximum" in schema and value > schema["maximum"]:
                self.errors.append(
                    f"{path}: {value!r} above maximum {schema['maximum']}"
                )

        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                self.errors.append(f"{path}: fewer than minItems")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                self.errors.append(f"{path}: more than maxItems")
            if schema.get("uniqueItems"):
                for index in range(len(value)):
                    for other in range(index):
                        if _json_equal(value[index], value[other]):
                            self.errors.append(
                                f"{path}: duplicate items at "
                                f"[{other}] and [{index}]"
                            )
            item_schema = schema.get("items")
            if item_schema is not None:
                for index, item in enumerate(value):
                    self.check(item, item_schema, f"{path}[{index}]")

        if isinstance(value, dict):
            for key in schema.get("required", []):
                if key not in value:
                    self.errors.append(f"{path}: missing {key!r}")
            for property_name, requirements in schema.get(
                "dependentRequired", {}
            ).items():
                if property_name in value:
                    for dependency in requirements:
                        if dependency not in value:
                            self.errors.append(
                                f"{path}: {property_name!r} requires "
                                f"{dependency!r}"
                            )
            properties = schema.get("properties", {})
            for key, subschema in properties.items():
                if key in value:
                    self.check(value[key], subschema, f"{path}.{key}")
            additional = schema.get("additionalProperties")
            extras = set(value) - set(properties)
            if additional is False:
                for key in sorted(extras):
                    self.errors.append(
                        f"{path}: unexpected property {key!r}"
                    )
            elif isinstance(additional, dict):
                for key in sorted(extras):
                    self.check(value[key], additional, f"{path}.{key}")

        for subschema in schema.get("allOf", []):
            self.check(value, subschema, path)
        if "anyOf" in schema:
            if not any(
                self._probe_valid(value, sub)
                for sub in schema["anyOf"]
            ):
                self.errors.append(f"{path}: no anyOf branch matched")
        if "oneOf" in schema:
            matches = sum(
                1
                for sub in schema["oneOf"]
                if self._probe_valid(value, sub)
            )
            if matches != 1:
                self.errors.append(
                    f"{path}: expected exactly one oneOf match, "
                    f"got {matches}"
                )
        if "if" in schema:
            branch = "then" if self._probe_valid(
                value, schema["if"]
            ) else "else"
            if branch in schema:
                self.check(value, schema[branch], path)

        unknown = set(schema) - _IGNORED_KEYWORDS - {
            "$ref",
            "type",
            "const",
            "enum",
            "required",
            "dependentRequired",
            "properties",
            "additionalProperties",
            "items",
            "minItems",
            "maxItems",
            "uniqueItems",
            "minLength",
            "pattern",
            "minimum",
            "maximum",
            "allOf",
            "anyOf",
            "oneOf",
        }
        if unknown:
            self.errors.append(
                f"{path}: unsupported schema keywords {sorted(unknown)}"
            )


def validate_plan_document(plan, schema: dict | None = None) -> list[str]:
    """Return a list of schema violations for a generated plan."""
    root = schema if schema is not None else load_schema()
    checker = _Checker(root)
    checker.check(plan, root, "$")
    return checker.errors
