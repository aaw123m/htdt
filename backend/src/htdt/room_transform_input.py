from __future__ import annotations

from math import radians, tan
from typing import Callable

import numpy as np
from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QMouseEvent

from .cad_input import CadAxis
from .cad_scene import (
    Position3,
    domain_to_render,
    render_delta_to_domain,
    rotate_orientation_world,
)
from .cad_snap import (
    AxisName,
    SnapSelector,
    generate_snap_candidates,
    snap_angle_deg,
    snap_position_axis,
)
from .room_viewport import RoomViewport3D
from .room_workspace import RoomWorkspace


_SNAP_KIND_LABELS = {
    'vertex': '頂点',
    'midpoint': '中点',
    'edge': '辺',
    'alignment': '整列',
}


class RoomEntityTransformController(QObject):
    """Transient M/R direct-manipulation bridge for the UX120 Room workspace.

    The controller calculates viewport deltas only. Preview/commit/cancel are
    delegated to the existing TheaterWorkingDocument, preserving Undo/recovery and
    SceneRevision authority.

    N20b restoration (#480/#481): the controller is group-aware — when several
    entities are selected it drives ``begin_group_move``/``begin_group_rotate``
    so one gesture is one Undo step and relative geometry is preserved. Snap
    settings come from ``EditorViewState``: grid/angle stepping and
    vertex/midpoint/edge/alignment object snap (screen-space hysteresis via
    ``SnapSelector``); holding Shift during a drag disables snap temporarily.
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
        self._group_ids: tuple[str, ...] = ()
        self._group_base_positions: dict[str, Position3] = {}
        self._group_pivot: Position3 | None = None
        self._snap_selectors: dict[AxisName, SnapSelector] = {}
        #: Optional hard-constraint gate: called with moved entity ids before
        #: commit; returns a Japanese reason string to reject, or None.
        self.commit_gate: Callable[[tuple[str, ...]], str | None] | None = None
        viewport.interactor.installEventFilter(self)

    @property
    def is_active(self) -> bool:
        return self.mode is not None or self._dragging

    @property
    def _controller(self):
        return self.workspace.controller

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

    def _edit_targets(self) -> tuple[str, ...]:
        controller = self._controller
        selection = controller.view_state.selection or (
            () if controller.selected_id is None else (controller.selected_id,)
        )
        known = {entity.entity_id for entity in controller.document.entities}
        return tuple(entity_id for entity_id in selection if entity_id in known)

    def _arm(self, mode: str) -> None:
        entity_id = self._controller.selected_id
        if entity_id is None:
            self.workspace._set_status("移動または回転する項目を選択してください", error=True)
            return
        if not self._controller.can_edit:
            self.workspace._set_status("現在の状態では選択項目を編集できません", error=True)
            return
        targets = self._edit_targets()
        locked = [
            self._controller.document.entity(item).name
            for item in targets
            if self._controller.view_state.is_locked(item)
        ]
        if locked:
            # Group edits reject the whole gesture when any member is locked —
            # the explicit N20b locked rule (#480/#482).
            self.workspace._set_status(
                f"選択にロック中の項目が含まれています: {'、'.join(locked)}", error=True
            )
            return
        if self.workspace.geometry_input is not None and self.workspace.geometry_input.is_active:
            self.workspace._set_status("部屋形状の編集を終了してから項目を操作してください", error=True)
            return
        if self._controller.working.has_preview:
            self._controller.working.cancel_preview()
        self.mode = mode
        self.axis = None
        self._dragging = False
        self._start_pointer = None
        self._snap_selectors = {}
        group_hint = f" · {len(targets)}項目" if len(targets) > 1 else ""
        self.workspace._set_status(
            (
                "移動: ドラッグして配置 · X/Y/Zで軸拘束 · Shiftでスナップ一時解除 · Escで中止"
                if mode == "move"
                else "回転: ドラッグして回転 · X/Y/Zで回転軸 · Shiftでスナップ一時解除 · Escで中止"
            )
            + group_hint
        )

    def set_axis(self, axis: CadAxis) -> None:
        if self.mode is None:
            return
        self.axis = axis
        self.workspace.active_axis_constraint = axis.value
        self.workspace._set_status(f"{axis.value.upper()}軸に拘束")

    def _snap_enabled(self) -> bool:
        modifiers = QGuiApplication.keyboardModifiers()
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            return False  # temporary snap disable during the drag (#481)
        return True

    def begin_at(self, position: QPointF) -> bool:
        if self.mode is None or self._dragging:
            return False
        entity_id = self._controller.selected_id
        if entity_id is None:
            self.cancel()
            return False
        document = self._controller.document
        entity = document.entity(entity_id)
        targets = self._edit_targets()
        if entity_id not in targets:
            targets = (entity_id,)
        if len(targets) > 1:
            self._group_ids = targets
            self._group_base_positions = {
                item: document.entity(item).position for item in targets
            }
            self._group_pivot = entity.position
            if self.mode == "move":
                self._controller.working.begin_group_move(targets)
                self._base_position = entity.position
                self._base_orientation = None
            else:
                self._controller.working.begin_group_rotate(targets)
                self._base_orientation = entity.orientation
                self._base_position = entity.position
        else:
            self._group_ids = ()
            self._group_base_positions = {}
            self._group_pivot = None
            if self.mode == "move":
                self._controller.working.begin_move(entity_id)
                self._base_position = entity.position
                self._base_orientation = None
            else:
                self._controller.working.begin_rotate(entity_id)
                self._base_orientation = entity.orientation
                self._base_position = entity.position
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
                self.workspace.set_snap_feedback(None)
                return True
            return False
        if position is not None:
            self.drag_to(position)
        working = self._controller.working
        gate_message: str | None = None
        if self.commit_gate is not None and working.preview_kind == 'move':
            moved_ids = self._group_ids or (
                () if self._controller.selected_id is None else (self._controller.selected_id,)
            )
            gate_message = self.commit_gate(moved_ids)
        if gate_message is not None:
            working.cancel_preview()
            self._reset_state()
            self.workspace.refresh()
            self.workspace._set_status(f"移動を拒否しました · {gate_message}", error=True)
            return False
        changed = working.commit_preview()
        self._reset_state()
        if changed:
            self._controller._sync_recovery()
        self.workspace.refresh()
        self.workspace._set_status("操作を確定しました" if changed else "位置・回転は変更されませんでした")
        return changed

    def commit(self) -> bool:
        return self.finish_at(None)

    def cancel(self) -> bool:
        was_active = self.is_active
        if self._controller.working.has_preview:
            self._controller.working.cancel_preview()
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
        self._group_ids = ()
        self._group_base_positions = {}
        self._group_pivot = None
        self._snap_selectors = {}
        self.workspace.active_axis_constraint = None
        self.workspace.set_snap_feedback(None)

    def _free_axes(self) -> tuple[AxisName, ...]:
        if self.axis is None:
            return ("x", "y", "z")
        return (self.axis.value,)  # type: ignore[return-value]

    def _apply_grid_snap(self, candidate: Position3) -> tuple[Position3, bool]:
        view_state = self._controller.view_state
        if not view_state.grid_snap_enabled or not self._snap_enabled():
            return candidate, False
        base = self._base_position
        assert base is not None
        snapped = candidate
        for axis in self._free_axes():
            snapped = snap_position_axis(snapped, axis, view_state.grid_step_m)
        return snapped, snapped != candidate

    def _project_to_screen(self, position: Position3) -> tuple[float, float]:
        world = domain_to_render(position)
        return self.viewport.world_to_screen(world)

    def _apply_object_snap(
        self,
        candidate: Position3,
        *,
        grid_snapped: bool,
    ) -> tuple[Position3, str | None]:
        view_state = self._controller.view_state
        if (
            not view_state.object_snap_enabled
            or not self._snap_enabled()
            or not hasattr(self.viewport, "world_to_screen")
        ):
            return candidate, None
        document = self._controller.document
        exclude_ids = set(self._group_ids or ())
        if self._controller.selected_id is not None:
            exclude_ids.add(self._controller.selected_id)
        label: str | None = None
        for axis in self._free_axes():
            candidates = generate_snap_candidates(
                document,
                exclude_ids=exclude_ids,
                axis=axis,
                probe=candidate,
            )
            selector = self._snap_selectors.setdefault(axis, SnapSelector())
            selection = selector.select(candidates, candidate, self._project_to_screen)
            if selection is None:
                continue
            target = selection.candidate.target
            candidate = Position3(
                x_m=target.x_m if axis == 'x' else candidate.x_m,
                y_m=target.y_m if axis == 'y' else candidate.y_m,
                z_m=target.z_m if axis == 'z' else candidate.z_m,
            )
            label = selection.candidate.label
        return candidate, label

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
        # Snap order (#481): axis constraint is already applied, so snapping
        # only ever adjusts free axes; grid snap first (absolute grid), then
        # object snap overrides per-axis against scene features.
        candidate, grid_hit = self._apply_grid_snap(candidate)
        candidate, snap_label = self._apply_object_snap(candidate, grid_snapped=grid_hit)
        view_state = self._controller.view_state
        if snap_label is not None:
            self.workspace.set_snap_feedback(f"スナップ: {snap_label}")
        elif grid_hit:
            self.workspace.set_snap_feedback(f"グリッド {view_state.grid_step_m:.3f} m")
        elif not self._snap_enabled() and (
            view_state.object_snap_enabled or view_state.grid_snap_enabled
        ):
            self.workspace.set_snap_feedback("スナップ一時解除中")
        else:
            self.workspace.set_snap_feedback(None)

        if self._group_ids:
            delta_vector = (
                candidate.x_m - self._base_position.x_m,
                candidate.y_m - self._base_position.y_m,
                candidate.z_m - self._base_position.z_m,
            )
            self._controller.working.preview_group_move(delta_vector)
        else:
            self._controller.working.preview_move(candidate)

    def _preview_rotate(self, delta: QPointF) -> None:
        if self._base_orientation is None:
            return
        axis = self.axis.value if self.axis is not None else "z"
        view_state = self._controller.view_state
        raw_deg = float(delta.x()) * 0.5
        if view_state.angle_snap_enabled and self._snap_enabled():
            angle_deg = snap_angle_deg(raw_deg, view_state.angle_step_deg)
            self.workspace.set_snap_feedback(f"角度 {angle_deg:.1f}°")
        else:
            angle_deg = raw_deg
            self.workspace.set_snap_feedback(None)
        if self._group_ids and self._group_pivot is not None:
            self._controller.working.preview_group_rotate(
                axis,
                angle_deg,
                self._group_pivot,
            )
            return
        orientation = rotate_orientation_world(
            self._base_orientation,
            axis,
            angle_deg,
        )
        self._controller.working.preview_rotate(orientation)

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
