"""Issue #1009: measurement stationarity & temporal drift — repeat sweeps
describing one time-invariant room state, or blocking the average."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_stationarity import (
    EnvironmentTimelineEntry,
    MeasurementStationarityAssessment,
    MeasurementStationarityScope,
    RepeatDeviation,
    RepeatTimingRecord,
    assess_stationarity,
    build_stationarity_scope,
)

_H = '5' * 64


def _scope(**overrides):
    kwargs = dict(
        scope_id='st-1',
        schema_version='stat_v1',
        document_id='doc-1',
        measurement_ids=('m1', 'm2', 'm3'),
        observable='frequency_response',
        frequency_band_hz=(20.0, 200.0),
        method='pairwise_deviation',
        method_version='impl_v1',
        deviation_threshold_db=0.5,
        created_at_utc='2026-09-25T00:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_stationarity_scope(**kwargs)


def _timings():
    return (
        RepeatTimingRecord(
            measurement_id='m1',
            repeat_index=0,
            started_at_utc='2026-09-25T00:00:00+00:00',
        ),
        RepeatTimingRecord(
            measurement_id='m2',
            repeat_index=1,
            started_at_utc='2026-09-25T00:05:00+00:00',
        ),
        RepeatTimingRecord(
            measurement_id='m3',
            repeat_index=2,
            started_at_utc='2026-09-25T00:10:00+00:00',
        ),
    )


def _deviation(a='m1', b='m2', **overrides):
    kwargs = dict(
        measurement_id_a=a,
        measurement_id_b=b,
        max_deviation_db=0.2,
        coherence=0.99,
    )
    kwargs.update(overrides)
    return RepeatDeviation(**kwargs)


def _assess(scope=None, **overrides):
    kwargs = dict(
        assessment_id='sta-1',
        scope=scope or _scope(),
        repeat_timings=_timings(),
        deviations=(
            _deviation('m1', 'm2'),
            _deviation('m1', 'm3', max_deviation_db=0.3),
            _deviation('m2', 'm3', max_deviation_db=0.1),
        ),
        environment_timeline=(
            EnvironmentTimelineEntry(
                observed_at_utc='2026-09-25T00:06:00+00:00',
                state='observed',
                temperature_c=21.0,
                relative_humidity_percent=45.0,
            ),
        ),
        alignment_method='none',
        created_at_utc='2026-09-25T00:11:00+00:00',
    )
    kwargs.update(overrides)
    return assess_stationarity(**kwargs)


def test_scope_sealed():
    scope = _scope()
    assert len(scope.scope_sha256) == 64
    MeasurementStationarityScope.model_validate(scope.model_dump(mode='json'))


def test_scope_needs_two_measurements():
    with pytest.raises(ValidationError):
        _scope(measurement_ids=('m1',))


def test_scope_requires_method():
    with pytest.raises(ValidationError, match='explicit method'):
        _scope(method='unknown')


def test_stationary_result_permits_average():
    assessment = _assess()
    assert assessment.stationarity_state == 'stationary'
    assert assessment.averaging_gate == 'average_permitted'
    MeasurementStationarityAssessment.model_validate(
        assessment.model_dump(mode='json')
    )


def test_drift_blocks_average():
    assessment = _assess(
        deviations=(_deviation('m1', 'm2', max_deviation_db=1.5),),
    )
    assert assessment.stationarity_state == 'drifted'
    assert assessment.averaging_gate == 'average_blocked'


def test_gate_state_consistency():
    with pytest.raises(ValidationError, match='stationary scope cannot block'):
        MeasurementStationarityAssessment(
            assessment_id='a',
            schema_version='v1',
            document_id='d',
            scope_id='s',
            scope_sha256=_H,
            stationarity_state='stationary',
            averaging_gate='average_blocked',
            created_at_utc='t',
            assessment_sha256=_H,
        )
    with pytest.raises(ValidationError, match='only permitted'):
        MeasurementStationarityAssessment(
            assessment_id='a',
            schema_version='v1',
            document_id='d',
            scope_id='s',
            scope_sha256=_H,
            stationarity_state='drifted',
            averaging_gate='average_permitted',
            created_at_utc='t',
            assessment_sha256=_H,
        )


def test_alignment_is_derived_only():
    with pytest.raises(ValidationError, match='derived comparison'):
        MeasurementStationarityAssessment(
            assessment_id='a',
            schema_version='v1',
            document_id='d',
            scope_id='s',
            scope_sha256=_H,
            alignment_method='global_delay_alignment',
            alignment_applied_derived_only=False,
            created_at_utc='t',
            assessment_sha256=_H,
        )


def test_short_time_coherence_method():
    scope = _scope(method='short_time_coherence', coherence_threshold=0.95)
    ok = _assess(scope=scope, deviations=(_deviation(coherence=0.99),))
    assert ok.stationarity_state == 'stationary'
    low = _assess(scope=scope, deviations=(_deviation(coherence=0.80),))
    assert low.stationarity_state == 'suspect'
    assert low.averaging_gate == 'average_limited'


def test_unsupported_methods_report_insufficient():
    assessment = _assess(
        scope=_scope(method='environment_correlation'),
        deviations=(),
    )
    assert assessment.stationarity_state == 'insufficient_evidence'


def test_deviations_outside_scope_rejected():
    with pytest.raises(ValueError, match='outside the scoped'):
        _assess(deviations=(_deviation('m9', 'm2'),))


def test_environment_entry_needs_timestamp_when_observed():
    with pytest.raises(ValidationError, match='timestamp'):
        EnvironmentTimelineEntry(state='observed')
