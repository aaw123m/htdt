from __future__ import annotations

from dataclasses import dataclass
from math import radians, tan

import numpy as np
import pyvista as pv
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget
from pyvistaqt import QtInteractor

from .cad_prediction_models import CadPredictionResult
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
    points = np.asarray(
        [(vertex.x_m, -vertex.y_m, 0.0) for vertex in vertices]
        + [(vertex.x_m, -vertex.y_m, room.height_m) for vertex in vertices],
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

        self.plotter = QtInteractor(self)
        self.interactor = self.plotter.interactor
        layout.addWidget(self.interactor)

        self._actor_entity_ids: dict[int, str] = {}
        self._actor_proposed_entity_ids: dict[int, str] = {}
        self._actor_underlay_ids: dict[int, str] = {}
        self._underlay_items: tuple[UnderlayRenderItem, ...] = ()
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
        self._search_domain_handles: dict[int, tuple[str, str, str, bool]] = {}
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
        self._actor_entity_ids.clear()
        self._actor_proposed_entity_ids.clear()
        self._actor_underlay_ids.clear()
        self._search_domain_handles.clear()
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
            mesh = self._apply_section(_entity_mesh(entity))
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
            )
            self._actor_entity_ids[id(actor)] = entity.entity_id
            # Semantic glyph proxies read as the entity's type at a glance;
            # they are render-only, pick back to the entity, and dim with it.
            glyph_opacity = (0.55 if is_locked else 0.98) if not focused_out else 0.12
            for index, glyph in enumerate(semantic_entity_meshes(entity)):
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
                )
                self._actor_entity_ids[id(glyph_actor)] = entity.entity_id
            if (
                entity.size_m is not None
                and entity.body_geometry is not None
                and entity.body_geometry.kind != 'box'
            ):
                # The bounding envelope stays visible as a separate wireframe
                # authority whenever an entity opts into richer body geometry.
                self.plotter.add_mesh(
                    _entity_envelope_mesh(entity),
                    color=DARK_THEME.viewport.geometry_edge.hex,
                    style="wireframe",
                    line_width=1,
                    opacity=0.45,
                    pickable=False,
                    name=f"envelope-{entity.entity_id}",
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
        self.plotter.render()

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
        self.plotter.render()

    def render_proposed_entities(
        self,
        entities: tuple[SceneEntity, ...],
        *,
        selected_id: str | None = None,
        label: str = "提案 ghost · 未設置 / current Sceneは変更しません",
    ) -> None:
        """Overlay proposal ghosts without changing current SceneDocument truth."""
        self._actor_proposed_entity_ids.clear()
        if not entities:
            return
        for entity in entities:
            # Ghosts stay wireframe — the proposed/current grammar is unchanged;
            # only the fill color now carries the entity category (#572).
            ghost_color = _category_color(_entity_category(entity))
            actor = self.plotter.add_mesh(
                _entity_mesh(entity),
                color=ghost_color,
                style="wireframe",
                line_width=4 if entity.entity_id == selected_id else 2,
                opacity=0.62 if entity.entity_id == selected_id else 0.34,
                pickable=True,
                name=f"proposal-ghost-{entity.entity_id}",
                render=False,
            )
            self._actor_proposed_entity_ids[id(actor)] = entity.entity_id
            for index, glyph in enumerate(semantic_entity_meshes(entity)):
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
        self.plotter.render()

    def _render_acoustic_overlay(self, document: SceneDocument) -> None:
        for entity in document.entities:
            reference = acoustic_reference_position(entity)
            if reference is not None:
                self.plotter.add_mesh(
                    pv.Sphere(radius=0.035, center=domain_to_render(reference)),
                    color=DARK_THEME.accent.primary.hex,
                    opacity=0.92,
                    pickable=False,
                    name=f"reference-{entity.entity_id}",
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
        self.plotter.render()

    def _render_labels(self, document: SceneDocument, selected_id: str | None) -> None:
        visible = [
            entity
            for entity in document.entities
            if (
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

    def _render_underlays(self) -> None:
        for item in self._underlay_items:
            if item.quad_domain is not None and item.image is not None:
                points = np.asarray(
                    [(x, -y, z) for x, y, z in item.quad_domain],
                    dtype=float,
                )
                quad = pv.PolyData(points, [4, 0, 1, 2, 3])
                quad.active_t_coords = np.asarray(
                    [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
                    dtype=float,
                )
                actor = self.plotter.add_mesh(
                    quad,
                    texture=pv.Texture(item.image),
                    opacity=item.opacity,
                    lighting=False,
                    pickable=True,
                    show_scalar_bar=False,
                    name=f"underlay-{item.underlay_id}",
                )
                self._actor_underlay_ids[id(actor)] = item.underlay_id
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
                mesh = pv.PolyData(points)
                mesh.lines = lines
                actor = self.plotter.add_mesh(
                    mesh,
                    color=DARK_THEME.scientific.primary_trace.hex,
                    line_width=1,
                    opacity=min(1.0, item.opacity + 0.2),
                    pickable=True,
                    name=f"underlay-lines-{item.underlay_id}",
                )
                self._actor_underlay_ids[id(actor)] = item.underlay_id

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
            )

    def _picked_actor(self, actor) -> None:
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
            self.entityPicked.emit(entity_id, self._last_display_position())
            self.entitySelected.emit(entity_id)
            return
        proposed_id = self._actor_proposed_entity_ids.get(id(actor))
        if proposed_id is not None:
            self.proposedEntitySelected.emit(proposed_id)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QMouseEvent

        if obj is self.interactor and isinstance(event, QMouseEvent):
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._press_position = QPointF(event.position())
            elif event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                pressed = self._press_position
                self._press_position = None
                moved = pressed is None or (
                    (event.position() - pressed).manhattanLength() < 6.0
                )
                if moved and self.pick_actor_at(event.position()) is None:
                    self.emptyClicked.emit(event.position())
        return False

    def _last_display_position(self) -> QPointF | None:
        try:
            pos = self.interactor.GetEventPosition()
        except Exception:
            return None
        return QPointF(float(pos[0]), float(pos[1]))

    def pick_actor_at(self, position: QPointF):
        """Run the shared picker at a display position; return the actor or None."""

        try:
            picker = self.plotter.iren.picker
            renderer = self.plotter.iren.get_poked_renderer()
            picker.Pick(float(position.x()), float(position.y()), 0.0, renderer)
            return picker.GetActor()
        except Exception:
            return None

    def pick_world_position(self, position: QPointF) -> tuple[float, float, float] | None:
        """Render-space world position under a display point (or None)."""

        try:
            picker = self.plotter.iren.picker
            renderer = self.plotter.iren.get_poked_renderer()
            picker.Pick(float(position.x()), float(position.y()), 0.0, renderer)
            picked = picker.GetPickPosition()
        except Exception:
            return None
        if picked is None:
            return None
        return (float(picked[0]), float(picked[1]), float(picked[2]))

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
        self.plotter.render()

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
        self.plotter.render()

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
        self.plotter.render()

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
        self.plotter.render()

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
            self.plotter.render()
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
        self.plotter.render()

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
        self.plotter.render()

    def fit_scene(self) -> None:
        self.plotter.reset_camera()
        self.plotter.camera.zoom(0.92)
        self.plotter.render()

    def focus_entity(self, entity_id: str) -> None:
        if self._document is None:
            return
        try:
            entity = self._document.entity(entity_id)
        except KeyError:
            return
        self.plotter.camera.focal_point = domain_to_render(entity.position)
        self.plotter.render()

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
        self.plotter.render()

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
        self.plotter.render()

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
                    )

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
            )

    def render_video_overlay(self, evaluation) -> None:
        """Projector cone + sightline + collision overlays from one evaluation (#455).

        Everything drawn comes from the returned ``VideoGeometryEvaluation``
        (or ``DirectViewGeometryEvaluation``, whose image surface replaces the
        projector cone — #1054); no geometry is invented here.
        """

        if evaluation is None:
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
                )
            # Image aperture rectangle.
            ring = np.asarray(corners + [corners[0]], dtype=float)
            self.plotter.add_mesh(
                pv.lines_from_points(ring),
                color=cone_color,
                line_width=3,
                pickable=False,
                name="video-aperture",
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
                )
            self.plotter.add_mesh(
                pv.Sphere(radius=0.05, center=lens_render),
                color=cone_color,
                pickable=False,
                name="video-lens",
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
            )
            self.plotter.add_mesh(
                pv.Sphere(
                    radius=0.05,
                    center=domain_to_render(surface.image_center),
                ),
                color=surface_color,
                pickable=False,
                name="video-display-center",
            )

        image_center = None
        if projection is not None:
            image_center = projection.screen_image_center
        elif surface is not None:
            image_center = surface.image_center

        document = self._document
        if document is not None and image_center is not None:
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
                    seat = document.entity(seat_result.seat_entity_id)
                except KeyError:
                    continue
                eye = seat.position
                eye_render = domain_to_render(
                    type(eye)(
                        x_m=eye.x_m + binding.eye_reference_offset_local_m.x_m,
                        y_m=eye.y_m + binding.eye_reference_offset_local_m.y_m,
                        z_m=eye.z_m + binding.eye_reference_offset_local_m.z_m,
                    )
                )
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
                )

    def render_history_ghost(self, document: SceneDocument, *, label: str | None = None) -> None:
        """Ghost a historical revision over the current scene — read-only (#485)."""

        for entity in document.entities:
            self.plotter.add_mesh(
                _entity_mesh(entity),
                color='slategray',
                style='wireframe',
                line_width=2,
                opacity=0.55,
                pickable=False,
                name=f"history-ghost-{entity.entity_id}",
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
        self.plotter.render()

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
            )
            for tag, point in (("min", low), ("max", high)):
                actor = self.plotter.add_mesh(
                    pv.Sphere(radius=0.06 if bright else 0.045, center=point),
                    color=axis_colors[axis],
                    pickable=True,
                    name=f"{name}-{tag}",
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

    def closeEvent(self, event) -> None:  # noqa: N802
        self.plotter.close()
        super().closeEvent(event)


__all__ = ["RoomOverlayState", "RoomViewport3D", "SelectionDirectionRay"]
