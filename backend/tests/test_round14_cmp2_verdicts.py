"""Round-14 CMP2 review: independent recomputation of comparison verdicts.

Each test constructs inputs whose correct verdict is known a priori and
asserts the real producer emits exactly that verdict — the same verdict
that the UI shows, the persisted row stores and exports carry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_acoustic_target import (
    AcousticTargetBand,
    AcousticTargetCriterion,
    AcousticTargetObservation,
    build_acoustic_target_profile,
    evaluate_acoustic_targets,
)
from htdt.cad_acoustic_treatment_comparison import (
    TreatmentCandidateOutcome,
    build_treatment_comparison_outcome,
    build_treatment_design_candidate,
    build_treatment_design_comparison,
)
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_comparison_semantics import (
    ComparisonSideContext,
    derive_comparison_semantics,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_evidence_reconciliation import (
    AlignmentRef,
    build_evidence_subject,
    build_observation,
    reconcile_subject,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import RoomPrism, SceneDocument
from htdt.cad_standards import CriterionRule, CriterionSource, _compare
from htdt.cad_validation_dashboard import (
    ValidationCaseRecord,
    compare_provider_versions,
)
from htdt.comparison import FrequencyResponse, compare_frequency_responses
from htdt.optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveValidDomain,
    ObjectiveVector,
)
from htdt.optimization_robustness import PerturbationSample
from htdt.optimization_robustness_multidimensional import (
    build_multidimensional_evaluations_from_provenance,
)
from htdt.pareto import ParetoError, pareto_front
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


NOW = '2026-09-28T00:00:00+00:00'
SOURCE = CriterionSource(
    publisher='CMP2 fixture',
    document_title='CMP2 verdict fixture',
    document_version='1.0',
    reference='cmp2',
)


# ----------------------------------------------------------------------
# Threshold honesty: the declared bound/inclusivity decides, exactly.
# ----------------------------------------------------------------------


def test_threshold_compare_honors_exact_bound_and_inclusivity() -> None:
    inclusive = CriterionRule(operator='max', maximum=0.5)
    exclusive = CriterionRule(
        operator='max', maximum=0.5, upper_inclusive=False
    )
    assert _compare(0.5, inclusive) is True
    assert _compare(0.5, exclusive) is False
    assert _compare(0.5 + 1e-15, inclusive) is False

    lower_inc = CriterionRule(operator='min', minimum=0.2)
    lower_exc = CriterionRule(
        operator='min', minimum=0.2, lower_inclusive=False
    )
    assert _compare(0.2, lower_inc) is True
    assert _compare(0.2, lower_exc) is False

    rng = CriterionRule(operator='range', minimum=0.2, maximum=0.5)
    assert _compare(0.2, rng) is True
    assert _compare(0.5, rng) is True
    assert _compare(0.2 - 1e-9, rng) is False
    assert _compare(0.5 + 1e-9, rng) is False

    # 'equals' is exact — no implicit tolerance is introduced.
    eq = CriterionRule(operator='equals', expected=0.3)
    assert _compare(0.3, eq) is True
    assert _compare(0.1 + 0.2, eq) is False
    assert _compare(0.3 + 1e-12, eq) is False


def test_threshold_angle_wrap_and_absolute_value_are_stated() -> None:
    wrapped = CriterionRule(
        operator='min', minimum=350.0, angle_wrap='unsigned_360'
    )
    # -5 deg ≡ 355 deg ≥ 350 — wrap applies to the observed value.
    assert _compare(-5.0, wrapped) is True
    plain = CriterionRule(operator='min', minimum=350.0)
    assert _compare(-5.0, plain) is False

    abs_rule = CriterionRule(
        operator='max', maximum=10.0, absolute_value=True
    )
    assert _compare(-9.0, abs_rule) is True
    assert _compare(-11.0, abs_rule) is False


# ----------------------------------------------------------------------
# Acoustic target verdicts: member → band → criterion aggregation.
# ----------------------------------------------------------------------


def _band() -> AcousticTargetBand:
    return AcousticTargetBand(
        band_id='125-250',
        frequency=FrequencyDomain(minimum_hz=125.0, maximum_hz=250.0),
    )


def _target_criterion(
    *,
    rule: CriterionRule,
    aggregation: str,
    members: tuple[str, ...] = ('seat-1', 'seat-2'),
) -> AcousticTargetCriterion:
    return AcousticTargetCriterion(
        criterion_id='t30-main',
        name='t30-main',
        metric='decay_t30',
        metric_version='iso3382-2:2008-method',
        origin='standard',
        source=SOURCE,
        bands=(_band(),),
        rule=rule,
        unit='s',
        aggregation=aggregation,
        population_entity_ids=members,
        required_capability='predicted_t30',
        role='objective',
        allowed_basis='predicted_or_measured',
    )


def _target_observation(
    seat: str, value: float
) -> AcousticTargetObservation:
    return AcousticTargetObservation(
        criterion_id='t30-main',
        band_id='125-250',
        observed_value=value,
        unit='s',
        evidence_basis='predicted',
        provided_capability='predicted_t30',
        entity_ids=(seat,),
    )


def _target_evaluation(
    criterion: AcousticTargetCriterion,
    observations: tuple[AcousticTargetObservation, ...],
):
    profile = build_acoustic_target_profile(
        profile_id='apt-cmp2',
        profile_version='1',
        name='CMP2 targets',
        document_id='doc-1',
        criteria=(criterion,),
        created_at_utc=NOW,
    )
    return evaluate_acoustic_targets(
        profile=profile,
        observations=observations,
        available_capabilities=('predicted_t30',),
        created_at_utc=NOW,
    )


def test_spatial_worst_verdict_is_the_worst_member_not_the_mean() -> None:
    # max 0.5: seat-2 at 0.6 is the worst member → band UNMET and the
    # limiting seat is disclosed, never hidden behind the mean.
    criterion = _target_criterion(
        rule=CriterionRule(operator='max', maximum=0.5),
        aggregation='spatial_worst',
    )
    evaluation = _target_evaluation(
        criterion,
        (
            _target_observation('seat-1', 0.30),
            _target_observation('seat-2', 0.60),
        ),
    )
    band = evaluation.results[0].band_results[0]
    assert band.verdict == 'UNMET'
    assert band.limiting_entity_id == 'seat-2'
    assert band.observed_value == pytest.approx(0.60)
    by_member = {item.entity_id: item for item in band.member_results}
    assert by_member['seat-1'].verdict == 'MET'
    assert by_member['seat-2'].verdict == 'UNMET'
    assert evaluation.results[0].verdict == 'UNMET'


def test_spatial_mean_verdicts_the_mean_with_worst_member_disclosed() -> None:
    criterion = _target_criterion(
        rule=CriterionRule(operator='max', maximum=0.5),
        aggregation='spatial_mean',
    )
    evaluation = _target_evaluation(
        criterion,
        (
            _target_observation('seat-1', 0.30),
            _target_observation('seat-2', 0.60),
        ),
    )
    band = evaluation.results[0].band_results[0]
    assert band.observed_value == pytest.approx(0.45)
    assert band.verdict == 'MET'
    # The member that would fail alone is still the disclosed limiter.
    assert band.limiting_entity_id == 'seat-2'


def test_per_position_requires_every_member() -> None:
    criterion = _target_criterion(
        rule=CriterionRule(operator='max', maximum=0.5),
        aggregation='per_position',
    )
    evaluation = _target_evaluation(
        criterion,
        (
            _target_observation('seat-1', 0.30),
            _target_observation('seat-2', 0.60),
        ),
    )
    band = evaluation.results[0].band_results[0]
    assert band.verdict == 'UNMET'
    assert band.limiting_entity_id == 'seat-2'


def test_missing_population_member_makes_band_not_evaluated() -> None:
    criterion = _target_criterion(
        rule=CriterionRule(operator='max', maximum=0.5),
        aggregation='spatial_mean',
        members=('seat-1', 'seat-2'),
    )
    evaluation = _target_evaluation(
        criterion,
        (_target_observation('seat-1', 0.30),),
    )
    band = evaluation.results[0].band_results[0]
    # A missing leg must never be silently averaged away.
    assert band.verdict == 'NOT_EVALUATED'
    assert evaluation.results[0].evaluability == 'UNKNOWN'
    assert evaluation.results[0].verdict == 'NOT_EVALUATED'
    by_member = {item.entity_id: item for item in band.member_results}
    assert by_member['seat-2'].status == 'missing'


def test_ambiguous_member_observation_is_not_silently_picked() -> None:
    criterion = _target_criterion(
        rule=CriterionRule(operator='max', maximum=0.5),
        aggregation='spatial_worst',
        members=('seat-1',),
    )
    evaluation = _target_evaluation(
        criterion,
        (
            _target_observation('seat-1', 0.30),
            _target_observation('seat-1', 0.60),
        ),
    )
    band = evaluation.results[0].band_results[0]
    assert band.verdict == 'NOT_EVALUATED'
    assert band.member_results[0].status == 'ambiguous'


def test_exactly_at_bound_member_verdict_follows_declared_inclusivity() -> None:
    inclusive = _target_criterion(
        rule=CriterionRule(operator='max', maximum=0.5),
        aggregation='spatial_mean',
        members=('seat-1',),
    )
    evaluation = _target_evaluation(
        inclusive, (_target_observation('seat-1', 0.5),)
    )
    assert evaluation.results[0].band_results[0].verdict == 'MET'

    exclusive = _target_criterion(
        rule=CriterionRule(
            operator='max', maximum=0.5, upper_inclusive=False
        ),
        aggregation='spatial_mean',
        members=('seat-1',),
    )
    evaluation = _target_evaluation(
        exclusive, (_target_observation('seat-1', 0.5),)
    )
    assert evaluation.results[0].band_results[0].verdict == 'UNMET'


# ----------------------------------------------------------------------
# Pareto dominance: direction is honored per objective.
# ----------------------------------------------------------------------


def _definition(
    objective_id: str, *, direction: str, unit: str = 'dB'
) -> ObjectiveDefinition:
    return ObjectiveDefinition(
        objective_id=objective_id,
        quantity=objective_id,
        unit=unit,
        direction=direction,
        valid_domain=ObjectiveValidDomain(kind='finite_real'),
        comparison_model_id='cmp2-model',
        comparison_model_version='1',
    )


def _vector(candidate: str, pairs) -> ObjectiveVector:
    return ObjectiveVector(
        candidate_id=candidate,
        metrics=tuple(
            ObjectiveMetric(
                objective_id=definition.objective_id,
                value=value,
                unit=definition.unit,
                direction=definition.direction,
                definition=definition,
            )
            for definition, value in pairs
        ),
    )


def test_pareto_dominance_honors_maximize_and_minimize() -> None:
    maximize = _definition('max.metric', direction='maximize', unit='dB')
    minimize = _definition('min.metric', direction='minimize', unit='dB')

    # A is better on both axes (higher max-metric, lower min-metric) → B
    # is dominated by A on the true direction of both metrics.
    a = _vector('cand-a', ((maximize, 90.0), (minimize, 2.0)))
    b = _vector('cand-b', ((maximize, 80.0), (minimize, 5.0)))
    result = pareto_front((a, b))
    assert result.non_dominated_candidate_ids == ('cand-a',)
    assert result.dominated_by['cand-b'] == ('cand-a',)
    assert result.algorithm_version == 'pareto-front-2'

    # Mixed tradeoff: neither dominates.
    c = _vector('cand-c', ((maximize, 95.0), (minimize, 6.0)))
    tied = pareto_front((a, c))
    assert 'cand-a' in tied.non_dominated_candidate_ids
    assert 'cand-c' in tied.non_dominated_candidate_ids


def test_pareto_rejects_missing_or_diverging_definition() -> None:
    definition = _definition('obj', direction='minimize')
    available = _vector('ok', ((definition, 1.0),))
    missing = ObjectiveVector(
        candidate_id='missing',
        metrics=(
            ObjectiveMetric(
                objective_id='obj',
                value=None,
                unit='dB',
                direction='minimize',
                state='missing',
                definition=definition,
            ),
        ),
    )
    # A candidate with a missing leg is never silently dropped from the
    # axes — the extraction fails closed.
    with pytest.raises(ParetoError):
        pareto_front((available, missing))

    other_definition = _definition('obj', direction='minimize', unit='s')
    divergent = ObjectiveVector(
        candidate_id='divergent',
        metrics=(
            ObjectiveMetric(
                objective_id='obj',
                value=1.0,
                unit='s',
                direction='minimize',
                definition=other_definition,
            ),
        ),
    )
    with pytest.raises(ParetoError):
        pareto_front((available, divergent))


# ----------------------------------------------------------------------
# Provider-version diff: symmetric newly passing AND newly failing.
# ----------------------------------------------------------------------


def _version_case(
    case_id: str, version: str, verdict: str
) -> ValidationCaseRecord:
    return ValidationCaseRecord(
        case_id=case_id,
        provider_id='solver-x',
        provider_version=version,
        geometry_class='rectangular',
        source_class='subwoofer',
        observable='magnitude_fr',
        band_low_hz=25.0,
        band_high_hz=80.0,
        evidence_level='owned_room_holdout',
        verdict=verdict,
        qualification='production_qualified',
        is_holdout=True,
        recorded_at_utc=NOW,
    )


def test_version_diff_transition_matrix_is_symmetric() -> None:
    transitions = {
        'pp': ('pass', 'pass', 'unchanged_pass'),
        'pf': ('pass', 'fail', 'newly_failing'),
        'pn': ('pass', 'not_applicable', 'not_rerun'),
        'fp': ('fail', 'pass', 'newly_passing'),
        'ff': ('fail', 'fail', 'unchanged_fail'),
        'fn': ('fail', 'not_applicable', 'not_rerun'),
        'np': ('not_applicable', 'pass', 'newly_passing'),
        # A case that gains a *failing* verdict under the new version is a
        # regression — it must surface beside newly_passing, not hide under
        # not_rerun (the module's own rule: never label a version better).
        'nf': ('not_applicable', 'fail', 'newly_failing'),
        'nn': ('not_applicable', 'not_applicable', 'not_rerun'),
        'gone': ('fail', None, 'removed_case'),
        'born': (None, 'fail', 'new_case'),
    }
    cases = []
    for case_id, (old_verdict, new_verdict, _expected) in transitions.items():
        if old_verdict is not None:
            cases.append(_version_case(case_id, 'v1', old_verdict))
        if new_verdict is not None:
            cases.append(_version_case(case_id, 'v2', new_verdict))
    deltas = compare_provider_versions(
        tuple(cases), provider_id='solver-x', old_version='v1',
        new_version='v2',
    )
    by_case = {delta.case_id: delta for delta in deltas}
    for case_id, (old_verdict, new_verdict, expected) in transitions.items():
        assert by_case[case_id].outcome == expected, case_id
        assert by_case[case_id].old_verdict == old_verdict
        assert by_case[case_id].new_verdict == new_verdict


# ----------------------------------------------------------------------
# Treatment comparison outcome compatibility.
# ----------------------------------------------------------------------


def _treatment_spec(tmp_path: Path):
    baseline = SceneRepository(tmp_path / 'cad.sqlite3').save(
        SceneDocument(
            document_id='cmp2-treatment',
            room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
            entities=(),
        ),
        parent_revision_id=None,
    ).revision
    candidates = tuple(
        build_treatment_design_candidate(
            baseline=baseline,
            label=label,
            role='no_treatment',
        )
        for label in ('cand-A', 'cand-B')
    )
    return build_treatment_design_comparison(
        name='cmp2', baseline=baseline, candidates=candidates
    )


def _candidate_outcome(
    candidate, *, evaluated: bool
) -> TreatmentCandidateOutcome:
    if evaluated:
        return TreatmentCandidateOutcome(
            candidate_id=candidate.candidate_id,
            candidate_sha256=candidate.candidate_sha256,
            availability='evaluated',
            evaluated_result_refs=(
                ExactExternalAuthorityRef(
                    authority_id=f'result:{candidate.label}',
                    authority_version='1',
                    semantic_hash_sha256='b' * 64,
                ),
            ),
        )
    return TreatmentCandidateOutcome(
        candidate_id=candidate.candidate_id,
        candidate_sha256=candidate.candidate_sha256,
        availability='unavailable',
        unavailable_reason='no evaluated result bound',
    )


def test_treatment_outcome_compatibility_counts_evaluated_candidates(
    tmp_path: Path,
) -> None:
    spec = _treatment_spec(tmp_path)
    cand_a, cand_b = spec.candidates

    all_evaluated = build_treatment_comparison_outcome(
        spec=spec,
        outcomes=(
            _candidate_outcome(cand_a, evaluated=True),
            _candidate_outcome(cand_b, evaluated=True),
        ),
        evaluated_at_utc=NOW,
    )
    assert all_evaluated.compatibility == 'compatible'

    one_missing = build_treatment_comparison_outcome(
        spec=spec,
        outcomes=(
            _candidate_outcome(cand_a, evaluated=True),
            _candidate_outcome(cand_b, evaluated=False),
        ),
        evaluated_at_utc=NOW,
    )
    assert one_missing.compatibility == 'partial'

    none_evaluated = build_treatment_comparison_outcome(
        spec=spec,
        outcomes=(
            _candidate_outcome(cand_a, evaluated=False),
            _candidate_outcome(cand_b, evaluated=False),
        ),
        evaluated_at_utc=NOW,
    )
    assert none_evaluated.compatibility == 'incompatible'

    # A candidate with no outcome at all counts as not evaluated.
    unbound = build_treatment_comparison_outcome(
        spec=spec,
        outcomes=(_candidate_outcome(cand_a, evaluated=True),),
        evaluated_at_utc=NOW,
    )
    assert unbound.compatibility == 'partial'
    assert any('cand-B' in reason for reason in unbound.compatibility_reasons)


# ----------------------------------------------------------------------
# Measurement comparison semantics: level compatibility verdicts.
# ----------------------------------------------------------------------


def _side(
    *,
    level_reference_kind: str = 'unknown',
    calibration_sha256: str | None = None,
    timing_group: str | None = None,
) -> ComparisonSideContext:
    return ComparisonSideContext(
        measurement_id='m',
        dataset_id='d',
        dataset_sha256='c' * 64,
        evidence_type='measured',
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash='d' * 64,
        measurement_entity_id='mlp',
        channel_role='FL',
        source_speaker_ids=('fl',),
        radiation_scope='full',
        routing_evidence='direct',
        level_reference_kind=level_reference_kind,
        calibration_sha256=calibration_sha256,
        timing_group=timing_group,
    )


def test_level_compatibility_matrix_is_conservative() -> None:
    absolute_a = _side(
        level_reference_kind='absolute_spl', calibration_sha256='e' * 64
    )
    absolute_b = _side(
        level_reference_kind='absolute_spl', calibration_sha256='f' * 64
    )
    semantics = derive_comparison_semantics(
        side_a=absolute_a, side_b=absolute_b
    )
    assert semantics.level_compatibility == 'absolute_level_comparable'
    assert semantics.absolute_level == 'available'

    # Different level semantics + no reference band → diagnostic only.
    relative = _side(level_reference_kind='relative_level')
    semantics = derive_comparison_semantics(
        side_a=absolute_a, side_b=relative
    )
    assert semantics.level_compatibility == 'diagnostic_only'
    assert semantics.absolute_level == 'unavailable'
    assert 'level_reference' in semantics.mismatches

    # The same pair with a stated normalization band upgrades only shape.
    semantics = derive_comparison_semantics(
        side_a=absolute_a,
        side_b=relative,
        reference_band_hz=(200.0, 400.0),
    )
    assert semantics.level_compatibility == 'normalized_shape_comparable'
    assert semantics.absolute_level == 'unavailable'
    assert semantics.normalized_shape == 'available'

    # 'incompatible' is declared in the type but is never emitted —
    # diagnostic_only is the floor verdict.
    seen = {
        derive_comparison_semantics(
            side_a=_side(level_reference_kind=left),
            side_b=_side(level_reference_kind=right),
        ).level_compatibility
        for left in ('unknown', 'absolute_spl', 'relative_level')
        for right in ('unknown', 'absolute_spl', 'relative_level')
    }
    assert 'incompatible' not in seen


def test_level_compatibility_requires_real_calibration_on_both_sides() -> None:
    no_calibration = _side(
        level_reference_kind='absolute_spl', calibration_sha256=None
    )
    calibrated = _side(
        level_reference_kind='absolute_spl', calibration_sha256='e' * 64
    )
    semantics = derive_comparison_semantics(
        side_a=no_calibration, side_b=calibrated
    )
    # One uncalibrated 'absolute_spl' side can never claim absolute level.
    assert semantics.level_compatibility == 'normalized_shape_comparable'
    assert semantics.absolute_level == 'unavailable'


# ----------------------------------------------------------------------
# Frequency-response diff: the displayed numbers are recomputed a - b.
# ----------------------------------------------------------------------


def test_comparison_diff_is_stored_a_minus_b_and_metrics_recompute() -> None:
    frequencies = tuple(20.0 * (2 ** (k / 3.0)) for k in range(12))
    a = FrequencyResponse(
        frequency_hz=frequencies,
        level_db=tuple(70.0 + k for k in range(12)),
    )
    b = FrequencyResponse(
        frequency_hz=frequencies,
        level_db=tuple(68.0 + 0.5 * k for k in range(12)),
    )
    result = compare_frequency_responses(a, b, 20.0, frequencies[-1])
    for ya, yb, diff in zip(result.a_db, result.b_db, result.difference_db):
        assert diff == pytest.approx(ya - yb)
    expected_mean = sum(result.difference_db) / len(result.difference_db)
    assert result.mean_difference_db == pytest.approx(expected_mean)
    expected_rms = (
        sum(value * value for value in result.difference_db)
        / len(result.difference_db)
    ) ** 0.5
    assert result.rms_difference_db == pytest.approx(expected_rms)

    # Too few shared points → metrics honestly absent, never zero-filled.
    a_narrow = FrequencyResponse(
        frequency_hz=(100.0, 200.0), level_db=(70.0, 70.0)
    )
    b_narrow = FrequencyResponse(
        frequency_hz=(198.0, 300.0), level_db=(69.0, 69.0)
    )
    sparse = compare_frequency_responses(a_narrow, b_narrow, 20.0, 20000.0)
    assert sparse.valid_points < 2
    assert sparse.mean_difference_db is None
    assert sparse.rms_difference_db is None


# ----------------------------------------------------------------------
# Robustness sampled_worst: direction decides which tail is adverse.
# ----------------------------------------------------------------------


def _robust_sample(
    index: int,
    *,
    step: str,
    metrics: tuple[tuple[ObjectiveDefinition, float], ...],
) -> PerturbationSample:
    return PerturbationSample.model_construct(
        schema_version=1,
        sample_id=f'sample-{index}',
        robustness_spec_id='spec-1',
        robustness_spec_sha256='a' * 64,
        candidate_id='cand-1',
        sample_index=index,
        axis_id=None,
        step=step,
        parameter_deltas={},
        perturbed_scene_content_hash='b' * 64,
        feasible=True,
        model_id='m',
        model_version='1',
        prediction_provider_id='provider',
        fidelity='fidelity',
        objective_evaluation_spec_sha256='c' * 64,
        prediction_result_ref=f'result-{index}',
        objective_vector=ObjectiveVector(
            candidate_id=f'sample-{index}',
            metrics=tuple(
                ObjectiveMetric(
                    objective_id=definition.objective_id,
                    value=value,
                    unit=definition.unit,
                    direction=definition.direction,
                    definition=definition,
                )
                for definition, value in metrics
            ),
        ),
        sample_sha256='d' * 64,
        created_at_utc=NOW,
    )


def test_sampled_worst_tracks_the_declared_direction() -> None:
    minimize = _definition('min.obj', direction='minimize')
    samples = (
        _robust_sample(0, step='nominal', metrics=((minimize, 1.0),)),
        _robust_sample(1, step='multidimensional', metrics=((minimize, 2.0),)),
        _robust_sample(2, step='multidimensional', metrics=((minimize, 0.5),)),
    )
    (evaluation,) = build_multidimensional_evaluations_from_provenance(
        robustness_spec_id='spec-1',
        robustness_spec_sha256='a' * 64,
        candidate_id='cand-1',
        expected_sample_ids=('sample-0', 'sample-1', 'sample-2'),
        samples=samples,
        sampling_provenance={'kind': 'cmp2'},
        created_at_utc=NOW,
    )
    # minimize → adverse tail is the maximum.
    assert evaluation.sampled_worst_sample_id == 'sample-1'
    assert evaluation.sampled_worst_value == pytest.approx(2.0)

    maximize = _definition('max.obj', direction='maximize')
    samples = (
        _robust_sample(0, step='nominal', metrics=((maximize, 80.0),)),
        _robust_sample(1, step='multidimensional', metrics=((maximize, 85.0),)),
        _robust_sample(2, step='multidimensional', metrics=((maximize, 78.0),)),
    )
    (evaluation,) = build_multidimensional_evaluations_from_provenance(
        robustness_spec_id='spec-1',
        robustness_spec_sha256='a' * 64,
        candidate_id='cand-1',
        expected_sample_ids=('sample-0', 'sample-1', 'sample-2'),
        samples=samples,
        sampling_provenance={'kind': 'cmp2'},
        created_at_utc=NOW,
    )
    # maximize → adverse tail is the minimum.
    assert evaluation.sampled_worst_sample_id == 'sample-2'
    assert evaluation.sampled_worst_value == pytest.approx(78.0)


# ----------------------------------------------------------------------
# Evidence reconciliation: symmetric tolerance + uncertainty bound.
# ----------------------------------------------------------------------


def test_reconciliation_bound_is_tolerance_plus_both_uncertainties() -> None:
    subject = build_evidence_subject(
        document_id='doc-1',
        subject_kind='metric',
        target=AuthorityRef(
            kind='measurement_point',
            ref_id='mlp',
            ref_sha256='e' * 64,
        ),
        attribute='seat_spl_db',
    )
    frame = AlignmentRef(
        frame_kind='capture_frame',
        frame_id='capture-A',
        frame_sha256='f' * 64,
    )
    pair = (
        build_observation(
            subject_id=subject.subject_id,
            source='measurement',
            source_ref='mic-a',
            source_sha256='a' * 64,
            value=80.0,
            unit='db',
            uncertainty=0.5,
            alignment=frame,
        ),
        build_observation(
            subject_id=subject.subject_id,
            source='derived',
            source_ref='model-b',
            source_sha256='b' * 64,
            value=81.4,
            unit='db',
            uncertainty=0.5,
            alignment=frame,
        ),
    )
    # delta = 1.4 ≤ tolerance 0.5 + 0.5 + 0.5 = 1.5 → consistent.
    consistent = reconcile_subject(
        subject,
        pair,
        tolerance=0.5,
        tolerance_unit='db',
        decided_at_utc=NOW,
    )
    assert consistent.outcome == 'consistent'

    conflict = reconcile_subject(
        subject,
        pair,
        tolerance=0.3,
        tolerance_unit='db',
        decided_at_utc=NOW,
    )
    assert conflict.outcome == 'conflict'
