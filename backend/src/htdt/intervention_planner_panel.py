"""Intervention Planner surface (#961): finding → study → compare → apply → verify.

Thin widget over :class:`InterventionPlanner` and
:class:`SystemExpansionWorkflowService`. A user picks a study bound to an
explicit problem ROI, compares recorded alternatives along independent
columns (semantic diff, evidence state, target metrics, guardrail
regressions — never a single winner score), previews what the alternative
would change without mutating the scene, and applies only through the typed
``generated_authorities`` proposal — a ``system_variant`` authority applies
via the canonical SystemVariant lifecycle; other kinds hand off to the
workspace that owns that authority. Verification hands off to the
measurement campaign workspace.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from .cad_intervention_study import (
    InterventionAlternative,
    InterventionFamily,
    InterventionFinding,
)
from .intervention_planner import InterventionPlanner
from .system_expansion_workflow import SystemExpansionWorkflowService
from .ui_theme import (
    ControlSize,
    SurfaceRole,
    TypographyRole,
    set_control_size,
    set_primary_action,
    set_surface_role,
    set_typography_role,
)
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId
from .user_facing_error import operation_error_message


_EVIDENCE_LABELS: dict[str, str] = {
    'exploratory': '探索的',
    'predicted_unvalidated': '予測(未検証)',
    'predicted_validated': '予測(検証済)',
    'measured_verified': '実測済み(検証済)',
}
_COVERAGE_LABELS: dict[str, str] = {
    'complete': '完全',
    'partial': '一部',
    'blocked': '閉塞',
}
_FAMILY_LABELS: dict[str, str] = {
    'geometry': '配置',
    'calibration': '校正',
    'treatment': '吸音・処理',
    'topology': 'トポロジー',
}
# Where each generated-authority kind is executed when it cannot be
# materialized by the SystemVariant lifecycle on this page.
_APPLY_HANDOFFS: dict[str, tuple[WorkspaceId, str]] = {
    'search_spec': (WorkspaceId.OPTIMIZATION, 'candidates'),
    'extended_search_spec': (WorkspaceId.OPTIMIZATION, 'candidates'),
    'topology_search_spec': (WorkspaceId.OPTIMIZATION, 'candidates'),
    'joint_optimization_spec': (WorkspaceId.OPTIMIZATION, 'candidates'),
    'joint_candidate': (WorkspaceId.OPTIMIZATION, 'candidates'),
    'joint_candidate_evaluation': (WorkspaceId.OPTIMIZATION, 'candidates'),
    'joint_candidate_selection': (WorkspaceId.OPTIMIZATION, 'comparison'),
    'pareto_set': (WorkspaceId.OPTIMIZATION, 'comparison'),
    'topology_candidate': (WorkspaceId.OPTIMIZATION, 'comparison'),
    'calibration_plan': (WorkspaceId.MEASUREMENT, 'campaign'),
    'calibration_export': (WorkspaceId.MEASUREMENT, 'campaign'),
    'acoustic_treatment_definition': (WorkspaceId.ROOM, 'acoustics'),
    'acoustic_treatment_placement': (WorkspaceId.ROOM, 'acoustics'),
    'treatment_boundary_overlay': (WorkspaceId.ROOM, 'acoustics'),
    'treatment_boundary_composition': (WorkspaceId.ROOM, 'acoustics'),
    'treatment_comparison': (WorkspaceId.ROOM, 'acoustics'),
    'treatment_evidence_authority': (WorkspaceId.ROOM, 'acoustics'),
}


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec='seconds')
        .replace('+00:00', 'Z')
    )


def _secondary(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    set_typography_role(label, TypographyRole.SECONDARY)
    return label


class InterventionPlannerPanel(QFrame):
    """finding → study → compare → apply → verify (#961)."""

    applied = Signal(str)

    def __init__(
        self,
        planner: InterventionPlanner,
        service: SystemExpansionWorkflowService,
        *,
        on_status: Callable[[str], None] | None = None,
        on_navigate: Callable[[WorkspaceDeepLink], bool] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.planner = planner
        self.service = service
        self.treatment_repository = CadAcousticTreatmentRepository(
            planner.scene_repository
        )
        self._on_status = on_status or (lambda _text: None)
        self._on_navigate = on_navigate
        self._head_revision_id: str | None = None
        self._family_checks: dict[InterventionFamily, QCheckBox] = {}
        # Alternatives for the currently selected study, keyed by id —
        # populated by _on_study_selection so row clicks and Apply reuse the
        # validated list instead of re-running list_alternatives per click.
        self._alternatives: dict[str, InterventionAlternative] = {}

        set_surface_role(self, SurfaceRole.BASE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        create = QFrame()
        set_surface_role(create, SurfaceRole.RAISED)
        create_layout = QVBoxLayout(create)
        create_layout.setContentsMargins(14, 12, 14, 14)
        create_layout.setSpacing(8)
        title = QLabel("スタディを作成")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        create_layout.addWidget(title)
        create_layout.addWidget(
            _secondary(
                "課題領域を宣言して介入スタディを発行します。"
                "族は現在の能力で裏付けがあるものだけ選択できます。"
            )
        )
        form = QFormLayout()
        self.finding_detail_field = QLineEdit()
        self.finding_detail_field.setPlaceholderText(
            "例: 中域の座席でレベルが落ちる"
        )
        form.addRow("課題", self.finding_detail_field)
        self.observable_combo = QComboBox()
        self.observable_combo.setEditable(True)
        self.observable_combo.addItem("magnitude_response")
        form.addRow("観測量", self.observable_combo)
        self.band_enabled_check = QCheckBox("帯域を指定")
        form.addRow("", self.band_enabled_check)
        band_row = QHBoxLayout()
        self.band_low_field = QDoubleSpinBox()
        self.band_low_field.setRange(1.0, 20000.0)
        self.band_low_field.setValue(80.0)
        self.band_high_field = QDoubleSpinBox()
        self.band_high_field.setAccessibleName('帯域上限 Hz')
        self.band_high_field.setRange(2.0, 22000.0)
        self.band_high_field.setValue(160.0)
        band_row.addWidget(self.band_low_field)
        band_row.addWidget(QLabel("〜"))
        band_row.addWidget(self.band_high_field)
        band_row.addStretch(1)
        band_widget = QWidget()
        band_widget.setLayout(band_row)
        form.addRow("帯域 Hz", band_widget)
        self.fidelity_field = QLineEdit("native")
        form.addRow("忠実度ラベル", self.fidelity_field)
        self.candidate_budget_field = QSpinBox()
        self.candidate_budget_field.setRange(1, 50_000)
        self.candidate_budget_field.setValue(64)
        form.addRow("候補予算", self.candidate_budget_field)
        self.evidence_floor_combo = QComboBox()
        for state, label in _EVIDENCE_LABELS.items():
            self.evidence_floor_combo.addItem(label, state)
        form.addRow("証拠状態の下限", self.evidence_floor_combo)
        create_layout.addLayout(form)

        family_row = QHBoxLayout()
        for family, label in _FAMILY_LABELS.items():
            check = QCheckBox(label)
            check.setChecked(True)
            self._family_checks[family] = check
            family_row.addWidget(check)
        family_row.addStretch(1)
        create_layout.addLayout(family_row)
        self.family_reason_label = _secondary("")
        create_layout.addWidget(self.family_reason_label)
        self.create_button = QPushButton("スタディを作成")
        set_control_size(self.create_button, ControlSize.STANDARD)
        set_primary_action(self.create_button)
        self.create_button.clicked.connect(self._create_study)
        create_layout.addWidget(self.create_button)
        layout.addWidget(create)

        splitter = QSplitter(Qt.Orientation.Vertical)
        study_frame = QFrame()
        set_surface_role(study_frame, SurfaceRole.RAISED)
        study_layout = QVBoxLayout(study_frame)
        study_layout.setContentsMargins(14, 12, 14, 14)
        study_title = QLabel("スタディ")
        set_typography_role(study_title, TypographyRole.SECTION_TITLE)
        study_layout.addWidget(study_title)
        self.study_tree = QTreeWidget()
        # Height caps keep this page's size hint inside the workspace stack's
        # minimum so the action buttons stay reachable on small displays.
        self.study_tree.setMinimumHeight(140)
        self.study_tree.setMaximumHeight(260)
        self.study_tree.setColumnCount(4)
        self.study_tree.setHeaderLabels(
            ["課題 / 観測量", "族", "基準リビジョン", "状態"]
        )
        self.study_tree.itemSelectionChanged.connect(
            self._on_study_selection
        )
        study_layout.addWidget(self.study_tree)
        splitter.addWidget(study_frame)

        alt_frame = QFrame()
        set_surface_role(alt_frame, SurfaceRole.RAISED)
        alt_layout = QVBoxLayout(alt_frame)
        alt_layout.setContentsMargins(14, 12, 14, 14)
        alt_title = QLabel("介入案の比較")
        set_typography_role(alt_title, TypographyRole.SECTION_TITLE)
        alt_layout.addWidget(alt_title)
        alt_layout.addWidget(
            _secondary(
                "各行は独立した観測量です。勝者スコアは算出しません。"
                "選択すると差分・指標・権威をそのまま確認できます"
                "(プレビューはシーンを変更しません)。"
            )
        )
        self.alternative_tree = QTreeWidget()
        self.alternative_tree.setMinimumHeight(160)
        self.alternative_tree.setMaximumHeight(320)
        self.alternative_tree.setColumnCount(5)
        self.alternative_tree.setHeaderLabels(
            ["族", "差分", "証拠状態", "評価網羅", "ガードレール回帰"]
        )
        self.alternative_tree.itemSelectionChanged.connect(
            self._on_alternative_selection
        )
        alt_layout.addWidget(self.alternative_tree)
        self.alternative_detail = QLabel("介入案を選択してください。")
        self.alternative_detail.setWordWrap(True)
        set_typography_role(
            self.alternative_detail, TypographyRole.SECONDARY
        )
        alt_layout.addWidget(self.alternative_detail)
        action_row = QHBoxLayout()
        self.apply_button = QPushButton("選択した介入案を適用")
        set_control_size(self.apply_button, ControlSize.STANDARD)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply_selected)
        self.verify_button = QPushButton("検証測定へ")
        set_control_size(self.verify_button, ControlSize.STANDARD)
        self.verify_button.clicked.connect(self._open_verification)
        action_row.addWidget(self.apply_button)
        action_row.addWidget(self.verify_button)
        action_row.addStretch(1)
        alt_layout.addLayout(action_row)
        splitter.addWidget(alt_frame)
        layout.addWidget(splitter, 1)

        self.refresh()

    def refresh(self) -> None:
        """Reload studies/alternatives and re-evaluate capability gating."""
        baseline = self.planner.context.resolve_baseline()
        head = self.planner.scene_repository.current_head(
            self.planner.document_id
        )
        self._head_revision_id = (
            head.revision_id if head is not None else None
        )
        self._refresh_family_gates(baseline)
        self._reload_studies()

    def _refresh_family_gates(self, baseline) -> None:
        reasons: list[str] = []
        checks = self._family_checks
        if baseline is None:
            for check in checks.values():
                check.setEnabled(False)
            self.create_button.setEnabled(False)
            self.family_reason_label.setText(
                "探索ベースライン(保存済み探索設定と目的)がありません。"
                "探索設定を保存すると介入スタディを作成できます。"
            )
            return
        self.create_button.setEnabled(True)
        checks['geometry'].setEnabled(True)
        checks['topology'].setEnabled(True)

        calibration_ready = any(
            option.enabled
            for option in self.planner.context.dsp_variable_options(baseline)
        )
        checks['calibration'].setEnabled(calibration_ready)
        if not calibration_ready:
            reasons.append(
                "校正: 測定能力に裏付けられたDSP変数がありません"
            )

        definitions = self.treatment_repository.list_definitions()
        checks['treatment'].setEnabled(bool(definitions))
        if not definitions:
            reasons.append(
                "吸音・処理: トリートメント定義(能力権威)が未登録です"
            )
        self.family_reason_label.setText("\n".join(reasons))

    def _reload_studies(self) -> None:
        self.study_tree.clear()
        self.alternative_tree.clear()
        self.alternative_detail.setText("介入案を選択してください。")
        for spec in sorted(
            self.planner.list_studies(),
            key=lambda item: item.created_at_utc,
            reverse=True,
        ):
            stale = (
                self._head_revision_id is not None
                and spec.scene_revision_id != self._head_revision_id
            )
            item = QTreeWidgetItem(
                [
                    spec.finding.observable,
                    ", ".join(
                        _FAMILY_LABELS.get(family, family)
                        for family in spec.allowed_families
                    ),
                    spec.scene_revision_id[:24],
                    "基準が更新済み" if stale else "最新の基準",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, spec.spec_id)
            self.study_tree.addTopLevelItem(item)

    def _selected_spec_id(self) -> str | None:
        items = self.study_tree.selectedItems()
        if not items:
            return None
        value = items[0].data(0, Qt.ItemDataRole.UserRole)
        return str(value) if value is not None else None

    def _on_study_selection(self) -> None:
        self.alternative_tree.clear()
        self._alternatives.clear()
        self.alternative_detail.setText("介入案を選択してください。")
        self.apply_button.setEnabled(False)
        spec_id = self._selected_spec_id()
        if spec_id is None:
            return
        for alternative in self.planner.list_alternatives(spec_id):
            self._alternatives[alternative.alternative_id] = alternative
            item = QTreeWidgetItem(
                [
                    _FAMILY_LABELS.get(
                        alternative.family, alternative.family
                    ),
                    alternative.semantic_diff.summary,
                    _EVIDENCE_LABELS.get(
                        alternative.evidence_state, alternative.evidence_state
                    ),
                    _COVERAGE_LABELS.get(
                        alternative.evaluation_coverage,
                        alternative.evaluation_coverage,
                    ),
                    str(len(alternative.regressions)),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, spec_id)
            item.setData(
                1, Qt.ItemDataRole.UserRole, alternative.alternative_id
            )
            self.alternative_tree.addTopLevelItem(item)

    def _selected_alternative(self) -> InterventionAlternative | None:
        items = self.alternative_tree.selectedItems()
        if not items:
            return None
        spec_id = items[0].data(0, Qt.ItemDataRole.UserRole)
        alternative_id = items[0].data(1, Qt.ItemDataRole.UserRole)
        if spec_id is None or alternative_id is None:
            return None
        return self._alternatives.get(str(alternative_id))

    def _on_alternative_selection(self) -> None:
        alternative = self._selected_alternative()
        self.apply_button.setEnabled(alternative is not None)
        if alternative is None:
            return
        lines = [
            f"差分: {alternative.semantic_diff.summary}",
            f"変更対象: {', '.join(alternative.semantic_diff.changed_entity_ids) or '—'}",
            f"DSP: {', '.join(alternative.semantic_diff.dsp_parameters) or '—'}",
            f"トリートメント要素: {', '.join(alternative.semantic_diff.treatment_item_ids) or '—'}",
            f"トポロジー: {', '.join(alternative.semantic_diff.topology_changes) or '—'}",
            "",
            "指標 (目的 / ガードレール):",
        ]
        if not alternative.metrics:
            lines.append("  (指標なし)")
        for metric in alternative.metrics:
            domain = (
                '目的' if metric.domain == 'target_roi' else 'ガードレール'
            )
            if metric.state == 'available':
                value = f"{metric.value:g}"
            else:
                value = (
                    '欠測' if metric.state == 'missing' else '未対応'
                )
            axis = ' / '.join(
                part
                for part in (
                    metric.observable,
                    f"{metric.band_hz[0]:g}-{metric.band_hz[1]:g} Hz"
                    if metric.band_hz
                    else None,
                    metric.seat_entity_id,
                )
                if part
            )
            lines.append(f"  [{domain}] {metric.objective_id}: {value} {axis}")
        if alternative.regressions:
            lines.append("")
            lines.append("ガードレール回帰: " + ", ".join(alternative.regressions))
        lines.append("")
        if alternative.generated_authorities:
            lines.append("適用権威:")
            lines.extend(
                f"  {ref.authority_kind}: {ref.authority_id[:32]}"
                for ref in alternative.generated_authorities
            )
        else:
            lines.append("適用権威: なし — 直接適用できません")
        if alternative.evidence_authorities:
            lines.append("証拠権威:")
            lines.extend(
                f"  {ref.authority_kind}: {ref.authority_id[:32]}"
                for ref in alternative.evidence_authorities
            )
        if not alternative.quantitatively_comparable:
            lines.append(
                f"比較不能: {alternative.incomparability_reason or alternative.evaluation_coverage}"
            )
        if alternative.application_note:
            lines.append(f"適用メモ: {alternative.application_note}")
        self.alternative_detail.setText("\n".join(lines))

    def _create_study(self) -> None:
        families = [
            family
            for family, check in self._family_checks.items()
            if check.isChecked() and check.isEnabled()
        ]
        if not families:
            self._on_status("有効な介入族を1つ以上選択してください。")
            return
        observable = self.observable_combo.currentText().strip()
        if not observable:
            self._on_status("観測量を指定してください。")
            return
        band: tuple[float, float] | None = None
        if self.band_enabled_check.isChecked():
            band = (
                float(self.band_low_field.value()),
                float(self.band_high_field.value()),
            )
        detail = self.finding_detail_field.text().strip() or None
        try:
            finding = InterventionFinding(
                source_kind='explicit_user_region',
                observable=observable,
                target_band_hz=band,
                detail=detail,
            )
        except ValueError as exc:
            self._on_status(f"課題領域が無効です: {operation_error_message(exc)}")
            return
        treatment_authority: tuple[str | None, str | None] = (None, None)
        if 'treatment' in families:
            definitions = self.treatment_repository.list_definitions()
            if not definitions:
                self._on_status("トリートメント能力権威が解決できません。")
                return
            capability = definitions[-1]
            treatment_authority = (
                capability.definition_id,
                capability.definition_sha256,
            )
        try:
            spec = self.planner.create_study(
                finding=finding,
                allowed_families=families,
                fidelity_label=self.fidelity_field.text().strip() or 'native',
                provider_id='intervention-planner',
                provider_version='1',
                candidate_budget=self.candidate_budget_field.value(),
                created_at_utc=_utc_now(),
                treatment_capability_authority_id=treatment_authority[0],
                treatment_capability_authority_sha256=treatment_authority[1],
                evidence_state_floor=self.evidence_floor_combo.currentData(),
            )
        except ValueError as exc:
            self._on_status(f"スタディを作成できません: {operation_error_message(exc)}")
            return
        if spec is None:
            self._on_status(
                "探索ベースライン(目的定義)がありません。"
                "探索設定を保存してから作成してください。"
            )
            return
        self._on_status(
            f"介入スタディ {spec.spec_id[:24]} を作成しました。"
            "介入案は評価権威から登録されます。"
        )
        self.refresh()

    def _apply_selected(self) -> None:
        alternative = self._selected_alternative()
        if alternative is None:
            self._on_status("適用する介入案を選択してください。")
            return
        if not alternative.generated_authorities:
            self._on_status(
                "この介入案は型付き提案権威を持たないため"
                "適用できません。"
            )
            return
        variant_ref = next(
            (
                ref
                for ref in alternative.generated_authorities
                if ref.authority_kind == 'system_variant'
            ),
            None,
        )
        if variant_ref is not None:
            self._apply_variant(alternative, variant_ref.authority_id)
            return
        first = alternative.generated_authorities[0]
        destination = _APPLY_HANDOFFS.get(first.authority_kind)
        if destination is None:
            self._on_status(
                f"権威 '{first.authority_kind}' の適用先が未対応です。"
            )
            return
        if self._on_navigate is not None and self._on_navigate(
            WorkspaceDeepLink(
                workspace=destination[0],
                section=destination[1],
            )
        ):
            self._on_status(
                "型付き提案権威を持つワークスペースで適用を続けます。"
            )
        else:
            self._on_status("適用先ワークスペースへ移動できませんでした。")

    def _apply_variant(
        self, alternative: InterventionAlternative, variant_id: str
    ) -> None:
        preview = self.service.apply_preview(variant_id)
        if preview.stale:
            self._on_status(preview.stale_reason or "古い提案")
            return
        message = (
            f"{alternative.semantic_diff.summary}\n\n"
            f"{preview.name}\n"
            + ("\n".join(preview.change_lines) or "差分なし")
            + "\n\nベースラインを上書きせず、新しいシーンリビジョンを作成します。"
            "この操作だけでは「設置済み」にはなりません。"
        )
        answer = QMessageBox.question(
            self,
            "介入案を適用",
            message,
            QMessageBox.StandardButton.Apply | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Apply:
            return
        try:
            application = self.service.apply(variant_id)
        except ValueError as exc:
            self._on_status(f"適用できません: {operation_error_message(exc)}")
            return
        self._on_status(
            "介入案を新しいシーンリビジョンへ適用しました。"
            "検証は測定ワークスペースで実施してください。"
        )
        self.applied.emit(application.applied_revision_id)
        self.refresh()

    def _open_verification(self) -> None:
        if self._on_navigate is not None and self._on_navigate(
            WorkspaceDeepLink(
                workspace=WorkspaceId.MEASUREMENT,
                section="campaign",
            )
        ):
            return
        self._on_status("測定ワークスペースへ移動できませんでした。")


__all__ = [
    'InterventionPlannerPanel',
]
