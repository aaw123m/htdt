from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from math import log10
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_amplifier_headroom import (
    AmplifierChannelCountCondition,
    AmplifierLoadDomain,
    ElectricalValue,
    PlaybackChainEvaluation,
    PlaybackRouting,
    SimultaneousChannelCondition,
    amplifier_headroom_objective_vector,
    build_amplifier_output_capability,
    build_playback_chain_scenario,
    build_speaker_electrical_load_authority,
    evaluate_playback_chain,
)
from htdt.cad_amplifier_headroom_repository import CadAmplifierHeadroomRepository
from htdt.cad_direct_level import DirectLevelFrequencyBand
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
from htdt.pareto import pareto_front


NOW = '2026-09-19T13:45:00+00:00'


def _save_equipment(repository, definition) -> None:
    """Persist explicit manual evidence for every cited provenance, then save."""
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='equipment-test-fixture',
        recorded_at_utc=NOW,
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)


DOCUMENT_ID = 'o100d-amplifier-headroom-fixture'


def _provenance(name: str, digit: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name=name,
        source_version='2026-09-19',
        source_reference='issue-169-amplifier-headroom-fixture',
        source_sha256=digit * 64,
    )


def _equipment(
    *,
    definition_id: str = 'speaker-a',
    source_digit: str = '1',
    sensitivity_quantity: str = 'voltage_v_rms',
    sensitivity_input_value: float = 2.83,
    sensitivity_db_spl: float | None = 88.0,
    continuous_db_spl: float | None = 110.0,
    peak_db_spl: float | None = 116.0,
):
    provenance = _provenance(definition_id, source_digit)
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
                input_quantity=sensitivity_quantity,
                input_value=sensitivity_input_value,
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


def _repositories(tmp_path: Path):
    database = tmp_path / 'cad.sqlite3'
    scene_repository = SceneRepository(database)
    revision = scene_repository.save(
        SceneDocument(
            document_id=DOCUMENT_ID,
            schema_version=2,
            room=RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5),
            entities=(_speaker(),),
        ),
        parent_revision_id=None,
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository,
        variant_repository,
    )
    return (
        database,
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    )


def _persist_variant(
    *,
    variant_repository: CadSystemVariantRepository,
    equipment_repository: CadEquipmentRepository,
    revision,
    definition,
    name: str,
):
    _save_equipment(equipment_repository, definition)
    variant = build_system_variant(
        baseline=revision,
        name=name,
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(),
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='speaker-fl',
                equipment_definition_id=definition.definition_id,
                equipment_definition_version=definition.version,
                equipment_definition_sha256=definition.semantic_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    return variant


def _load(
    definition,
    *,
    semantics: str = 'exact_resistive_reference',
    resistance_ohm: float = 8.0,
    load_id: str = 'speaker-a-load',
):
    return build_speaker_electrical_load_authority(
        load_id=load_id,
        version='1',
        equipment_definition=definition,
        semantics=semantics,
        resistance_ohm=resistance_ohm,
        valid_frequency_band=FrequencyDomain(
            minimum_hz=100.0,
            maximum_hz=10000.0,
        ),
        provenance=_provenance(load_id, '2'),
    )


def _amplifier(
    *,
    capability_id: str = 'amp-a-output-fl',
    output_id: str = 'amp-out-fl',
    quantity: str = 'voltage_v_rms',
    continuous_value: float = 10.0,
    peak_value: float = 20.0,
    continuous_duration_s: float = 60.0,
    peak_duration_s: float = 0.1,
    minimum_load_ohm: float = 4.0,
    maximum_load_ohm: float = 8.0,
    simultaneous_channel_count: int = 1,
    shared_supply_evidence: bool = False,
    source_digit: str = '3',
):
    provenance = _provenance(capability_id, source_digit)
    return build_amplifier_output_capability(
        capability_id=capability_id,
        version='1',
        identity_kind='user_defined',
        user_label=capability_id,
        output_id=output_id,
        provenance=(provenance,),
        supported_load=AmplifierLoadDomain(
            reference_load_ohm=8.0,
            minimum_load_ohm=minimum_load_ohm,
            maximum_load_ohm=maximum_load_ohm,
        ),
        continuous_capability=ElectricalValue(
            quantity=quantity,
            value=continuous_value,
        ),
        peak_capability=ElectricalValue(
            quantity=quantity,
            value=peak_value,
        ),
        continuous_duration_s=continuous_duration_s,
        peak_duration_s=peak_duration_s,
        gain_db=26.0,
        reference_input=ElectricalValue(
            quantity='voltage_v_rms',
            value=1.0,
        ),
        clipping_reference_definition='onset of clipping at evidenced output',
        valid_frequency_band=FrequencyDomain(
            minimum_hz=100.0,
            maximum_hz=10000.0,
        ),
        weighting='unweighted',
        channel_count_condition=AmplifierChannelCountCondition(
            simultaneous_channel_count=simultaneous_channel_count,
            shared_supply_evidence=shared_supply_evidence,
            condition_description=(
                'single channel driven'
                if simultaneous_channel_count == 1
                else 'two channels simultaneously driven with evidenced shared supply'
            ),
        ),
    )


def _scenario(
    *,
    revision,
    variant,
    definition,
    amplifier,
    load,
    requested_output_quantity: str = 'voltage_v_rms',
    requested_continuous_output_value: float = 6.0,
    requested_peak_output_value: float = 12.0,
    continuous_duration_s: float = 60.0,
    peak_duration_s: float = 0.1,
    simultaneous_output_ids: tuple[str, ...] | None = None,
    target_spl_db_spl: float = 95.0,
):
    output_ids = (
        (amplifier.output_id,)
        if simultaneous_output_ids is None
        else simultaneous_output_ids
    )
    return build_playback_chain_scenario(
        revision=revision,
        variant=variant,
        source_equipment=definition,
        amplifier_capability=amplifier,
        speaker_load=load,
        routing=PlaybackRouting(
            source_entity_id='speaker-fl',
            channel_role_id='FL',
            amplifier_output_id=amplifier.output_id,
        ),
        requested_input=ElectricalValue(
            quantity='voltage_v_rms',
            value=1.0,
        ),
        requested_output_quantity=requested_output_quantity,
        requested_continuous_output_value=requested_continuous_output_value,
        requested_peak_output_value=requested_peak_output_value,
        continuous_duration_s=continuous_duration_s,
        peak_duration_s=peak_duration_s,
        frequency_band=DirectLevelFrequencyBand(
            low_hz=100.0,
            high_hz=10000.0,
        ),
        weighting='unweighted',
        target_spl_db_spl=target_spl_db_spl,
        target_reference_condition='single-channel direct target at 1 m',
        acoustic_target_distance_m=1.0,
        target_mode='continuous',
        simultaneous_channel_condition=SimultaneousChannelCondition(
            output_ids=output_ids,
        ),
    )


def _evaluate(revision, variant, definition, amplifier, load, scenario):
    return evaluate_playback_chain(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        amplifier_capability=amplifier,
        speaker_load=load,
        scenario=scenario,
    )


def test_voltage_capability_has_explicit_continuous_peak_and_target_margins(
    tmp_path: Path,
) -> None:
    _, _, revision, variant_repository, equipment_repository = _repositories(
        tmp_path
    )
    definition = _equipment()
    variant = _persist_variant(
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
        revision=revision,
        definition=definition,
        name='Voltage amplifier variant',
    )
    load = _load(definition)
    amplifier = _amplifier()
    scenario = _scenario(
        revision=revision,
        variant=variant,
        definition=definition,
        amplifier=amplifier,
        load=load,
    )

    evaluation = _evaluate(
        revision,
        variant,
        definition,
        amplifier,
        load,
        scenario,
    )

    assert evaluation.continuous_electrical_margin.unit == 'V RMS'
    assert evaluation.continuous_electrical_margin.value == pytest.approx(4.0)
    assert evaluation.peak_electrical_margin.value == pytest.approx(8.0)
    assert evaluation.continuous_amplifier_spl_ceiling.value == pytest.approx(
        88.0 + 20.0 * log10(10.0 / 2.83)
    )
    assert evaluation.amplifier_constrained_target_margin.value == pytest.approx(
        evaluation.continuous_amplifier_spl_ceiling.value - 95.0
    )
    assert evaluation.continuous_limiter == 'amplifier'
    assert evaluation.peak_limiter == 'amplifier'


def test_power_capability_converts_only_with_exact_resistive_reference(
    tmp_path: Path,
) -> None:
    _, _, revision, variant_repository, equipment_repository = _repositories(
        tmp_path
    )
    definition = _equipment()
    variant = _persist_variant(
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
        revision=revision,
        definition=definition,
        name='Power amplifier variant',
    )
    exact_load = _load(definition)
    amplifier = _amplifier(
        quantity='power_w',
        continuous_value=50.0,
        peak_value=100.0,
    )
    scenario = _scenario(
        revision=revision,
        variant=variant,
        definition=definition,
        amplifier=amplifier,
        load=exact_load,
        requested_output_quantity='voltage_v_rms',
        requested_continuous_output_value=10.0,
        requested_peak_output_value=20.0,
    )
    evaluation = _evaluate(
        revision,
        variant,
        definition,
        amplifier,
        exact_load,
        scenario,
    )

    assert evaluation.continuous_electrical_margin.value == pytest.approx(10.0)
    assert evaluation.peak_electrical_margin.value == pytest.approx(
        (100.0 * 8.0) ** 0.5 - 20.0
    )

    nominal_load = _load(
        definition,
        semantics='nominal_impedance_only',
        load_id='speaker-a-nominal-load',
    )
    nominal_scenario = _scenario(
        revision=revision,
        variant=variant,
        definition=definition,
        amplifier=amplifier,
        load=nominal_load,
        requested_output_quantity='voltage_v_rms',
    )
    nominal_evaluation = _evaluate(
        revision,
        variant,
        definition,
        amplifier,
        nominal_load,
        nominal_scenario,
    )
    assert nominal_evaluation.continuous_electrical_margin.state == 'unsupported'
    assert 'nominal impedance' in (
        nominal_evaluation.continuous_electrical_margin.reason or ''
    )
    assert nominal_evaluation.continuous_amplifier_spl_ceiling.state == 'unsupported'


def test_incompatible_or_missing_load_fails_closed(tmp_path: Path) -> None:
    _, _, revision, variant_repository, equipment_repository = _repositories(
        tmp_path
    )
    definition = _equipment()
    variant = _persist_variant(
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
        revision=revision,
        definition=definition,
        name='Load validation variant',
    )
    amplifier = _amplifier(minimum_load_ohm=6.0)
    incompatible_load = _load(
        definition,
        resistance_ohm=4.0,
        load_id='speaker-a-4ohm-reference',
    )
    incompatible_scenario = _scenario(
        revision=revision,
        variant=variant,
        definition=definition,
        amplifier=amplifier,
        load=incompatible_load,
    )
    incompatible = _evaluate(
        revision,
        variant,
        definition,
        amplifier,
        incompatible_load,
        incompatible_scenario,
    )
    assert incompatible.continuous_electrical_margin.state == 'unsupported'
    assert 'outside amplifier evidenced load domain' in (
        incompatible.continuous_electrical_margin.reason or ''
    )
    assert incompatible.continuous_limiter == 'unknown'

    missing_load_scenario = _scenario(
        revision=revision,
        variant=variant,
        definition=definition,
        amplifier=amplifier,
        load=None,
    )
    missing = _evaluate(
        revision,
        variant,
        definition,
        amplifier,
        None,
        missing_load_scenario,
    )
    assert missing.continuous_electrical_margin.state == 'unsupported'
    assert 'load authority is missing' in (
        missing.continuous_electrical_margin.reason or ''
    )


def test_duration_and_channel_count_conditions_are_not_promoted(
    tmp_path: Path,
) -> None:
    _, _, revision, variant_repository, equipment_repository = _repositories(
        tmp_path
    )
    definition = _equipment()
    variant = _persist_variant(
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
        revision=revision,
        definition=definition,
        name='Condition guard variant',
    )
    load = _load(definition)
    amplifier = _amplifier()
    duration_mismatch = _scenario(
        revision=revision,
        variant=variant,
        definition=definition,
        amplifier=amplifier,
        load=load,
        continuous_duration_s=120.0,
    )
    duration_evaluation = _evaluate(
        revision,
        variant,
        definition,
        amplifier,
        load,
        duration_mismatch,
    )
    assert duration_evaluation.continuous_electrical_margin.state == 'unsupported'
    assert duration_evaluation.peak_electrical_margin.state == 'available'

    multi_channel = _scenario(
        revision=revision,
        variant=variant,
        definition=definition,
        amplifier=amplifier,
        load=load,
        simultaneous_output_ids=(amplifier.output_id, 'amp-out-fr'),
    )
    multi_evaluation = _evaluate(
        revision,
        variant,
        definition,
        amplifier,
        load,
        multi_channel,
    )
    assert multi_evaluation.continuous_electrical_margin.state == 'unsupported'
    assert 'single-channel amplifier capability' in (
        multi_evaluation.continuous_electrical_margin.reason or ''
    )

    with pytest.raises(
        ValueError,
        match='multi-channel capability requires explicit shared-supply evidence',
    ):
        _amplifier(
            simultaneous_channel_count=2,
            shared_supply_evidence=False,
        )


def test_explicit_multi_channel_shared_supply_capability_is_usable(
    tmp_path: Path,
) -> None:
    _, _, revision, variant_repository, equipment_repository = _repositories(
        tmp_path
    )
    definition = _equipment()
    variant = _persist_variant(
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
        revision=revision,
        definition=definition,
        name='Shared supply variant',
    )
    load = _load(definition)
    amplifier = _amplifier(
        simultaneous_channel_count=2,
        shared_supply_evidence=True,
    )
    scenario = _scenario(
        revision=revision,
        variant=variant,
        definition=definition,
        amplifier=amplifier,
        load=load,
        simultaneous_output_ids=(amplifier.output_id, 'amp-out-fr'),
    )
    evaluation = _evaluate(
        revision,
        variant,
        definition,
        amplifier,
        load,
        scenario,
    )
    assert evaluation.continuous_electrical_margin.state == 'available'
    assert evaluation.peak_electrical_margin.state == 'available'


def test_speaker_and_amplifier_limiting_states_require_exact_acoustic_comparison(
    tmp_path: Path,
) -> None:
    _, _, revision, variant_repository, equipment_repository = _repositories(
        tmp_path
    )
    definition = _equipment(
        continuous_db_spl=95.0,
        peak_db_spl=100.0,
    )
    variant = _persist_variant(
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
        revision=revision,
        definition=definition,
        name='Speaker limited variant',
    )
    load = _load(definition)
    amplifier = _amplifier(
        continuous_value=40.0,
        peak_value=80.0,
    )
    scenario = _scenario(
        revision=revision,
        variant=variant,
        definition=definition,
        amplifier=amplifier,
        load=load,
    )
    evaluation = _evaluate(
        revision,
        variant,
        definition,
        amplifier,
        load,
        scenario,
    )
    assert evaluation.continuous_limiter == 'speaker'
    assert evaluation.peak_limiter == 'speaker'

    no_sensitivity = _equipment(
        definition_id='speaker-no-sensitivity',
        source_digit='4',
        sensitivity_db_spl=None,
    )
    no_sensitivity_variant = _persist_variant(
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
        revision=revision,
        definition=no_sensitivity,
        name='Unknown limiter variant',
    )
    no_sensitivity_load = _load(
        no_sensitivity,
        load_id='speaker-no-sensitivity-load',
    )
    no_sensitivity_scenario = _scenario(
        revision=revision,
        variant=no_sensitivity_variant,
        definition=no_sensitivity,
        amplifier=amplifier,
        load=no_sensitivity_load,
    )
    no_sensitivity_evaluation = _evaluate(
        revision,
        no_sensitivity_variant,
        no_sensitivity,
        amplifier,
        no_sensitivity_load,
        no_sensitivity_scenario,
    )
    assert no_sensitivity_evaluation.continuous_electrical_margin.state == 'available'
    assert no_sensitivity_evaluation.continuous_amplifier_spl_ceiling.state == 'missing'
    assert no_sensitivity_evaluation.continuous_limiter == 'unknown'


def test_objectives_are_maximize_and_conditions_gate_comparison(
    tmp_path: Path,
) -> None:
    _, _, revision, variant_repository, equipment_repository = _repositories(
        tmp_path
    )
    definition = _equipment()
    weak_variant = _persist_variant(
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
        revision=revision,
        definition=definition,
        name='Weak amplifier variant',
    )
    strong_variant = _persist_variant(
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
        revision=revision,
        definition=definition,
        name='Strong amplifier variant',
    )
    load = _load(definition)
    weak_amp = _amplifier(
        capability_id='weak-amp',
        continuous_value=8.0,
        peak_value=16.0,
        source_digit='5',
    )
    strong_amp = _amplifier(
        capability_id='strong-amp',
        continuous_value=12.0,
        peak_value=24.0,
        source_digit='6',
    )
    weak_scenario = _scenario(
        revision=revision,
        variant=weak_variant,
        definition=definition,
        amplifier=weak_amp,
        load=load,
    )
    strong_scenario = _scenario(
        revision=revision,
        variant=strong_variant,
        definition=definition,
        amplifier=strong_amp,
        load=load,
    )
    weak_vector = amplifier_headroom_objective_vector(
        _evaluate(
            revision,
            weak_variant,
            definition,
            weak_amp,
            load,
            weak_scenario,
        )
    )
    strong_vector = amplifier_headroom_objective_vector(
        _evaluate(
            revision,
            strong_variant,
            definition,
            strong_amp,
            load,
            strong_scenario,
        )
    )

    continuous = weak_vector.metric('o100d.electrical_headroom.continuous')
    peak = weak_vector.metric('o100d.electrical_headroom.peak')
    target = weak_vector.metric('o100d.amplifier_constrained_target_margin')
    assert continuous.direction == 'maximize'
    assert peak.direction == 'maximize'
    assert target.direction == 'maximize'
    assert continuous.unit == 'V RMS'
    assert target.unit == 'dB'
    assert continuous.definition_id == strong_vector.metric(
        'o100d.electrical_headroom.continuous'
    ).definition_id

    result = pareto_front(
        (weak_vector, strong_vector),
        (
            'o100d.electrical_headroom.continuous',
            'o100d.electrical_headroom.peak',
        ),
    )
    assert result.non_dominated_candidate_ids == (strong_variant.variant_id,)

    duration_scenario = _scenario(
        revision=revision,
        variant=weak_variant,
        definition=definition,
        amplifier=weak_amp,
        load=load,
        continuous_duration_s=120.0,
    )
    duration_vector = amplifier_headroom_objective_vector(
        _evaluate(
            revision,
            weak_variant,
            definition,
            weak_amp,
            load,
            duration_scenario,
        )
    )
    assert continuous.definition_id != duration_vector.metric(
        'o100d.electrical_headroom.continuous'
    ).definition_id

    power_scenario = _scenario(
        revision=revision,
        variant=weak_variant,
        definition=definition,
        amplifier=weak_amp,
        load=load,
        requested_output_quantity='power_w',
        requested_continuous_output_value=4.0,
        requested_peak_output_value=8.0,
    )
    power_vector = amplifier_headroom_objective_vector(
        _evaluate(
            revision,
            weak_variant,
            definition,
            weak_amp,
            load,
            power_scenario,
        )
    )
    assert power_vector.metric('o100d.electrical_headroom.continuous').unit == 'W'
    assert continuous.definition_id != power_vector.metric(
        'o100d.electrical_headroom.continuous'
    ).definition_id

    different_load = _load(
        definition,
        resistance_ohm=6.0,
        load_id='speaker-a-6ohm-reference',
    )
    different_load_scenario = _scenario(
        revision=revision,
        variant=weak_variant,
        definition=definition,
        amplifier=weak_amp,
        load=different_load,
    )
    assert weak_scenario.comparison_sha256 != (
        different_load_scenario.comparison_sha256
    )


def test_append_only_save_reopen_revalidates_exact_authorities(
    tmp_path: Path,
) -> None:
    (
        database,
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    definition = _equipment()
    variant = _persist_variant(
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
        revision=revision,
        definition=definition,
        name='Persistence variant',
    )
    load = _load(definition)
    amplifier = _amplifier()
    scenario = _scenario(
        revision=revision,
        variant=variant,
        definition=definition,
        amplifier=amplifier,
        load=load,
    )
    evaluation = _evaluate(
        revision,
        variant,
        definition,
        amplifier,
        load,
        scenario,
    )
    repository = CadAmplifierHeadroomRepository(
        scene_repository,
        variant_repository,
        equipment_repository,
    )

    with pytest.raises(
        ValueError,
        match='unpersisted amplifier capability',
    ):
        repository.save_scenario(scenario)

    repository.save_amplifier_capability(amplifier)
    with pytest.raises(
        ValueError,
        match='unpersisted speaker load authority',
    ):
        repository.save_scenario(scenario)

    repository.save_speaker_load(load)
    assert repository.save_scenario(scenario) == scenario
    assert repository.save_evaluation(evaluation) == evaluation
    assert repository.save_amplifier_capability(amplifier) == amplifier
    assert repository.save_speaker_load(load) == load
    assert repository.save_scenario(scenario) == scenario
    assert repository.save_evaluation(evaluation) == evaluation

    reopened_scene = SceneRepository(database)
    reopened_variants = CadSystemVariantRepository(reopened_scene)
    reopened_equipment = CadEquipmentRepository(
        reopened_scene,
        reopened_variants,
    )
    reopened = CadAmplifierHeadroomRepository(
        reopened_scene,
        reopened_variants,
        reopened_equipment,
    )

    assert reopened.get_amplifier_capability(
        amplifier.capability_id,
        amplifier.version,
    ) == amplifier
    assert reopened.get_speaker_load(load.load_id, load.version) == load
    assert reopened.get_scenario(scenario.scenario_id) == scenario
    assert reopened.get_evaluation(evaluation.evaluation_id) == evaluation
    assert reopened.list_evaluations_for_variant(variant.variant_id) == (
        evaluation,
    )


def _forged_playback_chain_evaluation(
    evaluation: PlaybackChainEvaluation,
    mutate,
) -> PlaybackChainEvaluation:
    """Return a self-hash-valid evaluation carrying fabricated results."""

    payload = evaluation.model_dump(mode='json')
    mutate(payload)
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
    payload['evaluation_id'] = f'amp-headroom-{digest[:24]}'
    payload['evaluation_sha256'] = digest
    return PlaybackChainEvaluation.model_validate(payload)


def test_fabricated_self_hashed_evaluation_is_rejected_on_save_and_read(
    tmp_path: Path,
) -> None:
    (
        _database,
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    ) = _repositories(tmp_path)
    definition = _equipment()
    variant = _persist_variant(
        variant_repository=variant_repository,
        equipment_repository=equipment_repository,
        revision=revision,
        definition=definition,
        name='Forgery variant',
    )
    load = _load(definition)
    amplifier = _amplifier()
    scenario = _scenario(
        revision=revision,
        variant=variant,
        definition=definition,
        amplifier=amplifier,
        load=load,
    )
    evaluation = _evaluate(
        revision,
        variant,
        definition,
        amplifier,
        load,
        scenario,
    )
    repository = CadAmplifierHeadroomRepository(
        scene_repository,
        variant_repository,
        equipment_repository,
    )
    repository.save_amplifier_capability(amplifier)
    repository.save_speaker_load(load)
    repository.save_scenario(scenario)

    def fabricate_margin(payload: dict) -> None:
        margin = dict(payload['continuous_electrical_margin'])
        margin['value'] = float(margin['value']) + 2.0
        payload['continuous_electrical_margin'] = margin

    def fabricate_limiter(payload: dict) -> None:
        payload['continuous_limiter'] = 'speaker'

    forged_margin = _forged_playback_chain_evaluation(
        evaluation,
        fabricate_margin,
    )
    forged_limiter = _forged_playback_chain_evaluation(
        evaluation,
        fabricate_limiter,
    )
    assert forged_margin.evaluation_sha256 != evaluation.evaluation_sha256
    assert forged_margin.continuous_electrical_margin.value == pytest.approx(
        float(evaluation.continuous_electrical_margin.value) + 2.0
    )
    assert forged_limiter.continuous_limiter == 'speaker'

    for forged in (forged_margin, forged_limiter):
        with pytest.raises(
            ValueError,
            match='playback-chain evaluation does not match evaluator authority',
        ):
            repository.save_evaluation(forged)

    repository.save_evaluation(evaluation)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            """
            UPDATE cad_playback_chain_evaluations
            SET evaluation_id=?, evaluation_sha256=?, payload_json=?
            WHERE evaluation_id=?
            """,
            (
                forged_margin.evaluation_id,
                forged_margin.evaluation_sha256,
                forged_margin.model_dump_json(),
                evaluation.evaluation_id,
            ),
        )

    with pytest.raises(
        ValueError,
        match='playback-chain evaluation does not match evaluator authority',
    ):
        repository.get_evaluation(forged_margin.evaluation_id)
    with pytest.raises(
        ValueError,
        match='playback-chain evaluation does not match evaluator authority',
    ):
        repository.list_evaluations_for_variant(variant.variant_id)
    # TopologyComparison resolves headroom evidence exclusively through
    # resolve_evaluation_exact; the tampered row must fail closed there too.
    with pytest.raises(
        ValueError,
        match='playback-chain evaluation does not match evaluator authority',
    ):
        repository.resolve_evaluation_exact(
            forged_margin.evaluation_id,
            evaluation_sha256=forged_margin.evaluation_sha256,
            document_id=scenario.document_id,
            scene_revision_id=scenario.scene_revision_id,
            scene_content_hash=scenario.scene_content_hash,
            variant_id=scenario.variant_id,
            variant_sha256=scenario.variant_sha256,
        )
