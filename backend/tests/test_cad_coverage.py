from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from math import cos, radians, sin
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_coverage import (
    CoverageEvaluation,
    build_coverage_evaluation_scenario,
    coverage_objective_vector,
    evaluate_coverage,
)
from htdt.cad_coverage_repository import CadCoverageRepository
from htdt.cad_direct_level import SeatPopulation
from htdt.cad_directivity import (
    NORMALIZED_JSON_DIRECTIVITY_ADAPTER,
)
from htdt.cad_directivity_repository import CadDirectivityRepository
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
    build_equipment_definition,
)
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
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.pareto import pareto_front


NOW = '2026-09-19T13:00:00+00:00'
DOCUMENT_ID = 'o100d-coverage-fixture'


def _provenance(source_hash: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name='O100D coverage deterministic fixture',
        source_version='2026-09-19',
        source_reference='issue-169-focused-fixture',
        source_sha256=source_hash,
    )


def _authority(
    *,
    definition_id: str = 'coverage-speaker',
    off_axis_500_db: float = -6.0,
    off_axis_1000_db: float = -12.0,
):
    source_payload = {
        'schema': 'htdt.normalized-directivity.v1',
        'dataset_id': f'{definition_id}-directivity',
        'version': '1',
        'source_format': 'custom',
        'evidence_kind': 'user_defined',
        'source_name': 'O100D coverage deterministic fixture',
        'source_version': '2026-09-19',
        'source_reference': 'issue-169-focused-fixture',
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
                'horizontal_angle_deg': horizontal_angle_deg,
                'vertical_angle_deg': 0.0,
                'magnitude': (
                    0.0
                    if horizontal_angle_deg == 0.0
                    else (
                        off_axis_500_db
                        if frequency_hz == 500.0
                        else off_axis_1000_db
                    )
                ),
                'phase_deg': None,
            }
            for frequency_hz in (500.0, 1000.0)
            for horizontal_angle_deg in (-60.0, 0.0, 60.0)
        ],
    }
    source_bytes = json.dumps(
        source_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    source_hash = sha256(source_bytes).hexdigest()
    provenance = _provenance(source_hash)
    domain = DirectivityDomain(
        frequency=FrequencyDomain(
            minimum_hz=500.0,
            maximum_hz=1000.0,
        ),
        horizontal=AngleDomain(
            minimum_deg=-60.0,
            maximum_deg=60.0,
        ),
        vertical=AngleDomain(
            minimum_deg=0.0,
            maximum_deg=0.0,
        ),
    )
    interpolation = InterpolationProvenance(
        method='linear',
        implementation='htdt-grid-linear',
        implementation_version='1',
        provenance=provenance,
    )
    definition = build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(
            x_m=0.20,
            y_m=0.25,
            z_m=0.35,
        ),
        acoustic_reference_point_m=Offset3(x_m=0.20),
        directivity=DirectivityCapability(
            tier='magnitude_only',
            data_format='custom',
            provenance=provenance,
            data_asset_sha256=source_hash,
            valid_domain=domain,
            interpolation=interpolation,
        ),
    )
    dataset = NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(
        source_bytes,
        definition,
    )
    return source_bytes, definition, dataset


def _speaker(
    *,
    aim: Direction3 | None = Direction3(x=0.0, y=1.0, z=0.0),
    yaw_deg: float = 0.0,
) -> SceneEntity:
    return SceneEntity(
        entity_id='speaker-fl',
        kind='speaker',
        name='Front Left',
        speaker_role='FL',
        position=Position3(x_m=0.0, y_m=0.0, z_m=1.0),
        orientation=quaternion_from_euler_deg(
            yaw_deg=yaw_deg,
            pitch_deg=0.0,
            roll_deg=0.0,
        ),
        size_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        aim_xyz=aim,
    )


def _seat(
    entity_id: str,
    *,
    x_m: float,
    y_m: float,
) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='seat',
        name=entity_id,
        position=Position3(x_m=x_m, y_m=y_m, z_m=1.0),
        size_m=Size3(x_m=0.60, y_m=0.80, z_m=1.0),
        acoustic_reference_offset_m=Offset3(),
    )


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(
            width_m=6.0,
            depth_m=6.0,
            height_m=2.5,
        ),
        entities=(
            _speaker(),
            _seat('seat-on', x_m=0.20, y_m=2.0),
            _seat('seat-off', x_m=2.20, y_m=2.0),
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
    directivity_repository = CadDirectivityRepository(
        scene_repository,
        equipment_repository,
    )
    return (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
        directivity_repository,
    )


def _persist_authority(
    equipment_repository: CadEquipmentRepository,
    directivity_repository: CadDirectivityRepository,
    definition,
    dataset,
    source_bytes: bytes,
) -> None:
    equipment_repository.save_definition(definition)
    directivity_repository.save_dataset(
        dataset,
        source_bytes=source_bytes,
        source_filename=f'{definition.definition_id}.normalized.json',
        media_type='application/json',
        declared_schema='htdt.normalized-directivity.v1',
    )


def _binding(definition) -> EquipmentBindingRef:
    return EquipmentBindingRef(
        entity_id='speaker-fl',
        equipment_definition_id=definition.definition_id,
        equipment_definition_version=definition.version,
        equipment_definition_sha256=definition.semantic_sha256,
    )


def _variant(
    variant_repository: CadSystemVariantRepository,
    revision,
    definition,
    *,
    replacement: SceneEntity | None = None,
    name: str = 'Coverage baseline',
):
    proposals = (
        ()
        if replacement is None
        else (
            ProposedEntitySpec(
                spec_id=f'{name}-speaker',
                entity=replacement,
                role_binding_id='FL',
            ),
        )
    )
    variant = build_system_variant(
        baseline=revision,
        name=name,
        role_bindings=(
            ChannelRoleBinding(
                role_id='FL',
                display_name='Front Left',
            ),
        ),
        proposed_entities=proposals,
        equipment_bindings=(_binding(definition),),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    return variant


def _scenario(
    definition,
    dataset,
    *,
    seats: tuple[str, ...] = ('seat-on', 'seat-off'),
    frequencies: tuple[float, ...] = (500.0, 1000.0),
    aggregation: str = 'worst_over_requested_frequencies',
    threshold_db: float = -6.0,
):
    return build_coverage_evaluation_scenario(
        source_entity_id='speaker-fl',
        channel_role_id='FL',
        receiver_population=SeatPopulation(
            population_id='coverage-two-seat-population',
            seat_entity_ids=seats,
        ),
        directivity_dataset=dataset,
        equipment_definition=definition,
        evaluation_frequencies_hz=frequencies,
        frequency_aggregation_semantics=aggregation,
        coverage_threshold_db=threshold_db,
    )


def _fixture(tmp_path: Path):
    (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
        directivity_repository,
    ) = _repositories(tmp_path)
    source_bytes, definition, dataset = _authority()
    _persist_authority(
        equipment_repository,
        directivity_repository,
        definition,
        dataset,
        source_bytes,
    )
    variant = _variant(
        variant_repository,
        revision,
        definition,
    )
    scenario = _scenario(definition, dataset)
    return (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
        directivity_repository,
        definition,
        dataset,
        variant,
        scenario,
    )


def test_on_axis_off_axis_multi_seat_frequency_coverage_and_objectives(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        revision,
        _variant_repository,
        _equipment_repository,
        _directivity_repository,
        definition,
        dataset,
        variant,
        scenario,
    ) = _fixture(tmp_path)

    evaluation = evaluate_coverage(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=scenario,
    )
    repeated = evaluate_coverage(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=scenario,
    )

    on_axis, off_axis = evaluation.seat_results
    assert on_axis.frequency_results[0].source_relative_angles is not None
    assert (
        on_axis.frequency_results[0]
        .source_relative_angles.horizontal_angle_deg
        == pytest.approx(0.0, abs=1e-12)
    )
    assert off_axis.frequency_results[0].source_relative_angles is not None
    assert (
        off_axis.frequency_results[0]
        .source_relative_angles.horizontal_angle_deg
        == pytest.approx(-45.0)
    )

    assert on_axis.aggregated_relative_directivity_level.value == pytest.approx(
        0.0
    )
    assert off_axis.frequency_results[0].relative_level_db == pytest.approx(
        -4.5
    )
    assert off_axis.frequency_results[1].relative_level_db == pytest.approx(
        -9.0
    )
    assert off_axis.aggregated_relative_directivity_level.value == pytest.approx(
        -9.0
    )
    assert off_axis.aggregated_off_axis_loss.value == pytest.approx(9.0)
    assert on_axis.coverage_pass is True
    assert off_axis.coverage_pass is False

    aggregates = evaluation.aggregates
    assert aggregates.useful_coverage_fraction.value == pytest.approx(0.5)
    assert (
        aggregates.worst_seat_relative_directivity_level.value
        == pytest.approx(-9.0)
    )
    assert aggregates.worst_seat_off_axis_loss.value == pytest.approx(9.0)
    assert (
        aggregates.seat_to_seat_directivity_spread.value
        == pytest.approx(9.0)
    )

    assert (
        off_axis.frequency_results[0].directivity_evaluation_sha256
        is not None
    )
    assert off_axis.frequency_results[0].reference_evaluation_sha256 is not None
    assert (
        scenario.off_axis_loss_authority.equation
        == 'loss_db=reference_level_db-evaluated_relative_level_db'
    )
    assert scenario.off_axis_loss_authority.clamp_negative_loss is False
    assert scenario.frequency_aggregation_semantics == (
        'worst_over_requested_frequencies'
    )
    assert evaluation == repeated
    assert evaluation.evaluation_sha256 == repeated.evaluation_sha256

    vector = coverage_objective_vector(evaluation)
    coverage = vector.metric('o100d.coverage.useful_fraction')
    relative = vector.metric(
        'o100d.directivity.worst_seat_relative_level_db'
    )
    loss = vector.metric(
        'o100d.directivity.worst_seat_off_axis_loss_db'
    )
    spread = vector.metric(
        'o100d.directivity.seat_to_seat_spread_db'
    )
    assert coverage.unit == 'ratio'
    assert coverage.direction == 'maximize'
    assert coverage.definition is not None
    assert coverage.definition.valid_domain.minimum == 0.0
    assert coverage.definition.valid_domain.maximum == 1.0
    assert relative.direction == 'maximize'
    assert loss.direction == 'minimize'
    assert spread.direction == 'minimize'


def test_body_yaw_changes_angle_through_exact_source_reference_not_implicit_aim(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        revision,
        variant_repository,
        _equipment_repository,
        _directivity_repository,
        definition,
        dataset,
        baseline_variant,
        scenario,
    ) = _fixture(tmp_path)

    baseline = evaluate_coverage(
        revision=revision,
        variant=baseline_variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=scenario,
    )
    body_yawed_source = _speaker(yaw_deg=90.0)
    body_yawed_variant = _variant(
        variant_repository,
        revision,
        definition,
        replacement=body_yawed_source,
        name='Body yaw 90',
    )
    yawed = evaluate_coverage(
        revision=revision,
        variant=body_yawed_variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=scenario,
    )

    baseline_angle = (
        baseline.seat_results[0]
        .frequency_results[0]
        .source_relative_angles
    )
    yawed_angle = (
        yawed.seat_results[0]
        .frequency_results[0]
        .source_relative_angles
    )
    assert baseline_angle is not None
    assert yawed_angle is not None
    assert baseline_angle.horizontal_angle_deg == pytest.approx(0.0)
    assert yawed_angle.horizontal_angle_deg != pytest.approx(
        baseline_angle.horizontal_angle_deg
    )
    assert baseline.source_acoustic_axis == yawed.source_acoustic_axis
    assert yawed.source_acoustic_axis == Direction3(
        x=0.0,
        y=1.0,
        z=0.0,
    )
    assert baseline.source_reference_position_m == Position3(
        x_m=0.20,
        y_m=0.0,
        z_m=1.0,
    )
    assert yawed.source_reference_position_m.x_m == pytest.approx(0.0, abs=1e-12)
    assert yawed.source_reference_position_m.y_m == pytest.approx(0.20)


def test_frequency_aggregation_is_explicit_and_semantically_distinct(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        revision,
        _variant_repository,
        _equipment_repository,
        _directivity_repository,
        definition,
        dataset,
        variant,
        worst_scenario,
    ) = _fixture(tmp_path)
    mean_scenario = _scenario(
        definition,
        dataset,
        aggregation='mean_over_requested_frequencies',
    )

    worst = evaluate_coverage(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=worst_scenario,
    )
    mean = evaluate_coverage(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=mean_scenario,
    )

    assert worst_scenario.scenario_sha256 != mean_scenario.scenario_sha256
    assert worst.evaluation_sha256 != mean.evaluation_sha256
    assert (
        worst.seat_results[1].aggregated_relative_directivity_level.value
        == pytest.approx(-9.0)
    )
    assert (
        mean.seat_results[1].aggregated_relative_directivity_level.value
        == pytest.approx(-6.75)
    )
    assert (
        coverage_objective_vector(worst)
        .metric('o100d.coverage.useful_fraction')
        .definition_id
        != coverage_objective_vector(mean)
        .metric('o100d.coverage.useful_fraction')
        .definition_id
    )


def test_domain_missing_seat_and_missing_aim_fail_closed_without_partial_fraction(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        revision,
        variant_repository,
        _equipment_repository,
        _directivity_repository,
        definition,
        dataset,
        variant,
        _scenario_default,
    ) = _fixture(tmp_path)

    outside = evaluate_coverage(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=_scenario(
            definition,
            dataset,
            frequencies=(1500.0,),
        ),
    )
    assert outside.seat_results[0].state == 'unsupported'
    assert (
        outside.seat_results[0].frequency_results[0]
        .directivity_evaluation_sha256
        is not None
    )
    assert outside.aggregates.useful_coverage_fraction.state == 'unsupported'
    assert outside.aggregates.useful_coverage_fraction.value is None

    missing = evaluate_coverage(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=_scenario(
            definition,
            dataset,
            seats=('seat-on', 'seat-missing'),
        ),
    )
    assert missing.seat_results[0].state == 'available'
    assert missing.seat_results[1].state == 'unsupported'
    assert missing.aggregates.useful_coverage_fraction.state == 'unsupported'
    assert missing.aggregates.useful_coverage_fraction.value is None

    missing_aim_variant = _variant(
        variant_repository,
        revision,
        definition,
        replacement=_speaker(aim=None),
        name='Missing explicit aim',
    )
    missing_aim = evaluate_coverage(
        revision=revision,
        variant=missing_aim_variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=_scenario(definition, dataset),
    )
    assert all(item.state == 'unsupported' for item in missing_aim.seat_results)
    assert 'must not be guessed' in (missing_aim.seat_results[0].reason or '')
    assert missing_aim.aggregates.worst_seat_off_axis_loss.value is None


def test_negative_off_axis_loss_is_preserved_without_clamp(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        revision,
        variant_repository,
        equipment_repository,
        directivity_repository,
    ) = _repositories(tmp_path)
    source_bytes, definition, dataset = _authority(
        definition_id='strong-off-axis-speaker',
        off_axis_500_db=3.0,
        off_axis_1000_db=6.0,
    )
    _persist_authority(
        equipment_repository,
        directivity_repository,
        definition,
        dataset,
        source_bytes,
    )
    variant = _variant(
        variant_repository,
        revision,
        definition,
        name='Strong off axis',
    )
    evaluation = evaluate_coverage(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=_scenario(definition, dataset),
    )

    off_axis = evaluation.seat_results[1]
    assert off_axis.frequency_results[0].off_axis_loss_db == pytest.approx(
        -2.25
    )
    assert off_axis.frequency_results[1].off_axis_loss_db == pytest.approx(
        -4.5
    )
    assert off_axis.aggregated_off_axis_loss.value == pytest.approx(-2.25)
    assert evaluation.aggregates.worst_seat_off_axis_loss.value == pytest.approx(
        0.0
    )


def test_exact_equipment_and_dataset_mismatch_are_rejected(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        revision,
        _variant_repository,
        equipment_repository,
        directivity_repository,
        definition,
        dataset,
        variant,
        scenario,
    ) = _fixture(tmp_path)
    other_bytes, other_definition, other_dataset = _authority(
        definition_id='other-coverage-speaker',
    )
    _persist_authority(
        equipment_repository,
        directivity_repository,
        other_definition,
        other_dataset,
        other_bytes,
    )

    with pytest.raises(
        ValueError,
        match='EquipmentDefinition scenario mismatch',
    ):
        evaluate_coverage(
            revision=revision,
            variant=variant,
            equipment_definition=other_definition,
            directivity_dataset=dataset,
            scenario=scenario,
        )

    with pytest.raises(
        ValueError,
        match='DirectivityDataset scenario mismatch',
    ):
        evaluate_coverage(
            revision=revision,
            variant=variant,
            equipment_definition=definition,
            directivity_dataset=other_dataset,
            scenario=scenario,
        )


def test_coverage_scenario_and_evaluation_round_trip(
    tmp_path: Path,
) -> None:
    (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
        directivity_repository,
        definition,
        dataset,
        variant,
        scenario,
    ) = _fixture(tmp_path)
    evaluation = evaluate_coverage(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=scenario,
    )
    repository = CadCoverageRepository(
        scene_repository,
        variant_repository,
        equipment_repository,
        directivity_repository,
    )
    assert repository.save_scenario(scenario) == scenario
    assert repository.save_scenario(scenario) == scenario
    assert repository.save_evaluation(evaluation) == evaluation
    assert repository.save_evaluation(evaluation) == evaluation

    reopened_variants = CadSystemVariantRepository(scene_repository)
    reopened_equipment = CadEquipmentRepository(
        scene_repository,
        reopened_variants,
    )
    reopened_directivity = CadDirectivityRepository(
        scene_repository,
        reopened_equipment,
    )
    reopened = CadCoverageRepository(
        scene_repository,
        reopened_variants,
        reopened_equipment,
        reopened_directivity,
    )
    assert reopened.get_scenario(scenario.scenario_id) == scenario
    assert reopened.get_evaluation(evaluation.evaluation_id) == evaluation
    assert reopened.list_scenarios() == (scenario,)
    assert reopened.list_evaluations_for_variant(variant.variant_id) == (
        evaluation,
    )
    assert reopened.list_evaluations_for_scenario(scenario.scenario_id) == (
        evaluation,
    )


def _forged_coverage_evaluation(
    evaluation: CoverageEvaluation,
) -> CoverageEvaluation:
    """Return a self-hash-valid evaluation carrying a fabricated frequency value."""

    payload = evaluation.model_dump(mode='json')
    seat_results = [dict(item) for item in payload['seat_results']]
    off_axis = dict(seat_results[1])
    frequency_results = [dict(item) for item in off_axis['frequency_results']]
    first = dict(frequency_results[0])
    first['relative_level_db'] = float(first['relative_level_db']) + 3.0
    frequency_results[0] = first
    off_axis['frequency_results'] = frequency_results
    seat_results[1] = off_axis
    payload['seat_results'] = seat_results
    identity = {
        key: value
        for key, value in payload.items()
        if key not in ('evaluation_id', 'evaluation_sha256')
    }
    digest = sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()
    payload['evaluation_id'] = f'coverage-{digest[:24]}'
    payload['evaluation_sha256'] = digest
    return CoverageEvaluation.model_validate(payload)


def test_fabricated_self_hashed_evaluation_is_rejected_on_save_and_read(
    tmp_path: Path,
) -> None:
    (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
        directivity_repository,
        definition,
        dataset,
        variant,
        scenario,
    ) = _fixture(tmp_path)
    evaluation = evaluate_coverage(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        directivity_dataset=dataset,
        scenario=scenario,
    )
    repository = CadCoverageRepository(
        scene_repository,
        variant_repository,
        equipment_repository,
        directivity_repository,
    )
    repository.save_scenario(scenario)

    forged = _forged_coverage_evaluation(evaluation)
    assert forged.evaluation_sha256 != evaluation.evaluation_sha256
    assert (
        forged.seat_results[1].frequency_results[0].relative_level_db
        == pytest.approx(
            float(
                evaluation.seat_results[1]
                .frequency_results[0]
                .relative_level_db
            )
            + 3.0
        )
    )

    with pytest.raises(
        ValueError,
        match='coverage evaluation does not match evaluator authority',
    ):
        repository.save_evaluation(forged)

    repository.save_evaluation(evaluation)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            """
            UPDATE cad_coverage_evaluations
            SET evaluation_id=?, evaluation_sha256=?, payload_json=?
            WHERE evaluation_id=?
            """,
            (
                forged.evaluation_id,
                forged.evaluation_sha256,
                forged.model_dump_json(),
                evaluation.evaluation_id,
            ),
        )

    with pytest.raises(
        ValueError,
        match='coverage evaluation does not match evaluator authority',
    ):
        repository.get_evaluation(forged.evaluation_id)
    with pytest.raises(
        ValueError,
        match='coverage evaluation does not match evaluator authority',
    ):
        repository.list_evaluations_for_variant(variant.variant_id)
    with pytest.raises(
        ValueError,
        match='coverage evaluation does not match evaluator authority',
    ):
        repository.list_evaluations_for_scenario(scenario.scenario_id)


def test_coverage_maximize_and_loss_minimize_use_direction_aware_pareto(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        revision,
        variant_repository,
        _equipment_repository,
        _directivity_repository,
        definition,
        dataset,
        baseline_variant,
        scenario,
    ) = _fixture(tmp_path)

    half_angle = radians(22.5)
    aimed_source = _speaker(
        aim=Direction3(
            x=sin(half_angle),
            y=cos(half_angle),
            z=0.0,
        )
    )
    aimed_variant = _variant(
        variant_repository,
        revision,
        definition,
        replacement=aimed_source,
        name='Aim between seats',
    )

    baseline_vector = coverage_objective_vector(
        evaluate_coverage(
            revision=revision,
            variant=baseline_variant,
            equipment_definition=definition,
            directivity_dataset=dataset,
            scenario=scenario,
        )
    )
    aimed_vector = coverage_objective_vector(
        evaluate_coverage(
            revision=revision,
            variant=aimed_variant,
            equipment_definition=definition,
            directivity_dataset=dataset,
            scenario=scenario,
        )
    )

    coverage_id = 'o100d.coverage.useful_fraction'
    loss_id = 'o100d.directivity.worst_seat_off_axis_loss_db'
    assert baseline_vector.metric(coverage_id).value == pytest.approx(0.5)
    assert aimed_vector.metric(coverage_id).value == pytest.approx(1.0)
    assert baseline_vector.metric(loss_id).value == pytest.approx(9.0)
    assert aimed_vector.metric(loss_id).value == pytest.approx(4.5)
    assert baseline_vector.metric(coverage_id).direction == 'maximize'
    assert baseline_vector.metric(loss_id).direction == 'minimize'

    result = pareto_front(
        (baseline_vector, aimed_vector),
        (coverage_id, loss_id),
    )
    assert result.algorithm_version == 'pareto-front-2'
    assert result.non_dominated_candidate_ids == (aimed_variant.variant_id,)
    assert result.dominated_by[baseline_variant.variant_id] == (
        aimed_variant.variant_id,
    )
