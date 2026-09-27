"""Round-8 regression: CommissioningWizard summary links queue behind the
modal instead of navigating while ``exec()`` is still open."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from htdt.commissioning_wizard import CommissioningWizard
from htdt.cad_repository import SceneRepository
from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId


@pytest.fixture
def qapp():
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


@pytest.fixture
def repository(tmp_path):
    return SceneRepository(tmp_path / "cad.sqlite3")


def test_summary_link_queues_and_closes_instead_of_navigating_mid_exec(
    qapp, repository, tmp_path
):
    wizard = CommissioningWizard(repository, "doc-1", data_dir=tmp_path)
    emitted: list[object] = []
    wizard.navigate_requested.connect(emitted.append)

    link = WorkspaceDeepLink(WorkspaceId.MEASUREMENT, "quality")
    wizard._queue_navigation(link)

    # The signal still fires (non-modal embedders keep working)...
    assert emitted == [link]
    # ...but the dialog closes, and the link is handed to the composition
    # for post-exec() navigation rather than landing behind the modal.
    assert wizard.result() == QDialog.DialogCode.Accepted
    assert wizard.take_pending_navigations() == (link,)
    assert wizard.take_pending_navigations() == ()


def test_queued_navigation_does_not_save_a_plan(qapp, repository, tmp_path):
    wizard = CommissioningWizard(repository, "doc-1", data_dir=tmp_path)
    wizard._queue_navigation(
        WorkspaceDeepLink(WorkspaceId.ROOM, "geometry")
    )
    # Leaving via a link is a suspend, not a save — no plan is written.
    assert not (tmp_path / "commissioning-plans.json").exists()


def test_pending_navigation_default_is_empty(qapp, repository):
    wizard = CommissioningWizard(repository, "doc-1")
    assert wizard.take_pending_navigations() == ()
