from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
from math import isfinite, sqrt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_repository import SceneRevision
from .semantic_geometry import SemanticAcousticGeometry


R120_GEOMETRY_COMPILER_ID = 'htdt.r120.solver_neutral_geometry_compiler'
R120_GEOMETRY_COMPILER_VERSION = '1'
R120_LEAK_DIAGNOSTIC_ID = 'htdt.r120.ga_leak_portal_diagnostic'
R120_LEAK_DIAGNOSTIC_VERSION = '1'

CompilerTargetRepresentation = Literal['indexed_triangle_surface_v1']
CompilationInputPolicy = Literal[
    'require_contract_ready',
    'diagnostic_compile_unresolved',
]
ApproximationPolicy = Literal['none', 'explicit_policy_only']
TinyFeaturePolicy = Literal[
    'preserve',
    'reject_below_tolerance',
    'drop_below_tolerance',
]
CoplanarHandlingPolicy = Literal['preserve_source_surface_boundaries']
SurfaceTriangulationPolicy = Literal['preserve_semantic_triangles']
CoordinateConvention = Literal['htdt-x-right-y-rear-z-up']
UnitConvention = Literal['metre']
PortalDeclarationMode = Literal['unknown', 'explicit_none', 'explicit_list']
DiagnosticSeverity = Literal['info', 'warning', 'error', 'blocked']


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


def _normalized_edge(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


class R120GeometryCompilationError(ValueError):
    pass


class ExactExternalAuthorityRef(BaseModel):
    """Exact external authority identity; never materializes invented physics."""

    model_config = ConfigDict(frozen=True)

    authority_id: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class SurfaceBoundaryAuthorityBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')
    material_authority: ExactExternalAuthorityRef | None = None
    boundary_physics_authority: ExactExternalAuthorityRef | None = None


class AcousticRegionDeclaration(BaseModel):
    model_config = ConfigDict(frozen=True)

    region_id: str = Field(min_length=1)
    boundary_surface_ids: tuple[str, ...]

    @model_validator(mode='after')
    def validate_surfaces(self) -> 'AcousticRegionDeclaration':
        if not self.boundary_surface_ids:
            raise ValueError('acoustic region must declare at least one boundary surface')
        if len(self.boundary_surface_ids) != len(set(self.boundary_surface_ids)):
            raise ValueError('acoustic region boundary surfaces must be unique')
        return self


class AcousticRegionAuthority(BaseModel):
    model_config = ConfigDict(frozen=True)

    authority_id: str = Field(pattern=r'^r120-acoustic-regions:[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    declarations: tuple[AcousticRegionDeclaration, ...]
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_identity(self) -> 'AcousticRegionAuthority':
        core = self.model_dump(mode='json', exclude={'authority_id', 'semantic_hash_sha256'})
        expected = _semantic_hash(core)
        if self.semantic_hash_sha256 != expected:
            raise ValueError('acoustic region authority hash mismatch')
        if self.authority_id != f'r120-acoustic-regions:{expected}':
            raise ValueError('acoustic region authority id mismatch')
        ids = [item.region_id for item in self.declarations]
        if len(ids) != len(set(ids)):
            raise ValueError('acoustic region ids must be unique')
        return self


class PortalBoundaryEdge(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')
    vertex_a: int = Field(ge=0)
    vertex_b: int = Field(ge=0)

    @model_validator(mode='after')
    def distinct_vertices(self) -> 'PortalBoundaryEdge':
        if self.vertex_a == self.vertex_b:
            raise ValueError('portal boundary edge must use distinct vertices')
        return self

    def canonical_key(self) -> tuple[str, int, int]:
        a, b = _normalized_edge(self.vertex_a, self.vertex_b)
        return (self.source_surface_id, a, b)


class PortalDeclaration(BaseModel):
    model_config = ConfigDict(frozen=True)

    portal_id: str = Field(min_length=1)
    region_ids: tuple[str, ...]
    boundary_edges: tuple[PortalBoundaryEdge, ...]
    state: Literal['open', 'closed'] | None = None
    region_side_semantics: Literal[
        'directed_boundary_edge_loop_normal_from_first_to_second'
    ] | None = None

    @model_validator(mode='after')
    def validate_declaration(self) -> 'PortalDeclaration':
        if not self.boundary_edges:
            raise ValueError('portal must explicitly declare at least one boundary edge')
        if len(self.region_ids) not in (1, 2):
            raise ValueError('portal must declare one or two adjacent acoustic regions')
        if len(self.region_ids) != len(set(self.region_ids)):
            raise ValueError('portal region ids must be unique')
        keys = [edge.canonical_key() for edge in self.boundary_edges]
        if len(keys) != len(set(keys)):
            raise ValueError('portal boundary edges must be unique')
        if (self.state is None) != (self.region_side_semantics is None):
            raise ValueError(
                'portal state and explicit region-side semantics must be declared together'
            )
        if self.region_side_semantics is not None and len(self.region_ids) != 2:
            raise ValueError(
                'explicit portal side semantics require exactly two adjacent regions'
            )
        return self


class PortalAuthority(BaseModel):
    model_config = ConfigDict(frozen=True)

    authority_id: str = Field(pattern=r'^r120-portals:[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    declaration_mode: PortalDeclarationMode
    declarations: tuple[PortalDeclaration, ...] = ()
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_identity(self) -> 'PortalAuthority':
        if self.declaration_mode == 'explicit_none' and self.declarations:
            raise ValueError('explicit_none portal authority cannot contain declarations')
        if self.declaration_mode == 'explicit_list' and not self.declarations:
            raise ValueError('explicit_list portal authority requires declarations')
        if self.declaration_mode == 'unknown' and self.declarations:
            raise ValueError('unknown portal authority cannot contain declarations')
        ids = [item.portal_id for item in self.declarations]
        if len(ids) != len(set(ids)):
            raise ValueError('portal ids must be unique')
        core = {
            'authority_version': self.authority_version,
            'declaration_mode': self.declaration_mode,
            'declarations': [
                item.model_dump(mode='json', exclude_none=True)
                for item in self.declarations
            ],
        }
        expected = _semantic_hash(core)
        if self.semantic_hash_sha256 != expected:
            raise ValueError('portal authority hash mismatch')
        if self.authority_id != f'r120-portals:{expected}':
            raise ValueError('portal authority id mismatch')
        return self


class BoundaryTerminationDeclaration(BaseModel):
    model_config = ConfigDict(frozen=True)

    termination_id: str = Field(min_length=1)
    boundary_edges: tuple[PortalBoundaryEdge, ...]
    external_authority: ExactExternalAuthorityRef

    @model_validator(mode='after')
    def validate_edges(self) -> 'BoundaryTerminationDeclaration':
        if not self.boundary_edges:
            raise ValueError('boundary termination must explicitly declare boundary edges')
        keys = [edge.canonical_key() for edge in self.boundary_edges]
        if len(keys) != len(set(keys)):
            raise ValueError('boundary termination edges must be unique')
        return self


class BoundaryTerminationAuthority(BaseModel):
    model_config = ConfigDict(frozen=True)

    authority_id: str = Field(pattern=r'^r120-boundary-terminations:[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    declaration_mode: PortalDeclarationMode
    declarations: tuple[BoundaryTerminationDeclaration, ...] = ()
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_identity(self) -> 'BoundaryTerminationAuthority':
        if self.declaration_mode == 'explicit_none' and self.declarations:
            raise ValueError('explicit_none termination authority cannot contain declarations')
        if self.declaration_mode == 'explicit_list' and not self.declarations:
            raise ValueError('explicit_list termination authority requires declarations')
        if self.declaration_mode == 'unknown' and self.declarations:
            raise ValueError('unknown termination authority cannot contain declarations')
        ids = [item.termination_id for item in self.declarations]
        if len(ids) != len(set(ids)):
            raise ValueError('boundary termination ids must be unique')
        core = self.model_dump(mode='json', exclude={'authority_id', 'semantic_hash_sha256'})
        expected = _semantic_hash(core)
        if self.semantic_hash_sha256 != expected:
            raise ValueError('boundary termination authority hash mismatch')
        if self.authority_id != f'r120-boundary-terminations:{expected}':
            raise ValueError('boundary termination authority id mismatch')
        return self


class R120GeometryCompilationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(pattern=r'^r120-geometry-compile-request:[0-9a-f]{64}$')
    request_semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    scene_revision_id: str = Field(min_length=1)
    scene_revision_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    semantic_geometry_id: str = Field(pattern=r'^semantic-acoustic-geometry:[0-9a-f]{64}$')
    semantic_geometry_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    compiler_id: Literal['htdt.r120.solver_neutral_geometry_compiler'] = R120_GEOMETRY_COMPILER_ID
    compiler_version: Literal['1'] = R120_GEOMETRY_COMPILER_VERSION
    target_representation: CompilerTargetRepresentation = 'indexed_triangle_surface_v1'
    geometric_tolerance_m: float = Field(gt=0)
    approximation_policy: ApproximationPolicy = 'none'
    tiny_feature_policy: TinyFeaturePolicy = 'preserve'
    coplanar_handling_policy: CoplanarHandlingPolicy = 'preserve_source_surface_boundaries'
    surface_triangulation_policy: SurfaceTriangulationPolicy = 'preserve_semantic_triangles'
    coordinate_convention: CoordinateConvention = 'htdt-x-right-y-rear-z-up'
    unit_convention: UnitConvention = 'metre'
    input_policy: CompilationInputPolicy = 'require_contract_ready'

    @field_validator('geometric_tolerance_m')
    @classmethod
    def finite_tolerance(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value):
            raise ValueError('geometric tolerance must be finite')
        return value

    @model_validator(mode='after')
    def validate_identity(self) -> 'R120GeometryCompilationRequest':
        if self.tiny_feature_policy == 'drop_below_tolerance' and self.approximation_policy != 'explicit_policy_only':
            raise ValueError('dropping tiny features requires explicit_policy_only approximation policy')
        core = self.model_dump(
            mode='json',
            exclude={'request_id', 'request_semantic_hash_sha256'},
        )
        expected = _semantic_hash(core)
        if self.request_semantic_hash_sha256 != expected:
            raise ValueError('R120 compilation request hash mismatch')
        if self.request_id != f'r120-geometry-compile-request:{expected}':
            raise ValueError('R120 compilation request id mismatch')
        return self


class CompiledVertex(BaseModel):
    model_config = ConfigDict(frozen=True)

    x_m: float
    y_m: float
    z_m: float


class CompiledTriangle(BaseModel):
    model_config = ConfigDict(frozen=True)

    a: int = Field(ge=0)
    b: int = Field(ge=0)
    c: int = Field(ge=0)
    source_triangle_id: str = Field(min_length=1)
    source_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')

    @model_validator(mode='after')
    def distinct_indices(self) -> 'CompiledTriangle':
        if len({self.a, self.b, self.c}) != 3:
            raise ValueError('compiled triangle indices must be distinct')
        return self


class ApproximationOperation(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation: Literal['drop_tiny_triangle']
    source_triangle_id: str = Field(min_length=1)
    source_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')
    tolerance_m: float = Field(gt=0)
    measured_max_edge_m: float = Field(ge=0)
    reason: str = Field(min_length=1)


class DroppedFeature(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_triangle_id: str = Field(min_length=1)
    source_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')
    reason_code: Literal['tiny_feature_below_tolerance']
    measured_max_edge_m: float = Field(ge=0)
    tolerance_m: float = Field(gt=0)


class CompiledSurfaceMapping(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')
    source_surface_key: str = Field(min_length=1)
    semantic_class: Literal['room_boundary', 'object_surface', 'unknown']
    source_triangle_ids: tuple[str, ...]
    compiled_triangle_indices: tuple[int, ...]
    dropped_source_triangle_ids: tuple[str, ...]
    material_authority: ExactExternalAuthorityRef | None = None
    boundary_physics_authority: ExactExternalAuthorityRef | None = None


class BoundingVolume(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_x_m: float
    min_y_m: float
    min_z_m: float
    max_x_m: float
    max_y_m: float
    max_z_m: float


class CompiledBoundaryEdge(BaseModel):
    model_config = ConfigDict(frozen=True)

    vertex_a: int = Field(ge=0)
    vertex_b: int = Field(ge=0)
    source_surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')

    @model_validator(mode='after')
    def canonical_vertices(self) -> 'CompiledBoundaryEdge':
        if self.vertex_a >= self.vertex_b:
            raise ValueError('compiled boundary edge vertices must be canonical ascending order')
        return self

    def canonical_key(self) -> tuple[str, int, int]:
        return (self.source_surface_id, self.vertex_a, self.vertex_b)


class ClosedShellDiagnostics(BaseModel):
    model_config = ConfigDict(frozen=True)

    boundary_edge_count: int = Field(ge=0)
    non_manifold_edge_count: int = Field(ge=0)
    closed_shell: bool
    enclosed_volume_m3: float | None = Field(default=None, ge=0)


class R120GeometryReadiness(BaseModel):
    model_config = ConfigDict(frozen=True)

    geometry_compiled: bool
    wave_geometry_ready: bool
    geometric_acoustics_geometry_ready: bool
    material_assignment_missing: bool
    region_definition_missing: bool
    portal_definition_missing: bool
    boundary_physics_missing: bool
    unresolved_conditions: tuple[str, ...]


class R120CompiledGeometry(BaseModel):
    model_config = ConfigDict(frozen=True)

    compiled_geometry_id: str = Field(pattern=r'^r120-compiled-geometry:[0-9a-f]{64}$')
    compiled_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    request: R120GeometryCompilationRequest
    exact_scene_revision_id: str
    exact_scene_revision_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    exact_semantic_geometry_id: str = Field(pattern=r'^semantic-acoustic-geometry:[0-9a-f]{64}$')
    exact_semantic_geometry_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    vertices: tuple[CompiledVertex, ...]
    triangles: tuple[CompiledTriangle, ...]
    surface_mapping: tuple[CompiledSurfaceMapping, ...]
    topology_identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    bounding_volume: BoundingVolume
    closed_shell_diagnostics: ClosedShellDiagnostics
    approximation_operations: tuple[ApproximationOperation, ...]
    dropped_features: tuple[DroppedFeature, ...]
    geometric_tolerance_m: float = Field(gt=0)
    approximation_error_bound_m: float | None = Field(default=None, ge=0)
    approximation_error_status: Literal['exact_preservation', 'not_computed_for_dropped_features']
    maximum_dropped_feature_extent_m: float = Field(ge=0)
    compiler_warnings: tuple[str, ...]
    unresolved_conditions: tuple[str, ...]
    readiness: R120GeometryReadiness
    region_authority_ref: ExactExternalAuthorityRef | None = None
    portal_authority_ref: ExactExternalAuthorityRef | None = None
    boundary_termination_authority_ref: ExactExternalAuthorityRef | None = None

    @model_validator(mode='after')
    def validate_identity(self) -> 'R120CompiledGeometry':
        if self.exact_scene_revision_id != self.request.scene_revision_id:
            raise ValueError('compiled geometry SceneRevision id does not match request')
        if self.exact_scene_revision_content_hash != self.request.scene_revision_content_hash:
            raise ValueError('compiled geometry SceneRevision hash does not match request')
        if self.exact_semantic_geometry_id != self.request.semantic_geometry_id:
            raise ValueError('compiled geometry semantic geometry id does not match request')
        if self.exact_semantic_geometry_hash_sha256 != self.request.semantic_geometry_hash_sha256:
            raise ValueError('compiled geometry semantic geometry hash does not match request')
        expected_topology = _compiled_topology_identity(
            self.vertices,
            self.triangles,
            self.surface_mapping,
        )
        if self.topology_identity_sha256 != expected_topology:
            raise ValueError('compiled geometry topology identity mismatch')
        core = self.model_dump(mode='json', exclude={'compiled_geometry_id', 'compiled_hash_sha256'})
        expected = _semantic_hash(core)
        if self.compiled_hash_sha256 != expected:
            raise ValueError('compiled geometry hash mismatch')
        if self.compiled_geometry_id != f'r120-compiled-geometry:{expected}':
            raise ValueError('compiled geometry id mismatch')
        return self


class LeakDiagnosticSample(BaseModel):
    model_config = ConfigDict(frozen=True)

    sample_id: str = Field(min_length=1)
    origin_m: tuple[float, float, float]
    direction_unit: tuple[float, float, float]

    @model_validator(mode='after')
    def validate_vector(self) -> 'LeakDiagnosticSample':
        values = tuple(float(value) for value in (*self.origin_m, *self.direction_unit))
        if any(not isfinite(value) for value in values):
            raise ValueError('leak diagnostic sample values must be finite')
        length = sqrt(sum(float(value) * float(value) for value in self.direction_unit))
        if abs(length - 1.0) > 1e-6:
            raise ValueError('leak diagnostic direction must be normalized')
        return self


class LeakSamplingAuthority(BaseModel):
    model_config = ConfigDict(frozen=True)

    authority_id: str = Field(pattern=r'^r120-leak-sampling:[0-9a-f]{64}$')
    authority_version: Literal['1'] = '1'
    samples: tuple[LeakDiagnosticSample, ...]
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_identity(self) -> 'LeakSamplingAuthority':
        if not self.samples:
            raise ValueError('leak sampling authority requires at least one sample')
        ids = [sample.sample_id for sample in self.samples]
        if len(ids) != len(set(ids)):
            raise ValueError('leak diagnostic sample ids must be unique')
        core = self.model_dump(mode='json', exclude={'authority_id', 'semantic_hash_sha256'})
        expected = _semantic_hash(core)
        if self.semantic_hash_sha256 != expected:
            raise ValueError('leak sampling authority hash mismatch')
        if self.authority_id != f'r120-leak-sampling:{expected}':
            raise ValueError('leak sampling authority id mismatch')
        return self


class LeakPortalDiagnosticRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(pattern=r'^r120-leak-diagnostic-request:[0-9a-f]{64}$')
    request_semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    compiled_geometry_id: str = Field(pattern=r'^r120-compiled-geometry:[0-9a-f]{64}$')
    compiled_geometry_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    diagnostic_id: Literal['htdt.r120.ga_leak_portal_diagnostic'] = R120_LEAK_DIAGNOSTIC_ID
    diagnostic_version: Literal['1'] = R120_LEAK_DIAGNOSTIC_VERSION
    closed_boundary_expectation: bool
    sampling_authority: LeakSamplingAuthority

    @model_validator(mode='after')
    def validate_identity(self) -> 'LeakPortalDiagnosticRequest':
        core = self.model_dump(
            mode='json',
            exclude={'request_id', 'request_semantic_hash_sha256'},
        )
        expected = _semantic_hash(core)
        if self.request_semantic_hash_sha256 != expected:
            raise ValueError('leak diagnostic request hash mismatch')
        if self.request_id != f'r120-leak-diagnostic-request:{expected}':
            raise ValueError('leak diagnostic request id mismatch')
        return self


class RayEscapeEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    sample_id: str
    positive_intersection_count: int = Field(ge=0)
    escaped_without_intersection: bool


class LeakPortalFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    severity: DiagnosticSeverity
    message: str = Field(min_length=1)
    edge_keys: tuple[str, ...] = ()
    sample_ids: tuple[str, ...] = ()


class R120LeakPortalDiagnostic(BaseModel):
    model_config = ConfigDict(frozen=True)

    diagnostic_result_id: str = Field(pattern=r'^r120-leak-portal-diagnostic:[0-9a-f]{64}$')
    diagnostic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    request: LeakPortalDiagnosticRequest
    exact_compiled_geometry_id: str
    exact_compiled_geometry_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evidence_method: Literal['boundary_edge_topology_plus_explicit_ray_segments_v1'] = (
        'boundary_edge_topology_plus_explicit_ray_segments_v1'
    )
    boundary_edges: tuple[CompiledBoundaryEdge, ...]
    explicit_portal_boundary_edges: tuple[CompiledBoundaryEdge, ...]
    unintended_boundary_edges: tuple[CompiledBoundaryEdge, ...]
    portal_declaration_mismatch_edges: tuple[str, ...]
    ray_escape_evidence: tuple[RayEscapeEvidence, ...]
    findings: tuple[LeakPortalFinding, ...]
    unresolved_conditions: tuple[str, ...]
    portal_authority_ref: ExactExternalAuthorityRef | None = None

    @model_validator(mode='after')
    def validate_identity(self) -> 'R120LeakPortalDiagnostic':
        if self.exact_compiled_geometry_id != self.request.compiled_geometry_id:
            raise ValueError('leak diagnostic compiled geometry id mismatch')
        if self.exact_compiled_geometry_hash_sha256 != self.request.compiled_geometry_hash_sha256:
            raise ValueError('leak diagnostic compiled geometry hash mismatch')
        core = self.model_dump(mode='json', exclude={'diagnostic_result_id', 'diagnostic_hash_sha256'})
        expected = _semantic_hash(core)
        if self.diagnostic_hash_sha256 != expected:
            raise ValueError('leak diagnostic result hash mismatch')
        if self.diagnostic_result_id != f'r120-leak-portal-diagnostic:{expected}':
            raise ValueError('leak diagnostic result id mismatch')
        return self


def make_acoustic_region_authority(
    declarations: tuple[AcousticRegionDeclaration, ...],
) -> AcousticRegionAuthority:
    core = {'authority_version': '1', 'declarations': [item.model_dump(mode='json') for item in declarations]}
    digest = _semantic_hash(core)
    return AcousticRegionAuthority(
        authority_id=f'r120-acoustic-regions:{digest}',
        authority_version='1',
        declarations=declarations,
        semantic_hash_sha256=digest,
    )


def make_portal_authority(
    *,
    declaration_mode: PortalDeclarationMode,
    declarations: tuple[PortalDeclaration, ...] = (),
) -> PortalAuthority:
    core = {
        'authority_version': '1',
        'declaration_mode': declaration_mode,
        'declarations': [
            item.model_dump(mode='json', exclude_none=True)
            for item in declarations
        ],
    }
    digest = _semantic_hash(core)
    return PortalAuthority(
        authority_id=f'r120-portals:{digest}',
        authority_version='1',
        declaration_mode=declaration_mode,
        declarations=declarations,
        semantic_hash_sha256=digest,
    )


def make_boundary_termination_authority(
    *,
    declaration_mode: PortalDeclarationMode,
    declarations: tuple[BoundaryTerminationDeclaration, ...] = (),
) -> BoundaryTerminationAuthority:
    core = {
        'authority_version': '1',
        'declaration_mode': declaration_mode,
        'declarations': [item.model_dump(mode='json') for item in declarations],
    }
    digest = _semantic_hash(core)
    return BoundaryTerminationAuthority(
        authority_id=f'r120-boundary-terminations:{digest}',
        authority_version='1',
        declaration_mode=declaration_mode,
        declarations=declarations,
        semantic_hash_sha256=digest,
    )


def make_r120_geometry_compilation_request(
    revision: SceneRevision,
    *,
    geometric_tolerance_m: float,
    target_representation: CompilerTargetRepresentation = 'indexed_triangle_surface_v1',
    approximation_policy: ApproximationPolicy = 'none',
    tiny_feature_policy: TinyFeaturePolicy = 'preserve',
    coplanar_handling_policy: CoplanarHandlingPolicy = 'preserve_source_surface_boundaries',
    surface_triangulation_policy: SurfaceTriangulationPolicy = 'preserve_semantic_triangles',
    coordinate_convention: CoordinateConvention = 'htdt-x-right-y-rear-z-up',
    unit_convention: UnitConvention = 'metre',
    input_policy: CompilationInputPolicy = 'require_contract_ready',
) -> R120GeometryCompilationRequest:
    geometry = revision.document.r120_semantic_geometry
    if geometry is None:
        raise R120GeometryCompilationError('SceneRevision has no SemanticAcousticGeometry')
    core = {
        'scene_revision_id': revision.revision_id,
        'scene_revision_content_hash': revision.content_hash,
        'semantic_geometry_id': geometry.geometry_id,
        'semantic_geometry_hash_sha256': geometry.semantic_hash_sha256,
        'compiler_id': R120_GEOMETRY_COMPILER_ID,
        'compiler_version': R120_GEOMETRY_COMPILER_VERSION,
        'target_representation': target_representation,
        'geometric_tolerance_m': float(geometric_tolerance_m),
        'approximation_policy': approximation_policy,
        'tiny_feature_policy': tiny_feature_policy,
        'coplanar_handling_policy': coplanar_handling_policy,
        'surface_triangulation_policy': surface_triangulation_policy,
        'coordinate_convention': coordinate_convention,
        'unit_convention': unit_convention,
        'input_policy': input_policy,
    }
    digest = _semantic_hash(core)
    return R120GeometryCompilationRequest(
        request_id=f'r120-geometry-compile-request:{digest}',
        request_semantic_hash_sha256=digest,
        **core,
    )


def compile_r120_geometry(
    revision: SceneRevision,
    request: R120GeometryCompilationRequest,
    *,
    surface_boundary_bindings: tuple[SurfaceBoundaryAuthorityBinding, ...] = (),
    region_authority: AcousticRegionAuthority | None = None,
    portal_authority: PortalAuthority | None = None,
    boundary_termination_authority: BoundaryTerminationAuthority | None = None,
) -> R120CompiledGeometry:
    geometry = _validate_compilation_input(revision, request)
    if (
        geometry.geometry_compiler_readiness != 'ready_for_r120_geometry_compiler_contract'
        and request.input_policy == 'require_contract_ready'
    ):
        raise R120GeometryCompilationError(
            'SemanticAcousticGeometry is not ready_for_r120_geometry_compiler_contract'
        )

    surface_by_triangle: dict[str, str] = {}
    surface_lookup = {surface.surface_id: surface for surface in geometry.surfaces}
    for surface in geometry.surfaces:
        for triangle_id in surface.triangle_ids:
            surface_by_triangle[triangle_id] = surface.surface_id

    bindings = {item.source_surface_id: item for item in surface_boundary_bindings}
    if len(bindings) != len(surface_boundary_bindings):
        raise R120GeometryCompilationError('surface boundary bindings must be unique')
    unknown_binding_surfaces = set(bindings) - set(surface_lookup)
    if unknown_binding_surfaces:
        raise R120GeometryCompilationError(
            f'surface boundary binding references unknown surface: {sorted(unknown_binding_surfaces)}'
        )

    vertices = tuple(
        CompiledVertex(x_m=vertex.x_m, y_m=vertex.y_m, z_m=vertex.z_m)
        for vertex in geometry.vertices
    )
    compiled_triangles: list[CompiledTriangle] = []
    operations: list[ApproximationOperation] = []
    dropped: list[DroppedFeature] = []
    rejected_tiny: list[str] = []

    for triangle in geometry.triangles:
        surface_id = surface_by_triangle[triangle.triangle_id]
        max_edge = _triangle_max_edge_m(vertices, triangle.a, triangle.b, triangle.c)
        tiny = max_edge < request.geometric_tolerance_m
        if tiny and request.tiny_feature_policy == 'drop_below_tolerance':
            operations.append(
                ApproximationOperation(
                    operation='drop_tiny_triangle',
                    source_triangle_id=triangle.triangle_id,
                    source_surface_id=surface_id,
                    tolerance_m=request.geometric_tolerance_m,
                    measured_max_edge_m=max_edge,
                    reason='explicit compilation request drops triangles below geometric tolerance',
                )
            )
            dropped.append(
                DroppedFeature(
                    source_triangle_id=triangle.triangle_id,
                    source_surface_id=surface_id,
                    reason_code='tiny_feature_below_tolerance',
                    measured_max_edge_m=max_edge,
                    tolerance_m=request.geometric_tolerance_m,
                )
            )
            continue
        if tiny and request.tiny_feature_policy == 'reject_below_tolerance':
            rejected_tiny.append(triangle.triangle_id)
        compiled_triangles.append(
            CompiledTriangle(
                a=triangle.a,
                b=triangle.b,
                c=triangle.c,
                source_triangle_id=triangle.triangle_id,
                source_surface_id=surface_id,
            )
        )

    if not compiled_triangles:
        raise R120GeometryCompilationError('compilation produced no triangles')

    triangles = tuple(compiled_triangles)
    mapping: list[CompiledSurfaceMapping] = []
    for surface in geometry.surfaces:
        compiled_indices = tuple(
            index
            for index, triangle in enumerate(triangles)
            if triangle.source_surface_id == surface.surface_id
        )
        dropped_ids = tuple(
            item.source_triangle_id for item in dropped if item.source_surface_id == surface.surface_id
        )
        binding = bindings.get(surface.surface_id)
        mapping.append(
            CompiledSurfaceMapping(
                source_surface_id=surface.surface_id,
                source_surface_key=surface.surface_key,
                semantic_class=surface.semantic_class,
                source_triangle_ids=surface.triangle_ids,
                compiled_triangle_indices=compiled_indices,
                dropped_source_triangle_ids=dropped_ids,
                material_authority=binding.material_authority if binding else None,
                boundary_physics_authority=binding.boundary_physics_authority if binding else None,
            )
        )
    surface_mapping = tuple(mapping)

    topology_identity = _compiled_topology_identity(
        vertices,
        triangles,
        surface_mapping,
    )
    bounds = _bounding_volume(vertices)
    edge_uses = _edge_uses(triangles)
    boundary_edges = _compiled_boundary_edges(triangles, edge_uses)
    non_manifold_count = sum(1 for uses in edge_uses.values() if len(uses) > 2)
    closed_shell = not boundary_edges and non_manifold_count == 0
    volume = _enclosed_volume_m3(vertices, triangles) if closed_shell else None
    shell_diagnostics = ClosedShellDiagnostics(
        boundary_edge_count=len(boundary_edges),
        non_manifold_edge_count=non_manifold_count,
        closed_shell=closed_shell,
        enclosed_volume_m3=volume,
    )

    warnings: list[str] = []
    unresolved: list[str] = []
    if geometry.geometry_compiler_readiness != 'ready_for_r120_geometry_compiler_contract':
        warnings.append(
            f'input semantic geometry readiness is {geometry.geometry_compiler_readiness}; '
            'compiled representation is diagnostic-only'
        )
        unresolved.append('input_semantic_geometry_not_compiler_contract_ready')
    if rejected_tiny:
        warnings.append(f'{len(rejected_tiny)} triangle(s) are below geometric tolerance and rejected by policy')
        unresolved.append('tiny_features_rejected')
    if dropped:
        warnings.append(f'{len(dropped)} triangle(s) dropped by explicit tiny feature approximation policy')
    if non_manifold_count:
        unresolved.append('compiled_non_manifold_edges')
    if any(item.semantic_class == 'unknown' for item in surface_mapping):
        unresolved.append('unknown_semantic_surface')

    region_missing = region_authority is None
    portal_missing = portal_authority is None or portal_authority.declaration_mode == 'unknown'
    material_missing = any(item.material_authority is None for item in surface_mapping)
    boundary_physics_missing = any(
        item.boundary_physics_authority is None for item in surface_mapping
    )

    if region_missing:
        unresolved.append('region_definition_missing')
    else:
        unresolved.extend(_validate_region_authority(region_authority, surface_lookup))
    if portal_missing:
        unresolved.append('portal_definition_missing')
    else:
        unresolved.extend(_validate_portal_authority_surface_refs(portal_authority, surface_lookup))
    if material_missing:
        unresolved.append('material_assignment_missing')
    if boundary_physics_missing:
        unresolved.append('boundary_physics_missing')

    if boundary_edges:
        if portal_authority is None or portal_authority.declaration_mode == 'unknown':
            warnings.append('compiled geometry has open boundary edges with no explicit portal authority')
        if boundary_termination_authority is None:
            unresolved.append('boundary_termination_definition_missing')
        elif boundary_termination_authority.declaration_mode == 'unknown':
            unresolved.append('boundary_termination_definition_missing')
        else:
            unresolved.extend(
                _validate_termination_authority_surface_refs(
                    boundary_termination_authority,
                    surface_lookup,
                )
            )

    unresolved = list(dict.fromkeys(unresolved))
    geometry_compiled = True
    authority_complete = not unresolved
    readiness = R120GeometryReadiness(
        geometry_compiled=geometry_compiled,
        wave_geometry_ready=authority_complete,
        geometric_acoustics_geometry_ready=authority_complete,
        material_assignment_missing=material_missing,
        region_definition_missing=region_missing,
        portal_definition_missing=portal_missing,
        boundary_physics_missing=boundary_physics_missing,
        unresolved_conditions=tuple(unresolved),
    )
    maximum_dropped_extent = max(
        (item.measured_max_edge_m for item in dropped),
        default=0.0,
    )
    approximation_error_status = (
        'not_computed_for_dropped_features'
        if dropped
        else 'exact_preservation'
    )
    region_ref = _authority_ref(region_authority) if region_authority else None
    portal_ref = _authority_ref(portal_authority) if portal_authority else None
    termination_ref = (
        _authority_ref(boundary_termination_authority)
        if boundary_termination_authority
        else None
    )
    core = {
        'authority_version': '1',
        'request': request,
        'exact_scene_revision_id': revision.revision_id,
        'exact_scene_revision_content_hash': revision.content_hash,
        'exact_semantic_geometry_id': geometry.geometry_id,
        'exact_semantic_geometry_hash_sha256': geometry.semantic_hash_sha256,
        'vertices': vertices,
        'triangles': triangles,
        'surface_mapping': surface_mapping,
        'topology_identity_sha256': topology_identity,
        'bounding_volume': bounds,
        'closed_shell_diagnostics': shell_diagnostics,
        'approximation_operations': tuple(operations),
        'dropped_features': tuple(dropped),
        'geometric_tolerance_m': request.geometric_tolerance_m,
        'approximation_error_bound_m': None,
        'approximation_error_status': approximation_error_status,
        'maximum_dropped_feature_extent_m': maximum_dropped_extent,
        'compiler_warnings': tuple(warnings),
        'unresolved_conditions': tuple(unresolved),
        'readiness': readiness,
        'region_authority_ref': region_ref,
        'portal_authority_ref': portal_ref,
        'boundary_termination_authority_ref': termination_ref,
    }
    digest = _semantic_hash(_jsonable(core))
    return R120CompiledGeometry(
        compiled_geometry_id=f'r120-compiled-geometry:{digest}',
        compiled_hash_sha256=digest,
        **core,
    )


def make_leak_sampling_authority(
    samples: tuple[LeakDiagnosticSample, ...],
) -> LeakSamplingAuthority:
    core = {'authority_version': '1', 'samples': [item.model_dump(mode='json') for item in samples]}
    digest = _semantic_hash(core)
    return LeakSamplingAuthority(
        authority_id=f'r120-leak-sampling:{digest}',
        authority_version='1',
        samples=samples,
        semantic_hash_sha256=digest,
    )


def make_leak_portal_diagnostic_request(
    compiled: R120CompiledGeometry,
    *,
    closed_boundary_expectation: bool,
    sampling_authority: LeakSamplingAuthority,
) -> LeakPortalDiagnosticRequest:
    core = {
        'compiled_geometry_id': compiled.compiled_geometry_id,
        'compiled_geometry_hash_sha256': compiled.compiled_hash_sha256,
        'diagnostic_id': R120_LEAK_DIAGNOSTIC_ID,
        'diagnostic_version': R120_LEAK_DIAGNOSTIC_VERSION,
        'closed_boundary_expectation': bool(closed_boundary_expectation),
        'sampling_authority': sampling_authority.model_dump(mode='json'),
    }
    digest = _semantic_hash(core)
    return LeakPortalDiagnosticRequest(
        request_id=f'r120-leak-diagnostic-request:{digest}',
        request_semantic_hash_sha256=digest,
        **core,
    )


def diagnose_r120_leak_and_portals(
    compiled: R120CompiledGeometry,
    request: LeakPortalDiagnosticRequest,
    *,
    portal_authority: PortalAuthority | None = None,
) -> R120LeakPortalDiagnostic:
    if request.compiled_geometry_id != compiled.compiled_geometry_id:
        raise R120GeometryCompilationError('leak diagnostic compiled geometry id mismatch')
    if request.compiled_geometry_hash_sha256 != compiled.compiled_hash_sha256:
        raise R120GeometryCompilationError('leak diagnostic compiled geometry hash mismatch')

    edge_uses = _edge_uses(compiled.triangles)
    actual_edges = _compiled_boundary_edges(compiled.triangles, edge_uses)
    actual_by_key = {edge.canonical_key(): edge for edge in actual_edges}
    declared_keys: set[tuple[str, int, int]] = set()
    portal_ref = None
    portal_unknown = portal_authority is None or portal_authority.declaration_mode == 'unknown'
    if portal_authority is not None:
        portal_ref = _authority_ref(portal_authority)
        for declaration in portal_authority.declarations:
            for edge in declaration.boundary_edges:
                declared_keys.add(edge.canonical_key())

    matched_keys = set(actual_by_key).intersection(declared_keys)
    mismatch_keys = declared_keys - set(actual_by_key)
    if portal_unknown:
        unintended_keys = set(actual_by_key)
    else:
        unintended_keys = set(actual_by_key) - declared_keys

    explicit_edges = tuple(actual_by_key[key] for key in sorted(matched_keys))
    unintended_edges = tuple(actual_by_key[key] for key in sorted(unintended_keys))
    mismatch_strings = tuple(_edge_key_string(key) for key in sorted(mismatch_keys))

    ray_evidence = tuple(
        _ray_escape_evidence(compiled, sample)
        for sample in request.sampling_authority.samples
    )
    escaped_ids = tuple(
        evidence.sample_id for evidence in ray_evidence if evidence.escaped_without_intersection
    )

    findings: list[LeakPortalFinding] = []
    unresolved: list[str] = []
    if portal_unknown:
        findings.append(
            LeakPortalFinding(
                code='portal_definition_unknown',
                severity='blocked',
                message='Portal authority is undefined; geometric openings cannot be classified as intentional portals.',
            )
        )
        unresolved.append('portal_definition_missing')
    if mismatch_keys:
        findings.append(
            LeakPortalFinding(
                code='portal_boundary_mismatch',
                severity='error',
                message='Explicit portal declaration does not match compiled open boundary edges.',
                edge_keys=mismatch_strings,
            )
        )
        unresolved.append('portal_boundary_mismatch')
    if explicit_edges:
        findings.append(
            LeakPortalFinding(
                code='explicit_portal_opening',
                severity='info',
                message='Compiled open boundary edges are explicitly covered by portal declarations.',
                edge_keys=tuple(_edge_key_string(edge.canonical_key()) for edge in explicit_edges),
            )
        )
    if unintended_edges:
        severity: DiagnosticSeverity = 'error' if request.closed_boundary_expectation else 'warning'
        findings.append(
            LeakPortalFinding(
                code='unintended_geometric_opening',
                severity=severity,
                message='Compiled geometry contains open boundary edges not covered by an explicit portal declaration.',
                edge_keys=tuple(_edge_key_string(edge.canonical_key()) for edge in unintended_edges),
            )
        )
        unresolved.append('unintended_geometric_opening')
    if request.closed_boundary_expectation and not actual_edges:
        findings.append(
            LeakPortalFinding(
                code='closed_boundary_expectation_satisfied',
                severity='info',
                message='No open boundary edges were found in the compiled geometry.',
            )
        )
    if escaped_ids:
        if request.closed_boundary_expectation and (unintended_edges or portal_unknown):
            severity = 'error'
            code = 'unintended_ray_escape'
            unresolved.append('unintended_ray_escape')
        elif explicit_edges and not unintended_edges and not mismatch_keys:
            severity = 'info'
            code = 'explicit_portal_ray_escape'
        else:
            severity = 'warning'
            code = 'ray_escape_evidence'
        findings.append(
            LeakPortalFinding(
                code=code,
                severity=severity,
                message='One or more explicitly sampled ray segments escaped without intersecting compiled triangles.',
                sample_ids=escaped_ids,
            )
        )
    else:
        findings.append(
            LeakPortalFinding(
                code='sampled_ray_escape_not_observed',
                severity='info',
                message='All explicitly sampled rays intersected at least one compiled triangle.',
            )
        )

    unresolved = list(dict.fromkeys(unresolved))
    core = {
        'request': request,
        'exact_compiled_geometry_id': compiled.compiled_geometry_id,
        'exact_compiled_geometry_hash_sha256': compiled.compiled_hash_sha256,
        'evidence_method': 'boundary_edge_topology_plus_explicit_ray_segments_v1',
        'boundary_edges': actual_edges,
        'explicit_portal_boundary_edges': explicit_edges,
        'unintended_boundary_edges': unintended_edges,
        'portal_declaration_mismatch_edges': mismatch_strings,
        'ray_escape_evidence': ray_evidence,
        'findings': tuple(findings),
        'unresolved_conditions': tuple(unresolved),
        'portal_authority_ref': portal_ref,
    }
    digest = _semantic_hash(_jsonable(core))
    return R120LeakPortalDiagnostic(
        diagnostic_result_id=f'r120-leak-portal-diagnostic:{digest}',
        diagnostic_hash_sha256=digest,
        **core,
    )


def serialize_r120_compiled_geometry(compiled: R120CompiledGeometry) -> str:
    return _canonical_json(compiled.model_dump(mode='json'))


def deserialize_r120_compiled_geometry(payload: str) -> R120CompiledGeometry:
    return R120CompiledGeometry.model_validate(json.loads(payload))


def serialize_r120_leak_portal_diagnostic(result: R120LeakPortalDiagnostic) -> str:
    return _canonical_json(result.model_dump(mode='json'))


def deserialize_r120_leak_portal_diagnostic(payload: str) -> R120LeakPortalDiagnostic:
    return R120LeakPortalDiagnostic.model_validate(json.loads(payload))


def _validate_compilation_input(
    revision: SceneRevision,
    request: R120GeometryCompilationRequest,
) -> SemanticAcousticGeometry:
    geometry = revision.document.r120_semantic_geometry
    if geometry is None:
        raise R120GeometryCompilationError('SceneRevision has no SemanticAcousticGeometry')
    if request.scene_revision_id != revision.revision_id:
        raise R120GeometryCompilationError('compilation request SceneRevision id mismatch')
    if request.scene_revision_content_hash != revision.content_hash:
        raise R120GeometryCompilationError('compilation request SceneRevision content hash mismatch')
    if request.semantic_geometry_id != geometry.geometry_id:
        raise R120GeometryCompilationError('compilation request SemanticAcousticGeometry id mismatch')
    if request.semantic_geometry_hash_sha256 != geometry.semantic_hash_sha256:
        raise R120GeometryCompilationError('compilation request SemanticAcousticGeometry hash mismatch')
    return geometry


def _authority_ref(
    authority: AcousticRegionAuthority | PortalAuthority | BoundaryTerminationAuthority,
) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=authority.authority_id,
        authority_version=authority.authority_version,
        semantic_hash_sha256=authority.semantic_hash_sha256,
    )


def _validate_region_authority(
    authority: AcousticRegionAuthority,
    surfaces: dict[str, object],
) -> list[str]:
    unresolved: list[str] = []
    referenced = {
        surface_id
        for declaration in authority.declarations
        for surface_id in declaration.boundary_surface_ids
    }
    if not authority.declarations:
        unresolved.append('region_definition_missing')
    if referenced - set(surfaces):
        unresolved.append('region_definition_references_unknown_surface')
    return unresolved


def _validate_portal_authority_surface_refs(
    authority: PortalAuthority,
    surfaces: dict[str, object],
) -> list[str]:
    if authority.declaration_mode != 'explicit_list':
        return []
    referenced = {
        edge.source_surface_id
        for declaration in authority.declarations
        for edge in declaration.boundary_edges
    }
    if referenced - set(surfaces):
        return ['portal_definition_references_unknown_surface']
    return []


def _validate_termination_authority_surface_refs(
    authority: BoundaryTerminationAuthority,
    surfaces: dict[str, object],
) -> list[str]:
    if authority.declaration_mode != 'explicit_list':
        return []
    referenced = {
        edge.source_surface_id
        for declaration in authority.declarations
        for edge in declaration.boundary_edges
    }
    if referenced - set(surfaces):
        return ['boundary_termination_references_unknown_surface']
    return []


def _triangle_max_edge_m(
    vertices: tuple[CompiledVertex, ...],
    a: int,
    b: int,
    c: int,
) -> float:
    return max(
        _distance(vertices[a], vertices[b]),
        _distance(vertices[b], vertices[c]),
        _distance(vertices[c], vertices[a]),
    )


def _distance(left: CompiledVertex, right: CompiledVertex) -> float:
    return sqrt(
        (left.x_m - right.x_m) ** 2
        + (left.y_m - right.y_m) ** 2
        + (left.z_m - right.z_m) ** 2
    )


def _compiled_topology_identity(
    vertices: tuple[CompiledVertex, ...],
    triangles: tuple[CompiledTriangle, ...],
    surface_mapping: tuple[CompiledSurfaceMapping, ...],
) -> str:
    # Topology identity deliberately excludes material/boundary physics authorities.
    # Those authorities remain part of the compiled semantic hash, but changing
    # physics must not pretend that vertex/triangle/surface topology changed.
    topology_mapping = [
        {
            'source_surface_id': item.source_surface_id,
            'source_surface_key': item.source_surface_key,
            'semantic_class': item.semantic_class,
            'source_triangle_ids': list(item.source_triangle_ids),
            'compiled_triangle_indices': list(item.compiled_triangle_indices),
            'dropped_source_triangle_ids': list(item.dropped_source_triangle_ids),
        }
        for item in surface_mapping
    ]
    return _semantic_hash(
        {
            'vertices': [item.model_dump(mode='json') for item in vertices],
            'triangles': [item.model_dump(mode='json') for item in triangles],
            'surface_mapping': topology_mapping,
        }
    )


def _bounding_volume(vertices: tuple[CompiledVertex, ...]) -> BoundingVolume:
    if not vertices:
        raise R120GeometryCompilationError('compiled geometry has no vertices')
    xs = [vertex.x_m for vertex in vertices]
    ys = [vertex.y_m for vertex in vertices]
    zs = [vertex.z_m for vertex in vertices]
    return BoundingVolume(
        min_x_m=min(xs),
        min_y_m=min(ys),
        min_z_m=min(zs),
        max_x_m=max(xs),
        max_y_m=max(ys),
        max_z_m=max(zs),
    )


def _edge_uses(
    triangles: tuple[CompiledTriangle, ...],
) -> dict[tuple[int, int], list[int]]:
    uses: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, triangle in enumerate(triangles):
        for edge in (
            _normalized_edge(triangle.a, triangle.b),
            _normalized_edge(triangle.b, triangle.c),
            _normalized_edge(triangle.c, triangle.a),
        ):
            uses[edge].append(index)
    return dict(uses)


def _compiled_boundary_edges(
    triangles: tuple[CompiledTriangle, ...],
    edge_uses: dict[tuple[int, int], list[int]],
) -> tuple[CompiledBoundaryEdge, ...]:
    result: list[CompiledBoundaryEdge] = []
    for (a, b), uses in sorted(edge_uses.items()):
        if len(uses) != 1:
            continue
        triangle = triangles[uses[0]]
        result.append(
            CompiledBoundaryEdge(
                vertex_a=a,
                vertex_b=b,
                source_surface_id=triangle.source_surface_id,
            )
        )
    return tuple(result)


def _enclosed_volume_m3(
    vertices: tuple[CompiledVertex, ...],
    triangles: tuple[CompiledTriangle, ...],
) -> float:
    signed_six_volume = 0.0
    for triangle in triangles:
        a = vertices[triangle.a]
        b = vertices[triangle.b]
        c = vertices[triangle.c]
        signed_six_volume += (
            a.x_m * (b.y_m * c.z_m - b.z_m * c.y_m)
            - a.y_m * (b.x_m * c.z_m - b.z_m * c.x_m)
            + a.z_m * (b.x_m * c.y_m - b.y_m * c.x_m)
        )
    return abs(signed_six_volume) / 6.0


def _ray_escape_evidence(
    compiled: R120CompiledGeometry,
    sample: LeakDiagnosticSample,
) -> RayEscapeEvidence:
    count = 0
    for triangle in compiled.triangles:
        if _ray_intersects_triangle(
            sample.origin_m,
            sample.direction_unit,
            compiled.vertices[triangle.a],
            compiled.vertices[triangle.b],
            compiled.vertices[triangle.c],
        ):
            count += 1
    return RayEscapeEvidence(
        sample_id=sample.sample_id,
        positive_intersection_count=count,
        escaped_without_intersection=count == 0,
    )


def _ray_intersects_triangle(
    origin: tuple[float, float, float],
    direction: tuple[float, float, float],
    va: CompiledVertex,
    vb: CompiledVertex,
    vc: CompiledVertex,
) -> bool:
    epsilon = 1.0e-10
    edge1 = (vb.x_m - va.x_m, vb.y_m - va.y_m, vb.z_m - va.z_m)
    edge2 = (vc.x_m - va.x_m, vc.y_m - va.y_m, vc.z_m - va.z_m)
    h = _cross(direction, edge2)
    determinant = _dot(edge1, h)
    if abs(determinant) < epsilon:
        return False
    inverse = 1.0 / determinant
    s = (origin[0] - va.x_m, origin[1] - va.y_m, origin[2] - va.z_m)
    u = inverse * _dot(s, h)
    if u < -epsilon or u > 1.0 + epsilon:
        return False
    q = _cross(s, edge1)
    v = inverse * _dot(direction, q)
    if v < -epsilon or u + v > 1.0 + epsilon:
        return False
    distance = inverse * _dot(edge2, q)
    return distance > epsilon


def _cross(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _dot(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def _edge_key_string(key: tuple[str, int, int]) -> str:
    return f'{key[0]}:{key[1]}-{key[2]}'


def _jsonable(payload: object) -> object:
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode='json')
    if isinstance(payload, dict):
        return {key: _jsonable(value) for key, value in payload.items()}
    if isinstance(payload, tuple):
        return [_jsonable(value) for value in payload]
    if isinstance(payload, list):
        return [_jsonable(value) for value in payload]
    return payload
