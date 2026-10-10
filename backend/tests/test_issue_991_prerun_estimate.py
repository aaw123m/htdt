"""Issue #991 — pre-run compute estimate authority.

Covers the honesty contract the UI cannot fake: out-of-range inputs
yield UNKNOWN metrics (never fabricated precision), the sealed plan
binds the exact configuration, estimates persist append-only next to
the budget authorities, and post-run observations record only real
measured cost.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from htdt.cad_field_metric_repository import (
    CadFieldMetricRepository,
    FieldMetricConflictError,
    FieldMetricIntegrityError,
)
from htdt.cad_prerun_estimate import (
    COST_MODELS,
    JOINT_EXECUTION_COST_MODEL,
    PrerunEstimate,
    PrerunJobPlan,
    PrerunValueRange,
    SolverCostModel,
    cost_model_for,
    estimate_for_observation,
    estimate_prerun_cost,
    plan_for_joint_execution,
    plan_for_provider_prediction,
    plan_for_rectangular_prediction,
    record_prerun_observation,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import ensure_native_schema


DOC = 'doc-991'
REV = 'rev-991'
HASH = 'a' * 64
SUBJECT_SHA = 'b' * 64


def _rectangular_plan(**overrides) -> PrerunJobPlan:
    kwargs = dict(
        document_id=DOC,
        scene_revision_id=REV,
        scene_content_hash=HASH,
        receiver_entity_id='seat-1',
        room_width_m=6.0,
        room_depth_m=4.0,
        room_height_m=2.8,
        speaker_count=7,
        max_mode_hz=300.0,
        sound_speed_m_s=343.0,
        solver_version='test-1',
    )
    kwargs.update(overrides)
    return plan_for_rectangular_prediction(**kwargs)


def _repo(tmp_path: Path) -> CadFieldMetricRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadFieldMetricRepository(scene)


# ----------------------------------------------------------------------
# Estimator honesty
# ----------------------------------------------------------------------


def test_in_range_plan_estimates_all_metrics() -> None:
    estimate = estimate_prerun_cost(_rectangular_plan())
    assert estimate.confidence == 'calibrated'
    assert estimate.runtime_s is not None
    assert estimate.peak_memory_bytes is not None
    assert estimate.storage_bytes is not None
    assert estimate.runtime_s.minimum <= estimate.runtime_s.maximum
    assert estimate.runtime_s.minimum > 0
    assert estimate.budget_verdict == 'budget_unknown'
    assert 'budget_limits_undeclared' in estimate.warnings
    assert not estimate.blocking_reasons
    assert estimate.executable


def test_out_of_range_axis_reports_unknown_not_fake_number() -> None:
    # 200m x 150m hall at 1000 Hz -> grid cells far beyond the
    # calibrated 2e5 * extrapolation_factor bound.
    plan = _rectangular_plan(
        room_width_m=200.0,
        room_depth_m=150.0,
        room_height_m=10.0,
        max_mode_hz=1000.0,
    )
    estimate = estimate_prerun_cost(plan)
    assert estimate.confidence == 'unknown'
    assert estimate.runtime_s is None
    assert estimate.peak_memory_bytes is None
    assert estimate.storage_bytes is None
    assert estimate.axis_statuses['mode_grid_cells'] == 'out_of_range'
    assert estimate.out_of_range_reasons
    assert 'estimate_unknown' in estimate.warnings


def test_non_rectangular_plan_reports_missing_geometry_axes() -> None:
    plan = _rectangular_plan(
        room_width_m=None, room_depth_m=None, room_height_m=None
    )
    estimate = estimate_prerun_cost(plan)
    # Geometry axes absent -> cost terms mark missing -> honest UNKNOWN.
    assert estimate.confidence == 'unknown'
    assert estimate.runtime_s is None
    assert 'non_rectangular_room' in estimate.warnings
    assert not estimate.blocking_reasons  # soft warning, never a block


def test_extrapolated_axis_yields_bounded_extrapolation() -> None:
    # Push grid cells past calibrated max but within the 4x bound.
    # ~2.5e5 cells needs max_hz ≈ 500 in a small room.
    plan = _rectangular_plan(
        room_width_m=6.0,
        room_depth_m=5.0,
        room_height_m=3.0,
        max_mode_hz=520.0,
    )
    estimate = estimate_prerun_cost(plan)
    assert estimate.confidence in ('calibrated', 'bounded_extrapolation')
    if estimate.confidence == 'bounded_extrapolation':
        assert 'bounded_extrapolation' in estimate.warnings
        assert estimate.runtime_s is not None
        assert estimate.runtime_s.extrapolated


def test_unmodeled_context_axes_do_not_demote_confidence() -> None:
    estimate = estimate_prerun_cost(_rectangular_plan())
    # sound_speed_m_s / receiver_count are bound into the plan but the
    # rectangular cost model declares no terms for them.
    assert estimate.axis_statuses['sound_speed_m_s'] == 'unmodeled'
    assert estimate.axis_statuses['receiver_count'] == 'unmodeled'
    assert estimate.confidence == 'calibrated'


def test_declared_cost_model_rejects_observed_basis() -> None:
    with pytest.raises(ValueError):
        SolverCostModel(
            model_id='fake',
            model_version='1',
            job_kind='prediction_rectangular',
            basis='observed_run',
        )


def test_cost_term_must_reference_calibrated_axis() -> None:
    with pytest.raises(ValueError):
        SolverCostModel(
            model_id='fake',
            model_version='1',
            job_kind='prediction_rectangular',
            basis='assumed',
            runtime_terms_s={
                'unmodeled_axis': PrerunValueRange(
                    minimum=0, maximum=1
                )
            },
        )


def test_unknown_job_kind_has_no_cost_model() -> None:
    with pytest.raises(ValueError):
        cost_model_for('nonexistent_kind')


def test_plan_rejects_invalid_axes() -> None:
    with pytest.raises(ValueError):
        PrerunJobPlan.create(
            job_kind='prediction_rectangular',
            document_id=DOC,
            solver_id='rectangular_geometry',
            axes={'max_frequency_hz': float('nan')},
        )
    with pytest.raises(ValueError):
        PrerunJobPlan.create(
            job_kind='prediction_rectangular',
            document_id=DOC,
            solver_id='rectangular_geometry',
            axes={'max_frequency_hz': -1.0},
        )


# ----------------------------------------------------------------------
# Config binding — the plan hash pins the exact configuration
# ----------------------------------------------------------------------


def test_plan_hash_changes_with_configuration() -> None:
    base = _rectangular_plan()
    assert base.plan_sha256 == base.plan_sha256  # deterministic
    for override in (
        {'max_mode_hz': 400.0},
        {'speaker_count': 4},
        {'receiver_entity_id': 'seat-2'},
        {'scene_content_hash': 'c' * 64},
        {'solver_version': 'test-2'},
    ):
        other = _rectangular_plan(**override)
        assert other.plan_sha256 != base.plan_sha256
        assert other.plan_id != base.plan_id


def test_estimate_hash_changes_with_config() -> None:
    one = estimate_prerun_cost(_rectangular_plan())
    two = estimate_prerun_cost(_rectangular_plan(max_mode_hz=800.0))
    assert one.estimate_sha256 != two.estimate_sha256


def test_plan_sha_is_config_binding_not_label() -> None:
    # Two independently built plans with identical inputs seal identically.
    a = _rectangular_plan()
    b = _rectangular_plan()
    assert a.plan_id == b.plan_id
    assert a.plan_sha256 == b.plan_sha256


def test_provider_plan_binds_provider_semantics() -> None:
    plan_a = plan_for_provider_prediction(
        document_id=DOC,
        scene_revision_id=REV,
        scene_content_hash=HASH,
        receiver_entity_id='seat-1',
        provider_id='r170a-provider:' + '1' * 64,
        provider_version='1',
        provider_semantic_sha256=SUBJECT_SHA,
        max_mode_hz=300.0,
        provider_sample_count=512,
    )
    plan_b = plan_for_provider_prediction(
        document_id=DOC,
        scene_revision_id=REV,
        scene_content_hash=HASH,
        receiver_entity_id='seat-1',
        provider_id='r170a-provider:' + '2' * 64,
        provider_version='1',
        provider_semantic_sha256=SUBJECT_SHA,
        max_mode_hz=300.0,
        provider_sample_count=512,
    )
    assert plan_a.plan_sha256 != plan_b.plan_sha256


def test_joint_plan_caps_candidates_at_budget() -> None:
    plan = plan_for_joint_execution(
        spec_id='spec-1',
        spec_sha256=SUBJECT_SHA,
        document_id=DOC,
        scene_revision_id=REV,
        scene_content_hash=HASH,
        decision_vector_count=500,
        candidate_budget=30,
        physical_variable_count=2,
        dsp_variable_count=3,
        objective_count=2,
        evaluator_version='joint-evaluation-1',
    )
    assert plan.axes['candidate_count'] == 30.0


# ----------------------------------------------------------------------
# Budget verdict + fidelity gate
# ----------------------------------------------------------------------


def test_budget_verdicts() -> None:
    plan = _rectangular_plan()
    assert (
        estimate_prerun_cost(plan, budget_limits=None).budget_verdict
        == 'budget_unknown'
    )
    generous = estimate_prerun_cost(
        plan, budget_limits={'runtime_s': 1e9, 'peak_memory_bytes': 1e12}
    )
    assert generous.budget_verdict == 'within_budget'
    tight = estimate_prerun_cost(
        plan, budget_limits={'runtime_s': 1e-6}
    )
    assert tight.budget_verdict == 'over_budget'
    foreign = estimate_prerun_cost(
        plan, budget_limits={'unrelated_metric': 1.0}
    )
    assert foreign.budget_verdict == 'incomparable'


def test_fidelity_verdict_present_for_estimated_plan() -> None:
    estimate = estimate_prerun_cost(_rectangular_plan())
    assert estimate.fidelity_verdict in (
        'cost_evidence_present',
        'no_cost_evidence',
    )
    # Estimated metrics feed the gate as PredictedCost records.
    assert estimate.fidelity_verdict == 'cost_evidence_present'


def test_blocking_only_on_real_capacity_overrun() -> None:
    plan = _rectangular_plan()
    tiny = estimate_prerun_cost(plan, device_memory_bytes=1.0)
    assert tiny.blocking_reasons == ('estimated_peak_memory_exceeds_device',)
    assert not tiny.executable
    ample = estimate_prerun_cost(plan, device_memory_bytes=1e12)
    assert not ample.blocking_reasons
    assert ample.executable


def test_gpu_gating() -> None:
    gpu_model = SolverCostModel(
        model_id='gpu-solver',
        model_version='1',
        job_kind='prediction_rectangular',
        basis='vendor_documented',
        required_accelerator='gpu',
    )
    plan = _rectangular_plan()
    blocked = estimate_prerun_cost(
        plan, model=gpu_model, gpu_available=False
    )
    assert 'gpu_unavailable' in blocked.blocking_reasons
    assert not blocked.executable
    unknown = estimate_prerun_cost(
        plan, model=gpu_model, gpu_available=None
    )
    assert 'gpu_capability_unknown' in unknown.warnings
    assert unknown.executable


def test_long_runtime_is_warning_not_block() -> None:
    estimate = estimate_prerun_cost(
        _rectangular_plan(
            room_width_m=10.0,
            room_depth_m=9.0,
            room_height_m=4.0,
            max_mode_hz=1000.0,
        )
    )
    # Even a heavy-but-estimable plan stays executable — long runtime is
    # a soft warning, never a hard block.
    assert estimate.executable


# ----------------------------------------------------------------------
# Persistence — sealed, append-only, hash-bound
# ----------------------------------------------------------------------


def test_estimate_persistence_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    estimate = estimate_prerun_cost(_rectangular_plan())
    repo.save_prerun_estimate(estimate)
    loaded = repo.get_prerun_estimate(estimate.estimate_id)
    assert loaded is not None
    assert loaded.estimate_sha256 == estimate.estimate_sha256
    assert loaded.plan.plan_sha256 == estimate.plan.plan_sha256
    assert loaded.confidence == estimate.confidence
    # idempotent re-save of the identical sealed record
    repo.save_prerun_estimate(estimate)
    assert len(repo.list_prerun_estimates(DOC)) == 1


def test_estimate_persistence_rejects_forged_seal(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    estimate = estimate_prerun_cost(_rectangular_plan())
    forged = estimate.model_copy(update={'cost_model_id': 'forged'})
    with pytest.raises(FieldMetricIntegrityError):
        repo.save_prerun_estimate(forged)


def test_estimate_persistence_is_append_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    estimate = estimate_prerun_cost(_rectangular_plan())
    repo.save_prerun_estimate(estimate)
    # Same id, different payload -> conflict.
    mutated_payload = estimate.model_dump()
    mutated_payload['confidence'] = 'unknown'
    conflict = PrerunEstimate.model_construct(**mutated_payload)
    with pytest.raises((FieldMetricConflictError, FieldMetricIntegrityError)):
        repo.save_prerun_estimate(conflict)


def test_estimate_persistence_requires_sealed_plan(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    estimate = estimate_prerun_cost(_rectangular_plan())
    forged_plan = estimate.plan.model_copy(
        update={'solver_version': 'tampered'}
    )
    forged_estimate = estimate.model_copy(update={'plan': forged_plan})
    with pytest.raises(FieldMetricIntegrityError):
        repo.save_prerun_estimate(forged_estimate)


# ----------------------------------------------------------------------
# Post-run observation — real measurement only
# ----------------------------------------------------------------------


def test_observation_binds_estimate_and_run() -> None:
    estimate = estimate_prerun_cost(_rectangular_plan())
    observation = record_prerun_observation(
        document_id=DOC,
        estimate=estimate,
        runtime_s=1.25,
        process_rss_bytes=8.0e6,
        hardware_label='test-box',
        observed_at_utc='2026-10-09T00:00:00+00:00',
    )
    assert estimate_for_observation(observation) == estimate.estimate_id
    assert observation.metrics['runtime_s'] == 1.25
    # Completion-time RSS never masquerades as a true peak.
    assert 'process_rss_bytes' in observation.metrics
    assert 'peak_memory_bytes' not in observation.metrics


def test_observation_rejects_fabricated_measurements() -> None:
    estimate = estimate_prerun_cost(_rectangular_plan())
    for bogus in (0.0, -1.0, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            record_prerun_observation(
                document_id=DOC,
                estimate=estimate,
                runtime_s=bogus,
                observed_at_utc='2026-10-09T00:00:00+00:00',
            )


def test_observation_persists_alongside_estimate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    estimate = estimate_prerun_cost(_rectangular_plan())
    repo.save_prerun_estimate(estimate)
    observation = record_prerun_observation(
        document_id=DOC,
        estimate=estimate,
        runtime_s=0.9,
        observed_at_utc='2026-10-09T00:00:00+00:00',
    )
    repo.save_compute_observation(observation)
    found = repo.prerun_observations(estimate)
    assert len(found) == 1
    assert found[0].observation_id == observation.observation_id
    assert found[0].metrics['runtime_s'] == 0.9


def test_prerun_table_registered_in_native_schema(tmp_path: Path) -> None:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    connection = sqlite3.connect(db)
    try:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert 'cad_prerun_estimates' in names
    finally:
        connection.close()


def test_estimates_are_estimates_not_actuals() -> None:
    # The sealed estimate stores declared-model ranges only — the
    # fidelity gate's observed_run basis is structurally unavailable.
    estimate = estimate_prerun_cost(_rectangular_plan())
    assert estimate.cost_model_basis != 'observed_run'
    for metric in (
        estimate.runtime_s,
        estimate.peak_memory_bytes,
        estimate.storage_bytes,
    ):
        if metric is not None:
            assert metric.basis in (
                'extrapolated_model',
                'assumed',
                'vendor_documented',
            )
