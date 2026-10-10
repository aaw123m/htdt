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

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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

from . import file_dialog_memory
from .activity_center import ActivityCenter
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
from .cad_repository import SceneRepository
from .cad_review_note import ReviewNoteRepository, add_review_note
from .cad_review_package import derived_yaw_steps
from .cad_scene_history import ENTITY_FIELD_LABELS
from .design_ab_overlay import (
    AB_OVERLAY_CATEGORY_VOCAB,
    DesignAbOverlayPreview,
    build_ab_overlay_preview,
)
from .cad_system_variant_repository import CadSystemVariantRepository
from .clock import utc_now_iso as _utc_now
from .output_target import OutputTargetError, validate_output_target
from .package_progress import PackageBuildProgress
from .presentation_export_runner import (
    PresentationExportRunner,
    PresentationExportJob,
    _bytes_label,
)
from .room_viewport import RoomOverlayState, RoomViewport3D
from .operation_error_dialog import warn_user


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
        activity_center: ActivityCenter | None = None,
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
        # #1007: last loaded A/B pair + preview for the overlay view —
        # pinned scene revisions, never the live head.
        self._ab_preview: DesignAbOverlayPreview | None = None
        self._ab_diff_only: bool = False
        self._session_document = None
        self._pending_viewpoints: list[PresentationViewpoint] = []
        self._step_index = 0
        # Last successfully built package directory — the only location
        # 「出力先を開く」 will ever open (a verified build, never a
        # merely-validated one).
        self._last_output_dir: str | None = None

        # #985: review/proposal package builds run on a bounded worker
        # lane (never the Qt event loop), registered in the shared
        # ActivityCenter with measured progress + cooperative cancel.
        self._export_runner = PresentationExportRunner(
            scene_repository=repository,
            presentation_repository=self.presentation_repository,
            comparison_repository=self.comparison_repository,
            decision_repository=self.decision_repository,
            activity_center=activity_center,
            parent=self,
        )
        self._export_runner.export_started.connect(self._export_job_started)
        self._export_runner.export_progress.connect(self._export_job_progress)
        self._export_runner.export_completed.connect(
            self._export_job_completed
        )
        self._export_runner.export_failed.connect(self._export_job_failed)
        self._export_runner.export_cancelled.connect(
            self._export_job_cancelled
        )
        self._export_runner.export_finished.connect(self._export_job_finished)

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
            warn_user(self, 'プレゼン: セッションを保存できませんでした', exc)
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
            warn_user(self, 'プレゼン: セッションを再現できませんでした', exc)
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
        controls.addWidget(QLabel('表示:'))
        # #1007: 横並び / 単一画面重畳 / 差分のみ の切替 — the same pinned
        # pair drives all three; 重畳/差分のみ draw the color-coded
        # authority-driven diff ghosts.
        self.view_mode_combo = QComboBox()
        self.view_mode_combo.setAccessibleName('比較表示モード')
        for _label, _mode in (
            ('横並び', 'side'),
            ('重畳（単一画面）', 'overlay'),
            ('差分のみ', 'diff_only'),
        ):
            self.view_mode_combo.addItem(_label, _mode)
        self.view_mode_combo.currentIndexChanged.connect(
            self._on_compare_view_mode
        )
        controls.addWidget(self.view_mode_combo)
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

        self.compare_stack = QStackedWidget()
        self.compare_stack.addWidget(splitter)
        # 重畳/差分のみ page: one read-only viewport drawing the diff
        # actors, plus the 差分理由カード column.
        overlay_page = QWidget()
        overlay_layout = QHBoxLayout(overlay_page)
        overlay_layout.setContentsMargins(0, 0, 0, 0)
        overlay_splitter = QSplitter(Qt.Horizontal)
        overlay_canvas = QWidget()
        overlay_canvas_layout = QVBoxLayout(overlay_canvas)
        overlay_canvas_layout.setContentsMargins(0, 0, 0, 0)
        self.overlay_label = QLabel('')
        overlay_canvas_layout.addWidget(self.overlay_label)
        self.overlay_viewport = self._viewport()
        overlay_canvas_layout.addWidget(self.overlay_viewport)
        overlay_splitter.addWidget(overlay_canvas)
        card_host = QWidget()
        card_layout = QVBoxLayout(card_host)
        card_layout.setContentsMargins(8, 0, 0, 0)
        card_layout.setSpacing(4)
        card_heading = QLabel('差分理由カード')
        card_heading.setAccessibleName('差分理由カード見出し')
        card_layout.addWidget(card_heading)
        self.diff_summary_label = QLabel('')
        self.diff_summary_label.setWordWrap(True)
        self.diff_summary_label.setAccessibleName('比較サマリ')
        card_layout.addWidget(self.diff_summary_label)
        self.diff_card = QListWidget()
        self.diff_card.setAccessibleName('差分理由カード')
        self.diff_card.itemClicked.connect(self._on_diff_card_row)
        card_layout.addWidget(self.diff_card, 1)
        overlay_splitter.addWidget(card_host)
        overlay_splitter.setStretchFactor(0, 1)
        overlay_splitter.setStretchFactor(1, 0)
        overlay_layout.addWidget(overlay_splitter)
        self.compare_stack.addWidget(overlay_page)
        layout.addWidget(self.compare_stack, 1)
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
        # Set/alternative pin changed (or project switched): drop any loaded
        # overlay so stale ghosts never linger next to a new selection.
        self._ab_preview = None
        if getattr(self, 'overlay_viewport', None) is not None:
            self.overlay_viewport.clear_design_ab_overlay()
        if getattr(self, 'diff_card', None) is not None:
            self.diff_card.clear()
            self.diff_summary_label.setText('')
            self.overlay_label.setText('')

    def _load_comparison(self) -> None:
        pair = self._selected_alternative_pair(show_dialogs=True)
        if pair is None:
            return
        comparison_set, left_alt, right_alt = pair
        self._dispatch_comparison_load(comparison_set, left_alt, right_alt)

    def _selected_alternative_pair(
        self, *, show_dialogs: bool
    ) -> tuple[DesignComparisonSet, object, object] | None:
        comparison_set = self._current_set()
        if comparison_set is None:
            if show_dialogs:
                QMessageBox.warning(
                    self, 'A/B比較', '比較セットを選択してください'
                )
            return None
        left_pin = self.left_combo.currentData()
        right_pin = self.right_combo.currentData()
        if left_pin is None or right_pin is None:
            return None
        if left_pin[2] == right_pin[2]:
            if show_dialogs:
                QMessageBox.warning(
                    self, 'A/B比較', '左右に異なる案を選んでください'
                )
            return None
        left_alt = comparison_set.alternative(left_pin[2])
        right_alt = comparison_set.alternative(right_pin[2])
        if left_alt is None or right_alt is None:
            return None
        return comparison_set, left_alt, right_alt

    def _dispatch_comparison_load(
        self, comparison_set, left_alt, right_alt
    ) -> None:
        mode = self.view_mode_combo.currentData()
        if mode == 'side':
            self.compare_stack.setCurrentIndex(0)
            self._load_side_comparison(comparison_set, left_alt, right_alt)
            # Side-by-side is the active view — the overlay layer holds
            # nothing visible; drop its actors so no stale diff lingers.
            self.overlay_viewport.clear_design_ab_overlay()
            self.diff_card.clear()
            self.diff_summary_label.setText('')
            self._ab_preview = None
            return
        self.compare_stack.setCurrentIndex(1)
        self._load_overlay_comparison(
            comparison_set, left_alt, right_alt, diff_only=(mode == 'diff_only')
        )

    def _on_compare_view_mode(self, *_args) -> None:
        # Fires once during page construction (first addItem) before the
        # compare stack exists — no-op then.
        if getattr(self, 'compare_stack', None) is None:
            return
        mode = self.view_mode_combo.currentData()
        self.compare_stack.setCurrentIndex(0 if mode == 'side' else 1)
        # A mode switch re-dispatches the loaded pair silently — the pins
        # are re-verified on every load, so a stale pin degrades to the
        # honest '比較不可' state rather than painting guessed ghosts.
        pair = self._selected_alternative_pair(show_dialogs=False)
        if pair is not None:
            comparison_set, left_alt, right_alt = pair
            self._dispatch_comparison_load(comparison_set, left_alt, right_alt)
        elif mode != 'side':
            self.overlay_viewport.clear_design_ab_overlay()
            self.diff_card.clear()
            self.diff_summary_label.setText('')
            self._ab_preview = None

    def _load_side_comparison(
        self, comparison_set, left_alt, right_alt
    ) -> None:
        try:
            left_doc = self.presentation_repository.alternative_document(
                left_alt
            )
            right_doc = self.presentation_repository.alternative_document(
                right_alt
            )
        except ValueError as exc:
            warn_user(self, 'A/B比較: 対象を再現できませんでした', exc)
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

    def _load_overlay_comparison(
        self, comparison_set, left_alt, right_alt, *, diff_only: bool
    ) -> None:
        """Single-view A/B overlay — authority-driven diff ghosts (#1007).

        Resolution re-runs ``alternative_document`` so stale pins or hash
        drift degrade to the preview's 'impossible' state instead of an
        invented overlay.
        """
        head = None
        try:
            head = self.repository.current_head(comparison_set.document_id)
        except Exception:
            head = None
        preview = build_ab_overlay_preview(
            comparison_set,
            left_alt,
            right_alt,
            resolve_document=self.presentation_repository.alternative_document,
            evidence_resolver=self.comparison_repository.ref_resolver,
            head_revision_id=(
                head.revision_id if head is not None else None
            ),
        )
        self._ab_preview = preview
        self._ab_diff_only = diff_only
        self.overlay_viewport.render_design_ab_overlay(
            preview, show_context=not diff_only
        )
        self.overlay_label.setText(
            f'案A「{left_alt.label}」 / 案B「{right_alt.label}」'
            ' — 差分ゴースト（読み取り専用）'
        )
        self._fill_diff_card(preview)
        self.overlay_viewport.fit_scene()

    def _fill_diff_card(self, preview: DesignAbOverlayPreview) -> None:
        self.diff_card.clear()
        self.diff_summary_label.setText(preview.summary)

        def _row(text: str, entity_id: str | None = None) -> None:
            item = QListWidgetItem(text)
            if entity_id is not None:
                item.setData(Qt.ItemDataRole.UserRole, entity_id)
            self.diff_card.addItem(item)

        if preview.state == 'impossible':
            _row('比較不可')
            for reason in preview.impossible_reasons:
                _row(f'・{reason}')
            _row(preview.disclaimer)
        else:
            if preview.staleness_note:
                _row(preview.staleness_note)
            for line in preview.context_changed:
                _row(line)
            if not preview.items and not preview.context_changed:
                _row('変更なし（同一内容）')
            for item in preview.items:
                label, _color = AB_OVERLAY_CATEGORY_VOCAB[item.category]
                fields = '、'.join(
                    ENTITY_FIELD_LABELS.get(field, field)
                    for field in item.changed_fields
                )
                text = f'{item.name}（{item.kind}）— {label}'
                if fields:
                    text += f': {fields}'
                text += f' / 理由: {item.reason}'
                _row(text, entity_id=item.entity_id)
            if preview.evidence_rows:
                _row('— 証跡の利用可否 —')
                for row in preview.evidence_rows:
                    _row(
                        f'{row.alternative_label} · '
                        f'{row.kind}:{row.ref_id} — {row.state_label}'
                        f'（{row.reason}）'
                    )
            _row(preview.disclaimer)

    def _on_diff_card_row(self, item) -> None:
        """Diff-row → overlay deep link: re-render with the row's entity
        highlighted. The row names a pinned entity id only — no selection
        or edit is implied."""
        preview = self._ab_preview
        if preview is None or preview.state != 'ready':
            return
        entity_id = item.data(Qt.ItemDataRole.UserRole)
        self.overlay_viewport.render_design_ab_overlay(
            preview,
            show_context=not getattr(self, '_ab_diff_only', False),
            highlight_entity_id=entity_id,
        )

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
            warn_user(self, 'A/B比較: バインディングを保存できませんでした', exc)

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
            warn_user(self, '決定を記録できませんでした', exc)

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
            warn_user(self, '提案を記録できませんでした', exc)

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
            warn_user(self, 'レビューメモを保存できませんでした', exc)

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
        self.open_output_button = QPushButton('出力先を開く')
        self.open_output_button.setAccessibleName(
            '生成したパッケージの出力先フォルダを開く'
        )
        self.open_output_button.setVisible(False)
        self.open_output_button.clicked.connect(self._open_output_dir)
        dir_row.addWidget(self.open_output_button)
        dir_host = QWidget()
        dir_host.setLayout(dir_row)
        layout.addRow('出力先', dir_host)

        self.yaw_check = QCheckBox('水平回転フレームを生成（360代替）')
        self.yaw_check.setChecked(True)
        self.yaw_check.setAccessibleName('ヨーステップレンダリング有効化')
        layout.addRow(self.yaw_check)

        self.export_review_button = QPushButton(
            'レビューパッケージを生成'
        )
        self.export_review_button.setAccessibleName(
            'オフラインレビューパッケージを生成'
        )
        self.export_review_button.setToolTip(
            'バックグラウンドで生成します — 実行中も画面は操作できます'
        )
        self.export_review_button.setWhatsThis(
            'バックグラウンドで生成します — 実行中も画面は操作できます'
        )
        self.export_review_button.clicked.connect(self._build_review)
        layout.addRow(self.export_review_button)

        self.export_proposal_button = QPushButton('提案パッケージを生成')
        self.export_proposal_button.setAccessibleName(
            '提案書パッケージを生成'
        )
        self.export_proposal_button.setToolTip(
            'バックグラウンドで生成します — 実行中も画面は操作できます'
        )
        self.export_proposal_button.setWhatsThis(
            'バックグラウンドで生成します — 実行中も画面は操作できます'
        )
        self.export_proposal_button.clicked.connect(self._build_proposal)
        layout.addRow(self.export_proposal_button)

        self.export_cancel_button = QPushButton('出力を中止')
        self.export_cancel_button.setAccessibleName('パッケージ出力を中止')
        self.export_cancel_button.setToolTip(
            '実行中の出力ジョブを次の安全な区切りで中止します'
        )
        self.export_cancel_button.setWhatsThis(
            '実行中の出力ジョブを次の安全な区切りで中止します'
        )
        self.export_cancel_button.setVisible(False)
        self.export_cancel_button.clicked.connect(self._cancel_export)
        layout.addRow(self.export_cancel_button)

        self.export_status = QLabel('')
        self.export_status.setWordWrap(True)
        layout.addRow(self.export_status)
        self.pages['export'] = page
        self.stack.addWidget(page)

    def _pick_export_dir(self) -> None:
        chosen = file_dialog_memory.get_existing_directory(
            self,
            'パッケージ出力先',
            'presentation.export_dir',
            default_dir=str(Path.home()),
        )
        if chosen:
            self.export_dir_label.setText(chosen)

    def _open_output_dir(self) -> None:
        if not self._last_output_dir:
            return
        if not QDesktopServices.openUrl(
            QUrl.fromLocalFile(self._last_output_dir)
        ):
            QMessageBox.warning(
                self, '出力', '出力先を開けませんでした'
            )

    def _validated_export_dir(self, package_name: str) -> Path | None:
        """The one output-target rule for both package builders (#984).

        The chooser label is the operator's spelled root; validation
        resolves it against the real filesystem (any drive letter,
        UNC shares, permissions, reparse points, collisions). A
        rejection shows the named reason + suggested alternative and
        returns ``None`` — nothing falls back outside the chosen root.
        """
        raw = self.export_dir_label.text().strip()
        if raw == '（未選択）':
            raw = ''
        try:
            target = validate_output_target(raw, package_name=package_name)
        except OutputTargetError as exc:
            QMessageBox.warning(self, '出力', exc.operator_text())
            return None
        return target.package_dir

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
        self._start_export('review')

    def _build_proposal(self) -> None:
        self._start_export('proposal')

    def _cancel_export(self) -> None:
        self._export_runner.request_cancel()
        self.export_cancel_button.setEnabled(False)

    def _start_export(self, kind: str) -> None:
        """Pin the job on the UI thread, then hand it to the worker.

        Everything the build needs — the sealed session, its source
        revision, the render intent, the validated output folder and the
        expected work items — is resolved *here* (#985): after
        ``runner.start`` the combo can point at another session without
        re-attributing the running job's result.
        """
        if self._export_runner.busy:
            # Double-click / parallel presses never start a second job.
            return
        session = self._export_session()
        if session is None:
            QMessageBox.warning(
                self, '出力', 'セッションを選択してください'
            )
            return
        # #989: sensitive-data preflight. Sealed packages cannot drop
        # members, so external scope blocks when any category is
        # ineligible — the gate is whole-or-nothing by design.
        if not self._export_preflight(kind, session):
            return
        package_dir = self._validated_export_dir(
            f'{kind}-{session.session_id[:8]}'
        )
        if package_dir is None:
            return
        expected_frames = 0
        expected_sheets = 0
        if kind == 'review':
            # Yaw intent is an explicit export-time override — never a
            # rebuilt session object, whose hash would name a session
            # that was never persisted.
            if not self.yaw_check.isChecked():
                yaw_steps: tuple[int, ...] | None = ()
            elif session.render.yaw_step_deg is not None:
                yaw_steps = None  # derive from the session's step
            else:
                yaw_steps = derived_yaw_steps(30)
            effective_steps = (
                yaw_steps
                if yaw_steps is not None
                else derived_yaw_steps(session.render.yaw_step_deg)
            )
            expected_frames = len(session.ordered_viewpoints()) * (
                1 + len(effective_steps)
            )
            expected_sheets = 3
        else:
            yaw_steps = None
            expected_sheets = 4
        job = PresentationExportJob(
            kind=kind,
            session=session,
            package_dir=package_dir,
            output_root=package_dir.parent,
            yaw_steps_deg=yaw_steps,
            include_drawings=True,
            expected_frames=expected_frames,
            expected_sheets=expected_sheets,
        )
        self._export_runner.start(job)

    def _export_preflight(self, kind: str, session) -> bool:
        """#989 review gate for sealed review/proposal packages."""
        from .cad_code_policy_repository import CadCodePolicyRepository
        from .export_preflight import (
            PreflightPlan,
            analyze_member_categories,
            build_manifest,
            ensure_export_policy,
            evaluate_elements,
            record_confirmations,
            stored_classification_map,
        )
        from .export_preflight_dialog import ExportPreflightDialog

        code_repository = CadCodePolicyRepository(self.repository)
        categories = (
            {'rendered_frames': 'レンダリング画像（撮影済み視点）',
             'drawing_sheets': '図面シート',
             'spec_manifest': '仕様・マニフェスト（セッション固有情報）'}
            if kind == 'review'
            else {'rendered_frames': 'レンダリング画像（撮影済み視点）',
                  'drawing_sheets': '図面シート',
                  'spec_manifest': '仕様・マニフェスト（セッション固有情報）'}
        )
        head = self.repository.current_head(self.document_id)
        plan = PreflightPlan(
            export_kind=f'presentation_{kind}',
            document_id=self.document_id,
            source_revision_id=session.scene_revision_id,
            source_sha256=(None if head is None else head.content_hash),
            elements=analyze_member_categories(
                categories,
                stored_classification_map(
                    code_repository, self.document_id
                ),
            ),
            member_exclusion_supported=False,
        )
        dialog = ExportPreflightDialog(
            plan,
            title='プレゼン出力',
            default_scope='external_review',
            parent=self,
        )
        if dialog.exec() != ExportPreflightDialog.DialogCode.Accepted:
            return False
        scope = dialog.scope()
        self._export_preflight_scope = scope
        record_confirmations(code_repository, plan)
        plan.policy = ensure_export_policy(
            code_repository, self.document_id
        )
        if scope == 'external_review':
            if plan.excluded():
                QMessageBox.warning(
                    self,
                    '出力できません',
                    '外部送付の条件を満たさない要素があるため、'
                    'パッケージ全体の出力を停止しました。',
                )
                return False
            try:
                build_manifest(
                    code_repository,
                    plan,
                    bundle_kind='client_package',
                    policy=plan.policy,
                )
            except ValueError as exc:
                QMessageBox.warning(self, '出力できません', str(exc))
                return False
            blockers = evaluate_elements(plan)
            if blockers:
                QMessageBox.warning(
                    self,
                    '出力できません',
                    '外部送付の条件を満たさない項目があります:\n'
                    + '\n'.join(blockers),
                )
                return False
        self._export_preflight_plan = plan
        return True

    def _export_job_started(self, job: PresentationExportJob) -> None:
        self.export_review_button.setEnabled(False)
        self.export_proposal_button.setEnabled(False)
        self.export_cancel_button.setVisible(True)
        self.export_cancel_button.setEnabled(True)
        self.open_output_button.setVisible(False)
        expected = ''
        if job.expected_frames:
            expected += f'・フレーム {job.expected_frames} 件'
        if job.expected_sheets:
            expected += f'・図面 {job.expected_sheets} 枚'
        self.export_status.setText(
            '実行中 — セッション'
            f'「{job.session.label}」'
            f'（リビジョン {job.session.scene_revision_id[:8]}）を\n'
            f'{job.package_dir} へ出力しています{expected}'
        )

    def _export_job_progress(self, progress: PackageBuildProgress) -> None:
        line = (
            f'{progress.stage_label}'
            f'（{progress.stage_index}/{progress.stage_count}）'
        )
        if progress.done_units is not None and progress.total_units:
            line += (
                f' — {progress.done_units}/{progress.total_units}'
                f' {progress.unit_label}'
            )
        if progress.bytes_written:
            line += f'、{_bytes_label(progress.bytes_written)} 書込み'
        self.export_status.setText(f'実行中: {line}')

    def _export_job_completed(self, built) -> None:
        result = built.result
        job = built.job
        # The published name is the pinned package_dir — the result's
        # own output_dir is the staging path, which no longer exists
        # after the atomic rename.
        self._last_output_dir = job.package_dir
        self.open_output_button.setVisible(True)
        try:
            stale = self.presentation_repository.session_stale(job.session)
        except Exception:
            stale = False
        lines = [
            f'生成完了: {job.package_dir}',
            f'エントリ {len(result.manifest.entries)} 件',
            f'マニフェスト SHA-256: {result.manifest.manifest_sha256}',
        ]
        capability_rows = getattr(result.manifest, 'capability_rows', None)
        if capability_rows:
            caps = '; '.join(
                f'{row.capability}={row.state}'
                for row in capability_rows
            )
            lines.append(f'能力宣言: {caps}')
        if stale:
            # #985: the pinned input is no longer the head — present the
            # package as a historical result, never a fresh one.
            lines.append(
                '注意: ピン留めされたリビジョンは最新ではありません'
                '（旧リビジョンの結果として保持）'
            )
        if result.warnings:
            lines.extend(result.warnings)
        self.export_status.setText('\n'.join(lines))

    def _export_job_failed(self, payload) -> None:
        _job, error_text, diagnostic_id = payload
        # Non-blocking notification: the ActivityCenter row carries the
        # FAILED state + diagnostic id; the status line mirrors it. No
        # partial output folder exists at this point (#985).
        self.export_status.setText(
            f'生成できませんでした: {error_text} '
            f'[diag: {diagnostic_id}]'
        )

    def _export_job_cancelled(self) -> None:
        self.export_status.setText(
            '出力を中止しました — 部分出力は残っていません'
        )

    def _export_job_finished(self) -> None:
        self.export_review_button.setEnabled(True)
        self.export_proposal_button.setEnabled(True)
        self.export_cancel_button.setVisible(False)

    def closeEvent(self, event) -> None:
        # An in-flight export must be cancelled and the worker lane
        # drained before the mount is disposed (project switch / close);
        # the runner's bounded shutdown also removes any staged output.
        self._export_runner.shutdown()
        super().closeEvent(event)

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
