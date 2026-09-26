"""Optimize-side context tests for #524 joint placement+DSP authoring."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from htdt.cad_joint_optimization import JointDspVariable
from htdt.joint_optimization_context import JointOptimizationContext

from test_cad_joint_optimization import (  # noqa: E402  (shared fixtures)
    DOCUMENT_ID,
    _dsp_variables,
    _fixture,
)


def _context(fixture, tmp_path: Path) -> JointOptimizationContext:
    return JointOptimizationContext(
        fixture.scene_repository,
        DOCUMENT_ID,
        objective_repository=fixture.objective_repository,
    )


def test_resolve_baseline_binds_exact_authorities(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    baseline = _context(fixture, tmp_path).resolve_baseline()

    assert baseline is not None
    assert baseline.scene_revision.revision_id == fixture.revision.revision_id
    assert (
        baseline.base_variant.baseline_revision_id
        == fixture.revision.revision_id
    )
    assert (
        baseline.physical_search_spec.search_spec_id
        == fixture.search_spec.search_spec_id
    )
    assert baseline.calibration_plan is not None
    assert baseline.calibration_plan.plan_id == fixture.base_plan.plan_id
    assert baseline.quality_report is not None
    assert baseline.quality_report.report_id == fixture.quality_report.report_id
    assert baseline.robustness_spec is not None
    assert baseline.objective_definitions


def test_dsp_variable_options_gate_on_quality_claims(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    context = _context(fixture, tmp_path)
    baseline = context.resolve_baseline()
    options = {item.parameter: item for item in context.dsp_variable_options(baseline)}

    delay = options['delay_s']
    assert delay.required_claim_ja == '共通タイミング'
    polarity = options['polarity']
    assert polarity.required_claim_ja == '極性'
    gain = options['gain_db']
    assert gain.required_claim_ja.startswith('振幅応答')
    for option in options.values():
        assert option.enabled == (option.decision == 'ALLOWED')
        if not option.enabled:
            assert option.reason_ja

    # Without a quality report every variable fails closed to UNKNOWN.
    no_report = replace(baseline, quality_report=None)
    for option in context.dsp_variable_options(no_report):
        assert option.decision == 'UNKNOWN'
        assert not option.enabled


def test_candidate_preflight_counts_and_budget(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    context = _context(fixture, tmp_path)
    baseline = context.resolve_baseline()

    dsp = _dsp_variables()
    joint = context.estimate_candidates(
        baseline, mode='joint', dsp_variables=dsp, candidate_budget=64
    )
    placement = context.estimate_candidates(
        baseline, mode='placement_only', dsp_variables=(), candidate_budget=64
    )
    assert placement.dsp_candidate_count == 1
    assert placement.physical_candidate_count == joint.physical_candidate_count
    assert joint.dsp_candidate_count > 1
    assert joint.combined_candidate_count == (
        joint.physical_candidate_count * joint.dsp_candidate_count
    )

    tight = context.estimate_candidates(
        baseline, mode='joint', dsp_variables=dsp, candidate_budget=1
    )
    assert not tight.within_budget


def test_create_spec_persists_placement_only_and_joint(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    context = _context(fixture, tmp_path)
    baseline = context.resolve_baseline()

    spec = context.create_spec(
        baseline=baseline,
        mode='placement_only',
        dsp_variables=(),
        candidate_budget=32,
    )
    assert spec.dsp_variables == ()
    assert spec.dsp_authority is None
    assert spec.semantic_sha256

    dsp_vars = _dsp_variables()
    joint = context.create_spec(
        baseline=baseline,
        mode='joint',
        dsp_variables=dsp_vars,
        candidate_budget=64,
    )
    assert joint.dsp_authority is not None
    assert (
        joint.dsp_authority.base_calibration_plan_id
        == fixture.base_plan.plan_id
    )
    assert {item.parameter for item in joint.dsp_variables} == {
        item.parameter for item in dsp_vars
    }

    ids = {item.spec_id for item in context.list_specs()}
    assert ids == {spec.spec_id, joint.spec_id}


def test_create_spec_mode_requires_dsp_variables(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    context = _context(fixture, tmp_path)
    baseline = context.resolve_baseline()

    with pytest.raises(ValueError, match='DSP'):
        context.create_spec(
            baseline=baseline,
            mode='dsp_only',
            dsp_variables=(),
            candidate_budget=8,
        )


def test_create_spec_fails_closed_without_robustness_or_objectives(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    context = _context(fixture, tmp_path)
    baseline = context.resolve_baseline()

    with pytest.raises(ValueError, match='RobustnessSpec'):
        context.create_spec(
            baseline=replace(baseline, robustness_spec=None),
            mode='placement_only',
            dsp_variables=(),
            candidate_budget=8,
        )


def test_execute_spec_runs_persisted_spec_end_to_end(tmp_path: Path) -> None:
    """#945: the native context executes a saved spec, not just authors it."""
    fixture = _fixture(tmp_path)
    context = _context(fixture, tmp_path)
    baseline = context.resolve_baseline()
    spec = context.create_spec(
        baseline=baseline,
        mode='placement_only',
        dsp_variables=(),
        candidate_budget=8,
    )

    result = context.execute_spec(spec.spec_id)

    assert result.parent_spec_id == spec.spec_id
    assert result.parent_spec_sha256 == spec.semantic_sha256
    # x_m grid {1.0, 2.0} over the fixture speaker axis.
    assert result.decision_vectors_total == 2
    assert result.candidates_generated == 2
    assert result.evaluations_recorded == 2
    assert not result.cancelled

    candidates = context.joint_repository.list_candidates(spec.spec_id)
    assert len(candidates) == 2
    assert all(
        item.candidate_class == 'position_only' for item in candidates
    )
    evaluations = context.joint_repository.list_evaluations(spec.spec_id)
    assert len(evaluations) == 2
    # No evaluator is wired in the native lane: the canonical unsupported
    # vector is persisted instead of fabricated prediction numbers.
    assert all(
        metric.state == 'unsupported'
        for evaluation in evaluations
        for metric in evaluation.objective_vector.metrics
    )


def test_execute_spec_materializes_dsp_candidate_plans(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    context = _context(fixture, tmp_path)
    baseline = context.resolve_baseline()
    dsp = (
        JointDspVariable(
            variable_id='dsp:FL:gain_db',
            channel_id='FL',
            parameter='gain_db',
            minimum=0.0,
            maximum=1.0,
            step=0.5,
            required_measurement_claim='magnitude_response',
            required_band_hz=(20.0, 20000.0),
        ),
    )
    spec = context.create_spec(
        baseline=baseline,
        mode='joint',
        dsp_variables=dsp,
        candidate_budget=8,
    )

    result = context.execute_spec(spec.spec_id)

    # x_m {1.0, 2.0} x gain {0.0, 0.5, 1.0} = 6 joint candidates.
    assert result.decision_vectors_total == 6
    candidates = context.joint_repository.list_candidates(spec.spec_id)
    assert len(candidates) == result.candidates_generated + result.candidates_reused
    assert all(item.candidate_class == 'joint' for item in candidates)
    plan_ids = {
        item.calibration_candidate.plan_id
        for item in candidates
        if item.calibration_candidate is not None
    }
    assert len(plan_ids) == len(candidates)
    assert fixture.base_plan.plan_id not in plan_ids


def test_execute_spec_rerun_reuses_persisted_candidates(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    context = _context(fixture, tmp_path)
    baseline = context.resolve_baseline()
    spec = context.create_spec(
        baseline=baseline,
        mode='placement_only',
        dsp_variables=(),
        candidate_budget=8,
    )

    first = context.execute_spec(spec.spec_id)
    second = context.execute_spec(spec.spec_id)

    assert first.candidates_generated == 2
    assert second.candidates_generated == 0
    assert second.candidates_reused == 2


def test_execute_spec_fails_closed_on_stale_baseline(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    context = _context(fixture, tmp_path)
    baseline = context.resolve_baseline()
    spec = context.create_spec(
        baseline=baseline,
        mode='placement_only',
        dsp_variables=(),
        candidate_budget=8,
    )

    moved = fixture.revision.document.model_copy(
        update={
            'room': fixture.revision.document.room.model_copy(
                update={'width_m': 7.0}
            )
        }
    )
    fixture.scene_repository.save(
        moved, parent_revision_id=fixture.revision.revision_id
    )

    assert context.assess_spec_staleness(spec.spec_id) != ()
    with pytest.raises(ValueError):
        context.execute_spec(spec.spec_id)
    assert context.joint_repository.list_candidates(spec.spec_id) == ()


def test_assess_spec_staleness_tracks_authority_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    context = _context(fixture, tmp_path)
    baseline = context.resolve_baseline()
    spec = context.create_spec(
        baseline=baseline,
        mode='placement_only',
        dsp_variables=(),
        candidate_budget=8,
    )

    assert context.assess_spec_staleness(spec.spec_id) == ()

    with pytest.raises(ValueError, match='not persisted'):
        context.assess_spec_staleness('joint-spec-missing')
    with pytest.raises(ValueError, match='not persisted'):
        context.execute_spec('joint-spec-missing')
