"""Project Deliverables Center dialog (#900).

Thin presentation surface over :class:`DeliverablesCatalogService`: the
project name/scope stays visible, every deliverable row shows its
availability and missing input, and generation routes to the existing
commands — never to a new output authority.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .deliverables_catalog import (
    DeliverableAvailability,
    DeliverableEntry,
    DeliverablesCatalogService,
)
from .ui_theme import TypographyRole, set_typography_role
from .workflow_navigation import WorkspaceDeepLink


_AVAILABILITY_LABEL: dict[DeliverableAvailability, str] = {
    'available': '利用可能',
    'available_degraded': '一部未確定',
    'stale_review': '要レビュー',
    'blocked': 'ブロック',
    'not_applicable': '対象外',
}

_CATEGORY_TITLES = {
    'engineering_analysis': 'エンジニアリング解析',
    'installation_field': '設置・現場デリバラブル',
    'commissioning_verification': 'コミッショニング・検証',
    'interoperability': '相互運用',
}


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
        self._on_command = on_command or (lambda command_id: None)
        self._on_navigate = on_navigate or (lambda link: None)

        self.setWindowTitle('プロジェクト デリバラブル')
        self.resize(640, 520)
        layout = QVBoxLayout(self)

        # Project identity is always visible (#900-2) — the surface never
        # silently exports whichever project happens to be active.
        scope = QLabel(f'プロジェクト: {document_id}')
        set_typography_role(scope, TypographyRole.SECTION_TITLE)
        layout.addWidget(scope)

        by_category: dict[str, list[DeliverableEntry]] = {}
        for entry in self._service.catalog():
            by_category.setdefault(entry.category, []).append(entry)
        for category, title in _CATEGORY_TITLES.items():
            entries = by_category.get(category)
            if not entries:
                continue
            group = QGroupBox(title)
            group_layout = QVBoxLayout(group)
            for entry in entries:
                group_layout.addLayout(self._entry_row(entry))
            layout.addWidget(group)
        layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close, parent=self
        )
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _entry_row(self, entry: DeliverableEntry) -> QHBoxLayout:
        row = QHBoxLayout()
        badge = QLabel(_AVAILABILITY_LABEL[entry.availability])
        badge.setMinimumWidth(90)
        row.addWidget(badge)
        detail = entry.title
        if entry.reason:
            detail += f' — {entry.reason}'
        if entry.source_authorities:
            detail += (
                f' [{", ".join(entry.source_authorities)}]'
            )
        label = QLabel(detail)
        label.setWordWrap(True)
        row.addWidget(label, 1)
        if (
            entry.command_id is not None
            and entry.availability
            in ('available', 'available_degraded', 'stale_review')
        ):
            generate = QPushButton('書き出し')
            command_id = entry.command_id
            generate.clicked.connect(
                lambda _checked=False, cid=command_id: self._on_command(cid)
            )
            row.addWidget(generate)
        elif entry.action is not None:
            open_input = QPushButton('開く')
            link = entry.action
            open_input.clicked.connect(
                lambda _checked=False, target=link: self._on_navigate(target)
            )
            row.addWidget(open_input)
        return row


__all__ = ['DeliverablesDialog']
