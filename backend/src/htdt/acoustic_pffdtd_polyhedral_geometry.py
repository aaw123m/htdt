from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_solver_result import (
    AcousticSolverObservableArtifact,
    AcousticSolverResultEnvelope,
    build_acoustic_solver_result_envelope,
)
from .cad_candidate_wave_execution import (
    COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
    PFFDTD_CANDIDATE_POLYHEDRAL_INPUT_AUTHORITY_VERSION,
    CandidateBoundaryBinding,
    CandidatePolyhedralGeometryBinding,
    CandidateWaveExecutionError,
    CandidateWaveExecutionInput,
    ExactJsonAuthorityStore,
    PffdtdCandidateConfiguration,
    PffdtdCandidateWaveExecutor,
)
from .cad_pffdtd_resource_estimator import PffdtdCandidateResourceEstimator
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .r120_polyhedral_geometry import (
    R120PolyhedralCompiledGeometry,
    R120PolyhedralSemanticGeometry,
)


PFFDTD_POLYHEDRAL_GEOMETRY_ADAPTER_ID = (
    'htdt.r130d.pffdtd_polyhedral_geometry_adapter'
)
PFFDTD_POLYHEDRAL_GEOMETRY_ADAPTER_VERSION = '1'
PFFDTD_POLYHEDRAL_CONTAINMENT_ID = (
    'htdt.r130d.oriented_triangle_solid_angle_containment'
)
PFFDTD_POLYHEDRAL_CONTAINMENT_VERSION = '1'
PFFDTD_POLYHEDRAL_GRID_ID = 'bsxfun.pffdtd.cart_grid.offset_3_5'
PFFDTD_POLYHEDRAL_GRID_VERSION = (
    'aa319f6c86517cb95aabfae8656277da62c3ead5'
)
PFFDTD_POLYHEDRAL_TRIANGLE_INTERSECTION_RULE = (
    'bsxfun.pffdtd.common.tri_box_intersection.'
    'tri_box_intersection_vec@aa319f6c86517cb95aabfae8656277da62c3ead5'
)
PFFDTD_POLYHEDRAL_REPRESENTATION_VERSION = (
    'r130d-pffdtd-polyhedral-geometry-1'
)
PFFDTD_POLYHEDRAL_EXECUTED_GRID_VERSION = (
    'r130d-pffdtd-executed-grid-1'
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _exact_ref(
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


def r130d_snapshot_geometry_identity_prefix(
    snapshot_id: str,
    snapshot_sha256: str,
) -> str:
    return f'htdt:r130d:snapshot:{snapshot_id}:{snapshot_sha256}:source:'


def r130d_snapshot_geometry_identity(
    snapshot_id: str,
    snapshot_sha256: str,
    source_key: str,
) -> str:
    if not source_key:
        raise ValueError('R130D polyhedral source key must be non-empty')
    return (
        r130d_snapshot_geometry_identity_prefix(snapshot_id, snapshot_sha256)
        + source_key
    )


def semantic_geometry_ref(
    geometry: R120PolyhedralSemanticGeometry,
) -> ExactExternalAuthorityRef:
    return _exact_ref(
        authority_id=geometry.geometry_id,
        authority_version=geometry.authority_version,
        semantic_hash_sha256=geometry.semantic_hash_sha256,
    )


def compiled_geometry_ref(
    geometry: R120PolyhedralCompiledGeometry,
) -> ExactExternalAuthorityRef:
    return _exact_ref(
        authority_id=geometry.compiled_geometry_id,
        authority_version=geometry.authority_version,
        semantic_hash_sha256=geometry.compiled_hash_sha256,
    )


def register_r120b_polyhedral_authorities(
    store: ExactJsonAuthorityStore,
    *,
    semantic: R120PolyhedralSemanticGeometry,
    compiled: R120PolyhedralCompiledGeometry,
) -> tuple[ExactExternalAuthorityRef, ExactExternalAuthorityRef]:
    semantic_ref = semantic_geometry_ref(semantic)
    compiled_ref = compiled_geometry_ref(compiled)
    store.put_exact_json(
        semantic_ref,
        semantic.model_dump(
            mode='json',
            exclude={'geometry_id', 'semantic_hash_sha256'},
        ),
    )
    store.put_exact_json(
        compiled_ref,
        compiled.model_dump(
            mode='json',
            exclude={'compiled_geometry_id', 'compiled_hash_sha256'},
        ),
    )
    return semantic_ref, compiled_ref


def load_r120b_polyhedral_authorities(
    store: ExactJsonAuthorityStore,
    *,
    semantic_ref: ExactExternalAuthorityRef,
    compiled_ref: ExactExternalAuthorityRef,
) -> tuple[R120PolyhedralSemanticGeometry, R120PolyhedralCompiledGeometry]:
    try:
        semantic_payload = store.read_payload(semantic_ref)
        compiled_payload = store.read_payload(compiled_ref)
        semantic = R120PolyhedralSemanticGeometry.model_validate(
            {
                **semantic_payload,
                'geometry_id': semantic_ref.authority_id,
                'semantic_hash_sha256': semantic_ref.semantic_hash_sha256,
            }
        )
        compiled = R120PolyhedralCompiledGeometry.model_validate(
            {
                **compiled_payload,
                'compiled_geometry_id': compiled_ref.authority_id,
                'compiled_hash_sha256': compiled_ref.semantic_hash_sha256,
            }
        )
    except Exception as exc:
        raise CandidateWaveExecutionError(
            'R120B exact polyhedral authority is missing, stale, or modified'
        ) from exc
    if (
        compiled.exact_semantic_geometry_id != semantic.geometry_id
        or compiled.exact_semantic_geometry_hash_sha256
        != semantic.semantic_hash_sha256
    ):
        raise CandidateWaveExecutionError(
            'R120B compiled polyhedron does not bind the exact semantic polyhedron'
        )
    return semantic, compiled


class PolyhedralPointContainment(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    label: str = Field(min_length=1)
    position_m: tuple[float, float, float]
    classification: Literal['inside']


class PffdtdPolyhedralSurfaceMaterialBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_surface_id: str = Field(min_length=1)
    source_surface_key: str = Field(min_length=1)
    material_authority_ref: ExactExternalAuthorityRef


class PffdtdPolyhedralGeometryRepresentation(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r130d-pffdtd-polyhedral-geometry-1'
    ] = PFFDTD_POLYHEDRAL_REPRESENTATION_VERSION
    representation_id: str = Field(
        pattern=r'^r130d-pffdtd-polyhedral-geometry:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    adapter_id: Literal[
        'htdt.r130d.pffdtd_polyhedral_geometry_adapter'
    ] = PFFDTD_POLYHEDRAL_GEOMETRY_ADAPTER_ID
    adapter_version: Literal['1'] = PFFDTD_POLYHEDRAL_GEOMETRY_ADAPTER_VERSION

    source_geometry_identity: str = Field(min_length=1)
    exact_semantic_geometry_ref: ExactExternalAuthorityRef
    exact_compiled_geometry_ref: ExactExternalAuthorityRef
    exact_topology_report_id: str = Field(min_length=1)
    exact_topology_report_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_tolerance_m: float = Field(gt=0.0)
    region_id: str = Field(min_length=1)

    point_count: int = Field(ge=4)
    triangle_count: int = Field(ge=4)
    surface_material_bindings: tuple[
        PffdtdPolyhedralSurfaceMaterialBinding, ...
    ]
    canonical_triangle_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_model_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    generated_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    bounding_box_min_m: tuple[float, float, float]
    bounding_box_max_m: tuple[float, float, float]
    grid_origin_m: tuple[float, float, float]
    grid_spacing_m: float = Field(gt=0.0)
    grid_dimensions: tuple[int, int, int]
    grid_cells: int = Field(ge=1)
    grid_offset_cells: Literal[3.5] = 3.5
    grid_algorithm_id: Literal[
        'bsxfun.pffdtd.cart_grid.offset_3_5'
    ] = PFFDTD_POLYHEDRAL_GRID_ID
    grid_algorithm_version: Literal[
        'aa319f6c86517cb95aabfae8656277da62c3ead5'
    ] = PFFDTD_POLYHEDRAL_GRID_VERSION

    interior_classification_rule: Literal[
        'boundary_distance_then_oriented_triangle_solid_angle'
    ] = 'boundary_distance_then_oriented_triangle_solid_angle'
    containment_algorithm_id: Literal[
        'htdt.r130d.oriented_triangle_solid_angle_containment'
    ] = PFFDTD_POLYHEDRAL_CONTAINMENT_ID
    containment_algorithm_version: Literal[
        '1'
    ] = PFFDTD_POLYHEDRAL_CONTAINMENT_VERSION
    containment_tolerance_m: float = Field(gt=0.0)
    triangle_intersection_rule: Literal[
        'bsxfun.pffdtd.common.tri_box_intersection.tri_box_intersection_vec@aa319f6c86517cb95aabfae8656277da62c3ead5'
    ] = PFFDTD_POLYHEDRAL_TRIANGLE_INTERSECTION_RULE
    source_containment: PolyhedralPointContainment
    receiver_containment: tuple[PolyhedralPointContainment, ...] = Field(
        min_length=1
    )

    @model_validator(mode='after')
    def validate_identity(self) -> 'PffdtdPolyhedralGeometryRepresentation':
        if any(item <= 0 for item in self.grid_dimensions):
            raise ValueError('polyhedral PFFDTD grid dimensions must be positive')
        if math.prod(self.grid_dimensions) != self.grid_cells:
            raise ValueError('polyhedral PFFDTD grid cell count mismatch')
        expected = _digest(self.semantic_payload())
        if expected != self.semantic_sha256:
            raise ValueError('polyhedral PFFDTD representation hash mismatch')
        if self.representation_id != (
            f'r130d-pffdtd-polyhedral-geometry:{expected}'
        ):
            raise ValueError('polyhedral PFFDTD representation id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'representation_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return _exact_ref(
            authority_id=self.representation_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


class PffdtdExecutedGridGeometry(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r130d-pffdtd-executed-grid-1'
    ] = PFFDTD_POLYHEDRAL_EXECUTED_GRID_VERSION
    grid_identity_id: str = Field(
        pattern=r'^r130d-pffdtd-executed-grid:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_geometry_ref: ExactExternalAuthorityRef
    origin_m: tuple[float, float, float]
    spacing_m: float = Field(gt=0.0)
    dimensions: tuple[int, int, int]
    cart_grid_logical_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    boundary_mask_logical_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    boundary_node_count: int = Field(ge=1)
    triangle_intersection_rule: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'PffdtdExecutedGridGeometry':
        expected = _digest(self.semantic_payload())
        if expected != self.semantic_sha256:
            raise ValueError('executed PFFDTD grid identity hash mismatch')
        if self.grid_identity_id != f'r130d-pffdtd-executed-grid:{expected}':
            raise ValueError('executed PFFDTD grid identity id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'grid_identity_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return _exact_ref(
            authority_id=self.grid_identity_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def _sub(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        left[0] - right[0],
        left[1] - right[1],
        left[2] - right[2],
    )


def _dot(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def _cross(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _norm(value: tuple[float, float, float]) -> float:
    return math.sqrt(_dot(value, value))


def _canonical_oriented_triangle(
    triangle: tuple[int, int, int],
) -> tuple[int, int, int]:
    a, b, c = triangle
    return min((a, b, c), (b, c, a), (c, a, b))


def canonical_polyhedral_triangles(
    compiled: R120PolyhedralCompiledGeometry,
) -> tuple[tuple[str, str, int, int, int], ...]:
    values = tuple(
        sorted(
            (
                item.source_surface_id,
                item.source_surface_key,
                *_canonical_oriented_triangle((item.a, item.b, item.c)),
            )
            for item in compiled.triangles
        )
    )
    if len(values) != len(set(values)):
        raise CandidateWaveExecutionError(
            'R120B compiled polyhedron contains duplicate canonical triangles'
        )
    return values


def _point_on_triangle(
    point: tuple[float, float, float],
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
    tolerance_m: float,
) -> bool:
    ab = _sub(b, a)
    ac = _sub(c, a)
    ap = _sub(point, a)
    normal = _cross(ab, ac)
    normal_length = _norm(normal)
    if normal_length <= tolerance_m * tolerance_m:
        raise CandidateWaveExecutionError(
            'R120B compiled polyhedron contains a degenerate triangle'
        )
    distance = abs(_dot(ap, normal)) / normal_length
    if distance > tolerance_m:
        return False

    d00 = _dot(ab, ab)
    d01 = _dot(ab, ac)
    d11 = _dot(ac, ac)
    d20 = _dot(ap, ab)
    d21 = _dot(ap, ac)
    denominator = d00 * d11 - d01 * d01
    if abs(denominator) <= 1.0e-30:
        raise CandidateWaveExecutionError(
            'R120B compiled polyhedron contains a degenerate triangle'
        )
    v = (d11 * d20 - d01 * d21) / denominator
    w = (d00 * d21 - d01 * d20) / denominator
    u = 1.0 - v - w
    scale = max(_norm(ab), _norm(ac), _norm(_sub(c, b)), tolerance_m)
    barycentric_tolerance = min(0.25, tolerance_m / scale)
    return (
        u >= -barycentric_tolerance
        and v >= -barycentric_tolerance
        and w >= -barycentric_tolerance
        and u <= 1.0 + barycentric_tolerance
        and v <= 1.0 + barycentric_tolerance
        and w <= 1.0 + barycentric_tolerance
    )


def classify_point_in_closed_polyhedron(
    *,
    point_m: tuple[float, float, float],
    vertices: tuple[tuple[float, float, float], ...],
    triangles: Sequence[tuple[int, int, int]],
    tolerance_m: float,
) -> Literal['inside', 'outside', 'boundary_or_ambiguous']:
    if tolerance_m <= 0.0 or not math.isfinite(tolerance_m):
        raise ValueError('polyhedral containment tolerance must be finite/positive')

    for a_index, b_index, c_index in triangles:
        if _point_on_triangle(
            point_m,
            vertices[a_index],
            vertices[b_index],
            vertices[c_index],
            tolerance_m,
        ):
            return 'boundary_or_ambiguous'

    solid_angle = 0.0
    for a_index, b_index, c_index in triangles:
        a = _sub(vertices[a_index], point_m)
        b = _sub(vertices[b_index], point_m)
        c = _sub(vertices[c_index], point_m)
        la = _norm(a)
        lb = _norm(b)
        lc = _norm(c)
        if min(la, lb, lc) <= tolerance_m:
            return 'boundary_or_ambiguous'
        numerator = _dot(a, _cross(b, c))
        denominator = (
            la * lb * lc
            + _dot(a, b) * lc
            + _dot(b, c) * la
            + _dot(c, a) * lb
        )
        solid_angle += 2.0 * math.atan2(numerator, denominator)

    magnitude = abs(solid_angle)
    if magnitude > 2.0 * math.pi:
        return 'inside'
    if magnitude < math.pi:
        return 'outside'
    return 'boundary_or_ambiguous'


def _planned_pffdtd_grid(
    *,
    points: tuple[tuple[float, float, float], ...],
    sound_speed_m_s: float,
    fmax_hz: float,
    points_per_wavelength: float,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    float,
    tuple[int, int, int],
]:
    if (
        not math.isfinite(sound_speed_m_s)
        or sound_speed_m_s <= 0.0
        or not math.isfinite(fmax_hz)
        or fmax_hz <= 0.0
        or not math.isfinite(points_per_wavelength)
        or points_per_wavelength <= 0.0
    ):
        raise CandidateWaveExecutionError(
            'polyhedral PFFDTD grid inputs must be finite and positive'
        )
    mins = tuple(min(point[axis] for point in points) for axis in range(3))
    maxs = tuple(max(point[axis] for point in points) for axis in range(3))
    if any(maxs[axis] <= mins[axis] for axis in range(3)):
        raise CandidateWaveExecutionError(
            'polyhedral PFFDTD geometry bounding box has zero extent'
        )
    spacing = sound_speed_m_s / (fmax_hz * points_per_wavelength)
    origin = tuple(value - 3.5 * spacing for value in mins)
    dimensions = tuple(
        int(math.ceil((maxs[axis] - mins[axis] + 7.0 * spacing) / spacing))
        + 1
        for axis in range(3)
    )
    return mins, maxs, origin, spacing, dimensions


def compile_r120b_polyhedral_to_pffdtd(
    *,
    semantic: R120PolyhedralSemanticGeometry,
    compiled: R120PolyhedralCompiledGeometry,
    source_position_m: tuple[float, float, float],
    receivers: tuple[tuple[str, tuple[float, float, float]], ...],
    sound_speed_m_s: float,
    configuration: PffdtdCandidateConfiguration,
    containment_tolerance_m: float = 1.0e-9,
) -> tuple[PffdtdPolyhedralGeometryRepresentation, dict[str, Any]]:
    if semantic.source_geometry_kind != 'explicit_polyhedral':
        raise CandidateWaveExecutionError(
            'UNSUPPORTED R130D wave geometry: bounded/curved approximation is not '
            'authorized in the exact-polyhedron execution slice'
        )
    if semantic.approximation_authority or compiled.approximation_authority:
        raise CandidateWaveExecutionError(
            'UNSUPPORTED R130D wave geometry: approximation authority is non-empty'
        )
    if len(semantic.air_volumes) != 1:
        raise CandidateWaveExecutionError(
            'UNSUPPORTED R130D wave geometry: multiple AcousticRegions'
        )
    if semantic.portals:
        raise CandidateWaveExecutionError(
            'UNSUPPORTED R130D wave geometry: Portal-bearing geometry'
        )
    if compiled.readiness.wave_representation != 'READY':
        raise CandidateWaveExecutionError(
            'UNSUPPORTED R130D wave geometry: R120B wave representation is not READY'
        )
    if any(item.holes_present for item in compiled.surface_mapping):
        raise CandidateWaveExecutionError(
            'UNSUPPORTED R130D wave geometry: polygon holes'
        )
    if (
        compiled.exact_semantic_geometry_id != semantic.geometry_id
        or compiled.exact_semantic_geometry_hash_sha256
        != semantic.semantic_hash_sha256
    ):
        raise CandidateWaveExecutionError(
            'R120B semantic/compiled geometry identity mismatch'
        )
    if not receivers:
        raise CandidateWaveExecutionError(
            'R130D polyhedral execution requires at least one receiver'
        )

    volume = semantic.air_volumes[0]
    boundary_keys = set(volume.boundary_surface_keys)
    mapping_keys = {item.source_surface_key for item in compiled.surface_mapping}
    triangle_keys = {item.source_surface_key for item in compiled.triangles}
    if mapping_keys != boundary_keys or triangle_keys != boundary_keys:
        raise CandidateWaveExecutionError(
            'R120B compiled triangles do not exactly cover the single AcousticRegion'
        )

    points = tuple(vertex.point() for vertex in compiled.vertices)
    canonical = canonical_polyhedral_triangles(compiled)
    triangle_indices = tuple(
        (a, b, c) for _, _, a, b, c in canonical
    )
    if len(triangle_indices) < 4:
        raise CandidateWaveExecutionError(
            'R120B polyhedral execution requires at least four triangles'
        )

    signed_volume = sum(
        _dot(points[a], _cross(points[b], points[c])) / 6.0
        for a, b, c in triangle_indices
    )
    if signed_volume <= containment_tolerance_m ** 3:
        raise CandidateWaveExecutionError(
            'R120B polyhedron is not a consistently outward closed positive volume'
        )

    source_classification = classify_point_in_closed_polyhedron(
        point_m=source_position_m,
        vertices=points,
        triangles=triangle_indices,
        tolerance_m=containment_tolerance_m,
    )
    if source_classification != 'inside':
        raise CandidateWaveExecutionError(
            'source acoustic reference must be strictly inside exact R120B polyhedron; '
            f'classification={source_classification}'
        )
    receiver_evidence: list[PolyhedralPointContainment] = []
    for receiver_id, position in receivers:
        classification = classify_point_in_closed_polyhedron(
            point_m=position,
            vertices=points,
            triangles=triangle_indices,
            tolerance_m=containment_tolerance_m,
        )
        if classification != 'inside':
            raise CandidateWaveExecutionError(
                f'receiver {receiver_id} must be strictly inside exact R120B '
                f'polyhedron; classification={classification}'
            )
        receiver_evidence.append(
            PolyhedralPointContainment(
                label=receiver_id,
                position_m=position,
                classification='inside',
            )
        )

    surface_material_bindings = tuple(
        PffdtdPolyhedralSurfaceMaterialBinding(
            source_surface_id=item.source_surface_id,
            source_surface_key=item.source_surface_key,
            material_authority_ref=_exact_ref(
                authority_id=item.material_authority.authority_id,
                authority_version=item.material_authority.authority_version,
                semantic_hash_sha256=item.material_authority.semantic_hash_sha256,
            ),
        )
        for item in sorted(
            compiled.surface_mapping,
            key=lambda value: (value.source_surface_key, value.source_surface_id),
        )
    )

    triangles = [[a, b, c] for a, b, c in triangle_indices]
    model = {
        'mats_hash': {
            '_RIGID': {
                'tris': triangles,
                'pts': [list(point) for point in points],
                'color': [220, 220, 220],
                'sides': [0] * len(triangles),
            }
        },
        'sources': [
            {
                'xyz': list(source_position_m),
                'name': 'r130d-exact-polyhedral-source',
            }
        ],
        'receivers': [
            {'xyz': list(position), 'name': receiver_id}
            for receiver_id, position in receivers
        ],
        'export_datetime': 'HTDT R130D exact polyhedral deterministic compiler v1',
    }
    model_hash = _digest(model)
    geometry_hash = _digest(
        {
            'points_m': points,
            'canonical_oriented_triangles': canonical,
            'surface_material_bindings': [
                item.model_dump(mode='json')
                for item in surface_material_bindings
            ],
        }
    )

    mins, maxs, origin, spacing, dimensions = _planned_pffdtd_grid(
        points=points,
        sound_speed_m_s=sound_speed_m_s,
        fmax_hz=float(configuration.fmax_hz),
        points_per_wavelength=float(configuration.points_per_wavelength),
    )
    grid_cells = math.prod(dimensions)
    if grid_cells > configuration.resource.max_grid_cells:
        raise CandidateWaveExecutionError(
            'R130D polyhedral grid exceeds resource ceiling before PFFDTD setup: '
            f'{grid_cells} > {configuration.resource.max_grid_cells}'
        )

    core = {
        'authority_version': PFFDTD_POLYHEDRAL_REPRESENTATION_VERSION,
        'adapter_id': PFFDTD_POLYHEDRAL_GEOMETRY_ADAPTER_ID,
        'adapter_version': PFFDTD_POLYHEDRAL_GEOMETRY_ADAPTER_VERSION,
        'source_geometry_identity': semantic.source_geometry_identity,
        'exact_semantic_geometry_ref': semantic_geometry_ref(semantic).model_dump(
            mode='json'
        ),
        'exact_compiled_geometry_ref': compiled_geometry_ref(compiled).model_dump(
            mode='json'
        ),
        'exact_topology_report_id': compiled.exact_topology_report_id,
        'exact_topology_report_hash_sha256': (
            compiled.exact_topology_report_hash_sha256
        ),
        'topology_identity_sha256': compiled.topology_identity_sha256,
        'topology_tolerance_m': float(
            _topology_tolerance_from_semantic_and_compiled(semantic, compiled)
        ),
        'region_id': volume.region_id,
        'point_count': len(points),
        'triangle_count': len(triangle_indices),
        'surface_material_bindings': [
            item.model_dump(mode='json') for item in surface_material_bindings
        ],
        'canonical_triangle_sha256': _digest(canonical),
        'solver_model_sha256': model_hash,
        'generated_geometry_sha256': geometry_hash,
        'bounding_box_min_m': list(mins),
        'bounding_box_max_m': list(maxs),
        'grid_origin_m': list(origin),
        'grid_spacing_m': spacing,
        'grid_dimensions': list(dimensions),
        'grid_cells': grid_cells,
        'grid_offset_cells': 3.5,
        'grid_algorithm_id': PFFDTD_POLYHEDRAL_GRID_ID,
        'grid_algorithm_version': PFFDTD_POLYHEDRAL_GRID_VERSION,
        'interior_classification_rule': (
            'boundary_distance_then_oriented_triangle_solid_angle'
        ),
        'containment_algorithm_id': PFFDTD_POLYHEDRAL_CONTAINMENT_ID,
        'containment_algorithm_version': PFFDTD_POLYHEDRAL_CONTAINMENT_VERSION,
        'containment_tolerance_m': float(containment_tolerance_m),
        'triangle_intersection_rule': (
            PFFDTD_POLYHEDRAL_TRIANGLE_INTERSECTION_RULE
        ),
        'source_containment': PolyhedralPointContainment(
            label='source',
            position_m=source_position_m,
            classification='inside',
        ).model_dump(mode='json'),
        'receiver_containment': [
            item.model_dump(mode='json') for item in receiver_evidence
        ],
    }
    digest = _digest(core)
    representation = PffdtdPolyhedralGeometryRepresentation(
        representation_id=f'r130d-pffdtd-polyhedral-geometry:{digest}',
        semantic_sha256=digest,
        **core,
    )
    return representation, model


def _topology_tolerance_from_semantic_and_compiled(
    semantic: R120PolyhedralSemanticGeometry,
    compiled: R120PolyhedralCompiledGeometry,
) -> float:
    # R120B v1 deliberately stores the topology report hash/id rather than
    # duplicating its tolerance in the compiled authority. Exact planar input
    # carries no approximation tolerance, so R130D records the compiler's
    # current explicit v1 default here and binds it into its own authority.
    del semantic, compiled
    return 1.0e-9


def _logical_h5_hash(handle: Any, names: Sequence[str]) -> str:
    digest = sha256()
    for name in names:
        value = handle[name][...]
        digest.update(name.encode('utf-8'))
        digest.update(str(value.dtype).encode('ascii'))
        digest.update(str(tuple(value.shape)).encode('ascii'))
        digest.update(value.tobytes(order='C'))
    return digest.hexdigest()


def _read_executed_grid(
    *,
    run_dir: Path,
    representation: PffdtdPolyhedralGeometryRepresentation,
) -> PffdtdExecutedGridGeometry:
    try:
        import h5py
    except Exception as exc:
        raise CandidateWaveExecutionError(
            'h5py is required to bind exact PFFDTD grid/voxel provenance'
        ) from exc

    cart_path = run_dir / 'sim' / 'cart_grid.h5'
    voxel_path = run_dir / 'sim' / 'vox_out.h5'
    if not cart_path.is_file() or not voxel_path.is_file():
        raise CandidateWaveExecutionError(
            'PFFDTD did not persist exact cart_grid.h5/vox_out.h5 geometry assets'
        )
    with h5py.File(cart_path, 'r') as handle:
        xv = handle['xv'][...]
        yv = handle['yv'][...]
        zv = handle['zv'][...]
        spacing = float(handle['h'][()])
        cart_hash = _logical_h5_hash(handle, ('h', 'xv', 'yv', 'zv'))
    origin = (float(xv[0]), float(yv[0]), float(zv[0]))
    dimensions = (int(xv.size), int(yv.size), int(zv.size))

    with h5py.File(voxel_path, 'r') as handle:
        voxel_hash = _logical_h5_hash(
            handle,
            (
                'Nb',
                'Nx',
                'Ny',
                'Nz',
                'adj_bn',
                'bn_ixyz',
                'h',
                'mat_bn',
                'saf_bn',
                'xv',
                'yv',
                'zv',
            ),
        )
        boundary_node_count = int(handle['Nb'][()])

    if dimensions != representation.grid_dimensions:
        raise CandidateWaveExecutionError(
            'actual PFFDTD grid dimensions differ from deterministic R130D plan'
        )
    if not math.isclose(
        spacing,
        representation.grid_spacing_m,
        rel_tol=0.0,
        abs_tol=1.0e-12,
    ):
        raise CandidateWaveExecutionError(
            'actual PFFDTD grid spacing differs from deterministic R130D plan'
        )
    for actual, planned in zip(origin, representation.grid_origin_m, strict=True):
        if not math.isclose(actual, planned, rel_tol=0.0, abs_tol=1.0e-12):
            raise CandidateWaveExecutionError(
                'actual PFFDTD grid origin differs from deterministic R130D plan'
            )
    if boundary_node_count < 1:
        raise CandidateWaveExecutionError(
            'PFFDTD polyhedral voxelization produced no boundary nodes'
        )

    core = {
        'authority_version': PFFDTD_POLYHEDRAL_EXECUTED_GRID_VERSION,
        'solver_geometry_ref': representation.as_external_ref().model_dump(
            mode='json'
        ),
        'origin_m': list(origin),
        'spacing_m': spacing,
        'dimensions': list(dimensions),
        'cart_grid_logical_sha256': cart_hash,
        'boundary_mask_logical_sha256': voxel_hash,
        'boundary_node_count': boundary_node_count,
        'triangle_intersection_rule': PFFDTD_POLYHEDRAL_TRIANGLE_INTERSECTION_RULE,
    }
    digest = _digest(core)
    return PffdtdExecutedGridGeometry(
        grid_identity_id=f'r130d-pffdtd-executed-grid:{digest}',
        semantic_sha256=digest,
        **core,
    )


class PffdtdPolyhedralCandidateWaveExecutor:
    """R130D exact-polyhedron lane over the established R130 CPU candidate."""

    def __init__(
        self,
        *,
        base_executor: PffdtdCandidateWaveExecutor,
        containment_tolerance_m: float = 1.0e-9,
    ) -> None:
        if containment_tolerance_m <= 0.0:
            raise ValueError('R130D containment tolerance must be positive')
        self.base_executor = base_executor
        self.containment_tolerance_m = float(containment_tolerance_m)

    def compile_input(
        self,
        *,
        dispatch_binding_id: str,
        configuration: PffdtdCandidateConfiguration,
        semantic_geometry_ref: ExactExternalAuthorityRef,
        compiled_geometry_ref: ExactExternalAuthorityRef,
        rigid_boundary_physics_ref: ExactExternalAuthorityRef,
    ) -> tuple[
        CandidateWaveExecutionInput,
        dict[str, Any],
        PffdtdPolyhedralGeometryRepresentation,
    ]:
        base_authority, base_model = self.base_executor.compile_input(
            dispatch_binding_id=dispatch_binding_id,
            configuration=configuration,
        )
        if any(
            item.impedance_mapping is not None or item.causal_mapping is not None
            for item in base_authority.boundary_bindings
        ):
            raise CandidateWaveExecutionError(
                'R130D initial polyhedral slice supports rigid boundary only'
            )

        semantic, compiled = load_r120b_polyhedral_authorities(
            self.base_executor.authority_store,
            semantic_ref=semantic_geometry_ref,
            compiled_ref=compiled_geometry_ref,
        )
        expected_prefix = r130d_snapshot_geometry_identity_prefix(
            base_authority.snapshot_id,
            base_authority.snapshot_sha256,
        )
        if not semantic.source_geometry_identity.startswith(expected_prefix):
            raise CandidateWaveExecutionError(
                'R120B semantic polyhedron is stale/unbound for this exact '
                'AcousticSceneSnapshot'
            )

        boundary_payload = self.base_executor._require_external(
            rigid_boundary_physics_ref,
            label='R130D rigid boundary physics',
        )
        if (
            not isinstance(boundary_payload, dict)
            or boundary_payload.get('authority_kind') != 'wave_boundary_physics'
            or boundary_payload.get('model') != 'rigid_zero_normal_velocity'
            or float(boundary_payload.get('normal_velocity_m_s', math.nan)) != 0.0
        ):
            raise CandidateWaveExecutionError(
                'R130D polyhedral slice requires exact rigid_zero_normal_velocity '
                'boundary authority'
            )

        if (
            len(base_model.get('sources', ())) != 1
            or len(base_authority.receivers) < 1
        ):
            raise CandidateWaveExecutionError(
                'R130D polyhedral candidate requires exactly one source and '
                'at least one receiver'
            )
        source_position = tuple(
            float(item) for item in base_model['sources'][0]['xyz']
        )
        receiver_positions = tuple(
            (item.receiver_id, tuple(float(value) for value in item.position_m))
            for item in base_authority.receivers
        )
        snapshot = self.base_executor.snapshot_repository.get_snapshot(
            base_authority.snapshot_id
        )
        if (
            snapshot is None
            or snapshot.semantic_sha256 != base_authority.snapshot_sha256
            or snapshot.environment is None
            or snapshot.environment.sound_speed_m_s is None
        ):
            raise CandidateWaveExecutionError(
                'R130D exact snapshot/environment authority disappeared or changed'
            )

        representation, model = compile_r120b_polyhedral_to_pffdtd(
            semantic=semantic,
            compiled=compiled,
            source_position_m=source_position,
            receivers=receiver_positions,
            sound_speed_m_s=float(snapshot.environment.sound_speed_m_s),
            configuration=configuration,
            containment_tolerance_m=self.containment_tolerance_m,
        )
        self.base_executor.authority_store.put_exact_json(
            representation.as_external_ref(),
            representation.semantic_payload(),
        )

        boundary_bindings: list[CandidateBoundaryBinding] = []
        for mapping in sorted(
            compiled.surface_mapping,
            key=lambda value: (value.source_surface_key, value.source_surface_id),
        ):
            material_ref = _exact_ref(
                authority_id=mapping.material_authority.authority_id,
                authority_version=mapping.material_authority.authority_version,
                semantic_hash_sha256=mapping.material_authority.semantic_hash_sha256,
            )
            self.base_executor._require_external(
                material_ref,
                label=f'R130D material {mapping.source_surface_id}',
            )
            boundary_bindings.append(
                CandidateBoundaryBinding(
                    source_surface_id=mapping.source_surface_id,
                    material_authority=material_ref,
                    boundary_physics_authority=rigid_boundary_physics_ref,
                )
            )

        treatment_hash = _digest([])
        boundary_hash = _digest(
            [
                item.model_dump(mode='json', exclude_none=True)
                for item in boundary_bindings
            ]
        )
        binding = CandidatePolyhedralGeometryBinding(
            scene_revision_id=snapshot.scene_revision_id,
            scene_revision_content_hash=snapshot.scene_content_hash,
            snapshot_id=snapshot.snapshot_id,
            snapshot_sha256=snapshot.semantic_sha256,
            semantic_geometry_ref=semantic_geometry_ref,
            compiled_geometry_ref=compiled_geometry_ref,
            solver_geometry_ref=representation.as_external_ref(),
            exact_topology_report_id=compiled.exact_topology_report_id,
            exact_topology_report_hash_sha256=(
                compiled.exact_topology_report_hash_sha256
            ),
            topology_identity_sha256=compiled.topology_identity_sha256,
            topology_tolerance_m=representation.topology_tolerance_m,
            region_id=representation.region_id,
            containment_algorithm_id=representation.containment_algorithm_id,
            containment_algorithm_version=(
                representation.containment_algorithm_version
            ),
            containment_tolerance_m=representation.containment_tolerance_m,
            grid_algorithm_id=representation.grid_algorithm_id,
            grid_algorithm_version=representation.grid_algorithm_version,
            grid_origin_m=representation.grid_origin_m,
            grid_spacing_m=representation.grid_spacing_m,
            grid_dimensions=representation.grid_dimensions,
            grid_geometry_sha256=representation.generated_geometry_sha256,
        )

        core = base_authority.semantic_payload()
        core.update(
            {
                'authority_version': (
                    PFFDTD_CANDIDATE_POLYHEDRAL_INPUT_AUTHORITY_VERSION
                ),
                'compiled_geometry_id': compiled.compiled_geometry_id,
                'compiled_geometry_sha256': compiled.compiled_hash_sha256,
                'compiled_topology_sha256': compiled.topology_identity_sha256,
                'material_boundary_configuration_sha256': boundary_hash,
                'boundary_bindings': [
                    item.model_dump(mode='json', exclude_none=True)
                    for item in boundary_bindings
                ],
                'treatment_boundary_composition_sha256': treatment_hash,
                'adapter_compiler_id': (
                    'htdt.r130d.pffdtd_polyhedral_input_compiler'
                ),
                'adapter_compiler_version': '4',
                'solver_model_sha256': representation.solver_model_sha256,
                'polyhedral_geometry_binding': binding.model_dump(mode='json'),
            }
        )
        digest = _digest(core)
        authority = CandidateWaveExecutionInput(
            execution_input_id=f'candidate-wave-input:{digest}',
            semantic_sha256=digest,
            **core,
        )
        return authority, model, representation

    def execute(
        self,
        *,
        dispatch_binding_id: str,
        configuration: PffdtdCandidateConfiguration,
        semantic_geometry_ref: ExactExternalAuthorityRef,
        compiled_geometry_ref: ExactExternalAuthorityRef,
        rigid_boundary_physics_ref: ExactExternalAuthorityRef,
    ) -> AcousticSolverResultEnvelope:
        authority, model, representation = self.compile_input(
            dispatch_binding_id=dispatch_binding_id,
            configuration=configuration,
            semantic_geometry_ref=semantic_geometry_ref,
            compiled_geometry_ref=compiled_geometry_ref,
            rigid_boundary_physics_ref=rigid_boundary_physics_ref,
        )
        snapshot = self.base_executor.snapshot_repository.get_snapshot(
            authority.snapshot_id
        )
        request = self.base_executor.snapshot_repository.get_prediction_request(
            authority.prediction_request_id
        )
        dispatch = self.base_executor.dispatch_repository.get_dispatch(
            authority.dispatch_binding_id
        )
        if snapshot is None or request is None or dispatch is None:
            raise CandidateWaveExecutionError(
                'R130D exact snapshot/request/dispatch chain disappeared'
            )
        if (
            snapshot.semantic_sha256 != authority.snapshot_sha256
            or dispatch.semantic_sha256 != authority.dispatch_binding_sha256
            or request.request_semantic_sha256
            != authority.prediction_request_sha256
        ):
            raise CandidateWaveExecutionError(
                'R130D exact snapshot/request/dispatch chain became stale'
            )
        assert snapshot.environment is not None
        assert snapshot.environment.sound_speed_m_s is not None

        estimation = PffdtdCandidateResourceEstimator().estimate(
            authority=authority,
            model=model,
            configuration=configuration,
            sound_speed_m_s=float(snapshot.environment.sound_speed_m_s),
        )
        workload = estimation.workload
        workload_ref = _exact_ref(
            authority_id=workload.workload_estimate_id,
            authority_version=workload.authority_version,
            semantic_hash_sha256=workload.semantic_sha256,
        )
        self.base_executor.authority_store.put_exact_json(
            workload_ref,
            workload.semantic_payload(),
        )

        wave_binding = self.base_executor.wave_excitation_repository.get_binding(
            authority.wave_excitation_binding_id
        )
        if wave_binding is None:
            raise CandidateWaveExecutionError(
                'R130D exact wave excitation binding disappeared'
            )
        excitation = self.base_executor.wave_excitation_repository.get_excitation(
            wave_binding.excitation_id
        )
        if excitation is None:
            raise CandidateWaveExecutionError(
                'R130D exact wave excitation authority disappeared'
            )

        numerical = self.base_executor._run_pffdtd(
            authority=authority,
            model=model,
            configuration=configuration,
            excitation=excitation,
            sound_speed_m_s=float(snapshot.environment.sound_speed_m_s),
            cancel_check=lambda: False,
        )
        executed_grid = _read_executed_grid(
            run_dir=self.base_executor.work_root / authority.semantic_sha256,
            representation=representation,
        )
        if numerical.grid_shape != executed_grid.dimensions:
            raise CandidateWaveExecutionError(
                'R130D numerical grid shape differs from exact executed-grid authority'
            )
        self.base_executor.authority_store.put_exact_json(
            executed_grid.as_external_ref(),
            executed_grid.semantic_payload(),
        )

        schema_payload = self.base_executor._require_external(
            self.base_executor.output_schema_ref,
            label='complex pressure artifact schema',
        )
        if (
            not isinstance(schema_payload, dict)
            or schema_payload.get('schema_version')
            != COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION
            or schema_payload.get('quantity_type') != 'complex_pressure'
        ):
            raise CandidateWaveExecutionError(
                'R130D complex-pressure artifact schema authority is incompatible'
            )

        execution_id = (
            f'r130d-candidate-polyhedral:{authority.semantic_sha256[:20]}:'
            f'{uuid4().hex}'
        )
        artifact_payload = {
            'schema_version': COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
            'quantity_type': 'complex_pressure',
            'complex_representation': {
                'form': 'cartesian_real_imag',
                'phasor_convention': 'exp(-i*omega*t)',
                'analysis_fourier_kernel': 'exp(+i*omega*t)',
            },
            'receiver_identity_order': [
                {
                    'receiver_id': item.receiver_id,
                    'entity_id': item.entity_id,
                    'position_m': list(item.position_m),
                }
                for item in authority.receivers
            ],
            'frequency_axis_hz': list(numerical.frequency_hz),
            'time_sampling': {
                'time_step_s': numerical.time_step_s,
                'sample_count': numerical.time_step_count,
                'finite_record_interval': '[0,T)',
                'requested_duration_s': authority.observation_time_s,
            },
            'units': 'Pa',
            'reference': (
                'absolute complex acoustic pressure from actual pinned PFFDTD '
                'CPU solve over exact R120B polyhedral triangle geometry'
            ),
            'valid_domain': request.requested_frequency_domain.model_dump(
                mode='json'
            ),
            'solver_execution_id': execution_id,
            'candidate_execution_input_id': authority.execution_input_id,
            'candidate_execution_input_sha256': authority.semantic_sha256,
            'polyhedral_geometry_binding': (
                authority.polyhedral_geometry_binding.model_dump(mode='json')
                if authority.polyhedral_geometry_binding is not None
                else None
            ),
            'solver_geometry_ref': representation.as_external_ref().model_dump(
                mode='json'
            ),
            'executed_grid_ref': executed_grid.as_external_ref().model_dump(
                mode='json'
            ),
            'resource_estimate_ref': workload_ref.model_dump(mode='json'),
            'source_authority': {
                'r110_compiled_source_sha256': (
                    authority.r110_compiled_source_sha256
                ),
                'wave_excitation_binding_sha256': (
                    authority.wave_excitation_binding_sha256
                ),
                'wave_excitation_sha256': authority.wave_excitation_sha256,
            },
            'solver_raw_asset': {
                'name': numerical.raw_solver_asset_name,
                'sha256': numerical.raw_solver_asset_sha256,
            },
            'pressure_real_pa': [
                list(row) for row in numerical.pressure_real_pa
            ],
            'pressure_imag_pa': [
                list(row) for row in numerical.pressure_imag_pa
            ],
        }
        artifact_ref = self.base_executor.authority_store.put_json(
            'r130d-acoustic-solver-artifact',
            COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
            artifact_payload,
        )

        provenance_payload = {
            'schema_version': (
                'htdt.r130d.polyhedral-candidate-execution-provenance-1'
            ),
            'execution_id': execution_id,
            'candidate_only': True,
            'production_solver_selected': False,
            'r130d_software_execution_completed': True,
            'r130d_general_3d_physics_validated': False,
            'r130_numerical_acceptance_completed': False,
            'scene_revision_id': snapshot.scene_revision_id,
            'scene_revision_content_hash': snapshot.scene_content_hash,
            'snapshot_id': snapshot.snapshot_id,
            'snapshot_sha256': snapshot.semantic_sha256,
            'execution_input_id': authority.execution_input_id,
            'execution_input_sha256': authority.semantic_sha256,
            'semantic_geometry_ref': semantic_geometry_ref.model_dump(
                mode='json'
            ),
            'compiled_geometry_ref': compiled_geometry_ref.model_dump(
                mode='json'
            ),
            'solver_geometry_ref': representation.as_external_ref().model_dump(
                mode='json'
            ),
            'executed_grid_ref': executed_grid.as_external_ref().model_dump(
                mode='json'
            ),
            'resource_estimate_ref': workload_ref.model_dump(mode='json'),
            'resource_estimate': workload.model_dump(mode='json'),
            'solver_implementation_ref': (
                dispatch.solver_implementation_ref.model_dump(mode='json')
            ),
            'solver_configuration_ref': (
                dispatch.solver_configuration_ref.model_dump(mode='json')
            ),
            'runtime_identity': authority.runtime_identity.model_dump(
                mode='json'
            ),
            'compiled_solver_model_sha256': authority.solver_model_sha256,
            'raw_solver_asset_sha256': numerical.raw_solver_asset_sha256,
            'grid_shape': list(numerical.grid_shape),
            'time_step_s': numerical.time_step_s,
            'time_step_count': numerical.time_step_count,
            'sound_speed_m_s': numerical.sound_speed_m_s,
            'timings_s': {
                'compile': numerical.compile_seconds,
                'solve': numerical.solve_seconds,
                'postprocess': numerical.postprocess_seconds,
            },
            'compatibility_patch': numerical.compatibility_patch,
        }
        provenance_ref = self.base_executor.authority_store.put_json(
            'solver-execution-provenance',
            'htdt.r130d.polyhedral-candidate-execution-provenance-1',
            provenance_payload,
        )

        artifact = AcousticSolverObservableArtifact(
            observable='complex_pressure',
            artifact_authority=artifact_ref,
            encoding_schema_ref=self.base_executor.output_schema_ref,
            valid_frequency_domain=request.requested_frequency_domain,
        )
        result = build_acoustic_solver_result_envelope(
            dispatch=dispatch,
            request=request,
            execution_id=execution_id,
            execution_provenance_ref=provenance_ref,
            artifacts=(artifact,),
            completed_at_utc=__import__('datetime').datetime.now(
                __import__('datetime').timezone.utc
            ).isoformat(),
        )
        return self.base_executor.result_repository.save(result)
