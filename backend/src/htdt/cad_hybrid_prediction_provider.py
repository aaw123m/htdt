from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import closing
from hashlib import sha256
import json
from math import atan2, degrees, hypot, isfinite, log10
from pathlib import Path
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import FrequencyDomain
from .cad_hybrid_numerical_composition import (
    COMMON_ANALYSIS_FOURIER_KERNEL,
    COMMON_PHASOR_CONVENTION,
    COMMON_TIME_ORIGIN,
    ExactSolverResultIdentity,
    NumericalHybridCompositionSpec,
    NumericalHybridResponseArtifact,
    convert_complex_phasor,
)
from .cad_prediction_provider import (
    CadPredictionProviderRepository,
    LowBandPredictionProvider,
    PredictionProviderCapability,
    PredictionProviderEnvironmentIdentity,
    PredictionProviderReceiverIdentity,
    PredictionProviderRef,
    PredictionProviderSourceIdentity,
    ProviderCurrentAuthority,
)
from .cad_repository import SceneRepository
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
)
from .cad_wave_excitation import AcousticWaveExcitationAuthority
from .comparison import FrequencyResponse
from .r120_geometry_compiler import ExactExternalAuthorityRef


HYBRID_PREDICTION_PROVIDER_SCHEMA_VERSION = 1
HYBRID_PREDICTION_PROVIDER_AUTHORITY_VERSION = (
    'r170b-hybrid-prediction-provider-1'
)
HYBRID_PREDICTION_PROVIDER_ADAPTER_ID = 'htdt.r170b.r160_numerical_hybrid'
HYBRID_PREDICTION_PROVIDER_ADAPTER_VERSION = '1'
HYBRID_PROVIDER_BINDING_AUTHORITY_VERSION = 'r170b-hybrid-provider-binding-1'
HYBRID_PRESSURE_RECONSTRUCTION = (
    'P_hybrid(f)=H_hybrid(f)*Q(f)_converted_to_exp(+i*omega*t)'
)
SPL_REFERENCE_PA = 20.0e-6

HybridProviderEvidenceState = Literal['candidate']
HybridProviderEvidenceScope = Literal['unvalidated']
HybridProviderStaleState = Literal['CURRENT', 'STALE']
HybridProviderConsumerKind = Literal[
    'O50_MEASUREMENT_PLAN',
    'O60_VALIDATION',
    'O70_ADAPTIVE',
]


HybridArtifactResolver = Callable[[str], NumericalHybridResponseArtifact | None]
HybridCompositionSpecResolver = Callable[[str], NumericalHybridCompositionSpec | None]
WaveExcitationResolver = Callable[[str], AcousticWaveExcitationAuthority | None]


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


def _excitation_ref(
    excitation: AcousticWaveExcitationAuthority,
) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=excitation.excitation_id,
        authority_version=excitation.authority_version,
        semantic_hash_sha256=excitation.semantic_sha256,
    )


def _crossover_ref(crossover: object) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=getattr(crossover, 'authority_id'),
        authority_version=getattr(crossover, 'authority_version'),
        semantic_hash_sha256=getattr(crossover, 'semantic_sha256'),
    )


def _domain_for_grid(grid: tuple[float, ...]) -> FrequencyDomain:
    return FrequencyDomain(
        minimum_hz=float(grid[0]),
        maximum_hz=float(grid[-1]),
    )


def _receiver_identity(
    provider: LowBandPredictionProvider,
    receiver_id: str,
) -> PredictionProviderReceiverIdentity:
    matches = tuple(
        item
        for item in provider.receiver_identities
        if item.receiver_binding.receiver_id == receiver_id
    )
    if len(matches) != 1:
        raise ValueError(
            'R170B receiver identity must resolve exactly once in base R170A provider'
        )
    return matches[0]


def evaluate_excitation_volume_velocity(
    excitation: AcousticWaveExcitationAuthority,
    frequency_hz: float,
) -> complex:
    """Evaluate exact complex Q(f) without inventing phase or extrapolating."""

    excitation = AcousticWaveExcitationAuthority.model_validate(
        excitation.model_dump(mode='python')
    )
    frequency = float(frequency_hz)
    if not isfinite(frequency) or frequency <= 0.0:
        raise ValueError('R170B Q(f) frequency must be finite and positive')

    samples = tuple(excitation.samples)
    for sample in samples:
        if float(sample.frequency_hz) == frequency:
            return complex(float(sample.real_m3_s), float(sample.imag_m3_s))

    if (
        frequency < float(excitation.valid_frequency_domain.minimum_hz)
        or frequency > float(excitation.valid_frequency_domain.maximum_hz)
    ):
        raise ValueError('R170B Q(f) extrapolation is forbidden')

    if excitation.interpolation.method != 'linear':
        raise ValueError(
            'R170B Q(f) is not exactly evaluable on output grid with the '
            f'authoritative interpolation method: {excitation.interpolation.method}'
        )

    for left, right in zip(samples, samples[1:], strict=False):
        left_hz = float(left.frequency_hz)
        right_hz = float(right.frequency_hz)
        if left_hz < frequency < right_hz:
            ratio = (frequency - left_hz) / (right_hz - left_hz)
            left_value = complex(float(left.real_m3_s), float(left.imag_m3_s))
            right_value = complex(float(right.real_m3_s), float(right.imag_m3_s))
            return left_value + ratio * (right_value - left_value)

    raise ValueError('R170B Q(f) output-grid frequency is missing from exact authority')


class HybridAbsolutePressureSample(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    complex_real_pa: float
    complex_imag_pa: float
    magnitude_pa: float = Field(gt=0.0)
    magnitude_db_spl: float
    phase_deg: float

    @model_validator(mode='after')
    def validate_sample(self) -> 'HybridAbsolutePressureSample':
        values = (
            self.frequency_hz,
            self.complex_real_pa,
            self.complex_imag_pa,
            self.magnitude_pa,
            self.magnitude_db_spl,
            self.phase_deg,
        )
        if any(not isfinite(float(value)) for value in values):
            raise ValueError('R170B absolute-pressure sample values must be finite')
        value = complex(self.complex_real_pa, self.complex_imag_pa)
        if abs(abs(value) - float(self.magnitude_pa)) > max(
            1e-12,
            float(self.magnitude_pa) * 1e-10,
        ):
            raise ValueError('R170B absolute-pressure magnitude mismatch')
        expected_db = 20.0 * log10(float(self.magnitude_pa) / SPL_REFERENCE_PA)
        if abs(expected_db - float(self.magnitude_db_spl)) > 1e-10:
            raise ValueError('R170B dB SPL conversion mismatch')
        expected_phase = degrees(atan2(value.imag, value.real))
        if abs(expected_phase - float(self.phase_deg)) > 1e-10:
            raise ValueError('R170B absolute-pressure phase mismatch')
        return self


class HybridPredictionProviderRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    provider_id: str = Field(pattern=r'^r170b-hybrid-provider:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class HybridPredictionProvider(BaseModel):
    """Solver-neutral product contract over one exact R160 numerical hybrid result."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = HYBRID_PREDICTION_PROVIDER_SCHEMA_VERSION
    authority_version: Literal[
        'r170b-hybrid-prediction-provider-1'
    ] = HYBRID_PREDICTION_PROVIDER_AUTHORITY_VERSION
    adapter_id: Literal[
        'htdt.r170b.r160_numerical_hybrid'
    ] = HYBRID_PREDICTION_PROVIDER_ADAPTER_ID
    adapter_version: Literal['1'] = HYBRID_PREDICTION_PROVIDER_ADAPTER_VERSION

    provider_id: str = Field(pattern=r'^r170b-hybrid-provider:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    base_provider_ref: PredictionProviderRef
    r160_artifact_ref: ExactExternalAuthorityRef
    r160_composition_spec_ref: ExactExternalAuthorityRef
    grid_reconciliation_ref: ExactExternalAuthorityRef
    crossover_configuration_ref: ExactExternalAuthorityRef
    normalization_authority_ref: ExactExternalAuthorityRef
    wave_excitation_ref: ExactExternalAuthorityRef

    exact_r130_result: ExactSolverResultIdentity
    exact_r150_response_refs: tuple[ExactExternalAuthorityRef, ...] = Field(
        min_length=1
    )
    source_entity_id: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)

    base_current_authority: ProviderCurrentAuthority
    source_identity: PredictionProviderSourceIdentity
    receiver_identity: PredictionProviderReceiverIdentity
    environment_identity: PredictionProviderEnvironmentIdentity

    output_frequency_grid_hz: tuple[float, ...] = Field(min_length=2)
    valid_frequency_domain: FrequencyDomain
    transition_start_hz: float = Field(gt=0.0)
    transition_end_hz: float = Field(gt=0.0)
    blend_law: str = Field(min_length=1)

    response_quantity: Literal['complex_pressure'] = 'complex_pressure'
    response_unit: Literal['Pa'] = 'Pa'
    magnitude_level_reference: Literal['20_uPa'] = '20_uPa'
    pressure_reconstruction: Literal[
        'P_hybrid(f)=H_hybrid(f)*Q(f)_converted_to_exp(+i*omega*t)'
    ] = HYBRID_PRESSURE_RECONSTRUCTION
    excitation_phasor_convention: str = Field(min_length=1)
    phasor_convention: Literal[
        'exp(+i*omega*t)'
    ] = COMMON_PHASOR_CONVENTION
    analysis_fourier_kernel: Literal[
        'exp(-i*omega*t)'
    ] = COMMON_ANALYSIS_FOURIER_KERNEL
    time_origin: Literal['source_t0'] = COMMON_TIME_ORIGIN

    absolute_pressure_samples: tuple[HybridAbsolutePressureSample, ...] = Field(
        min_length=2
    )

    observable_capabilities: tuple[PredictionProviderCapability, ...] = Field(
        min_length=1
    )
    magnitude_capability: Literal['READY'] = 'READY'
    phase_capability: Literal['READY'] = 'READY'
    broadband_hybrid_capability: Literal['READY'] = 'READY'
    timing_capability: Literal['UNSUPPORTED'] = 'UNSUPPORTED'
    spatial_field_capability: Literal['UNSUPPORTED'] = 'UNSUPPORTED'

    evidence_state: HybridProviderEvidenceState = 'candidate'
    evidence_scope: HybridProviderEvidenceScope = 'unvalidated'
    validation_authority_ref: None = None
    production_adoption: Literal[False] = False
    production_adoption_authority_ref: None = None
    stale_state: Literal['CURRENT'] = 'CURRENT'

    @model_validator(mode='after')
    def validate_contract(self) -> 'HybridPredictionProvider':
        grid = tuple(float(item) for item in self.output_frequency_grid_hz)
        if grid != tuple(sorted(set(grid))):
            raise ValueError('R170B output frequency grid must be sorted and unique')
        if any(not isfinite(item) or item <= 0.0 for item in grid):
            raise ValueError('R170B output frequency grid must be finite and positive')
        if self.valid_frequency_domain != _domain_for_grid(grid):
            raise ValueError('R170B valid frequency domain must equal output-grid bounds')
        if tuple(
            float(item.frequency_hz) for item in self.absolute_pressure_samples
        ) != grid:
            raise ValueError('R170B absolute-pressure samples must cover exact output grid')
        if self.transition_start_hz >= self.transition_end_hz:
            raise ValueError('R170B transition requires start < end')
        if (
            self.transition_start_hz < grid[0]
            or self.transition_end_hz > grid[-1]
        ):
            raise ValueError('R170B transition must lie inside output grid')

        if self.base_current_authority.solver_result_id != self.exact_r130_result.result_id:
            raise ValueError('R170B base provider/R160 R130 result id mismatch')
        if (
            self.base_current_authority.solver_result_sha256
            != self.exact_r130_result.semantic_sha256
        ):
            raise ValueError('R170B base provider/R160 R130 result hash mismatch')
        if self.base_current_authority.source_entity_id != self.source_entity_id:
            raise ValueError('R170B base provider/R160 source identity mismatch')
        if self.source_identity.source_binding.source_entity_id != self.source_entity_id:
            raise ValueError('R170B source binding identity mismatch')
        if self.receiver_identity.receiver_binding.receiver_id != self.receiver_id:
            raise ValueError('R170B receiver binding identity mismatch')
        if self.receiver_id not in self.base_current_authority.receiver_ids:
            raise ValueError('R170B receiver is absent from base R170A result identity')

        capability_map = {
            item.observable: item.state for item in self.observable_capabilities
        }
        if len(capability_map) != len(self.observable_capabilities):
            raise ValueError('R170B observable capabilities must be unique')
        required_ready = (
            'frequency_response_magnitude',
            'frequency_response_phase',
            'broadband_hybrid',
        )
        if any(capability_map.get(item) != 'READY' for item in required_ready):
            raise ValueError('R170B required hybrid capabilities must remain READY')
        required_unsupported = (
            'impulse_response',
            'rt60',
            'edt',
            'c50',
            'c80',
            'arrival_timing',
            'late_decay',
            'diffraction_completeness',
            'spatial_pressure_field',
        )
        if any(
            capability_map.get(item) != 'UNSUPPORTED'
            for item in required_unsupported
        ):
            raise ValueError('R170B unsupported capability table was widened')

        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R170B provider semantic hash mismatch')
        if self.provider_id != f'r170b-hybrid-provider:{expected}':
            raise ValueError('R170B provider id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'provider_id', 'semantic_sha256'},
        )

    def ref(self) -> HybridPredictionProviderRef:
        return HybridPredictionProviderRef(
            provider_id=self.provider_id,
            semantic_sha256=self.semantic_sha256,
        )

    def capability(self, observable: str) -> PredictionProviderCapability:
        for item in self.observable_capabilities:
            if item.observable == observable:
                return item
        return PredictionProviderCapability(
            observable=observable,
            state='UNSUPPORTED',
            reason='observable is outside the bounded R170B provider contract',
        )

    def require_observable(self, observable: str) -> None:
        capability = self.capability(observable)
        if capability.state != 'READY':
            raise ValueError(
                'hybrid prediction provider observable is unsupported: '
                f'{observable}: {capability.reason}'
            )

    def frequency_response(
        self,
        *,
        source_entity_id: str,
        receiver_id: str,
    ) -> FrequencyResponse:
        self.require_observable('frequency_response_magnitude')
        if source_entity_id != self.source_entity_id:
            raise ValueError('hybrid prediction provider source identity mismatch')
        if receiver_id != self.receiver_id:
            raise ValueError('hybrid prediction provider receiver identity mismatch')
        return FrequencyResponse(
            frequency_hz=self.output_frequency_grid_hz,
            level_db=tuple(
                item.magnitude_db_spl for item in self.absolute_pressure_samples
            ),
        )


def _capabilities() -> tuple[PredictionProviderCapability, ...]:
    return (
        PredictionProviderCapability(
            observable='frequency_response_magnitude',
            state='READY',
        ),
        PredictionProviderCapability(
            observable='frequency_response_phase',
            state='READY',
        ),
        PredictionProviderCapability(
            observable='broadband_hybrid',
            state='READY',
        ),
        PredictionProviderCapability(
            observable='impulse_response',
            state='UNSUPPORTED',
            reason='R170B does not synthesize an impulse response from R160 samples',
        ),
        PredictionProviderCapability(
            observable='rt60',
            state='UNSUPPORTED',
            reason='R170B carries no validated late-decay authority for RT60',
        ),
        PredictionProviderCapability(
            observable='edt',
            state='UNSUPPORTED',
            reason='R170B carries no validated early-decay authority for EDT',
        ),
        PredictionProviderCapability(
            observable='c50',
            state='UNSUPPORTED',
            reason='R170B has no impulse-response clarity authority',
        ),
        PredictionProviderCapability(
            observable='c80',
            state='UNSUPPORTED',
            reason='R170B has no impulse-response clarity authority',
        ),
        PredictionProviderCapability(
            observable='arrival_timing',
            state='UNSUPPORTED',
            reason='R170B frequency response does not establish arrival timing',
        ),
        PredictionProviderCapability(
            observable='late_decay',
            state='UNSUPPORTED',
            reason='R160 hybrid numerical composition does not validate late decay',
        ),
        PredictionProviderCapability(
            observable='diffraction_completeness',
            state='UNSUPPORTED',
            reason='R160 hybrid numerical composition does not establish diffraction completeness',
        ),
        PredictionProviderCapability(
            observable='spatial_pressure_field',
            state='UNSUPPORTED',
            reason='R170B bounded provider covers one exact R160 receiver only',
        ),
    )


def build_hybrid_prediction_provider(
    *,
    base_provider: LowBandPredictionProvider,
    r160_artifact: NumericalHybridResponseArtifact,
    composition_spec: NumericalHybridCompositionSpec,
    wave_excitation: AcousticWaveExcitationAuthority,
) -> HybridPredictionProvider:
    """Project exact R170A + R160 authorities into a bounded product contract."""

    base = LowBandPredictionProvider.model_validate(
        base_provider.model_dump(mode='python')
    )
    artifact = NumericalHybridResponseArtifact.model_validate(
        r160_artifact.model_dump(mode='python')
    )
    spec = NumericalHybridCompositionSpec.model_validate(
        composition_spec.model_dump(mode='python')
    )
    excitation = AcousticWaveExcitationAuthority.model_validate(
        wave_excitation.model_dump(mode='python')
    )

    if artifact.capability_state != 'COMPLEX_SUPPORTED':
        raise ValueError('R170B requires a COMPLEX_SUPPORTED R160 artifact')
    if artifact.composition_spec != spec:
        raise ValueError('R170B exact R160 composition spec binding mismatch')
    if artifact.as_external_ref() != r160_artifact.as_external_ref():
        raise ValueError('R170B exact R160 artifact identity mismatch')
    if artifact.exact_r130_result.result_id != base.result_envelope_id:
        raise ValueError('R170B exact R130 result identity mismatch')
    if artifact.exact_r130_result.semantic_sha256 != base.result_envelope_sha256:
        raise ValueError('R170B exact R130 result hash mismatch')
    if artifact.exact_r130_artifact_ref != base.result_artifact_ref:
        raise ValueError('R170B exact R130 complex-pressure artifact mismatch')
    if spec.source_entity_id != base.current_authority.source_entity_id:
        raise ValueError('R170B exact source identity mismatch')
    receiver_identity = _receiver_identity(base, spec.receiver_id)
    if _excitation_ref(excitation) != spec.wave_excitation_ref:
        raise ValueError('R170B exact AcousticWaveExcitationAuthority mismatch')

    if artifact.grid_reconciliation != spec.grid_reconciliation:
        raise ValueError('R170B exact grid reconciliation authority mismatch')
    if artifact.crossover_configuration != spec.crossover_configuration:
        raise ValueError('R170B exact crossover authority mismatch')
    if artifact.exact_r150_response_refs != spec.r150_response_refs:
        raise ValueError('R170B exact R150 response identity mismatch')

    pressure_samples: list[HybridAbsolutePressureSample] = []
    for sample in artifact.samples:
        transfer = complex(
            float(sample.complex_real_pa_per_m3_s),
            float(sample.complex_imag_pa_per_m3_s),
        )
        q_input = evaluate_excitation_volume_velocity(
            excitation,
            float(sample.frequency_hz),
        )
        q_common = convert_complex_phasor(
            q_input,
            input_convention=excitation.phasor_convention,
            output_convention=artifact.common_phasor_convention,
        )
        absolute_pressure = transfer * q_common
        if not isfinite(absolute_pressure.real) or not isfinite(absolute_pressure.imag):
            raise ValueError('R170B reconstructed absolute complex pressure is not finite')
        magnitude = hypot(absolute_pressure.real, absolute_pressure.imag)
        if magnitude <= 0.0:
            raise ValueError(
                'R170B finite dB SPL publication requires non-zero absolute pressure'
            )
        pressure_samples.append(
            HybridAbsolutePressureSample(
                frequency_hz=float(sample.frequency_hz),
                complex_real_pa=float(absolute_pressure.real),
                complex_imag_pa=float(absolute_pressure.imag),
                magnitude_pa=float(magnitude),
                magnitude_db_spl=20.0 * log10(magnitude / SPL_REFERENCE_PA),
                phase_deg=degrees(
                    atan2(absolute_pressure.imag, absolute_pressure.real)
                ),
            )
        )

    grid = tuple(float(item) for item in artifact.exact_frequency_grid_hz)
    core = {
        'schema_version': HYBRID_PREDICTION_PROVIDER_SCHEMA_VERSION,
        'authority_version': HYBRID_PREDICTION_PROVIDER_AUTHORITY_VERSION,
        'adapter_id': HYBRID_PREDICTION_PROVIDER_ADAPTER_ID,
        'adapter_version': HYBRID_PREDICTION_PROVIDER_ADAPTER_VERSION,
        'base_provider_ref': base.ref().model_dump(mode='json'),
        'r160_artifact_ref': artifact.as_external_ref().model_dump(mode='json'),
        'r160_composition_spec_ref': spec.as_external_ref().model_dump(mode='json'),
        'grid_reconciliation_ref': (
            artifact.grid_reconciliation.as_external_ref().model_dump(mode='json')
        ),
        'crossover_configuration_ref': (
            _crossover_ref(artifact.crossover_configuration).model_dump(mode='json')
        ),
        'normalization_authority_ref': (
            spec.normalization_authority_ref.model_dump(mode='json')
        ),
        'wave_excitation_ref': _excitation_ref(excitation).model_dump(mode='json'),
        'exact_r130_result': artifact.exact_r130_result.model_dump(mode='json'),
        'exact_r150_response_refs': [
            item.model_dump(mode='json')
            for item in artifact.exact_r150_response_refs
        ],
        'source_entity_id': spec.source_entity_id,
        'receiver_id': spec.receiver_id,
        'base_current_authority': base.current_authority.model_dump(mode='json'),
        'source_identity': base.source_identity.model_dump(mode='json'),
        'receiver_identity': receiver_identity.model_dump(mode='json'),
        'environment_identity': base.environment_identity.model_dump(mode='json'),
        'output_frequency_grid_hz': list(grid),
        'valid_frequency_domain': _domain_for_grid(grid).model_dump(mode='json'),
        'transition_start_hz': float(artifact.transition_start_hz),
        'transition_end_hz': float(artifact.transition_end_hz),
        'blend_law': artifact.weight_law,
        'response_quantity': 'complex_pressure',
        'response_unit': 'Pa',
        'magnitude_level_reference': '20_uPa',
        'pressure_reconstruction': HYBRID_PRESSURE_RECONSTRUCTION,
        'excitation_phasor_convention': excitation.phasor_convention,
        'phasor_convention': artifact.common_phasor_convention,
        'analysis_fourier_kernel': artifact.common_analysis_fourier_kernel,
        'time_origin': artifact.time_origin,
        'absolute_pressure_samples': [
            item.model_dump(mode='json') for item in pressure_samples
        ],
        'observable_capabilities': [
            item.model_dump(mode='json') for item in _capabilities()
        ],
        'magnitude_capability': 'READY',
        'phase_capability': 'READY',
        'broadband_hybrid_capability': 'READY',
        'timing_capability': 'UNSUPPORTED',
        'spatial_field_capability': 'UNSUPPORTED',
        'evidence_state': 'candidate',
        'evidence_scope': 'unvalidated',
        'validation_authority_ref': None,
        'production_adoption': False,
        'production_adoption_authority_ref': None,
        'stale_state': 'CURRENT',
    }
    digest = _semantic_hash(core)
    return HybridPredictionProvider(
        provider_id=f'r170b-hybrid-provider:{digest}',
        semantic_sha256=digest,
        **core,
    )


def hybrid_provider_frequency_response(
    provider: HybridPredictionProvider,
    *,
    source_entity_id: str,
    receiver_id: str,
    low_hz: float,
    high_hz: float,
) -> FrequencyResponse:
    """N70 product-facing typed read. R160 raw JSON never crosses this boundary."""

    provider = HybridPredictionProvider.model_validate(
        provider.model_dump(mode='python')
    )
    if source_entity_id != provider.source_entity_id:
        raise ValueError('hybrid provider source identity mismatch')
    if receiver_id != provider.receiver_id:
        raise ValueError('hybrid provider receiver identity mismatch')
    provider.require_observable('frequency_response_magnitude')

    low = float(low_hz)
    high = float(high_hz)
    if not isfinite(low) or not isfinite(high) or high <= low:
        raise ValueError('hybrid provider requested frequency band is invalid')
    domain = provider.valid_frequency_domain
    if low < float(domain.minimum_hz) or high > float(domain.maximum_hz):
        raise ValueError('hybrid provider requested band exceeds exact output domain')

    selected = tuple(
        item
        for item in provider.absolute_pressure_samples
        if low <= float(item.frequency_hz) <= high
    )
    if len(selected) < 2:
        raise ValueError(
            'hybrid provider exact output grid has fewer than two points in requested band'
        )
    return FrequencyResponse(
        frequency_hz=tuple(float(item.frequency_hz) for item in selected),
        level_db=tuple(float(item.magnitude_db_spl) for item in selected),
    )


class HybridPredictionProviderBinding(BaseModel):
    """Immutable O50/O60/O70 consumer binding to one exact R170B provider."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r170b-hybrid-provider-binding-1'
    ] = HYBRID_PROVIDER_BINDING_AUTHORITY_VERSION
    binding_id: str = Field(
        pattern=r'^r170b-hybrid-provider-binding:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    provider_ref: HybridPredictionProviderRef
    base_provider_ref: PredictionProviderRef
    consumer_kind: HybridProviderConsumerKind
    consumer_id: str = Field(min_length=1)
    consumer_semantic_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    required_observables: tuple[str, ...] = Field(min_length=1)
    expected_authority: ProviderCurrentAuthority
    stale_state: Literal['CURRENT'] = 'CURRENT'

    @model_validator(mode='after')
    def validate_identity(self) -> 'HybridPredictionProviderBinding':
        if len(self.required_observables) != len(set(self.required_observables)):
            raise ValueError('R170B provider binding observables must be unique')
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R170B provider binding semantic hash mismatch')
        if self.binding_id != f'r170b-hybrid-provider-binding:{expected}':
            raise ValueError('R170B provider binding id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'binding_id', 'semantic_sha256'},
        )


def build_hybrid_provider_binding(
    provider: HybridPredictionProvider,
    *,
    consumer_kind: HybridProviderConsumerKind,
    consumer_id: str,
    required_observables: Sequence[str],
    consumer_semantic_sha256: str | None = None,
) -> HybridPredictionProviderBinding:
    required = tuple(sorted(set(required_observables)))
    if not required:
        raise ValueError('R170B provider binding requires observable capabilities')
    for observable in required:
        provider.require_observable(observable)
    core = {
        'authority_version': HYBRID_PROVIDER_BINDING_AUTHORITY_VERSION,
        'provider_ref': provider.ref().model_dump(mode='json'),
        'base_provider_ref': provider.base_provider_ref.model_dump(mode='json'),
        'consumer_kind': consumer_kind,
        'consumer_id': consumer_id,
        'consumer_semantic_sha256': consumer_semantic_sha256,
        'required_observables': list(required),
        'expected_authority': provider.base_current_authority.model_dump(mode='json'),
        'stale_state': 'CURRENT',
    }
    digest = _semantic_hash(core)
    return HybridPredictionProviderBinding(
        binding_id=f'r170b-hybrid-provider-binding:{digest}',
        semantic_sha256=digest,
        provider_ref=provider.ref(),
        base_provider_ref=provider.base_provider_ref,
        consumer_kind=consumer_kind,
        consumer_id=consumer_id,
        consumer_semantic_sha256=consumer_semantic_sha256,
        required_observables=required,
        expected_authority=provider.base_current_authority,
    )


def require_hybrid_binding_current(
    binding: HybridPredictionProviderBinding,
    provider: HybridPredictionProvider,
    current: ProviderCurrentAuthority,
) -> None:
    if binding.provider_ref != provider.ref():
        raise ValueError('R170B provider binding references another provider')
    if binding.base_provider_ref != provider.base_provider_ref:
        raise ValueError('R170B provider binding base provider mismatch')
    if binding.expected_authority != provider.base_current_authority:
        raise ValueError('R170B provider binding exact authority mismatch')
    if binding.expected_authority != current:
        raise ValueError('R170B provider binding authority is stale')
    for observable in binding.required_observables:
        provider.require_observable(observable)


class CadHybridPredictionProviderRepository:
    """Append-only R170B persistence with exact R170A/R160 reopen validation."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        base_provider_repository: CadPredictionProviderRepository,
        r160_repository: object,
        composition_spec_resolver: HybridCompositionSpecResolver,
        wave_excitation_resolver: WaveExcitationResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.base_provider_repository = base_provider_repository
        self.r160_repository = r160_repository
        self.composition_spec_resolver = composition_spec_resolver
        self.wave_excitation_resolver = wave_excitation_resolver
        self.path = Path(scene_repository.path)
        for label, repository in (
            ('R170A provider', base_provider_repository),
            ('R160 numerical response', r160_repository),
        ):
            repository_path = getattr(repository, 'path', None)
            if repository_path is None or Path(repository_path) != self.path:
                raise ValueError(
                    f'R170B and {label} repositories must share one native CAD database'
                )
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
            require_native_tables(
                connection,
                'cad_hybrid_prediction_providers',
                'cad_hybrid_prediction_provider_bindings',
            )

    def build_current(
        self,
        *,
        base_provider_id: str,
        r160_artifact_id: str,
    ) -> HybridPredictionProvider:
        """Build only after both R170A and R160 repositories prove currentness."""

        base = self.base_provider_repository.get_provider(base_provider_id)
        if base is None:
            raise ValueError('R170B base R170A provider is missing/stale')

        get_artifact = getattr(self.r160_repository, 'get', None)
        if not callable(get_artifact):
            raise ValueError('R170B R160 repository does not expose exact get()')
        artifact = get_artifact(r160_artifact_id)
        if artifact is None:
            raise ValueError('R170B R160 numerical artifact is missing/stale')

        spec = self.composition_spec_resolver(
            artifact.composition_spec.composition_spec_id
        )
        if spec is None or spec != artifact.composition_spec:
            raise ValueError('R170B R160 composition spec is missing/stale')

        excitation = self.wave_excitation_resolver(
            spec.wave_excitation_ref.authority_id
        )
        if (
            excitation is None
            or _excitation_ref(excitation) != spec.wave_excitation_ref
        ):
            raise ValueError('R170B source excitation is missing/stale')

        return build_hybrid_prediction_provider(
            base_provider=base,
            r160_artifact=artifact,
            composition_spec=spec,
            wave_excitation=excitation,
        )

    def _validate(
        self,
        provider: HybridPredictionProvider,
    ) -> HybridPredictionProvider:
        provider = HybridPredictionProvider.model_validate(
            provider.model_dump(mode='python')
        )

        base = self.base_provider_repository.get_provider(
            provider.base_provider_ref.provider_id
        )
        if base is None or base.ref() != provider.base_provider_ref:
            raise ValueError('R170B base R170A provider is missing/stale')

        get_artifact = getattr(self.r160_repository, 'get', None)
        if not callable(get_artifact):
            raise ValueError('R170B R160 repository does not expose exact get()')
        artifact = get_artifact(provider.r160_artifact_ref.authority_id)
        if artifact is None or artifact.as_external_ref() != provider.r160_artifact_ref:
            raise ValueError('R170B R160 numerical artifact is missing/stale')

        spec = self.composition_spec_resolver(
            provider.r160_composition_spec_ref.authority_id
        )
        if spec is None or spec.as_external_ref() != provider.r160_composition_spec_ref:
            raise ValueError('R170B R160 composition spec is missing/stale')
        if spec != artifact.composition_spec:
            raise ValueError('R170B R160 artifact/spec exact identity changed')

        excitation = self.wave_excitation_resolver(
            provider.wave_excitation_ref.authority_id
        )
        if excitation is None or _excitation_ref(excitation) != provider.wave_excitation_ref:
            raise ValueError('R170B source excitation is missing/stale')

        rebuilt = build_hybrid_prediction_provider(
            base_provider=base,
            r160_artifact=artifact,
            composition_spec=spec,
            wave_excitation=excitation,
        )
        if rebuilt != provider:
            raise ValueError(
                'R170B provider no longer reproduces from exact current authorities'
            )
        return rebuilt

    def save(
        self,
        provider: HybridPredictionProvider,
    ) -> HybridPredictionProvider:
        provider = self._validate(provider)
        payload = _canonical_json(provider.model_dump(mode='json'))
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT semantic_sha256, payload_json '
                'FROM cad_hybrid_prediction_providers WHERE provider_id=?',
                (provider.provider_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing['semantic_sha256'] != provider.semantic_sha256
                    or existing['payload_json'] != payload
                ):
                    raise ValueError('R170B provider identity collision')
                return self._validate(provider)
            connection.execute(
                """
                INSERT INTO cad_hybrid_prediction_providers(
                    provider_id,
                    semantic_sha256,
                    base_provider_id,
                    r160_artifact_id,
                    r160_composition_spec_id,
                    source_entity_id,
                    receiver_id,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider.provider_id,
                    provider.semantic_sha256,
                    provider.base_provider_ref.provider_id,
                    provider.r160_artifact_ref.authority_id,
                    provider.r160_composition_spec_ref.authority_id,
                    provider.source_entity_id,
                    provider.receiver_id,
                    payload,
                ),
            )
        return provider

    def get(
        self,
        provider_id: str,
    ) -> HybridPredictionProvider | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_hybrid_prediction_providers '
                'WHERE provider_id=?',
                (provider_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            HybridPredictionProvider.model_validate_json(row['payload_json'])
        )

    def _validate_binding(
        self,
        binding: HybridPredictionProviderBinding,
    ) -> HybridPredictionProviderBinding:
        binding = HybridPredictionProviderBinding.model_validate(
            binding.model_dump(mode='python')
        )
        provider = self.get(binding.provider_ref.provider_id)
        if provider is None or provider.ref() != binding.provider_ref:
            raise ValueError('R170B binding provider is missing/stale')
        if binding.base_provider_ref != provider.base_provider_ref:
            raise ValueError('R170B binding base provider mismatch')
        if binding.expected_authority != provider.base_current_authority:
            raise ValueError('R170B binding exact authority is stale')
        for observable in binding.required_observables:
            provider.require_observable(observable)
        return binding

    def save_binding(
        self,
        binding: HybridPredictionProviderBinding,
    ) -> HybridPredictionProviderBinding:
        binding = self._validate_binding(binding)
        payload = _canonical_json(binding.model_dump(mode='json'))
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT semantic_sha256, payload_json '
                'FROM cad_hybrid_prediction_provider_bindings WHERE binding_id=?',
                (binding.binding_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing['semantic_sha256'] != binding.semantic_sha256
                    or existing['payload_json'] != payload
                ):
                    raise ValueError('R170B provider binding identity collision')
                return self._validate_binding(binding)
            connection.execute(
                """
                INSERT INTO cad_hybrid_prediction_provider_bindings(
                    binding_id,
                    semantic_sha256,
                    provider_id,
                    consumer_kind,
                    consumer_id,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.semantic_sha256,
                    binding.provider_ref.provider_id,
                    binding.consumer_kind,
                    binding.consumer_id,
                    payload,
                ),
            )
        return binding

    def get_binding(
        self,
        binding_id: str,
    ) -> HybridPredictionProviderBinding | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_hybrid_prediction_provider_bindings '
                'WHERE binding_id=?',
                (binding_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_binding(
            HybridPredictionProviderBinding.model_validate_json(row['payload_json'])
        )

    def bindings_for_consumer(
        self,
        *,
        consumer_kind: HybridProviderConsumerKind,
        consumer_id: str,
    ) -> tuple[HybridPredictionProviderBinding, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_hybrid_prediction_provider_bindings '
                'WHERE consumer_kind=? AND consumer_id=? ORDER BY seq ASC',
                (consumer_kind, consumer_id),
            ).fetchall()
        return tuple(
            self._validate_binding(
                HybridPredictionProviderBinding.model_validate_json(
                    row['payload_json']
                )
            )
            for row in rows
        )
