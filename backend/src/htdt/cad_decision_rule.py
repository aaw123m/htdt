"""Evidence-aware decision rule + recommendation verdict authority (#577).

HTDT already quantifies uncertainty in component authorities — measurement
uncertainty (#572), state drift (#573), validation/model error (#566),
input/installation variation (O90/#4), spatial observation bounds (#1023),
and the #979 uncertainty budget. What was missing is the **decision rule**:
the explicit, versioned contract that decides whether a difference between
candidates is strong enough to support a recommendation *after* the
declared uncertainty is accounted for.

Metrological basis (research verified 2026-10-05):

- ILAC G8:09/2019 — a statement of conformity documents its decision rule;
  a guard band ``w = TL - AL`` reduces the acceptance limit below the
  specification limit; the false-accept/false-reject risk policy belongs to
  the rule, never hidden inside business logic.
- ILAC G17:01/2021 — uncertainty in testing under ISO/IEC 17025.
- JCGM 106 — the rule describing how uncertainty affects a pass/fail or
  conformity decision must be explicit rather than implicit.
- Fieldsend & Everson-style probabilistic dominance — under noise,
  dominance is a probability/degree statement, not a point-estimate fact.
- Abstention: a decision procedure that cannot support a conclusion may
  answer *indeterminate* and name the evidence that would resolve it.

Authority boundary:

- this module decides *verdicts*; it does not rank candidates, run the
  optimizer, or re-estimate the component uncertainties — those stay with
  their owning authorities (#564/#566/#572/#573/O90/#979);
- common-mode (paired/shared) uncertainty is never added into a pairwise
  difference; independent components combine only under a declared method;
- a numerically smaller objective is not a stronger recommendation;
- every verdict pins the exact sealed rule that produced it, so a later
  policy change never rewrites a historical recommendation;
- an indeterminate verdict may carry a value-of-information request
  (compose with #519 intervention planning) instead of forcing a winner.
"""

from __future__ import annotations

from math import isfinite, sqrt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import (
    canonical_sha256 as _hash,
    canonicalize_payload,
)


DECISION_RULE_SCHEMA_VERSION = 1
DECISION_RULE_AUTHORITY_VERSION = 'decision-rule-1'
DECISION_VERDICT_AUTHORITY_VERSION = 'decision-verdict-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


DecisionType = Literal[
    'pairwise_candidate_preference',
    'pareto_dominance',
    'hard_constraint_conformity',
    'target_tolerance_conformity',
    'before_after_improvement',
    'production_recommendation',
    'evidence_gate_unlock',
]
"""The kind of decision being made. A rule suitable for a hard limit is not
automatically suitable for a perceptual preference claim."""

DecisionVerdictKind = Literal[
    'clearly_superior_within_declared_evidence',
    'superior_with_limitations',
    'evidentially_indeterminate',
    'practically_equivalent_within_tolerance',
    'conflicting_objectives',
    'insufficient_evidence',
    'incomparable',
    'constraint_pass_with_guard_band',
    'constraint_not_yet_conforming',
    'constraint_fail',
]
"""Verdict taxonomy. Not every comparison produces a winner."""

DominanceVerdictKind = Literal[
    'nominally_dominates',
    'robustly_dominates_within_tested_domain',
    'dominance_uncertain',
    'tradeoff',
    'dominance_incomparable',
    'dominance_insufficient_evidence',
]
"""Uncertainty-aware Pareto dominance states (issue §8). Nominal dominance
alone never removes a candidate from consideration."""


VERDICT_LABELS: dict[str, str] = {
    'clearly_superior_within_declared_evidence': '宣言された証拠の範囲で明確に優位',
    'superior_with_limitations': '優位（制約付き）',
    'evidentially_indeterminate': '証拠上は区別不能',
    'practically_equivalent_within_tolerance': '実用上同等（許容差内）',
    'conflicting_objectives': '指標間で相殺',
    'insufficient_evidence': '証拠不足',
    'incomparable': '比較不能',
    'constraint_pass_with_guard_band': '適合（ガードバンド込み）',
    'constraint_not_yet_conforming': '未適合（ガードバンド内）',
    'constraint_fail': '不適合',
}

DOMINANCE_VERDICT_LABELS: dict[str, str] = {
    'nominally_dominates': '名目支配のみ',
    'robustly_dominates_within_tested_domain': '検証済み範囲内で堅牢に支配',
    'dominance_uncertain': '支配関係は不確か',
    'tradeoff': 'トレードオフ',
    'dominance_incomparable': '支配比較不能',
    'dominance_insufficient_evidence': '証拠不足（支配判定）',
}

DECISION_TYPE_LABELS: dict[str, str] = {
    'pairwise_candidate_preference': '候補ペア優劣判定',
    'pareto_dominance': 'Pareto支配判定',
    'hard_constraint_conformity': 'ハード制約適合判定',
    'target_tolerance_conformity': '目標許容差適合判定',
    'before_after_improvement': '改前改後の改善判定',
    'production_recommendation': '本番推薦判定',
    'evidence_gate_unlock': '証拠ゲート解除判定',
}


UncertaintySourceClass = Literal[
    'measurement_uncertainty',
    'measurement_state_drift',
    'model_prediction_error',
    'numerical_solver_uncertainty',
    'input_installation_variation',
    'material_source_input',
    'sampling_search_uncertainty',
    'subjective_preference',
    'registration_uncertainty',
    'declared_resolution',
    'other',
]
"""Uncertainty classes stay distinct before the decision (issue §1); the
owning authorities remain canonical for each component."""

UncertaintyScope = Literal[
    'independent',
    'paired_common_mode',
    'declared_correlated',
]
"""``paired_common_mode`` components are shared by both sides of a paired
comparison and cancel in the difference — they are reported but never
added to the pairwise resolution. ``declared_correlated`` components combine
linearly inside their correlation group before quadrature across groups."""

ManifestInclusion = Literal['included', 'excluded']

CombinationMethod = Literal[
    'independent_rss',
    'bounded_linear_sum',
    'declared_resolution',
]
"""How included components combine. ``independent_rss`` is justified only
when the manifest declares independence/distribution semantics;
``bounded_linear_sum`` is a worst-case linear sum of half-widths;
``declared_resolution`` carries a caller-supplied comparison resolution
(used when the honest resolution is computed upstream, e.g. paired
residual statistics)."""


class UncertaintySourceRef(BaseModel):
    """One entry of the composition manifest (issue §10 anti-double-counting).

    ``overlaps`` names other source ids whose coverage this entry already
    includes — overlapped entries must be excluded from combination so a
    shared physical effect is never counted twice.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    source_id: str = Field(min_length=1)
    source_class: UncertaintySourceClass
    inclusion: ManifestInclusion = 'included'
    representation: Literal[
        'standard_uncertainty',
        'expanded_uncertainty',
        'bounded_interval',
        'sampled_envelope',
        'declared',
        'unknown',
    ] = 'unknown'
    value: float | None = Field(default=None, ge=0.0)
    coverage_probability: float | None = None
    unit: str | None = None
    scope: UncertaintyScope = 'independent'
    correlation_group_id: str | None = Field(default=None, min_length=1)
    authority_ref: str | None = None
    overlaps: tuple[str, ...] = ()
    reason: str | None = None

    @model_validator(mode='after')
    def valid_source(self) -> 'UncertaintySourceRef':
        if self.inclusion == 'excluded':
            if not self.reason:
                raise ValueError('an excluded uncertainty source requires a reason')
            return self
        if self.representation == 'unknown':
            raise ValueError(
                'an included uncertainty source requires an explicit representation'
            )
        if self.value is None or not isfinite(float(self.value)):
            raise ValueError(
                'an included uncertainty source requires a finite value'
            )
        if self.scope == 'declared_correlated' and not self.correlation_group_id:
            raise ValueError(
                'declared_correlated uncertainty requires a correlation_group_id'
            )
        if self.coverage_probability is not None and not (
            0.0 < float(self.coverage_probability) <= 1.0
        ):
            raise ValueError('coverage_probability must lie in (0, 1]')
        return self


class UncertaintyCompositionManifest(BaseModel):
    """Which uncertainty contributors the decision includes, and why.

    The manifest is the anti-double-counting contract: each entry records
    whether it enters the comparison resolution, at what scope, and which
    other sources it overlaps. A measured solver envelope that already
    folds in material uncertainty must be declared so the material entry
    can be excluded rather than added again.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    sources: tuple[UncertaintySourceRef, ...] = ()
    combination_method: CombinationMethod
    combination_justification: str = Field(min_length=1)
    declared_resolution: float | None = Field(default=None, gt=0.0)

    @model_validator(mode='after')
    def valid_manifest(self) -> 'UncertaintyCompositionManifest':
        ids = [source.source_id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError('uncertainty source ids must be unique')
        known = set(ids)
        included = [source for source in self.sources if source.inclusion == 'included']
        for source in self.sources:
            unknown = set(source.overlaps) - known
            if unknown:
                raise ValueError(
                    f'uncertainty source {source.source_id!r} overlaps unknown '
                    f'sources: {sorted(unknown)}'
                )
            if source.inclusion == 'included' and source.overlaps:
                overlapping = set(source.overlaps)
                still_included = {
                    item.source_id for item in included
                } & overlapping
                if still_included:
                    raise ValueError(
                        f'uncertainty source {source.source_id!r} already covers '
                        f'{sorted(still_included)} — exclude the overlapped '
                        'entries instead of double counting them'
                    )
        if self.combination_method == 'declared_resolution':
            if self.declared_resolution is None:
                raise ValueError(
                    'declared_resolution method requires the declared resolution value'
                )
            if included:
                raise ValueError(
                    'declared_resolution replaces numeric composition — list '
                    'components as excluded with their reason instead'
                )
        else:
            if self.declared_resolution is not None:
                raise ValueError(
                    'declared_resolution value is only valid for the '
                    'declared_resolution method'
                )
            # an empty/all-excluded source set is honest "no declared
            # evidence" — pairwise_resolution() then returns None and the
            # verdict degrades to insufficient_evidence rather than
            # fabricating a zero uncertainty.
            if self.combination_method == 'independent_rss':
                bad = [
                    source.source_id
                    for source in included
                    if source.representation
                    not in ('standard_uncertainty', 'expanded_uncertainty')
                ]
                if bad:
                    raise ValueError(
                        'independent_rss requires standard/expanded uncertainties '
                        f'— got interval/envelope sources: {bad}'
                    )
            if self.combination_method == 'bounded_linear_sum':
                bad = [
                    source.source_id
                    for source in included
                    if source.representation != 'bounded_interval'
                ]
                if bad:
                    raise ValueError(
                        'bounded_linear_sum requires bounded_interval sources '
                        f'— got: {bad}'
                    )
        return self

    def pairwise_resolution(self) -> float | None:
        """Combined resolution of the *difference* between two candidates.

        Common-mode entries cancel and never enter; correlated entries sum
        linearly inside their declared group; independent entries combine by
        the declared method. Returns None when nothing usable is declared —
        an honest absence, never a fabricated zero.
        """
        if self.combination_method == 'declared_resolution':
            return float(self.declared_resolution)  # type: ignore[arg-type]
        included = [
            source
            for source in self.sources
            if source.inclusion == 'included'
        ]
        effective = [
            source for source in included if source.scope != 'paired_common_mode'
        ]
        if not effective:
            return None
        if self.combination_method == 'bounded_linear_sum':
            return sum(float(source.value) for source in effective)  # type: ignore[arg-type]
        groups: dict[str, list[float]] = {}
        for source in effective:
            if source.scope == 'declared_correlated':
                key = source.correlation_group_id or ''
            else:
                key = f'__independent__:{source.source_id}'
            groups.setdefault(key, []).append(float(source.value))  # type: ignore[arg-type]
        return sqrt(sum((sum(values)) ** 2 for values in groups.values()))


class PracticalEquivalence(BaseModel):
    """Minimum-relevant-difference threshold (issue §7).

    The threshold is project/evidence specific — never a universal JND the
    code invented. ``basis`` records why this magnitude is meaningful.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    threshold: float = Field(gt=0.0)
    basis: Literal[
        'project_tolerance',
        'installer_defined',
        'evidence_derived',
        'listening_test_evidence',
        'declared',
    ]
    basis_detail: str | None = None
    evidence_ref: str | None = None


class GuardBand(BaseModel):
    """ILAC G8 guard band for a hard/tolerance limit (issue §5).

    ``acceptance_limit`` is stricter than ``specification_limit`` by the
    guard band ``w``. The band, its basis and the risk policy are persisted
    — never a hidden safety factor.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    specification_limit: float
    direction: Literal['upper', 'lower']
    acceptance_limit: float
    uncertainty_basis: str = Field(min_length=1)
    risk_policy: Literal[
        'false_accept_guarded', 'false_reject_guarded', 'balanced', 'declared'
    ]

    @model_validator(mode='after')
    def valid_band(self) -> 'GuardBand':
        values = (self.specification_limit, self.acceptance_limit)
        if any(not isfinite(float(value)) for value in values):
            raise ValueError('guard band limits must be finite')
        if self.direction == 'upper' and not (
            self.acceptance_limit <= self.specification_limit
        ):
            raise ValueError(
                'an upper-limit guard band requires acceptance <= specification'
            )
        if self.direction == 'lower' and not (
            self.acceptance_limit >= self.specification_limit
        ):
            raise ValueError(
                'a lower-limit guard band requires acceptance >= specification'
            )
        return self

    @property
    def width(self) -> float:
        return abs(self.specification_limit - self.acceptance_limit)


class RiskPolicy(BaseModel):
    """Recommendation risk policy (issue §6).

    The policy label is documentation; the parameters carry the operative
    semantics. ``resolution_factor`` multiplies the composed resolution —
    a conservative production rule may require separation strictly above
    1x combined resolution, and ``production`` rules must prefer
    indeterminate over unsupported precision.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    policy_id: Literal[
        'conservative_production',
        'balanced_design_exploration',
        'experimental_research',
        'user_explicit_override',
        'custom',
    ]
    resolution_factor: float = Field(default=1.0, gt=0.0)
    dominance_reversal_tolerance: float = Field(default=0.0, ge=0.0, lt=1.0)
    allow_nominal_rank_display: bool = False
    request_evidence_on_indeterminate: bool = True
    parameters_note: str | None = None
    override_note: str | None = None

    @model_validator(mode='after')
    def valid_policy(self) -> 'RiskPolicy':
        if self.policy_id == 'user_explicit_override' and not self.override_note:
            raise ValueError(
                'a user explicit override policy requires an override_note — '
                'overrides never rewrite the underlying evidence verdict'
            )
        if not isfinite(float(self.resolution_factor)):
            raise ValueError('resolution_factor must be finite')
        return self


CriterionDirection = Literal[
    'minimize',
    'maximize',
    'upper_limit',
    'lower_limit',
    'target',
]


class DecisionRuleSpec(BaseModel):
    """Sealed, versioned decision rule (issue §11).

    A rule names the exact decision type, criterion, uncertainty manifest,
    guard band, practical-equivalence threshold and risk policy. Historical
    verdicts pin its semantic hash, so revising a rule creates a new
    identity rather than rewriting prior recommendations.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = DECISION_RULE_SCHEMA_VERSION
    authority_version: Literal['decision-rule-1'] = DECISION_RULE_AUTHORITY_VERSION
    rule_id: str = Field(pattern=r'^decision-rule:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    rule_version_label: str = Field(min_length=1)
    decision_type: DecisionType
    criterion_id: str = Field(min_length=1)
    criterion_unit: str | None = None
    criterion_direction: CriterionDirection
    target_value: float | None = None
    guard_band: GuardBand | None = None
    practical_equivalence: PracticalEquivalence | None = None
    uncertainty_manifest: UncertaintyCompositionManifest
    risk_policy: RiskPolicy
    declared_limitations: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_rule(self) -> 'DecisionRuleSpec':
        if self.criterion_direction == 'target':
            if self.target_value is None or not isfinite(float(self.target_value)):
                raise ValueError('a target criterion requires a finite target_value')
        elif self.target_value is not None:
            raise ValueError('target_value is only valid for a target criterion')
        conformity = {
            'hard_constraint_conformity',
            'target_tolerance_conformity',
            'evidence_gate_unlock',
        }
        if self.decision_type in conformity:
            if self.criterion_direction not in ('upper_limit', 'lower_limit', 'target'):
                raise ValueError(
                    'conformity decisions require a limit or target direction'
                )
            if self.guard_band is None:
                raise ValueError(
                    'conformity decisions require an explicit guard band — '
                    'use acceptance == specification for declared simple acceptance'
                )
            if self.criterion_direction == 'upper_limit' and (
                self.guard_band.direction != 'upper'
            ):
                raise ValueError('guard band direction must match the criterion')
            if self.criterion_direction == 'lower_limit' and (
                self.guard_band.direction != 'lower'
            ):
                raise ValueError('guard band direction must match the criterion')
        elif self.guard_band is not None:
            raise ValueError('guard bands only apply to conformity decisions')
        if self.decision_type in (
            'pairwise_candidate_preference',
            'before_after_improvement',
            'production_recommendation',
        ) and self.criterion_direction not in ('minimize', 'maximize'):
            raise ValueError(
                'preference decisions require a minimize/maximize criterion'
            )
        expected = _hash(self.identity_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('decision rule semantic hash mismatch')
        if self.rule_id != f'decision-rule:{expected}':
            raise ValueError('decision rule id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'rule_id', 'semantic_sha256'},
        )

    def comparison_resolution(self) -> float | None:
        return self.uncertainty_manifest.pairwise_resolution()


class DecisionSubject(BaseModel):
    """One side of a decision — an exact evaluated candidate."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    candidate_id: str = Field(min_length=1)
    evaluation_id: str | None = Field(default=None, min_length=1)
    evaluation_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )


class EvidenceRequest(BaseModel):
    """Value-of-information outcome (issue §9): what evidence would resolve
    an indeterminate decision."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    request_kind: Literal[
        'paired_remeasurement',
        'repeat_captures',
        'source_directivity_measurement',
        'higher_fidelity_solve',
        'reduce_position_uncertainty',
        'verify_deployed_state',
        'adjacent_room_measurement',
        'domain_coverage_extension',
        'other',
    ]
    target: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class DominanceAxisEvidence(BaseModel):
    """Per-objective evidence feeding a Pareto-dominance decision."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    objective_id: str = Field(min_length=1)
    direction: Literal['minimize', 'maximize']
    nominal_a: float
    nominal_b: float
    resolution: float | None = Field(default=None, gt=0.0)
    interval_a: tuple[float, float] | None = None
    interval_b: tuple[float, float] | None = None

    @model_validator(mode='after')
    def valid_axis(self) -> 'DominanceAxisEvidence':
        for value in (self.nominal_a, self.nominal_b, self.resolution):
            if value is not None and not isfinite(float(value)):
                raise ValueError('dominance axis values must be finite')
        for interval in (self.interval_a, self.interval_b):
            if interval is not None:
                low, high = float(interval[0]), float(interval[1])
                if not (isfinite(low) and isfinite(high)) or high < low:
                    raise ValueError('dominance intervals must be finite [low, high]')
        return self


class RecommendationEvidenceVerdict(BaseModel):
    """Sealed decision verdict pinned to the exact rule that produced it."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = DECISION_RULE_SCHEMA_VERSION
    authority_version: Literal['decision-verdict-1'] = (
        DECISION_VERDICT_AUTHORITY_VERSION
    )
    verdict_id: str = Field(pattern=r'^decision-verdict:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    rule_id: str = Field(pattern=r'^decision-rule:[0-9a-f]{64}$')
    rule_sha256: str = Field(pattern=_SHA256_PATTERN)
    decision_type: DecisionType
    document_id: str = Field(min_length=1)
    scene_revision_id: str | None = None
    search_spec_id: str | None = None

    subjects: tuple[DecisionSubject, ...] = Field(min_length=1)
    nominal_values: dict[str, float] = {}
    nominal_difference: float | None = None
    resolution: float | None = None
    practical_threshold: float | None = None
    point_estimate: float | None = None
    specification_limit: float | None = None
    acceptance_limit: float | None = None

    verdict: DecisionVerdictKind
    dominance_verdict: DominanceVerdictKind | None = None
    leading_candidate_id: str | None = None
    reason_codes: tuple[str, ...] = ()
    evidence_requests: tuple[EvidenceRequest, ...] = ()
    limitations: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_verdict(self) -> 'RecommendationEvidenceVerdict':
        # integrity before semantics: any tampered field must surface as a
        # hash mismatch rather than a coincidental semantic complaint.
        expected = _hash(self.identity_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('decision verdict semantic hash mismatch')
        if self.verdict_id != f'decision-verdict:{expected}':
            raise ValueError('decision verdict id mismatch')
        for value in (
            self.nominal_difference,
            self.resolution,
            self.practical_threshold,
            self.point_estimate,
            self.specification_limit,
            self.acceptance_limit,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('verdict numeric fields must be finite')
        subject_ids = [subject.candidate_id for subject in self.subjects]
        if len(subject_ids) != len(set(subject_ids)):
            raise ValueError('verdict subjects must be unique')
        if self.leading_candidate_id is not None and (
            self.leading_candidate_id not in subject_ids
        ):
            raise ValueError('verdict leader must be a declared subject')
        if self.verdict in (
            'clearly_superior_within_declared_evidence',
            'superior_with_limitations',
        ) and self.leading_candidate_id is None:
            raise ValueError('a superiority verdict must name the leading candidate')
        if self.verdict == 'evidentially_indeterminate' and (
            self.leading_candidate_id is not None
        ):
            raise ValueError(
                'an indeterminate verdict must not claim a leading candidate'
            )
        if self.dominance_verdict is not None and (
            self.decision_type != 'pareto_dominance'
        ):
            raise ValueError('dominance verdicts only apply to pareto_dominance')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'verdict_id', 'semantic_sha256'},
        )


def build_decision_rule_spec(**kwargs: Any) -> DecisionRuleSpec:
    """Assemble and seal a :class:`DecisionRuleSpec`."""
    payload = {'rule_id': 'decision-rule:' + '0' * 64, 'semantic_sha256': '0' * 64, **kwargs}
    provisional = DecisionRuleSpec.model_construct(
        **canonicalize_payload(DecisionRuleSpec, dict(payload))
    )
    digest = _hash(provisional.identity_payload())
    payload['semantic_sha256'] = digest
    payload['rule_id'] = f'decision-rule:{digest}'
    return DecisionRuleSpec(**payload)


def _seal_verdict(**payload: Any) -> RecommendationEvidenceVerdict:
    payload = dict(payload)
    payload['verdict_id'] = 'decision-verdict:' + '0' * 64
    payload['semantic_sha256'] = '0' * 64
    provisional = RecommendationEvidenceVerdict.model_construct(
        **canonicalize_payload(RecommendationEvidenceVerdict, dict(payload))
    )
    digest = _hash(provisional.identity_payload())
    payload['semantic_sha256'] = digest
    payload['verdict_id'] = f'decision-verdict:{digest}'
    return RecommendationEvidenceVerdict(**payload)


def _better_sign(direction: str) -> int:
    if direction == 'minimize':
        return -1
    if direction == 'maximize':
        return 1
    raise ValueError(f'{direction} has no better/worse ordering')


def evaluate_pairwise_preference(
    rule: DecisionRuleSpec,
    *,
    candidate_a: DecisionSubject,
    candidate_b: DecisionSubject,
    value_a: float,
    value_b: float,
    scene_revision_id: str | None = None,
    search_spec_id: str | None = None,
    evidence_requests: tuple[EvidenceRequest, ...] = (),
    limitations: tuple[str, ...] = (),
    created_at_utc: str,
) -> RecommendationEvidenceVerdict:
    """Pairwise candidate preference under the declared resolution.

    A |a-b| smaller than the composed pairwise resolution is
    ``evidentially_indeterminate`` — never "A wins by 0.2 dB". A resolved
    difference below the declared practical-equivalence threshold is
    ``practically_equivalent_within_tolerance`` — resolved but not worth
    recommending.
    """
    if rule.decision_type not in (
        'pairwise_candidate_preference',
        'before_after_improvement',
        'production_recommendation',
    ):
        raise ValueError('pairwise evaluation requires a preference decision rule')
    if not isfinite(float(value_a)) or not isfinite(float(value_b)):
        raise ValueError('pairwise values must be finite')

    resolution = rule.comparison_resolution()
    threshold = (
        None
        if rule.practical_equivalence is None
        else float(rule.practical_equivalence.threshold)
    )
    nominal_difference = value_a - value_b
    sign = _better_sign(rule.criterion_direction)
    # positive advantage means A is better under the criterion direction
    advantage = nominal_difference * sign
    leader_id = candidate_a.candidate_id if advantage > 0 else (
        candidate_b.candidate_id if advantage < 0 else None
    )
    separation = abs(advantage)

    reasons: list[str] = []
    verdict: DecisionVerdictKind
    named_leader: str | None = None

    if resolution is None:
        verdict = 'insufficient_evidence'
        reasons.append('no_declared_uncertainty_resolution')
    elif threshold is not None and separation <= threshold:
        verdict = 'practically_equivalent_within_tolerance'
        reasons.append('separation_within_practical_equivalence')
    elif separation <= resolution * rule.risk_policy.resolution_factor:
        verdict = 'evidentially_indeterminate'
        reasons.append('separation_below_evidence_resolution')
    else:
        reasons.append('separation_exceeds_composed_resolution')
        if limitations or rule.declared_limitations:
            verdict = 'superior_with_limitations'
            reasons.append('declared_limitations_apply')
        else:
            verdict = 'clearly_superior_within_declared_evidence'
        named_leader = leader_id

    if (
        verdict == 'evidentially_indeterminate'
        and rule.risk_policy.allow_nominal_rank_display
        and leader_id is not None
    ):
        # Exploratory policies may display the nominal order without turning
        # it into a recommendation — the verdict stays indeterminate.
        reasons.append('nominal_leader_display_only')

    return _seal_verdict(
        rule_id=rule.rule_id,
        rule_sha256=rule.semantic_sha256,
        decision_type=rule.decision_type,
        document_id=rule.document_id,
        scene_revision_id=scene_revision_id,
        search_spec_id=search_spec_id,
        subjects=(candidate_a, candidate_b),
        nominal_values={
            candidate_a.candidate_id: value_a,
            candidate_b.candidate_id: value_b,
        },
        nominal_difference=nominal_difference,
        resolution=resolution,
        practical_threshold=threshold,
        verdict=verdict,
        leading_candidate_id=named_leader,
        reason_codes=tuple(reasons),
        evidence_requests=(
            evidence_requests
            if verdict == 'evidentially_indeterminate'
            else ()
        ),
        limitations=tuple(limitations) + tuple(rule.declared_limitations),
        created_at_utc=created_at_utc,
    )


def evaluate_limit_conformity(
    rule: DecisionRuleSpec,
    *,
    subject: DecisionSubject,
    point_estimate: float,
    scene_revision_id: str | None = None,
    search_spec_id: str | None = None,
    limitations: tuple[str, ...] = (),
    created_at_utc: str,
) -> RecommendationEvidenceVerdict:
    """Hard-limit / target-tolerance conformity with an ILAC G8 guard band.

    The specification limit, the stricter acceptance limit and the band
    width all come from the sealed rule — the verdict states which boundary
    the point estimate crossed.
    """
    if rule.decision_type not in (
        'hard_constraint_conformity',
        'target_tolerance_conformity',
        'evidence_gate_unlock',
    ):
        raise ValueError('conformity evaluation requires a conformity decision rule')
    assert rule.guard_band is not None
    if not isfinite(float(point_estimate)):
        raise ValueError('point estimate must be finite')

    band = rule.guard_band
    resolution = rule.comparison_resolution()
    reasons: list[str] = []

    if band.direction == 'upper':
        if point_estimate > band.specification_limit:
            verdict = 'constraint_fail'
            reasons.append('point_estimate_beyond_specification_limit')
        elif point_estimate > band.acceptance_limit:
            verdict = 'constraint_not_yet_conforming'
            reasons.append('point_estimate_inside_guard_band')
        else:
            verdict = 'constraint_pass_with_guard_band'
            reasons.append('within_acceptance_limit')
    else:
        if point_estimate < band.specification_limit:
            verdict = 'constraint_fail'
            reasons.append('point_estimate_beyond_specification_limit')
        elif point_estimate < band.acceptance_limit:
            verdict = 'constraint_not_yet_conforming'
            reasons.append('point_estimate_inside_guard_band')
        else:
            verdict = 'constraint_pass_with_guard_band'
            reasons.append('within_acceptance_limit')

    return _seal_verdict(
        rule_id=rule.rule_id,
        rule_sha256=rule.semantic_sha256,
        decision_type=rule.decision_type,
        document_id=rule.document_id,
        scene_revision_id=scene_revision_id,
        search_spec_id=search_spec_id,
        subjects=(subject,),
        nominal_values={subject.candidate_id: point_estimate},
        resolution=resolution,
        point_estimate=point_estimate,
        specification_limit=band.specification_limit,
        acceptance_limit=band.acceptance_limit,
        verdict=verdict,
        reason_codes=tuple(reasons),
        limitations=tuple(limitations) + tuple(rule.declared_limitations),
        created_at_utc=created_at_utc,
    )


def _axis_relation(axis: DominanceAxisEvidence) -> Literal[
    'robustly_better', 'nominally_better', 'uncertain', 'robustly_worse',
    'nominally_worse', 'indeterminate', 'equal', 'unresolvable',
]:
    """A's relation to B on one objective under declared resolution/intervals."""
    sign = _better_sign(axis.direction)
    nominal = (axis.nominal_a - axis.nominal_b) * sign
    intervals = axis.interval_a is not None and axis.interval_b is not None

    if intervals:
        assert axis.interval_a is not None and axis.interval_b is not None
        a_lo, a_hi = float(axis.interval_a[0]), float(axis.interval_a[1])
        b_lo, b_hi = float(axis.interval_b[0]), float(axis.interval_b[1])
        # Robust superiority inside the tested domain: A's worst sampled
        # value still beats B's best sampled value.
        a_worst, a_best = (a_hi, a_lo) if sign < 0 else (a_lo, a_hi)
        b_worst, b_best = (b_hi, b_lo) if sign < 0 else (b_lo, b_hi)
        if a_worst < b_best:
            return 'robustly_better'
        if a_best > b_worst:
            return 'robustly_worse'
        if nominal > 0:
            return 'uncertain'
        if nominal < 0:
            return 'uncertain'
        return 'equal'

    if axis.resolution is None:
        if nominal == 0:
            return 'equal'
        return 'unresolvable'
    if abs(nominal) > float(axis.resolution):
        return 'nominally_better' if nominal > 0 else 'nominally_worse'
    return 'indeterminate' if nominal != 0 else 'equal'


def evaluate_pareto_dominance(
    rule: DecisionRuleSpec,
    *,
    candidate_a: DecisionSubject,
    candidate_b: DecisionSubject,
    axes: tuple[DominanceAxisEvidence, ...],
    scene_revision_id: str | None = None,
    search_spec_id: str | None = None,
    evidence_requests: tuple[EvidenceRequest, ...] = (),
    limitations: tuple[str, ...] = (),
    created_at_utc: str,
) -> RecommendationEvidenceVerdict:
    """Uncertainty-aware dominance between two candidates (issue §8).

    ``robustly_dominates_within_tested_domain`` requires every axis to stay
    better-or-equal under the declared intervals; nominal dominance with any
    unresolved or overlapping axis degrades to ``dominance_uncertain``;
    resolved conflicts are a ``tradeoff``. No hidden aggregate score.
    """
    if rule.decision_type != 'pareto_dominance':
        raise ValueError('dominance evaluation requires a pareto_dominance rule')
    if not axes:
        raise ValueError('dominance evaluation requires at least one objective axis')

    relations = [_axis_relation(axis) for axis in axes]
    verdict: DominanceVerdictKind
    reasons: list[str] = []

    if any(relation == 'unresolvable' for relation in relations):
        verdict = 'dominance_insufficient_evidence'
        reasons.append('axis_without_declared_resolution')
    elif all(relation == 'equal' for relation in relations):
        verdict = 'dominance_incomparable'
        reasons.append('no_axis_separates_candidates')
    elif all(relation in ('robustly_better', 'equal') for relation in relations) and any(
        relation == 'robustly_better' for relation in relations
    ):
        verdict = 'robustly_dominates_within_tested_domain'
        reasons.append('all_axes_robustly_better_or_equal')
    elif any(relation in ('robustly_better', 'nominally_better') for relation in relations) and any(
        relation in ('robustly_worse', 'nominally_worse') for relation in relations
    ):
        verdict = 'tradeoff'
        reasons.append('resolved_axes_conflict')
    elif any(
        relation in ('robustly_better', 'nominally_better', 'uncertain')
        for relation in relations
    ) and all(
        relation in ('robustly_better', 'nominally_better', 'uncertain', 'equal')
        for relation in relations
    ):
        if all(relation in ('nominally_better', 'equal') for relation in relations):
            verdict = 'nominally_dominates'
            reasons.append('nominal_dominance_without_interval_evidence')
        else:
            verdict = 'dominance_uncertain'
            reasons.append('interval_overlap_or_unresolved_axis')
    elif any(
        relation in ('robustly_worse', 'nominally_worse')
        for relation in relations
    ) and all(
        relation in ('robustly_worse', 'nominally_worse', 'uncertain', 'equal')
        for relation in relations
    ):
        # symmetric: B leads where evidence allows; A never wins robustly
        verdict = 'dominance_uncertain' if any(
            relation == 'uncertain' for relation in relations
        ) else 'nominally_dominates'
        reasons.append('reversed_nominal_dominance')
    else:
        verdict = 'dominance_uncertain'
        reasons.append('mixed_evidence')

    top_level: DecisionVerdictKind = {
        'robustly_dominates_within_tested_domain': (
            'clearly_superior_within_declared_evidence'
        ),
        'nominally_dominates': 'superior_with_limitations',
        'dominance_uncertain': 'evidentially_indeterminate',
        'tradeoff': 'conflicting_objectives',
        'dominance_incomparable': 'incomparable',
        'dominance_insufficient_evidence': 'insufficient_evidence',
    }[verdict]

    nominal_diff = sum(
        axis.nominal_a - axis.nominal_b for axis in axes
    ) / len(axes)
    leader = None
    if top_level in (
        'clearly_superior_within_declared_evidence',
        'superior_with_limitations',
    ):
        leader = candidate_a.candidate_id
    if verdict == 'nominally_dominates':
        # nominal-only dominance names a leader but the dominance_verdict
        # records that reversal within the declared resolution is possible
        reasons.append('nominal_dominance_only_not_robust')

    return _seal_verdict(
        rule_id=rule.rule_id,
        rule_sha256=rule.semantic_sha256,
        decision_type=rule.decision_type,
        document_id=rule.document_id,
        scene_revision_id=scene_revision_id,
        search_spec_id=search_spec_id,
        subjects=(candidate_a, candidate_b),
        nominal_difference=nominal_diff,
        resolution=rule.comparison_resolution(),
        practical_threshold=(
            None
            if rule.practical_equivalence is None
            else float(rule.practical_equivalence.threshold)
        ),
        verdict=top_level,
        dominance_verdict=verdict,
        leading_candidate_id=leader,
        reason_codes=tuple(reasons),
        evidence_requests=(
            evidence_requests
            if top_level == 'evidentially_indeterminate'
            else ()
        ),
        limitations=tuple(limitations) + tuple(rule.declared_limitations),
        created_at_utc=created_at_utc,
    )


__all__ = [
    'CombinationMethod',
    'CriterionDirection',
    'DecisionRuleSpec',
    'DecisionSubject',
    'DecisionType',
    'DecisionVerdictKind',
    'DominanceAxisEvidence',
    'DominanceVerdictKind',
    'EvidenceRequest',
    'GuardBand',
    'ManifestInclusion',
    'PracticalEquivalence',
    'RecommendationEvidenceVerdict',
    'RiskPolicy',
    'UncertaintyCompositionManifest',
    'UncertaintyScope',
    'UncertaintySourceClass',
    'UncertaintySourceRef',
    'DECISION_RULE_AUTHORITY_VERSION',
    'DECISION_RULE_SCHEMA_VERSION',
    'DECISION_VERDICT_AUTHORITY_VERSION',
    'VERDICT_LABELS',
    'DOMINANCE_VERDICT_LABELS',
    'DECISION_TYPE_LABELS',
    'build_decision_rule_spec',
    'evaluate_pairwise_preference',
    'evaluate_limit_conformity',
    'evaluate_pareto_dominance',
]
