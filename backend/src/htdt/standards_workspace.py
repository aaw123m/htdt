from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

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
    reevaluate_standards_profile,
)
from .cad_standards_authorities import builtin_standards_source_authorities
from .cad_standards_profiles import builtin_standards_profiles
from .cad_standards_repository import CadStandardsRepository
from .cad_system_variant import materialize_system_variant
from .cad_system_variant_repository import CadSystemVariantRepository
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
    "comparison_passed": "基準を満たしています",
    "comparison_failed": "基準を満たしていません",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                raise ValueError("SystemVariantの基準SceneRevisionがありません")
            if revision.content_hash != variant.baseline_content_hash:
                raise ValueError("SystemVariantの基準SceneRevisionが一致しません")
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
        target = self.target_view(variant_id).target
        history = self.history_for_target(target)
        exact = [
            item
            for item in history
            if item.profile_id == profile.profile_id
            and item.profile_version == profile.version
            and item.profile_semantic_hash == profile.profile_semantic_hash
        ]
        if exact:
            return exact[-1]

        prior = [
            item
            for item in history
            if item.profile_id == profile.profile_id
        ]
        if prior:
            previous = prior[-1]
            allowed_ids = {criterion.criterion_id for criterion in profile.criteria}
            observations = tuple(
                item
                for item in previous.observations
                if item.criterion_id in allowed_ids
            )
            evaluation = reevaluate_standards_profile(
                previous=previous,
                profile=profile,
                observations=observations,
                created_at_utc=_utc_now(),
            )
        else:
            evaluation = evaluate_standards_profile(
                profile=profile,
                target=target,
                observations=(),
                created_at_utc=_utc_now(),
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
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel("配置基準")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        note = QLabel(
            "基準は項目ごとの証拠です。総合点や自動推薦には使いません。"
        )
        note.setWordWrap(True)
        set_typography_role(note, TypographyRole.SECONDARY)
        layout.addWidget(note)

        self.profile_combo = QComboBox()
        self.profile_combo.setAccessibleName("StandardsProfile")
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        layout.addWidget(self.profile_combo)

        self.target_combo = QComboBox()
        self.target_combo.setAccessibleName("Standards target")
        self.target_combo.currentIndexChanged.connect(self.refresh)
        layout.addWidget(self.target_combo)

        self.profile_meta = QLabel()
        self.profile_meta.setWordWrap(True)
        set_typography_role(self.profile_meta, TypographyRole.SECONDARY)
        layout.addWidget(self.profile_meta)

        self.evaluate_button = QPushButton("この基準で評価")
        set_control_size(self.evaluate_button, ControlSize.STANDARD)
        self.evaluate_button.clicked.connect(self.evaluate_selected)
        layout.addWidget(self.evaluate_button)

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
        header = self.tree.header()
        header.setMinimumSectionSize(0)
        for index in range(self.tree.columnCount()):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
        self.tree.itemChanged.connect(self._constraint_changed)
        layout.addWidget(self.tree, 1)

        self.gate_label = QLabel()
        self.gate_label.setWordWrap(True)
        layout.addWidget(self.gate_label)

        self.advanced_button = QPushButton("詳細 / provenance")
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

    def refresh(self, *_args) -> None:
        profile = self.selected_profile()
        if profile is None:
            self.tree.clear()
            self.profile_meta.setText("利用できる基準プロファイルがありません")
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

    def _render_results(self, profile: StandardsProfile) -> None:
        results = (
            {}
            if self._evaluation is None
            else {item.criterion_id: item for item in self._evaluation.results}
        )
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
            return
        gate = self.model.hard_constraint_gate(
            self._evaluation,
            self.selected_criterion_ids,
        )
        if gate.allowed:
            self.gate_label.setText(
                "配置制約: 許可 · 未選択の不適合は証拠表示のみです"
            )
            set_semantic_state(self.gate_label, SemanticState.SUCCESS)
        else:
            self.gate_label.setText(
                "配置制約: ブロック · "
                + ", ".join(gate.blocking_criterion_ids)
                + " · 判定材料不足も選択時は安全側にブロックします"
            )
            set_semantic_state(self.gate_label, SemanticState.WARNING)

    def advanced_text(self) -> str:
        profile = self.selected_profile()
        if profile is None:
            return "基準プロファイルがありません"
        lines = [
            f"Profile: {profile.profile_id} / {profile.version}",
            f"Profile SHA-256: {profile.profile_semantic_hash}",
        ]
        if self._evaluation is None:
            lines.append("Evaluation: 未評価")
        else:
            evaluation = self._evaluation
            lines.extend(
                [
                    f"SceneRevision: {evaluation.target.scene_revision_id}",
                    f"Scene content SHA-256: {evaluation.target.scene_content_hash}",
                    f"SystemVariant: {evaluation.target.system_variant_id or 'なし'}",
                    f"SystemVariant SHA-256: {evaluation.target.system_variant_sha256 or 'なし'}",
                    f"Evaluation: {evaluation.evaluation_id}",
                    f"Evaluation SHA-256: {evaluation.evaluation_sha256}",
                    f"Evaluator: {evaluation.evaluator_version}",
                    f"Evaluated at: {evaluation.created_at_utc}",
                    f"Re-evaluation of: {evaluation.reevaluation_of_id or 'なし'}",
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
                        f"source: {criterion.source.publisher} / {criterion.source.document_title}",
                        f"source version: {criterion.source.document_version}",
                        f"reference: {criterion.source.reference}",
                        f"content kind: {criterion.source.content_kind or 'なし'}",
                        "source authority: "
                        + (
                            authority.authority_id
                            if authority is not None
                            else "なし"
                        ),
                        f"extraction: {criterion.source.extraction_id or 'なし'}",
                        f"criterion SHA-256: {result.criterion_sha256}",
                        "evidence: "
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
        dialog.setWindowTitle("StandardsProfile 詳細 / provenance")
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
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        controls.addWidget(QLabel("基準"))
        controls.addWidget(self.profile_combo, 1)
        self.evaluate_button = QPushButton("各構成を評価")
        self.evaluate_button.clicked.connect(self.evaluate_all)
        controls.addWidget(self.evaluate_button)
        self.editor_button = QPushButton("プロファイル編集…")
        set_control_size(self.editor_button, ControlSize.COMPACT)
        self.editor_button.clicked.connect(self._open_profile_editor)
        controls.addWidget(self.editor_button)
        layout.addLayout(controls)

        self.matrix = QTreeWidget()
        self.matrix.setObjectName("standardsVariantMatrix")
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
