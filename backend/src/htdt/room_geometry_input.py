from __future__ import annotations

from math import hypot
from uuid import uuid4

import numpy as np
import pyvista as pv
from shapely.geometry import Polygon
from PySide6.QtCore import QEvent, QObject, QPointF, Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent

from .cad_document import EditStateError
from .cad_room_authoring import ceiling_height_at
from .cad_scene import (
    RoomAuthoringModel,
    RoomPrism,
    RoomVertex,
    make_polygon_room,
    room_vertices,
)
from .cad_wall_models import WallSegment, WallTopology
from .cad_walls import (
    WallTopologyError,
    delete_wall,
    make_wall_topology,
    merge_walls,
    move_wall,
    split_wall,
)
from .room_viewport import (
    RoomViewport3D,
    reset_camera_or_floor_default,
)
from .room_workspace import RoomWorkspace
from .ui_theme import DARK_THEME
from .user_facing_error import operation_error_message


class RoomGeometryInputController(QObject):
    """Direct-manipulation room/wall editor for the UX120 viewport.

    This controller owns transient pointer/selection state only. Valid geometry is
    committed through RoomWorkingDocument / cad_walls authority so Undo, recovery,
    wall IDs, openings and SceneRevision semantics remain centralized.
    """

    selectionChanged = Signal()

    def __init__(self, workspace: RoomWorkspace, viewport: RoomViewport3D) -> None:
        super().__init__(workspace)
        self.workspace = workspace
        self.viewport = viewport
        self.mode = "idle"
        self._sketch: list[RoomVertex] = []
        self._cursor: tuple[float, float] | None = None
        self._drag_index: int | None = None
        self._drag_base: tuple[RoomVertex, ...] = ()
        self._drag_preview: tuple[RoomVertex, ...] = ()
        self._wall_drag_edge_index: int | None = None
        self._wall_drag_start_xy: tuple[float, float] | None = None
        self._wall_drag_room: RoomPrism | None = None
        self._wall_drag_topology: WallTopology | None = None
        self._wall_drag_preview_room: RoomPrism | None = None
        self._wall_drag_preview_topology: WallTopology | None = None
        self.selected_vertex_id: str | None = None
        self.selected_edge_index: int | None = None
        # Issue #976: (kind, primitive_id) of the selected semantic primitive.
        self.selected_authoring: tuple[str, str] | None = None
        self._authoring_preview: RoomAuthoringModel | None = None
        self._authoring_hit_targets: list[tuple[str, str]] = []
        self._authoring_actor_names: set[str] = set()
        self._opening_actor_names: set[str] = set()
        viewport.interactor.installEventFilter(self)

    @property
    def is_active(self) -> bool:
        return self.mode != "idle"

    @property
    def room(self) -> RoomPrism | None:
        return self.workspace.controller.committed_document.room

    @property
    def topology(self) -> WallTopology | None:
        return self.workspace.controller.committed_document.wall_topology

    @property
    def authoring(self) -> RoomAuthoringModel | None:
        return self.workspace.controller.committed_document.room_authoring

    @property
    def selected_vertex(self) -> RoomVertex | None:
        room = self.room
        if room is None or self.selected_vertex_id is None:
            return None
        return next(
            (item for item in room_vertices(room) if item.vertex_id == self.selected_vertex_id),
            None,
        )

    @property
    def selected_wall(self) -> WallSegment | None:
        room = self.room
        topology = self.topology
        if room is None or topology is None or self.selected_edge_index is None:
            return None
        return self._wall_for_edge(room, topology, self.selected_edge_index)

    def dispose(self) -> None:
        try:
            self.viewport.interactor.removeEventFilter(self)
        except RuntimeError:
            pass
        self._clear_overlays()
        self.mode = "idle"

    def start_sketch(self) -> None:
        if self.workspace.controller.recovery_candidate is not None:
            self.workspace._set_status("復旧データを処理してから部屋を描いてください", error=True)
            return
        if self.workspace.controller.working.has_preview:
            self.workspace.controller.working.cancel_preview()
        self.workspace.select_entity(None)
        self.mode = "sketch"
        self._sketch = []
        self._cursor = None
        self._reset_drag()
        self._clear_selection()
        self._view_top()
        self._render_sketch()
        self.workspace._set_status(
            "部屋を描画中 · 頂点をクリック · 最初の頂点または Enter で閉じる · Esc で中止"
        )

    def start_edit(self) -> None:
        room = self.room
        if room is None:
            self.workspace._set_status("先に部屋を描いてください", error=True)
            return
        if self.workspace.controller.recovery_candidate is not None:
            self.workspace._set_status("復旧データを処理してから形状を編集してください", error=True)
            return
        self.workspace.select_entity(None)
        self.mode = "edit"
        self._sketch = []
        self._cursor = None
        self._reset_drag()
        self._clear_selection()
        self._view_top()
        self._render_edit_handles()
        self.workspace._set_status(
            "形状編集中 · 頂点をドラッグ · 壁を選択・ドラッグで移動 · Enter で終了"
        )

    def commit(self) -> bool:
        if self.mode == "sketch":
            return self._close_sketch()
        if self.mode == "edit":
            self._finish()
            return True
        return False

    def cancel(self) -> bool:
        if self.mode == "idle":
            return False
        self._finish()
        self.workspace._set_status("形状編集を終了しました")
        return True

    def select_vertex(self, vertex_id: str | None) -> None:
        room = self.room
        if vertex_id is not None:
            if room is None or not any(v.vertex_id == vertex_id for v in room_vertices(room)):
                raise KeyError(vertex_id)
        self.selected_vertex_id = vertex_id
        self.selected_edge_index = None
        if vertex_id is not None:
            self.selected_authoring = None
        self.selectionChanged.emit()
        if self.mode == "edit":
            self._render_edit_handles()

    def select_authoring_primitive(self, ref: tuple[str, str] | None) -> None:
        """Select one #976 primitive as ``(kind, primitive_id)``.

        The same (kind, id) pair names the panel row, the 3D outline and the
        saved revision entry — selection never invents a second identity.
        """

        if ref is not None:
            model = self.authoring
            kinds = _authoring_primitive_ids(model) if model is not None else {}
            if ref[1] not in kinds.get(ref[0], ()):
                raise KeyError(ref)
        self.selected_authoring = ref
        if ref is not None:
            self.selected_vertex_id = None
            self.selected_edge_index = None
        self.selectionChanged.emit()
        if self.mode == "edit":
            self._render_edit_handles()

    def set_authoring_preview(self, candidate: RoomAuthoringModel | None) -> None:
        """Ghost-render a candidate model before commit (preview→commit parity)."""

        self._authoring_preview = candidate
        if self.mode == "edit":
            self._render_edit_handles()
        else:
            self._clear_overlays()
            self._render_authoring_overlays()
            self.viewport.plotter.render()

    def select_edge(self, edge_index: int | None) -> None:
        room = self.room
        if edge_index is not None:
            if room is None:
                raise ValueError("部屋がありません")
            edge_index %= len(room_vertices(room))
        self.selected_edge_index = edge_index
        self.selected_vertex_id = None
        if edge_index is not None:
            self.selected_authoring = None
        self.selectionChanged.emit()
        if self.mode == "edit":
            self._render_edit_handles()

    def set_selected_vertex_coordinates(self, *, x_m: float, y_m: float) -> bool:
        room = self.room
        selected = self.selected_vertex
        if room is None or selected is None:
            return False
        vertices = list(room_vertices(room))
        for index, vertex in enumerate(vertices):
            if vertex.vertex_id == selected.vertex_id:
                vertices[index] = RoomVertex(
                    vertex_id=vertex.vertex_id,
                    x_m=float(x_m),
                    y_m=float(y_m),
                )
                break
        replacement = make_polygon_room(
            vertices,
            height_m=room.height_m,
            room_id=room.room_id,
        )
        changed = self._commit_room_preserving_topology(replacement)
        if changed:
            self._after_geometry_change("頂点座標を更新しました")
        return changed

    def set_selected_edge_length(self, length_m: float) -> bool:
        room = self.room
        if room is None or self.selected_edge_index is None:
            return False
        requested = float(length_m)
        if requested <= 0.0:
            raise ValueError("辺の長さは0より大きい値が必要です")
        vertices = list(room_vertices(room))
        index = self.selected_edge_index % len(vertices)
        start = vertices[index]
        end_index = (index + 1) % len(vertices)
        end = vertices[end_index]
        dx = end.x_m - start.x_m
        dy = end.y_m - start.y_m
        current = hypot(dx, dy)
        if current <= 1e-12:
            raise ValueError("長さ0の辺は編集できません")
        scale = requested / current
        vertices[end_index] = RoomVertex(
            vertex_id=end.vertex_id,
            x_m=start.x_m + dx * scale,
            y_m=start.y_m + dy * scale,
        )
        replacement = make_polygon_room(
            vertices,
            height_m=room.height_m,
            room_id=room.room_id,
        )
        changed = self._commit_room_preserving_topology(replacement)
        if changed:
            self._after_geometry_change("辺の長さを更新しました")
        return changed

    def set_room_height(self, height_m: float) -> bool:
        room = self.room
        if room is None:
            return False
        replacement = RoomPrism.model_validate(
            room.model_copy(update={"height_m": float(height_m)}).model_dump(mode="python")
        )
        changed = self._commit_room_preserving_topology(replacement)
        if changed:
            self._after_geometry_change("天井高を更新しました")
        return changed

    def insert_selected_edge_midpoint(self) -> bool:
        return self.insert_selected_edge_vertex(None)

    def insert_selected_edge_vertex(self, offset_m: float | None) -> bool:
        """Insert a vertex on the selected edge ``offset_m`` from its start.

        ``None`` resolves to the edge midpoint. With wall topology the call
        routes through ``split_wall`` so openings and constraint bindings
        migrate onto the child walls.
        """

        room = self.room
        if room is None or self.selected_edge_index is None:
            return False
        vertices = list(room_vertices(room))
        edge_index = self.selected_edge_index % len(vertices)
        start = vertices[edge_index]
        end = vertices[(edge_index + 1) % len(vertices)]
        edge_length = hypot(end.x_m - start.x_m, end.y_m - start.y_m)
        offset = edge_length * 0.5 if offset_m is None else float(offset_m)
        if not 1e-9 < offset < edge_length - 1e-9:
            raise ValueError("分割位置は辺の内側を指定してください")
        ratio = offset / edge_length
        topology = self.topology
        if topology is None:
            inserted = RoomVertex(
                vertex_id=f"room-v-{uuid4().hex[:12]}",
                x_m=start.x_m + (end.x_m - start.x_m) * ratio,
                y_m=start.y_m + (end.y_m - start.y_m) * ratio,
            )
            vertices.insert(edge_index + 1, inserted)
            replacement = make_polygon_room(
                vertices,
                height_m=room.height_m,
                room_id=room.room_id,
            )
            changed = self.workspace.controller.replace_room(replacement)
            if changed:
                self.selected_vertex_id = inserted.vertex_id
                self.selected_edge_index = None
                self._after_geometry_change("辺に頂点を追加しました")
            return changed

        wall = self._wall_for_edge(room, topology, edge_index)
        token = uuid4().hex[:10]
        split_room, split_topology = split_wall(
            room,
            topology,
            wall.wall_id,
            offset_m=offset,
            new_vertex_id=f"room-v-{token}",
            first_wall_id=f"{wall.wall_id}:a:{token}",
            second_wall_id=f"{wall.wall_id}:b:{token}",
        )
        new_vertex_ids = {
            vertex.vertex_id for vertex in room_vertices(split_room)
        } - {vertex.vertex_id for vertex in room_vertices(room)}
        changed = self.workspace.controller.replace_room_topology(
            split_room,
            split_topology,
        )
        if changed:
            self.selected_vertex_id = next(iter(new_vertex_ids), None)
            self.selected_edge_index = None
            self._after_geometry_change("壁参照を維持して頂点を追加しました")
        return changed

    def delete_selected_vertex(self) -> bool:
        room = self.room
        selected = self.selected_vertex
        if room is None or selected is None:
            return False
        vertices = tuple(room_vertices(room))
        if len(vertices) <= 3:
            raise ValueError("部屋には3頂点以上が必要です")
        vertex_index = next(
            index for index, vertex in enumerate(vertices) if vertex.vertex_id == selected.vertex_id
        )
        topology = self.topology
        if topology is None:
            replacement = make_polygon_room(
                tuple(v for v in vertices if v.vertex_id != selected.vertex_id),
                height_m=room.height_m,
                room_id=room.room_id,
            )
            changed = self.workspace.controller.replace_room(replacement)
        else:
            predecessor_edge = (vertex_index - 1) % len(vertices)
            predecessor = self._wall_for_edge(room, topology, predecessor_edge)
            token = uuid4().hex[:10]
            replacement_room, replacement_topology = delete_wall(
                room,
                topology,
                predecessor.wall_id,
                replacement_wall_id=f"wall:{predecessor.from_vertex_id}->{vertices[(vertex_index + 1) % len(vertices)].vertex_id}:{token}",
            )
            changed = self.workspace.controller.replace_room_topology(
                replacement_room,
                replacement_topology,
            )
        if changed:
            self._clear_selection()
            self._after_geometry_change("頂点を削除しました")
        return changed

    def ensure_wall_topology(self) -> bool:
        room = self.room
        if room is None:
            return False
        if self.topology is not None:
            return False
        topology = make_wall_topology(room)
        changed = self.workspace.controller.replace_room_topology(room, topology)
        if changed:
            self._after_geometry_change("壁編集を有効にしました")
        return changed

    def merge_selected_wall_with_next(self) -> bool:
        room = self.room
        topology = self.topology
        wall = self.selected_wall
        if room is None or topology is None or wall is None:
            return False
        walls = list(topology.walls)
        index = walls.index(wall)
        next_wall = walls[(index + 1) % len(walls)]
        merged_room, merged_topology = merge_walls(
            room,
            topology,
            wall.wall_id,
            next_wall.wall_id,
            merged_wall_id=f"wall-merged-{uuid4().hex[:10]}",
        )
        changed = self.workspace.controller.replace_room_topology(
            merged_room,
            merged_topology,
        )
        if changed:
            self.selected_edge_index = min(index, len(merged_topology.walls) - 1)
            self._after_geometry_change("隣接する壁を結合しました")
        return changed

    def delete_selected_wall(self) -> bool:
        room = self.room
        topology = self.topology
        wall = self.selected_wall
        if room is None or topology is None or wall is None:
            return False
        token = uuid4().hex[:10]
        deleted_room, deleted_topology = delete_wall(
            room,
            topology,
            wall.wall_id,
            replacement_wall_id=f"wall-replacement-{token}",
        )
        changed = self.workspace.controller.replace_room_topology(
            deleted_room,
            deleted_topology,
        )
        if changed:
            self.selected_edge_index = None
            self._after_geometry_change("壁を削除しました")
        return changed

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        viewport = getattr(self, "viewport", None)
        if viewport is None or watched is not viewport.interactor or self.mode == "idle":
            return False
        event_type = event.type()
        if event_type == QEvent.Type.MouseButtonPress:
            return self._mouse_press(event)  # type: ignore[arg-type]
        if event_type == QEvent.Type.MouseMove:
            return self._mouse_move(event)  # type: ignore[arg-type]
        if event_type == QEvent.Type.MouseButtonRelease:
            return self._mouse_release(event)  # type: ignore[arg-type]
        if event_type == QEvent.Type.KeyPress:
            return self._key_press(event)  # type: ignore[arg-type]
        return False

    def _key_press(self, event: QKeyEvent) -> bool:
        """Arrow-key nudge of the selected vertex/wall (round8 keyboard parity).

        Step = the workspace grid step (Shift = x10, matching MetricSpinBox);
        each keypress commits through the working document so Undo restores
        the previous geometry exactly.
        """

        if self.mode != "edit" or not isinstance(event, QKeyEvent):
            return False
        if event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        ):
            return False
        step = float(self.workspace.controller.view_state.grid_step_m) * (
            10.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
        )
        delta = {
            Qt.Key.Key_Left: (-step, 0.0),
            Qt.Key.Key_Right: (step, 0.0),
            Qt.Key.Key_Up: (0.0, step),
            Qt.Key.Key_Down: (0.0, -step),
        }.get(event.key())
        if delta is None:
            return False
        room = self.room
        if room is None:
            return False
        dx, dy = delta
        if self.selected_vertex_id is not None:
            vertex = self.selected_vertex
            if vertex is None:
                return False
            try:
                changed = self.set_selected_vertex_coordinates(
                    x_m=vertex.x_m + dx, y_m=vertex.y_m + dy
                )
            except (EditStateError, ValueError, WallTopologyError) as exc:
                self.workspace._set_status(
                    f"頂点を移動できません: {operation_error_message(exc)}",
                    error=True,
                )
                event.accept()
                return True
            if changed:
                self.workspace._set_status("頂点を移動しました · 元に戻すで復元できます")
                event.accept()
                return True
            return False
        if self.selected_edge_index is not None:
            topology = self.topology or make_wall_topology(room)
            wall = self._wall_for_edge(room, topology, self.selected_edge_index)
            try:
                moved_room, moved_topology = move_wall(
                    room, topology, wall.wall_id, delta_x_m=dx, delta_y_m=dy
                )
            except WallTopologyError as exc:
                self.workspace._set_status(
                    f"この位置には壁を移動できません · {operation_error_message(exc)}",
                    error=True,
                )
                event.accept()
                return True
            edge_index = self.selected_edge_index
            changed = self.workspace.controller.replace_room_topology(
                moved_room, moved_topology
            )
            if changed:
                self.selected_edge_index = edge_index
                self.workspace.refresh()
                self.selectionChanged.emit()
                self._render_edit_handles()
                self.workspace._set_status("壁を移動しました · 開口と壁IDを維持しています")
                event.accept()
                return True
            return False
        return False

    def _mouse_press(self, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        position = QPointF(event.position())
        if self.mode == "sketch":
            floor = self._screen_to_floor(position)
            if floor is None:
                return False
            if len(self._sketch) >= 3 and self._near_first(position):
                self._close_sketch()
            else:
                x_m, y_m = floor
                self._sketch.append(
                    RoomVertex(
                        vertex_id=f"room-v-{uuid4().hex[:12]}",
                        x_m=x_m,
                        y_m=y_m,
                    )
                )
                self._render_sketch()
            event.accept()
            return True

        hit = self._hit_handle(position)
        if hit is None:
            self._clear_selection()
            self.selectionChanged.emit()
            self._render_edit_handles()
            return False
        kind, index = hit
        if kind == "authoring":
            ref = self._authoring_hit_targets[index]
            self.selected_vertex_id = None
            self.selected_edge_index = None
            self.selected_authoring = ref
            self.selectionChanged.emit()
            self._render_edit_handles()
            event.accept()
            return True
        room = self.room
        if room is None:
            return False
        vertices = tuple(room_vertices(room))
        if kind == "vertex":
            self.selected_vertex_id = vertices[index].vertex_id
            self.selected_edge_index = None
            self.selectionChanged.emit()
            self._drag_index = index
            self._drag_base = vertices
            self._drag_preview = vertices
        else:
            self.selected_vertex_id = None
            self.selected_edge_index = index
            self.selectionChanged.emit()
            floor = self._screen_to_floor(position)
            if floor is not None:
                self._wall_drag_edge_index = index
                self._wall_drag_start_xy = floor
                self._wall_drag_room = room
                self._wall_drag_topology = self.topology or make_wall_topology(room)
        self._render_edit_handles()
        event.accept()
        return True

    def _mouse_move(self, event: QMouseEvent) -> bool:
        position = QPointF(event.position())
        if self.mode == "sketch":
            floor = self._screen_to_floor(position)
            if floor is not None:
                self._cursor = floor
                self._render_sketch()
            return False

        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return False
        if self._drag_index is not None:
            floor = self._screen_to_floor(position)
            if floor is None:
                return True
            x_m, y_m = floor
            vertices = list(self._drag_base)
            original = vertices[self._drag_index]
            vertices[self._drag_index] = RoomVertex(
                vertex_id=original.vertex_id,
                x_m=x_m,
                y_m=y_m,
            )
            self._drag_preview = tuple(vertices)
            self._render_edit_handles(self._drag_preview)
            event.accept()
            return True

        if self._wall_drag_edge_index is not None:
            floor = self._screen_to_floor(position)
            if (
                floor is None
                or self._wall_drag_start_xy is None
                or self._wall_drag_room is None
                or self._wall_drag_topology is None
            ):
                return True
            dx = floor[0] - self._wall_drag_start_xy[0]
            dy = floor[1] - self._wall_drag_start_xy[1]
            wall = self._wall_for_edge(
                self._wall_drag_room,
                self._wall_drag_topology,
                self._wall_drag_edge_index,
            )
            try:
                moved_room, moved_topology = move_wall(
                    self._wall_drag_room,
                    self._wall_drag_topology,
                    wall.wall_id,
                    delta_x_m=dx,
                    delta_y_m=dy,
                )
            except WallTopologyError as exc:
                self._wall_drag_preview_room = None
                self._wall_drag_preview_topology = None
                self.workspace._set_status(f"この位置には壁を移動できません · {operation_error_message(exc)}", error=True)
                self._render_edit_handles()
                return True
            self._wall_drag_preview_room = moved_room
            self._wall_drag_preview_topology = moved_topology
            self._render_edit_handles(tuple(room_vertices(moved_room)))
            event.accept()
            return True
        return False

    def _mouse_release(self, event: QMouseEvent) -> bool:
        if self.mode != "edit" or event.button() != Qt.MouseButton.LeftButton:
            return False
        if self._drag_index is not None:
            self._commit_vertex_drag()
            event.accept()
            return True
        if self._wall_drag_edge_index is not None:
            self._commit_wall_drag()
            event.accept()
            return True
        return False

    def _close_sketch(self) -> bool:
        if len(self._sketch) < 3:
            self.workspace._set_status("部屋には3頂点以上が必要です", error=True)
            return False
        # Domain validation (polygon_from_vertices) raises English
        # diagnostics, which the error mapper renders as a generic
        # 'データを処理できませんでした'. The two failures a user can
        # actually draw — stacked vertices and a self-intersecting
        # outline — get explicit guidance here instead.
        coords = tuple((vertex.x_m, vertex.y_m) for vertex in self._sketch)
        if len(set(coords)) != len(coords):
            self.workspace._set_status(
                "部屋形状を確定できません: 頂点が重なっています · Esc で描き直してください",
                error=True,
            )
            return False
        if not Polygon(coords).is_valid:
            self.workspace._set_status(
                "部屋形状を確定できません: 外形が自己交差しています · 頂点を時計回りに置くか Esc で描き直してください",
                error=True,
            )
            return False
        current_document = self.workspace.controller.committed_document
        current = current_document.room
        height_m = 2.4 if current is None else current.height_m
        try:
            room = make_polygon_room(tuple(self._sketch), height_m=height_m)
            if current_document.wall_topology is None:
                changed = self.workspace.controller.replace_room(room)
            else:
                old_topology = current_document.wall_topology
                if old_topology.openings or old_topology.constraint_bindings:
                    raise ValueError(
                        "開口または壁制約があるため部屋を描き直せません。先に参照を整理してください"
                    )
                thickness = (
                    old_topology.walls[0].thickness_m
                    if old_topology.walls
                    else 0.10
                )
                changed = self.workspace.controller.replace_room_topology(
                    room,
                    make_wall_topology(room, thickness_m=thickness),
                )
        except (EditStateError, ValueError, WallTopologyError) as exc:
            self.workspace._set_status(f"部屋形状を確定できません: {operation_error_message(exc)}", error=True)
            return False
        self.mode = "edit"
        self._sketch = []
        self._cursor = None
        self.workspace.refresh(reset_camera=False)
        self._render_edit_handles()
        self.workspace._set_status(
            "部屋を作成しました · 頂点や辺を選択して調整 · Enter で終了"
            if changed
            else "部屋形状は変更されていません"
        )
        return changed

    def _commit_vertex_drag(self) -> None:
        room = self.room
        preview = self._drag_preview
        self._drag_index = None
        self._drag_base = ()
        self._drag_preview = ()
        if room is None or not preview:
            self._render_edit_handles()
            return
        try:
            replacement = make_polygon_room(
                preview,
                height_m=room.height_m,
                room_id=room.room_id,
            )
            changed = self._commit_room_preserving_topology(replacement)
        except (EditStateError, ValueError, WallTopologyError) as exc:
            self.workspace._set_status(f"頂点移動を適用できません: {operation_error_message(exc)}", error=True)
            self.workspace.refresh()
            self._render_edit_handles()
            return
        self.workspace.refresh()
        self._render_edit_handles()
        if changed:
            self.workspace._set_status("頂点を移動しました · 元に戻すで復元できます")

    def _commit_wall_drag(self) -> None:
        moved_room = self._wall_drag_preview_room
        moved_topology = self._wall_drag_preview_topology
        edge_index = self._wall_drag_edge_index
        self._reset_wall_drag()
        if moved_room is None or moved_topology is None:
            self._render_edit_handles()
            return
        try:
            changed = self.workspace.controller.replace_room_topology(
                moved_room,
                moved_topology,
            )
        except (EditStateError, ValueError, WallTopologyError) as exc:
            self.workspace._set_status(f"壁移動を適用できません: {operation_error_message(exc)}", error=True)
            self.workspace.refresh()
            self._render_edit_handles()
            return
        if changed:
            self.selected_edge_index = edge_index
            self.workspace.refresh()
            self.selectionChanged.emit()
            self._render_edit_handles()
            self.workspace._set_status("壁を移動しました · 開口と壁IDを維持しています")

    def _commit_room_preserving_topology(self, room: RoomPrism) -> bool:
        topology = self.topology
        if topology is None:
            return self.workspace.controller.replace_room(room)
        return self.workspace.controller.replace_room_topology(room, topology)

    @staticmethod
    def _wall_for_edge(
        room: RoomPrism,
        topology: WallTopology,
        edge_index: int,
    ) -> WallSegment:
        vertices = tuple(room_vertices(room))
        index = edge_index % len(vertices)
        pair = (
            vertices[index].vertex_id,
            vertices[(index + 1) % len(vertices)].vertex_id,
        )
        wall = next(
            (
                item
                for item in topology.walls
                if (item.from_vertex_id, item.to_vertex_id) == pair
            ),
            None,
        )
        if wall is None:
            raise WallTopologyError(f"boundary edge {pair} has no wall")
        return wall

    def _after_geometry_change(self, message: str) -> None:
        self.workspace.refresh()
        self.selectionChanged.emit()
        if self.mode == "edit":
            self._render_edit_handles()
        self.workspace._set_status(message)

    def _finish(self) -> None:
        self.mode = "idle"
        self._sketch = []
        self._cursor = None
        self._reset_drag()
        self._clear_selection()
        self._clear_overlays()
        self.workspace.refresh()

    def _reset_drag(self) -> None:
        self._drag_index = None
        self._drag_base = ()
        self._drag_preview = ()
        self._reset_wall_drag()

    def _reset_wall_drag(self) -> None:
        self._wall_drag_edge_index = None
        self._wall_drag_start_xy = None
        self._wall_drag_room = None
        self._wall_drag_topology = None
        self._wall_drag_preview_room = None
        self._wall_drag_preview_topology = None

    def _clear_selection(self) -> None:
        self.selected_vertex_id = None
        self.selected_edge_index = None
        self.selected_authoring = None

    def _view_top(self) -> None:
        plotter = self.viewport.plotter
        plotter.view_xy(negative=True)
        plotter.enable_parallel_projection()
        reset_camera_or_floor_default(plotter)
        plotter.render()

    def _screen_to_floor(self, position: QPointF) -> tuple[float, float] | None:
        renderer = self.viewport.plotter.renderer
        dpr = max(float(self.viewport.interactor.devicePixelRatioF()), 1.0)
        _, render_height = self.viewport.plotter.render_window.GetSize()
        display_x = float(position.x()) * dpr
        display_y = float(render_height) - float(position.y()) * dpr

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
        return self._snap_floor(float(point[0]), -float(point[1]))

    def _snap_floor(self, x_m: float, y_m: float) -> tuple[float, float]:
        """Grid-snap a floor-plane pick (legacy room_editor parity).

        The workflow toggle/step live on the same view_state the transform
        path uses; honoring them here keeps sketch picks, vertex drags, and
        wall drags on the grid the user asked for.
        """

        view_state = self.workspace.controller.view_state
        if not view_state.grid_snap_enabled:
            return x_m, y_m
        step = float(view_state.grid_step_m)
        if step <= 0.0:
            return x_m, y_m
        return (round(x_m / step) * step, round(y_m / step) * step)

    def _project(self, vertex: RoomVertex) -> QPointF:
        return self._project_point3((vertex.x_m, vertex.y_m, 0.0))

    def _project_point3(self, point: tuple[float, float, float]) -> QPointF:
        """Domain-space (x, y, z) to display space; same DPR/flip as vertices."""

        renderer = self.viewport.plotter.renderer
        renderer.SetWorldPoint(float(point[0]), -float(point[1]), float(point[2]), 1.0)
        renderer.WorldToDisplay()
        x, y, _ = renderer.GetDisplayPoint()
        dpr = max(float(self.viewport.interactor.devicePixelRatioF()), 1.0)
        _, render_height = self.viewport.plotter.render_window.GetSize()
        return QPointF(float(x) / dpr, (float(render_height) - float(y)) / dpr)

    def _near_first(self, position: QPointF) -> bool:
        if not self._sketch:
            return False
        point = self._project(self._sketch[0])
        return hypot(position.x() - point.x(), position.y() - point.y()) <= 12.0

    @staticmethod
    def _point_segment_distance(
        position: QPointF,
        start: QPointF,
        end: QPointF,
    ) -> float:
        """Screen-space distance from ``position`` to segment ``start``-``end``."""

        px, py = position.x(), position.y()
        ax, ay = start.x(), start.y()
        bx, by = end.x(), end.y()
        dx = bx - ax
        dy = by - ay
        denom = dx * dx + dy * dy
        if denom <= 1e-12:
            return hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
        return hypot(px - (ax + t * dx), py - (ay + t * dy))

    def _hit_handle(self, position: QPointF) -> tuple[str, int] | None:
        room = self.room
        if room is None:
            return None
        vertices = tuple(room_vertices(room))
        hits: list[tuple[float, str, int]] = []
        # Issue #976: primitive outlines are selectable wherever they project —
        # top/front/section views all resolve through the same display-space hit.
        self._authoring_hit_targets = []
        for ref, segments in _authoring_outline_segments(self.authoring):
            best: float | None = None
            for start, end in segments:
                start_point = self._project_point3(start)
                end_point = self._project_point3(end)
                distance = self._point_segment_distance(position, start_point, end_point)
                if best is None or distance < best:
                    best = distance
            if best is not None and best <= 10.0:
                self._authoring_hit_targets.append(ref)
                hits.append((best, "authoring", len(self._authoring_hit_targets) - 1))
        for index, vertex in enumerate(vertices):
            point = self._project(vertex)
            distance = hypot(position.x() - point.x(), position.y() - point.y())
            if distance <= 12.0:
                hits.append((distance, "vertex", index))
        for index, start in enumerate(vertices):
            end = vertices[(index + 1) % len(vertices)]
            start_point = self._project(start)
            end_point = self._project(end)
            # Legacy wall-mode parity: the whole wall body is selectable, not
            # just its midpoint handle. Stay clear of the vertex grab zone so
            # a vertex pick keeps resolving to the vertex handle.
            endpoint_distance = min(
                hypot(position.x() - start_point.x(), position.y() - start_point.y()),
                hypot(position.x() - end_point.x(), position.y() - end_point.y()),
            )
            if endpoint_distance <= 14.0:
                continue
            distance = self._point_segment_distance(position, start_point, end_point)
            if distance <= 10.0:
                hits.append((distance, "edge", index))
        if not hits:
            return None
        _distance, kind, index = min(hits, key=lambda item: item[0])
        return kind, index

    def _render_authoring_overlays(self) -> None:
        """Issue #976: committed outlines + selection accent + ghost preview."""

        counter = 0

        def _emit(segments, *, color: str, width: float, opacity: float, tag: str) -> None:
            nonlocal counter
            for start, end in segments:
                name = f"ux120-authoring-{tag}-{counter}"
                counter += 1
                self._authoring_actor_names.add(name)
                self.viewport.plotter.add_mesh(
                    pv.Line((start[0], -start[1], start[2]), (end[0], -end[1], end[2])),
                    color=color,
                    line_width=width,
                    opacity=opacity,
                    pickable=False,
                    name=name,
                    render=False,
                )

        muted = DARK_THEME.viewport.geometry_edge.hex
        accent = DARK_THEME.viewport.selection_outline.hex
        for (kind, primitive_id), segments in _authoring_outline_segments(self.authoring):
            selected = self.selected_authoring == (kind, primitive_id)
            _emit(
                segments,
                color=accent if selected else muted,
                width=5.0 if selected else 2.5,
                opacity=0.95 if selected else 0.7,
                tag=f"{kind}-{primitive_id}",
            )
        if self._authoring_preview is not None:
            for (kind, primitive_id), segments in _authoring_outline_segments(
                self._authoring_preview
            ):
                _emit(
                    segments,
                    color="#C98A3B",
                    width=2.0,
                    opacity=0.55,
                    tag=f"preview-{kind}-{primitive_id}",
                )

    def _clear_overlays(self) -> None:
        for name in (
            "ux120-room-sketch-line",
            "ux120-room-sketch-points",
            "ux120-room-sketch-cursor",
            "ux120-room-edit-line",
            "ux120-room-edit-points",
            "ux120-room-edit-midpoints",
            "ux120-room-edit-selection",
            "ux120-room-edit-selected-wall",
        ):
            self.viewport.plotter.remove_actor(name, reset_camera=False, render=False)
        for name in tuple(self._opening_actor_names):
            self.viewport.plotter.remove_actor(name, reset_camera=False, render=False)
        self._opening_actor_names.clear()
        for name in tuple(self._authoring_actor_names):
            self.viewport.plotter.remove_actor(name, reset_camera=False, render=False)
        self._authoring_actor_names.clear()
        self.viewport.plotter.render()

    def _render_sketch(self) -> None:
        self._clear_overlays()
        vertices = tuple(self._sketch)
        if len(vertices) >= 2:
            points = np.asarray([(v.x_m, -v.y_m, 0.0) for v in vertices], dtype=float)
            self.viewport.plotter.add_mesh(
                pv.lines_from_points(points, close=False),
                line_width=3,
                pickable=False,
                name="ux120-room-sketch-line",
                render=False,
            )
        if vertices:
            points = np.asarray([(v.x_m, -v.y_m, 0.0) for v in vertices], dtype=float)
            self.viewport.plotter.add_mesh(
                pv.PolyData(points),
                render_points_as_spheres=True,
                point_size=12,
                pickable=False,
                name="ux120-room-sketch-points",
                render=False,
            )
        if vertices and self._cursor is not None:
            x_m, y_m = self._cursor
            self.viewport.plotter.add_mesh(
                pv.Line(
                    (vertices[-1].x_m, -vertices[-1].y_m, 0.0),
                    (x_m, -y_m, 0.0),
                ),
                line_width=2,
                pickable=False,
                # pv.Line's 'Distance' scalars would otherwise auto-show a
                # scalar bar whose first range is ~1e-7 — a stale tick that
                # only recalibrates once the cursor has real length.
                show_scalar_bar=False,
                name="ux120-room-sketch-cursor",
                render=False,
            )
        self.viewport.plotter.render()

    def _render_edit_handles(
        self,
        vertices: tuple[RoomVertex, ...] | None = None,
    ) -> None:
        self._clear_overlays()
        room = self.room
        if room is None:
            return
        values = vertices or tuple(room_vertices(room))
        if len(values) >= 2:
            points = np.asarray(
                [(v.x_m, -v.y_m, 0.0) for v in (*values, values[0])],
                dtype=float,
            )
            self.viewport.plotter.add_mesh(
                pv.lines_from_points(points, close=False),
                line_width=3,
                pickable=False,
                name="ux120-room-edit-line",
                render=False,
            )
        points = np.asarray([(v.x_m, -v.y_m, 0.0) for v in values], dtype=float)
        self.viewport.plotter.add_mesh(
            pv.PolyData(points),
            render_points_as_spheres=True,
            point_size=13,
            pickable=False,
            name="ux120-room-edit-points",
            render=False,
        )
        self._render_authoring_overlays()

        midpoint_points = np.asarray(
            [
                (
                    (start.x_m + values[(index + 1) % len(values)].x_m) * 0.5,
                    -(start.y_m + values[(index + 1) % len(values)].y_m) * 0.5,
                    0.0,
                )
                for index, start in enumerate(values)
            ],
            dtype=float,
        )
        self.viewport.plotter.add_mesh(
            pv.PolyData(midpoint_points),
            render_points_as_spheres=True,
            point_size=8,
            pickable=False,
            name="ux120-room-edit-midpoints",
            render=False,
        )

        selected_point: tuple[float, float, float] | None = None
        if self.selected_vertex_id is not None:
            selected = next(
                (v for v in values if v.vertex_id == self.selected_vertex_id),
                None,
            )
            if selected is not None:
                selected_point = (selected.x_m, -selected.y_m, 0.0)
        elif self.selected_edge_index is not None and values:
            index = self.selected_edge_index % len(values)
            start = values[index]
            end = values[(index + 1) % len(values)]
            selected_point = (
                (start.x_m + end.x_m) * 0.5,
                -(start.y_m + end.y_m) * 0.5,
                0.0,
            )
        if selected_point is not None:
            self.viewport.plotter.add_mesh(
                pv.Sphere(radius=0.07, center=selected_point),
                color=DARK_THEME.viewport.selection_outline.hex,
                render_points_as_spheres=True,
                pickable=False,
                name="ux120-room-edit-selection",
                render=False,
            )

        if self.selected_edge_index is not None and values:
            index = self.selected_edge_index % len(values)
            start = values[index]
            end = values[(index + 1) % len(values)]
            self.viewport.plotter.add_mesh(
                pv.Line(
                    (start.x_m, -start.y_m, 0.015),
                    (end.x_m, -end.y_m, 0.015),
                ),
                color=DARK_THEME.viewport.selection_outline.hex,
                line_width=5,
                pickable=False,
                name="ux120-room-edit-selected-wall",
                render=False,
            )

        # Legacy wall-mode parity: every opening is drawn, not only the
        # selected wall's. The selected wall keeps the accent color; other
        # walls render muted so the selection still stands out. Endpoints
        # resolve against the preview vertex set so a shared-vertex drag or a
        # wall move keeps the outlines tracking the geometry.
        topology = self._wall_drag_preview_topology or self.topology
        if topology is not None and values:
            selected_wall_id: str | None = None
            if self.selected_edge_index is not None:
                try:
                    selected_wall = self._wall_for_edge(
                        make_polygon_room(
                            values,
                            height_m=room.height_m,
                            room_id=room.room_id,
                        ),
                        topology,
                        self.selected_edge_index % len(values),
                    )
                except (ValueError, WallTopologyError):
                    selected_wall = None
                if selected_wall is not None:
                    selected_wall_id = selected_wall.wall_id
            by_vertex_id = {vertex.vertex_id: vertex for vertex in values}
            for wall in topology.walls:
                start = by_vertex_id.get(wall.from_vertex_id)
                end = by_vertex_id.get(wall.to_vertex_id)
                if start is None or end is None:
                    continue
                dx = end.x_m - start.x_m
                dy = end.y_m - start.y_m
                length = hypot(dx, dy)
                if length <= 1e-12:
                    continue
                ux, uy = dx / length, dy / length
                color = (
                    DARK_THEME.viewport.selection_outline.hex
                    if wall.wall_id == selected_wall_id
                    else DARK_THEME.viewport.geometry_edge.hex
                )
                for opening in topology.openings:
                    if opening.wall_id != wall.wall_id:
                        continue
                    x0 = start.x_m + ux * opening.offset_m
                    y0 = start.y_m + uy * opening.offset_m
                    x1 = start.x_m + ux * (opening.offset_m + opening.width_m)
                    y1 = start.y_m + uy * (opening.offset_m + opening.width_m)
                    z0 = opening.sill_m
                    z1 = opening.sill_m + opening.height_m
                    points = np.asarray(
                        [
                            (x0, -y0, z0),
                            (x1, -y1, z0),
                            (x1, -y1, z1),
                            (x0, -y0, z1),
                            (x0, -y0, z0),
                        ],
                        dtype=float,
                    )
                    name = f"ux120-room-opening-{opening.opening_id}"
                    self._opening_actor_names.add(name)
                    self.viewport.plotter.add_mesh(
                        pv.lines_from_points(points, close=False),
                        color=color,
                        line_width=3,
                        opacity=0.82,
                        pickable=False,
                        name=name,
                        render=False,
                    )
        self.viewport.plotter.render()


def _authoring_primitive_ids(model: RoomAuthoringModel | None) -> dict[str, tuple[str, ...]]:
    """(kind, primitive_id) inventory — the shared selection identity (#976)."""

    if model is None:
        return {}
    kinds: dict[str, tuple[str, ...]] = {}
    if model.ceiling is not None:
        kinds['ceiling'] = ('ceiling',)
    kinds['soffit'] = tuple(s.soffit_id for s in model.soffits)
    kinds['riser'] = tuple(r.riser_id for r in model.risers)
    kinds['partial_wall'] = tuple(w.wall_id for w in model.partial_walls)
    kinds['adjacent_region'] = tuple(r.region_id for r in model.adjacent_regions)
    return {kind: ids for kind, ids in kinds.items() if ids}


def _rect_ring_segments(
    corners: list[tuple[float, float]], z: float
) -> list[tuple[tuple[float, float, float], tuple[float, float, float]]]:
    pts = [(x, y, z) for x, y in corners]
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def _vertical_segments(
    corners: list[tuple[float, float]], z0: float, z1: float
) -> list[tuple[tuple[float, float, float], tuple[float, float, float]]]:
    return [((x, y, z0), (x, y, z1)) for x, y in corners]


def _edge_outward_normal(
    model: RoomAuthoringModel, a: RoomVertex, b: RoomVertex
) -> tuple[float, float]:
    """Outward plan normal of room edge ``a→b`` — same test the compiler uses."""

    dx = b.x_m - a.x_m
    dy = b.y_m - a.y_m
    length = hypot(dx, dy)
    if length <= 1e-9:
        return 0.0, 0.0
    nx, ny = dy / length, -dx / length
    vertices = room_vertices(model.room)
    cx = sum(v.x_m for v in vertices) / len(vertices)
    cy = sum(v.y_m for v in vertices) / len(vertices)
    mx = (a.x_m + b.x_m) * 0.5
    my = (a.y_m + b.y_m) * 0.5
    if nx * (mx - cx) + ny * (my - cy) < 0.0:
        nx, ny = -nx, -ny
    return nx, ny


def _authoring_outline_segments(
    model: RoomAuthoringModel | None,
) -> list[tuple[tuple[str, str], list[tuple[tuple[float, float, float], tuple[float, float, float]]]]]:
    """Selection/highlight outlines for every committed primitive (#976).

    Same shapes the R120 compiler emits — the overlay is a sketch of the
    committed geometry, never a second geometry format.
    """

    if model is None:
        return []
    out: list[tuple[tuple[str, str], list]] = []
    room = model.room
    vertices = room_vertices(room)
    if model.ceiling is not None:
        ring = [
            (
                (a.x_m, a.y_m, ceiling_height_at(model, a.x_m, a.y_m)),
                (b.x_m, b.y_m, ceiling_height_at(model, b.x_m, b.y_m)),
            )
            for a, b in zip(vertices, (*vertices[1:], vertices[0]))
        ]
        out.append((('ceiling', 'ceiling'), ring))
    for spec in model.soffits:
        corners = [
            (spec.min_x_m, spec.min_y_m),
            (spec.max_x_m, spec.min_y_m),
            (spec.max_x_m, spec.max_y_m),
            (spec.min_x_m, spec.max_y_m),
        ]
        # Soffits hang below the local (possibly sloped) ceiling plane.
        bottom = min(
            ceiling_height_at(model, x, y) for x, y in corners
        ) - spec.drop_m
        segments = _rect_ring_segments(corners, bottom) + _vertical_segments(
            corners,
            bottom,
            min(ceiling_height_at(model, x, y) for x, y in corners),
        )
        out.append((('soffit', spec.soffit_id), segments))
    for spec in model.risers:
        corners = [
            (spec.min_x_m, spec.min_y_m),
            (spec.max_x_m, spec.min_y_m),
            (spec.max_x_m, spec.max_y_m),
            (spec.min_x_m, spec.max_y_m),
        ]
        segments = _rect_ring_segments(corners, spec.height_m) + _vertical_segments(
            corners, 0.0, spec.height_m
        )
        out.append((('riser', spec.riser_id), segments))
    for spec in model.partial_walls:
        dx = spec.x2_m - spec.x1_m
        dy = spec.y2_m - spec.y1_m
        length = hypot(dx, dy)
        if length <= 1e-9:
            continue
        nx = -dy / length * (spec.thickness_m * 0.5)
        ny = dx / length * (spec.thickness_m * 0.5)
        corners = [
            (spec.x1_m + nx, spec.y1_m + ny),
            (spec.x2_m + nx, spec.y2_m + ny),
            (spec.x2_m - nx, spec.y2_m - ny),
            (spec.x1_m - nx, spec.y1_m - ny),
        ]
        top = spec.base_height_m + spec.height_m
        segments = (
            _rect_ring_segments(corners, spec.base_height_m)
            + _rect_ring_segments(corners, top)
            + _vertical_segments(corners, spec.base_height_m, top)
        )
        out.append((('partial_wall', spec.wall_id), segments))
    for spec in model.adjacent_regions:
        index = spec.shared_edge_index % len(vertices)
        a = vertices[index]
        b = vertices[(index + 1) % len(vertices)]
        nx, ny = _edge_outward_normal(model, a, b)
        o1 = (a.x_m + nx * spec.outward_depth_m, a.y_m + ny * spec.outward_depth_m)
        o2 = (b.x_m + nx * spec.outward_depth_m, b.y_m + ny * spec.outward_depth_m)
        corners = [(a.x_m, a.y_m), (b.x_m, b.y_m), o2, o1]
        region_height = spec.ceiling_height_m or room.height_m
        offset_m, width_m, opening_height_m = spec.opening
        edge_length = hypot(b.x_m - a.x_m, b.y_m - a.y_m)
        ux = (b.x_m - a.x_m) / edge_length if edge_length > 1e-9 else 0.0
        uy = (b.y_m - a.y_m) / edge_length if edge_length > 1e-9 else 0.0
        p0 = (a.x_m + ux * offset_m, a.y_m + uy * offset_m)
        p1 = (a.x_m + ux * (offset_m + width_m), a.y_m + uy * (offset_m + width_m))
        segments = (
            _rect_ring_segments(corners, 0.0)
            + _rect_ring_segments(corners, region_height)
            + _vertical_segments(corners, 0.0, region_height)
            # portal span on the shared edge — where room and region connect
            + [
                ((p0[0], p0[1], 0.0), (p0[0], p0[1], opening_height_m)),
                ((p1[0], p1[1], 0.0), (p1[0], p1[1], opening_height_m)),
                ((p0[0], p0[1], opening_height_m), (p1[0], p1[1], opening_height_m)),
            ]
        )
        out.append((('adjacent_region', spec.region_id), segments))
    return out


__all__ = ["RoomGeometryInputController"]
