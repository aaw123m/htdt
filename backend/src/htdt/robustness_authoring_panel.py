"""O90 robustness-spec authoring panel on the Optimize > robustness page.

Until this panel existed no UI could create a ``RobustnessSpec`` — the joint
placement+DSP lane (which requires ``baseline.robustness_spec``) was a dead
end. The panel is a thin widget over :class:`RobustnessAuthoringContext`:
it lists evaluated candidates of the selected SearchSpec, offers only axes
the candidate document can actually perturb, compiles through
``build_robustness_spec``, persists via the repository, and runs
``evaluate_local_robustness`` on a ``NativeWorkerPool`` — same worker
pattern as ``JointOptimizationPanel``.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

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

from .tree_item_role import ROLE
from .native_worker import (
    WORKER_CANCELLED,
    NativeWorkerPool,
    WorkerShutdownReport,
)
from .robustness_authoring_context import (
    DEFAULT_ANGLE_DELTA_DEG,
    DEFAULT_POSITION_DELTA_M,
    RobustnessAuthoringContext,
    RobustnessAxisChoice,
    RobustnessCandidateChoice,
)
from .ui_theme import TypographyRole, set_typography_role
from .user_facing_error import operation_error_message


class RobustnessAuthoringPanel(QWidget):
    """Author + evaluate an O90 local-stencil RobustnessSpec."""

    evaluationCompleted = Signal(object)  # RobustnessSpec, GUI thread

    def __init__(
        self,
        context: RobustnessAuthoringContext,
        *,
        selected_spec_id: Callable[[], str | None],
        on_status=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self._selected_spec_id = selected_spec_id
        self._on_status = on_status
        self._pool = NativeWorkerPool(self)
        self._disposed = False
        self._candidates: tuple[RobustnessCandidateChoice, ...] = ()
        self._axes: tuple[RobustnessAxisChoice, ...] = ()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.spec_label = QLabel()
        self.spec_label.setWordWrap(True)
        layout.addWidget(self.spec_label)

        form = QFormLayout()
        self.candidate_combo = QComboBox(self)
        self.candidate_combo.setToolTip(
            'ばらつき評価の対象にする候補（既に評価済みのもの）です。'
        )
        self.candidate_combo.currentIndexChanged.connect(
            self._candidate_changed
        )
        form.addRow('評価済み候補', self.candidate_combo)
        self.delta_m_spin = QDoubleSpinBox(self)
        self.delta_m_spin.setRange(0.001, 1.0)
        self.delta_m_spin.setDecimals(3)
        self.delta_m_spin.setSingleStep(0.01)
        self.delta_m_spin.setValue(DEFAULT_POSITION_DELTA_M)
        self.delta_m_spin.setSuffix(' m')
        self.delta_m_spin.setToolTip(
            '評価点を基準位置からどれだけずらすか（m）。'
            '現実の設置誤差を想定した値を指定します。'
        )
        form.addRow('位置の揺らぎ ±', self.delta_m_spin)
        self.delta_deg_spin = QDoubleSpinBox(self)
        self.delta_deg_spin.setRange(0.1, 45.0)
        self.delta_deg_spin.setDecimals(1)
        self.delta_deg_spin.setSingleStep(0.5)
        self.delta_deg_spin.setValue(DEFAULT_ANGLE_DELTA_DEG)
        self.delta_deg_spin.setSuffix(' °')
        self.delta_deg_spin.setToolTip(
            '評価点を基準角度からどれだけずらすか（°）。'
            'スピーカーの向きの誤差を想定した値を指定します。'
        )
        form.addRow('角度の揺らぎ ±', self.delta_deg_spin)
        layout.addLayout(form)

        axis_note = QLabel(
            'サンプル数は 1 + 2 × 軸数です（既定: 2軸 → 5サンプル）。'
        )
        axis_note.setWordWrap(True)
        set_typography_role(axis_note, TypographyRole.SECONDARY)
        layout.addWidget(axis_note)

        self.axis_tree = QTreeWidget(self)
        self.axis_tree.setHeaderLabels(('軸', '基準値'))
        self.axis_tree.setToolTip(
            'ばらつかせる軸の一覧です。チェックした軸について、'
            '基準値の±揺らぎの位置で評価点を生成します。'
        )
        _axis_header = self.axis_tree.headerItem()
        _axis_header.setToolTip(0, 'ばらつかせる方向・角度の軸')
        _axis_header.setToolTip(1, 'その軸の基準配置での値')
        self.axis_tree.setRootIsDecorated(False)
        self.axis_tree.itemChanged.connect(self._axis_changed)
        layout.addWidget(self.axis_tree)

        actions = QHBoxLayout()
        self.run_button = QPushButton('ばらつき評価を作成・実行', self)
        self.run_button.setToolTip(
            '指定した揺らぎ範囲で評価点を生成し、指標がどれだけ変わるか計算します。'
        )
        self.run_button.clicked.connect(self._run)
        actions.addWidget(self.run_button)
        self.cancel_button = QPushButton('中止', self)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.refresh()

    # ------------------------------------------------------------------
    # Workspace lifecycle
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        return self._pool.active_count > 0

    def stop(self) -> WorkerShutdownReport:
        """Drain in-flight authoring but keep the panel usable (#REV19/D1)."""
        report = self._pool.stop_all()
        self.status_label.setText(
            'ばらつき評価を中止しました'
            if report.all_stopped
            else 'ばらつき評価の停止が遅延しています · 遅延結果は適用しません'
        )
        self._refresh_run_state()
        return report

    def dispose(self) -> None:
        self._disposed = True
        report = self._pool.shutdown()
        if not report.all_stopped and self._on_status is not None:
            self._on_status(
                'ばらつき評価の停止が遅延しています · 遅延結果は適用しません'
            )

    def refresh(self) -> None:
        """Rebind to the currently selected SearchSpec."""
        if self.is_running():
            return
        spec_id = self._selected_spec_id()
        spec = (
            None
            if spec_id is None
            else self.context.search_repository.get(spec_id)
        )
        self._candidates = ()
        self._axes = ()
        self.candidate_combo.blockSignals(True)
        self.candidate_combo.clear()
        self.candidate_combo.blockSignals(False)
        self.axis_tree.blockSignals(True)
        self.axis_tree.clear()
        self.axis_tree.blockSignals(False)
        if spec is None:
            self.spec_label.setText(
                '探索設定を選択してください。ばらつき評価は保存済み探索設定と'
                '評価済み候補に固定されています。'
            )
            self._refresh_run_state()
            return

        self.spec_label.setText(
            f'対象探索設定: {spec.name or spec.search_spec_id[:12]}'
        )
        try:
            self._candidates = self.context.candidate_choices(spec)
        except Exception as exc:
            self.status_label.setText(
                f'候補を読み込めません: {operation_error_message(exc)}'
            )
            self._refresh_run_state()
            return
        if not self._candidates:
            self.status_label.setText(
                'この探索設定には評価済み候補がありません。'
                '先に候補評価を実行してください。'
            )
            self._refresh_run_state()
            return
        self.candidate_combo.blockSignals(True)
        for index, choice in enumerate(self._candidates):
            self.candidate_combo.addItem(choice.label, index)
        self.candidate_combo.blockSignals(False)
        self.candidate_combo.setCurrentIndex(0)
        self._reload_axes()
        self._refresh_run_state()

    # ------------------------------------------------------------------
    # Internal UI plumbing
    # ------------------------------------------------------------------

    def _spec(self):
        spec_id = self._selected_spec_id()
        if spec_id is None:
            return None
        return self.context.search_repository.get(spec_id)

    def _candidate_changed(self, _index: int) -> None:
        self._reload_axes()
        self._refresh_run_state()

    def _reload_axes(self) -> None:
        self._axes = ()
        self.axis_tree.blockSignals(True)
        self.axis_tree.clear()
        self.axis_tree.blockSignals(False)
        spec = self._spec()
        index = self.candidate_combo.currentData()
        if spec is None or index is None or not self._candidates:
            return
        choice = self._candidates[int(index)]
        try:
            self._axes = self.context.axis_choices(
                spec, choice.candidate_id
            )
        except Exception as exc:
            self.status_label.setText(
                f'軸を読み込めません: {operation_error_message(exc)}'
            )
            return
        self.axis_tree.blockSignals(True)
        for index, axis in enumerate(self._axes):
            item = QTreeWidgetItem(
                (axis.label, f'{axis.nominal_value:.4g} {axis.unit}')
            )
            item.setData(0, ROLE, index)
            item.setFlags(
                item.flags() | Qt.ItemFlag.ItemIsUserCheckable
            )
            # Default: first two axes checked — the bounded ±5 cm × 2 axes
            # stencil (5 samples) the authoring default commits to.
            item.setCheckState(
                0,
                Qt.CheckState.Checked
                if index < 2
                else Qt.CheckState.Unchecked,
            )
            self.axis_tree.addTopLevelItem(item)
        self.axis_tree.blockSignals(False)
        if not self._axes:
            self.status_label.setText(
                'この候補に揺らせる軸がありません。'
            )

    def _axis_changed(self, _item: QTreeWidgetItem, _column: int) -> None:
        self._refresh_run_state()

    def _selected_axes(self) -> tuple[RobustnessAxisChoice, ...]:
        selected: list[RobustnessAxisChoice] = []
        for row in range(self.axis_tree.topLevelItemCount()):
            item = self.axis_tree.topLevelItem(row)
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            index = item.data(0, ROLE)
            if isinstance(index, int) and index < len(self._axes):
                selected.append(self._axes[index])
        return tuple(selected)

    def _refresh_run_state(self) -> None:
        if self._disposed:
            return
        running = self.is_running()
        self.cancel_button.setEnabled(running)
        if running:
            self.run_button.setEnabled(False)
            self.run_button.setToolTip('実行中です。')
            return
        choices = self._selected_axes()
        enabled = bool(self._candidates) and bool(choices)
        self.run_button.setEnabled(enabled)
        if not self._candidates:
            self.run_button.setToolTip(
                '評価済み候補がないため実行できません。'
            )
        elif not choices:
            self.run_button.setToolTip('揺らす軸を1つ以上選択してください。')
        else:
            self.run_button.setToolTip(
                'O90ばらつき評価仕様を保存し、'
                f'{1 + 2 * len(choices)}サンプルを実行します。'
            )

    # ------------------------------------------------------------------
    # Worker path
    # ------------------------------------------------------------------

    def _cancel(self) -> None:
        self._pool.cancel_all()

    def _run(self) -> None:
        if self._disposed:
            return
        spec = self._spec()
        index = self.candidate_combo.currentData()
        if spec is None or index is None or not self._candidates:
            return
        choice = self._candidates[int(index)]
        axes = tuple(
            axis.to_axis(
                self.delta_m_spin.value()
                if axis.unit == 'm'
                else self.delta_deg_spin.value()
            )
            for axis in self._selected_axes()
        )
        if not axes:
            self.status_label.setText('揺らす軸を1つ以上選択してください。')
            return
        key = f'robustness-authoring:{uuid4()}'
        self.status_label.setText('ばらつき評価仕様を作成しています…')
        self._refresh_run_state()
        self._pool.start(
            key,
            lambda cancel_event: self._run_authoring(
                spec, choice.candidate_id, axes, cancel_event.is_set
            ),
            self._run_completed,
            on_finished=lambda _key: self._refresh_run_state(),
        )

    def _run_authoring(
        self,
        spec,
        candidate_id: str,
        axes,
        is_cancelled: Callable[[], bool],
    ) -> tuple[object, int, int]:
        robustness_spec = self.context.create_spec(
            search_spec=spec,
            candidate_id=candidate_id,
            axes=axes,
            cancelled=is_cancelled,
        )
        samples, evaluations = self.context.evaluate_spec(
            robustness_spec,
            is_cancelled=is_cancelled,
        )
        return robustness_spec, len(samples), len(evaluations)

    def _run_completed(self, key: object, result: object, error: object) -> None:
        if self._disposed:
            return
        if error == WORKER_CANCELLED:
            self.status_label.setText(
                '評価を中止しました · 作成済みの仕様は保存されています'
            )
            return
        if error is not None:
            self.status_label.setText(
                f'ばらつき評価を作成できません: {operation_error_message(error)}'
            )
            return
        spec, sample_count, evaluation_count = result  # type: ignore[misc]
        self.status_label.setText(
            f'ばらつき評価を保存しました · サンプル {sample_count} 件 · '
            f'評価 {evaluation_count} 件'
        )
        if self._on_status is not None:
            self._on_status(
                'O90ばらつき評価を作成・実行しました · '
                'ジョイント最適化で利用できます'
            )
        self.evaluationCompleted.emit(spec)


__all__ = ['RobustnessAuthoringPanel']
