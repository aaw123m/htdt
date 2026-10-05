"""Measurement transformation authority (#575, REV56-MEASEV).

REW-style measurement workflows transform measured evidence — alignment,
averaging, clock correction, windowing, arithmetic — and every such
operation can silently manufacture or destroy traceability. This module
makes each transform an immutable, sealed provenance record: what was
applied, to which exact inputs, with what declared effect on level /
time / phase / distortion / calibration authority — and what the derived
output may still claim.

Literature basis
----------------
- REW semantics (John Mulcahy): RMS average is an incoherent magnitude
  average (root-mean-square of linear magnitudes); dB average is an
  arithmetic mean of dB values; vector/complex averaging requires phase
  coherence and is only meaningful for repeat captures of the same
  physical event; "Align SPL" level alignment and time alignment via a
  measured timing reference vs. estimated IR delay / cross-correlation
  are distinct operations with distinct authority.
- Clock drift between asynchronous devices (e.g. a USB mic and an AVR
  output path) makes sweeps physically inconsistent — a declared,
  explicit clock-rate correction preserves the raw source and records
  the correction; it is never an invisible fix.
- GUM-style provenance: derived values must be re-derivable from raw
  inputs plus the operation list; raw evidence is never overwritten and
  derived outputs are never indistinguishable from measured data.

Design
------
- ``MeasurementTransform`` — one sealed operation node in the DAG.
  Inputs may be measured datasets/IRs or other transforms (derived
  chains); capabilities can only ever be downgraded, never upgraded.
- ``AlignmentSpec`` / ``ClockCorrectionSpec`` / ``ArithmeticSpec`` /
  ``TransformParameters`` — typed, versioned parameter blocks; an
  estimated alignment never becomes an exact one.
- ``DerivedFrequencyResponseOutput`` — the sealed derived output for
  frequency-response-domain transforms, bound to the transform hash.
- ``apply_transform`` — the declared executor for FR-domain kinds;
  time-domain kinds (IR delay removal, window/gate, clock correction)
  are recordable authorities whose execution is honestly out of scope
  and refused rather than faked.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_measurement_models import CadFrequencyResponseDataset
from .canonical_json import (
    canonical_sha256 as _hash,
    canonicalize_payload,
)


MEASUREMENT_TRANSFORM_SCHEMA_VERSION = 'measev-mta-1'
TRANSFORM_EXECUTOR_VERSION = 'measev-mta-exec-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


TransformKind = Literal[
    'time_shift',
    'ir_delay_removal',
    'cross_correlation_alignment',
    'level_alignment',
    'clock_rate_correction',
    'rms_magnitude_average',
    'db_average',
    'vector_complex_average',
    'rms_magnitude_plus_phase_average',
    'db_magnitude_plus_phase_average',
    'trace_add',
    'trace_subtract',
    'trace_multiply',
    'trace_divide',
    'frequency_merge',
    'window',
    'gate',
    'resample',
    'smooth',
    'band_aggregate',
    'normalize',
]

CapabilityFlag = Literal[
    'magnitude_valid',
    'absolute_spl_valid',
    'relative_phase_valid',
    'absolute_phase_valid',
    'relative_timing_valid',
    'absolute_timing_valid',
    'impulse_response_physical',
    'distortion_valid',
]

AuthorityDisposition = Literal['preserved', 'changed', 'invalidated']
"""What a transform does to each authority dimension — declared, never
implicit."""

QuantityDomain = Literal[
    'complex_transfer',
    'linear_magnitude',
    'db_magnitude',
    'power',
    'spl',
    'ir_amplitude',
]

AlignmentMethod = Literal[
    'measured_common_timing_reference',
    'estimated_cross_correlation',
    'manual_shift',
    'direct_arrival',
    'unknown',
]

TimingReferenceType = Literal[
    'none',
    'loopback',
    'acoustic_reference_speaker',
    'known_hardware_reference',
    'other',
]

AveragingClass = Literal['repeat_capture', 'spatial']

DistortionDisposition = Literal[
    'preserved', 'independently_averaged', 'invalidated', 'dropped'
]

AverageKinds = frozenset(
    {
        'rms_magnitude_average',
        'db_average',
        'vector_complex_average',
        'rms_magnitude_plus_phase_average',
        'db_magnitude_plus_phase_average',
    }
)
PhaseAverageKinds = frozenset(
    {
        'vector_complex_average',
        'rms_magnitude_plus_phase_average',
        'db_magnitude_plus_phase_average',
    }
)
AlignmentKinds = frozenset(
    {
        'time_shift',
        'ir_delay_removal',
        'cross_correlation_alignment',
        'level_alignment',
    }
)
ArithmeticKinds = frozenset(
    {'trace_add', 'trace_subtract', 'trace_multiply', 'trace_divide'}
)


class TransformInput(BaseModel):
    """One DAG input: an exact measured dataset/IR or an exact upstream
    transform — bound by id + content hash, never by name."""

    model_config = ConfigDict(frozen=True)

    input_id: str = Field(min_length=1)
    role: Literal['primary', 'member', 'reference', 'operand'] = 'member'
    source_kind: Literal[
        'measured_dataset',
        'measured_ir',
        'derived_transform',
        'measurement',
    ]
    dataset_id: str | None = None
    dataset_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    ir_dataset_id: str | None = None
    ir_dataset_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    transform_id: str | None = None
    transform_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    measurement_id: str | None = None
    position_id: str | None = None
    weight: float | None = Field(default=None, gt=0.0)
    declared_domain: QuantityDomain | None = None
    capability_flags: tuple[CapabilityFlag, ...] = ()

    @model_validator(mode='after')
    def consistent(self) -> 'TransformInput':
        bound = sum(
            ref is not None
            for ref in (
                self.dataset_id,
                self.ir_dataset_id,
                self.transform_id,
                self.measurement_id,
            )
        )
        if bound != 1:
            raise ValueError(
                'a transform input must bind exactly one measured dataset, '
                'measured IR, derived transform or measurement'
            )
        if self.dataset_id is not None and self.dataset_sha256 is None:
            raise ValueError('dataset inputs require the dataset sha256')
        if self.ir_dataset_id is not None and self.ir_dataset_sha256 is None:
            raise ValueError('IR inputs require the dataset sha256')
        if self.transform_id is not None and self.transform_sha256 is None:
            raise ValueError(
                'derived-transform inputs require the transform sha256'
            )
        if self.weight is not None and not isfinite(float(self.weight)):
            raise ValueError('weight must be finite')
        return self


class InputDelayGain(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_id: str = Field(min_length=1)
    delay_s: float | None = None
    gain_db: float | None = None

    @model_validator(mode='after')
    def finite(self) -> 'InputDelayGain':
        for value in (self.delay_s, self.gain_db):
            if value is not None and not isfinite(float(value)):
                raise ValueError('delay/gain must be finite')
        if self.delay_s is None and self.gain_db is None:
            raise ValueError('a delay/gain entry must carry a value')
        return self


class AlignmentSpec(BaseModel):
    """Declared alignment authority — an estimated alignment never
    becomes an exact one."""

    model_config = ConfigDict(frozen=True)

    method: AlignmentMethod
    timing_reference_type: TimingReferenceType = 'none'
    reference_input_id: str | None = None
    per_input: tuple[InputDelayGain, ...] = ()
    correlation_band_hz: tuple[float, float] | None = None
    correlation_window_s: tuple[float, float] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    algorithm_version: str | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def consistent(self) -> 'AlignmentSpec':
        if self.method == 'measured_common_timing_reference':
            if self.timing_reference_type in ('none',):
                raise ValueError(
                    'a measured common timing reference requires a declared '
                    'timing reference type (loopback / acoustic reference / '
                    'hardware reference)'
                )
        if self.method == 'estimated_cross_correlation':
            if self.algorithm_version is None:
                raise ValueError(
                    'estimated cross-correlation alignment requires a '
                    'versioned algorithm declaration'
                )
            if self.correlation_band_hz is None:
                raise ValueError(
                    'estimated cross-correlation alignment requires the '
                    'declared correlation band'
                )
        if self.correlation_band_hz is not None:
            lo, hi = self.correlation_band_hz
            if not (isfinite(float(lo)) and isfinite(float(hi))) or not (
                0.0 < float(lo) < float(hi)
            ):
                raise ValueError('correlation band must satisfy 0 < lo < hi')
        if self.correlation_window_s is not None:
            lo, hi = self.correlation_window_s
            if not (isfinite(float(lo)) and isfinite(float(hi))) or not (
                float(lo) < float(hi)
            ):
                raise ValueError('correlation window must satisfy lo < hi')
        return self


class ClockCorrectionSpec(BaseModel):
    """Declared clock-rate correction — explicit, immutable, and the raw
    source is always preserved."""

    model_config = ConfigDict(frozen=True)

    relation: Literal[
        'common_reference',
        'independent_estimated',
        'independent_declared',
        'unknown',
    ]
    estimated_rate_difference: float | None = None
    method: str | None = None
    applied_factor: float | None = None
    resampling_algorithm: str | None = None
    residual_error_bound: float | None = Field(default=None, ge=0.0)
    raw_source_preserved: bool = True
    notes: str | None = None

    @model_validator(mode='after')
    def consistent(self) -> 'ClockCorrectionSpec':
        if not self.raw_source_preserved:
            raise ValueError(
                'clock-rate correction must preserve the raw source — '
                'in-place overwrite is refused'
            )
        if self.applied_factor is not None and not (
            isfinite(float(self.applied_factor))
            and float(self.applied_factor) > 0.0
        ):
            raise ValueError('applied_factor must be finite and positive')
        for value in (
            self.estimated_rate_difference,
            self.residual_error_bound,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('clock spec values must be finite')
        return self


class ArithmeticSpec(BaseModel):
    """Declared domain semantics for trace arithmetic — quantity and
    domain are validated, never assumed."""

    model_config = ConfigDict(frozen=True)

    input_domains: tuple[QuantityDomain, ...]
    output_domain: QuantityDomain
    notes: str | None = None


_ARITHMETIC_LEGAL: dict[str, frozenset[tuple[QuantityDomain, ...]]] = {
    'trace_add': frozenset(
        {
            ('linear_magnitude', 'linear_magnitude'),
            ('complex_transfer', 'complex_transfer'),
            ('db_magnitude', 'db_magnitude'),
            ('ir_amplitude', 'ir_amplitude'),
        }
    ),
    'trace_subtract': frozenset(
        {
            ('linear_magnitude', 'linear_magnitude'),
            ('complex_transfer', 'complex_transfer'),
            ('db_magnitude', 'db_magnitude'),
            ('spl', 'spl'),
            ('ir_amplitude', 'ir_amplitude'),
        }
    ),
    'trace_multiply': frozenset(
        {
            ('linear_magnitude', 'linear_magnitude'),
            ('complex_transfer', 'complex_transfer'),
            ('power', 'power'),
        }
    ),
    'trace_divide': frozenset(
        {
            ('linear_magnitude', 'linear_magnitude'),
            ('complex_transfer', 'complex_transfer'),
            ('db_magnitude', 'db_magnitude'),
            ('power', 'power'),
        }
    ),
}
"""Legal input-domain pairs per arithmetic kind. dB-domain add/subtract
are allowed as *declared* dB arithmetic; multiply/divide require a real
quantity domain — never a silent dB product."""


class TransformParameters(BaseModel):
    """Typed parameter blocks — one per applicable family."""

    model_config = ConfigDict(frozen=True)

    alignment: AlignmentSpec | None = None
    clock_correction: ClockCorrectionSpec | None = None
    arithmetic: ArithmeticSpec | None = None
    weighting: Literal['equal', 'explicit'] = 'equal'
    output_grid_hz: tuple[float, ...] | None = None
    interpolation_rule: Literal['linear_v1'] | None = None
    smooth_fraction_octave: float | None = Field(default=None, gt=0.0)
    smooth_include_phase: bool = False
    aggregate_bands_hz: tuple[tuple[float, float], ...] | None = None
    merge_overlap_rule: Literal[
        'non_overlapping', 'prefer_first', 'prefer_last'
    ] = 'non_overlapping'
    normalize_rule: Literal[
        'peak', 'level_at_frequency', 'reference_trace', 'target_level_db'
    ] | None = None
    normalize_frequency_hz: float | None = None
    normalize_target_db: float | None = None
    level_alignment_span_hz: tuple[float, float] | None = None
    window_s: tuple[float, float] | None = None
    shift_s: float | None = None
    preserve_absolute_level: bool = False

    @model_validator(mode='after')
    def consistent(self) -> 'TransformParameters':
        if self.output_grid_hz is not None:
            freqs = list(self.output_grid_hz)
            if len(freqs) < 2 or sorted(freqs) != freqs:
                raise ValueError('output grid must be strictly increasing')
            if any(
                not isfinite(float(f)) or float(f) <= 0 for f in freqs
            ):
                raise ValueError('output grid frequencies must be > 0')
        if self.aggregate_bands_hz is not None:
            last_hi = 0.0
            for lo, hi in self.aggregate_bands_hz:
                if not (
                    isfinite(float(lo)) and isfinite(float(hi))
                ) or not (0.0 < float(lo) < float(hi)):
                    raise ValueError('aggregate bands must satisfy 0<lo<hi')
                if float(lo) <= last_hi:
                    raise ValueError(
                        'aggregate bands must be ordered and non-overlapping'
                    )
                last_hi = float(hi)
        if self.level_alignment_span_hz is not None:
            lo, hi = self.level_alignment_span_hz
            if not (0.0 < float(lo) < float(hi)):
                raise ValueError('alignment span must satisfy 0 < lo < hi')
        if self.window_s is not None:
            lo, hi = self.window_s
            if not (isfinite(float(lo)) and isfinite(float(hi))) or not (
                float(lo) < float(hi)
            ):
                raise ValueError('window must satisfy lo < hi')
        return self


class DerivedFrequencyResponseOutput(BaseModel):
    """Sealed derived output — a transformed frequency response that is
    never indistinguishable from a measured dataset."""

    model_config = ConfigDict(frozen=True)

    output_id: str = Field(min_length=1)
    frequency_hz: tuple[float, ...]
    level_db: tuple[float, ...]
    phase_deg: tuple[float, ...] | None = None
    quantity_domain: QuantityDomain = 'db_magnitude'
    capability_flags: tuple[CapabilityFlag, ...] = ()
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'output_id': self.output_id,
            'frequency_hz': list(self.frequency_hz),
            'level_db': list(self.level_db),
            'phase_deg': (
                None if self.phase_deg is None else list(self.phase_deg)
            ),
            'quantity_domain': self.quantity_domain,
            'capability_flags': list(self.capability_flags),
        }

    @model_validator(mode='after')
    def consistent(self) -> 'DerivedFrequencyResponseOutput':
        if len(self.frequency_hz) != len(self.level_db):
            raise ValueError('frequency/level arrays must match in length')
        if len(self.frequency_hz) < 1:
            raise ValueError('derived output requires samples')
        if self.phase_deg is not None and len(self.phase_deg) != len(
            self.frequency_hz
        ):
            raise ValueError('phase array must match frequency length')
        if any(
            not isfinite(float(f)) or float(f) <= 0
            for f in self.frequency_hz
        ):
            raise ValueError('frequencies must be finite and positive')
        if any(not isfinite(float(v)) for v in self.level_db):
            raise ValueError('levels must be finite')
        if self.phase_deg is not None and any(
            not isfinite(float(v)) for v in self.phase_deg
        ):
            raise ValueError('phases must be finite')
        if self.content_sha256 != _hash(self.identity_payload()):
            raise ValueError('derived output content hash mismatch')
        return self


# --- declared per-kind authority effects ---------------------------------

#: For each kind: capabilities a transform may output (the *allowed* set);
#: the effective output is input∩allowed, further restricted by declared
#: dispositions. Everything not listed is invalidated by the kind itself.
_ALLOWED_OUTPUT_CAPS: dict[str, frozenset[CapabilityFlag]] = {
    'time_shift': frozenset(
        {
            'magnitude_valid',
            'absolute_spl_valid',
            'relative_phase_valid',
            'absolute_phase_valid',
            'impulse_response_physical',
        }
    ),
    'ir_delay_removal': frozenset(
        {
            'magnitude_valid',
            'absolute_spl_valid',
            'relative_phase_valid',
            'relative_timing_valid',
            'impulse_response_physical',
        }
    ),
    'cross_correlation_alignment': frozenset(
        {
            'magnitude_valid',
            'absolute_spl_valid',
            'relative_phase_valid',
            'relative_timing_valid',
            'impulse_response_physical',
        }
    ),
    'level_alignment': frozenset(
        {
            'magnitude_valid',
            'relative_phase_valid',
            'absolute_phase_valid',
            'relative_timing_valid',
            'absolute_timing_valid',
            'impulse_response_physical',
        }
    ),
    'clock_rate_correction': frozenset(
        {
            'magnitude_valid',
            'absolute_spl_valid',
            'relative_phase_valid',
            'absolute_phase_valid',
            'relative_timing_valid',
            'absolute_timing_valid',
            'impulse_response_physical',
        }
    ),
    'rms_magnitude_average': frozenset(
        {'magnitude_valid', 'absolute_spl_valid', 'distortion_valid'}
    ),
    'db_average': frozenset(
        {'magnitude_valid', 'absolute_spl_valid', 'distortion_valid'}
    ),
    'vector_complex_average': frozenset(
        {
            'magnitude_valid',
            'absolute_spl_valid',
            'relative_phase_valid',
            'relative_timing_valid',
        }
    ),
    'rms_magnitude_plus_phase_average': frozenset(
        {
            'magnitude_valid',
            'absolute_spl_valid',
            'relative_phase_valid',
            'relative_timing_valid',
        }
    ),
    'db_magnitude_plus_phase_average': frozenset(
        {
            'magnitude_valid',
            'absolute_spl_valid',
            'relative_phase_valid',
            'relative_timing_valid',
        }
    ),
    'trace_add': frozenset({'magnitude_valid'}),
    'trace_subtract': frozenset({'magnitude_valid'}),
    'trace_multiply': frozenset({'magnitude_valid'}),
    'trace_divide': frozenset({'magnitude_valid'}),
    'frequency_merge': frozenset(
        {
            'magnitude_valid',
            'absolute_spl_valid',
            'relative_phase_valid',
            'relative_timing_valid',
            'distortion_valid',
        }
    ),
    'window': frozenset(
        {
            'magnitude_valid',
            'absolute_spl_valid',
            'relative_phase_valid',
            'impulse_response_physical',
            'distortion_valid',
        }
    ),
    'gate': frozenset(
        {
            'magnitude_valid',
            'absolute_spl_valid',
            'relative_phase_valid',
            'impulse_response_physical',
        }
    ),
    'resample': frozenset(
        {
            'magnitude_valid',
            'absolute_spl_valid',
            'relative_phase_valid',
            'relative_timing_valid',
            'impulse_response_physical',
            'distortion_valid',
        }
    ),
    'smooth': frozenset({'magnitude_valid', 'absolute_spl_valid'}),
    'band_aggregate': frozenset({'magnitude_valid', 'absolute_spl_valid'}),
    'normalize': frozenset(
        {
            'magnitude_valid',
            'relative_phase_valid',
            'relative_timing_valid',
        }
    ),
}

_TIME_DOMAIN_ONLY = frozenset(
    {'time_shift', 'ir_delay_removal', 'window', 'gate', 'clock_rate_correction'}
)


class MeasurementTransform(BaseModel):
    """Sealed provenance node: one declared transform over exact inputs.

    ``output_capabilities`` may only downgrade relative to the
    intersection of input capabilities and the kind's allowed set — a
    transform never manufactures validity that was not already there.
    """

    model_config = ConfigDict(frozen=True)

    transform_id: str = Field(min_length=1)
    schema_version: str = MEASUREMENT_TRANSFORM_SCHEMA_VERSION
    document_id: str = Field(min_length=1)
    kind: TransformKind
    label: str | None = None
    inputs: tuple[TransformInput, ...]
    parameters: TransformParameters = Field(
        default_factory=TransformParameters
    )
    averaging_class: AveragingClass | None = None
    input_capabilities: tuple[CapabilityFlag, ...] = ()
    output_capabilities: tuple[CapabilityFlag, ...] = ()
    level_disposition: AuthorityDisposition = 'preserved'
    timing_disposition: AuthorityDisposition = 'preserved'
    phase_disposition: AuthorityDisposition = 'preserved'
    distortion_disposition: AuthorityDisposition | DistortionDisposition = (
        'preserved'
    )
    calibration_disposition: AuthorityDisposition = 'preserved'
    executor_version: str | None = TRANSFORM_EXECUTOR_VERSION
    derived_output: DerivedFrequencyResponseOutput | None = None
    limitations: tuple[str, ...] = ()
    notes: str = ''
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'transform_id', 'semantic_sha256'}
        )

    @model_validator(mode='after')
    def consistent(self) -> 'MeasurementTransform':
        if not self.inputs:
            raise ValueError('a transform requires at least one input')
        input_ids = [i.input_id for i in self.inputs]
        if len(set(input_ids)) != len(input_ids):
            raise ValueError('transform input ids must be unique')

        # DAG inputs only declare capabilities they carried — the
        # effective input set is the intersection across inputs.
        declared_inputs = set(self.input_capabilities)
        for entry in self.inputs:
            declared_inputs &= set(entry.capability_flags)
        if declared_inputs != set(self.input_capabilities):
            raise ValueError(
                'input_capabilities must equal the intersection of every '
                'input\'s declared capability flags'
            )

        allowed = _ALLOWED_OUTPUT_CAPS[self.kind]
        output = set(self.output_capabilities)
        if not output <= allowed:
            raise ValueError(
                f'{self.kind} may not output capabilities '
                f'{sorted(output - allowed)}'
            )
        if not output <= set(self.input_capabilities):
            raise ValueError(
                'transform capabilities may only downgrade — output claims '
                'a capability no input carried: '
                f'{sorted(output - set(self.input_capabilities))}'
            )

        params = self.parameters
        if self.kind in AverageKinds:
            if len(self.inputs) < 2:
                raise ValueError('averaging requires at least two inputs')
            if self.averaging_class is None:
                raise ValueError(
                    'averaging requires a declared class '
                    '(repeat_capture vs spatial) — generic averages are '
                    'refused'
                )
            if params.weighting == 'explicit':
                if any(i.weight is None for i in self.inputs):
                    raise ValueError(
                        'explicit weighting requires a weight per input'
                    )
            if self.kind in PhaseAverageKinds:
                if 'relative_phase_valid' not in self.input_capabilities:
                    raise ValueError(
                        'complex/phase averaging requires '
                        'relative_phase_valid on every input'
                    )
                if params.alignment is None:
                    raise ValueError(
                        'complex/phase averaging requires a declared '
                        'alignment authority'
                    )
            if self.averaging_class == 'spatial':
                positions = {
                    i.position_id for i in self.inputs if i.position_id
                }
                if len(positions) < 2:
                    raise ValueError(
                        'a spatial average requires inputs bound to '
                        'distinct positions'
                    )
                if (
                    params.alignment is None
                    and 'impulse_response_physical' in output
                ):
                    raise ValueError(
                        'a spatial average without a declared alignment '
                        'authority may not claim impulse_response_physical'
                    )
            elif self.averaging_class == 'repeat_capture':
                positions = {
                    i.position_id for i in self.inputs if i.position_id
                }
                if len(positions) > 1:
                    raise ValueError(
                        'repeat-capture averaging must not mix distinct '
                        'positions — declare a spatial average instead'
                    )
        if self.kind in (
            'time_shift',
            'ir_delay_removal',
            'cross_correlation_alignment',
        ):
            if params.alignment is None:
                raise ValueError(
                    f'{self.kind} requires a declared alignment spec'
                )
            if (
                params.alignment.method
                in ('estimated_cross_correlation', 'unknown')
                and 'absolute_timing_valid' in output
            ):
                raise ValueError(
                    'an estimated alignment never upgrades absolute '
                    'timing — absolute_timing_valid is refused'
                )
        if self.kind == 'clock_rate_correction':
            if params.clock_correction is None:
                raise ValueError(
                    'clock_rate_correction requires a declared clock '
                    'specification'
                )
        if self.kind in ArithmeticKinds:
            if len(self.inputs) != 2:
                raise ValueError(
                    'trace arithmetic requires exactly two inputs'
                )
            if params.arithmetic is None:
                raise ValueError(
                    'trace arithmetic requires a declared domain spec'
                )
            pair = tuple(params.arithmetic.input_domains)
            if len(pair) != 2:
                raise ValueError('arithmetic declares two input domains')
            if pair not in _ARITHMETIC_LEGAL[self.kind]:
                raise ValueError(
                    f'{self.kind} is not defined for input domains {pair}'
                )
        if self.kind == 'level_alignment' and params.alignment is None:
            if params.level_alignment_span_hz is None:
                raise ValueError(
                    'level alignment requires an alignment spec or a '
                    'declared span'
                )
        if (
            self.distortion_disposition == 'preserved'
            and 'distortion_valid' in self.input_capabilities
            and self.kind not in (
                'resample',
                'frequency_merge',
                'rms_magnitude_average',
                'db_average',
                'clock_rate_correction',
            )
            and self.phase_disposition != 'preserved'
        ):
            raise ValueError(
                'distortion evidence cannot be declared preserved across '
                'a transform that changes phase'
            )
        if self.derived_output is not None:
            if set(self.derived_output.capability_flags) != output:
                raise ValueError(
                    'derived output capability flags must equal the '
                    'declared output capabilities'
                )
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement transform hash mismatch')
        expected_id = f'mtr:{self.semantic_sha256}'
        if self.transform_id != expected_id:
            raise ValueError('transform_id must be mtr:<semantic sha256>')
        return self


def build_measurement_transform(**kwargs: Any) -> MeasurementTransform:
    payload = {
        'transform_id': '0' * 64,
        'semantic_sha256': '0' * 64,
        **kwargs,
    }
    provisional = MeasurementTransform.model_construct(
        **canonicalize_payload(MeasurementTransform, dict(**payload))
    )
    payload['semantic_sha256'] = _hash(provisional.identity_payload())
    payload['transform_id'] = f'mtr:{payload["semantic_sha256"]}'
    return MeasurementTransform(**payload)


# --- FR-domain executor ---------------------------------------------------


def _common_grid(
    datasets: list[CadFrequencyResponseDataset],
    params: TransformParameters,
) -> np.ndarray:
    grids = {d.frequency_hz for d in datasets}
    if len(grids) == 1:
        return np.asarray(datasets[0].frequency_hz, dtype=float)
    if params.output_grid_hz is None or params.interpolation_rule is None:
        raise ValueError(
            'inputs on different frequency grids require a declared '
            'output_grid_hz + interpolation_rule — silent regridding is '
            'refused'
        )
    return np.asarray(params.output_grid_hz, dtype=float)


def _resampled_levels(
    dataset: CadFrequencyResponseDataset, grid: np.ndarray
) -> np.ndarray:
    source = np.asarray(dataset.frequency_hz, dtype=float)
    if np.array_equal(source, grid):
        return np.asarray(dataset.level_db, dtype=float)
    values = np.interp(grid, source, dataset.level_db)
    return values


def _resampled_phase(
    dataset: CadFrequencyResponseDataset, grid: np.ndarray
) -> np.ndarray:
    if dataset.phase_deg is None:
        raise ValueError(
            f'dataset {dataset.dataset_id} carries no phase data'
        )
    source = np.asarray(dataset.frequency_hz, dtype=float)
    if np.array_equal(source, grid):
        return np.asarray(dataset.phase_deg, dtype=float)
    radians = np.deg2rad(np.asarray(dataset.phase_deg, dtype=float))
    real = np.interp(grid, source, np.cos(radians))
    imag = np.interp(grid, source, np.sin(radians))
    return np.rad2deg(np.arctan2(imag, real))


def _weights(
    inputs: tuple[TransformInput, ...], params: TransformParameters
) -> np.ndarray:
    if params.weighting == 'explicit':
        w = np.asarray([float(i.weight) for i in inputs])
        return w / w.sum()
    return np.full(len(inputs), 1.0 / len(inputs))


def apply_transform(
    transform: MeasurementTransform,
    datasets: dict[str, CadFrequencyResponseDataset],
    *,
    output_id: str,
) -> DerivedFrequencyResponseOutput:
    """Execute one declared transform over measured FR datasets.

    Only frequency-response-domain kinds are executable here; time-domain
    authorities (IR delay removal, window/gate, clock correction) are
    recorded, not computed — callers get an honest refusal.
    ``datasets`` maps ``TransformInput.input_id`` → the measured dataset;
    the bound sha256 is verified so an input swap can never silently
    alter the DAG.
    """
    kind = transform.kind
    if kind in _TIME_DOMAIN_ONLY:
        raise ValueError(
            f'{kind} is a time-domain authority — recording only; no '
            'frequency-response execution exists for it'
        )
    ordered: list[CadFrequencyResponseDataset] = []
    for entry in transform.inputs:
        ds = datasets.get(entry.input_id)
        if ds is None:
            raise ValueError(f'missing dataset for input {entry.input_id}')
        if ds.dataset_id != entry.dataset_id or (
            entry.dataset_sha256 is not None
            and ds.dataset_sha256 != entry.dataset_sha256
        ):
            raise ValueError(
                f'input {entry.input_id} does not match the bound dataset '
                'identity — refusing to transform substituted evidence'
            )
        ordered.append(ds)
    params = transform.parameters
    domain: QuantityDomain = 'db_magnitude'
    phase_out: np.ndarray | None = None

    if kind == 'frequency_merge':
        # merge keeps each input's own grid; overlapping frequencies are a
        # conflict unless a declared rule resolves them
        merged: dict[float, float] = {}
        datasets_for_merge = (
            ordered
            if params.merge_overlap_rule != 'prefer_last'
            else list(reversed(ordered))
        )
        for dataset in datasets_for_merge:
            for frequency, level in zip(
                dataset.frequency_hz, dataset.level_db
            ):
                if (
                    frequency in merged
                    and params.merge_overlap_rule == 'non_overlapping'
                ):
                    raise ValueError(
                        'frequency_merge inputs overlap — declare '
                        'prefer_first/prefer_last or clip the ranges'
                    )
                merged.setdefault(frequency, level)
        keys = sorted(merged)
        return _seal_output(
            transform,
            output_id,
            np.asarray(keys),
            np.asarray([merged[k] for k in keys]),
            None,
            domain,
        )

    grid = _common_grid(ordered, params)
    levels = np.stack(
        [_resampled_levels(d, grid) for d in ordered], axis=0
    )
    weights = _weights(transform.inputs, params)

    if kind == 'rms_magnitude_average':
        power = np.sum(
            weights[:, None] * (10.0 ** (levels / 10.0)), axis=0
        )
        out_levels = 10.0 * np.log10(np.maximum(power, 1e-30))
    elif kind == 'db_average':
        out_levels = np.sum(weights[:, None] * levels, axis=0)
    elif kind in PhaseAverageKinds:
        phases = np.stack(
            [_resampled_phase(d, grid) for d in ordered], axis=0
        )
        rad = np.deg2rad(phases)
        if params.alignment is not None:
            delays = {
                d.input_id: d.delay_s
                for d in params.alignment.per_input
                if d.delay_s is not None
            }
            for index, entry in enumerate(transform.inputs):
                delay = delays.get(entry.input_id)
                if delay:
                    rad[index] = rad[index] - 2.0 * np.pi * grid * float(
                        delay
                    )
        if kind == 'vector_complex_average':
            linear = 10.0 ** (levels / 20.0)
            vec = np.sum(
                weights[:, None] * linear * np.exp(1j * rad), axis=0
            )
            out_levels = 20.0 * np.log10(np.maximum(np.abs(vec), 1e-30))
            phase_out = np.rad2deg(np.angle(vec))
            domain = 'complex_transfer'
        else:
            mag = (
                np.sum(weights[:, None] * (10.0 ** (levels / 10.0)), axis=0)
                if kind == 'rms_magnitude_plus_phase_average'
                else np.sum(weights[:, None] * levels, axis=0)
            )
            out_levels = (
                10.0 * np.log10(np.maximum(mag, 1e-30))
                if kind == 'rms_magnitude_plus_phase_average'
                else mag
            )
            unit = np.sum(
                weights[:, None] * np.exp(1j * rad), axis=0
            )
            phase_out = np.rad2deg(np.angle(unit))
            domain = 'complex_transfer'
    elif kind in ArithmeticKinds:
        spec = params.arithmetic
        a, b = levels[0], levels[1]
        if spec.input_domains == ('complex_transfer', 'complex_transfer'):
            ph = np.deg2rad(
                np.stack(
                    [_resampled_phase(d, grid) for d in ordered], axis=0
                )
            )
            ca = 10.0 ** (a / 20.0) * np.exp(1j * ph[0])
            cb = 10.0 ** (b / 20.0) * np.exp(1j * ph[1])
            result = {
                'trace_add': ca + cb,
                'trace_subtract': ca - cb,
                'trace_multiply': ca * cb,
                'trace_divide': ca / np.where(np.abs(cb) > 0, cb, np.nan),
            }[kind]
            if np.isnan(result).any() or np.isinf(result).any():
                raise ValueError(
                    'complex division produced non-finite values — '
                    'refusing to emit an invalid derived trace'
                )
            out_levels = 20.0 * np.log10(np.maximum(np.abs(result), 1e-30))
            phase_out = np.rad2deg(np.angle(result))
            domain = spec.output_domain
        elif spec.input_domains == ('linear_magnitude', 'linear_magnitude'):
            la, lb = 10.0 ** (a / 20.0), 10.0 ** (b / 20.0)
            result = {
                'trace_add': la + lb,
                'trace_subtract': la - lb,
                'trace_multiply': la * lb,
                'trace_divide': np.where(lb > 0, la / lb, np.nan),
            }[kind]
            if np.isnan(result).any() or np.isinf(result).any() or (
                result <= 0
            ).any():
                raise ValueError(
                    'linear arithmetic produced non-positive/non-finite '
                    'values — refusing to emit an invalid derived trace'
                )
            out_levels = 20.0 * np.log10(result)
            domain = spec.output_domain
        else:
            # declared dB / spl arithmetic
            result = {
                'trace_add': a + b,
                'trace_subtract': a - b,
                'trace_divide': a - b,
            }.get(kind)
            if result is None:
                raise ValueError(
                    f'{kind} is not defined for domains '
                    f'{spec.input_domains}'
                )
            out_levels = result
            domain = spec.output_domain
    elif kind == 'level_alignment':
        reference_index = next(
            (
                i
                for i, e in enumerate(transform.inputs)
                if e.role == 'reference'
            ),
            0,
        )
        span = params.level_alignment_span_hz
        mask = (
            (grid >= span[0]) & (grid <= span[1])
            if span is not None
            else np.ones(grid.shape, dtype=bool)
        )
        if not mask.any():
            raise ValueError('level alignment span covers no grid points')
        means = levels[:, mask].mean(axis=1)
        offsets = means[reference_index] - means
        out_levels = levels + offsets[:, None]
        # alignment output is the aligned set stacked into one trace:
        out_levels = np.sum(
            weights[:, None] * out_levels, axis=0
        )
    elif kind == 'resample':
        if len(ordered) != 1:
            raise ValueError('resample operates on exactly one input')
        if params.output_grid_hz is None or params.interpolation_rule is None:
            raise ValueError(
                'resample requires declared output_grid_hz and '
                'interpolation_rule'
            )
        out_grid = np.asarray(params.output_grid_hz)
        out_levels = np.interp(
            out_grid,
            np.asarray(ordered[0].frequency_hz),
            np.asarray(ordered[0].level_db),
        )
        phase_vals = None
        if 'relative_phase_valid' in transform.output_capabilities or (
            'absolute_phase_valid' in transform.output_capabilities
        ):
            phase_vals = _resampled_phase(ordered[0], out_grid)
        return _seal_output(
            transform, output_id, out_grid, out_levels, phase_vals, domain
        )
    elif kind == 'smooth':
        if len(ordered) != 1:
            raise ValueError('smooth operates on exactly one input')
        fraction = params.smooth_fraction_octave
        if fraction is None:
            raise ValueError(
                'smooth requires a declared fractional-octave width'
            )
        out_levels = _fractional_octave_smooth(
            grid, levels[0], float(fraction)
        )
        if params.smooth_include_phase and ordered[0].phase_deg is not None:
            phase_out = _fractional_octave_smooth(
                grid, _resampled_phase(ordered[0], grid), float(fraction)
            )
    elif kind == 'band_aggregate':
        bands = params.aggregate_bands_hz
        if bands is None:
            raise ValueError('band_aggregate requires declared bands')
        centers: list[float] = []
        values: list[float] = []
        for lo, hi in bands:
            mask = (grid >= lo) & (grid <= hi)
            if not mask.any():
                values.append(np.nan)
            else:
                values.append(
                    float(np.mean(levels[:, mask].mean(axis=0)))
                )
            centers.append(float(np.sqrt(lo * hi)))
        if any(np.isnan(values)):
            raise ValueError(
                'a declared band covers no grid points — refine bands '
                'or grid'
            )
        return _seal_output(
            transform,
            output_id,
            np.asarray(centers),
            np.asarray(values),
            None,
            domain,
        )
    elif kind == 'normalize':
        if len(ordered) != 1:
            raise ValueError('normalize operates on exactly one input')
        rule = params.normalize_rule
        if rule == 'peak':
            out_levels = levels[0] - float(np.max(levels[0]))
        elif rule == 'level_at_frequency':
            if params.normalize_frequency_hz is None:
                raise ValueError(
                    'level_at_frequency normalization requires '
                    'normalize_frequency_hz'
                )
            ref = float(
                np.interp(
                    params.normalize_frequency_hz,
                    grid,
                    levels[0],
                )
            )
            out_levels = levels[0] - ref
        elif rule == 'target_level_db':
            if params.normalize_target_db is None:
                raise ValueError(
                    'target_level_db normalization requires '
                    'normalize_target_db'
                )
            span = params.level_alignment_span_hz
            mask = (
                (grid >= span[0]) & (grid <= span[1])
                if span is not None
                else np.ones(grid.shape, dtype=bool)
            )
            out_levels = levels[0] + (
                float(params.normalize_target_db)
                - float(levels[0][mask].mean())
            )
        else:
            raise ValueError('normalize requires a declared rule')
    else:
        raise ValueError(f'unsupported transform kind {kind!r}')

    return _seal_output(
        transform, output_id, grid, out_levels, phase_out, domain
    )


def _fractional_octave_smooth(
    grid: np.ndarray, values: np.ndarray, fraction: float
) -> np.ndarray:
    """Moving mean of ``values`` over a fractional-octave window in the
    log-frequency domain."""
    logf = np.log(grid)
    half_width = 0.5 * fraction * np.log(2.0)
    out = np.empty_like(values)
    for index, center in enumerate(logf):
        mask = (logf >= center - half_width) & (logf <= center + half_width)
        out[index] = float(np.mean(values[mask])) if mask.any() else values[
            index
        ]
    return out


def _seal_output(
    transform: MeasurementTransform,
    output_id: str,
    grid: np.ndarray,
    levels: np.ndarray,
    phases: np.ndarray | None,
    domain: QuantityDomain,
) -> DerivedFrequencyResponseOutput:
    payload = {
        'output_id': output_id,
        'frequency_hz': tuple(float(f) for f in grid),
        'level_db': tuple(float(v) for v in levels),
        'phase_deg': (
            None if phases is None else tuple(float(v) for v in phases)
        ),
        'quantity_domain': domain,
        'capability_flags': tuple(transform.output_capabilities),
        'content_sha256': '0' * 64,
    }
    provisional = DerivedFrequencyResponseOutput.model_construct(
        **canonicalize_payload(
            DerivedFrequencyResponseOutput, dict(**payload)
        )
    )
    payload['content_sha256'] = _hash(provisional.identity_payload())
    return DerivedFrequencyResponseOutput(**payload)
