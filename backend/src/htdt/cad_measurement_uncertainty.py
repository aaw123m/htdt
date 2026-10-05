"""Measurement uncertainty budget & metrological traceability (#572, REV56-MEASEV).

A measurement is only evidence when a consumer can ask *how trustworthy is
this value?* and get an honest answer. This module binds a versioned,
sealed uncertainty budget to measurement evidence and exposes the
significance verdicts that REV55 registration (#564), comparison (#566)
and correction qualification (#568) consume before they let a residual or
a before/after delta drive a claim.

Literature basis
----------------
- ISO/IEC Guide 98-3 (GUM): Type A evaluation from statistical analysis
  of repeated observations; Type B from any other evidence (certificates,
  declared specifications, bounds); combined standard uncertainty
  ``u_c(y) = sqrt(sum_i c_i^2 u_i^2)`` for uncorrelated inputs; expanded
  uncertainty ``U = k * u_c`` with an explicit coverage factor (k ≈ 2 for
  ~95 %).
- IEC 61094 / IEC 60942 microphone calibration chains: a traceable
  measurement is an *unbroken chain* of calibrations back to a reference.
  A chain with an undecided link is recorded honestly — it never upgrades
  itself to ``traceable_documented``.
- GUM Supplement 1 (JCGM 101): Monte-Carlo propagation is permitted only
  when the sampling assignments are declared (normal for a bare standard
  uncertainty, uniform inside a declared bound, bootstrap resampling for
  empirical samples) together with seed and draw count.

Design
------
- ``UncertaintyContributor`` extends the #979 ``UncertaintyComponent``
  semantics with the contributor *kind* (what physical source), the
  *provenance class* (how strong the evidence is — a declared bound is
  never promoted to calibration evidence) and the GUM Type A/B
  evaluation kind. Spectral (per-frequency) contributors propagate on a
  declared common grid.
- ``CalibrationChainLink`` records each link from instrument to SI
  reference; a broken or unknown link is explicit.
- ``MeasurementUncertaintyBudget`` is the sealed authority: subject
  binding (measurement / dataset / acquisition context / derived
  transform), contributors, traceability chain, propagation method,
  combined outcome and limitations.
- Significance verdicts: ``RESIDUAL_CLEARLY_ABOVE_MEASUREMENT_UNCERTAINTY``
  / ``RESIDUAL_COMPARABLE_TO_MEASUREMENT_UNCERTAINTY`` /
  ``RESIDUAL_BELOW_RESOLUTION_OF_EVIDENCE`` /
  ``INSUFFICIENT_UNCERTAINTY_INFORMATION`` for residuals, plus delta
  verdicts for before/after evidence and calibrator-drift verdicts for
  pre/post calibrator checks.
"""

from __future__ import annotations

from math import isfinite, sqrt
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_uncertainty_budget import (
    BudgetCombinationState,
    ComponentContribution,
    PropagationMethod,
    UncertaintyCategory,
    UncertaintyComponent,
    UncertaintyRepresentation,
    build_uncertainty_budget_spec,
    propagate_uncertainty_budget,
)
from .canonical_json import (
    canonical_sha256 as _hash,
    canonicalize_payload,
)
from .r120_geometry_compiler import ExactExternalAuthorityRef


MEASUREMENT_UNCERTAINTY_SCHEMA_VERSION = 'measev-mub-1'
"""Payload schema version for uncertainty budgets."""

UNCERTAINTY_EVALUATOR_VERSION = 'measev-mub-eval-1'
"""Version of the significance verdict algorithm; bump when rules change."""

MONTE_CARLO_SAMPLER_VERSION = 'gum-s1-assignments-1'
"""Declared sampling assignments for Monte-Carlo propagation."""

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


# --- contributor taxonomy -------------------------------------------------

UncertaintyContributorKind = Literal[
    'instrument_calibration',
    'calibration_file_application',
    'sound_calibrator_check',
    'interface_adc_gain',
    'clock_sample_rate',
    'microphone_position',
    'source_position',
    'microphone_orientation',
    'seat_fixture',
    'acoustic_environment',
    'background_noise',
    'repeatability',
    'averaging_count',
    'window_processing',
    'time_variance',
    'other',
]
"""Where the uncertainty physically comes from — never collapsed."""

UncertaintyProvenanceClass = Literal[
    'repeatability_empirical',
    'calibration_certificate',
    'manufacturer_spec',
    'standard_typical_value',
    'geometry_tolerance',
    'model_propagation',
    'assumed_bound',
    'unknown',
]
"""Evidence class. ``assumed_bound`` is the weakest evidence and may only
back a bounded interval; it is never promoted to certificate strength."""

EvaluationKind = Literal['type_a', 'type_b', 'unknown']
"""GUM evaluation kind. ``type_a`` is only legal for statistical
evaluation of a series of observations (empirical samples); every other
source is ``type_b``."""

TraceabilityClass = Literal[
    'traceable_documented',
    'traceable_limited',
    'relative_only',
    'untraceable',
    'unknown',
]
"""``traceable_documented`` requires an unbroken declared chain ending at
a reference or SI-traceable standard. ``relative_only`` supports shape
claims only — never absolute SPL."""

ObservableKind = Literal[
    'magnitude_db',
    'phase_deg',
    'direct_arrival_time_s',
    'absolute_spl_db',
    'decay_time_s',
    'time_series',
    'other',
]


_KIND_TO_CATEGORY: dict[str, UncertaintyCategory] = {
    'instrument_calibration': 'measurement_instrument',
    'calibration_file_application': 'measurement_instrument',
    'sound_calibrator_check': 'measurement_instrument',
    'interface_adc_gain': 'measurement_instrument',
    'clock_sample_rate': 'measurement_instrument',
    'microphone_position': 'spatial_operating_condition',
    'source_position': 'spatial_operating_condition',
    'microphone_orientation': 'spatial_operating_condition',
    'seat_fixture': 'spatial_operating_condition',
    'acoustic_environment': 'spatial_operating_condition',
    'background_noise': 'spatial_operating_condition',
    'repeatability': 'spatial_operating_condition',
    'time_variance': 'spatial_operating_condition',
    'averaging_count': 'numerical',
    'window_processing': 'numerical',
}


class UncertaintySpectrumPoint(BaseModel):
    """One frequency point of a spectral (per-frequency) uncertainty."""

    model_config = ConfigDict(frozen=True)

    frequency_hz: float = Field(gt=0.0)
    standard_uncertainty: float = Field(ge=0.0)

    @model_validator(mode='after')
    def finite(self) -> 'UncertaintySpectrumPoint':
        if not isfinite(float(self.frequency_hz)) or not isfinite(
            float(self.standard_uncertainty)
        ):
            raise ValueError('spectrum point values must be finite')
        return self


class UncertaintyContributor(BaseModel):
    """One contributor to a measurement uncertainty budget.

    ``provenance_class`` states how strong the underlying evidence is —
    ``assumed_bound`` contributors never leave the bounded-interval
    representation and never masquerade as calibration evidence.
    ``evaluation_kind`` states the GUM Type A/B classification; Type A is
    reserved for statistical evaluation of repeated observations.
    """

    model_config = ConfigDict(frozen=True)

    contributor_id: str = Field(min_length=1)
    kind: UncertaintyContributorKind
    provenance_class: UncertaintyProvenanceClass
    evaluation_kind: EvaluationKind = 'unknown'
    category: UncertaintyCategory | None = None
    subject_quantity: str = Field(min_length=1)
    quantity_unit: str | None = None
    representation: UncertaintyRepresentation
    bound_low: float | None = None
    bound_high: float | None = None
    standard_uncertainty: float | None = Field(default=None, ge=0.0)
    empirical_samples: tuple[float, ...] | None = None
    spectral_points: tuple[UncertaintySpectrumPoint, ...] | None = None
    sensitivity: float | None = None
    correlation_group_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    provenance_json: str = '{}'
    notes: str | None = None

    @model_validator(mode='after')
    def valid_contributor(self) -> 'UncertaintyContributor':
        if self.evaluation_kind == 'type_a' and self.representation != (
            'empirical_samples'
        ):
            raise ValueError(
                'type_a evaluation is reserved for statistical evaluation '
                'of a series of observations (empirical_samples)'
            )
        if self.evaluation_kind == 'unknown' and self.representation not in (
            'unknown',
        ):
            raise ValueError(
                'a contributor with a quantified representation requires '
                'an explicit type_a/type_b evaluation kind'
            )
        if (
            self.provenance_class == 'assumed_bound'
            and self.representation not in ('bounded_interval', 'unknown')
        ):
            raise ValueError(
                'an assumed bound may only back a bounded interval — it is '
                'never promoted to a standard uncertainty'
            )
        if self.provenance_class == 'calibration_certificate' and not (
            self.evidence_refs
        ):
            raise ValueError(
                'calibration_certificate contributors must cite their '
                'certificate evidence'
            )
        if (
            self.provenance_class == 'repeatability_empirical'
            and self.evaluation_kind != 'type_a'
            and self.representation == 'empirical_samples'
        ):
            raise ValueError(
                'empirical repeatability samples are a Type A evaluation'
            )
        if self.spectral_points is not None:
            freqs = [point.frequency_hz for point in self.spectral_points]
            if len(freqs) < 2:
                raise ValueError(
                    'spectral contributors require at least two points'
                )
            if sorted(freqs) != freqs or len(set(freqs)) != len(freqs):
                raise ValueError(
                    'spectral points must be strictly increasing in frequency'
                )
        return self

    def uncertainty_category(self) -> UncertaintyCategory:
        """Resolved budget category; ``model_propagation`` stays separate."""
        if self.category is not None:
            return self.category
        if self.provenance_class == 'model_propagation':
            return 'model_discrepancy'
        return _KIND_TO_CATEGORY.get(self.kind, 'unknown')

    def to_component(self) -> UncertaintyComponent:
        """Map onto the #979 propagation component (scalar part only)."""
        return UncertaintyComponent(
            component_id=self.contributor_id,
            subject_quantity=self.subject_quantity,
            quantity_unit=self.quantity_unit,
            category=self.uncertainty_category(),
            representation=self.representation,
            bound_low=self.bound_low,
            bound_high=self.bound_high,
            standard_uncertainty=self.standard_uncertainty,
            empirical_samples=self.empirical_samples,
            correlation_group_id=self.correlation_group_id,
            sensitivity=self.sensitivity,
            evidence_refs=self.evidence_refs,
            provenance_json=self.provenance_json,
        )


class CalibrationChainLink(BaseModel):
    """One link in the measurement → SI traceability chain.

    ``state='broken'`` is an honest declaration that the chain cannot be
    completed through this link; a budget containing a broken or unknown
    link may never claim ``traceable_documented``.
    """

    model_config = ConfigDict(frozen=True)

    link_id: str = Field(min_length=1)
    role: Literal[
        'measurement_instrument',
        'calibration_artifact',
        'sound_level_calibrator',
        'reference_standard',
        'si_traceable_reference',
    ]
    label: str | None = None
    instrument_id: str | None = None
    artifact_ref: ExactExternalAuthorityRef | None = None
    state: Literal['linked', 'broken', 'unknown'] = 'linked'
    declared_uncertainty_refs: tuple[str, ...] = ()
    notes: str | None = None

    @model_validator(mode='after')
    def valid_link(self) -> 'CalibrationChainLink':
        if self.state != 'linked' and not (self.notes or self.label):
            raise ValueError(
                'broken/unknown chain links require a stated reason'
            )
        return self


class CalibratorCheck(BaseModel):
    """One pre- or post-measurement sound-calibrator reading."""

    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1)
    check_kind: Literal['pre', 'post']
    measured_level_db: float
    nominal_level_db: float
    observed_at_utc: str | None = None
    instrument_id: str | None = None

    @model_validator(mode='after')
    def valid_check(self) -> 'CalibratorCheck':
        if not isfinite(float(self.measured_level_db)) or not isfinite(
            float(self.nominal_level_db)
        ):
            raise ValueError('calibrator levels must be finite')
        return self

    @property
    def deviation_db(self) -> float:
        return float(self.measured_level_db) - float(self.nominal_level_db)


class SpectralUncertaintyPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    frequency_hz: float = Field(gt=0.0)
    combined_standard_uncertainty: float = Field(ge=0.0)
    expanded_uncertainty: float | None = Field(default=None, ge=0.0)


class MeasurementUncertaintyOutcome(BaseModel):
    """Propagated outcome of the budget — the honest combined statistic."""

    model_config = ConfigDict(frozen=True)

    combination_state: BudgetCombinationState
    combined_standard_uncertainty: float | None = Field(default=None, ge=0.0)
    combined_bound_half_width: float | None = Field(default=None, ge=0.0)
    expanded_uncertainty: float | None = Field(default=None, ge=0.0)
    spectral_points: tuple[SpectralUncertaintyPoint, ...] = ()
    contributions: tuple[ComponentContribution, ...] = ()
    model_discrepancy_contribution: float | None = Field(
        default=None, ge=0.0
    )
    result_ref: str | None = None

    @model_validator(mode='after')
    def finite(self) -> 'MeasurementUncertaintyOutcome':
        for value in (
            self.combined_standard_uncertainty,
            self.combined_bound_half_width,
            self.expanded_uncertainty,
            self.model_discrepancy_contribution,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('outcome values must be finite')
        if self.combination_state == 'propagated' and (
            self.combined_standard_uncertainty is None
            and self.combined_bound_half_width is None
            and not self.spectral_points
        ):
            raise ValueError(
                'a propagated outcome requires a combined statistic'
            )
        return self


class MeasurementUncertaintyBudget(BaseModel):
    """Sealed uncertainty budget bound to one measurement subject.

    The budget is the authority for "how far can this evidence be pushed":
    residuals and deltas are judged against its combined or expanded
    uncertainty — never against an invented global tolerance.
    """

    model_config = ConfigDict(frozen=True)

    budget_id: str = Field(min_length=1)
    schema_version: str = MEASUREMENT_UNCERTAINTY_SCHEMA_VERSION
    document_id: str = Field(min_length=1)
    measurand: str = Field(min_length=1)
    measurand_unit: str | None = None
    observable_kind: ObservableKind = 'other'
    measurement_id: str | None = None
    dataset_id: str | None = None
    dataset_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    acquisition_context_id: str | None = None
    derived_transform_id: str | None = None
    position_id: str | None = None
    instrument_ids: tuple[str, ...] = ()
    calibration_artifact_ids: tuple[str, ...] = ()
    calibration_profile_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    contributors: tuple[UncertaintyContributor, ...] = ()
    traceability_links: tuple[CalibrationChainLink, ...] = ()
    traceability_class: TraceabilityClass = 'unknown'
    repeatability_measurement_ids: tuple[str, ...] = ()
    calibrator_checks: tuple[CalibratorCheck, ...] = ()
    propagation_method: PropagationMethod
    correlation_policy: Literal[
        'independent_unless_declared', 'correlation_groups'
    ] = 'independent_unless_declared'
    monte_carlo_seed: int | None = None
    monte_carlo_sample_count: int | None = None
    coverage_factor: float | None = None
    coverage_semantics: str | None = None
    outcome: MeasurementUncertaintyOutcome
    valid_frequency_hz: tuple[float, float] | None = None
    limitations: tuple[str, ...] = ()
    notes: str = ''
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'budget_id', 'semantic_sha256'}
        )

    @model_validator(mode='after')
    def consistent(self) -> 'MeasurementUncertaintyBudget':
        if not any(
            (
                self.measurement_id,
                self.dataset_id,
                self.acquisition_context_id,
                self.derived_transform_id,
            )
        ):
            raise ValueError(
                'an uncertainty budget must bind to a measurement, dataset, '
                'acquisition context or derived transform'
            )
        if not self.contributors:
            raise ValueError(
                'an uncertainty budget requires at least one contributor — '
                'missing uncertainty is declared via provenance_class '
                'unknown, never by an empty budget claiming certainty'
            )
        ids = [c.contributor_id for c in self.contributors]
        if len(ids) != len(set(ids)):
            raise ValueError('contributor ids must be unique')
        if self.propagation_method == 'monte_carlo':
            if (
                self.monte_carlo_seed is None
                or self.monte_carlo_sample_count is None
            ):
                raise ValueError(
                    'monte_carlo propagation requires a declared seed and '
                    'sample count'
                )
            if int(self.monte_carlo_sample_count) < 100:
                raise ValueError(
                    'monte_carlo propagation requires >= 100 draws'
                )
        if (
            self.outcome.expanded_uncertainty is not None
            and self.coverage_factor is None
        ):
            raise ValueError(
                'expanded uncertainty requires a declared coverage factor'
            )
        self._validate_traceability()
        self._validate_repeatability_binding()
        self._validate_frequency_binding()
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement uncertainty budget hash mismatch')
        expected_id = f'mub:{self.semantic_sha256}'
        if self.budget_id != expected_id:
            raise ValueError(
                'budget_id must be mub:<semantic sha256>'
            )
        return self

    def _validate_traceability(self) -> None:
        links = self.traceability_links
        if self.traceability_class == 'traceable_documented':
            if not links:
                raise ValueError(
                    'traceable_documented requires a declared chain'
                )
            if any(link.state != 'linked' for link in links):
                raise ValueError(
                    'a chain containing a broken or unknown link may never '
                    'claim traceable_documented'
                )
            if links[-1].role not in (
                'reference_standard',
                'si_traceable_reference',
            ):
                raise ValueError(
                    'a documented chain must terminate at a reference '
                    'standard or SI-traceable reference'
                )
        elif self.traceability_class == 'traceable_limited':
            if not links:
                raise ValueError(
                    'traceable_limited requires a declared chain with '
                    'declared gaps'
                )
        if self.traceability_class == 'relative_only' and (
            self.observable_kind == 'absolute_spl_db'
        ):
            raise ValueError(
                'absolute SPL evidence cannot be claimed on a '
                'relative_only traceability class'
            )

    def _validate_repeatability_binding(self) -> None:
        type_a_repeatability = [
            c
            for c in self.contributors
            if c.kind == 'repeatability'
            and c.evaluation_kind == 'type_a'
        ]
        if type_a_repeatability and not self.repeatability_measurement_ids:
            raise ValueError(
                'Type A repeatability contributors must cite the repeat '
                'captures they were evaluated from'
            )

    def _validate_frequency_binding(self) -> None:
        if self.valid_frequency_hz is not None:
            low, high = self.valid_frequency_hz
            if not (isfinite(float(low)) and isfinite(float(high))):
                raise ValueError('valid_frequency_hz must be finite')
            if not (0.0 < float(low) < float(high)):
                raise ValueError(
                    'valid_frequency_hz must satisfy 0 < low < high'
                )
        for point in self.outcome.spectral_points:
            if (
                self.valid_frequency_hz is not None
                and not (
                    self.valid_frequency_hz[0]
                    <= point.frequency_hz
                    <= self.valid_frequency_hz[1]
                )
            ):
                raise ValueError(
                    'spectral outcome points must lie inside '
                    'valid_frequency_hz'
                )


# --- significance verdicts ------------------------------------------------

ResidualSignificanceState = Literal[
    'residual_clearly_above_measurement_uncertainty',
    'residual_comparable_to_measurement_uncertainty',
    'residual_below_resolution_of_evidence',
    'insufficient_uncertainty_information',
]

DeltaSignificanceState = Literal[
    'delta_exceeds_combined_evidence_uncertainty',
    'delta_inconclusive_within_evidence_uncertainty',
    'insufficient_uncertainty_information',
]

CalibratorCheckState = Literal[
    'calibrator_stable',
    'calibrator_drift_detected',
    'insufficient_calibrator_evidence',
]


def significance_threshold(
    budget: MeasurementUncertaintyBudget,
) -> tuple[float, str] | None:
    """The honest significance threshold for this budget.

    Expanded uncertainty (declared coverage factor) is the strict gate;
    a combined bound is the worst-case gate; a bare combined standard
    uncertainty is the weakest gate — its use is always declared in the
    assessment limitations.
    """
    outcome = budget.outcome
    if outcome.expanded_uncertainty is not None:
        return float(outcome.expanded_uncertainty), 'expanded'
    if outcome.combined_bound_half_width is not None:
        return float(outcome.combined_bound_half_width), 'combined_bound'
    if outcome.combined_standard_uncertainty is not None:
        return (
            float(outcome.combined_standard_uncertainty),
            'combined_standard',
        )
    return None


def classify_residual(
    residual_value: float,
    budget: MeasurementUncertaintyBudget,
) -> ResidualSignificanceState:
    """Classify one residual against the budget — fail-closed when the
    budget has no usable combined statistic."""
    value = abs(float(residual_value))
    threshold = significance_threshold(budget)
    if threshold is None:
        return 'insufficient_uncertainty_information'
    gate, _semantics = threshold
    if value > gate:
        return 'residual_clearly_above_measurement_uncertainty'
    resolution = budget.outcome.combined_standard_uncertainty
    if resolution is None:
        resolution = budget.outcome.combined_bound_half_width
    if resolution is not None and value <= float(resolution) / 2.0:
        return 'residual_below_resolution_of_evidence'
    return 'residual_comparable_to_measurement_uncertainty'


def evaluate_delta_significance(
    delta: float,
    budget_a: MeasurementUncertaintyBudget,
    budget_b: MeasurementUncertaintyBudget,
) -> tuple[DeltaSignificanceState, float | None, list[str]]:
    """Judge a before/after delta against the combined uncertainty of the
    two evidence items (independent combination, u_delta = sqrt(u_a^2 +
    u_b^2) — the weakest gate of the pair is used as each side's u)."""
    limitations: list[str] = []
    ua = significance_threshold(budget_a)
    ub = significance_threshold(budget_b)
    if ua is None or ub is None:
        return 'insufficient_uncertainty_information', None, limitations
    for label, (value, semantics) in (('a', ua), ('b', ub)):
        if semantics != 'expanded':
            limitations.append(
                f"budget {label} gate uses '{semantics}' semantics — "
                'no expanded uncertainty was declared'
            )
    combined = sqrt(ua[0] ** 2 + ub[0] ** 2)
    if abs(float(delta)) > combined:
        return (
            'delta_exceeds_combined_evidence_uncertainty',
            combined,
            limitations,
        )
    return (
        'delta_inconclusive_within_evidence_uncertainty',
        combined,
        limitations,
    )


def evaluate_calibrator_drift(
    checks: tuple[CalibratorCheck, ...],
    tolerance_db: float,
) -> tuple[CalibratorCheckState, float | None]:
    """Compare pre/post calibrator checks — drift beyond the declared
    tolerance invalidates the measurement chain for that session."""
    if tolerance_db <= 0 or not isfinite(float(tolerance_db)):
        raise ValueError('tolerance_db must be a positive finite value')
    pre = [c for c in checks if c.check_kind == 'pre']
    post = [c for c in checks if c.check_kind == 'post']
    if not pre or not post:
        return 'insufficient_calibrator_evidence', None
    pre_level = sum(c.measured_level_db for c in pre) / len(pre)
    post_level = sum(c.measured_level_db for c in post) / len(post)
    drift = abs(post_level - pre_level)
    if drift > float(tolerance_db):
        return 'calibrator_drift_detected', drift
    return 'calibrator_stable', drift


class SignificanceObservation(BaseModel):
    """One residual value judged against the bound budget."""

    model_config = ConfigDict(frozen=True)

    observable: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    residual_value: float
    threshold_used: float | None = None
    threshold_semantics: Literal[
        'expanded', 'combined_bound', 'combined_standard', 'none'
    ] = 'none'
    verdict: ResidualSignificanceState

    @model_validator(mode='after')
    def finite(self) -> 'SignificanceObservation':
        if not isfinite(float(self.residual_value)):
            raise ValueError('residual_value must be finite')
        if self.threshold_used is not None and not isfinite(
            float(self.threshold_used)
        ):
            raise ValueError('threshold_used must be finite')
        return self


class ResidualSignificanceAssessment(BaseModel):
    """Sealed assessment: a residual report or delta judged against an
    uncertainty budget. Persisted as evidence — the verdict consumers cite."""

    model_config = ConfigDict(frozen=True)

    assessment_id: str = Field(min_length=1)
    schema_version: str = MEASUREMENT_UNCERTAINTY_SCHEMA_VERSION
    document_id: str = Field(min_length=1)
    subject_kind: Literal[
        'prediction_residual_report',
        'measurement_delta',
        'calibration_delta',
        'value_set',
    ]
    subject_ref_id: str | None = None
    subject_ref_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    budget_id: str = Field(min_length=1)
    budget_sha256: str = Field(pattern=_SHA256_PATTERN)
    evaluator_version: str = UNCERTAINTY_EVALUATOR_VERSION
    observations: tuple[SignificanceObservation, ...] = ()
    summary_state: ResidualSignificanceState | DeltaSignificanceState
    limitations: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'assessment_id', 'semantic_sha256'}
        )

    @model_validator(mode='after')
    def consistent(self) -> 'ResidualSignificanceAssessment':
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('significance assessment hash mismatch')
        expected_id = f'msa:{self.semantic_sha256}'
        if self.assessment_id != expected_id:
            raise ValueError(
                'assessment_id must be msa:<semantic sha256>'
            )
        return self


def _observation_for(
    observable: str,
    reference: str,
    value: float,
    budget: MeasurementUncertaintyBudget,
) -> SignificanceObservation:
    threshold = significance_threshold(budget)
    verdict = classify_residual(value, budget)
    return SignificanceObservation(
        observable=observable,
        reference=reference,
        residual_value=float(value),
        threshold_used=None if threshold is None else float(threshold[0]),
        threshold_semantics='none' if threshold is None else threshold[1],
        verdict=verdict,
    )


def assess_residual_report(
    report: Any,
    budget: MeasurementUncertaintyBudget,
    *,
    created_at_utc: str,
) -> ResidualSignificanceAssessment:
    """Judge every computed residual of a #564 residual report against the
    bound measurement uncertainty — band by band, observable by
    observable. Residuals below the evidence threshold are reported as
    such, never silently read as agreement."""
    observations: list[SignificanceObservation] = []
    for observable in report.observables:
        if observable.state != 'computed':
            continue
        for band in observable.magnitude_bands:
            lo, hi = band.band_hz
            label = f'{lo:g}-{hi:g} Hz'
            for metric_name, value in (
                ('mean_difference_db', band.mean_difference_db),
                ('rms_difference_db', band.rms_difference_db),
                ('shape_rms_db', band.shape_rms_db),
            ):
                if value is None:
                    continue
                observations.append(
                    _observation_for(
                        observable.observable,
                        f'{label}/{metric_name}',
                        float(value),
                        budget,
                    )
                )
        for band in observable.phase_bands:
            lo, hi = band.band_hz
            label = f'{lo:g}-{hi:g} Hz'
            for field_name in band.model_fields:
                value = getattr(band, field_name)
                if (
                    field_name.endswith('_deg')
                    and isinstance(value, (int, float))
                    and value is not None
                ):
                    observations.append(
                        _observation_for(
                            observable.observable,
                            f'{label}/{field_name}',
                            float(value),
                            budget,
                        )
                    )
        if observable.direct_arrival is not None and (
            observable.direct_arrival.error_s is not None
        ):
            observations.append(
                _observation_for(
                    observable.observable,
                    'direct_arrival/error_s',
                    float(observable.direct_arrival.error_s),
                    budget,
                )
            )
        if observable.modal_peaks is not None:
            for pair in observable.modal_peaks.pairs:
                observations.append(
                    _observation_for(
                        observable.observable,
                        f'modal:{pair.predicted_mode_hz:g}Hz/error_hz',
                        float(pair.error_hz),
                        budget,
                    )
                )
    if not observations:
        summary: ResidualSignificanceState = (
            'insufficient_uncertainty_information'
        )
    elif any(
        o.verdict == 'residual_clearly_above_measurement_uncertainty'
        for o in observations
    ):
        summary = 'residual_clearly_above_measurement_uncertainty'
    elif all(
        o.verdict == 'residual_below_resolution_of_evidence'
        for o in observations
    ):
        summary = 'residual_below_resolution_of_evidence'
    else:
        summary = 'residual_comparable_to_measurement_uncertainty'
    limitations: list[str] = []
    threshold = significance_threshold(budget)
    if threshold is not None and threshold[1] != 'expanded':
        limitations.append(
            f"gate used '{threshold[1]}' semantics — no expanded "
            'uncertainty was declared on the bound budget'
        )
    payload: dict[str, Any] = {
        'assessment_id': '0' * 64,
        'schema_version': MEASUREMENT_UNCERTAINTY_SCHEMA_VERSION,
        'document_id': report.document_id,
        'subject_kind': 'prediction_residual_report',
        'subject_ref_id': report.report_id,
        'subject_ref_sha256': report.semantic_sha256,
        'budget_id': budget.budget_id,
        'budget_sha256': budget.semantic_sha256,
        'evaluator_version': UNCERTAINTY_EVALUATOR_VERSION,
        'observations': tuple(observations),
        'summary_state': summary,
        'limitations': tuple(limitations),
        'created_at_utc': created_at_utc,
        'semantic_sha256': '0' * 64,
    }
    provisional = ResidualSignificanceAssessment.model_construct(
        **canonicalize_payload(
            ResidualSignificanceAssessment, dict(**payload)
        )
    )
    payload['semantic_sha256'] = _hash(provisional.identity_payload())
    payload['assessment_id'] = f'msa:{payload["semantic_sha256"]}'
    return ResidualSignificanceAssessment(**payload)


def assess_measurement_delta(
    *,
    document_id: str,
    subject_ref_id: str | None,
    deltas: tuple[tuple[str, str, float], ...],
    budget_a: MeasurementUncertaintyBudget,
    budget_b: MeasurementUncertaintyBudget,
    subject_kind: Literal[
        'measurement_delta', 'calibration_delta', 'value_set'
    ] = 'measurement_delta',
    created_at_utc: str,
) -> ResidualSignificanceAssessment:
    """Judge before/after deltas (e.g. 0.2 dB correction deltas) against
    the *combined* uncertainty of both evidence items — a delta smaller
    than the combined evidence uncertainty is inconclusive, not a win."""
    combined_threshold, limitations = _combined_delta_gate(
        budget_a, budget_b
    )
    observations: list[SignificanceObservation] = []
    for observable, reference, value in deltas:
        verdict: ResidualSignificanceState
        if combined_threshold is None:
            verdict = 'insufficient_uncertainty_information'
        elif abs(float(value)) > combined_threshold:
            verdict = 'residual_clearly_above_measurement_uncertainty'
        else:
            verdict = 'residual_comparable_to_measurement_uncertainty'
        observations.append(
            SignificanceObservation(
                observable=observable,
                reference=reference,
                residual_value=float(value),
                threshold_used=combined_threshold,
                threshold_semantics='expanded'
                if combined_threshold is not None
                else 'none',
                verdict=verdict,
            )
        )
    if combined_threshold is None or not observations:
        summary: DeltaSignificanceState = (
            'insufficient_uncertainty_information'
        )
    elif all(
        o.verdict == 'residual_clearly_above_measurement_uncertainty'
        for o in observations
    ):
        summary = 'delta_exceeds_combined_evidence_uncertainty'
    else:
        summary = 'delta_inconclusive_within_evidence_uncertainty'
    payload: dict[str, Any] = {
        'assessment_id': '0' * 64,
        'schema_version': MEASUREMENT_UNCERTAINTY_SCHEMA_VERSION,
        'document_id': document_id,
        'subject_kind': subject_kind,
        'subject_ref_id': subject_ref_id,
        'subject_ref_sha256': None,
        'budget_id': budget_a.budget_id,
        'budget_sha256': budget_a.semantic_sha256,
        'evaluator_version': UNCERTAINTY_EVALUATOR_VERSION,
        'observations': tuple(observations),
        'summary_state': summary,
        'limitations': tuple(limitations),
        'created_at_utc': created_at_utc,
        'semantic_sha256': '0' * 64,
    }
    provisional = ResidualSignificanceAssessment.model_construct(
        **canonicalize_payload(
            ResidualSignificanceAssessment, dict(**payload)
        )
    )
    payload['semantic_sha256'] = _hash(provisional.identity_payload())
    payload['assessment_id'] = f'msa:{payload["semantic_sha256"]}'
    return ResidualSignificanceAssessment(**payload)


def _combined_delta_gate(
    budget_a: MeasurementUncertaintyBudget,
    budget_b: MeasurementUncertaintyBudget,
) -> tuple[float | None, list[str]]:
    _state, combined, limitations = evaluate_delta_significance(
        0.0, budget_a, budget_b
    )
    return combined, limitations


# --- construction ---------------------------------------------------------


def _monte_carlo_combine(
    contributors: tuple[UncertaintyContributor, ...],
    *,
    seed: int,
    sample_count: int,
    coverage_factor: float | None,
) -> tuple[
    float | None,
    float | None,
    float | None,
    tuple[ComponentContribution, ...],
    tuple[str, ...],
]:
    """GUM-S1 Monte-Carlo propagation with declared assignments:

    - ``standard_uncertainty`` → normal(0, u) (GUM-S1 default assignment)
    - ``bounded_interval`` → uniform(low, high) (declared bound)
    - ``empirical_samples`` → bootstrap resample of the recorded series

    Anything else is skipped and declared — never resampled by fiat.
    """
    rng = np.random.default_rng(seed)
    draws: list[np.ndarray] = []
    contributions: list[ComponentContribution] = []
    limitations: list[str] = [
        f'monte_carlo sampling assignments: {MONTE_CARLO_SAMPLER_VERSION} '
        '(normal for standard_uncertainty, uniform for bounded_interval, '
        'bootstrap resample for empirical_samples)',
    ]
    discrepancy = 0.0
    discrepancy_seen = False
    for contributor in contributors:
        comp = contributor.to_component()
        if comp.representation == 'standard_uncertainty':
            sample = rng.normal(
                0.0, float(comp.standard_uncertainty), sample_count
            )
        elif comp.representation == 'bounded_interval':
            sample = rng.uniform(
                float(comp.bound_low), float(comp.bound_high), sample_count
            )
        elif comp.representation == 'empirical_samples':
            sample = rng.choice(
                np.asarray(comp.empirical_samples, dtype=float),
                size=sample_count,
            )
        else:
            limitations.append(
                f'contributor {comp.component_id} representation '
                f'{comp.representation!r} is not samplable under '
                f'{MONTE_CARLO_SAMPLER_VERSION} — excluded'
            )
            continue
        sensitivity = float(
            comp.sensitivity if comp.sensitivity is not None else 1.0
        )
        if comp.category == 'model_discrepancy':
            discrepancy += float(np.std(sample, ddof=1)) * abs(sensitivity)
            discrepancy_seen = True
            continue
        draws.append(sample * sensitivity)
        contributions.append(
            ComponentContribution(
                component_id=comp.component_id,
                category=comp.category,
                contribution=float(np.std(sample, ddof=1))
                * abs(sensitivity),
                representation=comp.representation,
            )
        )
    if not draws:
        limitations.append('no contributor was samplable — declared_only')
        return (
            None,
            None if not discrepancy_seen else discrepancy,
            None,
            tuple(contributions),
            tuple(limitations),
        )
    total = np.sum(np.stack(draws, axis=0), axis=0)
    combined = float(np.std(total, ddof=1))
    expanded = (
        combined * float(coverage_factor)
        if coverage_factor is not None
        else None
    )
    return (
        combined,
        discrepancy if discrepancy_seen else None,
        expanded,
        tuple(contributions),
        tuple(limitations),
    )


def _spectral_combine(
    contributors: tuple[UncertaintyContributor, ...],
    coverage_factor: float | None,
) -> tuple[tuple[SpectralUncertaintyPoint, ...], list[str]]:
    """Per-frequency RSS combination of spectral contributors.

    All spectral contributors must share one declared frequency grid —
    interpolation across different grids is itself a transform and is
    refused rather than invented.
    """
    limitations: list[str] = []
    spectral = [c for c in contributors if c.spectral_points]
    if not spectral:
        return (), limitations
    grids = {
        tuple(p.frequency_hz for p in c.spectral_points) for c in spectral
    }
    if len(grids) != 1:
        limitations.append(
            'spectral contributors declared different frequency grids — '
            'per-frequency combination refused (declare a resampling '
            'transform instead of silent interpolation)'
        )
        return (), limitations
    grid = grids.pop()
    points: list[SpectralUncertaintyPoint] = []
    for index, frequency in enumerate(grid):
        rss_sq = 0.0
        for contributor in spectral:
            u = contributor.spectral_points[index].standard_uncertainty
            sens = (
                contributor.sensitivity
                if contributor.sensitivity is not None
                else 1.0
            )
            rss_sq += (u * abs(sens)) ** 2
        combined = sqrt(rss_sq)
        points.append(
            SpectralUncertaintyPoint(
                frequency_hz=float(frequency),
                combined_standard_uncertainty=combined,
                expanded_uncertainty=(
                    combined * float(coverage_factor)
                    if coverage_factor is not None
                    else None
                ),
            )
        )
    return tuple(points), limitations


def build_measurement_uncertainty_budget(
    *,
    document_id: str,
    measurand: str,
    measurand_unit: str | None = None,
    observable_kind: ObservableKind = 'other',
    measurement_id: str | None = None,
    dataset_id: str | None = None,
    dataset_sha256: str | None = None,
    acquisition_context_id: str | None = None,
    derived_transform_id: str | None = None,
    position_id: str | None = None,
    instrument_ids: tuple[str, ...] = (),
    calibration_artifact_ids: tuple[str, ...] = (),
    calibration_profile_sha256: str | None = None,
    contributors: tuple[UncertaintyContributor, ...],
    traceability_links: tuple[CalibrationChainLink, ...] = (),
    traceability_class: TraceabilityClass = 'unknown',
    repeatability_measurement_ids: tuple[str, ...] = (),
    calibrator_checks: tuple[CalibratorCheck, ...] = (),
    propagation_method: PropagationMethod,
    correlation_policy: Literal[
        'independent_unless_declared', 'correlation_groups'
    ] = 'independent_unless_declared',
    monte_carlo_seed: int | None = None,
    monte_carlo_sample_count: int | None = None,
    coverage_factor: float | None = None,
    coverage_semantics: str | None = None,
    valid_frequency_hz: tuple[float, float] | None = None,
    limitations: tuple[str, ...] = (),
    notes: str = '',
    created_at_utc: str,
    spec_id: str | None = None,
    spec_model_ref: str | None = None,
) -> MeasurementUncertaintyBudget:
    """Assemble the budget, propagate under the declared method and seal.

    Scalar combination reuses the #979 propagator (first_order_linear /
    bounded_worst_case / declared_only); Monte-Carlo runs the declared
    GUM-S1 assignments; spectral contributors combine per frequency.
    """
    limitations_list = list(limitations)
    components = tuple(c.to_component() for c in contributors)
    result_ref: str | None = None
    combination_state: BudgetCombinationState = 'declared_only'
    combined_std: float | None = None
    combined_bound: float | None = None
    expanded: float | None = None
    contributions: tuple[ComponentContribution, ...] = ()
    discrepancy: float | None = None

    if propagation_method in (
        'first_order_linear',
        'bounded_worst_case',
        'declared_only',
    ):
        spec = build_uncertainty_budget_spec(
            spec_id=spec_id or 'mub-spec',
            schema_version=MEASUREMENT_UNCERTAINTY_SCHEMA_VERSION,
            document_id=document_id,
            measurand=measurand,
            measurand_unit=measurand_unit,
            model_ref=spec_model_ref,
            components=components,
            propagation_method=propagation_method,
            correlation_policy=correlation_policy,
            output_semantics=measurand_unit,
            created_at_utc=created_at_utc,
        )
        try:
            result = propagate_uncertainty_budget(
                spec,
                result_id='mub-result',
                created_at_utc=created_at_utc,
                coverage_factor=coverage_factor,
            )
        except ValueError as exc:
            combination_state = 'unsupported'
            limitations_list.append(f'propagation refused: {exc}')
        else:
            result_ref = result.result_sha256
            combination_state = result.combination_state
            combined_std = result.combined_standard_uncertainty
            combined_bound = result.combined_bound_half_width
            expanded = result.expanded_uncertainty
            contributions = result.contributions
            discrepancy = result.model_discrepancy_contribution
            limitations_list.extend(result.limitations)
    elif propagation_method == 'monte_carlo':
        if monte_carlo_seed is None or monte_carlo_sample_count is None:
            raise ValueError(
                'monte_carlo propagation requires a declared seed and '
                'sample count'
            )
        (
            combined_std,
            discrepancy,
            expanded,
            contributions,
            mc_limitations,
        ) = _monte_carlo_combine(
            contributors,
            seed=int(monte_carlo_seed),
            sample_count=int(monte_carlo_sample_count),
            coverage_factor=coverage_factor,
        )
        limitations_list.extend(mc_limitations)
        combination_state = (
            'propagated' if combined_std is not None else 'declared_only'
        )
    elif propagation_method == 'hybrid':
        raise ValueError(
            'hybrid propagation is not implemented — decompose into '
            'first_order_linear + bounded_worst_case budgets explicitly'
        )
    else:
        combination_state = 'unsupported'
        limitations_list.append(
            f"propagation method {propagation_method!r} is not implemented"
        )

    spectral_points, spectral_limitations = _spectral_combine(
        contributors, coverage_factor
    )
    limitations_list.extend(spectral_limitations)
    if spectral_points and combination_state == 'declared_only':
        combination_state = 'partial'

    outcome = MeasurementUncertaintyOutcome(
        combination_state=combination_state,
        combined_standard_uncertainty=combined_std,
        combined_bound_half_width=combined_bound,
        expanded_uncertainty=expanded,
        spectral_points=spectral_points,
        contributions=contributions,
        model_discrepancy_contribution=discrepancy,
        result_ref=result_ref,
    )
    payload: dict[str, Any] = {
        'budget_id': '0' * 64,
        'schema_version': MEASUREMENT_UNCERTAINTY_SCHEMA_VERSION,
        'document_id': document_id,
        'measurand': measurand,
        'measurand_unit': measurand_unit,
        'observable_kind': observable_kind,
        'measurement_id': measurement_id,
        'dataset_id': dataset_id,
        'dataset_sha256': dataset_sha256,
        'acquisition_context_id': acquisition_context_id,
        'derived_transform_id': derived_transform_id,
        'position_id': position_id,
        'instrument_ids': tuple(instrument_ids),
        'calibration_artifact_ids': tuple(calibration_artifact_ids),
        'calibration_profile_sha256': calibration_profile_sha256,
        'contributors': tuple(contributors),
        'traceability_links': tuple(traceability_links),
        'traceability_class': traceability_class,
        'repeatability_measurement_ids': tuple(
            repeatability_measurement_ids
        ),
        'calibrator_checks': tuple(calibrator_checks),
        'propagation_method': propagation_method,
        'correlation_policy': correlation_policy,
        'monte_carlo_seed': monte_carlo_seed,
        'monte_carlo_sample_count': monte_carlo_sample_count,
        'coverage_factor': coverage_factor,
        'coverage_semantics': coverage_semantics,
        'outcome': outcome,
        'valid_frequency_hz': valid_frequency_hz,
        'limitations': tuple(limitations_list),
        'notes': notes,
        'created_at_utc': created_at_utc,
        'semantic_sha256': '0' * 64,
    }
    provisional = MeasurementUncertaintyBudget.model_construct(
        **canonicalize_payload(
            MeasurementUncertaintyBudget, dict(**payload)
        )
    )
    payload['semantic_sha256'] = _hash(provisional.identity_payload())
    payload['budget_id'] = f'mub:{payload["semantic_sha256"]}'
    return MeasurementUncertaintyBudget(**payload)


def uncertainty_display_decimals(
    budget: MeasurementUncertaintyBudget,
) -> int | None:
    """Recommended display precision derived from the budget — UI never
    shows more decimals than the evidence supports. ``None`` when no
    usable combined statistic exists."""
    threshold = significance_threshold(budget)
    if threshold is None or threshold[0] <= 0:
        return None
    gate = threshold[0]
    # one more digit than the gate's order of magnitude, floored at 0
    decimals = 0
    scale = gate
    while scale < 1.0 and decimals < 4:
        scale *= 10.0
        decimals += 1
    return decimals
