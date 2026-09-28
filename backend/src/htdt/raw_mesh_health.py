"""Geometry health summary read-model (#762 §1, §3, §6, §8).

Derives a user-facing ``MeshHealthSummary`` from canonical
``RawMeshDiagnosticResult`` findings — counts + severity + exact impact
+ suggested action per issue, never a single opaque health score. Also
provides:

- ``MeshSolverReadiness`` rows per consumer (visual, semantic room
  surfaces, GA direct/early, wave closed-volume, general-3D
  production) — ``solver_ready`` stays False like the diagnostics, but
  each consumer gets an explicit state and blocking issue refs instead
  of one boolean;
- ``MeshComponentInventory`` for connected-component noise cleanup
  (components are inventoried, never auto-discarded — a small part may
  be a speaker or fixture);
- ``classify_repair_operation`` risk classes matching the issue's
  A (deterministic) / B (bounded geometry-changing) / C (semantic)
  repair taxonomy over the existing repair operation kinds.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .raw_mesh import (
    DiagnosticCode,
    RawMeshDiagnosticResult,
    RawVisualMesh,
)
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash, canonicalize_payload


IssueCategory = Literal['topology', 'surface_quality', 'acoustic_model']
IssueSeverity = Literal['info', 'warning', 'blocker']
RepairRiskClass = Literal[
    'A_deterministic', 'B_bounded', 'C_semantic', 'unsupported'
]
ConsumerReadinessState = Literal[
    'ready',
    'ready_with_limitations',
    'unresolved_items',
    'blocked',
    'not_validated',
]






class MeshHealthIssue(BaseModel):
    """One user-facing issue row: count + severity + impact + action."""

    model_config = ConfigDict(frozen=True)

    code: DiagnosticCode
    category: IssueCategory
    severity: IssueSeverity
    count: int | None = Field(default=None, ge=0)
    impact: str = Field(min_length=1)
    action: str = Field(min_length=1)
    finding_detail: str = Field(min_length=1)


class MeshComponent(BaseModel):
    """One connected component of a raw mesh (#762 §6)."""

    model_config = ConfigDict(frozen=True)

    component_index: int = Field(ge=0)
    face_count: int = Field(ge=0)
    surface_area_m2: float = Field(ge=0.0)


class MeshComponentInventory(BaseModel):
    """Component inventory for scan-noise cleanup decisions — inspection
    only, components are never auto-discarded."""

    model_config = ConfigDict(frozen=True)

    mesh_id: str = Field(min_length=1)
    components: tuple[MeshComponent, ...]
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_inventory(self) -> 'MeshComponentInventory':
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('component inventory hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'mesh_id': self.mesh_id,
            'components': [
                item.model_dump(mode='json') for item in self.components
            ],
        }


class MeshSolverReadiness(BaseModel):
    """Readiness of the geometry for one consumer (#762 §8) — per-target
    state plus the issues that block or limit it."""

    model_config = ConfigDict(frozen=True)

    target: Literal[
        'visual_mesh',
        'semantic_room',
        'ga_direct_early',
        'wave_closed_volume',
        'general_3d_production',
    ]
    state: ConsumerReadinessState
    blocking_codes: tuple[DiagnosticCode, ...] = ()
    detail: str = ''


class MeshHealthSummary(BaseModel):
    """Derived health summary bound to exact diagnostics + mesh
    identity. Deterministic — the same diagnostics always produce the
    same summary."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    summary_id: str = Field(min_length=1)
    raw_mesh_id: str = Field(min_length=1)
    raw_mesh_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    diagnostic_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    issues: tuple[MeshHealthIssue, ...]
    readiness: tuple[MeshSolverReadiness, ...]
    component_count: int | None = Field(default=None, ge=0)
    created_at_utc: str = Field(min_length=1)
    summary_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_summary(self) -> 'MeshHealthSummary':
        if not self.summary_id.startswith('mesh-health:'):
            raise ValueError('summary id must use mesh-health: prefix')
        if self.summary_sha256 != _hash(self.identity_payload()):
            raise ValueError('health summary hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'summary_id': self.summary_id,
            'raw_mesh_id': self.raw_mesh_id,
            'raw_mesh_semantic_hash': self.raw_mesh_semantic_hash,
            'diagnostic_semantic_hash': self.diagnostic_semantic_hash,
            'issues': [item.model_dump(mode='json') for item in self.issues],
            'readiness': [
                item.model_dump(mode='json') for item in self.readiness
            ],
            'component_count': self.component_count,
            'created_at_utc': self.created_at_utc,
        }


#: (category, impact, action) per diagnostic code — user language, not
#: mesh jargon (#762 §12).
_ISSUE_TEXT: dict[str, tuple[IssueCategory, str, str]] = {
    'open_boundary': (
        'topology',
        'closed-room wave model unavailable — the room volume leaks',
        'inspect open boundary edges; close small holes or mark intentional openings',
    ),
    'non_manifold_edge': (
        'topology',
        'mesh topology is not manifold; solver compilation is blocked',
        'inspect non-manifold edges; these often need reconstruction, not cleanup',
    ),
    'duplicate_face': (
        'surface_quality',
        'duplicate surfaces add phantom area and may confuse boundaries',
        'remove exact duplicates via deterministic cleanup',
    ),
    'overlapping_face': (
        'surface_quality',
        'overlapping coplanar surfaces create ambiguous boundaries',
        'inspect overlaps; resolve manually — overlap repair is geometry-changing',
    ),
    'inverted_normal': (
        'topology',
        'inconsistent surface orientation weakens inside/outside semantics',
        'correct winding only where orientation resolves deterministically',
    ),
    'sliver_face': (
        'surface_quality',
        'degenerate sliver faces degrade surface quality and grid placement',
        'inspect slivers; bounded repair needs an explicit tolerance',
    ),
    'tiny_feature': (
        'surface_quality',
        'tiny features may disappear below the selected acoustic resolution',
        'inventory components; keep, hide, or exclude explicitly — never auto-delete',
    ),
    'watertightness': (
        'acoustic_model',
        'room volume is not closed; closed-volume wave prediction is unavailable',
        'find the leaking boundaries; decide wall vs intentional opening per gap',
    ),
}

_BLOCKER_CODES = frozenset(
    {'open_boundary', 'non_manifold_edge', 'watertightness'}
)


def _issue_for(finding: Any) -> MeshHealthIssue | None:
    if finding.state == 'pass':
        return None
    category, impact, action = _ISSUE_TEXT[finding.code]
    if finding.code in _BLOCKER_CODES and finding.state == 'fail':
        severity: IssueSeverity = 'blocker'
    elif finding.state == 'unknown':
        severity = 'warning'
    else:
        severity = 'warning'
    return MeshHealthIssue(
        code=finding.code,
        category=category,
        severity=severity,
        count=finding.count,
        impact=impact,
        action=action,
        finding_detail=finding.detail,
    )


def _readiness(issues: tuple[MeshHealthIssue, ...]) -> tuple[MeshSolverReadiness, ...]:
    failed = {issue.code for issue in issues}
    leaking = failed & {'open_boundary', 'watertightness'}
    topology_bad = failed & {'open_boundary', 'non_manifold_edge'}
    quality = failed & {
        'duplicate_face',
        'overlapping_face',
        'inverted_normal',
        'sliver_face',
        'tiny_feature',
    }
    semantic_state: ConsumerReadinessState
    if topology_bad:
        semantic_state = 'unresolved_items'
    else:
        semantic_state = 'ready'
    ga_state: ConsumerReadinessState
    if 'non_manifold_edge' in failed or 'watertightness' in failed:
        ga_state = 'blocked'
    elif leaking or quality:
        ga_state = 'ready_with_limitations'
    else:
        ga_state = 'ready'
    wave_state: ConsumerReadinessState
    if leaking or 'non_manifold_edge' in failed:
        wave_state = 'blocked'
    elif quality:
        wave_state = 'ready_with_limitations'
    else:
        wave_state = 'ready'
    return (
        MeshSolverReadiness(
            target='visual_mesh',
            state='ready',
            detail='raw imported mesh is always usable for visualization',
        ),
        MeshSolverReadiness(
            target='semantic_room',
            state=semantic_state,
            blocking_codes=tuple(sorted(topology_bad)),
            detail='openings/topology must be resolved before semantic promotion',
        ),
        MeshSolverReadiness(
            target='ga_direct_early',
            state=ga_state,
            blocking_codes=tuple(sorted(failed)),
            detail='geometric solver tolerates cosmetic defects; leaks limit meaning',
        ),
        MeshSolverReadiness(
            target='wave_closed_volume',
            state=wave_state,
            blocking_codes=tuple(sorted(leaking | {'non_manifold_edge'})),
            detail='closed volume required — open boundaries block prediction',
        ),
        MeshSolverReadiness(
            target='general_3d_production',
            state='not_validated',
            detail='production 3D wave solving is not validated per #727 semantics',
        ),
    )


def build_mesh_health_summary(
    diagnostics: RawMeshDiagnosticResult,
    *,
    mesh: RawVisualMesh | None = None,
    created_at_utc: str,
) -> MeshHealthSummary:
    """Derive the geometry health summary from canonical diagnostics."""
    issues = tuple(
        issue
        for issue in (_issue_for(finding) for finding in diagnostics.findings)
        if issue is not None
    )
    component_count: int | None = None
    if mesh is not None:
        component_count = len(
            mesh_component_inventory(mesh).components
        )
    payload: dict[str, Any] = {
        'summary_id': f'mesh-health:{diagnostics.diagnostic_id.split(":")[-1][:32]}',
        'raw_mesh_id': diagnostics.raw_mesh_id,
        'raw_mesh_semantic_hash': diagnostics.raw_mesh_semantic_hash,
        'diagnostic_semantic_hash': diagnostics.semantic_hash(),
        'issues': issues,
        'readiness': _readiness(issues),
        'component_count': component_count,
        'created_at_utc': created_at_utc,
    }
    provisional = MeshHealthSummary.model_construct(**canonicalize_payload(MeshHealthSummary, dict(
        **payload, summary_sha256='0' * 64
    )))
    return MeshHealthSummary.model_validate(
        {
            **payload,
            'summary_sha256': _hash(provisional.identity_payload()),
        }
    )


def mesh_component_inventory(mesh: RawVisualMesh) -> MeshComponentInventory:
    """Face-connected component inventory with per-component surface
    area (#762 §6). Deterministic; ordering is by first face index."""
    parent = list(range(len(mesh.triangles)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    edge_owner: dict[tuple[int, int], int] = {}
    points = [(v.x, v.y, v.z) for v in mesh.vertices]
    for index, tri in enumerate(mesh.triangles):
        for start, end in (
            (tri.a, tri.b),
            (tri.b, tri.c),
            (tri.c, tri.a),
        ):
            key = tuple(sorted((start, end)))
            if key in edge_owner:
                union(index, edge_owner[key])
            else:
                edge_owner[key] = index

    components: dict[int, list[int]] = {}
    for index in range(len(mesh.triangles)):
        components.setdefault(find(index), []).append(index)

    def area(face: int) -> float:
        tri = mesh.triangles[face]
        a, b, c = points[tri.a], points[tri.b], points[tri.c]
        ab = tuple(b[k] - a[k] for k in range(3))
        ac = tuple(c[k] - a[k] for k in range(3))
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        return 0.5 * (cross[0] ** 2 + cross[1] ** 2 + cross[2] ** 2) ** 0.5

    rows = [
        MeshComponent(
            component_index=order,
            face_count=len(faces),
            surface_area_m2=sum(area(f) for f in faces),
        )
        for order, (_, faces) in enumerate(
            sorted(components.items(), key=lambda kv: min(kv[1]))
        )
    ]
    payload: dict[str, Any] = {
        'mesh_id': mesh.mesh_id,
        'components': tuple(rows),
    }
    provisional = MeshComponentInventory.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return MeshComponentInventory.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


#: Risk class per repair operation kind (#762 §3) over the kinds declared
#: in ``raw_mesh_repair.py``. C-class items are semantic authoring
#: decisions, not mesh repairs.
_REPAIR_RISK: dict[str, RepairRiskClass] = {
    'exact_duplicate_vertex_consolidation': 'A_deterministic',
    'remove_unreferenced_vertices': 'A_deterministic',
    'remove_exact_duplicate_faces': 'A_deterministic',
    'remove_degenerate_faces': 'A_deterministic',
    'correct_consistent_winding': 'B_bounded',
    'tolerance_vertex_weld': 'B_bounded',
    'fill_hole': 'B_bounded',
    'large_gap_closure': 'B_bounded',
    'non_manifold_surgery': 'unsupported',
    'self_intersection_remesh': 'unsupported',
    'boolean_reconstruction': 'unsupported',
    'point_cloud_surface_reconstruction': 'unsupported',
    'overlapping_surface_resolution': 'unsupported',
    'acoustic_room_inference': 'C_semantic',
}


def classify_repair_operation(kind: str) -> RepairRiskClass:
    """Map a repair operation kind onto the issue's repair-risk taxonomy.

    Unknown kinds classify as ``unsupported`` — never assumed safe."""
    return _REPAIR_RISK.get(kind, 'unsupported')


__all__ = [
    'IssueCategory',
    'IssueSeverity',
    'MeshComponent',
    'MeshComponentInventory',
    'MeshHealthIssue',
    'MeshHealthSummary',
    'MeshSolverReadiness',
    'RepairRiskClass',
    'build_mesh_health_summary',
    'classify_repair_operation',
    'mesh_component_inventory',
]
