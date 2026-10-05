"""REV55-MULTISUB regression tests: conventional multi-sub optimization (#569).

Covers the fail-closed evidence contract: sealed candidate/evaluation/
qualification/deployment records, per-band seat-to-seat metrics,
optimization/holdout partition enforcement, claim downgrades without
holdout evidence, holdout regression failure (overfit), common-fidelity
comparison, staged A-D attribution, observed-state deployment verification,
UNKNOWN honesty, and persistence re-verification.

Fixture map (issue #569 section 11):
  MSB10 single-sub baseline; MSB20 two fixed subs; MSB30 gain/delay/
  polarity optimized; MSB40 placement optimized; MSB50 bounded PEQ
  (delegated EQ qualification); MSB60 overfit must fail; MSB70 deployment
  verification.
"""

from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from htdt.cad_multi_sub_optimization import (
    DEFAULT_EVALUATION_BANDS_HZ,
    BoundedEqBinding,
    MultiSubEvaluationSpec,
    MultiSubEvaluatorIdentity,
    MultiSubSeatPartition,
    MultiSubSeatWeight,
    MultiSubSeatWeighting,
    MultiSubStageEntry,
    ObservedSubState,
    SeatResponseBinding,
    SubChannelBinding,
    assert_conventional_strategy,
    attribute_stage_transitions,
    build_multi_sub_candidate,
    build_multi_sub_deployment_verification,
    build_multi_sub_qualification,
    build_multi_sub_stage_comparison,
    evaluate_deployment,
    evaluate_multi_sub_candidate,
)
from htdt.cad_multi_sub_optimization_repository import (
    CadMultiSubOptimizationRepository,
    MultiSubConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, make_f1_scene
from htdt.comparison import FrequencyResponse


NOW = '2026-10-05T00:00:00Z'


def _evaluator(fidelity: str = 'fixture-grid96') -> MultiSubEvaluatorIdentity:
    return MultiSubEvaluatorIdentity(
        evaluator_id='fixture-evaluator',
        evaluator_version='1',
        model_id='fixture-model',
        model_version='1',
        fidelity=fidelity,
        evidence_scope='fixture_only',
        fixture_only=True,
        synthetic=True,
        production_eligible=False,
    )


def _spec(
    *,
    fidelity: str = 'fixture-grid96',
    with_target: bool = False,
) -> MultiSubEvaluationSpec:
    frequencies = tuple(20.0 * (1.05**i) for i in range(60))
    return MultiSubEvaluationSpec(
        requested_band_hz=(20.0, 160.0),
        evaluation_bands_hz=DEFAULT_EVALUATION_BANDS_HZ,
        target_frequency_hz=frequencies if with_target else (),
        target_level_db=tuple(70.0 for _ in frequencies) if with_target else (),
        evaluator=_evaluator(fidelity),
    )


def _partition() -> MultiSubSeatPartition:
    return MultiSubSeatPartition(
        optimization_seat_entity_ids=('seat-a', 'seat-b', 'seat-c'),
        holdout_seat_entity_ids=('seat-h1', 'seat-h2'),
        repeatability_position_ids=('repeat-1',),
    )


def _response(offset_db: float, wobble_db: float = 2.0) -> FrequencyResponse:
    frequencies = tuple(20.0 * (1.05**i) for i in range(60))
    levels = tuple(
        70.0 + offset_db + math.sin(f / 15.0) * wobble_db
        for f in frequencies
    )
    return FrequencyResponse(frequency_hz=frequencies, level_db=levels)


def _sub(
    entity_id: str,
    *,
    gain_db: float | None = 0.0,
    delay_s: float | None = 0.0,
    polarity: str = 'normal',
    installable: str = 'feasible',
    x_m: float = 0.5,
) -> SubChannelBinding:
    return SubChannelBinding(
        sub_entity_id=entity_id,
        position_state='declared',
        position=Position3(x_m=x_m, y_m=0.3, z_m=0.4),
        gain_db=gain_db,
        delay_s=delay_s,
        polarity=polarity,  # type: ignore[arg-type]
        installable=installable,  # type: ignore[arg-type]
    )


def _candidate(
    candidate_id: str,
    strategy: str,
    subs: tuple[SubChannelBinding, ...],
    *,
    partition: MultiSubSeatPartition | None = None,
    weighting: MultiSubSeatWeighting | None = None,
    eq_binding: BoundedEqBinding | None = None,
    optimized_variables: tuple = (),
    document_id: str = 'doc-1',
    scene_revision_id: str = 'rev-1',
) -> object:
    return build_multi_sub_candidate(
        candidate_id=candidate_id,
        document_id=document_id,
        scene_revision_id=scene_revision_id,
        scene_content_hash='a' * 64,
        system_variant_id='sv-1',
        system_variant_sha256='b' * 64,
        strategy=strategy,
        subs=subs,
        partition=partition or _partition(),
        weighting=weighting or MultiSubSeatWeighting(mode='uniform_declared'),
        eq_binding=eq_binding,
        optimized_variables=optimized_variables,
        objective_family_ids=('seat_to_seat_variation', 'region_mean_response'),
        created_at_utc=NOW,
    )


def _bindings(
    seat_ids: tuple[str, ...], role: str
) -> tuple[SeatResponseBinding, ...]:
    return tuple(
        SeatResponseBinding(
            seat_entity_id=seat_id,
            role=role,  # type: ignore[arg-type]
            evidence_kind='synthetic',
        )
        for seat_id in seat_ids
    )


def _evaluate(
    candidate,
    role: str,
    offsets: tuple[float, ...],
    *,
    evaluation_id: str,
    spec: MultiSubEvaluationSpec | None = None,
):
    partition = candidate.partition
    seat_ids = partition.seats_for(role)
    return evaluate_multi_sub_candidate(
        candidate,
        role,  # type: ignore[arg-type]
        _bindings(seat_ids, role),
        [_response(offset) for offset in offsets],
        spec or _spec(),
        evaluation_id=evaluation_id,
        created_at_utc=NOW,
    )


# --- MSB10: single-sub baseline -------------------------------------------


def test_msb10_single_sub_baseline_evaluation() -> None:
    candidate = _candidate(
        'cand-a', 'single_sub_baseline', (_sub('sub-front'),)
    )
    evaluation = _evaluate(
        candidate, 'optimization', (0.0, -4.0, 3.0), evaluation_id='ev-a'
    )
    assert evaluation.population == 'optimization'
    assert len(evaluation.band_metrics) == 3
    band = evaluation.band_metrics[0]
    assert band.mean_std_db is not None and band.mean_std_db > 2.0
    assert band.per_seat_mean_db is not None
    assert len(band.per_seat_mean_db) == 3
    # Raw per-seat observables are preserved verbatim.
    assert len(evaluation.member_levels_db) == 3
    assert evaluation.evaluation_sha256 == evaluation.evaluation_sha256


# --- MSB20/30/40/50: staged candidates -------------------------------------


def test_msb20_fixed_two_sub_candidate() -> None:
    candidate = _candidate(
        'cand-b',
        'multi_sub_fixed_layout',
        (_sub('sub-front'), _sub('sub-rear', x_m=4.0)),
    )
    evaluation = _evaluate(
        candidate, 'optimization', (0.0, -1.0, 0.5), evaluation_id='ev-b'
    )
    assert evaluation.band_metrics[0].mean_std_db < 1.0  # type: ignore[operator]


def test_msb30_alignment_and_msb40_placement_candidates() -> None:
    aligned = _candidate(
        'cand-c',
        'multi_sub_gain_delay_polarity_optimized',
        (
            _sub('sub-front'),
            _sub('sub-rear', gain_db=-3.0, delay_s=0.0042, polarity='inverted', x_m=4.0),
        ),
        optimized_variables=('gain_db', 'delay_s', 'polarity'),
    )
    placed = _candidate(
        'cand-d',
        'multi_sub_placement_optimized',
        (_sub('sub-front'), _sub('sub-side', x_m=2.4)),
        optimized_variables=('position_xyz',),
    )
    assert aligned.candidate_sha256 != placed.candidate_sha256
    # A placement change alone is a new identity.
    moved = _candidate(
        'cand-d2',
        'multi_sub_placement_optimized',
        (_sub('sub-front'), _sub('sub-side', x_m=2.5)),
        optimized_variables=('position_xyz',),
    )
    assert placed.candidate_sha256 != moved.candidate_sha256


def test_msb50_bounded_eq_candidate_requires_binding() -> None:
    with pytest.raises(ValueError, match='BoundedEqBinding'):
        _candidate(
            'cand-e',
            'multi_sub_with_bounded_eq',
            (_sub('sub-front'), _sub('sub-rear', x_m=4.0)),
        )
    eq = BoundedEqBinding(
        eq_authority_id='eq-auth-1',
        eq_authority_sha256='c' * 64,
        formulation='peq',
        filter_count=3,
        max_boost_db=6.0,
        max_cut_db=12.0,
        delegated_qualification_ref='eqp10-qualification-1',
    )
    candidate = _candidate(
        'cand-e',
        'multi_sub_with_bounded_eq',
        (_sub('sub-front'), _sub('sub-rear', x_m=4.0)),
        eq_binding=eq,
        optimized_variables=('peq',),
    )
    assert candidate.eq_binding is not None
    assert candidate.eq_binding.delegated_qualification_ref == (
        'eqp10-qualification-1'
    )


def test_eq_binding_rejected_on_non_eq_stages() -> None:
    eq = BoundedEqBinding(
        eq_authority_id='eq-auth-1', eq_authority_sha256='c' * 64
    )
    with pytest.raises(ValueError, match='bounded-EQ strategy'):
        _candidate(
            'cand-x',
            'multi_sub_gain_delay_polarity_optimized',
            (_sub('sub-front'), _sub('sub-rear', x_m=4.0)),
            eq_binding=eq,
        )


# --- strategy / vocabulary gates -------------------------------------------


def test_active_control_labels_fail_closed() -> None:
    for label in (
        'cross_channel_support_control',
        'wavefront_active_control',
        'external_proprietary_control',
    ):
        with pytest.raises(ValueError, match='active-control'):
            assert_conventional_strategy(label)
    with pytest.raises(ValueError, match='unknown'):
        assert_conventional_strategy('some_other_label')
    assert (
        assert_conventional_strategy('multi_sub_fixed_layout')
        == 'multi_sub_fixed_layout'
    )


def test_partition_disjointness_enforced() -> None:
    with pytest.raises(ValueError, match='both optimization and holdout'):
        MultiSubSeatPartition(
            optimization_seat_entity_ids=('seat-a', 'seat-b'),
            holdout_seat_entity_ids=('seat-b',),
        )


def test_unknown_channel_state_stays_unknown() -> None:
    candidate = _candidate(
        'cand-u',
        'multi_sub_fixed_layout',
        (_sub('sub-front', gain_db=None, delay_s=None, polarity='unknown'), _sub('sub-rear', x_m=4.0)),
    )
    sub = candidate.sub('sub-front')
    assert sub is not None
    assert sub.gain_db is None and sub.delay_s is None
    assert sub.polarity == 'unknown'


def test_weight_change_is_new_identity() -> None:
    weighted = MultiSubSeatWeighting(
        mode='explicit_weights',
        weights=tuple(
            MultiSubSeatWeight(seat_entity_id=s, weight=w)
            for s, w in zip(
                ('seat-a', 'seat-b', 'seat-c'), (2.0, 1.0, 1.0)
            )
        ),
    )
    a = _candidate(
        'cand-w1', 'multi_sub_fixed_layout',
        (_sub('sub-front'), _sub('sub-rear', x_m=4.0)),
    )
    b = _candidate(
        'cand-w2', 'multi_sub_fixed_layout',
        (_sub('sub-front'), _sub('sub-rear', x_m=4.0)),
        weighting=weighted,
    )
    assert a.candidate_sha256 != b.candidate_sha256


def test_explicit_weights_must_cover_optimization_seats() -> None:
    partial = MultiSubSeatWeighting(
        mode='explicit_weights',
        weights=(MultiSubSeatWeight(seat_entity_id='seat-a', weight=1.0),),
    )
    with pytest.raises(ValueError, match='cover all optimization seats'):
        _candidate(
            'cand-wx', 'multi_sub_fixed_layout',
            (_sub('sub-front'), _sub('sub-rear', x_m=4.0)),
            weighting=partial,
        )


# --- qualification ---------------------------------------------------------


def _qualified_pair():
    baseline = _candidate(
        'cand-a', 'single_sub_baseline', (_sub('sub-front'),)
    )
    candidate = _candidate(
        'cand-c',
        'multi_sub_gain_delay_polarity_optimized',
        (
            _sub('sub-front'),
            _sub('sub-rear', gain_db=-3.0, delay_s=0.0042, polarity='inverted', x_m=4.0),
        ),
    )
    base_opt = _evaluate(
        baseline, 'optimization', (0.0, -4.0, 3.0), evaluation_id='ev-a-o'
    )
    base_hold = _evaluate(
        baseline, 'holdout', (2.0, -3.0), evaluation_id='ev-a-h'
    )
    cand_opt = _evaluate(
        candidate, 'optimization', (0.0, -1.0, 0.5), evaluation_id='ev-c-o'
    )
    cand_hold = _evaluate(
        candidate, 'holdout', (0.3, -0.4), evaluation_id='ev-c-h'
    )
    return baseline, candidate, base_opt, cand_opt, base_hold, cand_hold


def test_listening_region_claim_qualifies_with_holdout() -> None:
    baseline, candidate, base_opt, cand_opt, base_hold, cand_hold = (
        _qualified_pair()
    )
    qualification = build_multi_sub_qualification(
        qualification_id='qual-1',
        baseline=baseline,
        candidate=candidate,
        claim='listening_region',
        baseline_optimization=base_opt,
        candidate_optimization=cand_opt,
        baseline_holdout=base_hold,
        candidate_holdout=cand_hold,
        created_at_utc=NOW,
    )
    assert qualification.verdict == 'qualified'
    assert qualification.effective_claim == 'listening_region'
    populations = {delta.population for delta in qualification.improvements}
    assert populations == {'optimization', 'holdout'}
    # Seat-to-seat std improved at both populations.
    opt_deltas = [
        d.delta_mean_std_db
        for d in qualification.improvements
        if d.population == 'optimization' and d.scope == 'band'
    ]
    assert opt_deltas and all(delta < 0 for delta in opt_deltas)


def test_listening_region_claim_downgrades_without_holdout() -> None:
    baseline, candidate, base_opt, cand_opt, _, _ = _qualified_pair()
    qualification = build_multi_sub_qualification(
        qualification_id='qual-2',
        baseline=baseline,
        candidate=candidate,
        claim='listening_region',
        baseline_optimization=base_opt,
        candidate_optimization=cand_opt,
        created_at_utc=NOW,
    )
    assert qualification.verdict == 'qualified_with_limitations'
    assert qualification.effective_claim == 'optimization_seats'
    assert any(
        r.code == 'missing_holdout_evidence' for r in qualification.reasons
    )


def test_msb60_overfit_fails_on_holdout_regression() -> None:
    baseline, candidate, base_opt, cand_opt, base_hold, _ = _qualified_pair()
    overfit_holdout = _evaluate(
        candidate,
        'holdout',
        (6.0, -7.0),  # fitted seats look great; unseen seats collapse
        evaluation_id='ev-c-h-bad',
    )
    qualification = build_multi_sub_qualification(
        qualification_id='qual-3',
        baseline=baseline,
        candidate=candidate,
        claim='listening_region',
        baseline_optimization=base_opt,
        candidate_optimization=cand_opt,
        baseline_holdout=base_hold,
        candidate_holdout=overfit_holdout,
        created_at_utc=NOW,
    )
    assert qualification.verdict == 'failed_holdout_regression'
    assert qualification.effective_claim == 'optimization_seats'
    assert any(
        r.code == 'holdout_seat_consistency_regression'
        for r in qualification.reasons
    )


def test_fidelity_mismatch_is_incomparable() -> None:
    baseline, candidate, base_opt, cand_opt, base_hold, _ = _qualified_pair()
    cheap = _evaluate(
        candidate,
        'holdout',
        (0.3, -0.4),
        evaluation_id='ev-c-h-cheap',
        spec=_spec(fidelity='cheap-screening'),
    )
    qualification = build_multi_sub_qualification(
        qualification_id='qual-4',
        baseline=baseline,
        candidate=candidate,
        claim='listening_region',
        baseline_optimization=base_opt,
        candidate_optimization=cand_opt,
        baseline_holdout=base_hold,
        candidate_holdout=cheap,
        created_at_utc=NOW,
    )
    assert qualification.verdict == 'incomparable_fidelity'


def test_qualification_requires_same_partition() -> None:
    baseline, candidate, base_opt, cand_opt, _, _ = _qualified_pair()
    other_partition = MultiSubSeatPartition(
        optimization_seat_entity_ids=('seat-a', 'seat-b', 'seat-z'),
        holdout_seat_entity_ids=('seat-h1', 'seat-h2'),
    )
    other = _candidate(
        'cand-z',
        'multi_sub_gain_delay_polarity_optimized',
        (_sub('sub-front'), _sub('sub-rear', x_m=4.0)),
        partition=other_partition,
    )
    with pytest.raises(ValueError, match='identical'):
        build_multi_sub_qualification(
            qualification_id='qual-5',
            baseline=baseline,
            candidate=other,
            claim='optimization_seats',
            baseline_optimization=base_opt,
            candidate_optimization=cand_opt,
            created_at_utc=NOW,
        )


def test_single_seat_claim_needs_one_seat_partition() -> None:
    baseline, candidate, base_opt, cand_opt, _, _ = _qualified_pair()
    with pytest.raises(ValueError, match='one-seat'):
        build_multi_sub_qualification(
            qualification_id='qual-6',
            baseline=baseline,
            candidate=candidate,
            claim='single_seat',
            baseline_optimization=base_opt,
            candidate_optimization=cand_opt,
            created_at_utc=NOW,
        )


def test_hash_tamper_fails_closed() -> None:
    baseline, candidate, base_opt, cand_opt, _, _ = _qualified_pair()
    qualification = build_multi_sub_qualification(
        qualification_id='qual-7',
        baseline=baseline,
        candidate=candidate,
        claim='optimization_seats',
        baseline_optimization=base_opt,
        candidate_optimization=cand_opt,
        created_at_utc=NOW,
    )
    payload = qualification.model_dump()
    payload['verdict'] = 'qualified'  # tamper: pardon the verdict
    with pytest.raises(ValueError, match='hash mismatch'):
        type(qualification)(**payload)


# --- staged comparison ------------------------------------------------------


def test_staged_comparison_attributes_per_family() -> None:
    baseline, candidate, base_opt, cand_opt, _, _ = _qualified_pair()
    entries = (
        MultiSubStageEntry(
            stage='a_single_sub_baseline',
            candidate_id=baseline.candidate_id,
            candidate_sha256=baseline.candidate_sha256,
            evaluation_id=base_opt.evaluation_id,
            evaluation_sha256=base_opt.evaluation_sha256,
        ),
        MultiSubStageEntry(
            stage='c_gain_delay_polarity',
            candidate_id=candidate.candidate_id,
            candidate_sha256=candidate.candidate_sha256,
            evaluation_id=cand_opt.evaluation_id,
            evaluation_sha256=cand_opt.evaluation_sha256,
        ),
    )
    attributions = attribute_stage_transitions(entries, (base_opt, cand_opt))
    assert attributions
    band_rows = [a for a in attributions if a.scope == 'band']
    assert len(band_rows) == len(DEFAULT_EVALUATION_BANDS_HZ)
    assert all(a.delta_mean_std_db < 0 for a in band_rows)  # type: ignore[operator]
    comparison = build_multi_sub_stage_comparison(
        comparison_id='cmp-1',
        document_id='doc-1',
        scene_revision_id='rev-1',
        entries=entries,
        evaluations=(base_opt, cand_opt),
        partition_sha256=baseline.partition.partition_sha256,
        created_at_utc=NOW,
    )
    assert comparison.comparison_sha256


def test_staged_comparison_rejects_out_of_order() -> None:
    baseline, candidate, base_opt, cand_opt, _, _ = _qualified_pair()
    entries = (
        MultiSubStageEntry(
            stage='c_gain_delay_polarity',
            candidate_id=candidate.candidate_id,
            candidate_sha256=candidate.candidate_sha256,
            evaluation_id=cand_opt.evaluation_id,
            evaluation_sha256=cand_opt.evaluation_sha256,
        ),
        MultiSubStageEntry(
            stage='a_single_sub_baseline',
            candidate_id=baseline.candidate_id,
            candidate_sha256=baseline.candidate_sha256,
            evaluation_id=base_opt.evaluation_id,
            evaluation_sha256=base_opt.evaluation_sha256,
        ),
    )
    with pytest.raises(ValueError, match='canonical'):
        build_multi_sub_stage_comparison(
            comparison_id='cmp-2',
            document_id='doc-1',
            scene_revision_id='rev-1',
            entries=entries,
            evaluations=(base_opt, cand_opt),
            partition_sha256=baseline.partition.partition_sha256,
            created_at_utc=NOW,
        )


# --- MSB70: deployment verification ------------------------------------------


def test_msb70_deployment_verified_when_observed_and_remeasured() -> None:
    baseline, candidate, base_opt, cand_opt, base_hold, cand_hold = (
        _qualified_pair()
    )
    qualification = build_multi_sub_qualification(
        qualification_id='qual-8',
        baseline=baseline,
        candidate=candidate,
        claim='listening_region',
        baseline_optimization=base_opt,
        candidate_optimization=cand_opt,
        baseline_holdout=base_hold,
        candidate_holdout=cand_hold,
        created_at_utc=NOW,
    )
    observed = (
        ObservedSubState(
            sub_entity_id='sub-front',
            gain_db=0.0,
            delay_s=0.0,
            polarity='normal',
            position=Position3(x_m=0.5, y_m=0.3, z_m=0.4),
        ),
        ObservedSubState(
            sub_entity_id='sub-rear',
            gain_db=-3.0,
            delay_s=0.0042,
            polarity='inverted',
            position=Position3(x_m=4.0, y_m=0.3, z_m=0.4),
        ),
    )
    remeasurement = (
        _bindings(candidate.partition.optimization_seat_entity_ids, 'optimization')
        + _bindings(candidate.partition.holdout_seat_entity_ids, 'holdout')
    )
    verification = build_multi_sub_deployment_verification(
        verification_id='dep-1',
        candidate=candidate,
        qualification=qualification,
        observed_subs=observed,
        remeasurement_seats=remeasurement,
        post_deployment_optimization=cand_opt,
        post_deployment_holdout=cand_hold,
        created_at_utc=NOW,
    )
    assert verification.verdict == 'verified'


def test_deployment_incomplete_without_remeasurement() -> None:
    baseline, candidate, base_opt, cand_opt, base_hold, cand_hold = (
        _qualified_pair()
    )
    observed = (
        ObservedSubState(sub_entity_id='sub-front', gain_db=0.0, delay_s=0.0, polarity='normal', position=Position3(x_m=0.5, y_m=0.3, z_m=0.4)),
        ObservedSubState(sub_entity_id='sub-rear', gain_db=-3.0, delay_s=0.0042, polarity='inverted', position=Position3(x_m=4.0, y_m=0.3, z_m=0.4)),
    )
    partial = _bindings(
        candidate.partition.optimization_seat_entity_ids, 'optimization'
    )
    verdict, reasons = evaluate_deployment(
        candidate=candidate,
        qualification=None,
        observed_subs=observed,
        remeasurement_seats=partial,
        post_deployment_optimization=cand_opt,
        post_deployment_holdout=cand_hold,
    )
    assert verdict == 'verification_incomplete'
    assert any(r.code == 'remeasurement_incomplete' for r in reasons)


def test_deployment_infeasible_installation() -> None:
    baseline, candidate, base_opt, cand_opt, base_hold, cand_hold = (
        _qualified_pair()
    )
    infeasible = _candidate(
        'cand-bad',
        'multi_sub_gain_delay_polarity_optimized',
        (
            _sub('sub-front'),
            _sub('sub-rear', gain_db=-3.0, delay_s=0.0042, polarity='inverted', x_m=4.0, installable='infeasible'),
        ),
    )
    observed = (
        ObservedSubState(sub_entity_id='sub-front', gain_db=0.0, delay_s=0.0, polarity='normal', position=Position3(x_m=0.5, y_m=0.3, z_m=0.4)),
        ObservedSubState(sub_entity_id='sub-rear', installed=False),
    )
    verdict, reasons = evaluate_deployment(
        candidate=infeasible,
        qualification=None,
        observed_subs=observed,
        remeasurement_seats=(),
        post_deployment_optimization=None,
        post_deployment_holdout=None,
    )
    assert verdict == 'infeasible_installation'
    assert any(r.code == 'infeasible_installation' for r in reasons)


def test_deployment_observed_state_mismatch() -> None:
    baseline, candidate, base_opt, cand_opt, base_hold, cand_hold = (
        _qualified_pair()
    )
    observed = (
        ObservedSubState(sub_entity_id='sub-front', gain_db=0.0, delay_s=0.0, polarity='normal', position=Position3(x_m=0.5, y_m=0.3, z_m=0.4)),
        ObservedSubState(sub_entity_id='sub-rear', gain_db=-1.0, delay_s=0.0042, polarity='inverted'),
    )
    verdict, reasons = evaluate_deployment(
        candidate=candidate,
        qualification=None,
        observed_subs=observed,
        remeasurement_seats=(),
        post_deployment_optimization=None,
        post_deployment_holdout=None,
    )
    assert verdict == 'observed_state_mismatch'
    assert any(r.code == 'observed_state_mismatch' for r in reasons)


def test_deployment_incomplete_without_observed_sub() -> None:
    baseline, candidate, *_ = _qualified_pair()
    verdict, reasons = evaluate_deployment(
        candidate=candidate,
        qualification=None,
        observed_subs=(
            ObservedSubState(
                sub_entity_id='sub-front', gain_db=0.0, delay_s=0.0,
                polarity='normal',
                position=Position3(x_m=0.5, y_m=0.3, z_m=0.4),
            ),
        ),
        remeasurement_seats=(),
        post_deployment_optimization=None,
        post_deployment_holdout=None,
    )
    assert verdict == 'verification_incomplete'
    assert any(r.code == 'sub_not_observed' for r in reasons)


# --- persistence ------------------------------------------------------------


def _repository(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    repository = CadMultiSubOptimizationRepository(scene_repository)
    return scene_repository, revision, repository


def test_persistence_round_trip_and_conflict(tmp_path: Path) -> None:
    scene_repository, revision, repository = _repository(tmp_path)
    # Scene content hash must match the persisted revision.
    baseline = build_multi_sub_candidate(
        candidate_id='cand-a',
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id='sv-1',
        system_variant_sha256='b' * 64,
        strategy='single_sub_baseline',
        subs=(_sub('sub-front'),),
        partition=_partition(),
        objective_family_ids=('seat_to_seat_variation',),
        created_at_utc=NOW,
    )
    repository.save_candidate(baseline)
    repository.save_candidate(baseline)  # idempotent
    loaded = repository.get_candidate(baseline.candidate_id)
    assert loaded is not None
    assert loaded == baseline

    candidate = build_multi_sub_candidate(
        candidate_id='cand-c',
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id='sv-1',
        system_variant_sha256='b' * 64,
        strategy='multi_sub_gain_delay_polarity_optimized',
        subs=(
            _sub('sub-front'),
            _sub('sub-rear', gain_db=-3.0, delay_s=0.0042, polarity='inverted', x_m=4.0),
        ),
        partition=_partition(),
        objective_family_ids=('seat_to_seat_variation',),
        created_at_utc=NOW,
    )
    repository.save_candidate(candidate)

    base_opt = _evaluate(
        baseline, 'optimization', (0.0, -4.0, 3.0), evaluation_id='ev-a-o'
    )
    cand_opt = _evaluate(
        candidate, 'optimization', (0.0, -1.0, 0.5), evaluation_id='ev-c-o'
    )
    repository.save_evaluation(base_opt)
    repository.save_evaluation(cand_opt)
    assert repository.get_evaluation('ev-c-o') == cand_opt

    qualification = build_multi_sub_qualification(
        qualification_id='qual-9',
        baseline=baseline,
        candidate=candidate,
        claim='optimization_seats',
        baseline_optimization=base_opt,
        candidate_optimization=cand_opt,
        created_at_utc=NOW,
    )
    repository.save_qualification(qualification)
    assert repository.get_qualification('qual-9') == qualification

    # Conflict: same id, different content.
    other = build_multi_sub_candidate(
        candidate_id='cand-a',
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id='sv-1',
        system_variant_sha256='b' * 64,
        strategy='single_sub_baseline',
        subs=(_sub('sub-front', gain_db=-1.0),),
        partition=_partition(),
        objective_family_ids=('seat_to_seat_variation',),
        created_at_utc=NOW,
    )
    with pytest.raises(MultiSubConflictError):
        repository.save_candidate(other)


def test_persistence_verifies_stored_rows(tmp_path: Path) -> None:
    scene_repository, revision, repository = _repository(tmp_path)
    candidate = build_multi_sub_candidate(
        candidate_id='cand-t',
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id='sv-1',
        system_variant_sha256='b' * 64,
        strategy='multi_sub_fixed_layout',
        subs=(_sub('sub-front'), _sub('sub-rear', x_m=4.0)),
        partition=_partition(),
        objective_family_ids=('seat_to_seat_variation',),
        created_at_utc=NOW,
    )
    repository.save_candidate(candidate)
    # Corrupt the strategy column — the read must fail closed.
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            "UPDATE cad_multi_sub_candidates SET strategy='wavefront' "
            'WHERE candidate_id=?',
            (candidate.candidate_id,),
        )
    with pytest.raises(ValueError, match='disagrees'):
        repository.get_candidate(candidate.candidate_id)


def test_candidate_pins_persisted_scene_revision(tmp_path: Path) -> None:
    scene_repository, revision, repository = _repository(tmp_path)
    candidate = build_multi_sub_candidate(
        candidate_id='cand-orphan',
        document_id=revision.document_id,
        scene_revision_id='rev-does-not-exist',
        scene_content_hash=revision.content_hash,
        system_variant_id='sv-1',
        system_variant_sha256='b' * 64,
        strategy='single_sub_baseline',
        subs=(_sub('sub-front'),),
        partition=_partition(),
        objective_family_ids=('seat_to_seat_variation',),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_candidate(candidate)
