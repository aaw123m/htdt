"""Issue #1023: activity history project scoping.

Two documents living in one app store must never contaminate each other's
activity page: the revision SQL filters by ``document_id`` at the query,
the 「このプロジェクト / 全プロジェクト」 scope is stated honestly in each
section heading, the timeline takes kind/date-range/search filters with
explicit paging (no fixed cap can bury the past), selections survive
re-render by (event_id / revision_id / document_id), and a row from
another project only activates through a project-stamped deep link.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from htdt.activity_center import ActivityCenter, OperationClass
from htdt.application_pages import (
    ActivityPage,
    count_recent_revisions,
    list_known_document_ids,
    list_recent_revisions,
)
from htdt.cad_project_activity import CadProjectActivityService
from htdt.cad_project_activity_repository import (
    CadProjectActivityNoteRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import RoomPrism, make_empty_scene
from htdt.navigation_target import (
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
    """Append ``count`` head revisions; returns revision ids oldest→newest.

    ``SceneRepository.save`` dedupes an unchanged payload against its
    parent, so every save must carry a real document change.
    """

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
    extra_project_refs=(),
) -> ActivityPage:
    """ActivityPage wired exactly like the app composition."""

    service = _service(repository)

    def list_revisions(doc_id, limit, offset):
        return list_recent_revisions(
            repository,
            limit,
            scope="global" if doc_id is None else "project",
            document_id=doc_id,
            offset=offset,
        )

    def count_revisions(doc_id):
        return count_recent_revisions(
            repository,
            scope="global" if doc_id is None else "project",
            document_id=doc_id,
        )

    def list_events(doc_id):
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
        project_refs=extra_project_refs,
    )


def _revision_docs(page: ActivityPage) -> set[str]:
    docs = set()
    for row in range(page.table.rowCount()):
        cell = page.table.item(row, 1)
        docs.add(cell.data(Qt.ItemDataRole.UserRole))
    return docs


def _event_docs(page: ActivityPage) -> set[str]:
    docs = set()
    for row in range(page.events_table.rowCount()):
        cell = page.events_table.item(row, 0)
        docs.add(cell.data(Qt.ItemDataRole.UserRole + 2))
    return docs


# -- the SQL filter is scoped at the query ------------------------------------


def test_project_scope_never_returns_foreign_revisions(tmp_path) -> None:
    repository = _repository(tmp_path)
    alpha_ids = _save_revisions(repository, "doc-alpha", 3)
    beta_ids = _save_revisions(repository, "doc-beta", 5)

    rows = list_recent_revisions(
        repository, scope="project", document_id="doc-alpha"
    )
    assert rows
    assert {row[1] for row in rows} == {"doc-alpha"}
    expected = {
        r.revision_id for r in repository.list_revisions("doc-alpha")
    }
    assert {row[2] for row in rows} == expected
    assert not {row[2] for row in rows} & set(beta_ids)


def test_global_scope_is_an_explicit_opt_in(tmp_path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(ValueError):
        list_recent_revisions(repository)  # project scope, no document_id
    with pytest.raises(ValueError):
        count_recent_revisions(repository)
    with pytest.raises(ValueError):
        list_recent_revisions(repository, scope="bogus")  # type: ignore[arg-type]

    rows = list_recent_revisions(repository, scope="global")
    assert {row[1] for row in rows} == {"doc-alpha", "doc-beta"}


def test_revision_counts_match_scope(tmp_path) -> None:
    repository = _repository(tmp_path)
    _save_revisions(repository, "doc-alpha", 2)

    assert (
        count_recent_revisions(
            repository, scope="project", document_id="doc-alpha"
        )
        == 3
    )
    assert (
        count_recent_revisions(
            repository, scope="project", document_id="doc-beta"
        )
        == 1
    )
    assert count_recent_revisions(repository, scope="global") == 4


def test_revision_paging_covers_full_history(tmp_path) -> None:
    repository = _repository(tmp_path)
    ids = _save_revisions(repository, "doc-alpha", 120)
    expected = set(ids) | {repository.list_revisions("doc-alpha")[0].revision_id}

    seen: list[str] = []
    offset = 0
    while True:
        rows = list_recent_revisions(
            repository,
            50,
            scope="project",
            document_id="doc-alpha",
            offset=offset,
        )
        if not rows:
            break
        seen.extend(row[2] for row in rows)
        offset += 50

    assert set(seen) == expected
    assert len(seen) == len(set(seen)) == 121


# -- the page surfaces scope honestly ------------------------------------------


def test_page_project_scope_shows_zero_cross_contamination(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    page = _page(repository, document_id="doc-alpha")

    assert page.scope_combo.currentData() == "project"
    assert {row for row in _revision_docs(page)} == {"doc-alpha"}
    assert _event_docs(page) == {"doc-alpha"}
    assert "このプロジェクト" in page.timeline_heading.text()
    assert "このプロジェクト" in page.revisions_heading.text()


def test_page_global_scope_lists_every_project(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    page = _page(repository, document_id="doc-alpha")

    page.scope_combo.setCurrentIndex(1)
    assert page.scope_combo.currentData() == "global"
    assert _revision_docs(page) == {"doc-alpha", "doc-beta"}
    assert _event_docs(page) == {"doc-alpha", "doc-beta"}
    assert "全プロジェクト" in page.timeline_heading.text()
    assert "全プロジェクト" in page.revisions_heading.text()
    # The per-event project column appears only in the global view.
    assert not page.events_table.isColumnHidden(1)

    page.scope_combo.setCurrentIndex(0)
    assert page.events_table.isColumnHidden(1)


def test_operations_split_by_project_scope(tmp_path) -> None:
    _app()
    center = ActivityCenter()
    project_op = center.submit(
        operation_kind="backup.create",
        operation_class=OperationClass.DATA_MANAGEMENT,
        title="このプロジェクトのバックアップ",
        project_ref="doc-alpha",
    )
    other_op = center.submit(
        operation_kind="backup.create",
        operation_class=OperationClass.DATA_MANAGEMENT,
        title="他プロジェクトのバックアップ",
        project_ref="doc-beta",
    )
    global_op = center.submit(
        operation_kind="storage_integrity_scan",
        operation_class=OperationClass.DATA_MANAGEMENT,
        title="アプリ全体スキャン",
    )
    page = _page(
        repository=_repository(tmp_path),
        list_operations=lambda: (*center.active(), *center.recent()),
    )

    project_rows = {
        page.operations_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        for row in range(page.operations_table.rowCount())
    }
    other_rows = {
        page.other_operations_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        for row in range(page.other_operations_table.rowCount())
    }
    assert project_rows == {project_op}
    assert other_rows == {other_op, global_op}
    assert "このプロジェクト" in page.operations_heading.text()
    assert page.other_operations_heading.text()


def test_operation_project_ref_matches_display_names(tmp_path) -> None:
    _app()
    center = ActivityCenter()
    op = center.submit(
        operation_kind="backup.create",
        operation_class=OperationClass.DATA_MANAGEMENT,
        title="名前での一致",
        project_ref="リビングシアター",
    )
    page = _page(
        repository=_repository(tmp_path),
        list_operations=lambda: (*center.active(), *center.recent()),
        extra_project_refs=("リビングシアター", "proj-canonical-1"),
    )
    ids = {
        page.operations_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        for row in range(page.operations_table.rowCount())
    }
    assert ids == {op}


# -- timeline filters, counts, paging -------------------------------------------


def _seed_notes(service, document_id, count, *, stamp):
    for index in range(count):
        service.add_note(
            document_id=document_id,
            title=f"検証メモ {index:02d}",
            created_at_utc=stamp,
        )


def test_timeline_kind_filter(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    _seed_notes(
        _service(repository),
        "doc-alpha",
        3,
        stamp=datetime.now(timezone.utc).isoformat(),
    )
    page = _page(repository, document_id="doc-alpha")

    memo_index = next(
        i
        for i in range(page.kind_combo.count())
        if page.kind_combo.itemData(i) == frozenset({"project_note"})
    )
    page.kind_combo.setCurrentIndex(memo_index)
    event_ids = {
        page.events_table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1)
        for row in range(page.events_table.rowCount())
    }
    assert len(event_ids) == 3
    titles = {
        page.events_table.item(row, 2).text()
        for row in range(page.events_table.rowCount())
    }
    assert all("検証メモ" in title for title in titles)


def test_timeline_date_range_filter(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    service = _service(repository)
    service.add_note(
        document_id="doc-alpha",
        title="古いメモ",
        created_at_utc="2020-01-01T00:00:00+00:00",
    )
    service.add_note(
        document_id="doc-alpha",
        title="新しいメモ",
        created_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    page = _page(repository, document_id="doc-alpha")

    today_index = next(
        i
        for i in range(page.range_combo.count())
        if page.range_combo.itemData(i) == 0
    )
    page.range_combo.setCurrentIndex(today_index)
    titles = {
        page.events_table.item(row, 2).text()
        for row in range(page.events_table.rowCount())
    }
    assert "メモ「新しいメモ」" in titles
    assert "メモ「古いメモ」" not in titles


def test_timeline_search_filter(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    service = _service(repository)
    service.add_note(
        document_id="doc-alpha",
        title="スピーカー配置の検討",
        created_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    service.add_note(
        document_id="doc-alpha",
        title="吸音材の候補",
        created_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    page = _page(repository, document_id="doc-alpha")

    page.search_edit.setText("スピーカー")
    titles = {
        page.events_table.item(row, 2).text()
        for row in range(page.events_table.rowCount())
    }
    assert titles == {"メモ「スピーカー配置の検討」"}
    page.search_edit.setText("")
    assert page.events_table.rowCount() >= 2


def test_timeline_paging_walks_every_row(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    _seed_notes(
        _service(repository),
        "doc-alpha",
        60,
        stamp=datetime.now(timezone.utc).isoformat(),
    )
    page = _page(repository, document_id="doc-alpha")

    first_page = page.events_table.rowCount()
    assert first_page == 50
    count_text = page.events_pager[3].text()
    assert "全" in count_text and "件" in count_text
    first_page_ids = {
        page.events_table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1)
        for row in range(first_page)
    }

    page._events_next_page()
    rest = {
        page.events_table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1)
        for row in range(page.events_table.rowCount())
    }
    assert rest
    assert not rest & first_page_ids
    assert page.events_pager[1].isEnabled()  # 前へ


def test_revision_paging_walks_every_row(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    _save_revisions(repository, "doc-alpha", 60)
    page = _page(repository, document_id="doc-alpha")

    assert page.table.rowCount() == 50
    assert "61" in page.revisions_pager[3].text()
    first_ids = {
        page.table.item(row, 2).data(Qt.ItemDataRole.UserRole)
        for row in range(page.table.rowCount())
    }
    page._revisions_next_page()
    assert page.table.rowCount() == 11
    rest_ids = {
        page.table.item(row, 2).data(Qt.ItemDataRole.UserRole)
        for row in range(page.table.rowCount())
    }
    assert not rest_ids & first_ids
    assert page.revisions_pager[1].isEnabled()


# -- selection stability + honest activation ------------------------------------


def test_revision_selection_survives_refresh(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    page = _page(repository, document_id="doc-alpha")

    page.table.selectRow(0)
    key = (
        page.table.item(0, 2).data(Qt.ItemDataRole.UserRole),
        page.table.item(0, 1).data(Qt.ItemDataRole.UserRole),
    )
    page.refresh()
    row = page.table.currentRow()
    assert row >= 0
    assert (
        page.table.item(row, 2).data(Qt.ItemDataRole.UserRole),
        page.table.item(row, 1).data(Qt.ItemDataRole.UserRole),
    ) == key


def test_event_selection_survives_filter_change(tmp_path) -> None:
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
    key = (
        page.events_table.item(0, 0).data(Qt.ItemDataRole.UserRole + 1),
        page.events_table.item(0, 0).data(Qt.ItemDataRole.UserRole + 2),
    )
    # A filter that keeps the row must keep the selection on the same
    # (event_id, document_id), not just the same row index.
    memo_index = next(
        i
        for i in range(page.kind_combo.count())
        if page.kind_combo.itemData(i) == frozenset({"project_note"})
    )
    page.kind_combo.setCurrentIndex(memo_index)
    row = page.events_table.currentRow()
    assert row >= 0
    assert (
        page.events_table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1),
        page.events_table.item(row, 0).data(Qt.ItemDataRole.UserRole + 2),
    ) == key


def test_revision_row_links_carry_the_owning_project(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    page = _page(repository, document_id="doc-alpha")
    page.scope_combo.setCurrentIndex(1)  # 全プロジェクト

    projects = {}
    for row in range(page.table.rowCount()):
        doc = page.table.item(row, 1).data(Qt.ItemDataRole.UserRole)
        link = page.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        target = navigation_target_from_uri(link)
        assert target.kind is NavigationTargetKind.SCENE_REVISION
        projects[doc] = target.project_id

    # Every deep link names the project that owns the row — activating a
    # doc-beta row can only go through the guarded project switch, never a
    # silent activation inside doc-alpha (#1023).
    assert projects["doc-alpha"] == "doc-alpha"
    assert projects["doc-beta"] == "doc-beta"


def test_foreign_event_activation_stamps_its_project(tmp_path) -> None:
    _app()
    repository = _repository(tmp_path)
    opened: list[str] = []
    page = _page(repository, document_id="doc-alpha")
    page._open_link = opened.append
    page.scope_combo.setCurrentIndex(1)

    foreign_row = next(
        row
        for row in range(page.events_table.rowCount())
        if page.events_table.item(row, 0).data(Qt.ItemDataRole.UserRole + 2)
        == "doc-beta"
    )
    page._activate_event(page.events_table.item(foreign_row, 0))
    assert opened
    target = navigation_target_from_uri(opened[0])
    assert target.project_id == "doc-beta"


def test_project_scope_rows_never_activate_foreign_revisions(
    tmp_path,
) -> None:
    _app()
    repository = _repository(tmp_path)
    opened: list[str] = []
    page = _page(repository, document_id="doc-alpha")
    page._open_link = opened.append

    # Project scope shows no foreign revision rows at all — there is
    # nothing a user could activate from another project by accident.
    assert _revision_docs(page) == {"doc-alpha"}
    for row in range(page.table.rowCount()):
        page._activate_revision(page.table.item(row, 0))
    assert opened
    for uri in opened:
        assert (
            navigation_target_from_uri(uri).project_id == "doc-alpha"
        )


# -- projection purity -----------------------------------------------------------


def test_event_projection_is_reconstructible(tmp_path) -> None:
    repository = _repository(tmp_path)
    service = _service(repository)
    first = [e.event_id for e in service.events("doc-alpha")]
    second = [e.event_id for e in service.events("doc-alpha")]
    assert first == second
    events = service.events("doc-alpha")
    assert all(e.document_id == "doc-alpha" for e in events)


def test_list_known_document_ids_covers_both_projects(tmp_path) -> None:
    repository = _repository(tmp_path)
    assert set(list_known_document_ids(repository)) == {
        "doc-alpha",
        "doc-beta",
    }
