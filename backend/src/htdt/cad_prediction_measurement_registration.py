"""REV55-REGCAL: prediction<->measurement registration authority (#564).

A ``PredictionMeasurementRegistration`` is a self-sealed, first-class authority
that binds one prediction artifact (a persisted ``CadPredictionResult`` or an
imported predicted-evidence dataset) to one measurement (a
``CadMeasurementRecord`` plus its exact datasets). Predicted and measured data
must not be compared without an explicit registration record — residuals
computed from unregistered pairs would be contaminated by coordinate mismatch,
timing-reference mismatch, mic/source position error, level-reference mismatch
and phase/windowing assumptions leaking in silently.

The gate is fail-closed: ``INCOMPARABLE`` when a hard reason exists,
``INSUFFICIENT_EVIDENCE`` when the record cannot even be judged,
``COMPARABLE_WITH_LIMITATIONS`` when shape-level comparison is valid but
absolute level, phase or timing claims are not, and ``COMPARABLE`` only when
nothing limits it. Unknown values stay explicit ``unknown`` — nominal values
are never silently substituted.

Thresholds are literature-anchored where the literature supplies them:
  * Sound-speed temperature law c = 331.4 + 0.6*T m/s and the ISO 3382-1
    Annex A.6 demand for temperature accurate to about +/-1 C — a 0.5%
    relative sound-speed mismatch (~3 C at room temperature) is the level
    where environment mismatch starts biasing predicted arrival times, so
    it becomes a comparability limitation and disables arrival-time claims.
  * IR onset detection uses a declared relative-threshold + noise-floor
    method (Defrance & Polack, JASA 123(3) 2008 — the absolute maximum of a
    RIR is not a reliable onset because a strong first reflection can
    exceed the direct arrival).
  * Correlation-based delay estimation reports a conservative half-sample
    floor on peak-location uncertainty (Knapp & Carter, IEEE Trans ASSP-24
    1976; Bendat & Piersol 1986), never a promoted "exact" alignment.
  * Reflection peak matching default tolerance 2 ms and modal peak matching
    default 5 Hz follow the spatial-resolution scales discussed for
    Bistafa & Bradley (JASA 108(4) 2000) style predicted-vs-measured
    studies: a 2 ms window corresponds to ~0.7 m of path length at room
    temperature, well inside typical mic placement uncertainty.
"""

from __future__ import annotations

import math
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical_json import canonical_json, canonical_sha256
from .cad_equipment import FrequencyDomain
from .cad_scene import Position3, Quaternion4
from .comparison import ComparisonResult, FrequencyResponse, compare_frequency_responses


# --- authority identity -------------------------------------------------

REGISTRATION_SCHEMA_VERSION = 1
REGISTRATION_AUTHORITY_VERSION = 'rev55-pm-registration-1'
COMPARABILITY_ALGORITHM_VERSION = 'rev55-comparability-1'
RESIDUAL_ALGORITHM_VERSION = 'rev55-residual-1'

#: ISO 3382-1 Annex A.6 requires temperature measurement to ~+/-1 C for room
#: acoustics; via c(T) = 331.4 + 0.6*T m/s a 0.5% sound-speed mismatch is the
#: scale at which environment divergence (~3 C) biases predicted arrivals.
SOUND_SPEED_RELATIVE_MISMATCH_TOLERANCE = 0.005

#: Default onset detection parameters (see detect_ir_onset): noise-floor
#: estimate from the leading 10% of the trace, threshold at max(8*floor,
#: 5% of absolute peak). Declared, replayable, conservative.
DEFAULT_ONSET_FRACTION = 0.05
DEFAULT_ONSET_NOISE_SIGMA = 8.0
DEFAULT_ONSET_NOISE_FRACTION = 0.10

#: Default matching tolerances. 2 ms ~ 0.7 m path at 343 m/s — inside the
#: mic-position uncertainty a survey-grade campaign should still carry; 5 Hz
#: is comfortably below axial-mode spacing in small rooms near 50-100 Hz.
DEFAULT_REFLECTION_MATCH_TOLERANCE_S = 0.002
DEFAULT_MODAL_MATCH_TOLERANCE_HZ = 5.0
DEFAULT_MODAL_PROMINENCE_DB = 3.0
DEFAULT_EARLY_REFLECTION_WINDOW_S = 0.080
DEFAULT_EARLY_REFLECTION_MIN_GAP_S = 0.0005

#: Octave evaluation bands on ISO 266 preferred centre frequencies —
#: residuals are reported per observable AND per band, never as one opaque
#: score (ISO 3382-2 evaluation practices, per-band reporting).
_OCTAVE_CENTERS_HZ = (31.5, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0)
DEFAULT_RESIDUAL_BANDS_HZ: tuple[tuple[float, float], ...] = tuple(
    (centre / math.sqrt(2.0), centre * math.sqrt(2.0)) for centre in _OCTAVE_CENTERS_HZ
)

#: Correlation delay search window defaults (records must not search
#: unbounded windows — a bounded declared window is part of provenance).
DEFAULT_CORRELATION_MAX_LAG_S = 0.010

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


# --- literals -----------------------------------------------------------

PredictionAuthorityKind = Literal[
    'prediction_result',
    'imported_prediction_dataset',
    'unknown',
]

SpatialRegistrationMethod = Literal[
    'exact_scene_xyz',
    'named_position_binding',
    'local_frame_transform',
    'manual_correction',
    'unknown',
]

CoordinateFrameKind = Literal['scene', 'local_frame', 'unknown']

RegistrationProvenance = Literal[
    'surveyed',
    'tracked',
    'captured_annotated',
    'imported',
    'manual',
    'unknown',
]

TimeReferenceMethod = Literal[
    'exact_reference',
    'acoustic_timing_reference',
    'known_hardware_latency',
    'estimated_from_direct_arrival',
    'estimated_by_correlation',
    'unknown',
]

PhaseValidityState = Literal[
    'valid',
    'invalid_after_processing',
    'absent',
    'unknown',
]

LevelReferenceState = Literal[
    'absolute_calibrated',
    'absolute_uncalibrated',
    'relative_only',
    'unknown',
]

RegistrationPartition = Literal[
    'calibration',
    'holdout',
    'repeatability',
    'unassigned',
]

ComparabilityState = Literal[
    'comparable',
    'comparable_with_limitations',
    'incomparable',
    'insufficient_evidence',
]

RegistrationFreshness = Literal['current', 'stale_geometry_revision']

ResidualObservableKind = Literal[
    'magnitude_db',
    'phase_deg',
    'direct_arrival_time',
    'early_reflection_time',
    'modal_peak_frequency',
    'decay_time',
]

ResidualObservableState = Literal['computed', 'unsupported', 'insufficient_data']

ProcessingOperationKind = Literal[
    'window',
    'gate',
    'resample',
    'smoothing',
    'normalization',
    'other',
]


# --- helpers ------------------------------------------------------------


def _finite(value: float, *, field_name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f'{field_name} must be finite')
    return result


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _hash(payload: dict[str, Any]) -> str:
    return canonical_sha256(payload)


def _position_delta_m(a: Position3 | None, b: Position3 | None) -> float | None:
    if a is None or b is None:
        return None
    return math.sqrt(
        (a.x_m - b.x_m) ** 2 + (a.y_m - b.y_m) ** 2 + (a.z_m - b.z_m) ** 2
    )


# --- sub-authorities ----------------------------------------------------


class LocalFrameBinding(BaseModel):
    """Local measurement frame -> scene frame rigid transform.

    A measured point ``p_local`` maps to the scene frame as
    ``origin + R * p_local`` where ``R`` is the declared orientation.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    frame_label: str | None = None
    origin: Position3
    orientation: Quaternion4
    provenance: RegistrationProvenance = 'unknown'
    uncertainty_m: float | None = Field(default=None, ge=0.0)

    @field_validator('uncertainty_m')
    @classmethod
    def finite_uncertainty(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='local frame uncertainty_m')

    def to_scene(self, local: Position3) -> Position3:
        from .cad_scene import quaternion_to_matrix3

        matrix = quaternion_to_matrix3(self.orientation)
        local_xyz = (local.x_m, local.y_m, local.z_m)
        rotated = tuple(
            sum(matrix[row][column] * local_xyz[column] for column in range(3))
            for row in range(3)
        )
        return Position3(
            x_m=self.origin.x_m + rotated[0],
            y_m=self.origin.y_m + rotated[1],
            z_m=self.origin.z_m + rotated[2],
        )


class ManualPositionCorrection(BaseModel):
    """A bounded manual position correction with explicit provenance.

    Corrections are never hidden inside solver parameters: they are a
    first-class, inspectable part of the registration record. ``bound_m``
    is the declared maximum permissible correction magnitude — the gate can
    therefore tell a bounded manual fix from an arbitrary repositioning.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    applied_offset_m: tuple[float, float, float]
    bound_m: float = Field(gt=0.0)
    reason: str = Field(min_length=1)
    provenance: RegistrationProvenance = 'manual'

    @field_validator('applied_offset_m')
    @classmethod
    def finite_offsets(
        cls, value: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        return tuple(
            _finite(component, field_name='manual correction offset')
            for component in value
        )

    @model_validator(mode='after')
    def bounded(self) -> 'ManualPositionCorrection':
        magnitude = math.sqrt(sum(component**2 for component in self.applied_offset_m))
        if magnitude > self.bound_m:
            raise ValueError('manual correction exceeds its declared bound')
        return self


class RegistrationEndpoint(BaseModel):
    """One end of the predicted<->measured mapping.

    ``predicted_*`` is the position/orientation the prediction used (a scene
    entity position in the registration's pinned SceneRevision).
    ``measured_*`` is where the instrument actually was, expressed in the
    declared ``measurement_frame`` — usually an operator-declared scene
    coordinate or a surveyed seat position. A missing value is UNKNOWN; the
    comparability gate refuses to pretend otherwise.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    role: Literal['source', 'receiver']
    entity_id: str | None = None
    predicted_position: Position3 | None = None
    predicted_orientation: Quaternion4 | None = None
    measured_position: Position3 | None = None
    measured_orientation: Quaternion4 | None = None
    #: The measured position as declared, before any local-frame transform or
    #: manual correction — the raw input stays auditable on the record.
    measured_position_declared: Position3 | None = None
    position_uncertainty_m: float | None = Field(default=None, ge=0.0)
    position_delta_m: float | None = Field(default=None, ge=0.0)

    @field_validator('position_uncertainty_m', 'position_delta_m')
    @classmethod
    def finite_distance(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='position metric')

    @model_validator(mode='after')
    def consistent_delta(self) -> 'RegistrationEndpoint':
        delta = _position_delta_m(self.measured_position, self.predicted_position)
        if self.position_delta_m is None:
            if delta is not None:
                raise ValueError('position_delta_m must be recorded when both positions are known')
        elif delta is None or not math.isclose(delta, self.position_delta_m, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError('position_delta_m must equal |measured - predicted|')
        return self


class SpatialRegistration(BaseModel):
    """The spatial part of the registration (issue #564 section 2)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    method: SpatialRegistrationMethod
    measurement_frame: CoordinateFrameKind = 'scene'
    scene_frame: Literal['scene'] = 'scene'
    local_frame: LocalFrameBinding | None = None
    manual_correction: ManualPositionCorrection | None = None
    named_position_entity_id: str | None = None
    provenance: RegistrationProvenance = 'unknown'
    position_tolerance_m: float | None = Field(default=None, ge=0.0)

    @field_validator('position_tolerance_m')
    @classmethod
    def finite_tolerance(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='position tolerance')

    @model_validator(mode='after')
    def consistent(self) -> 'SpatialRegistration':
        if self.measurement_frame == 'local_frame' and self.local_frame is None:
            raise ValueError('local_frame measurement frame requires a local frame binding')
        if self.method == 'local_frame_transform' and self.local_frame is None:
            raise ValueError('local_frame_transform registration requires a local frame binding')
        if self.method == 'named_position_binding' and not self.named_position_entity_id:
            raise ValueError('named_position_binding requires the named entity id')
        if self.method == 'manual_correction' and self.manual_correction is None:
            raise ValueError('manual_correction registration requires a correction record')
        if self.manual_correction is not None and self.method != 'manual_correction':
            raise ValueError('a manual correction must use the manual_correction method')
        if self.manual_correction is not None:
            correction = self.manual_correction
            if self.position_tolerance_m is None:
                raise ValueError('manual corrections require a declared position tolerance')
            correction_magnitude = math.sqrt(
                sum(component**2 for component in correction.applied_offset_m)
            )
            if correction_magnitude > self.position_tolerance_m:
                raise ValueError('manual correction exceeds the registration tolerance')
        return self


class TimingRegistration(BaseModel):
    """The time base of the comparison (issue #564 section 3).

    ``method`` says what kind of time reference the measurement carries.
    Estimated alignments (direct-arrival / correlation) stay ESTIMATED — they
    are never promoted to an exact hardware reference, and absolute phase
    claims require ``exact_reference``, ``acoustic_timing_reference`` or
    ``known_hardware_latency``.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    method: TimeReferenceMethod
    #: Authoritative offset applied to the measured time axis (seconds added
    #: to measurement time to express it in the prediction's reference).
    #: ``None`` means the offset is UNKNOWN — never silently zero.
    applied_offset_s: float | None = None
    reference_channel: str | None = None
    reference_event: str | None = None
    propagation_delay_s: float | None = None
    hardware_latency_s: float | None = None
    ir_time_zero_offset_s: float | None = None
    uncertainty_s: float | None = Field(default=None, ge=0.0)
    #: Declared provenance of the method (e.g. a persisted timing-reference
    #: authority id, or the estimation algorithm run).
    reference_authority_id: str | None = None
    reference_authority_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    #: What the measurement-side authority declared (acoustic_reference,
    #: loopback, imported, ...) — kept separate from ``method`` so a manual
    #: declaration is recorded but never upgrades the comparability claim.
    declared_reference_semantics: str | None = None
    t0_semantics: str = 'unknown'
    absolute_phase_valid: PhaseValidityState = 'unknown'

    @field_validator(
        'applied_offset_s',
        'propagation_delay_s',
        'hardware_latency_s',
        'ir_time_zero_offset_s',
        'uncertainty_s',
    )
    @classmethod
    def finite_time(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='timing field')

    @model_validator(mode='after')
    def consistent(self) -> 'TimingRegistration':
        if self.method in (
            'estimated_from_direct_arrival',
            'estimated_by_correlation',
        ):
            if self.applied_offset_s is None:
                raise ValueError('estimated alignments must record the applied offset')
            if self.uncertainty_s is None:
                raise ValueError('estimated alignments must record an uncertainty')
        if self.method == 'known_hardware_latency' and self.hardware_latency_s is None:
            raise ValueError('known-hardware-latency timing must record the latency')
        return self


class LevelRegistration(BaseModel):
    """Level-reference and calibration state (issue #564 section 4)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    level_state: LevelReferenceState
    #: Level offset applied to the measured axis before comparison (dB added
    #: to the measurement). ``None`` = no declared offset / unknown.
    level_offset_db: float | None = None
    normalization_state: Literal['none', 'normalized', 'unknown'] = 'unknown'
    mic_calibration_profile: str | None = None
    mic_calibration_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    soundcard_calibration_state: Literal['calibrated', 'uncalibrated', 'unknown'] = 'unknown'
    gain_routing_state: str | None = None
    level_reference_authority_id: str | None = None
    level_reference_authority_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    declared_level_reference_kind: str | None = None

    @field_validator('level_offset_db')
    @classmethod
    def finite_level(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='level offset')


class EnvironmentRegistration(BaseModel):
    """Environment state relevant to sound speed (issue #564 section 5)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    sound_speed_m_s: float | None = Field(default=None, gt=0.0)
    temperature_c: float | None = None
    relative_humidity_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    prediction_sound_speed_m_s: float | None = Field(default=None, gt=0.0)
    prediction_temperature_c: float | None = None
    sound_speed_relative_difference: float | None = None

    @field_validator('sound_speed_m_s', 'prediction_sound_speed_m_s')
    @classmethod
    def finite_speed(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='sound speed')

    @field_validator('temperature_c', 'prediction_temperature_c')
    @classmethod
    def finite_temperature(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='temperature')

    @model_validator(mode='after')
    def consistent_difference(self) -> 'EnvironmentRegistration':
        if (
            self.sound_speed_m_s is not None
            and self.prediction_sound_speed_m_s is not None
        ):
            difference = (
                abs(self.sound_speed_m_s - self.prediction_sound_speed_m_s)
                / self.prediction_sound_speed_m_s
            )
            if self.sound_speed_relative_difference is None:
                raise ValueError('sound speed relative difference must be recorded')
            if not math.isclose(
                difference, self.sound_speed_relative_difference,
                rel_tol=1e-9, abs_tol=1e-9,
            ):
                raise ValueError('sound speed relative difference mismatch')
        elif self.sound_speed_relative_difference is not None:
            raise ValueError('sound speed difference requires both speeds')
        return self


def sound_speed_from_temperature_c(temperature_c: float) -> float:
    """c(T) = 331.4 + 0.6*T m/s (standard textbook/ISO reference law)."""

    return 331.4 + 0.6 * _finite(temperature_c, field_name='temperature_c')


class ProcessingOperation(BaseModel):
    """One auditable windowing/gating/resampling/smoothing operation."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: ProcessingOperationKind
    label: str = Field(min_length=1)
    parameters_json: str = '{}'
    provenance: RegistrationProvenance = 'unknown'


class PredictionAuthorityBinding(BaseModel):
    """Exactly which prediction artifact the registration pins."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: PredictionAuthorityKind
    prediction_id: str = Field(min_length=1)
    prediction_sha256: str = Field(pattern=_SHA256_PATTERN)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    model_id: str | None = None
    model_version: str | None = None
    solver_label: str | None = None
    provider_id: str | None = None
    provider_evidence_state: str | None = None
    source_entity_ids: tuple[str, ...] = ()
    receiver_entity_id: str | None = None
    predicted_frequency_domain: FrequencyDomain | None = None
    fidelity_state: str | None = None

    @model_validator(mode='after')
    def non_empty_sources(self) -> 'PredictionAuthorityBinding':
        if any(not entity_id for entity_id in self.source_entity_ids):
            raise ValueError('prediction source entity ids must be non-empty')
        if len(set(self.source_entity_ids)) != len(self.source_entity_ids):
            raise ValueError('prediction source entity ids must be unique')
        return self


class MeasurementAuthorityBinding(BaseModel):
    """Exactly which measurement (and datasets) the registration pins."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    measurement_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    dataset_id: str | None = None
    dataset_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    ir_dataset_id: str | None = None
    ir_dataset_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    acquisition_context_id: str | None = None
    acquisition_context_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    timing_reference_id: str | None = None
    timing_reference_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    level_reference_id: str | None = None
    level_reference_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    measured_frequency_domain: FrequencyDomain | None = None
    radiation_scope: str = 'unknown'
    routing_evidence: str = 'unknown'
    evidence_type: str = 'unknown'

    @model_validator(mode='after')
    def consistent_datasets(self) -> 'MeasurementAuthorityBinding':
        if (self.dataset_id is None) != (self.dataset_sha256 is None):
            raise ValueError('dataset binding requires both id and sha256')
        if (self.ir_dataset_id is None) != (self.ir_dataset_sha256 is None):
            raise ValueError('IR dataset binding requires both id and sha256')
        if (self.acquisition_context_id is None) != (
            self.acquisition_context_sha256 is None
        ):
            raise ValueError('acquisition-context binding requires both id and sha256')
        if (self.timing_reference_id is None) != (
            self.timing_reference_sha256 is None
        ):
            raise ValueError('timing-reference binding requires both id and sha256')
        if (self.level_reference_id is None) != (
            self.level_reference_sha256 is None
        ):
            raise ValueError('level-reference binding requires both id and sha256')
        return self


# --- comparability gate ---------------------------------------------------


class ComparabilityVerdict(BaseModel):
    """Machine-readable comparability state + explicit reason codes."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    state: ComparabilityState
    algorithm_version: str = COMPARABILITY_ALGORITHM_VERSION
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    comparable_band_hz: tuple[float, float] | None = None
    magnitude_supported: bool = False
    absolute_level_supported: bool = False
    phase_supported: bool = False
    arrival_time_supported: bool = False

    @model_validator(mode='after')
    def consistent(self) -> 'ComparabilityVerdict':
        if self.state == 'incomparable' and not self.reasons:
            raise ValueError('INCOMPARABLE verdicts require reason codes')
        if self.state == 'insufficient_evidence' and not (self.reasons or self.limitations):
            raise ValueError('INSUFFICIENT_EVIDENCE verdicts require reason codes')
        if self.state == 'comparable' and (self.reasons or self.limitations):
            raise ValueError('COMPARABLE verdicts carry no reasons or limitations')
        if self.state == 'comparable_with_limitations' and not self.limitations:
            raise ValueError('COMPARABLE_WITH_LIMITATIONS verdicts require limitations')
        if self.comparable_band_hz is not None:
            low, high = self.comparable_band_hz
            if not (math.isfinite(low) and math.isfinite(high)) or high <= low:
                raise ValueError('comparable band must be a finite increasing pair')
        return self


def evaluate_comparability(
    registration: 'PredictionMeasurementRegistration',
) -> ComparabilityVerdict:
    """Evaluate whether the pinned pair may be compared, and which claims hold.

    Fail-closed: any hard reason produces ``incomparable``; missing
    information needed even to judge produces ``insufficient_evidence``;
    otherwise ``comparable_with_limitations`` when any claim is limited, else
    ``comparable``. Nothing here may upgrade an ``unknown`` to a capability.
    """

    hard_reasons: list[str] = []
    limitations: list[str] = []

    geometry_mismatch = (
        registration.prediction.scene_revision_id != registration.scene_revision_id
        or registration.prediction.scene_content_hash != registration.scene_content_hash
        or registration.measurement.scene_revision_id != registration.scene_revision_id
        or registration.measurement.scene_content_hash != registration.scene_content_hash
    )
    if geometry_mismatch:
        hard_reasons.append('geometry_revision_mismatch')

    receiver_position_known = registration.receiver.measured_position is not None
    if not receiver_position_known:
        hard_reasons.append('receiver_position_unknown')
    if registration.source.measured_position is None:
        limitations.append('source_position_unknown')

    if (
        registration.measurement.radiation_scope in ('bass_managed', 'mixed')
        and len(registration.prediction.source_entity_ids) <= 1
    ):
        hard_reasons.append('routing_topology_mismatch')

    predicted_domain = registration.prediction.predicted_frequency_domain
    measured_domain = registration.measurement.measured_frequency_domain
    comparable_band: tuple[float, float] | None = None
    if predicted_domain is None or measured_domain is None:
        limitations.append('frequency_domain_evidence_missing')
    else:
        overlap_low = max(predicted_domain.minimum_hz, measured_domain.minimum_hz)
        overlap_high = min(predicted_domain.maximum_hz, measured_domain.maximum_hz)
        if overlap_high <= overlap_low:
            hard_reasons.append('unsupported_frequency_domain')
        else:
            comparable_band = (overlap_low, overlap_high)
            union_low = min(predicted_domain.minimum_hz, measured_domain.minimum_hz)
            union_high = max(predicted_domain.maximum_hz, measured_domain.maximum_hz)
            if overlap_low > union_low or overlap_high < union_high:
                limitations.append('restricted_frequency_range')

    timing = registration.timing
    if timing.method == 'unknown':
        limitations.append(
            'timing_reference_unverified'
            if timing.declared_reference_semantics not in (None, 'unknown')
            else 'timing_reference_unknown'
        )
    elif timing.method in (
        'estimated_from_direct_arrival',
        'estimated_by_correlation',
    ):
        limitations.append('timing_estimated_not_exact')
    if timing.method != 'unknown' and timing.uncertainty_s is None:
        limitations.append('timing_uncertainty_undeclared')

    phase_supported = (
        timing.absolute_phase_valid == 'valid'
        and timing.method
        in ('exact_reference', 'acoustic_timing_reference', 'known_hardware_latency')
    )
    if not phase_supported:
        if timing.absolute_phase_valid == 'valid':
            limitations.append('absolute_phase_invalidated_by_timing')
        else:
            limitations.append(f'phase_{timing.absolute_phase_valid}')

    level = registration.level
    absolute_level_supported = (
        level.level_state == 'absolute_calibrated'
        and level.mic_calibration_sha256 is not None
    )
    if not absolute_level_supported:
        if level.level_state == 'absolute_calibrated':
            limitations.append('microphone_calibration_unavailable')
        elif level.level_state == 'absolute_uncalibrated':
            limitations.append('absolute_spl_unsupported')
        elif level.level_state == 'relative_only':
            limitations.append('relative_level_only')
        else:
            limitations.append('level_reference_unknown')

    if any(op.provenance == 'unknown' for op in registration.processing_operations):
        limitations.append('processing_provenance_incomplete')

    environment = registration.environment
    arrival_time_supported = (
        registration.measurement.ir_dataset_sha256 is not None
        and timing.method != 'unknown'
    )
    if environment.sound_speed_relative_difference is not None and (
        environment.sound_speed_relative_difference
        > SOUND_SPEED_RELATIVE_MISMATCH_TOLERANCE
    ):
        limitations.append('environment_sound_speed_mismatch')
        arrival_time_supported = False
    elif (
        environment.sound_speed_m_s is None
        and environment.prediction_sound_speed_m_s is None
    ):
        limitations.append('environment_sound_speed_unknown')

    if registration.spatial.provenance == 'unknown':
        limitations.append('spatial_provenance_unknown')
    if registration.receiver.position_uncertainty_m is None:
        limitations.append('receiver_position_uncertainty_unknown')

    magnitude_supported = comparable_band is not None and not hard_reasons
    if not magnitude_supported and not hard_reasons:
        limitations.append('magnitude_domain_unavailable')

    if hard_reasons:
        state: ComparabilityState = 'incomparable'
    elif comparable_band is None:
        state = 'insufficient_evidence'
    else:
        state = 'comparable_with_limitations' if limitations else 'comparable'

    return ComparabilityVerdict(
        state=state,
        reasons=tuple(sorted(set(hard_reasons))),
        limitations=tuple(sorted(set(limitations))),
        comparable_band_hz=comparable_band if magnitude_supported else None,
        magnitude_supported=magnitude_supported,
        absolute_level_supported=absolute_level_supported and magnitude_supported,
        phase_supported=phase_supported and magnitude_supported,
        arrival_time_supported=arrival_time_supported and magnitude_supported,
    )


# --- the registration record ---------------------------------------------


class PredictionMeasurementRegistration(BaseModel):
    """Self-sealed registration authority (issue #564 section 1)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    registration_id: str = Field(min_length=1)
    authority_version: str = REGISTRATION_AUTHORITY_VERSION
    schema_version: int = REGISTRATION_SCHEMA_VERSION
    document_id: str = Field(min_length=1)
    #: The SceneRevision this registration is anchored to — both bindings
    #: must pin this same revision for the pair to be comparable.
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    system_variant_id: str | None = None
    partition: RegistrationPartition = 'unassigned'
    campaign_id: str | None = None

    prediction: PredictionAuthorityBinding
    measurement: MeasurementAuthorityBinding
    source: RegistrationEndpoint
    receiver: RegistrationEndpoint
    spatial: SpatialRegistration
    timing: TimingRegistration
    level: LevelRegistration
    environment: EnvironmentRegistration = EnvironmentRegistration(
        sound_speed_m_s=None,
        temperature_c=None,
    )
    processing_operations: tuple[ProcessingOperation, ...] = ()

    registration_method: str = Field(min_length=1)
    algorithm_version: str = COMPARABILITY_ALGORITHM_VERSION
    confidence_label: str | None = None
    recorded_limitations: tuple[str, ...] = ()
    provenance_json: str = '{}'
    created_at_utc: str = Field(min_length=1)
    comparability: ComparabilityVerdict
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def consistent(self) -> 'PredictionMeasurementRegistration':
        _require_iso8601(self.created_at_utc, 'registration created_at_utc')
        if self.source.role != 'source' or self.receiver.role != 'receiver':
            raise ValueError('endpoint roles must be source/receiver')
        evaluated = evaluate_comparability(self)
        if evaluated != self.comparability:
            raise ValueError('comparability verdict must match evaluation')
        if len(set(self.recorded_limitations)) != len(self.recorded_limitations):
            raise ValueError('limitations must be unique')
        payload = self.semantic_payload()
        if self.semantic_sha256 != _hash(payload):
            raise ValueError('registration semantic hash mismatch')
        expected_id = f'pm-registration:{self.semantic_sha256}'
        if self.registration_id != expected_id:
            raise ValueError('registration id must be pm-registration:<semantic sha256>')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json', exclude={'registration_id', 'semantic_sha256'})
        return payload


def build_prediction_measurement_registration(
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    prediction: PredictionAuthorityBinding,
    measurement: MeasurementAuthorityBinding,
    source: RegistrationEndpoint,
    receiver: RegistrationEndpoint,
    spatial: SpatialRegistration,
    timing: TimingRegistration,
    level: LevelRegistration,
    registration_method: str,
    environment: EnvironmentRegistration | None = None,
    processing_operations: tuple[ProcessingOperation, ...] = (),
    system_variant_id: str | None = None,
    partition: RegistrationPartition = 'unassigned',
    campaign_id: str | None = None,
    confidence_label: str | None = None,
    recorded_limitations: tuple[str, ...] = (),
    provenance_json: str = '{}',
    created_at_utc: str,
) -> PredictionMeasurementRegistration:
    """Build a sealed registration: evaluate the gate, then pin identity."""

    if environment is None:
        environment = EnvironmentRegistration(
            sound_speed_m_s=None, temperature_c=None
        )
    fields: dict[str, Any] = {
        'registration_id': 'pending',
        'authority_version': REGISTRATION_AUTHORITY_VERSION,
        'schema_version': REGISTRATION_SCHEMA_VERSION,
        'document_id': document_id,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'system_variant_id': system_variant_id,
        'partition': partition,
        'campaign_id': campaign_id,
        'prediction': prediction,
        'measurement': measurement,
        'source': source,
        'receiver': receiver,
        'spatial': spatial,
        'timing': timing,
        'level': level,
        'environment': environment,
        'processing_operations': processing_operations,
        'registration_method': registration_method,
        'algorithm_version': COMPARABILITY_ALGORITHM_VERSION,
        'confidence_label': confidence_label,
        'recorded_limitations': recorded_limitations,
        'provenance_json': provenance_json,
        'created_at_utc': created_at_utc,
        'comparability': ComparabilityVerdict(
            state='insufficient_evidence', reasons=('pending',)
        ),
        'semantic_sha256': '0' * 64,
    }
    provisional = PredictionMeasurementRegistration.model_construct(**fields)
    verdict = evaluate_comparability(provisional)
    fields['comparability'] = verdict
    provisional = PredictionMeasurementRegistration.model_construct(**fields)
    sha256 = _hash(provisional.semantic_payload())
    fields['registration_id'] = f'pm-registration:{sha256}'
    fields['semantic_sha256'] = sha256
    return PredictionMeasurementRegistration(**fields)


# --- freshness ------------------------------------------------------------


def evaluate_registration_freshness(
    registration: PredictionMeasurementRegistration,
    *,
    current_scene_revision_id: str | None,
    current_scene_content_hash: str | None,
) -> RegistrationFreshness:
    """Live staleness check against the document's current head.

    A registration pinned to a superseded revision is stale — residuals
    computed from it would silently compare against obsolete geometry.
    """

    if current_scene_revision_id is None or current_scene_content_hash is None:
        return 'current'
    if (
        current_scene_revision_id == registration.scene_revision_id
        and current_scene_content_hash == registration.scene_content_hash
    ):
        return 'current'
    return 'stale_geometry_revision'


def assert_partition_disjoint(
    registrations: tuple[PredictionMeasurementRegistration, ...] | list[PredictionMeasurementRegistration],
) -> None:
    """Fail-closed partition enforcement at exact measurement identity.

    A measurement registered under ``calibration`` must never also appear
    under ``holdout`` — holdout measurements cannot be used during fitting.
    """

    calibration_ids: set[str] = set()
    holdout_ids: set[str] = set()
    for registration in registrations:
        measurement_id = registration.measurement.measurement_id
        if registration.partition == 'calibration':
            calibration_ids.add(measurement_id)
        elif registration.partition == 'holdout':
            holdout_ids.add(measurement_id)
    overlap = calibration_ids & holdout_ids
    if overlap:
        raise ValueError(
            'measurements cannot be both calibration and holdout evidence: '
            + ', '.join(sorted(overlap))
        )


# --- residual computation -------------------------------------------------


class ResidualComputationSpec(BaseModel):
    """Sealed processing parameters a residual report was computed under."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    algorithm_version: str = RESIDUAL_ALGORITHM_VERSION
    bands_hz: tuple[tuple[float, float], ...] = DEFAULT_RESIDUAL_BANDS_HZ
    reference_band_hz: tuple[float, float] | None = None
    level_offset_applied_db: float | None = None
    onset_method: Literal['threshold_relative_v1'] = 'threshold_relative_v1'
    onset_fraction: float = Field(default=DEFAULT_ONSET_FRACTION, gt=0.0, lt=1.0)
    onset_noise_sigma: float = Field(default=DEFAULT_ONSET_NOISE_SIGMA, gt=0.0)
    onset_noise_fraction: float = Field(
        default=DEFAULT_ONSET_NOISE_FRACTION, gt=0.0, lt=1.0
    )
    reflection_match_tolerance_s: float = Field(
        default=DEFAULT_REFLECTION_MATCH_TOLERANCE_S, gt=0.0
    )
    early_reflection_window_s: float = Field(
        default=DEFAULT_EARLY_REFLECTION_WINDOW_S, gt=0.0
    )
    early_reflection_min_gap_s: float = Field(
        default=DEFAULT_EARLY_REFLECTION_MIN_GAP_S, ge=0.0
    )
    modal_match_tolerance_hz: float = Field(
        default=DEFAULT_MODAL_MATCH_TOLERANCE_HZ, gt=0.0
    )
    modal_prominence_db: float = Field(default=DEFAULT_MODAL_PROMINENCE_DB, gt=0.0)
    modal_band_hz: tuple[float, float] = (20.0, 300.0)
    predicted_response_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    measured_response_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    ir_dataset_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def consistent(self) -> 'ResidualComputationSpec':
        for band in self.bands_hz:
            if not (
                math.isfinite(band[0])
                and math.isfinite(band[1])
                and band[1] > band[0] > 0
            ):
                raise ValueError('residual bands must be finite increasing pairs')
        if self.reference_band_hz is not None:
            band = self.reference_band_hz
            if not (math.isfinite(band[0]) and math.isfinite(band[1]) and band[1] > band[0]):
                raise ValueError('reference band must be a finite increasing pair')
        for band in (self.modal_band_hz,):
            if not (
                math.isfinite(band[0])
                and math.isfinite(band[1])
                and band[1] > band[0] > 0
            ):
                raise ValueError('modal band must be a finite increasing pair')
        payload = self.model_dump(mode='json', exclude={'spec_sha256'})
        if self.spec_sha256 != canonical_sha256(payload):
            raise ValueError('residual spec hash mismatch')
        return self


def build_residual_computation_spec(
    **kwargs: Any,
) -> ResidualComputationSpec:
    payload = dict(kwargs)
    payload.pop('spec_sha256', None)
    spec = ResidualComputationSpec.model_construct(**payload, spec_sha256='0' * 64)
    canonical = spec.model_dump(mode='json', exclude={'spec_sha256'})
    digest = canonical_sha256(canonical)
    return ResidualComputationSpec(**payload, spec_sha256=digest)


class BandMagnitudeResidual(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    band_hz: tuple[float, float]
    valid_points: int = Field(ge=0)
    mean_difference_db: float | None = None
    rms_difference_db: float | None = None
    level_offset_db: float | None = None
    shape_rms_db: float | None = None

    @field_validator('mean_difference_db', 'rms_difference_db', 'level_offset_db', 'shape_rms_db')
    @classmethod
    def finite_metric(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='band residual')


class BandPhaseResidual(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    band_hz: tuple[float, float]
    valid_points: int = Field(ge=0)
    mean_abs_difference_deg: float | None = None
    rms_difference_deg: float | None = None
    max_abs_difference_deg: float | None = None

    @field_validator('mean_abs_difference_deg', 'rms_difference_deg', 'max_abs_difference_deg')
    @classmethod
    def finite_metric(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='phase residual')


class DirectArrivalResidual(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    measured_onset_s: float | None = None
    predicted_delay_s: float | None = None
    error_s: float | None = None
    error_m: float | None = None
    sound_speed_m_s: float | None = None
    onset_method: str = 'threshold_relative_v1'

    @field_validator('measured_onset_s', 'predicted_delay_s', 'error_s', 'error_m', 'sound_speed_m_s')
    @classmethod
    def finite_field(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='direct-arrival field')


class ReflectionMatchResidual(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    predicted_delay_s: float
    measured_delay_s: float | None = None
    error_s: float | None = None
    matched: bool
    label: str | None = None


class EarlyReflectionResidual(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    events: tuple[ReflectionMatchResidual, ...] = ()
    unmatched_predicted: int = Field(ge=0)
    measured_peak_count: int = Field(ge=0)


class ModalPeakPair(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    measured_peak_hz: float
    predicted_mode_hz: float
    error_hz: float


class ModalPeakResidual(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    pairs: tuple[ModalPeakPair, ...] = ()
    unmatched_predicted_modes: tuple[float, ...] = ()
    unmatched_measured_peaks: tuple[float, ...] = ()


class ResidualObservable(BaseModel):
    """One observable's residual result — computed, unsupported or
    insufficient-data, always with a reason when not computed."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    observable: ResidualObservableKind
    state: ResidualObservableState
    reason: str | None = None
    magnitude_bands: tuple[BandMagnitudeResidual, ...] = ()
    phase_bands: tuple[BandPhaseResidual, ...] = ()
    direct_arrival: DirectArrivalResidual | None = None
    early_reflections: EarlyReflectionResidual | None = None
    modal_peaks: ModalPeakResidual | None = None

    @model_validator(mode='after')
    def consistent(self) -> 'ResidualObservable':
        if self.state != 'computed':
            if not self.reason:
                raise ValueError('non-computed observables require a reason')
            if (
                self.magnitude_bands
                or self.phase_bands
                or self.direct_arrival is not None
                or self.early_reflections is not None
                or self.modal_peaks is not None
            ):
                raise ValueError('non-computed observables must not carry results')
            return self
        populated = sum(
            bool(group)
            for group in (
                self.magnitude_bands,
                self.phase_bands,
            )
        ) + sum(
            item is not None
            for item in (self.direct_arrival, self.early_reflections, self.modal_peaks)
        )
        if populated == 0:
            raise ValueError('computed observables require results')
        return self


class PredictionMeasurementResidualReport(BaseModel):
    """Sealed residual report bound to one exact registration record."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    report_id: str = Field(min_length=1)
    registration_id: str = Field(min_length=1)
    registration_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    comparability_state: ComparabilityState
    partition: RegistrationPartition
    spec: ResidualComputationSpec
    observables: tuple[ResidualObservable, ...]
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def consistent(self) -> 'PredictionMeasurementResidualReport':
        _require_iso8601(self.created_at_utc, 'residual report created_at_utc')
        if self.comparability_state not in ('comparable', 'comparable_with_limitations'):
            raise ValueError(
                'residual reports are refused when the pair is not comparable'
            )
        observables = [observable.observable for observable in self.observables]
        if len(set(observables)) != len(observables):
            raise ValueError('observable entries must be unique')
        payload = self.model_dump(mode='json', exclude={'report_id', 'semantic_sha256'})
        if self.semantic_sha256 != canonical_sha256(payload):
            raise ValueError('residual report semantic hash mismatch')
        expected_id = f'pm-residual:{self.semantic_sha256}'
        if self.report_id != expected_id:
            raise ValueError('residual report id must be pm-residual:<semantic sha256>')
        return self


# --- IR signal processing -------------------------------------------------


def detect_ir_onset(
    amplitudes: tuple[float, ...] | list[float] | np.ndarray,
    sample_rate_hz: float,
    start_time_s: float = 0.0,
    *,
    onset_fraction: float = DEFAULT_ONSET_FRACTION,
    noise_sigma: float = DEFAULT_ONSET_NOISE_SIGMA,
    noise_fraction: float = DEFAULT_ONSET_NOISE_FRACTION,
) -> tuple[float, int, float, float]:
    """Threshold-relative onset detection.

    Returns ``(onset_s, onset_index, noise_floor, peak_amplitude)``. The
    noise floor is the median absolute amplitude over the leading
    ``noise_fraction`` of the trace (before the global peak is included) and
    the onset is the first sample whose magnitude exceeds
    ``max(noise_sigma * floor, onset_fraction * |peak|)``. The absolute
    maximum is *not* used as the onset: a strong early reflection can
    exceed the direct arrival (Defrance & Polack 2008).
    """

    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise ValueError('sample rate must be finite and positive')
    if not math.isfinite(start_time_s):
        raise ValueError('start time must be finite')
    if not (0.0 < onset_fraction < 1.0):
        raise ValueError('onset fraction must be in (0, 1)')
    if not (0.0 < noise_fraction < 1.0):
        raise ValueError('noise fraction must be in (0, 1)')
    samples = np.asarray(amplitudes, dtype=np.float64)
    if samples.size == 0:
        raise ValueError('IR must contain samples')
    if not np.all(np.isfinite(samples)):
        raise ValueError('IR samples must be finite')

    magnitudes = np.abs(samples)
    peak_index = int(np.argmax(magnitudes))
    peak = float(magnitudes[peak_index])

    leading_count = max(1, min(int(samples.size * noise_fraction), peak_index))
    noise_floor = float(np.median(magnitudes[:leading_count])) if leading_count else 0.0
    threshold = max(noise_sigma * noise_floor, onset_fraction * peak)
    above = np.nonzero(magnitudes >= threshold)[0]
    onset_index = int(above[0]) if above.size else peak_index
    onset_s = start_time_s + onset_index / sample_rate_hz
    return onset_s, onset_index, noise_floor, peak


def estimate_delay_by_correlation(
    reference: tuple[float, ...] | list[float] | np.ndarray,
    target: tuple[float, ...] | list[float] | np.ndarray,
    sample_rate_hz: float,
    *,
    max_lag_s: float = DEFAULT_CORRELATION_MAX_LAG_S,
) -> tuple[float, float, float]:
    """Cross-correlation delay estimate with honest uncertainty.

    Returns ``(delay_s, peak_norm, uncertainty_s)``: the lag of the
    normalized cross-correlation peak between ``reference`` and ``target``
    inside ``+/-max_lag_s``, the normalized peak magnitude, and a
    conservative half-sample uncertainty floor (Knapp & Carter 1976 show the
    true bound depends on SNR and bandwidth — a half-sample floor is never
    optimistic). Parabolic interpolation around the peak provides
    sub-sample refinement of the estimate, but the reported uncertainty is
    not reduced below the half-sample floor.
    """

    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise ValueError('sample rate must be finite and positive')
    if not math.isfinite(max_lag_s) or max_lag_s <= 0:
        raise ValueError('max lag must be finite and positive')
    reference_array = np.asarray(reference, dtype=np.float64)
    target_array = np.asarray(target, dtype=np.float64)
    if reference_array.size == 0 or target_array.size == 0:
        raise ValueError('correlation inputs must be non-empty')
    if not (np.all(np.isfinite(reference_array)) and np.all(np.isfinite(target_array))):
        raise ValueError('correlation inputs must be finite')

    max_lag = min(int(max_lag_s * sample_rate_hz), max(reference_array.size, target_array.size) - 1)
    correlation = np.correlate(target_array, reference_array, mode='full')
    lags = np.arange(-reference_array.size + 1, target_array.size)
    window = np.abs(lags) <= max_lag
    if not np.any(window):
        raise ValueError('correlation window is empty')
    windowed_correlation = correlation[window]
    windowed_lags = lags[window]
    peak_position = int(np.argmax(np.abs(windowed_correlation)))

    lag = float(windowed_lags[peak_position])
    # Parabolic sub-sample refinement.
    if 0 < peak_position < windowed_correlation.size - 1:
        left = float(np.abs(windowed_correlation[peak_position - 1]))
        center = float(np.abs(windowed_correlation[peak_position]))
        right = float(np.abs(windowed_correlation[peak_position + 1]))
        denominator = left - 2.0 * center + right
        if abs(denominator) > 1e-30:
            lag += 0.5 * (left - right) / denominator

    norm = float(np.linalg.norm(reference_array) * np.linalg.norm(target_array))
    peak_norm = (
        float(np.abs(windowed_correlation[peak_position])) / norm if norm > 0 else 0.0
    )
    delay_s = lag / sample_rate_hz
    uncertainty_s = 0.5 / sample_rate_hz
    return delay_s, peak_norm, uncertainty_s


def _detect_ir_peaks(
    amplitudes: np.ndarray,
    sample_rate_hz: float,
    *,
    start_index: int,
    end_index: int,
    min_gap_samples: int,
    prominence_fraction: float = 0.1,
) -> tuple[int, ...]:
    """Local maxima of |IR| inside a window, greedy by descending height."""

    magnitudes = np.abs(amplitudes[start_index:end_index])
    if magnitudes.size < 3:
        return ()
    global_peak = float(np.abs(amplitudes).max())
    threshold = prominence_fraction * global_peak
    candidates = [
        index
        for index in range(1, magnitudes.size - 1)
        if magnitudes[index] >= magnitudes[index - 1]
        and magnitudes[index] >= magnitudes[index + 1]
        and magnitudes[index] >= threshold
    ]
    candidates.sort(key=lambda index: magnitudes[index], reverse=True)
    selected: list[int] = []
    for index in candidates:
        absolute_index = start_index + index
        if all(abs(absolute_index - kept) > min_gap_samples for kept in selected):
            selected.append(absolute_index)
    selected.sort()
    return tuple(selected)


def _wrap_phase_deg(value_deg: float) -> float:
    wrapped = math.fmod(value_deg + 180.0, 360.0)
    if wrapped < 0:
        wrapped += 360.0
    return wrapped - 180.0


def compute_residual_report(
    registration: PredictionMeasurementRegistration,
    *,
    predicted_response: FrequencyResponse | None,
    measured_response: FrequencyResponse | None,
    measured_phase_deg: tuple[float, ...] | None = None,
    predicted_phase_deg: tuple[float, ...] | None = None,
    measured_ir: tuple[tuple[float, ...] | np.ndarray, float, float] | None = None,
    predicted_direct_delay_s: float | None = None,
    predicted_reflection_delays_s: tuple[tuple[float, str | None], ...] = (),
    predicted_mode_frequencies_hz: tuple[float, ...] = (),
    spec_overrides: dict[str, Any] | None = None,
    created_at_utc: str,
) -> PredictionMeasurementResidualReport:
    """Compute per-observable residuals under the registration's gate.

    Refuses (raises) when the registration is not comparable — an invalid
    comparison must not produce a polished residual result. Every entry in
    the report binds the exact registration record and the sealed
    computation spec.
    """

    comparability = registration.comparability
    if comparability.state not in ('comparable', 'comparable_with_limitations'):
        raise ValueError(
            f'residuals refused: pair is {comparability.state} '
            f'({", ".join(comparability.reasons + comparability.limitations)})'
        )

    spec_kwargs: dict[str, Any] = dict(spec_overrides or {})
    if measured_response is not None:
        payload = {
            'frequency_hz': list(measured_response.frequency_hz),
            'level_db': list(measured_response.level_db),
        }
        spec_kwargs.setdefault(
            'measured_response_sha256', canonical_sha256(payload)
        )
    if predicted_response is not None:
        payload = {
            'frequency_hz': list(predicted_response.frequency_hz),
            'level_db': list(predicted_response.level_db),
        }
        spec_kwargs.setdefault(
            'predicted_response_sha256', canonical_sha256(payload)
        )
    spec_kwargs.setdefault('ir_dataset_sha256', registration.measurement.ir_dataset_sha256)
    spec_kwargs.setdefault(
        'level_offset_applied_db', registration.level.level_offset_db
    )
    spec = build_residual_computation_spec(**spec_kwargs)

    observables: list[ResidualObservable] = []

    level_offset_db = registration.level.level_offset_db or 0.0
    shifted_measured: FrequencyResponse | None = None
    if measured_response is not None:
        shifted_measured = FrequencyResponse(
            frequency_hz=measured_response.frequency_hz,
            level_db=tuple(level + level_offset_db for level in measured_response.level_db),
        )

    # --- magnitude -----------------------------------------------------
    magnitude_bands: list[BandMagnitudeResidual] = []
    if not comparability.magnitude_supported:
        observables.append(
            ResidualObservable(
                observable='magnitude_db',
                state='unsupported',
                reason='magnitude_not_supported_by_gate',
            )
        )
    elif predicted_response is None or shifted_measured is None:
        observables.append(
            ResidualObservable(
                observable='magnitude_db',
                state='insufficient_data',
                reason='response_missing',
            )
        )
    else:
        band = comparability.comparable_band_hz
        assert band is not None
        for low_hz, high_hz in spec.bands_hz:
            band_low = max(low_hz, band[0])
            band_high = min(high_hz, band[1])
            if band_high <= band_low:
                continue
            try:
                result: ComparisonResult = compare_frequency_responses(
                    predicted_response,
                    shifted_measured,
                    band_low,
                    band_high,
                    reference_band_hz=spec.reference_band_hz,
                )
            except Exception:
                continue
            magnitude_bands.append(
                BandMagnitudeResidual(
                    band_hz=(band_low, band_high),
                    valid_points=result.valid_points,
                    mean_difference_db=result.mean_difference_db,
                    rms_difference_db=result.rms_difference_db,
                    level_offset_db=result.level_offset_db,
                    shape_rms_db=result.shape_rms_db,
                )
            )
        observables.append(
            ResidualObservable(
                observable='magnitude_db',
                state='computed' if magnitude_bands else 'insufficient_data',
                reason=None if magnitude_bands else 'no_band_overlap',
                magnitude_bands=tuple(magnitude_bands),
            )
        )

    # --- phase ---------------------------------------------------------
    if not comparability.phase_supported:
        observables.append(
            ResidualObservable(
                observable='phase_deg',
                state='unsupported',
                reason='phase_not_supported_by_gate',
            )
        )
    elif (
        measured_phase_deg is None
        or predicted_phase_deg is None
        or predicted_response is None
        or shifted_measured is None
    ):
        observables.append(
            ResidualObservable(
                observable='phase_deg',
                state='insufficient_data',
                reason='phase_response_missing',
            )
        )
    else:
        measured_phase_array = np.asarray(measured_phase_deg, dtype=np.float64)
        measured_freqs = np.asarray(measured_response.frequency_hz, dtype=np.float64)
        predicted_freqs = np.asarray(predicted_response.frequency_hz, dtype=np.float64)
        predicted_phase_array = np.asarray(predicted_phase_deg, dtype=np.float64)
        if (
            measured_phase_array.shape != measured_freqs.shape
            or predicted_phase_array.shape != predicted_freqs.shape
            or measured_phase_array.size == 0
            or predicted_phase_array.size == 0
        ):
            observables.append(
                ResidualObservable(
                    observable='phase_deg',
                    state='insufficient_data',
                    reason='phase_array_inconsistent',
                )
            )
        else:
            band = comparability.comparable_band_hz
            assert band is not None
            phase_bands: list[BandPhaseResidual] = []
            for low_hz, high_hz in spec.bands_hz:
                band_low = max(low_hz, band[0])
                band_high = min(high_hz, band[1])
                if band_high <= band_low:
                    continue
                selection = (measured_freqs >= band_low) & (measured_freqs <= band_high)
                selected_freqs = measured_freqs[selection]
                if selected_freqs.size == 0:
                    continue
                predicted_on_grid = np.interp(
                    np.log2(selected_freqs),
                    np.log2(predicted_freqs),
                    predicted_phase_array,
                )
                differences = np.array(
                    [
                        _wrap_phase_deg(float(predicted) - float(measured))
                        for predicted, measured in zip(
                            predicted_on_grid, measured_phase_array[selection]
                        )
                    ]
                )
                absolute = np.abs(differences)
                phase_bands.append(
                    BandPhaseResidual(
                        band_hz=(band_low, band_high),
                        valid_points=int(selected_freqs.size),
                        mean_abs_difference_deg=float(absolute.mean()),
                        rms_difference_deg=float(np.sqrt((differences**2).mean())),
                        max_abs_difference_deg=float(absolute.max()),
                    )
                )
            observables.append(
                ResidualObservable(
                    observable='phase_deg',
                    state='computed' if phase_bands else 'insufficient_data',
                    reason=None if phase_bands else 'no_band_overlap',
                    phase_bands=tuple(phase_bands),
                )
            )

    # --- direct arrival -------------------------------------------------
    if not comparability.arrival_time_supported:
        observables.append(
            ResidualObservable(
                observable='direct_arrival_time',
                state='unsupported',
                reason='arrival_time_not_supported_by_gate',
            )
        )
    elif measured_ir is None:
        observables.append(
            ResidualObservable(
                observable='direct_arrival_time',
                state='insufficient_data',
                reason='ir_dataset_missing',
            )
        )
    else:
        ir_amplitudes, ir_sample_rate, ir_start = measured_ir
        onset_s, _index, _floor, _peak = detect_ir_onset(
            ir_amplitudes,
            ir_sample_rate,
            ir_start,
            onset_fraction=spec.onset_fraction,
            noise_sigma=spec.onset_noise_sigma,
            noise_fraction=spec.onset_noise_fraction,
        )
        applied_offset = registration.timing.applied_offset_s or 0.0
        measured_onset_s = onset_s + applied_offset
        sound_speed = (
            registration.environment.prediction_sound_speed_m_s
            or registration.environment.sound_speed_m_s
        )
        error_s = (
            measured_onset_s - predicted_direct_delay_s
            if predicted_direct_delay_s is not None
            else None
        )
        error_m = error_s * sound_speed if error_s is not None and sound_speed is not None else None
        observables.append(
            ResidualObservable(
                observable='direct_arrival_time',
                state='computed',
                direct_arrival=DirectArrivalResidual(
                    measured_onset_s=measured_onset_s,
                    predicted_delay_s=predicted_direct_delay_s,
                    error_s=error_s,
                    error_m=error_m,
                    sound_speed_m_s=sound_speed,
                    onset_method=spec.onset_method,
                ),
            )
        )

    # --- early reflections ---------------------------------------------
    if not comparability.arrival_time_supported:
        observables.append(
            ResidualObservable(
                observable='early_reflection_time',
                state='unsupported',
                reason='arrival_time_not_supported_by_gate',
            )
        )
    elif measured_ir is None or not predicted_reflection_delays_s:
        observables.append(
            ResidualObservable(
                observable='early_reflection_time',
                state='insufficient_data',
                reason='ir_or_reflection_data_missing',
            )
        )
    else:
        ir_amplitudes, ir_sample_rate, ir_start = measured_ir
        samples = np.asarray(ir_amplitudes, dtype=np.float64)
        onset_s, onset_index, _floor, _peak = detect_ir_onset(
            samples,
            ir_sample_rate,
            ir_start,
            onset_fraction=spec.onset_fraction,
            noise_sigma=spec.onset_noise_sigma,
            noise_fraction=spec.onset_noise_fraction,
        )
        window_start = onset_index + max(
            1, int(spec.early_reflection_min_gap_s * ir_sample_rate)
        )
        window_end = min(
            samples.size,
            window_start + int(spec.early_reflection_window_s * ir_sample_rate),
        )
        peaks = _detect_ir_peaks(
            samples,
            ir_sample_rate,
            start_index=window_start,
            end_index=window_end,
            min_gap_samples=max(1, int(spec.early_reflection_min_gap_s * ir_sample_rate)),
        )
        peak_times = tuple(ir_start + index / ir_sample_rate for index in peaks)
        applied_offset = registration.timing.applied_offset_s or 0.0
        used_peaks: set[int] = set()
        events: list[ReflectionMatchResidual] = []
        unmatched = 0
        for predicted_delay_s, label in predicted_reflection_delays_s:
            candidates = [
                (index, peak_time)
                for index, peak_time in enumerate(peak_times)
                if index not in used_peaks
                and abs((peak_time + applied_offset) - predicted_delay_s)
                <= spec.reflection_match_tolerance_s
            ]
            if candidates:
                index, peak_time = min(
                    candidates, key=lambda item: abs(item[1] + applied_offset - predicted_delay_s)
                )
                used_peaks.add(index)
                error = (peak_time + applied_offset) - predicted_delay_s
                events.append(
                    ReflectionMatchResidual(
                        predicted_delay_s=predicted_delay_s,
                        measured_delay_s=peak_time + applied_offset,
                        error_s=error,
                        matched=True,
                        label=label,
                    )
                )
            else:
                unmatched += 1
                events.append(
                    ReflectionMatchResidual(
                        predicted_delay_s=predicted_delay_s,
                        measured_delay_s=None,
                        error_s=None,
                        matched=False,
                        label=label,
                    )
                )
        observables.append(
            ResidualObservable(
                observable='early_reflection_time',
                state='computed',
                early_reflections=EarlyReflectionResidual(
                    events=tuple(events),
                    unmatched_predicted=unmatched,
                    measured_peak_count=len(peak_times),
                ),
            )
        )

    # --- modal peaks ----------------------------------------------------
    if (
        predicted_response is None
        or shifted_measured is None
        or not predicted_mode_frequencies_hz
        or comparability.comparable_band_hz is None
    ):
        observables.append(
            ResidualObservable(
                observable='modal_peak_frequency',
                state='insufficient_data'
                if comparability.magnitude_supported
                else 'unsupported',
                reason=(
                    'modal_evidence_missing'
                    if comparability.magnitude_supported
                    else 'magnitude_not_supported_by_gate'
                ),
            )
        )
    else:
        modal_band_low = max(spec.modal_band_hz[0], comparability.comparable_band_hz[0])
        modal_band_high = min(spec.modal_band_hz[1], comparability.comparable_band_hz[1])
        measured_freqs = np.asarray(shifted_measured.frequency_hz, dtype=np.float64)
        measured_levels = np.asarray(shifted_measured.level_db, dtype=np.float64)
        selection = (measured_freqs >= modal_band_low) & (measured_freqs <= modal_band_high)
        selected_freqs = measured_freqs[selection]
        selected_levels = measured_levels[selection]
        measured_peaks: list[float] = []
        if selected_freqs.size >= 3:
            smoothed = np.convolve(selected_levels, np.ones(3) / 3.0, mode='same')
            for index in range(1, smoothed.size - 1):
                if not (
                    smoothed[index] >= smoothed[index - 1]
                    and smoothed[index] >= smoothed[index + 1]
                ):
                    continue
                # Coarse prominence: peak height above the local minima in a
                # +-6-sample window must reach half the declared prominence.
                window_low = max(0, index - 6)
                window_high = min(selected_levels.size, index + 7)
                local_floor = min(
                    float(smoothed[window_low : index + 1].min()),
                    float(smoothed[index:window_high].min()),
                )
                if smoothed[index] - local_floor >= spec.modal_prominence_db * 0.5:
                    measured_peaks.append(float(selected_freqs[index]))

        pairs: list[ModalPeakPair] = []
        unmatched_modes: list[float] = []
        unmatched_peaks = list(measured_peaks)
        for mode_hz in predicted_mode_frequencies_hz:
            candidates = [
                peak for peak in unmatched_peaks
                if abs(peak - mode_hz) <= spec.modal_match_tolerance_hz
            ]
            if candidates:
                peak = min(candidates, key=lambda value: abs(value - mode_hz))
                unmatched_peaks.remove(peak)
                pairs.append(
                    ModalPeakPair(
                        measured_peak_hz=peak,
                        predicted_mode_hz=mode_hz,
                        error_hz=mode_hz - peak,
                    )
                )
            else:
                unmatched_modes.append(mode_hz)
        observables.append(
            ResidualObservable(
                observable='modal_peak_frequency',
                state='computed',
                modal_peaks=ModalPeakResidual(
                    pairs=tuple(pairs),
                    unmatched_predicted_modes=tuple(unmatched_modes),
                    unmatched_measured_peaks=tuple(unmatched_peaks),
                ),
            )
        )

    # --- decay ----------------------------------------------------------
    observables.append(
        ResidualObservable(
            observable='decay_time',
            state='unsupported',
            reason='prediction_has_no_decay_observable',
        )
    )

    return _seal_residual_report(
        registration=registration,
        comparability=comparability,
        spec=spec,
        observables=tuple(observables),
        created_at_utc=created_at_utc,
    )


def _seal_residual_report(
    *,
    registration: PredictionMeasurementRegistration,
    comparability: ComparabilityVerdict,
    spec: ResidualComputationSpec,
    observables: tuple[ResidualObservable, ...],
    created_at_utc: str,
) -> PredictionMeasurementResidualReport:
    provisional = PredictionMeasurementResidualReport.model_construct(
        report_id='pending',
        registration_id=registration.registration_id,
        registration_sha256=registration.semantic_sha256,
        document_id=registration.document_id,
        comparability_state=comparability.state,
        partition=registration.partition,
        spec=spec,
        observables=observables,
        created_at_utc=created_at_utc,
        semantic_sha256='0' * 64,
    )
    payload = provisional.model_dump(mode='json', exclude={'report_id', 'semantic_sha256'})
    sha256 = canonical_sha256(payload)
    return PredictionMeasurementResidualReport(
        report_id=f'pm-residual:{sha256}',
        registration_id=registration.registration_id,
        registration_sha256=registration.semantic_sha256,
        document_id=registration.document_id,
        comparability_state=comparability.state,
        partition=registration.partition,
        spec=spec,
        observables=observables,
        created_at_utc=created_at_utc,
        semantic_sha256=sha256,
    )


def aggregate_magnitude_statistics(
    reports: tuple[PredictionMeasurementResidualReport, ...] | list[PredictionMeasurementResidualReport],
) -> tuple[dict[str, Any], ...]:
    """Seat-to-seat aggregation across residual reports (#564 section 7).

    Returns per-band statistics over the magnitude observables: band, count,
    mean/std of the per-band RMS. Reports that did not compute a magnitude
    residual are counted honestly, never silently dropped.
    """

    per_band: dict[tuple[float, float], list[float]] = {}
    for report in reports:
        magnitude = next(
            (
                observable
                for observable in report.observables
                if observable.observable == 'magnitude_db'
            ),
            None,
        )
        if magnitude is None or magnitude.state != 'computed':
            continue
        for band in magnitude.magnitude_bands:
            if band.rms_difference_db is not None:
                per_band.setdefault(band.band_hz, []).append(band.rms_difference_db)
    result: list[dict[str, Any]] = []
    for band_hz, values in sorted(per_band.items()):
        array = np.asarray(values, dtype=np.float64)
        result.append(
            {
                'band_hz': band_hz,
                'count': int(array.size),
                'mean_rms_db': float(array.mean()),
                'std_rms_db': float(array.std(ddof=0)),
            }
        )
    return tuple(result)
