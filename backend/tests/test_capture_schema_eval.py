"""Direct tests for the deterministic JSON-Schema subset evaluator.

``capture_schema_eval`` guards the Capture Bundle v1 ingestion boundary —
it is deliberately dependency-free and fails closed on any keyword or $ref
target outside the implemented subset, so these tests pin both the
supported semantics and the refusal modes.
"""

from __future__ import annotations

import pytest

from htdt.capture_schema_eval import SchemaError, check_schema, validate


# ---------------------------------------------------------------- types

@pytest.mark.parametrize(
    ('instance', 'type_name'),
    [
        ({}, 'object'),
        ([], 'array'),
        ('x', 'string'),
        (1, 'number'),
        (1.5, 'number'),
        (1, 'integer'),
        (3.0, 'integer'),  # draft 2020-12: integral floats count as integers
        (True, 'boolean'),
        (None, 'null'),
    ],
)
def test_type_accepts_matching_instances(instance, type_name) -> None:
    validate(instance, {'type': type_name})


@pytest.mark.parametrize(
    ('instance', 'type_name'),
    [
        ([], 'object'),
        ({}, 'array'),
        (1, 'string'),
        ('1', 'integer'),
        (True, 'number'),  # bool is never a number
        (1.5, 'integer'),
        (0, 'boolean'),
        ('', 'null'),
    ],
)
def test_type_rejects_mismatched_instances(instance, type_name) -> None:
    with pytest.raises(SchemaError, match='expected type'):
        validate(instance, {'type': type_name})


def test_type_list_union() -> None:
    schema = {'type': ['string', 'null']}
    validate('ok', schema)
    validate(None, schema)
    with pytest.raises(SchemaError):
        validate(3, schema)


def test_unknown_type_name_fails_closed() -> None:
    with pytest.raises(SchemaError, match='unsupported type name'):
        validate(1, {'type': 'integerish'})


# ------------------------------------------------------------ enum/const

def test_const_and_enum_use_json_equality() -> None:
    validate({'a': [1, 2]}, {'const': {'a': [1, 2]}})
    with pytest.raises(SchemaError):
        validate({'a': [1, 2]}, {'const': {'a': [2, 1]}})
    # numeric-aware equality: 1 equals 1.0 but not True
    validate(1, {'enum': [1.0, 2]})
    with pytest.raises(SchemaError):
        validate(True, {'enum': [1]})


def test_enum_must_be_non_empty_array() -> None:
    with pytest.raises(SchemaError, match='enum must be a non-empty array'):
        validate(1, {'enum': []})


# ------------------------------------------------------------- strings

def test_string_length_and_pattern() -> None:
    schema = {'type': 'string', 'minLength': 2, 'maxLength': 4, 'pattern': '^a'}
    validate('abc', schema)
    with pytest.raises(SchemaError, match='minLength'):
        validate('a', schema)
    with pytest.raises(SchemaError, match='maxLength'):
        validate('abcde', schema)
    with pytest.raises(SchemaError, match='pattern'):
        validate('bzz', schema)


def test_unsupported_format_fails_closed() -> None:
    with pytest.raises(SchemaError, match='unsupported format'):
        validate('x', {'type': 'string', 'format': 'email'})


def test_format_uuid_is_asserted_lowercase() -> None:
    schema = {'type': 'string', 'format': 'uuid'}
    validate('10000000-0000-4000-8000-000000000005', schema)
    with pytest.raises(SchemaError):
        validate('10000000-0000-4000-8000-00000000000G', schema)
    # Uppercase hex is not a canonical v1 uuid and must be rejected.
    with pytest.raises(SchemaError):
        validate('ABCDEF01-0000-4000-8000-00000000000A', schema)


def test_format_date_and_date_time_are_asserted() -> None:
    validate('2026-09-26', {'type': 'string', 'format': 'date'})
    validate('2026-09-26T21:00:00Z', {'type': 'string', 'format': 'date-time'})
    validate('2026-09-26T21:00:00+02:30', {'type': 'string', 'format': 'date-time'})
    with pytest.raises(SchemaError):
        validate('2026-13-40', {'type': 'string', 'format': 'date'})
    with pytest.raises(SchemaError):
        validate('2026-09-26 21:00:00', {'type': 'string', 'format': 'date-time'})


# ------------------------------------------------------------- numbers

def test_minimum_maximum_bounds() -> None:
    schema = {'minimum': 0.0, 'maximum': 10.0}
    validate(0, schema)
    validate(10, schema)
    with pytest.raises(SchemaError, match='minimum'):
        validate(-0.5, schema)
    with pytest.raises(SchemaError, match='maximum'):
        validate(10.5, schema)


def test_non_finite_number_rejected() -> None:
    with pytest.raises(SchemaError, match='non-finite'):
        validate(float('inf'), {'type': 'number'})
    with pytest.raises(SchemaError, match='non-finite'):
        validate(float('nan'), {'type': 'number'})


# -------------------------------------------------------------- arrays

def test_items_and_size_bounds() -> None:
    schema = {'type': 'array', 'minItems': 1, 'maxItems': 3,
              'items': {'type': 'integer'}}
    validate([1, 2, 3], schema)
    with pytest.raises(SchemaError, match='minItems'):
        validate([], schema)
    with pytest.raises(SchemaError, match='maxItems'):
        validate([1, 2, 3, 4], schema)
    with pytest.raises(SchemaError):
        validate([1, 'x'], schema)


def test_unique_items_uses_json_equality() -> None:
    schema = {'type': 'array', 'uniqueItems': True}
    validate([1, 2, {'a': 1}], schema)
    with pytest.raises(SchemaError, match='duplicate items'):
        validate([{'a': 1}, {'a': 1}], schema)
    with pytest.raises(SchemaError, match='duplicate items'):
        validate([1, 1.0], schema)


# -------------------------------------------------------------- objects

def test_required_and_properties() -> None:
    schema = {
        'type': 'object',
        'required': ['id'],
        'properties': {'id': {'type': 'string'},
                       'n': {'type': 'integer'}},
    }
    validate({'id': 'x'}, schema)
    validate({'id': 'x', 'n': 1, 'extra': True}, schema)  # extras allowed
    with pytest.raises(SchemaError, match='missing required property'):
        validate({}, schema)
    with pytest.raises(SchemaError):
        validate({'id': 'x', 'n': 'bad'}, schema)


def test_additional_properties_false_rejects_extras() -> None:
    schema = {
        'type': 'object',
        'properties': {'id': {'type': 'string'}},
        'additionalProperties': False,
    }
    validate({'id': 'x'}, schema)
    with pytest.raises(SchemaError, match='unexpected properties'):
        validate({'id': 'x', 'rogue': 1}, schema)


def test_additional_properties_schema_validates_extras() -> None:
    schema = {
        'type': 'object',
        'properties': {'id': {'type': 'string'}},
        'additionalProperties': {'type': 'integer'},
    }
    validate({'id': 'x', 'count': 3}, schema)
    with pytest.raises(SchemaError):
        validate({'id': 'x', 'count': 'nope'}, schema)


# ---------------------------------------------------------- composition

def test_all_of() -> None:
    schema = {'allOf': [{'type': 'integer', 'minimum': 0}, {'maximum': 5}]}
    validate(3, schema)
    with pytest.raises(SchemaError):
        validate(7, schema)


def test_any_of() -> None:
    schema = {'anyOf': [{'type': 'string'}, {'type': 'integer'}]}
    validate('s', schema)
    validate(2, schema)
    with pytest.raises(SchemaError, match='no anyOf branch matched'):
        validate([], schema)


def test_one_of_requires_exactly_one_match() -> None:
    schema = {'oneOf': [{'type': 'integer'}, {'minimum': 0}]}
    validate('not-an-int', schema)
    with pytest.raises(SchemaError, match='exactly one'):
        validate(3, schema)


def test_if_then_else() -> None:
    schema = {
        'if': {'type': 'integer'},
        'then': {'minimum': 0},
        'else': {'type': 'string'},
    }
    validate(4, schema)
    validate('ok', schema)
    with pytest.raises(SchemaError):
        validate(-1, schema)
    with pytest.raises(SchemaError):
        validate([], schema)


# ----------------------------------------------------------------- $ref

def test_local_ref_resolution() -> None:
    schema = {
        '$defs': {'ident': {'type': 'string', 'minLength': 1}},
        'type': 'object',
        'properties': {'id': {'$ref': '#/$defs/ident'}},
    }
    validate({'id': 'x'}, schema)
    with pytest.raises(SchemaError):
        validate({'id': ''}, schema)


def test_ref_sibling_keywords_still_apply() -> None:
    # Draft 2020-12: $ref is an applicator; siblings are enforced too.
    schema = {
        '$defs': {'ident': {'type': 'string'}},
        'type': 'object',
        'properties': {
            'id': {'$ref': '#/$defs/ident', 'minLength': 3},
        },
    }
    with pytest.raises(SchemaError, match='minLength'):
        validate({'id': 'ab'}, schema)


def test_non_local_ref_fails_closed() -> None:
    with pytest.raises(SchemaError, match='unsupported \\$ref target'):
        validate('x', {'$ref': 'https://example.com/schema.json'})
    with pytest.raises(SchemaError, match='unsupported \\$ref target'):
        validate('x', {'$ref': '#'})


def test_unresolvable_ref_fails_closed() -> None:
    with pytest.raises(SchemaError, match='unresolvable \\$ref'):
        validate('x', {'$ref': '#/$defs/missing'})
    with pytest.raises(SchemaError, match='unresolvable \\$ref'):
        validate('x', {'$ref': '#/'})


def test_ref_escaped_segments() -> None:
    schema = {
        '$defs': {'a/b': {'type': 'integer'}, 't~x': {'type': 'string'}},
        'type': 'object',
        'properties': {
            'a': {'$ref': '#/$defs/a~1b'},
            't': {'$ref': '#/$defs/t~0x'},
        },
    }
    validate({'a': 1, 't': 's'}, schema)
    with pytest.raises(SchemaError):
        validate({'a': 's'}, schema)


# --------------------------------------------------------- boolean schemas

def test_boolean_schemas() -> None:
    validate({'anything': 1}, True)
    with pytest.raises(SchemaError, match='boolean schema false'):
        validate({'anything': 1}, False)
    with pytest.raises(SchemaError, match='boolean schema false'):
        validate({'id': 1},
                 {'type': 'object',
                  'properties': {'id': False}})


# ------------------------------------------------- fail-closed policy

def test_unknown_keyword_fails_closed_at_validation() -> None:
    with pytest.raises(SchemaError, match='unsupported schema keyword'):
        validate('x', {'type': 'string', 'patternProperties': {'^a': {}}})


def test_check_schema_recurses_into_composition() -> None:
    check_schema({'allOf': [{'type': 'integer'}]})
    check_schema({'properties': {'a': {'anyOf': [{'const': 1}]}}})
    with pytest.raises(SchemaError, match='unsupported schema keyword'):
        check_schema({'properties': {'a': {'not': {}}}})
    with pytest.raises(SchemaError, match='unsupported schema keyword'):
        check_schema({'$defs': {'x': {'dependentRequired': ['y']}}})


def test_annotation_keywords_are_ignored() -> None:
    schema = {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        '$id': 'urn:test',
        'title': 't', 'description': 'd', '$comment': 'c',
        'examples': [1], 'default': 1, 'deprecated': True,
        'readOnly': True, 'writeOnly': False,
        'type': 'integer',
    }
    validate(3, schema)
    check_schema(schema)


def test_schema_must_be_object_or_boolean() -> None:
    with pytest.raises(SchemaError, match='object or boolean'):
        validate(1, 'not-a-schema')
    with pytest.raises(SchemaError, match='object or boolean'):
        check_schema([1, 2, 3])
