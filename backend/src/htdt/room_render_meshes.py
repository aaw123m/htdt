"""pyvista render-mesh builders — Qt-free (#807 boundary refactor).

The document->PolyData builders (room shell, floor, grid, entity bodies,
semantic glyphs) are pure pyvista/shapely work: ``room_viewport`` imports
and re-exports them for its Qt surface, while domain modules that need
rendered geometry (review packaging) take them without the Qt dependency.
"""

from __future__ import annotations

from collections import OrderedDict
from math import isfinite

import numpy as np
import pyvista as pv
from shapely.geometry import Point, Polygon
from shapely.ops import triangulate

from .cad_room_authoring import compile_room_authoring_to_r120
from .cad_scene import (
    EntityBodyGeometry,
    SceneDocument,
    SceneEntity,
    domain_pose_to_render_matrix,
    room_vertices,
)
from .room_render_items import UnderlayRenderItem
from .ui_theme_tokens import DARK_THEME


def _room_wireframe(document: SceneDocument) -> pv.PolyData | None:
    room = document.room
    if room is None:
        return None
    vertices = room_vertices(room)
    count = len(vertices)
    # Issue #976: a sloped ceiling tilts the shell's top ring; the flat
    # rectangle outline would draw a ceiling that does not exist.
    model = document.room_authoring
    if model is not None and model.ceiling is not None:
        from .cad_room_authoring import ceiling_height_at

        ceiling_z = [ceiling_height_at(model, v.x_m, v.y_m) for v in vertices]
    else:
        ceiling_z = [float(room.height_m)] * count
    points = np.asarray(
        [(vertex.x_m, -vertex.y_m, 0.0) for vertex in vertices]
        + [(vertex.x_m, -vertex.y_m, z) for vertex, z in zip(vertices, ceiling_z)],
        dtype=float,
    )
    lines: list[int] = []
    for offset in (0, count):
        for index in range(count):
            lines.extend((2, offset + index, offset + ((index + 1) % count)))
    for index in range(count):
        lines.extend((2, index, count + index))
    mesh = pv.PolyData(points)
    mesh.lines = np.asarray(lines, dtype=np.int64)
    return mesh


# Issue #976: committed semantic primitives render as translucent solids on
# top of the base floor+shell, in the same muted palette as entity fills.
_AUTHORING_SURFACE_COLORS = {
    'ceiling': '#4A6FA5',
    'wall': '#5D6B7A',
    'opening': '#C98A3B',
    'riser': '#8A6D3B',
    'soffit': '#4A6FA5',
    'partial-wall': '#5C7457',
    'region': '#4D5D6C',
}


def _authoring_surface_kind(surface_key: str) -> str | None:
    """Primitive overlay kind for a compiled surface key, or None when the
    base floor/shell already covers it (plain floor and unsplit walls)."""

    if surface_key == 'ceiling':
        return 'ceiling'
    head = surface_key.split(':', 1)[0]
    if head == 'wall':
        tail = surface_key.rsplit(':', 1)[-1]
        if tail.isdigit():
            return None  # unsplit boundary wall — the room shell draws it
        return 'opening' if tail == 'opening' else 'wall'
    if head in _AUTHORING_SURFACE_COLORS:
        return head
    return None


def _newell_normal(points: np.ndarray) -> np.ndarray:
    normal = np.zeros(3)
    for index, point in enumerate(points):
        nxt = points[(index + 1) % len(points)]
        normal += np.asarray(
            (
                (point[1] - nxt[1]) * (point[2] + nxt[2]),
                (point[2] - nxt[2]) * (point[0] + nxt[0]),
                (point[0] - nxt[0]) * (point[1] + nxt[1]),
            )
        )
    return normal


def _planar_polygon_mesh(geometry, surface) -> pv.PolyData | None:
    """Triangulated render mesh for one compiled planar surface.

    Projects onto the dominant normal plane, triangulates with shapely (the
    floor surface carries riser holes), then lifts back into render space.
    """

    ring = surface.outer_vertex_indices
    outer = np.asarray([geometry.vertices[i].point() for i in ring], dtype=float)
    if len(outer) < 3:
        return None
    drop = int(np.argmax(np.abs(_newell_normal(outer))))
    keep = [axis for axis in range(3) if axis != drop]
    holes2d = [
        np.asarray([geometry.vertices[i].point() for i in hole])[:, keep]
        for hole in surface.hole_vertex_indices
    ]
    poly2d = Polygon(outer[:, keep], holes=holes2d or None)
    if not poly2d.is_valid or poly2d.area <= 1e-12:
        return None
    pool_index_by_2d = {
        (round(geometry.vertices[i].point()[keep[0]], 9), round(geometry.vertices[i].point()[keep[1]], 9)): i
        for i in (*ring, *sum(surface.hole_vertex_indices, ()))
    }
    vertex_ids = sorted({*ring, *sum(surface.hole_vertex_indices, ())})
    remap = {old: new for new, old in enumerate(vertex_ids)}
    points = np.asarray(
        [geometry.vertices[i].point() for i in vertex_ids], dtype=float
    )
    points[:, 1] *= -1.0  # domain +Y rear → render -Y
    faces: list[int] = []
    for triangle in triangulate(poly2d):
        if not poly2d.covers(triangle.representative_point()):
            continue
        ids = [
            pool_index_by_2d.get((round(x, 9), round(y, 9)))
            for x, y in triangle.exterior.coords[:-1]
        ]
        if len(ids) != 3 or any(idx is None for idx in ids):
            continue
        faces.extend((3, *(remap[idx] for idx in ids)))
    if not faces:
        return None
    return pv.PolyData(points, np.asarray(faces, dtype=np.int64))


def _semantic_surface_overlay_mesh(geometry, surface) -> pv.PolyData | None:
    """Render mesh for the triangle subset one semantic surface covers.

    Domain +Y rear maps to render -Y (same convention as
    ``_planar_polygon_mesh``).
    """

    triangle_ids = set(surface.triangle_ids)
    triangles = [
        triangle
        for triangle in geometry.triangles
        if triangle.triangle_id in triangle_ids
    ]
    if not triangles:
        return None
    vertex_ids = sorted(
        {
            index
            for triangle in triangles
            for index in (triangle.a, triangle.b, triangle.c)
        }
    )
    remap = {old: new for new, old in enumerate(vertex_ids)}
    points = np.asarray(
        [
            (
                geometry.vertices[i].x_m,
                -geometry.vertices[i].y_m,
                geometry.vertices[i].z_m,
            )
            for i in vertex_ids
        ],
        dtype=float,
    )
    faces: list[int] = []
    for triangle in triangles:
        faces.extend(
            (
                3,
                remap[triangle.a],
                remap[triangle.b],
                remap[triangle.c],
            )
        )
    return pv.PolyData(points, np.asarray(faces, dtype=np.int64))


def _iter_polygon_members(geometry) -> tuple:
    """Polygon members of any shapely geometry (collections recursed)."""

    if geometry is None or getattr(geometry, 'is_empty', True):
        return ()
    geom_type = geometry.geom_type
    if geom_type == 'Polygon':
        return (geometry,)
    if geom_type in ('MultiPolygon', 'GeometryCollection'):
        members: list = []
        for member in geometry.geoms:
            members.extend(_iter_polygon_members(member))
        return tuple(members)
    return ()


def _iter_linear_members(geometry) -> tuple:
    """LineString members of any shapely geometry (collections recursed)."""

    if geometry is None or getattr(geometry, 'is_empty', True):
        return ()
    geom_type = geometry.geom_type
    if geom_type == 'LineString':
        return (geometry,)
    if geom_type in ('MultiLineString', 'GeometryCollection'):
        members: list = []
        for member in geometry.geoms:
            members.extend(_iter_linear_members(member))
        return tuple(members)
    return ()


def _iter_point_members(geometry) -> tuple:
    """Point members of any shapely geometry (collections recursed)."""

    if geometry is None or getattr(geometry, 'is_empty', True):
        return ()
    geom_type = geometry.geom_type
    if geom_type == 'Point':
        return (geometry,)
    if geom_type in ('MultiPoint', 'GeometryCollection'):
        members: list = []
        for member in geometry.geoms:
            members.extend(_iter_point_members(member))
        return tuple(members)
    return ()


def _shapely_xy_fill_mesh(geometry, z_m: float) -> pv.PolyData | None:
    """Triangulated render mesh for a domain-XY shapely (multi)polygon.

    Mirrors ``_planar_polygon_mesh``: shapely triangulation can leak a
    triangle outside a concave ring, so each candidate must be covered by
    the source polygon. Domain +Y rear maps to render -Y.
    """

    points: list[tuple[float, float, float]] = []
    faces: list[int] = []
    for poly in _iter_polygon_members(geometry):
        for triangle in triangulate(poly):
            if not poly.covers(triangle.representative_point()):
                continue
            base = len(points)
            for x, y, *_ in list(triangle.exterior.coords)[:-1]:
                points.append((float(x), -float(y), z_m))
            faces.extend((3, base, base + 1, base + 2))
    if not faces:
        return None
    return pv.PolyData(
        np.asarray(points, dtype=float), np.asarray(faces, dtype=np.int64)
    )


def _xy_segments(coords) -> list:
    """Consecutive-pair segments of one ring/line coordinate sequence."""

    pts = [(float(x), float(y)) for x, y, *_ in coords]
    return list(zip(pts, pts[1:]))


def _segments_render_mesh(segments, z_m: float) -> pv.PolyData | None:
    """vtk lines for (a, b) domain-XY segment pairs at a fixed z."""

    if not segments:
        return None
    points: list[tuple[float, float, float]] = []
    lines: list[int] = []
    for (ax, ay), (bx, by) in segments:
        base = len(points)
        points.extend(((ax, -ay, z_m), (bx, -by, z_m)))
        lines.extend((2, base, base + 1))
    mesh = pv.PolyData(np.asarray(points, dtype=float))
    mesh.lines = np.asarray(lines, dtype=np.int64)
    return mesh


def _shapely_xy_outline_mesh(geometry, z_m: float) -> pv.PolyData | None:
    """Ring linework for (multi)polygons plus bare line members."""

    segments: list = []
    for poly in _iter_polygon_members(geometry):
        for ring in (poly.exterior, *poly.interiors):
            segments.extend(_xy_segments(ring.coords))
    for line in _iter_linear_members(geometry):
        segments.extend(_xy_segments(line.coords))
    return _segments_render_mesh(segments, z_m)


def _room_authoring_surface_meshes(
    document: SceneDocument,
) -> tuple[tuple[str, str, pv.PolyData], ...]:
    """Issue #976: compiled primitive surfaces of the committed authoring model.

    Flat ceilings and unsplit boundary walls stay with the base floor/shell —
    only shapes the base room cannot express render here. An uncompilable
    committed model renders as the plain room (the doc validator blocks such
    states; this is defensive, not a silent repair path).
    """

    model = document.room_authoring
    if model is None:
        return ()
    try:
        geometry = compile_room_authoring_to_r120(model)
    except ValueError:
        return ()
    meshes: list[tuple[str, str, pv.PolyData]] = []
    for surface in geometry.surfaces:
        kind = _authoring_surface_kind(surface.surface_key)
        if kind is None:
            continue
        if kind == 'ceiling' and model.ceiling is None:
            continue
        mesh = _planar_polygon_mesh(geometry, surface)
        if mesh is not None:
            meshes.append((surface.surface_key, kind, mesh))
    return tuple(meshes)


def _underlay_item_key(item: UnderlayRenderItem) -> tuple:
    """Content key for one frozen underlay item.

    Covers every field the rendered assets derive from plus the decoded
    image's identity — upstream memoizes decodes per blob sha256, so a
    stable array object means identical bytes (and a same-address new
    array can only come from the same content-addressed bytes anyway).
    """

    return (
        item.underlay_id,
        item.quad_domain,
        item.segments_domain,
        item.opacity,
        item.elevation_m,
        item.missing_source,
        id(item.image),
    )


def _room_floor_mesh(document: SceneDocument) -> pv.PolyData | None:
    room = document.room
    if room is None:
        return None
    vertices = room_vertices(room)
    if len(vertices) < 3:
        return None
    points = np.asarray(
        [(vertex.x_m, -vertex.y_m, 0.0) for vertex in vertices],
        dtype=float,
    )
    faces = np.asarray((len(vertices), *range(len(vertices))), dtype=np.int64)
    mesh = pv.PolyData(points, faces)
    return mesh.triangulate()


def _grid_mesh(
    document: SceneDocument,
    *,
    step_m: float = 0.5,
    z_m: float = 0.003,
) -> pv.PolyData | None:
    room = document.room
    if room is None:
        return None
    min_x, min_y, max_x, max_y = room.bounds_m
    margin = max(step_m * 2.0, 0.5)
    x0 = np.floor((min_x - margin) / step_m) * step_m
    x1 = np.ceil((max_x + margin) / step_m) * step_m
    y0 = np.floor((min_y - margin) / step_m) * step_m
    y1 = np.ceil((max_y + margin) / step_m) * step_m
    points: list[tuple[float, float, float]] = []
    lines: list[int] = []

    def add_line(start: tuple[float, float, float], end: tuple[float, float, float]) -> None:
        index = len(points)
        points.extend((start, end))
        lines.extend((2, index, index + 1))

    for x_m in np.arange(x0, x1 + step_m * 0.5, step_m):
        add_line(
            (float(x_m), float(-y0), z_m),
            (float(x_m), float(-y1), z_m),
        )
    for y_m in np.arange(y0, y1 + step_m * 0.5, step_m):
        add_line(
            (float(x0), float(-y_m), z_m),
            (float(x1), float(-y_m), z_m),
        )
    mesh = pv.PolyData(np.asarray(points, dtype=float))
    mesh.lines = np.asarray(lines, dtype=np.int64)
    return mesh


def _entity_envelope_mesh(entity: SceneEntity) -> pv.PolyData:
    """Bounding-envelope box at the entity pose (``size_m`` authority)."""

    assert entity.size_m is not None
    mesh = pv.Cube(
        center=(0.0, 0.0, 0.0),
        x_length=entity.size_m.x_m,
        y_length=entity.size_m.y_m,
        z_length=entity.size_m.z_m,
    )
    mesh.transform(
        np.asarray(domain_pose_to_render_matrix(entity.position, entity.orientation), dtype=float),
        inplace=True,
    )
    return mesh


def _footprint_prism_mesh(entity: SceneEntity, body: EntityBodyGeometry) -> pv.PolyData:
    """Extrude the entity-local XY footprint over the full ``size_m`` Z extent.

    Local mesh coordinates are render-local (domain Y negated); the pose
    matrix applies the C4-conjugated domain transform.
    """

    assert entity.size_m is not None
    assert body.footprint_vertices is not None
    half_z = float(entity.size_m.z_m) * 0.5
    vertices = body.footprint_vertices
    count = len(vertices)
    points = np.asarray(
        [
            (float(vertex.x_m), -float(vertex.y_m), -half_z)
            for vertex in vertices
        ],
        dtype=float,
    )
    base = pv.PolyData(points, np.asarray([count, *range(count)], dtype=np.int64))
    return base.extrude((0.0, 0.0, float(entity.size_m.z_m)), capping=True)


def _mesh_asset_mesh(body: EntityBodyGeometry) -> pv.PolyData:
    """Entity-local imported triangle mesh (domain frame → render-local)."""

    assert body.mesh is not None
    mesh = body.mesh
    offset = mesh.local_offset_m
    scale = float(mesh.uniform_scale)
    points = np.asarray(
        [
            (
                float(vertex.x_m) * scale + offset.x_m,
                -(float(vertex.y_m) * scale + offset.y_m),
                float(vertex.z_m) * scale + offset.z_m,
            )
            for vertex in mesh.vertices
        ],
        dtype=float,
    )
    faces = np.asarray(
        [[3, triangle.a, triangle.b, triangle.c] for triangle in mesh.triangles],
        dtype=np.int64,
    ).ravel()
    return pv.PolyData(points, faces)


def _entity_local_mesh(entity: SceneEntity) -> pv.PolyData | None:
    """Authored non-envelope body mesh in entity-local render coordinates.

    Returns ``None`` when the entity has no explicit non-box body geometry —
    callers then use the ``size_m`` envelope box. The pose transform is left
    to the caller so legacy editors with their own transform pipeline can
    reuse the same shape construction (issue #464).
    """

    body = entity.body_geometry
    if entity.size_m is None or body is None:
        return None
    if body.kind == 'cylinder' and body.radius_m is not None:
        return pv.Cylinder(
            center=(0.0, 0.0, 0.0),
            direction=(0.0, 0.0, 1.0),
            radius=float(body.radius_m),
            height=float(entity.size_m.z_m),
            resolution=48,
        )
    if body.kind == 'extruded_polygon' and body.footprint_vertices:
        return _footprint_prism_mesh(entity, body)
    if body.kind == 'mesh_asset' and body.mesh is not None:
        return _mesh_asset_mesh(body)
    return None


def _entity_mesh(entity: SceneEntity) -> pv.PolyData:
    """Render the authored body geometry; fall back to the bounding envelope.

    ``size_m`` remains the broad-phase envelope; ``body_geometry`` refines the
    displayed/collided shape. Mesh assets render their local-coordinate mesh;
    a missing/invalid body degrades to the envelope box.
    """

    if entity.size_m is None:
        mesh = pv.Sphere(radius=0.08)
    else:
        mesh = _entity_local_mesh(entity)
        if mesh is None:
            return _entity_envelope_mesh(entity)
    mesh.transform(
        np.asarray(domain_pose_to_render_matrix(entity.position, entity.orientation), dtype=float),
        inplace=True,
    )
    return mesh


# -- semantic visual language (#572) ------------------------------------------
#
# Entity kinds map onto a small set of functional categories. Each category has
# a muted theme color (``viewport.categories``) and, for the kinds that need to
# read at a glance, a low-cost glyph proxy built in entity-local render
# coordinates. The authored ``size_m`` envelope stays the geometric authority:
# semantic actors are render-only extras layered on (or inside) that envelope,
# and they remain pickable and mapped back to the entity id like the envelope.

_SEMANTIC_CATEGORY_BY_KIND: dict[str, str] = {
    'speaker': 'source',
    'seat': 'listener',
    'screen': 'display',
    'display': 'display',
    'projector': 'display',
    'av_equipment': 'infrastructure',
    'riser': 'architecture',
    'furniture': 'architecture',
    'measurement_point': 'reference',
    # 'treatment' is reserved for a future entity kind; the palette slot exists
    # so treatment surfaces keep the same muted-mauve identity when they land.
}

# Category labels mirror room_workspace KIND_LABELS wording for the kinds that
# actually appear in the legend.
_CATEGORY_LEGEND_LABELS: dict[str, str] = {
    'architecture': '建築・家具',
    'source': 'スピーカー',
    'listener': '座席・受聴位置',
    'display': 'スクリーン・映像',
    'treatment': '音響処理',
    'infrastructure': '機器・ラック',
    'reference': '測定点',
}


def _entity_category(entity: SceneEntity) -> str:
    return _SEMANTIC_CATEGORY_BY_KIND.get(entity.kind, 'architecture')


def _category_color(category: str) -> str:
    return getattr(DARK_THEME.viewport.categories, category).hex


def _shade_hex(color: str, factor: float) -> str:
    """Darken a ``#RRGGBB`` hex color toward black by ``factor`` (0..1)."""

    value = int(color.lstrip('#'), 16)
    r = int(((value >> 16) & 0xFF) * factor)
    g = int(((value >> 8) & 0xFF) * factor)
    b = int((value & 0xFF) * factor)
    return f'#{r:02X}{g:02X}{b:02X}'


def _semantic_glyph_local_meshes(entity: SceneEntity) -> tuple[pv.PolyData, ...]:
    """Semantic glyph proxies in entity-local render coordinates.

    Local frame convention matches ``_entity_local_mesh``: x = local +X,
    y = -local Y (so the entity front face sits at y = -dy/2), z = local +Z.
    The pose transform is applied by the caller so these stay cheap template
    meshes that never touch placement/physics state.
    """

    size = entity.size_m
    if size is None:
        return ()
    x_m, y_m, z_m = float(size.x_m), float(size.y_m), float(size.z_m)
    front_y = -y_m * 0.5
    back_y = y_m * 0.5
    if entity.kind == 'speaker':
        # Baffle plate proud of the front face + a driver disc — the cabinet
        # reads as "speaker" from any angle without needing the front ray.
        depth = min(0.014, max(y_m * 0.05, 0.005))
        plate = pv.Cube(
            center=(0.0, front_y - depth * 0.5 - 0.001, 0.0),
            x_length=x_m * 0.82,
            y_length=depth,
            z_length=z_m * 0.82,
        )
        driver = pv.Disc(
            center=(0.0, front_y - depth - 0.0025, 0.0),
            inner=0.0,
            outer=min(x_m, z_m) * 0.20,
            normal=(0.0, -1.0, 0.0),
            r_res=28,
        )
        return (plate, driver)
    if entity.kind in ('screen', 'display'):
        # Image face inset into the front — a frame-thin proud panel.
        depth = min(0.012, max(y_m * 0.12, 0.006))
        panel = pv.Cube(
            center=(0.0, front_y - depth * 0.5 - 0.001, 0.0),
            x_length=x_m * 0.94,
            y_length=depth,
            z_length=z_m * 0.90,
        )
        return (panel,)
    if entity.kind == 'projector':
        # Lens barrel protruding from the front face.
        radius = min(x_m, z_m) * 0.16
        lens = pv.Cylinder(
            center=(0.0, front_y - 0.018, z_m * 0.08),
            direction=(0.0, -1.0, 0.0),
            radius=max(radius, 0.012),
            height=0.036,
            resolution=24,
        )
        return (lens,)
    if entity.kind == 'seat':
        # Backrest slab rising at the rear (+local Y / render +Y) reads as a
        # seat silhouette in plan and perspective views.
        thick = max(y_m * 0.14, 0.025)
        slab = pv.Cube(
            center=(0.0, back_y - thick * 0.5, z_m * 0.18),
            x_length=x_m * 0.92,
            y_length=thick,
            z_length=z_m * 0.58,
        )
        return (slab,)
    if entity.kind == 'av_equipment':
        # Rack/rack-stack read: three equipment shelf slats on the front face.
        depth = min(0.016, max(y_m * 0.06, 0.008))
        slats = [
            pv.Cube(
                center=(0.0, front_y - depth * 0.5 - 0.001, z_m * frac),
                x_length=x_m * 0.78,
                y_length=depth,
                z_length=max(z_m * 0.11, 0.02),
            )
            for frac in (-0.27, 0.0, 0.27)
        ]
        return tuple(slats)
    return ()


def _semantic_marker_local_meshes(entity: SceneEntity) -> tuple[pv.PolyData, ...]:
    """Measurement/reference marker: an axis crosshair + a small centre bead."""

    if entity.kind != 'measurement_point':
        return ()
    half = 0.11
    cross = pv.PolyData(
        np.asarray(
            [
                (-half, 0.0, 0.0),
                (half, 0.0, 0.0),
                (0.0, -half, 0.0),
                (0.0, half, 0.0),
                (0.0, 0.0, -half),
                (0.0, 0.0, half),
            ],
            dtype=float,
        )
    )
    cross.lines = np.asarray(
        [2, 0, 1, 2, 2, 3, 2, 4, 5],
        dtype=np.int64,
    )
    bead = pv.Sphere(radius=0.028)
    return (cross, bead)


def semantic_entity_meshes(entity: SceneEntity) -> tuple[pv.PolyData, ...]:
    """Render-only semantic proxies for ``entity``, posed to its transform.

    An empty tuple means the kind relies on its envelope alone (architecture
    category). Every returned mesh is safe to use as a pickable extra actor —
    it carries no state and is rebuilt fresh each render.
    """

    local = (
        _semantic_glyph_local_meshes(entity)
        + _semantic_marker_local_meshes(entity)
    )
    if not local:
        return ()
    matrix = np.asarray(
        domain_pose_to_render_matrix(entity.position, entity.orientation),
        dtype=float,
    )
    meshes = []
    for mesh in local:
        mesh.transform(matrix, inplace=True)
        meshes.append(mesh)
    return tuple(meshes)


# Entity meshes are pure functions of the immutable SceneEntity, and a
# workspace refresh re-renders the whole document several times per
# navigation plus once per interactive edit. Memoizing by content hash means
# only the entities that actually changed get rebuilt; the cap bounds memory
# for pathological scenes (each entry is a handful of small PolyData).
_ENTITY_MESH_CACHE_LIMIT = 2048
_entity_mesh_cache: OrderedDict[
    tuple[str, str],
    tuple[pv.PolyData, tuple[pv.PolyData, ...], pv.PolyData | None],
] = OrderedDict()


def entity_render_meshes(
    entity: SceneEntity,
) -> tuple[pv.PolyData, tuple[pv.PolyData, ...], pv.PolyData | None]:
    """(body mesh, semantic glyphs, non-box envelope wireframe) for ``entity``.

    Callers may clip/section the returned meshes but must not mutate them —
    the same objects are shared across viewports and repeat renders.
    """

    # model_dump_json rather than hash(): SemanticCapabilityBinding carries a
    # dict field, which makes the model unhashable.
    key = (entity.entity_id, entity.model_dump_json())
    cached = _entity_mesh_cache.pop(key, None)
    if cached is not None:
        _entity_mesh_cache[key] = cached
        return cached
    body = _entity_mesh(entity)
    glyphs = semantic_entity_meshes(entity)
    envelope = (
        _entity_envelope_mesh(entity)
        if entity.size_m is not None
        and entity.body_geometry is not None
        and entity.body_geometry.kind != 'box'
        else None
    )
    entry = (body, glyphs, envelope)
    _entity_mesh_cache[key] = entry
    while len(_entity_mesh_cache) > _ENTITY_MESH_CACHE_LIMIT:
        _entity_mesh_cache.popitem(last=False)
    return entry

__all__ = [
    'entity_render_meshes',
    'semantic_entity_meshes',
]
