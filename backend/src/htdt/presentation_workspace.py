"""Presentation workspace — client-facing review surface (#534).

Read-only replay surface over exact design authority. A session is
assembled from the persisted scene (or a pinned variant) plus captured
camera viewpoints, sealed into a ``PresentationSession``, then replayed,
A/B-compared in lockstep, exported as an offline review/proposal
package, and annotated through proposals/decisions/review notes —
never by mutating engineering state.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .cad_design_comparison import DesignComparisonSet
from .cad_design_comparison_repository import CadDesignComparisonRepository
from .cad_design_decision import (
    DecisionAuthorityRef,
    build_decision_record,
)
from .cad_design_decision_repository import CadDesignDecisionRepository
from .cad_presentation_repository import (
    CadPresentationRepository,
    PresentationConflictError,
)
from .cad_presentation_session import (
    PresentationSection,
    PresentationSession,
    PresentationViewpoint,
    SynchronizedSide,
    build_presentation_proposal,
    build_presentation_session,
    build_sync_binding,
    build_viewpoint,
)
from .cad_proposal_package import build_proposal_package
from .cad_repository import SceneRepository
from .cad_review_note import ReviewNoteRepository, add_review_note
from .cad_review_package import (
    OffscreenSceneRenderer,
    build_review_package,
    derived_yaw_steps,
)
from .cad_system_variant_repository import CadSystemVariantRepository
from .clock import utc_now_iso as _utc_now
from .room_viewport import RoomOverlayState, RoomViewport3D


_VIEW_ONLY_OVERLAYS = RoomOverlayState(grid=True, labels=True)


class PresentationWorkspace(QWidget):
    """Bounded presentation surface — replay + capture, never editing."""

    CONTEXTS = ('session', 'compare', 'decisions', 'export')

    def __init__(
        self,
        repository: SceneRepository,
        document_id: str,
        *,
        navigate=None,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.document_id = document_id
        self._navigate = navigate
        self.presentation_repository = CadPresentationRepository(repository)
        self.variant_repository = CadSystemVariantRepository(repository)
        self.comparison_repository = CadDesignComparisonRepository(repository)
        self.decision_repository = CadDesignDecisionRepository(repository)
        self.note_repository = ReviewNoteRepository(repository.path)

        self._session: PresentationSession | None = None
        # The document the pinned session resolves to — replay applies
        # per-viewpoint hidden/section/focus state against this, not the
        # live head.
        self._session_document = None
        self._pending_viewpoints: list[PresentationViewpoint] = []
        self._step_index = 0

        self.pages: dict[str, QWidget] = {}
        self.stack = QStackedWidget(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.addWidget(self.stack)

        self._build_session_page()
        self._build_compare_page()
        self._build_decisions_page()
        self._build_export_page()
        self.set_context('session')
        self.refresh()

    # ------------------------------------------------------------------
    # Shell mount contract

    def set_context(self, context_id: str) -> None:
        if context_id not in self.pages:
            raise ValueError(f'unknown Presentation context: {context_id}')
        self.stack.setCurrentWidget(self.pages[context_id])

    def refresh(self) -> None:
        self._refresh_session_list()
        self._refresh_compare_sets()
        self._refresh_decision_lists()

    # ------------------------------------------------------------------
    # Shared builders

    def _viewport(self) -> RoomViewport3D:
        viewport = RoomViewport3D()
        # Read-only surface: never attach the editing controllers the
        # Room workspace uses — the camera moves, nothing else does.
        return viewport

    def _render_document_into(
        self,
        viewport: RoomViewport3D,
        document,
        *,
        hidden_ids=(),
    ) -> None:
        viewport.render_document(
            document,
            selected_id=None,
            hidden_ids=frozenset(hidden_ids),
            overlays=_VIEW_ONLY_OVERLAYS,
            reset_camera=True,
        )

    # ------------------------------------------------------------------
    # Session page

    def _build_session_page(self) -> None:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Horizontal, page)

        canvas_host = QWidget()
        canvas_layout = QVBoxLayout(canvas_host)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        self.viewport = self._viewport()
        canvas_layout.addWidget(self.viewport)

        self.stale_banner = QLabel('')
        self.stale_banner.setObjectName('presentationStaleBanner')
        self.stale_banner.setStyleSheet('color:#b8892a;padding:4px;')
        self.stale_banner.setVisible(False)
        canvas_layout.insertWidget(0, self.stale_banner)
        splitter.addWidget(canvas_host)

        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(10, 0, 0, 0)
        side_layout.setSpacing(8)

        session_group = QGroupBox('プレゼンセッション')
        session_form = QVBoxLayout(session_group)
        self.session_list = QListWidget()
        self.session_list.setAccessibleName('プレゼンセッション一覧')
        self.session_list.currentRowChanged.connect(
            self._on_session_selected
        )
        session_form.addWidget(self.session_list)
        row = QHBoxLayout()
        load_button = QPushButton('再生')
        load_button.setAccessibleName('選択したセッションを再生')
        load_button.clicked.connect(self._replay_selected)
        row.addWidget(load_button)
        session_form.addLayout(row)
        side_layout.addWidget(session_group)

        build_group = QGroupBox('新規セッション')
        build_form = QFormLayout(build_group)
        self.session_label_edit = QLineEdit()
        self.session_label_edit.setAccessibleName('セッション名')
        build_form.addRow('セッション名', self.session_label_edit)
        self.status_combo = QComboBox()
        for key, label in (
            ('draft', '下書き'),
            ('proposed', '提案（未確定）'),
            ('accepted', '承認済み案'),
            ('as_built', '竣工実績'),
        ):
            self.status_combo.addItem(label, key)
        self.status_combo.setAccessibleName('ステータスラベル')
        build_form.addRow('ステータス', self.status_combo)
        self.variant_combo = QComboBox()
        self.variant_combo.setAccessibleName('ピン留めするバリアント')
        build_form.addRow('バリアント', self.variant_combo)
        self.set_combo = QComboBox()
        self.set_combo.setAccessibleName('ピン留めする比較セット')
        build_form.addRow('比較セット', self.set_combo)
        viewpoint_row = QHBoxLayout()
        capture_button = QPushButton('現在の視点を追加')
        capture_button.setAccessibleName('現在のカメラ状態をビューポイントとして記録')
        capture_button.clicked.connect(self._capture_viewpoint)
        viewpoint_row.addWidget(capture_button)
        self.pending_label = QLabel('0 件')
        viewpoint_row.addWidget(self.pending_label)
        build_form.addRow('ビューポイント', viewpoint_row)
        save_button = QPushButton('セッションを保存')
        save_button.setAccessibleName('プレゼンセッションを保存')
        save_button.clicked.connect(self._save_session)
        build_form.addRow(save_button)
        side_layout.addWidget(build_group)

        step_group = QGroupBox('再生')
        step_layout = QHBoxLayout(step_group)
        back_button = QPushButton('◀')
        back_button.setAccessibleName('前のビューポイント')
        back_button.clicked.connect(lambda: self._step(-1))
        step_layout.addWidget(back_button)
        fwd_button = QPushButton('▶')
        fwd_button.setAccessibleName('次のビューポイント')
        fwd_button.clicked.connect(lambda: self._step(1))
        step_layout.addWidget(fwd_button)
        self.step_label = QLabel('—')
        step_layout.addWidget(self.step_label, 1)
        side_layout.addWidget(step_group)
        side_layout.addStretch(1)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        layout.addWidget(splitter)
        self.pages['session'] = page
        self.stack.addWidget(page)

    def _refresh_session_list(self) -> None:
        current_id = (
            None if self._session is None else self._session.session_id
        )
        self.session_list.blockSignals(True)
        self.session_list.clear()
        for session in self.presentation_repository.list_sessions(
            self.document_id
        ):
            item = QListWidgetItem(
                f'{session.label}（{len(session.viewpoints)} 視点）'
            )
            item.setData(Qt.UserRole, session.session_id)
            self.session_list.addItem(item)
            if session.session_id == current_id:
                self.session_list.setCurrentItem(item)
        self.session_list.blockSignals(False)
        self._refresh_authority_combos()

    def _refresh_authority_combos(self) -> None:
        self.variant_combo.blockSignals(True)
        self.set_combo.blockSignals(True)
        self.variant_combo.clear()
        self.set_combo.clear()
        self.variant_combo.addItem('（なし — 基準リビジョン）', None)
        try:
            for variant in self.variant_repository.list_variants(
                self.document_id
            ):
                self.variant_combo.addItem(
                    variant.name, (variant.variant_id, variant.variant_sha256)
                )
        except Exception:
            pass
        self.set_combo.addItem('（なし）', None)
        try:
            for item in self.comparison_repository.list_sets(
                self.document_id
            ):
                self.set_combo.addItem(
                    f'{item.name} rev{item.revision}',
                    (item.set_id, item.set_sha256),
                )
        except Exception:
            pass
        self.variant_combo.blockSignals(False)
        self.set_combo.blockSignals(False)

    def _on_session_selected(self, _row: int) -> None:
        pass  # 選択だけでは描かない — 明示的に再生

    def _load_head_into_viewport(self) -> None:
        head = self.repository.current_head(self.document_id)
        if head is None:
            return
        self._render_document_into(self.viewport, head.document)
        self.viewport.fit_scene()

    def _capture_viewpoint(self) -> None:
        index = len(self._pending_viewpoints) + 1
        state = self.viewport.capture_camera_state()
        viewpoint = build_viewpoint(
            name=f'視点 {index}',
            camera=state,
        )
        self._pending_viewpoints.append(viewpoint)
        self.pending_label.setText(
            f'{len(self._pending_viewpoints)} 件'
        )

    def _save_session(self) -> None:
        label = self.session_label_edit.text().strip()
        if not label:
            QMessageBox.warning(self, 'プレゼン', 'セッション名を入力してください')
            return
        if not self._pending_viewpoints:
            QMessageBox.warning(
                self, 'プレゼン', '先にビューポイントを追加してください'
            )
            return
        head = self.repository.current_head(self.document_id)
        if head is None:
            QMessageBox.warning(
                self, 'プレゼン', 'プロジェクトに保存済みのリビジョンがありません'
            )
            return
        variant_pin = self.variant_combo.currentData()
        set_pin = self.set_combo.currentData()
        status_label = self.status_combo.currentData()
        if variant_pin is not None and status_label == 'as_built':
            QMessageBox.warning(
                self,
                'プレゼン',
                'バリアントにピン留めされたセッションは「竣工実績」に'
                'できません — 提案として記録してください',
            )
            return
        session = build_presentation_session(
            document_id=self.document_id,
            label=label,
            scene_revision_id=head.revision_id,
            scene_content_hash=head.content_hash,
            viewpoints=tuple(self._pending_viewpoints),
            system_variant_id=None if variant_pin is None else variant_pin[0],
            system_variant_sha256=(
                None if variant_pin is None else variant_pin[1]
            ),
            comparison_set_id=None if set_pin is None else set_pin[0],
            comparison_set_sha256=None if set_pin is None else set_pin[1],
            status_label=status_label,
            sections=tuple(
                PresentationSection(
                    section_id=f'section-{i + 1}',
                    title=viewpoint.name,
                    viewpoint_id=viewpoint.viewpoint_id,
                )
                for i, viewpoint in enumerate(self._pending_viewpoints)
            ),
            author=None,
            created_at_utc=_utc_now(),
        )
        try:
            self.presentation_repository.save_session(session)
        except (ValueError, PresentationConflictError) as exc:
            QMessageBox.warning(self, 'プレゼン', f'保存に失敗: {exc}')
            return
        self._pending_viewpoints.clear()
        self.pending_label.setText('0 件')
        self._session = session
        self._refresh_session_list()
        self._show_session(session)

    def _replay_selected(self) -> None:
        item = self.session_list.currentItem()
        if item is None:
            return
        session = self.presentation_repository.get_session(
            item.data(Qt.UserRole)
        )
        if session is None:
            return
        self._session = session
        self._step_index = 0
        self._show_session(session)

    def _show_session(self, session: PresentationSession) -> None:
        try:
            document = self.presentation_repository.session_document(session)
        except ValueError as exc:
            QMessageBox.warning(self, 'プレゼン', f'再現に失敗: {exc}')
            return
        self._session_document = document
        self._render_document_into(self.viewport, document)
        stale = self.presentation_repository.session_stale(session)
        self.stale_banner.setVisible(stale)
        if stale:
            self.stale_banner.setText(
                f'このセッションは旧リビジョン {session.scene_revision_id[:12]}… '
                'にピン留めされています（現在の最新とは異なります）'
            )
        self._show_step(0)

    def _show_step(self, index: int) -> None:
        if self._session is None:
            return
        viewpoints = self._session.ordered_viewpoints()
        if not viewpoints:
            return
        index = max(0, min(index, len(viewpoints) - 1))
        self._step_index = index
        viewpoint = viewpoints[index]
        document = self._session_document
        if document is not None:
            # A viewpoint records the whole replay state — camera AND the
            # visibility/section context it was captured against. Steps
            # that only move the camera would silently lie about what was
            # presented.
            self.viewport.set_aux_render_state(section=viewpoint.section)
            self._render_document_into(
                self.viewport,
                document,
                hidden_ids=viewpoint.hidden_ids or (),
            )
            if viewpoint.focus_entity_id:
                self.viewport.focus_entity(viewpoint.focus_entity_id)
        self.viewport.apply_camera_state(viewpoint.camera)
        self.step_label.setText(
            f'{index + 1} / {len(viewpoints)} — {viewpoint.name}'
        )

    def _step(self, delta: int) -> None:
        self._show_step(self._step_index + delta)

    # ------------------------------------------------------------------
    # Compare page

    def _build_compare_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        controls = QHBoxLayout()
        controls.addWidget(QLabel('比較セット:'))
        self.compare_set_combo = QComboBox()
        self.compare_set_combo.setAccessibleName('比較セット選択')
        self.compare_set_combo.currentIndexChanged.connect(
            self._refresh_compare_alternatives
        )
        controls.addWidget(self.compare_set_combo)
        controls.addWidget(QLabel('左:'))
        self.left_combo = QComboBox()
        self.left_combo.setAccessibleName('左側の比較案')
        controls.addWidget(self.left_combo)
        controls.addWidget(QLabel('右:'))
        self.right_combo = QComboBox()
        self.right_combo.setAccessibleName('右側の比較案')
        controls.addWidget(self.right_combo)
        self.lockstep_check = QCheckBox('カメラ同期')
        self.lockstep_check.setChecked(True)
        controls.addWidget(self.lockstep_check)
        compare_button = QPushButton('読み込み')
        compare_button.setAccessibleName('比較を読み込み')
        compare_button.clicked.connect(self._load_comparison)
        controls.addWidget(compare_button)
        bind_button = QPushButton('同期バインディングを保存')
        bind_button.setAccessibleName('同期レビューバインディングを保存')
        bind_button.clicked.connect(self._save_binding)
        controls.addWidget(bind_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        splitter = QSplitter(Qt.Horizontal)
        left_host = QWidget()
        left_layout = QVBoxLayout(left_host)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.left_label = QLabel('左')
        left_layout.addWidget(self.left_label)
        self.left_viewport = self._viewport()
        left_layout.addWidget(self.left_viewport)
        splitter.addWidget(left_host)
        right_host = QWidget()
        right_layout = QVBoxLayout(right_host)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.right_label = QLabel('右')
        right_layout.addWidget(self.right_label)
        self.right_viewport = self._viewport()
        right_layout.addWidget(self.right_viewport)
        splitter.addWidget(right_host)
        layout.addWidget(splitter, 1)
        self.pages['compare'] = page
        self.stack.addWidget(page)

    def _refresh_compare_sets(self) -> None:
        self.compare_set_combo.blockSignals(True)
        self.compare_set_combo.clear()
        try:
            for item in self.comparison_repository.list_sets(
                self.document_id
            ):
                self.compare_set_combo.addItem(
                    f'{item.name} rev{item.revision}', item.set_id
                )
        except Exception:
            pass
        self.compare_set_combo.blockSignals(False)
        self._refresh_compare_alternatives()

    def _current_set(self) -> DesignComparisonSet | None:
        set_id = self.compare_set_combo.currentData()
        if set_id is None:
            return None
        try:
            return self.comparison_repository.get_set(set_id)
        except Exception:
            return None

    def _refresh_compare_alternatives(self, *_args) -> None:
        comparison_set = self._current_set()
        for combo in (self.left_combo, self.right_combo):
            combo.blockSignals(True)
            combo.clear()
            if comparison_set is not None:
                for alternative in comparison_set.alternatives:
                    combo.addItem(
                        alternative.label,
                        (
                            comparison_set.set_id,
                            comparison_set.set_sha256,
                            alternative.alternative_id,
                            alternative.alternative_sha256,
                            alternative.scene_revision_id,
                            alternative.scene_content_hash,
                        ),
                    )
            combo.blockSignals(False)
        if comparison_set is not None and len(comparison_set.alternatives) > 1:
            self.right_combo.setCurrentIndex(1)

    def _load_comparison(self) -> None:
        comparison_set = self._current_set()
        if comparison_set is None:
            QMessageBox.warning(
                self, 'A/B比較', '比較セットを選択してください'
            )
            return
        left_pin = self.left_combo.currentData()
        right_pin = self.right_combo.currentData()
        if left_pin is None or right_pin is None:
            return
        if left_pin[2] == right_pin[2]:
            QMessageBox.warning(
                self, 'A/B比較', '左右に異なる案を選んでください'
            )
            return
        left_alt = comparison_set.alternative(left_pin[2])
        right_alt = comparison_set.alternative(right_pin[2])
        try:
            left_doc = self.presentation_repository.alternative_document(
                left_alt
            )
            right_doc = self.presentation_repository.alternative_document(
                right_alt
            )
        except ValueError as exc:
            QMessageBox.warning(self, 'A/B比較', f'再現に失敗: {exc}')
            return
        self._render_document_into(self.left_viewport, left_doc)
        self._render_document_into(self.right_viewport, right_doc)
        self.left_label.setText(f'左: {left_alt.label}')
        self.right_label.setText(f'右: {right_alt.label}')
        # Lockstep: seed both cameras from the left pin when the
        # alternative carries one; otherwise fit both deterministically.
        self.left_viewport.fit_scene()
        if self.lockstep_check.isChecked():
            state = self.left_viewport.capture_camera_state()
            self.right_viewport.apply_camera_state(state)
        else:
            self.right_viewport.fit_scene()

    def _save_binding(self) -> None:
        comparison_set = self._current_set()
        left_pin = self.left_combo.currentData()
        right_pin = self.right_combo.currentData()
        if (
            comparison_set is None
            or left_pin is None
            or right_pin is None
            or left_pin[2] == right_pin[2]
        ):
            QMessageBox.warning(
                self, 'A/B比較', '左右に異なる案を選んでください'
            )
            return
        left_alt = comparison_set.alternative(left_pin[2])
        right_alt = comparison_set.alternative(right_pin[2])
        binding = build_sync_binding(
            document_id=self.document_id,
            label=f'{comparison_set.name} — {left_alt.label} / {right_alt.label}',
            left=SynchronizedSide(
                kind='comparison_alternative',
                comparison_set_id=comparison_set.set_id,
                comparison_set_sha256=comparison_set.set_sha256,
                alternative_id=left_alt.alternative_id,
                alternative_sha256=left_alt.alternative_sha256,
                scene_revision_id=left_alt.scene_revision_id,
                scene_content_hash=left_alt.scene_content_hash,
                label=left_alt.label,
            ),
            right=SynchronizedSide(
                kind='comparison_alternative',
                comparison_set_id=comparison_set.set_id,
                comparison_set_sha256=comparison_set.set_sha256,
                alternative_id=right_alt.alternative_id,
                alternative_sha256=right_alt.alternative_sha256,
                scene_revision_id=right_alt.scene_revision_id,
                scene_content_hash=right_alt.scene_content_hash,
                label=right_alt.label,
            ),
            lockstep_viewpoint=self.lockstep_check.isChecked(),
            created_at_utc=_utc_now(),
        )
        try:
            self.presentation_repository.save_binding(binding)
            QMessageBox.information(
                self, 'A/B比較', '同期バインディングを保存しました'
            )
        except (ValueError, PresentationConflictError) as exc:
            QMessageBox.warning(self, 'A/B比較', f'保存に失敗: {exc}')

    # ------------------------------------------------------------------
    # Decisions page

    def _build_decisions_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        form = QGroupBox('決定を記録')
        form_layout = QFormLayout(form)
        self.decision_title_edit = QLineEdit()
        self.decision_title_edit.setAccessibleName('決定件名')
        form_layout.addRow('件名', self.decision_title_edit)
        self.decision_author_edit = QLineEdit()
        self.decision_author_edit.setAccessibleName('記録者')
        form_layout.addRow('記録者', self.decision_author_edit)
        self.decision_rationale_edit = QLineEdit()
        self.decision_rationale_edit.setAccessibleName('根拠')
        form_layout.addRow('根拠', self.decision_rationale_edit)
        self.decision_scope_combo = QComboBox()
        for scope in (
            'design_direction',
            'exploratory_preference',
            'installation_planning',
        ):
            self.decision_scope_combo.addItem(scope, scope)
        self.decision_scope_combo.setAccessibleName('決定範囲')
        form_layout.addRow('範囲', self.decision_scope_combo)
        self.decision_selected_combo = QComboBox()
        self.decision_selected_combo.setAccessibleName('選択した案')
        form_layout.addRow('選択案', self.decision_selected_combo)
        record_button = QPushButton('決定を記録')
        record_button.setAccessibleName('決定レコードを保存')
        record_button.clicked.connect(self._save_decision)
        form_layout.addRow(record_button)
        layout.addWidget(form)

        proposal_form = QGroupBox('提案（トラッキング）')
        proposal_layout = QFormLayout(proposal_form)
        self.proposal_title_edit = QLineEdit()
        self.proposal_title_edit.setAccessibleName('提案件名')
        proposal_layout.addRow('件名', self.proposal_title_edit)
        self.proposal_kind_combo = QComboBox()
        self.proposal_kind_combo.addItem('バリアント候補', 'variant_candidate')
        self.proposal_kind_combo.addItem('注記のみ', 'annotation_only')
        self.proposal_kind_combo.setAccessibleName('提案種別')
        proposal_layout.addRow('種別', self.proposal_kind_combo)
        self.proposal_variant_combo = QComboBox()
        self.proposal_variant_combo.setAccessibleName('バリアント参照')
        proposal_layout.addRow('バリアント', self.proposal_variant_combo)
        proposal_button = QPushButton('提案を記録')
        proposal_button.setAccessibleName('提案レコードを保存')
        proposal_button.clicked.connect(self._save_proposal)
        proposal_layout.addRow(proposal_button)
        layout.addWidget(proposal_form)

        note_form = QGroupBox('レビューメモ')
        note_layout = QFormLayout(note_form)
        self.note_author_edit = QLineEdit()
        self.note_author_edit.setAccessibleName('記録者')
        note_layout.addRow('記録者', self.note_author_edit)
        self.note_body_edit = QLineEdit()
        self.note_body_edit.setAccessibleName('メモ本文')
        note_layout.addRow('本文', self.note_body_edit)
        note_button = QPushButton('メモを追加（現在のセッションに紐付け）')
        note_button.setAccessibleName('レビューメモを保存')
        note_button.clicked.connect(self._save_note)
        note_layout.addRow(note_button)
        layout.addWidget(note_form)

        self.decisions_list = QListWidget()
        self.decisions_list.setAccessibleName('記録済み決定・提案')
        layout.addWidget(self.decisions_list, 1)
        self.pages['decisions'] = page
        self.stack.addWidget(page)

    def _refresh_decision_lists(self) -> None:
        self.decisions_list.clear()
        try:
            for decision in self.decision_repository.list_decisions(
                self.document_id
            ):
                self.decisions_list.addItem(
                    f'[決定] {decision.title} — {decision.decision_scope}'
                )
            for proposal in self.presentation_repository.list_proposals(
                self.document_id
            ):
                self.decisions_list.addItem(
                    f'[提案] {proposal.title} — {proposal.kind}'
                )
        except Exception:
            pass
        self.proposal_variant_combo.clear()
        try:
            for variant in self.variant_repository.list_variants(
                self.document_id
            ):
                self.proposal_variant_combo.addItem(
                    variant.name, (variant.variant_id, variant.variant_sha256)
                )
        except Exception:
            pass
        self.decision_selected_combo.clear()
        comparison_set = self._current_set()
        if comparison_set is not None:
            for alternative in comparison_set.alternatives:
                self.decision_selected_combo.addItem(
                    alternative.label,
                    (
                        comparison_set.set_id,
                        alternative.alternative_id,
                        alternative.alternative_sha256,
                    ),
                )

    def _save_decision(self) -> None:
        title = self.decision_title_edit.text().strip()
        selected = self.decision_selected_combo.currentData()
        comparison_set = self._current_set()
        if not title:
            QMessageBox.warning(self, '決定', '件名を入力してください')
            return
        if selected is None or comparison_set is None:
            QMessageBox.warning(
                self, '決定', 'A/B比較タブで比較セットと案を選んでください'
            )
            return
        alternatives = comparison_set.alternatives
        considered = tuple(
            DecisionAuthorityRef(
                kind='comparison_alternative',
                ref_id=item.alternative_id,
                ref_sha256=item.alternative_sha256,
                label=item.label,
            )
            for item in alternatives
        )
        record = build_decision_record(
            document_id=self.document_id,
            title=title,
            decision_scope=self.decision_scope_combo.currentData(),
            lifecycle_intent='choose_for_design',
            selected_ref=DecisionAuthorityRef(
                kind='comparison_alternative',
                ref_id=selected[1],
                ref_sha256=selected[2],
            ),
            considered_refs=considered,
            comparison_set_ref=DecisionAuthorityRef(
                kind='design_comparison_set',
                ref_id=comparison_set.set_id,
                ref_sha256=comparison_set.set_sha256,
            ),
            rationale_note=self.decision_rationale_edit.text() or None,
            author=self.decision_author_edit.text() or None,
            created_at_utc=_utc_now(),
        )
        try:
            self.decision_repository.save_decision(record)
            QMessageBox.information(
                self, '決定', '決定を記録しました'
            )
            self._refresh_decision_lists()
        except Exception as exc:
            QMessageBox.warning(self, '決定', f'記録に失敗: {exc}')

    def _save_proposal(self) -> None:
        if self._session is None:
            QMessageBox.warning(
                self, '提案', '先にセッションを再生してください'
            )
            return
        title = self.proposal_title_edit.text().strip()
        if not title:
            QMessageBox.warning(self, '提案', '件名を入力してください')
            return
        kind = self.proposal_kind_combo.currentData()
        variant_pin = self.proposal_variant_combo.currentData()
        proposal = build_presentation_proposal(
            session=self._session,
            kind=kind,
            title=title,
            system_variant_id=(
                variant_pin[0] if kind == 'variant_candidate' else None
            ),
            system_variant_sha256=(
                variant_pin[1] if kind == 'variant_candidate' else None
            ),
            author_label=self.note_author_edit.text() or None,
            created_at_utc=_utc_now(),
        )
        try:
            self.presentation_repository.save_proposal(proposal)
            QMessageBox.information(self, '提案', '提案を記録しました')
            self._refresh_decision_lists()
        except (ValueError, PresentationConflictError) as exc:
            QMessageBox.warning(self, '提案', f'記録に失敗: {exc}')

    def _save_note(self) -> None:
        if self._session is None:
            QMessageBox.warning(
                self, 'レビューメモ', '先にセッションを再生してください'
            )
            return
        author = self.note_author_edit.text().strip()
        body = self.note_body_edit.text().strip()
        if not author or not body:
            QMessageBox.warning(
                self, 'レビューメモ', '記録者と本文を入力してください'
            )
            return
        viewpoints = self._session.ordered_viewpoints()
        anchor = (
            viewpoints[self._step_index]
            if viewpoints
            else self._session.viewpoints[0]
        )
        try:
            note = add_review_note(
                document_id=self.document_id,
                subject_kind='scene_revision',
                subject_ref=self._session.scene_revision_id,
                subject_sha256=self._session.scene_content_hash,
                author_label=author,
                body=f'[{anchor.name}] {body}',
                created_at_utc=_utc_now(),
            )
            self.note_repository.save_note(note)
            self.note_body_edit.clear()
            QMessageBox.information(
                self, 'レビューメモ', 'メモを保存しました'
            )
        except Exception as exc:
            QMessageBox.warning(self, 'レビューメモ', f'保存に失敗: {exc}')

    # ------------------------------------------------------------------
    # Export page

    def _build_export_page(self) -> None:
        page = QWidget()
        layout = QFormLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)

        self.export_session_combo = QComboBox()
        self.export_session_combo.setAccessibleName('出力対象セッション')
        layout.addRow('セッション', self.export_session_combo)

        dir_row = QHBoxLayout()
        self.export_dir_label = QLabel('（未選択）')
        dir_row.addWidget(self.export_dir_label, 1)
        pick_button = QPushButton('出力先を選択')
        pick_button.setAccessibleName('パッケージ出力先フォルダを選択')
        pick_button.clicked.connect(self._pick_export_dir)
        dir_row.addWidget(pick_button)
        dir_host = QWidget()
        dir_host.setLayout(dir_row)
        layout.addRow('出力先', dir_host)

        self.yaw_check = QCheckBox('水平回転フレームを生成（360代替）')
        self.yaw_check.setChecked(True)
        self.yaw_check.setAccessibleName('ヨーステップレンダリング有効化')
        layout.addRow(self.yaw_check)

        review_button = QPushButton('レビューパッケージを生成')
        review_button.setAccessibleName('オフラインレビューパッケージを生成')
        review_button.clicked.connect(self._build_review)
        layout.addRow(review_button)

        proposal_button = QPushButton('提案パッケージを生成')
        proposal_button.setAccessibleName('提案書パッケージを生成')
        proposal_button.clicked.connect(self._build_proposal)
        layout.addRow(proposal_button)

        self.export_status = QLabel('')
        self.export_status.setWordWrap(True)
        layout.addRow(self.export_status)
        self.pages['export'] = page
        self.stack.addWidget(page)

    def _pick_export_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, 'パッケージ出力先', str(Path.home())
        )
        if chosen:
            self.export_dir_label.setText(chosen)

    def _export_session(self) -> PresentationSession | None:
        session_id = self.export_session_combo.currentData()
        if session_id is None:
            return None
        return self.presentation_repository.get_session(session_id)

    def _refresh_export_sessions(self) -> None:
        self.export_session_combo.clear()
        for session in self.presentation_repository.list_sessions(
            self.document_id
        ):
            self.export_session_combo.addItem(
                f'{session.label}（{session.status_label}）',
                session.session_id,
            )

    def _build_review(self) -> None:
        session = self._export_session()
        target = self.export_dir_label.text()
        if session is None:
            QMessageBox.warning(
                self, '出力', 'セッションを選択してください'
            )
            return
        if not target.startswith(('/', 'C:', 'D:')):
            QMessageBox.warning(
                self, '出力', '出力先を選択してください'
            )
            return
        # Yaw intent is an explicit export-time override — never a
        # rebuilt session object, whose hash would name a session that
        # was never persisted.
        if not self.yaw_check.isChecked():
            yaw_steps: tuple[int, ...] | None = ()
        elif session.render.yaw_step_deg is not None:
            yaw_steps = None  # derive from the session's declared step
        else:
            yaw_steps = derived_yaw_steps(30)
        settings = session.render
        try:
            renderer = OffscreenSceneRenderer(
                settings.image_width_px, settings.image_height_px
            )
            result = build_review_package(
                session,
                Path(target) / f'review-{session.session_id[:8]}',
                self.repository,
                presentation_repository=self.presentation_repository,
                renderer=renderer,
                yaw_steps_deg=yaw_steps,
            )
        except Exception as exc:
            QMessageBox.warning(self, '出力', f'生成に失敗: {exc}')
            return
        caps = '; '.join(
            f'{row.capability}={row.state}'
            for row in result.manifest.capability_rows
        )
        self.export_status.setText(
            f'生成完了: {result.output_dir}\n'
            f'エントリ {len(result.manifest.entries)} 件\n'
            f'能力宣言: {caps}\n'
            + ('\n'.join(result.warnings) if result.warnings else '')
        )

    def _build_proposal(self) -> None:
        session = self._export_session()
        target = self.export_dir_label.text()
        if session is None:
            QMessageBox.warning(
                self, '出力', 'セッションを選択してください'
            )
            return
        if not target.startswith(('/', 'C:', 'D:')):
            QMessageBox.warning(
                self, '出力', '出力先を選択してください'
            )
            return
        try:
            result = build_proposal_package(
                session,
                Path(target) / f'proposal-{session.session_id[:8]}',
                self.repository,
                presentation_repository=self.presentation_repository,
                comparison_repository=self.comparison_repository,
                decision_repository=self.decision_repository,
            )
        except Exception as exc:
            QMessageBox.warning(self, '出力', f'生成に失敗: {exc}')
            return
        self.export_status.setText(
            f'生成完了: {result.output_dir}\n'
            f'エントリ {len(result.manifest.entries)} 件\n'
            + ('\n'.join(result.warnings) if result.warnings else '')
        )

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_export_sessions()
        if (
            self.stack.currentWidget() is self.pages['session']
            and self._session is None
        ):
            self._load_head_into_viewport()


__all__ = [
    'PresentationWorkspace',
]
