from __future__ import annotations

import argparse
from math import hypot
import os
from pathlib import Path
import sys
from typing import Literal
from uuid import uuid4

import numpy as np
import pyvista as pv
from PySide6.QtCore import QEvent, QSignalBlocker, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QToolBar,
    QTreeWidgetItem,
    QWidget,
)

from .cad_document import EditorViewState
from .cad_repository import SceneRepository
from .cad_room import RoomWorkingDocument
from .cad_scene import (
    F1_DOCUMENT_ID,
    Position3,
    RoomPrism,
    RoomVertex,
    make_empty_scene,
    make_f1_scene,
    make_polygon_room,
    room_vertices,
)
from .native_editor import NativeEditorWindow, ROLE, default_data_dir


RoomMode = Literal['idle', 'sketch', 'edit']


def _room_wireframe(room: RoomPrism) -> pv.PolyData:
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


def _polyline(vertices: tuple[RoomVertex, ...], *, close: bool) -> pv.PolyData | None:
    if len(vertices) < 2:
        return None
    points = [(vertex.x_m, -vertex.y_m, 0.0) for vertex in vertices]
    if close:
        points.append(points[0])
    return pv.lines_from_points(np.asarray(points, dtype=float), close=False)


class RoomEditorWindow(NativeEditorWindow):
    """N30a room-sketch layer over the accepted N20b native editor shell."""

    def __init__(self, repository: SceneRepository, document_id: str = F1_DOCUMENT_ID) -> None:
        # NativeEditorWindow calls virtual _load_or_seed/_rebuild during construction,
        # so room-tool state must exist before super().__init__().
        self.room_mode: RoomMode = 'idle'
        self.room_sketch_vertices: list[RoomVertex] = []
        self.room_cursor_xy: tuple[float, float] | None = None
        self.room_drag_vertex_id: str | None = None
        self.room_drag_before: tuple[RoomVertex, ...] = ()
        self.room_drag_preview: tuple[RoomVertex, ...] = ()
        self.selected_room_vertex_id: str | None = None
        self.selected_room_edge_index: int | None = None
        super().__init__(repository, document_id)
        self.setWindowTitle('Home Theater Digital Twin — N30a Room Editor')

        room_toolbar = QToolBar('Room', self)
        self.addToolBar(room_toolbar)
        self.draw_room_action = QAction('Draw Room', self)
        self.draw_room_action.triggered.connect(self.start_room_sketch)
        self.edit_room_action = QAction('Edit Room', self)
        self.edit_room_action.triggered.connect(self.start_room_edit)
        self.close_room_action = QAction('Close Room', self)
        self.close_room_action.triggered.connect(self.close_room_sketch)
        self.done_room_action = QAction('Done Room', self)
        self.done_room_action.triggered.connect(self.finish_room_edit)
        self.insert_vertex_action = QAction('Insert Vertex', self)
        self.insert_vertex_action.setCheckable(True)
        self.insert_vertex_action.toggled.connect(self._insert_vertex_toggled)
        self.delete_vertex_action = QAction('Delete Vertex', self)
        self.delete_vertex_action.triggered.connect(self.delete_room_vertex)
        room_toolbar.addActions(
            (
                self.draw_room_action,
                self.edit_room_action,
                self.close_room_action,
                self.done_room_action,
                self.insert_vertex_action,
                self.delete_vertex_action,
            )
        )

        room_inspector = QWidget()
        room_form = QFormLayout(room_inspector)
        self.room_tool_label = QLabel('Objects')
        self.room_bounds_label = QLabel('—')
        room_form.addRow('Room tool', self.room_tool_label)
        room_form.addRow('Bounds', self.room_bounds_label)

        self.room_vertex_x = QDoubleSpinBox()
        self.room_vertex_x.setRange(-1000.0, 1000.0)
        self.room_vertex_x.setDecimals(4)
        self.room_vertex_x.setSingleStep(0.01)
        self.room_vertex_x.setSuffix(' m')
        self.room_vertex_x.setKeyboardTracking(False)
        self.room_vertex_x.editingFinished.connect(self._numeric_room_vertex_edited)
        room_form.addRow('Vertex X', self.room_vertex_x)

        self.room_vertex_y = QDoubleSpinBox()
        self.room_vertex_y.setRange(-1000.0, 1000.0)
        self.room_vertex_y.setDecimals(4)
        self.room_vertex_y.setSingleStep(0.01)
        self.room_vertex_y.setSuffix(' m')
        self.room_vertex_y.setKeyboardTracking(False)
        self.room_vertex_y.editingFinished.connect(self._numeric_room_vertex_edited)
        room_form.addRow('Vertex Y', self.room_vertex_y)

        self.room_edge_length = QDoubleSpinBox()
        self.room_edge_length.setRange(0.001, 1000.0)
        self.room_edge_length.setDecimals(4)
        self.room_edge_length.setSingleStep(0.01)
        self.room_edge_length.setSuffix(' m')
        self.room_edge_length.setKeyboardTracking(False)
        self.room_edge_length.editingFinished.connect(self._numeric_room_edge_edited)
        room_form.addRow('Edge length', self.room_edge_length)

        self.room_height = QDoubleSpinBox()
        self.room_height.setRange(0.1, 20.0)
        self.room_height.setDecimals(3)
        self.room_height.setSingleStep(0.05)
        self.room_height.setSuffix(' m')
        self.room_height.setKeyboardTracking(False)
        self.room_height.editingFinished.connect(self._numeric_room_height_edited)
        room_form.addRow('Ceiling height', self.room_height)

        room_dock = QDockWidget('Room', self)
        room_dock.setWidget(room_inspector)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, room_dock)
        self._refresh_room_inspector()
        self._update_actions()

    def _load_or_seed(self) -> None:
        revision = self.repository.current_head(self.document_id)
        if revision is None:
            seed = make_f1_scene() if self.document_id == F1_DOCUMENT_ID else make_empty_scene(self.document_id)
            revision = self.repository.save(seed, parent_revision_id=None).revision
        self.working = RoomWorkingDocument(
            revision.document,
            source_revision_id=revision.revision_id,
            saved_content_hash=revision.content_hash,
        )
        record = self.repository.view_state(self.document_id)
        if record is not None:
            self.view_state = EditorViewState(
                selected_id=record.selected_id,
                selected_ids=list(record.selected_ids),
                hidden_ids=set(record.hidden_ids),
                locked_ids=set(record.locked_ids),
            )
            self.view_state.sanitize(revision.document)
        self._sync_transform_controls()
        self.selected_id = self.view_state.selected_id
        self.recovery_candidate = self.repository.recovery(self.document_id)
        self._rebuild(reset_camera=True)
        if self.recovery_candidate is not None:
            self.statusBar().showMessage(
                f'revision {revision.revision_id[:8]} · recovery available · choose Recover Draft or Discard Recovery'
            )
        else:
            room_state = 'room ready' if revision.document.room is not None else 'empty scene · Draw Room to begin'
            self.statusBar().showMessage(f'revision {revision.revision_id[:8]} · {room_state} · clean')

    def recover_draft(self) -> None:
        if self.recovery_candidate is None:
            return
        source_id = self.recovery_candidate.source_revision_id
        source = self.repository.get(source_id) if source_id is not None else None
        if source is None:
            self.statusBar().showMessage('Recovery cannot be opened because its source revision is missing')
            return
        self.working = RoomWorkingDocument(
            self.recovery_candidate.document,
            source_revision_id=source.revision_id,
            saved_content_hash=source.content_hash,
        )
        self.view_state.sanitize(self.working.committed_document)
        self.selected_id = self.view_state.selected_id
        self.recovery_candidate = None
        self._rebuild(reset_camera=True)
        self.statusBar().showMessage(f'Recovered draft from revision {source.revision_id[:8]} · dirty')

    def _rebuild(self, *, reset_camera: bool = False) -> None:
        if self.working is None:
            return
        selected_ids = self.view_state.selection
        primary_id = self.view_state.selected_id
        self.gizmo_rebuild_timer.stop()
        self.preview_inspect_timer.stop()
        self._reset_drag_snap_state()
        self._invalidate_scene_pick_cache()
        self._remove_gizmo()
        self.viewport.clear()
        self.viewport.add_axes()
        self.viewport.show_grid()
        self.tree.clear()
        self.actors.clear()
        self.actor_ids.clear()
        self.scene_picker.InitializePickList()
        self.items.clear()
        document = self.working.committed_document

        if document.room is None:
            room_item = QTreeWidgetItem(['Room · not created'])
        else:
            vertices = room_vertices(document.room)
            room_item = QTreeWidgetItem([
                f'Room · {len(vertices)} vertices · {document.room.height_m:.3g} m high'
            ])
            room_actor = self.viewport.add_mesh(
                _room_wireframe(document.room),
                line_width=2,
                pickable=False,
                name='room-prism',
            )
            room_actor.prop.opacity = 0.55
        self.tree.addTopLevelItem(room_item)

        groups: dict[str, QTreeWidgetItem] = {}
        for key, label in (
            ('speaker', 'Speakers'),
            ('measurement_point', 'Listening / Measurement'),
            ('furniture', 'Furniture'),
        ):
            groups[key] = QTreeWidgetItem([label])
            self.tree.addTopLevelItem(groups[key])
        for entity in document.entities:
            self._add_entity(groups[entity.kind], entity)
        self.tree.expandAll()
        if reset_camera:
            self._perspective()
        valid_selected = tuple(entity_id for entity_id in selected_ids if entity_id in self.items)
        self._set_selection(valid_selected, primary_id=primary_id, cancel_preview=False, persist=False)
        if self.room_mode == 'edit':
            self._render_room_edit_handles()
        elif self.room_mode == 'sketch':
            self._render_room_sketch_overlay()
        self._refresh_room_inspector()
        self._update_actions()

    def _create_gizmo(self, entity_id: str | None) -> None:
        if self.room_mode != 'idle':
            return
        super()._create_gizmo(entity_id)

    def _current_room(self) -> RoomPrism | None:
        if self.working is None:
            return None
        return self.working.committed_document.room

    def _room_vertices(self) -> tuple[RoomVertex, ...]:
        room = self._current_room()
        if room is None:
            return ()
        return room_vertices(room)

    def _project_room_to_qt(self, vertex: RoomVertex) -> tuple[float, float]:
        renderer = self.viewport.renderer
        renderer.SetWorldPoint(float(vertex.x_m), -float(vertex.y_m), 0.0, 1.0)
        renderer.WorldToDisplay()
        display_x, display_y, _ = renderer.GetDisplayPoint()
        dpr = max(float(self.viewport.interactor.devicePixelRatioF()), 1e-9)
        _, render_height = self.viewport.render_window.GetSize()
        return (float(display_x) / dpr, (float(render_height) - float(display_y)) / dpr)

    def _screen_to_floor(self, qt_x: float, qt_y: float) -> tuple[float, float] | None:
        renderer = self.viewport.renderer
        dpr = max(float(self.viewport.interactor.devicePixelRatioF()), 1.0)
        _, render_height = self.viewport.render_window.GetSize()
        display_x = float(qt_x) * dpr
        display_y = float(render_height) - float(qt_y) * dpr

        def world(depth: float) -> np.ndarray | None:
            renderer.SetDisplayPoint(display_x, display_y, depth)
            renderer.DisplayToWorld()
            x, y, z, w = renderer.GetWorldPoint()
            if abs(float(w)) <= 1e-12:
                return None
            return np.asarray((x / w, y / w, z / w), dtype=float)

        near = world(0.0)
        far = world(1.0)
        if near is None or far is None:
            return None
        ray = far - near
        if abs(float(ray[2])) <= 1e-12:
            return None
        fraction = -float(near[2]) / float(ray[2])
        point = near + ray * fraction
        x_m = float(point[0])
        y_m = -float(point[1])
        if self.view_state.grid_snap_enabled:
            step = self.view_state.grid_step_m
            x_m = round(x_m / step) * step
            y_m = round(y_m / step) * step
        return (x_m, y_m)

    def _remove_room_tool_overlays(self) -> None:
        for name in (
            'room-sketch-line',
            'room-sketch-points',
            'room-sketch-cursor',
            'room-drag-line',
            'room-vertex-handles',
            'room-midpoint-handles',
        ):
            self.viewport.remove_actor(name, reset_camera=False, render=False)

    def _render_room_sketch_overlay(self) -> None:
        self._remove_room_tool_overlays()
        vertices = tuple(self.room_sketch_vertices)
        line = _polyline(vertices, close=False)
        if line is not None:
            self.viewport.add_mesh(line, line_width=3, pickable=False, name='room-sketch-line', render=False)
        if vertices:
            points = np.asarray([(v.x_m, -v.y_m, 0.0) for v in vertices], dtype=float)
            self.viewport.add_mesh(
                pv.PolyData(points),
                render_points_as_spheres=True,
                point_size=12,
                pickable=False,
                name='room-sketch-points',
                render=False,
            )
        if vertices and self.room_cursor_xy is not None:
            x_m, y_m = self.room_cursor_xy
            cursor_line = pv.Line(
                (vertices[-1].x_m, -vertices[-1].y_m, 0.0),
                (x_m, -y_m, 0.0),
            )
            self.viewport.add_mesh(
                cursor_line,
                line_width=2,
                pickable=False,
                name='room-sketch-cursor',
                render=False,
            )
        self.viewport.render()

    def _render_room_edit_handles(self) -> None:
        self._remove_room_tool_overlays()
        vertices = self.room_drag_preview or self._room_vertices()
        if not vertices:
            return
        if self.room_drag_preview:
            line = _polyline(vertices, close=True)
            if line is not None:
                self.viewport.add_mesh(line, line_width=3, pickable=False, name='room-drag-line', render=False)
        vertex_points = np.asarray([(v.x_m, -v.y_m, 0.0) for v in vertices], dtype=float)
        self.viewport.add_mesh(
            pv.PolyData(vertex_points),
            render_points_as_spheres=True,
            point_size=13,
            pickable=False,
            name='room-vertex-handles',
            render=False,
        )
        midpoint_points = []
        for index, start in enumerate(vertices):
            end = vertices[(index + 1) % len(vertices)]
            midpoint_points.append(((start.x_m + end.x_m) / 2.0, -(start.y_m + end.y_m) / 2.0, 0.0))
        self.viewport.add_mesh(
            pv.PolyData(np.asarray(midpoint_points, dtype=float)),
            render_points_as_spheres=True,
            point_size=8,
            pickable=False,
            name='room-midpoint-handles',
            render=False,
        )
        self.viewport.render()

    def _near_sketch_start(self, qt_x: float, qt_y: float) -> bool:
        if len(self.room_sketch_vertices) < 3:
            return False
        start_x, start_y = self._project_room_to_qt(self.room_sketch_vertices[0])
        return hypot(qt_x - start_x, qt_y - start_y) <= 12.0

    def _hit_room_handle(self, qt_x: float, qt_y: float) -> tuple[str, int] | None:
        vertices = self._room_vertices()
        if not vertices:
            return None
        hits: list[tuple[float, str, int]] = []
        for index, vertex in enumerate(vertices):
            x, y = self._project_room_to_qt(vertex)
            distance = hypot(qt_x - x, qt_y - y)
            if distance <= 12.0:
                hits.append((distance, 'vertex', index))
        for index, start in enumerate(vertices):
            end = vertices[(index + 1) % len(vertices)]
            midpoint = RoomVertex(
                vertex_id='midpoint',
                x_m=(start.x_m + end.x_m) / 2.0,
                y_m=(start.y_m + end.y_m) / 2.0,
            )
            x, y = self._project_room_to_qt(midpoint)
            distance = hypot(qt_x - x, qt_y - y)
            if distance <= 10.0:
                hits.append((distance, 'edge', index))
        if not hits:
            return None
        _, kind, index = min(hits, key=lambda item: item[0])
        return (kind, index)

    def start_room_sketch(self) -> None:
        if self.recovery_candidate is not None or self.working is None:
            return
        if self.working.has_preview:
            super().cancel_preview()
        self._select(None)
        self._remove_gizmo()
        self.room_mode = 'sketch'
        self.room_sketch_vertices = []
        self.room_cursor_xy = None
        self.room_drag_vertex_id = None
        self.room_drag_before = ()
        self.room_drag_preview = ()
        self.selected_room_vertex_id = None
        self.selected_room_edge_index = None
        self._top()
        self._rebuild()
        self.statusBar().showMessage('Draw Room · click vertices · click the first vertex or press Enter to close · Esc cancels')

    def close_room_sketch(self) -> None:
        if self.room_mode != 'sketch':
            return
        if len(self.room_sketch_vertices) < 3:
            self.statusBar().showMessage('Room needs at least three vertices before it can be closed')
            return
        existing = self._current_room()
        height_m = existing.height_m if existing is not None else 2.4
        try:
            room = make_polygon_room(tuple(self.room_sketch_vertices), height_m=height_m)
        except ValueError as exc:
            self.statusBar().showMessage(f'Room cannot be closed · {exc}')
            return
        changed = self._replace_room(room)
        self.room_mode = 'edit'
        self.room_sketch_vertices = []
        self.room_cursor_xy = None
        self.selected_room_vertex_id = room_vertices(room)[0].vertex_id
        self.selected_room_edge_index = None
        self._rebuild()
        verb = 'created' if changed else 'unchanged'
        self.statusBar().showMessage(f'Room {verb} · edit vertices or dimensions · Done Room returns to objects')

    def start_room_edit(self) -> None:
        if self.recovery_candidate is not None or self._current_room() is None:
            self.statusBar().showMessage('Create a room before entering Room Edit')
            return
        if self.working is not None and self.working.has_preview:
            super().cancel_preview()
        self._select(None)
        self._remove_gizmo()
        self.room_mode = 'edit'
        self.room_sketch_vertices = []
        self.room_cursor_xy = None
        self.room_drag_vertex_id = None
        self.room_drag_before = ()
        self.room_drag_preview = ()
        self.selected_room_edge_index = None
        self._top()
        self._rebuild()
        self.statusBar().showMessage('Room Edit · drag vertices · select a midpoint for edge length · Insert Vertex + midpoint adds a vertex')

    def finish_room_edit(self) -> None:
        if self.room_mode == 'sketch':
            self.cancel_preview()
            return
        if self.room_mode != 'edit':
            return
        self._cancel_room_drag()
        self.room_mode = 'idle'
        self.selected_room_vertex_id = None
        self.selected_room_edge_index = None
        if hasattr(self, 'insert_vertex_action'):
            with QSignalBlocker(self.insert_vertex_action):
                self.insert_vertex_action.setChecked(False)
        self._rebuild()
        self.statusBar().showMessage('Room Edit finished · object tools active')

    def _insert_vertex_toggled(self, checked: bool) -> None:
        if checked and self.room_mode != 'edit':
            self.start_room_edit()
        self._update_actions()

    def _insert_room_vertex(self, edge_index: int) -> None:
        room = self._current_room()
        if room is None:
            return
        vertices = list(room_vertices(room))
        start = vertices[edge_index]
        end = vertices[(edge_index + 1) % len(vertices)]
        inserted = RoomVertex(
            vertex_id=f'room-v-{uuid4().hex[:12]}',
            x_m=(start.x_m + end.x_m) / 2.0,
            y_m=(start.y_m + end.y_m) / 2.0,
        )
        vertices.insert(edge_index + 1, inserted)
        try:
            replacement = make_polygon_room(vertices, height_m=room.height_m, room_id=room.room_id)
        except ValueError as exc:
            self.statusBar().showMessage(f'Vertex insertion rejected · {exc}')
            return
        if self._replace_room(replacement):
            self.selected_room_vertex_id = inserted.vertex_id
            self.selected_room_edge_index = None
            self._rebuild()
            self.statusBar().showMessage('Vertex inserted · one Undo restores the previous room')

    def delete_room_vertex(self) -> None:
        room = self._current_room()
        if self.room_mode != 'edit' or room is None or self.selected_room_vertex_id is None:
            return
        vertices = list(room_vertices(room))
        if len(vertices) <= 3:
            self.statusBar().showMessage('A room must keep at least three vertices')
            return
        vertices = [vertex for vertex in vertices if vertex.vertex_id != self.selected_room_vertex_id]
        try:
            replacement = make_polygon_room(vertices, height_m=room.height_m, room_id=room.room_id)
        except ValueError as exc:
            self.statusBar().showMessage(f'Vertex deletion rejected · {exc}')
            return
        if self._replace_room(replacement):
            self.selected_room_vertex_id = None
            self.selected_room_edge_index = None
            self._rebuild()
            self.statusBar().showMessage('Vertex deleted · one Undo restores it')

    def _replace_room(self, room: RoomPrism) -> bool:
        if not isinstance(self.working, RoomWorkingDocument):
            return False
        changed = self.working.replace_room(room)
        if changed:
            self._sync_recovery()
        self._update_actions()
        return changed

    def _cancel_room_drag(self) -> None:
        if self.room_drag_vertex_id is None:
            return
        if QWidget.mouseGrabber() is self.viewport.interactor:
            self.viewport.interactor.releaseMouse()
        self.room_drag_vertex_id = None
        self.room_drag_before = ()
        self.room_drag_preview = ()
        self._rebuild()

    def _commit_room_drag(self) -> None:
        room = self._current_room()
        if room is None or self.room_drag_vertex_id is None:
            self._cancel_room_drag()
            return
        preview = self.room_drag_preview or self.room_drag_before
        vertex_id = self.room_drag_vertex_id
        if QWidget.mouseGrabber() is self.viewport.interactor:
            self.viewport.interactor.releaseMouse()
        self.room_drag_vertex_id = None
        self.room_drag_before = ()
        self.room_drag_preview = ()
        try:
            replacement = make_polygon_room(preview, height_m=room.height_m, room_id=room.room_id)
        except ValueError as exc:
            self._rebuild()
            self.statusBar().showMessage(f'Vertex move rejected · {exc}')
            return
        changed = self._replace_room(replacement)
        self.selected_room_vertex_id = vertex_id
        self._rebuild()
        self.statusBar().showMessage('Vertex move committed · one Undo' if changed else 'Vertex move unchanged')

    def _numeric_room_vertex_edited(self) -> None:
        room = self._current_room()
        if self.room_mode != 'edit' or room is None or self.selected_room_vertex_id is None:
            return
        vertices = list(room_vertices(room))
        for index, vertex in enumerate(vertices):
            if vertex.vertex_id == self.selected_room_vertex_id:
                vertices[index] = RoomVertex(
                    vertex_id=vertex.vertex_id,
                    x_m=self.room_vertex_x.value(),
                    y_m=self.room_vertex_y.value(),
                )
                break
        try:
            replacement = make_polygon_room(vertices, height_m=room.height_m, room_id=room.room_id)
        except ValueError as exc:
            self._refresh_room_inspector()
            self.statusBar().showMessage(f'Vertex coordinate rejected · {exc}')
            return
        changed = self._replace_room(replacement)
        if changed:
            self._rebuild()
            self.statusBar().showMessage('Vertex coordinate committed · bounds updated automatically')
        else:
            self._refresh_room_inspector()

    def _numeric_room_edge_edited(self) -> None:
        room = self._current_room()
        if self.room_mode != 'edit' or room is None or self.selected_room_edge_index is None:
            return
        vertices = list(room_vertices(room))
        index = self.selected_room_edge_index % len(vertices)
        start = vertices[index]
        end_index = (index + 1) % len(vertices)
        end = vertices[end_index]
        dx = end.x_m - start.x_m
        dy = end.y_m - start.y_m
        current_length = hypot(dx, dy)
        if current_length <= 1e-12:
            return
        requested = self.room_edge_length.value()
        scale = requested / current_length
        vertices[end_index] = RoomVertex(
            vertex_id=end.vertex_id,
            x_m=start.x_m + dx * scale,
            y_m=start.y_m + dy * scale,
        )
        try:
            replacement = make_polygon_room(vertices, height_m=room.height_m, room_id=room.room_id)
        except ValueError as exc:
            self._refresh_room_inspector()
            self.statusBar().showMessage(f'Edge dimension rejected · {exc}')
            return
        changed = self._replace_room(replacement)
        if changed:
            self._rebuild()
            self.statusBar().showMessage('Edge dimension committed · endpoint and bounds updated')
        else:
            self._refresh_room_inspector()

    def _numeric_room_height_edited(self) -> None:
        room = self._current_room()
        if self.room_mode != 'edit' or room is None:
            return
        replacement = room.model_copy(update={'height_m': float(self.room_height.value())})
        replacement = RoomPrism.model_validate(replacement.model_dump(mode='python'))
        changed = self._replace_room(replacement)
        if changed:
            self._rebuild()
            self.statusBar().showMessage('Ceiling height committed · one Undo')
        else:
            self._refresh_room_inspector()

    def _refresh_room_inspector(self) -> None:
        if not hasattr(self, 'room_bounds_label'):
            return
        room = self._current_room()
        self.room_tool_label.setText(self.room_mode.title())
        editable = self.room_mode == 'edit' and self.recovery_candidate is None and room is not None
        for field in (self.room_vertex_x, self.room_vertex_y, self.room_edge_length, self.room_height):
            field.setEnabled(False)
        if room is None:
            self.room_bounds_label.setText('—')
            return
        min_x, min_y, max_x, max_y = room.bounds_m
        self.room_bounds_label.setText(
            f'X {min_x:.3f}…{max_x:.3f} m · Y {min_y:.3f}…{max_y:.3f} m'
        )
        with QSignalBlocker(self.room_height):
            self.room_height.setValue(room.height_m)
        self.room_height.setEnabled(editable)

        vertices = room_vertices(room)
        selected_vertex = next(
            (vertex for vertex in vertices if vertex.vertex_id == self.selected_room_vertex_id),
            None,
        )
        if selected_vertex is not None:
            with QSignalBlocker(self.room_vertex_x), QSignalBlocker(self.room_vertex_y):
                self.room_vertex_x.setValue(selected_vertex.x_m)
                self.room_vertex_y.setValue(selected_vertex.y_m)
            self.room_vertex_x.setEnabled(editable)
            self.room_vertex_y.setEnabled(editable)
        if self.selected_room_edge_index is not None and vertices:
            index = self.selected_room_edge_index % len(vertices)
            start = vertices[index]
            end = vertices[(index + 1) % len(vertices)]
            with QSignalBlocker(self.room_edge_length):
                self.room_edge_length.setValue(hypot(end.x_m - start.x_m, end.y_m - start.y_m))
            self.room_edge_length.setEnabled(editable)

    def _update_actions(self) -> None:
        super()._update_actions()
        if not hasattr(self, 'draw_room_action'):
            return
        blocked = self.recovery_candidate is not None or self.working is None
        room_exists = self._current_room() is not None
        self.draw_room_action.setEnabled(not blocked and self.room_mode != 'sketch')
        self.edit_room_action.setEnabled(not blocked and room_exists and self.room_mode != 'sketch')
        self.close_room_action.setEnabled(not blocked and self.room_mode == 'sketch')
        self.done_room_action.setEnabled(not blocked and self.room_mode == 'edit')
        self.insert_vertex_action.setEnabled(not blocked and self.room_mode == 'edit' and room_exists)
        self.delete_vertex_action.setEnabled(
            not blocked and self.room_mode == 'edit' and self.selected_room_vertex_id is not None
        )
        room_busy = self.room_mode == 'sketch' or self.room_drag_vertex_id is not None
        self.save_action.setEnabled(self.save_action.isEnabled() and not room_busy)

    def save(self) -> None:
        if self.room_mode == 'sketch' or self.room_drag_vertex_id is not None:
            self.statusBar().showMessage('Finish or cancel the active room operation before Save')
            return
        super().save()

    def cancel_preview(self) -> None:
        if self.room_drag_vertex_id is not None:
            self._cancel_room_drag()
            self.statusBar().showMessage('Vertex move cancelled · history unchanged')
            return
        if self.room_mode == 'sketch':
            self.room_mode = 'idle'
            self.room_sketch_vertices = []
            self.room_cursor_xy = None
            self._rebuild()
            self.statusBar().showMessage('Room sketch cancelled · history unchanged')
            return
        if hasattr(self, 'insert_vertex_action') and self.insert_vertex_action.isChecked():
            self.insert_vertex_action.setChecked(False)
            self.statusBar().showMessage('Insert Vertex cancelled')
            return
        super().cancel_preview()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.viewport.interactor and self.room_mode in {'sketch', 'edit'}:
            event_type = event.type()
            if event_type == QEvent.Type.KeyPress:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.room_mode == 'sketch':
                    self.close_room_sketch()
                    return True
                if event.key() == Qt.Key.Key_Escape:
                    self.cancel_preview()
                    return True

            position = getattr(event, 'position', lambda: None)()
            if position is not None:
                qt_x = float(position.x())
                qt_y = float(position.y())
            else:
                qt_x = qt_y = 0.0

            if self.room_mode == 'sketch':
                if event_type == QEvent.Type.MouseMove:
                    floor = self._screen_to_floor(qt_x, qt_y)
                    if floor is not None:
                        self.room_cursor_xy = floor
                        self._render_room_sketch_overlay()
                    return True
                if event_type == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                    if self._near_sketch_start(qt_x, qt_y):
                        self.close_room_sketch()
                        return True
                    floor = self._screen_to_floor(qt_x, qt_y)
                    if floor is None:
                        return True
                    x_m, y_m = floor
                    self.room_sketch_vertices.append(
                        RoomVertex(vertex_id=f'room-v-{uuid4().hex[:12]}', x_m=x_m, y_m=y_m)
                    )
                    self.room_cursor_xy = floor
                    self._render_room_sketch_overlay()
                    self.statusBar().showMessage(
                        f'Draw Room · {len(self.room_sketch_vertices)} vertices · click start/Enter to close · Esc cancels'
                    )
                    return True
                if event_type in (QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick):
                    return True

            if self.room_mode == 'edit':
                if event_type == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                    hit = self._hit_room_handle(qt_x, qt_y)
                    if hit is None:
                        self.selected_room_vertex_id = None
                        self.selected_room_edge_index = None
                        self._refresh_room_inspector()
                        self._update_actions()
                        return True
                    kind, index = hit
                    vertices = self._room_vertices()
                    if kind == 'edge':
                        self.selected_room_vertex_id = None
                        self.selected_room_edge_index = index
                        if hasattr(self, 'insert_vertex_action') and self.insert_vertex_action.isChecked():
                            self._insert_room_vertex(index)
                        else:
                            self._refresh_room_inspector()
                            self._update_actions()
                            self.statusBar().showMessage('Edge selected · type an exact length or enable Insert Vertex')
                        return True
                    vertex = vertices[index]
                    self.selected_room_vertex_id = vertex.vertex_id
                    self.selected_room_edge_index = None
                    self.room_drag_vertex_id = vertex.vertex_id
                    self.room_drag_before = vertices
                    self.room_drag_preview = vertices
                    self.viewport.interactor.grabMouse()
                    self._refresh_room_inspector()
                    self._update_actions()
                    return True
                if event_type == QEvent.Type.MouseMove and self.room_drag_vertex_id is not None:
                    floor = self._screen_to_floor(qt_x, qt_y)
                    if floor is None:
                        return True
                    x_m, y_m = floor
                    preview = list(self.room_drag_before)
                    for index, vertex in enumerate(preview):
                        if vertex.vertex_id == self.room_drag_vertex_id:
                            preview[index] = RoomVertex(vertex_id=vertex.vertex_id, x_m=x_m, y_m=y_m)
                            break
                    self.room_drag_preview = tuple(preview)
                    self._render_room_edit_handles()
                    return True
                if (
                    event_type == QEvent.Type.MouseButtonRelease
                    and event.button() == Qt.MouseButton.LeftButton
                    and self.room_drag_vertex_id is not None
                ):
                    self._commit_room_drag()
                    return True
                if event_type == QEvent.Type.MouseButtonRelease:
                    return True
        return super().eventFilter(watched, event)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate and self.room_drag_vertex_id is not None:
            self._cancel_room_drag()
        return super().event(event)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Run the HTDT native CAD editor N30a room shell')
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    parser.add_argument('--document-id', default=F1_DOCUMENT_ID)
    args = parser.parse_args(argv)
    app = QApplication([sys.argv[0]])
    repository = SceneRepository(args.data_dir / 'cad-scenes.sqlite3')
    window = RoomEditorWindow(repository, args.document_id)
    window.show()
    return int(app.exec())


if __name__ == '__main__':
    raise SystemExit(main())
