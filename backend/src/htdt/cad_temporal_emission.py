"""Display temporal emission & motion-quality authority (#1030).

Distinct from #1005 input latency: that issue asks *when* the response
begins; this authority records *what temporal waveform, transition,
persistence and modulation follows* — flicker/PWM, VRR luminance
modulation, pixel step response, dropped/repeated frames, jitter and
color-sequential emission are separate metrics, never one universal
"flicker" or "rainbow" score.

Authorities:

- :class:`DisplayTemporalCondition` — exact operating state: display
  instance, firmware, resolution/refresh (fixed vs VRR + range +
  effective rate + cadence), HDR family, picture mode, BFI/strobe,
  local dimming, backlight and light-source levels, motion
  interpolation, ALLM, tone-map, warmup, test pattern.
- :class:`TemporalLightWaveform` — replayable evidence: detector,
  bandwidth/sample-rate, position, stimulus, time origin, samples or a
  hashed asset, optional per-channel capture.
- :class:`TemporalMetricResult` — derived metrics, each bound to a
  waveform and carrying an explicit ``metric_version``.

Honesty rules:

- Detector bandwidth/sample rate are persisted — PWM frequency or fast
  GtG is never reported beyond instrument capability.
- Static-refresh flicker and VRR luminance modulation are separate
  metric kinds; one 60 Hz result never characterizes a 40–120 Hz range.
- Observed waveform is stored first; PWM/scanning/BFI topology is never
  inferred from the waveform alone.
- Simultaneous RGB channels are only claimed when the waveform carries
  channel identity.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance


def _hash(payload) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


WaveformChannel = Literal['luminance', 'red', 'green', 'blue']
RefreshKind = Literal['fixed', 'vrr', 'unknown']
BfiState = Literal[
    'off', 'bfi', 'strobe', 'rolling_strobe', 'global_strobe', 'unknown'
]
TemporalMetricKind = Literal[
    'modulation_depth',
    'dominant_frequency',
    'flicker_visibility',
    'gtg_rise',
    'gtg_fall',
    'overshoot',
    'settling_time',
    'dropped_frames',
    'repeated_frames',
    'jitter',
    'vrr_luminance_modulation',
    'color_sequential',
]

# Versioned derived-metric identities (mandatory on results).
MODULATION_DEPTH_VERSION = 'modulation-depth-v1'
DOMINANT_FREQUENCY_VERSION = 'dft-dominant-frequency-v1'
STEP_RESPONSE_VERSION = 'step-10-90-settling-v1'


class DisplayTemporalCondition(BaseModel):
    """Exact temporal measurement condition (#1030 §2)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['display-temporal-condition-1'] = (
        'display-temporal-condition-1'
    )
    condition_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    display_instance_id: str | None = None
    display_specification_id: str | None = None
    display_specification_version: str | None = None
    display_specification_sha256: str | None = None
    firmware: str | None = None
    input_resolution_w: int | None = Field(default=None, gt=0)
    input_resolution_h: int | None = Field(default=None, gt=0)
    refresh_hz: float | None = Field(default=None, gt=0.0)
    refresh_kind: RefreshKind = 'unknown'
    vrr_min_hz: float | None = Field(default=None, gt=0.0)
    vrr_max_hz: float | None = Field(default=None, gt=0.0)
    vrr_effective_hz: float | None = Field(default=None, gt=0.0)
    frame_cadence: str | None = None
    hdr_format_family: str | None = None
    picture_mode: str | None = None
    bfi_state: BfiState = 'unknown'
    local_dimming_state: str | None = None
    backlight_level_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    light_source_level: str | None = None
    motion_interpolation_mode: str | None = None
    allm_state: str | None = None
    tone_map_mode: str | None = None
    presentation_profile_id: str | None = None
    warmup_minutes: float | None = Field(default=None, ge=0.0)
    test_pattern: str | None = None
    condition_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'condition_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'DisplayTemporalCondition':
        triple = (
            self.display_specification_id,
            self.display_specification_version,
            self.display_specification_sha256,
        )
        if (None in triple) and any(v is not None for v in triple):
            raise ValueError(
                'display specification id/version/sha256 must be '
                'supplied together or not at all'
            )
        if self.refresh_kind == 'vrr' and (
            self.vrr_min_hz is None or self.vrr_max_hz is None
        ):
            raise ValueError(
                'vrr refresh_kind requires vrr_min_hz and vrr_max_hz'
            )
        if (
            self.vrr_effective_hz is not None
            and self.vrr_min_hz is not None
            and self.vrr_max_hz is not None
            and not self.vrr_min_hz <= self.vrr_effective_hz <= self.vrr_max_hz
        ):
            raise ValueError(
                'vrr_effective_hz must lie inside [vrr_min_hz, vrr_max_hz]'
            )
        expected = _hash(self.semantic_payload())
        if expected != self.condition_sha256:
            raise ValueError('display temporal condition hash mismatch')
        return self


class TemporalLightWaveform(BaseModel):
    """Replayable temporal light evidence (#1030 §3-4).

    Carries inline ``samples`` or a hashed acquisition asset (or both);
    the sample rate and detector bandwidth bound every derived metric.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['temporal-light-waveform-1'] = (
        'temporal-light-waveform-1'
    )
    waveform_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    condition_version: str = Field(min_length=1)
    condition_sha256: str = Field(min_length=16)
    detector: str | None = None
    detector_bandwidth_hz: float | None = Field(default=None, gt=0.0)
    sample_rate_hz: float | None = Field(default=None, gt=0.0)
    sample_position: str | None = None
    stimulus: str | None = None
    time_origin: str | None = None
    duration_seconds: float | None = Field(default=None, gt=0.0)
    channel: WaveformChannel = 'luminance'
    samples: tuple[float, ...] = ()
    asset_sha256: str | None = None
    producer: str | None = None
    producer_version: str | None = None
    measured_at_utc: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    waveform_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'waveform_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'TemporalLightWaveform':
        if not self.samples and self.asset_sha256 is None:
            raise ValueError(
                'waveform requires inline samples or an asset_sha256 — '
                'derived metrics must reference replayable evidence'
            )
        if self.samples and self.sample_rate_hz is None:
            raise ValueError(
                'inline samples require sample_rate_hz'
            )
        expected = _hash(self.semantic_payload())
        if expected != self.waveform_sha256:
            raise ValueError('temporal light waveform hash mismatch')
        return self


class TemporalMetricResult(BaseModel):
    """One derived temporal metric, bound to its source waveform."""

    model_config = ConfigDict(frozen=True)

    metric_kind: TemporalMetricKind
    metric_version: str = Field(min_length=1)
    waveform_id: str = Field(min_length=1)
    value: float
    unit: str = Field(min_length=1)
    start_level_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    end_level_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    note: str | None = None


class TemporalEmissionMeasurement(BaseModel):
    """Waveforms + derived metrics under one exact condition."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['temporal-emission-measurement-1'] = (
        'temporal-emission-measurement-1'
    )
    measurement_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    condition_version: str = Field(min_length=1)
    condition_sha256: str = Field(min_length=16)
    instrument: str | None = None
    method_note: str | None = None
    waveforms: tuple[TemporalLightWaveform, ...] = ()
    metrics: tuple[TemporalMetricResult, ...] = ()
    measured_at_utc: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    measurement_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'measurement_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'TemporalEmissionMeasurement':
        waveform_ids = {w.waveform_id for w in self.waveforms}
        for metric in self.metrics:
            if metric.waveform_id not in waveform_ids:
                raise ValueError(
                    f'metric {metric.metric_kind} references waveform '
                    f'{metric.waveform_id!r} not in this measurement — '
                    'derived metrics must reference the waveform'
                )
        expected = _hash(self.semantic_payload())
        if expected != self.measurement_sha256:
            raise ValueError(
                'temporal emission measurement hash mismatch'
            )
        return self


def derive_modulation_depth(
    waveform: TemporalLightWaveform,
) -> TemporalMetricResult | None:
    """``modulation-depth-v1`` = (max − min)/(max + min) over the samples."""
    if not waveform.samples:
        return None
    lo = min(waveform.samples)
    hi = max(waveform.samples)
    if hi + lo <= 0.0:
        return None
    return TemporalMetricResult(
        metric_kind='modulation_depth',
        metric_version=MODULATION_DEPTH_VERSION,
        waveform_id=waveform.waveform_id,
        value=(hi - lo) / (hi + lo),
        unit='fraction',
    )


def _next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p <<= 1
    return p


def _fft_magnitudes(samples: tuple[float, ...]) -> list[float]:
    """Radix-2 DFT magnitudes (DC removed) via recursive FFT."""
    n = _next_pow2(len(samples))
    mean = sum(samples) / len(samples)
    padded = [v - mean for v in samples] + [0.0] * (n - len(samples))

    def fft(x: list[complex]) -> list[complex]:
        m = len(x)
        if m <= 1:
            return x
        even = fft(x[0::2])
        odd = fft(x[1::2])
        out = [0j] * m
        for k in range(m // 2):
            t = complex(
                math.cos(-2.0 * math.pi * k / m),
                math.sin(-2.0 * math.pi * k / m),
            ) * odd[k]
            out[k] = even[k] + t
            out[k + m // 2] = even[k] - t
        return out

    spectrum = fft([complex(v) for v in padded])
    return [abs(c) for c in spectrum[: n // 2]]


def derive_dominant_frequency_hz(
    waveform: TemporalLightWaveform,
) -> TemporalMetricResult | None:
    """``dft-dominant-frequency-v1`` — strongest non-DC DFT bin."""
    if not waveform.samples or waveform.sample_rate_hz is None:
        return None
    if len(waveform.samples) < 4:
        return None
    mags = _fft_magnitudes(waveform.samples)
    if not mags or max(mags) <= 0.0:
        return None
    bin_hz = waveform.sample_rate_hz / _next_pow2(len(waveform.samples))
    peak_bin = mags.index(max(mags[1:]) if len(mags) > 1 else mags[0])
    if peak_bin == 0:
        peak_bin = 1
    return TemporalMetricResult(
        metric_kind='dominant_frequency',
        metric_version=DOMINANT_FREQUENCY_VERSION,
        waveform_id=waveform.waveform_id,
        value=peak_bin * bin_hz,
        unit='hz',
    )


def derive_step_response(
    waveform: TemporalLightWaveform,
    *,
    settle_band_fraction: float = 0.02,
) -> tuple[TemporalMetricResult, ...]:
    """``step-10-90-settling-v1``: 10–90 % rise time, overshoot and
    settling time from a step waveform (first sustained transition)."""
    if not waveform.samples or waveform.sample_rate_hz is None:
        return ()
    s = waveform.samples
    lo = min(s)
    hi = max(s)
    span = hi - lo
    if span <= 0.0:
        return ()
    t10 = lo + 0.1 * span
    t90 = lo + 0.9 * span

    def _cross(threshold: float) -> float | None:
        for i in range(1, len(s)):
            if s[i - 1] < threshold <= s[i]:
                frac = (threshold - s[i - 1]) / (s[i] - s[i - 1])
                return (i - 1 + frac) / waveform.sample_rate_hz
        return None

    rise = None
    c10 = _cross(t10)
    c90 = _cross(t90)
    if c10 is not None and c90 is not None and c90 >= c10:
        rise = c90 - c10
    overshoot = max(0.0, (max(s) - hi) / span) if rise is not None else None
    settle = None
    if rise is not None:
        band = hi * settle_band_fraction if hi > 0 else settle_band_fraction
        last_out = None
        for i, v in enumerate(s):
            if abs(v - hi) > band:
                last_out = i
        if last_out is not None and c10 is not None:
            settle = max(0.0, last_out / waveform.sample_rate_hz - c90)
    results: list[TemporalMetricResult] = []
    if rise is not None:
        results.append(
            TemporalMetricResult(
                metric_kind='gtg_rise',
                metric_version=STEP_RESPONSE_VERSION,
                waveform_id=waveform.waveform_id,
                value=rise,
                unit='seconds',
            )
        )
    if overshoot is not None:
        results.append(
            TemporalMetricResult(
                metric_kind='overshoot',
                metric_version=STEP_RESPONSE_VERSION,
                waveform_id=waveform.waveform_id,
                value=overshoot,
                unit='fraction',
            )
        )
    if settle is not None:
        results.append(
            TemporalMetricResult(
                metric_kind='settling_time',
                metric_version=STEP_RESPONSE_VERSION,
                waveform_id=waveform.waveform_id,
                value=settle,
                unit='seconds',
            )
        )
    return tuple(results)


def build_display_temporal_condition(**kwargs) -> DisplayTemporalCondition:
    probe = DisplayTemporalCondition.model_construct(
        condition_sha256='x' * 64, **kwargs
    )
    digest = _hash(probe.semantic_payload())
    return DisplayTemporalCondition(
        **probe.model_dump(
            mode='python', exclude={'condition_sha256', 'schema_version'}
        ),
        condition_sha256=digest,
    )


def build_temporal_light_waveform(
    *, condition: DisplayTemporalCondition, **kwargs
) -> TemporalLightWaveform:
    probe = TemporalLightWaveform.model_construct(
        condition_id=condition.condition_id,
        condition_version=condition.version,
        condition_sha256=condition.condition_sha256,
        waveform_sha256='x' * 64,
        **kwargs,
    )
    digest = _hash(probe.semantic_payload())
    return TemporalLightWaveform(
        **probe.model_dump(
            mode='python', exclude={'waveform_sha256', 'schema_version'}
        ),
        waveform_sha256=digest,
    )


def build_temporal_emission_measurement(
    *, condition: DisplayTemporalCondition, **kwargs
) -> TemporalEmissionMeasurement:
    probe = TemporalEmissionMeasurement.model_construct(
        condition_id=condition.condition_id,
        condition_version=condition.version,
        condition_sha256=condition.condition_sha256,
        measurement_sha256='x' * 64,
        **kwargs,
    )
    digest = _hash(probe.semantic_payload())
    return TemporalEmissionMeasurement(
        **probe.model_dump(
            mode='python', exclude={'measurement_sha256', 'schema_version'}
        ),
        measurement_sha256=digest,
    )
