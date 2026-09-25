"""Issue #969: installed-system nonlinear measurement authority."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_system_nonlinearity import (
    CadDistortionSample,
    SystemNonlinearityMeasurement,
    build_system_nonlinearity_measurement,
)

_H = 'a' * 64


def _sample(**overrides):
    kwargs = dict(
        metric='thd',
        unit='percent',
        frequency_hz=(20.0, 40.0, 80.0),
        values=(0.5, 1.2, 0.8),
    )
    kwargs.update(overrides)
    return CadDistortionSample(**kwargs)


def _measurement(**overrides):
    kwargs = dict(
        result_id='nl-1',
        document_id='doc-1',
        measurement_id='meas-1',
        scene_revision_id='rev-1',
        scene_content_hash=_H,
        producer='rew',
        producer_version='5.40b90',
        method='stepped_sine',
        frequency_hz=(20.0, 40.0, 80.0),
        excitation_levels_db=(70.0, 80.0, 90.0),
        distortion_samples=(_sample(metric='thd'),),
        created_at_utc='2026-09-25T00:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_system_nonlinearity_measurement(**kwargs)


def test_sealed_measurement_roundtrips():
    measurement = _measurement()
    assert len(measurement.semantic_sha256) == 64
    assert measurement.evidence_kind == 'installed_system_measurement'
    # a persisted row self-verifies on re-parse
    SystemNonlinearityMeasurement.model_validate(measurement.model_dump(mode='json'))


def test_hash_detects_tamper():
    measurement = _measurement()
    payload = measurement.model_dump(mode='json')
    payload['method'] = 'ramped_level'
    with pytest.raises(ValidationError, match='hash mismatch'):
        SystemNonlinearityMeasurement.model_validate(payload)


def test_harmonic_metrics_require_order():
    with pytest.raises(ValidationError, match='harmonic_order'):
        _sample(metric='harmonic_level', unit='db_spl')


def test_sample_axis_grid_shape():
    # 2 frequencies x 3 excitation levels must declare 6 values
    with pytest.raises(ValidationError, match='values length'):
        _sample(
            frequency_hz=(20.0, 40.0),
            level_axis_db=(70.0, 80.0, 90.0),
            values=(0.1, 0.2),
        )
    sample = _sample(
        metric='thd',
        frequency_hz=(20.0, 40.0),
        level_axis_db=(70.0, 80.0, 90.0),
        values=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6),
    )
    assert len(sample.values) == 6


def test_samples_must_stay_within_declared_axis():
    with pytest.raises(ValidationError, match='within the'):
        _measurement(
            distortion_samples=(_sample(frequency_hz=(1.0, 40.0, 80.0)),)
        )


def test_harmonic_order_capped_by_highest():
    with pytest.raises(ValidationError, match='highest_harmonic'):
        _measurement(
            highest_harmonic=3,
            distortion_samples=(
                _sample(
                    metric='harmonic_ratio',
                    unit='ratio',
                    harmonic_order=5,
                    values=(0.01, 0.02, 0.03),
                ),
            ),
        )


def test_valid_quality_requires_evidence():
    with pytest.raises(ValidationError, match='valid nonlinearity'):
        _measurement(quality='valid', distortion_samples=())


def test_evidence_kind_is_installed_system_only():
    measurement = _measurement()
    payload = measurement.model_dump(mode='json')
    payload['evidence_kind'] = 'unknown'
    with pytest.raises(ValidationError, match='installed-system'):
        SystemNonlinearityMeasurement.model_validate(payload)
