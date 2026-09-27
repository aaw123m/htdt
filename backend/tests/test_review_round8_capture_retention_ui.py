"""Round-8 regression: RetentionPolicyWidget — the Settings > 保持管理
tab surfacing CaptureRetentionService (round-6 deferred wiring): inventory,
revision picker, dry-run plan, confirmed purge."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capture_lifecycle import (  # noqa: E402
    REVISION_ID,
    _promotion_request,
    _repositories,
)

from htdt.capture_ingestion_transaction import (  # noqa: E402
    CaptureIngestionRepository,
)
from htdt.capture_retention import CaptureRetentionService  # noqa: E402
from htdt.capture_retention_ui import RetentionPolicyWidget  # noqa: E402


@pytest.fixture()
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def seeded(tmp_path: Path):
    return _repositories(tmp_path)


def test_widget_lists_inventory_and_revisions(qapp, seeded) -> None:
    scene, _capture, _p, _r, _b, _root = seeded
    widget = RetentionPolicyWidget(CaptureRetentionService(scene))
    try:
        assert "リビジョン 1 件" in widget.inventory_label.text()
        assert widget.revision_combo.count() == 1
        assert widget.revision_combo.currentData() == REVISION_ID
        assert widget.plan_button.isEnabled()
        # Purge stays disarmed until a ready dry-run exists.
        assert not widget.purge_button.isEnabled()
    finally:
        widget.close()
        widget.deleteLater()


def test_dry_run_shows_plan_and_arms_purge(qapp, seeded) -> None:
    scene, _capture, _p, _r, _b, _root = seeded
    service = CaptureRetentionService(scene)
    widget = RetentionPolicyWidget(service)
    try:
        widget._run_dry_run()
        text = widget.plan_label.text()
        assert "削除可能" in text
        assert "エビデンス 7 件" in text
        assert widget.plan_label.isVisibleTo(widget)
        assert widget.purge_button.isEnabled()
        # Still nothing deleted.
        assert service.inventory().ingestion_run_count == 1
    finally:
        widget.close()
        widget.deleteLater()


def test_confirmed_purge_removes_revision(
    qapp, seeded, monkeypatch
) -> None:
    scene, capture, _p, _r, _b, _root = seeded
    service = CaptureRetentionService(scene)
    widget = RetentionPolicyWidget(service)
    monkeypatch.setattr(
        QMessageBox, "exec", lambda _self: QMessageBox.StandardButton.Yes
    )
    try:
        widget._run_dry_run()
        widget._confirm_and_purge()
        assert "削除しました" in widget.plan_label.text()
        assert service.plan_capture_revision_purge(REVISION_ID).status == (
            "absent"
        )
        assert widget.revision_combo.count() == 0
    finally:
        widget.close()
        widget.deleteLater()


def test_busy_gate_disarms_purge(qapp, seeded) -> None:
    scene, _capture, _p, _r, _b, _root = seeded
    busy = {"flag": False}
    widget = RetentionPolicyWidget(
        CaptureRetentionService(scene),
        is_busy=lambda: busy["flag"],
    )
    try:
        widget._run_dry_run()
        assert widget.purge_button.isEnabled()
        busy["flag"] = True
        widget._refresh_actions()
        assert not widget.purge_button.isEnabled()
        assert not widget.plan_button.isEnabled()
    finally:
        widget.close()
        widget.deleteLater()


def test_blocked_revision_never_arms_purge(
    qapp, tmp_path: Path, seeded
) -> None:
    """A revision still referenced by a promotion reports blocked and
    cannot be purged through the widget."""
    scene, capture, promotion, run_id, binding_ids, _root = seeded
    service = CaptureRetentionService(scene)
    widget = RetentionPolicyWidget(service)
    try:
        promoted = promotion.promote(
            _promotion_request(
                capture, run_id, scene,
                raw_mesh_binding_id=binding_ids[0],
            )
        )
        widget._run_dry_run()
        assert "削除できません" in widget.plan_label.text()
        assert promoted.promotion_id in widget.plan_label.text()
        assert not widget.purge_button.isEnabled()
    finally:
        widget.close()
        widget.deleteLater()


def test_empty_catalog_renders_empty_picker(
    qapp, tmp_path: Path
) -> None:
    from htdt.cad_repository import SceneRepository

    scene = SceneRepository(tmp_path / "cad.sqlite3")
    widget = RetentionPolicyWidget(CaptureRetentionService(scene))
    try:
        assert "リビジョン 0 件" in widget.inventory_label.text()
        assert widget.revision_combo.count() == 0
        assert not widget.plan_button.isEnabled()
    finally:
        widget.close()
        widget.deleteLater()
