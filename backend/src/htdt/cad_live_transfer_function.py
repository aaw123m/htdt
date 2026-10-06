"""Live dual-channel transfer-function authority (issue #663).

A live TF session (simultaneous reference + measurement channels,
magnitude/phase/coherence/live-IR, delay find/track during alignment)
is operational surface, not immutable evidence. These records pin the
reference identity, estimator semantics, averaging, delay authority
and the live→capture promotion boundary so a live graph is never
treated as a reproducible measurement record.

Basis: Rational Acoustics Smaart Suite/RT feature documentation
(dual-channel TF, coherence, live IR, delay finder/tracking, MTW);
Rational coherence guidance (coherence is diagnostic, not a quality
score); Brüel & Kjær dual-channel FFT technical reviews (BV0013/
BV0014 — TF estimate error vs coherence and averages).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


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


ReferenceKind = Literal[
    'electrical_loopback',
    'digital_reference',
    'processor_output_tap',
    'known_playback_reference',
    'other_simultaneous',
    'unknown',
]

ClockTopology = Literal[
    'common_clock',
    'digitally_locked',
    'async_uncorrected',
    'unknown',
]

TFEstimator = Literal[
    'h1_gxy_gxx',
    'h2_gyy_gyx',
    'cross_spectral_other',
    'provider_defined',
    'unknown',
]

MtwMethod = Literal[
    'single_fft',
    'multi_time_window',
    'provider_hybrid',
    'unknown',
]

AveragingKind = Literal[
    'finite_fifo',
    'progressive_exponential',
    'infinite_running',
    'none',
    'unknown',
]

CaptureState = Literal[
    'live_ephemeral',
    'captured_snapshot',
    'qualified_capture',
]

DelayMethod = Literal[
    'manual_fixed',
    'auto_found',
    'auto_tracked',
    'external_known',
    'unknown',
]

MaskedChangeKind = Literal[
    'none',
    'suspected',
    'reference_path_drift',
    'microphone_position_change',
    'device_latency_change',
    'clock_rate_drift',
    'source_delay_change',
    'unknown',
]

LowCoherenceCause = Literal[
    'reference_delay_error',
    'low_measurement_snr',
    'uncorrelated_background_noise',
    'system_nonlinearity',
    'time_variation',
    'reverberant_window_effect',
    'clock_drift',
    'analysis_leakage',
    'insufficient_averaging',
    'unknown',
]


class LiveTransferFunctionSession(BaseModel):
    """Versioned live TF session identity (#663 §1-§2).

    Binds the simultaneous reference and measurement channels, the
    clock topology (#609) and the operator/system state. 'unknown'
    reference or clock fails closed for coherent claims.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str
    session_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    scene_ref: AuthorityRef | None = None
    reference_kind: ReferenceKind
    reference_channel: str
    measurement_channels: tuple[str, ...]
    output_route: str | None = None
    interface_identity: str | None = None
    sample_rate_hz: float
    clock_topology: ClockTopology
    method_tool: str | None = None
    operator: str | None = None
    system_state_ref: AuthorityRef | None = None
    started_at_utc: str | None = None
    ended_at_utc: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('reference_kind') in (None, 'unknown'):
                raise ValueError(
                    'the simultaneous reference signal must be '
                    'declared — loopback, digital ref, tap or known '
                    'playback'
                )
            if not data.get('measurement_channels'):
                raise ValueError(
                    'a live TF session requires at least one '
                    'measurement channel'
                )
            if not data.get('reference_channel'):
                raise ValueError(
                    'a live TF session requires the reference channel'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'session_id', 'session_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'LiveTransferFunctionSession':
        return _seal(
            cls, payload, 'session_id', 'session_sha256', 'lts'
        )


class DualChannelTFObservation(BaseModel):
    """One live TF observation and its capture-promotion state (#663).

    Estimator identity (H1/H2/other), FFT/window/MTW and averaging
    semantics are part of the observation. ``qualified_capture``
    requires the delay, coherence, timebase and stimulus/state refs
    to be pinned with it.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    session_ref: AuthorityRef
    estimator: TFEstimator
    mtw_method: MtwMethod
    averaging: AveragingKind
    capture_state: CaptureState
    fft_size: int | None = None
    window: str | None = None
    overlap_fraction: float | None = None
    averaging_depth: int | None = None
    averaging_time_constant_s: float | None = None
    smoothing_fraction_oct: float | None = None
    frequency_resolution_hz: float | None = None
    tf_payload_ref: AuthorityRef | None = None
    delay_ref: AuthorityRef | None = None
    coherence_ref: AuthorityRef | None = None
    timebase_ref: AuthorityRef | None = None
    stimulus_state_ref: AuthorityRef | None = None
    observed_at_utc: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('session_ref') is None:
                raise ValueError(
                    'a TF observation requires its live session'
                )
            if data.get('estimator') in (None, 'unknown'):
                raise ValueError(
                    'the transfer-function estimator must be declared '
                    '— a ratio of spectra is not an estimator identity'
                )
            if data.get('mtw_method') in (None, 'unknown'):
                raise ValueError(
                    'the FFT/MTW method must be declared — MTW and '
                    'single-FFT traces are not interchangeable'
                )
            if data.get('averaging') in (None, 'unknown'):
                raise ValueError(
                    'averaging semantics must be declared — a live '
                    'average over changing conditions is a different '
                    'measurand'
                )
            if data.get('capture_state') == 'qualified_capture':
                missing = [
                    name
                    for name in (
                        'tf_payload_ref', 'delay_ref', 'coherence_ref',
                        'timebase_ref', 'stimulus_state_ref',
                    )
                    if data.get(name) is None
                ]
                if missing:
                    raise ValueError(
                        'a qualified capture must pin: '
                        + ', '.join(missing)
                    )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'},
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'DualChannelTFObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'dto'
        )


class CoherenceObservation(BaseModel):
    """Coherence trace as a diagnostic observable (#663 §4-§5).

    Coherence is not a pass/fail sound-quality score; low-coherence
    causes stay non-exclusive until evidence isolates them, and
    display blanking never deletes the underlying trace.
    """

    model_config = ConfigDict(frozen=True)

    coherence_id: str
    coherence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    observation_ref: AuthorityRef
    raw_trace_ref: AuthorityRef
    frequency_grid_ref: AuthorityRef | None = None
    averaging_depth: int | None = None
    estimator_window: str | None = None
    display_blanking_policy: Literal[
        'none', 'threshold_blank', 'provider_defined', 'unknown'
    ] = 'none'
    blanking_threshold: float | None = None
    possible_causes: tuple[LowCoherenceCause, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('observation_ref') is None:
                raise ValueError(
                    'coherence evidence requires its TF observation'
                )
            if data.get('raw_trace_ref') is None:
                raise ValueError(
                    'the raw coherence trace must be preserved — '
                    'display blanking is not data deletion'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'coherence_id', 'coherence_sha256'},
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'CoherenceObservation':
        return _seal(
            cls, payload, 'coherence_id', 'coherence_sha256', 'coh'
        )


class ReferenceDelayTrack(BaseModel):
    """One reference-delay value with provenance (#663 §6-§7).

    Every delay change is timestamped and method-qualified. Automatic
    tracking that moves materially must expose what it may have
    absorbed — a tracker must not hide a speaker move, DSP latency or
    routing change.
    """

    model_config = ConfigDict(frozen=True)

    track_id: str
    track_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    session_ref: AuthorityRef
    delay_ms: float
    method: DelayMethod
    masked_change: MaskedChangeKind = 'none'
    effective_at_utc: str | None = None
    search_band_hz: tuple[float, float] | None = None
    confidence: float | None = None
    source_observation_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('session_ref') is None:
                raise ValueError(
                    'a delay track requires its live session'
                )
            if data.get('method') in (None, 'unknown'):
                raise ValueError(
                    'the applied delay must declare its provenance — '
                    'fixed, auto-found, auto-tracked or external'
                )
            if data.get('method') == 'auto_tracked' and (
                data.get('masked_change') in ('suspected', 'unknown')
            ) and data.get('source_observation_ref') is None:
                raise ValueError(
                    'a materially moving delay tracker requires the '
                    'source observation that motivated it'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'track_id', 'track_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'ReferenceDelayTrack':
        return _seal(
            cls, payload, 'track_id', 'track_sha256', 'rdt'
        )


CaptureVerdict = Literal[
    'qualified_capture',
    'captured_evidence',
    'ephemeral_only',
    'clock_limited',
    'estimator_unqualified',
    'tracker_masked_change',
    'delay_unqualified',
]


def evaluate_capture_claim(
    session: LiveTransferFunctionSession | None,
    observation: DualChannelTFObservation | None,
    delay_track: ReferenceDelayTrack | None = None,
) -> tuple[CaptureVerdict, str]:
    """Judge whether a live observation is promotable evidence (#663)."""
    if observation is None:
        return (
            'ephemeral_only',
            'no observation record — a live view is not evidence',
        )
    if session is None:
        return (
            'ephemeral_only',
            'no session identity — channels/clock/state unpinned',
        )
    if observation.estimator == 'provider_defined':
        return (
            'estimator_unqualified',
            'provider-defined estimator without exact spectral '
            'semantics',
        )
    if session.clock_topology in ('async_uncorrected', 'unknown'):
        return (
            'clock_limited',
            'common-clock/digital-lock evidence missing — phase, '
            'delay and live-IR claims are denied while magnitude '
            'use may remain',
        )
    if (
        delay_track is not None
        and delay_track.method == 'auto_tracked'
        and delay_track.masked_change not in ('none',)
    ):
        return (
            'tracker_masked_change',
            'the delay tracker moved materially — a physical/device '
            'change may be hidden in the track',
        )
    if observation.capture_state == 'live_ephemeral':
        return (
            'ephemeral_only',
            'live ephemeral data — promotion requires an explicit '
            'capture event with all state pinned',
        )
    if delay_track is None:
        return (
            'delay_unqualified',
            'no reference-delay record — phase/IR interpretation is '
            'unpinned',
        )
    if observation.capture_state == 'qualified_capture':
        return (
            'qualified_capture',
            'captured with delay, coherence, timebase and state '
            'pinned — commissionable evidence',
        )
    return (
        'captured_evidence',
        'captured snapshot — evidence-bound but not a fully '
        'qualified capture',
    )


CAPTURE_LABELS: dict[str, str] = {
    'qualified_capture': '適格キャプチャ',
    'captured_evidence': '捕捉証拠',
    'ephemeral_only': '一時表示のみ',
    'clock_limited': 'クロック限定',
    'estimator_unqualified': '推定器未適格',
    'tracker_masked_change': '追跡隠蔽変更',
    'delay_unqualified': '遅延未適格',
}
