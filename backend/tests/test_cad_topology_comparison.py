from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_standards import (
    CriterionDefinition,
    CriterionObservation,
    CriterionRule,
    CriterionSource,
    StandardsEvaluationTarget,
    build_user_standards_profile,
    evaluate_standards_profile,
)
from htdt.cad_standards_evidence import build_standards_observation_authority
from htdt.cad_standards_repository import CadStandardsRepository
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.cad_topology_comparison import (
    ExactAuthorityRef,
    ObjectiveEvidenceBinding,
    build_system_topology_comparison_spec,
    build_topology_comparison_selection,
    build_variant_evaluation_bundle,
    compared_system_variant,
    evaluate_topology_comparison,
    standards_evaluation_ref,
)
from htdt.cad_topology_comparison_repository import (
    CadTopologyComparisonRepository,
)
from htdt.optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveValidDomain,
    ObjectiveVector,
)


NOW = '2026-09-19T14:00:00+00:00'
DOCUMENT_ID = 'o100d-topology-comparison-fixture'


def _hash(value: str) -> str:
    return sha256(value.encode('utf-8')).hexdigest()


def _speaker(
    entity_id: str,
    role: str,
    x_m: float,
    y_m: float,
    z_m: float,
) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=role,
        speaker_role=role,
        position=Position3(x_m=x_m, y_m=y_m, z_m=z_m),
        size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
    )


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            _speaker('fl', 'FL', 1.2, 0.8, 1.0),
            _speaker('c', 'C', 3.0, 0.6, 0.9),
            _speaker('fr', 'FR', 4.8, 0.8, 1.0),
            _speaker('tfl', 'TFL', 1.8, 2.0, 2.2),
            _speaker('tfr', 'TFR', 4.2, 2.0, 2.2),
            SceneEntity(
                entity_id='mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.2, z_m=1.1),
            ),
        ),
    )


def _roles(*, surround: bool) -> tuple[ChannelRoleBinding, ...]:
    values = [
        ChannelRoleBinding(role_id='FL', display_name='FL'),
        ChannelRoleBinding(role_id='C', display_name='C'),
        ChannelRoleBinding(role_id='FR', display_name='FR'),
        ChannelRoleBinding(role_id='TFL', display_name='TFL'),
        ChannelRoleBinding(role_id='TFR', display_name='TFR'),
    ]
    if surround:
        values.extend(
            (
                ChannelRoleBinding(role_id='SL', display_name='SL'),
                ChannelRoleBinding(role_id='SR', display_name='SR'),
            )
        )
    return tuple(values)


def _surround_proposals() -> tuple[ProposedEntitySpec, ...]:
    return (
        ProposedEntitySpec(
            spec_id='proposal-sl',
            entity=_speaker('sl', 'SL', 0.8, 2.9, 1.3),
            role_binding_id='SL',
        ),
        ProposedEntitySpec(
            spec_id='proposal-sr',
            entity=_speaker('sr', 'SR', 5.2, 2.9, 1.3),
            role_binding_id='SR',
        ),
    )


def _definition(
    objective_id: str,
    *,
    quantity: str,
    unit: str,
    direction: str,
    model_id: str,
    model_version: str = '1',
    bounded: tuple[float, float] | None = None,
) -> ObjectiveDefinition:
    valid_domain = (
        ObjectiveValidDomain(
            kind='bounded_real',
            minimum=bounded[0],
            maximum=bounded[1],
        )
        if bounded is not None
        else ObjectiveValidDomain(kind='finite_real')
    )
    return ObjectiveDefinition(
        objective_id=objective_id,
        quantity=quantity,
        unit=unit,
        direction=direction,
        valid_domain=valid_domain,
        comparison_model_id=model_id,
        comparison_model_version=model_version,
    )


def _metric(
    definition: ObjectiveDefinition,
    value: float,
) -> ObjectiveMetric:
    return ObjectiveMetric(
        objective_id=definition.objective_id,
        value=value,
        unit=definition.unit,
        direction=definition.direction,
        state='available',
        definition=definition,
    )


def _external_ref(
    kind: str,
    variant_id: str,
    *,
    model_id: str,
) -> ExactAuthorityRef:
    return ExactAuthorityRef(
        authority_kind=kind,
        authority_id=f'{kind}:{variant_id}',
        authority_version='fixture-1',
        semantic_sha256=_hash(f'{kind}:{variant_id}:fixture-1'),
        evaluator_id=f'{kind}-fixture-evaluator',
        evaluator_version='1',
        model_id=model_id,
        model_version='1',
        fidelity='deterministic-fixture',
    )


def _standards_evaluation(
    *,
    standards_repository: CadStandardsRepository,
    profile,
    baseline,
    variant,
    timestamp: str,
    observed_value: float,
):
    authority = build_standards_observation_authority(
        document_id=baseline.document_id,
        scene_revision_id=baseline.revision_id,
        scene_content_hash=baseline.content_hash,
        system_variant_id=variant.variant_id,
        system_variant_sha256=variant.variant_sha256,
        quantity='clearance',
        unit='m',
        observed_value=observed_value,
        evidence_basis='predicted',
        observed_at_utc=timestamp,
    )
    standards_repository.save_observation_authority(authority)
    evaluation = evaluate_standards_profile(
        profile=profile,
        target=StandardsEvaluationTarget(
            document_id=baseline.document_id,
            scene_revision_id=baseline.revision_id,
            scene_content_hash=baseline.content_hash,
            system_variant_id=variant.variant_id,
            system_variant_sha256=variant.variant_sha256,
            entity_ids=(),
            applicable_domains=('topology-comparison-fixture',),
        ),
        observations=(
            CriterionObservation(
                criterion_id='fixture-clearance',
                observed_value=observed_value,
                unit='m',
                evidence_basis='predicted',
                evidence_refs=(authority.ref(),),
            ),
        ),
        created_at_utc=timestamp,
    )
    standards_repository.save_evaluation(evaluation)
    return evaluation


def test_named_topology_comparison_exact_authority_pareto_and_reopen(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = scene_repository.save(
        _scene(),
        parent_revision_id=None,
    ).revision
    baseline_before = scene_repository.latest(DOCUMENT_ID)

    variant_repository = CadSystemVariantRepository(scene_repository)
    variant_specs = (
        ('Current 3.0.2 authority', False),
        ('Proposed 5.0.2 A authority', True),
        ('Proposed 5.0.2 B authority', True),
        ('Proposed missing required', True),
        ('Proposed incompatible unit', True),
        ('Proposed incompatible model', True),
        ('Proposed unsupported required', True),
    )
    variants = tuple(
        build_system_variant(
            baseline=baseline,
            name=name,
            role_bindings=_roles(surround=surround),
            proposed_entities=(
                _surround_proposals()
                if surround
                else ()
            ),
            created_at_utc=f'2026-09-19T14:0{index}:00+00:00',
        )
        for index, (name, surround) in enumerate(variant_specs)
    )
    for variant in variants:
        variant_repository.save_variant(variant)

    standards_repository = CadStandardsRepository(
        scene_repository,
        variant_repository,
    )
    profile = build_user_standards_profile(
        profile_id='topology-comparison-fixture-profile',
        version='1',
        name='Topology comparison fixture profile',
        criteria=(
            CriterionDefinition(
                criterion_id='fixture-clearance',
                name='Fixture clearance',
                source=CriterionSource(
                    publisher='HTDT fixture',
                    document_title='Topology comparison fixture',
                    document_version='1',
                    reference='fixture-clearance',
                ),
                quantity='clearance',
                unit='m',
                applicable_domains=('topology-comparison-fixture',),
                rule=CriterionRule(
                    operator='min',
                    minimum=0.5,
                ),
            ),
        ),
    )
    standards_repository.save_profile(profile)
    standards_evaluations = {
        variant.variant_id: _standards_evaluation(
            standards_repository=standards_repository,
            profile=profile,
            baseline=baseline,
            variant=variant,
            timestamp=f'2026-09-19T14:1{index}:00+00:00',
            observed_value=0.25 if index == 0 else 0.75,
        )
        for index, variant in enumerate(variants)
    }

    coverage = _definition(
        'o100d.coverage.useful_fraction',
        quantity='useful_coverage_fraction',
        unit='ratio',
        direction='maximize',
        model_id='fixture-coverage-model',
        bounded=(0.0, 1.0),
    )
    loss = _definition(
        'o100d.directivity.worst_seat_off_axis_loss_db',
        quantity='worst_seat_off_axis_loss',
        unit='dB',
        direction='minimize',
        model_id='fixture-coverage-model',
    )
    headroom = _definition(
        'o100d.continuous_headroom.worst_seat_db',
        quantity='continuous_headroom_margin',
        unit='dB',
        direction='maximize',
        model_id='fixture-direct-level-model',
    )
    spl = _definition(
        'o100d.direct_level.worst_seat_db_spl',
        quantity='worst_seat_spl',
        unit='dB SPL',
        direction='maximize',
        model_id='fixture-direct-level-model',
    )

    candidate_refs = (
        compared_system_variant(
            variants[0],
            role='current',
            comparison_label='3.0.2 current',
        ),
        *(
            compared_system_variant(
                variant,
                role='proposed',
                comparison_label=label,
            )
            for variant, label in zip(
                variants[1:],
                (
                    '5.0.2 proposed A',
                    '5.0.2 proposed B',
                    '5.0.2 missing-required fixture',
                    '5.0.2 incompatible-unit fixture',
                    '5.0.2 incompatible-model fixture',
                    '5.0.2 unsupported-required fixture',
                ),
                strict=True,
            )
        ),
    )
    spec = build_system_topology_comparison_spec(
        name='3.0.2 current vs 5.0.2 proposals',
        baseline=baseline,
        candidate_variants=candidate_refs,
        required_objectives=(coverage, loss, headroom),
        optional_objectives=(spl,),
        standards_profile=profile,
    )
    rebuilt_spec = build_system_topology_comparison_spec(
        name='3.0.2 current vs 5.0.2 proposals',
        baseline=baseline,
        candidate_variants=candidate_refs,
        required_objectives=(coverage, loss, headroom),
        optional_objectives=(spl,),
        standards_profile=profile,
    )
    assert rebuilt_spec.comparison_id == spec.comparison_id
    assert rebuilt_spec.semantic_sha256 == spec.semantic_sha256
    assert [item.comparison_label for item in spec.candidate_variants[:3]] == [
        '3.0.2 current',
        '5.0.2 proposed A',
        '5.0.2 proposed B',
    ]
    assert spec.candidate_variants[0].layout_profile_ref is None

    coverage_refs = {
        variant.variant_id: _external_ref(
            'coverage_evaluation',
            variant.variant_id,
            model_id='fixture-coverage-model',
        )
        for variant in variants
    }
    direct_refs = {
        variant.variant_id: _external_ref(
            'direct_level_evaluation',
            variant.variant_id,
            model_id='fixture-direct-level-model',
        )
        for variant in variants
    }
    external_authorities = {
        ref.authority_id: ref
        for ref in (*coverage_refs.values(), *direct_refs.values())
    }

    def resolve_external(authority_id: str) -> ExactAuthorityRef | None:
        return external_authorities.get(authority_id)

    repository = CadTopologyComparisonRepository(
        scene_repository=scene_repository,
        system_variant_repository=variant_repository,
        standards_repository=standards_repository,
        external_resolvers={
            'coverage_evaluation': resolve_external,
            'direct_level_evaluation': resolve_external,
        },
    )
    repository.save_spec(spec)

    bad_unit_headroom = headroom.model_copy(
        update={'unit': 'dBV'}
    )
    bad_model_loss = loss.model_copy(
        update={'comparison_model_version': '2'}
    )

    values = (
        (0.70, 4.0, 3.0, 90.0),
        (0.90, 2.0, 2.0, 92.0),
        (0.85, 3.0, 5.0, None),
        (0.88, 2.5, None, 91.0),
        (0.82, 3.5, 4.0, 91.0),
        (0.86, 2.8, 4.5, 91.5),
        (0.84, 3.1, 'unsupported', 90.5),
    )

    bundles = []
    for index, (variant, value_set) in enumerate(zip(variants, values, strict=True)):
        coverage_value, loss_value, headroom_value, spl_value = value_set
        metrics = [
            _metric(coverage, coverage_value),
            _metric(
                bad_model_loss if index == 5 else loss,
                loss_value,
            ),
        ]
        if headroom_value == 'unsupported':
            metrics.append(
                ObjectiveMetric(
                    objective_id=headroom.objective_id,
                    value=None,
                    unit=headroom.unit,
                    direction=headroom.direction,
                    state='unsupported',
                    definition=headroom,
                )
            )
        elif headroom_value is not None:
            metrics.append(
                _metric(
                    bad_unit_headroom if index == 4 else headroom,
                    headroom_value,
                )
            )
        if spl_value is not None:
            metrics.append(_metric(spl, spl_value))
        vector = ObjectiveVector(
            candidate_id=variant.variant_id,
            metrics=tuple(metrics),
        )

        coverage_ref = coverage_refs[variant.variant_id]
        direct_ref = direct_refs[variant.variant_id]
        evidence = []
        for metric in metrics:
            source = (
                coverage_ref
                if metric.objective_id in {
                    coverage.objective_id,
                    loss.objective_id,
                }
                else direct_ref
            )
            evidence.append(
                ObjectiveEvidenceBinding(
                    objective_id=metric.objective_id,
                    source_authority_kind=source.authority_kind,
                    source_authority_id=source.authority_id,
                    source_semantic_sha256=source.semantic_sha256,
                )
            )
        bundle = build_variant_evaluation_bundle(
            spec=spec,
            variant=variant,
            objective_vector=vector,
            objective_evidence=evidence,
            coverage_evaluation=coverage_ref,
            direct_level_evaluation=direct_ref,
            standards_evaluation=standards_evaluation_ref(
                standards_evaluations[variant.variant_id]
            ),
        )
        repository.save_bundle(bundle)
        bundles.append(bundle)

    evaluation = evaluate_topology_comparison(
        spec=spec,
        bundles=tuple(bundles),
        created_at_utc='2026-09-19T14:30:00+00:00',
    )
    repository.save_evaluation(evaluation)

    state = {
        item.variant_id: item
        for item in evaluation.eligibility
    }
    assert state[variants[0].variant_id].state == 'ELIGIBLE'
    assert state[variants[1].variant_id].state == 'ELIGIBLE'
    assert state[variants[2].variant_id].state == 'ELIGIBLE'
    assert state[variants[3].variant_id].state == 'INELIGIBLE'
    assert state[variants[4].variant_id].state == 'INELIGIBLE'
    assert state[variants[5].variant_id].state == 'INELIGIBLE'
    assert state[variants[6].variant_id].state == 'INELIGIBLE'

    assert {
        issue.code for issue in state[variants[3].variant_id].issues
    } == {'required_objective_missing'}
    assert 'incompatible_unit' in {
        issue.code for issue in state[variants[4].variant_id].issues
    }
    assert 'incompatible_comparison_model_version' in {
        issue.code for issue in state[variants[5].variant_id].issues
    }
    assert {
        issue.code for issue in state[variants[6].variant_id].issues
    } == {'required_objective_unsupported'}

    # Optional SPL is absent for proposed B but does not make it ineligible and
    # is not silently substituted; it is simply not a common Pareto axis.
    assert spl.objective_id not in evaluation.pareto_objective_ids
    assert evaluation.pareto_objective_ids == (
        coverage.objective_id,
        loss.objective_id,
        headroom.objective_id,
    )
    assert evaluation.pareto_result is not None
    assert set(evaluation.pareto_result.non_dominated_candidate_ids) == {
        variants[1].variant_id,
        variants[2].variant_id,
    }
    assert evaluation.pareto_result.dominated_by[variants[0].variant_id] == (
        variants[2].variant_id,
    )

    # Standards remain criterion-level evidence and are not converted to an
    # objective score by the comparison layer.
    assert all(
        not objective_id.startswith('standards.')
        for objective_id in evaluation.pareto_objective_ids
    )
    assert standards_evaluations[variants[0].variant_id].results[0].status == 'FAIL'
    assert state[variants[0].variant_id].state == 'ELIGIBLE'
    assert all(
        evaluation.results[0].status in {'PASS', 'FAIL', 'UNKNOWN'}
        for evaluation in standards_evaluations.values()
    )

    selection = build_topology_comparison_selection(
        spec=spec,
        evaluation=evaluation,
        selected_variant_id=variants[1].variant_id,
        selected_by='fixture-user',
        selected_at_utc='2026-09-19T14:31:00+00:00',
        rationale='Selected for downstream review only.',
    )
    repository.save_selection(selection)
    assert selection.selection_state == 'selected_only'
    assert selection.applied is False
    assert selection.installed is False
    assert selection.measured is False

    # Selection is not deployment/application. No SceneRevision is created.
    assert scene_repository.latest(DOCUMENT_ID).revision_id == baseline_before.revision_id
    assert scene_repository.latest(DOCUMENT_ID).content_hash == baseline_before.content_hash

    reopened = CadTopologyComparisonRepository(
        scene_repository=scene_repository,
        system_variant_repository=variant_repository,
        standards_repository=standards_repository,
        external_resolvers={
            'coverage_evaluation': resolve_external,
            'direct_level_evaluation': resolve_external,
        },
    )
    assert reopened.get_spec(spec.comparison_id) == spec
    assert reopened.get_bundle(bundles[1].bundle_id) == bundles[1]
    assert reopened.get_evaluation(evaluation.evaluation_id) == evaluation
    assert reopened.get_selection(selection.selection_id) == selection

    # Reopen resolves the exact external evaluation hash rather than trusting
    # persisted JSON alone.
    coverage_ref = coverage_refs[variants[1].variant_id]
    external_authorities[coverage_ref.authority_id] = coverage_ref.model_copy(
        update={'semantic_sha256': '0' * 64}
    )
    with pytest.raises(ValueError, match='exact authority'):
        reopened.get_bundle(bundles[1].bundle_id)
