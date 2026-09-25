"""Issue #974: spatial room impulse response authority — array geometry,
exact pose provenance, per-channel bindings, directional event semantics."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_scene import Direction3, Position3
from htdt.cad_spatial_ir_measurement import (
    ArrayPose,
    DirectionalReflectionEvent,
    SpatialIrChannelBinding,
    SpatialMeasurementArrayProfile,
    build_array_profile,
    build_spatial_ir_dataset,
    validate_spatial_dataset_geometry,
)

_H = 'f' * 64


def _dir(x, y, z):
    length = (x**2 + y**2 + z**2) ** 0.5
    return Direction3(x=x / length, y=y / length, z=z / length)


def _profile(**overrides):
    kwargs = dict(
        profile_id='arr-1',
        schema_version='array_v1',
        array_kind='ambisonic_foa',
        receiver_positions=(
            Position3(x_m=0.02, y_m=0.0, z_m=0.0),
            Position3(x_m=-0.02, y_m=0.0, z_m=0.0),
            Position3(x_m=0.0, y_m=0.02, z_m=0.0),
            Position3(x_m=0.0, y_m=-0.02, z_m=0.0),
        ),
        channel_mapping=('W', 'X', 'Y', 'Z'),
        handedness='right_handed',
        sample_rate_hz=48000.0,
        geometry_uncertainty_m=0.001,
    )
    kwargs.update(overrides)
    return build_array_profile(**kwargs)


def _pose(**overrides):
    kwargs = dict(
        position=Position3(x_m=2.0, y_m=3.0, z_m=1.2),
        orientation_xyz_deg=(0.0, 0.0, 90.0),
        frame_semantics='yaw_pitch_roll_scene',
        provenance='captured_annotated',
        orientation_uncertainty_deg=2.0,
    )
    kwargs.update(overrides)
    return ArrayPose(**kwargs)


def _dataset(profile=None, **overrides):
    profile = profile or _profile()
    kwargs = dict(
        dataset_id='srir-1',
        schema_version='srir_v1',
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_H,
        array_profile_id=profile.profile_id,
        array_profile_sha256=profile.profile_sha256,
        array_pose=_pose(),
        channels=tuple(
            SpatialIrChannelBinding(
                channel_index=i, ir_dataset_id=f'ir-{i}', ir_dataset_sha256=_H
            )
            for i in range(4)
        ),
        sample_rate_hz=48000.0,
        sofa_convention='none',
        created_at_utc='2026-09-25T00:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_spatial_ir_dataset(**kwargs)


def test_profile_sealed_and_requires_geometry():
    profile = _profile()
    assert profile.receiver_count == 4
    SpatialMeasurementArrayProfile.model_validate(profile.model_dump(mode='json'))
    with pytest.raises(ValidationError, match='array kind'):
        _profile(array_kind='unknown')
    with pytest.raises(ValidationError, match='handedness'):
        _profile(handedness='unknown')


def test_channel_mapping_aligns():
    with pytest.raises(ValidationError, match='channel_mapping'):
        _profile(channel_mapping=('W', 'X'))


def test_pose_provenance_required():
    with pytest.raises(ValidationError, match='provenance'):
        _dataset(array_pose=_pose(provenance='unknown'))


def test_sofa_import_keeps_producer_identity():
    dataset = _dataset(
        sofa_convention='SingleRoomSRIR',
        sofa_producer='external_lab',
        sofa_source_sha256=_H,
    )
    assert dataset.sofa_convention == 'SingleRoomSRIR'
    with pytest.raises(ValidationError, match='producer'):
        _dataset(sofa_convention='SingleRoomMIMOSRIR')


def test_geometry_validation_binds_channels():
    profile = _profile()
    dataset = _dataset(profile=profile)
    validate_spatial_dataset_geometry(dataset, profile)
    short = _dataset(profile=profile, channels=dataset.channels[:3])
    with pytest.raises(ValueError, match='every declared receiver'):
        validate_spatial_dataset_geometry(short, profile)
    other = _profile(profile_id='arr-2')
    with pytest.raises(ValueError, match='does not match'):
        validate_spatial_dataset_geometry(dataset, other)


def test_directional_event_requires_estimator_and_consistency():
    dataset = _dataset()
    with pytest.raises(ValidationError, match='estimator'):
        DirectionalReflectionEvent(
            event_id='ev-1',
            dataset_id=dataset.dataset_id,
            dataset_sha256=dataset.dataset_sha256,
            estimator='unknown',
            estimator_version='v1',
            delay_s=0.008,
            direction=_dir(1.0, 0.0, 0.0),
        )
    event = DirectionalReflectionEvent(
        event_id='ev-2',
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        estimator='sdm',
        estimator_version='sdm_v1',
        delay_s=0.008,
        direction=_dir(0.7, 0.7, 0.0),
        level_db=-12.0,
        confidence=0.8,
        associated_path_id='path-1',
        association_state='associated',
    )
    assert event.association_state == 'associated'
    with pytest.raises(ValidationError, match='associated_path_id'):
        DirectionalReflectionEvent(
            event_id='ev-3',
            dataset_id=dataset.dataset_id,
            dataset_sha256=dataset.dataset_sha256,
            estimator='sdm',
            estimator_version='sdm_v1',
            delay_s=0.008,
            direction=_dir(0.7, 0.7, 0.0),
            association_state='associated',
        )
