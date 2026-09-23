from __future__ import annotations

from cmath import exp
from hashlib import sha256
import json
from math import isfinite, pi, sqrt
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_scene import Direction3, Position3


SPATIAL_FIELD_DECOMPOSITION_SCHEMA_VERSION = 1
SPATIAL_FIELD_DECOMPOSITION_AUTHORITY_VERSION = 'r100-spatial-decomposition-1'
SPATIAL_FIELD_DECOMPOSITION_METHOD_ID = (
    'htdt.planar-normal-incidence-two-point-complex-pressure'
)
SPATIAL_FIELD_DECOMPOSITION_METHOD_VERSION = '1'
SPATIAL_FIELD_DECOMPOSITION_CONDITIONING_POLICY_ID = (
    'htdt.planar-normal-incidence-two-point-conditioning'
)
SPATIAL_FIELD_DECOMPOSITION_CONDITIONING_POLICY_VERSION = '1'

CanonicalPhasorConvention = Literal['exp(-i*omega*t)']
PressureEvidencePhasorConvention = Literal[
    'exp(-i*omega*t)',
    'exp(+i*omega*t)',
]
PressureAnalysisKernel = Literal[
    'exp(+i*omega*t)',
    'exp(-i*omega*t)',
]
DecompositionStatus = Literal['AVAILABLE', 'BLOCKED', 'UNSUPPORTED']
ConditioningState = Literal[
    'WELL_CONDITIONED',
    'SINGULAR',
    'ILL_CONDITIONED',
    'INCIDENT_UNDEFINED',
    'UNSUPPORTED_CONVENTION',
]


def canonical_spatial_decomposition_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def canonical_spatial_decomposition_sha256(value: Any) -> str:
    return sha256(
        canonical_spatial_decomposition_json(value).encode('utf-8')
    ).hexdigest()


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}:{digest}'


def _position_array(position: Position3) -> tuple[float, float, float]:
    return (float(position.x_m), float(position.y_m), float(position.z_m))


def _direction_array(direction: Direction3) -> tuple[float, float, float]:
    return (float(direction.x), float(direction.y), float(direction.z))


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(float(a) * float(b) for a, b in zip(left, right, strict=True))


def _sub(left: Sequence[float], right: Sequence[float]) -> tuple[float, float, float]:
    return tuple(
        float(a) - float(b)
        for a, b in zip(left, right, strict=True)
    )  # type: ignore[return-value]


def _scale(value: Sequence[float], factor: float) -> tuple[float, float, float]:
    return tuple(float(item) * float(factor) for item in value)  # type: ignore[return-value]


def _norm(value: Sequence[float]) -> float:
    return sqrt(sum(float(item) * float(item) for item in value))


class ExactDecompositionAuthorityRef(BaseModel):
    """Exact opaque authority reference used by the solver-neutral contract."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_kind: str = Field(min_length=1)
    authority_id: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class ComplexPressureValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    real: float
    imag: float

    @model_validator(mode='after')
    def finite_components(self) -> 'ComplexPressureValue':
        if not isfinite(float(self.real)) or not isfinite(float(self.imag)):
            raise ValueError('complex pressure components must be finite')
        return self

    def as_complex(self) -> complex:
        return complex(float(self.real), float(self.imag))

    @classmethod
    def from_complex(cls, value: complex) -> 'ComplexPressureValue':
        if not isfinite(float(value.real)) or not isfinite(float(value.imag)):
            raise ValueError('complex pressure value must be finite')
        return cls(real=float(value.real), imag=float(value.imag))


class SpatialPressureSamplePoint(BaseModel):
    """One pressure sample on the canonical boundary-normal line."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    sample_id: str = Field(min_length=1)
    position: Position3
    inward_distance_m: float = Field(ge=0.0)

    @model_validator(mode='after')
    def finite_distance(self) -> 'SpatialPressureSamplePoint':
        if not isfinite(float(self.inward_distance_m)):
            raise ValueError('sample inward distance must be finite')
        return self


class SpatialDecompositionValidDomain(BaseModel):
    """Deliberately narrow first authority; everything else is unsupported."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    boundary_geometry: Literal['planar'] = 'planar'
    local_field_model: Literal['one_dimensional'] = 'one_dimensional'
    incidence: Literal['normal'] = 'normal'
    sample_count: Literal[2] = 2
    sample_alignment: Literal['same_boundary_normal_line'] = (
        'same_boundary_normal_line'
    )
    pressure_quantity: Literal['complex_pressure'] = 'complex_pressure'


class SpatialDecompositionConditioningPolicy(BaseModel):
    """Versioned fail-closed numerical threshold authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    policy_id: Literal[
        'htdt.planar-normal-incidence-two-point-conditioning'
    ] = SPATIAL_FIELD_DECOMPOSITION_CONDITIONING_POLICY_ID
    policy_version: Literal['1'] = (
        SPATIAL_FIELD_DECOMPOSITION_CONDITIONING_POLICY_VERSION
    )
    minimum_abs_determinant: float = Field(gt=0.0)
    maximum_condition_number_2: float = Field(gt=1.0)
    incident_magnitude_floor: float = Field(gt=0.0)
    sample_geometry_tolerance_m: float = Field(gt=0.0)

    @model_validator(mode='after')
    def finite_thresholds(self) -> 'SpatialDecompositionConditioningPolicy':
        values = (
            self.minimum_abs_determinant,
            self.maximum_condition_number_2,
            self.incident_magnitude_floor,
            self.sample_geometry_tolerance_m,
        )
        if any(not isfinite(float(value)) for value in values):
            raise ValueError('conditioning thresholds must be finite')
        return self


def default_spatial_decomposition_conditioning_policy(
) -> SpatialDecompositionConditioningPolicy:
    """Return the v1 explicit threshold authority.

    Matrix entries are unit-magnitude phasors, so the determinant threshold is
    dimensionless. The pressure floor is expressed in the exact normalization
    named by SpatialFieldDecompositionSpec.
    """

    return SpatialDecompositionConditioningPolicy(
        minimum_abs_determinant=1.0e-8,
        maximum_condition_number_2=1.0e8,
        incident_magnitude_floor=1.0e-12,
        sample_geometry_tolerance_m=1.0e-9,
    )


class SpatialFieldDecompositionSpec(BaseModel):
    """Immutable solver-neutral planar/normal-incidence decomposition authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SPATIAL_FIELD_DECOMPOSITION_SCHEMA_VERSION
    authority_version: Literal[
        'r100-spatial-decomposition-1'
    ] = SPATIAL_FIELD_DECOMPOSITION_AUTHORITY_VERSION
    method_id: Literal[
        'htdt.planar-normal-incidence-two-point-complex-pressure'
    ] = SPATIAL_FIELD_DECOMPOSITION_METHOD_ID
    method_version: Literal['1'] = SPATIAL_FIELD_DECOMPOSITION_METHOD_VERSION

    spec_id: str = Field(pattern=r'^spatial-field-decomposition:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    valid_domain: SpatialDecompositionValidDomain = SpatialDecompositionValidDomain()

    fixture_authority_ref: ExactDecompositionAuthorityRef | None = None
    prediction_authority_ref: ExactDecompositionAuthorityRef | None = None
    raw_complex_pressure_evidence_ref: ExactDecompositionAuthorityRef
    timing_fourier_authority_ref: ExactDecompositionAuthorityRef
    normalization_authority_ref: ExactDecompositionAuthorityRef
    sound_speed_authority_ref: ExactDecompositionAuthorityRef
    pressure_evidence_source: Literal[
        'analytic_synthetic',
        'solver_derived',
        'measured_or_external',
    ]
    solver_backend_provenance_ref: ExactDecompositionAuthorityRef | None = None

    boundary_plane_point: Position3
    surface_outward_normal: Direction3
    canonical_inward_coordinate_semantics: Literal[
        'd>=0_into_acoustic_domain_and_inward=-surface_outward_normal'
    ] = 'd>=0_into_acoustic_domain_and_inward=-surface_outward_normal'
    sample_points: tuple[SpatialPressureSamplePoint, SpatialPressureSamplePoint]

    frequency_grid_hz: tuple[float, ...] = Field(min_length=1)
    sound_speed_m_s: float = Field(gt=0.0)

    canonical_phasor_convention: CanonicalPhasorConvention = 'exp(-i*omega*t)'
    pressure_evidence_phasor_convention: PressureEvidencePhasorConvention
    pressure_analysis_kernel: PressureAnalysisKernel
    complex_pressure_normalization: str = Field(min_length=1)

    conditioning_policy: SpatialDecompositionConditioningPolicy

    @model_validator(mode='after')
    def validate_contract(self) -> 'SpatialFieldDecompositionSpec':
        if self.fixture_authority_ref is None and self.prediction_authority_ref is None:
            raise ValueError(
                'decomposition spec requires exact fixture or prediction authority'
            )
        if (
            self.pressure_evidence_source == 'solver_derived'
            and self.solver_backend_provenance_ref is None
        ):
            raise ValueError(
                'solver-derived pressure evidence requires exact solver/backend provenance'
            )
        if not isfinite(float(self.sound_speed_m_s)):
            raise ValueError('sound speed must be finite')
        frequencies = [float(item) for item in self.frequency_grid_hz]
        if any(not isfinite(item) or item <= 0.0 for item in frequencies):
            raise ValueError('frequency grid must contain finite positive values')
        if frequencies != sorted(frequencies) or len(frequencies) != len(set(frequencies)):
            raise ValueError('frequency grid must be unique and sorted')

        first, second = self.sample_points
        if first.sample_id == second.sample_id:
            raise ValueError('two-point decomposition requires distinct sample ids')
        if float(first.inward_distance_m) == float(second.inward_distance_m):
            raise ValueError('two-point decomposition requires distinct inward distances')

        outward = _direction_array(self.surface_outward_normal)
        inward = _scale(outward, -1.0)
        plane = _position_array(self.boundary_plane_point)
        tolerance = float(self.conditioning_policy.sample_geometry_tolerance_m)
        for sample in self.sample_points:
            displacement = _sub(_position_array(sample.position), plane)
            projected_distance = _dot(displacement, inward)
            lateral = _sub(displacement, _scale(inward, projected_distance))
            if projected_distance < -tolerance:
                raise ValueError(
                    f'UNSUPPORTED: sample {sample.sample_id} lies outside canonical acoustic domain'
                )
            if abs(projected_distance - float(sample.inward_distance_m)) > tolerance:
                raise ValueError(
                    f'UNSUPPORTED: sample {sample.sample_id} inward distance does not match '
                    'boundary plane/normal authority'
                )
            if _norm(lateral) > tolerance:
                raise ValueError(
                    f'UNSUPPORTED: sample {sample.sample_id} is not on the canonical boundary-normal line'
                )

        digest = canonical_spatial_decomposition_sha256(self.semantic_payload())
        if self.semantic_sha256 != digest:
            raise ValueError('SpatialFieldDecompositionSpec semantic hash mismatch')
        if self.spec_id != _semantic_id('spatial-field-decomposition', digest):
            raise ValueError('SpatialFieldDecompositionSpec deterministic id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'method_id': self.method_id,
            'method_version': self.method_version,
            'valid_domain': self.valid_domain.model_dump(mode='json'),
            'fixture_authority_ref': (
                self.fixture_authority_ref.model_dump(mode='json')
                if self.fixture_authority_ref is not None
                else None
            ),
            'prediction_authority_ref': (
                self.prediction_authority_ref.model_dump(mode='json')
                if self.prediction_authority_ref is not None
                else None
            ),
            'raw_complex_pressure_evidence_ref': (
                self.raw_complex_pressure_evidence_ref.model_dump(mode='json')
            ),
            'timing_fourier_authority_ref': (
                self.timing_fourier_authority_ref.model_dump(mode='json')
            ),
            'normalization_authority_ref': (
                self.normalization_authority_ref.model_dump(mode='json')
            ),
            'sound_speed_authority_ref': (
                self.sound_speed_authority_ref.model_dump(mode='json')
            ),
            'pressure_evidence_source': self.pressure_evidence_source,
            'solver_backend_provenance_ref': (
                self.solver_backend_provenance_ref.model_dump(mode='json')
                if self.solver_backend_provenance_ref is not None
                else None
            ),
            'boundary_plane_point': self.boundary_plane_point.model_dump(mode='json'),
            'surface_outward_normal': self.surface_outward_normal.model_dump(mode='json'),
            'canonical_inward_coordinate_semantics': (
                self.canonical_inward_coordinate_semantics
            ),
            'sample_points': [
                item.model_dump(mode='json') for item in self.sample_points
            ],
            'frequency_grid_hz': list(self.frequency_grid_hz),
            'sound_speed_m_s': self.sound_speed_m_s,
            'canonical_phasor_convention': self.canonical_phasor_convention,
            'pressure_evidence_phasor_convention': (
                self.pressure_evidence_phasor_convention
            ),
            'pressure_analysis_kernel': self.pressure_analysis_kernel,
            'complex_pressure_normalization': self.complex_pressure_normalization,
            'conditioning_policy': self.conditioning_policy.model_dump(mode='json'),
        }


def build_spatial_field_decomposition_spec(
    *,
    fixture_authority_ref: ExactDecompositionAuthorityRef | None,
    prediction_authority_ref: ExactDecompositionAuthorityRef | None,
    raw_complex_pressure_evidence_ref: ExactDecompositionAuthorityRef,
    timing_fourier_authority_ref: ExactDecompositionAuthorityRef,
    normalization_authority_ref: ExactDecompositionAuthorityRef,
    sound_speed_authority_ref: ExactDecompositionAuthorityRef,
    pressure_evidence_source: Literal[
        'analytic_synthetic',
        'solver_derived',
        'measured_or_external',
    ],
    solver_backend_provenance_ref: ExactDecompositionAuthorityRef | None,
    boundary_plane_point: Position3,
    surface_outward_normal: Direction3,
    sample_points: Sequence[SpatialPressureSamplePoint],
    frequency_grid_hz: Sequence[float],
    sound_speed_m_s: float,
    pressure_evidence_phasor_convention: PressureEvidencePhasorConvention,
    pressure_analysis_kernel: PressureAnalysisKernel,
    complex_pressure_normalization: str,
    conditioning_policy: SpatialDecompositionConditioningPolicy | None = None,
) -> SpatialFieldDecompositionSpec:
    points = tuple(sample_points)
    if len(points) != 2:
        raise ValueError('two-point decomposition requires exactly two sample points')
    policy = conditioning_policy or default_spatial_decomposition_conditioning_policy()
    payload = {
        'schema_version': SPATIAL_FIELD_DECOMPOSITION_SCHEMA_VERSION,
        'authority_version': SPATIAL_FIELD_DECOMPOSITION_AUTHORITY_VERSION,
        'method_id': SPATIAL_FIELD_DECOMPOSITION_METHOD_ID,
        'method_version': SPATIAL_FIELD_DECOMPOSITION_METHOD_VERSION,
        'valid_domain': SpatialDecompositionValidDomain().model_dump(mode='json'),
        'fixture_authority_ref': (
            fixture_authority_ref.model_dump(mode='json')
            if fixture_authority_ref is not None
            else None
        ),
        'prediction_authority_ref': (
            prediction_authority_ref.model_dump(mode='json')
            if prediction_authority_ref is not None
            else None
        ),
        'raw_complex_pressure_evidence_ref': (
            raw_complex_pressure_evidence_ref.model_dump(mode='json')
        ),
        'timing_fourier_authority_ref': (
            timing_fourier_authority_ref.model_dump(mode='json')
        ),
        'normalization_authority_ref': (
            normalization_authority_ref.model_dump(mode='json')
        ),
        'sound_speed_authority_ref': (
            sound_speed_authority_ref.model_dump(mode='json')
        ),
        'pressure_evidence_source': pressure_evidence_source,
        'solver_backend_provenance_ref': (
            solver_backend_provenance_ref.model_dump(mode='json')
            if solver_backend_provenance_ref is not None
            else None
        ),
        'boundary_plane_point': boundary_plane_point.model_dump(mode='json'),
        'surface_outward_normal': surface_outward_normal.model_dump(mode='json'),
        'canonical_inward_coordinate_semantics': (
            'd>=0_into_acoustic_domain_and_inward=-surface_outward_normal'
        ),
        'sample_points': [item.model_dump(mode='json') for item in points],
        'frequency_grid_hz': [float(item) for item in frequency_grid_hz],
        'sound_speed_m_s': float(sound_speed_m_s),
        'canonical_phasor_convention': 'exp(-i*omega*t)',
        'pressure_evidence_phasor_convention': pressure_evidence_phasor_convention,
        'pressure_analysis_kernel': pressure_analysis_kernel,
        'complex_pressure_normalization': complex_pressure_normalization,
        'conditioning_policy': policy.model_dump(mode='json'),
    }
    digest = canonical_spatial_decomposition_sha256(payload)
    return SpatialFieldDecompositionSpec(
        spec_id=_semantic_id('spatial-field-decomposition', digest),
        semantic_sha256=digest,
        fixture_authority_ref=fixture_authority_ref,
        prediction_authority_ref=prediction_authority_ref,
        raw_complex_pressure_evidence_ref=raw_complex_pressure_evidence_ref,
        timing_fourier_authority_ref=timing_fourier_authority_ref,
        normalization_authority_ref=normalization_authority_ref,
        sound_speed_authority_ref=sound_speed_authority_ref,
        pressure_evidence_source=pressure_evidence_source,
        solver_backend_provenance_ref=solver_backend_provenance_ref,
        boundary_plane_point=boundary_plane_point,
        surface_outward_normal=surface_outward_normal,
        sample_points=points,  # type: ignore[arg-type]
        frequency_grid_hz=tuple(float(item) for item in frequency_grid_hz),
        sound_speed_m_s=float(sound_speed_m_s),
        pressure_evidence_phasor_convention=pressure_evidence_phasor_convention,
        pressure_analysis_kernel=pressure_analysis_kernel,
        complex_pressure_normalization=complex_pressure_normalization,
        conditioning_policy=policy,
    )


class TwoPointFrequencyPressure(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    sample_1_pressure: ComplexPressureValue
    sample_2_pressure: ComplexPressureValue

    @model_validator(mode='after')
    def finite_frequency(self) -> 'TwoPointFrequencyPressure':
        if not isfinite(float(self.frequency_hz)):
            raise ValueError('pressure evidence frequency must be finite')
        return self


class TwoPointComplexPressureEvidence(BaseModel):
    """Exact complex pressure evidence consumed by the decomposition algorithm."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal['1'] = '1'
    evidence_id: str = Field(
        pattern=r'^two-point-complex-pressure-evidence:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sample_ids: tuple[str, str]
    frequencies: tuple[TwoPointFrequencyPressure, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def exact_identity(self) -> 'TwoPointComplexPressureEvidence':
        if self.sample_ids[0] == self.sample_ids[1]:
            raise ValueError('complex pressure evidence sample ids must be distinct')
        frequency_values = [float(item.frequency_hz) for item in self.frequencies]
        if (
            frequency_values != sorted(frequency_values)
            or len(frequency_values) != len(set(frequency_values))
        ):
            raise ValueError('complex pressure evidence frequencies must be unique and sorted')
        digest = canonical_spatial_decomposition_sha256(self.semantic_payload())
        if self.semantic_sha256 != digest:
            raise ValueError('TwoPointComplexPressureEvidence semantic hash mismatch')
        if self.evidence_id != _semantic_id(
            'two-point-complex-pressure-evidence',
            digest,
        ):
            raise ValueError('TwoPointComplexPressureEvidence deterministic id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'sample_ids': list(self.sample_ids),
            'frequencies': [
                item.model_dump(mode='json') for item in self.frequencies
            ],
        }


def build_two_point_complex_pressure_evidence(
    *,
    sample_ids: tuple[str, str],
    frequencies: Sequence[TwoPointFrequencyPressure],
) -> TwoPointComplexPressureEvidence:
    values = tuple(frequencies)
    payload = {
        'authority_version': '1',
        'sample_ids': list(sample_ids),
        'frequencies': [item.model_dump(mode='json') for item in values],
    }
    digest = canonical_spatial_decomposition_sha256(payload)
    return TwoPointComplexPressureEvidence(
        evidence_id=_semantic_id('two-point-complex-pressure-evidence', digest),
        semantic_sha256=digest,
        sample_ids=sample_ids,
        frequencies=values,
    )


class FrequencyConditioningEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    state: ConditioningState
    determinant: ComplexPressureValue
    abs_determinant: float = Field(ge=0.0)
    condition_number_2: float | None = Field(default=None, gt=0.0)
    condition_number_is_infinite: bool = False
    minimum_abs_determinant: float = Field(gt=0.0)
    maximum_condition_number_2: float = Field(gt=1.0)

    @model_validator(mode='after')
    def exact_condition_number_representation(
        self,
    ) -> 'FrequencyConditioningEvidence':
        if not isfinite(float(self.abs_determinant)):
            raise ValueError('determinant magnitude must be finite')
        if self.condition_number_is_infinite:
            if self.condition_number_2 is not None:
                raise ValueError(
                    'infinite condition number must not carry a fabricated finite value'
                )
        elif self.condition_number_2 is None:
            raise ValueError('finite condition number requires an exact numeric value')
        elif not isfinite(float(self.condition_number_2)):
            raise ValueError('finite condition number representation must be finite')
        return self


class SpatialFieldDecompositionFrequencyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    incident_pressure: ComplexPressureValue | None = None
    reflected_pressure: ComplexPressureValue | None = None
    reflection_coefficient: ComplexPressureValue | None = None
    reconstructed_sample_1_pressure: ComplexPressureValue | None = None
    reconstructed_sample_2_pressure: ComplexPressureValue | None = None
    reconstruction_residual_max_abs: float | None = Field(default=None, ge=0.0)
    conditioning: FrequencyConditioningEvidence
    status: DecompositionStatus
    reasons: tuple[str, ...]

    @model_validator(mode='after')
    def result_state_is_consistent(
        self,
    ) -> 'SpatialFieldDecompositionFrequencyResult':
        if self.status == 'AVAILABLE':
            required = (
                self.incident_pressure,
                self.reflected_pressure,
                self.reflection_coefficient,
                self.reconstructed_sample_1_pressure,
                self.reconstructed_sample_2_pressure,
                self.reconstruction_residual_max_abs,
            )
            if any(item is None for item in required):
                raise ValueError('AVAILABLE decomposition result must be complete')
            if self.reasons:
                raise ValueError('AVAILABLE decomposition result must not carry reasons')
        else:
            if not self.reasons:
                raise ValueError('blocked/unsupported decomposition requires a reason')
            if self.status == 'UNSUPPORTED' and any(
                item is not None
                for item in (
                    self.incident_pressure,
                    self.reflected_pressure,
                    self.reflection_coefficient,
                    self.reconstructed_sample_1_pressure,
                    self.reconstructed_sample_2_pressure,
                    self.reconstruction_residual_max_abs,
                )
            ):
                raise ValueError('UNSUPPORTED decomposition must not expose numerical output')
            if (
                self.conditioning.state in {'SINGULAR', 'ILL_CONDITIONED'}
                and any(
                    item is not None
                    for item in (
                        self.incident_pressure,
                        self.reflected_pressure,
                        self.reflection_coefficient,
                        self.reconstructed_sample_1_pressure,
                        self.reconstructed_sample_2_pressure,
                        self.reconstruction_residual_max_abs,
                    )
                )
            ):
                raise ValueError(
                    'singular/ill-conditioned decomposition must not expose numerical output'
                )
        return self


class SpatialFieldDecompositionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SPATIAL_FIELD_DECOMPOSITION_SCHEMA_VERSION
    authority_version: Literal[
        'r100-spatial-decomposition-1'
    ] = SPATIAL_FIELD_DECOMPOSITION_AUTHORITY_VERSION
    algorithm_id: Literal[
        'htdt.planar-normal-incidence-two-point-complex-pressure'
    ] = SPATIAL_FIELD_DECOMPOSITION_METHOD_ID
    algorithm_version: Literal['1'] = SPATIAL_FIELD_DECOMPOSITION_METHOD_VERSION

    result_id: str = Field(
        pattern=r'^spatial-field-decomposition-result:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    spec_id: str = Field(pattern=r'^spatial-field-decomposition:[0-9a-f]{64}$')
    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    pressure_evidence_id: str = Field(
        pattern=r'^two-point-complex-pressure-evidence:[0-9a-f]{64}$'
    )
    pressure_evidence_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    overall_status: DecompositionStatus
    frequency_results: tuple[SpatialFieldDecompositionFrequencyResult, ...] = Field(
        min_length=1
    )

    @model_validator(mode='after')
    def exact_result_identity(self) -> 'SpatialFieldDecompositionResult':
        statuses = {item.status for item in self.frequency_results}
        expected_status: DecompositionStatus
        if statuses == {'AVAILABLE'}:
            expected_status = 'AVAILABLE'
        elif statuses == {'UNSUPPORTED'}:
            expected_status = 'UNSUPPORTED'
        else:
            expected_status = 'BLOCKED'
        if self.overall_status != expected_status:
            raise ValueError('decomposition overall status does not match frequency states')

        digest = canonical_spatial_decomposition_sha256(self.semantic_payload())
        if self.semantic_sha256 != digest:
            raise ValueError('SpatialFieldDecompositionResult semantic hash mismatch')
        if self.result_id != _semantic_id(
            'spatial-field-decomposition-result',
            digest,
        ):
            raise ValueError('SpatialFieldDecompositionResult deterministic id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'algorithm_id': self.algorithm_id,
            'algorithm_version': self.algorithm_version,
            'spec_id': self.spec_id,
            'spec_semantic_sha256': self.spec_semantic_sha256,
            'pressure_evidence_id': self.pressure_evidence_id,
            'pressure_evidence_semantic_sha256': (
                self.pressure_evidence_semantic_sha256
            ),
            'overall_status': self.overall_status,
            'frequency_results': [
                item.model_dump(mode='json') for item in self.frequency_results
            ],
        }


def _matrix_conditioning(
    a: complex,
    b: complex,
    c: complex,
    d: complex,
) -> tuple[complex, float, float]:
    determinant = a * d - b * c
    abs_determinant = abs(determinant)

    frobenius_squared = (
        abs(a) ** 2
        + abs(b) ** 2
        + abs(c) ** 2
        + abs(d) ** 2
    )
    discriminant = max(
        0.0,
        frobenius_squared * frobenius_squared
        - 4.0 * abs_determinant * abs_determinant,
    )
    lambda_max = 0.5 * (frobenius_squared + sqrt(discriminant))
    lambda_min = 0.5 * (frobenius_squared - sqrt(discriminant))
    if lambda_min <= 0.0:
        condition_number = float('inf')
    else:
        condition_number = sqrt(lambda_max / lambda_min)
    return determinant, float(abs_determinant), float(condition_number)


def _conditioning_evidence(
    *,
    state: ConditioningState,
    determinant: complex,
    abs_determinant: float,
    condition_number: float,
    policy: SpatialDecompositionConditioningPolicy,
) -> FrequencyConditioningEvidence:
    finite_condition_number = isfinite(float(condition_number))
    return FrequencyConditioningEvidence(
        state=state,
        determinant=ComplexPressureValue.from_complex(determinant),
        abs_determinant=abs_determinant,
        condition_number_2=(
            float(condition_number) if finite_condition_number else None
        ),
        condition_number_is_infinite=not finite_condition_number,
        minimum_abs_determinant=policy.minimum_abs_determinant,
        maximum_condition_number_2=policy.maximum_condition_number_2,
    )


def decompose_planar_normal_incidence_two_point(
    *,
    spec: SpatialFieldDecompositionSpec,
    pressure_evidence: TwoPointComplexPressureEvidence,
) -> SpatialFieldDecompositionResult:
    """Solve the canonical two-by-two complex system frequency by frequency.

    HTDT's R100 comparison authority uses the time dependence exp(-j*omega*t).
    With inward distance d >= 0 increasing into the acoustic domain:

        p(d) = I * exp(-j*k*d) + R * exp(+j*k*d)

    I propagates toward the boundary (decreasing d). R propagates away from the
    boundary (increasing d). No conjugation or sign flip is performed here.
    """

    if spec.raw_complex_pressure_evidence_ref.authority_id != pressure_evidence.evidence_id:
        raise ValueError('spec raw complex pressure evidence id does not match input evidence')
    if (
        spec.raw_complex_pressure_evidence_ref.semantic_sha256
        != pressure_evidence.semantic_sha256
    ):
        raise ValueError(
            'spec raw complex pressure evidence hash does not match input evidence'
        )
    expected_sample_ids = tuple(item.sample_id for item in spec.sample_points)
    if pressure_evidence.sample_ids != expected_sample_ids:
        raise ValueError('complex pressure evidence sample identity mismatch')
    evidence_frequencies = tuple(
        float(item.frequency_hz) for item in pressure_evidence.frequencies
    )
    if evidence_frequencies != tuple(float(item) for item in spec.frequency_grid_hz):
        raise ValueError('complex pressure evidence frequency grid mismatch')

    unsupported_convention = (
        spec.canonical_phasor_convention != 'exp(-i*omega*t)'
        or spec.pressure_evidence_phasor_convention != 'exp(-i*omega*t)'
        or spec.pressure_analysis_kernel != 'exp(+i*omega*t)'
    )

    d1 = float(spec.sample_points[0].inward_distance_m)
    d2 = float(spec.sample_points[1].inward_distance_m)
    c_m_s = float(spec.sound_speed_m_s)
    policy = spec.conditioning_policy
    results: list[SpatialFieldDecompositionFrequencyResult] = []

    for sample in pressure_evidence.frequencies:
        frequency_hz = float(sample.frequency_hz)
        k = 2.0 * pi * frequency_hz / c_m_s
        a = exp(-1j * k * d1)
        b = exp(+1j * k * d1)
        c = exp(-1j * k * d2)
        d = exp(+1j * k * d2)
        determinant, abs_determinant, condition_number = _matrix_conditioning(
            a,
            b,
            c,
            d,
        )

        if unsupported_convention:
            results.append(
                SpatialFieldDecompositionFrequencyResult(
                    frequency_hz=frequency_hz,
                    conditioning=_conditioning_evidence(
                        state='UNSUPPORTED_CONVENTION',
                        determinant=determinant,
                        abs_determinant=abs_determinant,
                        condition_number=condition_number,
                        policy=policy,
                    ),
                    status='UNSUPPORTED',
                    reasons=(
                        'pressure evidence convention is not the canonical '
                        'exp(-i*omega*t) phasor with exp(+i*omega*t) analysis kernel; '
                        'an explicit adapter conversion is required before decomposition',
                    ),
                )
            )
            continue

        if abs_determinant < float(policy.minimum_abs_determinant):
            results.append(
                SpatialFieldDecompositionFrequencyResult(
                    frequency_hz=frequency_hz,
                    conditioning=_conditioning_evidence(
                        state='SINGULAR',
                        determinant=determinant,
                        abs_determinant=abs_determinant,
                        condition_number=condition_number,
                        policy=policy,
                    ),
                    status='BLOCKED',
                    reasons=(
                        'decomposition matrix determinant is below the explicit '
                        'conditioning threshold',
                    ),
                )
            )
            continue

        if (
            not isfinite(condition_number)
            or condition_number > float(policy.maximum_condition_number_2)
        ):
            results.append(
                SpatialFieldDecompositionFrequencyResult(
                    frequency_hz=frequency_hz,
                    conditioning=_conditioning_evidence(
                        state='ILL_CONDITIONED',
                        determinant=determinant,
                        abs_determinant=abs_determinant,
                        condition_number=condition_number,
                        policy=policy,
                    ),
                    status='BLOCKED',
                    reasons=(
                        'decomposition matrix condition number exceeds the explicit '
                        'conditioning threshold',
                    ),
                )
            )
            continue

        p1 = sample.sample_1_pressure.as_complex()
        p2 = sample.sample_2_pressure.as_complex()
        incident = (p1 * d - b * p2) / determinant
        reflected = (a * p2 - p1 * c) / determinant

        reconstructed_1 = incident * a + reflected * b
        reconstructed_2 = incident * c + reflected * d
        residual = max(
            abs(reconstructed_1 - p1),
            abs(reconstructed_2 - p2),
        )

        common = {
            'frequency_hz': frequency_hz,
            'incident_pressure': ComplexPressureValue.from_complex(incident),
            'reflected_pressure': ComplexPressureValue.from_complex(reflected),
            'reconstructed_sample_1_pressure': (
                ComplexPressureValue.from_complex(reconstructed_1)
            ),
            'reconstructed_sample_2_pressure': (
                ComplexPressureValue.from_complex(reconstructed_2)
            ),
            'reconstruction_residual_max_abs': float(residual),
        }

        if abs(incident) <= float(policy.incident_magnitude_floor):
            results.append(
                SpatialFieldDecompositionFrequencyResult(
                    **common,
                    conditioning=_conditioning_evidence(
                        state='INCIDENT_UNDEFINED',
                        determinant=determinant,
                        abs_determinant=abs_determinant,
                        condition_number=condition_number,
                        policy=policy,
                    ),
                    status='BLOCKED',
                    reasons=(
                        'incident pressure magnitude is at or below the explicit floor; '
                        'reflection coefficient R/I is undefined',
                    ),
                )
            )
            continue

        results.append(
            SpatialFieldDecompositionFrequencyResult(
                **common,
                reflection_coefficient=ComplexPressureValue.from_complex(
                    reflected / incident
                ),
                conditioning=_conditioning_evidence(
                    state='WELL_CONDITIONED',
                    determinant=determinant,
                    abs_determinant=abs_determinant,
                    condition_number=condition_number,
                    policy=policy,
                ),
                status='AVAILABLE',
                reasons=(),
            )
        )

    statuses = {item.status for item in results}
    overall_status: DecompositionStatus
    if statuses == {'AVAILABLE'}:
        overall_status = 'AVAILABLE'
    elif statuses == {'UNSUPPORTED'}:
        overall_status = 'UNSUPPORTED'
    else:
        overall_status = 'BLOCKED'

    payload = {
        'schema_version': SPATIAL_FIELD_DECOMPOSITION_SCHEMA_VERSION,
        'authority_version': SPATIAL_FIELD_DECOMPOSITION_AUTHORITY_VERSION,
        'algorithm_id': SPATIAL_FIELD_DECOMPOSITION_METHOD_ID,
        'algorithm_version': SPATIAL_FIELD_DECOMPOSITION_METHOD_VERSION,
        'spec_id': spec.spec_id,
        'spec_semantic_sha256': spec.semantic_sha256,
        'pressure_evidence_id': pressure_evidence.evidence_id,
        'pressure_evidence_semantic_sha256': pressure_evidence.semantic_sha256,
        'overall_status': overall_status,
        'frequency_results': [item.model_dump(mode='json') for item in results],
    }
    digest = canonical_spatial_decomposition_sha256(payload)
    return SpatialFieldDecompositionResult(
        result_id=_semantic_id('spatial-field-decomposition-result', digest),
        semantic_sha256=digest,
        spec_id=spec.spec_id,
        spec_semantic_sha256=spec.semantic_sha256,
        pressure_evidence_id=pressure_evidence.evidence_id,
        pressure_evidence_semantic_sha256=pressure_evidence.semantic_sha256,
        overall_status=overall_status,
        frequency_results=tuple(results),
    )
