from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_repository import SceneRepository
from .cad_standards import (
    CriterionDefinition,
    CriterionEvaluationResult,
    StandardsEvaluation,
    StandardsEvaluationTarget,
    StandardsHardConstraintGate,
    StandardsProfile,
    evaluate_standards_profile,
    explicit_hard_constraint_gate,
)
from .cad_standards_authorities import builtin_standards_source_authorities
from .cad_standards_layout_observation import derive_layout_observations
from .cad_standards_profiles import builtin_standards_profiles
from .cad_standards_repository import CadStandardsRepository
from .cad_system_variant import materialize_system_variant
from .cad_system_variant_repository import CadSystemVariantRepository
from .dynamic_a11y import (
    DynamicAnnouncer,
    capture_focus,
    disabled_hint,
    reason_label,
    restore_focus,
)
from .ui_theme import (
    ControlSize,
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_control_size,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)
from .clock import utc_now_iso as _utc_now


_STATUS_JA = {
    "PASS": "✓ 適合",
    "FAIL": "✕ 不適合",
    "UNKNOWN": "? 判定材料不足",
    "NOT_APPLICABLE": "— 対象外",
}
_EVIDENCE_JA = {
    "predicted": "予測",
    "measured": "実測",
    None: "証拠なし",
}
_REASON_JA = {
    "missing_observation": "観測値がありません",
    "missing_input_or_capability": "必要な入力または能力が不足しています",
    "measurement_evidence_required": "実測証拠が必要です",
    "domain_not_applicable": "この対象には適用されません",
    "explicit_not_applicable": "対象外として明示されています",
    "unit_mismatch": "単位が一致しません",
    "missing_evidence": "証拠がありません",
    "missing_observed_value": "観測値が設定されていません",
    "invalid_observed_value": "観測値が無効です",
    "comparison_pass": "基準を満たしています",
    "comparison_fail": "基準を満たしていません",
}


def _profile_key(profile: StandardsProfile) -> str:
    return f"{profile.profile_id}\x1f{profile.version}"


def _domains_for_document(document) -> tuple[str, ...]:
    domains: set[str] = set()
    if document.room is not None:
        domains.add("room")
    kinds = {entity.kind for entity in document.entities}
    if "seat" in kinds:
        domains.add("seat")
    if "speaker" in kinds:
        # Speaker-specific source criteria still require explicit observations.
        # Including these domains prefers UNKNOWN over a false NOT_APPLICABLE
        # when the exact role/evidence provider is unavailable.
        domains.update(
            {
                "speaker_layout",
                "wide_speaker",
                "auro_lower_speaker",
                "auro_height_speaker",
                "auro_top_speaker",
            }
        )
    return tuple(sorted(domains or {"room"}))


@dataclass(frozen=True, slots=True)
class StandardsTargetView:
    label: str
    target: StandardsEvaluationTarget
    variant_id: str | None


class StandardsWorkspaceModel:
    """S130 presentation/application adapter over Issue #170 authorities."""

    def __init__(self, scene_repository: SceneRepository, document_id: str) -> None:
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.variant_repository = CadSystemVariantRepository(scene_repository)
        self.repository = CadStandardsRepository(
            scene_repository,
            self.variant_repository,
        )
        for authority in builtin_standards_source_authorities():
            self.repository.save_source_authority(authority)
        for profile in builtin_standards_profiles():
            self.repository.save_profile(profile)

    def profiles(self) -> tuple[StandardsProfile, ...]:
        return self.repository.list_profiles()

    def profile(self, profile_id: str, version: str) -> StandardsProfile:
        profile = self.repository.get_profile(profile_id, version)
        if profile is None:
            raise KeyError((profile_id, version))
        return profile

    def targets(self) -> tuple[StandardsTargetView, ...]:
        result = [self.target_view(None)]
        for variant in self.variant_repository.list_variants(self.document_id):
            result.append(self.target_view(variant.variant_id))
        return tuple(result)

    def target_view(self, variant_id: str | None) -> StandardsTargetView:
        if variant_id is None:
            revision = self.scene_repository.current_head(self.document_id)
            if revision is None:
                raise ValueError("保存済みの部屋状態がありません")
            document = revision.document
            label = "現在の部屋"
            variant = None
        else:
            variant = self.variant_repository.get_variant(variant_id)
            if variant is None or variant.document_id != self.document_id:
                raise KeyError(variant_id)
            revision = self.scene_repository.get(variant.baseline_revision_id)
            if revision is None:
                raise ValueError("システムバリアントの基準シーンリビジョンがありません")
            if revision.content_hash != variant.baseline_content_hash:
                raise ValueError("システムバリアントの基準シーンリビジョンが一致しません")
            document = materialize_system_variant(revision, variant)
            label = variant.name
        target = StandardsEvaluationTarget(
            document_id=revision.document_id,
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            system_variant_id=None if variant is None else variant.variant_id,
            system_variant_sha256=None if variant is None else variant.variant_sha256,
            entity_ids=tuple(entity.entity_id for entity in document.entities),
            applicable_domains=_domains_for_document(document),
        )
        return StandardsTargetView(label=label, target=target, variant_id=variant_id)

    def history_for_target(
        self,
        target: StandardsEvaluationTarget,
    ) -> tuple[StandardsEvaluation, ...]:
        return tuple(
            item
            for item in self.repository.list_evaluations_for_scene(
                target.scene_revision_id
            )
            if item.target == target
        )

    def latest_evaluation(
        self,
        profile: StandardsProfile,
        *,
        variant_id: str | None,
    ) -> StandardsEvaluation | None:
        target = self.target_view(variant_id).target
        matches = (
            item
            for item in self.history_for_target(target)
            if item.profile_id == profile.profile_id
            and item.profile_version == profile.version
            and item.profile_semantic_hash == profile.profile_semantic_hash
        )
        return next(reversed(tuple(matches)), None)

    def evaluate(
        self,
        profile: StandardsProfile,
        *,
        variant_id: str | None,
    ) -> StandardsEvaluation:
        view = self.target_view(variant_id)
        target = view.target
        revision = self.scene_repository.get(target.scene_revision_id)
        if revision is None:
            raise ValueError("保存済みの部屋状態がありません")
        variant = None
        document = revision.document
        if variant_id is not None:
            variant = self.variant_repository.get_variant(variant_id)
            if variant is None or variant.document_id != self.document_id:
                raise KeyError(variant_id)
            document = materialize_system_variant(revision, variant)

        # Layout-derivable quantities are recomputed against the exact target
        # every run; retained manual observations for criteria this lane does
        # not derive carry forward unchanged.
        derived = derive_layout_observations(
            repository=self.repository,
            profile=profile,
            target=target,
            document=document,
            observed_at_utc=_utc_now(),
        )
        derived_ids = {item.criterion_id for item in derived}

        history = self.history_for_target(target)
        prior = [
            item
            for item in history
            if item.profile_id == profile.profile_id
        ]
        allowed_ids = {criterion.criterion_id for criterion in profile.criteria}
        carried = (
            tuple(
                item
                for item in prior[-1].observations
                if item.criterion_id in allowed_ids
                and item.criterion_id not in derived_ids
            )
            if prior
            else ()
        )
        observations = tuple(
            sorted(
                derived + carried,
                key=lambda item: item.criterion_id,
            )
        )
        matching = [
            item
            for item in prior
            if item.profile_version == profile.version
            and item.profile_semantic_hash == profile.profile_semantic_hash
            and item.observations == observations
        ]
        if matching:
            return matching[-1]

        evaluation = evaluate_standards_profile(
            profile=profile,
            target=target,
            observations=observations,
            created_at_utc=_utc_now(),
            reevaluation_of_id=prior[-1].evaluation_id if prior else None,
        )
        return self.repository.save_evaluation(evaluation)

    @staticmethod
    def hard_constraint_gate(
        evaluation: StandardsEvaluation,
        selected_criterion_ids: tuple[str, ...],
    ) -> StandardsHardConstraintGate:
        return explicit_hard_constraint_gate(
            evaluation,
            selected_criterion_ids=selected_criterion_ids,
        )


def _rule_text(criterion: CriterionDefinition) -> str:
    rule = criterion.rule
    unit = f" {criterion.unit}" if criterion.unit else ""
    if rule.operator == "min":
        op = "以上" if rule.lower_inclusive else "より大きい"
        return f"{rule.minimum:g}{unit} {op}"
    if rule.operator == "max":
        op = "以下" if rule.upper_inclusive else "未満"
        return f"{rule.maximum:g}{unit} {op}"
    if rule.operator == "range":
        lower = "以上" if rule.lower_inclusive else "より大きい"
        upper = "以下" if rule.upper_inclusive else "未満"
        return f"{rule.minimum:g}{unit} {lower} / {rule.maximum:g}{unit} {upper}"
    expected = rule.expected
    if isinstance(expected, bool):
        expected_text = "はい" if expected else "いいえ"
    else:
        expected_text = str(expected)
    return f"{expected_text} と一致"


def _observed_text(result: CriterionEvaluationResult) -> str:
    value = result.observed_value
    if value is None:
        return "—"
    if isinstance(value, bool):
        text = "はい" if value else "いいえ"
    else:
        text = str(value)
    return f"{text} {result.unit or ''}".strip()


def _reason_text(result: CriterionEvaluationResult) -> str:
    parts = [_REASON_JA.get(result.reason_code, result.reason_code)]
    if result.missing_inputs:
        parts.append("不足入力: " + ", ".join(result.missing_inputs))
    if result.missing_capabilities:
        parts.append("不足能力: " + ", ".join(result.missing_capabilities))
    return " / ".join(parts)


def _profile_source_text(profile: StandardsProfile) -> str:
    sources: list[str] = []
    for criterion in profile.criteria:
        value = (
            f"{criterion.source.publisher} · {criterion.source.document_version} · "
            f"{criterion.source.reference}"
        )
        if value not in sources:
            sources.append(value)
    return "\n".join(sources)


class StandardsCriterionPanel(QFrame):
    """Compact criterion evidence view used by Room."""

    evaluationChanged = Signal(object)

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.model = StandardsWorkspaceModel(scene_repository, document_id)
        self._evaluation: StandardsEvaluation | None = None
        self._selected_constraints: set[str] = set()
        self._announcer = DynamicAnnouncer(self)
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel("配置基準")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        note = QLabel(
            "基準は項目ごとの証拠です。総合点や自動推奨には使いません。"
        )
        note.setWordWrap(True)
        set_typography_role(note, TypographyRole.SECONDARY)
        layout.addWidget(note)

        self.profile_combo = QComboBox()
        self.profile_combo.setAccessibleName("規格プロファイル")
        self.profile_combo.setToolTip(
            "評価に使う配置基準のセット（規格プロファイル）です。"
        )
        # Long profile names must not widen the dock — cap the size hint;
        # the popup still shows full text.
        self.profile_combo.setMinimumContentsLength(12)
        self.profile_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        layout.addWidget(self.profile_combo)

        self.target_combo = QComboBox()
        self.target_combo.setAccessibleName("規格ターゲット")
        self.target_combo.setToolTip(
            "基準を評価する対象（現在の部屋や候補）を選びます。"
        )
        self.target_combo.setMinimumContentsLength(12)
        self.target_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.target_combo.currentIndexChanged.connect(self.refresh)
        layout.addWidget(self.target_combo)

        self.profile_meta = QLabel()
        self.profile_meta.setWordWrap(True)
        set_typography_role(self.profile_meta, TypographyRole.SECONDARY)
        layout.addWidget(self.profile_meta)

        self.evaluate_button = QPushButton("この基準で評価")
        self.evaluate_button.setObjectName('standards-evaluate')
        self.evaluate_button.setToolTip(
            "選択した対象をこの基準で評価し、各項目の適合状態を更新します。"
        )
        set_control_size(self.evaluate_button, ControlSize.STANDARD)
        self.evaluate_button.clicked.connect(self.evaluate_selected)
        layout.addWidget(self.evaluate_button)
        # #975: a disabled evaluate button leaves the Tab order — the
        # reason + resolution path live in this focusable label.
        self.evaluate_hint = reason_label('', self)
        self.evaluate_hint.setVisible(False)
        layout.addWidget(self.evaluate_hint)

        self.editor_button = QPushButton("プロファイルを編集 / 複製…")
        set_control_size(self.editor_button, ControlSize.COMPACT)
        self.editor_button.setToolTip(
            "ユーザー定義プロファイルの作成・複製・新バージョン発行・JSON入出力"
        )
        self.editor_button.clicked.connect(self._open_profile_editor)
        layout.addWidget(self.editor_button)

        constraint_note = QLabel(
            "チェックした基準だけを配置制約として使用します。"
            " 未選択の不適合は候補を除外しません。"
        )
        constraint_note.setWordWrap(True)
        set_typography_role(constraint_note, TypographyRole.SECONDARY)
        layout.addWidget(constraint_note)

        self.tree = QTreeWidget()
        self.tree.setMinimumWidth(0)
        self.tree.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Expanding,
        )
        self.tree.setObjectName("standardsCriterionTree")
        self.tree.setHeaderLabels(
            ["配置制約 / 基準", "状態", "観測値", "必要条件", "証拠"]
        )
        self.tree.setToolTip(
            "配置基準の項目一覧です。チェックした項目は候補生成時のハード制約になります。"
        )
        _std_header = self.tree.headerItem()
        for _c, _t in {
            0: '基準項目の名前。チェックすると配置制約として使われます',
            1: '評価結果（適合・不適合・未評価）',
            2: '対象について実際に観測・計算された値',
            3: '基準が要求する値・範囲',
            4: '判定の根拠となる証拠',
        }.items():
            _std_header.setToolTip(_c, _t)
        header = self.tree.header()
        header.setMinimumSectionSize(0)
        for index in range(self.tree.columnCount()):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
        self.tree.itemChanged.connect(self._constraint_changed)
        layout.addWidget(self.tree, 1)

        self.gate_label = QLabel()
        self.gate_label.setWordWrap(True)
        layout.addWidget(self.gate_label)

        self.advanced_button = QPushButton("詳細 / 出典")
        self.advanced_button.clicked.connect(self.show_advanced)
        set_control_size(self.advanced_button, ControlSize.COMPACT)
        layout.addWidget(self.advanced_button)

        self.refresh_profiles()

    def refresh_profiles(self) -> None:
        current = self.profile_combo.currentData()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self.model.profiles():
            kind = "ユーザー定義" if profile.profile_kind == "user_defined" else "組み込み"
            self.profile_combo.addItem(
                f"{profile.name} · {profile.version} · {kind}",
                _profile_key(profile),
            )
        if current is not None:
            index = self.profile_combo.findData(current)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)
        self.refresh_targets()
        self.refresh()

    def _profile_changed(self, *_args) -> None:
        # Hard-constraint opt-in is profile/version specific. Switching the
        # authority must never carry criterion IDs into another evaluation.
        self._selected_constraints.clear()
        self.refresh()

    def _open_profile_editor(self) -> None:
        from .standards_profile_editor import (
            StandardsProfileEditorDialog,
            StandardsProfileLibraryService,
        )

        service = StandardsProfileLibraryService(self.model.repository)
        dialog = StandardsProfileEditorDialog(service, parent=self)
        dialog.exec()
        self.refresh_profiles()

    def refresh_targets(self) -> None:
        current = self.target_combo.currentData()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        for target in self.model.targets():
            self.target_combo.addItem(target.label, target.variant_id)
        if current is not None:
            index = self.target_combo.findData(current)
            if index >= 0:
                self.target_combo.setCurrentIndex(index)
        self.target_combo.blockSignals(False)

    def selected_profile(self) -> StandardsProfile | None:
        key = self.profile_combo.currentData()
        if key is None:
            return None
        profile_id, version = str(key).split("\x1f", 1)
        return self.model.profile(profile_id, version)

    def selected_variant_id(self) -> str | None:
        value = self.target_combo.currentData()
        return None if value is None else str(value)

    @property
    def evaluation(self) -> StandardsEvaluation | None:
        return self._evaluation

    @property
    def selected_criterion_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._selected_constraints))

    def select_profile(self, profile_id: str, version: str) -> bool:
        index = self.profile_combo.findData(f"{profile_id}\x1f{version}")
        if index < 0:
            return False
        self.profile_combo.setCurrentIndex(index)
        return True

    def select_target_variant(self, variant_id: str | None) -> bool:
        index = self.target_combo.findData(variant_id)
        if index < 0:
            return False
        self.target_combo.setCurrentIndex(index)
        return True

    def evaluate_selected(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return
        self._evaluation = self.model.evaluate(
            profile,
            variant_id=self.selected_variant_id(),
        )
        self.refresh()
        self.evaluationChanged.emit(self._evaluation)

    def _sync_evaluate_enabled(self) -> None:
        """#975: gate 'この基準で評価' honestly — disabled with the
        reason AND the resolution path shown on this screen; never
        enabled just so the surface looks complete."""
        reason = None
        if self.selected_profile() is None:
            reason = disabled_hint(
                'standards.evaluate',
                '評価を実行できません: 利用できる基準プロファイルがありません',
                '「プロファイルを編集 / 複製…」からプロファイルを'
                '作成してください',
            )
        elif self.target_combo.count() == 0:
            reason = disabled_hint(
                'standards.evaluate',
                '評価を実行できません: 評価対象がまだ登録されていません',
                '部屋または候補が登録されると評価できます',
            )
        self.evaluate_button.setEnabled(reason is None)
        self.evaluate_hint.setText(reason or '')
        self.evaluate_hint.setVisible(reason is not None)
        if reason is not None:
            self.evaluate_button.setToolTip(reason)
        else:
            self.evaluate_button.setToolTip(
                "選択した対象をこの基準で評価し、各項目の適合状態を更新します。"
            )

    def refresh(self, *_args) -> None:
        profile = self.selected_profile()
        self._sync_evaluate_enabled()
        if profile is None:
            self.tree.clear()
            self.profile_meta.setText("利用できる基準プロファイルがありません")
            self._evaluation = None
            self._refresh_gate()
            self._announcer.announce_state(
                'evaluation', None, 'operation_completed', ''
            )
            return
        domains = sorted(
            {domain for item in profile.criteria for domain in item.applicable_domains}
        )
        self.profile_meta.setText(
            f"version {profile.version} · 対象: {', '.join(domains)}\n"
            f"{_profile_source_text(profile)}"
        )
        self._evaluation = self.model.latest_evaluation(
            profile,
            variant_id=self.selected_variant_id(),
        )
        self._render_results(profile)
        self._refresh_gate()
        # #975: a new/updated evaluation is announced once per
        # evaluation_id — re-render of unchanged state stays silent.
        evaluation = self._evaluation
        if evaluation is None:
            self._announcer.announce_state(
                'evaluation', None, 'operation_completed', ''
            )
        else:
            counts = {'PASS': 0, 'FAIL': 0, 'UNKNOWN': 0}
            for item in evaluation.results:
                counts[item.status] = counts.get(item.status, 0) + 1
            self._announcer.announce_state(
                'evaluation',
                evaluation.evaluation_id,
                'operation_completed',
                '基準評価を更新しました — '
                f"適合 {counts['PASS']} / 不適合 {counts['FAIL']} / "
                f"判定材料不足 {counts['UNKNOWN']} 件",
            )

    def _render_results(self, profile: StandardsProfile) -> None:
        results = (
            {}
            if self._evaluation is None
            else {item.criterion_id: item for item in self._evaluation.results}
        )
        # #975: the tree is rebuilt from scratch — capture the
        # criterion_id under the keyboard anchor first, then re-point
        # it at the rebuilt item (or keep focus on the tree when the
        # criterion vanished).
        token = capture_focus(self)
        self.tree.blockSignals(True)
        self.tree.clear()
        for criterion in profile.criteria:
            result = results.get(criterion.criterion_id)
            item = QTreeWidgetItem()
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                0,
                Qt.CheckState.Checked
                if criterion.criterion_id in self._selected_constraints
                else Qt.CheckState.Unchecked,
            )
            item.setText(0, criterion.name)
            item.setData(0, Qt.ItemDataRole.UserRole, criterion.criterion_id)
            if result is None:
                item.setText(1, "未評価")
                item.setText(2, "—")
                item.setText(3, _rule_text(criterion))
                item.setText(4, "評価を実行してください")
            else:
                item.setText(1, _STATUS_JA[result.status])
                item.setText(2, _observed_text(result))
                item.setText(3, _rule_text(criterion))
                item.setText(
                    4,
                    f"{_EVIDENCE_JA[result.evidence_basis]} · {_reason_text(result)}",
                )
                item.setData(1, Qt.ItemDataRole.UserRole, result.status)
            self.tree.addTopLevelItem(item)
        self.tree.blockSignals(False)
        restore_focus(self, token, fallback=self.evaluate_button)

    def _constraint_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        criterion_id = item.data(0, Qt.ItemDataRole.UserRole)
        if criterion_id is None:
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            self._selected_constraints.add(str(criterion_id))
        else:
            self._selected_constraints.discard(str(criterion_id))
        self._refresh_gate()

    def _refresh_gate(self) -> None:
        if self._evaluation is None:
            self.gate_label.setText("配置制約: 評価後に判定します")
            set_semantic_state(self.gate_label, None)
            self._announcer.announce_state(
                'gate', None, 'operation_completed', ''
            )
            return
        gate = self.model.hard_constraint_gate(
            self._evaluation,
            self.selected_criterion_ids,
        )
        # #975: block/unblock transitions are announced once per state —
        # the resolution path is in the same label for keyboard users.
        if gate.allowed:
            self.gate_label.setText(
                "配置制約: 許可 · 未選択の不適合は証拠表示のみです"
            )
            set_semantic_state(self.gate_label, SemanticState.SUCCESS)
            self._announcer.announce_state(
                'gate',
                'allowed',
                'operation_unblocked',
                '配置制約のブロックは解除されています',
            )
        else:
            blocked_ids = ', '.join(gate.blocking_criterion_ids)
            self.gate_label.setText(
                "配置制約: ブロック · "
                + blocked_ids
                + " · 判定材料不足も選択時は安全側にブロックします。"
                + "解消: 該当項目のチェックを外すか、対象を修正して"
                + "再評価してください"
            )
            set_semantic_state(self.gate_label, SemanticState.WARNING)
            self._announcer.announce_state(
                'gate',
                f'blocked:{blocked_ids}',
                'operation_blocked',
                f'配置制約でブロックされています: {blocked_ids}',
            )

    def advanced_text(self) -> str:
        profile = self.selected_profile()
        if profile is None:
            return "基準プロファイルがありません"
        lines = [
            f"プロファイル: {profile.profile_id} / {profile.version}",
            f"プロファイル SHA-256: {profile.profile_semantic_hash}",
        ]
        if self._evaluation is None:
            lines.append("Evaluation: 未評価")
        else:
            evaluation = self._evaluation
            lines.extend(
                [
                    f"シーンリビジョン: {evaluation.target.scene_revision_id}",
                    f"シーン内容 SHA-256: {evaluation.target.scene_content_hash}",
                    f"システムバリアント: {evaluation.target.system_variant_id or 'なし'}",
                    f"システムバリアント SHA-256: {evaluation.target.system_variant_sha256 or 'なし'}",
                    f"評価: {evaluation.evaluation_id}",
                    f"評価 SHA-256: {evaluation.evaluation_sha256}",
                    f"評価器: {evaluation.evaluator_version}",
                    f"評価日時: {evaluation.created_at_utc}",
                    f"再評価対象: {evaluation.reevaluation_of_id or 'なし'}",
                ]
            )
            result_by_id = {item.criterion_id: item for item in evaluation.results}
            for criterion in profile.criteria:
                result = result_by_id[criterion.criterion_id]
                authority = criterion.source.authority_ref
                lines.extend(
                    [
                        "",
                        f"[{criterion.criterion_id}] {criterion.name}",
                        f"出典: {criterion.source.publisher} / {criterion.source.document_title}",
                        f"出典バージョン: {criterion.source.document_version}",
                        f"reference: {criterion.source.reference}",
                        f"コンテンツ種別: {criterion.source.content_kind or 'なし'}",
                        "出典権威: "
                        + (
                            authority.authority_id
                            if authority is not None
                            else "なし"
                        ),
                        f"抽出: {criterion.source.extraction_id or 'なし'}",
                        f"基準 SHA-256: {result.criterion_sha256}",
                        "証拠: "
                        + (
                            ", ".join(
                                f"{ref.evidence_id}"
                                + (
                                    f" ({ref.evidence_sha256})"
                                    if ref.evidence_sha256
                                    else ""
                                )
                                for ref in result.evidence_refs
                            )
                            or "なし"
                        ),
                    ]
                )
        return "\n".join(lines)

    def show_advanced(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("規格プロファイル詳細 / 出典")
        dialog.resize(720, 520)
        layout = QVBoxLayout(dialog)
        label = QLabel(self.advanced_text())
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setWordWrap(True)
        layout.addWidget(label)
        close = QPushButton("閉じる")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()


class StandardsVariantComparisonPanel(QFrame):
    """Criterion matrix for O100 SystemVariant comparison; never ranks variants."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.model = StandardsWorkspaceModel(scene_repository, document_id)
        self._selected_constraints: set[str] = set()
        self._targets: tuple[StandardsTargetView, ...] = ()
        self._evaluations: dict[str | None, StandardsEvaluation | None] = {}
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        title = QLabel("配置基準の項目別比較")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        note = QLabel(
            "各基準項目を独立表示します。適合数による自動優先・総合点・暗黙の最適化目的は作りません。"
        )
        note.setWordWrap(True)
        set_typography_role(note, TypographyRole.SECONDARY)
        layout.addWidget(note)

        controls = QHBoxLayout()
        self.profile_combo = QComboBox()
        self.profile_combo.setToolTip(
            "比較に使う配置基準のセット（規格プロファイル）です。"
        )
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        controls.addWidget(QLabel("基準"))
        controls.addWidget(self.profile_combo, 1)
        self.evaluate_button = QPushButton("各構成を評価")
        self.evaluate_button.setToolTip(
            "全ての構成（システムバリアント）をこの基準で評価し、項目ごとの適合状態を更新します。"
        )
        self.evaluate_button.clicked.connect(self.evaluate_all)
        controls.addWidget(self.evaluate_button)
        self.editor_button = QPushButton("プロファイル編集…")
        set_control_size(self.editor_button, ControlSize.COMPACT)
        self.editor_button.clicked.connect(self._open_profile_editor)
        controls.addWidget(self.editor_button)
        layout.addLayout(controls)

        self.matrix = QTreeWidget()
        self.matrix.setObjectName("standardsVariantMatrix")
        self.matrix.setToolTip(
            "基準項目を行、構成を列とする適合状態の比較です。"
            "チェックした項目は候補生成時のハード制約になります。"
        )
        self.matrix.itemChanged.connect(self._constraint_changed)
        layout.addWidget(self.matrix, 1)

        self.gate_label = QLabel()
        self.gate_label.setWordWrap(True)
        layout.addWidget(self.gate_label)

        self._load_profiles()

    def _open_profile_editor(self) -> None:
        from .standards_profile_editor import (
            StandardsProfileEditorDialog,
            StandardsProfileLibraryService,
        )

        service = StandardsProfileLibraryService(self.model.repository)
        dialog = StandardsProfileEditorDialog(service, parent=self)
        dialog.exec()
        self._load_profiles()

    def _load_profiles(self) -> None:
        current = self.profile_combo.currentData()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self.model.profiles():
            self.profile_combo.addItem(
                f"{profile.name} · {profile.version}",
                _profile_key(profile),
            )
        if current is not None:
            index = self.profile_combo.findData(current)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)
        self.refresh()

    def _profile_changed(self, *_args) -> None:
        # Constraint opt-in belongs to the exact StandardsProfile version.
        # Require a fresh explicit selection after changing that authority.
        self._selected_constraints.clear()
        self.refresh()

    def selected_profile(self) -> StandardsProfile | None:
        key = self.profile_combo.currentData()
        if key is None:
            return None
        profile_id, version = str(key).split("\x1f", 1)
        return self.model.profile(profile_id, version)

    def select_profile(self, profile_id: str, version: str) -> bool:
        index = self.profile_combo.findData(f"{profile_id}\x1f{version}")
        if index < 0:
            return False
        self.profile_combo.setCurrentIndex(index)
        return True

    @property
    def selected_criterion_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._selected_constraints))

    def evaluate_all(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return
        for target in self.model.targets():
            self.model.evaluate(profile, variant_id=target.variant_id)
        self.refresh()

    def refresh(self, *_args) -> None:
        profile = self.selected_profile()
        if profile is None:
            self.matrix.clear()
            return
        self._targets = self.model.targets()
        self._evaluations = {
            target.variant_id: self.model.latest_evaluation(
                profile,
                variant_id=target.variant_id,
            )
            for target in self._targets
        }
        self.matrix.blockSignals(True)
        self.matrix.clear()
        self.matrix.setColumnCount(2 + len(self._targets))
        self.matrix.setHeaderLabels(
            ["配置制約 / 基準", "必要条件"]
            + [target.label for target in self._targets]
        )
        header = self.matrix.header()
        for index in range(self.matrix.columnCount()):
            header.setSectionResizeMode(
                index,
                QHeaderView.ResizeMode.ResizeToContents
                if index < 2
                else QHeaderView.ResizeMode.Stretch,
            )
        for criterion in profile.criteria:
            item = QTreeWidgetItem()
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                0,
                Qt.CheckState.Checked
                if criterion.criterion_id in self._selected_constraints
                else Qt.CheckState.Unchecked,
            )
            item.setText(0, criterion.name)
            item.setData(0, Qt.ItemDataRole.UserRole, criterion.criterion_id)
            item.setText(1, _rule_text(criterion))
            for offset, target in enumerate(self._targets, start=2):
                evaluation = self._evaluations[target.variant_id]
                if evaluation is None:
                    item.setText(offset, "未評価")
                    continue
                result = next(
                    value
                    for value in evaluation.results
                    if value.criterion_id == criterion.criterion_id
                )
                item.setText(offset, _STATUS_JA[result.status])
                item.setData(offset, Qt.ItemDataRole.UserRole, result.status)
                item.setToolTip(
                    offset,
                    f"観測: {_observed_text(result)}\n"
                    f"証拠: {_EVIDENCE_JA[result.evidence_basis]}\n"
                    f"{_reason_text(result)}\n"
                    f"source: {criterion.source.reference}",
                )
            self.matrix.addTopLevelItem(item)
        self.matrix.blockSignals(False)
        self._refresh_gate()

    def _constraint_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        criterion_id = item.data(0, Qt.ItemDataRole.UserRole)
        if criterion_id is None:
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            self._selected_constraints.add(str(criterion_id))
        else:
            self._selected_constraints.discard(str(criterion_id))
        self._refresh_gate()

    def gate_for_variant(
        self,
        variant_id: str | None,
    ) -> StandardsHardConstraintGate | None:
        evaluation = self._evaluations.get(variant_id)
        if evaluation is None:
            return None
        return self.model.hard_constraint_gate(
            evaluation,
            self.selected_criterion_ids,
        )

    def _refresh_gate(self) -> None:
        parts: list[str] = []
        any_blocked = False
        for target in self._targets:
            gate = self.gate_for_variant(target.variant_id)
            if gate is None:
                parts.append(f"{target.label}: 未評価")
                continue
            if gate.allowed:
                parts.append(f"{target.label}: 制約上は許可")
            else:
                any_blocked = True
                parts.append(
                    f"{target.label}: 制約でブロック ({', '.join(gate.blocking_criterion_ids)})"
                )
        self.gate_label.setText(" / ".join(parts))
        set_semantic_state(
            self.gate_label,
            SemanticState.WARNING if any_blocked else None,
        )


__all__ = [
    "StandardsCriterionPanel",
    "StandardsTargetView",
    "StandardsVariantComparisonPanel",
    "StandardsWorkspaceModel",
]
