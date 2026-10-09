"""Deterministic minimal JSON Schema evaluator for Capture Bundle v1.

This module intentionally implements only the JSON Schema subset used by the
published schemas in ``schemas/capture-bundle-v1/``. It is dependency-free
(stdlib only) and deterministic: every evaluation either returns a definite
result or raises ``SchemaError``.

Fail-closed policy: encountering a schema keyword this evaluator does not
implement raises ``SchemaError`` rather than silently skipping the check, so a
schema can never smuggle an unenforced constraint past the validator.

Supported keywords:

- structure: ``type``, ``properties``, ``additionalProperties``, ``required``,
  ``items``, ``enum``, ``const``
- numbers/strings/arrays: ``minimum``, ``maximum``, ``minLength``,
  ``maxLength``, ``pattern``, ``minItems``, ``maxItems``, ``uniqueItems``
- composition: ``allOf``, ``anyOf``, ``oneOf``, ``if``/``then``/``else``
- references: ``$ref`` to local ``#/$defs/<name>`` or ``#/definitions/<name>``
  only; any other reference target fails closed
- formats (asserted, not annotation-only): ``date-time``, ``date``, ``uuid``

Annotation keywords (``$schema``, ``$id``, ``$defs``, ``definitions``,
``title``, ``description``, ``$comment``, ``examples``, ``default``,
``deprecated``, ``readOnly``, ``writeOnly``) carry no validation semantics and
are ignored. ``$defs``/``definitions`` subtrees are still keyword-checked when
they are reached through ``$ref``.
"""

from __future__ import annotations

from datetime import date, datetime
import math
import re
from uuid import UUID


class SchemaError(ValueError):
    """Raised for both schema-invalid instances and unsupported schemas."""


_ASSERTION_KEYWORDS = {
    "$ref",
    "type",
    "enum",
    "const",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "format",
    "allOf",
    "anyOf",
    "oneOf",
    "if",
    "then",
    "else",
}

_ANNOTATION_KEYWORDS = {
    "$schema",
    "$id",
    "$defs",
    "definitions",
    "title",
    "description",
    "$comment",
    "examples",
    "default",
    "deprecated",
    "readOnly",
    "writeOnly",
}

_TYPE_NAMES = {
    "object",
    "array",
    "string",
    "number",
    "integer",
    "boolean",
    "null",
}

_FORMATTERS = {}

_DATE_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _assert_format_datetime(value: str) -> bool:
    if not _DATE_TIME_RE.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    return True


def _assert_format_date(value: str) -> bool:
    if not _DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _assert_format_uuid(value: str) -> bool:
    if not _UUID_RE.fullmatch(value):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


_FORMATTERS["date-time"] = _assert_format_datetime
_FORMATTERS["date"] = _assert_format_date
_FORMATTERS["uuid"] = _assert_format_uuid


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_integer(value) -> bool:
    # Draft 2020-12: an integer is any number with a zero fractional part.
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


def _type_matches(value, type_name: str) -> bool:
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "number":
        return _is_number(value)
    if type_name == "integer":
        return _is_integer(value)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "null":
        return value is None
    raise SchemaError(f"unsupported type name: {type_name!r}")


def json_equal(left, right) -> bool:
    """JSON Schema instance equality (type-sensitive, numeric-aware)."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if _is_number(left) and _is_number(right):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if left.keys() != right.keys():
            return False
        return all(json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        if len(left) != len(right):
            return False
        return all(json_equal(a, b) for a, b in zip(left, right))
    return left == right


def _check_schema_node(schema, path: str) -> None:
    """Fail closed on keywords outside the implemented subset."""
    if isinstance(schema, bool):
        # JSON Schema boolean schemas: `true` accepts, `false` rejects all.
        return
    if not isinstance(schema, dict):
        raise SchemaError(f"schema at {path} must be an object or boolean")
    for key in schema:
        if key in _ASSERTION_KEYWORDS or key in _ANNOTATION_KEYWORDS:
            continue
        raise SchemaError(f"unsupported schema keyword at {path}: {key!r}")


class _Evaluator:
    def __init__(self, root_schema):
        self.root = root_schema

    def resolve_ref(self, ref: str):
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise SchemaError(f"unsupported $ref target: {ref!r}")
        node = self.root
        for raw_part in ref[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise SchemaError(f"unresolvable $ref: {ref!r}")
            node = node[part]
        return node

    def errors(self, instance, schema, path: str, schema_path: str) -> list[str]:
        """Return validation error strings; empty means valid."""
        try:
            self._validate(instance, schema, path, schema_path)
        except SchemaError as exc:
            return [str(exc)]
        return []

    def _validate(self, instance, schema, path: str, schema_path: str) -> None:
        if isinstance(schema, bool):
            if not schema:
                raise SchemaError(f"{path}: rejected by boolean schema false")
            return
        _check_schema_node(schema, schema_path)

        ref = schema.get("$ref")
        if ref is not None:
            # Draft 2020-12 treats $ref as a regular applicator: sibling
            # keywords still apply.
            self._validate(
                instance,
                self.resolve_ref(ref),
                path,
                f"$ref({ref})",
            )

        type_spec = schema.get("type")
        if type_spec is not None:
            if isinstance(type_spec, str):
                type_names = [type_spec]
            elif isinstance(type_spec, list) and all(
                isinstance(name, str) for name in type_spec
            ):
                type_names = list(type_spec)
            else:
                raise SchemaError(
                    f"{schema_path}.type must be a string or string array"
                )
            for name in type_names:
                if name not in _TYPE_NAMES:
                    raise SchemaError(f"unsupported type name: {name!r}")
            if not any(_type_matches(instance, name) for name in type_names):
                raise SchemaError(
                    f"{path}: expected type {type_spec!r}, "
                    f"got {type(instance).__name__}"
                )

        if "const" in schema and not json_equal(instance, schema["const"]):
            raise SchemaError(
                f"{path}: expected constant {schema['const']!r}, "
                f"got {instance!r}"
            )

        if "enum" in schema:
            options = schema["enum"]
            if not isinstance(options, list) or not options:
                raise SchemaError(f"{schema_path}.enum must be a non-empty array")
            if not any(json_equal(instance, option) for option in options):
                raise SchemaError(
                    f"{path}: value {instance!r} not in enum {options!r}"
                )

        if isinstance(instance, str):
            min_length = schema.get("minLength")
            if min_length is not None and len(instance) < min_length:
                raise SchemaError(
                    f"{path}: string shorter than minLength {min_length}"
                )
            max_length = schema.get("maxLength")
            if max_length is not None and len(instance) > max_length:
                raise SchemaError(
                    f"{path}: string longer than maxLength {max_length}"
                )
            pattern = schema.get("pattern")
            if pattern is not None:
                try:
                    matched = re.search(pattern, instance) is not None
                except re.error as exc:
                    raise SchemaError(
                        f"unsupported pattern at {schema_path}: {exc}"
                    ) from exc
                if not matched:
                    raise SchemaError(
                        f"{path}: string does not match pattern {pattern!r}"
                    )
            fmt = schema.get("format")
            if fmt is not None:
                checker = _FORMATTERS.get(fmt)
                if checker is None:
                    raise SchemaError(
                        f"unsupported format at {schema_path}: {fmt!r}"
                    )
                if not checker(instance):
                    raise SchemaError(
                        f"{path}: string is not a valid {fmt}: {instance!r}"
                    )

        if _is_number(instance):
            if isinstance(instance, float) and not math.isfinite(instance):
                raise SchemaError(f"{path}: non-finite number")
            minimum = schema.get("minimum")
            if minimum is not None and instance < minimum:
                raise SchemaError(f"{path}: {instance} < minimum {minimum}")
            maximum = schema.get("maximum")
            if maximum is not None and instance > maximum:
                raise SchemaError(f"{path}: {instance} > maximum {maximum}")

        if isinstance(instance, list):
            min_items = schema.get("minItems")
            if min_items is not None and len(instance) < min_items:
                raise SchemaError(
                    f"{path}: fewer than minItems {min_items} items"
                )
            max_items = schema.get("maxItems")
            if max_items is not None and len(instance) > max_items:
                raise SchemaError(
                    f"{path}: more than maxItems {max_items} items"
                )
            if schema.get("uniqueItems"):
                for left_index in range(len(instance)):
                    for right_index in range(left_index + 1, len(instance)):
                        if json_equal(
                            instance[left_index], instance[right_index]
                        ):
                            raise SchemaError(
                                f"{path}: duplicate items at indexes "
                                f"{left_index} and {right_index}"
                            )
            items_schema = schema.get("items")
            if items_schema is not None:
                for index, child in enumerate(instance):
                    self._validate(
                        child,
                        items_schema,
                        f"{path}[{index}]",
                        f"{schema_path}.items",
                    )

        if isinstance(instance, dict):
            required = schema.get("required")
            if required is not None:
                if not isinstance(required, list) or not all(
                    isinstance(name, str) for name in required
                ):
                    raise SchemaError(
                        f"{schema_path}.required must be a string array"
                    )
                for name in required:
                    if name not in instance:
                        raise SchemaError(
                            f"{path}: missing required property {name!r}"
                        )
            properties = schema.get("properties")
            if properties is not None:
                if not isinstance(properties, dict):
                    raise SchemaError(
                        f"{schema_path}.properties must be an object"
                    )
                for name, subschema in properties.items():
                    if name in instance:
                        self._validate(
                            instance[name],
                            subschema,
                            f"{path}.{name}",
                            f"{schema_path}.properties.{name}",
                        )
            if "additionalProperties" in schema:
                additional = schema["additionalProperties"]
                declared = set(properties) if properties else set()
                extras = [key for key in instance if key not in declared]
                if additional is False:
                    if extras:
                        raise SchemaError(
                            f"{path}: unexpected properties "
                            f"{sorted(extras)!r}"
                        )
                elif additional is True:
                    pass
                else:
                    for key in extras:
                        self._validate(
                            instance[key],
                            additional,
                            f"{path}.{key}",
                            f"{schema_path}.additionalProperties",
                        )

        for index, subschema in enumerate(schema.get("allOf", [])):
            self._validate(
                instance, subschema, path, f"{schema_path}.allOf[{index}]"
            )

        if "anyOf" in schema:
            branches = schema["anyOf"]
            if not isinstance(branches, list) or not branches:
                raise SchemaError(
                    f"{schema_path}.anyOf must be a non-empty array"
                )
            if not any(
                not self.errors(instance, branch, path, f"{schema_path}.anyOf")
                for branch in branches
            ):
                raise SchemaError(f"{path}: no anyOf branch matched")

        if "oneOf" in schema:
            branches = schema["oneOf"]
            if not isinstance(branches, list) or not branches:
                raise SchemaError(
                    f"{schema_path}.oneOf must be a non-empty array"
                )
            matched = [
                index
                for index, branch in enumerate(branches)
                if not self.errors(instance, branch, path, f"{schema_path}.oneOf")
            ]
            if len(matched) != 1:
                raise SchemaError(
                    f"{path}: expected exactly one oneOf match, "
                    f"got {len(matched)}"
                )

        if "if" in schema:
            condition = schema["if"]
            condition_ok = not self.errors(
                instance, condition, path, f"{schema_path}.if"
            )
            branch_key = "then" if condition_ok else "else"
            branch = schema.get(branch_key)
            if branch is not None:
                self._validate(
                    instance, branch, path, f"{schema_path}.{branch_key}"
                )


def validate(instance, schema) -> None:
    """Validate ``instance`` against ``schema``; raise SchemaError on failure."""
    if not isinstance(schema, (dict, bool)):
        raise SchemaError("schema root must be an object or boolean")
    _Evaluator(schema)._validate(instance, schema, "$", "#")


def check_schema(schema) -> None:
    """Fail closed if a schema uses constructs this evaluator cannot express."""
    _check_whole_schema(schema, "#")


def _check_whole_schema(schema, path: str) -> None:
    _check_schema_node(schema, path)
    if not isinstance(schema, dict):
        return
    for key, value in schema.items():
        if key in ("properties", "$defs", "definitions"):
            if not isinstance(value, dict):
                raise SchemaError(f"{path}.{key} must be an object")
            for name, subschema in value.items():
                _check_whole_schema(subschema, f"{path}.{key}.{name}")
        elif key == "additionalProperties":
            _check_whole_schema(value, f"{path}.{key}")
        elif key == "items":
            _check_whole_schema(value, f"{path}.{key}")
        elif key in ("allOf", "anyOf", "oneOf"):
            for index, subschema in enumerate(value):
                _check_whole_schema(subschema, f"{path}.{key}[{index}]")
        elif key in ("if", "then", "else"):
            _check_whole_schema(value, f"{path}.{key}")
