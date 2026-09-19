from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from htdt.cad_constraint_models import (
    CadConstraintPoint2D,
    CadConstraintSet,
)
from htdt.cad_proposal_robustness import (
    CadProposalRobustnessRepository,
    ProposalObjectiveEvidenceBinding,
    ProposalPerturbationObjectiveResult,
    build_proposal_robustness_spec,
    derive_proposal_multidimensional_robustness_spec,
    evaluate_proposal_local_robustness,
    evaluate_proposal_multidimensional_robustness,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.cad_topology_comparison import (
    ExactAuthorityRef,
    ObjectiveEvidenceBinding,
    VariantEvaluationBundle,
    canonical_topology_comparison_sha256,
)
from htdt.cad_topology_search import (
    ProposedPlacementSpec,
    build_topology_placement_search_spec,
    generate_topology_placement_candidates,
    topology_candidate_to_system_variant,
)
from htdt.cad_topology_search_repository import CadTopologySearchRepository
from htdt.cad_topology_space import build_topology_search_spec
from htdt.optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveValidDomain,
    ObjectiveVector,
)
from htdt.optimization_robustness import (
    LinkedPerturbationGroup,
    UncertaintyAxis,
)


NOW = '2026-09-20T00:00:00+00:00'
DOCUMENT_ID = 'o100f-proposal-robustness-fixture'
SPEAKER_ID = 'sl-proposed'
OBJECTIVE_ID = 'fixture.response_error_db'


def _point(x: float, y: float) -> CadConstraintPoint2D:
    return CadConstraintPoint2D(x_m=x, y_m=y)


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=2.5, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _proposed_speaker() -> SceneEntity:
    return SceneEntity(
        entity_id=SPEAKER_ID,
        kind='speaker',
        name='SL proposed',
        speaker_role='SL',
        position=Position3(x_m=1.0, y_m=2.0, z_m=1.3),
        size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.3),
        aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
    )


def _definition() -> ObjectiveDefinition:
    return ObjectiveDefinition(
        objective_id=OBJECTIVE_ID,
        quantity='response error',
        unit='dB',
        direction='minimize',
        valid_domain=ObjectiveValidDomain(kind='finite_real'),
        comparison_model_id='fixture-model',
        comparison_model_version='1',
    )


def _source_ref(authority_id: str) -> ExactAuthorityRef:
    return ExactAuthorityRef(
        authority_kind='fixture_prediction',
        authority_id=authority_id,
        authority_version='fixture-result-v1',
        semantic_sha256=sha256(authority_id.encode('utf-8')).hexdigest(),
        evaluator_id='fixture-objective-evaluator',
        evaluator_version='1',
        model_id='fixture-model',
        model_version='1',
        fidelity='fixture-common-fidelity',
    )


def _bundle(candidate_variant_id: str, candidate_variant_sha256: str):
    definition = _definition()
    source = _source_ref('nominal-result')
    vector = ObjectiveVector(
        candidate_id=candidate_variant_id,
        metrics=(
            ObjectiveMetric(
                objective_id=OBJECTIVE_ID,
                value=2.0,
                unit='dB',
                direction='minimize',
                definition=definition,
            ),
        ),
    )
    vector_sha = canonical_topology_comparison_sha256(
        vector.identity_payload()
    )
    evidence = (
        ObjectiveEvidenceBinding(
            objective_id=OBJECTIVE_ID,
            source_authority_kind=source.authority_kind,
            source_authority_id=source.authority_id,
            source_semantic_sha256=source.semantic_sha256,
        ),
    )
    comparison_sha = 'd' * 64
    base = {
        'schema_version': 1,
        'authority_version': 'o100d-topology-variant-bundle-1',
        'comparison_id': 'fixture-comparison',
        'comparison_semantic_sha256': comparison_sha,
        'variant_id': candidate_variant_id,
        'variant_sha256': candidate_variant_sha256,
        'coverage_evaluation': None,
        'direct_level_evaluation': None,
        'amplifier_headroom_evaluation': None,
        'standards_evaluation': None,
        'fr_prediction_refs': [source.identity_payload()],
        'reflection_prediction_refs': [],
        'installation_evidence_refs': [],
        'objective_vector': vector.identity_payload(),
        'objective_vector_sha256': vector_sha,
        'objective_evidence': [
            item.model_dump(mode='json') for item in evidence
        ],
    }
    digest = canonical_topology_comparison_sha256(base)
    return VariantEvaluationBundle(
        bundle_id=f'topology-bundle-{digest[:24]}',
        comparison_id='fixture-comparison',
        comparison_semantic_sha256=comparison_sha,
        variant_id=candidate_variant_id,
        variant_sha256=candidate_variant_sha256,
        fr_prediction_refs=(source,),
        objective_vector=vector,
        objective_vector_sha256=vector_sha,
        objective_evidence=evidence,
        bundle_sha256=digest,
    )


class _BundleResolver:
    def __init__(self, path: Path, bundle: VariantEvaluationBundle) -> None:
        self.path = Path(path)
        self.bundle = bundle

    def get_bundle(self, bundle_id: str):
        return self.bundle if bundle_id == self.bundle.bundle_id else None


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = scene_repository.save(
        _scene(),
        parent_revision_id=None,
    ).revision

    template = build_system_variant(
        baseline=baseline,
        name='Proposed SL template',
        role_bindings=(
            ChannelRoleBinding(role_id='SL', display_name='SL'),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='proposal-sl',
                entity=_proposed_speaker(),
                role_binding_id='SL',
            ),
        ),
        created_at_utc=NOW,
    )
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant_repository.save_variant(template)

    topology = build_topology_search_spec(
        baseline=baseline,
        template_variants=(template,),
        optional_role_ids=('SL',),
        include_baseline=True,
        created_at_utc=NOW,
    )
    option_id = topology.options[0].option_id
    constraint_set = CadConstraintSet(
        document_id=DOCUMENT_ID,
        constraints=(),
    )
    placement = ProposedPlacementSpec(
        entity_id=SPEAKER_ID,
        role_id='SL',
        zone_id='left-side',
        allowed_region=(
            _point(0.7, 1.7),
            _point(1.3, 1.7),
            _point(1.3, 2.3),
            _point(0.7, 2.3),
        ),
        min_z_m=1.2,
        max_z_m=1.4,
        xyz_axes=(
            CadSearchAxis(
                entity_id=SPEAKER_ID,
                axis='x',
                min_m=1.0,
                max_m=1.0,
                step_m=0.1,
            ),
            CadSearchAxis(
                entity_id=SPEAKER_ID,
                axis='y',
                min_m=2.0,
                max_m=2.0,
                step_m=0.1,
            ),
            CadSearchAxis(
                entity_id=SPEAKER_ID,
                axis='z',
                min_m=1.3,
                max_m=1.3,
                step_m=0.1,
            ),
        ),
    )
    search = build_topology_placement_search_spec(
        baseline=baseline,
        template_variant=template,
        topology_spec=topology,
        topology_option_id=option_id,
        placement_specs=(placement,),
        constraint_set=constraint_set,
        candidate_limit=10,
        created_at_utc=NOW,
    )

    topology_repository = CadTopologySearchRepository(variant_repository)
    topology_repository.save_topology_spec(topology)
    topology_repository.save_spec(search)
    page = generate_topology_placement_candidates(
        baseline=baseline,
        template_variant=template,
        spec=search,
        offset=0,
        limit=10,
    )
    assert len(page.candidates) == 1
    candidate = page.candidates[0]
    topology_repository.save_candidate_page(page)

    candidate_variant = topology_candidate_to_system_variant(
        baseline=baseline,
        template_variant=template,
        spec=search,
        candidate=candidate,
        created_at_utc=NOW,
    )
    topology_repository.save_candidate_variant(
        candidate.candidate_id,
        candidate_variant,
    )
    comparison_ref = topology_repository.comparison_ref(
        candidate.candidate_id
    )
    assert comparison_ref.variant_id == candidate_variant.variant_id
    assert comparison_ref.applied_revision_id is None

    bundle = _bundle(
        candidate_variant.variant_id,
        candidate_variant.variant_sha256,
    )
    exact_constraints = CadConstraintSet.model_validate(
        json.loads(search.constraint_snapshot_json)
    )
    axes = (
        UncertaintyAxis(
            axis_id='speaker-x',
            entity_id=SPEAKER_ID,
            parameter='speaker_x_m',
            unit='m',
            nominal_value=1.0,
            minus_delta=0.4,
            plus_delta=0.4,
        ),
        UncertaintyAxis(
            axis_id='aim-yaw',
            entity_id=SPEAKER_ID,
            parameter='aim_yaw_deg',
            unit='deg',
            nominal_value=0.0,
            minus_delta=5.0,
            plus_delta=5.0,
        ),
        UncertaintyAxis(
            axis_id='body-yaw',
            entity_id=SPEAKER_ID,
            parameter='body_yaw_deg',
            unit='deg',
            nominal_value=0.0,
            minus_delta=5.0,
            plus_delta=5.0,
        ),
    )
    spec = build_proposal_robustness_spec(
        baseline=baseline,
        template_variant=template,
        candidate_variant=candidate_variant,
        topology_spec=search,
        topology_candidate=candidate,
        candidate_set_sha256=page.candidate_set_sha256,
        nominal_bundle=bundle,
        objective_ids=(OBJECTIVE_ID,),
        axes=axes,
        software_version='fixture-1',
        created_at_utc=NOW,
    )
    return {
        'scene_repository': scene_repository,
        'baseline': baseline,
        'variant_repository': variant_repository,
        'template': template,
        'topology_repository': topology_repository,
        'search': search,
        'candidate': candidate,
        'candidate_page': page,
        'candidate_variant': candidate_variant,
        'bundle': bundle,
        'constraint_set': exact_constraints,
        'spec': spec,
    }


def _execute(fx):
    refs: dict[str, ExactAuthorityRef] = {}

    def evaluator(document: SceneDocument, sample_id: str):
        speaker = document.entity(SPEAKER_ID)
        value = 2.0 + abs(float(speaker.position.x_m) - 1.0)
        ref = _source_ref(f'perturbed:{sample_id}')
        refs[ref.authority_id] = ref
        return ProposalPerturbationObjectiveResult(
            objective_vector=ObjectiveVector(
                candidate_id=sample_id,
                metrics=(
                    ObjectiveMetric(
                        objective_id=OBJECTIVE_ID,
                        value=value,
                        unit='dB',
                        direction='minimize',
                        definition=_definition(),
                    ),
                ),
            ),
            objective_evidence=(
                ProposalObjectiveEvidenceBinding(
                    objective_id=OBJECTIVE_ID,
                    result_ref=ref,
                ),
            ),
        )

    samples, evaluations = evaluate_proposal_local_robustness(
        baseline=fx['baseline'],
        template_variant=fx['template'],
        candidate_variant=fx['candidate_variant'],
        topology_spec=fx['search'],
        topology_candidate=fx['candidate'],
        candidate_set_sha256=fx['candidate_page'].candidate_set_sha256,
        spec=fx['spec'],
        constraint_set=fx['constraint_set'],
        nominal_bundle=fx['bundle'],
        evaluator=evaluator,
        created_at_utc=NOW,
    )
    return refs, samples, evaluations


def test_proposed_speaker_uses_existing_o90_local_semantics_without_baseline_mutation(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    refs, samples, evaluations = _execute(fx)

    assert len(samples) == 7
    x_samples = [
        item for item in samples
        if item.axis_id == 'speaker-x'
    ]
    assert len(x_samples) == 2
    assert all(not item.feasible for item in x_samples)
    assert all(item.objective_vector is None for item in x_samples)
    assert all(
        item.failure_reason == 'hard_constraint_violation'
        for item in x_samples
    )

    for axis_id in ('aim-yaw', 'body-yaw'):
        axis_samples = [item for item in samples if item.axis_id == axis_id]
        assert len(axis_samples) == 2
        assert all(item.feasible for item in axis_samples)
        assert all(item.objective_vector is not None for item in axis_samples)

    assert len(evaluations) == 1
    evaluation = evaluations[0]
    assert evaluation.objective_id == OBJECTIVE_ID
    assert set(evaluation.infeasible_sample_ids) == {
        item.sample_id for item in x_samples
    }
    assert evaluation.direction == 'minimize'
    assert refs

    latest = fx['scene_repository'].latest(DOCUMENT_ID)
    assert latest is not None
    assert latest.revision_id == fx['baseline'].revision_id
    with pytest.raises(KeyError):
        latest.document.entity(SPEAKER_ID)
    assert (
        fx['variant_repository'].application_for_variant(
            fx['candidate_variant'].variant_id
        )
        is None
    )


def test_proposal_robustness_rejects_template_as_candidate_variant(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)

    with pytest.raises(
        ValueError,
        match='candidate variant must descend from template',
    ):
        build_proposal_robustness_spec(
            baseline=fx['baseline'],
            template_variant=fx['template'],
            candidate_variant=fx['template'],
            topology_spec=fx['search'],
            topology_candidate=fx['candidate'],
            candidate_set_sha256=fx['candidate_page'].candidate_set_sha256,
            nominal_bundle=fx['bundle'],
            objective_ids=(OBJECTIVE_ID,),
            axes=fx['spec'].axes,
            software_version='fixture-1',
            created_at_utc=NOW,
        )


def test_proposal_robustness_save_reopen_reresolves_exact_lineage(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    refs, samples, evaluations = _execute(fx)
    bundle_resolver = _BundleResolver(
        fx['scene_repository'].path,
        fx['bundle'],
    )

    def resolve(authority_id: str):
        return refs.get(authority_id)

    repository = CadProposalRobustnessRepository(
        scene_repository=fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        topology_repository=fx['topology_repository'],
        bundle_resolver=bundle_resolver,
        external_resolvers={'fixture_prediction': resolve},
    )
    repository.save_spec(fx['spec'])
    repository.save_samples(samples)
    repository.save_evaluations(evaluations)

    reopened_scene = SceneRepository(fx['scene_repository'].path)
    reopened_variants = CadSystemVariantRepository(reopened_scene)
    reopened_topology = CadTopologySearchRepository(reopened_variants)
    reopened = CadProposalRobustnessRepository(
        scene_repository=reopened_scene,
        variant_repository=reopened_variants,
        topology_repository=reopened_topology,
        bundle_resolver=_BundleResolver(reopened_scene.path, fx['bundle']),
        external_resolvers={'fixture_prediction': resolve},
    )

    assert reopened.get_spec(fx['spec'].robustness_spec_id) == fx['spec']
    assert reopened.list_samples(fx['spec'].robustness_spec_id) == samples
    assert (
        reopened.list_evaluations(fx['spec'].robustness_spec_id)
        == evaluations
    )

    stale_id = next(iter(refs))
    refs.pop(stale_id)
    with pytest.raises(
        ValueError,
        match='external authority does not exist',
    ):
        reopened.list_samples(fx['spec'].robustness_spec_id)


def _execute_multidimensional(fx):
    multidimensional_axes = tuple(
        axis.model_copy(update={'plus_delta': 0.2})
        if axis.axis_id == 'speaker-x'
        else axis
        for axis in fx['spec'].axes
    )
    parent = build_proposal_robustness_spec(
        baseline=fx['baseline'],
        template_variant=fx['template'],
        candidate_variant=fx['candidate_variant'],
        topology_spec=fx['search'],
        topology_candidate=fx['candidate'],
        candidate_set_sha256=fx['candidate_page'].candidate_set_sha256,
        nominal_bundle=fx['bundle'],
        objective_ids=(OBJECTIVE_ID,),
        axes=multidimensional_axes,
        software_version='fixture-multidimensional-1',
        created_at_utc=NOW,
    )
    child = derive_proposal_multidimensional_robustness_spec(
        parent,
        sample_count=5,
        seed=71656,
        linked_groups=(
            LinkedPerturbationGroup(
                group_id='orientation-link',
                axis_multipliers={
                    'aim-yaw': 1.0,
                    'body-yaw': 1.0,
                },
            ),
        ),
        created_at_utc=NOW,
    )
    refs: dict[str, ExactAuthorityRef] = {}

    def evaluator(document: SceneDocument, sample_id: str):
        speaker = document.entity(SPEAKER_ID)
        value = 2.0 + abs(float(speaker.position.x_m) - 1.0)
        ref = _source_ref(f'multidimensional:{sample_id}')
        refs[ref.authority_id] = ref
        return ProposalPerturbationObjectiveResult(
            objective_vector=ObjectiveVector(
                candidate_id=sample_id,
                metrics=(
                    ObjectiveMetric(
                        objective_id=OBJECTIVE_ID,
                        value=value,
                        unit='dB',
                        direction='minimize',
                        definition=_definition(),
                    ),
                ),
            ),
            objective_evidence=(
                ProposalObjectiveEvidenceBinding(
                    objective_id=OBJECTIVE_ID,
                    result_ref=ref,
                ),
            ),
        )

    samples, evaluations = evaluate_proposal_multidimensional_robustness(
        baseline=fx['baseline'],
        template_variant=fx['template'],
        candidate_variant=fx['candidate_variant'],
        topology_spec=fx['search'],
        topology_candidate=fx['candidate'],
        candidate_set_sha256=fx['candidate_page'].candidate_set_sha256,
        spec=child,
        parent_spec=parent,
        constraint_set=fx['constraint_set'],
        nominal_bundle=fx['bundle'],
        evaluator=evaluator,
        created_at_utc=NOW,
    )
    return parent, child, refs, samples, evaluations


def test_proposal_multidimensional_reuses_o90b_sampling_and_envelope(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    parent, child, refs, samples, evaluations = _execute_multidimensional(fx)

    assert child.parent_robustness_spec_id == parent.robustness_spec_id
    assert child.sample_count == 5
    assert len(samples) == 5
    assert samples[0].step == 'nominal'
    assert samples[1].step == 'multidimensional'
    assert samples[2].step == 'multidimensional'
    assert not samples[1].feasible
    assert samples[2].feasible
    assert samples[1].objective_vector is None
    assert samples[2].objective_vector is not None

    assert len(evaluations) == 1
    evaluation = evaluations[0]
    assert evaluation.objective_id == OBJECTIVE_ID
    assert evaluation.sampled_envelope is not None
    assert evaluation.feasible_fraction is not None
    assert 0.0 < evaluation.feasible_fraction < 1.0
    assert evaluation.percentile_semantics == 'not_available_bounded_interval'
    assert evaluation.sampling_provenance_sha256 is not None
    assert set(evaluation.infeasible_sample_ids).issuperset(
        {samples[1].sample_id}
    )
    assert refs

    latest = fx['scene_repository'].latest(DOCUMENT_ID)
    assert latest is not None
    assert latest.revision_id == fx['baseline'].revision_id
    with pytest.raises(KeyError):
        latest.document.entity(SPEAKER_ID)


def test_proposal_multidimensional_save_reopen_requires_parent_and_exact_results(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    parent, child, refs, samples, evaluations = _execute_multidimensional(fx)

    def resolve(authority_id: str):
        return refs.get(authority_id)

    repository = CadProposalRobustnessRepository(
        scene_repository=fx['scene_repository'],
        variant_repository=fx['variant_repository'],
        topology_repository=fx['topology_repository'],
        bundle_resolver=_BundleResolver(
            fx['scene_repository'].path,
            fx['bundle'],
        ),
        external_resolvers={'fixture_prediction': resolve},
    )
    with pytest.raises(
        ValueError,
        match='requires persisted local parent',
    ):
        repository.save_spec(child)

    repository.save_spec(parent)
    repository.save_spec(child)
    repository.save_samples(samples)
    repository.save_evaluations(evaluations)

    reopened_scene = SceneRepository(fx['scene_repository'].path)
    reopened_variants = CadSystemVariantRepository(reopened_scene)
    reopened_topology = CadTopologySearchRepository(reopened_variants)
    reopened = CadProposalRobustnessRepository(
        scene_repository=reopened_scene,
        variant_repository=reopened_variants,
        topology_repository=reopened_topology,
        bundle_resolver=_BundleResolver(reopened_scene.path, fx['bundle']),
        external_resolvers={'fixture_prediction': resolve},
    )

    assert reopened.get_spec(child.robustness_spec_id) == child
    assert reopened.list_samples(child.robustness_spec_id) == samples
    assert reopened.list_evaluations(child.robustness_spec_id) == evaluations

    stale_id = next(iter(refs))
    refs.pop(stale_id)
    with pytest.raises(
        ValueError,
        match='external authority does not exist',
    ):
        reopened.list_samples(child.robustness_spec_id)
