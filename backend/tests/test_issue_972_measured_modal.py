"""Issue #972: measured modal identification — derived measured-model
evidence, common-pole aggregation, reconstructed (not measured-everywhere)
mode shapes, conservative predicted-mode association."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_measured_modal_analysis import (
    MeasuredMode,
    MeasuredModeResidue,
    MeasuredModalAnalysisSpec,
    ReconstructedModeShape,
    aggregate_common_poles,
    associate_predicted_mode,
    build_measured_modal_model,
    build_modal_analysis_spec,
    reconstruct_mode_shape,
)
from htdt.cad_scene import Position3

_H = 'd' * 64


def _pos(x, y, z=1.0):
    return Position3(x_m=x, y_m=y, z_m=z)


def _spec(**overrides):
    kwargs = dict(
        spec_id='mas-1',
        schema_version='modal_v1',
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_H,
        measurement_ids=('m1', 'm2', 'm3'),
        measurement_positions=(_pos(0.5, 0.5), _pos(2.0, 0.5), _pos(3.0, 3.0)),
        ir_capability='measured_ir',
        analysis_band_hz=(10.0, 250.0),
        algorithm='matrix_pencil',
        algorithm_version='impl_v1',
        model_order=4,
        stability_checks=('window_perturbation', 'order_perturbation'),
        created_at_utc='2026-09-25T00:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_modal_analysis_spec(**kwargs)


def _mode(mode_id, freq, residues=()):
    return MeasuredMode(
        mode_id=mode_id,
        center_frequency_hz=freq,
        decay_rate_nepers_per_s=6.9,
        decay_time_s=6.907755278982137 / 6.9,
        residues=residues,
        stability='stable',
        snr_db=40.0,
    )


def test_spec_sealed_and_positions_align():
    spec = _spec()
    MeasuredModalAnalysisSpec.model_validate(spec.model_dump(mode='json'))
    with pytest.raises(ValidationError, match='1:1'):
        _spec(measurement_positions=(_pos(0.5, 0.5),))


def test_magnitude_only_capability_fails_closed():
    with pytest.raises(ValidationError, match='capability'):
        _spec(ir_capability='unknown')


def test_decay_consistency():
    with pytest.raises(ValidationError, match='T60'):
        MeasuredMode(
            mode_id='x',
            center_frequency_hz=30.0,
            decay_rate_nepers_per_s=2.0,
            decay_time_s=10.0,
        )


def test_common_pole_aggregation():
    per_pos = {
        'm1': (_mode('a1', 40.0), _mode('a2', 75.0)),
        'm2': (_mode('b1', 40.4), _mode('b2', 120.0)),
        'm3': (_mode('c1', 39.8),),
    }
    groups = aggregate_common_poles(per_pos, frequency_tolerance_hz=1.0)
    group_sets = {frozenset(g) for g in groups}
    assert frozenset({'a1', 'b1', 'c1'}) in group_sets
    assert frozenset({'a2'}) in group_sets
    assert frozenset({'b2'}) in group_sets


def test_mode_shape_is_reconstructed_only():
    mode = _mode(
        'm-1',
        42.0,
        residues=(
            MeasuredModeResidue(position_id='m1', amplitude=0.5),
            MeasuredModeResidue(position_id='m2', amplitude=1.0),
        ),
    )
    shape = reconstruct_mode_shape(mode, evaluation_positions=(_pos(0.5, 0.5), _pos(2.0, 0.5)))
    assert shape.kind == 'reconstructed'
    assert shape.normalized_values == (0.5, 1.0)
    with pytest.raises(ValueError, match='explicit'):
        reconstruct_mode_shape(mode)


def test_shape_cannot_exceed_normalization():
    with pytest.raises(ValidationError, match='exceed 1'):
        ReconstructedModeShape(
            mode_id='m-1',
            kind='reconstructed',
            evaluation_positions=(_pos(0.0, 0.0),),
            normalized_values=(1.5,),
            normalization='max_amplitude',
        )


def test_nearest_frequency_cannot_assert_identity():
    mode = _mode('m-1', 43.0)
    assert (
        associate_predicted_mode(
            mode,
            predicted_mode_id='pm-1',
            predicted_frequency_hz=43.2,
            frequency_tolerance_hz=1.0,
            spatial_pattern_agreement=None,
        )
        == 'ambiguous'
    )
    assert (
        associate_predicted_mode(
            mode,
            predicted_mode_id='pm-1',
            predicted_frequency_hz=43.2,
            frequency_tolerance_hz=1.0,
            spatial_pattern_agreement=True,
        )
        == 'associated'
    )
    assert (
        associate_predicted_mode(
            mode,
            predicted_mode_id='pm-2',
            predicted_frequency_hz=90.0,
            frequency_tolerance_hz=1.0,
        )
        == 'unmatched'
    )


def test_model_sealed():
    model = build_measured_modal_model(
        model_id='mm-1',
        spec=_spec(),
        modes=(_mode('m-1', 42.0),),
        created_at_utc='2026-09-25T00:01:00+00:00',
    )
    assert len(model.model_sha256) == 64
    payload = model.model_dump(mode='json')
    payload['modes'][0]['center_frequency_hz'] = 44.0
    from htdt.cad_measured_modal_analysis import MeasuredModalModel

    with pytest.raises(ValidationError, match='hash mismatch'):
        MeasuredModalModel.model_validate(payload)
