"""REV58-IDENT regression tests — #691 typed logarithmic quantity / dB
reference authority, #689 calibration-parameter identifiability
authority, #698 validation sample-dependence / benchmark-leakage
authority.

Fixtures (DB/IDN/VSD) exercise the fail-closed rules the issues demand:
a dB without a reference is not a level, a good fit is not a unique
physical parameter, and N = bins × seats is not an independent sample
count.
"""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

from htdt.cad_logarithmic_quantity import (
    CadLogReference,
    build_calibration_bridge,
    build_log_quantity,
    evaluate_log_operation,
    log_quantity_binding,
)
from htdt.cad_logarithmic_quantity_repository import (
    CadLogQuantityRepository,
    LogQuantityConflictError,
    LogQuantityIntegrityError,
)
from htdt.cad_parameter_identifiability import (
    CadCorrelationPair,
    CadEquivalentSolution,
    CadIdentFrequencyDomain,
    CadSensitivityEntry,
    build_calibration_parameter,
    build_correlation_evidence,
    build_equivalent_solution_set,
    build_sensitivity_evidence,
    evaluate_identifiability,
)
from htdt.cad_parameter_identifiability_repository import (
    CadParameterIdentifiabilityRepository,
    IdentifiabilityConflictError,
    IdentifiabilityIntegrityError,
)
from htdt.cad_validation_statistics import (
    CadHierarchyLevel,
    build_benchmark_exposure,
    build_dataset_role_assignment,
    build_dependence_model,
    build_statistical_design,
    evaluate_validation_claim,
)
from htdt.cad_validation_statistics_repository import (
    CadValidationStatisticsRepository,
    ValidationStatisticsConflictError,
    ValidationStatisticsIntegrityError,
)


DOC = 'doc-rev58-ident'
T0 = '2026-10-06T00:00:00+00:00'
T1 = '2026-10-06T01:00:00+00:00'
T2 = '2026-10-06T02:00:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64

RUN_REF = AuthorityRef(
    kind='calibration_run', ref_id='calrun-1', ref_sha256=SHA_A
)
MEAS_REF = AuthorityRef(
    kind='impedance_measurement', ref_id='zmeas-1', ref_sha256=SHA_B
)
CORPUS_REF = AuthorityRef(
    kind='validation_corpus', ref_id='vcorpus-1', ref_sha256=SHA_C
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
# #691 — typed logarithmic quantity / dB reference (DB fixtures)
# ---------------------------------------------------------------------------


def _spl_reference() -> CadLogReference:
    return CadLogReference(
        reference_value=20e-6,
        reference_unit='Pa',
        reference_label='dB SPL re 20 uPa',
        standard_profile='iso_80000-8:2020',
        weighting='z',
        time_integration='fast',
    )


def _spl(value_db: float = 74.0, document_id: str = DOC):
    return build_log_quantity(
        document_id=document_id,
        quantity_class='absolute_log_level',
        domain='acoustic',
        quantity='sound_pressure_level',
        value_db=value_db,
        ratio_basis='field_amplitude_like',
        reference=_spl_reference(),
        declared_at_utc=T0,
    )


def _dbfs(value_db: float = -20.0, document_id: str = DOC):
    return build_log_quantity(
        document_id=document_id,
        quantity_class='absolute_log_level',
        domain='digital',
        quantity='dbfs_rms_level',
        value_db=value_db,
        ratio_basis='power_energy_like',
        reference=CadLogReference(
            reference_value=1.0,
            reference_unit='FS',
            reference_label='digital full scale rms',
            standard_profile='aes17:2020',
        ),
        declared_at_utc=T0,
    )


def test_db10_spl_seals_with_reference_identity() -> None:
    spl = _spl()
    assert spl.quantity_id.startswith('logqty-')
    assert spl.reference is not None
    assert spl.reference.weighting == 'z'
    assert spl.reference.time_integration == 'fast'
    assert spl.same_quantity_identity(_spl())


def test_db20_absolute_level_without_reference_rejected() -> None:
    with pytest.raises(ValueError, match='reference'):
        build_log_quantity(
            document_id=DOC,
            quantity_class='absolute_log_level',
            domain='acoustic',
            quantity='sound_pressure_level',
            value_db=74.0,
            ratio_basis='field_amplitude_like',
            declared_at_utc=T0,
        )


def test_db30_cross_domain_difference_is_incompatible() -> None:
    op = evaluate_log_operation(
        document_id=DOC,
        operation='level_difference',
        operands=(_spl(), _dbfs()),
        evaluated_at_utc=T1,
    )
    assert op.state == 'incompatible_quantities'
    assert op.derived is None


def test_db40_same_identity_difference_is_compatible() -> None:
    op = evaluate_log_operation(
        document_id=DOC,
        operation='level_difference',
        operands=(_spl(80.0), _spl(74.0)),
        evaluated_at_utc=T1,
    )
    assert op.state == 'compatible'
    assert op.derived is not None
    assert op.derived.value_db == pytest.approx(6.0)


def test_db50_gain_application_requires_linear_chain() -> None:
    gain = build_log_quantity(
        document_id=DOC,
        quantity_class='relative_gain_loss',
        domain='dimensionless',
        quantity='gain_ratio',
        value_db=-6.0,
        ratio_basis='power_energy_like',
        declared_at_utc=T0,
    )
    linear = evaluate_log_operation(
        document_id=DOC,
        operation='apply_gain',
        operands=(_spl(), gain),
        linearity_state='linear_chain_declared',
        evaluated_at_utc=T1,
    )
    assert linear.state == 'compatible'
    assert linear.derived is not None
    assert linear.derived.value_db == pytest.approx(68.0)

    undeclared = evaluate_log_operation(
        document_id=DOC,
        operation='apply_gain',
        operands=(_spl(), gain),
        evaluated_at_utc=T1,
    )
    assert undeclared.state == 'unverified'
    assert undeclared.derived is None

    limiter = evaluate_log_operation(
        document_id=DOC,
        operation='apply_gain',
        operands=(_spl(), gain),
        linearity_state='nonlinear_or_dynamic',
        evaluated_at_utc=T1,
    )
    assert limiter.state == 'nonlinear_chain'
    assert limiter.derived is None


def test_db60_coherent_sum_requires_phase_data() -> None:
    coherent = evaluate_log_operation(
        document_id=DOC,
        operation='coherent_sum',
        operands=(_spl(74.0), _spl(74.0)),
        sum_kind='coherent_in_phase',
        evaluated_at_utc=T1,
    )
    assert coherent.state == 'coherent_requires_phase_data'
    assert coherent.derived is None

    incoherent = evaluate_log_operation(
        document_id=DOC,
        operation='energetic_sum',
        operands=(_spl(74.0), _spl(74.0)),
        sum_kind='incoherent_independent',
        evaluated_at_utc=T1,
    )
    assert incoherent.state == 'compatible'
    assert incoherent.derived is not None
    assert incoherent.derived.value_db == pytest.approx(
        74.0 + 10.0 * 0.3010299956639812
    )


def test_db70_convert_requires_matching_active_bridge() -> None:
    dbfs = _dbfs()
    missing = evaluate_log_operation(
        document_id=DOC,
        operation='convert',
        operands=(dbfs,),
        target_domain='acoustic',
        target_quantity='sound_pressure_level',
        evaluated_at_utc=T1,
    )
    assert missing.state == 'requires_calibration_bridge'
    assert missing.derived is None

    bridge = build_calibration_bridge(
        document_id=DOC,
        bridge_label='full-scale to acoustic SPL at seat A1',
        from_domain='digital',
        from_quantity='dbfs_rms_level',
        to_domain='acoustic',
        to_quantity='sound_pressure_level',
        transfer_db=94.0,
        chain_refs=(log_quantity_binding(dbfs),),
        declared_at_utc=T0,
    )
    converted = evaluate_log_operation(
        document_id=DOC,
        operation='convert',
        operands=(dbfs,),
        bridges=(bridge,),
        target_domain='acoustic',
        target_quantity='sound_pressure_level',
        evaluated_at_utc=T1,
    )
    assert converted.state == 'compatible'
    assert converted.derived is not None
    assert converted.derived.value_db == pytest.approx(74.0)


def test_db80_over_full_scale_dbfs_rejected() -> None:
    with pytest.raises(ValueError):
        build_log_quantity(
            document_id=DOC,
            quantity_class='absolute_log_level',
            domain='digital',
            quantity='dbfs_sample_peak',
            value_db=+3.0,
            ratio_basis='field_amplitude_like',
            reference=CadLogReference(
                reference_value=1.0,
                reference_unit='FS',
                reference_label='full-scale sample peak',
                standard_profile='aes17:2020',
            ),
            declared_at_utc=T0,
        )


def test_db90_bridge_status_lifecycle() -> None:
    bridge = build_calibration_bridge(
        document_id=DOC,
        bridge_label='DSP output to seat SPL',
        from_domain='digital',
        from_quantity='dbfs_rms_level',
        to_domain='acoustic',
        to_quantity='sound_pressure_level',
        transfer_db=94.0,
        chain_refs=(log_quantity_binding(_dbfs()),),
        declared_at_utc=T0,
    )
    assert bridge.status == 'active'
    assert bridge.connects(_dbfs(), 'acoustic', 'sound_pressure_level')
    assert not bridge.connects(_spl(), 'acoustic', 'sound_pressure_level')


def test_log_quantity_repository_roundtrip_and_integrity(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadLogQuantityRepository(scene)
    spl = _spl()
    repo.save_quantity(spl)
    assert repo.get_quantity(spl.quantity_id) == spl
    assert repo.list_quantities(DOC) == (spl,)

    bridge = build_calibration_bridge(
        document_id=DOC,
        bridge_label='fs to seat',
        from_domain='digital',
        from_quantity='dbfs_rms_level',
        to_domain='acoustic',
        to_quantity='sound_pressure_level',
        transfer_db=94.0,
        chain_refs=(log_quantity_binding(_dbfs()),),
        declared_at_utc=T0,
    )
    repo.save_bridge(bridge)
    assert repo.get_bridge(bridge.bridge_id) == bridge

    op = evaluate_log_operation(
        document_id=DOC,
        operation='level_difference',
        operands=(_spl(80.0), _spl(74.0)),
        evaluated_at_utc=T1,
    )
    repo.save_operation(op)
    assert repo.get_operation(op.operation_id) == op

    # Append-only: re-saving the same payload is a no-op; a different
    # payload under the same id is a conflict.
    repo.save_quantity(spl)

    # Column tampering must surface on read.
    _tamper(
        scene.path,
        "UPDATE cad_log_quantities SET quantity_class='linear_value' "
        'WHERE quantity_id=?',
        (spl.quantity_id,),
    )
    with pytest.raises(LogQuantityIntegrityError):
        repo.get_quantity(spl.quantity_id)


# ---------------------------------------------------------------------------
# #689 — calibration-parameter identifiability (IDN fixtures)
# ---------------------------------------------------------------------------


def _param(
    label: str = 'ceiling absorption 1 kHz',
    *,
    provenance='calibration_adjusted',
    role='boundary_absorption',
    **kwargs,
):
    return build_calibration_parameter(
        document_id=DOC,
        parameter_label=label,
        role=role,
        provenance=provenance,
        unit='absorption_coefficient',
        calibration_run_ref=RUN_REF,
        declared_at_utc=T0,
        **kwargs,
    )


def _sensitivity(
    label: str = 'ceiling absorption 1 kHz',
    *,
    normalized: float = 0.5,
    insensitive: bool = False,
):
    return build_sensitivity_evidence(
        document_id=DOC,
        method='finite_difference',
        parameter_scaling='relative to fitted value',
        observable_normalization='per-band energy fraction',
        perturbation_method='relative step 1e-3',
        entries=(
            CadSensitivityEntry(
                parameter_label=label,
                normalized_sensitivity=normalized,
                insensitive=insensitive,
                observable_label='edf_rmse',
            ),
        ),
        calibration_run_ref=RUN_REF,
        declared_at_utc=T0,
    )


def _correlation(
    *,
    a: str = 'ceiling absorption 1 kHz',
    b: str = 'floor scattering 1 kHz',
    correlation: float = -0.93,
    klass='strong',
):
    return build_correlation_evidence(
        document_id=DOC,
        method='fisher_hessian_local',
        assumptions=('local linearization at fitted point',),
        pairs=(
            CadCorrelationPair(
                parameter_a=a,
                parameter_b=b,
                correlation=correlation,
                correlation_class=klass,
            ),
        ),
        calibration_run_ref=RUN_REF,
        declared_at_utc=T0,
    )


def test_idn10_fitted_parameter_requires_run_ref() -> None:
    with pytest.raises(ValueError, match='calibration run'):
        build_calibration_parameter(
            document_id=DOC,
            parameter_label='orphan',
            role='boundary_absorption',
            provenance='inverse_estimated',
            unit='absorption_coefficient',
            declared_at_utc=T0,
        )


def test_idn20_measured_parameter_requires_evidence_ref() -> None:
    with pytest.raises(ValueError, match='source evidence'):
        build_calibration_parameter(
            document_id=DOC,
            parameter_label='orphan measured',
            role='boundary_complex_impedance',
            provenance='directly_measured',
            unit='rayl',
            declared_at_utc=T0,
        )
    measured = build_calibration_parameter(
        document_id=DOC,
        parameter_label='impedance tube α',
        role='boundary_absorption',
        provenance='directly_measured',
        unit='absorption_coefficient',
        prior_evidence_ref=MEAS_REF,
        declared_at_utc=T0,
    )
    verdict = evaluate_identifiability(
        document_id=DOC, parameter=measured, evaluated_at_utc=T1,
    )
    assert (
        verdict.identifiability_class
        == 'practically_identifiable_within_data'
    )
    assert verdict.parameter_claim == 'parameter_identified_within_domain'


def test_idn30_equivalent_set_is_structurally_non_unique() -> None:
    param = _param()
    equivalent = build_equivalent_solution_set(
        document_id=DOC,
        tolerance_objective_delta=0.001,
        retention_method='multi-start ensemble',
        members=(
            CadEquivalentSolution(
                member_label='α=0.32 / r=1.4 m',
                parameter_values_json='{"alpha":0.32,"r":1.4}',
                objective_delta=0.0,
                physically_distinct_claims=True,
                claim_summary='farther source, higher absorption',
            ),
            CadEquivalentSolution(
                member_label='α=0.18 / r=1.9 m',
                parameter_values_json='{"alpha":0.18,"r":1.9}',
                objective_delta=0.0005,
                physically_distinct_claims=True,
                claim_summary='nearer source, lower absorption',
            ),
        ),
        calibration_run_ref=RUN_REF,
        declared_at_utc=T0,
    )
    verdict = evaluate_identifiability(
        document_id=DOC,
        parameter=param,
        equivalent_sets=(equivalent,),
        evaluated_at_utc=T1,
    )
    assert verdict.identifiability_class == 'structurally_non_unique'
    assert verdict.parameter_claim == 'parameter_not_identifiable'
    assert 'alternate_source_position' in verdict.evidence_needs


def test_idn40_strong_correlation_is_weakly_identifiable() -> None:
    param = _param()
    verdict = evaluate_identifiability(
        document_id=DOC,
        parameter=param,
        sensitivity=_sensitivity(),
        correlations=(_correlation(),),
        evaluated_at_utc=T1,
    )
    assert verdict.identifiability_class == 'weakly_identifiable'
    assert verdict.parameter_claim == 'parameter_weakly_identified'
    assert 'floor scattering 1 kHz' in verdict.correlated_with


def test_idn50_no_evidence_is_insufficient() -> None:
    verdict = evaluate_identifiability(
        document_id=DOC, parameter=_param(), evaluated_at_utc=T1,
    )
    assert verdict.identifiability_class == 'insufficient_evidence'
    assert verdict.parameter_claim == 'parameter_not_identifiable'


def test_idn60_prior_dominated_is_not_data_identified() -> None:
    param = _param(provenance='prior_assumed')
    # prior_assumed needs no run pin but is dominated by the prior.
    verdict = evaluate_identifiability(
        document_id=DOC,
        parameter=param,
        sensitivity=_sensitivity(),
        evaluated_at_utc=T1,
    )
    assert verdict.identifiability_class == 'prior_dominated'
    assert verdict.parameter_claim == 'parameter_not_identifiable'


def test_idn70_bound_saturated_is_bound_dominated() -> None:
    param = _param(
        bound_min=0.0, bound_max=1.0, bound_distance='at_upper_bound',
    )
    verdict = evaluate_identifiability(
        document_id=DOC,
        parameter=param,
        sensitivity=_sensitivity(),
        evaluated_at_utc=T1,
    )
    assert verdict.identifiability_class == 'bound_dominated'
    assert verdict.parameter_claim == 'parameter_not_identifiable'


def test_idn80_confirmed_model_discrepancy_caps_claim() -> None:
    verdict = evaluate_identifiability(
        document_id=DOC,
        parameter=_param(),
        sensitivity=_sensitivity(),
        model_discrepancy='confirmed',
        evaluated_at_utc=T1,
    )
    assert verdict.identifiability_class == 'model_discrepancy_limited'
    assert verdict.parameter_claim == 'parameter_model_dependent'


def test_idn85_unasked_model_form_question_caps_claim() -> None:
    """A physical-parameter claim requires the model-form question to
    have been asked — default 'not_evaluated' caps at weak."""
    verdict = evaluate_identifiability(
        document_id=DOC,
        parameter=_param(),
        sensitivity=_sensitivity(),
        evaluated_at_utc=T1,
    )
    assert (
        verdict.identifiability_class
        == 'practically_identifiable_within_data'
    )
    assert verdict.parameter_claim == 'parameter_weakly_identified'


def test_idn90_clean_evidence_identifies_within_domain() -> None:
    param = _param(
        effective_domain=CadIdentFrequencyDomain(
            low_hz=250.0, high_hz=2000.0,
        ),
        constraining_source_refs=(
            AuthorityRef(
                kind='source', ref_id='src-1', ref_sha256=SHA_A,
            ),
        ),
    )
    verdict = evaluate_identifiability(
        document_id=DOC,
        parameter=param,
        sensitivity=_sensitivity(),
        model_discrepancy='none_declared',
        evaluated_at_utc=T1,
    )
    assert (
        verdict.identifiability_class
        == 'practically_identifiable_within_data'
    )
    assert verdict.parameter_claim == 'parameter_identified_within_domain'
    assert verdict.domain_note is not None
    assert '250' in verdict.domain_note


def test_idn95_insensitive_entry_is_weak() -> None:
    verdict = evaluate_identifiability(
        document_id=DOC,
        parameter=_param(),
        sensitivity=_sensitivity(insensitive=True),
        evaluated_at_utc=T1,
    )
    assert verdict.identifiability_class == 'weakly_identifiable'


def test_identifiability_repository_roundtrip_and_integrity(
    tmp_path,
) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadParameterIdentifiabilityRepository(scene)
    param = _param()
    repo.save_parameter(param)
    assert repo.get_parameter(param.parameter_id) == param
    assert repo.list_parameters(DOC) == (param,)

    sens = _sensitivity()
    repo.save_sensitivity(sens)
    assert repo.get_sensitivity(sens.sensitivity_id) == sens

    corr = _correlation()
    repo.save_correlation(corr)
    assert repo.get_correlation(corr.correlation_id) == corr

    eq = build_equivalent_solution_set(
        document_id=DOC,
        tolerance_objective_delta=0.01,
        retention_method='posterior ensemble',
        members=(
            CadEquivalentSolution(member_label='m1', objective_delta=0.0),
            CadEquivalentSolution(member_label='m2', objective_delta=0.005),
        ),
        declared_at_utc=T0,
    )
    repo.save_equivalent_set(eq)
    assert repo.get_equivalent_set(eq.set_id) == eq

    verdict = evaluate_identifiability(
        document_id=DOC,
        parameter=param,
        sensitivity=sens,
        evaluated_at_utc=T1,
    )
    repo.save_assessment(verdict)
    assert repo.get_assessment(verdict.assessment_id) == verdict
    assert repo.list_assessments(DOC) == (verdict,)

    repo.save_parameter(param)  # idempotent

    _tamper(
        scene.path,
        "UPDATE cad_calib_parameter_records SET provenance='directly_measured' "
        'WHERE parameter_id=?',
        (param.parameter_id,),
    )
    with pytest.raises(IdentifiabilityIntegrityError):
        repo.get_parameter(param.parameter_id)


# ---------------------------------------------------------------------------
# #698 — validation sample-dependence / benchmark leakage (VSD fixtures)
# ---------------------------------------------------------------------------


def _hierarchy() -> tuple[CadHierarchyLevel, ...]:
    return (
        CadHierarchyLevel(level_kind='room', count=3),
        CadHierarchyLevel(level_kind='source_position', count=4),
        CadHierarchyLevel(level_kind='receiver_position', count=8),
        CadHierarchyLevel(level_kind='frequency_bin', count=400),
    )


def _design(
    *,
    claim='unseen_rooms',
    unit='room',
    independent_count: int | None = 3,
    corpus_refs=(CORPUS_REF,),
    hierarchy=_hierarchy(),
    label='scene-A design',
    document_id: str = DOC,
):
    return build_statistical_design(
        document_id=document_id,
        design_label=label,
        generalization_claim=claim,
        independent_unit=unit,
        hierarchy=hierarchy,
        raw_observation_count=3 * 4 * 8 * 400,
        independent_unit_count=independent_count,
        frequency_bin_count=400,
        corpus_refs=corpus_refs,
        error_distribution_scope=(
            'within-room seat-population dispersion'
        ),
        declared_at_utc=T0,
    )


def _dependence(design):
    return build_dependence_model(
        document_id=DOC,
        design_ref=design,
        dependence_classes=(
            'spatial_correlation', 'frequency_bin_coupling',
        ),
        spatial_correlation_model='kuster_reverberant_family',
        spatial_model_detail=(
            'reverberant-field sinc model, T60-weighted'
        ),
        frequency_coupling_causes=('smoothing_banding',),
        resampling_unit='room',
        resampling_method='cluster bootstrap over rooms',
        declared_at_utc=T0,
    )


def test_vsd10_design_seals_counts_and_hierarchy() -> None:
    design = _design()
    assert design.design_id.startswith('vsdes-')
    assert design.independent_cap() == 3
    assert design.frequency_bin_count == 400


def test_vsd20_bins_as_units_is_pseudoreplication() -> None:
    design = build_statistical_design(
        document_id=DOC,
        design_label='inflated N design',
        generalization_claim='unseen_rooms',
        independent_unit='room',
        hierarchy=(
            CadHierarchyLevel(level_kind='room', count=3),
            CadHierarchyLevel(level_kind='frequency_bin', count=400),
        ),
        raw_observation_count=1200,
        independent_unit_count=1200,
        corpus_refs=(CORPUS_REF,),
        declared_at_utc=T0,
    )
    verdict = evaluate_validation_claim(
        document_id=DOC, design=design, evaluated_at_utc=T1,
    )
    assert verdict.state == 'pseudoreplication_detected'


def test_vsd30_split_granularity_mismatch() -> None:
    design = _design(
        claim='unseen_rooms',
        unit='receiver_position',
        independent_count=96,
    )
    verdict = evaluate_validation_claim(
        document_id=DOC, design=design, evaluated_at_utc=T1,
    )
    assert verdict.state == 'split_mismatch'


def test_vsd40_no_dependence_model_is_unestablished() -> None:
    verdict = evaluate_validation_claim(
        document_id=DOC, design=_design(), evaluated_at_utc=T1,
    )
    assert verdict.state == 'independence_unestablished'


def test_vsd50_clean_design_qualifies() -> None:
    design = _design()
    verdict = evaluate_validation_claim(
        document_id=DOC,
        design=design,
        dependence=_dependence(design),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'qualified_generalization'
    assert verdict.independent_unit_count == 3


def test_vsd60_locked_challenge_corpus_is_eligible() -> None:
    design = _design()
    role = build_dataset_role_assignment(
        document_id=DOC,
        corpus_ref=CORPUS_REF,
        role='locked_challenge_test',
        context_label='locked benchmark A',
        declared_at_utc=T0,
    )
    verdict = evaluate_validation_claim(
        document_id=DOC,
        design=design,
        dependence=_dependence(design),
        role_assignments=(role,),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'locked_challenge_eligible'


def test_vsd70_exposed_selection_corpus_is_biased() -> None:
    design = _design()
    role = build_dataset_role_assignment(
        document_id=DOC,
        corpus_ref=CORPUS_REF,
        role='model_selection_development',
        context_label='solver iteration',
        declared_at_utc=T0,
    )
    verdict = evaluate_validation_claim(
        document_id=DOC,
        design=design,
        dependence=_dependence(design),
        role_assignments=(role,),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'winner_selection_biased'

    exposure = build_benchmark_exposure(
        document_id=DOC,
        corpus_ref=CORPUS_REF,
        metrics_revealed=('edf_rmse',),
        decision_class='solver_change',
        solver_version='solver-0.42',
        exposed_at_utc=T0,
    )
    dev_role = build_dataset_role_assignment(
        document_id=DOC,
        corpus_ref=CORPUS_REF,
        role='development_validation',
        context_label='dev loop',
        declared_at_utc=T0,
    )
    verdict2 = evaluate_validation_claim(
        document_id=DOC,
        design=design,
        dependence=_dependence(design),
        role_assignments=(dev_role,),
        exposures=(exposure,),
        evaluated_at_utc=T1,
    )
    assert verdict2.state in (
        'development_validation_only', 'winner_selection_biased',
    )
    assert verdict2.corpus_states[0].status == 'exposed'


def test_vsd80_undeclared_independent_count_is_insufficient() -> None:
    verdict = evaluate_validation_claim(
        document_id=DOC,
        design=_design(independent_count=None),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'insufficient_evidence'


def test_vsd90_effective_size_needs_method() -> None:
    with pytest.raises(ValueError, match='estimation method'):
        build_statistical_design(
            document_id=DOC,
            design_label='bare effective size',
            generalization_claim='unseen_rooms',
            independent_unit='room',
            hierarchy=_hierarchy(),
            raw_observation_count=1200,
            independent_unit_count=3,
            effective_sample_size=11.5,
            corpus_refs=(CORPUS_REF,),
            declared_at_utc=T0,
        )


def test_validation_statistics_repository_roundtrip_and_integrity(
    tmp_path,
) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadValidationStatisticsRepository(scene)
    design = _design()
    repo.save_design(design)
    assert repo.get_design(design.design_id) == design
    assert repo.list_designs(DOC) == (design,)

    dep = _dependence(design)
    repo.save_dependence(dep)
    assert repo.get_dependence(dep.dependence_id) == dep

    role = build_dataset_role_assignment(
        document_id=DOC,
        corpus_ref=CORPUS_REF,
        role='locked_challenge_test',
        context_label='locked',
        declared_at_utc=T0,
    )
    repo.save_role_assignment(role)
    assert repo.get_role_assignment(role.assignment_id) == role

    exposure = build_benchmark_exposure(
        document_id=DOC,
        corpus_ref=CORPUS_REF,
        metrics_revealed=('edf_rmse',),
        decision_class='debugging',
        exposed_at_utc=T0,
    )
    repo.save_exposure(exposure)
    assert repo.get_exposure(exposure.exposure_id) == exposure

    verdict = evaluate_validation_claim(
        document_id=DOC,
        design=design,
        dependence=dep,
        role_assignments=(role,),
        evaluated_at_utc=T1,
    )
    repo.save_qualification(verdict)
    assert repo.get_qualification(verdict.qualification_id) == verdict
    assert repo.list_qualifications(DOC) == (verdict,)

    repo.save_design(design)  # idempotent

    _tamper(
        scene.path,
        "UPDATE cad_challenge_qualifications SET "
        "state='qualified_generalization' WHERE qualification_id=?",
        (verdict.qualification_id,),
    )
    with pytest.raises(ValidationStatisticsIntegrityError):
        repo.get_qualification(verdict.qualification_id)


# ---------------------------------------------------------------------------
# Cross-cutting: seal honesty + display lines
# ---------------------------------------------------------------------------


def test_append_only_tables_reject_forged_payloads(tmp_path) -> None:
    """A forged sealed record must fail identity validation at write."""
    scene = _scene_repo(tmp_path)
    repo = CadLogQuantityRepository(scene)
    spl = _spl()
    forged = spl.model_copy(update={'value_db': 999.0})
    with pytest.raises((ValueError, LogQuantityIntegrityError)):
        repo.save_quantity(forged)
    # But the genuine record still saves cleanly.
    repo.save_quantity(spl)


def test_display_lines_report_component_states() -> None:
    from htdt.measurement_evidence_display import (
        identifiability_line,
        log_operation_line,
        validation_claim_line,
    )

    spl = _spl(80.0)
    op = evaluate_log_operation(
        document_id=DOC,
        operation='level_difference',
        operands=(spl, _spl(74.0)),
        evaluated_at_utc=T1,
    )
    line = log_operation_line(op)
    assert '対数量演算' in line
    assert '適合' in line

    verdict = evaluate_identifiability(
        document_id=DOC,
        parameter=_param(),
        sensitivity=_sensitivity(),
        correlations=(_correlation(),),
        evaluated_at_utc=T1,
    )
    line = identifiability_line(verdict)
    assert '校正同定性' in line
    assert '弱同定' in line

    design = _design()
    qual = evaluate_validation_claim(
        document_id=DOC,
        design=design,
        dependence=_dependence(design),
        evaluated_at_utc=T1,
    )
    line = validation_claim_line(qual)
    assert '検証主張適格' in line
    assert '汎化適格' in line
    assert '独立単位' in line


def test_ident_correlation_numeric_threshold() -> None:
    """A numeric |r| at/over the threshold is strong even without a
    qualitative class declaration."""
    param = _param()
    corr = build_correlation_evidence(
        document_id=DOC,
        method='profile_objective',
        assumptions=('profile likelihood',),
        pairs=(
            CadCorrelationPair(
                parameter_a='ceiling absorption 1 kHz',
                parameter_b='floor scattering 1 kHz',
                correlation=-0.95,
                correlation_class='unknown',
            ),
        ),
        declared_at_utc=T0,
    )
    verdict = evaluate_identifiability(
        document_id=DOC,
        parameter=param,
        sensitivity=_sensitivity(),
        correlations=(corr,),
        evaluated_at_utc=T1,
    )
    assert verdict.identifiability_class == 'weakly_identifiable'
