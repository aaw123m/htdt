"""Sub-20 Hz / infrasonic acoustic authority (#779).

Extending the validated acoustic band below 20 Hz is not a matter of
running the same solver or microphone "a bit lower". Infrasound has its
own weighting (ISO 7196 G), its own measurement-chain pitfalls
(AC coupling, hidden high-pass filters, DC removal, wind/vent pressure
equalization, AGC, software preprocessing), its own room-coupling states
(doors, windows, HVAC, connected volumes), and solver validity floors.
This module keeps the layers separate: declared ULF scope → measurement
chain capability → per-band observations → system qualification.
Claims fail closed: nothing below the declared band is inferred, and
human exposure interpretation is delegated to #602 (never evaluated
here).

Basis: issue #779 scope; ISO 7196:1995 (G-weighting); IEC TR
61094-10:2022 (infrasound microphone calibration); ANSI/CTA-2010-C
(sub-woofer LF measurement practice); ISO 2896 / ISO 1996-2 annex
(infrasound in environmental noise context); infrasound perception
literature (Møller & Pedersen 2004; Kühler et al.). HTDT records
capability and evidence — it never asserts audibility, tactile, or
health effects from levels alone.
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


ULFQuantityKind = Literal[
    'complex_pressure', 'linear_spl_unweighted', 'g_weighted_level',
    'narrowband_spectrum', 'time_waveform_peak_pressure',
    'room_spatial_variation', 'source_output_compression',
    'other', 'unknown',
]

ULFCapabilityState = Literal[
    'ulf_calibrated', 'ulf_characterized_with_limitations',
    'audible_band_only', 'high_pass_limited', 'noise_floor_limited',
    'overload_limited', 'unknown',
]

InterfaceState = Literal[
    'ac_coupled', 'high_pass_filtered', 'dc_removal_suspected',
    'agc_detected', 'vent_pressure_equalized', 'input_protection_active',
    'software_preprocessing', 'none_detected', 'unknown',
]

OpeningState = Literal['open', 'partial', 'closed', 'unknown']

ULFQualificationState = Literal[
    'measured', 'measured_with_limitations', 'diagnostic_only',
    'below_measurement_capability', 'unknown',
]

PerceptualState = Literal[
    'below_selected_perceptual_reference',
    'audible_detected_in_controlled_test', 'tactile_detected',
    'no_difference_demonstrated', 'unknown',
]

ULF_LABELS: dict[str, str] = {
    'ulf_capability_verified': '超低域測定能力が確認済み',
    'ulf_capability_verified_limited': '超低域測定能力（限定条件付き）',
    'diagnostic_only': '診断表示のみ（能力未確認）',
    'below_measurement_capability': '測定チェーン能力未達',
    'measurement_chain_inadequate': '測定チェーン不適格',
    'below_validated_domain': 'ソルバー検証域を下回る要求',
    'insufficient_evidence': '証拠不足',
    'stale_after_room_change': '室状態変更により陳腐化',
    'unknown': '不明',
}


class UltraLowFrequencyAcousticProfile(BaseModel):
    """Declared sub-20 Hz scope for one document (ulfap- prefix).

    The profile pins the requested band, the room-boundary state under
    which ULF claims apply (openings, HVAC, connected volumes), the
    solver whose validity floor bounds the claim, and the source
    inventory. Nothing here asserts that the band was measured or that
    any listener could perceive it.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    request_low_hz: float
    request_high_hz: float
    frequency_resolution_hz: float
    banding: Literal[
        'fractional_octave', 'narrowband_fixed', 'time_waveform',
        'peak_pressure', 'other',
    ]
    door_state: OpeningState = 'unknown'
    window_state: OpeningState = 'unknown'
    hvac_state: Literal['off', 'steady_on', 'cycling', 'unknown'] \
        = 'unknown'
    leakage_state: Literal['sealed', 'vented', 'unknown'] = 'unknown'
    connected_volume_refs: tuple[AuthorityRef, ...] = ()
    construction_state: str | None = None
    air_temperature_c: float | None = None
    solver_ref: AuthorityRef | None = None
    solver_validated_low_hz: float | None = None
    source_refs: tuple[AuthorityRef, ...] = ()
    bass_management_semantics: Literal[
        'declared', 'not_declared', 'unknown',
    ] = 'unknown'
    bass_management_notes: str | None = None
    profile_ref: AuthorityRef | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'UltraLowFrequencyAcousticProfile':
        if not (0.0 < self.request_low_hz < 20.0):
            raise ValueError('request_low_hz must be in (0, 20) Hz')
        if not (self.request_low_hz < self.request_high_hz <= 20.0):
            raise ValueError(
                'request_high_hz must satisfy low < high <= 20 Hz')
        if self.frequency_resolution_hz <= 0.0:
            raise ValueError('frequency_resolution_hz must be > 0')
        if self.solver_ref is not None:
            _require_refs(self.solver_ref)
            if self.solver_validated_low_hz is None:
                raise ValueError(
                    'a pinned solver requires solver_validated_low_hz')
        if self.solver_validated_low_hz is not None \
                and self.solver_validated_low_hz < 0.0:
            raise ValueError('solver_validated_low_hz must be >= 0')
        for ref in self.connected_volume_refs:
            _require_refs(ref)
        for ref in self.source_refs:
            _require_refs(ref)
        if self.profile_ref is not None:
            _require_refs(self.profile_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'UltraLowFrequencyAcousticProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'ulfap')


class InfrasonicMeasurementCapability(BaseModel):
    """One measurement chain's eligibility for sub-20 Hz work
    (imc- prefix). The chain's own calibrated floor, interface states
    (hidden HPF / AC coupling / AGC / DC removal / preprocessing),
    noise floor and overload limit are recorded so that a consumer-grade
    chain can never silently claim infrasonic validity.
    """

    model_config = ConfigDict(frozen=True)

    capability_id: str
    capability_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    chain_label: str
    microphone_ref: AuthorityRef | None = None
    preamp_ref: AuthorityRef | None = None
    calibration_file_ref: AuthorityRef | None = None
    calibration_standard: str | None = None
    valid_low_hz: float | None = None
    capability_state: ULFCapabilityState = 'unknown'
    interface_states: tuple[InterfaceState, ...] = ('unknown',)
    adc_sample_rate_hz: float | None = None
    overload_limit_db: float | None = None
    noise_floor_db: float | None = None
    limitation_notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'InfrasonicMeasurementCapability':
        for ref in (self.microphone_ref, self.preamp_ref,
                    self.calibration_file_ref):
            if ref is not None:
                _require_refs(ref)
        if self.valid_low_hz is not None \
                and self.valid_low_hz <= 0.0:
            raise ValueError('valid_low_hz must be > 0')
        if self.capability_state == 'ulf_calibrated':
            if self.calibration_file_ref is None:
                raise ValueError(
                    'ulf_calibrated requires a pinned calibration file')
            if not self.calibration_standard:
                raise ValueError(
                    'ulf_calibrated requires a calibration standard '
                    '(e.g. IEC TR 61094-10:2022)')
            if self.valid_low_hz is None:
                raise ValueError(
                    'ulf_calibrated requires the chain valid_low_hz')
        if self.capability_state \
                == 'ulf_characterized_with_limitations' \
                and not self.limitation_notes:
            raise ValueError(
                'ulf_characterized_with_limitations requires '
                'limitation_notes')
        if self.adc_sample_rate_hz is not None \
                and self.adc_sample_rate_hz <= 0.0:
            raise ValueError('adc_sample_rate_hz must be > 0')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'capability_id', 'capability_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'InfrasonicMeasurementCapability':
        return _seal(
            cls, payload, 'capability_id', 'capability_sha256', 'imc')


class ULFAcousticObservation(BaseModel):
    """One measured/observed sub-20 Hz quantity (ulfo- prefix). Pins the
    quantity kind, the band actually covered, the measuring capability
    chain, source/position context and uncertainty. A G-weighted level
    must pin ISO 7196 explicitly — an unlabeled 'dB' never implies G.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    capability_ref: AuthorityRef
    quantity_kind: ULFQuantityKind
    band_low_hz: float
    band_high_hz: float
    resolution_hz: float | None = None
    level_db: float | None = None
    uncertainty_db: float | None = None
    seat_ref: AuthorityRef | None = None
    source_ref: AuthorityRef | None = None
    distortion_pct: float | None = None
    compression_db: float | None = None
    excursion_limited: bool = False
    limiter_state: Literal[
        'inactive', 'engaged', 'suspected', 'unknown',
    ] = 'unknown'
    acquisition_duration_s: float | None = None
    cycles_observed: int | None = None
    window_kind: str | None = None
    method_references: tuple[str, ...] = ()
    perceptual_state: PerceptualState | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ULFAcousticObservation':
        _require_refs(self.profile_ref, self.capability_ref)
        for ref in (self.seat_ref, self.source_ref):
            if ref is not None:
                _require_refs(ref)
        if not (0.0 < self.band_low_hz < self.band_high_hz):
            raise ValueError('band bounds must satisfy 0 < low < high')
        if self.quantity_kind == 'g_weighted_level':
            if not any(
                    ref.startswith('ISO 7196@')
                    for ref in self.method_references):
                raise ValueError(
                    'g_weighted_level requires an ISO 7196@<edition> '
                    'method reference')
        for pinned in self.method_references:
            if '@' not in pinned:
                raise ValueError(
                    'method_references must pin exact editions '
                    '(standard_id@edition)')
        if (self.distortion_pct is not None
                or self.compression_db is not None) \
                and self.source_ref is None:
            raise ValueError(
                'source-output quantities require a pinned source_ref')
        if self.cycles_observed is not None \
                and self.cycles_observed < 1:
            raise ValueError('cycles_observed must be >= 1')
        if self.resolution_hz is not None \
                and self.resolution_hz <= 0.0:
            raise ValueError('resolution_hz must be > 0')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ULFAcousticObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256',
            'ulfo')


class ULFSystemQualification(BaseModel):
    """The verdict record for one requested ULF band (ulfq- prefix).

    ``measured_low_hz`` is the lowest frequency actually covered by
    bound observations — a 'measured' claim only extends as far down as
    evidence goes. Exposure interpretation is delegated to #602 and is
    never computed here.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    capability_ref: AuthorityRef
    observation_refs: tuple[AuthorityRef, ...] = ()
    requested_low_hz: float
    requested_high_hz: float
    qualification_state: ULFQualificationState = 'unknown'
    measured_low_hz: float | None = None
    limitation_reasons: tuple[str, ...] = ()
    exposure_interpretation: Literal[
        'delegated_to_issue_602', 'not_assessed',
    ] = 'not_assessed'
    perceptual_claim: PerceptualState | None = None
    staling_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ULFSystemQualification':
        _require_refs(self.profile_ref, self.capability_ref)
        for ref in self.observation_refs:
            _require_refs(ref)
        if self.staling_ref is not None:
            _require_refs(self.staling_ref)
        if not (0.0 < self.requested_low_hz
                < self.requested_high_hz <= 20.0):
            raise ValueError(
                'requested band must satisfy 0 < low < high <= 20 Hz')
        if self.qualification_state in (
                'measured', 'measured_with_limitations'):
            if not self.observation_refs:
                raise ValueError(
                    'a measured claim requires bound observations')
            if self.measured_low_hz is None:
                raise ValueError(
                    'a measured claim requires measured_low_hz')
            if self.measured_low_hz > self.requested_low_hz:
                raise ValueError(
                    'measured coverage cannot start above the '
                    'requested low bound')
        if self.qualification_state == 'measured_with_limitations' \
                and not self.limitation_reasons:
            raise ValueError(
                'measured_with_limitations requires limitation_reasons')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ULFSystemQualification':
        return _seal(
            cls, payload, 'qualification_id',
            'qualification_sha256', 'ulfq')


def evaluate_ulf_claim(
    profile: UltraLowFrequencyAcousticProfile | None,
    capability: InfrasonicMeasurementCapability | None,
    observations: tuple[ULFAcousticObservation, ...],
    qualification: ULFSystemQualification | None = None,
) -> tuple[str, str]:
    """Fail-closed sub-20 Hz claim gate (#779).

    A ULF capability verdict requires: a declared profile, a solver
    whose validated floor reaches the requested low bound (when a solver
    is pinned), a measurement chain eligible below 20 Hz, and at least
    one observation covering the requested band. Human-perception and
    exposure interpretation are out of scope — delegated to #602.
    """
    if profile is None:
        return ('insufficient_evidence', 'no_ulf_profile')
    if profile.solver_ref is not None \
            and profile.solver_validated_low_hz is not None \
            and profile.request_low_hz < profile.solver_validated_low_hz:
        return (
            'below_validated_domain',
            f'requested {profile.request_low_hz} Hz < solver validated '
            f'floor {profile.solver_validated_low_hz} Hz')
    if capability is None:
        return ('insufficient_evidence', 'no_measurement_capability')
    if capability.capability_state in (
            'audible_band_only', 'high_pass_limited', 'unknown'):
        return ('measurement_chain_inadequate',
                'capability_state:' + capability.capability_state)
    if capability.valid_low_hz is not None \
            and capability.valid_low_hz > profile.request_low_hz:
        return (
            'below_measurement_capability',
            f'chain floor {capability.valid_low_hz} Hz exceeds '
            f'requested {profile.request_low_hz} Hz')
    if capability.capability_state in (
            'noise_floor_limited', 'overload_limited'):
        return ('measurement_chain_inadequate',
                'capability_state:' + capability.capability_state)
    covering = [
        obs for obs in observations
        if obs.band_low_hz <= profile.request_low_hz
        and obs.band_high_hz >= profile.request_high_hz
        and obs.quantity_kind != 'unknown'
    ]
    if not covering:
        return ('insufficient_evidence',
                'no_observation_covers_requested_band')
    if qualification is None:
        return ('insufficient_evidence', 'no_qualification_record')
    if qualification.qualification_state == 'unknown':
        return ('unknown', 'qualification_state:unknown')
    if qualification.qualification_state == 'diagnostic_only':
        return ('diagnostic_only', 'qualification_state:diagnostic_only')
    if qualification.qualification_state \
            == 'below_measurement_capability':
        return ('below_measurement_capability',
                'qualification_state:below_measurement_capability')
    if qualification.qualification_state \
            == 'measured_with_limitations':
        return ('ulf_capability_verified_limited',
                'limitations:' + ','.join(qualification.limitation_reasons))
    if capability.capability_state \
            == 'ulf_characterized_with_limitations':
        return ('ulf_capability_verified_limited',
                'capability_limitations:'
                + ','.join(capability.limitation_notes))
    return ('ulf_capability_verified',
            'measured_coverage:' + str(qualification.measured_low_hz))
