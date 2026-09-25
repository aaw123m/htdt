"""Issue #1046: simultaneous multi-receiver authority — per-input pose,
calibration, timing claims, derived averages that never pose as receivers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_multi_receiver_acquisition import (
    DerivedReceiverAverage,
    MultiReceiverAcquisition,
    MultiReceiverChannel,
    build_derived_receiver_average,
    build_multi_receiver_acquisition,
    map_acquisition_to_plan_cells,
)
from htdt.cad_scene import Position3

_H = '6' * 64


def _pos(x, y, z=1.2):
    return Position3(x_m=x, y_m=y, z_m=z)


def _channel(channel_id='ch1', index=0, **overrides):
    kwargs = dict(
        channel_id=channel_id,
        channel_index=index,
        microphone_ref='umik-1',
        response_calibration_ref='cal-1',
        receiver_position=_pos(1.0 + index, 1.0),
        quality_state='valid',
    )
    kwargs.update(overrides)
    return MultiReceiverChannel(**kwargs)


def _acquisition(channels=None, **overrides):
    kwargs = dict(
        acquisition_id='acq-1',
        schema_version='mra_v1',
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_H,
        producer='rew',
        producer_version='5.40b90',
        sample_rate_hz=48000.0,
        capture_mode='simultaneous',
        timing_capabilities=('simultaneous_capture', 'common_clock'),
        channels=channels or (_channel('ch1', 0), _channel('ch2', 1)),
        observed_at_utc='2026-09-25T00:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_multi_receiver_acquisition(**kwargs)


def test_acquisition_sealed():
    acq = _acquisition()
    assert len(acq.acquisition_sha256) == 64
    MultiReceiverAcquisition.model_validate(acq.model_dump(mode='json'))


def test_channel_needs_pose_or_unknown():
    with pytest.raises(ValidationError, match='explicit'):
        MultiReceiverChannel(channel_id='c', channel_index=0)


def test_unique_channels_required():
    with pytest.raises(ValidationError, match='unique'):
        _acquisition(channels=(_channel('ch1', 0), _channel('ch1', 1)))


def test_simultaneous_claim_requires_simultaneous_mode():
    with pytest.raises(ValidationError, match='simultaneous'):
        _acquisition(capture_mode='sequential')


def test_timing_reference_claim_needs_binding():
    with pytest.raises(ValidationError, match='timing reference'):
        _acquisition(
            timing_capabilities=('simultaneous_capture', 'timing_reference_bound')
        )
    ok = _acquisition(
        timing_capabilities=('simultaneous_capture', 'timing_reference_bound'),
        timing_reference_id='ref-1',
    )
    assert 'timing_reference_bound' in ok.timing_capabilities


def test_derived_average_sealed_and_membership():
    acq = _acquisition()
    avg = build_derived_receiver_average(
        acq,
        average_id='avg-1',
        schema_version='mra_v1',
        member_channel_ids=('ch1', 'ch2'),
        method='rms',
        region_ref='seat-area',
        created_at_utc='2026-09-25T00:01:00+00:00',
    )
    assert len(avg.average_sha256) == 64
    DerivedReceiverAverage.model_validate(avg.model_dump(mode='json'))


def test_average_members_must_exist():
    acq = _acquisition()
    with pytest.raises(ValueError, match='not a channel'):
        build_derived_receiver_average(
            acq,
            average_id='avg-1',
        schema_version='mra_v1',
            member_channel_ids=('ch9',),
            method='rms',
            created_at_utc='t',
        )


def test_average_is_not_a_receiver():
    acq = _acquisition()
    with pytest.raises(ValidationError, match='physical receiver'):
        build_derived_receiver_average(
            acq,
            average_id='avg-1',
        schema_version='mra_v1',
            member_channel_ids=('ch1',),
            method='rms',
            receiver_position=_pos(1.0, 1.0),
            created_at_utc='t',
        )


def test_weighted_average_needs_weights():
    acq = _acquisition()
    with pytest.raises(ValidationError, match='weight'):
        build_derived_receiver_average(
            acq,
            average_id='avg-1',
        schema_version='mra_v1',
            member_channel_ids=('ch1', 'ch2'),
            method='weighted',
            created_at_utc='t',
        )
    avg = build_derived_receiver_average(
        acq,
        average_id='avg-2',
        schema_version='mra_v1',
        member_channel_ids=('ch1', 'ch2'),
        method='weighted',
        weights=(0.7, 0.3),
        created_at_utc='t',
    )
    assert avg.method == 'weighted'


def test_plan_cell_mapping_is_per_channel():
    acq = _acquisition(
        channels=(
            _channel('ch1', 0),
            _channel('ch2', 1, quality_state='failed', receiver_position=_pos(2.0, 1.0)),
        )
    )
    mapped = map_acquisition_to_plan_cells(
        acq, channel_cell_map={'ch1': 'cell-a', 'ch2': 'cell-b'}
    )
    # failed channel yields no cell — its retake is independent
    assert mapped == {'cell-a': 'ch1'}
    with pytest.raises(ValueError, match='unknown channel'):
        map_acquisition_to_plan_cells(acq, channel_cell_map={'ghost': 'cell-z'})
