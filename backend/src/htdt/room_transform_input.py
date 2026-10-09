from __future__ import annotations

from math import radians, tan
from typing import Callable

import numpy as np
from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent, QMouseEvent

from .cad_display_units import (
    LengthDisplayPolicy,
    display_length_policy,
    display_to_si,
    format_length_m,
)
from .cad_input import CadAxis
from .cad_scene import (
    Position3,
    domain_to_render,
    render_delta_to_domain,
    rotate_orientation_world,
)
from .cad_snap import (
    AxisName,
    SnapSelection,
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

#: Keys that re-aim the delta axis — valid both during a pointer drag and
#: inside numeric entry (#979).
_AXIS_KEYS = {
    Qt.Key.Key_X: CadAxis.X,
    Qt.Key.Key_Y: CadAxis.Y,
    Qt.Key.Key_Z: CadAxis.Z,
}

#: Characters that may appear in a numeric-delta buffer (#979). Kept ASCII
#: so entry is layout-independent; keypad digits produce the same text.
_NUMERIC_ENTRY_CHARS = frozenset('0123456789.-')


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
        self._drag_entity_id: str | None = None
        self._group_ids: tuple[str, ...] = ()
        self._group_base_positions: dict[str, Position3] = {}
        self._group_pivot: Position3 | None = None
        self._snap_selectors: dict[AxisName, SnapSelector] = {}
        #: Cursor-side HUD state (#979): last pointer position in interactor
        #: DIP (fallback anchor), the snap candidate's world anchor, and the
        #: current preview target/rotation used for the Δ readout.
        self._last_pointer: QPointF | None = None
        self._snap_anchor_world: Position3 | None = None
        self._preview_target: Position3 | None = None
        self._preview_rotate_value: tuple[AxisName, float] | None = None
        #: Numeric-delta entry (#979): once digits start during an armed
        #: gesture the typed value owns the preview until Enter/Esc, so a
        #: stray pointer move cannot overwrite it.
        self._numeric_entry = False
        self._numeric_buffer = ''
        #: #496 display policy for HUD text and numeric parsing; internal
        #: authority stays SI metres either way.
        self._length_policy: LengthDisplayPolicy = display_length_policy('m')
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

    def set_length_policy(self, policy: LengthDisplayPolicy) -> None:
        """Live #496 display-unit binding for HUD text and numeric parsing.

        Wired via ``bind_length_policy_widget`` where a preferences object
        exists; the default metre policy covers bare workspaces. Authority
        stays SI metres either way.
        """

        self._length_policy = policy

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
        # An armed gizmo owns the pointer's hit stack — close and suppress
        # the overlapping-pick chooser for the gesture's duration (#983).
        dismiss = getattr(self.viewport, 'dismiss_pick_candidates', None)
        if callable(dismiss):
            dismiss()
        if hasattr(self.viewport, 'pick_popover_enabled'):
            self.viewport.pick_popover_enabled = False
        self.mode = mode
        self.axis = None
        self._dragging = False
        self._start_pointer = None
        self._last_pointer = None
        self._snap_anchor_world = None
        self._preview_target = None
        self._preview_rotate_value = None
        self._numeric_entry = False
        self._numeric_buffer = ''
        self._snap_selectors = {}
        group_hint = f" · {len(targets)}項目" if len(targets) > 1 else ""
        self.workspace._set_status(
            (
                "移動: ドラッグして配置 · X/Y/Zで軸拘束 · 0-9で数値入力 · Shiftでスナップ一時解除 · Escで中止"
                if mode == "move"
                else "回転: ドラッグして回転 · X/Y/Zで回転軸 · 0-9で角度入力 · Shiftでスナップ一時解除 · Escで中止"
            )
            + group_hint
        )

    def set_axis(self, axis: CadAxis) -> None:
        if self.mode is None:
            return
        self.axis = axis
        self.workspace.active_axis_constraint = axis.value
        self.workspace._set_status(f"{axis.value.upper()}軸に拘束")
        # Retarget the live preview immediately (#979): typed input re-aims
        # its delta axis, a held drag re-snaps onto the constraint instead
        # of waiting for the next pointer move.
        if self._numeric_entry:
            self._apply_numeric_preview()
        elif self._dragging and self._start_pointer is not None and self._last_pointer is not None:
            self.drag_to(self._last_pointer)

    def _snap_enabled(self) -> bool:
        modifiers = QGuiApplication.keyboardModifiers()
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            return False  # temporary snap disable during the drag (#481)
        return True

    def begin_at(self, position: QPointF) -> bool:
        if self.mode is None or self._dragging:
            return False
        if not self._begin_gesture():
            return False
        self._start_pointer = QPointF(position)
        self._last_pointer = QPointF(position)
        return True

    def _begin_gesture(self) -> bool:
        """Shared preview-open for pointer drags and numeric entry (#979).

        Both input styles own the same working-document preview: whichever
        supplies the value (pointer delta or a typed number) drives
        ``preview_move``/``preview_rotate`` while Enter and the mouse
        release funnel into the identical ``commit_preview`` path — the
        existing command port, so the two styles can never double-commit.
        """

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
        self._drag_entity_id = entity_id
        self._dragging = True
        return True

    def drag_to(self, position: QPointF) -> bool:
        if not self._dragging or self.mode is None:
            return False
        self._last_pointer = QPointF(position)
        if self._numeric_entry:
            # Typed input owns the delta until Enter/Esc — the pointer must
            # not overwrite a value the user is mid-way through (#979).
            return True
        if self._start_pointer is None:
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
                if hasattr(self.viewport, 'pick_popover_enabled'):
                    self.viewport.pick_popover_enabled = True
                self.workspace.active_axis_constraint = None
                self.workspace.set_snap_feedback(None)
                return True
            return False
        if position is not None:
            if self._numeric_entry:
                self._apply_numeric_preview()
            else:
                self.drag_to(position)
        elif self._numeric_entry:
            # A mouse release during entry commits what was typed, not
            # where the pointer happens to be.
            self._apply_numeric_preview()
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
        edited_ids = set(self._group_ids) or (
            {self._drag_entity_id} if self._drag_entity_id else set()
        )
        self._reset_state()
        if changed:
            notes = self.workspace.controller.propagate_constraints(
                edited_ids, merge_with_previous=True
            )
            self._controller._sync_recovery()
        else:
            notes = ()
        self.workspace.refresh()
        if notes:
            self.workspace._set_status(" / ".join(notes))
        else:
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
        self._last_pointer = None
        self._snap_anchor_world = None
        self._preview_target = None
        self._preview_rotate_value = None
        self._numeric_entry = False
        self._numeric_buffer = ''
        self._base_position = None
        self._base_orientation = None
        self._drag_entity_id = None
        self._group_ids = ()
        self._group_base_positions = {}
        self._group_pivot = None
        self._snap_selectors = {}
        if hasattr(self.viewport, 'pick_popover_enabled'):
            self.viewport.pick_popover_enabled = True
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

    def _project_to_screen(self, position: Position3) -> tuple[float, float] | None:
        """Project a domain point to interactor DIP coordinates.

        The snap selector's acquire/retain radii and the HUD anchor both
        live in Qt widget DIP space, so this deliberately returns DIP —
        not VTK display pixels, which differ by the device pixel ratio
        (200 % DPI halved the effective snap radius before). ``None`` when
        the point cannot be projected; callers fall back to the pointer or
        the lower-left label.
        """

        world = domain_to_render(position)
        to_widget = getattr(self.viewport, 'world_to_widget_position', None)
        if callable(to_widget):
            point = to_widget(world)
            if point is None:
                return None
            return (float(point.x()), float(point.y()))
        try:
            display = self.viewport.world_to_screen(world)
        except Exception:
            return None
        return (float(display[0]), float(display[1]))

    def _screen_project_or_far(self, position: Position3) -> tuple[float, float]:
        # An unprojectable point (behind the camera, degenerate view) can
        # never be acquired — park it infinitely far away instead of
        # failing the whole snap pass.
        projected = self._project_to_screen(position)
        if projected is None:
            return (1e9, 1e9)
        return projected

    def _apply_object_snap(
        self,
        candidate: Position3,
        *,
        grid_snapped: bool,
    ) -> tuple[Position3, SnapSelection | None]:
        view_state = self._controller.view_state
        if (
            not view_state.object_snap_enabled
            or not self._snap_enabled()
            or not (
                hasattr(self.viewport, "world_to_widget_position")
                or hasattr(self.viewport, "world_to_screen")
            )
        ):
            return candidate, None
        document = self._controller.document
        exclude_ids = set(self._group_ids or ())
        if self._controller.selected_id is not None:
            exclude_ids.add(self._controller.selected_id)
        last_selection: SnapSelection | None = None
        for axis in self._free_axes():
            candidates = generate_snap_candidates(
                document,
                exclude_ids=exclude_ids,
                axis=axis,
                probe=candidate,
            )
            selector = self._snap_selectors.setdefault(axis, SnapSelector())
            selection = selector.select(candidates, candidate, self._screen_project_or_far)
            if selection is None:
                continue
            target = selection.candidate.target
            candidate = Position3(
                x_m=target.x_m if axis == 'x' else candidate.x_m,
                y_m=target.y_m if axis == 'y' else candidate.y_m,
                z_m=target.z_m if axis == 'z' else candidate.z_m,
            )
            last_selection = selection
        return candidate, last_selection

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
        candidate, snap_selection = self._apply_object_snap(candidate, grid_snapped=grid_hit)
        view_state = self._controller.view_state
        self._preview_target = candidate
        self._preview_rotate_value = None
        self._snap_anchor_world = (
            snap_selection.candidate.screen_anchor if snap_selection is not None else None
        )
        if snap_selection is not None:
            snap_line = self._snap_candidate_line(snap_selection)
            status_label = f"スナップ: {snap_selection.candidate.label}"
        elif grid_hit:
            snap_line = f"グリッドスナップ {format_length_m(view_state.grid_step_m, self._length_policy)}"
            status_label = f"グリッド {view_state.grid_step_m:.3f} m"
        elif not self._snap_enabled() and (
            view_state.object_snap_enabled or view_state.grid_snap_enabled
        ):
            snap_line = "スナップ一時解除中 (Shift)"
            status_label = "スナップ一時解除中"
        elif not view_state.object_snap_enabled and not view_state.grid_snap_enabled:
            snap_line = "グリッド・オブジェクトスナップ: オフ"
            status_label = None
        else:
            snap_line = "スナップなし (取得範囲外)"
            status_label = None
        self._push_feedback(snap_line=snap_line, status_label=status_label)

        if self._group_ids:
            delta_vector = (
                candidate.x_m - self._base_position.x_m,
                candidate.y_m - self._base_position.y_m,
                candidate.z_m - self._base_position.z_m,
            )
            self._controller.working.preview_group_move(delta_vector)
        else:
            self._controller.working.preview_move(candidate)

    def _fmt_signed(self, value_m: float) -> str:
        text = format_length_m(value_m, self._length_policy)
        return text if value_m < 0 else f'+{text}'

    def _snap_candidate_line(self, selection: SnapSelection) -> str:
        candidate = selection.candidate
        kind = _SNAP_KIND_LABELS.get(candidate.kind, candidate.kind)
        try:
            name = self._controller.document.entity(candidate.entity_id).name
        except (KeyError, RuntimeError):
            name = candidate.entity_id
        axis_value = {
            'x': candidate.target.x_m,
            'y': candidate.target.y_m,
            'z': candidate.target.z_m,
        }[candidate.axis]
        return (
            f"{kind} · {name} ({candidate.entity_id}) · "
            f"{candidate.axis.upper()}={format_length_m(axis_value, self._length_policy)} · "
            f"{selection.distance_dip:.0f} px"
        )

    def _mode_line(self) -> str:
        parts = ['移動' if self.mode == 'move' else '回転']
        if self.mode == 'move':
            if self.axis is not None:
                parts.append(f'{self.axis.value.upper()}軸拘束')
        else:
            axis = self.axis.value if self.axis is not None else 'z'
            parts.append(f'{axis.upper()}軸')
        if len(self._group_ids) > 1:
            parts.append(f'{len(self._group_ids)}項目')
        if self._numeric_entry:
            parts.append('数値入力')
        return ' · '.join(parts)

    def _delta_line(self) -> str | None:
        if self.mode == 'move':
            if self._preview_target is None or self._base_position is None:
                return None
            delta = (
                self._preview_target.x_m - self._base_position.x_m,
                self._preview_target.y_m - self._base_position.y_m,
                self._preview_target.z_m - self._base_position.z_m,
            )
            return (
                f"ΔX {self._fmt_signed(delta[0])} · "
                f"ΔY {self._fmt_signed(delta[1])} · "
                f"ΔZ {self._fmt_signed(delta[2])}"
            )
        if self._preview_rotate_value is None:
            return None
        return f"角度 {self._preview_rotate_value[1]:+.1f}°"

    def _hud_anchor(self) -> tuple[float, float] | None:
        """Interactor-DIP point the HUD should sit beside (#979).

        Object snap anchors on the candidate itself — the exact point the
        value belongs to. Otherwise the live pointer; when neither
        projects (e.g. camera mid-rotation) the caller falls back to the
        renderer's lower-left label.
        """

        if self._snap_anchor_world is not None:
            anchor = self._project_to_screen(self._snap_anchor_world)
            if anchor is not None:
                return anchor
        if self._last_pointer is not None:
            return (float(self._last_pointer.x()), float(self._last_pointer.y()))
        if self._preview_target is not None:
            return self._project_to_screen(self._preview_target)
        if self._base_position is not None:
            return self._project_to_screen(self._base_position)
        return None

    def _push_feedback(
        self,
        *,
        snap_line: str | None,
        status_label: str | None,
    ) -> None:
        """Compose the cursor-side HUD body and forward it (#979).

        ``status_label`` keeps the compact legacy wording for the status
        strip and the lower-left fallback; ``hud_lines`` is the fuller
        cursor-side readout (snap kind · name · coordinate · distance,
        gesture delta, axis/snap state, numeric echo, commit hint).
        """

        lines: list[str] = []
        if snap_line:
            lines.append(snap_line)
        delta_line = self._delta_line()
        if delta_line is not None:
            lines.append(delta_line)
        if self._numeric_entry:
            lines.append(f'入力: {self._numeric_buffer or "0"}')
        lines.append(self._mode_line())
        lines.append('Enter: 確定 · Esc: 中止')
        self.workspace.set_snap_feedback(
            status_label,
            screen_position=self._hud_anchor(),
            hud_lines=tuple(lines),
        )

    def _apply_numeric_preview(self) -> None:
        """Preview the typed delta exactly — snap never rewrites typed input.

        Move: the value is a display-unit delta along the constrained axis
        (X while unconstrained); rotate: degrees about the selected axis
        (Z default, matching pointer drags). The preview flows through the
        same ``working.preview_*`` port as a drag, so commit on Enter is
        indistinguishable from a drag release and cannot double-commit.
        """

        if self.mode is None or self._base_position is None or not self._numeric_entry:
            return
        text = self._numeric_buffer.strip()
        try:
            value = float(text) if text else 0.0
        except ValueError:
            return
        if self.mode == "move":
            try:
                delta_m = display_to_si(value, self._length_policy.unit)
            except ValueError:
                return  # out-of-range display value stays a preview no-op
            axis: AxisName = self.axis.value if self.axis is not None else 'x'
            base = self._base_position
            candidate = Position3(
                x_m=base.x_m + delta_m if axis == 'x' else base.x_m,
                y_m=base.y_m + delta_m if axis == 'y' else base.y_m,
                z_m=base.z_m + delta_m if axis == 'z' else base.z_m,
            )
            self._preview_target = candidate
            self._preview_rotate_value = None
            if self._group_ids:
                delta_vector = (
                    candidate.x_m - base.x_m,
                    candidate.y_m - base.y_m,
                    candidate.z_m - base.z_m,
                )
                self._controller.working.preview_group_move(delta_vector)
            else:
                self._controller.working.preview_move(candidate)
            self._push_feedback(
                snap_line="数値入力 (スナップ適用なし)",
                status_label=None,
            )
        else:
            axis = self.axis.value if self.axis is not None else 'z'
            self._preview_target = None
            self._preview_rotate_value = (axis, value)
            if self._group_ids and self._group_pivot is not None:
                self._controller.working.preview_group_rotate(axis, value, self._group_pivot)
            else:
                orientation = rotate_orientation_world(self._base_orientation, axis, value)
                self._controller.working.preview_rotate(orientation)
            self._push_feedback(
                snap_line="数値入力 (スナップ適用なし)",
                status_label=None,
            )
        self.workspace.refresh()

    def _preview_rotate(self, delta: QPointF) -> None:
        if self._base_orientation is None:
            return
        axis = self.axis.value if self.axis is not None else "z"
        view_state = self._controller.view_state
        raw_deg = float(delta.x()) * 0.5
        if view_state.angle_snap_enabled and self._snap_enabled():
            angle_deg = snap_angle_deg(raw_deg, view_state.angle_step_deg)
            snap_line = f"角度スナップ {view_state.angle_step_deg:g}°"
            status_label = f"角度 {angle_deg:.1f}°"
        else:
            angle_deg = raw_deg
            if not self._snap_enabled() and view_state.angle_snap_enabled:
                snap_line = "スナップ一時解除中 (Shift)"
            elif not view_state.angle_snap_enabled:
                snap_line = "角度スナップ: オフ"
            else:
                snap_line = None
            status_label = None
        self._preview_target = None
        self._preview_rotate_value = (axis, angle_deg)
        self._snap_anchor_world = None
        self._push_feedback(snap_line=snap_line, status_label=status_label)
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

    def nudge_selection(self, dx_m: float, dy_m: float) -> bool:
        """Arrow-key move of the selection by one grid step (round8 key parity
        with vertex nudge in the geometry editor).

        One keypress = one undoable move through the same group-aware
        preview/commit path as a drag, including the hard-constraint gate.
        """

        if self.mode is not None or self._dragging:
            return False
        geometry = self.workspace.geometry_input
        if geometry is not None and geometry.is_active:
            return False
        controller = self._controller
        targets = self._edit_targets()
        if not targets or not controller.can_edit:
            return False
        locked = [
            controller.document.entity(item).name
            for item in targets
            if controller.view_state.is_locked(item)
        ]
        if locked:
            self.workspace._set_status(
                f"選択にロック中の項目が含まれています: {'、'.join(locked)}",
                error=True,
            )
            return False
        working = controller.working
        if working.has_preview:
            working.cancel_preview()
        working.begin_group_move(targets)
        working.preview_group_move((dx_m, dy_m, 0.0))
        gate_message = self.commit_gate(targets) if self.commit_gate is not None else None
        if gate_message is not None:
            working.cancel_preview()
            self.workspace.refresh()
            self.workspace._set_status(f"移動を拒否しました · {gate_message}", error=True)
            return False
        changed = working.commit_preview()
        if changed:
            notes = self.workspace.controller.propagate_constraints(
                set(targets), merge_with_previous=True
            )
            controller._sync_recovery()
        else:
            notes = ()
        self.workspace.refresh()
        if notes:
            self.workspace._set_status(" / ".join(notes))
        elif changed:
            self.workspace._set_status("ナッジしました · 元に戻すで復元できます")
        return changed

    def _modal_key_press(self, event: QKeyEvent) -> bool:
        """Keys while a move/rotate gesture is armed (#979).

        Once digits start, numeric entry owns the gesture: digits, '-' and
        '.' extend the buffer (each edit re-applies the exact typed delta
        through the same preview port as a drag), Backspace corrects, X/Y/Z
        re-aim the axis, Enter commits and Esc cancels — both through the
        working-document port the pointer paths use. Every other key is
        swallowed so edit shortcuts (L/H/Delete/arrows) cannot misfire
        mid-input. Before entry starts, Enter/Esc still commit/cancel the
        armed gesture directly.
        """

        key = event.key()
        if self._numeric_entry:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                return bool(self.finish_at(None))
            if key == Qt.Key.Key_Escape:
                return bool(self.cancel())
            if key == Qt.Key.Key_Backspace:
                if self._numeric_buffer:
                    self._numeric_buffer = self._numeric_buffer[:-1]
                    self._apply_numeric_preview()
                return True
            axis = _AXIS_KEYS.get(key)
            if axis is not None:
                self.set_axis(axis)
                return True
            text = event.text()
            if text and all(char in _NUMERIC_ENTRY_CHARS for char in text):
                self._numeric_buffer += text
                self._apply_numeric_preview()
            return True  # editing context — nothing else may fire
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return bool(self.finish_at(None))
        if key == Qt.Key.Key_Escape:
            return bool(self.cancel())
        text = event.text()
        if text and all(char in _NUMERIC_ENTRY_CHARS for char in text):
            # Starting entry mid-drag takes over the live preview — only
            # open one when the gesture hasn't begun yet, otherwise
            # working.begin_* fires on an open preview (EditStateError).
            if self._dragging or self._begin_gesture():
                self._numeric_entry = True
                self._numeric_buffer = text
                self._apply_numeric_preview()
            return True
        return False

    def _key_press(self, event: QKeyEvent) -> bool:
        if not isinstance(event, QKeyEvent):
            return False
        if event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        ):
            return False
        if self.mode is not None:
            return self._modal_key_press(event)
        step = float(self._controller.view_state.grid_step_m) * (
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
        if self.nudge_selection(*delta):
            event.accept()
            return True
        return False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        viewport = getattr(self, "viewport", None)
        if viewport is None or watched is not viewport.interactor:
            return False
        event_type = event.type()
        if event_type == QEvent.Type.KeyPress:
            return self._key_press(event)
        if event_type == QEvent.Type.ShortcutOverride and self._numeric_entry:
            # Numeric entry is a modal text context: every QShortcut on the
            # workspace (L/Delete/Enter/Esc/…) must be denied here, or edit
            # verbs fire mid-input (#979 誤キーバインド — verified on real
            # GUI where L locked the entity during entry).
            event.accept()
            return True
        if self.mode is None:
            return False
        if event_type == QEvent.Type.MouseButtonPress:
            mouse = event  # type: ignore[assignment]
            if isinstance(mouse, QMouseEvent) and mouse.button() == Qt.MouseButton.LeftButton:
                if self._numeric_entry:
                    # Click commits what was typed — and the press must not
                    # fall through to the picker mid-input (#979).
                    mouse.accept()
                    return True
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
