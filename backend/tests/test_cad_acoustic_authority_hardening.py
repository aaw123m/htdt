from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from htdt.acoustic_benchmark import GeometricAcousticBand
from htdt.cad_acoustic_environment import (
    build_acoustic_environment_profile,
    sound_speed_from_temperature_c,
)
from htdt.cad_acoustic_material import build_acoustic_material
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
    SensitivityReference,
    build_equipment_definition,
)
from htdt.cad_listener_pose import (
    CadListenerPoseRepository,
    build_listener_pose,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_screen_transfer import (
    CadScreenTransferRepository,
    TransferSample,
    build_screen_transfer,
)
from htdt.cad_source_response import (
    CadSourceResponseRepository,
    SourceResponseCondition,
    SourceResponseSample,
    build_source_response,
)
from htdt.cad_video_geometry import (
    AngleRange,
    ScreenGeometryBinding,
    SeatGeometryBinding,
    SightlineSample,
    VideoGeometryPolicy,
    VideoGeometryRequest,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef
from htdt.semantic_geometry import SemanticSurface
from htdt.acoustics.persistence.cad_acoustic_material_repository import CadAcousticMaterialRepository


NOW = '2026-09-19T13:00:00+00:00'


def _provenance(source_sha256: str, name: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='measured',
        source_name=name,
        source_version='2026-09-19',
        source_reference=f'{name}-fixture',
        source_sha256=source_sha256,
    )


def _definition(definition_id: str):
    provenance = _provenance('a' * 64, definition_id)
    directivity = DirectivityCapability(
        tier='magnitude_only',
        data_format='custom',
        provenance=provenance,
        data_asset_sha256='a' * 64,
        valid_domain=DirectivityDomain(
            frequency=FrequencyDomain(minimum_hz=500.0, maximum_hz=1000.0),
            horizontal=AngleDomain(minimum_deg=-30.0, maximum_deg=30.0),
            vertical=AngleDomain(minimum_deg=0.0, maximum_deg=0.0),
        ),
        interpolation=InterpolationProvenance(
            method='linear',
            implementation='test-grid-linear',
            implementation_version='1',
            provenance=provenance,
        ),
    )
    return build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(x_m=0.0, y_m=0.1, z_m=0.0),
        sensitivity=SensitivityReference(
            level_db_spl=88.0,
            input_quantity='voltage_v_rms',
            input_value=2.83,
            distance_m=1.0,
            valid_frequency_domain=FrequencyDomain(
                minimum_hz=500.0,
                maximum_hz=1000.0,
            ),
            provenance=provenance,
        ),
        directivity=directivity,
    )


def _spl_condition() -> SourceResponseCondition:
    return SourceResponseCondition(
        input_quantity='voltage_v_rms',
        input_value=2.83,
        reference_distance_m=1.0,
        field_condition='free_field',
        calibration='calibrated mic #42',
    )


def _seat() -> SceneEntity:
    return SceneEntity(
        entity_id='seat-1',
        kind='seat',
        name='MLP seat',
        position=Position3(x_m=2.0, y_m=3.0, z_m=0.0),
        size_m=Size3(x_m=0.6, y_m=0.6, z_m=1.0),
        acoustic_reference_offset_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.1),
    )


def _surfaces() -> tuple[SemanticSurface, SemanticSurface]:
    return (
        SemanticSurface(
            surface_id='semantic-surface:' + '1' * 64,
            surface_key='wall-left',
            semantic_class='room_boundary',
            triangle_ids=('t1',),
            assignment_provenance='explicit',
        ),
        SemanticSurface(
            surface_id='semantic-surface:' + '2' * 64,
            surface_key='wall-right',
            semantic_class='room_boundary',
            triangle_ids=('t2',),
            assignment_provenance='explicit',
        ),
    )


def _pose(pose_id: str | None = None, document_id: str | None = None):
    return build_listener_pose(
        seat_entity_id='seat-1',
        document_id=document_id,
        label='upright',
        head_center_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.2),
        eye_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.15),
        acoustic_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.1),
        provenance='fixture',
        pose_id=pose_id,
        created_at_utc=NOW,
    )


def _transfer(
    transfer_id: str | None = None, document_id: str | None = None
):
    return build_screen_transfer(
        screen_entity_id='screen-1',
        document_id=document_id,
        label='woven AT screen',
        capability_tier='AT_CLAIM',
        provenance='vendor-claim',
        transfer_id=transfer_id,
        created_at_utc=NOW,
    )


def _response(response_id: str | None = None):
    return build_source_response(
        equipment_definition=_definition('sp-imm'),
        label='on-axis SPL sweep',
        capability_tier='ABSOLUTE_FREE_FIELD_SPL',
        provenance='lab',
        condition=_spl_condition(),
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=100.0, maximum_hz=5000.0
        ),
        response_samples=(
            SourceResponseSample(frequency_hz=500.0, magnitude_db_spl=88.0),
        ),
        response_id=response_id,
        created_at_utc=NOW,
    )


def _material(material_id: str | None = None):
    return build_acoustic_material(
        label='石膏ボード',
        provenance='datasheet',
        wave_model='rigid',
        material_id=material_id,
        created_at_utc=NOW,
    )


# ---------------------------------------------------------------------------
# #789.A + #828 — save is idempotent-or-reject, never an update in place
# ---------------------------------------------------------------------------


def test_save_is_idempotent_and_rejects_id_collision(tmp_path: Path) -> None:
    materials = CadAcousticMaterialRepository(tmp_path / 'materials.sqlite3')
    poses = CadListenerPoseRepository(tmp_path / 'poses.sqlite3')
    transfers = CadScreenTransferRepository(tmp_path / 'transfers.sqlite3')
    responses = CadSourceResponseRepository(tmp_path / 'responses.sqlite3')

    material = _material()
    pose = _pose()
    transfer = _transfer()
    response = _response()
    for save, authority in (
        (materials.save_material, material),
        (poses.save_pose, pose),
        (transfers.save_transfer, transfer),
        (responses.save_response, response),
    ):
        save(authority)
        save(authority)  # same id + identical payload -> no-op

    pose_b = build_listener_pose(
        seat_entity_id='seat-1',
        label='reclined',
        head_center_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.0),
        eye_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.95),
        acoustic_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.1),
        provenance='fixture',
        pose_id=pose.pose_id,
        created_at_utc=NOW,
    )
    transfer_b = build_screen_transfer(
        screen_entity_id='screen-1',
        label='different label',
        capability_tier='AT_CLAIM',
        provenance='vendor-claim',
        transfer_id=transfer.transfer_id,
        created_at_utc=NOW,
    )
    response_b = build_source_response(
        equipment_definition=_definition('sp-imm'),
        label='different label',
        capability_tier='ABSOLUTE_FREE_FIELD_SPL',
        provenance='lab',
        condition=_spl_condition(),
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=100.0, maximum_hz=5000.0
        ),
        response_samples=(
            SourceResponseSample(frequency_hz=500.0, magnitude_db_spl=88.0),
        ),
        response_id=response.response_id,
        created_at_utc=NOW,
    )
    material_b = build_acoustic_material(
        label='MDF',
        provenance='datasheet',
        wave_model='rigid',
        material_id=material.material_id,
        created_at_utc=NOW,
    )
    for save, authority in (
        (materials.save_material, material_b),
        (poses.save_pose, pose_b),
        (transfers.save_transfer, transfer_b),
        (responses.save_response, response_b),
    ):
        with pytest.raises(ValueError, match='collision'):
            save(authority)


def test_selection_requires_persisted_authority(tmp_path: Path) -> None:
    materials = CadAcousticMaterialRepository(tmp_path / 'materials.sqlite3')
    poses = CadListenerPoseRepository(tmp_path / 'poses.sqlite3')
    transfers = CadScreenTransferRepository(tmp_path / 'transfers.sqlite3')
    responses = CadSourceResponseRepository(tmp_path / 'responses.sqlite3')

    with pytest.raises(ValueError, match='not persisted'):
        poses.select_pose('doc-1', _pose())
    with pytest.raises(ValueError, match='not persisted'):
        transfers.select_transfer('doc-1', _transfer())
    with pytest.raises(ValueError, match='not persisted'):
        responses.select_response('doc-1', _response())
    with pytest.raises(ValueError, match='not persisted'):
        materials.assign_material(
            'doc-1', 'semantic-surface:' + '1' * 64, _material()
        )


def test_project_local_pose_and_transfer_cannot_cross_documents(
    tmp_path: Path,
) -> None:
    # Two projects can share a local entity id ('seat-1'); a pose authored
    # for one document can never be selected into another.
    poses = CadListenerPoseRepository(tmp_path / 'poses.sqlite3')
    transfers = CadScreenTransferRepository(tmp_path / 'transfers.sqlite3')

    pose = _pose(document_id='doc-a')
    poses.save_pose(pose)
    with pytest.raises(ValueError, match='authored for document'):
        poses.select_pose('doc-b', pose)
    poses.select_pose('doc-a', pose)
    assert poses.selected_pose('doc-a', 'seat-1') == pose

    transfer = _transfer(document_id='doc-a')
    transfers.save_transfer(transfer)
    with pytest.raises(ValueError, match='authored for document'):
        transfers.select_transfer('doc-b', transfer)
    transfers.select_transfer('doc-a', transfer)
    assert transfers.selected_transfer('doc-a', 'screen-1') == transfer


def _scene_with_seat_and_screen(tmp_path: Path) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='fixture-doc',
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.5),
        entities=(
            _seat(),
            SceneEntity(
                entity_id='screen-1',
                kind='screen',
                name='Screen',
                position=Position3(x_m=2.0, y_m=0.0, z_m=1.0),
                size_m=Size3(x_m=2.6, y_m=0.1, z_m=1.1),
            ),
        ),
    )
    scene_repository.save(document, parent_revision_id=None)
    return scene_repository


def test_select_validates_document_and_entity_when_scene_repo_wired(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_with_seat_and_screen(tmp_path)
    poses = CadListenerPoseRepository(
        tmp_path / 'poses.sqlite3', scene_repository
    )
    transfers = CadScreenTransferRepository(
        tmp_path / 'transfers.sqlite3', scene_repository
    )

    pose = _pose()
    poses.save_pose(pose)
    transfer = _transfer()
    transfers.save_transfer(transfer)

    poses.select_pose('fixture-doc', pose)
    transfers.select_transfer('fixture-doc', transfer)

    with pytest.raises(ValueError, match='does not exist'):
        poses.select_pose('other-doc', pose)
    with pytest.raises(ValueError, match='does not exist'):
        transfers.select_transfer('other-doc', transfer)

    foreign_pose = build_listener_pose(
        seat_entity_id='seat-absent',
        label='upright',
        head_center_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.2),
        eye_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.15),
        acoustic_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.1),
        provenance='fixture',
        created_at_utc=NOW,
    )
    poses.save_pose(foreign_pose)
    with pytest.raises(ValueError, match='does not exist in document'):
        poses.select_pose('fixture-doc', foreign_pose)

    wrong_kind_transfer = build_screen_transfer(
        screen_entity_id='seat-1',
        label='not a screen',
        capability_tier='AT_CLAIM',
        provenance='fixture',
        created_at_utc=NOW,
    )
    transfers.save_transfer(wrong_kind_transfer)
    with pytest.raises(ValueError, match='not a screen'):
        transfers.select_transfer('fixture-doc', wrong_kind_transfer)


def test_material_assignment_surfaces_and_stale_reads(tmp_path: Path) -> None:
    repository = CadAcousticMaterialRepository(tmp_path / 'materials.sqlite3')
    material = _material()
    repository.save_material(material)
    surfaces = _surfaces()

    # Surface not present in the supplied geometry authority -> reject.
    with pytest.raises(ValueError, match='not present'):
        repository.assign_material(
            'doc-1',
            'semantic-surface:' + '9' * 64,
            material,
            surfaces=surfaces,
        )

    repository.assign_material(
        'doc-1', surfaces[0].surface_id, material, surfaces=surfaces
    )

    # A dangling assignment (surface absent from the passed geometry) is an
    # explicit failure at read-side, not a silently dropped binding.
    repository.assign_material('doc-1', 'semantic-surface:' + '5' * 64, material)
    with pytest.raises(ValueError, match='missing'):
        repository.boundary_bindings('doc-1', surfaces)
    repository.clear_assignment('doc-1', 'semantic-surface:' + '5' * 64)
    bindings = repository.boundary_bindings('doc-1', surfaces)
    assert [b.source_surface_id for b in bindings] == [
        surfaces[0].surface_id
    ]


# ---------------------------------------------------------------------------
# #789.F/G + #829.B-E — tier evidence semantics + sample-domain forged claims
# ---------------------------------------------------------------------------


def test_relative_and_absolute_tier_evidence_requirements() -> None:
    definition = _definition('sp-tiers')
    condition = _spl_condition()
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=5000.0)

    # RELATIVE_ON_AXIS_MAGNITUDE: magnitude required on every sample — a
    # phase-only sample is not relative-magnitude evidence.
    with pytest.raises(ValueError, match='magnitude'):
        build_source_response(
            equipment_definition=definition,
            label='phase only',
            capability_tier='RELATIVE_ON_AXIS_MAGNITUDE',
            provenance='lab',
            condition=condition,
            valid_frequency_domain=domain,
            response_samples=(
                SourceResponseSample(frequency_hz=500.0, phase_deg=-12.0),
            ),
            created_at_utc=NOW,
        )

    # ABSOLUTE_FREE_FIELD_SPL must prove free-field + calibration semantics.
    unspecified = SourceResponseCondition(
        input_quantity='voltage_v_rms',
        input_value=2.83,
        reference_distance_m=1.0,
        calibration='calibrated mic #42',
    )
    with pytest.raises(ValueError, match='free_field'):
        build_source_response(
            equipment_definition=definition,
            label='unspecified field',
            capability_tier='ABSOLUTE_FREE_FIELD_SPL',
            provenance='lab',
            condition=unspecified,
            valid_frequency_domain=domain,
            response_samples=(
                SourceResponseSample(frequency_hz=500.0, magnitude_db_spl=88.0),
            ),
            created_at_utc=NOW,
        )
    bare = SourceResponseCondition(
        input_quantity='voltage_v_rms',
        input_value=2.83,
        reference_distance_m=1.0,
        field_condition='free_field',
    )
    with pytest.raises(ValueError, match='calibration'):
        build_source_response(
            equipment_definition=definition,
            label='uncalibrated absolute',
            capability_tier='ABSOLUTE_FREE_FIELD_SPL',
            provenance='lab',
            condition=bare,
            valid_frequency_domain=domain,
            response_samples=(
                SourceResponseSample(frequency_hz=500.0, magnitude_db_spl=88.0),
            ),
            created_at_utc=NOW,
        )


def test_response_samples_must_stay_inside_declared_domain() -> None:
    definition = _definition('sp-domain')
    condition = _spl_condition()
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=5000.0)

    with pytest.raises(ValueError, match='outside the declared'):
        build_source_response(
            equipment_definition=definition,
            label='out of band',
            capability_tier='ABSOLUTE_FREE_FIELD_SPL',
            provenance='lab',
            condition=condition,
            valid_frequency_domain=domain,
            response_samples=(
                SourceResponseSample(frequency_hz=50.0, magnitude_db_spl=88.0),
            ),
            created_at_utc=NOW,
        )
    with pytest.raises(ValueError, match='duplicate'):
        build_source_response(
            equipment_definition=definition,
            label='dup freqs',
            capability_tier='ABSOLUTE_FREE_FIELD_SPL',
            provenance='lab',
            condition=condition,
            valid_frequency_domain=domain,
            response_samples=(
                SourceResponseSample(frequency_hz=500.0, magnitude_db_spl=88.0),
                SourceResponseSample(frequency_hz=500.0, magnitude_db_spl=86.0),
            ),
            created_at_utc=NOW,
        )


def test_transfer_evidence_domain_and_dataset_requirements() -> None:
    domain = FrequencyDomain(minimum_hz=200.0, maximum_hz=8000.0)
    angles = AngleRange(minimum_deg=0.0, maximum_deg=45.0)
    dataset_ref = ExactExternalAuthorityRef(
        authority_id='measurement-dataset:lab',
        authority_version='1',
        semantic_hash_sha256='c' * 64,
    )

    # MEASURED_DATASET claims need an exact measured authority — a free-text
    # provenance label alone is a forged claim.
    with pytest.raises(ValueError, match='measured dataset'):
        build_screen_transfer(
            screen_entity_id='screen-1',
            label='claimed measured',
            capability_tier='MEASURED_DATASET',
            provenance='lab-measurement-2026',
            transfer_samples=(
                TransferSample(frequency_hz=500.0, magnitude=0.9),
            ),
            valid_frequency_domain=domain,
            valid_incidence_angle_deg=angles,
            created_at_utc=NOW,
        )

    # Angle tiers require a declared incidence-angle domain.
    with pytest.raises(ValueError, match='incidence-angle domain'):
        build_screen_transfer(
            screen_entity_id='screen-1',
            label='angled without range',
            capability_tier='FREQUENCY_AND_ANGLE',
            provenance='lab',
            transfer_samples=(
                TransferSample(
                    frequency_hz=500.0,
                    incidence_angle_deg=15.0,
                    magnitude=0.8,
                ),
            ),
            valid_frequency_domain=domain,
            created_at_utc=NOW,
        )

    # Samples can never claim frequencies/angles outside the declared domain.
    with pytest.raises(ValueError, match='outside the declared'):
        build_screen_transfer(
            screen_entity_id='screen-1',
            label='out of band',
            capability_tier='MAGNITUDE_NORMAL_INCIDENCE',
            provenance='lab',
            transfer_samples=(
                TransferSample(frequency_hz=50.0, magnitude=0.9),
            ),
            valid_frequency_domain=domain,
            created_at_utc=NOW,
        )
    with pytest.raises(ValueError, match='outside the declared'):
        build_screen_transfer(
            screen_entity_id='screen-1',
            label='angle out of range',
            capability_tier='FREQUENCY_AND_ANGLE',
            provenance='lab',
            transfer_samples=(
                TransferSample(
                    frequency_hz=500.0,
                    incidence_angle_deg=60.0,
                    magnitude=0.8,
                ),
            ),
            valid_frequency_domain=domain,
            valid_incidence_angle_deg=angles,
            created_at_utc=NOW,
        )

    # Duplicate sample points are rejected.
    with pytest.raises(ValueError, match='duplicate'):
        build_screen_transfer(
            screen_entity_id='screen-1',
            label='dup points',
            capability_tier='FREQUENCY_AND_ANGLE',
            provenance='lab',
            transfer_samples=(
                TransferSample(
                    frequency_hz=500.0,
                    incidence_angle_deg=15.0,
                    magnitude=0.8,
                ),
                TransferSample(
                    frequency_hz=500.0,
                    incidence_angle_deg=15.0,
                    magnitude=0.7,
                ),
            ),
            valid_frequency_domain=domain,
            valid_incidence_angle_deg=angles,
            created_at_utc=NOW,
        )

    # The honest positive case persists.
    authority = build_screen_transfer(
        screen_entity_id='screen-1',
        label='measured',
        capability_tier='MEASURED_DATASET',
        provenance='lab',
        measured_dataset_ref=dataset_ref,
        transfer_samples=(
            TransferSample(
                frequency_hz=500.0,
                incidence_angle_deg=15.0,
                magnitude=0.9,
            ),
        ),
        valid_frequency_domain=domain,
        valid_incidence_angle_deg=angles,
        created_at_utc=NOW,
    )
    assert authority.measured_dataset_ref == dataset_ref


def test_derived_environment_speed_must_recompute() -> None:
    # The documented derivation c = 331.3 + 0.606·T — a declared value that
    # does not recompute is a forged claim.
    with pytest.raises(ValueError, match='331.3'):
        build_acoustic_environment_profile(
            label='forged',
            sound_speed_source_kind='derived_from_temperature',
            temperature_c=20.0,
            temperature_source_kind='manual_measured',
            sound_speed_m_s=343.0,
            provenance='x',
            created_at_utc=NOW,
        )
    profile = build_acoustic_environment_profile(
        label='honest',
        sound_speed_source_kind='derived_from_temperature',
        temperature_c=20.0,
        temperature_source_kind='manual_measured',
        sound_speed_m_s=sound_speed_from_temperature_c(20.0),
        provenance='x',
        created_at_utc=NOW,
    )
    assert profile.sound_speed_m_s == pytest.approx(343.42)


# ---------------------------------------------------------------------------
# #794 — pre-#541 serialized request identity must survive revalidation
# ---------------------------------------------------------------------------


def _v1_request_payload(screen_dict: dict) -> dict:
    """A request payload exactly as the pre-#541 model serialized it: the
    v1 ScreenGeometryBinding always dumped ``acoustically_transparent``
    (including null) and never carried ``screen_transfer_ref``."""
    seat = SeatGeometryBinding(
        entity_id='seat-1',
        row_id='row-1',
        eye_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.15),
        head_center_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.2),
        head_radius_m=0.1,
    )
    policy = VideoGeometryPolicy(
        sightline_samples=(
            SightlineSample(
                sample_id='center',
                horizontal_fraction=0.5,
                vertical_fraction=0.5,
            ),
        ),
        sightline_clearance_m=0.0,
        riser_support_tolerance_m=0.0,
        max_optical_axis_deviation_deg=10.0,
        collision_clearance_m=0.0,
    )
    return {
        'schema_version': 1,
        'authority_version': 'video-geometry-1',
        'projector_entity_id': 'projector-1',
        'projector_specification_id': 'proj-spec-1',
        'projector_specification_version': '1',
        'projector_specification_sha256': 'b' * 64,
        'screen': screen_dict,
        'seats': [
            {
                key: value
                for key, value in seat.model_dump(mode='json').items()
                # Pre-#1056 payloads never carried the authority marker.
                if key not in ('geometry_source', 'pose_ref')
            }
        ],
        'policy': policy.model_dump(mode='json'),
        'collision_entity_ids': [],
    }


def _v1_screen_dict(transparent) -> dict:
    return {
        'entity_id': 'screen-1',
        'visible_width_m': 2.6,
        'visible_height_m': 1.1,
        'image_center_offset_local_m': {'x_m': 0.0, 'y_m': 0.0, 'z_m': 0.0},
        'frame_clearance_m': 0.02,
        'acoustically_transparent': transparent,
    }


def _request_sha(payload: dict) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )
    return sha256(canonical.encode('utf-8')).hexdigest()


@pytest.mark.parametrize('transparent', [True, False, None])
def test_pre_541_serialized_request_retains_identity(transparent) -> None:
    payload = _v1_request_payload(_v1_screen_dict(transparent))
    request_sha256 = _request_sha(payload)
    request = VideoGeometryRequest.model_validate(
        {**payload, 'request_sha256': request_sha256}
    )
    # The v1 hash is accepted verbatim — quarantining the dead flag must not
    # break previously persisted request identity.
    assert request.request_sha256 == request_sha256
    assert 'acoustically_transparent' not in (
        request.identity_payload()['screen']
    )


def test_screen_transfer_ref_joins_identity_only_when_present() -> None:
    payload = _v1_request_payload(
        {
            'entity_id': 'screen-1',
            'visible_width_m': 2.6,
            'visible_height_m': 1.1,
            'image_center_offset_local_m': {
                'x_m': 0.0,
                'y_m': 0.0,
                'z_m': 0.0,
            },
            'frame_clearance_m': 0.02,
        }
    )
    request = VideoGeometryRequest.model_validate(
        {**payload, 'request_sha256': _request_sha(payload)}
    )
    # A screen that never declared transfer evidence hashes identically to
    # v1 content without the flag — no ``screen_transfer_ref`` key at all.
    assert 'screen_transfer_ref' not in request.identity_payload()['screen']
    # An unknown extra field can never silently enter semantic identity.
    with pytest.raises(Exception, match='unknown fields'):
        ScreenGeometryBinding.model_validate(
            {
                'entity_id': 'screen-1',
                'visible_width_m': 2.6,
                'visible_height_m': 1.1,
                'frame_clearance_m': 0.02,
                'stealth_field': 'x',
            }
        )
