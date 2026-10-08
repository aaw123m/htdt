"""Geometry intake readiness authority (Issue #866).

Implements the intake chain

    SOURCE_GEOMETRY -> HEALTH_CHECK -> REPAIR_PROPOSAL ->
    EXPLICIT_ACCEPTANCE -> DERIVED_GEOMETRY_REVISION -> SOLVER_READINESS

over imported scene geometry, with sealed records at every link and no
silent mutation of the source model.

Records (all content-addressed, append-only via
``cad_geometry_intake_repository``):

* :class:`GeometryIntakeSubject` (``gis-``) — the exact solver-facing
  source geometry view: one part per room boundary / entity body /
  unresolved geometry stub, declared openings, and the unit-authority
  summary. Built by :func:`build_geometry_intake_subject`; identical
  inputs always produce an identical ``subject_sha256``.
* :class:`GeometryIntakeReport` (``gdr-``) — the health-check output:
  a defect list where every defect carries kind, severity, origin
  (``source_model`` vs ``solver_limitation``), affected parts/entities,
  consequence, and a suggested action.
* :class:`GeometryRepairProposal` (``grp-``) — proposed repairs only;
  nothing is applied here.
* :class:`GeometryRepairAcceptance` (``gra-``) — the explicit operator
  decision record: who decided, when, and which parameters were chosen.
* :class:`DerivedGeometryRevision` (``gdv-``) — the immutable derived
  geometry produced by applying the *accepted* actions, linked to the
  source subject, report, proposal and acceptance; carries re-diagnosed
  residual defects.
* :class:`GeometrySolverReadinessVerdict` (``srv-``) — the readiness
  verdict against a selected solver adapter + capability manifest:
  ``supported`` / ``degraded`` / ``unsupported`` / ``unknown``, binding
  the exact geometry hash it evaluated. Any geometry or solver change
  deterministically invalidates the evidence via
  :func:`readiness_evidence_state`.

Fail-closed: unresolved ``critical`` defects, solver-unsupported
conditions, and missing evidence (undeclared units, unavailable
geometry, undetermined diagnostics) never read as success —
:func:`assert_geometry_solver_execution_permitted` blocks ``unknown``
just as hard as ``unsupported``.
"""

from __future__ import annotations

from math import cos, isfinite, pi, sin, sqrt
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_body_mesh import decode_body_mesh_bin, encode_body_mesh_bin
from .cad_scene import (
    BodyMeshAsset,
    BodyMeshTriangle,
    BodyMeshVertex,
    MeshImportAuthority,
    PHYSICAL_ENTITY_KINDS,
    SceneDocument,
    room_vertices,
)
from .cad_solver_capability_manifest import SolverCapabilityManifest
from .cad_acoustic_solver_adapter import AcousticSolverAdapterDescriptor
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .raw_mesh import (
    RawMeshDiagnosticFinding,
    RawMeshDiagnosticProfile,
    RawMeshVertex,
    RawVisualMesh,
    diagnose_raw_visual_mesh,
    import_raw_visual_mesh,
)
from .raw_mesh_health import mesh_component_inventory
from .raw_mesh_repair import (
    CorrectConsistentWinding,
    ExactDuplicateVertexConsolidation,
    RemoveDegenerateFaces,
    RemoveExactDuplicateFaces,
    RemoveUnreferencedVertices,
    RawMeshRepairLineageRef,
    RepairedRawMesh,
    RepairedRawMeshDiagnosticResult,
    ToleranceVertexWeld,
    apply_raw_mesh_repair,
    diagnose_repaired_raw_mesh,
    make_raw_mesh_repair_lineage_ref,
    make_raw_mesh_repair_plan,
)

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

GEOMETRY_INTAKE_AUTHORITY_VERSION = '1'
GEOMETRY_INTAKE_EVALUATOR_ID = 'htdt.geometry_intake_diagnostics'
GEOMETRY_INTAKE_EVALUATOR_VERSION = '1'
GEOMETRY_REPAIR_EVALUATOR_ID = 'htdt.geometry_intake_repair'
GEOMETRY_READINESS_EVALUATOR_ID = 'htdt.geometry_solver_readiness'


# --- errors ----------------------------------------------------------------


class GeometryIntakeError(ValueError):
    """Geometry intake operation failed."""


class GeometryRepairAcceptanceError(GeometryIntakeError):
    """An acceptance record does not cover its proposal exactly."""


class GeometrySolverExecutionBlockedError(GeometryIntakeError):
    """Unresolved defects/evidence gaps block solver execution."""

    def __init__(self, verdict_id: str, reasons: tuple[str, ...]) -> None:
        self.verdict_id = verdict_id
        self.reasons = reasons
        super().__init__(
            'geometry intake readiness blocks solver execution '
            f'({verdict_id}): ' + '; '.join(reasons)
        )


# --- literal vocabularies ---------------------------------------------------

IntakeUnitState = Literal['declared', 'legacy_assumed', 'undeclared']
IntakePartKind = Literal['room_boundary', 'entity_body']
IntakePartRole = Literal['room_boundary', 'object_surface']
IntakeMaterialState = Literal['assigned', 'unassigned', 'unknown']
IntakeOpeningResolution = Literal['unresolved', 'resolved_open', 'resolved_closed']
GeometrySourceKind = Literal['scene_document', 'ifc_import', 'external_reference']

GeometryDefectKind = Literal[
    'open_boundary_edges',
    'non_manifold_edges',
    'disconnected_regions',
    'duplicate_or_overlapping_faces',
    'inverted_normals',
    'degenerate_or_tiny_features',
    'portal_opening_ambiguity',
    'material_assignment_gap',
    'coordinate_unit_anomaly',
    'not_watertight',
    'solver_unsupported_condition',
    'geometry_unavailable',
    'diagnostic_evidence_gap',
]
GeometryDefectSeverity = Literal['info', 'warning', 'critical']
GeometryDefectOrigin = Literal['source_model', 'solver_limitation']
GeometryRepairability = Literal['automatic', 'operator_required', 'none']

GeometryRepairActionKind = Literal[
    'consolidate_exact_duplicate_vertices',
    'weld_vertices_within_tolerance',
    'remove_unreferenced_vertices',
    'remove_exact_duplicate_faces',
    'correct_consistent_winding',
    'remove_degenerate_faces',
    'remove_disconnected_fragment',
    'declare_units',
    'assign_material',
    'resolve_portal',
    'no_repair_available',
]
RepairAutomation = Literal['automatic', 'operator_required']
RepairDecisionValue = Literal['accepted', 'rejected']
PortalResolutionChoice = Literal['open_portal', 'closed_boundary']

SolverReadinessState = Literal['supported', 'degraded', 'unsupported', 'unknown']
ReadinessEvidenceState = Literal[
    'current', 'stale_geometry', 'stale_solver', 'stale_manifest'
]


# --- sealed-record helpers (mirrors cad_delegated_provider) -----------------


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _require_refs(*refs: AuthorityRef | None) -> None:
    for ref in refs:
        if ref is not None and ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


def _require_iso8601(value: str, label: str) -> None:
    if not isinstance(value, str) or 'T' not in value:
        raise ValueError(f'{label} must be an ISO-8601 UTC timestamp')


def _require_nonblank(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be a non-empty string')


# --- subject model ----------------------------------------------------------


class IntakeMeshPart(BaseModel):
    """One solver-facing geometry part in subject (world) coordinates."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    part_id: str = Field(min_length=1)
    owner_kind: IntakePartKind
    owner_ref: str = Field(min_length=1)
    role: IntakePartRole
    mesh: RawVisualMesh
    material_state: IntakeMaterialState = 'unknown'
    material_label: str | None = None
    import_authority: MeshImportAuthority | None = None
    source_asset_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def consistent(self) -> 'IntakeMeshPart':
        if self.material_state == 'assigned' and not self.material_label:
            raise ValueError('assigned material state requires a material label')
        return self


class IntakePartStub(BaseModel):
    """A geometry part whose mesh could not be resolved — the subject keeps
    it so diagnostics can emit ``geometry_unavailable`` instead of the part
    silently disappearing from the intake record."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    part_id: str = Field(min_length=1)
    owner_kind: IntakePartKind
    owner_ref: str = Field(min_length=1)
    role: IntakePartRole
    unavailability_reason: str = Field(min_length=1)
    source_asset_ref: AuthorityRef | None = None


class IntakeOpening(BaseModel):
    """A declared opening/portal candidate on a boundary part."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    opening_id: str = Field(min_length=1)
    host_part_id: str = Field(min_length=1)
    host_wall_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    is_open: bool
    area_m2: float = Field(gt=0.0)
    resolution: IntakeOpeningResolution = 'unresolved'


class GeometryIntakeSubject(BaseModel):
    """The exact source-geometry view the intake chain operates on.

    Parts are normalized into subject (world) coordinates; source assets
    remain content-addressed through each part's ``source_asset_ref`` and
    the mesh's own import provenance.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    subject_id: str = Field(pattern=r'^gis-[0-9a-f]{24}$')
    subject_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority_version: Literal['1'] = GEOMETRY_INTAKE_AUTHORITY_VERSION
    document_id: str = Field(min_length=1)
    source_kind: GeometrySourceKind
    source_refs: tuple[AuthorityRef, ...]
    parts: tuple[IntakeMeshPart, ...]
    unresolved_parts: tuple[IntakePartStub, ...] = ()
    openings: tuple[IntakeOpening, ...] = ()
    unit_state: IntakeUnitState
    unit_details: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_subject(self) -> 'GeometryIntakeSubject':
        part_ids = [part.part_id for part in self.parts]
        stub_ids = [stub.part_id for stub in self.unresolved_parts]
        if len(part_ids) != len(set(part_ids)):
            raise ValueError('intake subject part ids must be unique')
        if len(stub_ids) != len(set(stub_ids)):
            raise ValueError('intake subject stub ids must be unique')
        if set(part_ids) & set(stub_ids):
            raise ValueError('part ids and stub ids must not overlap')
        part_id_set = set(part_ids)
        opening_ids = [opening.opening_id for opening in self.openings]
        if len(opening_ids) != len(set(opening_ids)):
            raise ValueError('intake opening ids must be unique')
        for opening in self.openings:
            if opening.host_part_id not in part_id_set:
                raise ValueError(
                    f'opening {opening.opening_id} references unknown part '
                    f'{opening.host_part_id}'
                )
        expected = _hash(self.identity_payload())
        if self.subject_sha256 != expected:
            raise ValueError('intake subject sha256 mismatch')
        if self.subject_id != _semantic_id('gis', expected):
            raise ValueError('intake subject id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'subject_id', 'subject_sha256'}
        )

    def part(self, part_id: str) -> IntakeMeshPart:
        for part in self.parts:
            if part.part_id == part_id:
                return part
        raise KeyError(f'unknown intake part {part_id}')

    @classmethod
    def create(
        cls,
        *,
        document_id: str,
        source_kind: GeometrySourceKind,
        source_refs: Sequence[AuthorityRef],
        parts: Sequence[IntakeMeshPart],
        unresolved_parts: Sequence[IntakePartStub] = (),
        openings: Sequence[IntakeOpening] = (),
        unit_state: IntakeUnitState,
        unit_details: str,
    ) -> 'GeometryIntakeSubject':
        return _seal(
            cls,
            {
                'authority_version': GEOMETRY_INTAKE_AUTHORITY_VERSION,
                'document_id': document_id,
                'source_kind': source_kind,
                'source_refs': tuple(source_refs),
                'parts': tuple(parts),
                'unresolved_parts': tuple(unresolved_parts),
                'openings': tuple(openings),
                'unit_state': unit_state,
                'unit_details': unit_details,
            },
            'subject_id',
            'subject_sha256',
            'gis',
        )


# --- subject construction ---------------------------------------------------


def _quaternion_matrix(
    w: float, x: float, y: float, z: float
) -> tuple[tuple[float, float, float], ...]:
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)),
        (2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)),
        (2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)),
    )


def _world_point(
    matrix: tuple[tuple[float, float, float], ...],
    position: tuple[float, float, float],
    point: tuple[float, float, float],
) -> tuple[float, float, float]:
    return tuple(
        matrix[row][0] * point[0]
        + matrix[row][1] * point[1]
        + matrix[row][2] * point[2]
        + position[row]
        for row in range(3)
    )  # type: ignore[return-value]


def _mesh_via_meshbin(
    vertices: Sequence[tuple[float, float, float]],
    triangles: Sequence[tuple[int, int, int]],
    *,
    source_name: str,
    source_asset_sha256: str | None = None,
) -> RawVisualMesh:
    """Normalize synthesized/transformed geometry through the canonical
    meshbin importer so every part carries honest ``htdt_meshbin_v1``
    provenance (vertices round-trip through f32 — the documented intake
    precision)."""
    body_vertices = [
        BodyMeshVertex(x_m=v[0], y_m=v[1], z_m=v[2]) for v in vertices
    ]
    body_triangles = [
        BodyMeshTriangle(a=t[0], b=t[1], c=t[2]) for t in triangles
    ]
    blob = encode_body_mesh_bin(body_vertices, body_triangles)
    mesh = import_raw_visual_mesh(
        blob,
        source_name=source_name,
        format_hint='htdt_meshbin_v1',
    )
    return mesh


def _cross2d(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _point_in_triangle_2d(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    orientation: float,
) -> bool:
    return (
        orientation * _cross2d(a, b, p) >= 0.0
        and orientation * _cross2d(b, c, p) >= 0.0
        and orientation * _cross2d(c, a, p) >= 0.0
    )


def _triangulate_footprint(
    points: Sequence[tuple[float, float]],
) -> tuple[tuple[int, int, int], ...]:
    """Deterministic ear-clip triangulation of a simple footprint polygon.

    Returns triangle index triples with the polygon's winding (CCW for a
    positively-oriented footprint)."""
    n = len(points)
    if n < 3:
        raise GeometryIntakeError('footprint requires at least 3 vertices')
    if n == 3:
        return ((0, 1, 2),)
    signed = sum(
        points[i][0] * points[(i + 1) % n][1]
        - points[(i + 1) % n][0] * points[i][1]
        for i in range(n)
    ) * 0.5
    if abs(signed) <= 1.0e-12:
        raise GeometryIntakeError('footprint polygon has zero area')
    orientation = 1.0 if signed > 0.0 else -1.0
    remaining = list(range(n))
    triangles: list[tuple[int, int, int]] = []
    while len(remaining) > 3:
        ear_found = False
        for position, current in enumerate(remaining):
            previous = remaining[position - 1]
            following = remaining[(position + 1) % len(remaining)]
            a, b, c = points[previous], points[current], points[following]
            if orientation * _cross2d(a, b, c) <= 1.0e-12:
                continue
            if any(
                candidate not in {previous, current, following}
                and _point_in_triangle_2d(
                    points[candidate], a, b, c, orientation
                )
                for candidate in remaining
            ):
                continue
            triangles.append((previous, current, following))
            del remaining[position]
            ear_found = True
            break
        if not ear_found:
            raise GeometryIntakeError(
                'deterministic ear clipping could not triangulate footprint'
            )
    triangles.append((remaining[0], remaining[1], remaining[2]))
    return tuple(triangles)


def _prism_vertices_triangles(
    footprint: Sequence[tuple[float, float]],
    height: float,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Closed outward-wound prism over an XY footprint of height ``height``."""
    n = len(footprint)
    vertices: list[tuple[float, float, float]] = [
        (x, y, 0.0) for x, y in footprint
    ] + [(x, y, height) for x, y in footprint]
    cap = _triangulate_footprint(footprint)
    triangles: list[tuple[int, int, int]] = []
    # floor faces down: reverse footprint winding
    for a, b, c in cap:
        triangles.append((a, c, b))
    # ceiling faces up: keep footprint winding, offset into top ring
    for a, b, c in cap:
        triangles.append((a + n, b + n, c + n))
    # walls: outward for a CCW footprint — (i, j, j+n), (i, j+n, i+n);
    # reverse the edge direction for CW footprints to keep normals outward.
    signed = sum(
        footprint[i][0] * footprint[(i + 1) % n][1]
        - footprint[(i + 1) % n][0] * footprint[i][1]
        for i in range(n)
    ) * 0.5
    for i in range(n):
        j = (i + 1) % n
        if signed > 0:
            triangles.append((i, j, j + n))
            triangles.append((i, j + n, i + n))
        else:
            triangles.append((i, j + n, j))
            triangles.append((i, i + n, j + n))
    return vertices, triangles


def _box_vertices_triangles(
    size: tuple[float, float, float],
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    x, y, z = size
    vertices = [
        (0.0, 0.0, 0.0), (x, 0.0, 0.0), (x, y, 0.0), (0.0, y, 0.0),
        (0.0, 0.0, z), (x, 0.0, z), (x, y, z), (0.0, y, z),
    ]
    triangles = [
        (0, 2, 1), (0, 3, 2),      # bottom (-z)
        (4, 5, 6), (4, 6, 7),      # top (+z)
        (0, 1, 5), (0, 5, 4),      # front (-y)
        (1, 2, 6), (1, 6, 5),      # right (+x)
        (2, 3, 7), (2, 7, 6),      # back (+y)
        (3, 0, 4), (3, 4, 7),      # left (-x)
    ]
    return vertices, triangles


def _cylinder_vertices_triangles(
    radius: float,
    height: float,
    segments: int = 16,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    vertices: list[tuple[float, float, float]] = [(0.0, 0.0, 0.0)]
    for i in range(segments):
        angle = 2.0 * pi * i / segments
        vertices.append((radius * cos(angle), radius * sin(angle), 0.0))
    vertices.append((0.0, 0.0, height))
    top_center = segments + 1
    for i in range(segments):
        angle = 2.0 * pi * i / segments
        vertices.append((radius * cos(angle), radius * sin(angle), height))
    triangles: list[tuple[int, int, int]] = []
    for i in range(segments):
        j = (i + 1) % segments
        # bottom cap faces down
        triangles.append((0, j + 1, i + 1))
        # top cap faces up
        triangles.append((top_center, top_center + 1 + i, top_center + 1 + j))
        # side wall, outward
        triangles.append((i + 1, j + 1, top_center + 1 + j))
        triangles.append((i + 1, top_center + 1 + j, top_center + 1 + i))
    return vertices, triangles


def _entity_local_mesh_arrays(
    entity: Any,
    read_blob: Any,
) -> tuple[
    list[tuple[float, float, float]] | None,
    list[tuple[int, int, int]] | None,
    MeshImportAuthority | None,
    str | None,
    AuthorityRef | None,
]:
    """Return entity-local (vertices, triangles, import_authority,
    unavailable_reason, source_asset_ref)."""
    body = entity.body_geometry
    size = entity.size_m
    if body is None:
        if size is None:
            return None, None, None, 'physical entity has no size', None
        v, t = _box_vertices_triangles(
            (float(size.x_m), float(size.y_m), float(size.z_m))
        )
        return v, t, None, None, None
    if body.kind == 'box':
        if size is None:
            return None, None, None, 'box entity has no size', None
        v, t = _box_vertices_triangles(
            (float(size.x_m), float(size.y_m), float(size.z_m))
        )
        return v, t, None, None, None
    if body.kind == 'cylinder':
        if size is None or body.radius_m is None:
            return None, None, None, 'cylinder entity lacks size/radius', None
        v, t = _cylinder_vertices_triangles(
            float(body.radius_m), float(size.z_m)
        )
        return v, t, None, None, None
    if body.kind == 'extruded_polygon':
        if size is None or body.footprint_vertices is None:
            return None, None, None, 'extruded entity lacks footprint', None
        footprint = [
            (float(vertex.x_m), float(vertex.y_m))
            for vertex in body.footprint_vertices
        ]
        v, t = _prism_vertices_triangles(footprint, float(size.z_m))
        return v, t, None, None, None
    if body.kind == 'mesh_asset':
        mesh = body.mesh
        if mesh is None:
            reference = body.mesh_reference
            if reference is None:
                return None, None, None, 'mesh_asset body has no mesh', None
            if read_blob is None:
                return (
                    None, None, None,
                    'mesh_asset blob unavailable without a blob reader',
                    AuthorityRef(
                        kind='content_blob',
                        ref_id=reference.geometry_asset_sha256,
                        ref_sha256=reference.geometry_asset_sha256,
                    ),
                )
            payload = read_blob(reference.geometry_asset_sha256)
            if payload is None:
                return (
                    None, None, None,
                    'mesh_asset blob missing from store',
                    AuthorityRef(
                        kind='content_blob',
                        ref_id=reference.geometry_asset_sha256,
                        ref_sha256=reference.geometry_asset_sha256,
                    ),
                )
            vertices_v, triangles_t = decode_body_mesh_bin(payload)
            mesh = BodyMeshAsset(
                asset_sha256=reference.source_asset_sha256,
                source_name=reference.source_name,
                asset_format=reference.source_format,
                original_size_bytes=reference.original_size_bytes,
                local_offset_m=reference.local_offset_m,
                uniform_scale=reference.uniform_scale,
                vertices=list(vertices_v),
                triangles=list(triangles_t),
                import_authority=reference.import_authority,
            )
        scale = float(mesh.uniform_scale)
        offset = mesh.local_offset_m
        vertices = [
            (
                float(vertex.x_m) * scale + offset.x_m,
                float(vertex.y_m) * scale + offset.y_m,
                float(vertex.z_m) * scale + offset.z_m,
            )
            for vertex in mesh.vertices
        ]
        triangles = [(t.a, t.b, t.c) for t in mesh.triangles]
        asset_ref = AuthorityRef(
            kind='entity_mesh_asset',
            ref_id=f'{entity.entity_id}:{mesh.asset_sha256}',
            ref_sha256=mesh.asset_sha256,
        )
        return vertices, triangles, mesh.import_authority, None, asset_ref
    return None, None, None, f'unsupported body kind {body.kind}', None


def _subject_unit_summary(
    parts: Sequence[IntakeMeshPart],
) -> tuple[IntakeUnitState, str]:
    undeclared: list[str] = []
    legacy: list[str] = []
    for part in parts:
        authority = part.import_authority
        if authority is None:
            continue
        if authority.unit_declared_by == 'undeclared' or (
            authority.source_unit == 'unknown'
        ):
            undeclared.append(part.part_id)
        elif authority.unit_declared_by == 'legacy_assumed_meter':
            legacy.append(part.part_id)
    if undeclared:
        return 'undeclared', 'undeclared unit authority on ' + ','.join(undeclared)
    if legacy:
        return (
            'legacy_assumed',
            'legacy assumed-meter authority on ' + ','.join(legacy),
        )
    return 'declared', 'all mesh parts carry declared unit authority'


def build_geometry_intake_subject(
    document: SceneDocument,
    *,
    source_kind: GeometrySourceKind = 'scene_document',
    source_revision_ref: AuthorityRef | None = None,
    extra_source_refs: Sequence[AuthorityRef] = (),
    read_blob: Any = None,
    material_assignments: Mapping[str, str] | None = None,
) -> GeometryIntakeSubject:
    """Build the sealed solver-facing source-geometry view of a document.

    The room boundary is normalized to a closed outward-wound prism;
    declared openings are tracked separately so the difference between
    semantic portals and geometric enclosure is exactly what
    ``portal_opening_ambiguity`` measures. Entity bodies are transformed
    into the subject (world) frame — diagnostics on each part are
    frame-invariant but evidence reads in meters where declared.
    ``material_assignments`` maps owner refs (``'room'`` or entity ids)
    to material labels; omitting it marks every part ``unknown`` rather
    than ``unassigned``.
    """
    parts: list[IntakeMeshPart] = []
    stubs: list[IntakePartStub] = []

    # --- room boundary -------------------------------------------------
    room = document.room
    if room is not None:
        footprint = [(v.x_m, v.y_m) for v in room_vertices(room)]
        rv, rt = _prism_vertices_triangles(footprint, float(room.height_m))
        room_mesh = _mesh_via_meshbin(
            rv, rt, source_name=f'{document.document_id}:room-boundary'
        )
        room_material_state: IntakeMaterialState = 'unknown'
        room_material_label: str | None = None
        if material_assignments is not None:
            room_material_label = material_assignments.get('room')
            room_material_state = (
                'assigned' if room_material_label else 'unassigned'
            )
        parts.append(
            IntakeMeshPart(
                part_id='room-boundary',
                owner_kind='room_boundary',
                owner_ref=room.room_id,
                role='room_boundary',
                mesh=room_mesh,
                material_state=room_material_state,
                material_label=room_material_label,
                source_asset_ref=AuthorityRef(
                    kind='room_prism', ref_id=room.room_id
                ),
            )
        )

    # --- entity bodies ---------------------------------------------------
    for entity in document.entities:
        if entity.kind not in PHYSICAL_ENTITY_KINDS:
            continue
        part_id = f'entity:{entity.entity_id}'
        vertices, triangles, authority, unavailable, asset_ref = (
            _entity_local_mesh_arrays(entity, read_blob)
        )
        material_state: IntakeMaterialState = 'unknown'
        material_label: str | None = None
        if material_assignments is not None:
            material_label = material_assignments.get(entity.entity_id)
            material_state = 'assigned' if material_label else 'unassigned'
        if vertices is None or triangles is None:
            stubs.append(
                IntakePartStub(
                    part_id=part_id,
                    owner_kind='entity_body',
                    owner_ref=entity.entity_id,
                    role='object_surface',
                    unavailability_reason=unavailable or 'unresolved',
                    source_asset_ref=asset_ref,
                )
            )
            continue
        rotation = _quaternion_matrix(
            float(entity.orientation.w),
            float(entity.orientation.x),
            float(entity.orientation.y),
            float(entity.orientation.z),
        )
        position = (
            float(entity.position.x_m),
            float(entity.position.y_m),
            float(entity.position.z_m),
        )
        world = [
            _world_point(rotation, position, point) for point in vertices
        ]
        mesh = _mesh_via_meshbin(
            world,
            triangles,
            source_name=f'{document.document_id}:{entity.entity_id}',
        )
        parts.append(
            IntakeMeshPart(
                part_id=part_id,
                owner_kind='entity_body',
                owner_ref=entity.entity_id,
                role='object_surface',
                mesh=mesh,
                material_state=material_state,
                material_label=material_label,
                import_authority=authority,
                source_asset_ref=asset_ref,
            )
        )

    # --- openings ---------------------------------------------------------
    openings: list[IntakeOpening] = []
    topology = document.wall_topology
    if topology is not None:
        for opening in topology.openings:
            openings.append(
                IntakeOpening(
                    opening_id=opening.opening_id,
                    host_part_id='room-boundary',
                    host_wall_id=opening.wall_id,
                    kind=opening.kind,
                    is_open=bool(opening.is_open),
                    area_m2=float(opening.width_m) * float(opening.height_m),
                )
            )

    unit_state, unit_details = _subject_unit_summary(parts)
    refs: list[AuthorityRef] = []
    if source_revision_ref is not None:
        refs.append(source_revision_ref)
    refs.extend(extra_source_refs)
    return GeometryIntakeSubject.create(
        document_id=document.document_id,
        source_kind=source_kind,
        source_refs=tuple(refs),
        parts=tuple(parts),
        unresolved_parts=tuple(stubs),
        openings=tuple(openings),
        unit_state=unit_state,
        unit_details=unit_details,
    )


# --- solver geometry context --------------------------------------------------


class SolverGeometryContext(BaseModel):
    """The solver-side geometry contract a readiness verdict is measured
    against. Derived from the adapter descriptor (+ capability manifest
    when present) or supplied directly for one-off evaluation."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    acoustic_domain: Literal['wave', 'geometric'] | None = None
    requires_watertight_volume: bool = False
    requires_material_assignments: bool = False
    requires_resolved_portals: bool = False
    open_portals_unsupported: bool = False
    requires_consistent_winding: bool = False
    minimum_feature_m: float | None = Field(default=None, gt=0.0)


def derive_solver_geometry_context(
    descriptor: AcousticSolverAdapterDescriptor | None = None,
    manifest: SolverCapabilityManifest | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> SolverGeometryContext:
    """Derive the geometry contract from a solver adapter descriptor.

    ``wave`` solvers require a watertight volume, declared materials and
    consistent winding; ``geometric`` solvers require materials and
    resolved portals but not watertightness. A manifest row declaring
    ``portal_region_coupling`` UNSUPPORTED flips open portals into a
    solver-unsupported condition. ``overrides`` may set any field.
    """
    if manifest is not None and descriptor is not None:
        if manifest.adapter_descriptor_id != descriptor.descriptor_id:
            raise GeometryIntakeError(
                'capability manifest does not describe this adapter'
            )
    values: dict[str, Any] = {}
    if descriptor is not None:
        values['acoustic_domain'] = descriptor.acoustic_domain
        if descriptor.acoustic_domain == 'wave':
            values.update(
                requires_watertight_volume=True,
                requires_material_assignments=True,
                requires_resolved_portals=True,
                requires_consistent_winding=True,
            )
        else:
            values.update(
                requires_material_assignments=True,
                requires_resolved_portals=True,
            )
    if manifest is not None:
        portal_row = manifest.row_for('portal_region_coupling')
        if portal_row.state == 'UNSUPPORTED':
            values['open_portals_unsupported'] = True
            values['requires_resolved_portals'] = True
        elif portal_row.state == 'BOUNDED':
            values['requires_resolved_portals'] = True
    if overrides:
        values.update(overrides)
    return SolverGeometryContext(**values)


# --- defect model -------------------------------------------------------------


class GeometryDefect(BaseModel):
    """One intake defect: kind + severity + origin + affected geometry."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    defect_id: str = Field(pattern=r'^dgx-[0-9a-f]{24}$')
    kind: GeometryDefectKind
    severity: GeometryDefectSeverity
    origin: GeometryDefectOrigin
    part_refs: tuple[str, ...] = ()
    entity_refs: tuple[str, ...] = ()
    opening_refs: tuple[str, ...] = ()
    evidence: dict[str, Any] = Field(default_factory=dict)
    consequence: str = Field(min_length=1)
    suggested_action: str = Field(min_length=1)
    repairable: GeometryRepairability

    @model_validator(mode='after')
    def validate_defect(self) -> 'GeometryDefect':
        expected = _semantic_id('dgx', _hash(self.defect_payload()))
        if self.defect_id != expected:
            raise ValueError('defect id does not match defect content')
        return self

    def defect_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'defect_id'})

    @classmethod
    def create(cls, **payload: Any) -> 'GeometryDefect':
        probe = cls.model_construct(**canonicalize_payload(cls, dict(payload)))
        digest = _hash(probe.defect_payload())
        return cls(**payload, defect_id=_semantic_id('dgx', digest))


# --- diagnostic evaluator -----------------------------------------------------

_FINDING_DEFECTS: Mapping[
    str, tuple[GeometryDefectKind, GeometryDefectSeverity]
] = {
    'open_boundary': ('open_boundary_edges', 'critical'),
    'non_manifold_edge': ('non_manifold_edges', 'critical'),
    'duplicate_face': ('duplicate_or_overlapping_faces', 'warning'),
    'overlapping_face': ('duplicate_or_overlapping_faces', 'warning'),
    'inverted_normal': ('inverted_normals', 'warning'),
    'sliver_face': ('degenerate_or_tiny_features', 'warning'),
    'tiny_feature': ('degenerate_or_tiny_features', 'warning'),
    'watertightness': ('not_watertight', 'warning'),
}

_FINDING_CONSEQUENCE: Mapping[str, tuple[str, str]] = {
    'open_boundary': (
        'boundary holes make any closed-volume acoustic solve invalid',
        'weld duplicate/tolerance vertices or repair the source mesh',
    ),
    'non_manifold_edge': (
        'non-manifold topology has no consistent inside/outside volume',
        'repair topology in the source tool; HTDT cannot auto-split edges',
    ),
    'duplicate_face': (
        'duplicate faces double-count surface area in acoustic exchange',
        'remove exact duplicate faces',
    ),
    'overlapping_face': (
        'overlapping faces corrupt projected-area energy estimates',
        'remove exact duplicates; remodel residual overlaps upstream',
    ),
    'inverted_normal': (
        'inverted faces misorient absorption/reflection direction',
        'apply consistent-winding repair',
    ),
    'sliver_face': (
        'sliver faces fall below solver numerical conditioning limits',
        'remove degenerate faces at a declared area tolerance',
    ),
    'tiny_feature': (
        'features below solver resolution silently disappear or alias',
        'remove or consolidate features below the solver feature limit',
    ),
    'watertightness': (
        'non-watertight geometry cannot bound a closed acoustic volume',
        'close boundary edges or select a solver not requiring closure',
    ),
}

_SUSPICIOUS_EXTENT_M = 500.0


def _part_bounds(
    mesh: RawVisualMesh,
) -> tuple[float, float, float]:
    xs = [v.x for v in mesh.vertices]
    ys = [v.y for v in mesh.vertices]
    zs = [v.z for v in mesh.vertices]
    return (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


def _entity_ref_of(part: IntakeMeshPart | IntakePartStub) -> tuple[str, ...]:
    if part.owner_kind == 'entity_body':
        return (part.owner_ref,)
    return ()


class _PartView:
    """One part under diagnosis: source mesh or a repaired-mesh view."""

    def __init__(
        self,
        *,
        part_id: str,
        owner_kind: IntakePartKind,
        owner_ref: str,
        role: IntakePartRole,
        mesh: RawVisualMesh | None,
        material_state: IntakeMaterialState,
        material_label: str | None,
        import_authority: MeshImportAuthority | None,
        unavailable_reason: str | None = None,
        diagnostic: Any | None = None,
    ) -> None:
        self.part_id = part_id
        self.owner_kind = owner_kind
        self.owner_ref = owner_ref
        self.role = role
        self.mesh = mesh
        self.material_state = material_state
        self.material_label = material_label
        self.import_authority = import_authority
        self.unavailable_reason = unavailable_reason
        self.diagnostic = diagnostic


def _view_mesh(part_view: _PartView) -> RawVisualMesh:
    assert part_view.mesh is not None
    return part_view.mesh


def _evaluate_part_findings(
    view: _PartView,
    profile: RawMeshDiagnosticProfile,
) -> list[GeometryDefect]:
    mesh = _view_mesh(view)
    if view.diagnostic is not None:
        findings: Iterable[RawMeshDiagnosticFinding] = view.diagnostic.findings
    else:
        result = diagnose_raw_visual_mesh(mesh, profile=profile)
        findings = result.findings
    defects: list[GeometryDefect] = []
    for finding in findings:
        if finding.state == 'pass':
            continue
        mapped = _FINDING_DEFECTS.get(finding.code)
        if mapped is None:
            continue
        kind, severity = mapped
        if finding.state == 'unknown':
            defects.append(
                GeometryDefect.create(
                    kind='diagnostic_evidence_gap',
                    severity='warning',
                    origin='source_model',
                    part_refs=(view.part_id,),
                    entity_refs=_entity_ref_of(view),  # type: ignore[arg-type]
                    evidence={'diagnostic_code': finding.code},
                    consequence=(
                        f'{finding.code} could not be determined: '
                        f'{finding.detail}'
                    ),
                    suggested_action=(
                        'resolve the evidence gap (units, asset bytes) '
                        'and re-run intake diagnostics'
                    ),
                    repairable='operator_required',
                )
            )
            continue
        consequence, action = _FINDING_CONSEQUENCE[finding.code]
        if finding.code == 'open_boundary':
            repairable: GeometryRepairability = 'automatic'
        elif finding.code == 'non_manifold_edge':
            repairable = 'operator_required'
        elif finding.code in ('duplicate_face', 'inverted_normal', 'sliver_face'):
            repairable = 'automatic'
        elif finding.code == 'overlapping_face':
            repairable = 'automatic'
        elif finding.code == 'tiny_feature':
            repairable = 'automatic'
        else:  # watertightness
            repairable = 'operator_required'
        defects.append(
            GeometryDefect.create(
                kind=kind,
                severity=severity,
                origin='source_model',
                part_refs=(view.part_id,),
                entity_refs=_entity_ref_of(view),  # type: ignore[arg-type]
                evidence={
                    'diagnostic_code': finding.code,
                    'count': finding.count,
                    'detail': finding.detail,
                },
                consequence=consequence,
                suggested_action=action,
                repairable=repairable,
            )
        )
    return defects


def _evaluate_component_defects(view: _PartView) -> list[GeometryDefect]:
    mesh = _view_mesh(view)
    inventory = mesh_component_inventory(mesh)
    if len(inventory.components) <= 1:
        return []
    largest = max(
        inventory.components, key=lambda component: component.face_count
    )
    fragments = [
        component.component_index
        for component in inventory.components
        if component.component_index != largest.component_index
    ]
    return [
        GeometryDefect.create(
            kind='disconnected_regions',
            severity='warning',
            origin='source_model',
            part_refs=(view.part_id,),
            entity_refs=_entity_ref_of(view),  # type: ignore[arg-type]
            evidence={
                'component_count': len(inventory.components),
                'largest_component_faces': largest.face_count,
                'fragment_component_indices': fragments,
                'component_face_counts': [
                    component.face_count
                    for component in inventory.components
                ],
            },
            consequence=(
                'disconnected fragments may be scan noise or distinct '
                'bodies; solvers treat them as one part'
            ),
            suggested_action=(
                'remove fragment components or split the part upstream'
            ),
            repairable='automatic',
        )
    ]


def _evaluate_unit_defects(view: _PartView) -> list[GeometryDefect]:
    defects: list[GeometryDefect] = []
    authority = view.import_authority
    if authority is not None and (
        authority.unit_declared_by == 'undeclared'
        or authority.source_unit == 'unknown'
    ):
        defects.append(
            GeometryDefect.create(
                kind='coordinate_unit_anomaly',
                severity='warning',
                origin='source_model',
                part_refs=(view.part_id,),
                entity_refs=_entity_ref_of(view),  # type: ignore[arg-type]
                evidence={'unit_state': 'undeclared'},
                consequence=(
                    'coordinates have no declared unit authority; metric '
                    'extent claims are unverifiable'
                ),
                suggested_action=(
                    'operator-declare the source unit and scale to meters'
                ),
                repairable='operator_required',
            )
        )
    elif authority is not None and (
        authority.unit_declared_by == 'legacy_assumed_meter'
    ):
        defects.append(
            GeometryDefect.create(
                kind='coordinate_unit_anomaly',
                severity='info',
                origin='source_model',
                part_refs=(view.part_id,),
                entity_refs=_entity_ref_of(view),  # type: ignore[arg-type]
                evidence={'unit_state': 'legacy_assumed'},
                consequence=(
                    'mesh predates explicit import authority; meters are '
                    'assumed, not declared'
                ),
                suggested_action=(
                    'operator-confirm the source unit if it is not meters'
                ),
                repairable='operator_required',
            )
        )
    mesh = view.mesh
    if mesh is not None and mesh.vertices:
        extents = _part_bounds(mesh)
        max_extent = max(extents)
        if max_extent > _SUSPICIOUS_EXTENT_M:
            defects.append(
                GeometryDefect.create(
                    kind='coordinate_unit_anomaly',
                    severity='warning',
                    origin='source_model',
                    part_refs=(view.part_id,),
                    entity_refs=_entity_ref_of(view),  # type: ignore[arg-type]
                    evidence={
                        'unit_state': 'suspicious_extent',
                        'max_extent_m': max_extent,
                    },
                    consequence=(
                        f'part extent {max_extent:g} m is implausible for '
                        'room acoustics; likely a unit confusion'
                    ),
                    suggested_action=(
                        'verify the declared source unit and re-import if '
                        'the scale is wrong'
                    ),
                    repairable='operator_required',
                )
            )
    return defects


def _evaluate_material_defects(view: _PartView) -> list[GeometryDefect]:
    if view.material_state == 'assigned':
        return []
    return [
        GeometryDefect.create(
            kind='material_assignment_gap',
            severity='warning',
            origin='source_model',
            part_refs=(view.part_id,),
            entity_refs=_entity_ref_of(view),  # type: ignore[arg-type]
            evidence={'material_state': view.material_state},
            consequence=(
                'surface has no material assignment; solvers requiring '
                'boundary materials cannot treat it'
            ),
            suggested_action='assign an acoustic surface material',
            repairable='operator_required',
        )
    ]


def _evaluate_opening_defects(
    openings: Sequence[IntakeOpening],
) -> list[GeometryDefect]:
    defects: list[GeometryDefect] = []
    for opening in openings:
        if opening.resolution != 'unresolved':
            continue
        if not opening.is_open:
            continue
        defects.append(
            GeometryDefect.create(
                kind='portal_opening_ambiguity',
                severity='warning',
                origin='source_model',
                part_refs=(opening.host_part_id,),
                opening_refs=(opening.opening_id,),
                evidence={
                    'opening_kind': opening.kind,
                    'area_m2': opening.area_m2,
                    'is_open': True,
                },
                consequence=(
                    'opening is declared open but the boundary geometry '
                    'encloses it; solvers need an explicit portal state'
                ),
                suggested_action=(
                    'resolve as an open portal or as a closed boundary'
                ),
                repairable='operator_required',
            )
        )
    return defects


def _evaluate_stub_defects(
    stubs: Sequence[IntakePartStub],
) -> list[GeometryDefect]:
    return [
        GeometryDefect.create(
            kind='geometry_unavailable',
            severity='critical',
            origin='source_model',
            part_refs=(stub.part_id,),
            entity_refs=_entity_ref_of(stub),
            evidence={'reason': stub.unavailability_reason},
            consequence=(
                'geometry could not be resolved; the part is absent from '
                'the evaluated subject'
            ),
            suggested_action=(
                'restore the referenced asset blob or re-import the part'
            ),
            repairable='none',
        )
        for stub in stubs
    ]


def _evaluate_solver_condition_defects(
    subject_openings: Sequence[IntakeOpening],
    solver_context: SolverGeometryContext | None,
) -> list[GeometryDefect]:
    if solver_context is None:
        return []
    defects: list[GeometryDefect] = []
    open_portals = [
        opening
        for opening in subject_openings
        if opening.is_open and opening.resolution in ('unresolved', 'resolved_open')
    ]
    if open_portals and solver_context.open_portals_unsupported:
        defects.append(
            GeometryDefect.create(
                kind='solver_unsupported_condition',
                severity='critical',
                origin='solver_limitation',
                part_refs=tuple(
                    sorted({o.host_part_id for o in open_portals})
                ),
                opening_refs=tuple(o.opening_id for o in open_portals),
                evidence={
                    'condition': 'open_portals',
                    'open_portal_count': len(open_portals),
                },
                consequence=(
                    'the selected solver cannot model open portal '
                    'coupling on this geometry'
                ),
                suggested_action=(
                    'resolve portals as closed boundary or select a '
                    'solver with portal coupling support'
                ),
                repairable='operator_required',
            )
        )
    return defects


def _evaluate_defects(
    part_views: Sequence[_PartView],
    stubs: Sequence[IntakePartStub],
    openings: Sequence[IntakeOpening],
    solver_context: SolverGeometryContext | None,
    profile: RawMeshDiagnosticProfile | None = None,
) -> tuple[GeometryDefect, ...]:
    profile = profile or RawMeshDiagnosticProfile()
    defects: list[GeometryDefect] = []
    for view in part_views:
        if view.mesh is None:
            continue
        defects.extend(_evaluate_part_findings(view, profile))
        defects.extend(_evaluate_component_defects(view))
        defects.extend(_evaluate_unit_defects(view))
        defects.extend(_evaluate_material_defects(view))
    defects.extend(_evaluate_stub_defects(stubs))
    defects.extend(_evaluate_opening_defects(openings))
    defects.extend(_evaluate_solver_condition_defects(openings, solver_context))
    return tuple(defects)


# --- sealed records ------------------------------------------------------------


class GeometryIntakeReport(BaseModel):
    """Sealed health-check output for one intake subject (``gdr-``)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    report_id: str = Field(pattern=r'^gdr-[0-9a-f]{24}$')
    report_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority_version: Literal['1'] = GEOMETRY_INTAKE_AUTHORITY_VERSION
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    subject_sha256: str = Field(pattern=_SHA256_PATTERN)
    solver_context: SolverGeometryContext | None = None
    defects: tuple[GeometryDefect, ...]
    defect_count: int = Field(ge=0)
    critical_count: int = Field(ge=0)
    evaluator_id: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_report(self) -> 'GeometryIntakeReport':
        if self.subject_ref.kind != 'geometry_intake_subject':
            raise ValueError('report subject_ref must be geometry_intake_subject')
        if self.subject_ref.ref_sha256 != self.subject_sha256:
            raise ValueError('report subject_ref must pin subject sha256')
        if self.defect_count != len(self.defects):
            raise ValueError('defect_count must equal len(defects)')
        if self.critical_count != sum(
            1 for d in self.defects if d.severity == 'critical'
        ):
            raise ValueError('critical_count must match defects')
        ids = [d.defect_id for d in self.defects]
        if len(ids) != len(set(ids)):
            raise ValueError('defect ids must be unique within a report')
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        expected = _hash(self.identity_payload())
        if self.report_sha256 != expected:
            raise ValueError('report sha256 mismatch')
        if self.report_id != _semantic_id('gdr', expected):
            raise ValueError('report id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'report_id', 'report_sha256'}
        )

    def defects_of_kind(
        self, kind: GeometryDefectKind
    ) -> tuple[GeometryDefect, ...]:
        return tuple(d for d in self.defects if d.kind == kind)

    @classmethod
    def create(cls, **payload: Any) -> 'GeometryIntakeReport':
        return _seal(
            cls, payload, 'report_id', 'report_sha256', 'gdr'
        )


def diagnose_geometry_intake(
    subject: GeometryIntakeSubject,
    *,
    evaluated_at_utc: str,
    solver_context: SolverGeometryContext | None = None,
    profile: RawMeshDiagnosticProfile | None = None,
) -> GeometryIntakeReport:
    """HEALTH_CHECK: evaluate every defect class over the subject."""
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    views = [
        _PartView(
            part_id=part.part_id,
            owner_kind=part.owner_kind,
            owner_ref=part.owner_ref,
            role=part.role,
            mesh=part.mesh,
            material_state=part.material_state,
            material_label=part.material_label,
            import_authority=part.import_authority,
        )
        for part in subject.parts
    ]
    defects = _evaluate_defects(
        views, subject.unresolved_parts, subject.openings, solver_context, profile
    )
    return GeometryIntakeReport.create(
        authority_version=GEOMETRY_INTAKE_AUTHORITY_VERSION,
        document_id=subject.document_id,
        subject_ref=AuthorityRef(
            kind='geometry_intake_subject',
            ref_id=subject.subject_id,
            ref_sha256=subject.subject_sha256,
        ),
        subject_sha256=subject.subject_sha256,
        solver_context=solver_context,
        defects=defects,
        defect_count=len(defects),
        critical_count=sum(1 for d in defects if d.severity == 'critical'),
        evaluator_id=GEOMETRY_INTAKE_EVALUATOR_ID,
        evaluator_version=GEOMETRY_INTAKE_EVALUATOR_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )


# --- repair proposals -----------------------------------------------------------


class GeometryRepairAction(BaseModel):
    """One proposed repair step. Never applied until explicitly accepted."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    action_id: str = Field(pattern=r'^gax-[0-9a-f]{24}$')
    kind: GeometryRepairActionKind
    automation: RepairAutomation
    target_part_id: str | None = None
    target_opening_id: str | None = None
    target_defect_ids: tuple[str, ...]
    proposed_parameters: dict[str, Any] = Field(default_factory=dict)
    description: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_action(self) -> 'GeometryRepairAction':
        expected = _semantic_id('gax', _hash(self.action_payload()))
        if self.action_id != expected:
            raise ValueError('repair action id does not match its payload')
        return self

    def action_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'action_id'})

    @classmethod
    def create(cls, **payload: Any) -> 'GeometryRepairAction':
        probe = cls.model_construct(**canonicalize_payload(cls, dict(payload)))
        digest = _hash(probe.action_payload())
        return cls(**payload, action_id=_semantic_id('gax', digest))


class GeometryRepairProposal(BaseModel):
    """Sealed repair proposal for one report (``grp-``)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    proposal_id: str = Field(pattern=r'^grp-[0-9a-f]{24}$')
    proposal_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority_version: Literal['1'] = GEOMETRY_INTAKE_AUTHORITY_VERSION
    document_id: str = Field(min_length=1)
    report_ref: AuthorityRef
    subject_ref: AuthorityRef
    actions: tuple[GeometryRepairAction, ...]
    generated_by: str = Field(min_length=1)
    generated_at_utc: str = Field(min_length=1)
    proposal_reason: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_proposal(self) -> 'GeometryRepairProposal':
        if self.report_ref.kind != 'geometry_intake_report':
            raise ValueError('proposal report_ref must be geometry_intake_report')
        if self.subject_ref.kind != 'geometry_intake_subject':
            raise ValueError('proposal subject_ref must be geometry_intake_subject')
        _require_refs(self.report_ref, self.subject_ref)
        action_ids = [a.action_id for a in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError('proposal action ids must be unique')
        _require_iso8601(self.generated_at_utc, 'generated_at_utc')
        expected = _hash(self.identity_payload())
        if self.proposal_sha256 != expected:
            raise ValueError('proposal sha256 mismatch')
        if self.proposal_id != _semantic_id('grp', expected):
            raise ValueError('proposal id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'proposal_id', 'proposal_sha256'}
        )

    def action(self, action_id: str) -> GeometryRepairAction:
        for action in self.actions:
            if action.action_id == action_id:
                return action
        raise KeyError(f'unknown repair action {action_id}')

    @classmethod
    def create(cls, **payload: Any) -> 'GeometryRepairProposal':
        return _seal(
            cls, payload, 'proposal_id', 'proposal_sha256', 'grp'
        )


def _mesh_action_kind_operations() -> Mapping[str, type]:
    return {
        'consolidate_exact_duplicate_vertices': ExactDuplicateVertexConsolidation,
        'weld_vertices_within_tolerance': ToleranceVertexWeld,
        'remove_unreferenced_vertices': RemoveUnreferencedVertices,
        'remove_exact_duplicate_faces': RemoveExactDuplicateFaces,
        'correct_consistent_winding': CorrectConsistentWinding,
        'remove_degenerate_faces': RemoveDegenerateFaces,
    }

_MESH_OP_KINDS = frozenset(_mesh_action_kind_operations().keys()) | {
    'remove_disconnected_fragment'
}


def propose_geometry_repairs(
    report: GeometryIntakeReport,
    subject: GeometryIntakeSubject,
    *,
    proposed_by: str,
    proposed_at_utc: str,
    proposal_reason: str = 'geometry intake repair proposal',
) -> GeometryRepairProposal:
    """REPAIR_PROPOSAL: translate repairable defects into proposed actions.

    Automatic (mesh-level) actions map onto the bounded raw-mesh repair
    operation set; operator-required actions carry the parameter slots the
    acceptance decision must fill.
    """
    _require_nonblank(proposed_by, 'proposed_by')
    _require_iso8601(proposed_at_utc, 'proposed_at_utc')
    if report.subject_ref.ref_sha256 != subject.subject_sha256:
        raise GeometryIntakeError('report does not describe this subject')
    actions: list[GeometryRepairAction] = []
    for defect in report.defects:
        if defect.repairable == 'none':
            continue
        part = defect.part_refs[0] if defect.part_refs else None
        if defect.kind == 'open_boundary_edges':
            actions.append(
                GeometryRepairAction.create(
                    kind='consolidate_exact_duplicate_vertices',
                    automation='automatic',
                    target_part_id=part,
                    target_defect_ids=(defect.defect_id,),
                    description=(
                        'consolidate vertices with exactly equal coordinates'
                    ),
                )
            )
            actions.append(
                GeometryRepairAction.create(
                    kind='weld_vertices_within_tolerance',
                    automation='automatic',
                    target_part_id=part,
                    target_defect_ids=(defect.defect_id,),
                    proposed_parameters={
                        'tolerance_source_units': 1.0e-9,
                    },
                    description=(
                        'weld vertices within the declared tolerance'
                    ),
                )
            )
        elif defect.kind == 'duplicate_or_overlapping_faces':
            actions.append(
                GeometryRepairAction.create(
                    kind='remove_exact_duplicate_faces',
                    automation='automatic',
                    target_part_id=part,
                    target_defect_ids=(defect.defect_id,),
                    description='remove exact duplicate faces',
                )
            )
        elif defect.kind == 'inverted_normals':
            actions.append(
                GeometryRepairAction.create(
                    kind='correct_consistent_winding',
                    automation='automatic',
                    target_part_id=part,
                    target_defect_ids=(defect.defect_id,),
                    description='make triangle winding consistent per component',
                )
            )
        elif defect.kind == 'degenerate_or_tiny_features':
            actions.append(
                GeometryRepairAction.create(
                    kind='remove_degenerate_faces',
                    automation='automatic',
                    target_part_id=part,
                    target_defect_ids=(defect.defect_id,),
                    proposed_parameters={
                        'area_tolerance_source_units_squared': 1.0e-12,
                    },
                    description=(
                        'remove faces at or below the declared area tolerance'
                    ),
                )
            )
        elif defect.kind == 'disconnected_regions':
            for component_index in defect.evidence.get(
                'fragment_component_indices', ()
            ):
                actions.append(
                    GeometryRepairAction.create(
                        kind='remove_disconnected_fragment',
                        automation='automatic',
                        target_part_id=part,
                        target_defect_ids=(defect.defect_id,),
                        proposed_parameters={
                            'component_index': int(component_index),
                        },
                        description=(
                            'remove a non-largest face component of the '
                            'part'
                        ),
                    )
                )
        elif defect.kind == 'material_assignment_gap':
            actions.append(
                GeometryRepairAction.create(
                    kind='assign_material',
                    automation='operator_required',
                    target_part_id=part,
                    target_defect_ids=(defect.defect_id,),
                    proposed_parameters={'material_label': None},
                    description=(
                        'operator assigns the acoustic surface material'
                    ),
                )
            )
        elif defect.kind == 'coordinate_unit_anomaly':
            actions.append(
                GeometryRepairAction.create(
                    kind='declare_units',
                    automation='operator_required',
                    target_part_id=part,
                    target_defect_ids=(defect.defect_id,),
                    proposed_parameters={
                        'source_unit': None,
                        'scale_to_meters': None,
                    },
                    description=(
                        'operator declares the source unit authority'
                    ),
                )
            )
        elif defect.kind == 'portal_opening_ambiguity':
            for opening_id in defect.opening_refs:
                actions.append(
                    GeometryRepairAction.create(
                        kind='resolve_portal',
                        automation='operator_required',
                        target_part_id=part,
                        target_opening_id=opening_id,
                        target_defect_ids=(defect.defect_id,),
                        proposed_parameters={'resolution': None},
                        description=(
                            'operator resolves the opening as open portal '
                            'or closed boundary'
                        ),
                    )
                )
        elif defect.kind == 'non_manifold_edges':
            actions.append(
                GeometryRepairAction.create(
                    kind='no_repair_available',
                    automation='operator_required',
                    target_part_id=part,
                    target_defect_ids=(defect.defect_id,),
                    description=(
                        'non-manifold topology needs upstream remodeling; '
                        'no bounded repair exists'
                    ),
                )
            )
        elif defect.kind in (
            'not_watertight', 'diagnostic_evidence_gap',
            'solver_unsupported_condition',
        ):
            actions.append(
                GeometryRepairAction.create(
                    kind='no_repair_available',
                    automation='operator_required',
                    target_part_id=part,
                    target_defect_ids=(defect.defect_id,),
                    description=(
                        'no automatic repair; resolve the underlying '
                        'condition or choose a different solver'
                    ),
                )
            )
        else:  # pragma: no cover - defect kinds closed
            continue
    return GeometryRepairProposal.create(
        authority_version=GEOMETRY_INTAKE_AUTHORITY_VERSION,
        document_id=report.document_id,
        report_ref=AuthorityRef(
            kind='geometry_intake_report',
            ref_id=report.report_id,
            ref_sha256=report.report_sha256,
        ),
        subject_ref=report.subject_ref,
        actions=tuple(actions),
        generated_by=proposed_by,
        generated_at_utc=proposed_at_utc,
        proposal_reason=proposal_reason,
    )


# --- explicit acceptance -------------------------------------------------------


class RepairActionDecision(BaseModel):
    """One operator decision on one proposed action."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    action_id: str = Field(min_length=1)
    decision: RepairDecisionValue
    decided_by: str = Field(min_length=1)
    decided_at_utc: str = Field(min_length=1)
    accepted_parameters: dict[str, Any] = Field(default_factory=dict)
    decision_reason: str | None = None

    @model_validator(mode='after')
    def validate_decision(self) -> 'RepairActionDecision':
        _require_iso8601(self.decided_at_utc, 'decided_at_utc')
        return self


class GeometryRepairAcceptance(BaseModel):
    """Sealed operator acceptance record (``gra-``)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    acceptance_id: str = Field(pattern=r'^gra-[0-9a-f]{24}$')
    acceptance_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority_version: Literal['1'] = GEOMETRY_INTAKE_AUTHORITY_VERSION
    document_id: str = Field(min_length=1)
    proposal_ref: AuthorityRef
    decisions: tuple[RepairActionDecision, ...]
    accepted_action_ids: tuple[str, ...]
    rejected_action_ids: tuple[str, ...]
    decided_by: str = Field(min_length=1)
    decided_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_acceptance(self) -> 'GeometryRepairAcceptance':
        if self.proposal_ref.kind != 'geometry_repair_proposal':
            raise ValueError(
                'acceptance proposal_ref must be geometry_repair_proposal'
            )
        _require_refs(self.proposal_ref)
        ids = [d.action_id for d in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError('each action may be decided exactly once')
        accepted = tuple(
            d.action_id for d in self.decisions if d.decision == 'accepted'
        )
        rejected = tuple(
            d.action_id for d in self.decisions if d.decision == 'rejected'
        )
        if self.accepted_action_ids != accepted:
            raise ValueError('accepted_action_ids must match decisions')
        if self.rejected_action_ids != rejected:
            raise ValueError('rejected_action_ids must match decisions')
        _require_iso8601(self.decided_at_utc, 'decided_at_utc')
        expected = _hash(self.identity_payload())
        if self.acceptance_sha256 != expected:
            raise ValueError('acceptance sha256 mismatch')
        if self.acceptance_id != _semantic_id('gra', expected):
            raise ValueError('acceptance id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'acceptance_id', 'acceptance_sha256'}
        )

    @classmethod
    def create(cls, **payload: Any) -> 'GeometryRepairAcceptance':
        return _seal(
            cls, payload, 'acceptance_id', 'acceptance_sha256', 'gra'
        )


_OPERATOR_PARAMETER_REQUIREMENTS: Mapping[str, tuple[str, ...]] = {
    'assign_material': ('material_label',),
    'declare_units': ('source_unit', 'scale_to_meters'),
    'resolve_portal': ('resolution',),
    'remove_disconnected_fragment': ('component_index',),
    'weld_vertices_within_tolerance': (),
    'remove_degenerate_faces': (),
}

_ALLOWED_SOURCE_UNITS = frozenset({
    'meters', 'millimeters', 'centimeters', 'inches', 'feet',
})


def _validate_decision_parameters(
    action: GeometryRepairAction,
    decision: RepairActionDecision,
) -> None:
    if decision.decision != 'accepted':
        return
    required = _OPERATOR_PARAMETER_REQUIREMENTS.get(action.kind, ())
    supplied = dict(action.proposed_parameters)
    supplied.update(decision.accepted_parameters)
    for key in required:
        if supplied.get(key) in (None, ''):
            raise GeometryRepairAcceptanceError(
                f'accepted {action.kind} action requires parameter {key}'
            )
    if action.kind == 'declare_units':
        unit = supplied.get('source_unit')
        if unit not in _ALLOWED_SOURCE_UNITS:
            raise GeometryRepairAcceptanceError(
                f'declare_units source_unit must be one of '
                f'{sorted(_ALLOWED_SOURCE_UNITS)}'
            )
        scale = supplied.get('scale_to_meters')
        if (
            not isinstance(scale, (int, float))
            or not isfinite(float(scale))
            or float(scale) <= 0
        ):
            raise GeometryRepairAcceptanceError(
                'declare_units scale_to_meters must be a positive number'
            )
    if action.kind == 'resolve_portal':
        if supplied.get('resolution') not in ('open_portal', 'closed_boundary'):
            raise GeometryRepairAcceptanceError(
                "resolve_portal resolution must be 'open_portal' or "
                "'closed_boundary'"
            )
    if action.kind == 'remove_disconnected_fragment':
        index = supplied.get('component_index')
        if not isinstance(index, int) or index < 0:
            raise GeometryRepairAcceptanceError(
                'remove_disconnected_fragment requires a non-negative '
                'component_index'
            )


def record_geometry_repair_acceptance(
    proposal: GeometryRepairProposal,
    decisions: Sequence[RepairActionDecision],
    *,
    decided_by: str,
    decided_at_utc: str,
) -> GeometryRepairAcceptance:
    """EXPLICIT_ACCEPTANCE: build the sealed decision record.

    Every proposed action must be decided exactly once; accepted
    operator-required actions must supply their required parameters.
    """
    _require_nonblank(decided_by, 'decided_by')
    _require_iso8601(decided_at_utc, 'decided_at_utc')
    proposal_ids = {action.action_id for action in proposal.actions}
    decision_ids = [decision.action_id for decision in decisions]
    unknown = sorted(set(decision_ids) - proposal_ids)
    if unknown:
        raise GeometryRepairAcceptanceError(
            f'decisions reference actions outside the proposal: {unknown}'
        )
    undecided = sorted(proposal_ids - set(decision_ids))
    if undecided:
        raise GeometryRepairAcceptanceError(
            f'proposal actions left undecided: {undecided}'
        )
    for decision in decisions:
        action = proposal.action(decision.action_id)
        _validate_decision_parameters(action, decision)
    return GeometryRepairAcceptance.create(
        authority_version=GEOMETRY_INTAKE_AUTHORITY_VERSION,
        document_id=proposal.document_id,
        proposal_ref=AuthorityRef(
            kind='geometry_repair_proposal',
            ref_id=proposal.proposal_id,
            ref_sha256=proposal.proposal_sha256,
        ),
        decisions=tuple(decisions),
        accepted_action_ids=tuple(
            d.action_id for d in decisions if d.decision == 'accepted'
        ),
        rejected_action_ids=tuple(
            d.action_id for d in decisions if d.decision == 'rejected'
        ),
        decided_by=decided_by,
        decided_at_utc=decided_at_utc,
    )


# --- derived geometry revision ---------------------------------------------------


class DerivedGeometryPart(BaseModel):
    """One part inside a derived geometry revision.

    ``geometry_state`` is exactly one of:

    * ``unchanged_source`` — the part carries its source mesh forward;
    * ``repaired_mesh`` — bounded raw-mesh repair output
      (:class:`RepairedRawMesh`) with the full plan/apply/lineage chain;
    * ``replaced_mesh`` — a deterministic subject-level rebuild (e.g.
      fragment removal) recorded as a fresh content-addressed
      :class:`RawVisualMesh`.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    part_id: str = Field(min_length=1)
    owner_kind: IntakePartKind
    owner_ref: str = Field(min_length=1)
    role: IntakePartRole
    geometry_state: Literal[
        'unchanged_source', 'repaired_mesh', 'replaced_mesh'
    ]
    source_part_id: str = Field(min_length=1)
    source_mesh_id: str = Field(min_length=1)
    repaired_mesh: RepairedRawMesh | None = None
    replaced_mesh: RawVisualMesh | None = None
    repair_lineage: RawMeshRepairLineageRef | None = None
    material_state: IntakeMaterialState
    material_label: str | None = None
    import_authority: MeshImportAuthority | None = None

    @model_validator(mode='after')
    def consistent(self) -> 'DerivedGeometryPart':
        if self.geometry_state == 'repaired_mesh':
            if self.repaired_mesh is None:
                raise ValueError('repaired_mesh state requires repaired_mesh')
        elif self.repaired_mesh is not None:
            raise ValueError(
                'repaired_mesh is only valid for repaired_mesh state'
            )
        if self.geometry_state == 'replaced_mesh':
            if self.replaced_mesh is None:
                raise ValueError('replaced_mesh state requires replaced_mesh')
        elif self.replaced_mesh is not None:
            raise ValueError(
                'replaced_mesh is only valid for replaced_mesh state'
            )
        if self.material_state == 'assigned' and not self.material_label:
            raise ValueError('assigned material state requires a label')
        if self.repair_lineage is not None and (
            self.geometry_state != 'repaired_mesh'
        ):
            raise ValueError(
                'repair_lineage is only valid for repaired_mesh state'
            )
        return self


class DerivedGeometryRevision(BaseModel):
    """Immutable derived geometry produced by accepted repairs (``gdv-``).

    Provenance chain: source subject -> report -> proposal -> acceptance
    -> this revision. ``derived_geometry_sha256`` is the content hash the
    readiness verdict binds; any upstream change makes it differ.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    derived_revision_id: str = Field(pattern=r'^gdv-[0-9a-f]{24}$')
    derived_revision_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority_version: Literal['1'] = GEOMETRY_INTAKE_AUTHORITY_VERSION
    document_id: str = Field(min_length=1)
    source_subject_ref: AuthorityRef
    source_report_ref: AuthorityRef
    proposal_ref: AuthorityRef
    acceptance_ref: AuthorityRef
    applied_action_ids: tuple[str, ...]
    rejected_action_ids: tuple[str, ...]
    parts: tuple[DerivedGeometryPart, ...]
    openings: tuple[IntakeOpening, ...]
    unit_state: IntakeUnitState
    residual_defects: tuple[GeometryDefect, ...]
    derived_geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    created_by: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_revision(self) -> 'DerivedGeometryRevision':
        if self.source_subject_ref.kind != 'geometry_intake_subject':
            raise ValueError('source_subject_ref kind mismatch')
        if self.source_report_ref.kind != 'geometry_intake_report':
            raise ValueError('source_report_ref kind mismatch')
        if self.proposal_ref.kind != 'geometry_repair_proposal':
            raise ValueError('proposal_ref kind mismatch')
        if self.acceptance_ref.kind != 'geometry_repair_acceptance':
            raise ValueError('acceptance_ref kind mismatch')
        _require_refs(
            self.source_subject_ref,
            self.source_report_ref,
            self.proposal_ref,
            self.acceptance_ref,
        )
        part_ids = [part.part_id for part in self.parts]
        if len(part_ids) != len(set(part_ids)):
            raise ValueError('derived part ids must be unique')
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        expected_geometry = self.geometry_identity_hash()
        if self.derived_geometry_sha256 != expected_geometry:
            raise ValueError('derived_geometry_sha256 mismatch')
        expected = _hash(self.identity_payload())
        if self.derived_revision_sha256 != expected:
            raise ValueError('derived revision sha256 mismatch')
        if self.derived_revision_id != _semantic_id('gdv', expected):
            raise ValueError('derived revision id mismatch')
        return self

    def geometry_payload(self) -> dict[str, Any]:
        """The canonical geometry content the readiness verdict binds."""
        return {
            'parts': [part.model_dump(mode='json') for part in self.parts],
            'openings': [
                opening.model_dump(mode='json') for opening in self.openings
            ],
            'unit_state': self.unit_state,
        }

    def geometry_identity_hash(self) -> str:
        return _hash(self.geometry_payload())

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'derived_revision_id', 'derived_revision_sha256'},
        )

    def subject_ref(self) -> AuthorityRef:
        return AuthorityRef(
            kind='derived_geometry_revision',
            ref_id=self.derived_revision_id,
            ref_sha256=self.derived_revision_sha256,
        )

    def defects_of_kind(
        self, kind: GeometryDefectKind
    ) -> tuple[GeometryDefect, ...]:
        return tuple(d for d in self.residual_defects if d.kind == kind)

    @classmethod
    def create(cls, **payload: Any) -> 'DerivedGeometryRevision':
        geometry_hash = _hash({
            'parts': [
                p.model_dump(mode='json') for p in payload['parts']
            ],
            'openings': [
                o.model_dump(mode='json') for o in payload['openings']
            ],
            'unit_state': payload['unit_state'],
        })
        payload['derived_geometry_sha256'] = geometry_hash
        return _seal(
            cls,
            payload,
            'derived_revision_id',
            'derived_revision_sha256',
            'gdv',
        )


def _view_from_derived_part(
    part: DerivedGeometryPart,
    source_part: IntakeMeshPart,
    profile: RawMeshDiagnosticProfile,
) -> _PartView:
    if part.geometry_state == 'repaired_mesh':
        assert part.repaired_mesh is not None
        view_mesh = source_part.mesh.model_copy(
            update={
                'vertices': part.repaired_mesh.vertices,
                'triangles': part.repaired_mesh.triangles,
            }
        )
    elif part.geometry_state == 'replaced_mesh':
        view_mesh = part.replaced_mesh
    else:
        view_mesh = source_part.mesh
    return _PartView(
        part_id=part.part_id,
        owner_kind=part.owner_kind,
        owner_ref=part.owner_ref,
        role=part.role,
        mesh=view_mesh,
        material_state=part.material_state,
        material_label=part.material_label,
        import_authority=part.import_authority,
    )


def _drop_mesh_components(
    mesh: RawVisualMesh,
    component_indices: Sequence[int],
) -> RawVisualMesh:
    """Deterministically drop face components; produces a new content-
    addressed mesh through the canonical meshbin importer."""
    inventory = mesh_component_inventory(mesh)
    index_set = set(component_indices)
    if any(index >= len(inventory.components) for index in index_set):
        raise GeometryIntakeError(
            'component_index exceeds the part component inventory'
        )
    # The inventory only reports per-component counts, so recompute face
    # membership deterministically with the same shared-edge union.
    parent = list(range(len(mesh.triangles)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    edge_owner: dict[tuple[int, int], int] = {}
    for index, tri in enumerate(mesh.triangles):
        for start, end in (
            (tri.a, tri.b), (tri.b, tri.c), (tri.c, tri.a),
        ):
            key = tuple(sorted((start, end)))
            if key in edge_owner:
                ri, rj = find(index), find(edge_owner[key])
                if ri != rj:
                    parent[rj] = ri
            else:
                edge_owner[key] = index
    components: dict[int, list[int]] = {}
    for index in range(len(mesh.triangles)):
        components.setdefault(find(index), []).append(index)
    ordered = [
        sorted(component) for _, component in sorted(
            components.items(), key=lambda item: min(item[1])
        )
    ]
    drop_faces: set[int] = set()
    for index in index_set:
        drop_faces.update(ordered[index])
    kept = [
        triangle
        for position, triangle in enumerate(mesh.triangles)
        if position not in drop_faces
    ]
    if not kept:
        raise GeometryIntakeError(
            'component removal would delete every face of the part'
        )
    used: dict[int, int] = {}
    new_vertices: list[tuple[float, float, float]] = []
    new_triangles: list[tuple[int, int, int]] = []
    for tri in kept:
        mapped = []
        for index in (tri.a, tri.b, tri.c):
            if index not in used:
                vertex = mesh.vertices[index]
                used[index] = len(new_vertices)
                new_vertices.append((vertex.x, vertex.y, vertex.z))
            mapped.append(used[index])
        new_triangles.append((mapped[0], mapped[1], mapped[2]))
    return _mesh_via_meshbin(
        new_vertices,
        new_triangles,
        source_name=mesh.provenance.source_name + ':fragment-removed',
    )


def derive_geometry_revision(
    subject: GeometryIntakeSubject,
    report: GeometryIntakeReport,
    proposal: GeometryRepairProposal,
    acceptance: GeometryRepairAcceptance,
    *,
    derived_by: str,
    derived_at_utc: str,
    solver_context: SolverGeometryContext | None = None,
    profile: RawMeshDiagnosticProfile | None = None,
) -> DerivedGeometryRevision:
    """DERIVED_GEOMETRY_REVISION: apply the *accepted* actions to a new
    immutable geometry record; the source subject is never mutated.

    Mesh-level actions are applied through the bounded raw-mesh repair
    operations (plan -> apply -> re-diagnose -> lineage). Subject-level
    actions (material, units, portals) update the corresponding intake
    fields. Rejected actions leave their defects unresolved; the derived
    state is then re-diagnosed so ``residual_defects`` is always honest.
    """
    _require_nonblank(derived_by, 'derived_by')
    _require_iso8601(derived_at_utc, 'derived_at_utc')
    if report.subject_ref.ref_sha256 != subject.subject_sha256:
        raise GeometryIntakeError('report does not describe this subject')
    if proposal.report_ref.ref_sha256 != report.report_sha256:
        raise GeometryIntakeError('proposal does not describe this report')
    if acceptance.proposal_ref.ref_sha256 != proposal.proposal_sha256:
        raise GeometryIntakeError(
            'acceptance does not describe this proposal'
        )
    profile = profile or RawMeshDiagnosticProfile()

    accepted: dict[str, RepairActionDecision] = {
        d.action_id: d for d in acceptance.decisions if d.decision == 'accepted'
    }

    derived_parts: list[DerivedGeometryPart] = []
    openings = list(subject.openings)
    unit_state = subject.unit_state

    for source_part in subject.parts:
        part_actions = [
            proposal.action(action_id)
            for action_id, decision in accepted.items()
            if proposal.action(action_id).target_part_id == source_part.part_id
        ]
        material_state = source_part.material_state
        material_label = source_part.material_label
        import_authority = source_part.import_authority
        repaired_mesh: RepairedRawMesh | None = None
        lineage: RawMeshRepairLineageRef | None = None

        mesh_ops = [
            action for action in part_actions
            if action.kind in _MESH_OP_KINDS
        ]
        subject_ops = [
            action for action in part_actions
            if action.kind not in _MESH_OP_KINDS
        ]

        # fragment removal runs first (it changes face indexing for the
        # bounded operation stack) and produces a ``replaced_mesh``; the
        # bounded raw-mesh operation stack then runs on top, producing a
        # ``repaired_mesh`` with the full plan/apply/lineage chain.
        removal = [
            a for a in mesh_ops if a.kind == 'remove_disconnected_fragment'
        ]
        bounded = [
            a for a in mesh_ops if a.kind != 'remove_disconnected_fragment'
        ]
        working_mesh = source_part.mesh
        replaced_mesh: RawVisualMesh | None = None
        for action in removal:
            decision = accepted[action.action_id]
            params = dict(action.proposed_parameters)
            params.update(decision.accepted_parameters)
            working_mesh = _drop_mesh_components(
                working_mesh, (int(params['component_index']),)
            )
            replaced_mesh = working_mesh
        if bounded:
            operations = []
            for action in bounded:
                decision = accepted[action.action_id]
                params = dict(action.proposed_parameters)
                params.update(decision.accepted_parameters)
                if action.kind == 'weld_vertices_within_tolerance':
                    operations.append(
                        ToleranceVertexWeld(
                            tolerance_source_units=float(
                                params['tolerance_source_units']
                            )
                        )
                    )
                elif action.kind == 'consolidate_exact_duplicate_vertices':
                    operations.append(ExactDuplicateVertexConsolidation())
                elif action.kind == 'remove_unreferenced_vertices':
                    operations.append(RemoveUnreferencedVertices())
                elif action.kind == 'remove_exact_duplicate_faces':
                    operations.append(RemoveExactDuplicateFaces())
                elif action.kind == 'correct_consistent_winding':
                    operations.append(CorrectConsistentWinding())
                elif action.kind == 'remove_degenerate_faces':
                    operations.append(
                        RemoveDegenerateFaces(
                            area_tolerance_source_units_squared=float(
                                params[
                                    'area_tolerance_source_units_squared'
                                ]
                            )
                        )
                    )
            if operations:
                working_diagnostic = diagnose_raw_visual_mesh(
                    working_mesh, profile=profile
                )
                plan = make_raw_mesh_repair_plan(
                    working_mesh,
                    working_diagnostic,
                    operations=tuple(operations),
                    requested_by='explicit_user_selected',
                    request_reason=(
                        f'accepted via {acceptance.acceptance_id} by '
                        f'{acceptance.decided_by}'
                    ),
                )
                repaired = apply_raw_mesh_repair(
                    working_mesh, working_diagnostic, plan
                )
                post = diagnose_repaired_raw_mesh(
                    working_mesh, repaired, profile=profile
                )
                repaired_mesh = repaired
                lineage = make_raw_mesh_repair_lineage_ref(repaired, post)
        for action in subject_ops:
            decision = accepted[action.action_id]
            params = dict(action.proposed_parameters)
            params.update(decision.accepted_parameters)
            if action.kind == 'assign_material':
                material_state = 'assigned'
                material_label = str(params['material_label'])
            elif action.kind == 'declare_units':
                import_authority = MeshImportAuthority(
                    source_unit=params['source_unit'],
                    unit_declared_by='operator_confirmed',
                    importer_id='htdt.geometry_intake',
                    importer_version=GEOMETRY_INTAKE_EVALUATOR_VERSION,
                )
                unit_state = 'declared'
            elif action.kind == 'resolve_portal':
                opening_id = action.target_opening_id
                resolution = params['resolution']
                openings = [
                    opening.model_copy(
                        update={
                            'resolution': (
                                'resolved_open'
                                if resolution == 'open_portal'
                                else 'resolved_closed'
                            ),
                            'is_open': resolution == 'open_portal',
                        }
                    )
                    if opening.opening_id == opening_id
                    else opening
                    for opening in openings
                ]
            elif action.kind == 'no_repair_available':
                pass  # accepted acknowledgment; nothing applied

        derived_parts.append(
            DerivedGeometryPart(
                part_id=source_part.part_id,
                owner_kind=source_part.owner_kind,
                owner_ref=source_part.owner_ref,
                role=source_part.role,
                geometry_state=(
                    'repaired_mesh' if repaired_mesh is not None
                    else 'replaced_mesh' if replaced_mesh is not None
                    else 'unchanged_source'
                ),
                source_part_id=source_part.part_id,
                source_mesh_id=source_part.mesh.mesh_id,
                repaired_mesh=repaired_mesh,
                replaced_mesh=replaced_mesh,
                repair_lineage=lineage,
                material_state=material_state,
                material_label=material_label,
                import_authority=import_authority,
            )
        )

    # re-diagnose the derived state ------------------------------------
    views = [
        _view_from_derived_part(
            part, subject.part(part.source_part_id), profile
        )
        for part in derived_parts
    ]
    residual = _evaluate_defects(
        views, subject.unresolved_parts, openings, solver_context, profile
    )
    return DerivedGeometryRevision.create(
        authority_version=GEOMETRY_INTAKE_AUTHORITY_VERSION,
        document_id=subject.document_id,
        source_subject_ref=AuthorityRef(
            kind='geometry_intake_subject',
            ref_id=subject.subject_id,
            ref_sha256=subject.subject_sha256,
        ),
        source_report_ref=AuthorityRef(
            kind='geometry_intake_report',
            ref_id=report.report_id,
            ref_sha256=report.report_sha256,
        ),
        proposal_ref=AuthorityRef(
            kind='geometry_repair_proposal',
            ref_id=proposal.proposal_id,
            ref_sha256=proposal.proposal_sha256,
        ),
        acceptance_ref=AuthorityRef(
            kind='geometry_repair_acceptance',
            ref_id=acceptance.acceptance_id,
            ref_sha256=acceptance.acceptance_sha256,
        ),
        applied_action_ids=acceptance.accepted_action_ids,
        rejected_action_ids=acceptance.rejected_action_ids,
        parts=tuple(derived_parts),
        openings=tuple(openings),
        unit_state=unit_state,
        residual_defects=residual,
        derived_geometry_sha256='0' * 64,  # overwritten by create()
        created_by=derived_by,
        created_at_utc=derived_at_utc,
    )


# --- solver readiness ------------------------------------------------------------


class ReadinessReason(BaseModel):
    """One machine-readable reason contributing to a verdict."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    origin: GeometryDefectOrigin
    defect_id: str | None = None
    defect_kind: GeometryDefectKind | None = None
    phenomenon: str | None = None
    text: str = Field(min_length=1)


class GeometrySolverReadinessVerdict(BaseModel):
    """Sealed solver-readiness verdict (``srv-``).

    ``verdict`` is ``supported`` / ``degraded`` / ``unsupported`` /
    ``unknown`` against the *exact* geometry hash and adapter descriptor
    it binds. Evidence state can be re-checked cheaply via
    :func:`readiness_evidence_state`.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    verdict_id: str = Field(pattern=r'^srv-[0-9a-f]{24}$')
    verdict_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority_version: Literal['1'] = GEOMETRY_INTAKE_AUTHORITY_VERSION
    document_id: str = Field(min_length=1)
    geometry_ref: AuthorityRef
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    report_ref: AuthorityRef
    adapter_descriptor_ref: AuthorityRef
    capability_manifest_ref: AuthorityRef | None = None
    solver_context: SolverGeometryContext
    verdict: SolverReadinessState
    blocking_reasons: tuple[ReadinessReason, ...]
    degraded_reasons: tuple[ReadinessReason, ...]
    evidence_gaps: tuple[ReadinessReason, ...]
    unresolved_defect_ids: tuple[str, ...]
    evaluated_at_utc: str = Field(min_length=1)
    evaluator_id: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_verdict(self) -> 'GeometrySolverReadinessVerdict':
        if self.geometry_ref.kind not in (
            'geometry_intake_subject', 'derived_geometry_revision'
        ):
            raise ValueError('geometry_ref kind mismatch')
        if self.geometry_ref.kind == 'geometry_intake_subject' and (
            self.geometry_ref.ref_sha256 != self.geometry_sha256
        ):
            raise ValueError(
                'geometry_ref must pin geometry_sha256 for a subject'
            )
        # for a derived_geometry_revision the ref_sha256 pins the revision
        # RECORD seal while geometry_sha256 pins the geometry content —
        # both are bound into the verdict so either tampering breaks it.
        if self.report_ref.kind != 'geometry_intake_report':
            raise ValueError('report_ref kind mismatch')
        if self.adapter_descriptor_ref.kind != 'solver_adapter_descriptor':
            raise ValueError('adapter_descriptor_ref kind mismatch')
        _require_refs(
            self.geometry_ref, self.report_ref, self.adapter_descriptor_ref,
            self.capability_manifest_ref,
        )
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        if self.verdict == 'supported' and (
            self.blocking_reasons or self.evidence_gaps or self.degraded_reasons
        ):
            raise ValueError('supported verdict cannot carry reasons')
        if self.verdict == 'unsupported' and not self.blocking_reasons:
            raise ValueError('unsupported verdict requires blocking reasons')
        if self.verdict == 'unknown' and not self.evidence_gaps:
            raise ValueError('unknown verdict requires evidence gaps')
        if self.verdict == 'degraded' and not self.degraded_reasons:
            raise ValueError('degraded verdict requires degraded reasons')
        expected = _hash(self.identity_payload())
        if self.verdict_sha256 != expected:
            raise ValueError('verdict sha256 mismatch')
        if self.verdict_id != _semantic_id('srv', expected):
            raise ValueError('verdict id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'verdict_id', 'verdict_sha256'}
        )

    @classmethod
    def create(cls, **payload: Any) -> 'GeometrySolverReadinessVerdict':
        return _seal(
            cls, payload, 'verdict_id', 'verdict_sha256', 'srv'
        )


def _defect_solver_effect(
    defect: GeometryDefect,
    context: SolverGeometryContext,
) -> Literal['block', 'degrade', 'evidence_gap', 'informational']:
    """Map one unresolved defect onto its solver-conditioned effect."""
    kind = defect.kind
    if kind in ('non_manifold_edges', 'geometry_unavailable'):
        return 'block'
    if kind == 'solver_unsupported_condition':
        return 'block'
    if kind in ('open_boundary_edges', 'not_watertight'):
        return 'block' if context.requires_watertight_volume else 'degrade'
    if kind == 'portal_opening_ambiguity':
        return 'block' if context.requires_resolved_portals else 'degrade'
    if kind == 'material_assignment_gap':
        return 'block' if context.requires_material_assignments else 'degrade'
    if kind == 'inverted_normals':
        return 'block' if context.requires_consistent_winding else 'degrade'
    if kind == 'degenerate_or_tiny_features':
        if context.minimum_feature_m is not None:
            min_size = defect.evidence.get('minimum_feature_m')
            if min_size is not None and float(min_size) < float(
                context.minimum_feature_m
            ):
                return 'block'
        return 'degrade'
    if kind == 'coordinate_unit_anomaly':
        if defect.evidence.get('unit_state') == 'undeclared':
            return 'evidence_gap'
        return 'degrade'
    if kind == 'diagnostic_evidence_gap':
        return 'evidence_gap'
    if defect.severity == 'critical':
        return 'block'
    if defect.severity == 'warning':
        return 'degrade'
    return 'informational'


def evaluate_solver_readiness(
    *,
    subject: GeometryIntakeSubject | None = None,
    derived_revision: DerivedGeometryRevision | None = None,
    report: GeometryIntakeReport,
    defects: Sequence[GeometryDefect] | None = None,
    adapter_descriptor: AcousticSolverAdapterDescriptor,
    capability_manifest: SolverCapabilityManifest | None = None,
    solver_context: SolverGeometryContext | None = None,
    evaluated_at_utc: str,
) -> GeometrySolverReadinessVerdict:
    """SOLVER_READINESS: verdict against the selected solver contract.

    Exactly one of ``subject`` / ``derived_revision`` supplies the
    evaluated geometry. ``defects`` defaults to the report's defect list
    for a source subject or the revision's residual defects for a derived
    geometry — unresolved defects gate by solver-conditioned effect.
    """
    if (subject is None) == (derived_revision is None):
        raise ValueError(
            'exactly one of subject or derived_revision is required'
        )
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    if derived_revision is not None:
        geometry_ref = AuthorityRef(
            kind='derived_geometry_revision',
            ref_id=derived_revision.derived_revision_id,
            ref_sha256=derived_revision.derived_revision_sha256,
        )
        geometry_sha256 = derived_revision.derived_geometry_sha256
        effective_defects = (
            tuple(defects)
            if defects is not None
            else derived_revision.residual_defects
        )
        openings = derived_revision.openings
    else:
        assert subject is not None
        geometry_ref = AuthorityRef(
            kind='geometry_intake_subject',
            ref_id=subject.subject_id,
            ref_sha256=subject.subject_sha256,
        )
        geometry_sha256 = subject.subject_sha256
        effective_defects = (
            tuple(defects) if defects is not None else report.defects
        )
        openings = subject.openings
    context = solver_context or derive_solver_geometry_context(
        adapter_descriptor, capability_manifest
    )
    if capability_manifest is not None and (
        capability_manifest.adapter_descriptor_id
        != adapter_descriptor.descriptor_id
    ):
        raise GeometryIntakeError(
            'capability manifest does not describe this adapter'
        )

    blocking: list[ReadinessReason] = []
    degraded: list[ReadinessReason] = []
    gaps: list[ReadinessReason] = []
    unresolved: list[str] = []

    for defect in effective_defects:
        effect = _defect_solver_effect(defect, context)
        if effect == 'informational':
            continue
        unresolved.append(defect.defect_id)
        reason = ReadinessReason(
            origin=defect.origin,
            defect_id=defect.defect_id,
            defect_kind=defect.kind,
            text=(
                f'{defect.kind} on '
                f'{",".join(defect.part_refs) or "subject"}: '
                f'{defect.consequence}'
            ),
        )
        if effect == 'block':
            blocking.append(reason)
        elif effect == 'degrade':
            degraded.append(reason)
        else:
            gaps.append(reason)

    # capability-manifest conditioning ---------------------------------
    open_portals = [
        opening for opening in openings
        if opening.is_open and opening.resolution != 'resolved_closed'
    ]
    if capability_manifest is not None and open_portals:
        portal_row = capability_manifest.row_for('portal_region_coupling')
        if portal_row.state == 'UNSUPPORTED':
            blocking.append(
                ReadinessReason(
                    origin='solver_limitation',
                    phenomenon='portal_region_coupling',
                    text=(
                        'solver manifest declares portal_region_coupling '
                        'UNSUPPORTED but the geometry declares '
                        f'{len(open_portals)} open portal(s)'
                    ),
                )
            )
        elif portal_row.state == 'BOUNDED':
            degraded.append(
                ReadinessReason(
                    origin='solver_limitation',
                    phenomenon='portal_region_coupling',
                    text=(
                        'portal_region_coupling is BOUNDED: '
                        f'{portal_row.bound_description or "see manifest"}'
                    ),
                )
            )

    if blocking:
        verdict = 'unsupported'
    elif gaps:
        verdict = 'unknown'
    elif degraded:
        verdict = 'degraded'
    else:
        verdict = 'supported'

    manifest_ref = None
    if capability_manifest is not None:
        manifest_ref = AuthorityRef(
            kind='solver_capability_manifest',
            ref_id=capability_manifest.manifest_id,
            ref_sha256=capability_manifest.semantic_sha256,
        )
    return GeometrySolverReadinessVerdict.create(
        authority_version=GEOMETRY_INTAKE_AUTHORITY_VERSION,
        document_id=report.document_id,
        geometry_ref=geometry_ref,
        geometry_sha256=geometry_sha256,
        report_ref=AuthorityRef(
            kind='geometry_intake_report',
            ref_id=report.report_id,
            ref_sha256=report.report_sha256,
        ),
        adapter_descriptor_ref=AuthorityRef(
            kind='solver_adapter_descriptor',
            ref_id=adapter_descriptor.descriptor_id,
            ref_sha256=adapter_descriptor.semantic_sha256,
        ),
        capability_manifest_ref=manifest_ref,
        solver_context=context,
        verdict=verdict,
        blocking_reasons=tuple(blocking),
        degraded_reasons=tuple(degraded),
        evidence_gaps=tuple(gaps),
        unresolved_defect_ids=tuple(unresolved),
        evaluated_at_utc=evaluated_at_utc,
        evaluator_id=GEOMETRY_READINESS_EVALUATOR_ID,
        evaluator_version=GEOMETRY_INTAKE_EVALUATOR_VERSION,
    )


def readiness_evidence_state(
    verdict: GeometrySolverReadinessVerdict,
    *,
    current_geometry_sha256: str,
    current_adapter_sha256: str | None = None,
    current_manifest_sha256: str | None = None,
) -> ReadinessEvidenceState:
    """Deterministic invalidation: the verdict is only current while the
    geometry hash, adapter descriptor and manifest it binds are
    unchanged. A re-import or scene revision changes the geometry hash
    and therefore retires every verdict derived from it."""
    if current_geometry_sha256 != verdict.geometry_sha256:
        return 'stale_geometry'
    if current_adapter_sha256 is not None and (
        current_adapter_sha256 != verdict.adapter_descriptor_ref.ref_sha256
    ):
        return 'stale_solver'
    if current_manifest_sha256 is not None and (
        verdict.capability_manifest_ref is None
        or current_manifest_sha256
        != verdict.capability_manifest_ref.ref_sha256
    ):
        return 'stale_manifest'
    return 'current'


def geometry_solver_execution_gate(
    verdict: GeometrySolverReadinessVerdict,
    *,
    current_geometry_sha256: str | None = None,
) -> tuple[Literal['allow', 'deny'], tuple[str, ...]]:
    """Structured gate: ``allow`` only for supported/degraded verdicts
    whose evidence is still current."""
    if current_geometry_sha256 is not None and (
        current_geometry_sha256 != verdict.geometry_sha256
    ):
        return 'deny', (
            f'verdict {verdict.verdict_id} binds stale geometry '
            '(re-import or revision since evaluation)',
        )
    if verdict.verdict == 'unsupported':
        return 'deny', tuple(r.text for r in verdict.blocking_reasons)
    if verdict.verdict == 'unknown':
        return 'deny', tuple(r.text for r in verdict.evidence_gaps)
    return 'allow', tuple(r.text for r in verdict.degraded_reasons)


def assert_geometry_solver_execution_permitted(
    verdict: GeometrySolverReadinessVerdict,
    *,
    current_geometry_sha256: str | None = None,
) -> None:
    """Fail-closed gate used by solver dispatch before execution."""
    state, reasons = geometry_solver_execution_gate(
        verdict, current_geometry_sha256=current_geometry_sha256
    )
    if state == 'deny':
        raise GeometrySolverExecutionBlockedError(
            verdict.verdict_id, reasons
        )


# --- operator-facing helpers ------------------------------------------------------


def defect_locate_targets(defect: GeometryDefect) -> tuple[str, ...]:
    """The scene entity ids a workspace highlight should select for this
    defect (headless-testable; the UI applies the selection idioms)."""
    targets: list[str] = list(defect.entity_refs)
    for part_ref in defect.part_refs:
        if part_ref.startswith('entity:'):
            entity_id = part_ref.split(':', 1)[1]
            if entity_id not in targets:
                targets.append(entity_id)
    return tuple(targets)


# ---------------------------------------------------------------------------
# IFC source intake (#866): real file -> subject ingestion producing
# artifact/mapping-pinned ``source_refs`` instead of fixture-built subjects.
# ---------------------------------------------------------------------------

IFC_INTAKE_IMPORTER_ID = 'htdt.ifc_step_intake'

_IFC_LENGTH_UNITS: dict[str, str] = {
    'metre': 'meters',
    'meter': 'meters',
    'millimetre': 'millimeters',
    'millimeter': 'millimeters',
    'centimetre': 'centimeters',
    'centimeter': 'centimeters',
    'inch': 'inches',
    'foot': 'feet',
    'feet': 'feet',
}


def _ifc_unit_declared(coordinate: Any) -> bool:
    return (
        coordinate.length_unit_state == 'declared'
        and coordinate.length_scale_to_meter is not None
    )


def _ifc_import_authority(
    coordinate: Any,
    importer_version: str,
) -> MeshImportAuthority:
    """Map the resolved IFC coordinate authority onto mesh import authority.

    A declared IFC unit still maps to ``custom`` when its unit name is not a
    shape HTDT models directly (e.g. decimetre); the exact scale is then the
    authoritative interpretation. Undeclared/ambiguous units stay
    ``undeclared`` — the coordinate anomaly defect carries the story.
    """
    unit_name = (coordinate.length_unit_name or '').strip().lower()
    source_unit = _IFC_LENGTH_UNITS.get(unit_name)
    declared = _ifc_unit_declared(coordinate)
    custom = declared and source_unit is None
    unit_known = declared and source_unit is not None
    return MeshImportAuthority(
        source_unit='custom' if custom else (source_unit or 'unknown'),
        unit_declared_by=(
            'format_specification' if (unit_known or custom) else 'undeclared'
        ),
        custom_scale_to_meters=(
            coordinate.length_scale_to_meter if custom else None
        ),
        source_up_axis='z+' if (unit_known or custom) else 'unknown',
        source_forward_axis='y+' if (unit_known or custom) else 'unknown',
        handedness='right' if (unit_known or custom) else 'unknown',
        convention_declared_by=(
            'format_specification' if (unit_known or custom) else 'undeclared'
        ),
        local_anchor='source_origin',
        importer_id=IFC_INTAKE_IMPORTER_ID,
        importer_version=importer_version,
    )


def _ifc_apply_transform(
    matrix: Sequence[float],
    point: tuple[float, float, float],
    source_scale_to_meters: float,
) -> tuple[float, float, float]:
    """Source-unit local point -> world meters via ``world_transform_m``.

    ``world_transform_m`` is rigid rotation + translation expressed in
    meters (the importer rescales only the translation), so the local
    point is rescaled to meters before the matrix is applied.
    """
    x = point[0] * source_scale_to_meters
    y = point[1] * source_scale_to_meters
    z = point[2] * source_scale_to_meters
    return (
        matrix[0] * x + matrix[1] * y + matrix[2] * z + matrix[3],
        matrix[4] * x + matrix[5] * y + matrix[6] * z + matrix[7],
        matrix[8] * x + matrix[9] * y + matrix[10] * z + matrix[11],
    )


def _ifc_direction_basis(
    direction: tuple[float, float, float],
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
] | None:
    """Orthonormal (u, v, w) basis with w along ``direction``."""
    dx, dy, dz = direction
    length = sqrt(dx * dx + dy * dy + dz * dz)
    if not isfinite(length) or length <= 1.0e-12:
        return None
    w = (dx / length, dy / length, dz / length)
    # Any axis sufficiently non-parallel seeds u; prefer global Z.
    seed = (0.0, 0.0, 1.0)
    if abs(w[2]) > 0.9:
        seed = (1.0, 0.0, 0.0)
    u = (
        seed[1] * w[2] - seed[2] * w[1],
        seed[2] * w[0] - seed[0] * w[2],
        seed[0] * w[1] - seed[1] * w[0],
    )
    ul = sqrt(u[0] * u[0] + u[1] * u[1] + u[2] * u[2])
    if ul <= 1.0e-12:
        return None
    u = (u[0] / ul, u[1] / ul, u[2] / ul)
    v = (
        w[1] * u[2] - w[2] * u[1],
        w[2] * u[0] - w[0] * u[2],
        w[0] * u[1] - w[1] * u[0],
    )
    return u, v, w


def _ifc_extruded_mesh(
    profile_points: Sequence[tuple[float, float]],
    direction: tuple[float, float, float] | None,
    depth: float | None,
) -> tuple[
    list[tuple[float, float, float]], list[tuple[int, int, int]]
] | None:
    """Mesh an ``extruded_area_solid`` descriptor in local source units."""
    if depth is None or not isfinite(depth) or depth <= 0.0:
        return None
    axis = direction if direction is not None else (0.0, 0.0, 1.0)
    basis = _ifc_direction_basis(axis)
    if basis is None:
        return None
    vertices, triangles = _prism_vertices_triangles(
        tuple(profile_points), depth
    )
    u, v, w = basis
    if w != (0.0, 0.0, 1.0):
        vertices = [
            (
                u[0] * vx + v[0] * vy + w[0] * vz,
                u[1] * vx + v[1] * vy + w[1] * vz,
                u[2] * vx + v[2] * vy + w[2] * vz,
            )
            for vx, vy, vz in vertices
        ]
    return vertices, triangles


def _ifc_box_mesh(
    bounds_min: Sequence[float],
    bounds_max: Sequence[float],
) -> tuple[
    list[tuple[float, float, float]], list[tuple[int, int, int]]
] | None:
    """Mesh a ``bounding_box`` descriptor as a corner-anchored box."""
    size = tuple(bounds_max[i] - bounds_min[i] for i in range(3))
    if any(not isfinite(d) or d <= 0.0 for d in size):
        return None
    vertices, triangles = _box_vertices_triangles(
        (size[0], size[1], size[2])
    )
    vertices = [
        (vx + bounds_min[0], vy + bounds_min[1], vz + bounds_min[2])
        for vx, vy, vz in vertices
    ]
    return vertices, triangles


def _ifc_descriptor_mesh(
    descriptor: Any,
) -> tuple[
    list[tuple[float, float, float]] | None,
    list[tuple[int, int, int]] | None,
]:
    """Return local source-unit (vertices, triangles) or ``(None, None)``.

    ``faceted_brep`` descriptors only carry vertex evidence — face topology
    is absent, so the mapping degrades to a stub rather than pretending the
    point cloud is a boundary mesh.
    """
    kind = getattr(descriptor, 'kind', None)
    if kind == 'extruded_area_solid':
        mesh = _ifc_extruded_mesh(
            descriptor.profile_points,
            getattr(descriptor, 'extrusion_direction', None),
            getattr(descriptor, 'depth_source_units', None),
        )
        if mesh is None:
            return None, None
        return mesh[0], mesh[1]
    if kind == 'bounding_box':
        mesh = _ifc_box_mesh(
            descriptor.bounds_min, descriptor.bounds_max
        )
        if mesh is None:
            return None, None
        return mesh[0], mesh[1]
    return None, None


def _ifc_descriptor_area_m2(descriptor: Any, scale: float) -> float | None:
    """Opening-area proxy for a void element's descriptor (meters²)."""
    kind = getattr(descriptor, 'kind', None)
    if kind == 'extruded_area_solid' and descriptor.profile_points:
        area = 0.0
        pts = list(descriptor.profile_points)
        for i, (x0, y0) in enumerate(pts):
            x1, y1 = pts[(i + 1) % len(pts)]
            area += x0 * y1 - x1 * y0
        return abs(area) * 0.5 * scale * scale
    if kind == 'bounding_box':
        dx = descriptor.bounds_max[0] - descriptor.bounds_min[0]
        dy = descriptor.bounds_max[1] - descriptor.bounds_min[1]
        dz = descriptor.bounds_max[2] - descriptor.bounds_min[2]
        if dx <= 0.0 or dy <= 0.0 or dz <= 0.0:
            return None
        # The opening face is the largest plane — honest approximation of
        # a wall-hosted void whose orientation is unresolvable.
        largest = max(dx * dy, dx * dz, dy * dz)
        return largest * scale * scale
    if kind == 'faceted_brep' and len(descriptor.vertices) >= 3:
        pts = list(descriptor.vertices)
        ax, ay, az = 0.0, 0.0, 0.0
        for i, (x0, y0, z0) in enumerate(pts):
            x1, y1, z1 = pts[(i + 1) % len(pts)]
            ax += (y0 - y1) * (z0 + z1)
            ay += (z0 - z1) * (x0 + x1)
            az += (x0 - x1) * (y0 + y1)
        return 0.5 * sqrt(ax * ax + ay * ay + az * az) * scale * scale
    return None


def _ifc_opening_area_m2(void_mapping: Any, coordinate: Any) -> float | None:
    scale = coordinate.length_scale_to_meter
    if scale is None or scale <= 0.0:
        return None
    for descriptor in void_mapping.geometry_descriptors:
        area = _ifc_descriptor_area_m2(descriptor, scale)
        if area is not None and isfinite(area) and area > 0.0:
            return area
    return None


def _ifc_owner_ref(mapping: Any) -> str:
    return mapping.ifc_global_id or f'#{mapping.step_entity_id}'


def build_ifc_intake_subject(
    document_id: str,
    artifact: Any,
    mappings: Sequence[Any],
) -> GeometryIntakeSubject:
    """SOURCE_GEOMETRY: intake subject from a persisted IFC import artifact.

    Translates resolved IFC entity mappings into world-meter intake parts:
    ``extruded_area_solid`` descriptors become prism meshes,
    ``bounding_box`` descriptors become box meshes, and anything
    unresolvable (faceted breps, withheld transforms, entities without
    descriptors) degrades to a fail-closed :class:`IntakePartStub` so the
    coverage count stays honest. Every part pins its
    ``ifc_entity_mapping`` record as ``source_asset_ref`` and the subject
    pins the ``ifc_import_artifact`` in ``source_refs``.

    Host ``openings`` links become declared :class:`IntakeOpening` records
    when the void element's area is computable; void elements never become
    parts themselves (they are boundary gaps, not surfaces), while their
    filling elements (doors/windows) surface as ordinary object parts.
    """
    coordinate = artifact.coordinate
    authority = _ifc_import_authority(
        coordinate, artifact.importer_version
    )
    declared = _ifc_unit_declared(coordinate)
    if declared:
        unit_state: IntakeUnitState = 'declared'
        unit_details = (
            f'ifc length unit declared ({coordinate.length_unit_name or "?"}; '
            f'{coordinate.length_scale_to_meter:g} m per source unit)'
        )
    else:
        unit_state = 'undeclared'
        unit_details = (
            f'ifc length_unit_state={coordinate.length_unit_state}: '
            'transforms withheld; geometries resolved as stubs'
        )

    by_global_id = {
        mapping.ifc_global_id: mapping
        for mapping in mappings
        if mapping.ifc_global_id
    }
    consumed_void_ids: set[str] = set()

    parts: list[IntakeMeshPart] = []
    stubs: list[IntakePartStub] = []
    openings: list[IntakeOpening] = []
    part_ids: set[str] = set()

    def _consume_host_openings(
        mapping: Any, part_id: str, *, host_resolved: bool
    ) -> None:
        """Turn a host's openings links into IntakeOpenings or honest stubs.

        When the host itself could not be resolved, its linked voids still
        surface as stubs (never silently dropped, never claimed as portals
        against a boundary that does not exist in the subject).
        """
        for link in mapping.openings:
            void = by_global_id.get(link.opening_global_id)
            if void is None:
                continue
            consumed_void_ids.add(void.mapping_id)
            if not host_resolved:
                stubs.append(
                    IntakePartStub(
                        part_id=f'ifc:{void.step_entity_id}',
                        owner_kind='entity_body',
                        owner_ref=_ifc_owner_ref(void),
                        role='object_surface',
                        unavailability_reason=(
                            'host boundary unresolvable; '
                            'portal cannot be declared'
                        ),
                    )
                )
                continue
            area_m2 = _ifc_opening_area_m2(void, coordinate)
            if area_m2 is None:
                stubs.append(
                    IntakePartStub(
                        part_id=f'ifc:{void.step_entity_id}',
                        owner_kind='entity_body',
                        owner_ref=_ifc_owner_ref(void),
                        role='object_surface',
                        unavailability_reason=(
                            'opening void geometry unmeasurable; '
                            'portal area cannot be declared'
                        ),
                    )
                )
                continue
            openings.append(
                IntakeOpening(
                    opening_id=link.opening_global_id,
                    host_part_id=part_id,
                    host_wall_id=_ifc_owner_ref(mapping),
                    kind=link.filling_type or 'opening',
                    area_m2=area_m2,
                    is_open=link.filling_global_id is None,
                    resolution='unresolved',
                )
            )

    for mapping in mappings:
        if mapping.ifc_type == 'IFCOPENINGELEMENT':
            continue  # void — consumed via the host's openings links
        part_id = f'ifc:{mapping.step_entity_id}'
        owner_kind: IntakePartKind = (
            'room_boundary'
            if mapping.htdt_role in ('room_candidate', 'boundary')
            else 'entity_body'
        )
        role: IntakePartRole = (
            'room_boundary' if owner_kind == 'room_boundary' else 'object_surface'
        )
        part_ids.add(part_id)
        material_labels = [
            layer.material_name
            for layer in mapping.material_layers
            if layer.material_name
        ]
        material_state: IntakeMaterialState = (
            'assigned' if material_labels else 'unassigned'
        )
        if mapping.world_transform_m is None:
            stubs.append(
                IntakePartStub(
                    part_id=part_id,
                    owner_kind=owner_kind,
                    owner_ref=_ifc_owner_ref(mapping),
                    role=role,
                    unavailability_reason=(
                        'ifc placement transform withheld '
                        f'({coordinate.length_unit_state} units or '
                        'unresolved placement chain)'
                    ),
                )
            )
            _consume_host_openings(mapping, part_id, host_resolved=False)
            continue
        descriptor_mesh: tuple[
            list[tuple[float, float, float]] | None,
            list[tuple[int, int, int]] | None,
        ] = (None, None)
        for descriptor in mapping.geometry_descriptors:
            descriptor_mesh = _ifc_descriptor_mesh(descriptor)
            if descriptor_mesh[0] is not None:
                break
        vertices, triangles = descriptor_mesh
        if vertices is None or triangles is None:
            descriptor_note = (
                'no resolvable geometry descriptors'
                if not mapping.geometry_descriptors
                else 'descriptors present but none meshable '
                '(faceted brep / degenerate bounds)'
            )
            stubs.append(
                IntakePartStub(
                    part_id=part_id,
                    owner_kind=owner_kind,
                    owner_ref=_ifc_owner_ref(mapping),
                    role=role,
                    unavailability_reason=descriptor_note,
                )
            )
            _consume_host_openings(mapping, part_id, host_resolved=False)
            continue
        world_vertices = [
            _ifc_apply_transform(
                mapping.world_transform_m,
                vertex,
                coordinate.length_scale_to_meter,
            )
            for vertex in vertices
        ]
        mesh = _mesh_via_meshbin(
            world_vertices,
            triangles,
            source_name=f'{artifact.file_name}#{mapping.step_entity_id}',
        )
        parts.append(
            IntakeMeshPart(
                part_id=part_id,
                owner_kind=owner_kind,
                owner_ref=_ifc_owner_ref(mapping),
                role=role,
                mesh=mesh,
                material_state=material_state,
                material_label=(
                    ', '.join(material_labels) if material_labels else None
                ),
                import_authority=authority,
                source_asset_ref=AuthorityRef(
                    kind='ifc_entity_mapping',
                    ref_id=mapping.mapping_id,
                    ref_sha256=mapping.mapping_sha256,
                ),
            )
        )
        _consume_host_openings(mapping, part_id, host_resolved=True)

    # Orphaned voids: referenced by no host — still surfaced as stubs so the
    # file content is never silently dropped.
    for mapping in mappings:
        if (
            mapping.ifc_type == 'IFCOPENINGELEMENT'
            and mapping.mapping_id not in consumed_void_ids
        ):
            stubs.append(
                IntakePartStub(
                    part_id=f'ifc:{mapping.step_entity_id}',
                    owner_kind='entity_body',
                    owner_ref=_ifc_owner_ref(mapping),
                    role='object_surface',
                    unavailability_reason=(
                        'opening element with no host boundary; '
                        'portal cannot be declared'
                    ),
                )
            )

    return GeometryIntakeSubject.create(
        document_id=document_id,
        source_kind='ifc_import',
        source_refs=(
            AuthorityRef(
                kind='ifc_import_artifact',
                ref_id=artifact.artifact_id,
                ref_sha256=artifact.artifact_sha256,
            ),
        ),
        parts=parts,
        unresolved_parts=stubs,
        openings=openings,
        unit_state=unit_state,
        unit_details=unit_details,
    )


GEOMETRY_INTAKE_LABELS: dict[str, str] = {
    # defect kinds
    'defect.open_boundary_edges': '境界が開いた辺',
    'defect.non_manifold_edges': '非多様体エッジ',
    'defect.disconnected_regions': '分離した領域',
    'defect.duplicate_or_overlapping_faces': '重複・重なり面',
    'defect.inverted_normals': '反転した法線',
    'defect.degenerate_or_tiny_features': '退化・微小フィーチャ',
    'defect.portal_opening_ambiguity': '開口部/ポータルの曖昧さ',
    'defect.material_assignment_gap': '材質未割当',
    'defect.coordinate_unit_anomaly': '座標・単位の異常',
    'defect.not_watertight': '水密性なし',
    'defect.solver_unsupported_condition': 'ソルバー非対応条件',
    'defect.geometry_unavailable': 'ジオメトリ取得不可',
    'defect.diagnostic_evidence_gap': '診断証跡不足',
    # severities
    'severity.info': '情報',
    'severity.warning': '警告',
    'severity.critical': '致命的',
    # origins
    'origin.source_model': 'ソースモデルの欠陥',
    'origin.solver_limitation': 'ソルバー側の制約',
    # readiness verdicts
    'verdict.supported': '対応',
    'verdict.degraded': '条件付き対応',
    'verdict.unsupported': '非対応',
    'verdict.unknown': '判定不能',
    # evidence states
    'evidence.current': '最新',
    'evidence.stale_geometry': 'ジオメトリ変更済み',
    'evidence.stale_solver': 'ソルバー変更済み',
    'evidence.stale_manifest': 'マニフェスト変更済み',
    # repair actions
    'repair.consolidate_exact_duplicate_vertices': '完全一致頂点の統合',
    'repair.weld_vertices_within_tolerance': '許容誤差内の頂点溶接',
    'repair.remove_unreferenced_vertices': '未参照頂点の削除',
    'repair.remove_exact_duplicate_faces': '完全重複面の削除',
    'repair.correct_consistent_winding': '巻き方向の統一',
    'repair.remove_degenerate_faces': '退化面の削除',
    'repair.remove_disconnected_fragment': '分離フラグメントの削除',
    'repair.declare_units': '単位の宣言',
    'repair.assign_material': '材質の割当',
    'repair.resolve_portal': 'ポータル状態の解決',
    'repair.no_repair_available': '自動修復なし',
    # decision values
    'decision.accepted': '承認',
    'decision.rejected': '却下',
    # resolution choices
    'resolution.open_portal': '開ポータルとして扱う',
    'resolution.closed_boundary': '閉境界として扱う',
    # intake sections
    'section.source_defects': 'ソースモデルの欠陥',
    'section.solver_limitations': 'ソルバー側の制約',
    'section.repair_proposal': '修復提案',
    'section.readiness': 'ソルバー適合性',
    'ui.locate': '対象を表示',
    'ui.accept': '承認',
    'ui.reject': '却下',
    'ui.diagnose': '診断を実行',
    'ui.derive': '派生リビジョンを生成',
    'ui.import_ifc': 'IFC を取り込む',
    'ui.adopt_scene': '現在のシーンを採用',
    'ui.solver': '対象ソルバー',
}


def geometry_intake_label(key: str) -> str:
    """JA operator-facing label; unknown keys fall back to the key."""
    return GEOMETRY_INTAKE_LABELS.get(key, key)


__all__ = [
    'DerivedGeometryPart',
    'DerivedGeometryRevision',
    'GEOMETRY_INTAKE_AUTHORITY_VERSION',
    'GEOMETRY_INTAKE_EVALUATOR_ID',
    'GEOMETRY_INTAKE_EVALUATOR_VERSION',
    'GEOMETRY_INTAKE_LABELS',
    'GEOMETRY_READINESS_EVALUATOR_ID',
    'GeometryDefect',
    'GeometryDefectKind',
    'GeometryDefectOrigin',
    'GeometryDefectSeverity',
    'GeometryIntakeError',
    'GeometryIntakeReport',
    'GeometryIntakeSubject',
    'GeometryRepairAcceptance',
    'GeometryRepairAcceptanceError',
    'GeometryRepairAction',
    'GeometryRepairActionKind',
    'GeometryRepairProposal',
    'GeometrySolverExecutionBlockedError',
    'GeometrySolverReadinessVerdict',
    'IFC_INTAKE_IMPORTER_ID',
    'IntakeMeshPart',
    'IntakeOpening',
    'IntakePartStub',
    'IntakeUnitState',
    'ReadinessEvidenceState',
    'ReadinessReason',
    'RepairActionDecision',
    'SolverGeometryContext',
    'SolverReadinessState',
    'assert_geometry_solver_execution_permitted',
    'build_geometry_intake_subject',
    'build_ifc_intake_subject',
    'defect_locate_targets',
    'derive_geometry_revision',
    'derive_solver_geometry_context',
    'diagnose_geometry_intake',
    'evaluate_solver_readiness',
    'geometry_intake_label',
    'geometry_solver_execution_gate',
    'propose_geometry_repairs',
    'readiness_evidence_state',
    'record_geometry_repair_acceptance',
]
