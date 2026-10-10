"""Optimize > 配置 + DSP panel (#524 spec authoring, #945 execution).

Thin widget over :class:`JointOptimizationContext`: it resolves the exact
baseline authorities itself (no typed ids/hashes), gates DSP variables by
measurement/device capability, shows the candidate-count preflight, saves
canonical ``JointOptimizationSpec`` records, and executes a selected spec
through the bounded canonical execution pass — materialized
SystemVariant/CalibrationPlan candidates and persisted evaluation
bindings included. Execution mutates neither the room nor device settings —
apply/export stays with the existing SystemVariant / CalibrationPlan
lifecycle (#452).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...cad_display_labels import (
    format_versioned_label,
    revision_display_label,
    saved_label,
    spec_display_label,
)
from ...worker_pool import (
    WORKER_CANCELLED,
    NativeWorkerPool,
    WorkerShutdownReport,
)
from ..domain.cad_joint_optimization import JointDspVariable
from ...prerun_cost_card import PrerunCostCard
from ..services.joint_optimization_context import (
    DEFAULT_MAGNITUDE_BAND_HZ,
    JointBaseline,
    JointOptimizationContext,
    JointSearchMode,
    _UNSET_BASELINE,
)
from ...ui_theme import (
    SemanticState,
    TypographyRole,
    set_semantic_state,
    set_typography_role,
)
from ...error_boundary import (
    EXPECTED_OPERATION_ERRORS,
    is_authority_failure,
    report_boundary_failure,
)
from ...user_facing_error import operation_error_message


_MODE_ITEMS: tuple[tuple[JointSearchMode, str], ...] = (
    ('placement_only', '配置のみ'),
    ('dsp_only', 'DSPのみ'),
    ('joint', '配置 + DSP'),
)

_MODE_LABELS: dict[str, str] = {mode: label for mode, label in _MODE_ITEMS}

_STALE_REASON_LABELS: dict[str, str] = {
    'baseline_unresolved': 'ベースライン未解決',
    'scene_revision_changed': '部屋リビジョン変更',
    'scene_content_changed': '部屋内容変更',
    'base_system_variant_changed': '基準バリアント変更',
    'base_calibration_plan_missing': '基準校正プランなし',
    'base_calibration_plan_changed': '基準校正プラン変更',
    'assessment_failed': '状態判定失敗',
}

_DSP_NUMERIC_DEFAULTS: dict[str, tuple[float, float, float, str]] = {
    # parameter -> (min, max, step, unit label)
    'gain_db': (-6.0, 6.0, 0.5, 'dB'),
    'peq_frequency_hz': (20.0, 200.0, 5.0, 'Hz'),
    'peq_q': (0.5, 8.0, 0.5, ''),
    'peq_gain_db': (-12.0, 6.0, 1.0, 'dB'),
    'crossover_frequency_hz': (60.0, 120.0, 5.0, 'Hz'),
    'crossover_order': (2.0, 4.0, 1.0, ''),
    'delay_s': (0.0, 0.02, 0.0005, 's'),
}


class JointOptimizationPanel(QWidget):
    """Workflow-first authoring of #174 joint placement+DSP search specs."""

    progressChanged = Signal(object)  # JointExecutionResult, worker thread

    def __init__(
        self,
        context: JointOptimizationContext,
        *,
        on_status=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self._on_status = on_status
        self._baseline: JointBaseline | None = None
        self._dsp_rows: dict[str, dict] = {}
        self._pool = NativeWorkerPool(self)
        self._disposed = False
        self.progressChanged.connect(self._on_execution_progress)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.baseline_label = QLabel()
        self.baseline_label.setWordWrap(True)
        layout.addWidget(self.baseline_label)

        mode_form = QFormLayout()
        self.mode_combo = QComboBox(self)
        for _mode, label in _MODE_ITEMS:
            self.mode_combo.addItem(label, _mode)
        self.mode_combo.setToolTip(
            '配置とDSP設定をどう組み合わせて探索するかを選びます。'
        )
        self.mode_combo.currentIndexChanged.connect(self._refresh_preflight)
        mode_form.addRow('探索モード', self.mode_combo)
        self.budget_spin = QSpinBox(self)
        self.budget_spin.setRange(1, 50_000)
        self.budget_spin.setValue(64)
        self.budget_spin.setToolTip(
            '1回の実行で評価する候補の最大数です。'
            '大きいほど網羅的になりますが計算時間が増えます。'
        )
        self.budget_spin.valueChanged.connect(self._refresh_preflight)
        mode_form.addRow('候補上限（予算）', self.budget_spin)
        layout.addLayout(mode_form)

        dsp_heading = QLabel(
            'DSP変数 — 測定能力とデバイス制約が許す項目だけ選択できます。'
        )
        dsp_heading.setWordWrap(True)
        set_typography_role(dsp_heading, TypographyRole.SECONDARY)
        layout.addWidget(dsp_heading)

        self.dsp_host = QWidget(self)
        self.dsp_layout = QFormLayout(self.dsp_host)
        self.dsp_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.dsp_host)

        self.preflight_label = QLabel()
        self.preflight_label.setWordWrap(True)
        layout.addWidget(self.preflight_label)

        # #991: pre-run compute-cost card — shows the estimate for the
        # spec the authoring controls would create, or the selected
        # persisted spec, before the operator commits a run.
        self.prerun_card = PrerunCostCard(self)
        layout.addWidget(self.prerun_card)

        actions = QHBoxLayout()
        self.create_button = QPushButton('ジョイント最適化仕様を保存', self)
        self.create_button.setToolTip(
            '現在の探索モード・DSP変数・候補上限を仕様として保存します。'
            '保存後に一覧から選んで実行します。'
        )
        self.create_button.clicked.connect(self._create_spec)
        actions.addWidget(self.create_button)
        self.execute_button = QPushButton('選択した仕様を実行', self)
        self.execute_button.setEnabled(False)
        self.execute_button.setToolTip(
            '保存済みの仕様を選択すると実行できます。'
        )
        self.execute_button.clicked.connect(self._execute_selected)
        actions.addWidget(self.execute_button)
        self.cancel_button = QPushButton('実行を中止', self)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_execution)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.spec_tree = QTreeWidget(self)
        self.spec_tree.setAccessibleName('結合最適化仕様')
        self.spec_tree.setColumnCount(5)
        self.spec_tree.setHeaderLabels(
            ('仕様', 'モード', 'DSP変数', '候補上限', '状態')
        )
        self.spec_tree.setToolTip(
            '保存済みのジョイント最適化仕様です。選択すると実行できます。'
        )
        joint_header = self.spec_tree.headerItem()
        for _col, _tip in {
            0: '仕様の名前',
            1: '配置とDSP設定の探索方法',
            2: '探索対象に含まれるDSP変数',
            3: '1回の実行で評価する候補の最大数',
            4: 'この仕様が現在実行可能かどうか',
        }.items():
            joint_header.setToolTip(_col, _tip)
        self.spec_tree.setRootIsDecorated(False)
        self.spec_tree.itemSelectionChanged.connect(self._on_spec_selection)
        layout.addWidget(self.spec_tree)

        self.execution_label = QLabel()
        self.execution_label.setWordWrap(True)
        layout.addWidget(self.execution_label)

        self.refresh()

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self._baseline = self.context.resolve_baseline()
        baseline = self._baseline
        if baseline is None:
            self.baseline_label.setText(
                'ベースライン未解決: 現在のシーンリビジョン・システムバリアント・'
                '物理探索設定が必要です。'
            )
            self._build_dsp_rows(())
            self.create_button.setEnabled(False)
            self.preflight_label.setText('')
            self._refresh_saved_specs()
            self._refresh_execution_state()
            return

        plan = baseline.calibration_plan
        report = baseline.quality_report
        robustness = baseline.robustness_spec
        revision_labels = self.context.repository.revision_labels(
            self.context.document_id
        )
        self.baseline_label.setText(
            'ベースライン: 部屋リビジョン '
            f'{revision_display_label(baseline.scene_revision, revision_labels)} / '
            f'バリアント {baseline.base_variant.name} / '
            f'物理探索 '
            f'{spec_display_label(baseline.physical_search_spec.name, baseline.physical_search_spec.created_at_utc)} / '
            f'CalibrationPlan '
            + (
                format_versioned_label('v', plan.plan_version, plan.created_at_utc)
                if plan is not None
                else 'なし'
            )
            + ' / 品質レポート '
            + (
                saved_label(report.created_at_utc)
                if report is not None
                else 'なし'
            )
            + ' / O90 '
            + (
                saved_label(robustness.created_at_utc)
                if robustness is not None
                else 'なし'
            )
        )
        options = self.context.dsp_variable_options(baseline)
        self._build_dsp_rows(options)
        self._refresh_preflight()
        self._refresh_saved_specs()
        self._refresh_execution_state()

    def _build_dsp_rows(self, options) -> None:
        while self.dsp_layout.count():
            item = self.dsp_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._dsp_rows = {}
        for option in options:
            checkbox = QCheckBox(
                f'{option.label_ja}（要: {option.required_claim_ja}）', self
            )
            checkbox.setEnabled(option.enabled)
            if not option.enabled:
                tooltip = option.reason_ja
                checkbox.setToolTip(tooltip)
                note = QLabel(option.reason_ja, self)
                set_semantic_state(note, SemanticState.WARNING)
                note.setWordWrap(True)
            elif option.device_limit_note_ja:
                note = QLabel(option.device_limit_note_ja, self)
                note.setWordWrap(True)
            else:
                note = QLabel('有効', self)
            row = QHBoxLayout()
            row.addWidget(checkbox)
            row.addWidget(note, 1)
            bounds: dict[str, QDoubleSpinBox] = {}
            defaults = _DSP_NUMERIC_DEFAULTS.get(option.parameter)
            if defaults is not None:
                _bound_tips = {
                    'min': f'{option.label_ja} の探索下限です。',
                    'max': f'{option.label_ja} の探索上限です。',
                    'step': f'{option.label_ja} の探索刻みです。',
                }
                for key, value in zip(
                    ('min', 'max', 'step'), defaults[:3]
                ):
                    spin = QDoubleSpinBox(self)
                    spin.setDecimals(4)
                    spin.setRange(-1_000_000.0, 1_000_000.0)
                    spin.setValue(value)
                    spin.setEnabled(option.enabled)
                    spin.setToolTip(_bound_tips[key])
                    bounds[key] = spin
                    row.addWidget(spin)
            host = QWidget(self)
            host.setLayout(row)
            self.dsp_layout.addRow(option.label_ja, host)
            checkbox.stateChanged.connect(self._refresh_preflight)
            for spin in bounds.values():
                spin.valueChanged.connect(self._refresh_preflight)
            self._dsp_rows[option.parameter] = {
                'option': option,
                'checkbox': checkbox,
                'bounds': bounds,
            }

    def _mode(self) -> JointSearchMode:
        return self.mode_combo.currentData()

    def _selected_dsp_variables(self) -> tuple[JointDspVariable, ...]:
        variables: list[JointDspVariable] = []
        baseline = self._baseline
        channel_id = 'ch-1'
        if baseline is not None and baseline.calibration_plan is not None:
            channels = baseline.calibration_plan.channels
            if channels:
                channel_id = channels[0].channel_id
        for parameter, row in self._dsp_rows.items():
            checkbox: QCheckBox = row['checkbox']
            if not (checkbox.isEnabled() and checkbox.isChecked()):
                continue
            bounds = row['bounds']
            if parameter == 'polarity':
                variables.append(
                    JointDspVariable(
                        variable_id=f'dsp:{channel_id}:polarity',
                        channel_id=channel_id,
                        parameter='polarity',
                        allowed_values=('normal', 'inverted'),
                        required_measurement_claim='polarity',
                    )
                )
                continue
            minimum = bounds['min'].value()
            maximum = bounds['max'].value()
            step = bounds['step'].value()
            filter_id = (
                'peq-1' if parameter.startswith('peq_') else None
            )
            crossover_index = (
                0 if parameter.startswith('crossover_') else None
            )
            variables.append(
                JointDspVariable(
                    variable_id=f'dsp:{channel_id}:{parameter}',
                    channel_id=channel_id,
                    parameter=parameter,
                    filter_id=filter_id,
                    crossover_index=crossover_index,
                    minimum=minimum,
                    maximum=maximum,
                    step=step,
                    required_measurement_claim=(
                        'common_timing'
                        if parameter == 'delay_s'
                        else 'polarity'
                        if parameter == 'polarity'
                        else 'magnitude_response'
                    ),
                    required_band_hz=(
                        DEFAULT_MAGNITUDE_BAND_HZ
                        if parameter not in ('delay_s', 'polarity')
                        else None
                    ),
                )
            )
        return tuple(variables)

    def _refresh_preflight(self) -> None:
        baseline = self._baseline
        if baseline is None:
            self.preflight_label.setText('')
            self.create_button.setEnabled(False)
            return
        mode = self._mode()
        dsp_variables = self._selected_dsp_variables()
        try:
            estimate = self.context.estimate_candidates(
                baseline,
                mode=mode,
                dsp_variables=dsp_variables,
                candidate_budget=self.budget_spin.value(),
            )
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: advisory preflight — expected estimate errors surface with reason and keep the create disabled; unexpected errors propagate to diagnostics
            self.preflight_label.setText(f'候補数を推定できません: {operation_error_message(exc)}')
            self.create_button.setEnabled(False)
            return
        state = (
            '予算内'
            if estimate.within_budget
            else '予算超過 — 範囲を狭めるか予算を増やしてください'
        )
        # create_spec fail-closes when the joint lane lacks its required
        # authorities — surface the exact reason here instead of letting the
        # button reach the error path (#4: the O90 dead-end was illegible).
        missing: list[str] = []
        if baseline.robustness_spec is None:
            missing.append(
                'O90ばらつき評価仕様（ばらつき耐性ページで作成・実行）'
            )
        if not baseline.objective_definitions:
            missing.append('O30目標評価')
        detail = ''
        if missing:
            detail = ' · 不足権威: ' + ' / '.join(missing)
        self.preflight_label.setText(
            '候補数の見積もり: 物理 '
            f'{estimate.physical_candidate_count} × DSP '
            f'{estimate.dsp_candidate_count} = '
            f'{estimate.combined_candidate_count} / 上限 '
            f'{estimate.candidate_budget}（{state}）'
            + detail
        )
        # #991: while no persisted spec is selected the card previews the
        # estimate for the spec these authoring controls would create —
        # the operator can adjust mode/budget and see the cost update.
        if self._selected_spec_id() is None:
            self._show_authoring_estimate(baseline, mode, dsp_variables)
        self.create_button.setEnabled(
            estimate.within_budget and not missing
        )
        self.create_button.setToolTip(
            '仕様を保存します。'
            if not missing
            else '作成に不足している権威: ' + ' / '.join(missing)
        )

    def _show_authoring_estimate(
        self,
        baseline: JointBaseline,
        mode: JointSearchMode,
        dsp_variables,
    ) -> None:
        try:
            estimate = self.context.prerun_estimate_for_authoring(
                baseline,
                mode=mode,
                dsp_variables=dsp_variables,
                candidate_budget=self.budget_spin.value(),
            )
        except Exception as exc:  # error-boundary: estimate authority — computation failures surface honestly as unavailable, never a partial estimate (noqa: BLE001)
            self.prerun_card.show_unavailable(
                operation_error_message(exc)
            )
            return
        try:
            observations = (
                self.context.field_metric_repository.prerun_observations(
                    estimate
                )
            )
        except Exception:  # error-boundary: advisory history — observations are optional context; failures degrade to empty rather than masking the estimate (noqa: BLE001)
            observations = ()
        self.prerun_card.show_estimate(estimate, observations)

    def _show_spec_estimate(self, spec_id: str) -> None:
        """Show the sealed estimate for the selected persisted spec."""
        try:
            estimate = self.context.prerun_estimate_for_spec(spec_id)
        except Exception as exc:  # error-boundary: estimate authority — computation failures surface honestly as unavailable, never a partial estimate (noqa: BLE001)
            self.prerun_card.show_unavailable(
                operation_error_message(exc)
            )
            return
        try:
            observations = (
                self.context.field_metric_repository.prerun_observations(
                    estimate
                )
            )
        except Exception:  # error-boundary: advisory history — observations are optional context; failures degrade to empty rather than masking the estimate (noqa: BLE001)
            observations = ()
        self.prerun_card.show_estimate(estimate, observations)

    def _create_spec(self) -> None:
        baseline = self._baseline
        if baseline is None:
            return
        try:
            spec = self.context.create_spec(
                baseline=baseline,
                mode=self._mode(),
                dsp_variables=self._selected_dsp_variables(),
                candidate_budget=self.budget_spin.value(),
            )
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: spec save surface — expected validation/store errors surface with reason; unexpected errors propagate to diagnostics
            if self._on_status is not None:
                self._on_status(f'ジョイント最適化仕様を保存できません: {operation_error_message(exc)}')
            return
        if self._on_status is not None:
            self._on_status(
                'ジョイント最適化仕様を保存しました: '
                f'{saved_label(spec.created_at_utc)}'
            )
        self._refresh_saved_specs()

    def _selected_spec_id(self) -> str | None:
        items = self.spec_tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, Qt.ItemDataRole.UserRole)

    def _spec_staleness(
        self,
        spec_id: str,
        *,
        baseline: JointBaseline | None | object = _UNSET_BASELINE,
    ) -> tuple[str, ...]:
        try:
            return self.context.assess_spec_staleness(
                spec_id, baseline=baseline
            )
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: staleness probe — expected failures report and mark the row 'assessment_failed', never fabricate 'fresh'
            report_boundary_failure(exc, operation='仕様の最新性評価')
            return ('assessment_failed',)

    def _on_spec_selection(self) -> None:
        self._refresh_execution_state()

    def is_running(self) -> bool:
        return self._pool.active_count > 0

    def stop(self) -> WorkerShutdownReport:
        """Drain in-flight evaluation but keep the panel usable.

        Same bounded stop as ``dispose`` minus the ``_disposed`` latch —
        used by the stop-busy deactivation escape so detached workers'
        completions stay disconnected and never apply (#REV19/D1).
        """
        report = self._pool.stop_all()
        self.execution_label.setText(
            'ジョイント最適化を中止しました'
            if report.all_stopped
            else 'ジョイント最適化の停止が遅延しています · 遅延結果は適用しません'
        )
        self._refresh_execution_state()
        return report

    def dispose(self) -> None:
        self._disposed = True
        report = self._pool.shutdown()
        if not report.all_stopped and self._on_status is not None:
            self._on_status(
                'ジョイント最適化の停止が遅延しています · 遅延結果は適用しません'
            )

    def _refresh_execution_state(self) -> None:
        if self.is_running():
            self.execute_button.setEnabled(False)
            self.execute_button.setToolTip('実行中です。')
            self.cancel_button.setEnabled(True)
            return
        self.cancel_button.setEnabled(False)
        spec_id = self._selected_spec_id()
        if spec_id is None:
            self.execute_button.setEnabled(False)
            self.execute_button.setToolTip(
                '保存済みの仕様を選択すると実行できます。'
            )
            # #991: nothing selected → the card follows the authoring
            # controls (refreshed by _refresh_preflight).
            if self._baseline is not None:
                self._show_authoring_estimate(
                    self._baseline,
                    self._mode(),
                    self._selected_dsp_variables(),
                )
            return
        self._show_spec_estimate(spec_id)
        reasons = self._spec_staleness(spec_id)
        if reasons:
            self.execute_button.setEnabled(False)
            self.execute_button.setToolTip(
                'ベースラインが変わったため実行できません: '
                + ', '.join(
                    _STALE_REASON_LABELS.get(reason, reason)
                    for reason in reasons
                )
            )
            return
        if self.prerun_card.blocking:
            self.execute_button.setEnabled(False)
            self.execute_button.setToolTip(
                '計算リソース見積もりが実行をブロックしています。'
            )
            return
        self.execute_button.setEnabled(True)
        self.execute_button.setToolTip(
            'この仕様を候補上限内で実行し、候補と評価を永続化します。'
        )

    def _execute_selected(self) -> None:
        if self._disposed:
            return
        spec_id = self._selected_spec_id()
        if spec_id is None or self.is_running():
            return
        # #991: re-check the estimate at commit — blocking reasons stay
        # fail-closed even if selection raced the last refresh.
        self._show_spec_estimate(spec_id)
        if self.prerun_card.blocking:
            self.execution_label.setText(
                '計算リソース見積もりが実行をブロックしました。'
            )
            return
        key = f'joint-{spec_id}'

        def operation(cancel_event) -> object:
            return self.context.execute_spec(
                spec_id,
                is_cancelled=cancel_event.is_set,
                on_progress=lambda result: self.progressChanged.emit(result),
            )

        self.execution_label.setText('実行中…')
        self._pool.start(key, operation, self._on_execution_completed)
        self._refresh_execution_state()

    def _cancel_execution(self) -> None:
        self._pool.cancel_all()
        self.cancel_button.setEnabled(False)
        self.execution_label.setText('中止を要求しました…')

    def _on_execution_progress(self, result) -> None:
        if self._disposed:
            return
        processed = result.candidates_generated + result.candidates_reused
        self.execution_label.setText(
            '実行中: 候補 '
            f'{processed}/{result.decision_vectors_total} 処理 · '
            f'生成 {result.candidates_generated} · '
            f'再利用 {result.candidates_reused} · '
            f'ブロック {result.candidates_blocked}'
        )

    def _on_execution_completed(self, _key, result, error) -> None:
        if self._disposed:
            return
        if error == WORKER_CANCELLED:
            self.execution_label.setText('実行を中止しました（部分結果は保持）')
            if self._on_status is not None:
                self._on_status('ジョイント最適化を中止しました')
            self._refresh_saved_specs()
            self._refresh_execution_state()
            return
        if error is not None:
            self.execution_label.setText('実行に失敗しました')
            if self._on_status is not None:
                self._on_status(
                    'ジョイント最適化を実行できません: '
                    f'{operation_error_message(error if isinstance(error, BaseException) else Exception(str(error)))}'
                )
            self._refresh_saved_specs()
            self._refresh_execution_state()
            return
        pareto = len(result.pareto_candidate_ids)
        summary = (
            '実行完了: 候補 '
            f'{result.candidates_generated} 生成 / '
            f'{result.candidates_reused} 再利用 / '
            f'{result.candidates_blocked} ブロック、評価 '
            f'{result.evaluations_recorded} 件'
            f'、Pareto前線 {pareto} 件'
        )
        if result.budget_limited:
            summary += '（候補上限で打ち切り）'
        if result.cancelled:
            summary += '（キャンセル済み — 部分結果は保持）'
        self.execution_label.setText(summary)
        if self._on_status is not None:
            self._on_status(f'ジョイント最適化を実行しました: {summary}')
        self._refresh_saved_specs()
        self._refresh_execution_state()

    def _refresh_saved_specs(self) -> None:
        selected = self._selected_spec_id()
        self.spec_tree.clear()
        try:
            specs = self.context.list_specs()
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: spec listing probe — expected failures report and leave the tree empty; sealed-store failures must not silently empty it
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='ジョイント最適化仕様の読み込み')
            return
        # One baseline resolution serves every row: assessing each spec
        # separately would re-read the whole authority chain per spec.
        baseline: JointBaseline | None | object = _UNSET_BASELINE
        if specs:
            try:
                baseline = self.context.resolve_baseline()
            except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: baseline resolution probe — expected failures report and re-resolve per spec; sealed-store failures propagate
                if is_authority_failure(exc):
                    raise
                report_boundary_failure(exc, operation='ベースラインの解決')
                baseline = _UNSET_BASELINE
        for spec in specs:
            dsp_count = len(spec.dsp_variables)
            mode = (
                _MODE_LABELS['joint']
                if spec.dsp_variables
                else _MODE_LABELS['placement_only']
            )
            reasons = self._spec_staleness(spec.spec_id, baseline=baseline)
            candidates = self.context.joint_repository.count_candidates(
                spec.spec_id
            )
            state = (
                '変更あり: '
                + ', '.join(
                    _STALE_REASON_LABELS.get(reason, reason)
                    for reason in reasons
                )
                if reasons
                else f'{candidates} 候補'
            )
            item = QTreeWidgetItem(
                (
                    saved_label(spec.created_at_utc),
                    mode,
                    str(dsp_count),
                    str(spec.candidate_budget),
                    state,
                )
            )
            item.setData(0, Qt.ItemDataRole.UserRole, spec.spec_id)
            item.setToolTip(0, spec.spec_id)
            self.spec_tree.addTopLevelItem(item)
            if selected is not None and spec.spec_id == selected:
                item.setSelected(True)
