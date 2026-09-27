"""Measurement-chain dynamic range authority (#1007).

Correct calibration, timing and position do not rescue a measurement
whose input chain was non-linear: capsule overload, preamp clipping ahead
of the ADC, gain-state mismatch against the assumed calibration, or
evidence buried in the instrument noise floor. In particular:

> ``digital_peak < 0 dBFS`` does not prove the whole chain was linear —
  clipping can occur in the capsule/preamp before the ADC ever sees full
  scale.

This module adds:

- ``MeasurementInputChainProfile`` — immutable per-chain capability
  (capsule / preamp / ADC stages kept distinct where manufacturer
  evidence exists; the exact gain/PGA state is part of the authority, not
  a footnote);
- ``CaptureDynamicRangeObservation`` — sealed per-capture evidence
  (observed peak, headroom, typed clipping state, noise-floor proximity);
- gate helpers that decide whether FR/IR and distortion evidence is
  supported, limited or unsupported given the chain capability and the
  observation.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash






_SHA256_PATTERN = r'^[0-9a-f]{64}$'


ClippingState = Literal[
    'none',
    'digital',
    'analog_suspected',
    'upstream_suspected',
    'unknown',
]
"""``analog_suspected``/``upstream_suspected`` require evidence (producer
warning, overloaded stage flag, SPL-vs-capability exceedance); a clean
digital waveform alone can never assert 'none' for upstream stages."""

EvidenceGateState = Literal[
    'supported', 'limited', 'unsupported', 'unknown'
]


class ChainStageCapability(BaseModel):
    """Capability of one input-chain stage (capsule / preamp / ADC).

    Only quantities with manufacturer/producer evidence are populated —
    internal-stage limits are never invented for products that publish a
    single overall limit.
    """

    model_config = ConfigDict(frozen=True)

    stage: Literal['capsule', 'preamp', 'adc', 'interface', 'combined', 'unknown']
    max_spl_db: float | None = None
    max_spl_distortion_percent: float | None = None
    max_spl_at_frequency_hz: float | None = None
    max_input_dbfs: float | None = None
    self_noise_db_spl: float | None = None
    self_noise_dbfs: float | None = None
    equivalent_input_noise_db_spl: float | None = None
    gain_db: float | None = None
    sensitivity_mv_per_pa: float | None = Field(default=None, gt=0.0)
    bit_depth: int | None = Field(default=None, ge=1)
    adc_snr_db: float | None = None

    @model_validator(mode='after')
    def valid_stage(self) -> 'ChainStageCapability':
        for label, value in (
            ('max_spl_db', self.max_spl_db),
            ('max_spl_distortion_percent', self.max_spl_distortion_percent),
            ('max_spl_at_frequency_hz', self.max_spl_at_frequency_hz),
            ('max_input_dbfs', self.max_input_dbfs),
            ('self_noise_db_spl', self.self_noise_db_spl),
            ('self_noise_dbfs', self.self_noise_dbfs),
            ('equivalent_input_noise_db_spl', self.equivalent_input_noise_db_spl),
            ('gain_db', self.gain_db),
            ('sensitivity_mv_per_pa', self.sensitivity_mv_per_pa),
            ('adc_snr_db', self.adc_snr_db),
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{label} must be finite')
        if self.max_spl_db is not None and self.max_spl_distortion_percent is None:
            raise ValueError(
                'max_spl_db requires the stated distortion criterion '
                '(e.g. 1% THD at 1 kHz) — an unqualified max SPL is not a '
                'capability'
            )
        return self


class MeasurementInputChainProfile(BaseModel):
    """Immutable input-chain capability; gain state is part of identity."""

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    microphone_ref: str | None = None
    stages: tuple[ChainStageCapability, ...] = ()
    input_channel: str | None = None
    gain_state_db: float | None = None
    gain_state_label: str | None = None
    sample_rate_hz: float | None = Field(default=None, gt=0.0)
    bit_depth: int | None = Field(default=None, ge=1)
    coupling: Literal['analog', 'digital_usb', 'combined', 'unknown'] = 'unknown'
    calibration_refs: tuple[str, ...] = ()
    producer: str | None = None
    device_firmware: str | None = None
    valid_conditions: str | None = None
    provenance_json: str = '{}'
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_profile(self) -> 'MeasurementInputChainProfile':
        stage_kinds = [stage.stage for stage in self.stages]
        if len(stage_kinds) != len(set(stage_kinds)):
            raise ValueError('duplicate chain stages are not allowed')
        if not self.stages:
            raise ValueError(
                'input-chain profile requires at least one stage capability '
                '(or a combined stage) — no invented limits'
            )
        if self.gain_state_db is not None and not isfinite(float(self.gain_state_db)):
            raise ValueError('gain_state_db must be finite')
        if self.profile_sha256 != _hash(self.identity_payload()):
            raise ValueError('input-chain profile hash mismatch')
        return self

    def stage(self, kind: str) -> ChainStageCapability | None:
        for stage in self.stages:
            if stage.stage == kind:
                return stage
        return None

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'profile_sha256'})


class CaptureDynamicRangeObservation(BaseModel):
    """Sealed per-capture dynamic-range evidence bound to one chain."""

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    chain_profile_id: str = Field(min_length=1)
    chain_profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    gain_state_db: float | None = None
    observed_peak_dbfs: float | None = None
    observed_headroom_db: float | None = None
    clipping_state: ClippingState = 'unknown'
    clipping_evidence_refs: tuple[str, ...] = ()
    noise_floor_dbfs: float | None = None
    noise_floor_db_spl: float | None = None
    signal_to_noise_floor_db: float | None = None
    observed_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_observation(self) -> 'CaptureDynamicRangeObservation':
        for label, value in (
            ('gain_state_db', self.gain_state_db),
            ('observed_peak_dbfs', self.observed_peak_dbfs),
            ('observed_headroom_db', self.observed_headroom_db),
            ('noise_floor_dbfs', self.noise_floor_dbfs),
            ('noise_floor_db_spl', self.noise_floor_db_spl),
            ('signal_to_noise_floor_db', self.signal_to_noise_floor_db),
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{label} must be finite')
        if (
            self.observed_peak_dbfs is not None
            and self.observed_headroom_db is not None
            and abs(
                (0.0 - float(self.observed_peak_dbfs))
                - float(self.observed_headroom_db)
            )
            > 0.51
        ):
            raise ValueError(
                'observed_headroom_db must equal 0 - observed_peak_dbfs '
                'under the dBFS convention'
            )
        if self.clipping_state in ('analog_suspected', 'upstream_suspected'):
            if not self.clipping_evidence_refs:
                raise ValueError(
                    'upstream clipping states require explicit evidence — '
                    'a clean digital peak never proves analog linearity'
                )
        if self.observation_sha256 != _hash(self.identity_payload()):
            raise ValueError('dynamic-range observation hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'observation_sha256'})


class EvidenceGateResult(BaseModel):
    """Whether a downstream claim is gated in by the chain authority."""

    model_config = ConfigDict(frozen=True)

    gate: Literal['fr_ir', 'distortion', 'absolute_spl', 'unknown']
    state: EvidenceGateState
    reasons: tuple[str, ...] = ()


def build_input_chain_profile(**kwargs: Any) -> MeasurementInputChainProfile:
    """Assemble and seal a :class:`MeasurementInputChainProfile`."""
    payload = {'profile_sha256': '0' * 64, **kwargs}
    provisional = MeasurementInputChainProfile.model_construct(**payload)
    payload['profile_sha256'] = _hash(provisional.identity_payload())
    return MeasurementInputChainProfile(**payload)


def build_dynamic_range_observation(
    **kwargs: Any,
) -> CaptureDynamicRangeObservation:
    """Assemble and seal a :class:`CaptureDynamicRangeObservation`."""
    payload = {'observation_sha256': '0' * 64, **kwargs}
    provisional = CaptureDynamicRangeObservation.model_construct(**payload)
    payload['observation_sha256'] = _hash(provisional.identity_payload())
    return CaptureDynamicRangeObservation(**payload)


def gate_fr_ir_evidence(
    profile: MeasurementInputChainProfile,
    observation: CaptureDynamicRangeObservation | None,
) -> EvidenceGateResult:
    """FR/IR gating: clipping and headroom bound the usable evidence."""
    reasons: list[str] = []
    if observation is None:
        return EvidenceGateResult(
            gate='fr_ir',
            state='unknown',
            reasons=('no dynamic-range observation bound',),
        )
    if observation.clipping_state in (
        'digital',
        'analog_suspected',
        'upstream_suspected',
    ):
        return EvidenceGateResult(
            gate='fr_ir',
            state='unsupported',
            reasons=(
                f'capture carries clipping state {observation.clipping_state} — '
                'the derived FR/IR cannot claim linear evidence',
            ),
        )
    if observation.observed_headroom_db is not None:
        if observation.observed_headroom_db < 6.0:
            reasons.append(
                f'headroom {observation.observed_headroom_db:g} dB below the '
                '6 dB caution band'
            )
            return EvidenceGateResult(
                gate='fr_ir', state='limited', reasons=tuple(reasons)
            )
    if observation.clipping_state == 'unknown':
        reasons.append('upstream clipping state unknown — not proven clean')
        return EvidenceGateResult(
            gate='fr_ir', state='unknown', reasons=tuple(reasons)
        )
    reasons.append('no clipping evidence and adequate headroom')
    return EvidenceGateResult(gate='fr_ir', state='supported', reasons=tuple(reasons))


def gate_distortion_evidence(
    profile: MeasurementInputChainProfile,
    observation: CaptureDynamicRangeObservation | None,
    *,
    measured_noise_floor_db: float | None = None,
) -> EvidenceGateResult:
    """Distortion gating: the result must sit above the instrument floor."""
    if observation is None:
        return EvidenceGateResult(
            gate='distortion',
            state='unknown',
            reasons=('no dynamic-range observation bound',),
        )
    if observation.clipping_state in (
        'digital',
        'analog_suspected',
        'upstream_suspected',
    ):
        return EvidenceGateResult(
            gate='distortion',
            state='unsupported',
            reasons=(
                f'capture carries clipping state {observation.clipping_state} — '
                'distortion evidence is dominated by chain nonlinearity',
            ),
        )
    instrument_floor_db = observation.noise_floor_dbfs
    if instrument_floor_db is None:
        for stage in profile.stages:
            if stage.self_noise_dbfs is not None:
                instrument_floor_db = stage.self_noise_dbfs
                break
    if measured_noise_floor_db is not None and instrument_floor_db is not None:
        margin = float(measured_noise_floor_db) - float(instrument_floor_db)
        if margin <= 0.0:
            return EvidenceGateResult(
                gate='distortion',
                state='unsupported',
                reasons=(
                    'measured floor is at or below the instrument noise '
                    'floor — nonlinear content cannot be resolved',
                ),
            )
        if margin < 6.0:
            return EvidenceGateResult(
                gate='distortion',
                state='limited',
                reasons=(
                    f'measured floor only {margin:.1f} dB above the '
                    'instrument floor — marginal separation',
                ),
            )
        return EvidenceGateResult(
            gate='distortion',
            state='supported',
            reasons=(
                f'measured floor {margin:.1f} dB above instrument floor',
            ),
        )
    if instrument_floor_db is None:
        return EvidenceGateResult(
            gate='distortion',
            state='unknown',
            reasons=(
                'no instrument noise-floor capability declared — '
                'distortion evidence cannot be gated against the chain'
            ),
        )
    return EvidenceGateResult(
        gate='distortion',
        state='limited',
        reasons=(
            'instrument floor known but no measured floor supplied for '
            'separation',
        ),
    )
