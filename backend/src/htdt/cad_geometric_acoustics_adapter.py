from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version as distribution_version
import json
from math import atan2, degrees, isfinite, sqrt
from pathlib import Path
import sqlite3
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .acoustic_benchmark import AcousticMaterial
from .cad_acoustic_snapshot import AcousticPredictionRequest, AcousticSceneSnapshot
from .cad_acoustic_snapshot_repository import CadAcousticSnapshotRepository
from .cad_acoustic_solver_adapter import (
    AcousticSolverAdapterDescriptor,
    AcousticSolverDispatchBinding,
)
from .cad_acoustic_solver_dispatch_repository import CadAcousticSolverDispatchRepository
from .cad_acoustic_solver_result import (
    AcousticSolverObservableArtifact,
    AcousticSolverResultEnvelope,
    build_acoustic_solver_result_envelope,
)
from .cad_directivity import DirectivityDataset, evaluate_directivity
from .cad_equipment import FrequencyDomain
from .cad_geometric_acoustics_portal import (
    PORTAL_SIDE_SEMANTICS,
    GeometricPortalAperture,
    compile_single_portal_aperture,
    region_membership_with_portal_cap,
    resolve_direct_portal_crossing,
)
from .cad_repository import SceneRepository
from .cad_scene import Direction3, Position3
from .cad_schema import ensure_native_schema
from .r120_geometry_compiler import (
    AcousticRegionAuthority,
    BoundaryTerminationAuthority,
    CompiledSurfaceMapping,
    ExactExternalAuthorityRef,
    PortalAuthority,
    R120CompiledGeometry,
)


DETERMINISTIC_GA_SCHEMA_VERSION = 1
DETERMINISTIC_GA_AUTHORITY_VERSION = 'r150-deterministic-ga-1'
DETERMINISTIC_GA_ADAPTER_ID = 'htdt.r150.deterministic-path'
DETERMINISTIC_GA_ADAPTER_VERSION = '1'
PYROOMACOUSTICS_ENGINE_ID = 'pyroomacoustics.image_source_model'
PYROOMACOUSTICS_ENGINE_VERSION = '0.10.1'
PYROOMACOUSTICS_CANDIDATE_SOURCE_COMMIT = 'f02b01dd6609709e2089aefa5d1e59c91d3a0601'
HTDT_PLANAR_ENGINE_ID = 'htdt.r150.general_planar_image_construction'
HTDT_PLANAR_ENGINE_VERSION = '1'
HTDT_PLANAR_SECOND_ORDER_ENGINE_VERSION = '2'
HTDT_PORTAL_DIRECT_ENGINE_ID = 'htdt.r150.explicit_portal_direct'
HTDT_PORTAL_DIRECT_ENGINE_VERSION = '1'

PathType = Literal['direct', 'specular_reflection']
PathCandidateDecision = Literal[
    'BLOCKED_VISIBILITY',
    'UNSUPPORTED_DIRECTIVITY',
    'UNSUPPORTED_BOUNDARY_QUANTITY',
    'UNSUPPORTED_GEOMETRY',
    'INVALID_PORTAL_CROSSING',
    'INVALID_REGION_SEQUENCE',
]
AxisName = Literal['x', 'y', 'z']
PlaneSide = Literal['min', 'max']
GeometryPolicy = Literal[
    'exact_axis_aligned_closed_shoebox_v1',
    'general_planar_closed_polyhedral_v1',
    'general_planar_multi_region_portal_v1',
]
UnsupportedCapabilityReason = Literal[
    'UNSUPPORTED_PORTAL_TOPOLOGY',
    'UNSUPPORTED_BOUNDARY_TERMINATION',
    'UNSUPPORTED_REGION_TOPOLOGY',
    'UNSUPPORTED_REGION_MEMBERSHIP',
    'UNSUPPORTED_GEOMETRY',
    'UNSUPPORTED_PORTAL_APERTURE',
    'UNSUPPORTED_PORTAL_STATE',
    'UNSUPPORTED_PORTAL_ORIENTATION',
]


class DeterministicGaUnsupportedError(ValueError):
    """Typed fail-closed capability error for unsupported GA geometry/topology."""

    def __init__(
        self,
        reason_code: UnsupportedCapabilityReason,
        message: str,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code



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


PYROOMACOUSTICS_SOLVER_IMPLEMENTATION_REF = ExactExternalAuthorityRef(
    authority_id='candidate:pyroomacoustics',
    authority_version=PYROOMACOUSTICS_ENGINE_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'package': 'pyroomacoustics',
            'version': PYROOMACOUSTICS_ENGINE_VERSION,
            'source_commit': PYROOMACOUSTICS_CANDIDATE_SOURCE_COMMIT,
        }
    ),
)


HTDT_PLANAR_IMAGE_SOURCE_IMPLEMENTATION_REF = ExactExternalAuthorityRef(
    authority_id='adapter-kernel:htdt-r150-general-planar-first-order',
    authority_version=HTDT_PLANAR_ENGINE_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'implementation': HTDT_PLANAR_ENGINE_ID,
            'version': HTDT_PLANAR_ENGINE_VERSION,
            'construction': 'exact_plane_mirror_and_triangle_domain_first_order',
            'maximum_reflection_order': 1,
        }
    ),
)

HTDT_PORTAL_DIRECT_IMPLEMENTATION_REF = ExactExternalAuthorityRef(
    authority_id='adapter-kernel:htdt-r150-explicit-portal-direct',
    authority_version=HTDT_PORTAL_DIRECT_ENGINE_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'implementation': HTDT_PORTAL_DIRECT_ENGINE_ID,
            'version': HTDT_PORTAL_DIRECT_ENGINE_VERSION,
            'construction': 'exact_single_directed_portal_aperture_crossing',
            'maximum_portal_crossings': 1,
            'maximum_reflection_order': 0,
            'coherent_phase': 'unavailable_not_synthesized',
        }
    ),
)


HTDT_PLANAR_SECOND_ORDER_IMAGE_SOURCE_IMPLEMENTATION_REF = ExactExternalAuthorityRef(
    authority_id='adapter-kernel:htdt-r150-general-planar-second-order',
    authority_version=HTDT_PLANAR_SECOND_ORDER_ENGINE_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'implementation': HTDT_PLANAR_ENGINE_ID,
            'version': HTDT_PLANAR_SECOND_ORDER_ENGINE_VERSION,
            'construction': (
                'ordered_exact_plane_mirror_reverse_reconstruction_'
                'and_triangle_domain_through_second_order'
            ),
            'maximum_reflection_order': 2,
        }
    ),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ref_key(ref: ExactExternalAuthorityRef) -> tuple[str, str, str]:
    return (ref.authority_id, ref.authority_version, ref.semantic_hash_sha256)


def _position_tuple(value: Position3) -> tuple[float, float, float]:
    return (float(value.x_m), float(value.y_m), float(value.z_m))


def _point(value: tuple[float, float, float]) -> Position3:
    return Position3(x_m=value[0], y_m=value[1], z_m=value[2])


def _vector(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (
        float(b[0]) - float(a[0]),
        float(b[1]) - float(a[1]),
        float(b[2]) - float(a[2]),
    )


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(float(left) * float(right) for left, right in zip(a, b, strict=True))


def _cross(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (
        float(a[1]) * float(b[2]) - float(a[2]) * float(b[1]),
        float(a[2]) * float(b[0]) - float(a[0]) * float(b[2]),
        float(a[0]) * float(b[1]) - float(a[1]) * float(b[0]),
    )


def _norm(value: Sequence[float]) -> float:
    return sqrt(_dot(value, value))


def _unit(value: Sequence[float]) -> tuple[float, float, float]:
    length = _norm(value)
    if length <= 0.0:
        raise ValueError('zero-length direction is not permitted')
    return tuple(float(item) / length for item in value)  # type: ignore[return-value]


def _direction(value: Sequence[float]) -> Direction3:
    unit = _unit(value)
    return Direction3(x=unit[0], y=unit[1], z=unit[2])


def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    return _norm(_vector(a, b))


def _point_strictly_inside_box(
    point: Sequence[float],
    *,
    bounds: tuple[
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
    ],
    tolerance_m: float,
) -> bool:
    return all(
        minimum + tolerance_m < float(value) < maximum - tolerance_m
        for value, (minimum, maximum) in zip(point, bounds, strict=True)
    )


def _round_float(value: float, decimals: int) -> float:
    result = round(float(value), decimals)
    return 0.0 if result == 0.0 else result


def _rounded_position(
    value: Sequence[float],
    decimals: int,
) -> Position3:
    return Position3(
        x_m=_round_float(float(value[0]), decimals),
        y_m=_round_float(float(value[1]), decimals),
        z_m=_round_float(float(value[2]), decimals),
    )


def _rounded_direction(
    value: Sequence[float],
    decimals: int,
) -> Direction3:
    unit = _unit(value)
    rounded = tuple(_round_float(item, decimals) for item in unit)
    return _direction(rounded)


def _authority_ref(
    authority: AcousticRegionAuthority | PortalAuthority | BoundaryTerminationAuthority,
) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=authority.authority_id,
        authority_version=authority.authority_version,
        semantic_hash_sha256=authority.semantic_hash_sha256,
    )


class DeterministicGaConfiguration(BaseModel):
    """Exact bounded execution policy for this R150 foundation slice."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = DETERMINISTIC_GA_SCHEMA_VERSION
    authority_version: Literal['r150-deterministic-ga-1'] = (
        DETERMINISTIC_GA_AUTHORITY_VERSION
    )
    configuration_id: str = Field(pattern=r'^r150-ga-configuration:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    maximum_reflection_order: Literal[1, 2] = 1
    maximum_portal_crossings: Literal[1] | None = None
    frequency_centers_hz: tuple[float, ...] = Field(min_length=1)
    geometric_tolerance_m: float = Field(gt=0.0)
    engine_image_match_tolerance_m: float = Field(gt=0.0)
    identity_decimal_places: int = Field(default=12, ge=6, le=15)

    room_policy: GeometryPolicy = 'exact_axis_aligned_closed_shoebox_v1'
    source_directivity_policy: Literal[
        'exact_bound_dataset_magnitude_only_or_complex_magnitude'
    ] = 'exact_bound_dataset_magnitude_only_or_complex_magnitude'
    material_policy: Literal[
        'exact_center_banded_absorption_scattering'
    ] = 'exact_center_banded_absorption_scattering'
    specular_energy_policy: Literal[
        'one_minus_absorption_times_one_minus_scattering'
    ] = 'one_minus_absorption_times_one_minus_scattering'
    spreading_policy: Literal[
        'relative_energy_inverse_square'
    ] = 'relative_energy_inverse_square'
    coherent_phase_policy: Literal['unavailable_not_synthesized'] = (
        'unavailable_not_synthesized'
    )

    @field_validator(
        'frequency_centers_hz',
    )
    @classmethod
    def valid_frequency_centers(
        cls,
        values: tuple[float, ...],
    ) -> tuple[float, ...]:
        values = tuple(float(value) for value in values)
        if any(not isfinite(value) or value <= 0.0 for value in values):
            raise ValueError('GA frequency centers must be finite and positive')
        if values != tuple(sorted(set(values))):
            raise ValueError('GA frequency centers must be unique and sorted')
        return values

    @field_validator('geometric_tolerance_m', 'engine_image_match_tolerance_m')
    @classmethod
    def finite_positive(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value) or value <= 0.0:
            raise ValueError('GA numeric tolerances must be finite and positive')
        return value

    @model_validator(mode='after')
    def validate_identity(self) -> 'DeterministicGaConfiguration':
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('DeterministicGaConfiguration semantic hash mismatch')
        if self.configuration_id != f'r150-ga-configuration:{expected}':
            raise ValueError('DeterministicGaConfiguration id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'configuration_id', 'semantic_sha256'},
        )
        if self.maximum_portal_crossings is None:
            payload.pop('maximum_portal_crossings', None)
        return payload

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.configuration_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def build_deterministic_ga_configuration(
    *,
    frequency_centers_hz: Sequence[float],
    geometric_tolerance_m: float = 1.0e-9,
    engine_image_match_tolerance_m: float = 1.0e-8,
    identity_decimal_places: int = 12,
    room_policy: GeometryPolicy = 'exact_axis_aligned_closed_shoebox_v1',
    maximum_reflection_order: Literal[1, 2] = 1,
    maximum_portal_crossings: Literal[1] | None = None,
) -> DeterministicGaConfiguration:
    if (
        maximum_reflection_order == 2
        and room_policy != 'general_planar_closed_polyhedral_v1'
    ):
        raise ValueError(
            'bounded second-order specular execution is supported only by the '
            'general-planar geometry policy'
        )
    if room_policy == 'general_planar_multi_region_portal_v1':
        if maximum_reflection_order != 1:
            raise ValueError(
                'multi-region Portal lane is direct-only and does not enable reflected Portal paths'
            )
        if maximum_portal_crossings != 1:
            raise ValueError(
                'multi-region Portal lane requires explicit maximum_portal_crossings=1'
            )
    elif maximum_portal_crossings is not None:
        raise ValueError(
            'maximum_portal_crossings is only valid for the explicit multi-region Portal lane'
        )
    core: dict[str, Any] = {
        'schema_version': DETERMINISTIC_GA_SCHEMA_VERSION,
        'authority_version': DETERMINISTIC_GA_AUTHORITY_VERSION,
        'maximum_reflection_order': int(maximum_reflection_order),
        'frequency_centers_hz': sorted(set(float(value) for value in frequency_centers_hz)),
        'geometric_tolerance_m': float(geometric_tolerance_m),
        'engine_image_match_tolerance_m': float(engine_image_match_tolerance_m),
        'identity_decimal_places': int(identity_decimal_places),
        'room_policy': room_policy,
        'source_directivity_policy': (
            'exact_bound_dataset_magnitude_only_or_complex_magnitude'
        ),
        'material_policy': 'exact_center_banded_absorption_scattering',
        'specular_energy_policy': (
            'one_minus_absorption_times_one_minus_scattering'
        ),
        'spreading_policy': 'relative_energy_inverse_square',
        'coherent_phase_policy': 'unavailable_not_synthesized',
    }
    if maximum_portal_crossings is not None:
        core['maximum_portal_crossings'] = int(maximum_portal_crossings)
    digest = _semantic_hash(core)
    return DeterministicGaConfiguration(
        configuration_id=f'r150-ga-configuration:{digest}',
        semantic_sha256=digest,
        **core,
    )


class GeometricMaterialAuthority(BaseModel):
    """Resolved external material authority plus its existing GA material payload."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_ref: ExactExternalAuthorityRef
    material: AcousticMaterial


class GeometricSurfacePlane(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')
    axis: AxisName | None = None
    side: PlaneSide | None = None
    coordinate_m: float | None = None
    point_m: Position3 | None = None
    normal: Direction3 | None = None
    compiled_triangle_indices: tuple[int, ...] | None = None
    material_authority: ExactExternalAuthorityRef | None = None
    boundary_physics_authority: ExactExternalAuthorityRef | None = None

    @model_validator(mode='after')
    def validate_plane_representation(self) -> 'GeometricSurfacePlane':
        legacy = (
            self.axis is not None
            and self.side is not None
            and self.coordinate_m is not None
            and self.point_m is None
            and self.normal is None
            and self.compiled_triangle_indices is None
        )
        general = (
            self.axis is None
            and self.side is None
            and self.coordinate_m is None
            and self.point_m is not None
            and self.normal is not None
            and bool(self.compiled_triangle_indices)
        )
        if legacy == general:
            raise ValueError(
                'surface plane must use exactly one legacy-axis or '
                'general-planar representation'
            )
        return self

class DeterministicGaSourceInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_entity_id: str = Field(min_length=1)
    r110_compiled_source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_reference_point: Position3
    source_axis: Direction3
    directivity_dataset_id: str = Field(min_length=1)
    directivity_dataset_version: str = Field(min_length=1)
    directivity_dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    receiver_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    world_position: Position3
    acoustic_region_id: str | None = None


class DeterministicGaExecutionInput(BaseModel):
    """Typed exact input handed across the HTDT -> candidate-engine boundary."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = DETERMINISTIC_GA_SCHEMA_VERSION
    authority_version: Literal['r150-deterministic-ga-1'] = (
        DETERMINISTIC_GA_AUTHORITY_VERSION
    )
    execution_input_id: str = Field(
        pattern=r'^r150-ga-execution-input:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    snapshot_id: str
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prediction_request_id: str
    prediction_request_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    dispatch_binding_id: str
    dispatch_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    adapter_descriptor_id: str
    adapter_descriptor_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_implementation_ref: ExactExternalAuthorityRef
    solver_configuration_ref: ExactExternalAuthorityRef

    r120_compiled_geometry_id: str
    r120_compiled_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    region_authority_ref: ExactExternalAuthorityRef
    portal_authority_ref: ExactExternalAuthorityRef
    boundary_termination_authority_ref: ExactExternalAuthorityRef

    room_origin_m: Position3
    room_dimensions_m: tuple[float, float, float]
    boundary_planes: tuple[GeometricSurfacePlane, ...]
    geometry_policy: Literal[
        'general_planar_closed_polyhedral_v1',
        'general_planar_multi_region_portal_v1',
    ] | None = None
    unsupported_reflection_surface_ids: tuple[str, ...] | None = None
    portal_apertures: tuple[GeometricPortalAperture, ...] | None = None
    maximum_portal_crossings: Literal[1] | None = None
    occluder_triangle_indices: tuple[int, ...]
    sources: tuple[DeterministicGaSourceInput, ...]
    receivers: tuple[DeterministicGaReceiverInput, ...]
    sound_speed_m_s: float = Field(gt=0.0)
    frequency_domain: FrequencyDomain
    frequency_centers_hz: tuple[float, ...]
    geometric_tolerance_m: float = Field(gt=0.0)
    engine_image_match_tolerance_m: float = Field(gt=0.0)
    identity_decimal_places: int = Field(ge=6, le=15)
    maximum_reflection_order: Literal[2] | None = None

    @model_validator(mode='after')
    def validate_identity(self) -> 'DeterministicGaExecutionInput':
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('DeterministicGaExecutionInput semantic hash mismatch')
        if self.execution_input_id != f'r150-ga-execution-input:{expected}':
            raise ValueError('DeterministicGaExecutionInput id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'execution_input_id', 'semantic_sha256'},
        )
        if self.geometry_policy is None:
            payload.pop('geometry_policy', None)
        if self.unsupported_reflection_surface_ids is None:
            payload.pop('unsupported_reflection_surface_ids', None)
        if self.maximum_reflection_order is None:
            payload.pop('maximum_reflection_order', None)
        if self.portal_apertures is None:
            payload.pop('portal_apertures', None)
        if self.maximum_portal_crossings is None:
            payload.pop('maximum_portal_crossings', None)
        for source in payload['sources']:
            if source.get('acoustic_region_id') is None:
                source.pop('acoustic_region_id', None)
        for receiver in payload['receivers']:
            if receiver.get('acoustic_region_id') is None:
                receiver.pop('acoustic_region_id', None)
        for plane in payload['boundary_planes']:
            for key in (
                'axis',
                'side',
                'coordinate_m',
                'point_m',
                'normal',
                'compiled_triangle_indices',
            ):
                if plane.get(key) is None:
                    plane.pop(key, None)
        return payload


class SourceDirectivityContribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    dataset_id: str
    dataset_version: str
    dataset_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evaluation_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    frequency_hz: float = Field(gt=0.0)
    horizontal_angle_deg: float
    vertical_angle_deg: float
    magnitude_db: float
    magnitude_linear: float = Field(gt=0.0)
    energy_factor: float = Field(gt=0.0)


class BoundaryMaterialContribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_surface_id: str
    material_authority: ExactExternalAuthorityRef
    boundary_physics_authority: ExactExternalAuthorityRef | None = None
    frequency_hz: float = Field(gt=0.0)
    absorption: float = Field(ge=0.0, le=1.0)
    scattering: float = Field(ge=0.0, le=1.0)
    specular_energy_factor: float = Field(ge=0.0, le=1.0)
    coherent_reflection_phase: Literal['UNAVAILABLE'] = 'UNAVAILABLE'


class DeterministicPathBandQuantity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    band_definition: Literal['exact_center_frequency_sample'] = (
        'exact_center_frequency_sample'
    )
    center_hz: float = Field(gt=0.0)
    quantity: Literal['relative_energy_transport_per_m2'] = (
        'relative_energy_transport_per_m2'
    )
    spreading_factor_per_m2: float = Field(gt=0.0)
    source_directivity: SourceDirectivityContribution
    boundary_material: BoundaryMaterialContribution | None = None
    boundary_materials: tuple[BoundaryMaterialContribution, ...] | None = None
    relative_energy_transport_per_m2: float = Field(ge=0.0)
    coherent_phase: Literal['UNAVAILABLE_NOT_SYNTHESIZED'] = (
        'UNAVAILABLE_NOT_SYNTHESIZED'
    )


class DeterministicPathInteraction(BaseModel):
    """Version-compatible typed reflection/Portal interaction sequence entry."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: Literal['reflection', 'portal_crossing']
    point: Position3
    surface_id: str | None = None
    portal_id: str | None = None
    from_region_id: str | None = None
    to_region_id: str | None = None

    @model_validator(mode='after')
    def validate_interaction(self) -> 'DeterministicPathInteraction':
        if self.kind == 'reflection':
            if self.surface_id is None:
                raise ValueError('reflection interaction requires surface_id')
            if any(
                item is not None
                for item in (self.portal_id, self.from_region_id, self.to_region_id)
            ):
                raise ValueError('reflection interaction cannot carry Portal fields')
        else:
            if (
                self.portal_id is None
                or self.from_region_id is None
                or self.to_region_id is None
            ):
                raise ValueError(
                    'portal crossing requires portal_id and ordered from/to regions'
                )
            if self.surface_id is not None:
                raise ValueError('portal crossing cannot masquerade as a reflection surface')
            if self.from_region_id == self.to_region_id:
                raise ValueError('portal crossing regions must be distinct')
        return self


class DeterministicAcousticPath(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    path_id: str = Field(pattern=r'^deterministic-acoustic-path:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_entity_id: str
    receiver_id: str
    receiver_entity_id: str
    path_type: PathType
    ordered_interaction_surface_ids: tuple[str, ...]
    ordered_interaction_points: tuple[Position3, ...]
    ordered_interactions: tuple[DeterministicPathInteraction, ...] | None = None
    ordered_region_ids: tuple[str, ...] | None = None
    geometric_path_length_m: float = Field(gt=0.0)
    propagation_delay_s: float = Field(gt=0.0)
    departure_direction: Direction3
    arrival_direction: Direction3
    direction_semantics: Literal[
        'world_propagation_direction_source_out_and_receiver_in'
    ] = 'world_propagation_direction_source_out_and_receiver_in'
    bands: tuple[DeterministicPathBandQuantity, ...] = Field(min_length=1)
    adapter_id: Literal[
        'htdt.r150.deterministic-path'
    ] = DETERMINISTIC_GA_ADAPTER_ID
    adapter_version: Literal['1'] = DETERMINISTIC_GA_ADAPTER_VERSION
    solver_implementation_ref: ExactExternalAuthorityRef

    @model_validator(mode='after')
    def validate_path(self) -> 'DeterministicAcousticPath':
        interaction_count = len(self.ordered_interaction_surface_ids)
        if interaction_count != len(self.ordered_interaction_points):
            raise ValueError('interaction surface/point sequence lengths must match')
        if self.path_type == 'direct':
            if interaction_count:
                raise ValueError('direct path cannot carry interaction surfaces')
        elif interaction_count not in (1, 2):
            raise ValueError(
                'bounded specular reflection requires one or two ordered surfaces'
            )

        if self.ordered_interactions is not None:
            reflections = tuple(
                item for item in self.ordered_interactions if item.kind == 'reflection'
            )
            if tuple(item.surface_id for item in reflections) != self.ordered_interaction_surface_ids:
                raise ValueError('typed reflection interactions must match legacy ordered surfaces')
            if tuple(item.point for item in reflections) != self.ordered_interaction_points:
                raise ValueError('typed reflection points must match legacy ordered points')
            portals = tuple(
                item for item in self.ordered_interactions if item.kind == 'portal_crossing'
            )
            if portals:
                if self.path_type != 'direct' or len(portals) != 1 or reflections:
                    raise ValueError(
                        'current bounded Portal lane supports one Portal crossing on direct paths only'
                    )
                if self.ordered_region_ids is None or len(self.ordered_region_ids) != 2:
                    raise ValueError('Portal path requires exactly two ordered regions')
                portal = portals[0]
                if (
                    portal.from_region_id,
                    portal.to_region_id,
                ) != self.ordered_region_ids:
                    raise ValueError('Portal interaction region order mismatch')
            elif self.ordered_region_ids is not None:
                raise ValueError('region sequence without Portal crossing is not emitted by this schema')
        elif self.ordered_region_ids is not None:
            raise ValueError('ordered_region_ids require typed interactions')

        for band in self.bands:
            if interaction_count == 0:
                if (
                    band.boundary_material is not None
                    or band.boundary_materials is not None
                ):
                    raise ValueError(
                        'direct path cannot carry boundary material contribution'
                    )
            elif interaction_count == 1:
                if (
                    band.boundary_material is None
                    or band.boundary_materials is not None
                ):
                    raise ValueError(
                        'first-order reflection requires exactly one boundary '
                        'material contribution'
                    )
                if (
                    band.boundary_material.source_surface_id
                    != self.ordered_interaction_surface_ids[0]
                ):
                    raise ValueError(
                        'first-order boundary material must match ordered surface identity'
                    )
            else:
                if (
                    band.boundary_material is not None
                    or band.boundary_materials is None
                ):
                    raise ValueError(
                        'second-order reflection requires ordered boundary '
                        'material contributions'
                    )
                if len(band.boundary_materials) != 2:
                    raise ValueError(
                        'second-order reflection requires exactly two material contributions'
                    )
                if tuple(
                    item.source_surface_id for item in band.boundary_materials
                ) != self.ordered_interaction_surface_ids:
                    raise ValueError(
                        'second-order material contributions must match ordered surfaces'
                    )

        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('DeterministicAcousticPath semantic hash mismatch')
        if self.path_id != f'deterministic-acoustic-path:{expected}':
            raise ValueError('DeterministicAcousticPath id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'path_id', 'semantic_sha256'},
        )
        if self.ordered_interactions is None:
            payload.pop('ordered_interactions', None)
        if self.ordered_region_ids is None:
            payload.pop('ordered_region_ids', None)
        for band in payload['bands']:
            if band.get('boundary_materials') is None:
                band.pop('boundary_materials', None)
        return payload


class RejectedPathCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_entity_id: str
    receiver_id: str
    path_type: PathType
    interaction_surface_ids: tuple[str, ...] = ()
    decision: PathCandidateDecision
    reason: str = Field(min_length=1)


class DeterministicPathArtifact(BaseModel):
    """Immutable phase-free direct/first-/second-order specular path artifact."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = DETERMINISTIC_GA_SCHEMA_VERSION
    authority_version: Literal['r150-deterministic-ga-1'] = (
        DETERMINISTIC_GA_AUTHORITY_VERSION
    )
    artifact_id: str = Field(
        pattern=r'^deterministic-path-artifact:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    execution_id: str = Field(pattern=r'^r150-ga-execution:[0-9a-f]{64}$')
    execution_input_id: str
    execution_input_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    snapshot_id: str
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prediction_request_id: str
    prediction_request_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    dispatch_binding_id: str
    dispatch_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    adapter_descriptor_id: str
    adapter_descriptor_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_implementation_ref: ExactExternalAuthorityRef
    solver_configuration_ref: ExactExternalAuthorityRef
    r120_compiled_geometry_id: str
    r120_compiled_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    engine_id: str
    engine_version: str
    candidate_source_commit: str | None = None
    numeric_comparison_tolerance_m: float = Field(gt=0.0)
    identity_decimal_places: int = Field(ge=6, le=15)
    frequency_domain: FrequencyDomain
    path_scope: Literal[
        'direct_and_first_order_specular',
        'direct_through_second_order_specular',
        'direct_single_portal_propagation',
    ] = 'direct_and_first_order_specular'
    coherent_phase_authority: Literal['UNAVAILABLE_NOT_SYNTHESIZED'] = (
        'UNAVAILABLE_NOT_SYNTHESIZED'
    )
    paths: tuple[DeterministicAcousticPath, ...]
    rejected_candidates: tuple[RejectedPathCandidate, ...]

    @model_validator(mode='after')
    def validate_artifact(self) -> 'DeterministicPathArtifact':
        path_ids = [item.path_id for item in self.paths]
        if len(path_ids) != len(set(path_ids)):
            raise ValueError('deterministic path artifact contains duplicate path ids')
        ordering = [
            (
                item.source_entity_id,
                item.receiver_id,
                0
                if item.path_type == 'direct'
                else len(item.ordered_interaction_surface_ids),
                item.ordered_interaction_surface_ids,
                item.path_id,
            )
            for item in self.paths
        ]
        if ordering != sorted(ordering):
            raise ValueError('deterministic paths must use canonical ordering')
        rejected_order = [
            (
                item.source_entity_id,
                item.receiver_id,
                0
                if item.path_type == 'direct'
                else len(item.interaction_surface_ids),
                item.interaction_surface_ids,
                item.decision,
                item.reason,
            )
            for item in self.rejected_candidates
        ]
        if rejected_order != sorted(rejected_order):
            raise ValueError('rejected path candidates must use canonical ordering')
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('DeterministicPathArtifact semantic hash mismatch')
        if self.artifact_id != f'deterministic-path-artifact:{expected}':
            raise ValueError('DeterministicPathArtifact id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'artifact_id', 'semantic_sha256'},
        )
        for path in payload['paths']:
            for band in path['bands']:
                if band.get('boundary_materials') is None:
                    band.pop('boundary_materials', None)
        return payload

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.artifact_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )

    def execution_provenance_ref(self) -> ExactExternalAuthorityRef:
        payload = {
            'execution_id': self.execution_id,
            'execution_input_id': self.execution_input_id,
            'execution_input_sha256': self.execution_input_sha256,
            'adapter_descriptor_id': self.adapter_descriptor_id,
            'adapter_descriptor_sha256': self.adapter_descriptor_sha256,
            'solver_implementation_ref': self.solver_implementation_ref.model_dump(
                mode='json'
            ),
            'solver_configuration_ref': self.solver_configuration_ref.model_dump(
                mode='json'
            ),
            'engine_id': self.engine_id,
            'engine_version': self.engine_version,
            'candidate_source_commit': self.candidate_source_commit,
        }
        digest = _semantic_hash(payload)
        return ExactExternalAuthorityRef(
            authority_id=f'r150-ga-execution-provenance:{digest}',
            authority_version=self.authority_version,
            semantic_hash_sha256=digest,
        )


DETERMINISTIC_PATH_ARTIFACT_SCHEMA_REF = ExactExternalAuthorityRef(
    authority_id='htdt.deterministic-path-artifact.schema',
    authority_version=DETERMINISTIC_GA_AUTHORITY_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'schema': 'DeterministicPathArtifact',
            'schema_version': DETERMINISTIC_GA_SCHEMA_VERSION,
            'quantity': 'relative_energy_transport_per_m2',
            'coherent_phase': 'unavailable_not_synthesized',
        }
    ),
)


@dataclass(frozen=True)
class NativeImageSource:
    position_local_m: tuple[float, float, float]


class DeterministicImageSourceEngine(Protocol):
    engine_id: str
    engine_version: str
    candidate_source_commit: str | None
    solver_implementation_ref: ExactExternalAuthorityRef

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        ...


class HtdtPlanarImageSourceEngine:
    """Deterministic HTDT analytic kernel for bounded arbitrary-planar images."""

    engine_id = HTDT_PLANAR_ENGINE_ID
    candidate_source_commit = None

    def __init__(self, maximum_reflection_order: Literal[1, 2] = 1) -> None:
        self.maximum_reflection_order = maximum_reflection_order
        if maximum_reflection_order == 2:
            self.engine_version = HTDT_PLANAR_SECOND_ORDER_ENGINE_VERSION
            self.solver_implementation_ref = (
                HTDT_PLANAR_SECOND_ORDER_IMAGE_SOURCE_IMPLEMENTATION_REF
            )
        else:
            self.engine_version = HTDT_PLANAR_ENGINE_VERSION
            self.solver_implementation_ref = HTDT_PLANAR_IMAGE_SOURCE_IMPLEMENTATION_REF

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        del dimensions_m, source_local_m, receiver_local_m
        raise RuntimeError(
            'HTDT general-planar kernel does not construct or approximate a shoebox'
        )


class HtdtPortalDirectEngine:
    """Bounded exact engine marker for one directed Portal crossing."""

    engine_id = HTDT_PORTAL_DIRECT_ENGINE_ID
    engine_version = HTDT_PORTAL_DIRECT_ENGINE_VERSION
    candidate_source_commit = None
    solver_implementation_ref = HTDT_PORTAL_DIRECT_IMPLEMENTATION_REF

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        del dimensions_m, source_local_m, receiver_local_m
        raise RuntimeError('explicit Portal direct engine does not execute a shoebox')


class PyroomacousticsImageSourceEngine:
    """Candidate engine bridge. pyroomacoustics objects never cross this adapter."""

    engine_id = PYROOMACOUSTICS_ENGINE_ID
    engine_version = PYROOMACOUSTICS_ENGINE_VERSION
    candidate_source_commit = PYROOMACOUSTICS_CANDIDATE_SOURCE_COMMIT
    solver_implementation_ref = PYROOMACOUSTICS_SOLVER_IMPLEMENTATION_REF

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        import numpy as np
        import pyroomacoustics as pra

        installed = distribution_version('pyroomacoustics')
        if installed != self.engine_version:
            raise RuntimeError(
                f'expected pyroomacoustics {self.engine_version}, installed {installed}'
            )
        room = pra.ShoeBox(
            list(dimensions_m),
            fs=16000,
            materials=pra.Material(0.0),
            max_order=1,
        )
        room.add_source(list(source_local_m))
        room.add_microphone(list(receiver_local_m))
        room.image_source_model()
        images = np.asarray(room.sources[0].images, dtype=np.float64).T
        if images.ndim != 2 or images.shape[1] != 3:
            raise RuntimeError(
                f'unexpected pyroomacoustics image-source shape: {images.shape}'
            )
        return tuple(
            NativeImageSource(
                position_local_m=(
                    float(image[0]),
                    float(image[1]),
                    float(image[2]),
                )
            )
            for image in images
        )


def _validate_dispatch_chain(
    *,
    snapshot: AcousticSceneSnapshot,
    request: AcousticPredictionRequest,
    dispatch: AcousticSolverDispatchBinding,
    descriptor: AcousticSolverAdapterDescriptor,
    configuration: DeterministicGaConfiguration,
) -> None:
    if dispatch.state != 'READY':
        raise ValueError('deterministic GA execution requires READY dispatch')
    if request.requested_observables != ('deterministic_paths',):
        raise ValueError(
            'deterministic GA foundation requires exactly deterministic_paths observable'
        )
    if (
        request.acoustic_scene_snapshot_id != snapshot.snapshot_id
        or request.acoustic_scene_snapshot_sha256 != snapshot.semantic_sha256
    ):
        raise ValueError('GA request does not bind exact snapshot')
    if (
        dispatch.prediction_request_id != request.request_id
        or dispatch.prediction_request_semantic_sha256
        != request.request_semantic_sha256
        or dispatch.acoustic_scene_snapshot_id != snapshot.snapshot_id
        or dispatch.acoustic_scene_snapshot_sha256 != snapshot.semantic_sha256
    ):
        raise ValueError('GA dispatch/request/snapshot exact identity mismatch')
    if (
        dispatch.adapter_descriptor_id != descriptor.descriptor_id
        or dispatch.adapter_descriptor_semantic_sha256 != descriptor.semantic_sha256
    ):
        raise ValueError('GA dispatch adapter descriptor identity mismatch')
    if descriptor.adapter_id != DETERMINISTIC_GA_ADAPTER_ID:
        raise ValueError('unexpected deterministic GA adapter id')
    if descriptor.adapter_version != DETERMINISTIC_GA_ADAPTER_VERSION:
        raise ValueError('unexpected deterministic GA adapter version')
    if descriptor.acoustic_domain != 'geometric':
        raise ValueError('deterministic GA adapter descriptor must be geometric')
    if dispatch.solver_implementation_ref != descriptor.solver_implementation_ref:
        raise ValueError('GA solver implementation exact authority mismatch')
    if dispatch.solver_configuration_ref != configuration.as_external_ref():
        raise ValueError('GA solver configuration exact authority mismatch')


def _room_boundary_shell_metrics(
    compiled: R120CompiledGeometry,
    mappings: Sequence[CompiledSurfaceMapping],
) -> tuple[int, int, float]:
    triangle_indices = tuple(
        sorted(
            {
                index
                for mapping in mappings
                for index in mapping.compiled_triangle_indices
            }
        )
    )
    edge_counts: dict[tuple[int, int], int] = {}
    signed_volume_times_six = 0.0
    for index in triangle_indices:
        triangle = compiled.triangles[index]
        for left, right in (
            (triangle.a, triangle.b),
            (triangle.b, triangle.c),
            (triangle.c, triangle.a),
        ):
            key = (left, right) if left < right else (right, left)
            edge_counts[key] = edge_counts.get(key, 0) + 1
        a, b, c = _triangle_vertices(compiled, index)
        signed_volume_times_six += _dot(a, _cross(b, c))
    boundary_edge_count = sum(1 for count in edge_counts.values() if count == 1)
    non_manifold_edge_count = sum(1 for count in edge_counts.values() if count > 2)
    return (
        boundary_edge_count,
        non_manifold_edge_count,
        abs(signed_volume_times_six) / 6.0,
    )


def _surface_plane(
    compiled: R120CompiledGeometry,
    mapping: CompiledSurfaceMapping,
    *,
    tolerance_m: float,
) -> GeometricSurfacePlane:
    bounds = compiled.bounding_volume
    plane_candidates = (
        ('x', 'min', float(bounds.min_x_m)),
        ('x', 'max', float(bounds.max_x_m)),
        ('y', 'min', float(bounds.min_y_m)),
        ('y', 'max', float(bounds.max_y_m)),
        ('z', 'min', float(bounds.min_z_m)),
        ('z', 'max', float(bounds.max_z_m)),
    )
    vertex_indices: set[int] = set()
    for triangle_index in mapping.compiled_triangle_indices:
        triangle = compiled.triangles[triangle_index]
        vertex_indices.update((triangle.a, triangle.b, triangle.c))
    if not vertex_indices:
        raise ValueError(
            f'room boundary surface has no compiled triangles: {mapping.source_surface_id}'
        )

    matches: list[tuple[AxisName, PlaneSide, float]] = []
    for axis, side, coordinate in plane_candidates:
        axis_index = {'x': 0, 'y': 1, 'z': 2}[axis]
        if all(
            abs(
                (
                    compiled.vertices[index].x_m,
                    compiled.vertices[index].y_m,
                    compiled.vertices[index].z_m,
                )[axis_index]
                - coordinate
            )
            <= tolerance_m
            for index in vertex_indices
        ):
            matches.append((axis, side, coordinate))  # type: ignore[arg-type]
    if len(matches) != 1:
        raise ValueError(
            'candidate pyroomacoustics adapter supports only one exact axis-aligned '
            f'box plane per semantic room surface: {mapping.source_surface_id}'
        )
    axis, side, coordinate = matches[0]
    return GeometricSurfacePlane(
        source_surface_id=mapping.source_surface_id,
        axis=axis,
        side=side,
        coordinate_m=coordinate,
        material_authority=mapping.material_authority,
        boundary_physics_authority=mapping.boundary_physics_authority,
    )



def _general_surface_plane(
    compiled: R120CompiledGeometry,
    mapping: CompiledSurfaceMapping,
    *,
    tolerance_m: float,
) -> GeometricSurfacePlane:
    indices = tuple(sorted(mapping.compiled_triangle_indices))
    if not indices:
        raise ValueError(
            f'semantic surface has no compiled triangles: {mapping.source_surface_id}'
        )

    all_vertices: list[tuple[float, float, float]] = []
    reference_normal: tuple[float, float, float] | None = None
    for triangle_index in indices:
        triangle = _triangle_vertices(compiled, triangle_index)
        edge1 = _vector(triangle[0], triangle[1])
        edge2 = _vector(triangle[0], triangle[2])
        raw_normal = _cross(edge1, edge2)
        if _norm(raw_normal) <= tolerance_m * tolerance_m:
            raise ValueError(
                f'degenerate triangle in semantic surface {mapping.source_surface_id}'
            )
        normal = _unit(raw_normal)
        if reference_normal is None:
            for component in normal:
                if abs(component) > tolerance_m:
                    if component < 0.0:
                        normal = tuple(-value for value in normal)
                    break
            reference_normal = normal
        all_vertices.extend(triangle)

    assert reference_normal is not None
    point = min(set(all_vertices))
    for vertex in all_vertices:
        signed_distance = _dot(_vector(point, vertex), reference_normal)
        if abs(signed_distance) > tolerance_m:
            raise ValueError(
                f'nonplanar semantic surface {mapping.source_surface_id} exceeds '
                f'declared tolerance {tolerance_m}'
            )

    return GeometricSurfacePlane(
        source_surface_id=mapping.source_surface_id,
        point_m=_point(point),
        normal=_direction(reference_normal),
        compiled_triangle_indices=indices,
        material_authority=mapping.material_authority,
        boundary_physics_authority=mapping.boundary_physics_authority,
    )


def _validate_supported_topology(
    *,
    region_authority: AcousticRegionAuthority,
    portal_authority: PortalAuthority,
    boundary_termination_authority: BoundaryTerminationAuthority,
) -> None:
    if portal_authority.declaration_mode != 'explicit_none':
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_TOPOLOGY',
            'deterministic GA foundation does not simplify or traverse explicit/unknown portals',
        )
    if boundary_termination_authority.declaration_mode != 'explicit_none':
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_BOUNDARY_TERMINATION',
            'deterministic GA foundation does not simplify explicit/unknown terminations',
        )
    if len(region_authority.declarations) != 1:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_REGION_TOPOLOGY',
            'deterministic GA foundation supports exactly one explicit acoustic region',
        )

def compile_deterministic_ga_execution_input(
    *,
    snapshot: AcousticSceneSnapshot,
    request: AcousticPredictionRequest,
    dispatch: AcousticSolverDispatchBinding,
    descriptor: AcousticSolverAdapterDescriptor,
    compiled_geometry: R120CompiledGeometry,
    region_authority: AcousticRegionAuthority,
    portal_authority: PortalAuthority,
    boundary_termination_authority: BoundaryTerminationAuthority,
    directivity_datasets: Sequence[DirectivityDataset],
    configuration: DeterministicGaConfiguration,
    source_region_bindings: dict[str, str] | None = None,
    receiver_region_bindings: dict[str, str] | None = None,
) -> DeterministicGaExecutionInput:
    _validate_dispatch_chain(
        snapshot=snapshot,
        request=request,
        dispatch=dispatch,
        descriptor=descriptor,
        configuration=configuration,
    )
    if snapshot.treatment_boundary_bindings:
        raise ValueError(
            'deterministic GA foundation does not flatten attached-treatment '
            'composition into one specular surface quantity; active treatment '
            'boundary bindings require a future exact GA composition evaluator'
        )
    if (
        snapshot.r120_compiled_geometry_id != compiled_geometry.compiled_geometry_id
        or snapshot.r120_compiled_geometry_sha256
        != compiled_geometry.compiled_hash_sha256
        or snapshot.compiled_topology_sha256
        != compiled_geometry.topology_identity_sha256
    ):
        raise ValueError('GA execution R120 geometry identity mismatch')
    for expected, actual, label in (
        (compiled_geometry.region_authority_ref, _authority_ref(region_authority), 'region'),
        (compiled_geometry.portal_authority_ref, _authority_ref(portal_authority), 'portal'),
        (
            compiled_geometry.boundary_termination_authority_ref,
            _authority_ref(boundary_termination_authority),
            'boundary termination',
        ),
    ):
        if expected is None or expected != actual:
            raise ValueError(f'GA exact {label} authority mismatch')
    if configuration.room_policy == 'general_planar_multi_region_portal_v1':
        return _compile_multi_region_portal_execution_input(
            snapshot=snapshot,
            request=request,
            dispatch=dispatch,
            descriptor=descriptor,
            compiled_geometry=compiled_geometry,
            region_authority=region_authority,
            portal_authority=portal_authority,
            boundary_termination_authority=boundary_termination_authority,
            directivity_datasets=directivity_datasets,
            configuration=configuration,
            source_region_bindings=source_region_bindings,
            receiver_region_bindings=receiver_region_bindings,
        )
    _validate_supported_topology(
        region_authority=region_authority,
        portal_authority=portal_authority,
        boundary_termination_authority=boundary_termination_authority,
    )
    if not compiled_geometry.readiness.geometric_acoustics_geometry_ready:
        raise ValueError('R120 geometry is not ready for geometric acoustics')
    if compiled_geometry.approximation_operations or compiled_geometry.dropped_features:
        raise ValueError('deterministic GA foundation rejects approximated/dropped R120 geometry')
    if compiled_geometry.approximation_error_status != 'exact_preservation':
        raise ValueError('deterministic GA foundation requires exact R120 preservation')


    if snapshot.environment is None or snapshot.environment.sound_speed_m_s is None:
        raise ValueError('deterministic GA path delay requires exact sound-speed authority')
    sound_speed = float(snapshot.environment.sound_speed_m_s)

    for center in configuration.frequency_centers_hz:
        if not request.requested_frequency_domain.contains(center):
            raise ValueError(
                f'GA frequency center {center} is outside requested frequency domain'
            )

    room_mappings = tuple(
        item
        for item in compiled_geometry.surface_mapping
        if item.semantic_class == 'room_boundary'
    )
    bounds = compiled_geometry.bounding_volume
    region_surfaces = set(region_authority.declarations[0].boundary_surface_ids)
    room_mapping_by_id = {item.source_surface_id: item for item in room_mappings}
    room_surface_ids = set(room_mapping_by_id)
    if region_surfaces != room_surface_ids:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_REGION_TOPOLOGY',
            'explicit acoustic region boundary surfaces do not match exact room-boundary shell',
        )

    general_geometry = configuration.room_policy == 'general_planar_closed_polyhedral_v1'
    if configuration.maximum_reflection_order == 2 and not general_geometry:
        raise ValueError(
            'second-order deterministic GA execution requires the general-planar lane'
        )
    unsupported_reflection_surface_ids: tuple[str, ...] | None = None
    region_triangle_indices: tuple[int, ...] = ()
    region_bounds: tuple[
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
    ] | None = None

    if general_geometry:
        shell_boundary_edges, shell_non_manifold_edges, shell_volume_m3 = (
            _room_boundary_shell_metrics(
                compiled_geometry,
                tuple(room_mapping_by_id[surface_id] for surface_id in sorted(region_surfaces)),
            )
        )
        if shell_boundary_edges or shell_non_manifold_edges:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_GEOMETRY',
                'general planar GA requires the exact region room-boundary subset '
                'to be a closed manifold; holes/open edges are not filled',
            )
        if shell_volume_m3 <= configuration.geometric_tolerance_m ** 3:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_GEOMETRY',
                'general planar GA region shell is degenerate within declared tolerance',
            )

        plane_list: list[GeometricSurfacePlane] = []
        unsupported_surface_ids: list[str] = []
        for mapping in sorted(
            compiled_geometry.surface_mapping,
            key=lambda item: item.source_surface_id,
        ):
            try:
                plane_list.append(
                    _general_surface_plane(
                        compiled_geometry,
                        mapping,
                        tolerance_m=configuration.geometric_tolerance_m,
                    )
                )
            except ValueError as exc:
                if mapping.source_surface_id in region_surfaces:
                    raise DeterministicGaUnsupportedError(
                        'UNSUPPORTED_GEOMETRY',
                        f'region boundary surface is not exact planar geometry: {exc}',
                    ) from exc
                unsupported_surface_ids.append(mapping.source_surface_id)
        planes = tuple(plane_list)
        unsupported_reflection_surface_ids = tuple(sorted(unsupported_surface_ids))
        region_triangle_indices = tuple(
            sorted(
                {
                    index
                    for surface_id in region_surfaces
                    for index in room_mapping_by_id[surface_id].compiled_triangle_indices
                }
            )
        )
    else:
        planes = tuple(
            _surface_plane(
                compiled_geometry,
                item,
                tolerance_m=configuration.geometric_tolerance_m,
            )
            for item in room_mappings
        )
        plane_keys = [(item.axis, item.side) for item in planes]
        required_plane_keys = {
            ('x', 'min'),
            ('x', 'max'),
            ('y', 'min'),
            ('y', 'max'),
            ('z', 'min'),
            ('z', 'max'),
        }
        if len(planes) != 6 or set(plane_keys) != required_plane_keys:
            raise ValueError(
                'candidate pyroomacoustics adapter requires six exact semantic '
                'room-boundary surfaces, one per shoebox plane'
            )
        if len(plane_keys) != len(set(plane_keys)):
            raise ValueError('multiple semantic surfaces map to one shoebox boundary plane')

        shell_boundary_edges, shell_non_manifold_edges, shell_volume_m3 = (
            _room_boundary_shell_metrics(compiled_geometry, room_mappings)
        )
        if shell_boundary_edges or shell_non_manifold_edges:
            raise ValueError(
                'candidate pyroomacoustics adapter requires the exact semantic '
                'room-boundary subset itself to be a closed manifold; holes/open '
                'edges are not filled into a shoebox'
            )
        expected_room_volume_m3 = (
            float(bounds.max_x_m - bounds.min_x_m)
            * float(bounds.max_y_m - bounds.min_y_m)
            * float(bounds.max_z_m - bounds.min_z_m)
        )
        room_surface_area_m2 = 2.0 * (
            float(bounds.max_x_m - bounds.min_x_m)
            * float(bounds.max_y_m - bounds.min_y_m)
            + float(bounds.max_x_m - bounds.min_x_m)
            * float(bounds.max_z_m - bounds.min_z_m)
            + float(bounds.max_y_m - bounds.min_y_m)
            * float(bounds.max_z_m - bounds.min_z_m)
        )
        volume_tolerance_m3 = max(
            configuration.geometric_tolerance_m ** 3,
            room_surface_area_m2 * configuration.geometric_tolerance_m,
        )
        if abs(shell_volume_m3 - expected_room_volume_m3) > volume_tolerance_m3:
            raise ValueError(
                'semantic room-boundary shell volume does not exactly match its '
                'shoebox bounds within the declared geometric tolerance; candidate '
                'geometry must not fill or clip the R120 shell'
            )
        region_bounds = (
            (float(bounds.min_x_m), float(bounds.max_x_m)),
            (float(bounds.min_y_m), float(bounds.max_y_m)),
            (float(bounds.min_z_m), float(bounds.max_z_m)),
        )

    def require_inside_region(
        point: tuple[float, float, float],
        *,
        label: str,
        legacy_message: str,
    ) -> None:
        if general_geometry:
            membership = _region_point_membership(
                compiled_geometry,
                region_triangle_indices,
                point,
                tolerance=configuration.geometric_tolerance_m,
            )
            if membership != 'inside':
                raise DeterministicGaUnsupportedError(
                    'UNSUPPORTED_REGION_MEMBERSHIP',
                    f'{label} is not unambiguously inside the sole explicit acoustic '
                    f'region (membership={membership})',
                )
        else:
            assert region_bounds is not None
            if not _point_strictly_inside_box(
                point,
                bounds=region_bounds,
                tolerance_m=configuration.geometric_tolerance_m,
            ):
                raise ValueError(legacy_message)

    dataset_by_hash = {item.semantic_sha256: item for item in directivity_datasets}
    source_inputs: list[DeterministicGaSourceInput] = []
    for source in sorted(snapshot.sources, key=lambda item: item.source_entity_id):
        require_inside_region(
            _position_tuple(source.source_reference_point),
            label=f'source {source.source_entity_id} acoustic reference point',
            legacy_message=(
                f'source {source.source_entity_id} acoustic reference point is not '
                'strictly inside the sole explicit acoustic region; unmodeled '
                'external space is not an implicit propagation region'
            ),
        )
        if (
            source.geometric_directivity_state
            != 'SUPPORTED_FOR_GEOMETRIC_DIRECTIVITY'
        ):
            raise ValueError(
                f'source {source.source_entity_id} lacks exact geometric directivity capability'
            )
        if source.source_axis is None:
            raise ValueError(
                f'source {source.source_entity_id} lacks exact directivity reference axis'
            )
        if (
            source.directivity_dataset_id is None
            or source.directivity_dataset_version is None
            or source.directivity_dataset_sha256 is None
        ):
            raise ValueError(
                'deterministic GA foundation does not substitute omnidirectional '
                f'directivity for source {source.source_entity_id}'
            )
        dataset = dataset_by_hash.get(source.directivity_dataset_sha256)
        if dataset is None:
            raise ValueError(
                f'exact DirectivityDataset is unresolved for source {source.source_entity_id}'
            )
        if (
            dataset.dataset_id != source.directivity_dataset_id
            or dataset.version != source.directivity_dataset_version
        ):
            raise ValueError('snapshot/directivity dataset exact identity mismatch')
        source_inputs.append(
            DeterministicGaSourceInput(
                source_entity_id=source.source_entity_id,
                r110_compiled_source_sha256=source.r110_compiled_source_sha256,
                source_reference_point=source.source_reference_point,
                source_axis=source.source_axis,
                directivity_dataset_id=dataset.dataset_id,
                directivity_dataset_version=dataset.version,
                directivity_dataset_sha256=dataset.semantic_sha256,
            )
        )

    receiver_inputs_list: list[DeterministicGaReceiverInput] = []
    for item in sorted(snapshot.receivers, key=lambda item: item.receiver_id):
        require_inside_region(
            _position_tuple(item.world_position),
            label=f'receiver {item.receiver_id} position',
            legacy_message=(
                f'receiver {item.receiver_id} position is not strictly inside the '
                'sole explicit acoustic region; unmodeled external space is not '
                'an implicit propagation region'
            ),
        )
        receiver_inputs_list.append(
            DeterministicGaReceiverInput(
                receiver_id=item.receiver_id,
                entity_id=item.entity_id,
                world_position=item.world_position,
            )
        )
    receiver_inputs = tuple(receiver_inputs_list)
    if not source_inputs or not receiver_inputs:
        raise ValueError('deterministic GA execution requires source and receiver authority')

    object_triangle_indices = tuple(
        sorted(
            index
            for mapping in compiled_geometry.surface_mapping
            if mapping.semantic_class != 'room_boundary'
            for index in mapping.compiled_triangle_indices
        )
    )
    origin = Position3(
        x_m=bounds.min_x_m,
        y_m=bounds.min_y_m,
        z_m=bounds.min_z_m,
    )
    dimensions = (
        float(bounds.max_x_m - bounds.min_x_m),
        float(bounds.max_y_m - bounds.min_y_m),
        float(bounds.max_z_m - bounds.min_z_m),
    )
    if any(value <= 0.0 for value in dimensions):
        raise ValueError('R120 bounding dimensions must be positive')

    core: dict[str, Any] = {
        'schema_version': DETERMINISTIC_GA_SCHEMA_VERSION,
        'authority_version': DETERMINISTIC_GA_AUTHORITY_VERSION,
        'snapshot_id': snapshot.snapshot_id,
        'snapshot_sha256': snapshot.semantic_sha256,
        'prediction_request_id': request.request_id,
        'prediction_request_sha256': request.request_semantic_sha256,
        'dispatch_binding_id': dispatch.binding_id,
        'dispatch_binding_sha256': dispatch.semantic_sha256,
        'adapter_descriptor_id': descriptor.descriptor_id,
        'adapter_descriptor_sha256': descriptor.semantic_sha256,
        'solver_implementation_ref': dispatch.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'solver_configuration_ref': dispatch.solver_configuration_ref.model_dump(
            mode='json'
        ),
        'r120_compiled_geometry_id': compiled_geometry.compiled_geometry_id,
        'r120_compiled_geometry_sha256': compiled_geometry.compiled_hash_sha256,
        'topology_identity_sha256': compiled_geometry.topology_identity_sha256,
        'region_authority_ref': _authority_ref(region_authority).model_dump(mode='json'),
        'portal_authority_ref': _authority_ref(portal_authority).model_dump(mode='json'),
        'boundary_termination_authority_ref': _authority_ref(
            boundary_termination_authority
        ).model_dump(mode='json'),
        'room_origin_m': origin.model_dump(mode='json'),
        'room_dimensions_m': list(dimensions),
        'boundary_planes': [
            {
                key: value
                for key, value in item.model_dump(mode='json').items()
                if value is not None
            }
            for item in (
                sorted(planes, key=lambda item: item.source_surface_id)
                if general_geometry
                else sorted(planes, key=lambda item: (item.axis, item.side))
            )
        ],
        'occluder_triangle_indices': list(object_triangle_indices),
        'sources': [item.model_dump(mode='json') for item in source_inputs],
        'receivers': [item.model_dump(mode='json') for item in receiver_inputs],
        'sound_speed_m_s': sound_speed,
        'frequency_domain': request.requested_frequency_domain.model_dump(mode='json'),
        'frequency_centers_hz': list(configuration.frequency_centers_hz),
        'geometric_tolerance_m': configuration.geometric_tolerance_m,
        'engine_image_match_tolerance_m': configuration.engine_image_match_tolerance_m,
        'identity_decimal_places': configuration.identity_decimal_places,
    }
    if general_geometry:
        core['geometry_policy'] = 'general_planar_closed_polyhedral_v1'
        core['unsupported_reflection_surface_ids'] = list(
            unsupported_reflection_surface_ids or ()
        )
        if configuration.maximum_reflection_order == 2:
            core['maximum_reflection_order'] = 2
    digest = _semantic_hash(core)
    return DeterministicGaExecutionInput(
        execution_input_id=f'r150-ga-execution-input:{digest}',
        semantic_sha256=digest,
        **core,
    )


def _triangle_vertices(
    compiled: R120CompiledGeometry,
    triangle_index: int,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]:
    triangle = compiled.triangles[triangle_index]
    return (
        (
            compiled.vertices[triangle.a].x_m,
            compiled.vertices[triangle.a].y_m,
            compiled.vertices[triangle.a].z_m,
        ),
        (
            compiled.vertices[triangle.b].x_m,
            compiled.vertices[triangle.b].y_m,
            compiled.vertices[triangle.b].z_m,
        ),
        (
            compiled.vertices[triangle.c].x_m,
            compiled.vertices[triangle.c].y_m,
            compiled.vertices[triangle.c].z_m,
        ),
    )


def _segment_triangle_intersection_parameter(
    start: Sequence[float],
    end: Sequence[float],
    triangle: tuple[Sequence[float], Sequence[float], Sequence[float]],
    *,
    tolerance: float,
    distance_scaled_tolerance: bool = False,
) -> float | None:
    direction = _vector(start, end)
    edge1 = _vector(triangle[0], triangle[1])
    edge2 = _vector(triangle[0], triangle[2])
    pvec = _cross(direction, edge2)
    determinant = _dot(edge1, pvec)

    if distance_scaled_tolerance:
        segment_length = _norm(direction)
        edge1_length = _norm(edge1)
        edge2_length = _norm(edge2)
        if (
            segment_length <= tolerance
            or edge1_length <= tolerance
            or edge2_length <= tolerance
        ):
            return None
        geometry_scale = max(segment_length, edge1_length, edge2_length)
        relative_tolerance = min(0.25, tolerance / geometry_scale)
        endpoint_parameter_tolerance = min(0.25, tolerance / segment_length)
        determinant_scale = segment_length * edge1_length * edge2_length
        if abs(determinant) <= determinant_scale * relative_tolerance:
            return None
    else:
        relative_tolerance = tolerance
        endpoint_parameter_tolerance = tolerance
        if abs(determinant) <= tolerance:
            return None

    inv_det = 1.0 / determinant
    tvec = _vector(triangle[0], start)
    u = _dot(tvec, pvec) * inv_det
    if u < -relative_tolerance or u > 1.0 + relative_tolerance:
        return None
    qvec = _cross(tvec, edge1)
    v = _dot(direction, qvec) * inv_det
    if v < -relative_tolerance or u + v > 1.0 + relative_tolerance:
        return None
    t = _dot(edge2, qvec) * inv_det
    if (
        t <= endpoint_parameter_tolerance
        or t >= 1.0 - endpoint_parameter_tolerance
    ):
        return None
    return t

def _segment_blocked(
    compiled: R120CompiledGeometry,
    start: Sequence[float],
    end: Sequence[float],
    *,
    tolerance: float,
    ignored_surface_ids: frozenset[str] = frozenset(),
    distance_scaled_tolerance: bool = False,
) -> bool:
    for index, triangle in enumerate(compiled.triangles):
        if triangle.source_surface_id in ignored_surface_ids:
            continue
        hit = _segment_triangle_intersection_parameter(
            start,
            end,
            _triangle_vertices(compiled, index),
            tolerance=tolerance,
            distance_scaled_tolerance=distance_scaled_tolerance,
        )
        if hit is not None:
            return True
    return False



def _point_on_triangle_surface(
    point: Sequence[float],
    triangle: tuple[Sequence[float], Sequence[float], Sequence[float]],
    *,
    tolerance: float,
) -> bool:
    edge1 = _vector(triangle[0], triangle[1])
    edge2 = _vector(triangle[0], triangle[2])
    normal = _cross(edge1, edge2)
    normal_length = _norm(normal)
    if normal_length <= tolerance * tolerance:
        return False
    plane_distance = abs(_dot(_vector(triangle[0], point), normal)) / normal_length
    return plane_distance <= tolerance and _point_in_triangle(
        point,
        triangle,
        tolerance=tolerance,
    )


def _region_point_membership(
    compiled: R120CompiledGeometry,
    triangle_indices: Sequence[int],
    point: Sequence[float],
    *,
    tolerance: float,
) -> Literal['inside', 'outside', 'boundary', 'ambiguous']:
    if not triangle_indices:
        return 'ambiguous'
    triangles = tuple(
        _triangle_vertices(compiled, index)
        for index in triangle_indices
    )
    if any(
        _point_on_triangle_surface(point, triangle, tolerance=tolerance)
        for triangle in triangles
    ):
        return 'boundary'

    bounds = compiled.bounding_volume
    diagonal = sqrt(
        (bounds.max_x_m - bounds.min_x_m) ** 2
        + (bounds.max_y_m - bounds.min_y_m) ** 2
        + (bounds.max_z_m - bounds.min_z_m) ** 2
    )
    ray_length = max(1.0, diagonal * 4.0)
    directions = (
        _unit((1.0, 0.3713906763541037, 0.217031)),
        _unit((-0.419, 1.0, 0.163)),
        _unit((0.271, -0.337, 1.0)),
    )
    decisions: list[bool] = []
    t_tolerance = max(1.0e-12, tolerance / ray_length * 4.0)
    for direction in directions:
        end = tuple(
            float(point[index]) + ray_length * direction[index]
            for index in range(3)
        )
        hits = sorted(
            hit
            for triangle in triangles
            if (
                hit := _segment_triangle_intersection_parameter(
                    point,
                    end,
                    triangle,
                    tolerance=tolerance,
                    distance_scaled_tolerance=True,
                )
            )
            is not None
        )
        distinct_hits: list[float] = []
        for hit in hits:
            if not distinct_hits or abs(hit - distinct_hits[-1]) > t_tolerance:
                distinct_hits.append(hit)
        decisions.append(len(distinct_hits) % 2 == 1)
    if len(set(decisions)) != 1:
        return 'ambiguous'
    return 'inside' if decisions[0] else 'outside'


def _point_in_triangle(
    point: Sequence[float],
    triangle: tuple[Sequence[float], Sequence[float], Sequence[float]],
    *,
    tolerance: float,
) -> bool:
    v0 = _vector(triangle[0], triangle[2])
    v1 = _vector(triangle[0], triangle[1])
    v2 = _vector(triangle[0], point)
    dot00 = _dot(v0, v0)
    dot01 = _dot(v0, v1)
    dot02 = _dot(v0, v2)
    dot11 = _dot(v1, v1)
    dot12 = _dot(v1, v2)
    denominator = dot00 * dot11 - dot01 * dot01
    if abs(denominator) <= tolerance:
        return False
    inverse = 1.0 / denominator
    u = (dot11 * dot02 - dot01 * dot12) * inverse
    v = (dot00 * dot12 - dot01 * dot02) * inverse
    return u >= -tolerance and v >= -tolerance and u + v <= 1.0 + tolerance


def _point_on_surface(
    compiled: R120CompiledGeometry,
    surface_id: str,
    point: Sequence[float],
    *,
    tolerance: float,
) -> bool:
    mapping = next(
        item
        for item in compiled.surface_mapping
        if item.source_surface_id == surface_id
    )
    return any(
        _point_on_triangle_surface(
            point,
            _triangle_vertices(compiled, index),
            tolerance=tolerance,
        )
        for index in mapping.compiled_triangle_indices
    )


def _point_has_other_surface_contact(
    compiled: R120CompiledGeometry,
    surface_id: str,
    point: Sequence[float],
    *,
    tolerance: float,
) -> bool:
    return any(
        mapping.source_surface_id != surface_id
        and any(
            _point_on_triangle_surface(
                point,
                _triangle_vertices(compiled, index),
                tolerance=tolerance,
            )
            for index in mapping.compiled_triangle_indices
        )
        for mapping in compiled.surface_mapping
    )


def _local(
    world: Sequence[float],
    origin: Sequence[float],
) -> tuple[float, float, float]:
    return (
        float(world[0]) - float(origin[0]),
        float(world[1]) - float(origin[1]),
        float(world[2]) - float(origin[2]),
    )


def _world(
    local: Sequence[float],
    origin: Sequence[float],
) -> tuple[float, float, float]:
    return (
        float(local[0]) + float(origin[0]),
        float(local[1]) + float(origin[1]),
        float(local[2]) + float(origin[2]),
    )


def _nearest_engine_image(
    images: Sequence[NativeImageSource],
    target_local_m: Sequence[float],
    *,
    tolerance_m: float,
    label: str,
) -> tuple[float, float, float]:
    if not images:
        raise RuntimeError('candidate image-source engine returned no images')
    ranked = sorted(
        (
            (_distance(image.position_local_m, target_local_m), image.position_local_m)
            for image in images
        ),
        key=lambda item: (item[0], item[1]),
    )
    distance, position = ranked[0]
    if distance > tolerance_m:
        raise RuntimeError(
            f'candidate engine did not emit expected {label} image source; '
            f'nearest distance={distance}'
        )
    return position


def _mirror_source(
    source_world: Sequence[float],
    plane: GeometricSurfacePlane,
) -> tuple[float, float, float]:
    if plane.point_m is not None and plane.normal is not None:
        point = _position_tuple(plane.point_m)
        normal = _unit((plane.normal.x, plane.normal.y, plane.normal.z))
        signed_distance = _dot(_vector(point, source_world), normal)
        return tuple(
            float(source_world[index]) - 2.0 * signed_distance * normal[index]
            for index in range(3)
        )  # type: ignore[return-value]
    if plane.axis is None or plane.coordinate_m is None:
        raise ValueError('surface plane representation is incomplete')
    values = [float(item) for item in source_world]
    axis_index = {'x': 0, 'y': 1, 'z': 2}[plane.axis]
    values[axis_index] = 2.0 * float(plane.coordinate_m) - values[axis_index]
    return (values[0], values[1], values[2])


def _plane_point_normal(
    plane: GeometricSurfacePlane,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    if plane.point_m is not None and plane.normal is not None:
        return (
            _position_tuple(plane.point_m),
            _unit((plane.normal.x, plane.normal.y, plane.normal.z)),
        )
    if plane.axis is None or plane.coordinate_m is None:
        raise ValueError('surface plane representation is incomplete')
    axis_index = {'x': 0, 'y': 1, 'z': 2}[plane.axis]
    point = [0.0, 0.0, 0.0]
    normal = [0.0, 0.0, 0.0]
    point[axis_index] = float(plane.coordinate_m)
    normal[axis_index] = 1.0
    return (
        (point[0], point[1], point[2]),
        (normal[0], normal[1], normal[2]),
    )


def _planes_coincident(
    left: GeometricSurfacePlane,
    right: GeometricSurfacePlane,
    *,
    tolerance: float,
) -> bool:
    left_point, left_normal = _plane_point_normal(left)
    right_point, right_normal = _plane_point_normal(right)
    alignment = abs(_dot(left_normal, right_normal))
    if 1.0 - alignment > min(0.25, tolerance):
        return False
    return abs(_dot(_vector(left_point, right_point), left_normal)) <= tolerance


def _segment_grazes_plane(
    start: Sequence[float],
    end: Sequence[float],
    plane: GeometricSurfacePlane,
    *,
    tolerance: float,
) -> bool:
    segment = _vector(start, end)
    length = _norm(segment)
    if length <= tolerance:
        return True
    _, normal = _plane_point_normal(plane)
    angular_tolerance = min(0.25, tolerance / length)
    return abs(_dot(_unit(segment), normal)) <= angular_tolerance


def _reflection_point(
    image_world: Sequence[float],
    receiver_world: Sequence[float],
    plane: GeometricSurfacePlane,
    *,
    tolerance: float,
) -> tuple[float, float, float] | None:
    if plane.point_m is not None and plane.normal is not None:
        point = _position_tuple(plane.point_m)
        normal = _unit((plane.normal.x, plane.normal.y, plane.normal.z))
        denominator = _dot(_vector(image_world, receiver_world), normal)
        if abs(denominator) <= tolerance:
            return None
        t = _dot(_vector(image_world, point), normal) / denominator
    else:
        if plane.axis is None or plane.coordinate_m is None:
            raise ValueError('surface plane representation is incomplete')
        axis_index = {'x': 0, 'y': 1, 'z': 2}[plane.axis]
        denominator = float(receiver_world[axis_index]) - float(image_world[axis_index])
        if abs(denominator) <= tolerance:
            return None
        t = (float(plane.coordinate_m) - float(image_world[axis_index])) / denominator
    if t < -tolerance or t > 1.0 + tolerance:
        return None
    return tuple(
        float(image_world[index])
        + t * (float(receiver_world[index]) - float(image_world[index]))
        for index in range(3)
    )  # type: ignore[return-value]


def _second_order_reflection_points(
    source_world: Sequence[float],
    receiver_world: Sequence[float],
    first_plane: GeometricSurfacePlane,
    second_plane: GeometricSurfacePlane,
    *,
    tolerance: float,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
] | None:
    first_image = _mirror_source(source_world, first_plane)
    second_image = _mirror_source(first_image, second_plane)
    second_point = _reflection_point(
        second_image,
        receiver_world,
        second_plane,
        tolerance=tolerance,
    )
    if second_point is None:
        return None
    first_point = _reflection_point(
        first_image,
        second_point,
        first_plane,
        tolerance=tolerance,
    )
    if first_point is None:
        return None
    return first_point, second_point


def _directivity_angles(
    source_axis: Direction3,
    departure_direction: Sequence[float],
    *,
    tolerance: float,
) -> tuple[float, float]:
    forward = _unit((source_axis.x, source_axis.y, source_axis.z))
    if abs(forward[2]) > tolerance:
        raise ValueError(
            'R150 foundation directivity evaluator currently requires a horizontal '
            'explicit source axis; cabinet roll/elevated axis is not inferred'
        )
    horizontal_forward = _unit((forward[0], forward[1], 0.0))
    left = _unit((-horizontal_forward[1], horizontal_forward[0], 0.0))
    direction = _unit(departure_direction)
    horizontal_projection = sqrt(direction[0] ** 2 + direction[1] ** 2)
    elevation = degrees(atan2(direction[2], horizontal_projection))
    if horizontal_projection <= tolerance:
        horizontal = 0.0
    else:
        horizontal_direction = (
            direction[0] / horizontal_projection,
            direction[1] / horizontal_projection,
            0.0,
        )
        horizontal = degrees(
            atan2(
                _dot(horizontal_direction, left),
                _dot(horizontal_direction, horizontal_forward),
            )
        )
    return horizontal, elevation


def _directivity_contribution(
    dataset: DirectivityDataset,
    *,
    frequency_hz: float,
    source_axis: Direction3,
    departure_direction: Sequence[float],
    tolerance: float,
) -> SourceDirectivityContribution | None:
    try:
        horizontal, vertical = _directivity_angles(
            source_axis,
            departure_direction,
            tolerance=tolerance,
        )
    except ValueError:
        return None
    evaluation = evaluate_directivity(
        dataset,
        frequency_hz=frequency_hz,
        horizontal_angle_deg=horizontal,
        vertical_angle_deg=vertical,
        request='magnitude',
    )
    if (
        evaluation.decision != 'SUPPORTED'
        or evaluation.magnitude_db is None
        or evaluation.magnitude_linear is None
    ):
        return None
    return SourceDirectivityContribution(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_semantic_sha256=dataset.semantic_sha256,
        evaluation_semantic_sha256=evaluation.semantic_sha256,
        frequency_hz=frequency_hz,
        horizontal_angle_deg=horizontal,
        vertical_angle_deg=vertical,
        magnitude_db=evaluation.magnitude_db,
        magnitude_linear=evaluation.magnitude_linear,
        energy_factor=evaluation.magnitude_linear ** 2,
    )


def _material_contribution(
    authority: GeometricMaterialAuthority,
    plane: GeometricSurfacePlane,
    *,
    frequency_hz: float,
    tolerance: float,
) -> BoundaryMaterialContribution | None:
    if authority.authority_ref != plane.material_authority:
        raise ValueError('resolved material exact authority does not match surface binding')
    material = authority.material
    if material.geometric_model != 'banded':
        return None
    matches = [
        band
        for band in material.geometric_bands
        if abs(float(band.center_hz) - float(frequency_hz)) <= tolerance
    ]
    if len(matches) != 1:
        return None
    band = matches[0]
    specular = (1.0 - float(band.absorption)) * (
        1.0 - float(band.scattering)
    )
    return BoundaryMaterialContribution(
        source_surface_id=plane.source_surface_id,
        material_authority=authority.authority_ref,
        boundary_physics_authority=plane.boundary_physics_authority,
        frequency_hz=frequency_hz,
        absorption=band.absorption,
        scattering=band.scattering,
        specular_energy_factor=specular,
    )


def _make_path(
    *,
    path_type: PathType,
    source: DeterministicGaSourceInput,
    receiver: DeterministicGaReceiverInput,
    points: tuple[tuple[float, float, float], ...],
    surface_ids: tuple[str, ...],
    length_m: float,
    sound_speed_m_s: float,
    departure: Sequence[float],
    arrival: Sequence[float],
    bands: Sequence[DeterministicPathBandQuantity],
    decimals: int,
    solver_implementation_ref: ExactExternalAuthorityRef,
    typed_interactions: Sequence[DeterministicPathInteraction] | None = None,
    ordered_region_ids: Sequence[str] | None = None,
) -> DeterministicAcousticPath:
    core: dict[str, Any] = {
        'source_entity_id': source.source_entity_id,
        'receiver_id': receiver.receiver_id,
        'receiver_entity_id': receiver.entity_id,
        'path_type': path_type,
        'ordered_interaction_surface_ids': list(surface_ids),
        'ordered_interaction_points': [
            _rounded_position(point, decimals).model_dump(mode='json')
            for point in points
        ],
        'geometric_path_length_m': _round_float(length_m, decimals),
        'propagation_delay_s': _round_float(
            length_m / sound_speed_m_s,
            decimals,
        ),
        'departure_direction': _rounded_direction(
            departure,
            decimals,
        ).model_dump(mode='json'),
        'arrival_direction': _rounded_direction(
            arrival,
            decimals,
        ).model_dump(mode='json'),
        'direction_semantics': (
            'world_propagation_direction_source_out_and_receiver_in'
        ),
        'bands': [
            {
                key: value
                for key, value in item.model_dump(mode='json').items()
                if not (key == 'boundary_materials' and value is None)
            }
            for item in bands
        ],
        'adapter_id': DETERMINISTIC_GA_ADAPTER_ID,
        'adapter_version': DETERMINISTIC_GA_ADAPTER_VERSION,
        'solver_implementation_ref': solver_implementation_ref.model_dump(
            mode='json'
        ),
    }
    if typed_interactions is not None:
        core['ordered_interactions'] = [
            {
                key: value
                for key, value in item.model_dump(mode='json').items()
                if value is not None
            }
            for item in typed_interactions
        ]
    if ordered_region_ids is not None:
        core['ordered_region_ids'] = list(ordered_region_ids)
    digest = _semantic_hash(core)
    return DeterministicAcousticPath(
        path_id=f'deterministic-acoustic-path:{digest}',
        semantic_sha256=digest,
        **core,
    )


MaterialAuthorityResolver = Callable[
    [ExactExternalAuthorityRef],
    GeometricMaterialAuthority | None,
]


def execute_deterministic_ga(
    *,
    execution_input: DeterministicGaExecutionInput,
    compiled_geometry: R120CompiledGeometry,
    directivity_datasets: Sequence[DirectivityDataset],
    material_resolver: MaterialAuthorityResolver,
    engine: DeterministicImageSourceEngine,
) -> DeterministicPathArtifact:
    execution_input = DeterministicGaExecutionInput.model_validate(
        execution_input.model_dump(mode='python')
    )
    if engine.solver_implementation_ref != execution_input.solver_implementation_ref:
        raise ValueError(
            'candidate engine exact solver implementation authority does not '
            'match the READY dispatch execution input'
        )
    if (
        compiled_geometry.compiled_geometry_id
        != execution_input.r120_compiled_geometry_id
        or compiled_geometry.compiled_hash_sha256
        != execution_input.r120_compiled_geometry_sha256
        or compiled_geometry.topology_identity_sha256
        != execution_input.topology_identity_sha256
    ):
        raise ValueError('GA execution compiled geometry exact identity mismatch')

    dataset_by_hash = {item.semantic_sha256: item for item in directivity_datasets}
    origin = _position_tuple(execution_input.room_origin_m)
    general_geometry = (
        execution_input.geometry_policy == 'general_planar_closed_polyhedral_v1'
    )
    paths: list[DeterministicAcousticPath] = []
    rejected: list[RejectedPathCandidate] = []

    for source in execution_input.sources:
        dataset = dataset_by_hash.get(source.directivity_dataset_sha256)
        if dataset is None:
            raise ValueError(
                f'GA execution missing exact DirectivityDataset for {source.source_entity_id}'
            )
        if (
            dataset.dataset_id != source.directivity_dataset_id
            or dataset.version != source.directivity_dataset_version
        ):
            raise ValueError('GA execution DirectivityDataset identity mismatch')
        source_world = _position_tuple(source.source_reference_point)
        source_local = _local(source_world, origin)

        for receiver in execution_input.receivers:
            receiver_world = _position_tuple(receiver.world_position)
            receiver_local = _local(receiver_world, origin)
            images: tuple[NativeImageSource, ...] = ()
            if not general_geometry:
                images = engine.execute_shoebox(
                    dimensions_m=execution_input.room_dimensions_m,
                    source_local_m=source_local,
                    receiver_local_m=receiver_local,
                )

                _nearest_engine_image(
                    images,
                    source_local,
                    tolerance_m=execution_input.engine_image_match_tolerance_m,
                    label='direct',
                )
            direct_departure = _vector(source_world, receiver_world)
            direct_length = _distance(source_world, receiver_world)
            if _segment_blocked(
                compiled_geometry,
                source_world,
                receiver_world,
                tolerance=execution_input.geometric_tolerance_m,
                distance_scaled_tolerance=general_geometry,
            ):
                rejected.append(
                    RejectedPathCandidate(
                        source_entity_id=source.source_entity_id,
                        receiver_id=receiver.receiver_id,
                        path_type='direct',
                        decision='BLOCKED_VISIBILITY',
                        reason='exact R120 triangle surface blocks source-to-receiver segment',
                    )
                )
            else:
                direct_bands: list[DeterministicPathBandQuantity] = []
                directivity_failed = False
                for frequency_hz in execution_input.frequency_centers_hz:
                    directivity = _directivity_contribution(
                        dataset,
                        frequency_hz=frequency_hz,
                        source_axis=source.source_axis,
                        departure_direction=direct_departure,
                        tolerance=execution_input.geometric_tolerance_m,
                    )
                    if directivity is None:
                        directivity_failed = True
                        break
                    spreading = 1.0 / (direct_length * direct_length)
                    direct_bands.append(
                        DeterministicPathBandQuantity(
                            center_hz=frequency_hz,
                            spreading_factor_per_m2=spreading,
                            source_directivity=directivity,
                            relative_energy_transport_per_m2=(
                                spreading * directivity.energy_factor
                            ),
                        )
                    )
                if directivity_failed:
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='direct',
                            decision='UNSUPPORTED_DIRECTIVITY',
                            reason=(
                                'exact source directivity cannot evaluate departure '
                                'angle/frequency; no omnidirectional fallback is permitted'
                            ),
                        )
                    )
                else:
                    paths.append(
                        _make_path(
                            path_type='direct',
                            source=source,
                            receiver=receiver,
                            points=(),
                            surface_ids=(),
                            length_m=direct_length,
                            sound_speed_m_s=execution_input.sound_speed_m_s,
                            departure=direct_departure,
                            arrival=direct_departure,
                            bands=direct_bands,
                            decimals=execution_input.identity_decimal_places,
                            solver_implementation_ref=(
                                execution_input.solver_implementation_ref
                            ),
                        )
                    )

            if general_geometry:
                for surface_id in execution_input.unsupported_reflection_surface_ids or ():
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(surface_id,),
                            decision='UNSUPPORTED_GEOMETRY',
                            reason=(
                                'semantic surface is nonplanar or degenerate within the '
                                'declared general-planar tolerance; it is retained for '
                                'visibility but not silently planarized'
                            ),
                        )
                    )
            else:
                for mapping in sorted(
                    (
                        item
                        for item in compiled_geometry.surface_mapping
                        if item.semantic_class != 'room_boundary'
                    ),
                    key=lambda item: item.source_surface_id,
                ):
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(mapping.source_surface_id,),
                            decision='UNSUPPORTED_GEOMETRY',
                            reason=(
                                'candidate pyroomacoustics image-source foundation does not '
                                'silently synthesize first-order images for non-shoebox '
                                'semantic surfaces; the exact surface remains an occluder'
                            ),
                        )
                    )

            for plane in execution_input.boundary_planes:
                mirrored_world = _mirror_source(source_world, plane)
                if general_geometry:
                    image_world = mirrored_world
                else:
                    target_local = _local(mirrored_world, origin)
                    image_local = _nearest_engine_image(
                        images,
                        target_local,
                        tolerance_m=execution_input.engine_image_match_tolerance_m,
                        label=f'first reflection {plane.source_surface_id}',
                    )
                    image_world = _world(image_local, origin)
                reflection = _reflection_point(
                    image_world,
                    receiver_world,
                    plane,
                    tolerance=execution_input.geometric_tolerance_m,
                )
                if reflection is None or not _point_on_surface(
                    compiled_geometry,
                    plane.source_surface_id,
                    reflection,
                    tolerance=execution_input.geometric_tolerance_m,
                ):
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision='UNSUPPORTED_GEOMETRY',
                            reason=(
                                'candidate image source does not intersect the exact '
                                'semantic R120 surface triangle extent'
                            ),
                        )
                    )
                    continue

                ignored = (
                    frozenset()
                    if general_geometry
                    else frozenset((plane.source_surface_id,))
                )
                if _segment_blocked(
                    compiled_geometry,
                    source_world,
                    reflection,
                    tolerance=execution_input.geometric_tolerance_m,
                    ignored_surface_ids=ignored,
                    distance_scaled_tolerance=general_geometry,
                ) or _segment_blocked(
                    compiled_geometry,
                    reflection,
                    receiver_world,
                    tolerance=execution_input.geometric_tolerance_m,
                    ignored_surface_ids=ignored,
                    distance_scaled_tolerance=general_geometry,
                ):
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision='BLOCKED_VISIBILITY',
                            reason=(
                                'exact R120 triangle surface blocks one reflection segment'
                            ),
                        )
                    )
                    continue

                departure = _vector(source_world, reflection)
                arrival = _vector(reflection, receiver_world)
                path_length = _distance(source_world, reflection) + _distance(
                    reflection,
                    receiver_world,
                )
                if plane.material_authority is None:
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision='UNSUPPORTED_BOUNDARY_QUANTITY',
                            reason=(
                                'reflection surface has no exact geometric material authority'
                            ),
                        )
                    )
                    continue
                resolved_material = material_resolver(plane.material_authority)
                if (
                    resolved_material is None
                    or resolved_material.authority_ref != plane.material_authority
                ):
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision='UNSUPPORTED_BOUNDARY_QUANTITY',
                            reason='exact reflection material authority cannot be resolved',
                        )
                    )
                    continue

                reflection_bands: list[DeterministicPathBandQuantity] = []
                failure: PathCandidateDecision | None = None
                failure_reason = ''
                for frequency_hz in execution_input.frequency_centers_hz:
                    directivity = _directivity_contribution(
                        dataset,
                        frequency_hz=frequency_hz,
                        source_axis=source.source_axis,
                        departure_direction=departure,
                        tolerance=execution_input.geometric_tolerance_m,
                    )
                    if directivity is None:
                        failure = 'UNSUPPORTED_DIRECTIVITY'
                        failure_reason = (
                            'exact source directivity cannot evaluate reflected-path '
                            'departure angle/frequency'
                        )
                        break
                    boundary = _material_contribution(
                        resolved_material,
                        plane,
                        frequency_hz=frequency_hz,
                        tolerance=execution_input.geometric_tolerance_m,
                    )
                    if boundary is None:
                        failure = 'UNSUPPORTED_BOUNDARY_QUANTITY'
                        failure_reason = (
                            'surface lacks an exact matching banded absorption/scattering '
                            'quantity; no reflection coefficient/phase is fabricated'
                        )
                        break
                    spreading = 1.0 / (path_length * path_length)
                    reflection_bands.append(
                        DeterministicPathBandQuantity(
                            center_hz=frequency_hz,
                            spreading_factor_per_m2=spreading,
                            source_directivity=directivity,
                            boundary_material=boundary,
                            relative_energy_transport_per_m2=(
                                spreading
                                * directivity.energy_factor
                                * boundary.specular_energy_factor
                            ),
                        )
                    )
                if failure is not None:
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision=failure,
                            reason=failure_reason,
                        )
                    )
                    continue

                paths.append(
                    _make_path(
                        path_type='specular_reflection',
                        source=source,
                        receiver=receiver,
                        points=(reflection,),
                        surface_ids=(plane.source_surface_id,),
                        length_m=path_length,
                        sound_speed_m_s=execution_input.sound_speed_m_s,
                        departure=departure,
                        arrival=arrival,
                        bands=reflection_bands,
                        decimals=execution_input.identity_decimal_places,
                        solver_implementation_ref=execution_input.solver_implementation_ref,
                    )
                )

            if general_geometry and execution_input.maximum_reflection_order == 2:
                ordered_planes = tuple(
                    sorted(
                        execution_input.boundary_planes,
                        key=lambda item: item.source_surface_id,
                    )
                )
                for first_plane in ordered_planes:
                    for second_plane in ordered_planes:
                        surface_ids = (
                            first_plane.source_surface_id,
                            second_plane.source_surface_id,
                        )
                        if first_plane.source_surface_id == second_plane.source_surface_id:
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'same-surface immediate repeat is a degenerate '
                                        'second-order interaction and is not synthesized'
                                    ),
                                )
                            )
                            continue
                        if _planes_coincident(
                            first_plane,
                            second_plane,
                            tolerance=execution_input.geometric_tolerance_m,
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'ordered second-order planes are coincident within '
                                        'the declared geometric tolerance'
                                    ),
                                )
                            )
                            continue

                        reconstructed = _second_order_reflection_points(
                            source_world,
                            receiver_world,
                            first_plane,
                            second_plane,
                            tolerance=execution_input.geometric_tolerance_m,
                        )
                        if reconstructed is None:
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'ordered second-order image reconstruction has no '
                                        'unambiguous finite plane intersection'
                                    ),
                                )
                            )
                            continue
                        first_point, second_point = reconstructed

                        segment_lengths = (
                            _distance(source_world, first_point),
                            _distance(first_point, second_point),
                            _distance(second_point, receiver_world),
                        )
                        if any(
                            value <= execution_input.geometric_tolerance_m
                            for value in segment_lengths
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'second-order reflection points collapse or create '
                                        'a zero-length propagation segment'
                                    ),
                                )
                            )
                            continue

                        if not _point_on_surface(
                            compiled_geometry,
                            first_plane.source_surface_id,
                            first_point,
                            tolerance=execution_input.geometric_tolerance_m,
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'first second-order reflection point lies outside '
                                        'the exact semantic R120 surface triangle extent'
                                    ),
                                )
                            )
                            continue
                        if not _point_on_surface(
                            compiled_geometry,
                            second_plane.source_surface_id,
                            second_point,
                            tolerance=execution_input.geometric_tolerance_m,
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'second second-order reflection point lies outside '
                                        'the exact semantic R120 surface triangle extent'
                                    ),
                                )
                            )
                            continue

                        if _point_has_other_surface_contact(
                            compiled_geometry,
                            first_plane.source_surface_id,
                            first_point,
                            tolerance=execution_input.geometric_tolerance_m,
                        ) or _point_has_other_surface_contact(
                            compiled_geometry,
                            second_plane.source_surface_id,
                            second_point,
                            tolerance=execution_input.geometric_tolerance_m,
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'second-order reflection contact is shared-edge or '
                                        'multi-surface ambiguous within declared tolerance'
                                    ),
                                )
                            )
                            continue

                        if (
                            _segment_grazes_plane(
                                source_world,
                                first_point,
                                first_plane,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                            or _segment_grazes_plane(
                                first_point,
                                second_point,
                                first_plane,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                            or _segment_grazes_plane(
                                first_point,
                                second_point,
                                second_plane,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                            or _segment_grazes_plane(
                                second_point,
                                receiver_world,
                                second_plane,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'grazing or plane-parallel second-order contact is '
                                        'ambiguous within declared tolerance'
                                    ),
                                )
                            )
                            continue

                        if any(
                            _segment_blocked(
                                compiled_geometry,
                                segment_start,
                                segment_end,
                                tolerance=execution_input.geometric_tolerance_m,
                                distance_scaled_tolerance=True,
                            )
                            for segment_start, segment_end in (
                                (source_world, first_point),
                                (first_point, second_point),
                                (second_point, receiver_world),
                            )
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='BLOCKED_VISIBILITY',
                                    reason=(
                                        'exact R120 triangle surface blocks one of three '
                                        'ordered second-order propagation segments'
                                    ),
                                )
                            )
                            continue

                        resolved_materials: list[GeometricMaterialAuthority] = []
                        material_failure = False
                        for interaction_plane in (first_plane, second_plane):
                            if interaction_plane.material_authority is None:
                                material_failure = True
                                break
                            resolved = material_resolver(
                                interaction_plane.material_authority
                            )
                            if (
                                resolved is None
                                or resolved.authority_ref
                                != interaction_plane.material_authority
                            ):
                                material_failure = True
                                break
                            resolved_materials.append(resolved)
                        if material_failure or len(resolved_materials) != 2:
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_BOUNDARY_QUANTITY',
                                    reason=(
                                        'one or more ordered second-order surfaces lack '
                                        'an exact resolvable geometric material authority'
                                    ),
                                )
                            )
                            continue

                        departure = _vector(source_world, first_point)
                        arrival = _vector(second_point, receiver_world)
                        path_length = sum(segment_lengths)
                        second_order_bands: list[
                            DeterministicPathBandQuantity
                        ] = []
                        failure: PathCandidateDecision | None = None
                        failure_reason = ''
                        for frequency_hz in execution_input.frequency_centers_hz:
                            directivity = _directivity_contribution(
                                dataset,
                                frequency_hz=frequency_hz,
                                source_axis=source.source_axis,
                                departure_direction=departure,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                            if directivity is None:
                                failure = 'UNSUPPORTED_DIRECTIVITY'
                                failure_reason = (
                                    'exact source directivity cannot evaluate second-order '
                                    'departure angle/frequency'
                                )
                                break

                            boundary_contributions: list[
                                BoundaryMaterialContribution
                            ] = []
                            for interaction_plane, resolved in zip(
                                (first_plane, second_plane),
                                resolved_materials,
                                strict=True,
                            ):
                                boundary = _material_contribution(
                                    resolved,
                                    interaction_plane,
                                    frequency_hz=frequency_hz,
                                    tolerance=execution_input.geometric_tolerance_m,
                                )
                                if boundary is None:
                                    failure = 'UNSUPPORTED_BOUNDARY_QUANTITY'
                                    failure_reason = (
                                        'one or more second-order surfaces lack an exact '
                                        'matching banded absorption/scattering quantity; '
                                        'no reflection coefficient/phase is fabricated'
                                    )
                                    break
                                boundary_contributions.append(boundary)
                            if failure is not None:
                                break

                            spreading = 1.0 / (path_length * path_length)
                            specular_product = 1.0
                            for boundary in boundary_contributions:
                                specular_product *= boundary.specular_energy_factor
                            second_order_bands.append(
                                DeterministicPathBandQuantity(
                                    center_hz=frequency_hz,
                                    spreading_factor_per_m2=spreading,
                                    source_directivity=directivity,
                                    boundary_materials=tuple(
                                        boundary_contributions
                                    ),
                                    relative_energy_transport_per_m2=(
                                        spreading
                                        * directivity.energy_factor
                                        * specular_product
                                    ),
                                )
                            )
                        if failure is not None:
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision=failure,
                                    reason=failure_reason,
                                )
                            )
                            continue

                        paths.append(
                            _make_path(
                                path_type='specular_reflection',
                                source=source,
                                receiver=receiver,
                                points=(first_point, second_point),
                                surface_ids=surface_ids,
                                length_m=path_length,
                                sound_speed_m_s=execution_input.sound_speed_m_s,
                                departure=departure,
                                arrival=arrival,
                                bands=second_order_bands,
                                decimals=execution_input.identity_decimal_places,
                                solver_implementation_ref=(
                                    execution_input.solver_implementation_ref
                                ),
                            )
                        )

    paths.sort(
        key=lambda item: (
            item.source_entity_id,
            item.receiver_id,
            0
            if item.path_type == 'direct'
            else len(item.ordered_interaction_surface_ids),
            item.ordered_interaction_surface_ids,
            item.path_id,
        )
    )
    rejected.sort(
        key=lambda item: (
            item.source_entity_id,
            item.receiver_id,
            0
            if item.path_type == 'direct'
            else len(item.interaction_surface_ids),
            item.interaction_surface_ids,
            item.decision,
            item.reason,
        )
    )
    execution_digest = _semantic_hash(
        {
            'execution_input_id': execution_input.execution_input_id,
            'execution_input_sha256': execution_input.semantic_sha256,
            'engine_id': engine.engine_id,
            'engine_version': engine.engine_version,
            'candidate_source_commit': engine.candidate_source_commit,
        }
    )
    execution_id = f'r150-ga-execution:{execution_digest}'
    core: dict[str, Any] = {
        'schema_version': DETERMINISTIC_GA_SCHEMA_VERSION,
        'authority_version': DETERMINISTIC_GA_AUTHORITY_VERSION,
        'execution_id': execution_id,
        'execution_input_id': execution_input.execution_input_id,
        'execution_input_sha256': execution_input.semantic_sha256,
        'snapshot_id': execution_input.snapshot_id,
        'snapshot_sha256': execution_input.snapshot_sha256,
        'prediction_request_id': execution_input.prediction_request_id,
        'prediction_request_sha256': execution_input.prediction_request_sha256,
        'dispatch_binding_id': execution_input.dispatch_binding_id,
        'dispatch_binding_sha256': execution_input.dispatch_binding_sha256,
        'adapter_descriptor_id': execution_input.adapter_descriptor_id,
        'adapter_descriptor_sha256': execution_input.adapter_descriptor_sha256,
        'solver_implementation_ref': execution_input.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'solver_configuration_ref': execution_input.solver_configuration_ref.model_dump(
            mode='json'
        ),
        'r120_compiled_geometry_id': execution_input.r120_compiled_geometry_id,
        'r120_compiled_geometry_sha256': (
            execution_input.r120_compiled_geometry_sha256
        ),
        'topology_identity_sha256': execution_input.topology_identity_sha256,
        'engine_id': engine.engine_id,
        'engine_version': engine.engine_version,
        'candidate_source_commit': engine.candidate_source_commit,
        'numeric_comparison_tolerance_m': execution_input.geometric_tolerance_m,
        'identity_decimal_places': execution_input.identity_decimal_places,
        'frequency_domain': execution_input.frequency_domain.model_dump(mode='json'),
        'path_scope': (
            'direct_through_second_order_specular'
            if execution_input.maximum_reflection_order == 2
            else 'direct_and_first_order_specular'
        ),
        'coherent_phase_authority': 'UNAVAILABLE_NOT_SYNTHESIZED',
        'paths': [
            {
                **item.model_dump(mode='json'),
                'bands': [
                    {
                        key: value
                        for key, value in band.model_dump(mode='json').items()
                        if not (key == 'boundary_materials' and value is None)
                    }
                    for band in item.bands
                ],
            }
            for item in paths
        ],
        'rejected_candidates': [
            item.model_dump(mode='json') for item in rejected
        ],
    }
    digest = _semantic_hash(core)
    return DeterministicPathArtifact(
        artifact_id=f'deterministic-path-artifact:{digest}',
        semantic_sha256=digest,
        **core,
    )


def deterministic_path_observable_manifest(
    artifact: DeterministicPathArtifact,
) -> AcousticSolverObservableArtifact:
    return AcousticSolverObservableArtifact(
        observable='deterministic_paths',
        artifact_authority=artifact.as_external_ref(),
        encoding_schema_ref=DETERMINISTIC_PATH_ARTIFACT_SCHEMA_REF,
        valid_frequency_domain=artifact.frequency_domain,
    )


def build_deterministic_ga_result_envelope(
    *,
    artifact: DeterministicPathArtifact,
    dispatch: AcousticSolverDispatchBinding,
    request: AcousticPredictionRequest,
    completed_at_utc: str,
) -> AcousticSolverResultEnvelope:
    if artifact.dispatch_binding_id != dispatch.binding_id:
        raise ValueError('path artifact/dispatch identity mismatch')
    if artifact.prediction_request_id != request.request_id:
        raise ValueError('path artifact/request identity mismatch')
    return build_acoustic_solver_result_envelope(
        dispatch=dispatch,
        request=request,
        execution_id=artifact.execution_id,
        execution_provenance_ref=artifact.execution_provenance_ref(),
        artifacts=(deterministic_path_observable_manifest(artifact),),
        completed_at_utc=completed_at_utc,
    )


ConfigurationResolver = Callable[
    [ExactExternalAuthorityRef],
    DeterministicGaConfiguration | None,
]
GeometryAuthority = AcousticRegionAuthority | PortalAuthority | BoundaryTerminationAuthority
GeometryAuthorityResolver = Callable[
    [ExactExternalAuthorityRef],
    GeometryAuthority | None,
]


class CadDeterministicPathArtifactRepository:
    """Append-only typed path persistence with exact authority re-resolution."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        snapshot_repository: CadAcousticSnapshotRepository,
        dispatch_repository: CadAcousticSolverDispatchRepository,
        configuration_resolver: ConfigurationResolver,
        material_resolver: MaterialAuthorityResolver,
        geometry_authority_resolver: GeometryAuthorityResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.snapshot_repository = snapshot_repository
        self.dispatch_repository = dispatch_repository
        self.configuration_resolver = configuration_resolver
        self.material_resolver = material_resolver
        self.geometry_authority_resolver = geometry_authority_resolver
        self.path = Path(scene_repository.path)
        for label, repository in (
            ('snapshot', snapshot_repository),
            ('dispatch', dispatch_repository),
        ):
            if Path(repository.path) != self.path:
                raise ValueError(
                    f'deterministic path and {label} repositories must share one CAD database'
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_deterministic_ga_execution_inputs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_input_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    snapshot_id TEXT NOT NULL,
                    prediction_request_id TEXT NOT NULL,
                    dispatch_binding_id TEXT NOT NULL,
                    r120_compiled_geometry_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_deterministic_ga_input_request_seq
                    ON cad_deterministic_ga_execution_inputs(
                        prediction_request_id, seq ASC
                    );

                CREATE TABLE IF NOT EXISTS cad_deterministic_path_artifacts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    artifact_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    execution_id TEXT NOT NULL,
                    execution_provenance_authority_id TEXT NOT NULL,
                    execution_input_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    prediction_request_id TEXT NOT NULL,
                    dispatch_binding_id TEXT NOT NULL,
                    r120_compiled_geometry_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_deterministic_path_request_seq
                    ON cad_deterministic_path_artifacts(
                        prediction_request_id, seq ASC
                    );
                """
            )

    def _resolve_geometry_authority(
        self,
        ref: ExactExternalAuthorityRef,
        expected_type: type,
        label: str,
    ):
        authority = self.geometry_authority_resolver(ref)
        if authority is None or not isinstance(authority, expected_type):
            raise ValueError(f'{label} exact authority does not exist with expected type')
        if _authority_ref(authority) != ref:
            raise ValueError(f'{label} exact authority mismatch')
        return authority

    def _validate_execution_input(
        self,
        execution_input: DeterministicGaExecutionInput,
    ) -> DeterministicGaExecutionInput:
        execution_input = DeterministicGaExecutionInput.model_validate(
            execution_input.model_dump(mode='python')
        )
        snapshot = self.snapshot_repository.get_snapshot(execution_input.snapshot_id)
        if (
            snapshot is None
            or snapshot.semantic_sha256 != execution_input.snapshot_sha256
        ):
            raise ValueError('GA execution input exact snapshot is missing or mismatched')
        request = self.snapshot_repository.get_prediction_request(
            execution_input.prediction_request_id
        )
        if (
            request is None
            or request.request_semantic_sha256
            != execution_input.prediction_request_sha256
        ):
            raise ValueError(
                'GA execution input exact prediction request is missing or mismatched'
            )
        dispatch = self.dispatch_repository.get_dispatch(
            execution_input.dispatch_binding_id
        )
        if (
            dispatch is None
            or dispatch.semantic_sha256 != execution_input.dispatch_binding_sha256
            or dispatch.state != 'READY'
        ):
            raise ValueError(
                'GA execution input exact READY dispatch is missing or mismatched'
            )
        descriptor = self.dispatch_repository.get_descriptor(
            execution_input.adapter_descriptor_id
        )
        if (
            descriptor is None
            or descriptor.semantic_sha256 != execution_input.adapter_descriptor_sha256
        ):
            raise ValueError(
                'GA execution input exact adapter descriptor is missing or mismatched'
            )
        configuration = self.configuration_resolver(
            execution_input.solver_configuration_ref
        )
        if (
            configuration is None
            or configuration.as_external_ref()
            != execution_input.solver_configuration_ref
        ):
            raise ValueError(
                'GA execution input exact configuration is missing or mismatched'
            )
        compiled = self.snapshot_repository.r120_repository.get_compiled_geometry(
            execution_input.r120_compiled_geometry_id
        )
        if (
            compiled is None
            or compiled.compiled_hash_sha256
            != execution_input.r120_compiled_geometry_sha256
            or compiled.topology_identity_sha256
            != execution_input.topology_identity_sha256
        ):
            raise ValueError('GA execution input exact R120 geometry is missing or mismatched')
        if compiled.region_authority_ref is None:
            raise ValueError('GA execution input R120 region authority is missing')
        if compiled.portal_authority_ref is None:
            raise ValueError('GA execution input R120 portal authority is missing')
        if compiled.boundary_termination_authority_ref is None:
            raise ValueError('GA execution input R120 termination authority is missing')
        region = self._resolve_geometry_authority(
            compiled.region_authority_ref,
            AcousticRegionAuthority,
            'region',
        )
        portals = self._resolve_geometry_authority(
            compiled.portal_authority_ref,
            PortalAuthority,
            'portal',
        )
        terminations = self._resolve_geometry_authority(
            compiled.boundary_termination_authority_ref,
            BoundaryTerminationAuthority,
            'boundary termination',
        )
        datasets: list[DirectivityDataset] = []
        for source in execution_input.sources:
            dataset = (
                self.snapshot_repository.r110_repository.directivity_repository
                .get_dataset_by_hash(source.directivity_dataset_sha256)
            )
            if dataset is None:
                raise ValueError(
                    'GA execution input exact DirectivityDataset is missing'
                )
            datasets.append(dataset)
        regenerated = compile_deterministic_ga_execution_input(
            snapshot=snapshot,
            request=request,
            dispatch=dispatch,
            descriptor=descriptor,
            compiled_geometry=compiled,
            region_authority=region,
            portal_authority=portals,
            boundary_termination_authority=terminations,
            directivity_datasets=datasets,
            configuration=configuration,
            source_region_bindings={
                item.source_entity_id: item.acoustic_region_id
                for item in execution_input.sources
                if item.acoustic_region_id is not None
            } or None,
            receiver_region_bindings={
                item.receiver_id: item.acoustic_region_id
                for item in execution_input.receivers
                if item.acoustic_region_id is not None
            } or None,
        )
        if regenerated != execution_input:
            raise ValueError(
                'GA execution input does not reproduce from exact persisted authorities'
            )
        return execution_input

    def save_execution_input(
        self,
        execution_input: DeterministicGaExecutionInput,
    ) -> DeterministicGaExecutionInput:
        execution_input = self._validate_execution_input(execution_input)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_deterministic_ga_execution_inputs
                WHERE execution_input_id=?
                """,
                (execution_input.execution_input_id,),
            ).fetchone()
            if existing is not None:
                persisted = DeterministicGaExecutionInput.model_validate_json(
                    existing['payload_json']
                )
                if persisted != execution_input:
                    raise ValueError(
                        'GA execution input id exists with different semantics'
                    )
                return self._validate_execution_input(persisted)
            connection.execute(
                """
                INSERT INTO cad_deterministic_ga_execution_inputs(
                    execution_input_id,
                    semantic_sha256,
                    snapshot_id,
                    prediction_request_id,
                    dispatch_binding_id,
                    r120_compiled_geometry_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_input.execution_input_id,
                    execution_input.semantic_sha256,
                    execution_input.snapshot_id,
                    execution_input.prediction_request_id,
                    execution_input.dispatch_binding_id,
                    execution_input.r120_compiled_geometry_id,
                    execution_input.model_dump_json(),
                    _utc_now(),
                ),
            )
        return execution_input

    def get_execution_input(
        self,
        execution_input_id: str,
    ) -> DeterministicGaExecutionInput | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_deterministic_ga_execution_inputs
                WHERE execution_input_id=?
                """,
                (execution_input_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_execution_input(
            DeterministicGaExecutionInput.model_validate_json(
                row['payload_json']
            )
        )

    def _validate(
        self,
        artifact: DeterministicPathArtifact,
    ) -> DeterministicPathArtifact:
        artifact = DeterministicPathArtifact.model_validate(
            artifact.model_dump(mode='python')
        )
        execution_input = self.get_execution_input(artifact.execution_input_id)
        if (
            execution_input is None
            or execution_input.semantic_sha256 != artifact.execution_input_sha256
            or execution_input.snapshot_id != artifact.snapshot_id
            or execution_input.prediction_request_id != artifact.prediction_request_id
            or execution_input.dispatch_binding_id != artifact.dispatch_binding_id
            or execution_input.r120_compiled_geometry_id
            != artifact.r120_compiled_geometry_id
        ):
            raise ValueError(
                'path artifact exact GA execution input is missing or mismatched'
            )
        snapshot = self.snapshot_repository.get_snapshot(artifact.snapshot_id)
        if snapshot is None or snapshot.semantic_sha256 != artifact.snapshot_sha256:
            raise ValueError('path artifact exact snapshot is missing or mismatched')
        request = self.snapshot_repository.get_prediction_request(
            artifact.prediction_request_id
        )
        if (
            request is None
            or request.request_semantic_sha256 != artifact.prediction_request_sha256
        ):
            raise ValueError('path artifact exact prediction request is missing or mismatched')
        dispatch = self.dispatch_repository.get_dispatch(
            artifact.dispatch_binding_id
        )
        if (
            dispatch is None
            or dispatch.semantic_sha256 != artifact.dispatch_binding_sha256
            or dispatch.state != 'READY'
        ):
            raise ValueError('path artifact exact READY dispatch is missing or mismatched')
        descriptor = self.dispatch_repository.get_descriptor(
            artifact.adapter_descriptor_id
        )
        if (
            descriptor is None
            or descriptor.semantic_sha256 != artifact.adapter_descriptor_sha256
        ):
            raise ValueError('path artifact exact adapter descriptor is missing or mismatched')
        if (
            artifact.solver_implementation_ref != dispatch.solver_implementation_ref
            or artifact.solver_configuration_ref != dispatch.solver_configuration_ref
        ):
            raise ValueError('path artifact solver implementation/config mismatch')

        configuration = self.configuration_resolver(
            artifact.solver_configuration_ref
        )
        if (
            configuration is None
            or configuration.as_external_ref() != artifact.solver_configuration_ref
        ):
            raise ValueError('path artifact exact GA configuration is missing or mismatched')

        compiled = self.snapshot_repository.r120_repository.get_compiled_geometry(
            artifact.r120_compiled_geometry_id
        )
        if (
            compiled is None
            or compiled.compiled_hash_sha256
            != artifact.r120_compiled_geometry_sha256
            or compiled.topology_identity_sha256 != artifact.topology_identity_sha256
            or compiled.compiled_geometry_id != snapshot.r120_compiled_geometry_id
        ):
            raise ValueError('path artifact exact R120 geometry is missing or mismatched')

        if compiled.region_authority_ref is None:
            raise ValueError('path artifact R120 region authority is missing')
        if compiled.portal_authority_ref is None:
            raise ValueError('path artifact R120 portal authority is missing')
        if compiled.boundary_termination_authority_ref is None:
            raise ValueError('path artifact R120 termination authority is missing')
        self._resolve_geometry_authority(
            compiled.region_authority_ref,
            AcousticRegionAuthority,
            'region',
        )
        self._resolve_geometry_authority(
            compiled.portal_authority_ref,
            PortalAuthority,
            'portal',
        )
        self._resolve_geometry_authority(
            compiled.boundary_termination_authority_ref,
            BoundaryTerminationAuthority,
            'boundary termination',
        )

        source_by_id = {item.source_entity_id: item for item in snapshot.sources}
        receiver_by_id = {item.receiver_id: item for item in snapshot.receivers}
        for path in artifact.paths:
            source = source_by_id.get(path.source_entity_id)
            receiver = receiver_by_id.get(path.receiver_id)
            if source is None or receiver is None:
                raise ValueError('path artifact source/receiver no longer resolves')
            model = self.snapshot_repository.r110_repository.get_model(
                source.r110_compiled_source_sha256
            )
            if model is None:
                raise ValueError('path artifact exact R110 source is missing')
            if source.directivity_dataset_sha256 is None:
                raise ValueError('path artifact source DirectivityDataset is missing')
            dataset = (
                self.snapshot_repository.r110_repository.directivity_repository
                .get_dataset_by_hash(source.directivity_dataset_sha256)
            )
            if dataset is None:
                raise ValueError('path artifact exact DirectivityDataset is missing')
            for band in path.bands:
                contribution = band.source_directivity
                if contribution.dataset_semantic_sha256 != dataset.semantic_sha256:
                    raise ValueError('path directivity dataset hash mismatch')
                reevaluated = evaluate_directivity(
                    dataset,
                    frequency_hz=contribution.frequency_hz,
                    horizontal_angle_deg=contribution.horizontal_angle_deg,
                    vertical_angle_deg=contribution.vertical_angle_deg,
                    request='magnitude',
                )
                if (
                    reevaluated.decision != 'SUPPORTED'
                    or reevaluated.semantic_sha256
                    != contribution.evaluation_semantic_sha256
                    or reevaluated.magnitude_linear
                    != contribution.magnitude_linear
                ):
                    raise ValueError(
                        'path directivity contribution no longer reproduces exactly'
                    )
                boundary = band.boundary_material
                if boundary is not None:
                    material = self.material_resolver(
                        boundary.material_authority
                    )
                    if (
                        material is None
                        or material.authority_ref != boundary.material_authority
                    ):
                        raise ValueError(
                            'path boundary material exact authority is missing'
                        )
                    matching = [
                        item
                        for item in material.material.geometric_bands
                        if abs(
                            float(item.center_hz) - float(boundary.frequency_hz)
                        )
                        <= configuration.geometric_tolerance_m
                    ]
                    if len(matching) != 1:
                        raise ValueError(
                            'path boundary material band no longer resolves exactly'
                        )
                    item = matching[0]
                    expected_specular = (1.0 - item.absorption) * (
                        1.0 - item.scattering
                    )
                    if (
                        item.absorption != boundary.absorption
                        or item.scattering != boundary.scattering
                        or expected_specular != boundary.specular_energy_factor
                    ):
                        raise ValueError(
                            'path boundary material contribution no longer reproduces'
                        )

        return artifact

    def save(
        self,
        artifact: DeterministicPathArtifact,
    ) -> DeterministicPathArtifact:
        artifact = self._validate(artifact)
        provenance = artifact.execution_provenance_ref()
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_deterministic_path_artifacts
                WHERE artifact_id=?
                """,
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                persisted = DeterministicPathArtifact.model_validate_json(
                    existing['payload_json']
                )
                if persisted != artifact:
                    raise ValueError(
                        'deterministic path artifact id exists with different semantics'
                    )
                return self._validate(persisted)
            connection.execute(
                """
                INSERT INTO cad_deterministic_path_artifacts(
                    artifact_id,
                    semantic_sha256,
                    execution_id,
                    execution_provenance_authority_id,
                    execution_input_id,
                    snapshot_id,
                    prediction_request_id,
                    dispatch_binding_id,
                    r120_compiled_geometry_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.semantic_sha256,
                    artifact.execution_id,
                    provenance.authority_id,
                    artifact.execution_input_id,
                    artifact.snapshot_id,
                    artifact.prediction_request_id,
                    artifact.dispatch_binding_id,
                    artifact.r120_compiled_geometry_id,
                    artifact.model_dump_json(),
                    _utc_now(),
                ),
            )
        return artifact

    def get(self, artifact_id: str) -> DeterministicPathArtifact | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_deterministic_path_artifacts
                WHERE artifact_id=?
                """,
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            DeterministicPathArtifact.model_validate_json(row['payload_json'])
        )

    def resolve_external_authority(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> ExactExternalAuthorityRef | None:
        if ref == DETERMINISTIC_PATH_ARTIFACT_SCHEMA_REF:
            return ref
        if ref.authority_id.startswith('deterministic-path-artifact:'):
            artifact = self.get(ref.authority_id)
            if artifact is not None and artifact.as_external_ref() == ref:
                return ref
            return None
        if ref.authority_id.startswith('r150-ga-execution-provenance:'):
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM cad_deterministic_path_artifacts
                    WHERE execution_provenance_authority_id=?
                    """,
                    (ref.authority_id,),
                ).fetchone()
            if row is None:
                return None
            artifact = self._validate(
                DeterministicPathArtifact.model_validate_json(row['payload_json'])
            )
            return ref if artifact.execution_provenance_ref() == ref else None
        return None
)
    acoustic_region_id: str | None = None


class DeterministicGaReceiverInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    receiver_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    world_position: Position3


class DeterministicGaExecutionInput(BaseModel):
    """Typed exact input handed across the HTDT -> candidate-engine boundary."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = DETERMINISTIC_GA_SCHEMA_VERSION
    authority_version: Literal['r150-deterministic-ga-1'] = (
        DETERMINISTIC_GA_AUTHORITY_VERSION
    )
    execution_input_id: str = Field(
        pattern=r'^r150-ga-execution-input:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    snapshot_id: str
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prediction_request_id: str
    prediction_request_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    dispatch_binding_id: str
    dispatch_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    adapter_descriptor_id: str
    adapter_descriptor_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_implementation_ref: ExactExternalAuthorityRef
    solver_configuration_ref: ExactExternalAuthorityRef

    r120_compiled_geometry_id: str
    r120_compiled_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    region_authority_ref: ExactExternalAuthorityRef
    portal_authority_ref: ExactExternalAuthorityRef
    boundary_termination_authority_ref: ExactExternalAuthorityRef

    room_origin_m: Position3
    room_dimensions_m: tuple[float, float, float]
    boundary_planes: tuple[GeometricSurfacePlane, ...]
    geometry_policy: Literal['general_planar_closed_polyhedral_v1'] | None = None
    unsupported_reflection_surface_ids: tuple[str, ...] | None = None
    occluder_triangle_indices: tuple[int, ...]
    sources: tuple[DeterministicGaSourceInput, ...]
    receivers: tuple[DeterministicGaReceiverInput, ...]
    sound_speed_m_s: float = Field(gt=0.0)
    frequency_domain: FrequencyDomain
    frequency_centers_hz: tuple[float, ...]
    geometric_tolerance_m: float = Field(gt=0.0)
    engine_image_match_tolerance_m: float = Field(gt=0.0)
    identity_decimal_places: int = Field(ge=6, le=15)
    maximum_reflection_order: Literal[2] | None = None

    @model_validator(mode='after')
    def validate_identity(self) -> 'DeterministicGaExecutionInput':
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('DeterministicGaExecutionInput semantic hash mismatch')
        if self.execution_input_id != f'r150-ga-execution-input:{expected}':
            raise ValueError('DeterministicGaExecutionInput id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'execution_input_id', 'semantic_sha256'},
        )
        if self.geometry_policy is None:
            payload.pop('geometry_policy', None)
        if self.unsupported_reflection_surface_ids is None:
            payload.pop('unsupported_reflection_surface_ids', None)
        if self.maximum_reflection_order is None:
            payload.pop('maximum_reflection_order', None)
        for plane in payload['boundary_planes']:
            for key in (
                'axis',
                'side',
                'coordinate_m',
                'point_m',
                'normal',
                'compiled_triangle_indices',
            ):
                if plane.get(key) is None:
                    plane.pop(key, None)
        return payload


class SourceDirectivityContribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    dataset_id: str
    dataset_version: str
    dataset_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evaluation_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    frequency_hz: float = Field(gt=0.0)
    horizontal_angle_deg: float
    vertical_angle_deg: float
    magnitude_db: float
    magnitude_linear: float = Field(gt=0.0)
    energy_factor: float = Field(gt=0.0)


class BoundaryMaterialContribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_surface_id: str
    material_authority: ExactExternalAuthorityRef
    boundary_physics_authority: ExactExternalAuthorityRef | None = None
    frequency_hz: float = Field(gt=0.0)
    absorption: float = Field(ge=0.0, le=1.0)
    scattering: float = Field(ge=0.0, le=1.0)
    specular_energy_factor: float = Field(ge=0.0, le=1.0)
    coherent_reflection_phase: Literal['UNAVAILABLE'] = 'UNAVAILABLE'


class DeterministicPathBandQuantity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    band_definition: Literal['exact_center_frequency_sample'] = (
        'exact_center_frequency_sample'
    )
    center_hz: float = Field(gt=0.0)
    quantity: Literal['relative_energy_transport_per_m2'] = (
        'relative_energy_transport_per_m2'
    )
    spreading_factor_per_m2: float = Field(gt=0.0)
    source_directivity: SourceDirectivityContribution
    boundary_material: BoundaryMaterialContribution | None = None
    boundary_materials: tuple[BoundaryMaterialContribution, ...] | None = None
    relative_energy_transport_per_m2: float = Field(ge=0.0)
    coherent_phase: Literal['UNAVAILABLE_NOT_SYNTHESIZED'] = (
        'UNAVAILABLE_NOT_SYNTHESIZED'
    )


class DeterministicAcousticPath(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    path_id: str = Field(pattern=r'^deterministic-acoustic-path:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_entity_id: str
    receiver_id: str
    receiver_entity_id: str
    path_type: PathType
    ordered_interaction_surface_ids: tuple[str, ...]
    ordered_interaction_points: tuple[Position3, ...]
    geometric_path_length_m: float = Field(gt=0.0)
    propagation_delay_s: float = Field(gt=0.0)
    departure_direction: Direction3
    arrival_direction: Direction3
    direction_semantics: Literal[
        'world_propagation_direction_source_out_and_receiver_in'
    ] = 'world_propagation_direction_source_out_and_receiver_in'
    bands: tuple[DeterministicPathBandQuantity, ...] = Field(min_length=1)
    adapter_id: Literal[
        'htdt.r150.deterministic-path'
    ] = DETERMINISTIC_GA_ADAPTER_ID
    adapter_version: Literal['1'] = DETERMINISTIC_GA_ADAPTER_VERSION
    solver_implementation_ref: ExactExternalAuthorityRef

    @model_validator(mode='after')
    def validate_path(self) -> 'DeterministicAcousticPath':
        interaction_count = len(self.ordered_interaction_surface_ids)
        if interaction_count != len(self.ordered_interaction_points):
            raise ValueError('interaction surface/point sequence lengths must match')
        if self.path_type == 'direct':
            if interaction_count:
                raise ValueError('direct path cannot carry interaction surfaces')
        elif interaction_count not in (1, 2):
            raise ValueError(
                'bounded specular reflection requires one or two ordered surfaces'
            )

        for band in self.bands:
            if interaction_count == 0:
                if (
                    band.boundary_material is not None
                    or band.boundary_materials is not None
                ):
                    raise ValueError(
                        'direct path cannot carry boundary material contribution'
                    )
            elif interaction_count == 1:
                if (
                    band.boundary_material is None
                    or band.boundary_materials is not None
                ):
                    raise ValueError(
                        'first-order reflection requires exactly one boundary '
                        'material contribution'
                    )
                if (
                    band.boundary_material.source_surface_id
                    != self.ordered_interaction_surface_ids[0]
                ):
                    raise ValueError(
                        'first-order boundary material must match ordered surface identity'
                    )
            else:
                if (
                    band.boundary_material is not None
                    or band.boundary_materials is None
                ):
                    raise ValueError(
                        'second-order reflection requires ordered boundary '
                        'material contributions'
                    )
                if len(band.boundary_materials) != 2:
                    raise ValueError(
                        'second-order reflection requires exactly two material contributions'
                    )
                if tuple(
                    item.source_surface_id for item in band.boundary_materials
                ) != self.ordered_interaction_surface_ids:
                    raise ValueError(
                        'second-order material contributions must match ordered surfaces'
                    )

        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('DeterministicAcousticPath semantic hash mismatch')
        if self.path_id != f'deterministic-acoustic-path:{expected}':
            raise ValueError('DeterministicAcousticPath id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'path_id', 'semantic_sha256'},
        )
        for band in payload['bands']:
            if band.get('boundary_materials') is None:
                band.pop('boundary_materials', None)
        return payload


class RejectedPathCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_entity_id: str
    receiver_id: str
    path_type: PathType
    interaction_surface_ids: tuple[str, ...] = ()
    decision: PathCandidateDecision
    reason: str = Field(min_length=1)


class DeterministicPathArtifact(BaseModel):
    """Immutable phase-free direct/first-/second-order specular path artifact."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = DETERMINISTIC_GA_SCHEMA_VERSION
    authority_version: Literal['r150-deterministic-ga-1'] = (
        DETERMINISTIC_GA_AUTHORITY_VERSION
    )
    artifact_id: str = Field(
        pattern=r'^deterministic-path-artifact:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    execution_id: str = Field(pattern=r'^r150-ga-execution:[0-9a-f]{64}$')
    execution_input_id: str
    execution_input_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    snapshot_id: str
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prediction_request_id: str
    prediction_request_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    dispatch_binding_id: str
    dispatch_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    adapter_descriptor_id: str
    adapter_descriptor_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_implementation_ref: ExactExternalAuthorityRef
    solver_configuration_ref: ExactExternalAuthorityRef
    r120_compiled_geometry_id: str
    r120_compiled_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    engine_id: str
    engine_version: str
    candidate_source_commit: str | None = None
    numeric_comparison_tolerance_m: float = Field(gt=0.0)
    identity_decimal_places: int = Field(ge=6, le=15)
    frequency_domain: FrequencyDomain
    path_scope: Literal[
        'direct_and_first_order_specular',
        'direct_through_second_order_specular',
    ] = 'direct_and_first_order_specular'
    coherent_phase_authority: Literal['UNAVAILABLE_NOT_SYNTHESIZED'] = (
        'UNAVAILABLE_NOT_SYNTHESIZED'
    )
    paths: tuple[DeterministicAcousticPath, ...]
    rejected_candidates: tuple[RejectedPathCandidate, ...]

    @model_validator(mode='after')
    def validate_artifact(self) -> 'DeterministicPathArtifact':
        path_ids = [item.path_id for item in self.paths]
        if len(path_ids) != len(set(path_ids)):
            raise ValueError('deterministic path artifact contains duplicate path ids')
        ordering = [
            (
                item.source_entity_id,
                item.receiver_id,
                0
                if item.path_type == 'direct'
                else len(item.ordered_interaction_surface_ids),
                item.ordered_interaction_surface_ids,
                item.path_id,
            )
            for item in self.paths
        ]
        if ordering != sorted(ordering):
            raise ValueError('deterministic paths must use canonical ordering')
        rejected_order = [
            (
                item.source_entity_id,
                item.receiver_id,
                0
                if item.path_type == 'direct'
                else len(item.interaction_surface_ids),
                item.interaction_surface_ids,
                item.decision,
                item.reason,
            )
            for item in self.rejected_candidates
        ]
        if rejected_order != sorted(rejected_order):
            raise ValueError('rejected path candidates must use canonical ordering')
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('DeterministicPathArtifact semantic hash mismatch')
        if self.artifact_id != f'deterministic-path-artifact:{expected}':
            raise ValueError('DeterministicPathArtifact id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'artifact_id', 'semantic_sha256'},
        )
        for path in payload['paths']:
            for band in path['bands']:
                if band.get('boundary_materials') is None:
                    band.pop('boundary_materials', None)
        return payload

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.artifact_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )

    def execution_provenance_ref(self) -> ExactExternalAuthorityRef:
        payload = {
            'execution_id': self.execution_id,
            'execution_input_id': self.execution_input_id,
            'execution_input_sha256': self.execution_input_sha256,
            'adapter_descriptor_id': self.adapter_descriptor_id,
            'adapter_descriptor_sha256': self.adapter_descriptor_sha256,
            'solver_implementation_ref': self.solver_implementation_ref.model_dump(
                mode='json'
            ),
            'solver_configuration_ref': self.solver_configuration_ref.model_dump(
                mode='json'
            ),
            'engine_id': self.engine_id,
            'engine_version': self.engine_version,
            'candidate_source_commit': self.candidate_source_commit,
        }
        digest = _semantic_hash(payload)
        return ExactExternalAuthorityRef(
            authority_id=f'r150-ga-execution-provenance:{digest}',
            authority_version=self.authority_version,
            semantic_hash_sha256=digest,
        )


DETERMINISTIC_PATH_ARTIFACT_SCHEMA_REF = ExactExternalAuthorityRef(
    authority_id='htdt.deterministic-path-artifact.schema',
    authority_version=DETERMINISTIC_GA_AUTHORITY_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'schema': 'DeterministicPathArtifact',
            'schema_version': DETERMINISTIC_GA_SCHEMA_VERSION,
            'quantity': 'relative_energy_transport_per_m2',
            'coherent_phase': 'unavailable_not_synthesized',
        }
    ),
)


@dataclass(frozen=True)
class NativeImageSource:
    position_local_m: tuple[float, float, float]


class DeterministicImageSourceEngine(Protocol):
    engine_id: str
    engine_version: str
    candidate_source_commit: str | None
    solver_implementation_ref: ExactExternalAuthorityRef

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        ...


class HtdtPlanarImageSourceEngine:
    """Deterministic HTDT analytic kernel for bounded arbitrary-planar images."""

    engine_id = HTDT_PLANAR_ENGINE_ID
    candidate_source_commit = None

    def __init__(self, maximum_reflection_order: Literal[1, 2] = 1) -> None:
        self.maximum_reflection_order = maximum_reflection_order
        if maximum_reflection_order == 2:
            self.engine_version = HTDT_PLANAR_SECOND_ORDER_ENGINE_VERSION
            self.solver_implementation_ref = (
                HTDT_PLANAR_SECOND_ORDER_IMAGE_SOURCE_IMPLEMENTATION_REF
            )
        else:
            self.engine_version = HTDT_PLANAR_ENGINE_VERSION
            self.solver_implementation_ref = HTDT_PLANAR_IMAGE_SOURCE_IMPLEMENTATION_REF

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        del dimensions_m, source_local_m, receiver_local_m
        raise RuntimeError(
            'HTDT general-planar kernel does not construct or approximate a shoebox'
        )


class PyroomacousticsImageSourceEngine:
    """Candidate engine bridge. pyroomacoustics objects never cross this adapter."""

    engine_id = PYROOMACOUSTICS_ENGINE_ID
    engine_version = PYROOMACOUSTICS_ENGINE_VERSION
    candidate_source_commit = PYROOMACOUSTICS_CANDIDATE_SOURCE_COMMIT
    solver_implementation_ref = PYROOMACOUSTICS_SOLVER_IMPLEMENTATION_REF

    def execute_shoebox(
        self,
        *,
        dimensions_m: tuple[float, float, float],
        source_local_m: tuple[float, float, float],
        receiver_local_m: tuple[float, float, float],
    ) -> tuple[NativeImageSource, ...]:
        import numpy as np
        import pyroomacoustics as pra

        installed = distribution_version('pyroomacoustics')
        if installed != self.engine_version:
            raise RuntimeError(
                f'expected pyroomacoustics {self.engine_version}, installed {installed}'
            )
        room = pra.ShoeBox(
            list(dimensions_m),
            fs=16000,
            materials=pra.Material(0.0),
            max_order=1,
        )
        room.add_source(list(source_local_m))
        room.add_microphone(list(receiver_local_m))
        room.image_source_model()
        images = np.asarray(room.sources[0].images, dtype=np.float64).T
        if images.ndim != 2 or images.shape[1] != 3:
            raise RuntimeError(
                f'unexpected pyroomacoustics image-source shape: {images.shape}'
            )
        return tuple(
            NativeImageSource(
                position_local_m=(
                    float(image[0]),
                    float(image[1]),
                    float(image[2]),
                )
            )
            for image in images
        )


def _validate_dispatch_chain(
    *,
    snapshot: AcousticSceneSnapshot,
    request: AcousticPredictionRequest,
    dispatch: AcousticSolverDispatchBinding,
    descriptor: AcousticSolverAdapterDescriptor,
    configuration: DeterministicGaConfiguration,
) -> None:
    if dispatch.state != 'READY':
        raise ValueError('deterministic GA execution requires READY dispatch')
    if request.requested_observables != ('deterministic_paths',):
        raise ValueError(
            'deterministic GA foundation requires exactly deterministic_paths observable'
        )
    if (
        request.acoustic_scene_snapshot_id != snapshot.snapshot_id
        or request.acoustic_scene_snapshot_sha256 != snapshot.semantic_sha256
    ):
        raise ValueError('GA request does not bind exact snapshot')
    if (
        dispatch.prediction_request_id != request.request_id
        or dispatch.prediction_request_semantic_sha256
        != request.request_semantic_sha256
        or dispatch.acoustic_scene_snapshot_id != snapshot.snapshot_id
        or dispatch.acoustic_scene_snapshot_sha256 != snapshot.semantic_sha256
    ):
        raise ValueError('GA dispatch/request/snapshot exact identity mismatch')
    if (
        dispatch.adapter_descriptor_id != descriptor.descriptor_id
        or dispatch.adapter_descriptor_semantic_sha256 != descriptor.semantic_sha256
    ):
        raise ValueError('GA dispatch adapter descriptor identity mismatch')
    if descriptor.adapter_id != DETERMINISTIC_GA_ADAPTER_ID:
        raise ValueError('unexpected deterministic GA adapter id')
    if descriptor.adapter_version != DETERMINISTIC_GA_ADAPTER_VERSION:
        raise ValueError('unexpected deterministic GA adapter version')
    if descriptor.acoustic_domain != 'geometric':
        raise ValueError('deterministic GA adapter descriptor must be geometric')
    if dispatch.solver_implementation_ref != descriptor.solver_implementation_ref:
        raise ValueError('GA solver implementation exact authority mismatch')
    if dispatch.solver_configuration_ref != configuration.as_external_ref():
        raise ValueError('GA solver configuration exact authority mismatch')


def _room_boundary_shell_metrics(
    compiled: R120CompiledGeometry,
    mappings: Sequence[CompiledSurfaceMapping],
) -> tuple[int, int, float]:
    triangle_indices = tuple(
        sorted(
            {
                index
                for mapping in mappings
                for index in mapping.compiled_triangle_indices
            }
        )
    )
    edge_counts: dict[tuple[int, int], int] = {}
    signed_volume_times_six = 0.0
    for index in triangle_indices:
        triangle = compiled.triangles[index]
        for left, right in (
            (triangle.a, triangle.b),
            (triangle.b, triangle.c),
            (triangle.c, triangle.a),
        ):
            key = (left, right) if left < right else (right, left)
            edge_counts[key] = edge_counts.get(key, 0) + 1
        a, b, c = _triangle_vertices(compiled, index)
        signed_volume_times_six += _dot(a, _cross(b, c))
    boundary_edge_count = sum(1 for count in edge_counts.values() if count == 1)
    non_manifold_edge_count = sum(1 for count in edge_counts.values() if count > 2)
    return (
        boundary_edge_count,
        non_manifold_edge_count,
        abs(signed_volume_times_six) / 6.0,
    )


def _surface_plane(
    compiled: R120CompiledGeometry,
    mapping: CompiledSurfaceMapping,
    *,
    tolerance_m: float,
) -> GeometricSurfacePlane:
    bounds = compiled.bounding_volume
    plane_candidates = (
        ('x', 'min', float(bounds.min_x_m)),
        ('x', 'max', float(bounds.max_x_m)),
        ('y', 'min', float(bounds.min_y_m)),
        ('y', 'max', float(bounds.max_y_m)),
        ('z', 'min', float(bounds.min_z_m)),
        ('z', 'max', float(bounds.max_z_m)),
    )
    vertex_indices: set[int] = set()
    for triangle_index in mapping.compiled_triangle_indices:
        triangle = compiled.triangles[triangle_index]
        vertex_indices.update((triangle.a, triangle.b, triangle.c))
    if not vertex_indices:
        raise ValueError(
            f'room boundary surface has no compiled triangles: {mapping.source_surface_id}'
        )

    matches: list[tuple[AxisName, PlaneSide, float]] = []
    for axis, side, coordinate in plane_candidates:
        axis_index = {'x': 0, 'y': 1, 'z': 2}[axis]
        if all(
            abs(
                (
                    compiled.vertices[index].x_m,
                    compiled.vertices[index].y_m,
                    compiled.vertices[index].z_m,
                )[axis_index]
                - coordinate
            )
            <= tolerance_m
            for index in vertex_indices
        ):
            matches.append((axis, side, coordinate))  # type: ignore[arg-type]
    if len(matches) != 1:
        raise ValueError(
            'candidate pyroomacoustics adapter supports only one exact axis-aligned '
            f'box plane per semantic room surface: {mapping.source_surface_id}'
        )
    axis, side, coordinate = matches[0]
    return GeometricSurfacePlane(
        source_surface_id=mapping.source_surface_id,
        axis=axis,
        side=side,
        coordinate_m=coordinate,
        material_authority=mapping.material_authority,
        boundary_physics_authority=mapping.boundary_physics_authority,
    )



def _general_surface_plane(
    compiled: R120CompiledGeometry,
    mapping: CompiledSurfaceMapping,
    *,
    tolerance_m: float,
) -> GeometricSurfacePlane:
    indices = tuple(sorted(mapping.compiled_triangle_indices))
    if not indices:
        raise ValueError(
            f'semantic surface has no compiled triangles: {mapping.source_surface_id}'
        )

    all_vertices: list[tuple[float, float, float]] = []
    reference_normal: tuple[float, float, float] | None = None
    for triangle_index in indices:
        triangle = _triangle_vertices(compiled, triangle_index)
        edge1 = _vector(triangle[0], triangle[1])
        edge2 = _vector(triangle[0], triangle[2])
        raw_normal = _cross(edge1, edge2)
        if _norm(raw_normal) <= tolerance_m * tolerance_m:
            raise ValueError(
                f'degenerate triangle in semantic surface {mapping.source_surface_id}'
            )
        normal = _unit(raw_normal)
        if reference_normal is None:
            for component in normal:
                if abs(component) > tolerance_m:
                    if component < 0.0:
                        normal = tuple(-value for value in normal)
                    break
            reference_normal = normal
        all_vertices.extend(triangle)

    assert reference_normal is not None
    point = min(set(all_vertices))
    for vertex in all_vertices:
        signed_distance = _dot(_vector(point, vertex), reference_normal)
        if abs(signed_distance) > tolerance_m:
            raise ValueError(
                f'nonplanar semantic surface {mapping.source_surface_id} exceeds '
                f'declared tolerance {tolerance_m}'
            )

    return GeometricSurfacePlane(
        source_surface_id=mapping.source_surface_id,
        point_m=_point(point),
        normal=_direction(reference_normal),
        compiled_triangle_indices=indices,
        material_authority=mapping.material_authority,
        boundary_physics_authority=mapping.boundary_physics_authority,
    )


def _validate_supported_topology(
    *,
    region_authority: AcousticRegionAuthority,
    portal_authority: PortalAuthority,
    boundary_termination_authority: BoundaryTerminationAuthority,
) -> None:
    if portal_authority.declaration_mode != 'explicit_none':
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_PORTAL_TOPOLOGY',
            'deterministic GA foundation does not simplify or traverse explicit/unknown portals',
        )
    if boundary_termination_authority.declaration_mode != 'explicit_none':
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_BOUNDARY_TERMINATION',
            'deterministic GA foundation does not simplify explicit/unknown terminations',
        )
    if len(region_authority.declarations) != 1:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_REGION_TOPOLOGY',
            'deterministic GA foundation supports exactly one explicit acoustic region',
        )

def compile_deterministic_ga_execution_input(
    *,
    snapshot: AcousticSceneSnapshot,
    request: AcousticPredictionRequest,
    dispatch: AcousticSolverDispatchBinding,
    descriptor: AcousticSolverAdapterDescriptor,
    compiled_geometry: R120CompiledGeometry,
    region_authority: AcousticRegionAuthority,
    portal_authority: PortalAuthority,
    boundary_termination_authority: BoundaryTerminationAuthority,
    directivity_datasets: Sequence[DirectivityDataset],
    configuration: DeterministicGaConfiguration,
) -> DeterministicGaExecutionInput:
    _validate_dispatch_chain(
        snapshot=snapshot,
        request=request,
        dispatch=dispatch,
        descriptor=descriptor,
        configuration=configuration,
    )
    if snapshot.treatment_boundary_bindings:
        raise ValueError(
            'deterministic GA foundation does not flatten attached-treatment '
            'composition into one specular surface quantity; active treatment '
            'boundary bindings require a future exact GA composition evaluator'
        )
    if (
        snapshot.r120_compiled_geometry_id != compiled_geometry.compiled_geometry_id
        or snapshot.r120_compiled_geometry_sha256
        != compiled_geometry.compiled_hash_sha256
        or snapshot.compiled_topology_sha256
        != compiled_geometry.topology_identity_sha256
    ):
        raise ValueError('GA execution R120 geometry identity mismatch')
    for expected, actual, label in (
        (compiled_geometry.region_authority_ref, _authority_ref(region_authority), 'region'),
        (compiled_geometry.portal_authority_ref, _authority_ref(portal_authority), 'portal'),
        (
            compiled_geometry.boundary_termination_authority_ref,
            _authority_ref(boundary_termination_authority),
            'boundary termination',
        ),
    ):
        if expected is None or expected != actual:
            raise ValueError(f'GA exact {label} authority mismatch')
    _validate_supported_topology(
        region_authority=region_authority,
        portal_authority=portal_authority,
        boundary_termination_authority=boundary_termination_authority,
    )
    if not compiled_geometry.readiness.geometric_acoustics_geometry_ready:
        raise ValueError('R120 geometry is not ready for geometric acoustics')
    if compiled_geometry.approximation_operations or compiled_geometry.dropped_features:
        raise ValueError('deterministic GA foundation rejects approximated/dropped R120 geometry')
    if compiled_geometry.approximation_error_status != 'exact_preservation':
        raise ValueError('deterministic GA foundation requires exact R120 preservation')


    if snapshot.environment is None or snapshot.environment.sound_speed_m_s is None:
        raise ValueError('deterministic GA path delay requires exact sound-speed authority')
    sound_speed = float(snapshot.environment.sound_speed_m_s)

    for center in configuration.frequency_centers_hz:
        if not request.requested_frequency_domain.contains(center):
            raise ValueError(
                f'GA frequency center {center} is outside requested frequency domain'
            )

    room_mappings = tuple(
        item
        for item in compiled_geometry.surface_mapping
        if item.semantic_class == 'room_boundary'
    )
    bounds = compiled_geometry.bounding_volume
    region_surfaces = set(region_authority.declarations[0].boundary_surface_ids)
    room_mapping_by_id = {item.source_surface_id: item for item in room_mappings}
    room_surface_ids = set(room_mapping_by_id)
    if region_surfaces != room_surface_ids:
        raise DeterministicGaUnsupportedError(
            'UNSUPPORTED_REGION_TOPOLOGY',
            'explicit acoustic region boundary surfaces do not match exact room-boundary shell',
        )

    general_geometry = configuration.room_policy == 'general_planar_closed_polyhedral_v1'
    if configuration.maximum_reflection_order == 2 and not general_geometry:
        raise ValueError(
            'second-order deterministic GA execution requires the general-planar lane'
        )
    unsupported_reflection_surface_ids: tuple[str, ...] | None = None
    region_triangle_indices: tuple[int, ...] = ()
    region_bounds: tuple[
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
    ] | None = None

    if general_geometry:
        shell_boundary_edges, shell_non_manifold_edges, shell_volume_m3 = (
            _room_boundary_shell_metrics(
                compiled_geometry,
                tuple(room_mapping_by_id[surface_id] for surface_id in sorted(region_surfaces)),
            )
        )
        if shell_boundary_edges or shell_non_manifold_edges:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_GEOMETRY',
                'general planar GA requires the exact region room-boundary subset '
                'to be a closed manifold; holes/open edges are not filled',
            )
        if shell_volume_m3 <= configuration.geometric_tolerance_m ** 3:
            raise DeterministicGaUnsupportedError(
                'UNSUPPORTED_GEOMETRY',
                'general planar GA region shell is degenerate within declared tolerance',
            )

        plane_list: list[GeometricSurfacePlane] = []
        unsupported_surface_ids: list[str] = []
        for mapping in sorted(
            compiled_geometry.surface_mapping,
            key=lambda item: item.source_surface_id,
        ):
            try:
                plane_list.append(
                    _general_surface_plane(
                        compiled_geometry,
                        mapping,
                        tolerance_m=configuration.geometric_tolerance_m,
                    )
                )
            except ValueError as exc:
                if mapping.source_surface_id in region_surfaces:
                    raise DeterministicGaUnsupportedError(
                        'UNSUPPORTED_GEOMETRY',
                        f'region boundary surface is not exact planar geometry: {exc}',
                    ) from exc
                unsupported_surface_ids.append(mapping.source_surface_id)
        planes = tuple(plane_list)
        unsupported_reflection_surface_ids = tuple(sorted(unsupported_surface_ids))
        region_triangle_indices = tuple(
            sorted(
                {
                    index
                    for surface_id in region_surfaces
                    for index in room_mapping_by_id[surface_id].compiled_triangle_indices
                }
            )
        )
    else:
        planes = tuple(
            _surface_plane(
                compiled_geometry,
                item,
                tolerance_m=configuration.geometric_tolerance_m,
            )
            for item in room_mappings
        )
        plane_keys = [(item.axis, item.side) for item in planes]
        required_plane_keys = {
            ('x', 'min'),
            ('x', 'max'),
            ('y', 'min'),
            ('y', 'max'),
            ('z', 'min'),
            ('z', 'max'),
        }
        if len(planes) != 6 or set(plane_keys) != required_plane_keys:
            raise ValueError(
                'candidate pyroomacoustics adapter requires six exact semantic '
                'room-boundary surfaces, one per shoebox plane'
            )
        if len(plane_keys) != len(set(plane_keys)):
            raise ValueError('multiple semantic surfaces map to one shoebox boundary plane')

        shell_boundary_edges, shell_non_manifold_edges, shell_volume_m3 = (
            _room_boundary_shell_metrics(compiled_geometry, room_mappings)
        )
        if shell_boundary_edges or shell_non_manifold_edges:
            raise ValueError(
                'candidate pyroomacoustics adapter requires the exact semantic '
                'room-boundary subset itself to be a closed manifold; holes/open '
                'edges are not filled into a shoebox'
            )
        expected_room_volume_m3 = (
            float(bounds.max_x_m - bounds.min_x_m)
            * float(bounds.max_y_m - bounds.min_y_m)
            * float(bounds.max_z_m - bounds.min_z_m)
        )
        room_surface_area_m2 = 2.0 * (
            float(bounds.max_x_m - bounds.min_x_m)
            * float(bounds.max_y_m - bounds.min_y_m)
            + float(bounds.max_x_m - bounds.min_x_m)
            * float(bounds.max_z_m - bounds.min_z_m)
            + float(bounds.max_y_m - bounds.min_y_m)
            * float(bounds.max_z_m - bounds.min_z_m)
        )
        volume_tolerance_m3 = max(
            configuration.geometric_tolerance_m ** 3,
            room_surface_area_m2 * configuration.geometric_tolerance_m,
        )
        if abs(shell_volume_m3 - expected_room_volume_m3) > volume_tolerance_m3:
            raise ValueError(
                'semantic room-boundary shell volume does not exactly match its '
                'shoebox bounds within the declared geometric tolerance; candidate '
                'geometry must not fill or clip the R120 shell'
            )
        region_bounds = (
            (float(bounds.min_x_m), float(bounds.max_x_m)),
            (float(bounds.min_y_m), float(bounds.max_y_m)),
            (float(bounds.min_z_m), float(bounds.max_z_m)),
        )

    def require_inside_region(
        point: tuple[float, float, float],
        *,
        label: str,
        legacy_message: str,
    ) -> None:
        if general_geometry:
            membership = _region_point_membership(
                compiled_geometry,
                region_triangle_indices,
                point,
                tolerance=configuration.geometric_tolerance_m,
            )
            if membership != 'inside':
                raise DeterministicGaUnsupportedError(
                    'UNSUPPORTED_REGION_MEMBERSHIP',
                    f'{label} is not unambiguously inside the sole explicit acoustic '
                    f'region (membership={membership})',
                )
        else:
            assert region_bounds is not None
            if not _point_strictly_inside_box(
                point,
                bounds=region_bounds,
                tolerance_m=configuration.geometric_tolerance_m,
            ):
                raise ValueError(legacy_message)

    dataset_by_hash = {item.semantic_sha256: item for item in directivity_datasets}
    source_inputs: list[DeterministicGaSourceInput] = []
    for source in sorted(snapshot.sources, key=lambda item: item.source_entity_id):
        require_inside_region(
            _position_tuple(source.source_reference_point),
            label=f'source {source.source_entity_id} acoustic reference point',
            legacy_message=(
                f'source {source.source_entity_id} acoustic reference point is not '
                'strictly inside the sole explicit acoustic region; unmodeled '
                'external space is not an implicit propagation region'
            ),
        )
        if (
            source.geometric_directivity_state
            != 'SUPPORTED_FOR_GEOMETRIC_DIRECTIVITY'
        ):
            raise ValueError(
                f'source {source.source_entity_id} lacks exact geometric directivity capability'
            )
        if source.source_axis is None:
            raise ValueError(
                f'source {source.source_entity_id} lacks exact directivity reference axis'
            )
        if (
            source.directivity_dataset_id is None
            or source.directivity_dataset_version is None
            or source.directivity_dataset_sha256 is None
        ):
            raise ValueError(
                'deterministic GA foundation does not substitute omnidirectional '
                f'directivity for source {source.source_entity_id}'
            )
        dataset = dataset_by_hash.get(source.directivity_dataset_sha256)
        if dataset is None:
            raise ValueError(
                f'exact DirectivityDataset is unresolved for source {source.source_entity_id}'
            )
        if (
            dataset.dataset_id != source.directivity_dataset_id
            or dataset.version != source.directivity_dataset_version
        ):
            raise ValueError('snapshot/directivity dataset exact identity mismatch')
        source_inputs.append(
            DeterministicGaSourceInput(
                source_entity_id=source.source_entity_id,
                r110_compiled_source_sha256=source.r110_compiled_source_sha256,
                source_reference_point=source.source_reference_point,
                source_axis=source.source_axis,
                directivity_dataset_id=dataset.dataset_id,
                directivity_dataset_version=dataset.version,
                directivity_dataset_sha256=dataset.semantic_sha256,
            )
        )

    receiver_inputs_list: list[DeterministicGaReceiverInput] = []
    for item in sorted(snapshot.receivers, key=lambda item: item.receiver_id):
        require_inside_region(
            _position_tuple(item.world_position),
            label=f'receiver {item.receiver_id} position',
            legacy_message=(
                f'receiver {item.receiver_id} position is not strictly inside the '
                'sole explicit acoustic region; unmodeled external space is not '
                'an implicit propagation region'
            ),
        )
        receiver_inputs_list.append(
            DeterministicGaReceiverInput(
                receiver_id=item.receiver_id,
                entity_id=item.entity_id,
                world_position=item.world_position,
            )
        )
    receiver_inputs = tuple(receiver_inputs_list)
    if not source_inputs or not receiver_inputs:
        raise ValueError('deterministic GA execution requires source and receiver authority')

    object_triangle_indices = tuple(
        sorted(
            index
            for mapping in compiled_geometry.surface_mapping
            if mapping.semantic_class != 'room_boundary'
            for index in mapping.compiled_triangle_indices
        )
    )
    origin = Position3(
        x_m=bounds.min_x_m,
        y_m=bounds.min_y_m,
        z_m=bounds.min_z_m,
    )
    dimensions = (
        float(bounds.max_x_m - bounds.min_x_m),
        float(bounds.max_y_m - bounds.min_y_m),
        float(bounds.max_z_m - bounds.min_z_m),
    )
    if any(value <= 0.0 for value in dimensions):
        raise ValueError('R120 bounding dimensions must be positive')

    core: dict[str, Any] = {
        'schema_version': DETERMINISTIC_GA_SCHEMA_VERSION,
        'authority_version': DETERMINISTIC_GA_AUTHORITY_VERSION,
        'snapshot_id': snapshot.snapshot_id,
        'snapshot_sha256': snapshot.semantic_sha256,
        'prediction_request_id': request.request_id,
        'prediction_request_sha256': request.request_semantic_sha256,
        'dispatch_binding_id': dispatch.binding_id,
        'dispatch_binding_sha256': dispatch.semantic_sha256,
        'adapter_descriptor_id': descriptor.descriptor_id,
        'adapter_descriptor_sha256': descriptor.semantic_sha256,
        'solver_implementation_ref': dispatch.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'solver_configuration_ref': dispatch.solver_configuration_ref.model_dump(
            mode='json'
        ),
        'r120_compiled_geometry_id': compiled_geometry.compiled_geometry_id,
        'r120_compiled_geometry_sha256': compiled_geometry.compiled_hash_sha256,
        'topology_identity_sha256': compiled_geometry.topology_identity_sha256,
        'region_authority_ref': _authority_ref(region_authority).model_dump(mode='json'),
        'portal_authority_ref': _authority_ref(portal_authority).model_dump(mode='json'),
        'boundary_termination_authority_ref': _authority_ref(
            boundary_termination_authority
        ).model_dump(mode='json'),
        'room_origin_m': origin.model_dump(mode='json'),
        'room_dimensions_m': list(dimensions),
        'boundary_planes': [
            {
                key: value
                for key, value in item.model_dump(mode='json').items()
                if value is not None
            }
            for item in (
                sorted(planes, key=lambda item: item.source_surface_id)
                if general_geometry
                else sorted(planes, key=lambda item: (item.axis, item.side))
            )
        ],
        'occluder_triangle_indices': list(object_triangle_indices),
        'sources': [item.model_dump(mode='json') for item in source_inputs],
        'receivers': [item.model_dump(mode='json') for item in receiver_inputs],
        'sound_speed_m_s': sound_speed,
        'frequency_domain': request.requested_frequency_domain.model_dump(mode='json'),
        'frequency_centers_hz': list(configuration.frequency_centers_hz),
        'geometric_tolerance_m': configuration.geometric_tolerance_m,
        'engine_image_match_tolerance_m': configuration.engine_image_match_tolerance_m,
        'identity_decimal_places': configuration.identity_decimal_places,
    }
    if general_geometry:
        core['geometry_policy'] = 'general_planar_closed_polyhedral_v1'
        core['unsupported_reflection_surface_ids'] = list(
            unsupported_reflection_surface_ids or ()
        )
        if configuration.maximum_reflection_order == 2:
            core['maximum_reflection_order'] = 2
    digest = _semantic_hash(core)
    return DeterministicGaExecutionInput(
        execution_input_id=f'r150-ga-execution-input:{digest}',
        semantic_sha256=digest,
        **core,
    )


def _triangle_vertices(
    compiled: R120CompiledGeometry,
    triangle_index: int,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]:
    triangle = compiled.triangles[triangle_index]
    return (
        (
            compiled.vertices[triangle.a].x_m,
            compiled.vertices[triangle.a].y_m,
            compiled.vertices[triangle.a].z_m,
        ),
        (
            compiled.vertices[triangle.b].x_m,
            compiled.vertices[triangle.b].y_m,
            compiled.vertices[triangle.b].z_m,
        ),
        (
            compiled.vertices[triangle.c].x_m,
            compiled.vertices[triangle.c].y_m,
            compiled.vertices[triangle.c].z_m,
        ),
    )


def _segment_triangle_intersection_parameter(
    start: Sequence[float],
    end: Sequence[float],
    triangle: tuple[Sequence[float], Sequence[float], Sequence[float]],
    *,
    tolerance: float,
    distance_scaled_tolerance: bool = False,
) -> float | None:
    direction = _vector(start, end)
    edge1 = _vector(triangle[0], triangle[1])
    edge2 = _vector(triangle[0], triangle[2])
    pvec = _cross(direction, edge2)
    determinant = _dot(edge1, pvec)

    if distance_scaled_tolerance:
        segment_length = _norm(direction)
        edge1_length = _norm(edge1)
        edge2_length = _norm(edge2)
        if (
            segment_length <= tolerance
            or edge1_length <= tolerance
            or edge2_length <= tolerance
        ):
            return None
        geometry_scale = max(segment_length, edge1_length, edge2_length)
        relative_tolerance = min(0.25, tolerance / geometry_scale)
        endpoint_parameter_tolerance = min(0.25, tolerance / segment_length)
        determinant_scale = segment_length * edge1_length * edge2_length
        if abs(determinant) <= determinant_scale * relative_tolerance:
            return None
    else:
        relative_tolerance = tolerance
        endpoint_parameter_tolerance = tolerance
        if abs(determinant) <= tolerance:
            return None

    inv_det = 1.0 / determinant
    tvec = _vector(triangle[0], start)
    u = _dot(tvec, pvec) * inv_det
    if u < -relative_tolerance or u > 1.0 + relative_tolerance:
        return None
    qvec = _cross(tvec, edge1)
    v = _dot(direction, qvec) * inv_det
    if v < -relative_tolerance or u + v > 1.0 + relative_tolerance:
        return None
    t = _dot(edge2, qvec) * inv_det
    if (
        t <= endpoint_parameter_tolerance
        or t >= 1.0 - endpoint_parameter_tolerance
    ):
        return None
    return t

def _segment_blocked(
    compiled: R120CompiledGeometry,
    start: Sequence[float],
    end: Sequence[float],
    *,
    tolerance: float,
    ignored_surface_ids: frozenset[str] = frozenset(),
    distance_scaled_tolerance: bool = False,
) -> bool:
    for index, triangle in enumerate(compiled.triangles):
        if triangle.source_surface_id in ignored_surface_ids:
            continue
        hit = _segment_triangle_intersection_parameter(
            start,
            end,
            _triangle_vertices(compiled, index),
            tolerance=tolerance,
            distance_scaled_tolerance=distance_scaled_tolerance,
        )
        if hit is not None:
            return True
    return False



def _point_on_triangle_surface(
    point: Sequence[float],
    triangle: tuple[Sequence[float], Sequence[float], Sequence[float]],
    *,
    tolerance: float,
) -> bool:
    edge1 = _vector(triangle[0], triangle[1])
    edge2 = _vector(triangle[0], triangle[2])
    normal = _cross(edge1, edge2)
    normal_length = _norm(normal)
    if normal_length <= tolerance * tolerance:
        return False
    plane_distance = abs(_dot(_vector(triangle[0], point), normal)) / normal_length
    return plane_distance <= tolerance and _point_in_triangle(
        point,
        triangle,
        tolerance=tolerance,
    )


def _region_point_membership(
    compiled: R120CompiledGeometry,
    triangle_indices: Sequence[int],
    point: Sequence[float],
    *,
    tolerance: float,
) -> Literal['inside', 'outside', 'boundary', 'ambiguous']:
    if not triangle_indices:
        return 'ambiguous'
    triangles = tuple(
        _triangle_vertices(compiled, index)
        for index in triangle_indices
    )
    if any(
        _point_on_triangle_surface(point, triangle, tolerance=tolerance)
        for triangle in triangles
    ):
        return 'boundary'

    bounds = compiled.bounding_volume
    diagonal = sqrt(
        (bounds.max_x_m - bounds.min_x_m) ** 2
        + (bounds.max_y_m - bounds.min_y_m) ** 2
        + (bounds.max_z_m - bounds.min_z_m) ** 2
    )
    ray_length = max(1.0, diagonal * 4.0)
    directions = (
        _unit((1.0, 0.3713906763541037, 0.217031)),
        _unit((-0.419, 1.0, 0.163)),
        _unit((0.271, -0.337, 1.0)),
    )
    decisions: list[bool] = []
    t_tolerance = max(1.0e-12, tolerance / ray_length * 4.0)
    for direction in directions:
        end = tuple(
            float(point[index]) + ray_length * direction[index]
            for index in range(3)
        )
        hits = sorted(
            hit
            for triangle in triangles
            if (
                hit := _segment_triangle_intersection_parameter(
                    point,
                    end,
                    triangle,
                    tolerance=tolerance,
                    distance_scaled_tolerance=True,
                )
            )
            is not None
        )
        distinct_hits: list[float] = []
        for hit in hits:
            if not distinct_hits or abs(hit - distinct_hits[-1]) > t_tolerance:
                distinct_hits.append(hit)
        decisions.append(len(distinct_hits) % 2 == 1)
    if len(set(decisions)) != 1:
        return 'ambiguous'
    return 'inside' if decisions[0] else 'outside'


def _point_in_triangle(
    point: Sequence[float],
    triangle: tuple[Sequence[float], Sequence[float], Sequence[float]],
    *,
    tolerance: float,
) -> bool:
    v0 = _vector(triangle[0], triangle[2])
    v1 = _vector(triangle[0], triangle[1])
    v2 = _vector(triangle[0], point)
    dot00 = _dot(v0, v0)
    dot01 = _dot(v0, v1)
    dot02 = _dot(v0, v2)
    dot11 = _dot(v1, v1)
    dot12 = _dot(v1, v2)
    denominator = dot00 * dot11 - dot01 * dot01
    if abs(denominator) <= tolerance:
        return False
    inverse = 1.0 / denominator
    u = (dot11 * dot02 - dot01 * dot12) * inverse
    v = (dot00 * dot12 - dot01 * dot02) * inverse
    return u >= -tolerance and v >= -tolerance and u + v <= 1.0 + tolerance


def _point_on_surface(
    compiled: R120CompiledGeometry,
    surface_id: str,
    point: Sequence[float],
    *,
    tolerance: float,
) -> bool:
    mapping = next(
        item
        for item in compiled.surface_mapping
        if item.source_surface_id == surface_id
    )
    return any(
        _point_on_triangle_surface(
            point,
            _triangle_vertices(compiled, index),
            tolerance=tolerance,
        )
        for index in mapping.compiled_triangle_indices
    )


def _point_has_other_surface_contact(
    compiled: R120CompiledGeometry,
    surface_id: str,
    point: Sequence[float],
    *,
    tolerance: float,
) -> bool:
    return any(
        mapping.source_surface_id != surface_id
        and any(
            _point_on_triangle_surface(
                point,
                _triangle_vertices(compiled, index),
                tolerance=tolerance,
            )
            for index in mapping.compiled_triangle_indices
        )
        for mapping in compiled.surface_mapping
    )


def _local(
    world: Sequence[float],
    origin: Sequence[float],
) -> tuple[float, float, float]:
    return (
        float(world[0]) - float(origin[0]),
        float(world[1]) - float(origin[1]),
        float(world[2]) - float(origin[2]),
    )


def _world(
    local: Sequence[float],
    origin: Sequence[float],
) -> tuple[float, float, float]:
    return (
        float(local[0]) + float(origin[0]),
        float(local[1]) + float(origin[1]),
        float(local[2]) + float(origin[2]),
    )


def _nearest_engine_image(
    images: Sequence[NativeImageSource],
    target_local_m: Sequence[float],
    *,
    tolerance_m: float,
    label: str,
) -> tuple[float, float, float]:
    if not images:
        raise RuntimeError('candidate image-source engine returned no images')
    ranked = sorted(
        (
            (_distance(image.position_local_m, target_local_m), image.position_local_m)
            for image in images
        ),
        key=lambda item: (item[0], item[1]),
    )
    distance, position = ranked[0]
    if distance > tolerance_m:
        raise RuntimeError(
            f'candidate engine did not emit expected {label} image source; '
            f'nearest distance={distance}'
        )
    return position


def _mirror_source(
    source_world: Sequence[float],
    plane: GeometricSurfacePlane,
) -> tuple[float, float, float]:
    if plane.point_m is not None and plane.normal is not None:
        point = _position_tuple(plane.point_m)
        normal = _unit((plane.normal.x, plane.normal.y, plane.normal.z))
        signed_distance = _dot(_vector(point, source_world), normal)
        return tuple(
            float(source_world[index]) - 2.0 * signed_distance * normal[index]
            for index in range(3)
        )  # type: ignore[return-value]
    if plane.axis is None or plane.coordinate_m is None:
        raise ValueError('surface plane representation is incomplete')
    values = [float(item) for item in source_world]
    axis_index = {'x': 0, 'y': 1, 'z': 2}[plane.axis]
    values[axis_index] = 2.0 * float(plane.coordinate_m) - values[axis_index]
    return (values[0], values[1], values[2])


def _plane_point_normal(
    plane: GeometricSurfacePlane,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    if plane.point_m is not None and plane.normal is not None:
        return (
            _position_tuple(plane.point_m),
            _unit((plane.normal.x, plane.normal.y, plane.normal.z)),
        )
    if plane.axis is None or plane.coordinate_m is None:
        raise ValueError('surface plane representation is incomplete')
    axis_index = {'x': 0, 'y': 1, 'z': 2}[plane.axis]
    point = [0.0, 0.0, 0.0]
    normal = [0.0, 0.0, 0.0]
    point[axis_index] = float(plane.coordinate_m)
    normal[axis_index] = 1.0
    return (
        (point[0], point[1], point[2]),
        (normal[0], normal[1], normal[2]),
    )


def _planes_coincident(
    left: GeometricSurfacePlane,
    right: GeometricSurfacePlane,
    *,
    tolerance: float,
) -> bool:
    left_point, left_normal = _plane_point_normal(left)
    right_point, right_normal = _plane_point_normal(right)
    alignment = abs(_dot(left_normal, right_normal))
    if 1.0 - alignment > min(0.25, tolerance):
        return False
    return abs(_dot(_vector(left_point, right_point), left_normal)) <= tolerance


def _segment_grazes_plane(
    start: Sequence[float],
    end: Sequence[float],
    plane: GeometricSurfacePlane,
    *,
    tolerance: float,
) -> bool:
    segment = _vector(start, end)
    length = _norm(segment)
    if length <= tolerance:
        return True
    _, normal = _plane_point_normal(plane)
    angular_tolerance = min(0.25, tolerance / length)
    return abs(_dot(_unit(segment), normal)) <= angular_tolerance


def _reflection_point(
    image_world: Sequence[float],
    receiver_world: Sequence[float],
    plane: GeometricSurfacePlane,
    *,
    tolerance: float,
) -> tuple[float, float, float] | None:
    if plane.point_m is not None and plane.normal is not None:
        point = _position_tuple(plane.point_m)
        normal = _unit((plane.normal.x, plane.normal.y, plane.normal.z))
        denominator = _dot(_vector(image_world, receiver_world), normal)
        if abs(denominator) <= tolerance:
            return None
        t = _dot(_vector(image_world, point), normal) / denominator
    else:
        if plane.axis is None or plane.coordinate_m is None:
            raise ValueError('surface plane representation is incomplete')
        axis_index = {'x': 0, 'y': 1, 'z': 2}[plane.axis]
        denominator = float(receiver_world[axis_index]) - float(image_world[axis_index])
        if abs(denominator) <= tolerance:
            return None
        t = (float(plane.coordinate_m) - float(image_world[axis_index])) / denominator
    if t < -tolerance or t > 1.0 + tolerance:
        return None
    return tuple(
        float(image_world[index])
        + t * (float(receiver_world[index]) - float(image_world[index]))
        for index in range(3)
    )  # type: ignore[return-value]


def _second_order_reflection_points(
    source_world: Sequence[float],
    receiver_world: Sequence[float],
    first_plane: GeometricSurfacePlane,
    second_plane: GeometricSurfacePlane,
    *,
    tolerance: float,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
] | None:
    first_image = _mirror_source(source_world, first_plane)
    second_image = _mirror_source(first_image, second_plane)
    second_point = _reflection_point(
        second_image,
        receiver_world,
        second_plane,
        tolerance=tolerance,
    )
    if second_point is None:
        return None
    first_point = _reflection_point(
        first_image,
        second_point,
        first_plane,
        tolerance=tolerance,
    )
    if first_point is None:
        return None
    return first_point, second_point


def _directivity_angles(
    source_axis: Direction3,
    departure_direction: Sequence[float],
    *,
    tolerance: float,
) -> tuple[float, float]:
    forward = _unit((source_axis.x, source_axis.y, source_axis.z))
    if abs(forward[2]) > tolerance:
        raise ValueError(
            'R150 foundation directivity evaluator currently requires a horizontal '
            'explicit source axis; cabinet roll/elevated axis is not inferred'
        )
    horizontal_forward = _unit((forward[0], forward[1], 0.0))
    left = _unit((-horizontal_forward[1], horizontal_forward[0], 0.0))
    direction = _unit(departure_direction)
    horizontal_projection = sqrt(direction[0] ** 2 + direction[1] ** 2)
    elevation = degrees(atan2(direction[2], horizontal_projection))
    if horizontal_projection <= tolerance:
        horizontal = 0.0
    else:
        horizontal_direction = (
            direction[0] / horizontal_projection,
            direction[1] / horizontal_projection,
            0.0,
        )
        horizontal = degrees(
            atan2(
                _dot(horizontal_direction, left),
                _dot(horizontal_direction, horizontal_forward),
            )
        )
    return horizontal, elevation


def _directivity_contribution(
    dataset: DirectivityDataset,
    *,
    frequency_hz: float,
    source_axis: Direction3,
    departure_direction: Sequence[float],
    tolerance: float,
) -> SourceDirectivityContribution | None:
    try:
        horizontal, vertical = _directivity_angles(
            source_axis,
            departure_direction,
            tolerance=tolerance,
        )
    except ValueError:
        return None
    evaluation = evaluate_directivity(
        dataset,
        frequency_hz=frequency_hz,
        horizontal_angle_deg=horizontal,
        vertical_angle_deg=vertical,
        request='magnitude',
    )
    if (
        evaluation.decision != 'SUPPORTED'
        or evaluation.magnitude_db is None
        or evaluation.magnitude_linear is None
    ):
        return None
    return SourceDirectivityContribution(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_semantic_sha256=dataset.semantic_sha256,
        evaluation_semantic_sha256=evaluation.semantic_sha256,
        frequency_hz=frequency_hz,
        horizontal_angle_deg=horizontal,
        vertical_angle_deg=vertical,
        magnitude_db=evaluation.magnitude_db,
        magnitude_linear=evaluation.magnitude_linear,
        energy_factor=evaluation.magnitude_linear ** 2,
    )


def _material_contribution(
    authority: GeometricMaterialAuthority,
    plane: GeometricSurfacePlane,
    *,
    frequency_hz: float,
    tolerance: float,
) -> BoundaryMaterialContribution | None:
    if authority.authority_ref != plane.material_authority:
        raise ValueError('resolved material exact authority does not match surface binding')
    material = authority.material
    if material.geometric_model != 'banded':
        return None
    matches = [
        band
        for band in material.geometric_bands
        if abs(float(band.center_hz) - float(frequency_hz)) <= tolerance
    ]
    if len(matches) != 1:
        return None
    band = matches[0]
    specular = (1.0 - float(band.absorption)) * (
        1.0 - float(band.scattering)
    )
    return BoundaryMaterialContribution(
        source_surface_id=plane.source_surface_id,
        material_authority=authority.authority_ref,
        boundary_physics_authority=plane.boundary_physics_authority,
        frequency_hz=frequency_hz,
        absorption=band.absorption,
        scattering=band.scattering,
        specular_energy_factor=specular,
    )


def _make_path(
    *,
    path_type: PathType,
    source: DeterministicGaSourceInput,
    receiver: DeterministicGaReceiverInput,
    points: tuple[tuple[float, float, float], ...],
    surface_ids: tuple[str, ...],
    length_m: float,
    sound_speed_m_s: float,
    departure: Sequence[float],
    arrival: Sequence[float],
    bands: Sequence[DeterministicPathBandQuantity],
    decimals: int,
    solver_implementation_ref: ExactExternalAuthorityRef,
) -> DeterministicAcousticPath:
    core: dict[str, Any] = {
        'source_entity_id': source.source_entity_id,
        'receiver_id': receiver.receiver_id,
        'receiver_entity_id': receiver.entity_id,
        'path_type': path_type,
        'ordered_interaction_surface_ids': list(surface_ids),
        'ordered_interaction_points': [
            _rounded_position(point, decimals).model_dump(mode='json')
            for point in points
        ],
        'geometric_path_length_m': _round_float(length_m, decimals),
        'propagation_delay_s': _round_float(
            length_m / sound_speed_m_s,
            decimals,
        ),
        'departure_direction': _rounded_direction(
            departure,
            decimals,
        ).model_dump(mode='json'),
        'arrival_direction': _rounded_direction(
            arrival,
            decimals,
        ).model_dump(mode='json'),
        'direction_semantics': (
            'world_propagation_direction_source_out_and_receiver_in'
        ),
        'bands': [
            {
                key: value
                for key, value in item.model_dump(mode='json').items()
                if not (key == 'boundary_materials' and value is None)
            }
            for item in bands
        ],
        'adapter_id': DETERMINISTIC_GA_ADAPTER_ID,
        'adapter_version': DETERMINISTIC_GA_ADAPTER_VERSION,
        'solver_implementation_ref': solver_implementation_ref.model_dump(
            mode='json'
        ),
    }
    digest = _semantic_hash(core)
    return DeterministicAcousticPath(
        path_id=f'deterministic-acoustic-path:{digest}',
        semantic_sha256=digest,
        **core,
    )


MaterialAuthorityResolver = Callable[
    [ExactExternalAuthorityRef],
    GeometricMaterialAuthority | None,
]


def execute_deterministic_ga(
    *,
    execution_input: DeterministicGaExecutionInput,
    compiled_geometry: R120CompiledGeometry,
    directivity_datasets: Sequence[DirectivityDataset],
    material_resolver: MaterialAuthorityResolver,
    engine: DeterministicImageSourceEngine,
) -> DeterministicPathArtifact:
    execution_input = DeterministicGaExecutionInput.model_validate(
        execution_input.model_dump(mode='python')
    )
    if engine.solver_implementation_ref != execution_input.solver_implementation_ref:
        raise ValueError(
            'candidate engine exact solver implementation authority does not '
            'match the READY dispatch execution input'
        )
    if (
        compiled_geometry.compiled_geometry_id
        != execution_input.r120_compiled_geometry_id
        or compiled_geometry.compiled_hash_sha256
        != execution_input.r120_compiled_geometry_sha256
        or compiled_geometry.topology_identity_sha256
        != execution_input.topology_identity_sha256
    ):
        raise ValueError('GA execution compiled geometry exact identity mismatch')

    dataset_by_hash = {item.semantic_sha256: item for item in directivity_datasets}
    origin = _position_tuple(execution_input.room_origin_m)
    general_geometry = (
        execution_input.geometry_policy == 'general_planar_closed_polyhedral_v1'
    )
    paths: list[DeterministicAcousticPath] = []
    rejected: list[RejectedPathCandidate] = []

    for source in execution_input.sources:
        dataset = dataset_by_hash.get(source.directivity_dataset_sha256)
        if dataset is None:
            raise ValueError(
                f'GA execution missing exact DirectivityDataset for {source.source_entity_id}'
            )
        if (
            dataset.dataset_id != source.directivity_dataset_id
            or dataset.version != source.directivity_dataset_version
        ):
            raise ValueError('GA execution DirectivityDataset identity mismatch')
        source_world = _position_tuple(source.source_reference_point)
        source_local = _local(source_world, origin)

        for receiver in execution_input.receivers:
            receiver_world = _position_tuple(receiver.world_position)
            receiver_local = _local(receiver_world, origin)
            images: tuple[NativeImageSource, ...] = ()
            if not general_geometry:
                images = engine.execute_shoebox(
                    dimensions_m=execution_input.room_dimensions_m,
                    source_local_m=source_local,
                    receiver_local_m=receiver_local,
                )

                _nearest_engine_image(
                    images,
                    source_local,
                    tolerance_m=execution_input.engine_image_match_tolerance_m,
                    label='direct',
                )
            direct_departure = _vector(source_world, receiver_world)
            direct_length = _distance(source_world, receiver_world)
            if _segment_blocked(
                compiled_geometry,
                source_world,
                receiver_world,
                tolerance=execution_input.geometric_tolerance_m,
                distance_scaled_tolerance=general_geometry,
            ):
                rejected.append(
                    RejectedPathCandidate(
                        source_entity_id=source.source_entity_id,
                        receiver_id=receiver.receiver_id,
                        path_type='direct',
                        decision='BLOCKED_VISIBILITY',
                        reason='exact R120 triangle surface blocks source-to-receiver segment',
                    )
                )
            else:
                direct_bands: list[DeterministicPathBandQuantity] = []
                directivity_failed = False
                for frequency_hz in execution_input.frequency_centers_hz:
                    directivity = _directivity_contribution(
                        dataset,
                        frequency_hz=frequency_hz,
                        source_axis=source.source_axis,
                        departure_direction=direct_departure,
                        tolerance=execution_input.geometric_tolerance_m,
                    )
                    if directivity is None:
                        directivity_failed = True
                        break
                    spreading = 1.0 / (direct_length * direct_length)
                    direct_bands.append(
                        DeterministicPathBandQuantity(
                            center_hz=frequency_hz,
                            spreading_factor_per_m2=spreading,
                            source_directivity=directivity,
                            relative_energy_transport_per_m2=(
                                spreading * directivity.energy_factor
                            ),
                        )
                    )
                if directivity_failed:
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='direct',
                            decision='UNSUPPORTED_DIRECTIVITY',
                            reason=(
                                'exact source directivity cannot evaluate departure '
                                'angle/frequency; no omnidirectional fallback is permitted'
                            ),
                        )
                    )
                else:
                    paths.append(
                        _make_path(
                            path_type='direct',
                            source=source,
                            receiver=receiver,
                            points=(),
                            surface_ids=(),
                            length_m=direct_length,
                            sound_speed_m_s=execution_input.sound_speed_m_s,
                            departure=direct_departure,
                            arrival=direct_departure,
                            bands=direct_bands,
                            decimals=execution_input.identity_decimal_places,
                            solver_implementation_ref=(
                                execution_input.solver_implementation_ref
                            ),
                        )
                    )

            if general_geometry:
                for surface_id in execution_input.unsupported_reflection_surface_ids or ():
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(surface_id,),
                            decision='UNSUPPORTED_GEOMETRY',
                            reason=(
                                'semantic surface is nonplanar or degenerate within the '
                                'declared general-planar tolerance; it is retained for '
                                'visibility but not silently planarized'
                            ),
                        )
                    )
            else:
                for mapping in sorted(
                    (
                        item
                        for item in compiled_geometry.surface_mapping
                        if item.semantic_class != 'room_boundary'
                    ),
                    key=lambda item: item.source_surface_id,
                ):
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(mapping.source_surface_id,),
                            decision='UNSUPPORTED_GEOMETRY',
                            reason=(
                                'candidate pyroomacoustics image-source foundation does not '
                                'silently synthesize first-order images for non-shoebox '
                                'semantic surfaces; the exact surface remains an occluder'
                            ),
                        )
                    )

            for plane in execution_input.boundary_planes:
                mirrored_world = _mirror_source(source_world, plane)
                if general_geometry:
                    image_world = mirrored_world
                else:
                    target_local = _local(mirrored_world, origin)
                    image_local = _nearest_engine_image(
                        images,
                        target_local,
                        tolerance_m=execution_input.engine_image_match_tolerance_m,
                        label=f'first reflection {plane.source_surface_id}',
                    )
                    image_world = _world(image_local, origin)
                reflection = _reflection_point(
                    image_world,
                    receiver_world,
                    plane,
                    tolerance=execution_input.geometric_tolerance_m,
                )
                if reflection is None or not _point_on_surface(
                    compiled_geometry,
                    plane.source_surface_id,
                    reflection,
                    tolerance=execution_input.geometric_tolerance_m,
                ):
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision='UNSUPPORTED_GEOMETRY',
                            reason=(
                                'candidate image source does not intersect the exact '
                                'semantic R120 surface triangle extent'
                            ),
                        )
                    )
                    continue

                ignored = (
                    frozenset()
                    if general_geometry
                    else frozenset((plane.source_surface_id,))
                )
                if _segment_blocked(
                    compiled_geometry,
                    source_world,
                    reflection,
                    tolerance=execution_input.geometric_tolerance_m,
                    ignored_surface_ids=ignored,
                    distance_scaled_tolerance=general_geometry,
                ) or _segment_blocked(
                    compiled_geometry,
                    reflection,
                    receiver_world,
                    tolerance=execution_input.geometric_tolerance_m,
                    ignored_surface_ids=ignored,
                    distance_scaled_tolerance=general_geometry,
                ):
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision='BLOCKED_VISIBILITY',
                            reason=(
                                'exact R120 triangle surface blocks one reflection segment'
                            ),
                        )
                    )
                    continue

                departure = _vector(source_world, reflection)
                arrival = _vector(reflection, receiver_world)
                path_length = _distance(source_world, reflection) + _distance(
                    reflection,
                    receiver_world,
                )
                if plane.material_authority is None:
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision='UNSUPPORTED_BOUNDARY_QUANTITY',
                            reason=(
                                'reflection surface has no exact geometric material authority'
                            ),
                        )
                    )
                    continue
                resolved_material = material_resolver(plane.material_authority)
                if (
                    resolved_material is None
                    or resolved_material.authority_ref != plane.material_authority
                ):
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision='UNSUPPORTED_BOUNDARY_QUANTITY',
                            reason='exact reflection material authority cannot be resolved',
                        )
                    )
                    continue

                reflection_bands: list[DeterministicPathBandQuantity] = []
                failure: PathCandidateDecision | None = None
                failure_reason = ''
                for frequency_hz in execution_input.frequency_centers_hz:
                    directivity = _directivity_contribution(
                        dataset,
                        frequency_hz=frequency_hz,
                        source_axis=source.source_axis,
                        departure_direction=departure,
                        tolerance=execution_input.geometric_tolerance_m,
                    )
                    if directivity is None:
                        failure = 'UNSUPPORTED_DIRECTIVITY'
                        failure_reason = (
                            'exact source directivity cannot evaluate reflected-path '
                            'departure angle/frequency'
                        )
                        break
                    boundary = _material_contribution(
                        resolved_material,
                        plane,
                        frequency_hz=frequency_hz,
                        tolerance=execution_input.geometric_tolerance_m,
                    )
                    if boundary is None:
                        failure = 'UNSUPPORTED_BOUNDARY_QUANTITY'
                        failure_reason = (
                            'surface lacks an exact matching banded absorption/scattering '
                            'quantity; no reflection coefficient/phase is fabricated'
                        )
                        break
                    spreading = 1.0 / (path_length * path_length)
                    reflection_bands.append(
                        DeterministicPathBandQuantity(
                            center_hz=frequency_hz,
                            spreading_factor_per_m2=spreading,
                            source_directivity=directivity,
                            boundary_material=boundary,
                            relative_energy_transport_per_m2=(
                                spreading
                                * directivity.energy_factor
                                * boundary.specular_energy_factor
                            ),
                        )
                    )
                if failure is not None:
                    rejected.append(
                        RejectedPathCandidate(
                            source_entity_id=source.source_entity_id,
                            receiver_id=receiver.receiver_id,
                            path_type='specular_reflection',
                            interaction_surface_ids=(plane.source_surface_id,),
                            decision=failure,
                            reason=failure_reason,
                        )
                    )
                    continue

                paths.append(
                    _make_path(
                        path_type='specular_reflection',
                        source=source,
                        receiver=receiver,
                        points=(reflection,),
                        surface_ids=(plane.source_surface_id,),
                        length_m=path_length,
                        sound_speed_m_s=execution_input.sound_speed_m_s,
                        departure=departure,
                        arrival=arrival,
                        bands=reflection_bands,
                        decimals=execution_input.identity_decimal_places,
                        solver_implementation_ref=execution_input.solver_implementation_ref,
                    )
                )

            if general_geometry and execution_input.maximum_reflection_order == 2:
                ordered_planes = tuple(
                    sorted(
                        execution_input.boundary_planes,
                        key=lambda item: item.source_surface_id,
                    )
                )
                for first_plane in ordered_planes:
                    for second_plane in ordered_planes:
                        surface_ids = (
                            first_plane.source_surface_id,
                            second_plane.source_surface_id,
                        )
                        if first_plane.source_surface_id == second_plane.source_surface_id:
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'same-surface immediate repeat is a degenerate '
                                        'second-order interaction and is not synthesized'
                                    ),
                                )
                            )
                            continue
                        if _planes_coincident(
                            first_plane,
                            second_plane,
                            tolerance=execution_input.geometric_tolerance_m,
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'ordered second-order planes are coincident within '
                                        'the declared geometric tolerance'
                                    ),
                                )
                            )
                            continue

                        reconstructed = _second_order_reflection_points(
                            source_world,
                            receiver_world,
                            first_plane,
                            second_plane,
                            tolerance=execution_input.geometric_tolerance_m,
                        )
                        if reconstructed is None:
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'ordered second-order image reconstruction has no '
                                        'unambiguous finite plane intersection'
                                    ),
                                )
                            )
                            continue
                        first_point, second_point = reconstructed

                        segment_lengths = (
                            _distance(source_world, first_point),
                            _distance(first_point, second_point),
                            _distance(second_point, receiver_world),
                        )
                        if any(
                            value <= execution_input.geometric_tolerance_m
                            for value in segment_lengths
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'second-order reflection points collapse or create '
                                        'a zero-length propagation segment'
                                    ),
                                )
                            )
                            continue

                        if not _point_on_surface(
                            compiled_geometry,
                            first_plane.source_surface_id,
                            first_point,
                            tolerance=execution_input.geometric_tolerance_m,
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'first second-order reflection point lies outside '
                                        'the exact semantic R120 surface triangle extent'
                                    ),
                                )
                            )
                            continue
                        if not _point_on_surface(
                            compiled_geometry,
                            second_plane.source_surface_id,
                            second_point,
                            tolerance=execution_input.geometric_tolerance_m,
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'second second-order reflection point lies outside '
                                        'the exact semantic R120 surface triangle extent'
                                    ),
                                )
                            )
                            continue

                        if _point_has_other_surface_contact(
                            compiled_geometry,
                            first_plane.source_surface_id,
                            first_point,
                            tolerance=execution_input.geometric_tolerance_m,
                        ) or _point_has_other_surface_contact(
                            compiled_geometry,
                            second_plane.source_surface_id,
                            second_point,
                            tolerance=execution_input.geometric_tolerance_m,
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'second-order reflection contact is shared-edge or '
                                        'multi-surface ambiguous within declared tolerance'
                                    ),
                                )
                            )
                            continue

                        if (
                            _segment_grazes_plane(
                                source_world,
                                first_point,
                                first_plane,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                            or _segment_grazes_plane(
                                first_point,
                                second_point,
                                first_plane,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                            or _segment_grazes_plane(
                                first_point,
                                second_point,
                                second_plane,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                            or _segment_grazes_plane(
                                second_point,
                                receiver_world,
                                second_plane,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_GEOMETRY',
                                    reason=(
                                        'grazing or plane-parallel second-order contact is '
                                        'ambiguous within declared tolerance'
                                    ),
                                )
                            )
                            continue

                        if any(
                            _segment_blocked(
                                compiled_geometry,
                                segment_start,
                                segment_end,
                                tolerance=execution_input.geometric_tolerance_m,
                                distance_scaled_tolerance=True,
                            )
                            for segment_start, segment_end in (
                                (source_world, first_point),
                                (first_point, second_point),
                                (second_point, receiver_world),
                            )
                        ):
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='BLOCKED_VISIBILITY',
                                    reason=(
                                        'exact R120 triangle surface blocks one of three '
                                        'ordered second-order propagation segments'
                                    ),
                                )
                            )
                            continue

                        resolved_materials: list[GeometricMaterialAuthority] = []
                        material_failure = False
                        for interaction_plane in (first_plane, second_plane):
                            if interaction_plane.material_authority is None:
                                material_failure = True
                                break
                            resolved = material_resolver(
                                interaction_plane.material_authority
                            )
                            if (
                                resolved is None
                                or resolved.authority_ref
                                != interaction_plane.material_authority
                            ):
                                material_failure = True
                                break
                            resolved_materials.append(resolved)
                        if material_failure or len(resolved_materials) != 2:
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision='UNSUPPORTED_BOUNDARY_QUANTITY',
                                    reason=(
                                        'one or more ordered second-order surfaces lack '
                                        'an exact resolvable geometric material authority'
                                    ),
                                )
                            )
                            continue

                        departure = _vector(source_world, first_point)
                        arrival = _vector(second_point, receiver_world)
                        path_length = sum(segment_lengths)
                        second_order_bands: list[
                            DeterministicPathBandQuantity
                        ] = []
                        failure: PathCandidateDecision | None = None
                        failure_reason = ''
                        for frequency_hz in execution_input.frequency_centers_hz:
                            directivity = _directivity_contribution(
                                dataset,
                                frequency_hz=frequency_hz,
                                source_axis=source.source_axis,
                                departure_direction=departure,
                                tolerance=execution_input.geometric_tolerance_m,
                            )
                            if directivity is None:
                                failure = 'UNSUPPORTED_DIRECTIVITY'
                                failure_reason = (
                                    'exact source directivity cannot evaluate second-order '
                                    'departure angle/frequency'
                                )
                                break

                            boundary_contributions: list[
                                BoundaryMaterialContribution
                            ] = []
                            for interaction_plane, resolved in zip(
                                (first_plane, second_plane),
                                resolved_materials,
                                strict=True,
                            ):
                                boundary = _material_contribution(
                                    resolved,
                                    interaction_plane,
                                    frequency_hz=frequency_hz,
                                    tolerance=execution_input.geometric_tolerance_m,
                                )
                                if boundary is None:
                                    failure = 'UNSUPPORTED_BOUNDARY_QUANTITY'
                                    failure_reason = (
                                        'one or more second-order surfaces lack an exact '
                                        'matching banded absorption/scattering quantity; '
                                        'no reflection coefficient/phase is fabricated'
                                    )
                                    break
                                boundary_contributions.append(boundary)
                            if failure is not None:
                                break

                            spreading = 1.0 / (path_length * path_length)
                            specular_product = 1.0
                            for boundary in boundary_contributions:
                                specular_product *= boundary.specular_energy_factor
                            second_order_bands.append(
                                DeterministicPathBandQuantity(
                                    center_hz=frequency_hz,
                                    spreading_factor_per_m2=spreading,
                                    source_directivity=directivity,
                                    boundary_materials=tuple(
                                        boundary_contributions
                                    ),
                                    relative_energy_transport_per_m2=(
                                        spreading
                                        * directivity.energy_factor
                                        * specular_product
                                    ),
                                )
                            )
                        if failure is not None:
                            rejected.append(
                                RejectedPathCandidate(
                                    source_entity_id=source.source_entity_id,
                                    receiver_id=receiver.receiver_id,
                                    path_type='specular_reflection',
                                    interaction_surface_ids=surface_ids,
                                    decision=failure,
                                    reason=failure_reason,
                                )
                            )
                            continue

                        paths.append(
                            _make_path(
                                path_type='specular_reflection',
                                source=source,
                                receiver=receiver,
                                points=(first_point, second_point),
                                surface_ids=surface_ids,
                                length_m=path_length,
                                sound_speed_m_s=execution_input.sound_speed_m_s,
                                departure=departure,
                                arrival=arrival,
                                bands=second_order_bands,
                                decimals=execution_input.identity_decimal_places,
                                solver_implementation_ref=(
                                    execution_input.solver_implementation_ref
                                ),
                            )
                        )

    paths.sort(
        key=lambda item: (
            item.source_entity_id,
            item.receiver_id,
            0
            if item.path_type == 'direct'
            else len(item.ordered_interaction_surface_ids),
            item.ordered_interaction_surface_ids,
            item.path_id,
        )
    )
    rejected.sort(
        key=lambda item: (
            item.source_entity_id,
            item.receiver_id,
            0
            if item.path_type == 'direct'
            else len(item.interaction_surface_ids),
            item.interaction_surface_ids,
            item.decision,
            item.reason,
        )
    )
    execution_digest = _semantic_hash(
        {
            'execution_input_id': execution_input.execution_input_id,
            'execution_input_sha256': execution_input.semantic_sha256,
            'engine_id': engine.engine_id,
            'engine_version': engine.engine_version,
            'candidate_source_commit': engine.candidate_source_commit,
        }
    )
    execution_id = f'r150-ga-execution:{execution_digest}'
    core: dict[str, Any] = {
        'schema_version': DETERMINISTIC_GA_SCHEMA_VERSION,
        'authority_version': DETERMINISTIC_GA_AUTHORITY_VERSION,
        'execution_id': execution_id,
        'execution_input_id': execution_input.execution_input_id,
        'execution_input_sha256': execution_input.semantic_sha256,
        'snapshot_id': execution_input.snapshot_id,
        'snapshot_sha256': execution_input.snapshot_sha256,
        'prediction_request_id': execution_input.prediction_request_id,
        'prediction_request_sha256': execution_input.prediction_request_sha256,
        'dispatch_binding_id': execution_input.dispatch_binding_id,
        'dispatch_binding_sha256': execution_input.dispatch_binding_sha256,
        'adapter_descriptor_id': execution_input.adapter_descriptor_id,
        'adapter_descriptor_sha256': execution_input.adapter_descriptor_sha256,
        'solver_implementation_ref': execution_input.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'solver_configuration_ref': execution_input.solver_configuration_ref.model_dump(
            mode='json'
        ),
        'r120_compiled_geometry_id': execution_input.r120_compiled_geometry_id,
        'r120_compiled_geometry_sha256': (
            execution_input.r120_compiled_geometry_sha256
        ),
        'topology_identity_sha256': execution_input.topology_identity_sha256,
        'engine_id': engine.engine_id,
        'engine_version': engine.engine_version,
        'candidate_source_commit': engine.candidate_source_commit,
        'numeric_comparison_tolerance_m': execution_input.geometric_tolerance_m,
        'identity_decimal_places': execution_input.identity_decimal_places,
        'frequency_domain': execution_input.frequency_domain.model_dump(mode='json'),
        'path_scope': (
            'direct_through_second_order_specular'
            if execution_input.maximum_reflection_order == 2
            else 'direct_and_first_order_specular'
        ),
        'coherent_phase_authority': 'UNAVAILABLE_NOT_SYNTHESIZED',
        'paths': [
            {
                **item.model_dump(mode='json'),
                'bands': [
                    {
                        key: value
                        for key, value in band.model_dump(mode='json').items()
                        if not (key == 'boundary_materials' and value is None)
                    }
                    for band in item.bands
                ],
            }
            for item in paths
        ],
        'rejected_candidates': [
            item.model_dump(mode='json') for item in rejected
        ],
    }
    digest = _semantic_hash(core)
    return DeterministicPathArtifact(
        artifact_id=f'deterministic-path-artifact:{digest}',
        semantic_sha256=digest,
        **core,
    )


def deterministic_path_observable_manifest(
    artifact: DeterministicPathArtifact,
) -> AcousticSolverObservableArtifact:
    return AcousticSolverObservableArtifact(
        observable='deterministic_paths',
        artifact_authority=artifact.as_external_ref(),
        encoding_schema_ref=DETERMINISTIC_PATH_ARTIFACT_SCHEMA_REF,
        valid_frequency_domain=artifact.frequency_domain,
    )


def build_deterministic_ga_result_envelope(
    *,
    artifact: DeterministicPathArtifact,
    dispatch: AcousticSolverDispatchBinding,
    request: AcousticPredictionRequest,
    completed_at_utc: str,
) -> AcousticSolverResultEnvelope:
    if artifact.dispatch_binding_id != dispatch.binding_id:
        raise ValueError('path artifact/dispatch identity mismatch')
    if artifact.prediction_request_id != request.request_id:
        raise ValueError('path artifact/request identity mismatch')
    return build_acoustic_solver_result_envelope(
        dispatch=dispatch,
        request=request,
        execution_id=artifact.execution_id,
        execution_provenance_ref=artifact.execution_provenance_ref(),
        artifacts=(deterministic_path_observable_manifest(artifact),),
        completed_at_utc=completed_at_utc,
    )


ConfigurationResolver = Callable[
    [ExactExternalAuthorityRef],
    DeterministicGaConfiguration | None,
]
GeometryAuthority = AcousticRegionAuthority | PortalAuthority | BoundaryTerminationAuthority
GeometryAuthorityResolver = Callable[
    [ExactExternalAuthorityRef],
    GeometryAuthority | None,
]


class CadDeterministicPathArtifactRepository:
    """Append-only typed path persistence with exact authority re-resolution."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        snapshot_repository: CadAcousticSnapshotRepository,
        dispatch_repository: CadAcousticSolverDispatchRepository,
        configuration_resolver: ConfigurationResolver,
        material_resolver: MaterialAuthorityResolver,
        geometry_authority_resolver: GeometryAuthorityResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.snapshot_repository = snapshot_repository
        self.dispatch_repository = dispatch_repository
        self.configuration_resolver = configuration_resolver
        self.material_resolver = material_resolver
        self.geometry_authority_resolver = geometry_authority_resolver
        self.path = Path(scene_repository.path)
        for label, repository in (
            ('snapshot', snapshot_repository),
            ('dispatch', dispatch_repository),
        ):
            if Path(repository.path) != self.path:
                raise ValueError(
                    f'deterministic path and {label} repositories must share one CAD database'
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_deterministic_ga_execution_inputs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_input_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    snapshot_id TEXT NOT NULL,
                    prediction_request_id TEXT NOT NULL,
                    dispatch_binding_id TEXT NOT NULL,
                    r120_compiled_geometry_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_deterministic_ga_input_request_seq
                    ON cad_deterministic_ga_execution_inputs(
                        prediction_request_id, seq ASC
                    );

                CREATE TABLE IF NOT EXISTS cad_deterministic_path_artifacts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    artifact_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    execution_id TEXT NOT NULL,
                    execution_provenance_authority_id TEXT NOT NULL,
                    execution_input_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    prediction_request_id TEXT NOT NULL,
                    dispatch_binding_id TEXT NOT NULL,
                    r120_compiled_geometry_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_deterministic_path_request_seq
                    ON cad_deterministic_path_artifacts(
                        prediction_request_id, seq ASC
                    );
                """
            )

    def _resolve_geometry_authority(
        self,
        ref: ExactExternalAuthorityRef,
        expected_type: type,
        label: str,
    ):
        authority = self.geometry_authority_resolver(ref)
        if authority is None or not isinstance(authority, expected_type):
            raise ValueError(f'{label} exact authority does not exist with expected type')
        if _authority_ref(authority) != ref:
            raise ValueError(f'{label} exact authority mismatch')
        return authority

    def _validate_execution_input(
        self,
        execution_input: DeterministicGaExecutionInput,
    ) -> DeterministicGaExecutionInput:
        execution_input = DeterministicGaExecutionInput.model_validate(
            execution_input.model_dump(mode='python')
        )
        snapshot = self.snapshot_repository.get_snapshot(execution_input.snapshot_id)
        if (
            snapshot is None
            or snapshot.semantic_sha256 != execution_input.snapshot_sha256
        ):
            raise ValueError('GA execution input exact snapshot is missing or mismatched')
        request = self.snapshot_repository.get_prediction_request(
            execution_input.prediction_request_id
        )
        if (
            request is None
            or request.request_semantic_sha256
            != execution_input.prediction_request_sha256
        ):
            raise ValueError(
                'GA execution input exact prediction request is missing or mismatched'
            )
        dispatch = self.dispatch_repository.get_dispatch(
            execution_input.dispatch_binding_id
        )
        if (
            dispatch is None
            or dispatch.semantic_sha256 != execution_input.dispatch_binding_sha256
            or dispatch.state != 'READY'
        ):
            raise ValueError(
                'GA execution input exact READY dispatch is missing or mismatched'
            )
        descriptor = self.dispatch_repository.get_descriptor(
            execution_input.adapter_descriptor_id
        )
        if (
            descriptor is None
            or descriptor.semantic_sha256 != execution_input.adapter_descriptor_sha256
        ):
            raise ValueError(
                'GA execution input exact adapter descriptor is missing or mismatched'
            )
        configuration = self.configuration_resolver(
            execution_input.solver_configuration_ref
        )
        if (
            configuration is None
            or configuration.as_external_ref()
            != execution_input.solver_configuration_ref
        ):
            raise ValueError(
                'GA execution input exact configuration is missing or mismatched'
            )
        compiled = self.snapshot_repository.r120_repository.get_compiled_geometry(
            execution_input.r120_compiled_geometry_id
        )
        if (
            compiled is None
            or compiled.compiled_hash_sha256
            != execution_input.r120_compiled_geometry_sha256
            or compiled.topology_identity_sha256
            != execution_input.topology_identity_sha256
        ):
            raise ValueError('GA execution input exact R120 geometry is missing or mismatched')
        if compiled.region_authority_ref is None:
            raise ValueError('GA execution input R120 region authority is missing')
        if compiled.portal_authority_ref is None:
            raise ValueError('GA execution input R120 portal authority is missing')
        if compiled.boundary_termination_authority_ref is None:
            raise ValueError('GA execution input R120 termination authority is missing')
        region = self._resolve_geometry_authority(
            compiled.region_authority_ref,
            AcousticRegionAuthority,
            'region',
        )
        portals = self._resolve_geometry_authority(
            compiled.portal_authority_ref,
            PortalAuthority,
            'portal',
        )
        terminations = self._resolve_geometry_authority(
            compiled.boundary_termination_authority_ref,
            BoundaryTerminationAuthority,
            'boundary termination',
        )
        datasets: list[DirectivityDataset] = []
        for source in execution_input.sources:
            dataset = (
                self.snapshot_repository.r110_repository.directivity_repository
                .get_dataset_by_hash(source.directivity_dataset_sha256)
            )
            if dataset is None:
                raise ValueError(
                    'GA execution input exact DirectivityDataset is missing'
                )
            datasets.append(dataset)
        regenerated = compile_deterministic_ga_execution_input(
            snapshot=snapshot,
            request=request,
            dispatch=dispatch,
            descriptor=descriptor,
            compiled_geometry=compiled,
            region_authority=region,
            portal_authority=portals,
            boundary_termination_authority=terminations,
            directivity_datasets=datasets,
            configuration=configuration,
        )
        if regenerated != execution_input:
            raise ValueError(
                'GA execution input does not reproduce from exact persisted authorities'
            )
        return execution_input

    def save_execution_input(
        self,
        execution_input: DeterministicGaExecutionInput,
    ) -> DeterministicGaExecutionInput:
        execution_input = self._validate_execution_input(execution_input)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_deterministic_ga_execution_inputs
                WHERE execution_input_id=?
                """,
                (execution_input.execution_input_id,),
            ).fetchone()
            if existing is not None:
                persisted = DeterministicGaExecutionInput.model_validate_json(
                    existing['payload_json']
                )
                if persisted != execution_input:
                    raise ValueError(
                        'GA execution input id exists with different semantics'
                    )
                return self._validate_execution_input(persisted)
            connection.execute(
                """
                INSERT INTO cad_deterministic_ga_execution_inputs(
                    execution_input_id,
                    semantic_sha256,
                    snapshot_id,
                    prediction_request_id,
                    dispatch_binding_id,
                    r120_compiled_geometry_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_input.execution_input_id,
                    execution_input.semantic_sha256,
                    execution_input.snapshot_id,
                    execution_input.prediction_request_id,
                    execution_input.dispatch_binding_id,
                    execution_input.r120_compiled_geometry_id,
                    execution_input.model_dump_json(),
                    _utc_now(),
                ),
            )
        return execution_input

    def get_execution_input(
        self,
        execution_input_id: str,
    ) -> DeterministicGaExecutionInput | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_deterministic_ga_execution_inputs
                WHERE execution_input_id=?
                """,
                (execution_input_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_execution_input(
            DeterministicGaExecutionInput.model_validate_json(
                row['payload_json']
            )
        )

    def _validate(
        self,
        artifact: DeterministicPathArtifact,
    ) -> DeterministicPathArtifact:
        artifact = DeterministicPathArtifact.model_validate(
            artifact.model_dump(mode='python')
        )
        execution_input = self.get_execution_input(artifact.execution_input_id)
        if (
            execution_input is None
            or execution_input.semantic_sha256 != artifact.execution_input_sha256
            or execution_input.snapshot_id != artifact.snapshot_id
            or execution_input.prediction_request_id != artifact.prediction_request_id
            or execution_input.dispatch_binding_id != artifact.dispatch_binding_id
            or execution_input.r120_compiled_geometry_id
            != artifact.r120_compiled_geometry_id
        ):
            raise ValueError(
                'path artifact exact GA execution input is missing or mismatched'
            )
        snapshot = self.snapshot_repository.get_snapshot(artifact.snapshot_id)
        if snapshot is None or snapshot.semantic_sha256 != artifact.snapshot_sha256:
            raise ValueError('path artifact exact snapshot is missing or mismatched')
        request = self.snapshot_repository.get_prediction_request(
            artifact.prediction_request_id
        )
        if (
            request is None
            or request.request_semantic_sha256 != artifact.prediction_request_sha256
        ):
            raise ValueError('path artifact exact prediction request is missing or mismatched')
        dispatch = self.dispatch_repository.get_dispatch(
            artifact.dispatch_binding_id
        )
        if (
            dispatch is None
            or dispatch.semantic_sha256 != artifact.dispatch_binding_sha256
            or dispatch.state != 'READY'
        ):
            raise ValueError('path artifact exact READY dispatch is missing or mismatched')
        descriptor = self.dispatch_repository.get_descriptor(
            artifact.adapter_descriptor_id
        )
        if (
            descriptor is None
            or descriptor.semantic_sha256 != artifact.adapter_descriptor_sha256
        ):
            raise ValueError('path artifact exact adapter descriptor is missing or mismatched')
        if (
            artifact.solver_implementation_ref != dispatch.solver_implementation_ref
            or artifact.solver_configuration_ref != dispatch.solver_configuration_ref
        ):
            raise ValueError('path artifact solver implementation/config mismatch')

        configuration = self.configuration_resolver(
            artifact.solver_configuration_ref
        )
        if (
            configuration is None
            or configuration.as_external_ref() != artifact.solver_configuration_ref
        ):
            raise ValueError('path artifact exact GA configuration is missing or mismatched')

        compiled = self.snapshot_repository.r120_repository.get_compiled_geometry(
            artifact.r120_compiled_geometry_id
        )
        if (
            compiled is None
            or compiled.compiled_hash_sha256
            != artifact.r120_compiled_geometry_sha256
            or compiled.topology_identity_sha256 != artifact.topology_identity_sha256
            or compiled.compiled_geometry_id != snapshot.r120_compiled_geometry_id
        ):
            raise ValueError('path artifact exact R120 geometry is missing or mismatched')

        if compiled.region_authority_ref is None:
            raise ValueError('path artifact R120 region authority is missing')
        if compiled.portal_authority_ref is None:
            raise ValueError('path artifact R120 portal authority is missing')
        if compiled.boundary_termination_authority_ref is None:
            raise ValueError('path artifact R120 termination authority is missing')
        self._resolve_geometry_authority(
            compiled.region_authority_ref,
            AcousticRegionAuthority,
            'region',
        )
        self._resolve_geometry_authority(
            compiled.portal_authority_ref,
            PortalAuthority,
            'portal',
        )
        self._resolve_geometry_authority(
            compiled.boundary_termination_authority_ref,
            BoundaryTerminationAuthority,
            'boundary termination',
        )

        source_by_id = {item.source_entity_id: item for item in snapshot.sources}
        receiver_by_id = {item.receiver_id: item for item in snapshot.receivers}
        for path in artifact.paths:
            source = source_by_id.get(path.source_entity_id)
            receiver = receiver_by_id.get(path.receiver_id)
            if source is None or receiver is None:
                raise ValueError('path artifact source/receiver no longer resolves')
            model = self.snapshot_repository.r110_repository.get_model(
                source.r110_compiled_source_sha256
            )
            if model is None:
                raise ValueError('path artifact exact R110 source is missing')
            if source.directivity_dataset_sha256 is None:
                raise ValueError('path artifact source DirectivityDataset is missing')
            dataset = (
                self.snapshot_repository.r110_repository.directivity_repository
                .get_dataset_by_hash(source.directivity_dataset_sha256)
            )
            if dataset is None:
                raise ValueError('path artifact exact DirectivityDataset is missing')
            for band in path.bands:
                contribution = band.source_directivity
                if contribution.dataset_semantic_sha256 != dataset.semantic_sha256:
                    raise ValueError('path directivity dataset hash mismatch')
                reevaluated = evaluate_directivity(
                    dataset,
                    frequency_hz=contribution.frequency_hz,
                    horizontal_angle_deg=contribution.horizontal_angle_deg,
                    vertical_angle_deg=contribution.vertical_angle_deg,
                    request='magnitude',
                )
                if (
                    reevaluated.decision != 'SUPPORTED'
                    or reevaluated.semantic_sha256
                    != contribution.evaluation_semantic_sha256
                    or reevaluated.magnitude_linear
                    != contribution.magnitude_linear
                ):
                    raise ValueError(
                        'path directivity contribution no longer reproduces exactly'
                    )
                boundary = band.boundary_material
                if boundary is not None:
                    material = self.material_resolver(
                        boundary.material_authority
                    )
                    if (
                        material is None
                        or material.authority_ref != boundary.material_authority
                    ):
                        raise ValueError(
                            'path boundary material exact authority is missing'
                        )
                    matching = [
                        item
                        for item in material.material.geometric_bands
                        if abs(
                            float(item.center_hz) - float(boundary.frequency_hz)
                        )
                        <= configuration.geometric_tolerance_m
                    ]
                    if len(matching) != 1:
                        raise ValueError(
                            'path boundary material band no longer resolves exactly'
                        )
                    item = matching[0]
                    expected_specular = (1.0 - item.absorption) * (
                        1.0 - item.scattering
                    )
                    if (
                        item.absorption != boundary.absorption
                        or item.scattering != boundary.scattering
                        or expected_specular != boundary.specular_energy_factor
                    ):
                        raise ValueError(
                            'path boundary material contribution no longer reproduces'
                        )

        return artifact

    def save(
        self,
        artifact: DeterministicPathArtifact,
    ) -> DeterministicPathArtifact:
        artifact = self._validate(artifact)
        provenance = artifact.execution_provenance_ref()
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_deterministic_path_artifacts
                WHERE artifact_id=?
                """,
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                persisted = DeterministicPathArtifact.model_validate_json(
                    existing['payload_json']
                )
                if persisted != artifact:
                    raise ValueError(
                        'deterministic path artifact id exists with different semantics'
                    )
                return self._validate(persisted)
            connection.execute(
                """
                INSERT INTO cad_deterministic_path_artifacts(
                    artifact_id,
                    semantic_sha256,
                    execution_id,
                    execution_provenance_authority_id,
                    execution_input_id,
                    snapshot_id,
                    prediction_request_id,
                    dispatch_binding_id,
                    r120_compiled_geometry_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.semantic_sha256,
                    artifact.execution_id,
                    provenance.authority_id,
                    artifact.execution_input_id,
                    artifact.snapshot_id,
                    artifact.prediction_request_id,
                    artifact.dispatch_binding_id,
                    artifact.r120_compiled_geometry_id,
                    artifact.model_dump_json(),
                    _utc_now(),
                ),
            )
        return artifact

    def get(self, artifact_id: str) -> DeterministicPathArtifact | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_deterministic_path_artifacts
                WHERE artifact_id=?
                """,
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            DeterministicPathArtifact.model_validate_json(row['payload_json'])
        )

    def resolve_external_authority(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> ExactExternalAuthorityRef | None:
        if ref == DETERMINISTIC_PATH_ARTIFACT_SCHEMA_REF:
            return ref
        if ref.authority_id.startswith('deterministic-path-artifact:'):
            artifact = self.get(ref.authority_id)
            if artifact is not None and artifact.as_external_ref() == ref:
                return ref
            return None
        if ref.authority_id.startswith('r150-ga-execution-provenance:'):
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM cad_deterministic_path_artifacts
                    WHERE execution_provenance_authority_id=?
                    """,
                    (ref.authority_id,),
                ).fetchone()
            if row is None:
                return None
            artifact = self._validate(
                DeterministicPathArtifact.model_validate_json(row['payload_json'])
            )
            return ref if artifact.execution_provenance_ref() == ref else None
        return None
