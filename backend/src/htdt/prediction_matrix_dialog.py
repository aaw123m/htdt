"""REV44: 行列・プロバイダー管理 — the persisted solver-stack surface.

One dialog covering the three previously unreachable product steps:

1. ソルバー結果 — persisted ``AcousticSolverResultEnvelope`` rows for this
   document, re-verified through :class:`PredictionAuthorityLane`; eligible
   rows register as candidate ``LowBandPredictionProvider`` authorities.
2. 登録済みプロバイダー — the validated provider list (replayed on every
   read); a selection derives its spec inputs through
   :meth:`PredictionMatrixService.creation_plan` — a selection that cannot
   produce a spec shows its reasons, it never silently disables.
3. 行列 — the latest persisted spec's run state; 実行 triggers
   ``run_matrix`` through the real batching semantics.

The dialog holds no authority: every write goes through the registered
repositories, every read re-verifies, and a matrix with no runs still
shows 未実行 honestly in the panel's grid.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from .cad_display_labels import state_token_label
from .cad_prediction_provider import LowBandPredictionProvider
from .cad_prediction_registration import (
    PredictionAuthorityLane,
    RegistrableSolverResult,
)
from .prediction_matrix_service import (
    MatrixCreationPlan,
    PredictionMatrixService,
)
from .ui_theme import TypographyRole, set_typography_role
from .user_facing_error import operation_error_message


class PredictionMatrixDialog(QDialog):
    """Manage solver-result registration and the persisted 行列 lifecycle."""

    def __init__(
        self,
        *,
        controller,
        lane: PredictionAuthorityLane,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.lane = lane
        self.document_id = controller.document_id
        self.matrix_service = PredictionMatrixService(
            controller.scene_repository, self.document_id
        )
        self._entries: list[RegistrableSolverResult] = []
        self._providers: list[LowBandPredictionProvider] = []
        self._plan = MatrixCreationPlan()
        self._entity_names: dict[str, str] = {}

        self.setWindowTitle("行列・プロバイダー管理")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)

        results_title = QLabel("ソルバー結果")
        set_typography_role(results_title, TypographyRole.SECONDARY)
        layout.addWidget(results_title)

        self.results = QTreeWidget()
        self.results.setHeaderLabels(["結果", "ソース", "受音点", "状態"])
        self.results.setRootIsDecorated(False)
        self.results.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.results.setMinimumHeight(130)
        self.results.itemSelectionChanged.connect(self._refresh_actions)
        layout.addWidget(self.results)

        self.register_button = QPushButton(
            "選択した結果をプロバイダー登録"
        )
        self.register_button.setToolTip(
            "検証済みのソルバー結果を候補プロバイダーとして登録します"
        )
        self.register_button.clicked.connect(self._register_selected)
        layout.addWidget(self.register_button)

        providers_title = QLabel("登録済みプロバイダー")
        set_typography_role(providers_title, TypographyRole.SECONDARY)
        layout.addWidget(providers_title)

        self.providers = QTreeWidget()
        self.providers.setHeaderLabels(
            ["プロバイダー", "ソース", "受音点数", "状態"]
        )
        self.providers.setRootIsDecorated(False)
        self.providers.setMinimumHeight(110)
        self.providers.itemChanged.connect(self._recompute_plan)
        layout.addWidget(self.providers)

        self.plan_label = QLabel()
        self.plan_label.setWordWrap(True)
        set_typography_role(self.plan_label, TypographyRole.SECONDARY)
        layout.addWidget(self.plan_label)

        self.create_button = QPushButton(
            "選択したプロバイダーで行列を作成"
        )
        self.create_button.setToolTip(
            "選択したプロバイダーの権威から伝達行列仕様を生成します"
        )
        self.create_button.clicked.connect(self._create_matrix)
        layout.addWidget(self.create_button)

        matrix_title = QLabel("行列")
        set_typography_role(matrix_title, TypographyRole.SECONDARY)
        layout.addWidget(matrix_title)

        self.matrix_label = QLabel()
        self.matrix_label.setWordWrap(True)
        layout.addWidget(self.matrix_label)

        self.run_button = QPushButton("最新の行列を実行")
        self.run_button.setToolTip(
            "保存済み行列仕様を登録済みプロバイダーで実行します"
        )
        self.run_button.clicked.connect(self._run_matrix)
        layout.addWidget(self.run_button)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close
        )
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._reload()

    # ------------------------------------------------------------------
    # Loading / display

    def _entity_name(self, entity_id: str | None) -> str:
        if entity_id is None:
            return '—'
        return self._entity_names.get(entity_id, entity_id)

    def _reload(self) -> None:
        revision = self.controller.scene_repository.current_head(
            self.document_id
        )
        self._entity_names = (
            {
                entity.entity_id: entity.name
                for entity in revision.document.entities
            }
            if revision is not None
            else {}
        )
        self._reload_results()
        self._reload_providers()
        self._reload_matrix()
        self._refresh_actions()

    def _reload_results(self) -> None:
        self.results.clear()
        try:
            self._entries = list(
                self.lane.list_registrable_results(self.document_id)
            )
        except ValueError as exc:
            self._entries = []
            self.status.setText(f"ソルバー結果の読み込みに失敗しました: {operation_error_message(exc)}")
        for entry in self._entries:
            state = (
                '登録済み'
                if entry.registered
                else ('登録可' if entry.eligible else '登録不可')
            )
            item = QTreeWidgetItem(
                [
                    f"…{entry.result_id[-12:]}",
                    self._entity_name(entry.source_entity_id),
                    str(len(entry.receiver_entity_ids)) or '—',
                    state,
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, entry.result_id)
            if not entry.eligible and entry.reason:
                item.setToolTip(3, entry.reason)
                item.setToolTip(0, entry.reason)
            self.results.addTopLevelItem(item)
        if not self._entries:
            self.results.addTopLevelItem(
                QTreeWidgetItem(['保存済みソルバー結果がありません', '', '', ''])
            )

    def _reload_providers(self) -> None:
        self.providers.blockSignals(True)
        self.providers.clear()
        try:
            self._providers = list(self.controller.available_providers())
        except ValueError as exc:
            self._providers = []
            self.status.setText(
                f"プロバイダーの読み込みに失敗しました: {operation_error_message(exc)}"
            )
        revision = self.controller.scene_repository.current_head(
            self.document_id
        )
        head_hash = None if revision is None else revision.content_hash
        for provider in self._providers:
            entity_id = (
                provider.source_identity.source_binding.source_entity_id
            )
            stale = (
                head_hash is not None
                and provider.current_authority.scene_content_hash != head_hash
            )
            item = QTreeWidgetItem(
                [
                    f"…{provider.provider_id[-12:]}",
                    self._entity_name(entity_id),
                    str(len(provider.receiver_identities)),
                    '鮮度不足' if stale else '候補',
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, provider.provider_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            if stale:
                item.setToolTip(
                    3,
                    'シーン更新後の未実行プロバイダーです — 再登録してください',
                )
            self.providers.addTopLevelItem(item)
        if not self._providers:
            self.providers.addTopLevelItem(
                QTreeWidgetItem(
                    ['登録済みプロバイダーがありません', '', '', '']
                )
            )
        self.providers.blockSignals(False)
        self._recompute_plan()

    def _reload_matrix(self) -> None:
        presentation = self.matrix_service.matrix_presentation()
        if presentation.spec_id is None:
            self.matrix_label.setText(
                f"行列なし · {presentation.reason or ''}"
            )
            return
        parts = [presentation.spec_name]
        if presentation.run_state is not None:
            parts.append(
                f"実行 {presentation.run_attempt}: "
                f"{state_token_label(presentation.run_state)}"
            )
        else:
            parts.append('未実行')
        if presentation.currency_state is not None:
            parts.append(
                f"鮮度 {state_token_label(presentation.currency_state)}"
            )
        self.matrix_label.setText(' · '.join(parts))

    # ------------------------------------------------------------------
    # Plan / actions

    def _checked_providers(self) -> dict[str, LowBandPredictionProvider]:
        by_id = {
            provider.provider_id: provider for provider in self._providers
        }
        selected: dict[str, LowBandPredictionProvider] = {}
        for index in range(self.providers.topLevelItemCount()):
            item = self.providers.topLevelItem(index)
            provider_id = item.data(0, Qt.ItemDataRole.UserRole)
            provider = by_id.get(provider_id)
            if provider is None or item.checkState(0) != Qt.CheckState.Checked:
                continue
            entity_id = (
                provider.source_identity.source_binding.source_entity_id
            )
            selected[entity_id] = provider
        return selected

    def _recompute_plan(self) -> None:
        self._plan = self.matrix_service.creation_plan(
            self._checked_providers()
        )
        if not self._checked_providers():
            self.plan_label.setText(
                '行列を作成するプロバイダーを選択してください'
            )
        elif self._plan.problems:
            self.plan_label.setText(
                '作成できません: ' + ' / '.join(self._plan.problems)
            )
        else:
            sources = '、'.join(
                self._entity_name(entity_id)
                for entity_id in self._plan.source_entity_ids
            )
            self.plan_label.setText(
                f"作成プラン: {sources} × "
                f"{len(self._plan.receiver_ids)}受音点 · "
                f"{len(self._plan.frequency_axis_hz)}点軸"
            )
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        items = self.results.selectedItems()
        entry = None
        if items:
            result_id = items[0].data(0, Qt.ItemDataRole.UserRole)
            entry = next(
                (
                    item
                    for item in self._entries
                    if item.result_id == result_id
                ),
                None,
            )
        self.register_button.setEnabled(
            entry is not None and entry.eligible and not entry.registered
        )
        self.create_button.setEnabled(
            bool(self._checked_providers()) and not self._plan.problems
        )
        presentation = self.matrix_service.matrix_presentation()
        self.run_button.setEnabled(
            presentation.spec_id is not None and bool(self._providers)
        )

    def _register_selected(self) -> None:
        items = self.results.selectedItems()
        if not items:
            return
        result_id = items[0].data(0, Qt.ItemDataRole.UserRole)
        try:
            provider = self.lane.register_provider(result_id)
        except ValueError as exc:
            self.status.setText(f"プロバイダー登録に失敗しました: {operation_error_message(exc)}")
            return
        self.status.setText(
            f"プロバイダーを登録しました: …{provider.provider_id[-12:]}"
        )
        self._reload()

    def _create_matrix(self) -> None:
        providers = self._checked_providers()
        plan = self._plan
        if not providers or plan.problems:
            return
        try:
            spec = self.matrix_service.create_matrix(
                source_entity_ids=plan.source_entity_ids,
                receiver_ids=plan.receiver_ids,
                providers=providers,
                frequency_axis_hz=plan.frequency_axis_hz,
                solver_implementation_ref=plan.solver_implementation_ref,
                valid_frequency_domain=plan.valid_frequency_domain,
            )
        except ValueError as exc:
            self.status.setText(f"行列の作成に失敗しました: {operation_error_message(exc)}")
            return
        self.status.setText(f"行列を作成しました: {spec.spec_id[:24]}…")
        self._reload_matrix()
        self._refresh_actions()

    def _run_matrix(self) -> None:
        presentation = self.matrix_service.matrix_presentation()
        if presentation.spec_id is None:
            return
        spec = self.matrix_service.repository.get_spec(presentation.spec_id)
        if spec is None:
            return
        providers = {
            provider.source_identity.source_binding.source_entity_id: provider
            for provider in self._providers
        }
        try:
            run = self.matrix_service.run_matrix(spec.spec_id, providers)
        except ValueError as exc:
            self.status.setText(f"行列の実行に失敗しました: {operation_error_message(exc)}")
            return
        self.status.setText(
            f"行列を実行しました: 実行 {run.attempt} "
            f"({state_token_label(run.state)})"
        )
        self._reload_matrix()
        self._refresh_actions()


__all__ = ['PredictionMatrixDialog']
