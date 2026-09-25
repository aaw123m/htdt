"""Playback-chain idle-noise / hum / buzz commissioning authority (#1044).

A home theater's "noise floor" is two different things: the room's ambient
noise (HVAC, traffic) and the playback chain's own self-noise (amplifier
hiss, ground-loop hum, switching-supply whine). This module records
measurements of the *playback chain's* idle noise — per channel, per
device-state — without ever conflating it with room ambient, fabricating
a diagnosis, or recommending defeating protective earthing.

Fail-closed contract:

- an idle-noise measurement is bound to the exact device/routing state it
  was recorded under — the same channel can have different noise under
  'electronics_powered', 'muted', 'unmuted_zero_input';
- channel identity is explicit (FL/FR/C/sub/…), never inferred from file
  order or list position;
- spectrum evidence is a recorded band series — a single dBA figure
  never becomes a spectrum;
- diagnostic hypotheses are *candidates* ("mains-frequency harmonic
  pattern candidate"), never declared causes — "ground loop" is a
  hypothesis to verify, not a conclusion;
- no hard-coded mains frequency — 50 Hz vs 60 Hz is an input, not a
  default;
- absolute SPL requires a calibration authority reference — without one
  the level is recorded as relative;
- measurements are immutable records; a device-state change produces a
  *new* record, never a mutated one;
- never recommend defeating protective earthing — the safety guard is
  hard-coded refusal.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus


IDLE_NOISE_AUTHORITY_VERSION = 'idle-noise-1'

#: What portion of the playback chain's state the measurement captures —
#: separating room ambient from electronics noise requires explicit states.
IdleNoiseDeviceState = Literal[
    'electronics_powered_off',
    'electronics_powered',
    'muted',
    'unmuted_zero_input',
    'unknown',
]

#: Which channel the measurement describes.
PlaybackChannel = Literal[
    'FL', 'FR', 'C', 'sub1', 'sub2',
    'SL', 'SR', 'SBL', 'SBR', 'TFL', 'TFR',
    'custom', 'unknown',
]

#: Candidate explanations a spectrum can suggest — hypotheses only.
IdleNoiseCandidate = Literal[
    'mains_harmonic_pattern_candidate',
    'switching_supply_candidate',
    'broadband_hiss_candidate',
    'ground_loop_candidate',
    'mechanical_vibration_candidate',
    'unknown',
]


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


def _finite(value: object, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class IdleNoiseBand(BaseModel):
    """One band level in an idle-noise spectrum record."""

    model_config = ConfigDict(frozen=True)

    band_center_hz: float = Field(gt=0.0)
    band_level_db: float

    @model_validator(mode='after')
    def finite_band(self) -> 'IdleNoiseBand':
        _finite(self.band_center_hz, field_name='band_center_hz')
        _finite(self.band_level_db, field_name='band_level_db')
        return self


class IdleNoiseSpectrum(BaseModel):
    """Recorded band-level evidence for an idle-noise measurement."""

    model_config = ConfigDict(frozen=True)

    bands: tuple[IdleNoiseBand, ...] = ()
    weighting: Literal['A', 'C', 'Z', 'flat', 'unknown'] = 'unknown'
    level_semantics: Literal[
        'absolute_spl', 'relative_difference', 'unknown'
    ] = 'unknown'

    @model_validator(mode='after')
    def valid_spectrum(self) -> 'IdleNoiseSpectrum':
        centers = [b.band_center_hz for b in self.bands]
        if len(set(centers)) != len(centers):
            raise ValueError('duplicate band centers')
        return self


class PlaybackIdleNoiseMeasurement(BaseModel):
    """An immutable record of one playback-chain noise measurement.

    ``calibration_authority_id`` binds an SPL calibration when the record
    claims absolute dB SPL; ``level_semantics='relative_difference'``
    records a level without claiming absolute SPL.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'idle-noise-1'
    ] = IDLE_NOISE_AUTHORITY_VERSION
    measurement_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    channel: PlaybackChannel = 'unknown'
    device_state: IdleNoiseDeviceState = 'unknown'
    volume_setting: str | None = None
    mute_state: Literal['muted', 'unmuted', 'unknown'] = 'unknown'
    spectrum: IdleNoiseSpectrum | None = None
    acquisition_context_id: str | None = None
    calibration_authority_id: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_measurement(self) -> 'PlaybackIdleNoiseMeasurement':
        if self.spectrum is not None:
            if self.spectrum.level_semantics == 'absolute_spl' and (
                self.calibration_authority_id is None
            ):
                raise ValueError(
                    'absolute SPL requires a calibration authority'
                )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'PlaybackIdleNoiseMeasurement semantic hash mismatch'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'measurement_id': self.measurement_id,
            'version': self.version,
            'session_id': self.session_id,
            'device_id': self.device_id,
            'channel': self.channel,
            'device_state': self.device_state,
            'volume_setting': self.volume_setting,
            'mute_state': self.mute_state,
            'spectrum': (
                self.spectrum.model_dump(mode='json')
                if self.spectrum is not None
                else None
            ),
            'acquisition_context_id': self.acquisition_context_id,
            'calibration_authority_id': self.calibration_authority_id,
            'provenance': [
                item.model_dump(mode='json') for item in self.provenance
            ],
        }


def build_idle_noise_measurement(
    *,
    measurement_id: str | None = None,
    version: str = '1',
    session_id: str,
    device_id: str,
    channel: PlaybackChannel = 'unknown',
    device_state: IdleNoiseDeviceState = 'unknown',
    volume_setting: str | None = None,
    mute_state: Literal['muted', 'unmuted', 'unknown'] = 'unknown',
    spectrum: IdleNoiseSpectrum | None = None,
    acquisition_context_id: str | None = None,
    calibration_authority_id: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> PlaybackIdleNoiseMeasurement:
    payload: dict[str, Any] = {
        'authority_version': IDLE_NOISE_AUTHORITY_VERSION,
        'measurement_id': measurement_id or str(uuid4()),
        'version': version,
        'session_id': session_id,
        'device_id': device_id,
        'channel': channel,
        'device_state': device_state,
        'volume_setting': volume_setting,
        'mute_state': mute_state,
        'spectrum': spectrum,
        'acquisition_context_id': acquisition_context_id,
        'calibration_authority_id': calibration_authority_id,
        'provenance': provenance,
    }
    provisional = PlaybackIdleNoiseMeasurement.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return PlaybackIdleNoiseMeasurement(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


def detect_mains_harmonic_candidate(
    *,
    measurement: PlaybackIdleNoiseMeasurement,
    mains_frequency_hz: float,
    tolerance_hz: float = 2.0,
) -> bool:
    """Check whether the spectrum shows a mains-harmonic pattern.

    A candidate is ``True`` when the recorded bands concentrate at
    integer multiples of the *given* mains frequency — 50 vs 60 Hz is an
    input, never a default. The result is a candidate for investigation,
    not a diagnosis ("mains-frequency harmonic pattern candidate",
    never "ground loop confirmed").
    """

    _finite(mains_frequency_hz, field_name='mains_frequency_hz')
    _finite(tolerance_hz, field_name='tolerance_hz')
    if measurement.spectrum is None or not measurement.spectrum.bands:
        return False
    hits = 0
    for band in measurement.spectrum.bands:
        ratio = band.band_center_hz / mains_frequency_hz
        nearest = round(ratio)
        if nearest >= 1 and abs(
            band.band_center_hz - nearest * mains_frequency_hz
        ) <= tolerance_hz:
            hits += 1
    return hits >= 2


class IdleNoiseCheck(BaseModel):
    """One commissioning check on an idle-noise measurement."""

    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str


class IdleNoiseCommissioningReport(BaseModel):
    """Commissioning report for an idle-noise measurement — channel,
    device state, spectrum evidence and calibration are evaluated
    separately so an unqualified record stays UNKNOWN."""

    model_config = ConfigDict(frozen=True)

    report_id: str = Field(min_length=1)
    measurement_id: str
    measurement_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    checks: tuple[IdleNoiseCheck, ...]
    candidates: tuple[IdleNoiseCandidate, ...] = ()
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_report(self) -> 'IdleNoiseCommissioningReport':
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('idle-noise report hash mismatch')
        expected = 'inr-' + self.report_sha256[:24]
        if self.report_id != expected:
            raise ValueError('idle-noise report id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': IDLE_NOISE_AUTHORITY_VERSION,
            'measurement_id': self.measurement_id,
            'measurement_sha256': self.measurement_sha256,
            'checks': [
                check.model_dump(mode='json') for check in self.checks
            ],
            'candidates': list(self.candidates),
        }


def evaluate_idle_noise_commissioning(
    *,
    measurement: PlaybackIdleNoiseMeasurement,
    mains_frequency_hz: float | None = None,
) -> IdleNoiseCommissioningReport:
    """Evaluate one idle-noise measurement for commissioning.

    Checks:
    - ``channel_bound`` — explicit channel identity;
    - ``device_state_recorded`` — the record's electronics/mute state is
      declared, separating powered/muted/unmuted-zero-input;
    - ``spectrum_recorded`` — band evidence exists, not a bare dBA;
    - ``absolute_spl_calibrated`` — absolute claims carry calibration;
    - ``mains_candidate`` — flagged only when ``mains_frequency_hz`` is
      supplied AND the bands concentrate at its harmonics; surfaced as a
      *candidate* label, never a diagnosis.
    """

    checks: list[IdleNoiseCheck] = []
    candidates: list[IdleNoiseCandidate] = []

    checks.append(
        IdleNoiseCheck(
            check='channel_bound',
            status=(
                'PASS' if measurement.channel != 'unknown' else 'UNKNOWN'
            ),
            reason=(
                f'channel: {measurement.channel}'
                if measurement.channel != 'unknown'
                else 'no channel identity recorded'
            ),
        )
    )

    checks.append(
        IdleNoiseCheck(
            check='device_state_recorded',
            status=(
                'PASS'
                if measurement.device_state != 'unknown'
                else 'UNKNOWN'
            ),
            reason=(
                f'device state: {measurement.device_state}'
                if measurement.device_state != 'unknown'
                else 'device state unrecorded — cannot separate '
                'electronics noise from room ambient'
            ),
        )
    )

    has_spectrum = (
        measurement.spectrum is not None
        and len(measurement.spectrum.bands) > 0
    )
    checks.append(
        IdleNoiseCheck(
            check='spectrum_recorded',
            status='PASS' if has_spectrum else 'UNKNOWN',
            reason=(
                'spectrum bands recorded'
                if has_spectrum
                else 'no band-level spectrum — a bare dBA figure is not '
                'a spectrum'
            ),
        )
    )

    semantics = (
        measurement.spectrum.level_semantics
        if measurement.spectrum is not None
        else 'unknown'
    )
    if semantics == 'absolute_spl':
        calibrated = measurement.calibration_authority_id is not None
        checks.append(
            IdleNoiseCheck(
                check='absolute_spl_calibrated',
                status='PASS' if calibrated else 'FAIL',
                reason=(
                    'absolute SPL bound to calibration authority'
                    if calibrated
                    else 'absolute SPL without calibration authority'
                ),
            )
        )
    else:
        checks.append(
            IdleNoiseCheck(
                check='absolute_spl_calibrated',
                status='NOT_APPLICABLE',
                reason='level is not claimed as absolute SPL',
            )
        )

    if mains_frequency_hz is not None and has_spectrum:
        if detect_mains_harmonic_candidate(
            measurement=measurement,
            mains_frequency_hz=mains_frequency_hz,
        ):
            candidates.append('mains_harmonic_pattern_candidate')
            checks.append(
                IdleNoiseCheck(
                    check='mains_candidate',
                    status='UNKNOWN',
                    reason='spectrum concentrates at mains harmonics — '
                    'candidate for investigation, not a diagnosis',
                )
            )
        else:
            checks.append(
                IdleNoiseCheck(
                    check='mains_candidate',
                    status='PASS',
                    reason='no mains-harmonic pattern in recorded bands',
                )
            )
    else:
        checks.append(
            IdleNoiseCheck(
                check='mains_candidate',
                status='NOT_APPLICABLE',
                reason='mains frequency not supplied — no mains check '
                'performed',
            )
        )

    probe = IdleNoiseCommissioningReport.model_construct(
        report_id='',
        measurement_id=measurement.measurement_id,
        measurement_sha256=measurement.semantic_sha256,
        checks=tuple(checks),
        candidates=tuple(candidates),
        report_sha256='',
    )
    digest = _hash(probe.identity_payload())
    return IdleNoiseCommissioningReport(
        **probe.model_dump(
            mode='python',
            exclude={'report_sha256', 'report_id'},
        ),
        report_id='inr-' + digest[:24],
        report_sha256=digest,
    )


def suggest_idle_noise_actions(
    *, candidates: tuple[IdleNoiseCandidate, ...]
) -> tuple[str, ...]:
    """Map candidates to safe next-investigation steps.

    Never recommends defeating protective earthing — the safety note is
    unconditional. Returns empty tuple when there are no candidates.
    """

    if not candidates:
        return ()
    notes: list[str] = []
    if 'mains_harmonic_pattern_candidate' in candidates:
        notes.append(
            'check cable routing, shielding and ground topology — '
            'never defeat protective earthing'
        )
    if 'switching_supply_candidate' in candidates:
        notes.append('inspect switch-mode power supply loading')
    if 'broadband_hiss_candidate' in candidates:
        notes.append('check gain staging and amplifier noise floor')
    if 'ground_loop_candidate' in candidates:
        notes.append(
            'verify ground topology with isolation-safe methods — '
            'never lift protective earth'
        )
    if 'mechanical_vibration_candidate' in candidates:
        notes.append('check fans, drives and mechanical decoupling')
    notes.append(
        'safety: never defeat protective earthing as a noise fix'
    )
    return tuple(notes)


__all__ = [
    'IDLE_NOISE_AUTHORITY_VERSION',
    'IdleNoiseCandidate',
    'IdleNoiseCheck',
    'IdleNoiseBand',
    'IdleNoiseCommissioningReport',
    'IdleNoiseDeviceState',
    'IdleNoiseSpectrum',
    'PlaybackChannel',
    'PlaybackIdleNoiseMeasurement',
    'build_idle_noise_measurement',
    'detect_mains_harmonic_candidate',
    'evaluate_idle_noise_commissioning',
    'suggest_idle_noise_actions',
]
