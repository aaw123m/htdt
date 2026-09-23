from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .raw_mesh import (
    RawMeshDiagnosticFinding,
    RawMeshDiagnosticProfile,
    RawMeshTriangle,
    RawMeshVertex,
    RawVisualMesh,
    diagnose_raw_visual_mesh,
)

from .raw_mesh_repair import (
    RawMeshRepairLineageRef,
    RepairedRawMesh,
    RepairedRawMeshDiagnosticResult,
    diagnose_repaired_raw_mesh,
    make_raw_mesh_repair_lineage_ref,
    repaired_triangle_ids,
)


SEMANTIC_GEOMETRY_ALGORITHM = 'htdt.r120.semantic_geometry_conversion'
SEMANTIC_GEOMETRY_ALGORITHM_VERSION = '1'

SemanticSurfaceClass = Literal['room_boundary', 'object_surface', 'unknown']
GeometryCompilerReadiness = Literal[
    'blocked_by_geometry',
    'blocked_by_surface_semantics',
    'ready_for_r120_geometry_compiler_contract',
]


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _remove_absent_raw_mesh_repair_lineage(payload: dict[str, object]) -> None:
    request = payload.get('conversion_request')
    if isinstance(request, dict) and request.get('raw_mesh_repair_lineage') is None:
        request.pop('raw_mesh_repair_lineage', None)


def _semantic_hash(payload: object) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


class SemanticGeometryConversionError(ValueError):
    pass


class SemanticVertex(BaseModel):
    model_config = ConfigDict(frozen=True)

    x_m: float
    y_m: float
    z_m: float

    @field_validator('x_m', 'y_m', 'z_m')
    @classmethod
    def finite(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value):
            raise ValueError('semantic vertex coordinates must be finite')
        return value


class SemanticCoordinateTransform(BaseModel):
    """Explicit affine mapping from source asset coordinates into HTDT scene metres."""

    model_config = ConfigDict(frozen=True)

    matrix_source_to_scene_m: tuple[
        tuple[float, float, float, float],
        tuple[float, float, float, float],
        tuple[float, float, float, float],
        tuple[float, float, float, float],
    ]
    provenance: Literal['explicit_user_authority', 'explicit_import_metadata']
    reason: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_affine_transform(self) -> 'SemanticCoordinateTransform':
        rows = self.matrix_source_to_scene_m
        if any(not isfinite(float(value)) for row in rows for value in row):
            raise ValueError('source-to-scene transform values must be finite')
        if tuple(float(value) for value in rows[3]) != (0.0, 0.0, 0.0, 1.0):
            raise ValueError('source-to-scene transform must be affine with last row 0,0,0,1')
        a, b, c = rows[0][:3]
        d, e, f = rows[1][:3]
        g, h, i = rows[2][:3]
        determinant = (
            a * (e * i - f * h)
            - b * (d * i - f * g)
            + c * (d * h - e * g)
        )
        if abs(determinant) <= 1.0e-15:
            raise ValueError('source-to-scene transform must be non-singular')
        return self

    def semantic_hash(self) -> str:
        return _semantic_hash(self.model_dump(mode='json'))


def explicit_identity_source_to_scene_transform(*, reason: str) -> SemanticCoordinateTransform:
    return SemanticCoordinateTransform(
        matrix_source_to_scene_m=(
            (1.0, 0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        ),
        provenance='explicit_user_authority',
        reason=reason,
    )


class SemanticGeometryConversionProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    algorithm_id: Literal['htdt.r120.semantic_geometry_conversion'] = SEMANTIC_GEOMETRY_ALGORITHM
    algorithm_version: Literal['1'] = SEMANTIC_GEOMETRY_ALGORITHM_VERSION
    diagnostic_profile: RawMeshDiagnosticProfile = Field(default_factory=RawMeshDiagnosticProfile)
    require_clean_geometry_for_compiler_contract: Literal[True] = True
    require_explicit_surface_semantics_for_compiler_contract: Literal[True] = True
    material_assignment_policy: Literal['external_authority_required_downstream'] = (
        'external_authority_required_downstream'
    )
    region_portal_termination_policy: Literal['never_infer'] = 'never_infer'

    def semantic_hash(self) -> str:
        return _semantic_hash(self.model_dump(mode='json'))


class RemoveTriangleRepair(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal['remove_triangle'] = 'remove_triangle'
    triangle_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class FlipTriangleRepair(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal['flip_triangle'] = 'flip_triangle'
    triangle_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class AddTriangleRepair(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal['add_triangle'] = 'add_triangle'
    a: int = Field(ge=0)
    b: int = Field(ge=0)
    c: int = Field(ge=0)
    repair_label: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @model_validator(mode='after')
    def distinct_indices(self) -> 'AddTriangleRepair':
        if len({self.a, self.b, self.c}) != 3:
            raise ValueError('added triangle indices must be distinct')
        return self


class SetVertexRepair(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal['set_vertex'] = 'set_vertex'
    vertex_index: int = Field(ge=0)
    expected_before: SemanticVertex
    after: SemanticVertex
    reason: str = Field(min_length=1)


RepairAction = RemoveTriangleRepair | FlipTriangleRepair | AddTriangleRepair | SetVertexRepair


class SurfaceSemanticAssignment(BaseModel):
    model_config = ConfigDict(frozen=True)

    surface_key: str = Field(min_length=1)
    triangle_ids: tuple[str, ...]
    semantic_class: SemanticSurfaceClass

    @model_validator(mode='after')
    def valid_triangle_ids(self) -> 'SurfaceSemanticAssignment':
        if not self.triangle_ids:
            raise ValueError('surface assignment must include at least one triangle')
        if len(self.triangle_ids) != len(set(self.triangle_ids)):
            raise ValueError('surface assignment triangle_ids must be unique')
        return self


class SemanticGeometryConversionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(pattern=r'^semantic-geometry-request:[0-9a-f]{64}$')
    input_raw_mesh_id: str = Field(pattern=r'^raw-mesh:[0-9a-f]{64}$')
    input_raw_mesh_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    input_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    input_diagnostic_id: str = Field(
        pattern=r'^(?:raw-mesh-diagnostic|repaired-raw-mesh-diagnostic):[0-9a-f]{64}$'
    )
    input_diagnostic_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    raw_mesh_repair_lineage: RawMeshRepairLineageRef | None = None
    source_scene_revision_id: str | None
    source_to_scene_transform: SemanticCoordinateTransform
    profile: SemanticGeometryConversionProfile
    profile_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    repairs: tuple[RepairAction, ...] = ()
    surface_assignments: tuple[SurfaceSemanticAssignment, ...] = ()

    @model_validator(mode='after')
    def validate_identity(self) -> 'SemanticGeometryConversionRequest':
        if self.profile_semantic_hash != self.profile.semantic_hash():
            raise ValueError('semantic geometry conversion profile hash mismatch')
        lineage = self.raw_mesh_repair_lineage
        if lineage is not None:
            if lineage.source_raw_mesh_id != self.input_raw_mesh_id:
                raise ValueError('raw-mesh repair lineage source id mismatch')
            if lineage.source_raw_mesh_semantic_hash != self.input_raw_mesh_semantic_hash:
                raise ValueError('raw-mesh repair lineage source hash mismatch')
            if lineage.post_repair_diagnostic_id != self.input_diagnostic_id:
                raise ValueError('raw-mesh repair lineage post diagnostic id mismatch')
            if (
                lineage.post_repair_diagnostic_semantic_hash
                != self.input_diagnostic_semantic_hash
            ):
                raise ValueError('raw-mesh repair lineage post diagnostic hash mismatch')
        exclude = {'request_id'}
        if lineage is None:
            exclude.add('raw_mesh_repair_lineage')
        payload = self.model_dump(mode='json', exclude=exclude)
        expected = f"semantic-geometry-request:{_semantic_hash(payload)}"
        if self.request_id != expected:
            raise ValueError('semantic geometry conversion request_id does not match inputs')
        return self

    def semantic_hash(self) -> str:
        exclude = {'raw_mesh_repair_lineage'} if self.raw_mesh_repair_lineage is None else set()
        return _semantic_hash(self.model_dump(mode='json', exclude=exclude))


class RepairLineageStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(pattern=r'^semantic-geometry-repair:[0-9a-f]{64}$')
    action: RepairAction
    input_geometry_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    output_geometry_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    provenance: Literal['explicit_guided_repair'] = 'explicit_guided_repair'

    @model_validator(mode='after')
    def validate_operation_id(self) -> 'RepairLineageStep':
        expected = repair_operation_id(self.action, self.input_geometry_hash)
        if self.operation_id != expected:
            raise ValueError('repair operation_id does not match the explicit repair action')
        return self


class SemanticTriangle(BaseModel):
    model_config = ConfigDict(frozen=True)

    triangle_id: str = Field(min_length=1)
    a: int = Field(ge=0)
    b: int = Field(ge=0)
    c: int = Field(ge=0)
    source_triangle_id: str | None = None
    source_primitive: str | None = None
    repair_operation_ids: tuple[str, ...] = ()

    @model_validator(mode='after')
    def distinct_indices(self) -> 'SemanticTriangle':
        if len({self.a, self.b, self.c}) != 3:
            raise ValueError('semantic triangle indices must be distinct')
        return self


class SemanticSurface(BaseModel):
    model_config = ConfigDict(frozen=True)

    surface_id: str = Field(pattern=r'^semantic-surface:[0-9a-f]{64}$')
    surface_key: str = Field(min_length=1)
    semantic_class: SemanticSurfaceClass
    triangle_ids: tuple[str, ...]
    assignment_provenance: Literal['explicit', 'unassigned']

    @model_validator(mode='after')
    def valid_membership(self) -> 'SemanticSurface':
        if not self.triangle_ids:
            raise ValueError('semantic surface must include at least one triangle')
        if len(self.triangle_ids) != len(set(self.triangle_ids)):
            raise ValueError('semantic surface triangle_ids must be unique')
        if self.assignment_provenance == 'unassigned' and self.semantic_class != 'unknown':
            raise ValueError('unassigned surfaces must remain unknown')
        return self


class GuidedRepairSuggestion(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_code: str = Field(min_length=1)
    suggested_action_kinds: tuple[str, ...]
    guidance: str = Field(min_length=1)
    automatic: Literal[False] = False


class SemanticAcousticGeometry(BaseModel):
    """R120 semantic geometry authority embedded in a SceneRevision.

    This is a derived immutable representation. It never mutates or replaces the
    source RawVisualMesh bytes, and it does not claim material/boundary physics.
    """

    model_config = ConfigDict(frozen=True)

    geometry_id: str = Field(pattern=r'^semantic-acoustic-geometry:[0-9a-f]{64}$')
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    input_raw_mesh_id: str = Field(pattern=r'^raw-mesh:[0-9a-f]{64}$')
    input_raw_mesh_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    input_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    input_diagnostic_id: str = Field(
        pattern=r'^(?:raw-mesh-diagnostic|repaired-raw-mesh-diagnostic):[0-9a-f]{64}$'
    )
    input_diagnostic_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_scene_revision_id: str | None
    source_to_scene_transform: SemanticCoordinateTransform
    conversion_request: SemanticGeometryConversionRequest
    conversion_request_id: str = Field(pattern=r'^semantic-geometry-request:[0-9a-f]{64}$')
    conversion_profile_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    lineage_root_geometry_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    repair_lineage: tuple[RepairLineageStep, ...]
    derived_geometry_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    vertices: tuple[SemanticVertex, ...]
    triangles: tuple[SemanticTriangle, ...]
    surfaces: tuple[SemanticSurface, ...]
    conversion_findings: tuple[RawMeshDiagnosticFinding, ...]
    guided_repair_suggestions: tuple[GuidedRepairSuggestion, ...]
    unresolved_conditions: tuple[str, ...]
    geometry_compiler_readiness: GeometryCompilerReadiness
    solver_ready: Literal[False] = False
    material_assignment_status: Literal['not_part_of_this_authority'] = 'not_part_of_this_authority'
    acoustic_regions_status: Literal['not_inferred'] = 'not_inferred'
    portals_status: Literal['not_inferred'] = 'not_inferred'
    boundary_terminations_status: Literal['not_inferred'] = 'not_inferred'

    @model_validator(mode='after')
    def validate_identity_and_membership(self) -> 'SemanticAcousticGeometry':
        request = self.conversion_request
        if request.request_id != self.conversion_request_id:
            raise ValueError('conversion request id mismatch')
        if request.input_raw_mesh_id != self.input_raw_mesh_id:
            raise ValueError('conversion request raw mesh id mismatch')
        if request.input_raw_mesh_semantic_hash != self.input_raw_mesh_semantic_hash:
            raise ValueError('conversion request raw mesh semantic hash mismatch')
        if request.input_asset_sha256 != self.input_asset_sha256:
            raise ValueError('conversion request original asset hash mismatch')
        if request.input_diagnostic_id != self.input_diagnostic_id:
            raise ValueError('conversion request diagnostic id mismatch')
        if request.input_diagnostic_semantic_hash != self.input_diagnostic_semantic_hash:
            raise ValueError('conversion request diagnostic hash mismatch')
        if request.source_scene_revision_id != self.source_scene_revision_id:
            raise ValueError('conversion request SceneRevision binding mismatch')
        if request.source_to_scene_transform != self.source_to_scene_transform:
            raise ValueError('conversion request source-to-scene transform mismatch')
        if request.profile_semantic_hash != self.conversion_profile_semantic_hash:
            raise ValueError('conversion request profile hash mismatch')

        if self.derived_geometry_hash != _topology_hash(self.vertices, self.triangles):
            raise ValueError('derived geometry hash does not match vertices/triangles')

        triangle_ids = {triangle.triangle_id for triangle in self.triangles}
        assigned: set[str] = set()
        for surface in self.surfaces:
            if not set(surface.triangle_ids) <= triangle_ids:
                raise ValueError('semantic surface references an unknown triangle')
            overlap = assigned.intersection(surface.triangle_ids)
            if overlap:
                raise ValueError(f'triangle assigned to multiple semantic surfaces: {sorted(overlap)}')
            assigned.update(surface.triangle_ids)
            expected_surface_id = _surface_id(
                self.input_raw_mesh_id,
                surface.surface_key,
            )
            if surface.surface_id != expected_surface_id:
                raise ValueError('semantic surface_id does not match geometry and assignment')
        if assigned != triangle_ids:
            raise ValueError('every semantic triangle must belong to exactly one semantic surface')

        core = self.model_dump(mode='json', exclude={'geometry_id', 'semantic_hash_sha256'})
        _remove_absent_raw_mesh_repair_lineage(core)
        expected_hash = _semantic_hash(core)
        if self.semantic_hash_sha256 != expected_hash:
            raise ValueError('semantic acoustic geometry hash mismatch')
        if self.geometry_id != f'semantic-acoustic-geometry:{expected_hash}':
            raise ValueError('semantic acoustic geometry_id does not match semantic hash')
        return self

    def semantic_hash(self) -> str:
        return self.semantic_hash_sha256


def raw_triangle_ids(mesh: RawVisualMesh) -> tuple[str, ...]:
    return tuple(_raw_triangle_id(mesh, index, triangle) for index, triangle in enumerate(mesh.triangles))


def repair_operation_id(action: RepairAction, input_geometry_hash: str) -> str:
    identity = _semantic_hash(
        {
            'input_geometry_hash': input_geometry_hash,
            'action': action.model_dump(mode='json'),
        }
    )
    return f'semantic-geometry-repair:{identity}'


def added_triangle_id(action: AddTriangleRepair, operation_id: str) -> str:
    return f"semantic-triangle-added:{_semantic_hash({'operation_id': operation_id, 'repair_label': action.repair_label})}"


def make_semantic_geometry_conversion_request(
    mesh: RawVisualMesh,
    *,
    source_scene_revision_id: str | None,
    source_to_scene_transform: SemanticCoordinateTransform,
    profile: SemanticGeometryConversionProfile | None = None,
    repairs: tuple[RepairAction, ...] = (),
    surface_assignments: tuple[SurfaceSemanticAssignment, ...] = (),
    repaired_mesh: RepairedRawMesh | None = None,
    repaired_diagnostic: RepairedRawMeshDiagnosticResult | None = None,
) -> SemanticGeometryConversionRequest:
    profile = profile or SemanticGeometryConversionProfile()
    if (repaired_mesh is None) != (repaired_diagnostic is None):
        raise SemanticGeometryConversionError(
            'repaired_mesh and repaired_diagnostic must be supplied together'
        )
    repair_lineage: RawMeshRepairLineageRef | None = None
    if repaired_mesh is None:
        diagnostic = diagnose_raw_visual_mesh(mesh, profile=profile.diagnostic_profile)
        input_diagnostic_id = diagnostic.diagnostic_id
        input_diagnostic_hash = diagnostic.semantic_hash()
    else:
        assert repaired_diagnostic is not None
        _validate_repair_conversion_inputs(mesh, repaired_mesh, repaired_diagnostic)
        if repaired_diagnostic.profile != profile.diagnostic_profile:
            raise SemanticGeometryConversionError(
                'repaired diagnostic profile must match conversion diagnostic profile'
            )
        recomputed = diagnose_repaired_raw_mesh(
            mesh,
            repaired_mesh,
            profile=repaired_diagnostic.profile,
        )
        if recomputed != repaired_diagnostic:
            raise SemanticGeometryConversionError(
                'repaired diagnostic does not match exact repaired mesh'
            )
        input_diagnostic_id = repaired_diagnostic.diagnostic_id
        input_diagnostic_hash = repaired_diagnostic.semantic_hash()
        repair_lineage = make_raw_mesh_repair_lineage_ref(
            repaired_mesh,
            repaired_diagnostic,
        )

    payload = {
        'input_raw_mesh_id': mesh.mesh_id,
        'input_raw_mesh_semantic_hash': mesh.semantic_hash(),
        'input_asset_sha256': mesh.provenance.original_asset_sha256,
        'input_diagnostic_id': input_diagnostic_id,
        'input_diagnostic_semantic_hash': input_diagnostic_hash,
        'source_scene_revision_id': source_scene_revision_id,
        'source_to_scene_transform': source_to_scene_transform.model_dump(mode='json'),
        'profile': profile.model_dump(mode='json'),
        'profile_semantic_hash': profile.semantic_hash(),
        'repairs': [repair.model_dump(mode='json') for repair in repairs],
        'surface_assignments': [assignment.model_dump(mode='json') for assignment in surface_assignments],
    }
    if repair_lineage is not None:
        payload['raw_mesh_repair_lineage'] = repair_lineage.model_dump(mode='json')
    request_id = f"semantic-geometry-request:{_semantic_hash(payload)}"
    return SemanticGeometryConversionRequest(request_id=request_id, **payload)


def convert_raw_visual_mesh_to_semantic_geometry(
    mesh: RawVisualMesh,
    request: SemanticGeometryConversionRequest,
    *,
    repaired_mesh: RepairedRawMesh | None = None,
    repaired_diagnostic: RepairedRawMeshDiagnosticResult | None = None,
) -> SemanticAcousticGeometry:
    _validate_request_inputs(
        mesh,
        request,
        repaired_mesh=repaired_mesh,
        repaired_diagnostic=repaired_diagnostic,
    )
    if request.raw_mesh_repair_lineage is None:
        source_vertices = mesh.vertices
        source_triangles = mesh.triangles
        source_triangle_ids = raw_triangle_ids(mesh)
    else:
        assert repaired_mesh is not None
        source_vertices = repaired_mesh.vertices
        source_triangles = repaired_mesh.triangles
        source_triangle_ids = repaired_triangle_ids(repaired_mesh)

    vertices = [
        _transform_source_vertex(vertex, request.source_to_scene_transform)
        for vertex in source_vertices
    ]
    triangles = [
        SemanticTriangle(
            triangle_id=source_triangle_ids[index],
            a=triangle.a,
            b=triangle.b,
            c=triangle.c,
            source_triangle_id=source_triangle_ids[index],
            source_primitive=triangle.source_primitive,
        )
        for index, triangle in enumerate(source_triangles)
    ]

    root_hash = _topology_hash(tuple(vertices), tuple(triangles))
    current_hash = root_hash
    lineage: list[RepairLineageStep] = []
    for action in request.repairs:
        before_hash = current_hash
        operation_id = repair_operation_id(action, before_hash)
        vertices, triangles = _apply_repair(vertices, triangles, action, operation_id)
        current_hash = _topology_hash(tuple(vertices), tuple(triangles))
        lineage.append(
            RepairLineageStep(
                operation_id=operation_id,
                action=action,
                input_geometry_hash=before_hash,
                output_geometry_hash=current_hash,
            )
        )

    _validate_triangle_indices(vertices, triangles)
    derived_hash = _topology_hash(tuple(vertices), tuple(triangles))
    post_diagnostic = _diagnose_derived_geometry(
        mesh,
        vertices,
        triangles,
        profile=request.profile.diagnostic_profile,
    )
    surfaces = _build_surfaces(mesh.mesh_id, triangles, request.surface_assignments)

    geometry_failures = tuple(
        finding.code for finding in post_diagnostic.findings if finding.state != 'pass'
    )
    semantic_unknowns = tuple(
        surface.surface_id for surface in surfaces if surface.semantic_class == 'unknown'
    )
    unresolved = tuple(
        [f'geometry:{code}' for code in geometry_failures]
        + [f'surface_semantics:{surface_id}' for surface_id in semantic_unknowns]
    )
    if geometry_failures:
        readiness: GeometryCompilerReadiness = 'blocked_by_geometry'
    elif semantic_unknowns:
        readiness = 'blocked_by_surface_semantics'
    else:
        readiness = 'ready_for_r120_geometry_compiler_contract'

    suggestions = _guided_repair_suggestions(post_diagnostic.findings)
    core = {
        'input_raw_mesh_id': mesh.mesh_id,
        'input_raw_mesh_semantic_hash': mesh.semantic_hash(),
        'input_asset_sha256': mesh.provenance.original_asset_sha256,
        'input_diagnostic_id': request.input_diagnostic_id,
        'input_diagnostic_semantic_hash': request.input_diagnostic_semantic_hash,
        'source_scene_revision_id': request.source_scene_revision_id,
        'source_to_scene_transform': request.source_to_scene_transform,
        'conversion_request': request,
        'conversion_request_id': request.request_id,
        'conversion_profile_semantic_hash': request.profile_semantic_hash,
        'lineage_root_geometry_hash': root_hash,
        'repair_lineage': tuple(lineage),
        'derived_geometry_hash': derived_hash,
        'vertices': tuple(vertices),
        'triangles': tuple(triangles),
        'surfaces': tuple(surfaces),
        'conversion_findings': post_diagnostic.findings,
        'guided_repair_suggestions': suggestions,
        'unresolved_conditions': unresolved,
        'geometry_compiler_readiness': readiness,
        'solver_ready': False,
        'material_assignment_status': 'not_part_of_this_authority',
        'acoustic_regions_status': 'not_inferred',
        'portals_status': 'not_inferred',
        'boundary_terminations_status': 'not_inferred',
    }
    provisional = SemanticAcousticGeometry.model_construct(
        geometry_id=f"semantic-acoustic-geometry:{'0' * 64}",
        semantic_hash_sha256='0' * 64,
        **core,
    )
    identity_payload = provisional.model_dump(
        mode='json',
        exclude={'geometry_id', 'semantic_hash_sha256'},
    )
    _remove_absent_raw_mesh_repair_lineage(identity_payload)
    semantic_hash = _semantic_hash(identity_payload)
    return SemanticAcousticGeometry(
        geometry_id=f'semantic-acoustic-geometry:{semantic_hash}',
        semantic_hash_sha256=semantic_hash,
        **core,
    )


def serialize_semantic_acoustic_geometry(geometry: SemanticAcousticGeometry) -> str:
    payload = geometry.model_dump(mode='json')
    _remove_absent_raw_mesh_repair_lineage(payload)
    return _canonical_json(payload)


def deserialize_semantic_acoustic_geometry(payload: str) -> SemanticAcousticGeometry:
    return SemanticAcousticGeometry.model_validate(json.loads(payload))


def _transform_source_vertex(
    vertex: RawMeshVertex,
    transform: SemanticCoordinateTransform,
) -> SemanticVertex:
    matrix = transform.matrix_source_to_scene_m
    source = (vertex.x, vertex.y, vertex.z, 1.0)
    values = tuple(
        sum(float(matrix[row][column]) * source[column] for column in range(4))
        for row in range(3)
    )
    return SemanticVertex(x_m=values[0], y_m=values[1], z_m=values[2])


def _raw_triangle_id(mesh: RawVisualMesh, index: int, triangle: RawMeshTriangle) -> str:
    identity = _semantic_hash(
        {
            'raw_mesh_id': mesh.mesh_id,
            'raw_mesh_semantic_hash': mesh.semantic_hash(),
            'triangle_index': index,
            'a': triangle.a,
            'b': triangle.b,
            'c': triangle.c,
            'source_primitive': triangle.source_primitive,
        }
    )
    return f'raw-triangle:{identity}'


def _validate_request_inputs(
    mesh: RawVisualMesh,
    request: SemanticGeometryConversionRequest,
    *,
    repaired_mesh: RepairedRawMesh | None,
    repaired_diagnostic: RepairedRawMeshDiagnosticResult | None,
) -> None:
    if request.input_raw_mesh_id != mesh.mesh_id:
        raise SemanticGeometryConversionError('conversion request raw mesh id mismatch')
    if request.input_raw_mesh_semantic_hash != mesh.semantic_hash():
        raise SemanticGeometryConversionError('conversion request raw mesh semantic hash mismatch')
    if request.input_asset_sha256 != mesh.provenance.original_asset_sha256:
        raise SemanticGeometryConversionError('conversion request original asset hash mismatch')

    lineage = request.raw_mesh_repair_lineage
    if lineage is None:
        if repaired_mesh is not None or repaired_diagnostic is not None:
            raise SemanticGeometryConversionError(
                'conversion request has no repaired-mesh lineage'
            )
        diagnostic = diagnose_raw_visual_mesh(mesh, profile=request.profile.diagnostic_profile)
        if request.input_diagnostic_id != diagnostic.diagnostic_id:
            raise SemanticGeometryConversionError('conversion request input diagnostic id mismatch')
        if request.input_diagnostic_semantic_hash != diagnostic.semantic_hash():
            raise SemanticGeometryConversionError('conversion request input diagnostic hash mismatch')
        return

    if repaired_mesh is None or repaired_diagnostic is None:
        raise SemanticGeometryConversionError(
            'conversion request requires exact repaired mesh and diagnostic'
        )
    _validate_repair_conversion_inputs(mesh, repaired_mesh, repaired_diagnostic)
    if repaired_diagnostic.profile != request.profile.diagnostic_profile:
        raise SemanticGeometryConversionError(
            'conversion request repaired diagnostic profile mismatch'
        )
    if lineage != make_raw_mesh_repair_lineage_ref(repaired_mesh, repaired_diagnostic):
        raise SemanticGeometryConversionError('conversion request raw-mesh repair lineage mismatch')
    if request.input_diagnostic_id != repaired_diagnostic.diagnostic_id:
        raise SemanticGeometryConversionError('conversion request repaired diagnostic id mismatch')
    if request.input_diagnostic_semantic_hash != repaired_diagnostic.semantic_hash():
        raise SemanticGeometryConversionError('conversion request repaired diagnostic hash mismatch')
    recomputed = diagnose_repaired_raw_mesh(
        mesh,
        repaired_mesh,
        profile=repaired_diagnostic.profile,
    )
    if recomputed != repaired_diagnostic:
        raise SemanticGeometryConversionError(
            'conversion request repaired diagnostic does not recompute exactly'
        )


def _validate_repair_conversion_inputs(
    mesh: RawVisualMesh,
    repaired_mesh: RepairedRawMesh,
    repaired_diagnostic: RepairedRawMeshDiagnosticResult,
) -> None:
    if repaired_mesh.source_raw_mesh_id != mesh.mesh_id:
        raise SemanticGeometryConversionError('repaired mesh source raw mesh id mismatch')
    if repaired_mesh.source_raw_mesh_semantic_hash != mesh.semantic_hash():
        raise SemanticGeometryConversionError('repaired mesh source raw mesh hash mismatch')
    if repaired_mesh.original_asset_sha256 != mesh.provenance.original_asset_sha256:
        raise SemanticGeometryConversionError('repaired mesh original asset hash mismatch')
    if repaired_diagnostic.repaired_mesh_id != repaired_mesh.repaired_mesh_id:
        raise SemanticGeometryConversionError('repaired diagnostic repaired mesh id mismatch')
    if repaired_diagnostic.repaired_mesh_semantic_hash != repaired_mesh.semantic_hash():
        raise SemanticGeometryConversionError('repaired diagnostic repaired mesh hash mismatch')
    if repaired_diagnostic.source_raw_mesh_id != mesh.mesh_id:
        raise SemanticGeometryConversionError('repaired diagnostic source raw mesh id mismatch')
    if repaired_diagnostic.source_raw_mesh_semantic_hash != mesh.semantic_hash():
        raise SemanticGeometryConversionError('repaired diagnostic source raw mesh hash mismatch')


def _apply_repair(
    vertices: list[SemanticVertex],
    triangles: list[SemanticTriangle],
    action: RepairAction,
    operation_id: str,
) -> tuple[list[SemanticVertex], list[SemanticTriangle]]:
    next_vertices = list(vertices)
    next_triangles = list(triangles)

    if isinstance(action, RemoveTriangleRepair):
        index = _triangle_index(next_triangles, action.triangle_id)
        next_triangles.pop(index)
    elif isinstance(action, FlipTriangleRepair):
        index = _triangle_index(next_triangles, action.triangle_id)
        triangle = next_triangles[index]
        next_triangles[index] = triangle.model_copy(
            update={
                'b': triangle.c,
                'c': triangle.b,
                'repair_operation_ids': triangle.repair_operation_ids + (operation_id,),
            }
        )
    elif isinstance(action, AddTriangleRepair):
        if max(action.a, action.b, action.c) >= len(next_vertices):
            raise SemanticGeometryConversionError('added triangle references an unknown vertex')
        triangle_id = added_triangle_id(action, operation_id)
        if any(item.triangle_id == triangle_id for item in next_triangles):
            raise SemanticGeometryConversionError(f'added triangle already exists: {triangle_id}')
        next_triangles.append(
            SemanticTriangle(
                triangle_id=triangle_id,
                a=action.a,
                b=action.b,
                c=action.c,
                source_triangle_id=None,
                source_primitive=f'guided-repair:{action.repair_label}',
                repair_operation_ids=(operation_id,),
            )
        )
    elif isinstance(action, SetVertexRepair):
        if action.vertex_index >= len(next_vertices):
            raise SemanticGeometryConversionError('set_vertex references an unknown vertex')
        if next_vertices[action.vertex_index] != action.expected_before:
            raise SemanticGeometryConversionError(
                f'set_vertex expected_before mismatch at vertex {action.vertex_index}'
            )
        next_vertices[action.vertex_index] = action.after
        affected: list[SemanticTriangle] = []
        for triangle in next_triangles:
            if action.vertex_index in (triangle.a, triangle.b, triangle.c):
                affected.append(
                    triangle.model_copy(
                        update={
                            'repair_operation_ids': triangle.repair_operation_ids + (operation_id,)
                        }
                    )
                )
            else:
                affected.append(triangle)
        next_triangles = affected
    else:  # pragma: no cover - closed union is defensive
        raise SemanticGeometryConversionError(f'unsupported repair action: {action!r}')

    _validate_triangle_indices(next_vertices, next_triangles)
    return next_vertices, next_triangles


def _triangle_index(triangles: list[SemanticTriangle], triangle_id: str) -> int:
    matches = [index for index, triangle in enumerate(triangles) if triangle.triangle_id == triangle_id]
    if not matches:
        raise SemanticGeometryConversionError(f'unknown repair triangle_id: {triangle_id}')
    if len(matches) != 1:
        raise SemanticGeometryConversionError(f'ambiguous repair triangle_id: {triangle_id}')
    return matches[0]


def _validate_triangle_indices(
    vertices: list[SemanticVertex],
    triangles: list[SemanticTriangle],
) -> None:
    vertex_count = len(vertices)
    if not triangles:
        raise SemanticGeometryConversionError('semantic geometry must retain at least one triangle')
    ids = [triangle.triangle_id for triangle in triangles]
    if len(ids) != len(set(ids)):
        raise SemanticGeometryConversionError('semantic triangle ids must be unique')
    for triangle in triangles:
        if max(triangle.a, triangle.b, triangle.c) >= vertex_count:
            raise SemanticGeometryConversionError('semantic triangle references an unknown vertex')


def _topology_hash(
    vertices: tuple[SemanticVertex, ...],
    triangles: tuple[SemanticTriangle, ...],
) -> str:
    return _semantic_hash(
        {
            'vertices': [vertex.model_dump(mode='json') for vertex in vertices],
            'triangles': [triangle.model_dump(mode='json') for triangle in triangles],
        }
    )


def _diagnose_derived_geometry(
    source_mesh: RawVisualMesh,
    vertices: list[SemanticVertex],
    triangles: list[SemanticTriangle],
    *,
    profile: RawMeshDiagnosticProfile,
):
    # Reuse PR #195's diagnostic implementation over an in-memory derived view.
    # The source RawVisualMesh itself is frozen and remains byte-for-byte unchanged.
    diagnostic_triangles = tuple(
        RawMeshTriangle(
            a=triangle.a,
            b=triangle.b,
            c=triangle.c,
            source_primitive=triangle.source_primitive or triangle.triangle_id,
        )
        for triangle in triangles
    )
    diagnostic_vertices = tuple(
        RawMeshVertex(x=vertex.x_m, y=vertex.y_m, z=vertex.z_m)
        for vertex in vertices
    )
    derived_view = source_mesh.model_copy(
        update={'vertices': diagnostic_vertices, 'triangles': diagnostic_triangles}
    )
    return diagnose_raw_visual_mesh(derived_view, profile=profile)


def _build_surfaces(
    input_raw_mesh_id: str,
    triangles: list[SemanticTriangle],
    assignments: tuple[SurfaceSemanticAssignment, ...],
) -> tuple[SemanticSurface, ...]:
    triangle_ids = {triangle.triangle_id for triangle in triangles}
    assigned: set[str] = set()
    surfaces: list[SemanticSurface] = []
    keys: set[str] = set()
    for assignment in assignments:
        if assignment.surface_key == '__unassigned__':
            raise SemanticGeometryConversionError('__unassigned__ is reserved')
        if assignment.surface_key in keys:
            raise SemanticGeometryConversionError(
                f'duplicate semantic surface_key: {assignment.surface_key}'
            )
        keys.add(assignment.surface_key)
        members = set(assignment.triangle_ids)
        missing = members - triangle_ids
        if missing:
            raise SemanticGeometryConversionError(
                f'surface assignment references unknown triangles: {sorted(missing)}'
            )
        overlap = assigned.intersection(members)
        if overlap:
            raise SemanticGeometryConversionError(
                f'triangle assigned to multiple surfaces: {sorted(overlap)}'
            )
        assigned.update(members)
        canonical_members = tuple(sorted(assignment.triangle_ids))
        surfaces.append(
            SemanticSurface(
                surface_id=_surface_id(
                    input_raw_mesh_id,
                    assignment.surface_key,
                ),
                surface_key=assignment.surface_key,
                semantic_class=assignment.semantic_class,
                triangle_ids=canonical_members,
                assignment_provenance='explicit',
            )
        )

    unassigned = tuple(
        triangle.triangle_id for triangle in triangles if triangle.triangle_id not in assigned
    )
    if unassigned:
        surfaces.append(
            SemanticSurface(
                surface_id=_surface_id(
                    input_raw_mesh_id,
                    '__unassigned__',
                ),
                surface_key='__unassigned__',
                semantic_class='unknown',
                triangle_ids=unassigned,
                assignment_provenance='unassigned',
            )
        )
    return tuple(surfaces)


def _surface_id(input_raw_mesh_id: str, surface_key: str) -> str:
    identity = _semantic_hash(
        {
            'input_raw_mesh_id': input_raw_mesh_id,
            'surface_key': surface_key,
        }
    )
    return f'semantic-surface:{identity}'


def _guided_repair_suggestions(
    findings: tuple[RawMeshDiagnosticFinding, ...],
) -> tuple[GuidedRepairSuggestion, ...]:
    mapping = {
        'open_boundary': (
            ('add_triangle', 'set_vertex'),
            'Close only a user-identified acoustic boundary; do not infer a portal or termination.',
        ),
        'non_manifold_edge': (
            ('remove_triangle', 'set_vertex'),
            'Resolve the explicitly identified topology; do not auto-delete source geometry.',
        ),
        'duplicate_face': (
            ('remove_triangle',),
            'Remove only an explicitly selected duplicate triangle in the derived geometry.',
        ),
        'overlapping_face': (
            ('remove_triangle', 'set_vertex'),
            'Resolve overlap with an explicit derived-geometry edit.',
        ),
        'inverted_normal': (
            ('flip_triangle',),
            'Flip only explicitly selected triangles after confirming intended outward orientation.',
        ),
        'sliver_face': (
            ('remove_triangle', 'set_vertex'),
            'Repair the selected sliver explicitly; source mesh bytes remain immutable.',
        ),
        'tiny_feature': (
            ('remove_triangle', 'set_vertex'),
            'Remove or reshape only an explicitly selected sub-resolution feature.',
        ),
        'watertightness': (
            ('add_triangle', 'remove_triangle', 'set_vertex'),
            'Resolve topology explicitly; watertightness alone does not assign acoustic semantics.',
        ),
    }
    suggestions: list[GuidedRepairSuggestion] = []
    for finding in findings:
        if finding.state == 'pass':
            continue
        action_kinds, guidance = mapping[finding.code]
        suggestions.append(
            GuidedRepairSuggestion(
                finding_code=finding.code,
                suggested_action_kinds=action_kinds,
                guidance=guidance,
            )
        )
    return tuple(suggestions)
