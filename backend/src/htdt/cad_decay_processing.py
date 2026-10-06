"""Decay-curve noise / truncation processing authority (#676,
REV58-DSPDECAY).

RT/EDT/T20/T30 values derived from the same measured RIR can differ
materially because of noise-floor estimation, backward-integration
truncation/tail correction, band filtering/windowing and fit-range
choices — Lundeby-era inter-analyzer differences are processing
differences, not room differences. A bare ``T30 = 0.41 s`` is not a
reproducible measurand.

This module makes decay-curve processing a sealed, fail-closed
authority:

- :class:`CadDecayProcessingProfile` — the declared processing identity:
  band/filter semantics (kind, edges, filter class/order, zero-phase,
  padding), backward-integration variant, noise-floor method identity,
  tail-compensation model, per-metric fit windows and the
  algorithm/version. Two ``125 Hz RT`` values are comparable only when
  their processing profiles agree or are explicitly reconciled.

- :class:`CadDecayNoiseEstimate` — the background-noise estimate as its
  own evidence record: tail interval, estimator/statistic, stationarity
  assumption, level, uncertainty, method/version. Methods
  (``fixed_tail_estimate`` / ``lundeby_style_intersection`` /
  ``nonlinear_decay_plus_noise_model`` / ``two_rir_product_method`` …)
  are named, never silently hard-coded.

- :class:`CadRirTruncationDecision` — where the RIR/EDC was truncated and
  why: estimated decay/noise intersection, truncation time, available
  decay range before noise domination, reason and confidence. A capture
  that simply ends early reports ``capture_truncated`` — never a
  noise-floor figure masquerading as a processed decay.

- :class:`CadDecayEdcArtifact` — one retained decay curve: raw backward
  integral and compensated/corrected EDC are distinct derived artifacts,
  hash-linked by ``derived_from_ref``. The raw EDC is never destructively
  replaced.

- :class:`CadDecayFitRecord` — one scalar slope metric with its full fit
  identity: metric, fit level/time window, regression kind, sample count,
  dynamic range, noise margin, residual evidence and the sealed
  eligibility state — ``insufficient_decay_range``/``noise_floor_too_high``/
  ``capture_truncated``/``non_stationary_noise``/``multi_slope``/
  ``modal_method_required``/``indeterminate`` instead of a bare number.

Composition:

- #571 ``acoustic_metric_applicability`` — the modal/low-frequency
  applicability decision that can force ``modal_method_required``; this
  module supplies processing provenance and never overrides that gate.
- #671 coupled-space multi-slope decay — a multi-slope EDC declares
  ``multi_slope_model_mismatch`` rather than fitting one slope.
- #573/#580 background-noise state — nonstationary contamination input.
- #575 transforms — a downstream transform may re-derive but never
  silently upgrade eligibility.
- #566 solver convergence — simulated finite-tail truncation is a
  different cause class than measured background noise; this authority
  keeps them separate (``solver_truncation_declared`` on simulated
  inputs).
- #608/#609/#611 — the RIR stays the canonical artifact; this module pins
  it, never rewrites it.

Literature / standards basis
----------------------------
- Lundeby, Vigran, Bietz & Vorländer, *Uncertainties of Measurements in
  Room Acoustics*, Acustica 81(4), 344–355 (1995): systematic
  inter-analyzer differences from time windowing/filtering, reverse-time
  integration and noise compensation — processing identity belongs in
  measurement identity.
- Bodlund, *On the use of the integrated impulse response method for
  laboratory reverberation measurements*, JSV 56(3) (1978): backward
  integration gives a reproducible decay curve; the background-noise tail
  requires explicit treatment.
- Dragonetti, Ianniello & Romano, *Reverberation time measurement by the
  product of two room impulse responses*, Applied Acoustics 70(1) (2009):
  more than one defensible noise-handling method exists — method identity
  must be retained.
- Janković, Ćirić & Pantić, *Automated estimation of the truncation of
  room impulse response by applying a nonlinear decay model*, JASA
  139(3) (2016): exponential-plus-noise modelling determines truncation
  by fit rather than a fixed tail cutoff; truncation-correction terms can
  materially change RT estimates.
- ISO 3382-1:2009 / ISO 3382-2:2008: T20/T30 fit windows and
  accuracy-class semantics; ISO/DIS 3382-1 Ed.2 remains draft/research
  only.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


DECAY_AUTHORITY_SCHEMA_VERSION = 'decay-proc-1'
DECAY_EVALUATION_VERSION = 'decay-proc-eval-1'

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


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#676)
# ---------------------------------------------------------------------------

DecayBandKind = Literal[
    'broadband',
    'octave',
    'third_octave',
    'narrowband',
    'custom',
]

DecayFilterClass = Literal[
    'iir_bandpass',
    'fir_bandpass',
    'fft_brickwall',
    'zero_phase_forward_reverse',
    'external_undeclared',
    'unknown',
]

BackwardIntegrationKind = Literal[
    'schroeder_reverse_cumulative',
    'schroeder_with_noise_correction',
    'schroeder_with_tail_compensation',
    'two_rir_product',
    'declared_external',
    'unknown',
]

NoiseFloorMethod = Literal[
    'fixed_tail_estimate',
    'lundeby_style_intersection',
    'nonlinear_decay_plus_noise_model',
    'two_rir_product_method',
    'declared_external',
    'custom_validated',
    'none_declared',
]

NoiseStationarity = Literal[
    'stationary_assumed',
    'stationarity_verified',
    'nonstationary_detected',
    'unknown',
]

TruncationReason = Literal[
    'noise_intersection',
    'capture_end',
    'declared_window',
    'model_fit',
    'user_selected',
    'unknown',
]

EdcArtifactKind = Literal[
    'raw_backward_integral',
    'noise_compensated_edc',
    'tail_corrected_edc',
    'declared_external',
]

DecayMetricKind = Literal[
    'edt',
    't10',
    't20',
    't30',
    'custom',
]

DecayRegressionKind = Literal[
    'least_squares_line',
    'weighted_least_squares',
    'nonlinear_decay_model',
    'external_undeclared',
    'unknown',
]

DecayEligibility = Literal[
    'eligible',
    'eligible_with_limitations',
    'insufficient_decay_range',
    'noise_floor_too_high',
    'capture_truncated',
    'non_stationary_noise',
    'multi_slope_model_mismatch',
    'modal_method_required',
    'indeterminate',
]

#: Eligibility states that are genuinely usable (possibly with caveats).
_ELIGIBLE_STATES: frozenset[DecayEligibility] = frozenset(
    {'eligible', 'eligible_with_limitations'}
)

#: Truncation-cause classes kept distinct (#676 §11): measured background
#: noise and simulated finite-tail truncation are different limitations.
DecayTruncationCause = Literal[
    'measurement_noise_limit',
    'solver_time_or_order_truncation',
    'physical_decay_complete',
    'unknown',
]


# ---------------------------------------------------------------------------
# Embedded evidence blocks
# ---------------------------------------------------------------------------


class CadDecayBandSpec(BaseModel):
    """The exact band/filter semantics behind a decay curve (#676 §2).

    ``center_hz``/``edge`` frequencies, filter class/order, zero-phase vs
    causal implementation and sample rate are part of result identity —
    different filters produce different decay traces near low-frequency
    modes.
    """

    model_config = ConfigDict(frozen=True)

    band_kind: DecayBandKind
    center_hz: float | None = None
    band_low_hz: float | None = None
    band_high_hz: float | None = None
    filter_class: DecayFilterClass = 'unknown'
    filter_order: int | None = None
    zero_phase: bool | None = None
    padding_semantics: str | None = None
    sample_rate_hz: float | None = None
    filter_label: str | None = None

    @model_validator(mode='after')
    def valid_band(self) -> 'CadDecayBandSpec':
        for label, value in (
            ('center_hz', self.center_hz),
            ('band_low_hz', self.band_low_hz),
            ('band_high_hz', self.band_high_hz),
            ('sample_rate_hz', self.sample_rate_hz),
        ):
            if value is not None:
                _require_finite(value, f'decay band {label}')
                if value <= 0:
                    raise ValueError(f'decay band {label} must be positive')
        if (
            self.band_low_hz is not None
            and self.band_high_hz is not None
            and self.band_high_hz <= self.band_low_hz
        ):
            raise ValueError('decay band edges must be ascending')
        if self.band_kind != 'broadband' and (
            self.center_hz is None
            and (self.band_low_hz is None or self.band_high_hz is None)
        ):
            raise ValueError(
                'a banded decay spec requires a center frequency or band '
                'edges — an unnamed "125 Hz RT" band is not reproducible'
            )
        if self.filter_order is not None and self.filter_order < 1:
            raise ValueError('filter order must be positive')
        return self


class CadDecayFitWindow(BaseModel):
    """One declared fit window for one metric (#676 §6)."""

    model_config = ConfigDict(frozen=True)

    metric: DecayMetricKind
    start_level_db: float
    end_level_db: float
    min_dynamic_range_db: float | None = None

    @model_validator(mode='after')
    def valid_window(self) -> 'CadDecayFitWindow':
        _require_finite(self.start_level_db, 'fit window start_level_db')
        _require_finite(self.end_level_db, 'fit window end_level_db')
        if self.end_level_db >= self.start_level_db:
            raise ValueError(
                'a decay fit window must descend (end < start level)'
            )
        if self.min_dynamic_range_db is not None:
            _require_finite(
                self.min_dynamic_range_db,
                'fit window min_dynamic_range_db',
            )
            if self.min_dynamic_range_db < 0:
                raise ValueError('min_dynamic_range_db must be >= 0')
        return self


# ---------------------------------------------------------------------------
# Sealed authorities
# ---------------------------------------------------------------------------


class CadDecayProcessingProfile(BaseModel):
    """Sealed decay-processing identity (#676 §1–§6).

    The exact processing contract a decay curve was produced under:
    band/filter semantics, the backward-integration variant, the declared
    noise-floor method and tail-compensation model, per-metric fit
    windows, and the algorithm/version string. An external-tool import
    declares ``declared_external``/``unknown`` pieces honestly — a REW CSV
    scalar is weaker evidence than a retained RIR with reproducible
    processing.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_label: str = Field(min_length=1)
    band: CadDecayBandSpec
    backward_integration: BackwardIntegrationKind
    noise_floor_method: NoiseFloorMethod
    tail_compensation: Literal[
        'none', 'declared_model', 'external', 'unknown'
    ] = 'unknown'
    fit_windows: tuple[CadDecayFitWindow, ...] = ()
    noise_stationarity_assumption: NoiseStationarity = 'unknown'
    measurement_or_simulated: Literal[
        'measured', 'simulated', 'unknown'
    ] = 'unknown'
    solver_truncation_declared: DecayTruncationCause = 'unknown'
    algorithm_version: str = Field(min_length=1)
    implementation_reference: str | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_profile(self) -> 'CadDecayProcessingProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        metrics = [w.metric for w in self.fit_windows]
        if len(metrics) != len(set(metrics)):
            raise ValueError(
                'each metric may declare at most one fit window in a '
                'profile'
            )
        if (
            self.backward_integration == 'unknown'
            and self.measurement_or_simulated != 'simulated'
        ):
            raise ValueError(
                'a measured-decay profile must declare its '
                'backward-integration variant — an undeclared integration '
                'is not a reproducible measurand'
            )
        if (
            self.noise_floor_method == 'none_declared'
            and self.backward_integration
            in ('schroeder_with_noise_correction',
                'schroeder_with_tail_compensation')
        ):
            raise ValueError(
                'a compensated/corrected integration requires a declared '
                'noise-floor method — compensation without a noise '
                'estimate is fabrication'
            )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('decay processing profile hash mismatch')
        if self.profile_id != _semantic_id('decpro', expected):
            raise ValueError(
                'decay processing profile id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_label': self.profile_label,
            'band': self.band.model_dump(mode='json'),
            'backward_integration': self.backward_integration,
            'noise_floor_method': self.noise_floor_method,
            'tail_compensation': self.tail_compensation,
            'fit_windows': [
                w.model_dump(mode='json') for w in self.fit_windows
            ],
            'noise_stationarity_assumption':
                self.noise_stationarity_assumption,
            'measurement_or_simulated': self.measurement_or_simulated,
            'solver_truncation_declared': self.solver_truncation_declared,
            'algorithm_version': self.algorithm_version,
            'implementation_reference': self.implementation_reference,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    def window_for(self, metric: DecayMetricKind) -> CadDecayFitWindow | None:
        for window in self.fit_windows:
            if window.metric == metric:
                return window
        return None


def decay_profile_binding(
    profile: CadDecayProcessingProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='decay_processing_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


class CadDecayNoiseEstimate(BaseModel):
    """Sealed background-noise estimate for one decay analysis (#676 §3).

    The noise estimate is first-class evidence: tail interval, estimator
    identity, stationarity assumption, level, uncertainty and
    method/version. ``stationarity`` binds the assumption honestly — a
    ``nonstationary_detected`` tail never feeds a stationary-noise
    correction.
    """

    model_config = ConfigDict(frozen=True)

    estimate_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    rir_ref: AuthorityRef
    tail_start_s: float | None = None
    tail_end_s: float | None = None
    estimator: str = Field(min_length=1)
    method: NoiseFloorMethod
    stationarity: NoiseStationarity = 'unknown'
    level_db: float | None = None
    uncertainty_db: float | None = None
    method_version: str | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    estimate_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_estimate(self) -> 'CadDecayNoiseEstimate':
        _require_iso8601(
            self.declared_at_utc, 'noise estimate declared_at_utc'
        )
        for label, ref in (('profile_ref', self.profile_ref),
                           ('rir_ref', self.rir_ref)):
            if ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        for label, value in (
            ('tail_start_s', self.tail_start_s),
            ('tail_end_s', self.tail_end_s),
            ('level_db', self.level_db),
            ('uncertainty_db', self.uncertainty_db),
        ):
            if value is not None:
                _require_finite(value, f'noise estimate {label}')
        if (
            self.tail_start_s is not None
            and self.tail_end_s is not None
            and self.tail_end_s <= self.tail_start_s
        ):
            raise ValueError('noise tail interval must be ascending')
        if self.uncertainty_db is not None and self.uncertainty_db < 0:
            raise ValueError('noise uncertainty must be non-negative')
        expected = _hash(self.identity_payload())
        if self.estimate_sha256 != expected:
            raise ValueError('decay noise estimate hash mismatch')
        if self.estimate_id != _semantic_id('decnse', expected):
            raise ValueError(
                'decay noise estimate id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'rir_ref': self.rir_ref.model_dump(mode='json'),
            'tail_start_s': self.tail_start_s,
            'tail_end_s': self.tail_end_s,
            'estimator': self.estimator,
            'method': self.method,
            'stationarity': self.stationarity,
            'level_db': self.level_db,
            'uncertainty_db': self.uncertainty_db,
            'method_version': self.method_version,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def decay_noise_binding(
    estimate: CadDecayNoiseEstimate,
) -> AuthorityRef:
    return AuthorityRef(
        kind='decay_noise_estimate',
        ref_id=estimate.estimate_id,
        ref_sha256=estimate.estimate_sha256,
    )


class CadRirTruncationDecision(BaseModel):
    """Sealed RIR/EDC truncation decision (#676 §4).

    ``intersection_time_s`` is the estimated decay/noise crossing;
    ``truncation_time_s`` is where the analysis actually cut the record;
    ``available_decay_range_db`` is the usable decay above the noise
    floor. ``reason`` + ``capture_truncated`` keep a finite recording
    length distinct from a noise-limited decay — a capture that ends
    early is ``CAPTURE_TRUNCATED`` evidence, not a processed curve.
    """

    model_config = ConfigDict(frozen=True)

    decision_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    rir_ref: AuthorityRef
    noise_estimate_ref: AuthorityRef | None = None
    intersection_time_s: float | None = None
    truncation_time_s: float
    available_decay_range_db: float | None = None
    capture_length_s: float | None = None
    capture_truncated: bool = False
    reason: TruncationReason
    confidence: Literal['high', 'medium', 'low', 'unknown'] = 'unknown'
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    decision_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_decision(self) -> 'CadRirTruncationDecision':
        _require_iso8601(
            self.declared_at_utc, 'truncation decision declared_at_utc'
        )
        for label, ref in (
            ('profile_ref', self.profile_ref),
            ('rir_ref', self.rir_ref),
            ('noise_estimate_ref', self.noise_estimate_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        for label, value in (
            ('intersection_time_s', self.intersection_time_s),
            ('truncation_time_s', self.truncation_time_s),
            ('available_decay_range_db', self.available_decay_range_db),
            ('capture_length_s', self.capture_length_s),
        ):
            if value is not None:
                _require_finite(value, f'truncation decision {label}')
        if self.truncation_time_s <= 0:
            raise ValueError('truncation_time_s must be positive')
        if self.available_decay_range_db is not None and (
            self.available_decay_range_db < 0
        ):
            raise ValueError(
                'available_decay_range_db must be non-negative'
            )
        if self.capture_truncated and self.reason == 'noise_intersection':
            raise ValueError(
                'a capture that ended before the noise intersection is '
                'CAPTURE_TRUNCATED — reason "noise_intersection" claims a '
                'crossing that was never observed'
            )
        expected = _hash(self.identity_payload())
        if self.decision_sha256 != expected:
            raise ValueError('rir truncation decision hash mismatch')
        if self.decision_id != _semantic_id('dectrn', expected):
            raise ValueError(
                'rir truncation decision id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'rir_ref': self.rir_ref.model_dump(mode='json'),
            'noise_estimate_ref': (
                self.noise_estimate_ref.model_dump(mode='json')
                if self.noise_estimate_ref is not None
                else None
            ),
            'intersection_time_s': self.intersection_time_s,
            'truncation_time_s': self.truncation_time_s,
            'available_decay_range_db': self.available_decay_range_db,
            'capture_length_s': self.capture_length_s,
            'capture_truncated': self.capture_truncated,
            'reason': self.reason,
            'confidence': self.confidence,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def decay_truncation_binding(
    decision: CadRirTruncationDecision,
) -> AuthorityRef:
    return AuthorityRef(
        kind='rir_truncation_decision',
        ref_id=decision.decision_id,
        ref_sha256=decision.decision_sha256,
    )


class CadDecayEdcArtifact(BaseModel):
    """One retained energy-decay-curve artifact (#676 §5).

    ``edc_kind`` distinguishes the raw backward integral from compensated
    or tail-corrected curves; ``derived_from_ref`` hash-links a corrected
    EDC back to its raw integral, and ``compensation`` carries the exact
    applied model (formula identity, estimated compensation energy, fit
    interval) when a correction exists. The raw EDC is never overwritten —
    corrected curves are *new* derived artifacts.
    """

    model_config = ConfigDict(frozen=True)

    artifact_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    rir_ref: AuthorityRef
    edc_kind: EdcArtifactKind
    derived_from_ref: AuthorityRef | None = None
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    sample_count: int | None = None
    compensation: str | None = None
    compensation_energy_db: float | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_artifact(self) -> 'CadDecayEdcArtifact':
        _require_iso8601(
            self.declared_at_utc, 'edc artifact declared_at_utc'
        )
        for label, ref in (
            ('profile_ref', self.profile_ref),
            ('rir_ref', self.rir_ref),
            ('derived_from_ref', self.derived_from_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        if self.edc_kind == 'raw_backward_integral' and (
            self.derived_from_ref is not None
        ):
            raise ValueError(
                'the raw backward integral has no upstream EDC — it is '
                'the canonical derived artifact, not a derivation'
            )
        if self.edc_kind != 'raw_backward_integral' and (
            self.derived_from_ref is None
        ):
            raise ValueError(
                'a compensated/corrected EDC must name the raw artifact '
                'it derives from'
            )
        if self.sample_count is not None and self.sample_count < 1:
            raise ValueError('edc sample_count must be positive')
        if self.compensation_energy_db is not None:
            _require_finite(
                self.compensation_energy_db, 'compensation_energy_db'
            )
        expected = _hash(self.identity_payload())
        if self.artifact_sha256 != expected:
            raise ValueError('edc artifact hash mismatch')
        if self.artifact_id != _semantic_id('decedc', expected):
            raise ValueError('edc artifact id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'rir_ref': self.rir_ref.model_dump(mode='json'),
            'edc_kind': self.edc_kind,
            'derived_from_ref': (
                self.derived_from_ref.model_dump(mode='json')
                if self.derived_from_ref is not None
                else None
            ),
            'content_sha256': self.content_sha256,
            'sample_count': self.sample_count,
            'compensation': self.compensation,
            'compensation_energy_db': self.compensation_energy_db,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def decay_edc_binding(artifact: CadDecayEdcArtifact) -> AuthorityRef:
    return AuthorityRef(
        kind='decay_edc_artifact',
        ref_id=artifact.artifact_id,
        ref_sha256=artifact.artifact_sha256,
    )


class CadDecayFitRecord(BaseModel):
    """Sealed scalar decay-metric record (#676 §6/§7).

    The fitted segment is part of result identity: metric, fit level/time
    window, regression kind, sample count, dynamic range, noise margin,
    residual evidence and the sealed ``eligibility`` — a number without
    its eligibility state is not a qualified measurand. ``value_s`` is
    never discarded on ineligible verdicts: the record stays honest
    evidence of what the fit produced.
    """

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    rir_ref: AuthorityRef
    edc_ref: AuthorityRef | None = None
    noise_estimate_ref: AuthorityRef | None = None
    truncation_ref: AuthorityRef | None = None
    metric: DecayMetricKind
    custom_metric_label: str | None = None
    value_s: float | None = None
    fit_start_level_db: float | None = None
    fit_end_level_db: float | None = None
    fit_start_s: float | None = None
    fit_end_s: float | None = None
    regression: DecayRegressionKind
    sample_count: int | None = None
    dynamic_range_db: float | None = None
    noise_margin_db: float | None = None
    fit_residual_db: float | None = None
    eligibility: DecayEligibility
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    record_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_record(self) -> 'CadDecayFitRecord':
        _require_iso8601(
            self.evaluated_at_utc, 'decay fit record evaluated_at_utc'
        )
        for label, ref in (
            ('profile_ref', self.profile_ref),
            ('rir_ref', self.rir_ref),
            ('edc_ref', self.edc_ref),
            ('noise_estimate_ref', self.noise_estimate_ref),
            ('truncation_ref', self.truncation_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        if self.metric == 'custom' and not self.custom_metric_label:
            raise ValueError(
                'a custom decay metric requires its label — "custom" '
                'alone is not a metric identity'
            )
        for label, value in (
            ('value_s', self.value_s),
            ('fit_start_level_db', self.fit_start_level_db),
            ('fit_end_level_db', self.fit_end_level_db),
            ('fit_start_s', self.fit_start_s),
            ('fit_end_s', self.fit_end_s),
            ('dynamic_range_db', self.dynamic_range_db),
            ('noise_margin_db', self.noise_margin_db),
            ('fit_residual_db', self.fit_residual_db),
        ):
            if value is not None:
                _require_finite(value, f'decay fit {label}')
        if self.value_s is not None and self.value_s <= 0:
            raise ValueError('a decay time must be positive')
        if (
            self.fit_start_level_db is not None
            and self.fit_end_level_db is not None
            and self.fit_end_level_db >= self.fit_start_level_db
        ):
            raise ValueError('the decay fit window must descend')
        if (
            self.fit_start_s is not None
            and self.fit_end_s is not None
            and self.fit_end_s <= self.fit_start_s
        ):
            raise ValueError('the decay fit time window must ascend')
        if self.sample_count is not None and self.sample_count < 1:
            raise ValueError('decay fit sample_count must be positive')
        if self.dynamic_range_db is not None and self.dynamic_range_db < 0:
            raise ValueError('dynamic_range_db must be non-negative')
        if self.eligibility in _ELIGIBLE_STATES and self.value_s is None:
            raise ValueError(
                'an eligible decay metric requires its fitted value'
            )
        expected = _hash(self.identity_payload())
        if self.record_sha256 != expected:
            raise ValueError('decay fit record hash mismatch')
        if self.record_id != _semantic_id('decfit', expected):
            raise ValueError(
                'decay fit record id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'rir_ref': self.rir_ref.model_dump(mode='json'),
            'edc_ref': (
                self.edc_ref.model_dump(mode='json')
                if self.edc_ref is not None
                else None
            ),
            'noise_estimate_ref': (
                self.noise_estimate_ref.model_dump(mode='json')
                if self.noise_estimate_ref is not None
                else None
            ),
            'truncation_ref': (
                self.truncation_ref.model_dump(mode='json')
                if self.truncation_ref is not None
                else None
            ),
            'metric': self.metric,
            'custom_metric_label': self.custom_metric_label,
            'value_s': self.value_s,
            'fit_start_level_db': self.fit_start_level_db,
            'fit_end_level_db': self.fit_end_level_db,
            'fit_start_s': self.fit_start_s,
            'fit_end_s': self.fit_end_s,
            'regression': self.regression,
            'sample_count': self.sample_count,
            'dynamic_range_db': self.dynamic_range_db,
            'noise_margin_db': self.noise_margin_db,
            'fit_residual_db': self.fit_residual_db,
            'eligibility': self.eligibility,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }


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


def build_decay_processing_profile(
    *,
    document_id: str,
    profile_label: str,
    band: CadDecayBandSpec,
    backward_integration: BackwardIntegrationKind,
    noise_floor_method: NoiseFloorMethod,
    algorithm_version: str,
    tail_compensation: Literal[
        'none', 'declared_model', 'external', 'unknown'
    ] = 'unknown',
    fit_windows: tuple[CadDecayFitWindow, ...]
    | list[CadDecayFitWindow] = (),
    noise_stationarity_assumption: NoiseStationarity = 'unknown',
    measurement_or_simulated: Literal[
        'measured', 'simulated', 'unknown'
    ] = 'unknown',
    solver_truncation_declared: DecayTruncationCause = 'unknown',
    implementation_reference: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadDecayProcessingProfile:
    """Seal a decay-processing profile."""
    payload = dict(
        document_id=document_id,
        profile_label=profile_label,
        band=band,
        backward_integration=backward_integration,
        noise_floor_method=noise_floor_method,
        tail_compensation=tail_compensation,
        fit_windows=tuple(fit_windows),
        noise_stationarity_assumption=noise_stationarity_assumption,
        measurement_or_simulated=measurement_or_simulated,
        solver_truncation_declared=solver_truncation_declared,
        algorithm_version=algorithm_version,
        implementation_reference=implementation_reference,
        authority_version=DECAY_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadDecayProcessingProfile, payload,
        'profile_id', 'profile_sha256', 'decpro',
    )


def build_decay_noise_estimate(
    *,
    document_id: str,
    profile: CadDecayProcessingProfile,
    rir_ref: AuthorityRef,
    estimator: str,
    method: NoiseFloorMethod,
    tail_start_s: float | None = None,
    tail_end_s: float | None = None,
    stationarity: NoiseStationarity = 'unknown',
    level_db: float | None = None,
    uncertainty_db: float | None = None,
    method_version: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadDecayNoiseEstimate:
    """Seal a background-noise estimate."""
    payload = dict(
        document_id=document_id,
        profile_ref=decay_profile_binding(profile),
        rir_ref=rir_ref,
        tail_start_s=tail_start_s,
        tail_end_s=tail_end_s,
        estimator=estimator,
        method=method,
        stationarity=stationarity,
        level_db=level_db,
        uncertainty_db=uncertainty_db,
        method_version=method_version,
        authority_version=DECAY_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadDecayNoiseEstimate, payload,
        'estimate_id', 'estimate_sha256', 'decnse',
    )


def build_rir_truncation_decision(
    *,
    document_id: str,
    profile: CadDecayProcessingProfile,
    rir_ref: AuthorityRef,
    truncation_time_s: float,
    reason: TruncationReason,
    noise_estimate: CadDecayNoiseEstimate | None = None,
    intersection_time_s: float | None = None,
    available_decay_range_db: float | None = None,
    capture_length_s: float | None = None,
    capture_truncated: bool = False,
    confidence: Literal['high', 'medium', 'low', 'unknown'] = 'unknown',
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadRirTruncationDecision:
    """Seal an RIR/EDC truncation decision."""
    payload = dict(
        document_id=document_id,
        profile_ref=decay_profile_binding(profile),
        rir_ref=rir_ref,
        noise_estimate_ref=(
            decay_noise_binding(noise_estimate)
            if noise_estimate is not None
            else None
        ),
        intersection_time_s=intersection_time_s,
        truncation_time_s=truncation_time_s,
        available_decay_range_db=available_decay_range_db,
        capture_length_s=capture_length_s,
        capture_truncated=capture_truncated,
        reason=reason,
        confidence=confidence,
        authority_version=DECAY_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadRirTruncationDecision, payload,
        'decision_id', 'decision_sha256', 'dectrn',
    )


def build_decay_edc_artifact(
    *,
    document_id: str,
    profile: CadDecayProcessingProfile,
    rir_ref: AuthorityRef,
    edc_kind: EdcArtifactKind,
    content_sha256: str,
    derived_from_ref: AuthorityRef | CadDecayEdcArtifact | None = None,
    sample_count: int | None = None,
    compensation: str | None = None,
    compensation_energy_db: float | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadDecayEdcArtifact:
    """Seal one retained EDC artifact (raw or derived)."""
    if isinstance(derived_from_ref, CadDecayEdcArtifact):
        derived_from_ref = decay_edc_binding(derived_from_ref)
    payload = dict(
        document_id=document_id,
        profile_ref=decay_profile_binding(profile),
        rir_ref=rir_ref,
        edc_kind=edc_kind,
        derived_from_ref=derived_from_ref,
        content_sha256=content_sha256,
        sample_count=sample_count,
        compensation=compensation,
        compensation_energy_db=compensation_energy_db,
        authority_version=DECAY_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadDecayEdcArtifact, payload,
        'artifact_id', 'artifact_sha256', 'decedc',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_decay_fit(
    *,
    document_id: str,
    profile: CadDecayProcessingProfile,
    rir_ref: AuthorityRef,
    metric: DecayMetricKind,
    regression: DecayRegressionKind,
    value_s: float | None = None,
    edc_artifact: CadDecayEdcArtifact | None = None,
    noise_estimate: CadDecayNoiseEstimate | None = None,
    truncation: CadRirTruncationDecision | None = None,
    fit_start_level_db: float | None = None,
    fit_end_level_db: float | None = None,
    fit_start_s: float | None = None,
    fit_end_s: float | None = None,
    sample_count: int | None = None,
    dynamic_range_db: float | None = None,
    noise_margin_db: float | None = None,
    fit_residual_db: float | None = None,
    custom_metric_label: str | None = None,
    multi_slope_detected: bool = False,
    modal_method_required: bool = False,
    evaluated_at_utc: str | None = None,
) -> CadDecayFitRecord:
    """Fail-closed decay-metric eligibility verdict (#676 §7).

    Derivation order: capture-truncated and modal/multi-slope gates beat
    range checks; a nonstationary noise estimate degrades any stationary
    correction; insufficient dynamic range below the declared window is a
    state, not a number. The fitted value is retained on every verdict —
    an ineligible metric is evidence, not silence.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []

    window = profile.window_for(metric)
    required_range_db = (
        window.min_dynamic_range_db
        if window is not None and window.min_dynamic_range_db is not None
        else (
            abs(window.end_level_db - window.start_level_db)
            if window is not None
            else None
        )
    )

    capture_truncated = (
        truncation is not None and truncation.capture_truncated
    )
    nonstationary = (
        noise_estimate is not None
        and noise_estimate.stationarity == 'nonstationary_detected'
    )
    noise_unqualified = (
        profile.noise_floor_method != 'none_declared'
        and noise_estimate is None
        and profile.measurement_or_simulated == 'measured'
    )
    available_range = (
        truncation.available_decay_range_db
        if truncation is not None
        else None
    )
    range_source = (
        dynamic_range_db
        if dynamic_range_db is not None
        else available_range
    )
    insufficient_range = (
        required_range_db is not None
        and range_source is not None
        and range_source < required_range_db
    )
    low_margin = noise_margin_db is not None and noise_margin_db < 0

    if modal_method_required:
        eligibility: DecayEligibility = 'modal_method_required'
        reasons.append(
            'the #571 modal/low-frequency gate requires a modal decay '
            'method — a conventional band EDC slope is not this metric'
        )
    elif multi_slope_detected:
        eligibility = 'multi_slope_model_mismatch'
        reasons.append(
            'a coupled-space multi-slope decay cannot be honestly '
            'reported as one slope (#671)'
        )
    elif capture_truncated:
        eligibility = 'capture_truncated'
        reasons.append(
            'the capture ended before the decay/noise intersection — a '
            'finite recording length is not a processed noise limit'
        )
    elif nonstationary:
        eligibility = 'non_stationary_noise'
        reasons.append(
            'the noise tail is nonstationary — a stationary-noise '
            'correction must not be applied blindly (#573/#580)'
        )
    elif insufficient_range:
        eligibility = 'insufficient_decay_range'
        reasons.append(
            f'usable decay range {range_source} dB is below the required '
            f'{required_range_db} dB window — the fit ran but the metric '
            'is not a qualified measurand'
        )
    elif low_margin:
        eligibility = 'noise_floor_too_high'
        reasons.append(
            'the fitted window reaches below the noise floor'
        )
    elif noise_unqualified:
        eligibility = 'indeterminate'
        reasons.append(
            'the profile declares a noise-floor method but no estimate '
            'was supplied — the tail handling is unevidenced'
        )
    elif (
        truncation is not None
        and truncation.reason == 'capture_end'
        and truncation.confidence in ('low', 'unknown')
    ) or noise_estimate is None or truncation is None:
        eligibility = 'eligible_with_limitations'
        reasons.append(
            'partially unevidenced processing chain — the metric is '
            'usable but its noise/truncation provenance is incomplete'
        )
    else:
        eligibility = 'eligible'

    if (
        fit_residual_db is not None
        and eligibility == 'eligible'
        and fit_residual_db > 1.0
    ):
        eligibility = 'eligible_with_limitations'
        reasons.append(
            'the fit residual exceeds 1 dB — usable with a declared '
            'goodness limitation'
        )

    payload = dict(
        document_id=document_id,
        profile_ref=decay_profile_binding(profile),
        rir_ref=rir_ref,
        edc_ref=(
            decay_edc_binding(edc_artifact)
            if edc_artifact is not None
            else None
        ),
        noise_estimate_ref=(
            decay_noise_binding(noise_estimate)
            if noise_estimate is not None
            else None
        ),
        truncation_ref=(
            decay_truncation_binding(truncation)
            if truncation is not None
            else None
        ),
        metric=metric,
        custom_metric_label=custom_metric_label,
        value_s=value_s,
        fit_start_level_db=(
            fit_start_level_db
            if fit_start_level_db is not None
            else (window.start_level_db if window is not None else None)
        ),
        fit_end_level_db=(
            fit_end_level_db
            if fit_end_level_db is not None
            else (window.end_level_db if window is not None else None)
        ),
        fit_start_s=fit_start_s,
        fit_end_s=fit_end_s,
        regression=regression,
        sample_count=sample_count,
        dynamic_range_db=dynamic_range_db,
        noise_margin_db=noise_margin_db,
        fit_residual_db=fit_residual_db,
        eligibility=eligibility,
        reasons=tuple(reasons),
        evaluation_version=DECAY_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadDecayFitRecord, payload,
        'record_id', 'record_sha256', 'decfit',
    )


__all__ = [
    'BackwardIntegrationKind',
    'CadDecayBandSpec',
    'CadDecayEdcArtifact',
    'CadDecayFitRecord',
    'CadDecayFitWindow',
    'CadDecayNoiseEstimate',
    'CadDecayProcessingProfile',
    'CadRirTruncationDecision',
    'DECAY_AUTHORITY_SCHEMA_VERSION',
    'DECAY_EVALUATION_VERSION',
    'DecayBandKind',
    'DecayEligibility',
    'DecayFilterClass',
    'DecayMetricKind',
    'DecayRegressionKind',
    'DecayTruncationCause',
    'EdcArtifactKind',
    'NoiseFloorMethod',
    'NoiseStationarity',
    'TruncationReason',
    'build_decay_edc_artifact',
    'build_decay_noise_estimate',
    'build_decay_processing_profile',
    'build_rir_truncation_decision',
    'decay_edc_binding',
    'decay_noise_binding',
    'decay_profile_binding',
    'decay_truncation_binding',
    'evaluate_decay_fit',
]
