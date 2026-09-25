"""#945: headless joint-execution service — bounded candidate stream,
exact SystemVariant/CalibrationPlan materialization, persisted
candidate/evaluation authority, staleness and cancellation."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_joint_execution import (
    JointEvaluationOutcome,
    assess_joint_spec_staleness,
    enumerate_joint_decision_vectors,
    joint_decision_vector_count,
    run_joint_execution,
)
from htdt.cad_joint_optimization import (
    JointDspVariable,
    JointEvaluationInputRef,
    build_joint_optimization_spec,
    canonical_joint_sha256,
)
from htdt.optimization_objectives import (
    ObjectiveMetric,
    ObjectiveVector,
)

from test_cad_joint_optimization import (
    NOW,
    _build_spec,
    _channel,
    _evaluator,
    _fixture,
    _objectives,
    _peq,
    _repository,
    _robustness_ref,
    _save_plan,
)


def _outcome_evaluator(fixture, spec, *, error: float = 1.0):
    """Fixture evaluator mirroring the _evaluation result-record pattern."""

    def evaluate(context) -> JointEvaluationOutcome:
        definitions = {
            item.objective_id: item for item in spec.objectives
        }
        candidate = context.candidate
        vector = ObjectiveVector(
            candidate_id=candidate.candidate_id,
            metrics=(
                ObjectiveMetric(
                    objective_id='fixture.response_error_db',
                    value=error,
                    unit='dB',
                    direction='minimize',
                    definition=definitions[
                        'fixture.response_error_db'
                    ],
                ),
                ObjectiveMetric(
                    objective_id='fixture.headroom_db',
                    value=5.0,
                    unit='dB',
                    direction='maximize',
                    definition=definitions['fixture.headroom_db'],
                ),
            ),
        )
        payload = {
            'candidate_id': candidate.candidate_id,
            'metrics': [
                {
                    'objective_id': metric.objective_id,
                    'value': metric.value,
                    'unit': metric.unit,
                    'direction': metric.direction,
                }
                for metric in vector.metrics
            ],
        }
        record_id = (
            'joint-fixture-result-'
            + canonical_joint_sha256(payload)[:24]
        )
        fixture.joint_results[record_id] = payload
        return JointEvaluationOutcome(
            objective_vector=vector,
            input_refs=(
                JointEvaluationInputRef(
                    evidence_class='predicted',
                    source_kind='joint_fixture_result',
                    source_id=record_id,
                    source_sha256=canonical_joint_sha256(payload),
                ),
            ),
        )

    return evaluate


def _position_only_spec(fixture, **overrides):
    """build_joint_optimization_spec with DSP authority fully absent."""

    return build_joint_optimization_spec(
        scene_revision=fixture.revision,
        base_system_variant=fixture.base_variant,
        physical_search_spec=fixture.search_spec,
        extended_search_spec=None,
        base_calibration_plan=None,
        measurement_quality_report=None,
        dsp_variables=(),
        objectives=_objectives(),
        robustness=_robustness_ref(fixture),
        evaluator=_evaluator(),
        candidate_budget=overrides.get('candidate_budget', 8),
        created_at_utc=NOW,
        spec_id=overrides.get('spec_id', 'joint-spec-exec-position'),
    )


def test_position_execution_persists_candidates_and_evaluations(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    spec = _position_only_spec(fixture, candidate_budget=8)
    repository = _repository(fixture)
    repository.save_spec(spec)

    result = run_joint_execution(
        repository=repository,
        spec=spec,
        baseline=fixture.revision,
        base_variant=fixture.base_variant,
        system_variant_repository=fixture.system_variant_repository,
        evaluator=_outcome_evaluator(fixture, spec),
        created_at_utc=NOW,
    )

    assert result.decision_vectors_total == 2  # x_m grid {1.0, 2.0}
    assert result.candidates_generated == 2
    assert result.evaluations_recorded == 2
    assert result.candidates_blocked == 0
    assert not result.cancelled
    assert result.pareto_candidate_ids != ()

    candidates = repository.list_candidates(spec.spec_id)
    assert len(candidates) == 2
    assert all(
        item.candidate_class == 'position_only' for item in candidates
    )
    evaluations = repository.list_evaluations(spec.spec_id)
    assert len(evaluations) == 2
    # The two candidates materialize distinct exact variants.
    variant_ids = {
        item.physical_system_variant_id for item in candidates
    }
    assert len(variant_ids) == 2


def test_joint_execution_materializes_candidate_calibration_plan(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    base_plan = _save_plan(
        fixture,
        plan_id='base-plan-joint-exec',
        channel=_channel(
            peq=(_peq('peq-1', frequency_hz=100.0, gain_db=0.0),)
        ),
    )
    dsp_variables = (
        JointDspVariable(
            variable_id='dsp:gain',
            channel_id='FL',
            parameter='gain_db',
            minimum=0.0,
            maximum=1.0,
            step=0.5,
            required_measurement_claim='magnitude_response',
            required_band_hz=(20.0, 20000.0),
        ),
    )
    spec = _build_spec(
        fixture,
        base_plan=base_plan,
        dsp_variables=dsp_variables,
        candidate_budget=8,
    )
    repository = _repository(fixture)
    repository.save_spec(spec)

    result = run_joint_execution(
        repository=repository,
        spec=spec,
        baseline=fixture.revision,
        base_variant=fixture.base_variant,
        system_variant_repository=fixture.system_variant_repository,
        calibration_repository=fixture.calibration_repository,
        base_plan=base_plan,
        quality_report=fixture.quality_report,
        evaluator=_outcome_evaluator(fixture, spec),
        created_at_utc=NOW,
    )

    # x_m {1.0, 2.0} x gain {0.0, 0.5, 1.0} = 6 joint candidates.
    assert result.decision_vectors_total == 6
    candidates = repository.list_candidates(spec.spec_id)
    assert len(candidates) == result.candidates_generated + result.candidates_reused
    assert all(
        item.candidate_class == 'joint' for item in candidates
    )
    assert all(
        item.calibration_candidate is not None for item in candidates
    )
    plan_ids = {
        item.calibration_candidate.plan_id for item in candidates
    }
    assert len(plan_ids) == len(candidates)
    assert base_plan.plan_id not in plan_ids


def test_execution_budget_is_never_exceeded(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    spec = _position_only_spec(fixture, candidate_budget=1)
    repository = _repository(fixture)
    repository.save_spec(spec)

    assert joint_decision_vector_count(spec) == 2
    vectors = enumerate_joint_decision_vectors(spec)
    assert len(vectors) == 1

    result = run_joint_execution(
        repository=repository,
        spec=spec,
        baseline=fixture.revision,
        base_variant=fixture.base_variant,
        system_variant_repository=fixture.system_variant_repository,
        evaluator=_outcome_evaluator(fixture, spec),
        created_at_utc=NOW,
    )
    assert result.budget_limited
    assert len(repository.list_candidates(spec.spec_id)) == 1


def test_execution_rerun_reuses_persisted_candidates(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    spec = _position_only_spec(fixture, candidate_budget=8)
    repository = _repository(fixture)
    repository.save_spec(spec)

    first = run_joint_execution(
        repository=repository,
        spec=spec,
        baseline=fixture.revision,
        base_variant=fixture.base_variant,
        system_variant_repository=fixture.system_variant_repository,
        evaluator=_outcome_evaluator(fixture, spec),
        created_at_utc=NOW,
    )
    second = run_joint_execution(
        repository=repository,
        spec=spec,
        baseline=fixture.revision,
        base_variant=fixture.base_variant,
        system_variant_repository=fixture.system_variant_repository,
        evaluator=_outcome_evaluator(fixture, spec),
        created_at_utc=NOW,
    )
    assert first.candidates_generated == 2
    assert second.candidates_generated == 0
    assert second.candidates_reused == 2
    assert len(repository.list_candidates(spec.spec_id)) == 2


def test_execution_fails_closed_on_stale_baseline(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    spec = _position_only_spec(fixture)
    repository = _repository(fixture)
    repository.save_spec(spec)

    moved = fixture.revision.document.model_copy(
        update={
            'room': fixture.revision.document.room.model_copy(
                update={'width_m': 7.0}
            )
        }
    )
    new_revision = fixture.scene_repository.save(
        moved, parent_revision_id=fixture.revision.revision_id
    ).revision
    reasons = assess_joint_spec_staleness(
        spec=spec,
        baseline=new_revision,
        base_variant=fixture.base_variant,
        base_plan=None,
    )
    assert 'scene_revision_changed' in reasons
    assert 'scene_content_changed' in reasons

    with pytest.raises(ValueError, match='stale'):
        run_joint_execution(
            repository=repository,
            spec=spec,
            baseline=new_revision,
            base_variant=fixture.base_variant,
        system_variant_repository=fixture.system_variant_repository,
            created_at_utc=NOW,
        )
    assert repository.list_candidates(spec.spec_id) == ()


def test_execution_cancellation_preserves_partial_progress(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    spec = _position_only_spec(fixture, candidate_budget=8)
    repository = _repository(fixture)
    repository.save_spec(spec)

    calls = {'count': 0}

    def cancel_after_first() -> bool:
        calls['count'] += 1
        return calls['count'] > 1

    result = run_joint_execution(
        repository=repository,
        spec=spec,
        baseline=fixture.revision,
        base_variant=fixture.base_variant,
        system_variant_repository=fixture.system_variant_repository,
        evaluator=_outcome_evaluator(fixture, spec),
        is_cancelled=cancel_after_first,
        created_at_utc=NOW,
    )
    assert result.cancelled
    assert result.candidates_generated == 1
    assert len(repository.list_candidates(spec.spec_id)) == 1


def test_execution_requires_exact_dsp_baseline(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    other_plan = _save_plan(
        fixture,
        plan_id='other-plan-joint-exec',
        channel=_channel(gain_db=2.0),
    )
    spec = _build_spec(fixture)  # dsp_variables fixture default
    repository = _repository(fixture)
    repository.save_spec(spec)

    reasons = assess_joint_spec_staleness(
        spec=spec,
        baseline=fixture.revision,
        base_variant=fixture.base_variant,
        base_plan=other_plan,
    )
    assert 'base_calibration_plan_changed' in reasons
    with pytest.raises(ValueError, match='stale'):
        run_joint_execution(
            repository=repository,
            spec=spec,
            baseline=fixture.revision,
            base_variant=fixture.base_variant,
        system_variant_repository=fixture.system_variant_repository,
            base_plan=other_plan,
            quality_report=fixture.quality_report,
            created_at_utc=NOW,
        )
