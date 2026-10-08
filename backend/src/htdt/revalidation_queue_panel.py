"""Revalidation queue panel — the #964 invalidation surface.

Renders the newest sealed :class:`RevalidationQueue` for the document —
the pinned diff it was built from, each item's stale/uncertain verdict,
its action and the surface the next single operation lives on — plus the
newest run verdict. Two operations:

* 「影響を再評価」 re-composes the queue from the caller-supplied
  ``queue_supplier`` (workspace gathers revisions/artifacts/edges);
* 「変更の影響を検証」 runs ``run_queue_software`` — software items execute
  through the wired ``software_runner`` in one operation, physical items
  always stop for human confirmation and any head-pin drift aborts the
  run without touching old results.

Route buttons navigate to the surface owning each item's next step
(measurement position plan, commissioning authorization, prediction
recompute, evidence review) via the caller's ``on_navigate`` — the panel
never starts hardware I/O or production applies itself.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .cad_evidence_invalidation import (
    RevalidationQueue,
    RevalidationQueueBundle,
    RevalidationQueueRun,
    SoftwareRunner,
    run_queue_software,
)
from .cad_evidence_invalidation_repository import (
    CadEvidenceInvalidationRepository,
)
from .cad_repository import SceneRepository
from .measurement_evidence_display import (
    queue_item_status_label,
    queue_route_label,
    queue_run_verdict_label,
    revalidation_queue_item_line,
)
from .ui_theme import (
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_primary_action,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)
from .user_facing_error import operation_error_message


class RevalidationQueuePanel(QFrame):
    """Comparison-page surface for the sealed revalidation queue."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
        *,
        revalidation_repository: CadEvidenceInvalidationRepository | None = None,
        queue_supplier: 'Callable[[], RevalidationQueueBundle | None] | None' = None,
        software_runner: SoftwareRunner | None = None,
        on_navigate: 'Callable[[str], None] | None' = None,
        on_status: 'Callable[[str], None] | None' = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._scene_repository = scene_repository
        self._document_id = document_id
        self._revalidation_repository = (
            revalidation_repository
            or CadEvidenceInvalidationRepository(scene_repository)
        )
        self._queue_supplier = queue_supplier
        self._software_runner = software_runner
        self._on_navigate = on_navigate
        self._on_status = on_status
        set_surface_role(self, SurfaceRole.RAISED)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        title = QLabel('再検証キュー')
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        description = QLabel(
            'シーン変更の差分から、陳腐化した証拠と次の一手を決定的に'
            '列挙します。影響範囲が確定できない項目は常にレビュー対象'
            'です。物理的な操作（再測定・再試運転）はこの画面から'
            '自動では開始されず、各項目の確認画面へ進みます。'
        )
        description.setWordWrap(True)
        set_typography_role(description, TypographyRole.SECONDARY)
        layout.addWidget(description)

        self.freshness_label = QLabel()
        self.freshness_label.setWordWrap(True)
        layout.addWidget(self.freshness_label)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.items_container = QWidget()
        self._items_layout = QVBoxLayout(self.items_container)
        self._items_layout.setContentsMargins(0, 0, 0, 0)
        self._items_layout.setSpacing(4)
        layout.addWidget(self.items_container)

        self.run_label = QLabel()
        self.run_label.setWordWrap(True)
        layout.addWidget(self.run_label)

        self.routes_container = QWidget()
        self._routes_layout = QHBoxLayout(self.routes_container)
        self._routes_layout.setContentsMargins(0, 0, 0, 0)
        self._routes_layout.setSpacing(6)
        layout.addWidget(self.routes_container)

        controls = QHBoxLayout()
        self.compose_button = QPushButton('影響を再評価')
        self.compose_button.setToolTip(
            '現在のシーン版と直前の版の差分を評価し、再検証キューを'
            '決定的に再構成して保存します。同じ差分の再評価は同じ'
            '記録を再利用し、重複は作りません。'
        )
        self.compose_button.setAccessibleName('影響を再評価')
        self.compose_button.clicked.connect(self._compose)
        self.compose_button.setEnabled(self._queue_supplier is not None)
        controls.addWidget(self.compose_button)

        self.verify_button = QPushButton('変更の影響を検証')
        self.verify_button.setToolTip(
            'キュー内のソフトウェア処理（再評価・再読み込み等）を一操作で'
            '順次実行し、結果を封印記録として保存します。実行前後で'
            'シーンの版とハッシュを照合し、処理中の変更があれば中止'
            'します。物理操作は実行されません。'
        )
        self.verify_button.setAccessibleName('変更の影響を検証')
        set_primary_action(self.verify_button)
        self.verify_button.clicked.connect(self._verify)
        controls.addWidget(self.verify_button)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        controls.addWidget(self.status_label, 1)
        layout.addLayout(controls)

        self._queue: RevalidationQueue | None = None
        self.refresh()

    # -- helpers --------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        if self._on_status is not None:
            self._on_status(text)

    def _clear(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _head(self):
        return self._scene_repository.current_head(self._document_id)

    def _latest_run(self) -> RevalidationQueueRun | None:
        if self._queue is None:
            return None
        return self._revalidation_repository.latest_run_for_queue(
            self._queue.queue_id
        )

    # -- rendering ------------------------------------------------------

    def refresh(self) -> None:
        """Reload the newest persisted queue + run for this document."""
        try:  # error-boundary: repository read must never crash the page
            self._queue = self._revalidation_repository.latest_queue(
                self._document_id
            )
            run = self._latest_run()
            head = self._head()
        except Exception as exc:  # noqa: BLE001
            self._queue = None
            run = None
            head = None
            self.freshness_label.setText(
                f'再検証キューを読み込めません: '
                f'{operation_error_message(exc)}'
            )
        else:
            self._render_freshness(head)
            self._render_summary()
            self._render_items()
            self._render_run(run)
        self.verify_button.setEnabled(
            self._queue is not None and bool(self._queue.items)
        )

    def _render_freshness(self, head) -> None:
        if self._queue is None:
            self.freshness_label.setText(
                'まだ再検証キューはありません。「影響を再評価」で'
                '最新の変更から作成します。'
            )
            return
        if head is None:
            self.freshness_label.setText(
                '現在のシーンリビジョンを解決できません。'
            )
            set_semantic_state(self.freshness_label, SemanticState.WARNING)
            return
        if (
            head.revision_id == self._queue.to_revision_id
            and head.content_hash == self._queue.to_content_hash
        ):
            self.freshness_label.setText(
                '現在のシーン版に対するキューです。'
            )
            set_semantic_state(self.freshness_label, SemanticState.SUCCESS)
        else:
            self.freshness_label.setText(
                'その後の変更があるため、このキューは現在の版より'
                '古い差分に基づいています。「影響を再評価」で更新して'
                'ください。'
            )
            set_semantic_state(self.freshness_label, SemanticState.WARNING)

    def _render_summary(self) -> None:
        if self._queue is None:
            self.summary_label.setText('')
            return
        queue = self._queue
        software = sum(
            1 for i in queue.items if i.execution_class == 'software'
        )
        physical = sum(
            1 for i in queue.items if i.execution_class == 'physical'
        )
        review = sum(
            1 for i in queue.items if i.execution_class == 'review'
        )
        self.summary_label.setText(
            f'差分 {queue.diff_ref.ref_id} → '
            f'対象版 {queue.to_revision_id[:16]}… — '
            f'項目 {len(queue.items)}件'
            f'（ソフトウェア {software} / 物理 {physical} / '
            f'レビュー {review}）'
        )

    def _render_items(self) -> None:
        self._clear(self._items_layout)
        self._clear(self._routes_layout)
        if self._queue is None or not self._queue.items:
            return
        routes: list[str] = []
        for item in self._queue.items:
            label = QLabel(revalidation_queue_item_line(item))
            label.setWordWrap(True)
            self._items_layout.addWidget(label)
            if item.route not in routes:
                routes.append(item.route)
        for route in routes:
            button = QPushButton(queue_route_label(route))
            button.setAccessibleName(queue_route_label(route))
            button.clicked.connect(
                lambda checked=False, r=route: self._navigate(r)
            )
            self._routes_layout.addWidget(button)
        self._routes_layout.addStretch(1)

    def _render_run(self, run: RevalidationQueueRun | None) -> None:
        if run is None:
            self.run_label.setText('')
            return
        parts = [
            f'実行結果: {queue_run_verdict_label(run.verdict)}',
        ]
        counts: dict[str, int] = {}
        for outcome in run.outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        if counts:
            parts.append(
                '（'
                + ' / '.join(
                    f'{queue_item_status_label(status)} {count}件'
                    for status, count in sorted(counts.items())
                )
                + '）'
            )
        self.run_label.setText(''.join(parts))
        set_semantic_state(
            self.run_label,
            SemanticState.WARNING
            if run.verdict in ('drift_detected', 'failed')
            else SemanticState.SUCCESS,
        )

    def _navigate(self, route: str) -> None:
        if self._on_navigate is not None:
            self._on_navigate(route)

    # -- operations -----------------------------------------------------

    def _compose(self) -> None:
        if self._queue_supplier is None:
            return
        try:  # error-boundary: compose must report, not crash the page
            bundle = self._queue_supplier()
        except Exception as exc:  # noqa: BLE001
            self._set_status(
                f'再検証キューを作成できません: '
                f'{operation_error_message(exc)}'
            )
            return
        if bundle is None:
            self._set_status(
                '比較できるシーンリビジョンが2件必要です'
            )
        else:
            self._set_status(
                f'再検証キューを保存しました（項目 '
                f'{len(bundle.queue.items)}件）'
            )
        self.refresh()

    def _verify(self) -> None:
        if self._queue is None:
            return
        try:  # error-boundary: the run must report, not crash the page
            pre_head = self._head()
            run = run_queue_software(
                self._queue,
                pre_head_revision=pre_head,
                post_head_revision=self._head(),
                software_runner=self._software_runner,
                revalidation_repository=self._revalidation_repository,
            )
        except Exception as exc:  # noqa: BLE001
            self._set_status(
                f'影響の検証を実行できません: '
                f'{operation_error_message(exc)}'
            )
            return
        self._set_status(
            f'影響の検証を記録しました: '
            f'{queue_run_verdict_label(run.verdict)}'
        )
        self.refresh()


__all__ = ['RevalidationQueuePanel']
