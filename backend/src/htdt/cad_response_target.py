"""Response-target / spectral-balance authority (issue #588).

``target curve``, ``spectral balance``, ``seat-to-seat uniformity`` and
``equalizer objective`` are related but **not the same quantity**. This
module is the canonical declaration + evaluation authority that keeps them
separate:

- :class:`ResponseTargetProfile` is the immutable, hash-identified *target*
  — what response shape the project asks for, where it came from, which
  channels/seats it applies to, and the exact smoothing/window/
  normalization semantics a comparison uses. A named "house curve" without
  exact data/version is not a reproducible target.
- :func:`evaluate_response_target` produces a :class:`SpectralBalanceEvaluation`
  — per-seat target deviation, seat-to-seat spread, aggregation and
  control/holdout split as *independent* metrics. A system can be uniform
  yet miss the target, or match the mean while one seat fails; no single
  scalar may hide that.
- ``kind`` is an honest taxonomy: ``project_defined`` /
  ``user_preference`` / ``provider_device_profile`` /
  ``external_standard_profile`` / ``measured_reference_derived`` /
  ``system_capability_derived`` / ``research_profile`` / ``unknown``.
  External-standard targets bind an exact registry document and may stay
  ``source_version_ambiguous`` (the AVIXA A103.01 catalogue vs store-page
  revision-label conflict is the canonical case); measurement-derived
  targets must retain their derivation provenance.

Composition:

- :class:`CadTargetCurveProfile` (#508) remains the calibration-plan
  binding authority; this module's profile pins the *semantic* target —
  comparison results and verification claims reference
  ``profile_id + version + target_sha256``.
- ``cedia-cta-rp22`` parameters 19/20 evaluate relative to a target
  declared here (#579); this module owns the target itself.
- decision significance (e.g. "A beats B by 0.2 dB inside uncertainty")
  belongs to the #577 decision-rule authority — this module emits metrics,
  never rankings.
"""

from __future__ import annotations

from math import isfinite, log10, sqrt
from statistics import median
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_calibration import (
    CadTargetCurve,
    CadTargetNormalizationCondition,
)
from .cad_target_profile import TargetProfileTolerance
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


RESPONSE_TARGET_AUTHORITY_VERSION = 'response-target-1'
SPECTRAL_BALANCE_AUTHORITY_VERSION = 'spectral-balance-evaluation-1'

_SHA256 = r'^[0-9a-f]{64}$'


ResponseTargetKind = Literal[
    'project_defined',
    'user_preference',
    'provider_device_profile',
    'external_standard_profile',
    'measured_reference_derived',
    'system_capability_derived',
    'research_profile',
    'unknown',
]
"""Where the target comes from (#588 §1). ``unknown`` is an honest
admission — never silently normalize it to a guessed kind."""

ResponseChannelScope = Literal[
    'screen',
    'wide',
    'surround',
    'upper',
    'lfe',
    'subwoofer',
    'redirected_bass',
    'system_sum',
    'custom',
]
"""Channel/role group a target or a response applies to (#588 §7). A
full-range target is never applied to an LFE-only or band-limited
response."""

TargetSmoothing = Literal[
    'none',
    '1/1_oct',
    '1/3_oct',
    '1/6_oct',
    '1/12_oct',
    'variable_resolution',
    'custom',
]
"""Declared smoothing of the compared responses. ``custom`` requires a
description — an undocumented smoothing is not a comparison method."""

TargetGridRule = Literal[
    'target_points',
    'measurement_grid',
    'explicit_grid',
]
"""Where the deviation is evaluated: at the declared curve's frequency
points, on the union of supplied measurement grids, or on an explicit
comparison grid carried by the profile."""

TargetInterpolation = Literal[
    'linear_db_log_hz',
    'linear_db_hz',
    'nearest',
    'step',
]
"""Exact interpolation between declared points. ``linear_db_log_hz`` is
the conventional audio comparison; any other choice is declared, never
assumed."""

TargetAggregation = Literal[
    'unweighted_mean',
    'weighted_mean',
    'rms_energy',
    'median',
    'percentile',
    'worst',
]
"""Listening-area aggregation across seats (#588 §5) — always an explicit
choice, and the raw per-seat rows survive next to it."""

ExternalBindingState = Literal[
    'resolved_exact',
    'source_version_ambiguous',
    'unregistered',
    'license_profile_unavailable',
]
"""State of an external-standard target binding (#588 §10). Public pages
that disagree on revision/status keep the profile
``source_version_ambiguous`` until the exact document is obtained."""

TargetProviderAcquisition = Literal['generated', 'imported', 'observed']
"""For provider/device curves: whether HTDT generated the target, or
merely imported/observed it (#588 §11) — visual similarity to a provider
curve never claims provider-algorithm equivalence."""

SeatResponseRole = Literal['control', 'holdout', 'evaluation']
"""Role of one seat response in the evaluation (#588 §13). Control seats
feed the filter design; holdout seats are evaluated against the *same*
pinned target but reported separately."""


class ResponseTargetDerivation(BaseModel):
    """Provenance of a ``measured_reference_derived`` target (#588 §9):
    the source measurements, transform path and algorithm that produced
    the target — a smoothed measurement is never relabelled an independent
    engineering standard."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    source_measurement_ids: tuple[str, ...] = Field(min_length=1)
    transform_dag_ref: str | None = None
    smoothing_or_fit: str = ''
    derivation_algorithm: str = Field(min_length=1)
    derivation_version: str = Field(min_length=1)
    seat_sample_set: tuple[str, ...] = ()
    note: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'ResponseTargetDerivation':
        if len(set(self.source_measurement_ids)) != len(
            self.source_measurement_ids
        ):
            raise ValueError('source measurement ids must be unique')
        if len(set(self.seat_sample_set)) != len(self.seat_sample_set):
            raise ValueError('seat sample set entries must be unique')
        return self


class ResponseTargetExternalBinding(BaseModel):
    """Exact external-document binding for an
    ``external_standard_profile`` target (#588 §10) — publisher/document/
    edition resolved against the standards registry, or an honest
    ambiguous/unavailable state."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    standard_id: str = Field(min_length=1)
    edition: str | None = None
    registry_key: str | None = None
    binding_state: ExternalBindingState
    source_note: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'ResponseTargetExternalBinding':
        if self.registry_key is not None and self.edition is None:
            raise ValueError('a registry key requires its edition')
        if self.binding_state == 'resolved_exact' and (
            self.edition is None
        ):
            raise ValueError(
                'a resolved_exact binding needs the exact edition — a '
                'document number alone is not an identity'
            )
        if self.binding_state in (
            'source_version_ambiguous',
            'unregistered',
            'license_profile_unavailable',
        ) and not self.source_note:
            raise ValueError(
                'an unresolved/unavailable binding must record why'
            )
        return self


class ResponseTargetProviderRef(BaseModel):
    """Provider/device target identity (#588 §11)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    provider: str = Field(min_length=1)
    product: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    acquisition: TargetProviderAcquisition
    device_profile_id: str | None = None
    note: str = ''


class TargetComparisonSemantics(BaseModel):
    """The exact comparison contract a ±dB statement is incomplete without
    (#588 §4): grid, smoothing, magnitude representation, weighting,
    window/gate, and the declared listening-area aggregation."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    grid_rule: TargetGridRule = 'target_points'
    explicit_grid_hz: tuple[float, ...] = ()
    smoothing: TargetSmoothing = 'none'
    smoothing_description: str = ''
    magnitude: Literal['db'] = 'db'
    weighting: str | None = None
    window_gate: str | None = None
    aggregation: TargetAggregation = 'unweighted_mean'
    aggregation_percentile: float | None = None
    seat_weight_map: dict[str, float] | None = None
    note: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'TargetComparisonSemantics':
        if self.grid_rule == 'explicit_grid' and not self.explicit_grid_hz:
            raise ValueError(
                'explicit_grid requires explicit_grid_hz'
            )
        if self.explicit_grid_hz:
            freqs = self.explicit_grid_hz
            if any(f <= 0 or not isfinite(float(f)) for f in freqs):
                raise ValueError('explicit grid frequencies must be > 0')
            if any(b <= a for a, b in zip(freqs, freqs[1:])):
                raise ValueError(
                    'explicit grid frequencies must be strictly increasing'
                )
        if self.smoothing == 'custom' and not self.smoothing_description:
            raise ValueError('custom smoothing requires a description')
        if self.aggregation == 'percentile' and (
            self.aggregation_percentile is None
        ):
            raise ValueError(
                'percentile aggregation requires aggregation_percentile'
            )
        if self.aggregation_percentile is not None and not (
            0.0 < float(self.aggregation_percentile) <= 100.0
        ):
            raise ValueError('aggregation percentile must be in (0, 100]')
        if self.aggregation == 'weighted_mean' and not self.seat_weight_map:
            raise ValueError(
                'weighted_mean aggregation requires a seat_weight_map'
            )
        if self.seat_weight_map is not None:
            if any(
                (not isfinite(float(w))) or float(w) < 0.0
                for w in self.seat_weight_map.values()
            ):
                raise ValueError('seat weights must be finite and >= 0')
        return self


class ResponseTargetProfile(BaseModel):
    """Immutable, versioned, hash-identified response target (#588 §3).

    Changing one point, slope, normalization, range or scope produces a
    different ``target_sha256`` — no software update silently alters a
    historical default target.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'response-target-1'
    ] = RESPONSE_TARGET_AUTHORITY_VERSION
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: ResponseTargetKind
    curve: CadTargetCurve
    interpolation: TargetInterpolation = 'linear_db_log_hz'
    frequency_validity_hz: tuple[float, float] | None = None
    channel_scope: tuple[ResponseChannelScope, ...] = ('system_sum',)
    seat_scope: tuple[str, ...] = ()
    semantics: TargetComparisonSemantics = Field(
        default_factory=TargetComparisonSemantics
    )
    tolerances: tuple[TargetProfileTolerance, ...] = ()
    derivation: ResponseTargetDerivation | None = None
    external_binding: ResponseTargetExternalBinding | None = None
    provider: ResponseTargetProviderRef | None = None
    usable_band_note: str = ''
    rationale: str = ''
    limitations: str = ''
    created_at_utc: str = Field(min_length=1)
    target_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'target_sha256', 'profile_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ResponseTargetProfile':
        from datetime import datetime

        try:
            parsed = datetime.fromisoformat(self.created_at_utc)
        except ValueError as exc:
            raise ValueError('created_at_utc must be ISO-8601') from exc
        if parsed.tzinfo is None:
            raise ValueError('created_at_utc must be timezone-aware')
        if self.frequency_validity_hz is not None:
            low, high = self.frequency_validity_hz
            if not (isfinite(float(low)) and isfinite(float(high))):
                raise ValueError('frequency validity range must be finite')
            if not (0.0 < float(low) < float(high)):
                raise ValueError(
                    'frequency validity range must satisfy 0 < low < high'
                )
        if len(set(self.channel_scope)) != len(self.channel_scope):
            raise ValueError('channel scope entries must be unique')
        if len(set(self.seat_scope)) != len(self.seat_scope):
            raise ValueError('seat scope entries must be unique')
        derivation_required = self.kind == 'measured_reference_derived'
        if derivation_required and self.derivation is None:
            raise ValueError(
                'a measurement-derived target requires its derivation '
                'provenance'
            )
        if not derivation_required and self.derivation is not None:
            raise ValueError(
                'derivation provenance is only valid for '
                'measured_reference_derived targets'
            )
        if self.kind == 'external_standard_profile' and (
            self.external_binding is None
        ):
            raise ValueError(
                'an external-standard target requires its external binding'
            )
        if self.kind != 'external_standard_profile' and (
            self.external_binding is not None
        ):
            raise ValueError(
                'an external binding is only valid for '
                'external_standard_profile targets'
            )
        if self.kind == 'provider_device_profile' and self.provider is None:
            raise ValueError(
                'a provider/device target requires the provider identity'
            )
        if self.kind != 'provider_device_profile' and (
            self.provider is not None
        ):
            raise ValueError(
                'a provider reference is only valid for '
                'provider_device_profile targets'
            )
        if self.kind == 'unknown' and not self.rationale:
            raise ValueError(
                'an unknown-kind target must record why it is unknown'
            )
        digest = _hash(self.semantic_payload())
        if self.target_sha256 != digest:
            raise ValueError('response target hash mismatch')
        if self.profile_id != 'rtgt-' + digest[:24]:
            raise ValueError('response target id mismatch')
        return self


def build_response_target_profile(
    *,
    document_id: str,
    version: str,
    name: str,
    kind: ResponseTargetKind,
    curve: CadTargetCurve,
    interpolation: TargetInterpolation = 'linear_db_log_hz',
    frequency_validity_hz: tuple[float, float] | None = None,
    channel_scope: tuple[ResponseChannelScope, ...] = ('system_sum',),
    seat_scope: tuple[str, ...] = (),
    semantics: TargetComparisonSemantics | None = None,
    tolerances: tuple[TargetProfileTolerance, ...] = (),
    derivation: ResponseTargetDerivation | None = None,
    external_binding: ResponseTargetExternalBinding | None = None,
    provider: ResponseTargetProviderRef | None = None,
    usable_band_note: str = '',
    rationale: str = '',
    limitations: str = '',
    created_at_utc: str | None = None,
) -> ResponseTargetProfile:
    payload = canonicalize_payload(
        ResponseTargetProfile,
        dict(
            authority_version=RESPONSE_TARGET_AUTHORITY_VERSION,
            profile_id='',
            version=version,
            document_id=document_id,
            name=name,
            kind=kind,
            curve=curve,
            interpolation=interpolation,
            frequency_validity_hz=frequency_validity_hz,
            channel_scope=tuple(channel_scope),
            seat_scope=tuple(seat_scope),
            semantics=semantics or TargetComparisonSemantics(),
            tolerances=tuple(tolerances),
            derivation=derivation,
            external_binding=external_binding,
            provider=provider,
            usable_band_note=usable_band_note,
            rationale=rationale,
            limitations=limitations,
            created_at_utc=created_at_utc or _utc_now(),
            target_sha256='0' * 64,
        ),
    )
    probe = ResponseTargetProfile.model_construct(**payload)
    digest = _hash(probe.semantic_payload())
    return ResponseTargetProfile(
        **probe.model_dump(
            mode='python', exclude={'target_sha256', 'profile_id'}
        ),
        profile_id='rtgt-' + digest[:24],
        target_sha256=digest,
    )


def target_identity_differences(
    first: ResponseTargetProfile,
    second: ResponseTargetProfile,
) -> tuple[str, ...]:
    """Semantic fields on which two target profiles differ (#588 §12).

    Identity fields (id, hash, creation time, version tag) are excluded —
    this answers whether an optimization target and a deployed/measured
    commissioning target *are* the same target, not whether two records
    exist.
    """
    a = first.semantic_payload()
    b = second.semantic_payload()
    for field in ('version', 'created_at_utc'):
        a.pop(field, None)
        b.pop(field, None)
    return tuple(
        sorted(name for name in a.keys() | b.keys() if a.get(name) != b.get(name))
    )


# ---------------------------------------------------------------------------
# Evaluation (#588 §2, §4, §5, §6, §13)
# ---------------------------------------------------------------------------


class SeatResponseObservation(BaseModel):
    """One seat's response input for a spectral-balance evaluation.

    ``points`` are (frequency_hz, level_db) pairs under the smoothing the
    caller declares — the evaluation records which seats were compared but
    never stores claims about points outside the target's validity range.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    seat_id: str = Field(min_length=1)
    role: SeatResponseRole = 'evaluation'
    channel_group: ResponseChannelScope = 'system_sum'
    points: tuple[tuple[float, float], ...] = Field(min_length=2)
    response_ref: str | None = None
    note: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'SeatResponseObservation':
        freqs = [point[0] for point in self.points]
        if any(f <= 0 or not isfinite(float(f)) for f in freqs):
            raise ValueError('response frequencies must be > 0 and finite')
        if any(b <= a for a, b in zip(freqs, freqs[1:])):
            raise ValueError(
                'response frequencies must be strictly increasing'
            )
        if any(
            not isfinite(float(point[1])) for point in self.points
        ):
            raise ValueError('response levels must be finite')
        return self


def _interpolate(
    points: tuple[tuple[float, float], ...],
    frequency_hz: float,
    rule: TargetInterpolation,
) -> float | None:
    """Interpolate a (hz, dB) series at ``frequency_hz``.

    Returns ``None`` outside the series' declared range — an
    extrapolated value is never evidence.
    """
    if frequency_hz < points[0][0] or frequency_hz > points[-1][0]:
        return None
    if rule == 'nearest':
        return min(
            points, key=lambda point: abs(point[0] - frequency_hz)
        )[1]
    if rule == 'step':
        value = points[0][1]
        for point in points:
            if point[0] <= frequency_hz:
                value = point[1]
            else:
                break
        return value
    for before, after in zip(points, points[1:]):
        if before[0] <= frequency_hz <= after[0]:
            if after[0] == before[0]:
                return before[1]
            if rule == 'linear_db_log_hz':
                position = (
                    (log10(frequency_hz) - log10(before[0]))
                    / (log10(after[0]) - log10(before[0]))
                )
            else:
                position = (
                    (frequency_hz - before[0]) / (after[0] - before[0])
                )
            return before[1] + position * (after[1] - before[1])
    return points[-1][1]


def _curve_points(curve: CadTargetCurve) -> tuple[tuple[float, float], ...]:
    return tuple(
        (point.frequency_hz, point.level_db) for point in curve.points
    )


def _normalization_offset(
    response_points: tuple[tuple[float, float], ...],
    target_points: tuple[tuple[float, float], ...],
    normalization: CadTargetNormalizationCondition,
    interpolation: TargetInterpolation,
) -> float | None:
    """Level offset aligning one seat's response to the target at the
    declared normalization anchor.

    - ``reference_frequency`` — the response is shifted so it equals the
      target level at the reference frequency;
    - ``band_average`` — shifted so its mean over the reference band
      equals the target's mean over the same band;
    - ``absolute_level`` — no shift; the comparison uses absolute dB at
      the declared reference level.

    ``None`` means the anchor cannot be resolved for this seat — recorded
    as ``normalization_failed`` coverage, never a zero guess.
    """
    if normalization.method == 'absolute_level':
        return 0.0
    if normalization.method == 'reference_frequency':
        reference = normalization.reference_frequency_hz
        assert reference is not None
        response_level = _interpolate(
            response_points, reference, interpolation
        )
        target_level = _interpolate(
            target_points, reference, interpolation
        )
        if response_level is None or target_level is None:
            return None
        return target_level - response_level
    band = normalization.reference_band_hz
    assert band is not None
    low, high = band
    # Pair the samples — a response point whose frequency the target does
    # not cover must not enter either mean, or the offset is biased by
    # asymmetric sample sets.
    pairs = [
        (level, _interpolate(target_points, freq, interpolation))
        for freq, level in response_points
        if low <= freq <= high
    ]
    pairs = [(response, target) for response, target in pairs
             if target is not None]
    if not pairs:
        return None
    return (sum(target for _response, target in pairs) / len(pairs)) - (
        sum(response for response, _target in pairs) / len(pairs)
    )


def _comparison_grid(
    profile: ResponseTargetProfile,
    responses: tuple[SeatResponseObservation, ...],
) -> tuple[float, ...]:
    semantics = profile.semantics
    if semantics.grid_rule == 'explicit_grid':
        grid = tuple(semantics.explicit_grid_hz)
    elif semantics.grid_rule == 'target_points':
        grid = tuple(
            point.frequency_hz for point in profile.curve.points
        )
    else:
        freqs = sorted(
            {
                float(point[0])
                for response in responses
                for point in response.points
            }
        )
        grid = tuple(freqs)
    if profile.frequency_validity_hz is not None:
        low, high = profile.frequency_validity_hz
        grid = tuple(freq for freq in grid if low <= freq <= high)
    return grid


class SeatTargetMetrics(BaseModel):
    """Independent per-seat deviation metrics against the pinned target —
    kept raw even after area aggregation (#588 §5, §6)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    seat_id: str = Field(min_length=1)
    role: SeatResponseRole
    channel_group: ResponseChannelScope
    coverage_status: Literal['evaluated', 'no_coverage',
                             'normalization_failed']
    points_compared: int = Field(ge=0)
    rms_target_deviation_db: float | None = None
    mean_target_deviation_db: float | None = None
    max_abs_target_deviation_db: float | None = None
    normalization_offset_db: float | None = None

    @model_validator(mode='after')
    def _check(self) -> 'SeatTargetMetrics':
        if self.coverage_status == 'evaluated':
            if self.points_compared == 0:
                raise ValueError(
                    'an evaluated seat needs at least one compared point'
                )
            for value in (
                self.rms_target_deviation_db,
                self.mean_target_deviation_db,
                self.max_abs_target_deviation_db,
            ):
                if value is None:
                    raise ValueError(
                        'an evaluated seat needs its deviation metrics'
                    )
        else:
            if self.points_compared != 0:
                raise ValueError(
                    'a non-evaluated seat cannot have compared points'
                )
        for value in (
            self.rms_target_deviation_db,
            self.mean_target_deviation_db,
            self.max_abs_target_deviation_db,
            self.normalization_offset_db,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('metric values must be finite')
        return self


class SeatGroupMetrics(BaseModel):
    """Aggregate metrics for one declared seat group (control or holdout)
    under the same pinned target (#588 §13)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    group_role: Literal['control', 'holdout']
    seat_ids: tuple[str, ...]
    seats_evaluated: int = Field(ge=0)
    mean_rms_target_deviation_db: float | None = None
    seat_to_seat_spread_rms_db: float | None = None
    seat_to_seat_spread_max_db: float | None = None

    @model_validator(mode='after')
    def _check(self) -> 'SeatGroupMetrics':
        if len(set(self.seat_ids)) != len(self.seat_ids):
            raise ValueError('group seat ids must be unique')
        if self.seats_evaluated == 0:
            for value in (
                self.mean_rms_target_deviation_db,
                self.seat_to_seat_spread_rms_db,
                self.seat_to_seat_spread_max_db,
            ):
                if value is not None:
                    raise ValueError(
                        'an empty group carries no metrics'
                    )
        return self


class SpectralBalanceEvaluation(BaseModel):
    """Sealed spectral-balance evaluation: target tracking and spatial
    uniformity reported as independent metrics under the pinned
    comparison semantics — never one hidden score (#588 §2, §6)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'spectral-balance-evaluation-1'
    ] = SPECTRAL_BALANCE_AUTHORITY_VERSION
    evaluation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    target_sha256: str = Field(pattern=_SHA256)
    target_kind: ResponseTargetKind
    grid_rule: TargetGridRule
    smoothing: TargetSmoothing
    aggregation: TargetAggregation
    interpolation: TargetInterpolation
    comparison_grid_hz: tuple[float, ...]
    frequency_validity_hz: tuple[float, float] | None = None
    seat_metrics: tuple[SeatTargetMetrics, ...]
    seats_evaluated: int = Field(ge=0)
    seats_without_coverage: int = Field(ge=0)
    mean_rms_target_deviation_db: float | None = None
    seat_to_seat_spread_rms_db: float | None = None
    seat_to_seat_spread_max_db: float | None = None
    control_group: SeatGroupMetrics | None = None
    holdout_group: SeatGroupMetrics | None = None
    holdout_vs_control_delta_db: float | None = None
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'SpectralBalanceEvaluation':
        from datetime import datetime

        try:
            parsed = datetime.fromisoformat(self.evaluated_at_utc)
        except ValueError as exc:
            raise ValueError('evaluated_at_utc must be ISO-8601') from exc
        if parsed.tzinfo is None:
            raise ValueError('evaluated_at_utc must be timezone-aware')
        ids = [item.seat_id for item in self.seat_metrics]
        if len(ids) != len(set(ids)):
            raise ValueError('seat metric seat ids must be unique')
        if self.seats_evaluated == 0:
            for value in (
                self.mean_rms_target_deviation_db,
                self.seat_to_seat_spread_rms_db,
                self.seat_to_seat_spread_max_db,
            ):
                if value is not None:
                    raise ValueError(
                        'an evaluation with no covered seats carries no '
                        'area metrics'
                    )
        if self.holdout_vs_control_delta_db is not None and (
            self.holdout_group is None or self.control_group is None
        ):
            raise ValueError(
                'holdout_vs_control delta needs both groups evaluated'
            )
        digest = _hash(self.semantic_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('spectral-balance evaluation hash mismatch')
        if self.evaluation_id != 'sbev-' + digest[:24]:
            raise ValueError('spectral-balance evaluation id mismatch')
        return self

    def seat(self, seat_id: str) -> SeatTargetMetrics | None:
        for item in self.seat_metrics:
            if item.seat_id == seat_id:
                return item
        return None


def _aggregate(values: list[float], semantics: TargetComparisonSemantics,
               seat_ids: tuple[str, ...]) -> float | None:
    if not values:
        return None
    kind = semantics.aggregation
    if kind == 'unweighted_mean':
        return sum(values) / len(values)
    if kind == 'weighted_mean':
        weights = semantics.seat_weight_map or {}
        missing = [seat for seat in seat_ids if seat not in weights]
        if missing:
            raise ValueError(
                'weighted_mean aggregation lacks weights for seats: '
                + ', '.join(sorted(missing))
            )
        total = sum(weights[seat] for seat in seat_ids)
        if total <= 0:
            raise ValueError('seat weights sum to zero')
        return sum(
            value * weights[seat]
            for value, seat in zip(values, seat_ids)
        ) / total
    if kind == 'rms_energy':
        return sqrt(sum(value * value for value in values) / len(values))
    if kind == 'median':
        return float(median(values))
    if kind == 'percentile':
        percentile = float(semantics.aggregation_percentile or 0.0)
        ordered = sorted(values)
        rank = (percentile / 100.0) * (len(ordered) - 1)
        low_index = int(rank)
        high_index = min(low_index + 1, len(ordered) - 1)
        fraction = rank - low_index
        return (
            ordered[low_index]
            + (ordered[high_index] - ordered[low_index]) * fraction
        )
    return max(values)


def _group_metrics(
    role: Literal['control', 'holdout'],
    seat_metrics: tuple[SeatTargetMetrics, ...],
    spreads: list[float],
) -> SeatGroupMetrics:
    seat_ids = tuple(item.seat_id for item in seat_metrics)
    evaluated = [
        item.rms_target_deviation_db
        for item in seat_metrics
        if item.coverage_status == 'evaluated'
    ]
    return SeatGroupMetrics(
        group_role=role,
        seat_ids=seat_ids,
        seats_evaluated=len(evaluated),
        mean_rms_target_deviation_db=(
            (sum(evaluated) / len(evaluated)) if evaluated else None
        ),
        seat_to_seat_spread_rms_db=(
            sqrt(sum(v * v for v in spreads) / len(spreads))
            if spreads
            else None
        ),
        seat_to_seat_spread_max_db=(max(spreads) if spreads else None),
    )


def evaluate_response_target(
    profile: ResponseTargetProfile,
    *,
    document_id: str,
    responses: tuple[SeatResponseObservation, ...],
    evaluated_at_utc: str | None = None,
) -> SpectralBalanceEvaluation:
    """Evaluate seat responses against the pinned target.

    Only observations whose ``channel_group`` is inside the profile's
    ``channel_scope`` are eligible — a full-range target is never applied
    to a band-limited response. Deviations are computed on the declared
    grid under the declared interpolation and normalization; per-seat
    deviation and seat-to-seat spread remain separate metrics.
    """
    if not responses:
        raise ValueError('an evaluation needs at least one seat response')
    for response in responses:
        if response.channel_group not in profile.channel_scope:
            raise ValueError(
                f'response channel group {response.channel_group!r} is '
                f'outside target channel scope '
                f'{tuple(profile.channel_scope)!r} — a target never '
                'evaluates a response it does not apply to'
            )
    seat_ids = [response.seat_id for response in responses]
    if len(seat_ids) != len(set(seat_ids)):
        raise ValueError('seat response ids must be unique')

    target_points = _curve_points(profile.curve)
    interpolation = profile.interpolation
    grid = _comparison_grid(profile, responses)

    seat_metrics: list[SeatTargetMetrics] = []
    deviations_by_seat: dict[str, dict[float, float]] = {}
    for response in responses:
        offset = _normalization_offset(
            response.points,
            target_points,
            profile.curve.normalization,
            interpolation,
        )
        if offset is None:
            seat_metrics.append(
                SeatTargetMetrics(
                    seat_id=response.seat_id,
                    role=response.role,
                    channel_group=response.channel_group,
                    coverage_status='normalization_failed',
                    points_compared=0,
                )
            )
            continue
        deviations: dict[float, float] = {}
        for freq in grid:
            response_level = _interpolate(
                response.points, freq, interpolation
            )
            target_level = _interpolate(
                target_points, freq, interpolation
            )
            if response_level is None or target_level is None:
                continue
            deviations[freq] = response_level + offset - target_level
        if not deviations:
            seat_metrics.append(
                SeatTargetMetrics(
                    seat_id=response.seat_id,
                    role=response.role,
                    channel_group=response.channel_group,
                    coverage_status='no_coverage',
                    points_compared=0,
                    normalization_offset_db=offset,
                )
            )
            continue
        deviations_by_seat[response.seat_id] = deviations
        values = list(deviations.values())
        seat_metrics.append(
            SeatTargetMetrics(
                seat_id=response.seat_id,
                role=response.role,
                channel_group=response.channel_group,
                coverage_status='evaluated',
                points_compared=len(values),
                rms_target_deviation_db=sqrt(
                    sum(value * value for value in values) / len(values)
                ),
                mean_target_deviation_db=sum(values) / len(values),
                max_abs_target_deviation_db=max(
                    abs(value) for value in values
                ),
                normalization_offset_db=offset,
            )
        )

    # Seat-to-seat spread per grid frequency: only over seats that cover
    # the point — a seat absent at one frequency never fabricates a
    # uniform-sounding spread.
    spreads: list[float] = []
    for freq in grid:
        covered = [
            deviations[freq]
            for deviations in deviations_by_seat.values()
            if freq in deviations
        ]
        if len(covered) >= 2:
            spreads.append(max(covered) - min(covered))
    evaluated_metrics = [
        item for item in seat_metrics if item.coverage_status == 'evaluated'
    ]
    evaluated_ids = tuple(item.seat_id for item in evaluated_metrics)
    rms_values = [
        float(item.rms_target_deviation_db) for item in evaluated_metrics
    ]
    mean_rms = _aggregate(
        rms_values, profile.semantics, evaluated_ids
    )

    control_metrics = tuple(
        item for item in seat_metrics if item.role == 'control'
    )
    holdout_metrics = tuple(
        item for item in seat_metrics if item.role == 'holdout'
    )
    control_deviations = {
        seat_id: deviations
        for seat_id, deviations in deviations_by_seat.items()
        if any(
            item.seat_id == seat_id and item.role == 'control'
            for item in seat_metrics
        )
    }
    holdout_deviations = {
        seat_id: deviations
        for seat_id, deviations in deviations_by_seat.items()
        if any(
            item.seat_id == seat_id and item.role == 'holdout'
            for item in seat_metrics
        )
    }

    def _group_spreads(
        deviations_map: dict[str, dict[float, float]]
    ) -> list[float]:
        group_spreads: list[float] = []
        for freq in grid:
            covered = [
                deviations[freq]
                for deviations in deviations_map.values()
                if freq in deviations
            ]
            if len(covered) >= 2:
                group_spreads.append(max(covered) - min(covered))
        return group_spreads

    control_group = (
        _group_metrics('control', control_metrics, _group_spreads(control_deviations))
        if control_metrics
        else None
    )
    holdout_group = (
        _group_metrics('holdout', holdout_metrics, _group_spreads(holdout_deviations))
        if holdout_metrics
        else None
    )
    holdout_delta = None
    if (
        control_group is not None
        and holdout_group is not None
        and control_group.mean_rms_target_deviation_db is not None
        and holdout_group.mean_rms_target_deviation_db is not None
    ):
        holdout_delta = (
            holdout_group.mean_rms_target_deviation_db
            - control_group.mean_rms_target_deviation_db
        )

    payload = canonicalize_payload(
        SpectralBalanceEvaluation,
        dict(
            authority_version=SPECTRAL_BALANCE_AUTHORITY_VERSION,
            evaluation_id='',
            document_id=document_id,
            profile_id=profile.profile_id,
            profile_version=profile.version,
            target_sha256=profile.target_sha256,
            target_kind=profile.kind,
            grid_rule=profile.semantics.grid_rule,
            smoothing=profile.semantics.smoothing,
            aggregation=profile.semantics.aggregation,
            interpolation=profile.interpolation,
            comparison_grid_hz=grid,
            frequency_validity_hz=profile.frequency_validity_hz,
            seat_metrics=tuple(seat_metrics),
            seats_evaluated=len(evaluated_metrics),
            seats_without_coverage=(
                len(seat_metrics) - len(evaluated_metrics)
            ),
            mean_rms_target_deviation_db=mean_rms,
            seat_to_seat_spread_rms_db=(
                sqrt(sum(v * v for v in spreads) / len(spreads))
                if spreads
                else None
            ),
            seat_to_seat_spread_max_db=(
                max(spreads) if spreads else None
            ),
            control_group=control_group,
            holdout_group=holdout_group,
            holdout_vs_control_delta_db=holdout_delta,
            evaluated_at_utc=evaluated_at_utc or _utc_now(),
            evaluation_sha256='0' * 64,
        ),
    )
    probe = SpectralBalanceEvaluation.model_construct(**payload)
    digest = _hash(probe.semantic_payload())
    return SpectralBalanceEvaluation(
        **probe.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        ),
        evaluation_id='sbev-' + digest[:24],
        evaluation_sha256=digest,
    )


__all__ = [
    'ExternalBindingState',
    'RESPONSE_TARGET_AUTHORITY_VERSION',
    'ResponseChannelScope',
    'ResponseTargetDerivation',
    'ResponseTargetExternalBinding',
    'ResponseTargetKind',
    'ResponseTargetProfile',
    'ResponseTargetProviderRef',
    'SPECTRAL_BALANCE_AUTHORITY_VERSION',
    'SeatGroupMetrics',
    'SeatResponseObservation',
    'SeatResponseRole',
    'SeatTargetMetrics',
    'SpectralBalanceEvaluation',
    'TargetAggregation',
    'TargetComparisonSemantics',
    'TargetGridRule',
    'TargetInterpolation',
    'TargetProviderAcquisition',
    'TargetSmoothing',
    'build_response_target_profile',
    'evaluate_response_target',
    'target_identity_differences',
]
