from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .system_expansion_workflow import SystemExpansionWorkflowService
from .ui_theme import (
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


class _VariantSelector(QWidget):
    changed = Signal(str)

    def __init__(self, service: SystemExpansionWorkflowService, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        self.combo.setAccessibleName("SystemVariant")
        self.refresh_button = QPushButton("更新")
        set_control_size(self.refresh_button, ControlSize.COMPACT)
        row.addWidget(QLabel("提案"))
        row.addWidget(self.combo, 1)
        row.addWidget(self.refresh_button)
        self.refresh_button.clicked.connect(self.refresh)
        self.combo.currentIndexChanged.connect(self._emit)
        self.refresh()

    def refresh(self) -> None:
        selected = self.current_variant_id()
        self.combo.blockSignals(True)
        self.combo.clear()
        for variant in self.service.variants():
            # Human-readable name is the primary UI identity; internal IDs stay in userData.
            self.combo.addItem(variant.name, variant.variant_id)
        if selected is not None:
            index = self.combo.findData(selected)
            if index >= 0:
                self.combo.setCurrentIndex(index)
        self.combo.blockSignals(False)
        self._emit()

    def current_variant_id(self) -> str | None:
        value = self.combo.currentData()
        return None if value is None else str(value)

    def select_variant(self, variant_id: str) -> bool:
        index = self.combo.findData(variant_id)
        if index < 0:
            return False
        self.combo.setCurrentIndex(index)
        return True

    def _emit(self, *_args) -> None:
        variant_id = self.current_variant_id()
        if variant_id is not None:
            self.changed.emit(variant_id)


class SystemExpansionRoomPanel(QFrame):
    """Room / speaker-placement presentation for proposed SystemVariants."""

    variantChanged = Signal(str)
    ghostEntityRequested = Signal(str)

    def __init__(
        self,
        service: SystemExpansionWorkflowService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("システム拡張")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        intro = QLabel(
            "現在構成と提案を分けて確認します。提案ghostは未設置で、現在のScene truthを変更しません。"
        )
        intro.setWordWrap(True)
        set_typography_role(intro, TypographyRole.SECONDARY)
        layout.addWidget(intro)

        self.current_label = QLabel()
        self.current_label.setWordWrap(True)
        layout.addWidget(self.current_label)

        author = QFrame()
        set_surface_role(author, SurfaceRole.BASE)
        author_layout = QVBoxLayout(author)
        author_layout.setContentsMargins(0, 6, 0, 8)
        author_layout.setSpacing(8)
        author_title = QLabel("追加speaker/channel と設置可能領域")
        set_typography_role(author_title, TypographyRole.SECTION_TITLE)
        author_layout.addWidget(author_title)
        author_note = QLabel(
            "内部IDを入力せず、既存O100A/B/C authorityで提案とplacement candidateを作成します。"
        )
        author_note.setWordWrap(True)
        set_typography_role(author_note, TypographyRole.SECONDARY)
        author_layout.addWidget(author_note)

        self.proposal_name = QLineEdit()
        self.proposal_name.setPlaceholderText("例: proposed 5.0.2 A")
        self.role_field = QLineEdit()
        self.role_field.setPlaceholderText("例: SL")
        self.equipment_combo = QComboBox()
        self.zone_name = QLineEdit("install-zone")
        self.zone_min_x = self._coordinate_field()
        self.zone_max_x = self._coordinate_field()
        self.zone_min_y = self._coordinate_field()
        self.zone_max_y = self._coordinate_field()
        self.zone_z = self._coordinate_field()
        self.zone_step = self._coordinate_field(minimum=0.01)
        self.zone_step.setValue(0.25)

        proposal_form = QFormLayout()
        proposal_form.addRow("提案名", self.proposal_name)
        proposal_form.addRow("speaker role", self.role_field)
        proposal_form.addRow("equipment/source", self.equipment_combo)
        proposal_form.addRow("設置可能領域", self.zone_name)
        proposal_form.addRow("X 最小 m", self.zone_min_x)
        proposal_form.addRow("X 最大 m", self.zone_max_x)
        proposal_form.addRow("Y 最小 m", self.zone_min_y)
        proposal_form.addRow("Y 最大 m", self.zone_max_y)
        proposal_form.addRow("高さ m", self.zone_z)
        proposal_form.addRow("探索刻み m", self.zone_step)
        author_layout.addLayout(proposal_form)
        self.create_proposal_button = QPushButton("提案とplacement candidateを作成")
        set_primary_action(self.create_proposal_button)
        self.create_proposal_button.clicked.connect(self._create_proposal)
        author_layout.addWidget(self.create_proposal_button)
        self.authoring_status = QLabel()
        self.authoring_status.setWordWrap(True)
        author_layout.addWidget(self.authoring_status)
        layout.addWidget(author)

        self.selector = _VariantSelector(service)
        self.selector.changed.connect(self._variant_changed)
        layout.addWidget(self.selector)

        self.lifecycle_badge = QLabel("提案")
        set_typography_role(self.lifecycle_badge, TypographyRole.BODY)
        self.lifecycle_badge.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(self.lifecycle_badge)
        self.lifecycle_label = QLabel("提案がありません")
        self.lifecycle_label.setWordWrap(True)
        set_typography_role(self.lifecycle_label, TypographyRole.SECONDARY)
        layout.addWidget(self.lifecycle_label)

        self.entity_tree = QTreeWidget()
        self.entity_tree.setHeaderLabels(
            ["追加speaker/channel", "役割", "状態", "equipment", "設置可能領域", "理由"]
        )
        self.entity_tree.itemSelectionChanged.connect(self._entity_selected)
        layout.addWidget(self.entity_tree, 1)

        self.advanced_button = QPushButton("詳細を表示")
        self.advanced_button.setCheckable(True)
        set_control_size(self.advanced_button, ControlSize.COMPACT)
        self.advanced_label = QLabel()
        self.advanced_label.setWordWrap(True)
        self.advanced_label.hide()
        self.advanced_button.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_button)
        layout.addWidget(self.advanced_label)
        self.refresh()

    @staticmethod
    def _coordinate_field(*, minimum: float = -1000.0) -> QDoubleSpinBox:
        field = QDoubleSpinBox()
        field.setRange(minimum, 1000.0)
        field.setDecimals(3)
        field.setSingleStep(0.1)
        return field

    def _refresh_equipment(self) -> None:
        selected = self.equipment_combo.currentData()
        self.equipment_combo.clear()
        for semantic_sha256, label in self.service.equipment_choices():
            self.equipment_combo.addItem(label, semantic_sha256)
        if selected is not None:
            index = self.equipment_combo.findData(selected)
            if index >= 0:
                self.equipment_combo.setCurrentIndex(index)
        self.create_proposal_button.setEnabled(self.equipment_combo.count() > 0)
        if self.equipment_combo.count() == 0:
            self.authoring_status.setText(
                "equipment/source modelがありません。先にEquipmentDefinitionを登録してください。"
            )

    def _initialize_zone_from_room(self) -> None:
        latest = self.service.scene_repository.latest(self.service.document_id)
        if latest is None or latest.document.room is None:
            return
        min_x, min_y, max_x, max_y = latest.document.room.bounds_m
        if (
            self.zone_min_x.value() == 0.0
            and self.zone_max_x.value() == 0.0
            and self.zone_min_y.value() == 0.0
            and self.zone_max_y.value() == 0.0
        ):
            self.zone_min_x.setValue(min_x)
            self.zone_max_x.setValue(max_x)
            self.zone_min_y.setValue(min_y)
            self.zone_max_y.setValue(max_y)
            self.zone_z.setValue(min(latest.document.room.height_m, 1.2))

    def _create_proposal(self) -> None:
        equipment_sha = self.equipment_combo.currentData()
        if equipment_sha is None:
            self.authoring_status.setText(
                "equipment/source modelを選択してください。"
            )
            return
        try:
            result = self.service.create_single_speaker_proposal(
                proposal_name=self.proposal_name.text(),
                role_id=self.role_field.text(),
                equipment_sha256=str(equipment_sha),
                zone_name=self.zone_name.text(),
                min_x_m=self.zone_min_x.value(),
                max_x_m=self.zone_max_x.value(),
                min_y_m=self.zone_min_y.value(),
                max_y_m=self.zone_max_y.value(),
                z_m=self.zone_z.value(),
                step_m=self.zone_step.value(),
                max_returned_candidates=24,
            )
        except ValueError as exc:
            self.authoring_status.setText(f"作成できません: {exc}")
            return
        self.authoring_status.setText(
            f"提案を保存しました。配置候補 {len(result.candidate_variant_ids)} 件を"
            f"保存（feasible {result.feasible_candidate_count} 件）。"
        )
        self.selector.refresh()
        self.selector.select_variant(result.template_variant_id)

    def refresh(self) -> None:
        self._refresh_equipment()
        self._initialize_zone_from_room()
        latest = self.service.scene_repository.latest(self.service.document_id)
        if latest is None:
            self.current_label.setText("現在構成: SceneRevisionがありません")
        else:
            speakers = [
                entity for entity in latest.document.entities
                if entity.kind == "speaker"
            ]
            roles = [item.speaker_role or item.name for item in speakers]
            summary = " / ".join(roles) if roles else "speakerなし"
            self.current_label.setText(f"現在構成: {summary}")
        self.selector.refresh()
        current = self.selector.current_variant_id()
        if current is not None:
            self._show_variant(current)
        else:
            self.entity_tree.clear()
            self.lifecycle_label.setText("保存済みのSystemVariant提案がありません。")

    def current_variant_id(self) -> str | None:
        return self.selector.current_variant_id()

    def _variant_changed(self, variant_id: str) -> None:
        self._show_variant(variant_id)
        self.variantChanged.emit(variant_id)

    def _show_variant(self, variant_id: str) -> None:
        view = self.service.variant_presentation(variant_id)
        self.lifecycle_badge.setText(view.lifecycle.label)
        set_semantic_state(
            self.lifecycle_badge,
            {
                "current": SemanticState.SELECTED,
                "proposed": SemanticState.WARNING,
                "as_built": SemanticState.SUCCESS,
                "measured": SemanticState.SUCCESS,
            }[view.lifecycle.state],
        )
        state = view.lifecycle.validation_label
        if view.stale and view.stale_reason:
            state += f" / 要再評価: {view.stale_reason}"
        self.lifecycle_label.setText(state)
        self.entity_tree.clear()
        for entity in view.entities:
            row = QTreeWidgetItem(
                [
                    entity.name,
                    entity.role or "未設定",
                    entity.lifecycle_label,
                    entity.equipment,
                    entity.install_zone,
                    entity.reason or "",
                ]
            )
            # Selection metadata is not rendered as a standard field.
            ghost = next(
                (
                    item for item in self.service.ghost_preview(variant_id)
                    if item.name == entity.name and item.speaker_role == entity.role
                ),
                None,
            )
            if ghost is not None:
                row.setData(0, 0x0100, ghost.entity_id)
            self.entity_tree.addTopLevelItem(row)
        p = view.advanced
        self.advanced_label.setText(
            "\n".join(
                (
                    f"variant_id: {p.variant_id}",
                    f"variant_sha256: {p.variant_sha256}",
                    f"baseline_revision_id: {p.baseline_revision_id}",
                    f"baseline_content_hash: {p.baseline_content_hash}",
                    f"schema_version: {p.schema_version}",
                    f"authority_version: {p.authority_version}",
                    "repository/provenance keys: " + (", ".join(p.repository_keys) or "なし"),
                )
            )
        )

    def _entity_selected(self) -> None:
        rows = self.entity_tree.selectedItems()
        if not rows:
            return
        entity_id = rows[0].data(0, 0x0100)
        if entity_id:
            self.ghostEntityRequested.emit(str(entity_id))

    def _toggle_advanced(self, checked: bool) -> None:
        self.advanced_button.setText("詳細を隠す" if checked else "詳細を表示")
        self.advanced_label.setVisible(checked)


class SystemExpansionOptimizePanel(QFrame):
    robustnessRequested = Signal(str)
    applied = Signal(str)

    def __init__(
        self,
        service: SystemExpansionWorkflowService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("SystemVariant 比較")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        note = QLabel(
            "coverage / SPL・headroom / Standards / objectiveを独立表示します。"
            "比較できない値は順位付けせず、理由を表示します。"
        )
        note.setWordWrap(True)
        set_typography_role(note, TypographyRole.SECONDARY)
        layout.addWidget(note)

        self.selector = _VariantSelector(service)
        layout.addWidget(self.selector)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            [
                "candidate",
                "状態",
                "coverage",
                "SPL/headroom",
                "Standards",
                "比較",
                "Pareto",
                "理由",
            ]
        )
        layout.addWidget(self.tree, 1)

        self.advanced_button = QPushButton("詳細を表示")
        self.advanced_button.setCheckable(True)
        set_control_size(self.advanced_button, ControlSize.COMPACT)
        self.advanced_label = QLabel()
        self.advanced_label.setWordWrap(True)
        self.advanced_label.hide()
        self.advanced_button.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_button)
        layout.addWidget(self.advanced_label)

        actions = QHBoxLayout()
        self.robustness_button = QPushButton("ばらつき耐性を確認")
        self.apply_button = QPushButton("この提案を適用…")
        set_primary_action(self.apply_button)
        actions.addWidget(self.robustness_button)
        actions.addWidget(self.apply_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.robustness_button.clicked.connect(self._robustness)
        self.apply_button.clicked.connect(self._apply)
        self.selector.changed.connect(lambda _variant_id: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        view = self.service.comparison()
        self.tree.clear()
        if view is None:
            self.summary.setText(
                "no eligible comparison: 保存済みTopologyComparisonEvaluationがありません。"
            )
            return
        text = view.name
        if view.authority_stale and view.stale_reason:
            text += f" / 要再評価: {view.stale_reason}"
        self.summary.setText(text)
        for variant in view.variants:
            row = QTreeWidgetItem(
                [
                    variant.name,
                    variant.lifecycle_label,
                    variant.coverage,
                    variant.spl_headroom,
                    variant.standards,
                    variant.eligibility_label,
                    variant.pareto_state,
                    variant.blocked_reason or "",
                ]
            )
            row.setData(0, 0x0100, variant.variant_id)
            if variant.blocked_reason:
                row.setToolTip(7, variant.blocked_reason)
            for metric in variant.objectives:
                child = QTreeWidgetItem(
                    [
                        metric.objective_id,
                        "",
                        metric.value_text,
                        metric.direction_label,
                        "",
                        "比較可能" if metric.eligible else "利用不可",
                        metric.reason or "",
                        metric.reason or "",
                    ]
                )
                row.addChild(child)
            self.tree.addTopLevelItem(row)
        self.tree.expandAll()

    def _toggle_advanced(self, checked: bool) -> None:
        self.advanced_button.setText("詳細を隠す" if checked else "詳細を表示")
        self.advanced_label.setVisible(checked)
        if not checked:
            return
        variant_id = self.selector.current_variant_id()
        if variant_id is None:
            self.advanced_label.setText("選択中のSystemVariantがありません。")
            return
        try:
            p = self.service.variant_presentation(variant_id).advanced
        except KeyError:
            self.advanced_label.setText("provenanceを再解決できません。")
            return
        self.advanced_label.setText(
            "\n".join(
                (
                    f"variant_id: {p.variant_id}",
                    f"variant_sha256: {p.variant_sha256}",
                    f"baseline_revision_id: {p.baseline_revision_id}",
                    f"baseline_content_hash: {p.baseline_content_hash}",
                    f"schema_version: {p.schema_version}",
                    f"authority_version: {p.authority_version}",
                    "repository/provenance keys: "
                    + (", ".join(p.repository_keys) or "なし"),
                )
            )
        )

    def _robustness(self) -> None:
        variant_id = self.selector.current_variant_id()
        if variant_id is None:
            self.summary.setText("提案を選択してください。")
            return
        target = self.service.robustness_target(variant_id)
        if not target.available:
            self.summary.setText(target.reason or "ばらつきevidenceを利用できません。")
            return
        self.robustnessRequested.emit(variant_id)

    def _apply(self) -> None:
        variant_id = self.selector.current_variant_id()
        if variant_id is None:
            self.summary.setText("提案を選択してください。")
            return
        preview = self.service.apply_preview(variant_id)
        if preview.stale:
            self.summary.setText(preview.stale_reason or "stale proposal")
            return
        change_text = "\n".join(preview.change_lines) or "差分なし"
        message = (
            f"{preview.name}\n\n{change_text}\n\n"
            "baselineを上書きせず、新しいSceneRevisionを作成します。"
            "この操作だけでは「設置済み」にはなりません。"
        )
        answer = QMessageBox.question(
            self,
            "提案を適用",
            message,
            QMessageBox.StandardButton.Apply | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Apply:
            return
        try:
            application = self.service.apply(variant_id)
        except ValueError as exc:
            self.summary.setText(f"適用できません: {exc}")
            return
        self.summary.setText(
            "提案を新しいSceneRevisionへ適用しました。"
            "As-builtは実設置確認後に別途記録してください。"
        )
        self.applied.emit(application.applied_revision_id)
        self.selector.refresh()
        self.refresh()


class SystemExpansionRobustnessPanel(QFrame):
    """Read-only O100F proposal robustness surface mounted inside O90D."""

    def __init__(
        self,
        service: SystemExpansionWorkflowService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._variant_id: str | None = None
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("SystemVariant 提案のばらつき耐性")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        note = QLabel(
            "O100Fの保存済みproposal robustness authorityを、この既存ばらつき耐性"
            "workspace内で読み取り専用表示します。通常のO90 CadCandidateへ偽装しません。"
        )
        note.setWordWrap(True)
        set_typography_role(note, TypographyRole.SECONDARY)
        layout.addWidget(note)

        self.summary = QLabel("比較画面から提案を選択してください。")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            [
                "objective",
                "向き",
                "nominal",
                "評価サンプル内の不利側",
                "局所感度",
                "確率",
            ]
        )
        self.tree.setMinimumHeight(150)
        layout.addWidget(self.tree)

        self.advanced_button = QPushButton("詳細を表示")
        self.advanced_button.setCheckable(True)
        set_control_size(self.advanced_button, ControlSize.COMPACT)
        self.advanced_label = QLabel()
        self.advanced_label.setWordWrap(True)
        self.advanced_label.hide()
        self.advanced_button.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_button)
        layout.addWidget(self.advanced_label)

    def select_variant(self, variant_id: str) -> None:
        self._variant_id = variant_id
        self.refresh()

    def refresh(self) -> None:
        self.tree.clear()
        if self._variant_id is None:
            self.summary.setText("比較画面から提案を選択してください。")
            self.advanced_label.clear()
            return
        try:
            view = self.service.proposal_robustness_presentation(self._variant_id)
        except (KeyError, ValueError) as exc:
            self.summary.setText(f"ばらつきauthorityを表示できません: {exc}")
            self.advanced_label.clear()
            return
        if view is None:
            target = self.service.robustness_target(self._variant_id)
            self.summary.setText(
                target.reason or "保存済みproposal robustness authorityがありません。"
            )
            self.advanced_label.clear()
            return

        state = (
            "現在のbaselineと一致"
            if view.current
            else f"要再評価: {view.stale_reason}"
        )
        self.summary.setText(
            f"{view.variant_name} / {view.sampling_label} / {state} / "
            f"sample {view.sample_count} "
            f"(feasible {view.feasible_count}, infeasible {view.infeasible_count}, "
            f"failed {view.failed_count}, unscored {view.unscored_count})"
        )
        for objective in view.objectives:
            self.tree.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        objective.objective_id,
                        objective.direction_label,
                        objective.nominal_text,
                        objective.sampled_adverse_text,
                        objective.sensitivity_text,
                        objective.probability_text,
                    ]
                )
            )
        if not view.objectives:
            self.tree.addTopLevelItem(
                QTreeWidgetItem(
                    ["評価結果なし", "", "", "", "", ""]
                )
            )
        self.advanced_label.setText(
            "\n".join(
                (
                    f"robustness_spec_id: {view.robustness_spec_id}",
                    f"robustness_spec_sha256: {view.robustness_spec_sha256}",
                    f"candidate_variant_sha256: {view.candidate_variant_sha256}",
                )
            )
        )

    def _toggle_advanced(self, checked: bool) -> None:
        self.advanced_button.setText("詳細を隠す" if checked else "詳細を表示")
        self.advanced_label.setVisible(checked)


class SystemExpansionMeasurementPanel(QFrame):
    def __init__(
        self,
        service: SystemExpansionWorkflowService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("SystemVariant lifecycle / 実測")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        note = QLabel(
            "generic N60 measurementの存在だけでは実測済みに昇格しません。"
            "SystemVariant固有plan/campaign/completionとvalidationを区別します。"
        )
        note.setWordWrap(True)
        set_typography_role(note, TypographyRole.SECONDARY)
        layout.addWidget(note)
        self.selector = _VariantSelector(service)
        layout.addWidget(self.selector)
        self.lifecycle_badge = QLabel()
        self.lifecycle_badge.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(self.lifecycle_badge)
        self.state = QLabel()
        self.state.setWordWrap(True)
        layout.addWidget(self.state)
        self.selector.changed.connect(lambda _variant_id: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        variant_id = self.selector.current_variant_id()
        if variant_id is None:
            self.state.setText("SystemVariant提案がありません。")
            return
        lifecycle = self.service.lifecycle(variant_id)
        self.lifecycle_badge.setText(lifecycle.label)
        set_semantic_state(
            self.lifecycle_badge,
            {
                "current": SemanticState.SELECTED,
                "proposed": SemanticState.WARNING,
                "as_built": SemanticState.SUCCESS,
                "measured": SemanticState.SUCCESS,
            }[lifecycle.state],
        )
        view = self.service.measurement(variant_id)
        self.state.setText(
            f"{view.state_label} / {view.validation_label}\n{view.detail}"
        )
