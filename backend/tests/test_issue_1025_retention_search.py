"""#1025 — capture retention candidates as a searchable/groupable list.

The flat QComboBox picker made same-prefix/one-of-many-series revisions easy
to confuse at scale. This file covers the searchable/groupable QTreeWidget
that replaced it: filter+sort+group by ID/series/project/date/count/bytes,
selection pinned by capture_revision_id across scope changes, dry-run
invalidation, itemized plan display, and UIA/narrow-UI reachability.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_accessible_labels import _unnamed_controls  # noqa: E402
from test_capture_lifecycle import (  # noqa: E402
    REVISION_ID,
    SERIES_ID,
    _ingestion_fixture,
    _promotion_request,
    _repositories,
)

from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_schema import connect_sqlite  # noqa: E402
from htdt.capture_ingestion_transaction import (  # noqa: E402
    CaptureIngestionRepository,
)
from htdt.capture_retention import (  # noqa: E402
    CaptureRetentionService,
    CaptureRevisionListing,
)
from htdt.capture_retention_ui import RetentionPolicyWidget  # noqa: E402


@pytest.fixture()
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _revision(
    revision_id: str,
    *,
    series: str = SERIES_ID,
    runs: int = 1,
    latest: str = "2026-09-01T00:00:00Z",
    evidence: int = 1,
    payload: int = 1024,
    docs: tuple[str, ...] = (),
) -> CaptureRevisionListing:
    return CaptureRevisionListing(
        capture_revision_id=revision_id,
        capture_series_id=series,
        ingestion_run_count=runs,
        latest_recorded_at_utc=latest,
        linked_evidence_count=evidence,
        linked_payload_bytes=payload,
        assigned_document_ids=docs,
    )


def _plan(revision_id: str, status: str = "ready", dependents=()):
    return SimpleNamespace(
        status=status,
        capture_revision_id=revision_id,
        ingestion_run_ids=("run-1",),
        lineage_digests=("d" * 64,),
        deletable_source_evidence_ids=("e1", "e2"),
        retained_source_evidence_ids=(),
        deletable_mesh_binding_ids=("b1",),
        retained_mesh_binding_ids=(),
        deletable_authority_record_ids=(),
        retained_authority_record_ids=(),
        deletable_coordinate_authority_ids=("c1",),
        retained_coordinate_authority_ids=(),
        roomplan_record_count=3,
        blocking_dependents=tuple(dependents),
        reclaimable_bytes=2048,
        reclaimed_blob_sha256=("ab",),
    )


def _service(revisions, *, status: str = "ready") -> Mock:
    service = Mock()
    service.inventory.return_value = SimpleNamespace(
        capture_revision_count=len(revisions),
        ingestion_run_count=sum(r.ingestion_run_count for r in revisions),
        source_evidence_count=sum(
            r.linked_evidence_count for r in revisions
        ),
        source_payload_bytes=sum(
            r.linked_payload_bytes for r in revisions
        ),
        content_blob_count=0,
        content_blob_bytes=0,
    )
    service.list_capture_revisions.return_value = list(revisions)
    service.plan_capture_revision_purge.side_effect = (
        lambda revision_id: _plan(revision_id, status)
    )
    service.purge_capture_revision.side_effect = (
        lambda revision_id: _plan(revision_id, status)
    )
    return service


def _widget(revisions, *, status: str = "ready", is_busy=None):
    widget = RetentionPolicyWidget(
        _service(revisions, status=status), is_busy=is_busy
    )
    return widget


def _insert_runs(scene: SceneRepository, rows) -> None:
    """Bulk-seed run rows for scale tests — the listing reads only the
    normalized run table, so runs need no payloads."""
    # Ensure capture tables exist.
    CaptureIngestionRepository(scene)
    with closing(connect_sqlite(scene.path)) as connection:
        with connection:
            for revision_id, series_id, run_id, recorded in rows:
                lineage = f"lineage-{run_id}"
                connection.execute(
                    "INSERT INTO capture_ingestion_lineages "
                    "(lineage_digest) VALUES (?)",
                    (lineage,),
                )
                connection.execute(
                    """
                    INSERT INTO capture_ingestion_runs (
                        ingestion_run_id, lineage_digest, plan_sha256,
                        bundle_digest, capture_revision_id,
                        capture_series_id, parent_revision_id,
                        capture_session_ids_json,
                        coordinate_space_ids_json,
                        ingestor_name, ingestor_version,
                        configuration_digest, plan_json, recorded_at_utc
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        lineage,
                        "a" * 64,
                        f"bundle-{revision_id}",
                        revision_id,
                        series_id,
                        None,
                        "[]",
                        "[]",
                        "htdt-capture-test",
                        "0.0.1",
                        "c" * 64,
                        "{}",
                        recorded,
                    ),
                )


# ---- listing fields --------------------------------------------------------


def test_listing_carries_search_fields(qapp, tmp_path: Path) -> None:
    scene, capture, _p, _r, _b, _root = _repositories(tmp_path)
    service = CaptureRetentionService(scene)
    (listing,) = service.list_capture_revisions()
    assert listing.capture_revision_id == REVISION_ID
    assert listing.capture_series_id == SERIES_ID
    assert listing.ingestion_run_count == 1
    # The fixture ingests 7 evidence payloads.
    assert listing.linked_evidence_count == 7
    assert listing.linked_payload_bytes > 0
    # No promotion → no project assignment (exact authority only).
    assert listing.assigned_document_ids == ()


def test_project_assignment_uses_exact_promotion_authority(
    qapp, tmp_path: Path
) -> None:
    """A revision promoted into a document reports that document id; an
    un-promoted sibling stays unassigned."""
    scene, capture, promotion, run_id, binding_ids, _root = _repositories(
        tmp_path
    )
    promotion.promote(
        _promotion_request(
            capture, run_id, scene, raw_mesh_binding_id=binding_ids[0]
        )
    )
    # A sibling revision that stays un-promoted reports no assignment.
    plan_b, payloads_b = _ingestion_fixture(
        revision_id="40000000-0000-4000-8000-000000000002",
        series_id="40000000-0000-4000-8000-000000000001",
    )
    capture.ingest(plan_b, payloads_b)
    service = CaptureRetentionService(scene)
    by_id = {
        r.capture_revision_id: r for r in service.list_capture_revisions()
    }
    assert by_id[REVISION_ID].assigned_document_ids == ("capture-doc",)
    assert by_id[
        "40000000-0000-4000-8000-000000000002"
    ].assigned_document_ids == ()


# ---- listing scale ---------------------------------------------------------


def test_listing_handles_1000_revisions(qapp, tmp_path: Path) -> None:
    scene = SceneRepository(tmp_path / "cad.sqlite3")
    rows = [
        (
            f"{i:08d}-0000-4000-8000-{i:012d}",
            f"series-{i % 10:02d}",
            f"run-{i:05d}",
            f"2026-09-{(i % 28) + 1:02d}T00:00:00Z",
        )
        for i in range(1000)
    ]
    _insert_runs(scene, rows)
    service = CaptureRetentionService(scene)
    revisions = service.list_capture_revisions()
    assert len(revisions) == 1000
    assert {r.capture_series_id for r in revisions} == {
        f"series-{i:02d}" for i in range(10)
    }


# ---- widget: filtering / sorting / grouping ---------------------------------


def test_filter_narrows_and_pins_selection(qapp) -> None:
    revisions = [
        _revision("rev-aaaa-0001", series="series-alpha"),
        _revision("rev-bbbb-0002", series="series-beta"),
        _revision("rev-cccc-0003", series="series-alpha"),
    ]
    widget = _widget(revisions)
    try:
        assert set(widget.listed_revision_ids()) == {
            "rev-aaaa-0001",
            "rev-bbbb-0002",
            "rev-cccc-0003",
        }
        assert widget.select_revision("rev-cccc-0003")
        # Filter to the alpha series — beta row drops out, selection stays.
        widget.search_edit.setText("alpha")
        assert set(widget.listed_revision_ids()) == {
            "rev-aaaa-0001",
            "rev-cccc-0003",
        }
        assert widget._selected_revision_id() == "rev-cccc-0003"
        # Filter hides the pinned selection: nothing else is auto-selected
        # and the reason is surfaced.
        widget.search_edit.setText("beta")
        assert widget.listed_revision_ids() == ("rev-bbbb-0002",)
        assert widget._selected_revision_id() is None
        assert not widget.plan_button.isEnabled()
        assert "非表示" in widget.selection_note.text()
        # Clearing restores the pinned selection — never a different row.
        widget.search_edit.clear()
        assert widget._selected_revision_id() == "rev-cccc-0003"
        assert not widget.selection_note.isVisibleTo(widget)
    finally:
        widget.close()
        widget.deleteLater()


def test_sort_and_group_keep_pinned_selection(qapp) -> None:
    revisions = [
        _revision(
            "rev-aaaa-0001",
            series="series-alpha",
            latest="2026-09-03T00:00:00Z",
        ),
        _revision(
            "rev-bbbb-0002",
            series="series-beta",
            latest="2026-09-01T00:00:00Z",
        ),
        _revision(
            "rev-cccc-0003",
            series="series-alpha",
            latest="2026-09-02T00:00:00Z",
        ),
    ]
    widget = _widget(revisions)
    try:
        assert widget.select_revision("rev-bbbb-0002")
        # Sort by id — order changes, selection stays pinned to the id.
        index = widget.sort_combo.findData("revision_id")
        widget.sort_combo.setCurrentIndex(index)
        assert widget.listed_revision_ids() == (
            "rev-aaaa-0001",
            "rev-bbbb-0002",
            "rev-cccc-0003",
        )
        assert widget._selected_revision_id() == "rev-bbbb-0002"
        # Group by series — tree gains non-selectable group headers and the
        # pinned selection survives regrouping.
        index = widget.group_combo.findData("series")
        widget.group_combo.setCurrentIndex(index)
        top_texts = [
            widget.revision_tree.topLevelItem(i).text(0)
            for i in range(widget.revision_tree.topLevelItemCount())
        ]
        assert sorted(top_texts) == [
            "series-alpha（2 件）",
            "series-beta（1 件）",
        ]
        assert set(widget.listed_revision_ids()) == {
            "rev-aaaa-0001",
            "rev-bbbb-0002",
            "rev-cccc-0003",
        }
        assert widget._selected_revision_id() == "rev-bbbb-0002"
    finally:
        widget.close()
        widget.deleteLater()


def test_colliding_id_prefixes_select_by_full_id(qapp) -> None:
    """Two revisions sharing an 8-char prefix are displayed with their
    full ids and always resolve to the exact selection."""
    first = "abcdef12-0000-4000-8000-000000000001"
    second = "abcdef12-9999-4000-8000-000000000002"
    widget = _widget([_revision(first), _revision(second)])
    try:
        texts = [
            widget.revision_tree.topLevelItem(i).text(0)
            for i in range(widget.revision_tree.topLevelItemCount())
        ]
        assert sorted(texts) == sorted([first, second])
        assert widget.select_revision(second)
        assert widget._selected_revision_id() == second
        widget._run_dry_run()
        widget._service.plan_capture_revision_purge.assert_called_with(
            second
        )
        assert second in widget.plan_label.text()
    finally:
        widget.close()
        widget.deleteLater()


def test_project_grouping_uses_document_assignment(qapp) -> None:
    revisions = [
        _revision("rev-1", docs=("doc-a",)),
        _revision("rev-2", docs=("doc-b",)),
        _revision("rev-3", docs=()),
    ]
    widget = _widget(revisions)
    try:
        index = widget.group_combo.findData("project")
        widget.group_combo.setCurrentIndex(index)
        top_texts = [
            widget.revision_tree.topLevelItem(i).text(0)
            for i in range(widget.revision_tree.topLevelItemCount())
        ]
        assert sorted(top_texts) == [
            "doc-a（1 件）",
            "doc-b（1 件）",
            "（プロジェクト未割当）（1 件）",
        ]
    finally:
        widget.close()
        widget.deleteLater()


# ---- plan arming / invalidation ---------------------------------------------


def test_dry_run_shows_itemized_plan(qapp) -> None:
    widget = _widget([_revision("rev-1")])
    try:
        assert widget.select_revision("rev-1")
        widget._run_dry_run()
        text = widget.plan_label.text()
        assert "削除可能" in text
        assert "対象リビジョン: rev-1" in text
        assert "リンクされた証拠" in text
        assert "削除対象 2 件" in text
        assert "保持義務" in text
        assert "参照・再利用先" in text
        assert "回収可能" in text
        assert widget.purge_button.isEnabled()
    finally:
        widget.close()
        widget.deleteLater()


def test_scope_change_invalidates_plan(qapp) -> None:
    """Editing the filter changes the selection scope — a plan computed
    for the old scope can never be executed."""
    widget = _widget([_revision("rev-1"), _revision("rev-2")])
    try:
        assert widget.select_revision("rev-1")
        widget._run_dry_run()
        assert widget.purge_button.isEnabled()
        widget.search_edit.setText("rev")
        assert not widget.purge_button.isEnabled()
        # Re-selecting alone does not re-arm; a fresh dry-run is required.
        assert widget._selected_revision_id() == "rev-1"
        assert not widget.purge_button.isEnabled()
        widget._run_dry_run()
        assert widget.purge_button.isEnabled()
    finally:
        widget.close()
        widget.deleteLater()


def test_refresh_invalidates_plan_and_keeps_selection(qapp) -> None:
    """A refresh re-reads the store — underlying references may have
    changed, so the computed plan is dropped but the id-pinned selection
    stays."""
    revisions = [_revision("rev-1"), _revision("rev-2")]
    widget = _widget(revisions)
    try:
        assert widget.select_revision("rev-2")
        widget._run_dry_run()
        assert widget.purge_button.isEnabled()
        # Underlying store gained a revision (e.g. import finished).
        revisions.append(_revision("rev-3"))
        widget.refresh()
        assert widget._selected_revision_id() == "rev-2"
        assert not widget.purge_button.isEnabled()
        assert not widget.plan_label.isVisibleTo(widget)
    finally:
        widget.close()
        widget.deleteLater()


def test_disappeared_revision_clears_selection(qapp) -> None:
    """After the pinned revision is purged/removed, nothing else is
    auto-selected — the user must pick again explicitly."""
    revisions = [_revision("rev-1"), _revision("rev-2")]
    widget = _widget(revisions)
    try:
        assert widget.select_revision("rev-1")
        widget._run_dry_run()
        assert widget.purge_button.isEnabled()
        # Simulate the purge: the service now omits rev-1.
        widget._service.list_capture_revisions.return_value = [
            revisions[1]
        ]
        widget.refresh()
        assert widget._selected_revision_id() is None
        assert not widget.purge_button.isEnabled()
        assert not widget.plan_button.isEnabled()
        assert "ありません" in widget.selection_note.text()
    finally:
        widget.close()
        widget.deleteLater()


def test_blocked_plan_never_arms_purge(qapp) -> None:
    widget = _widget([_revision("rev-1")], status="blocked")
    try:
        assert widget.select_revision("rev-1")
        widget._run_dry_run()
        assert "削除できません" in widget.plan_label.text()
        assert not widget.purge_button.isEnabled()
    finally:
        widget.close()
        widget.deleteLater()


def test_busy_gate_blocks_plan_and_purge(qapp) -> None:
    busy = {"flag": True}
    widget = _widget([_revision("rev-1")], is_busy=lambda: busy["flag"])
    try:
        assert widget.select_revision("rev-1")
        assert not widget.plan_button.isEnabled()
        assert not widget.purge_button.isEnabled()
        busy["flag"] = False
        widget._refresh_actions()
        assert widget.plan_button.isEnabled()
        widget._run_dry_run()
        assert widget.purge_button.isEnabled()
        busy["flag"] = True
        widget._refresh_actions()
        assert not widget.purge_button.isEnabled()
    finally:
        widget.close()
        widget.deleteLater()


def test_confirmed_purge_targets_only_selected(qapp, monkeypatch) -> None:
    """End-to-end: select → scan → confirm purges exactly the pinned
    revision id — never a row that merely sits in the same list."""
    revisions = [_revision("rev-1"), _revision("rev-2")]
    widget = _widget(revisions)
    monkeypatch.setattr(
        QMessageBox, "exec", lambda _self: QMessageBox.StandardButton.Yes
    )
    try:
        assert widget.select_revision("rev-2")
        widget._run_dry_run()
        widget._confirm_and_purge()
        widget._service.purge_capture_revision.assert_called_once_with(
            "rev-2"
        )
    finally:
        widget.close()
        widget.deleteLater()


def test_project_switch_selects_nothing_stale(qapp, tmp_path: Path) -> None:
    """Two projects are two repositories — a widget built for project B
    can never carry over project A's selection."""
    scene_a = SceneRepository(tmp_path / "a" / "cad.sqlite3")
    scene_b = SceneRepository(tmp_path / "b" / "cad.sqlite3")
    _insert_runs(scene_a, [("rev-a-1", "s-a", "run-a-1", "2026-09-01T00:00:00Z")])
    _insert_runs(scene_b, [("rev-b-1", "s-b", "run-b-1", "2026-09-01T00:00:00Z")])
    widget_a = RetentionPolicyWidget(CaptureRetentionService(scene_a))
    widget_b = RetentionPolicyWidget(CaptureRetentionService(scene_b))
    try:
        assert widget_a.select_revision("rev-a-1")
        # The B widget never lists or selects project A's revision.
        assert widget_b.listed_revision_ids() == ("rev-b-1",)
        assert not widget_b.select_revision("rev-a-1")
        assert widget_b._selected_revision_id() is None
    finally:
        widget_a.close()
        widget_a.deleteLater()
        widget_b.close()
        widget_b.deleteLater()


def test_1000_rows_widget_filters_and_pins(qapp) -> None:
    revisions = [
        _revision(
            f"{i:08d}-0000-4000-8000-{i:012d}",
            series=f"series-{i % 10:02d}",
            latest=f"2026-09-{(i % 28) + 1:02d}T00:00:00Z",
        )
        for i in range(1000)
    ]
    widget = _widget(revisions)
    try:
        assert len(widget.listed_revision_ids()) == 1000
        assert "1000" in widget.count_label.text()
        target = revisions[432].capture_revision_id
        widget.search_edit.setText(target)
        assert widget.listed_revision_ids() == (target,)
        assert widget.select_revision(target)
        assert widget._selected_revision_id() == target
        widget._run_dry_run()
        assert widget.purge_button.isEnabled()
        # Clearing the filter is a scope change: the plan disarms even
        # though the pinned selection survives.
        widget.search_edit.clear()
        assert widget._selected_revision_id() == target
        assert not widget.purge_button.isEnabled()
        # Re-scan in the new scope, then regroup: regrouping alone keeps
        # both the armed plan and the pinned selection.
        widget._run_dry_run()
        assert widget.purge_button.isEnabled()
        index = widget.group_combo.findData("series")
        widget.group_combo.setCurrentIndex(index)
        assert widget._selected_revision_id() == target
        assert widget.purge_button.isEnabled()
    finally:
        widget.close()
        widget.deleteLater()


# ---- narrow UI / UIA --------------------------------------------------------


def test_narrow_ui_keeps_controls_reachable(qapp) -> None:
    widget = _widget([_revision("rev-1")])
    try:
        widget.resize(360, 640)
        widget.show()
        qapp.processEvents()
        assert widget.search_edit.isVisible()
        assert widget.revision_tree.isVisible()
        assert widget.plan_button.isVisible()
        assert widget.purge_button.isVisible()
        # Every interactive control exposes a name to assistive tech.
        leftovers = list(_unnamed_controls(widget))
        assert leftovers == [], [c.objectName() for c in leftovers]
        assert widget.select_revision("rev-1")
        widget._run_dry_run()
        assert widget.purge_button.isEnabled()
    finally:
        widget.close()
        widget.deleteLater()
