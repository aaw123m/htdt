from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from math import log10
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_direct_level import (
    DirectLevelEvaluation,
    DirectLevelFrequencyBand,
    ReferenceInputCondition,
    SeatPopulation,
    build_playback_excitation_scenario,
    direct_level_objective_vector,
    evaluate_direct_level,
)
from htdt.cad_direct_level_repository import CadDirectLevelRepository
from htdt.cad_equipment import (
    DirectivityCapability,
    EquipmentDataProvenance,
    FrequencyDomain,
    SensitivityReference,
    SplCapability,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.pareto import ParetoError, pareto_front


NOW = '2026-09-19T12:30:00+00:00'


def _save_equipment(repository, definition) -> None:
    """Persist explicit manual evidence for every cited provenance, then save."""
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='equipment-test-fixture',
        recorded_at_utc=NOW,
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)


DOCUMENT_ID = 'o100d-direct-level-fixture'


def _provenance(source_name: str, source_hash: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name=source_name,
        source_version='2026-09-19',
        source_reference='o100d-focused-fixture',
        source_sha256=source_hash,
    )


def _equipment(
    *,
    definition_id: str,
    source_hash: str,
    sensitivity_db_spl: float | None = 88.0,
    input_quantity: str = 'voltage_v_rms',
    input_value: float = 2.83,
    continuous_db_spl: float | None = 105.0,
    peak_db_spl: float | None = 111.0,
):
    provenance = _provenance(definition_id, source_hash)
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=10000.0)
    return build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(),
        sensitivity=(
            None
            if sensitivity_db_spl is None
            else SensitivityReference(
                level_db_spl=sensitivity_db_spl,
                input_quantity=input_quantity,
                input_value=input_value,
                distance_m=1.0,
                valid_frequency_domain=domain,
                weighting=None,
                provenance=provenance,
            )
        ),
        spl_capability=(
            None
            if continuous_db_spl is None and peak_db_spl is None
            else SplCapability(
                continuous_db_spl=continuous_db_spl,
                peak_db_spl=peak_db_spl,
                reference_distance_m=1.0,
                valid_frequency_domain=domain,
                continuous_duration_s=(
                    None if continuous_db_spl is None else 60.0
                ),
                peak_duration_s=None if peak_db_spl is None else 0.1,
                provenance=provenance,
            )
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
        size_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
    )


def _seat(entity_id: str, y_m: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='seat',
        name=entity_id,
        position=Position3(x_m=1.0, y_m=y_m, z_m=0.5),
        size_m=Size3(x_m=0.6, y_m=0.8, z_m=1.0),
        acoustic_reference_offset_m=Offset3(z_m=0.5),
    )


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5),
        entities=(
            _speaker(),
            _seat('seat-near', 3.0),
            _seat('seat-far', 5.0),
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


def _persist_variant(
    variant_repository: CadSystemVariantRepository,
    equipment_repository: CadEquipmentRepository,
    revision,
    definition,
):
    _save_equipment(equipment_repository, definition)
    binding = EquipmentBindingRef(
        entity_id='speaker-fl',
        equipment_definition_id=definition.definition_id,
        equipment_definition_version=definition.version,
        equipment_definition_sha256=definition.semantic_sha256,
    )
    variant = build_system_variant(
        baseline=revision,
        name=f'Variant {definition.definition_id}',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(),
        equipment_bindings=(binding,),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    return variant


def _scenario(
    *,
    input_quantity: str = 'voltage_v_rms',
    input_value: float = 2.83,
    low_hz: float = 100.0,
    high_hz: float = 10000.0,
):
    return build_playback_excitation_scenario(
        source_entity_id='speaker-fl',
        channel_role_id='FL',
        reference_input=ReferenceInputCondition(
            input_quantity=input_quantity,
            input_value=input_value,
        ),
        target_spl_db_spl=75.0,
        target_reference_condition='single-channel direct target at each seat',
        continuous_reference_duration_s=60.0,
        peak_reference_duration_s=0.1,
        frequency_band=DirectLevelFrequencyBand(
            low_hz=low_hz,
            high_hz=high_hz,
        ),
        weighting='unweighted',
        receiver_population=SeatPopulation(
            population_id='two-seat-fixture',
            seat_entity_ids=('seat-near', 'seat-far'),
        ),
    )


def test_direct_distance_level_target_margin_and_headroom_are_explicit(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    definition = _equipment(
        definition_id='fixture-voltage-speaker',
        source_hash='1' * 64,
    )
    variant = _persist_variant(
        variant_repository,
        equipment_repository,
        revision,
        definition,
    )
    scenario = _scenario()

    evaluation = evaluate_direct_level(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        scenario=scenario,
    )
    repeated = evaluate_direct_level(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        scenario=scenario,
    )

    near, far = evaluation.seat_results
    expected_near = 88.0 + 20.0 * log10(1.0 / 2.0)
    expected_far = 88.0 + 20.0 * log10(1.0 / 4.0)

    assert near.distance_m == pytest.approx(2.0)
    assert far.distance_m == pytest.approx(4.0)
    assert near.direct_level.value == pytest.approx(expected_near)
    assert far.direct_level.value == pytest.approx(expected_far)
    assert (
        evaluation.aggregates.worst_seat_direct_level.value
        == pytest.approx(expected_far)
    )
    assert (
        evaluation.aggregates.seat_to_seat_direct_level_spread.value
        == pytest.approx(expected_near - expected_far)
    )
    assert (
        evaluation.aggregates.worst_seat_target_margin.value
        == pytest.approx(expected_far - 75.0)
    )
    assert (
        evaluation.aggregates.worst_seat_continuous_headroom.value
        == pytest.approx(105.0 + 20.0 * log10(1.0 / 4.0) - 75.0)
    )
    assert (
        evaluation.aggregates.worst_seat_peak_headroom.value
        == pytest.approx(111.0 + 20.0 * log10(1.0 / 4.0) - 75.0)
    )
    assert scenario.distance_authority.model_version == '20log10-distance-ratio-1'
    assert (
        scenario.input_normalization_authority.model_version
        == 'voltage20-power10-log-ratio-1'
    )
    assert (
        scenario.level_semantics
        == 'direct_equipment_derived_no_room_gain_no_reflections'
    )
    assert evaluation.evaluation_id == repeated.evaluation_id
    assert evaluation.evaluation_sha256 == repeated.evaluation_sha256
    assert evaluation == repeated


def test_direct_level_objectives_use_direction_authority_and_pareto(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    base = _equipment(
        definition_id='fixture-base-speaker',
        source_hash='2' * 64,
    )
    stronger = _equipment(
        definition_id='fixture-stronger-speaker',
        source_hash='3' * 64,
        sensitivity_db_spl=90.0,
        continuous_db_spl=107.0,
        peak_db_spl=113.0,
    )
    base_variant = _persist_variant(
        variant_repository,
        equipment_repository,
        revision,
        base,
    )
    stronger_variant = _persist_variant(
        variant_repository,
        equipment_repository,
        revision,
        stronger,
    )
    scenario = _scenario()

    base_vector = direct_level_objective_vector(
        evaluate_direct_level(
            revision=revision,
            variant=base_variant,
            equipment_definition=base,
            scenario=scenario,
        )
    )
    stronger_vector = direct_level_objective_vector(
        evaluate_direct_level(
            revision=revision,
            variant=stronger_variant,
            equipment_definition=stronger,
            scenario=scenario,
        )
    )

    assert base_vector.metric(
        'o100d.target_margin.worst_seat_db'
    ).direction == 'maximize'
    assert base_vector.metric(
        'o100d.continuous_headroom.worst_seat_db'
    ).direction == 'maximize'
    assert base_vector.metric(
        'o100d.peak_headroom.worst_seat_db'
    ).direction == 'maximize'
    assert base_vector.metric(
        'o100d.direct_level.seat_to_seat_spread_db'
    ).direction == 'minimize'

    result = pareto_front(
        (base_vector, stronger_vector),
        (
            'o100d.target_margin.worst_seat_db',
            'o100d.continuous_headroom.worst_seat_db',
            'o100d.peak_headroom.worst_seat_db',
        ),
    )
    assert result.algorithm_version == 'pareto-front-2'
    assert result.non_dominated_candidate_ids == (stronger_variant.variant_id,)
    assert result.dominated_by[base_variant.variant_id] == (
        stronger_variant.variant_id,
    )


def test_capability_missing_variant_is_comparison_ineligible(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    supported = _equipment(
        definition_id='fixture-supported',
        source_hash='4' * 64,
    )
    missing = _equipment(
        definition_id='fixture-missing',
        source_hash='5' * 64,
        sensitivity_db_spl=None,
        continuous_db_spl=None,
        peak_db_spl=None,
    )
    supported_variant = _persist_variant(
        variant_repository,
        equipment_repository,
        revision,
        supported,
    )
    missing_variant = _persist_variant(
        variant_repository,
        equipment_repository,
        revision,
        missing,
    )
    scenario = _scenario()

    supported_vector = direct_level_objective_vector(
        evaluate_direct_level(
            revision=revision,
            variant=supported_variant,
            equipment_definition=supported,
            scenario=scenario,
        )
    )
    missing_evaluation = evaluate_direct_level(
        revision=revision,
        variant=missing_variant,
        equipment_definition=missing,
        scenario=scenario,
    )
    missing_vector = direct_level_objective_vector(missing_evaluation)

    assert missing_evaluation.seat_results[0].direct_level.state == 'missing'
    assert (
        missing_evaluation.aggregates.worst_seat_continuous_headroom.state
        == 'missing'
    )
    assert missing_vector.metric(
        'o100d.target_margin.worst_seat_db'
    ).value is None

    with pytest.raises(ParetoError, match='not comparison-eligible: missing'):
        pareto_front(
            (supported_vector, missing_vector),
            ('o100d.target_margin.worst_seat_db',),
        )


def test_frequency_domain_outside_equipment_authority_is_unsupported(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    definition = _equipment(
        definition_id='fixture-domain-speaker',
        source_hash='6' * 64,
    )
    variant = _persist_variant(
        variant_repository,
        equipment_repository,
        revision,
        definition,
    )

    evaluation = evaluate_direct_level(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        scenario=_scenario(low_hz=80.0),
    )

    assert evaluation.seat_results[0].direct_level.state == 'unsupported'
    assert evaluation.seat_results[0].continuous_headroom.state == 'unsupported'
    assert evaluation.seat_results[0].peak_headroom.state == 'unsupported'
    assert evaluation.aggregates.worst_seat_direct_level.value is None


def test_mismatched_excitation_reference_conditions_are_not_comparable(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    voltage_definition = _equipment(
        definition_id='fixture-voltage-reference',
        source_hash='7' * 64,
    )
    power_definition = _equipment(
        definition_id='fixture-power-reference',
        source_hash='8' * 64,
        input_quantity='power_w',
        input_value=1.0,
    )
    voltage_variant = _persist_variant(
        variant_repository,
        equipment_repository,
        revision,
        voltage_definition,
    )
    power_variant = _persist_variant(
        variant_repository,
        equipment_repository,
        revision,
        power_definition,
    )
    voltage_scenario = _scenario()
    power_scenario = _scenario(input_quantity='power_w', input_value=1.0)

    voltage_vector = direct_level_objective_vector(
        evaluate_direct_level(
            revision=revision,
            variant=voltage_variant,
            equipment_definition=voltage_definition,
            scenario=voltage_scenario,
        )
    )
    power_vector = direct_level_objective_vector(
        evaluate_direct_level(
            revision=revision,
            variant=power_variant,
            equipment_definition=power_definition,
            scenario=power_scenario,
        )
    )

    assert voltage_vector.metric(
        'o100d.target_margin.worst_seat_db'
    ).state == 'available'
    assert power_vector.metric(
        'o100d.target_margin.worst_seat_db'
    ).state == 'available'
    assert (
        voltage_vector.metric(
            'o100d.target_margin.worst_seat_db'
        ).definition_id
        != power_vector.metric(
            'o100d.target_margin.worst_seat_db'
        ).definition_id
    )

    with pytest.raises(
        ParetoError,
        match='definition/unit/direction/model mismatch',
    ):
        pareto_front(
            (voltage_vector, power_vector),
            ('o100d.target_margin.worst_seat_db',),
        )


def test_direct_level_scenario_and_evaluation_round_trip(
    tmp_path: Path,
) -> None:
    (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    definition = _equipment(
        definition_id='fixture-persisted-speaker',
        source_hash='9' * 64,
    )
    variant = _persist_variant(
        variant_repository,
        equipment_repository,
        revision,
        definition,
    )
    scenario = _scenario()
    evaluation = evaluate_direct_level(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        scenario=scenario,
    )

    repository = CadDirectLevelRepository(
        scene_repository,
        variant_repository,
        equipment_repository,
    )
    assert repository.save_scenario(scenario) == scenario
    assert repository.save_scenario(scenario) == scenario
    assert repository.save_evaluation(evaluation) == evaluation
    assert repository.save_evaluation(evaluation) == evaluation

    reopened = CadDirectLevelRepository(
        scene_repository,
        CadSystemVariantRepository(scene_repository),
        CadEquipmentRepository(
            scene_repository,
            CadSystemVariantRepository(scene_repository),
        ),
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


def _forged_direct_level_evaluation(
    evaluation: DirectLevelEvaluation,
) -> DirectLevelEvaluation:
    """Return a self-hash-valid evaluation carrying a fabricated seat value."""

    payload = evaluation.model_dump(mode='json')
    seat_results = [dict(item) for item in payload['seat_results']]
    first = dict(seat_results[0])
    direct_level = dict(first['direct_level'])
    direct_level['value'] = float(direct_level['value']) + 6.0
    first['direct_level'] = direct_level
    seat_results[0] = first
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
    payload['evaluation_id'] = f'o100d-{digest[:24]}'
    payload['evaluation_sha256'] = digest
    return DirectLevelEvaluation.model_validate(payload)


def test_fabricated_self_hashed_evaluation_is_rejected_on_save_and_read(
    tmp_path: Path,
) -> None:
    (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    definition = _equipment(
        definition_id='fixture-forged-speaker',
        source_hash='a' * 64,
    )
    variant = _persist_variant(
        variant_repository,
        equipment_repository,
        revision,
        definition,
    )
    scenario = _scenario()
    evaluation = evaluate_direct_level(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        scenario=scenario,
    )
    repository = CadDirectLevelRepository(
        scene_repository,
        variant_repository,
        equipment_repository,
    )
    repository.save_scenario(scenario)

    forged = _forged_direct_level_evaluation(evaluation)
    assert forged.evaluation_sha256 != evaluation.evaluation_sha256
    assert forged.seat_results[0].direct_level.value == pytest.approx(
        float(evaluation.seat_results[0].direct_level.value) + 6.0
    )

    with pytest.raises(
        ValueError,
        match='direct-level evaluation does not match evaluator authority',
    ):
        repository.save_evaluation(forged)

    repository.save_evaluation(evaluation)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            """
            UPDATE cad_direct_level_evaluations
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
        match='direct-level evaluation does not match evaluator authority',
    ):
        repository.get_evaluation(forged.evaluation_id)
    with pytest.raises(
        ValueError,
        match='direct-level evaluation does not match evaluator authority',
    ):
        repository.list_evaluations_for_variant(variant.variant_id)
    with pytest.raises(
        ValueError,
        match='direct-level evaluation does not match evaluator authority',
    ):
        repository.list_evaluations_for_scenario(scenario.scenario_id)
