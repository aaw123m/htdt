from __future__ import annotations

from math import isfinite, sqrt
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .cad_acoustic_treatment import (
    AcousticTreatmentDefinition,
    AcousticTreatmentPlacement,
    TreatmentCoverage,
    TreatmentFrequencyBand,
    TreatmentSurfaceBindingEvaluation,
    TreatmentUncertainty,
)
from .cad_repository import SceneRevision
from .cad_scene import quaternion_to_matrix3
from .r120_geometry_compiler import (
    ExactExternalAuthorityRef,
    R120CompiledGeometry,
    SurfaceBoundaryAuthorityBinding,
)
from .semantic_geometry import SemanticAcousticGeometry, SemanticSurface
from .canonical_json import canonical_json as _canonical_json, canonical_sha256


TREATMENT_BOUNDARY_OVERLAY_COMPILER_ID = 'htdt.r120.treatment_boundary_overlay'
TREATMENT_BOUNDARY_OVERLAY_COMPILER_VERSION = '1'
TREATMENT_BOUNDARY_OVERLAY_AUTHORITY_VERSION = '1'
TREATMENT_BOUNDARY_COMPOSITION_AUTHORITY_VERSION = '1'

TreatmentBoundaryTarget = Literal['wave', 'geometric']
TreatmentBoundaryCompileStatus = Literal[
    'AVAILABLE',
    'BLOCKED_NO_ACOUSTIC_MODEL',
    'BLOCKED_WAVE_MODEL_UNAVAILABLE',
    'BLOCKED_GEOMETRIC_MODEL_UNAVAILABLE',
    'BLOCKED_PARTIAL_COVERAGE',
    'BLOCKED_COVERAGE_MISMATCH',
    'BLOCKED_OVERLAP',
    'BLOCKED_STALE_SURFACE',
    'BLOCKED_STALE_R120_COMPILED_GEOMETRY',
]
TreatmentBoundaryCapabilityState = Literal['AVAILABLE', 'UNKNOWN']
TransmissionCapabilityState = Literal['UNKNOWN']




def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode='json')
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _semantic_hash(payload: object) -> str:
    return canonical_sha256(_jsonable(payload))


FOOTPRINT_DERIVATION_VERSION = 'treatment-footprint-derivation-1'
FOOTPRINT_COPLANAR_TOLERANCE_M = 1.0e-3
FOOTPRINT_COVERAGE_MISMATCH_TOLERANCE = 1.0e-3
FOOTPRINT_FULL_COVERAGE_EPSILON = 1.0e-6
FOOTPRINT_OVERLAP_TOLERANCE_M2 = 1.0e-9


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _norm(v: tuple[float, float, float]) -> float:
    return sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _scale(
    v: tuple[float, float, float], s: float
) -> tuple[float, float, float]:
    return (v[0] * s, v[1] * s, v[2] * s)


class TreatmentFootprint(BaseModel):
    """Solver-facing patch of one treatment on its exact host surface (#976).

    The patch is derived — never caller-supplied: the placement's
    ``position``/``orientation``/``width_m``/``height_m`` rectangle is
    projected orthogonally onto the host surface's fitted plane and clipped
    to the surface boundary polygon. ``derived_fraction`` is the covered
    share of the host surface area; ``covers_entire_surface`` is only true
    when the patch reproduces the full surface within tolerance — a
    supplied ``host_surface_fraction`` scalar can never assert that.
    """

    model_config = ConfigDict(frozen=True)

    derivation_version: Literal[
        'treatment-footprint-derivation-1'
    ] = FOOTPRINT_DERIVATION_VERSION
    host_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')
    surface_area_m2: float = Field(gt=0.0)
    patch_area_m2: float = Field(ge=0.0)
    derived_fraction: float = Field(ge=0.0, le=1.0)
    covers_entire_surface: bool
    supplied_fraction: float | None = None
    patch_uv_polygons: tuple[tuple[tuple[float, float], ...], ...] = ()
    plane_origin_m: tuple[float, float, float]
    plane_u_axis: tuple[float, float, float]
    plane_v_axis: tuple[float, float, float]
    plane_normal: tuple[float, float, float]
    geometric_tolerance_m2: float = Field(ge=0.0)
    footprint_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_footprint(self) -> 'TreatmentFootprint':
        for value in (
            self.surface_area_m2,
            self.patch_area_m2,
            self.derived_fraction,
            *(coord for ring in self.patch_uv_polygons for pt in ring for coord in pt),
            *self.plane_origin_m,
            *self.plane_u_axis,
            *self.plane_v_axis,
            *self.plane_normal,
        ):
            if not isfinite(float(value)):
                raise ValueError('treatment footprint must be finite')
        if self.patch_area_m2 > self.surface_area_m2 + self.geometric_tolerance_m2:
            raise ValueError('derived patch cannot exceed the host surface area')
        expected = _semantic_hash(
            self.model_dump(mode='json', exclude={'footprint_sha256'})
        )
        if self.footprint_sha256 != expected:
            raise ValueError('treatment footprint semantic hash mismatch')
        return self


def _surface_plane_frame(
    geometry: SemanticAcousticGeometry,
    surface: SemanticSurface,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    list[tuple[tuple[float, float], ...]],
] | None:
    """Fit the host surface's plane and project its triangles to local UV.

    Returns ``None`` when the surface cannot furnish a coplanar patch
    reference (degenerate or non-planar triangle set within tolerance).
    """
    vertex_xyz = {
        vertex_id: (
            float(vertex.x_m),
            float(vertex.y_m),
            float(vertex.z_m),
        )
        for vertex_id, vertex in enumerate(geometry.vertices)
    }
    triangles = {
        triangle.triangle_id: triangle for triangle in geometry.triangles
    }
    points_xyz: list[tuple[float, float, float]] = []
    triangles_xyz: list[
        tuple[tuple[float, float, float], ...]
    ] = []
    normal_accum = (0.0, 0.0, 0.0)
    for triangle_id in surface.triangle_ids:
        triangle = triangles.get(triangle_id)
        if triangle is None:
            return None
        try:
            pts = (
                vertex_xyz[triangle.a],
                vertex_xyz[triangle.b],
                vertex_xyz[triangle.c],
            )
        except KeyError:
            return None
        normal_accum = tuple(
            a + b
            for a, b in zip(
                normal_accum,
                _cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0])),
            )
        )
        points_xyz.extend(pts)
        triangles_xyz.append(pts)
    if not triangles_xyz:
        return None
    normal_length = _norm(normal_accum)
    if normal_length <= 0.0 or not isfinite(normal_length):
        return None
    normal = _scale(normal_accum, 1.0 / normal_length)
    origin = points_xyz[0]
    # Coplanarity: every surface vertex must sit on the fitted plane.
    for point in points_xyz:
        if abs(_dot(_sub(point, origin), normal)) > FOOTPRINT_COPLANAR_TOLERANCE_M:
            return None
    u_axis: tuple[float, float, float] | None = None
    for pts in triangles_xyz:
        for a, b in ((pts[0], pts[1]), (pts[1], pts[2]), (pts[2], pts[0])):
            edge = _sub(b, a)
            if _norm(edge) > 1.0e-9:
                u_axis = _scale(edge, 1.0 / _norm(edge))
                break
        if u_axis is not None:
            break
    if u_axis is None:
        return None
    v_axis = _cross(normal, u_axis)
    v_axis = _scale(v_axis, 1.0 / _norm(v_axis))
    # Re-orthogonalize u to the plane for numerical stability.
    u_axis = _cross(v_axis, normal)
    u_axis = _scale(u_axis, 1.0 / _norm(u_axis))
    triangles_uv = [
        tuple(
            (
                _dot(_sub(p, origin), u_axis),
                _dot(_sub(p, origin), v_axis),
            )
            for p in pts
        )
        for pts in triangles_xyz
    ]
    return origin, u_axis, v_axis, normal, triangles_uv


def _polygons_of(geometry: BaseGeometry) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if geometry.geom_type == 'Polygon':
        return [geometry]
    if geometry.geom_type == 'MultiPolygon':
        return [poly for poly in geometry.geoms]
    return []


def _derive_footprint(
    placement: AcousticTreatmentPlacement,
    geometry: SemanticAcousticGeometry,
) -> TreatmentFootprint | None:
    """Derive the treatment's clipped patch on the exact host surface (#976).

    Convention: the treatment rectangle is centred at ``position`` with
    ``width_m`` along the placement's local +X axis, ``height_m`` along
    local +Y, and the panel normal along local +Z (the frame given by
    ``orientation``). Depth offsets (air gap/thickness) do not move the
    patch — the rectangle is projected orthogonally onto the surface
    plane. Returns ``None`` when no planar footprint can be derived.
    """
    host_id = placement.host_surface_id
    if host_id is None:
        return None
    surfaces = [
        surface
        for surface in geometry.surfaces
        if surface.surface_id == host_id
    ]
    if len(surfaces) != 1:
        return None
    frame = _surface_plane_frame(geometry, surfaces[0])
    if frame is None:
        return None
    origin, u_axis, v_axis, normal, triangles_uv = frame

    surface_polygon = unary_union([Polygon(t) for t in triangles_uv])
    if surface_polygon.is_empty or surface_polygon.area <= 0.0:
        return None

    matrix = quaternion_to_matrix3(placement.orientation)
    width_axis = (matrix[0][0], matrix[1][0], matrix[2][0])
    height_axis = (matrix[0][1], matrix[1][1], matrix[2][1])
    center = (
        float(placement.position.x_m),
        float(placement.position.y_m),
        float(placement.position.z_m),
    )
    half_w = float(placement.coverage.width_m) / 2.0
    half_h = float(placement.coverage.height_m) / 2.0
    corners_uv = []
    for sx, sy in ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)):
        point = (
            center[0] + sx * half_w * width_axis[0] + sy * half_h * height_axis[0],
            center[1] + sx * half_w * width_axis[1] + sy * half_h * height_axis[1],
            center[2] + sx * half_w * width_axis[2] + sy * half_h * height_axis[2],
        )
        projected = _sub(point, _scale(normal, _dot(_sub(point, origin), normal)))
        corners_uv.append(
            (
                _dot(_sub(projected, origin), u_axis),
                _dot(_sub(projected, origin), v_axis),
            )
        )
    patch = surface_polygon.intersection(Polygon(corners_uv))
    patch_area = float(patch.area)
    surface_area = float(surface_polygon.area)
    derived_fraction = patch_area / surface_area
    patch_uv_polygons = tuple(
        tuple((float(x), float(y)) for x, y in polygon.exterior.coords[:-1])
        for polygon in _polygons_of(patch)
    )
    payload = {
        'derivation_version': FOOTPRINT_DERIVATION_VERSION,
        'host_surface_id': host_id,
        'surface_area_m2': surface_area,
        'patch_area_m2': patch_area,
        'derived_fraction': derived_fraction,
        'covers_entire_surface': (
            derived_fraction >= 1.0 - FOOTPRINT_FULL_COVERAGE_EPSILON
        ),
        'supplied_fraction': placement.coverage.host_surface_fraction,
        'patch_uv_polygons': patch_uv_polygons,
        'plane_origin_m': tuple(float(c) for c in origin),
        'plane_u_axis': tuple(float(c) for c in u_axis),
        'plane_v_axis': tuple(float(c) for c in v_axis),
        'plane_normal': tuple(float(c) for c in normal),
        'geometric_tolerance_m2': FOOTPRINT_OVERLAP_TOLERANCE_M2,
    }
    return TreatmentFootprint(
        **payload,
        footprint_sha256=_semantic_hash(payload),
    )


def _acoustic_model_ref(
    definition: AcousticTreatmentDefinition,
) -> ExactExternalAuthorityRef | None:
    model = definition.acoustic_model
    if model is None:
        return None
    digest = _semantic_hash(model.model_dump(mode='json'))
    return ExactExternalAuthorityRef(
        authority_id=f'treatment-acoustic-model:{model.model_id}',
        authority_version=model.model_version,
        semantic_hash_sha256=digest,
    )


class TreatmentBoundaryCompileInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    definition: AcousticTreatmentDefinition
    placement: AcousticTreatmentPlacement
    surface_binding_evaluation: TreatmentSurfaceBindingEvaluation

    @model_validator(mode='after')
    def exact_lineage(self) -> 'TreatmentBoundaryCompileInput':
        if (
            self.placement.definition_id != self.definition.definition_id
            or self.placement.definition_version != self.definition.version
            or self.placement.definition_sha256 != self.definition.definition_sha256
        ):
            raise ValueError('treatment placement does not reference the exact definition authority')
        if self.surface_binding_evaluation.placement_sha256 != self.placement.placement_sha256:
            raise ValueError('surface-binding evaluation does not reference the exact placement')
        return self


class TreatmentBoundaryOverlay(BaseModel):
    """Attached-treatment boundary authority. Base construction is intentionally absent."""

    model_config = ConfigDict(frozen=True)

    overlay_id: str = Field(pattern=r'^treatment-boundary-overlay:[0-9a-f]{64}$')
    overlay_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    authority_version: Literal['1'] = TREATMENT_BOUNDARY_OVERLAY_AUTHORITY_VERSION
    compiler_id: Literal['htdt.r120.treatment_boundary_overlay'] = (
        TREATMENT_BOUNDARY_OVERLAY_COMPILER_ID
    )
    compiler_version: Literal['1'] = TREATMENT_BOUNDARY_OVERLAY_COMPILER_VERSION

    exact_scene_revision_id: str = Field(min_length=1)
    exact_scene_revision_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    exact_semantic_geometry_id: str = Field(
        pattern=r'^semantic-acoustic-geometry:[0-9a-f]{64}$'
    )
    exact_semantic_geometry_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    exact_r120_compiled_geometry_id: str = Field(
        pattern=r'^r120-compiled-geometry:[0-9a-f]{64}$'
    )
    exact_r120_compiled_geometry_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    host_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')
    host_surface_authority_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    treatment_definition_id: str = Field(min_length=1)
    treatment_definition_version: str = Field(min_length=1)
    treatment_definition_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    treatment_placement_instance_id: str = Field(min_length=1)
    treatment_placement_version: int = Field(ge=1)
    treatment_placement_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    surface_binding_evaluation_id: str = Field(
        pattern=r'^treatment-surface-binding:[0-9a-f]{64}$'
    )
    surface_binding_evaluation_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    lifecycle: Literal['proposed', 'installed']
    treatment_coverage: TreatmentCoverage
    thickness_m: float = Field(gt=0.0)
    air_gap_m: float = Field(ge=0.0)

    # #976: the solver-facing patch is derived from placement geometry + the
    # exact host surface — never asserted by a caller-supplied scalar.
    derived_footprint: TreatmentFootprint | None = None

    treatment_acoustic_model_id: str | None = Field(default=None, min_length=1)
    treatment_acoustic_model_version: str | None = Field(default=None, min_length=1)
    evidence_basis: Literal['measured', 'inferred', 'modelled'] | None = None
    valid_frequency_band: TreatmentFrequencyBand | None = None
    uncertainty: TreatmentUncertainty
    wave_capability_state: TreatmentBoundaryCapabilityState
    geometric_capability_state: TreatmentBoundaryCapabilityState
    wave_material_candidate_ref: ExactExternalAuthorityRef | None = None
    geometric_material_candidate_ref: ExactExternalAuthorityRef | None = None
    transmission_capability_state: TransmissionCapabilityState = 'UNKNOWN'

    @model_validator(mode='after')
    def validate_identity(self) -> 'TreatmentBoundaryOverlay':
        model_fields = (
            self.treatment_acoustic_model_id,
            self.treatment_acoustic_model_version,
            self.evidence_basis,
            self.valid_frequency_band,
        )
        if self.treatment_acoustic_model_id is None and any(
            value is not None for value in model_fields[1:]
        ):
            raise ValueError('acoustic-model metadata must be supplied together')
        if self.treatment_acoustic_model_id is not None and any(
            value is None for value in model_fields[1:]
        ):
            raise ValueError('acoustic-model metadata must be supplied together')
        if self.wave_capability_state == 'AVAILABLE' and self.wave_material_candidate_ref is None:
            raise ValueError('available wave capability requires an exact candidate authority')
        if self.wave_capability_state == 'UNKNOWN' and self.wave_material_candidate_ref is not None:
            raise ValueError('unknown wave capability cannot expose a material candidate')
        if (
            self.geometric_capability_state == 'AVAILABLE'
            and self.geometric_material_candidate_ref is None
        ):
            raise ValueError('available geometric capability requires an exact candidate authority')
        if (
            self.geometric_capability_state == 'UNKNOWN'
            and self.geometric_material_candidate_ref is not None
        ):
            raise ValueError('unknown geometric capability cannot expose a material candidate')
        core = self.model_dump(mode='json', exclude={'overlay_id', 'overlay_hash_sha256'})
        expected = _semantic_hash(core)
        if self.overlay_hash_sha256 != expected:
            raise ValueError('TreatmentBoundaryOverlay hash mismatch')
        if self.overlay_id != f'treatment-boundary-overlay:{expected}':
            raise ValueError('TreatmentBoundaryOverlay id mismatch')
        return self

    def as_external_authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.overlay_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.overlay_hash_sha256,
        )


class TreatmentBoundaryCompositionRequest(BaseModel):
    """Final solver-facing request preserving base authority and attached overlays separately."""

    model_config = ConfigDict(frozen=True)

    composition_id: str = Field(pattern=r'^treatment-boundary-composition:[0-9a-f]{64}$')
    composition_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    authority_version: Literal['1'] = TREATMENT_BOUNDARY_COMPOSITION_AUTHORITY_VERSION
    target_domain: TreatmentBoundaryTarget

    exact_scene_revision_id: str = Field(min_length=1)
    exact_scene_revision_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    exact_semantic_geometry_id: str = Field(
        pattern=r'^semantic-acoustic-geometry:[0-9a-f]{64}$'
    )
    exact_semantic_geometry_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    exact_r120_compiled_geometry_id: str = Field(
        pattern=r'^r120-compiled-geometry:[0-9a-f]{64}$'
    )
    exact_r120_compiled_geometry_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    host_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')

    selected_treatment_lifecycle: Literal['proposed', 'installed']
    base_material_authority: ExactExternalAuthorityRef | None = None
    base_boundary_physics_authority: ExactExternalAuthorityRef | None = None
    attached_treatment_overlays: tuple[ExactExternalAuthorityRef, ...] = Field(min_length=1)
    selected_treatment_material_authorities: tuple[ExactExternalAuthorityRef, ...] = Field(
        min_length=1
    )
    # #976: the composition binds the derived patch; an untreated remainder
    # keeps the exact base authorities below.
    treatment_footprint_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    derived_coverage_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    covers_entire_surface: bool | None = None
    transmission_capability_state: TransmissionCapabilityState = 'UNKNOWN'

    @model_validator(mode='after')
    def validate_identity(self) -> 'TreatmentBoundaryCompositionRequest':
        if len(self.attached_treatment_overlays) != len(
            self.selected_treatment_material_authorities
        ):
            raise ValueError('each treatment overlay requires one selected material authority')
        footprint_fields = (
            self.treatment_footprint_sha256,
            self.derived_coverage_fraction,
            self.covers_entire_surface,
        )
        if any(value is None for value in footprint_fields) != (
            all(value is None for value in footprint_fields)
        ):
            raise ValueError(
                'treatment footprint sha/fraction/coverage must be supplied together'
            )
        core = self.model_dump(
            mode='json',
            exclude={'composition_id', 'composition_hash_sha256'},
        )
        expected = _semantic_hash(core)
        if self.composition_hash_sha256 != expected:
            raise ValueError('TreatmentBoundaryCompositionRequest hash mismatch')
        if self.composition_id != f'treatment-boundary-composition:{expected}':
            raise ValueError('TreatmentBoundaryCompositionRequest id mismatch')
        return self

    def as_external_authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.composition_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.composition_hash_sha256,
        )


class TreatmentBoundaryCompilationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: TreatmentBoundaryCompileStatus
    target_domain: TreatmentBoundaryTarget
    host_surface_id: str | None = None
    overlay: TreatmentBoundaryOverlay | None = None
    composition_request: TreatmentBoundaryCompositionRequest | None = None
    r120_surface_binding: SurfaceBoundaryAuthorityBinding | None = None
    reasons: tuple[str, ...]

    @model_validator(mode='after')
    def available_is_complete(self) -> 'TreatmentBoundaryCompilationResult':
        available = self.status == 'AVAILABLE'
        if available != (
            self.overlay is not None
            and self.composition_request is not None
            and self.r120_surface_binding is not None
        ):
            raise ValueError('AVAILABLE result must contain overlay, composition, and R120 binding')
        if not available and (
            self.composition_request is not None or self.r120_surface_binding is not None
        ):
            raise ValueError('blocked result cannot expose solver-facing composition/binding')
        return self


def adapt_treatment_boundary_composition_to_r120(
    composition: TreatmentBoundaryCompositionRequest,
) -> SurfaceBoundaryAuthorityBinding:
    """Preserve base material; point boundary physics at the exact composition request."""

    return SurfaceBoundaryAuthorityBinding(
        source_surface_id=composition.host_surface_id,
        material_authority=composition.base_material_authority,
        boundary_physics_authority=composition.as_external_authority_ref(),
    )


def compile_treatment_boundary_overlay(
    definition: AcousticTreatmentDefinition,
    placement: AcousticTreatmentPlacement,
    evaluation: TreatmentSurfaceBindingEvaluation,
    revision: SceneRevision,
    compiled: R120CompiledGeometry,
) -> TreatmentBoundaryOverlay:
    """Deterministically derive the canonical overlay from exact authorities.

    Persistence layers replay this builder against the resolved SceneRevision,
    R120CompiledGeometry, TreatmentDefinition, TreatmentPlacement and
    surface-binding evaluation so a persisted overlay is never trusted beyond
    what those exact inputs derive.
    """

    geometry = revision.document.r120_semantic_geometry
    if geometry is None:
        raise ValueError('exact SceneRevision has no SemanticAcousticGeometry')
    if placement.host_surface_id is None or placement.host_surface_authority_sha256 is None:
        raise ValueError('treatment placement has no exact host SemanticSurface authority')
    footprint = _derive_footprint(placement, geometry)

    acoustic_model = definition.acoustic_model
    if acoustic_model is None:
        uncertainty = TreatmentUncertainty(
            kind='unknown',
            note='no treatment acoustic model authority is attached',
        )
        wave_state: TreatmentBoundaryCapabilityState = 'UNKNOWN'
        geometric_state: TreatmentBoundaryCapabilityState = 'UNKNOWN'
        candidate_ref = None
    else:
        uncertainty = acoustic_model.uncertainty
        wave_state = (
            'AVAILABLE' if acoustic_model.material.wave_model != 'unsupported' else 'UNKNOWN'
        )
        geometric_state = (
            'AVAILABLE'
            if acoustic_model.material.geometric_model == 'banded'
            else 'UNKNOWN'
        )
        candidate_ref = _acoustic_model_ref(definition)

    core = {
        'authority_version': TREATMENT_BOUNDARY_OVERLAY_AUTHORITY_VERSION,
        'compiler_id': TREATMENT_BOUNDARY_OVERLAY_COMPILER_ID,
        'compiler_version': TREATMENT_BOUNDARY_OVERLAY_COMPILER_VERSION,
        'exact_scene_revision_id': revision.revision_id,
        'exact_scene_revision_content_hash': revision.content_hash,
        'exact_semantic_geometry_id': geometry.geometry_id,
        'exact_semantic_geometry_hash_sha256': geometry.semantic_hash_sha256,
        'exact_r120_compiled_geometry_id': compiled.compiled_geometry_id,
        'exact_r120_compiled_geometry_hash_sha256': compiled.compiled_hash_sha256,
        'host_surface_id': placement.host_surface_id,
        'host_surface_authority_sha256': placement.host_surface_authority_sha256,
        'treatment_definition_id': definition.definition_id,
        'treatment_definition_version': definition.version,
        'treatment_definition_hash_sha256': definition.definition_sha256,
        'treatment_placement_instance_id': placement.instance_id,
        'treatment_placement_version': placement.placement_version,
        'treatment_placement_hash_sha256': placement.placement_sha256,
        'surface_binding_evaluation_id': (
            f'treatment-surface-binding:{evaluation.evaluation_sha256}'
        ),
        'surface_binding_evaluation_hash_sha256': evaluation.evaluation_sha256,
        'lifecycle': placement.lifecycle,
        'treatment_coverage': placement.coverage,
        'thickness_m': definition.dimensions.thickness_m,
        'air_gap_m': definition.air_gap_m,
        'treatment_acoustic_model_id': (
            None if acoustic_model is None else acoustic_model.model_id
        ),
        'treatment_acoustic_model_version': (
            None if acoustic_model is None else acoustic_model.model_version
        ),
        'evidence_basis': None if acoustic_model is None else acoustic_model.evidence_basis,
        'valid_frequency_band': (
            None if acoustic_model is None else acoustic_model.valid_frequency_band
        ),
        'uncertainty': uncertainty,
        'wave_capability_state': wave_state,
        'geometric_capability_state': geometric_state,
        'wave_material_candidate_ref': (
            candidate_ref if wave_state == 'AVAILABLE' else None
        ),
        'geometric_material_candidate_ref': (
            candidate_ref if geometric_state == 'AVAILABLE' else None
        ),
        'transmission_capability_state': 'UNKNOWN',
        'derived_footprint': (
            None if footprint is None else footprint
        ),
    }
    digest = _semantic_hash(core)
    return TreatmentBoundaryOverlay(
        overlay_id=f'treatment-boundary-overlay:{digest}',
        overlay_hash_sha256=digest,
        **core,
    )


def _compiled_matches_revision(
    revision: SceneRevision,
    compiled: R120CompiledGeometry,
) -> bool:
    geometry = revision.document.r120_semantic_geometry
    return bool(
        geometry is not None
        and compiled.exact_scene_revision_id == revision.revision_id
        and compiled.exact_scene_revision_content_hash == revision.content_hash
        and compiled.exact_semantic_geometry_id == geometry.geometry_id
        and compiled.exact_semantic_geometry_hash_sha256 == geometry.semantic_hash_sha256
    )


def _exact_surface_binding(
    item: TreatmentBoundaryCompileInput,
    revision: SceneRevision,
    compiled: R120CompiledGeometry,
) -> bool:
    placement = item.placement
    evaluation = item.surface_binding_evaluation
    if placement.host_surface_id is None or placement.host_surface_authority_sha256 is None:
        return False
    if (
        evaluation.binding_state != 'exact'
        or not evaluation.bound_authority_valid
        or not evaluation.placement_authority_valid
        or evaluation.evaluated_scene_revision_id != revision.revision_id
        or evaluation.evaluated_scene_content_hash != revision.content_hash
        or evaluation.actual_host_surface_authority_sha256
        != placement.host_surface_authority_sha256
    ):
        return False
    return any(
        mapping.source_surface_id == placement.host_surface_id
        for mapping in compiled.surface_mapping
    )


def _base_binding_for_surface(
    surface_id: str,
    base_surface_bindings: Sequence[SurfaceBoundaryAuthorityBinding],
) -> SurfaceBoundaryAuthorityBinding:
    matches = [item for item in base_surface_bindings if item.source_surface_id == surface_id]
    if len(matches) > 1:
        raise ValueError('base surface boundary bindings must be unique per SemanticSurface')
    if matches:
        return matches[0]
    return SurfaceBoundaryAuthorityBinding(source_surface_id=surface_id)


def compile_treatment_boundary_composition(
    overlay: TreatmentBoundaryOverlay,
    *,
    target_domain: TreatmentBoundaryTarget,
    base_binding: SurfaceBoundaryAuthorityBinding,
) -> TreatmentBoundaryCompositionRequest:
    """Deterministically derive the canonical composition from exact authorities.

    Persistence layers replay this builder against the resolved persisted
    overlay and the exact R120 host-surface base binding so a persisted
    composition is never trusted beyond what those exact inputs derive.
    """

    selected = (
        overlay.wave_material_candidate_ref
        if target_domain == 'wave'
        else overlay.geometric_material_candidate_ref
    )
    if selected is None:
        raise ValueError('cannot compose unavailable treatment material capability')
    core = {
        'authority_version': TREATMENT_BOUNDARY_COMPOSITION_AUTHORITY_VERSION,
        'target_domain': target_domain,
        'exact_scene_revision_id': overlay.exact_scene_revision_id,
        'exact_scene_revision_content_hash': overlay.exact_scene_revision_content_hash,
        'exact_semantic_geometry_id': overlay.exact_semantic_geometry_id,
        'exact_semantic_geometry_hash_sha256': overlay.exact_semantic_geometry_hash_sha256,
        'exact_r120_compiled_geometry_id': overlay.exact_r120_compiled_geometry_id,
        'exact_r120_compiled_geometry_hash_sha256': (
            overlay.exact_r120_compiled_geometry_hash_sha256
        ),
        'host_surface_id': overlay.host_surface_id,
        'selected_treatment_lifecycle': overlay.lifecycle,
        'base_material_authority': base_binding.material_authority,
        'base_boundary_physics_authority': base_binding.boundary_physics_authority,
        'attached_treatment_overlays': (overlay.as_external_authority_ref(),),
        'selected_treatment_material_authorities': (selected,),
        'treatment_footprint_sha256': (
            None
            if overlay.derived_footprint is None
            else overlay.derived_footprint.footprint_sha256
        ),
        'derived_coverage_fraction': (
            None
            if overlay.derived_footprint is None
            else overlay.derived_footprint.derived_fraction
        ),
        'covers_entire_surface': (
            None
            if overlay.derived_footprint is None
            else overlay.derived_footprint.covers_entire_surface
        ),
        'transmission_capability_state': 'UNKNOWN',
    }
    digest = _semantic_hash(core)
    return TreatmentBoundaryCompositionRequest(
        composition_id=f'treatment-boundary-composition:{digest}',
        composition_hash_sha256=digest,
        **core,
    )


def _footprints_overlap(footprints: Sequence[TreatmentFootprint]) -> bool:
    """True when any two derived patches intersect beyond tolerance (#976).

    Overlap is a property of derived geometry only — two panels on one wall
    are allowed exactly when their clipped patches are disjoint.
    """
    polygons = [
        Polygon(ring)
        for footprint in footprints
        for ring in footprint.patch_uv_polygons
        if len(ring) >= 3
    ]
    for index, first in enumerate(polygons):
        for second in polygons[index + 1 :]:
            if (
                first.intersects(second)
                and first.intersection(second).area
                > FOOTPRINT_OVERLAP_TOLERANCE_M2
            ):
                return True
    return False


def compile_treatment_boundary_overlays(
    revision: SceneRevision,
    compiled: R120CompiledGeometry,
    inputs: Sequence[TreatmentBoundaryCompileInput],
    *,
    target_domain: TreatmentBoundaryTarget,
    base_surface_bindings: Sequence[SurfaceBoundaryAuthorityBinding] = (),
) -> tuple[TreatmentBoundaryCompilationResult, ...]:
    """Compile exact attached-treatment overlays without replacing base construction."""

    input_items = tuple(inputs)
    if not input_items:
        return ()

    base_ids = [item.source_surface_id for item in base_surface_bindings]
    if len(base_ids) != len(set(base_ids)):
        raise ValueError('base surface boundary bindings must be unique')

    compiled_matches = _compiled_matches_revision(revision, compiled)
    geometry = revision.document.r120_semantic_geometry
    host_counts: dict[str, int] = {}
    for item in input_items:
        host = item.placement.host_surface_id
        if host is not None:
            host_counts[host] = host_counts.get(host, 0) + 1

    results: list[TreatmentBoundaryCompilationResult] = []
    for item in input_items:
        definition = item.definition
        placement = item.placement
        evaluation = item.surface_binding_evaluation
        host = placement.host_surface_id

        if not compiled_matches:
            results.append(
                TreatmentBoundaryCompilationResult(
                    status='BLOCKED_STALE_R120_COMPILED_GEOMETRY',
                    target_domain=target_domain,
                    host_surface_id=host,
                    reasons=(
                        'R120CompiledGeometry does not match the exact SceneRevision/SemanticAcousticGeometry',
                    ),
                )
            )
            continue

        if not _exact_surface_binding(item, revision, compiled):
            results.append(
                TreatmentBoundaryCompilationResult(
                    status='BLOCKED_STALE_SURFACE',
                    target_domain=target_domain,
                    host_surface_id=host,
                    reasons=(
                        'treatment placement/surface-binding evaluation is not exact for the selected SceneRevision',
                    ),
                )
            )
            continue

        assert host is not None
        overlay = compile_treatment_boundary_overlay(
            definition,
            placement,
            evaluation,
            revision,
            compiled,
        )
        footprint = overlay.derived_footprint
        if footprint is None:
            results.append(
                TreatmentBoundaryCompilationResult(
                    status='BLOCKED_PARTIAL_COVERAGE',
                    target_domain=target_domain,
                    host_surface_id=host,
                    overlay=overlay,
                    reasons=(
                        'treatment footprint cannot be derived: the host '
                        'surface is missing or not planar within tolerance',
                    ),
                )
            )
            continue

        if host_counts.get(host, 0) > 1:
            peers = [
                other
                for other in input_items
                if other.placement.host_surface_id == host
            ]
            peer_footprints = [
                (
                    _derive_footprint(other.placement, geometry)
                    if geometry is not None
                    else None
                )
                for other in peers
            ]
            overlap = (
                len(peers) > 1
                and (
                    any(item is None for item in peer_footprints)
                    or _footprints_overlap(
                        [item for item in peer_footprints if item is not None]
                    )
                )
            )
            if overlap:
                results.append(
                    TreatmentBoundaryCompilationResult(
                        status='BLOCKED_OVERLAP',
                        target_domain=target_domain,
                        host_surface_id=host,
                        overlay=overlay,
                        reasons=(
                            'derived treatment footprints on the same host '
                            'surface overlap or cannot be proven disjoint',
                            'order-dependent or last-saved-wins material '
                            'replacement is forbidden',
                        ),
                    )
                )
                continue

        supplied = placement.coverage.host_surface_fraction
        if (
            supplied is not None
            and abs(supplied - footprint.derived_fraction)
            > FOOTPRINT_COVERAGE_MISMATCH_TOLERANCE
        ):
            results.append(
                TreatmentBoundaryCompilationResult(
                    status='BLOCKED_COVERAGE_MISMATCH',
                    target_domain=target_domain,
                    host_surface_id=host,
                    overlay=overlay,
                    reasons=(
                        'supplied host_surface_fraction disagrees with the '
                        'geometrically derived coverage; caller-asserted '
                        'coverage is never solver authority',
                    ),
                )
            )
            continue

        if footprint.patch_area_m2 <= FOOTPRINT_OVERLAP_TOLERANCE_M2:
            results.append(
                TreatmentBoundaryCompilationResult(
                    status='BLOCKED_PARTIAL_COVERAGE',
                    target_domain=target_domain,
                    host_surface_id=host,
                    overlay=overlay,
                    reasons=(
                        'derived treatment footprint is disjoint from the '
                        'host surface',
                    ),
                )
            )
            continue

        if definition.acoustic_model is None:
            results.append(
                TreatmentBoundaryCompilationResult(
                    status='BLOCKED_NO_ACOUSTIC_MODEL',
                    target_domain=target_domain,
                    host_surface_id=host,
                    overlay=overlay,
                    reasons=('treatment definition has no acoustic model authority',),
                )
            )
            continue

        selected_ref = (
            overlay.wave_material_candidate_ref
            if target_domain == 'wave'
            else overlay.geometric_material_candidate_ref
        )
        if selected_ref is None:
            status: TreatmentBoundaryCompileStatus = (
                'BLOCKED_WAVE_MODEL_UNAVAILABLE'
                if target_domain == 'wave'
                else 'BLOCKED_GEOMETRIC_MODEL_UNAVAILABLE'
            )
            reason = (
                'wave boundary physics is unavailable; scalar/geometric absorption is not converted to complex impedance'
                if target_domain == 'wave'
                else 'explicit geometric absorption/scattering model is unavailable'
            )
            results.append(
                TreatmentBoundaryCompilationResult(
                    status=status,
                    target_domain=target_domain,
                    host_surface_id=host,
                    overlay=overlay,
                    reasons=(reason,),
                )
            )
            continue

        base_binding = _base_binding_for_surface(host, base_surface_bindings)
        composition = compile_treatment_boundary_composition(
            overlay,
            target_domain=target_domain,
            base_binding=base_binding,
        )
        r120_binding = adapt_treatment_boundary_composition_to_r120(composition)
        results.append(
            TreatmentBoundaryCompilationResult(
                status='AVAILABLE',
                target_domain=target_domain,
                host_surface_id=host,
                overlay=overlay,
                composition_request=composition,
                r120_surface_binding=r120_binding,
                reasons=(
                    'exact treatment overlay compiled without mutating base construction/material authority',
                    'transmission remains UNKNOWN and is not silently treated as opaque',
                ),
            )
        )
    return tuple(results)
