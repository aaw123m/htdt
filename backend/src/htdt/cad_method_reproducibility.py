"""Measurement-method reproducibility / precision authority (#693,
REV58-MEASELEC).

An internally consistent repeated measurement is *repeatability*; a
method that survives different operators, instruments, mic positions and
days has *reproducibility*. They are different claims with different
variance terms — and ISO 5725 reserves "reproducibility" for the
between-conditions case. This module owns the campaign design, the
factor-attributed precision model and the fail-closed qualification that
gates method claims:

- :class:`CadMethodProcedure` — the sealed procedure being
  characterized: what is measured, the stimulus/pipeline refs, and the
  documented steps. A method with no written procedure cannot produce
  reproducibility evidence — undocumented procedure = unreproducible.
- :class:`CadReproducibilityCampaign` — the sealed study: which factors
  were deliberately varied (operator / instrument / mic position / day /
  session / environment / procedure variant / software version) and the
  runs attributed to each condition. A single changed factor at a time
  confounds; the design class records whether factors were crossed.
- :class:`CadMethodPrecisionModel` — the sealed variance decomposition:
  per named metric, within-run repeatability kept *separately* from
  between-operator / between-instrument / between-position /
  between-session variance — never pooled into one σ.
- :class:`CadReproducibilityQualification` — the fail-closed verdict:
  evidence tier (repeatability-only / intermediate / internal
  reproducibility / formal interlaboratory), and the two consequence
  gates — #566 prediction validation and #577 decision rules may only
  consume a method whose precision supports the claim.

Honesty rules baked in:

- Precision ≠ trueness: a tight spread around a biased value is still
  biased — this model never declares accuracy.
- One-operand repeats are never relabeled reproducibility.
- An internal HTDT campaign is never formal ISO 5725 conformance; the
  vocabulary keeps internal studies and interlaboratory studies apart.
- Small-n runs and single-factor-at-a-time designs fail closed to
  limited/consumed-nothing states rather than overclaiming.

Literature / standards basis
----------------------------
- ISO 5725-2/-3 terminology: repeatability conditions, intermediate
  precision conditions, reproducibility conditions; variance decomposed
  per condition factor rather than pooled.
- ISO 5725 lifecycle: internal method validation precedes any
  interlaboratory claim — an internal campaign is evidence, not
  conformance.
- GUM (JCGM 100) / ANOVA variance-component practice for attributing
  spread to named condition factors.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


REP_AUTHORITY_SCHEMA_VERSION = 'method-reproducibility-1'
REP_EVALUATION_VERSION = 'method-reproducibility-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


def _require_nonnegative(value: float, label: str) -> None:
    _require_finite(value, label)
    if value < 0:
        raise ValueError(f'{label} must be non-negative')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#693)
# ---------------------------------------------------------------------------

PrecisionFactor = Literal[
    'operator',
    'instrument_chain',
    'microphone_position',
    'day',
    'session',
    'environment_condition',
    'procedure_variant',
    'software_version',
    'calibration_drift',
    'residual_within_run',
]

CampaignDesignClass = Literal[
    'crossed_factorial',
    'single_factor_at_a_time',
    'nested',
    'repeated_only',
    'unknown',
]

EvidenceTier = Literal[
    'repeatability_only',
    'intermediate_precision',
    'internal_reproducibility',
    'interlaboratory_formal',
    'unknown',
]

RunStatus = Literal['completed', 'excluded_with_reason', 'invalid']

PrecisionMetricKind = Literal[
    'spl_band',
    'spl_broadband',
    'decay_time',
    'reflection_arrival_time',
    'reflection_level',
    'frequency_response_deviation',
    'polar_response',
    'other_named',
]

ReproducibilityState = Literal[
    'precision_model_established',
    'limited_scope_precision',
    'repeatability_only_established',
    'confounded_design',
    'insufficient_runs',
    'unqualified_insufficient_evidence',
]

ReproducibilityCapability = Literal[
    'repeatability_known',
    'between_operator_known',
    'between_instrument_known',
    'between_position_known',
    'between_session_known',
    'prediction_gate_eligible',
    'decision_gate_eligible',
]

ALL_REPRODUCIBILITY_CAPABILITIES: tuple[ReproducibilityCapability, ...] = (
    'repeatability_known',
    'between_operator_known',
    'between_instrument_known',
    'between_position_known',
    'between_session_known',
    'prediction_gate_eligible',
    'decision_gate_eligible',
)

CapabilityState = Literal['valid', 'limited', 'invalid', 'unknown']

#: Factors that must *vary* across runs for a claim beyond
#: repeatability-only. Repeatability requires none; each between-X
#: capability needs its own factor varied.
_BETWEEN_CAPABILITY_FACTORS: dict[
    ReproducibilityCapability, frozenset[PrecisionFactor]
] = {
    'between_operator_known': frozenset({'operator'}),
    'between_instrument_known': frozenset({'instrument_chain'}),
    'between_position_known': frozenset({'microphone_position'}),
    'between_session_known': frozenset({'day', 'session'}),
}


# ---------------------------------------------------------------------------
# Embedded evidence blocks
# ---------------------------------------------------------------------------


class CadConditionAssignment(BaseModel):
    """One factor's level for one run — e.g. operator=A, day=3."""

    model_config = ConfigDict(frozen=True)

    factor: PrecisionFactor
    level: str = Field(min_length=1)


class CadCampaignRun(BaseModel):
    """One measurement run inside a campaign, with its condition
    assignment and result artifact pin."""

    model_config = ConfigDict(frozen=True)

    run_label: str = Field(min_length=1)
    conditions: tuple[CadConditionAssignment, ...] = ()
    artifact_ref: AuthorityRef | None = None
    status: RunStatus = 'completed'
    exclusion_reason: str | None = None
    observed_at_utc: str | None = None

    @model_validator(mode='after')
    def valid_run(self) -> 'CadCampaignRun':
        if self.observed_at_utc is not None:
            _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        if self.artifact_ref is not None and (
            self.artifact_ref.ref_sha256 is None
        ):
            raise ValueError(
                'a run artifact pin must carry its sha256'
            )
        if self.status == 'excluded_with_reason' and not (
            self.exclusion_reason
        ):
            raise ValueError(
                'an excluded run must carry its exclusion reason — '
                'silent exclusion is cherry-picking'
            )
        if self.status == 'completed' and self.artifact_ref is None:
            raise ValueError(
                'a completed run requires its measurement artifact pin'
            )
        factors = [c.factor for c in self.conditions]
        if len(factors) != len(set(factors)):
            raise ValueError('a run assigns each factor at most once')
        return self


class CadVarianceComponent(BaseModel):
    """Variance attributed to one precision factor — reported per
    factor, never pooled into a single σ."""

    model_config = ConfigDict(frozen=True)

    factor: PrecisionFactor
    variance: float
    std_dev: float | None = None
    degrees_of_freedom: int | None = None
    n_levels: int | None = None

    @model_validator(mode='after')
    def valid_component(self) -> 'CadVarianceComponent':
        _require_nonnegative(self.variance, 'variance component')
        if self.std_dev is not None:
            _require_nonnegative(self.std_dev, 'component std_dev')
            if abs(self.std_dev ** 2 - self.variance) > (
                1e-6 * max(1.0, self.variance)
            ):
                raise ValueError(
                    'std_dev must be consistent with variance'
                )
        if self.degrees_of_freedom is not None and (
            self.degrees_of_freedom < 1
        ):
            raise ValueError('degrees_of_freedom must be >= 1')
        if self.n_levels is not None and self.n_levels < 1:
            raise ValueError('n_levels must be >= 1')
        return self


class CadMetricPrecision(BaseModel):
    """Precision decomposition for one named metric of the method —
    e.g. SPL in a band, decay time, reflection arrival."""

    model_config = ConfigDict(frozen=True)

    metric_kind: PrecisionMetricKind
    metric_label: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    repeatability_std: float | None = None
    components: tuple[CadVarianceComponent, ...] = ()
    repeatability_limit_r: float | None = None
    reproducibility_limit_R: float | None = None
    n_runs: int = 0

    @model_validator(mode='after')
    def valid_precision(self) -> 'CadMetricPrecision':
        for label, value in (
            ('repeatability_std', self.repeatability_std),
            ('repeatability_limit_r', self.repeatability_limit_r),
            ('reproducibility_limit_R', self.reproducibility_limit_R),
        ):
            if value is not None:
                _require_nonnegative(value, f'precision {label}')
        if self.n_runs < 0:
            raise ValueError('n_runs must be non-negative')
        factors = [c.factor for c in self.components]
        if len(factors) != len(set(factors)):
            raise ValueError(
                'each variance component attributes one factor once'
            )
        if self.reproducibility_limit_R is not None and (
            self.repeatability_limit_r is not None
            and self.reproducibility_limit_R < self.repeatability_limit_r
        ):
            raise ValueError(
                'reproducibility limit R must exceed repeatability '
                'limit r — between-condition spread never shrinks below '
                'within-condition spread'
            )
        return self

    def component(
        self, factor: PrecisionFactor
    ) -> CadVarianceComponent | None:
        for c in self.components:
            if c.factor == factor:
                return c
        return None


# ---------------------------------------------------------------------------
# Sealed authorities
# ---------------------------------------------------------------------------


class CadMethodProcedure(BaseModel):
    """Sealed declaration of the measurement procedure being
    characterized — steps, stimulus and pipeline refs.

    Reproducibility evidence attaches to a *named, written* procedure;
    an undocumented habit is not a method.
    """

    model_config = ConfigDict(frozen=True)

    procedure_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    method_name: str = Field(min_length=1)
    procedure_version: str = Field(min_length=1)
    steps_digest: str | None = None
    stimulus_ref: AuthorityRef | None = None
    pipeline_ref: AuthorityRef | None = None
    measchain_ref: AuthorityRef | None = None
    documented: bool
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    procedure_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_procedure(self) -> 'CadMethodProcedure':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        for label, ref in (
            ('#608 stimulus', self.stimulus_ref),
            ('pipeline', self.pipeline_ref),
            ('measchain', self.measchain_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'the {label} pin must carry its sha256')
        expected = _hash(self.identity_payload())
        if self.procedure_sha256 != expected:
            raise ValueError('procedure hash mismatch')
        if self.procedure_id != _semantic_id('repproc', expected):
            raise ValueError('procedure id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'method_name': self.method_name,
            'procedure_version': self.procedure_version,
            'steps_digest': self.steps_digest,
            'stimulus_ref': (
                self.stimulus_ref.model_dump(mode='json')
                if self.stimulus_ref is not None
                else None
            ),
            'pipeline_ref': (
                self.pipeline_ref.model_dump(mode='json')
                if self.pipeline_ref is not None
                else None
            ),
            'measchain_ref': (
                self.measchain_ref.model_dump(mode='json')
                if self.measchain_ref is not None
                else None
            ),
            'documented': self.documented,
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }


def method_procedure_binding(
    procedure: CadMethodProcedure,
) -> AuthorityRef:
    return AuthorityRef(
        kind='method_procedure',
        ref_id=procedure.procedure_id,
        ref_sha256=procedure.procedure_sha256,
    )


class CadReproducibilityCampaign(BaseModel):
    """Sealed precision study: which factors were deliberately varied
    and which runs land under which conditions.

    The design class is honest about what was crossed: a
    single-factor-at-a-time study confounds every factor it did not
    hold; repeated-only is repeatability evidence, never
    reproducibility.
    """

    model_config = ConfigDict(frozen=True)

    campaign_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    procedure_ref: AuthorityRef
    design_class: CampaignDesignClass
    varied_factors: tuple[PrecisionFactor, ...] = ()
    held_factors: tuple[PrecisionFactor, ...] = ()
    runs: tuple[CadCampaignRun, ...] = ()
    evidence_tier: EvidenceTier
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    campaign_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_campaign(self) -> 'CadReproducibilityCampaign':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.procedure_ref.ref_sha256 is None:
            raise ValueError(
                'the procedure pin must carry its sha256'
            )
        overlap = set(self.varied_factors) & set(self.held_factors)
        if overlap:
            raise ValueError(
                'a factor cannot be both varied and held: '
                + ', '.join(sorted(overlap))
            )
        completed = [r for r in self.runs if r.status == 'completed']
        if self.evidence_tier in (
            'intermediate_precision',
            'internal_reproducibility',
            'interlaboratory_formal',
        ) and not self.varied_factors:
            raise ValueError(
                'a beyond-repeatability tier requires at least one '
                'deliberately varied factor — one-condition repeats are '
                'repeatability_only'
            )
        if self.evidence_tier == 'interlaboratory_formal' and (
            self.design_class != 'crossed_factorial'
        ):
            raise ValueError(
                'interlaboratory_formal requires a crossed factorial '
                'design — an internal HTDT campaign is never formal '
                'ISO 5725 conformance'
            )
        if self.evidence_tier == 'repeatability_only' and (
            self.varied_factors
        ):
            raise ValueError(
                'a campaign that varied factors is not '
                'repeatability_only — relabel or hold the factors'
            )
        if len(completed) < 2 and self.evidence_tier != 'unknown':
            raise ValueError(
                'a claimed evidence tier requires at least two '
                'completed runs'
            )
        expected = _hash(self.identity_payload())
        if self.campaign_sha256 != expected:
            raise ValueError('campaign hash mismatch')
        if self.campaign_id != _semantic_id('repcamp', expected):
            raise ValueError('campaign id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'procedure_ref': self.procedure_ref.model_dump(mode='json'),
            'design_class': self.design_class,
            'varied_factors': list(self.varied_factors),
            'held_factors': list(self.held_factors),
            'runs': [r.model_dump(mode='json') for r in self.runs],
            'evidence_tier': self.evidence_tier,
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }

    def completed_runs(self) -> tuple[CadCampaignRun, ...]:
        return tuple(r for r in self.runs if r.status == 'completed')

    def levels_of(self, factor: PrecisionFactor) -> set[str]:
        """Distinct levels actually assigned to a factor across
        completed runs."""
        return {
            c.level
            for r in self.completed_runs()
            for c in r.conditions
            if c.factor == factor
        }


def reproducibility_campaign_binding(
    campaign: CadReproducibilityCampaign,
) -> AuthorityRef:
    return AuthorityRef(
        kind='reproducibility_campaign',
        ref_id=campaign.campaign_id,
        ref_sha256=campaign.campaign_sha256,
    )


class CadMethodPrecisionModel(BaseModel):
    """Sealed variance decomposition bound to one campaign — within vs
    between components kept separately per metric."""

    model_config = ConfigDict(frozen=True)

    model_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    campaign_ref: AuthorityRef
    metrics: tuple[CadMetricPrecision, ...]
    analysis_method: str | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    model_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_model(self) -> 'CadMethodPrecisionModel':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.campaign_ref.ref_sha256 is None:
            raise ValueError(
                'the campaign pin must carry its sha256'
            )
        if not self.metrics:
            raise ValueError(
                'a precision model requires at least one metric'
            )
        labels = [m.metric_label for m in self.metrics]
        if len(labels) != len(set(labels)):
            raise ValueError('metric labels must be unique')
        expected = _hash(self.identity_payload())
        if self.model_sha256 != expected:
            raise ValueError('precision model hash mismatch')
        if self.model_id != _semantic_id('repmod', expected):
            raise ValueError('model id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'campaign_ref': self.campaign_ref.model_dump(mode='json'),
            'metrics': [m.model_dump(mode='json') for m in self.metrics],
            'analysis_method': self.analysis_method,
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }


def precision_model_binding(
    model: CadMethodPrecisionModel,
) -> AuthorityRef:
    return AuthorityRef(
        kind='method_precision_model',
        ref_id=model.model_id,
        ref_sha256=model.model_sha256,
    )


class CadReproducibilityQualification(BaseModel):
    """Sealed fail-closed verdict: which precision claims the method
    supports and which downstream gates (#566 / #577) may consume it."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    procedure_ref: AuthorityRef
    campaign_ref: AuthorityRef | None = None
    model_ref: AuthorityRef | None = None
    evidence_tier: EvidenceTier
    state: ReproducibilityState
    capabilities: tuple[
        tuple[ReproducibilityCapability, CapabilityState], ...
    ]
    confounded_factors: tuple[PrecisionFactor, ...] = ()
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'CadReproducibilityQualification':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        for label, ref in (
            ('procedure', self.procedure_ref),
            ('campaign', self.campaign_ref),
            ('precision model', self.model_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(
                    f'the {label} pin must carry its sha256'
                )
        covered = {capability for capability, _ in self.capabilities}
        if covered != set(ALL_REPRODUCIBILITY_CAPABILITIES):
            raise ValueError(
                'a reproducibility qualification must report every '
                'capability'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('qualification hash mismatch')
        if self.qualification_id != _semantic_id('repqual', expected):
            raise ValueError('qualification id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'procedure_ref': self.procedure_ref.model_dump(mode='json'),
            'campaign_ref': (
                self.campaign_ref.model_dump(mode='json')
                if self.campaign_ref is not None
                else None
            ),
            'model_ref': (
                self.model_ref.model_dump(mode='json')
                if self.model_ref is not None
                else None
            ),
            'evidence_tier': self.evidence_tier,
            'state': self.state,
            'capabilities': [list(item) for item in self.capabilities],
            'confounded_factors': list(self.confounded_factors),
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    def capability_state(
        self, capability: ReproducibilityCapability
    ) -> CapabilityState:
        return dict(self.capabilities)[capability]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _seal_model(model, payload: dict[str, Any], id_field: str,
                sha_field: str, prefix: str):
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{id_field: _semantic_id(prefix, digest), sha_field: digest},
    )


def build_method_procedure(
    *,
    document_id: str,
    method_name: str,
    procedure_version: str,
    documented: bool,
    steps_digest: str | None = None,
    stimulus_ref: AuthorityRef | None = None,
    pipeline_ref: AuthorityRef | None = None,
    measchain_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadMethodProcedure:
    """Seal the named measurement procedure (#693 §2)."""
    payload = dict(
        document_id=document_id,
        method_name=method_name,
        procedure_version=procedure_version,
        steps_digest=steps_digest,
        stimulus_ref=stimulus_ref,
        pipeline_ref=pipeline_ref,
        measchain_ref=measchain_ref,
        documented=documented,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=REP_AUTHORITY_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadMethodProcedure, payload,
        'procedure_id', 'procedure_sha256', 'repproc',
    )


def build_reproducibility_campaign(
    *,
    document_id: str,
    procedure_ref: AuthorityRef | CadMethodProcedure,
    design_class: CampaignDesignClass,
    varied_factors: tuple[PrecisionFactor, ...]
    | list[PrecisionFactor] = (),
    held_factors: tuple[PrecisionFactor, ...]
    | list[PrecisionFactor] = (),
    runs: tuple[CadCampaignRun, ...] | list[CadCampaignRun] = (),
    evidence_tier: EvidenceTier = 'unknown',
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadReproducibilityCampaign:
    """Seal the precision-study design + run roster (#693 §3/§4)."""
    if isinstance(procedure_ref, CadMethodProcedure):
        procedure_ref = method_procedure_binding(procedure_ref)
    payload = dict(
        document_id=document_id,
        procedure_ref=procedure_ref,
        design_class=design_class,
        varied_factors=tuple(varied_factors),
        held_factors=tuple(held_factors),
        runs=tuple(runs),
        evidence_tier=evidence_tier,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=REP_AUTHORITY_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadReproducibilityCampaign, payload,
        'campaign_id', 'campaign_sha256', 'repcamp',
    )


def build_precision_model(
    *,
    document_id: str,
    campaign_ref: AuthorityRef | CadReproducibilityCampaign,
    metrics: tuple[CadMetricPrecision, ...] | list[CadMetricPrecision],
    analysis_method: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadMethodPrecisionModel:
    """Seal the factor-attributed precision model (#693 §5)."""
    if isinstance(campaign_ref, CadReproducibilityCampaign):
        campaign_ref = reproducibility_campaign_binding(campaign_ref)
    payload = dict(
        document_id=document_id,
        campaign_ref=campaign_ref,
        metrics=tuple(metrics),
        analysis_method=analysis_method,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=REP_AUTHORITY_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadMethodPrecisionModel, payload,
        'model_id', 'model_sha256', 'repmod',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_reproducibility(
    *,
    document_id: str,
    procedure: CadMethodProcedure,
    campaign: CadReproducibilityCampaign | None = None,
    model: CadMethodPrecisionModel | None = None,
    evaluated_at_utc: str | None = None,
) -> CadReproducibilityQualification:
    """Fail-closed precision verdict.

    The tier is taken from the campaign's own declaration — this
    evaluator never upgrades it. What it does enforce: undocumented
    procedures, confounded designs, insufficient runs and missing
    between-factor variance fail closed; the #566 prediction gate needs
    reproducibility-grade evidence (not just repeatability), and the
    #577 decision gate additionally needs a bound precision model so a
    verdict can be compared against method spread.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []
    confounded: list[PrecisionFactor] = []
    caps: dict[ReproducibilityCapability, CapabilityState] = {
        capability: 'unknown'
        for capability in ALL_REPRODUCIBILITY_CAPABILITIES
    }

    if not procedure.documented:
        reasons.append(
            'procedure is not documented — an undocumented habit '
            'produces no reproducibility evidence'
        )

    tier: EvidenceTier = (
        campaign.evidence_tier if campaign is not None else 'unknown'
    )

    if campaign is None:
        state: ReproducibilityState = 'unqualified_insufficient_evidence'
        reasons.append('no reproducibility campaign bound')
        caps.update({c: 'invalid' for c in caps})
    else:
        completed = campaign.completed_runs()
        varied = set(campaign.varied_factors)
        caps['repeatability_known'] = (
            'valid' if len(completed) >= 2 else 'invalid'
        )
        if len(completed) < 2:
            reasons.append(
                'fewer than two completed runs — no within-method '
                'spread observable'
            )

        if campaign.design_class == 'single_factor_at_a_time':
            confounded.extend(sorted(varied))
            reasons.append(
                'single-factor-at-a-time design confounds every varied '
                'factor — between-factor attribution is limited'
            )
        elif campaign.design_class == 'unknown':
            reasons.append('campaign design class is unknown')

        for capability, factors in _BETWEEN_CAPABILITY_FACTORS.items():
            needed = factors & varied
            if not needed:
                caps[capability] = 'invalid'
                continue
            well_sampled = all(
                len(campaign.levels_of(f)) >= 2 for f in needed
            )
            if not well_sampled:
                caps[capability] = 'limited'
                reasons.append(
                    f'{capability}: factor varied but fewer than two '
                    'levels actually ran'
                )
            else:
                caps[capability] = (
                    'limited'
                    if campaign.design_class == 'single_factor_at_a_time'
                    else 'valid'
                )

        # Model-attributed factors that the campaign never varied are
        # claims without evidence — flag them.
        if model is not None:
            unattributed: set[str] = set()
            for metric in model.metrics:
                for component in metric.components:
                    if component.factor != 'residual_within_run' and (
                        component.factor not in varied
                    ):
                        unattributed.add(component.factor)
            if unattributed:
                reasons.append(
                    'precision model carries between-factor variance for '
                    'factors the campaign never varied: '
                    + ', '.join(sorted(unattributed))
                )
                for capability, factors in (
                    _BETWEEN_CAPABILITY_FACTORS.items()
                ):
                    if factors & set(unattributed):
                        caps[capability] = 'limited'
            small_n = [
                m.metric_label for m in model.metrics if m.n_runs < 4
            ]
            if small_n:
                reasons.append(
                    'metrics with fewer than 4 runs — variance '
                    'estimates are fragile: ' + ', '.join(small_n)
                )
                if caps['repeatability_known'] == 'valid':
                    caps['repeatability_known'] = 'limited'

        # --- state --------------------------------------------------------
        if campaign.design_class in (
            'single_factor_at_a_time', 'unknown',
        ) and varied:
            state = 'confounded_design'
        elif len(completed) < 2:
            state = 'insufficient_runs'
        elif tier == 'repeatability_only' or not varied:
            state = 'repeatability_only_established'
        elif model is None:
            state = 'limited_scope_precision'
            reasons.append(
                'campaign exists but no precision model decomposes the '
                'variance — between-condition claims stay limited'
            )
        elif confounded or caps['repeatability_known'] == 'limited':
            state = 'limited_scope_precision'
        elif tier == 'interlaboratory_formal':
            # unreachable in practice (validator requires crossed design),
            # but never auto-promote an internal run to conformance
            state = 'precision_model_established'
        else:
            state = 'precision_model_established'

        # --- downstream gates -------------------------------------------
        any_between_valid = any(
            caps[c] == 'valid' for c in _BETWEEN_CAPABILITY_FACTORS
        )
        caps['prediction_gate_eligible'] = (
            'valid'
            if (
                state == 'precision_model_established'
                and any_between_valid
            )
            else (
                'limited'
                if state in (
                    'limited_scope_precision',
                    'confounded_design',
                )
                else 'invalid'
            )
        )
        caps['decision_gate_eligible'] = (
            'valid'
            if (
                state == 'precision_model_established'
                and model is not None
            )
            else (
                'limited'
                if state in (
                    'limited_scope_precision',
                    'repeatability_only_established',
                ) and model is not None
                else 'invalid'
            )
        )
        if caps['prediction_gate_eligible'] != 'valid':
            reasons.append(
                '#566 prediction↔measurement validation requires '
                'reproducibility-grade evidence — a repeatable method is '
                'not a reproducible one'
            )
        if caps['decision_gate_eligible'] != 'valid':
            reasons.append(
                '#577 decision rules need a bound precision model to '
                'compare effect size against method spread'
            )

    payload = dict(
        document_id=document_id,
        procedure_ref=method_procedure_binding(procedure),
        campaign_ref=(
            reproducibility_campaign_binding(campaign)
            if campaign is not None
            else None
        ),
        model_ref=(
            precision_model_binding(model) if model is not None else None
        ),
        evidence_tier=tier,
        state=state,
        capabilities=tuple(
            (capability, caps[capability])
            for capability in ALL_REPRODUCIBILITY_CAPABILITIES
        ),
        confounded_factors=tuple(confounded),
        reasons=tuple(reasons),
        evaluation_version=REP_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadReproducibilityQualification, payload,
        'qualification_id', 'qualification_sha256', 'repqual',
    )


__all__ = [
    'ALL_REPRODUCIBILITY_CAPABILITIES',
    'CadCampaignRun',
    'CadConditionAssignment',
    'CadMethodPrecisionModel',
    'CadMethodProcedure',
    'CadMetricPrecision',
    'CadReproducibilityCampaign',
    'CadReproducibilityQualification',
    'CadVarianceComponent',
    'CampaignDesignClass',
    'CapabilityState',
    'EvidenceTier',
    'PrecisionFactor',
    'PrecisionMetricKind',
    'REP_AUTHORITY_SCHEMA_VERSION',
    'REP_EVALUATION_VERSION',
    'ReproducibilityCapability',
    'ReproducibilityState',
    'RunStatus',
    'build_method_procedure',
    'build_precision_model',
    'build_reproducibility_campaign',
    'evaluate_reproducibility',
    'method_procedure_binding',
    'precision_model_binding',
    'reproducibility_campaign_binding',
]
