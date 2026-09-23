"""Optimize > 配置 + DSP panel (#524).

Thin widget over :class:`JointOptimizationContext`: it resolves the exact
baseline authorities itself (no typed ids/hashes), gates DSP variables by
measurement/device capability, shows the candidate-count preflight, and
saves canonical ``JointOptimizationSpec`` records. Creating a spec mutates
neither the room nor device settings — apply/export stays with the existing
SystemVariant / CalibrationPlan lifecycle (#452).
"""

from __future__ import annotations

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

from .cad_joint_optimization import JointDspVariable
from .joint_optimization_context import (
    DEFAULT_MAGNITUDE_BAND_HZ,
    JointBaseline,
    JointOptimizationContext,
    JointSearchMode,
)
from .ui_theme import (
    SemanticState,
    TypographyRole,
    set_semantic_state,
    set_typography_role,
)


_MODE_ITEMS: tuple[tuple[JointSearchMode, str], ...] = (
    ('placement_only', '配置のみ'),
    ('dsp_only', 'DSPのみ'),
    ('joint', '配置 + DSP'),
)

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
        self.mode_combo.currentIndexChanged.connect(self._refresh_preflight)
        mode_form.addRow('探索モード', self.mode_combo)
        self.budget_spin = QSpinBox(self)
        self.budget_spin.setRange(1, 50_000)
        self.budget_spin.setValue(64)
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

        actions = QHBoxLayout()
        self.create_button = QPushButton('ジョイント最適化仕様を保存', self)
        self.create_button.clicked.connect(self._create_spec)
        actions.addWidget(self.create_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.spec_tree = QTreeWidget(self)
        self.spec_tree.setColumnCount(4)
        self.spec_tree.setHeaderLabels(('仕様', 'モード', 'DSP変数', '候補上限'))
        self.spec_tree.setRootIsDecorated(False)
        layout.addWidget(self.spec_tree)

        self.refresh()

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self._baseline = self.context.resolve_baseline()
        baseline = self._baseline
        if baseline is None:
            self.baseline_label.setText(
                'ベースライン未解決: 現在のSceneRevision・SystemVariant・'
                '物理探索設定が必要です。'
            )
            self._build_dsp_rows(())
            self.create_button.setEnabled(False)
            self.preflight_label.setText('')
            self._refresh_saved_specs()
            return

        plan = baseline.calibration_plan
        report = baseline.quality_report
        robustness = baseline.robustness_spec
        self.baseline_label.setText(
            'ベースライン: 部屋リビジョン '
            f'{baseline.scene_revision.revision_id[:12]}… / '
            f'バリアント {baseline.base_variant.variant_id[:18]}… / '
            f'物理探索 {baseline.physical_search_spec.search_spec_id[:18]}… / '
            f'CalibrationPlan '
            f'{plan.plan_id[:18] + "…" if plan is not None else "なし"} / '
            f'品質レポート '
            f'{report.report_id[:18] + "…" if report is not None else "なし"} / '
            f'O90 '
            f'{robustness.robustness_spec_id[:18] + "…" if robustness is not None else "なし"}'
        )
        options = self.context.dsp_variable_options(baseline)
        self._build_dsp_rows(options)
        self._refresh_preflight()
        self._refresh_saved_specs()

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
                for key, value in zip(
                    ('min', 'max', 'step'), defaults[:3]
                ):
                    spin = QDoubleSpinBox(self)
                    spin.setDecimals(4)
                    spin.setRange(-1_000_000.0, 1_000_000.0)
                    spin.setValue(value)
                    spin.setEnabled(option.enabled)
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
        except Exception as exc:  # defensive: preflight stays advisory
            self.preflight_label.setText(f'候補数を推定できません: {exc}')
            return
        state = (
            '予算内'
            if estimate.within_budget
            else '予算超過 — 範囲を狭めるか予算を増やしてください'
        )
        self.preflight_label.setText(
            '候補数の見積もり: 物理 '
            f'{estimate.physical_candidate_count} × DSP '
            f'{estimate.dsp_candidate_count} = '
            f'{estimate.combined_candidate_count} / 上限 '
            f'{estimate.candidate_budget}（{state}）'
        )
        self.create_button.setEnabled(estimate.within_budget)

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
        except Exception as exc:
            if self._on_status is not None:
                self._on_status(f'ジョイント最適化仕様を保存できません: {exc}')
            return
        if self._on_status is not None:
            self._on_status(
                f'ジョイント最適化仕様を保存しました: {spec.spec_id[:18]}…'
            )
        self._refresh_saved_specs()

    def _refresh_saved_specs(self) -> None:
        self.spec_tree.clear()
        try:
            specs = self.context.list_specs()
        except Exception:
            return
        for spec in specs:
            dsp_count = len(spec.dsp_variables)
            mode = 'joint' if spec.dsp_variables else 'placement_only'
            item = QTreeWidgetItem(
                (
                    spec.spec_id,
                    mode,
                    str(dsp_count),
                    str(spec.candidate_budget),
                )
            )
            self.spec_tree.addTopLevelItem(item)
