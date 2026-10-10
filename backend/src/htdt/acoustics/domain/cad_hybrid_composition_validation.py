"""R160 hybrid composition validation authority (issue #812).

R160's hybrid model — wave solver below the crossover, geometric acoustics
above it — is a new model form, not a roll-up of its parts. R130 PASS plus
R150 PASS does not entail R160 PASS: the composition itself needs its own
verification/validation envelope or hybrid results inherit unearned
credibility.

This module is the validation authority for that model form:

* ``HybridCompositionValidationSpec`` (hvspec-) declares what must be
  verified for a composed result: the component solver qualification
  minimums, the expected region structure using the stitching vocabulary
  (``wave_only`` / ``crossover_blend`` / ``ga_only``), the versioned
  crossover-selection rule and the evidence it requires, the disjoint
  contribution-ownership policy that prevents double counting, the
  preregistered crossover-sensitivity sweep, and the bounded applicability
  envelope the pass verdict is valid within.
* ``HybridValidationEvidence`` (hve-) is one sealed evidence row — a
  component qualification, a per-region evaluation, the crossover-selection
  rule discharge, phase/time-alignment checks for coherent composition,
  the frequency-grid reconciliation error bound, the sensitivity sweep,
  an external benchmark class, or a late-field decomposition.
* ``HybridValidationVerdict`` (hvv-) is the sealed verdict produced by
  ``evaluate_hybrid_validation`` — fail closed, per-region results embedded
  so wave-only / crossover / GA-only regions stay separately inspectable,
  and explicit gap accounting (``HybridStitchGap``) when no valid overlap
  exists.

Evidence classes deliberately distinguish measured external truth
(``measured_external``), an independent numerical reference
(``independent_numerical``), a same-code fine-grid reference
(``same_code_fine_reference``), and a fitted/calibrated reference
(``fitted_calibrated``). Only the first two qualify the external
benchmark gate; same-code references support numerical verification only,
and a fitted reference never qualifies predictive validity without a
separate holdout reference.

Basis: issue #812 scope and acceptance criteria; ISO/IEC/IEEE 29119 and
ASME V&V model-form concepts (a composition of validated components is
itself a model requiring V&V); HTDT R160 stitching conventions
(cad_hybrid_stitching.py) for region and gap identity.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...cad_authority_resolver import AuthorityRef
from .cad_hybrid_grid_reconciliation import GridReconciliationMethod
from .cad_hybrid_stitching import HybridStitchGap, HybridStitchRegionKind
from .cad_solver_confidence_bound import ClaimClass
from ...canonical_json import canonical_sha256 as _hash, canonicalize_payload

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

HYBRID_VALIDATION_SPEC_VERSION = 'r160-hybrid-validation-spec-1'
HYBRID_VALIDATION_EVIDENCE_VERSION = 'r160-hybrid-validation-evidence-1'
HYBRID_VALIDATION_VERDICT_VERSION = 'r160-hybrid-validation-verdict-1'
HYBRID_VALIDATION_EVALUATOR_VERSION = (
    'r160-hybrid-composition-validation-1')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise HybridCompositionValidationIntegrityError(
                f'{ref.kind} reference must pin its sha256')


class HybridCompositionValidationIntegrityError(ValueError):
    """A sealed hybrid-validation record failed integrity checks."""


# --------------------------------------------------------------------------
# Vocabulary — reuses R160 stitching region identities rather than
# inventing parallel names (issue #812: report wave-only / overlap /
# GA-only regions separately inspectable; gaps remain explicit).
# --------------------------------------------------------------------------

HybridValidationDomainKind = Literal[
    'wave_only', 'crossover_blend', 'ga_only', 'gap']

HybridSolverPath = Literal['wave_r130', 'geometric_r150']

HybridContributionKind = Literal[
    'direct',
    'specular_reflection',
    'scattered_late',
    'diffracted',
    'modal_coherent',
]

ContributionOwner = Literal[
    'wave_r130',
    'geometric_r150',
    'complementary_blend',
    'not_present',
    'unassigned',
]

CompositionSemantics = Literal['coherent_complex', 'energetic']

CrossoverAgreementKind = Literal[
    'magnitude_agreement',
    'relative_discrepancy_bound',
    'phase_time_compatibility',
    'grid_point_agreement',
    'tolerance_band_membership',
    'narrowband_resonance_agreement',
]

PhaseTimeCheckKind = Literal[
    'coherent_phase_authority_both',
    'shared_time_origin',
    'compatible_fourier_sign_convention',
    'compatible_propagation_timing',
    'phase_reference_alignment',
    'interference_structure',
    'crossover_continuity',
    'group_delay_consistency',
]

SensitivityObservable = Literal[
    'level_spectrum',
    'level_stability',
    'decay_time',
    'phase_metric',
    'early_late_structure',
]

LateContributionKind = Literal['specular', 'scattered', 'diffracted']

EarlyFieldState = Literal[
    'early_field_separately_verified', 'early_field_unverified']

HybridEvidenceKind = Literal[
    'component_qualification',
    'common_input_binding',
    'region_evaluation',
    'crossover_selection_evidence',
    'phase_time_alignment',
    'grid_reconciliation_error',
    'crossover_sensitivity',
    'external_benchmark',
    'late_field_decomposition',
    'numerical_verification',
]

EvidenceOutcome = Literal[
    'satisfied', 'violated', 'inconclusive', 'not_applicable']

HybridReferenceEvidenceClass = Literal[
    'measured_external',
    'independent_numerical',
    'same_code_fine_reference',
    'fitted_calibrated',
]

ComponentQualificationLevel = Literal[
    'numerically_verified',
    'external_benchmark_validated',
    'owned_room_validated',
]

LevelRank: dict[str, int] = {
    'numerically_verified': 1,
    'external_benchmark_validated': 2,
    'owned_room_validated': 3,
}

RegionOutcome = Literal[
    'validated', 'discrepant', 'insufficient_evidence', 'gap_preserved']

CrossoverState = Literal[
    'not_applicable_no_blend',
    'unsupported',
    'inconclusive',
    'rule_mismatch',
    'selected_bounded',
]

OwnershipState = Literal[
    'disjoint_ownership_declared',
    'incomplete_ownership',
    'ownership_conflict',
]

PhaseTimeState = Literal[
    'not_required_energetic',
    'checks_incomplete',
    'violated',
    'validated',
]

GridErrorState = Literal[
    'not_required',
    'unreconciled',
    'extrapolation_used',
    'bounded',
]

SensitivityState = Literal[
    'not_applicable',
    'unevaluated',
    'incomplete_coverage',
    'within_envelope',
    'materially_sensitive',
]

LateFieldState = Literal[
    'not_composed',
    'unbounded',
    'bounded_masking_early_unverified',
    'bounded_separate',
]

HybridQualificationLevel = Literal[
    'none', 'numerically_verified', 'external_benchmark_validated']

HybridValidationVerdictKind = Literal[
    'validated_bounded',
    'numerically_verified',
    'insufficient_evidence',
    'region_evaluation_failed',
    'component_inheritance_denied',
    'components_unqualified',
    'common_inputs_unverified',
    'crossover_selection_unsupported',
    'double_count_unchecked',
    'coherent_claim_unvalidated',
    'crossover_sensitive',
    'early_field_unverified',
]

HYBRID_VALIDATION_LABELS: dict[str, str] = {
    'validated_bounded': '範囲限定で検証済み',
    'numerically_verified': '数値的に検証済み（外部実証なし）',
    'insufficient_evidence': '証拠不足',
    'region_evaluation_failed': '領域評価が不一致',
    'component_inheritance_denied': 'コンポーネント継承による検証は不可',
    'components_unqualified': 'コンポーネントソルバー未認定',
    'common_inputs_unverified': '共通入力未検証',
    'crossover_selection_unsupported': 'クロスオーバー選定未支持',
    'double_count_unchecked': '二重計上の未チェック',
    'coherent_claim_unvalidated': 'コヒーレント主張未検証',
    'crossover_sensitive': 'クロスオーバー感度あり',
    'early_field_unverified': '初期反射場未検証',
    'wave_only': '波動ソルバー専有領域',
    'crossover_blend': 'クロスオーバー重畳領域',
    'ga_only': '幾何音響専有領域',
    'gap': '未カバー帯域（ギャップ）',
    'coherent_complex': 'コヒーレント複素合成',
    'energetic': 'エネルギー合成',
    'measured_external': '外部実測参照',
    'independent_numerical': '独立数値参照',
    'same_code_fine_reference': '同一コード細格子参照',
    'fitted_calibrated': 'フィッティング/校正済み参照',
    'none': '認定なし',
    'external_benchmark_validated': '外部ベンチマーク検証済み',
    'owned_room_validated': '保有ルーム検証済み',
    'not_applicable_no_blend': '重畳なし（対象外）',
    'unsupported': '未支持',
    'inconclusive': '不確定',
    'rule_mismatch': 'ルール不一致',
    'selected_bounded': '範囲限定で選定済み',
    'disjoint_ownership_declared': '排他的帰属を宣言済み',
    'incomplete_ownership': '帰属不完全',
    'ownership_conflict': '帰属矛盾あり',
    'not_required_energetic': 'エネルギー合成のため不要',
    'checks_incomplete': '検査不完全',
    'violated': '違反あり',
    'validated': '検証済み',
    'discrepant': '不一致',
    'gap_preserved': 'ギャップを保持',
    'not_required': '不要',
    'unreconciled': '未調整',
    'extrapolation_used': '外挿が使用された',
    'bounded': '誤差限界あり',
    'not_applicable': '対象外',
    'unevaluated': '未評価',
    'incomplete_coverage': 'カバレッジ不完全',
    'within_envelope': '許容範囲内',
    'materially_sensitive': '実質感度あり',
    'not_composed': '後部場未合成',
    'unbounded': '限界なし',
    'bounded_masking_early_unverified': '初期場未検証を覆い隠す限界あり',
    'bounded_separate': '分離済みの限界あり',
}


# --------------------------------------------------------------------------
# Embedded records
# --------------------------------------------------------------------------


class ContributionOwnerEntry(BaseModel):
    """One declared contribution owner inside one region — the disjoint-
    ownership policy row that prevents double counting (issue #812 §3)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    region: HybridStitchRegionKind
    contribution: HybridContributionKind
    owner: ContributionOwner


class CrossoverRuleSpec(BaseModel):
    """The versioned crossover-selection rule the spec requires and the
    agreement evidence kinds that must be on record (issue #812 §1)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    required_agreement_evidence: tuple[CrossoverAgreementKind, ...]


class SensitivityRequirement(BaseModel):
    """Preregistered admissible crossover range plus the observables the
    sensitivity sweep must evaluate (issue #812 §5)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    admissible_range_hz: tuple[float, float]
    observables: tuple[SensitivityObservable, ...]

    @model_validator(mode='after')
    def _validate(self) -> 'SensitivityRequirement':
        lo, hi = self.admissible_range_hz
        if not (lo > 0.0 and lo < hi):
            raise HybridCompositionValidationIntegrityError(
                'admissible_range_hz requires 0 < lower < upper')
        if not self.observables:
            raise HybridCompositionValidationIntegrityError(
                'at least one sensitivity observable is required')
        if len(set(self.observables)) != len(self.observables):
            raise HybridCompositionValidationIntegrityError(
                'duplicate sensitivity observables')
        return self


class ApplicabilityEnvelope(BaseModel):
    """The explicit boundary of what a pass verdict means: frequency band,
    claim phenomena, and deployment contexts (issue #812 §8)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    band_hz: tuple[float, float] | None = None
    phenomena: tuple[ClaimClass, ...] = ()
    contexts: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'ApplicabilityEnvelope':
        if self.band_hz is not None:
            lo, hi = self.band_hz
            if not (lo > 0.0 and lo < hi):
                raise HybridCompositionValidationIntegrityError(
                    'applicability band_hz requires 0 < lower < upper')
        if len(set(self.phenomena)) != len(self.phenomena):
            raise HybridCompositionValidationIntegrityError(
                'duplicate applicability phenomena')
        return self


class SensitivityPoint(BaseModel):
    """One evaluated crossover position inside the swept admissible
    range (issue #812 §5)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    crossover_hz: float = Field(gt=0.0)
    observable: SensitivityObservable
    material_change: bool = False
    detail: str = ''


class PhaseTimeCheck(BaseModel):
    """One phase/time-alignment check outcome — the gate a coherent
    (complex-valued) composition claim must pass (issue #812 §4)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    check: PhaseTimeCheckKind
    outcome: EvidenceOutcome
    detail: str = ''


class LateContribution(BaseModel):
    """One late-field contribution kind with its declared owner — lets
    the evaluator see whether late-field agreement was produced
    separately from early-reflection behavior (issue #812 §6)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: LateContributionKind
    owner: HybridSolverPath
    present: bool = True
    energy_share: float | None = Field(default=None, ge=0.0, le=1.0)
    provenance_ref: AuthorityRef | None = None


class HybridMetricValue(BaseModel):
    """One measured/derived metric an evidence row records against its
    own tolerance — never normalized into a universal score."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    name: str = Field(min_length=1)
    value: float
    tolerance: float | None = Field(default=None, gt=0.0)
    within_tolerance: bool | None = None
    unit: str | None = None


class HybridRegionResult(BaseModel):
    """Embedded per-region inspection row on the verdict — wave-only,
    overlap, GA-only (and gaps) stay separately visible (issue #812 §2)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    domain_kind: HybridValidationDomainKind
    band_hz: tuple[float, float] | None = None
    outcome: RegionOutcome
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'HybridRegionResult':
        if self.band_hz is not None:
            lo, hi = self.band_hz
            if not (lo > 0.0 and lo < hi):
                raise HybridCompositionValidationIntegrityError(
                    'region band_hz requires 0 < lower < upper')
        if self.domain_kind == 'gap' and self.outcome != 'gap_preserved':
            raise HybridCompositionValidationIntegrityError(
                "a 'gap' domain row must carry outcome 'gap_preserved'")
        if self.domain_kind != 'gap' and self.outcome == 'gap_preserved':
            raise HybridCompositionValidationIntegrityError(
                "only 'gap' domain rows may carry 'gap_preserved'")
        return self


# --------------------------------------------------------------------------
# Sealed records
# --------------------------------------------------------------------------


class HybridCompositionValidationSpec(BaseModel):
    """Pinned verification/validation contract for one composed hybrid
    result (hvspec- prefix).

    The spec declares the model-form's obligations before evidence is
    collected: component minimums, expected regions, the versioned
    crossover-selection rule, the complete disjoint ownership policy
    (every expected region x every contribution kind must be declared —
    ``unassigned`` is an explicit declaration the evaluator fails on),
    the sensitivity sweep requirement, and the applicability envelope.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    spec_id: str
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    subject_ref: AuthorityRef
    wave_qualification_ref: AuthorityRef
    ga_qualification_ref: AuthorityRef
    wave_min_level: ComponentQualificationLevel = 'numerically_verified'
    ga_min_level: ComponentQualificationLevel = 'external_benchmark_validated'
    composition_semantics: CompositionSemantics
    crossover_rule: CrossoverRuleSpec | None = None
    expected_regions: tuple[HybridStitchRegionKind, ...]
    ownership_policy: tuple[ContributionOwnerEntry, ...]
    late_field_composed: bool = True
    grid_reconciliation_required: bool = True
    sensitivity_requirement: SensitivityRequirement | None = None
    applicability: ApplicabilityEnvelope
    authority_version: Literal[
        'r160-hybrid-validation-spec-1'] = HYBRID_VALIDATION_SPEC_VERSION

    @model_validator(mode='after')
    def _validate(self) -> 'HybridCompositionValidationSpec':
        _require_refs(
            self.subject_ref,
            self.wave_qualification_ref,
            self.ga_qualification_ref,
        )
        if not self.expected_regions:
            raise HybridCompositionValidationIntegrityError(
                'expected_regions must declare at least one region')
        if len(set(self.expected_regions)) != len(self.expected_regions):
            raise HybridCompositionValidationIntegrityError(
                'duplicate expected regions')
        has_blend = 'crossover_blend' in self.expected_regions
        if has_blend and self.crossover_rule is None:
            raise HybridCompositionValidationIntegrityError(
                'crossover_blend region requires a crossover_rule')
        if has_blend and self.sensitivity_requirement is None:
            raise HybridCompositionValidationIntegrityError(
                'crossover_blend region requires a preregistered '
                'sensitivity_requirement')
        if (
            has_blend
            and self.composition_semantics == 'coherent_complex'
            and 'phase_time_compatibility'
            not in self.crossover_rule.required_agreement_evidence
        ):
            raise HybridCompositionValidationIntegrityError(
                'coherent composition through a blend requires the '
                'crossover rule to demand phase_time_compatibility '
                'evidence')
        seen: set[tuple[str, str]] = set()
        for entry in self.ownership_policy:
            key = (entry.region, entry.contribution)
            if key in seen:
                raise HybridCompositionValidationIntegrityError(
                    'duplicate ownership policy entry for '
                    f'{entry.region}/{entry.contribution}')
            seen.add(key)
            if (
                entry.owner == 'complementary_blend'
                and entry.region != 'crossover_blend'
            ):
                raise HybridCompositionValidationIntegrityError(
                    "'complementary_blend' ownership is only valid inside "
                    'crossover_blend')
        for region in self.expected_regions:
            for contribution in (
                'direct',
                'specular_reflection',
                'scattered_late',
                'diffracted',
                'modal_coherent',
            ):
                if (region, contribution) not in seen:
                    raise HybridCompositionValidationIntegrityError(
                        'ownership policy must declare every '
                        f'contribution for region {region} — missing '
                        f'{contribution}')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'spec_id', 'spec_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'HybridCompositionValidationSpec':
        return _seal(
            cls, payload, 'spec_id', 'spec_sha256', 'hvspec')


class HybridValidationEvidence(BaseModel):
    """One sealed evidence row bound to a spec (hve- prefix).

    ``evidence_kind`` selects which fields are meaningful — the model
    validator enforces the per-kind contract so a malformed row can never
    read as supporting evidence:

    * ``component_qualification`` — solver_path + achieved_level +
      qualification_ref (the component's own R130/R150 record).
    * ``common_input_binding`` — provenance_refs pinning the exact input
      authorities both solvers consumed (same geometry/materials/
      source/receiver).
    * ``region_evaluation`` — domain_kind row per region (or 'gap' with
      gap_reason); contributions_present lists what physics produced
      energy there.
    * ``crossover_selection_evidence`` — rule_id/rule_version discharge;
      'satisfied' requires agreement_kinds + the selected band_hz.
    * ``phase_time_alignment`` — the eight phase/time checks; 'satisfied'
      requires every check satisfied.
    * ``grid_reconciliation_error`` — reconciliation_method + the
      interpolation error bound; 'satisfied' forbids extrapolation.
    * ``crossover_sensitivity`` — swept_range_hz + sensitivity_points.
    * ``external_benchmark`` — reference_class + case_ref;
      fitted_calibrated requires a separate holdout_ref.
    * ``late_field_decomposition`` — late_contributions + early_field_state.
    * ``numerical_verification`` — same-code/self-consistency checks with
      metrics or provenance.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    spec_ref: AuthorityRef
    evidence_kind: HybridEvidenceKind
    outcome: EvidenceOutcome
    band_hz: tuple[float, float] | None = None
    domain_kind: HybridValidationDomainKind | None = None
    solver_path: HybridSolverPath | None = None
    achieved_level: ComponentQualificationLevel | None = None
    qualification_ref: AuthorityRef | None = None
    rule_id: str | None = None
    rule_version: str | None = None
    agreement_kinds: tuple[CrossoverAgreementKind, ...] = ()
    reference_class: HybridReferenceEvidenceClass | None = None
    case_ref: AuthorityRef | None = None
    holdout_ref: AuthorityRef | None = None
    swept_range_hz: tuple[float, float] | None = None
    sensitivity_points: tuple[SensitivityPoint, ...] = ()
    phase_checks: tuple[PhaseTimeCheck, ...] = ()
    late_contributions: tuple[LateContribution, ...] = ()
    early_field_state: EarlyFieldState | None = None
    reconciliation_method: GridReconciliationMethod | None = None
    interpolation_error_bound: float | None = Field(
        default=None, ge=0.0)
    extrapolation_performed: bool = False
    narrow_resonance_checked: bool = False
    gap_reason: str | None = None
    contributions_present: tuple[HybridContributionKind, ...] = ()
    metrics: tuple[HybridMetricValue, ...] = ()
    provenance_refs: tuple[AuthorityRef, ...] = ()
    limitations: tuple[str, ...] = ()
    detail: str = ''
    authority_version: Literal[
        'r160-hybrid-validation-evidence-1'
    ] = HYBRID_VALIDATION_EVIDENCE_VERSION

    @model_validator(mode='after')
    def _validate(self) -> 'HybridValidationEvidence':
        _require_refs(self.spec_ref, *self.provenance_refs)
        for ref in (
            self.qualification_ref,
            self.case_ref,
            self.holdout_ref,
        ):
            if ref is not None:
                _require_refs(ref)
        if self.band_hz is not None:
            lo, hi = self.band_hz
            if not (lo > 0.0 and lo < hi):
                raise HybridCompositionValidationIntegrityError(
                    'band_hz requires 0 < lower < upper')
        if self.swept_range_hz is not None:
            lo, hi = self.swept_range_hz
            if not (lo > 0.0 and lo < hi):
                raise HybridCompositionValidationIntegrityError(
                    'swept_range_hz requires 0 < lower < upper')
        kind = self.evidence_kind
        if kind == 'component_qualification':
            if (
                self.solver_path is None
                or self.achieved_level is None
                or self.qualification_ref is None
            ):
                raise HybridCompositionValidationIntegrityError(
                    'component_qualification requires solver_path, '
                    'achieved_level and qualification_ref')
            if self.domain_kind is not None:
                raise HybridCompositionValidationIntegrityError(
                    'component_qualification is not a region row')
        elif kind == 'common_input_binding':
            if not self.provenance_refs:
                raise HybridCompositionValidationIntegrityError(
                    'common_input_binding requires provenance_refs '
                    'pinning the shared input authorities')
        elif kind == 'region_evaluation':
            if self.domain_kind is None:
                raise HybridCompositionValidationIntegrityError(
                    'region_evaluation requires domain_kind')
            if self.domain_kind == 'gap' and not self.gap_reason:
                raise HybridCompositionValidationIntegrityError(
                    "a 'gap' evaluation requires gap_reason")
        elif kind == 'crossover_selection_evidence':
            if self.rule_id is None or self.rule_version is None:
                raise HybridCompositionValidationIntegrityError(
                    'crossover_selection_evidence requires rule_id and '
                    'rule_version')
            if self.outcome == 'satisfied' and (
                not self.agreement_kinds or self.band_hz is None
            ):
                raise HybridCompositionValidationIntegrityError(
                    "a 'satisfied' selection requires agreement_kinds "
                    'and the selected band_hz')
        elif kind == 'phase_time_alignment':
            required = set(_PHASE_CHECK_KINDS)
            seen = {check.check for check in self.phase_checks}
            if seen != required:
                raise HybridCompositionValidationIntegrityError(
                    'phase_time_alignment must record all eight check '
                    'kinds — missing '
                    + ', '.join(sorted(required - seen)))
            if self.outcome == 'satisfied' and any(
                check.outcome != 'satisfied' for check in self.phase_checks
            ):
                raise HybridCompositionValidationIntegrityError(
                    "a 'satisfied' phase/time row requires every check "
                    'satisfied')
        elif kind == 'grid_reconciliation_error':
            if self.reconciliation_method is None:
                raise HybridCompositionValidationIntegrityError(
                    'grid_reconciliation_error requires '
                    'reconciliation_method')
            if self.outcome == 'satisfied':
                if self.extrapolation_performed:
                    raise HybridCompositionValidationIntegrityError(
                        "a 'satisfied' reconciliation never extrapolates")
                if (
                    self.reconciliation_method != 'exact_bin_identity_v1'
                    and self.interpolation_error_bound is None
                ):
                    raise HybridCompositionValidationIntegrityError(
                        'an interpolating reconciliation requires '
                        'interpolation_error_bound')
        elif kind == 'crossover_sensitivity':
            if self.swept_range_hz is None or not self.sensitivity_points:
                raise HybridCompositionValidationIntegrityError(
                    'crossover_sensitivity requires swept_range_hz and '
                    'at least one sensitivity point')
        elif kind == 'external_benchmark':
            if self.reference_class is None or self.case_ref is None:
                raise HybridCompositionValidationIntegrityError(
                    'external_benchmark requires reference_class and '
                    'case_ref')
            if (
                self.reference_class == 'fitted_calibrated'
                and self.holdout_ref is None
            ):
                raise HybridCompositionValidationIntegrityError(
                    'a fitted_calibrated reference requires a separate '
                    'holdout_ref — tuning then claiming generic '
                    'predictive validity is rejected')
        elif kind == 'late_field_decomposition':
            if (
                not self.late_contributions
                or self.early_field_state is None
            ):
                raise HybridCompositionValidationIntegrityError(
                    'late_field_decomposition requires '
                    'late_contributions and early_field_state')
        elif kind == 'numerical_verification':
            if not self.provenance_refs and not self.metrics:
                raise HybridCompositionValidationIntegrityError(
                    'numerical_verification requires metrics or '
                    'provenance_refs')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'HybridValidationEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'hve')


class HybridValidationVerdict(BaseModel):
    """Sealed per-spec verdict (hvv- prefix).

    ``verdict`` is the terminal state; the embedded ``region_results``
    keep wave-only / blend / GA-only outcomes separately inspectable and
    ``gap_domains`` records every uncovered interval explicitly. The
    sub-state fields (crossover/ownership/phase/grid/sensitivity/late)
    explain *why* the verdict landed where it did — a reader never has
    to re-derive it from raw evidence.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    verdict_id: str
    verdict_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    spec_ref: AuthorityRef
    subject_ref: AuthorityRef
    verdict: HybridValidationVerdictKind
    qualification_level: HybridQualificationLevel
    composition_semantics: CompositionSemantics
    coherent_claim_allowed: bool
    region_results: tuple[HybridRegionResult, ...]
    gap_domains: tuple[HybridStitchGap, ...]
    crossover_state: CrossoverState
    ownership_state: OwnershipState
    phase_time_state: PhaseTimeState
    grid_error_state: GridErrorState
    sensitivity_state: SensitivityState
    late_field_state: LateFieldState
    evidence_classes: tuple[HybridReferenceEvidenceClass, ...]
    external_benchmark_refs: tuple[AuthorityRef, ...]
    component_refs: tuple[AuthorityRef, ...]
    missing_requirements: tuple[str, ...]
    applicability: ApplicabilityEnvelope
    rationale: str = ''
    authority_version: Literal[
        'r160-hybrid-validation-verdict-1'
    ] = HYBRID_VALIDATION_VERDICT_VERSION
    evaluator_version: Literal[
        'r160-hybrid-composition-validation-1'
    ] = HYBRID_VALIDATION_EVALUATOR_VERSION

    @model_validator(mode='after')
    def _validate(self) -> 'HybridValidationVerdict':
        _require_refs(
            self.spec_ref,
            self.subject_ref,
            *self.component_refs,
            *self.external_benchmark_refs,
        )
        if self.verdict == 'component_inheritance_denied' and (
            self.qualification_level != 'none'
        ):
            raise HybridCompositionValidationIntegrityError(
                'component_inheritance_denied requires qualification '
                "level 'none'")
        if self.verdict == 'validated_bounded':
            if self.qualification_level != 'external_benchmark_validated':
                raise HybridCompositionValidationIntegrityError(
                    'validated_bounded requires external_benchmark_validated')
            if self.missing_requirements:
                raise HybridCompositionValidationIntegrityError(
                    'validated_bounded with open missing_requirements '
                    'is a contradiction')
            if self.ownership_state != 'disjoint_ownership_declared':
                raise HybridCompositionValidationIntegrityError(
                    'validated_bounded requires disjoint ownership')
            if any(
                result.outcome in ('discrepant', 'insufficient_evidence')
                for result in self.region_results
            ):
                raise HybridCompositionValidationIntegrityError(
                    'validated_bounded with an unvalidated or discrepant '
                    'region is a contradiction')
        if self.verdict == 'numerically_verified' and (
            self.qualification_level == 'external_benchmark_validated'
        ):
            raise HybridCompositionValidationIntegrityError(
                "numerically_verified verdict cannot claim the external "
                'level')
        if self.coherent_claim_allowed and (
            self.composition_semantics != 'coherent_complex'
            or self.phase_time_state != 'validated'
        ):
            raise HybridCompositionValidationIntegrityError(
                'coherent_claim_allowed requires coherent semantics '
                'and validated phase/time alignment')
        if (
            self.composition_semantics == 'coherent_complex'
            and self.verdict in ('validated_bounded', 'numerically_verified')
            and self.phase_time_state != 'validated'
        ):
            raise HybridCompositionValidationIntegrityError(
                'a pass verdict on coherent composition requires '
                'validated phase/time alignment')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'verdict_id', 'verdict_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'HybridValidationVerdict':
        return _seal(
            cls, payload, 'verdict_id', 'verdict_sha256', 'hvv')


# --------------------------------------------------------------------------
# Evaluator — fail closed; unknown/insufficient evidence never reads as
# success, and component-solver passes alone can never validate the hybrid.
# --------------------------------------------------------------------------

_HYBRID_SPECIFIC_KINDS = {
    'region_evaluation',
    'crossover_selection_evidence',
    'phase_time_alignment',
    'grid_reconciliation_error',
    'crossover_sensitivity',
    'external_benchmark',
    'late_field_decomposition',
    'numerical_verification',
}

_EXTERNAL_QUALIFYING_CLASSES = {
    'measured_external',
    'independent_numerical',
}

_PHASE_CHECK_KINDS = (
    'coherent_phase_authority_both',
    'shared_time_origin',
    'compatible_fourier_sign_convention',
    'compatible_propagation_timing',
    'phase_reference_alignment',
    'interference_structure',
    'crossover_continuity',
    'group_delay_consistency',
)


def _rows(
    evidence: Sequence[HybridValidationEvidence],
    kind: HybridEvidenceKind,
) -> list[HybridValidationEvidence]:
    return [row for row in evidence if row.evidence_kind == kind]


def _satisfied(
    evidence: Sequence[HybridValidationEvidence],
    kind: HybridEvidenceKind,
) -> list[HybridValidationEvidence]:
    return [
        row
        for row in evidence
        if row.evidence_kind == kind and row.outcome == 'satisfied'
    ]


def evaluate_hybrid_validation(
    spec: HybridCompositionValidationSpec,
    evidence: Sequence[HybridValidationEvidence],
    document_id: str,
) -> HybridValidationVerdict:
    """Evaluate hybrid composition evidence against the spec and seal a
    fail-closed verdict.

    Ordering matters: the first terminal state wins, so component
    qualification is checked before hybrid evidence, and the
    'component_inheritance_denied' signature verdict only fires when the
    components ARE qualified but no hybrid-specific evidence exists —
    that is the exact "R130 PASS + R150 PASS != R160 PASS" condition.
    """

    rows = [
        row
        for row in evidence
        if row.spec_ref.ref_id == spec.spec_id
    ]
    missing: list[str] = []
    has_blend = 'crossover_blend' in spec.expected_regions

    # --- 1. Component qualification gates ---------------------------------
    component_refs: list[AuthorityRef] = []
    components_ok = True
    for path, min_level, qual_ref in (
        ('wave_r130', spec.wave_min_level, spec.wave_qualification_ref),
        ('geometric_r150', spec.ga_min_level, spec.ga_qualification_ref),
    ):
        match = [
            row
            for row in _satisfied(rows, 'component_qualification')
            if row.solver_path == path
            and row.qualification_ref is not None
            and row.qualification_ref.ref_id == qual_ref.ref_id
            and LevelRank[row.achieved_level] >= LevelRank[min_level]
        ]
        if match:
            component_refs.append(match[0].qualification_ref)
        else:
            components_ok = False
            missing.append(f'component_qualification:{path}')
    if not components_ok:
        return _verdict(
            spec=spec,
            document_id=document_id,
            verdict='components_unqualified',
            level='none',
            missing=missing,
            rationale='component solver qualification below the spec '
            'minimum — hybrid validation cannot start',
        )

    # --- 2. Common input binding ------------------------------------------
    if not _satisfied(rows, 'common_input_binding'):
        missing.append('common_input_binding')
        return _verdict(
            spec=spec,
            document_id=document_id,
            verdict='common_inputs_unverified',
            level='none',
            component_refs=component_refs,
            missing=missing,
            rationale='no evidence both solvers consumed the same pinned '
            'inputs',
        )

    # --- 3. The signature fail-closed state -------------------------------
    hybrid_rows = [
        row for row in rows if row.evidence_kind in _HYBRID_SPECIFIC_KINDS
    ]
    if not hybrid_rows:
        return _verdict(
            spec=spec,
            document_id=document_id,
            verdict='component_inheritance_denied',
            level='none',
            component_refs=component_refs,
            missing=['hybrid_specific_evidence'],
            rationale='passing component solvers alone cannot mark the '
            'hybrid production validated',
        )

    # --- 4. Region evaluations --------------------------------------------
    region_results: list[HybridRegionResult] = []
    gap_domains: list[HybridStitchGap] = []
    region_failed = False
    for region in spec.expected_regions:
        region_rows = [
            row
            for row in _rows(rows, 'region_evaluation')
            if row.domain_kind == region
        ]
        if not region_rows:
            missing.append(f'region_evaluation:{region}')
            region_results.append(
                HybridRegionResult(
                    domain_kind=region,
                    outcome='insufficient_evidence',
                )
            )
            continue
        outcomes = {row.outcome for row in region_rows}
        refs = [row.evidence_id for row in region_rows]
        band = region_rows[0].band_hz
        if 'violated' in outcomes:
            region_failed = True
            region_results.append(
                HybridRegionResult(
                    domain_kind=region,
                    band_hz=band,
                    outcome='discrepant',
                    evidence_refs=tuple(refs),
                )
            )
        elif all(outcome == 'satisfied' for outcome in outcomes):
            region_results.append(
                HybridRegionResult(
                    domain_kind=region,
                    band_hz=band,
                    outcome='validated',
                    evidence_refs=tuple(refs),
                )
            )
        else:
            missing.append(f'region_evaluation:{region}')
            region_results.append(
                HybridRegionResult(
                    domain_kind=region,
                    band_hz=band,
                    outcome='insufficient_evidence',
                    evidence_refs=tuple(refs),
                )
            )
    for row in _rows(rows, 'region_evaluation'):
        if row.domain_kind == 'gap':
            gap_domains.append(
                HybridStitchGap(
                    lower_hz=row.band_hz[0],
                    upper_hz=row.band_hz[1],
                    reason=row.gap_reason or 'unvalidated overlap',
                )
            )
            region_results.append(
                HybridRegionResult(
                    domain_kind='gap',
                    band_hz=row.band_hz,
                    outcome='gap_preserved',
                    evidence_refs=(row.evidence_id,),
                )
            )

    if region_failed:
        return _verdict(
            spec=spec,
            document_id=document_id,
            verdict='region_evaluation_failed',
            level='none',
            component_refs=component_refs,
            region_results=region_results,
            gap_domains=gap_domains,
            missing=missing,
            rationale='a region evaluation returned violated evidence — '
            'the composition disagrees with its components',
        )

    # --- 5. Crossover selection -------------------------------------------
    crossover_state: CrossoverState = 'not_applicable_no_blend'
    if has_blend:
        selection_rows = _rows(rows, 'crossover_selection_evidence')
        rule = spec.crossover_rule
        selection_ok = False
        saw_violated = False
        for row in selection_rows:
            if (
                row.rule_id != rule.rule_id
                or row.rule_version != rule.rule_version
            ):
                crossover_state = 'rule_mismatch'
                continue
            if row.outcome == 'violated':
                saw_violated = True
            elif row.outcome == 'satisfied':
                required = set(rule.required_agreement_evidence)
                if spec.composition_semantics == 'coherent_complex':
                    required.add('phase_time_compatibility')
                if required.issubset(set(row.agreement_kinds)):
                    selection_ok = True
                    crossover_state = 'selected_bounded'
        if not selection_ok:
            if saw_violated:
                crossover_state = 'unsupported'
            elif crossover_state != 'rule_mismatch':
                crossover_state = 'inconclusive'
            if crossover_state == 'unsupported':
                return _verdict(
                    spec=spec,
                    document_id=document_id,
                    verdict='crossover_selection_unsupported',
                    level='none',
                    component_refs=component_refs,
                    region_results=region_results,
                    gap_domains=gap_domains,
                    crossover_state=crossover_state,
                    missing=missing,
                    rationale='the versioned crossover-selection rule '
                    'found no admissible agreement band',
                )
            missing.append('crossover_selection_evidence')
    else:
        if not any(
            row.domain_kind == 'gap' and row.outcome == 'satisfied'
            for row in _rows(rows, 'region_evaluation')
        ):
            missing.append('gap_accounting')

    # --- 6. Disjoint ownership audit --------------------------------------
    policy = {
        (entry.region, entry.contribution): entry.owner
        for entry in spec.ownership_policy
    }
    ownership_state: OwnershipState = 'disjoint_ownership_declared'
    present: set[tuple[str, str]] = set()
    for row in _rows(rows, 'region_evaluation'):
        if row.domain_kind in ('wave_only', 'crossover_blend', 'ga_only'):
            for contribution in row.contributions_present:
                present.add((row.domain_kind, contribution))
    conflict = False
    for region, contribution in sorted(present):
        owner = policy.get((region, contribution))
        if owner is None or owner == 'unassigned':
            missing.append(
                f'ownership_unassigned:{region}:{contribution}')
            ownership_state = 'incomplete_ownership'
        elif owner == 'not_present':
            conflict = True
            missing.append(
                f'ownership_conflict:{region}:{contribution}')
    if conflict:
        ownership_state = 'ownership_conflict'
    if ownership_state != 'disjoint_ownership_declared':
        return _verdict(
            spec=spec,
            document_id=document_id,
            verdict='double_count_unchecked',
            level='none',
            component_refs=component_refs,
            region_results=region_results,
            gap_domains=gap_domains,
            crossover_state=crossover_state,
            ownership_state=ownership_state,
            missing=missing,
            rationale='a contribution produced energy without a declared '
            'disjoint owner — double counting is not excluded',
        )

    # --- 7. Phase/time alignment for coherent composition -----------------
    phase_time_state: PhaseTimeState = 'not_required_energetic'
    if spec.composition_semantics == 'coherent_complex':
        phase_rows = _rows(rows, 'phase_time_alignment')
        if any(
            row.outcome == 'satisfied'
            and all(
                check.outcome == 'satisfied'
                for check in row.phase_checks
            )
            for row in phase_rows
        ):
            phase_time_state = 'validated'
        elif any(
            check.outcome == 'violated'
            for row in phase_rows
            for check in row.phase_checks
        ):
            phase_time_state = 'violated'
        else:
            phase_time_state = 'checks_incomplete'
        if phase_time_state != 'validated':
            missing.append('phase_time_alignment')
            return _verdict(
                spec=spec,
                document_id=document_id,
                verdict='coherent_claim_unvalidated',
                level='none',
                component_refs=component_refs,
                region_results=region_results,
                gap_domains=gap_domains,
                crossover_state=crossover_state,
                phase_time_state=phase_time_state,
                missing=missing,
                rationale='coherent (complex) composition was claimed '
                'without validated phase/time alignment',
            )

    # --- 8. Late-field decomposition --------------------------------------
    late_field_state: LateFieldState = 'not_composed'
    if spec.late_field_composed:
        late_rows = _rows(rows, 'late_field_decomposition')
        if any(
            row.outcome == 'satisfied'
            and row.early_field_state == 'early_field_separately_verified'
            for row in late_rows
        ):
            late_field_state = 'bounded_separate'
        elif any(row.outcome == 'satisfied' for row in late_rows):
            late_field_state = 'bounded_masking_early_unverified'
        else:
            late_field_state = 'unbounded'
        if late_field_state != 'bounded_separate':
            missing.append('late_field_decomposition')
            return _verdict(
                spec=spec,
                document_id=document_id,
                verdict='early_field_unverified',
                level='none',
                component_refs=component_refs,
                region_results=region_results,
                gap_domains=gap_domains,
                crossover_state=crossover_state,
                phase_time_state=phase_time_state,
                late_field_state=late_field_state,
                missing=missing,
                rationale='late-field validation cannot hide incorrect '
                'early-reflection behavior',
            )

    # --- 9. Sensitivity sweep ----------------------------------------------
    sensitivity_state: SensitivityState = 'not_applicable'
    if spec.sensitivity_requirement is not None:
        requirement = spec.sensitivity_requirement
        sensitivity_rows = _rows(rows, 'crossover_sensitivity')
        state: SensitivityState = 'unevaluated'
        for row in sensitivity_rows:
            if row.outcome == 'violated' or any(
                point.material_change for point in row.sensitivity_points
            ):
                state = 'materially_sensitive'
                break
            if row.outcome != 'satisfied':
                state = 'incomplete_coverage'
                continue
            lo, hi = row.swept_range_hz
            req_lo, req_hi = requirement.admissible_range_hz
            evaluated = {
                point.observable for point in row.sensitivity_points
            }
            if (
                lo <= req_lo
                and hi >= req_hi
                and set(requirement.observables).issubset(evaluated)
            ):
                state = 'within_envelope'
                break
            state = 'incomplete_coverage'
        sensitivity_state = state
        if sensitivity_state == 'materially_sensitive':
            return _verdict(
                spec=spec,
                document_id=document_id,
                verdict='crossover_sensitive',
                level='none',
                component_refs=component_refs,
                region_results=region_results,
                gap_domains=gap_domains,
                crossover_state=crossover_state,
                phase_time_state=phase_time_state,
                late_field_state=late_field_state,
                sensitivity_state=sensitivity_state,
                missing=missing,
                rationale='predictions change materially when the '
                'crossover varies inside the admissible range',
            )
        if sensitivity_state != 'within_envelope':
            missing.append('crossover_sensitivity')

    # --- 10. Grid reconciliation error -------------------------------------
    grid_error_state: GridErrorState = 'not_required'
    if spec.grid_reconciliation_required:
        grid_rows = _rows(rows, 'grid_reconciliation_error')
        grid_error_state = 'unreconciled'
        for row in grid_rows:
            if row.extrapolation_performed:
                grid_error_state = 'extrapolation_used'
                break
            if row.outcome == 'satisfied' and (
                row.reconciliation_method == 'exact_bin_identity_v1'
                or row.interpolation_error_bound is not None
            ):
                grid_error_state = 'bounded'
                break
            if row.outcome == 'violated':
                grid_error_state = 'unreconciled'
        if grid_error_state != 'bounded':
            missing.append('grid_reconciliation_error')

    # --- 11. External benchmark ---------------------------------------------
    benchmark_rows = _satisfied(rows, 'external_benchmark')
    evidence_classes = tuple(sorted(
        {row.reference_class for row in benchmark_rows}
    ))
    external_refs = [
        row.case_ref
        for row in benchmark_rows
        if row.reference_class in _EXTERNAL_QUALIFYING_CLASSES
    ]
    external_ok = bool(external_refs)

    if missing:
        return _verdict(
            spec=spec,
            document_id=document_id,
            verdict='insufficient_evidence',
            level='none',
            component_refs=component_refs,
            region_results=region_results,
            gap_domains=gap_domains,
            crossover_state=crossover_state,
            phase_time_state=phase_time_state,
            grid_error_state=grid_error_state,
            sensitivity_state=sensitivity_state,
            late_field_state=late_field_state,
            evidence_classes=evidence_classes,
            external_benchmark_refs=external_refs,
            missing=missing,
            rationale='open requirements remain — the composition cannot '
            'read as validated',
        )

    if external_ok:
        return _verdict(
            spec=spec,
            document_id=document_id,
            verdict='validated_bounded',
            level='external_benchmark_validated',
            component_refs=component_refs,
            region_results=region_results,
            gap_domains=gap_domains,
            crossover_state=crossover_state,
            phase_time_state=phase_time_state,
            grid_error_state=grid_error_state,
            sensitivity_state=sensitivity_state,
            late_field_state=late_field_state,
            evidence_classes=evidence_classes,
            external_benchmark_refs=external_refs,
            rationale='all requirements satisfied against measured or '
            'independent numerical truth — valid only inside the '
            'declared applicability envelope',
        )
    return _verdict(
        spec=spec,
        document_id=document_id,
        verdict='numerically_verified',
        level='numerically_verified',
        component_refs=component_refs,
        region_results=region_results,
        gap_domains=gap_domains,
        crossover_state=crossover_state,
        phase_time_state=phase_time_state,
        grid_error_state=grid_error_state,
        sensitivity_state=sensitivity_state,
        late_field_state=late_field_state,
        evidence_classes=evidence_classes,
        rationale='self-consistency and numerical checks pass; no '
        'measured or independent-solver external benchmark — the '
        'composition is not production validated',
    )


def _verdict(
    *,
    spec: HybridCompositionValidationSpec,
    document_id: str,
    verdict: HybridValidationVerdictKind,
    level: HybridQualificationLevel,
    rationale: str,
    component_refs: Sequence[AuthorityRef] = (),
    region_results: Sequence[HybridRegionResult] = (),
    gap_domains: Sequence[HybridStitchGap] = (),
    crossover_state: CrossoverState = 'not_applicable_no_blend',
    ownership_state: OwnershipState = 'disjoint_ownership_declared',
    phase_time_state: PhaseTimeState = 'not_required_energetic',
    grid_error_state: GridErrorState = 'not_required',
    sensitivity_state: SensitivityState = 'not_applicable',
    late_field_state: LateFieldState = 'not_composed',
    evidence_classes: Sequence[HybridReferenceEvidenceClass] = (),
    external_benchmark_refs: Sequence[AuthorityRef] = (),
    missing: Sequence[str] = (),
) -> HybridValidationVerdict:
    coherent_allowed = (
        spec.composition_semantics == 'coherent_complex'
        and phase_time_state == 'validated'
        and verdict in ('validated_bounded', 'numerically_verified')
    )
    return HybridValidationVerdict.create(
        document_id=document_id,
        spec_ref=AuthorityRef(
            kind='hybrid_composition_validation_spec',
            ref_id=spec.spec_id,
            ref_sha256=spec.spec_sha256,
        ),
        subject_ref=spec.subject_ref,
        verdict=verdict,
        qualification_level=level,
        composition_semantics=spec.composition_semantics,
        coherent_claim_allowed=coherent_allowed,
        region_results=tuple(region_results),
        gap_domains=tuple(gap_domains),
        crossover_state=crossover_state,
        ownership_state=ownership_state,
        phase_time_state=phase_time_state,
        grid_error_state=grid_error_state,
        sensitivity_state=sensitivity_state,
        late_field_state=late_field_state,
        evidence_classes=tuple(evidence_classes),
        external_benchmark_refs=tuple(external_benchmark_refs),
        component_refs=tuple(component_refs),
        missing_requirements=tuple(missing),
        applicability=spec.applicability,
        rationale=rationale,
    )


__all__ = [
    'ApplicabilityEnvelope',
    'ComponentQualificationLevel',
    'CompositionSemantics',
    'ContributionOwner',
    'ContributionOwnerEntry',
    'CrossoverAgreementKind',
    'CrossoverRuleSpec',
    'CrossoverState',
    'EarlyFieldState',
    'EvidenceOutcome',
    'GridErrorState',
    'HybridCompositionValidationIntegrityError',
    'HybridCompositionValidationSpec',
    'HybridContributionKind',
    'HybridEvidenceKind',
    'HybridMetricValue',
    'HybridQualificationLevel',
    'HybridReferenceEvidenceClass',
    'HybridRegionResult',
    'HybridSolverPath',
    'HybridValidationDomainKind',
    'HybridValidationEvidence',
    'HybridValidationVerdict',
    'HybridValidationVerdictKind',
    'HYBRID_VALIDATION_EVALUATOR_VERSION',
    'HYBRID_VALIDATION_EVIDENCE_VERSION',
    'HYBRID_VALIDATION_LABELS',
    'HYBRID_VALIDATION_SPEC_VERSION',
    'HYBRID_VALIDATION_VERDICT_VERSION',
    'LateContribution',
    'LateContributionKind',
    'LateFieldState',
    'LevelRank',
    'OwnershipState',
    'PhaseTimeCheck',
    'PhaseTimeCheckKind',
    'PhaseTimeState',
    'RegionOutcome',
    'SensitivityObservable',
    'SensitivityPoint',
    'SensitivityRequirement',
    'SensitivityState',
    'evaluate_hybrid_validation',
]
