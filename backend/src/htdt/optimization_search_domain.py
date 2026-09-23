"""Visual search-domain authoring layer for Optimize (#530).

A live 3D preview of the *draft* ``CadSearchSpec`` axes — line/plane extents in
world space, draggable min/max handles bound to the exact numeric fields, raw
grid cardinality with a big-product warning, and the #486 hard constraints
overlaid with distinct semantics. The preview is editor state only: it never
mutates the SceneDocument and produces the same immutable authority the
numeric fields already materialize via ``CadSearchAxis``.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from .cad_constraints import evaluate_cad_constraints
from .cad_search_models import CadSearchAxis
from .room_viewport import RoomOverlayState
from .ui_theme import SurfaceRole, TypographyRole, set_surface_role, set_typography_role

_AXIS_INDEX = {'x': 0, 'y': 1, 'z': 2}
# Render space negates domain +Y (domain_to_render), so a domain Y bound maps
# onto the render y-axis with flipped sign.
_AXIS_RENDER_SIGN = {'x': 1.0, 'y': -1.0, 'z': 1.0}
_CARDINALITY_WARN = 10_000


def axis_cardinality(axis: CadSearchAxis) -> int:
    """Exact deterministic grid samples for one axis: floor(span/step) + 1."""

    span = float(axis.max_m) - float(axis.min_m)
    if span < 0.0:
        return 0
    return int(math.floor(span / float(axis.step_m) + 1e-9)) + 1


def search_cardinality(axes: tuple[CadSearchAxis, ...]) -> int:
    """Raw Cartesian-product count across all authored axes."""

    total = 1
    for axis in axes:
        count = axis_cardinality(axis)
        if count <= 0:
            return 0
        total *= count
    return total


def cardinality_summary(axes: tuple[CadSearchAxis, ...]) -> str:
    """Japanese one-liner: per-axis samples and the raw total."""

    if not axes:
        return "探索軸がありません · 軸を追加してください"
    per_axis = " × ".join(
        f"{axis.axis.upper()}: {axis_cardinality(axis)}" for axis in axes
    )
    return f"{per_axis} → 生候補数 {search_cardinality(axes)} 件"


class SearchDomainPreview(QFrame):
    """Setup-page 3D preview + handle-drag authoring bound to the draft fields."""

    axesChanged = Signal()

    def __init__(
        self,
        controller,
        viewport,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        set_surface_role(self, SurfaceRole.CANVAS)
        self.controller = controller
        self.viewport = viewport
        self._dragging: tuple[str, str, str] | None = None
        self._refreshing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        heading = QLabel("探索領域プレビュー（3D）")
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(heading)
        hint = QLabel(
            "座標: ワールド m（物体の現在位置基準）· ハンドルをドラッグで範囲を編集 · "
            "紫/橙の面 = ハード制約（探索領域とは別に表示）"
        )
        hint.setWordWrap(True)
        set_typography_role(hint, TypographyRole.SECONDARY)
        layout.addWidget(hint)
        layout.addWidget(viewport, 1)
        self.cardinality_label = QLabel("探索軸がありません · 軸を追加してください")
        self.cardinality_label.setWordWrap(True)
        set_typography_role(self.cardinality_label, TypographyRole.SECONDARY)
        layout.addWidget(self.cardinality_label)

        interactor = getattr(viewport, "interactor", None)
        if interactor is not None:
            interactor.installEventFilter(self)

    # -- refresh ------------------------------------------------------------------

    def draft_axis(self) -> dict | None:
        """The in-progress authoring row, as a dict render_search_domain reads."""

        entity_id = self.controller.search_entity_combo.currentData()
        axis = self.controller.search_axis_combo.currentData()
        if entity_id is None or axis not in _AXIS_INDEX:
            return None
        return {
            'entity_id': str(entity_id),
            'axis': axis,
            'min_m': float(self.controller.search_min_field.value()),
            'max_m': float(self.controller.search_max_field.value()),
        }

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            document = self.controller.working.document
            render_document = getattr(self.viewport, "render_document", None)
            if callable(render_document):
                render_document(
                    document,
                    selected_id=self.controller.selected_id,
                    overlays=RoomOverlayState(grid=True, labels=False, acoustics=False),
                )
            axes = self.controller._draft_search_axes()
            draft = self.draft_axis()
            evaluation = None
            constraint_set = self.controller.constraint_set
            if constraint_set is not None and constraint_set.constraints:
                try:
                    evaluation = evaluate_cad_constraints(document, constraint_set)
                except Exception:
                    evaluation = None
            render_constraints = getattr(self.viewport, "render_constraint_overlay", None)
            if callable(render_constraints) and constraint_set is not None:
                render_constraints(constraint_set, evaluation)
            render_domain = getattr(self.viewport, "render_search_domain", None)
            if callable(render_domain) and (axes or draft is not None):
                entity_id = draft['entity_id'] if draft is not None else axes[0].entity_id
                render_domain(
                    entity_id,
                    axes,
                    draft_axis=draft,
                    hard_constraints_satisfied=(
                        None if evaluation is None else evaluation.constraints_satisfied
                    ),
                )
            total = search_cardinality(axes)
            summary = cardinality_summary(axes)
            if total > _CARDINALITY_WARN:
                summary += f" · 警告: 大きな直積（{_CARDINALITY_WARN} 超）"
            self.cardinality_label.setText(summary)
        finally:
            self._refreshing = False

    # -- drag handles -----------------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if not isinstance(event, QMouseEvent):
            return False
        pick_actor = getattr(self.viewport, "pick_actor_at", None)
        pick_world = getattr(self.viewport, "pick_world_position", None)
        handles = getattr(self.viewport, "_search_domain_handles", None)
        if not callable(pick_actor) or not callable(pick_world) or not handles:
            return False
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            actor = pick_actor(event.position())
            if actor is None:
                return False
            key = handles.get(id(actor))
            if key is None:
                return False
            entity_id, axis, tag, _bright = key
            # A saved-axis handle first binds the numeric form to that axis so
            # the drag edits the same fields (bidirectional contract).
            self._bind_fields_to_axis(entity_id, axis)
            self._dragging = (entity_id, axis, tag)
            return True
        if event.type() == QEvent.Type.MouseMove and self._dragging is not None:
            world = pick_world(event.position())
            if world is None:
                return True
            entity_id, axis, tag = self._dragging
            try:
                entity = self.controller.working.document.entity(entity_id)
            except KeyError:
                self._dragging = None
                return True
            idx = _AXIS_INDEX[axis]
            center = (
                float(entity.position.x_m),
                float(-entity.position.y_m),
                float(entity.position.z_m),
            )[idx]
            value = (float(world[idx]) - center) * _AXIS_RENDER_SIGN[axis]
            target = (
                self.controller.search_min_field
                if tag == 'min'
                else self.controller.search_max_field
            )
            target.setValue(round(value, 3))
            return True
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            if self._dragging is not None:
                self._dragging = None
                return True
        return False

    def _bind_fields_to_axis(self, entity_id: str, axis: str) -> None:
        """Point the numeric form at (entity_id, axis) without losing edits."""

        entity_combo = self.controller.search_entity_combo
        axis_combo = self.controller.search_axis_combo
        index = entity_combo.findData(entity_id)
        if index >= 0 and entity_combo.currentData() != entity_id:
            entity_combo.setCurrentIndex(index)
        index = axis_combo.findData(axis)
        if index >= 0 and axis_combo.currentData() != axis:
            axis_combo.setCurrentIndex(index)


__all__ = [
    "SearchDomainPreview",
    "axis_cardinality",
    "cardinality_summary",
    "search_cardinality",
]
