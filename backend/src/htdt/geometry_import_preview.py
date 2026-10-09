"""Read-only 3D import preview resolver (#980).

Turns the guided import dialog's declared unit/axis/handedness/anchor and
the bounded-repair preview into drawable scene data. Resolution uses the
SAME transform authority the commit paths use
(``make_mesh_import_authority`` + ``mesh_import_scene_transform``), so the
preview cannot drift from what an import would commit.

Honesty contract:

- Nothing is estimated or forced. An undeclared unit renders source
  coordinates with an explicit notice and no meter conversion; an
  unresolved axis convention (``mesh_import_axis_matrix`` → ``None``)
  renders unrotated, matching the recorded authority.
- Preview decimation is display-only — the capped face list is rebuilt
  from the parsed mesh every refresh and never travels into the import
  request, the repaired mesh, or any persisted asset.
- Diff fates derive only from the actual ``RepairedRawMesh`` lineage of
  the same source mesh id. The lineage records per-operation counts, not
  per-face attribution, so fates are reconstructed deterministically
  where the data allows — and reported ``'unknown'`` where vertex-moving
  repairs make removal-vs-movement genuinely ambiguous.
- Defect-focus positions are render-only derivations: diagnostics carry
  counts, never locations. The adapter re-derives *positions* for codes
  whose location follows from one topological pass and reports
  ``'unknown'`` for the rest; the QA verdict itself stays with
  ``diagnose_raw_visual_mesh`` / ``MeshHealthSummary`` and is never
  recomputed here.
- ``supported=False`` means the geometry itself cannot be drawn (empty
  or non-finite). A VTK/Qt failure while drawing is a separate render
  failure reported by the widget layer, never conflated with geometry
  support.

No Qt imports — the dialog owns widgets; this module owns resolution.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import floor, isfinite, sqrt
from typing import Literal, Sequence

from .mesh_import_authority import (
    format_declared_source_unit,
    make_mesh_import_authority,
    mesh_import_scene_transform,
)
from .raw_mesh import (
    RawMeshDiagnosticProfile,
    RawMeshDiagnosticResult,
    RawMeshTriangle,
    RawMeshVertex,
    RawVisualMesh,
)
from .raw_mesh_repair import RepairedRawMesh


PreviewViewMode = Literal['original', 'repaired', 'diff']

#: Per-source-triangle fate in the diff view. ``'kept'``/``'flipped'``/
#: ``'removed'``/``'moved'`` are only asserted when derivable from the
#: actual repair lineage; ``'unknown'`` is the honest residual.
PreviewFaceFate = Literal['kept', 'flipped', 'moved', 'removed', 'unknown']

#: Defect codes whose focus position follows from one topological pass.
#: ``inverted_normal`` needs the full orientation propagation and
#: ``overlapping_face`` the coplanar-overlap sweep — both stay 'unknown'
#: rather than re-running diagnostic internals for display.
_FOCUSABLE_CODES = frozenset(
    {
        'open_boundary',
        'watertightness',
        'non_manifold_edge',
        'duplicate_face',
        'sliver_face',
        'tiny_feature',
    }
)

#: Display budget for the preview. Import caps bound the source mesh to
#: 16M vertices / 32M faces — far past what Mesa software-GL can draw
#: interactively — so the preview decimates deterministically to this
#: face count. Decimation never touches the imported data.
PREVIEW_MAX_FACES = 200_000



@dataclass(frozen=True, slots=True)
class PreviewDeclaration:
    """The dialog's current unit/axis/anchor declaration snapshot."""

    source_unit: str | None
    custom_scale_to_meters: float | None
    up_axis: str
    forward_axis: str
    handedness: str
    local_anchor: str


@dataclass(frozen=True, slots=True)
class PreviewDefectFocus:
    """Focus positions for one defect code, in preview coordinates."""

    code: str
    state: Literal['located', 'unknown']
    points: tuple[tuple[float, float, float], ...]


@dataclass(frozen=True, slots=True)
class ImportPreviewScene:
    """Everything one preview render pass draws or announces."""

    view_mode: PreviewViewMode
    supported: bool
    #: 'unsupported_geometry' when supported is False; render failures are
    #: reported by the widget layer, never here.
    unsupported_reason: str | None
    #: 'meters' once the declared transform resolved; 'source_units' while
    #: the unit is undeclared (no meter conversion is implied then).
    coordinate_space: Literal['meters', 'source_units']
    axis_convention_resolved: bool
    vertices: tuple[tuple[float, float, float], ...]
    faces: tuple[tuple[int, int, int], ...]
    #: Per-source-triangle fates for ``view_mode='diff'`` (``()`` else),
    #: one entry per FULL source triangle — pair with ``kept_face_indices``
    #: when the preview was decimated.
    source_fates: tuple[PreviewFaceFate, ...]
    #: Source-triangle index each drawn face maps back to (identity when
    #: not decimated) — fate colors stay lineage-exact under decimation.
    kept_face_indices: tuple[int, ...]
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    dims: tuple[float, float, float]
    vertex_count_full: int
    face_count_full: int
    decimated: bool
    focuses: tuple[PreviewDefectFocus, ...]
    notices: tuple[str, ...]


def _quantized_key(
    point: tuple[float, float, float], tolerance: float
) -> tuple[int, int, int]:
    return tuple(floor(value / tolerance + 0.5) for value in point)


def _triangle_points(
    vertices: Sequence[tuple[float, float, float]],
    triangle: RawMeshTriangle,
) -> tuple[tuple[float, float, float], ...]:
    return (vertices[triangle.a], vertices[triangle.b], vertices[triangle.c])


def _centroid(points: Sequence[tuple[float, float, float]]) -> tuple[float, float, float]:
    count = len(points)
    return (
        sum(p[0] for p in points) / count,
        sum(p[1] for p in points) / count,
        sum(p[2] for p in points) / count,
    )


def _distance_sq(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return sum((a[i] - b[i]) ** 2 for i in range(3))


def _triangle_area(points: tuple[tuple[float, float, float], ...]) -> float:
    ux, uy, uz = (points[1][i] - points[0][i] for i in range(3))
    vx, vy, vz = (points[2][i] - points[0][i] for i in range(3))
    cx, cy, cz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    return 0.5 * sqrt(cx * cx + cy * cy + cz * cz)


def _transform_point(
    matrix: tuple[tuple[float, float, float, float], ...],
    point: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        matrix[0][0] * point[0] + matrix[0][1] * point[1] + matrix[0][2] * point[2] + matrix[0][3],
        matrix[1][0] * point[0] + matrix[1][1] * point[1] + matrix[1][2] * point[2] + matrix[1][3],
        matrix[2][0] * point[0] + matrix[2][1] * point[1] + matrix[2][2] * point[2] + matrix[2][3],
    )


def resolve_preview_declaration(
    declaration: PreviewDeclaration,
    *,
    spec_unit: str,
    source_vertices: Sequence[tuple[float, float, float]],
) -> tuple[tuple[tuple[float, float, float, float], ...] | None, bool, tuple[str, ...]]:
    """Resolve the declared transform exactly as the commit paths do.

    Returns ``(matrix_source_to_preview, axis_convention_resolved, notices)``.
    ``matrix`` is ``None`` while the unit is undeclared — there is no
    honest meter conversion to preview, so source coordinates stand.
    """

    notices: list[str] = []
    unit = declaration.source_unit
    if unit is None:
        notices.append('unit_undeclared')
        return None, False, tuple(notices)
    declared_by = (
        'format_specification' if spec_unit != 'unknown' else 'operator_confirmed'
    )
    try:
        authority = make_mesh_import_authority(
            source_unit=unit,
            unit_declared_by=declared_by,
            custom_scale_to_meters=declaration.custom_scale_to_meters,
            up_axis=declaration.up_axis,
            forward_axis=declaration.forward_axis,
            handedness=declaration.handedness,
            local_anchor=declaration.local_anchor,
        )
    except ValueError:
        # e.g. 'custom' picked but no scale supplied — the declaration is
        # incomplete, not wrong; preview falls back to source coordinates.
        notices.append('unit_undeclared')
        return None, False, tuple(notices)
    matrix = mesh_import_scene_transform(authority, source_vertices)
    resolved = not (
        declaration.up_axis == 'unknown'
        or declaration.forward_axis == 'unknown'
        or declaration.handedness not in ('right', 'left')
    )
    if not resolved:
        notices.append('axis_convention_unresolved')
    return matrix, resolved, tuple(notices)


def _vertex_moving_ops_applied(repaired: RepairedRawMesh) -> bool:
    return any(
        result.execution_state == 'applied' and result.moved_vertex_count > 0
        for result in repaired.operation_results
    )


def source_triangle_fates(
    source: RawVisualMesh,
    repaired: RepairedRawMesh,
) -> tuple[PreviewFaceFate, ...]:
    """Per-source-triangle fate reconstructed from the actual lineage.

    Repairs are order-preserving: operations only delete triangle entries,
    remap vertex indices, or reverse a triangle's winding in place — the
    repaired list is a subsequence of the source list. Within each
    ``source_primitive`` group, a repaired triangle is matched greedily to
    the first unmatched source triangle with the same vertex-position
    multiset (exact duplicates are display-equivalent so any in-order
    choice draws the same faces):

    - same positions, same cyclic order → ``'kept'``
    - same positions, reversed winding → ``'flipped'``
    - no exact-position match, vertex-moving operations ran → ``'moved'``
      for position-matched leftovers paired in order, else ``'unknown'``
    - leftover sources with no vertex movement → ``'removed'``

    When vertex-moving repairs ran, a removed face and a vertex-moved
    face are not distinguishable from the lineage counts alone; those
    residuals report ``'unknown'`` rather than a guess.
    """

    if (
        repaired.source_raw_mesh_id != source.mesh_id
        or repaired.source_raw_mesh_semantic_hash != source.semantic_hash()
    ):
        return tuple('unknown' for _ in source.triangles)

    source_points = [(v.x, v.y, v.z) for v in source.vertices]
    repaired_points = [(v.x, v.y, v.z) for v in repaired.vertices]

    def multiset(points, tri) -> tuple[tuple[float, float, float], ...]:
        return tuple(sorted((points[tri.a], points[tri.b], points[tri.c])))

    # Repaired triangles grouped by (primitive, position multiset), in
    # order — exact-match lookups stay O(1) instead of rescanning groups.
    repaired_by_key: dict[tuple[str, tuple[tuple[float, float, float], ...]], deque[int]] = {}
    for index, tri in enumerate(repaired.triangles):
        key = (tri.source_primitive, multiset(repaired_points, tri))
        repaired_by_key.setdefault(key, deque()).append(index)

    moving = _vertex_moving_ops_applied(repaired)
    fates: list[PreviewFaceFate] = ['unknown'] * len(source.triangles)
    unmatched_sources: list[int] = []
    consumed: set[int] = set()

    for source_index, tri in enumerate(source.triangles):
        want = (tri.source_primitive, multiset(source_points, tri))
        pool = repaired_by_key.get(want)
        match = pool.popleft() if pool else None
        if match is None:
            unmatched_sources.append(source_index)
            continue
        consumed.add(match)
        ordered_repaired = repaired.triangles[match]
        source_order = (
            source_points[tri.a],
            source_points[tri.b],
            source_points[tri.c],
        )
        repaired_order = (
            repaired_points[ordered_repaired.a],
            repaired_points[ordered_repaired.b],
            repaired_points[ordered_repaired.c],
        )
        # Same position multiset: a cyclic rotation preserves winding,
        # any transposition reverses it.
        rotations = (
            source_order,
            (source_order[1], source_order[2], source_order[0]),
            (source_order[2], source_order[0], source_order[1]),
        )
        if repaired_order in rotations:
            fates[source_index] = 'kept'
        else:
            fates[source_index] = 'flipped'

    if not moving:
        for index in unmatched_sources:
            fates[index] = 'removed'
        return tuple(fates)

    # Vertex-moving ops ran: per primitive, a moved source triangle still
    # leaves a repaired entry (its source_primitive survives the move),
    # while a removed one leaves none — so when the leftover repaired
    # count differs from the unmatched source count the boundary is
    # determinate:
    #   - no leftover entry            → removed
    #   - counts equal                 → all moved (paired in order)
    #   - some leftover, some missing  → which is which is not derivable
    #     from lineage counts → 'unknown' (honest residual)
    remaining: dict[str, deque[int]] = {}
    for index, tri in enumerate(repaired.triangles):
        if index not in consumed:
            remaining.setdefault(tri.source_primitive, deque()).append(index)
    unmatched_by_primitive: dict[str, list[int]] = {}
    for source_index in unmatched_sources:
        unmatched_by_primitive.setdefault(
            source.triangles[source_index].source_primitive, []
        ).append(source_index)
    for primitive, source_indices in unmatched_by_primitive.items():
        pool = remaining.get(primitive)
        leftover = len(pool) if pool else 0
        if leftover == 0:
            for source_index in source_indices:
                fates[source_index] = 'removed'
        elif leftover == len(source_indices):
            for source_index in source_indices:
                consumed.add(pool.popleft())
                fates[source_index] = 'moved'
        else:
            for source_index in source_indices:
                fates[source_index] = 'unknown'
    return tuple(fates)


def defect_focus_points(
    mesh: RawVisualMesh,
    diagnostic: RawMeshDiagnosticResult,
    code: str,
) -> tuple[tuple[float, float, float], ...] | None:
    """Source-coordinate focus positions for a defect code.

    Positions only — derived from the same edge-map/canonicalization the
    diagnostics used, but carrying no verdict. ``None`` means the code's
    location is not derivable in one pass (``inverted_normal``,
    ``overlapping_face``, or an unrecognized code): the caller reports it
    'unknown' instead of guessing.
    """

    if code not in _FOCUSABLE_CODES:
        return None
    points = [(v.x, v.y, v.z) for v in mesh.vertices]
    if not mesh.triangles:
        return ()
    profile: RawMeshDiagnosticProfile = diagnostic.profile
    diagonal = sqrt(
        (max(p[0] for p in points) - min(p[0] for p in points)) ** 2
        + (max(p[1] for p in points) - min(p[1] for p in points)) ** 2
        + (max(p[2] for p in points) - min(p[2] for p in points)) ** 2
    )
    weld_tolerance = max(
        diagonal * profile.weld_relative_tolerance,
        profile.weld_absolute_floor_source_units,
    )
    canonical = [_quantized_key(p, weld_tolerance) for p in points]

    if code in ('open_boundary', 'watertightness', 'non_manifold_edge'):
        edge_faces: dict[tuple[tuple[int, int, int], tuple[int, int, int]], int] = {}
        for tri in mesh.triangles:
            keys = (canonical[tri.a], canonical[tri.b], canonical[tri.c])
            for start, end in ((0, 1), (1, 2), (2, 0)):
                left, right = keys[start], keys[end]
                if left == right:
                    continue
                edge_key = tuple(sorted((left, right)))
                edge_faces[edge_key] = edge_faces.get(edge_key, 0) + 1
        # Canonical edge key → first source vertex carrying it (edge
        # midpoints are approximate focus positions by design).
        first_vertex: dict[tuple[int, int, int], tuple[float, float, float]] = {}
        for key, point in zip(canonical, points):
            first_vertex.setdefault(key, point)
        focus: list[tuple[float, float, float]] = []
        for (ka, kb), count in edge_faces.items():
            if (code == 'non_manifold_edge' and count > 2) or (
                code in ('open_boundary', 'watertightness') and count == 1
            ):
                pa, pb = first_vertex[ka], first_vertex[kb]
                focus.append(((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2, (pa[2] + pb[2]) / 2))
        return tuple(focus)

    if code == 'duplicate_face':
        groups: dict[tuple[tuple[int, int, int], ...], list[int]] = {}
        for index, tri in enumerate(mesh.triangles):
            key = tuple(sorted((canonical[tri.a], canonical[tri.b], canonical[tri.c])))
            groups.setdefault(key, []).append(index)
        return tuple(
            _centroid(_triangle_points(points, mesh.triangles[index]))
            for indices in groups.values()
            if len(indices) > 1
            for index in indices[1:]
        )

    if code in ('sliver_face', 'tiny_feature'):
        tiny_length = max(
            diagonal * profile.tiny_feature_relative_size,
            profile.weld_absolute_floor_source_units,
        )
        tiny_area = tiny_length * tiny_length
        found: list[tuple[float, float, float]] = []
        for tri in mesh.triangles:
            tri_points = _triangle_points(points, tri)
            area = _triangle_area(tri_points)
            if code == 'tiny_feature':
                if area <= tiny_area:
                    found.append(_centroid(tri_points))
            else:
                lengths_sq = (
                    _distance_sq(tri_points[0], tri_points[1]),
                    _distance_sq(tri_points[1], tri_points[2]),
                    _distance_sq(tri_points[2], tri_points[0]),
                )
                denominator = sum(lengths_sq)
                quality = 0.0 if denominator <= 0.0 else (
                    4.0 * sqrt(3.0) * area / denominator
                )
                if quality < profile.sliver_quality_threshold:
                    found.append(_centroid(tri_points))
        return tuple(found)

    return None


def decimate_for_preview(
    vertices: Sequence[tuple[float, float, float]],
    triangles: Sequence[RawMeshTriangle],
    *,
    max_faces: int = PREVIEW_MAX_FACES,
) -> tuple[
    tuple[tuple[float, float, float], ...],
    tuple[tuple[int, int, int], ...],
    tuple[int, ...],
    bool,
]:
    """Deterministic stride decimation, display-only.

    Keeps every ``stride``-th triangle in source order (deterministic —
    the same mesh always decimates to the same preview), then re-indexes
    the referenced vertices in first-use order. Returns ``(vertices,
    faces, kept_source_indices, decimated)``. Never mutates or feeds back
    into the import data.
    """

    if len(triangles) <= max_faces:
        return (
            tuple(vertices),
            tuple((t.a, t.b, t.c) for t in triangles),
            tuple(range(len(triangles))),
            False,
        )
    stride = len(triangles) // max_faces + 1
    remap: dict[int, int] = {}
    new_vertices: list[tuple[float, float, float]] = []
    new_faces: list[tuple[int, int, int]] = []
    kept_indices: list[int] = []
    for index in range(0, len(triangles), stride):
        if len(new_faces) >= max_faces:
            break
        tri = triangles[index]
        remapped = []
        for vertex_index in (tri.a, tri.b, tri.c):
            target = remap.get(vertex_index)
            if target is None:
                target = len(new_vertices)
                remap[vertex_index] = target
                new_vertices.append(vertices[vertex_index])
            remapped.append(target)
        new_faces.append((remapped[0], remapped[1], remapped[2]))
        kept_indices.append(index)
    return tuple(new_vertices), tuple(new_faces), tuple(kept_indices), True


def build_import_preview_scene(
    mesh: RawVisualMesh,
    diagnostic: RawMeshDiagnosticResult,
    declaration: PreviewDeclaration,
    *,
    view_mode: PreviewViewMode = 'original',
    repaired: RepairedRawMesh | None = None,
    focus_codes: Sequence[str] = (),
    max_faces: int = PREVIEW_MAX_FACES,
) -> ImportPreviewScene:
    """Resolve everything the dialog's preview pane draws for one state.

    The mesh chosen by ``view_mode`` is transformed by the declared
    authority exactly like the commit transform (unit scale → axis
    rotation → anchor shift, anchor resolved on the previewed mesh's own
    bounds — the same rule the entity-body commit applies).
    """

    notices: list[str] = []
    if view_mode in ('repaired', 'diff') and repaired is None:
        return ImportPreviewScene(
            view_mode=view_mode,
            supported=False,
            unsupported_reason='no_repair_preview',
            coordinate_space='source_units',
            axis_convention_resolved=False,
            vertices=(),
            faces=(),
            source_fates=(),
            bbox_min=(0.0, 0.0, 0.0),
            bbox_max=(0.0, 0.0, 0.0),
            dims=(0.0, 0.0, 0.0),
            vertex_count_full=len(mesh.vertices),
            face_count_full=len(mesh.triangles),
            decimated=False,
            focuses=(),
            notices=(),
            kept_face_indices=(),
        )

    draw_vertices: tuple[RawMeshVertex, ...]
    draw_triangles: tuple[RawMeshTriangle, ...]
    fates: tuple[PreviewFaceFate, ...] = ()
    if view_mode == 'repaired' and repaired is not None:
        draw_vertices = repaired.vertices
        draw_triangles = repaired.triangles
    else:
        draw_vertices = mesh.vertices
        draw_triangles = mesh.triangles
    if view_mode == 'diff' and repaired is not None:
        fates = source_triangle_fates(mesh, repaired)

    source_points = [(v.x, v.y, v.z) for v in draw_vertices]
    spec_unit = format_declared_source_unit(mesh.provenance.asset_format)
    matrix, axis_resolved, decl_notices = resolve_preview_declaration(
        declaration, spec_unit=spec_unit, source_vertices=source_points
    )
    notices.extend(decl_notices)
    space: Literal['meters', 'source_units'] = 'meters' if matrix is not None else 'source_units'
    if matrix is not None:
        points = [_transform_point(matrix, p) for p in source_points]
    else:
        points = source_points

    if not points or not draw_triangles:
        return ImportPreviewScene(
            view_mode=view_mode,
            supported=False,
            unsupported_reason='unsupported_geometry',
            coordinate_space=space,
            axis_convention_resolved=axis_resolved,
            vertices=(),
            faces=(),
            source_fates=fates,
            bbox_min=(0.0, 0.0, 0.0),
            bbox_max=(0.0, 0.0, 0.0),
            dims=(0.0, 0.0, 0.0),
            vertex_count_full=len(draw_vertices),
            face_count_full=len(draw_triangles),
            decimated=False,
            focuses=(),
            notices=tuple(notices),
            kept_face_indices=(),
        )
    if any(not isfinite(c) for p in points for c in p):
        return ImportPreviewScene(
            view_mode=view_mode,
            supported=False,
            unsupported_reason='unsupported_geometry',
            coordinate_space=space,
            axis_convention_resolved=axis_resolved,
            vertices=(),
            faces=(),
            source_fates=fates,
            bbox_min=(0.0, 0.0, 0.0),
            bbox_max=(0.0, 0.0, 0.0),
            dims=(0.0, 0.0, 0.0),
            vertex_count_full=len(draw_vertices),
            face_count_full=len(draw_triangles),
            decimated=False,
            focuses=(),
            notices=tuple(notices),
            kept_face_indices=(),
        )

    vertices, faces, kept_indices, decimated = decimate_for_preview(
        points, draw_triangles, max_faces=max_faces
    )
    if decimated:
        notices.append('preview_decimated')

    xs = [p[0] for p in vertices]
    ys = [p[1] for p in vertices]
    zs = [p[2] for p in vertices]
    bbox_min = (min(xs), min(ys), min(zs))
    bbox_max = (max(xs), max(ys), max(zs))
    dims = (
        bbox_max[0] - bbox_min[0],
        bbox_max[1] - bbox_min[1],
        bbox_max[2] - bbox_min[2],
    )

    focuses: list[PreviewDefectFocus] = []
    for code in dict.fromkeys(focus_codes):
        located = defect_focus_points(mesh, diagnostic, code)
        if located is None:
            focuses.append(PreviewDefectFocus(code=code, state='unknown', points=()))
        else:
            transformed = (
                tuple(_transform_point(matrix, p) for p in located)
                if matrix is not None
                else located
            )
            focuses.append(
                PreviewDefectFocus(
                    code=code,
                    state='located' if transformed else 'unknown',
                    points=transformed,
                )
            )

    return ImportPreviewScene(
        view_mode=view_mode,
        supported=True,
        unsupported_reason=None,
        coordinate_space=space,
        axis_convention_resolved=axis_resolved,
        vertices=vertices,
        faces=faces,
        source_fates=fates,
        bbox_min=bbox_min,
        bbox_max=bbox_max,
        dims=dims,
        vertex_count_full=len(draw_vertices),
        face_count_full=len(draw_triangles),
        decimated=decimated,
        focuses=tuple(focuses),
        notices=tuple(notices),
        kept_face_indices=kept_indices,
    )


__all__ = [
    'ImportPreviewScene',
    'PREVIEW_MAX_FACES',
    'PreviewDeclaration',
    'PreviewDefectFocus',
    'PreviewFaceFate',
    'PreviewViewMode',
    'build_import_preview_scene',
    'decimate_for_preview',
    'defect_focus_points',
    'resolve_preview_declaration',
    'source_triangle_fates',
]
