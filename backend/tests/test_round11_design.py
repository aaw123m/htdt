"""Round-11 visual/design regressions.

Guards the layout fixes from the design audit:
  * the shell's minimumSizeHint must stay within a 1366x768 screen even
    after every workspace/context has been mounted (a single unscrollable
    tall page otherwise raises the whole window minimum forever —
    QStackedWidget takes the max over children),
  * the right-dock panels are wrapped in scroll pages so narrow-dock
    overflow is scrollable instead of silently clipped,
  * Vector3Editor reports a narrow-width minimumSizeHint so its stacked
    (narrow) layout can actually engage inside the 260-320 px dock,
  * TreatmentDefinitionDialog keeps QWidget.width()/height() usable
    (member names must not shadow widget accessors),
  * the project-library table sizes non-stretch columns to their content
    so timestamps never elide.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QScrollArea
from PySide6.QtWidgets import QHeaderView

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _f1_repository(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def test_shell_minimum_stays_within_768_screen(tmp_path: Path) -> None:
    from htdt.workflow_application import WorkflowApplicationComposition
    from htdt.workflow_navigation import (
        ApplicationDestinationId,
        CANONICAL_WORKSPACE_CONTEXTS,
        WorkspaceId,
    )

    app = _app()
    comp = WorkflowApplicationComposition(
        _f1_repository(tmp_path), F1_DOCUMENT_ID
    )
    shell = comp.shell
    shell.resize(1366, 768)
    shell.show()
    app.processEvents()

    for destination in ApplicationDestinationId:
        shell.navigate(destination)
        app.processEvents()
    for workspace_id in WorkspaceId:
        shell.navigate(workspace_id)
        app.processEvents()
        for context in CANONICAL_WORKSPACE_CONTEXTS[workspace_id]:
            try:
                shell.select_context(context.context_id)
            except ValueError:
                continue
            app.processEvents()

    hint = shell.minimumSizeHint()
    assert hint.height() <= 768, (
        f"shell minimum height {hint.height()} exceeds a 768px screen"
    )
    assert hint.width() <= 1366
    shell.close()
    shell.deleteLater()
    app.processEvents()


def test_room_dock_panels_are_scroll_wrapped(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QWidget
    from htdt.room_workspace import RoomWorkspace
    from test_room_workspace import FakeRoomViewport  # noqa: E402

    app = _app()
    workspace = RoomWorkspace(
        _f1_repository(tmp_path),
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    workspace.resize(1100, 700)
    workspace.show()
    app.processEvents()

    workspace.attach_geometry_panel(QWidget())
    workspace.attach_acoustics_panel(QWidget())
    app.processEvents()

    assert isinstance(workspace._geometry_page, QScrollArea)
    assert isinstance(workspace._acoustics_page, QScrollArea)
    for page in (
        workspace.objects_page,
        workspace.placement_panel,
        workspace.history_page,
        workspace._geometry_page,
        workspace._acoustics_page,
    ):
        assert isinstance(page, QScrollArea)
        assert page.widgetResizable()
        assert page.horizontalScrollBarPolicy() != (
            page.horizontalScrollBarPolicy().ScrollBarAlwaysOff
        )

    # Context switching must target the wrapped pages, not the raw panels.
    workspace.set_context("geometry")
    assert workspace.right_stack.currentWidget() is workspace._geometry_page
    workspace.set_context("acoustics")
    assert workspace.right_stack.currentWidget() is workspace._acoustics_page

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_vector3_editor_minimum_fits_compact_dock(tmp_path: Path) -> None:
    from htdt.room_workspace import Vector3Editor

    app = _app()
    editor = Vector3Editor()
    editor.show()
    app.processEvents()
    # Compact dock is 248 px at its narrowest; the stacked (narrow)
    # layout must be able to fit inside it.
    assert editor.minimumSizeHint().width() <= 248
    editor.close()
    editor.deleteLater()
    app.processEvents()


def test_treatment_dialog_keeps_widget_accessors() -> None:
    from htdt.room_acoustics_panel import TreatmentDefinitionDialog

    app = _app()
    dialog = TreatmentDefinitionDialog()
    assert isinstance(dialog.width(), int)
    assert isinstance(dialog.height(), int)
    dialog.deleteLater()
    app.processEvents()


def test_prediction_combos_do_not_impose_wide_minimum(tmp_path: Path) -> None:
    from htdt.room_prediction import RoomPredictionController, RoomPredictionPanel
    from htdt.room_workspace import RoomWorkspaceController

    app = _app()
    repository = _f1_repository(tmp_path)
    room = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    controller = RoomPredictionController(repository, room)
    panel = RoomPredictionPanel(controller)
    panel.show()
    app.processEvents()
    for combo in panel.findChildren(QComboBox):
        assert combo.sizeAdjustPolicy() == (
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        assert combo.minimumSizeHint().width() <= 300
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_projects_table_sizes_data_columns_to_contents(
    tmp_path: Path,
) -> None:
    from htdt.application_pages import ProjectLibraryPage, ProjectLibraryService

    app = _app()
    service = ProjectLibraryService(_f1_repository(tmp_path))
    page = ProjectLibraryPage(service, lambda: F1_DOCUMENT_ID)
    header = page.table.horizontalHeader()
    assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch
    for column in range(1, page.table.columnCount()):
        assert header.sectionResizeMode(column) == (
            QHeaderView.ResizeMode.ResizeToContents
        )
    page.deleteLater()
    app.processEvents()
