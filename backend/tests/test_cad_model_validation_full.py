from __future__ import annotations

from hashlib import sha256
import json
import pytest

from htdt.cad_model_validation import CadModelValidationRecord, build_full_model_validation
from htdt.cad_validation_metrics import (
    CadApplicabilityCheck,
    CadObjectiveValidationSample,
    build_candidate_separation_check,
    build_repeatability_check,
    build_sensitivity_check,
)
from htdt.comparison import FrequencyResponse


AUTH = dict(
    document_id='doc',
    search_spec_id='spec',
    search_spec_sha256='1' * 64,
    candidate_set_sha256='2' * 64,
    model_id='rew-roomsim',
    model_version='fixture-1',
)


def _fr(offset: float) -> FrequencyResponse:
    return FrequencyResponse(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(80.0 + offset, 81.0 + offset, 79.0 + offset, 80.0 + offset),
    )


def _objective(candidate: str, predicted: float, measured: float) -> CadObjectiveValidationSample:
    return CadObjectiveValidationSample(
        candidate_id=candidate,
        split='holdout',
        objective_id='response.shape_rms_db',
        unit='dB',
        predicted_evaluation_id=f'pred-eval:{candidate}',
        measured_evaluation_id=f'meas-eval:{candidate}',
        predicted_value=predicted,
        measured_value=measured,
    )


def _advanced_inputs(*, reverse_measured: bool = False):
    objectives = (
        _objective('a', 1.0, 3.0 if reverse_measured else 1.1),
        _objective('b', 2.0, 2.0),
        _objective('c', 3.0, 1.0 if reverse_measured else 3.1),
    )
    sensitivity = build_sensitivity_check(
        objective_id='response.shape_rms_db',
        unit='dB',
        candidate_a_id='a',
        candidate_b_id='b',
        placement_delta_m=0.2,
        predicted_a=1.0,
        predicted_b=2.0,
        measured_a=1.1,
        measured_b=2.0,
        max_observed_sensitivity_per_m=6.0,
        max_model_error_per_m=1.0,
    )
    repeatability = build_repeatability_check(
        scene_revision_id='scene-a',
        measurements=(('repeat-1', _fr(0.0)), ('repeat-2', _fr(0.1))),
        low_hz=20.0,
        high_hz=160.0,
    )
    separation = build_candidate_separation_check(
        candidate_a_id='a',
        candidate_b_id='b',
        measurement_a_id='meas:a',
        measurement_b_id='meas:b',
        response_a=_fr(0.0),
        response_b=_fr(2.0),
        low_hz=20.0,
        high_hz=160.0,
        repeatability_floor_db=repeatability.rms_floor_db,
        min_repeatability_multiple=2.0,
    )
    return objectives, (sensitivity,), (repeatability,), (separation,)


def _record(*, evidence_scope: str, reverse_measured: bool = False):
    objectives, sensitivity, repeatability, separation = _advanced_inputs(
        reverse_measured=reverse_measured
    )
    return build_full_model_validation(
        **AUTH,
        response_samples=(
            ('cal', 'calibration', 'pred:cal', 'meas:cal', _fr(0.0), _fr(0.1)),
            ('a', 'holdout', 'pred:a', 'meas:a', _fr(0.0), _fr(0.2)),
            ('b', 'holdout', 'pred:b', 'meas:b', _fr(1.0), _fr(1.2)),
            ('c', 'holdout', 'pred:c', 'meas:c', _fr(2.0), _fr(2.2)),
        ),
        objective_samples=objectives,
        sensitivity_checks=sensitivity,
        repeatability_checks=repeatability,
        separation_checks=separation,
        applicability_checks=(
            CadApplicabilityCheck(code='geometry', passed=True, detail='exact rectangular room'),
            CadApplicabilityCheck(code='band', passed=True, detail='20-160 Hz inside validated band'),
            CadApplicabilityCheck(code='routing', passed=True, detail='channel mapping verified'),
        ),
        low_hz=20.0,
        high_hz=160.0,
        max_holdout_rms_db=1.0,
        evidence_scope=evidence_scope,
        campaign_id='campaign-fixture' if evidence_scope == 'owned_room' else None,
        campaign_sha256='3' * 64 if evidence_scope == 'owned_room' else None,
        campaign_registration_id=(
            'o60-validation-campaign-registration:' + '4' * 64
            if evidence_scope == 'owned_room'
            else None
        ),
        campaign_registration_sha256=(
            '5' * 64 if evidence_scope == 'owned_room' else None
        ),
        trend_min_agreement_ratio=0.75,
    )


def test_full_validation_can_be_eligible_only_with_owned_room_and_all_gates():
    record = _record(evidence_scope='owned_room')
    assert record.residual_gate == 'pass'
    assert record.trend_checks[0].gate == 'pass'
    assert record.sensitivity_checks[0].gate == 'pass'
    assert record.separation_checks[0].gate == 'pass'
    assert record.recommendation_gate == 'eligible'
    assert record.gate_reasons == ()


def test_synthetic_fixture_never_enables_recommendation():
    record = _record(evidence_scope='synthetic_fixture')
    assert record.recommendation_gate == 'disabled'
    assert 'owned-room evidence' in ' '.join(record.gate_reasons)


def test_discordant_holdout_trend_disables_recommendation():
    record = _record(evidence_scope='owned_room', reverse_measured=True)
    assert record.trend_checks[0].gate == 'fail'
    assert record.recommendation_gate == 'disabled'
    assert any('objective trend' in reason for reason in record.gate_reasons)


def test_stop_reasons_cannot_be_removed_even_with_rehashed_payload():
    record = _record(evidence_scope='synthetic_fixture')
    payload = record.model_dump(mode='python')
    payload['gate_reasons'] = ()
    identity = record.identity_payload()
    identity['gate_reasons'] = []
    payload['validation_sha256'] = sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()

    with pytest.raises(ValueError, match='gate reasons'):
        CadModelValidationRecord.model_validate(payload)
