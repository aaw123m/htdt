from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from htdt.acoustic_benchmark import GeometricAcousticBand, SpecificImpedancePoint
from htdt.cad_acoustic_environment import (
    CadAcousticEnvironmentRepository,
    air_density_moist_ideal_gas_v1,
    build_acoustic_environment_profile,
    environment_compatibility,
    nominal_environment_profile,
    snapshot_environment_ref,
    sound_speed_from_temperature_c,
)
from htdt.cad_acoustic_material import (
    CadAcousticMaterialRepository,
    build_acoustic_material,
)
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
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_listener_pose import (
    CadListenerPoseRepository,
    build_listener_pose,
    listener_pose_for_seat,
    seat_binding_from_pose,
)
from htdt.cad_r110_source import compile_r110_source_model
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    quaternion_from_euler_deg,
)
from htdt.cad_screen_transfer import (
    CadScreenTransferRepository,
    TransferSample,
    build_screen_transfer,
    transfer_capability_label,
)
from htdt.cad_source_response import (
    CadSourceResponseRepository,
    SourceResponseCondition,
    SourceResponseSample,
    build_source_response,
    response_capability_label,
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    build_system_variant,
)
from htdt.cad_video_geometry import ScreenGeometryBinding, build_video_geometry_request
from htdt.cad_video_geometry import (
    AngleRange,
    ProjectorSpecification,
    SeatGeometryBinding,
    SightlineSample,
    VideoGeometryPolicy,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef
from htdt.semantic_geometry import SemanticSurface


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


# ---------------------------------------------------------------------------
# #479 — Acoustic environment authority
# ---------------------------------------------------------------------------


def test_environment_profile_is_sealed_and_derives_id_from_content() -> None:
    profile = build_acoustic_environment_profile(
        label='測定室 22 °C',
        sound_speed_source_kind='derived_from_temperature',
        temperature_c=22.0,
        temperature_source_kind='manual_measured',
        sound_speed_m_s=sound_speed_from_temperature_c(22.0),
        provenance='room-thermometer',
        created_at_utc=NOW,
    )
    assert profile.authority_id.startswith('acoustic-environment:')
    assert profile.semantic_hash_sha256 == profile.authority_id.split(':', 1)[1]
    assert profile.authority_ref().authority_id == profile.authority_id

    same = build_acoustic_environment_profile(
        label='測定室 22 °C',
        sound_speed_source_kind='derived_from_temperature',
        temperature_c=22.0,
        temperature_source_kind='manual_measured',
        sound_speed_m_s=sound_speed_from_temperature_c(22.0),
        provenance='room-thermometer',
        created_at_utc=NOW,
    )
    assert same.authority_id == profile.authority_id

    other = build_acoustic_environment_profile(
        label='測定室 22 °C',
        sound_speed_source_kind='derived_from_temperature',
        temperature_c=23.0,
        temperature_source_kind='manual_measured',
        sound_speed_m_s=sound_speed_from_temperature_c(23.0),
        provenance='room-thermometer',
        created_at_utc=NOW,
    )
    assert other.authority_id != profile.authority_id
    assert (
        environment_compatibility(
            profile.authority_ref(), other.authority_ref()
        )
        == 'different'
    )
    assert (
        environment_compatibility(
            profile.authority_ref(), profile.authority_ref()
        )
        == 'same'
    )
    assert (
        environment_compatibility(profile.authority_ref(), None) == 'unknown'
    )


def test_nominal_profile_is_343_assumption_and_stable() -> None:
    profile = nominal_environment_profile()
    assert profile.sound_speed_m_s == pytest.approx(343.0)
    assert profile.sound_speed_source_kind == 'nominal_assumption'
    assert nominal_environment_profile().authority_id == profile.authority_id


def test_environment_repository_round_trip_and_selection(tmp_path: Path) -> None:
    repository = CadAcousticEnvironmentRepository(tmp_path / 'env.sqlite3')
    default = repository.ensure_default_profile()
    assert default.authority_id == nominal_environment_profile().authority_id

    profile = build_acoustic_environment_profile(
        label='夏 28 °C',
        sound_speed_source_kind='manual_measured',
        sound_speed_m_s=348.0,
        provenance='engineer',
        created_at_utc=NOW,
    )
    repository.save_profile(profile)
    loaded = repository.get_profile(profile.authority_id)
    assert loaded == profile

    repository.select_profile('doc-1', profile)
    assert repository.selected_profile('doc-1') == profile
    assert repository.selected_profile('doc-2') is None

    repository.clear_selection('doc-1')
    assert repository.selected_profile('doc-1') is None


def test_snapshot_environment_ref_carries_per_field_sources() -> None:
    profile = build_acoustic_environment_profile(
        label='p',
        sound_speed_source_kind='derived_from_temperature',
        temperature_c=18.0,
        temperature_source_kind='manual_measured',
        sound_speed_m_s=sound_speed_from_temperature_c(18.0),
        provenance='x',
        created_at_utc=NOW,
    )
    ref = snapshot_environment_ref(profile)
    assert ref.authority.authority_id == profile.authority_id
    assert ref.sound_speed_m_s == pytest.approx(331.3 + 0.606 * 18.0)


# ---------------------------------------------------------------------------
# #932 — Unified acoustic air-state authority
# ---------------------------------------------------------------------------


def _full_air_state_profile(**overrides):
    kwargs = dict(
        label='measured air state 20 °C',
        sound_speed_source_kind='derived_from_temperature',
        sound_speed_m_s=sound_speed_from_temperature_c(20.0),
        temperature_c=20.0,
        temperature_source_kind='manual_measured',
        air_density_source_kind='derived_from_air_state',
        air_pressure_pa=101325.0,
        air_pressure_source_kind='manual_measured',
        relative_humidity_percent=50.0,
        relative_humidity_source_kind='manual_measured',
        provenance='weather station',
        created_at_utc=NOW,
    )
    kwargs.update(overrides)
    if 'air_density_kg_m3' not in overrides and (
        kwargs['air_density_source_kind'] == 'derived_from_air_state'
        and kwargs['temperature_c'] is not None
        and kwargs['air_pressure_pa'] is not None
        and kwargs['relative_humidity_percent'] is not None
    ):
        kwargs['air_density_kg_m3'] = air_density_moist_ideal_gas_v1(
            kwargs['temperature_c'],
            kwargs['air_pressure_pa'],
            kwargs['relative_humidity_percent'],
        )
    return build_acoustic_environment_profile(**kwargs)


def test_air_state_profile_binds_density_pressure_humidity() -> None:
    profile = _full_air_state_profile()
    assert profile.air_density_kg_m3 == pytest.approx(1.1989, abs=1e-3)
    ref = snapshot_environment_ref(profile)
    assert ref.air_density_kg_m3 == profile.air_density_kg_m3
    assert ref.air_density_source_authority is not None
    assert ref.air_pressure_pa == pytest.approx(101325.0)
    assert ref.air_pressure_source_authority is not None
    assert ref.relative_humidity_percent == pytest.approx(50.0)
    assert ref.relative_humidity_source_authority is not None
    # Per-field refs all resolve inside the single environment authority.
    assert ref.authority.authority_id == profile.authority_id


def test_air_state_value_and_source_kind_are_paired() -> None:
    with pytest.raises(ValueError, match='must be supplied together'):
        _full_air_state_profile(
            air_density_kg_m3=1.2,
            air_density_source_kind=None,
        )
    with pytest.raises(ValueError, match='must be supplied together'):
        _full_air_state_profile(
            air_density_kg_m3=None,
            air_density_source_kind=None,
            relative_humidity_percent=None,
        )
    with pytest.raises(ValueError, match='unknown .* source cannot carry'):
        _full_air_state_profile(air_pressure_source_kind='unknown')


def test_derived_air_density_must_replay_the_documented_derivation() -> None:
    # A claimed density that does not recompute from (T, p, RH) is forged.
    with pytest.raises(ValueError, match='moist ideal-gas derivation'):
        _full_air_state_profile(air_density_kg_m3=9.99)
    # The derivation needs all three inputs declared.
    with pytest.raises(ValueError, match='requires temperature, pressure'):
        _full_air_state_profile(
            air_density_kg_m3=1.2,
            air_pressure_pa=None,
            air_pressure_source_kind=None,
        )


def test_air_state_edit_stales_the_environment_authority() -> None:
    base = _full_air_state_profile()
    changed = _full_air_state_profile(relative_humidity_percent=60.0)
    # RH also changes the derived density, so an honest profile must carry it.
    assert changed.authority_id != base.authority_id
    assert (
        environment_compatibility(base.authority_ref(), changed.authority_ref())
        == 'different'
    )


def test_legacy_profile_without_air_state_keeps_content_hash() -> None:
    minimal = build_acoustic_environment_profile(
        label='pre-split profile',
        sound_speed_source_kind='nominal_assumption',
        sound_speed_m_s=343.0,
        provenance='x',
        created_at_utc=NOW,
    )
    ref = snapshot_environment_ref(minimal)
    assert ref.air_density_kg_m3 is None
    assert ref.air_density_source_authority is None
    assert ref.air_pressure_pa is None
    assert ref.relative_humidity_percent is None


def test_snapshot_environment_ref_pairs_each_air_state_field() -> None:
    from htdt.cad_acoustic_snapshot import SnapshotEnvironmentAuthorityRef
    from htdt.r120_geometry_compiler import ExactExternalAuthorityRef

    ref_id = ExactExternalAuthorityRef(
        authority_id='acoustic-environment:' + 'a' * 64,
        authority_version='1',
        semantic_hash_sha256='a' * 64,
    )
    field_ref = ExactExternalAuthorityRef(
        authority_id='acoustic-environment-field:' + 'b' * 64,
        authority_version='1',
        semantic_hash_sha256='b' * 64,
    )
    with pytest.raises(ValueError, match='supplied together'):
        SnapshotEnvironmentAuthorityRef(
            authority=ref_id,
            air_density_kg_m3=1.2,
        )
    with pytest.raises(ValueError, match='supplied together'):
        SnapshotEnvironmentAuthorityRef(
            authority=ref_id,
            relative_humidity_percent=50.0,
        )
    ok = SnapshotEnvironmentAuthorityRef(
        authority=ref_id,
        air_density_kg_m3=1.2,
        air_density_source_authority=field_ref,
        air_pressure_pa=101325.0,
        air_pressure_source_authority=field_ref,
        relative_humidity_percent=50.0,
        relative_humidity_source_authority=field_ref,
    )
    assert ok.air_density_kg_m3 == pytest.approx(1.2)


# ---------------------------------------------------------------------------
# #632 — Listener pose authority
# ---------------------------------------------------------------------------


def _seat() -> SceneEntity:
    return SceneEntity(
        entity_id='seat-1',
        kind='seat',
        name='MLP seat',
        position=Position3(x_m=2.0, y_m=3.0, z_m=0.0),
        size_m=Size3(x_m=0.6, y_m=0.6, z_m=1.0),
        acoustic_reference_offset_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.1),
    )


def test_listener_pose_is_sealed_and_seat_bound() -> None:
    pose = build_listener_pose(
        seat_entity_id='seat-1',
        label='upright',
        head_center_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.2),
        eye_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.15),
        acoustic_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.1),
        provenance='fixture',
        created_at_utc=NOW,
    )
    assert pose.pose_id.startswith('listener-pose:')
    assert pose.authority_ref().authority_id == pose.pose_id

    binding = seat_binding_from_pose(pose)
    assert binding.entity_id == 'seat-1'
    assert binding.eye_reference_offset_local_m.z_m == pytest.approx(1.15)
    assert binding.head_center_offset_local_m.z_m == pytest.approx(1.2)


def test_listener_pose_derives_acoustic_offset_from_seat() -> None:
    seat = _seat()
    pose = listener_pose_for_seat(
        seat,
        label='recline',
        head_center_offset_local_m=Offset3(x_m=0.0, y_m=-0.2, z_m=1.0),
        eye_reference_offset_local_m=Offset3(x_m=0.0, y_m=-0.2, z_m=0.95),
        provenance='fixture',
    )
    assert pose.acoustic_reference_offset_local_m.z_m == pytest.approx(1.1)
    assert pose.posture_kind == 'upright'


def test_listener_pose_refuses_seat_without_acoustic_offset() -> None:
    seat = SceneEntity(
        entity_id='seat-2',
        kind='seat',
        name='bare seat',
        position=Position3(x_m=1.0, y_m=1.0, z_m=0.0),
        size_m=Size3(x_m=0.6, y_m=0.6, z_m=1.0),
    )
    with pytest.raises(ValueError, match='acoustic reference offset'):
        listener_pose_for_seat(
            seat,
            label='p',
            head_center_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.0),
            eye_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.9),
            provenance='fixture',
        )


def test_listener_pose_repository_selects_per_seat(tmp_path: Path) -> None:
    repository = CadListenerPoseRepository(tmp_path / 'poses.sqlite3')
    pose_a = build_listener_pose(
        seat_entity_id='seat-1',
        label='upright',
        head_center_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.2),
        eye_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.15),
        acoustic_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.1),
        provenance='fixture',
        created_at_utc=NOW,
    )
    pose_b = build_listener_pose(
        seat_entity_id='seat-1',
        label='reclined',
        head_center_offset_local_m=Offset3(x_m=0.0, y_m=-0.3, z_m=1.0),
        eye_reference_offset_local_m=Offset3(x_m=0.0, y_m=-0.3, z_m=0.95),
        acoustic_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.1),
        posture_kind='reclined',
        provenance='fixture',
        created_at_utc=NOW,
    )
    repository.save_pose(pose_a)
    repository.save_pose(pose_b)

    repository.select_pose('doc-1', pose_a)
    selected = repository.selected_pose('doc-1', 'seat-1')
    assert selected is not None and selected.pose_id == pose_a.pose_id

    repository.select_pose('doc-1', pose_b)
    assert repository.selected_pose('doc-1', 'seat-1').pose_id == pose_b.pose_id
    assert repository.selected_pose('doc-1', 'seat-2') is None


# ---------------------------------------------------------------------------
# #541 — Acoustic screen transfer authority
# ---------------------------------------------------------------------------


def test_screen_transfer_tiers_and_hash(tmp_path: Path) -> None:
    repository = CadScreenTransferRepository(tmp_path / 'transfers.sqlite3')
    authority = build_screen_transfer(
        screen_entity_id='screen-1',
        label='woven AT screen measured',
        capability_tier='MEASURED_DATASET',
        provenance='lab-measurement-2026',
        measured_dataset_ref=ExactExternalAuthorityRef(
            authority_id='measurement-dataset:lab-2026',
            authority_version='1',
            semantic_hash_sha256='c' * 64,
        ),
        transfer_samples=(
            TransferSample(
                frequency_hz=500.0,
                incidence_angle_deg=0.0,
                magnitude=0.9,
                phase_deg=-2.0,
                reflection_magnitude=0.02,
            ),
        ),
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=200.0,
            maximum_hz=8000.0,
        ),
        valid_incidence_angle_deg=AngleRange(
            minimum_deg=0.0, maximum_deg=45.0
        ),
        measurement_condition='anechoic, 1 m mic',
        created_at_utc=NOW,
    )
    repository.save_transfer(authority)
    assert repository.get_transfer(authority.transfer_id) == authority
    assert transfer_capability_label(authority.capability_tier)
    assert transfer_capability_label('UNKNOWN')

    repository.select_transfer('doc-1', authority)
    assert repository.selected_transfer('doc-1', 'screen-1') == authority
    repository.clear_selection('doc-1', 'screen-1')
    assert repository.selected_transfer('doc-1', 'screen-1') is None


def test_legacy_acoustically_transparent_flag_is_quarantined_not_identity() -> (
    None
):
    """#541: the dead legacy flag still parses but does not touch identity."""
    legacy = ScreenGeometryBinding.model_validate(
        {
            'entity_id': 'screen-1',
            'visible_width_m': 2.6,
            'visible_height_m': 1.1,
            'frame_clearance_m': 0.02,
            'acoustically_transparent': True,
        }
    )
    clean = ScreenGeometryBinding(
        entity_id='screen-1',
        visible_width_m=2.6,
        visible_height_m=1.1,
        frame_clearance_m=0.02,
    )
    spec = ProjectorSpecification.model_construct(
        specification_id='proj-spec-1',
        version='1',
        specification_sha256='b' * 64,
    )
    seat = SeatGeometryBinding(
        geometry_source='manual',
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
    request_legacy = build_video_geometry_request(
        projector_entity_id='projector-1',
        projector_specification=spec,
        screen=legacy,
        seats=(seat,),
        policy=policy,
        collision_entity_ids=(),
    )
    request_clean = build_video_geometry_request(
        projector_entity_id='projector-1',
        projector_specification=spec,
        screen=clean,
        seats=(seat,),
        policy=policy,
        collision_entity_ids=(),
    )
    assert request_legacy.request_sha256 == request_clean.request_sha256


# ---------------------------------------------------------------------------
# #542 — Frequency-dependent source response authority
# ---------------------------------------------------------------------------


def _spl_condition() -> SourceResponseCondition:
    return SourceResponseCondition(
        input_quantity='voltage_v_rms',
        input_value=2.83,
        reference_distance_m=1.0,
        field_condition='free_field',
        mounting_condition='anechoic baffle',
        on_axis_direction=Direction3(x=1.0, y=0.0, z=0.0),
        calibration='calibrated mic #42',
    )


def test_source_response_tier_contracts() -> None:
    definition = _definition('sp-1')
    condition = _spl_condition()
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=5000.0)
    samples = (
        SourceResponseSample(frequency_hz=500.0, magnitude_db_spl=88.0),
        SourceResponseSample(frequency_hz=1000.0, magnitude_db_spl=86.5),
    )
    authority = build_source_response(
        equipment_definition=definition,
        label='on-axis SPL sweep',
        capability_tier='ABSOLUTE_FREE_FIELD_SPL',
        provenance='lab',
        condition=condition,
        valid_frequency_domain=domain,
        response_samples=samples,
        created_at_utc=NOW,
    )
    assert authority.response_id.startswith('source-response:')
    assert authority.authority_ref().authority_id == authority.response_id
    assert response_capability_label(authority.capability_tier)

    with pytest.raises(ValueError, match='requires explicit response samples'):
        build_source_response(
            equipment_definition=definition,
            label='empty',
            capability_tier='ABSOLUTE_FREE_FIELD_SPL',
            provenance='lab',
            condition=condition,
            valid_frequency_domain=domain,
            created_at_utc=NOW,
        )
    with pytest.raises(ValueError, match='reference condition'):
        build_source_response(
            equipment_definition=definition,
            label='no-condition',
            capability_tier='ABSOLUTE_FREE_FIELD_SPL',
            provenance='lab',
            valid_frequency_domain=domain,
            response_samples=samples,
            created_at_utc=NOW,
        )
    with pytest.raises(ValueError, match='phase'):
        build_source_response(
            equipment_definition=definition,
            label='magnitude-only complex',
            capability_tier='COMPLEX_RESPONSE',
            provenance='lab',
            condition=condition,
            valid_frequency_domain=domain,
            response_samples=samples,
            created_at_utc=NOW,
        )
    with pytest.raises(ValueError, match='UNKNOWN tier'):
        build_source_response(
            equipment_definition=definition,
            label='unknown-with-samples',
            capability_tier='UNKNOWN',
            provenance='lab',
            response_samples=samples,
            created_at_utc=NOW,
        )


def test_source_response_repository_selection_and_sha_resolution(
    tmp_path: Path,
) -> None:
    definition = _definition('sp-repo')
    repository = CadSourceResponseRepository(tmp_path / 'responses.sqlite3')
    authority = build_source_response(
        equipment_definition=definition,
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
        created_at_utc=NOW,
    )
    repository.save_response(authority)
    assert repository.get_response(authority.response_id) == authority
    assert (
        repository.get_response_by_sha256(authority.semantic_sha256)
        == authority
    )
    assert repository.list_responses_for_equipment(
        definition.definition_id
    ) == (authority,)

    repository.select_response('doc-1', authority)
    assert repository.selected_response(
        'doc-1', definition.definition_id
    ) == authority


def _speaker(*, entity_id: str = 'fl') -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name='Front left',
        speaker_role='FL',
        position=Position3(x_m=1.0, y_m=2.0, z_m=1.5),
        orientation=quaternion_from_euler_deg(
            yaw_deg=90.0, pitch_deg=0.0, roll_deg=0.0
        ),
        size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        aim_xyz=Direction3(x=1.0, y=0.0, z=0.0),
    )


def test_r110_compiles_source_response_capability(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='fixture-doc',
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.5),
        entities=(_speaker(),),
    )
    baseline = scene_repository.save(document, parent_revision_id=None).revision
    definition = _definition('sp-r110')
    variant = build_system_variant(
        baseline=baseline,
        name='v1',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front left'),
        ),
        proposed_entities=(),
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='fl',
                equipment_definition_id=definition.definition_id,
                equipment_definition_version=definition.version,
                equipment_definition_sha256=definition.semantic_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    response = build_source_response(
        equipment_definition=definition,
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
        created_at_utc=NOW,
    )
    model = compile_r110_source_model(
        scene_revision=baseline,
        system_variant=variant,
        source_entity_id='fl',
        equipment_definition=definition,
        source_response=response,
    )
    assert model.capability('source_response').decision == 'SUPPORTED'
    assert model.source_response_authority_id == response.response_id
    assert (
        model.source_response_authority_sha256 == response.semantic_sha256
    )
    assert model.source_response_capability_tier == 'ABSOLUTE_FREE_FIELD_SPL'

    # Compiling without a response keeps the base capability set unchanged.
    bare = compile_r110_source_model(
        scene_revision=baseline,
        system_variant=variant,
        source_entity_id='fl',
        equipment_definition=definition,
    )
    assert bare.source_response_authority_id is None
    assert all(
        item.capability != 'source_response' for item in bare.capabilities
    )


def test_r110_rejects_response_bound_to_other_equipment(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='fixture-doc',
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.5),
        entities=(_speaker(),),
    )
    baseline = scene_repository.save(document, parent_revision_id=None).revision
    definition = _definition('sp-r110')
    variant = build_system_variant(
        baseline=baseline,
        name='v1',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front left'),
        ),
        proposed_entities=(),
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='fl',
                equipment_definition_id=definition.definition_id,
                equipment_definition_version=definition.version,
                equipment_definition_sha256=definition.semantic_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    foreign = build_source_response(
        equipment_definition=_definition('other-sp'),
        label='other speaker response',
        capability_tier='ABSOLUTE_FREE_FIELD_SPL',
        provenance='lab',
        condition=_spl_condition(),
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=100.0, maximum_hz=5000.0
        ),
        response_samples=(
            SourceResponseSample(frequency_hz=500.0, magnitude_db_spl=88.0),
        ),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='EquipmentDefinition'):
        compile_r110_source_model(
            scene_revision=baseline,
            system_variant=variant,
            source_entity_id='fl',
            equipment_definition=definition,
            source_response=foreign,
        )


# ---------------------------------------------------------------------------
# #465 — Acoustic material authority + surface assignment
# ---------------------------------------------------------------------------


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


def test_material_capability_tiers_stay_separate() -> None:
    material = build_acoustic_material(
        label='石膏ボード',
        provenance='datasheet',
        wave_model='specific_impedance_table',
        specific_impedance=(
            SpecificImpedancePoint(
                frequency_hz=500.0,
                resistance_pa_s_m=800.0,
                reactance_pa_s_m=-200.0,
            ),
        ),
        geometric_model='banded',
        geometric_bands=(
            GeometricAcousticBand(
                center_hz=500.0, absorption=0.1, scattering=0.05
            ),
        ),
        created_at_utc=NOW,
    )
    assert material.material_id.startswith('acoustic-material:')
    projected = material.as_acoustic_material()
    assert projected.wave_model == 'specific_impedance_table'
    assert projected.geometric_model == 'banded'

    # Impedance evidence never fabricates geometric bands and vice versa.
    with pytest.raises(ValueError, match='impedance'):
        build_acoustic_material(
            label='bad',
            provenance='x',
            wave_model='rigid',
            specific_impedance=(
                SpecificImpedancePoint(
                    frequency_hz=500.0,
                    resistance_pa_s_m=1.0,
                    reactance_pa_s_m=0.0,
                ),
            ),
            created_at_utc=NOW,
        )
    with pytest.raises(ValueError, match='UNKNOWN surface state'):
        build_acoustic_material(
            label='nothing',
            provenance='x',
            created_at_utc=NOW,
        )


def test_material_assignment_replays_into_r120_bindings(tmp_path: Path) -> None:
    repository = CadAcousticMaterialRepository(tmp_path / 'materials.sqlite3')
    material = build_acoustic_material(
        label='石膏ボード',
        provenance='datasheet',
        wave_model='rigid',
        geometric_model='banded',
        geometric_bands=(
            GeometricAcousticBand(
                center_hz=500.0, absorption=0.1, scattering=0.05
            ),
        ),
        created_at_utc=NOW,
    )
    repository.save_material(material)
    surfaces = _surfaces()
    repository.assign_material('doc-1', surfaces[0].surface_id, material)

    bindings = repository.boundary_bindings('doc-1', surfaces)
    by_surface = {b.source_surface_id: b for b in bindings}
    assigned = by_surface[surfaces[0].surface_id]
    # Unassigned surfaces produce no binding at all — R120 reports them as
    # material_missing downstream instead of guessing physics.
    assert surfaces[1].surface_id not in by_surface
    assert assigned.material_authority is not None
    assert assigned.material_authority.authority_id == material.material_id
    assert assigned.material_authority.semantic_hash_sha256 == (
        material.semantic_sha256
    )

    assignment = repository.assignment_for('doc-1', surfaces[0].surface_id)
    assert assignment is not None and assignment.material_id == material.material_id

    repository.clear_assignment('doc-1', surfaces[0].surface_id)
    assert repository.assignment_for('doc-1', surfaces[0].surface_id) is None
