"""Uncertainty-aware acoustic validation authority (issue #810, REV63).

The O60 residual gate ``max_holdout_rms_db`` is a useful software check, but
one broadband dB threshold is not a statement of physical validity. This
module makes validation *uncertainty-aware*: every predicted-vs-measured
residual is judged against declared uncertainty on **both** sides, per
observable and per frequency band, under a preregistered, versioned
protocol — never by a universal scalar.

Basis
-----
- ASME V&V 20: validation comparison error must be weighed against the
  *combined* uncertainty of simulation and experiment; neither side may be
  silently assumed exact.
- Thydal et al. 2021 (Appl. Acoustics 178:107939): acoustic VUQ separates
  propagated input uncertainty from model-form discrepancy; the two are
  never merged into "noise".
- JCGM 100 (GUM) / JCGM 101: combined standard uncertainty, expanded
  uncertainty with a declared coverage factor, and honest treatment of
  bounded intervals (``±x`` is a bound, not a Gaussian sigma).
- ISO 3382-1/-2, ISO 18233: room-acoustic measurement is per-observable
  and per-band; a smooth high-frequency region cannot excuse a modal-band
  failure inside one average.

Boundaries held here
--------------------
- #572 ``cad_measurement_uncertainty`` owns the measurement-side budget;
  this layer binds it (``mub:`` refs and spectral outcomes), never
  re-derives it.
- #979 ``cad_uncertainty_budget`` owns combination mathematics and the
  category taxonomy; dominance reporting reuses its categories and keeps
  ``model_discrepancy`` strictly separate from measurement noise.
- #604 ``cad_uncertainty_propagation`` owns input-uncertainty propagation;
  a propagated interval enters as a *bounded* side, never a sigma.
- #698 ``cad_validation_statistics`` owns sampling/dependence governance;
  ``correlation_policy`` here only declares how the two *sides* of one
  comparison combine.
- JND values are perceptual interpretation only — they annotate, never
  gate (issue §4).
- Calibration and holdout stay absolutely separate: an evaluation carries
  exactly one split, a candidate can never appear in both, and the top
  verdict is driven by holdout evidence with calibration reported as a
  distinct state.
- The legacy ``max_holdout_rms_db`` record remains a valid input — bound
  as evidence and snapshotted — but it is exactly one metric on the
  record; it can never alone yield ``consistent_with_reference_within_
  uncertainty``.

Fail-closed vocabulary (issue §5, snake_case per repo convention):
``consistent_with_reference_within_uncertainty`` / ``discrepancy_
significant`` / ``model_form_discrepancy_required`` /
``reference_too_uncertain`` / ``input_uncertainty_dominates`` /
``numerical_uncertainty_dominates`` / ``band_coverage_incomplete`` /
``insufficient_evidence``. No verdict in this module ever means
"production validated" — that claim lives upstream of VUQ entirely.
"""

from __future__ import annotations

from math import isfinite, sqrt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_model_validation import CadModelValidationRecord, EvidenceScope
from .cad_uncertainty_budget import UncertaintyCategory
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


VUQ_PROTOCOL_SCHEMA_VERSION = 'vuq-protocol-1'
"""Payload schema version for the preregistered validation protocol."""

VUQ_EVALUATOR_VERSION = 'vuq-eval-1'
"""Version of the comparison/summary verdict algorithm; bump on rule change."""

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


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


def _require_sha_ref(ref: AuthorityRef, label: str) -> None:
    if ref.ref_sha256 is None:
        raise ValueError(f'{label} reference must pin its sha256')


# ---------------------------------------------------------------------------
# Taxonomies (#810 §1/§2)
# ---------------------------------------------------------------------------

ObservableQuantity = Literal[
    'magnitude_response_db',
    'phase_response_deg',
    'arrival_time_s',
    'modal_frequency_hz',
    'decay_time_s',
    'early_energy_db',
    'room_acoustic_parameter',
    'objective_metric',
    'other_declared',
]
"""Production-relevant observables each get an explicit quantity — one
metric is never silently shared across them (issue §2)."""

ObservableDomain = Literal['frequency', 'time', 'level', 'spatial', 'other']

ComparisonMetricKind = Literal[
    'signed_error',
    'rms_difference',
    'band_rms',
    'peak_error',
    'relative_error',
]

UncertaintySideSource = Literal[
    'measurement_uncertainty_budget',
    'uncertainty_budget_result',
    'propagated_interval',
    'declared_bound',
    'none',
]
"""Where one side's uncertainty authority comes from:
``measurement_uncertainty_budget`` binds #572; ``uncertainty_budget_result``
binds a #979 result; ``propagated_interval`` binds a #604 propagated
sampled interval (always bounded semantics); ``declared_bound`` records a
declared engineering bound that never becomes a sigma; ``none`` is honest
absence — it produces ``insufficient_uncertainty_information``, never a
pass."""

SideThresholdSemantics = Literal[
    'expanded', 'combined_bound', 'combined_standard', 'none',
]
"""The significance-gate semantics one side actually supports — expanded
(declared coverage factor) is the strictest, a combined bound the
worst-case gate, a bare combined standard uncertainty the weakest, and
``none`` means the side carries no usable combined statistic."""

CorrelationPolicy = Literal[
    'independent_unless_declared', 'declared_correlated',
]
"""How measurement-side and prediction-side uncertainty combine in one
band (issue §7): quadrature unless correlation is declared; a declared
correlation combines linearly — conservative and labelled, never an
estimated coefficient."""

BoundedInputPolicy = Literal[
    'bounded_worst_case', 'declared_distribution_required',
]
"""How ``±`` inputs may enter a comparison (issue §6): linear worst-case
summation, or require an explicitly declared distribution/budget — never
a silent Gaussian."""

BandCoverageState = Literal[
    'evaluated', 'excluded_unsupported', 'unmeasured',
]
"""Every declared band is accounted for: evaluated against evidence,
explicitly excluded as unsupported (retained, never hidden), or declared
unmeasured. Coverage gaps are evidence about the study, not silence."""

BandUncertaintyVerdict = Literal[
    'consistent_within_uncertainty',
    'discrepancy_significant',
    'below_evidence_resolution',
    'evidence_too_uncertain',
    'insufficient_uncertainty_information',
]

ObservableUncertaintyVerdict = Literal[
    'consistent_with_reference_within_uncertainty',
    'discrepancy_significant',
    'model_form_discrepancy_required',
    'reference_too_uncertain',
    'input_uncertainty_dominates',
    'numerical_uncertainty_dominates',
    'band_coverage_incomplete',
    'insufficient_evidence',
]
"""Issue §5 vocabulary in repo snake_case. ``model_form_discrepancy_
required`` means the significant excess cannot be attributed to the
model's own declared model-form envelope — new model-form evidence is
required; the discrepancy is never disguised as measurement noise."""

AbsoluteValidationVerdict = ObservableUncertaintyVerdict

RankingVerdict = Literal[
    'ranking_supported', 'ranking_contradicted', 'ranking_not_evaluated',
]
"""Direction/order accuracy is a separate axis from absolute residual
accuracy (issue §9) — a common model bias can coexist with correct
ranking, so each reports its own verdict."""

CalibrationEvidenceState = Literal[
    'calibration_consistent', 'calibration_discrepancy', 'no_calibration_evidence',
]

CombinedSemantics = Literal[
    'expanded', 'combined_bound', 'combined_standard', 'none',
]

_SEMANTICS_RANK: dict[str, int] = {
    'expanded': 3,
    'combined_bound': 2,
    'combined_standard': 1,
    'none': 0,
}

#: Fixed precedence for dominance ties — deterministic, declared here.
_CATEGORY_PRECEDENCE: tuple[str, ...] = (
    'model_discrepancy',
    'measurement_instrument',
    'spatial_operating_condition',
    'model_input',
    'numerical',
    'unknown',
)


# ---------------------------------------------------------------------------
# Protocol (preregistered, versioned — issue §8)
# ---------------------------------------------------------------------------


class UncertaintyDecisionRule(BaseModel):
    """The preregistered decision rule for one observable.

    Thresholds are *significance ratios* against combined uncertainty, not
    dB values: ``significance_ratio`` scales the combined uncertainty the
    residual must exceed to count as significant, and
    ``resolution_fraction`` marks residuals too small for the evidence to
    resolve. ``jnd_interpretation_value`` is perceptual annotation only —
    it is never read by any verdict path.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    significance_ratio: float = Field(default=1.0, ge=1.0)
    resolution_fraction: float = Field(default=0.5, gt=0.0, lt=1.0)
    max_tolerable_comparison_uncertainty: float | None = Field(
        default=None, gt=0.0
    )
    jnd_interpretation_value: float | None = Field(default=None, gt=0.0)
    jnd_note: str | None = None

    @model_validator(mode='after')
    def valid_rule(self) -> 'UncertaintyDecisionRule':
        for value in (
            self.significance_ratio,
            self.resolution_fraction,
            self.max_tolerable_comparison_uncertainty,
            self.jnd_interpretation_value,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('decision rule values must be finite')
        return self


class ValidationObservableMetric(BaseModel):
    """One observable's declared validation contract (issue §2).

    Every production-relevant observable carries its own quantity, unit,
    domain, comparison metric, applicability range and the minimum
    uncertainty semantics its evidence must reach — nothing inherits a
    shared broadband default.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    observable_id: str = Field(min_length=1)
    quantity: ObservableQuantity
    unit: str = Field(min_length=1)
    domain: ObservableDomain = 'frequency'
    comparison_metric: ComparisonMetricKind
    applicability_band_hz: tuple[float, float] | None = None
    minimum_uncertainty_semantics: SideThresholdSemantics = 'combined_standard'
    decision_rule: UncertaintyDecisionRule

    @model_validator(mode='after')
    def valid_metric(self) -> 'ValidationObservableMetric':
        if self.applicability_band_hz is not None:
            low, high = self.applicability_band_hz
            if not (isfinite(float(low)) and isfinite(float(high))) or not (
                0.0 < float(low) < float(high)
            ):
                raise ValueError('applicability band must satisfy 0 < low < high')
        return self


class ValidationUncertaintyProtocol(BaseModel):
    """Sealed, versioned validation protocol (issue §8).

    The protocol is preregistered authority: decision rules, uncertainty
    combination policies and observable contracts are sealed *before* the
    evaluation consumes them, and a relaxed rule can only arrive through a
    new ``protocol_version`` bound by ``supersedes_protocol_ref`` — a
    threshold can never be loosened inside the same identity after
    inspecting holdout evidence.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    protocol_id: str = Field(min_length=1)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_version: Literal['vuq-protocol-1'] = VUQ_PROTOCOL_SCHEMA_VERSION
    document_id: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    supersedes_protocol_ref: AuthorityRef | None = None
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    observable_metrics: tuple[ValidationObservableMetric, ...] = Field(
        min_length=1
    )
    correlation_policy: CorrelationPolicy = 'independent_unless_declared'
    bounded_input_policy: BoundedInputPolicy = 'bounded_worst_case'
    calibration_uncertainty_tuned: bool = False
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_protocol(self) -> 'ValidationUncertaintyProtocol':
        ids = [metric.observable_id for metric in self.observable_metrics]
        if len(ids) != len(set(ids)):
            raise ValueError('protocol observable ids must be unique')
        if self.supersedes_protocol_ref is not None:
            _require_sha_ref(
                self.supersedes_protocol_ref, 'superseded protocol'
            )
        if self.protocol_sha256 != _hash(self.identity_payload()):
            raise ValueError('validation uncertainty protocol hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'protocol_id', 'protocol_sha256'}
        )

    def observable(self, observable_id: str) -> ValidationObservableMetric | None:
        for metric in self.observable_metrics:
            if metric.observable_id == observable_id:
                return metric
        return None

    @classmethod
    def create(cls, **payload: Any) -> 'ValidationUncertaintyProtocol':
        return _seal(
            cls, payload, 'protocol_id', 'protocol_sha256', 'vup'
        )


# ---------------------------------------------------------------------------
# Side evidence — declared uncertainty on ONE side of a comparison
# ---------------------------------------------------------------------------


class SideSpectralUncertaintyPoint(BaseModel):
    """One frequency point of a side's spectral uncertainty (issue §3)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    uncertainty: float = Field(ge=0.0)

    @model_validator(mode='after')
    def finite(self) -> 'SideSpectralUncertaintyPoint':
        if not isfinite(float(self.frequency_hz)) or not isfinite(
            float(self.uncertainty)
        ):
            raise ValueError('spectral uncertainty values must be finite')
        return self


class CategoryContributionValue(BaseModel):
    """One uncertainty category's contribution snapshot — dominance
    reporting reuses the #979 taxonomy and never folds model discrepancy
    into measurement noise."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    category: UncertaintyCategory
    contribution: float = Field(ge=0.0)

    @model_validator(mode='after')
    def finite(self) -> 'CategoryContributionValue':
        if not isfinite(float(self.contribution)):
            raise ValueError('category contribution must be finite')
        return self


class UncertaintySideEvidence(BaseModel):
    """Declared uncertainty for one side of the comparison.

    This is a *bound snapshot* of upstream authority (a #572 budget, a
    #979 result, a #604 propagated interval or an honestly declared
    bound): the numeric fields are exactly what the bound record declared,
    pinned by ``source_ref`` — never recomputed or reinterpreted here.
    ``source_kind='none'`` is explicit absence: it yields insufficient
    information downstream, never a silent zero.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    source_kind: UncertaintySideSource
    source_ref: AuthorityRef | None = None
    threshold_semantics: SideThresholdSemantics = 'none'
    scalar_uncertainty: float | None = Field(default=None, ge=0.0)
    bound_half_width: float | None = Field(default=None, ge=0.0)
    spectral_points: tuple[SideSpectralUncertaintyPoint, ...] | None = None
    valid_frequency_hz: tuple[float, float] | None = None
    category_contributions: tuple[CategoryContributionValue, ...] = ()
    model_discrepancy_contribution: float | None = Field(default=None, ge=0.0)

    @model_validator(mode='after')
    def valid_side(self) -> 'UncertaintySideEvidence':
        for value in (
            self.scalar_uncertainty,
            self.bound_half_width,
            self.model_discrepancy_contribution,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('uncertainty values must be finite')
        quantified = (
            self.scalar_uncertainty is not None
            or self.bound_half_width is not None
            or bool(self.spectral_points)
        )
        if self.source_kind == 'none':
            if (
                self.threshold_semantics != 'none'
                or quantified
                or self.source_ref is not None
                or self.category_contributions
                or self.model_discrepancy_contribution is not None
            ):
                raise ValueError(
                    "source_kind 'none' declares no usable statistic — "
                    'it cannot carry uncertainty values or provenance'
                )
        if self.threshold_semantics == 'none' and quantified:
            raise ValueError(
                "'none' semantics declares no usable statistic — "
                'quantified values cannot ride along silently'
            )
        if self.source_kind in (
            'measurement_uncertainty_budget',
            'uncertainty_budget_result',
            'propagated_interval',
        ):
            if self.source_ref is None:
                raise ValueError(
                    'an authority-bound uncertainty source requires '
                    'source_ref'
                )
            _require_sha_ref(self.source_ref, self.source_kind)
        if self.source_kind == 'propagated_interval':
            if self.scalar_uncertainty is not None:
                raise ValueError(
                    'a propagated sampled interval is bounded evidence — '
                    'it never presents as a standard uncertainty'
                )
            if self.threshold_semantics not in ('combined_bound', 'none'):
                raise ValueError(
                    'a propagated interval only supports combined_bound '
                    'semantics — a finite sweep is never a coverage claim'
                )
        if self.source_kind == 'declared_bound' and (
            self.bound_half_width is None
            or self.scalar_uncertainty is not None
            or self.threshold_semantics != 'combined_bound'
        ):
            raise ValueError(
                'a declared bound carries bound_half_width under '
                'combined_bound semantics — it is never silently promoted '
                'to a standard uncertainty'
            )
        if self.spectral_points is not None:
            freqs = [p.frequency_hz for p in self.spectral_points]
            if len(freqs) < 2 or sorted(freqs) != freqs or (
                len(set(freqs)) != len(freqs)
            ):
                raise ValueError(
                    'spectral points must be strictly increasing in frequency'
                )
        if self.valid_frequency_hz is not None:
            low, high = self.valid_frequency_hz
            if not (isfinite(float(low)) and isfinite(float(high))) or not (
                0.0 < float(low) < float(high)
            ):
                raise ValueError('valid_frequency_hz must satisfy 0 < low < high')
            if self.spectral_points is not None:
                for point in self.spectral_points:
                    if not (low <= point.frequency_hz <= high):
                        raise ValueError(
                            'spectral uncertainty points must lie inside '
                            'valid_frequency_hz'
                        )
        return self


# ---------------------------------------------------------------------------
# Band evaluations
# ---------------------------------------------------------------------------


def _band_side_uncertainty(
    side: UncertaintySideEvidence, band_hz: tuple[float, float]
) -> float | None:
    """One side's uncertainty inside one band — honest coverage.

    A spectral side contributes the *worst* declared point inside the band
    (a good region cannot dilute a bad one); a band lying at all outside
    the side's declared ``valid_frequency_hz`` is a gap, not an extrapolation.
    """
    low, high = band_hz
    if side.valid_frequency_hz is not None:
        valid_low, valid_high = side.valid_frequency_hz
        if low < valid_low or high > valid_high:
            return None
    if side.spectral_points:
        values = [
            point.uncertainty
            for point in side.spectral_points
            if low <= point.frequency_hz <= high
        ]
        if not values:
            return None
        return max(values)
    if side.scalar_uncertainty is not None:
        return float(side.scalar_uncertainty)
    if side.bound_half_width is not None:
        return float(side.bound_half_width)
    return None


def _combine_sides(
    u_measurement: float,
    u_prediction: float,
    measurement_semantics: SideThresholdSemantics,
    prediction_semantics: SideThresholdSemantics,
    policy: CorrelationPolicy,
) -> tuple[float, CombinedSemantics]:
    """Combine the two sides' declared uncertainties for one band.

    Quadrature when independent is defensible; a *linear* sum when either
    side is a bound or correlation is declared — conservative and labelled
    by ``combination_rule`` so the weaker semantics is never disguised.
    """
    bounded_side = (
        'combined_bound' in (measurement_semantics, prediction_semantics)
    )
    weakest_rank = min(
        _SEMANTICS_RANK[measurement_semantics],
        _SEMANTICS_RANK[prediction_semantics],
    )
    if policy == 'declared_correlated' or bounded_side:
        # Bound or declared correlation: linear worst-case combination is
        # the honest reading — semantics degrade to combined_bound.
        return u_measurement + u_prediction, 'combined_bound'
    combined = sqrt(u_measurement ** 2 + u_prediction ** 2)
    semantics: CombinedSemantics = {
        3: 'expanded',
        2: 'combined_bound',
        1: 'combined_standard',
        0: 'none',
    }[weakest_rank]
    return combined, semantics


class BandUncertaintyEvaluation(BaseModel):
    """One declared band's residual judged against both sides' uncertainty.

    ``residual_value``/``residual_unit``/``coverage`` are inputs; every
    derived field (per-side u, combined u, semantics, ratio, verdict) is
    recomputed by the parent evaluation's validator from those inputs plus
    the bound side evidence — a stored verdict cannot drift from its
    evidence without breaking the seal.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    band_hz: tuple[float, float]
    coverage: BandCoverageState = 'evaluated'
    residual_value: float | None = None
    residual_unit: str | None = None
    excluded_reason: str | None = None
    measurement_uncertainty: float | None = Field(default=None, ge=0.0)
    prediction_uncertainty: float | None = Field(default=None, ge=0.0)
    comparison_uncertainty: float | None = Field(default=None, ge=0.0)
    combined_semantics: CombinedSemantics = 'none'
    combination_rule: Literal['quadrature', 'linear', 'none'] = 'none'
    significance_ratio_value: float | None = Field(default=None, ge=0.0)
    verdict: BandUncertaintyVerdict | None = None

    @model_validator(mode='after')
    def valid_band(self) -> 'BandUncertaintyEvaluation':
        low, high = self.band_hz
        if not (isfinite(float(low)) and isfinite(float(high))) or not (
            0.0 < float(low) < float(high)
        ):
            raise ValueError('band_hz must satisfy 0 < low < high')
        if self.coverage == 'evaluated':
            if self.residual_value is None or self.residual_unit is None:
                raise ValueError(
                    'an evaluated band requires residual_value and unit'
                )
            if not isfinite(float(self.residual_value)):
                raise ValueError('residual_value must be finite')
            if self.verdict is None:
                raise ValueError('an evaluated band requires a verdict')
        else:
            if self.verdict is not None:
                raise ValueError(
                    'a non-evaluated band carries no verdict — it is '
                    'coverage evidence, not a judgment'
                )
            if self.coverage == 'excluded_unsupported' and (
                not self.excluded_reason
            ):
                raise ValueError(
                    'an excluded band must state why it is unsupported'
                )
        for value in (
            self.measurement_uncertainty,
            self.prediction_uncertainty,
            self.comparison_uncertainty,
            self.significance_ratio_value,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('band uncertainty values must be finite')
        return self


def _evaluate_band(
    *,
    band_hz: tuple[float, float],
    coverage: BandCoverageState,
    residual_value: float | None,
    residual_unit: str | None,
    excluded_reason: str | None,
    measurement: UncertaintySideEvidence,
    prediction: UncertaintySideEvidence,
    observable: ValidationObservableMetric,
    correlation_policy: CorrelationPolicy,
) -> dict[str, Any]:
    """Derive one band's comparison from inputs + declared side evidence.

    Fail-closed: a band with a residual but no usable combined statistic,
    or whose combined semantics fall below the observable's declared
    minimum, yields ``insufficient_uncertainty_information``.
    """
    derived: dict[str, Any] = {
        'band_hz': band_hz,
        'coverage': coverage,
        'residual_value': residual_value,
        'residual_unit': residual_unit,
        'excluded_reason': excluded_reason,
    }
    if coverage != 'evaluated':
        derived.update(
            measurement_uncertainty=None,
            prediction_uncertainty=None,
            comparison_uncertainty=None,
            combined_semantics='none',
            combination_rule='none',
            significance_ratio_value=None,
            verdict=None,
        )
        return derived

    u_meas = _band_side_uncertainty(measurement, band_hz)
    u_pred = _band_side_uncertainty(prediction, band_hz)
    if u_meas is None or u_pred is None:
        derived.update(
            measurement_uncertainty=u_meas,
            prediction_uncertainty=u_pred,
            comparison_uncertainty=None,
            combined_semantics='none',
            combination_rule='none',
            significance_ratio_value=None,
            verdict='insufficient_uncertainty_information',
        )
        return derived

    combined, semantics = _combine_sides(
        u_meas,
        u_pred,
        measurement.threshold_semantics,
        prediction.threshold_semantics,
        correlation_policy,
    )
    rule = observable.decision_rule
    residual = abs(float(residual_value))  # type: ignore[arg-type]
    ratio = residual / combined if combined > 0 else None
    if _SEMANTICS_RANK[semantics] < _SEMANTICS_RANK[
        observable.minimum_uncertainty_semantics
    ]:
        verdict: BandUncertaintyVerdict = 'insufficient_uncertainty_information'
    elif residual > float(rule.significance_ratio) * combined:
        verdict = 'discrepancy_significant'
    elif (
        rule.max_tolerable_comparison_uncertainty is not None
        and combined > float(rule.max_tolerable_comparison_uncertainty)
    ):
        verdict = 'evidence_too_uncertain'
    elif (
        combined > 0
        and residual <= float(rule.resolution_fraction) * combined
    ):
        verdict = 'below_evidence_resolution'
    else:
        verdict = 'consistent_within_uncertainty'
    derived.update(
        measurement_uncertainty=u_meas,
        prediction_uncertainty=u_pred,
        comparison_uncertainty=combined,
        combined_semantics=semantics,
        combination_rule='linear' if semantics == 'combined_bound' and (
            correlation_policy == 'declared_correlated'
            or 'combined_bound' in (
                measurement.threshold_semantics,
                prediction.threshold_semantics,
            )
        ) else 'quadrature',
        significance_ratio_value=ratio,
        verdict=verdict,
    )
    return derived


def _dominant_category(
    measurement: UncertaintySideEvidence,
    prediction: UncertaintySideEvidence,
) -> UncertaintyCategory:
    """Which declared uncertainty category dominates this comparison."""
    totals: dict[str, float] = {}
    for side in (measurement, prediction):
        for contribution in side.category_contributions:
            totals[contribution.category] = totals.get(
                contribution.category, 0.0
            ) + float(contribution.contribution)
    if not totals:
        return 'unknown'
    best = max(totals.values())
    for category in _CATEGORY_PRECEDENCE:
        if totals.get(category) == best:
            return category  # type: ignore[return-value]
    return 'unknown'


def _model_form_declared(
    measurement: UncertaintySideEvidence,
    prediction: UncertaintySideEvidence,
) -> float | None:
    """The model-form envelope the model itself declared — kept strictly
    separate from measurement noise; ``None`` when nothing was declared."""
    declared = [
        value
        for value in (
            measurement.model_discrepancy_contribution,
            prediction.model_discrepancy_contribution,
        )
        if value is not None
    ]
    if not declared:
        return None
    return sum(float(value) for value in declared)


def _summarize_bands(
    *,
    bands: tuple[BandUncertaintyEvaluation, ...],
    observable: ValidationObservableMetric,
    measurement: UncertaintySideEvidence,
    prediction: UncertaintySideEvidence,
) -> tuple[ObservableUncertaintyVerdict, list[str]]:
    """Aggregate band verdicts into the observable verdict — fail closed.

    A significant discrepancy outranks incompleteness (a found failure is
    a finding, not a gap); missing uncertainty or zero evaluated bands is
    ``insufficient_evidence``; coverage holes surface explicitly as
    ``band_coverage_incomplete`` — never as agreement.
    """
    limitations: list[str] = []
    evaluated = [band for band in bands if band.coverage == 'evaluated']
    significant = [
        band for band in evaluated if band.verdict == 'discrepancy_significant'
    ]
    if significant:
        worst_excess = max(
            abs(float(band.residual_value))  # type: ignore[arg-type]
            - float(band.comparison_uncertainty)  # type: ignore[arg-type]
            for band in significant
        )
        declared = _model_form_declared(measurement, prediction)
        if declared is not None and declared >= worst_excess:
            limitations.append(
                'significant residual excess is within the model’s own '
                'declared model-form envelope'
            )
            return 'discrepancy_significant', limitations
        return 'model_form_discrepancy_required', limitations
    if not evaluated:
        return 'insufficient_evidence', limitations
    if any(
        band.verdict == 'insufficient_uncertainty_information'
        for band in evaluated
    ):
        return 'insufficient_evidence', limitations
    if any(band.verdict == 'evidence_too_uncertain' for band in evaluated):
        dominant = _dominant_category(measurement, prediction)
        if dominant == 'model_input':
            return 'input_uncertainty_dominates', limitations
        if dominant == 'numerical':
            return 'numerical_uncertainty_dominates', limitations
        return 'reference_too_uncertain', limitations
    gaps = [band for band in bands if band.coverage != 'evaluated']
    if gaps:
        limitations.append(
            f'{len(gaps)} declared band(s) were excluded or unmeasured — '
            'coverage is explicitly incomplete'
        )
        return 'band_coverage_incomplete', limitations
    if observable.applicability_band_hz is not None:
        low, high = observable.applicability_band_hz
        cursor = float(low)
        for band in sorted(evaluated, key=lambda item: item.band_hz[0]):
            band_low, band_high = band.band_hz
            if float(band_low) > cursor:
                break
            cursor = max(cursor, float(band_high))
        if cursor < float(high):
            return 'band_coverage_incomplete', limitations
    if all(
        band.verdict == 'below_evidence_resolution' for band in evaluated
    ):
        limitations.append(
            'every evaluated residual lies below the resolution of the '
            'declared evidence — consistency is bounded by that resolution'
        )
    if any(
        band.combined_semantics != 'expanded' for band in evaluated
    ):
        limitations.append(
            'at least one band was judged below expanded-uncertainty '
            'semantics — the weakest declared gate applies'
        )
    return 'consistent_with_reference_within_uncertainty', limitations


# ---------------------------------------------------------------------------
# Observable evaluation (sealed, one candidate × one observable × one split)
# ---------------------------------------------------------------------------


class PerceptualInterpretation(BaseModel):
    """JND-style annotation (issue §4): perceptual context only.

    ``role`` is pinned to ``perceptual_interpretation_only`` — a JND
    comparison can interpret an outcome for a listener but can never feed
    a verdict, replace measurement uncertainty or stand in for numerical
    convergence.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    role: Literal['perceptual_interpretation_only'] = (
        'perceptual_interpretation_only'
    )
    bands_below_declared_jnd: int = Field(ge=0)
    bands_evaluated: int = Field(ge=0)
    jnd_unit: str | None = None
    note: str | None = None

    @model_validator(mode='after')
    def valid_interpretation(self) -> 'PerceptualInterpretation':
        if self.bands_below_declared_jnd > self.bands_evaluated:
            raise ValueError(
                'bands below JND cannot exceed evaluated bands'
            )
        return self


class ObservableUncertaintyEvaluation(BaseModel):
    """Sealed per-observable uncertainty evaluation (one candidate, one
    split, one observable).

    The record binds the protocol that authorized the decision rule, the
    measurement-side and prediction-side uncertainty evidence, and every
    declared band — evaluated, excluded or unmeasured. The validator
    recomputes all derived band state and the summary verdict, so the
    seal covers the *derivation*, not just the stored fields.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority_version: Literal['vuq-eval-1'] = VUQ_EVALUATOR_VERSION
    document_id: str = Field(min_length=1)
    protocol_ref: AuthorityRef
    protocol_version: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    split: Literal['calibration', 'holdout']
    observable: ValidationObservableMetric
    measurement_evidence: UncertaintySideEvidence
    prediction_evidence: UncertaintySideEvidence
    correlation_policy: CorrelationPolicy = 'independent_unless_declared'
    bands: tuple[BandUncertaintyEvaluation, ...] = Field(min_length=1)
    summary_verdict: ObservableUncertaintyVerdict
    dominant_uncertainty_category: UncertaintyCategory = 'unknown'
    perceptual_interpretation: PerceptualInterpretation | None = None
    limitations: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def consistent(self) -> 'ObservableUncertaintyEvaluation':
        _require_sha_ref(self.protocol_ref, 'validation protocol')
        for field in (self.measurement_evidence, self.prediction_evidence):
            if field.source_ref is not None:
                _require_sha_ref(field.source_ref, 'uncertainty source')
        keys = [tuple(band.band_hz) for band in self.bands]
        if len(keys) != len(set(keys)):
            raise ValueError('band evaluations must be unique per band')
        for band in self.bands:
            if (
                band.coverage == 'evaluated'
                and band.residual_unit != self.observable.unit
            ):
                raise ValueError(
                    'band residual unit must match the observable unit'
                )
            expected = _evaluate_band(
                band_hz=band.band_hz,
                coverage=band.coverage,
                residual_value=band.residual_value,
                residual_unit=band.residual_unit,
                excluded_reason=band.excluded_reason,
                measurement=self.measurement_evidence,
                prediction=self.prediction_evidence,
                observable=self.observable,
                correlation_policy=self.correlation_policy,
            )
            expected_band = BandUncertaintyEvaluation(**expected)
            if band != expected_band:
                raise ValueError(
                    'band evaluation does not match the derivation from '
                    'bound uncertainty evidence'
                )
        expected_verdict, expected_limitations = _summarize_bands(
            bands=self.bands,
            observable=self.observable,
            measurement=self.measurement_evidence,
            prediction=self.prediction_evidence,
        )
        if self.summary_verdict != expected_verdict:
            raise ValueError(
                'observable summary verdict does not match its band '
                'evidence'
            )
        expected_dominant = _dominant_category(
            self.measurement_evidence, self.prediction_evidence
        )
        if self.dominant_uncertainty_category != expected_dominant:
            raise ValueError(
                'dominant uncertainty category does not match declared '
                'contributions'
            )
        if tuple(expected_limitations) != self.limitations:
            raise ValueError('evaluation limitations do not match derivation')
        if self.evaluation_sha256 != _hash(self.identity_payload()):
            raise ValueError('observable uncertainty evaluation hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'evaluation_id', 'evaluation_sha256'}
        )

    @classmethod
    def create(cls, **payload: Any) -> 'ObservableUncertaintyEvaluation':
        return _seal(
            cls, payload, 'evaluation_id', 'evaluation_sha256', 'uoe'
        )


def build_observable_uncertainty_evaluation(
    *,
    document_id: str,
    protocol: ValidationUncertaintyProtocol,
    candidate_id: str,
    split: Literal['calibration', 'holdout'],
    observable_id: str,
    measurement_evidence: UncertaintySideEvidence,
    prediction_evidence: UncertaintySideEvidence,
    bands: tuple[dict[str, Any] | BandUncertaintyEvaluation, ...],
    created_at_utc: str | None = None,
) -> ObservableUncertaintyEvaluation:
    """Evaluate one candidate's residual evidence for one observable under
    the bound protocol.

    ``bands`` carries only the *inputs* (band range, coverage, residual,
    exclusion reason): every derived field is computed here, so the sealed
    evaluation cannot declare a verdict its evidence does not support.
    The observable must be declared in the protocol — undeclared
    observables can never be evaluated.
    """
    observable = protocol.observable(observable_id)
    if observable is None:
        raise ValueError(
            f'observable {observable_id!r} is not declared in the bound '
            'validation uncertainty protocol'
        )
    inputs = tuple(
        item if isinstance(item, BandUncertaintyEvaluation) else
        BandUncertaintyEvaluation.model_construct(**item)
        for item in bands
    )
    evaluated_bands = tuple(
        BandUncertaintyEvaluation(
            **_evaluate_band(
                band_hz=tuple(item.band_hz),
                coverage=item.coverage,
                residual_value=item.residual_value,
                residual_unit=item.residual_unit,
                excluded_reason=item.excluded_reason,
                measurement=measurement_evidence,
                prediction=prediction_evidence,
                observable=observable,
                correlation_policy=protocol.correlation_policy,
            )
        )
        for item in inputs
    )
    summary, limitations = _summarize_bands(
        bands=evaluated_bands,
        observable=observable,
        measurement=measurement_evidence,
        prediction=prediction_evidence,
    )
    perceptual = None
    rule = observable.decision_rule
    if rule.jnd_interpretation_value is not None:
        below = sum(
            1
            for band in evaluated_bands
            if band.coverage == 'evaluated'
            and band.residual_value is not None
            and abs(float(band.residual_value))
            <= float(rule.jnd_interpretation_value)
        )
        perceptual = PerceptualInterpretation(
            bands_below_declared_jnd=below,
            bands_evaluated=sum(
                1 for band in evaluated_bands if band.coverage == 'evaluated'
            ),
            jnd_unit=observable.unit,
            note=rule.jnd_note,
        )
    payload: dict[str, Any] = {
        'authority_version': VUQ_EVALUATOR_VERSION,
        'document_id': document_id,
        'protocol_ref': AuthorityRef(
            kind='validation_uncertainty_protocol',
            ref_id=protocol.protocol_id,
            ref_sha256=protocol.protocol_sha256,
        ),
        'protocol_version': protocol.protocol_version,
        'candidate_id': candidate_id,
        'split': split,
        'observable': observable,
        'measurement_evidence': measurement_evidence,
        'prediction_evidence': prediction_evidence,
        'correlation_policy': protocol.correlation_policy,
        'bands': evaluated_bands,
        'summary_verdict': summary,
        'dominant_uncertainty_category': _dominant_category(
            measurement_evidence, prediction_evidence
        ),
        'perceptual_interpretation': perceptual,
        'limitations': tuple(limitations),
        'created_at_utc': created_at_utc or _utc_now(),
    }
    return ObservableUncertaintyEvaluation.create(**payload)


# ---------------------------------------------------------------------------
# Validation verdict (sealed, whole-study verdict)
# ---------------------------------------------------------------------------


class EvaluationBinding(BaseModel):
    """A pinned snapshot of one bound observable evaluation.

    ``evaluation_sha256`` is copied from the sealed evaluation at build
    time; the repository re-checks every snapshot field against the stored
    row before the verdict is accepted, so a verdict can never restate an
    evaluation it did not see.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    evaluation_ref: AuthorityRef
    observable_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    split: Literal['calibration', 'holdout']
    summary_verdict: ObservableUncertaintyVerdict
    dominant_uncertainty_category: UncertaintyCategory = 'unknown'

    @model_validator(mode='after')
    def valid_binding(self) -> 'EvaluationBinding':
        _require_sha_ref(self.evaluation_ref, 'observable evaluation')
        return self


class UncertaintyValidationVerdict(BaseModel):
    """Sealed whole-study uncertainty-aware validation verdict.

    ``absolute_verdict`` is driven by holdout bindings alone; calibration
    evidence is reported separately as ``calibration_state`` and can never
    migrate into the holdout judgment. ``ranking_verdict`` is the distinct
    direction/order axis (issue §9) derived from the bound legacy record's
    trend/separation gates — itself only evidence, never a production
    claim. ``legacy_*`` snapshot the bound ``CadModelValidationRecord``'s
    scalar gate: recorded as one metric, structurally incapable of
    producing ``consistent_with_reference_within_uncertainty`` alone.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    verdict_id: str = Field(min_length=1)
    verdict_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority_version: Literal['vuq-eval-1'] = VUQ_EVALUATOR_VERSION
    document_id: str = Field(min_length=1)
    protocol_ref: AuthorityRef
    protocol_version: str = Field(min_length=1)
    protocol_observable_ids: tuple[str, ...] = ()
    """Snapshot of the bound protocol's declared observable ids — the
    repository re-checks it against the stored protocol, and a declared
    observable with no holdout binding means the study verdict is
    ``insufficient_evidence`` (§2: every production-relevant observable
    must be explicitly evaluated)."""
    evidence_scope: EvidenceScope = 'synthetic_fixture'
    campaign_id: str | None = Field(default=None, min_length=1)
    campaign_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    campaign_registration_id: str | None = Field(
        default=None,
        pattern=r'^o60-validation-campaign-registration:[0-9a-f]{64}$',
    )
    campaign_registration_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    validation_ref: AuthorityRef | None = None
    legacy_residual_gate: Literal['pass', 'fail', 'insufficient'] | None = None
    legacy_holdout_rms_db: float | None = Field(default=None, ge=0.0)
    legacy_max_holdout_rms_db: float | None = Field(default=None, gt=0.0)
    legacy_trend_gates: tuple[str, ...] = ()
    legacy_separation_gates: tuple[str, ...] = ()
    evaluation_bindings: tuple[EvaluationBinding, ...] = ()
    absolute_verdict: AbsoluteValidationVerdict
    ranking_verdict: RankingVerdict = 'ranking_not_evaluated'
    calibration_state: CalibrationEvidenceState = 'no_calibration_evidence'
    dominant_uncertainty_category: UncertaintyCategory = 'unknown'
    model_form_discrepancy_suspected: bool = False
    gate_reasons: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def consistent(self) -> 'UncertaintyValidationVerdict':
        _require_sha_ref(self.protocol_ref, 'validation protocol')
        if self.validation_ref is not None:
            _require_sha_ref(self.validation_ref, 'model validation record')
        registration_fields = (
            self.campaign_registration_id,
            self.campaign_registration_sha256,
        )
        if self.evidence_scope == 'owned_room':
            if self.campaign_id is None or self.campaign_sha256 is None:
                raise ValueError(
                    'owned-room validation requires a preregistered campaign'
                )
            if any(field is None for field in registration_fields):
                raise ValueError(
                    'owned-room validation requires a durable campaign '
                    'registration'
                )
        else:
            if self.campaign_id is not None or self.campaign_sha256 is not None:
                raise ValueError(
                    'synthetic validation must not claim an owned-room '
                    'campaign'
                )
            if any(field is not None for field in registration_fields):
                raise ValueError(
                    'synthetic validation must not claim a campaign '
                    'registration'
                )

        ref_ids = [binding.evaluation_ref.ref_id for binding in self.evaluation_bindings]
        if len(ref_ids) != len(set(ref_ids)):
            raise ValueError('evaluation bindings must be unique')
        seen: dict[tuple[str, str], str] = {}
        for binding in self.evaluation_bindings:
            key = (binding.observable_id, binding.candidate_id)
            if key in seen and seen[key] != binding.split:
                raise ValueError(
                    'one observable for one candidate cannot be evaluated '
                    'in both calibration and holdout splits'
                )
            seen[key] = binding.split
        candidate_splits: dict[str, str] = {}
        for binding in self.evaluation_bindings:
            prior = candidate_splits.setdefault(
                binding.candidate_id, binding.split
            )
            if prior != binding.split:
                raise ValueError(
                    'candidate cannot appear in both calibration and '
                    'holdout splits'
                )

        if len(self.protocol_observable_ids) != len(
            set(self.protocol_observable_ids)
        ) or any(not oid for oid in self.protocol_observable_ids):
            raise ValueError(
                'protocol observable snapshot must be unique non-empty ids'
            )
        expected = _derive_verdict_payload(self)
        for field_name, value in expected.items():
            if getattr(self, field_name) != value:
                raise ValueError(
                    f'verdict field {field_name} does not match the '
                    'derivation from bound evidence'
                )
        for value in (self.legacy_holdout_rms_db, self.legacy_max_holdout_rms_db):
            if value is not None and not isfinite(float(value)):
                raise ValueError('legacy RMS values must be finite')
        if self.verdict_sha256 != _hash(self.identity_payload()):
            raise ValueError('uncertainty validation verdict hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'verdict_id', 'verdict_sha256'}
        )

    @classmethod
    def create(cls, **payload: Any) -> 'UncertaintyValidationVerdict':
        return _seal(cls, payload, 'verdict_id', 'verdict_sha256', 'uvv')


def _derive_verdict_payload(
    record: 'UncertaintyValidationVerdict',
) -> dict[str, Any]:
    """Recompute the verdict's derived fields from bound snapshots.

    Used by both the builder and the sealed validator — the verdict is a
    pure function of its inputs, so a stored verdict cannot disagree with
    its evidence without breaking the seal.
    """
    reasons: list[str] = []
    holdout = [
        binding for binding in record.evaluation_bindings
        if binding.split == 'holdout'
    ]
    calibration = [
        binding for binding in record.evaluation_bindings
        if binding.split == 'calibration'
    ]

    # Calibration is reported, never mixed into the holdout judgment.
    if not calibration:
        calibration_state: CalibrationEvidenceState = 'no_calibration_evidence'
        reasons.append('independent calibration evidence is required')
    elif any(
        binding.summary_verdict
        in ('discrepancy_significant', 'model_form_discrepancy_required')
        for binding in calibration
    ):
        calibration_state = 'calibration_discrepancy'
    else:
        calibration_state = 'calibration_consistent'

    missing_observables = sorted(
        oid
        for oid in record.protocol_observable_ids
        if oid not in {binding.observable_id for binding in holdout}
    )
    if not holdout:
        absolute: AbsoluteValidationVerdict = 'insufficient_evidence'
        reasons.append('independent holdout evidence is required')
    else:
        by_observable: dict[str, list[EvaluationBinding]] = {}
        for binding in holdout:
            by_observable.setdefault(binding.observable_id, []).append(binding)
        verdicts = {
            binding.summary_verdict for binding in holdout
        }
        for observable_id, items in sorted(by_observable.items()):
            worst = sorted(
                {item.summary_verdict for item in items},
                key=_VERDICT_SEVERITY.index,
            )[0]
            reasons.append(
                f'holdout observable {observable_id} worst verdict is {worst}'
            )
        if 'model_form_discrepancy_required' in verdicts:
            absolute = 'model_form_discrepancy_required'
        elif 'discrepancy_significant' in verdicts:
            absolute = 'discrepancy_significant'
        elif 'insufficient_evidence' in verdicts:
            absolute = 'insufficient_evidence'
        elif 'band_coverage_incomplete' in verdicts:
            absolute = 'band_coverage_incomplete'
        elif verdicts == {'consistent_with_reference_within_uncertainty'}:
            absolute = 'consistent_with_reference_within_uncertainty'
        elif verdicts <= {
            'input_uncertainty_dominates',
            'numerical_uncertainty_dominates',
            'reference_too_uncertain',
            'consistent_with_reference_within_uncertainty',
        }:
            if 'input_uncertainty_dominates' in verdicts:
                absolute = 'input_uncertainty_dominates'
            elif 'numerical_uncertainty_dominates' in verdicts:
                absolute = 'numerical_uncertainty_dominates'
            elif 'reference_too_uncertain' in verdicts:
                absolute = 'reference_too_uncertain'
            else:
                absolute = 'consistent_with_reference_within_uncertainty'
        else:
            absolute = 'insufficient_evidence'
        if missing_observables:
            reasons.append(
                'declared protocol observables lack holdout evidence: '
                + ', '.join(missing_observables)
            )
            # §2 — every declared observable must be explicitly evaluated;
            # incomplete coverage never reads as agreement, though it can
            # never hide a harder finding already reported
            if absolute not in (
                'model_form_discrepancy_required',
                'discrepancy_significant',
            ):
                absolute = 'insufficient_evidence'

    if record.validation_ref is None:
        ranking: RankingVerdict = 'ranking_not_evaluated'
    elif not record.legacy_trend_gates and not record.legacy_separation_gates:
        ranking = 'ranking_not_evaluated'
        reasons.append(
            'bound validation record carries no trend/separation evidence — '
            'ranking cannot be judged'
        )
    elif (
        any(gate != 'pass' for gate in record.legacy_trend_gates)
        or any(gate != 'pass' for gate in record.legacy_separation_gates)
    ):
        ranking = 'ranking_contradicted'
    else:
        ranking = 'ranking_supported'

    if record.legacy_residual_gate is not None:
        reasons.append(
            'legacy max_holdout_rms_db gate is one recorded metric — it '
            'can never alone produce a validation verdict'
        )

    counts: dict[str, int] = {}
    for binding in record.evaluation_bindings:
        counts[binding.dominant_uncertainty_category] = counts.get(
            binding.dominant_uncertainty_category, 0
        ) + 1
    counts.pop('unknown', None)
    if counts:
        best = max(counts.values())
        dominant = next(
            category
            for category in _CATEGORY_PRECEDENCE
            if counts.get(category) == best
        )
    else:
        dominant = 'unknown'

    suspected = any(
        binding.summary_verdict
        in ('discrepancy_significant', 'model_form_discrepancy_required')
        for binding in record.evaluation_bindings
    )

    return {
        'absolute_verdict': absolute,
        'ranking_verdict': ranking,
        'calibration_state': calibration_state,
        'dominant_uncertainty_category': dominant,
        'model_form_discrepancy_suspected': suspected,
        'gate_reasons': tuple(reasons),
    }


#: Worst-first ordering used to summarize per-observable verdicts.
_VERDICT_SEVERITY: tuple[str, ...] = (
    'model_form_discrepancy_required',
    'discrepancy_significant',
    'insufficient_evidence',
    'band_coverage_incomplete',
    'input_uncertainty_dominates',
    'numerical_uncertainty_dominates',
    'reference_too_uncertain',
    'consistent_with_reference_within_uncertainty',
)


def build_validation_uncertainty_verdict(
    *,
    document_id: str,
    protocol: ValidationUncertaintyProtocol,
    evaluations: tuple[ObservableUncertaintyEvaluation, ...],
    validation_record: CadModelValidationRecord | None = None,
    evidence_scope: EvidenceScope = 'synthetic_fixture',
    campaign_id: str | None = None,
    campaign_sha256: str | None = None,
    campaign_registration_id: str | None = None,
    campaign_registration_sha256: str | None = None,
    created_at_utc: str | None = None,
) -> UncertaintyValidationVerdict:
    """Aggregate bound observable evaluations into the study verdict.

    Every bound evaluation must have been produced under ``protocol``; a
    bound legacy record contributes only its snapshot metrics — the
    record's scalar RMS gate is preserved as evidence and can never drive
    the verdict by itself.
    """
    protocol_observable_ids = tuple(
        metric.observable_id for metric in protocol.observable_metrics
    )
    bindings: list[EvaluationBinding] = []
    for evaluation in evaluations:
        if (
            evaluation.protocol_ref.ref_sha256 != protocol.protocol_sha256
            or evaluation.protocol_ref.ref_id != protocol.protocol_id
        ):
            raise ValueError(
                'an evaluation bound to a different protocol cannot join '
                'this verdict'
            )
        bindings.append(
            EvaluationBinding(
                evaluation_ref=AuthorityRef(
                    kind='observable_uncertainty_evaluation',
                    ref_id=evaluation.evaluation_id,
                    ref_sha256=evaluation.evaluation_sha256,
                ),
                observable_id=evaluation.observable.observable_id,
                candidate_id=evaluation.candidate_id,
                split=evaluation.split,
                summary_verdict=evaluation.summary_verdict,
                dominant_uncertainty_category=(
                    evaluation.dominant_uncertainty_category
                ),
            )
        )

    validation_ref = None
    legacy_gate = None
    legacy_holdout = None
    legacy_max = None
    trend_gates: tuple[str, ...] = ()
    separation_gates: tuple[str, ...] = ()
    if validation_record is not None:
        validation_ref = AuthorityRef(
            kind='model_validation_record',
            ref_id=validation_record.validation_id,
            ref_sha256=validation_record.validation_sha256,
        )
        legacy_gate = validation_record.residual_gate
        legacy_holdout = validation_record.holdout_rms_db
        legacy_max = validation_record.max_holdout_rms_db
        trend_gates = tuple(
            check.gate for check in validation_record.trend_checks
        )
        separation_gates = tuple(
            check.gate for check in validation_record.separation_checks
        )
        record_splits = {
            (pair.candidate_id, pair.split) for pair in validation_record.pairs
        }
        for evaluation in evaluations:
            if (
                evaluation.candidate_id,
                evaluation.split,
            ) not in record_splits:
                raise ValueError(
                    'bound evaluation candidate/split is not part of the '
                    'bound validation record — evidence cannot migrate '
                    'across pairs'
                )

    provisional_fields = _derive_verdict_payload(
        UncertaintyValidationVerdict.model_construct(
            verdict_id='0' * 64,
            verdict_sha256='0' * 64,
            document_id=document_id,
            protocol_ref=AuthorityRef(
                kind='validation_uncertainty_protocol',
                ref_id=protocol.protocol_id,
                ref_sha256=protocol.protocol_sha256,
            ),
            protocol_version=protocol.protocol_version,
            protocol_observable_ids=protocol_observable_ids,
            evidence_scope=evidence_scope,
            campaign_id=campaign_id,
            campaign_sha256=campaign_sha256,
            campaign_registration_id=campaign_registration_id,
            campaign_registration_sha256=campaign_registration_sha256,
            validation_ref=validation_ref,
            legacy_residual_gate=legacy_gate,
            legacy_holdout_rms_db=legacy_holdout,
            legacy_max_holdout_rms_db=legacy_max,
            legacy_trend_gates=trend_gates,
            legacy_separation_gates=separation_gates,
            evaluation_bindings=tuple(bindings),
        )
    )
    return UncertaintyValidationVerdict.create(
        authority_version=VUQ_EVALUATOR_VERSION,
        document_id=document_id,
        protocol_ref=AuthorityRef(
            kind='validation_uncertainty_protocol',
            ref_id=protocol.protocol_id,
            ref_sha256=protocol.protocol_sha256,
        ),
        protocol_version=protocol.protocol_version,
        protocol_observable_ids=protocol_observable_ids,
        evidence_scope=evidence_scope,
        campaign_id=campaign_id,
        campaign_sha256=campaign_sha256,
        campaign_registration_id=campaign_registration_id,
        campaign_registration_sha256=campaign_registration_sha256,
        validation_ref=validation_ref,
        legacy_residual_gate=legacy_gate,
        legacy_holdout_rms_db=legacy_holdout,
        legacy_max_holdout_rms_db=legacy_max,
        legacy_trend_gates=trend_gates,
        legacy_separation_gates=separation_gates,
        evaluation_bindings=tuple(bindings),
        created_at_utc=created_at_utc or _utc_now(),
        **provisional_fields,
    )


# ---------------------------------------------------------------------------
# JA label registry (verdict display strings)
# ---------------------------------------------------------------------------

VUQ_LABELS: dict[str, str] = {
    'consistent_within_uncertainty': '不確かさ内で一致',
    'discrepancy_significant': '有意な残差',
    'below_evidence_resolution': '証拠分解能以下',
    'evidence_too_uncertain': '証拠不確かさ過大',
    'insufficient_uncertainty_information': '不確かさ情報不足',
    'consistent_with_reference_within_uncertainty': '不確かさ内で参照と一致',
    'model_form_discrepancy_required': 'モデル形式誤差の説明が必要',
    'reference_too_uncertain': '参照不確かさ過大',
    'input_uncertainty_dominates': '入力不確かさ支配',
    'numerical_uncertainty_dominates': '数値不確かさ支配',
    'band_coverage_incomplete': '帯域カバレッジ不完全',
    'insufficient_evidence': '証拠不足',
    'ranking_supported': '順位付け支持',
    'ranking_contradicted': '順位付け矛盾',
    'ranking_not_evaluated': '順位付け未評価',
    'calibration_consistent': '校正一致',
    'calibration_discrepancy': '校正不一致',
    'no_calibration_evidence': '校正証拠なし',
    'unknown': '不明',
}
