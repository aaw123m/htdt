"""REV65-SHELL regression tests — Capture Inbox reconciliation surface.

Covers the error-boundary contracts on the field-return detail pane:
authority/integrity failures propagate instead of degrading to a fake
'nothing to decide' state, expected failures degrade to an honest
operator-mapped note (never raw exception text), and the rebase-decision
picker can never bind a decision to the wrong task on a label collision.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QInputDialog

sys.path.insert(0, str(Path(__file__).resolve().parent))

from htdt.application_pages import CaptureInboxPage
from htdt.cad_acoustic_metrology_repository import (
    AcousticMetrologyIntegrityError,
)
from htdt.user_facing_error import operation_error_message


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _contribution(**overrides) -> SimpleNamespace:
    base = {
        "contribution_id": "cid-rev65",
        "validation_state": "validated",
        "routing": "matched",
        "matched_project_id": "proj-alpha",
        "mission_id": "mission-1",
        "plan_sha256": "cd" * 32,
        "detail": None,
        "artifact_sha256": "ef" * 32,
        "artifact_retained": True,
        "recorded_at_utc": "2026-10-07T00:00:00Z",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _close(page: CaptureInboxPage, app: QApplication) -> None:
    page.close()
    page.deleteLater()
    app.processEvents()


def test_resolve_failure_surfaces_mapped_message_not_raw_text() -> None:
    """A resolver failure must render operation_error_message output —
    raw exception text (English internals) never reaches the pane."""

    app = _app()
    contribution = _contribution()

    def _broken(item):
        raise OSError("manifest crc mismatch at offset 0x2f")

    page = CaptureInboxPage(
        lambda: (),
        on_navigate=lambda link: True,
        list_contributions=lambda: (contribution,),
        resolve_return_evidence=_broken,
    )
    try:
        page.contribution_table.selectRow(0)
        app.processEvents()
        text = page.contribution_detail.text()
        assert "証跡: 解読失敗" in text
        assert operation_error_message(OSError()) in text
        assert "crc mismatch" not in text
        assert "0x2f" not in text
    finally:
        _close(page, app)


def test_resolve_authority_failure_propagates_instead_of_degrading() -> None:
    """A sealed-store integrity failure is not a decode failure — it must
    propagate to the uncaught boundary, never read as 解読失敗."""

    app = _app()
    contribution = _contribution()

    def _tampered(item):
        raise AcousticMetrologyIntegrityError("seal mismatch row 7")

    page = CaptureInboxPage(
        lambda: (),
        on_navigate=lambda link: True,
        list_contributions=lambda: (contribution,),
        resolve_return_evidence=_tampered,
    )
    try:
        # Qt absorbs slot exceptions into sys.excepthook — select the row
        # with the signal detached, then invoke the sync directly so the
        # propagation is observable to the test.
        page.contribution_table.itemSelectionChanged.disconnect(
            page._sync_contribution_detail
        )
        page.contribution_table.selectRow(0)
        app.processEvents()
        with pytest.raises(AcousticMetrologyIntegrityError):
            page._sync_contribution_detail()
    finally:
        _close(page, app)


def test_rebase_context_authority_failure_propagates() -> None:
    """An integrity failure resolving the decision surface must not
    masquerade as 'nothing to decide' via a silent context=None."""

    app = _app()
    contribution = _contribution(artifact_retained=False)

    def _tampered(item):
        raise AcousticMetrologyIntegrityError("seal mismatch row 3")

    page = CaptureInboxPage(
        lambda: (),
        on_navigate=lambda link: True,
        list_contributions=lambda: (contribution,),
        rebase_context=_tampered,
        rebase_record=lambda *args: None,
        apply_record=lambda *args: None,
    )
    try:
        page.contribution_table.itemSelectionChanged.disconnect(
            page._sync_contribution_detail
        )
        page.contribution_table.selectRow(0)
        app.processEvents()
        with pytest.raises(AcousticMetrologyIntegrityError):
            page._sync_contribution_detail()
    finally:
        _close(page, app)


def test_rebase_context_expected_failure_degrades_closed() -> None:
    """Ordinary store failures still degrade to 'no context' — buttons
    disabled — but the failure is reported, not swallowed silently."""

    app = _app()
    contribution = _contribution(artifact_retained=False)

    def _unreadable(item):
        raise OSError("device not ready")

    page = CaptureInboxPage(
        lambda: (),
        on_navigate=lambda link: True,
        list_contributions=lambda: (contribution,),
        rebase_context=_unreadable,
        rebase_record=lambda *args: None,
        apply_record=lambda *args: None,
    )
    try:
        page.contribution_table.selectRow(0)
        app.processEvents()
        assert page._active_rebase_context is None
        assert not page.rebase_button.isEnabled()
        assert not page.apply_button.isEnabled()
    finally:
        _close(page, app)


def test_rebase_picker_binds_the_picked_task_on_label_collision(
    monkeypatch,
) -> None:
    """Two undecided tasks sharing the 8-char id prefix and reason must
    not silently record the decision against whichever sorts first."""

    app = _app()
    contribution = _contribution(artifact_retained=False)
    undecided = (
        SimpleNamespace(
            task_id="aaaaaaaa-1111-4000-8000-000000000001",
            reason="対象が変更されています",
        ),
        SimpleNamespace(
            task_id="aaaaaaaa-2222-4000-8000-000000000002",
            reason="対象が変更されています",
        ),
    )
    context = SimpleNamespace(
        undecided=undecided,
        revision=SimpleNamespace(
            document=SimpleNamespace(
                entities=(
                    SimpleNamespace(
                        entity_id="e-1", name="メインL", kind="speaker"
                    ),
                )
            )
        ),
    )
    recorded = []
    page = CaptureInboxPage(
        lambda: (),
        on_navigate=lambda link: True,
        list_contributions=lambda: (contribution,),
        rebase_context=lambda item: context,
        rebase_record=lambda *args: recorded.append(args),
    )
    picked_lists = []

    def fake_get_item(parent, title, label, items, current, editable):
        items = list(items)
        picked_lists.append(items)
        # First dialog is the task picker — choose the SECOND task.
        return (items[1] if len(picked_lists) == 1 else items[0], True)

    answers = iter(("理由テキスト", "決定者テスト"))
    monkeypatch.setattr(
        QInputDialog, "getItem", staticmethod(fake_get_item)
    )
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        staticmethod(lambda *args, **kwargs: (next(answers), True)),
    )
    try:
        page.contribution_table.selectRow(0)
        app.processEvents()
        page._record_rebase_decision()
        assert recorded == [
            (
                contribution,
                undecided[1].task_id,
                "e-1",
                "理由テキスト",
                "決定者テスト",
            )
        ]
    finally:
        _close(page, app)
