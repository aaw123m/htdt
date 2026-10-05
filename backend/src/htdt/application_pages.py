"""Application-scope destinations for the workflow shell (UX160 IA v2).

These pages are read-mostly surfaces over existing persisted authorities —
the project library lists real documents, the inbox lists real capture items,
activity lists real revisions, the library wraps EquipmentLibraryService, and
support reports the real diagnostics/version data. None of them fabricate
project state.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .build_info import version_string
from .cad_repository import SceneRepository
from .capture_inbox import capture_inbox_item_project_id
from .navigation_target import (
    NavigationTarget,
    NavigationTargetKind,
    navigation_target_from_uri,
)
from .automatic_backup import AutomaticBackupScheduler
from .project_library_repository import ProjectLibraryRepository
from .project_lifecycle import (
    DeletionBlocker,
    ProjectDeletionBlockedError,
    ProjectDeletionPlan,
    ProjectDeletionStaleError,
    ProjectLibrary as _LifecycleProjectLibrary,
    ProjectTombstone,
)
from .native_diagnostics import diagnostics_dir
from .ui_theme import TypographyRole, set_typography_role
from .user_facing_error import operation_error_message, warn_user
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId
from .workflow_shell import TargetFocusResult


@dataclass(frozen=True, slots=True)
class ProjectEntry:
    """One canonical project row for the Projects listing (#919).

    ``project_id`` is the stable semantic identity used to open a project;
    ``display_name`` is presentation only.
    """

    project_id: str
    document_id: str
    display_name: str
    head_revision_id: str | None
    created_at_utc: str
    revision_count: int
    archived: bool = False


class ProjectLibraryService:
    """Lists canonical projects — read-only over the library authority.

    ``ProjectLibraryRepository`` auto-registers every document that already
    carries scene content, so canonical ``htdt_project_documents`` rows cover
    legacy and unregistered documents alike (#919).
    """

    def __init__(
        self,
        repository: SceneRepository,
        project_library: ProjectLibraryRepository | None = None,
    ) -> None:
        self.path = Path(repository.path)
        self._repository = repository
        self._project_library = project_library or ProjectLibraryRepository(
            repository
        )
        self._lifecycle: _LifecycleProjectLibrary | None = None

    def _lifecycle_library(self) -> _LifecycleProjectLibrary:
        """The lifecycle authority over the same store (archive/delete)."""
        if self._lifecycle is None:
            self._lifecycle = _LifecycleProjectLibrary(self.path)
        return self._lifecycle

    def list_projects(self) -> tuple[ProjectEntry, ...]:
        """All registered projects, archived included (they stay restorable)."""
        heads = self._document_heads()
        return tuple(
            ProjectEntry(
                project_id=entry.project_id,
                document_id=entry.document_id,
                display_name=entry.display_name,
                head_revision_id=(
                    None
                    if entry.document_id not in heads
                    else heads[entry.document_id][0]
                ),
                created_at_utc=entry.created_at_utc,
                revision_count=(
                    0
                    if entry.document_id not in heads
                    else heads[entry.document_id][1]
                ),
                archived=entry.archived,
            )
            for entry in self._project_library.list_projects(
                include_archived=True
            )
        )

    def set_archived(self, project_id: str, archived: bool) -> None:
        """Archive/restore via the lifecycle authority (#753)."""
        if archived:
            self._lifecycle_library().archive_project(project_id)
        else:
            self._lifecycle_library().unarchive_project(project_id)

    def plan_project_deletion(self, project_id: str) -> ProjectDeletionPlan:
        """Read-only deletion preview; nothing is removed yet."""
        return self._lifecycle_library().plan_project_deletion(project_id)

    def delete_project(
        self, project_id: str, *, expected_plan: ProjectDeletionPlan
    ) -> ProjectTombstone:
        """Atomic delete pinned to the plan the user approved."""
        return self._lifecycle_library().delete_project(
            project_id, expected_plan=expected_plan
        )

    def _document_heads(self) -> dict[str, tuple[str, int]]:
        if not self.path.is_file():
            return {}
        try:
            with closing(self._repository._read()) as connection, connection:
                rows = connection.execute(
                    """
                    SELECT h.document_id AS document_id,
                           h.head_revision_id AS head_revision_id,
                           (SELECT COUNT(*) FROM scene_revisions s
                            WHERE s.document_id = h.document_id) AS revisions
                    FROM scene_document_heads h
                    """
                ).fetchall()
        except sqlite3.Error:
            return {}
        return {
            str(document_id): (str(head_revision_id), int(revisions))
            for document_id, head_revision_id, revisions in rows
        }


def _page_layout(page: QWidget, title: str, hint: str | None = None) -> QVBoxLayout:
    layout = QVBoxLayout(page)
    layout.setContentsMargins(28, 24, 28, 24)
    layout.setSpacing(10)
    heading = QLabel(title)
    set_typography_role(heading, TypographyRole.WORKSPACE_TITLE)
    layout.addWidget(heading)
    if hint:
        label = QLabel(hint)
        set_typography_role(label, TypographyRole.SECONDARY)
        label.setWordWrap(True)
        layout.addWidget(label)
    return layout


#: JP labels for the authority tables a deletion plan can enumerate.
#: Unknown names render verbatim — the plan never invents a friendlier
#: name for a table it did not predict.
_LIFECYCLE_TABLE_LABELS = {
    "scene_revisions": "シーンリビジョン",
    "scene_bookmarks": "ブックマーク",
    "scene_review_marks": "レビュー",
    "cad_measurements": "測定",
    "cad_frequency_responses": "周波数応答",
    "cad_impulse_responses": "インパルス応答",
    "cad_calibration_plans": "校正プラン",
    "cad_correction_qualifications": "補正修飾レコード",
    "cad_comparison_records": "比較履歴",
    "cad_measurement_uncertainty_budgets": "測定不確かさ予算",
    "cad_measurement_significance_assessments": "残差有意性評価",
    "cad_measurement_state_policies": "測定状態ポリシー",
    "cad_measurement_state_snapshots": "測定状態スナップショット",
    "cad_measurement_state_verdicts": "測定状態判定",
    "cad_measurement_transforms": "測定変換レコード",
    "cad_decision_rule_specs": "決定ルール仕様",
    "cad_decision_verdicts": "証拠判定レコード",
    "cad_uncertain_input_sets": "不確かさ入力セット",
    "cad_robust_design_assessments": "堅牢設計評価",
    "cad_stimulus_assets": "刺激アセット登録",
    "cad_stimulus_pins": "測定刺激ピン",
    "cad_stimulus_eligibility": "刺激適格性判定",
    "cad_bass_splice_evidence": "バス合成証拠",
    "cad_bass_qualifications": "バス管理適格性",
    "cad_device_backup_artifacts": "デバイスバックアップアーティファクト",
    "cad_device_config_snapshots": "デバイス設定スナップショット",
    "cad_device_firmware_transitions": "ファームウェア更新記録",
    "cad_device_known_good_baselines": "既知良好ベースライン",
    "cad_device_replacement_assessments": "代替機器ポータビリティ評価",
    "cad_device_restore_records": "デバイス設定復元記録",
    "cad_external_standard_documents": "外部規格登録ドキュメント",
    "cad_standard_evaluation_pins": "規格評価ピン",
    "cad_standard_lifecycle_observations": "規格ライフサイクル観測",
    "cad_standard_profile_mappings": "規格プロファイルマッピング",
    "cad_standard_revision_diffs": "規格改版差分",
    "cad_spatial_campaign_designs": "空間測定キャンペーン設計",
    "cad_spatial_campaign_evaluations": "空間キャンペーン設計評価",
    "cad_spatial_campaign_bindings": "測定点キャプチャ束縛",
    "cad_rp32_profiles": "RP32コミッショニングプロファイル",
    "cad_rp32_reconciliations": "RP32設計・実測照合",
    "cad_rp32_readiness": "RP32測定準備評価",
    "cad_rp32_verification_plans": "RP32検証計画",
    "cad_rp32_verification_records": "RP32検証レコード",
    "cad_rp32_reports": "RP32コミッショニングレポート",
}


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0 or unit == "GiB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size):,} B"
        size /= 1024.0
    return f"{value:,} B"


#: Operator-facing rendering of each lifecycle blocker kind — the
#: blocker's ``detail`` is diagnostic English; the dialog shows the kind's
#: localized form with ``count`` supplying the numbers.
_DELETION_BLOCKER_LINES = {
    'project_not_archived': (
        'プロジェクトがまだアクティブです — 先にアーカイブしてください'
    ),
    'active_descendants': (
        'このプロジェクトからクローンされたプロジェクトが {count} 件'
        'あります — 先にそれらを削除またはアーカイブしてください'
    ),
    'pending_capture_missions': (
        'このプロジェクトを対象とするキャプチャミッションが {count} 件'
        '残っています — 先に中止または退役させてください'
    ),
    'pending_inbox_items': (
        'このプロジェクトのキャプチャ受信ボックスに未処理の項目が {count} 件'
        'あります'
    ),
    'unknown_project': 'プロジェクトが見つかりません',
}


def _deletion_blocker_line(blocker: DeletionBlocker) -> str:
    template = _DELETION_BLOCKER_LINES.get(blocker.kind)
    if template is None:
        # A future kind must not render silently wrong copy — keep the
        # authored detail rather than inventing a reason.
        return blocker.detail
    return template.format(count=blocker.count)


def _deletion_plan_lines(plan: ProjectDeletionPlan) -> list[str]:
    """Render the planner's own numbers verbatim — consequence preview."""
    lines = [
        f"対象: {plan.display_name}",
        f"削除対象: 合計 {plan.total_rows} 行"
        f"（約 {_format_bytes(plan.estimated_bytes)}）",
    ]
    for count in plan.authorities:
        lines.append(
            f"・{_LIFECYCLE_TABLE_LABELS.get(count.table, count.table)}: "
            f"{count.row_count} 件（約 {_format_bytes(count.estimated_bytes)}）"
        )
    lines.append(
        f"共有アセット（保持）: {plan.assets.shared_asset_count} 件 / "
        f"{_format_bytes(plan.assets.shared_asset_bytes)}"
    )
    lines.append(
        f"プロジェクト専用アセット（削除後GC対象）: "
        f"{plan.assets.local_asset_count} 件 / "
        f"{_format_bytes(plan.assets.local_asset_bytes)}"
    )
    if plan.pending_mission_count:
        lines.append(
            f"未処理のキャプチャミッション: {plan.pending_mission_count} 件"
        )
    if plan.pending_inbox_item_count:
        lines.append(
            f"未処理の受信ボックス項目: {plan.pending_inbox_item_count} 件"
        )
    return lines


class ProjectLibraryPage(QWidget):
    """Project library: open/switch, archive/restore, delete documents.

    Deletion follows the lifecycle authority's confirm contract: a
    read-only ``plan_project_deletion`` preview listing every consequence
    (per-authority counts, shared-vs-local assets, hard blockers), an
    explicit confirm, then archive-if-needed + ``delete_project`` pinned
    to the plan the user approved. The currently-open project refuses
    lifecycle changes — the shell is standing on it.
    """

    project_open_requested = Signal(str)
    commission_requested = Signal()

    def __init__(
        self,
        service: ProjectLibraryService,
        current_document_id: Callable[[], str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._current_document_id = current_document_id
        self._entries: dict[str, ProjectEntry] = {}
        layout = _page_layout(
            self,
            "プロジェクト",
            "保存済みのプロジェクトです。開くとそのプロジェクトに切り替わります。"
            "アーカイブ済みのプロジェクトは開けず、削除は確認のうえ実行されます。",
        )
        self.table = QTableWidget(0, 5)
        self.table.setAccessibleName("プロジェクト一覧")
        self.table.setToolTip(
            "保存済みプロジェクトの一覧です。列の見出しにカーソルを合わせると各列の説明が表示されます。"
        )
        self.table.setHorizontalHeaderLabels(
            ("プロジェクト", "作成日時", "リビジョン数", "現在", "状態")
        )
        for _col, _tip in enumerate((
            "プロジェクトの表示名",
            "プロジェクトを作成した日時",
            "保存されている版（リビジョン）の数",
            "現在開いているプロジェクトには ● が付きます",
            "アクティブ / アーカイブ済み の状態",
        )):
            self.table.horizontalHeaderItem(_col).setToolTip(_tip)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for column in range(1, self.table.columnCount()):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        layout.addWidget(self.table, 1)

        self.empty_label = QLabel(
            "まだプロジェクトはありません。"
            "「新規プロジェクト…」から作成できます。"
        )
        set_typography_role(self.empty_label, TypographyRole.SECONDARY)
        self.empty_label.setWordWrap(True)
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)

        actions = QHBoxLayout()
        self.open_button = QPushButton("開く")
        # Tooltip stays live while disabled so the gating is discoverable.
        self.open_button.setToolTip("一覧からプロジェクトを選択すると開けます")
        self.open_button.setAttribute(
            Qt.WidgetAttribute.WA_AlwaysShowToolTips, True
        )
        self.open_button.clicked.connect(self._open_selected)
        actions.addWidget(self.open_button)
        self.archive_button = QPushButton("アーカイブ")
        self.archive_button.setToolTip(
            "アクティブなプロジェクトをアーカイブします（データは保持されます）"
        )
        self.archive_button.setAttribute(
            Qt.WidgetAttribute.WA_AlwaysShowToolTips, True
        )
        self.archive_button.clicked.connect(
            lambda: self._set_archived_selected(True)
        )
        actions.addWidget(self.archive_button)
        self.restore_button = QPushButton("アーカイブ解除")
        self.restore_button.setToolTip(
            "アーカイブ済みのプロジェクトをアクティブに戻します"
        )
        self.restore_button.setAttribute(
            Qt.WidgetAttribute.WA_AlwaysShowToolTips, True
        )
        self.restore_button.clicked.connect(
            lambda: self._set_archived_selected(False)
        )
        actions.addWidget(self.restore_button)
        self.delete_button = QPushButton("削除…")
        self.delete_button.setToolTip(
            "削除内容の確認後、プロジェクトを完全に削除します"
        )
        self.delete_button.setAttribute(
            Qt.WidgetAttribute.WA_AlwaysShowToolTips, True
        )
        self.delete_button.clicked.connect(self._delete_selected)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.new_button = QPushButton("新規プロジェクト…")
        self.new_button.setToolTip("新しいプロジェクトの作成を開始します（作成ウィザードが開きます）")
        self.new_button.setWhatsThis("新しいプロジェクトの作成を開始します（作成ウィザードが開きます）")
        self.new_button.clicked.connect(lambda: self.commission_requested.emit())
        layout.addWidget(self.new_button)
        self.refresh()

    def refresh(self) -> None:
        entries = self.service.list_projects()
        self._entries = {entry.project_id: entry for entry in entries}
        current = self._current_document_id()
        self.table.setRowCount(0)
        self.empty_label.setVisible(not entries)
        for entry in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                entry.display_name,
                entry.created_at_utc,
                str(entry.revision_count),
                "●" if entry.document_id == current else "",
                "アーカイブ済み" if entry.archived else "",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry.project_id)
                self.table.setItem(row, column, item)
        self._sync_buttons()

    def _selected_entry(self) -> ProjectEntry | None:
        project_id = self._selected_project_id()
        if project_id is None:
            return None
        return self._entries.get(project_id)

    def _sync_buttons(self) -> None:
        entry = self._selected_entry()
        current = self._current_document_id()
        is_current = entry is not None and entry.document_id == current
        self.open_button.setEnabled(
            entry is not None and not entry.archived
        )
        self.archive_button.setEnabled(
            entry is not None and not entry.archived and not is_current
        )
        self.restore_button.setEnabled(
            entry is not None and entry.archived
        )
        self.delete_button.setEnabled(entry is not None and not is_current)
        if is_current:
            tip = "現在開いているプロジェクトは変更できません"
            self.archive_button.setToolTip(tip)
            self.delete_button.setToolTip(tip)
        else:
            self.archive_button.setToolTip(
                "このプロジェクトはすでにアーカイブ済みです"
                if entry is not None and entry.archived
                else "アクティブなプロジェクトをアーカイブします"
                "（データは保持されます）"
            )
            self.delete_button.setToolTip(
                "削除内容の確認後、プロジェクトを完全に削除します"
            )

    def _selected_project_id(self) -> str | None:
        items = self.table.selectedItems()
        for item in items:
            value = item.data(Qt.ItemDataRole.UserRole)
            if value:
                return str(value)
        return None

    def _open_selected(self) -> None:
        # Open routes through the stable canonical project_id (#919).
        project_id = self._selected_project_id()
        if project_id:
            self.project_open_requested.emit(project_id)

    def _set_archived_selected(self, archived: bool) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        try:
            self.service.set_archived(entry.project_id, archived)
        except Exception as exc:
            warn_user(
                self,
                "アーカイブ" if archived else "アーカイブ解除",
                exc,
            )
            return
        self.refresh()

    def _delete_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None or entry.document_id == self._current_document_id():
            return
        try:
            plan = self.service.plan_project_deletion(entry.project_id)
        except Exception as exc:
            warn_user(self, "削除内容を確認できませんでした", exc)
            return
        non_archive_blockers = [
            blocker
            for blocker in plan.hard_blockers
            if blocker.kind != "project_not_archived"
        ]
        if non_archive_blockers:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("プロジェクトを削除できません")
            box.setText(f"{plan.display_name} は削除をブロックされています。")
            box.setInformativeText(
                "\n".join(
                    f"・{_deletion_blocker_line(blocker)}"
                    for blocker in non_archive_blockers
                )
            )
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.exec()
            return

        # The persisted policy decides whether a pre-destructive safety
        # generation is offered — `load_policy` already falls back to
        # defaults on unreadable state, so this can never raise.
        safety_scheduler = AutomaticBackupScheduler(self.service.path.parent)
        safety_backup_offered = safety_scheduler.policy.enabled

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("プロジェクトを削除")
        box.setText(
            f"「{plan.display_name}」を完全に削除します。"
            "この操作は取り消せません（削除記録はトゥームストーンとして残ります）。"
        )
        detail_lines = _deletion_plan_lines(plan)
        if not entry.archived:
            detail_lines.append(
                "このプロジェクトはまだアクティブです。削除の前に"
                "自動でアーカイブします。"
            )
        if safety_backup_offered:
            detail_lines.append(
                "削除の前に現在のデータの安全バックアップを作成します。"
            )
        detail_lines.append("アセットファイル自体は削除されません。")
        box.setInformativeText("\n".join(detail_lines))
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        safety_backup_created = False
        try:
            if not entry.archived:
                self.service.set_archived(entry.project_id, True)
            # Re-plan so the approved fingerprint matches the world the
            # delete actually validates (archiving lifts the blocker).
            plan = self.service.plan_project_deletion(entry.project_id)
            if not plan.executable:
                raise ProjectDeletionBlockedError(plan)
            if safety_backup_offered:
                # The delete is atomic, but a validated pre-destructive
                # generation is the only way back to the project's
                # content — the designed-for boundary of the 'pre_destructive'
                # trigger. Best-effort: a failed safety net must not strand
                # the deletion itself, so warn and continue.
                try:
                    safety_backup_created = (
                        safety_scheduler.run_due('pre_destructive')
                        is not None
                    )
                except Exception as backup_exc:  # noqa: BLE001
                    warn_user(
                        self,
                        "削除前の安全バックアップを作成できませんでした",
                        backup_exc,
                        effect="削除はこのまま続行します。",
                    )
            tombstone = self.service.delete_project(
                entry.project_id, expected_plan=plan
            )
        except ProjectDeletionStaleError:
            self.refresh()
            QMessageBox.information(
                self,
                "削除できませんでした",
                "プレビュー後にプロジェクトが変更されました。"
                "もう一度削除内容を確認してください。",
            )
            return
        except ProjectDeletionBlockedError as exc:
            self.refresh()
            QMessageBox.warning(
                self,
                "削除をブロックしました",
                "\n".join(
                    f"・{_deletion_blocker_line(blocker)}"
                    for blocker in exc.plan.hard_blockers
                ),
            )
            return
        except Exception as exc:  # noqa: BLE001 - surface any store fault verbatim
            warn_user(self, "削除できませんでした", exc)
            self.refresh()
            return
        self.refresh()
        success_detail = (
            f"{tombstone.display_name}: {tombstone.removed_rows} 行を削除しました。"
        )
        if safety_backup_created:
            success_detail += "削除前の安全バックアップを作成しました。"
        QMessageBox.information(
            self,
            "プロジェクトを削除しました",
            success_detail,
        )


_INBOX_LINEAGE_ROLE = int(Qt.ItemDataRole.UserRole) + 1

_INBOX_GATE_LABELS = {
    "bundle_validation": "バンドル検証",
    "dependency_state": "依存関係",
    "alignment_state": "整列状態",
    "evidence_conflict_state": "証拠競合",
}
_INBOX_GATE_DETAIL_FIELDS = {
    "bundle_validation": "validation_detail",
    "dependency_state": "dependency_detail",
    "alignment_state": "alignment_detail",
    "evidence_conflict_state": "evidence_conflict_detail",
}
_INBOX_DISPOSITION_LABELS = {
    "pending": "保留中",
    "deferred": "延期",
    "partially_promoted": "一部昇格",
    "promoted": "昇格済",
    "rejected": "却下",
    "superseded": "置換済",
}
_INBOX_PROMOTABILITY_LABELS = {
    "promotable": "昇格可能",
    "partially_promotable": "一部昇格可能",
    "blocked": "昇格不可",
    "complete": "昇格完了",
}
_INBOX_OUTCOME_LABELS = {
    "promoted": "昇格成功",
    "blocked": "ブロック",
}
_INBOX_CLASSIFICATION_LABELS = {
    "validation_rejected": "検証で却下",
    "exact_duplicate": "完全一致の重複",
    "identity_digest_conflict": "同一性ダイジェストの競合",
    "revision_variant": "リビジョンバリアント",
    "fills_missing_predecessor": "欠落した先行リビジョンの補完",
    "extends_known_head": "既知の先端の延長",
    "continues_branch": "ブランチの継続",
    "parallel_branch_head": "並行ブランチの先端",
    "new_series": "新しい系列",
}
_INBOX_GATE_STATE_LABELS = {
    "validated": "検証済",
    "rejected": "却下",
    "not_evaluated": "未評価",
    "satisfied": "充足",
    "unresolved": "未解決",
    "not_required": "不要",
    "pending": "保留中",
    "resolved": "解決済",
    "blocked": "ブロック",
    "none": "なし",
    "open": "未解決",
}
_INBOX_AUTHORITY_KIND_LABELS = {
    "raw_visual_evidence": "生の視覚証拠",
    "semantic_geometry": "意味ジオメトリ",
    "annotations": "注釈",
    "measurements": "測定",
    "as_built_observations": "竣工観測",
    "connected_space": "接続空間",
    "reference_targets": "参照ターゲット",
    "supplemental_authority": "補足権威",
}
_INBOX_UNASSIGNED_SCOPE = "capture-inbox-unassigned"


def _inbox_scope_label(scope: str) -> str:
    return "（未割り当て）" if scope == _INBOX_UNASSIGNED_SCOPE else scope


def _classification_label(classification: str) -> str:
    return _INBOX_CLASSIFICATION_LABELS.get(classification, classification)


class CaptureInboxPage(QWidget):
    """Capture Inbox: staged deliveries awaiting review (#770).

    The listing stays compact; selecting a row opens the exact item and
    project context (identity, scope, gate facets, promotability) in a
    detail pane so review, defer/reject, and scope assignment never operate
    on a bare list row.
    """

    def __init__(
        self,
        list_items: Callable[[], tuple],
        on_navigate: Callable[[WorkspaceDeepLink], bool],
        *,
        inspect_item: Callable[[str], object] | None = None,
        defer_item: Callable[[str, str], object] | None = None,
        reject_item: Callable[[str, str], object] | None = None,
        resume_item: Callable[[str], object] | None = None,
        promote_item: Callable[[str, str], object] | None = None,
        list_projects: Callable[[], tuple] | None = None,
        assign_scope: Callable[[str, str], object] | None = None,
        list_contributions: Callable[[], tuple] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_items = list_items
        self._on_navigate = on_navigate
        self._inspect_item = inspect_item
        self._defer_item = defer_item
        self._reject_item = reject_item
        self._resume_item = resume_item
        self._promote_item = promote_item
        self._list_projects = list_projects
        self._assign_scope = assign_scope
        self._list_contributions = list_contributions
        self._contributions: tuple = ()
        self._last_inspection = None
        layout = _page_layout(
            self,
            "取り込み",
            "取得済みのキャプチャ配送です。項目を選ぶと内容と判断材料を確認できます。",
        )
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 5)
        self.table.setAccessibleName("取り込み一覧")
        self.table.setToolTip(
            "受け取ったキャプチャ配送の一覧です。行を選ぶと詳細と操作が下に表示されます。"
        )
        self.table.setHorizontalHeaderLabels(
            ("スコープ", "シリーズ", "分類", "状態", "到着数")
        )
        for _col, _tip in enumerate((
            "届いたデータの対象スコープ（プロジェクトまたは受信機）",
            "同じ測定系列に属するグループ名",
            "内容の種類（周波数応答・写真・メモなど）",
            "取り込みの処理状態（保留・延期・却下など）",
            "その系列で届いた項目の数",
        )):
            self.table.horizontalHeaderItem(_col).setToolTip(_tip)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.itemSelectionChanged.connect(self._sync_detail)
        splitter.addWidget(self.table)

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 4, 0, 0)
        self.detail = QLabel("一覧から項目を選択すると詳細を表示します。")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        set_typography_role(self.detail, TypographyRole.SECONDARY)
        detail_layout.addWidget(self.detail, 1)
        actions = QHBoxLayout()
        self.defer_button = QPushButton("延期…")
        self.defer_button.setToolTip("選択項目の判断をあとに回します（一覧から一時的に外れます）")
        self.defer_button.setWhatsThis("選択項目の判断をあとに回します（一覧から一時的に外れます）")
        self.defer_button.clicked.connect(lambda: self._dispose("defer"))
        actions.addWidget(self.defer_button)
        self.reject_button = QPushButton("却下…")
        self.reject_button.setToolTip("選択項目を取り込まずに破棄します（理由を確認してから実行されます）")
        self.reject_button.setWhatsThis("選択項目を取り込まずに破棄します（理由を確認してから実行されます）")
        self.reject_button.clicked.connect(lambda: self._dispose("reject"))
        actions.addWidget(self.reject_button)
        self.resume_button = QPushButton("再開")
        self.resume_button.setToolTip("延期・却下した項目を再度「保留」に戻して検討対象にします")
        self.resume_button.setWhatsThis("延期・却下した項目を再度「保留」に戻して検討対象にします")
        self.resume_button.clicked.connect(lambda: self._dispose("resume"))
        actions.addWidget(self.resume_button)
        self.promote_button = QPushButton("昇格…")
        self.promote_button.setToolTip("取り込み可能な権威レコード（注釈エンティティ）をプロジェクトのシーンに反映します")
        self.promote_button.setWhatsThis("取り込み可能な権威レコード（注釈エンティティ）をプロジェクトのシーンに反映します")
        self.promote_button.clicked.connect(self._promote)
        actions.addWidget(self.promote_button)
        self.scope_combo = QComboBox()
        self.scope_combo.setToolTip("選択項目を取り込む先のプロジェクトを選びます")
        self.scope_combo.setWhatsThis("選択項目を取り込む先のプロジェクトを選びます")
        actions.addWidget(QLabel("プロジェクト:"))
        actions.addWidget(self.scope_combo, 1)
        self.scope_button = QPushButton("割り当て")
        self.scope_button.setToolTip("選択項目を左で選んだプロジェクトに取り込み（関連付け）ます")
        self.scope_button.setWhatsThis("選択項目を左で選んだプロジェクトに取り込み（関連付け）ます")
        self.scope_button.clicked.connect(self._apply_scope)
        actions.addWidget(self.scope_button)
        link = QPushButton("測定ワークスペースを開く")
        link.setToolTip("測定ワークスペースの「読み込み」ページへ移動します")
        link.setWhatsThis("測定ワークスペースの「読み込み」ページへ移動します")
        link.clicked.connect(
            lambda: self._on_navigate(
                WorkspaceDeepLink(WorkspaceId.MEASUREMENT, "import")
            )
        )
        actions.addWidget(link)
        detail_layout.addLayout(actions)
        splitter.addWidget(detail_panel)
        splitter.setStretchFactor(0, 1)
        tabs = QTabWidget()
        tabs.addTab(splitter, "キャプチャ配送")
        if self._list_contributions is not None:
            tabs.addTab(self._build_contributions_tab(), "フィールドリターン")
        layout.addWidget(tabs, 1)
        self.refresh()

    def _selected_row_data(self) -> tuple[str, str] | None:
        items = self.table.selectedItems()
        for item in items:
            if item.column() != 0:
                continue
            item_id = item.data(Qt.ItemDataRole.UserRole)
            digest = item.data(_INBOX_LINEAGE_ROLE)
            if item_id and digest:
                return str(item_id), str(digest)
        return None

    def _selected_digest(self) -> str | None:
        data = self._selected_row_data()
        return data[1] if data else None

    def _sync_detail(self) -> None:
        digest = self._selected_digest()
        if digest is None:
            if self.table.rowCount() == 0:
                self.detail.setText(
                    "取り込み待ちの配送はありません。"
                    "配送が到着するとここに表示されます。"
                )
            else:
                self.detail.setText("一覧から項目を選択すると詳細を表示します。")
            self._sync_actions(None)
            return
        inspection = (
            self._inspect_item(digest) if self._inspect_item is not None else None
        )
        if inspection is None:
            self.detail.setText(f"項目を確認できません: {digest[:12]}…")
            self._sync_actions(None)
            return
        self._populate_detail(inspection)
        self._sync_actions(inspection)

    def _populate_detail(self, inspection) -> None:
        item = inspection.item
        flags = (
            "・".join(
                _INBOX_CLASSIFICATION_LABELS.get(flag, flag)
                for flag in item.classification_flags
            )
            if item.classification_flags
            else _classification_label(item.primary_classification)
        )
        scope = _inbox_scope_label(item.scope)
        lines = [
            f"スコープ: {scope}",
            f"項目: {item.inbox_item_id.split(':', 1)[-1][:16]}…"
            f" / 系列 {item.capture_series_id} / リビジョン {item.capture_revision_id}",
            f"分類: {_classification_label(item.primary_classification)}（{flags}）",
            f"到着: {item.arrival_source} ×{item.arrival_count}（{item.first_arrived_at_utc}）",
            "ゲート: "
            + " / ".join(
                f"{_INBOX_GATE_LABELS[key]}="
                f"{_INBOX_GATE_STATE_LABELS.get(getattr(item, key), getattr(item, key))}"
                + (
                    f"（{getattr(item, _INBOX_GATE_DETAIL_FIELDS[key], '')}）"
                    if getattr(item, _INBOX_GATE_DETAIL_FIELDS[key], '')
                    else ""
                )
                for key in _INBOX_GATE_LABELS
            ),
            f"昇格可能性: {_INBOX_PROMOTABILITY_LABELS.get(inspection.promotability, inspection.promotability)}"
            + (
                "（昇格対象: "
                + (
                    "・".join(
                        _INBOX_AUTHORITY_KIND_LABELS.get(kind, kind)
                        for kind in inspection.available_authority_kinds
                    )
                    or "なし"
                )
                + "）"
            ),
            (
                "昇格済: "
                + (
                    "・".join(
                        _INBOX_AUTHORITY_KIND_LABELS.get(kind, kind)
                        for kind in inspection.promoted_authority_kinds
                    )
                    or "なし"
                )
                + " / 不可: "
                + (
                    "・".join(
                        _INBOX_AUTHORITY_KIND_LABELS.get(kind, kind)
                        for kind in inspection.blocked_authority_kinds
                    )
                    or "なし"
                )
            ),
            f"内容: 証拠{inspection.source_evidence_count} / "
            f"間取り{inspection.roomplan_record_count} / "
            f"メッシュ{inspection.raw_mesh_count} / "
            f"権威{inspection.authority_record_count}",
            f"状態: {_INBOX_DISPOSITION_LABELS.get(item.disposition, item.disposition)}"
            + (f" — {item.disposition_reason}" if item.disposition_reason else ""),
        ]
        promotions = getattr(inspection, "promotions", ()) or ()
        if promotions:
            latest = max(
                promotions, key=lambda record: record.promoted_at_utc
            )
            lines.append(
                "直近の昇格: "
                f"{_INBOX_AUTHORITY_KIND_LABELS.get(latest.authority_kind, latest.authority_kind)} — "
                f"{_INBOX_OUTCOME_LABELS.get(latest.outcome, latest.outcome)}"
                + (f"（{latest.detail}）" if latest.detail else "")
            )
        if item.operator_notes:
            lines.append(f"メモ: {item.operator_notes}")
        self.detail.setText("\n".join(lines))
        self._populate_scope_combo(item.scope)

    def _populate_scope_combo(self, current_scope: str) -> None:
        self.scope_combo.clear()
        self.scope_combo.addItem("（未割り当て）", "capture-inbox-unassigned")
        if self._list_projects is None:
            return
        current_index = 0
        for entry in self._list_projects():
            label = getattr(entry, "display_name", "") or getattr(
                entry, "project_id", ""
            )
            document_id = getattr(entry, "document_id", "")
            if not document_id:
                continue
            self.scope_combo.addItem(label, document_id)
            if document_id == current_scope:
                current_index = self.scope_combo.count() - 1
        self.scope_combo.setCurrentIndex(current_index)

    def _sync_actions(self, inspection) -> None:
        disposition = (
            getattr(inspection.item, "disposition", None)
            if inspection is not None
            else None
        )
        self._last_inspection = inspection
        self.defer_button.setEnabled(
            self._defer_item is not None and disposition == "pending"
        )
        self.reject_button.setEnabled(
            self._reject_item is not None
            and disposition in ("pending", "deferred")
        )
        self.resume_button.setEnabled(
            self._resume_item is not None
            and disposition in ("deferred", "rejected")
        )
        self.promote_button.setEnabled(
            self._promote_item is not None
            and self._promotable(inspection)
        )
        self.scope_button.setEnabled(
            self._assign_scope is not None
            and disposition in ("pending", "deferred")
        )

    @staticmethod
    def _promotable(inspection) -> bool:
        """True when the item can execute a promotion right now."""

        if inspection is None:
            return False
        item = inspection.item
        if item.disposition not in ("pending", "partially_promoted"):
            return False
        if not capture_inbox_item_project_id(item):
            return False
        return bool(
            set(inspection.available_authority_kinds)
            & {"annotations", "measurements"}
        )

    def _promote(self) -> None:
        digest = self._selected_digest()
        if digest is None or self._promote_item is None:
            return
        reason, ok = QInputDialog.getText(
            self, "昇格", "昇格の理由を入力してください。"
        )
        if not ok or not reason.strip():
            return
        try:
            self._promote_item(digest, reason.strip())
        except Exception as exc:
            warn_user(self, "昇格できませんでした", exc)
            return
        self._refresh_keep_selection()

    def _dispose(self, action: str) -> None:
        digest = self._selected_digest()
        if digest is None:
            return
        handler = {
            "defer": self._defer_item,
            "reject": self._reject_item,
            "resume": self._resume_item,
        }[action]
        if handler is None:
            return
        reason = ""
        if action in ("defer", "reject"):
            title = {"defer": "延期", "reject": "却下"}[action]
            reason, ok = QInputDialog.getText(
                self, title, f"{title}の理由を入力してください。"
            )
            if not ok or not reason.strip():
                return
            reason = reason.strip()
        try:
            handler(digest, reason) if reason else handler(digest)
        except Exception as exc:
            warn_user(self, "取り込みできませんでした", exc)
            return
        self._refresh_keep_selection()

    def _apply_scope(self) -> None:
        digest = self._selected_digest()
        scope = self.scope_combo.currentData()
        if digest is None or not scope or self._assign_scope is None:
            return
        try:
            self._assign_scope(digest, str(scope))
        except Exception as exc:
            warn_user(self, "プロジェクト領域を割り当てできませんでした", exc)
            return
        self._refresh_keep_selection()

    def _refresh_keep_selection(self) -> None:
        selected = self._selected_row_data()
        self.refresh()
        if selected is None:
            return
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, 0)
            if (
                cell is not None
                and cell.data(Qt.ItemDataRole.UserRole) == selected[0]
            ):
                self.table.selectRow(row)
                break
        self._sync_detail()

    def refresh(self) -> None:
        items = self._list_items()
        self.table.setRowCount(0)
        for item in items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate(
                (
                    _inbox_scope_label(item.scope),
                    item.capture_series_id,
                    _classification_label(item.primary_classification),
                    _INBOX_DISPOSITION_LABELS.get(
                        item.disposition, item.disposition
                    ),
                    str(item.arrival_count),
                )
            ):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, item.inbox_item_id)
                    cell.setData(_INBOX_LINEAGE_ROLE, item.lineage_digest)
                self.table.setItem(row, column, cell)
        self._sync_detail()
        self._refresh_contributions()

    # -- field-return contributions -------------------------------------

    _CONTRIB_VALIDATION_LABELS = {
        "validated": "検証済み",
        "unsupported": "未対応バージョン",
        "malformed": "不正",
    }
    _CONTRIB_ROUTING_LABELS = {
        "exact_project_match": "プロジェクト一致",
        "known_project_lineage": "系譜一致",
        "unknown_project_reference": "不明なプロジェクト参照",
        "legacy_project_ref": "従来参照",
        "channel_project_match": "ペアリング割当",
        "unrouted": "未振分",
    }

    def _build_contributions_tab(self) -> QWidget:
        """Received field-return contributions staged by the receiver."""

        panel = QWidget()
        layout = QVBoxLayout(panel)
        intro = QLabel(
            "ペアリング済みデバイスから届いたフィールドリターン（現地作業の完了報告）"
            "の一覧です。項目を選ぶと詳細を確認できます。"
        )
        intro.setWordWrap(True)
        set_typography_role(intro, TypographyRole.SECONDARY)
        layout.addWidget(intro)
        self.contribution_table = QTableWidget(0, 4)
        self.contribution_table.setAccessibleName("フィールドリターン一覧")
        self.contribution_table.setToolTip(
            "受け取ったフィールドリターン貢献の一覧です。"
            "行を選ぶと下に詳細が表示されます。"
        )
        self.contribution_table.setHorizontalHeaderLabels(
            ("貢献", "検証", "ルーティング", "受信")
        )
        for _col, _tip in enumerate((
            "貢献の識別子（先頭のみ表示）",
            "アーティファクトの検証状態",
            "保存先プロジェクトへの振分状態",
            "受信日時（UTC）",
        )):
            self.contribution_table.horizontalHeaderItem(_col).setToolTip(_tip)
        self.contribution_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.contribution_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.contribution_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.contribution_table.itemSelectionChanged.connect(
            self._sync_contribution_detail
        )
        layout.addWidget(self.contribution_table, 1)
        self.contribution_detail = QLabel(
            "一覧から項目を選択すると詳細を表示します。"
        )
        self.contribution_detail.setWordWrap(True)
        self.contribution_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        set_typography_role(
            self.contribution_detail, TypographyRole.SECONDARY
        )
        layout.addWidget(self.contribution_detail)
        return panel

    def _refresh_contributions(self) -> None:
        if self._list_contributions is None:
            return
        self._contributions = self._list_contributions()
        self.contribution_table.setRowCount(0)
        for contribution in self._contributions:
            row = self.contribution_table.rowCount()
            self.contribution_table.insertRow(row)
            for column, value in enumerate(
                (
                    f"{(contribution.contribution_id or '—')[:12]}…",
                    self._CONTRIB_VALIDATION_LABELS.get(
                        contribution.validation_state,
                        contribution.validation_state,
                    ),
                    self._CONTRIB_ROUTING_LABELS.get(
                        contribution.routing, contribution.routing
                    ),
                    contribution.recorded_at_utc or "",
                )
            ):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(
                        Qt.ItemDataRole.UserRole, contribution.contribution_id
                    )
                self.contribution_table.setItem(row, column, cell)
        self._sync_contribution_detail()

    def _sync_contribution_detail(self) -> None:
        items = self.contribution_table.selectedItems()
        index = None
        for item in items:
            if item.column() == 0:
                index = item.row()
                break
        if index is None or index >= len(self._contributions):
            if self.contribution_table.rowCount() == 0:
                self.contribution_detail.setText(
                    "フィールドリターンはまだ届いていません。"
                )
            else:
                self.contribution_detail.setText(
                    "一覧から項目を選択すると詳細を表示します。"
                )
            return
        contribution = self._contributions[index]
        lines = [
            f"貢献: {contribution.contribution_id}",
            f"検証: "
            + self._CONTRIB_VALIDATION_LABELS.get(
                contribution.validation_state, contribution.validation_state
            ),
            "ルーティング: "
            + self._CONTRIB_ROUTING_LABELS.get(
                contribution.routing, contribution.routing
            )
            + (
                f"（{contribution.matched_project_id}）"
                if contribution.matched_project_id
                else ""
            ),
        ]
        if contribution.mission_id:
            lines.append(f"ミッション: {contribution.mission_id}")
        if contribution.plan_sha256:
            lines.append(f"計画: {contribution.plan_sha256[:16]}…")
        lines.append(f"アーティファクト: {contribution.artifact_sha256[:16]}…")
        if contribution.detail:
            lines.append(f"詳細: {contribution.detail}")
        self.contribution_detail.setText("\n".join(lines))


_OPERATION_STATE_LABELS = {
    "queued": "待機中",
    "preflighting": "準備中",
    "running": "実行中",
    "cancellation_requested": "キャンセル要求中",
    "cancelled": "キャンセル済み",
    "completed": "完了",
    "failed": "失敗",
    "completed_for_historical_input": "完了（旧入力）",
    "result_stale": "結果が古い",
}


class ActivityPage(QWidget):
    """Activity: app operations, the projected project timeline, revisions.

    Sections (all read-mostly):

    * ``operations`` — live/recent :class:`ApplicationOperation` rows from the
      application-scoped ActivityCenter (data-management jobs, etc.),
    * ``timeline`` — human-readable project events projected by
      :class:`CadProjectActivityService` (variants, captures, measurements,
      checkpoints, notes); a row's nav URI deep link opens on double-click,
    * ``revisions`` — the raw persisted scene-revision ledger (latest first).
    """

    def __init__(
        self,
        list_revisions: Callable[[int], tuple],
        list_operations: Callable[[], tuple] | None = None,
        list_events: Callable[[int], tuple] | None = None,
        open_link: Callable[[str], bool] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_revisions = list_revisions
        self._list_operations = list_operations
        self._list_events = list_events
        self._open_link = open_link
        layout = _page_layout(
            self,
            "アクティビティ",
            "実行中の操作・プロジェクトの記録（最新順）です。"
            "タイムラインの行をダブルクリックすると、"
            "その出来事が起きた画面へ移動します。",
        )
        if self._list_operations is not None:
            operations_heading = QLabel("操作")
            set_typography_role(
                operations_heading, TypographyRole.SECTION_TITLE
            )
            layout.addWidget(operations_heading)
            self.operations_table = QTableWidget(0, 3)
            self.operations_table.setToolTip(
                "実行中・実行済みの操作（バックアップ・復元など）の一覧です。"
            )
            self.operations_table.setHorizontalHeaderLabels(
                ("状態", "操作", "更新時刻")
            )
            for _col, _tip in enumerate((
                "操作の進行状態（実行中・完了・失敗など）",
                "行われた操作の種類（バックアップ・復元・インポートなど）",
                "状態が最後に更新された時刻",
            )):
                self.operations_table.horizontalHeaderItem(_col).setToolTip(_tip)
            self.operations_table.horizontalHeader().setSectionResizeMode(
                1, QHeaderView.ResizeMode.Stretch
            )
            self.operations_table.horizontalHeader().setSectionResizeMode(
                2, QHeaderView.ResizeMode.ResizeToContents
            )
            self.operations_table.setEditTriggers(
                QTableWidget.EditTrigger.NoEditTriggers
            )
            layout.addWidget(self.operations_table, 1)
        else:
            self.operations_table = None
        if self._list_events is not None:
            timeline_heading = QLabel("プロジェクトタイムライン")
            set_typography_role(
                timeline_heading, TypographyRole.SECTION_TITLE
            )
            layout.addWidget(timeline_heading)
            self.events_table = QTableWidget(0, 3)
            self.events_table.setToolTip(
                "プロジェクトで起きた出来事の記録です。行をダブルクリックすると該当画面へ移動できます。"
            )
            self.events_table.setHorizontalHeaderLabels(
                ("時刻", "内容", "詳細")
            )
            for _col, _tip in enumerate((
                "記録された時刻（新しい順）",
                "プロジェクトで起きた出来事の概要",
                "対象の詳細（ダブルクリックで該当画面へ移動できます）",
            )):
                self.events_table.horizontalHeaderItem(_col).setToolTip(_tip)
            self.events_table.horizontalHeader().setSectionResizeMode(
                1, QHeaderView.ResizeMode.Stretch
            )
            self.events_table.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.ResizeToContents
            )
            self.events_table.setEditTriggers(
                QTableWidget.EditTrigger.NoEditTriggers
            )
            self.events_table.itemActivated.connect(self._activate_event)
            self.events_table.itemDoubleClicked.connect(self._activate_event)
            layout.addWidget(self.events_table, 1)
        else:
            self.events_table = None
        revisions_heading = QLabel("リビジョン履歴")
        set_typography_role(revisions_heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(revisions_heading)
        self.table = QTableWidget(0, 3)
        self.table.setToolTip(
            "保存された版（リビジョン）の履歴一覧です。"
        )
        self.table.setHorizontalHeaderLabels(("時刻", "プロジェクト", "リビジョン"))
        for _col, _tip in enumerate((
            "版（リビジョン）が保存された時刻",
            "対象のプロジェクト名",
            "保存された版の識別子（履歴・差分比較で使われます）",
        )):
            self.table.horizontalHeaderItem(_col).setToolTip(_tip)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)
        self.empty_label = QLabel(
            "まだ記録はありません。保存や昇格を行うとここに表示されます。"
        )
        set_typography_role(self.empty_label, TypographyRole.SECONDARY)
        self.empty_label.setWordWrap(True)
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)
        self.refresh()

    def _activate_event(self, item: QTableWidgetItem) -> None:
        anchor = self.events_table.item(item.row(), 0)
        link = anchor.data(Qt.ItemDataRole.UserRole) if anchor is not None else None
        if link and self._open_link is not None:
            self._open_link(str(link))

    def refresh(self) -> None:
        if self.operations_table is not None and self._list_operations is not None:
            self.operations_table.setRowCount(0)
            for operation in self._list_operations():
                row = self.operations_table.rowCount()
                self.operations_table.insertRow(row)
                state = getattr(operation.state, "value", operation.state)
                detail = (
                    operation.error_summary
                    or operation.result_summary
                    or operation.operation_kind
                )
                for column, value in enumerate(
                    (
                        _OPERATION_STATE_LABELS.get(state, str(state)),
                        f"{operation.title} — {detail}",
                        operation.updated_at,
                    )
                ):
                    cell = QTableWidgetItem(str(value))
                    if column == 0:
                        cell.setData(
                            Qt.ItemDataRole.UserRole, operation.operation_id
                        )
                    self.operations_table.setItem(row, column, cell)
        if self.events_table is not None and self._list_events is not None:
            self.events_table.setRowCount(0)
            for event in self._list_events(50):
                row = self.events_table.rowCount()
                self.events_table.insertRow(row)
                for column, value in enumerate(
                    (event.occurred_at_utc, event.title, event.detail or "")
                ):
                    cell = QTableWidgetItem(str(value))
                    if column == 0 and event.deep_link is not None:
                        cell.setData(Qt.ItemDataRole.UserRole, event.deep_link)
                    self.events_table.setItem(row, column, cell)
        self.table.setRowCount(0)
        rows = self._list_revisions(50)
        self.empty_label.setVisible(not rows)
        for created_at, document_id, revision_id in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate((created_at, document_id, revision_id)):
                cell = QTableWidgetItem(str(value))
                if column == 2:
                    cell.setData(Qt.ItemDataRole.UserRole, revision_id)
                self.table.setItem(row, column, cell)


def list_recent_revisions(repository: SceneRepository, limit: int = 50) -> tuple:
    """(created_at_utc, document_id, revision_id) rows — read-only."""

    path = Path(repository.path)
    if not path.is_file():
        return ()
    try:
        with closing(repository._read()) as connection, connection:
            rows = connection.execute(
                """
                SELECT created_at_utc, document_id, revision_id
                FROM scene_revisions WHERE detached = 0 ORDER BY seq DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        return ()
    return tuple((str(a), str(b), str(c)) for a, b, c in rows)


_LIBRARY_FAMILY_TITLES = {
    "equipment": "機材・スピーカー定義",
    "treatment": "吸音・処理材",
    "target_curve": "目標カーブ",
    "standard_profile": "基準プロファイル",
    "instrument": "測定機器",
    "material": "音響材料",
    "operating_profile": "動作プロファイル",
}

_LIBRARY_SCOPE_LABELS = {
    "builtin": "同梱",
    "user_library": "ユーザーライブラリ",
    "project_local": "プロジェクト",
    "imported_dependency": "依存として取り込み",
    "historical": "履歴",
}


class ReferenceLibraryPage(QWidget):
    """Reference library: equipment/source definitions shared across projects.

    ``library_index`` (the #630 hub read model) renders one additional
    read-only section per registered authority family — speakers, materials,
    standards profiles — so shared authorities are discoverable in one place.
    """

    manage_requested = Signal()

    def __init__(
        self,
        list_definitions: Callable[[], tuple],
        library_index=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_definitions = list_definitions
        self._library_index = library_index
        layout = _page_layout(
            self,
            "ライブラリ",
            "機材・ソース定義のライブラリです（プロジェクト共通）。",
        )
        self.table = QTableWidget(0, 3)
        self.table.setToolTip(
            "登録済みの機材・ソース定義の一覧です。列の見出しにカーソルを合わせると各列の説明が表示されます。"
        )
        self.table.setHorizontalHeaderLabels(("メーカー", "モデル", "バージョン"))
        for _col, _tip in enumerate((
            "機材の製造メーカー名",
            "機材のモデル・型番名",
            "登録されている定義のバージョン",
        )):
            self.table.horizontalHeaderItem(_col).setToolTip(_tip)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        self.empty_label = QLabel(
            "まだ定義はありません。"
            "「機材ライブラリを管理…」で機材や素材を登録できます。",
            self,
        )
        set_typography_role(self.empty_label, TypographyRole.SECONDARY)
        self.empty_label.setWordWrap(True)
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)
        manage = QPushButton("機材ライブラリを管理…")
        manage.setToolTip("機材・素材・ソース定義の登録・編集を行う管理画面を開きます")
        manage.setWhatsThis("機材・素材・ソース定義の登録・編集を行う管理画面を開きます")
        manage.clicked.connect(lambda: self.manage_requested.emit())
        layout.addWidget(manage)

        self._family_frames: dict[str, tuple[QLabel, QTableWidget]] = {}
        if library_index is not None:
            for family in library_index.families():
                header = QLabel(
                    _LIBRARY_FAMILY_TITLES.get(str(family), str(family)),
                    self,
                )
                set_typography_role(header, TypographyRole.SECTION_TITLE)
                table = QTableWidget(0, 4, self)
                table.setToolTip(
                    "この区分で登録されている項目の一覧です。列の見出しにカーソルを合わせると各列の説明が表示されます。"
                )
                table.setHorizontalHeaderLabels(
                    ("名前", "区分", "スコープ", "バージョン")
                )
                for _col, _tip in enumerate((
                    "登録されている項目の名前",
                    "項目の種類・区分",
                    "この項目が有効な範囲（プロジェクト共通など）",
                    "登録されている定義のバージョン",
                )):
                    table.horizontalHeaderItem(_col).setToolTip(_tip)
                table.horizontalHeader().setSectionResizeMode(
                    0, QHeaderView.ResizeMode.Stretch
                )
                table.setEditTriggers(
                    QTableWidget.EditTrigger.NoEditTriggers
                )
                table.setSelectionBehavior(
                    QTableWidget.SelectionBehavior.SelectRows
                )
                header.hide()
                table.hide()
                layout.addWidget(header)
                layout.addWidget(table)
                self._family_frames[str(family)] = (header, table)
        self.refresh()

    def refresh(self) -> None:
        self.table.setRowCount(0)
        for definition in self._list_definitions():
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate(
                (
                    getattr(definition, "manufacturer", "") or "",
                    getattr(definition, "model", "")
                    or getattr(definition, "user_label", "")
                    or getattr(definition, "definition_id", ""),
                    str(getattr(definition, "version", "")),
                )
            ):
                item = QTableWidgetItem(str(value))
                if column == 1:
                    item.setData(
                        Qt.ItemDataRole.UserRole,
                        getattr(definition, "definition_id", ""),
                    )
                self.table.setItem(row, column, item)
        self.empty_label.setVisible(self.table.rowCount() == 0)
        self._refresh_family_sections()

    def _refresh_family_sections(self) -> None:
        if self._library_index is None:
            return
        for family, (header, table) in self._family_frames.items():
            try:
                entries = self._library_index.entries(family=family)
            except Exception:  # noqa: BLE001 - a broken provider must not blank the page
                entries = ()
            header.setVisible(bool(entries))
            table.setVisible(bool(entries))
            table.setRowCount(0)
            for entry in entries:
                row = table.rowCount()
                table.insertRow(row)
                for column, value in enumerate(
                    (
                        entry.display_name,
                        entry.capability_summary
                        or entry.source_summary
                        or "",
                        _LIBRARY_SCOPE_LABELS.get(
                            str(entry.scope), str(entry.scope)
                        ),
                        entry.version,
                    )
                ):
                    cell = QTableWidgetItem(str(value))
                    if column == 0:
                        cell.setData(
                            Qt.ItemDataRole.UserRole, entry.semantic_key
                        )
                    table.setItem(row, column, cell)

    def focus_definition(self, definition_id: str) -> TargetFocusResult:
        for row in range(self.table.rowCount()):
            model = self.table.item(row, 1)
            if (
                model is not None
                and model.data(Qt.ItemDataRole.UserRole) == definition_id
            ):
                self.table.selectRow(row)
                return TargetFocusResult(focused=True)
        return TargetFocusResult(
            focused=False,
            message="ライブラリ内に該当の定義が見つかりません",
        )


class SupportPage(QWidget):
    """Support: diagnostics locations, version, and package export (#604)."""

    def __init__(
        self,
        data_dir: Path,
        status_provider: Callable[[], tuple[str, ...]] | None = None,
        export_diagnostics: Callable[[QWidget], str | None] | None = None,
        open_authority_graph: Callable[[QWidget], None] | None = None,
        open_solver_diagnostics: Callable[[QWidget], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._status_provider = status_provider
        self._export_diagnostics = export_diagnostics
        self._open_authority_graph = open_authority_graph
        self._open_solver_diagnostics = open_solver_diagnostics
        layout = _page_layout(
            self,
            "サポート",
            "問題が発生したときの確認情報です。",
        )
        for label_text in (
            f"バージョン: {version_string()}",
            f"データフォルダー: {data_dir}",
            f"診断ログ: {diagnostics_dir(data_dir)}",
        ):
            label = QLabel(label_text)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            label.setWordWrap(True)
            layout.addWidget(label)
        self._status_layout = layout
        self._status_labels: list[QLabel] = []
        note = QLabel(
            "起動に失敗した場合は診断ログをサポートに共有してください。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        if self._open_authority_graph is not None:
            self.authority_button = QPushButton("権威グラフを開く", self)
            self.authority_button.setToolTip("データの由来（どの定義・設定から生成されたか）を辿れるグラフ画面を開きます")
            self.authority_button.setWhatsThis("データの由来（どの定義・設定から生成されたか）を辿れるグラフ画面を開きます")
            self.authority_button.setObjectName("supportOpenAuthorityGraph")
            self.authority_button.clicked.connect(
                lambda: self._open_authority_graph(self)
            )
            layout.addWidget(self.authority_button)
        else:
            self.authority_button = None
        if self._open_solver_diagnostics is not None:
            self.solver_button = QPushButton("ソルバー出力の診断", self)
            self.solver_button.setToolTip("音響ソルバーが出力した計算結果の内部診断情報を確認します")
            self.solver_button.setWhatsThis("音響ソルバーが出力した計算結果の内部診断情報を確認します")
            self.solver_button.setObjectName("supportOpenSolverDiagnostics")
            self.solver_button.clicked.connect(
                lambda: self._open_solver_diagnostics(self)
            )
            layout.addWidget(self.solver_button)
        else:
            self.solver_button = None
        if self._export_diagnostics is not None:
            self.export_button = QPushButton("診断パッケージをエクスポート", self)
            self.export_button.setToolTip("サポート共有用の診断情報（ログ・設定の概要など）を1つのファイルにまとめて書き出します")
            self.export_button.setWhatsThis("サポート共有用の診断情報（ログ・設定の概要など）を1つのファイルにまとめて書き出します")
            self.export_button.setObjectName("supportExportDiagnostics")
            self.export_button.clicked.connect(self._run_export)
            layout.addWidget(self.export_button)
            self.export_status = QLabel(self)
            self.export_status.setObjectName("supportExportStatus")
            self.export_status.setWordWrap(True)
            layout.addWidget(self.export_status)
        else:
            self.export_button = None
            self.export_status = None
        layout.addStretch(1)
        self.refresh()

    def _run_export(self) -> None:
        try:
            path = self._export_diagnostics(self)
        except Exception as exc:
            self.export_status.setText(
                "診断パッケージを作成できませんでした: "
                f"{operation_error_message(exc)}"
            )
            return
        if path is not None:
            self.export_status.setText(f"保存しました: {path}")

    def refresh(self) -> None:
        """Re-render live status lines (e.g. effective receiver state)."""
        for label in self._status_labels:
            self._status_layout.removeWidget(label)
            label.deleteLater()
        self._status_labels.clear()
        if self._status_provider is None:
            return
        for text in self._status_provider():
            label = QLabel(text)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            label.setWordWrap(True)
            self._status_layout.insertWidget(
                self._status_layout.count() - 2, label
            )
            self._status_labels.append(label)


def projects_focus(
    page: ProjectLibraryPage, target: NavigationTarget
) -> TargetFocusResult:
    project_id = target.primary_id
    if project_id is None:
        return TargetFocusResult(focused=True)
    for row in range(page.table.rowCount()):
        cell = page.table.item(row, 0)
        if cell is not None and cell.data(Qt.ItemDataRole.UserRole) == project_id:
            page.table.selectRow(row)
            return TargetFocusResult(focused=True)
    return TargetFocusResult(
        focused=False,
        message="プロジェクト一覧に該当の項目がありません",
    )


def inbox_focus(page: CaptureInboxPage, target: NavigationTarget) -> TargetFocusResult:
    item_id = target.primary_id
    if item_id is None:
        return TargetFocusResult(focused=True)
    for row in range(page.table.rowCount()):
        cell = page.table.item(row, 0)
        if cell is not None and cell.data(Qt.ItemDataRole.UserRole) == item_id:
            page.table.selectRow(row)
            return TargetFocusResult(focused=True)
    return TargetFocusResult(
        focused=False,
        message="取り込み一覧に該当の項目がありません",
    )


def _event_row_matches(cell: QTableWidgetItem, target: NavigationTarget) -> bool:
    """A timeline row matches when its stored nav URI targets the same id."""

    link = cell.data(Qt.ItemDataRole.UserRole)
    if not isinstance(link, str):
        return False
    try:
        linked = navigation_target_from_uri(link)
    except ValueError:
        return False
    if linked.kind is not target.kind:
        return False
    return bool(set(linked.object_ids) & set(target.object_ids))


def activity_focus(page: ActivityPage, target: NavigationTarget) -> TargetFocusResult:
    if target.primary_id is None:
        return TargetFocusResult(focused=True)
    if page.events_table is not None:
        for row in range(page.events_table.rowCount()):
            cell = page.events_table.item(row, 0)
            if cell is not None and _event_row_matches(cell, target):
                page.events_table.selectRow(row)
                page.events_table.scrollToItem(cell)
                return TargetFocusResult(focused=True)
    if page.operations_table is not None:
        for row in range(page.operations_table.rowCount()):
            cell = page.operations_table.item(row, 0)
            if (
                cell is not None
                and cell.data(Qt.ItemDataRole.UserRole) in target.object_ids
            ):
                page.operations_table.selectRow(row)
                page.operations_table.scrollToItem(cell)
                return TargetFocusResult(focused=True)
    for row in range(page.table.rowCount()):
        cell = page.table.item(row, 2)
        if (
            cell is not None
            and cell.data(Qt.ItemDataRole.UserRole) in target.object_ids
        ):
            page.table.selectRow(row)
            page.table.scrollToItem(cell)
            return TargetFocusResult(focused=True)
    return TargetFocusResult(
        focused=False,
        message="アクティビティ一覧に該当の記録がありません",
    )


__all__ = [
    "ActivityPage",
    "CaptureInboxPage",
    "ProjectEntry",
    "ProjectLibraryPage",
    "ProjectLibraryService",
    "ReferenceLibraryPage",
    "SupportPage",
    "activity_focus",
    "inbox_focus",
    "projects_focus",
    "list_recent_revisions",
]
