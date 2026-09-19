from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_multifidelity import (
    CadMultiFidelityRepository,
    MultiFidelityAuthorityRef,
    MultiFidelityStageDefinition,
    MultiFidelityStageOutcome,
    build_multifidelity_plan,
    build_multifidelity_stage_result,
    evaluate_multifidelity_screening,
    finalize_o90_multifidelity,
)
from htdt.cad_objective_models import CadObjectiveInputRef
from htdt.cad_objectives import build_objective_evaluation
from htdt.cad_repository import SceneRepository
from htdt.cad_robust_pareto import (
    CadO90RobustParetoRepository,
    build_o90_robust_pareto_evaluation,
)
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_search import build_cad_search_spec, generate_cad_candidates
from htdt.cad_search_models import CadSearchAxis
from htdt.optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveValidDomain,
    ObjectiveVector,
)
from htdt.optimization_robustness import (
    PerturbationObjectiveResult,
    UncertaintyAxis,
    build_robustness_spec,
    evaluate_local_robustness,
)
from htdt.optimization_robustness_multidimensional import RobustParetoSelection


NOW = '2026-09-20T00:00:00+00:00'
DOCUMENT_ID = 'o90c-robust-pareto-fixture'
OBJECTIVE_ID = 'fixture.response_error_db'


def _definition() -> ObjectiveDefinition:
    return ObjectiveDefinition(
        objective_id=OBJECTIVE_ID,
        quantity='response error',
        unit='dB',
        direction='minimize',
        valid_domain=ObjectiveValidDomain(kind='finite_real'),
        comparison_model_id='fixture-response-model',
        comparison_model_version='1',
    )


def _fixture(tmp_path: Path, *, second_fidelity: str = 'common'):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        SceneDocument(
            document_id=DOCUMENT_ID,
            schema_version=3,
            room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
            entities=(
                SceneEntity(
                    entity_id='speaker-fl',
                    kind='speaker',
                    name='FL',
                    speaker_role='FL',
                    position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                    size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.4),
                    aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
                ),
            ),
        ),
        parent_revision_id=None,
    ).revision
    constraints = CadConstraintSet(document_id=DOCUMENT_ID, constraints=())
    search_spec, _ = build_cad_search_spec(
        revision,
        constraints,
        (
            CadSearchAxis(
                entity_id='speaker-fl',
                axis='x',
                min_m=1.0,
                max_m=1.1,
                step_m=0.1,
            ),
        ),
        candidate_limit=10,
        name='O90C robust Pareto fixture',
    )
    page = generate_cad_candidates(scene_repository, search_spec)
    assert len(page.candidates) == 2

    definition = _definition()
    nominals = []
    specs = []
    evaluations_by_candidate = {}
    worst_by_index = (3.0, 2.5)
    nominal_by_index = (1.0, 2.0)

    for index, candidate in enumerate(page.candidates):
        prediction_ref = f'prediction:{candidate.candidate_id}'
        nominal = build_objective_evaluation(
            revision,
            search_spec,
            candidate.candidate_id,
            ObjectiveVector(
                candidate_id=candidate.candidate_id,
                metrics=(
                    ObjectiveMetric(
                        objective_id=OBJECTIVE_ID,
                        value=nominal_by_index[index],
                        unit='dB',
                        direction='minimize',
                        definition=definition,
                    ),
                ),
            ),
            evaluation_spec={
                'algorithm_version': 'fixture-objective-1',
                'objectives': [OBJECTIVE_ID],
            },
            input_refs=(
                CadObjectiveInputRef(
                    evidence_class='predicted',
                    source_kind='fixture_prediction',
                    source_id=prediction_ref,
                ),
            ),
        )
        candidate_x = float(candidate.positions['speaker-fl']['x_m'])
        spec = build_robustness_spec(
            source_revision=revision,
            search_spec=search_spec,
            candidate=candidate,
            candidate_set_sha256=page.candidate_set_sha256,
            nominal_objective=nominal,
            nominal_prediction_result_ref=prediction_ref,
            model_id='fixture-model',
            model_version='1',
            prediction_provider_id='fixture-provider',
            fidelity=(
                'common' if index == 0 else second_fidelity
            ),
            axes=(
                UncertaintyAxis(
                    axis_id='speaker-x',
                    entity_id='speaker-fl',
                    parameter='speaker_x_m',
                    unit='m',
                    nominal_value=candidate_x,
                    minus_delta=0.01,
                    plus_delta=0.01,
                ),
            ),
            software_version='fixture-1',
            created_at_utc=NOW,
        )

        def evaluator(
            _document: SceneDocument,
            sample_id: str,
            *,
            worst_value: float = worst_by_index[index],
        ) -> PerturbationObjectiveResult:
            return PerturbationObjectiveResult(
                prediction_result_ref=f'prediction:{sample_id}',
                objective_vector=ObjectiveVector(
                    candidate_id=sample_id,
                    metrics=(
                        ObjectiveMetric(
                            objective_id=OBJECTIVE_ID,
                            value=worst_value,
                            unit='dB',
                            direction='minimize',
                            definition=definition,
                        ),
                    ),
                ),
            )

        _samples, evaluations = evaluate_local_robustness(
            source_revision=revision,
            search_spec=search_spec,
            spec=spec,
            constraint_set=constraints,
            nominal_objective=nominal,
            evaluator=evaluator,
            created_at_utc=NOW,
        )
        nominals.append(nominal)
        specs.append(spec)
        evaluations_by_candidate[candidate.candidate_id] = evaluations

    return (
        scene_repository,
        revision,
        page,
        tuple(nominals),
        tuple(specs),
        evaluations_by_candidate,
    )


class _ObjectiveResolver:
    def __init__(self, path: Path, evaluations) -> None:
        self.path = Path(path)
        self.values = {item.evaluation_id: item for item in evaluations}

    def get_evaluation(self, evaluation_id: str):
        return self.values.get(evaluation_id)


class _RobustnessResolver:
    def __init__(self, path: Path, specs, evaluations) -> None:
        self.db_path = Path(path)
        self.specs = {item.robustness_spec_id: item for item in specs}
        self.evaluations = {
            spec.robustness_spec_id: tuple(evaluations[spec.candidate_id])
            for spec in specs
        }

    def get_spec(self, robustness_spec_id: str):
        try:
            return self.specs[robustness_spec_id]
        except KeyError as exc:
            raise KeyError(robustness_spec_id) from exc

    def list_evaluations(self, robustness_spec_id: str):
        return self.evaluations.get(robustness_spec_id, ())


class _RobustParetoResolver:
    def __init__(self, path: Path, evaluation) -> None:
        self.path = Path(path)
        self.evaluation = evaluation

    def get(self, evaluation_id: str):
        if evaluation_id == self.evaluation.evaluation_id:
            return self.evaluation
        return None


def _selection() -> RobustParetoSelection:
    return RobustParetoSelection(
        nominal_objective_ids=(OBJECTIVE_ID,),
        robustness_objective_ids=(OBJECTIVE_ID,),
    )


def test_o90_robust_pareto_reuses_direction_aware_nominal_robust_tradeoff(
    tmp_path: Path,
) -> None:
    (
        _scene_repository,
        _revision,
        _page,
        nominals,
        specs,
        evaluations,
    ) = _fixture(tmp_path)

    result = build_o90_robust_pareto_evaluation(
        nominal_evaluations=nominals,
        robustness_specs=specs,
        robustness_evaluations=evaluations,
        selection=_selection(),
        created_at_utc=NOW,
    )

    assert set(result.pareto_result.non_dominated_candidate_ids) == {
        item.candidate_id for item in nominals
    }
    assert result.pareto_result.objective_ids == (
        f'nominal::{OBJECTIVE_ID}',
        f'robust.sampled_worst::{OBJECTIVE_ID}',
    )
    assert len(result.comparison_signature_sha256) == 64


def test_o90_robust_pareto_rejects_fidelity_mismatch(tmp_path: Path) -> None:
    (
        _scene_repository,
        _revision,
        _page,
        nominals,
        specs,
        evaluations,
    ) = _fixture(tmp_path, second_fidelity='different')

    with pytest.raises(
        ValueError,
        match='robustness comparison signature mismatch',
    ):
        build_o90_robust_pareto_evaluation(
            nominal_evaluations=nominals,
            robustness_specs=specs,
            robustness_evaluations=evaluations,
            selection=_selection(),
            created_at_utc=NOW,
        )


def test_o90_robust_pareto_save_reopen_and_stale_eval_rejection(
    tmp_path: Path,
) -> None:
    (
        scene_repository,
        _revision,
        _page,
        nominals,
        specs,
        evaluations,
    ) = _fixture(tmp_path)
    result = build_o90_robust_pareto_evaluation(
        nominal_evaluations=nominals,
        robustness_specs=specs,
        robustness_evaluations=evaluations,
        selection=_selection(),
        created_at_utc=NOW,
    )
    objective_resolver = _ObjectiveResolver(scene_repository.path, nominals)
    robustness_resolver = _RobustnessResolver(
        scene_repository.path,
        specs,
        evaluations,
    )
    repository = CadO90RobustParetoRepository(
        objective_repository=objective_resolver,
        robustness_repository=robustness_resolver,
    )
    repository.save(result)

    reopened = CadO90RobustParetoRepository(
        objective_repository=objective_resolver,
        robustness_repository=robustness_resolver,
    )
    assert reopened.get(result.evaluation_id) == result

    first_spec = specs[0]
    robustness_resolver.evaluations[first_spec.robustness_spec_id] = ()
    with pytest.raises(
        ValueError,
        match='robustness evaluation disappeared',
    ):
        reopened.get(result.evaluation_id)


def _multifidelity_ref(kind: str, authority_id: str, digest: str):
    return MultiFidelityAuthorityRef(
        authority_kind=kind,
        authority_id=authority_id,
        authority_version='1',
        semantic_sha256=digest,
    )


def test_o90_multifidelity_finalization_requires_exact_survivor_hashes(
    tmp_path: Path,
) -> None:
    (
        scene_repository,
        revision,
        _page,
        nominals,
        specs,
        evaluations,
    ) = _fixture(tmp_path)
    robust = build_o90_robust_pareto_evaluation(
        nominal_evaluations=nominals,
        robustness_specs=specs,
        robustness_evaluations=evaluations,
        selection=_selection(),
        created_at_utc=NOW,
    )
    candidates = tuple(
        _multifidelity_ref(
            'cad_candidate',
            item.candidate_id,
            spec.candidate_sha256,
        )
        for item, spec in zip(nominals, specs, strict=True)
    )
    plan = build_multifidelity_plan(
        domain='o90_robustness',
        name='O90C finalization fixture',
        baseline_authority=_multifidelity_ref(
            'scene_revision',
            revision.revision_id,
            revision.content_hash,
        ),
        candidates=candidates,
        stages=(
            MultiFidelityStageDefinition(
                stage_id='screen',
                order=0,
                name='Screen',
                policy='hard_gate',
                fidelity_label='screen',
                evaluator_authority=_multifidelity_ref(
                    'o90_screen_evaluator',
                    'screen-evaluator',
                    'a' * 64,
                ),
            ),
            MultiFidelityStageDefinition(
                stage_id='final',
                order=1,
                name='Final',
                policy='final_common_fidelity',
                fidelity_label='common',
                evaluator_authority=_multifidelity_ref(
                    'o90_robust_pareto_evaluator',
                    'robust-pareto',
                    'b' * 64,
                ),
            ),
        ),
    )
    stage = build_multifidelity_stage_result(
        plan=plan,
        stage_id='screen',
        input_candidates=plan.candidates,
        outcomes=tuple(
            MultiFidelityStageOutcome(
                candidate=item,
                decision='ADVANCE',
            )
            for item in plan.candidates
        ),
    )
    screening = evaluate_multifidelity_screening(
        plan=plan,
        stage_results=(stage,),
        created_at_utc=NOW,
    )
    finalization = finalize_o90_multifidelity(
        plan=plan,
        screening=screening,
        final_robust_pareto=robust,
    )
    assert finalization.claim_state == 'COMPLETE'
    assert finalization.final_comparison_ref.authority_id == robust.evaluation_id

    multifidelity = CadMultiFidelityRepository(
        scene_repository,
        o90_robust_pareto_repository=_RobustParetoResolver(
            scene_repository.path,
            robust,
        ),
    )
    multifidelity.save_plan(plan)
    multifidelity.save_stage_result(stage)
    multifidelity.save_screening(screening)
    multifidelity.save_finalization(finalization)

    reopened = CadMultiFidelityRepository(
        SceneRepository(scene_repository.path),
        o90_robust_pareto_repository=_RobustParetoResolver(
            scene_repository.path,
            robust,
        ),
    )
    assert reopened.get_finalization(finalization.finalization_id) == finalization

    wrong_candidates = list(plan.candidates)
    wrong_candidates[0] = wrong_candidates[0].model_copy(
        update={'semantic_sha256': 'f' * 64}
    )
    wrong_plan = build_multifidelity_plan(
        domain='o90_robustness',
        name='Wrong O90C finalization fixture',
        baseline_authority=plan.baseline_authority,
        candidates=tuple(wrong_candidates),
        stages=plan.stages,
    )
    wrong_stage = build_multifidelity_stage_result(
        plan=wrong_plan,
        stage_id='screen',
        input_candidates=wrong_plan.candidates,
        outcomes=tuple(
            MultiFidelityStageOutcome(candidate=item, decision='ADVANCE')
            for item in wrong_plan.candidates
        ),
    )
    wrong_screening = evaluate_multifidelity_screening(
        plan=wrong_plan,
        stage_results=(wrong_stage,),
        created_at_utc=NOW,
    )
    with pytest.raises(
        ValueError,
        match='candidate hash differs from plan candidate',
    ):
        finalize_o90_multifidelity(
            plan=wrong_plan,
            screening=wrong_screening,
            final_robust_pareto=robust,
        )
