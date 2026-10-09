"""#986: project list search/sort/filter + stable-ID selection.

The library table and the menu switch picker share one vocabulary
(``filter_project_entries``): name substring search, 最近使った順/作成日時/
名前 sort, 作業中/アーカイブ済み/全件 state filter. Selection is pinned to
``project_id`` — display-name collisions never move the operator to a
different project.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip('PySide6')

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from htdt.application_pages import (
    PROJECT_FILTER_ACTIVE,
    PROJECT_FILTER_ARCHIVED,
    PROJECT_FILTER_ALL,
    PROJECT_SORT_CREATED,
    PROJECT_SORT_NAME,
    PROJECT_SORT_RECENT,
    ProjectEntry,
    ProjectLibraryPage,
    filter_project_entries,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _entry(
    pid: str,
    name: str,
    *,
    created: str,
    opened: str | None = None,
    archived: bool = False,
    doc: str | None = None,
) -> ProjectEntry:
    return ProjectEntry(
        project_id=pid,
        document_id=doc or f'doc-{pid}',
        display_name=name,
        head_revision_id='rev-1',
        created_at_utc=created,
        revision_count=1,
        archived=archived,
        last_opened_at_utc=opened,
    )


def _entries() -> tuple[ProjectEntry, ...]:
    return (
        _entry('p-alpha', 'Alpha シアター', created='2026-01-01', opened='2026-02-01'),
        _entry('p-beta', 'Beta シアター', created='2026-01-05', opened=None),
        _entry('p-alpha2', 'Alpha シアター', created='2026-01-03', opened='2026-03-01'),
        _entry('p-old', '旧 Alpha', created='2025-12-01', opened='2025-12-15', archived=True),
    )


def test_search_filters_by_name_substring() -> None:
    hits = filter_project_entries(_entries(), text='beta')
    assert [e.project_id for e in hits] == ['p-beta']
    hits = filter_project_entries(_entries(), text='alpha')
    assert len(hits) == 3  # Alpha シアター x2 + 旧 Alpha


def test_state_filters() -> None:
    active = filter_project_entries(
        _entries(), state=PROJECT_FILTER_ACTIVE
    )
    assert all(not e.archived for e in active)
    assert len(active) == 3
    archived = filter_project_entries(
        _entries(), state=PROJECT_FILTER_ARCHIVED
    )
    assert [e.project_id for e in archived] == ['p-old']
    assert len(filter_project_entries(_entries(), state=PROJECT_FILTER_ALL)) == 4


def test_sort_orders() -> None:
    recent = filter_project_entries(_entries(), sort=PROJECT_SORT_RECENT)
    assert [e.project_id for e in recent] == [
        'p-alpha2', 'p-alpha', 'p-old', 'p-beta',
    ]  # opened desc, never-opened last
    created = filter_project_entries(_entries(), sort=PROJECT_SORT_CREATED)
    assert created[0].project_id == 'p-beta'
    by_name = filter_project_entries(_entries(), sort=PROJECT_SORT_NAME)
    # Same display name → stable project_id tiebreak, never arbitrary.
    alpha_rows = [e for e in by_name if e.display_name == 'Alpha シアター']
    assert [e.project_id for e in alpha_rows] == ['p-alpha', 'p-alpha2']


class _FakeService:
    def __init__(self, entries):
        self._entries = entries
        self.archived_calls = []

    def list_projects(self):
        return self._entries

    def set_archived(self, project_id, archived):
        self.archived_calls.append((project_id, archived))


def test_page_search_sort_filter_and_id_pinned_selection(
    tmp_path: Path,
) -> None:
    _app()
    service = _FakeService(_entries())
    page = ProjectLibraryPage(service, current_document_id=lambda: 'doc-p-alpha')
    try:
        # Baseline: all 4 rows, current marker on doc-p-alpha.
        assert page.table.rowCount() == 4
        current_rows = [
            r for r in range(4) if page.table.item(r, 3).text() == '●'
        ]
        assert len(current_rows) == 1

        # Search narrows to same-name pair — both stay visible, IDs distinct.
        page.search_edit.setText('Alpha シアター')
        assert page.table.rowCount() == 2
        ids = {
            page.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
            for r in range(2)
        }
        assert ids == {'p-alpha', 'p-alpha2'}

        # Pin selection to p-alpha2, then sort by name — selection follows
        # the project_id, not the row position.
        page.search_edit.setText('')
        for r in range(page.table.rowCount()):
            if page.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == 'p-alpha2':
                page.table.selectRow(r)
        assert page._selected_project_id() == 'p-alpha2'
        page.sort_combo.setCurrentIndex(2)  # 名前
        assert page._selected_project_id() == 'p-alpha2'

        # Archived filter shows only the archived row; selection does not
        # jump to a different project.
        page.state_combo.setCurrentIndex(2)  # アーカイブ済み
        assert page.table.rowCount() == 1
        assert page.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == 'p-old'
    finally:
        page.deleteLater()


def test_page_blocker_text_for_unopenable_selection(tmp_path: Path) -> None:
    _app()
    service = _FakeService(_entries())
    page = ProjectLibraryPage(service, current_document_id=lambda: 'doc-p-alpha')
    try:
        # Select the archived row → open disabled + reason shown.
        page.state_combo.setCurrentIndex(2)
        page.table.selectRow(0)
        assert not page.open_button.isEnabled()
        assert 'アーカイブ済み' in page.selection_status.text()

        # Select the current row → reason is "currently open".
        page.state_combo.setCurrentIndex(0)
        page.search_edit.setText('')
        for r in range(page.table.rowCount()):
            if page.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == 'p-alpha':
                page.table.selectRow(r)
        assert '現在開いています' in page.selection_status.text()
    finally:
        page.deleteLater()


def test_page_large_list_renders(tmp_path: Path) -> None:
    _app()
    entries = tuple(
        _entry(f'p-{i:04d}', f'プロジェクト {i:04d}', created='2026-01-01')
        for i in range(200)
    )
    service = _FakeService(entries)
    page = ProjectLibraryPage(service, current_document_id=lambda: 'doc-none')
    try:
        assert page.table.rowCount() == 200
        page.search_edit.setText('0199')
        assert page.table.rowCount() == 1
        assert page.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == 'p-0199'
    finally:
        page.deleteLater()
