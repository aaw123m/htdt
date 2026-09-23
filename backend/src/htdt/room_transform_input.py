from __future__ import annotations

from math import radians, tan

import numpy as np
from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from .cad_input import CadAxis
from .cad_scene import Position3, render_delta_to_domain, rotate_orientation_world
from .room_viewport import RoomViewport3D
from .room_workspace import RoomWorkspace


class RoomEntityTransformController(QObject):
    """Transient M/R direct-manipulation bridge for the UX120 Room workspace.

    The controller calculates viewport deltas only. Preview/commit/cancel are
    delegated to the existing TheaterWorkingDocument, preserving Undo/recovery and
    SceneRevision authority.
    """

    def __init__(self, workspace: RoomWorkspace, viewport: RoomViewport3D) -> None:
        super().__init__(workspace)
        self.workspace = workspace
        self.viewport = viewport
        self.mode: str | None = None
        self.axis: CadAxis | None = None
        self._dragging = False
        self._start_pointer: QPointF | None = None
        self._base_position: Position3 | None = None
        self._base_orientation = None
        viewport.interactor.installEventFilter(self)

    @property
    def is_active(self) -> bool:
        return self.mode is not None or self._dragging

    def dispose(self) -> None:
        try:
            self.viewport.interactor.removeEventFilter(self)
        except RuntimeError:
            pass
        self.cancel()

    def arm_move(self) -> None:
        self._arm("move")

    def arm_rotate(self) -> None:
        self._arm("rotate")

    def _arm(self, mode: str) -> None:
        entity_id = self.workspace.controller.selected_id
        if entity_id is None:
            self.workspace._set_status("移動または回転する項目を選択してください", error=True)
            return
        if not self.workspace.controller.can_edit:
            self.workspace._set_status("現在の状態では選択項目を編集できません", error=True)
            return
        if self.workspace.controller.view_state.is_locked(entity_id):
            self.workspace._set_status("ロック中の項目は編集できません", error=True)
            return
        if self.workspace.geometry_input is not None and self.workspace.geometry_input.is_active:
            self.workspace._set_status("部屋形状の編集を終了してから項目を操作してください", error=True)
            return
        if self.workspace.controller.working.has_preview:
            self.workspace.controller.working.cancel_preview()
        self.mode = mode
        self.axis = None
        self._dragging = False
        self._start_pointer = None
        self.workspace._set_status(
            "移動: ドラッグして配置 · X/Y/Zで軸拘束 · Escで中止"
            if mode == "move"
            else "回転: ドラッグして回転 · X/Y/Zで回転軸 · Escで中止"
        )

    def set_axis(self, axis: CadAxis) -> None:
        if self.mode is None:
            return
        self.axis = axis
        self.workspace.active_axis_constraint = axis.value
        self.workspace._set_status(f"{axis.value.upper()}軸に拘束")

    def begin_at(self, position: QPointF) -> bool:
        if self.mode is None or self._dragging:
            return False
        entity_id = self.workspace.controller.selected_id
        if entity_id is None:
            self.cancel()
            return False
        entity = self.workspace.controller.document.entity(entity_id)
        if self.mode == "move":
            self.workspace.controller.working.begin_move(entity_id)
            self._base_position = entity.position
            self._base_orientation = None
        else:
            self.workspace.controller.working.begin_rotate(entity_id)
            self._base_orientation = entity.orientation
            self._base_position = None
        self._start_pointer = QPointF(position)
        self._dragging = True
        return True

    def drag_to(self, position: QPointF) -> bool:
        if not self._dragging or self._start_pointer is None or self.mode is None:
            return False
        delta = QPointF(position) - self._start_pointer
        if self.mode == "move":
            self._preview_move(delta)
        else:
            self._preview_rotate(delta)
        self.workspace.refresh()
        return True

    def finish_at(self, position: QPointF | None = None) -> bool:
        if not self._dragging:
            if self.mode is not None:
                self.mode = None
                self.axis = None
                self.workspace.active_axis_constraint = None
                return True
            return False
        if position is not None:
            self.drag_to(position)
        changed = self.workspace.controller.working.commit_preview()
        self._reset_state()
        if changed:
            self.workspace.controller._sync_recovery()
        self.workspace.refresh()
        self.workspace._set_status("操作を確定しました" if changed else "位置・回転は変更されませんでした")
        return changed

    def commit(self) -> bool:
        return self.finish_at(None)

    def cancel(self) -> bool:
        was_active = self.is_active
        if self.workspace.controller.working.has_preview:
            self.workspace.controller.working.cancel_preview()
        self._reset_state()
        if was_active:
            self.workspace.refresh()
            self.workspace._set_status("操作を取り消しました")
        return was_active

    def _reset_state(self) -> None:
        self.mode = None
        self.axis = None
        self._dragging = False
        self._start_pointer = None
        self._base_position = None
        self._base_orientation = None
        self.workspace.active_axis_constraint = None

    def _preview_move(self, delta: QPointF) -> None:
        if self._base_position is None:
            return
        camera = self.viewport.plotter.camera
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

        _, height = self.viewport.plotter.render_window.GetSize()
        pixel_height = max(float(height), 1.0)
        if camera.GetParallelProjection():
            world_per_pixel = 2.0 * float(camera.GetParallelScale()) / pixel_height
        else:
            world_per_pixel = (
                2.0
                * distance
                * tan(radians(float(camera.GetViewAngle())) * 0.5)
                / pixel_height
            )
        render_delta = (
            float(delta.x()) * world_per_pixel * right
            - float(delta.y()) * world_per_pixel * up
        )
        candidate = render_delta_to_domain(
            tuple(float(value) for value in render_delta),
            self._base_position,
        )
        if self.axis is not None:
            candidate = Position3(
                x_m=candidate.x_m if self.axis is CadAxis.X else self._base_position.x_m,
                y_m=candidate.y_m if self.axis is CadAxis.Y else self._base_position.y_m,
                z_m=candidate.z_m if self.axis is CadAxis.Z else self._base_position.z_m,
            )
        self.workspace.controller.working.preview_move(candidate)

    def _preview_rotate(self, delta: QPointF) -> None:
        if self._base_orientation is None:
            return
        axis = self.axis.value if self.axis is not None else "z"
        angle_deg = float(delta.x()) * 0.5
        orientation = rotate_orientation_world(
            self._base_orientation,
            axis,
            angle_deg,
        )
        self.workspace.controller.working.preview_rotate(orientation)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        viewport = getattr(self, "viewport", None)
        if viewport is None or watched is not viewport.interactor or self.mode is None:
            return False
        event_type = event.type()
        if event_type == QEvent.Type.MouseButtonPress:
            mouse = event  # type: ignore[assignment]
            if isinstance(mouse, QMouseEvent) and mouse.button() == Qt.MouseButton.LeftButton:
                if self.begin_at(mouse.position()):
                    mouse.accept()
                    return True
        elif event_type == QEvent.Type.MouseMove:
            mouse = event  # type: ignore[assignment]
            if (
                isinstance(mouse, QMouseEvent)
                and self._dragging
                and mouse.buttons() & Qt.MouseButton.LeftButton
            ):
                self.drag_to(mouse.position())
                mouse.accept()
                return True
        elif event_type == QEvent.Type.MouseButtonRelease:
            mouse = event  # type: ignore[assignment]
            if (
                isinstance(mouse, QMouseEvent)
                and mouse.button() == Qt.MouseButton.LeftButton
                and self._dragging
            ):
                self.finish_at(mouse.position())
                mouse.accept()
                return True
        return False


__all__ = ["RoomEntityTransformController"]
