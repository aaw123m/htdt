from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from math import hypot, isfinite, radians, tan
from typing import TYPE_CHECKING, Iterator, Sequence

import numpy as np
import pyvista as pv
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QLabel, QRubberBand, QVBoxLayout, QWidget
from pyvistaqt import QtInteractor
from shapely.geometry import Point, Polygon
from shapely.ops import triangulate

from .cad_prediction_models import CadPredictionResult
from .cad_room_authoring import compile_room_authoring_to_r120
from .prediction_interpretation import PredictionSpatialLink
from .cad_view_state import (
    CUSTOM_VIEW,
    ORTHOGRAPHIC_VIEWS,
    STANDARD_VIEW_GEOMETRY,
    RoomCameraState,
    SectionPlaneState,
    StandardView,
)
from .cad_scene import (
    EntityBodyGeometry,
    PHYSICAL_ENTITY_KINDS,
    Position3,
    SceneDocument,
    SceneEntity,
    acoustic_reference_position,
    domain_pose_to_render_matrix,
    domain_to_render,
    quaternion_to_matrix3,
    room_vertices,
    scene_content_hash,
)
from .room_lighting_preview import (
    LIGHTING_NEUTRAL_ANCHOR_COLOR,
    LIGHTING_PREVIEW_DISCLAIMER,
    LIGHTING_STAGE_VOCAB,
    LIGHTING_UNKNOWN_STAGE_COLOR,
    LIGHTING_ZONE_VOCAB,
    LightingScenePreview,
    stage_bead_label,
)
from .room_operational_clearance import (
    OPERATIONAL_CONFLICT_COLOR,
    OPERATIONAL_UNDECLARED_COLOR,
    OPERATIONAL_UNKNOWN_ZONE_COLOR,
    OPERATIONAL_ZONE_KIND_VOCAB,
    OperationalClearancePreview,
)
from .design_ab_overlay import (
    AB_OVERLAY_CATEGORY_VOCAB,
    AB_OVERLAY_CONTEXT_COLOR,
    DesignAbOverlayPreview,
)
from .room_directivity_overlay import (
    DIRECTIVITY_BLOCKED_COLOR,
    DIRECTIVITY_UNKNOWN_COLOR,
)
from .installation_feasibility_viewmodel import (
    FEASIBILITY_VERDICT_VOCAB,
    InstallationFeasibilityPreview,
    SlabMountFace,
    WallMountFace,
)
from .ui_theme import (
    DARK_THEME,
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)

if TYPE_CHECKING:
    from .reflection_guidance_presentation import (
        ReflectionGuidanceOverlayMarker,
    )


#: Camera framing used when the scene has no usable bounds yet — a
#: typical-room floor patch (x/y ±5 m, z 0–3 m). On an empty document VTK's
#: bounds are degenerate (~1e-9 m extent); framing them leaves the floor so
#: small that sketch clicks land at nanometre scale and first-run room
#: drawing is effectively impossible (REV34-DIALOGUX).
DEFAULT_EMPTY_SCENE_BOUNDS = (-5.0, 5.0, -5.0, 5.0, 0.0, 3.0)


#: Bounds tuple VTK reports for a renderer with no scene actors — the
#: sentinel for "empty scene" alongside degenerate extents.
_VTK_EMPTY_SCENE_BOUNDS = (-1.0, 1.0, -1.0, 1.0, -1.0, 1.0)


def scene_has_usable_bounds(plotter) -> bool:
    """Whether the plotter's scene bounds are usable for camera framing.

    An empty renderer reports the exact ±1 sentinel
    (:data:`_VTK_EMPTY_SCENE_BOUNDS`), and overlay-only scenes can report a
    degenerate ~1e-9 m extent; neither may be used to frame the camera.
    """
    bounds = getattr(plotter, 'bounds', None)
    if bounds is None or len(bounds) < 6:
        return False
    if all(abs(float(a) - b) < 1e-9 for a, b in zip(bounds, _VTK_EMPTY_SCENE_BOUNDS)):
        return False
    extent = float(
        max(
            bounds[1] - bounds[0],
            bounds[3] - bounds[2],
            bounds[5] - bounds[4],
        )
    )
    return isfinite(extent) and extent >= 0.1


def reset_camera_or_floor_default(plotter) -> None:
    """``plotter.reset_camera()`` that stays drawable on an empty scene.

    When the scene has no usable bounds (see :func:`scene_has_usable_bounds`)
    the camera frames :data:`DEFAULT_EMPTY_SCENE_BOUNDS` instead; every other
    scene keeps VTK's own reset.
    """
    if scene_has_usable_bounds(plotter):
        plotter.reset_camera()
    else:
        plotter.reset_camera(bounds=DEFAULT_EMPTY_SCENE_BOUNDS)


@dataclass(frozen=True, slots=True)
class RoomOverlayState:
    grid: bool = True
    labels: bool = False
    acoustics: bool = False
    focus_selection: bool = False
    hidden_ids: frozenset[str] = frozenset()
    guides_visible: bool = True
    # #1013: read-only lighting-scene preview (desired/commanded/read-back/
    # measured state glyphs on exact-bound fixtures). Explanation symbols
    # only — never a photometric render.
    lighting_scene: bool = False
    # #1010: read-only 運用クリアランス layer — declared operational-zone
    # XY footprints + operational_clearance_conflicts highlights.
    operational_clearance: bool = False
    # Zone-kind filter for the clearance layer; defaults to every kind.
    operational_zone_kinds: frozenset[str] = frozenset(OPERATIONAL_ZONE_KIND_VOCAB)
    # #1004: read-only as-built survey overlay — None or one of
    # ('tier', 'uncertainty_mm', 'verification', 'delta_mm').
    survey_mode: str | None = None


@dataclass(frozen=True, slots=True)
class UnderlayRenderItem:
    """One floor-plan underlay resolved for rendering (#534).

    All geometry is in domain coordinates; the viewport maps to render space.
    ``quad_domain`` is the 4-corner raster quad (``None`` for vector-only
    underlays), ``image`` a uint8 HxWx3/4 array consumed by ``pv.Texture``,
    and ``segments_domain`` optional line segments (DXF vectors).
    """

    underlay_id: str
    name: str
    quad_domain: tuple[tuple[float, float, float], ...] | None
    image: np.ndarray | None
    segments_domain: tuple[tuple[tuple[float, float, float], tuple[float, float, float]], ...]
    opacity: float
    elevation_m: float
    # True when the record references a blob the store no longer carries —
    # the underlay renders empty, so surfaces must label it missing rather
    # than present it as a normal-but-blank underlay.
    missing_source: bool = False


@dataclass(frozen=True, slots=True)
class GuideRenderItem:
    """One construction guide line segment in domain coordinates (#618)."""

    start: tuple[float, float, float]
    end: tuple[float, float, float]
    label: str | None = None


@dataclass(frozen=True, slots=True)
class CableRouteEndpointItem:
    """One cable-run endpoint resolved for overlay rendering (#1011).

    ``position`` is a domain-coordinate (x, y, z) metres triple — present
    only when the endpoint is exactly bound to an entity that exists in
    the current head document (status ``'bound'``).
    """

    label: str
    position: tuple[float, float, float] | None
    status: str  # 'bound' | 'unbound' | 'missing'


@dataclass(frozen=True, slots=True)
class CableRouteSegmentRouteItem:
    """Recorded waypoint polyline for one segment, domain coordinates."""

    segment_sequence: int
    points: tuple[tuple[float, float, float], ...]


@dataclass(frozen=True, slots=True)
class CableRouteOverlayItem:
    """One cable run resolved for the honest route overlay (#1011).

    ``route_state`` drives what may be drawn: ``'unregistered'`` renders
    bound endpoints plus the unregistered-route label and never a line;
    ``'registered'`` may connect only the explicitly recorded waypoints of
    ``waypoint_segments``; ``'stale'`` draws endpoints and a stale label —
    the recorded line is never presented as the current route.
    """

    run_id: str
    label: str
    route_state: str  # 'unregistered' | 'registered' | 'stale'
    from_endpoint: CableRouteEndpointItem
    to_endpoint: CableRouteEndpointItem
    waypoint_segments: tuple[CableRouteSegmentRouteItem, ...] = ()
    record_kind: str = 'design'  # 'design' | 'as_built'


def _domain_to_render_tuple(point: tuple[float, float, float]) -> tuple[float, float, float]:
    """Map a domain (x, y, z) metres triple into VTK's right-handed world."""

    return (point[0], -point[1], point[2])


def _ray_room_boundary_domain(
    near: Sequence[float],
    far: Sequence[float],
    document: SceneDocument | None,
) -> tuple[float, float, float] | None:
    """First room-boundary hit along a domain-space click ray (#1011 M3).

    Waypoint recording fallback: when no pickable actor lies under the
    cursor, the honest "point on the room" is the nearest boundary plane
    the ray actually meets — the floor (z=0), one of the footprint walls
    (each vertex edge extruded to the room height), or the ceiling plane
    (z=height_m — for sloped ceilings the declared envelope plane, never
    a fabricated sloped hit). The smallest positive ray parameter wins;
    a ray that misses every boundary returns None.
    """

    room = getattr(document, 'room', None) if document is not None else None
    if room is None:
        return None
    try:
        origin = np.asarray(near, dtype=float).reshape(-1)[:3]
        direction = np.asarray(far, dtype=float).reshape(-1)[:3] - origin
    except Exception:
        return None
    if not np.all(np.isfinite(direction)) or not np.any(direction):
        return None

    vertices = room_vertices(room)
    height = float(room.height_m)
    hits: list[tuple[float, np.ndarray]] = []

    def _push(t: float) -> None:
        if isfinite(t) and t > 0.0:
            hits.append((t, origin + t * direction))

    # Floor / ceiling planes: accept only hits inside the footprint.
    footprint = Polygon(
        [(float(vertex.x_m), float(vertex.y_m)) for vertex in vertices]
    )
    for plane_z in (0.0, height):
        if abs(direction[2]) > 1e-12:
            t = (plane_z - origin[2]) / direction[2]
            if isfinite(t) and t > 0.0:
                point = origin + t * direction
                # covers() is boundary-inclusive — a ray landing exactly
                # on a wall edge still resolves to the floor/ceiling.
                if footprint.covers(Point(point[0], point[1])):
                    _push(t)
    # Footprint walls: each vertex edge extruded vertically; the hit must
    # land inside the edge's span and below the ceiling.
    for index, start in enumerate(vertices):
        end = vertices[(index + 1) % len(vertices)]
        edge = np.asarray(
            (float(end.x_m) - float(start.x_m),
             float(end.y_m) - float(start.y_m)),
            dtype=float,
        )
        edge_len = float(np.hypot(*edge))
        if edge_len < 1e-12:
            continue
        normal = np.asarray((edge[1], -edge[0]), dtype=float) / edge_len
        denom = float(normal[0] * direction[0] + normal[1] * direction[1])
        if abs(denom) < 1e-12:
            continue
        offset = (
            normal[0] * (float(start.x_m) - origin[0])
            + normal[1] * (float(start.y_m) - origin[1])
        )
        t = float(offset / denom)
        if not (isfinite(t) and t > 0.0):
            continue
        point = origin + t * direction
        along = (
            (point[0] - float(start.x_m)) * edge[0]
            + (point[1] - float(start.y_m)) * edge[1]
        ) / (edge_len * edge_len)
        if (
            -1e-9 <= along <= 1.0 + 1e-9
            and -1e-9 <= point[2] <= height + 1e-9
        ):
            _push(t)
    if not hits:
        return None
    hits.sort(key=lambda item: item[0])
    point = hits[0][1]
    return (float(point[0]), float(point[1]), float(point[2]))


_SELECTION_FORWARD_RAY_LENGTH_M = 0.6
_SELECTION_AIM_RAY_LENGTH_M = 1.2


@dataclass(frozen=True, slots=True)
class SelectionDirectionRay:
    """One rendered direction cue for the selected entity.

    ``role`` is 'forward' (physical body front, local +Y under the persisted
    quaternion) or 'aim' (independent speaker acoustic aim). The two are
    distinct authorities: a divergent speaker shows both rays in different
    colors so toe-in vs acoustic aim can be compared at a glance.
    """

    mesh: pv.PolyData
    color: str
    role: str


def _selection_direction_rays(entity: SceneEntity) -> tuple[SelectionDirectionRay, ...]:
    """Body-forward and acoustic-aim rays for the selected physical entity.

    Body forward always renders for physical entities; the acoustic aim ray is
    drawn only when the speaker's aim is explicitly known (aim_xyz is not
    None) — an unknown aim is never visualized as a guessed direction.
    """

    if entity.kind not in PHYSICAL_ENTITY_KINDS:
        return ()
    origin = acoustic_reference_position(entity) or entity.position
    matrix = quaternion_to_matrix3(entity.orientation)
    forward = (matrix[0][1], matrix[1][1], matrix[2][1])  # local +Y front axis
    forward_end = Position3(
        x_m=origin.x_m + forward[0] * _SELECTION_FORWARD_RAY_LENGTH_M,
        y_m=origin.y_m + forward[1] * _SELECTION_FORWARD_RAY_LENGTH_M,
        z_m=origin.z_m + forward[2] * _SELECTION_FORWARD_RAY_LENGTH_M,
    )
    rays = [
        SelectionDirectionRay(
            mesh=pv.Line(domain_to_render(origin), domain_to_render(forward_end)),
            color=DARK_THEME.semantic.warning.hex,
            role="forward",
        )
    ]
    if entity.kind == "speaker" and entity.aim_xyz is not None:
        aim_end = Position3(
            x_m=origin.x_m + entity.aim_xyz.x * _SELECTION_AIM_RAY_LENGTH_M,
            y_m=origin.y_m + entity.aim_xyz.y * _SELECTION_AIM_RAY_LENGTH_M,
            z_m=origin.z_m + entity.aim_xyz.z * _SELECTION_AIM_RAY_LENGTH_M,
        )
        rays.append(
            SelectionDirectionRay(
                mesh=pv.Line(domain_to_render(origin), domain_to_render(aim_end)),
                color=DARK_THEME.accent.primary.hex,
                role="aim",
            )
        )
    return tuple(rays)


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


# -- overlapping-pick chooser (#983) --------------------------------------------
#
# When one click resolves more than one entity (front-to-back via
# ``vtkCellPicker.GetProp3Ds``) the chooser below makes the otherwise invisible
# click-through cycle explicit: a cursor-side list of ``{i}. {name} · {kind}``
# rows. Candidates stay keyed by stable entity id; locked/hidden/missing or
# document-wide non-editable state is spelled out per row — selection is not
# edit permission. The set is never re-picked: it reuses the prop list of the
# pick that fired ``_picked_actor``, and it is dropped whenever the view or
# scene changes in a way that would invalidate that stack.

#: Kind wording mirrors ``room_workspace.SelectionInspector.KIND_LABELS`` so
#: the chooser reads identically to the objects list (#978) it syncs with.
_PICK_KIND_LABELS: dict[str, str] = {
    'speaker': 'スピーカー',
    'seat': '座席',
    'screen': 'スクリーン',
    'display': 'ディスプレイ',
    'projector': 'プロジェクター',
    'riser': 'ライザー',
    'furniture': '家具',
    'av_equipment': 'AV機器',
    'measurement_point': '測定点',
}


@dataclass(frozen=True, slots=True)
class PickCandidateEntry:
    """One row of the overlapping-pick chooser (#983).

    ``reason`` is the explicit "cannot operate" text — locked, hidden,
    missing from the scene, or blocked by the workspace's edit authority.
    A reason never disables selection itself: selection ≠ edit permission.
    """

    entity_id: str
    name: str
    kind: str
    kind_label: str
    reason: str | None = None


class _PickCandidateRow(QLabel):
    """One clickable chooser row — menu semantics, single click confirms."""

    clicked = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName('pickCandidateRow')
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _PickCandidatePopover(QFrame):
    """Cursor-side chooser for overlapping pick candidates (#983).

    Presentational only — the viewport owns the candidate set, cycling and
    confirm/dismiss. Rows are rebuilt per update so window, highlight and
    reason text always match the live entry list. ``activated`` carries the
    entity id of a row that was clicked (menu semantics: click = confirm).

    It is a real ``Qt::ToolTip`` window, not an alien child of the
    interactor: the VTK render HWND swallows clicks aimed at plain child
    widgets on Windows (verified on the real GUI — row clicks picked
    entities *behind* the popover). ToolTip windows sit on top and never
    activate, so keyboard focus stays on the interactor where the chooser's
    key filter lives, and clicks outside still reach the viewport as
    normal picks (which then re-anchor or dismiss the chooser).
    """

    activated = Signal(object)
    step_requested = Signal(int)
    _MAX_ROWS = 8

    def __init__(self, parent: QWidget) -> None:
        super().__init__(
            parent,
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setObjectName('pickCandidatePopover')
        set_surface_role(self, SurfaceRole.RAISED)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName('重なり候補の選択')
        self._entries: tuple[PickCandidateEntry, ...] = ()
        self._index = 0
        self._rows: list[_PickCandidateRow] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        self.header = QLabel(self)
        self.header.setObjectName('pickCandidateHeader')
        set_typography_role(self.header, TypographyRole.SECTION_TITLE)
        layout.addWidget(self.header)
        self._layout = layout
        self.hint = QLabel(
            'ホイール・↑↓: 切替 / Enter: 確定 / Esc: 閉じる', self
        )
        self.hint.setObjectName('pickCandidateHint')
        self.hint.setAccessibleName('操作: ホイールか上下キーで切替、Enterで確定、Escで閉じる')
        set_typography_role(self.hint, TypographyRole.SECONDARY)
        self.hide()

    def set_entries(
        self, entries: tuple[PickCandidateEntry, ...], index: int
    ) -> None:
        self._entries = tuple(entries)
        self._index = index
        self._refill()

    def set_index(self, index: int) -> None:
        self._index = index
        self._refill()

    def _refill(self) -> None:
        for row in self._rows:
            self._layout.removeWidget(row)
            row.deleteLater()
        self._rows = []
        entries = self._entries
        total = len(entries)
        self.header.setText(
            f'重なり候補 {self._index + 1 if total else 0}/{total}'
        )
        # A long hit stack renders as a window around the active row so the
        # chooser stays small on dense scenes; ellipses mark hidden ends.
        start = 0
        if total > self._MAX_ROWS:
            start = max(0, min(self._index - 3, total - self._MAX_ROWS))
        stop = min(total, start + self._MAX_ROWS)
        visible: list[tuple[int, PickCandidateEntry | None]] = []
        if start > 0:
            visible.append((-1, None))
        visible.extend((i, entries[i]) for i in range(start, stop))
        if stop < total:
            visible.append((-1, None))
        for offset, (entry_index, entry) in enumerate(visible):
            row = _PickCandidateRow(self)
            if entry is None:
                row.setText('…')
                row.setEnabled(False)
                row.setAccessibleName('その他の候補')
            else:
                full = f'{entry_index + 1}. {entry.name}'
                if entry.kind_label:
                    full += f' · {entry.kind_label}'
                if entry.reason:
                    full += f' · {entry.reason}'
                row.setText(
                    row.fontMetrics().elidedText(
                        full, Qt.TextElideMode.ElideRight, 260
                    )
                )
                row.setToolTip(full)
                row.setAccessibleName(full)
                set_typography_role(row, TypographyRole.BODY)
                entity_id = entry.entity_id
                row.clicked.connect(
                    lambda _checked=False, _eid=entity_id: self.activated.emit(_eid)
                )
                if entry_index == self._index:
                    set_semantic_state(row, SemanticState.SELECTED)
            self._layout.insertWidget(1 + offset, row)
            self._rows.append(row)
        self._layout.addWidget(self.hint)
        self.adjustSize()

    def wheelEvent(self, event) -> None:  # noqa: N802
        # Wheel over the popover itself goes to this window (the interactor
        # filter covers wheel over the viewport); same scroll direction.
        delta = event.angleDelta().y()
        if delta:
            self.step_requested.emit(-1 if delta > 0 else 1)
            event.accept()
            return
        super().wheelEvent(event)


class _PickCandidateKeyFilter(QObject):
    """Keyboard/wheel contract while the pick chooser is open (#983).

    Installed on the interactor only while the popover is visible — last
    installed, so it runs ahead of the camera/transform/gesture filters
    attached at workspace wiring time. Chooser keys are also accepted as
    ``ShortcutOverride`` so the workspace cancel/commit shortcuts cannot
    fire underneath it; every other key falls through unchanged.
    """

    _CHOOSER_KEYS = frozenset(
        {
            Qt.Key.Key_Escape,
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Tab,
            Qt.Key.Key_Backtab,
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
        }
    )

    def __init__(self, viewport: 'RoomViewport3D') -> None:
        super().__init__(viewport)
        self._viewport = viewport

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        viewport = self._viewport
        event_type = event.type()
        if event_type == QEvent.Type.ShortcutOverride:
            if event.key() in self._CHOOSER_KEYS:
                event.accept()
                return True
            return False
        if event_type == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Up or key == Qt.Key.Key_Backtab:
                viewport._step_pick_candidates(-1)
            elif key == Qt.Key.Key_Down or key == Qt.Key.Key_Tab:
                viewport._step_pick_candidates(1)
            elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                viewport._confirm_pick_candidate()
            elif key == Qt.Key.Key_Escape:
                viewport.dismiss_pick_candidates()
            else:
                return False
            event.accept()
            return True
        if event_type == QEvent.Type.Wheel:
            delta = event.angleDelta().y()
            if delta:
                # Scroll-up moves toward the front-most candidate, scroll-down
                # toward the back — a list scroll, not a camera zoom.
                viewport._step_pick_candidates(-1 if delta > 0 else 1)
            event.accept()
            return True
        if event_type in (QEvent.Type.FocusOut, QEvent.Type.Hide):
            # Keyboard ownership is void once the interactor loses focus or
            # is hidden (workspace switch) — the chooser closes rather than
            # floating over state it can no longer steer. BUT: on Windows a
            # press inside the popover still transitions the interactor
            # through FocusOut toward the separate ToolTip window despite
            # WA_ShowWithoutActivating — dismissing here killed the chooser
            # on press before the row could confirm (real-GUI finding).
            # Ignore the transition when the focus target is the chooser.
            if event_type == QEvent.Type.FocusOut:
                from PySide6.QtGui import QCursor, QGuiApplication
                from PySide6.QtWidgets import QApplication

                popover = viewport._pick_popover
                if popover is not None and not popover.isHidden():
                    if QGuiApplication.focusWindow() is popover.windowHandle():
                        return False
                    focus_widget = QApplication.focusWidget()
                    if focus_widget is not None and popover.isAncestorOf(
                        focus_widget
                    ):
                        return False
                    # Timing fallback: the focus target may not be updated
                    # yet when FocusOut dispatches — a pressed button with
                    # the cursor inside the popover's screen rect means the
                    # press itself caused the transition.
                    if (
                        QApplication.mouseButtons()
                        != Qt.MouseButton.NoButton
                        and popover.frameGeometry().contains(QCursor.pos())
                    ):
                        return False
            viewport.dismiss_pick_candidates()
        return False


class _SnapHud(QFrame):
    """Cursor-side snap/transform HUD (#979).

    Presentational only: a read-only multi-line readout floating near the
    pointer while a move/rotate gesture runs — snap candidate (kind · name ·
    coordinate · acquire distance), the gesture delta (ΔX/ΔY/ΔZ or angle),
    axis/snap state, and the numeric-entry echo. The transform controller
    owns the content; the working document owns every mutation.

    Same window shape as the pick chooser: a real ``Qt::ToolTip`` window,
    never an alien child of the interactor (the VTK render HWND swallows
    plain child widgets on Windows). It never activates and never takes
    focus, so keys keep landing on the interactor where the gizmo's axis
    keys and numeric entry live.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(
            parent,
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setObjectName('snapTransformHud')
        set_surface_role(self, SurfaceRole.RAISED)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName('スナップ・移動量HUD')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(0)
        self.label = QLabel(self)
        self.label.setObjectName('snapTransformHudText')
        # Plain text keeps typed numeric input from ever being re-parsed as
        # rich text (e.g. a stray '<' while editing).
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        set_typography_role(self.label, TypographyRole.BODY)
        layout.addWidget(self.label)
        self.hide()

    def set_lines(self, lines: tuple[str, ...]) -> None:
        self.label.setText('\n'.join(lines))
        self.adjustSize()


class RoomViewport3D(QFrame):
    """Dark, scene-authority-neutral viewport for the UX120 Room workspace.

    Camera navigation and keyboard shortcut policy are intentionally not owned here.
    Agent B can attach the CAD input controller to the public interactor attribute.
    """

    entitySelected = Signal(object)
    proposedEntitySelected = Signal(object)
    contextMenuRequested = Signal(object, object)
    #: Emitted while waypoint recording is armed (#1011 M3): a domain-space
    #: (x, y, z) triple for every successful pick — entities, room surfaces
    #: and authoring primitives all resolve to the point on their face.
    waypointPicked = Signal(object)
    #: Emitted when a left click lands on no pickable entity (deselect/measure free point).
    emptyClicked = Signal(object)
    #: Emitted for every successful entity pick with the display position; lets
    #: controllers read keyboard modifiers at pick time (Ctrl = additive select).
    entityPicked = Signal(object, object)
    #: Emitted on left-drag release: (entity ids whose projected bounds
    #: intersect the marquee rect, additive) — Shift held extends the selection.
    entitiesMarqueeSelected = Signal(object, bool)
    # (underlay_id, domain_x, domain_y) — emitted when an underlay quad/line
    # is picked, used by the calibration/tracing flow.
    underlayClicked = Signal(object, float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("roomViewport")
        set_surface_role(self, SurfaceRole.CANVAS)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # auto_update=False: pyvistaqt otherwise starts a 200 ms timer that
        # re-renders the whole scene 5x/s forever while the widget is visible.
        # Every mutation path below already renders explicitly through
        # _render()/deferred_render(), and VTK's move observers are removed by
        # pyvistaqt, so nothing relied on the idle re-render.
        self.plotter = QtInteractor(self, auto_update=False)
        self.interactor = self.plotter.interactor
        self.interactor.setAccessibleName('部屋3Dビュー')
        self.interactor.setAccessibleDescription(
            '部屋の形状と配置されたオブジェクトを表示する3Dビューポート'
        )
        layout.addWidget(self.interactor)

        self._actor_entity_ids: dict[int, str] = {}
        self._actor_proposed_entity_ids: dict[int, str] = {}
        self._actor_underlay_ids: dict[int, str] = {}
        self._underlay_items: tuple[UnderlayRenderItem, ...] = ()
        # Content-keyed memo of the (quad, texture, line mesh) each frozen
        # underlay item renders as; scene rebuilds that only change entities
        # must not re-upload identical rasters. Keys cover every field the
        # assets derive from plus the decoded image's identity (upstream
        # memoizes decodes per blob sha, so a stable array means same bytes).
        self._underlay_render_cache: dict[tuple, tuple] = {}
        self._guide_items: tuple[GuideRenderItem, ...] = ()
        self._section: SectionPlaneState | None = None
        self._standard_view: str = CUSTOM_VIEW
        self._document: SceneDocument | None = None
        self._selected_id: str | None = None
        self._selected_ids: frozenset[str] = frozenset()
        self._hidden_ids: frozenset[str] = frozenset()
        self._locked_ids: frozenset[str] = frozenset()
        self._overlays = RoomOverlayState()
        self._press_position: QPointF | None = None
        # Left-drag marquee: band widget + lifecycle; a release pick that lands
        # on an actor right after a marquee is suppressed so it cannot
        # collapse the just-computed region selection.
        self._marquee_band: QRubberBand | None = None
        self._marquee_active = False
        self._suppress_next_pick = False
        # Camera pose at left-press, restored when the marquee activates so a
        # box-select drag cannot net-rotate the trackball camera.
        self._press_camera_state: tuple[tuple[float, ...], ...] | None = None
        # (entity_id, actor) for every pickable entity actor — the marquee
        # hit-test set. Mirrors _actor_entity_ids membership.
        self._marquee_actors: list[tuple[str, object]] = []
        # Click-through cycling: repeated clicks within a few px of the same
        # point walk the picker's front-to-back candidate list.
        self._cycle_position: QPointF | None = None
        self._cycle_ids: tuple[str, ...] = ()
        self._cycle_index = 0
        # vtkPicker.Pick fires EndPickEvent, which re-enters this widget's
        # pick callback — never re-pick while dispatching one.
        self._in_pick_dispatch = False
        # Overlapping-pick chooser (#983): the candidate tuple/index reuse the
        # click-through cycle state — never re-picked — and the chooser is
        # dropped on any camera/scene change that would invalidate the stack
        # it was captured from (pan/orbit/zoom/standard view/fit/focus/
        # camera restore, section cut, document or hidden set change,
        # marquee, context menu, empty click, resize/DPI, focus loss).
        self._pick_entries: tuple[PickCandidateEntry, ...] = ()
        self._pick_index = 0
        self._pick_anchor: QPointF | None = None
        self._pick_popover: _PickCandidatePopover | None = None
        self._pick_key_filter: _PickCandidateKeyFilter | None = None
        self._pick_preview_name: str | None = None
        # Cursor-side snap/transform HUD (#979): same ToolTip window shape as
        # the pick chooser, anchored in interactor DIP space and clamped
        # inside it. ``_snap_hud_anchor`` is the last clamped widget-space
        # origin (kept for diagnostics/tests). ``_snap_hud_active`` is true
        # while a feedback call owns the HUD — interactor Hide/Resize events
        # must not kill a live readout: at 200 % DPI the VTK render surface
        # re-parents mid-gesture and fires exactly those events every frame,
        # which hid the HUD immediately after each show. A live gesture
        # re-pushes the HUD every update anyway; the controller's clear call
        # (``render_snap_feedback(None)``) is what actually hides it.
        self._snap_hud: _SnapHud | None = None
        self._snap_hud_anchor: QPointF | None = None
        self._snap_hud_active = False
        #: Input controllers suppress the chooser while an edit gesture is
        #: armed (active gizmo) so a mid-gesture click cannot reopen it.
        self.pick_popover_enabled = True
        #: Optional workspace hook: ``(entity_id) -> reason | None`` for
        #: edit-blocking state the viewport cannot see (e.g. ``!can_edit``).
        self.pick_candidate_reason_provider = None
        # Waypoint recording mode (#1011 M3): while armed, every pick emits
        # ``waypointPicked`` instead of selecting entities, and the room
        # floor/shell/authoring surfaces become pickable so a click on them
        # resolves to the true surface point. Disarming restores the normal
        # "entity selection = pickable mesh" contract exactly.
        self._waypoint_pick_armed = False
        self._search_domain_handles: dict[int, tuple[str, str, str, bool]] = {}
        # deferred_render(): compositing callers (e.g. the workspace refresh
        # that stacks document + constraint + measure + video + proposal
        # renderers) coalesce the per-helper ``plotter.render()`` calls into
        # one draw of the final state instead of one full pass each.
        self._defer_render_depth = 0
        self._render_pending = False
        # Signature of the inputs the last render_document consumed; repeated
        # refreshes with identical inputs skip the whole scene rebuild and
        # only re-present the current actors.
        self._last_render_signature: tuple | None = None
        # Field-overlay (issue #999): content-keyed ImageData cache so a
        # slider move (same view, different slice index) never rebuilds the
        # volume grid, and the scalar-bar title set for clean removal.
        self._field_image_cache: OrderedDict[tuple, pv.ImageData] = OrderedDict()
        self._field_scalar_bars: set[str] = set()
        # (fixed axis, render coordinate) of the current display slice —
        # the probe's click-ray fallback surface (#999).
        self._field_probe_plane: tuple[int, float] | None = None
        # Survey overlay (#1004): the armed scene lets wheel-zoom
        # re-evaluate the visible-span label gate; re-rendering is
        # debounced so a zoom drag costs one repaint, not one per notch.
        self._survey_overlay_scene = None
        self._survey_zoom_timer = QTimer(self)
        self._survey_zoom_timer.setSingleShot(True)
        self._survey_zoom_timer.setInterval(180)
        self._survey_zoom_timer.timeout.connect(self._survey_zoom_refresh)
        self.plotter.set_background(DARK_THEME.viewport.background.hex)
        self.plotter.enable_anti_aliasing("fxaa")
        self.interactor.installEventFilter(self)
        try:
            self.plotter.enable_mesh_picking(
                callback=self._picked_actor,
                show=False,
                show_message=False,
                left_clicking=True,
                use_actor=True,
            )
        except (TypeError, RuntimeError):
            # Picking is optional at this layer; Agent B may own selection input.
            pass

    def _render(self) -> None:
        if self._defer_render_depth:
            self._render_pending = True
            return
        self.plotter.render()

    @contextmanager
    def deferred_render(self) -> Iterator[None]:
        """Coalesce ``_render()`` calls into one draw at the outermost exit.

        Behavior is preserved: the final scene state is rendered exactly
        once, identical to what the last deferred ``render()`` would have
        drawn. Rendering still happens on exit even if the body raised —
        whatever state was reached is what gets drawn.
        """
        self._defer_render_depth += 1
        try:
            yield
        finally:
            self._defer_render_depth -= 1
            if self._defer_render_depth == 0 and self._render_pending:
                self._render_pending = False
                self.plotter.render()

    def render_document(
        self,
        document: SceneDocument,
        *,
        selected_id: str | None,
        selected_ids: tuple[str, ...] | list[str] | None = None,
        hidden_ids: set[str] | frozenset[str] = frozenset(),
        locked_ids: set[str] | frozenset[str] = frozenset(),
        overlays: RoomOverlayState,
        reset_camera: bool = False,
    ) -> None:
        previous_document = self._document
        previous_hidden = self._hidden_ids
        self._document = document
        if selected_ids is None:
            selected_ids = () if selected_id is None else (selected_id,)
        selected_set = frozenset(selected_ids)
        if selected_id is not None and selected_id not in selected_set:
            selected_set = selected_set | {selected_id}
        self._selected_id = selected_id
        self._selected_ids = selected_set
        self._hidden_ids = frozenset(hidden_ids)
        self._locked_ids = frozenset(locked_ids)
        self._overlays = overlays
        signature = (
            # The document object itself, not id(): keeping the reference
            # prevents its address being reused by a different document,
            # and frozen-model equality catches any replacement.
            document,
            selected_id,
            selected_set,
            self._hidden_ids,
            self._locked_ids,
            overlays,
            # Content keys, not id(): a same-length replacement tuple can
            # land on the freed previous tuple's address (observed on
            # underlay field edits via _refresh_underlay_ui's double sync),
            # which would skip the rebuild and never repaint the change.
            # Underlay items can't compare elementwise (np.ndarray image),
            # so they use the same content key as the render cache; guide
            # items are plain frozen float tuples and compare directly.
            tuple(
                _underlay_item_key(item) for item in self._underlay_items
            ),
            self._guide_items,
            None if self._section is None else self._section.model_dump_json(),
        )
        if not reset_camera and signature == self._last_render_signature:
            # Identical scene inputs — keep every render_document-owned actor
            # and drop only the overlay namespaces the compositing callers
            # re-populate right after this method returns (or, when their
            # state went away, correctly do not).
            self._remove_overlay_actors()
            self._render()
            return
        self._actor_entity_ids.clear()
        self._actor_proposed_entity_ids.clear()
        self._actor_underlay_ids.clear()
        self._search_domain_handles.clear()
        self._marquee_actors.clear()
        # NOTE: _cycle_* state is intentionally NOT reset here — selection
        # changes re-render the scene on every click, and wiping the cycle
        # history would make click-through cycling unreachable. Stale state
        # self-corrects: the next click's candidate tuple differs whenever
        # the rebuilt scene's hit stack changed, resetting the index then.
        self.plotter.clear()
        self.plotter.set_background(DARK_THEME.viewport.background.hex)

        floor = _room_floor_mesh(document)
        if floor is not None:
            floor = self._apply_section(floor)
            if floor is not None:
                self.plotter.add_mesh(
                    floor,
                    color=DARK_THEME.viewport.floor.hex,
                    opacity=0.72,
                    lighting=False,
                    pickable=False,
                    name="room-floor",
                    render=False,
                )

        self._render_underlays()
        if overlays.guides_visible:
            self._render_guides()

        if overlays.grid:
            minor_grid = _grid_mesh(document, step_m=0.5)
            if minor_grid is not None:
                self.plotter.add_mesh(
                    minor_grid,
                    color=DARK_THEME.viewport.grid_minor.hex,
                    line_width=1,
                    opacity=0.34,
                    pickable=False,
                    name="room-grid-minor",
                    render=False,
                )
            major_grid = _grid_mesh(document, step_m=2.0, z_m=0.004)
            if major_grid is not None:
                self.plotter.add_mesh(
                    major_grid,
                    color=DARK_THEME.viewport.grid_major.hex,
                    line_width=2,
                    opacity=0.58,
                    pickable=False,
                    name="room-grid-major",
                    render=False,
                )

        room_mesh = _room_wireframe(document)
        if room_mesh is not None:
            self.plotter.add_mesh(
                room_mesh,
                color=DARK_THEME.viewport.geometry_edge.hex,
                line_width=2,
                opacity=0.78,
                pickable=False,
                name="room-shell",
                render=False,
            )

        # Issue #976: committed semantic primitives as translucent solids,
        # named per surface key so tests and overlays can address them.
        for surface_key, kind, mesh in _room_authoring_surface_meshes(document):
            mesh = self._apply_section(mesh)
            if mesh is None:
                continue
            self.plotter.add_mesh(
                mesh,
                color=_AUTHORING_SURFACE_COLORS[kind],
                opacity=0.40 if kind != 'opening' else 0.18,
                show_edges=True,
                edge_color=DARK_THEME.viewport.geometry_edge.hex,
                line_width=1,
                pickable=False,
                name=f'authoring-surface-{surface_key}',
                render=False,
            )

        semantic_categories: set[str] = set()
        for entity in document.entities:
            if entity.entity_id in self._hidden_ids:
                # Hidden entities are not rendered, therefore not pickable —
                # the N20b "hidden objects cannot be selected" contract (#482).
                continue
            is_primary = entity.entity_id == selected_id
            is_selected = entity.entity_id in selected_set
            is_locked = entity.entity_id in self._locked_ids
            focused_out = bool(
                overlays.focus_selection
                and selected_set
                and not is_selected
            )
            body_mesh, glyphs, envelope_mesh = entity_render_meshes(entity)
            mesh = self._apply_section(body_mesh)
            if mesh is None:
                continue
            category = _entity_category(entity)
            fill_color = _category_color(category)
            edge_color = DARK_THEME.viewport.geometry_edge.hex
            if is_selected:
                edge_color = DARK_THEME.viewport.selection_outline.hex
            elif is_locked:
                edge_color = DARK_THEME.text.muted.hex
            actor = self.plotter.add_mesh(
                mesh,
                color=fill_color,
                show_edges=True,
                edge_color=edge_color,
                line_width=3 if is_primary else (2 if is_selected else 1),
                opacity=(0.55 if is_locked else 0.90) if not focused_out else 0.12,
                ambient=0.32,
                diffuse=0.62,
                specular=0.10,
                specular_power=12.0,
                pickable=True,
                name=f"entity-{entity.entity_id}",
                render=False,
            )
            self._actor_entity_ids[id(actor)] = entity.entity_id
            self._marquee_actors.append((entity.entity_id, actor))
            # Semantic glyph proxies read as the entity's type at a glance;
            # they are render-only, pick back to the entity, and dim with it.
            glyph_opacity = (0.55 if is_locked else 0.98) if not focused_out else 0.12
            for index, glyph in enumerate(glyphs):
                glyph = self._apply_section(glyph)
                if glyph is None:
                    continue
                glyph_actor = self.plotter.add_mesh(
                    glyph,
                    color=_shade_hex(fill_color, 0.72),
                    show_edges=False,
                    line_width=2 if is_selected else 1,
                    opacity=glyph_opacity,
                    ambient=0.30,
                    diffuse=0.55,
                    specular=0.15,
                    specular_power=16.0,
                    pickable=True,
                    name=f"glyph-{entity.entity_id}-{index}",
                    render=False,
                )
                self._actor_entity_ids[id(glyph_actor)] = entity.entity_id
                self._marquee_actors.append((entity.entity_id, glyph_actor))
            if envelope_mesh is not None:
                # The bounding envelope stays visible as a separate wireframe
                # authority whenever an entity opts into richer body geometry.
                self.plotter.add_mesh(
                    envelope_mesh,
                    color=DARK_THEME.viewport.geometry_edge.hex,
                    style="wireframe",
                    line_width=1,
                    opacity=0.45,
                    pickable=False,
                    name=f"envelope-{entity.entity_id}",
                    render=False,
                )
            semantic_categories.add(category)

        if selected_id is not None:
            try:
                selected_entity = document.entity(selected_id)
            except KeyError:
                selected_entity = None
            if selected_entity is not None:
                for ray in _selection_direction_rays(selected_entity):
                    self.plotter.add_mesh(
                        ray.mesh,
                        color=ray.color,
                        line_width=3,
                        opacity=0.95,
                        pickable=False,
                        name=f"selection-{ray.role}-{selected_id}",
                        render=False,
                    )
        # Secondary (non-primary) members of a multi-selection get the outline
        # without direction rays — the primary stays visually distinct (#480).
        for entity_id in selected_set:
            if entity_id == selected_id:
                continue
            try:
                secondary = document.entity(entity_id)
            except KeyError:
                continue
            bounds = secondary.size_m
            radius = 0.05 if bounds is None else max(bounds.x_m, bounds.y_m) * 0.06 + 0.02
            self.plotter.add_mesh(
                pv.Sphere(
                    radius=radius,
                    center=domain_to_render(
                        type(secondary.position)(
                            x_m=secondary.position.x_m,
                            y_m=secondary.position.y_m,
                            z_m=secondary.position.z_m
                            + (bounds.z_m * 0.6 if bounds is not None else 0.0),
                        )
                    ),
                ),
                color=DARK_THEME.viewport.selection_outline.hex,
                opacity=0.5,
                pickable=False,
                name=f"selection-marker-{entity_id}",
                render=False,
            )

        if overlays.acoustics:
            self._render_acoustic_overlay(document)
        if overlays.labels:
            self._render_labels(document, selected_id)
        self._render_category_legend(semantic_categories)

        self.plotter.add_axes(
            color=DARK_THEME.text.muted.hex,
            line_width=1,
            labels_off=True,
        )
        if reset_camera:
            self.fit_scene()
        self._last_render_signature = signature
        # The rebuild wiped every actor — revalidate the chooser's armed
        # candidate set against the new document/hidden state and re-add its
        # preview envelope, or dismiss it when the stack went stale (#983).
        self._restore_pick_candidates(previous_document, previous_hidden)
        if self._waypoint_pick_armed:
            # Room surfaces were recreated above — re-arm their pickable
            # flags or waypoint recording silently loses its targets.
            self._apply_waypoint_pickable()
        self._render()

    #: Named-actor prefixes owned by the compositing overlay renderers
    #: (measure/constraint/video/proposal/prediction/history/search-domain/
    #: snap-feedback), which run after render_document inside one deferred
    #: render block. A signature-skipped render_document drops exactly these
    #: actors — the overlays re-add their current set right after — while
    #: keeping the expensive entity/grid/shell/selection scene.
    _OVERLAY_ACTOR_PREFIXES = (
        'proposal-',
        'prediction-',
        'measure-',
        'measurement-',
        'constraint-',
        'video-',
        'history-ghost-',
        'search-domain-',
        'snap-feedback-',
        'guidance-',
        'acoustic-field-',
        'lighting-',
        'opclear-',
        'treatment-overlay-',
        'fabrication-',
        'quality-map-',
        'cable-route-',
        'abdiff-',
        'campaign-overlay-',
        'survey-overlay-',
        'coverage-overlay-',
        'correspond-',
        'directivity-',
    )

    def _remove_overlay_actors(self) -> None:
        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if not actors:
            return
        for name in tuple(actors):
            if isinstance(name, str) and name.startswith(
                self._OVERLAY_ACTOR_PREFIXES
            ):
                self.plotter.remove_actor(name)
        # Swept survey actors disarm the zoom-refresh cache; the
        # compositor re-arms it iff it re-renders the overlay right away.
        self._survey_overlay_scene = None
        self._survey_zoom_timer.stop()

    def _render_category_legend(self, categories: set[str]) -> None:
        """Small category key — appears only when two or more categories show.

        The legend names categories, not individual entities, so it stays
        compact as the scene grows. Entity labels remain the opt-in detail.
        """

        if len(categories) < 2:
            return
        labels = [
            (_CATEGORY_LEGEND_LABELS[category], _category_color(category))
            for category in sorted(categories)
        ]
        # add_legend has no render kwarg and renders internally; suppress so
        # the scene rebuild still ends in a single draw.
        self.plotter.suppress_rendering = True
        try:
            self.plotter.add_legend(
                labels=labels,
                loc="lower right",
                face="rectangle",
                size=(0.17, 0.035 * len(labels) + 0.02),
                bcolor=DARK_THEME.text.secondary.hex,
                border=False,
                background_opacity=0.55,
                name="semantic-category-legend",
            )
        finally:
            self.plotter.suppress_rendering = False

    def render_measurement_overlay(
        self,
        *,
        position: Position3 | None,
        direction: tuple[float, float, float] | None,
    ) -> None:
        """Measurement-point marker and optional direction ray (#487).

        The marker is drawn at the acoustic reference position stored on the
        measurement's bound scene revision; the direction ray only renders
        when the measurement recorded an explicit direction — unknown
        direction is never visualized as a guess.
        """
        if position is None:
            return
        self.plotter.add_mesh(
            pv.Sphere(radius=0.06, center=domain_to_render(position)),
            color=DARK_THEME.semantic.warning.hex,
            opacity=0.95,
            pickable=False,
            name="measurement-point",
            render=False,
        )
        if direction is not None:
            end = Position3(
                x_m=position.x_m + direction[0],
                y_m=position.y_m + direction[1],
                z_m=position.z_m + direction[2],
            )
            self.plotter.add_mesh(
                pv.Line(domain_to_render(position), domain_to_render(end)),
                color=DARK_THEME.accent.primary.hex,
                line_width=3,
                opacity=0.95,
                pickable=False,
                name="measurement-direction",
                render=False,
            )
        self._render()

    _CORRESPONDENCE_PREFIX = 'correspond-'

    def clear_reflection_correspondence_overlay(self) -> None:
        """Drop every ``correspond-*`` actor (#1002 — toggle off/reload)."""
        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if not actors:
            return
        removed = False
        for name in tuple(actors):
            if isinstance(name, str) and name.startswith(
                self._CORRESPONDENCE_PREFIX
            ):
                self.plotter.remove_actor(name)
                removed = True
        if removed:
            self._render()

    def render_reflection_correspondence_overlay(
        self,
        paths: Sequence[tuple[str, tuple[tuple[float, float, float], ...]]],
        *,
        highlighted_row_ids: Sequence[str] = (),
        path_classes: dict[str, str] | None = None,
    ) -> None:
        """Measured↔predicted correspondence review layer (#1002).

        ``paths`` are ``(row_id, points)`` pairs in *domain* coordinates,
        each point list ordered source → interactions → receiver. Only the
        rows named in ``highlighted_row_ids`` get ``pv.Line`` actors — the
        runtime-load requirement keeps full geometry exclusive to the
        selected pairing/cluster; every other candidate contributes its
        interaction points to one batched glyph cloud so context stays
        visible without per-path meshes.

        ``path_classes`` maps row_id → a display class
        (``'matched'``/``'ambiguous'``/``'hypothesis'``/``'unmatched'``);
        ambiguous members intentionally share one colour — an unresolved
        cluster never gets an arbitrary 'correct' colour. All actors are
        non-pickable so CAD picking and the measure tool stay undisturbed.
        """
        self.clear_reflection_correspondence_overlay()
        if not paths:
            return
        highlighted = set(highlighted_row_ids)
        classes = path_classes or {}
        class_colors = {
            'matched': DARK_THEME.scientific.predicted.hex,
            'ambiguous': DARK_THEME.semantic.warning.hex,
            'hypothesis': DARK_THEME.accent.primary.hex,
            'unmatched': DARK_THEME.text.muted.hex,
        }
        context_points: list[tuple[float, float, float]] = []
        for index, (row_id, points) in enumerate(paths):
            if len(points) < 2:
                continue
            emphasized = row_id in highlighted
            class_label = classes.get(row_id, 'matched')
            if not emphasized:
                # Interior points only — endpoints duplicate the shared
                # source/receiver markers for every candidate.
                context_points.extend(
                    (px, -py, pz) for (px, py, pz) in points[1:-1]
                )
                continue
            color = (
                class_colors['hypothesis']
                if class_label == 'hypothesis'
                else class_colors.get(class_label, class_colors['matched'])
            )
            render_points = [(px, -py, pz) for (px, py, pz) in points]
            for seg_index, (a, b) in enumerate(
                zip(render_points, render_points[1:])
            ):
                self.plotter.add_mesh(
                    pv.Line(a, b),
                    color=color,
                    line_width=4 if class_label != 'ambiguous' else 3,
                    opacity=0.85,
                    pickable=False,
                    name=(
                        f'{self._CORRESPONDENCE_PREFIX}'
                        f'path-{index}-seg-{seg_index}'
                    ),
                    render=False,
                )
            for point_index, point in enumerate(render_points[1:-1]):
                self.plotter.add_mesh(
                    pv.Sphere(radius=0.045, center=point),
                    color=color,
                    opacity=0.9,
                    lighting=False,
                    pickable=False,
                    name=(
                        f'{self._CORRESPONDENCE_PREFIX}'
                        f'path-{index}-pt-{point_index}'
                    ),
                    render=False,
                )
        if context_points:
            cloud = pv.PolyData(
                np.asarray(context_points, dtype=float)
            )
            self.plotter.add_mesh(
                cloud,
                color=DARK_THEME.scientific.predicted.hex,
                point_size=6.0,
                render_points_as_spheres=True,
                opacity=0.55,
                lighting=False,
                pickable=False,
                name=f'{self._CORRESPONDENCE_PREFIX}candidates',
                render=False,
            )
        # ASCII only — VTK/Mesa cannot render .ttc CJK glyphs.
        self.plotter.add_text(
            'reflection correspondence review '
            '(declared pairings / candidates)',
            name=f'{self._CORRESPONDENCE_PREFIX}label',
            position='lower_right',
            font_size=9,
            color=DARK_THEME.text.secondary.hex,
            render=False,
        )
        self._render()

    def render_proposed_entities(
        self,
        entities: tuple[SceneEntity, ...],
        *,
        selected_id: str | None = None,
        label: str = "提案ゴースト · 未設置 / 現在シーンは変更しません",
    ) -> None:
        """Overlay proposal ghosts without changing current SceneDocument truth."""
        self._actor_proposed_entity_ids.clear()
        if not entities:
            return
        for entity in entities:
            # Ghosts stay wireframe — the proposed/current grammar is unchanged;
            # only the fill color now carries the entity category (#572).
            ghost_color = _category_color(_entity_category(entity))
            ghost_mesh, ghost_glyphs, _env = entity_render_meshes(entity)
            actor = self.plotter.add_mesh(
                ghost_mesh,
                color=ghost_color,
                style="wireframe",
                line_width=4 if entity.entity_id == selected_id else 2,
                opacity=0.62 if entity.entity_id == selected_id else 0.34,
                pickable=True,
                name=f"proposal-ghost-{entity.entity_id}",
                render=False,
            )
            self._actor_proposed_entity_ids[id(actor)] = entity.entity_id
            for index, glyph in enumerate(ghost_glyphs):
                glyph_actor = self.plotter.add_mesh(
                    glyph,
                    color=ghost_color,
                    style="wireframe",
                    line_width=2 if entity.entity_id == selected_id else 1,
                    opacity=0.62 if entity.entity_id == selected_id else 0.30,
                    pickable=True,
                    name=f"proposal-glyph-{entity.entity_id}-{index}",
                    render=False,
                )
                self._actor_proposed_entity_ids[id(glyph_actor)] = entity.entity_id
        self.plotter.add_text(
            label,
            name="proposal-ghost-label",
            position="upper_left",
            font_size=9,
            color=DARK_THEME.text.secondary.hex,
            render=False,
        )
        self._render()

    def render_cable_route_overlay(
        self,
        items: tuple[CableRouteOverlayItem, ...] | list[CableRouteOverlayItem],
    ) -> None:
        """Honest cable-route overlay (#1011): endpoints + recorded waypoints.

        Only exactly-bound endpoints render — an endpoint whose entity is
        absent draws nothing and a label-only endpoint has no position to
        place. Between endpoints nothing is connected: a run without
        recorded geometry gets the ``経路形状未登録`` label at the span's
        midpoint, never an invented line. A run whose geometry exists
        draws only the recorded waypoint polylines — design records in
        the accent colour, as-built records in the warning colour — while
        a stale geometry draws the stale label instead of presenting an
        old line as the current route. All actors are non-pickable and
        share the ``cable-route-`` prefix so signature-skipped renders
        drop them with every other overlay namespace.
        """
        if not items:
            return
        endpoint_colors = {
            'bound': DARK_THEME.semantic.success.hex,
            'missing': DARK_THEME.semantic.error.hex,
        }
        for item in items:
            bound_positions: list[tuple[float, float, float]] = []
            for side, endpoint in (
                ('from', item.from_endpoint),
                ('to', item.to_endpoint),
            ):
                if endpoint.position is None or endpoint.status == 'unbound':
                    continue
                bound_positions.append(endpoint.position)
                self.plotter.add_mesh(
                    pv.Sphere(radius=0.05, center=_domain_to_render_tuple(endpoint.position)),
                    color=endpoint_colors.get(
                        endpoint.status, DARK_THEME.semantic.warning.hex
                    ),
                    opacity=0.95,
                    pickable=False,
                    name=f"cable-route-endpoint-{item.run_id}-{side}",
                    render=False,
                )
                self.plotter.add_point_labels(
                    [_domain_to_render_tuple(endpoint.position)],
                    [endpoint.label],
                    text_color=DARK_THEME.text.primary.hex,
                    shape_color=DARK_THEME.surfaces.overlay.hex,
                    shape_opacity=0.85,
                    font_size=10,
                    point_size=0,
                    always_visible=True,
                    name=f"cable-route-endpoint-label-{item.run_id}-{side}",
                    render=False,
                )
            if item.route_state == 'registered':
                color = (
                    DARK_THEME.accent.primary.hex
                    if item.record_kind == 'design'
                    else DARK_THEME.semantic.warning.hex
                )
                for segment in item.waypoint_segments:
                    for first, second in zip(
                        segment.points, segment.points[1:]
                    ):
                        self.plotter.add_mesh(
                            pv.Line(
                                _domain_to_render_tuple(first),
                                _domain_to_render_tuple(second),
                            ),
                            color=color,
                            line_width=4,
                            opacity=0.9,
                            pickable=False,
                            name=(
                                f"cable-route-line-{item.run_id}-"
                                f"{segment.segment_sequence}"
                            ),
                            render=False,
                        )
            elif bound_positions:
                # No line between endpoints — ever. The label marks the
                # gap honestly at the span midpoint (or beside the only
                # bound endpoint).
                midpoint = tuple(
                    sum(point[axis] for point in bound_positions)
                    / len(bound_positions)
                    for axis in range(3)
                )
                # JA text first; the ASCII fallback keeps the honest
                # state readable where VTK's label font has no CJK
                # coverage (observed on the Windows Mesa build).
                state_label = (
                    '経路形状未登録'
                    if item.route_state == 'unregistered'
                    else '経路情報が最新ではありません'
                )
                ascii_label = (
                    'route shape unregistered'
                    if item.route_state == 'unregistered'
                    else 'route record stale'
                )
                self.plotter.add_point_labels(
                    [_domain_to_render_tuple(midpoint)],
                    [f'{item.label} — {state_label} ({ascii_label})'],
                    text_color=DARK_THEME.semantic.warning.hex,
                    shape_color=DARK_THEME.surfaces.overlay.hex,
                    shape_opacity=0.88,
                    font_size=10,
                    point_size=0,
                    always_visible=True,
                    name=f"cable-route-state-{item.run_id}",
                    render=False,
                )
        self._render()

    def _render_acoustic_overlay(self, document: SceneDocument) -> None:
        for entity in document.entities:
            if entity.entity_id in self._hidden_ids:
                continue
            reference = acoustic_reference_position(entity)
            if reference is not None:
                self.plotter.add_mesh(
                    pv.Sphere(radius=0.035, center=domain_to_render(reference)),
                    color=DARK_THEME.accent.primary.hex,
                    opacity=0.92,
                    pickable=False,
                    name=f"reference-{entity.entity_id}",
                    render=False,
                )
            if entity.kind != "speaker" or entity.aim_xyz is None:
                continue
            origin = reference or entity.position
            end = type(origin)(
                x_m=origin.x_m + entity.aim_xyz.x * 1.2,
                y_m=origin.y_m + entity.aim_xyz.y * 1.2,
                z_m=origin.z_m + entity.aim_xyz.z * 1.2,
            )
            self.plotter.add_mesh(
                pv.Line(domain_to_render(origin), domain_to_render(end)),
                color=DARK_THEME.accent.primary.hex,
                line_width=2,
                opacity=0.80,
                pickable=False,
                name=f"aim-{entity.entity_id}",
                render=False,
            )

    def render_prediction_results(
        self,
        results: tuple[CadPredictionResult, ...],
        *,
        highlight: PredictionSpatialLink | None = None,
    ) -> None:
        """Render current N70 geometry evidence without becoming prediction authority.

        ``highlight`` is the spatial link of the selected interpretation
        finding (Issue #469): a ``reflection_path`` link emphasises exactly the
        stored path geometry, while ``receiver``/``source`` links mark the
        referenced position. Everything drawn comes from the persisted result;
        no geometry is invented here.
        """

        document = self._document
        if document is None or not results:
            return
        first = results[0]
        if (
            first.scene_content_hash != scene_content_hash(document)
            or first.geometry_compatibility != "exact_for_model_geometry"
        ):
            return
        reflection_result = next(
            (item for item in results if item.result_kind == "geometry_reflections"),
            None,
        )

        highlight_index: int | None = None
        marker_position = None
        if highlight is not None:
            if getattr(highlight, "kind", None) == "reflection_path":
                highlight_index = getattr(highlight, "reflection_index", None)
            else:
                marker_position = (
                    getattr(highlight, "reflection_position", None)
                    or getattr(highlight, "source_position", None)
                    or getattr(highlight, "receiver_position", None)
                )
        if highlight_index is not None and (
            not isinstance(highlight_index, int)
            or reflection_result is None
            or not (0 <= highlight_index < len(reflection_result.reflections))
        ):
            # A persisted link that no longer matches the stored payload is
            # non-authoritative: fall back to the plain overlay.
            highlight_index = None

        if reflection_result is not None:
            direct_seen: set[str] = set()
            for index, reflection in enumerate(reflection_result.reflections):
                source = domain_to_render(reflection.source_position)
                receiver = domain_to_render(reflection.receiver_position)
                point = domain_to_render(reflection.reflection_position)
                emphasized = highlight_index == index
                dimmed = highlight_index is not None and not emphasized
                if reflection.speaker_entity_id not in direct_seen:
                    direct_seen.add(reflection.speaker_entity_id)
                    self.plotter.add_mesh(
                        pv.Line(source, receiver),
                        color=DARK_THEME.scientific.primary_trace.hex,
                        line_width=3 if emphasized else 2,
                        opacity=0.25 if dimmed else 0.64,
                        pickable=False,
                        name=f"prediction-direct-{reflection.speaker_entity_id}",
                        render=False,
                    )
                self.plotter.add_mesh(
                    pv.Line(source, point),
                    color=(
                        DARK_THEME.accent.primary.hex
                        if emphasized
                        else DARK_THEME.scientific.predicted.hex
                    ),
                    line_width=4 if emphasized else 2,
                    opacity=0.25 if dimmed else 0.80,
                    pickable=False,
                    name=f"prediction-reflection-a-{index}",
                    render=False,
                )
                self.plotter.add_mesh(
                    pv.Line(point, receiver),
                    color=(
                        DARK_THEME.accent.primary.hex
                        if emphasized
                        else DARK_THEME.scientific.predicted.hex
                    ),
                    line_width=4 if emphasized else 2,
                    opacity=0.25 if dimmed else 0.80,
                    pickable=False,
                    name=f"prediction-reflection-b-{index}",
                    render=False,
                )
                self.plotter.add_mesh(
                    pv.Sphere(radius=0.055 if emphasized else 0.035, center=point),
                    color=(
                        DARK_THEME.accent.primary.hex
                        if emphasized
                        else DARK_THEME.scientific.cursor.hex
                    ),
                    pickable=False,
                    name=f"prediction-reflection-point-{index}",
                    render=False,
                )
            if reflection_result.reflections:
                self.plotter.add_text(
                    "予測幾何 · 実測ではありません",
                    name="prediction-overlay-label",
                    position="lower_left",
                    font_size=9,
                    color=DARK_THEME.text.secondary.hex,
                    render=False,
                )
        if marker_position is not None:
            self.plotter.add_mesh(
                pv.Sphere(radius=0.06, center=domain_to_render(marker_position)),
                color=DARK_THEME.accent.primary.hex,
                opacity=0.95,
                pickable=False,
                name="prediction-focus-marker",
                render=False,
            )
        self._render()

    def render_reflection_guidance_overlay(
        self,
        markers: tuple['ReflectionGuidanceOverlayMarker', ...],
    ) -> None:
        """Draw persisted-artifact reflection-guidance markers (REV40).

        Every anchor is replayed deterministic path authority (see
        ``reflection_guidance_presentation.guidance_overlay_markers``):
        the treat-zone sphere sits on the proven first-reflection point,
        the source sphere on the snapshot-pinned source reference point,
        and the legs trace the proven source → reflection → receiver
        path. Nothing is synthesized — an empty tuple draws nothing.
        """

        if not markers:
            return
        label_points: list[tuple[float, float, float]] = []
        label_texts: list[str] = []
        for index, marker in enumerate(markers):
            zone = domain_to_render(marker.zone_anchor)
            zone_color = (
                DARK_THEME.scientific.measured.hex
                if marker.confidence
                in ('authority_backed', 'measured_supported')
                else DARK_THEME.semantic.warning.hex
            )
            for leg, (start, end) in enumerate(
                zip(marker.path_points, marker.path_points[1:])
            ):
                self.plotter.add_mesh(
                    pv.Line(domain_to_render(start), domain_to_render(end)),
                    color=DARK_THEME.scientific.secondary_trace.hex,
                    line_width=2,
                    opacity=0.55,
                    pickable=False,
                    name=f"guidance-path-{index}-{leg}",
                    render=False,
                )
            self.plotter.add_mesh(
                pv.Sphere(radius=0.05, center=zone),
                color=zone_color,
                pickable=False,
                name=f"guidance-zone-{index}",
                render=False,
            )
            label_points.append(zone)
            label_texts.append(marker.label)
            if marker.source_anchor is not None:
                source = domain_to_render(marker.source_anchor)
                self.plotter.add_mesh(
                    pv.Sphere(radius=0.045, center=source),
                    color=DARK_THEME.scientific.primary_trace.hex,
                    pickable=False,
                    name=f"guidance-source-{index}",
                    render=False,
                )
                if marker.source_label:
                    label_points.append(source)
                    label_texts.append(marker.source_label)
        if label_points:
            self.plotter.add_point_labels(
                np.asarray(label_points, dtype=float),
                label_texts,
                text_color=DARK_THEME.text.primary.hex,
                shape_color=DARK_THEME.surfaces.overlay.hex,
                shape_opacity=0.88,
                font_size=10,
                point_size=0,
                always_visible=True,
                name="guidance-labels",
                render=False,
            )
        self.plotter.add_text(
            "反射ガイダンス · 確定的パス権威（R150）",
            name="guidance-overlay-label",
            position="lower_right",
            font_size=9,
            color=DARK_THEME.text.secondary.hex,
            render=False,
        )
        self._render()

    # -- Lighting-scene preview (issue #1013) ---------------------------------
    #
    # Read-only state glyphs. A pin exists ONLY for a scene-referenced
    # fixture whose entity_id resolves exactly to a document entity; the
    # four stage beads (desired/commanded/read_back/measured) are drawn as
    # separate colored beads — never merged — and an absent stage draws a
    # dim unknown bead, never a 0% or a desired echo. Colors and glow are
    # explanation symbols: the legend states that nothing here is a
    # photometric (lux / beam spread / room illumination) render.

    _LIGHTING_BEAD_RADIUS_M = 0.045
    _LIGHTING_BEAD_SPACING_M = 0.10
    _LIGHTING_BEAD_LIFT_M = 0.18

    def clear_lighting_scene_preview(self) -> None:
        """Drop every ``lighting-*`` actor (toggled off / scene changed)."""
        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if not actors:
            return
        removed = False
        for name in tuple(actors):
            if isinstance(name, str) and name.startswith('lighting-'):
                self.plotter.remove_actor(name)
                removed = True
        if removed:
            self._render()

    def render_lighting_scene_preview(
        self,
        preview: LightingScenePreview | None,
    ) -> None:
        """Draw the read-only lighting-scene preview (#1013).

        ``None`` clears the overlay (toggle off / no current scene). Every
        actor is non-pickable — the preview is an explanation surface, not
        an input device; applying a scene stays on the approved
        device-action path, never on this overlay.
        """
        self.clear_lighting_scene_preview()
        if preview is None:
            return

        stage_count = len(LIGHTING_STAGE_VOCAB)
        for pin in preview.pins:
            x, y_neg, _z = domain_to_render(
                Position3(
                    x_m=pin.position[0],
                    y_m=pin.position[1],
                    z_m=pin.position[2],
                )
            )
            top_z = pin.top_z_m
            anchor_color = (
                LIGHTING_ZONE_VOCAB[pin.zone_roles[0]][1]
                if pin.zone_roles
                else LIGHTING_NEUTRAL_ANCHOR_COLOR
            )
            self.plotter.add_mesh(
                pv.Disc(
                    center=(x, y_neg, top_z + 0.02),
                    inner=0.0,
                    outer=0.085,
                    normal=(0.0, 0.0, 1.0),
                ),
                color=anchor_color,
                opacity=0.75,
                lighting=False,
                pickable=False,
                name=f'lighting-anchor-{pin.fixture_id}',
                render=False,
            )
            bead_z = top_z + self._LIGHTING_BEAD_LIFT_M
            row_x0 = x - (stage_count - 1) * self._LIGHTING_BEAD_SPACING_M / 2.0
            for index, state in enumerate(pin.states):
                bead_x = row_x0 + index * self._LIGHTING_BEAD_SPACING_M
                if state.known:
                    bead_color = LIGHTING_STAGE_VOCAB[state.stage][1]
                    opacity = 0.95
                else:
                    bead_color = LIGHTING_UNKNOWN_STAGE_COLOR
                    opacity = 0.35
                self.plotter.add_mesh(
                    pv.Sphere(
                        radius=self._LIGHTING_BEAD_RADIUS_M,
                        center=(bead_x, y_neg, bead_z),
                    ),
                    color=bead_color,
                    opacity=opacity,
                    lighting=False,
                    pickable=False,
                    name=f'lighting-bead-{pin.fixture_id}-{state.stage}',
                    render=False,
                )
            if pin.multi_assigned:
                # Zone multi-assignment is information, never an error —
                # flag the pin so the several assigning refs stay visible.
                self.plotter.add_mesh(
                    pv.Sphere(
                        radius=self._LIGHTING_BEAD_RADIUS_M * 2.6,
                        center=(x, y_neg, bead_z),
                    ),
                    color='#e8ecf4',
                    style='wireframe',
                    line_width=1,
                    opacity=0.5,
                    pickable=False,
                    name=f'lighting-multi-{pin.fixture_id}',
                    render=False,
                )
            if pin.bias_target_position is not None:
                target = domain_to_render(
                    Position3(
                        x_m=pin.bias_target_position[0],
                        y_m=pin.bias_target_position[1],
                        z_m=pin.bias_target_position[2],
                    )
                )
                self.plotter.add_mesh(
                    pv.Line((x, y_neg, bead_z), target),
                    color=LIGHTING_ZONE_VOCAB['bias'][1],
                    line_width=2,
                    opacity=0.55,
                    pickable=False,
                    name=f'lighting-bias-{pin.fixture_id}',
                    render=False,
                )
            unsupported = sorted(
                {dim for state in pin.states for dim in state.unsupported}
            )
            label_text = (
                f'{pin.fixture_label or pin.fixture_id} '
                f'{stage_bead_label(pin)}'
            )
            if pin.multi_assigned:
                label_text += ' ※複合割当'
            if unsupported:
                label_text += ' / 非対応:' + ','.join(unsupported)
            self.plotter.add_point_labels(
                np.asarray([(x, y_neg, bead_z + 0.12)], dtype=float),
                [label_text],
                text_color=DARK_THEME.text.primary.hex,
                shape_color=DARK_THEME.surfaces.overlay.hex,
                shape_opacity=0.88,
                font_size=9,
                point_size=0,
                always_visible=True,
                name=f'lighting-label-{pin.fixture_id}',
                render=False,
            )

        summary = (
            f'照明シーン {preview.scene_label} '
            f'({preview.scene_id} v{preview.scene_version}) · '
            f'ピン{len(preview.pins)}件 · '
            f'3D未配置{len(preview.unplaced)}件'
        )
        if preview.missing_refs:
            summary += f' · 未解決参照{len(preview.missing_refs)}件'
        self.plotter.add_text(
            summary,
            name='lighting-scene-summary',
            position='upper_left',
            font_size=9,
            color=DARK_THEME.text.secondary.hex,
            render=False,
        )
        if preview.unplaced or preview.missing_refs:
            lines = [
                '3D未配置: '
                + ', '.join(item.fixture_id for item in preview.unplaced)
                if preview.unplaced
                else None,
                '未解決参照: ' + ', '.join(preview.missing_refs)
                if preview.missing_refs
                else None,
            ]
            self.plotter.add_text(
                '\n'.join(line for line in lines if line),
                name='lighting-scene-unplaced',
                position='upper_edge',
                font_size=8,
                color=DARK_THEME.text.secondary.hex,
                render=False,
            )
        self.plotter.add_text(
            preview.disclaimer,
            name='lighting-scene-disclaimer',
            position='lower_edge',
            font_size=8,
            color=DARK_THEME.text.secondary.hex,
            render=False,
        )
        # add_legend has no render kwarg and renders internally; suppress so
        # the compositing render still ends in a single draw.
        self.plotter.suppress_rendering = True
        try:
            self.plotter.add_legend(
                labels=list(preview.stage_legend) + list(preview.zone_legend),
                loc='lower left',
                face='rectangle',
                size=(0.2, 0.035 * (len(preview.stage_legend) + len(preview.zone_legend)) + 0.02),
                bcolor=DARK_THEME.text.secondary.hex,
                border=False,
                background_opacity=0.55,
                name='lighting-scene-legend',
            )
        finally:
            self.plotter.suppress_rendering = False
        self._render()

    # -- 運用クリアランス layer (issue #1010) ----------------------------------
    #
    # Read-only display of the persisted ``operational_zones`` authority:
    # each declared zone renders as the XY footprint
    # ``operational_zone_footprint`` produced — a shaded floor fill plus an
    # outline — and every ``operational_clearance_conflicts`` record
    # highlights BOTH sides (the owning zone plus the conflicting entity,
    # other zone, or room boundary). Zones that cannot be evaluated draw
    # dim UNKNOWN markers; physical entities that declare no zones draw
    # dim 未宣言 badges — neither is ever presented as "no interference".
    # Zones with a declared height get a faint prism; zones without stay
    # flat and earn no "clear in height" claim anywhere.

    _OPCLEAR_FLOOR_Z_M = 0.012
    _OPCLEAR_CLASH_Z_M = 0.018
    _OPCLEAR_OUTLINE_Z_M = 0.024

    def clear_operational_clearance_overlay(self) -> None:
        """Drop every ``opclear-*`` actor (toggle off / refresh)."""
        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if not actors:
            return
        removed = False
        for name in tuple(actors):
            if isinstance(name, str) and name.startswith('opclear-'):
                self.plotter.remove_actor(name)
                removed = True
        if removed:
            self._render()

    def render_operational_clearance_overlay(
        self,
        preview: OperationalClearancePreview | None,
    ) -> None:
        """Draw the read-only 運用クリアランス layer (#1010).

        ``None`` clears the overlay. Every actor is non-pickable — this is
        an explanation surface, never an input device; zone authoring
        stays on the approved entity-edit path.
        """
        self.clear_operational_clearance_overlay()
        if preview is None:
            return

        for zone in preview.zones:
            kind_color = OPERATIONAL_ZONE_KIND_VOCAB.get(
                zone.kind, ('?', '#9aa3b2')
            )[1]
            outline_color = (
                OPERATIONAL_CONFLICT_COLOR if zone.in_conflict else kind_color
            )
            fill = _shapely_xy_fill_mesh(zone.footprint, self._OPCLEAR_FLOOR_Z_M)
            if fill is not None:
                self.plotter.add_mesh(
                    fill,
                    color=kind_color,
                    opacity=0.16 if zone.in_conflict else 0.10,
                    lighting=False,
                    pickable=False,
                    name=f'opclear-zone-{zone.entity_id}-{zone.zone_id}',
                    render=False,
                )
            outline = _shapely_xy_outline_mesh(
                zone.footprint, self._OPCLEAR_OUTLINE_Z_M
            )
            if outline is not None:
                self.plotter.add_mesh(
                    outline,
                    color=outline_color,
                    line_width=3 if zone.in_conflict else 2,
                    opacity=0.9,
                    pickable=False,
                    name=f'opclear-zone-outline-{zone.entity_id}-{zone.zone_id}',
                    render=False,
                )
            # A declared height renders a faint prism — the only volume
            # the layer ever draws. Zones without height data stay flat;
            # the disclaimer refuses the 'clear in height' claim for them.
            if zone.height_max_m is not None:
                thickness = float(zone.height_max_m) - float(zone.height_min_m)
                prism = _shapely_xy_fill_mesh(
                    zone.footprint, float(zone.height_min_m)
                )
                if thickness > 0 and prism is not None:
                    volume = prism.extrude((0.0, 0.0, thickness), capping=True)
                    self.plotter.add_mesh(
                        volume,
                        color=kind_color,
                        opacity=0.07,
                        lighting=False,
                        pickable=False,
                        name=(
                            f'opclear-zone-volume-{zone.entity_id}-{zone.zone_id}'
                        ),
                        render=False,
                    )

        for conflict in preview.conflicts:
            region_mesh = _shapely_xy_fill_mesh(
                conflict.region, self._OPCLEAR_CLASH_Z_M
            )
            if region_mesh is not None:
                self.plotter.add_mesh(
                    region_mesh,
                    color=OPERATIONAL_CONFLICT_COLOR,
                    opacity=0.42,
                    lighting=False,
                    pickable=False,
                    name=f'opclear-clash-{conflict.entity_id}-{conflict.zone_id}',
                    render=False,
                )
            # Room-boundary highlight for leaves_room — the wall section
            # the zone covers, plus any tangent point contacts.
            boundary_mesh = _segments_render_mesh(
                [
                    segment
                    for line in _iter_linear_members(conflict.boundary_geometry)
                    for segment in _xy_segments(line.coords)
                ],
                self._OPCLEAR_OUTLINE_Z_M,
            )
            if boundary_mesh is not None:
                self.plotter.add_mesh(
                    boundary_mesh,
                    color=OPERATIONAL_CONFLICT_COLOR,
                    line_width=4,
                    opacity=0.95,
                    pickable=False,
                    name=(
                        f'opclear-boundary-{conflict.entity_id}-{conflict.zone_id}'
                    ),
                    render=False,
                )
            for point_index, point in enumerate(
                _iter_point_members(conflict.boundary_geometry)
            ):
                self.plotter.add_mesh(
                    pv.Sphere(
                        radius=0.05,
                        center=(point.x, -point.y, self._OPCLEAR_OUTLINE_Z_M),
                    ),
                    color=OPERATIONAL_CONFLICT_COLOR,
                    opacity=0.9,
                    lighting=False,
                    pickable=False,
                    name=(
                        f'opclear-boundary-pt-{conflict.entity_id}-'
                        f'{conflict.zone_id}-{point_index}'
                    ),
                    render=False,
                )
            # The conflicting side's own geometry — dual highlight.
            other_mesh = _shapely_xy_outline_mesh(
                conflict.other_footprint, self._OPCLEAR_OUTLINE_Z_M
            )
            if other_mesh is not None:
                self.plotter.add_mesh(
                    other_mesh,
                    color=OPERATIONAL_CONFLICT_COLOR,
                    line_width=3,
                    opacity=0.95,
                    pickable=False,
                    name=f'opclear-conflict-entity-{conflict.other_entity_id}',
                    render=False,
                )
            other_zone_mesh = _shapely_xy_outline_mesh(
                conflict.other_zone_footprint, self._OPCLEAR_OUTLINE_Z_M
            )
            if other_zone_mesh is not None:
                self.plotter.add_mesh(
                    other_zone_mesh,
                    color=OPERATIONAL_CONFLICT_COLOR,
                    line_width=3,
                    opacity=0.95,
                    pickable=False,
                    name=(
                        f'opclear-conflict-zone-{conflict.other_entity_id}-'
                        f'{conflict.other_zone_id}'
                    ),
                    render=False,
                )

        # UNDECLARED physical entities — clearance undetermined, never
        # conflated with a verified 'no interference' state.
        for marker in preview.undeclared:
            x, y, _z = marker.position
            self.plotter.add_mesh(
                pv.Disc(
                    center=(x, -y, marker.top_z_m + 0.02),
                    inner=0.05,
                    outer=0.085,
                    normal=(0.0, 0.0, 1.0),
                ),
                color=OPERATIONAL_UNDECLARED_COLOR,
                opacity=0.4,
                lighting=False,
                pickable=False,
                name=f'opclear-undeclared-{marker.entity_id}',
                render=False,
            )

        # UNKNOWN zones — footprint could not be evaluated. A dim
        # wireframe bead on the owning entity, visually distinct from any
        # clear or conflict state.
        for zone in preview.undefined_zones:
            if zone.marker_position is None:
                continue
            x, y, top_z = zone.marker_position
            self.plotter.add_mesh(
                pv.Sphere(radius=0.06, center=(x, -y, top_z + 0.05)),
                color=OPERATIONAL_UNKNOWN_ZONE_COLOR,
                style='wireframe',
                line_width=2,
                opacity=0.8,
                lighting=False,
                pickable=False,
                name=f'opclear-unknown-{zone.entity_id}-{zone.zone_id}',
                render=False,
            )

        self.plotter.add_text(
            preview.summary,
            name='opclear-summary',
            position='upper_right',
            font_size=9,
            color=DARK_THEME.text.secondary.hex,
            render=False,
        )
        self.plotter.add_text(
            preview.disclaimer,
            name='opclear-disclaimer',
            position='left_edge',
            font_size=8,
            color=DARK_THEME.text.secondary.hex,
            render=False,
        )
        if preview.kind_legend:
            # add_legend has no render kwarg and renders internally;
            # suppress so the compositing render ends in a single draw.
            self.plotter.suppress_rendering = True
            try:
                self.plotter.add_legend(
                    labels=list(preview.kind_legend),
                    loc='lower left',
                    face='rectangle',
                    size=(0.19, 0.035 * len(preview.kind_legend) + 0.02),
                    bcolor=DARK_THEME.text.secondary.hex,
                    border=False,
                    background_opacity=0.55,
                    name='opclear-legend',
                )
            finally:
                self.plotter.suppress_rendering = False
        self._render()

    # -- A/B design-alternative diff overlay (issue #1007) ------------------
    #
    # Read-only explanation layer for saved design alternatives: every
    # verdict comes from ``diff_alternatives`` via DesignAbOverlayPreview —
    # the viewport draws categorized ghosts, it never compares meshes.
    # All actors are non-pickable and use the ``abdiff-`` prefix so the
    # sweep removes them on toggle-off / scene rebuild.

    def clear_design_ab_overlay(self) -> None:
        """Drop every ``abdiff-*`` actor (toggle off / refresh)."""
        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if not actors:
            return
        removed = False
        for name in tuple(actors):
            if isinstance(name, str) and name.startswith('abdiff-'):
                self.plotter.remove_actor(name)
                removed = True
        if removed:
            self._render()

    def _abdiff_ghost(
        self,
        entity: SceneEntity,
        *,
        name: str,
        color: str,
        opacity: float,
        line_width: float,
    ) -> None:
        """One bounded wireframe ghost of a pinned entity."""
        mesh, _glyphs, _env = entity_render_meshes(entity)
        if mesh is None:
            return
        self.plotter.add_mesh(
            mesh,
            color=color,
            style='wireframe',
            line_width=line_width,
            opacity=opacity,
            pickable=False,
            name=name,
            render=False,
        )

    def render_design_ab_overlay(
        self,
        preview: DesignAbOverlayPreview | None,
        *,
        show_context: bool = True,
        highlight_entity_id: str | None = None,
    ) -> None:
        """Draw the A/B alternative-diff layer (#1007).

        ``None`` clears the overlay. A preview in state 'impossible'
        renders only its honest reason text — no guessed geometry.
        ``show_context=False`` is the 差分のみ mode: unchanged entities
        and the unchanged room shell are not drawn.
        """
        self.clear_design_ab_overlay()
        if preview is None:
            return

        if preview.state == 'impossible':
            self.plotter.add_text(
                '比較不可\n' + '\n'.join(preview.impossible_reasons),
                name='abdiff-blocked',
                position='upper_right',
                font_size=9,
                color=OPERATIONAL_CONFLICT_COLOR,
                render=False,
            )
            self.plotter.add_text(
                preview.disclaimer,
                name='abdiff-disclaimer',
                position='left_edge',
                font_size=8,
                color=DARK_THEME.text.secondary.hex,
                render=False,
            )
            self.plotter.add_axes(
                color=DARK_THEME.text.muted.hex,
                line_width=1,
                labels_off=True,
            )
            self._render()
            return

        # Frame context: the shared room shell in a neutral color, or the
        # A/B shells in their category colors when the room itself moved.
        if preview.before_document is not None and (
            preview.context_changed
            and any('部屋' in line for line in preview.context_changed)
        ):
            before_shell = _room_wireframe(preview.before_document)
            if before_shell is not None:
                self.plotter.add_mesh(
                    before_shell,
                    color=AB_OVERLAY_CATEGORY_VOCAB['removed'][1],
                    style='wireframe',
                    line_width=2,
                    opacity=0.5,
                    pickable=False,
                    name='abdiff-room-a',
                    render=False,
                )
            after_shell = (
                _room_wireframe(preview.after_document)
                if preview.after_document is not None
                else None
            )
            if after_shell is not None:
                self.plotter.add_mesh(
                    after_shell,
                    color=AB_OVERLAY_CATEGORY_VOCAB['added'][1],
                    style='wireframe',
                    line_width=2,
                    opacity=0.5,
                    pickable=False,
                    name='abdiff-room-b',
                    render=False,
                )
        elif preview.after_document is not None:
            shell = _room_wireframe(preview.after_document)
            if shell is not None:
                self.plotter.add_mesh(
                    shell,
                    color=AB_OVERLAY_CONTEXT_COLOR,
                    style='wireframe',
                    line_width=1,
                    opacity=0.3,
                    pickable=False,
                    name='abdiff-room',
                    render=False,
                )

        if show_context:
            for entity in preview.unchanged_entities:
                self._abdiff_ghost(
                    entity,
                    name=f'abdiff-context-{entity.entity_id}',
                    color=AB_OVERLAY_CONTEXT_COLOR,
                    opacity=0.14,
                    line_width=1,
                )

        for item in preview.items:
            color = AB_OVERLAY_CATEGORY_VOCAB[item.category][1]
            highlighted = item.entity_id == highlight_entity_id
            width = 4 if highlighted else 2
            if item.category == 'removed' and item.before_entity is not None:
                self._abdiff_ghost(
                    item.before_entity,
                    name=f'abdiff-removed-{item.entity_id}',
                    color=color,
                    opacity=0.65 if highlighted else 0.5,
                    line_width=width,
                )
            elif item.category == 'added' and item.after_entity is not None:
                self._abdiff_ghost(
                    item.after_entity,
                    name=f'abdiff-added-{item.entity_id}',
                    color=color,
                    opacity=0.85 if highlighted else 0.65,
                    line_width=width,
                )
            elif item.category in ('moved', 'position_unverified'):
                # Both persisted positions — origin outline dim, destination
                # outline bright; an authoritative arrow only in 'moved'
                # (shared frame, both positions evidenced).
                if item.before_entity is not None:
                    self._abdiff_ghost(
                        item.before_entity,
                        name=f'abdiff-movedfrom-{item.entity_id}',
                        color=color,
                        opacity=0.4,
                        line_width=width,
                    )
                if item.after_entity is not None:
                    self._abdiff_ghost(
                        item.after_entity,
                        name=f'abdiff-movedto-{item.entity_id}',
                        color=color,
                        opacity=0.85 if highlighted else 0.7,
                        line_width=width + (1 if highlighted else 0),
                    )
                if item.category == 'moved' and (
                    item.before_entity is not None
                    and item.after_entity is not None
                ):
                    start = domain_to_render(item.before_entity.position)
                    end = domain_to_render(item.after_entity.position)
                    delta = tuple(e - s for s, e in zip(start, end))
                    if sum(d * d for d in delta) > 1e-12:
                        self.plotter.add_mesh(
                            pv.Arrow(
                                start=start,
                                direction=delta,
                                tip_length=0.2,
                                tip_radius=0.05,
                                shaft_radius=0.018,
                                scale='auto',
                            ),
                            color=color,
                            opacity=0.95,
                            pickable=False,
                            name=f'abdiff-arrow-{item.entity_id}',
                            render=False,
                        )
            elif item.category == 'attribute_only':
                outline_source = item.after_entity or item.before_entity
                if outline_source is not None:
                    self._abdiff_ghost(
                        outline_source,
                        name=f'abdiff-attr-{item.entity_id}',
                        color=color,
                        opacity=0.9 if highlighted else 0.7,
                        line_width=width + 1,
                    )

        self.plotter.add_text(
            preview.summary,
            name='abdiff-summary',
            position='upper_right',
            font_size=9,
            color=DARK_THEME.text.secondary.hex,
            render=False,
        )
        self.plotter.add_text(
            preview.disclaimer,
            name='abdiff-disclaimer',
            position='left_edge',
            font_size=8,
            color=DARK_THEME.text.secondary.hex,
            render=False,
        )
        if preview.legend:
            self.plotter.suppress_rendering = True
            try:
                self.plotter.add_legend(
                    labels=list(preview.legend),
                    loc='lower left',
                    face='rectangle',
                    size=(0.2, 0.035 * len(preview.legend) + 0.02),
                    bcolor=DARK_THEME.text.secondary.hex,
                    border=False,
                    background_opacity=0.55,
                    name='abdiff-legend',
                )
            finally:
                self.plotter.suppress_rendering = False
        self.plotter.add_axes(
            color=DARK_THEME.text.muted.hex,
            line_width=1,
            labels_off=True,
        )
        self._render()

    # #1005: installation-feasibility inspection layer. Every glyph color
    # comes from the authority verdict vocabulary — UNKNOWN is gray (不明),
    # never presented as a red failure. Mount faces, recess/service
    # volumes and the cavity prism only appear for dimensions with
    # declared evidence; an assembly without in-wall cavity data draws
    # the face + glyphs and leaves the depth question to the panel's
    # on-site confirmation list.

    _INSTALL_FEASIBILITY_PREFIX = 'installation-feasibility-'

    def clear_installation_feasibility_overlay(self) -> None:
        """Drop every ``installation-feasibility-*`` actor."""
        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if not actors:
            return
        removed = False
        for name in tuple(actors):
            if isinstance(name, str) and name.startswith(
                self._INSTALL_FEASIBILITY_PREFIX
            ):
                self.plotter.remove_actor(name)
                removed = True
        if removed:
            self._render()

    def _wall_face_render_mesh(self, face: WallMountFace) -> pv.PolyData:
        """Two-triangle quad of a wall mount face (domain → render)."""
        cx, cy, cz = face.center
        ux, uy, _uz = face.axis_u
        hw, hh = face.width_m * 0.5, face.height_m * 0.5
        corners = [
            (cx - ux * hw, cy - uy * hw, cz - hh),
            (cx + ux * hw, cy + uy * hw, cz - hh),
            (cx + ux * hw, cy + uy * hw, cz + hh),
            (cx - ux * hw, cy - uy * hw, cz + hh),
        ]
        points = [(x, -y, z) for (x, y, z) in corners]
        return pv.PolyData(
            np.asarray(points, dtype=float),
            [3, 0, 1, 2, 3, 0, 2, 3],
        )

    def _wall_face_outline_mesh(self, face: WallMountFace) -> pv.PolyData:
        cx, cy, cz = face.center
        ux, uy, _uz = face.axis_u
        hw, hh = face.width_m * 0.5, face.height_m * 0.5
        corners = [
            (cx - ux * hw, cy - uy * hw, cz - hh),
            (cx + ux * hw, cy + uy * hw, cz - hh),
            (cx + ux * hw, cy + uy * hw, cz + hh),
            (cx - ux * hw, cy - uy * hw, cz + hh),
        ]
        points = [(x, -y, z) for (x, y, z) in corners]
        return pv.PolyData(
            np.asarray(points, dtype=float), [5, 0, 1, 2, 3, 0]
        )

    def _face_base_mesh(self, item) -> pv.PolyData | None:
        """Mount-face triangle mesh for either face kind (render frame)."""
        if item.wall_face is not None:
            return self._wall_face_render_mesh(item.wall_face)
        if item.slab_face is not None:
            return _shapely_xy_fill_mesh(
                item.slab_face.footprint, item.slab_face.z_m
            )
        return None

    def _volume_mesh(
        self,
        base: pv.PolyData | None,
        item,
        depth_m: float,
    ) -> pv.PolyData | None:
        """Extrude the mount face into the element by ``depth_m``."""
        if base is None or depth_m <= 0.0:
            return None
        face = item.wall_face
        if face is not None:
            direction = face.recess_direction
        elif item.slab_face is not None:
            direction = item.slab_face.recess_direction
        else:
            return None
        vector = (
            direction[0] * depth_m,
            -direction[1] * depth_m,  # domain → render y flip
            direction[2] * depth_m,
        )
        volume = base.extrude(vector, capping=True)
        return volume

    def render_installation_feasibility_overlay(
        self,
        preview: InstallationFeasibilityPreview | None,
    ) -> None:
        """Draw the read-only 設置実現性検査 layer (#1005).

        ``None`` clears the overlay. Every actor is non-pickable — this
        is an evidence inspection surface, never an input device. The
        preview arrives pre-keyed to the current head: glyph anchors and
        volumes are rebuilt from the live document every render, so an
        edited scene or changed assembly can never leave a stale verdict
        on screen.
        """
        self.clear_installation_feasibility_overlay()
        if preview is None:
            return

        for item in preview.items:
            color = (
                FEASIBILITY_VERDICT_VOCAB[item.overall][1]
                if item.overall is not None
                else FEASIBILITY_VERDICT_VOCAB['unknown'][1]
            )
            unevaluated = item.overall is None
            if item.entity_anchor is not None:
                ex, ey, ez = item.entity_anchor
                self.plotter.add_mesh(
                    pv.Sphere(radius=0.055, center=(ex, -ey, ez + 0.03)),
                    color=color,
                    style='wireframe' if unevaluated else None,
                    line_width=2,
                    opacity=0.75 if unevaluated else 0.9,
                    lighting=False,
                    pickable=False,
                    name=(
                        f'{self._INSTALL_FEASIBILITY_PREFIX}'
                        f'glyph-{item.entity_id}'
                    ),
                    render=False,
                )
            if item.substrate_anchor is not None:
                sx, sy, sz = item.substrate_anchor
                self.plotter.add_mesh(
                    pv.Sphere(radius=0.045, center=(sx, -sy, sz)),
                    color=color,
                    opacity=0.85,
                    lighting=False,
                    pickable=False,
                    name=(
                        f'{self._INSTALL_FEASIBILITY_PREFIX}'
                        f'substrate-{item.entity_id}'
                    ),
                    render=False,
                )

            base = self._face_base_mesh(item)
            if base is not None:
                self.plotter.add_mesh(
                    base,
                    color=color,
                    opacity=0.18,
                    lighting=False,
                    pickable=False,
                    name=(
                        f'{self._INSTALL_FEASIBILITY_PREFIX}'
                        f'face-{item.entity_id}'
                    ),
                    render=False,
                )
            if item.wall_face is not None:
                self.plotter.add_mesh(
                    self._wall_face_outline_mesh(item.wall_face),
                    color=color,
                    line_width=3,
                    opacity=0.9,
                    pickable=False,
                    name=(
                        f'{self._INSTALL_FEASIBILITY_PREFIX}'
                        f'face-outline-{item.entity_id}'
                    ),
                    render=False,
                )
            elif item.slab_face is not None:
                outline = _shapely_xy_outline_mesh(
                    item.slab_face.footprint,
                    item.slab_face.z_m,
                )
                if outline is not None:
                    self.plotter.add_mesh(
                        outline,
                        color=color,
                        line_width=3,
                        opacity=0.9,
                        pickable=False,
                        name=(
                            f'{self._INSTALL_FEASIBILITY_PREFIX}'
                            f'face-outline-{item.entity_id}'
                        ),
                        render=False,
                    )

            # Translucent depth volumes — requirement (amber/cyan) vs
            # assembly-declared cavity (green). A cavity prism only
            # exists when the assembly actually declares the depth.
            if item.cutout_volume is not None:
                volume = self._volume_mesh(
                    base, item, item.cutout_volume.depth_m
                )
                if volume is not None:
                    self.plotter.add_mesh(
                        volume,
                        color='#d9a05b',
                        opacity=0.14,
                        lighting=False,
                        pickable=False,
                        name=(
                            f'{self._INSTALL_FEASIBILITY_PREFIX}'
                            f'vol-cutout-{item.entity_id}'
                        ),
                        render=False,
                    )
            if item.service_volume is not None:
                volume = self._volume_mesh(
                    base, item, item.service_volume.depth_m
                )
                if volume is not None:
                    self.plotter.add_mesh(
                        volume,
                        color='#4cc4d9',
                        opacity=0.12,
                        lighting=False,
                        pickable=False,
                        name=(
                            f'{self._INSTALL_FEASIBILITY_PREFIX}'
                            f'vol-service-{item.entity_id}'
                        ),
                        render=False,
                    )
            if item.cavity_volume is not None:
                volume = self._volume_mesh(
                    base, item, item.cavity_volume.depth_m
                )
                if volume is not None:
                    self.plotter.add_mesh(
                        volume,
                        color='#59d98c',
                        opacity=0.10,
                        lighting=False,
                        pickable=False,
                        name=(
                            f'{self._INSTALL_FEASIBILITY_PREFIX}'
                            f'vol-cavity-{item.entity_id}'
                        ),
                        render=False,
                    )

        self.plotter.add_text(
            preview.summary,
            name=f'{self._INSTALL_FEASIBILITY_PREFIX}summary',
            position='lower_right',
            font_size=9,
            color=DARK_THEME.text.secondary.hex,
            render=False,
        )
        self.plotter.add_text(
            preview.disclaimer,
            name=f'{self._INSTALL_FEASIBILITY_PREFIX}disclaimer',
            position='lower_left',
            font_size=8,
            color=DARK_THEME.text.secondary.hex,
            render=False,
        )
        if preview.verdict_legend:
            # add_legend has no render kwarg and renders internally;
            # suppress so the compositing render ends in a single draw.
            self.plotter.suppress_rendering = True
            try:
                self.plotter.add_legend(
                    labels=list(preview.verdict_legend),
                    loc='lower left',
                    face='rectangle',
                    size=(
                        0.19,
                        0.035 * len(preview.verdict_legend) + 0.02,
                    ),
                    bcolor=DARK_THEME.text.secondary.hex,
                    border=False,
                    background_opacity=0.55,
                    name=f'{self._INSTALL_FEASIBILITY_PREFIX}legend',
                )
            finally:
                self.plotter.suppress_rendering = False
        self._render()

    # -- 測定キャンペーン空間オーバーレイ (issue #1006) ----------------------
    #
    # Read-only read/select surface: SpatialCampaignDesign points at their
    # exact declared XYZ on the CURRENT SceneRevision. Marker SHAPE carries
    # the primary role (spatial holdout is a cube — it can never visually
    # merge with the optimization sphere); a flat halo ring carries the
    # executor progress channel; a stale-revision design draws dimmed
    # wireframe with all progress/validity claims withheld (lapsed).
    # Every actor is non-pickable — markers are an explanation surface,
    # never an input device.

    _CAMPAIGN_MARKER_RADIUS_M = 0.055
    _CAMPAIGN_FOCUS_RADIUS_M = 0.08
    _CAMPAIGN_LAPSED_COLOR = '#B09BC6'
    _CAMPAIGN_FOCUS_COLOR = '#F2F5F8'

    def clear_campaign_overlay(self) -> None:
        """Drop every ``campaign-overlay-*`` actor."""
        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if not actors:
            return
        removed = False
        for name in tuple(actors):
            if isinstance(name, str) and name.startswith('campaign-overlay-'):
                self.plotter.remove_actor(name)
                removed = True
        if removed:
            self._render()

    def _campaign_marker_mesh(self, marker, radius: float):
        from .room_campaign_overlay import CAMPAIGN_ROLE_GLYPH

        center = domain_to_render(marker.position)
        if marker.glyph == 'cube':
            return pv.Cube(
                center=center,
                x_length=radius * 2,
                y_length=radius * 2,
                z_length=radius * 2,
            )
        if marker.glyph == 'cone':
            return pv.Cone(
                center=center,
                direction=(0.0, 0.0, 1.0),
                height=radius * 2,
                radius=radius,
            )
        if marker.glyph == 'disc':
            return pv.Disc(
                center=center,
                inner=radius * 0.3,
                outer=radius,
                normal=(0.0, 0.0, 1.0),
            )
        if marker.glyph == 'cylinder':
            return pv.Cylinder(
                center=center,
                direction=(0.0, 0.0, 1.0),
                radius=radius * 0.55,
                height=radius * 2,
            )
        if marker.glyph == 'diamond':
            # Rotated cube ≈ octahedron silhouette — no exotic pv class.
            mesh = pv.Cube(
                center=center,
                x_length=radius * 2,
                y_length=radius * 2,
                z_length=radius * 2,
            )
            mesh = mesh.rotate_z(45.0, point=center, inplace=False)
            return mesh.rotate_x(54.7356, point=center, inplace=False)
        # 'sphere' (optimization) and 'wireframe_sphere' (diagnostic)
        return pv.Sphere(radius=radius, center=center)

    def render_campaign_overlay(self, scene) -> None:
        """Draw the read-only campaign spatial overlay (#1006).

        ``scene`` is a ``CampaignOverlayScene`` — the Qt-free resolution
        produced against the current head. ``None`` clears the overlay.
        """
        from .room_campaign_overlay import (
            CAMPAIGN_PROGRESS_COLORS,
            CAMPAIGN_ROLE_COLORS,
        )

        self.clear_campaign_overlay()
        if scene is None:
            return

        for index, marker in enumerate(scene.markers):
            center = domain_to_render(marker.position)
            radius = (
                self._CAMPAIGN_FOCUS_RADIUS_M
                if marker.focused
                else self._CAMPAIGN_MARKER_RADIUS_M
            )
            mesh = self._campaign_marker_mesh(marker, radius)
            if scene.lapsed:
                color = self._CAMPAIGN_LAPSED_COLOR
                style = 'wireframe'
            else:
                color = CAMPAIGN_ROLE_COLORS.get(marker.primary_role, '#98A2AD')
                style = 'wireframe' if marker.glyph == 'wireframe_sphere' else 'surface'
            self.plotter.add_mesh(
                mesh,
                color=color,
                opacity=0.55 if scene.lapsed else 0.92,
                lighting=style == 'surface',
                style=style,
                line_width=2,
                pickable=False,
                name=f'campaign-overlay-marker-{index}',
                render=False,
            )
            # Progress channel: a flat halo ring in the progress color.
            # Lapsed designs keep the ring but in the lapsed hue — the
            # markers stay legible as a stale cluster, never as live
            # progress.
            ring_color = (
                self._CAMPAIGN_LAPSED_COLOR
                if scene.lapsed
                else CAMPAIGN_PROGRESS_COLORS.get(
                    marker.progress, CAMPAIGN_PROGRESS_COLORS['unknown']
                )
            )
            self.plotter.add_mesh(
                pv.Disc(
                    center=center,
                    inner=radius * 1.08,
                    outer=radius * 1.38,
                    normal=(0.0, 0.0, 1.0),
                ),
                color=ring_color,
                opacity=0.45 if scene.lapsed else 0.85,
                lighting=False,
                pickable=False,
                name=f'campaign-overlay-progress-{index}',
                render=False,
            )
            if marker.focused and not scene.lapsed:
                self.plotter.add_mesh(
                    pv.Disc(
                        center=center,
                        inner=radius * 1.5,
                        outer=radius * 1.75,
                        normal=(0.0, 0.0, 1.0),
                    ),
                    color=self._CAMPAIGN_FOCUS_COLOR,
                    opacity=0.9,
                    lighting=False,
                    pickable=False,
                    name=f'campaign-overlay-focus-{index}',
                    render=False,
                )

        if scene.viewport_lines:
            self.plotter.add_text(
                '\n'.join(scene.viewport_lines),
                name='campaign-overlay-status',
                position='upper_left',
                font_size=9,
                color=DARK_THEME.text.secondary.hex,
                render=False,
            )
        if scene.legend:
            self.plotter.suppress_rendering = True
            try:
                self.plotter.add_legend(
                    labels=list(scene.legend),
                    loc='upper right',
                    face='rectangle',
                    size=(0.19, 0.035 * len(scene.legend) + 0.02),
                    bcolor=DARK_THEME.text.secondary.hex,
                    border=False,
                    background_opacity=0.55,
                    name='campaign-overlay-legend',
                )
            finally:
                self.plotter.suppress_rendering = False
        self._render()

    # -- Per-seat coverage marker overlay (issue #1001) ---------------------
    #
    # Read-only ear-position evidence points from a sealed
    # CoverageEvaluation resolved through CadCoverageRepository. The
    # viewport draws exactly the resolved scene — discrete markers + fixed
    # colour bands + geometric direction lines — and never recomputes
    # coverage, interpolates between seats, or draws a heatmap. All actors
    # are non-pickable under the ``coverage-overlay-`` prefix so the
    # signature-skipped render_document sweep drops them with the rest of
    # the overlay layers.

    _COVERAGE_MARKER_RADIUS_M = 0.055
    _COVERAGE_FOCUS_RADIUS_M = 0.082

    def clear_coverage_overlay(self) -> None:
        """Drop every ``coverage-overlay-*`` actor (toggle off / refresh)."""
        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if not actors:
            return
        removed = False
        for name in tuple(actors):
            if isinstance(name, str) and name.startswith('coverage-overlay-'):
                self.plotter.remove_actor(name)
                removed = True
        if removed:
            self._render()

    def _coverage_marker_mesh(self, marker, radius: float):
        center = domain_to_render(marker.position)
        if marker.glyph == 'cube':
            return pv.Cube(
                center=center,
                x_length=radius * 2,
                y_length=radius * 2,
                z_length=radius * 2,
            )
        return pv.Sphere(radius=radius, center=center)

    def render_coverage_overlay(self, scene) -> None:
        """Draw the resolved ``CoverageOverlayScene`` (#1001).

        ``None`` clears the overlay. A historical-resolution scene keeps
        its markers as a dimmed wireframe stale cluster — value labels,
        pass/fail claims and direction lines are withheld by the resolver,
        and the viewport only paints what it is given.
        """
        self.clear_coverage_overlay()
        if scene is None:
            return

        for index, marker in enumerate(scene.markers):
            center = domain_to_render(marker.position)
            radius = (
                self._COVERAGE_FOCUS_RADIUS_M
                if marker.focused
                else self._COVERAGE_MARKER_RADIUS_M
            )
            mesh = self._coverage_marker_mesh(marker, radius)
            self.plotter.add_mesh(
                mesh,
                color=marker.color,
                opacity=0.5 if marker.wireframe else 0.92,
                lighting=not marker.wireframe,
                style='wireframe' if marker.wireframe else 'surface',
                line_width=2,
                pickable=False,
                name=f'coverage-overlay-marker-{index}',
                render=False,
            )
            # Required seats carry a hard-floor ring so the priority
            # channel stays in the 3D view — a diagnostic/weighted seat
            # can never stand in for the required subset.
            if marker.priority_role == 'required':
                self.plotter.add_mesh(
                    pv.Disc(
                        center=center,
                        inner=radius * 1.1,
                        outer=radius * 1.4,
                        normal=(0.0, 0.0, 1.0),
                    ),
                    color=marker.color,
                    opacity=0.4 if marker.wireframe else 0.8,
                    lighting=False,
                    pickable=False,
                    name=f'coverage-overlay-required-{index}',
                    render=False,
                )
            if marker.focused:
                self.plotter.add_mesh(
                    pv.Disc(
                        center=center,
                        inner=radius * 1.55,
                        outer=radius * 1.85,
                        normal=(0.0, 0.0, 1.0),
                    ),
                    color=DARK_THEME.text.primary.hex,
                    opacity=0.9,
                    lighting=False,
                    pickable=False,
                    name=f'coverage-overlay-focus-{index}',
                    render=False,
                )
            if marker.label:
                texts = [marker.label]
                if marker.delta_label:
                    texts.append(marker.delta_label)
                self.plotter.add_point_labels(
                    np.asarray([center], dtype=float),
                    ['\n'.join(texts)],
                    text_color=marker.color,
                    shape_color=DARK_THEME.surfaces.overlay.hex,
                    shape_opacity=0.8,
                    font_size=9,
                    point_size=0,
                    always_visible=True,
                    name=f'coverage-overlay-label-{index}',
                    render=False,
                )

        # Speaker→seat geometric direction lines — drawn only when the
        # resolver produced them (evidence-determined acoustic axis), and
        # always paired with the status text that disclaims any measured
        # energy-path claim.
        for index, ray in enumerate(scene.rays):
            self.plotter.add_mesh(
                pv.Line(domain_to_render(ray.start), domain_to_render(ray.end)),
                color=ray.color,
                opacity=0.35,
                line_width=1,
                pickable=False,
                name=f'coverage-overlay-ray-{index}',
                render=False,
            )
        if scene.axis_determined and scene.source_position is not None:
            source = domain_to_render(scene.source_position)
            if scene.source_axis is not None:
                axis = scene.source_axis
                axis_end = (
                    source[0] + axis[0] * 0.55,
                    source[1] - axis[1] * 0.55,
                    source[2] + axis[2] * 0.55,
                )
                self.plotter.add_mesh(
                    pv.Line(source, axis_end),
                    color='#E58383',
                    opacity=0.9,
                    line_width=3,
                    pickable=False,
                    name='coverage-overlay-axis',
                    render=False,
                )
            self.plotter.add_mesh(
                pv.Sphere(radius=0.05, center=source),
                color='#E58383',
                opacity=0.95,
                pickable=False,
                name='coverage-overlay-source',
                render=False,
            )

        if scene.viewport_lines:
            self.plotter.add_text(
                '\n'.join(scene.viewport_lines),
                name='coverage-overlay-status',
                position='lower_left',
                font_size=9,
                color=DARK_THEME.text.secondary.hex,
                render=False,
            )
        if scene.legend:
            self.plotter.suppress_rendering = True
            try:
                self.plotter.add_legend(
                    labels=list(scene.legend),
                    loc='lower right',
                    face='rectangle',
                    size=(0.16, 0.032 * len(scene.legend) + 0.02),
                    bcolor=DARK_THEME.text.secondary.hex,
                    border=False,
                    background_opacity=0.55,
                    name='coverage-overlay-legend',
                )
            finally:
                self.plotter.suppress_rendering = False
        self._render()

    def clear_directivity_overlay(self) -> None:
        """Drop every ``directivity-*`` actor (toggle off / refresh)."""
        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if not actors:
            return
        removed = False
        for name in tuple(actors):
            if isinstance(name, str) and name.startswith('directivity-'):
                self.plotter.remove_actor(name)
                removed = True
        if removed:
            self._render()

    def render_directivity_overlay(self, scene) -> None:
        """Draw the resolved ``DirectivityOverlayScene`` (#1000).

        ``None`` clears the overlay. The viewport only paints what the
        resolver produced: measured-grid vertices/faces, honest UNKNOWN
        wireframes for speakers without sealed data, aim arrows, and the
        relative-dB legend. All text is ASCII — Mesa GL drops CJK glyphs.
        """
        self.clear_directivity_overlay()
        if scene is None:
            return

        for index, balloon in enumerate(scene.balloons):
            if balloon.origin is None:
                continue
            origin = domain_to_render(balloon.origin)
            tag = f'{index}'
            if balloon.state == 'mesh' and balloon.faces:
                points = np.asarray(
                    [domain_to_render(v.position) for v in balloon.vertices],
                    dtype=float,
                )
                faces = np.asarray(balloon.faces, dtype=np.int64)
                cells = np.hstack(
                    [
                        np.full((faces.shape[0], 1), 3, dtype=np.int64),
                        faces,
                    ]
                ).ravel()
                mesh = pv.PolyData(points, cells)
                colors = np.asarray(
                    [v.color_rgb for v in balloon.vertices], dtype=np.uint8
                )
                mesh.point_data['RGB'] = colors
                self.plotter.add_mesh(
                    mesh,
                    scalars='RGB',
                    rgb=True,
                    opacity=0.85,
                    smooth_shading=False,
                    pickable=False,
                    name=f'directivity-balloon-{tag}',
                    render=False,
                )
                # measured grid stays visible on top of the surface
                self.plotter.add_points(
                    points,
                    scalars=colors,
                    rgb=True,
                    point_size=4,
                    render_points_as_spheres=True,
                    pickable=False,
                    name=f'directivity-verts-{tag}',
                    render=False,
                )
            elif balloon.state in ('mesh', 'points') and balloon.vertices:
                points = np.asarray(
                    [domain_to_render(v.position) for v in balloon.vertices],
                    dtype=float,
                )
                colors = np.asarray(
                    [v.color_rgb for v in balloon.vertices], dtype=np.uint8
                )
                self.plotter.add_points(
                    points,
                    scalars=colors,
                    rgb=True,
                    point_size=7,
                    render_points_as_spheres=True,
                    pickable=False,
                    name=f'directivity-points-{tag}',
                    render=False,
                )
            else:
                # UNKNOWN / blocked: honest wireframe marker — never a
                # synthesized lobe.
                color = (
                    DIRECTIVITY_BLOCKED_COLOR
                    if balloon.state == 'blocked'
                    else DIRECTIVITY_UNKNOWN_COLOR
                )
                self.plotter.add_mesh(
                    pv.Sphere(radius=0.16, center=origin),
                    color=color,
                    style='wireframe',
                    opacity=0.8,
                    lighting=False,
                    pickable=False,
                    name=f'directivity-unknown-{tag}',
                    render=False,
                )

            for a_index, arrow in enumerate(balloon.arrows):
                if arrow.direction_domain is None:
                    continue
                d = arrow.direction_domain
                end = (
                    origin[0] + d[0] * 0.75,
                    origin[1] - d[1] * 0.75,
                    origin[2] + d[2] * 0.75,
                )
                self.plotter.add_mesh(
                    pv.Line(origin, end),
                    color=arrow.color,
                    line_width=3 if arrow.kind == 'aim' else 2,
                    pickable=False,
                    name=f'directivity-arrow-{tag}-{a_index}',
                    render=False,
                )

            state_tag = balloon.state.upper()
            self.plotter.add_point_labels(
                np.asarray([origin], dtype=float),
                [f'{balloon.speaker_entity_id} [{state_tag}]'],
                text_color=DARK_THEME.text.primary.hex,
                shape_color=DARK_THEME.surfaces.overlay.hex,
                shape_opacity=0.8,
                font_size=9,
                point_size=0,
                always_visible=True,
                name=f'directivity-label-{tag}',
                render=False,
            )

        if scene.viewport_lines:
            self.plotter.add_text(
                '\n'.join(scene.viewport_lines),
                name='directivity-status',
                position='upper_right',
                font_size=9,
                color=DARK_THEME.text.secondary.hex,
                render=False,
            )
        if scene.legend:
            self.plotter.suppress_rendering = True
            try:
                self.plotter.add_legend(
                    labels=list(scene.legend),
                    loc='lower left',
                    face='rectangle',
                    size=(0.14, 0.03 * len(scene.legend) + 0.02),
                    bcolor=DARK_THEME.text.secondary.hex,
                    border=False,
                    background_opacity=0.55,
                    name='directivity-legend',
                )
            finally:
                self.plotter.suppress_rendering = False
        self._render()

    def _render_labels(self, document: SceneDocument, selected_id: str | None) -> None:
        visible = [
            entity
            for entity in document.entities
            # Hidden entities are not rendered (and not pickable); labeling
            # them would leave a floating annotation for invisible geometry —
            # the same contract as the mesh path above (#482).
            if entity.entity_id not in self._hidden_ids
            and (
                not self._overlays.focus_selection
                or selected_id is None
                or entity.entity_id == selected_id
            )
        ]
        if not visible:
            return
        self.plotter.add_point_labels(
            np.asarray([domain_to_render(entity.position) for entity in visible], dtype=float),
            [entity.name for entity in visible],
            text_color=DARK_THEME.text.primary.hex,
            shape_color=DARK_THEME.surfaces.overlay.hex,
            shape_opacity=0.88,
            font_size=12,
            point_size=0,
            always_visible=True,
            name="entity-labels",
            render=False,
        )

    def set_aux_render_state(
        self,
        *,
        underlays: tuple[UnderlayRenderItem, ...] = (),
        guides: tuple[GuideRenderItem, ...] = (),
        section: SectionPlaneState | None = None,
    ) -> None:
        """Replace the non-authoritative display extras rendered on the next
        :meth:`render_document` call (floor-plan underlays, construction
        guides, display-only section plane)."""

        self._underlay_items = tuple(underlays)
        self._guide_items = tuple(guides)
        if section != self._section:
            # A section cut changes what each pixel's hit stack means — the
            # armed candidate set was captured pre-cut and is stale (#983).
            self.dismiss_pick_candidates()
        self._section = section

    def _apply_section(self, mesh: pv.PolyData) -> pv.PolyData | None:
        """Clip ``mesh`` by the display-only section plane, if armed.

        The section is presentation state: it changes only what is drawn,
        never the solver geometry. A degenerate clip result is reported as
        ``None`` (nothing to draw), never raised.
        """

        section = self._section
        if section is None or not section.enabled:
            return mesh
        origin = np.asarray(section.origin, dtype=float)
        normal = np.asarray(section.normal, dtype=float)
        render_origin = (origin[0], -origin[1], origin[2])
        render_normal = (normal[0], -normal[1], normal[2])
        clipped = mesh.clip(normal=render_normal, origin=render_origin, invert=False)
        if clipped.n_points <= 0:
            return None
        return clipped

    @staticmethod
    def _build_underlay_assets(
        item: UnderlayRenderItem,
    ) -> tuple[pv.PolyData | None, pv.Texture | None, pv.PolyData | None]:
        """Build the (quad, texture, line mesh) a frozen underlay item renders
        as — memoized per item content in ``_render_underlays``."""
        quad: pv.PolyData | None = None
        texture: pv.Texture | None = None
        if item.quad_domain is not None and item.image is not None:
            points = np.asarray(
                [(x, -y, z) for x, y, z in item.quad_domain],
                dtype=float,
            )
            quad = pv.PolyData(points, [4, 0, 1, 2, 3])
            quad.active_texture_coordinates = np.asarray(
                [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
                dtype=float,
            )
            texture = pv.Texture(item.image)
        line_mesh: pv.PolyData | None = None
        if item.segments_domain:
            segment_count = len(item.segments_domain)
            points = np.asarray(
                [
                    (x, -y, z)
                    for segment in item.segments_domain
                    for (x, y, z) in segment
                ],
                dtype=float,
            )
            lines = np.asarray(
                [
                    value
                    for index in range(segment_count)
                    for value in (2, 2 * index, 2 * index + 1)
                ],
                dtype=np.int64,
            )
            line_mesh = pv.PolyData(points)
            line_mesh.lines = lines
        return quad, texture, line_mesh

    def _render_underlays(self) -> None:
        cache = getattr(self, "_underlay_render_cache", None)
        if cache is None:
            cache = self._underlay_render_cache = {}
        alive: set[tuple] = set()
        for item in self._underlay_items:
            key = _underlay_item_key(item)
            alive.add(key)
            assets = cache.get(key)
            if assets is None:
                assets = self._build_underlay_assets(item)
                cache[key] = assets
            quad, texture, line_mesh = assets
            if quad is not None and texture is not None:
                actor = self.plotter.add_mesh(
                    quad,
                    texture=texture,
                    opacity=item.opacity,
                    lighting=False,
                    pickable=True,
                    show_scalar_bar=False,
                    name=f"underlay-{item.underlay_id}",
                    render=False,
                )
                self._actor_underlay_ids[id(actor)] = item.underlay_id
            if line_mesh is not None:
                actor = self.plotter.add_mesh(
                    line_mesh,
                    color=DARK_THEME.scientific.primary_trace.hex,
                    line_width=1,
                    opacity=min(1.0, item.opacity + 0.2),
                    pickable=True,
                    name=f"underlay-lines-{item.underlay_id}",
                    render=False,
                )
                self._actor_underlay_ids[id(actor)] = item.underlay_id
        for key in tuple(cache):
            if key not in alive:
                del cache[key]

    def _render_guides(self) -> None:
        for index, guide in enumerate(self._guide_items):
            start = (guide.start[0], -guide.start[1], guide.start[2])
            end = (guide.end[0], -guide.end[1], guide.end[2])
            self.plotter.add_mesh(
                pv.Line(start, end),
                color=DARK_THEME.scientific.cursor.hex,
                line_width=1,
                opacity=0.85,
                style="wireframe",
                pickable=False,
                name=f"guide-{index}",
                render=False,
            )

    def _picked_actor(self, actor) -> None:
        if self._suppress_next_pick:
            # The vtk release pick that lands after a marquee drag must not
            # collapse the region selection just committed.
            self._suppress_next_pick = False
            return
        if self._in_pick_dispatch:
            # vtkPicker.Pick fires EndPickEvent: any nested pick (a connected
            # slot calling pick_actor_at, or this callback re-picking) would
            # recurse. Nested dispatches are dropped; the outer pick result
            # already carries everything needed.
            return
        self._in_pick_dispatch = True
        try:
            if self._waypoint_pick_armed:
                # #1011 M3: while waypoint recording is armed every pick —
                # entity, room floor/shell, committed authoring surface —
                # resolves to the domain point on its face, never to a
                # selection. Entities stay pickable anyway; the room
                # surfaces were armed by set_waypoint_pick_armed. The pick
                # position comes off the shared picker — it is populated by
                # the Pick() whose EndPickEvent invoked this callback, so no
                # second pick (and no recursion) is needed.
                picked = self._last_pick_render_position()
                if picked is not None:
                    array = np.asarray(picked, dtype=float).reshape(-1)
                    if array.size >= 3:
                        # Render space -> domain space (Y negated).
                        self.waypointPicked.emit(
                            (
                                float(array[0]),
                                float(-array[1]),
                                float(array[2]),
                            )
                        )
                return
            underlay_id = self._actor_underlay_ids.get(id(actor))
            if underlay_id is not None:
                self.dismiss_pick_candidates()
                picked = self._last_pick_render_position()
                if picked is not None:
                    array = np.asarray(picked, dtype=float).reshape(-1)
                    if array.size >= 3:
                        # Render space -> domain space (Y negated).
                        self.underlayClicked.emit(
                            underlay_id,
                            float(array[0]),
                            float(-array[1]),
                        )
                return
            entity_id = self._actor_entity_ids.get(id(actor))
            if entity_id is not None:
                # The picker's Prop3D list is already populated by the pick
                # that fired this event — cycling must read it, not re-Pick
                # (a nested Pick fires EndPickEvent and recurses).
                entity_id = self._cycle_pick_candidate(
                    entity_id, self._current_pick_candidates()
                )
                display = self._last_display_position()
                self.entityPicked.emit(entity_id, display)
                self.entitySelected.emit(entity_id)
                self._sync_pick_candidates(display)
                return
            proposed_id = self._actor_proposed_entity_ids.get(id(actor))
            if proposed_id is not None:
                self.dismiss_pick_candidates()
                self.proposedEntitySelected.emit(proposed_id)
        finally:
            self._in_pick_dispatch = False

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QMouseEvent

        if obj is self.interactor and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.ScreenChangeInternal,
            QEvent.Type.Hide,
        ):
            # Widget/DPI changes reproject the whole scene — the armed
            # candidate stack belongs to the old frame (#983). An idle
            # cursor HUD is dropped here too; a LIVE one stays (#979):
            # firing a hide between the controller's pushes made the HUD
            # permanently invisible at 200 % DPI, and the owning gesture
            # re-anchors it on the next update regardless.
            self.dismiss_pick_candidates()
            if not self._snap_hud_active:
                self._hide_snap_hud()
        if obj is self.interactor and isinstance(event, QMouseEvent):
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._press_position = QPointF(event.position())
                self._press_camera_state = self._camera_state()
            elif (
                event.type() == QEvent.Type.MouseMove
                and self._press_position is not None
                and event.buttons() & Qt.MouseButton.LeftButton
            ):
                self._update_marquee(event.position())
                # Once the band owns the gesture, consume moves so the
                # trackball style never sees them — otherwise every box
                # select would also orbit the camera.
                if self._marquee_active:
                    return True
            elif event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                pressed = self._press_position
                self._press_position = None
                self._press_camera_state = None
                if self._marquee_active:
                    self._finish_marquee(event)
                else:
                    moved = pressed is None or (
                        (event.position() - pressed).manhattanLength() < 6.0
                    )
                    if moved and self.pick_actor_at(event.position()) is None:
                        self.dismiss_pick_candidates()
                        self.emptyClicked.emit(event.position())
        return False

    _MARQUEE_THRESHOLD_PX = 6.0
    _CYCLE_HYSTERESIS_PX = 4.0

    def _update_marquee(self, position: QPointF) -> None:
        """Grow/activate the left-drag rubber band once past the click slop."""

        origin = self._press_position
        if origin is None:
            return
        rect = QRectF(origin, position).normalized()
        if not self._marquee_active and (
            rect.width() < self._MARQUEE_THRESHOLD_PX
            and rect.height() < self._MARQUEE_THRESHOLD_PX
        ):
            return
        if self._marquee_band is None:
            self._marquee_band = QRubberBand(
                QRubberBand.Shape.Rectangle, self.interactor
            )
        self._marquee_band.setGeometry(rect.toAlignedRect())
        if not self._marquee_active:
            # Moves before the threshold already reached the trackball and
            # rotated a few px; restore the press-time pose so the marquee
            # region is evaluated against the view the user drew it over.
            self._restore_camera_state(self._press_camera_state)
            self._marquee_active = True
            self._marquee_band.show()
            # Region select is a different gesture — the per-point candidate
            # stack no longer describes what the band will hit (#983).
            self.dismiss_pick_candidates()

    def _finish_marquee(self, event) -> None:
        """Resolve the marquee rect into an ordered entity selection signal."""

        band = self._marquee_band
        self._marquee_active = False
        if band is not None:
            rect = QRectF(band.geometry())
            band.hide()
        else:
            rect = QRectF(event.position(), event.position())
        # VTK still delivers its release pick for this gesture; suppress it so
        # a release over an entity cannot collapse the marquee selection.
        self._suppress_next_pick = True
        additive = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.entitiesMarqueeSelected.emit(
            self.pick_entities_in_region(rect), additive
        )

    def _camera_state(self):
        """(position, focal_point, view_up) snapshot, or None if unavailable."""

        try:
            camera = self.plotter.camera
            if camera is None:
                return None
            return (
                tuple(float(v) for v in camera.GetPosition()),
                tuple(float(v) for v in camera.GetFocalPoint()),
                tuple(float(v) for v in camera.GetViewUp()),
            )
        except Exception:
            return None

    def _restore_camera_state(self, state) -> None:
        if state is None:
            return
        self.dismiss_pick_candidates()
        try:
            camera = self.plotter.camera
            camera.SetPosition(*state[0])
            camera.SetFocalPoint(*state[1])
            camera.SetViewUp(*state[2])
        except Exception:
            return
        self._render()

    # -- Picking -------------------------------------------------------------
    #
    # Coordinate contract: every public pick entry point and every emitted
    # position uses the Qt widget convention — top-left origin, device-
    # independent pixels, the same space as ``QMouseEvent.position()``. VTK
    # display coordinates (bottom-left origin, physical pixels) are produced
    # or consumed only at the picker boundary via the two helpers below; the
    # interactor's ``GetEventPosition`` already reports VTK convention.

    def _widget_to_display_position(self, position) -> tuple[float, float] | None:
        """Qt widget coords -> VTK display coords (flip Y, scale by DPR)."""

        try:
            if position is None:
                return None
            dpr = float(self.interactor.devicePixelRatioF())
            if not isfinite(dpr) or dpr <= 0.0:
                dpr = 1.0
            height = float(self.interactor.height())
            x = float(position.x()) * dpr
            y = (height - 1.0 - float(position.y())) * dpr
        except Exception:
            return None
        if not (isfinite(x) and isfinite(y)):
            return None
        return (x, y)

    def _display_to_widget_position(self, position) -> QPointF | None:
        """VTK display coords -> Qt widget coords (inverse of the above)."""

        try:
            if position is None:
                return None
            dpr = float(self.interactor.devicePixelRatioF())
            if not isfinite(dpr) or dpr <= 0.0:
                dpr = 1.0
            height = float(self.interactor.height())
            x = float(position[0]) / dpr
            y = (height - 1.0) - float(position[1]) / dpr
        except Exception:
            return None
        if not (isfinite(x) and isfinite(y)):
            return None
        return QPointF(x, y)

    def _last_display_position(self) -> QPointF | None:
        """Last VTK pick event position, returned in Qt widget coords."""

        try:
            pos = self.interactor.GetEventPosition()
        except Exception:
            return None
        return self._display_to_widget_position(pos)

    def _last_pick_render_position(self) -> tuple[float, float, float] | None:
        """Render-space position of the pick currently being dispatched.

        Only valid inside ``_picked_actor`` — the shared interactor picker's
        pick position is still populated by the ``Pick()`` whose
        EndPickEvent invoked the callback. Reading it performs no new pick,
        so it cannot recurse through EndPickEvent.
        """

        try:
            picker = self.plotter.iren.picker
            picked = picker.GetPickPosition()
        except Exception:
            return None
        if picked is None:
            return None
        return (float(picked[0]), float(picked[1]), float(picked[2]))

    def pick_actor_at(self, position: QPointF):
        """Run the shared picker at a Qt display position; actor or None."""

        display = self._widget_to_display_position(position)
        if display is None:
            return None
        try:
            picker = self.plotter.iren.picker
            renderer = self.plotter.iren.get_poked_renderer()
            picker.Pick(display[0], display[1], 0.0, renderer)
            return picker.GetActor()
        except Exception:
            return None

    def pick_world_position(self, position: QPointF) -> tuple[float, float, float] | None:
        """Render-space world position under a Qt display point (or None)."""

        display = self._widget_to_display_position(position)
        if display is None:
            return None
        try:
            picker = self.plotter.iren.picker
            renderer = self.plotter.iren.get_poked_renderer()
            picker.Pick(display[0], display[1], 0.0, renderer)
            picked = picker.GetPickPosition()
        except Exception:
            return None
        if picked is None:
            return None
        return (float(picked[0]), float(picked[1]), float(picked[2]))

    # -- Waypoint recording (#1011 M3) ----------------------------------------
    #
    # The authoring mode coexists with entity picking exactly like the
    # measure tool does: while armed, the pick pipeline feeds
    # ``waypointPicked``/``pick_waypoint_domain`` instead of selection —
    # entity actors keep their pickable flag (a click on a speaker records
    # the point on its cabinet, where a cable would touch), and the room
    # floor/shell/authoring surfaces go pickable for the armed duration
    # only. Everything emitted is a domain-space (x, y, z) triple.

    #: Named actors that become pickable while waypoint recording is armed —
    #: the room surfaces a cable route actually runs along. Grid lines are
    #: intentionally left non-pickable: they are hairline targets that would
    #: report floor-plane points slightly off the true floor.
    _WAYPOINT_SURFACE_NAMES = ('room-floor', 'room-shell')
    _WAYPOINT_SURFACE_PREFIXES = ('authoring-surface-',)

    def set_waypoint_pick_armed(self, armed: bool) -> None:
        """Arm/disarm waypoint recording picks (#1011 M3).

        Armed: every pick emits ``waypointPicked`` (domain point) instead of
        selecting, room surfaces become pickable, and the overlapping-pick
        chooser stays suppressed so a record click cannot open it. Disarmed
        restores the pickable flags and chooser behaviour exactly — a click
        on the floor deselects again like it always did.
        """

        armed = bool(armed)
        if armed == self._waypoint_pick_armed:
            return
        self._waypoint_pick_armed = armed
        self.pick_popover_enabled = not armed
        self.dismiss_pick_candidates()
        self._apply_waypoint_pickable()

    def _apply_waypoint_pickable(self) -> None:
        """Set room-surface pickable flags to the armed state.

        ``render_document`` rebuilds the room actors on every real scene
        change, so this must re-run after a rebuild — a re-render while
        armed must never silently drop the recording surface.
        """

        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if not actors:
            return
        for name in tuple(actors):
            if not isinstance(name, str):
                continue
            if name in self._WAYPOINT_SURFACE_NAMES or name.startswith(
                self._WAYPOINT_SURFACE_PREFIXES
            ):
                actor = actors.get(name) if hasattr(actors, 'get') else None
                if actor is not None:
                    actor.SetPickable(self._waypoint_pick_armed)

    def pick_waypoint_domain(
        self, position: QPointF
    ) -> tuple[float, float, float] | None:
        """Domain-space room point under a Qt display point (#1011 M3).

        First the shared picker — while armed, room surfaces are pickable,
        so a click on them resolves to the true surface point (an entity
        click resolves to the point on its body). When nothing pickable is
        under the cursor, the click ray is intersected with the room
        boundary — floor, the footprint walls, the ceiling plane — which
        is the surface the user is actually pointing at; a ray that misses
        every boundary returns None rather than a fabricated point.
        """

        display = self._widget_to_display_position(position)
        if display is None:
            return None
        try:
            picker = self.plotter.iren.picker
            renderer = self.plotter.iren.get_poked_renderer()
            if picker.Pick(display[0], display[1], 0.0, renderer):
                got = picker.GetPickPosition()
                if got is not None and len(got) >= 3:
                    return (float(got[0]), -float(got[1]), float(got[2]))
        except Exception:
            pass
        try:
            renderer = self.plotter.renderer
            renderer.SetDisplayPoint(display[0], display[1], 0.0)
            renderer.DisplayToWorld()
            near = renderer.GetWorldPoint()
            renderer.SetDisplayPoint(display[0], display[1], 1.0)
            renderer.DisplayToWorld()
            far = renderer.GetWorldPoint()
        except Exception:
            return None
        if near is None or far is None or len(near) < 4 or len(far) < 4:
            return None
        near_domain = (near[0], -near[1], near[2])
        far_domain = (far[0], -far[1], far[2])
        return _ray_room_boundary_domain(
            near_domain, far_domain, self._document
        )

    def render_cable_route_draft(
        self,
        active_points: (
            tuple[tuple[float, float, float], ...]
            | list[tuple[float, float, float]]
        ),
        staged_segments: (
            tuple[CableRouteSegmentRouteItem, ...]
            | list[CableRouteSegmentRouteItem]
        ) = (),
    ) -> None:
        """In-progress waypoint recording preview (#1011 M3).

        The uncommitted point list draws in the draft colour under
        ``cable-route-draft-`` — visibly distinct from the recorded-route
        accent/warning palette so a work-in-progress line can never be
        mistaken for a registered route. Staged-but-uncommitted segments
        draw dimmer under the same prefix. Every actor is non-pickable
        (clicks keep recording) and the ``cable-route-`` prefix sweep drops
        them with every other overlay — nothing draft persists across a
        scene rebuild unless the controller re-pushes it.
        """

        color = DARK_THEME.semantic.stale.hex
        for segment in staged_segments:
            for leg, (first, second) in enumerate(
                zip(segment.points, segment.points[1:])
            ):
                self.plotter.add_mesh(
                    pv.Line(
                        _domain_to_render_tuple(first),
                        _domain_to_render_tuple(second),
                    ),
                    color=color,
                    line_width=2,
                    opacity=0.45,
                    pickable=False,
                    name=(
                        f"cable-route-draft-staged-"
                        f"{segment.segment_sequence}-{leg}"
                    ),
                    render=False,
                )
        points = tuple(active_points)
        for index, point in enumerate(points):
            self.plotter.add_mesh(
                pv.Sphere(
                    radius=0.04, center=_domain_to_render_tuple(point)
                ),
                color=color,
                opacity=0.95,
                pickable=False,
                name=f"cable-route-draft-point-{index}",
                render=False,
            )
            # Point order is authoring state, so the indices are drawn —
            # plain ASCII digits (VTK label fonts have no CJK coverage).
            self.plotter.add_point_labels(
                [_domain_to_render_tuple(point)],
                [str(index + 1)],
                text_color=DARK_THEME.text.primary.hex,
                shape_color=DARK_THEME.surfaces.overlay.hex,
                shape_opacity=0.85,
                font_size=9,
                point_size=0,
                always_visible=True,
                name=f"cable-route-draft-point-label-{index}",
                render=False,
            )
        for leg, (first, second) in enumerate(zip(points, points[1:])):
            self.plotter.add_mesh(
                pv.Line(
                    _domain_to_render_tuple(first),
                    _domain_to_render_tuple(second),
                ),
                color=color,
                line_width=3,
                opacity=0.85,
                pickable=False,
                name=f"cable-route-draft-line-{leg}",
                render=False,
            )
        self._render()

    # -- Field overlay (issue #999) -------------------------------------------
    #
    # The overlay is fully derived: every actor is named under
    # ``acoustic-field-``, is ``pickable=False``, and is dropped either by
    # :meth:`_remove_overlay_actors` (signature-skipped renders, then the
    # compositor re-adds the current set) or by
    # :meth:`clear_field_overlay`. No field actor survives a render where
    # the controller resolved to "not shown".

    _FIELD_CMAP = 'viridis'
    _FIELD_CYCLIC_CMAP = 'hsv'  # phase: cyclic at ±180

    def _field_image_data(self, view) -> pv.ImageData:
        """Cached ImageData for one display view — keyed by content pins."""

        key = (
            view.result_semantic_sha256,
            view.quantity,
            view.display_stride,
            view.dims,
            view.render_origin,
            view.render_spacing,
        )
        cached = self._field_image_cache.get(key)
        if cached is None:
            grid = pv.ImageData(
                dimensions=view.dims,
                origin=view.render_origin,
                spacing=view.render_spacing,
            )
            grid.point_data['field_value'] = view.scalars_xyz.ravel(order='F')
            cached = grid
            self._field_image_cache[key] = cached
            while len(self._field_image_cache) > 2:
                self._field_image_cache.popitem(last=False)
        else:
            self._field_image_cache.move_to_end(key)
        return cached

    def render_field_overlay(self, scene) -> None:
        """Draw one resolved FieldOverlayScene (slices + iso + volume + probe).

        Must run inside ``deferred_render()`` from the compositor so the
        whole frame still ends in a single ``plotter.render()``.
        """

        view = scene.view
        cmap = self._FIELD_CYCLIC_CMAP if view.cyclic_colormap else self._FIELD_CMAP
        status_extra: list[str] = []
        grid = self._field_image_data(view)

        for item in scene.slices:
            slab = pv.ImageData(
                dimensions=item.dims,
                origin=item.render_origin,
                spacing=item.render_spacing,
            )
            slab.point_data['field_value'] = item.scalars.ravel(order='F')
            self.plotter.add_mesh(
                slab,
                scalars='field_value',
                clim=view.clim,
                cmap=cmap,
                nan_color=(0.0, 0.0, 0.0),
                nan_opacity=0.0,
                show_scalar_bar=False,
                pickable=False,
                name=f"acoustic-field-slice-{item.axis_plane}-{item.display_index}",
                render=False,
            )

        if scene.iso_values:
            contours = grid.contour(isosurfaces=list(scene.iso_values))
            if contours.n_points:
                self.plotter.add_mesh(
                    contours,
                    scalars='field_value',
                    clim=view.clim,
                    cmap=cmap,
                    opacity=0.85,
                    show_scalar_bar=False,
                    pickable=False,
                    name='acoustic-field-iso',
                    render=False,
                )
            else:
                status_extra.append('等値面: 交差なし (表示されません)')

        if scene.volume_enabled:
            if min(view.dims) < 2:
                status_extra.append('ボリューム表示不可 (3D格子が必要) → 断面のみ')
            else:
                try:
                    self.plotter.add_volume(
                        grid,
                        scalars='field_value',
                        clim=view.clim,
                        cmap=cmap,
                        opacity='linear',
                        mapper='smart',
                        show_scalar_bar=False,
                        pickable=False,
                        name='acoustic-field-volume',
                        render=False,
                    )
                except Exception as exc:  # GPU/driver dependent — never fatal
                    status_extra.append(
                        f'ボリューム表示不可 ({type(exc).__name__}) → 断面のみ'
                    )

        if scene.probe is not None:
            position = scene.probe.sampled_position
            self.plotter.add_mesh(
                pv.Sphere(
                    radius=max(view.render_spacing) * 0.4,
                    center=(position.x_m, -position.y_m, position.z_m),
                ),
                color=DARK_THEME.accent.primary.hex,
                pickable=False,
                name='acoustic-field-probe-marker',
                render=False,
            )

        bar_title = view.scalar_bar_title
        self.plotter.add_scalar_bar(
            title=bar_title,
            n_labels=5,
            render=False,
            title_font_size=10,
            label_font_size=8,
            position_x=0.02,
            position_y=0.02,
        )
        self._field_scalar_bars.add(bar_title)

        self._field_probe_plane = scene.probe_plane
        self.plotter.add_text(
            '\n'.join([*scene.status_lines, *status_extra]),
            position='upper_right',
            font_size=9,
            color=DARK_THEME.text.secondary.hex,
            name='acoustic-field-status',
            render=False,
        )
        self._render()

    def clear_field_overlay(self) -> None:
        """Remove every field-overlay actor, scalar bar, and the ImageData cache."""

        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if actors:
            for name in tuple(actors):
                if isinstance(name, str) and name.startswith('acoustic-field-'):
                    self.plotter.remove_actor(name)
        for title in tuple(self._field_scalar_bars):
            try:
                self.plotter.remove_scalar_bar(title, render=False)
            except Exception:
                pass
        self._field_scalar_bars.clear()
        self._field_image_cache.clear()
        self._field_probe_plane = None

    def field_probe_world(
        self, position: QPointF
    ) -> tuple[float, float, float] | None:
        """World point for the field probe under a Qt display point (#999).

        First the usual pick; when nothing pickable is under the cursor
        (slice/volume/wireframe are all non-pickable by design) the click
        ray is intersected with the current slice's render-space plane —
        the surface the user is actually pointing at.
        """

        world = self.pick_world_position(position)
        if world is not None:
            return world
        plane = self._field_probe_plane
        if plane is None:
            return None
        display = self._widget_to_display_position(position)
        if display is None:
            return None
        try:
            renderer = self.plotter.renderer
            renderer.SetDisplayPoint(display[0], display[1], 0.0)
            renderer.DisplayToWorld()
            near = renderer.GetWorldPoint()
            renderer.SetDisplayPoint(display[0], display[1], 1.0)
            renderer.DisplayToWorld()
            far = renderer.GetWorldPoint()
        except Exception:
            return None
        if near is None or far is None or len(near) < 4 or len(far) < 4:
            return None
        axis, value = plane
        depth = far[axis] - near[axis]
        if abs(depth) < 1e-12:
            return None
        t = (value - near[axis]) / depth
        return tuple(near[i] + t * (far[i] - near[i]) for i in range(3))

    # -- Treatment coverage overlay (issue #1009) ----------------------------
    #
    # Read-only: actors under ``treatment-overlay-`` are derived patches of
    # exact-bound placements — the clipped effective footprint only, never
    # the authored rectangle. Signature-skipped renders drop them via
    # ``_remove_overlay_actors`` and the compositor re-resolves; a stale
    # scene therefore cannot keep a superseded patch on screen.

    _TREATMENT_PATCH_LIFECYCLE_COLORS = {
        'proposed': DARK_THEME.accent.primary.hex,
        'installed': DARK_THEME.semantic.success.hex,
    }

    @staticmethod
    def _treatment_patch_mesh(
        ring: tuple[tuple[float, float, float], ...],
    ) -> pv.PolyData | None:
        """Triangulate one planar render-space ring (shapely ear clipping).

        The clipped patch may be concave when the host surface carries
        openings, so a single n-gon face is not safe; triangulation in the
        ring's dominant projection matches ``_planar_polygon_mesh``.
        """

        if len(ring) < 3:
            return None
        points = np.asarray(ring, dtype=float)
        normal = _newell_normal(points)
        drop = int(np.argmax(np.abs(normal)))
        keep = [axis for axis in range(3) if axis != drop]
        poly2d = Polygon(points[:, keep])
        if not poly2d.is_valid or poly2d.area <= 1e-12:
            return None
        faces: list[int] = []
        for triangle in triangulate(poly2d):
            if not poly2d.covers(triangle.representative_point()):
                continue
            coords = np.asarray(triangle.exterior.coords[:-1])
            indices = []
            for x, y in coords:
                distance = np.hypot(
                    points[:, keep[0]] - x, points[:, keep[1]] - y
                )
                indices.append(int(np.argmin(distance)))
            if len(indices) == 3:
                faces.extend((3, *indices))
        if not faces:
            return None
        return pv.PolyData(points, np.asarray(faces, dtype=np.int64))

    def render_treatment_overlay(self, scene) -> None:
        """Draw one resolved TreatmentOverlayScene (#1009).

        Must run inside ``deferred_render()`` from the compositor so the
        whole frame still ends in a single ``plotter.render()``.
        """

        for patch in scene.patches:
            key = patch.placement.placement_sha256[:12]
            fill = self._TREATMENT_PATCH_LIFECYCLE_COLORS.get(
                patch.lifecycle, DARK_THEME.accent.primary.hex
            )
            edge = (
                DARK_THEME.semantic.warning.hex
                if patch.warning
                else fill
            )
            for index, ring in enumerate(patch.render_polygons):
                mesh = self._treatment_patch_mesh(ring)
                if mesh is None:
                    continue
                patch_actor = self.plotter.add_mesh(
                    mesh,
                    color=fill,
                    opacity=0.45,
                    pickable=False,
                    lighting=False,
                    name=f'treatment-overlay-patch-{key}-{index}',
                    render=False,
                )
                outline = pv.lines_from_points(
                    np.asarray(ring, dtype=float), close=True
                )
                outline_actor = self.plotter.add_mesh(
                    outline,
                    color=edge,
                    line_width=3,
                    pickable=False,
                    lighting=False,
                    name=f'treatment-overlay-edge-{key}-{index}',
                    render=False,
                )
                # The patch lies exactly on the host surface plane; polygon
                # offset keeps it legible over coplanar translucent walls
                # without moving the geometry off the surface.
                for actor in (patch_actor, outline_actor):
                    mapper = actor.GetMapper()
                    mapper.SetResolveCoincidentTopologyToPolygonOffset()
                    mapper.SetResolveCoincidentTopologyPolygonOffsetParameters(
                        -2.0, -2.0
                    )
        self.plotter.add_text(
            '\n'.join(scene.viewport_lines),
            position='lower_left',
            font_size=9,
            color=DARK_THEME.text.secondary.hex,
            name='treatment-overlay-status',
            render=False,
        )
        self._render()

    def clear_treatment_overlay(self) -> None:
        """Remove every treatment-overlay actor and its status text."""

        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if actors:
            for name in tuple(actors):
                if isinstance(name, str) and name.startswith(
                    'treatment-overlay-'
                ):
                    self.plotter.remove_actor(name)

    # -- #1008 fabrication package preview ----------------------------------

    class _EnvelopeBox:
        """Duck-typed stand-in carrying the clearance envelope's
        center/dims for ``_fabrication_box_mesh``."""

        __slots__ = ('center', 'dims_whs')

        def __init__(self, center, dims) -> None:
            self.center = center
            self.dims_whs = dims

    _FAB_PART_COLORS = {
        'absorber_layer': DARK_THEME.accent.primary.hex,
        'backing_panel': '#8d7b68',
        'well_fin': DARK_THEME.semantic.success.hex,
        'frame_member': '#b8a88f',
        'facing': '#a8b6c2',
        'spacer': '#7d8a96',
    }
    _FAB_DERIVED_COLOR = DARK_THEME.text.secondary.hex
    _FAB_SELECTED_COLOR = DARK_THEME.semantic.warning.hex
    _FAB_REFERENCE_COLOR = DARK_THEME.semantic.warning.hex

    @staticmethod
    def _fabrication_box_mesh(box, anchor) -> pv.PolyData:
        """Oriented box for one resolved preview part.

        Local cube X→width axis, Y→stack (depth) axis, Z→height axis, in
        domain space; the matrix maps straight into VTK render space
        (domain Y negated), exactly like ``domain_to_render``."""

        width, height, depth = box.dims_whs
        mesh = pv.Cube(
            center=(0.0, 0.0, 0.0),
            x_length=width,
            y_length=depth,
            z_length=height,
        )
        wa, sa, ha = (
            anchor.width_axis,
            anchor.stack_axis,
            anchor.height_axis,
        )
        cx, cy, cz = box.center
        matrix = np.asarray(
            (
                (wa[0], sa[0], ha[0], cx),
                (-wa[1], -sa[1], -ha[1], -cy),
                (wa[2], sa[2], ha[2], cz),
                (0.0, 0.0, 0.0, 1.0),
            ),
            dtype=float,
        )
        mesh.transform(matrix, inplace=True)
        return mesh

    def render_fabrication_preview(self, scene) -> None:
        """Draw one resolved FabricationPreviewScene (#1008).

        Read-only: every actor is non-pickable and named under the
        'fabrication-' prefix so a signature-skipped render or a
        disarm/close sweeps the whole preview. Must run inside
        ``deferred_render()`` from the compositor.
        """

        self.clear_fabrication_preview()
        if not scene.supported:
            self.plotter.add_text(
                '\n'.join(scene.viewport_lines),
                position='lower_left',
                font_size=9,
                color=DARK_THEME.text.secondary.hex,
                name='fabrication-status',
                render=False,
            )
            self._render()
            return

        anchor = scene.anchor
        wa_render = (-anchor.width_axis[0], anchor.width_axis[1],
                     -anchor.width_axis[2])
        section_origin = (
            None
            if scene.section_position is None
            else (
                scene.section_position[0],
                -scene.section_position[1],
                scene.section_position[2],
            )
        )
        label_points: list[tuple[float, float, float]] = []
        label_texts: list[str] = []
        seen_label_index: set[int] = set()
        for box in scene.parts:
            mesh = self._fabrication_box_mesh(box, anchor)
            if scene.section_fraction is not None and section_origin is not None:
                clipped = mesh.clip(
                    normal=wa_render, origin=section_origin, invert=False
                )
                if clipped.n_points > 0:
                    mesh = clipped
                cross = mesh.slice(
                    normal=(-wa_render[0], -wa_render[1], -wa_render[2]),
                    origin=section_origin,
                )
                if cross.n_points > 0:
                    self.plotter.add_mesh(
                        cross,
                        color=DARK_THEME.semantic.warning.hex,
                        line_width=3,
                        pickable=False,
                        lighting=False,
                        name=(
                            f'fabrication-section-{box.index}-'
                            f'{box.part_id.replace("@", "-")}'
                        ),
                        render=False,
                    )
            color = (
                self._FAB_SELECTED_COLOR
                if box.selected
                else (
                    self._FAB_DERIVED_COLOR
                    if box.derived
                    else self._FAB_PART_COLORS.get(
                        box.part_kind, DARK_THEME.accent.primary.hex
                    )
                )
            )
            safe_id = ''.join(
                ch if ch.isalnum() or ch in '-_' else '-'
                for ch in box.part_id
            )
            actor = self.plotter.add_mesh(
                mesh,
                color=color,
                opacity=0.35 if box.derived else (0.9 if box.selected else 0.7),
                style='wireframe' if box.derived else 'surface',
                pickable=False,
                name=f'fabrication-part-{box.index}-{safe_id}',
                render=False,
            )
            if box.selected:
                edges = getattr(mesh, 'extract_all_edges', None)
                edge_mesh = (
                    edges() if callable(edges) else mesh.extract_edges()
                )
                self.plotter.add_mesh(
                    edge_mesh,
                    color=self._FAB_SELECTED_COLOR,
                    line_width=4,
                    pickable=False,
                    lighting=False,
                    name=f'fabrication-select-{box.index}-{safe_id}',
                    render=False,
                )
            if box.index not in seen_label_index:
                seen_label_index.add(box.index)
                label_points.append(
                    (box.center[0], -box.center[1], box.center[2])
                )
                label_texts.append(box.label)

        for well in scene.well_labels:
            label_points.append(
                (well.position[0], -well.position[1], well.position[2])
            )
            label_texts.append(well.label)
        if label_points:
            self.plotter.add_point_labels(
                label_points,
                label_texts,
                text_color=DARK_THEME.text.primary.hex,
                shape_color=DARK_THEME.surfaces.overlay.hex,
                shape_opacity=0.8,
                font_size=8,
                point_size=0,
                always_visible=True,
                name='fabrication-labels',
                render=False,
            )

        reference = scene.reference
        if reference is not None:
            ring = np.asarray(
                [(x, -y, z) for x, y, z in reference.mount_ring],
                dtype=float,
            )
            if len(ring) >= 3:
                self.plotter.add_mesh(
                    pv.lines_from_points(ring, close=True),
                    color=self._FAB_REFERENCE_COLOR,
                    line_width=2,
                    pickable=False,
                    lighting=False,
                    name=(
                        f'fabrication-ref-mount-'
                        f'{reference.placement_instance_id}'
                    ),
                    render=False,
                )
            envelope = self._EnvelopeBox(
                center=reference.clearance_center,
                dims=reference.clearance_dims,
            )
            envelope_mesh = self._fabrication_box_mesh(envelope, anchor)
            self.plotter.add_mesh(
                envelope_mesh,
                color=self._FAB_REFERENCE_COLOR,
                opacity=0.12,
                pickable=False,
                lighting=False,
                name=(
                    f'fabrication-ref-clearance-'
                    f'{reference.placement_instance_id}'
                ),
                render=False,
            )
        self.plotter.add_text(
            '\n'.join(scene.viewport_lines),
            position='lower_left',
            font_size=9,
            color=DARK_THEME.text.secondary.hex,
            name='fabrication-status',
            render=False,
        )
        self._render()

    def clear_fabrication_preview(self) -> None:
        """Remove every fabrication-preview actor and its status text."""

        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if actors:
            for name in tuple(actors):
                if isinstance(name, str) and name.startswith(
                    'fabrication-'
                ):
                    self.plotter.remove_actor(name)

    # -- #1003 screen quality map -------------------------------------------

    _QUALITY_MARKER_UNMEASURED = 'white'
    _QUALITY_MARKER_UNSCALED = 'gray'
    _QUALITY_MARKER_MISALIGNED = DARK_THEME.semantic.warning.hex
    _QUALITY_MARKER_OUTLINE = 'white'

    def render_screen_quality_overlay(self, scene) -> None:
        """Draw one resolved ScreenQualityMapScene (#1003).

        Measured points draw as filled markers in the absolute-scale color;
        unmeasured plan points draw as transparent outline markers; cells of
        a *sha-verified* derived-map artifact draw underneath as quads.
        Everything comes from the resolver — no colors or positions are
        synthesized here. Must run inside ``deferred_render()``.
        """

        if scene is None:
            return
        document = self._document
        # Same staleness contract as render_video_overlay: a scene resolved
        # against a superseded head must never paint.
        if (
            document is not None
            and scene.revision_content_hash != scene_content_hash(document)
        ):
            return

        quad = scene.surface_render_quad
        if quad is None or len(quad) != 4:
            # Text-only scene (no surface / stale evaluation) still gets its
            # honesty banner so the reason is visible in the viewport.
            if scene.viewport_lines:
                self.plotter.add_text(
                    '\n'.join(scene.viewport_lines),
                    position='lower_left',
                    font_size=9,
                    color=DARK_THEME.text.secondary.hex,
                    name='quality-map-status',
                    render=False,
                )
                self._render()
            return

        # Surface frame in render space for marker placement.
        bl, br, tr, tl = quad
        u_axis = np.subtract(br, bl)
        v_axis = np.subtract(tl, bl)
        normal = np.cross(u_axis, v_axis)
        width_m = float(np.linalg.norm(u_axis))
        height_m = float(np.linalg.norm(v_axis))
        if width_m > 0.0:
            u_axis = u_axis / width_m
        if height_m > 0.0:
            v_axis = v_axis / height_m
        normal_len = float(np.linalg.norm(normal))
        normal = normal / normal_len if normal_len > 0.0 else np.array(
            [0.0, 0.0, 1.0]
        )
        marker_radius = max(0.02, 0.018 * min(width_m, height_m))

        if scene.heatmap is not None:
            for cell in scene.heatmap.cells:
                if len(cell.render_quad) != 4:
                    continue
                points = np.asarray(cell.render_quad, dtype=float)
                mesh = pv.PolyData(points, [4, 0, 1, 2, 3])
                actor = self.plotter.add_mesh(
                    mesh,
                    color=cell.fill_color or self._QUALITY_MARKER_UNSCALED,
                    opacity=0.55,
                    pickable=False,
                    lighting=False,
                    name=(
                        f'quality-map-heat-{cell.row}-{cell.column}'
                    ),
                    render=False,
                )
                mapper = actor.GetMapper()
                mapper.SetResolveCoincidentTopologyToPolygonOffset()
                mapper.SetResolveCoincidentTopologyPolygonOffsetParameters(
                    -1.0, -1.0
                )

        fill_allowed = not scene.read_only
        for marker in scene.markers:
            if marker.render_xyz is None:
                continue
            center = np.asarray(marker.render_xyz, dtype=float)
            ring = np.asarray(
                [
                    center - u_axis * marker_radius - v_axis * marker_radius,
                    center + u_axis * marker_radius - v_axis * marker_radius,
                    center + u_axis * marker_radius + v_axis * marker_radius,
                    center - u_axis * marker_radius + v_axis * marker_radius,
                ],
                dtype=float,
            )
            key = marker.point_id
            if marker.state == 'measured' and fill_allowed and marker.fill_color:
                disc = pv.Disc(
                    center=center,
                    inner=0.0,
                    outer=marker_radius,
                    normal=normal,
                    c_res=24,
                )
                actor = self.plotter.add_mesh(
                    disc,
                    color=marker.fill_color,
                    opacity=0.9,
                    pickable=False,
                    lighting=False,
                    name=f'quality-map-pt-{key}',
                    render=False,
                )
                mapper = actor.GetMapper()
                mapper.SetResolveCoincidentTopologyToPolygonOffset()
                mapper.SetResolveCoincidentTopologyPolygonOffsetParameters(
                    -2.0, -2.0
                )
                edge_color = self._QUALITY_MARKER_OUTLINE
            else:
                edge_color = {
                    'unmeasured': self._QUALITY_MARKER_UNMEASURED,
                    'unscaled': self._QUALITY_MARKER_UNSCALED,
                    'misaligned': self._QUALITY_MARKER_MISALIGNED,
                }.get(marker.state, self._QUALITY_MARKER_UNSCALED)
            self.plotter.add_mesh(
                pv.lines_from_points(ring, close=True),
                color=edge_color,
                line_width=2,
                pickable=False,
                lighting=False,
                name=f'quality-map-edge-{key}',
                render=False,
            )

        if scene.viewport_lines:
            self.plotter.add_text(
                '\n'.join(scene.viewport_lines),
                position='lower_left',
                font_size=9,
                color=DARK_THEME.text.secondary.hex,
                name='quality-map-status',
                render=False,
            )
        self._render()

    def clear_screen_quality_overlay(self) -> None:
        """Remove every quality-map actor and its status text (#1003)."""

        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if actors:
            for name in tuple(actors):
                if isinstance(name, str) and name.startswith('quality-map-'):
                    self.plotter.remove_actor(name)

    # -- as-built survey overlay (#1004) -----------------------------------

    #: Per-mode bucket fills. 'unknown'/record-less buckets always resolve
    #: to the same grey — a no-data element is never painted 0mm-green.
    _SURVEY_MODE_COLORS: dict[str, dict[str, str]] = {
        'tier': {
            't0': '#6E7B8A',
            't1': '#8FB8C9',
            't2': '#E4C06B',
            't3': '#35B779',
            't4': '#FDE725',
            'unknown': '#6E7B8A',
        },
        'uncertainty_mm': {
            'le5': '#35B779',
            'le25': '#FDE725',
            'le50': '#E1B15A',
            'gt50': '#E58383',
            'unknown': '#6E7B8A',
        },
        'verification': {
            'verified': '#68B98A',
            'unverified': '#E1B15A',
            'stale': '#E8A05C',
            'failed': '#E58383',
            'unknown': '#6E7B8A',
        },
        'delta_mm': {
            'delta_le2': '#35B779',
            'delta_le10': '#FDE725',
            'delta_le50': '#E1B15A',
            'delta_gt50': '#E58383',
            'delta_na': '#6E7B8A',
            'none': '#6E7B8A',
            'unknown': '#6E7B8A',
        },
    }
    _SURVEY_CONTROL_COLORS: dict[str, str] = {
        'pass': '#68B98A',
        'fail': '#E58383',
        'evidence_only': '#8FB8C9',
        'reg_used': '#9B88A0',
        'unmapped': '#6E7B8A',
    }

    def render_survey_overlay(self, scene) -> None:
        """Draw one resolved SurveyOverlayScene (#1004).

        Must run inside ``deferred_render()`` from the compositor. Fills
        carry the mode bucket colour; per-element text and control labels
        appear only at near zoom — zoomed out the scene keeps the
        aggregate counts + ASCII legend so thin detail is never
        exaggerated. ``delta_mm`` mode draws label+number on the surface
        only: the authority carries no displacement direction, so no
        arrows are ever invented.
        """

        self.clear_survey_overlay()
        colors = self._SURVEY_MODE_COLORS.get(scene.mode, {})
        unknown_fill = self._SURVEY_MODE_COLORS['verification']['unknown']
        camera = getattr(self.plotter, 'camera', None)
        zoomed_out = False
        if camera is not None:
            try:
                # Key on the visible world span, never camera distance:
                # this viewport zooms via SetParallelScale / camera.Zoom,
                # so GetDistance() does not track wheel zoom and would
                # leave labels permanently suppressed or shown.
                if camera.GetParallelProjection():
                    visible_half = float(camera.GetParallelScale())
                else:
                    visible_half = float(camera.GetDistance()) * tan(
                        radians(float(camera.GetViewAngle()) / 2.0)
                    )
                zoomed_out = visible_half > max(
                    scene.scene_diagonal_m * 2.5, 12.0
                )
            except Exception:
                zoomed_out = False

        label_points: list = []
        label_texts: list = []
        for index, item in enumerate(scene.elements):
            target = item.target
            if target is None:
                continue
            color = colors.get(item.bucket, unknown_fill)
            name = f'survey-overlay-item-{index}'
            mesh = None
            if target.kind == 'surface':
                mesh = _planar_polygon_mesh(
                    target.compiled_geometry, target.surface
                )
            elif target.kind == 'semantic_surface':
                mesh = _semantic_surface_overlay_mesh(
                    target.semantic_geometry, target.semantic_surface
                )
            elif target.kind == 'entity':
                body, _glyphs, _envelope = entity_render_meshes(target.entity)
                mesh = body
            if mesh is not None:
                fill_actor = self.plotter.add_mesh(
                    mesh,
                    color=color,
                    opacity=0.38,
                    pickable=False,
                    lighting=False,
                    name=f'{name}-fill',
                    render=False,
                )
                edge = self._survey_surface_outline(target)
                if edge is not None:
                    edge_actor = self.plotter.add_mesh(
                        edge,
                        color=color,
                        line_width=3,
                        pickable=False,
                        lighting=False,
                        name=f'{name}-edge',
                        render=False,
                    )
                    for actor in (fill_actor, edge_actor):
                        mapper = actor.GetMapper()
                        mapper.SetResolveCoincidentTopologyToPolygonOffset()
                        mapper.SetResolveCoincidentTopologyPolygonOffsetParameters(
                            -2.0, -2.0
                        )
                else:
                    mapper = fill_actor.GetMapper()
                    mapper.SetResolveCoincidentTopologyToPolygonOffset()
                    mapper.SetResolveCoincidentTopologyPolygonOffsetParameters(
                        -2.0, -2.0
                    )
            if not zoomed_out:
                label_points.append(
                    _domain_to_render_tuple(target.centroid_domain)
                )
                label_texts.append(item.label_ascii)

        marker_radius = max(scene.scene_diagonal_m * 0.006, 0.02)
        for index, item in enumerate(scene.controls):
            if not item.anchors:
                continue
            color = self._SURVEY_CONTROL_COLORS.get(
                item.bucket, self._SURVEY_CONTROL_COLORS['unmapped']
            )
            name = f'survey-overlay-ctrl-{index}'
            render_anchors = [
                _domain_to_render_tuple(point) for point in item.anchors
            ]
            for point in render_anchors:
                self.plotter.add_mesh(
                    pv.Sphere(
                        radius=marker_radius, center=point, theta_resolution=12
                    ),
                    color=color,
                    pickable=False,
                    lighting=False,
                    name=f'{name}-pt',
                    render=False,
                )
            if len(render_anchors) >= 2:
                self.plotter.add_mesh(
                    pv.Line(render_anchors[0], render_anchors[-1]),
                    color=color,
                    line_width=3,
                    pickable=False,
                    lighting=False,
                    name=f'{name}-line',
                    render=False,
                )
            if not zoomed_out:
                mid = render_anchors[len(render_anchors) // 2]
                label_points.append(mid)
                label_texts.append(item.label_ascii)

        if label_points:
            self.plotter.add_point_labels(
                label_points,
                label_texts,
                font_size=22,
                text_color='#F2F5F8',
                shape_color='#1B232C',
                shape_opacity=0.75,
                always_visible=True,
                pickable=False,
                name='survey-overlay-labels',
                render=False,
            )
        self.plotter.add_text(
            '\n'.join(scene.viewport_lines),
            position='upper_left',
            font_size=9,
            color=DARK_THEME.text.secondary.hex,
            name='survey-overlay-status',
            render=False,
        )
        # Arm the zoom-refresh hook only once the paint is complete: a
        # mid-render exception must not leave the overlay "armed" with a
        # partial actor set that wheel-zoom would keep repainting.
        self._survey_overlay_scene = scene
        self._render()

    def _survey_surface_outline(self, target) -> pv.PolyData | None:
        """Closed ring outline for a resolved target (render coords)."""

        try:
            if target.kind == 'surface':
                points = [
                    _domain_to_render_tuple(
                        target.compiled_geometry.vertices[i].point()
                    )
                    for i in target.surface.outer_vertex_indices
                ]
            elif target.kind == 'semantic_surface':
                tri_ids = set(target.semantic_surface.triangle_ids)
                vertex_ids = sorted(
                    {
                        index
                        for triangle in target.semantic_geometry.triangles
                        if triangle.triangle_id in tri_ids
                        for index in (triangle.a, triangle.b, triangle.c)
                    }
                )
                if not vertex_ids:
                    return None
                # Hull outline is not derivable without topology walking —
                # draw the wireframe edges instead (honest, no invented
                # boundary).
                edges: list[tuple[int, int]] = []
                id_to_new = {
                    old: new for new, old in enumerate(vertex_ids)
                }
                for triangle in target.semantic_geometry.triangles:
                    if triangle.triangle_id not in tri_ids:
                        continue
                    edges.extend(
                        (
                            (triangle.a, triangle.b),
                            (triangle.b, triangle.c),
                            (triangle.c, triangle.a),
                        )
                    )
                pts = np.asarray(
                    [
                        _domain_to_render_tuple(
                            (
                                target.semantic_geometry.vertices[i].x_m,
                                target.semantic_geometry.vertices[i].y_m,
                                target.semantic_geometry.vertices[i].z_m,
                            )
                        )
                        for i in vertex_ids
                    ],
                    dtype=float,
                )
                lines: list[int] = []
                for a, b in edges:
                    lines.extend((2, id_to_new[a], id_to_new[b]))
                mesh = pv.PolyData(pts)
                mesh.lines = np.asarray(lines, dtype=np.int64)
                return mesh
            elif target.kind == 'entity':
                _body, _glyphs, envelope = entity_render_meshes(
                    target.entity
                )
                return envelope
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        if len(points) < 3:
            return None
        return pv.lines_from_points(np.asarray(points, dtype=float), close=True)

    def clear_survey_overlay(self) -> None:
        """Remove every survey-overlay actor, labels, and status text."""

        self._survey_overlay_scene = None
        self._survey_zoom_timer.stop()
        renderer = getattr(self.plotter, 'renderer', None)
        actors = getattr(renderer, 'actors', None)
        if actors:
            for name in tuple(actors):
                if isinstance(name, str) and name.startswith(
                    'survey-overlay-'
                ):
                    self.plotter.remove_actor(name)

    def _survey_zoom_refresh(self) -> None:
        """Debounced zoom hook: re-evaluate the visible-span label gate.

        Wheel zoom never re-runs the compositor, so without this the
        zoomed-out aggregate view (and its near-zoom labels) could only
        change on an unrelated re-resolve. Re-rendering the armed scene
        is read-only — the resolver cache is untouched.
        """

        scene = self._survey_overlay_scene
        if scene is not None:
            self.render_survey_overlay(scene)

    def pick_actor_candidates(self, position: QPointF) -> tuple[str, ...]:
        """Entity ids under a Qt display point, ordered front-to-back."""

        display = self._widget_to_display_position(position)
        if display is None:
            return ()
        try:
            picker = self.plotter.iren.picker
            renderer = self.plotter.iren.get_poked_renderer()
            picker.Pick(display[0], display[1], 0.0, renderer)
            props = picker.GetProp3Ds()
        except Exception:
            return ()
        return self._candidates_from_props(props)

    def _current_pick_candidates(self) -> tuple[str, ...]:
        """Front-to-back entity ids of the pick that just fired — no re-Pick.

        Only safe inside ``_picked_actor`` (the picker's Prop3D collection is
        populated by the pick whose EndPickEvent invoked the callback).
        """

        try:
            props = self.plotter.iren.picker.GetProp3Ds()
        except Exception:
            return ()
        return self._candidates_from_props(props)

    def _candidates_from_props(self, props) -> tuple[str, ...]:
        ordered: list[str] = []
        if props is not None:
            try:
                props.InitTraversal()
                while True:
                    prop = props.GetNextProp()
                    if prop is None:
                        break
                    entity_id = self._actor_entity_ids.get(id(prop))
                    if entity_id is not None and entity_id not in ordered:
                        ordered.append(entity_id)
            except Exception:
                pass
        return tuple(ordered)

    def _cycle_pick_candidate(self, entity_id: str, candidates: tuple[str, ...]) -> str:
        """Click-through: repeated clicks at one spot walk the hit stack.

        VTK already reports every intersected prop via ``GetProp3Ds``; the
        front-to-back list is cycled while the click point stays inside the
        hysteresis radius and the candidate set is unchanged.
        """

        position = self._last_display_position()
        if entity_id not in candidates:
            candidates = (entity_id, *candidates)
        if (
            position is not None
            and self._cycle_position is not None
            and hypot(
                position.x() - self._cycle_position.x(),
                position.y() - self._cycle_position.y(),
            )
            <= self._CYCLE_HYSTERESIS_PX
            and self._cycle_ids == candidates
        ):
            index = (self._cycle_index + 1) % len(candidates)
        else:
            index = candidates.index(entity_id)
        self._cycle_position = position
        self._cycle_ids = candidates
        self._cycle_index = index
        return candidates[index]

    # -- Overlapping-pick chooser (#983) ---------------------------------------
    #
    # The chooser makes the invisible click-through cycle explicit: while it
    # is open the wheel and arrow keys step a preview highlight across the
    # captured stack, Enter commits that candidate through the same
    # entityPicked/entitySelected path as a click, and Esc closes it without
    # touching the selection. The candidate set is the picker's own hit stack
    # — this feature performs no additional scene picks.

    _PICK_PREVIEW_PREFIX = 'pick-candidate-'

    @property
    def pick_candidates_active(self) -> bool:
        """Whether the overlapping-pick chooser is currently open."""

        return (
            self._pick_popover is not None
            and not self._pick_popover.isHidden()
        )

    def pick_candidate_ids(self) -> tuple[str, ...]:
        """Entity ids of the armed candidate stack, front-to-back."""

        return tuple(entry.entity_id for entry in self._pick_entries)

    def _sync_pick_candidates(self, anchor: QPointF | None) -> None:
        """Reflect the pick that just fired in the chooser (#983).

        Runs after the entityPicked/entitySelected emissions so the rows and
        preview describe the post-refresh scene. Single-hit picks (and picks
        taken while an edit gesture suppresses the chooser) close it.
        """

        candidates = self._cycle_ids
        if not self.pick_popover_enabled or len(candidates) < 2:
            self.dismiss_pick_candidates()
            return
        self._pick_entries = tuple(
            self._pick_entry_for(entity_id) for entity_id in candidates
        )
        self._pick_index = min(self._cycle_index, len(self._pick_entries) - 1)
        self._pick_anchor = QPointF(anchor) if anchor is not None else None
        self._show_pick_popover()
        self._refresh_pick_preview()
        self._render()

    def _pick_entry_for(self, entity_id: str) -> PickCandidateEntry:
        """Resolve one candidate id into a chooser row (name/kind/reason)."""

        document = self._document
        entity = None
        if document is not None:
            try:
                entity = document.entity(entity_id)
            except KeyError:
                entity = None
        reasons: list[str] = []
        if entity is None:
            reasons.append('シーンに存在しません')
        else:
            if entity_id in self._hidden_ids:
                reasons.append('非表示')
            if entity_id in self._locked_ids:
                reasons.append('ロック中')
        provider = self.pick_candidate_reason_provider
        if provider is not None:
            try:
                extra = provider(entity_id)
            except Exception:
                extra = None
            if extra:
                reasons.append(str(extra))
        kind = entity.kind if entity is not None else ''
        return PickCandidateEntry(
            entity_id=entity_id,
            name=entity.name if entity is not None else entity_id,
            kind=kind,
            kind_label=_PICK_KIND_LABELS.get(kind, kind),
            reason='・'.join(reasons) if reasons else None,
        )

    def _show_pick_popover(self) -> None:
        if self._pick_popover is None:
            self._pick_popover = _PickCandidatePopover(self.interactor)
            self._pick_popover.activated.connect(self._confirm_pick_candidate)
            self._pick_popover.step_requested.connect(
                self._step_pick_candidates
            )
        self._pick_popover.set_entries(self._pick_entries, self._pick_index)
        popover = self._pick_popover
        popover.adjustSize()
        anchor = self._pick_anchor
        x = int(anchor.x()) + 16 if anchor is not None else 16
        y = int(anchor.y()) + 12 if anchor is not None else 16
        # Keep the chooser inside the viewport — flip left/up near edges.
        max_x = max(4, self.interactor.width() - popover.width() - 4)
        max_y = max(4, self.interactor.height() - popover.height() - 4)
        # A ToolTip window positions in global screen coordinates.
        popover.move(
            self.interactor.mapToGlobal(
                QPointF(
                    max(4, min(x, max_x)), max(4, min(y, max_y))
                ).toPoint()
            )
        )
        popover.show()
        popover.raise_()
        if self._pick_key_filter is None:
            # Installed last, so it runs ahead of the camera/transform
            # filters attached at workspace wiring time.
            self._pick_key_filter = _PickCandidateKeyFilter(self)
            self.interactor.installEventFilter(self._pick_key_filter)

    def _step_pick_candidates(self, step: int) -> None:
        """Move the armed candidate by ``step`` (wraps) — preview only."""

        if not self._pick_entries:
            return
        self._pick_index = (self._pick_index + step) % len(self._pick_entries)
        if self._pick_popover is not None:
            self._pick_popover.set_index(self._pick_index)
        self._refresh_pick_preview()
        self._render()

    def _confirm_pick_candidate(self, entity_id: object = None) -> None:
        """Commit the armed (or clicked) candidate as the selection.

        Goes through the same entityPicked/entitySelected emissions a click
        produces, so the objects list (#978), inspector and view state stay
        in lockstep. The click-through cursor is also advanced so a
        following same-spot click continues past the confirmed index.
        """

        if not self._pick_entries:
            return
        ids = [entry.entity_id for entry in self._pick_entries]
        if entity_id is None:
            index = self._pick_index
        elif str(entity_id) in ids:
            index = ids.index(str(entity_id))
        else:
            return
        target = ids[index]
        anchor = self._pick_anchor
        self._cycle_ids = tuple(ids)
        self._cycle_index = index
        self.entityPicked.emit(
            target, QPointF(anchor) if anchor is not None else None
        )
        self.entitySelected.emit(target)
        self.dismiss_pick_candidates()

    def dismiss_pick_candidates(self) -> None:
        """Drop the armed candidate stack, chooser and preview (#983)."""

        had_state = (
            bool(self._pick_entries)
            or self._pick_preview_name is not None
            or (
                self._pick_popover is not None
                and not self._pick_popover.isHidden()
            )
        )
        self._pick_entries = ()
        self._pick_index = 0
        self._pick_anchor = None
        if self._pick_key_filter is not None:
            try:
                self.interactor.removeEventFilter(self._pick_key_filter)
            except Exception:
                pass
            self._pick_key_filter.deleteLater()
            self._pick_key_filter = None
        if self._pick_popover is not None:
            self._pick_popover.hide()
        self._remove_pick_preview()
        if had_state:
            self._render()

    def _restore_pick_candidates(
        self,
        previous_document: SceneDocument | None,
        previous_hidden: frozenset[str],
    ) -> None:
        """Revalidate the armed stack across a scene rebuild (#983).

        Selection/lock re-renders rebuild actors but keep the hit stack —
        the chooser survives with refreshed rows and a re-added preview.
        Document or hidden-set changes alter what each pixel would hit, so
        the captured set is stale and must be discarded.
        """

        if not self._pick_entries:
            return
        if (
            self._document != previous_document
            or self._hidden_ids != previous_hidden
        ):
            self.dismiss_pick_candidates()
            return
        self._pick_entries = tuple(
            self._pick_entry_for(entry.entity_id)
            for entry in self._pick_entries
        )
        if (
            self._pick_popover is not None
            and not self._pick_popover.isHidden()
        ):
            self._pick_popover.set_entries(self._pick_entries, self._pick_index)
        self._refresh_pick_preview()

    def _remove_pick_preview(self) -> None:
        name = self._pick_preview_name
        self._pick_preview_name = None
        if name is None:
            return
        try:
            self.plotter.remove_actor(name, render=False)
        except Exception:
            pass

    def _refresh_pick_preview(self) -> None:
        """Draw/remove the preview-only highlight for the armed candidate.

        Preview-only means a render-space wireframe envelope — nothing here
        mutates the document, view state, or any SceneRevision. When the
        armed candidate is already the primary selection the real selection
        outline is drawn instead and no preview is needed.
        """

        target: str | None = None
        if self._pick_entries:
            entity_id = self._pick_entries[self._pick_index].entity_id
            if entity_id != self._selected_id:
                target = entity_id
        if target is None:
            self._remove_pick_preview()
            return
        name = f'{self._PICK_PREVIEW_PREFIX}{target}'
        if self._pick_preview_name == name:
            return
        self._remove_pick_preview()
        document = self._document
        if document is None:
            return
        try:
            entity = document.entity(target)
        except KeyError:
            return
        _body, _glyphs, envelope = entity_render_meshes(entity)
        mesh = envelope if envelope is not None else _body
        mesh = self._apply_section(mesh)
        if mesh is None:
            return
        self.plotter.add_mesh(
            mesh,
            color=DARK_THEME.viewport.selection_outline.hex,
            style='wireframe',
            line_width=3,
            opacity=0.95,
            pickable=False,
            name=name,
            render=False,
        )
        self._pick_preview_name = name

    def pick_entities_in_region(self, rect: QRectF) -> list[str]:
        """Entity ids whose projected bounds intersect a Qt-space marquee rect."""

        try:
            dpr = float(self.interactor.devicePixelRatioF())
            if not isfinite(dpr) or dpr <= 0.0:
                dpr = 1.0
            height = float(self.interactor.height())
            if not isfinite(height) or height <= 0.0:
                return []
            x_lo = min(float(rect.left()), float(rect.right())) * dpr
            x_hi = max(float(rect.left()), float(rect.right())) * dpr
            # Qt y grows downward; VTK display y grows upward — the rect's
            # top edge maps to the larger display y.
            y_lo = (height - 1.0 - max(float(rect.top()), float(rect.bottom()))) * dpr
            y_hi = (height - 1.0 - min(float(rect.top()), float(rect.bottom()))) * dpr
        except Exception:
            return []
        ordered: list[str] = []
        seen: set[str] = set()
        for entity_id, actor in self._marquee_actors:
            if entity_id in seen:
                continue
            try:
                bounds = actor.GetBounds()
            except Exception:
                continue
            if bounds is None or len(bounds) != 6:
                continue
            xs: list[float] = []
            ys: list[float] = []
            try:
                for cx in (bounds[0], bounds[1]):
                    for cy in (bounds[2], bounds[3]):
                        for cz in (bounds[4], bounds[5]):
                            sx, sy = self.world_to_screen((cx, cy, cz))
                            xs.append(sx)
                            ys.append(sy)
            except Exception:
                continue
            if not xs or not all(isfinite(v) for v in (*xs, *ys)):
                continue
            if (
                x_lo <= max(xs)
                and x_hi >= min(xs)
                and y_lo <= max(ys)
                and y_hi >= min(ys)
            ):
                ordered.append(entity_id)
                seen.add(entity_id)
        return ordered

    def world_to_screen(self, position: tuple[float, float, float]) -> tuple[float, float]:
        """Project a render-space world point to display coordinates (snap selector)."""

        renderer = self.plotter.renderer
        renderer.SetWorldPoint(float(position[0]), float(position[1]), float(position[2]), 1.0)
        renderer.WorldToDisplay()
        display = renderer.GetDisplayPoint()
        return (float(display[0]), float(display[1]))

    def world_to_widget_position(self, position: tuple[float, float, float]) -> QPointF | None:
        """Project a render-space world point to interactor DIP coordinates.

        The snap HUD anchors in the same DIP space as
        ``QMouseEvent.position()`` — unlike ``world_to_screen``, which
        reports VTK display pixels (bottom-left origin, DPR-scaled). Under
        200 % DPI the two differ by exactly the device pixel ratio; returns
        ``None`` when the point cannot be projected.
        """

        try:
            display = self.world_to_screen(position)
        except Exception:
            return None
        return self._display_to_widget_position(display)

    @property
    def snap_hud(self) -> _SnapHud | None:
        """The live cursor-side snap HUD while it is visible (tests/diagnostics)."""

        if self._snap_hud is not None and not self._snap_hud.isHidden():
            return self._snap_hud
        return None

    @property
    def snap_hud_anchor(self) -> QPointF | None:
        """Last clamped HUD origin in interactor DIP coords (tests)."""

        return self._snap_hud_anchor

    def _clamped_hud_origin(self, anchor: QPointF, hud: QWidget) -> QPointF:
        """Cursor-neighbourhood placement that never covers the target (#979).

        The HUD opens right+below the anchor; near the viewport edges it
        flips to the left/above side of the cursor so it cannot sit on top
        of the point being snapped to, then clamps to a 4 px margin inside
        the interactor. Clamping is purely local — it also behaves on
        multi-monitor layouts and at 200 % DPI since both the anchor and
        the bounds are already DIP values.
        """

        width = float(self.interactor.width())
        height = float(self.interactor.height())
        hud_w = float(hud.width())
        hud_h = float(hud.height())
        x = float(anchor.x()) + 18.0
        y = float(anchor.y()) + 14.0
        if x + hud_w > width - 4.0:
            x = float(anchor.x()) - hud_w - 8.0
        if y + hud_h > height - 4.0:
            y = float(anchor.y()) - hud_h - 8.0
        x = max(4.0, min(x, max(4.0, width - hud_w - 4.0)))
        y = max(4.0, min(y, max(4.0, height - hud_h - 4.0)))
        return QPointF(x, y)

    def _show_snap_hud(
        self,
        lines: tuple[str, ...],
        screen_position: tuple[float, float] | QPointF,
    ) -> None:
        if self._snap_hud is None:
            self._snap_hud = _SnapHud(self.interactor)
        hud = self._snap_hud
        hud.set_lines(lines)
        if isinstance(screen_position, QPointF):
            anchor = QPointF(screen_position)
        else:
            anchor = QPointF(float(screen_position[0]), float(screen_position[1]))
        origin = self._clamped_hud_origin(anchor, hud)
        self._snap_hud_anchor = origin
        # A ToolTip window positions in global screen coordinates — correct
        # across monitors as long as the interactor maps it.
        hud.move(self.interactor.mapToGlobal(origin.toPoint()))
        self._snap_hud_active = True
        hud.show()
        hud.raise_()

    def _hide_snap_hud(self) -> None:
        self._snap_hud_active = False
        self._snap_hud_anchor = None
        if self._snap_hud is not None:
            self._snap_hud.hide()

    def render_snap_feedback(
        self,
        label: str | None,
        *,
        screen_position: tuple[float, float] | QPointF | None = None,
        hud_lines: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        """Snap-target/transform indicator during a drag (#481, HUD #979).

        With ``screen_position`` (the candidate/cursor point converted to
        interactor DIP coords) the cursor-side ``_SnapHud`` carries the
        feedback at the operation site; ``hud_lines`` supplies the richer
        readout (delta, snap state, numeric echo) and falls back to
        ``label`` alone. Without a position the renderer's lower-left text
        stays as the documented fallback — e.g. when the candidate point
        cannot be projected.
        """

        try:
            self.plotter.remove_actor("snap-feedback-label", render=False)
        except Exception:
            pass
        lines = tuple(hud_lines or ())
        if screen_position is not None and not lines and label:
            lines = (label,)
        if screen_position is not None and lines:
            self._show_snap_hud(lines, screen_position)
            self._render()
            return
        self._hide_snap_hud()
        if not label:
            return
        try:
            self.plotter.add_text(
                label,
                name="snap-feedback-label",
                position="lower_left",
                font_size=9,
                color=DARK_THEME.viewport.selection_outline.hex,
                render=False,
            )
        except Exception:
            return
        self._render()

    def begin_pan(self, position: QPointF) -> None:
        # Gesture lifetime is owned by CadInputController; this renderer only
        # applies normalized deltas.
        del position

    def pan_by(self, delta: QPointF) -> None:
        self.dismiss_pick_candidates()
        camera = self.plotter.camera
        position = np.asarray(camera.GetPosition(), dtype=float)
        focal = np.asarray(camera.GetFocalPoint(), dtype=float)
        direction = focal - position
        distance = float(np.linalg.norm(direction))
        if distance <= 1e-9:
            return
        forward = direction / distance
        up = np.asarray(camera.GetViewUp(), dtype=float)
        up_norm = float(np.linalg.norm(up))
        if up_norm <= 1e-9:
            return
        up /= up_norm
        right = np.cross(forward, up)
        right_norm = float(np.linalg.norm(right))
        if right_norm <= 1e-9:
            return
        right /= right_norm
        self._standard_view = CUSTOM_VIEW

        _, height = self.plotter.render_window.GetSize()
        pixel_height = max(float(height), 1.0)
        if camera.GetParallelProjection():
            world_per_pixel = (2.0 * float(camera.GetParallelScale())) / pixel_height
        else:
            world_per_pixel = (
                2.0
                * distance
                * tan(radians(float(camera.GetViewAngle())) * 0.5)
                / pixel_height
            )
        shift = (
            -float(delta.x()) * world_per_pixel * right
            + float(delta.y()) * world_per_pixel * up
        )
        camera.SetPosition(*(position + shift))
        camera.SetFocalPoint(*(focal + shift))
        self._render()

    def end_pan(self, position: QPointF) -> None:
        del position

    def begin_orbit(self, position: QPointF) -> None:
        del position

    def orbit_by(self, delta: QPointF) -> None:
        self.dismiss_pick_candidates()
        camera = self.plotter.camera
        camera.Azimuth(-float(delta.x()) * 0.25)
        camera.Elevation(float(delta.y()) * 0.25)
        camera.OrthogonalizeViewUp()
        self._standard_view = CUSTOM_VIEW
        self.plotter.reset_camera_clipping_range()
        self._render()

    def end_orbit(self, position: QPointF) -> None:
        del position

    def zoom_by(self, steps: float, position: QPointF) -> None:
        del position
        if abs(float(steps)) <= 1e-12:
            return
        # Camera zooms also invalidate the pixel the stack was picked at —
        # note the chooser's own wheel handling never reaches this path.
        self.dismiss_pick_candidates()
        camera = self.plotter.camera
        factor = 1.15 ** float(steps)
        if camera.GetParallelProjection():
            camera.SetParallelScale(float(camera.GetParallelScale()) / factor)
        else:
            camera.Zoom(factor)
        self.plotter.reset_camera_clipping_range()
        self._render()
        if self._survey_overlay_scene is not None:
            self._survey_zoom_timer.start()

    def open_context_menu(
        self,
        position: QPointF,
        global_position: QPointF,
    ) -> None:
        # Command content remains outside the renderer and can be supplied by the
        # Room workspace / central command registry.
        self.dismiss_pick_candidates()
        self.contextMenuRequested.emit(QPointF(position), QPointF(global_position))

    @property
    def standard_view(self) -> str:
        """The canonical view the camera is in, or ``'custom'`` after a free
        orbit."""
        return self._standard_view

    def apply_standard_view(self, view: StandardView | str) -> None:
        """Snap the camera to one of the six canonical working views.

        Orthographic views enable parallel projection; ``PERSPECTIVE``
        restores a perspective isometric-style framing. The camera only
        changes what is rendered — it never writes SceneRevisions.
        """

        view = StandardView(view)
        self.dismiss_pick_candidates()
        camera = self.plotter.camera
        self._standard_view = view.value
        if view is StandardView.PERSPECTIVE:
            camera.SetParallelProjection(0)
            bounds = self.plotter.bounds
            center = np.asarray(
                [
                    (bounds[0] + bounds[1]) * 0.5,
                    (bounds[2] + bounds[3]) * 0.5,
                    (bounds[4] + bounds[5]) * 0.5,
                ],
                dtype=float,
            )
            extent = float(
                max(
                    bounds[1] - bounds[0],
                    bounds[3] - bounds[2],
                    bounds[5] - bounds[4],
                    1.0,
                )
            )
            direction = np.asarray([-0.45, -0.78, -0.44], dtype=float)
            direction /= np.linalg.norm(direction)
            camera.SetFocalPoint(*center)
            camera.SetPosition(*(center - direction * extent * 2.2))
            camera.SetViewUp(0.0, 0.0, 1.0)
            self.plotter.reset_camera()
            self.plotter.reset_camera_clipping_range()
            self._render()
            return
        direction_domain, up_domain = STANDARD_VIEW_GEOMETRY[view]
        direction = np.asarray(
            (direction_domain[0], -direction_domain[1], direction_domain[2]),
            dtype=float,
        )
        up = np.asarray(
            (up_domain[0], -up_domain[1], up_domain[2]),
            dtype=float,
        )
        bounds = self.plotter.bounds
        center = np.asarray(
            [
                (bounds[0] + bounds[1]) * 0.5,
                (bounds[2] + bounds[3]) * 0.5,
                (bounds[4] + bounds[5]) * 0.5,
            ],
            dtype=float,
        )
        extent = float(
            max(
                bounds[1] - bounds[0],
                bounds[3] - bounds[2],
                bounds[5] - bounds[4],
                1.0,
            )
        )
        camera.SetFocalPoint(*center)
        camera.SetPosition(*(center - direction * extent * 2.0))
        camera.SetViewUp(*up)
        camera.SetParallelProjection(1)
        self.plotter.reset_camera()
        self.plotter.reset_camera_clipping_range()
        self._render()

    def capture_camera_state(self) -> RoomCameraState:
        """Snapshot the live camera as a domain-space :class:`RoomCameraState`."""

        camera = self.plotter.camera
        parallel = bool(camera.GetParallelProjection())
        position = camera.GetPosition()
        focal = camera.GetFocalPoint()
        view_up = camera.GetViewUp()
        return RoomCameraState(
            standard_view=self._standard_view,
            projection='parallel' if parallel else 'perspective',
            position=(float(position[0]), float(-position[1]), float(position[2])),
            focal_point=(float(focal[0]), float(-focal[1]), float(focal[2])),
            view_up=(float(view_up[0]), float(-view_up[1]), float(view_up[2])),
            parallel_scale=float(camera.GetParallelScale()) if parallel else None,
            view_angle=float(camera.GetViewAngle()) if not parallel else None,
        )

    def apply_camera_state(self, state: RoomCameraState) -> None:
        """Restore a previously captured camera record (display-only)."""

        camera = self.plotter.camera
        camera.SetPosition(
            state.position[0], -state.position[1], state.position[2]
        )
        camera.SetFocalPoint(
            state.focal_point[0], -state.focal_point[1], state.focal_point[2]
        )
        camera.SetViewUp(state.view_up[0], -state.view_up[1], state.view_up[2])
        camera.SetParallelProjection(1 if state.projection == 'parallel' else 0)
        if state.projection == 'parallel' and state.parallel_scale is not None:
            camera.SetParallelScale(float(state.parallel_scale))
        if state.projection == 'perspective' and state.view_angle is not None:
            camera.SetViewAngle(float(state.view_angle))
        self._standard_view = state.standard_view or CUSTOM_VIEW
        self.plotter.reset_camera_clipping_range()
        self._render()

    def fit_scene(self) -> None:
        self.dismiss_pick_candidates()
        document = self._document
        if document is not None and document.room is None and not document.entities:
            self.plotter.reset_camera(bounds=DEFAULT_EMPTY_SCENE_BOUNDS)
        else:
            reset_camera_or_floor_default(self.plotter)
        self.plotter.camera.zoom(0.92)
        self._render()

    def focus_entity(self, entity_id: str) -> None:
        self.focus_entities((entity_id,))

    def focus_entities(self, entity_ids) -> None:
        """Frame the selected entities' bounds — zoom-to-selection (F).

        Falls back to a 0.5 m pad for sizeless items (measurement points) so the
        frame is always finite. Keeps the current view direction.
        """

        if self._document is None:
            return
        mins = [float('inf')] * 3
        maxs = [float('-inf')] * 3
        found = False
        for entity_id in entity_ids:
            try:
                entity = self._document.entity(entity_id)
            except KeyError:
                continue
            found = True
            center = domain_to_render(entity.position)
            size = entity.size_m
            half = (
                [0.25, 0.25, 0.25]
                if size is None
                else [
                    max(0.5 * float(size.x_m), 0.25),
                    max(0.5 * float(size.y_m), 0.25),
                    max(0.5 * float(size.z_m), 0.25),
                ]
            )
            for axis in range(3):
                mins[axis] = min(mins[axis], center[axis] - half[axis])
                maxs[axis] = max(maxs[axis], center[axis] + half[axis])
        if not found:
            return
        self.dismiss_pick_candidates()
        bounds = (mins[0], maxs[0], mins[1], maxs[1], mins[2], maxs[2])
        self.plotter.reset_camera(bounds=bounds)
        self._render()

    def capture_camera_view(self) -> tuple:
        """Snapshot the current camera so a transient view can be restored."""

        camera = self.plotter.camera
        return (
            tuple(camera.GetPosition()),
            tuple(camera.GetFocalPoint()),
            tuple(camera.GetViewUp()),
            float(camera.GetViewAngle()),
            bool(camera.GetParallelProjection()),
            float(camera.GetParallelScale()),
        )

    def apply_camera_view(self, view: tuple) -> None:
        """Restore a camera snapshot produced by ``capture_camera_view``."""

        position, focal, view_up, view_angle, parallel, scale = view
        self.dismiss_pick_candidates()
        camera = self.plotter.camera
        camera.SetPosition(*position)
        camera.SetFocalPoint(*focal)
        camera.SetViewUp(*view_up)
        camera.SetParallelProjection(parallel)
        if parallel:
            camera.SetParallelScale(scale)
        else:
            camera.SetViewAngle(view_angle)
        self.plotter.reset_camera_clipping_range()
        self._render()

    def view_from(
        self,
        eye_render: tuple[float, float, float],
        target_render: tuple[float, float, float],
        *,
        view_angle_deg: float | None = None,
    ) -> None:
        """Place the camera at an eye point aimed at a target (#455 view-from-seat).

        Perspective projection with a fixed 45° vertical FOV documents the
        seat-view policy: the preview approximates human field of view, not a
        claimed exact match, and never modifies the scene.
        """

        self.dismiss_pick_candidates()
        camera = self.plotter.camera
        camera.SetParallelProjection(False)
        camera.SetPosition(*eye_render)
        camera.SetFocalPoint(*target_render)
        camera.SetViewUp(0.0, 0.0, 1.0)
        camera.SetViewAngle(float(view_angle_deg) if view_angle_deg else 45.0)
        self.plotter.reset_camera_clipping_range()
        self._render()

    def render_measure_overlay(self, result, *, draft_endpoints: tuple = ()) -> None:
        """Draw the measurement lines/points + label. Purely visual (#491)."""

        endpoints = list(draft_endpoints)
        if result is not None:
            endpoints = [endpoint.position for endpoint in result.endpoints]
        for index, position in enumerate(endpoints):
            self.plotter.add_mesh(
                pv.Sphere(radius=0.045, center=domain_to_render(position)),
                color=DARK_THEME.viewport.selection_outline.hex,
                pickable=False,
                name=f"measure-point-{index}",
                render=False,
            )
        if result is not None:
            if result.mode == 'distance' and len(result.endpoints) == 2:
                a, b = result.endpoints
                self.plotter.add_mesh(
                    pv.Line(domain_to_render(a.position), domain_to_render(b.position)),
                    color=DARK_THEME.viewport.selection_outline.hex,
                    line_width=4,
                    pickable=False,
                    name="measure-line",
                    render=False,
                )
            elif result.mode == 'angle' and len(result.endpoints) == 3:
                a, v, b = result.endpoints
                for label, pair in (("measure-line-a", (a, v)), ("measure-line-b", (v, b))):
                    self.plotter.add_mesh(
                        pv.Line(domain_to_render(pair[0].position), domain_to_render(pair[1].position)),
                        color=DARK_THEME.viewport.selection_outline.hex,
                        line_width=4,
                        pickable=False,
                        name=label,
                        render=False,
                    )
        self._render()

    def render_constraint_overlay(self, constraint_set, evaluation, *, highlight_result=None) -> None:
        """Render region polygons/wall bands/pair connectors for constraints (#486)."""

        from .cad_constraint_authoring import constraint_entity_ids, wall_points
        from .cad_constraint_models import (
            CadAllowedRegionConstraint,
            CadExclusionRegionConstraint,
            CadPairDistanceConstraint,
            CadWallClearanceConstraint,
        )

        document = self._document
        if document is None:
            return
        highlight_id = (
            None if highlight_result is None else highlight_result.constraint_id
        )
        violated_entities: set[str] = set()
        if evaluation is not None:
            for result in evaluation.violations:
                violated_entities.update(result.entity_ids)

        for constraint in constraint_set.constraints:
            is_highlighted = constraint.constraint_id == highlight_id
            if isinstance(constraint, (CadAllowedRegionConstraint, CadExclusionRegionConstraint)):
                if isinstance(constraint, CadAllowedRegionConstraint):
                    color, opacity = 'steelblue', 0.10
                elif constraint.region_role == 'walkway':
                    color, opacity = 'darkorange', 0.18
                else:
                    color, opacity = 'tomato', 0.16
                points = np.asarray(
                    [(item.x_m, -item.y_m, 0.015) for item in constraint.vertices],
                    dtype=float,
                )
                faces = np.asarray([len(points), *range(len(points))], dtype=np.int64)
                mesh = pv.PolyData(points, faces).triangulate()
                self.plotter.add_mesh(
                    mesh,
                    color=color,
                    opacity=opacity,
                    show_edges=True,
                    line_width=5 if is_highlighted else 2,
                    pickable=False,
                    name=f"constraint-region-{constraint.constraint_id}",
                    render=False,
                )
            elif isinstance(constraint, CadWallClearanceConstraint):
                try:
                    start, end = wall_points(document, constraint.wall_id)
                except ValueError:
                    continue
                band_z = 0.04
                self.plotter.add_mesh(
                    pv.Line((start[0], -start[1], band_z), (end[0], -end[1], band_z)),
                    color='mediumpurple',
                    line_width=6 if is_highlighted else 3,
                    pickable=False,
                    name=f"constraint-wall-{constraint.constraint_id}",
                    render=False,
                )
            elif isinstance(constraint, CadPairDistanceConstraint):
                try:
                    a = document.entity(constraint.entity_a)
                    b = document.entity(constraint.entity_b)
                except KeyError:
                    continue
                z = max(a.position.z_m, b.position.z_m, 0.05)
                self.plotter.add_mesh(
                    pv.Line(
                        (a.position.x_m, -a.position.y_m, z),
                        (b.position.x_m, -b.position.y_m, z),
                    ),
                    color='mediumpurple',
                    line_width=5 if is_highlighted else 2,
                    pickable=False,
                    name=f"constraint-pair-{constraint.constraint_id}",
                    render=False,
                )

        # Violating entities get a red marker so a failure is spatially obvious.
        for entity_id in violated_entities:
            try:
                entity = document.entity(entity_id)
            except KeyError:
                continue
            position = entity.position
            z = max(position.z_m, 0.06)
            self.plotter.add_mesh(
                pv.Sphere(radius=0.10, center=(position.x_m, -position.y_m, z)),
                style='wireframe',
                line_width=3,
                color='red',
                pickable=False,
                name=f"constraint-violation-{entity_id}",
                render=False,
            )
        self._render()

    def render_video_overlay(self, evaluation) -> None:
        """Projector cone + sightline + collision overlays from one evaluation (#455).

        Everything drawn comes from the returned ``VideoGeometryEvaluation``
        (or ``DirectViewGeometryEvaluation``, whose image surface replaces the
        projector cone — #1054); no geometry is invented here.
        """

        if evaluation is None:
            return
        document = self._document
        # Staleness guard (same contract as render_prediction_results): an
        # evaluation computed against a previous scene revision must not draw
        # over edited geometry — sightlines/cones would be misinformation.
        baseline_hash = getattr(
            getattr(evaluation, 'target', None), 'scene_content_hash', None
        )
        if (
            document is not None
            and baseline_hash is not None
            and baseline_hash != scene_content_hash(document)
        ):
            return
        projection = getattr(evaluation, 'projection', None)
        surface = getattr(evaluation, 'surface', None)
        if projection is not None:
            lens = projection.lens_position
            lens_render = domain_to_render(lens)
            corners = [domain_to_render(corner) for corner in projection.image_plane_corners]
            cone_color = 'gold' if projection.status != 'FAIL' else 'red'
            # Cone edges: lens → each image corner.
            for index, corner in enumerate(corners):
                self.plotter.add_mesh(
                    pv.Line(lens_render, corner),
                    color=cone_color,
                    line_width=3,
                    opacity=0.85,
                    pickable=False,
                    name=f"video-cone-{index}",
                    render=False,
                )
            # Image aperture rectangle.
            ring = np.asarray(corners + [corners[0]], dtype=float)
            self.plotter.add_mesh(
                pv.lines_from_points(ring),
                color=cone_color,
                line_width=3,
                pickable=False,
                name="video-aperture",
                render=False,
            )
            if projection.optical_axis_intersection is not None:
                self.plotter.add_mesh(
                    pv.Line(
                        lens_render,
                        domain_to_render(projection.optical_axis_intersection),
                    ),
                    color='cyan',
                    line_width=2,
                    pickable=False,
                    name="video-optical-axis",
                    render=False,
                )
            self.plotter.add_mesh(
                pv.Sphere(radius=0.05, center=lens_render),
                color=cone_color,
                pickable=False,
                name="video-lens",
                render=False,
            )

        if surface is not None:
            # Direct view: the active image aperture ring + centre marker take
            # the place of the projector cone.
            surface_color = 'gold' if surface.status != 'FAIL' else 'red'
            corners = [
                domain_to_render(corner) for corner in surface.image_plane_corners
            ]
            ring = np.asarray(corners + [corners[0]], dtype=float)
            self.plotter.add_mesh(
                pv.lines_from_points(ring),
                color=surface_color,
                line_width=3,
                pickable=False,
                name="video-aperture",
                render=False,
            )
            self.plotter.add_mesh(
                pv.Sphere(
                    radius=0.05,
                    center=domain_to_render(surface.image_center),
                ),
                color=surface_color,
                pickable=False,
                name="video-display-center",
                render=False,
            )

        image_center = None
        if projection is not None:
            image_center = projection.screen_image_center
        elif surface is not None:
            image_center = surface.image_center

        if document is not None and image_center is not None:
            # The evaluator's eye positions are the authority: they carry the
            # binding's local offset rotated into the world frame, and are None
            # for bindings without eye authority (legacy). Recomputing the eye
            # here from the raw local offset would ignore seat orientation and
            # would fabricate an eye point the evaluator never produced.
            viewing_eyes = {
                item.seat_entity_id: item.eye_position
                for item in getattr(evaluation, 'viewing', ())
            }
            for seat_result in evaluation.sightlines:
                binding = next(
                    (
                        item
                        for item in evaluation.request.seats
                        if item.entity_id == seat_result.seat_entity_id
                    ),
                    None,
                )
                if binding is None:
                    continue
                try:
                    document.entity(seat_result.seat_entity_id)
                except KeyError:
                    continue
                eye_position = viewing_eyes.get(seat_result.seat_entity_id)
                if eye_position is None:
                    continue
                eye_render = domain_to_render(eye_position)
                blocked = bool(seat_result.blocked_sample_ids)
                color = 'red' if blocked or seat_result.status == 'FAIL' else (
                    'goldenrod' if seat_result.status == 'UNKNOWN' else 'seagreen'
                )
                self.plotter.add_mesh(
                    pv.Line(eye_render, domain_to_render(image_center)),
                    color=color,
                    line_width=3 if seat_result.status == 'FAIL' else 2,
                    opacity=0.9,
                    pickable=False,
                    name=f"video-sightline-{seat_result.seat_entity_id}",
                    render=False,
                )
            for collision in evaluation.collisions:
                if not collision.intersects_or_violates_clearance:
                    continue
                try:
                    a = document.entity(collision.entity_a)
                    b = document.entity(collision.entity_b)
                except KeyError:
                    continue
                z = max(a.position.z_m, b.position.z_m, 0.05)
                self.plotter.add_mesh(
                    pv.Line(
                        (a.position.x_m, -a.position.y_m, z),
                        (b.position.x_m, -b.position.y_m, z),
                    ),
                    color='red',
                    line_width=4,
                    pickable=False,
                    name=f"video-collision-{collision.entity_a}-{collision.entity_b}",
                    render=False,
                )
        self._render()

    def render_history_ghost(self, document: SceneDocument, *, label: str | None = None) -> None:
        """Ghost a historical revision over the current scene — read-only (#485)."""

        for entity in document.entities:
            ghost_mesh, _glyphs, _env = entity_render_meshes(entity)
            self.plotter.add_mesh(
                ghost_mesh,
                color='slategray',
                style='wireframe',
                line_width=2,
                opacity=0.55,
                pickable=False,
                name=f"history-ghost-{entity.entity_id}",
                render=False,
            )
        if label:
            self.plotter.add_text(
                label,
                name="history-ghost-label",
                position="upper_right",
                font_size=9,
                color=DARK_THEME.text.secondary.hex,
                render=False,
            )
        self._render()

    def render_search_domain(
        self,
        entity_id: str,
        axes: tuple,
        *,
        draft_axis=None,
        hard_constraints_satisfied: bool | None = None,
    ) -> None:
        """Search-domain overlay: authored axis ranges in world space (#530).

        ``axes`` are ``CadSearchAxis``-like objects (entity_id, axis, minimum_m,
        maximum_m, step_m); ``draft_axis`` (dict-like with axis/min/max) is drawn
        brighter so the in-progress authoring row is visible before saving.
        """

        document = self._document
        if document is None:
            return
        self._search_domain_handles = {}
        axis_colors = {'x': 'tomato', 'y': 'seagreen', 'z': 'cornflowerblue'}
        seen: set[tuple[str, str]] = set()

        def _render_axis(entity, axis: str, min_m: float, max_m: float, name: str, *, bright: bool) -> None:
            center = domain_to_render(entity.position)
            low = list(center)
            high = list(center)
            idx = {'x': 0, 'y': 1, 'z': 2}[axis]
            low[idx] += float(min_m) * (-1.0 if axis == 'y' else 1.0)
            high[idx] += float(max_m) * (-1.0 if axis == 'y' else 1.0)
            self.plotter.add_mesh(
                pv.Line(tuple(low), tuple(high)),
                color=axis_colors[axis],
                line_width=8 if bright else 5,
                opacity=0.95 if bright else 0.7,
                pickable=False,
                name=name,
                render=False,
            )
            for tag, point in (("min", low), ("max", high)):
                actor = self.plotter.add_mesh(
                    pv.Sphere(radius=0.06 if bright else 0.045, center=point),
                    color=axis_colors[axis],
                    pickable=True,
                    name=f"{name}-{tag}",
                    render=False,
                )
                self._search_domain_handles[id(actor)] = (
                    entity.entity_id,
                    axis,
                    tag,
                    bright,
                )

        for item in axes:
            try:
                entity = document.entity(item.entity_id)
            except KeyError:
                continue
            key = (item.entity_id, item.axis)
            if key in seen:
                continue
            seen.add(key)
            _render_axis(
                entity,
                item.axis,
                item.min_m,
                item.max_m,
                f"search-domain-{item.entity_id}-{item.axis}",
                bright=False,
            )
        if draft_axis is not None:
            try:
                entity = document.entity(draft_axis['entity_id'])
            except KeyError:
                return
            _render_axis(
                entity,
                draft_axis['axis'],
                draft_axis['min_m'],
                draft_axis['max_m'],
                f"search-domain-draft-{draft_axis['entity_id']}-{draft_axis['axis']}",
                bright=True,
            )
        if hard_constraints_satisfied is False:
            self.plotter.add_text(
                "警告: 検索領域がハード制約に違反する可能性があります",
                name="search-domain-warning",
                position="upper_left",
                font_size=9,
                color='tomato',
                render=False,
            )
        self._render()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._hide_snap_hud()
        self.plotter.close()
        super().closeEvent(event)


__all__ = ["RoomOverlayState", "RoomViewport3D", "SelectionDirectionRay"]
