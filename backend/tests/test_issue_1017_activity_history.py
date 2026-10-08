"""Issue #1017 residual: arbitrary range, cursor paging, detail inspector.

On top of #1023 (scope SQL filter, preset 期間, search, numbered paging)
this round adds the remaining contract: operator-entered start/end date
bounds, さらに読み込む load-more walking into arbitrarily deep history via
a (occurred_at_utc, event_id) cursor for the timeline and a SQL keyset
``seq`` cursor for the revision ledger, and a read-only detail inspector
that types each event as evidence / assumption / historical / failed /
note — with メモ presented as documentation, never canonical evidence.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QAbstractItemView, QApplication

from htdt.application_pages import (
    ActivityPage,
    count_recent_revisions,
    event_evidence_class,
    list_known_document_ids,
    list_revisions_page,
    activity_focus,
)
from htdt.cad_project_activity import (
    ActivitySourceRef,
    CadProjectActivityService,
    _event,
)
from htdt.cad_project_activity_repository import (
    CadProjectActivityNoteRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import RoomPrism, make_empty_scene
from htdt.navigation_target import (
    NavigationTarget,
    NavigationTargetKind,
    navigation_target_from_uri,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _repository(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    repository.save(make_empty_scene("doc-alpha"), parent_revision_id=None)
    repository.save(make_empty_scene("doc-beta"), parent_revision_id=None)
    return repository


def _save_revisions(
    repository: SceneRepository, document_id: str, count: int
) -> list[str]:
    head = repository.latest(document_id)
    parent = head.revision_id if head is not None else None
    ids: list[str] = []
    for index in range(count):
        scene = make_empty_scene(document_id).model_copy(
            update={
                "room": RoomPrism(
                    width_m=2.0 + index * 0.01,
                    depth_m=4.0,
                    height_m=2.4,
                )
            }
        )
        result = repository.save(scene, parent_revision_id=parent)
        parent = result.revision.revision_id
        ids.append(parent)
    return ids


def _service(repository: SceneRepository) -> CadProjectActivityService:
    return CadProjectActivityService(
        scene_repository=repository,
        notes_repository=CadProjectActivityNoteRepository(repository),
    )


def _page(
    repository: SceneRepository,
    *,
    document_id: str = "doc-alpha",
    list_operations=None,
    event_spy: list | None = None,
) -> ActivityPage:
    """ActivityPage wired like the composition; ``event_spy`` counts the
    projection rebuilds when supplied."""

    service = _service(repository)

    def list_revisions(doc_id, limit, after):
        return list_revisions_page(
            repository,
            limit,
            scope="global" if doc_id is None else "project",
            document_id=doc_id,
            after=after,
        )

    def count_revisions(doc_id):
        return count_recent_revisions(
            repository,
            scope="global" if doc_id is None else "project",
            document_id=doc_id,
        )

    def list_events(doc_id):
        if event_spy is not None:
            event_spy.append(doc_id)
        if doc_id is not None:
            return tuple(reversed(service.events(doc_id)))
        merged = [
            event
            for doc in list_known_document_ids(repository)
            for event in service.events(doc)
        ]
        merged.sort(key=lambda e: e.occurred_at_utc, reverse=True)
        return tuple(merged)

    return ActivityPage(
        list_revisions,
        count_revisions=count_revisions,
        list_operations=list_operations,
        list_events=list_events,
        document_id=document_id,
    )


def _seed_notes(service, document_id, count, *, stamp) -> None:
    for index in range(count):
        service.add_note(
            document_id=document_id,
            title=f"検証メモ {index:03d}",
            created_at_utc=stamp,
        )


def _event_ids(page: ActivityPage) -> list[str]:
    return [
        page.events_table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1)
        for row in range(page.events_table.rowCount())
    ]


def _custom_index(page: ActivityPage) -> int:
    return next(
        i
        for i in range(page.range_combo.count())
        if page.range_combo.itemData(i) == "custom"
    )


# -- arbitrary start/end date bounds --------------------------------------


def test_custom_range_filters_by_operator_bounds(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    service = _service(repository)
    service.add_note(
        document_id="doc-alpha",
        title="古いメモ",
        created_at_utc="2020-01-05T10:00:00+00:00",
    )
    service.add_note(
        document_id="doc-alpha",
        title="期間内のメモ",
        created_at_utc="2024-06-15T12:00:00+00:00",
    )
    service.add_note(
        document_id="doc-alpha",
        title="新しいメモ",
        created_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    page = _page(repository, document_id="doc-alpha")

    page.range_combo.setCurrentIndex(_custom_index(page))
    page.range_start_edit.setDate(QDate(2024, 1, 1))
    page.range_end_edit.setDate(QDate(2024, 12, 31))

    titles = {
        page.events_table.item(row, 2).text()
        for row in range(page.events_table.rowCount())
    }
    assert titles == {"メモ「期間内のメモ」"}
    assert "全1件" in page.events_pager[2].text()


def test_custom_range_bounds_are_inclusive(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    service = _service(repository)
    service.add_note(
        document_id="doc-alpha",
        title="朝のメモ",
        created_at_utc="2024-03-10T00:00:01+00:00",
    )
    service.add_note(
        document_id="doc-alpha",
        title="夜のメモ",
        created_at_utc="2024-03-10T23:59:58+00:00",
    )
    page = _page(repository, document_id="doc-alpha")

    page.range_combo.setCurrentIndex(_custom_index(page))
    page.range_start_edit.setDate(QDate(2024, 3, 10))
    page.range_end_edit.setDate(QDate(2024, 3, 10))

    # Same calendar day covers both boundary instants (#1017).
    titles = {
        page.events_table.item(row, 2).text()
        for row in range(page.events_table.rowCount())
    }
    assert titles == {"メモ「朝のメモ」", "メモ「夜のメモ」"}


def test_custom_range_reversed_bounds_show_zero(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    _seed_notes(
        _service(repository),
        "doc-alpha",
        3,
        stamp=datetime.now(timezone.utc).isoformat(),
    )
    page = _page(repository, document_id="doc-alpha")

    page.range_combo.setCurrentIndex(_custom_index(page))
    page.range_start_edit.setDate(QDate(2025, 6, 1))
    page.range_end_edit.setDate(QDate(2024, 1, 1))

    # Literal application — nothing is silently reordered (#1017).
    assert page.events_table.rowCount() == 0
    assert "全0件" in page.events_pager[2].text()


def test_preset_selection_disables_custom_date_edits(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    page = _page(repository, document_id="doc-alpha")

    page.range_combo.setCurrentIndex(_custom_index(page))
    assert page.range_start_edit.isEnabled()
    assert page.range_end_edit.isEnabled()

    preset = next(
        i
        for i in range(page.range_combo.count())
        if page.range_combo.itemData(i) == 30
    )
    page.range_combo.setCurrentIndex(preset)
    assert not page.range_start_edit.isEnabled()
    assert not page.range_end_edit.isEnabled()


# -- cursor / さらに読み込む deep-history walks ----------------------------


def test_events_load_more_reaches_oldest_row(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    service = _service(repository)
    _seed_notes(service, "doc-alpha", 220, stamp="2024-05-01T00:00:00+00:00")
    oldest = service.add_note(
        document_id="doc-alpha",
        title="最古のメモ",
        created_at_utc="2019-01-01T00:00:00+00:00",
    )
    page = _page(repository, document_id="doc-alpha")

    # 220 seeded notes + oldest + the project_created row = 222.
    assert page.events_table.rowCount() == 50
    assert "222" in page.events_pager[2].text()
    while page.events_pager[1].isEnabled():
        page._events_load_more()

    ids = _event_ids(page)
    assert len(ids) == len(set(ids)) == 222
    # The deepest row is reachable — nothing behind a fixed cap (#1017).
    last = page.events_table.item(
        page.events_table.rowCount() - 1, 2
    ).text()
    assert "最古のメモ" in last


def test_events_cursor_never_skips_or_duplicates_on_growth(
    tmp_path,
) -> None:
    _app()
    repository = _repository(tmp_path)
    service = _service(repository)
    _seed_notes(
        service, "doc-alpha", 60, stamp="2024-05-01T00:00:00+00:00"
    )
    page = _page(repository, document_id="doc-alpha")
    assert page.events_table.rowCount() == 50

    # New authority lands while the operator browses: refresh re-derives
    # the projection, the cursor keeps the continue point stable.
    _seed_notes(
        service, "doc-alpha", 5, stamp="2026-10-08T00:00:00+00:00"
    )
    page.refresh()
    page._events_load_more()

    ids = _event_ids(page)
    assert len(ids) == len(set(ids)) == 66
    assert not page.events_pager[1].isEnabled()


def test_revision_keyset_cursor_walks_every_row(tmp_path) -> None:
    repository = _repository(tmp_path)
    ids = _save_revisions(repository, "doc-alpha", 220)
    expected = set(ids) | {
        repository.list_revisions("doc-alpha")[0].revision_id
    }

    seen: list[str] = []
    cursor = None
    while True:
        rows, cursor = list_revisions_page(
            repository,
            50,
            scope="project",
            document_id="doc-alpha",
            after=cursor,
        )
        if not rows:
            break
        seen.extend(row[2] for row in rows)
        if cursor is None:
            break

    assert cursor is None
    assert set(seen) == expected
    assert len(seen) == len(set(seen)) == 221


def test_revision_keyset_cursor_is_stable_under_new_commits(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    _save_revisions(repository, "doc-alpha", 60)

    rows, cursor = list_revisions_page(
        repository, 50, scope="project", document_id="doc-alpha"
    )
    first_ids = {row[2] for row in rows}
    assert cursor is not None

    # A commit landing mid-browse shifts OFFSET windows but never a
    # keyset cursor: the next page still starts exactly after seq.
    _save_revisions(repository, "doc-alpha", 3)
    more, cursor = list_revisions_page(
        repository,
        50,
        scope="project",
        document_id="doc-alpha",
        after=cursor,
    )
    remaining = {row[2] for row in more}
    assert len(more) == 11
    assert cursor is None
    assert not remaining & first_ids
    assert len(first_ids | remaining) == 61


def test_revision_cursor_rejects_garbage(tmp_path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(ValueError):
        list_revisions_page(
            repository, scope="project", document_id="doc-alpha",
            after="bogus",
        )
    with pytest.raises(ValueError):
        list_revisions_page(
            repository, scope="project", document_id="doc-alpha",
            after="seq:not-a-number",
        )
    with pytest.raises(ValueError):
        list_revisions_page(
            repository, scope="project", document_id="doc-alpha",
            after=42,  # type: ignore[arg-type]
        )


def test_revision_page_scope_contract_unchanged(tmp_path) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(ValueError):
        list_revisions_page(repository)  # project scope, no document_id
    with pytest.raises(ValueError):
        list_revisions_page(repository, scope="bogus")  # type: ignore[arg-type]

    rows, _ = list_revisions_page(repository, scope="global")
    assert {row[1] for row in rows} == {"doc-alpha", "doc-beta"}


def test_page_revisions_load_more_appends(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    _save_revisions(repository, "doc-alpha", 60)
    page = _page(repository, document_id="doc-alpha")

    assert page.table.rowCount() == 50
    assert page.revisions_pager[1].isEnabled()
    page._revisions_load_more()
    assert page.table.rowCount() == 61
    assert not page.revisions_pager[1].isEnabled()


# -- read-only detail inspector -------------------------------------------


def test_evidence_class_mapping() -> None:
    base = dict(
        document_id="doc",
        source_kind="scene_revision",
        source_id="rev-1",
        occurred_at_utc="2024-01-01T00:00:00+00:00",
        title="x",
    )
    assert (
        event_evidence_class(_event(kind="scene_revision_saved", **base))
        == "evidence"
    )
    assert event_evidence_class(
        _event(kind="project_note", source_kind="project_note",
               source_id="n1", document_id="doc", title="m",
               occurred_at_utc="2024-01-01T00:00:00+00:00")
    ) == "note"
    assert event_evidence_class(
        _event(kind="capture_rejected", source_kind="capture_inbox_item",
               source_id="c1:rejected", document_id="doc", title="r",
               occurred_at_utc="2024-01-01T00:00:00+00:00")
    ) == "failed"
    assert event_evidence_class(
        _event(kind="operating_preset_applied",
               source_kind="applied_preset_state", source_id="a1",
               document_id="doc", title="p",
               occurred_at_utc="2024-01-01T00:00:00+00:00")
    ) == "assumption"
    assert event_evidence_class(
        _event(kind="system_variant_as_built",
               source_kind="system_variant_as_built", source_id="b1",
               document_id="doc", title="b",
               occurred_at_utc="2024-01-01T00:00:00+00:00")
    ) == "assumption"
    assert event_evidence_class(
        _event(kind="capture_superseded",
               source_kind="capture_inbox_supersession", source_id="s1",
               document_id="doc", title="s",
               occurred_at_utc="2024-01-01T00:00:00+00:00")
    ) == "historical"
    assert event_evidence_class(
        _event(kind="scene_revision_saved", document_id="doc",
               source_kind="scene_revision", source_id="r9",
               occurred_at_utc="2024-01-01T00:00:00+00:00", title="d",
               detail="非ヘッド履歴")
    ) == "historical"
    assert event_evidence_class(
        _event(kind="scene_revision_saved", document_id="doc",
               source_kind="scene_revision", source_id="r8",
               occurred_at_utc="2024-01-01T00:00:00+00:00", title="i",
               inherited=True)
    ) == "historical"


def test_inspector_populates_typed_detail(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    service = _service(repository)
    note = service.add_note(
        document_id="doc-alpha",
        title="調査メモ",
        created_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    page = _page(repository, document_id="doc-alpha")

    note_row = next(
        row
        for row in range(page.events_table.rowCount())
        if page.events_table.item(row, 2).text() == "メモ「調査メモ」"
    )
    page.events_table.selectRow(note_row)
    assert page._inspector_title.text() == "内容: メモ「調査メモ」"
    assert "メモ" in page._inspector_class.text()
    assert "証拠としては扱いません" in page._inspector_note.text()
    assert "project_note" in page._inspector_sources.text()
    assert note.note_id in page._inspector_sources.text()
    assert "activity:" in page._inspector_id.text()

    # A revision row types as canonical evidence with its source ref.
    rev_row = next(
        row
        for row in range(page.events_table.rowCount())
        if page.events_table.item(row, 2).text().startswith(
            "プロジェクト作成"
        )
    )
    page.events_table.selectRow(rev_row)
    assert "証跡" in page._inspector_class.text()
    assert "scene_revision" in page._inspector_sources.text()
    assert "プロジェクト作成" in page._inspector_title.text()


def test_inspector_shows_correlated_refs(tmp_path) -> None:
    _app()
    # An applied variant event carries a second source_ref — the
    # correlation lane must show it (primary vs related authority).
    event = _event(
        kind="system_variant_applied",
        document_id="doc-alpha",
        source_kind="system_variant_application",
        source_id="app-1",
        source_sha256="a" * 64,
        occurred_at_utc="2024-01-01T00:00:00+00:00",
        title="バリアント適用",
        extra_refs=(
            ActivitySourceRef(
                kind="system_variant",
                ref_id="var-1",
                ref_sha256="b" * 64,
            ),
        ),
    )
    repository = _repository(tmp_path)
    service = _service(repository)

    def list_events(doc_id):
        return (*reversed(service.events(doc_id)), event)

    page = ActivityPage(
        lambda doc, limit, after: list_revisions_page(
            repository,
            limit,
            scope="global" if doc is None else "project",
            document_id=doc,
            after=after,
        ),
        list_events=list_events,
        document_id="doc-alpha",
    )
    row = next(
        row
        for row in range(page.events_table.rowCount())
        if "バリアント適用" in page.events_table.item(row, 2).text()
    )
    page.events_table.selectRow(row)
    assert "system_variant_application" in page._inspector_sources.text()
    assert "app-1" in page._inspector_sources.text()
    assert "system_variant" in page._inspector_correlation.text()
    assert "var-1" in page._inspector_correlation.text()


def test_inspector_clears_when_selection_empty(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    _seed_notes(
        _service(repository),
        "doc-alpha",
        2,
        stamp=datetime.now(timezone.utc).isoformat(),
    )
    page = _page(repository, document_id="doc-alpha")

    page.events_table.selectRow(0)
    assert "メモ" in page._inspector_class.text()
    page.events_table.clearSelection()
    assert (
        page._inspector_summary.text() == "行を選ぶと詳細を表示します。"
    )


def test_inspector_follows_body_cell_click(tmp_path) -> None:
    """A body-cell click selects the row — the inspector must populate
    without requiring a row-header click (real-GUI regression: the
    timeline shipped SelectItems, so selectedRows() stayed empty)."""

    _app()
    repository = _repository(tmp_path)
    _seed_notes(
        _service(repository),
        "doc-alpha",
        2,
        stamp=datetime.now(timezone.utc).isoformat(),
    )
    page = _page(repository, document_id="doc-alpha")

    assert page.events_table.selectionBehavior() == (
        QAbstractItemView.SelectionBehavior.SelectRows
    )
    assert page.table.selectionBehavior() == (
        QAbstractItemView.SelectionBehavior.SelectRows
    )

    # Simulate a mouse click on a body cell in the second column: the
    # view's selectionCommand expands it to the whole row under
    # SelectRows, and the inspector fills in.
    page.events_table.setCurrentCell(0, 1)
    assert page._selected_event() is not None
    assert "メモ" in page._inspector_class.text()
    assert "証拠としては扱いません" in page._inspector_note.text()


def test_deselected_row_does_not_resurrect_on_refresh(tmp_path) -> None:
    """Ctrl+click deselect leaves a stale ``currentRow``; a refresh must
    not re-select the row the operator deliberately cleared."""

    _app()
    repository = _repository(tmp_path)
    _seed_notes(
        _service(repository),
        "doc-alpha",
        3,
        stamp=datetime.now(timezone.utc).isoformat(),
    )
    page = _page(repository, document_id="doc-alpha")

    page.events_table.selectRow(0)
    page.events_table.clearSelection()
    assert page._selected_event() is None

    # Any re-render path (filter change / scope change / refresh) used to
    # restore the stale current row; the cleared selection must stay
    # cleared.
    page.search_edit.setText("メモ")
    page.search_edit.setText("")
    page.refresh()

    assert page._selected_event() is None
    assert (
        page._inspector_summary.text() == "行を選ぶと詳細を表示します。"
    )


def test_inspector_is_read_only() -> None:
    _app()
    page = ActivityPage(
        lambda doc, limit, after: ((), None),
        list_events=lambda doc: (),
    )
    for label in (
        page._inspector_summary,
        page._inspector_class,
        page._inspector_title,
        page._inspector_detail,
        page._inspector_sources,
        page._inspector_correlation,
        page._inspector_id,
        page._inspector_note,
    ):
        # Labels are never editable; keyboard text selection is enabled
        # so screen readers and copy reach the same content (#1017).
        assert label.textInteractionFlags() & (
            Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        assert label.accessibleName()


# -- selection stability across load-more -----------------------------------


def test_selection_survives_load_more_and_refresh(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    _seed_notes(
        _service(repository),
        "doc-alpha",
        70,
        stamp="2024-05-01T00:00:00+00:00",
    )
    page = _page(repository, document_id="doc-alpha")

    page.events_table.selectRow(0)
    key = (
        page.events_table.item(0, 0).data(Qt.ItemDataRole.UserRole + 1),
        page.events_table.item(0, 0).data(Qt.ItemDataRole.UserRole + 2),
    )
    page._events_load_more()
    row = page.events_table.currentRow()
    assert row == 0
    assert (
        page.events_table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1),
        page.events_table.item(row, 0).data(Qt.ItemDataRole.UserRole + 2),
    ) == key

    page.refresh()
    row = page.events_table.currentRow()
    assert (
        page.events_table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1),
        page.events_table.item(row, 0).data(Qt.ItemDataRole.UserRole + 2),
    ) == key


def test_revision_selection_survives_load_more(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    _save_revisions(repository, "doc-alpha", 60)
    page = _page(repository, document_id="doc-alpha")

    page.table.selectRow(0)
    key = (
        page.table.item(0, 2).data(Qt.ItemDataRole.UserRole),
        page.table.item(0, 1).data(Qt.ItemDataRole.UserRole),
    )
    page._revisions_load_more()
    row = page.table.currentRow()
    assert (
        page.table.item(row, 2).data(Qt.ItemDataRole.UserRole),
        page.table.item(row, 1).data(Qt.ItemDataRole.UserRole),
    ) == key


# -- deep-link reach into loaded history ------------------------------------


def test_activity_focus_walks_to_deep_event(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    service = _service(repository)
    _seed_notes(service, "doc-alpha", 80, stamp="2024-05-01T00:00:00+00:00")
    page = _page(repository, document_id="doc-alpha")
    assert page.events_table.rowCount() == 50

    # The oldest event (a 2024 note) sits beyond the initial window —
    # the focus walk must load more rows until the row exists (#1017).
    oldest_event = service.events("doc-alpha")[0]
    assert oldest_event.kind == "project_note"
    target = NavigationTarget(
        kind=NavigationTargetKind.PROJECT_NOTE,
        object_ids=tuple(ref.ref_id for ref in oldest_event.source_refs),
    )
    result = activity_focus(page, target)
    assert result.focused is True
    row = page.events_table.currentRow()
    assert row == page.events_table.rowCount() - 1
    assert row >= 50


def test_activity_focus_walks_to_deep_revision(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    ids = _save_revisions(repository, "doc-alpha", 70)
    page = _page(repository, document_id="doc-alpha")

    result = activity_focus(
        page,
        NavigationTarget(
            kind=NavigationTargetKind.SCENE_REVISION,
            object_ids=(ids[0],),  # oldest saved revision
        ),
    )
    assert result.focused is True
    # The deep link resolves either to the timeline row whose nav URI
    # references the revision (timeline is scanned first and is the
    # canonical projection) or to the revision-ledger row itself.
    event_row = page.events_table.currentRow()
    ledger_row = page.table.currentRow()
    if event_row >= 0:
        cell = page.events_table.item(event_row, 0)
        linked = navigation_target_from_uri(
            cell.data(Qt.ItemDataRole.UserRole)
        )
        assert linked.kind is NavigationTargetKind.SCENE_REVISION
        assert ids[0] in linked.object_ids
        # Deep history: the row only exists past the initial window.
        assert event_row >= 50
    else:
        assert ledger_row >= 50
        assert page.table.item(ledger_row, 2).data(
            Qt.ItemDataRole.UserRole
        ) == ids[0]


# -- scope / count / cache invariants ----------------------------------------


def test_projection_cached_between_filter_changes(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    _seed_notes(
        _service(repository),
        "doc-alpha",
        3,
        stamp=datetime.now(timezone.utc).isoformat(),
    )
    calls: list = []
    page = _page(repository, document_id="doc-alpha", event_spy=calls)
    baseline = len(calls)

    # Filter/search/paging reuse the cached projection — one rebuild per
    # scope, not per keystroke (#1017 bounded rebuild).
    page.search_edit.setText("メモ")
    page.search_edit.setText("")
    page._events_load_more()
    assert len(calls) == baseline

    page.refresh()
    assert len(calls) == baseline + 1


def test_counts_match_filtered_rows(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    _seed_notes(
        _service(repository),
        "doc-alpha",
        4,
        stamp=datetime.now(timezone.utc).isoformat(),
    )
    page = _page(repository, document_id="doc-alpha")

    page.search_edit.setText("検証メモ 002")
    label = page.events_pager[2].text()
    assert "全1件" in label
    assert page.events_table.rowCount() == 1


def test_scope_contract_unchanged(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    page = _page(repository, document_id="doc-alpha")

    docs = {
        page.events_table.item(row, 0).data(Qt.ItemDataRole.UserRole + 2)
        for row in range(page.events_table.rowCount())
    }
    assert docs == {"doc-alpha"}
    page.scope_combo.setCurrentIndex(1)
    docs = {
        page.events_table.item(row, 0).data(Qt.ItemDataRole.UserRole + 2)
        for row in range(page.events_table.rowCount())
    }
    assert docs == {"doc-alpha", "doc-beta"}
    page.scope_combo.setCurrentIndex(0)


# -- narrow UI / accessibility reachability ---------------------------------


def test_controls_reachable_at_narrow_width(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    page = _page(repository, document_id="doc-alpha")
    page.resize(640, 480)
    page.show()

    for widget in (
        page.kind_combo,
        page.range_combo,
        page.range_start_edit,
        page.range_end_edit,
        page.search_edit,
        page.events_table,
        page.events_pager[1],
        page.event_inspector,
        page.table,
        page.revisions_pager[1],
    ):
        assert not widget.isHidden()
        assert widget.accessibleName() or widget.toolTip()

    # UIA reachability: named controls with keyboard focus.
    assert page.event_inspector.accessibleName()
    assert page.event_inspector.focusPolicy() != Qt.FocusPolicy.NoFocus
    assert page.events_pager[1].text() == "さらに読み込む"
    page.close()
