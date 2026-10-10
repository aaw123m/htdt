"""Read-only pre-run compute cost card (issue #991).

Shown on the prediction and optimization run surfaces before the
operator commits a run. Renders one sealed ``PrerunEstimate`` — solver,
backend, accuracy drivers, runtime/memory/storage ranges, budget and
fidelity verdicts — plus any past ``ComputeObservation`` measured for
the same plan. The card never launches work and never fabricates: an
unestimable metric renders as 不明 with its reason.
"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from .cad_compute_budget import (
    BUDGET_LABELS,
    ComputeObservation,
    FIDELITY_CLAIM_LABELS,
)
from .cad_prerun_estimate import (
    AXIS_LABELS,
    AXIS_STATUS_LABELS,
    BLOCKING_REASON_LABELS,
    CONFIDENCE_LABELS,
    JOB_KIND_LABELS,
    WARNING_LABELS,
    PrerunEstimate,
)
from .ui_theme import (
    SemanticState,
    TypographyRole,
    set_semantic_state,
    set_typography_role,
)


def _format_bytes(value: float) -> str:
    if value >= 1e9:
        return f'{value / 1e9:.1f}GB'
    if value >= 1e6:
        return f'{value / 1e6:.1f}MB'
    if value >= 1e3:
        return f'{value / 1e3:.1f}KB'
    return f'{value:.0f}B'


def _format_seconds(value: float) -> str:
    if value >= 120:
        return f'{value / 60:.1f}分'
    if value >= 1.0:
        return f'{value:.1f}秒'
    return f'{value * 1000:.0f}ms'


def _format_range(metric) -> str:
    if metric is None:
        return '不明（校正範囲外）'
    lo = metric.minimum
    hi = metric.maximum
    if metric.basis == 'extrapolated_model':
        tag = '外挿モデル'
    elif metric.basis == 'assumed':
        tag = '仮定値'
    elif metric.basis == 'vendor_documented':
        tag = 'ベンダー文書'
    else:
        tag = '不明'
    suffix = '（外挿）' if metric.extrapolated else ''
    return f'{_format_seconds(lo)}–{_format_seconds(hi)} [{tag}{suffix}]'


def _format_memory_range(metric) -> str:
    if metric is None:
        return '不明（校正範囲外）'
    suffix = '（外挿）' if metric.extrapolated else ''
    return (
        f'{_format_bytes(metric.minimum)}–{_format_bytes(metric.maximum)}'
        f'{suffix}'
    )


class PrerunCostCard(QFrame):
    """Compact estimate surface: config summary + verdicts + warnings."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName('prerunCostCard')
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._estimate: PrerunEstimate | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        self.title = QLabel('実行前の計算コスト見積もり', self)
        set_typography_role(self.title, TypographyRole.SECONDARY)
        layout.addWidget(self.title)

        self.body = QLabel(self)
        self.body.setWordWrap(True)
        layout.addWidget(self.body)

        self.notice = QLabel(self)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)

        self.clear()

    @property
    def estimate(self) -> PrerunEstimate | None:
        return self._estimate

    @property
    def blocking(self) -> bool:
        return bool(
            self._estimate is not None and self._estimate.blocking_reasons
        )

    def clear(self) -> None:
        self._estimate = None
        self.body.setText('実行条件を選ぶと計算コスト見積もりを表示します')
        self.notice.setText('')
        set_typography_role(self.body, TypographyRole.SECONDARY)

    def show_unavailable(self, message: str) -> None:
        """Fail-closed display: estimation itself could not run."""
        self._estimate = None
        self.body.setText(f'見積もりを作成できません: {message}')
        self.notice.setText('')
        set_semantic_state(self.body, SemanticState.ERROR)

    def show_estimate(
        self,
        estimate: PrerunEstimate,
        observations: tuple[ComputeObservation, ...] = (),
    ) -> None:
        self._estimate = estimate
        plan = estimate.plan

        config_parts = [
            JOB_KIND_LABELS.get(plan.job_kind, plan.job_kind),
            f'バックエンド: {plan.backend_label}',
        ]
        if plan.solver_version:
            config_parts.append(f'バージョン: {plan.solver_version}')
        axis_lines = []
        for name, value in sorted(plan.axes.items()):
            label = AXIS_LABELS.get(name, name)
            status = AXIS_STATUS_LABELS.get(
                estimate.axis_statuses.get(name, 'unmodeled'), ''
            )
            shown = f'{value:g}'
            axis_lines.append(f'{label}={shown}（{status}）')

        lines = [
            '構成: ' + ' / '.join(config_parts),
            '条件: ' + '、'.join(axis_lines),
            (
                '推定時間: '
                + _format_range(estimate.runtime_s)
            ),
            '推定ピークメモリ: '
            + _format_memory_range(estimate.peak_memory_bytes),
            '推定ストレージ: '
            + _format_memory_range(estimate.storage_bytes),
            (
                '予算判定: '
                + BUDGET_LABELS.get(
                    estimate.budget_verdict, estimate.budget_verdict
                )
                + ' · 精度コスト証拠: '
                + FIDELITY_CLAIM_LABELS.get(
                    estimate.fidelity_verdict, estimate.fidelity_verdict
                )
                + ' · 信頼度: '
                + CONFIDENCE_LABELS.get(
                    estimate.confidence, estimate.confidence
                )
            ),
        ]
        if observations:
            latest = observations[-1]
            observed = latest.metrics.get('runtime_s')
            rss = latest.metrics.get('process_rss_bytes')
            observed_parts = []
            if observed is not None:
                observed_parts.append(
                    f'実測時間 {_format_seconds(observed)}'
                )
            if rss is not None:
                observed_parts.append(
                    f'完了時RSS {_format_bytes(rss)}'
                )
            predicted = (
                _format_range(estimate.runtime_s)
                if estimate.runtime_s is not None
                else '不明'
            )
            lines.append(
                '同一計画の過去実測: '
                + '、'.join(observed_parts)
                + f'（推定 {predicted}、観測 {len(observations)} 件）'
            )
        self.body.setText('\n'.join(lines))
        set_typography_role(self.body, TypographyRole.BODY)

        notice_parts: list[str] = []
        if estimate.blocking_reasons:
            notice_parts.extend(
                '実行不可: '
                + BLOCKING_REASON_LABELS.get(reason, reason)
                for reason in estimate.blocking_reasons
            )
        if estimate.out_of_range_reasons:
            notice_parts.append(
                '範囲外: ' + ' / '.join(estimate.out_of_range_reasons)
            )
        notice_parts.extend(
            WARNING_LABELS.get(warning, warning)
            for warning in estimate.warnings
        )
        self.notice.setText('\n'.join(notice_parts))
        if estimate.blocking_reasons:
            set_semantic_state(self.notice, SemanticState.ERROR)
        elif estimate.warnings or estimate.out_of_range_reasons:
            set_semantic_state(self.notice, SemanticState.WARNING)
        else:
            set_typography_role(self.notice, TypographyRole.SECONDARY)
