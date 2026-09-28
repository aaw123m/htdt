"""EQP10: spatially robust room-EQ design policy (#992).

A ``CorrectionDesignPolicy`` is an immutable authority declaring exactly how
multi-position measurements may be aggregated into a correction: the precise
measurement population, the declared correction bands, the evidence
aggregation method, smoothing/windowing, gain limits, regularization and
spatial-robustness rules. Aggregation produces a ``SpatialCorrectionEvidence``
record that classifies each band — persistent peak, local null, spatially
variable — so a local null is never silently boosted into a poor DSP
candidate and every decision is reproducible from the policy's hash.
"""

from __future__ import annotations

from math import isfinite, log2, log10
from statistics import pvariance
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_calibration import CadTargetCurve
from .cad_equipment import FrequencyDomain
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


EQP_SCHEMA_VERSION = 1
EQP_POLICY_AUTHORITY_VERSION = 'eqp10-correction-design-policy-1'
EQP_EVIDENCE_AUTHORITY_VERSION = 'eqp10-spatial-evidence-1'
EQP_AGGREGATION_VERSION = 'spatial-aggregation-v1'






SpatialAggregationMethod = Literal[
    'arithmetic_db_mean',
    'linear_power_mean',
    'worst_seat',
    'percentile_envelope',
    'mlp_weighted',
]

CorrectionSmoothing = Literal['none', 'fractional_octave']

CorrectionFormulation = Literal['peq', 'fir']

SpatialBandClassification = Literal[
    'spatially_persistent_error',
    'local_null',
    'spatially_variable',
    'insufficient_evidence',
]

HoldoutRule = Literal['none', 'spatial_positions']


class MeasurementPopulationRef(BaseModel):
    """Exact pin of the measurement population the policy consumes."""

    model_config = ConfigDict(frozen=True)

    population_id: str = Field(min_length=1)
    population_version: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sample_count: int = Field(gt=0)


class CorrectionRegularization(BaseModel):
    """Declared regularization; its fields are formulation-typed."""

    model_config = ConfigDict(frozen=True)

    formulation: CorrectionFormulation
    #: Residual-energy penalty strength applied by the optimizer.
    strength_db: float = Field(ge=0.0)
    frequency_dependence: Literal['none', 'bass_limited', 'tilt'] = 'none'
    gain_penalty_db: float | None = Field(default=None, ge=0.0)
    filter_energy_penalty: float | None = Field(default=None, ge=0.0)
    time_pre_ring_penalty: float | None = Field(default=None, ge=0.0)
    max_filters: int | None = Field(default=None, gt=0)
    max_q: float | None = Field(default=None, gt=0.0)
    max_taps: int | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def valid_formulation(self) -> 'CorrectionRegularization':
        peq_only = (self.max_filters, self.max_q)
        fir_only = (self.filter_energy_penalty, self.time_pre_ring_penalty, self.max_taps)
        if self.formulation == 'peq' and any(v is not None for v in fir_only):
            raise ValueError('peq regularization must not set fir-only fields')
        if self.formulation == 'fir' and any(v is not None for v in peq_only):
            raise ValueError('fir regularization must not set peq-only fields')
        return self


class SpatialRobustnessRule(BaseModel):
    """What 'spatially robust' means before a band may be boosted."""

    model_config = ConfigDict(frozen=True)

    #: A band counts as persistent when at least this fraction of positions
    #: shows a significant error of the same sign.
    persistence_fraction: float = Field(gt=0.0, le=1.0)
    #: |residual| at or above this level counts as significant at a position.
    error_significance_db: float = Field(gt=0.0)
    sign_consistency_required: bool = True
    #: Hard cap on positive correction gain for bands that are not
    #: spatially persistent (nulls/variable): never boosted blindly.
    local_null_max_boost_db: float = Field(ge=0.0)
    #: Cross-position spread above this flags the band spatially_variable.
    movement_sensitivity_threshold_db: float | None = Field(
        default=None, gt=0.0
    )


class CorrectionValidationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: HoldoutRule
    #: Positions withheld from aggregation for validation.
    holdout_position_ids: tuple[str, ...] = ()
    validation_algorithm_version: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_validation(self) -> 'CorrectionValidationPolicy':
        if self.rule == 'spatial_positions' and not self.holdout_position_ids:
            raise ValueError(
                'spatial_positions holdout requires held-out position ids'
            )
        if self.rule == 'none' and self.holdout_position_ids:
            raise ValueError('holdout positions require a holdout rule')
        return self


class CorrectionDesignPolicy(BaseModel):
    """Immutable spatial correction design authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = EQP_SCHEMA_VERSION
    authority_version: Literal[
        'eqp10-correction-design-policy-1'
    ] = EQP_POLICY_AUTHORITY_VERSION

    policy_id: str = Field(pattern=r'^correction-design-policy:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    measurement_population: MeasurementPopulationRef
    frequency_domain: FrequencyDomain
    #: Non-overlapping declared correction bands, strictly increasing.
    correction_bands: tuple[tuple[float, float], ...] = Field(min_length=1)
    aggregation: SpatialAggregationMethod
    #: Percentile used only by percentile_envelope.
    envelope_percentile: float | None = Field(default=None, gt=0.0, lt=100.0)
    #: Position weights required only by mlp_weighted (issue #513 lineage).
    mlp_position_weights: dict[str, float] | None = None
    smoothing: CorrectionSmoothing
    smoothing_fraction_octaves: float | None = Field(default=None, gt=0.0)

    seat_ids: tuple[str, ...] = Field(min_length=1)
    min_positions: int = Field(ge=1)

    max_boost_db: float = Field(ge=0.0)
    max_cut_db: float = Field(ge=0.0)
    regularization: CorrectionRegularization
    spatial_robustness: SpatialRobustnessRule
    #: Phase correction stays gated behind FIR authority (#978): EQP10 only
    #: declares whether it is permitted at all; no phase correction is
    #: synthesized here.
    phase_correction: Literal['none', 'linear_phase_gate'] = 'none'
    validation: CorrectionValidationPolicy
    device_max_boost_db: float | None = Field(default=None, ge=0.0)
    algorithm_id: str = Field(min_length=1)
    algorithm_version: str = Field(min_length=1)

    @field_validator('device_max_boost_db')
    @classmethod
    def finite_ceiling(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(float(value)):
            raise ValueError('device boost ceiling must be finite')
        return value

    @model_validator(mode='after')
    def valid_policy(self) -> 'CorrectionDesignPolicy':
        bands = tuple(
            (float(low), float(high)) for low, high in self.correction_bands
        )
        previous_high = 0.0
        for low, high in bands:
            if not (
                isfinite(low) and isfinite(high) and high > low > 0.0
            ):
                raise ValueError('correction bands must be finite and rising')
            if low < previous_high:
                raise ValueError('correction bands must not overlap')
            if not self.frequency_domain.contains(low) or not (
                self.frequency_domain.contains(high)
            ):
                raise ValueError(
                    'correction band outside the declared frequency domain'
                )
            previous_high = high
        if self.min_positions > len(self.seat_ids):
            raise ValueError('min_positions exceeds declared seat count')
        if self.aggregation == 'percentile_envelope' and (
            self.envelope_percentile is None
        ):
            raise ValueError('percentile_envelope requires envelope_percentile')
        if self.aggregation != 'percentile_envelope' and (
            self.envelope_percentile is not None
        ):
            raise ValueError('envelope_percentile only valid for percentile_envelope')
        if self.aggregation == 'mlp_weighted':
            weights = self.mlp_position_weights or {}
            missing = set(self.seat_ids) - set(weights)
            if missing or any(
                not isfinite(float(w)) or float(w) <= 0.0
                for w in weights.values()
            ):
                raise ValueError(
                    'mlp_weighted requires a positive weight per declared seat'
                )
        elif self.mlp_position_weights is not None:
            raise ValueError(
                'mlp_position_weights only valid for mlp_weighted'
            )
        if self.smoothing == 'fractional_octave' and (
            self.smoothing_fraction_octaves is None
        ):
            raise ValueError(
                'fractional_octave smoothing requires a declared fraction'
            )
        if self.smoothing == 'none' and self.smoothing_fraction_octaves is not None:
            raise ValueError('smoothing fraction requires smoothing enabled')
        if (
            self.validation.rule == 'spatial_positions'
            and set(self.validation.holdout_position_ids) - set(self.seat_ids)
        ):
            raise ValueError(
                'holdout positions must belong to the declared seat set'
            )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('correction design policy hash mismatch')
        if self.policy_id != f'correction-design-policy:{expected}':
            raise ValueError('correction design policy id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'policy_id', 'semantic_sha256'},
        )


def build_correction_design_policy(**kwargs: Any) -> CorrectionDesignPolicy:
    candidate = CorrectionDesignPolicy.model_construct(
        **kwargs,
        policy_id='correction-design-policy:' + '0' * 64,
        semantic_sha256='0' * 64,
    )
    digest = _digest(candidate.semantic_payload())
    return CorrectionDesignPolicy(
        **kwargs,
        policy_id=f'correction-design-policy:{digest}',
        semantic_sha256=digest,
    )


class PositionMagnitudeSample(BaseModel):
    """One position's measured magnitude on the shared frequency grid."""

    model_config = ConfigDict(frozen=True)

    position_id: str = Field(min_length=1)
    frequencies_hz: tuple[float, ...] = Field(min_length=1)
    magnitudes_db: tuple[float, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def valid_sample(self) -> 'PositionMagnitudeSample':
        if len(self.frequencies_hz) != len(self.magnitudes_db):
            raise ValueError('sample frequency/magnitude length mismatch')
        frequencies = tuple(float(f) for f in self.frequencies_hz)
        if any(
            not isfinite(f) or f <= 0.0 for f in frequencies
        ) or any(b <= a for a, b in zip(frequencies, frequencies[1:])):
            raise ValueError('sample frequencies must be increasing')
        if any(not isfinite(float(v)) for v in self.magnitudes_db):
            raise ValueError('sample magnitudes must be finite')
        return self


class SpatialCorrectionBand(BaseModel):
    """One correction band's persisted spatial evidence."""

    model_config = ConfigDict(frozen=True)

    low_hz: float = Field(gt=0.0)
    high_hz: float = Field(gt=0.0)
    positions_with_evidence: int = Field(ge=0)
    classification: SpatialBandClassification
    aggregated_error_db: float | None = None
    position_spread_db: float | None = Field(default=None, ge=0.0)
    sign_consistent: bool = False
    persistent_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Positive correction gain this band may receive under the policy.
    max_allowed_boost_db: float = Field(ge=0.0)
    max_allowed_cut_db: float = Field(ge=0.0)
    #: Null/variable bands hand the problem to geometry or absorption.
    recommend_geometry_review: bool = False
    headroom_feasible: bool | None = None


class SpatialCorrectionEvidence(BaseModel):
    """Persisted, replayable band evidence under one exact policy."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = EQP_SCHEMA_VERSION
    authority_version: Literal[
        'eqp10-spatial-evidence-1'
    ] = EQP_EVIDENCE_AUTHORITY_VERSION
    evidence_id: str = Field(pattern=r'^spatial-correction-evidence:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    policy_id: str = Field(pattern=r'^correction-design-policy:[0-9a-f]{64}$')
    policy_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    position_ids_used: tuple[str, ...]
    holdout_position_ids: tuple[str, ...] = ()
    aggregation_version: str = EQP_AGGREGATION_VERSION
    bands: tuple[SpatialCorrectionBand, ...]

    @model_validator(mode='after')
    def valid_evidence(self) -> 'SpatialCorrectionEvidence':
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('spatial evidence hash mismatch')
        if self.evidence_id != f'spatial-correction-evidence:{expected}':
            raise ValueError('spatial evidence id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'evidence_id', 'semantic_sha256'},
        )


def _band_samples(
    samples: Sequence[PositionMagnitudeSample],
    low_hz: float,
    high_hz: float,
) -> list[list[tuple[float, float]]]:
    """Per-position (frequency, magnitude) points inside one band."""
    collected: list[list[tuple[float, float]]] = []
    for sample in samples:
        points = [
            (float(f), float(v))
            for f, v in zip(sample.frequencies_hz, sample.magnitudes_db)
            if low_hz <= f <= high_hz
        ]
        collected.append(points)
    return collected


def _interpolate_db(
    points: Sequence[tuple[float, float]],
    frequency_hz: float,
) -> float | None:
    """Level-vs-frequency lookup, linear in log2 frequency.

    Same interpolation convention as the canonical frequency-response
    comparison authority (``linear_in_log2_frequency``) — a target curve
    is a function on a log-frequency axis, so linear-in-Hz interpolation
    would skew every mid-segment residual. Frequencies outside the
    declared point range clamp flat to the nearest endpoint level.
    """
    ordered = sorted(points)
    if not ordered:
        return None
    if frequency_hz <= ordered[0][0]:
        return ordered[0][1]
    if frequency_hz >= ordered[-1][0]:
        return ordered[-1][1]
    x = log2(frequency_hz)
    for (f_a, v_a), (f_b, v_b) in zip(ordered, ordered[1:]):
        if f_a <= frequency_hz <= f_b:
            fraction = (x - log2(f_a)) / (log2(f_b) - log2(f_a))
            return v_a + fraction * (v_b - v_a)
    return ordered[-1][1]


def _fractional_octave_smooth(
    frequencies: Sequence[float],
    magnitudes_db: Sequence[float],
    fraction_octaves: float,
) -> tuple[float, ...]:
    """Power-mean smoothing over a fractional-octave window.

    Same convention as the measurement-analysis smoothing authority
    (``fractional-octave-power-mean-1``): levels in a window combine as
    ``10*log10(mean(10^(L/10)))``, so deep narrow dips do not drag the
    smoothed level the way an arithmetic dB mean would. The window is
    ``center * 2^(+-fraction_octaves/2)`` — the same ±1/(2N)-octave span
    for ``fraction_octaves = 1/N``.
    """
    center_fraction = 2.0 ** (fraction_octaves / 2.0)
    smoothed: list[float] = []
    for center, _ in zip(frequencies, magnitudes_db):
        low = center / center_fraction
        high = center * center_fraction
        window = [
            float(v)
            for f, v in zip(frequencies, magnitudes_db)
            if low <= f <= high
        ]
        power = sum(10.0 ** (v / 10.0) for v in window) / len(window)
        smoothed.append(10.0 * log10(power))
    return tuple(smoothed)


def _aggregate_band(
    policy: CorrectionDesignPolicy,
    band_errors: dict[str, float],
) -> float | None:
    """Combine per-position band residuals per the declared method."""
    if not band_errors:
        return None
    values = list(band_errors.values())
    method = policy.aggregation
    if method == 'arithmetic_db_mean':
        return sum(values) / len(values)
    if method == 'linear_power_mean':
        return 10.0 * log10(
            sum(10.0 ** (v / 10.0) for v in values) / len(values)
        )
    if method == 'worst_seat':
        return max(values, key=lambda v: abs(v))
    if method == 'percentile_envelope':
        ordered = sorted(values)
        rank = policy.envelope_percentile / 100.0 * (len(ordered) - 1)
        low_index = int(rank)
        if low_index + 1 >= len(ordered):
            return ordered[-1]
        fraction = rank - low_index
        return ordered[low_index] + fraction * (
            ordered[low_index + 1] - ordered[low_index]
        )
    if method == 'mlp_weighted':
        weights = policy.mlp_position_weights or {}
        total = sum(float(weights[p]) for p in band_errors)
        return sum(
            float(weights[p]) * band_errors[p] for p in band_errors
        ) / total
    raise AssertionError(f'unknown aggregation {method}')


def aggregate_position_magnitudes(
    measurements: Sequence[PositionMagnitudeSample],
    target: CadTargetCurve,
    policy: CorrectionDesignPolicy,
) -> SpatialCorrectionEvidence:
    """Aggregate per-position residuals into persisted band evidence.

    Residuals are measured minus the exact target the policy pins. Held-out
    validation positions are excluded from aggregation but recorded.
    """
    holdout = set(policy.validation.holdout_position_ids)
    usable = [
        sample for sample in measurements if sample.position_id not in holdout
    ]
    eligible = [
        sample
        for sample in usable
        if sample.position_id in set(policy.seat_ids)
    ]
    target_points = tuple(
        (point.frequency_hz, point.level_db) for point in target.points
    )
    smoothed_measurements: list[PositionMagnitudeSample] = []
    for sample in eligible:
        if (
            policy.smoothing == 'fractional_octave'
            and policy.smoothing_fraction_octaves is not None
        ):
            magnitudes = _fractional_octave_smooth(
                tuple(float(f) for f in sample.frequencies_hz),
                tuple(float(v) for v in sample.magnitudes_db),
                float(policy.smoothing_fraction_octaves),
            )
            sample = PositionMagnitudeSample(
                position_id=sample.position_id,
                frequencies_hz=sample.frequencies_hz,
                magnitudes_db=magnitudes,
            )
        smoothed_measurements.append(sample)

    bands: list[SpatialCorrectionBand] = []
    for low_hz, high_hz in policy.correction_bands:
        per_position: dict[str, float] = {}
        for sample in smoothed_measurements:
            points = _band_samples((sample,), low_hz, high_hz)[0]
            if not points:
                continue
            residuals = [
                magnitude - (_interpolate_db(target_points, f) or 0.0)
                for f, magnitude in points
            ]
            per_position[sample.position_id] = sum(residuals) / len(residuals)

        positions_with_evidence = len(per_position)
        if positions_with_evidence < policy.min_positions or not per_position:
            bands.append(
                SpatialCorrectionBand(
                    low_hz=float(low_hz),
                    high_hz=float(high_hz),
                    positions_with_evidence=positions_with_evidence,
                    classification='insufficient_evidence',
                    max_allowed_boost_db=0.0,
                    max_allowed_cut_db=policy.max_cut_db,
                    headroom_feasible=None,
                )
            )
            continue

        aggregated = _aggregate_band(policy, per_position)
        errors = list(per_position.values())
        spread = (
            pvariance(errors) ** 0.5 if len(errors) > 1 else 0.0
        )
        significance = policy.spatial_robustness.error_significance_db
        significant = [v for v in errors if abs(v) >= significance]
        persistent_fraction = (
            len(significant) / len(errors) if errors else 0.0
        )
        signs = {v >= 0.0 for v in significant}
        sign_consistent = len(signs) <= 1 if significant else False
        all_negative = bool(significant) and all(v < 0.0 for v in significant)
        persistent = persistent_fraction >= (
            policy.spatial_robustness.persistence_fraction
        ) and (
            not policy.spatial_robustness.sign_consistency_required
            or sign_consistent
        )
        variable = (
            policy.spatial_robustness.movement_sensitivity_threshold_db
            is not None
            and spread
            > policy.spatial_robustness.movement_sensitivity_threshold_db
        )
        if not significant:
            # No position shows a significant error — nothing to correct.
            classification: SpatialBandClassification = (
                'insufficient_evidence'
            )
        elif variable:
            classification = 'spatially_variable'
        elif all_negative and not persistent:
            classification = 'local_null'
        elif persistent:
            classification = 'spatially_persistent_error'
        elif all_negative:
            classification = 'local_null'
        else:
            classification = 'spatially_variable'

        geometry_review = classification in (
            'local_null',
            'spatially_variable',
        )
        if classification == 'insufficient_evidence':
            max_boost = 0.0
        elif classification == 'spatially_persistent_error':
            max_boost = policy.max_boost_db
        else:
            max_boost = min(
                policy.max_boost_db,
                policy.spatial_robustness.local_null_max_boost_db,
            )
        # A persistent *positive* error is a peak: correction is a cut, so no
        # boost at all may be applied to that band.
        if aggregated is not None and aggregated > 0.0:
            max_boost = 0.0
        headroom = None
        if policy.device_max_boost_db is not None:
            headroom = max_boost <= policy.device_max_boost_db
        bands.append(
            SpatialCorrectionBand(
                low_hz=float(low_hz),
                high_hz=float(high_hz),
                positions_with_evidence=positions_with_evidence,
                classification=classification,
                aggregated_error_db=aggregated,
                position_spread_db=spread,
                sign_consistent=sign_consistent,
                persistent_fraction=persistent_fraction,
                max_allowed_boost_db=max_boost,
                max_allowed_cut_db=policy.max_cut_db,
                recommend_geometry_review=geometry_review,
                headroom_feasible=headroom,
            )
        )

    payload: dict[str, Any] = {
        'schema_version': EQP_SCHEMA_VERSION,
        'authority_version': EQP_EVIDENCE_AUTHORITY_VERSION,
        'policy_id': policy.policy_id,
        'policy_sha256': policy.semantic_sha256,
        'position_ids_used': sorted(s.position_id for s in eligible),
        'holdout_position_ids': sorted(holdout),
        'aggregation_version': EQP_AGGREGATION_VERSION,
        'bands': [band.model_dump(mode='json') for band in bands],
    }
    digest = _digest(payload)
    return SpatialCorrectionEvidence(
        policy_id=policy.policy_id,
        policy_sha256=policy.semantic_sha256,
        position_ids_used=tuple(sorted(s.position_id for s in eligible)),
        holdout_position_ids=tuple(sorted(holdout)),
        bands=tuple(bands),
        evidence_id=f'spatial-correction-evidence:{digest}',
        semantic_sha256=digest,
    )
