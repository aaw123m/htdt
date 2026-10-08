from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from math import hypot, isfinite, radians, tan
from typing import TYPE_CHECKING, Iterator

import numpy as np
import pyvista as pv
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtWidgets import QFrame, QRubberBand, QVBoxLayout, QWidget
from pyvistaqt import QtInteractor
from shapely.geometry import Polygon
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
from .ui_theme import DARK_THEME, SurfaceRole, set_surface_role

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


class RoomViewport3D(QFrame):
    """Dark, scene-authority-neutral viewport for the UX120 Room workspace.

    Camera navigation and keyboard shortcut policy are intentionally not owned here.
    Agent B can attach the CAD input controller to the public interactor attribute.
    """

    entitySelected = Signal(object)
    proposedEntitySelected = Signal(object)
    contextMenuRequested = Signal(object, object)
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
            underlay_id = self._actor_underlay_ids.get(id(actor))
            if underlay_id is not None:
                picked = getattr(self.plotter, "picked_position", None)
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
                self.entityPicked.emit(entity_id, self._last_display_position())
                self.entitySelected.emit(entity_id)
                return
            proposed_id = self._actor_proposed_entity_ids.get(id(actor))
            if proposed_id is not None:
                self.proposedEntitySelected.emit(proposed_id)
        finally:
            self._in_pick_dispatch = False

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QMouseEvent

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

    def render_snap_feedback(self, label: str | None, *, screen_position: tuple[float, float] | None = None) -> None:
        """Floating snap-target indicator during a drag (#481)."""

        try:
            self.plotter.remove_actor("snap-feedback-label", render=False)
        except Exception:
            pass
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
        camera = self.plotter.camera
        factor = 1.15 ** float(steps)
        if camera.GetParallelProjection():
            camera.SetParallelScale(float(camera.GetParallelScale()) / factor)
        else:
            camera.Zoom(factor)
        self.plotter.reset_camera_clipping_range()
        self._render()

    def open_context_menu(
        self,
        position: QPointF,
        global_position: QPointF,
    ) -> None:
        # Command content remains outside the renderer and can be supplied by the
        # Room workspace / central command registry.
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
        self.plotter.close()
        super().closeEvent(event)


__all__ = ["RoomOverlayState", "RoomViewport3D", "SelectionDirectionRay"]
