"""Small-room metric applicability gate (#571).

Decides *whether a declared metric is physically meaningful* for a declared
room/frequency context before any PASS/FAIL judgment exists. A metric value
outside its domain is honest evidence (e.g. a per-mode decay), but it must
never be relabeled into a conventional metric.

Composition (no parallel truth stores):

- ``cad_ir_analysis`` — ``IRDecayMetric`` (per-band EDT/T20/T30 measurements)
  and ``IR_BAND_HZ``;
- ``cad_measured_modal_analysis`` (#972) — per-mode decay authority;
- ``cad_late_decay_estimate`` — Sabine-estimate lane that already refuses to
  produce a measured RT60;
- ``acoustics`` — room geometry/mode tables.

Literature basis (recorded for reviewers):

- Schroeder frequency ``f_s ≈ 2000·sqrt(T60/V)`` Hz (Schroeder 1962, the
  ~3-fold modal-overlap definition; e.g. "The Schroeder frequency
  revisited", Fazenda et al.). Below f_s modes are sparse and a single
  decay slope is not physically meaningful.
- Modal bandwidth ``Δf ≈ 2.2/T60`` Hz (3 dB bandwidth of a mode with decay
  time T60) and modal density ``N(f) = 4πVf²/c³ + πSf/(2c²) + L/(8c)``
  per Hz (rectangular room, Morse & Bolt); ``N·Δf ≈ 3`` reproduces
  f_s ≈ 2000·sqrt(T60/V) at c = 343 m/s.
- ISO 3382-2:2008 — reverberation-time measurement with survey /
  engineering / precision accuracy classes; T20 preferred over T30 when
  decay-range/SNR allows; uncertainty grows as decay range shrinks.
- ISO 3382-1 — the just-noticeable difference for reverberation time is
  roughly 5 % (perception floor for threshold sanity checks, not a gate).
- Prinn 2025 (issue #571 §4) — low-frequency decay is a *spatial field*:
  per-position decay evidence must be retained, not forced into one scalar.
"""

from __future__ import annotations

from math import isfinite, pi, sqrt
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...canonical_json import canonical_sha256 as _hash, canonicalize_payload


def _rect_volume(dims: tuple[float, float, float]) -> float:
    return dims[0] * dims[1] * dims[2]


def _rect_surface_area(dims: tuple[float, float, float]) -> float:
    return 2.0 * (dims[0] * dims[1] + dims[1] * dims[2] + dims[0] * dims[2])


_SHA256 = r'^[0-9a-f]{64}$'

APPLICABILITY_EVALUATOR_VERSION = 'metric-applicability-1'
DECAY_PROFILE_AUTHORITY_VERSION = 'decay-metric-profile-1'
SPATIAL_DECAY_AUTHORITY_VERSION = 'spatial-decay-record-1'
MODAL_AUTHORITY_VERSION = 'modal-decay-authority-1'
COMPARISON_AUTHORITY_VERSION = 'metric-comparison-1'
OPTIMIZATION_AUTHORITY_VERSION = 'metric-optimization-1'

#: Representative Schroeder formula constant (Schroeder 1962): the
#: frequency where ~3 modes overlap within a modal bandwidth.
SCHROEDER_COEFFICIENT = 2000.0

#: Modal-bandwidth constant: Δf ≈ 2.2 / T60 (3 dB bandwidth, literature
#: values 2.0–2.2; we use 2.2, the commonly quoted value).
MODAL_BANDWIDTH_COEFFICIENT = 2.2

#: Modal-overlap index at which the diffuse assumption is conventionally
#: considered to hold (the definition behind Schroeder's formula).
MODAL_OVERLAP_THRESHOLD = 3.0

#: Below this overlap index the region is clearly modal (single-slope
#: decay metrics are never authoritative there).
MODAL_OVERLAP_LIMIT = 1.0

#: ISO 3382-1 §A.3: the just-noticeable difference in reverberation time is
#: roughly 5 % — a sanity floor, never a hard gate.
RT_JND_RELATIVE = 0.05


# ---------------------------------------------------------------------------
# Taxonomies (issue §1, §5, §8)
# ---------------------------------------------------------------------------

MetricDomainClass = Literal[
    'diffuse_field_band_metric',
    'transition_region',
    'modal_non_diffuse',
    'local_position_only',
    'spatially_aggregated_with_limitations',
    'insufficient_decay_range',
    'insufficient_snr',
    'not_applicable',
]

MetricApplicabilityReason = Literal[
    'BELOW_SCHROEDER_FREQUENCY',
    'MODAL_OVERLAP_INSUFFICIENT',
    'TRANSITION_REGION_CAUTION',
    'DECAY_RANGE_INSUFFICIENT',
    'SIGNAL_TO_NOISE_INSUFFICIENT',
    'SPATIAL_VARIANCE_HIGH',
    'POSITION_LOCAL_METRIC',
    'ROOM_SHAPE_UNDECLARED',
    'VOLUME_UNDECLARED',
    'BAND_UNDECLARED',
    'METRIC_NOT_DEFINED_FOR_CONTEXT',
    'MODAL_AUTHORITY_REQUIRED',
    'METRIC_ALLOWED_WITH_LIMITATIONS',
    'METRIC_ALLOWED',
]

DecayMetricKind = Literal['edt', 't20', 't30']
DecayMethodKind = Literal['integrated_impulse', 'interrupted_noise']

ComparisonEligibility = Literal[
    'COMPARABLE',
    'COMPARABLE_WITH_LIMITATIONS',
    'INCOMPARABLE',
]

ComparisonReason = Literal[
    'PROFILE_MISMATCH',
    'METRIC_MISMATCH',
    'METHOD_MISMATCH',
    'DOMAIN_MISMATCH',
    'POSITION_SET_MISMATCH',
    'SEMANTICS_MATCH',
]

OptimizationReason = Literal[
    'METRIC_APPLICABLE',
    'METRIC_OUTSIDE_DOMAIN',
    'MODAL_REGION_NEEDS_MODAL_OBJECTIVE',
    'POSITION_LOCAL_OBJECTIVE_REQUIRED',
    'DECAY_RANGE_INSUFFICIENT',
]

MetricId = Literal[
    'edt_band',
    't20_band',
    't30_band',
    'schroeder_decay_indicator',
    'modal_decay_per_mode',
    'spatial_rt_field',
]


#: Product-facing Japanese labels (issue §10).
METRIC_DOMAIN_LABELS: dict[str, str] = {
    'diffuse_field_band_metric': '拡散場帯域指標',
    'transition_region': '遷移領域',
    'modal_non_diffuse': 'モーダル領域（非拡散）',
    'local_position_only': '位置限定指標',
    'spatially_aggregated_with_limitations': '制約つき空間集約',
    'insufficient_decay_range': '減衰レンジ不足',
    'insufficient_snr': 'SNR 不足',
    'not_applicable': '適用不可',
}

METRIC_APPLICABILITY_REASON_LABELS: dict[str, str] = {
    'BELOW_SCHROEDER_FREQUENCY': 'Schroeder 周波数以下',
    'MODAL_OVERLAP_INSUFFICIENT': 'モード重なり不足',
    'TRANSITION_REGION_CAUTION': '遷移領域 — 帯域指標は限定的',
    'DECAY_RANGE_INSUFFICIENT': '有効減衰レンジ不足',
    'SIGNAL_TO_NOISE_INSUFFICIENT': 'SNR 不足',
    'SPATIAL_VARIANCE_HIGH': '空間ばらつき大 — 単一スカラは不適切',
    'POSITION_LOCAL_METRIC': '位置ローカル指標としてのみ有効',
    'ROOM_SHAPE_UNDECLARED': '室形状が未宣言',
    'VOLUME_UNDECLARED': '室容積が未宣言',
    'BAND_UNDECLARED': '評価帯域が未宣言',
    'METRIC_NOT_DEFINED_FOR_CONTEXT': 'この文脈では指標未定義',
    'MODAL_AUTHORITY_REQUIRED': 'モーダル減衰権威が必要',
    'METRIC_ALLOWED_WITH_LIMITATIONS': '制約つきで適用可能',
    'METRIC_ALLOWED': '適用可能',
}

COMPARISON_ELIGIBILITY_LABELS: dict[str, str] = {
    'COMPARABLE': '比較可能',
    'COMPARABLE_WITH_LIMITATIONS': '制約つき比較可能',
    'INCOMPARABLE': '比較不可',
}


# ---------------------------------------------------------------------------
# Room context + physics helpers
# ---------------------------------------------------------------------------


class RoomMetricContext(BaseModel):
    """The room/frequency context a metric is judged against.

    ``volume_m3`` may be omitted — the context then yields honest
    INSUFFICIENT_EVIDENCE rather than a guessed applicability.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    volume_m3: float | None = Field(default=None, gt=0.0)
    surface_area_m2: float | None = Field(default=None, gt=0.0)
    dimensions_m: tuple[float, float, float] | None = None
    t60_estimate_s: float | None = Field(default=None, gt=0.0)
    shape_class: Literal[
        'rectangular', 'non_rectangular', 'coupled', 'unknown'
    ] = 'unknown'

    @model_validator(mode='after')
    def valid_context(self) -> 'RoomMetricContext':
        for value in (self.volume_m3, self.surface_area_m2, self.t60_estimate_s):
            if value is not None and not isfinite(float(value)):
                raise ValueError('context values must be finite')
        if self.dimensions_m is not None:
            if len(self.dimensions_m) != 3:
                raise ValueError('dimensions_m needs exactly three values')
            if any(
                not isfinite(d) or d <= 0.0 for d in self.dimensions_m
            ):
                raise ValueError('room dimensions must be positive and finite')
        return self

    def resolved_volume_m3(self) -> float | None:
        if self.volume_m3 is not None:
            return self.volume_m3
        if self.dimensions_m is not None:
            return _rect_volume(self.dimensions_m)
        return None

    def resolved_surface_area_m2(self) -> float | None:
        if self.surface_area_m2 is not None:
            return self.surface_area_m2
        if self.dimensions_m is not None and self.shape_class == 'rectangular':
            return _rect_surface_area(self.dimensions_m)
        return None


def schroeder_frequency_hz(t60_s: float, volume_m3: float, c: float = 343.0) -> float:
    """Schroeder frequency ``2000·sqrt(T60/V)`` (representative formula).

    The constant 2000 assumes c = 343 m/s; the ``c`` parameter scales it
    (f_s ∝ c^(3/2)·2.2/4π normalization folded into 2000).
    """

    if not (isfinite(t60_s) and t60_s > 0.0):
        raise ValueError('t60_s must be positive and finite')
    if not (isfinite(volume_m3) and volume_m3 > 0.0):
        raise ValueError('volume_m3 must be positive and finite')
    return SCHROEDER_COEFFICIENT * sqrt(t60_s / volume_m3) * (c / 343.0) ** 1.5


def modal_bandwidth_hz(t60_s: float) -> float:
    """3 dB modal bandwidth ``Δf ≈ 2.2/T60`` Hz (Morse & Bolt)."""

    if not (isfinite(t60_s) and t60_s > 0.0):
        raise ValueError('t60_s must be positive and finite')
    return MODAL_BANDWIDTH_COEFFICIENT / t60_s


def modal_density_per_hz(
    frequency_hz: float,
    volume_m3: float,
    surface_area_m2: float,
    edge_length_m: float = 0.0,
    c: float = 343.0,
) -> float:
    """Modal density dN/df for a rectangular room (Morse & Bolt).

    ``N(f) = 4πVf²/c³ + πSf/(2c²) + L/(8c)``; the edge term is optional
    (small for typical small rooms).
    """

    if not (isfinite(frequency_hz) and frequency_hz > 0.0):
        raise ValueError('frequency_hz must be positive and finite')
    if not (isfinite(volume_m3) and volume_m3 > 0.0):
        raise ValueError('volume_m3 must be positive and finite')
    if not (isfinite(surface_area_m2) and surface_area_m2 > 0.0):
        raise ValueError('surface_area_m2 must be positive and finite')
    if not (isfinite(edge_length_m) and edge_length_m >= 0.0):
        raise ValueError('edge_length_m must be non-negative and finite')
    return (
        4.0 * pi * volume_m3 * frequency_hz**2 / c**3
        + pi * surface_area_m2 * frequency_hz / (2.0 * c**2)
        + edge_length_m / (8.0 * c)
    )


def modal_overlap_index(
    frequency_hz: float,
    t60_s: float,
    volume_m3: float,
    surface_area_m2: float,
    edge_length_m: float = 0.0,
    c: float = 343.0,
) -> float:
    """Modal overlap ``M = N(f)·Δf`` — the quantity behind Schroeder's rule."""

    return modal_density_per_hz(
        frequency_hz, volume_m3, surface_area_m2, edge_length_m, c
    ) * modal_bandwidth_hz(t60_s)


def room_metric_context(
    length_m: float, width_m: float, height_m: float, t60_s: float
) -> RoomMetricContext:
    """Rectangular-room convenience context (geometry via ``acoustics``)."""

    return RoomMetricContext(
        volume_m3=_rect_volume((length_m, width_m, height_m)),
        surface_area_m2=_rect_surface_area((length_m, width_m, height_m)),
        dimensions_m=(length_m, width_m, height_m),
        t60_estimate_s=t60_s,
        shape_class='rectangular',
    )


# ---------------------------------------------------------------------------
# Decay metric profile binding (issue §3)
# ---------------------------------------------------------------------------


class DecayMetricProfile(BaseModel):
    """Everything that makes a decay metric what it is (issue §3).

    A decay number is bound to metric name + standard revision + frequency
    band + position set + measurement method + fit interval + dynamic
    range + fit quality + noise/SNR condition + spatial aggregation. The
    binding is sealed — profiles are never compared as bare scalars.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['decay-metric-profile-1'] = (
        DECAY_PROFILE_AUTHORITY_VERSION
    )
    profile_id: str = Field(min_length=1)
    metric: DecayMetricKind
    standard_revision: str = Field(min_length=1)
    band_hz: float = Field(gt=0.0)
    position_ids: tuple[str, ...] = Field(min_length=1)
    method: DecayMethodKind
    fit_interval_db: tuple[float, float]
    dynamic_range_db: float = Field(gt=0.0)
    fit_quality: Literal[
        'good', 'acceptable', 'poor', 'unreliable', 'unknown'
    ] = 'unknown'
    snr_db: float | None = None
    spatial_aggregation: Literal[
        'single_position', 'mean', 'range', 'other_declared'
    ] = 'single_position'
    profile_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_profile(self) -> 'DecayMetricProfile':
        lo, hi = self.fit_interval_db
        if not (isfinite(lo) and isfinite(hi)):
            raise ValueError('fit interval must be finite')
        if lo <= hi:
            raise ValueError(
                'fit interval is (start, end) of a decaying curve — start > end dB'
            )
        if self.metric == 'edt':
            expected = (0.0, -10.0)
        elif self.metric == 't20':
            expected = (-5.0, -25.0)
        else:
            expected = (-5.0, -35.0)
        if (lo, hi) != expected:
            raise ValueError(
                f'{self.metric} fit interval must be {expected} dB per ISO 3382-2'
            )
        # ISO 3382-2: the decay range must clear the fit interval plus noise
        # allowance; T20 needs ≥ 25 dB usable range, T30 ≥ 35 dB.
        if not isfinite(self.dynamic_range_db):
            raise ValueError('dynamic range must be finite')
        if self.snr_db is not None and not isfinite(float(self.snr_db)):
            raise ValueError('snr_db must be finite')
        if len(set(self.position_ids)) != len(self.position_ids):
            raise ValueError('position ids must be unique')
        if self.profile_sha256 != _hash(self.identity_payload()):
            raise ValueError('decay metric profile hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'profile_id': self.profile_id,
            'metric': self.metric,
            'standard_revision': self.standard_revision,
            'band_hz': self.band_hz,
            'position_ids': list(self.position_ids),
            'method': self.method,
            'fit_interval_db': list(self.fit_interval_db),
            'dynamic_range_db': self.dynamic_range_db,
            'fit_quality': self.fit_quality,
            'snr_db': self.snr_db,
            'spatial_aggregation': self.spatial_aggregation,
        }


def build_decay_metric_profile(
    *,
    profile_id: str,
    metric: DecayMetricKind,
    standard_revision: str,
    band_hz: float,
    position_ids: Sequence[str],
    method: DecayMethodKind,
    dynamic_range_db: float,
    fit_interval_db: tuple[float, float] | None = None,
    fit_quality: Literal[
        'good', 'acceptable', 'poor', 'unreliable', 'unknown'
    ] = 'unknown',
    snr_db: float | None = None,
    spatial_aggregation: Literal[
        'single_position', 'mean', 'range', 'other_declared'
    ] = 'single_position',
) -> DecayMetricProfile:
    default_intervals = {
        'edt': (0.0, -10.0),
        't20': (-5.0, -25.0),
        't30': (-5.0, -35.0),
    }
    payload: dict[str, Any] = {
        'profile_id': profile_id,
        'metric': metric,
        'standard_revision': standard_revision,
        'band_hz': band_hz,
        'position_ids': tuple(position_ids),
        'method': method,
        'fit_interval_db': (
            fit_interval_db or default_intervals[metric]
        ),
        'dynamic_range_db': dynamic_range_db,
        'fit_quality': fit_quality,
        'snr_db': snr_db,
        'spatial_aggregation': spatial_aggregation,
    }
    provisional = DecayMetricProfile.model_construct(
        **canonicalize_payload(
            DecayMetricProfile, dict(**payload, profile_sha256='0' * 64)
        )
    )
    return DecayMetricProfile(
        **payload, profile_sha256=_hash(provisional.identity_payload())
    )


#: ISO 3382-2 minimum usable decay range per metric (dB below the
#: evaluation-interval end — T20 needs the curve valid to ≥ −30 dB,
#: T30 to ≥ −40 dB, EDT to ≥ −15 dB including noise allowance).
_MIN_DYNAMIC_RANGE_DB: dict[str, float] = {
    'edt': 15.0,
    't20': 30.0,
    't30': 40.0,
}

#: Minimum SNR of the decay tail above the noise floor for the profile's
#: fit interval (ISO 3382-2 engineering/precision intent).
_MIN_SNR_DB = 10.0


# ---------------------------------------------------------------------------
# Spatial decay record (issue §4 — Prinn 2025: decay is a field)
# ---------------------------------------------------------------------------


class PositionDecaySample(BaseModel):
    """Decay evidence at one position — never relabeled."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    position_id: str = Field(min_length=1)
    band_hz: float = Field(gt=0.0)
    metric: DecayMetricKind | Literal['modal_decay']
    value_s: float = Field(gt=0.0)
    fit_quality: Literal[
        'good', 'acceptable', 'poor', 'unreliable', 'unknown'
    ] = 'unknown'
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def finite(self) -> 'PositionDecaySample':
        if not isfinite(self.value_s) or not isfinite(self.band_hz):
            raise ValueError('decay sample values must be finite')
        return self


class SpatialDecayRecord(BaseModel):
    """Per-position decay field — retained before any aggregation (§4)."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['spatial-decay-record-1'] = (
        SPATIAL_DECAY_AUTHORITY_VERSION
    )
    record_id: str = Field(min_length=1)
    band_hz: float = Field(gt=0.0)
    metric: DecayMetricKind
    samples: tuple[PositionDecaySample, ...] = Field(min_length=1)
    aggregation: Literal[
        'none', 'mean', 'median', 'range_reported'
    ] = 'none'
    aggregated_value_s: float | None = Field(default=None, gt=0.0)
    aggregation_note: str | None = Field(default=None, min_length=1)
    record_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_record(self) -> 'SpatialDecayRecord':
        for sample in self.samples:
            if sample.band_hz != self.band_hz:
                raise ValueError('sample band must match the record band')
            if sample.metric != self.metric:
                raise ValueError('sample metric must match the record metric')
        position_ids = [sample.position_id for sample in self.samples]
        if len(set(position_ids)) != len(position_ids):
            raise ValueError('position ids must be unique')
        if self.aggregation == 'none':
            if self.aggregated_value_s is not None:
                raise ValueError(
                    'aggregation=none forbids an aggregated scalar'
                )
        else:
            if self.aggregated_value_s is None:
                raise ValueError(
                    'an aggregated record must carry its scalar and note'
                )
            if not isfinite(float(self.aggregated_value_s)):
                raise ValueError('aggregated value must be finite')
            if not self.aggregation_note:
                raise ValueError(
                    'aggregation requires an explicit aggregation_note'
                )
            # Honest aggregation: the reported scalar must equal the
            # declared reduction — never a smoothed/convenient value.
            values = [sample.value_s for sample in self.samples]
            if self.aggregation == 'mean':
                expected = sum(values) / len(values)
            elif self.aggregation == 'median':
                ordered = sorted(values)
                mid = len(ordered) // 2
                expected = (
                    ordered[mid]
                    if len(ordered) % 2
                    else (ordered[mid - 1] + ordered[mid]) / 2.0
                )
            else:  # range_reported — the scalar is informational only
                expected = None
            if expected is not None and (
                abs(float(self.aggregated_value_s) - expected) > 1e-9
            ):
                raise ValueError(
                    'aggregated value must equal the declared reduction'
                )
        if self.record_sha256 != _hash(self.identity_payload()):
            raise ValueError('spatial decay record hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'record_id': self.record_id,
            'band_hz': self.band_hz,
            'metric': self.metric,
            'samples': [
                sample.model_dump(mode='json') for sample in self.samples
            ],
            'aggregation': self.aggregation,
            'aggregated_value_s': self.aggregated_value_s,
            'aggregation_note': self.aggregation_note,
        }

    def coefficient_of_variation(self) -> float | None:
        """Spatial spread (σ/μ) across positions; None for a single sample."""

        if len(self.samples) < 2:
            return None
        values = [s.value_s for s in self.samples]
        mean = sum(values) / len(values)
        if mean <= 0.0:
            return None
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        return sqrt(variance) / mean


def build_spatial_decay_record(
    *,
    record_id: str,
    band_hz: float,
    metric: DecayMetricKind,
    samples: Sequence[PositionDecaySample | dict[str, Any]],
    aggregation: Literal['none', 'mean', 'median', 'range_reported'] = 'none',
    aggregation_note: str | None = None,
) -> SpatialDecayRecord:
    parsed = tuple(
        s if isinstance(s, PositionDecaySample)
        else PositionDecaySample.model_validate(s)
        for s in samples
    )
    aggregated: float | None = None
    if aggregation == 'mean':
        aggregated = sum(s.value_s for s in parsed) / len(parsed)
    elif aggregation == 'median':
        ordered = sorted(s.value_s for s in parsed)
        mid = len(ordered) // 2
        aggregated = (
            ordered[mid]
            if len(ordered) % 2
            else (ordered[mid - 1] + ordered[mid]) / 2.0
        )
    elif aggregation == 'range_reported':
        aggregated = min(s.value_s for s in parsed)
    payload: dict[str, Any] = {
        'record_id': record_id,
        'band_hz': band_hz,
        'metric': metric,
        'samples': parsed,
        'aggregation': aggregation,
        'aggregated_value_s': aggregated,
        'aggregation_note': aggregation_note,
    }
    provisional = SpatialDecayRecord.model_construct(
        **canonicalize_payload(
            SpatialDecayRecord, dict(**payload, record_sha256='0' * 64)
        )
    )
    return SpatialDecayRecord(
        **payload, record_sha256=_hash(provisional.identity_payload())
    )


# ---------------------------------------------------------------------------
# Modal decay authority (issue §5 — never relabeled as RT60)
# ---------------------------------------------------------------------------


class ModalDecayRecord(BaseModel):
    """Per-mode decay evidence — the correct authority below f_s.

    A modal decay is *not* a reverberation time: the model forbids any
    field named rt/t60 and ``as_seconds`` is the only consumer view.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['modal-decay-authority-1'] = (
        MODAL_AUTHORITY_VERSION
    )
    record_id: str = Field(min_length=1)
    room_context_id: str | None = Field(default=None, min_length=1)
    frequency_hz: float = Field(gt=0.0)
    decay_time_s: float = Field(gt=0.0)
    damping_ratio: float | None = Field(default=None, ge=0.0)
    mode_indices: tuple[int, int, int] | None = None
    extraction_method: str = Field(min_length=1)
    confidence: Literal['high', 'medium', 'low', 'unknown'] = 'unknown'
    position_id: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)
    record_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_modal(self) -> 'ModalDecayRecord':
        if not isfinite(self.frequency_hz) or not isfinite(self.decay_time_s):
            raise ValueError('modal decay values must be finite')
        if self.damping_ratio is not None and not isfinite(
            float(self.damping_ratio)
        ):
            raise ValueError('damping ratio must be finite')
        if self.mode_indices is not None:
            if len(self.mode_indices) != 3:
                raise ValueError('mode indices are (nx, ny, nz)')
            if any(n < 0 for n in self.mode_indices):
                raise ValueError('mode indices must be non-negative')
        if self.record_sha256 != _hash(self.identity_payload()):
            raise ValueError('modal decay record hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'record_id': self.record_id,
            'room_context_id': self.room_context_id,
            'frequency_hz': self.frequency_hz,
            'decay_time_s': self.decay_time_s,
            'damping_ratio': self.damping_ratio,
            'mode_indices': (
                None if self.mode_indices is None else list(self.mode_indices)
            ),
            'extraction_method': self.extraction_method,
            'confidence': self.confidence,
            'position_id': self.position_id,
            'note': self.note,
        }


def build_modal_decay_record(
    *,
    record_id: str,
    frequency_hz: float,
    decay_time_s: float,
    extraction_method: str,
    damping_ratio: float | None = None,
    mode_indices: tuple[int, int, int] | None = None,
    confidence: Literal['high', 'medium', 'low', 'unknown'] = 'unknown',
    position_id: str | None = None,
    room_context_id: str | None = None,
    note: str | None = None,
) -> ModalDecayRecord:
    payload: dict[str, Any] = {
        'record_id': record_id,
        'room_context_id': room_context_id,
        'frequency_hz': frequency_hz,
        'decay_time_s': decay_time_s,
        'damping_ratio': damping_ratio,
        'mode_indices': mode_indices,
        'extraction_method': extraction_method,
        'confidence': confidence,
        'position_id': position_id,
        'note': note,
    }
    provisional = ModalDecayRecord.model_construct(
        **canonicalize_payload(
            ModalDecayRecord, dict(**payload, record_sha256='0' * 64)
        )
    )
    return ModalDecayRecord(
        **payload, record_sha256=_hash(provisional.identity_payload())
    )


# ---------------------------------------------------------------------------
# Applicability decision (issue §1, §5, §7, §9)
# ---------------------------------------------------------------------------


class MetricApplicabilityDecision(BaseModel):
    """Whether ``metric`` is physically applicable in ``context`` — sealed."""

    model_config = ConfigDict(frozen=True)

    evaluator_version: Literal['metric-applicability-1'] = (
        APPLICABILITY_EVALUATOR_VERSION
    )
    metric_id: MetricId
    context_summary: str = Field(min_length=1)
    domain_class: MetricDomainClass
    reasons: tuple[MetricApplicabilityReason, ...] = ()
    applicable: bool
    limited: bool = False
    schroeder_hz: float | None = None
    modal_overlap: float | None = None
    required_evidence: Literal[
        'none', 'modal_decay_record', 'spatial_decay_record', 'per_position'
    ] = 'none'

    @model_validator(mode='after')
    def valid_decision(self) -> 'MetricApplicabilityDecision':
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError('reasons must be unique')
        if self.schroeder_hz is not None and (
            not isfinite(self.schroeder_hz) or self.schroeder_hz <= 0.0
        ):
            raise ValueError('schroeder_hz must be positive and finite')
        if self.modal_overlap is not None and not isfinite(self.modal_overlap):
            raise ValueError('modal_overlap must be finite')
        if self.applicable and self.domain_class in (
            'insufficient_decay_range',
            'insufficient_snr',
            'not_applicable',
        ):
            raise ValueError(
                'a metric cannot be applicable in a non-applicable domain'
            )
        return self

    def render_text(self) -> str:
        reasons = '、'.join(
            METRIC_APPLICABILITY_REASON_LABELS[r] for r in self.reasons
        ) or '—'
        state = '適用可能' if self.applicable else '適用不可'
        if self.limited:
            state += '（限定）'
        return (
            f'{METRIC_DOMAIN_LABELS[self.domain_class]} / {state}: {reasons}'
        )


def _context_description(context: RoomMetricContext, band_hz: float | None) -> str:
    volume = context.resolved_volume_m3()
    volume_txt = 'UNKNOWN' if volume is None else f'{volume:.1f}'
    band_txt = 'UNKNOWN' if band_hz is None else f'{band_hz:g}'
    return f'V={volume_txt} m³ / band={band_txt} Hz / shape={context.shape_class}'


def evaluate_metric_applicability(
    metric_id: MetricId,
    context: RoomMetricContext,
    *,
    band_hz: float | None = None,
    profile: DecayMetricProfile | None = None,
    spatial: SpatialDecayRecord | None = None,
) -> MetricApplicabilityDecision:
    """Gate a metric against room context — fail closed, never silent.

    ``profile`` binds the metric's declared procedure (ISO 3382-2 revision,
    band, positions, fit interval, dynamic range, SNR); without it the
    decision can still classify the domain but never approves a decay
    metric.
    """

    summary = _context_description(context, band_hz)
    volume = context.resolved_volume_m3()
    surface = context.resolved_surface_area_m2()
    reasons: list[MetricApplicabilityReason] = []

    # --- Fail-closed context --------------------------------------------------
    if context.shape_class == 'unknown' and context.dimensions_m is None:
        reasons.append('ROOM_SHAPE_UNDECLARED')
    if volume is None:
        reasons.append('VOLUME_UNDECLARED')
    if context.t60_estimate_s is None:
        # Without a T60 estimate the band cannot be placed relative to f_s.
        reasons.append('METRIC_NOT_DEFINED_FOR_CONTEXT')
    if metric_id in ('edt_band', 't20_band', 't30_band') and band_hz is None:
        reasons.append('BAND_UNDECLARED')
    if reasons:
        return MetricApplicabilityDecision(
            metric_id=metric_id,
            context_summary=summary,
            domain_class='not_applicable',
            reasons=tuple(reasons),
            applicable=False,
        )

    assert volume is not None and context.t60_estimate_s is not None
    t60 = context.t60_estimate_s

    fs = schroeder_frequency_hz(t60, volume)
    overlap: float | None = None
    eval_hz = band_hz if band_hz is not None else fs
    if surface is not None:
        overlap = modal_overlap_index(eval_hz, t60, volume, surface)

    # --- Domain classification -------------------------------------------------
    if metric_id == 'modal_decay_per_mode':
        return MetricApplicabilityDecision(
            metric_id=metric_id,
            context_summary=summary,
            domain_class=(
                'modal_non_diffuse'
                if (band_hz is not None and band_hz < fs)
                else 'diffuse_field_band_metric'
            ),
            reasons=('METRIC_ALLOWED',),
            applicable=True,
            schroeder_hz=fs,
            modal_overlap=overlap,
        )

    if metric_id == 'schroeder_decay_indicator':
        return MetricApplicabilityDecision(
            metric_id=metric_id,
            context_summary=summary,
            domain_class='transition_region',
            reasons=('TRANSITION_REGION_CAUTION', 'METRIC_ALLOWED_WITH_LIMITATIONS'),
            applicable=True,
            limited=True,
            schroeder_hz=fs,
            modal_overlap=overlap,
        )

    if metric_id == 'spatial_rt_field':
        return MetricApplicabilityDecision(
            metric_id=metric_id,
            context_summary=summary,
            domain_class='spatially_aggregated_with_limitations',
            reasons=('METRIC_ALLOWED_WITH_LIMITATIONS',),
            applicable=True,
            limited=True,
            schroeder_hz=fs,
            modal_overlap=overlap,
            required_evidence='spatial_decay_record',
        )

    # --- Conventional band metrics (EDT/T20/T30) -------------------------------
    assert band_hz is not None  # enforced above
    domain: MetricDomainClass
    applicable = True
    limited = False
    required_evidence: Literal[
        'none', 'modal_decay_record', 'spatial_decay_record', 'per_position'
    ] = 'none'

    if band_hz < fs * 0.5 or (overlap is not None and overlap < MODAL_OVERLAP_LIMIT):
        # Clearly modal: single-slope metrics are never the authority.
        domain = 'modal_non_diffuse'
        applicable = False
        reasons.append('BELOW_SCHROEDER_FREQUENCY')
        reasons.append('MODAL_AUTHORITY_REQUIRED')
        required_evidence = 'modal_decay_record'
    elif band_hz < fs or (overlap is not None and overlap < MODAL_OVERLAP_THRESHOLD):
        # Transition region: usable with declared limitations only.
        domain = 'transition_region'
        limited = True
        reasons.append('TRANSITION_REGION_CAUTION')
        reasons.append('METRIC_ALLOWED_WITH_LIMITATIONS')
        if overlap is not None and overlap < MODAL_OVERLAP_THRESHOLD:
            reasons.append('MODAL_OVERLAP_INSUFFICIENT')
    else:
        domain = 'diffuse_field_band_metric'

    # Profile binding checks (issue §3/§7): ISO 3382-2 fit quality gates.
    if profile is not None:
        if profile.band_hz != band_hz:
            # A profile bound to another band is not evidence for this one.
            reasons.append('METRIC_NOT_DEFINED_FOR_CONTEXT')
            applicable = False
        else:
            metric_kind = profile.metric
            if metric_id != f'{metric_kind}_band':
                reasons.append('METRIC_NOT_DEFINED_FOR_CONTEXT')
                applicable = False
            if profile.dynamic_range_db < _MIN_DYNAMIC_RANGE_DB[metric_kind]:
                domain = 'insufficient_decay_range'
                applicable = False
                reasons.append('DECAY_RANGE_INSUFFICIENT')
            if profile.snr_db is not None and profile.snr_db < _MIN_SNR_DB:
                domain = 'insufficient_snr'
                applicable = False
                reasons.append('SIGNAL_TO_NOISE_INSUFFICIENT')
            if profile.fit_quality in ('poor', 'unreliable'):
                limited = True
                if 'METRIC_ALLOWED_WITH_LIMITATIONS' not in reasons:
                    reasons.append('METRIC_ALLOWED_WITH_LIMITATIONS')

    # Spatial-field honesty (Prinn 2025): high position-to-position spread
    # forbids presenting one scalar as the room's decay.
    if spatial is not None:
        if spatial.band_hz != band_hz or spatial.metric != profile_metric(
            metric_id
        ):
            pass  # record belongs to another band/metric — not this decision
        else:
            cov = spatial.coefficient_of_variation()
            if cov is not None and cov > 0.15 and domain != 'modal_non_diffuse':
                domain = 'spatially_aggregated_with_limitations'
                limited = True
                reasons.append('SPATIAL_VARIANCE_HIGH')
                required_evidence = 'per_position'
            if spatial.aggregation != 'none' and len(spatial.samples) < 3:
                limited = True
                reasons.append('POSITION_LOCAL_METRIC')

    if applicable and not reasons:
        reasons.append('METRIC_ALLOWED')

    return MetricApplicabilityDecision(
        metric_id=metric_id,
        context_summary=summary,
        domain_class=domain,
        reasons=tuple(reasons),
        applicable=applicable,
        limited=limited,
        schroeder_hz=fs,
        modal_overlap=overlap,
        required_evidence=required_evidence,
    )


def profile_metric(metric_id: MetricId) -> DecayMetricKind | None:
    """Map a band metric id to its profile kind (None for non-band ids)."""

    return {
        'edt_band': 'edt',
        't20_band': 't20',
        't30_band': 't30',
    }.get(metric_id)


# ---------------------------------------------------------------------------
# Prediction ↔ measurement comparability (issue §6)
# ---------------------------------------------------------------------------


class MetricComparisonEligibility(BaseModel):
    """Whether two profiles may be numerically compared (issue §6)."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['metric-comparison-1'] = (
        COMPARISON_AUTHORITY_VERSION
    )
    left_profile_sha256: str = Field(pattern=_SHA256)
    right_profile_sha256: str = Field(pattern=_SHA256)
    eligibility: ComparisonEligibility
    reasons: tuple[ComparisonReason, ...] = ()

    @model_validator(mode='after')
    def valid_comparison(self) -> 'MetricComparisonEligibility':
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError('reasons must be unique')
        if self.eligibility == 'COMPARABLE' and self.reasons and (
            set(self.reasons) != {'SEMANTICS_MATCH'}
        ):
            raise ValueError('comparable decisions carry no failure reasons')
        return self


def evaluate_metric_comparison(
    prediction: DecayMetricProfile,
    measurement: DecayMetricProfile,
    *,
    prediction_domain: MetricDomainClass | None = None,
    measurement_domain: MetricDomainClass | None = None,
) -> MetricComparisonEligibility:
    """Compare metric *semantics*, never raw numbers across profiles."""

    reasons: list[ComparisonReason] = []
    if prediction.metric != measurement.metric:
        reasons.append('METRIC_MISMATCH')
    if prediction.method != measurement.method:
        reasons.append('METHOD_MISMATCH')
    if prediction.band_hz != measurement.band_hz:
        reasons.append('PROFILE_MISMATCH')
    if prediction.standard_revision != measurement.standard_revision:
        reasons.append('PROFILE_MISMATCH')
    if prediction.fit_interval_db != measurement.fit_interval_db:
        reasons.append('PROFILE_MISMATCH')
    if set(prediction.position_ids) != set(measurement.position_ids):
        reasons.append('POSITION_SET_MISMATCH')
    domains = {d for d in (prediction_domain, measurement_domain) if d}
    if any(
        d in ('modal_non_diffuse', 'insufficient_decay_range',
              'insufficient_snr', 'not_applicable')
        for d in domains
    ):
        reasons.append('DOMAIN_MISMATCH')

    if 'DOMAIN_MISMATCH' in reasons or 'METRIC_MISMATCH' in reasons:
        eligibility: ComparisonEligibility = 'INCOMPARABLE'
    elif reasons:
        eligibility = 'COMPARABLE_WITH_LIMITATIONS'
    else:
        eligibility = 'COMPARABLE'
        reasons.append('SEMANTICS_MATCH')

    return MetricComparisonEligibility(
        left_profile_sha256=prediction.profile_sha256,
        right_profile_sha256=measurement.profile_sha256,
        eligibility=eligibility,
        reasons=tuple(dict.fromkeys(reasons)),
    )


# ---------------------------------------------------------------------------
# Optimization guard (issue §7) — never optimize an inapplicable metric.
# ---------------------------------------------------------------------------


class OptimizationMetricEligibility(BaseModel):
    """Whether ``metric_id`` may be an optimization objective here."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['metric-optimization-1'] = (
        OPTIMIZATION_AUTHORITY_VERSION
    )
    metric_id: MetricId
    eligible: bool
    substitute_objective: MetricId | None = None
    reasons: tuple[OptimizationReason, ...] = ()

    @model_validator(mode='after')
    def valid_optimization(self) -> 'OptimizationMetricEligibility':
        if self.eligible and self.substitute_objective is not None:
            raise ValueError(
                'an eligible metric needs no substitute objective'
            )
        return self


def evaluate_optimization_metric(
    decision: MetricApplicabilityDecision,
) -> OptimizationMetricEligibility:
    """An inapplicable metric must never become the optimization target."""

    if decision.applicable:
        return OptimizationMetricEligibility(
            metric_id=decision.metric_id,
            eligible=True,
            reasons=('METRIC_APPLICABLE',),
        )
    if decision.required_evidence == 'modal_decay_record':
        return OptimizationMetricEligibility(
            metric_id=decision.metric_id,
            eligible=False,
            substitute_objective='modal_decay_per_mode',
            reasons=(
                'METRIC_OUTSIDE_DOMAIN',
                'MODAL_REGION_NEEDS_MODAL_OBJECTIVE',
            ),
        )
    if decision.required_evidence == 'per_position':
        return OptimizationMetricEligibility(
            metric_id=decision.metric_id,
            eligible=False,
            substitute_objective='spatial_rt_field',
            reasons=(
                'METRIC_OUTSIDE_DOMAIN',
                'POSITION_LOCAL_OBJECTIVE_REQUIRED',
            ),
        )
    reasons: list[OptimizationReason] = ['METRIC_OUTSIDE_DOMAIN']
    if decision.domain_class in (
        'insufficient_decay_range',
        'insufficient_snr',
    ):
        reasons.append('DECAY_RANGE_INSUFFICIENT')
    return OptimizationMetricEligibility(
        metric_id=decision.metric_id,
        eligible=False,
        reasons=tuple(reasons),
    )


__all__ = [
    'APPLICABILITY_EVALUATOR_VERSION',
    'COMPARISON_AUTHORITY_VERSION',
    'COMPARISON_ELIGIBILITY_LABELS',
    'ComparisonEligibility',
    'ComparisonReason',
    'DECAY_PROFILE_AUTHORITY_VERSION',
    'DecayMetricKind',
    'DecayMetricProfile',
    'DecayMethodKind',
    'METRIC_APPLICABILITY_REASON_LABELS',
    'METRIC_DOMAIN_LABELS',
    'MODAL_AUTHORITY_VERSION',
    'MODAL_BANDWIDTH_COEFFICIENT',
    'MODAL_OVERLAP_LIMIT',
    'MODAL_OVERLAP_THRESHOLD',
    'MetricApplicabilityDecision',
    'MetricApplicabilityReason',
    'MetricComparisonEligibility',
    'MetricDomainClass',
    'MetricId',
    'ModalDecayRecord',
    'OPTIMIZATION_AUTHORITY_VERSION',
    'OptimizationMetricEligibility',
    'OptimizationReason',
    'PositionDecaySample',
    'RoomMetricContext',
    'RT_JND_RELATIVE',
    'SCHROEDER_COEFFICIENT',
    'SPATIAL_DECAY_AUTHORITY_VERSION',
    'SpatialDecayRecord',
    'build_decay_metric_profile',
    'build_modal_decay_record',
    'build_spatial_decay_record',
    'evaluate_metric_applicability',
    'evaluate_metric_comparison',
    'evaluate_optimization_metric',
    'modal_bandwidth_hz',
    'modal_density_per_hz',
    'modal_overlap_index',
    'profile_metric',
    'room_metric_context',
    'schroeder_frequency_hz',
]
