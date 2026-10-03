"""Hard-constraint authoring panel for the workflow-first Room (#486).

Creates/edits/deletes all four persisted constraint kinds (allowed region,
exclusion/walkway, wall clearance, pair distance) against the SAME
``CadConstraintRepository``/``CadConstraintSet`` consumed by Optimize, and
evaluates them live so violations name the subject and reason. No legacy
dock-editor dependency.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_constraint_models import CadConstraintEvaluation, CadConstraintSet
from .cad_scene import SceneDocument
from .ui_theme import TypographyRole, set_typography_role

_RESULT_ID_ROLE = Qt.ItemDataRole.UserRole
_CONSTRAINT_ID_ROLE = Qt.ItemDataRole.UserRole + 1


class RoomConstraintsPanel(QWidget):
    """Constraint list + status + add/delete actions."""

    constraintActionRequested = Signal(str)  # 'add_walkway' | 'add_allowed' | 'add_wall' | 'add_pair' | 'delete'
    resultSelected = Signal(object)  # CadConstraintResult | None
    optimizeRequested = Signal()  # navigate to Optimize (deep link out)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.heading = QLabel("配置制約（ハード制約）")
        set_typography_role(self.heading, TypographyRole.SECTION_TITLE)
        self.heading.setToolTip(
            '破ってはいけない配置ルール · 違反する移動はブロックされ、最適化の候補からも外れます'
        )
        layout.addWidget(self.heading)

        self.summary_label = QLabel("制約なし")
        set_typography_role(self.summary_label, TypographyRole.SECONDARY)
        layout.addWidget(self.summary_label)

        add_row = QHBoxLayout()
        add_row.setSpacing(4)
        self.add_walkway_button = QPushButton("通路")
        self.add_walkway_button.setToolTip(
            '選択物体を中心に人が通る余地（除外領域）を追加します'
        )
        self.add_allowed_button = QPushButton("許可領域")
        self.add_allowed_button.setToolTip(
            '選択物体が置いてよい範囲を追加します（その範囲外には置けません）'
        )
        self.add_wall_button = QPushButton("壁離隔")
        self.add_wall_button.setToolTip(
            '選択物体と壁の間に最低限の離隔を設けます（下の最小距離・対象壁を使用）'
        )
        self.add_pair_button = QPushButton("物体間離隔")
        self.add_pair_button.setToolTip(
            '選択した2物体の間に最低限の水平離隔を設けます（先に2つを選択）'
        )
        for button in (
            self.add_walkway_button,
            self.add_allowed_button,
            self.add_wall_button,
            self.add_pair_button,
        ):
            add_row.addWidget(button)
        layout.addLayout(add_row)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self.distance_field = QDoubleSpinBox()
        self.distance_field.setRange(0.0, 50.0)
        self.distance_field.setSingleStep(0.05)
        self.distance_field.setSuffix(" m")
        self.distance_field.setValue(0.50)
        self.distance_field.setToolTip(
            '「壁離隔」「物体間離隔」で使う最小の間隔（m）'
        )
        form.addRow("最小距離", self.distance_field)
        self.wall_combo = QComboBox()
        self.wall_combo.setToolTip(
            '離隔の対象となる壁 · 空欄は最も近い壁を自動で選びます'
        )
        form.addRow("対象壁（空=最近）", self.wall_combo)
        for field in (self.distance_field, self.wall_combo):
            label = form.labelForField(field)
            if label is not None:
                label.setToolTip(field.toolTip())
        layout.addLayout(form)

        self.delete_button = QPushButton("選択した制約を削除")
        self.delete_button.setToolTip('一覧で選択した制約を削除します')
        layout.addWidget(self.delete_button)
        self.optimize_button = QPushButton("最適化で確認")
        self.optimize_button.setToolTip(
            'この制約を含めた配置探索の結果を「最適化」ワークスペースで確認します'
        )
        layout.addWidget(self.optimize_button)

        self.results_tree = QTreeWidget()
        self.results_tree.setAccessibleName("配置制約一覧")
        self.results_tree.setHeaderLabels(("制約", "対象", "状態"))
        header = self.results_tree.headerItem()
        if header is not None:
            header.setToolTip(0, '制約の種類と名前')
            header.setToolTip(1, '制約が適用される物体')
            header.setToolTip(2, '現在の評価 · ✓=満たしている / ✕=違反（実測値つき）')
        self.results_tree.setRootIsDecorated(False)
        self.results_tree.setUniformRowHeights(True)
        layout.addWidget(self.results_tree, stretch=1)

        self.detail_label = QLabel("制約を選択すると理由を表示します")
        self.detail_label.setWordWrap(True)
        set_typography_role(self.detail_label, TypographyRole.SECONDARY)
        layout.addWidget(self.detail_label)

        self.add_walkway_button.clicked.connect(
            lambda: self.constraintActionRequested.emit('add_walkway')
        )
        self.add_allowed_button.clicked.connect(
            lambda: self.constraintActionRequested.emit('add_allowed')
        )
        self.add_wall_button.clicked.connect(
            lambda: self.constraintActionRequested.emit('add_wall')
        )
        self.add_pair_button.clicked.connect(
            lambda: self.constraintActionRequested.emit('add_pair')
        )
        self.delete_button.clicked.connect(
            lambda: self.constraintActionRequested.emit('delete')
        )
        self.optimize_button.clicked.connect(self.optimizeRequested)
        self.results_tree.currentItemChanged.connect(self._emit_result)

        self._results: dict[str, object] = {}

    def selected_constraint_id(self) -> str | None:
        item = self.results_tree.currentItem()
        if item is None:
            return None
        return item.data(0, _CONSTRAINT_ID_ROLE)

    def selected_wall_id(self) -> str | None:
        """Wall combo selection; ``None`` asks the solver for the nearest wall."""
        return self.wall_combo.currentData()

    def distance_m(self) -> float:
        """Minimum distance field used by the wall-clearance/pair buttons."""
        return float(self.distance_field.value())

    def _emit_result(self, current: QTreeWidgetItem | None, _previous) -> None:
        if current is None:
            self.resultSelected.emit(None)
            return
        result_id = current.data(0, _RESULT_ID_ROLE)
        self.resultSelected.emit(self._results.get(str(result_id)))

    def set_walls(self, document: SceneDocument) -> None:
        current = self.wall_combo.currentData()
        self.wall_combo.blockSignals(True)
        self.wall_combo.clear()
        self.wall_combo.addItem("（最近壁を自動選択）", None)
        if document is not None and document.wall_topology is not None:
            for wall in document.wall_topology.walls:
                self.wall_combo.addItem(wall.wall_id, wall.wall_id)
        index = self.wall_combo.findData(current)
        if index >= 0:
            self.wall_combo.setCurrentIndex(index)
        self.wall_combo.blockSignals(False)

    def sync_state(
        self,
        constraint_set: CadConstraintSet,
        evaluation: CadConstraintEvaluation | None,
        document: SceneDocument,
        *,
        evaluate_error: str | None = None,
    ) -> None:
        """Refresh the panel from authoritative state (call on refresh)."""

        self.set_walls(document)
        self._results = {}
        if evaluate_error is not None:
            self.summary_label.setText(f"制約を評価できません · {evaluate_error}")
        elif not constraint_set.constraints:
            self.summary_label.setText("制約なし · 移動/回転は自由です")
        elif evaluation is not None and evaluation.constraints_satisfied:
            self.summary_label.setText(f"制約を満たす ({len(constraint_set.constraints)} 件)")
        elif evaluation is not None:
            self.summary_label.setText(f"制約違反 {len(evaluation.violations)} 件")

        entity_names = {entity.entity_id: entity.name for entity in document.entities}
        self.results_tree.blockSignals(True)
        self.results_tree.clear()
        if evaluation is not None:
            ordered = sorted(
                evaluation.results,
                key=lambda item: (item.passed, item.name, item.result_id),
            )
            for result in ordered:
                self._results[result.result_id] = result
                subjects = '、'.join(
                    entity_names.get(entity_id, entity_id) for entity_id in result.entity_ids
                )
                measure = '' if result.actual_m is None else f' · {result.actual_m:.3f} m'
                item = QTreeWidgetItem(
                    [
                        f"{result.name}",
                        subjects,
                        ("✓ " if result.passed else "✕ ") + result.reason_ja + measure,
                    ]
                )
                item.setData(0, _RESULT_ID_ROLE, result.result_id)
                item.setData(0, _CONSTRAINT_ID_ROLE, result.constraint_id)
                self.results_tree.addTopLevelItem(item)
        self.results_tree.blockSignals(False)
