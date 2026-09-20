from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import closing
from hashlib import sha256
import json
from math import atan2, cos, isclose, isfinite, sin
from pathlib import Path
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_solver_result import AcousticSolverResultEnvelope
from .cad_candidate_wave_execution import (
    COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
    CandidateWaveExecutionInput,
)
from .cad_geometric_acoustics_response import (
    DeterministicPathFrequencyResponseArtifact,
    PHASOR_CONVENTION as R150_PHASOR_CONVENTION,
    TRANSFER_QUANTITY,
    TRANSFER_UNIT,
)
from .cad_hybrid_grid_reconciliation import (
    FrequencyGridReconciliationAuthority,
    HybridCrossoverConfigurationAuthority,
    HybridNumericalCompositionError,
    HybridNumericalFailureCode,
    build_frequency_grid_reconciliation_authority,
    build_hybrid_crossover_configuration_authority,
    reconcile_complex_series,
    validate_frequency_grid,
)
from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema
from .cad_wave_excitation import AcousticWaveExcitationAuthority
from .r120_geometry_compiler import ExactExternalAuthorityRef


R160_NUMERICAL_SPEC_AUTHORITY_VERSION = 'r160-numerical-hybrid-composition-spec-1'
R160_CONVENTION_AUTHORITY_VERSION = 'r160-complex-convention-normalization-1'
R160_GA_AGGREGATION_AUTHORITY_VERSION = 'r160-coherent-ga-path-sum-1'
R160_NUMERICAL_ARTIFACT_AUTHORITY_VERSION = 'r160-numerical-hybrid-response-1'
R160_NUMERICAL_SCHEMA_VERSION = 1

R130_PHASOR_CONVENTION = 'exp(-i*omega*t)'
R130_ANALYSIS_FOURIER_KERNEL = 'exp(+i*omega*t)'
COMMON_PHASOR_CONVENTION = R150_PHASOR_CONVENTION
COMMON_ANALYSIS_FOURIER_KERNEL = 'exp(-i*omega*t)'
COMMON_TIME_ORIGIN = 'source_t0'
R130_TIME_ORIGIN = 'finite_record_sample_0_unit_source_impulse'
R130_TRANSFER_DEFINITION = 'finite_record_direct_dtft_P_over_Q_exp_plus_iwt'
R130_SOURCE_INJECTION_MAPPING = (
    'unit_discrete_volume_velocity_impulse_for_transfer_then_exact_Q_spectrum'
)
COMMON_SOURCE_NORMALIZATION = 'unit_volume_velocity_m3_s'
R130_PRESSURE_REFERENCE = (
    'absolute complex acoustic pressure from finite-record P/Q '
    'transfer multiplied by exact AcousticWaveExcitationAuthority Q(f)'
)

HybridNumericalCapability = Literal['COMPLEX_SUPPORTED', 'UNSUPPORTED']
HybridWeightLaw = Literal['linear_frequency_complementary_v1']


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _semantic_hash(payload: object) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


def _phase(value: complex) -> float:
    return atan2(value.imag, value.real)


def _finite(value: float, *, label: str) -> float:
    result = float(value)
    if not isfinite(result):
        raise ValueError(f'{label} must be finite')
    return result


def _ref_key(ref: ExactExternalAuthorityRef) -> tuple[str, str, str]:
    return (
        ref.authority_id,
        ref.authority_version,
        ref.semantic_hash_sha256,
    )


def _excitation_ref(
    excitation: AcousticWaveExcitationAuthority,
) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=excitation.excitation_id,
        authority_version=excitation.authority_version,
        semantic_hash_sha256=excitation.semantic_sha256,
    )


def _response_ref(
    response: DeterministicPathFrequencyResponseArtifact,
) -> ExactExternalAuthorityRef:
    return response.as_external_ref()


def convert_complex_phasor(
    value: complex,
    *,
    input_convention: str,
    output_convention: str,
) -> complex:
    """Convert one physical complex amplitude between the two supported phasors."""

    if input_convention == output_convention:
        return value
    supported = {R130_PHASOR_CONVENTION, COMMON_PHASOR_CONVENTION}
    if {input_convention, output_convention} == supported:
        return value.conjugate()
    raise ValueError(
        'unsupported phasor conversion: '
        f'{input_convention!r} -> {output_convention!r}'
    )


class ExactSolverResultIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    result_id: str = Field(pattern=r'^acoustic-solver-result:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class ExactCandidateInputIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    execution_input_id: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class HybridConventionNormalizationAuthority(BaseModel):
    """Versioned R130 absolute-pressure -> common p/Q conversion authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r160-complex-convention-normalization-1'
    ] = R160_CONVENTION_AUTHORITY_VERSION
    authority_id: str = Field(
        pattern=r'^r160-convention-normalization:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    wave_input_quantity: Literal['complex_pressure'] = 'complex_pressure'
    wave_input_unit: Literal['Pa'] = 'Pa'
    wave_input_reference: Literal[
        'absolute complex acoustic pressure from finite-record P/Q transfer multiplied by exact AcousticWaveExcitationAuthority Q(f)'
    ] = R130_PRESSURE_REFERENCE
    wave_input_phasor_convention: Literal[
        'exp(-i*omega*t)'
    ] = R130_PHASOR_CONVENTION
    wave_analysis_fourier_kernel: Literal[
        'exp(+i*omega*t)'
    ] = R130_ANALYSIS_FOURIER_KERNEL
    wave_source_quantity: Literal[
        'complex_volume_velocity_m3_s'
    ] = 'complex_volume_velocity_m3_s'
    wave_source_phasor_convention: Literal[
        'exp(-i*omega*t)'
    ] = R130_PHASOR_CONVENTION
    wave_time_origin: Literal[
        'finite_record_sample_0_unit_source_impulse'
    ] = R130_TIME_ORIGIN
    wave_transfer_definition: Literal[
        'finite_record_direct_dtft_P_over_Q_exp_plus_iwt'
    ] = R130_TRANSFER_DEFINITION
    wave_source_injection_mapping: Literal[
        'unit_discrete_volume_velocity_impulse_for_transfer_then_exact_Q_spectrum'
    ] = R130_SOURCE_INJECTION_MAPPING

    ga_input_quantity: Literal[
        'complex_acoustic_pressure_per_volume_velocity'
    ] = TRANSFER_QUANTITY
    ga_input_unit: Literal['Pa/(m3/s)'] = TRANSFER_UNIT
    ga_input_source_normalization: Literal[
        'unit_volume_velocity_m3_s'
    ] = COMMON_SOURCE_NORMALIZATION
    ga_input_phasor_convention: Literal[
        'exp(+i*omega*t)'
    ] = COMMON_PHASOR_CONVENTION
    ga_input_time_origin: Literal['source_t0'] = COMMON_TIME_ORIGIN

    conversion_operation: Literal[
        'divide_pressure_by_exact_Q_then_complex_conjugate'
    ] = 'divide_pressure_by_exact_Q_then_complex_conjugate'
    time_origin_conversion: Literal[
        'finite_record_sample_0_source_impulse_equals_source_t0_no_shift'
    ] = 'finite_record_sample_0_source_impulse_equals_source_t0_no_shift'
    common_quantity: Literal[
        'complex_acoustic_pressure_per_volume_velocity'
    ] = TRANSFER_QUANTITY
    common_unit: Literal['Pa/(m3/s)'] = TRANSFER_UNIT
    common_source_normalization: Literal[
        'unit_volume_velocity_m3_s'
    ] = COMMON_SOURCE_NORMALIZATION
    common_phasor_convention: Literal[
        'exp(+i*omega*t)'
    ] = COMMON_PHASOR_CONVENTION
    common_analysis_fourier_kernel: Literal[
        'exp(-i*omega*t)'
    ] = COMMON_ANALYSIS_FOURIER_KERNEL
    common_time_origin: Literal['source_t0'] = COMMON_TIME_ORIGIN

    @model_validator(mode='after')
    def identity(self) -> 'HybridConventionNormalizationAuthority':
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R160 convention authority semantic hash mismatch')
        if self.authority_id != f'r160-convention-normalization:{expected}':
            raise ValueError('R160 convention authority id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'authority_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.authority_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def build_hybrid_convention_normalization_authority(
) -> HybridConventionNormalizationAuthority:
    core = {
        'authority_version': R160_CONVENTION_AUTHORITY_VERSION,
        'wave_input_quantity': 'complex_pressure',
        'wave_input_unit': 'Pa',
        'wave_input_reference': R130_PRESSURE_REFERENCE,
        'wave_input_phasor_convention': R130_PHASOR_CONVENTION,
        'wave_analysis_fourier_kernel': R130_ANALYSIS_FOURIER_KERNEL,
        'wave_source_quantity': 'complex_volume_velocity_m3_s',
        'wave_source_phasor_convention': R130_PHASOR_CONVENTION,
        'wave_time_origin': R130_TIME_ORIGIN,
        'wave_transfer_definition': R130_TRANSFER_DEFINITION,
        'wave_source_injection_mapping': R130_SOURCE_INJECTION_MAPPING,
        'ga_input_quantity': TRANSFER_QUANTITY,
        'ga_input_unit': TRANSFER_UNIT,
        'ga_input_source_normalization': COMMON_SOURCE_NORMALIZATION,
        'ga_input_phasor_convention': COMMON_PHASOR_CONVENTION,
        'ga_input_time_origin': COMMON_TIME_ORIGIN,
        'conversion_operation': (
            'divide_pressure_by_exact_Q_then_complex_conjugate'
        ),
        'time_origin_conversion': (
            'finite_record_sample_0_source_impulse_equals_source_t0_no_shift'
        ),
        'common_quantity': TRANSFER_QUANTITY,
        'common_unit': TRANSFER_UNIT,
        'common_source_normalization': COMMON_SOURCE_NORMALIZATION,
        'common_phasor_convention': COMMON_PHASOR_CONVENTION,
        'common_analysis_fourier_kernel': COMMON_ANALYSIS_FOURIER_KERNEL,
        'common_time_origin': COMMON_TIME_ORIGIN,
    }
    digest = _semantic_hash(core)
    return HybridConventionNormalizationAuthority(
        authority_id=f'r160-convention-normalization:{digest}',
        semantic_sha256=digest,
        **core,
    )


class NumericalHybridCompositionSpec(BaseModel):
    """Numerical composition request over an explicit, authority-bound output grid."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r160-numerical-hybrid-composition-spec-1'
    ] = R160_NUMERICAL_SPEC_AUTHORITY_VERSION
    composition_spec_id: str = Field(
        pattern=r'^r160-numerical-composition-spec:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    r130_result: ExactSolverResultIdentity
    r130_complex_pressure_artifact_ref: ExactExternalAuthorityRef
    r130_candidate_input: ExactCandidateInputIdentity
    wave_excitation_ref: ExactExternalAuthorityRef
    r150_response_refs: tuple[ExactExternalAuthorityRef, ...] = Field(
        min_length=1
    )

    source_entity_id: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)
    exact_frequency_grid_hz: tuple[float, ...] = Field(min_length=2)

    quantity: Literal[
        'complex_acoustic_pressure_per_volume_velocity'
    ] = TRANSFER_QUANTITY
    unit: Literal['Pa/(m3/s)'] = TRANSFER_UNIT
    common_phasor_convention: Literal[
        'exp(+i*omega*t)'
    ] = COMMON_PHASOR_CONVENTION
    common_analysis_fourier_kernel: Literal[
        'exp(-i*omega*t)'
    ] = COMMON_ANALYSIS_FOURIER_KERNEL
    source_normalization: Literal[
        'unit_volume_velocity_m3_s'
    ] = COMMON_SOURCE_NORMALIZATION
    time_origin: Literal['source_t0'] = COMMON_TIME_ORIGIN

    normalization_authority_ref: ExactExternalAuthorityRef
    grid_reconciliation: FrequencyGridReconciliationAuthority
    crossover_configuration: HybridCrossoverConfigurationAuthority
    transition_start_hz: float = Field(gt=0.0)
    transition_end_hz: float = Field(gt=0.0)
    weight_law: HybridWeightLaw = 'linear_frequency_complementary_v1'

    @model_validator(mode='after')
    def contract(self) -> 'NumericalHybridCompositionSpec':
        grid = tuple(float(item) for item in self.exact_frequency_grid_hz)
        if grid != tuple(sorted(set(grid))):
            raise ValueError('R160 numerical frequency grid must be sorted/unique')
        if not all(isfinite(item) and item > 0.0 for item in grid):
            raise ValueError('R160 numerical frequency grid must be finite/positive')
        if self.transition_start_hz >= self.transition_end_hz:
            raise ValueError('R160 transition requires start < end')
        if (
            self.transition_start_hz < grid[0]
            or self.transition_end_hz > grid[-1]
        ):
            raise ValueError('R160 transition endpoints must lie inside output grid domain')
        if grid != self.grid_reconciliation.requested_output_frequency_grid_hz:
            raise ValueError('R160 output grid does not match reconciliation authority')
        crossover = self.crossover_configuration
        if (
            self.transition_start_hz != crossover.overlap_lower_hz
            or self.transition_end_hz != crossover.overlap_upper_hz
            or self.weight_law != crossover.blend_law
        ):
            raise ValueError('R160 transition does not match crossover authority')
        if (
            self.grid_reconciliation.wave_valid_input_band_hz
            != crossover.wave_validity_band_hz
            or self.grid_reconciliation.ga_valid_input_band_hz
            != crossover.ga_validity_band_hz
        ):
            raise ValueError('R160 crossover validity bands do not match reconciliation authority')
        ref_keys = tuple(_ref_key(item) for item in self.r150_response_refs)
        if ref_keys != tuple(sorted(set(ref_keys))):
            raise ValueError('R160 R150 response refs must be unique/canonically sorted')
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R160 numerical composition spec semantic hash mismatch')
        if (
            self.composition_spec_id
            != f'r160-numerical-composition-spec:{expected}'
        ):
            raise ValueError('R160 numerical composition spec id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'composition_spec_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.composition_spec_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


class AggregatedGaComplexSample(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    complex_real_pa_per_m3_s: float
    complex_imag_pa_per_m3_s: float
    magnitude_pa_per_m3_s: float = Field(ge=0.0)
    phase_rad: float

    @model_validator(mode='after')
    def consistent(self) -> 'AggregatedGaComplexSample':
        value = complex(
            _finite(self.complex_real_pa_per_m3_s, label='GA real'),
            _finite(self.complex_imag_pa_per_m3_s, label='GA imag'),
        )
        if not isclose(
            abs(value),
            float(self.magnitude_pa_per_m3_s),
            rel_tol=1e-10,
            abs_tol=1e-12,
        ):
            raise ValueError('R160 GA aggregate magnitude mismatch')
        delta = _phase(value) - float(self.phase_rad)
        if abs(atan2(sin(delta), cos(delta))) > 1e-10:
            raise ValueError('R160 GA aggregate phase mismatch')
        return self


class AggregatedGaComplexResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r160-coherent-ga-path-sum-1'
    ] = R160_GA_AGGREGATION_AUTHORITY_VERSION
    aggregate_id: str = Field(
        pattern=r'^r160-ga-complex-aggregate:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    path_response_refs: tuple[ExactExternalAuthorityRef, ...] = Field(min_length=1)
    source_entity_id: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)
    exact_frequency_grid_hz: tuple[float, ...] = Field(min_length=2)

    quantity: Literal[
        'complex_acoustic_pressure_per_volume_velocity'
    ] = TRANSFER_QUANTITY
    unit: Literal['Pa/(m3/s)'] = TRANSFER_UNIT
    source_normalization: Literal[
        'unit_volume_velocity_m3_s'
    ] = COMMON_SOURCE_NORMALIZATION
    phasor_convention: Literal[
        'exp(+i*omega*t)'
    ] = COMMON_PHASOR_CONVENTION
    time_origin: Literal['source_t0'] = COMMON_TIME_ORIGIN

    capability_state: HybridNumericalCapability
    failure_codes: tuple[HybridNumericalFailureCode, ...] = ()
    unsupported_reasons: tuple[str, ...] = ()
    samples: tuple[AggregatedGaComplexSample, ...] = ()

    @model_validator(mode='after')
    def contract(self) -> 'AggregatedGaComplexResponse':
        if self.capability_state == 'COMPLEX_SUPPORTED':
            if self.unsupported_reasons:
                raise ValueError('supported GA aggregate cannot carry unsupported reasons')
            if tuple(item.frequency_hz for item in self.samples) != self.exact_frequency_grid_hz:
                raise ValueError('supported GA aggregate must cover exact grid')
        else:
            if not self.unsupported_reasons or self.samples:
                raise ValueError('unsupported GA aggregate requires reasons and no samples')
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R160 GA aggregate semantic hash mismatch')
        if self.aggregate_id != f'r160-ga-complex-aggregate:{expected}':
            raise ValueError('R160 GA aggregate id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'aggregate_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.aggregate_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


class NumericalHybridResponseSample(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    low_weight: float = Field(ge=0.0, le=1.0)
    high_weight: float = Field(ge=0.0, le=1.0)

    wave_complex_real_pa_per_m3_s: float
    wave_complex_imag_pa_per_m3_s: float
    ga_complex_real_pa_per_m3_s: float
    ga_complex_imag_pa_per_m3_s: float
    complex_real_pa_per_m3_s: float
    complex_imag_pa_per_m3_s: float
    magnitude_pa_per_m3_s: float = Field(ge=0.0)
    phase_rad: float

    @model_validator(mode='after')
    def consistent(self) -> 'NumericalHybridResponseSample':
        if not isclose(
            float(self.low_weight) + float(self.high_weight),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError('R160 complementary weights must sum to one')
        wave = complex(
            self.wave_complex_real_pa_per_m3_s,
            self.wave_complex_imag_pa_per_m3_s,
        )
        ga = complex(
            self.ga_complex_real_pa_per_m3_s,
            self.ga_complex_imag_pa_per_m3_s,
        )
        expected = self.low_weight * wave + self.high_weight * ga
        actual = complex(
            self.complex_real_pa_per_m3_s,
            self.complex_imag_pa_per_m3_s,
        )
        if abs(expected - actual) > max(1e-12, abs(expected) * 1e-10):
            raise ValueError('R160 hybrid sample does not equal complementary complex blend')
        if not isclose(
            abs(actual),
            self.magnitude_pa_per_m3_s,
            rel_tol=1e-10,
            abs_tol=1e-12,
        ):
            raise ValueError('R160 hybrid magnitude mismatch')
        delta = _phase(actual) - float(self.phase_rad)
        if abs(atan2(sin(delta), cos(delta))) > 1e-10:
            raise ValueError('R160 hybrid phase mismatch')
        return self


class NumericalHybridResponseArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R160_NUMERICAL_SCHEMA_VERSION
    authority_version: Literal[
        'r160-numerical-hybrid-response-1'
    ] = R160_NUMERICAL_ARTIFACT_AUTHORITY_VERSION
    artifact_id: str = Field(
        pattern=r'^r160-numerical-hybrid-response:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    composition_spec: NumericalHybridCompositionSpec
    exact_r130_result: ExactSolverResultIdentity
    exact_r130_artifact_ref: ExactExternalAuthorityRef
    exact_r150_response_refs: tuple[ExactExternalAuthorityRef, ...] = Field(
        min_length=1
    )
    exact_aggregated_ga_identity: ExactExternalAuthorityRef
    aggregated_ga: AggregatedGaComplexResponse
    grid_reconciliation: FrequencyGridReconciliationAuthority
    crossover_configuration: HybridCrossoverConfigurationAuthority

    exact_frequency_grid_hz: tuple[float, ...] = Field(min_length=2)
    quantity: Literal[
        'complex_acoustic_pressure_per_volume_velocity'
    ] = TRANSFER_QUANTITY
    unit: Literal['Pa/(m3/s)'] = TRANSFER_UNIT
    common_phasor_convention: Literal[
        'exp(+i*omega*t)'
    ] = COMMON_PHASOR_CONVENTION
    common_analysis_fourier_kernel: Literal[
        'exp(-i*omega*t)'
    ] = COMMON_ANALYSIS_FOURIER_KERNEL
    source_normalization: Literal[
        'unit_volume_velocity_m3_s'
    ] = COMMON_SOURCE_NORMALIZATION
    time_origin: Literal['source_t0'] = COMMON_TIME_ORIGIN

    transition_start_hz: float
    transition_end_hz: float
    weight_law: HybridWeightLaw

    capability_state: HybridNumericalCapability
    unsupported_reasons: tuple[str, ...] = ()
    samples: tuple[NumericalHybridResponseSample, ...] = ()

    @model_validator(mode='after')
    def contract(self) -> 'NumericalHybridResponseArtifact':
        if self.composition_spec.r130_result != self.exact_r130_result:
            raise ValueError('R160 output R130 result identity mismatch')
        if (
            self.composition_spec.r130_complex_pressure_artifact_ref
            != self.exact_r130_artifact_ref
        ):
            raise ValueError('R160 output R130 artifact identity mismatch')
        if (
            self.composition_spec.r150_response_refs
            != self.exact_r150_response_refs
        ):
            raise ValueError('R160 output R150 response identity mismatch')
        if self.aggregated_ga.as_external_ref() != self.exact_aggregated_ga_identity:
            raise ValueError('R160 output GA aggregate identity mismatch')
        if self.exact_frequency_grid_hz != self.composition_spec.exact_frequency_grid_hz:
            raise ValueError('R160 output frequency grid mismatch')
        if self.grid_reconciliation != self.composition_spec.grid_reconciliation:
            raise ValueError('R160 output reconciliation authority mismatch')
        if self.crossover_configuration != self.composition_spec.crossover_configuration:
            raise ValueError('R160 output crossover configuration mismatch')
        if (
            self.transition_start_hz != self.composition_spec.transition_start_hz
            or self.transition_end_hz != self.composition_spec.transition_end_hz
            or self.weight_law != self.composition_spec.weight_law
        ):
            raise ValueError('R160 output crossover authority mismatch')
        if self.capability_state == 'COMPLEX_SUPPORTED':
            if self.failure_codes or self.unsupported_reasons:
                raise ValueError('supported R160 output cannot carry failure metadata')
            if tuple(item.frequency_hz for item in self.samples) != self.exact_frequency_grid_hz:
                raise ValueError('supported R160 output must cover exact grid')
        else:
            if not self.failure_codes or not self.unsupported_reasons or self.samples:
                raise ValueError('unsupported R160 output requires failure codes/reasons and no samples')
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R160 numerical hybrid semantic hash mismatch')
        if self.artifact_id != f'r160-numerical-hybrid-response:{expected}':
            raise ValueError('R160 numerical hybrid id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'artifact_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.artifact_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def _complex_pressure_manifest(result: AcousticSolverResultEnvelope):
    matches = [item for item in result.artifacts if item.observable == 'complex_pressure']
    if len(matches) != 1:
        raise ValueError('R160 requires exactly one R130 complex_pressure artifact')
    return matches[0]


def _validate_wave_inputs(
    *,
    result: AcousticSolverResultEnvelope,
    payload: Any,
    candidate_input: CandidateWaveExecutionInput,
    excitation: AcousticWaveExcitationAuthority,
    receiver_id: str,
) -> tuple[int, tuple[float, ...]]:
    manifest = _complex_pressure_manifest(result)
    if not isinstance(payload, dict):
        raise ValueError('R130 complex-pressure artifact payload must be a mapping')
    if _semantic_hash(payload) != manifest.artifact_authority.semantic_hash_sha256:
        raise ValueError('R130 complex-pressure artifact payload hash mismatch')
    if payload.get('schema_version') != COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION:
        raise ValueError('R130 complex-pressure artifact schema mismatch')
    if payload.get('quantity_type') != 'complex_pressure':
        raise ValueError('R130 artifact quantity mismatch')
    if payload.get('units') != 'Pa' or payload.get('reference') != R130_PRESSURE_REFERENCE:
        raise ValueError('R130 pressure units/reference are unsupported')
    representation = payload.get('complex_representation')
    if not isinstance(representation, dict):
        raise ValueError('R130 complex representation metadata is missing')
    if (
        representation.get('form') != 'cartesian_real_imag'
        or representation.get('phasor_convention') != R130_PHASOR_CONVENTION
        or representation.get('analysis_fourier_kernel')
        != R130_ANALYSIS_FOURIER_KERNEL
    ):
        raise ValueError('R130 complex/Fourier convention is not explicitly supported')
    time_sampling = payload.get('time_sampling')
    if (
        not isinstance(time_sampling, dict)
        or time_sampling.get('finite_record_interval') != '[0,T)'
        or not isinstance(time_sampling.get('time_step_s'), (int, float))
        or float(time_sampling['time_step_s']) <= 0.0
        or not isinstance(time_sampling.get('sample_count'), int)
        or int(time_sampling['sample_count']) < 1
    ):
        raise ValueError(
            'R130 time-origin authority requires exact finite-record [0,T) metadata'
        )
    if (
        payload.get('candidate_execution_input_id')
        != candidate_input.execution_input_id
        or payload.get('candidate_execution_input_sha256')
        != candidate_input.semantic_sha256
    ):
        raise ValueError('R130 candidate execution input identity mismatch')
    source = payload.get('source_authority')
    if not isinstance(source, dict):
        raise ValueError('R130 source authority binding is missing')
    if (
        source.get('r110_compiled_source_sha256')
        != candidate_input.r110_compiled_source_sha256
        or source.get('wave_excitation_binding_sha256')
        != candidate_input.wave_excitation_binding_sha256
        or source.get('wave_excitation_sha256')
        != candidate_input.wave_excitation_sha256
    ):
        raise ValueError('R130 source authority is stale for candidate input')
    if (
        excitation.excitation_id != candidate_input.wave_excitation_id
        or excitation.semantic_sha256 != candidate_input.wave_excitation_sha256
        or excitation.quantity != 'complex_volume_velocity_m3_s'
        or excitation.phasor_convention != R130_PHASOR_CONVENTION
        or excitation.excitation_model
        != 'equivalent_monopole_volume_velocity_at_equipment_acoustic_reference'
    ):
        raise ValueError('R130 exact wave excitation is incompatible/stale')

    receiver_order = payload.get('receiver_identity_order')
    if not isinstance(receiver_order, list):
        raise ValueError('R130 receiver identity order is missing')
    receiver_indices = [
        index
        for index, item in enumerate(receiver_order)
        if isinstance(item, dict) and item.get('receiver_id') == receiver_id
    ]
    if len(receiver_indices) != 1:
        raise ValueError('R130 requested receiver identity is not exact/unique')
    candidate_receiver = next(
        (item for item in candidate_input.receivers if item.receiver_id == receiver_id),
        None,
    )
    if candidate_receiver is None:
        raise ValueError('R130 candidate input does not bind requested receiver')
    receiver_payload = receiver_order[receiver_indices[0]]
    if (
        receiver_payload.get('entity_id') != candidate_receiver.entity_id
        or tuple(receiver_payload.get('position_m', ()))
        != tuple(candidate_receiver.position_m)
    ):
        raise ValueError('R130 receiver identity/position is stale')

    frequencies = tuple(float(item) for item in payload.get('frequency_axis_hz', ()))
    if frequencies != tuple(sorted(set(frequencies))) or len(frequencies) < 2:
        raise ValueError('R130 frequency axis must be exact sorted unique bins')
    if frequencies != tuple(
        float(item) for item in candidate_input.frequency_samples_hz
    ):
        raise ValueError(
            'R130 artifact frequency axis is stale for exact candidate input'
        )
    real_rows = payload.get('pressure_real_pa')
    imag_rows = payload.get('pressure_imag_pa')
    if (
        not isinstance(real_rows, list)
        or not isinstance(imag_rows, list)
        or len(real_rows) != len(receiver_order)
        or len(imag_rows) != len(receiver_order)
    ):
        raise ValueError('R130 complex-pressure matrix shape is invalid')
    for rows in (real_rows, imag_rows):
        if any(not isinstance(row, list) or len(row) != len(frequencies) for row in rows):
            raise ValueError('R130 complex-pressure frequency dimension mismatch')
    return receiver_indices[0], frequencies


def _weights(
    frequency_hz: float,
    *,
    start_hz: float,
    end_hz: float,
) -> tuple[float, float]:
    if frequency_hz <= start_hz:
        return 1.0, 0.0
    if frequency_hz >= end_hz:
        return 0.0, 1.0
    high = (frequency_hz - start_hz) / (end_hz - start_hz)
    return 1.0 - high, high


def build_numerical_hybrid_composition_spec(
    *,
    r130_result: AcousticSolverResultEnvelope,
    r130_artifact_payload: Any,
    r130_candidate_input: CandidateWaveExecutionInput,
    wave_excitation: AcousticWaveExcitationAuthority,
    r150_responses: Sequence[DeterministicPathFrequencyResponseArtifact],
    receiver_id: str,
    exact_frequency_grid_hz: Sequence[float],
    transition_start_hz: float,
    transition_end_hz: float,
    normalization_authority: HybridConventionNormalizationAuthority | None = None,
) -> NumericalHybridCompositionSpec:
    result = AcousticSolverResultEnvelope.model_validate(
        r130_result.model_dump(mode='python')
    )
    candidate = CandidateWaveExecutionInput.model_validate(
        r130_candidate_input.model_dump(mode='python')
    )
    excitation = AcousticWaveExcitationAuthority.model_validate(
        wave_excitation.model_dump(mode='python')
    )
    responses = tuple(
        DeterministicPathFrequencyResponseArtifact.model_validate(
            item.model_dump(mode='python')
        )
        for item in r150_responses
    )
    if not responses:
        raise ValueError('R160 numerical composition requires at least one R150 path response')

    manifest = _complex_pressure_manifest(result)
    _, wave_grid = _validate_wave_inputs(
        result=result,
        payload=r130_artifact_payload,
        candidate_input=candidate,
        excitation=excitation,
        receiver_id=receiver_id,
    )
    grid = tuple(float(item) for item in exact_frequency_grid_hz)
    if grid != tuple(sorted(set(grid))) or len(grid) < 2:
        raise ValueError('R160 exact shared grid must contain sorted unique bins')
    if any(item not in wave_grid for item in grid):
        raise ValueError('R160 refuses R130 frequency interpolation/resampling')
    excitation_grid = {float(item.frequency_hz) for item in excitation.samples}
    if any(item not in excitation_grid for item in grid):
        raise ValueError('R160 requires exact Q(f) samples; interpolation is not authorized')

    source_ids = {item.source_entity_id for item in responses}
    receiver_ids = {item.receiver_id for item in responses}
    receiver_entity_ids = {item.receiver_entity_id for item in responses}
    candidate_receiver = next(
        (item for item in candidate.receivers if item.receiver_id == receiver_id),
        None,
    )
    if source_ids != {candidate.source_entity_id}:
        raise ValueError('R160 R130/R150 source identity mismatch')
    if receiver_ids != {receiver_id} or candidate_receiver is None:
        raise ValueError('R160 R130/R150 receiver identity mismatch')
    if receiver_entity_ids != {candidate_receiver.entity_id}:
        raise ValueError('R160 R130/R150 receiver entity identity mismatch')

    for item in responses:
        if not any(
            ref.authority_id == candidate.compiled_geometry_id
            and ref.semantic_hash_sha256 == candidate.compiled_geometry_sha256
            for ref in item.dependency_refs
        ):
            raise ValueError(
                'R160 R150 response does not bind the exact R130 compiled geometry'
            )
        if not any(
            ref.semantic_hash_sha256 == candidate.r110_compiled_source_sha256
            for ref in item.dependency_refs
        ):
            raise ValueError(
                'R160 R150 response does not bind the exact R130 R110 source'
            )

    path_artifact_ids = {
        (item.deterministic_path_artifact_id, item.deterministic_path_artifact_sha256)
        for item in responses
    }
    if len(path_artifact_ids) != 1:
        raise ValueError('R160 R150 responses must bind one exact deterministic path artifact')
    for item in responses:
        if (
            item.quantity != TRANSFER_QUANTITY
            or item.unit != TRANSFER_UNIT
            or item.source_normalization != COMMON_SOURCE_NORMALIZATION
            or item.phasor_convention != COMMON_PHASOR_CONVENTION
            or item.time_origin != COMMON_TIME_ORIGIN
        ):
            raise ValueError('R160 R150 physical convention mismatch')
        if any(frequency not in item.exact_frequency_grid_hz for frequency in grid):
            raise ValueError('R160 refuses R150 frequency interpolation/resampling')

    normalization = (
        build_hybrid_convention_normalization_authority()
        if normalization_authority is None
        else HybridConventionNormalizationAuthority.model_validate(
            normalization_authority.model_dump(mode='python')
        )
    )
    response_refs = tuple(
        sorted((_response_ref(item) for item in responses), key=_ref_key)
    )
    core = {
        'authority_version': R160_NUMERICAL_SPEC_AUTHORITY_VERSION,
        'r130_result': {
            'result_id': result.result_id,
            'semantic_sha256': result.semantic_sha256,
        },
        'r130_complex_pressure_artifact_ref': manifest.artifact_authority.model_dump(
            mode='json'
        ),
        'r130_candidate_input': {
            'execution_input_id': candidate.execution_input_id,
            'authority_version': candidate.authority_version,
            'semantic_sha256': candidate.semantic_sha256,
        },
        'wave_excitation_ref': _excitation_ref(excitation).model_dump(mode='json'),
        'r150_response_refs': [
            item.model_dump(mode='json') for item in response_refs
        ],
        'source_entity_id': candidate.source_entity_id,
        'receiver_id': receiver_id,
        'exact_frequency_grid_hz': list(grid),
        'quantity': TRANSFER_QUANTITY,
        'unit': TRANSFER_UNIT,
        'common_phasor_convention': COMMON_PHASOR_CONVENTION,
        'common_analysis_fourier_kernel': COMMON_ANALYSIS_FOURIER_KERNEL,
        'source_normalization': COMMON_SOURCE_NORMALIZATION,
        'time_origin': COMMON_TIME_ORIGIN,
        'normalization_authority_ref': normalization.as_external_ref().model_dump(
            mode='json'
        ),
        'transition_start_hz': float(transition_start_hz),
        'transition_end_hz': float(transition_end_hz),
        'weight_law': 'linear_frequency_complementary_v1',
    }
    digest = _semantic_hash(core)
    return NumericalHybridCompositionSpec(
        composition_spec_id=f'r160-numerical-composition-spec:{digest}',
        semantic_sha256=digest,
        **core,
    )


def aggregate_r150_complex_paths(
    *,
    spec: NumericalHybridCompositionSpec,
    responses: Sequence[DeterministicPathFrequencyResponseArtifact],
) -> AggregatedGaComplexResponse:
    response_tuple = tuple(responses)
    refs = tuple(sorted((_response_ref(item) for item in response_tuple), key=_ref_key))
    if refs != spec.r150_response_refs:
        raise ValueError('R160 R150 response set is stale or incomplete')

    path_ids = tuple(item.deterministic_path_id for item in response_tuple)
    if len(set(path_ids)) != len(path_ids):
        raise ValueError('R160 refuses duplicate deterministic path identity')

    reasons: list[str] = []
    for item in response_tuple:
        if item.capability != 'COMPLEX_SUPPORTED':
            reasons.append(
                f'{item.deterministic_path_id}:{item.capability}:'
                + (
                    ','.join(item.unsupported_reasons)
                    if item.unsupported_reasons
                    else 'coherent complex samples unavailable'
                )
            )

    core_base = {
        'authority_version': R160_GA_AGGREGATION_AUTHORITY_VERSION,
        'path_response_refs': [
            item.model_dump(mode='json') for item in refs
        ],
        'source_entity_id': spec.source_entity_id,
        'receiver_id': spec.receiver_id,
        'exact_frequency_grid_hz': list(spec.exact_frequency_grid_hz),
        'quantity': TRANSFER_QUANTITY,
        'unit': TRANSFER_UNIT,
        'source_normalization': COMMON_SOURCE_NORMALIZATION,
        'phasor_convention': COMMON_PHASOR_CONVENTION,
        'time_origin': COMMON_TIME_ORIGIN,
    }
    if reasons:
        core = {
            **core_base,
            'capability_state': 'UNSUPPORTED',
            'unsupported_reasons': sorted(set(reasons)),
            'samples': [],
        }
    else:
        sums = {frequency: 0.0 + 0.0j for frequency in spec.exact_frequency_grid_hz}
        for response in response_tuple:
            samples = {item.frequency_hz: item for item in response.samples}
            for frequency in spec.exact_frequency_grid_hz:
                sample = samples.get(frequency)
                if (
                    sample is None
                    or sample.complex_real_pa_per_m3_s is None
                    or sample.complex_imag_pa_per_m3_s is None
                ):
                    raise ValueError('R160 exact R150 complex sample is missing')
                sums[frequency] += complex(
                    sample.complex_real_pa_per_m3_s,
                    sample.complex_imag_pa_per_m3_s,
                )
        aggregate_samples = [
            {
                'frequency_hz': frequency,
                'complex_real_pa_per_m3_s': sums[frequency].real,
                'complex_imag_pa_per_m3_s': sums[frequency].imag,
                'magnitude_pa_per_m3_s': abs(sums[frequency]),
                'phase_rad': _phase(sums[frequency]),
            }
            for frequency in spec.exact_frequency_grid_hz
        ]
        core = {
            **core_base,
            'capability_state': 'COMPLEX_SUPPORTED',
            'unsupported_reasons': [],
            'samples': aggregate_samples,
        }
    digest = _semantic_hash(core)
    return AggregatedGaComplexResponse(
        aggregate_id=f'r160-ga-complex-aggregate:{digest}',
        semantic_sha256=digest,
        **core,
    )


def _normalized_wave_transfer(
    *,
    spec: NumericalHybridCompositionSpec,
    payload: dict[str, Any],
    excitation: AcousticWaveExcitationAuthority,
) -> dict[float, complex]:
    frequencies = tuple(float(item) for item in payload['frequency_axis_hz'])
    index_by_frequency = {item: index for index, item in enumerate(frequencies)}
    receiver_order = payload['receiver_identity_order']
    receiver_index = next(
        index
        for index, item in enumerate(receiver_order)
        if item['receiver_id'] == spec.receiver_id
    )
    pressure_real = payload['pressure_real_pa'][receiver_index]
    pressure_imag = payload['pressure_imag_pa'][receiver_index]
    q_by_frequency = {
        float(item.frequency_hz): complex(item.real_m3_s, item.imag_m3_s)
        for item in excitation.samples
    }

    normalized: dict[float, complex] = {}
    for frequency in spec.exact_frequency_grid_hz:
        pressure_index = index_by_frequency.get(frequency)
        q = q_by_frequency.get(frequency)
        if pressure_index is None or q is None:
            raise ValueError('R160 exact shared wave/Q frequency sample is missing')
        if abs(q) <= 1e-18:
            raise ValueError('R160 source-normalization conversion rejects zero Q(f)')
        pressure = complex(
            pressure_real[pressure_index],
            pressure_imag[pressure_index],
        )
        transfer_r130 = pressure / q
        normalized[frequency] = convert_complex_phasor(
            transfer_r130,
            input_convention=R130_PHASOR_CONVENTION,
            output_convention=COMMON_PHASOR_CONVENTION,
        )
    return normalized


def compose_numerical_hybrid_response(
    *,
    spec: NumericalHybridCompositionSpec,
    r130_result: AcousticSolverResultEnvelope,
    r130_artifact_payload: Any,
    r130_candidate_input: CandidateWaveExecutionInput,
    wave_excitation: AcousticWaveExcitationAuthority,
    r150_responses: Sequence[DeterministicPathFrequencyResponseArtifact],
    normalization_authority: HybridConventionNormalizationAuthority | None = None,
) -> NumericalHybridResponseArtifact:
    spec = NumericalHybridCompositionSpec.model_validate(
        spec.model_dump(mode='python')
    )
    normalization = (
        build_hybrid_convention_normalization_authority()
        if normalization_authority is None
        else HybridConventionNormalizationAuthority.model_validate(
            normalization_authority.model_dump(mode='python')
        )
    )
    if normalization.as_external_ref() != spec.normalization_authority_ref:
        raise ValueError('R160 convention-normalization authority is stale')

    expected_spec = build_numerical_hybrid_composition_spec(
        r130_result=r130_result,
        r130_artifact_payload=r130_artifact_payload,
        r130_candidate_input=r130_candidate_input,
        wave_excitation=wave_excitation,
        r150_responses=r150_responses,
        receiver_id=spec.receiver_id,
        exact_frequency_grid_hz=spec.exact_frequency_grid_hz,
        transition_start_hz=spec.transition_start_hz,
        transition_end_hz=spec.transition_end_hz,
        normalization_authority=normalization,
    )
    if expected_spec != spec:
        raise ValueError('R160 numerical composition spec is stale for exact inputs')

    aggregate = aggregate_r150_complex_paths(
        spec=spec,
        responses=r150_responses,
    )
    base = {
        'schema_version': R160_NUMERICAL_SCHEMA_VERSION,
        'authority_version': R160_NUMERICAL_ARTIFACT_AUTHORITY_VERSION,
        'composition_spec': spec.model_dump(mode='json'),
        'exact_r130_result': spec.r130_result.model_dump(mode='json'),
        'exact_r130_artifact_ref': (
            spec.r130_complex_pressure_artifact_ref.model_dump(mode='json')
        ),
        'exact_r150_response_refs': [
            item.model_dump(mode='json') for item in spec.r150_response_refs
        ],
        'exact_aggregated_ga_identity': aggregate.as_external_ref().model_dump(
            mode='json'
        ),
        'aggregated_ga': aggregate.model_dump(mode='json'),
        'exact_frequency_grid_hz': list(spec.exact_frequency_grid_hz),
        'quantity': TRANSFER_QUANTITY,
        'unit': TRANSFER_UNIT,
        'common_phasor_convention': COMMON_PHASOR_CONVENTION,
        'common_analysis_fourier_kernel': COMMON_ANALYSIS_FOURIER_KERNEL,
        'source_normalization': COMMON_SOURCE_NORMALIZATION,
        'time_origin': COMMON_TIME_ORIGIN,
        'transition_start_hz': spec.transition_start_hz,
        'transition_end_hz': spec.transition_end_hz,
        'weight_law': spec.weight_law,
    }

    if aggregate.capability_state != 'COMPLEX_SUPPORTED':
        core = {
            **base,
            'capability_state': 'UNSUPPORTED',
            'unsupported_reasons': [
                'R150 coherent path aggregation unavailable: '
                + '; '.join(aggregate.unsupported_reasons)
            ],
            'samples': [],
        }
    else:
        assert isinstance(r130_artifact_payload, dict)
        wave = _normalized_wave_transfer(
            spec=spec,
            payload=r130_artifact_payload,
            excitation=wave_excitation,
        )
        ga = {
            item.frequency_hz: complex(
                item.complex_real_pa_per_m3_s,
                item.complex_imag_pa_per_m3_s,
            )
            for item in aggregate.samples
        }
        samples: list[dict[str, Any]] = []
        for frequency in spec.exact_frequency_grid_hz:
            low_weight, high_weight = _weights(
                frequency,
                start_hz=spec.transition_start_hz,
                end_hz=spec.transition_end_hz,
            )
            hybrid = low_weight * wave[frequency] + high_weight * ga[frequency]
            samples.append(
                {
                    'frequency_hz': frequency,
                    'low_weight': low_weight,
                    'high_weight': high_weight,
                    'wave_complex_real_pa_per_m3_s': wave[frequency].real,
                    'wave_complex_imag_pa_per_m3_s': wave[frequency].imag,
                    'ga_complex_real_pa_per_m3_s': ga[frequency].real,
                    'ga_complex_imag_pa_per_m3_s': ga[frequency].imag,
                    'complex_real_pa_per_m3_s': hybrid.real,
                    'complex_imag_pa_per_m3_s': hybrid.imag,
                    'magnitude_pa_per_m3_s': abs(hybrid),
                    'phase_rad': _phase(hybrid),
                }
            )
        core = {
            **base,
            'capability_state': 'COMPLEX_SUPPORTED',
            'unsupported_reasons': [],
            'samples': samples,
        }

    digest = _semantic_hash(core)
    return NumericalHybridResponseArtifact(
        artifact_id=f'r160-numerical-hybrid-response:{digest}',
        semantic_sha256=digest,
        **core,
    )


WaveResultResolver = Callable[[str], AcousticSolverResultEnvelope | None]
WaveArtifactPayloadResolver = Callable[[ExactExternalAuthorityRef], Any]
CandidateInputResolver = Callable[[str], CandidateWaveExecutionInput | None]
WaveExcitationResolver = Callable[[str], AcousticWaveExcitationAuthority | None]
R150ResponseResolver = Callable[
    [str], DeterministicPathFrequencyResponseArtifact | None
]
NumericalCompositionSpecResolver = Callable[
    [str], NumericalHybridCompositionSpec | None
]
ConventionAuthorityResolver = Callable[
    [ExactExternalAuthorityRef], HybridConventionNormalizationAuthority | None
]


class CadNumericalHybridResponseRepository:
    """Append-only R160 numerical artifact persistence with exact stale rejection."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        wave_result_resolver: WaveResultResolver,
        wave_artifact_payload_resolver: WaveArtifactPayloadResolver,
        candidate_input_resolver: CandidateInputResolver,
        wave_excitation_resolver: WaveExcitationResolver,
        r150_response_resolver: R150ResponseResolver,
        composition_spec_resolver: NumericalCompositionSpecResolver,
        convention_authority_resolver: ConventionAuthorityResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        self.wave_result_resolver = wave_result_resolver
        self.wave_artifact_payload_resolver = wave_artifact_payload_resolver
        self.candidate_input_resolver = candidate_input_resolver
        self.wave_excitation_resolver = wave_excitation_resolver
        self.r150_response_resolver = r150_response_resolver
        self.composition_spec_resolver = composition_spec_resolver
        self.convention_authority_resolver = convention_authority_resolver
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS r160_numerical_hybrid_responses (
                    artifact_id TEXT PRIMARY KEY,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    composition_spec_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    def _rebuild(
        self,
        artifact: NumericalHybridResponseArtifact,
    ) -> NumericalHybridResponseArtifact:
        artifact = NumericalHybridResponseArtifact.model_validate(
            artifact.model_dump(mode='python')
        )
        spec = self.composition_spec_resolver(
            artifact.composition_spec.composition_spec_id
        )
        if spec is None or spec != artifact.composition_spec:
            raise ValueError('R160 numerical composition authority is missing/stale')
        normalization = self.convention_authority_resolver(
            spec.normalization_authority_ref
        )
        if (
            normalization is None
            or normalization.as_external_ref() != spec.normalization_authority_ref
        ):
            raise ValueError('R160 convention normalization authority is missing/stale')

        result = self.wave_result_resolver(spec.r130_result.result_id)
        if (
            result is None
            or result.semantic_sha256 != spec.r130_result.semantic_sha256
        ):
            raise ValueError('R160 exact R130 result dependency is missing/stale')
        manifest = _complex_pressure_manifest(result)
        if manifest.artifact_authority != spec.r130_complex_pressure_artifact_ref:
            raise ValueError('R160 exact R130 artifact dependency changed')
        payload = self.wave_artifact_payload_resolver(
            spec.r130_complex_pressure_artifact_ref
        )

        candidate = self.candidate_input_resolver(
            spec.r130_candidate_input.execution_input_id
        )
        if (
            candidate is None
            or candidate.semantic_sha256 != spec.r130_candidate_input.semantic_sha256
            or candidate.authority_version
            != spec.r130_candidate_input.authority_version
        ):
            raise ValueError('R160 exact R130 candidate input is missing/stale')

        excitation = self.wave_excitation_resolver(
            spec.wave_excitation_ref.authority_id
        )
        if (
            excitation is None
            or _excitation_ref(excitation) != spec.wave_excitation_ref
        ):
            raise ValueError('R160 exact wave excitation is missing/stale')

        responses: list[DeterministicPathFrequencyResponseArtifact] = []
        for ref in spec.r150_response_refs:
            response = self.r150_response_resolver(ref.authority_id)
            if response is None or _response_ref(response) != ref:
                raise ValueError(
                    f'R160 exact R150 response is missing/stale: {ref.authority_id}'
                )
            responses.append(response)

        rebuilt = compose_numerical_hybrid_response(
            spec=spec,
            r130_result=result,
            r130_artifact_payload=payload,
            r130_candidate_input=candidate,
            wave_excitation=excitation,
            r150_responses=responses,
            normalization_authority=normalization,
        )
        if rebuilt != artifact:
            raise ValueError(
                'R160 persisted numerical response no longer reproduces exactly'
            )
        return rebuilt

    def save(self, artifact: NumericalHybridResponseArtifact) -> None:
        artifact = self._rebuild(artifact)
        payload = _canonical_json(artifact.model_dump(mode='json'))
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT semantic_sha256, payload_json '
                'FROM r160_numerical_hybrid_responses WHERE artifact_id=?',
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing['semantic_sha256'] != artifact.semantic_sha256
                    or existing['payload_json'] != payload
                ):
                    raise ValueError('R160 numerical artifact identity collision')
                return
            connection.execute(
                'INSERT INTO r160_numerical_hybrid_responses '
                '(artifact_id, semantic_sha256, composition_spec_id, payload_json) '
                'VALUES (?, ?, ?, ?)',
                (
                    artifact.artifact_id,
                    artifact.semantic_sha256,
                    artifact.composition_spec.composition_spec_id,
                    payload,
                ),
            )

    def get(
        self,
        artifact_id: str,
    ) -> NumericalHybridResponseArtifact | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM r160_numerical_hybrid_responses '
                'WHERE artifact_id=?',
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        artifact = NumericalHybridResponseArtifact.model_validate_json(
            row['payload_json']
        )
        return self._rebuild(artifact)
