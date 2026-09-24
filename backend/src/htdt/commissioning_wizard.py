"""First-run project commissioning wizard (#588).

Turns an empty install into a usable digital twin: the user names a project,
declares intent, then a resumable plan drives room → system → measurement →
readiness stages. The wizard never creates measurement evidence; it persists
a CommissioningPlan (orchestration metadata) and deep-links to the real
workspaces.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .cad_scene import make_empty_scene
from .commissioning_plan import (
    CommissioningIntent,
    CommissioningPlan,
    CommissioningPlanRepository,
    CommissioningService,
    CommissioningStage,
    new_plan,
)
from .overview_readiness import OverviewReadinessService
from .ui_theme import TypographyRole, set_typography_role
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId


_STAGE_ORDER: tuple[CommissioningStage, ...] = (
    CommissioningStage.INTENT,
    CommissioningStage.ROOM,
    CommissioningStage.SYSTEM,
    CommissioningStage.MEASUREMENT,
    CommissioningStage.READINESS,
)


class CommissioningWizard(QDialog):
    """Resumable commissioning wizard; also the Overview resume entry point."""

    document_switch_requested = Signal(str)
    navigate_requested = Signal(object)  # WorkspaceDeepLink

    def __init__(
        self,
        repository,
        document_id: str,
        *,
        data_dir: Path | None = None,
        overview_service: OverviewReadinessService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.current_document_id = document_id
        self.created_document_id = document_id
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._overview_service = overview_service

        self.setWindowTitle('プロジェクト初期設定')
        self.setModal(True)
        self.resize(640, 520)

        layout = QVBoxLayout(self)
        self.title = QLabel()
        set_typography_role(self.title, TypographyRole.WORKSPACE_TITLE)
        layout.addWidget(self.title)

        self.page_stack = QVBoxLayout()
        layout.addLayout(self.page_stack)
        layout.addStretch(1)

        nav = QHBoxLayout()
        nav.addStretch(1)
        self.back_button = QPushButton('戻る')
        self.next_button = QPushButton('次へ')
        self.back_button.clicked.connect(lambda: self._show_page(self._page_index - 1))
        self.next_button.clicked.connect(self._advance)
        nav.addWidget(self.back_button)
        nav.addWidget(self.next_button)
        layout.addLayout(nav)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存して閉じる')
        buttons.accepted.connect(self._save_and_close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._pages: list[QWidget] = []
        self._page_index = 0
        self._build_pages()
        self._show_page(0)

    # -- pages -----------------------------------------------------------

    def _add_page(self, widget: QWidget, title: str) -> None:
        widget.hide()
        self.page_stack.addWidget(widget)
        self._pages.append(widget)
        widget.setObjectName(f"commissioning:{title}")

    def _build_pages(self) -> None:
        # 1. Intent — name + what the user is commissioning.
        intent = QWidget(self)
        form = QFormLayout(intent)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText('例: living-theater')
        form.addRow('プロジェクト名', self.name_edit)
        self.new_radio = QRadioButton('新しいプロジェクトを作成')
        self.new_radio.setChecked(True)
        self.existing_radio = QRadioButton('既存プロジェクトを設定')
        self.existing_combo = QComboBox()
        self.existing_combo.addItem(self.current_document_id, self.current_document_id)
        for document_id in self._other_documents():
            self.existing_combo.addItem(document_id, document_id)
        self.existing_combo.setEnabled(False)
        self.existing_radio.toggled.connect(self.existing_combo.setEnabled)
        form.addRow(self.new_radio)
        form.addRow(self.existing_radio)
        form.addRow('対象プロジェクト', self.existing_combo)
        self.has_room = QCheckBox('部屋データが既にある（スキップ検討可能）')
        self.audio_only = QCheckBox('オーディオのみ（映像システムなし）')
        self.rew_available = QCheckBox('REW などの測定データを利用する')
        self.hybrid = QCheckBox('ハイブリッド予測を有効にする')
        self.goals_label = QLabel('目標')
        self.goals = QListWidget()
        for goal in ('迫力', '定位', '広帯域再生', '低音の均一性'):
            item = QListWidgetItem(goal, self.goals)
            item.setCheckState(Qt.CheckState.Unchecked)
        self.speaker_count = QSpinBox()
        self.speaker_count.setRange(1, 64)
        self.speaker_count.setValue(5)
        for widget in (self.has_room, self.audio_only, self.rew_available, self.hybrid):
            form.addRow(widget)
        form.addRow(self.goals_label, self.goals)
        form.addRow('スピーカー台数（目安）', self.speaker_count)
        self._add_page(intent, 'intent')

        # 2-4. Guidance pages linking to the real workspaces.
        for stage, body in (
            (
                CommissioningStage.ROOM,
                '部屋ワークスペースで部屋形状を決定します。',
            ),
            (
                CommissioningStage.SYSTEM,
                '部屋ワークスペースでスピーカーとリスニングポイントを配置します。',
            ),
            (
                CommissioningStage.MEASUREMENT,
                '測定ワークスペースで実測データを登録します（後でも可）。',
            ),
        ):
            page = QWidget(self)
            page_layout = QVBoxLayout(page)
            label = QLabel(body, page)
            label.setWordWrap(True)
            page_layout.addWidget(label)
            skip = QCheckBox('このステップは後で行う（スキップ）', page)
            page_layout.addWidget(skip)
            page_layout.addStretch(1)
            setattr(self, f'_skip_{stage.value}', skip)
            self._add_page(page, stage.value)

        # 5. Readiness summary — derived live, deep links out.
        summary_page = QWidget(self)
        summary_layout = QVBoxLayout(summary_page)
        self.summary_list = QVBoxLayout()
        summary_layout.addLayout(self.summary_list)
        summary_layout.addStretch(1)
        self._add_page(summary_page, 'readiness')

    def _other_documents(self) -> tuple[str, ...]:
        try:
            rows = self.repository._connect().execute(
                'SELECT document_id FROM scene_document_heads'
            ).fetchall()
            return tuple(
                row[0] for row in rows if row[0] != self.current_document_id
            )
        except Exception:
            return ()

    def _show_page(self, index: int) -> None:
        for page in self._pages:
            page.hide()
        self._page_index = max(0, min(index, len(self._pages) - 1))
        page = self._pages[self._page_index]
        page.show()
        titles = ('プロジェクト', '部屋', 'システム', '測定', '準備状況')
        self.title.setText(f'初期設定 — {titles[self._page_index]}')
        self.back_button.setEnabled(self._page_index > 0)
        self.next_button.setEnabled(self._page_index < len(self._pages) - 1)
        if self._page_index == len(self._pages) - 1:
            self._rebuild_summary()

    def _advance(self) -> None:
        self._show_page(self._page_index + 1)

    # -- persistence ----------------------------------------------------

    def _collect_intent(self) -> CommissioningIntent:
        goals = tuple(
            self.goals.item(i).text()
            for i in range(self.goals.count())
            if self.goals.item(i).checkState() == Qt.CheckState.Checked
        )
        return CommissioningIntent(
            is_new_project=self.new_radio.isChecked(),
            has_existing_room=self.has_room.isChecked(),
            audio_only=self.audio_only.isChecked(),
            rew_available=self.rew_available.isChecked(),
            wants_hybrid_prediction=self.hybrid.isChecked(),
            planned_speaker_count=self.speaker_count.value(),
            goals=goals,
        )

    def _resolve_document_id(self, *, create_document: bool) -> str | None:
        if not self.new_radio.isChecked():
            return self.existing_combo.currentData()
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, 'プロジェクト名', 'プロジェクト名を入力してください。')
            self._show_page(0)
            return None
        if create_document and self.repository.latest(name) is None:
            self.repository.save(make_empty_scene(name), parent_revision_id=None)
        return name

    def _skipped(self) -> frozenset[str]:
        skipped: list[str] = []
        for requirement_id, stage in (
            ('room.geometry', CommissioningStage.ROOM),
            ('system.speakers', CommissioningStage.SYSTEM),
            ('system.listening', CommissioningStage.SYSTEM),
            ('measurement.data', CommissioningStage.MEASUREMENT),
        ):
            box = getattr(self, f'_skip_{stage.value}', None)
            if box is not None and box.isChecked():
                skipped.append(requirement_id)
        return frozenset(skipped)

    def build_plan(self, *, create_document: bool = True) -> CommissioningPlan | None:
        document_id = self._resolve_document_id(create_document=create_document)
        if document_id is None:
            return None
        plan = new_plan(
            document_id,
            name=self.name_edit.text().strip() or document_id,
            intent=self._collect_intent(),
        )
        plan = dataclasses.replace(
            plan, skipped_requirements=self._skipped()
        )
        self.created_document_id = document_id
        return plan

    def _save_and_close(self) -> None:
        plan = self.build_plan()
        if plan is None:
            return
        if self._data_dir is not None:
            CommissioningPlanRepository(self._data_dir).save(plan)
        if self._page_index == len(self._pages) - 1:
            plan = dataclasses.replace(plan, finished=True)
            if self._data_dir is not None:
                CommissioningPlanRepository(self._data_dir).save(plan)
        self.accept()

    def _rebuild_summary(self) -> None:
        while self.summary_list.count():
            item = self.summary_list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                sub = item.layout()
                while sub.count():
                    sub_item = sub.takeAt(0)
                    if sub_item.widget() is not None:
                        sub_item.widget().deleteLater()
        if self._overview_service is None:
            return
        # Preview only: the project document is created on save, not when the
        # summary page is reached — cancelling must not leave a scene behind.
        plan = self.build_plan(create_document=False)
        if plan is None:
            return
        service = CommissioningService(self.repository, self._overview_service)
        for requirement in service.requirements(plan):
            row = QHBoxLayout()
            state = {'satisfied': 'OK', 'pending': '未完了', 'skipped': 'スキップ'}
            label = QLabel(f"{state[requirement.status]}  {requirement.title} — {requirement.reason}")
            label.setWordWrap(True)
            row.addWidget(label, 1)
            if requirement.link is not None:
                button = QPushButton('開く')
                button.clicked.connect(
                    lambda checked=False, link=requirement.link: self.navigate_requested.emit(link)
                )
                row.addWidget(button)
            container = QWidget()
            container.setLayout(row)
            self.summary_list.addWidget(container)


__all__ = ['CommissioningWizard']
