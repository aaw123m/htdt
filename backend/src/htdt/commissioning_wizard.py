"""First-run project commissioning wizard (#588).

Turns an empty install into a usable digital twin: the user names a project,
declares intent, then a resumable plan drives room → system → measurement →
readiness stages. The wizard never creates measurement evidence; it persists
a CommissioningPlan (orchestration metadata) and deep-links to the real
workspaces.

#898: completing the wizard materializes the collected intent into the
canonical ProjectDesignBrief (via the setup-intent boundary), and the
start-method picker can seed the project from a built-in or user
ProjectTemplate instead of an empty scene.
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

from .cad_project_template import ProjectTemplate
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
        brief_repository=None,
        template_options: tuple[tuple[str, ProjectTemplate], ...] = (),
        template_starter=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.current_document_id = document_id
        self.created_document_id = document_id
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._overview_service = overview_service
        # Setup-intent convergence (#898): when supplied, saving the wizard
        # materializes the declared intent into the canonical
        # ProjectDesignBrief via CadDesignBriefRepository.
        self._brief_repository = brief_repository
        self._template_options = template_options
        # Callable (ProjectTemplate, display_name, document_id) ->
        # (document_id, instantiation); supplied by the composition root so
        # the wizard stays free of repository wiring.
        self._template_starter = template_starter
        self._used_instantiation = None
        # Summary "開く" links queue here: the wizard is exec()'d modal, so
        # navigating immediately would focus the destination behind the
        # still-open dialog. The composition drains the queue after exec().
        self._queued_links: list[object] = []

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
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText('保存して閉じる')
        buttons.accepted.connect(self._save_and_close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._pages: list[QWidget] = []
        self._page_index = 0
        self._build_pages()
        self.name_edit.textChanged.connect(self._update_save_enabled)
        self.new_radio.toggled.connect(self._update_save_enabled)
        self.existing_radio.toggled.connect(self._update_save_enabled)
        self.existing_combo.currentIndexChanged.connect(
            self._update_save_enabled
        )
        self._update_save_enabled()
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
        self.start_combo = QComboBox()
        self.start_combo.addItem('空のプロジェクト', None)
        for label, template in self._template_options:
            self.start_combo.addItem(label, template)
        if self._template_options and self._template_starter is not None:
            form.addRow('開始方法', self.start_combo)
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

        # 2-4. Guidance pages linking to the real workspaces. The room
        # step names the actual acquisition routes (#899-F) instead of a
        # single generic instruction.
        for stage, body in (
            (
                CommissioningStage.ROOM,
                '部屋ワークスペースで部屋権威を確定します。'
                '取得経路: 手動で描く / 基準図面のインポート / '
                'HTDT-Capture の取り込み / 既存プロジェクト・バンドルの利用。'
                '既存の部屋データを選んだ場合は既存権威の確認だけで済みます。',
            ),
            (
                CommissioningStage.SYSTEM,
                '部屋ワークスペースでスピーカーとリスニングポイントを配置します。'
                '計画した台数との差分は準備状況に表示されます。',
            ),
            (
                CommissioningStage.MEASUREMENT,
                '測定ワークスペースで実測データを登録します。'
                'REW インポート / HTDT-Capture / 既存ファイルから取得できます'
                '（ドキュメント目的だけの場合は任意です）。',
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
                ' ORDER BY document_id'
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

    def _update_save_enabled(self, *_args: object) -> None:
        if self.new_radio.isChecked():
            self.save_button.setEnabled(
                bool(self.name_edit.text().strip())
            )
        else:
            self.save_button.setEnabled(
                self.existing_combo.currentData() is not None
            )

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
        template = self.start_combo.currentData()
        if (
            create_document
            and template is not None
            and self._template_starter is not None
        ):
            # Template path (#898): the canonical creation pipeline seeds
            # the scene, library identity, instantiation provenance —
            # including the pending measurement pattern — and returns the
            # document identity. The design brief is materialized from the
            # merged intent when the plan is saved.
            document_id, instantiation = self._template_starter(
                template, name, name
            )
            self._used_instantiation = instantiation
            return document_id
        if create_document:
            if self.repository.latest(name) is not None:
                QMessageBox.warning(
                    self,
                    'プロジェクト名',
                    f'「{name}」というプロジェクトは既に存在します。'
                    '別の名前を入力するか、'
                    '「既存プロジェクトを設定」を選択してください。',
                )
                self._show_page(0)
                return None
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
        if self._brief_repository is not None:
            # Converge first-run intent into the canonical ProjectDesignBrief
            # (#898): never overwrites an existing brief, and merges the
            # template defaults when this save just instantiated one.
            from .project_setup_intent import materialize_commissioning_brief

            materialize_commissioning_brief(
                self._brief_repository,
                plan,
                template=(
                    self.start_combo.currentData()
                    if self._used_instantiation is not None
                    else None
                ),
                instantiation=self._used_instantiation,
            )
        if self._page_index == len(self._pages) - 1:
            plan = dataclasses.replace(plan, finished=True)
            if self._data_dir is not None:
                CommissioningPlanRepository(self._data_dir).save(plan)
        self.accept()

    def _queue_navigation(self, link: object) -> None:
        """Queue a summary link and close the modal before navigating.

        Emitted ``navigate_requested`` still fires for non-modal embedders;
        the composition instead consumes :meth:`take_pending_navigations`
        after ``exec()`` returns so focus lands after the wizard closes.
        """
        self._queued_links.append(link)
        self.navigate_requested.emit(link)
        self.accept()

    def take_pending_navigations(self) -> tuple[object, ...]:
        """Drain queued summary links (post-``exec()`` navigation list)."""
        links, self._queued_links = tuple(self._queued_links), []
        return links

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
        levels = {'required': '必須', 'recommended': '推奨', 'optional': '任意'}
        for requirement in service.requirements(plan):
            row = QHBoxLayout()
            state = {'satisfied': '完了', 'pending': '未完了', 'skipped': 'スキップ'}
            label = QLabel(
                f"[{levels[requirement.level]}] {state[requirement.status]}  "
                f"{requirement.title} — {requirement.reason}"
            )
            label.setWordWrap(True)
            row.addWidget(label, 1)
            if requirement.link is not None:
                button = QPushButton('開く')
                button.clicked.connect(
                    lambda checked=False, link=requirement.link: self._queue_navigation(link)
                )
                row.addWidget(button)
            container = QWidget()
            container.setLayout(row)
            self.summary_list.addWidget(container)


__all__ = ['CommissioningWizard']
