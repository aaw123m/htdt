"""REV58-VALIDMETH regression tests — #675 optimizer algorithm
qualification, #674 acoustic eigenmode / mode-shape validation, #673
sound-field diffuseness / statistical-model applicability, #671
coupled-room multi-slope decay, #677 predicted↔measured early-reflection
correspondence, #706 time-frequency modal-decay authority.

Fixtures (OPT/MOD/DFF/CPL/RPA/MDT) exercise the fail-closed rules the
issues demand: a stochastic best-found is not a global optimum, a mode
frequency is not a mode, an RT is not diffuseness, a coupled room is
not one T30, a nearest ETC peak is not a wall, and a waterfall picture
is not a decay constant.
"""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

from htdt.cad_optimizer_qualification import (
    BaselineDeclaration,
    DecisionVariableSpec,
    IndependentRunRecord,
    KnownOptimumFixturePin,
    MultiFidelityPolicy,
    ObjectiveDefinition,
    OptimizerAlgorithmSpec,
    ParetoIndicator,
    SearchBudget,
    build_optimization_problem,
    build_optimizer_run_profile,
    evaluate_optimizer_qualification,
    evaluate_pareto_approximation,
    problem_binding,
)
from htdt.cad_optimizer_qualification_repository import (
    CadOptimizerQualificationRepository,
    OptimizerQualificationIntegrityError,
)
from htdt.cad_eigenmode_validation import (
    MeasuredModalEvidencePin,
    ModeShapeComparisonSpec,
    PredictedEigenmodePin,
    ShapeComparisonResult,
    build_mode_pairing,
    evaluate_eigenmode_validation,
)
from htdt.cad_eigenmode_validation_repository import (
    CadEigenmodeValidationRepository,
    EigenmodeValidationIntegrityError,
)
from htdt.cad_diffuseness_applicability import (
    DiffuseFieldDomainPin,
    DiffusenessEvidenceRef,
    FieldUniformityProfile,
    build_diffuseness_assessment,
    evaluate_diffuseness_applicability,
)
from htdt.cad_diffuseness_applicability_repository import (
    CadDiffusenessApplicabilityRepository,
    DiffusenessApplicabilityIntegrityError,
)
from htdt.cad_coupled_decay import (
    CoupledDecayProfile,
    DecayComponent,
    build_multi_slope_fit,
    evaluate_coupled_decay,
    evaluate_single_slope_adequacy,
)
from htdt.cad_coupled_decay_repository import (
    CadCoupledDecayRepository,
    CoupledDecayIntegrityError,
)
from htdt.cad_reflection_correspondence import (
    ObservedReflectionEvent,
    PredictedReflectionPath,
    build_correspondence_set,
    build_reflection_pairing,
    evaluate_reflection_correspondence,
)
from htdt.cad_reflection_correspondence_repository import (
    CadReflectionCorrespondenceRepository,
    ReflectionCorrespondenceIntegrityError,
)
from htdt.cad_modal_decay_view import (
    TimeFrequencyDecayTransform,
    build_modal_decay_observation,
    evaluate_modal_decay_qualification,
    transform_binding,
)
from htdt.cad_modal_decay_view_repository import (
    CadModalDecayViewRepository,
    ModalDecayIntegrityError,
)


DOC = 'doc-rev58-validmeth'
T0 = '2026-10-06T00:00:00+00:00'
T1 = '2026-10-06T01:00:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64
SHA_D = 'd' * 64
SHA_E = 'e' * 64

SOLVER_REF = AuthorityRef(
    kind='wave_solver', ref_id='solver-1', ref_sha256=SHA_A
)
MEAS_REF = AuthorityRef(
    kind='impulse_response_measurement',
    ref_id='rir-1',
    ref_sha256=SHA_B,
)
REGION_A = AuthorityRef(
    kind='acoustic_region', ref_id='region-a', ref_sha256=SHA_C
)
REGION_B = AuthorityRef(
    kind='acoustic_region', ref_id='region-b', ref_sha256=SHA_D
)
PORTAL_REF = AuthorityRef(
    kind='acoustic_portal_coupling',
    ref_id='portal-1',
    ref_sha256=SHA_E,
)
REGISTRATION_REF = AuthorityRef(
    kind='source_receiver_registration',
    ref_id='reg-1',
    ref_sha256=SHA_A,
)
SURFACE_REF = AuthorityRef(
    kind='surface', ref_id='wall-north', ref_sha256=SHA_B
)


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _tamper(db_path, sql: str, params=()) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# #675 — optimizer algorithm qualification (OPT fixtures)
# ---------------------------------------------------------------------------


def _opt_problem():
    return build_optimization_problem(
        document_id=DOC,
        problem_label='absorber placement sweep',
        decision_variables=(
            DecisionVariableSpec(
                name='panel_x', kind='continuous', bounds='0..4 m'
            ),
        ),
        objectives=(
            ObjectiveDefinition(
                objective_id='obj-decay-dev',
                direction='minimize',
                unit='s',
            ),
        ),
        declared_at_utc=T0,
    )


def _algorithm(
    family: str = 'cma_es',
    *,
    stochastic: bool = True,
) -> OptimizerAlgorithmSpec:
    return OptimizerAlgorithmSpec(
        family=family,
        implementation='pycma' if family == 'cma_es' else 'htdt-search',
        implementation_version='3.3.0',
        parameters=('popsize=12', 'sigma0=0.3'),
        stochastic=stochastic,
        seed_policy='seeded_per_run' if stochastic else None,
        termination_rule='budget_exhausted',
    )


def _run(label: str, seed: int | None, **kw) -> IndependentRunRecord:
    return IndependentRunRecord(
        run_label=label,
        seed=seed,
        budget=kw.pop('budget', SearchBudget(function_evaluations=200)),
        best_objective_value=kw.pop('best_objective_value', 0.42),
        convergence_state=kw.pop(
            'convergence_state', 'converged_by_declared_criterion'
        ),
        **kw,
    )


def _profile(**kw):
    problem = _opt_problem()
    return build_optimizer_run_profile(
        document_id=DOC,
        problem_ref=problem_binding(problem),
        algorithm=kw.pop('algorithm', _algorithm()),
        declared_budget=kw.pop(
            'declared_budget',
            SearchBudget(function_evaluations=200),
        ),
        runs=kw.pop('runs', (_run('r1', 11), _run('r2', 22))),
        baselines=kw.pop(
            'baselines',
            (
                BaselineDeclaration(
                    baseline_family='random_search',
                    equal_budget=True,
                    outcome_summary='cma beats random at equal budget',
                ),
            ),
        ),
        **kw,
    )


def test_opt10_single_stochastic_run_never_reports_seed_robustness():
    profile = _profile(runs=(_run('r1', 11),))
    qual = evaluate_optimizer_qualification(
        DOC, profile, evaluated_at_utc=T1
    )
    assert qual.state == 'qualified_with_limitations'
    assert qual.seed_variability_observed is None
    assert any('seed' in lim for lim in qual.limitations)


def test_opt20_unqualified_global_claim_collapses():
    profile = _profile(
        optimality_claim_requested='global_optimum_proven',
    )
    qual = evaluate_optimizer_qualification(
        DOC, profile, evaluated_at_utc=T1
    )
    assert qual.optimality_claim == 'best_found_under_budget'
    assert qual.state == 'qualified_with_limitations'
    assert any('global optimality' in r for r in qual.reasons)


def test_opt30_exhaustive_enumeration_can_prove_optimum():
    profile = _profile(
        algorithm=_algorithm(
            'exhaustive_enumeration', stochastic=False
        ),
        runs=(
            IndependentRunRecord(
                run_label='sweep',
                seed=None,
                budget=SearchBudget(function_evaluations=1024),
                best_objective_value=0.30,
                convergence_state='converged_by_declared_criterion',
            ),
        ),
        known_optimum_fixtures=(
            KnownOptimumFixturePin(
                fixture_id='OPT20',
                known_optimum_basis='exhaustive_enumeration',
                recovered=True,
                regret=0.0,
            ),
        ),
        optimality_claim_requested='global_optimum_proven',
    )
    qual = evaluate_optimizer_qualification(
        DOC, profile, evaluated_at_utc=T1
    )
    assert qual.state == 'qualified'
    assert qual.optimality_claim == 'global_optimum_proven'
    assert qual.known_optimum_recovered == 1


def test_opt40_missed_known_optimum_is_unqualified():
    profile = _profile(
        known_optimum_fixtures=(
            KnownOptimumFixturePin(
                fixture_id='OPT30',
                known_optimum_basis='analytic',
                recovered=False,
                regret=0.17,
            ),
        ),
    )
    qual = evaluate_optimizer_qualification(
        DOC, profile, evaluated_at_utc=T1
    )
    assert qual.state == 'unqualified'
    assert qual.regression_flags


def test_opt50_unbenchmarked_search_is_capped():
    profile = _profile(baselines=(), known_optimum_fixtures=())
    qual = evaluate_optimizer_qualification(
        DOC, profile, evaluated_at_utc=T1
    )
    assert qual.state == 'qualified_with_limitations'
    assert qual.baselines_compared == 0
    assert any('unbenchmarked' in lim for lim in qual.limitations)


def test_opt60_multifidelity_without_common_fidelity_final():
    profile = _profile(
        multi_fidelity=MultiFidelityPolicy(
            screening_fidelity='low_fidelity',
            final_fidelity='high_fidelity',
            finalist_promotion_rule='top-4',
            common_fidelity_final_evaluation=False,
        ),
    )
    qual = evaluate_optimizer_qualification(
        DOC, profile, evaluated_at_utc=T1
    )
    assert qual.state == 'qualified_with_limitations'
    assert qual.common_fidelity_finalized is False


def test_opt70_pareto_without_reference_is_empirical_only():
    problem = _opt_problem()
    profile = build_optimizer_run_profile(
        document_id=DOC,
        problem_ref=problem_binding(problem),
        algorithm=_algorithm('evolutionary_multiobjective'),
        declared_budget=SearchBudget(function_evaluations=400),
        runs=(
            IndependentRunRecord(
                run_label='r1',
                seed=5,
                budget=SearchBudget(function_evaluations=400),
                final_front_ref=AuthorityRef(
                    kind='front', ref_id='fr-1', ref_sha256=SHA_C
                ),
            ),
            IndependentRunRecord(
                run_label='r2',
                seed=6,
                budget=SearchBudget(function_evaluations=400),
                final_front_ref=AuthorityRef(
                    kind='front', ref_id='fr-2', ref_sha256=SHA_D
                ),
            ),
        ),
        multi_objective=True,
    )
    no_ref = evaluate_pareto_approximation(
        DOC,
        profile,
        reference_status='no_reference_front',
        convergence_indicators=(
            ParetoIndicator(
                indicator='hypervolume',
                value=0.62,
                reference_point='1.0,1.0',
            ),
        ),
        nondominated_count=14,
        evaluated_at_utc=T1,
    )
    assert no_ref.state == 'assessed_empirical_only'

    # A reference-front indicator without a reference front is
    # structurally inconsistent.
    front_ref = AuthorityRef(
        kind='reference_front', ref_id='ref-front', ref_sha256=SHA_E
    )
    bad = evaluate_pareto_approximation(
        DOC,
        profile,
        reference_status='no_reference_front',
        convergence_indicators=(
            ParetoIndicator(
                indicator='inverted_generational_distance',
                value=0.01,
                reference_front_ref=front_ref,
            ),
        ),
        evaluated_at_utc=T1,
    )
    assert bad.state == 'insufficient_evidence'

    full = evaluate_pareto_approximation(
        DOC,
        profile,
        reference_status='exact_reference_front',
        convergence_indicators=(
            ParetoIndicator(
                indicator='inverted_generational_distance',
                value=0.02,
                reference_front_ref=front_ref,
            ),
        ),
        diversity_indicators=(
            ParetoIndicator(
                indicator='spacing_spread', value=0.81
            ),
        ),
        nondominated_count=16,
        evaluated_at_utc=T1,
    )
    assert full.state == 'assessed_with_reference'


def test_opt75_hypervolume_requires_reference_point():
    with pytest.raises(ValueError):
        ParetoIndicator(indicator='hypervolume', value=0.5)


def test_opt80_optimizer_repository_roundtrip_and_integrity(tmp_path):
    scene = _scene_repo(tmp_path)
    repo = CadOptimizerQualificationRepository(scene)

    problem = _opt_problem()
    repo.save_problem(problem)
    assert repo.get_problem(problem.problem_id) == problem
    assert repo.list_problems(DOC) == (problem,)

    profile = _profile()
    repo.save_profile(profile)
    assert repo.get_profile(profile.profile_id) == profile

    qual = evaluate_optimizer_qualification(
        DOC, profile, evaluated_at_utc=T1
    )
    repo.save_qualification(qual)
    assert repo.get_qualification(qual.qualification_id) == qual

    repo.save_problem(problem)  # idempotent re-save

    _tamper(
        scene.path,
        "UPDATE cad_optimizer_qualifications SET state='unqualified' "
        'WHERE qualification_id=?',
        (qual.qualification_id,),
    )
    with pytest.raises(OptimizerQualificationIntegrityError):
        repo.get_qualification(qual.qualification_id)


# ---------------------------------------------------------------------------
# #674 — acoustic eigenmode / mode-shape validation (MOD fixtures)
# ---------------------------------------------------------------------------


def _predicted_mode():
    return PredictedEigenmodePin(
        solver_ref=SOLVER_REF,
        eigenfrequency_hz=63.4,
        modal_decay_s=0.8,
        solver_mode_index=4,
        normalization_convention='unit_mass',
    )


def _measured_mode():
    return MeasuredModalEvidencePin(
        measurement_ref=MEAS_REF,
        identification_algorithm='polyfreq',
        algorithm_version='1.2.0',
        identified_frequency_hz=63.6,
        identified_decay_s=0.82,
        uncertainty_hz=0.4,
    )


def _shape_spec():
    return ModeShapeComparisonSpec(
        metric='mac_style_spatial_correlation',
        normalization='unit_max',
        phase_ambiguity_policy='global_sign_free',
        acceptance_band=(0.9, 1.0),
        metric_version='mac-1.0',
    )


def _pairing(**kw):
    return build_mode_pairing(
        document_id=DOC,
        pairing_state=kw.pop('pairing_state', 'paired_high_confidence'),
        pairing_algorithm='joint_freq_shape_assignment',
        pairing_algorithm_version='1.0.0',
        predicted_mode_ref=kw.pop(
            'predicted_mode_ref',
            AuthorityRef(
                kind='predicted_eigenmode',
                ref_id='pmode-4',
                ref_sha256=SHA_C,
            ),
        ),
        measured_mode_ref=kw.pop(
            'measured_mode_ref',
            AuthorityRef(
                kind='measured_modal_evidence',
                ref_id='mmode-1',
                ref_sha256=SHA_D,
            ),
        ),
        evidence_dimensions=kw.pop(
            'evidence_dimensions',
            ('frequency_proximity', 'mode_shape_similarity'),
        ),
        **kw,
    )


def test_mod10_clean_pairing_validates():
    pairing = _pairing(
        frequency_error_hz=0.2,
        shape_comparison=ShapeComparisonResult(
            spec=_shape_spec(), value=0.94, meets_declared_band=True
        ),
        participation='mode_visible',
    )
    verdict = evaluate_eigenmode_validation(
        DOC,
        pairing,
        declared_frequency_tolerance_hz=0.5,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'eigenmode_validated'
    assert verdict.shape_agreement == 'agree'
    assert verdict.frequency_agreement == 'agree'


def test_mod20_frequency_alone_never_validates():
    # A freq-only pairing cannot even be declared high-confidence
    # (record-level fail-close); however weak its declared confidence,
    # the verdict is the specific frequency-only cap, and an unpaired
    # state stays insufficient.
    pairing = _pairing(
        pairing_state='paired_with_ambiguity',
        evidence_dimensions=('frequency_proximity',),
        frequency_error_hz=0.1,
    )
    verdict = evaluate_eigenmode_validation(
        DOC,
        pairing,
        declared_frequency_tolerance_hz=0.5,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'frequency_only_match_insufficient'

    pairing2 = _pairing(
        pairing_state='paired_with_ambiguity',
        evidence_dimensions=('frequency_proximity',),
        frequency_error_hz=0.1,
        ambiguity_note='two measured peaks within tolerance',
    )
    verdict2 = evaluate_eigenmode_validation(
        DOC,
        pairing2,
        declared_frequency_tolerance_hz=0.5,
        evaluated_at_utc=T1,
    )
    assert verdict2.state == 'frequency_only_match_insufficient'

    unpaired = _pairing(
        pairing_state='unpaired_predicted',
        measured_mode_ref=None,
        evidence_dimensions=(),
    )
    verdict3 = evaluate_eigenmode_validation(
        DOC, unpaired, evaluated_at_utc=T1
    )
    assert verdict3.state == 'insufficient_evidence'


def test_mod30_shape_below_band_is_shape_mismatch():
    pairing = _pairing(
        frequency_error_hz=0.1,
        shape_comparison=ShapeComparisonResult(
            spec=_shape_spec(), value=0.6, meets_declared_band=False
        ),
    )
    verdict = evaluate_eigenmode_validation(
        DOC,
        pairing,
        declared_frequency_tolerance_hz=0.5,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'shape_mismatch'
    assert verdict.shape_agreement == 'disagree'


def test_mod35_frequency_disagree_is_frequency_mismatch():
    pairing = _pairing(
        frequency_error_hz=1.7,
        shape_comparison=ShapeComparisonResult(
            spec=_shape_spec(), value=0.94, meets_declared_band=True
        ),
    )
    verdict = evaluate_eigenmode_validation(
        DOC,
        pairing,
        declared_frequency_tolerance_hz=0.5,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'frequency_mismatch'
    assert verdict.frequency_agreement == 'disagree'


def test_mod40_degenerate_subspace_is_honest():
    pairing = _pairing(
        pairing_state='degenerate_subspace_match',
        subspace_members=(
            AuthorityRef(
                kind='predicted_eigenmode',
                ref_id='pmode-4',
                ref_sha256=SHA_C,
            ),
            AuthorityRef(
                kind='predicted_eigenmode',
                ref_id='pmode-5',
                ref_sha256=SHA_D,
            ),
        ),
        evidence_dimensions=(
            'frequency_proximity',
            'mode_shape_similarity',
        ),
        frequency_error_hz=0.2,
        shape_comparison=ShapeComparisonResult(
            spec=_shape_spec(), value=0.93, meets_declared_band=True
        ),
    )
    verdict = evaluate_eigenmode_validation(
        DOC,
        pairing,
        declared_frequency_tolerance_hz=0.5,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'eigenmode_validated_with_limitations'
    assert verdict.shape_agreement == 'subspace_evaluated'


def test_mod50_calibration_role_contaminates():
    pairing = _pairing(
        frequency_error_hz=0.1,
        shape_comparison=ShapeComparisonResult(
            spec=_shape_spec(), value=0.95, meets_declared_band=True
        ),
        validation_role='calibration',
    )
    verdict = evaluate_eigenmode_validation(
        DOC,
        pairing,
        declared_frequency_tolerance_hz=0.5,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'eigenmode_validated_with_limitations'
    assert verdict.calibration_contaminated is True


def test_mod60_damping_residual_beyond_tolerance_mismatches():
    pairing = _pairing(
        frequency_error_hz=0.1,
        damping_error_s=0.4,
        shape_comparison=ShapeComparisonResult(
            spec=_shape_spec(), value=0.93, meets_declared_band=True
        ),
    )
    verdict = evaluate_eigenmode_validation(
        DOC,
        pairing,
        declared_frequency_tolerance_hz=0.5,
        declared_damping_tolerance_s=0.1,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'damping_mismatch'
    assert verdict.damping_agreement == 'disagree'


def test_mod70_eigenmode_repository_roundtrip_and_integrity(tmp_path):
    scene = _scene_repo(tmp_path)
    repo = CadEigenmodeValidationRepository(scene)

    pairing = _pairing(frequency_error_hz=0.2)
    repo.save_pairing(pairing)
    assert repo.get_pairing(pairing.pairing_id) == pairing
    assert repo.list_pairings(DOC) == (pairing,)

    verdict = evaluate_eigenmode_validation(
        DOC, pairing, evaluated_at_utc=T1
    )
    repo.save_verdict(verdict)
    assert repo.get_verdict(verdict.verdict_id) == verdict

    _tamper(
        scene.path,
        "UPDATE cad_eigenmode_verdicts SET state='eigenmode_validated' "
        'WHERE verdict_id=?',
        (verdict.verdict_id,),
    )
    with pytest.raises(EigenmodeValidationIntegrityError):
        repo.get_verdict(verdict.verdict_id)


# ---------------------------------------------------------------------------
# #673 — diffuseness / statistical-model applicability (DFF fixtures)
# ---------------------------------------------------------------------------


def _domain(label: str = 'audience plane'):
    return DiffuseFieldDomainPin(
        spatial_extent_label=label,
        frequency_band_hz=(250.0, 4000.0),
        position_class='audience',
    )


def _measured_profile(prop: str = 'energy_uniformity'):
    return FieldUniformityProfile(
        estimator='spatial_level_variance',
        covered_property=prop,
        domain=_domain(),
        evidence=(
            DiffusenessEvidenceRef(
                evidence_ref=AuthorityRef(
                    kind='spatial_survey',
                    ref_id='survey-1',
                    ref_sha256=SHA_C,
                ),
                provenance='measured',
            ),
        ),
        value=1.1,
        confidence_interval=(0.9, 1.3),
        estimator_version='spl-var-1.0',
    )


def test_dff10_no_assessment_is_unsupported():
    decl = evaluate_diffuseness_applicability(
        DOC,
        None,
        use_domain=_domain(),
        needed_properties=('energy_uniformity',),
        evaluated_at_utc=T1,
    )
    assert decl.state == 'insufficient_evidence'
    assert decl.basis == 'unsupported'


def test_dff20_measured_full_coverage_applies():
    assessment = build_diffuseness_assessment(
        document_id=DOC,
        domain=_domain(),
        eligibility_state='eligible_within_declared_domain',
        estimator_profiles=(_measured_profile(),),
        covered_properties=('energy_uniformity',),
        declared_at_utc=T0,
    )
    decl = evaluate_diffuseness_applicability(
        DOC,
        assessment,
        use_domain=_domain(),
        needed_properties=('energy_uniformity',),
        evaluated_at_utc=T1,
    )
    assert decl.state == 'applicable_within_domain'
    assert decl.basis == 'direct_measured_diffuseness_evaluation'


def test_dff30_theory_only_never_claims_direct():
    assessment = build_diffuseness_assessment(
        document_id=DOC,
        domain=_domain(),
        eligibility_state='eligible_within_declared_domain',
        estimator_profiles=(
            FieldUniformityProfile(
                estimator='model_internal_diffuseness_indicator',
                covered_property='position_invariance',
                domain=_domain(),
                evidence=(
                    DiffusenessEvidenceRef(
                        evidence_ref=AuthorityRef(
                            kind='model_note',
                            ref_id='note-1',
                            ref_sha256=SHA_D,
                        ),
                        provenance='theory_assumption',
                    ),
                ),
                estimator_version='model-ind-0.1',
            ),
        ),
        covered_properties=('position_invariance',),
        declared_at_utc=T0,
    )
    decl = evaluate_diffuseness_applicability(
        DOC,
        assessment,
        use_domain=_domain(),
        needed_properties=('position_invariance',),
        evaluated_at_utc=T1,
    )
    assert decl.state == 'applicable_with_limitations'
    assert decl.basis == 'theory_assumption_and_limits'


def test_dff40_uncovered_needed_property_limits():
    assessment = build_diffuseness_assessment(
        document_id=DOC,
        domain=_domain(),
        eligibility_state='eligible_within_declared_domain',
        estimator_profiles=(_measured_profile('energy_uniformity'),),
        covered_properties=('energy_uniformity',),
        declared_at_utc=T0,
    )
    decl = evaluate_diffuseness_applicability(
        DOC,
        assessment,
        use_domain=_domain(),
        needed_properties=('energy_uniformity', 'directional_isotropy'),
        evaluated_at_utc=T1,
    )
    assert decl.state == 'applicable_with_limitations'
    assert any(
        'directional_isotropy' in lim for lim in decl.limitations
    )


def test_dff50_non_diffuse_field_is_not_applicable():
    assessment = build_diffuseness_assessment(
        document_id=DOC,
        domain=_domain(),
        eligibility_state='non_diffuse_field',
        estimator_profiles=(_measured_profile(),),
        covered_properties=('energy_uniformity',),
        declared_at_utc=T0,
    )
    decl = evaluate_diffuseness_applicability(
        DOC,
        assessment,
        use_domain=_domain(),
        needed_properties=('energy_uniformity',),
        evaluated_at_utc=T1,
    )
    assert decl.state == 'not_applicable'


def test_dff60_biased_field_blocks_isotropy_claim():
    assessment = build_diffuseness_assessment(
        document_id=DOC,
        domain=_domain(),
        eligibility_state='directionally_biased',
        estimator_profiles=(
            FieldUniformityProfile(
                estimator='intensity_based_diffuseness',
                covered_property='directional_isotropy',
                domain=_domain(),
                value=0.31,
                estimator_version='psi-1.0',
            ),
        ),
        covered_properties=('directional_isotropy',),
        declared_at_utc=T0,
    )
    decl = evaluate_diffuseness_applicability(
        DOC,
        assessment,
        use_domain=_domain(),
        needed_properties=('directional_isotropy',),
        evaluated_at_utc=T1,
    )
    assert decl.state == 'not_applicable'


def test_dff70_diffuseness_repository_roundtrip_and_integrity(tmp_path):
    scene = _scene_repo(tmp_path)
    repo = CadDiffusenessApplicabilityRepository(scene)

    assessment = build_diffuseness_assessment(
        document_id=DOC,
        domain=_domain(),
        eligibility_state='eligible_within_declared_domain',
        estimator_profiles=(_measured_profile(),),
        covered_properties=('energy_uniformity',),
        declared_at_utc=T0,
    )
    repo.save_assessment(assessment)
    assert repo.get_assessment(assessment.assessment_id) == assessment

    decl = evaluate_diffuseness_applicability(
        DOC,
        assessment,
        use_domain=_domain(),
        needed_properties=('energy_uniformity',),
        evaluated_at_utc=T1,
    )
    repo.save_declaration(decl)
    assert repo.get_declaration(decl.declaration_id) == decl

    _tamper(
        scene.path,
        "UPDATE cad_diffuseness_assessments "
        "SET eligibility_state='non_diffuse_field' "
        'WHERE assessment_id=?',
        (assessment.assessment_id,),
    )
    with pytest.raises(DiffusenessApplicabilityIntegrityError):
        repo.get_assessment(assessment.assessment_id)


# ---------------------------------------------------------------------------
# #671 — coupled-room multi-slope decay (CPL fixtures)
# ---------------------------------------------------------------------------


def _profile_coupled(exchange: bool = True):
    return CoupledDecayProfile(
        region_refs=(REGION_A, REGION_B),
        portal_refs=(PORTAL_REF,),
        solver_ref=SOLVER_REF,
        solver_supports_energy_exchange=exchange,
    )


def _adequacy(state_kw):
    return evaluate_single_slope_adequacy(
        DOC,
        raw_evidence_ref=MEAS_REF,
        evaluated_at_utc=T1,
        **state_kw,
    )


def test_cpl10_noise_floor_limited():
    a = _adequacy(
        dict(
            curvature_evidence_declared=False,
            dynamic_range_db=50.0,
            noise_floor_limited=True,
        )
    )
    assert a.adequacy_state == 'noise_limited'


def test_cpl20_short_range_cannot_exonerate_single_slope():
    a = _adequacy(
        dict(
            curvature_evidence_declared=False,
            dynamic_range_db=30.0,
            noise_floor_limited=False,
        )
    )
    assert a.adequacy_state == 'insufficient_range'


def test_cpl30_declared_curvature_supports_multi_slope():
    a = _adequacy(
        dict(
            curvature_evidence_declared=True,
            dynamic_range_db=60.0,
            noise_floor_limited=False,
        )
    )
    assert a.adequacy_state == 'multi_slope_supported'


def test_cpl40_clean_decay_is_single_slope_adequate():
    a = _adequacy(
        dict(
            curvature_evidence_declared=False,
            dynamic_range_db=60.0,
            noise_floor_limited=False,
        )
    )
    assert a.adequacy_state == 'single_slope_adequate'


def test_cpl50_solver_without_exchange_rejects_collapse():
    profile = _profile_coupled(exchange=False)
    qual = evaluate_coupled_decay(
        DOC,
        profile,
        adequacy_assessments=(
            _adequacy(
                dict(
                    curvature_evidence_declared=True,
                    dynamic_range_db=60.0,
                    noise_floor_limited=False,
                )
            ),
        ),
        evaluated_at_utc=T1,
    )
    assert qual.state == 'single_slope_collapse_rejected'


def test_cpl60_multi_slope_fit_qualifies_double_slope():
    profile = _profile_coupled()
    fit = build_multi_slope_fit(
        document_id=DOC,
        raw_evidence_ref=MEAS_REF,
        model_class='double_slope_model',
        model_version='eyring-pair-1.0',
        components=(
            DecayComponent(
                role='early', decay_rate_s=0.4, initial_level_db=55.0
            ),
            DecayComponent(
                role='late', decay_rate_s=1.4, initial_level_db=40.0
            ),
        ),
        declared_at_utc=T0,
    )
    qual = evaluate_coupled_decay(
        DOC,
        profile,
        adequacy_assessments=(
            _adequacy(
                dict(
                    curvature_evidence_declared=True,
                    dynamic_range_db=60.0,
                    noise_floor_limited=False,
                )
            ),
        ),
        multi_slope_fits=(fit,),
        evaluated_at_utc=T1,
    )
    assert qual.state == 'qualified'
    assert qual.behavior_state == 'coupled_volume_double_slope'
    assert qual.multi_slope_fit_refs


def test_cpl70_single_region_all_adequate_is_single_exponential():
    profile = CoupledDecayProfile(
        region_refs=(REGION_A,),
        solver_ref=SOLVER_REF,
        solver_supports_energy_exchange=True,
        position_subset_refs=(
            AuthorityRef(
                kind='position_set', ref_id='pos-1', ref_sha256=SHA_C
            ),
        ),
    )
    qual = evaluate_coupled_decay(
        DOC,
        profile,
        adequacy_assessments=(
            _adequacy(
                dict(
                    curvature_evidence_declared=False,
                    dynamic_range_db=60.0,
                    noise_floor_limited=False,
                )
            ),
        ),
        evaluated_at_utc=T1,
    )
    assert qual.state == 'qualified'
    assert qual.behavior_state == 'single_exponential_decay'
    assert qual.limitations  # position-scoped, never a room RT


def test_cpl75_no_adequacy_evidence_is_insufficient():
    qual = evaluate_coupled_decay(
        DOC, _profile_coupled(), evaluated_at_utc=T1
    )
    assert qual.state == 'insufficient_evidence'


def test_cpl80_coupled_repository_roundtrip_and_integrity(tmp_path):
    scene = _scene_repo(tmp_path)
    repo = CadCoupledDecayRepository(scene)

    fit = build_multi_slope_fit(
        document_id=DOC,
        raw_evidence_ref=MEAS_REF,
        model_class='double_slope_model',
        model_version='eyring-pair-1.0',
        components=(
            DecayComponent(role='early', decay_rate_s=0.4),
            DecayComponent(role='late', decay_rate_s=1.4),
        ),
        declared_at_utc=T0,
    )
    repo.save_fit(fit)
    assert repo.get_fit(fit.fit_id) == fit

    a = _adequacy(
        dict(
            curvature_evidence_declared=True,
            dynamic_range_db=60.0,
            noise_floor_limited=False,
        )
    )
    repo.save_assessment(a)
    assert repo.get_assessment(a.assessment_id) == a

    qual = evaluate_coupled_decay(
        DOC,
        _profile_coupled(),
        adequacy_assessments=(a,),
        multi_slope_fits=(fit,),
        evaluated_at_utc=T1,
    )
    repo.save_qualification(qual)
    assert repo.get_qualification(qual.qualification_id) == qual

    _tamper(
        scene.path,
        "UPDATE cad_coupled_decay_qualifications "
        "SET behavior_state='single_exponential_decay' "
        'WHERE qualification_id=?',
        (qual.qualification_id,),
    )
    with pytest.raises(CoupledDecayIntegrityError):
        repo.get_qualification(qual.qualification_id)


# ---------------------------------------------------------------------------
# #677 — early-reflection correspondence (RPA fixtures)
# ---------------------------------------------------------------------------


def _predicted_path():
    return PredictedReflectionPath(
        solver_ref=SOLVER_REF,
        interaction_refs=(SURFACE_REF,),
        path_class='specular',
        predicted_arrival_s=0.0123,
        reflection_order=1,
        predicted_level_db=-18.0,
        arrival_direction=(0.0, 1.0, 0.0),
    )


def _observed_event():
    return ObservedReflectionEvent(
        measurement_ref=MEAS_REF,
        extraction_algorithm='etc_peak_gate',
        extraction_version='etc-2.1',
        observed_time_s=0.0125,
        observed_doa=(0.02, 0.999, 0.0),
        observed_level_db=-18.5,
    )


def _rfx_pairing(**kw):
    return build_reflection_pairing(
        document_id=DOC,
        correspondence_state=kw.pop('correspondence_state', 'one_to_one'),
        matching_algorithm=kw.pop(
            'matching_algorithm', 'time_doa_geometric_match'
        ),
        matching_algorithm_version='matcher-1.0',
        predicted_path=kw.pop('predicted_path', _predicted_path()),
        observed_event=kw.pop('observed_event', _observed_event()),
        evidence_dimensions=kw.pop(
            'evidence_dimensions',
            (
                'time_alignment',
                'direction_of_arrival',
                'level_compatibility',
            ),
        ),
        **kw,
    )


def _rfx_set(pairings):
    return build_correspondence_set(
        document_id=DOC,
        registration_ref=REGISTRATION_REF,
        pairings=pairings,
        declared_at_utc=T0,
    )


def test_rpa10_missing_registration_fails_closed():
    pairing = _rfx_pairing()
    cs = _rfx_set((pairing,))
    verdict = evaluate_reflection_correspondence(
        DOC, cs, (pairing,),
        registration_valid=False, evaluated_at_utc=T1,
    )
    assert verdict.state == 'registration_prerequisite_missing'


def test_rpa20_time_only_pairing_is_ambiguous():
    pairing = _rfx_pairing(
        matching_algorithm='time_gate_peak_match',
        evidence_dimensions=('time_alignment',),
    )
    cs = _rfx_set((pairing,))
    verdict = evaluate_reflection_correspondence(
        DOC, cs, (pairing,), registration_valid=True,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'ambiguous_unresolved'


def test_rpa30_compound_evidence_qualifies():
    pairing = _rfx_pairing()
    cs = _rfx_set((pairing,))
    verdict = evaluate_reflection_correspondence(
        DOC, cs, (pairing,), registration_valid=True,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'qualified'
    assert verdict.matched_pair_count == 1


def test_rpa40_calibration_consumed_pairs_cannot_validate():
    pairing = _rfx_pairing(validation_role='calibration')
    cs = _rfx_set((pairing,))
    verdict = evaluate_reflection_correspondence(
        DOC, cs, (pairing,), registration_valid=True,
        evaluated_at_utc=T1,
    )
    assert verdict.state == (
        'calibration_only_no_independent_validation'
    )


def test_rpa50_unmatched_side_is_recorded_not_hidden():
    p_match = _rfx_pairing()
    p_unmatched = build_reflection_pairing(
        document_id=DOC,
        correspondence_state='unmatched_observed',
        matching_algorithm='time_doa_geometric_match',
        matching_algorithm_version='matcher-1.0',
        observed_event=_observed_event(),
        evidence_dimensions=('time_alignment',),
        declared_at_utc=T0,
    )
    cs = _rfx_set((p_match, p_unmatched))
    verdict = evaluate_reflection_correspondence(
        DOC, cs, (p_match, p_unmatched), registration_valid=True,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'qualified_with_limitations'
    assert verdict.unmatched_observed_count == 1


def test_rpa60_correspondence_repository_roundtrip_and_integrity(
    tmp_path,
):
    scene = _scene_repo(tmp_path)
    repo = CadReflectionCorrespondenceRepository(scene)

    pairing = _rfx_pairing()
    repo.save_pairing(pairing)
    assert repo.get_pairing(pairing.pairing_id) == pairing

    cs = _rfx_set((pairing,))
    repo.save_set(cs)
    assert repo.get_set(cs.set_id) == cs

    verdict = evaluate_reflection_correspondence(
        DOC, cs, (pairing,), registration_valid=True,
        evaluated_at_utc=T1,
    )
    repo.save_verdict(verdict)
    assert repo.get_verdict(verdict.verdict_id) == verdict

    _tamper(
        scene.path,
        "UPDATE cad_reflection_correspondence_verdicts "
        "SET matched_pair_count=999 WHERE verdict_id=?",
        (verdict.verdict_id,),
    )
    with pytest.raises(ReflectionCorrespondenceIntegrityError):
        repo.get_verdict(verdict.verdict_id)


# ---------------------------------------------------------------------------
# #706 — time-frequency modal decay (MDT fixtures)
# ---------------------------------------------------------------------------


def _transform():
    return TimeFrequencyDecayTransform(
        transform_kind='morlet_cwt',
        wavelet_parameters='morlet:bandwidth=6',
        frequency_grid='40..200Hz:1Hz',
        normalization='unit_area',
        parameter_hash=SHA_C,
        time_resolution_s=0.02,
        frequency_resolution_hz=1.0,
        trustworthy_time_range_s=(0.05, 1.5),
        trustworthy_frequency_range_hz=(45.0, 195.0),
    )


def _observation(**kw):
    return build_modal_decay_observation(
        document_id=DOC,
        transform=_transform(),
        raw_evidence_ref=MEAS_REF,
        mode_candidate_center_hz=kw.pop(
            'mode_candidate_center_hz', 63.0
        ),
        overlap_state=kw.pop('overlap_state', 'isolated_mode'),
        envelope_semantic=kw.pop(
            'envelope_semantic', 'energy_envelope'
        ),
        fit_model=kw.pop('fit_model', 'single_exponential'),
        ridge_range_hz=kw.pop('ridge_range_hz', (61.0, 65.0)),
        fitted_decay_s=kw.pop('fitted_decay_s', 0.78),
        fit_uncertainty_s=kw.pop('fit_uncertainty_s', 0.05),
        position_ref=kw.pop(
            'position_ref',
            AuthorityRef(
                kind='receiver_position',
                ref_id='rx-1',
                ref_sha256=SHA_D,
            ),
        ),
        **kw,
    )


def test_mdt10_isolated_mode_with_fit_qualifies():
    obs = _observation()
    qual = evaluate_modal_decay_qualification(
        DOC, obs, evaluated_at_utc=T1
    )
    assert qual.state == 'qualified'
    assert qual.decay_trustworthy is True


def test_mdt20_picture_without_fit_is_not_evidence():
    obs = _observation(fitted_decay_s=None, fit_uncertainty_s=None)
    qual = evaluate_modal_decay_qualification(
        DOC, obs, evaluated_at_utc=T1
    )
    assert qual.state == 'insufficient_evidence'
    assert qual.decay_trustworthy is False


def test_mdt30_unresolved_overlap_is_not_a_mode_decay():
    obs = _observation(
        overlap_state='unresolved_multiple_modes',
        fit_model='multi_exponential_declared',
    )
    qual = evaluate_modal_decay_qualification(
        DOC, obs, evaluated_at_utc=T1
    )
    assert qual.state == 'overlap_unresolved'
    assert qual.decay_trustworthy is False


def test_mdt40_noise_floor_blocks_truthful_decay():
    obs = _observation()
    qual = evaluate_modal_decay_qualification(
        DOC, obs, noise_floor_limited=True, evaluated_at_utc=T1
    )
    assert qual.state == 'noise_or_truncation_limited'
    assert qual.decay_trustworthy is False


def test_mdt50_partial_overlap_is_limited():
    obs = _observation(overlap_state='partially_overlapped')
    qual = evaluate_modal_decay_qualification(
        DOC, obs, evaluated_at_utc=T1
    )
    assert qual.state == 'qualified_with_limitations'
    assert qual.decay_trustworthy is True


def test_mdt60_missing_uncertainty_limits():
    obs = _observation(fit_uncertainty_s=None)
    qual = evaluate_modal_decay_qualification(
        DOC, obs, evaluated_at_utc=T1
    )
    assert qual.state == 'qualified_with_limitations'


def test_mdt70_transform_binding_is_content_addressed():
    t1 = _transform()
    t2 = _transform()
    assert transform_binding(t1) == transform_binding(t2)
    t3 = t1.model_copy(
        update={'frequency_resolution_hz': 2.0}
    )
    assert transform_binding(t3) != transform_binding(t1)


def test_mdt80_modal_decay_repository_roundtrip_and_integrity(tmp_path):
    scene = _scene_repo(tmp_path)
    repo = CadModalDecayViewRepository(scene)

    obs = _observation()
    repo.save_observation(obs)
    assert repo.get_observation(obs.observation_id) == obs

    qual = evaluate_modal_decay_qualification(
        DOC, obs, evaluated_at_utc=T1
    )
    repo.save_qualification(qual)
    assert repo.get_qualification(qual.qualification_id) == qual

    _tamper(
        scene.path,
        "UPDATE cad_modal_decay_qualifications "
        'SET decay_trustworthy=0 WHERE qualification_id=?',
        (qual.qualification_id,),
    )
    with pytest.raises(ModalDecayIntegrityError):
        repo.get_qualification(qual.qualification_id)


# ---------------------------------------------------------------------------
# Cross-cutting: schema, audit replay, display
# ---------------------------------------------------------------------------


def test_validmeth_tables_exist_after_fresh_migrate(tmp_path):
    scene = _scene_repo(tmp_path)
    conn = sqlite3.connect(scene.path)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table'"
            )
        }
    finally:
        conn.close()
    for table in (
        'cad_optimization_problems',
        'cad_optimizer_run_profiles',
        'cad_optimizer_qualifications',
        'cad_pareto_assessments',
        'cad_mode_pairings',
        'cad_eigenmode_verdicts',
        'cad_diffuseness_assessments',
        'cad_statistical_applicability_declarations',
        'cad_multi_slope_fits',
        'cad_single_slope_assessments',
        'cad_coupled_decay_qualifications',
        'cad_reflection_pairings',
        'cad_reflection_correspondence_sets',
        'cad_reflection_correspondence_verdicts',
        'cad_modal_decay_observations',
        'cad_modal_decay_qualifications',
    ):
        assert table in names, table


def test_display_lines_report_validmeth_states():
    from htdt.measurement_evidence_display import (
        applicability_state_label,
        coupled_decay_line,
        eigenmode_verdict_line,
        modal_decay_qualification_line,
        optimizer_qualification_line,
        pareto_assessment_line,
        reflection_correspondence_verdict_line,
        single_slope_adequacy_line,
        statistical_applicability_line,
        diffuseness_assessment_line,
    )

    profile = _profile()
    qual = evaluate_optimizer_qualification(
        DOC, profile, evaluated_at_utc=T1
    )
    assert optimizer_qualification_line(qual).startswith('最適化適格:')

    pairing = _pairing(frequency_error_hz=0.2)
    verdict = evaluate_eigenmode_validation(
        DOC, pairing, evaluated_at_utc=T1
    )
    assert eigenmode_verdict_line(verdict).startswith('固有モード検証:')

    assessment = build_diffuseness_assessment(
        document_id=DOC,
        domain=_domain(),
        eligibility_state='eligible_within_declared_domain',
        estimator_profiles=(_measured_profile(),),
        covered_properties=('energy_uniformity',),
        declared_at_utc=T0,
    )
    assert diffuseness_assessment_line(assessment).startswith(
        '拡散場評価:'
    )
    decl = evaluate_diffuseness_applicability(
        DOC, assessment, use_domain=_domain(), evaluated_at_utc=T1
    )
    assert statistical_applicability_line(decl).startswith(
        '統計モデル適用性:'
    )

    a = _adequacy(
        dict(
            curvature_evidence_declared=False,
            dynamic_range_db=60.0,
            noise_floor_limited=False,
        )
    )
    assert single_slope_adequacy_line(a).startswith(
        '単一スロープ適性:'
    )
    cqual = evaluate_coupled_decay(
        DOC,
        _profile_coupled(),
        adequacy_assessments=(a,),
        evaluated_at_utc=T1,
    )
    assert coupled_decay_line(cqual).startswith('結合室減衰適格:')

    p = _rfx_pairing()
    cs = _rfx_set((p,))
    rverdict = evaluate_reflection_correspondence(
        DOC, cs, (p,), registration_valid=True, evaluated_at_utc=T1
    )
    assert reflection_correspondence_verdict_line(rverdict).startswith(
        '初期反射対応:'
    )

    mqual = evaluate_modal_decay_qualification(
        DOC, _observation(), evaluated_at_utc=T1
    )
    assert modal_decay_qualification_line(mqual).startswith(
        '時周波モーダル減衰:'
    )
    assert applicability_state_label('not_applicable') == '適用不可'


def test_pareto_assessment_line_renders():
    from htdt.measurement_evidence_display import pareto_assessment_line

    problem = _opt_problem()
    profile = build_optimizer_run_profile(
        document_id=DOC,
        problem_ref=problem_binding(problem),
        algorithm=_algorithm('evolutionary_multiobjective'),
        declared_budget=SearchBudget(function_evaluations=400),
        runs=(
            IndependentRunRecord(
                run_label='r1',
                seed=5,
                budget=SearchBudget(function_evaluations=400),
                final_front_ref=AuthorityRef(
                    kind='front', ref_id='fr-1', ref_sha256=SHA_C
                ),
            ),
        ),
        multi_objective=True,
    )
    assess = evaluate_pareto_approximation(
        DOC,
        profile,
        reference_status='best_known_aggregate_front',
        diversity_indicators=(
            ParetoIndicator(indicator='spacing_spread', value=0.7),
        ),
        nondominated_count=9,
        evaluated_at_utc=T1,
    )
    assert pareto_assessment_line(assess).startswith('Pareto近似評価:')
