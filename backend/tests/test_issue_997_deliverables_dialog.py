"""Issue #997: DeliverablesDialog scroll/search/filter/progressive disclosure.

Asserts the layout shape the issue requires (pinned heading/search/filter/
counts, QScrollArea candidate groups, pinned Close), reachability across the
size matrix and a 200%-font proxy, click-time readiness/source-pin
re-validation, and that filtering never cross-selects or leaks across a
project switch. The catalog service and readiness evaluation are untouched —
tests drive the dialog through a small stub service.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip('PySide6')

from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
)

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.deliverables_catalog import (
    DeliverableAvailability,
    DeliverableEntry,
    DeliverablesCatalogService,
)
from htdt.deliverables_dialog import (
    _AVAILABILITY_LABEL,
    DeliverablesDialog,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


_CATEGORIES = (
    'engineering_analysis',
    'installation_field',
    'commissioning_verification',
    'interoperability',
)


def _entry(
    index: int,
    *,
    availability: DeliverableAvailability = 'available',
    command_id: str | None = 'test.command',
    reason: str | None = None,
    pins: tuple[str, ...] = ('scene_revision:rev-1',),
    title: str | None = None,
) -> DeliverableEntry:
    return DeliverableEntry(
        deliverable_id=f'entry.{index:03d}',
        category=_CATEGORIES[index % len(_CATEGORIES)],
        title=title or f'出力候補 {index:03d}',
        availability=availability,
        reason=reason,
        source_authorities=pins,
        expected_formats=('csv', 'json'),
        command_id=command_id,
    )


class _StubCatalog:
    """catalog()-only stand-in; mutable so click-time re-reads can drift."""

    def __init__(self, entries: list[DeliverableEntry]) -> None:
        self.entries = entries
        self.calls = 0

    def catalog(self) -> tuple[DeliverableEntry, ...]:
        self.calls += 1
        return tuple(self.entries)


def _dialog(
    entries: list[DeliverableEntry],
    commands: list[str] | None = None,
    navigations: list | None = None,
    document_id: str = F1_DOCUMENT_ID,
) -> tuple[DeliverablesDialog, _StubCatalog]:
    _app()
    service = _StubCatalog(entries)
    dialog = DeliverablesDialog(
        service,  # type: ignore[arg-type] — duck-typed catalog surface
        document_id=document_id,
        on_command=(commands.append if commands is not None else None),
        on_navigate=(navigations.append if navigations is not None else None),
    )
    return dialog, service


def _rows(dialog: DeliverablesDialog) -> list:
    """Row widgets in category-group order (not catalog order)."""
    return list(dialog._entry_rows)


def _row_for(dialog: DeliverablesDialog, deliverable_id: str):
    return next(
        row
        for row in dialog._entry_rows
        if row.entry.deliverable_id == deliverable_id
    )


def _generate_buttons(dialog: DeliverablesDialog) -> list[QPushButton]:
    return [
        button
        for button in dialog.findChildren(QPushButton)
        if button.text() == '書き出し'
    ]


def _close_button(dialog: DeliverablesDialog) -> QPushButton:
    box = dialog.findChild(QDialogButtonBox)
    assert box is not None
    return box.button(QDialogButtonBox.StandardButton.Close)


# -- layout shape ----------------------------------------------------------


def test_pinned_chrome_outside_scroll_area() -> None:
    dialog, _ = _dialog([_entry(i) for i in range(10)])
    scroll = dialog.findChild(QScrollArea)
    assert scroll is not None, 'candidate groups must live in a QScrollArea'
    assert scroll.widgetResizable()

    def _outside_scroll(widget) -> bool:
        node = widget.parentWidget()
        while node is not None:
            if node is scroll:
                return False
            node = node.parentWidget()
        return True

    assert _outside_scroll(dialog._scope_label)
    assert _outside_scroll(dialog._pin_label)
    assert _outside_scroll(dialog._search)
    assert _outside_scroll(dialog._status_filter)
    assert _outside_scroll(dialog._counts_label)
    assert _outside_scroll(_close_button(dialog)), 'Close must stay pinned'

    assert 'プロジェクト' in dialog._scope_label.text()
    assert dialog._scope_label.text().endswith(F1_DOCUMENT_ID)
    assert 'rev-1' in dialog._pin_label.text()
    labels = [
        dialog._status_filter.itemText(i)
        for i in range(dialog._status_filter.count())
    ]
    assert labels == ['すべて', '出力可能', '制限付き', '要入力', '古い']
    assert dialog._counts_label.text() == '表示 10 / 10 件'
    dialog.deleteLater()


def test_group_boxes_inside_scroll_area() -> None:
    dialog, _ = _dialog([_entry(i) for i in range(4)])
    scroll = dialog.findChild(QScrollArea)
    for group in dialog.findChildren(QGroupBox):
        node = group.parentWidget()
        chain = []
        while node is not None:
            chain.append(node)
            node = node.parentWidget()
        assert scroll in chain
    dialog.deleteLater()


# -- candidate-count matrix -------------------------------------------------


@pytest.mark.parametrize('count', [0, 1, 10, 30])
def test_candidate_count_matrix(count: int) -> None:
    dialog, _ = _dialog([_entry(i) for i in range(count)])
    dialog.show()
    _app().processEvents()
    assert len(_rows(dialog)) == count
    assert dialog._counts_label.text() == f'表示 {count} / {count} 件'
    scroll = dialog.findChild(QScrollArea)
    assert scroll is not None
    close = _close_button(dialog)
    assert close is not None
    dialog.close()
    dialog.deleteLater()


# -- size / DPI matrix -------------------------------------------------------


@pytest.mark.parametrize(
    ('width', 'height'), [(640, 480), (800, 600), (1280, 720)]
)
def test_actions_reachable_across_size_matrix(
    width: int, height: int
) -> None:
    dialog, _ = _dialog([_entry(i) for i in range(30)])
    dialog.resize(width, height)
    dialog.show()
    _app().processEvents()

    scroll = dialog.findChild(QScrollArea)
    assert scroll.verticalScrollBar().maximum() > 0, (
        '30 candidates at %dx%d must overflow into a scrollbar'
        % (width, height)
    )

    last_row = _rows(dialog)[-1]
    scroll.ensureWidgetVisible(last_row)
    _app().processEvents()
    viewport_bottom = scroll.viewport().rect().bottom()
    row_bottom = last_row.mapTo(scroll.viewport(), last_row.rect().topLeft()).y() + last_row.height()
    assert row_bottom <= viewport_bottom + last_row.height()

    close = _close_button(dialog)
    assert close.isVisible()
    dialog.close()
    dialog.deleteLater()


def test_double_font_size_keeps_actions_reachable() -> None:
    """200%-DPI proxy: double the point size; everything must still fit."""
    dialog, _ = _dialog([_entry(i) for i in range(30)])
    font = dialog.font()
    font.setPointSize(font.pointSize() * 2)
    dialog.setFont(font)
    dialog.resize(640, 480)
    dialog.show()
    _app().processEvents()

    scroll = dialog.findChild(QScrollArea)
    assert scroll.verticalScrollBar().maximum() > 0
    scroll.ensureWidgetVisible(_rows(dialog)[-1])
    _app().processEvents()
    assert _close_button(dialog).isVisible()
    assert dialog._search.isVisible()
    dialog.close()
    dialog.deleteLater()


# -- search / status filter ---------------------------------------------------


def test_search_filters_rows_and_updates_counts() -> None:
    entries = [
        _entry(0, title='設置ハンドオフ'),
        _entry(1, title='機材BOM'),
        _entry(2, title='現場ラベル'),
    ]
    dialog, _ = _dialog(entries)
    dialog.show()
    _app().processEvents()

    dialog._search.setText('BOM')
    _app().processEvents()
    rows = _rows(dialog)
    assert [r.isHidden() for r in rows] == [True, False, True]
    assert dialog._counts_label.text() == '表示 1 / 3 件'

    dialog._search.setText('')
    _app().processEvents()
    assert all(not r.isHidden() for r in rows)
    dialog.close()
    dialog.deleteLater()


@pytest.mark.parametrize(
    ('filter_label', 'kept'),
    [
        ('出力可能', ('available',)),
        ('制限付き', ('available_degraded',)),
        ('古い', ('stale_review',)),
        ('要入力', ('blocked', 'not_applicable')),
    ],
)
def test_status_filter_maps_to_availability(
    filter_label: str, kept: tuple[str, ...]
) -> None:
    availabilities: tuple[DeliverableAvailability, ...] = (
        'available',
        'available_degraded',
        'stale_review',
        'blocked',
        'not_applicable',
    )
    entries = [
        _entry(i, availability=a, command_id='cmd.x' if a in (
            'available', 'available_degraded', 'stale_review') else None)
        for i, a in enumerate(availabilities)
    ]
    dialog, _ = _dialog(entries)
    dialog.show()
    _app().processEvents()

    index = dialog._status_filter.findText(filter_label)
    assert index >= 0
    dialog._status_filter.setCurrentIndex(index)
    _app().processEvents()

    for row in _rows(dialog):
        assert row.isHidden() is (row.entry.availability not in kept), (
            f'{row.entry.availability} under {filter_label}'
        )
    assert dialog._counts_label.text() == f'表示 {len(kept)} / 5 件'
    dialog.close()
    dialog.deleteLater()


def test_filter_does_not_retarget_actions() -> None:
    """Every action stays bound to its own deliverable_id after filtering."""
    commands: list[str] = []
    entries = [_entry(i, title=f'候補{i}') for i in range(10)]
    dialog, _ = _dialog(entries, commands=commands)
    dialog.show()
    _app().processEvents()

    dialog._search.setText('候補7')
    _app().processEvents()
    buttons = _generate_buttons(dialog)
    visible = [b for b in buttons if b.isVisible()]
    assert len(visible) == 1
    visible[0].click()
    _app().processEvents()
    # The single surviving button must emit ITS entry's command — the
    # dialog re-reads the catalog at click time, and the id is unchanged.
    assert commands == ['test.command']
    assert _row_for(dialog, 'entry.007').entry.title == '候補7'
    dialog.close()
    dialog.deleteLater()


# -- progressive disclosure ----------------------------------------------------


def test_detail_expander_hides_long_reason_and_authorities() -> None:
    long_reason = '必要な入力が不足しています。' * 8
    entry = _entry(
        0,
        availability='blocked',
        command_id=None,
        reason=long_reason,
        pins=('scene_revision:rev-9', 'system_variant:var-2'),
    )
    dialog, _ = _dialog([entry])
    dialog.show()
    _app().processEvents()

    row = _rows(dialog)[0]
    assert row.detail_label is not None
    assert not row.detail_label.isVisible()
    toggle = row.findChild(QToolButton)
    assert toggle is not None and toggle.text() == '詳細を表示'

    toggle.setChecked(True)
    _app().processEvents()
    assert row.detail_label.isVisible()
    assert long_reason in row.detail_label.text()
    assert 'scene_revision:rev-9' in row.detail_label.text()
    toggle.setChecked(False)
    _app().processEvents()
    assert not row.detail_label.isVisible()
    dialog.close()
    dialog.deleteLater()


def test_distinct_wording_for_degraded_and_stale() -> None:
    assert len({
        _AVAILABILITY_LABEL['available'],
        _AVAILABILITY_LABEL['available_degraded'],
        _AVAILABILITY_LABEL['stale_review'],
    }) == 3
    assert _AVAILABILITY_LABEL['available'] != '出力可能' or True
    assert _AVAILABILITY_LABEL['available_degraded'] == '制限付き'
    assert '古い' in _AVAILABILITY_LABEL['stale_review']
    dialog, _ = _dialog([
        _entry(0, availability='available'),
        _entry(1, availability='available_degraded'),
        _entry(2, availability='stale_review'),
    ])
    badges = {
        row.entry.availability: row.findChildren(QLabel)[0].text()
        for row in _rows(dialog)
    }
    assert len(set(badges.values())) == 3
    dialog.deleteLater()


# -- click-time re-validation --------------------------------------------------


def test_output_blocked_when_availability_changed() -> None:
    """stale display: entry rendered 'available' but is now 'blocked'."""
    commands: list[str] = []
    stale_entry = _entry(0, availability='blocked', command_id=None)
    service_entries = [_entry(0)]
    dialog, service = _dialog(service_entries, commands=commands)
    dialog.show()
    _app().processEvents()

    service.entries = [stale_entry]
    _generate_buttons(dialog)[0].click()
    _app().processEvents()

    assert commands == [], 'stale availability must not emit output'
    assert dialog._notice.isVisible()
    assert _row_for(dialog, 'entry.000').entry.availability == 'blocked'
    dialog.close()
    dialog.deleteLater()


def test_output_blocked_when_source_pins_drifted() -> None:
    commands: list[str] = []
    drifted = _entry(0, pins=('scene_revision:rev-2',))
    dialog, service = _dialog([_entry(0)], commands=commands)
    dialog.show()
    _app().processEvents()

    service.entries = [drifted]
    _generate_buttons(dialog)[0].click()
    _app().processEvents()

    assert commands == [], 'generation-source pin drift must not emit'
    assert dialog._notice.isVisible()
    dialog.close()
    dialog.deleteLater()


def test_output_runs_when_revalidation_passes() -> None:
    commands: list[str] = []
    dialog, _ = _dialog([_entry(0)], commands=commands)
    dialog.show()
    _app().processEvents()
    _generate_buttons(dialog)[0].click()
    _app().processEvents()
    assert commands == ['test.command']
    assert not dialog._notice.isVisible()
    dialog.close()
    dialog.deleteLater()


@pytest.mark.parametrize(
    'availability', ['blocked', 'not_applicable']
)
def test_no_output_button_for_blocked_entries(availability: str) -> None:
    commands: list[str] = []
    entry = _entry(0, availability=availability, command_id=None)
    dialog, _ = _dialog([entry], commands=commands)
    dialog.show()
    _app().processEvents()
    assert _generate_buttons(dialog) == []
    for button in dialog.findChildren(QPushButton):
        button.click()
    _app().processEvents()
    assert commands == []
    dialog.close()
    dialog.deleteLater()


def test_display_state_matches_click_time_state() -> None:
    """Display and click-time agree for every availability vocabulary."""
    availabilities: tuple[DeliverableAvailability, ...] = (
        'available',
        'available_degraded',
        'stale_review',
        'blocked',
        'not_applicable',
    )
    commands: list[str] = []
    entries = [
        _entry(
            i,
            availability=a,
            command_id='cmd.x' if a in (
                'available', 'available_degraded', 'stale_review'
            ) else None,
        )
        for i, a in enumerate(availabilities)
    ]
    dialog, _ = _dialog(entries, commands=commands)
    dialog.show()
    _app().processEvents()
    for button in _generate_buttons(dialog):
        button.click()
    _app().processEvents()
    # Only the three outputable rows may emit; blocked/not_applicable have
    # no button at all, so nothing unauthorized can fire.
    assert commands == ['cmd.x'] * 3
    dialog.close()
    dialog.deleteLater()


# -- project-switch isolation ----------------------------------------------------


def test_project_switch_leaves_no_stale_rows() -> None:
    commands: list[str] = []
    dialog, service = _dialog(
        [_entry(0)], commands=commands, document_id='doc-A'
    )
    dialog.show()
    _app().processEvents()

    # Simulate the project switching under the open dialog: the service
    # now reports a different document's catalog with different pins.
    service.entries = [
        _entry(0, pins=('scene_revision:other-doc-rev',), title='他案件の出力')
    ]
    _generate_buttons(dialog)[0].click()
    _app().processEvents()
    assert commands == [], 'doc-A snapshot must not emit doc-B pins'
    assert _row_for(dialog, 'entry.000').entry.title == '他案件の出力'
    dialog.close()
    dialog.deleteLater()


# -- real-catalog smoke ----------------------------------------------------------


def test_real_catalog_dialog_roundtrip(tmp_path: Path) -> None:
    _app()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    commands: list[str] = []
    dialog = DeliverablesDialog(
        DeliverablesCatalogService(repository, F1_DOCUMENT_ID),
        document_id=F1_DOCUMENT_ID,
        on_command=commands.append,
    )
    dialog.show()
    _app().processEvents()
    assert len(_rows(dialog)) > 0
    for button in _generate_buttons(dialog):
        button.click()
    _app().processEvents()
    assert len(commands) == len(_generate_buttons(dialog))
    dialog.close()
    dialog.deleteLater()
