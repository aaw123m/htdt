from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import floor, isfinite, sqrt
from pathlib import Path
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_schema import ensure_native_schema
from .content_blobs import (
    ensure_content_blob_store,
    read_content_blob,
    store_content_blob,
)
from .raw_mesh import (
    AcousticVolumeReadiness,
    RawMeshDiagnosticFinding,
    RawMeshDiagnosticProfile,
    RawMeshDiagnosticResult,
    RawMeshTriangle,
    RawMeshVertex,
    RawVisualMesh,
    diagnose_raw_visual_mesh,
    rehydrate_raw_visual_mesh,
    serialize_raw_visual_mesh_reference,
)


RAW_MESH_REPAIR_ALGORITHM_ID = 'htdt.raw_mesh_repair'
RAW_MESH_REPAIR_ALGORITHM_VERSION = '1'
RAW_MESH_REPAIR_OPERATION_VERSION = '1'

RepairRequestedBy = Literal['explicit_user_selected', 'workflow_selected']
RepairSupportState = Literal['supported', 'unsupported']
RepairExecutionState = Literal['applied', 'no_change', 'blocked', 'unsupported']


class RawMeshRepairError(ValueError):
    pass


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


class ExactDuplicateVertexConsolidation(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal['exact_duplicate_vertex_consolidation'] = 'exact_duplicate_vertex_consolidation'
    operation_version: Literal['1'] = RAW_MESH_REPAIR_OPERATION_VERSION
    support_state: Literal['supported'] = 'supported'


class ToleranceVertexWeld(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal['tolerance_vertex_weld'] = 'tolerance_vertex_weld'
    operation_version: Literal['1'] = RAW_MESH_REPAIR_OPERATION_VERSION
    support_state: Literal['supported'] = 'supported'
    tolerance_source_units: float = Field(gt=0.0)

    @field_validator('tolerance_source_units')
    @classmethod
    def finite_tolerance(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value):
            raise ValueError('vertex weld tolerance must be finite')
        return value


class RemoveUnreferencedVertices(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal['remove_unreferenced_vertices'] = 'remove_unreferenced_vertices'
    operation_version: Literal['1'] = RAW_MESH_REPAIR_OPERATION_VERSION
    support_state: Literal['supported'] = 'supported'


class RemoveExactDuplicateFaces(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal['remove_exact_duplicate_faces'] = 'remove_exact_duplicate_faces'
    operation_version: Literal['1'] = RAW_MESH_REPAIR_OPERATION_VERSION
    support_state: Literal['supported'] = 'supported'


class CorrectConsistentWinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal['correct_consistent_winding'] = 'correct_consistent_winding'
    operation_version: Literal['1'] = RAW_MESH_REPAIR_OPERATION_VERSION
    support_state: Literal['supported'] = 'supported'


class RemoveDegenerateFaces(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal['remove_degenerate_faces'] = 'remove_degenerate_faces'
    operation_version: Literal['1'] = RAW_MESH_REPAIR_OPERATION_VERSION
    support_state: Literal['supported'] = 'supported'
    area_tolerance_source_units_squared: float = Field(ge=0.0)

    @field_validator('area_tolerance_source_units_squared')
    @classmethod
    def finite_tolerance(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value):
            raise ValueError('degenerate-face area tolerance must be finite')
        return value


UnsupportedRepairKind = Literal[
    'fill_hole',
    'non_manifold_surgery',
    'self_intersection_remesh',
    'boolean_reconstruction',
    'point_cloud_surface_reconstruction',
    'large_gap_closure',
    'overlapping_surface_resolution',
    'acoustic_room_inference',
]


class UnsupportedRawMeshRepairOperation(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: UnsupportedRepairKind
    operation_version: Literal['1'] = RAW_MESH_REPAIR_OPERATION_VERSION
    support_state: Literal['unsupported'] = 'unsupported'
    reason: str = Field(min_length=1)


RawMeshRepairOperation = (
    ExactDuplicateVertexConsolidation
    | ToleranceVertexWeld
    | RemoveUnreferencedVertices
    | RemoveExactDuplicateFaces
    | CorrectConsistentWinding
    | RemoveDegenerateFaces
    | UnsupportedRawMeshRepairOperation
)


class RawMeshRepairPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(pattern=r'^raw-mesh-repair-plan:[0-9a-f]{64}$')
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_raw_mesh_id: str = Field(pattern=r'^raw-mesh:[0-9a-f]{64}$')
    source_raw_mesh_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_diagnostic_id: str = Field(pattern=r'^raw-mesh-diagnostic:[0-9a-f]{64}$')
    source_diagnostic_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    algorithm_id: Literal['htdt.raw_mesh_repair'] = RAW_MESH_REPAIR_ALGORITHM_ID
    algorithm_version: Literal['1'] = RAW_MESH_REPAIR_ALGORITHM_VERSION
    requested_by: RepairRequestedBy
    request_reason: str = Field(min_length=1)
    operations: tuple[RawMeshRepairOperation, ...]
    unsupported_operation_kinds: tuple[str, ...] = ()

    @model_validator(mode='after')
    def validate_identity(self) -> 'RawMeshRepairPlan':
        expected_unsupported = tuple(
            operation.kind
            for operation in self.operations
            if operation.support_state == 'unsupported'
        )
        if self.unsupported_operation_kinds != expected_unsupported:
            raise ValueError('repair plan unsupported operation state mismatch')
        core = self.model_dump(
            mode='json',
            exclude={'plan_id', 'semantic_hash_sha256'},
        )
        expected_hash = _semantic_hash(core)
        if self.semantic_hash_sha256 != expected_hash:
            raise ValueError('repair plan semantic hash mismatch')
        if self.plan_id != f'raw-mesh-repair-plan:{expected_hash}':
            raise ValueError('repair plan id does not match authority inputs')
        return self

    def semantic_hash(self) -> str:
        return self.semantic_hash_sha256


class RawMeshRepairOperationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_result_id: str = Field(pattern=r'^raw-mesh-repair-operation-result:[0-9a-f]{64}$')
    operation_index: int = Field(ge=0)
    operation: RawMeshRepairOperation
    execution_state: RepairExecutionState
    vertex_count_before: int = Field(ge=0)
    vertex_count_after: int = Field(ge=0)
    triangle_count_before: int = Field(ge=0)
    triangle_count_after: int = Field(ge=0)
    moved_vertex_count: int = Field(ge=0)
    maximum_displacement_source_units: float = Field(ge=0.0)
    topology_change_count: int = Field(ge=0)
    detail: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'RawMeshRepairOperationResult':
        core = self.model_dump(mode='json', exclude={'operation_result_id'})
        expected = _semantic_hash(core)
        if self.operation_result_id != f'raw-mesh-repair-operation-result:{expected}':
            raise ValueError('repair operation result id mismatch')
        return self


class GeometryDisplacementMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    moved_vertex_events: int = Field(ge=0)
    maximum_displacement_source_units: float = Field(ge=0.0)
    topology_change_count: int = Field(ge=0)


class RepairedRawMesh(BaseModel):
    model_config = ConfigDict(frozen=True)

    repaired_mesh_id: str = Field(pattern=r'^repaired-raw-mesh:[0-9a-f]{64}$')
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_raw_mesh_id: str = Field(pattern=r'^raw-mesh:[0-9a-f]{64}$')
    source_raw_mesh_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_diagnostic_id: str = Field(pattern=r'^raw-mesh-diagnostic:[0-9a-f]{64}$')
    source_diagnostic_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    repair_plan_id: str = Field(pattern=r'^raw-mesh-repair-plan:[0-9a-f]{64}$')
    repair_plan_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    original_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    vertices: tuple[RawMeshVertex, ...]
    triangles: tuple[RawMeshTriangle, ...]
    operation_results: tuple[RawMeshRepairOperationResult, ...]
    repair_lineage: tuple[str, ...]
    vertex_count_before: int = Field(ge=0)
    vertex_count_after: int = Field(ge=0)
    triangle_count_before: int = Field(ge=0)
    triangle_count_after: int = Field(ge=0)
    displacement_metadata: GeometryDisplacementMetadata
    unsupported_unresolved_findings: tuple[str, ...]

    @model_validator(mode='after')
    def validate_identity(self) -> 'RepairedRawMesh':
        if self.vertex_count_after != len(self.vertices):
            raise ValueError('repaired mesh vertex_count_after mismatch')
        if self.triangle_count_after != len(self.triangles):
            raise ValueError('repaired mesh triangle_count_after mismatch')
        expected_lineage = tuple(result.operation_result_id for result in self.operation_results)
        if self.repair_lineage != expected_lineage:
            raise ValueError('repaired mesh repair lineage mismatch')
        vertex_count = len(self.vertices)
        for triangle in self.triangles:
            if max(triangle.a, triangle.b, triangle.c) >= vertex_count:
                raise ValueError('repaired mesh triangle references an unknown vertex')
        core = self.model_dump(
            mode='json',
            exclude={'repaired_mesh_id', 'semantic_hash_sha256'},
        )
        expected_hash = _semantic_hash(core)
        if self.semantic_hash_sha256 != expected_hash:
            raise ValueError('repaired mesh semantic hash mismatch')
        if self.repaired_mesh_id != f'repaired-raw-mesh:{expected_hash}':
            raise ValueError('repaired mesh id does not match authority inputs')
        return self

    def semantic_hash(self) -> str:
        return self.semantic_hash_sha256


class RepairedRawMeshDiagnosticResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    diagnostic_id: str = Field(pattern=r'^repaired-raw-mesh-diagnostic:[0-9a-f]{64}$')
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    repaired_mesh_id: str = Field(pattern=r'^repaired-raw-mesh:[0-9a-f]{64}$')
    repaired_mesh_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_raw_mesh_id: str = Field(pattern=r'^raw-mesh:[0-9a-f]{64}$')
    source_raw_mesh_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    profile: RawMeshDiagnosticProfile
    profile_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    findings: tuple[RawMeshDiagnosticFinding, ...]
    acoustic_volume_readiness: AcousticVolumeReadiness
    solver_ready: Literal[False] = False
    semantic_conversion_required: Literal[True] = True

    @model_validator(mode='after')
    def validate_identity(self) -> 'RepairedRawMeshDiagnosticResult':
        if self.profile_semantic_hash != self.profile.semantic_hash():
            raise ValueError('post-repair diagnostic profile hash mismatch')
        identity_hash = _semantic_hash(
            {
                'repaired_mesh_id': self.repaired_mesh_id,
                'repaired_mesh_semantic_hash': self.repaired_mesh_semantic_hash,
                'profile_semantic_hash': self.profile_semantic_hash,
            }
        )
        if self.diagnostic_id != f'repaired-raw-mesh-diagnostic:{identity_hash}':
            raise ValueError('post-repair diagnostic id mismatch')
        core = self.model_dump(mode='json', exclude={'semantic_hash_sha256'})
        expected_hash = _semantic_hash(core)
        if self.semantic_hash_sha256 != expected_hash:
            raise ValueError('post-repair diagnostic semantic hash mismatch')
        return self

    def semantic_hash(self) -> str:
        return self.semantic_hash_sha256


class RawMeshRepairLineageRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_raw_mesh_id: str = Field(pattern=r'^raw-mesh:[0-9a-f]{64}$')
    source_raw_mesh_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_diagnostic_id: str = Field(pattern=r'^raw-mesh-diagnostic:[0-9a-f]{64}$')
    source_diagnostic_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    repair_plan_id: str = Field(pattern=r'^raw-mesh-repair-plan:[0-9a-f]{64}$')
    repair_plan_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    repaired_mesh_id: str = Field(pattern=r'^repaired-raw-mesh:[0-9a-f]{64}$')
    repaired_mesh_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    post_repair_diagnostic_id: str = Field(
        pattern=r'^repaired-raw-mesh-diagnostic:[0-9a-f]{64}$'
    )
    post_repair_diagnostic_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


class RawMeshRepairBundle(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_raw_mesh: RawVisualMesh
    source_diagnostic: RawMeshDiagnosticResult
    repair_plan: RawMeshRepairPlan
    repaired_mesh: RepairedRawMesh
    post_repair_diagnostic: RepairedRawMeshDiagnosticResult

    @model_validator(mode='after')
    def validate_links(self) -> 'RawMeshRepairBundle':
        _validate_plan_inputs(self.source_raw_mesh, self.source_diagnostic, self.repair_plan)
        _validate_repaired_mesh_links(self.source_raw_mesh, self.repair_plan, self.repaired_mesh)
        _validate_post_diagnostic_links(
            self.source_raw_mesh,
            self.repaired_mesh,
            self.post_repair_diagnostic,
        )
        return self


def make_raw_mesh_repair_plan(
    mesh: RawVisualMesh,
    diagnostic: RawMeshDiagnosticResult,
    *,
    operations: tuple[RawMeshRepairOperation, ...],
    requested_by: RepairRequestedBy,
    request_reason: str,
) -> RawMeshRepairPlan:
    _validate_source_diagnostic(mesh, diagnostic)
    unsupported = tuple(
        operation.kind
        for operation in operations
        if operation.support_state == 'unsupported'
    )
    core = {
        'source_raw_mesh_id': mesh.mesh_id,
        'source_raw_mesh_semantic_hash': mesh.semantic_hash(),
        'source_diagnostic_id': diagnostic.diagnostic_id,
        'source_diagnostic_semantic_hash': diagnostic.semantic_hash(),
        'algorithm_id': RAW_MESH_REPAIR_ALGORITHM_ID,
        'algorithm_version': RAW_MESH_REPAIR_ALGORITHM_VERSION,
        'requested_by': requested_by,
        'request_reason': request_reason,
        'operations': [operation.model_dump(mode='json') for operation in operations],
        'unsupported_operation_kinds': unsupported,
    }
    semantic_hash = _semantic_hash(core)
    return RawMeshRepairPlan(
        plan_id=f'raw-mesh-repair-plan:{semantic_hash}',
        semantic_hash_sha256=semantic_hash,
        **core,
    )


_InternalTriangle = tuple[int, int, int, str]


def apply_raw_mesh_repair(
    mesh: RawVisualMesh,
    diagnostic: RawMeshDiagnosticResult,
    plan: RawMeshRepairPlan,
) -> RepairedRawMesh:
    _validate_plan_inputs(mesh, diagnostic, plan)
    vertices = list(mesh.vertices)
    triangles: list[_InternalTriangle] = [
        (triangle.a, triangle.b, triangle.c, triangle.source_primitive)
        for triangle in mesh.triangles
    ]
    results: list[RawMeshRepairOperationResult] = []
    unresolved: list[str] = []

    for operation_index, operation in enumerate(plan.operations):
        before_vertices = len(vertices)
        before_triangles = len(triangles)
        moved_vertex_count = 0
        maximum_displacement = 0.0
        topology_changes = 0
        detail = ''
        execution_state: RepairExecutionState

        if operation.support_state == 'unsupported':
            execution_state = 'unsupported'
            detail = f'{operation.kind} is deliberately unsupported by bounded repair v1'
            unresolved.append(f'unsupported_operation:{operation.kind}')
        elif isinstance(operation, ExactDuplicateVertexConsolidation):
            (
                vertices,
                triangles,
                moved_vertex_count,
                maximum_displacement,
                topology_changes,
            ) = _consolidate_exact_duplicate_vertices(vertices, triangles)
            execution_state = 'applied' if topology_changes else 'no_change'
            detail = 'consolidated vertices with exactly equal coordinates; first index is canonical'
        elif isinstance(operation, ToleranceVertexWeld):
            (
                vertices,
                triangles,
                moved_vertex_count,
                maximum_displacement,
                topology_changes,
            ) = _weld_vertices(vertices, triangles, operation.tolerance_source_units)
            execution_state = 'applied' if topology_changes else 'no_change'
            detail = (
                'deterministic nearest previously retained representative weld; '
                'ties use the earliest source vertex index; explicit tolerance '
                f'{operation.tolerance_source_units:g} source units'
            )
        elif isinstance(operation, RemoveUnreferencedVertices):
            vertices, triangles, removed = _remove_unreferenced_vertices(vertices, triangles)
            topology_changes = removed
            execution_state = 'applied' if removed else 'no_change'
            detail = f'removed {removed} unreferenced vertices'
        elif isinstance(operation, RemoveExactDuplicateFaces):
            triangles, removed = _remove_exact_duplicate_faces(vertices, triangles)
            topology_changes = removed
            execution_state = 'applied' if removed else 'no_change'
            detail = f'removed {removed} exact duplicate faces; first face is canonical'
        elif isinstance(operation, RemoveDegenerateFaces):
            candidate, removed = _remove_degenerate_faces(
                vertices,
                triangles,
                operation.area_tolerance_source_units_squared,
            )
            if removed and not candidate:
                execution_state = 'blocked'
                detail = 'degenerate-face removal would remove every triangle; operation left unapplied'
                unresolved.append('blocked_operation:remove_degenerate_faces:would_remove_all_faces')
            else:
                triangles = candidate
                topology_changes = removed
                execution_state = 'applied' if removed else 'no_change'
                detail = (
                    f'removed {removed} faces at or below explicit area tolerance '
                    f'{operation.area_tolerance_source_units_squared:g}'
                )
        elif isinstance(operation, CorrectConsistentWinding):
            (
                candidate,
                flipped,
                blocked_components,
                conflict_components,
            ) = _correct_consistent_winding(triangles)
            triangles = candidate
            topology_changes = flipped
            if blocked_components or conflict_components:
                execution_state = 'blocked' if not flipped else 'applied'
                if blocked_components:
                    unresolved.append(
                        f'blocked_operation:correct_consistent_winding:non_manifold_components={blocked_components}'
                    )
                if conflict_components:
                    unresolved.append(
                        f'blocked_operation:correct_consistent_winding:non_orientable_components={conflict_components}'
                    )
            else:
                execution_state = 'applied' if flipped else 'no_change'
            detail = (
                f'flipped {flipped} faces for relative winding consistency; '
                f'blocked_non_manifold_components={blocked_components}; '
                f'blocked_conflict_components={conflict_components}; '
                'global outward orientation was not inferred'
            )
        else:  # pragma: no cover - closed union is defensive
            raise RawMeshRepairError(f'unknown repair operation: {operation!r}')

        result = _make_operation_result(
            operation_index=operation_index,
            operation=operation,
            execution_state=execution_state,
            vertex_count_before=before_vertices,
            vertex_count_after=len(vertices),
            triangle_count_before=before_triangles,
            triangle_count_after=len(triangles),
            moved_vertex_count=moved_vertex_count,
            maximum_displacement_source_units=maximum_displacement,
            topology_change_count=topology_changes,
            detail=detail,
        )
        results.append(result)

    if not triangles:
        raise RawMeshRepairError('repair plan produced an empty mesh')
    if any(len({a, b, c}) != 3 for a, b, c, _ in triangles):
        raise RawMeshRepairError(
            'repair plan left degenerate triangle indices; explicitly request '
            'remove_degenerate_faces after any consolidating/weld operation that collapses faces'
        )

    repaired_triangles = tuple(
        RawMeshTriangle(a=a, b=b, c=c, source_primitive=source_primitive)
        for a, b, c, source_primitive in triangles
    )
    displacement = GeometryDisplacementMetadata(
        moved_vertex_events=sum(result.moved_vertex_count for result in results),
        maximum_displacement_source_units=max(
            (result.maximum_displacement_source_units for result in results),
            default=0.0,
        ),
        topology_change_count=sum(result.topology_change_count for result in results),
    )
    core = {
        'source_raw_mesh_id': mesh.mesh_id,
        'source_raw_mesh_semantic_hash': mesh.semantic_hash(),
        'source_diagnostic_id': diagnostic.diagnostic_id,
        'source_diagnostic_semantic_hash': diagnostic.semantic_hash(),
        'repair_plan_id': plan.plan_id,
        'repair_plan_semantic_hash': plan.semantic_hash(),
        'original_asset_sha256': mesh.provenance.original_asset_sha256,
        'vertices': tuple(vertices),
        'triangles': repaired_triangles,
        'operation_results': tuple(results),
        'repair_lineage': tuple(result.operation_result_id for result in results),
        'vertex_count_before': len(mesh.vertices),
        'vertex_count_after': len(vertices),
        'triangle_count_before': len(mesh.triangles),
        'triangle_count_after': len(repaired_triangles),
        'displacement_metadata': displacement,
        'unsupported_unresolved_findings': tuple(unresolved),
    }
    provisional = RepairedRawMesh.model_construct(
        repaired_mesh_id=f"repaired-raw-mesh:{'0' * 64}",
        semantic_hash_sha256='0' * 64,
        **core,
    )
    semantic_hash = _semantic_hash(
        provisional.model_dump(
            mode='json',
            exclude={'repaired_mesh_id', 'semantic_hash_sha256'},
        )
    )
    return RepairedRawMesh(
        repaired_mesh_id=f'repaired-raw-mesh:{semantic_hash}',
        semantic_hash_sha256=semantic_hash,
        **core,
    )


def diagnose_repaired_raw_mesh(
    source_mesh: RawVisualMesh,
    repaired_mesh: RepairedRawMesh,
    *,
    profile: RawMeshDiagnosticProfile | None = None,
) -> RepairedRawMeshDiagnosticResult:
    _validate_repaired_mesh_source(source_mesh, repaired_mesh)
    profile = profile or RawMeshDiagnosticProfile()
    derived_view = source_mesh.model_copy(
        update={
            'vertices': repaired_mesh.vertices,
            'triangles': repaired_mesh.triangles,
        }
    )
    underlying = diagnose_raw_visual_mesh(derived_view, profile=profile)
    profile_hash = profile.semantic_hash()
    identity_hash = _semantic_hash(
        {
            'repaired_mesh_id': repaired_mesh.repaired_mesh_id,
            'repaired_mesh_semantic_hash': repaired_mesh.semantic_hash(),
            'profile_semantic_hash': profile_hash,
        }
    )
    core = {
        'diagnostic_id': f'repaired-raw-mesh-diagnostic:{identity_hash}',
        'repaired_mesh_id': repaired_mesh.repaired_mesh_id,
        'repaired_mesh_semantic_hash': repaired_mesh.semantic_hash(),
        'source_raw_mesh_id': source_mesh.mesh_id,
        'source_raw_mesh_semantic_hash': source_mesh.semantic_hash(),
        'profile': profile,
        'profile_semantic_hash': profile_hash,
        'findings': underlying.findings,
        'acoustic_volume_readiness': underlying.acoustic_volume_readiness,
        'solver_ready': False,
        'semantic_conversion_required': True,
    }
    provisional = RepairedRawMeshDiagnosticResult.model_construct(
        semantic_hash_sha256='0' * 64,
        **core,
    )
    semantic_hash = _semantic_hash(
        provisional.model_dump(mode='json', exclude={'semantic_hash_sha256'})
    )
    return RepairedRawMeshDiagnosticResult(
        semantic_hash_sha256=semantic_hash,
        **core,
    )


def make_raw_mesh_repair_lineage_ref(
    repaired_mesh: RepairedRawMesh,
    post_repair_diagnostic: RepairedRawMeshDiagnosticResult,
) -> RawMeshRepairLineageRef:
    if post_repair_diagnostic.repaired_mesh_id != repaired_mesh.repaired_mesh_id:
        raise RawMeshRepairError('post-repair diagnostic repaired mesh id mismatch')
    if post_repair_diagnostic.repaired_mesh_semantic_hash != repaired_mesh.semantic_hash():
        raise RawMeshRepairError('post-repair diagnostic repaired mesh hash mismatch')
    return RawMeshRepairLineageRef(
        source_raw_mesh_id=repaired_mesh.source_raw_mesh_id,
        source_raw_mesh_semantic_hash=repaired_mesh.source_raw_mesh_semantic_hash,
        source_diagnostic_id=repaired_mesh.source_diagnostic_id,
        source_diagnostic_semantic_hash=repaired_mesh.source_diagnostic_semantic_hash,
        repair_plan_id=repaired_mesh.repair_plan_id,
        repair_plan_semantic_hash=repaired_mesh.repair_plan_semantic_hash,
        repaired_mesh_id=repaired_mesh.repaired_mesh_id,
        repaired_mesh_semantic_hash=repaired_mesh.semantic_hash(),
        post_repair_diagnostic_id=post_repair_diagnostic.diagnostic_id,
        post_repair_diagnostic_semantic_hash=post_repair_diagnostic.semantic_hash(),
    )


def repaired_triangle_ids(mesh: RepairedRawMesh) -> tuple[str, ...]:
    identifiers: list[str] = []
    for index, triangle in enumerate(mesh.triangles):
        identity = _semantic_hash(
            {
                'repaired_mesh_id': mesh.repaired_mesh_id,
                'triangle_index': index,
                'triangle': triangle.model_dump(mode='json'),
            }
        )
        identifiers.append(f'repaired-raw-triangle:{identity}')
    return tuple(identifiers)


def serialize_raw_mesh_repair_bundle(bundle: RawMeshRepairBundle) -> str:
    return _canonical_json(bundle.model_dump(mode='json'))


def deserialize_raw_mesh_repair_bundle(payload: str) -> RawMeshRepairBundle:
    return RawMeshRepairBundle.model_validate(json.loads(payload))


def _persisted_bundle_json(bundle: RawMeshRepairBundle) -> str:
    """Compact persisted form of a repair bundle.

    The embedded source RawVisualMesh is reduced to its content-addressed
    reference (mesh_id + provenance, which already carries
    original_asset_sha256); the canonical asset bytes live once in
    htdt_content_blobs rather than being embedded again as Base64 JSON.
    """

    data = bundle.model_dump(mode='json', exclude={'source_raw_mesh'})
    data['source_raw_mesh'] = serialize_raw_visual_mesh_reference(
        bundle.source_raw_mesh
    )
    return _canonical_json(data)


def _load_persisted_bundle(
    connection: sqlite3.Connection,
    payload_json: str,
) -> RawMeshRepairBundle:
    """Rebuild a persisted bundle from either storage representation.

    Legacy rows embed the complete source RawVisualMesh (including its
    original asset bytes as Base64). Compact rows carry only the mesh
    reference; the canonical asset bytes are fetched from the content blob
    store and the mesh is deterministically re-imported and re-verified.
    """

    data = json.loads(payload_json)
    source = data.get('source_raw_mesh') if isinstance(data, dict) else None
    if isinstance(source, dict) and 'original_asset_base64' not in source:
        provenance = source.get('provenance')
        asset_sha256 = (
            provenance.get('original_asset_sha256')
            if isinstance(provenance, dict)
            else None
        )
        asset = (
            read_content_blob(connection, asset_sha256)
            if isinstance(asset_sha256, str)
            else None
        )
        if asset is None:
            raise RawMeshRepairError(
                'canonical raw mesh asset is missing from the content blob store'
            )
        try:
            return RawMeshRepairBundle(
                source_raw_mesh=rehydrate_raw_visual_mesh(source, asset),
                source_diagnostic=RawMeshDiagnosticResult.model_validate(
                    data['source_diagnostic']
                ),
                repair_plan=RawMeshRepairPlan.model_validate(data['repair_plan']),
                repaired_mesh=RepairedRawMesh.model_validate(data['repaired_mesh']),
                post_repair_diagnostic=RepairedRawMeshDiagnosticResult.model_validate(
                    data['post_repair_diagnostic']
                ),
            )
        except ValueError as exc:
            raise RawMeshRepairError(
                f'persisted repair bundle is invalid: {exc}'
            ) from exc
    return deserialize_raw_mesh_repair_bundle(payload_json)


class RawMeshRepairRepository:
    """Append-only raw-mesh repair authority stored in the native HTDT SQLite database."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ensure_native_schema(self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def save(self, bundle: RawMeshRepairBundle) -> bool:
        _validate_bundle_against_recomputation(bundle)
        payload = _persisted_bundle_json(bundle)
        asset = bundle.source_raw_mesh.original_asset_bytes()
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            # Keep one canonical copy of the source asset bytes in the
            # content-addressed blob store; the bundle record references it
            # by the SHA-256 already carried in the mesh provenance.
            ensure_content_blob_store(connection)
            store_content_blob(
                connection,
                asset,
                expected_sha256=(
                    bundle.source_raw_mesh.provenance.original_asset_sha256
                ),
            )
            existing = connection.execute(
                'SELECT payload_json FROM cad_raw_mesh_repair_bundles WHERE repaired_mesh_id=?',
                (bundle.repaired_mesh.repaired_mesh_id,),
            ).fetchone()
            if existing is not None:
                if existing['payload_json'] != payload:
                    # Legacy rows embed the full source mesh; compare the
                    # rehydrated bundle semantically instead of bytes.
                    stored = _load_persisted_bundle(
                        connection,
                        existing['payload_json'],
                    )
                    if stored != bundle:
                        raise RawMeshRepairError(
                            'repaired mesh identity collision with different persisted payload'
                        )
                return False
            connection.execute(
                '''
                INSERT INTO cad_raw_mesh_repair_bundles(
                    repaired_mesh_id,
                    repaired_mesh_semantic_hash,
                    raw_mesh_id,
                    raw_mesh_semantic_hash,
                    repair_plan_id,
                    repair_plan_semantic_hash,
                    post_diagnostic_id,
                    post_diagnostic_semantic_hash,
                    payload_json,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    bundle.repaired_mesh.repaired_mesh_id,
                    bundle.repaired_mesh.semantic_hash(),
                    bundle.source_raw_mesh.mesh_id,
                    bundle.source_raw_mesh.semantic_hash(),
                    bundle.repair_plan.plan_id,
                    bundle.repair_plan.semantic_hash(),
                    bundle.post_repair_diagnostic.diagnostic_id,
                    bundle.post_repair_diagnostic.semantic_hash(),
                    payload,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return True

    def get(self, repaired_mesh_id: str) -> RawMeshRepairBundle | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_raw_mesh_repair_bundles WHERE repaired_mesh_id=?',
                (repaired_mesh_id,),
            ).fetchone()
            if row is None:
                return None
            bundle = _load_persisted_bundle(connection, row['payload_json'])
        if bundle.repaired_mesh.repaired_mesh_id != row['repaired_mesh_id']:
            raise RawMeshRepairError('persisted repaired mesh id mismatch')
        if bundle.repaired_mesh.semantic_hash() != row['repaired_mesh_semantic_hash']:
            raise RawMeshRepairError('persisted repaired mesh hash mismatch')
        if bundle.source_raw_mesh.mesh_id != row['raw_mesh_id']:
            raise RawMeshRepairError('persisted source raw mesh id mismatch')
        if bundle.source_raw_mesh.semantic_hash() != row['raw_mesh_semantic_hash']:
            raise RawMeshRepairError('persisted source raw mesh hash mismatch')
        if bundle.repair_plan.plan_id != row['repair_plan_id']:
            raise RawMeshRepairError('persisted repair plan id mismatch')
        if bundle.repair_plan.semantic_hash() != row['repair_plan_semantic_hash']:
            raise RawMeshRepairError('persisted repair plan hash mismatch')
        if bundle.post_repair_diagnostic.diagnostic_id != row['post_diagnostic_id']:
            raise RawMeshRepairError('persisted post-repair diagnostic id mismatch')
        if bundle.post_repair_diagnostic.semantic_hash() != row['post_diagnostic_semantic_hash']:
            raise RawMeshRepairError('persisted post-repair diagnostic hash mismatch')
        _validate_bundle_against_recomputation(bundle)
        return bundle


def _validate_source_diagnostic(
    mesh: RawVisualMesh,
    diagnostic: RawMeshDiagnosticResult,
) -> None:
    if diagnostic.raw_mesh_id != mesh.mesh_id:
        raise RawMeshRepairError('source diagnostic raw mesh id mismatch')
    if diagnostic.raw_mesh_semantic_hash != mesh.semantic_hash():
        raise RawMeshRepairError('source diagnostic raw mesh semantic hash mismatch')
    recomputed = diagnose_raw_visual_mesh(mesh, profile=diagnostic.profile)
    if recomputed != diagnostic:
        raise RawMeshRepairError('source diagnostic does not match exact source raw mesh')


def _validate_plan_inputs(
    mesh: RawVisualMesh,
    diagnostic: RawMeshDiagnosticResult,
    plan: RawMeshRepairPlan,
) -> None:
    _validate_source_diagnostic(mesh, diagnostic)
    if plan.source_raw_mesh_id != mesh.mesh_id:
        raise RawMeshRepairError('repair plan source raw mesh id mismatch')
    if plan.source_raw_mesh_semantic_hash != mesh.semantic_hash():
        raise RawMeshRepairError('repair plan source raw mesh semantic hash mismatch')
    if plan.source_diagnostic_id != diagnostic.diagnostic_id:
        raise RawMeshRepairError('repair plan source diagnostic id mismatch')
    if plan.source_diagnostic_semantic_hash != diagnostic.semantic_hash():
        raise RawMeshRepairError('repair plan source diagnostic hash mismatch')


def _validate_repaired_mesh_source(
    source_mesh: RawVisualMesh,
    repaired_mesh: RepairedRawMesh,
) -> None:
    if repaired_mesh.source_raw_mesh_id != source_mesh.mesh_id:
        raise RawMeshRepairError('repaired mesh source raw mesh id mismatch')
    if repaired_mesh.source_raw_mesh_semantic_hash != source_mesh.semantic_hash():
        raise RawMeshRepairError('repaired mesh source raw mesh hash mismatch')
    if repaired_mesh.original_asset_sha256 != source_mesh.provenance.original_asset_sha256:
        raise RawMeshRepairError('repaired mesh original asset hash mismatch')


def _validate_repaired_mesh_links(
    source_mesh: RawVisualMesh,
    plan: RawMeshRepairPlan,
    repaired_mesh: RepairedRawMesh,
) -> None:
    _validate_repaired_mesh_source(source_mesh, repaired_mesh)
    if repaired_mesh.repair_plan_id != plan.plan_id:
        raise RawMeshRepairError('repaired mesh repair plan id mismatch')
    if repaired_mesh.repair_plan_semantic_hash != plan.semantic_hash():
        raise RawMeshRepairError('repaired mesh repair plan hash mismatch')
    if repaired_mesh.source_diagnostic_id != plan.source_diagnostic_id:
        raise RawMeshRepairError('repaired mesh source diagnostic id mismatch')
    if repaired_mesh.source_diagnostic_semantic_hash != plan.source_diagnostic_semantic_hash:
        raise RawMeshRepairError('repaired mesh source diagnostic hash mismatch')


def _validate_post_diagnostic_links(
    source_mesh: RawVisualMesh,
    repaired_mesh: RepairedRawMesh,
    diagnostic: RepairedRawMeshDiagnosticResult,
) -> None:
    _validate_repaired_mesh_source(source_mesh, repaired_mesh)
    if diagnostic.repaired_mesh_id != repaired_mesh.repaired_mesh_id:
        raise RawMeshRepairError('post-repair diagnostic repaired mesh id mismatch')
    if diagnostic.repaired_mesh_semantic_hash != repaired_mesh.semantic_hash():
        raise RawMeshRepairError('post-repair diagnostic repaired mesh hash mismatch')
    if diagnostic.source_raw_mesh_id != source_mesh.mesh_id:
        raise RawMeshRepairError('post-repair diagnostic source raw mesh id mismatch')
    if diagnostic.source_raw_mesh_semantic_hash != source_mesh.semantic_hash():
        raise RawMeshRepairError('post-repair diagnostic source raw mesh hash mismatch')


def _validate_bundle_against_recomputation(bundle: RawMeshRepairBundle) -> None:
    source_recomputed = diagnose_raw_visual_mesh(
        bundle.source_raw_mesh,
        profile=bundle.source_diagnostic.profile,
    )
    if source_recomputed != bundle.source_diagnostic:
        raise RawMeshRepairError('persisted source diagnostic does not recompute exactly')
    repaired_recomputed = apply_raw_mesh_repair(
        bundle.source_raw_mesh,
        bundle.source_diagnostic,
        bundle.repair_plan,
    )
    if repaired_recomputed != bundle.repaired_mesh:
        raise RawMeshRepairError('persisted repaired mesh does not recompute exactly')
    diagnostic_recomputed = diagnose_repaired_raw_mesh(
        bundle.source_raw_mesh,
        bundle.repaired_mesh,
        profile=bundle.post_repair_diagnostic.profile,
    )
    if diagnostic_recomputed != bundle.post_repair_diagnostic:
        raise RawMeshRepairError('persisted post-repair diagnostic does not recompute exactly')


def _make_operation_result(
    *,
    operation_index: int,
    operation: RawMeshRepairOperation,
    execution_state: RepairExecutionState,
    vertex_count_before: int,
    vertex_count_after: int,
    triangle_count_before: int,
    triangle_count_after: int,
    moved_vertex_count: int,
    maximum_displacement_source_units: float,
    topology_change_count: int,
    detail: str,
) -> RawMeshRepairOperationResult:
    core = {
        'operation_index': operation_index,
        'operation': operation,
        'execution_state': execution_state,
        'vertex_count_before': vertex_count_before,
        'vertex_count_after': vertex_count_after,
        'triangle_count_before': triangle_count_before,
        'triangle_count_after': triangle_count_after,
        'moved_vertex_count': moved_vertex_count,
        'maximum_displacement_source_units': maximum_displacement_source_units,
        'topology_change_count': topology_change_count,
        'detail': detail,
    }
    provisional = RawMeshRepairOperationResult.model_construct(
        operation_result_id=f"raw-mesh-repair-operation-result:{'0' * 64}",
        **core,
    )
    identity = _semantic_hash(
        provisional.model_dump(mode='json', exclude={'operation_result_id'})
    )
    return RawMeshRepairOperationResult(
        operation_result_id=f'raw-mesh-repair-operation-result:{identity}',
        **core,
    )


def _remap_triangles(
    triangles: list[_InternalTriangle],
    mapping: dict[int, int],
) -> tuple[list[_InternalTriangle], int]:
    remapped: list[_InternalTriangle] = []
    changed_references = 0
    for a, b, c, source_primitive in triangles:
        next_indices = (mapping[a], mapping[b], mapping[c])
        changed_references += sum(
            1
            for before, after in zip((a, b, c), next_indices, strict=True)
            if before != after
        )
        remapped.append((*next_indices, source_primitive))
    return remapped, changed_references


def _consolidate_exact_duplicate_vertices(
    vertices: list[RawMeshVertex],
    triangles: list[_InternalTriangle],
) -> tuple[list[RawMeshVertex], list[_InternalTriangle], int, float, int]:
    canonical: dict[tuple[float, float, float], int] = {}
    new_vertices: list[RawMeshVertex] = []
    mapping: dict[int, int] = {}
    moved = 0
    for index, vertex in enumerate(vertices):
        key = (vertex.x, vertex.y, vertex.z)
        target = canonical.get(key)
        if target is None:
            target = len(new_vertices)
            canonical[key] = target
            new_vertices.append(vertex)
        else:
            moved += 1
        mapping[index] = target
    remapped, changed_references = _remap_triangles(triangles, mapping)
    removed = len(vertices) - len(new_vertices)
    return new_vertices, remapped, moved, 0.0, removed + changed_references


def _weld_vertices(
    vertices: list[RawMeshVertex],
    triangles: list[_InternalTriangle],
    tolerance: float,
) -> tuple[list[RawMeshVertex], list[_InternalTriangle], int, float, int]:
    cell_size = tolerance
    grid: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    new_vertices: list[RawMeshVertex] = []
    representative_source_index: list[int] = []
    mapping: dict[int, int] = {}
    moved = 0
    max_displacement = 0.0

    def cell(vertex: RawMeshVertex) -> tuple[int, int, int]:
        return (
            floor(vertex.x / cell_size),
            floor(vertex.y / cell_size),
            floor(vertex.z / cell_size),
        )

    for source_index, vertex in enumerate(vertices):
        cx, cy, cz = cell(vertex)
        candidates: list[tuple[float, int, int]] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for representative in grid.get((cx + dx, cy + dy, cz + dz), ()):
                        target_vertex = new_vertices[representative]
                        distance = _distance_vertices(vertex, target_vertex)
                        if distance <= tolerance:
                            candidates.append(
                                (
                                    distance,
                                    representative_source_index[representative],
                                    representative,
                                )
                            )
        if candidates:
            distance, _, representative = min(candidates)
            mapping[source_index] = representative
            moved += 1
            max_displacement = max(max_displacement, distance)
        else:
            representative = len(new_vertices)
            mapping[source_index] = representative
            new_vertices.append(vertex)
            representative_source_index.append(source_index)
            grid[(cx, cy, cz)].append(representative)

    remapped, changed_references = _remap_triangles(triangles, mapping)
    removed = len(vertices) - len(new_vertices)
    return (
        new_vertices,
        remapped,
        moved,
        max_displacement,
        removed + changed_references,
    )


def _remove_unreferenced_vertices(
    vertices: list[RawMeshVertex],
    triangles: list[_InternalTriangle],
) -> tuple[list[RawMeshVertex], list[_InternalTriangle], int]:
    referenced = sorted(
        {
            index
            for a, b, c, _ in triangles
            for index in (a, b, c)
        }
    )
    mapping = {old: new for new, old in enumerate(referenced)}
    new_vertices = [vertices[index] for index in referenced]
    remapped, _ = _remap_triangles(triangles, mapping)
    return new_vertices, remapped, len(vertices) - len(new_vertices)


def _remove_exact_duplicate_faces(
    vertices: list[RawMeshVertex],
    triangles: list[_InternalTriangle],
) -> tuple[list[_InternalTriangle], int]:
    seen: set[tuple[tuple[float, float, float], ...]] = set()
    kept: list[_InternalTriangle] = []
    removed = 0
    for triangle in triangles:
        a, b, c, _ = triangle
        key = tuple(
            sorted(
                (
                    _vertex_tuple(vertices[a]),
                    _vertex_tuple(vertices[b]),
                    _vertex_tuple(vertices[c]),
                )
            )
        )
        if key in seen:
            removed += 1
        else:
            seen.add(key)
            kept.append(triangle)
    return kept, removed


def _remove_degenerate_faces(
    vertices: list[RawMeshVertex],
    triangles: list[_InternalTriangle],
    area_tolerance: float,
) -> tuple[list[_InternalTriangle], int]:
    kept: list[_InternalTriangle] = []
    removed = 0
    for triangle in triangles:
        a, b, c, _ = triangle
        if len({a, b, c}) != 3:
            removed += 1
            continue
        area = _triangle_area(vertices[a], vertices[b], vertices[c])
        if area <= area_tolerance:
            removed += 1
            continue
        kept.append(triangle)
    return kept, removed


def _correct_consistent_winding(
    triangles: list[_InternalTriangle],
) -> tuple[list[_InternalTriangle], int, int, int]:
    if any(len({a, b, c}) != 3 for a, b, c, _ in triangles):
        return list(triangles), 0, 1, 0

    edge_faces: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    face_edges: list[list[tuple[int, int]]] = []
    for face_index, (a, b, c, _) in enumerate(triangles):
        local: list[tuple[int, int]] = []
        for left, right in ((a, b), (b, c), (c, a)):
            key = tuple(sorted((left, right)))
            direction = 1 if (left, right) == key else -1
            edge_faces[key].append((face_index, direction))
            local.append(key)
        face_edges.append(local)

    adjacency: dict[int, set[int]] = defaultdict(set)
    for faces in edge_faces.values():
        indices = [face_index for face_index, _ in faces]
        for left in indices:
            adjacency[left].update(index for index in indices if index != left)

    components: list[list[int]] = []
    unseen = set(range(len(triangles)))
    while unseen:
        root = min(unseen)
        queue = deque([root])
        unseen.remove(root)
        component: list[int] = []
        while queue:
            face = queue.popleft()
            component.append(face)
            for neighbor in sorted(adjacency.get(face, ())):
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    queue.append(neighbor)
        components.append(sorted(component))

    flip_state: dict[int, bool] = {}
    blocked_components = 0
    conflict_components = 0
    for component in components:
        component_set = set(component)
        component_edges = {
            edge
            for face in component
            for edge in face_edges[face]
        }
        if any(len(edge_faces[edge]) > 2 for edge in component_edges):
            blocked_components += 1
            continue

        local_state: dict[int, bool] = {component[0]: False}
        queue = deque([component[0]])
        conflict = False
        while queue and not conflict:
            face = queue.popleft()
            for edge in face_edges[face]:
                uses = [
                    item
                    for item in edge_faces[edge]
                    if item[0] in component_set
                ]
                if len(uses) != 2:
                    continue
                (left_face, left_direction), (right_face, right_direction) = uses
                if face == right_face:
                    left_face, right_face = right_face, left_face
                    left_direction, right_direction = right_direction, left_direction
                if left_face != face:
                    continue
                required_neighbor_flip = (
                    local_state[face]
                    if left_direction != right_direction
                    else not local_state[face]
                )
                existing = local_state.get(right_face)
                if existing is None:
                    local_state[right_face] = required_neighbor_flip
                    queue.append(right_face)
                elif existing != required_neighbor_flip:
                    conflict = True
                    break
        if conflict:
            conflict_components += 1
            continue
        flip_state.update(local_state)

    result: list[_InternalTriangle] = []
    flipped = 0
    for index, (a, b, c, source_primitive) in enumerate(triangles):
        if flip_state.get(index, False):
            result.append((a, c, b, source_primitive))
            flipped += 1
        else:
            result.append((a, b, c, source_primitive))
    return result, flipped, blocked_components, conflict_components


def _vertex_tuple(vertex: RawMeshVertex) -> tuple[float, float, float]:
    return (vertex.x, vertex.y, vertex.z)


def _distance_vertices(left: RawMeshVertex, right: RawMeshVertex) -> float:
    return sqrt(
        (left.x - right.x) ** 2
        + (left.y - right.y) ** 2
        + (left.z - right.z) ** 2
    )


def _triangle_area(
    first: RawMeshVertex,
    second: RawMeshVertex,
    third: RawMeshVertex,
) -> float:
    ux, uy, uz = second.x - first.x, second.y - first.y, second.z - first.z
    vx, vy, vz = third.x - first.x, third.y - first.y, third.z - first.z
    cx = uy * vz - uz * vy
    cy = uz * vx - ux * vz
    cz = ux * vy - uy * vx
    return 0.5 * sqrt(cx * cx + cy * cy + cz * cz)
