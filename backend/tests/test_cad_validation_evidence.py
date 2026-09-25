from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from htdt.cad_validation_evidence import (
    ExternalValidationDatasetRecord,
    evaluate_evidence_program,
)

NOW = '2026-09-24T00:00:00+00:00'


def _canonical(v) -> str:
    return json.dumps(
        v, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False,
    )


def _dataset(**overrides) -> ExternalValidationDatasetRecord:
    payload = {
        'dataset_id': 'validation-dataset:room-a',
        'title': 'Living room sweep campaign 1',
        'origin': 'measured',
        'evidence_classes': ('E2',),
        'license_kind': 'internal_owned',
        'holdout_role': 'development',
        'provenance_note': 'UMIK-1 sweeps in owned living room',
        **overrides,
    }
    provisional = ExternalValidationDatasetRecord.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return ExternalValidationDatasetRecord.model_validate(
        {
            **payload,
            'semantic_sha256': hashlib.sha256(
                _canonical(provisional.identity_payload()).encode('utf-8')
            ).hexdigest(),
        }
    )


def test_measured_origin_must_offer_e2() -> None:
    with pytest.raises(ValidationError):
        _dataset(evidence_classes=('E4',))


def test_simulated_never_provides_e2() -> None:
    with pytest.raises(ValidationError):
        _dataset(origin='simulated', evidence_classes=('E2', 'E5'))


def test_locked_requires_holdout_role() -> None:
    with pytest.raises(ValidationError):
        _dataset(locked=True, holdout_role='development')


def test_fresh_campaign_requires_reason() -> None:
    with pytest.raises(ValidationError):
        _dataset(fresh_campaign_required=True)


def test_holdout_and_fresh_fields_flow_into_program() -> None:
    holdout = _dataset(
        dataset_id='validation-dataset:holdout-1',
        holdout_role='holdout',
        locked=True,
    )
    stale = _dataset(
        dataset_id='validation-dataset:old-room',
        fresh_campaign_required=True,
        fresh_campaign_reason='room was renovated since acquisition',
    )
    program = evaluate_evidence_program(
        (holdout, stale),
        program_id='validation-program:p1',
        created_at_utc=NOW,
    )
    assert 'validation-dataset:holdout-1' in program.locked_holdout_ids
    assert 'validation-dataset:old-room' in program.fresh_campaign_ids
    e2 = next(r for r in program.rows if r.evidence_class == 'E2')
    # two providers but one needs a fresh campaign -> PARTIAL
    assert e2.status == 'PARTIAL'
    assert e2.measured_datasets == (
        'validation-dataset:holdout-1', 'validation-dataset:old-room'
    )


def test_empty_and_partial_coverage() -> None:
    synthetic = _dataset(
        dataset_id='validation-dataset:sim-1',
        origin='simulated',
        evidence_classes=('E5',),
    )
    program = evaluate_evidence_program(
        (synthetic,),
        program_id='validation-program:p1',
        created_at_utc=NOW,
    )
    by_class = {r.evidence_class: r for r in program.rows}
    assert by_class['E5'].status == 'PARTIAL'  # single provider
    assert by_class['E2'].status == 'EMPTY'
    assert 'no dataset' in by_class['E2'].gap_note
    assert by_class['E1'].status == 'EMPTY'


def test_simulated_only_e2_is_not_measured() -> None:
    fake_e2 = _dataset(
        dataset_id='validation-dataset:auth-1',
        origin='authored',
        evidence_classes=('E2',),
    )
    program = evaluate_evidence_program(
        (fake_e2,),
        program_id='validation-program:p1',
        created_at_utc=NOW,
    )
    e2 = next(r for r in program.rows if r.evidence_class == 'E2')
    assert e2.status == 'PARTIAL'
    assert 'no measured' in e2.gap_note
    assert e2.measured_datasets == ()


def test_all_fresh_is_needs_fresh_campaign() -> None:
    old = _dataset(
        dataset_id='validation-dataset:old',
        fresh_campaign_required=True,
        fresh_campaign_reason='stale',
    )
    program = evaluate_evidence_program(
        (old,),
        program_id='validation-program:p1',
        created_at_utc=NOW,
    )
    e2 = next(r for r in program.rows if r.evidence_class == 'E2')
    assert e2.status == 'NEEDS_FRESH_CAMPAIGN'


def test_program_deterministic() -> None:
    d = _dataset()
    first = evaluate_evidence_program(
        (d,), program_id='validation-program:p1', created_at_utc=NOW
    )
    second = evaluate_evidence_program(
        (d,), program_id='validation-program:p1', created_at_utc=NOW
    )
    assert first.semantic_sha256 == second.semantic_sha256
