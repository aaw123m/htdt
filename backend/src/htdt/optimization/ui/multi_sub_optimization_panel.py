"""REV55-MULTISUB comparison-surface panel (#569).

Read-only view over the persisted conventional multi-sub optimization
authorities: per-candidate seat-population evaluations expose the banded
seat-to-seat metrics (mean across-seat std, spread, worst-seat deviation,
raw per-seat band means) so the seat-consistency figure is visible beside —
never merged into — the Pareto comparison.

The panel never ranks candidates and never derives a "winner"; a
qualification's stored verdict and reasons are shown verbatim.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..domain.cad_multi_sub_optimization import (
    MultiSubEvaluation,
    MultiSubQualification,
)
from ..persistence.cad_multi_sub_optimization_repository import (
    CadMultiSubOptimizationRepository,
)
from ...cad_repository import SceneRepository
from ...ui_theme import (
    SurfaceRole,
    TypographyRole,
    set_surface_role,
    set_typography_role,
)


_STRATEGY_JA = {
    'single_sub_baseline': '単一サブ基準',
    'multi_sub_fixed_layout': '複数サブ（固定配置）',
    'multi_sub_placement_optimized': '複数サブ（配置最適化）',
    'multi_sub_gain_delay_polarity_optimized': '複数サブ（ゲイン/遅延/極性最適化）',
    'multi_sub_with_bounded_eq': '複数サブ（有界EQ併用）',
}
_POPULATION_JA = {
    'optimization': '最適化座席',
    'holdout': 'ホールドアウト座席',
    'repeatability': '再現性位置',
}
_VERDICT_JA = {
    'qualified': '適合',
    'qualified_with_limitations': '限定付き適合',
    'unqualified_insufficient_evidence': '証拠不足',
    'failed_holdout_regression': 'ホールドアウト退行',
    'incomparable_fidelity': '忠実度不一致（比較不可）',
}
_CLAIM_JA = {
    'single_seat': '単一座席',
    'optimization_seats': '最適化座席群',
    'listening_region': 'リスニング領域',
}


def _fmt(value: float | None, unit: str = ' dB') -> str:
    if value is None:
        return '不明'
    return f'{value:.2f}{unit}'


class MultiSubBaselinePanel(QFrame):
    """Seat-to-seat variation surface for conventional multi-sub records."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = CadMultiSubOptimizationRepository(scene_repository)
        self.document_id = document_id
        self._evaluations: dict[str, MultiSubEvaluation] = {}
        self._qualifications: tuple[MultiSubQualification, ...] = ()
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        title = QLabel('マルチサブ 座席間ばらつき評価')
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        note = QLabel(
            '従来型マルチサブ最適化（MULTI_SUB_SUM_OPTIMIZATION）の記録済み評価を表示します。'
            '帯域ごとの座席間標準偏差・スプレッド・最悪座席偏差と各座席の帯域平均を'
            '独立して並べ、総合点や推奨順位は作りません。'
            '座席間ばらつきの低減は見えた座席群でのみ確認できます。'
        )
        note.setWordWrap(True)
        set_typography_role(note, TypographyRole.SECONDARY)
        layout.addWidget(note)

        controls = QHBoxLayout()
        controls.addWidget(QLabel('評価'))
        self.evaluation_combo = QComboBox()
        self.evaluation_combo.setToolTip(
            '候補×座席群ごとの記録済み評価です。証拠種別（実測/予測/合成）は各行に表示されます。'
        )
        self.evaluation_combo.currentIndexChanged.connect(self.refresh)
        controls.addWidget(self.evaluation_combo, 1)
        layout.addLayout(controls)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setObjectName('multiSubBaselineTree')
        self.tree.setToolTip(
            '帯域ごとの座席間ばらつき指標です。各座席の帯域平均は個別列に残り、'
            '加重平均や単一スコアに隠れません。'
        )
        self.tree.setHeaderLabels(
            [
                '帯域 (Hz)',
                '座席間標準偏差',
                '最大-最小',
                '最悪座席偏差',
                '各座席の帯域平均 (dB)',
            ]
        )
        header = self.tree.header()
        for index in range(self.tree.columnCount()):
            header.setSectionResizeMode(
                index,
                QHeaderView.ResizeMode.ResizeToContents
                if index < 4
                else QHeaderView.ResizeMode.Stretch,
            )
        layout.addWidget(self.tree, 1)

        self.qualification_label = QLabel()
        self.qualification_label.setWordWrap(True)
        layout.addWidget(self.qualification_label)

        self._load()

    def _load(self) -> None:
        evaluations = self.repository.list_evaluations_for_document(
            self.document_id
        )
        self._evaluations = {
            evaluation.evaluation_id: evaluation for evaluation in evaluations
        }
        self._qualifications = self.repository.list_qualifications(
            self.document_id
        )
        current = self.evaluation_combo.currentData()
        self.evaluation_combo.blockSignals(True)
        self.evaluation_combo.clear()
        candidates = {
            candidate.candidate_id: candidate
            for candidate in self.repository.list_candidates(self.document_id)
        }
        for evaluation in evaluations:
            candidate = candidates.get(evaluation.candidate_id)
            strategy = (
                _STRATEGY_JA.get(candidate.strategy, candidate.strategy)
                if candidate is not None
                else evaluation.candidate_id
            )
            self.evaluation_combo.addItem(
                f'{strategy} · {_POPULATION_JA.get(evaluation.population, evaluation.population)}'
                f' · {len(evaluation.seat_bindings)}座席',
                evaluation.evaluation_id,
            )
        if current is not None:
            index = self.evaluation_combo.findData(current)
            if index >= 0:
                self.evaluation_combo.setCurrentIndex(index)
        self.evaluation_combo.blockSignals(False)
        self.refresh()

    def refresh(self, *_args) -> None:
        evaluation_id = self.evaluation_combo.currentData()
        evaluation = (
            self._evaluations.get(evaluation_id)
            if evaluation_id is not None
            else None
        )
        self.tree.clear()
        if evaluation is None:
            self.evaluation_combo.setVisible(
                self.evaluation_combo.count() > 0
            )
            self.summary.setText(
                '記録済みのマルチサブ評価はまだありません。'
                if self.evaluation_combo.count() == 0
                else '評価を選択してください。'
            )
            self.qualification_label.setText('')
            return
        kinds = sorted(
            {binding.evidence_kind for binding in evaluation.seat_bindings}
        )
        self.summary.setText(
            '座席: '
            + ', '.join(
                binding.seat_entity_id for binding in evaluation.seat_bindings
            )
            + f'　証拠: {", ".join(kinds)}'
            + f'　評価帯域: {evaluation.actual_band_hz[0]:.0f}–{evaluation.actual_band_hz[1]:.0f} Hz'
        )
        for metric in evaluation.band_metrics:
            item = QTreeWidgetItem()
            item.setText(
                0, f'{metric.band_hz[0]:.0f}–{metric.band_hz[1]:.0f}'
            )
            item.setText(1, _fmt(metric.mean_std_db))
            item.setText(2, _fmt(metric.spread_db))
            item.setText(3, _fmt(metric.worst_seat_deviation_db))
            seat_cells = [
                f'{seat} {level:.2f}'
                for seat, level in zip(
                    (
                        binding.seat_entity_id
                        for binding in evaluation.seat_bindings
                    ),
                    metric.per_seat_mean_db,
                )
            ]
            item.setText(4, ', '.join(seat_cells))
            self.tree.addTopLevelItem(item)
        related = [
            q
            for q in self._qualifications
            if evaluation.candidate_id
            in (q.candidate_id, q.baseline_candidate_id)
        ]
        if related:
            latest = related[-1]
            lines = [
                f'資格判定: {_VERDICT_JA.get(latest.verdict, latest.verdict)}'
                f'（主張: {_CLAIM_JA.get(latest.claim, latest.claim)} / '
                f'有効範囲: {_CLAIM_JA.get(latest.effective_claim, latest.effective_claim)}）'
            ]
            blocking = [r for r in latest.reasons if r.blocking]
            for reason in blocking[:3]:
                lines.append(f'· {reason.detail}')
            self.qualification_label.setText('\n'.join(lines))
        else:
            self.qualification_label.setText(
                'この候補の資格判定はまだ記録されていません。'
            )
