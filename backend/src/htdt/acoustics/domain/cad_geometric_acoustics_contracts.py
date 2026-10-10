
from ...cad_equipment import FrequencyDomain
from ...cad_scene import (
    Direction3,
    Position3,
)
from ...canonical_json import canonical_sha256 as _semantic_hash
from ...r120_geometry_compiler import (
    AcousticRegionAuthority,
    AcousticRegionDeclaration,
    BoundaryTerminationAuthority,
    ExactExternalAuthorityRef,
    PortalAuthority,
)
from ..domain.acoustic_benchmark import (
    AcousticMaterial,
    GeometricIncidenceCondition,
)
from ..domain.cad_geometric_acoustics_portal import (
    GeometricPortalAperture,
    GeometricPortalGraph,
)
from functools import lru_cache
from math import (
    degrees,
    isfinite,
    sqrt,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from typing import (
    Any,
    Literal,
    Sequence,
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

HTDT_PORTAL_GRAPH_DIRECT_ENGINE_VERSION = '2'

HTDT_PORTAL_FIRST_ORDER_ENGINE_ID = 'htdt.r150.explicit_portal_first_order'

HTDT_PORTAL_FIRST_ORDER_ENGINE_VERSION = '1'

MIN_SUPPORTED_FEATURE_M = 2.0e-3

PathType = Literal['direct', 'specular_reflection']

PathCandidateDecision = Literal[
    'BLOCKED_VISIBILITY',
    'UNSUPPORTED_DIRECTIVITY',
    'UNSUPPORTED_BOUNDARY_QUANTITY',
    'UNSUPPORTED_GEOMETRY',
    'INVALID_PORTAL_CROSSING',
    'INVALID_REGION_SEQUENCE',
    'DISCONNECTED_REGION_GRAPH',
    'PORTAL_CROSSING_LIMIT_EXCEEDED',
    'INTERMEDIATE_REGION_MEMBERSHIP_FAILURE',
    'UNSUPPORTED_PORTAL_TOPOLOGY',
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

HTDT_PORTAL_GRAPH_DIRECT_IMPLEMENTATION_REF = ExactExternalAuthorityRef(
    authority_id='adapter-kernel:htdt-r150-explicit-portal-direct',
    authority_version=HTDT_PORTAL_GRAPH_DIRECT_ENGINE_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'implementation': HTDT_PORTAL_DIRECT_ENGINE_ID,
            'version': HTDT_PORTAL_GRAPH_DIRECT_ENGINE_VERSION,
            'construction': 'exact_bounded_directed_portal_graph_direct_propagation',
            'traversal_policy': 'simple_region_path_v1',
            'repeated_region_traversal': False,
            'repeated_portal_traversal': False,
            'maximum_portal_crossings_range': [1, 16],
            'maximum_search_states': 4096,
            'maximum_reflection_order': 0,
            'coherent_phase': 'unavailable_not_synthesized',
        }
    ),
)

HTDT_PORTAL_FIRST_ORDER_IMPLEMENTATION_REF = ExactExternalAuthorityRef(
    authority_id='adapter-kernel:htdt-r150-explicit-portal-first-order',
    authority_version=HTDT_PORTAL_FIRST_ORDER_ENGINE_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'implementation': HTDT_PORTAL_FIRST_ORDER_ENGINE_ID,
            'version': HTDT_PORTAL_FIRST_ORDER_ENGINE_VERSION,
            'construction': 'exact_two_region_one_portal_one_ordinary_surface_first_order_specular',
            'maximum_portal_crossings': 1,
            'maximum_reflection_order': 1,
            'portal_reflection': False,
            'event_topologies': [
                'source_reflection_portal_receiver',
                'source_portal_reflection_receiver',
            ],
            'coherent_phase': 'delegated_to_r150_path_response_authority',
        }
    ),
)

HTDT_PORTAL_SPECULAR_GRAPH_ENGINE_VERSION = '2'

HTDT_PORTAL_SPECULAR_GRAPH_IMPLEMENTATION_REF = ExactExternalAuthorityRef(
    authority_id='adapter-kernel:htdt-r150-explicit-portal-first-order',
    authority_version=HTDT_PORTAL_SPECULAR_GRAPH_ENGINE_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'implementation': HTDT_PORTAL_FIRST_ORDER_ENGINE_ID,
            'version': HTDT_PORTAL_SPECULAR_GRAPH_ENGINE_VERSION,
            'construction': (
                'exact_bounded_directed_portal_graph_interleaved_'
                'ordered_specular_reflection'
            ),
            'traversal_policy': 'simple_region_path_v1',
            'repeated_region_traversal': False,
            'repeated_portal_traversal': False,
            'maximum_portal_crossings_range': [1, 16],
            'maximum_search_states': 4096,
            'maximum_reflection_order_range': [1, 2],
            'portal_surface_reflection': 'bounded_exact_finite_triangles_only',
            'coherent_phase': 'delegated_to_r150_path_response_authority',
        }
    ),
)

HTDT_PORTAL_SPECULAR_CHAIN_ENGINE_VERSION = '3'

HTDT_PORTAL_SPECULAR_CHAIN_IMPLEMENTATION_REF = ExactExternalAuthorityRef(
    authority_id='adapter-kernel:htdt-r150-explicit-portal-first-order',
    authority_version=HTDT_PORTAL_SPECULAR_CHAIN_ENGINE_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'implementation': HTDT_PORTAL_FIRST_ORDER_ENGINE_ID,
            'version': HTDT_PORTAL_SPECULAR_CHAIN_ENGINE_VERSION,
            'construction': (
                'exact_bounded_directed_portal_graph_interleaved_'
                'bounded_order_specular_reflection_chain'
            ),
            'traversal_policy': 'simple_region_path_v1',
            'repeated_region_traversal': False,
            'repeated_portal_traversal': False,
            'maximum_portal_crossings_range': [1, 16],
            'maximum_search_states': 4096,
            'maximum_reflection_order_range': [3, 4],
            'portal_surface_reflection': 'bounded_exact_finite_triangles_only',
            'coherent_phase': 'delegated_to_r150_path_response_authority',
        }
    ),
)

HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_ENGINE_VERSION = '4'

HTDT_PORTAL_HIGH_ORDER_CHAIN_MAXIMUM_EVALUATIONS = 2097152

HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_IMPLEMENTATION_REF = ExactExternalAuthorityRef(
    authority_id='adapter-kernel:htdt-r150-explicit-portal-first-order',
    authority_version=HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_ENGINE_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'implementation': HTDT_PORTAL_FIRST_ORDER_ENGINE_ID,
            'version': HTDT_PORTAL_HIGH_ORDER_SPECULAR_CHAIN_ENGINE_VERSION,
            'construction': (
                'exact_bounded_directed_portal_graph_interleaved_'
                'bounded_order_specular_reflection_chain'
            ),
            'traversal_policy': 'simple_region_path_v1',
            'repeated_region_traversal': False,
            'repeated_portal_traversal': False,
            'maximum_portal_crossings_range': [1, 16],
            'maximum_search_states': 4096,
            'maximum_reflection_order_range': [5, 6],
            'maximum_chain_sequence_evaluations': (
                HTDT_PORTAL_HIGH_ORDER_CHAIN_MAXIMUM_EVALUATIONS
            ),
            'portal_surface_reflection': 'bounded_exact_finite_triangles_only',
            'coherent_phase': 'delegated_to_r150_path_response_authority',
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
    return (
        float(value[0]) / length,
        float(value[1]) / length,
        float(value[2]) / length,
    )

def _direction(value: Sequence[float]) -> Direction3:
    unit = _unit(value)
    return Direction3(x=unit[0], y=unit[1], z=unit[2])

def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    return _norm(_vector(a, b))

def _portal_segment_region_ids(
    ordered_region_ids: Sequence[str],
    reflection_slots: Sequence[int],
) -> tuple[str, ...]:
    """Ordered AcousticRegion identity of every propagation segment.

    A specular reflection in slot ``i`` duplicates ``ordered_region_ids[i]``
    between the two segments adjacent to the reflection point; directed Portal
    crossings advance the region index in declared graph order.
    """

    regions = tuple(ordered_region_ids)
    segment_regions: list[str] = []
    previous_slot = 0
    for slot in reflection_slots:
        segment_regions.extend(regions[previous_slot : slot + 1])
        previous_slot = slot
    segment_regions.extend(regions[previous_slot:])
    return tuple(segment_regions)

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

    maximum_reflection_order: Literal[0, 1, 2, 3, 4, 5, 6] = 1
    maximum_portal_crossings: int | None = None
    portal_traversal_policy: Literal['simple_region_path_v1'] | None = None
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
    # The only scalar-boundary-coefficient interpolation rule currently
    # authorized. Non-exact band incidence evidence is applied at every
    # reflection angle as an explicit versioned approximation — never an
    # unlabelled exact angle-specific coefficient.
    incidence_coefficient_policy: Literal[
        'scalar_coefficient_all_angles_v1'
    ] = 'scalar_coefficient_all_angles_v1'
    # Angle tolerance (degrees) inside which a reflection's evaluated
    # incidence counts as matching a declared normal/angle-specific band.
    incidence_exact_angle_tolerance_deg: float = Field(
        default=1.0, gt=0.0, le=45.0
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

    @field_validator(
        'geometric_tolerance_m',
        'engine_image_match_tolerance_m',
        'incidence_exact_angle_tolerance_deg',
    )
    @classmethod
    def finite_positive(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value) or value <= 0.0:
            raise ValueError('GA numeric tolerances must be finite and positive')
        return value

    @field_validator('maximum_portal_crossings')
    @classmethod
    def bounded_portal_crossings(cls, value: int | None) -> int | None:
        if value is None:
            return None
        value = int(value)
        if value < 1 or value > 16:
            raise ValueError('maximum_portal_crossings must be within 1..16')
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
        if self.portal_traversal_policy is None:
            payload.pop('portal_traversal_policy', None)
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
    maximum_reflection_order: Literal[0, 1, 2, 3, 4, 5, 6] = 1,
    maximum_portal_crossings: int | None = None,
    incidence_exact_angle_tolerance_deg: float = 1.0,
) -> DeterministicGaConfiguration:
    if maximum_reflection_order >= 3 and room_policy != (
        'general_planar_multi_region_portal_v1'
    ):
        raise ValueError(
            'bounded third/fourth-order specular chains are supported only by '
            'the multi-region Portal geometry policy'
        )
    if maximum_reflection_order == 2 and room_policy not in (
        'general_planar_closed_polyhedral_v1',
        'general_planar_multi_region_portal_v1',
    ):
        raise ValueError(
            'bounded second-order specular execution is supported only by the '
            'general-planar or multi-region Portal geometry policy'
        )
    if room_policy == 'general_planar_multi_region_portal_v1':
        if (
            maximum_portal_crossings is None
            or maximum_portal_crossings < 1
            or maximum_portal_crossings > 16
        ):
            raise ValueError(
                'multi-region Portal lane requires explicit '
                'maximum_portal_crossings within 1..16'
            )
    elif maximum_portal_crossings is not None:
        raise ValueError(
            'maximum_portal_crossings is only valid for the explicit multi-region Portal lane'
        )
    elif maximum_reflection_order == 0:
        raise ValueError(
            'maximum_reflection_order=0 is reserved for the explicit multi-region Portal lane'
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
        'incidence_coefficient_policy': 'scalar_coefficient_all_angles_v1',
        'incidence_exact_angle_tolerance_deg': float(
            incidence_exact_angle_tolerance_deg
        ),
    }
    if maximum_portal_crossings is not None:
        core['maximum_portal_crossings'] = int(maximum_portal_crossings)
        core['portal_traversal_policy'] = 'simple_region_path_v1'
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
    directivity_dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    acoustic_region_id: str | None = None

class DeterministicGaReceiverInput(BaseModel):
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
    portal_graph: GeometricPortalGraph | None = None
    region_declarations: tuple[AcousticRegionDeclaration, ...] | None = None
    maximum_portal_crossings: int | None = None
    occluder_triangle_indices: tuple[int, ...]
    sources: tuple[DeterministicGaSourceInput, ...]
    receivers: tuple[DeterministicGaReceiverInput, ...]
    sound_speed_m_s: float = Field(gt=0.0)
    frequency_domain: FrequencyDomain
    frequency_centers_hz: tuple[float, ...]
    geometric_tolerance_m: float = Field(gt=0.0)
    engine_image_match_tolerance_m: float = Field(gt=0.0)
    identity_decimal_places: int = Field(ge=6, le=15)
    maximum_reflection_order: Literal[0, 1, 2, 3, 4, 5, 6] | None = None
    incidence_exact_angle_tolerance_deg: float | None = Field(
        default=None, gt=0.0, le=45.0
    )

    @model_validator(mode='after')
    def validate_identity(self) -> 'DeterministicGaExecutionInput':
        if self.maximum_portal_crossings is not None and not (
            1 <= self.maximum_portal_crossings <= 16
        ):
            raise ValueError('execution input maximum_portal_crossings must be within 1..16')
        if self.portal_graph is not None:
            if self.geometry_policy != 'general_planar_multi_region_portal_v1':
                raise ValueError('Portal graph requires the explicit multi-region Portal policy')
            if self.portal_apertures is None:
                raise ValueError('Portal graph requires exact Portal aperture authority')
            if self.region_declarations is None:
                raise ValueError('Portal graph requires exact AcousticRegion declarations')
            if tuple(sorted(item.region_id for item in self.region_declarations)) != self.portal_graph.region_ids:
                raise ValueError('Portal graph/AcousticRegion declaration identity mismatch')
            if self.maximum_portal_crossings != self.portal_graph.maximum_portal_crossings:
                raise ValueError('Portal graph/configuration crossing limit mismatch')
            if (
                self.r120_compiled_geometry_id
                != self.portal_graph.r120_compiled_geometry_id
                or self.r120_compiled_geometry_sha256
                != self.portal_graph.r120_compiled_geometry_sha256
            ):
                raise ValueError('Portal graph R120 geometry identity mismatch')
            aperture_by_id = {item.aperture_id: item for item in self.portal_apertures}
            if len(aperture_by_id) != len(self.portal_apertures):
                raise ValueError('execution input contains duplicate Portal aperture identity')
            if tuple(sorted(aperture_by_id)) != tuple(
                sorted(item.aperture_id for item in self.portal_graph.directed_edges)
            ):
                raise ValueError('Portal graph/aperture identity set mismatch')
            for edge in self.portal_graph.directed_edges:
                aperture = aperture_by_id[edge.aperture_id]
                if (
                    aperture.portal_id != edge.portal_id
                    or aperture.semantic_sha256 != edge.aperture_sha256
                    or aperture.from_region_id != edge.from_region_id
                    or aperture.to_region_id != edge.to_region_id
                ):
                    raise ValueError('Portal graph edge does not reproduce exact aperture authority')
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
        if self.portal_graph is None:
            payload.pop('portal_graph', None)
        if self.region_declarations is None:
            payload.pop('region_declarations', None)
        if self.maximum_portal_crossings is None:
            payload.pop('maximum_portal_crossings', None)
        if self.incidence_exact_angle_tolerance_deg is None:
            payload.pop('incidence_exact_angle_tolerance_deg', None)
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
    model_config = ConfigDict(frozen=True, extra='forbid', revalidate_instances='never')

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

BoundaryIncidenceEvaluation = Literal[
    'angle_specific_exact',
    'declared_condition_match',
    'scalar_coefficient_all_angles_v1',
]

class BoundaryMaterialContribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', revalidate_instances='never')

    source_surface_id: str
    material_authority: ExactExternalAuthorityRef
    boundary_physics_authority: ExactExternalAuthorityRef | None = None
    frequency_hz: float = Field(gt=0.0)
    absorption: float = Field(ge=0.0, le=1.0)
    scattering: float = Field(ge=0.0, le=1.0)
    specular_energy_factor: float = Field(ge=0.0, le=1.0)
    coherent_reflection_phase: Literal['UNAVAILABLE'] = 'UNAVAILABLE'
    incidence_angle_deg: float | None = Field(default=None, ge=0.0, le=90.0)
    incidence_cosine: float | None = Field(default=None, ge=0.0, le=1.0)
    coefficient_incidence_condition: GeometricIncidenceCondition | None = None
    incidence_evaluation: BoundaryIncidenceEvaluation | None = None

    @model_validator(mode='after')
    def valid_incidence_metadata(self) -> 'BoundaryMaterialContribution':
        parts = (
            self.incidence_angle_deg,
            self.incidence_cosine,
            self.coefficient_incidence_condition,
            self.incidence_evaluation,
        )
        if any(item is None for item in parts) and any(
            item is not None for item in parts
        ):
            raise ValueError(
                'boundary incidence metadata must be supplied together'
            )
        if self.incidence_angle_deg is not None and not isfinite(
            float(self.incidence_angle_deg)
        ):
            raise ValueError('boundary incidence angle must be finite')
        return self

class DeterministicPathBandQuantity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', revalidate_instances='never')

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

    model_config = ConfigDict(frozen=True, extra='forbid', revalidate_instances='never')

    kind: Literal['reflection', 'portal_crossing']
    point: Position3
    surface_id: str | None = None
    portal_id: str | None = None
    from_region_id: str | None = None
    to_region_id: str | None = None
    incidence_angle_deg: float | None = Field(default=None, ge=0.0, le=90.0)
    incidence_cosine: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode='after')
    def validate_interaction(self) -> 'DeterministicPathInteraction':
        if (self.incidence_angle_deg is None) != (self.incidence_cosine is None):
            raise ValueError(
                'reflection incidence angle and cosine must be supplied together'
            )
        if self.kind == 'reflection':
            if self.surface_id is None:
                raise ValueError('reflection interaction requires surface_id')
            if any(
                item is not None
                for item in (self.portal_id, self.from_region_id, self.to_region_id)
            ):
                raise ValueError('reflection interaction cannot carry Portal fields')
        else:
            if self.incidence_angle_deg is not None:
                raise ValueError('portal crossings carry no incidence metadata')
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

class PortalRegionSegmentEvidence(BaseModel):
    """Persisted proof result for one direct segment inside one AcousticRegion."""

    model_config = ConfigDict(frozen=True, extra='forbid', revalidate_instances='never')

    segment_index: int = Field(ge=0)
    region_id: str = Field(min_length=1)
    start_point: Position3
    end_point: Position3
    membership_result: Literal['valid'] = 'valid'
    occlusion_result: Literal['clear'] = 'clear'

class DeterministicAcousticPath(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', revalidate_instances='never')

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
    region_segment_evidence: tuple[PortalRegionSegmentEvidence, ...] | None = None
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
    execution_input_semantic_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_path(self) -> 'DeterministicAcousticPath':
        interaction_count = len(self.ordered_interaction_surface_ids)
        if interaction_count != len(self.ordered_interaction_points):
            raise ValueError('interaction surface/point sequence lengths must match')
        if self.path_type == 'direct':
            if interaction_count:
                raise ValueError('direct path cannot carry interaction surfaces')
        elif not 1 <= interaction_count <= 6:
            raise ValueError(
                'bounded specular reflection requires ordered surfaces within '
                'the declared six-order bound'
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
                if (
                    self.ordered_region_ids is None
                    or len(self.ordered_region_ids) != len(portals) + 1
                ):
                    raise ValueError(
                        'Portal path ordered region count must equal crossing count plus one'
                    )
                if len(set(self.ordered_region_ids)) != len(self.ordered_region_ids):
                    raise ValueError('Portal simple-path authority cannot repeat an AcousticRegion')
                portal_ids = tuple(item.portal_id for item in portals)
                if len(set(portal_ids)) != len(portal_ids):
                    raise ValueError('Portal simple-path authority cannot repeat a Portal')
                for index, portal in enumerate(portals):
                    if (portal.from_region_id, portal.to_region_id) != (
                        self.ordered_region_ids[index],
                        self.ordered_region_ids[index + 1],
                    ):
                        raise ValueError('Portal interaction region order mismatch')
                if reflections and self.path_type != 'specular_reflection':
                    raise ValueError(
                        'Portal direct paths cannot carry reflection interactions'
                    )
            elif self.ordered_region_ids is not None:
                if (
                    self.path_type != 'direct'
                    or reflections
                    or len(self.ordered_region_ids) != 1
                ):
                    raise ValueError(
                        'region-only direct path authority requires exactly one ordered region'
                    )
        elif self.ordered_region_ids is not None:
            raise ValueError('ordered_region_ids require typed interactions')

        if self.region_segment_evidence is not None:
            if self.ordered_region_ids is None:
                raise ValueError('region segment evidence requires ordered regions')
            # Each propagation segment lives in the region reached after its
            # preceding Portal crossings; a reflection duplicates its slot
            # region between the adjacent segments.
            reflection_slots: list[int] = []
            portal_seen = 0
            for item in self.ordered_interactions or ():
                if item.kind == 'reflection':
                    reflection_slots.append(portal_seen)
                else:
                    portal_seen += 1
            expected_segment_regions = _portal_segment_region_ids(
                self.ordered_region_ids,
                reflection_slots,
            )
            if len(self.region_segment_evidence) != len(expected_segment_regions):
                raise ValueError(
                    'region segment evidence count does not match ordered path topology'
                )
            for index, (evidence, expected_region_id) in enumerate(
                zip(self.region_segment_evidence, expected_segment_regions, strict=True)
            ):
                if evidence.segment_index != index:
                    raise ValueError(
                        'region segment evidence indices must be contiguous and ordered'
                    )
                if evidence.region_id != expected_region_id:
                    raise ValueError(
                        'region segment evidence identity does not match physical event order'
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
                        'multi-surface specular reflection requires ordered '
                        'boundary material contributions'
                    )
                if len(band.boundary_materials) != interaction_count:
                    raise ValueError(
                        'ordered boundary material contributions must match the '
                        'specular interaction count'
                    )
                if tuple(
                    item.source_surface_id for item in band.boundary_materials
                ) != self.ordered_interaction_surface_ids:
                    raise ValueError(
                        'ordered boundary material contributions must match '
                        'ordered surfaces'
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
        else:
            payload['ordered_interactions'] = [
                {
                    key: value
                    for key, value in interaction.items()
                    if value is not None
                }
                for interaction in payload['ordered_interactions']
            ]
        if self.ordered_region_ids is None:
            payload.pop('ordered_region_ids', None)
        if self.region_segment_evidence is None:
            payload.pop('region_segment_evidence', None)
        if self.execution_input_semantic_sha256 is None:
            payload.pop('execution_input_semantic_sha256', None)
        payload['bands'] = [
            _prune_band_payload(band) for band in payload['bands']
        ]
        return payload

class RejectedPathCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', revalidate_instances='never')

    source_entity_id: str
    receiver_id: str
    path_type: PathType
    interaction_surface_ids: tuple[str, ...] = ()
    decision: PathCandidateDecision
    reason: str = Field(min_length=1)

class DeterministicPathArtifact(BaseModel):
    """Immutable phase-free direct/bounded-order specular path artifact."""

    model_config = ConfigDict(frozen=True, extra='forbid', revalidate_instances='never')

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
        'direct_bounded_portal_graph_propagation',
        'single_portal_first_order_specular',
        'multi_portal_first_order_specular',
        'multi_portal_second_order_specular',
        'multi_portal_third_order_specular',
        'multi_portal_fourth_order_specular',
        'multi_portal_fifth_order_specular',
        'multi_portal_sixth_order_specular',
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

DETERMINISTIC_PATHS_OBSERVABLE = 'deterministic_paths'

_INCIDENCE_CONTRIBUTION_KEYS = (
    'incidence_angle_deg',
    'incidence_cosine',
    'coefficient_incidence_condition',
    'incidence_evaluation',
)

def _prune_band_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a serialized band payload for content hashing.

    Optional boundary-contribution incidence metadata is omitted when unset so
    payloads persisted before incidence evaluation existed keep their exact
    content hash.
    """
    if payload.get('boundary_materials') is None:
        payload.pop('boundary_materials', None)
    for key in ('boundary_material', 'boundary_materials'):
        value = payload.get(key)
        if value is None:
            continue
        entries = value if isinstance(value, list) else [value]
        for entry in entries:
            for field in _INCIDENCE_CONTRIBUTION_KEYS:
                if entry.get(field) is None:
                    entry.pop(field, None)
    return payload

@lru_cache(maxsize=512)
def _frozen_model_json_payload(model: BaseModel) -> dict[str, Any]:
    """JSON-mode ``model_dump`` of a frozen model, memoized by content.

    Frozen models hash by field values, so the cache is content-keyed and
    deterministic. Callers must treat the returned dict as read-only — it
    feeds only ``core`` digests (canonical_json reads, never mutates).
    """
    return model.model_dump(mode='json')
