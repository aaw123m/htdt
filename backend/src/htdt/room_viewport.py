from __future__ import annotations

from dataclasses import dataclass
from math import radians, tan

import numpy as np
import pyvista as pv
from PySide6.QtCore import QPointF, Signal
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget
from pyvistaqt import QtInteractor

from .cad_prediction_models import CadPredictionResult
from .prediction_interpretation import PredictionSpatialLink
from .cad_scene import (
    SceneDocument,
    SceneEntity,
    acoustic_reference_position,
    domain_pose_to_render_matrix,
    domain_to_render,
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


def _entity_mesh(entity: SceneEntity) -> pv.PolyData:
    if entity.size_m is None:
        mesh = pv.Sphere(radius=0.08)
    else:
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


class RoomViewport3D(QFrame):
    """Dark, scene-authority-neutral viewport for the UX120 Room workspace.

    Camera navigation and keyboard shortcut policy are intentionally not owned here.
    Agent B can attach the CAD input controller to the public interactor attribute.
    """

    entitySelected = Signal(object)
    proposedEntitySelected = Signal(object)
    contextMenuRequested = Signal(object, object)

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
        self._document: SceneDocument | None = None
        self._selected_id: str | None = None
        self._overlays = RoomOverlayState()
        self.plotter.set_background(DARK_THEME.viewport.background.hex)
        self.plotter.enable_anti_aliasing("fxaa")
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
        overlays: RoomOverlayState,
        reset_camera: bool = False,
    ) -> None:
        self._document = document
        self._selected_id = selected_id
        self._overlays = overlays
        self._actor_entity_ids.clear()
        self._actor_proposed_entity_ids.clear()
        self.plotter.clear()
        self.plotter.set_background(DARK_THEME.viewport.background.hex)

        floor = _room_floor_mesh(document)
        if floor is not None:
            self.plotter.add_mesh(
                floor,
                color=DARK_THEME.viewport.floor.hex,
                opacity=0.72,
                lighting=False,
                pickable=False,
                name="room-floor",
            )

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

        for entity in document.entities:
            focused_out = bool(
                overlays.focus_selection
                and selected_id
                and entity.entity_id != selected_id
            )
            actor = self.plotter.add_mesh(
                _entity_mesh(entity),
                color=DARK_THEME.viewport.geometry.hex,
                show_edges=True,
                edge_color=(
                    DARK_THEME.viewport.selection_outline.hex
                    if entity.entity_id == selected_id
                    else DARK_THEME.viewport.geometry_edge.hex
                ),
                line_width=3 if entity.entity_id == selected_id else 1,
                opacity=0.12 if focused_out else 0.90,
                ambient=0.32,
                diffuse=0.62,
                specular=0.10,
                specular_power=12.0,
                pickable=True,
                name=f"entity-{entity.entity_id}",
            )
            self._actor_entity_ids[id(actor)] = entity.entity_id

        if overlays.acoustics:
            self._render_acoustic_overlay(document)
        if overlays.labels:
            self._render_labels(document, selected_id)

        self.plotter.add_axes(
            color=DARK_THEME.text.muted.hex,
            line_width=1,
            labels_off=True,
        )
        if reset_camera:
            self.fit_scene()
        self.plotter.render()

    def render_proposed_entities(
        self,
        entities: tuple[SceneEntity, ...],
        *,
        selected_id: str | None = None,
    ) -> None:
        """Overlay proposal ghosts without changing current SceneDocument truth."""
        self._actor_proposed_entity_ids.clear()
        if not entities:
            return
        for entity in entities:
            actor = self.plotter.add_mesh(
                _entity_mesh(entity),
                color=DARK_THEME.viewport.geometry_edge.hex,
                style="wireframe",
                line_width=4 if entity.entity_id == selected_id else 2,
                opacity=0.62 if entity.entity_id == selected_id else 0.34,
                pickable=True,
                name=f"proposal-ghost-{entity.entity_id}",
                render=False,
            )
            self._actor_proposed_entity_ids[id(actor)] = entity.entity_id
        self.plotter.add_text(
            "提案 ghost · 未設置 / current Sceneは変更しません",
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

    def _picked_actor(self, actor) -> None:
        entity_id = self._actor_entity_ids.get(id(actor))
        if entity_id is not None:
            self.entitySelected.emit(entity_id)
            return
        proposed_id = self._actor_proposed_entity_ids.get(id(actor))
        if proposed_id is not None:
            self.proposedEntitySelected.emit(proposed_id)

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

    def closeEvent(self, event) -> None:  # noqa: N802
        self.plotter.close()
        super().closeEvent(event)


__all__ = ["RoomOverlayState", "RoomViewport3D"]
