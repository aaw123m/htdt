from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from htdt.cad_coverage import (
    build_coverage_evaluation_scenario,
    evaluate_coverage,
)
from htdt.cad_direct_level import (
    DirectLevelFrequencyBand,
    ReferenceInputCondition,
    SeatPopulation,
    build_playback_excitation_scenario,
    direct_level_objective_vector,
    evaluate_direct_level,
)
from htdt.cad_directivity import NORMALIZED_JSON_DIRECTIVITY_ADAPTER
from htdt.cad_directivity_repository import CadDirectivityRepository
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
    SensitivityReference,
    SplCapability,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
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
from htdt.cad_seat_priority import (
    CadSeatPriorityProfileRepository,
    SeatPriorityMember,
    SeatPriorityProfile,
    build_seat_priority_profile,
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository


NOW = '2026-09-20T12:00:00+00:00'
DOCUMENT_ID = 'seat-priority-fixture'


def _save_equipment(repository, definition) -> None:
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='seat-priority-fixture',
        recorded_at_utc=NOW,
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)


def _provenance(source_name: str, source_hash: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name=source_name,
        source_version='2026-09-20',
        source_reference='issue-513-fixture',
        source_sha256=source_hash,
    )


def _equipment():
    provenance = _provenance('seat-priority-speaker', 'a' * 64)
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=10000.0)
    return build_equipment_definition(
        definition_id='seat-priority-speaker',
        version='1',
        identity_kind='user_defined',
        user_label='seat-priority-speaker',
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(),
        sensitivity=SensitivityReference(
            level_db_spl=88.0,
            input_quantity='voltage_v_rms',
            input_value=2.83,
            distance_m=1.0,
            valid_frequency_domain=domain,
            weighting=None,
            provenance=provenance,
        ),
        spl_capability=SplCapability(
            continuous_db_spl=105.0,
            peak_db_spl=111.0,
            reference_distance_m=1.0,
            valid_frequency_domain=domain,
            continuous_duration_s=60.0,
            peak_duration_s=0.1,
            provenance=provenance,
        ),
        directivity=DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=provenance,
        ),
    )


def _speaker() -> SceneEntity:
    return SceneEntity(
        entity_id='speaker-fl',
        kind='speaker',
        name='Front Left',
        speaker_role='FL',
        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        orientation=quaternion_from_euler_deg(
            yaw_deg=0.0, pitch_deg=0.0, roll_deg=0.0
        ),
        size_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
    )


def _seat(entity_id: str, y_m: float, x_m: float = 1.0) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='seat',
        name=entity_id,
        position=Position3(x_m=x_m, y_m=y_m, z_m=1.0),
        size_m=Size3(x_m=0.6, y_m=0.8, z_m=1.0),
        acoustic_reference_offset_m=Offset3(),
    )


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=8.0, height_m=2.5),
        entities=(
            _speaker(),
            _seat('seat-mlp', 3.0),
            _seat('seat-secondary', 5.0, x_m=3.0),
            _seat('seat-diag', 6.0),
        ),
    )
    revision = scene_repository.save(
        document,
        parent_revision_id=None,
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository,
        variant_repository,
    )
    return (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    )


def _persist_variant(variant_repository, equipment_repository, revision, definition):
    _save_equipment(equipment_repository, definition)
    binding = EquipmentBindingRef(
        entity_id='speaker-fl',
        equipment_definition_id=definition.definition_id,
        equipment_definition_version=definition.version,
        equipment_definition_sha256=definition.semantic_sha256,
    )
    variant = build_system_variant(
        baseline=revision,
        name='seat-priority variant',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(),
        equipment_bindings=(binding,),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    return variant


def _members(
    *,
    secondary_required: bool = True,
    diagnostic: bool = True,
) -> tuple[SeatPriorityMember, ...]:
    members = []
    if diagnostic:
        members.append(
            SeatPriorityMember(
                seat_entity_id='seat-diag',
                seat_role='diagnostic',
                required=False,
                weight=7.0,
            )
        )
    members.extend(
        (
            SeatPriorityMember(
                seat_entity_id='seat-mlp',
                seat_role='primary',
                required=True,
                weight=3.0,
            ),
            SeatPriorityMember(
                seat_entity_id='seat-secondary',
                seat_role='secondary',
                required=secondary_required,
                weight=1.0,
            ),
        )
    )
    return tuple(members)


def _scenario(population: SeatPopulation):
    return build_playback_excitation_scenario(
        source_entity_id='speaker-fl',
        channel_role_id='FL',
        reference_input=ReferenceInputCondition(
            input_quantity='voltage_v_rms',
            input_value=2.83,
        ),
        target_spl_db_spl=75.0,
        target_reference_condition='single-channel direct target at each seat',
        continuous_reference_duration_s=60.0,
        peak_reference_duration_s=0.1,
        frequency_band=DirectLevelFrequencyBand(
            low_hz=100.0,
            high_hz=10000.0,
        ),
        weighting='unweighted',
        receiver_population=population,
    )


def test_profile_binds_head_revision_and_normalizes_required_weights(
    tmp_path: Path,
) -> None:
    scene_repository, revision, _vr, _er = _repositories(tmp_path)

    profile = build_seat_priority_profile(
        scene_repository=scene_repository,
        document_id=DOCUMENT_ID,
        members=_members(),
    )

    assert profile.profile_id.startswith('seat-priority:')
    assert profile.scene_revision_id == revision.revision_id
    assert profile.scene_content_hash == revision.content_hash
    assert profile.seat_entity_ids == ('seat-diag', 'seat-mlp', 'seat-secondary')
    assert profile.required_seat_entity_ids == ('seat-mlp', 'seat-secondary')
    assert profile.diagnostic_seat_entity_ids == ('seat-diag',)
    assert profile.normalized_weights() == pytest.approx(
        {'seat-mlp': 0.75, 'seat-secondary': 0.25}
    )


def test_profile_rejects_non_seat_and_unsorted_members(tmp_path: Path) -> None:
    scene_repository, _revision, _vr, _er = _repositories(tmp_path)

    with pytest.raises(ValueError, match='seat entity'):
        build_seat_priority_profile(
            scene_repository=scene_repository,
            document_id=DOCUMENT_ID,
            members=(
                SeatPriorityMember(
                    seat_entity_id='speaker-fl',
                    seat_role='primary',
                ),
            ),
        )
    with pytest.raises(ValueError, match='missing from the current scene'):
        build_seat_priority_profile(
            scene_repository=scene_repository,
            document_id=DOCUMENT_ID,
            members=(
                SeatPriorityMember(
                    seat_entity_id='seat-ghost',
                    seat_role='primary',
                ),
            ),
        )
    with pytest.raises(ValueError, match='unique and sorted'):
        build_seat_priority_profile(
            scene_repository=scene_repository,
            document_id=DOCUMENT_ID,
            members=(
                SeatPriorityMember(
                    seat_entity_id='seat-mlp',
                    seat_role='primary',
                ),
                SeatPriorityMember(
                    seat_entity_id='seat-diag',
                    seat_role='secondary',
                ),
            ),
        )


def test_profile_repository_round_trip_rejects_tampering(tmp_path: Path) -> None:
    scene_repository, _revision, _vr, _er = _repositories(tmp_path)
    repository = CadSeatPriorityProfileRepository(scene_repository)
    profile = build_seat_priority_profile(
        scene_repository=scene_repository,
        document_id=DOCUMENT_ID,
        members=_members(),
    )

    persisted = repository.save(profile)
    assert repository.get(profile.profile_id) == persisted
    assert repository.list_for_document(DOCUMENT_ID) == (persisted,)

    tampered = profile.model_dump(mode='python')
    members = [dict(item) for item in tampered['members']]
    members[1]['weight'] = 9.0
    tampered['members'] = tuple(members)
    with pytest.raises(ValueError, match='semantic hash mismatch'):
        repository.save(SeatPriorityProfile.model_validate(tampered))


def test_seat_priority_population_requires_exact_binding(tmp_path: Path) -> None:
    profile_ids = ('seat-priority:' + 'a' * 24, 'b' * 64)
    with pytest.raises(ValueError, match='exact profile binding'):
        SeatPopulation(
            population_id='unbound',
            seat_entity_ids=('seat-mlp',),
            population_weighting='seat_priority',
        )
    with pytest.raises(ValueError, match='requires seat_priority'):
        SeatPopulation(
            population_id='misbound',
            seat_entity_ids=('seat-mlp',),
            priority_profile_id=profile_ids[0],
            priority_profile_sha256=profile_ids[1],
        )


def test_weighted_direct_level_uses_priority_members_only(tmp_path: Path) -> None:
    (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    definition = _equipment()
    variant = _persist_variant(
        variant_repository, equipment_repository, revision, definition
    )
    profile = build_seat_priority_profile(
        scene_repository=scene_repository,
        document_id=DOCUMENT_ID,
        members=_members(),
    )
    population = SeatPopulation(
        population_id='mlp-weighted',
        seat_entity_ids=profile.seat_entity_ids,
        population_weighting='seat_priority',
        priority_profile_id=profile.profile_id,
        priority_profile_sha256=profile.profile_sha256,
    )
    scenario = _scenario(population)

    evaluation = evaluate_direct_level(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        scenario=scenario,
        priority_profile=profile,
    )
    repeated = evaluate_direct_level(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        scenario=scenario,
        priority_profile=profile,
    )

    assert evaluation.evaluation_sha256 == repeated.evaluation_sha256
    assert evaluation.priority_aggregates is not None
    priority = evaluation.priority_aggregates
    assert priority.priority_profile_sha256 == profile.profile_sha256
    assert priority.required_seat_entity_ids == ('seat-mlp', 'seat-secondary')
    assert priority.normalized_weights == pytest.approx(
        {'seat-mlp': 0.75, 'seat-secondary': 0.25}
    )

    by_seat = {item.seat_entity_id: item for item in evaluation.seat_results}
    expected_weighted = (
        0.75 * by_seat['seat-mlp'].direct_level.value
        + 0.25 * by_seat['seat-secondary'].direct_level.value
    )
    assert priority.weighted_direct_level.value == pytest.approx(
        expected_weighted
    )
    assert priority.worst_required_seat_direct_level.value == pytest.approx(
        by_seat['seat-secondary'].direct_level.value
    )
    # Diagnostics still appear as evidence but never enter the aggregate.
    assert 'seat-diag' in by_seat
    assert 'seat-diag' not in priority.normalized_weights

    vector = direct_level_objective_vector(evaluation)
    ids = {metric.objective_id for metric in vector.metrics}
    assert 'o100d.priority.weighted_direct_level_db_spl' in ids
    assert 'o100d.priority.worst_required_seat_direct_level_db_spl' in ids
    assert len(vector.metrics) == 13


def test_equal_unweighted_population_keeps_identity_unchanged(
    tmp_path: Path,
) -> None:
    (
        _sr,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    definition = _equipment()
    variant = _persist_variant(
        variant_repository, equipment_repository, revision, definition
    )
    population = SeatPopulation(
        population_id='equal',
        seat_entity_ids=('seat-mlp', 'seat-secondary'),
    )
    evaluation = evaluate_direct_level(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        scenario=_scenario(population),
    )
    assert evaluation.priority_aggregates is None
    vector = direct_level_objective_vector(evaluation)
    assert len(vector.metrics) == 5
    assert not any(
        metric.objective_id.startswith('o100d.priority.')
        for metric in vector.metrics
    )


def test_weighted_evaluation_fails_closed_on_binding_mismatch(
    tmp_path: Path,
) -> None:
    (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    definition = _equipment()
    variant = _persist_variant(
        variant_repository, equipment_repository, revision, definition
    )
    profile = build_seat_priority_profile(
        scene_repository=scene_repository,
        document_id=DOCUMENT_ID,
        members=_members(diagnostic=False),
    )

    # Population declares seat_priority but the profile is absent.
    population = SeatPopulation(
        population_id='missing-profile',
        seat_entity_ids=profile.seat_entity_ids,
        population_weighting='seat_priority',
        priority_profile_id=profile.profile_id,
        priority_profile_sha256=profile.profile_sha256,
    )
    with pytest.raises(ValueError, match='requires the bound'):
        evaluate_direct_level(
            revision=revision,
            variant=variant,
            equipment_definition=definition,
            scenario=_scenario(population),
        )

    # Member order/content mismatch fails closed.
    other_members = (
        SeatPriorityMember(seat_entity_id='seat-diag', seat_role='primary'),
        SeatPriorityMember(
            seat_entity_id='seat-secondary', seat_role='secondary'
        ),
    )
    other_profile = build_seat_priority_profile(
        scene_repository=scene_repository,
        document_id=DOCUMENT_ID,
        members=other_members,
    )
    mismatched = SeatPopulation(
        population_id='mismatched',
        seat_entity_ids=profile.seat_entity_ids,
        population_weighting='seat_priority',
        priority_profile_id=profile.profile_id,
        priority_profile_sha256=profile.profile_sha256,
    )
    with pytest.raises(ValueError, match='exact scenario binding|exact population'):
        evaluate_direct_level(
            revision=revision,
            variant=variant,
            equipment_definition=definition,
            scenario=_scenario(mismatched),
            priority_profile=other_profile,
        )


def _directivity_authority():
    source_payload = {
        'schema': 'htdt.normalized-directivity.v1',
        'dataset_id': 'seat-priority-directivity',
        'version': '1',
        'source_format': 'custom',
        'evidence_kind': 'user_defined',
        'source_name': 'seat-priority fixture',
        'source_version': '2026-09-20',
        'source_reference': 'issue-513-fixture',
        'kind': 'magnitude_only',
        'coordinate_convention': {
            'angle_semantics': 'horizontal_vertical',
            'horizontal_wrap': 'none',
            'reference_axis': 'equipment_acoustic_reference_axis',
            'azimuth_positive': 'left',
            'elevation_positive': 'up',
            'angle_unit': 'degree',
        },
        'normalization': {
            'source_magnitude_unit': 'db',
            'normalized_magnitude_unit': 'db',
            'reference': 'on_axis_per_frequency',
            'reference_level_db': None,
            'conversion_version': 'pressure-amplitude-db20-v1',
        },
        'phase_reference': None,
        'interpolation_method': 'linear',
        'interpolation_implementation': 'htdt-grid-linear',
        'interpolation_version': '1',
        'frequencies_hz': [500.0, 1000.0],
        'horizontal_angles_deg': [-60.0, 0.0, 60.0],
        'vertical_angles_deg': [0.0],
        'samples': [
            {
                'frequency_hz': frequency_hz,
                'horizontal_angle_deg': angle,
                'vertical_angle_deg': 0.0,
                'magnitude': 0.0 if angle == 0.0 else -6.0,
                'phase_deg': None,
            }
            for frequency_hz in (500.0, 1000.0)
            for angle in (-60.0, 0.0, 60.0)
        ],
    }
    source_bytes = json.dumps(
        source_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    source_hash = sha256(source_bytes).hexdigest()
    provenance = _provenance('seat-priority-directivity', source_hash)
    domain = DirectivityDomain(
        frequency=FrequencyDomain(minimum_hz=500.0, maximum_hz=1000.0),
        horizontal=AngleDomain(minimum_deg=-60.0, maximum_deg=60.0),
        vertical=AngleDomain(minimum_deg=0.0, maximum_deg=0.0),
    )
    definition = build_equipment_definition(
        definition_id='seat-priority-directivity-speaker',
        version='1',
        identity_kind='user_defined',
        user_label='seat-priority-directivity-speaker',
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='magnitude_only',
            data_format='custom',
            provenance=provenance,
            data_asset_sha256=source_hash,
            valid_domain=domain,
            interpolation=InterpolationProvenance(
                method='linear',
                implementation='htdt-grid-linear',
                implementation_version='1',
                provenance=provenance,
            ),
        ),
    )
    dataset = NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(source_bytes, definition)
    return source_bytes, definition, dataset


def test_weighted_coverage_fraction_respects_priority_profile(
    tmp_path: Path,
) -> None:
    (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    directivity_repository = CadDirectivityRepository(
        scene_repository,
        equipment_repository,
    )
    source_bytes, definition, dataset = _directivity_authority()
    _save_equipment(equipment_repository, definition)
    directivity_repository.save_dataset(
        dataset,
        source_bytes=source_bytes,
        source_filename='seat-priority.normalized.json',
        media_type='application/json',
        declared_schema='htdt.normalized-directivity.v1',
    )
    variant = _persist_variant(
        variant_repository, equipment_repository, revision, definition
    )

    profile = build_seat_priority_profile(
        scene_repository=scene_repository,
        document_id=DOCUMENT_ID,
        members=_members(),
    )
    population = SeatPopulation(
        population_id='priority-coverage',
        seat_entity_ids=profile.seat_entity_ids,
        population_weighting='seat_priority',
        priority_profile_id=profile.profile_id,
        priority_profile_sha256=profile.profile_sha256,
    )
    scenario = build_coverage_evaluation_scenario(
        source_entity_id='speaker-fl',
        channel_role_id='FL',
        receiver_population=population,
        directivity_dataset=dataset,
        equipment_definition=definition,
        evaluation_frequencies_hz=(500.0,),
        frequency_aggregation_semantics='worst_over_requested_frequencies',
        coverage_threshold_db=0.0,
    )
    assert scenario.seat_weighting_semantics == 'seat_priority'

    evaluation = evaluate_coverage(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=scenario,
        priority_profile=profile,
    )
    priority = evaluation.priority_aggregates
    assert priority is not None
    by_seat = {item.seat_entity_id: item for item in evaluation.seat_results}
    # On-axis MLP passes; off-axis secondary fails — the weighted fraction is
    # 0.75 even though the unweighted required fraction would be 0.5.
    assert by_seat['seat-mlp'].coverage_pass is True
    assert by_seat['seat-secondary'].coverage_pass is False
    expected_fraction = sum(
        priority.normalized_weights[seat_id]
        for seat_id in priority.required_seat_entity_ids
        if by_seat[seat_id].coverage_pass is True
    )
    assert priority.weighted_useful_coverage_fraction.value == pytest.approx(
        expected_fraction
    )
    assert 'seat-diag' in by_seat
    assert 'seat-diag' not in priority.normalized_weights
    worst = min(
        by_seat[seat_id].aggregated_relative_directivity_level.value
        for seat_id in priority.required_seat_entity_ids
    )
    assert priority.worst_required_seat_relative_directivity_level.value == (
        pytest.approx(worst)
    )
