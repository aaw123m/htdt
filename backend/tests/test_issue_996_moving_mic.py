"""Issue #996: moving-microphone spatial-average authority — trajectory /
RTA average represented apart from discrete-point campaigns; a spatial
average structurally cannot claim a point measurement."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_moving_mic_measurement import (
    MEASUREMENT_KIND_SPATIAL_AVERAGE_MOVING_MIC,
    MovingMicrophoneMeasurementSpec,
    SpatialAverageMeasurement,
    SpatialAverageRegion,
    TrajectorySample,
    build_moving_mic_spec,
    build_spatial_average_measurement,
    validate_regional_identity,
)
from htdt.cad_scene import Position3

_H = '3' * 64


def _pos(x, y, z=1.2):
    return Position3(x_m=x, y_m=y, z_m=z)


def _region():
    return SpatialAverageRegion(
        region_id='mmt-seat-area',
        bounds_min=_pos(0.5, 0.5, 0.8),
        bounds_max=_pos(1.5, 1.5, 1.6),
    )


def _spec(**overrides):
    kwargs = dict(
        spec_id='mm-1',
        schema_version='mm_v1',
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_H,
        producer='rew',
        producer_version='5.40b90',
        region=_region(),
        trajectory_representation='region_only_manual',
        created_at_utc='2026-09-25T00:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_moving_mic_spec(**kwargs)


def _measurement(spec=None, **overrides):
    spec = spec or _spec()
    kwargs = dict(
        result_id='sa-1',
        schema_version='sa_v1',
        document_id='doc-1',
        measurement_id='m-sa-1',
        spec_id=spec.spec_id,
        spec_sha256=spec.spec_sha256,
        region=spec.region,
        producer='rew',
        producer_version='5.40b90',
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 72.0),
        created_at_utc='2026-09-25T00:01:00+00:00',
    )
    kwargs.update(overrides)
    return build_spatial_average_measurement(**kwargs)


def test_spec_sealed():
    spec = _spec()
    assert len(spec.spec_sha256) == 64
    MovingMicrophoneMeasurementSpec.model_validate(spec.model_dump(mode='json'))


def test_region_needs_id_or_bounds():
    with pytest.raises(ValidationError, match='region_id'):
        SpatialAverageRegion()


def test_tracked_representation_requires_samples():
    with pytest.raises(ValidationError, match='trajectory samples'):
        _spec(trajectory_representation='tracked')
    spec = _spec(
        trajectory_representation='tracked',
        trajectory=(
            TrajectorySample(t_s=0.0, position=_pos(0.5, 0.5)),
            TrajectorySample(t_s=0.1, position=_pos(0.6, 0.5)),
        ),
        trajectory_frame='scene',
    )
    assert spec.trajectory_representation == 'tracked'


def test_tracked_representation_requires_frame():
    with pytest.raises(ValidationError, match='coordinate frame'):
        _spec(
            trajectory_representation='tracked',
            trajectory=(
                TrajectorySample(t_s=0.0, position=_pos(0.5, 0.5)),
                TrajectorySample(t_s=0.1, position=_pos(0.6, 0.5)),
            ),
        )


def test_samples_forbidden_when_not_tracked():
    with pytest.raises(ValidationError, match='only valid'):
        _spec(
            trajectory=(
                TrajectorySample(t_s=0.0, position=_pos(0.5, 0.5)),
                TrajectorySample(t_s=0.1, position=_pos(0.6, 0.5)),
            ),
        )


def test_samples_must_be_time_ordered():
    with pytest.raises(ValidationError, match='time-ordered'):
        _spec(
            trajectory_representation='tracked',
            trajectory=(
                TrajectorySample(t_s=0.2, position=_pos(0.5, 0.5)),
                TrajectorySample(t_s=0.1, position=_pos(0.6, 0.5)),
            ),
            trajectory_frame='scene',
        )


def test_spatial_average_cannot_claim_point():
    measurement = _measurement()
    assert measurement.measurement_kind == MEASUREMENT_KIND_SPATIAL_AVERAGE_MOVING_MIC
    # the model has no receiver_position field — it cannot pose as a point
    assert not hasattr(measurement, 'receiver_position')
    SpatialAverageMeasurement.model_validate(
        measurement.model_dump(mode='json')
    )


def test_regional_identity_never_converted_to_point():
    measurement = _measurement()
    # guard passes for a well-formed regional result
    assert validate_regional_identity(measurement) is None
    manual = _measurement(
        spec=_spec(region=SpatialAverageRegion(region_id='seat-2'))
    )
    assert validate_regional_identity(manual) is None


def test_frequency_axis_strictly_increasing():
    with pytest.raises(ValidationError, match='increasing'):
        _measurement(
            frequency_hz=(20.0, 10.0, 80.0),
            level_db=(70.0, 71.0, 72.0),
        )
