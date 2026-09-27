"""Settings > 保持管理 — capture retention surface (#620).

A thin presentation layer over :class:`CaptureRetentionService`: it lists the
persisted capture revisions, runs the dry-run ``plan_capture_revision_purge``
on demand, and only then offers the actual purge behind an explicit
confirmation. All deletion semantics (reference safety, transaction, blob GC)
stay in ``capture_retention``; this widget never touches SQLite itself.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .capture_retention import (
    CapturePurgePlan,
    CaptureRetentionError,
    CaptureRetentionService,
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


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0 or unit == "GiB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size):,} B"
        size /= 1024.0
    return f"{value:,} B"


def _plan_status_label(plan: CapturePurgePlan) -> str:
    return {
        "ready": "削除可能",
        "blocked": "依存関係があり削除できません",
        "absent": "このリビジョンは存在しません",
    }[plan.status]


def _plan_summary_lines(plan: CapturePurgePlan) -> list[str]:
    lines = [
        f"結果: {_plan_status_label(plan)}",
        "削除対象: "
        f"エビデンス {len(plan.deletable_source_evidence_ids)} 件 / "
        f"メッシュバインディング {len(plan.deletable_mesh_binding_ids)} 件 / "
        f"権威レコード {len(plan.deletable_authority_record_ids)} 件 / "
        f"座標権威 {len(plan.deletable_coordinate_authority_ids)} 件 / "
        f"ルームプラン {plan.roomplan_record_count} 件",
        "共有のため保持: "
        f"エビデンス {len(plan.retained_source_evidence_ids)} 件 / "
        f"バインディング {len(plan.retained_mesh_binding_ids)} 件 / "
        f"権威 {len(plan.retained_authority_record_ids)} 件 / "
        f"座標 {len(plan.retained_coordinate_authority_ids)} 件",
        f"回収可能: {_format_bytes(plan.reclaimable_bytes)}"
        f"（コンテンツ blob {len(plan.reclaimed_blob_sha256)} 件）",
    ]
    if plan.blocking_dependents:
        lines.append("削除を妨げている参照:")
        for dependent in plan.blocking_dependents[:8]:
            lines.append(
                f"・{dependent.kind} {dependent.identifier}"
            )
        remaining = len(plan.blocking_dependents) - 8
        if remaining > 0:
            lines.append(f"…ほか {remaining} 件")
    return lines


class RetentionPolicyWidget(QWidget):
    """Retention picker + dry-run + confirmed purge for capture revisions.

    ``is_busy`` (optional) lets the host disable purges while a backup,
    restore, or relocation holds the data lock.
    """

    def __init__(
        self,
        service: CaptureRetentionService,
        *,
        is_busy: Callable[[], bool] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("captureRetentionWidget")
        set_surface_role(self, SurfaceRole.BASE)
        self._service = service
        self._is_busy = is_busy or (lambda: False)
        # The purge button arms only for the plan computed against the
        # currently-selected revision, so a stale 'ready' can never leak
        # into a different selection.
        self._planned_revision_id: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 32)
        layout.setSpacing(16)

        title = QLabel("キャプチャデータの保持管理", self)
        set_typography_role(title, TypographyRole.WORKSPACE_TITLE)
        layout.addWidget(title)

        intro = QLabel(
            "取り込んだキャプチャリビジョンごとに、HTDT内データの削除内容を"
            "事前確認（ドライラン）してから削除できます。元の "
            ".htdtcapture バンドルは外部ファイルのため対象外です。",
            self,
        )
        intro.setWordWrap(True)
        set_typography_role(intro, TypographyRole.BODY)
        layout.addWidget(intro)

        card = QFrame(self)
        card.setObjectName("captureRetentionCard")
        set_surface_role(card, SurfaceRole.RAISED)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(12)

        self.inventory_label = QLabel(card)
        self.inventory_label.setWordWrap(True)
        set_typography_role(self.inventory_label, TypographyRole.SECONDARY)
        card_layout.addWidget(self.inventory_label)

        picker_row = QHBoxLayout()
        picker_row.setSpacing(10)
        self.revision_combo = QComboBox(card)
        self.revision_combo.setObjectName("captureRetentionRevisionCombo")
        self.revision_combo.currentIndexChanged.connect(
            self._on_selection_changed
        )
        picker_row.addWidget(self.revision_combo, 1)
        self.plan_button = QPushButton("削除内容を確認", card)
        self.plan_button.setObjectName("captureRetentionPlanButton")
        set_control_size(self.plan_button, ControlSize.STANDARD)
        self.plan_button.clicked.connect(self._run_dry_run)
        picker_row.addWidget(self.plan_button)
        self.purge_button = QPushButton("削除を実行", card)
        self.purge_button.setObjectName("captureRetentionPurgeButton")
        set_control_size(self.purge_button, ControlSize.STANDARD)
        self.purge_button.clicked.connect(self._confirm_and_purge)
        picker_row.addWidget(self.purge_button)
        card_layout.addLayout(picker_row)

        self.plan_label = QLabel(card)
        self.plan_label.setObjectName("captureRetentionPlanLabel")
        self.plan_label.setWordWrap(True)
        set_typography_role(self.plan_label, TypographyRole.BODY)
        self.plan_label.hide()
        card_layout.addWidget(self.plan_label)

        layout.addWidget(card)
        layout.addStretch(1)

        self.refresh()

    def refresh(self) -> None:
        """Reload inventory counts and the revision list."""
        try:
            inventory = self._service.inventory()
            revisions = self._service.list_capture_revisions()
        except Exception:  # noqa: BLE001 - UI must degrade, not crash
            self.inventory_label.setText(
                "キャプチャ情報を読み込めませんでした。"
            )
            revisions = ()
        else:
            self.inventory_label.setText(
                f"キャプチャ: リビジョン {inventory.capture_revision_count} 件 / "
                f"取り込み {inventory.ingestion_run_count} 回 / "
                f"エビデンス {inventory.source_evidence_count} 件 "
                f"({_format_bytes(inventory.source_payload_bytes)}) / "
                f"コンテンツ blob {inventory.content_blob_count} 件 "
                f"({_format_bytes(inventory.content_blob_bytes)})"
            )

        selected = self._selected_revision_id()
        self.revision_combo.blockSignals(True)
        self.revision_combo.clear()
        for revision in revisions:
            label = (
                f"{revision.capture_series_id} · "
                f"{revision.capture_revision_id[:8]}… · "
                f"取り込み {revision.ingestion_run_count} 回 · "
                f"{revision.latest_recorded_at_utc}"
            )
            self.revision_combo.addItem(label, revision.capture_revision_id)
        if selected:
            index = self.revision_combo.findData(selected)
            if index >= 0:
                self.revision_combo.setCurrentIndex(index)
        self.revision_combo.blockSignals(False)
        self._disarm_plan()

    def _selected_revision_id(self) -> str | None:
        data = self.revision_combo.currentData()
        return str(data) if data else None

    def _on_selection_changed(self, _index: int) -> None:
        self._disarm_plan()

    def _disarm_plan(self) -> None:
        self._planned_revision_id = None
        self.plan_label.hide()
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        available = self.revision_combo.count() > 0 and not self._is_busy()
        self.plan_button.setEnabled(available)
        self.purge_button.setEnabled(
            available
            and self._planned_revision_id is not None
            and self._planned_revision_id == self._selected_revision_id()
        )

    def _run_dry_run(self) -> None:
        revision_id = self._selected_revision_id()
        if revision_id is None:
            return
        try:
            plan = self._service.plan_capture_revision_purge(revision_id)
        except Exception:  # noqa: BLE001
            self._show_error("削除内容の確認に失敗しました。")
            return
        self._planned_revision_id = (
            revision_id if plan.status == "ready" else None
        )
        self.plan_label.setText("\n".join(_plan_summary_lines(plan)))
        set_semantic_state(
            self.plan_label,
            SemanticState.SUCCESS
            if plan.status == "ready"
            else SemanticState.WARNING,
        )
        self.plan_label.show()
        self._refresh_actions()

    def _confirm_and_purge(self) -> None:
        revision_id = self._selected_revision_id()
        if (
            revision_id is None
            or self._is_busy()
            or revision_id != self._planned_revision_id
        ):
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("キャプチャデータの削除")
        box.setText(
            "このキャプチャリビジョンのHTDT内データを削除します。"
        )
        box.setInformativeText(
            "外部の .htdtcapture バンドルは削除されません。"
            "実行直前に参照可否を再検証し、"
            "削除できない場合は何も変更しません。実行しますか？"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            plan = self._service.purge_capture_revision(revision_id)
        except CaptureRetentionError:
            self.refresh()
            self._show_error(
                "参照関係があるため削除できませんでした。"
                "削除内容の確認で詳細を確認してください。"
            )
            return
        except Exception:  # noqa: BLE001
            self.refresh()
            self._show_error(
                "削除に失敗しました。データは変更されていません。"
            )
            return
        self.refresh()
        self.plan_label.setText(
            "削除しました: "
            f"エビデンス {len(plan.deletable_source_evidence_ids)} 件 / "
            f"{_format_bytes(plan.reclaimable_bytes)} を回収"
        )
        set_semantic_state(self.plan_label, SemanticState.SUCCESS)
        self.plan_label.show()

    def _show_error(self, message: str) -> None:
        self.plan_label.setText(message)
        set_semantic_state(self.plan_label, SemanticState.ERROR)
        self.plan_label.show()
        self._refresh_actions()


__all__ = ["RetentionPolicyWidget"]
