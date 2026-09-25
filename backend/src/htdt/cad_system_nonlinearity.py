"""Installed-system nonlinear measurement & commissioning authority (#969).

``EquipmentDefinition`` nonlinear profiles (issue #648) describe what a
*source* can do in principle. This module owns the materially different
authority: **measured nonlinear behavior of the installed playback chain**
at the listening position — loudspeaker + amplifier + crossover/DSP +
bass management + room + microphone, under one exact stimulus and level.

A ``SystemNonlinearityMeasurement`` is immutable evidence sealed by a
SHA-256 of its ``identity_payload``, bound to the exact measurement /
dataset / scene revision / acquisition context / stimulus authorities it
was derived under. Distortion metrics keep their exact producer semantics
(harmonic levels, ratios, THD, THD+N/TD+N, noise floor, analysis
bandwidth, PPO); unsupported quantities remain absent — never zero-filled.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_finite(values: tuple[float, ...], label: str) -> None:
    if any(not isfinite(float(value)) for value in values):
        raise ValueError(f'{label} must be finite')


def _require_unique_non_empty(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)) or any(not item for item in values):
        raise ValueError(f'{label} must be unique non-empty values')


# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

NonlinearityMethod = Literal[
    'sweep_distortion',
    'stepped_sine',
    'ramped_level',
    'rta_distortion',
    'imported',
    'unknown',
]
"""How the nonlinear evidence was acquired.

- ``sweep_distortion``: single swept measurement with harmonic analysis;
- ``stepped_sine``: discrete stepped-sine acquisition at multiple levels
  and/or frequencies;
- ``ramped_level``: continuously ramped excitation level;
- ``rta_distortion``: RTA-based harmonic/intermodulation analysis;
- ``imported``: producer-supplied result where the acquisition method is
  asserted by the producer rather than observed;
- ``unknown``: method not evidenced.
"""

DistortionMetricKind = Literal[
    'harmonic_level',
    'harmonic_ratio',
    'harmonic_percent',
    'thd',
    'thd_n',
    'td_n',
    'n_d',
    'noise_only',
    'fundamental',
    'unknown',
]
"""Exact metric semantics; a THD value is never interchangeable with
THD+N/TD+N, and a harmonic level in dB SPL is never silently converted
into a ratio or percent."""

DistortionMetricUnit = Literal[
    'db_spl',
    'dbc',
    'dbfs',
    'percent',
    'ratio',
    'unknown',
]

NonlinearityQuality = Literal[
    'valid',
    'limited',
    'clipping_suspected',
    'noise_limited',
    'unsupported',
    'unknown',
]

SystemNonlinearityEvidenceKind = Literal[
    'installed_system_measurement',
    'unknown',
]
"""This authority never represents a source-capability profile."""


class CadDistortionSample(BaseModel):
    """One distortion observable on the retained axes.

    ``harmonic_order`` is the absolute harmonic index when the metric is
    harmonic-scoped (``harmonic_level``/``harmonic_ratio``/
    ``harmonic_percent``); ``None`` for aggregate metrics such as THD.
    """

    model_config = ConfigDict(frozen=True)

    metric: DistortionMetricKind
    unit: DistortionMetricUnit
    frequency_hz: tuple[float, ...] = ()
    level_axis_db: tuple[float, ...] | None = None
    harmonic_order: int | None = Field(default=None, ge=1)
    values: tuple[float, ...] = ()

    @model_validator(mode='after')
    def valid_sample(self) -> 'CadDistortionSample':
        if self.metric == 'unknown':
            raise ValueError('distortion sample requires explicit metric semantics')
        if self.unit == 'unknown':
            raise ValueError('distortion sample requires explicit unit semantics')
        if not self.frequency_hz:
            raise ValueError('distortion sample requires a frequency axis')
        if not self.values:
            raise ValueError('distortion sample requires values')
        expected = len(self.frequency_hz) * (
            len(self.level_axis_db) if self.level_axis_db is not None else 1
        )
        if len(self.values) != expected:
            raise ValueError(
                'values length must equal len(frequency_hz) * '
                'len(level_axis_db or (1,))'
            )
        _require_finite(self.frequency_hz, 'frequency_hz')
        _require_finite(self.values, 'values')
        if self.level_axis_db is not None:
            _require_finite(self.level_axis_db, 'level_axis_db')
        if any(frequency <= 0 for frequency in self.frequency_hz):
            raise ValueError('frequency_hz must be positive')
        if self.metric in ('harmonic_level', 'harmonic_ratio', 'harmonic_percent'):
            if self.harmonic_order is None:
                raise ValueError('harmonic metrics require harmonic_order')
        elif self.harmonic_order is not None:
            raise ValueError('harmonic_order is only valid for harmonic metrics')
        if self.unit == 'percent' and any(v < 0 for v in self.values):
            raise ValueError('percent distortion values must be non-negative')
        if self.unit in ('dbc', 'ratio') and self.metric == 'harmonic_level':
            raise ValueError('harmonic_level requires an absolute unit')
        return self


class SystemNonlinearityMeasurement(BaseModel):
    """Immutable measured-nonlinearity evidence for the installed system.

    The ``semantic_sha256`` seal covers the whole interpretation: the exact
    measurement/dataset it derives from, the scene/system-variant baseline,
    the acquisition context and stimulus authorities, the retained axes,
    every distortion sample set, the analysis semantics that produced them,
    and the provenance chain — so a persisted record cannot be reinterpreted
    against different evidence.
    """

    model_config = ConfigDict(frozen=True)

    result_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    dataset_id: str | None = None
    dataset_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    system_variant_id: str | None = None
    channel_role: str | None = None
    source_speaker_ids: tuple[str, ...] = ()
    listener_target_id: str | None = None
    acquisition_context_id: str | None = None
    stimulus_profile_id: str | None = None
    stimulus_profile_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    evidence_kind: SystemNonlinearityEvidenceKind = 'installed_system_measurement'
    level_reference: Literal['absolute_spl', 'dbfs', 'relative', 'unknown'] = 'unknown'
    absolute_spl_capable: bool | None = None
    method: NonlinearityMethod = 'unknown'
    calibration_refs: tuple[str, ...] = ()
    producer: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    adapter: str | None = None
    adapter_version: str | None = None

    frequency_hz: tuple[float, ...]
    excitation_levels_db: tuple[float, ...] | None = None
    fundamental: CadDistortionSample | None = None
    distortion_samples: tuple[CadDistortionSample, ...] = ()
    noise_floor: CadDistortionSample | None = None

    analysis_bandwidth_hz: tuple[float, float] | None = None
    highest_harmonic: int | None = Field(default=None, ge=1)
    points_per_octave: int | None = Field(default=None, ge=1)
    smoothing: str | None = None
    quality: NonlinearityQuality = 'unknown'
    quality_reasons: tuple[str, ...] = ()
    clipping_state: Literal[
        'none', 'digital', 'analog_suspected', 'upstream_suspected', 'unknown'
    ] = 'unknown'
    created_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator('source_speaker_ids', 'calibration_refs', 'quality_reasons')
    @classmethod
    def unique_non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _require_unique_non_empty(value, 'string list fields')
        return value

    @model_validator(mode='after')
    def valid_measurement(self) -> 'SystemNonlinearityMeasurement':
        if self.evidence_kind != 'installed_system_measurement':
            raise ValueError(
                'SystemNonlinearityMeasurement is installed-system evidence; '
                'source capability belongs to EquipmentDefinition authorities'
            )
        if not self.frequency_hz:
            raise ValueError('frequency axis required')
        _require_finite(self.frequency_hz, 'frequency_hz')
        if any(frequency <= 0 for frequency in self.frequency_hz):
            raise ValueError('frequency_hz must be positive')
        if self.excitation_levels_db is not None:
            if not self.excitation_levels_db:
                raise ValueError('excitation_levels_db must not be empty')
            _require_finite(self.excitation_levels_db, 'excitation_levels_db')
        for sample in (*self.distortion_samples, self.fundamental, self.noise_floor):
            if sample is None:
                continue
            for axis_frequency in sample.frequency_hz:
                if (
                    axis_frequency < min(self.frequency_hz) * (1 - 1e-6)
                    or axis_frequency > max(self.frequency_hz) * (1 + 1e-6)
                ):
                    raise ValueError(
                        'distortion sample axis must lie within the '
                        'declared frequency axis'
                    )
            if (
                sample.level_axis_db is not None
                and self.excitation_levels_db is None
            ):
                raise ValueError(
                    'sample excitation axis requires a declared '
                    'excitation_levels_db axis'
                )
        if self.highest_harmonic is not None:
            for sample in self.distortion_samples:
                if (
                    sample.harmonic_order is not None
                    and sample.harmonic_order > self.highest_harmonic
                ):
                    raise ValueError(
                        'harmonic sample exceeds declared highest_harmonic'
                    )
        if self.analysis_bandwidth_hz is not None:
            low, high = self.analysis_bandwidth_hz
            if not (isfinite(low) and isfinite(high)) or not (0 < low < high):
                raise ValueError('analysis_bandwidth_hz must satisfy 0 < low < high')
        if self.quality == 'valid' and not (
            self.distortion_samples or self.fundamental is not None
        ):
            raise ValueError(
                'a valid nonlinearity result requires at least one '
                'distortion sample set or a fundamental'
            )
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('system nonlinearity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'semantic_sha256'})


def build_system_nonlinearity_measurement(
    *,
    result_id: str,
    document_id: str,
    measurement_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    producer: str,
    producer_version: str,
    frequency_hz: tuple[float, ...],
    created_at_utc: str,
    schema_version: str = 'system_nonlinearity_v1',
    dataset_id: str | None = None,
    dataset_sha256: str | None = None,
    system_variant_id: str | None = None,
    channel_role: str | None = None,
    source_speaker_ids: tuple[str, ...] = (),
    listener_target_id: str | None = None,
    acquisition_context_id: str | None = None,
    stimulus_profile_id: str | None = None,
    stimulus_profile_sha256: str | None = None,
    level_reference: str = 'unknown',
    absolute_spl_capable: bool | None = None,
    method: str = 'unknown',
    calibration_refs: tuple[str, ...] = (),
    adapter: str | None = None,
    adapter_version: str | None = None,
    excitation_levels_db: tuple[float, ...] | None = None,
    fundamental: CadDistortionSample | None = None,
    distortion_samples: tuple[CadDistortionSample, ...] = (),
    noise_floor: CadDistortionSample | None = None,
    analysis_bandwidth_hz: tuple[float, float] | None = None,
    highest_harmonic: int | None = None,
    points_per_octave: int | None = None,
    smoothing: str | None = None,
    quality: str = 'unknown',
    quality_reasons: tuple[str, ...] = (),
    clipping_state: str = 'unknown',
    provenance_json: str = '{}',
) -> SystemNonlinearityMeasurement:
    """Assemble and seal a system nonlinearity measurement.

    The semantic hash is derived here so every persisted record
    self-verifies on parse; callers supply only the evidence fields the
    producer actually exposed — unsupported quantities stay absent.
    """
    payload: dict[str, Any] = {
        'result_id': result_id,
        'schema_version': schema_version,
        'document_id': document_id,
        'measurement_id': measurement_id,
        'dataset_id': dataset_id,
        'dataset_sha256': dataset_sha256,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'system_variant_id': system_variant_id,
        'channel_role': channel_role,
        'source_speaker_ids': source_speaker_ids,
        'listener_target_id': listener_target_id,
        'acquisition_context_id': acquisition_context_id,
        'stimulus_profile_id': stimulus_profile_id,
        'stimulus_profile_sha256': stimulus_profile_sha256,
        'level_reference': level_reference,
        'absolute_spl_capable': absolute_spl_capable,
        'method': method,
        'calibration_refs': calibration_refs,
        'producer': producer,
        'producer_version': producer_version,
        'adapter': adapter,
        'adapter_version': adapter_version,
        'frequency_hz': frequency_hz,
        'excitation_levels_db': excitation_levels_db,
        'fundamental': fundamental,
        'distortion_samples': distortion_samples,
        'noise_floor': noise_floor,
        'analysis_bandwidth_hz': analysis_bandwidth_hz,
        'highest_harmonic': highest_harmonic,
        'points_per_octave': points_per_octave,
        'smoothing': smoothing,
        'quality': quality,
        'quality_reasons': quality_reasons,
        'clipping_state': clipping_state,
        'created_at_utc': created_at_utc,
        'provenance_json': provenance_json,
        'semantic_sha256': '0' * 64,
    }
    provisional = SystemNonlinearityMeasurement.model_construct(**payload)
    payload['semantic_sha256'] = _hash(provisional.identity_payload())
    return SystemNonlinearityMeasurement(**payload)
