from __future__ import annotations

import json

import pytest

from htdt import cad_adaptive_planner
from htdt.cad_adaptive_planner import (
    build_adaptive_plan,
    development_validation_ready,
    production_validation_ready,
    select_predicted_evaluations,
)
from htdt.cad_adaptive_repository import CadAdaptivePlanRepository
from htdt.cad_adaptive_service import CadAdaptivePlannerService
from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_model_validation import build_full_model_validation
from htdt.cad_objective_authority import ResolvedObjectiveInput
from htdt.cad_objective_models import CadObjectiveInputRef, canonical_objective_sha256
from htdt.cad_objectives import build_objective_evaluation
from htdt.cad_objective_repository import CadObjectiveRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_search import build_cad_search_spec, generate_cad_candidates
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.cad_validation_metrics import (
    CadApplicabilityEvidenceRef,
    CadObjectiveValidationSample,
    build_applicability_check,
    build_candidate_separation_check,
    build_repeatability_check,
    build_sensitivity_check,
)
from htdt.comparison import FrequencyResponse
from htdt.optimization_objectives import ObjectiveMetric, ObjectiveVector


DOCUMENT_ID = 'o70-adaptive-fixture'


def _fr(offset: float) -> FrequencyResponse:
    return FrequencyResponse(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(80.0 + offset, 81.0 + offset, 79.0 + offset, 80.0 + offset),
    )


def _spec_metrics_evaluator(context):
    """Evaluator-owned fixture: replay objective metrics from the versioned spec."""

    metrics = context.spec.get('metrics')
    if not isinstance(metrics, list) or not metrics:
        raise ValueError('fixture metrics spec requires a non-empty metrics list')
    return ObjectiveVector(
        candidate_id=context.evaluation.candidate_id,
        metrics=tuple(
            ObjectiveMetric(
                objective_id=str(entry['objective_id']),
                value=float(entry['value']),
                unit=str(entry['unit']),
                direction=str(entry.get('direction', 'minimize')),
            )
            for entry in metrics
        ),
    )


def _prediction_fixture_resolver(context, ref):
    """Evaluator-owned fixture evidence pinned by exact source identity."""

    return ResolvedObjectiveInput(
        ref=ref,
        source_sha256=canonical_objective_sha256(
            {'source_kind': ref.source_kind, 'source_id': ref.source_id}
        ),
    )


def _manual_check(code: str, passed: bool, detail: str):
    """Fixture attestation-bound check; these tests never hit a repository."""
    return build_applicability_check(
        code=code,
        passed=passed,
        evaluator_id='o60-applicability-manual',
        evaluator_version='1',
        subject={
            'code': code,
            'document_id': DOCUMENT_ID,
            'scope': {},
            'search_spec_id': 'synthetic-fixture-spec',
            'search_spec_sha256': '7' * 64,
        },
        evidence_refs=(
            CadApplicabilityEvidenceRef(
                source_kind='o60_applicability_attestation',
                source_id='o60-applicability-attestation:' + '8' * 64,
                source_sha256='8' * 64,
            ),
        ),
        detail=detail,
    )


class _ValidationRepository:
    def __init__(self, path, records):
        self.path = path
        self.records = {record.validation_id: record for record in records}

    def get(self, validation_id):
        return self.records.get(validation_id)

    def latest_eligible_for_search_spec(self, search_spec_id):
        records = [
            record
            for record in self.records.values()
            if record.search_spec_id == search_spec_id
            and record.recommendation_gate == 'eligible'
        ]
        return records[-1] if records else None


def _fixture(tmp_path, *, applicability_pass: bool = True):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        SceneDocument(
            document_id=DOCUMENT_ID,
            schema_version=2,
            room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
            entities=(
                SceneEntity(
                    entity_id='fl',
                    kind='speaker',
                    name='FL',
                    speaker_role='FL',
                    position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                    size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.4),
                ),
            ),
        ),
        parent_revision_id=None,
    ).revision
    spec, _estimate = build_cad_search_spec(
        revision,
        CadConstraintSet(document_id=DOCUMENT_ID, constraints=()),
        (
            CadSearchAxis(
                entity_id='fl',
                axis='x',
                min_m=1.0,
                max_m=2.0,
                step_m=0.2,
            ),
        ),
        candidate_limit=20,
        name='O70 synthetic sweep',
    )
    search_repository = CadSearchRepository(scene_repository)
    search_repository.save(spec)
    page = generate_cad_candidates(scene_repository, spec, limit=20)
    assert len(page.candidates) == 6
    candidate_ids = tuple(item.candidate_id for item in page.candidates)

    objective_repository = CadObjectiveRepository(
        scene_repository,
        search_repository,
        input_resolvers={'prediction_fixture': _prediction_fixture_resolver},
        vector_evaluators={'fixture-metrics-1': _spec_metrics_evaluator},
    )
    predicted_values = {}
    for index, candidate in enumerate(page.candidates):
        value = float(index + 1)
        predicted_values[candidate.candidate_id] = value
        evaluation = build_objective_evaluation(
            revision,
            spec,
            candidate.candidate_id,
            ObjectiveVector(
                candidate_id=candidate.candidate_id,
                metrics=(
                    ObjectiveMetric(
                        objective_id='response.shape_rms_db',
                        value=value,
                        unit='dB',
                    ),
                ),
            ),
            evaluation_spec={
                'algorithm_version': 'objective-vector-1',
                'objective_method': 'fixture-metrics-1',
                'objectives': ['response.shape_rms_db'],
                'metrics': [
                    {
                        'objective_id': 'response.shape_rms_db',
                        'value': value,
                        'unit': 'dB',
                        'direction': 'minimize',
                    },
                ],
            },
            input_refs=(
                CadObjectiveInputRef(
                    evidence_class='predicted',
                    source_kind='prediction_fixture',
                    source_id=f'pred:{candidate.candidate_id}',
                ),
            ),
        )
        objective_repository.save_evaluation(evaluation)

    split = {
        candidate_ids[0]: 'calibration',
        candidate_ids[1]: 'calibration',
        candidate_ids[2]: 'holdout',
        candidate_ids[3]: 'holdout',
    }
    measured_values = {
        candidate_ids[0]: predicted_values[candidate_ids[0]] + 0.2,
        candidate_ids[1]: predicted_values[candidate_ids[1]] + 0.1,
        candidate_ids[2]: predicted_values[candidate_ids[2]] + 0.1,
        candidate_ids[3]: predicted_values[candidate_ids[3]] + 0.1,
    }
    objective_samples = tuple(
        CadObjectiveValidationSample(
            candidate_id=candidate_id,
            split=role,
            objective_id='response.shape_rms_db',
            unit='dB',
            predicted_evaluation_id=f'pred-eval:{candidate_id}',
            measured_evaluation_id=f'meas-eval:{candidate_id}',
            predicted_value=predicted_values[candidate_id],
            measured_value=measured_values[candidate_id],
        )
        for candidate_id, role in split.items()
    )

    holdout_a = candidate_ids[2]
    holdout_b = candidate_ids[3]
    sensitivity = build_sensitivity_check(
        objective_id='response.shape_rms_db',
        unit='dB',
        candidate_a_id=holdout_a,
        candidate_b_id=holdout_b,
        placement_delta_m=0.2,
        predicted_a=predicted_values[holdout_a],
        predicted_b=predicted_values[holdout_b],
        measured_a=measured_values[holdout_a],
        measured_b=measured_values[holdout_b],
        max_observed_sensitivity_per_m=6.0,
        max_model_error_per_m=1.0,
    )
    repeatability = build_repeatability_check(
        scene_revision_id='synthetic-repeatability',
        measurements=(('repeat-1', _fr(0.0)), ('repeat-2', _fr(0.1))),
        low_hz=20.0,
        high_hz=160.0,
    )
    separation = build_candidate_separation_check(
        candidate_a_id=holdout_a,
        candidate_b_id=holdout_b,
        measurement_a_id=f'meas:{holdout_a}',
        measurement_b_id=f'meas:{holdout_b}',
        response_a=_fr(0.0),
        response_b=_fr(2.0),
        low_hz=20.0,
        high_hz=160.0,
        repeatability_floor_db=repeatability.rms_floor_db,
        min_repeatability_multiple=2.0,
    )

    response_samples = tuple(
        (
            candidate_id,
            role,
            f'pred:{candidate_id}',
            f'meas:{candidate_id}',
            _fr(float(index)),
            _fr(float(index) + 0.1),
        )
        for index, (candidate_id, role) in enumerate(split.items())
    )
    record = build_full_model_validation(
        document_id=DOCUMENT_ID,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=page.candidate_set_sha256,
        model_id='rew-roomsim',
        model_version='synthetic-fixture-1',
        response_samples=response_samples,
        objective_samples=objective_samples,
        sensitivity_checks=(sensitivity,),
        repeatability_checks=(repeatability,),
        separation_checks=(separation,),
        applicability_checks=(
            _manual_check('geometry', applicability_pass, 'synthetic rectangular fixture'),
            _manual_check('band', True, 'synthetic 20-160 Hz fixture'),
            _manual_check('routing', True, 'synthetic routing fixture'),
        ),
        low_hz=20.0,
        high_hz=160.0,
        max_holdout_rms_db=1.0,
        evidence_scope='synthetic_fixture',
        trend_min_agreement_ratio=0.75,
    )
    validation_repository = _ValidationRepository(scene_repository.path, (record,))
    adaptive_repository = CadAdaptivePlanRepository(
        search_repository,
        validation_repository,
        objective_repository,
    )
    service = CadAdaptivePlannerService(
        search_repository,
        objective_repository,
        validation_repository,
        adaptive_repository,
    )
    return (
        record,
        page,
        adaptive_repository,
        service,
        candidate_ids,
        objective_repository,
        spec,
        revision,
    )


def test_synthetic_development_plan_reaches_o70_without_unlocking_production(tmp_path):
    (
        record,
        _page,
        repository,
        service,
        candidate_ids,
        objectives,
        spec,
        _revision,
    ) = _fixture(tmp_path)

    assert development_validation_ready(record)
    assert not production_validation_ready(record)
    assert record.recommendation_gate == 'disabled'

    plan = service.build_and_save(
        validation_id=record.validation_id,
        execution_scope='development_synthetic',
        length_scale_m=0.35,
        proposal_limit=10,
    )

    assert plan.execution_scope == 'development_synthetic'
    assert plan.source_evidence_scope == 'synthetic_fixture'
    assert plan.validation_recommendation_gate == 'disabled'
    assert plan.proposal_limit == 10
    assert plan.training_candidate_ids == candidate_ids[:2]
    assert set(plan.excluded_measured_candidate_ids) == set(candidate_ids[:4])
    assert {proposal.candidate_id for proposal in plan.proposals} == set(candidate_ids[4:])
    assert plan.selected_candidate_id == candidate_ids[-1]
    assert plan.proposals[0].acquisition_score >= plan.proposals[1].acquisition_score

    # Every predicted objective input is identity-bound in the plan.
    assert len(plan.predicted_evaluation_refs) == plan.candidate_pool_count
    assert {
        ref.candidate_id for ref in plan.predicted_evaluation_refs
    } == set(candidate_ids[4:])
    for ref in plan.predicted_evaluation_refs:
        evaluation = objectives.get_evaluation(ref.evaluation_id)
        assert evaluation is not None
        assert evaluation.evaluation_sha256 == ref.evaluation_sha256
        assert evaluation.candidate_id == ref.candidate_id
        assert evaluation.search_spec_sha256 == spec.search_spec_sha256

    # Authoritative reads replay the canonical planner and reopen the plan.
    assert repository.get(plan.plan_id) == plan
    assert (
        repository.find_by_sha(plan.search_spec_id, plan.adaptive_sha256)
        == plan
    )
    assert repository.list_for_search_spec(plan.search_spec_id) == (plan,)

    repeated = service.build_and_save(
        validation_id=record.validation_id,
        execution_scope='development_synthetic',
        length_scale_m=0.35,
        proposal_limit=10,
    )
    assert repeated.plan_id == plan.plan_id
    assert repeated.adaptive_sha256 == plan.adaptive_sha256


def test_production_scope_rejects_synthetic_o60_even_when_all_technical_gates_pass(tmp_path):
    record, _page, _repository, service, _candidate_ids, _o, _s, _r = _fixture(
        tmp_path
    )

    with pytest.raises(ValueError, match='production adaptive planning requires'):
        service.build_and_save(
            validation_id=record.validation_id,
            execution_scope='production_owned_room',
        )


def test_development_scope_still_fails_closed_on_non_scope_o60_failure(tmp_path):
    record, _page, _repository, service, _candidate_ids, _o, _s, _r = _fixture(
        tmp_path,
        applicability_pass=False,
    )

    assert not development_validation_ready(record)
    assert any('applicability' in reason for reason in record.gate_reasons)
    with pytest.raises(ValueError, match='fully-passing synthetic O60'):
        service.build_and_save(
            validation_id=record.validation_id,
            execution_scope='development_synthetic',
        )


def _rebound_plan(plan, **updates):
    """Rehash a tampered plan so only canonical replay can reject it."""

    tampered = plan.model_copy(update=updates)
    return tampered.model_copy(update={
        'adaptive_sha256': cad_adaptive_planner._digest(
            tampered.identity_payload()
        ),
    })


def _save_newer_predicted(objectives, revision, spec, candidate_id, value):
    """Persist a newer predicted O30 evaluation for the same candidate."""

    evaluation = build_objective_evaluation(
        revision,
        spec,
        candidate_id,
        ObjectiveVector(
            candidate_id=candidate_id,
            metrics=(
                ObjectiveMetric(
                    objective_id='response.shape_rms_db',
                    value=value,
                    unit='dB',
                ),
            ),
        ),
        evaluation_spec={
            'algorithm_version': 'objective-vector-1',
            'objective_method': 'fixture-metrics-1',
            'objectives': ['response.shape_rms_db'],
            'metrics': [
                {
                    'objective_id': 'response.shape_rms_db',
                    'value': value,
                    'unit': 'dB',
                    'direction': 'minimize',
                },
            ],
        },
        input_refs=(
            CadObjectiveInputRef(
                evidence_class='predicted',
                source_kind='prediction_fixture',
                source_id=f'pred-superseding:{candidate_id}:{value}',
            ),
        ),
    )
    objectives.save_evaluation(evaluation)
    return evaluation


def test_save_rejects_rehashed_fabricated_planner_output(tmp_path):
    (
        record,
        _page,
        repository,
        service,
        _candidate_ids,
        _objectives,
        _spec,
        _revision,
    ) = _fixture(tmp_path)
    plan = service.build_and_save(
        validation_id=record.validation_id,
        execution_scope='development_synthetic',
        length_scale_m=0.35,
        proposal_limit=10,
    )
    proposals = plan.proposals

    # Caller-fabricated acquisition score.
    forged = _rebound_plan(
        plan,
        proposals=(
            proposals[0].model_copy(update={
                'acquisition_score': proposals[0].acquisition_score + 0.5,
            }),
        )
        + proposals[1:],
    )
    with pytest.raises(ValueError, match='canonical planner replay'):
        repository.save(forged)

    # Caller-fabricated GP corrected mean / residual uncertainty.
    estimate = proposals[0].objectives[0]
    forged = _rebound_plan(
        plan,
        proposals=(
            proposals[0].model_copy(update={
                'objectives': (
                    estimate.model_copy(update={
                        'corrected_mean': estimate.corrected_mean + 1.0,
                        'residual_uncertainty': 0.0,
                    }),
                )
                + proposals[0].objectives[1:],
            }),
        )
        + proposals[1:],
    )
    with pytest.raises(ValueError, match='canonical planner replay'):
        repository.save(forged)

    # Caller-fabricated proposal order and selected candidate.
    reversed_order = tuple(reversed(proposals))
    forged = _rebound_plan(
        plan,
        proposals=reversed_order,
        selected_candidate_id=reversed_order[0].candidate_id,
    )
    with pytest.raises(ValueError, match='canonical planner replay'):
        repository.save(forged)

    # Caller-fabricated training and measured-exclusion lists.
    forged = _rebound_plan(
        plan,
        training_candidate_ids=(
            proposals[0].candidate_id,
        )
        + plan.training_candidate_ids[1:],
    )
    with pytest.raises(ValueError, match='canonical planner replay'):
        repository.save(forged)
    forged = _rebound_plan(
        plan,
        excluded_measured_candidate_ids=plan.excluded_measured_candidate_ids[1:],
    )
    with pytest.raises(ValueError, match='canonical planner replay'):
        repository.save(forged)

    # A proposal whose predicted evidence ref is dropped is not constructible.
    forged = _rebound_plan(
        plan,
        predicted_evaluation_refs=plan.predicted_evaluation_refs[1:],
    )
    with pytest.raises(
        ValueError,
        match='must bind their predicted objective evidence',
    ):
        repository.save(forged)

    # An inflated candidate pool disagrees with the bound evidence.
    forged = _rebound_plan(
        plan,
        candidate_pool_count=plan.candidate_pool_count + 1,
    )
    with pytest.raises(
        ValueError,
        match='must cover the candidate pool',
    ):
        repository.save(forged)


def test_save_rejects_plan_built_on_superseded_predicted_evidence(tmp_path):
    (
        record,
        page,
        repository,
        _service,
        _candidate_ids,
        objectives,
        spec,
        revision,
    ) = _fixture(tmp_path)

    predicted = select_predicted_evaluations(
        objectives.list_evaluations(spec.search_spec_id),
        ('response.shape_rms_db',),
    )
    stale_plan = build_adaptive_plan(
        spec=spec,
        candidate_set_sha256=page.candidate_set_sha256,
        validation=record,
        candidates=page.candidates,
        predicted_evaluations=predicted,
        execution_scope='development_synthetic',
        length_scale_m=0.35,
        proposal_limit=10,
    )

    target = stale_plan.predicted_evaluation_refs[0]
    _save_newer_predicted(
        objectives,
        revision,
        spec,
        target.candidate_id,
        99.0,
    )

    with pytest.raises(
        ValueError,
        match='predicted objective evidence is not current',
    ):
        repository.save(stale_plan)


def test_save_rejects_plan_built_on_partial_objective_evidence(tmp_path):
    (
        record,
        page,
        repository,
        _service,
        candidate_ids,
        objectives,
        spec,
        _revision,
    ) = _fixture(tmp_path)

    predicted = select_predicted_evaluations(
        objectives.list_evaluations(spec.search_spec_id),
        ('response.shape_rms_db',),
    )
    subset = tuple(
        evaluation
        for evaluation in predicted
        if evaluation.candidate_id == candidate_ids[4]
    )
    partial_plan = build_adaptive_plan(
        spec=spec,
        candidate_set_sha256=page.candidate_set_sha256,
        validation=record,
        candidates=page.candidates,
        predicted_evaluations=subset,
        execution_scope='development_synthetic',
        length_scale_m=0.35,
        proposal_limit=10,
    )
    assert len(partial_plan.proposals) == 1

    with pytest.raises(ValueError, match='canonical planner replay'):
        repository.save(partial_plan)


def test_authoritative_reads_fail_closed_on_superseded_evidence(tmp_path):
    (
        record,
        _page,
        repository,
        service,
        _candidate_ids,
        objectives,
        spec,
        revision,
    ) = _fixture(tmp_path)
    plan = service.build_and_save(
        validation_id=record.validation_id,
        execution_scope='development_synthetic',
        length_scale_m=0.35,
        proposal_limit=10,
    )

    _save_newer_predicted(
        objectives,
        revision,
        spec,
        plan.selected_candidate_id,
        99.0,
    )

    # Every authoritative read fails closed on the stale persisted row.
    with pytest.raises(
        ValueError,
        match='predicted objective evidence is not current',
    ):
        repository.get(plan.plan_id)
    with pytest.raises(
        ValueError,
        match='predicted objective evidence is not current',
    ):
        repository.find_by_sha(plan.search_spec_id, plan.adaptive_sha256)
    with pytest.raises(
        ValueError,
        match='predicted objective evidence is not current',
    ):
        repository.list_for_search_spec(plan.search_spec_id)

    # The canonical rebuild over current evidence still persists/reopens.
    rebuilt = service.build_and_save(
        validation_id=record.validation_id,
        execution_scope='development_synthetic',
        length_scale_m=0.35,
        proposal_limit=10,
    )
    assert rebuilt.adaptive_sha256 != plan.adaptive_sha256
    assert repository.get(rebuilt.plan_id) == rebuilt
    assert (
        repository.find_by_sha(rebuilt.search_spec_id, rebuilt.adaptive_sha256)
        == rebuilt
    )
    # The stale row remains in history, so the listing still fails closed.
    with pytest.raises(
        ValueError,
        match='predicted objective evidence is not current',
    ):
        repository.list_for_search_spec(plan.search_spec_id)
