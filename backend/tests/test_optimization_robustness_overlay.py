from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from htdt.cad_repository import SceneRevision
from htdt.cad_scene import Direction3, Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_search_models import CadCandidate
from htdt.optimization_robustness_overlay import build_robustness_overlay_model


def _fixture():
    document = SceneDocument(
        document_id='overlay-doc',
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker',
                kind='speaker',
                name='Speaker',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=1.5, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.4),
                aim_xyz=Direction3(x=1.0, y=0.0, z=0.0),
            ),
            SceneEntity(
                entity_id='seat',
                kind='seat',
                name='Seat',
                position=Position3(x_m=3.0, y_m=2.5, z_m=1.0),
                size_m=Size3(x_m=0.6, y_m=0.6, z_m=1.0),
            ),
        ),
    )
    revision = SceneRevision(
        revision_id='rev-1',
        document_id=document.document_id,
        parent_revision_id=None,
        created_at_utc='2026-09-20T00:00:00+00:00',
        content_hash='a' * 64,
        document=document,
    )
    candidate = CadCandidate(
        candidate_id='candidate-1',
        raw_index=0,
        feasible_index=0,
        positions={
            'speaker': {'x_m': 1.2, 'y_m': 1.5, 'z_m': 1.0},
            'seat': {'x_m': 3.0, 'y_m': 2.5, 'z_m': 1.0},
        },
    )
    axes = (
        SimpleNamespace(
            axis_id='speaker-x',
            entity_id='speaker',
            parameter='speaker_x_m',
            nominal_value=1.2,
            minus_delta=0.05,
            plus_delta=0.10,
        ),
        SimpleNamespace(
            axis_id='speaker-aim',
            entity_id='speaker',
            parameter='aim_yaw_deg',
            nominal_value=0.0,
            minus_delta=10.0,
            plus_delta=20.0,
        ),
    )
    spec = SimpleNamespace(
        document_id=document.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        candidate_kind='cad_candidate',
        candidate_payload_json=json.dumps(
            candidate.model_dump(mode='json'),
            sort_keys=True,
            separators=(',', ':'),
        ),
        axes=axes,
    )
    return revision, spec


def test_overlay_uses_candidate_nominal_position_and_declared_bounds() -> None:
    revision, spec = _fixture()
    overlay = build_robustness_overlay_model(
        source_revision=revision,
        spec=spec,
    )

    assert overlay.document.entity('speaker').position.x_m == pytest.approx(1.2)
    position = overlay.position_tolerances[0]
    assert position.nominal == pytest.approx((1.2, 1.5, 1.0))
    assert position.minimum == pytest.approx((1.15, 1.5, 1.0))
    assert position.maximum == pytest.approx((1.3, 1.5, 1.0))


def test_overlay_aim_rays_preserve_asymmetric_declared_yaw_tolerance() -> None:
    revision, spec = _fixture()
    overlay = build_robustness_overlay_model(
        source_revision=revision,
        spec=spec,
        ray_length_m=1.0,
    )

    aim = overlay.angular_tolerances[0]
    assert aim.origin == pytest.approx((1.2, 1.5, 1.0))
    assert aim.nominal_endpoint == pytest.approx((2.2, 1.5, 1.0))
    assert aim.minus_endpoint[1] < 1.5
    assert aim.plus_endpoint[1] > 1.5


def test_overlay_rejects_wrong_scene_revision_authority() -> None:
    revision, spec = _fixture()
    changed = SceneRevision(
        revision_id='rev-other',
        document_id=revision.document_id,
        parent_revision_id=None,
        created_at_utc=revision.created_at_utc,
        content_hash=revision.content_hash,
        document=revision.document,
    )

    with pytest.raises(ValueError, match='SceneRevision authority mismatch'):
        build_robustness_overlay_model(
            source_revision=changed,
            spec=spec,
        )


def test_overlay_marks_infeasible_position_perturbation_without_mutating_scene() -> None:
    revision, spec = _fixture()
    sample = SimpleNamespace(
        sample_id='sample-infeasible',
        feasible=False,
        parameter_deltas={'speaker-x': -0.05},
    )
    overlay = build_robustness_overlay_model(
        source_revision=revision,
        spec=spec,
        samples=(sample,),
    )

    assert len(overlay.infeasible_markers) == 1
    marker = overlay.infeasible_markers[0]
    assert marker.entity_id == 'speaker'
    assert marker.point == pytest.approx((1.15, 1.5, 1.0))
    assert revision.document.entity('speaker').position.x_m == pytest.approx(1.0)
