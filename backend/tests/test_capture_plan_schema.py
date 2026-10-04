"""Produced ingestion plans must validate against the published
``htdt.capture.ingestion-plan`` v1 contract.

The vendored schema (``capture_contract/htdt-ingestion-plan-v1.schema.json``)
is synced verbatim from the HTDT-Capture emitter by
``scripts/sync_capture_contract.py``; these tests catch plan drift the
moment the ingestor emits a shape the wire contract no longer describes —
the exact failure class that previously shipped silently on both sides.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from htdt.capture_bundle import FrozenBundle
from htdt.capture_plan_schema import (
    load_schema,
    validate_plan_document,
)
from htdt.capture_reference import build_ingestion_plan


FIXTURES = (
    Path(__file__).resolve().parent / 'fixtures' / 'capture'
)


@pytest.mark.parametrize(
    'fixture', ['phase6-integration', 'swift-extended', 'swift-maximal']
)
def test_produced_plan_conforms_to_published_schema(fixture: str) -> None:
    plan = build_ingestion_plan(FrozenBundle(FIXTURES / fixture))
    assert validate_plan_document(plan) == []


def test_schema_gate_catches_violations() -> None:
    plan = build_ingestion_plan(
        FrozenBundle(FIXTURES / 'phase6-integration')
    )
    plan['ingestor']['version'] = '9.9.9'
    errors = validate_plan_document(plan)
    assert errors
    assert any('version' in error or 'const' in error for error in errors)


def test_vendored_schema_is_valid_json_object() -> None:
    schema = load_schema()
    assert schema['$defs']
    assert schema['properties']['supplemental_documents']
