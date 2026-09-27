from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from math import acos, atan2, cos, degrees, isfinite, pi, sin, sqrt
from pathlib import Path
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .acoustic_benchmark import AcousticMaterial, GeometricIncidenceCondition
from .cad_directivity import DirectivityDataset, evaluate_directivity
from .cad_equipment import EquipmentDefinition, FrequencyDomain
from .cad_geometric_acoustics_adapter import (
    BoundaryIncidenceEvaluation,
    DeterministicAcousticPath,
    DeterministicGaExecutionInput,
    DeterministicPathArtifact,
    GeometricSurfacePlane,
)
from .cad_repository import SceneRepository
from .cad_scene import Position3
from .cad_schema import require_native_tables, connect_sqlite
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _semantic_hash


R150_PATH_RESPONSE_SCHEMA_VERSION = 1
R150_PATH_RESPONSE_AUTHORITY_VERSION = 'r150-complex-path-response-1'
POINT_SOURCE_MODEL = 'point_volume_velocity_monopole_free_field_v1'
ANALYTIC_OMNI_DIRECTIVITY_MODEL = 'point_volume_velocity_monopole_omnidirectional_v1'
PHASOR_CONVENTION = 'exp(+i*omega*t)'
GREEN_FUNCTION_CONVENTION = 'exp(-i*k*r)/(4*pi*r)'
TRANSFER_QUANTITY = 'complex_acoustic_pressure_per_volume_velocity'
TRANSFER_UNIT = 'Pa/(m3/s)'

ResponseCapability = Literal['COMPLEX_SUPPORTED', 'MAGNITUDE_ONLY', 'UNSUPPORTED']
SourceCapability = Literal[
    'COMPLEX_DIRECTIONAL_TRANSFER_AVAILABLE',
    'MAGNITUDE_ONLY_DIRECTIVITY',
    'ANALYTIC_OMNIDIRECTIONAL_MODEL',
    'UNSUPPORTED_UNKNOWN_DIRECTIVITY',
]
ReflectionCapability = Literal['COMPLEX', 'MAGNITUDE_ONLY']
PortalProvenanceState = Literal['measured', 'manufacturer', 'analytic', 'assumed']
ReflectionIncidenceCondition = Literal[
    'normal_incidence',
    'random_or_diffuse_incidence',
    'angle_specific',
    'model_derived_angle_response',
    'unknown_incidence',
    'angle_independent',
]
_INCIDENCE_COSINE_MATCH_TOLERANCE = 1e-9






def _finite(value: float, *, name: str) -> float:
    result = float(value)
    if not isfinite(result):
        raise ValueError(f'{name} must be finite')
    return result


def _phase(value: complex) -> float:
    return atan2(value.imag, value.real)


def _complex_from_parts(real: float, imag: float) -> complex:
    return complex(_finite(real, name='complex real'), _finite(imag, name='complex imag'))


def _ref_payload(ref: ExactExternalAuthorityRef) -> tuple[str, str, str]:
    return (ref.authority_id, ref.authority_version, ref.semantic_hash_sha256)


def _identity_ref(
    *,
    authority_id: str,
    authority_version: str,
    semantic_hash_sha256: str,
) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=authority_id,
        authority_version=authority_version,
        semantic_hash_sha256=semantic_hash_sha256,
    )


def _execution_input_ref(
    execution_input: DeterministicGaExecutionInput,
) -> ExactExternalAuthorityRef:
    return _identity_ref(
        authority_id=execution_input.execution_input_id,
        authority_version=execution_input.authority_version,
        semantic_hash_sha256=execution_input.semantic_sha256,
    )


class ComplexTransferSample(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    magnitude: float = Field(ge=0.0)
    phase_rad: float | None = None
    real: float | None = None
    imag: float | None = None

    @field_validator('frequency_hz', 'magnitude', 'phase_rad', 'real', 'imag')
    @classmethod
    def finite_values(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, name='transfer sample value')

    @model_validator(mode='after')
    def coherent_parts(self) -> 'ComplexTransferSample':
        complex_present = self.real is not None or self.imag is not None
        if complex_present != (self.phase_rad is not None):
            raise ValueError('complex transfer real/imag and phase must be present together')
        if complex_present:
            if self.real is None or self.imag is None:
                raise ValueError('complex transfer requires both real and imag')
            value = complex(self.real, self.imag)
            if abs(abs(value) - self.magnitude) > max(1e-12, self.magnitude * 1e-10):
                raise ValueError('complex transfer magnitude mismatch')
            phase_error = atan2(
                sin(float(self.phase_rad) - _phase(value)),
                cos(float(self.phase_rad) - _phase(value)),
            )
            if abs(phase_error) > 1e-10:
                raise ValueError('complex transfer phase does not match real/imag')
        return self

    @classmethod
    def from_complex(cls, frequency_hz: float, value: complex) -> 'ComplexTransferSample':
        return cls(
            frequency_hz=frequency_hz,
            magnitude=abs(value),
            phase_rad=_phase(value),
            real=value.real,
            imag=value.imag,
        )

    @classmethod
    def magnitude_only(cls, frequency_hz: float, magnitude: float) -> 'ComplexTransferSample':
        return cls(frequency_hz=frequency_hz, magnitude=magnitude)

    def complex_value(self) -> complex | None:
        if self.real is None or self.imag is None:
            return None
        return complex(self.real, self.imag)


class AcousticEnvironmentAuthority(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_id: str = Field(pattern=r'^r150-response-environment:[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    density_kg_m3: float = Field(gt=0.0)
    sound_speed_m_s: float = Field(gt=0.0)
    temperature_c: float | None = None
    relative_humidity_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    pressure_pa: float | None = Field(default=None, gt=0.0)
    valid_frequency_domain: FrequencyDomain

    @field_validator(
        'density_kg_m3',
        'sound_speed_m_s',
        'temperature_c',
        'relative_humidity_percent',
        'pressure_pa',
    )
    @classmethod
    def finite_values(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, name='environment value')

    @model_validator(mode='after')
    def identity(self) -> 'AcousticEnvironmentAuthority':
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_hash_sha256 != expected:
            raise ValueError('response environment semantic hash mismatch')
        if self.authority_id != f'r150-response-environment:{expected}':
            raise ValueError('response environment id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'authority_id', 'semantic_hash_sha256'})

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return _identity_ref(
            authority_id=self.authority_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_hash_sha256,
        )


def build_acoustic_environment_authority(
    *,
    density_kg_m3: float,
    sound_speed_m_s: float,
    valid_frequency_domain: FrequencyDomain,
    temperature_c: float | None = None,
    relative_humidity_percent: float | None = None,
    pressure_pa: float | None = None,
) -> AcousticEnvironmentAuthority:
    core = {
        'authority_version': '1',
        'density_kg_m3': float(density_kg_m3),
        'sound_speed_m_s': float(sound_speed_m_s),
        'temperature_c': temperature_c,
        'relative_humidity_percent': relative_humidity_percent,
        'pressure_pa': pressure_pa,
        'valid_frequency_domain': valid_frequency_domain.model_dump(mode='json'),
    }
    digest = _semantic_hash(core)
    return AcousticEnvironmentAuthority(
        authority_id=f'r150-response-environment:{digest}',
        semantic_hash_sha256=digest,
        **core,
    )


class FrequencyGridAuthority(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_id: str = Field(pattern=r'^r150-response-frequency-grid:[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    frequencies_hz: tuple[float, ...] = Field(min_length=1)
    valid_frequency_domain: FrequencyDomain

    @field_validator('frequencies_hz')
    @classmethod
    def valid_grid(cls, values: tuple[float, ...]) -> tuple[float, ...]:
        result = tuple(float(value) for value in values)
        if any(not isfinite(value) or value <= 0.0 for value in result):
            raise ValueError('response frequency grid must be finite and positive')
        if result != tuple(sorted(set(result))):
            raise ValueError('response frequency grid must be unique and sorted')
        return result

    @model_validator(mode='after')
    def identity(self) -> 'FrequencyGridAuthority':
        if (
            self.frequencies_hz[0] != self.valid_frequency_domain.minimum_hz
            or self.frequencies_hz[-1] != self.valid_frequency_domain.maximum_hz
        ):
            if len(self.frequencies_hz) > 1:
                raise ValueError('frequency-grid valid band must match exact grid extent')
            if not self.valid_frequency_domain.contains(self.frequencies_hz[0]):
                raise ValueError('single-point grid must lie inside its declared valid band')
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_hash_sha256 != expected:
            raise ValueError('response frequency grid semantic hash mismatch')
        if self.authority_id != f'r150-response-frequency-grid:{expected}':
            raise ValueError('response frequency grid id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'authority_id', 'semantic_hash_sha256'})

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return _identity_ref(
            authority_id=self.authority_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_hash_sha256,
        )


def build_frequency_grid_authority(
    frequencies_hz: Sequence[float],
) -> FrequencyGridAuthority:
    frequencies = tuple(float(value) for value in frequencies_hz)
    if not frequencies:
        raise ValueError('response frequency grid cannot be empty')
    if len(frequencies) == 1:
        domain = FrequencyDomain(
            minimum_hz=frequencies[0] * (1.0 - 1e-12),
            maximum_hz=frequencies[0] * (1.0 + 1e-12),
        )
    else:
        domain = FrequencyDomain(minimum_hz=frequencies[0], maximum_hz=frequencies[-1])
    core = {
        'authority_version': '1',
        'frequencies_hz': list(frequencies),
        'valid_frequency_domain': domain.model_dump(mode='json'),
    }
    digest = _semantic_hash(core)
    return FrequencyGridAuthority(
        authority_id=f'r150-response-frequency-grid:{digest}',
        semantic_hash_sha256=digest,
        **core,
    )


class PathResponseConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_id: str = Field(pattern=r'^r150-response-configuration:[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    quantity: Literal['complex_acoustic_pressure_per_volume_velocity'] = TRANSFER_QUANTITY
    unit: Literal['Pa/(m3/s)'] = TRANSFER_UNIT
    source_normalization: Literal['unit_volume_velocity_m3_s'] = 'unit_volume_velocity_m3_s'
    phasor_convention: Literal['exp(+i*omega*t)'] = PHASOR_CONVENTION
    angular_frequency_convention: Literal['omega=2*pi*f'] = 'omega=2*pi*f'
    green_function_convention: Literal['exp(-i*k*r)/(4*pi*r)'] = GREEN_FUNCTION_CONVENTION
    time_origin: Literal['source_t0'] = 'source_t0'
    distance_normalization: Literal['absolute_spherical_green_no_reference_distance'] = (
        'absolute_spherical_green_no_reference_distance'
    )
    reference_distance_m: None = None
    minimum_path_length_m: float = Field(default=1e-6, gt=0.0)
    path_sum_policy: Literal['per_path_only_no_sum'] = 'per_path_only_no_sum'
    # Atmospheric absorption is not implemented by this response engine; the
    # omission is declared rather than silently neglected.
    atmospheric_attenuation_policy: Literal['omitted_unsupported'] = (
        'omitted_unsupported'
    )

    @field_validator('minimum_path_length_m')
    @classmethod
    def finite_length(cls, value: float) -> float:
        return _finite(value, name='minimum_path_length_m')

    @model_validator(mode='after')
    def identity(self) -> 'PathResponseConfiguration':
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_hash_sha256 != expected:
            raise ValueError('path response configuration semantic hash mismatch')
        if self.authority_id != f'r150-response-configuration:{expected}':
            raise ValueError('path response configuration id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'authority_id', 'semantic_hash_sha256'})

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return _identity_ref(
            authority_id=self.authority_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_hash_sha256,
        )


def build_path_response_configuration(
    *,
    minimum_path_length_m: float = 1e-6,
) -> PathResponseConfiguration:
    core = {
        'authority_version': '1',
        'quantity': TRANSFER_QUANTITY,
        'unit': TRANSFER_UNIT,
        'source_normalization': 'unit_volume_velocity_m3_s',
        'phasor_convention': PHASOR_CONVENTION,
        'angular_frequency_convention': 'omega=2*pi*f',
        'green_function_convention': GREEN_FUNCTION_CONVENTION,
        'time_origin': 'source_t0',
        'distance_normalization': 'absolute_spherical_green_no_reference_distance',
        'reference_distance_m': None,
        'minimum_path_length_m': float(minimum_path_length_m),
        'path_sum_policy': 'per_path_only_no_sum',
        'atmospheric_attenuation_policy': 'omitted_unsupported',
    }
    digest = _semantic_hash(core)
    return PathResponseConfiguration(
        authority_id=f'r150-response-configuration:{digest}',
        semantic_hash_sha256=digest,
        **core,
    )


class PointSourceNormalizationAuthority(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_id: str = Field(pattern=r'^r150-point-source-normalization:[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    model: Literal['point_volume_velocity_monopole_free_field_v1'] = POINT_SOURCE_MODEL
    quantity: Literal['complex_acoustic_pressure_per_volume_velocity'] = TRANSFER_QUANTITY
    unit: Literal['Pa/(m3/s)'] = TRANSFER_UNIT
    source_reference: Literal['unit_volume_velocity_m3_s'] = 'unit_volume_velocity_m3_s'
    phasor_convention: Literal['exp(+i*omega*t)'] = PHASOR_CONVENTION
    green_function_convention: Literal['exp(-i*k*r)/(4*pi*r)'] = GREEN_FUNCTION_CONVENTION
    provenance_state: Literal['analytic'] = 'analytic'
    provenance: str = Field(min_length=1)
    valid_frequency_domain: FrequencyDomain

    @model_validator(mode='after')
    def identity(self) -> 'PointSourceNormalizationAuthority':
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_hash_sha256 != expected:
            raise ValueError('point-source normalization semantic hash mismatch')
        if self.authority_id != f'r150-point-source-normalization:{expected}':
            raise ValueError('point-source normalization id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'authority_id', 'semantic_hash_sha256'})

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return _identity_ref(
            authority_id=self.authority_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_hash_sha256,
        )


def build_point_source_normalization_authority(
    *,
    valid_frequency_domain: FrequencyDomain,
    provenance: str = 'analytic linear-acoustics monopole Green-function reference',
) -> PointSourceNormalizationAuthority:
    core = {
        'authority_version': '1',
        'model': POINT_SOURCE_MODEL,
        'quantity': TRANSFER_QUANTITY,
        'unit': TRANSFER_UNIT,
        'source_reference': 'unit_volume_velocity_m3_s',
        'phasor_convention': PHASOR_CONVENTION,
        'green_function_convention': GREEN_FUNCTION_CONVENTION,
        'provenance_state': 'analytic',
        'provenance': provenance,
        'valid_frequency_domain': valid_frequency_domain.model_dump(mode='json'),
    }
    digest = _semantic_hash(core)
    return PointSourceNormalizationAuthority(
        authority_id=f'r150-point-source-normalization:{digest}',
        semantic_hash_sha256=digest,
        **core,
    )


class SourceResponseAuthority(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_id: str = Field(pattern=r'^r150-source-response:[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_entity_id: str = Field(min_length=1)
    r110_source_ref: ExactExternalAuthorityRef
    equipment_definition_ref: ExactExternalAuthorityRef
    capability: SourceCapability
    directivity_dataset_ref: ExactExternalAuthorityRef | None = None
    analytic_directivity_model: str | None = None
    point_source_normalization_ref: ExactExternalAuthorityRef | None = None
    valid_frequency_domain: FrequencyDomain | None = None
    phase_reference: str | None = None
    unsupported_reason: str | None = None

    @model_validator(mode='after')
    def identity(self) -> 'SourceResponseAuthority':
        if self.capability == 'COMPLEX_DIRECTIONAL_TRANSFER_AVAILABLE':
            if self.directivity_dataset_ref is None or self.phase_reference is None:
                raise ValueError('complex directional source requires dataset and phase reference')
        elif self.capability == 'MAGNITUDE_ONLY_DIRECTIVITY':
            if self.directivity_dataset_ref is None or self.phase_reference is not None:
                raise ValueError('magnitude-only source requires dataset without phase reference')
        elif self.capability == 'ANALYTIC_OMNIDIRECTIONAL_MODEL':
            if self.analytic_directivity_model != ANALYTIC_OMNI_DIRECTIVITY_MODEL:
                raise ValueError('analytic source requires explicit supported omnidirectional model')
            if self.phase_reference is None:
                raise ValueError('analytic coherent source requires explicit phase reference')
        elif self.unsupported_reason is None:
            raise ValueError('unsupported source capability requires a reason')
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_hash_sha256 != expected:
            raise ValueError('source response authority semantic hash mismatch')
        if self.authority_id != f'r150-source-response:{expected}':
            raise ValueError('source response authority id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'authority_id', 'semantic_hash_sha256'})

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return _identity_ref(
            authority_id=self.authority_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_hash_sha256,
        )


def build_source_response_authority(
    *,
    source_entity_id: str,
    r110_source_ref: ExactExternalAuthorityRef,
    equipment_definition: EquipmentDefinition,
    directivity_dataset: DirectivityDataset | None = None,
    point_source_normalization: PointSourceNormalizationAuthority | None = None,
) -> SourceResponseAuthority:
    directivity = equipment_definition.directivity
    equipment_ref = _identity_ref(
        authority_id=equipment_definition.definition_id,
        authority_version=equipment_definition.version,
        semantic_hash_sha256=equipment_definition.semantic_sha256,
    )
    dataset_ref: ExactExternalAuthorityRef | None = None
    capability: SourceCapability
    analytic_model: str | None = None
    valid_band = directivity.valid_domain.frequency if directivity.valid_domain is not None else None
    phase_reference: str | None = None
    unsupported_reason: str | None = None

    if directivity.tier in {'complex', 'magnitude_only'}:
        if directivity_dataset is None:
            capability = 'UNSUPPORTED_UNKNOWN_DIRECTIVITY'
            unsupported_reason = 'declared imported directivity is missing exact DirectivityDataset authority'
        else:
            if (
                directivity_dataset.equipment_definition_id != equipment_definition.definition_id
                or directivity_dataset.equipment_definition_version != equipment_definition.version
                or directivity_dataset.equipment_definition_sha256 != equipment_definition.semantic_sha256
            ):
                raise ValueError('DirectivityDataset does not bind the supplied EquipmentDefinition')
            if directivity_dataset.kind != directivity.tier:
                raise ValueError(
                    'DirectivityDataset kind does not match EquipmentDefinition directivity tier'
                )
            dataset_ref = _identity_ref(
                authority_id=directivity_dataset.dataset_id,
                authority_version=directivity_dataset.version,
                semantic_hash_sha256=directivity_dataset.semantic_sha256,
            )
            valid_band = directivity_dataset.valid_domain.frequency
            if directivity.tier == 'complex':
                if directivity_dataset.phase_reference != directivity.phase_reference:
                    raise ValueError(
                        'DirectivityDataset phase reference does not match EquipmentDefinition'
                    )
                capability = 'COMPLEX_DIRECTIONAL_TRANSFER_AVAILABLE'
                phase_reference = directivity.phase_reference
            else:
                capability = 'MAGNITUDE_ONLY_DIRECTIVITY'
    elif directivity.tier == 'analytic':
        if (
            directivity.analytic_model == ANALYTIC_OMNI_DIRECTIVITY_MODEL
            and directivity.coherent_phase
            and directivity.phase_reference is not None
        ):
            capability = 'ANALYTIC_OMNIDIRECTIONAL_MODEL'
            analytic_model = directivity.analytic_model
            phase_reference = directivity.phase_reference
        else:
            capability = 'UNSUPPORTED_UNKNOWN_DIRECTIVITY'
            unsupported_reason = (
                'analytic directivity is not the explicit supported coherent omnidirectional model'
            )
    else:
        capability = 'UNSUPPORTED_UNKNOWN_DIRECTIVITY'
        unsupported_reason = (
            'source directivity is unknown or lacks exact directional transfer authority'
        )

    core = {
        'authority_version': '1',
        'source_entity_id': source_entity_id,
        'r110_source_ref': r110_source_ref.model_dump(mode='json'),
        'equipment_definition_ref': equipment_ref.model_dump(mode='json'),
        'capability': capability,
        'directivity_dataset_ref': (
            dataset_ref.model_dump(mode='json') if dataset_ref is not None else None
        ),
        'analytic_directivity_model': analytic_model,
        'point_source_normalization_ref': (
            point_source_normalization.as_external_ref().model_dump(mode='json')
            if point_source_normalization is not None
            else None
        ),
        'valid_frequency_domain': (
            valid_band.model_dump(mode='json') if valid_band is not None else None
        ),
        'phase_reference': phase_reference,
        'unsupported_reason': unsupported_reason,
    }
    digest = _semantic_hash(core)
    return SourceResponseAuthority(
        authority_id=f'r150-source-response:{digest}',
        semantic_hash_sha256=digest,
        **core,
    )


class ReceiverResponseAuthority(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_id: str = Field(pattern=r'^r150-receiver-response:[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    receiver_id: str = Field(min_length=1)
    receiver_entity_id: str = Field(min_length=1)
    receiver_authority_ref: ExactExternalAuthorityRef
    world_position: Position3
    observable: Literal['ideal_point_acoustic_pressure'] = 'ideal_point_acoustic_pressure'

    @model_validator(mode='after')
    def identity(self) -> 'ReceiverResponseAuthority':
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_hash_sha256 != expected:
            raise ValueError('receiver response authority semantic hash mismatch')
        if self.authority_id != f'r150-receiver-response:{expected}':
            raise ValueError('receiver response authority id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'authority_id', 'semantic_hash_sha256'})

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return _identity_ref(
            authority_id=self.authority_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_hash_sha256,
        )


def build_receiver_response_authority(
    *,
    receiver_id: str,
    receiver_entity_id: str,
    receiver_authority_ref: ExactExternalAuthorityRef,
    world_position: Position3,
) -> ReceiverResponseAuthority:
    core = {
        'authority_version': '1',
        'receiver_id': receiver_id,
        'receiver_entity_id': receiver_entity_id,
        'receiver_authority_ref': receiver_authority_ref.model_dump(mode='json'),
        'world_position': world_position.model_dump(mode='json'),
        'observable': 'ideal_point_acoustic_pressure',
    }
    digest = _semantic_hash(core)
    return ReceiverResponseAuthority(
        authority_id=f'r150-receiver-response:{digest}',
        semantic_hash_sha256=digest,
        **core,
    )


class SurfaceReflectionTransferAuthority(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_id: str = Field(pattern=r'^r150-surface-reflection:[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')
    r120_geometry_ref: ExactExternalAuthorityRef
    material_authority_ref: ExactExternalAuthorityRef
    boundary_physics_authority_ref: ExactExternalAuthorityRef | None = None
    capability: ReflectionCapability
    derivation: Literal[
        'mathematically_exact_rigid_boundary',
        'specific_impedance_local_reaction',
        'explicit_complex_reflection_transfer',
        'geometric_absorption_scattering_magnitude_only',
    ]
    incidence_cosine: float | None = Field(default=None, gt=0.0, le=1.0)
    # Declared incidence semantics of this transfer evidence. An angle-specific
    # authority must name the incidence angle; other conditions forbid one.
    # Optional — absent fields keep legacy authority identities valid.
    incidence_condition: ReflectionIncidenceCondition | None = None
    incidence_angle_deg: float | None = Field(default=None, ge=0.0, le=90.0)
    valid_frequency_domain: FrequencyDomain
    samples: tuple[ComplexTransferSample, ...] = Field(min_length=1)
    provenance: str = Field(min_length=1)

    @model_validator(mode='after')
    def identity(self) -> 'SurfaceReflectionTransferAuthority':
        frequencies = tuple(sample.frequency_hz for sample in self.samples)
        if frequencies != tuple(sorted(set(frequencies))):
            raise ValueError('surface reflection frequency samples must be unique and sorted')
        if self.capability == 'COMPLEX':
            if any(sample.complex_value() is None for sample in self.samples):
                raise ValueError('complex reflection authority requires complex samples')
        elif any(sample.complex_value() is not None for sample in self.samples):
            raise ValueError('magnitude-only reflection authority cannot carry phase')
        if self.derivation == 'specific_impedance_local_reaction' and self.incidence_cosine is None:
            raise ValueError('specific-impedance reflection requires explicit incidence cosine')
        if self.incidence_condition == 'angle_specific':
            if self.incidence_angle_deg is None:
                raise ValueError(
                    'angle_specific reflection authority requires an explicit '
                    'incidence angle'
                )
        elif self.incidence_angle_deg is not None:
            raise ValueError(
                'incidence angle is only meaningful for angle_specific '
                'reflection evidence'
            )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_hash_sha256 != expected:
            raise ValueError('surface reflection semantic hash mismatch')
        if self.authority_id != f'r150-surface-reflection:{expected}':
            raise ValueError('surface reflection id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'authority_id', 'semantic_hash_sha256'},
        )
        if self.incidence_condition is None:
            payload.pop('incidence_condition', None)
        if self.incidence_angle_deg is None:
            payload.pop('incidence_angle_deg', None)
        return payload

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return _identity_ref(
            authority_id=self.authority_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_hash_sha256,
        )


def _domain_from_samples(samples: Sequence[ComplexTransferSample]) -> FrequencyDomain:
    if not samples:
        raise ValueError('transfer authority requires at least one frequency sample')
    frequencies = [item.frequency_hz for item in samples]
    if len(frequencies) == 1:
        return FrequencyDomain(
            minimum_hz=frequencies[0] * (1.0 - 1e-12),
            maximum_hz=frequencies[0] * (1.0 + 1e-12),
        )
    return FrequencyDomain(minimum_hz=min(frequencies), maximum_hz=max(frequencies))


def _build_surface_reflection(
    *,
    source_surface_id: str,
    r120_geometry_ref: ExactExternalAuthorityRef,
    material_authority_ref: ExactExternalAuthorityRef,
    boundary_physics_authority_ref: ExactExternalAuthorityRef | None,
    capability: ReflectionCapability,
    derivation: str,
    samples: Sequence[ComplexTransferSample],
    provenance: str,
    incidence_cosine: float | None = None,
    incidence_condition: ReflectionIncidenceCondition | None = None,
    incidence_angle_deg: float | None = None,
) -> SurfaceReflectionTransferAuthority:
    sample_items = tuple(samples)
    core = {
        'authority_version': '1',
        'source_surface_id': source_surface_id,
        'r120_geometry_ref': r120_geometry_ref.model_dump(mode='json'),
        'material_authority_ref': material_authority_ref.model_dump(mode='json'),
        'boundary_physics_authority_ref': (
            boundary_physics_authority_ref.model_dump(mode='json')
            if boundary_physics_authority_ref is not None
            else None
        ),
        'capability': capability,
        'derivation': derivation,
        'incidence_cosine': incidence_cosine,
        'valid_frequency_domain': _domain_from_samples(sample_items).model_dump(mode='json'),
        'samples': [item.model_dump(mode='json') for item in sample_items],
        'provenance': provenance,
    }
    if incidence_condition is not None:
        core['incidence_condition'] = incidence_condition
    if incidence_angle_deg is not None:
        core['incidence_angle_deg'] = float(incidence_angle_deg)
    digest = _semantic_hash(core)
    return SurfaceReflectionTransferAuthority(
        authority_id=f'r150-surface-reflection:{digest}',
        semantic_hash_sha256=digest,
        **core,
    )


def build_rigid_surface_reflection_authority(
    *,
    source_surface_id: str,
    r120_geometry_ref: ExactExternalAuthorityRef,
    material_authority_ref: ExactExternalAuthorityRef,
    material: AcousticMaterial,
    frequency_grid: FrequencyGridAuthority,
    boundary_physics_authority_ref: ExactExternalAuthorityRef | None = None,
) -> SurfaceReflectionTransferAuthority:
    if material.wave_model != 'rigid':
        raise ValueError('rigid reflection authority requires material.wave_model=rigid')
    return _build_surface_reflection(
        source_surface_id=source_surface_id,
        r120_geometry_ref=r120_geometry_ref,
        material_authority_ref=material_authority_ref,
        boundary_physics_authority_ref=boundary_physics_authority_ref,
        capability='COMPLEX',
        derivation='mathematically_exact_rigid_boundary',
        samples=tuple(
            ComplexTransferSample.from_complex(frequency, 1.0 + 0.0j)
            for frequency in frequency_grid.frequencies_hz
        ),
        provenance=f'{material.material_id}@{material.version}: exact rigid pressure reflection +1',
        incidence_condition='angle_independent',
    )


def build_specific_impedance_surface_reflection_authority(
    *,
    source_surface_id: str,
    r120_geometry_ref: ExactExternalAuthorityRef,
    material_authority_ref: ExactExternalAuthorityRef,
    material: AcousticMaterial,
    environment: AcousticEnvironmentAuthority,
    frequency_grid: FrequencyGridAuthority,
    incidence_cosine: float,
    boundary_physics_authority_ref: ExactExternalAuthorityRef | None = None,
) -> SurfaceReflectionTransferAuthority:
    if material.wave_model != 'specific_impedance_table':
        raise ValueError('specific-impedance reflection requires matching wave material')
    mu = float(incidence_cosine)
    if not isfinite(mu) or not (0.0 < mu <= 1.0):
        raise ValueError('incidence_cosine must lie within (0, 1]')
    impedance_by_frequency = {
        item.frequency_hz: complex(item.resistance_pa_s_m, item.reactance_pa_s_m)
        for item in material.specific_impedance
    }
    characteristic = environment.density_kg_m3 * environment.sound_speed_m_s
    samples: list[ComplexTransferSample] = []
    for frequency in frequency_grid.frequencies_hz:
        impedance = impedance_by_frequency.get(frequency)
        if impedance is None:
            raise ValueError(
                'specific-impedance reflection requires an exact impedance sample at every response frequency'
            )
        denominator = impedance * mu + characteristic
        if abs(denominator) <= 1e-15:
            raise ValueError('specific-impedance reflection denominator is singular')
        coefficient = (impedance * mu - characteristic) / denominator
        samples.append(ComplexTransferSample.from_complex(frequency, coefficient))
    return _build_surface_reflection(
        source_surface_id=source_surface_id,
        r120_geometry_ref=r120_geometry_ref,
        material_authority_ref=material_authority_ref,
        boundary_physics_authority_ref=boundary_physics_authority_ref,
        capability='COMPLEX',
        derivation='specific_impedance_local_reaction',
        samples=samples,
        incidence_cosine=mu,
        incidence_condition='angle_specific',
        incidence_angle_deg=degrees(acos(mu)),
        provenance=(
            f'{material.material_id}@{material.version}: local-reaction pressure reflection '
            'R=(Z*cos(theta)-rho*c)/(Z*cos(theta)+rho*c)'
        ),
    )


def build_explicit_complex_surface_reflection_authority(
    *,
    source_surface_id: str,
    r120_geometry_ref: ExactExternalAuthorityRef,
    material_authority_ref: ExactExternalAuthorityRef,
    frequency_coefficients: Mapping[float, complex],
    provenance: str,
    boundary_physics_authority_ref: ExactExternalAuthorityRef | None = None,
    incidence_condition: ReflectionIncidenceCondition | None = None,
    incidence_angle_deg: float | None = None,
) -> SurfaceReflectionTransferAuthority:
    samples = tuple(
        ComplexTransferSample.from_complex(float(frequency), complex(value))
        for frequency, value in sorted(frequency_coefficients.items())
    )
    return _build_surface_reflection(
        source_surface_id=source_surface_id,
        r120_geometry_ref=r120_geometry_ref,
        material_authority_ref=material_authority_ref,
        boundary_physics_authority_ref=boundary_physics_authority_ref,
        capability='COMPLEX',
        derivation='explicit_complex_reflection_transfer',
        samples=samples,
        provenance=provenance,
        incidence_condition=incidence_condition,
        incidence_angle_deg=incidence_angle_deg,
    )


def build_magnitude_only_surface_reflection_authority(
    *,
    source_surface_id: str,
    r120_geometry_ref: ExactExternalAuthorityRef,
    material_authority_ref: ExactExternalAuthorityRef,
    material: AcousticMaterial,
    frequency_grid: FrequencyGridAuthority,
    boundary_physics_authority_ref: ExactExternalAuthorityRef | None = None,
) -> SurfaceReflectionTransferAuthority:
    if material.geometric_model != 'banded':
        raise ValueError('magnitude-only reflection requires geometric band material data')
    bands = {item.center_hz: item for item in material.geometric_bands}
    samples: list[ComplexTransferSample] = []
    for frequency in frequency_grid.frequencies_hz:
        band = bands.get(frequency)
        if band is None:
            raise ValueError('geometric reflection requires an exact material band center')
        magnitude = sqrt(max(0.0, (1.0 - band.absorption) * (1.0 - band.scattering)))
        samples.append(ComplexTransferSample.magnitude_only(frequency, magnitude))
    incidence_conditions = {item.incidence_condition for item in material.geometric_bands}
    incidence_angles = {item.incidence_angle_deg for item in material.geometric_bands}
    incidence_condition: ReflectionIncidenceCondition | None = None
    incidence_angle_deg: float | None = None
    if len(incidence_conditions) == 1:
        uniform = next(iter(incidence_conditions))
        incidence_condition = uniform
        if uniform == 'angle_specific' and len(incidence_angles) == 1:
            incidence_angle_deg = next(iter(incidence_angles))
        elif uniform == 'angle_specific':
            # Bands carry different declared incidence angles; the composite
            # authority cannot claim one angle, so it stays explicitly unknown.
            incidence_condition = 'unknown_incidence'
    else:
        incidence_condition = 'unknown_incidence'
    return _build_surface_reflection(
        source_surface_id=source_surface_id,
        r120_geometry_ref=r120_geometry_ref,
        material_authority_ref=material_authority_ref,
        boundary_physics_authority_ref=boundary_physics_authority_ref,
        capability='MAGNITUDE_ONLY',
        derivation='geometric_absorption_scattering_magnitude_only',
        samples=samples,
        provenance=(
            f'{material.material_id}@{material.version}: amplitude magnitude from '
            'sqrt((1-absorption)*(1-scattering)); phase explicitly unavailable'
        ),
        incidence_condition=incidence_condition,
        incidence_angle_deg=incidence_angle_deg,
    )


class PortalAcousticTransferAuthority(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_id: str = Field(pattern=r'^r150-portal-transfer:[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    portal_id: str = Field(min_length=1)
    from_region_id: str = Field(min_length=1)
    to_region_id: str = Field(min_length=1)
    portal_geometry_authority_ref: ExactExternalAuthorityRef
    quantity: Literal['complex_pressure_transmission_ratio'] = (
        'complex_pressure_transmission_ratio'
    )
    unit: Literal['dimensionless'] = 'dimensionless'
    valid_frequency_domain: FrequencyDomain
    samples: tuple[ComplexTransferSample, ...] = Field(min_length=1)
    provenance: str = Field(min_length=1)
    provenance_state: PortalProvenanceState
    uncertainty: str = Field(min_length=1)
    approximation_version: str | None = None
    approximation_tolerance: str | None = None

    @model_validator(mode='after')
    def identity(self) -> 'PortalAcousticTransferAuthority':
        if self.from_region_id == self.to_region_id:
            raise ValueError('Portal transfer direction requires distinct regions')
        if any(sample.complex_value() is None for sample in self.samples):
            raise ValueError('Portal transmission authority requires explicit complex samples')
        frequencies = tuple(sample.frequency_hz for sample in self.samples)
        if frequencies != tuple(sorted(set(frequencies))):
            raise ValueError('Portal transfer frequency samples must be unique and sorted')
        if self.provenance_state == 'analytic':
            if self.approximation_version is None or self.approximation_tolerance is None:
                raise ValueError(
                    'analytic Portal approximation requires version and tolerance/validity statement'
                )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_hash_sha256 != expected:
            raise ValueError('Portal transfer semantic hash mismatch')
        if self.authority_id != f'r150-portal-transfer:{expected}':
            raise ValueError('Portal transfer id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'authority_id', 'semantic_hash_sha256'})

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return _identity_ref(
            authority_id=self.authority_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_hash_sha256,
        )


def build_portal_acoustic_transfer_authority(
    *,
    portal_id: str,
    from_region_id: str,
    to_region_id: str,
    portal_geometry_authority_ref: ExactExternalAuthorityRef,
    frequency_coefficients: Mapping[float, complex],
    provenance: str,
    provenance_state: PortalProvenanceState,
    uncertainty: str,
    approximation_version: str | None = None,
    approximation_tolerance: str | None = None,
) -> PortalAcousticTransferAuthority:
    samples = tuple(
        ComplexTransferSample.from_complex(float(frequency), complex(value))
        for frequency, value in sorted(frequency_coefficients.items())
    )
    core = {
        'authority_version': '1',
        'portal_id': portal_id,
        'from_region_id': from_region_id,
        'to_region_id': to_region_id,
        'portal_geometry_authority_ref': portal_geometry_authority_ref.model_dump(mode='json'),
        'quantity': 'complex_pressure_transmission_ratio',
        'unit': 'dimensionless',
        'valid_frequency_domain': _domain_from_samples(samples).model_dump(mode='json'),
        'samples': [item.model_dump(mode='json') for item in samples],
        'provenance': provenance,
        'provenance_state': provenance_state,
        'uncertainty': uncertainty,
        'approximation_version': approximation_version,
        'approximation_tolerance': approximation_tolerance,
    }
    digest = _semantic_hash(core)
    return PortalAcousticTransferAuthority(
        authority_id=f'r150-portal-transfer:{digest}',
        semantic_hash_sha256=digest,
        **core,
    )


def build_open_aperture_unity_transfer_authority(
    *,
    portal_id: str,
    from_region_id: str,
    to_region_id: str,
    portal_geometry_authority_ref: ExactExternalAuthorityRef,
    frequency_grid: FrequencyGridAuthority,
    approximation_version: str,
    approximation_tolerance: str,
    validity_statement: str,
) -> PortalAcousticTransferAuthority:
    return build_portal_acoustic_transfer_authority(
        portal_id=portal_id,
        from_region_id=from_region_id,
        to_region_id=to_region_id,
        portal_geometry_authority_ref=portal_geometry_authority_ref,
        frequency_coefficients={
            frequency: 1.0 + 0.0j for frequency in frequency_grid.frequencies_hz
        },
        provenance=f'analytic open-aperture unity approximation: {validity_statement}',
        provenance_state='analytic',
        uncertainty='bounded only by the declared approximation validity/tolerance',
        approximation_version=approximation_version,
        approximation_tolerance=approximation_tolerance,
    )


class PathFrequencyResponseSample(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    propagation_phase_rad: float
    geometric_spreading_per_m: float = Field(gt=0.0)
    magnitude_pa_per_m3_s: float = Field(ge=0.0)
    phase_rad: float | None = None
    complex_real_pa_per_m3_s: float | None = None
    complex_imag_pa_per_m3_s: float | None = None

    @model_validator(mode='after')
    def valid_complex(self) -> 'PathFrequencyResponseSample':
        for value in (
            self.frequency_hz,
            self.propagation_phase_rad,
            self.geometric_spreading_per_m,
            self.magnitude_pa_per_m3_s,
            self.phase_rad,
            self.complex_real_pa_per_m3_s,
            self.complex_imag_pa_per_m3_s,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('path response sample values must be finite')
        present = (
            self.phase_rad is not None,
            self.complex_real_pa_per_m3_s is not None,
            self.complex_imag_pa_per_m3_s is not None,
        )
        if len(set(present)) != 1:
            raise ValueError('path response complex phase/real/imag must be present together')
        if all(present):
            value = complex(
                self.complex_real_pa_per_m3_s,
                self.complex_imag_pa_per_m3_s,
            )
            if abs(abs(value) - self.magnitude_pa_per_m3_s) > max(
                1e-12, self.magnitude_pa_per_m3_s * 1e-10
            ):
                raise ValueError('path response sample magnitude mismatch')
            phase_error = atan2(
                sin(float(self.phase_rad) - _phase(value)),
                cos(float(self.phase_rad) - _phase(value)),
            )
            if abs(phase_error) > 1e-10:
                raise ValueError('path response phase does not match real/imag')
        return self


class DeterministicPathFrequencyResponseArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = R150_PATH_RESPONSE_SCHEMA_VERSION
    authority_version: Literal['r150-complex-path-response-1'] = (
        R150_PATH_RESPONSE_AUTHORITY_VERSION
    )
    artifact_id: str = Field(pattern=r'^r150-path-frequency-response:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    deterministic_path_artifact_id: str
    deterministic_path_artifact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    execution_input_ref: ExactExternalAuthorityRef
    deterministic_path_id: str
    deterministic_path_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_entity_id: str
    receiver_id: str
    receiver_entity_id: str
    ordered_surface_interactions: tuple[str, ...]
    ordered_portal_interactions: tuple[str, ...]
    # Per-interaction evaluated incidence (angle from the surface normal and
    # its cosine), aligned to ordered_surface_interactions. Absent when the
    # path artifact predates incidence evaluation.
    ordered_reflection_incidence_angles_deg: tuple[float, ...] | None = None
    ordered_reflection_incidence_cosines: tuple[float, ...] | None = None
    path_length_m: float = Field(gt=0.0)
    quantity: Literal['complex_acoustic_pressure_per_volume_velocity'] = TRANSFER_QUANTITY
    unit: Literal['Pa/(m3/s)'] = TRANSFER_UNIT
    source_normalization: Literal['unit_volume_velocity_m3_s'] = 'unit_volume_velocity_m3_s'
    phasor_convention: Literal['exp(+i*omega*t)'] = PHASOR_CONVENTION
    time_origin: Literal['source_t0'] = 'source_t0'
    sound_speed_m_s: float = Field(gt=0.0)
    density_kg_m3: float = Field(gt=0.0)
    exact_frequency_grid_hz: tuple[float, ...] = Field(min_length=1)
    valid_frequency_domain: FrequencyDomain
    capability: ResponseCapability
    unsupported_reasons: tuple[str, ...] = ()
    samples: tuple[PathFrequencyResponseSample, ...] = ()
    dependency_refs: tuple[ExactExternalAuthorityRef, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def identity(self) -> 'DeterministicPathFrequencyResponseArtifact':
        if self.capability == 'UNSUPPORTED':
            if not self.unsupported_reasons or self.samples:
                raise ValueError('unsupported response requires reasons and no fabricated samples')
        else:
            if self.unsupported_reasons:
                raise ValueError('supported/magnitude response cannot carry unsupported reasons')
            if len(self.samples) != len(self.exact_frequency_grid_hz):
                raise ValueError('response sample count must match exact frequency grid')
            if tuple(item.frequency_hz for item in self.samples) != self.exact_frequency_grid_hz:
                raise ValueError('response samples must preserve exact frequency-grid ordering')
            if self.capability == 'COMPLEX_SUPPORTED':
                if any(item.phase_rad is None for item in self.samples):
                    raise ValueError('complex-supported response requires phase for every sample')
            elif any(item.phase_rad is not None for item in self.samples):
                raise ValueError('magnitude-only response must not fabricate phase')
        keys = tuple(_ref_payload(item) for item in self.dependency_refs)
        if keys != tuple(sorted(set(keys))):
            raise ValueError('response dependencies must be exact, unique, and canonically sorted')
        if _ref_payload(self.execution_input_ref) not in keys:
            raise ValueError('response dependency set must include exact execution input')
        incidence_parts = (
            self.ordered_reflection_incidence_angles_deg,
            self.ordered_reflection_incidence_cosines,
        )
        if any(item is None for item in incidence_parts) and any(
            item is not None for item in incidence_parts
        ):
            raise ValueError(
                'ordered reflection incidence angles/cosines must be supplied together'
            )
        if self.ordered_reflection_incidence_cosines is not None:
            if len(self.ordered_reflection_incidence_cosines) != len(
                self.ordered_surface_interactions
            ):
                raise ValueError(
                    'ordered reflection incidence entries must align to '
                    'ordered surface interactions'
                )
            for index, (angle, cosine) in enumerate(
                zip(
                    self.ordered_reflection_incidence_angles_deg or (),
                    self.ordered_reflection_incidence_cosines,
                    strict=True,
                )
            ):
                if not (0.0 <= float(angle) <= 90.0) or not (
                    0.0 <= float(cosine) <= 1.0
                ):
                    raise ValueError(
                        f'ordered reflection incidence {index} is outside the '
                        'valid angle/cosine domain'
                    )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('path frequency response semantic hash mismatch')
        if self.artifact_id != f'r150-path-frequency-response:{expected}':
            raise ValueError('path frequency response id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json', exclude={'artifact_id', 'semantic_sha256'})
        if self.ordered_reflection_incidence_angles_deg is None:
            payload.pop('ordered_reflection_incidence_angles_deg', None)
        if self.ordered_reflection_incidence_cosines is None:
            payload.pop('ordered_reflection_incidence_cosines', None)
        return payload

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return _identity_ref(
            authority_id=self.artifact_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def _sample_at(
    samples: Sequence[ComplexTransferSample],
    frequency_hz: float,
) -> ComplexTransferSample | None:
    return next((item for item in samples if item.frequency_hz == frequency_hz), None)


def _path_band(path: DeterministicAcousticPath, frequency_hz: float):
    return next((item for item in path.bands if item.center_hz == frequency_hz), None)


def _sorted_unique_refs(refs: Sequence[ExactExternalAuthorityRef]) -> tuple[ExactExternalAuthorityRef, ...]:
    by_key = {_ref_payload(item): item for item in refs}
    return tuple(by_key[key] for key in sorted(by_key))


def _response_artifact(
    *,
    path_artifact: DeterministicPathArtifact,
    path: DeterministicAcousticPath,
    environment: AcousticEnvironmentAuthority,
    frequency_grid: FrequencyGridAuthority,
    dependency_refs: Sequence[ExactExternalAuthorityRef],
    ordered_portal_ids: Sequence[str],
    ordered_reflection_incidences: Sequence[tuple[float, float]] | None = None,
    capability: ResponseCapability,
    unsupported_reasons: Sequence[str] = (),
    samples: Sequence[PathFrequencyResponseSample] = (),
) -> DeterministicPathFrequencyResponseArtifact:
    core = {
        'schema_version': R150_PATH_RESPONSE_SCHEMA_VERSION,
        'authority_version': R150_PATH_RESPONSE_AUTHORITY_VERSION,
        'deterministic_path_artifact_id': path_artifact.artifact_id,
        'deterministic_path_artifact_sha256': path_artifact.semantic_sha256,
        'execution_input_ref': {
            'authority_id': path_artifact.execution_input_id,
            'authority_version': path_artifact.authority_version,
            'semantic_hash_sha256': path_artifact.execution_input_sha256,
        },
        'deterministic_path_id': path.path_id,
        'deterministic_path_sha256': path.semantic_sha256,
        'source_entity_id': path.source_entity_id,
        'receiver_id': path.receiver_id,
        'receiver_entity_id': path.receiver_entity_id,
        'ordered_surface_interactions': list(path.ordered_interaction_surface_ids),
        'ordered_portal_interactions': list(ordered_portal_ids),
        'path_length_m': path.geometric_path_length_m,
        'quantity': TRANSFER_QUANTITY,
        'unit': TRANSFER_UNIT,
        'source_normalization': 'unit_volume_velocity_m3_s',
        'phasor_convention': PHASOR_CONVENTION,
        'time_origin': 'source_t0',
        'sound_speed_m_s': environment.sound_speed_m_s,
        'density_kg_m3': environment.density_kg_m3,
        'exact_frequency_grid_hz': list(frequency_grid.frequencies_hz),
        'valid_frequency_domain': frequency_grid.valid_frequency_domain.model_dump(mode='json'),
        'capability': capability,
        'unsupported_reasons': sorted(set(unsupported_reasons)),
        'samples': [item.model_dump(mode='json') for item in samples],
        'dependency_refs': [
            item.model_dump(mode='json') for item in _sorted_unique_refs(dependency_refs)
        ],
    }
    if ordered_reflection_incidences is not None:
        core['ordered_reflection_incidence_angles_deg'] = [
            item[0] for item in ordered_reflection_incidences
        ]
        core['ordered_reflection_incidence_cosines'] = [
            item[1] for item in ordered_reflection_incidences
        ]
    digest = _semantic_hash(core)
    return DeterministicPathFrequencyResponseArtifact(
        artifact_id=f'r150-path-frequency-response:{digest}',
        semantic_sha256=digest,
        **core,
    )


def _plane_unit_normal(plane: GeometricSurfacePlane) -> tuple[float, float, float] | None:
    if plane.point_m is not None and plane.normal is not None:
        normal = (
            float(plane.normal.x),
            float(plane.normal.y),
            float(plane.normal.z),
        )
        length = sqrt(sum(item * item for item in normal))
        if length <= 0.0:
            return None
        return (
            normal[0] / length,
            normal[1] / length,
            normal[2] / length,
        )
    if plane.axis is None:
        return None
    normal = [0.0, 0.0, 0.0]
    normal[{'x': 0, 'y': 1, 'z': 2}[plane.axis]] = 1.0
    return (normal[0], normal[1], normal[2])


def _incidence_from_direction(
    incoming: tuple[float, float, float],
    normal: tuple[float, float, float],
) -> tuple[float, float] | None:
    length = sqrt(sum(item * item for item in incoming))
    if length <= 0.0:
        return None
    cosine = abs(
        sum(incoming[index] * normal[index] for index in range(3))
    ) / length
    cosine = min(1.0, max(0.0, cosine))
    return (degrees(acos(cosine)), cosine)


def _path_reflection_incidences(
    path: DeterministicAcousticPath,
    execution_input: DeterministicGaExecutionInput,
) -> tuple[tuple[float, float], ...] | None:
    """Evaluated (angle_deg, cosine) per ordered surface interaction.

    Incidence is derived from the exact incoming propagation segment of each
    reflection (the departure direction for the first interaction, or the prior
    interaction's point for later reflections and Portal crossings) against the
    declared surface-plane normal. Returns None when the path artifact does not
    carry enough geometry to derive incidence for every reflection.
    """
    surface_ids = path.ordered_interaction_surface_ids
    if not surface_ids:
        return ()
    if 'boundary_planes' not in execution_input.model_fields_set:
        return None
    normals: dict[str, tuple[float, float, float]] = {}
    for plane in execution_input.boundary_planes:
        normal = _plane_unit_normal(plane)
        if normal is not None:
            normals[plane.source_surface_id] = normal
    if any(item not in normals for item in surface_ids):
        return None
    departure = (
        float(path.departure_direction.x),
        float(path.departure_direction.y),
        float(path.departure_direction.z),
    )
    incidences: list[tuple[float, float]] = []
    if path.ordered_interactions:
        previous_point: tuple[float, float, float] | None = None
        for interaction in path.ordered_interactions:
            point = (
                float(interaction.point.x_m),
                float(interaction.point.y_m),
                float(interaction.point.z_m),
            )
            if interaction.kind == 'portal_crossing':
                previous_point = point
                continue
            if previous_point is None:
                incoming = departure
            else:
                incoming = (
                    point[0] - previous_point[0],
                    point[1] - previous_point[1],
                    point[2] - previous_point[2],
                )
            assert interaction.surface_id is not None
            incidence = _incidence_from_direction(
                incoming,
                normals[interaction.surface_id],
            )
            if incidence is None:
                return None
            incidences.append(incidence)
            previous_point = point
    else:
        for index, surface_id in enumerate(surface_ids):
            if index == 0:
                incoming = departure
            else:
                current = path.ordered_interaction_points[index]
                prior = path.ordered_interaction_points[index - 1]
                incoming = (
                    float(current.x_m) - float(prior.x_m),
                    float(current.y_m) - float(prior.y_m),
                    float(current.z_m) - float(prior.z_m),
                )
            incidence = _incidence_from_direction(incoming, normals[surface_id])
            if incidence is None:
                return None
            incidences.append(incidence)
    if len(incidences) != len(surface_ids):
        return None
    return tuple(incidences)


def build_deterministic_path_frequency_response(
    *,
    path_artifact: DeterministicPathArtifact,
    execution_input: DeterministicGaExecutionInput,
    path_id: str,
    r120_geometry_ref: ExactExternalAuthorityRef,
    source_authority: SourceResponseAuthority,
    point_source_normalization: PointSourceNormalizationAuthority | None,
    receiver_authority: ReceiverResponseAuthority,
    environment: AcousticEnvironmentAuthority,
    frequency_grid: FrequencyGridAuthority,
    configuration: PathResponseConfiguration,
    surface_reflections: Mapping[str, SurfaceReflectionTransferAuthority] = {},
    portal_geometry_authority_ref: ExactExternalAuthorityRef | None = None,
    portal_transfers: Mapping[
        tuple[str, str, str], PortalAcousticTransferAuthority
    ] = {},
    directivity_dataset: DirectivityDataset | None = None,
) -> DeterministicPathFrequencyResponseArtifact:
    path = next((item for item in path_artifact.paths if item.path_id == path_id), None)
    if path is None:
        raise ValueError('deterministic path id is not present in the supplied path artifact')

    execution_ref = _execution_input_ref(execution_input)
    path_execution_ref = _identity_ref(
        authority_id=path_artifact.execution_input_id,
        authority_version=path_artifact.authority_version,
        semantic_hash_sha256=path_artifact.execution_input_sha256,
    )
    refs: list[ExactExternalAuthorityRef] = [
        execution_ref,
        path_execution_ref,
        r120_geometry_ref,
        source_authority.as_external_ref(),
        source_authority.r110_source_ref,
        source_authority.equipment_definition_ref,
        receiver_authority.as_external_ref(),
        receiver_authority.receiver_authority_ref,
        environment.as_external_ref(),
        frequency_grid.as_external_ref(),
        configuration.as_external_ref(),
    ]
    if point_source_normalization is not None:
        refs.append(point_source_normalization.as_external_ref())
    if source_authority.directivity_dataset_ref is not None:
        refs.append(source_authority.directivity_dataset_ref)

    reasons: list[str] = []
    if (
        path_artifact.execution_input_id != execution_input.execution_input_id
        or path_artifact.execution_input_sha256 != execution_input.semantic_sha256
    ):
        reasons.append('STALE_OR_MISMATCHED_EXECUTION_INPUT')
    if (
        execution_input.r120_compiled_geometry_id != r120_geometry_ref.authority_id
        or execution_input.r120_compiled_geometry_sha256
        != r120_geometry_ref.semantic_hash_sha256
    ):
        reasons.append('EXECUTION_INPUT_R120_GEOMETRY_MISMATCH')
    if abs(execution_input.sound_speed_m_s - environment.sound_speed_m_s) > max(
        1e-12, environment.sound_speed_m_s * 1e-12
    ):
        reasons.append('EXECUTION_INPUT_ENVIRONMENT_MISMATCH')

    execution_sources = [
        item
        for item in execution_input.sources
        if item.source_entity_id == path.source_entity_id
    ]
    execution_source = execution_sources[0] if len(execution_sources) == 1 else None
    if execution_source is None:
        reasons.append('SOURCE_EXECUTION_BINDING_MISMATCH')
    else:
        if (
            execution_source.r110_compiled_source_sha256
            != source_authority.r110_source_ref.semantic_hash_sha256
        ):
            reasons.append('SOURCE_R110_AUTHORITY_MISMATCH')
        if source_authority.directivity_dataset_ref is not None and (
            execution_source.directivity_dataset_id
            != source_authority.directivity_dataset_ref.authority_id
            or execution_source.directivity_dataset_version
            != source_authority.directivity_dataset_ref.authority_version
            or execution_source.directivity_dataset_sha256
            != source_authority.directivity_dataset_ref.semantic_hash_sha256
        ):
            reasons.append('SOURCE_DIRECTIVITY_EXECUTION_BINDING_MISMATCH')

    execution_receivers = [
        item
        for item in execution_input.receivers
        if item.receiver_id == path.receiver_id
        and item.entity_id == path.receiver_entity_id
    ]
    if len(execution_receivers) != 1:
        reasons.append('RECEIVER_EXECUTION_BINDING_MISMATCH')
    elif execution_receivers[0].world_position != receiver_authority.world_position:
        reasons.append('RECEIVER_POSITION_MISMATCH')

    if (
        path_artifact.r120_compiled_geometry_id != r120_geometry_ref.authority_id
        or path_artifact.r120_compiled_geometry_sha256
        != r120_geometry_ref.semantic_hash_sha256
    ):
        reasons.append('STALE_OR_MISMATCHED_R120_GEOMETRY')
    if source_authority.source_entity_id != path.source_entity_id:
        reasons.append('SOURCE_IDENTITY_MISMATCH')
    if (
        receiver_authority.receiver_id != path.receiver_id
        or receiver_authority.receiver_entity_id != path.receiver_entity_id
    ):
        reasons.append('RECEIVER_IDENTITY_MISMATCH')
    if (
        not isfinite(path.geometric_path_length_m)
        or path.geometric_path_length_m <= configuration.minimum_path_length_m
    ):
        reasons.append('ZERO_OR_NEAR_SINGULAR_PATH_LENGTH')
    expected_delay = path.geometric_path_length_m / environment.sound_speed_m_s
    if abs(path.propagation_delay_s - expected_delay) > max(1e-12, expected_delay * 1e-9):
        reasons.append('ENVIRONMENT_PATH_DELAY_MISMATCH')

    for frequency in frequency_grid.frequencies_hz:
        if not path_artifact.frequency_domain.contains(frequency):
            reasons.append('PATH_ARTIFACT_VALID_BAND_MISMATCH')
            break
        if not execution_input.frequency_domain.contains(frequency):
            reasons.append('EXECUTION_INPUT_VALID_BAND_MISMATCH')
            break
        if not environment.valid_frequency_domain.contains(frequency):
            reasons.append('ENVIRONMENT_VALID_BAND_MISMATCH')
            break
        if not configuration.minimum_path_length_m > 0.0:
            reasons.append('INVALID_RESPONSE_CONFIGURATION')
            break
        if source_authority.valid_frequency_domain is not None and not source_authority.valid_frequency_domain.contains(frequency):
            reasons.append('SOURCE_VALID_BAND_MISMATCH')
            break
        if (
            point_source_normalization is not None
            and not point_source_normalization.valid_frequency_domain.contains(frequency)
        ):
            reasons.append('SOURCE_NORMALIZATION_VALID_BAND_MISMATCH')
            break

    if source_authority.capability == 'UNSUPPORTED_UNKNOWN_DIRECTIVITY':
        reasons.append(source_authority.unsupported_reason or 'SOURCE_DIRECTIVITY_UNSUPPORTED')
    if source_authority.capability in {
        'COMPLEX_DIRECTIONAL_TRANSFER_AVAILABLE',
        'ANALYTIC_OMNIDIRECTIONAL_MODEL',
    } and source_authority.phase_reference != 'source_volume_velocity_t0':
        reasons.append('SOURCE_PHASE_REFERENCE_MISMATCH')
    if point_source_normalization is None:
        reasons.append('SOURCE_ABSOLUTE_NORMALIZATION_UNSUPPORTED')
    elif source_authority.point_source_normalization_ref != point_source_normalization.as_external_ref():
        reasons.append('SOURCE_NORMALIZATION_AUTHORITY_MISMATCH')

    source_mode: Literal['complex', 'magnitude', 'unsupported'] = 'complex'
    if source_authority.capability == 'MAGNITUDE_ONLY_DIRECTIVITY':
        if directivity_dataset is None:
            reasons.append('DIRECTIVITY_DATASET_AUTHORITY_MISMATCH')
            source_mode = 'unsupported'
        elif directivity_dataset.normalization.reference != 'on_axis_per_frequency':
            reasons.append('DIRECTIVITY_NORMALIZATION_NOT_POINT_SOURCE_RATIO')
            source_mode = 'unsupported'
        else:
            source_mode = 'magnitude'
    elif source_authority.capability == 'COMPLEX_DIRECTIONAL_TRANSFER_AVAILABLE':
        if (
            directivity_dataset is None
            or source_authority.directivity_dataset_ref is None
            or source_authority.directivity_dataset_ref
            != _identity_ref(
                authority_id=directivity_dataset.dataset_id,
                authority_version=directivity_dataset.version,
                semantic_hash_sha256=directivity_dataset.semantic_sha256,
            )
        ):
            reasons.append('DIRECTIVITY_DATASET_AUTHORITY_MISMATCH')
            source_mode = 'unsupported'
        elif directivity_dataset.normalization.reference != 'on_axis_per_frequency':
            reasons.append('DIRECTIVITY_NORMALIZATION_NOT_POINT_SOURCE_RATIO')
            source_mode = 'unsupported'
    elif source_authority.capability == 'UNSUPPORTED_UNKNOWN_DIRECTIVITY':
        source_mode = 'unsupported'

    reflection_incidences = _path_reflection_incidences(path, execution_input)

    reflection_authorities: list[SurfaceReflectionTransferAuthority] = []
    magnitude_only = source_mode == 'magnitude'
    for interaction_index, surface_id in enumerate(
        path.ordered_interaction_surface_ids
    ):
        authority = surface_reflections.get(surface_id)
        evaluated_incidence = (
            reflection_incidences[interaction_index]
            if reflection_incidences is not None
            and interaction_index < len(reflection_incidences)
            else None
        )
        if authority is None:
            reasons.append(f'MISSING_REFLECTION_AUTHORITY:{surface_id}')
            continue
        reflection_authorities.append(authority)
        if authority.source_surface_id != surface_id:
            reasons.append(
                f'REFLECTION_SURFACE_IDENTITY_MISMATCH:{surface_id}'
            )
        refs.extend(
            [
                authority.as_external_ref(),
                authority.material_authority_ref,
                authority.r120_geometry_ref,
            ]
        )
        if authority.boundary_physics_authority_ref is not None:
            refs.append(authority.boundary_physics_authority_ref)
        if authority.r120_geometry_ref != r120_geometry_ref:
            reasons.append(f'STALE_REFLECTION_SURFACE_AUTHORITY:{surface_id}')
        if authority.derivation == 'specific_impedance_local_reaction':
            # A local-reaction impedance authority is evaluated for exactly one
            # incidence cosine; it may never serve a different reflection angle
            # silently.
            if evaluated_incidence is None:
                reasons.append(
                    f'REFLECTION_INCIDENCE_UNDERIVABLE:{surface_id}'
                )
            elif authority.incidence_cosine is None or abs(
                evaluated_incidence[1] - float(authority.incidence_cosine)
            ) > _INCIDENCE_COSINE_MATCH_TOLERANCE:
                reasons.append(
                    f'REFLECTION_INCIDENCE_COSINE_MISMATCH:{surface_id}'
                )
        # The path-persisted contribution incidence (when present) must agree
        # with the incidence re-evaluated from exact path geometry.
        contribution_cosines = {
            float(item.boundary_material.incidence_cosine)
            for item in path.bands
            if item.boundary_material is not None
            and item.boundary_material.incidence_cosine is not None
        } | {
            float(entry.incidence_cosine)
            for item in path.bands
            for entry in (item.boundary_materials or ())
            if entry.incidence_cosine is not None
            and entry.source_surface_id == surface_id
        }
        if contribution_cosines and evaluated_incidence is not None:
            if any(
                abs(value - evaluated_incidence[1])
                > _INCIDENCE_COSINE_MATCH_TOLERANCE
                for value in contribution_cosines
            ):
                reasons.append(
                    f'REFLECTION_INCIDENCE_PATH_MISMATCH:{surface_id}'
                )
        if authority.capability == 'MAGNITUDE_ONLY':
            magnitude_only = True

    portal_interactions = tuple(
        item
        for item in (path.ordered_interactions or ())
        if item.kind == 'portal_crossing'
    )
    portal_authorities: list[PortalAcousticTransferAuthority] = []
    if portal_interactions:
        if portal_geometry_authority_ref is None:
            reasons.append('MISSING_PORTAL_GEOMETRY_AUTHORITY')
        else:
            refs.append(portal_geometry_authority_ref)
            if execution_input.portal_authority_ref != portal_geometry_authority_ref:
                reasons.append('EXECUTION_INPUT_PORTAL_AUTHORITY_MISMATCH')
        for interaction in portal_interactions:
            assert interaction.portal_id is not None
            assert interaction.from_region_id is not None
            assert interaction.to_region_id is not None
            key = (
                interaction.portal_id,
                interaction.from_region_id,
                interaction.to_region_id,
            )
            authority = portal_transfers.get(key)
            if authority is None:
                reasons.append(f'MISSING_PORTAL_TRANSFER_AUTHORITY:{interaction.portal_id}')
                continue
            portal_authorities.append(authority)
            refs.extend(
                [
                    authority.as_external_ref(),
                    authority.portal_geometry_authority_ref,
                ]
            )
            if (
                portal_geometry_authority_ref is None
                or authority.portal_geometry_authority_ref
                != portal_geometry_authority_ref
            ):
                reasons.append(f'STALE_PORTAL_TRANSFER_AUTHORITY:{interaction.portal_id}')

    if reasons:
        return _response_artifact(
            path_artifact=path_artifact,
            path=path,
            environment=environment,
            frequency_grid=frequency_grid,
            dependency_refs=refs,
            ordered_portal_ids=[
                item.portal_id for item in portal_interactions if item.portal_id is not None
            ],
            ordered_reflection_incidences=reflection_incidences,
            capability='UNSUPPORTED',
            unsupported_reasons=reasons,
        )

    assert point_source_normalization is not None
    response_samples: list[PathFrequencyResponseSample] = []
    distance = path.geometric_path_length_m
    spread = 1.0 / (4.0 * pi * distance)

    for frequency in frequency_grid.frequencies_hz:
        omega = 2.0 * pi * frequency
        k = omega / environment.sound_speed_m_s
        propagation_phase = -k * distance
        propagation = complex(cos(propagation_phase), sin(propagation_phase))
        monopole = 1j * omega * environment.density_kg_m3 * spread * propagation

        source_complex = 1.0 + 0.0j
        source_magnitude = 1.0
        if source_authority.capability in {
            'COMPLEX_DIRECTIONAL_TRANSFER_AVAILABLE',
            'MAGNITUDE_ONLY_DIRECTIVITY',
        }:
            band = _path_band(path, frequency)
            if band is None:
                return _response_artifact(
                    path_artifact=path_artifact,
                    path=path,
                    environment=environment,
                    frequency_grid=frequency_grid,
                    dependency_refs=refs,
                    ordered_portal_ids=[
                        item.portal_id for item in portal_interactions
                        if item.portal_id is not None
                    ],
                    capability='UNSUPPORTED',
                    unsupported_reasons=('PATH_DIRECTIVITY_FREQUENCY_GRID_MISMATCH',),
                )
            contribution = band.source_directivity
            if source_authority.directivity_dataset_ref is None or (
                contribution.dataset_id != source_authority.directivity_dataset_ref.authority_id
                or contribution.dataset_version
                != source_authority.directivity_dataset_ref.authority_version
                or contribution.dataset_semantic_sha256
                != source_authority.directivity_dataset_ref.semantic_hash_sha256
            ):
                return _response_artifact(
                    path_artifact=path_artifact,
                    path=path,
                    environment=environment,
                    frequency_grid=frequency_grid,
                    dependency_refs=refs,
                    ordered_portal_ids=[
                        item.portal_id for item in portal_interactions
                        if item.portal_id is not None
                    ],
                    capability='UNSUPPORTED',
                    unsupported_reasons=('PATH_DIRECTIVITY_AUTHORITY_MISMATCH',),
                )
            if source_authority.capability == 'MAGNITUDE_ONLY_DIRECTIVITY':
                source_magnitude = contribution.magnitude_linear
            else:
                assert directivity_dataset is not None
                evaluated = evaluate_directivity(
                    directivity_dataset,
                    frequency_hz=frequency,
                    horizontal_angle_deg=contribution.horizontal_angle_deg,
                    vertical_angle_deg=contribution.vertical_angle_deg,
                    request='complex',
                )
                if evaluated.decision != 'SUPPORTED':
                    return _response_artifact(
                        path_artifact=path_artifact,
                        path=path,
                        environment=environment,
                        frequency_grid=frequency_grid,
                        dependency_refs=refs,
                        ordered_portal_ids=[
                            item.portal_id for item in portal_interactions
                            if item.portal_id is not None
                        ],
                        capability='UNSUPPORTED',
                        unsupported_reasons=(
                            'COMPLEX_SOURCE_DIRECTIVITY_EVALUATION_UNSUPPORTED',
                        ),
                    )
                assert evaluated.complex_real is not None
                assert evaluated.complex_imag is not None
                source_complex = complex(evaluated.complex_real, evaluated.complex_imag)
                source_magnitude = abs(source_complex)

        reflection_complex = 1.0 + 0.0j
        reflection_magnitude = 1.0
        for authority in reflection_authorities:
            if not authority.valid_frequency_domain.contains(frequency):
                return _response_artifact(
                    path_artifact=path_artifact,
                    path=path,
                    environment=environment,
                    frequency_grid=frequency_grid,
                    dependency_refs=refs,
                    ordered_portal_ids=[
                        item.portal_id for item in portal_interactions
                        if item.portal_id is not None
                    ],
                    capability='UNSUPPORTED',
                    unsupported_reasons=(
                        f'REFLECTION_VALID_BAND_MISMATCH:{authority.source_surface_id}',
                    ),
                )
            sample = _sample_at(authority.samples, frequency)
            if sample is None:
                return _response_artifact(
                    path_artifact=path_artifact,
                    path=path,
                    environment=environment,
                    frequency_grid=frequency_grid,
                    dependency_refs=refs,
                    ordered_portal_ids=[
                        item.portal_id for item in portal_interactions
                        if item.portal_id is not None
                    ],
                    capability='UNSUPPORTED',
                    unsupported_reasons=(
                        f'REFLECTION_FREQUENCY_GRID_MISMATCH:{authority.source_surface_id}',
                    ),
                )
            reflection_magnitude *= sample.magnitude
            if authority.capability == 'COMPLEX':
                value = sample.complex_value()
                assert value is not None
                reflection_complex *= value

        portal_complex = 1.0 + 0.0j
        portal_magnitude = 1.0
        for authority in portal_authorities:
            if not authority.valid_frequency_domain.contains(frequency):
                return _response_artifact(
                    path_artifact=path_artifact,
                    path=path,
                    environment=environment,
                    frequency_grid=frequency_grid,
                    dependency_refs=refs,
                    ordered_portal_ids=[
                        item.portal_id for item in portal_interactions
                        if item.portal_id is not None
                    ],
                    capability='UNSUPPORTED',
                    unsupported_reasons=(
                        f'PORTAL_TRANSFER_VALID_BAND_MISMATCH:{authority.portal_id}',
                    ),
                )
            sample = _sample_at(authority.samples, frequency)
            if sample is None:
                return _response_artifact(
                    path_artifact=path_artifact,
                    path=path,
                    environment=environment,
                    frequency_grid=frequency_grid,
                    dependency_refs=refs,
                    ordered_portal_ids=[
                        item.portal_id for item in portal_interactions
                        if item.portal_id is not None
                    ],
                    capability='UNSUPPORTED',
                    unsupported_reasons=(
                        f'PORTAL_TRANSFER_FREQUENCY_GRID_MISMATCH:{authority.portal_id}',
                    ),
                )
            value = sample.complex_value()
            assert value is not None
            portal_complex *= value
            portal_magnitude *= sample.magnitude

        if magnitude_only:
            magnitude = abs(monopole) * source_magnitude * reflection_magnitude * portal_magnitude
            response_samples.append(
                PathFrequencyResponseSample(
                    frequency_hz=frequency,
                    propagation_phase_rad=propagation_phase,
                    geometric_spreading_per_m=spread,
                    magnitude_pa_per_m3_s=magnitude,
                )
            )
        else:
            value = monopole * source_complex * reflection_complex * portal_complex
            response_samples.append(
                PathFrequencyResponseSample(
                    frequency_hz=frequency,
                    propagation_phase_rad=propagation_phase,
                    geometric_spreading_per_m=spread,
                    magnitude_pa_per_m3_s=abs(value),
                    phase_rad=_phase(value),
                    complex_real_pa_per_m3_s=value.real,
                    complex_imag_pa_per_m3_s=value.imag,
                )
            )

    return _response_artifact(
        path_artifact=path_artifact,
        path=path,
        environment=environment,
        frequency_grid=frequency_grid,
        dependency_refs=refs,
        ordered_portal_ids=[
            item.portal_id for item in portal_interactions if item.portal_id is not None
        ],
        ordered_reflection_incidences=reflection_incidences,
        capability='MAGNITUDE_ONLY' if magnitude_only else 'COMPLEX_SUPPORTED',
        samples=response_samples,
    )


ResponseDependency = (
    AcousticEnvironmentAuthority
    | FrequencyGridAuthority
    | PathResponseConfiguration
    | PointSourceNormalizationAuthority
    | SourceResponseAuthority
    | ReceiverResponseAuthority
    | SurfaceReflectionTransferAuthority
    | PortalAcousticTransferAuthority
    | DirectivityDataset
    | DeterministicGaExecutionInput
    | ExactExternalAuthorityRef
)
PathArtifactResolver = Callable[[str], DeterministicPathArtifact | None]
ResponseDependencyResolver = Callable[[ExactExternalAuthorityRef], ResponseDependency | None]


def _dependency_ref(value: ResponseDependency) -> ExactExternalAuthorityRef:
    if isinstance(value, ExactExternalAuthorityRef):
        return value
    if isinstance(value, DirectivityDataset):
        return _identity_ref(
            authority_id=value.dataset_id,
            authority_version=value.version,
            semantic_hash_sha256=value.semantic_sha256,
        )
    if isinstance(value, DeterministicGaExecutionInput):
        return _execution_input_ref(value)
    return value.as_external_ref()


class CadPathFrequencyResponseRepository:
    """Append-only per-path response persistence with exact dependency re-resolution."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        path_artifact_resolver: PathArtifactResolver,
        dependency_resolver: ResponseDependencyResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        self.path_artifact_resolver = path_artifact_resolver
        self.dependency_resolver = dependency_resolver
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'r150_path_frequency_response_artifacts')

    def _resolve_dependency(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> ResponseDependency:
        value = self.dependency_resolver(ref)
        if value is None:
            raise ValueError(
                f'path response dependency is missing/stale: {ref.authority_id}'
            )
        if _dependency_ref(value) != ref:
            raise ValueError(
                f'path response dependency exact identity mismatch: {ref.authority_id}'
            )
        return value

    def _rebuild(
        self,
        artifact: DeterministicPathFrequencyResponseArtifact,
    ) -> DeterministicPathFrequencyResponseArtifact:
        path_artifact = self.path_artifact_resolver(
            artifact.deterministic_path_artifact_id
        )
        if path_artifact is None:
            raise ValueError('deterministic path artifact is missing/stale')
        if path_artifact.semantic_sha256 != artifact.deterministic_path_artifact_sha256:
            raise ValueError('deterministic path artifact exact identity mismatch')
        path = next(
            (
                item
                for item in path_artifact.paths
                if item.path_id == artifact.deterministic_path_id
            ),
            None,
        )
        if path is None or path.semantic_sha256 != artifact.deterministic_path_sha256:
            raise ValueError('deterministic path exact identity mismatch')

        resolved = {
            _ref_payload(ref): self._resolve_dependency(ref)
            for ref in artifact.dependency_refs
        }

        def typed(cls):
            matches = [value for value in resolved.values() if isinstance(value, cls)]
            if len(matches) != 1:
                raise ValueError(
                    f'path response requires exactly one resolved {cls.__name__} authority'
                )
            return matches[0]

        execution_input = typed(DeterministicGaExecutionInput)
        source = typed(SourceResponseAuthority)
        receiver = typed(ReceiverResponseAuthority)
        environment = typed(AcousticEnvironmentAuthority)
        grid = typed(FrequencyGridAuthority)
        configuration = typed(PathResponseConfiguration)

        normalizations = [
            value
            for value in resolved.values()
            if isinstance(value, PointSourceNormalizationAuthority)
        ]
        normalization = normalizations[0] if len(normalizations) == 1 else None
        if len(normalizations) > 1:
            raise ValueError('multiple point-source normalization authorities resolved')

        datasets = [
            value for value in resolved.values() if isinstance(value, DirectivityDataset)
        ]
        dataset = datasets[0] if len(datasets) == 1 else None
        if len(datasets) > 1:
            raise ValueError('multiple directivity datasets resolved for one source')

        reflections = {
            value.source_surface_id: value
            for value in resolved.values()
            if isinstance(value, SurfaceReflectionTransferAuthority)
        }
        portals = {
            (value.portal_id, value.from_region_id, value.to_region_id): value
            for value in resolved.values()
            if isinstance(value, PortalAcousticTransferAuthority)
        }

        r120_ref = next(
            (
                ref
                for ref in artifact.dependency_refs
                if ref.authority_id == path_artifact.r120_compiled_geometry_id
                and ref.semantic_hash_sha256
                == path_artifact.r120_compiled_geometry_sha256
            ),
            None,
        )
        if r120_ref is None:
            raise ValueError('R120 compiled geometry dependency is missing/stale')

        portal_interactions = tuple(
            item
            for item in (path.ordered_interactions or ())
            if item.kind == 'portal_crossing'
        )
        portal_ref: ExactExternalAuthorityRef | None = None
        if portal_interactions:
            portal_transfer_values = tuple(portals.values())
            if portal_transfer_values:
                candidate = portal_transfer_values[0].portal_geometry_authority_ref
                if any(
                    item.portal_geometry_authority_ref != candidate
                    for item in portal_transfer_values
                ):
                    raise ValueError('Portal transfer authorities bind different Portal geometry')
                portal_ref = candidate
            else:
                portal_refs = [
                    ref
                    for ref in artifact.dependency_refs
                    if ref.authority_id.startswith('r120-portals:')
                ]
                portal_ref = portal_refs[0] if len(portal_refs) == 1 else None

        rebuilt = build_deterministic_path_frequency_response(
            path_artifact=path_artifact,
            execution_input=execution_input,
            path_id=artifact.deterministic_path_id,
            r120_geometry_ref=r120_ref,
            source_authority=source,
            point_source_normalization=normalization,
            receiver_authority=receiver,
            environment=environment,
            frequency_grid=grid,
            configuration=configuration,
            surface_reflections=reflections,
            portal_geometry_authority_ref=portal_ref,
            portal_transfers=portals,
            directivity_dataset=dataset,
        )
        if rebuilt != artifact:
            raise ValueError(
                'persisted path response no longer reproduces from current exact authorities'
            )
        return rebuilt

    def save(self, artifact: DeterministicPathFrequencyResponseArtifact) -> None:
        self._rebuild(artifact)
        payload = _canonical_json(artifact.model_dump(mode='json'))
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT semantic_sha256, payload_json '
                'FROM r150_path_frequency_response_artifacts WHERE artifact_id = ?',
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing['semantic_sha256'] != artifact.semantic_sha256
                    or existing['payload_json'] != payload
                ):
                    raise ValueError('immutable path response artifact identity collision')
                return
            connection.execute(
                'INSERT INTO r150_path_frequency_response_artifacts '
                '(artifact_id, semantic_sha256, payload_json) VALUES (?, ?, ?)',
                (artifact.artifact_id, artifact.semantic_sha256, payload),
            )

    def get(
        self,
        artifact_id: str,
    ) -> DeterministicPathFrequencyResponseArtifact | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM r150_path_frequency_response_artifacts '
                'WHERE artifact_id = ?',
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        artifact = DeterministicPathFrequencyResponseArtifact.model_validate_json(
            row['payload_json']
        )
        return self._rebuild(artifact)
