"""#988: Capture Inbox mass-arrival triage.

検索・状態別キュー・ソート・グループ化、要レビュー件数、
次の未処理を表示、詳細の操作可能/ブロック理由、行 identity の固定を
検証する。キュー分類は行の保持 facet のみから導くため
10/100/1000 件でも inspect() を呼ばず即座に絞り込める。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QInputDialog

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.application_pages import (  # noqa: E402
    CaptureInboxPage,
    _inbox_queue_state,
    inbox_focus,
)
from htdt.capture_ingestion_transaction import (  # noqa: E402
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)
from htdt.capture_inbox import CaptureInboxItem, CaptureInboxRepository  # noqa: E402
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.navigation_target import NavigationTarget, NavigationTargetKind  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _item(
    index: int,
    *,
    scope: str = "capture-inbox-unassigned",
    series: str | None = None,
    classification: str = "new_series",
    disposition: str = "pending",
    arrived: str | None = None,
    validation: str = "validated",
    dependency: str = "not_evaluated",
    alignment: str = "not_required",
    conflict: str = "none",
    source: str = "file_import",
) -> CaptureInboxItem:
    digest = f"{index & 0xFFFFFFFFFFFFFFFF:064x}"
    return CaptureInboxItem(
        inbox_item_id=f"capture-inbox-item:{digest}",
        lineage_digest=digest,
        scope=scope,
        capture_series_id=series or f"series-{index % 4}",
        capture_revision_id=f"rev-{index}",
        bundle_digest=f"{(index * 7919) & 0xFFFFFFFFFFFFFFFF:064x}",
        arrival_source=source,
        first_arrived_at_utc=arrived
        or f"2026-10-{(index % 28) + 1:02d}T00:{index % 60:02d}:00Z",
        arrival_count=1,
        primary_classification=classification,
        classification_flags=(),
        bundle_validation=validation,
        dependency_state=dependency,
        alignment_state=alignment,
        evidence_conflict_state=conflict,
        disposition=disposition,
    )


def _inspection(item: CaptureInboxItem, **overrides):
    values = dict(
        item=item,
        promotability="blocked",
        promoted_authority_kinds=(),
        blocked_authority_kinds=(),
        available_authority_kinds=(),
        source_evidence_count=1,
        roomplan_record_count=0,
        raw_mesh_count=0,
        authority_record_count=0,
        promotions=(),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _page(items, *, inspect_map=None, **callbacks):
    """Page over a fabricated snapshot; inspect defaults to a per-item
    namespace so detail rendering runs without a real repository."""
    snapshot = tuple(items)

    def inspect(digest):
        entry = next(
            (item for item in snapshot if item.lineage_digest == digest),
            None,
        )
        if entry is None:
            return None
        if inspect_map and digest in inspect_map:
            return inspect_map[digest]
        promotable = (
            entry.disposition in ("pending", "partially_promoted")
            and entry.scope != "capture-inbox-unassigned"
        )
        return _inspection(
            entry,
            promotability="promotable" if promotable else "blocked",
            available_authority_kinds=("annotations",)
            if promotable
            else (),
        )

    page = CaptureInboxPage(
        lambda: snapshot,
        on_navigate=lambda link: True,
        inspect_item=inspect,
        **callbacks,
    )
    return page, snapshot


def _visible_item_ids(page) -> list[str]:
    """inbox_item_id of every selectable row, in display order."""
    return [
        item.inbox_item_id
        for item in page._displayed_items
        if item is not None
    ]


def _state_filter_index(page, key: str) -> int:
    for index in range(page.inbox_state_combo.count()):
        if page.inbox_state_combo.itemData(index) == key:
            return index
    raise AssertionError(f"no state filter {key}")


def test_queue_state_mirrors_promotion_gates() -> None:
    """The queue vocabulary is derived from the same facets
    _check_promotable vets — blocked means a real gate would fail."""
    base = dict(scope="doc-alpha")
    assert _inbox_queue_state(_item(1, **base)) == "promotable"
    assert _inbox_queue_state(_item(2)) == "pending"
    assert (
        _inbox_queue_state(_item(3, validation="rejected", **base))
        == "blocked"
    )
    assert (
        _inbox_queue_state(
            _item(4, classification="identity_digest_conflict", **base)
        )
        == "blocked"
    )
    assert (
        _inbox_queue_state(_item(5, dependency="unresolved", **base))
        == "blocked"
    )
    assert (
        _inbox_queue_state(_item(6, conflict="open", **base)) == "blocked"
    )
    assert (
        _inbox_queue_state(_item(7, alignment="blocked", **base))
        == "blocked"
    )
    # Alignment 'pending' is not a promotion veto — stays candidate.
    assert (
        _inbox_queue_state(_item(8, alignment="pending", **base))
        == "promotable"
    )
    for disposition, queue in (
        ("deferred", "deferred"),
        ("rejected", "rejected"),
        ("promoted", "processed"),
        ("superseded", "processed"),
        ("partially_promoted", "promotable"),
    ):
        assert _inbox_queue_state(
            _item(9, disposition=disposition, **base)
        ) == queue


def test_default_review_queue_counts_and_zero_state() -> None:
    app = _app()
    items = [
        _item(0),                                   # pending
        _item(1, scope="doc-alpha"),                # promotable
        _item(2, validation="rejected"),            # blocked
        _item(3, disposition="deferred"),           # parked
        _item(4, disposition="rejected"),           # rejected
        _item(5, disposition="promoted", scope="doc-alpha"),  # done
    ]
    page, _ = _page(items)
    try:
        # 要レビュー = pending + promotable + blocked; parked/done hidden.
        assert page.table.rowCount() == 3
        label = page.inbox_count_label.text()
        assert "要レビュー 3 件" in label
        assert "全 6 件" in label
        # The combo exposes per-queue counts for mass-arrival legibility.
        texts = [
            page.inbox_state_combo.itemText(i)
            for i in range(page.inbox_state_combo.count())
        ]
        assert any("要レビュー（3）" in text for text in texts)
        assert any("延期（1）" in text for text in texts)
        # Filter to a queue with nothing in it → honest zero-state.
        page.inbox_search_edit.setText("no-such-thing")
        app.processEvents()
        assert page.table.rowCount() == 0
        assert "条件に一致する項目はありません" in page.detail.text()
        page.inbox_search_edit.clear()
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_state_filter_and_search_at_scale() -> None:
    """10/100/1000-item fixtures refilter instantly and exactly."""
    app = _app()
    for size in (10, 100, 1000):
        items = [
            _item(
                index,
                scope=(
                    "doc-alpha"
                    if index % 5 == 0
                    else "capture-inbox-unassigned"
                ),
                disposition=(
                    "deferred" if index % 7 == 0
                    else "rejected" if index % 11 == 0
                    else "pending"
                ),
                validation="rejected" if index % 13 == 0 else "validated",
            )
            for index in range(size)
        ]
        expected = {
            queue: sum(
                1 for item in items if _inbox_queue_state(item) == queue
            )
            for queue in (
                "pending",
                "promotable",
                "blocked",
                "deferred",
                "rejected",
                "processed",
            )
        }
        page, _ = _page(items)
        try:
            start = time.monotonic()
            page._refilter()
            elapsed = time.monotonic() - start
            assert elapsed < 2.0, f"{size}件の絞り込みが{elapsed:.2f}秒"
            shown = page.table.rowCount()
            assert shown == (
                expected["pending"]
                + expected["promotable"]
                + expected["blocked"]
            )
            for key, queue in (
                ("deferred", "deferred"),
                ("rejected", "rejected"),
                ("blocked", "blocked"),
                ("all", None),
            ):
                page.inbox_state_combo.setCurrentIndex(
                    _state_filter_index(page, key)
                )
                app.processEvents()
                want = (
                    size if queue is None else expected[queue]
                )
                assert page.table.rowCount() == want, (size, key)
            page.inbox_state_combo.setCurrentIndex(
                _state_filter_index(page, "review")
            )
            # Search narrows within the active filter.
            page.inbox_search_edit.setText("series-1")
            app.processEvents()
            want = sum(
                1
                for item in items
                if item.capture_series_id == "series-1"
                and _inbox_queue_state(item)
                in ("pending", "promotable", "blocked")
            )
            assert page.table.rowCount() == want
            page.inbox_search_edit.clear()
        finally:
            page.close()
            page.deleteLater()
            app.processEvents()


def test_sort_modes_and_identity_pinned_selection() -> None:
    """Sorting/grouping/filtering never re-binds actions to a wrong row —
    selection stays pinned to inbox_item_id/lineage_digest."""
    app = _app()
    items = [
        _item(0, scope="doc-beta", arrived="2026-10-01T00:00:00Z"),
        _item(1, scope="doc-alpha", arrived="2026-10-03T00:00:00Z"),
        _item(2, scope="doc-alpha", arrived="2026-10-02T00:00:00Z"),
        _item(3, disposition="deferred"),
        _item(4, disposition="promoted", scope="doc-gamma"),
    ]
    page, snapshot = _page(items)
    try:
        target = items[2]  # promotable (scoped) item
        page.inbox_state_combo.setCurrentIndex(
            _state_filter_index(page, "all")
        )
        app.processEvents()
        assert page._select_delivery_row(target.inbox_item_id)
        assert page._selected_row_data() == (
            target.inbox_item_id,
            target.lineage_digest,
        )
        # Arrival-desc reorders — the same item stays selected.
        page.inbox_sort_combo.setCurrentIndex(1)  # 到着が新しい順
        app.processEvents()
        ids = _visible_item_ids(page)
        # items: 10-01, 10-03, 10-02, default 10-04, default 10-05.
        assert ids[0] == items[4].inbox_item_id  # 10-05 first
        assert ids[-1] == items[0].inbox_item_id  # 10-01 last
        assert page._selected_row_data() == (
            target.inbox_item_id,
            target.lineage_digest,
        )
        # Reorder the source snapshot (notification/sync arrival order)
        # — refresh() keeps the identity-pinned selection.
        page._items = tuple(reversed(snapshot))
        page._rebuild_delivery_rows()
        assert page._selected_row_data() == (
            target.inbox_item_id,
            target.lineage_digest,
        )
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_group_headers_never_selectable_and_next_walk() -> None:
    """Grouped headers carry no identity, cannot be selected, and
    次の未処理を表示 skips them plus parked/finished rows."""
    app = _app()
    items = [
        _item(0),                                   # pending
        _item(1, scope="doc-alpha"),                # promotable
        _item(2, validation="rejected"),            # blocked
        _item(3, disposition="deferred"),
        _item(4, disposition="rejected"),
        _item(5, disposition="promoted", scope="doc-alpha"),
        _item(6, scope="doc-beta"),                 # promotable
    ]
    page, _ = _page(items)
    try:
        page.inbox_state_combo.setCurrentIndex(
            _state_filter_index(page, "all")
        )
        app.processEvents()
        # Group by queue state — header rows interleave the listing.
        page.inbox_group_combo.setCurrentIndex(1)  # 状態
        app.processEvents()
        headers = [
            row
            for row, entry in enumerate(page._displayed_items)
            if entry is None
        ]
        # All six queues are present → six non-selectable headers.
        assert len(headers) == 6
        header_cell = page.table.item(headers[0], 0)
        assert header_cell is not None
        assert "件）" in header_cell.text()
        # A header row can never become the selected identity.
        page.table.selectRow(headers[0])
        app.processEvents()
        assert page._selected_row_data() is None
        # 次の未処理 walks actionable rows only, skipping headers and
        # parked/finished items entirely — 4 actionable items here.
        visited = []
        for _ in range(5):
            page.next_unprocessed_button.click()
            app.processEvents()
            selected = page._selected_row_data()
            if selected:
                visited.append(selected[0])
        actionable_ids = {
            item.inbox_item_id
            for item in items
            if _inbox_queue_state(item)
            in ("pending", "promotable", "blocked")
        }
        assert set(visited[:4]) == actionable_ids
        assert visited[4] == visited[0]  # wrapped around
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_processed_and_rejected_rows_offer_no_approval() -> None:
    """DoD: 処理済み/却下済み must never present an approve path."""
    app = _app()
    items = [
        _item(0, disposition="promoted", scope="doc-alpha"),
        _item(1, disposition="rejected", scope="doc-alpha"),
        _item(2, disposition="superseded", scope="doc-alpha"),
    ]
    promote_calls = []

    def promote(digest, reason):
        promote_calls.append((digest, reason))
        raise AssertionError("must never be invoked from this row")

    page = CaptureInboxPage(
        lambda: tuple(items),
        on_navigate=lambda link: True,
        inspect_item=lambda digest: _inspection(
            next(
                item for item in items if item.lineage_digest == digest
            ),
            promotability="complete",
        ),
        promote_item=promote,
        defer_item=lambda digest, reason: None,
        reject_item=lambda digest, reason: None,
        resume_item=lambda digest: None,
        assign_scope=lambda digest, scope: None,
    )
    try:
        page.inbox_state_combo.setCurrentIndex(
            _state_filter_index(page, "all")
        )
        app.processEvents()
        for item in items:
            assert page._select_delivery_row(item.inbox_item_id)
            app.processEvents()
            assert not page.promote_button.isEnabled()
            assert not page.defer_button.isEnabled()
            assert not page.reject_button.isEnabled()
            assert not page.scope_button.isEnabled()
            assert "次のアクション" in page.detail.text()
        page.promote_button.click()
        assert promote_calls == []
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_detail_lists_ops_blocked_reason_and_next_action() -> None:
    app = _app()
    blocked_item = _item(0, scope="doc-alpha", dependency="unresolved")
    blocked_item = CaptureInboxItem(
        **{
            **blocked_item.model_dump(),
            "dependency_detail": "先行リビジョンが未到着",
        }
    )
    pending_item = _item(1)
    page, _ = _page(
        [blocked_item, pending_item],
        inspect_map={
            blocked_item.lineage_digest: _inspection(
                blocked_item,
                promotability="blocked",
                blocked_authority_kinds=("measurements",),
            )
        },
        defer_item=lambda digest, reason: None,
        reject_item=lambda digest, reason: None,
        resume_item=lambda digest: None,
        assign_scope=lambda digest, scope: None,
    )
    try:
        assert page._select_delivery_row(blocked_item.inbox_item_id)
        app.processEvents()
        text = page.detail.text()
        assert "適用可能な操作" in text
        assert "不足・ブロック理由" in text
        assert "先行リビジョンが未到着" in text
        assert "次のアクション" in text
        assert "延期" in text  # blocked items may still be deferred
        # An unassigned pending item surfaces its missing scope instead.
        assert page._select_delivery_row(pending_item.inbox_item_id)
        app.processEvents()
        text = page.detail.text()
        assert "プロジェクト未割当" in text
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_secondary_actions_collapse_under_narrow_width() -> None:
    """高DPI/狭幅では二次操作が 操作 ▾ メニューに折りたたまれる。"""
    app = _app()
    page, _ = _page([_item(0), _item(1, scope="doc-alpha")])
    try:
        page.resize(1280, 800)
        page._sync_action_layout()
        assert not page.defer_button.isHidden()
        assert page.actions_overflow.isHidden()
        # Hidden offscreen widgets get no resizeEvent — drive the
        # same path the real resize handler calls.
        page.resize(700, 800)
        page._sync_action_layout()
        assert page.defer_button.isHidden()
        assert page.promote_button.isHidden()
        assert page.scope_button.isHidden()
        assert not page.actions_overflow.isHidden()
        # The menu exposes the same handlers — identity still pinned.
        page._select_delivery_row(
            page._displayed_items[0].inbox_item_id
        )
        app.processEvents()
        calls = []
        page._defer_item = lambda digest, reason: calls.append(digest)
        page._sync_actions(page._last_inspection)
        orig = QInputDialog.getText
        QInputDialog.getText = staticmethod(lambda *a, **k: ("後で", True))
        try:
            page._menu_actions["defer"].trigger()
        finally:
            QInputDialog.getText = orig
        assert calls
        page.resize(1280, 800)
        page._sync_action_layout()
        assert not page.defer_button.isHidden()
        assert page.actions_overflow.isHidden()
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_inbox_focus_reveals_filtered_row() -> None:
    """Deep links land on deferred/processed rows hidden by the default
    要レビュー queue — the filter resets rather than reporting missing."""
    app = _app()
    items = [
        _item(0),
        _item(1, disposition="deferred"),
        _item(2, disposition="promoted", scope="doc-alpha"),
    ]
    page, _ = _page(items)
    try:
        target = NavigationTarget(
            kind=NavigationTargetKind.CAPTURE_INBOX_ITEM,
            object_ids=(items[1].inbox_item_id,),
        )
        result = inbox_focus(page, target)
        assert result.focused
        assert page._selected_row_data() == (
            items[1].inbox_item_id,
            items[1].lineage_digest,
        )
        # Missing ids stay honest even after revealing.
        missing = NavigationTarget(
            kind=target.kind,
            object_ids=("capture-inbox-item:" + "f" * 64,),
        )
        result = inbox_focus(page, missing)
        assert not result.focused
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_real_repository_actions_keep_exact_identity(tmp_path) -> None:
    """Real staged items: defer drops the row out of the review queue,
    selection auto-advances, and assign_scope still runs on the exact
    digest — search/sort never become approval authority."""
    app = _app()
    scene = SceneRepository(tmp_path / "cad.sqlite3")
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    staged = []
    for name, revision in (
        ("aaa", "11111111-2222-4333-8444-555555555555"),
        ("bbb", "66666666-7777-4888-8999-000000000000"),
    ):
        plan, payloads, _ = support.plan_and_payloads(
            tmp_path / name,
            manifest_overrides={"capture_revision_id": revision},
        )
        ingestion.ingest(plan, payloads)
        staged.append(
            inbox.stage(
                CaptureIngestionPlan.model_validate(plan),
                arrival_source="file_import",
            )
        )
    project = SimpleNamespace(
        project_id="proj-alpha",
        document_id="doc-alpha",
        display_name="Alpha",
    )
    page = CaptureInboxPage(
        inbox.list_items,
        on_navigate=lambda link: True,
        inspect_item=inbox.inspect,
        defer_item=inbox.defer,
        reject_item=inbox.reject,
        resume_item=inbox.resume,
        list_projects=lambda: (project,),
        assign_scope=inbox.assign_scope,
    )
    try:
        assert page.table.rowCount() == 2
        first, second = staged
        # Both fixtures share the series — search narrows on the
        # revision id facet so acting hits exactly that row.
        page.inbox_search_edit.setText(second.item.capture_revision_id[:13])
        app.processEvents()
        assert page.table.rowCount() == 1
        page.inbox_search_edit.clear()
        app.processEvents()
        assert page.table.rowCount() == 2
        # Identity-pinned selection, then defer: the acted row leaves the
        # 要レビュー queue and selection auto-advances to the next
        # unprocessed item rather than a stale/wrong row.
        assert page._select_delivery_row(second.item.inbox_item_id)
        monkey_reason = lambda *a, **k: ("後で確認", True)
        original = QInputDialog.getText
        QInputDialog.getText = staticmethod(monkey_reason)
        try:
            page.defer_button.click()
        finally:
            QInputDialog.getText = original
        app.processEvents()
        assert inbox.get(second.lineage_digest).disposition == "deferred"
        assert "要レビュー 1 件" in page.inbox_count_label.text()
        selected = page._selected_row_data()
        assert selected == (
            first.item.inbox_item_id,
            first.lineage_digest,
        )
        # Assign on the auto-advanced selection binds the RIGHT item.
        index = page.scope_combo.findData("doc-alpha")
        assert index > 0
        page.scope_combo.setCurrentIndex(index)
        page.scope_button.click()
        app.processEvents()
        assert inbox.get(first.lineage_digest).scope == "doc-alpha"
        assert inbox.get(second.lineage_digest).scope == (
            "capture-inbox-unassigned"
        )
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()
