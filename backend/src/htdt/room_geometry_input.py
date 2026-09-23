from __future__ import annotations

from math import hypot
from uuid import uuid4

import numpy as np
import pyvista as pv
from PySide6.QtCore import QEvent, QObject, QPointF, Qt, Signal
from PySide6.QtGui import QMouseEvent

from .cad_scene import RoomPrism, RoomVertex, make_polygon_room, room_vertices
from .cad_wall_models import WallSegment, WallTopology
from .cad_walls import (
    WallTopologyError,
    delete_wall,
    make_wall_topology,
    merge_walls,
    move_wall,
    split_wall,
    wall_length,
)
from .room_viewport import RoomViewport3D
from .room_workspace import RoomWorkspace
from .ui_theme import DARK_THEME


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
            "形状編集中 · 頂点をドラッグ · 辺中央を選択して壁を移動 · Enter で終了"
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
        self.selectionChanged.emit()
        if self.mode == "edit":
            self._render_edit_handles()

    def select_edge(self, edge_index: int | None) -> None:
        room = self.room
        if edge_index is not None:
            if room is None:
                raise ValueError("部屋がありません")
            edge_index %= len(room_vertices(room))
        self.selected_edge_index = edge_index
        self.selected_vertex_id = None
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
        room = self.room
        if room is None or self.selected_edge_index is None:
            return False
        edge_index = self.selected_edge_index % len(room_vertices(room))
        topology = self.topology
        if topology is None:
            vertices = list(room_vertices(room))
            start = vertices[edge_index]
            end = vertices[(edge_index + 1) % len(vertices)]
            inserted = RoomVertex(
                vertex_id=f"room-v-{uuid4().hex[:12]}",
                x_m=(start.x_m + end.x_m) * 0.5,
                y_m=(start.y_m + end.y_m) * 0.5,
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
                self._after_geometry_change("辺の中央に頂点を追加しました")
            return changed

        wall = self._wall_for_edge(room, topology, edge_index)
        length = wall_length(room, wall)
        token = uuid4().hex[:10]
        split_room, split_topology = split_wall(
            room,
            topology,
            wall.wall_id,
            offset_m=length * 0.5,
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
        if index == len(walls) - 1:
            raise WallTopologyError("末尾と先頭の壁結合は現在のUIでは未対応です")
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
                self.workspace._set_status(f"この位置には壁を移動できません · {exc}", error=True)
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
        except (ValueError, WallTopologyError) as exc:
            self.workspace._set_status(f"部屋形状を確定できません: {exc}", error=True)
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
        except (ValueError, WallTopologyError) as exc:
            self.workspace._set_status(f"頂点移動を適用できません: {exc}", error=True)
            self.workspace.refresh()
            self._render_edit_handles()
            return
        self.workspace.refresh()
        self._render_edit_handles()
        if changed:
            self.workspace._set_status("頂点を移動しました · 元に戻す で復元できます")

    def _commit_wall_drag(self) -> None:
        moved_room = self._wall_drag_preview_room
        moved_topology = self._wall_drag_preview_topology
        edge_index = self._wall_drag_edge_index
        self._reset_wall_drag()
        if moved_room is None or moved_topology is None:
            self._render_edit_handles()
            return
        changed = self.workspace.controller.replace_room_topology(
            moved_room,
            moved_topology,
        )
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

    def _view_top(self) -> None:
        plotter = self.viewport.plotter
        plotter.view_xy(negative=True)
        plotter.enable_parallel_projection()
        plotter.reset_camera()
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
        return float(point[0]), -float(point[1])

    def _project(self, vertex: RoomVertex) -> QPointF:
        renderer = self.viewport.plotter.renderer
        renderer.SetWorldPoint(float(vertex.x_m), -float(vertex.y_m), 0.0, 1.0)
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

    def _hit_handle(self, position: QPointF) -> tuple[str, int] | None:
        room = self.room
        if room is None:
            return None
        vertices = tuple(room_vertices(room))
        hits: list[tuple[float, str, int]] = []
        for index, vertex in enumerate(vertices):
            point = self._project(vertex)
            distance = hypot(position.x() - point.x(), position.y() - point.y())
            if distance <= 12.0:
                hits.append((distance, "vertex", index))
        for index, start in enumerate(vertices):
            end = vertices[(index + 1) % len(vertices)]
            midpoint = RoomVertex(
                vertex_id="midpoint",
                x_m=(start.x_m + end.x_m) * 0.5,
                y_m=(start.y_m + end.y_m) * 0.5,
            )
            point = self._project(midpoint)
            distance = hypot(position.x() - point.x(), position.y() - point.y())
            if distance <= 10.0:
                hits.append((distance, "edge", index))
        if not hits:
            return None
        _distance, kind, index = min(hits, key=lambda item: item[0])
        return kind, index

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
            topology = self._wall_drag_preview_topology or self.topology
            if topology is not None:
                try:
                    wall = self._wall_for_edge(
                        make_polygon_room(
                            values,
                            height_m=room.height_m,
                            room_id=room.room_id,
                        ),
                        topology,
                        index,
                    )
                except (ValueError, WallTopologyError):
                    wall = None
                if wall is not None:
                    dx = end.x_m - start.x_m
                    dy = end.y_m - start.y_m
                    length = hypot(dx, dy)
                    if length > 1e-12:
                        ux, uy = dx / length, dy / length
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
                                color=DARK_THEME.viewport.selection_outline.hex,
                                line_width=3,
                                opacity=0.82,
                                pickable=False,
                                name=name,
                                render=False,
                            )
        self.viewport.plotter.render()


__all__ = ["RoomGeometryInputController"]
