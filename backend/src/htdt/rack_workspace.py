"""ラック配置ワークスペース (#1012) — 2D RU elevation + per-device fit surfacing.

The #562 rack-infrastructure authority (``cad_rack_infrastructure``) had no
GUI: ``RackDefinition`` / ``EquipmentPowerProfile`` / ``RackLayout`` records
and :func:`evaluate_rack_fit` existed but nothing let a designer see the
rack, move devices, or commit a revised layout. This module is that surface —
mounted on the room workspace's placement page, the same context that owns
equipment assignment and the installation-authority forms.

Contract properties:

- the main view is a 2D RU elevation (front + rear faces plus a shelf lane
  that stays readable on wall-mounted racks) built only from the persisted
  ``RackDefinition`` and ``RackLayout`` plus the session's declared
  ``EquipmentPowerProfile`` records — every RU slot is a correct 1-based
  integer and undeclared capacity never invents a grid extent;
- per-device ``ru_occupancy`` / ``depth_fit`` / ``clearance_fit`` rows come
  straight from :func:`evaluate_rack_fit`; a conflict highlights *every*
  participant, and an undeclared depth/RU/shelf dimension stays ``UNKNOWN``
  — nothing ever paints a blank panel with invented dimensions;
- moving a device is **drag preview → fit re-evaluation → diff confirm →
  explicit layout update**: the ghost never mutates, the confirm dialog
  shows the before/after fit and position diff, and the only write is a
  new append-only ``cad_rack_layouts`` row via ``save_rack_layout``;
- power-state / heat / circuit-endpoint summaries display only the values
  the stored scenario actually covers (``summarize_load`` /
  ``summarize_heat`` / ``summarize_endpoint_loads``); undeclared devices
  stay listed as unknown and no watts figure is ever presented as an
  electrical/cooling compliance verdict;
- session-scoped context (device profiles, endpoints, assignments,
  scenarios) is injected — via ``set_authority_context`` or a JSON context
  file — and honestly labelled as session data; persisted state is limited
  to the rack definition and layout tables that already exist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_display_units import (
    LengthDisplayPolicy,
    display_length_policy,
    format_length_m,
)
from .cad_feature_authority_repository import CadFeatureAuthorityRepository
from .cad_rack_infrastructure import (
    CircuitAssignment,
    CircuitEndpoint,
    DeviceFitResult,
    ElectricalScenario,
    EquipmentPowerProfile,
    RackDefinition,
    RackLayout,
    RackPlacement,
    evaluate_rack_fit,
    summarize_endpoint_loads,
    summarize_heat,
    summarize_load,
)
from .cad_repository import SceneRepository
from .user_facing_error import operation_error_message
from .ui_theme import (
    ACCENT,
    SCIENTIFIC,
    SEMANTIC,
    SURFACES,
    TEXT,
    VIEWPORT,
    ControlSize,
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_control_size,
    set_primary_action,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)


_FIT_LABELS: dict[str, str] = {
    'PASS': '合格',
    'FAIL': '不合格',
    'UNKNOWN': '不明',
}

_STATE_LABELS: dict[str, str] = {
    'standby_off': 'スタンバイ（オフ）',
    'standby_passthrough': 'スタンバイ（パススルー）',
    'networked_standby': 'ネットワークスタンバイ',
    'idle': 'アイドル',
    'typical': '通常',
    'rated_max': '定格最大',
    'custom': 'カスタム',
}

_KIND_LABELS: dict[str, str] = {
    'manufacturer_declared': 'メーカー宣言',
    'user_measured': 'ユーザー計測',
    'derived': '導出',
}

_ENDPOINT_KIND_LABELS: dict[str, str] = {
    'circuit': '回路',
    'outlet': 'コンセント',
    'pdu_branch': 'PDU分岐',
    'ups_branch': 'UPS分岐',
    'other': 'その他',
}


def _device_span(
    placement: RackPlacement,
    profile: EquipmentPowerProfile | None,
) -> range | None:
    """The device's declared RU span (1-based), or None when undeclared."""
    if placement.ru_position is None or profile is None or profile.ru_height is None:
        return None
    return range(placement.ru_position, placement.ru_position + profile.ru_height)


def _worst_status(result: DeviceFitResult) -> str:
    statuses = (result.ru_occupancy, result.depth_fit, result.clearance_fit)
    if 'FAIL' in statuses:
        return 'FAIL'
    if 'UNKNOWN' in statuses:
        return 'UNKNOWN'
    return 'PASS'


class RackElevationView(QFrame):
    """2D RU elevation — front/rear faces, shelf lane, depth profile band.

    Pure view: the panel hands it the evaluated context and it paints what
    the declared records say — no invented grid extent, no inferred device
    dimensions. Dragging a device block emits ``movePreviewRequested`` with
    the candidate 1-based bottom RU; the view itself never mutates the
    layout.
    """

    movePreviewRequested = Signal(str, int)
    deviceActivated = Signal(str)

    _GUTTER_W = 46
    _FACE_W = 80
    _FACE_GAP = 12
    _HEADER_H = 26
    _ROW_H = 20
    _PAD = 8
    _SHELF_LANE_H = 30
    _DEPTH_LABEL_W = 108
    _DEPTH_ROW_H = 15
    _DEPTH_HEADER_H = 20

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAccessibleName('ラックRU立面図')
        self.setAccessibleDescription(
            '前面・背面のRU段、棚、奥行プロファイルを表示します。'
            '機器ブロックをドラッグすると移動プレビューを要求します。'
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self._rack: RackDefinition | None = None
        self._layout: RackLayout | None = None
        self._devices: dict[str, EquipmentPowerProfile] = {}
        self._results: dict[str, DeviceFitResult] = {}
        self._conflicted: frozenset[str] = frozenset()
        self._selected_device_id: str | None = None
        self._drag_device_id: str | None = None
        self._drag_grab_offset = 0
        self._drag_target_ru: int | None = None
        self._press_pos: QPoint | None = None

    # -- context -----------------------------------------------------------

    def set_scene(
        self,
        rack: RackDefinition | None,
        layout: RackLayout | None,
        devices: Sequence[EquipmentPowerProfile],
        results: Sequence[DeviceFitResult],
    ) -> None:
        self._rack = rack
        self._layout = layout
        self._devices = {item.device_id: item for item in devices}
        self._results = {item.device_id: item for item in results}
        self._conflicted = frozenset(
            item.device_id for item in results if item.conflicts
        )
        if (
            self._selected_device_id is not None
            and self._selected_device_id not in self._layout_device_ids()
        ):
            self._selected_device_id = None
        self._sync_size_hint()
        self.update()

    def _layout_device_ids(self) -> set[str]:
        if self._layout is None:
            return set()
        return {item.device_id for item in self._layout.placements}

    def selected_device_id(self) -> str | None:
        return self._selected_device_id

    def set_selected_device(self, device_id: str | None) -> None:
        if device_id != self._selected_device_id:
            self._selected_device_id = device_id
            self.update()

    def conflicted_device_ids(self) -> frozenset[str]:
        return self._conflicted

    # -- geometry ----------------------------------------------------------

    def _top_row(self) -> int:
        """Highest rendered RU — declared capacity or the highest occupied
        row; undeclared capacity never invents an extent."""
        tops = [1]
        if self._rack is not None and self._rack.ru_capacity is not None:
            tops.append(self._rack.ru_capacity)
        if self._layout is not None:
            for placement in self._layout.placements:
                span = _device_span(
                    placement, self._devices.get(placement.device_id)
                )
                if span is not None:
                    tops.append(span.stop - 1)
                elif placement.ru_position is not None:
                    tops.append(placement.ru_position)
        return max(tops)

    def _capacity_declared(self) -> bool:
        return self._rack is not None and self._rack.ru_capacity is not None

    def displayed_ru_range(self) -> range:
        return range(1, self._top_row() + 1)

    def _face_rect(self, face: int) -> QRect:
        """face 0 = front, 1 = rear."""
        x = self._PAD + self._GUTTER_W + face * (self._FACE_W + self._FACE_GAP)
        return QRect(
            x, self._PAD + self._HEADER_H, self._FACE_W,
            self._top_row() * self._ROW_H,
        )

    def _row_y(self, ru: int) -> int:
        return (
            self._PAD + self._HEADER_H
            + (self._top_row() - ru) * self._ROW_H
        )

    def _row_rect(self, face: int, ru: int) -> QRect:
        face_rect = self._face_rect(face)
        return QRect(
            face_rect.left(), self._row_y(ru), face_rect.width(),
            self._ROW_H,
        )

    def _shelf_placements(self) -> tuple[RackPlacement, ...]:
        if self._layout is None:
            return ()
        return tuple(
            item for item in self._layout.placements
            if item.ru_position is None
        )

    def _shelf_rect(self) -> QRect:
        width = self._GUTTER_W + self._FACE_W * 2 + self._FACE_GAP
        top = self._PAD + self._HEADER_H + self._top_row() * self._ROW_H + 4
        return QRect(self._PAD, top, width, self._SHELF_LANE_H)

    def _depth_top(self) -> int:
        if self._shelf_placements():
            return self._shelf_rect().bottom() + 6
        return (
            self._PAD + self._HEADER_H
            + self._top_row() * self._ROW_H + 6
        )

    def _content_size(self) -> tuple[int, int]:
        width = (
            self._PAD * 2 + self._GUTTER_W
            + self._FACE_W * 2 + self._FACE_GAP
        )
        height = self._PAD * 2 + self._HEADER_H + self._top_row() * self._ROW_H
        if self._layout is None:
            return width, height
        height += self._SHELF_LANE_H + 6 if self._shelf_placements() else 6
        height += (
            self._DEPTH_HEADER_H
            + (len(self._layout.placements) + 1) * self._DEPTH_ROW_H
        )
        return width, height

    def _sync_size_hint(self) -> None:
        width, height = self._content_size()
        self.setMinimumSize(width, height)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        width, height = self._content_size()
        return QSize(width, height)

    # -- block occupancy ---------------------------------------------------

    def _occupancy(self) -> dict[int, list[str]]:
        """RU → device ids whose declared span covers it (placement order)."""
        occupancy: dict[int, list[str]] = {}
        if self._layout is None:
            return occupancy
        for placement in self._layout.placements:
            span = _device_span(
                placement, self._devices.get(placement.device_id)
            )
            if span is None:
                continue
            for ru in span:
                occupancy.setdefault(ru, []).append(placement.device_id)
        return occupancy

    def _block_slot(self, placement: RackPlacement) -> tuple[int, int]:
        """(index, count) slot for a device whose span shares RU rows —
        both parties of a conflict stay visible side by side."""
        span = _device_span(
            placement, self._devices.get(placement.device_id)
        )
        if span is None:
            return (0, 1)
        occupancy = self._occupancy()
        worst_index = 0
        worst_count = 1
        for ru in span:
            occupants = occupancy.get(ru, [])
            if len(occupants) > worst_count and placement.device_id in occupants:
                worst_count = len(occupants)
                worst_index = occupants.index(placement.device_id)
        return (worst_index, worst_count)

    def block_rect(
        self, device_id: str, *, face: int = 0
    ) -> QRect | None:
        """Front/rear face rect for one placed device (test/debug seam)."""
        if self._layout is None:
            return None
        placement = next(
            (p for p in self._layout.placements if p.device_id == device_id),
            None,
        )
        if placement is None or placement.ru_position is None:
            return None
        span = _device_span(placement, self._devices.get(device_id))
        top_ru = placement.ru_position if span is None else span.stop - 1
        bottom_ru = placement.ru_position
        height = (
            self._ROW_H if span is None else len(span) * self._ROW_H
        ) - 2
        slot_index, slot_count = self._block_slot(placement)
        face_rect = self._face_rect(face)
        slot_w = face_rect.width() / slot_count
        if face == 1:
            # Rear view is mirrored left/right, as the standards drawings do.
            x = (
                face_rect.left()
                + (slot_count - 1 - slot_index) * slot_w
            )
        else:
            x = face_rect.left() + slot_index * slot_w
        y = self._row_y(top_ru)
        return QRect(int(x) + 1, y + 1, int(slot_w) - 3, height)

    def device_at(self, pos: QPoint) -> str | None:
        if self._layout is None:
            return None
        for placement in reversed(self._layout.placements):
            rect = self.block_rect(placement.device_id)
            if rect is not None and rect.contains(pos):
                return placement.device_id
        # Shelf lane items.
        shelf = self._shelf_rect()
        if shelf.contains(pos):
            index = (pos.x() - shelf.left() - 4) // 84
            items = self._shelf_placements()
            if 0 <= index < len(items):
                return items[index].device_id
        return None

    def ru_at(self, pos: QPoint) -> int | None:
        """RU row number under a point, or None outside the grid."""
        for face in (0, 1):
            rect = self._face_rect(face)
            if rect.left() <= pos.x() <= rect.right() and rect.top() <= pos.y() <= rect.bottom():
                ru = self._top_row() - (pos.y() - rect.top()) // self._ROW_H
                return int(ru)
        return None

    # -- painting ----------------------------------------------------------

    def _status_color(self, status: str) -> QColor:
        if status == 'FAIL':
            return SEMANTIC.error.qcolor()
        if status == 'UNKNOWN':
            return SEMANTIC.unsupported.qcolor()
        return SEMANTIC.success.qcolor()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), SURFACES.canvas.qcolor())
        try:
            if self._rack is None:
                painter.setPen(TEXT.muted.qcolor())
                painter.drawText(
                    self.rect(), Qt.AlignmentFlag.AlignCenter,
                    'ラック定義が保存されていません'
                )
                return
            self._paint_faces(painter)
            self._paint_shelf(painter)
            self._paint_depth(painter)
            self._paint_ghost(painter)
        finally:
            painter.end()

    def _paint_faces(self, painter: QPainter) -> None:
        assert self._rack is not None
        top = self._top_row()
        header_y = self._PAD + 4
        painter.setPen(TEXT.secondary.qcolor())
        for face, label in ((0, '前面'), (1, '背面')):
            rect = self._face_rect(face)
            painter.drawText(
                QRect(rect.left(), header_y, rect.width(), 16),
                Qt.AlignmentFlag.AlignCenter, label,
            )
        # RU grid + 1-based numbers, bottom row = RU 1.
        grid_pen = QPen(VIEWPORT.grid_major.qcolor(), 1)
        grid_minor = QPen(VIEWPORT.grid_minor.qcolor(), 1)
        for ru in range(1, top + 1):
            y = self._row_y(ru)
            painter.setPen(TEXT.secondary.qcolor())
            painter.drawText(
                QRect(self._PAD, y, self._GUTTER_W - 6, self._ROW_H),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f'RU {ru}',
            )
            for face in (0, 1):
                row = self._row_rect(face, ru)
                painter.setPen(grid_pen if ru == 1 or ru == top else grid_minor)
                painter.drawRect(row)
        if not self._capacity_declared():
            painter.setPen(SEMANTIC.unsupported.qcolor())
            painter.drawText(
                self._face_rect(0).adjusted(0, -18, self._FACE_GAP, 0),
                Qt.AlignmentFlag.AlignCenter, 'RU容量 未宣言',
            )
        if self._layout is None:
            painter.setPen(TEXT.muted.qcolor())
            painter.drawText(
                self._face_rect(0).adjusted(0, 4, self._FACE_W + self._FACE_GAP, -4),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                'レイアウトがまだ保存されていません',
            )
            return
        for placement in self._layout.placements:
            self._paint_device(painter, placement)

    def _paint_device(self, painter: QPainter, placement: RackPlacement) -> None:
        if placement.ru_position is None:
            return  # shelf-only devices live in the shelf lane
        device_id = placement.device_id
        profile = self._devices.get(device_id)
        span = _device_span(placement, profile)
        result = self._results.get(device_id)
        status = 'UNKNOWN' if result is None else _worst_status(result)
        conflicted = device_id in self._conflicted
        for face in (0, 1):
            rect = self.block_rect(device_id, face=face)
            if rect is None:
                continue
            fill = QColor(SURFACES.overlay.hex)
            if conflicted:
                fill = QColor(SEMANTIC.error.hex)
                fill.setAlpha(70)
            elif device_id == self._selected_device_id:
                fill = QColor(ACCENT.selection_fill.hex)
            painter.fillRect(rect, fill)
            edge = self._status_color(status)
            pen = QPen(edge, 2 if conflicted else 1)
            if profile is None or profile.ru_height is None:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(rect)
            if device_id == self._selected_device_id:
                sel = QPen(ACCENT.focus_ring.qcolor(), 1)
                painter.setPen(sel)
                painter.drawRect(rect.adjusted(-2, -2, 2, 2))
            painter.setPen(TEXT.primary.qcolor())
            label = device_id
            if span is None:
                label += '（高さ未宣言）'
            painter.drawText(
                rect.adjusted(2, 0, -2, 0),
                Qt.AlignmentFlag.AlignCenter,
                painter.fontMetrics().elidedText(
                    label, Qt.TextElideMode.ElideRight, rect.width() - 4
                ),
            )

    def _paint_shelf(self, painter: QPainter) -> None:
        items = self._shelf_placements()
        if not items:
            return
        shelf = self._shelf_rect()
        painter.setPen(TEXT.secondary.qcolor())
        painter.drawText(
            QRect(shelf.left(), shelf.top() - 14, 60, 14),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            '棚',
        )
        painter.setPen(QPen(VIEWPORT.grid_major.qcolor(), 1))
        painter.drawRect(shelf)
        for index, placement in enumerate(items):
            rect = QRect(
                shelf.left() + 4 + index * 84,
                shelf.top() + 3, 80, self._SHELF_LANE_H - 6,
            )
            painter.fillRect(rect, QColor(SURFACES.overlay.hex))
            edge = SEMANTIC.unsupported.qcolor()
            result = self._results.get(placement.device_id)
            if result is not None:
                edge = self._status_color(_worst_status(result))
            painter.setPen(QPen(edge, 1))
            painter.drawRect(rect)
            painter.setPen(TEXT.primary.qcolor())
            painter.drawText(
                rect.adjusted(2, 0, -2, 0),
                Qt.AlignmentFlag.AlignCenter,
                painter.fontMetrics().elidedText(
                    f"{placement.device_id}（棚:{placement.shelf_label}）",
                    Qt.TextElideMode.ElideRight, rect.width() - 4,
                ),
            )

    def _paint_depth(self, painter: QPainter) -> None:
        if self._layout is None or self._rack is None:
            return
        top = self._depth_top()
        painter.setPen(TEXT.secondary.qcolor())
        painter.drawText(
            QRect(self._PAD, top, 200, 16),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            '奥行き（宣言値のみ）',
        )
        declared = [
            self._devices[p.device_id].chassis_depth_m
            for p in self._layout.placements
            if p.device_id in self._devices
            and self._devices[p.device_id].chassis_depth_m is not None
        ]
        scale_max = max(
            [self._rack.usable_depth_m, *declared], default=1.0
        ) * 1.05
        bar_x = self._PAD + self._DEPTH_LABEL_W
        bar_w = max(
            self.rect().width() - bar_x - self._PAD - 44, 60
        )
        y = top + self._DEPTH_HEADER_H
        # Rack usable depth is the reference bar.
        usable_w = int(bar_w * self._rack.usable_depth_m / scale_max)
        painter.fillRect(
            QRect(bar_x, y, usable_w, 10), VIEWPORT.grid_major.qcolor()
        )
        painter.setPen(TEXT.secondary.qcolor())
        painter.drawText(
            QRect(self._PAD, y - 1, self._DEPTH_LABEL_W - 6, 14),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f'ラック {format_length_m(self._rack.usable_depth_m, _M_POLICY)}',
        )
        y += self._DEPTH_ROW_H
        for placement in self._layout.placements:
            profile = self._devices.get(placement.device_id)
            depth = None if profile is None else profile.chassis_depth_m
            painter.setPen(TEXT.primary.qcolor())
            painter.drawText(
                QRect(self._PAD, y - 1, self._DEPTH_LABEL_W - 6, 14),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                painter.fontMetrics().elidedText(
                    placement.device_id, Qt.TextElideMode.ElideRight,
                    self._DEPTH_LABEL_W - 10,
                ),
            )
            if depth is None:
                painter.setPen(SEMANTIC.unsupported.qcolor())
                painter.drawText(
                    QRect(bar_x, y - 1, bar_w + 40, 14),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    '未宣言',
                )
            else:
                over = depth > self._rack.usable_depth_m
                w = int(bar_w * depth / scale_max)
                painter.fillRect(
                    QRect(bar_x, y, min(w, bar_w), 10),
                    SEMANTIC.error.qcolor() if over
                    else SCIENTIFIC.measured.qcolor(),
                )
                painter.setPen(
                    SEMANTIC.error.qcolor() if over else TEXT.secondary.qcolor()
                )
                painter.drawText(
                    QRect(bar_x + min(w, bar_w) + 4, y - 1, 120, 14),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    format_length_m(depth, _M_POLICY)
                    + (' 超過' if over else ''),
                )
            y += self._DEPTH_ROW_H

    def _paint_ghost(self, painter: QPainter) -> None:
        if self._drag_device_id is None or self._drag_target_ru is None:
            return
        profile = self._devices.get(self._drag_device_id)
        height = self._ROW_H
        if profile is not None and profile.ru_height is not None:
            height = profile.ru_height * self._ROW_H
        top_ru = self._drag_target_ru + (height // self._ROW_H) - 1
        face = self._face_rect(0)
        ghost = QRect(
            face.left() + 2, self._row_y(top_ru) + 1,
            face.width() - 4, height - 2,
        )
        fill = QColor(ACCENT.primary.hex)
        fill.setAlpha(80)
        painter.fillRect(ghost, fill)
        painter.setPen(QPen(ACCENT.primary.qcolor(), 1, Qt.PenStyle.DashLine))
        painter.drawRect(ghost)

    # -- mouse / keyboard --------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        device_id = self.device_at(event.position().toPoint())
        self._press_pos = event.position().toPoint()
        if device_id is None:
            super().mousePressEvent(event)
            return
        self._selected_device_id = device_id
        self.deviceActivated.emit(device_id)
        ru = self.ru_at(self._press_pos)
        placement = self._placement(device_id)
        if placement is not None and ru is not None:
            self._drag_device_id = device_id
            self._drag_grab_offset = ru - (placement.ru_position or 1)
            self._drag_target_ru = placement.ru_position
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_device_id is not None:
            ru = self.ru_at(event.position().toPoint())
            if ru is not None:
                candidate = max(1, ru - self._drag_grab_offset)
                if candidate != self._drag_target_ru:
                    self._drag_target_ru = candidate
                    self.update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        device_id = self._drag_device_id
        target = self._drag_target_ru
        press = self._press_pos
        self._drag_device_id = None
        self._drag_target_ru = None
        self._press_pos = None
        self.update()
        if device_id is None:
            return
        placement = self._placement(device_id)
        # A click without a drag is a selection; a real move goes to the
        # preview — never straight to a write.
        moved = press is not None and (
            event.position().toPoint() - press
        ).manhattanLength() > 4
        if moved and target is not None and target != placement.ru_position:
            self.movePreviewRequested.emit(device_id, int(target))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self._drag_device_id is not None:
            self._drag_device_id = None
            self._drag_target_ru = None
            self.update()
            return
        super().keyPressEvent(event)

    def _placement(self, device_id: str) -> RackPlacement | None:
        if self._layout is None:
            return None
        return next(
            (p for p in self._layout.placements if p.device_id == device_id),
            None,
        )

    def cancel_drag(self) -> None:
        """External cancel (e.g. focus loss) — no commit, no ghost."""
        self._drag_device_id = None
        self._drag_target_ru = None
        self._press_pos = None
        self.update()


_M_POLICY = display_length_policy('m')


class RackMoveConfirmDialog(QDialog):
    """Preview→diff→confirm step for a candidate layout update.

    Shows the position diff and the *re-evaluated* fit of the candidate
    layout before anything is written; the commit signal fires only on the
    explicit accept, never from the drag itself.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        device_id: str,
        old_layout: RackLayout,
        new_layout: RackLayout,
        before: Sequence[DeviceFitResult],
        after: Sequence[DeviceFitResult],
    ) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowTitle('レイアウト更新の確認')
        layout = QVBoxLayout(self)

        note = QLabel(
            'ドラッグはプレビューだけです。以下の差分を確認してから'
            '「レイアウトを更新」を押すと、新しい版として保存されます。'
        )
        note.setWordWrap(True)
        set_typography_role(note, TypographyRole.SECONDARY)
        layout.addWidget(note)

        diff_title = QLabel('位置の差分')
        set_typography_role(diff_title, TypographyRole.SECTION_TITLE)
        layout.addWidget(diff_title)
        self.diff_list = QListWidget()
        self.diff_list.setMaximumHeight(120)
        self.diff_list.setAccessibleName('位置差分リスト')
        before_positions = {
            item.device_id: item for item in old_layout.placements
        }
        after_positions = {
            item.device_id: item for item in new_layout.placements
        }
        for did, after_p in sorted(after_positions.items()):
            before_p = before_positions.get(did)
            if before_p == after_p:
                continue
            self.diff_list.addItem(QListWidgetItem(
                f'{did}: {_position_label(before_p)} → {_position_label(after_p)}'
            ))
        if self.diff_list.count() == 0:
            self.diff_list.addItem(QListWidgetItem('差分なし'))
        layout.addWidget(self.diff_list)

        fit_title = QLabel('再評価結果（更新候補）')
        set_typography_role(fit_title, TypographyRole.SECTION_TITLE)
        layout.addWidget(fit_title)
        self.fit_label = QLabel()
        self.fit_label.setWordWrap(True)
        self.fit_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.fit_label)
        after_map = {item.device_id: item for item in after}
        before_map = {item.device_id: item for item in before}
        lines: list[str] = []
        conflict_devices: list[str] = []
        for did, result in sorted(after_map.items()):
            prior = before_map.get(did)
            worst = _worst_status(result)
            tag = _FIT_LABELS[worst]
            change = ''
            if prior is not None and _worst_status(prior) != worst:
                change = f'（{_FIT_LABELS[_worst_status(prior)]} → {tag}）'
            extra = ''
            if result.conflicts:
                extra = ' — 競合: ' + '、'.join(result.conflicts)
            lines.append(
                f'{did}: RU{_FIT_LABELS[result.ru_occupancy]} / '
                f'奥行{_FIT_LABELS[result.depth_fit]} / '
                f'クリアランス{_FIT_LABELS[result.clearance_fit]}'
                f'{change}{extra}'
            )
            if result.conflicts:
                conflict_devices.extend(
                    [did, *(c for c in result.conflicts)]
                )
        self.fit_label.setText('\n'.join(lines) or '評価対象なし')
        conflict_set = sorted(set(conflict_devices))
        self.conflict_label = QLabel(
            '競合が解消されていません: ' + '、'.join(conflict_set)
            if conflict_set else ''
        )
        self.conflict_label.setWordWrap(True)
        set_semantic_state(self.conflict_label, SemanticState.ERROR)
        self.conflict_label.setVisible(bool(conflict_set))
        layout.addWidget(self.conflict_label)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.commit_button = self._buttons.button(
            QDialogButtonBox.StandardButton.Ok
        )
        self.commit_button.setText('レイアウトを更新')
        self.commit_button.setAccessibleName('レイアウトを更新')
        self._buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText('キャンセル')
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)
        # An empty diff never reaches a commit path.
        self.commit_button.setEnabled(self.diff_list.count() > 0 and (
            self.diff_list.item(0).text() != '差分なし'
        ))


def _position_label(placement: RackPlacement | None) -> str:
    if placement is None:
        return '未配置'
    parts: list[str] = []
    if placement.ru_position is not None:
        parts.append(f'RU {placement.ru_position}')
    if placement.shelf_label is not None:
        parts.append(f'棚:{placement.shelf_label}')
    return ' + '.join(parts) if parts else '未配置'


class _LoadBarWidget(QWidget):
    """Stacked per-device load bar — arithmetic over declared W only."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAccessibleName('機器別負荷バー')
        self.setMinimumHeight(30)
        self._segments: list[tuple[str, float]] = []
        self._total = 0.0

    def set_segments(self, segments: Sequence[tuple[str, float]]) -> None:
        self._segments = [(label, w) for label, w in segments if w > 0.0]
        self._total = sum(w for _, w in self._segments)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), SURFACES.base.qcolor())
            if not self._segments or self._total <= 0:
                painter.setPen(TEXT.muted.qcolor())
                painter.drawText(
                    self.rect(), Qt.AlignmentFlag.AlignCenter,
                    '集計可能な宣言値がありません',
                )
                return
            x = 0.0
            width = self.width()
            for index, (_label, watts) in enumerate(self._segments):
                share = watts / self._total
                seg_w = max(1, int(share * width))
                color = SCIENTIFIC.channels[
                    index % len(SCIENTIFIC.channels)
                ].qcolor()
                painter.fillRect(
                    QRect(x, 4, seg_w, self.height() - 8), color
                )
                x += seg_w
        finally:
            painter.end()


class RackWorkspacePanel(QFrame):
    """ラック配置ワークスペース — placement ページのラック権威サーフェス."""

    layoutCommitted = Signal(str)

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
        *,
        feature_repository: CadFeatureAuthorityRepository | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.feature_repository = (
            feature_repository
            if feature_repository is not None
            else CadFeatureAuthorityRepository(scene_repository)
        )
        self._length_policy = display_length_policy('m')
        self._racks: dict[str, RackDefinition] = {}
        self._layouts: list[RackLayout] = []
        self._layout: RackLayout | None = None
        self._devices: tuple[EquipmentPowerProfile, ...] = ()
        self._endpoints: tuple[CircuitEndpoint, ...] = ()
        self._assignments: tuple[CircuitAssignment, ...] = ()
        self._scenarios: tuple[ElectricalScenario, ...] = ()
        self._results: tuple[DeviceFitResult, ...] = ()
        self._context_source: str | None = None
        self._syncing = False
        self._build_ui()
        self.refresh()

    # -- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel('ラック配置ワークスペース')
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        note = QLabel(
            'ラックのRU立面（前面・背面）と棚を表示し、'
            '機器ごとのRU占有・奥行・クリアランス適合を確認します。'
            '移動はドラッグ → プレビュー → 差分確認 → 明示的な更新で行い、'
            'この画面は強度・冷却・電気工事の適合を認定しません。'
        )
        set_typography_role(note, TypographyRole.SECONDARY)
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        form.setSpacing(6)
        self.rack_combo = QComboBox()
        self.rack_combo.setAccessibleName('ラック定義')
        self.rack_combo.currentIndexChanged.connect(self._rack_changed)
        form.addRow('ラック', self.rack_combo)
        layout.addLayout(form)

        self.rack_info_label = QLabel('')
        self.rack_info_label.setWordWrap(True)
        set_typography_role(self.rack_info_label, TypographyRole.SECONDARY)
        layout.addWidget(self.rack_info_label)

        self.elevation = RackElevationView()
        self.elevation.movePreviewRequested.connect(self._preview_move)
        self.elevation.deviceActivated.connect(self._device_selected)
        self.elevation_scroll = QScrollArea()
        self.elevation_scroll.setWidgetResizable(False)
        self.elevation_scroll.setWidget(self.elevation)
        self.elevation_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.elevation_scroll.setMinimumHeight(220)
        self.elevation_scroll.setAccessibleName('ラック立面図スクロール')
        layout.addWidget(self.elevation_scroll)

        fit_title = QLabel('機器ごとの適合')
        set_typography_role(fit_title, TypographyRole.SECTION_TITLE)
        layout.addWidget(fit_title)
        self.fit_tree = QTreeWidget()
        self.fit_tree.setAccessibleName('機器適合一覧')
        self.fit_tree.setColumnCount(6)
        self.fit_tree.setHeaderLabels(
            ['機器', '位置', 'RU占有', '奥行', 'クリアランス', '競合 / 理由']
        )
        self.fit_tree.setRootIsDecorated(False)
        self.fit_tree.setMaximumHeight(160)
        self.fit_tree.currentItemChanged.connect(self._fit_row_changed)
        layout.addWidget(self.fit_tree)

        move_row = QHBoxLayout()
        self.move_device_combo = QComboBox()
        self.move_device_combo.setAccessibleName('移動する機器')
        # #1012: keep the controls usable inside the narrow placement
        # column — unconstrained they squeeze to unusable slivers ~360px.
        self.move_device_combo.setMinimumWidth(96)
        self.move_device_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        move_row.addWidget(self.move_device_combo)
        self.move_ru_spin = QSpinBox()
        self.move_ru_spin.setAccessibleName('移動先の下端RU')
        self.move_ru_spin.setMinimum(1)
        self.move_ru_spin.setMaximum(999)
        self.move_ru_spin.setMinimumWidth(72)
        move_row.addWidget(self.move_ru_spin)
        self.move_preview_button = QPushButton('移動プレビュー')
        self.move_preview_button.setAccessibleName('移動プレビュー')
        set_control_size(self.move_preview_button, ControlSize.COMPACT)
        self.move_preview_button.clicked.connect(self._preview_from_form)
        move_row.addWidget(self.move_preview_button)
        move_row.addStretch(1)
        layout.addLayout(move_row)

        summary_title = QLabel('電力・発熱・回路（宣言値の算術集計）')
        set_typography_role(summary_title, TypographyRole.SECTION_TITLE)
        layout.addWidget(summary_title)
        self.summary_note = QLabel(
            '数値は記録された宣言/計測/導出値だけの集計です。'
            '排熱経路・ケーブル温度・電気/建築/冷却の適合認定ではありません。'
        )
        self.summary_note.setWordWrap(True)
        set_typography_role(self.summary_note, TypographyRole.SECONDARY)
        layout.addWidget(self.summary_note)
        self.summary_tabs = QTabWidget()
        self.summary_tabs.setAccessibleName('電力・発熱・回路サマリ')

        power_tab = QWidget()
        power_layout = QVBoxLayout(power_tab)
        power_layout.setContentsMargins(4, 4, 4, 4)
        power_form = QFormLayout()
        self.scenario_combo = QComboBox()
        self.scenario_combo.setAccessibleName('動作シナリオ')
        self.scenario_combo.currentIndexChanged.connect(
            self._scenario_changed
        )
        power_form.addRow('シナリオ', self.scenario_combo)
        power_layout.addLayout(power_form)
        self.load_bar = _LoadBarWidget()
        power_layout.addWidget(self.load_bar)
        self.load_label = QLabel('')
        self.load_label.setWordWrap(True)
        self.load_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        power_layout.addWidget(self.load_label)
        self.summary_tabs.addTab(power_tab, '電力')

        heat_tab = QWidget()
        heat_layout = QVBoxLayout(heat_tab)
        heat_layout.setContentsMargins(4, 4, 4, 4)
        self.heat_label = QLabel('')
        self.heat_label.setWordWrap(True)
        self.heat_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        heat_layout.addWidget(self.heat_label)
        self.summary_tabs.addTab(heat_tab, '発熱')

        endpoint_tab = QWidget()
        endpoint_layout = QVBoxLayout(endpoint_tab)
        endpoint_layout.setContentsMargins(4, 4, 4, 4)
        self.endpoint_tree = QTreeWidget()
        self.endpoint_tree.setAccessibleName('エンドポイント負荷一覧')
        self.endpoint_tree.setColumnCount(4)
        self.endpoint_tree.setHeaderLabels(
            ['エンドポイント', '既知負荷', '未宣言機器', '宣言定格との比較']
        )
        self.endpoint_tree.setRootIsDecorated(False)
        endpoint_layout.addWidget(self.endpoint_tree)
        self.summary_tabs.addTab(endpoint_tab, '回路')
        layout.addWidget(self.summary_tabs)

        context_row = QHBoxLayout()
        self.import_button = QPushButton('コンテキストJSONを読み込む')
        self.import_button.setAccessibleName('コンテキストJSONを読み込む')
        self.import_button.setToolTip(
            '機器プロファイル・シナリオ・エンドポイントのJSONをこのセッションに読み込みます（未保存）。'
        )
        set_control_size(self.import_button, ControlSize.COMPACT)
        self.import_button.clicked.connect(self._import_context_dialog)
        context_row.addWidget(self.import_button)
        self.context_label = QLabel('コンテキスト: 未提供')
        self.context_label.setWordWrap(True)
        set_typography_role(self.context_label, TypographyRole.SECONDARY)
        context_row.addWidget(self.context_label, 1)
        layout.addLayout(context_row)

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        set_semantic_state(self.status_label, SemanticState.ERROR)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

    # -- context injection ---------------------------------------------------

    def set_authority_context(
        self,
        *,
        devices: Sequence[EquipmentPowerProfile] = (),
        endpoints: Sequence[CircuitEndpoint] = (),
        assignments: Sequence[CircuitAssignment] = (),
        scenarios: Sequence[ElectricalScenario] = (),
        source_label: str | None = None,
    ) -> None:
        """Supply the session-scoped planning records the workspace reads.

        These records have no persisted table yet — the panel treats them as
        declared-for-this-session data and labels the source honestly.
        """
        self._devices = tuple(devices)
        self._endpoints = tuple(endpoints)
        self._assignments = tuple(assignments)
        self._scenarios = tuple(scenarios)
        self._context_source = source_label
        self._sync_context_label()
        self._rebuild_rack_view()

    def _sync_context_label(self) -> None:
        if not any((
            self._devices, self._endpoints, self._assignments, self._scenarios
        )):
            self.context_label.setText(
                'コンテキスト: 未提供（プロファイルの無い機器は不明として表示）'
            )
            return
        origin = self._context_source or 'セッション注入'
        self.context_label.setText(
            f'コンテキスト: 機器 {len(self._devices)} / '
            f'エンドポイント {len(self._endpoints)} / '
            f'シナリオ {len(self._scenarios)}（{origin}・未保存）'
        )

    def import_context_json(self, path: str | Path) -> bool:
        """Load session context records from a JSON file — fail-closed."""
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
            if isinstance(data, list):
                data = {'devices': data}
            if not isinstance(data, dict):
                raise ValueError('context JSON must be an object or list')
            devices = tuple(
                EquipmentPowerProfile.model_validate(item)
                for item in data.get('devices', ())
            )
            endpoints = tuple(
                CircuitEndpoint.model_validate(item)
                for item in data.get('endpoints', ())
            )
            assignments = tuple(
                CircuitAssignment.model_validate(item)
                for item in data.get('assignments', ())
            )
            scenarios = tuple(
                ElectricalScenario.model_validate(item)
                for item in data.get('scenarios', ())
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, show why
            self._set_error(
                f'コンテキストの読み込みに失敗しました: {exc}'
            )
            return False
        self.set_authority_context(
            devices=devices,
            endpoints=endpoints,
            assignments=assignments,
            scenarios=scenarios,
            source_label=f'読み込み: {Path(path).name}',
        )
        self._set_error(None)
        return True

    def _import_context_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, 'ラックコンテキストJSON', '', 'JSON (*.json);;すべてのファイル (*)',
        )
        if path:
            self.import_context_json(path)

    # -- data flow -----------------------------------------------------------

    def refresh(self) -> None:
        """Reload persisted racks/layouts and re-evaluate the view."""
        self._syncing = True
        try:
            keep = self.rack_combo.currentData()
            self._racks = {
                rack.rack_id: rack
                for rack in self.feature_repository.list_rack_definitions(
                    self.document_id
                )
            }
            self.rack_combo.clear()
            for rack_id in sorted(self._racks):
                self.rack_combo.addItem(
                    f'{self._racks[rack_id].name}（{rack_id}）', rack_id
                )
            if keep in self._racks:
                self.rack_combo.setCurrentIndex(
                    self.rack_combo.findData(keep)
                )
        finally:
            self._syncing = False
        self._rebuild_rack_view()

    def _selected_rack(self) -> RackDefinition | None:
        data = self.rack_combo.currentData()
        return None if data is None else self._racks.get(str(data))

    def _rack_changed(self) -> None:
        if self._syncing:
            return
        self._rebuild_rack_view()

    def _rebuild_rack_view(self) -> None:
        rack = self._selected_rack()
        self._layouts = (
            [] if rack is None else list(
                self.feature_repository.list_rack_layouts(
                    rack.rack_id, self.document_id
                )
            )
        )
        self._layout = self._layouts[-1] if self._layouts else None
        self._results = ()
        if rack is not None and self._layout is not None:
            self._results = evaluate_rack_fit(
                self._layout, rack, self._devices
            )
        self._sync_rack_info(rack)
        self.elevation.set_scene(
            rack, self._layout, self._devices, self._results
        )
        self._sync_fit_table()
        self._sync_move_row()
        self._sync_summaries()

    def _sync_rack_info(self, rack: RackDefinition | None) -> None:
        if rack is None:
            self.rack_info_label.setText(
                'ラック定義が保存されていません。'
                'ラック権威レコードを登録するとここに表示されます。'
            )
            return
        parts = [
            f'RU容量: {rack.ru_capacity}' if rack.ru_capacity is not None
            else 'RU容量: 未宣言',
            f'使用可能奥行: {format_length_m(rack.usable_depth_m, self._length_policy)}',
            '前面クリアランス: ' + (
                format_length_m(rack.front_clearance_m, self._length_policy)
                if rack.front_clearance_m is not None else '未宣言'
            ),
            '背面クリアランス: ' + (
                format_length_m(rack.rear_clearance_m, self._length_policy)
                if rack.rear_clearance_m is not None else '未宣言'
            ),
            'サービスクリアランス: ' + (
                format_length_m(rack.service_clearance_m, self._length_policy)
                if rack.service_clearance_m is not None else '未宣言（記録のみ）'
            ),
            f'保存済みレイアウト: {len(self._layouts)} 版'
            + ('（最新を表示）' if self._layouts else ''),
            # #1005 owns the installability/service-space treatment — this
            # surface only cross-references it.
            '施工・運用空間の評価は #1005 の専用Issueを参照',
        ]
        self.rack_info_label.setText(' ｜ '.join(parts))

    def _sync_fit_table(self) -> None:
        self.fit_tree.blockSignals(True)
        try:
            self.fit_tree.clear()
            if self._layout is None:
                return
            results = {item.device_id: item for item in self._results}
            for placement in self._layout.placements:
                result = results.get(placement.device_id)
                conflict = result is not None and bool(result.conflicts)
                item = QTreeWidgetItem([
                    placement.device_id,
                    _position_label(placement),
                    '' if result is None else _FIT_LABELS[result.ru_occupancy],
                    '' if result is None else _FIT_LABELS[result.depth_fit],
                    '' if result is None else _FIT_LABELS[result.clearance_fit],
                    '' if result is None else self._reason_text(result),
                ])
                item.setData(0, Qt.ItemDataRole.UserRole, placement.device_id)
                for column in range(6):
                    if result is None:
                        item.setForeground(column, SEMANTIC.unsupported.qcolor())
                    elif result.ru_occupancy == 'FAIL' and column == 2:
                        item.setForeground(column, SEMANTIC.error.qcolor())
                    elif result.depth_fit == 'FAIL' and column == 3:
                        item.setForeground(column, SEMANTIC.error.qcolor())
                    elif result.clearance_fit == 'FAIL' and column == 4:
                        item.setForeground(column, SEMANTIC.error.qcolor())
                # Both parties of a conflict are highlighted, not just the
                # later placement.
                if conflict:
                    for column in range(6):
                        item.setBackground(
                            column, QBrush(QColor(ACCENT.selection_fill.hex))
                        )
                self.fit_tree.addTopLevelItem(item)
            self.fit_tree.resizeColumnToContents(0)
        finally:
            self.fit_tree.blockSignals(False)

    @staticmethod
    def _reason_text(result: DeviceFitResult) -> str:
        parts: list[str] = []
        if result.conflicts:
            parts.append('競合: ' + '、'.join(result.conflicts))
        parts.extend(result.reasons)
        return ' / '.join(parts)

    def _sync_move_row(self) -> None:
        self.move_device_combo.blockSignals(True)
        try:
            keep = self.move_device_combo.currentData()
            self.move_device_combo.clear()
            if self._layout is not None:
                for placement in self._layout.placements:
                    self.move_device_combo.addItem(
                        placement.device_id, placement.device_id
                    )
            if keep is not None:
                index = self.move_device_combo.findData(keep)
                if index >= 0:
                    self.move_device_combo.setCurrentIndex(index)
            rack = self._selected_rack()
            self.move_ru_spin.setMaximum(
                rack.ru_capacity if rack is not None
                and rack.ru_capacity is not None else 999
            )
            self.move_preview_button.setEnabled(
                self._layout is not None
                and self.move_device_combo.count() > 0
            )
        finally:
            self.move_device_combo.blockSignals(False)

    def _scenario_changed(self) -> None:
        self._sync_summaries()

    def _layout_device_ids(self) -> tuple[str, ...]:
        if self._layout is None:
            return ()
        return tuple(item.device_id for item in self._layout.placements)

    def _layout_devices(self) -> tuple[EquipmentPowerProfile, ...]:
        ids = set(self._layout_device_ids())
        return tuple(d for d in self._devices if d.device_id in ids)

    def _sync_summaries(self) -> None:
        layout_ids = set(self._layout_device_ids())
        devices = self._layout_devices()
        # Only scenarios whose stored device set is fully covered by the
        # evaluated layout are offered — a stored condition that does not
        # match this layout is never silently summarized.
        keep = self.scenario_combo.currentData()
        self.scenario_combo.blockSignals(True)
        try:
            self.scenario_combo.clear()
            self._usable_scenarios = tuple(
                scenario for scenario in self._scenarios
                if set(scenario.device_ids) <= layout_ids
            )
            for scenario in self._usable_scenarios:
                label = scenario.state_label or _STATE_LABELS.get(
                    scenario.state, scenario.state
                )
                self.scenario_combo.addItem(
                    f'{scenario.scenario_id}（{label}）',
                    scenario.scenario_id,
                )
            if keep is not None:
                index = self.scenario_combo.findData(keep)
                if index >= 0:
                    self.scenario_combo.setCurrentIndex(index)
        finally:
            self.scenario_combo.blockSignals(False)

        scenario = next(
            (
                s for s in getattr(self, '_usable_scenarios', ())
                if s.scenario_id == self.scenario_combo.currentData()
            ),
            None,
        )
        # Power tab
        if not layout_ids:
            self.load_label.setText('レイアウトが未保存のため集計対象がありません。')
            self.load_bar.set_segments(())
        elif scenario is None:
            self.load_label.setText(
                'このレイアウトに一致する保存済みシナリオがありません。'
                'シナリオを読み込むと、条件が一致する場合のみ集計します。'
            )
            self.load_bar.set_segments(())
        else:
            summary = summarize_load(scenario, devices)
            profile_by_id = {d.device_id: d for d in devices}
            segments: list[tuple[str, float]] = []
            detail: list[str] = []
            for did in summary.known_device_ids:
                value = profile_by_id[did].power_for(
                    scenario.state, scenario.state_label
                )
                segments.append((did, value.power_w))
                detail.append(
                    f'{did}: {value.power_w:g} W'
                    f'（{_KIND_LABELS.get(value.kind, value.kind)}）'
                )
            lines = [
                f'既知負荷 合計: {summary.known_w:g} W'
                f'（{_STATE_LABELS.get(summary.state, summary.state)}）',
                *detail,
            ]
            if summary.unknown_device_ids:
                lines.append(
                    '宣言値なし（0Wとは数えません）: '
                    + '、'.join(summary.unknown_device_ids)
                )
            self.load_label.setText('\n'.join(lines))
            self.load_bar.set_segments(segments)

        # Heat tab — documented dissipation only; a derived figure shows up
        # only when its recorded assumption was supplied.
        if not devices:
            self.heat_label.setText(
                '熱の宣言値を持つ機器プロファイルがありません。'
            )
        else:
            heat = summarize_heat(devices)
            lines = [
                f'記録済み発熱 合計: {heat.documented_heat_w:g} W'
                f'（{len(heat.documented_device_ids)} 台）',
            ]
            if heat.undocumented_device_ids:
                lines.append(
                    '発熱未宣言: ' + '、'.join(heat.undocumented_device_ids)
                )
            if heat.derived_heat_w is not None:
                lines.append(
                    f'導出値: {heat.derived_heat_w:g} W'
                    f'（仮定: {heat.derivation_note}）'
                )
            else:
                lines.append('導出値: なし（仮定の記録なし）')
            self.heat_label.setText('\n'.join(lines))

        # Endpoint tab — per-endpoint arithmetic under the *selected*
        # scenario state; no selected scenario → honestly un-evaluated.
        self.endpoint_tree.clear()
        if not self._endpoints:
            placeholder = QTreeWidgetItem(
                ['エンドポイントが提供されていません', '', '', '']
            )
            self.endpoint_tree.addTopLevelItem(placeholder)
            return
        if scenario is None:
            placeholder = QTreeWidgetItem(
                ['評価状態が未定 — 一致するシナリオを選択してください', '', '', '']
            )
            self.endpoint_tree.addTopLevelItem(placeholder)
            return
        try:
            summaries = summarize_endpoint_loads(
                self._assignments,
                self._endpoints,
                devices,
                state=scenario.state,
                state_label=scenario.state_label,
            )
        except ValueError as exc:
            placeholder = QTreeWidgetItem(
                [f'割当トポロジが無効です: {exc}', '', '', '']
            )
            self.endpoint_tree.addTopLevelItem(placeholder)
            return
        endpoint_by_id = {e.endpoint_id: e for e in self._endpoints}
        for summary in summaries:
            endpoint = endpoint_by_id.get(summary.endpoint_id)
            label = endpoint.label if endpoint is not None else summary.endpoint_id
            kind = (
                _ENDPOINT_KIND_LABELS.get(endpoint.kind, endpoint.kind)
                if endpoint is not None else ''
            )
            if summary.exceeds_declared_rating is None:
                compare = (
                    '定格未宣言' if summary.declared_rating_w is None
                    else '未宣言機器があるため比較不可'
                )
            elif summary.exceeds_declared_rating:
                compare = f'宣言定格 {summary.declared_rating_w:g} W を超過（算術比較）'
            else:
                compare = f'宣言定格 {summary.declared_rating_w:g} W 以内（算術比較）'
            row = QTreeWidgetItem([
                f'{label}（{kind}）',
                f'{summary.known_w:g} W',
                '、'.join(summary.unknown_device_ids) or '—',
                compare,
            ])
            if summary.exceeds_declared_rating:
                for column in range(4):
                    row.setForeground(column, SEMANTIC.error.qcolor())
            self.endpoint_tree.addTopLevelItem(row)

    # -- move preview / commit ------------------------------------------------

    def _device_selected(self, device_id: str) -> None:
        index = self.move_device_combo.findData(device_id)
        if index >= 0:
            self.move_device_combo.setCurrentIndex(index)
        for i in range(self.fit_tree.topLevelItemCount()):
            item = self.fit_tree.topLevelItem(i)
            if item.data(0, Qt.ItemDataRole.UserRole) == device_id:
                self.fit_tree.setCurrentItem(item)
                break
        self.elevation.set_selected_device(device_id)

    def _fit_row_changed(self, current, _previous) -> None:
        device_id = (
            None if current is None
            else current.data(0, Qt.ItemDataRole.UserRole)
        )
        if device_id:
            self.elevation.set_selected_device(str(device_id))

    def _preview_from_form(self) -> None:
        device_id = self.move_device_combo.currentData()
        if device_id is None:
            return
        self._preview_move(str(device_id), int(self.move_ru_spin.value()))

    def _preview_move(self, device_id: str, target_ru: int) -> None:
        """Build the candidate layout, re-evaluate, confirm — the only
        path to a write."""
        rack = self._selected_rack()
        if rack is None or self._layout is None:
            return
        placement = next(
            (
                p for p in self._layout.placements
                if p.device_id == device_id
            ),
            None,
        )
        if placement is None:
            return
        if placement.ru_position == target_ru:
            self._set_error(None)
            self.status_label.setText('位置に変更がありません。')
            set_semantic_state(self.status_label, SemanticState.STALE)
            self.status_label.setVisible(True)
            return
        candidate = RackLayout(
            rack_id=rack.rack_id,
            placements=tuple(
                (
                    RackPlacement(
                        device_id=p.device_id,
                        ru_position=(
                            target_ru if p.device_id == device_id
                            else p.ru_position
                        ),
                        shelf_label=p.shelf_label,
                    )
                )
                for p in self._layout.placements
            ),
        )
        after = evaluate_rack_fit(candidate, rack, self._devices)
        dialog = RackMoveConfirmDialog(
            self,
            device_id=device_id,
            old_layout=self._layout,
            new_layout=candidate,
            before=self._results,
            after=after,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._commit_layout(candidate)

    def _commit_layout(self, candidate: RackLayout) -> None:
        rack = self._selected_rack()
        if rack is None:
            return
        # Re-verify the exact objects the preview evaluated — the candidate
        # must still be "current layout + one moved placement" against the
        # selected rack, otherwise this is a different commit than what was
        # confirmed and it is refused rather than silently re-scoped.
        if (
            candidate.rack_id != rack.rack_id
            or not self._candidate_matches_current(candidate)
        ):
            self._set_error(
                'プレビュー後に対象が変わりました。もう一度確認してください。'
            )
            return
        try:
            layout_id = self.feature_repository.save_rack_layout(
                candidate, document_id=self.document_id
            )
        except Exception as exc:  # noqa: BLE001 - surfaced honestly
            self._set_error(
                'レイアウトの保存に失敗しました: '
                + operation_error_message(exc)
            )
            return
        self._set_error(None)
        self.status_label.setText(
            f'レイアウトを新しい版として保存しました（{layout_id}）。'
        )
        set_semantic_state(self.status_label, SemanticState.SUCCESS)
        self.status_label.setVisible(True)
        self.refresh()
        self.layoutCommitted.emit(layout_id)

    def _moved_device_id(self, candidate: RackLayout) -> str | None:
        if self._layout is None:
            return None
        before = {p.device_id: p for p in self._layout.placements}
        for placement in candidate.placements:
            if before.get(placement.device_id) != placement:
                return placement.device_id
        return None

    def _candidate_matches_current(self, candidate: RackLayout) -> bool:
        """The candidate is exactly the current layout plus one moved
        placement — the only admissible commit shape."""
        if self._layout is None or self._selected_rack() is None:
            return False
        if candidate.rack_id != self._layout.rack_id:
            return False
        if len(candidate.placements) != len(self._layout.placements):
            return False
        moved = self._moved_device_id(candidate)
        if moved is None:
            return False
        before = {p.device_id: p for p in self._layout.placements}
        for placement in candidate.placements:
            if placement.device_id == moved:
                continue
            if before.get(placement.device_id) != placement:
                return False
        return True

    def _set_error(self, message: str | None) -> None:
        if message:
            set_semantic_state(self.status_label, SemanticState.ERROR)
        self.status_label.setText(message or '')
        self.status_label.setVisible(bool(message))

    def set_length_policy(self, policy: LengthDisplayPolicy) -> None:
        """#496 display-unit binding — SI metres stay authoritative."""
        self._length_policy = policy
        self._sync_rack_info(self._selected_rack())


__all__ = [
    'RackElevationView',
    'RackMoveConfirmDialog',
    'RackWorkspacePanel',
]
