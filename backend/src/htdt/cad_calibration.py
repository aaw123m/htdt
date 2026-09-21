from __future__ import annotations

import cmath
import csv
from dataclasses import dataclass
from hashlib import sha256
import io
import json
from math import cos, isfinite, log10, pi, sin
from typing import Any, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_measurement_models import CadFrequencyResponseDataset, CadMeasurementRecord
from .cad_measurement_quality import (
    CadMeasurementQualityReport,
    MeasurementCapabilityClaim,
    dataset_sha256,
    gate_measurement_claim,
    measurement_sha256,
)
from .cad_repository import SceneRevision
from .cad_scene import Position3
from .cad_system_variant import SystemVariant
from .csv_export import csv_safe_row


CALIBRATION_PLAN_SCHEMA_VERSION = 1
CALIBRATION_PLAN_AUTHORITY_VERSION = 'calibration-plan-1'
GENERIC_BIQUAD_ADAPTER_ID = 'htdt-generic-biquad'
GENERIC_BIQUAD_ADAPTER_VERSION = '1'
BIQUAD_COEFFICIENT_CONVENTION = 'a0_normalized'
BIQUAD_COEFFICIENT_ORDER = 'b0,b1,b2,a1,a2'
BIQUAD_SIGN_CONVENTION = 'denominator=1+a1*z^-1+a2*z^-2'

CalibrationSupportState = Literal['SUPPORTED', 'UNSUPPORTED']
CalibrationSourceKind = Literal['user', 'provided_fixture', 'imported']
CalibrationLifecycleState = Literal[
    'proposed',
    'exported',
    'user_applied',
    'remeasured',
    'validated',
]
BiquadFilterType = Literal['peaking', 'low_pass', 'high_pass', 'all_pass']


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


def _finite(value: float, name: str) -> float:
    value = float(value)
    if not isfinite(value):
        raise ValueError(f'{name} must be finite')
    return value


def _quantize(value: float, resolution: float | None) -> float:
    value = float(value)
    if resolution is None:
        return value
    steps = round(value / resolution)
    quantized = round(steps * resolution, 12)
    if quantized == -0.0:
        return 0.0
    return quantized


class CadBiquadFilter(BaseModel):
    """Device-neutral, normalized direct-form biquad definition."""

    model_config = ConfigDict(frozen=True)

    filter_id: str = Field(min_length=1)
    filter_type: BiquadFilterType
    frequency_hz: float = Field(gt=0.0)
    q: float = Field(gt=0.0)
    gain_db: float = 0.0
    sample_rate_hz: int = Field(gt=0)
    coefficient_convention: Literal['a0_normalized'] = BIQUAD_COEFFICIENT_CONVENTION
    coefficient_ordering: Literal['b0,b1,b2,a1,a2'] = BIQUAD_COEFFICIENT_ORDER
    sign_convention: Literal[
        'denominator=1+a1*z^-1+a2*z^-2'
    ] = BIQUAD_SIGN_CONVENTION
    coefficients: tuple[float, float, float, float, float]

    @model_validator(mode='after')
    def valid_filter(self) -> 'CadBiquadFilter':
        if self.frequency_hz >= self.sample_rate_hz / 2:
            raise ValueError('biquad frequency must be below Nyquist')
        if self.filter_type != 'peaking' and abs(self.gain_db) > 1e-12:
            raise ValueError('non-peaking biquad gain_db must be zero')
        values = (
            self.frequency_hz,
            self.q,
            self.gain_db,
            *self.coefficients,
        )
        if any(not isfinite(float(value)) for value in values):
            raise ValueError('biquad parameters and coefficients must be finite')
        b0, b1, b2, a1, a2 = self.coefficients
        roots = (
            (-a1 + cmath.sqrt(complex(a1 * a1 - 4.0 * a2))) / 2.0,
            (-a1 - cmath.sqrt(complex(a1 * a1 - 4.0 * a2))) / 2.0,
        )
        if any(abs(root) >= 1.0 for root in roots):
            raise ValueError('biquad poles must be strictly inside the unit circle')
        expected = calculate_biquad_coefficients(
            filter_type=self.filter_type,
            frequency_hz=self.frequency_hz,
            q=self.q,
            gain_db=self.gain_db,
            sample_rate_hz=self.sample_rate_hz,
        )
        if any(abs(a - b) > 1e-12 for a, b in zip(self.coefficients, expected, strict=True)):
            raise ValueError('biquad coefficients do not match normalized parameter convention')
        return self


def calculate_biquad_coefficients(
    *,
    filter_type: BiquadFilterType,
    frequency_hz: float,
    q: float,
    gain_db: float = 0.0,
    sample_rate_hz: int,
) -> tuple[float, float, float, float, float]:
    frequency_hz = _finite(frequency_hz, 'frequency_hz')
    q = _finite(q, 'q')
    gain_db = _finite(gain_db, 'gain_db')
    if frequency_hz <= 0.0 or q <= 0.0 or sample_rate_hz <= 0:
        raise ValueError('invalid biquad parameters')
    if frequency_hz >= sample_rate_hz / 2:
        raise ValueError('biquad frequency must be below Nyquist')
    if filter_type != 'peaking' and abs(gain_db) > 1e-12:
        raise ValueError('non-peaking biquad gain_db must be zero')

    omega = 2.0 * pi * frequency_hz / sample_rate_hz
    c = cos(omega)
    alpha = sin(omega) / (2.0 * q)

    if filter_type == 'peaking':
        a = 10.0 ** (gain_db / 40.0)
        b0 = 1.0 + alpha * a
        b1 = -2.0 * c
        b2 = 1.0 - alpha * a
        a0 = 1.0 + alpha / a
        a1 = -2.0 * c
        a2 = 1.0 - alpha / a
    elif filter_type == 'low_pass':
        b0 = (1.0 - c) / 2.0
        b1 = 1.0 - c
        b2 = (1.0 - c) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * c
        a2 = 1.0 - alpha
    elif filter_type == 'high_pass':
        b0 = (1.0 + c) / 2.0
        b1 = -(1.0 + c)
        b2 = (1.0 + c) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * c
        a2 = 1.0 - alpha
    elif filter_type == 'all_pass':
        b0 = 1.0 - alpha
        b1 = -2.0 * c
        b2 = 1.0 + alpha
        a0 = 1.0 + alpha
        a1 = -2.0 * c
        a2 = 1.0 - alpha
    else:
        raise ValueError(f'unsupported biquad filter type: {filter_type}')

    values = (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)
    if any(not isfinite(float(value)) for value in values):
        raise ValueError('biquad coefficient calculation produced a non-finite value')
    return tuple(float(value) for value in values)  # type: ignore[return-value]


def build_biquad_filter(
    *,
    filter_id: str,
    filter_type: BiquadFilterType,
    frequency_hz: float,
    q: float,
    gain_db: float = 0.0,
    sample_rate_hz: int,
) -> CadBiquadFilter:
    return CadBiquadFilter(
        filter_id=filter_id,
        filter_type=filter_type,
        frequency_hz=frequency_hz,
        q=q,
        gain_db=gain_db,
        sample_rate_hz=sample_rate_hz,
        coefficients=calculate_biquad_coefficients(
            filter_type=filter_type,
            frequency_hz=frequency_hz,
            q=q,
            gain_db=gain_db,
            sample_rate_hz=sample_rate_hz,
        ),
    )


def evaluate_biquad_transfer(
    filter_definition: CadBiquadFilter,
    frequency_hz: float,
) -> complex:
    frequency_hz = _finite(frequency_hz, 'frequency_hz')
    if frequency_hz < 0.0 or frequency_hz > filter_definition.sample_rate_hz / 2:
        raise ValueError('transfer evaluation frequency is outside [0, Nyquist]')
    b0, b1, b2, a1, a2 = filter_definition.coefficients
    z1 = cmath.exp(complex(0.0, -2.0 * pi * frequency_hz / filter_definition.sample_rate_hz))
    numerator = b0 + b1 * z1 + b2 * z1 * z1
    denominator = 1.0 + a1 * z1 + a2 * z1 * z1
    if abs(denominator) == 0.0:
        raise ValueError('biquad transfer denominator is zero')
    return numerator / denominator


def evaluate_biquad_db(
    filter_definition: CadBiquadFilter,
    frequency_hz: float,
) -> float:
    magnitude = abs(evaluate_biquad_transfer(filter_definition, frequency_hz))
    if magnitude <= 0.0 or not isfinite(magnitude):
        raise ValueError('biquad transfer magnitude is not finite and positive')
    return 20.0 * log10(magnitude)


class CadCrossoverSetting(BaseModel):
    model_config = ConfigDict(frozen=True)

    crossover_type: Literal['high_pass', 'low_pass']
    frequency_hz: float = Field(gt=0.0)
    filter_order: int = Field(ge=1, le=8)


class CadTargetCurvePoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    frequency_hz: float = Field(gt=0.0)
    level_db: float


class CadTargetNormalizationCondition(BaseModel):
    model_config = ConfigDict(frozen=True)

    method: Literal['reference_frequency', 'band_average', 'absolute_level']
    reference_frequency_hz: float | None = Field(default=None, gt=0.0)
    reference_band_hz: tuple[float, float] | None = None
    reference_level_db: float | None = None

    @model_validator(mode='after')
    def valid_normalization(self) -> 'CadTargetNormalizationCondition':
        if self.method == 'reference_frequency' and self.reference_frequency_hz is None:
            raise ValueError('reference_frequency normalization requires reference_frequency_hz')
        if self.method == 'band_average':
            if self.reference_band_hz is None:
                raise ValueError('band_average normalization requires reference_band_hz')
            low, high = self.reference_band_hz
            if low <= 0.0 or high <= low or not isfinite(low) or not isfinite(high):
                raise ValueError('target normalization band is invalid')
        if self.method == 'absolute_level' and self.reference_level_db is None:
            raise ValueError('absolute_level normalization requires reference_level_db')
        for value in (
            self.reference_frequency_hz,
            self.reference_level_db,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('target normalization value must be finite')
        return self


class CadTargetCurve(BaseModel):
    model_config = ConfigDict(frozen=True)

    points: tuple[CadTargetCurvePoint, ...] = Field(min_length=2)
    normalization: CadTargetNormalizationCondition

    @model_validator(mode='after')
    def valid_curve(self) -> 'CadTargetCurve':
        frequencies = tuple(point.frequency_hz for point in self.points)
        if any(b <= a for a, b in zip(frequencies, frequencies[1:])):
            raise ValueError('target curve frequencies must be strictly increasing')
        if any(not isfinite(float(point.level_db)) for point in self.points):
            raise ValueError('target curve levels must be finite')
        return self


class CadDeviceCapabilityConstraints(BaseModel):
    """Explicit generic device limits. None means the limit is unknown, not unlimited."""

    model_config = ConfigDict(frozen=True)

    capability_id: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    supported_sample_rates_hz: tuple[int, ...] = Field(min_length=1)
    supported_filter_types: tuple[BiquadFilterType, ...] = ('peaking',)
    max_filters_per_channel: int | None = Field(default=None, ge=0)
    max_boost_db: float | None = Field(default=None, ge=0.0)
    max_cut_db: float | None = Field(default=None, ge=0.0)
    min_gain_db: float | None = None
    max_gain_db: float | None = None
    max_delay_s: float | None = Field(default=None, ge=0.0)
    supported_crossover_orders: tuple[int, ...] = ()
    allowed_physical_outputs: tuple[str, ...] = ()
    frequency_resolution_hz: float | None = Field(default=None, gt=0.0)
    q_resolution: float | None = Field(default=None, gt=0.0)
    filter_gain_resolution_db: float | None = Field(default=None, gt=0.0)
    channel_gain_resolution_db: float | None = Field(default=None, gt=0.0)
    delay_resolution_s: float | None = Field(default=None, gt=0.0)

    @model_validator(mode='after')
    def valid_constraints(self) -> 'CadDeviceCapabilityConstraints':
        if len(self.supported_sample_rates_hz) != len(set(self.supported_sample_rates_hz)):
            raise ValueError('supported sample rates must be unique')
        if any(rate <= 0 for rate in self.supported_sample_rates_hz):
            raise ValueError('supported sample rates must be positive')
        if len(self.supported_filter_types) != len(set(self.supported_filter_types)):
            raise ValueError('supported filter types must be unique')
        if len(self.allowed_physical_outputs) != len(set(self.allowed_physical_outputs)):
            raise ValueError('allowed physical outputs must be unique')
        if len(self.supported_crossover_orders) != len(set(self.supported_crossover_orders)):
            raise ValueError('supported crossover orders must be unique')
        for name in ('max_boost_db', 'max_cut_db', 'min_gain_db', 'max_gain_db', 'max_delay_s'):
            value = getattr(self, name)
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{name} must be finite')
        if (
            self.min_gain_db is not None
            and self.max_gain_db is not None
            and self.max_gain_db < self.min_gain_db
        ):
            raise ValueError('device gain range is invalid')
        return self


class CadCalibrationChannel(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel_id: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    source_entity_id: str = Field(min_length=1)
    physical_output_id: str = Field(min_length=1)
    sample_rate_hz: int = Field(gt=0)
    gain_db: float = 0.0
    delay_s: float = Field(default=0.0, ge=0.0)
    polarity: Literal['normal', 'inverted'] = 'normal'
    crossovers: tuple[CadCrossoverSetting, ...] = ()
    peq: tuple[CadBiquadFilter, ...] = ()
    routing: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_channel(self) -> 'CadCalibrationChannel':
        if not isfinite(float(self.gain_db)) or not isfinite(float(self.delay_s)):
            raise ValueError('channel gain and delay must be finite')
        filter_ids = tuple(item.filter_id for item in self.peq)
        if len(filter_ids) != len(set(filter_ids)):
            raise ValueError('channel filter ids must be unique')
        if any(item.sample_rate_hz != self.sample_rate_hz for item in self.peq):
            raise ValueError('channel biquad sample rate mismatch')
        if len(self.routing) != len(set(self.routing)):
            raise ValueError('channel routing entries must be unique')
        return self


class CadCalibrationPlan(BaseModel):
    """Immutable device-neutral calibration proposal over exact measurement authority."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = CALIBRATION_PLAN_SCHEMA_VERSION
    authority_version: Literal['calibration-plan-1'] = CALIBRATION_PLAN_AUTHORITY_VERSION
    plan_id: str = Field(min_length=1)
    plan_version: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    source_kind: CalibrationSourceKind

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str = Field(min_length=1)
    system_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    source_measurement_id: str = Field(min_length=1)
    source_measurement_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_dataset_id: str = Field(min_length=1)
    source_dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    measurement_quality_report_id: str = Field(min_length=1)
    measurement_quality_report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    sample_rate_hz: int = Field(gt=0)
    channels: tuple[CadCalibrationChannel, ...] = Field(min_length=1)
    target_curve: CadTargetCurve | None = None
    max_boost_db: float = Field(ge=0.0)
    max_cut_db: float = Field(ge=0.0)
    device_constraints: CadDeviceCapabilityConstraints

    support_state: CalibrationSupportState
    unsupported_reasons: tuple[str, ...] = ()
    plan_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_plan(self) -> 'CadCalibrationPlan':
        channel_ids = tuple(item.channel_id for item in self.channels)
        outputs = tuple(item.physical_output_id for item in self.channels)
        entities = tuple(item.source_entity_id for item in self.channels)
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError('calibration channel ids must be unique')
        if len(outputs) != len(set(outputs)):
            raise ValueError('physical output mapping must be one-to-one')
        if len(entities) != len(set(entities)):
            raise ValueError('source entity mapping must be one-to-one')
        if any(item.sample_rate_hz != self.sample_rate_hz for item in self.channels):
            raise ValueError('all calibration channels must use the plan sample rate')
        if self.support_state == 'SUPPORTED' and self.unsupported_reasons:
            raise ValueError('supported plan cannot carry unsupported reasons')
        if self.support_state == 'UNSUPPORTED' and not self.unsupported_reasons:
            raise ValueError('unsupported plan requires explicit reasons')
        if self.plan_semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('CalibrationPlan semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'plan_version': self.plan_version,
            'source_kind': self.source_kind,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'system_variant_id': self.system_variant_id,
            'system_variant_sha256': self.system_variant_sha256,
            'source_measurement_id': self.source_measurement_id,
            'source_measurement_sha256': self.source_measurement_sha256,
            'source_dataset_id': self.source_dataset_id,
            'source_dataset_sha256': self.source_dataset_sha256,
            'measurement_quality_report_id': self.measurement_quality_report_id,
            'measurement_quality_report_sha256': self.measurement_quality_report_sha256,
            'sample_rate_hz': self.sample_rate_hz,
            'channels': [item.model_dump(mode='json') for item in self.channels],
            'target_curve': None if self.target_curve is None else self.target_curve.model_dump(mode='json'),
            'max_boost_db': self.max_boost_db,
            'max_cut_db': self.max_cut_db,
            'device_constraints': self.device_constraints.model_dump(mode='json'),
            'support_state': self.support_state,
            'unsupported_reasons': list(self.unsupported_reasons),
        }


def _required_band(
    channels: Sequence[CadCalibrationChannel],
    target_curve: CadTargetCurve | None,
) -> tuple[float, float] | None:
    frequencies: list[float] = []
    for channel in channels:
        frequencies.extend(item.frequency_hz for item in channel.peq)
        frequencies.extend(item.frequency_hz for item in channel.crossovers)
    if target_curve is not None:
        frequencies.extend(item.frequency_hz for item in target_curve.points)
    if not frequencies:
        return None
    low = min(frequencies)
    high = max(frequencies)
    if low == high:
        low = max(1e-6, low * 0.99)
        high = high * 1.01
    return (low, high)


def _capability_reason(
    report: CadMeasurementQualityReport,
    claim: MeasurementCapabilityClaim,
    required_band_hz: tuple[float, float] | None = None,
) -> str | None:
    result = gate_measurement_claim(report, claim, required_band_hz=required_band_hz)
    if result.decision == 'ALLOWED':
        return None
    return f'{claim}: {result.decision}: ' + '; '.join(result.reasons)


def evaluate_calibration_support(
    *,
    quality_report: CadMeasurementQualityReport,
    sample_rate_hz: int,
    channels: Sequence[CadCalibrationChannel],
    target_curve: CadTargetCurve | None,
    max_boost_db: float,
    max_cut_db: float,
    device_constraints: CadDeviceCapabilityConstraints,
) -> tuple[CalibrationSupportState, tuple[str, ...]]:
    reasons: list[str] = []
    required_band = _required_band(channels, target_curve)

    uses_magnitude = target_curve is not None or any(
        channel.peq
        or channel.crossovers
        or abs(channel.gain_db) > 1e-12
        for channel in channels
    )
    if uses_magnitude:
        reason = _capability_reason(
            quality_report,
            'magnitude_response',
            required_band_hz=required_band,
        )
        if reason is not None:
            reasons.append(reason)

    if any(channel.delay_s > 1e-12 for channel in channels):
        reason = _capability_reason(quality_report, 'common_timing')
        if reason is not None:
            reasons.append(
                'absolute delay requires established common timing; ' + reason
            )

    if any(channel.polarity == 'inverted' for channel in channels):
        reason = _capability_reason(quality_report, 'polarity')
        if reason is not None:
            reasons.append('polarity change requires polarity authority; ' + reason)

    all_pass = tuple(
        item
        for channel in channels
        for item in channel.peq
        if item.filter_type == 'all_pass'
    )
    if all_pass:
        reasons.append(
            'all-pass correction is unsupported in calibration-plan-1 because '
            'coherent inter-channel phase correction authority is not established'
        )

    if sample_rate_hz not in device_constraints.supported_sample_rates_hz:
        reasons.append(f'device does not support sample rate {sample_rate_hz} Hz')

    for channel in channels:
        if (
            device_constraints.allowed_physical_outputs
            and channel.physical_output_id not in device_constraints.allowed_physical_outputs
        ):
            reasons.append(
                f'channel {channel.channel_id} maps to unsupported physical output '
                f'{channel.physical_output_id}'
            )
        if (
            device_constraints.max_filters_per_channel is None
            and channel.peq
        ):
            reasons.append('device filter-count capability is unknown')
        elif (
            device_constraints.max_filters_per_channel is not None
            and len(channel.peq) > device_constraints.max_filters_per_channel
        ):
            reasons.append(
                f'channel {channel.channel_id} filter count {len(channel.peq)} exceeds '
                f'device maximum {device_constraints.max_filters_per_channel}'
            )
        for filter_definition in channel.peq:
            if filter_definition.filter_type not in device_constraints.supported_filter_types:
                reasons.append(
                    f'filter {filter_definition.filter_id} type '
                    f'{filter_definition.filter_type} is unsupported by the device'
                )
            if filter_definition.filter_type == 'peaking':
                if filter_definition.gain_db > max_boost_db:
                    reasons.append(
                        f'filter {filter_definition.filter_id} boost '
                        f'{filter_definition.gain_db:g} dB exceeds plan maximum {max_boost_db:g} dB'
                    )
                if filter_definition.gain_db < -max_cut_db:
                    reasons.append(
                        f'filter {filter_definition.filter_id} cut '
                        f'{filter_definition.gain_db:g} dB exceeds plan maximum {-max_cut_db:g} dB'
                    )
                if (
                    device_constraints.max_boost_db is None
                    and filter_definition.gain_db > 0.0
                ):
                    reasons.append('device maximum boost capability is unknown')
                elif (
                    device_constraints.max_boost_db is not None
                    and filter_definition.gain_db > device_constraints.max_boost_db
                ):
                    reasons.append(
                        f'filter {filter_definition.filter_id} exceeds device maximum boost'
                    )
                if (
                    device_constraints.max_cut_db is None
                    and filter_definition.gain_db < 0.0
                ):
                    reasons.append('device maximum cut capability is unknown')
                elif (
                    device_constraints.max_cut_db is not None
                    and filter_definition.gain_db < -device_constraints.max_cut_db
                ):
                    reasons.append(
                        f'filter {filter_definition.filter_id} exceeds device maximum cut'
                    )
        if (
            device_constraints.min_gain_db is not None
            and channel.gain_db < device_constraints.min_gain_db
        ):
            reasons.append(f'channel {channel.channel_id} gain is below device minimum')
        if (
            device_constraints.max_gain_db is not None
            and channel.gain_db > device_constraints.max_gain_db
        ):
            reasons.append(f'channel {channel.channel_id} gain is above device maximum')
        if (
            device_constraints.max_delay_s is not None
            and channel.delay_s > device_constraints.max_delay_s
        ):
            reasons.append(f'channel {channel.channel_id} delay exceeds device maximum')
        for crossover in channel.crossovers:
            if (
                not device_constraints.supported_crossover_orders
                or crossover.filter_order not in device_constraints.supported_crossover_orders
            ):
                reasons.append(
                    f'channel {channel.channel_id} crossover order '
                    f'{crossover.filter_order} is unsupported or unknown'
                )

    unique = tuple(dict.fromkeys(reasons))
    return ('UNSUPPORTED', unique) if unique else ('SUPPORTED', ())


def build_calibration_plan(
    *,
    scene_revision: SceneRevision,
    system_variant: SystemVariant,
    measurement: CadMeasurementRecord,
    dataset: CadFrequencyResponseDataset,
    quality_report: CadMeasurementQualityReport,
    channels: Sequence[CadCalibrationChannel],
    sample_rate_hz: int,
    device_constraints: CadDeviceCapabilityConstraints,
    max_boost_db: float,
    max_cut_db: float,
    target_curve: CadTargetCurve | None = None,
    plan_id: str | None = None,
    plan_version: str = '1',
    created_at_utc: str,
    source_kind: CalibrationSourceKind = 'user',
) -> CadCalibrationPlan:
    if system_variant.document_id != scene_revision.document_id:
        raise ValueError('CalibrationPlan SceneRevision/SystemVariant document mismatch')
    if measurement.document_id != scene_revision.document_id:
        raise ValueError('CalibrationPlan measurement belongs to another document')
    if (
        measurement.scene_revision_id != scene_revision.revision_id
        or measurement.scene_content_hash != scene_revision.content_hash
    ):
        raise ValueError('CalibrationPlan source measurement is not bound to the exact SceneRevision')
    if dataset.measurement_id != measurement.measurement_id:
        raise ValueError('CalibrationPlan dataset does not belong to the source measurement')
    if (
        quality_report.measurement_id != measurement.measurement_id
        or quality_report.measurement_sha256 != measurement_sha256(measurement)
        or quality_report.dataset_id != dataset.dataset_id
        or quality_report.dataset_sha256 != dataset_sha256(dataset)
    ):
        raise ValueError('CalibrationPlan MeasurementQualityReport binding mismatch')

    channel_items = tuple(channels)
    support_state, unsupported_reasons = evaluate_calibration_support(
        quality_report=quality_report,
        sample_rate_hz=sample_rate_hz,
        channels=channel_items,
        target_curve=target_curve,
        max_boost_db=max_boost_db,
        max_cut_db=max_cut_db,
        device_constraints=device_constraints,
    )
    payload: dict[str, Any] = {
        'schema_version': CALIBRATION_PLAN_SCHEMA_VERSION,
        'authority_version': CALIBRATION_PLAN_AUTHORITY_VERSION,
        'plan_id': plan_id or str(uuid4()),
        'plan_version': plan_version,
        'created_at_utc': created_at_utc,
        'source_kind': source_kind,
        'document_id': scene_revision.document_id,
        'scene_revision_id': scene_revision.revision_id,
        'scene_content_hash': scene_revision.content_hash,
        'system_variant_id': system_variant.variant_id,
        'system_variant_sha256': system_variant.variant_sha256,
        'source_measurement_id': measurement.measurement_id,
        'source_measurement_sha256': measurement_sha256(measurement),
        'source_dataset_id': dataset.dataset_id,
        'source_dataset_sha256': dataset_sha256(dataset),
        'measurement_quality_report_id': quality_report.report_id,
        'measurement_quality_report_sha256': quality_report.report_sha256,
        'sample_rate_hz': sample_rate_hz,
        'channels': channel_items,
        'target_curve': target_curve,
        'max_boost_db': max_boost_db,
        'max_cut_db': max_cut_db,
        'device_constraints': device_constraints,
        'support_state': support_state,
        'unsupported_reasons': unsupported_reasons,
    }
    provisional = CadCalibrationPlan.model_construct(
        **payload,
        plan_semantic_sha256='0' * 64,
    )
    return CadCalibrationPlan(
        **payload,
        plan_semantic_sha256=_hash(provisional.semantic_payload()),
    )


class CadExportedChannelSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel_id: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    source_entity_id: str = Field(min_length=1)
    physical_output_id: str = Field(min_length=1)
    gain_db: float
    delay_s: float = Field(ge=0.0)
    polarity: Literal['normal', 'inverted']
    crossovers: tuple[CadCrossoverSetting, ...]
    peq: tuple[CadBiquadFilter, ...]
    routing: tuple[str, ...]


class CadCalibrationExportSnapshot(BaseModel):
    """Exact settings emitted by one deterministic generic adapter run."""

    model_config = ConfigDict(frozen=True)

    export_id: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    adapter_id: Literal['htdt-generic-biquad'] = GENERIC_BIQUAD_ADAPTER_ID
    adapter_version: Literal['1'] = GENERIC_BIQUAD_ADAPTER_VERSION
    format_id: Literal['generic-biquad-json-csv-1'] = 'generic-biquad-json-csv-1'
    state: Literal['exported'] = 'exported'
    calibration_plan_id: str = Field(min_length=1)
    requested_plan_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sample_rate_hz: int = Field(gt=0)
    channels: tuple[CadExportedChannelSettings, ...]
    quantization_applied: bool
    quantization_notes: tuple[str, ...]
    exported_settings_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_export(self) -> 'CadCalibrationExportSnapshot':
        if self.exported_settings_semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('exported settings semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'format_id': self.format_id,
            'calibration_plan_id': self.calibration_plan_id,
            'requested_plan_semantic_sha256': self.requested_plan_semantic_sha256,
            'sample_rate_hz': self.sample_rate_hz,
            'channels': [item.model_dump(mode='json') for item in self.channels],
            'quantization_applied': self.quantization_applied,
            'quantization_notes': list(self.quantization_notes),
        }


def build_generic_biquad_export(
    plan: CadCalibrationPlan,
    *,
    export_id: str | None = None,
    created_at_utc: str,
) -> CadCalibrationExportSnapshot:
    if plan.support_state != 'SUPPORTED':
        raise ValueError(
            'unsupported CalibrationPlan cannot be exported: '
            + '; '.join(plan.unsupported_reasons)
        )

    constraints = plan.device_constraints
    notes: list[str] = []
    exported_channels: list[CadExportedChannelSettings] = []
    for channel in plan.channels:
        gain = _quantize(channel.gain_db, constraints.channel_gain_resolution_db)
        delay = _quantize(channel.delay_s, constraints.delay_resolution_s)
        if gain != channel.gain_db:
            notes.append(f'{channel.channel_id}: channel gain quantized {channel.gain_db:g}->{gain:g} dB')
        if delay != channel.delay_s:
            notes.append(f'{channel.channel_id}: delay quantized {channel.delay_s:g}->{delay:g} s')

        exported_filters: list[CadBiquadFilter] = []
        for filter_definition in channel.peq:
            frequency = _quantize(
                filter_definition.frequency_hz,
                constraints.frequency_resolution_hz,
            )
            q = _quantize(filter_definition.q, constraints.q_resolution)
            filter_gain = _quantize(
                filter_definition.gain_db,
                constraints.filter_gain_resolution_db,
            )
            if frequency <= 0.0 or frequency >= plan.sample_rate_hz / 2 or q <= 0.0:
                raise ValueError('quantized biquad parameter is outside the valid domain')
            if filter_gain > plan.max_boost_db or filter_gain < -plan.max_cut_db:
                raise ValueError('quantized biquad gain exceeds plan boost/cut constraint')
            if (
                constraints.max_boost_db is not None
                and filter_gain > constraints.max_boost_db
            ):
                raise ValueError('quantized biquad gain exceeds device maximum boost')
            if (
                constraints.max_cut_db is not None
                and filter_gain < -constraints.max_cut_db
            ):
                raise ValueError('quantized biquad gain exceeds device maximum cut')
            if (
                frequency != filter_definition.frequency_hz
                or q != filter_definition.q
                or filter_gain != filter_definition.gain_db
            ):
                notes.append(
                    f'{channel.channel_id}/{filter_definition.filter_id}: '
                    f'filter parameters quantized'
                )
            exported_filters.append(build_biquad_filter(
                filter_id=filter_definition.filter_id,
                filter_type=filter_definition.filter_type,
                frequency_hz=frequency,
                q=q,
                gain_db=filter_gain,
                sample_rate_hz=plan.sample_rate_hz,
            ))

        exported_channels.append(CadExportedChannelSettings(
            channel_id=channel.channel_id,
            role_id=channel.role_id,
            source_entity_id=channel.source_entity_id,
            physical_output_id=channel.physical_output_id,
            gain_db=gain,
            delay_s=delay,
            polarity=channel.polarity,
            crossovers=channel.crossovers,
            peq=tuple(exported_filters),
            routing=channel.routing,
        ))

    payload: dict[str, Any] = {
        'export_id': export_id or str(uuid4()),
        'created_at_utc': created_at_utc,
        'adapter_id': GENERIC_BIQUAD_ADAPTER_ID,
        'adapter_version': GENERIC_BIQUAD_ADAPTER_VERSION,
        'format_id': 'generic-biquad-json-csv-1',
        'state': 'exported',
        'calibration_plan_id': plan.plan_id,
        'requested_plan_semantic_sha256': plan.plan_semantic_sha256,
        'sample_rate_hz': plan.sample_rate_hz,
        'channels': tuple(exported_channels),
        'quantization_applied': bool(notes),
        'quantization_notes': tuple(notes),
    }
    provisional = CadCalibrationExportSnapshot.model_construct(
        **payload,
        exported_settings_semantic_sha256='0' * 64,
    )
    return CadCalibrationExportSnapshot(
        **payload,
        exported_settings_semantic_sha256=_hash(provisional.semantic_payload()),
    )


def render_generic_biquad_json(snapshot: CadCalibrationExportSnapshot) -> str:
    return _canonical_json(snapshot.model_dump(mode='json')) + '\n'


def read_generic_biquad_json(text: str) -> CadCalibrationExportSnapshot:
    return CadCalibrationExportSnapshot.model_validate(json.loads(text))


def render_generic_biquad_csv(snapshot: CadCalibrationExportSnapshot) -> str:
    """Render a human/spreadsheet-facing CSV export of exported biquad settings.

    Every row is routed through :func:`csv_safe_row` so channel/output/filter
    identifiers are neutralized against spreadsheet formula interpretation
    (see ``htdt.csv_export`` for the single-quote escaping convention).
    """

    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(csv_safe_row([
        'channel_id',
        'role_id',
        'physical_output_id',
        'channel_gain_db',
        'delay_s',
        'polarity',
        'filter_index',
        'filter_id',
        'filter_type',
        'frequency_hz',
        'q',
        'filter_gain_db',
        'b0',
        'b1',
        'b2',
        'a1',
        'a2',
    ]))
    for channel in snapshot.channels:
        if not channel.peq:
            writer.writerow(csv_safe_row([
                channel.channel_id,
                channel.role_id,
                channel.physical_output_id,
                channel.gain_db,
                channel.delay_s,
                channel.polarity,
                '',
                '',
                '',
                '',
                '',
                '',
                '',
                '',
                '',
                '',
                '',
            ]))
            continue
        for index, item in enumerate(channel.peq):
            writer.writerow(csv_safe_row([
                channel.channel_id,
                channel.role_id,
                channel.physical_output_id,
                channel.gain_db,
                channel.delay_s,
                channel.polarity,
                index,
                item.filter_id,
                item.filter_type,
                item.frequency_hz,
                item.q,
                item.gain_db,
                *item.coefficients,
            ]))
    return buffer.getvalue()


class CadVerificationMeasurementPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    point_id: str = Field(min_length=1)
    position: Position3


class CadVerificationMeasurementPlan(BaseModel):
    """Immutable re-measure contract bound to exact exported settings."""

    model_config = ConfigDict(frozen=True)

    verification_plan_id: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    calibration_plan_id: str = Field(min_length=1)
    calibration_plan_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    exported_settings_id: str = Field(min_length=1)
    exported_settings_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str = Field(min_length=1)
    system_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    measurement_points: tuple[CadVerificationMeasurementPoint, ...] = Field(min_length=1)
    routing: tuple[str, ...] = Field(min_length=1)
    reference_level_db_spl: float
    required_measurement_capabilities: tuple[MeasurementCapabilityClaim, ...] = Field(min_length=1)
    before_measurement_ids: tuple[str, ...] = Field(min_length=1)
    after_measurement_ids: tuple[str, ...] = ()
    verification_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_verification(self) -> 'CadVerificationMeasurementPlan':
        if not isfinite(float(self.reference_level_db_spl)):
            raise ValueError('verification reference level must be finite')
        for values, name in (
            (tuple(item.point_id for item in self.measurement_points), 'measurement point ids'),
            (self.routing, 'routing'),
            (self.required_measurement_capabilities, 'required measurement capabilities'),
            (self.before_measurement_ids, 'before measurement ids'),
            (self.after_measurement_ids, 'after measurement ids'),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f'verification {name} must be unique')
        if set(self.before_measurement_ids).intersection(self.after_measurement_ids):
            raise ValueError('before/after verification measurements must be distinct')
        if self.verification_semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('VerificationMeasurementPlan semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'calibration_plan_id': self.calibration_plan_id,
            'calibration_plan_semantic_sha256': self.calibration_plan_semantic_sha256,
            'exported_settings_id': self.exported_settings_id,
            'exported_settings_semantic_sha256': self.exported_settings_semantic_sha256,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'system_variant_id': self.system_variant_id,
            'system_variant_sha256': self.system_variant_sha256,
            'measurement_points': [item.model_dump(mode='json') for item in self.measurement_points],
            'routing': list(self.routing),
            'reference_level_db_spl': self.reference_level_db_spl,
            'required_measurement_capabilities': list(self.required_measurement_capabilities),
            'before_measurement_ids': list(self.before_measurement_ids),
            'after_measurement_ids': list(self.after_measurement_ids),
        }


def build_verification_measurement_plan(
    *,
    plan: CadCalibrationPlan,
    exported_settings: CadCalibrationExportSnapshot,
    measurement_points: Sequence[CadVerificationMeasurementPoint],
    routing: Sequence[str],
    reference_level_db_spl: float,
    required_measurement_capabilities: Sequence[MeasurementCapabilityClaim],
    before_measurement_ids: Sequence[str],
    after_measurement_ids: Sequence[str] = (),
    verification_plan_id: str | None = None,
    created_at_utc: str,
) -> CadVerificationMeasurementPlan:
    if exported_settings.calibration_plan_id != plan.plan_id:
        raise ValueError('verification export does not belong to CalibrationPlan')
    if exported_settings.requested_plan_semantic_sha256 != plan.plan_semantic_sha256:
        raise ValueError('verification export/CalibrationPlan hash mismatch')
    payload: dict[str, Any] = {
        'verification_plan_id': verification_plan_id or str(uuid4()),
        'created_at_utc': created_at_utc,
        'calibration_plan_id': plan.plan_id,
        'calibration_plan_semantic_sha256': plan.plan_semantic_sha256,
        'exported_settings_id': exported_settings.export_id,
        'exported_settings_semantic_sha256': exported_settings.exported_settings_semantic_sha256,
        'document_id': plan.document_id,
        'scene_revision_id': plan.scene_revision_id,
        'scene_content_hash': plan.scene_content_hash,
        'system_variant_id': plan.system_variant_id,
        'system_variant_sha256': plan.system_variant_sha256,
        'measurement_points': tuple(measurement_points),
        'routing': tuple(routing),
        'reference_level_db_spl': reference_level_db_spl,
        'required_measurement_capabilities': tuple(required_measurement_capabilities),
        'before_measurement_ids': tuple(before_measurement_ids),
        'after_measurement_ids': tuple(after_measurement_ids),
    }
    provisional = CadVerificationMeasurementPlan.model_construct(
        **payload,
        verification_semantic_sha256='0' * 64,
    )
    return CadVerificationMeasurementPlan(
        **payload,
        verification_semantic_sha256=_hash(provisional.semantic_payload()),
    )


class CadCalibrationLifecycleEvent(BaseModel):
    """Append-only lifecycle fact; export never implies application or validation."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    calibration_plan_id: str = Field(min_length=1)
    calibration_plan_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    state: CalibrationLifecycleState
    exported_settings_id: str | None = Field(default=None, min_length=1)
    exported_settings_semantic_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    verification_plan_id: str | None = Field(default=None, min_length=1)
    verification_plan_semantic_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    measurement_ids: tuple[str, ...] = ()
    note: str | None = Field(default=None, min_length=1)
    event_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_event(self) -> 'CadCalibrationLifecycleEvent':
        export_pair = (
            self.exported_settings_id is not None,
            self.exported_settings_semantic_sha256 is not None,
        )
        verification_pair = (
            self.verification_plan_id is not None,
            self.verification_plan_semantic_sha256 is not None,
        )
        if export_pair[0] != export_pair[1]:
            raise ValueError('lifecycle export id/hash must be supplied together')
        if verification_pair[0] != verification_pair[1]:
            raise ValueError('lifecycle verification id/hash must be supplied together')
        if self.state in {'exported', 'user_applied', 'remeasured', 'validated'} and not export_pair[0]:
            raise ValueError(f'{self.state} lifecycle requires exact exported settings')
        if self.state in {'remeasured', 'validated'} and not verification_pair[0]:
            raise ValueError(f'{self.state} lifecycle requires exact verification plan')
        if self.state in {'remeasured', 'validated'} and not self.measurement_ids:
            raise ValueError(f'{self.state} lifecycle requires re-measurement ids')
        if len(self.measurement_ids) != len(set(self.measurement_ids)):
            raise ValueError('lifecycle measurement ids must be unique')
        if self.event_semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('Calibration lifecycle event hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'calibration_plan_id': self.calibration_plan_id,
            'calibration_plan_semantic_sha256': self.calibration_plan_semantic_sha256,
            'state': self.state,
            'exported_settings_id': self.exported_settings_id,
            'exported_settings_semantic_sha256': self.exported_settings_semantic_sha256,
            'verification_plan_id': self.verification_plan_id,
            'verification_plan_semantic_sha256': self.verification_plan_semantic_sha256,
            'measurement_ids': list(self.measurement_ids),
            'note': self.note,
        }


def build_calibration_lifecycle_event(
    *,
    plan: CadCalibrationPlan,
    state: CalibrationLifecycleState,
    exported_settings: CadCalibrationExportSnapshot | None = None,
    verification_plan: CadVerificationMeasurementPlan | None = None,
    measurement_ids: Sequence[str] = (),
    event_id: str | None = None,
    created_at_utc: str,
    note: str | None = None,
) -> CadCalibrationLifecycleEvent:
    payload: dict[str, Any] = {
        'event_id': event_id or str(uuid4()),
        'created_at_utc': created_at_utc,
        'calibration_plan_id': plan.plan_id,
        'calibration_plan_semantic_sha256': plan.plan_semantic_sha256,
        'state': state,
        'exported_settings_id': None if exported_settings is None else exported_settings.export_id,
        'exported_settings_semantic_sha256': (
            None if exported_settings is None else exported_settings.exported_settings_semantic_sha256
        ),
        'verification_plan_id': None if verification_plan is None else verification_plan.verification_plan_id,
        'verification_plan_semantic_sha256': (
            None if verification_plan is None else verification_plan.verification_semantic_sha256
        ),
        'measurement_ids': tuple(measurement_ids),
        'note': note,
    }
    provisional = CadCalibrationLifecycleEvent.model_construct(
        **payload,
        event_semantic_sha256='0' * 64,
    )
    return CadCalibrationLifecycleEvent(
        **payload,
        event_semantic_sha256=_hash(provisional.semantic_payload()),
    )
