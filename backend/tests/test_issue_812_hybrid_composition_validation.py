"""Issue #812 — R160 hybrid composition validation authority.

Covers the sealed spec/evidence/verdict records, the fail-closed
evaluator ordering (component qualification before hybrid evidence,
component-inheritance denial, per-region results with explicit gap
accounting, disjoint ownership, phase/time gating for coherent claims,
sensitivity sweep, grid reconciliation bound, evidence classes and the
bounded applicability envelope) and repository round-trip + tamper
detection.
"""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_hybrid_composition_validation import (
    ApplicabilityEnvelope,
    ContributionOwnerEntry,
    CrossoverRuleSpec,
    HYBRID_VALIDATION_LABELS,
    HybridCompositionValidationIntegrityError,
    HybridCompositionValidationSpec,
    HybridMetricValue,
    HybridValidationEvidence,
    HybridValidationVerdict,
    LateContribution,
    PhaseTimeCheck,
    SensitivityPoint,
    SensitivityRequirement,
    evaluate_hybrid_validation,
)
from htdt.cad_hybrid_composition_validation_repository import (
    CadHybridCompositionValidationRepository,
    HybridCompositionValidationConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import ensure_native_schema
from htdt.canonical_json import canonical_sha256
from htdt.measurement_evidence_display import (
    hybrid_validation_label,
    hybrid_validation_verdict_line,
)

_CONTRIBUTIONS = (
    'direct',
    'specular_reflection',
    'scattered_late',
    'diffracted',
    'modal_coherent',
)

_PHASE_CHECKS = (
    'coherent_phase_authority_both',
    'shared_time_origin',
    'compatible_fourier_sign_convention',
    'compatible_propagation_timing',
    'phase_reference_alignment',
    'interference_structure',
    'crossover_continuity',
    'group_delay_consistency',
)

_RULE_ID = 'widest_contiguous_agreement_band_v1'
_RULE_VERSION = 'r160-automatic-crossover-selection-1'


def _ref(kind: str, rid: str = 'x') -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256='a' * 64)


def _spec_ref(spec: HybridCompositionValidationSpec) -> AuthorityRef:
    return AuthorityRef(
        kind='hybrid_composition_validation_spec',
        ref_id=spec.spec_id,
        ref_sha256=spec.spec_sha256,
    )


def _policy(
    regions: tuple[str, ...] = ('wave_only', 'crossover_blend', 'ga_only'),
    overrides: dict[tuple[str, str], str] | None = None,
) -> tuple[ContributionOwnerEntry, ...]:
    defaults = {
        'wave_only': 'wave_r130',
        'crossover_blend': 'complementary_blend',
        'ga_only': 'geometric_r150',
    }
    entries = []
    for region in regions:
        for contribution in _CONTRIBUTIONS:
            owner = (overrides or {}).get(
                (region, contribution), defaults[region])
            entries.append(
                ContributionOwnerEntry(
                    region=region, contribution=contribution, owner=owner))
    return tuple(entries)


def _spec(
    blend: bool = True,
    semantics: str = 'energetic',
    late_field_composed: bool = True,
    grid_reconciliation_required: bool = True,
    wave_min_level: str = 'numerically_verified',
    ga_min_level: str = 'external_benchmark_validated',
    ownership_overrides: dict[tuple[str, str], str] | None = None,
    document_id: str = 'doc-1',
    **kw,
) -> HybridCompositionValidationSpec:
    regions = (
        ('wave_only', 'crossover_blend', 'ga_only')
        if blend
        else ('wave_only', 'ga_only')
    )
    return HybridCompositionValidationSpec.create(
        document_id=document_id,
        subject_ref=_ref('stitched_hybrid_response', 'subj-1'),
        wave_qualification_ref=_ref('wave_qualification', 'wq-1'),
        ga_qualification_ref=_ref('ga_qualification', 'gq-1'),
        wave_min_level=wave_min_level,
        ga_min_level=ga_min_level,
        composition_semantics=semantics,
        crossover_rule=(
            CrossoverRuleSpec(
                rule_id=_RULE_ID,
                rule_version=_RULE_VERSION,
                required_agreement_evidence=(
                    ('magnitude_agreement', 'relative_discrepancy_bound')
                    + (
                        ('phase_time_compatibility',)
                        if semantics == 'coherent_complex'
                        else ()
                    )
                ),
            )
            if blend
            else None
        ),
        expected_regions=regions,
        ownership_policy=_policy(regions, ownership_overrides),
        late_field_composed=late_field_composed,
        grid_reconciliation_required=grid_reconciliation_required,
        sensitivity_requirement=(
            SensitivityRequirement(
                admissible_range_hz=(1000.0, 2000.0),
                observables=('level_spectrum', 'decay_time'),
            )
            if blend
            else None
        ),
        applicability=ApplicabilityEnvelope(
            band_hz=(20.0, 8000.0),
            phenomena=('local_frequency_response',),
            contexts=('small_room',),
            limitations=('not valid outside the declared band',),
        ),
        **kw,
    )


def _ev(
    spec: HybridCompositionValidationSpec,
    kind: str,
    **kw,
) -> HybridValidationEvidence:
    return HybridValidationEvidence.create(
        document_id=kw.pop('document_id', 'doc-1'),
        spec_ref=_spec_ref(spec),
        evidence_kind=kind,
        **kw,
    )


def _component(
    spec: HybridCompositionValidationSpec,
    path: str,
    level: str = 'external_benchmark_validated',
    ref: AuthorityRef | None = None,
) -> HybridValidationEvidence:
    default_ref = (
        spec.wave_qualification_ref
        if path == 'wave_r130'
        else spec.ga_qualification_ref
    )
    return _ev(
        spec,
        'component_qualification',
        outcome='satisfied',
        solver_path=path,
        achieved_level=level,
        qualification_ref=ref or default_ref,
    )


def _binding(spec) -> HybridValidationEvidence:
    return _ev(
        spec,
        'common_input_binding',
        outcome='satisfied',
        provenance_refs=(
            _ref('geometry_authority', 'geo-1'),
            _ref('material_authority', 'mat-1'),
            _ref('source_authority', 'src-1'),
            _ref('receiver_authority', 'rcv-1'),
        ),
    )


def _region(
    spec,
    kind: str,
    outcome: str = 'satisfied',
    band: tuple[float, float] = (20.0, 1000.0),
    contributions: tuple[str, ...] = _CONTRIBUTIONS,
) -> HybridValidationEvidence:
    return _ev(
        spec,
        'region_evaluation',
        outcome=outcome,
        domain_kind=kind,
        band_hz=band,
        contributions_present=contributions,
    )


def _gap(spec) -> HybridValidationEvidence:
    return _ev(
        spec,
        'region_evaluation',
        outcome='satisfied',
        domain_kind='gap',
        band_hz=(1000.0, 1400.0),
        gap_reason='no admissible wave/GA agreement band',
    )


def _selection(
    spec,
    outcome: str = 'satisfied',
    rule_version: str = _RULE_VERSION,
    agreement: tuple[str, ...] = (),
) -> HybridValidationEvidence:
    if not agreement:
        agreement = tuple(
            spec.crossover_rule.required_agreement_evidence)
    return _ev(
        spec,
        'crossover_selection_evidence',
        outcome=outcome,
        rule_id=_RULE_ID,
        rule_version=rule_version,
        agreement_kinds=agreement if outcome == 'satisfied' else (),
        band_hz=(1000.0, 2000.0) if outcome == 'satisfied' else None,
    )


def _phase(spec, check_outcomes: dict[str, str] | None = None) -> (
    HybridValidationEvidence
):
    checks = tuple(
        PhaseTimeCheck(
            check=name,
            outcome=(check_outcomes or {}).get(name, 'satisfied'),
        )
        for name in _PHASE_CHECKS
    )
    all_ok = all(check.outcome == 'satisfied' for check in checks)
    return _ev(
        spec,
        'phase_time_alignment',
        outcome='satisfied' if all_ok else 'inconclusive',
        phase_checks=checks,
    )


def _grid(
    spec,
    outcome: str = 'satisfied',
    method: str = 'cartesian_linear_v1',
    bound: float | None = 0.02,
    extrapolation: bool = False,
) -> HybridValidationEvidence:
    return _ev(
        spec,
        'grid_reconciliation_error',
        outcome=outcome,
        reconciliation_method=method,
        interpolation_error_bound=bound,
        extrapolation_performed=extrapolation,
        narrow_resonance_checked=True,
    )


def _sens(
    spec,
    outcome: str = 'satisfied',
    material: bool = False,
    observables: tuple[str, ...] = ('level_spectrum', 'decay_time'),
    swept: tuple[float, float] = (950.0, 2050.0),
) -> HybridValidationEvidence:
    points = tuple(
        SensitivityPoint(
            crossover_hz=cross,
            observable=obs,
            material_change=material,
        )
        for cross in (1100.0, 1500.0, 1900.0)
        for obs in observables
    )
    return _ev(
        spec,
        'crossover_sensitivity',
        outcome=outcome,
        swept_range_hz=swept,
        sensitivity_points=points,
    )


def _late(
    spec,
    early: str = 'early_field_separately_verified',
    outcome: str = 'satisfied',
) -> HybridValidationEvidence:
    return _ev(
        spec,
        'late_field_decomposition',
        outcome=outcome,
        late_contributions=(
            LateContribution(
                kind='specular',
                owner='geometric_r150',
                energy_share=0.4,
            ),
            LateContribution(
                kind='scattered',
                owner='geometric_r150',
                energy_share=0.5,
            ),
        ),
        early_field_state=early,
    )


def _numerical(spec) -> HybridValidationEvidence:
    return _ev(
        spec,
        'numerical_verification',
        outcome='satisfied',
        metrics=(
            HybridMetricValue(
                name='self_consistency',
                value=0.01,
                tolerance=0.05,
                within_tolerance=True,
            ),
        ),
    )


def _benchmark(
    spec,
    ref_class: str = 'measured_external',
    holdout: bool = False,
) -> HybridValidationEvidence:
    return _ev(
        spec,
        'external_benchmark',
        outcome='satisfied',
        reference_class=ref_class,
        case_ref=_ref('benchmark_case', 'case-1'),
        holdout_ref=_ref('holdout_case', 'h-1') if holdout else None,
    )


def _full_evidence(
    spec, external: str | None = None,
) -> list[HybridValidationEvidence]:
    rows = [
        _component(spec, 'wave_r130'),
        _component(spec, 'geometric_r150'),
        _binding(spec),
        _region(spec, 'wave_only', band=(20.0, 1000.0)),
        _region(spec, 'ga_only', band=(2000.0, 8000.0)),
        _grid(spec),
        _numerical(spec),
    ]
    if 'crossover_blend' in spec.expected_regions:
        rows += [
            _region(spec, 'crossover_blend', band=(1000.0, 2000.0)),
            _selection(spec),
            _sens(spec),
        ]
    else:
        rows.append(_gap(spec))
    if spec.composition_semantics == 'coherent_complex':
        rows.append(_phase(spec))
    if spec.late_field_composed:
        rows.append(_late(spec))
    if external is not None:
        rows.append(_benchmark(spec, ref_class=external))
    return rows


def _repo(tmp_path) -> CadHybridCompositionValidationRepository:
    db = tmp_path / 'scene.sqlite3'
    ensure_native_schema(db)
    return CadHybridCompositionValidationRepository(SceneRepository(db))


# ----------------------------------------------------------------- spec


def test_spec_requires_pinned_refs() -> None:
    with pytest.raises(ValueError):
        HybridCompositionValidationSpec.create(
            document_id='d',
            subject_ref=AuthorityRef(kind='x', ref_id='r1'),
            wave_qualification_ref=_ref('wave_qualification', 'wq'),
            ga_qualification_ref=_ref('ga_qualification', 'gq'),
            composition_semantics='energetic',
            expected_regions=('wave_only',),
            ownership_policy=_policy(('wave_only',)),
            late_field_composed=False,
            grid_reconciliation_required=False,
            applicability=ApplicabilityEnvelope(),
        )


def test_spec_blend_requires_rule_and_sensitivity() -> None:
    kw = dict(
        document_id='d',
        subject_ref=_ref('stitched_hybrid_response'),
        wave_qualification_ref=_ref('wave_qualification', 'wq'),
        ga_qualification_ref=_ref('ga_qualification', 'gq'),
        composition_semantics='energetic',
        expected_regions=('wave_only', 'crossover_blend', 'ga_only'),
        ownership_policy=_policy(),
        applicability=ApplicabilityEnvelope(),
    )
    with pytest.raises(ValueError):
        HybridCompositionValidationSpec.create(
            crossover_rule=None,
            sensitivity_requirement=SensitivityRequirement(
                admissible_range_hz=(1000.0, 2000.0),
                observables=('level_spectrum',),
            ),
            **kw)
    with pytest.raises(ValueError):
        HybridCompositionValidationSpec.create(
            crossover_rule=CrossoverRuleSpec(
                rule_id=_RULE_ID,
                rule_version=_RULE_VERSION,
                required_agreement_evidence=('magnitude_agreement',),
            ),
            sensitivity_requirement=None,
            **kw)


def test_spec_coherent_blend_requires_phase_agreement_in_rule() -> None:
    with pytest.raises(ValueError):
        HybridCompositionValidationSpec.create(
            document_id='d',
            subject_ref=_ref('stitched_hybrid_response'),
            wave_qualification_ref=_ref('wave_qualification', 'wq'),
            ga_qualification_ref=_ref('ga_qualification', 'gq'),
            composition_semantics='coherent_complex',
            crossover_rule=CrossoverRuleSpec(
                rule_id=_RULE_ID,
                rule_version=_RULE_VERSION,
                required_agreement_evidence=('magnitude_agreement',),
            ),
            expected_regions=('wave_only', 'crossover_blend', 'ga_only'),
            ownership_policy=_policy(),
            sensitivity_requirement=SensitivityRequirement(
                admissible_range_hz=(1000.0, 2000.0),
                observables=('level_spectrum',),
            ),
            applicability=ApplicabilityEnvelope(),
        )


def test_spec_ownership_must_cover_every_region_contribution() -> None:
    policy = tuple(
        entry
        for entry in _policy()
        if (entry.region, entry.contribution)
        != ('ga_only', 'diffracted')
    )
    with pytest.raises(ValueError):
        HybridCompositionValidationSpec.create(
            document_id='d',
            subject_ref=_ref('stitched_hybrid_response'),
            wave_qualification_ref=_ref('wave_qualification', 'wq'),
            ga_qualification_ref=_ref('ga_qualification', 'gq'),
            composition_semantics='energetic',
            crossover_rule=CrossoverRuleSpec(
                rule_id=_RULE_ID,
                rule_version=_RULE_VERSION,
                required_agreement_evidence=('magnitude_agreement',),
            ),
            expected_regions=('wave_only', 'crossover_blend', 'ga_only'),
            ownership_policy=policy,
            sensitivity_requirement=SensitivityRequirement(
                admissible_range_hz=(1000.0, 2000.0),
                observables=('level_spectrum',),
            ),
            applicability=ApplicabilityEnvelope(),
        )


def test_spec_complementary_blend_owner_only_inside_blend() -> None:
    with pytest.raises(ValueError):
        _spec(
            blend=False,
            ownership_overrides={
                ('wave_only', 'direct'): 'complementary_blend',
            },
        )


def test_spec_rejects_duplicate_regions() -> None:
    with pytest.raises(ValueError):
        HybridCompositionValidationSpec.create(
            document_id='d',
            subject_ref=_ref('stitched_hybrid_response'),
            wave_qualification_ref=_ref('wave_qualification', 'wq'),
            ga_qualification_ref=_ref('ga_qualification', 'gq'),
            composition_semantics='energetic',
            expected_regions=('wave_only', 'wave_only'),
            ownership_policy=_policy(('wave_only',)),
            late_field_composed=False,
            grid_reconciliation_required=False,
            applicability=ApplicabilityEnvelope(),
        )


def test_seal_produces_prefix_and_matching_sha() -> None:
    spec = _spec()
    assert spec.spec_id.startswith('hvspec-')
    assert canonical_sha256(spec.identity_payload()) == spec.spec_sha256


# -------------------------------------------------------------- evidence


def test_evidence_kind_contracts() -> None:
    spec = _spec()
    with pytest.raises(ValueError):
        _ev(
            spec, 'component_qualification', outcome='satisfied',
            solver_path='wave_r130',
            achieved_level='numerically_verified',
            qualification_ref=None)
    with pytest.raises(ValueError):
        _ev(spec, 'common_input_binding', outcome='satisfied')
    with pytest.raises(ValueError):
        _ev(spec, 'region_evaluation', outcome='satisfied')
    with pytest.raises(ValueError):
        _ev(
            spec, 'region_evaluation', outcome='satisfied',
            domain_kind='gap', band_hz=(1000.0, 1400.0))
    with pytest.raises(ValueError):
        _ev(
            spec, 'crossover_selection_evidence', outcome='satisfied',
            rule_id=_RULE_ID, rule_version=_RULE_VERSION,
            agreement_kinds=('magnitude_agreement',))
    with pytest.raises(ValueError):
        _ev(
            spec, 'crossover_sensitivity', outcome='satisfied',
            swept_range_hz=(950.0, 2050.0))
    with pytest.raises(ValueError):
        _ev(
            spec, 'external_benchmark', outcome='satisfied',
            reference_class='measured_external')
    with pytest.raises(ValueError):
        _ev(
            spec, 'external_benchmark', outcome='satisfied',
            reference_class='fitted_calibrated',
            case_ref=_ref('benchmark_case', 'c'))
    with pytest.raises(ValueError):
        _ev(
            spec, 'late_field_decomposition', outcome='satisfied',
            late_contributions=(
                LateContribution(kind='specular', owner='wave_r130'),))
    with pytest.raises(ValueError):
        _ev(
            spec, 'grid_reconciliation_error', outcome='satisfied',
            reconciliation_method='cartesian_linear_v1',
            interpolation_error_bound=None)
    with pytest.raises(ValueError):
        _ev(
            spec, 'grid_reconciliation_error', outcome='satisfied',
            reconciliation_method='exact_bin_identity_v1',
            extrapolation_performed=True)
    with pytest.raises(ValueError):
        _ev(spec, 'numerical_verification', outcome='satisfied')


def test_phase_evidence_requires_all_eight_checks() -> None:
    spec = _spec(semantics='coherent_complex')
    partial = tuple(
        PhaseTimeCheck(check=name, outcome='satisfied')
        for name in _PHASE_CHECKS[:-1]
    )
    with pytest.raises(ValueError):
        _ev(
            spec, 'phase_time_alignment', outcome='inconclusive',
            phase_checks=partial)
    bad = _phase(spec, {'crossover_continuity': 'violated'})
    assert bad.outcome == 'inconclusive'
    with pytest.raises(ValueError):
        _ev(
            spec, 'phase_time_alignment', outcome='satisfied',
            phase_checks=bad.phase_checks)


# -------------------------------------------------------------- verdicts


def test_components_unqualified_when_evidence_missing() -> None:
    spec = _spec()
    verdict = evaluate_hybrid_validation(
        spec, [_component(spec, 'wave_r130')], 'doc-1')
    assert verdict.verdict == 'components_unqualified'
    assert verdict.qualification_level == 'none'
    assert 'component_qualification:geometric_r150' in (
        verdict.missing_requirements)


def test_components_unqualified_when_below_min_level() -> None:
    spec = _spec()
    verdict = evaluate_hybrid_validation(
        spec,
        [
            _component(spec, 'wave_r130'),
            _component(
                spec, 'geometric_r150', level='numerically_verified'),
            _binding(spec),
            _numerical(spec),
        ],
        'doc-1',
    )
    assert verdict.verdict == 'components_unqualified'


def test_common_inputs_unverified_without_binding() -> None:
    spec = _spec()
    verdict = evaluate_hybrid_validation(
        spec,
        [
            _component(spec, 'wave_r130'),
            _component(spec, 'geometric_r150'),
            _region(spec, 'wave_only'),
        ],
        'doc-1',
    )
    assert verdict.verdict == 'common_inputs_unverified'


def test_component_inheritance_denied_is_the_signature_state() -> None:
    spec = _spec()
    verdict = evaluate_hybrid_validation(
        spec,
        [
            _component(spec, 'wave_r130'),
            _component(spec, 'geometric_r150'),
            _binding(spec),
        ],
        'doc-1',
    )
    assert verdict.verdict == 'component_inheritance_denied'
    assert verdict.qualification_level == 'none'
    assert 'hybrid_specific_evidence' in verdict.missing_requirements


def test_region_evaluation_failed_on_violated_region() -> None:
    spec = _spec()
    rows = _full_evidence(spec)
    rows[3] = _region(spec, 'wave_only', outcome='violated')
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'region_evaluation_failed'
    wave = next(
        r for r in verdict.region_results
        if r.domain_kind == 'wave_only')
    assert wave.outcome == 'discrepant'
    assert wave.evidence_refs


def test_insufficient_evidence_when_region_row_missing() -> None:
    spec = _spec()
    rows = [
        row for row in _full_evidence(spec)
        if not (
            row.evidence_kind == 'region_evaluation'
            and row.domain_kind == 'ga_only'
        )
    ]
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'insufficient_evidence'
    assert 'region_evaluation:ga_only' in verdict.missing_requirements
    ga = next(
        r for r in verdict.region_results
        if r.domain_kind == 'ga_only')
    assert ga.outcome == 'insufficient_evidence'


def test_crossover_selection_unsupported_on_violated_rule() -> None:
    spec = _spec()
    rows = _full_evidence(spec)
    rows = [
        row
        for row in rows
        if row.evidence_kind != 'crossover_selection_evidence'
    ]
    rows.append(_selection(spec, outcome='violated'))
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'crossover_selection_unsupported'
    assert verdict.crossover_state == 'unsupported'


def test_crossover_selection_missing_is_insufficient() -> None:
    spec = _spec()
    rows = [
        row for row in _full_evidence(spec)
        if row.evidence_kind != 'crossover_selection_evidence'
    ]
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'insufficient_evidence'
    assert verdict.crossover_state == 'inconclusive'
    assert 'crossover_selection_evidence' in verdict.missing_requirements


def test_crossover_rule_version_mismatch_never_satisfies() -> None:
    spec = _spec()
    rows = [
        row for row in _full_evidence(spec)
        if row.evidence_kind != 'crossover_selection_evidence'
    ]
    rows.append(_selection(spec, rule_version='stale-0'))
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'insufficient_evidence'
    assert verdict.crossover_state == 'rule_mismatch'


def test_gap_accounting_required_when_no_blend() -> None:
    spec = _spec(blend=False)
    rows = [
        row for row in _full_evidence(spec)
        if row.domain_kind != 'gap'
    ]
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'insufficient_evidence'
    assert 'gap_accounting' in verdict.missing_requirements


def test_gap_preserved_is_explicit_not_hidden() -> None:
    spec = _spec(blend=False)
    verdict = evaluate_hybrid_validation(
        spec, _full_evidence(spec), 'doc-1')
    assert verdict.verdict == 'numerically_verified'
    assert len(verdict.gap_domains) == 1
    gap = verdict.gap_domains[0]
    assert gap.lower_hz == 1000.0 and gap.upper_hz == 1400.0
    assert any(
        r.domain_kind == 'gap' and r.outcome == 'gap_preserved'
        for r in verdict.region_results)


def test_double_count_unchecked_on_unassigned_owner() -> None:
    spec = _spec(
        ownership_overrides={('wave_only', 'direct'): 'unassigned'})
    verdict = evaluate_hybrid_validation(
        spec, _full_evidence(spec), 'doc-1')
    assert verdict.verdict == 'double_count_unchecked'
    assert verdict.ownership_state == 'incomplete_ownership'
    assert 'ownership_unassigned:wave_only:direct' in (
        verdict.missing_requirements)


def test_double_count_unchecked_on_not_present_conflict() -> None:
    spec = _spec(
        ownership_overrides={
            ('crossover_blend', 'diffracted'): 'not_present'})
    verdict = evaluate_hybrid_validation(
        spec, _full_evidence(spec), 'doc-1')
    assert verdict.verdict == 'double_count_unchecked'
    assert verdict.ownership_state == 'ownership_conflict'


def test_coherent_claim_unvalidated_without_phase_checks() -> None:
    spec = _spec(semantics='coherent_complex')
    rows = [
        row for row in _full_evidence(spec)
        if row.evidence_kind != 'phase_time_alignment'
    ]
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'coherent_claim_unvalidated'
    assert verdict.phase_time_state == 'checks_incomplete'
    assert not verdict.coherent_claim_allowed


def test_coherent_claim_unvalidated_on_violated_check() -> None:
    spec = _spec(semantics='coherent_complex')
    rows = [
        row for row in _full_evidence(spec)
        if row.evidence_kind != 'phase_time_alignment'
    ]
    rows.append(_phase(spec, {'shared_time_origin': 'violated'}))
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'coherent_claim_unvalidated'
    assert verdict.phase_time_state == 'violated'


def test_early_field_unverified_when_late_masks_early() -> None:
    spec = _spec()
    rows = [
        row for row in _full_evidence(spec)
        if row.evidence_kind != 'late_field_decomposition'
    ]
    rows.append(_late(spec, early='early_field_unverified'))
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'early_field_unverified'
    assert verdict.late_field_state == 'bounded_masking_early_unverified'


def test_early_field_unverified_when_late_row_missing() -> None:
    spec = _spec()
    rows = [
        row for row in _full_evidence(spec)
        if row.evidence_kind != 'late_field_decomposition'
    ]
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'early_field_unverified'
    assert verdict.late_field_state == 'unbounded'


def test_crossover_sensitive_on_material_change() -> None:
    spec = _spec()
    rows = [
        row for row in _full_evidence(spec)
        if row.evidence_kind != 'crossover_sensitivity'
    ]
    rows.append(_sens(spec, material=True))
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'crossover_sensitive'
    assert verdict.sensitivity_state == 'materially_sensitive'


def test_sensitivity_incomplete_coverage_is_insufficient() -> None:
    spec = _spec()
    rows = [
        row for row in _full_evidence(spec)
        if row.evidence_kind != 'crossover_sensitivity'
    ]
    rows.append(_sens(spec, observables=('level_spectrum',)))
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'insufficient_evidence'
    assert verdict.sensitivity_state == 'incomplete_coverage'
    assert 'crossover_sensitivity' in verdict.missing_requirements


def test_grid_reconciliation_unreconciled_is_insufficient() -> None:
    spec = _spec()
    rows = [
        row for row in _full_evidence(spec)
        if row.evidence_kind != 'grid_reconciliation_error'
    ]
    rows.append(_grid(spec, outcome='inconclusive'))
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'insufficient_evidence'
    assert verdict.grid_error_state == 'unreconciled'


def test_grid_extrapolation_is_flagged_explicitly() -> None:
    spec = _spec()
    rows = [
        row for row in _full_evidence(spec)
        if row.evidence_kind != 'grid_reconciliation_error'
    ]
    rows.append(_grid(spec, outcome='inconclusive', extrapolation=True))
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'insufficient_evidence'
    assert verdict.grid_error_state == 'extrapolation_used'


def test_numerically_verified_without_external_benchmark() -> None:
    spec = _spec()
    verdict = evaluate_hybrid_validation(
        spec, _full_evidence(spec), 'doc-1')
    assert verdict.verdict == 'numerically_verified'
    assert verdict.qualification_level == 'numerically_verified'
    assert verdict.external_benchmark_refs == ()
    assert verdict.applicability == spec.applicability
    assert all(
        r.outcome == 'validated' for r in verdict.region_results)


def test_same_code_fine_reference_is_not_external_truth() -> None:
    spec = _spec()
    verdict = evaluate_hybrid_validation(
        spec, _full_evidence(spec, external='same_code_fine_reference'),
        'doc-1')
    assert verdict.verdict == 'numerically_verified'
    assert 'same_code_fine_reference' in verdict.evidence_classes


def test_fitted_calibrated_reference_never_qualifies_external() -> None:
    spec = _spec()
    rows = _full_evidence(spec)
    rows.append(_benchmark(spec, ref_class='fitted_calibrated',
                           holdout=True))
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'numerically_verified'
    assert 'fitted_calibrated' in verdict.evidence_classes


def test_validated_bounded_with_measured_external() -> None:
    spec = _spec()
    verdict = evaluate_hybrid_validation(
        spec, _full_evidence(spec, external='measured_external'), 'doc-1')
    assert verdict.verdict == 'validated_bounded'
    assert verdict.qualification_level == 'external_benchmark_validated'
    assert verdict.missing_requirements == ()
    assert verdict.external_benchmark_refs
    assert verdict.applicability.band_hz == (20.0, 8000.0)
    assert verdict.applicability.phenomena == (
        'local_frequency_response',)


def test_independent_numerical_counts_as_external() -> None:
    spec = _spec()
    verdict = evaluate_hybrid_validation(
        spec, _full_evidence(spec, external='independent_numerical'),
        'doc-1')
    assert verdict.verdict == 'validated_bounded'


def test_coherent_validated_bounded_sets_claim_flag() -> None:
    spec = _spec(semantics='coherent_complex')
    verdict = evaluate_hybrid_validation(
        spec, _full_evidence(spec, external='measured_external'), 'doc-1')
    assert verdict.verdict == 'validated_bounded'
    assert verdict.phase_time_state == 'validated'
    assert verdict.coherent_claim_allowed
    assert verdict.crossover_state == 'selected_bounded'


def test_evidence_bound_to_other_spec_is_ignored() -> None:
    spec = _spec()
    other = _spec(document_id='doc-2')
    rows = _full_evidence(other)
    verdict = evaluate_hybrid_validation(spec, rows, 'doc-1')
    assert verdict.verdict == 'components_unqualified'


def test_verdict_is_sealed() -> None:
    spec = _spec()
    verdict = evaluate_hybrid_validation(
        spec, _full_evidence(spec), 'doc-1')
    assert verdict.verdict_id.startswith('hvv-')
    assert canonical_sha256(verdict.identity_payload()) == (
        verdict.verdict_sha256)


# ------------------------------------------------------------- repository


def test_repository_round_trip_all_records(tmp_path) -> None:
    repo = _repo(tmp_path)
    spec = _spec()
    evidence = _full_evidence(spec, external='measured_external')
    verdict = evaluate_hybrid_validation(spec, evidence, 'doc-1')
    repo.specs.save(spec)
    for row in evidence:
        repo.evidence.save(row)
    repo.verdicts.save(verdict)
    assert repo.get_spec(spec.spec_id) == spec
    assert repo.get_evidence(evidence[0].evidence_id) == evidence[0]
    assert repo.get_verdict(verdict.verdict_id) == verdict
    assert repo.list_specs('doc-1') == (spec,)
    assert len(repo.list_evidence('doc-1')) == len(evidence)
    assert repo.list_verdicts('doc-1') == (verdict,)


def test_repository_append_only_conflict(tmp_path) -> None:
    repo = _repo(tmp_path)
    spec = _spec()
    repo.specs.save(spec)
    tampered = _spec(
        document_id='doc-1', wave_min_level='owned_room_validated')
    object.__setattr__(tampered, 'spec_id', spec.spec_id)
    object.__setattr__(tampered, 'spec_sha256', spec.spec_sha256)
    with pytest.raises(
            (HybridCompositionValidationConflictError,
             HybridCompositionValidationIntegrityError)):
        repo.specs.save(tampered)
    repo.specs.save(spec)  # idempotent re-save


@pytest.mark.parametrize(
    'table,column,bad_value',
    [
        ('cad_hybrid_composition_validation_specs',
         'composition_semantics', 'coherent_complex'),
        ('cad_hybrid_validation_evidence', 'outcome', 'violated'),
        ('cad_hybrid_validation_verdicts', 'verdict', 'validated_bounded'),
    ],
)
def test_repository_detects_column_tampering(
    tmp_path, table, column, bad_value,
) -> None:
    repo = _repo(tmp_path)
    spec = _spec()
    row = _component(spec, 'wave_r130')
    verdict = evaluate_hybrid_validation(
        spec, [row, _component(spec, 'geometric_r150')], 'doc-1')
    repo.specs.save(spec)
    repo.evidence.save(row)
    repo.verdicts.save(verdict)
    rid_column = {
        'cad_hybrid_composition_validation_specs': (
            'spec_id', spec.spec_id),
        'cad_hybrid_validation_evidence': (
            'evidence_id', row.evidence_id),
        'cad_hybrid_validation_verdicts': (
            'verdict_id', verdict.verdict_id),
    }[table]
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            f'UPDATE {table} SET {column}=? WHERE {rid_column[0]}=?',
            (bad_value, rid_column[1]))
    with pytest.raises(HybridCompositionValidationIntegrityError):
        {
            'cad_hybrid_composition_validation_specs': repo.get_spec,
            'cad_hybrid_validation_evidence': repo.get_evidence,
            'cad_hybrid_validation_verdicts': repo.get_verdict,
        }[table](rid_column[1])


def test_repository_rejects_unsealed_save(tmp_path) -> None:
    repo = _repo(tmp_path)
    spec = _spec()
    broken = spec.model_copy(update={'spec_sha256': 'b' * 64})
    with pytest.raises(HybridCompositionValidationIntegrityError):
        repo.specs.save(broken)


def test_repository_list_filters_by_document(tmp_path) -> None:
    repo = _repo(tmp_path)
    a = _spec(document_id='doc-a')
    b = _spec(document_id='doc-b')
    repo.specs.save(a)
    repo.specs.save(b)
    assert repo.list_specs('doc-a') == (a,)
    assert repo.list_specs() == (a, b)


# ----------------------------------------------------------------- labels


def test_ja_label_coverage_all_verdicts() -> None:
    from typing import get_args
    from htdt.cad_hybrid_composition_validation import (
        HybridValidationVerdictKind,
    )
    for code in get_args(HybridValidationVerdictKind):
        assert code in HYBRID_VALIDATION_LABELS
        assert HYBRID_VALIDATION_LABELS[code]


def test_ja_label_accessors() -> None:
    assert hybrid_validation_verdict_line(
        'component_inheritance_denied').startswith('ハイブリッド検証:')
    assert hybrid_validation_label('wave_only') == '波動ソルバー専有領域'
    assert hybrid_validation_label('__none__') == '__none__'
