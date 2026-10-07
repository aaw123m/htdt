"""Single-channel live-observation authority (issue #793).

Field tuning depends on continuous observables that #663's dual-channel
transfer function does not own: single-channel real-time spectrum / RTA,
spectrograph, SPL/Leq history, peak/max/min hold and event logging while
HVAC, projector, DSP, routing, gain or room state changes. A live view
is operational surface, not immutable evidence — a technician seeing a
peak briefly is not a capture. This module pins the instrument/calibration/
timebase binding, the exact #749 estimator parameters, the IEC 61260
banding eligibility, the SPL/Leq quantity semantics, explicit time-history
gaps, and the live→capture promotion boundary.

Basis: issue #793 scope; IEC 61672-1:2013 (sound-level-meter performance,
stability 2029); IEC 61672-3:2013 (periodic tests, stability 2029);
IEC 61260-1:2014 (octave/fractional-octave filters, stability 2030 —
explicitly applicable to spectrum analyzers); Smaart RT/Suite/SPL and
Open Sound Meter feature surveys (issue text).

Boundaries held here: #663 owns dual-channel transfer function (this
authority never claims TF semantics); #749 owns estimator mathematics
(live spectra reference and pin its parameters, never re-derive them);
#580 owns background-noise qualification (a momentary RTA is a preview,
never qualified noise evidence); #602 owns exposure interpretation;
#575/#581 own spatial combination semantics (a multi-input display never
silently becomes a spatial average); #598 owns remote transport.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_spectral_estimator import WindowKind
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


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


LiveMeasurementMode = Literal[
    'realtime_spectrum',
    'fractional_octave_rta',
    'spectrograph',
    'spl_instantaneous',
    'spl_time_weighted',
    'leq_time_averaged',
    'peak_max_min_hold',
    'time_history',
    'other_profiled_live_observable',
]

CaptureState = Literal[
    'live_view_only',
    'captured_trace',
    'captured_session',
    'derived_summary',
]

CaptureKind = Literal[
    'captured_trace',
    'captured_session',
    'derived_summary',
]

BandingEligibility = Literal[
    'iec_61260_class_1_eligible',
    'iec_61260_class_2_eligible',
    'project_defined_banding',
    'ppo_display_only',
    'unknown',
]

FrequencyWeighting = Literal['a', 'c', 'z', 'other', 'unknown']

TimeWeighting = Literal[
    'fast', 'slow', 'impulse', 'profile_specific', 'unknown',
]

HistoryQuantity = Literal[
    'spl_instantaneous',
    'spl_time_weighted',
    'leq',
    'peak',
    'max_hold',
    'min_hold',
    'other',
]

HistoryPointKind = Literal[
    'measured',
    'aggregate',
    'missing',
    'session_boundary',
    'calibration_changed',
]

LiveVerdict = Literal[
    'live_evidence_bound',
    'diagnostic_only',
    'uncalibrated',
    'alert_denied_uncalibrated',
    'overload_limited',
    'estimator_unqualified',
    'banding_display_only',
    'unweighted_quantity',
    'insufficient_evidence',
]

InstrumentBinding = Literal[
    'instrument_bound',
    'uncalibrated',
    'diagnostic_only',
    'timebase_unbound',
    'insufficient_evidence',
]

CaptureVerdict = Literal[
    'qualified_capture',
    'captured_evidence',
    'live_view_only',
    'diagnostic_only',
    'uncalibrated_claim',
    'payload_not_canonical',
]

HistoryVerdict = Literal[
    'history_complete',
    'history_with_declared_gaps',
    'gap_unmarked',
    'insufficient_evidence',
]

CorrelationVerdict = Literal[
    'contextual_correlation',
    'annotation_only',
    'no_correlation',
]

NoiseEligibility = Literal[
    'captured_noise_candidate',
    'preview_only',
]

ComparisonVerdict = Literal[
    'comparable',
    'settings_differ',
    'insufficient_evidence',
]


LIVE_LABELS: dict[str, str] = {
    'live_evidence_bound': 'ライブ証拠拘束済み',
    'instrument_bound': '測定系拘束済み',
    'diagnostic_only': '診断表示のみ',
    'uncalibrated': '未校正',
    'alert_denied_uncalibrated': '未校正のためアラート拒否',
    'overload_limited': 'オーバーロード限定',
    'estimator_unqualified': '推定器未適格',
    'banding_display_only': '帯域表示のみ',
    'unweighted_quantity': '量意味未宣言',
    'timebase_unbound': '時間基準未拘束',
    'insufficient_evidence': '証拠不足',
    'qualified_capture': '適格キャプチャ',
    'captured_evidence': '捕捉証拠',
    'live_view_only': 'ライブ表示のみ',
    'uncalibrated_claim': '未校正クレーム',
    'payload_not_canonical': '正準ペイロードなし',
    'history_complete': '履歴完全',
    'history_with_declared_gaps': '宣言欠測あり履歴',
    'gap_unmarked': '未宣言の欠測',
    'contextual_correlation': '文脈相関（因果ではない）',
    'annotation_only': '注釈のみ',
    'no_correlation': '相関なし',
    'captured_noise_candidate': '騒音証拠候補（捕捉済み）',
    'preview_only': 'プレビューのみ',
    'comparable': '比較可能',
    'settings_differ': '測定設定差異',
    'unknown': '不明',
}


# ---------------------------------------------------------------------------
# Nested value bindings — pinned inside the sealed records, not themselves
# sealed rows.
# ---------------------------------------------------------------------------


class SpectrumEstimatorBinding(BaseModel):
    """Exact #749 estimator parameters a live observation ran under (#3).

    #749 remains canonical for the mathematics; a live spectrum pins the
    declared profile plus the running parameters. A screenshot is never
    the canonical spectrum result.
    """

    model_config = ConfigDict(frozen=True)

    estimator_ref: AuthorityRef | None = None
    fft_size: int | None = None
    window_kind: WindowKind = 'unknown'
    record_length_s: float | None = None
    overlap_fraction: float | None = None
    averaging: str | None = None
    detector: str | None = None
    power_scaling: str | None = None
    frequency_resolution_hz: float | None = None
    smoothing_fraction_oct: float | None = None
    peak_hold_enabled: bool = False
    update_rate_hz: float | None = None
    estimator_version: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'SpectrumEstimatorBinding':
        if self.estimator_ref is not None:
            _require_refs(self.estimator_ref)
        if self.fft_size is not None and self.fft_size <= 0:
            raise ValueError('fft_size must be positive')
        if self.record_length_s is not None \
                and self.record_length_s <= 0.0:
            raise ValueError('record_length_s must be positive')
        if self.overlap_fraction is not None and not (
                0.0 <= self.overlap_fraction < 1.0):
            raise ValueError('overlap_fraction must be in [0, 1)')
        if self.frequency_resolution_hz is not None \
                and self.frequency_resolution_hz <= 0.0:
            raise ValueError('frequency_resolution_hz must be positive')
        if self.smoothing_fraction_oct is not None \
                and self.smoothing_fraction_oct < 0.0:
            raise ValueError('smoothing_fraction_oct must be >= 0')
        if self.update_rate_hz is not None and self.update_rate_hz <= 0.0:
            raise ValueError('update_rate_hz must be positive')
        return self

    def has_estimator_identity(self) -> bool:
        """An estimator is only declared when the profile or the running
        parameters are pinned — 'the app did an FFT' is not an identity."""
        return (
            self.estimator_ref is not None
            or self.fft_size is not None
            or self.record_length_s is not None
        )


class FractionalOctaveBinding(BaseModel):
    """Fractional-octave filter semantics (#4).

    IEC 61260 class eligibility requires both the pinned standard
    revision AND bound class evidence — an FFT binned into approximate
    third-octave bars is at most a display approximation, never a Class
    1/2 analyzer from software math alone.
    """

    model_config = ConfigDict(frozen=True)

    banding_eligibility: BandingEligibility
    band_fraction: str | None = None
    filter_implementation: str | None = None
    standard_reference: str | None = None
    class_evidence_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'FractionalOctaveBinding':
        for ref in self.class_evidence_refs:
            _require_refs(ref)
        if self.banding_eligibility in (
                'iec_61260_class_1_eligible', 'iec_61260_class_2_eligible'):
            if not self.standard_reference or not (
                    self.standard_reference.startswith('IEC 61260-1@')):
                raise ValueError(
                    'IEC 61260 class eligibility requires the pinned '
                    'standard revision (IEC 61260-1@<edition>)')
            if not self.class_evidence_refs:
                raise ValueError(
                    'IEC 61260 class eligibility requires bound class '
                    'evidence — software math alone is not a Class 1/2 '
                    'analyzer')
        return self


class SplQuantityBinding(BaseModel):
    """SPL/Leq quantity semantics (#5).

    Persisted so a bare '92 dB' can never be emitted: frequency weighting,
    time weighting, integration/Leq interval, peak detector semantics and
    the max/min reset state are part of the quantity.
    """

    model_config = ConfigDict(frozen=True)

    frequency_weighting: FrequencyWeighting
    time_weighting: TimeWeighting = 'unknown'
    integration_interval_s: float | None = None
    leq_interval_s: float | None = None
    peak_detector_semantics: str | None = None
    max_min_reset_state: Literal[
        'initial', 'mid_history_reset', 'running_unreset', 'unknown',
    ] = 'unknown'
    reference_pressure: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'SplQuantityBinding':
        if self.integration_interval_s is not None \
                and self.integration_interval_s <= 0.0:
            raise ValueError('integration_interval_s must be positive')
        if self.leq_interval_s is not None and self.leq_interval_s <= 0.0:
            raise ValueError('leq_interval_s must be positive')
        return self


class SpectrographBinding(BaseModel):
    """Time-frequency (spectrograph) parameters (#7).

    The display color palette is visualization metadata — it rides along
    but is never part of the measurement claim.
    """

    model_config = ConfigDict(frozen=True)

    method: Literal['fft', 'band_filter', 'other', 'unknown']
    window_kind: WindowKind = 'unknown'
    time_step_s: float | None = None
    frequency_grid: str | None = None
    frequency_grid_ref: AuthorityRef | None = None
    dynamic_range_db: float | None = None
    weighting: FrequencyWeighting = 'unknown'
    normalization: str | None = None
    display_palette: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'SpectrographBinding':
        if self.frequency_grid_ref is not None:
            _require_refs(self.frequency_grid_ref)
        if self.time_step_s is not None and self.time_step_s <= 0.0:
            raise ValueError('time_step_s must be positive')
        if self.dynamic_range_db is not None \
                and self.dynamic_range_db <= 0.0:
            raise ValueError('dynamic_range_db must be positive')
        return self


class TimeHistoryPoint(BaseModel):
    """One point of a canonical live history (#6).

    ``missing`` and ``session_boundary`` points carry no level — a
    continuous line may never be drawn through a capture gap or a session
    boundary without the marker. ``calibration_changed`` marks the exact
    offset where the chain changed.
    """

    model_config = ConfigDict(frozen=True)

    offset_s: float
    kind: HistoryPointKind
    value_db: float | None = None
    marker: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'TimeHistoryPoint':
        if self.offset_s < 0.0:
            raise ValueError('offset_s must be >= 0')
        if self.kind in ('missing', 'session_boundary'):
            if self.value_db is not None:
                raise ValueError(
                    f'a {self.kind} point cannot carry a level value')
        if self.kind in ('measured', 'aggregate') \
                and self.value_db is None:
            raise ValueError(
                f'a {self.kind} point requires value_db')
        if self.kind in ('missing', 'calibration_changed') \
                and not self.marker:
            raise ValueError(
                f'a {self.kind} point requires a marker reason')
        return self


class LiveCaptureSettings(BaseModel):
    """Parameter snapshot sealed into a session-level capture (#16)."""

    model_config = ConfigDict(frozen=True)

    active_modes: tuple[LiveMeasurementMode, ...] = ()
    spectrum: SpectrumEstimatorBinding | None = None
    banding: FractionalOctaveBinding | None = None
    spl: SplQuantityBinding | None = None
    spectrograph: SpectrographBinding | None = None

    def comparison_payload(self) -> dict[str, Any]:
        """Settings minus session-specific refs — two traces captured on
        different sessions compare reproducibly only when this payload's
        digest matches (#16, 'compare reproducibly with another session').
        """
        def strip(value: Any) -> Any:
            if isinstance(value, BaseModel):
                return {
                    key: strip(item)
                    for key, item in value.model_dump(mode='python').items()
                    if 'ref' not in key
                }
            if isinstance(value, (tuple, list)):
                return [strip(item) for item in value]
            return value
        return strip(self)


# ---------------------------------------------------------------------------
# Sealed record types
# ---------------------------------------------------------------------------


class RealtimeMeasurementSession(BaseModel):
    """Versioned live single-channel session identity (#2, rms- prefix).

    Binds the exact monitored input: microphone/input channel, #611
    calibration, #699 interface/path correction, gain/range, #695
    measurement-chain linearity, #609 sample rate/timebase, #732
    orientation/incidence and the receiver location. A session without
    instrument identity is diagnostic-only — never rejected outright, but
    no evaluator will call its observations evidence.

    ``channel_semantics`` is pinned to ``single_channel``: dual-channel
    transfer-function claims belong to #663 and can never be smuggled in
    here (#11).
    """

    model_config = ConfigDict(frozen=True)

    session_id: str
    session_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    input_channel: str
    channel_semantics: Literal['single_channel'] = 'single_channel'
    input_domain: Literal[
        'acoustic_pressure', 'line_level', 'other', 'unknown',
    ] = 'unknown'
    instrument_ref: AuthorityRef | None = None
    calibration_ref: AuthorityRef | None = None
    interface_path_ref: AuthorityRef | None = None
    linearity_ref: AuthorityRef | None = None
    timebase_ref: AuthorityRef | None = None
    orientation_ref: AuthorityRef | None = None
    receiver_location_ref: AuthorityRef | None = None
    gain_range_state: str | None = None
    sample_rate_hz: float | None = None
    active_modes: tuple[LiveMeasurementMode, ...] = ()
    system_state_ref: AuthorityRef | None = None
    operator: str | None = None
    started_at_utc: str | None = None
    ended_at_utc: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'RealtimeMeasurementSession':
        if not self.input_channel:
            raise ValueError(
                'a live session must declare its input channel — '
                'an unattributed input is not an instrument identity')
        for ref in (
                self.instrument_ref, self.calibration_ref,
                self.interface_path_ref, self.linearity_ref,
                self.timebase_ref, self.orientation_ref,
                self.receiver_location_ref, self.system_state_ref):
            if ref is not None:
                _require_refs(ref)
        if self.sample_rate_hz is not None and self.sample_rate_hz <= 0.0:
            raise ValueError('sample_rate_hz must be positive')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'session_id', 'session_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'RealtimeMeasurementSession':
        return _seal(cls, payload, 'session_id', 'session_sha256', 'rms')

    def has_instrument_identity(self) -> bool:
        return self.instrument_ref is not None


class LiveEventAnnotation(BaseModel):
    """Time-bound operator/system annotation (#9, lea- prefix).

    Annotations are hypotheses and context, never automatic causality:
    ``claim_kind`` admits only contextual values, so an annotation record
    structurally cannot assert that a state change caused an observation.
    Composition with #573 state stability and #719 diagnostic hypotheses
    is via sha-pinned refs.
    """

    model_config = ConfigDict(frozen=True)

    annotation_id: str
    annotation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    session_ref: AuthorityRef
    text: str
    operator: str
    source: Literal['operator', 'system', 'imported', 'other'] = 'other'
    claim_kind: Literal[
        'contextual_observation', 'hypothesis', 'other',
    ] = 'contextual_observation'
    observed_at_utc: str
    onset_s: float | None = None
    offset_s: float | None = None
    related_state_ref: AuthorityRef | None = None
    related_hypothesis_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'LiveEventAnnotation':
        _require_refs(self.session_ref)
        if not self.text:
            raise ValueError('an annotation requires its text')
        if not self.operator:
            raise ValueError(
                'an annotation requires its operator/source attribution')
        if not self.observed_at_utc:
            raise ValueError('an annotation requires its time')
        for ref in (self.related_state_ref, self.related_hypothesis_ref):
            if ref is not None:
                _require_refs(ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'annotation_id', 'annotation_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'LiveEventAnnotation':
        return _seal(
            cls, payload, 'annotation_id', 'annotation_sha256', 'lea')


class LiveSpectrumObservation(BaseModel):
    """One single-channel live observable (#1-#16, lso- prefix).

    Every observation pins its session, its mode and the mode's exact
    parameters — spectrum modes carry the #749 estimator binding, RTA
    carries the IEC 61260 eligibility binding, SPL/Leq modes carry the
    quantity binding, spectrograph carries the time-frequency binding.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    session_ref: AuthorityRef
    mode: LiveMeasurementMode
    capture_state: CaptureState = 'live_view_only'
    spectrum: SpectrumEstimatorBinding | None = None
    banding: FractionalOctaveBinding | None = None
    spl: SplQuantityBinding | None = None
    spectrograph: SpectrographBinding | None = None
    quantity_value_db: float | None = None
    payload_ref: AuthorityRef | None = None
    payload_kind: Literal[
        'canonical_series', 'raw_stream', 'raster_image',
        'external_reference', 'unknown',
    ] = 'unknown'
    derived_from_refs: tuple[AuthorityRef, ...] = ()
    annotation_refs: tuple[AuthorityRef, ...] = ()
    state_change_ref: AuthorityRef | None = None
    hypothesis_refs: tuple[AuthorityRef, ...] = ()
    multi_input_combination: Literal[
        'none', 'display_only', 'spatial_average',
    ] = 'none'
    combination_semantics_ref: AuthorityRef | None = None
    alert_profile_refs: tuple[AuthorityRef, ...] = ()
    safety_critical_alert: bool = False
    overload_detected: bool = False
    duration_s: float | None = None
    observed_at_utc: str | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'LiveSpectrumObservation':
        _require_refs(self.session_ref)
        if self.mode in (
                'realtime_spectrum', 'fractional_octave_rta') \
                and self.spectrum is None:
            raise ValueError(
                f'{self.mode} requires the #749 spectrum estimator '
                'binding — a screenshot is not the canonical spectrum')
        if self.mode == 'fractional_octave_rta' and self.banding is None:
            raise ValueError(
                'fractional_octave_rta requires the IEC 61260 banding '
                'eligibility binding')
        if self.mode == 'spectrograph' and self.spectrograph is None:
            raise ValueError(
                'spectrograph requires its time-frequency binding')
        if self.mode in (
                'spl_instantaneous', 'spl_time_weighted',
                'leq_time_averaged', 'peak_max_min_hold') \
                and self.spl is None:
            raise ValueError(
                f'{self.mode} requires the SPL/Leq quantity binding — '
                'a bare dB carries no semantics')
        if self.mode == 'leq_time_averaged' and self.spl is not None \
                and self.spl.leq_interval_s is None:
            raise ValueError(
                'leq_time_averaged requires leq_interval_s')
        if self.capture_state != 'live_view_only':
            if self.payload_ref is None:
                raise ValueError(
                    'a captured live observation must pin its canonical '
                    'payload — promotion without content is not a capture')
            if self.payload_kind == 'raster_image':
                raise ValueError(
                    'a raster image can never be the canonical capture — '
                    'it is a derived export at most')
        if self.capture_state == 'derived_summary' \
                and not self.derived_from_refs:
            raise ValueError(
                'a derived summary must name the observations it '
                'summarizes')
        if self.quantity_value_db is not None:
            if self.spl is None:
                raise ValueError(
                    'a level value requires the SPL/Leq quantity binding')
            if self.spl.frequency_weighting == 'unknown':
                raise ValueError(
                    'a level value requires a declared frequency '
                    'weighting — 92 dB alone is not a quantity')
        for ref in (
                self.payload_ref, self.state_change_ref,
                self.combination_semantics_ref):
            if ref is not None:
                _require_refs(ref)
        for ref in (
                self.derived_from_refs, self.annotation_refs,
                self.hypothesis_refs, self.alert_profile_refs):
            _require_refs(*ref)
        if self.multi_input_combination == 'spatial_average' \
                and self.combination_semantics_ref is None:
            raise ValueError(
                'a spatial average requires the #575/#581 combination '
                'semantics reference — a multi-input display is never '
                'an implicit average')
        if self.safety_critical_alert and not self.alert_profile_refs:
            raise ValueError(
                'a safety-critical alert requires an explicit threshold '
                'profile reference')
        if self.duration_s is not None and self.duration_s <= 0.0:
            raise ValueError('duration_s must be positive')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'LiveSpectrumObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'lso')


class SPLTimeHistory(BaseModel):
    """Canonical sampled/aggregated live history (#6, sth- prefix).

    Retains the time series with its exact interval and quantity
    semantics; gaps, session boundaries and calibration changes are
    explicit point kinds so a viewer can never draw a continuous line
    through them by accident.

    ``exposure_interpretation`` is pinned to #602 delegation — room SPL
    logging is not personal exposure (#13).
    """

    model_config = ConfigDict(frozen=True)

    history_id: str
    history_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    session_ref: AuthorityRef
    quantity: HistoryQuantity
    spl: SplQuantityBinding
    interval_s: float
    points: tuple[TimeHistoryPoint, ...] = ()
    timebase_ref: AuthorityRef | None = None
    exposure_interpretation: Literal[
        'delegated_to_issue_602', 'not_assessed',
    ] = 'not_assessed'
    started_at_utc: str | None = None
    ended_at_utc: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'SPLTimeHistory':
        _require_refs(self.session_ref)
        if self.timebase_ref is not None:
            _require_refs(self.timebase_ref)
        if self.interval_s <= 0.0:
            raise ValueError('interval_s must be positive')
        if self.quantity == 'leq' and self.spl.leq_interval_s is None:
            raise ValueError('a Leq history requires leq_interval_s')
        if self.quantity == 'spl_time_weighted' \
                and self.spl.time_weighting == 'unknown':
            raise ValueError(
                'a time-weighted SPL history requires the time '
                'weighting')
        previous = -1.0
        for point in self.points:
            if point.offset_s <= previous:
                raise ValueError(
                    'history points must be strictly increasing in '
                    'offset_s')
            previous = point.offset_s
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'history_id', 'history_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'SPLTimeHistory':
        return _seal(cls, payload, 'history_id', 'history_sha256', 'sth')


class CapturedLiveTrace(BaseModel):
    """Immutable capture promoted from live view (#8, #16, clt- prefix).

    A capture is only as strong as what it binds: the canonical content
    payload, the source observations, the session/instrument chain, the
    state snapshot, the annotations and the software identity. Report
    images and CSVs ride in ``export_refs`` as derived exports — never
    the canonical source of truth.
    """

    model_config = ConfigDict(frozen=True)

    trace_id: str
    trace_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    session_ref: AuthorityRef
    capture_kind: CaptureKind
    source_observation_refs: tuple[AuthorityRef, ...]
    canonical_payload_ref: AuthorityRef
    raw_stream_ref: AuthorityRef | None = None
    settings_snapshot: LiveCaptureSettings | None = None
    annotation_refs: tuple[AuthorityRef, ...] = ()
    state_snapshot_ref: AuthorityRef | None = None
    state_delta_ref: AuthorityRef | None = None
    capture_event: Literal[
        'manual', 'scheduled', 'alarm_triggered', 'other',
    ] = 'other'
    operator: str | None = None
    software_version: str | None = None
    captured_at_utc: str | None = None
    export_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'CapturedLiveTrace':
        _require_refs(self.session_ref, self.canonical_payload_ref)
        if not self.source_observation_refs:
            raise ValueError(
                'a captured trace must name the live observations it '
                'captures — an unattributed capture is not evidence')
        for ref in (
                self.source_observation_refs, self.annotation_refs,
                self.export_refs):
            _require_refs(*ref)
        for ref in (
                self.raw_stream_ref, self.state_snapshot_ref,
                self.state_delta_ref):
            if ref is not None:
                _require_refs(ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'trace_id', 'trace_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CapturedLiveTrace':
        return _seal(cls, payload, 'trace_id', 'trace_sha256', 'clt')

    def comparison_signature(self) -> str | None:
        """Digest of the ref-free settings snapshot — equal signatures
        mean two captures ran under identical measurement settings and
        can be compared reproducibly across sessions."""
        if self.settings_snapshot is None:
            return None
        return _hash(self.settings_snapshot.comparison_payload())


# ---------------------------------------------------------------------------
# Evaluators — every path fails closed: absent or unverifiable evidence
# yields a degraded verdict, never inferred truth.
# ---------------------------------------------------------------------------


def evaluate_instrument_binding(
    session: RealtimeMeasurementSession | None,
) -> tuple[InstrumentBinding, str]:
    """Judge how far a session's identity supports quantitative claims."""
    if session is None:
        return ('insufficient_evidence', 'no_realtime_session')
    if not session.has_instrument_identity():
        return (
            'diagnostic_only',
            'instrument identity unbound — a live trace without its '
            'microphone/input identity is diagnostic-only',
        )
    if session.calibration_ref is None:
        return (
            'uncalibrated',
            'no #611 calibration binding — absolute level claims are '
            'denied',
        )
    if session.timebase_ref is None and session.sample_rate_hz is None:
        return (
            'timebase_unbound',
            'neither #609 timebase nor sample rate pinned — spectral and '
            'history claims are unpinned',
        )
    return (
        'instrument_bound',
        'instrument, calibration and timebase pinned',
    )


def banding_eligibility(
    banding: FractionalOctaveBinding | None,
) -> tuple[BandingEligibility, str]:
    """Highest banding claim the bound evidence supports (#4).

    The evaluator itself downgrades: even a forged
    ``iec_61260_class_*_eligible`` without pinned standard revision and
    class evidence degrades to ``project_defined_banding``.
    """
    if banding is None:
        return ('unknown', 'no_banding_binding')
    if banding.banding_eligibility in (
            'iec_61260_class_1_eligible', 'iec_61260_class_2_eligible'):
        if banding.standard_reference is None or not (
                banding.standard_reference.startswith('IEC 61260-1@')):
            return (
                'project_defined_banding',
                'IEC standard revision not pinned — downgraded',
            )
        if not banding.class_evidence_refs:
            return (
                'project_defined_banding',
                'no bound class evidence — an FFT binned into bands is '
                'not an IEC 61260 analyzer',
            )
        return (
            banding.banding_eligibility,
            'class eligibility evidenced',
        )
    if banding.banding_eligibility == 'ppo_display_only':
        return ('ppo_display_only', 'display approximation only')
    if banding.banding_eligibility == 'project_defined_banding':
        return ('project_defined_banding', 'project-defined banding')
    return ('unknown', 'banding eligibility unknown')


def spl_quantity_state(
    spl: SplQuantityBinding | None,
    quantity: str | None = None,
) -> tuple[str, str]:
    """Whether an SPL/Leq quantity carries its semantics (#5)."""
    if spl is None:
        return ('no_quantity_semantics', 'no SPL/Leq binding')
    if spl.frequency_weighting == 'unknown':
        return (
            'unweighted_quantity',
            'frequency weighting undeclared — a level without its '
            'weighting is not a quantity',
        )
    if quantity == 'leq' and spl.leq_interval_s is None:
        return ('unweighted_quantity', 'leq interval undeclared')
    if quantity in ('spl_time_weighted', 'max_hold', 'min_hold') \
            and spl.time_weighting == 'unknown':
        return ('unweighted_quantity', 'time weighting undeclared')
    return ('quantity_semantics_bound', 'quantity semantics declared')


def evaluate_live_observation(
    session: RealtimeMeasurementSession | None,
    observation: LiveSpectrumObservation | None,
) -> tuple[LiveVerdict, str]:
    """Judge how far one live observation supports its claims.

    The verdict never promotes beyond the bound evidence: no session or
    no instrument identity degrades to ``diagnostic_only``; overload
    (#695) limits every quantitative claim; uncalibrated SPL denies
    absolute levels; an undeclared estimator denies spectral claims; a
    display-only banding denies IEC class claims.
    """
    if observation is None:
        return ('insufficient_evidence', 'no_observation_record')
    if session is None:
        return (
            'diagnostic_only',
            'no session identity — input/instrument/calibration unpinned',
        )
    if not session.has_instrument_identity():
        return (
            'diagnostic_only',
            'instrument identity unbound — observation is '
            'diagnostic-only',
        )
    if observation.overload_detected:
        return (
            'overload_limited',
            '#695 overload flag set — quantitative claims limited or '
            'invalidated',
        )
    if observation.safety_critical_alert \
            and session.calibration_ref is None:
        return (
            'alert_denied_uncalibrated',
            'a safety-critical alert cannot run on an uncalibrated or '
            'ineligible input',
        )
    if observation.mode in (
            'realtime_spectrum', 'fractional_octave_rta'):
        if observation.spectrum is None or not (
                observation.spectrum.has_estimator_identity()):
            return (
                'estimator_unqualified',
                'no declared #749 estimator identity — the spectrum is '
                'an unattributed display',
            )
        if observation.spectrum.window_kind == 'unknown':
            return (
                'estimator_unqualified',
                'window undeclared — spectral claims stay unqualified',
            )
    if observation.mode == 'spectrograph':
        if observation.spectrograph is None \
                or observation.spectrograph.method == 'unknown':
            return (
                'estimator_unqualified',
                'spectrograph method undeclared',
            )
    if observation.mode == 'fractional_octave_rta':
        eligibility, _reason = banding_eligibility(observation.banding)
        if eligibility in (
                'ppo_display_only', 'project_defined_banding', 'unknown'):
            return (
                'banding_display_only',
                'banding is display/project-defined — not an IEC 61260 '
                'class result',
            )
    if observation.mode in (
            'spl_instantaneous', 'spl_time_weighted',
            'leq_time_averaged', 'peak_max_min_hold'):
        if session.calibration_ref is None:
            return (
                'uncalibrated',
                'SPL/Leq/peak claims require the #611 calibration '
                'binding',
            )
        state, reason = spl_quantity_state(
            observation.spl, observation.mode)
        if state != 'quantity_semantics_bound':
            return ('unweighted_quantity', reason)
    if observation.multi_input_combination == 'spatial_average' \
            and observation.combination_semantics_ref is None:
        return (
            'diagnostic_only',
            'spatial average without #575/#581 semantics — multi-input '
            'display is not an implicit average',
        )
    return (
        'live_evidence_bound',
        'observation bound to session, estimator and quantity '
        'semantics',
    )


def evaluate_time_history_integrity(
    history: SPLTimeHistory | None,
) -> tuple[HistoryVerdict, str]:
    """Whether a history honestly marks its gaps (#6).

    A jump between consecutive carried values larger than ~1.5 sample
    intervals with no ``missing``/``session_boundary`` marker is an
    unmarked gap — the history fails closed.
    """
    if history is None:
        return ('insufficient_evidence', 'no_history_record')
    if not history.points:
        return ('insufficient_evidence', 'history carries no points')
    gap_markers = 0
    previous: TimeHistoryPoint | None = None
    for point in history.points:
        if point.kind in ('missing', 'session_boundary'):
            gap_markers += 1
            previous = None
            continue
        if previous is not None:
            delta = point.offset_s - previous.offset_s
            if delta > history.interval_s * 1.5:
                return (
                    'gap_unmarked',
                    f'{delta:.3f}s of unmarked capture gap at offset '
                    f'{point.offset_s:.3f}s — the line must not be '
                    'drawn through it',
                )
        previous = point
    if gap_markers:
        return (
            'history_with_declared_gaps',
            f'{gap_markers} declared gap/boundary markers',
        )
    return ('history_complete', 'no gaps detected')


def evaluate_capture_promotion(
    session: RealtimeMeasurementSession | None,
    observation: LiveSpectrumObservation | None,
    trace: CapturedLiveTrace | None = None,
) -> tuple[CaptureVerdict, str]:
    """Judge the live→capture promotion boundary (#8, #16).

    A live view is never evidence; an observation promoted to
    ``captured_*`` with its canonical payload is captured evidence; a
    ``CapturedLiveTrace`` that additionally pins the settings snapshot
    and the state snapshot is a qualified capture.
    """
    if trace is None:
        if observation is None:
            return (
                'live_view_only',
                'no observation record — a live view is not evidence',
            )
        if observation.capture_state == 'live_view_only':
            return (
                'live_view_only',
                'live-view-only state — promotion requires an explicit '
                'capture with all state pinned',
            )
        return (
            'captured_evidence',
            'observation captured with its canonical payload — '
            'evidence-bound without a session-level trace',
        )
    if session is None or not session.has_instrument_identity():
        return (
            'diagnostic_only',
            'capture without instrument identity is diagnostic-only',
        )
    if session.calibration_ref is None:
        return (
            'uncalibrated_claim',
            'capture on an uncalibrated input cannot support absolute '
            'level claims',
        )
    if trace.settings_snapshot is not None \
            and trace.state_snapshot_ref is not None:
        return (
            'qualified_capture',
            'captured with settings snapshot, instrument chain and '
            'state pinned — commissionable live evidence',
        )
    return (
        'captured_evidence',
        'captured trace — evidence-bound but not a fully qualified '
        'capture',
    )


def evaluate_state_correlation(
    state_change_ref: AuthorityRef | None,
    annotations: tuple[LiveEventAnnotation, ...],
    observation_refs: tuple[AuthorityRef, ...],
) -> tuple[CorrelationVerdict, str]:
    """Correlate a controlled state change with live observations (#9,
    #10). The verdict is always contextual — the authority never claims
    the state change caused the observation."""
    if state_change_ref is None and not annotations:
        return (
            'no_correlation',
            'no state-change record and no annotations to correlate',
        )
    if state_change_ref is not None and observation_refs:
        return (
            'contextual_correlation',
            'state change and covering observations bound — a recorded '
            'correlation, not causality',
        )
    return (
        'annotation_only',
        'annotations recorded as context — no controlled state change '
        'or covering observations bound',
    )


def evaluate_noise_evidence_eligibility(
    observation: LiveSpectrumObservation | None,
    trace: CapturedLiveTrace | None = None,
) -> tuple[NoiseEligibility, str]:
    """#580 boundary (#12): a live or momentary spectrum is a preview.

    Only a captured observation/trace is even a *candidate* for #580 —
    which alone judges the measurement state, profile and duration
    requirements. This authority never returns 'qualified noise'.
    """
    if observation is None and trace is None:
        return ('preview_only', 'nothing observed')
    if trace is not None:
        return (
            'captured_noise_candidate',
            'immutable captured trace — eligible candidate for #580 '
            'evaluation, which owns the profile/duration requirements',
        )
    assert observation is not None
    if observation.capture_state == 'live_view_only' \
            or observation.payload_ref is None:
        return (
            'preview_only',
            'a live RTA view is a preview — #580 requires captured '
            'state/profile/duration',
        )
    return (
        'captured_noise_candidate',
        'captured observation — candidate for #580 qualification only',
    )


def compare_captured_traces(
    first: CapturedLiveTrace | None,
    second: CapturedLiveTrace | None,
) -> tuple[ComparisonVerdict, str]:
    """Reproducible cross-session comparison (#16): only identical
    settings signatures make two captures comparable."""
    if first is None or second is None:
        return ('insufficient_evidence', 'missing capture to compare')
    first_sig = first.comparison_signature()
    second_sig = second.comparison_signature()
    if first_sig is None or second_sig is None:
        return (
            'insufficient_evidence',
            'a capture without a settings snapshot cannot be compared '
            'reproducibly',
        )
    if first_sig == second_sig:
        return (
            'comparable',
            'identical measurement settings — reproducible comparison',
        )
    return (
        'settings_differ',
        'estimator/quantity settings differ — comparison is '
        'descriptive only',
    )
