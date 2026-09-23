"""Direct-view display authority tests (#637)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_direct_view import (
    CAPTURE_ENTITY_TYPE_TO_SCENE_KIND,
    DirectViewDisplaySpecification,
    DirectViewGeometryRequest,
    DisplayGeometryBinding,
    DisplayPhotometricCapability,
    DisplayVideoCapability,
    build_direct_view_display_specification,
    build_direct_view_geometry_request,
    capture_entity_type_to_scene_kind,
    evaluate_direct_view_geometry,
)
from htdt.cad_direct_view_repository import CadDirectViewRepository
from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    PHYSICAL_ENTITY_KINDS,
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_video_geometry import (
    AngleRange,
    SeatGeometryBinding,
    SightlineSample,
    VideoGeometryPolicy,
)


DOCUMENT_ID = 'direct-view-fixture'
NOW = '2026-09-23T00:00:00+00:00'


def _display() -> SceneEntity:
    return SceneEntity(
        entity_id='display-main',
        kind='display',
        name='65in OLED',
        position=Position3(x_m=3.0, y_m=0.2, z_m=1.1),
        size_m=Size3(x_m=1.50, y_m=0.06, z_m=0.85),
    )


def _seat(entity_id: str, *, y_m: float, z_m: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='seat',
        name=entity_id,
        position=Position3(x_m=3.0, y_m=y_m, z_m=z_m),
        size_m=Size3(x_m=0.8, y_m=0.8, z_m=1.0),
    )


def _speaker() -> SceneEntity:
    return SceneEntity(
        entity_id='speaker-c',
        kind='speaker',
        name='Center',
        speaker_role='C',
        position=Position3(x_m=0.7, y_m=0.7, z_m=0.3),
        size_m=Size3(x_m=0.4, y_m=0.3, z_m=0.3),
    )


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        room=RoomPrism(width_m=6.0, depth_m=5.0, height_m=3.0),
        entities=(
            _display(),
            _seat('seat-front', y_m=2.2, z_m=0.5),
            _seat('seat-rear', y_m=4.2, z_m=0.5),
            _speaker(),
        ),
    )


def _baseline(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    return scene_repository, revision


def _provenance() -> tuple[EquipmentDataProvenance, ...]:
    return (
        EquipmentDataProvenance(
            evidence_kind='manufacturer',
            source_name='Example Display Co.',
            source_version='2026.1',
            source_reference='Model OLED-65 datasheet',
            source_sha256='a' * 64,
        ),
    )


def _spec() -> DirectViewDisplaySpecification:
    return build_direct_view_display_specification(
        specification_id='example-oled-65',
        version='2026.1',
        manufacturer='Example Display Co.',
        model='OLED-65',
        user_label=None,
        display_class='oled',
        chassis_size_m=Size3(x_m=1.50, y_m=0.06, z_m=0.85),
        active_image_width_m=1.45,
        active_image_height_m=0.82,
        supported_mountings=('wall', 'stand'),
        video_capability=DisplayVideoCapability(
            native_width_px=3840,
            native_height_px=2160,
            max_refresh_hz=120.0,
            hdr_format_families=('hdr10', 'dolby_vision'),
            earc_supported=True,
            hdmi_input_count=4,
        ),
        photometric_capability=DisplayPhotometricCapability(
            peak_luminance_cd_m2=800.0,
            black_level_cd_m2=0.0005,
        ),
        provenance=_provenance(),
    )


def _binding() -> DisplayGeometryBinding:
    return DisplayGeometryBinding(
        entity_id='display-main',
        visible_width_m=1.45,
        visible_height_m=0.82,
        frame_clearance_m=0.03,
        mounting='wall',
    )


def _request(spec=None):
    kwargs = {}
    if spec is not None:
        kwargs['display_specification'] = spec
    return build_direct_view_geometry_request(
        display=_binding(),
        seats=(
            SeatGeometryBinding(
                entity_id='seat-front',
                row_id='row-front',
                eye_reference_offset_local_m=Offset3(z_m=0.65),
                head_center_offset_local_m=Offset3(z_m=0.65),
                head_radius_m=0.16,
            ),
            SeatGeometryBinding(
                entity_id='seat-rear',
                row_id='row-rear',
                eye_reference_offset_local_m=Offset3(z_m=0.65),
                head_center_offset_local_m=Offset3(z_m=0.65),
                head_radius_m=0.16,
            ),
        ),
        policy=VideoGeometryPolicy(
            horizontal_viewing_angle_deg=AngleRange(
                minimum_deg=20.0, maximum_deg=80.0
            ),
            vertical_viewing_angle_deg=AngleRange(
                minimum_deg=5.0, maximum_deg=40.0
            ),
            center_elevation_angle_deg=AngleRange(
                minimum_deg=-15.0, maximum_deg=20.0
            ),
            sightline_samples=(
                SightlineSample(
                    sample_id='bottom-center',
                    horizontal_fraction=0.5,
                    vertical_fraction=0.0,
                ),
            ),
            sightline_clearance_m=0.03,
            riser_support_tolerance_m=0.005,
            max_optical_axis_deviation_deg=0.1,
            collision_clearance_m=0.02,
        ),
        collision_entity_ids=('display-main', 'speaker-c'),
        **kwargs,
    )


def test_display_is_a_physical_scene_kind():
    assert 'display' in PHYSICAL_ENTITY_KINDS
    entity = _display()
    assert entity.kind == 'display'
    assert entity.size_m is not None


def test_display_requires_size():
    with pytest.raises(ValidationError, match='size_m'):
        SceneEntity(
            entity_id='display-x',
            kind='display',
            name='display',
            position=Position3(x_m=0, y_m=0, z_m=0),
        )


def test_display_rejects_speaker_fields():
    with pytest.raises(ValidationError, match='speaker fields'):
        SceneEntity(
            entity_id='display-x',
            kind='display',
            name='display',
            position=Position3(x_m=0, y_m=0, z_m=0),
            size_m=Size3(x_m=1.5, y_m=0.06, z_m=0.85),
            speaker_role='C',
        )


def test_capture_promotion_map_preserves_display_semantics():
    assert CAPTURE_ENTITY_TYPE_TO_SCENE_KIND['display'] == 'display'
    assert CAPTURE_ENTITY_TYPE_TO_SCENE_KIND['projection_screen'] == 'screen'
    assert CAPTURE_ENTITY_TYPE_TO_SCENE_KIND['projector'] == 'projector'
    assert capture_entity_type_to_scene_kind('display') == 'display'
    assert capture_entity_type_to_scene_kind('x_unknown') is None


def test_specification_hash_and_provenance():
    spec = _spec()
    assert spec.specification_sha256
    other = build_direct_view_display_specification(
        specification_id='example-oled-65',
        version='2026.1',
        manufacturer='Example Display Co.',
        model='OLED-65',
        user_label=None,
        display_class='lcd',
        chassis_size_m=Size3(x_m=1.50, y_m=0.06, z_m=0.85),
        active_image_width_m=1.45,
        active_image_height_m=0.82,
        provenance=_provenance(),
    )
    assert spec.specification_sha256 != other.specification_sha256


def test_specification_rejects_tampered_hash():
    payload = _spec().model_dump(mode='python')
    payload['active_image_width_m'] = 1.20
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        DirectViewDisplaySpecification(**payload)


def test_specification_rejects_oversized_active_area():
    with pytest.raises(ValueError, match='inside chassis'):
        build_direct_view_display_specification(
            specification_id='bad-spec',
            version='1',
            manufacturer=None,
            model=None,
            user_label='custom',
            display_class='other',
            chassis_size_m=Size3(x_m=1.0, y_m=0.05, z_m=0.6),
            active_image_width_m=1.45,
            active_image_height_m=0.82,
            provenance=(
                EquipmentDataProvenance(
                    evidence_kind='user_defined',
                    source_name='user',
                    source_version='1',
                    source_reference='manual entry',
                    source_sha256='b' * 64,
                ),
            ),
        )


def test_request_spec_pin_is_all_or_none():
    request = _request()
    with pytest.raises(ValidationError, match='supplied together'):
        DirectViewGeometryRequest(
            display=_binding(),
            display_specification_id='spec-only',
            display_specification_version=None,
            display_specification_sha256=None,
            seats=request.seats,
            policy=request.policy,
            collision_entity_ids=(),
            request_sha256='0' * 64,
        )


def test_evaluation_runs_without_projector(tmp_path: Path):
    scene_repository, revision = _baseline(tmp_path)
    evaluation = evaluate_direct_view_geometry(
        baseline=revision,
        variant=None,
        request=_request(),
    )
    assert evaluation.evaluation_id.startswith('dvge-')
    assert evaluation.projection_status == 'NOT_APPLICABLE'
    assert evaluation.surface.chassis_containment_status == 'PASS'
    # No spec bound -> conformance stays UNKNOWN rather than guessed, and
    # the combined surface status reflects that honestly.
    assert evaluation.surface.spec_conformance_status == 'UNKNOWN'
    assert evaluation.surface.status == 'UNKNOWN'
    assert evaluation.surface.diagonal_m == pytest.approx(
        (1.45**2 + 0.82**2) ** 0.5
    )
    assert len(evaluation.viewing) == 2
    front = next(
        item for item in evaluation.viewing
        if item.seat_entity_id == 'seat-front'
    )
    assert front.horizontal_status == 'PASS'
    assert evaluation.geometry_status == 'PASS'


def test_evaluation_with_spec_conformance(tmp_path: Path):
    _, revision = _baseline(tmp_path)
    spec = _spec()
    evaluation = evaluate_direct_view_geometry(
        baseline=revision,
        variant=None,
        display_specification=spec,
        request=_request(spec=spec),
    )
    assert evaluation.surface.spec_conformance_status == 'PASS'
    assert evaluation.display_specification_sha256 == (
        spec.specification_sha256
    )


def test_spec_binding_mismatch_rejected(tmp_path: Path):
    _, revision = _baseline(tmp_path)
    with pytest.raises(ValueError, match='specification binding mismatch'):
        evaluate_direct_view_geometry(
            baseline=revision,
            variant=None,
            display_specification=_spec(),
            request=_request(),  # no spec pinned
        )


def test_display_binding_rejects_screen_entity(tmp_path: Path):
    scene = _scene().model_copy(
        update={
            'entities': (
                SceneEntity(
                    entity_id='display-main',
                    kind='screen',
                    name='fake screen',
                    position=Position3(x_m=3.0, y_m=0.2, z_m=1.1),
                    size_m=Size3(x_m=1.5, y_m=0.06, z_m=0.85),
                ),
            )
            + _scene().entities[1:]
        }
    )
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(scene, parent_revision_id=None).revision
    with pytest.raises(ValueError, match='display'):
        evaluate_direct_view_geometry(
            baseline=revision, variant=None, request=_request()
        )


def test_repository_round_trip(tmp_path: Path):
    scene_repository, revision = _baseline(tmp_path)
    repository = CadDirectViewRepository(scene_repository)
    spec = _spec()
    repository.save_specification(spec)
    evaluation = evaluate_direct_view_geometry(
        baseline=revision,
        variant=None,
        display_specification=spec,
        request=_request(spec=spec),
    )
    repository.save_evaluation(evaluation)
    loaded_spec = repository.get_specification('example-oled-65', '2026.1')
    assert loaded_spec == spec
    loaded = repository.get_evaluation(evaluation.evaluation_id)
    assert loaded == evaluation
    listed = repository.list_evaluations_for_revision(revision.revision_id)
    assert [item.evaluation_id for item in listed] == [
        evaluation.evaluation_id
    ]
