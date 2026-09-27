"""Immutable acquisition-side authority records for the measurement domain.

The persisted ``CadMeasurementRecord``/``CadFrequencyResponseDataset`` pair
proves *what* was measured. The authorities here prove the surrounding
conditions a later consumer must resolve verbatim instead of trusting a
label: the exact timing reference a measurement used (#642), the acoustic
level calibration that authorizes an absolute-SPL reading (#643), the
verified output-channel-to-physical-speaker map (#473) and the independent
speaker wiring commissioning checks (#645).

Every model is frozen pydantic sealed by a SHA-256 of its canonical
``identity_payload`` — the same sealing convention as
``CadAcquisitionContext`` — and is persisted through
``CadMeasurementQualityRepository``. The repository re-validates the exact
bindings on save and on every read; a record that no longer resolves fails
closed rather than silently downgrading to 'unknown'.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash
from .clock import utc_now_iso as _utc_now


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_unique_non_empty(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)) or any(not item for item in values):
        raise ValueError(f'{label} must be unique non-empty values')


# ---------------------------------------------------------------------------
# Measurement timing-reference authority (#642)


TimingReferenceMethod = Literal[
    'acoustic_reference',
    'loopback',
    'shared_clock',
    'external_sync',
    'imported',
    'manual',
    'unknown',
]

TimingT0Convention = Literal[
    'acoustic_reference_signal',
    'loopback_edge',
    'sweep_start',
    'imported',
    'manual',
    'unknown',
]

TimingCorrectionKind = Literal[
    'loopback_path',
    'output_buffer',
    'input_buffer',
    'driver_latency',
    'acoustic_distance',
    'imported',
    'manual',
    'unknown',
]

TimingReferenceScope = Literal['measurement', 'session', 'persistent', 'unknown']

TimingCorrectionSignConvention = Literal[
    'subtract_from_arrival',
    'add_to_t0',
    'producer_shifted',
    'unknown',
]

TimingCorrectionApplicationState = Literal[
    'metadata_only',
    'applied_to_dataset',
    'producer_applied',
    'unknown',
]


class CadTimingDelayCorrection(BaseModel):
    """One typed delay correction applied to the timing reference.

    ``sign_convention`` says what a positive ``value_s`` means — subtract
    from the observed arrival, add to the time origin, or already shifted
    by the producer — and ``application_state`` says whether the value is
    still pending consumer application (``metadata_only``) or already
    reflected in the persisted IR/time coordinates
    (``applied_to_dataset``/``producer_applied``). Both fields are optional
    for legacy records; a missing or ``unknown`` state is ambiguous and
    blocks strong arrival-time claims rather than risking a double
    application (#860).
    """

    model_config = ConfigDict(frozen=True)

    correction_kind: TimingCorrectionKind
    value_s: float
    provenance: str | None = None
    sign_convention: TimingCorrectionSignConvention | None = None
    application_state: TimingCorrectionApplicationState | None = None

    @model_validator(mode='after')
    def valid_correction(self) -> 'CadTimingDelayCorrection':
        if not isfinite(float(self.value_s)):
            raise ValueError('delay correction value must be finite')
        return self


class CadMeasurementTimingReference(BaseModel):
    """Immutable timing-reference authority for IR arrival/alignment claims.

    ``method`` is the semantic the reference actually carries: an acoustic
    reference signal, a loopback channel, a shared hardware clock, external
    sync, an imported capture whose timing was fixed outside HTDT, a manual
    declaration, or unknown. ``imported``/``manual``/``unknown`` methods are
    recorded honestly but never authorize common-timing claims — a manual
    entry is not a verified timing source, and two measurements sharing a
    sample rate or a label do not prove a shared clock.

    ``validity_scope`` is backed by explicit scope identity (#849/#860),
    not just a label: ``measurement`` names exactly one subject
    measurement; ``session`` names one acquisition-session id plus the
    exact member measurements; ``persistent`` pins the signal-path
    fingerprint whose unchanged identity makes reuse valid; ``unknown``
    carries no scope identity and never authorizes common timing. The
    optional scope fields join the sealed identity only when present, so
    references persisted before they existed keep their hash.
    """

    model_config = ConfigDict(frozen=True)

    timing_reference_id: str = Field(min_length=1)
    version: str = Field(min_length=1, default='1')
    method: TimingReferenceMethod
    reference_channel: str | None = None
    input_clock_identity: str | None = None
    output_clock_identity: str | None = None
    sample_rate_hz: int | None = Field(default=None, gt=0)
    t0_convention: TimingT0Convention = 'unknown'
    delay_corrections: tuple[CadTimingDelayCorrection, ...] = ()
    validity_scope: TimingReferenceScope = 'unknown'
    subject_measurement_ids: tuple[str, ...] = ()
    acquisition_session_id: str | None = None
    signal_path_identity: str | None = None
    provenance_json: str = '{}'
    created_at_utc: str = Field(min_length=1)
    timing_reference_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_reference(self) -> 'CadMeasurementTimingReference':
        _require_iso8601(self.created_at_utc, 'timing reference created_at_utc')
        if self.subject_measurement_ids:
            _require_unique_non_empty(
                self.subject_measurement_ids, 'timing scope subject ids'
            )
        if self.method == 'shared_clock' and not (
            self.input_clock_identity and self.output_clock_identity
        ):
            raise ValueError(
                'shared-clock timing requires both clock identities'
            )
        if self.timing_reference_sha256 != _hash(self.identity_payload()):
            raise ValueError('timing reference hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'timing_reference_id': self.timing_reference_id,
            'version': self.version,
            'method': self.method,
            'reference_channel': self.reference_channel,
            'input_clock_identity': self.input_clock_identity,
            'output_clock_identity': self.output_clock_identity,
            'sample_rate_hz': self.sample_rate_hz,
            't0_convention': self.t0_convention,
            'delay_corrections': [
                _correction_identity_payload(correction)
                for correction in self.delay_corrections
            ],
            'validity_scope': self.validity_scope,
            'provenance_json': self.provenance_json,
            'created_at_utc': self.created_at_utc,
        }
        # Optional post-#849/#860 scope identity joins the seal only when
        # present — references persisted without it keep their hash.
        if self.subject_measurement_ids:
            payload['subject_measurement_ids'] = list(
                self.subject_measurement_ids
            )
        if self.acquisition_session_id is not None:
            payload['acquisition_session_id'] = self.acquisition_session_id
        if self.signal_path_identity is not None:
            payload['signal_path_identity'] = self.signal_path_identity
        return payload


def _correction_identity_payload(
    correction: CadTimingDelayCorrection,
) -> dict[str, Any]:
    """Canonical correction payload for the reference seal.

    ``sign_convention``/``application_state`` join only when set — a
    correction persisted before they existed keeps its exact 3-key payload.
    """
    payload = correction.model_dump(mode='json')
    if correction.sign_convention is None:
        payload.pop('sign_convention')
    if correction.application_state is None:
        payload.pop('application_state')
    return payload


#: t=0 conventions only a machine capture can carry: a manual/imported/unknown
#: convention is a label, not a timing edge the recording itself contains.
_MACHINE_T0_CONVENTIONS = frozenset(
    {'acoustic_reference_signal', 'loopback_edge', 'sweep_start'}
)


def timing_reference_supports_common_timing(
    reference: CadMeasurementTimingReference,
) -> bool:
    """Whether this authority can carry a common-timing claim at all.

    Only genuinely synchronized methods qualify, and each must carry the
    method evidence that makes the synchronization real (#826):

    - ``acoustic_reference``/``loopback``: a declared ``reference_channel``
      (the capture carrying the timing edge) plus a machine t=0 convention —
      the label alone says nothing about *where* the edge lives.
    - ``shared_clock``: both clock identities present and *equal* — a shared
      clock that does not name the same clock on both ends is not shared.
    - ``external_sync``: a declared sync identity or reference channel plus a
      machine t=0 convention.

    ``imported``, ``manual`` and ``unknown`` references are honest records of
    what was captured but can never authorize common timing, no matter how
    completely populated their fields are — a manual entry is not a verified
    timing source.
    """

    method = reference.method
    if method == 'shared_clock':
        return timing_clocks_shared(reference)
    if method in {'acoustic_reference', 'loopback'}:
        return (
            reference.reference_channel is not None
            and reference.t0_convention in _MACHINE_T0_CONVENTIONS
        )
    if method == 'external_sync':
        return (
            any(
                identity is not None
                for identity in (
                    reference.input_clock_identity,
                    reference.output_clock_identity,
                    reference.reference_channel,
                )
            )
            and reference.t0_convention in _MACHINE_T0_CONVENTIONS
        )
    return False


def timing_clocks_shared(reference: CadMeasurementTimingReference) -> bool:
    """True only for a declared shared hardware clock on both ends.

    Matching sample rates or matching clock labels are not proof — this is
    exactly the 'loopback/shared clock' distinction the authority exists
    for.
    """

    return (
        reference.method == 'shared_clock'
        and reference.input_clock_identity is not None
        and reference.input_clock_identity == reference.output_clock_identity
    )


def correction_application_state(
    correction: CadTimingDelayCorrection,
) -> TimingCorrectionApplicationState:
    """Effective applied-state; an absent state is ambiguous ``unknown``."""

    return (
        correction.application_state
        if correction.application_state is not None
        else 'unknown'
    )


def correction_sign_convention(
    correction: CadTimingDelayCorrection,
) -> TimingCorrectionSignConvention:
    """Effective sign convention; an absent convention reads ``unknown``."""

    return (
        correction.sign_convention
        if correction.sign_convention is not None
        else 'unknown'
    )


def correction_requires_application(
    correction: CadTimingDelayCorrection,
) -> bool:
    """True only when the consumer must still apply the correction itself."""

    return correction_application_state(correction) == 'metadata_only'


def correction_already_applied(
    correction: CadTimingDelayCorrection,
) -> bool:
    """True when the persisted IR/time coordinates already reflect the value."""

    return correction_application_state(correction) in (
        'applied_to_dataset',
        'producer_applied',
    )


def timing_corrections_unambiguous(
    reference: CadMeasurementTimingReference,
) -> bool:
    """Every correction carries an explicit sign and applied-state.

    A strong arrival-time claim requires this — an absent or ``unknown``
    state leaves it unclear whether the value is already reflected in the
    data, so the claim must stay conservative rather than risk applying
    the same correction twice (#860).
    """

    return all(
        correction_application_state(correction) != 'unknown'
        and correction_sign_convention(correction) != 'unknown'
        for correction in reference.delay_corrections
    )


def timing_reference_scope_is_applicable(
    reference: CadMeasurementTimingReference,
    *,
    subject_measurement_ids: Sequence[str],
    acquisition_session_id: str | None = None,
    signal_path_identity: str | None = None,
    sample_rate_hz: int | None = None,
) -> bool:
    """Whether declared context subjects fall inside the reference scope.

    ``measurement`` covers exactly its one named measurement; ``session``
    covers members of its exact member manifest (a declared context
    session id must also match when both sides record one); ``persistent``
    requires the same signal-path fingerprint plus any declared
    sample-rate constraint; ``unknown`` never applies.
    """

    subjects = frozenset(subject_measurement_ids)
    if not subjects:
        return False
    scope = reference.validity_scope
    if scope == 'measurement':
        return subjects <= frozenset(reference.subject_measurement_ids)
    if scope == 'session':
        if not subjects <= frozenset(reference.subject_measurement_ids):
            return False
        return (
            reference.acquisition_session_id is None
            or acquisition_session_id is None
            or acquisition_session_id == reference.acquisition_session_id
        )
    if scope == 'persistent':
        if (
            reference.signal_path_identity is not None
            and signal_path_identity != reference.signal_path_identity
        ):
            return False
        return not (
            reference.sample_rate_hz is not None
            and sample_rate_hz is not None
            and reference.sample_rate_hz != sample_rate_hz
        )
    return False


def timing_reference_authorizes_common_timing(
    reference: CadMeasurementTimingReference,
    *,
    subject_measurement_ids: Sequence[str],
    acquisition_session_id: str | None = None,
    signal_path_identity: str | None = None,
    sample_rate_hz: int | None = None,
) -> bool:
    """Method capability AND proven scope membership — both are required.

    Matching timing-reference hash alone is insufficient for a
    common-timing claim: the reference's declared scope must also cover
    the subjects the context groups (#849).
    """

    return timing_reference_supports_common_timing(
        reference
    ) and timing_reference_scope_is_applicable(
        reference,
        subject_measurement_ids=subject_measurement_ids,
        acquisition_session_id=acquisition_session_id,
        signal_path_identity=signal_path_identity,
        sample_rate_hz=sample_rate_hz,
    )


def _validate_timing_scope_identity(
    reference: CadMeasurementTimingReference,
) -> None:
    """Scope identity a non-``unknown`` timing reference must carry (#849).

    Enforced by builders and the repository at save time; the model itself
    stays permissive so legacy records without scope identity still load
    as honest ``unknown``/unscoped evidence.
    """
    if reference.validity_scope == 'measurement':
        if len(reference.subject_measurement_ids) != 1:
            raise ValueError(
                'measurement-scope timing requires exactly one subject '
                'measurement id'
            )
    elif reference.validity_scope == 'session':
        if not reference.acquisition_session_id:
            raise ValueError(
                'session-scope timing requires an acquisition session id'
            )
        if not reference.subject_measurement_ids:
            raise ValueError(
                'session-scope timing requires subject measurement ids'
            )
    elif reference.validity_scope == 'persistent':
        if not reference.signal_path_identity:
            raise ValueError(
                'persistent-scope timing requires a signal path identity'
            )


def build_timing_reference(
    *,
    method: TimingReferenceMethod,
    timing_reference_id: str | None = None,
    version: str = '1',
    reference_channel: str | None = None,
    input_clock_identity: str | None = None,
    output_clock_identity: str | None = None,
    sample_rate_hz: int | None = None,
    t0_convention: TimingT0Convention = 'unknown',
    delay_corrections: Sequence[CadTimingDelayCorrection | dict[str, Any]] = (),
    validity_scope: TimingReferenceScope = 'unknown',
    subject_measurement_ids: Sequence[str] = (),
    acquisition_session_id: str | None = None,
    signal_path_identity: str | None = None,
    created_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadMeasurementTimingReference:
    """Assemble a sealed timing-reference authority record.

    The record only becomes authoritative once persisted through the quality
    repository; acquisition contexts then bind the exact id+hash.
    """
    corrections = tuple(
        item
        if isinstance(item, CadTimingDelayCorrection)
        else CadTimingDelayCorrection.model_validate(item)
        for item in delay_corrections
    )
    payload: dict[str, Any] = {
        'timing_reference_id': timing_reference_id or str(uuid4()),
        'version': version,
        'method': method,
        'reference_channel': reference_channel,
        'input_clock_identity': input_clock_identity,
        'output_clock_identity': output_clock_identity,
        'sample_rate_hz': sample_rate_hz,
        't0_convention': t0_convention,
        'delay_corrections': corrections,
        'validity_scope': validity_scope,
        'subject_measurement_ids': tuple(subject_measurement_ids),
        'acquisition_session_id': acquisition_session_id,
        'signal_path_identity': signal_path_identity,
        'provenance_json': provenance_json,
        'created_at_utc': created_at_utc or _utc_now(),
    }
    provisional = CadMeasurementTimingReference.model_construct(
        **payload,
        timing_reference_sha256='0' * 64,
    )
    reference = CadMeasurementTimingReference(
        **payload,
        timing_reference_sha256=_hash(provisional.identity_payload()),
    )
    _validate_timing_scope_identity(reference)
    return reference


# ---------------------------------------------------------------------------
# Acoustic level-calibration authority (#643)


LevelCalibrationMethod = Literal[
    'manufacturer_sensitivity',
    'acoustic_calibrator',
    'rew_spl_session',
    'reference_meter_transfer',
    'imported',
    'manual',
    'unknown',
]

LevelCalibrationScope = Literal['measurement', 'session', 'instrument', 'unknown']


class CadAcousticLevelCalibration(BaseModel):
    """Immutable calibration authority for absolute dB-SPL claims.

    This is deliberately distinct from the microphone frequency-response
    correction file: a mic cal file corrects the *shape* of a response and
    never by itself authorizes an absolute SPL reading. Absolute SPL requires
    a calibrator trace, a REW SPL-session level agreement, or a calibrated
    reference-meter transfer — manufacturer sensitivity datasheets,
    imported/manual entries and unknown methods are recorded but do not
    authorize absolute SPL.
    """

    model_config = ConfigDict(frozen=True)

    calibration_id: str = Field(min_length=1)
    version: str = Field(min_length=1, default='1')
    method: LevelCalibrationMethod
    instrument_identity: str | None = None
    instrument_profile: str | None = None
    input_device_label: str | None = None
    input_channel: str | None = None
    sensitivity_v_per_pa: float | None = Field(default=None, gt=0.0)
    reference_level_db_spl: float | None = None
    reference_frequency_hz: float | None = Field(default=None, gt=0.0)
    #: Calibration uncertainty retained separately from the calibrated value
    #: — absence means UNKNOWN, never silently zero uncertainty.
    uncertainty_db: float | None = Field(default=None, ge=0.0)
    calibrated_at_utc: str = Field(min_length=1)
    validity_scope: LevelCalibrationScope = 'unknown'
    # #850/#859: enforceable applicability identity. ``measurement`` scope
    # names exactly one measurement; ``session`` names the acquisition
    # session it was calibrated under; ``instrument`` requires the
    # instrument instance plus the input-path fingerprint (device,
    # channel, gain, mode) the reuse remains valid under. Optional fields
    # join the sealed identity only when present.
    subject_measurement_id: str | None = None
    acquisition_session_id: str | None = None
    input_path_identity: str | None = None
    provenance_json: str = '{}'
    calibration_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_calibration(self) -> 'CadAcousticLevelCalibration':
        _require_iso8601(self.calibrated_at_utc, 'calibration calibrated_at_utc')
        for label, value in (
            ('reference_level_db_spl', self.reference_level_db_spl),
            ('reference_frequency_hz', self.reference_frequency_hz),
            ('sensitivity_v_per_pa', self.sensitivity_v_per_pa),
            ('uncertainty_db', self.uncertainty_db),
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{label} must be finite')
        if self.calibration_sha256 != _hash(self.identity_payload()):
            raise ValueError('acoustic level calibration hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'calibration_id': self.calibration_id,
            'version': self.version,
            'method': self.method,
            'instrument_identity': self.instrument_identity,
            'instrument_profile': self.instrument_profile,
            'input_device_label': self.input_device_label,
            'input_channel': self.input_channel,
            'sensitivity_v_per_pa': self.sensitivity_v_per_pa,
            'reference_level_db_spl': self.reference_level_db_spl,
            'reference_frequency_hz': self.reference_frequency_hz,
            'calibrated_at_utc': self.calibrated_at_utc,
            'validity_scope': self.validity_scope,
            'provenance_json': self.provenance_json,
        }
        # Optional fields join the identity only when present — the additive
        # convention that keeps existing authority hashes stable. The
        # post-#850/#859 applicability fields and ``uncertainty_db`` alike.
        if self.subject_measurement_id is not None:
            payload['subject_measurement_id'] = self.subject_measurement_id
        if self.acquisition_session_id is not None:
            payload['acquisition_session_id'] = self.acquisition_session_id
        if self.input_path_identity is not None:
            payload['input_path_identity'] = self.input_path_identity
        if self.uncertainty_db is not None:
            payload['uncertainty_db'] = self.uncertainty_db
        return payload


_LEVEL_METHODS_WITH_ABSOLUTE_SPL = frozenset(
    {'acoustic_calibrator', 'rew_spl_session', 'reference_meter_transfer'}
)


def absolute_spl_evidence_gaps(
    calibration: CadAcousticLevelCalibration,
) -> tuple[str, ...]:
    """The method-specific evidence missing for an absolute dB-SPL claim.

    A method capable of absolute SPL must carry its minimum evidence set —
    a bare method enum authorizes nothing:

    * ``acoustic_calibrator``: reference level + reference frequency;
      measured input-chain/microphone identity; derived scale/sensitivity
      or the exact producer session it came from; an applicability scope.
    * ``rew_spl_session``: the exact producer session/profile record;
      input path/device/channel context; the calibration result or
      derived scale; an applicability scope.
    * ``reference_meter_transfer``: the exact reference-meter identity;
      the target input chain; the observed transfer/calibration result;
      the transfer method/session record; an applicability scope.

    ``manufacturer_sensitivity``, ``imported``, ``manual`` and ``unknown``
    are never absolute-SPL authorities on their own — they may be stored
    as calibration-related information without authorizing absolute SPL.
    """

    if calibration.method not in _LEVEL_METHODS_WITH_ABSOLUTE_SPL:
        return ('method cannot authorize absolute SPL',)
    gaps: list[str] = []
    has_input_chain = any(
        (
            calibration.instrument_identity,
            calibration.input_device_label,
            calibration.input_channel,
            calibration.input_path_identity,
        )
    )
    has_session_record = (
        bool(calibration.instrument_profile)
        or bool(calibration.acquisition_session_id)
        or calibration.provenance_json not in ('', '{}')
    )
    if calibration.method == 'acoustic_calibrator':
        if calibration.reference_level_db_spl is None:
            gaps.append('calibrator reference level')
        if calibration.reference_frequency_hz is None:
            gaps.append('calibrator reference frequency')
        if not has_input_chain:
            gaps.append('measured input chain/microphone identity')
        if calibration.sensitivity_v_per_pa is None and not has_session_record:
            gaps.append('derived scale or exact producer session')
    elif calibration.method == 'rew_spl_session':
        if not has_session_record:
            gaps.append('exact producer session record')
        if not has_input_chain:
            gaps.append('input path/device/channel context')
        if (
            calibration.sensitivity_v_per_pa is None
            and calibration.reference_level_db_spl is None
        ):
            gaps.append('calibration result/scale semantics')
    elif calibration.method == 'reference_meter_transfer':
        if calibration.instrument_identity is None:
            gaps.append('reference meter/instrument authority')
        if not any(
            (calibration.input_device_label, calibration.input_channel)
        ):
            gaps.append('target input-chain identity')
        if (
            calibration.reference_level_db_spl is None
            and calibration.sensitivity_v_per_pa is None
        ):
            gaps.append('observed transfer/calibration result')
        if not has_session_record:
            gaps.append('transfer method/session record')
    if calibration.validity_scope == 'unknown':
        gaps.append('applicability scope')
    return tuple(gaps)


def calibration_supports_absolute_spl(
    calibration: CadAcousticLevelCalibration,
) -> bool:
    """Whether this calibration can authorize an absolute dB-SPL claim.

    Evaluates the complete authority — method alone never authorizes SPL;
    missing required method evidence leaves the capability unsupported.
    """

    return not absolute_spl_evidence_gaps(calibration)


def calibration_applies_to(
    calibration: CadAcousticLevelCalibration,
    *,
    measurement_id: str,
    acquisition_session_id: str | None = None,
    input_path_identity: str | None = None,
) -> bool:
    """Whether this exact calibration covers the named acquisition (#850).

    ``measurement`` covers exactly its one subject; ``session`` requires
    the same session id — plus the recorded input-path fingerprint when
    the calibration pins one; ``instrument`` requires the same input-path
    fingerprint; ``unknown`` never applies.
    """

    scope = calibration.validity_scope
    if scope == 'measurement':
        return calibration.subject_measurement_id == measurement_id
    if scope == 'session':
        if (
            calibration.acquisition_session_id is None
            or acquisition_session_id != calibration.acquisition_session_id
        ):
            return False
        return (
            calibration.input_path_identity is None
            or input_path_identity == calibration.input_path_identity
        )
    if scope == 'instrument':
        return (
            calibration.input_path_identity is not None
            and input_path_identity == calibration.input_path_identity
        )
    return False


def calibration_authorizes_absolute_spl(
    calibration: CadAcousticLevelCalibration,
    *,
    measurement_id: str,
    acquisition_session_id: str | None = None,
    input_path_identity: str | None = None,
) -> bool:
    """Method capability AND proven applicability — both are required (#850).

    An SPL-authorizing method alone never promotes a dataset to
    ``absolute_spl``; the calibration's declared scope must also cover
    this exact measurement/session/input chain.
    """

    return calibration_supports_absolute_spl(
        calibration
    ) and calibration_applies_to(
        calibration,
        measurement_id=measurement_id,
        acquisition_session_id=acquisition_session_id,
        input_path_identity=input_path_identity,
    )


def _validate_calibration_scope_identity(
    calibration: CadAcousticLevelCalibration,
) -> None:
    """Scope + method evidence a calibration must carry (#850/#859).

    Enforced by builders and the repository at save time; the model itself
    stays permissive so legacy records still load. ``unknown`` scope
    carries no applicability identity and never authorizes absolute SPL.
    """
    if calibration.validity_scope == 'measurement':
        if not calibration.subject_measurement_id:
            raise ValueError(
                'measurement-scope calibration requires a subject '
                'measurement id'
            )
    elif calibration.validity_scope == 'session':
        if not calibration.acquisition_session_id:
            raise ValueError(
                'session-scope calibration requires an acquisition '
                'session id'
            )
    elif calibration.validity_scope == 'instrument':
        if not calibration.instrument_identity:
            raise ValueError(
                'instrument-scope calibration requires an instrument '
                'identity'
            )
        if not calibration.input_path_identity:
            raise ValueError(
                'instrument-scope calibration requires an input path '
                'identity'
            )
    # Method name alone is not evidence: each SPL-authorizing method pins
    # the minimum authority-appropriate fields (#850 §D).
    if calibration.method == 'acoustic_calibrator':
        if (
            calibration.reference_level_db_spl is None
            or calibration.reference_frequency_hz is None
        ):
            raise ValueError(
                'acoustic-calibrator method requires a reference level '
                'and reference frequency'
            )
    elif calibration.method == 'rew_spl_session':
        if not calibration.acquisition_session_id:
            raise ValueError(
                'rew_spl_session method requires an acquisition session id'
            )
    elif calibration.method == 'reference_meter_transfer':
        if not calibration.instrument_identity:
            raise ValueError(
                'reference-meter transfer requires an instrument identity'
            )


def build_acoustic_level_calibration(
    *,
    method: LevelCalibrationMethod,
    calibration_id: str | None = None,
    version: str = '1',
    instrument_identity: str | None = None,
    instrument_profile: str | None = None,
    input_device_label: str | None = None,
    input_channel: str | None = None,
    sensitivity_v_per_pa: float | None = None,
    reference_level_db_spl: float | None = None,
    reference_frequency_hz: float | None = None,
    uncertainty_db: float | None = None,
    calibrated_at_utc: str | None = None,
    validity_scope: LevelCalibrationScope = 'unknown',
    subject_measurement_id: str | None = None,
    acquisition_session_id: str | None = None,
    input_path_identity: str | None = None,
    provenance_json: str = '{}',
) -> CadAcousticLevelCalibration:
    """Assemble a sealed acoustic level-calibration authority record."""
    payload: dict[str, Any] = {
        'calibration_id': calibration_id or str(uuid4()),
        'version': version,
        'method': method,
        'instrument_identity': instrument_identity,
        'instrument_profile': instrument_profile,
        'input_device_label': input_device_label,
        'input_channel': input_channel,
        'sensitivity_v_per_pa': sensitivity_v_per_pa,
        'reference_level_db_spl': reference_level_db_spl,
        'reference_frequency_hz': reference_frequency_hz,
        'uncertainty_db': uncertainty_db,
        'calibrated_at_utc': calibrated_at_utc or _utc_now(),
        'validity_scope': validity_scope,
        'subject_measurement_id': subject_measurement_id,
        'acquisition_session_id': acquisition_session_id,
        'input_path_identity': input_path_identity,
        'provenance_json': provenance_json,
    }
    provisional = CadAcousticLevelCalibration.model_construct(
        **payload,
        calibration_sha256='0' * 64,
    )
    calibration = CadAcousticLevelCalibration(
        **payload,
        calibration_sha256=_hash(provisional.identity_payload()),
    )
    _validate_calibration_scope_identity(calibration)
    return calibration


MeasurementLevelReferenceKind = Literal[
    'absolute_spl',
    'spl_uncalibrated',
    'dbfs',
    'pa',
    'relative',
    'unknown',
]


class CadDatasetLevelReference(BaseModel):
    """Immutable binding between one exact dataset and its level semantics.

    The reference says what the persisted ``level_db`` values actually mean
    and — only for ``absolute_spl`` — pins the exact
    ``CadAcousticLevelCalibration`` authority that authorizes the reading.
    The repository refuses an ``absolute_spl`` row whose bound calibration
    does not support absolute SPL, so a persisted row can never claim more
    level semantics than its evidence.
    """

    model_config = ConfigDict(frozen=True)

    level_reference_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=_SHA256_PATTERN)
    level_reference_kind: MeasurementLevelReferenceKind
    calibration_id: str | None = None
    calibration_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    created_at_utc: str = Field(min_length=1)
    level_reference_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_level_reference(self) -> 'CadDatasetLevelReference':
        _require_iso8601(self.created_at_utc, 'level reference created_at_utc')
        if self.level_reference_kind == 'absolute_spl':
            if self.calibration_id is None or self.calibration_sha256 is None:
                raise ValueError(
                    'absolute SPL requires a bound acoustic level calibration'
                )
        elif self.calibration_id is not None or self.calibration_sha256 is not None:
            raise ValueError(
                'non-absolute level references must not bind a calibration'
            )
        if self.level_reference_sha256 != _hash(self.identity_payload()):
            raise ValueError('dataset level reference hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'level_reference_id': self.level_reference_id,
            'measurement_id': self.measurement_id,
            'dataset_id': self.dataset_id,
            'dataset_sha256': self.dataset_sha256,
            'level_reference_kind': self.level_reference_kind,
            'calibration_id': self.calibration_id,
            'calibration_sha256': self.calibration_sha256,
            'created_at_utc': self.created_at_utc,
        }


def build_dataset_level_reference(
    *,
    measurement_id: str,
    dataset_id: str,
    dataset_sha256: str,
    level_reference_kind: MeasurementLevelReferenceKind,
    calibration_id: str | None = None,
    calibration_sha256: str | None = None,
    level_reference_id: str | None = None,
    created_at_utc: str | None = None,
) -> CadDatasetLevelReference:
    """Assemble a sealed dataset level-reference binding."""
    payload: dict[str, Any] = {
        'level_reference_id': level_reference_id or str(uuid4()),
        'measurement_id': measurement_id,
        'dataset_id': dataset_id,
        'dataset_sha256': dataset_sha256,
        'level_reference_kind': level_reference_kind,
        'calibration_id': calibration_id,
        'calibration_sha256': calibration_sha256,
        'created_at_utc': created_at_utc or _utc_now(),
    }
    provisional = CadDatasetLevelReference.model_construct(
        **payload,
        level_reference_sha256='0' * 64,
    )
    return CadDatasetLevelReference(
        **payload,
        level_reference_sha256=_hash(provisional.identity_payload()),
    )


# ---------------------------------------------------------------------------
# Verified channel-map / routing-profile authority (#473)


RoutingVerification = Literal['verified', 'unverified', 'mixed', 'unavailable']


class CadChannelMapEntry(BaseModel):
    """One verified output-path to physical-radiator binding.

    Captures the exact path seen while a test signal was playing: the
    output device label as the OS/REW named it, the REW channel label and
    hardware channel index, the logical role HTDT assigned, which physical
    speakers were expected vs actually observed, and the frequency band the
    observation covered — a bass-managed low band can legitimately land on
    a different radiator than the main band. ``avr_configuration_id``
    binds the AVR condition the map was verified under so later topology
    changes are detectable instead of silently reused.
    """

    model_config = ConfigDict(frozen=True)

    output_device_label: str = Field(min_length=1)
    rew_channel_label: str = Field(min_length=1)
    hardware_channel_index: int | None = Field(default=None, ge=0)
    logical_role: str = 'unknown'
    expected_speaker_ids: tuple[str, ...] = ()
    observed_speaker_ids: tuple[str, ...] = ()
    verification: RoutingVerification = 'unverified'
    verified_at_utc: str | None = None
    avr_configuration_id: str | None = None
    verified_band_hz: tuple[float, float] | None = None

    @field_validator('expected_speaker_ids', 'observed_speaker_ids')
    @classmethod
    def unique_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _require_unique_non_empty(value, 'speaker id lists')
        return value

    @model_validator(mode='after')
    def valid_entry(self) -> 'CadChannelMapEntry':
        if self.verified_at_utc is not None:
            _require_iso8601(self.verified_at_utc, 'channel map verified_at_utc')
        if self.verified_band_hz is not None:
            low, high = self.verified_band_hz
            if not (isfinite(low) and isfinite(high)) or low <= 0 or high <= low:
                raise ValueError('verified band is invalid')
        return self


class CadRoutingProfile(BaseModel):
    """Immutable verified channel map: output path to physical speakers.

    A saved profile is the authority a measurement assignment resolves
    against; it deliberately excludes electrical-commissioning truth
    (continuity, terminal polarity, acoustic polarity, load) — those stay
    with ``CadWiringVerificationCheck`` (#645) which may reference a profile
    but is never implied by it.
    """

    model_config = ConfigDict(frozen=True)

    routing_profile_id: str = Field(min_length=1)
    profile_name: str = Field(min_length=1, default='routing')
    # #848/#858: explicit project/topology scope. The speaker ids inside
    # entries only have meaning inside one SceneDocument; ``document_id``
    # binds the project and the optional ``scene_revision_id`` pins the
    # exact immutable revision the map was verified under. Both join the
    # sealed identity only when present — legacy unscoped profiles keep
    # their hash; the repository refuses to persist a new profile without
    # them.
    document_id: str | None = Field(default=None, min_length=1)
    scene_revision_id: str | None = Field(default=None, min_length=1)
    entries: tuple[CadChannelMapEntry, ...] = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    routing_profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_profile(self) -> 'CadRoutingProfile':
        seen = set()
        for entry in self.entries:
            key = (
                entry.output_device_label,
                entry.rew_channel_label,
                entry.hardware_channel_index,
                entry.verified_band_hz,
            )
            if key in seen:
                raise ValueError(
                    'duplicate channel map entry for the same output path/band'
                )
            seen.add(key)
        _require_iso8601(self.created_at_utc, 'routing profile created_at_utc')
        if self.routing_profile_sha256 != _hash(self.identity_payload()):
            raise ValueError('routing profile hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'routing_profile_id': self.routing_profile_id,
            'profile_name': self.profile_name,
            'entries': [
                entry.model_dump(mode='json') for entry in self.entries
            ],
            'created_at_utc': self.created_at_utc,
            'provenance_json': self.provenance_json,
        }
        # Optional scope identity joins the seal only when present
        # (additive convention) — profiles persisted unscoped keep
        # their hash.
        if self.document_id is not None:
            payload['document_id'] = self.document_id
        if self.scene_revision_id is not None:
            payload['scene_revision_id'] = self.scene_revision_id
        return payload

    def entry_for_role(self, logical_role: str) -> CadChannelMapEntry | None:
        """Resolve the channel-map entry carrying ``logical_role``."""
        matches = [
            entry for entry in self.entries if entry.logical_role == logical_role
        ]
        return matches[0] if len(matches) == 1 else None


class CadRoutingProfileBinding(BaseModel):
    """Exact id/hash reference to a persisted routing profile."""

    model_config = ConfigDict(frozen=True)

    routing_profile_id: str = Field(min_length=1)
    routing_profile_sha256: str = Field(pattern=_SHA256_PATTERN)


def routing_profile_binding(
    profile: CadRoutingProfile,
) -> CadRoutingProfileBinding:
    return CadRoutingProfileBinding(
        routing_profile_id=profile.routing_profile_id,
        routing_profile_sha256=profile.routing_profile_sha256,
    )


def build_routing_profile(
    *,
    entries: Sequence[CadChannelMapEntry | dict[str, Any]],
    profile_name: str = 'routing',
    routing_profile_id: str | None = None,
    document_id: str | None = None,
    scene_revision_id: str | None = None,
    created_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadRoutingProfile:
    """Assemble a sealed routing-profile authority record."""
    resolved = tuple(
        item if isinstance(item, CadChannelMapEntry) else CadChannelMapEntry.model_validate(item)
        for item in entries
    )
    payload: dict[str, Any] = {
        'routing_profile_id': routing_profile_id or str(uuid4()),
        'profile_name': profile_name,
        'document_id': document_id,
        'scene_revision_id': scene_revision_id,
        'entries': resolved,
        'created_at_utc': created_at_utc or _utc_now(),
        'provenance_json': provenance_json,
    }
    provisional = CadRoutingProfile.model_construct(
        **payload,
        routing_profile_sha256='0' * 64,
    )
    return CadRoutingProfile(
        **payload,
        routing_profile_sha256=_hash(provisional.identity_payload()),
    )


def routing_profile_staleness(
    profile: CadRoutingProfile,
    *,
    avr_configuration_id: str | None = None,
    output_device_labels: frozenset[str] | tuple[str, ...] | None = None,
    speaker_ids: frozenset[str] | tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Concrete reasons a saved profile must not be silently reused.

    An empty tuple means the profile still applies. A change to the AVR
    configuration any entry was verified under, to an output device label,
    or to the physical speaker set is each reported separately so the
    caller can surface *what* moved instead of a bare 'stale' flag.
    """
    reasons: list[str] = []
    if avr_configuration_id is not None:
        for entry in profile.entries:
            if (
                entry.avr_configuration_id is not None
                and entry.avr_configuration_id != avr_configuration_id
            ):
                reasons.append(
                    'AVR configuration changed for channel '
                    f'{entry.rew_channel_label}'
                )
    if output_device_labels is not None:
        labels = frozenset(output_device_labels)
        for entry in profile.entries:
            if entry.output_device_label not in labels:
                reasons.append(
                    f'output device label no longer present: '
                    f'{entry.output_device_label}'
                )
    if speaker_ids is not None:
        available = frozenset(speaker_ids)
        for entry in profile.entries:
            missing = [
                speaker
                for speaker in (*entry.expected_speaker_ids, *entry.observed_speaker_ids)
                if speaker not in available
            ]
            if missing:
                reasons.append(
                    'speaker topology changed: '
                    + ', '.join(sorted(set(missing)))
                )
    return tuple(dict.fromkeys(reasons))


# ---------------------------------------------------------------------------
# Speaker wiring commissioning checks (#645)


WiringCheckKind = Literal[
    'routing',
    'continuity',
    'terminal_polarity',
    'acoustic_polarity',
    'load',
]

WiringCheckResult = Literal['PASS', 'FAIL', 'UNKNOWN', 'NOT_APPLICABLE']


# ---------------------------------------------------------------------------
# Typed electrical-load observation evidence (#857)


LoadQuantityKind = Literal['dcr', 'impedance_magnitude']
LoadTestCondition = Literal['dc', 'frequency']


class CadElectricalLoadObservation(BaseModel):
    """Typed quantitative load observation for a wiring ``load`` check.

    A DC resistance (DCR) reading and a frequency-dependent impedance
    magnitude are different quantities — ``quantity_kind`` keeps them apart
    so a DCR probe result can never be reported as an impedance curve.
    ``expected_min_ohm``/``expected_max_ohm`` carry the expected range the
    PASS/FAIL interpretation is derived from; ``expected_source`` names the
    authority for that range (datasheet, nominal spec, as-built record).
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    quantity_kind: LoadQuantityKind
    value_ohm: float = Field(gt=0)
    test_condition: LoadTestCondition
    test_frequency_hz: float | None = Field(default=None, gt=0)
    test_point: str | None = Field(default=None, min_length=1)
    instrument_label: str | None = Field(default=None, min_length=1)
    instrument_ref: str | None = Field(default=None, min_length=1)
    uncertainty_ohm: float | None = Field(default=None, ge=0)
    expected_min_ohm: float | None = Field(default=None, gt=0)
    expected_max_ohm: float | None = Field(default=None, gt=0)
    expected_source: str | None = Field(default=None, min_length=1)
    measured_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_observation(self) -> 'CadElectricalLoadObservation':
        if not isfinite(self.value_ohm):
            raise ValueError('load observation value must be finite')
        if self.test_condition == 'dc':
            if self.test_frequency_hz is not None:
                raise ValueError(
                    'a DC load observation carries no test frequency'
                )
        elif self.test_frequency_hz is None:
            raise ValueError(
                'a frequency-domain load observation requires '
                'test_frequency_hz'
            )
        if self.quantity_kind == 'dcr' and self.test_condition != 'dc':
            raise ValueError(
                'a DCR observation is a DC measurement, not a '
                'frequency-dependent impedance'
            )
        if (
            self.quantity_kind == 'impedance_magnitude'
            and self.test_condition != 'frequency'
        ):
            raise ValueError(
                'an impedance magnitude observation requires a '
                'frequency-domain test condition'
            )
        if (self.expected_min_ohm is None) != (self.expected_max_ohm is None):
            raise ValueError('expected load range requires both bounds')
        if (
            self.expected_min_ohm is not None
            and self.expected_max_ohm is not None
            and self.expected_max_ohm <= self.expected_min_ohm
        ):
            raise ValueError('expected load range is invalid')
        if self.uncertainty_ohm is not None and not isfinite(
            self.uncertainty_ohm
        ):
            raise ValueError('load uncertainty must be finite')
        _require_iso8601(
            self.measured_at_utc, 'load observation measured_at_utc'
        )
        return self


def derive_load_result(
    observation: CadElectricalLoadObservation,
) -> WiringCheckResult:
    """Deterministic interpretation of a load observation.

    A quantitative PASS/FAIL is reproduced from the measured value and the
    expected range, never trusted from the caller; without an expected range
    the observation is kept as evidence but stays UNKNOWN.
    """
    if (
        observation.expected_min_ohm is None
        or observation.expected_max_ohm is None
    ):
        return 'UNKNOWN'
    return (
        'PASS'
        if observation.expected_min_ohm
        <= observation.value_ohm
        <= observation.expected_max_ohm
        else 'FAIL'
    )


def build_electrical_load_observation(
    *,
    quantity_kind: LoadQuantityKind,
    value_ohm: float,
    test_condition: LoadTestCondition,
    measured_at_utc: str,
    observation_id: str | None = None,
    test_frequency_hz: float | None = None,
    test_point: str | None = None,
    instrument_label: str | None = None,
    instrument_ref: str | None = None,
    uncertainty_ohm: float | None = None,
    expected_min_ohm: float | None = None,
    expected_max_ohm: float | None = None,
    expected_source: str | None = None,
) -> CadElectricalLoadObservation:
    """Assemble a typed electrical-load observation record."""
    return CadElectricalLoadObservation(
        observation_id=observation_id or str(uuid4()),
        quantity_kind=quantity_kind,
        value_ohm=float(value_ohm),
        test_condition=test_condition,
        test_frequency_hz=test_frequency_hz,
        test_point=test_point,
        instrument_label=instrument_label,
        instrument_ref=instrument_ref,
        uncertainty_ohm=uncertainty_ohm,
        expected_min_ohm=expected_min_ohm,
        expected_max_ohm=expected_max_ohm,
        expected_source=expected_source,
        measured_at_utc=measured_at_utc,
    )

# Applied polarity-compensation token: each occurrence of this token in a
# check's ``applied_compensation`` list is one intended signal-polarity
# inversion (e.g. a DSP/AVR polarity control). Composition is explicit —
# the acoustic result is evaluated against the expected net polarity, never
# collapsed into a single ambiguous 'polarity ok' flag.
POLARITY_INVERSION_TOKEN = 'polarity_invert'


class CadWiringVerificationCheck(BaseModel):
    """One independent wiring-commissioning check result.

    Each check kind is an independent claim: continuity passing does not
    imply the routing is right, terminal polarity passing does not imply
    the acoustic polarity is right (in-room driver/crossover behaviour can
    still invert), and a load observation (e.g. DCR) is not an impedance
    curve. ``check_kind`` and ``result`` are always separate axes so a
    record can honestly carry ``NOT_APPLICABLE`` for a channel that does
    not expose that check.
    """

    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    check_kind: WiringCheckKind
    # Exact as-built pins (#825): the check is bound to one sealed scene
    # revision — id + content hash — so speaker refs resolve against the
    # same speakers the claim described, and optionally to the installed
    # SystemVariant the wiring was verified under.
    scene_revision_id: str = Field(min_length=1)
    scene_revision_sha256: str = Field(pattern=_SHA256_PATTERN)
    system_variant_id: str | None = None
    system_variant_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    # The exact routing profile a routing check is conditioned on; required
    # for a routing PASS, resolved id+hash at save.
    routing_profile: CadRoutingProfileBinding | None = None
    expected_output_reference: str | None = None
    expected_speaker_ids: tuple[str, ...] = ()
    source_speaker_ids: tuple[str, ...] = ()
    method: str = Field(min_length=1)
    observed_output_reference: str | None = None
    observed_speaker_ids: tuple[str, ...] = ()
    applied_compensation: tuple[str, ...] = ()
    evidence_refs: tuple[AuthorityRef | str, ...] = ()
    # #848/#857: exact typed bindings. ``routing_profile_ref`` pins the
    # #473 RoutingProfile a routing check resolved against;
    # ``load_observation`` is the quantitative evidence a ``load`` check
    # derives its result from. Both are optional so checks persisted before
    # this authority existed keep their hash.
    routing_profile_ref: CadRoutingProfileBinding | None = None
    load_observation: CadElectricalLoadObservation | None = None
    measured_at_utc: str = Field(min_length=1)
    operator: str | None = None
    result: WiringCheckResult
    reason: str | None = None
    notes: tuple[str, ...] = ()
    provenance_json: str = '{}'
    check_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator(
        'expected_speaker_ids',
        'source_speaker_ids',
        'observed_speaker_ids',
        'applied_compensation',
        'evidence_refs',
    )
    @classmethod
    def unique_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item for item in value):
            raise ValueError('list fields must hold unique non-empty values')
        return value

    @model_validator(mode='after')
    def valid_check(self) -> 'CadWiringVerificationCheck':
        _require_iso8601(self.measured_at_utc, 'wiring check measured_at_utc')
        if self.result in ('FAIL', 'UNKNOWN') and not (
            self.reason and self.reason.strip()
        ):
            raise ValueError('wiring check FAIL/UNKNOWN requires a reason')
        if (self.system_variant_id is None) != (
            self.system_variant_sha256 is None
        ):
            raise ValueError(
                'system variant pin requires both id and sha256'
            )
        if self.load_observation is not None and self.check_kind != 'load':
            raise ValueError(
                'a load observation is only valid on a load wiring check'
            )
        if self.result == 'PASS':
            # A PASS is a claim: it needs typed evidence, or an explicitly
            # named operator whose observation the record attests (#825).
            if not self.evidence_refs and self.operator is None:
                raise ValueError(
                    'wiring check PASS requires typed evidence or a named '
                    'operator — an unattributed confirmation is not evidence'
                )
            if self.check_kind == 'acoustic_polarity' and not self.evidence_refs:
                raise ValueError(
                    'acoustic polarity PASS requires typed measurement/test '
                    'evidence — an operator label cannot establish it'
                )
            if self.check_kind == 'routing' and (
                self.routing_profile is None
                and self.routing_profile_ref is None
            ):
                raise ValueError(
                    'routing check PASS requires a bound routing profile'
                )
        if self.check_sha256 != _hash(self.identity_payload()):
            raise ValueError('wiring verification check hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'check_id': self.check_id,
            'document_id': self.document_id,
            'check_kind': self.check_kind,
            'scene_revision_id': self.scene_revision_id,
            'scene_revision_sha256': self.scene_revision_sha256,
            'system_variant_id': self.system_variant_id,
            'system_variant_sha256': self.system_variant_sha256,
            'routing_profile': (
                self.routing_profile.model_dump(mode='json')
                if self.routing_profile is not None
                else None
            ),
            'expected_output_reference': self.expected_output_reference,
            'expected_speaker_ids': list(self.expected_speaker_ids),
            'source_speaker_ids': list(self.source_speaker_ids),
            'method': self.method,
            'observed_output_reference': self.observed_output_reference,
            'observed_speaker_ids': list(self.observed_speaker_ids),
            'applied_compensation': list(self.applied_compensation),
            'evidence_refs': [
                ref.model_dump(mode='json')
                if isinstance(ref, AuthorityRef)
                else ref
                for ref in self.evidence_refs
            ],
            'measured_at_utc': self.measured_at_utc,
            'operator': self.operator,
            'result': self.result,
            'reason': self.reason,
            'notes': list(self.notes),
            'provenance_json': self.provenance_json,
        }
        # Optional exact bindings join identity only when present (additive
        # convention) so checks persisted before #848/#857 keep their hash.
        if self.routing_profile_ref is not None:
            payload['routing_profile_ref'] = (
                self.routing_profile_ref.model_dump(mode='json')
            )
        if self.load_observation is not None:
            payload['load_observation'] = self.load_observation.model_dump(
                mode='json'
            )
        if self.scene_revision_id is not None:
            payload['scene_revision_id'] = self.scene_revision_id
        return payload


def build_wiring_check(
    *,
    document_id: str,
    check_kind: WiringCheckKind,
    method: str,
    result: WiringCheckResult,
    measured_at_utc: str,
    scene_revision_id: str,
    scene_revision_sha256: str,
    check_id: str | None = None,
    system_variant_id: str | None = None,
    system_variant_sha256: str | None = None,
    routing_profile: CadRoutingProfileBinding | CadRoutingProfile | None = None,
    expected_output_reference: str | None = None,
    expected_speaker_ids: Sequence[str] = (),
    source_speaker_ids: Sequence[str] = (),
    observed_output_reference: str | None = None,
    observed_speaker_ids: Sequence[str] = (),
    applied_compensation: Sequence[str] = (),
    evidence_refs: Sequence[AuthorityRef | str] = (),
    routing_profile_ref: CadRoutingProfileBinding | None = None,
    load_observation: CadElectricalLoadObservation | None = None,
    operator: str | None = None,
    reason: str | None = None,
    notes: Sequence[str] = (),
    provenance_json: str = '{}',
) -> CadWiringVerificationCheck:
    """Assemble a sealed wiring-verification check record."""
    if isinstance(routing_profile, CadRoutingProfile):
        profile_binding: CadRoutingProfileBinding | None = (
            routing_profile_binding(routing_profile)
        )
    else:
        profile_binding = routing_profile
    payload: dict[str, Any] = {
        'check_id': check_id or str(uuid4()),
        'document_id': document_id,
        'check_kind': check_kind,
        'scene_revision_id': scene_revision_id,
        'scene_revision_sha256': scene_revision_sha256,
        'system_variant_id': system_variant_id,
        'system_variant_sha256': system_variant_sha256,
        'routing_profile': profile_binding,
        'expected_output_reference': expected_output_reference,
        'expected_speaker_ids': tuple(expected_speaker_ids),
        'source_speaker_ids': tuple(source_speaker_ids),
        'method': method,
        'observed_output_reference': observed_output_reference,
        'observed_speaker_ids': tuple(observed_speaker_ids),
        'applied_compensation': tuple(applied_compensation),
        'evidence_refs': tuple(evidence_refs),
        'routing_profile_ref': routing_profile_ref,
        'load_observation': load_observation,
        'measured_at_utc': measured_at_utc,
        'operator': operator,
        'result': result,
        'reason': reason,
        'notes': tuple(notes),
        'provenance_json': provenance_json,
    }
    provisional = CadWiringVerificationCheck.model_construct(
        **payload,
        check_sha256='0' * 64,
    )
    return CadWiringVerificationCheck(
        **payload,
        check_sha256=_hash(provisional.identity_payload()),
    )


def latest_wiring_checks(
    checks: Sequence[CadWiringVerificationCheck],
) -> dict[WiringCheckKind, CadWiringVerificationCheck]:
    """Latest result per check kind, ordered by ``measured_at_utc``.

    Commissioning reads a per-kind headline — but each row stays an
    independent check; this helper never merges kinds into one verdict.
    """
    latest: dict[WiringCheckKind, CadWiringVerificationCheck] = {}
    for check in checks:
        previous = latest.get(check.check_kind)
        if (
            previous is None
            or (check.measured_at_utc, check.check_id)
            >= (previous.measured_at_utc, previous.check_id)
        ):
            latest[check.check_kind] = check
    return latest


def net_polarity_state(
    *,
    terminal_polarity_correct: bool | None,
    applied_compensation: Sequence[str],
    acoustic_polarity_correct: bool | None,
) -> WiringCheckResult:
    """Compose applied inversions with the acoustic polarity observation.

    ``applied_compensation`` counts intended inversions (each
    ``POLARITY_INVERSION_TOKEN`` is one). The expected acoustic sign is the
    composition of intended inversions and the terminal conductor state;
    the acoustic observation must match that expectation for PASS. Missing
    observations report UNKNOWN rather than collapsing into the other
    checks.
    """
    if acoustic_polarity_correct is None or terminal_polarity_correct is None:
        return 'UNKNOWN'
    inversions = sum(
        1
        for token in applied_compensation
        if token == POLARITY_INVERSION_TOKEN
    )
    expected_inverted = inversions % 2 == 1
    if not terminal_polarity_correct:
        expected_inverted = not expected_inverted
    acoustic_inverted = not acoustic_polarity_correct
    return 'PASS' if acoustic_inverted == expected_inverted else 'FAIL'


__all__ = [
    'POLARITY_INVERSION_TOKEN',
    'CadAcousticLevelCalibration',
    'CadChannelMapEntry',
    'CadDatasetLevelReference',
    'CadElectricalLoadObservation',
    'CadMeasurementTimingReference',
    'CadRoutingProfile',
    'CadRoutingProfileBinding',
    'CadTimingDelayCorrection',
    'CadWiringVerificationCheck',
    'LevelCalibrationMethod',
    'LevelCalibrationScope',
    'LoadQuantityKind',
    'LoadTestCondition',
    'MeasurementLevelReferenceKind',
    'RoutingVerification',
    'TimingCorrectionApplicationState',
    'TimingCorrectionKind',
    'TimingCorrectionSignConvention',
    'TimingReferenceMethod',
    'TimingReferenceScope',
    'TimingT0Convention',
    'WiringCheckKind',
    'WiringCheckResult',
    'absolute_spl_evidence_gaps',
    'build_acoustic_level_calibration',
    'build_dataset_level_reference',
    'build_electrical_load_observation',
    'build_routing_profile',
    'build_timing_reference',
    'build_wiring_check',
    'calibration_applies_to',
    'calibration_authorizes_absolute_spl',
    'calibration_supports_absolute_spl',
    'correction_already_applied',
    'correction_application_state',
    'correction_requires_application',
    'correction_sign_convention',
    'derive_load_result',
    'latest_wiring_checks',
    'net_polarity_state',
    'routing_profile',
    'routing_profile_staleness',
    'timing_clocks_shared',
    'timing_corrections_unambiguous',
    'timing_reference_authorizes_common_timing',
    'timing_reference_scope_is_applicable',
    'timing_reference_supports_common_timing',
]
