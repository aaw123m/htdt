"""Playback-electronics / electrical audio-path qualification authority
(#790).

A calibrated acoustic measurement, correct routing and a declared gain
structure do not prove that the AVR / processor / DSP / DAC / preamp /
power-amplifier path is electrically transparent or linear over the
level, frequency and channel conditions the theater actually uses. This
module owns the *electrical* side of that question: which exact
source->processor->DAC->preamp->amplifier->load path was tested, under
what device/firmware/sample-rate/volume/DSP/load/thermal state, and what
transfer, noise and nonlinear quantities were observed there.

Layers stay separate and fail closed:

- the path profile pins the exact stimulated/observed endpoints and the
  material operating state — changing any of it is a different
  applicability identity (the sealed digest already enforces that);
- transfer measurements bind magnitude/phase/delay/gain per channel and
  declare whether the analyzer/interface transfer (#699) was
  de-embedded — an interface+device combined curve is never published as
  device response unlabeled;
- linearity evidence keeps level sweeps and method-exact nonlinear
  quantities (THD / THD+N / IMD / difference-frequency / DIM-style) —
  unlike test methods are never normalized into one scalar;
- the qualification record carries the verdict, its valid frequency/
  level domain, channel matching (independent of #650 crosstalk), and
  the evidence-composition state a prediction may consume.

Predicted/vendor-declared evidence never substitutes for a measured
one, provider 'Pure/Direct/Bypass' labels never substitute for observed
transfer, and no universal audibility threshold or 'transparent' score
is invented. Domains this module explicitly does not own: electro-
acoustic system distortion (#192), measurement-chain self-distortion
(#695/#699 interface internals), crosstalk/separation (#650), system
gain/noise-floor semantics (#651), hum/ground-loop diagnosis (#606),
and the DRC/limiter policy itself (#649) — it only records the observed
state.

Basis: issue #790 scope; IEC 60268-3:2018 (analogue amplifiers and the
analogue portions of analogue/digital sound-system amplifiers);
AES17-2020 (measurement of digital audio equipment); ANSI/CTA-490-B
(test conditions for single/multichannel power amplifiers, preamps,
integrated amplifiers, receivers).
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


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


def _require_method_pinning(references: tuple[str, ...]) -> None:
    for pinned in references:
        if '@' not in pinned:
            raise ValueError(
                'method references must pin exact editions '
                '(standard_id@edition)')


ElectronicPathClass = Literal[
    'digital_in_digital_out',
    'digital_in_analog_line_out',
    'analog_in_analog_line_out',
    'analog_in_speaker_out',
    'digital_in_speaker_out',
    'processor_loop_insert',
    'device_internal_test',
    'other',
]

ProcessingStageState = Literal[
    'sample_rate_conversion',
    'bass_management',
    'eq_filter',
    'crossover',
    'loudness',
    'drc_limiter',
    'upmix_downmix',
    'level_dependent_eq',
    'protection',
    'none_detected',
    'unknown',
]

LoadKind = Literal[
    'resistive_dummy_load',
    'loudspeaker',
    'complex_load',
    'line_level_input',
    'open_circuit',
    'unknown',
]

ThermalState = Literal[
    'cold_start', 'warmed_up', 'sustained_stress', 'unknown',
]

ChannelScope = Literal[
    'single_channel', 'multichannel', 'all_channels', 'unknown',
]

DeembeddingState = Literal[
    'de_embedded',
    'negligible_with_evidence',
    'included_in_result',
    'unknown',
]

EvidenceOrigin = Literal[
    'measured', 'predicted', 'simulated', 'vendor_declared',
]

MeasurementClass = Literal[
    'linear_transfer',
    'absolute_gain',
    'delay',
    'noise',
    'nonlinear_quantity',
    'level_sweep_point',
    'other',
]

DelayMethod = Literal[
    'group_delay', 'broadband_delay', 'pure_delay', 'unknown',
]

StimulusKind = Literal[
    'sweep', 'stepped_sine', 'mls', 'noise', 'impulse', 'tone',
    'other', 'unknown',
]

ReferenceConvention = Literal[
    'dbfs', 'volts_rms', 'watts_into_load', 'dbu', 'dbv',
    'other', 'unknown',
]

SrcState = Literal['bypassed', 'active', 'unknown']

DitherState = Literal['off', 'triangular', 'noise_shaped', 'unknown']

ClockState = Literal[
    'common_clock', 'digitally_locked', 'asynchronous', 'unknown',
]

PointState = Literal[
    'linear', 'compressed', 'limited', 'clipped', 'protected',
    'indeterminate', 'unknown',
]

LevelRegime = Literal[
    'linear_range',
    'gain_compression',
    'soft_limiting',
    'hard_clipping',
    'protection_engagement',
    'thermal_change',
    'indeterminate',
    'unknown',
]

NonlinearQuantity = Literal[
    'thd',
    'thd_plus_n',
    'harmonic_h2',
    'harmonic_h3',
    'harmonic_other',
    'imd',
    'difference_frequency',
    'dynamic_intermodulation',
    'level_dependent_gain',
    'other',
]

EvidenceDomain = Literal[
    'electronic_path',
    'electroacoustic_system',
    'measurement_chain',
]

QualificationState = Literal[
    'qualified_linear_transfer',
    'qualified_with_limitations',
    'measured_processing_effects',
    'path_unverified',
    'unknown',
]

EvidenceComposition = Literal[
    'ideal_electronics_assumed',
    'measured_linear_transfer_applied',
    'measured_level_dependent_transfer_applied',
    'device_transfer_unknown',
]

BypassVerdict = Literal[
    'provider_label_only',
    'observed_transfer_confirmed',
    'processing_detected',
    'not_applicable',
]

_MEASURED_COMPOSITIONS = (
    'measured_linear_transfer_applied',
    'measured_level_dependent_transfer_applied',
)

_NONLINEAR_POINT_STATES = ('compressed', 'limited', 'clipped', 'protected')

_HARD_LIMIT_STATES = ('clipped', 'protected')

_SOFT_LIMIT_STATES = ('compressed', 'limited')

ELECTRONICS_LABELS: dict[str, str] = {
    'electronics_qualified': '電子経路適格',
    'electronics_qualified_with_limitations': '電子経路適格（限定条件付き）',
    'measured_processing_effects': '処理影響を測定済み',
    'nonlinear_limited': '非線形制限域あり',
    'channel_mismatch': 'チャンネル間不一致',
    'processing_detected': '処理検出（バイパス表示と不一致）',
    'interface_confounded': '測定インターフェース混在',
    'state_dependent': '状態依存の証拠',
    'stale_after_device_change': '機器状態変更により陳腐化',
    'path_unverified': '経路未検証',
    'insufficient_evidence': '証拠不足',
    'unknown': '不明',
}


class ElectronicAudioPathProfile(BaseModel):
    """One exact electronic signal path under a pinned operating state
    (eapp- prefix).

    Binds the stimulated and observed endpoints, the path class, and
    every material device/DSP/load/thermal state — the sealed digest
    makes any material change a different applicability identity, so a
    result never silently carries over to a different firmware, preset,
    sample-rate or load condition. ``device_state_ref`` optionally pins
    the device/firmware configuration epoch (#592/#595) so a later change
    can stale every qualification bound to it without touching unrelated
    room/material evidence.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    path_class: ElectronicPathClass
    input_endpoint: str
    output_endpoint: str
    path_stages: tuple[str, ...]
    device_model: str
    hardware_revision: str | None = None
    firmware_version: str
    channel_scope: ChannelScope = 'unknown'
    sample_rate_hz: float | None = None
    sample_format: str | None = None
    volume_position: str | None = None
    input_trim_db: float | None = None
    output_trim_db: float | None = None
    processing_preset: str | None = None
    provider_bypass_label: str | None = None
    observed_processing_states: tuple[ProcessingStageState, ...] = (
        'unknown',)
    channels_driven: int | None = None
    load_kind: LoadKind = 'unknown'
    load_impedance_ohm: float | None = None
    supply_state: str | None = None
    thermal_state: ThermalState = 'unknown'
    channel_matching_tolerance_db: float | None = None
    phase_matching_tolerance_deg: float | None = None
    measurement_interface_ref: AuthorityRef | None = None
    device_state_ref: AuthorityRef | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ElectronicAudioPathProfile':
        if not self.input_endpoint:
            raise ValueError('the stimulated input endpoint is required')
        if not self.output_endpoint:
            raise ValueError('the observed output endpoint is required')
        if not self.path_stages:
            raise ValueError(
                'the actually stimulated/observed path stages are '
                'required')
        if not self.device_model:
            raise ValueError('device_model is required')
        if not self.firmware_version:
            raise ValueError('firmware_version is required')
        if self.sample_rate_hz is not None \
                and self.sample_rate_hz <= 0.0:
            raise ValueError('sample_rate_hz must be > 0')
        if self.channels_driven is not None \
                and self.channels_driven < 1:
            raise ValueError('channels_driven must be >= 1')
        if self.load_impedance_ohm is not None \
                and self.load_impedance_ohm <= 0.0:
            raise ValueError('load_impedance_ohm must be > 0')
        if self.channel_matching_tolerance_db is not None \
                and self.channel_matching_tolerance_db < 0.0:
            raise ValueError('channel_matching_tolerance_db must be >= 0')
        if self.phase_matching_tolerance_deg is not None \
                and self.phase_matching_tolerance_deg < 0.0:
            raise ValueError(
                'phase_matching_tolerance_deg must be >= 0')
        if 'none_detected' in self.observed_processing_states \
                and len(self.observed_processing_states) > 1:
            raise ValueError(
                "'none_detected' cannot coexist with observed "
                'processing states')
        for ref in (self.measurement_interface_ref,
                    self.device_state_ref):
            if ref is not None:
                _require_refs(ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'ElectronicAudioPathProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'eapp')


class ElectricalTransferMeasurement(BaseModel):
    """One measured electrical transfer/noise/nonlinear quantity bound
    to a path profile (etm- prefix).

    Magnitude, phase, delay and absolute gain stay independent fields —
    a flat magnitude response never implies phase or delay equivalence,
    and none of them is absorbed into a microphone calibration or a
    room-correction target. ``deembedding_state`` declares how the #699
    analyzer/interface transfer was handled; ``de_embedded`` requires
    the correction record that was applied. ``path_class_observed`` and
    ``sample_rate_hz`` repeat the state actually in force so a result
    can never be reused across materially different paths or states.
    """

    model_config = ConfigDict(frozen=True)

    measurement_id: str
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    channel_label: str
    measurement_class: MeasurementClass
    evidence_origin: EvidenceOrigin
    path_class_observed: ElectronicPathClass
    frequency_low_hz: float
    frequency_high_hz: float
    gain_db: float | None = None
    magnitude_response_ref: AuthorityRef | None = None
    phase_response_ref: AuthorityRef | None = None
    complex_response_ref: AuthorityRef | None = None
    delay_ms: float | None = None
    delay_method: DelayMethod | None = None
    stimulus_kind: StimulusKind = 'unknown'
    stimulus_level: float | None = None
    stimulus_level_unit: ReferenceConvention | None = None
    noise_level_db: float | None = None
    noise_weighting: str | None = None
    deembedding_state: DeembeddingState
    interface_correction_ref: AuthorityRef | None = None
    method_references: tuple[str, ...] = ()
    sample_rate_hz: float | None = None
    input_format: str | None = None
    output_format: str | None = None
    reference_convention: ReferenceConvention = 'unknown'
    word_length_bits: int | None = None
    src_state: SrcState = 'unknown'
    dither_state: DitherState = 'unknown'
    clock_state: ClockState = 'unknown'
    bit_perfect_verified: bool | None = None
    channels_driven: int | None = None
    load_impedance_ohm: float | None = None
    output_power_w: float | None = None
    output_voltage_v: float | None = None
    thermal_state: ThermalState = 'unknown'
    supply_state: str | None = None
    uncertainty_db: float | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ElectricalTransferMeasurement':
        _require_refs(self.profile_ref)
        for ref in (self.magnitude_response_ref, self.phase_response_ref,
                    self.complex_response_ref,
                    self.interface_correction_ref):
            if ref is not None:
                _require_refs(ref)
        if not self.channel_label:
            raise ValueError('channel_label is required')
        if not (0.0 < self.frequency_low_hz < self.frequency_high_hz):
            raise ValueError(
                'frequency bounds must satisfy 0 < low < high')
        if not any((
                self.gain_db is not None,
                self.magnitude_response_ref is not None,
                self.phase_response_ref is not None,
                self.complex_response_ref is not None,
                self.delay_ms is not None,
                self.noise_level_db is not None,
        )):
            raise ValueError(
                'a transfer measurement must carry at least one bound '
                'quantity — gain, a response payload, delay or noise')
        if self.delay_ms is not None \
                and self.delay_method in (None, 'unknown'):
            raise ValueError(
                'a reported delay must declare its method — group, '
                'broadband or pure delay are not interchangeable')
        if self.deembedding_state == 'de_embedded' \
                and self.interface_correction_ref is None:
            raise ValueError(
                'de_embedded requires the pinned interface-correction '
                'record that was applied (#699)')
        if self.deembedding_state == 'negligible_with_evidence' \
                and self.interface_correction_ref is None \
                and not self.notes:
            raise ValueError(
                'negligible_with_evidence requires a correction ref or '
                'recorded basis — never an unlabeled assumption')
        _require_method_pinning(self.method_references)
        if self.sample_rate_hz is not None \
                and self.sample_rate_hz <= 0.0:
            raise ValueError('sample_rate_hz must be > 0')
        if self.word_length_bits is not None \
                and self.word_length_bits < 8:
            raise ValueError('word_length_bits must be >= 8')
        if self.channels_driven is not None \
                and self.channels_driven < 1:
            raise ValueError('channels_driven must be >= 1')
        if self.load_impedance_ohm is not None \
                and self.load_impedance_ohm <= 0.0:
            raise ValueError('load_impedance_ohm must be > 0')
        if self.output_power_w is not None and self.output_power_w < 0.0:
            raise ValueError('output_power_w must be >= 0')
        if self.uncertainty_db is not None \
                and self.uncertainty_db < 0.0:
            raise ValueError('uncertainty_db must be >= 0')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'measurement_id', 'measurement_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'ElectricalTransferMeasurement':
        return _seal(
            cls, payload, 'measurement_id', 'measurement_sha256', 'etm')


class ElectronicsLevelPoint(BaseModel):
    """One point of a bounded level sweep (#790 section 13).

    ``point_state`` records what was actually observed at that level;
    exact stimulus and safe load limits remain prerequisites and are
    pinned on the owning evidence record, not invented here.
    """

    model_config = ConfigDict(frozen=True)

    input_level_db: float
    output_level_db: float | None = None
    observed_gain_db: float | None = None
    point_state: PointState = 'unknown'
    notes: str | None = None


class NonlinearQuantityEvidence(BaseModel):
    """One method-exact nonlinear quantity (#790 section 5).

    Method, unit, stimulus, bandwidth, level and load are retained per
    quantity — THD, THD+N, single harmonics, IMD, difference-frequency
    and DIM-style results are never normalized into one generic
    ``distortion_percent``.
    """

    model_config = ConfigDict(frozen=True)

    quantity: NonlinearQuantity
    value: float
    unit: Literal['percent', 'db', 'dbc', 'dbfs', 'vrms', 'watts']
    method_reference: str
    stimulus: str
    bandwidth_hz: float | None = None
    signal_level: str | None = None
    load: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'NonlinearQuantityEvidence':
        if '@' not in self.method_reference:
            raise ValueError(
                'method_reference must pin an exact edition '
                '(standard_id@edition)')
        if not self.stimulus:
            raise ValueError('the stimulus description is required')
        if self.bandwidth_hz is not None and self.bandwidth_hz <= 0.0:
            raise ValueError('bandwidth_hz must be > 0')
        return self


class ElectronicLinearityEvidence(BaseModel):
    """Level-sweep and method-exact nonlinear evidence for one path
    (ele- prefix).

    ``domain`` keeps electronic-path distortion separate from #192
    electroacoustic system distortion and #695/#699 measurement-chain
    distortion — an acoustic harmonic can never be assigned to the
    loudspeaker while upstream electronics remain unbounded, and a
    cross-domain record never counts as electrical-path evidence.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    domain: EvidenceDomain = 'electronic_path'
    evidence_origin: EvidenceOrigin = 'measured'
    level_points: tuple[ElectronicsLevelPoint, ...] = ()
    nonlinear_quantities: tuple[NonlinearQuantityEvidence, ...] = ()
    observed_regime: LevelRegime = 'unknown'
    method_references: tuple[str, ...] = ()
    safe_level_bound: str | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ElectronicLinearityEvidence':
        _require_refs(self.profile_ref)
        if not self.level_points and not self.nonlinear_quantities:
            raise ValueError(
                'linearity evidence requires at least one level point '
                'or nonlinear quantity')
        if self.observed_regime == 'linear_range' and any(
                p.point_state in _NONLINEAR_POINT_STATES
                for p in self.level_points):
            raise ValueError(
                "observed_regime 'linear_range' contradicts nonlinear "
                'level points')
        _require_method_pinning(self.method_references)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'ElectronicLinearityEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'ele')


class PlaybackElectronicsQualification(BaseModel):
    """The sealed verdict for one electronic audio path (peq- prefix).

    A qualified state requires bound measured transfer evidence, an
    explicit valid frequency/level domain, and a measured
    evidence-composition state a prediction may consume (#790 sections
    14/18). Channel matching stays a transfer-matching statement,
    independent of #650 crosstalk. ``staled_by_ref`` records the
    device/firmware/hardware change that superseded this qualification.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    measurement_refs: tuple[AuthorityRef, ...] = ()
    linearity_refs: tuple[AuthorityRef, ...] = ()
    qualification_state: QualificationState
    evidence_composition: EvidenceComposition
    valid_low_hz: float | None = None
    valid_high_hz: float | None = None
    level_domain: str | None = None
    limitation_reasons: tuple[str, ...] = ()
    max_gain_difference_db: float | None = None
    max_phase_difference_deg: float | None = None
    bypass_verdict: BypassVerdict = 'not_applicable'
    staled_by_ref: AuthorityRef | None = None
    uncertainty_notes: str | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'PlaybackElectronicsQualification':
        _require_refs(self.profile_ref)
        for ref in self.measurement_refs:
            _require_refs(ref)
        for ref in self.linearity_refs:
            _require_refs(ref)
        if self.staled_by_ref is not None:
            _require_refs(self.staled_by_ref)
        qualified = self.qualification_state in (
            'qualified_linear_transfer',
            'qualified_with_limitations',
            'measured_processing_effects',
        )
        if qualified:
            if not self.measurement_refs:
                raise ValueError(
                    'a measured qualification requires bound '
                    'measurement_refs')
            if self.valid_low_hz is None or self.valid_high_hz is None:
                raise ValueError(
                    'a measured qualification requires its valid '
                    'frequency domain')
            if not (0.0 < self.valid_low_hz < self.valid_high_hz):
                raise ValueError(
                    'valid domain must satisfy 0 < low < high')
            if self.evidence_composition not in _MEASURED_COMPOSITIONS:
                raise ValueError(
                    'a measured qualification requires a measured '
                    'evidence-composition state')
        if self.qualification_state == 'qualified_linear_transfer' \
                and self.evidence_composition \
                != 'measured_linear_transfer_applied':
            raise ValueError(
                'qualified_linear_transfer requires '
                'measured_linear_transfer_applied composition')
        if self.qualification_state == 'qualified_with_limitations' \
                and not self.limitation_reasons:
            raise ValueError(
                'qualified_with_limitations requires limitation_reasons')
        if self.max_gain_difference_db is not None \
                and self.max_gain_difference_db < 0.0:
            raise ValueError('max_gain_difference_db must be >= 0')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'PlaybackElectronicsQualification':
        return _seal(
            cls, payload, 'qualification_id', 'qualification_sha256',
            'peq')


def evaluate_electronics_claim(
    profile: ElectronicAudioPathProfile | None,
    measurements: tuple[ElectricalTransferMeasurement, ...] = (),
    linearity: tuple[ElectronicLinearityEvidence, ...] = (),
    qualification: PlaybackElectronicsQualification | None = None,
    *,
    current_device_state_ref: AuthorityRef | None = None,
) -> tuple[str, str]:
    """Fail-closed playback-electronics qualification gate (#790).

    A qualified verdict requires: a declared path profile, measured
    transfer evidence bound to that exact profile/path-class/state, a
    non-confounded interface-correction state, no contradicting
    processing or channel-matching evidence, and a sealed qualification
    record whose claim never exceeds what the evidence supports. A
    pinned device-state epoch that has moved (#592/#595) stales the
    path's electrical evidence only — room/material claims are
    unaffected.
    """
    if profile is None:
        return ('unknown', 'no_path_profile')
    if profile.device_state_ref is not None \
            and current_device_state_ref is not None \
            and current_device_state_ref != profile.device_state_ref:
        return (
            'stale_after_device_change',
            'the pinned device/firmware epoch no longer matches the '
            'current device state',
        )
    if qualification is not None \
            and qualification.staled_by_ref is not None:
        return (
            'stale_after_device_change',
            'a staling record supersedes this qualification',
        )
    bound = tuple(
        m for m in measurements
        if m.profile_ref.ref_id == profile.profile_id
        and m.profile_ref.ref_sha256 == profile.profile_sha256
        and m.evidence_origin == 'measured'
        and m.path_class_observed == profile.path_class
    )
    if not bound:
        return (
            'insufficient_evidence',
            'no measured transfer evidence bound to this exact path '
            'class and profile',
        )
    state_bound = tuple(
        m for m in bound
        if m.sample_rate_hz is None
        or profile.sample_rate_hz is None
        or m.sample_rate_hz == profile.sample_rate_hz
    )
    if not state_bound:
        return (
            'state_dependent',
            'transfer evidence exists only under a different '
            'sample-rate/format state than the declared path',
        )
    if all(m.deembedding_state in ('included_in_result', 'unknown')
            for m in state_bound):
        return (
            'interface_confounded',
            'every result includes the analyzer/interface transfer or '
            'leaves its state unknown — not publishable as device '
            'response',
        )
    observed_processing = tuple(
        s for s in profile.observed_processing_states
        if s not in ('none_detected', 'unknown')
    )
    if profile.provider_bypass_label and observed_processing:
        return (
            'processing_detected',
            'provider bypass label ' + repr(profile.provider_bypass_label)
            + ' contradicted by observed processing: '
            + ','.join(observed_processing),
        )
    domain_linearity = tuple(
        e for e in linearity
        if e.domain == 'electronic_path'
        and e.profile_ref.ref_id == profile.profile_id
        and e.profile_ref.ref_sha256 == profile.profile_sha256
    )
    point_states = {
        p.point_state
        for e in domain_linearity
        for p in e.level_points
    }
    if point_states & set(_HARD_LIMIT_STATES):
        return (
            'nonlinear_limited',
            'clipping/protection observed within the swept level '
            'domain — the path is not linear over the tested range',
        )
    if qualification is not None:
        if profile.channel_matching_tolerance_db is not None \
                and qualification.max_gain_difference_db is not None \
                and qualification.max_gain_difference_db \
                > profile.channel_matching_tolerance_db:
            return (
                'channel_mismatch',
                f'gain difference '
                f'{qualification.max_gain_difference_db} dB exceeds '
                f'tolerance {profile.channel_matching_tolerance_db} dB',
            )
        if profile.phase_matching_tolerance_deg is not None \
                and qualification.max_phase_difference_deg is not None \
                and qualification.max_phase_difference_deg \
                > profile.phase_matching_tolerance_deg:
            return (
                'channel_mismatch',
                f'phase difference '
                f'{qualification.max_phase_difference_deg} deg exceeds '
                f'tolerance {profile.phase_matching_tolerance_deg} deg',
            )
    if qualification is None:
        return ('insufficient_evidence', 'no_qualification_record')
    if qualification.qualification_state == 'unknown':
        return ('unknown', 'qualification_state:unknown')
    if qualification.qualification_state == 'path_unverified':
        return ('path_unverified', 'qualification_state:path_unverified')
    if qualification.bypass_verdict == 'processing_detected' \
            or observed_processing:
        return (
            'measured_processing_effects',
            'processing in the path was measured — the qualification '
            'stands only for the observed transfer, not for bypass',
        )
    if qualification.qualification_state \
            == 'measured_processing_effects':
        return (
            'measured_processing_effects',
            'qualification_state:measured_processing_effects',
        )
    if point_states & set(_SOFT_LIMIT_STATES):
        return (
            'electronics_qualified_with_limitations',
            'level-dependent behavior observed: '
            + ','.join(sorted(point_states & set(_SOFT_LIMIT_STATES))),
        )
    if any(m.deembedding_state in ('included_in_result', 'unknown')
            for m in state_bound):
        return (
            'electronics_qualified_with_limitations',
            'part of the bound evidence still includes or leaves '
            'unknown the interface transfer state',
        )
    if qualification.qualification_state \
            == 'qualified_with_limitations':
        return (
            'electronics_qualified_with_limitations',
            'limitations:' + ','.join(qualification.limitation_reasons),
        )
    return (
        'electronics_qualified',
        'qualified_linear_transfer:'
        f'{qualification.valid_low_hz}-'
        f'{qualification.valid_high_hz} Hz',
    )
