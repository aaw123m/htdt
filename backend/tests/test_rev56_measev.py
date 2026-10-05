"""REV56-MEASEV regression tests (#572/#573/#575).

Covers the three measurement-evidence authorities:

- ``MeasurementUncertaintyBudget`` (#572): contributor provenance classes
  (assumed bounds never become calibration evidence), GUM Type A/B
  semantics, scalar + spectral combination (RSS with declared
  correlation groups, bounded worst-case, GUM-S1 Monte-Carlo with
  declared assignments), metrological traceability chains with explicit
  break marking, calibrator pre/post drift checks, and the residual /
  delta significance verdicts consumed by #564/#566.
- ``MeasurementStateSnapshot`` + ``StateControlPolicy`` +
  ``StateComparabilityVerdict`` (#573): capture-time state evidence,
  fail-closed comparability gate (environment drift hits phase/time
  before magnitude; dynamic DSP / limiter engagement is contamination),
  bounded repeat-sequence stationarity.
- ``MeasurementTransform`` (#575): the transformation DAG — declared
  capability effects that may only downgrade, distinct averaging
  semantics, alignment/clock authorities that never upgrade estimated
  evidence to exact, domain-validated arithmetic, and the honest
  FR-domain executor.
"""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from math import sqrt
from pathlib import Path

import pytest

from htdt.cad_measurement_evidence_repository import (
    CadMeasurementEvidenceRepository,
    MeasurementEvidenceConflictError,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_state import (
    DeviceDynamicState,
    DynamicRequirement,
    EnvironmentObservation,
    MeasurementStateSnapshot,
    NoiseRegime,
    OpeningObservation,
    OpeningRequirement,
    StateComparabilityVerdict,
    StateControlPolicy,
    build_state_control_policy,
    build_state_snapshot,
    evaluate_sequence_stationarity,
    evaluate_state_comparability,
)
from htdt.cad_measurement_transform import (
    AlignmentSpec,
    ArithmeticSpec,
    ClockCorrectionSpec,
    InputDelayGain,
    MeasurementTransform,
    TransformInput,
    TransformParameters,
    apply_transform,
    build_measurement_transform,
)
from htdt.cad_measurement_uncertainty import (
    CalibratorCheck,
    CalibrationChainLink,
    MeasurementUncertaintyBudget,
    UncertaintyContributor,
    UncertaintySpectrumPoint,
    assess_measurement_delta,
    assess_residual_report,
    build_measurement_uncertainty_budget,
    classify_residual,
    evaluate_calibrator_drift,
    evaluate_delta_significance,
    significance_threshold,
)
from htdt.cad_prediction_measurement_registration import (
    BandMagnitudeResidual,
    PredictionMeasurementResidualReport,
    ResidualObservable,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.canonical_json import canonical_sha256
from htdt.native_authority_audit import audit_table_modes

NOW = '2026-10-05T00:00:00+00:00'
DOC = 'doc-rev56'


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _dataset(
    dataset_id: str,
    levels: tuple[float, ...] | list[float],
    *,
    frequencies: tuple[float, ...] | list[float] = (20.0, 40.0, 80.0, 160.0),
    phases: tuple[float, ...] | list[float] | None = None,
    phase_status: str = 'absent',
    level_reference: str = 'unknown',
    measurement_id: str = 'meas-1',
) -> CadFrequencyResponseDataset:
    return CadFrequencyResponseDataset(
        dataset_id=dataset_id,
        measurement_id=measurement_id,
        frequency_hz=tuple(frequencies),
        level_db=tuple(levels),
        phase_deg=None if phases is None else tuple(phases),
        phase_status=phase_status,  # type: ignore[arg-type]
        level_reference=level_reference,
        source_sha256=_hash(f'source:{dataset_id}'),
        importer_version='test-import-1',
    )


def _repository(tmp_path: Path) -> CadMeasurementEvidenceRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_f1_scene(), parent_revision_id=None)
    return CadMeasurementEvidenceRepository(scene_repository)


def _contributor(
    contributor_id: str = 'c1',
    *,
    kind: str = 'instrument_calibration',
    provenance: str = 'calibration_certificate',
    evaluation: str = 'type_b',
    representation: str = 'standard_uncertainty',
    standard_uncertainty: float | None = 0.3,
    bound_low: float | None = None,
    bound_high: float | None = None,
    empirical_samples: tuple[float, ...] | None = None,
    spectral_points: tuple | None = None,
    sensitivity: float | None = None,
    correlation_group_id: str | None = None,
    evidence_refs: tuple[str, ...] = ('cert:lab-1',),
) -> UncertaintyContributor:
    return UncertaintyContributor(
        contributor_id=contributor_id,
        kind=kind,  # type: ignore[arg-type]
        provenance_class=provenance,  # type: ignore[arg-type]
        evaluation_kind=evaluation,  # type: ignore[arg-type]
        subject_quantity='level_db',
        quantity_unit='dB',
        representation=representation,  # type: ignore[arg-type]
        bound_low=bound_low,
        bound_high=bound_high,
        standard_uncertainty=standard_uncertainty,
        empirical_samples=empirical_samples,
        spectral_points=spectral_points,
        sensitivity=sensitivity,
        correlation_group_id=correlation_group_id,
        evidence_refs=evidence_refs,
    )


def _budget(
    *,
    contributors: tuple[UncertaintyContributor, ...] | None = None,
    propagation_method: str = 'first_order_linear',
    traceability_class: str = 'untraceable',
    traceability_links: tuple[CalibrationChainLink, ...] = (),
    coverage_factor: float | None = 2.0,
    measurement_id: str = 'meas-1',
    observable_kind: str = 'magnitude_db',
    **kwargs,
) -> MeasurementUncertaintyBudget:
    return build_measurement_uncertainty_budget(
        document_id=DOC,
        measurand='frequency response level',
        measurand_unit='dB',
        observable_kind=observable_kind,  # type: ignore[arg-type]
        measurement_id=measurement_id,
        contributors=contributors
        if contributors is not None
        else (_contributor(),),
        traceability_links=traceability_links,
        traceability_class=traceability_class,  # type: ignore[arg-type]
        propagation_method=propagation_method,  # type: ignore[arg-type]
        coverage_factor=coverage_factor,
        created_at_utc=NOW,
        **kwargs,
    )


# =========================================================================
# #572 — uncertainty budget construction & sealing
# =========================================================================


def test_mub10_budget_seals_and_persists(tmp_path: Path) -> None:
    budget = _budget()
    assert budget.budget_id == f'mub:{budget.semantic_sha256}'
    assert budget.outcome.combination_state == 'propagated'
    repository = _repository(tmp_path)
    repository.save_uncertainty_budget(budget)
    loaded = repository.get_uncertainty_budget(budget.budget_id)
    assert loaded == budget
    assert repository.list_uncertainty_budgets(DOC) == (budget,)
    assert repository.uncertainty_budgets_for_measurement('meas-1') == (
        budget,
    )


def test_mub_budget_requires_subject_binding() -> None:
    with pytest.raises(ValueError, match='bind'):
        _budget(contributors=(_contributor(),), measurement_id=None)


def test_mub_budget_requires_contributors() -> None:
    with pytest.raises(ValueError, match='contributor'):
        _budget(contributors=())


def test_mub20_type_a_requires_empirical_samples() -> None:
    with pytest.raises(ValueError, match='type_a'):
        _contributor(
            evaluation='type_a',
            representation='standard_uncertainty',
        )


def test_mub_assumed_bound_never_promoted() -> None:
    with pytest.raises(ValueError, match='assumed bound'):
        _contributor(
            provenance='assumed_bound',
            representation='standard_uncertainty',
            evaluation='type_b',
        )
    contributor = _contributor(
        provenance='assumed_bound',
        representation='bounded_interval',
        bound_low=-0.5,
        bound_high=0.5,
        standard_uncertainty=None,
        evidence_refs=(),
    )
    assert contributor.provenance_class == 'assumed_bound'


def test_mub_calibration_certificate_requires_evidence() -> None:
    with pytest.raises(ValueError, match='certificate'):
        _contributor(
            provenance='calibration_certificate',
            evidence_refs=(),
        )


def test_mub_type_a_repeatability_requires_captures() -> None:
    contributor = _contributor(
        kind='repeatability',
        provenance='repeatability_empirical',
        evaluation='type_a',
        representation='empirical_samples',
        standard_uncertainty=None,
        empirical_samples=(0.1, -0.2, 0.05),
        evidence_refs=(),
    )
    with pytest.raises(ValueError, match='repeat'):
        _budget(contributors=(contributor,))
    budget = _budget(
        contributors=(contributor,),
        repeatability_measurement_ids=('meas-1', 'meas-2'),
    )
    assert budget.budget_id.startswith('mub:')


# =========================================================================
# #572 — numeric combination
# =========================================================================


def test_mub_rss_combination_numeric() -> None:
    budget = _budget(
        contributors=(
            _contributor('c1', standard_uncertainty=0.3),
            _contributor('c2', standard_uncertainty=0.4),
        ),
    )
    assert budget.outcome.combined_standard_uncertainty == pytest.approx(0.5)
    assert budget.outcome.expanded_uncertainty == pytest.approx(1.0)


def test_mub_declared_correlation_sums_linearly() -> None:
    grouped = _budget(
        contributors=(
            _contributor('c1', standard_uncertainty=0.3, correlation_group_id='g'),
            _contributor('c2', standard_uncertainty=0.3, correlation_group_id='g'),
        ),
        coverage_factor=None,
    )
    independent = _budget(
        contributors=(
            _contributor('c1', standard_uncertainty=0.3),
            _contributor('c2', standard_uncertainty=0.3),
        ),
        coverage_factor=None,
    )
    assert grouped.outcome.combined_standard_uncertainty == pytest.approx(0.6)
    assert independent.outcome.combined_standard_uncertainty == pytest.approx(
        sqrt(0.18)
    )


def test_mub_bounded_worst_case_linear_sum() -> None:
    budget = _budget(
        propagation_method='bounded_worst_case',
        coverage_factor=None,
        contributors=(
            _contributor(
                'c1',
                representation='bounded_interval',
                standard_uncertainty=None,
                bound_low=-0.2,
                bound_high=0.4,
                provenance='manufacturer_spec',
            ),
            _contributor(
                'c2',
                representation='bounded_interval',
                standard_uncertainty=None,
                bound_low=-0.1,
                bound_high=0.1,
                provenance='manufacturer_spec',
            ),
        ),
    )
    assert budget.outcome.combined_bound_half_width == pytest.approx(0.4)


def test_mub_monte_carlo_declared_assignments() -> None:
    budget = _budget(
        propagation_method='monte_carlo',
        monte_carlo_seed=7,
        monte_carlo_sample_count=4000,
        coverage_factor=2.0,
        contributors=(
            _contributor('c1', standard_uncertainty=0.3),
            _contributor('c2', standard_uncertainty=0.4),
        ),
    )
    assert budget.outcome.combination_state == 'propagated'
    assert budget.outcome.combined_standard_uncertainty == pytest.approx(
        0.5, rel=0.05
    )
    assert budget.outcome.expanded_uncertainty == pytest.approx(1.0, rel=0.05)
    assert any(
        'monte_carlo sampling assignments' in line
        for line in budget.limitations
    )
    # reproducibility: identical seed → identical result
    again = _budget(
        propagation_method='monte_carlo',
        monte_carlo_seed=7,
        monte_carlo_sample_count=4000,
        coverage_factor=2.0,
        contributors=(
            _contributor('c1', standard_uncertainty=0.3),
            _contributor('c2', standard_uncertainty=0.4),
        ),
    )
    assert again.semantic_sha256 == budget.semantic_sha256


def test_mub_monte_carlo_requires_seed_and_count() -> None:
    with pytest.raises(ValueError, match='monte_carlo'):
        _budget(
            propagation_method='monte_carlo',
            contributors=(_contributor(),),
        )


def test_mub_declared_only_never_fabricates_combination() -> None:
    budget = _budget(
        propagation_method='declared_only',
        coverage_factor=None,
    )
    assert budget.outcome.combination_state == 'declared_only'
    assert budget.outcome.combined_standard_uncertainty is None
    assert significance_threshold(budget) is None
    assert (
        classify_residual(5.0, budget)
        == 'insufficient_uncertainty_information'
    )


def test_mub_spectral_combination_same_grid() -> None:
    grid = (
        UncertaintySpectrumPoint(frequency_hz=100.0, standard_uncertainty=0.3),
        UncertaintySpectrumPoint(frequency_hz=200.0, standard_uncertainty=0.4),
    )
    budget = _budget(
        coverage_factor=2.0,
        valid_frequency_hz=(50.0, 500.0),
        contributors=(
            _contributor('c1', spectral_points=grid),
            _contributor(
                'c2',
                spectral_points=tuple(
                    UncertaintySpectrumPoint(
                        frequency_hz=p.frequency_hz,
                        standard_uncertainty=0.0,
                    )
                    for p in grid
                ),
            ),
        ),
    )
    points = budget.outcome.spectral_points
    assert len(points) == 2
    assert points[0].combined_standard_uncertainty == pytest.approx(0.3)
    assert points[0].expanded_uncertainty == pytest.approx(0.6)


def test_mub_spectral_mismatched_grids_refused() -> None:
    budget = _budget(
        contributors=(
            _contributor(
                'c1',
                spectral_points=(
                    UncertaintySpectrumPoint(
                        frequency_hz=100.0, standard_uncertainty=0.3
                    ),
                    UncertaintySpectrumPoint(
                        frequency_hz=200.0, standard_uncertainty=0.3
                    ),
                ),
            ),
            _contributor(
                'c2',
                spectral_points=(
                    UncertaintySpectrumPoint(
                        frequency_hz=150.0, standard_uncertainty=0.1
                    ),
                    UncertaintySpectrumPoint(
                        frequency_hz=250.0, standard_uncertainty=0.1
                    ),
                ),
            ),
        ),
        coverage_factor=None,
    )
    assert budget.outcome.spectral_points == ()
    assert any('different frequency grids' in line for line in budget.limitations)


# =========================================================================
# #572 — traceability chain & calibrator drift
# =========================================================================


def _linked_chain() -> tuple[CalibrationChainLink, ...]:
    return (
        CalibrationChainLink(
            link_id='l1',
            role='measurement_instrument',
            label='measurement mic',
            instrument_id='mic-1',
        ),
        CalibrationChainLink(
            link_id='l2',
            role='sound_level_calibrator',
            label='class-1 calibrator',
            instrument_id='cal-1',
        ),
        CalibrationChainLink(
            link_id='l3',
            role='reference_standard',
            label='lab reference',
        ),
        CalibrationChainLink(
            link_id='l4',
            role='si_traceable_reference',
            label='national standard',
        ),
    )


def test_mub_traceable_documented_chain() -> None:
    budget = _budget(
        traceability_class='traceable_documented',
        traceability_links=_linked_chain(),
        observable_kind='absolute_spl_db',
    )
    assert budget.traceability_class == 'traceable_documented'


def test_mub_broken_link_blocks_traceable_claim() -> None:
    links = _linked_chain()[:2] + (
        CalibrationChainLink(
            link_id='l3',
            role='reference_standard',
            state='broken',
            notes='certificate expired',
        ),
    )
    with pytest.raises(ValueError, match='broken or unknown'):
        _budget(
            traceability_class='traceable_documented',
            traceability_links=links,
        )
    # declared-limited is honest
    budget = _budget(
        traceability_class='traceable_limited',
        traceability_links=links,
    )
    assert budget.traceability_class == 'traceable_limited'


def test_mub_broken_link_requires_reason() -> None:
    with pytest.raises(ValueError, match='reason'):
        CalibrationChainLink(
            link_id='l1',
            role='calibration_artifact',
            state='broken',
        )


def test_mub_relative_only_cannot_claim_absolute_spl() -> None:
    with pytest.raises(ValueError, match='absolute SPL'):
        _budget(
            traceability_class='relative_only',
            observable_kind='absolute_spl_db',
        )


def test_mub30_calibrator_drift() -> None:
    stable = (
        CalibratorCheck(
            check_id='p1',
            check_kind='pre',
            measured_level_db=93.9,
            nominal_level_db=94.0,
        ),
        CalibratorCheck(
            check_id='p2',
            check_kind='post',
            measured_level_db=94.0,
            nominal_level_db=94.0,
        ),
    )
    state, drift = evaluate_calibrator_drift(stable, 0.3)
    assert state == 'calibrator_stable'
    assert drift == pytest.approx(0.1)

    drifted = (
        CalibratorCheck(
            check_id='p1',
            check_kind='pre',
            measured_level_db=93.9,
            nominal_level_db=94.0,
        ),
        CalibratorCheck(
            check_id='p2',
            check_kind='post',
            measured_level_db=94.6,
            nominal_level_db=94.0,
        ),
    )
    state, drift = evaluate_calibrator_drift(drifted, 0.3)
    assert state == 'calibrator_drift_detected'
    assert drift == pytest.approx(0.7)

    state, _ = evaluate_calibrator_drift(stable[:1], 0.3)
    assert state == 'insufficient_calibrator_evidence'


# =========================================================================
# #572 — significance verdicts
# =========================================================================


def test_mub_residual_classification_states() -> None:
    budget = _budget(
        contributors=(
            _contributor('c1', standard_uncertainty=0.25),
            _contributor('c2', standard_uncertainty=0.25),
        ),
        coverage_factor=2.0,
    )
    gate = significance_threshold(budget)
    assert gate == (pytest.approx(sqrt(2 * 0.0625) * 2), 'expanded')
    # |r| > U → clearly above
    assert (
        classify_residual(0.9, budget)
        == 'residual_clearly_above_measurement_uncertainty'
    )
    # u_c/2 ≈ 0.18 — inside resolution floor
    assert (
        classify_residual(0.05, budget)
        == 'residual_below_resolution_of_evidence'
    )
    # between floor and gate → comparable to uncertainty
    assert (
        classify_residual(0.5, budget)
        == 'residual_comparable_to_measurement_uncertainty'
    )


def test_mub40_delta_significance_fail_closed() -> None:
    budget = _budget(contributors=(_contributor('c1', standard_uncertainty=0.4),))
    other = _budget(contributors=(_contributor('c1', standard_uncertainty=0.3),))
    # combined gate = sqrt(0.8^2 + 0.6^2) = 1.0 — a 0.2 dB delta is noise
    state, combined, _ = evaluate_delta_significance(0.2, budget, other)
    assert state == 'delta_inconclusive_within_evidence_uncertainty'
    assert combined == pytest.approx(1.0)
    state, _, _ = evaluate_delta_significance(1.5, budget, other)
    assert state == 'delta_exceeds_combined_evidence_uncertainty'

    unevidenced = _budget(
        propagation_method='declared_only', coverage_factor=None
    )
    state, combined, _ = evaluate_delta_significance(0.2, budget, unevidenced)
    assert state == 'insufficient_uncertainty_information'
    assert combined is None


def _residual_report(mean_db: float) -> PredictionMeasurementResidualReport:
    from htdt.cad_prediction_measurement_registration import (
        build_residual_computation_spec,
    )

    spec = build_residual_computation_spec(
        bands_hz=((20.0, 200.0),),
        predicted_response_sha256=_hash('pred'),
        measured_response_sha256=_hash('meas'),
    )
    provisional = PredictionMeasurementResidualReport.model_construct(
        report_id='0' * 64,
        registration_id='pm-reg:test',
        registration_sha256=_hash('registration'),
        document_id=DOC,
        comparability_state='comparable',
        partition='unassigned',
        spec=spec,
        observables=(
            ResidualObservable(
                observable='magnitude_db',
                state='computed',
                magnitude_bands=(
                    BandMagnitudeResidual(
                        band_hz=(20.0, 200.0),
                        valid_points=48,
                        mean_difference_db=mean_db,
                        rms_difference_db=mean_db,
                    ),
                ),
            ),
        ),
        created_at_utc=NOW,
        semantic_sha256='0' * 64,
    )
    identity = provisional.model_dump(
        mode='json', exclude={'report_id', 'semantic_sha256'}
    )
    semantic = canonical_sha256(identity)
    return PredictionMeasurementResidualReport(
        **{**identity, 'semantic_sha256': semantic,
           'report_id': f'pm-residual:{semantic}'}
    )


def test_mub_residual_report_assessment(tmp_path: Path) -> None:
    budget = _budget(contributors=(_contributor('c1', standard_uncertainty=0.4),))
    report = _residual_report(0.1)
    assessment = assess_residual_report(report, budget, created_at_utc=NOW)
    assert assessment.assessment_id.startswith('msa:')
    assert assessment.subject_ref_sha256 == report.semantic_sha256
    assert (
        assessment.summary_state
        == 'residual_below_resolution_of_evidence'
    )
    assert assessment.observations
    # a clearly-above band flips the summary
    report2 = _residual_report(1.5)
    assessment2 = assess_residual_report(report2, budget, created_at_utc=NOW)
    assert (
        assessment2.summary_state
        == 'residual_clearly_above_measurement_uncertainty'
    )
    repository = _repository(tmp_path)
    repository.save_uncertainty_budget(budget)
    repository.save_significance_assessment(assessment)
    assert repository.get_significance_assessment(
        assessment.assessment_id
    ) == assessment


def test_mub_assess_measurement_delta() -> None:
    budget = _budget(contributors=(_contributor('c1', standard_uncertainty=0.4),))
    assessment = assess_measurement_delta(
        document_id=DOC,
        subject_ref_id='pair-1',
        deltas=(('magnitude_db', '20-200Hz/mean', 0.2),),
        budget_a=budget,
        budget_b=budget,
        created_at_utc=NOW,
    )
    assert assessment.summary_state == (
        'delta_inconclusive_within_evidence_uncertainty'
    )


# =========================================================================
# #573 — state snapshots, policy, comparability gate
# =========================================================================


def _snapshot(
    measurement_id: str,
    *,
    observed_at_utc: str = NOW,
    temperature_c: float | None = 22.0,
    sound_speed_m_s: float | None = 344.0,
    occupancy: str = 'unoccupied',
    openings: tuple[OpeningObservation, ...] = (
        OpeningObservation(opening_id='door-1', state='closed'),
    ),
    dynamic: DeviceDynamicState | None = None,
    noise: NoiseRegime | None = None,
    scene_content_hash: str | None = None,
    sequence_index: int | None = None,
) -> MeasurementStateSnapshot:
    return build_state_snapshot(
        document_id=DOC,
        measurement_id=measurement_id,
        sequence_index=sequence_index,
        observed_at_utc=observed_at_utc,
        scene_content_hash=scene_content_hash or _hash('scene-v1'),
        environment=EnvironmentObservation(
            temperature_c=temperature_c,
            relative_humidity_percent=45.0,
            sound_speed_m_s=sound_speed_m_s,
            source='measured',
        ),
        openings=openings,
        occupancy=occupancy,  # type: ignore[arg-type]
        dynamic_state=dynamic,
        noise_regime=noise,
    )


def _policy(**kwargs) -> StateControlPolicy:
    kwargs.setdefault('name', 'qualification-run')
    return build_state_control_policy(
        document_id=DOC,
        created_at_utc=NOW,
        **kwargs,
    )


def test_mss10_snapshot_seals_and_persists(tmp_path: Path) -> None:
    snapshot = _snapshot('meas-1')
    assert snapshot.snapshot_id == f'mss:{snapshot.semantic_sha256}'
    repository = _repository(tmp_path)
    repository.save_state_snapshot(snapshot)
    assert repository.get_state_snapshot(snapshot.snapshot_id) == snapshot
    assert repository.state_snapshots_for_measurement('meas-1') == (
        snapshot,
    )


def test_mss_policy_seals_and_persists(tmp_path: Path) -> None:
    policy = _policy(
        allowed_temperature_delta_c=0.5,
        occupancy_policy='must_be_unoccupied',
    )
    repository = _repository(tmp_path)
    repository.save_state_policy(policy)
    assert repository.get_state_policy(policy.policy_id) == policy
    assert repository.list_state_policies(DOC) == (policy,)


def test_mss_identical_states_comparable() -> None:
    a = _snapshot('meas-1')
    b = _snapshot('meas-2', observed_at_utc='2026-10-05T00:10:00+00:00')
    verdict = evaluate_state_comparability(
        a, b, _policy(allowed_temperature_delta_c=0.5),
        document_id=DOC, created_at_utc=NOW,
    )
    assert verdict.state == 'comparable_stable_state'
    assert verdict.phase_comparable
    assert verdict.timing_comparable
    assert verdict.verdict_id == f'msv:{verdict.semantic_sha256}'


def test_mss_missing_snapshot_is_insufficient() -> None:
    verdict = evaluate_state_comparability(
        None, _snapshot('meas-2'), _policy(),
        document_id=DOC, created_at_utc=NOW,
    )
    assert verdict.state == 'insufficient_state_evidence'
    assert 'snapshot_missing' in verdict.reason_codes
    assert not verdict.magnitude_comparable
    assert not verdict.phase_comparable


def test_mss20_temperature_drift_invalidates_phase_before_magnitude() -> None:
    a = _snapshot('meas-1', temperature_c=22.0)
    b = _snapshot(
        'meas-2', temperature_c=26.0,
        observed_at_utc='2026-10-05T01:00:00+00:00',
    )
    verdict = evaluate_state_comparability(
        a, b, _policy(allowed_temperature_delta_c=0.5),
        document_id=DOC, created_at_utc=NOW,
    )
    assert verdict.state == 'state_changed'
    assert 'environment_delta_exceeds' in verdict.reason_codes
    assert not verdict.phase_comparable
    assert not verdict.timing_comparable


def test_mss_sound_speed_drift_declared() -> None:
    a = _snapshot('meas-1', temperature_c=22.0, sound_speed_m_s=344.0)
    b = _snapshot(
        'meas-2', temperature_c=22.0, sound_speed_m_s=347.0,
        observed_at_utc='2026-10-05T01:00:00+00:00',
    )
    verdict = evaluate_state_comparability(
        a,
        b,
        _policy(
            allowed_temperature_delta_c=5.0,
            allowed_sound_speed_relative_delta=0.002,
        ),
        document_id=DOC,
        created_at_utc=NOW,
    )
    assert verdict.state == 'comparable_with_declared_drift'
    assert not verdict.timing_comparable
    assert not verdict.phase_comparable
    assert verdict.magnitude_comparable


def test_mss30_occupancy_gate() -> None:
    a = _snapshot('meas-1', occupancy='unoccupied')
    b = _snapshot(
        'meas-2', occupancy='occupied_as_designed',
        observed_at_utc='2026-10-05T01:00:00+00:00',
    )
    verdict = evaluate_state_comparability(
        a, b, _policy(occupancy_policy='must_match'),
        document_id=DOC, created_at_utc=NOW,
    )
    assert verdict.state == 'state_changed'
    assert 'occupancy_changed' in verdict.reason_codes
    assert not verdict.magnitude_comparable

    c = _snapshot(
        'meas-3', occupancy='unknown',
        observed_at_utc='2026-10-05T02:00:00+00:00',
    )
    verdict = evaluate_state_comparability(
        a, c, _policy(occupancy_policy='must_match'),
        document_id=DOC, created_at_utc=NOW,
    )
    assert verdict.state == 'insufficient_state_evidence'
    assert 'unknown_occupancy' in verdict.reason_codes


def test_mss_opening_requirement_and_change() -> None:
    policy = _policy(
        required_openings=(
            OpeningRequirement(opening_id='door-1', required_state='closed'),
        ),
    )
    a = _snapshot('meas-1')
    b = _snapshot(
        'meas-2',
        openings=(OpeningObservation(opening_id='door-1', state='open'),),
        observed_at_utc='2026-10-05T01:00:00+00:00',
    )
    verdict = evaluate_state_comparability(
        a, b, policy, document_id=DOC, created_at_utc=NOW
    )
    assert verdict.state == 'state_changed'
    assert 'opening_requirement_unmet' in verdict.reason_codes
    assert not verdict.decay_comparable


def test_mss40_dynamic_dsp_contamination() -> None:
    a = _snapshot(
        'meas-1',
        dynamic=DeviceDynamicState(loudness='disabled'),
    )
    b = _snapshot(
        'meas-2',
        dynamic=DeviceDynamicState(
            loudness='engaged', compressor_limiter='unknown'
        ),
        observed_at_utc='2026-10-05T01:00:00+00:00',
    )
    verdict = evaluate_state_comparability(
        a, b, _policy(), document_id=DOC, created_at_utc=NOW
    )
    assert verdict.state == 'state_changed'
    assert 'dynamic_dsp_state_changed' in verdict.reason_codes
    assert not verdict.level_comparable
    assert not verdict.magnitude_comparable


def test_mss_required_dynamic_unknown_is_insufficient() -> None:
    a = _snapshot('meas-1')
    b = _snapshot('meas-2', observed_at_utc='2026-10-05T01:00:00+00:00')
    verdict = evaluate_state_comparability(
        a,
        b,
        _policy(
            required_dynamic_states=(
                DynamicRequirement(
                    feature='adaptive_room_correction',
                    required_state='disabled',
                ),
            ),
        ),
        document_id=DOC,
        created_at_utc=NOW,
    )
    assert verdict.state == 'insufficient_state_evidence'
    assert 'unknown_device_state' in verdict.reason_codes


def test_mss_limiter_detected_fails_closed() -> None:
    a = _snapshot('meas-1', dynamic=DeviceDynamicState())
    b = _snapshot(
        'meas-2',
        dynamic=DeviceDynamicState(limiter_engagement_observed=True),
        observed_at_utc='2026-10-05T01:00:00+00:00',
    )
    verdict = evaluate_state_comparability(
        a, b, _policy(), document_id=DOC, created_at_utc=NOW
    )
    assert verdict.state == 'state_changed'
    assert 'limiter_detected' in verdict.reason_codes
    assert not verdict.distortion_comparable


def test_mss_noise_regime_change() -> None:
    policy = _policy(noise_rule='must_match')
    a = _snapshot('meas-1', noise=NoiseRegime(hvac='off'))
    b = _snapshot(
        'meas-2',
        noise=NoiseRegime(hvac='on'),
        observed_at_utc='2026-10-05T01:00:00+00:00',
    )
    verdict = evaluate_state_comparability(
        a, b, policy, document_id=DOC, created_at_utc=NOW
    )
    assert verdict.state == 'state_changed'
    assert 'noise_regime_changed' in verdict.reason_codes
    assert not verdict.decay_comparable


def test_mss_noise_floor_rule() -> None:
    policy = _policy(noise_rule='floor_below_db', noise_floor_max_db_spl=30.0)
    a = _snapshot('meas-1', noise=NoiseRegime(noise_floor_db_spl=25.0))
    b = _snapshot(
        'meas-2',
        noise=NoiseRegime(noise_floor_db_spl=42.0),
        observed_at_utc='2026-10-05T01:00:00+00:00',
    )
    verdict = evaluate_state_comparability(
        a, b, policy, document_id=DOC, created_at_utc=NOW
    )
    assert verdict.state == 'state_changed'
    assert 'noise_floor_exceeds' in verdict.reason_codes


def test_mss_elapsed_interval_drift() -> None:
    a = _snapshot('meas-1')
    b = _snapshot('meas-2', observed_at_utc='2026-10-05T01:00:00+00:00')
    verdict = evaluate_state_comparability(
        a,
        b,
        _policy(max_elapsed_s=1800.0),
        document_id=DOC,
        created_at_utc=NOW,
        elapsed_s=3600.0,
    )
    assert verdict.state == 'comparable_with_declared_drift'
    assert 'elapsed_interval_exceeds' in verdict.reason_codes
    assert not verdict.timing_comparable


def test_mss_floor_rule_requires_threshold() -> None:
    with pytest.raises(ValueError, match='noise_floor_max_db_spl'):
        _policy(noise_rule='floor_below_db')


def test_mss50_sequence_stationarity() -> None:
    policy = _policy(magnitude_db_tolerance=0.5)
    snapshots = tuple(
        _snapshot(f'meas-{i}', sequence_index=i) for i in range(3)
    )
    stable = tuple(
        _dataset(f'ds-{i}', (80.0, 81.0, 79.5, 82.0), measurement_id=f'meas-{i}')
        for i in range(3)
    )
    verdict = evaluate_sequence_stationarity(
        snapshots, stable, policy, document_id=DOC, created_at_utc=NOW
    )
    assert verdict.state == 'comparable_stable_state'

    drifting = (
        _dataset('ds-0', (80.0, 81.0, 79.5, 82.0), measurement_id='meas-0'),
        _dataset('ds-1', (81.0, 81.0, 79.5, 82.0), measurement_id='meas-1'),
        _dataset('ds-2', (83.0, 81.0, 79.5, 82.0), measurement_id='meas-2'),
    )
    verdict = evaluate_sequence_stationarity(
        snapshots, drifting, policy, document_id=DOC, created_at_utc=NOW
    )
    assert verdict.state == 'non_stationary_during_capture'
    assert 'repeat_non_stationarity' in verdict.reason_codes
    assert not verdict.magnitude_comparable


def test_mss_sequence_mixed_grids_insufficient() -> None:
    snapshots = tuple(
        _snapshot(f'meas-{i}', sequence_index=i) for i in range(2)
    )
    datasets = (
        _dataset('ds-0', (80.0, 81.0, 79.5, 82.0)),
        _dataset(
            'ds-1',
            (80.0, 81.0),
            frequencies=(20.0, 40.0),
            measurement_id='meas-1',
        ),
    )
    verdict = evaluate_sequence_stationarity(
        snapshots, datasets, _policy(), document_id=DOC, created_at_utc=NOW
    )
    assert verdict.state == 'insufficient_state_evidence'


def test_mss60_verdict_persists(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    policy = _policy(allowed_temperature_delta_c=0.5)
    a = _snapshot('meas-1', temperature_c=22.0)
    b = _snapshot('meas-2', temperature_c=26.0)
    verdict = evaluate_state_comparability(
        a, b, policy, document_id=DOC, created_at_utc=NOW
    )
    repository.save_state_policy(policy)
    repository.save_state_snapshot(a)
    repository.save_state_snapshot(b)
    repository.save_state_verdict(verdict)
    assert repository.get_state_verdict(verdict.verdict_id) == verdict
    assert repository.list_state_verdicts(DOC) == (verdict,)


# =========================================================================
# #575 — transform DAG authority
# =========================================================================


def _input(
    input_id: str,
    dataset: CadFrequencyResponseDataset,
    *,
    role: str = 'member',
    flags: tuple = ('magnitude_valid', 'relative_phase_valid'),
    domain: str | None = None,
    position_id: str | None = None,
    weight: float | None = None,
) -> TransformInput:
    return TransformInput(
        input_id=input_id,
        role=role,  # type: ignore[arg-type]
        source_kind='measured_dataset',
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        capability_flags=flags,
        declared_domain=domain,  # type: ignore[arg-type]
        position_id=position_id,
        weight=weight,
    )


def _transform(
    *,
    kind: str = 'rms_magnitude_average',
    inputs: tuple[TransformInput, ...],
    parameters: TransformParameters | None = None,
    averaging_class: str | None = 'repeat_capture',
    output_capabilities: tuple = ('magnitude_valid',),
    input_capabilities: tuple | None = None,
    **kwargs,
) -> MeasurementTransform:
    if input_capabilities is None:
        shared = set(inputs[0].capability_flags)
        for entry in inputs[1:]:
            shared &= set(entry.capability_flags)
        input_capabilities = tuple(shared)
    return build_measurement_transform(
        document_id=DOC,
        kind=kind,  # type: ignore[arg-type]
        inputs=inputs,
        parameters=parameters or TransformParameters(),
        averaging_class=averaging_class,  # type: ignore[arg-type]
        input_capabilities=input_capabilities,
        output_capabilities=output_capabilities,
        created_at_utc=NOW,
        **kwargs,
    )


def test_mta10_transform_seals_and_persists(tmp_path: Path) -> None:
    ds_a = _dataset('ds-a', (80.0, 80.0, 80.0, 80.0))
    ds_b = _dataset('ds-b', (82.0, 82.0, 82.0, 82.0))
    transform = _transform(inputs=(_input('a', ds_a), _input('b', ds_b)))
    assert transform.transform_id == f'mtr:{transform.semantic_sha256}'
    repository = _repository(tmp_path)
    repository.save_transform(transform)
    assert repository.get_transform(transform.transform_id) == transform
    assert repository.list_transforms(DOC) == (transform,)


def test_mta_average_requires_class_and_two_inputs() -> None:
    ds_a = _dataset('ds-a', (80.0, 80.0, 80.0, 80.0))
    ds_b = _dataset('ds-b', (82.0, 82.0, 82.0, 82.0))
    with pytest.raises(ValueError, match='declared class'):
        _transform(
            inputs=(_input('a', ds_a), _input('b', ds_b)),
            averaging_class=None,
        )
    with pytest.raises(ValueError, match='at least two'):
        _transform(inputs=(_input('a', ds_a),))


def test_mta_spatial_vs_repeat_classification() -> None:
    ds_a = _dataset('ds-a', (80.0, 80.0, 80.0, 80.0))
    ds_b = _dataset('ds-b', (82.0, 82.0, 82.0, 82.0))
    with pytest.raises(ValueError, match='distinct positions'):
        _transform(
            inputs=(_input('a', ds_a), _input('b', ds_b)),
            averaging_class='spatial',
        )
    with pytest.raises(ValueError, match='repeat-capture'):
        _transform(
            inputs=(
                _input('a', ds_a, position_id='pos-1'),
                _input('b', ds_b, position_id='pos-2'),
            ),
            averaging_class='repeat_capture',
        )


def test_mta20_alignment_authority_rules() -> None:
    ds_a = _dataset(
        'ds-a', (80.0,) * 4, phases=(0.0,) * 4, phase_status='valid'
    )
    ds_b = _dataset(
        'ds-b', (80.0,) * 4, phases=(0.0,) * 4, phase_status='valid'
    )
    # vector average without declared alignment → refused
    with pytest.raises(ValueError, match='alignment'):
        _transform(
            kind='vector_complex_average',
            inputs=(_input('a', ds_a), _input('b', ds_b)),
            output_capabilities=(
                'magnitude_valid',
                'relative_phase_valid',
            ),
        )
    # spatial + phase-average without alignment → rejected physical claim
    def _physical(entry: TransformInput) -> TransformInput:
        return TransformInput(
            input_id=entry.input_id,
            role=entry.role,
            source_kind='measured_dataset',
            dataset_id=entry.dataset_id,
            dataset_sha256=entry.dataset_sha256,
            capability_flags=(
                'magnitude_valid',
                'relative_phase_valid',
                'impulse_response_physical',
            ),
            position_id=entry.position_id,
        )

    with pytest.raises(ValueError, match='impulse_response_physical'):
        _transform(
            kind='rms_magnitude_average',
            averaging_class='spatial',
            inputs=(
                _physical(_input('a', ds_a, position_id='p1')),
                _physical(_input('b', ds_b, position_id='p2')),
            ),
            output_capabilities=(
                'magnitude_valid',
                'impulse_response_physical',
            ),
        )


def test_mta_estimated_alignment_never_absolute() -> None:
    ds_a = _dataset('ds-a', (80.0,) * 4)
    alignment = AlignmentSpec(
        method='estimated_cross_correlation',
        reference_input_id='a',
        correlation_band_hz=(100.0, 1000.0),
        algorithm_version='xcorr-v1',
    )
    with pytest.raises(ValueError, match='absolute'):
        _transform(
            kind='time_shift',
            inputs=(
                _input(
                    'a', ds_a,
                    flags=('magnitude_valid', 'absolute_timing_valid'),
                ),
            ),
            parameters=TransformParameters(alignment=alignment),
            averaging_class=None,
            input_capabilities=('magnitude_valid', 'absolute_timing_valid'),
            output_capabilities=(
                'magnitude_valid',
                'absolute_timing_valid',
            ),
        )


def test_mta_capabilities_never_upgrade() -> None:
    ds_a = _dataset('ds-a', (80.0,) * 4)
    ds_b = _dataset('ds-b', (80.0,) * 4)
    # inputs share magnitude+phase; claiming absolute SPL output is refused
    with pytest.raises(ValueError, match='downgrade'):
        _transform(
            inputs=(_input('a', ds_a), _input('b', ds_b)),
            output_capabilities=('magnitude_valid', 'absolute_spl_valid'),
        )


def test_mta_output_outside_allowed_set_rejected() -> None:
    ds_a = _dataset(
        'ds-a', (80.0,) * 4,
    )
    with pytest.raises(ValueError, match='may not output'):
        _transform(
            kind='normalize',
            inputs=(
                _input(
                    'a', ds_a,
                    flags=('magnitude_valid', 'absolute_spl_valid'),
                ),
            ),
            averaging_class=None,
            parameters=TransformParameters(normalize_rule='peak'),
            input_capabilities=('magnitude_valid', 'absolute_spl_valid'),
            output_capabilities=('magnitude_valid', 'absolute_spl_valid'),
        )


def test_mta50_arithmetic_domain_validation() -> None:
    ds_a = _dataset('ds-a', (80.0,) * 4)
    ds_b = _dataset('ds-b', (80.0,) * 4)
    # dB multiply is meaningless → refused
    with pytest.raises(ValueError, match='not defined'):
        _transform(
            kind='trace_multiply',
            inputs=(
                _input('a', ds_a, domain='db_magnitude'),
                _input('b', ds_b, domain='db_magnitude'),
            ),
            averaging_class=None,
            parameters=TransformParameters(
                arithmetic=ArithmeticSpec(
                    input_domains=('db_magnitude', 'db_magnitude'),
                    output_domain='db_magnitude',
                )
            ),
        )
    # declared dB subtract is fine
    transform = _transform(
        kind='trace_subtract',
        inputs=(
            _input('a', ds_a, domain='db_magnitude'),
            _input('b', ds_b, domain='db_magnitude'),
        ),
        averaging_class=None,
        parameters=TransformParameters(
            arithmetic=ArithmeticSpec(
                input_domains=('db_magnitude', 'db_magnitude'),
                output_domain='db_magnitude',
            )
        ),
    )
    assert transform.kind == 'trace_subtract'


def test_mta_clock_requires_spec_and_raw_preserved() -> None:
    ds_a = _dataset('ds-a', (80.0,) * 4)
    with pytest.raises(ValueError, match='clock'):
        _transform(
            kind='clock_rate_correction',
            inputs=(_input('a', ds_a),),
            averaging_class=None,
        )
    with pytest.raises(ValueError, match='raw source'):
        ClockCorrectionSpec(
            relation='independent_estimated',
            applied_factor=1.0001,
            raw_source_preserved=False,
        )


def test_mta_derived_output_capability_match() -> None:
    ds_a = _dataset('ds-a', (80.0,) * 4)
    ds_b = _dataset('ds-b', (80.0,) * 4)
    transform = _transform(
        inputs=(_input('a', ds_a), _input('b', ds_b)),
    )
    output = apply_transform(
        transform,
        {'a': ds_a, 'b': ds_b},
        output_id='derived-1',
    )
    assert tuple(output.capability_flags) == transform.output_capabilities
    sealed = _transform(
        inputs=(_input('a', ds_a), _input('b', ds_b)),
        derived_output=output,
    )
    assert sealed.derived_output is not None


# =========================================================================
# #575 — executor numerics
# =========================================================================


def test_mta_rms_average_numeric() -> None:
    ds_a = _dataset('ds-a', (80.0, 80.0, 80.0, 80.0))
    ds_b = _dataset('ds-b', (80.0, 80.0, 80.0, 80.0))
    transform = _transform(inputs=(_input('a', ds_a), _input('b', ds_b)))
    out = apply_transform(transform, {'a': ds_a, 'b': ds_b}, output_id='o1')
    assert out.level_db == pytest.approx((80.0,) * 4)

    ds_c = _dataset('ds-c', (80.0, 80.0, 80.0, 80.0))
    ds_d = _dataset('ds-d', (74.0, 74.0, 74.0, 74.0))
    transform = _transform(inputs=(_input('a', ds_c), _input('b', ds_d)))
    out = apply_transform(transform, {'a': ds_c, 'b': ds_d}, output_id='o2')
    # RMS of 10^(80/10) and 10^(74/10) → 10log10((P1+P2)/2)
    expected = 10.0 * (
        __import__('math').log10(
            (10.0 ** 8.0 + 10.0 ** 7.4) / 2.0
        )
    )
    assert out.level_db[0] == pytest.approx(expected)


def test_mta_db_average_numeric() -> None:
    ds_a = _dataset('ds-a', (80.0,) * 4)
    ds_b = _dataset('ds-b', (74.0,) * 4)
    transform = _transform(
        kind='db_average',
        inputs=(_input('a', ds_a), _input('b', ds_b)),
    )
    out = apply_transform(transform, {'a': ds_a, 'b': ds_b}, output_id='o')
    assert out.level_db == pytest.approx((77.0,) * 4)


def test_mta_vector_average_cancels_opposite_phase() -> None:
    ds_a = _dataset(
        'ds-a', (80.0,) * 4, phases=(0.0,) * 4, phase_status='valid'
    )
    ds_b = _dataset(
        'ds-b', (80.0,) * 4, phases=(180.0,) * 4, phase_status='valid'
    )
    alignment = AlignmentSpec(
        method='measured_common_timing_reference',
        timing_reference_type='loopback',
    )
    transform = _transform(
        kind='vector_complex_average',
        inputs=(_input('a', ds_a), _input('b', ds_b)),
        parameters=TransformParameters(alignment=alignment),
        output_capabilities=('magnitude_valid', 'relative_phase_valid'),
    )
    out = apply_transform(transform, {'a': ds_a, 'b': ds_b}, output_id='o')
    assert out.level_db[0] < -200.0  # equal amplitudes, opposite phases
    assert out.quantity_domain == 'complex_transfer'


def test_mta_explicit_weights() -> None:
    ds_a = _dataset('ds-a', (80.0,) * 4)
    ds_b = _dataset('ds-b', (70.0,) * 4)
    transform = _transform(
        kind='db_average',
        inputs=(
            _input('a', ds_a, weight=3.0),
            _input('b', ds_b, weight=1.0),
        ),
        parameters=TransformParameters(weighting='explicit'),
    )
    out = apply_transform(transform, {'a': ds_a, 'b': ds_b}, output_id='o')
    assert out.level_db[0] == pytest.approx(77.5)


def test_mta_trace_subtract_db() -> None:
    ds_a = _dataset('ds-a', (80.0,) * 4)
    ds_b = _dataset('ds-b', (75.0,) * 4)
    transform = _transform(
        kind='trace_subtract',
        inputs=(
            _input('a', ds_a, domain='db_magnitude'),
            _input('b', ds_b, domain='db_magnitude'),
        ),
        averaging_class=None,
        parameters=TransformParameters(
            arithmetic=ArithmeticSpec(
                input_domains=('db_magnitude', 'db_magnitude'),
                output_domain='db_magnitude',
            )
        ),
    )
    out = apply_transform(transform, {'a': ds_a, 'b': ds_b}, output_id='o')
    assert out.level_db == pytest.approx((5.0,) * 4)


def test_mta_level_alignment_offsets() -> None:
    ds_ref = _dataset('ds-ref', (80.0,) * 4)
    ds_b = _dataset('ds-b', (76.0,) * 4)
    transform = _transform(
        kind='level_alignment',
        inputs=(
            _input('ref', ds_ref, role='reference'),
            _input('b', ds_b),
        ),
        averaging_class=None,
        parameters=TransformParameters(
            level_alignment_span_hz=(20.0, 200.0)
        ),
    )
    out = apply_transform(
        transform, {'ref': ds_ref, 'b': ds_b}, output_id='o'
    )
    # aligned: b + 4.0 = 80 → equal-weight mean = 80
    assert out.level_db == pytest.approx((80.0,) * 4)


def test_mta_normalize_peak() -> None:
    ds_a = _dataset('ds-a', (80.0, 85.0, 82.0, 78.0))
    transform = _transform(
        kind='normalize',
        inputs=(_input('a', ds_a),),
        averaging_class=None,
        parameters=TransformParameters(normalize_rule='peak'),
    )
    out = apply_transform(transform, {'a': ds_a}, output_id='o')
    assert out.level_db == pytest.approx((-5.0, 0.0, -3.0, -7.0))


def test_mta_band_aggregate() -> None:
    ds_a = _dataset('ds-a', (80.0, 82.0, 84.0, 86.0))
    transform = _transform(
        kind='band_aggregate',
        inputs=(_input('a', ds_a),),
        averaging_class=None,
        parameters=TransformParameters(
            aggregate_bands_hz=((20.0, 80.0), (81.0, 200.0))
        ),
    )
    out = apply_transform(transform, {'a': ds_a}, output_id='o')
    assert len(out.frequency_hz) == 2
    assert out.level_db[0] == pytest.approx(82.0, rel=1e-5)
    assert out.level_db[1] == pytest.approx(86.0)


def test_mta_resample_declared_grid() -> None:
    ds_a = _dataset(
        'ds-a', (80.0, 82.0), frequencies=(100.0, 200.0)
    )
    transform = _transform(
        kind='resample',
        inputs=(_input('a', ds_a),),
        averaging_class=None,
        parameters=TransformParameters(
            output_grid_hz=(100.0, 150.0, 200.0),
            interpolation_rule='linear_v1',
        ),
    )
    out = apply_transform(transform, {'a': ds_a}, output_id='o')
    assert out.frequency_hz == (100.0, 150.0, 200.0)
    assert out.level_db[1] == pytest.approx(81.0)


def test_mta_frequency_merge_overlap_rules() -> None:
    ds_a = _dataset('ds-a', (80.0, 81.0), frequencies=(20.0, 100.0))
    ds_b = _dataset('ds-b', (85.0, 86.0), frequencies=(100.0, 200.0))
    transform = _transform(
        kind='frequency_merge',
        inputs=(_input('a', ds_a), _input('b', ds_b)),
        averaging_class=None,
    )
    with pytest.raises(ValueError, match='overlap'):
        apply_transform(transform, {'a': ds_a, 'b': ds_b}, output_id='o')
    transform = _transform(
        kind='frequency_merge',
        inputs=(_input('a', ds_a), _input('b', ds_b)),
        averaging_class=None,
        parameters=TransformParameters(merge_overlap_rule='prefer_first'),
    )
    out = apply_transform(transform, {'a': ds_a, 'b': ds_b}, output_id='o')
    assert out.frequency_hz == (20.0, 100.0, 200.0)
    # prefer_first: ds_a's level wins at the shared 100 Hz point
    assert out.level_db == (80.0, 81.0, 86.0)


def test_mta_input_substitution_refused() -> None:
    ds_a = _dataset('ds-a', (80.0,) * 4)
    ds_b = _dataset('ds-b', (80.0,) * 4)
    transform = _transform(inputs=(_input('a', ds_a), _input('b', ds_b)))
    swapped = _dataset('ds-a', (90.0,) * 4)
    with pytest.raises(ValueError, match='identity'):
        apply_transform(
            transform, {'a': swapped, 'b': ds_b}, output_id='o'
        )


def test_mta_time_domain_kinds_refuse_fr_execution() -> None:
    ds_a = _dataset('ds-a', (80.0,) * 4)
    transform = _transform(
        kind='clock_rate_correction',
        inputs=(_input('a', ds_a),),
        averaging_class=None,
        parameters=TransformParameters(
            clock_correction=ClockCorrectionSpec(
                relation='independent_estimated',
                estimated_rate_difference=1e-5,
                method='acoustic_reference',
                applied_factor=1.00001,
                residual_error_bound=1e-6,
            )
        ),
    )
    with pytest.raises(ValueError, match='time-domain'):
        apply_transform(transform, {'a': ds_a}, output_id='o')


def test_mta_dag_input_binds_upstream_transform() -> None:
    ds_a = _dataset('ds-a', (80.0,) * 4)
    ds_b = _dataset('ds-b', (80.0,) * 4)
    first = _transform(inputs=(_input('a', ds_a), _input('b', ds_b)))
    derived = apply_transform(
        first, {'a': ds_a, 'b': ds_b}, output_id='o1'
    )
    upstream_input = TransformInput(
        input_id='prior',
        role='member',
        source_kind='derived_transform',
        transform_id=first.transform_id,
        transform_sha256=first.semantic_sha256,
        capability_flags=('magnitude_valid',),
    )
    ds_c = _dataset('ds-c', (80.0,) * 4)
    second = _transform(
        inputs=(upstream_input, _input('c', ds_c)),
    )
    assert second.transform_id != first.transform_id
    assert derived.content_sha256


# =========================================================================
# persistence / audit / display integration
# =========================================================================


def test_repository_conflict_fails(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    policy = _policy(name='p')
    repository.save_state_policy(policy)
    repository.save_state_policy(policy)  # idempotent
    tampered = build_state_control_policy(
        document_id=DOC,
        name='p-different',
        created_at_utc=NOW,
    )
    # a genuinely different record shares neither id nor sha → distinct row
    repository.save_state_policy(tampered)
    assert len(repository.list_state_policies(DOC)) == 2
    # rewriting the payload under an existing id is a conflict
    raw = tampered.model_dump(mode='json')
    raw['notes_marker'] = None
    raw['policy_id'] = policy.policy_id  # same id, different sha
    with pytest.raises(ValueError):
        StateControlPolicy.model_validate(raw)
    from contextlib import closing

    with closing(sqlite3.connect(repository.path)) as conn, conn:
        conn.execute(
            'UPDATE cad_measurement_state_policies SET payload_json=? '
            'WHERE policy_id=?',
            (tampered.model_dump_json(), policy.policy_id),
        )
    with pytest.raises(ValueError, match='disagrees'):
        repository.get_state_policy(policy.policy_id)


def test_audit_coverage_includes_new_tables() -> None:
    modes = audit_table_modes()
    for table in (
        'cad_measurement_uncertainty_budgets',
        'cad_measurement_significance_assessments',
        'cad_measurement_state_policies',
        'cad_measurement_state_snapshots',
        'cad_measurement_state_verdicts',
        'cad_measurement_transforms',
    ):
        assert modes.get(table) == 'replay_canonical', table


def test_display_lines_honest_absence(tmp_path: Path) -> None:
    from htdt.measurement_evidence_display import (
        quality_evidence_detail_lines,
    )

    repository = _repository(tmp_path)
    lines = quality_evidence_detail_lines('meas-x', repository=repository)
    assert any('未登録' in line for line in lines)
    assert any('未記録' in line for line in lines)

    budget = _budget(measurement_id='meas-x')
    repository.save_uncertainty_budget(budget)
    snapshot = _snapshot('meas-x')
    repository.save_state_snapshot(snapshot)
    lines = quality_evidence_detail_lines('meas-x', repository=repository)
    assert any('±' in line for line in lines)
    assert any('スナップショット済み' in line for line in lines)


def test_repository_fresh_lists_empty(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    assert repository.list_uncertainty_budgets(DOC) == ()
    assert repository.list_state_policies(DOC) == ()
    assert repository.list_transforms(DOC) == ()


def test_persistence_roundtrip_all_record_types(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    budget = _budget()
    repository.save_uncertainty_budget(budget)
    assessment = assess_measurement_delta(
        document_id=DOC,
        subject_ref_id='pair-1',
        deltas=(('magnitude_db', 'band/mean', 0.2),),
        budget_a=budget,
        budget_b=budget,
        created_at_utc=NOW,
    )
    repository.save_significance_assessment(assessment)
    policy = _policy()
    repository.save_state_policy(policy)
    snapshot = _snapshot('meas-1')
    repository.save_state_snapshot(snapshot)
    verdict = evaluate_state_comparability(
        snapshot, snapshot, policy, document_id=DOC, created_at_utc=NOW
    )
    repository.save_state_verdict(verdict)
    ds_a = _dataset('ds-a', (80.0,) * 4)
    ds_b = _dataset('ds-b', (80.0,) * 4)
    transform = _transform(inputs=(_input('a', ds_a), _input('b', ds_b)))
    repository.save_transform(transform)
    assert repository.get_uncertainty_budget(budget.budget_id) == budget
    assert (
        repository.get_significance_assessment(assessment.assessment_id)
        == assessment
    )
    assert repository.get_state_policy(policy.policy_id) == policy
    assert repository.get_state_snapshot(snapshot.snapshot_id) == snapshot
    assert repository.get_state_verdict(verdict.verdict_id) == verdict
    assert repository.get_transform(transform.transform_id) == transform


def test_conflict_error_on_same_id_different_content(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    snapshot = _snapshot('meas-1')
    repository.save_state_snapshot(snapshot)
    payload = snapshot.model_dump(mode='json')
    payload['notes'] = 'tampered'
    payload['snapshot_id'] = snapshot.snapshot_id
    forged = MeasurementStateSnapshot.model_construct(
        **payload
    )
    with pytest.raises(MeasurementEvidenceConflictError):
        repository._insert_once(
            table='cad_measurement_state_snapshots',
            columns=(
                'snapshot_id',
                'semantic_sha256',
                'document_id',
                'measurement_id',
                'observed_at_utc',
            ),
            values=(
                forged.snapshot_id,
                forged.semantic_sha256,
                forged.document_id,
                forged.measurement_id,
                forged.observed_at_utc,
            ),
            id_column='snapshot_id',
            record_id=forged.snapshot_id,
            payload=forged.model_dump_json(),
        )
