"""Room ruler/measurement tool (#491).

Point-to-point distance (with ΔX/ΔY/ΔZ, azimuth, elevation), entity-reference
endpoints, and a 3-point angle mode. Measurement is ephemeral — it never
creates Scene entities, constraints, or Undo steps; cancelling discards the
overlay.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .cad_measure import (
    MEASURE_MODE_LABELS,
    MEASURE_REFERENCE_LABELS,
    MeasureEndpoint,
    MeasureResult,
    build_angle_result,
    build_distance_result,
    format_measure_result,
)
from .cad_scene import Position3, SceneEntity, acoustic_reference_position
from .cad_snap import generate_snap_candidates
from .ui_theme import TypographyRole, set_typography_role


class RoomMeasureController(QObject):
    """Endpoint collection + result resolution for the Room measure tool."""

    measurementChanged = Signal(object)  # MeasureResult | None
    stateChanged = Signal()

    def __init__(self, workspace, viewport, parent: QObject | None = None) -> None:
        super().__init__(parent or workspace)
        self.workspace = workspace
        self.viewport = viewport
        self._active = False
        self._mode = 'distance'
        self._reference_kind = 'position'
        self._endpoints: list[MeasureEndpoint] = []
        self.result: MeasureResult | None = None

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def reference_kind(self) -> str:
        return self._reference_kind

    @property
    def endpoints(self) -> tuple[MeasureEndpoint, ...]:
        return tuple(self._endpoints)

    @property
    def expected_picks(self) -> int:
        return 3 if self._mode == 'angle' else 2

    def begin(self, *, mode: str | None = None) -> None:
        if mode is not None:
            self._mode = mode
        self._active = True
        self._endpoints = []
        self.result = None
        self.measurementChanged.emit(None)
        self.stateChanged.emit()
        self.workspace._set_status(
            "計測: 端点をクリックしてください（物体=基準点、空間=自由点） · Escで中止"
        )
        self._render()

    def cancel(self) -> None:
        if not self._active and not self._endpoints and self.result is None:
            return
        self._active = False
        self._endpoints = []
        self.result = None
        self.measurementChanged.emit(None)
        self.stateChanged.emit()
        # stateChanged leaves the stale click-to-measure prompt otherwise.
        self.workspace._set_status("計測を中止しました")
        self.workspace.refresh()

    def set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        if self._active:
            self.begin()

    def set_reference_kind(self, kind: str) -> None:
        self._reference_kind = kind

    def _resolve_entity_point(
        self,
        entity: SceneEntity,
        display_position: QPointF | None,
    ) -> MeasureEndpoint:
        """Resolve the semantic reference the user asked to measure."""

        kind = self._reference_kind
        if kind == 'acoustic_reference':
            reference = acoustic_reference_position(entity)
            if reference is not None:
                return MeasureEndpoint(
                    position=reference,
                    entity_id=entity.entity_id,
                    entity_name=entity.name,
                    reference_kind='acoustic_reference',
                    reference_label=MEASURE_REFERENCE_LABELS['acoustic_reference'],
                )
            # Fallback is labelled honestly rather than silently assumed.
            return MeasureEndpoint(
                position=entity.position,
                entity_id=entity.entity_id,
                entity_name=entity.name,
                reference_kind='position',
                reference_label=f"{MEASURE_REFERENCE_LABELS['position']}（音響基準なし）",
            )
        if kind == 'snap_point' and display_position is not None:
            probe_world = self.viewport.pick_world_position(display_position)
            probe = entity.position if probe_world is None else Position3(
                x_m=probe_world[0], y_m=-probe_world[1], z_m=probe_world[2]
            )
            best: tuple[float, Position3] | None = None
            for axis in ('x', 'y', 'z'):
                for candidate in generate_snap_candidates(
                    self.workspace.controller.document,
                    exclude_ids=set(),
                    axis=axis,
                    probe=probe,
                ):
                    if candidate.entity_id != entity.entity_id:
                        continue
                    distance = (
                        (candidate.target.x_m - probe.x_m) ** 2
                        + (candidate.target.y_m - probe.y_m) ** 2
                        + (candidate.target.z_m - probe.z_m) ** 2
                    )
                    if best is None or distance < best[0]:
                        best = (distance, candidate.target)
            if best is not None:
                return MeasureEndpoint(
                    position=best[1],
                    entity_id=entity.entity_id,
                    entity_name=entity.name,
                    reference_kind='snap_point',
                    reference_label=MEASURE_REFERENCE_LABELS['snap_point'],
                )
        return MeasureEndpoint(
            position=entity.position,
            entity_id=entity.entity_id,
            entity_name=entity.name,
            reference_kind='position',
            reference_label=MEASURE_REFERENCE_LABELS['position'],
        )

    def pick_entity(self, entity_id: str, display_position: QPointF | None = None) -> bool:
        """Consume an entity pick as a measurement endpoint. Returns False if idle."""

        if not self._active:
            return False
        try:
            entity = self.workspace.controller.document.entity(entity_id)
        except KeyError:
            return True
        self._endpoints.append(self._resolve_entity_point(entity, display_position))
        self._after_pick()
        return True

    def pick_free_point(self, display_position: QPointF) -> bool:
        """Consume an empty-space click as a free floor-plane endpoint."""

        if not self._active:
            return False
        world = self.viewport.pick_world_position(display_position)
        if world is None:
            return True
        self._endpoints.append(
            MeasureEndpoint(
                position=Position3(x_m=world[0], y_m=-world[1], z_m=world[2]),
                entity_id=None,
                entity_name=None,
                reference_kind='free_point',
                reference_label=MEASURE_REFERENCE_LABELS['free_point'],
            )
        )
        self._after_pick()
        return True

    def _after_pick(self) -> None:
        if len(self._endpoints) >= self.expected_picks:
            if self._mode == 'angle':
                a, vertex, b = self._endpoints[:3]
                self.result = build_angle_result(a, vertex, b)
            else:
                a, b = self._endpoints[:2]
                self.result = build_distance_result(a, b)
            self.measurementChanged.emit(self.result)
            self.stateChanged.emit()
            # Later clicks only append to _endpoints without changing the
            # result — stop prompting for endpoints once it is computed.
            self.workspace._set_status(
                "計測結果を表示しました · 新しい計測は「計測開始」で開始"
            )
            self._render()
            return
        self.stateChanged.emit()
        self._render()

    def _render(self) -> None:
        self.workspace.refresh()
        render = getattr(self.viewport, 'render_measure_overlay', None)
        if render is None:
            return
        render(
            self.result,
            draft_endpoints=tuple(endpoint.position for endpoint in self._endpoints),
        )


class RoomMeasurePanel(QWidget):
    """Compact measure card: mode + reference + result + copy/cancel."""

    def __init__(
        self,
        controller: RoomMeasureController,
        parent: QWidget | None = None,
        *,
        display_policy_provider=None,
    ) -> None:
        """``display_policy_provider``: zero-arg callable returning the
        current ``LengthDisplayPolicy`` (read at render time so preference
        commits apply immediately). ``None`` keeps the SI readout; report
        and export surfaces never call through this path."""
        super().__init__(parent)
        self._display_policy_provider = display_policy_provider
        self.controller = controller
        self._length_policy = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        heading = QLabel("計測")
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(heading)

        row = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.setToolTip(
            '計測の種類 · 距離=2点間の長さと成分・方位角、角度=3点がなす角'
        )
        mode_hints = {
            'distance': '2点をクリックして距離・XYZ成分・方位角/仰角を計ります',
            'angle': '3点をクリックして、中間の点での角度を計ります',
        }
        for index, (mode, label) in enumerate(MEASURE_MODE_LABELS.items()):
            self.mode_combo.addItem(label, mode)
            hint = mode_hints.get(mode)
            if hint:
                self.mode_combo.setItemData(
                    index, hint, Qt.ItemDataRole.ToolTipRole
                )
        self.reference_combo = QComboBox()
        self.reference_combo.setAccessibleName('計測基準')
        self.reference_combo.setToolTip(
            '物体上のどの位置を計測点にするか選びます'
        )
        reference_hints = {
            'position': '物体の原点（位置フィールドの座標）を計測点にします',
            'acoustic_reference': 'スピーカー・座席の音響基準点（音の発生/評価位置）を計測点にします',
            'snap_point': '頂点・辺・中点などのスナップ位置を計測点にします',
        }
        for kind, label in MEASURE_REFERENCE_LABELS.items():
            if kind != 'free_point':
                index = self.reference_combo.count()
                self.reference_combo.addItem(label, kind)
                hint = reference_hints.get(kind)
                if hint:
                    self.reference_combo.setItemData(
                        index, hint, Qt.ItemDataRole.ToolTipRole
                    )
        row.addWidget(self.mode_combo)
        row.addWidget(self.reference_combo)
        layout.addLayout(row)

        button_row = QHBoxLayout()
        self.start_button = QPushButton("計測開始")
        self.start_button.setToolTip(
            "計測を開始し、3D上で点をクリックして確定します（Escで中止）"
        )
        self.copy_button = QPushButton("コピー")
        self.copy_button.setToolTip("計測結果をクリップボードにコピーします")
        self.copy_button.setEnabled(False)
        self.cancel_button = QPushButton("中止")
        self.cancel_button.setToolTip("計測中の操作を中止します")
        for button in (self.start_button, self.copy_button, self.cancel_button):
            button_row.addWidget(button)
        layout.addLayout(button_row)

        self.result_label = QLabel("計測ツールは一時的です · 結果は保存されません")
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        set_typography_role(self.result_label, TypographyRole.SECONDARY)
        layout.addWidget(self.result_label)

        self.start_button.clicked.connect(lambda: self.controller.begin())
        self.cancel_button.clicked.connect(self.controller.cancel)
        self.copy_button.clicked.connect(self._copy)
        self.mode_combo.currentIndexChanged.connect(
            lambda _i: self.controller.set_mode(self.mode_combo.currentData())
        )
        self.reference_combo.currentIndexChanged.connect(
            lambda _i: self.controller.set_reference_kind(self.reference_combo.currentData())
        )
        controller.measurementChanged.connect(self._on_result)
        controller.stateChanged.connect(self._on_state)

    def set_length_policy(self, policy) -> None:
        """Apply the #496 display-length policy to result text + clipboard copy."""
        self._length_policy = policy
        self._on_result(self.controller.result)

    def set_display_policy_provider(self, provider) -> None:
        self._display_policy_provider = provider

    def _policy(self):
        if self._length_policy is not None:
            return self._length_policy
        if self._display_policy_provider is None:
            return None
        return self._display_policy_provider()

    def _on_result(self, result: MeasureResult | None) -> None:
        self.copy_button.setEnabled(result is not None)
        if result is not None:
            text = format_measure_result(result, policy=self._policy())
            refs = ' → '.join(endpoint.describe() for endpoint in result.endpoints)
            self.result_label.setText(f'{text}\n{refs}')

    def _on_state(self) -> None:
        self.start_button.setText("計測中…" if self.controller.is_active else "計測開始")
        if self.controller.result is None and not self.controller.is_active:
            self.result_label.setText("計測ツールは一時的です · 結果は保存されません")

    def _copy(self) -> None:
        if self.controller.result is None:
            return
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(
            format_measure_result(self.controller.result, policy=self._policy())
        )
