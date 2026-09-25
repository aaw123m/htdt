from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_equipment_binding_repository import (
    CadEquipmentBindingRepository,
)
from htdt.cad_equipment_binding import build_equipment_binding_semantics
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_installation_cost import build_cost_record, build_cost_scenario
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
)
from htdt.cad_standards import build_user_standards_profile
from htdt.cad_standards_repository import CadStandardsRepository
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.cad_topology_comparison_execution import (
    ComparisonEvaluationPolicy,
    CoverageLanePolicy,
    DirectLevelLanePolicy,
    execute_topology_comparison,
    installation_cost_resolver,
)
from htdt.cad_amplifier_headroom_repository import (
    CadAmplifierHeadroomRepository,
)
from htdt.cad_coverage_repository import CadCoverageRepository
from htdt.cad_direct_level import (
    DirectLevelFrequencyBand,
    ReferenceInputCondition,
)
from htdt.cad_direct_level_repository import CadDirectLevelRepository
from htdt.cad_directivity_repository import CadDirectivityRepository
from htdt.cad_installation_cost_repository import (
    CadInstallationCostRepository,
)
from htdt.cad_topology_comparison_repository import (
    CadTopologyComparisonRepository,
)
from htdt.system_expansion_workflow import SystemExpansionWorkflowService

from test_cad_coverage import (  # noqa: E402
    _authority,
    _persist_authority,
    _seat,
    _speaker,
)


NOW = '2026-09-25T12:00:00+00:00'


def _full_authority():
    """Fixture authority carrying directivity AND SPL/sensitivity evidence.

    Rebuilds the coverage fixture definition with the SPL capability the
    direct-level evaluator needs, then re-parses the dataset against the
    enriched definition.
    """
    from htdt.cad_equipment import (
        FrequencyDomain,
        SensitivityReference,
        SplCapability,
        build_equipment_definition,
    )
    from htdt.cad_directivity import NORMALIZED_JSON_DIRECTIVITY_ADAPTER
    from htdt.cad_scene import Offset3, Size3

    source_bytes, sparse, _dataset = _authority()
    provenance = sparse.provenance[0]
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=10000.0)
    definition = build_equipment_definition(
        definition_id=sparse.definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=sparse.user_label,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(x_m=0.20),
        directivity=sparse.directivity,
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
    )
    dataset = NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(
        source_bytes, definition
    )
    return source_bytes, definition, dataset
DOCUMENT_ID = 'o100d-execution-fixture'


def _speaker_entity(entity_id: str, role: str, x_m: float) -> SceneEntity:
    entity = _speaker(
        aim=None,
        yaw_deg=0.0,
    )
    return entity.model_copy(
        update={
            'entity_id': entity_id,
            'name': entity_id,
            'speaker_role': role,
            'position': Position3(x_m=x_m, y_m=0.0, z_m=1.0),
        }
    )


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5),
        entities=(
            _speaker(),
            _seat('seat-on', x_m=0.20, y_m=2.0),
            _seat('seat-off', x_m=2.20, y_m=2.0),
        ),
    )
    revision = scene_repository.save(
        document, parent_revision_id=None
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository, variant_repository
    )
    directivity_repository = CadDirectivityRepository(
        scene_repository, equipment_repository
    )
    binding_repository = CadEquipmentBindingRepository(
        scene_repository, equipment_repository
    )
    standards_repository = CadStandardsRepository(
        scene_repository, variant_repository
    )
    coverage_repository = CadCoverageRepository(
        scene_repository,
        variant_repository,
        equipment_repository,
        directivity_repository,
    )
    direct_level_repository = CadDirectLevelRepository(
        scene_repository, variant_repository, equipment_repository
    )
    amplifier_repository = CadAmplifierHeadroomRepository(
        scene_repository, variant_repository, equipment_repository
    )
    cost_repository = CadInstallationCostRepository(scene_repository)
    comparison_repository = CadTopologyComparisonRepository(
        scene_repository=scene_repository,
        system_variant_repository=variant_repository,
        standards_repository=standards_repository,
        coverage_repository=coverage_repository,
        direct_level_repository=direct_level_repository,
        amplifier_headroom_repository=amplifier_repository,
        external_resolvers={
            'installation_cost_evaluation': installation_cost_resolver(
                cost_repository
            ),
        },
    )
    return {
        'scene_repository': scene_repository,
        'revision': revision,
        'variant_repository': variant_repository,
        'equipment_repository': equipment_repository,
        'directivity_repository': directivity_repository,
        'binding_repository': binding_repository,
        'standards_repository': standards_repository,
        'coverage_repository': coverage_repository,
        'direct_level_repository': direct_level_repository,
        'amplifier_repository': amplifier_repository,
        'cost_repository': cost_repository,
        'comparison_repository': comparison_repository,
    }


def _binding(definition) -> EquipmentBindingRef:
    return EquipmentBindingRef(
        entity_id='speaker-fl',
        equipment_definition_id=definition.definition_id,
        equipment_definition_version=definition.version,
        equipment_definition_sha256=definition.semantic_sha256,
    )


def _proposal(fx, definition, *, name='提案A'):
    """Proposal: rebind speaker-fl to the fixture definition."""
    variant = build_system_variant(
        baseline=fx['revision'],
        name=name,
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
            ChannelRoleBinding(role_id='SL', display_name='Surround Left'),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id=f'{name}-sl',
                entity=_speaker_entity('speaker-sl', 'SL', -1.5),
                role_binding_id='SL',
            ),
        ),
        equipment_bindings=(_binding(definition),),
        created_at_utc=NOW,
    )
    fx['variant_repository'].save_variant(variant)
    return variant


def _policy(cost_scenario=None) -> ComparisonEvaluationPolicy:
    return ComparisonEvaluationPolicy(
        coverage=CoverageLanePolicy(
            source_entity_id='speaker-fl',
            channel_role_id='FL',
            seat_entity_ids=('seat-on', 'seat-off'),
            evaluation_frequencies_hz=(500.0, 1000.0),
            coverage_threshold_db=-6.0,
        ),
        direct_level=DirectLevelLanePolicy(
            source_entity_id='speaker-fl',
            channel_role_id='FL',
            seat_entity_ids=('seat-on', 'seat-off'),
            reference_input=ReferenceInputCondition(
                input_quantity='voltage_v_rms',
                input_value=2.83,
            ),
            target_spl_db_spl=75.0,
            frequency_band=DirectLevelFrequencyBand(
                low_hz=500.0, high_hz=1000.0
            ),
            target_reference_condition='unit test target',
        ),
        cost_scenario=cost_scenario,
    )


def _profile():
    return build_user_standards_profile(
        profile_id='o100d-test-profile',
        version='1',
        name='O100D test profile',
        criteria=(),
    )


def _bundle_for(fx, comparison_id, variant_id):
    return next(
        (
            bundle
            for bundle in fx['comparison_repository'].list_bundles(
                comparison_id
            )
            if bundle.variant_id == variant_id
        ),
        None,
    )


def _run(fx, candidates, **kwargs):
    return execute_topology_comparison(
        scene_repository=fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        comparison_repository=fx['comparison_repository'],
        standards_repository=fx['standards_repository'],
        equipment_repository=fx['equipment_repository'],
        baseline=fx['revision'],
        name='O100D test comparison',
        standards_profile=_profile(),
        candidates=candidates,
        policy=kwargs.pop('policy', _policy()),
        include_current=kwargs.pop('include_current', True),
        coverage_repository=fx['coverage_repository'],
        direct_level_repository=fx['direct_level_repository'],
        amplifier_headroom_repository=fx['amplifier_repository'],
        directivity_repository=fx['directivity_repository'],
        cost_repository=fx['cost_repository'],
        equipment_binding_repository=fx['binding_repository'],
        created_at_utc=NOW,
        **kwargs,
    )


def test_execute_comparison_persists_bundle_and_evaluation(tmp_path):
    fx = _fixture(tmp_path)
    source_bytes, definition, dataset = _full_authority()
    _persist_authority(
        fx['equipment_repository'],
        fx['directivity_repository'],
        definition,
        dataset,
        source_bytes,
    )
    proposal = _proposal(fx, definition)

    result = _run(fx, [proposal])

    spec = fx['comparison_repository'].get_spec(result.spec.comparison_id)
    assert spec is not None
    assert spec.semantic_sha256 == result.spec.semantic_sha256

    bundle = _bundle_for(
        fx, result.spec.comparison_id, proposal.variant_id
    )
    assert bundle is not None
    metric_ids = {metric.objective_id for metric in bundle.objective_vector.metrics}
    evidence_ids = {
        binding.objective_id for binding in bundle.objective_evidence
    }
    assert metric_ids == evidence_ids
    assert any(
        metric.objective_id.startswith('o100d.coverage.')
        for metric in bundle.objective_vector.metrics
    )
    assert any(
        metric.objective_id.startswith('o100d.direct_level.')
        for metric in bundle.objective_vector.metrics
    )

    evaluation = fx['comparison_repository'].get_evaluation(
        result.evaluation.evaluation_id
    )
    assert evaluation is not None
    states = {
        item.variant_id: item.state
        for item in evaluation.eligibility
    }
    assert states[proposal.variant_id] == 'ELIGIBLE'


def test_reopen_replays_exact_authorities(tmp_path):
    fx = _fixture(tmp_path)
    source_bytes, definition, dataset = _full_authority()
    _persist_authority(
        fx['equipment_repository'],
        fx['directivity_repository'],
        definition,
        dataset,
        source_bytes,
    )
    proposal = _proposal(fx, definition)

    first = _run(fx, [proposal])
    reopened = SystemExpansionWorkflowService(
        fx['scene_repository'], DOCUMENT_ID
    )
    view = reopened.comparison()
    assert view.variants
    assert not view.authority_stale

    # Re-running after an authority change (a second proposal enters the
    # comparison) produces a new canonical spec and new persisted evidence.
    proposal_b = _proposal(fx, definition, name='提案B')
    second = _run(fx, [proposal, proposal_b])
    assert second.spec.comparison_id != first.spec.comparison_id
    assert (
        len(
            fx['comparison_repository'].list_bundles(
                second.spec.comparison_id
            )
        )
        >= 2
    )
    reopened2 = SystemExpansionWorkflowService(
        fx['scene_repository'], DOCUMENT_ID
    )
    latest = reopened2.comparison()
    assert latest.name == 'O100D test comparison'
    assert latest.variants


def test_candidate_without_binding_is_ineligible(tmp_path):
    fx = _fixture(tmp_path)
    source_bytes, definition, dataset = _full_authority()
    _persist_authority(
        fx['equipment_repository'],
        fx['directivity_repository'],
        definition,
        dataset,
        source_bytes,
    )
    bound = _proposal(fx, definition, name='提案A')
    unbound = build_system_variant(
        baseline=fx['revision'],
        name='提案B',
        role_bindings=(
            ChannelRoleBinding(role_id='SL', display_name='Surround Left'),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='pB-sl',
                entity=_speaker_entity('speaker-sl', 'SL', -1.5),
                role_binding_id='SL',
            ),
        ),
        equipment_bindings=(),
        created_at_utc=NOW,
    )
    fx['variant_repository'].save_variant(unbound)

    result = _run(fx, [bound, unbound])
    bundle_b = _bundle_for(
        fx, result.spec.comparison_id, unbound.variant_id
    )
    eligibility = {
        item.variant_id: item for item in result.evaluation.eligibility
    }
    assert eligibility[unbound.variant_id].state == 'INELIGIBLE'
    assert any(
        issue.code
        in (
            'variant_bundle_missing',
            'required_objective_missing',
            'required_objective_unsupported',
        )
        for issue in eligibility[unbound.variant_id].issues
    )
    # An unbound candidate never fabricates a metric; if a bundle exists
    # it must not contain coverage/direct-level axes.
    if bundle_b is not None:
        assert not {
            metric.objective_id
            for metric in bundle_b.metrics
            if metric.objective_id.startswith(('o100d.coverage.', 'o100d.direct_level.'))
        }


def test_cost_lane_persists_evaluation_and_metrics(tmp_path):
    fx = _fixture(tmp_path)
    source_bytes, definition, dataset = _full_authority()
    _persist_authority(
        fx['equipment_repository'],
        fx['directivity_repository'],
        definition,
        dataset,
        source_bytes,
    )
    proposal = _proposal(fx, definition)
    cost_repository = fx['cost_repository']
    record = build_cost_record(
        category='equipment_purchase',
        amount=1500.0,
        currency='JPY',
        source_kind='user_entered',
        equipment_definition_id=definition.definition_id,
        equipment_definition_version=definition.version,
        equipment_definition_sha256=definition.semantic_sha256,
        recorded_at_utc=NOW,
    )
    cost_repository.save_record(record, document_id=DOCUMENT_ID)
    scenario = build_cost_scenario(
        document_id=DOCUMENT_ID,
        name='初期導入',
        currency='JPY',
        hours_per_added_entity=2.0,
        labor_rate_per_hour=8000.0,
    )

    result = _run(
        fx,
        [proposal],
        policy=_policy(cost_scenario=scenario),
    )
    persisted = cost_repository.list_evaluations(DOCUMENT_ID)
    assert {
        evaluation.variant_id for evaluation in persisted
    } == {candidate.variant.variant_id for candidate in result.candidates}
    assert any(
        evaluation.variant_id == proposal.variant_id
        for evaluation in persisted
    )
    bundle = _bundle_for(
        fx, result.spec.comparison_id, proposal.variant_id
    )
    cost_metric_ids = {
        metric.objective_id
        for metric in bundle.objective_vector.metrics
        if metric.objective_id.startswith('o100c.')
    }
    assert cost_metric_ids
    installation_refs = [
        ref
        for ref in bundle.all_evaluation_refs()
        if ref.authority_kind == 'installation_cost_evaluation'
    ]
    assert installation_refs


def test_include_current_materializes_projection(tmp_path):
    fx = _fixture(tmp_path)
    source_bytes, definition, dataset = _full_authority()
    _persist_authority(
        fx['equipment_repository'],
        fx['directivity_repository'],
        definition,
        dataset,
        source_bytes,
    )
    fx['binding_repository'].save_binding(
        build_equipment_binding_semantics(
            binding_id='binding-fl',
            document_id=DOCUMENT_ID,
            entity_id='speaker-fl',
            equipment_definition=definition,
            body_geometry_authority='equipment_nominal',
            acoustic_reference_authority='equipment_derived',
            provenance=definition.provenance,
            created_at_utc=NOW,
        )
    )
    proposal = _proposal(fx, definition)

    result = _run(fx, [proposal], include_current=True)
    roles = {
        candidate.role for candidate in result.spec.candidate_variants
    }
    assert roles == {'current', 'proposed'}
    assert len(result.candidates) == 2


def test_workflow_evaluate_proposals_end_to_end(tmp_path):
    fx = _fixture(tmp_path)
    source_bytes, definition, dataset = _full_authority()
    _persist_authority(
        fx['equipment_repository'],
        fx['directivity_repository'],
        definition,
        dataset,
        source_bytes,
    )
    proposal = _proposal(fx, definition)

    service = SystemExpansionWorkflowService(
        fx['scene_repository'], DOCUMENT_ID
    )
    execution = service.evaluate_proposals(
        [proposal.variant_id], include_current=True
    )
    assert execution.spec.comparison_id

    reopened = SystemExpansionWorkflowService(
        fx['scene_repository'], DOCUMENT_ID
    )
    view = reopened.comparison()
    assert view.variants


def test_execution_fail_closed_for_foreign_baseline(tmp_path):
    fx = _fixture(tmp_path)
    source_bytes, definition, dataset = _full_authority()
    _persist_authority(
        fx['equipment_repository'],
        fx['directivity_repository'],
        definition,
        dataset,
        source_bytes,
    )
    proposal = _proposal(fx, definition)

    other_document = SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=7.0, depth_m=6.0, height_m=2.5),
        entities=(
            _speaker(),
            _seat('seat-on', x_m=0.20, y_m=2.0),
        ),
    )
    other_revision = fx['scene_repository'].save(
        other_document, parent_revision_id=fx['revision'].revision_id
    ).revision

    with pytest.raises(ValueError):
        execute_topology_comparison(
            scene_repository=fx['scene_repository'],
            variant_repository=fx['variant_repository'],
            comparison_repository=fx['comparison_repository'],
            standards_repository=fx['standards_repository'],
            equipment_repository=fx['equipment_repository'],
            baseline=other_revision,
            name='mismatch',
            standards_profile=_profile(),
            candidates=[proposal],
            policy=_policy(),
            coverage_repository=fx['coverage_repository'],
            direct_level_repository=fx['direct_level_repository'],
            amplifier_headroom_repository=fx['amplifier_repository'],
            directivity_repository=fx['directivity_repository'],
            cost_repository=fx['cost_repository'],
            created_at_utc=NOW,
        )

