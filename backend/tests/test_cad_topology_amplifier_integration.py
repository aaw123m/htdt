from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_amplifier_headroom import (
    AmplifierChannelCountCondition,
    AmplifierLoadDomain,
    ElectricalValue,
    PlaybackRouting,
    SimultaneousChannelCondition,
    amplifier_headroom_objective_definitions,
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
from htdt.cad_scene import Offset3, Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_standards import build_user_standards_profile
from htdt.cad_standards_repository import CadStandardsRepository
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.cad_topology_comparison import (
    ObjectiveEvidenceBinding,
    amplifier_headroom_evaluation_ref,
    build_system_topology_comparison_spec,
    build_variant_evaluation_bundle,
    compared_system_variant,
    evaluate_topology_comparison,
)
from htdt.cad_topology_comparison_repository import CadTopologyComparisonRepository
from htdt.optimization_objectives import ObjectiveVector




def _save_equipment(repository, definition) -> None:
    """Persist explicit manual evidence for every cited provenance, then save."""
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='equipment-test-fixture',
        recorded_at_utc='2026-01-01T00:00:00+00:00',
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)

DOCUMENT_ID = 'o100d-topology-amplifier-integration'


def _provenance(name: str, digit: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name=name,
        source_version='2026-09-19',
        source_reference='issue-169-topology-amplifier-integration',
        source_sha256=digit * 64,
    )


def _equipment():
    provenance = _provenance('speaker-fl', '1')
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=10000.0)
    return build_equipment_definition(
        definition_id='speaker-fl-definition',
        version='1',
        identity_kind='user_defined',
        user_label='Fixture FL',
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
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
            continuous_db_spl=110.0,
            peak_db_spl=116.0,
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


def _amplifier(
    capability_id: str,
    digit: str,
    continuous: float,
    peak: float,
    *,
    quantity: str = 'voltage_v_rms',
    continuous_duration_s: float = 60.0,
):
    provenance = _provenance(capability_id, digit)
    return build_amplifier_output_capability(
        capability_id=capability_id,
        version='1',
        identity_kind='user_defined',
        user_label=capability_id,
        output_id='amp-out-fl',
        provenance=(provenance,),
        supported_load=AmplifierLoadDomain(
            reference_load_ohm=8.0,
            minimum_load_ohm=4.0,
            maximum_load_ohm=8.0,
        ),
        continuous_capability=ElectricalValue(quantity=quantity, value=continuous),
        peak_capability=ElectricalValue(quantity=quantity, value=peak),
        continuous_duration_s=continuous_duration_s,
        peak_duration_s=0.1,
        gain_db=26.0,
        reference_input=ElectricalValue(quantity='voltage_v_rms', value=1.0),
        clipping_reference_definition='fixture evidenced clipping onset',
        valid_frequency_band=FrequencyDomain(minimum_hz=100.0, maximum_hz=10000.0),
        weighting='unweighted',
        channel_count_condition=AmplifierChannelCountCondition(
            simultaneous_channel_count=1,
            shared_supply_evidence=False,
            condition_description='single channel driven',
        ),
    )


def _fixture(tmp_path: Path, *, count: int = 6):
    database = tmp_path / 'cad.sqlite3'
    scene_repository = SceneRepository(database)
    baseline = scene_repository.save(
        SceneDocument(
            document_id=DOCUMENT_ID,
            schema_version=2,
            room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
            entities=(
                SceneEntity(
                    entity_id='speaker-fl',
                    kind='speaker',
                    name='Front Left',
                    speaker_role='FL',
                    position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                    size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
                ),
            ),
        ),
        parent_revision_id=None,
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(scene_repository, variant_repository)
    definition = _equipment()
    _save_equipment(equipment_repository, definition)
    variants = []
    for index in range(count):
        variant = build_system_variant(
            baseline=baseline,
            name=f'Amplifier topology {index}',
            role_bindings=(ChannelRoleBinding(role_id='FL', display_name='Front Left'),),
            proposed_entities=(),
            equipment_bindings=(
                EquipmentBindingRef(
                    entity_id='speaker-fl',
                    equipment_definition_id=definition.definition_id,
                    equipment_definition_version=definition.version,
                    equipment_definition_sha256=definition.semantic_sha256,
                ),
            ),
            created_at_utc=f'2026-09-19T15:{index:02d}:00+00:00',
        )
        variant_repository.save_variant(variant)
        variants.append(variant)

    standards_repository = CadStandardsRepository(scene_repository, variant_repository)
    profile = build_user_standards_profile(
        profile_id='issue-169-amplifier-comparison-profile',
        version='1',
        name='Issue 169 amplifier comparison fixture',
        criteria=(),
    )
    standards_repository.save_profile(profile)

    load = build_speaker_electrical_load_authority(
        load_id='speaker-fl-8ohm-reference',
        version='1',
        equipment_definition=definition,
        semantics='exact_resistive_reference',
        resistance_ohm=8.0,
        valid_frequency_band=FrequencyDomain(minimum_hz=100.0, maximum_hz=10000.0),
        provenance=_provenance('speaker-load', '2'),
    )
    amplifier_repository = CadAmplifierHeadroomRepository(
        scene_repository,
        variant_repository,
        equipment_repository,
    )
    amplifier_repository.save_speaker_load(load)
    return (
        database,
        scene_repository,
        baseline,
        variant_repository,
        equipment_repository,
        standards_repository,
        profile,
        load,
        amplifier_repository,
        tuple(variants),
        definition,
    )


def _evaluation(*, repository, revision, variant, definition, load, amplifier, duration: float = 60.0, quantity: str = 'voltage_v_rms'):
    repository.save_amplifier_capability(amplifier)
    scenario = build_playback_chain_scenario(
        revision=revision,
        variant=variant,
        source_equipment=definition,
        amplifier_capability=amplifier,
        speaker_load=load,
        routing=PlaybackRouting(
            source_entity_id='speaker-fl',
            channel_role_id='FL',
            amplifier_output_id='amp-out-fl',
        ),
        requested_input=ElectricalValue(quantity='voltage_v_rms', value=1.0),
        requested_output_quantity=quantity,
        requested_continuous_output_value=6.0 if quantity == 'voltage_v_rms' else 4.0,
        requested_peak_output_value=12.0 if quantity == 'voltage_v_rms' else 8.0,
        continuous_duration_s=duration,
        peak_duration_s=0.1,
        frequency_band=DirectLevelFrequencyBand(low_hz=100.0, high_hz=10000.0),
        weighting='unweighted',
        target_spl_db_spl=95.0,
        target_reference_condition='single-channel direct target at 1 m',
        acoustic_target_distance_m=1.0,
        target_mode='continuous',
        simultaneous_channel_condition=SimultaneousChannelCondition(output_ids=('amp-out-fl',)),
    )
    repository.save_scenario(scenario)
    evaluation = evaluate_playback_chain(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        amplifier_capability=amplifier,
        speaker_load=load,
        scenario=scenario,
    )
    repository.save_evaluation(evaluation)
    return evaluation


def _bundle(spec, variant, evaluation, objective_ids):
    source = amplifier_headroom_evaluation_ref(evaluation)
    canonical = amplifier_headroom_objective_vector(evaluation)
    vector = ObjectiveVector(
        candidate_id=variant.variant_id,
        metrics=tuple(canonical.metric(objective_id) for objective_id in objective_ids),
    )
    evidence = tuple(
        ObjectiveEvidenceBinding(
            objective_id=objective_id,
            source_authority_kind=source.authority_kind,
            source_authority_id=source.authority_id,
            source_semantic_sha256=source.semantic_sha256,
        )
        for objective_id in objective_ids
    )
    return build_variant_evaluation_bundle(
        spec=spec,
        variant=variant,
        objective_vector=vector,
        objective_evidence=evidence,
        amplifier_headroom_evaluation=source,
    )


def _topology_repository(scene_repository, variant_repository, standards_repository, amplifier_repository):
    return CadTopologyComparisonRepository(
        scene_repository=scene_repository,
        system_variant_repository=variant_repository,
        standards_repository=standards_repository,
        amplifier_headroom_repository=amplifier_repository,
    )


def test_typed_amplifier_objectives_compare_and_reopen_exactly(tmp_path: Path) -> None:
    (
        database, scene_repository, baseline, variant_repository, _,
        standards_repository, profile, load, amplifier_repository, variants, definition,
    ) = _fixture(tmp_path, count=2)
    current, proposed = variants
    current_eval = _evaluation(
        repository=amplifier_repository,
        revision=baseline,
        variant=current,
        definition=definition,
        load=load,
        amplifier=_amplifier('amp-current', '3', 10.0, 20.0),
    )
    proposed_eval = _evaluation(
        repository=amplifier_repository,
        revision=baseline,
        variant=proposed,
        definition=definition,
        load=load,
        amplifier=_amplifier('amp-proposed', '4', 14.0, 28.0),
    )
    current_ref = amplifier_headroom_evaluation_ref(current_eval)
    assert current_ref.authority_version == current_eval.authority_version
    assert current_ref.semantic_sha256 == current_eval.evaluation_sha256
    assert current_ref.model_version == current_eval.scenario.comparison_sha256
    assert current_ref.fidelity is None

    definitions = amplifier_headroom_objective_definitions(current_eval.scenario)
    assert definitions == amplifier_headroom_objective_definitions(proposed_eval.scenario)
    objective_ids = tuple(item.objective_id for item in definitions)
    spec = build_system_topology_comparison_spec(
        name='current vs proposed amplifier exact comparison',
        baseline=baseline,
        candidate_variants=(
            compared_system_variant(current, role='current', comparison_label='current topology'),
            compared_system_variant(proposed, role='proposed', comparison_label='proposed topology'),
        ),
        required_objectives=definitions,
        optional_objectives=(),
        standards_profile=profile,
    )
    repository = _topology_repository(
        scene_repository, variant_repository, standards_repository, amplifier_repository
    )
    repository.save_spec(spec)
    current_bundle = _bundle(spec, current, current_eval, objective_ids)
    proposed_bundle = _bundle(spec, proposed, proposed_eval, objective_ids)
    repository.save_bundle(current_bundle)
    repository.save_bundle(proposed_bundle)
    comparison = evaluate_topology_comparison(
        spec=spec,
        bundles=(current_bundle, proposed_bundle),
        created_at_utc='2026-09-19T15:30:00+00:00',
    )
    repository.save_evaluation(comparison)

    assert all(item.state == 'ELIGIBLE' for item in comparison.eligibility)
    assert comparison.pareto_result is not None
    assert comparison.pareto_result.objective_ids == objective_ids
    assert comparison.pareto_result.non_dominated_candidate_ids == (proposed.variant_id,)
    assert proposed_bundle.evidence_source(
        'o100d.electrical_headroom.continuous'
    ) == amplifier_headroom_evaluation_ref(proposed_eval)
    assert proposed_bundle.objective_vector.metric(
        'o100d.electrical_headroom.continuous'
    ).value == amplifier_headroom_objective_vector(proposed_eval).metric(
        'o100d.electrical_headroom.continuous'
    ).value

    reopened_scene = SceneRepository(database)
    reopened_variants = CadSystemVariantRepository(reopened_scene)
    reopened_equipment = CadEquipmentRepository(reopened_scene, reopened_variants)
    reopened_standards = CadStandardsRepository(reopened_scene, reopened_variants)
    reopened_amplifier = CadAmplifierHeadroomRepository(
        reopened_scene, reopened_variants, reopened_equipment
    )
    reopened = _topology_repository(
        reopened_scene, reopened_variants, reopened_standards, reopened_amplifier
    )
    assert reopened.get_bundle(proposed_bundle.bundle_id) == proposed_bundle
    assert reopened.get_evaluation(comparison.evaluation_id) == comparison


def test_missing_and_incompatible_amplifier_semantics_are_not_coerced(tmp_path: Path) -> None:
    (
        _, scene_repository, baseline, variant_repository, _, standards_repository,
        profile, load, amplifier_repository, variants, definition,
    ) = _fixture(tmp_path, count=5)
    current, optional_missing, required_missing, power_variant, duration_variant = variants
    evaluations = (
        _evaluation(repository=amplifier_repository, revision=baseline, variant=current, definition=definition, load=load, amplifier=_amplifier('amp-a', '3', 10.0, 20.0)),
        _evaluation(repository=amplifier_repository, revision=baseline, variant=optional_missing, definition=definition, load=load, amplifier=_amplifier('amp-b', '4', 12.0, 24.0)),
        _evaluation(repository=amplifier_repository, revision=baseline, variant=required_missing, definition=definition, load=load, amplifier=_amplifier('amp-c', '5', 11.0, 22.0)),
        _evaluation(repository=amplifier_repository, revision=baseline, variant=power_variant, definition=definition, load=load, amplifier=_amplifier('amp-d', '6', 50.0, 100.0, quantity='power_w'), quantity='power_w'),
        _evaluation(repository=amplifier_repository, revision=baseline, variant=duration_variant, definition=definition, load=load, amplifier=_amplifier('amp-e', '7', 13.0, 26.0, continuous_duration_s=120.0), duration=120.0),
    )
    continuous, peak, _ = amplifier_headroom_objective_definitions(evaluations[0].scenario)
    spec = build_system_topology_comparison_spec(
        name='amplifier missing and semantic compatibility policy',
        baseline=baseline,
        candidate_variants=tuple(
            compared_system_variant(
                variant,
                role='current' if index == 0 else 'proposed',
                comparison_label=f'candidate {index}',
            )
            for index, variant in enumerate(variants)
        ),
        required_objectives=(continuous,),
        optional_objectives=(peak,),
        standards_profile=profile,
    )
    repository = _topology_repository(
        scene_repository, variant_repository, standards_repository, amplifier_repository
    )
    repository.save_spec(spec)
    bundles = (
        _bundle(spec, current, evaluations[0], (continuous.objective_id, peak.objective_id)),
        _bundle(spec, optional_missing, evaluations[1], (continuous.objective_id,)),
        _bundle(spec, required_missing, evaluations[2], (peak.objective_id,)),
        _bundle(spec, power_variant, evaluations[3], (continuous.objective_id,)),
        _bundle(spec, duration_variant, evaluations[4], (continuous.objective_id,)),
    )
    for bundle in bundles:
        repository.save_bundle(bundle)
    comparison = evaluate_topology_comparison(
        spec=spec,
        bundles=bundles,
        created_at_utc='2026-09-19T15:40:00+00:00',
    )
    states = {item.variant_id: item for item in comparison.eligibility}
    assert states[current.variant_id].state == 'ELIGIBLE'
    assert states[optional_missing.variant_id].state == 'ELIGIBLE'
    assert {issue.code for issue in states[required_missing.variant_id].issues} == {
        'required_objective_missing'
    }
    assert 'incompatible_unit' in {
        issue.code for issue in states[power_variant.variant_id].issues
    }
    assert 'incompatible_comparison_model_version' in {
        issue.code for issue in states[duration_variant.variant_id].issues
    }
    assert comparison.pareto_objective_ids == (continuous.objective_id,)
    assert all(
        metric.value is not None
        for bundle in bundles
        for metric in bundle.objective_vector.metrics
        if metric.state == 'available'
    )
    assert evaluations[0].scenario.comparison_sha256 != evaluations[4].scenario.comparison_sha256


def test_typed_resolver_rejects_stale_variant_and_baseline_authority(tmp_path: Path) -> None:
    (
        _, scene_repository, baseline, variant_repository, _, standards_repository,
        profile, load, amplifier_repository, variants, definition,
    ) = _fixture(tmp_path, count=2)
    current, proposed = variants
    evaluation = _evaluation(
        repository=amplifier_repository,
        revision=baseline,
        variant=current,
        definition=definition,
        load=load,
        amplifier=_amplifier('amp-resolver', '8', 10.0, 20.0),
    )
    continuous = amplifier_headroom_objective_definitions(evaluation.scenario)[0]
    spec = build_system_topology_comparison_spec(
        name='typed resolver rejection fixture',
        baseline=baseline,
        candidate_variants=(
            compared_system_variant(current, role='current', comparison_label='current'),
            compared_system_variant(proposed, role='proposed', comparison_label='proposed'),
        ),
        required_objectives=(continuous,),
        optional_objectives=(),
        standards_profile=profile,
    )
    repository = _topology_repository(
        scene_repository, variant_repository, standards_repository, amplifier_repository
    )
    repository.save_spec(spec)

    exact_ref = amplifier_headroom_evaluation_ref(evaluation)
    stale_ref = exact_ref.model_copy(update={'semantic_sha256': '0' * 64})
    metric = amplifier_headroom_objective_vector(evaluation).metric(continuous.objective_id)
    stale_bundle = build_variant_evaluation_bundle(
        spec=spec,
        variant=current,
        objective_vector=ObjectiveVector(candidate_id=current.variant_id, metrics=(metric,)),
        objective_evidence=(
            ObjectiveEvidenceBinding(
                objective_id=continuous.objective_id,
                source_authority_kind=stale_ref.authority_kind,
                source_authority_id=stale_ref.authority_id,
                source_semantic_sha256=stale_ref.semantic_sha256,
            ),
        ),
        amplifier_headroom_evaluation=stale_ref,
    )
    with pytest.raises(ValueError, match='exact hash mismatch'):
        repository.save_bundle(stale_bundle)

    wrong_variant_bundle = build_variant_evaluation_bundle(
        spec=spec,
        variant=proposed,
        objective_vector=ObjectiveVector(candidate_id=proposed.variant_id, metrics=(metric,)),
        objective_evidence=(
            ObjectiveEvidenceBinding(
                objective_id=continuous.objective_id,
                source_authority_kind=exact_ref.authority_kind,
                source_authority_id=exact_ref.authority_id,
                source_semantic_sha256=exact_ref.semantic_sha256,
            ),
        ),
        amplifier_headroom_evaluation=exact_ref,
    )
    with pytest.raises(ValueError, match='SystemVariant authority mismatch'):
        repository.save_bundle(wrong_variant_bundle)

    with pytest.raises(ValueError, match='baseline authority mismatch'):
        amplifier_repository.resolve_evaluation_exact(
            evaluation.evaluation_id,
            evaluation_sha256=evaluation.evaluation_sha256,
            document_id=baseline.document_id,
            scene_revision_id='wrong-scene-revision',
            scene_content_hash='0' * 64,
            variant_id=current.variant_id,
            variant_sha256=current.variant_sha256,
        )
