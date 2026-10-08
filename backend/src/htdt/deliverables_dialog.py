"""Project Deliverables Center dialog (#900, #997).

Thin presentation surface over :class:`DeliverablesCatalogService`: the
project name/scope stays visible, every deliverable row shows its
availability and missing input, and generation routes to the existing
commands — never to a new output authority.

Layout (#997): the project heading, revision pin, search field, status
filter and row counts stay pinned at the top; the candidate groups live
inside a ``QScrollArea`` so every action stays reachable at small window
heights and 125–200% DPI; the Close button stays pinned at the bottom.
Long reasons and referenced authorities collapse behind a
「詳細を表示」 expander per row, and every output action re-validates the
entry's availability and generation source pins against a freshly read
catalog immediately before running — a stale display can never emit an
unauthorized or outdated output.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .deliverables_catalog import (
    DeliverableAvailability,
    DeliverableEntry,
    DeliverablesCatalogService,
)
from .ui_theme import (
    SemanticState,
    TypographyRole,
    set_semantic_state,
    set_typography_role,
)
from .workflow_navigation import WorkspaceDeepLink


# Badge wording mirrors the filter vocabulary (#997): degraded and
# stale-review rows are never conflated with plain "ready" rows.
_AVAILABILITY_LABEL: dict[DeliverableAvailability, str] = {
    'available': '出力可能',
    'available_degraded': '制限付き',
    'stale_review': '古い（要レビュー）',
    'blocked': '要入力',
    'not_applicable': '対象外',
}

_AVAILABILITY_TIP: dict[DeliverableAvailability, str] = {
    'available': '必要な入力がすべて揃っています — そのまま書き出せます',
    'available_degraded': '一部の入力が未確定です — 暫定値で書き出せますが確認が推奨されます',
    'stale_review': '入力が古くなっています — 書き出す前に最新の状態へのレビューが推奨されます',
    'blocked': '必須の入力が不足しています — 「開く」から対象ワークスペースで用意してください',
    'not_applicable': 'このプロジェクト構成では対象外です',
}

_CATEGORY_TITLES = {
    'engineering_analysis': 'エンジニアリング解析',
    'installation_field': '設置・現場デリバラブル',
    'commissioning_verification': 'コミッショニング・検証',
    'interoperability': '相互運用',
}

# Status filter (issue #997): 出力可能 / 制限付き / 要入力 / 古い.
_STATUS_FILTER_CHOICES: tuple[tuple[str, str], ...] = (
    ('all', 'すべて'),
    ('ready', '出力可能'),
    ('degraded', '制限付き'),
    ('input', '要入力'),
    ('stale', '古い'),
)
_STATUS_FILTER_AVAILABILITY: dict[str, tuple[DeliverableAvailability, ...]] = {
    'ready': ('available',),
    'degraded': ('available_degraded',),
    'input': ('blocked', 'not_applicable'),
    'stale': ('stale_review',),
}

# Availabilities whose command may run — re-checked live at click time.
_OUTPUTABLE: tuple[DeliverableAvailability, ...] = (
    'available',
    'available_degraded',
    'stale_review',
)

_SUMMARY_REASON_LIMIT = 60


def _summary_reason(reason: str | None) -> str:
    """First sentence of a missing/degraded reason, capped for one line."""
    if not reason:
        return ''
    first = reason.split('。', 1)[0].strip()
    if len(reason) > len(first):
        first += '。'
    if len(first) > _SUMMARY_REASON_LIMIT:
        first = first[: _SUMMARY_REASON_LIMIT - 1] + '…'
    return first


class _EntryRow(QWidget):
    """One deliverable row: state badge, summary, disclosure, action.

    The row captures its :class:`DeliverableEntry` snapshot; the action
    button always carries the same ``deliverable_id`` it was rendered
    with, and the owning dialog re-validates that id against a fresh
    catalog before any output command runs.
    """

    def __init__(
        self,
        entry: DeliverableEntry,
        dialog: 'DeliverablesDialog',
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.entry = entry

        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)

        badge = QLabel(_AVAILABILITY_LABEL[entry.availability])
        badge.setToolTip(_AVAILABILITY_TIP[entry.availability])
        badge.setMinimumWidth(96)
        row.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        center = QVBoxLayout()
        center.setContentsMargins(0, 0, 0, 0)
        summary_text = entry.title
        reason_summary = _summary_reason(entry.reason)
        if reason_summary:
            summary_text += f' — {reason_summary}'
        summary = QLabel(summary_text)
        summary.setWordWrap(True)
        summary.setAccessibleName(f'デリバラブル {entry.deliverable_id}')
        center.addWidget(summary)

        detail_lines: list[str] = []
        if entry.reason:
            detail_lines.append(entry.reason)
        if entry.source_authorities:
            detail_lines.append(
                '権威: ' + ', '.join(entry.source_authorities)
            )
        if entry.expected_formats:
            detail_lines.append(
                '形式: ' + ' / '.join(entry.expected_formats)
            )
        self.detail_label: QLabel | None = None
        if detail_lines:
            detail = QLabel('\n'.join(detail_lines))
            detail.setWordWrap(True)
            detail.setVisible(False)
            set_typography_role(detail, TypographyRole.SECONDARY)
            center.addWidget(detail)
            self.detail_label = detail
        row.addLayout(center, 1)

        if self.detail_label is not None:
            toggle = QToolButton(self)
            toggle.setText('詳細を表示')
            toggle.setAccessibleName(f'詳細を表示 {entry.deliverable_id}')
            toggle.setCheckable(True)
            detail_label = self.detail_label
            toggle.toggled.connect(
                lambda checked, label=detail_label, button=toggle: (
                    label.setVisible(checked),
                    button.setText('詳細を隠す' if checked else '詳細を表示'),
                )
            )
            row.addWidget(toggle, 0, Qt.AlignmentFlag.AlignTop)

        if (
            entry.command_id is not None
            and entry.availability in _OUTPUTABLE
        ):
            generate = QPushButton('書き出し')
            generate.setToolTip(f'{entry.title} を生成・書き出します')
            generate.setAccessibleName(f'書き出し {entry.deliverable_id}')
            generate.clicked.connect(
                lambda _checked=False, e=entry: dialog._execute_output(e)
            )
            row.addWidget(generate, 0, Qt.AlignmentFlag.AlignTop)
        elif entry.action is not None:
            open_input = QPushButton('開く')
            open_input.setToolTip('不足している入力を用意できるワークスペースを開きます')
            open_input.setAccessibleName(f'開く {entry.deliverable_id}')
            link = entry.action
            open_input.clicked.connect(
                lambda _checked=False, target=link: dialog._open_input(target)
            )
            row.addWidget(open_input, 0, Qt.AlignmentFlag.AlignTop)


class DeliverablesDialog(QDialog):
    """What this project can generate, from which authority, and what's missing."""

    def __init__(
        self,
        catalog_service: DeliverablesCatalogService,
        *,
        document_id: str,
        on_command: Callable[[str], None] | None = None,
        on_navigate: Callable[[WorkspaceDeepLink], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = catalog_service
        self._document_id = document_id
        self._on_command = on_command or (lambda command_id: None)
        self._on_navigate = on_navigate or (lambda link: None)
        self._entry_rows: list[_EntryRow] = []

        self.setWindowTitle('プロジェクトデリバラブル')
        self.resize(680, 560)
        self.setMinimumSize(420, 320)
        layout = QVBoxLayout(self)

        # --- Pinned header: identity + revision pin + search + filter.
        # Project identity is always visible (#900-2) — the surface never
        # silently exports whichever project happens to be active.
        self._scope_label = QLabel()
        set_typography_role(self._scope_label, TypographyRole.SECTION_TITLE)
        layout.addWidget(self._scope_label)

        self._pin_label = QLabel()
        self._pin_label.setWordWrap(True)
        set_typography_role(self._pin_label, TypographyRole.SECONDARY)
        layout.addWidget(self._pin_label)

        controls = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText('タイトル・理由で絞り込み')
        self._search.setClearButtonEnabled(True)
        self._search.setAccessibleName('デリバラブル検索')
        self._search.textChanged.connect(self._apply_filters)
        controls.addWidget(self._search, 1)

        self._status_filter = QComboBox()
        self._status_filter.setAccessibleName('状態フィルタ')
        for key, label in _STATUS_FILTER_CHOICES:
            self._status_filter.addItem(label, key)
        self._status_filter.currentIndexChanged.connect(self._apply_filters)
        controls.addWidget(self._status_filter)

        self._counts_label = QLabel()
        self._counts_label.setAccessibleName('件数')
        controls.addWidget(self._counts_label)
        layout.addLayout(controls)

        # --- Scrollable candidate groups (#997).
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setAccessibleName('出力候補一覧')
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._scroll.setWidget(self._body)
        layout.addWidget(self._scroll, 1)

        # --- Pinned notice + Close: a blocked re-validation lands here
        # instead of a modal box, so the surface itself stays driveable.
        self._notice = QLabel()
        self._notice.setWordWrap(True)
        set_semantic_state(self._notice, SemanticState.WARNING)
        self._notice.setAccessibleName('出力検証メッセージ')
        self._notice.hide()
        layout.addWidget(self._notice)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close, parent=self
        )
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._scope_label.setText(f'プロジェクト: {document_id}')
        self._rebuild()

    # -- content -------------------------------------------------------

    def _rebuild(self) -> None:
        """Re-read the catalog and rebuild every row from live authority."""
        entries = list(self._service.catalog())
        self._pin_label.setText(
            '現在のリビジョン: ' + self._revision_pin_text(entries)
        )
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._entry_rows = []

        by_category: dict[str, list[DeliverableEntry]] = {}
        for entry in entries:
            by_category.setdefault(entry.category, []).append(entry)
        for category, title in _CATEGORY_TITLES.items():
            group_entries = by_category.get(category)
            if not group_entries:
                continue
            group = QGroupBox(title)
            group_layout = QVBoxLayout(group)
            for entry in group_entries:
                row_widget = _EntryRow(entry, self, group)
                self._entry_rows.append(row_widget)
                group_layout.addWidget(row_widget)
            self._body_layout.addWidget(group)
        self._body_layout.addStretch(1)
        self._apply_filters()

    @staticmethod
    def _revision_pin_text(entries: list[DeliverableEntry]) -> str:
        for entry in entries:
            for pin in entry.source_authorities:
                if pin.startswith('scene_revision:'):
                    return pin.split(':', 1)[1]
        return '未保存'

    # -- filtering ------------------------------------------------------

    def _status_key(self) -> str:
        data = self._status_filter.currentData()
        return data if isinstance(data, str) else 'all'

    def _matches(self, row: _EntryRow) -> bool:
        key = self._status_key()
        if key != 'all' and (
            row.entry.availability not in _STATUS_FILTER_AVAILABILITY[key]
        ):
            return False
        query = self._search.text().strip().lower()
        if query:
            haystack = ' '.join(
                part
                for part in (
                    row.entry.title,
                    row.entry.reason or '',
                    row.entry.deliverable_id,
                )
            ).lower()
            if query not in haystack:
                return False
        return True

    def _apply_filters(self, *_args: object) -> None:
        """Show/hide rows; actions stay bound to their own entry id, so
        filtering can never cross-select or re-target an output."""
        visible = 0
        for row in self._entry_rows:
            show = self._matches(row)
            row.setVisible(show)
            visible += int(show)
        for index in range(self._body_layout.count()):
            group = self._body_layout.itemAt(index).widget()
            if isinstance(group, QGroupBox):
                has_visible_row = any(
                    row.parentWidget() is group and not row.isHidden()
                    for row in self._entry_rows
                )
                group.setVisible(has_visible_row)
        total = len(self._entry_rows)
        self._counts_label.setText(f'表示 {visible} / {total} 件')

    # -- actions --------------------------------------------------------

    def _open_input(self, link: WorkspaceDeepLink) -> None:
        self._on_navigate(link)

    def _execute_output(self, entry: DeliverableEntry) -> None:
        """Re-validate the entry against a fresh catalog, then run it.

        The displayed row is a snapshot; before any output runs we
        re-derive the catalog and require that the same ``deliverable_id``
        still carries the same command, a still-outputable availability
        and identical generation source pins — otherwise the click could
        emit stale or unauthorized data, so the surface refreshes instead.
        """
        fresh = {
            fresh_entry.deliverable_id: fresh_entry
            for fresh_entry in self._service.catalog()
        }
        current = fresh.get(entry.deliverable_id)
        if (
            current is None
            or current.command_id != entry.command_id
            or current.availability not in _OUTPUTABLE
            or current.source_authorities != entry.source_authorities
        ):
            self._notice.setText(
                f'「{entry.title}」の状態または生成元が更新されました — '
                '古い内容での書き出しを止め、一覧を最新にしました。'
            )
            self._notice.show()
            self._rebuild()
            return
        self._on_command(entry.command_id)


__all__ = ['DeliverablesDialog']
