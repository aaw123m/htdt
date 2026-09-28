"""Offscreen smoke tests for high-traffic Qt modules (#1202 tail).

Round-6 named ~30 Qt modules with no direct test coverage; several are on
the highest-traffic paths (project deliverables, seat priority, capture
intake, material/treatment panels, system expansion, the optimization page
navigator). These tests assert instantiation plus primary-action wiring —
buttons reach their handlers, navigation selects the right page — never
pixel output.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip('PySide6')

from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from htdt.application_preferences import ApplicationPreferenceStore
from htdt.capture_receiver_controller import CaptureReceiverController
from htdt.capture_receiver_settings import CaptureReceiverPanel, PairingDialog
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.deliverables_catalog import DeliverablesCatalogService
from htdt.deliverables_dialog import DeliverablesDialog
from htdt.room_acoustics_panel import (
    MaterialDialog,
    RoomAcousticsTabs,
    RoomTreatmentPanel,
    SurfaceMaterialPanel,
    TreatmentDefinitionDialog,
)
from htdt.room_workspace import RoomWorkspaceController
from htdt.seat_priority_panel import SeatPriorityPanel
from htdt.system_expansion_widgets import SystemExpansionRoomPanel
from htdt.system_expansion_workflow import SystemExpansionWorkflowService

from test_optimization_workflow_workspace import FakeOptimizationViewport


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _repository(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def test_seat_priority_panel_builds_tree_and_preset(tmp_path: Path) -> None:
    _app()
    repository = _repository(tmp_path)
    panel = SeatPriorityPanel(repository, F1_DOCUMENT_ID)

    preset_buttons = [
        button
        for button in panel.findChildren(QPushButton)
        if 'プリセット' in button.text()
    ]
    assert preset_buttons, 'expected the MLP preset button'
    preset_buttons[0].click()
    panel.deleteLater()


def test_deliverables_dialog_wires_command_buttons(tmp_path: Path) -> None:
    _app()
    repository = _repository(tmp_path)
    commands: list[str] = []
    dialog = DeliverablesDialog(
        DeliverablesCatalogService(repository, F1_DOCUMENT_ID),
        document_id=F1_DOCUMENT_ID,
        on_command=commands.append,
    )
    assert 'プロジェクト' in dialog.windowTitle()
    generate_buttons = [
        button
        for button in dialog.findChildren(QPushButton)
        if button.text() == '書き出し'
    ]
    for button in generate_buttons:
        button.click()
    # Whatever the catalog offers as available must reach the command sink;
    # an all-pending project legitimately wires no buttons.
    assert len(commands) == len(generate_buttons)
    assert all(commands)
    dialog.deleteLater()


def test_capture_receiver_panel_and_pairing_dialog(tmp_path: Path) -> None:
    _app()
    repository = _repository(tmp_path)
    preferences = ApplicationPreferenceStore(tmp_path / 'prefs.json')
    controller = CaptureReceiverController(repository, preferences)

    panel = CaptureReceiverPanel(controller, lambda: 'project-1')
    assert panel.enabled_combo.count() >= 2
    assert panel.port_spin.minimum() >= 1
    assert panel.apply_button is not None
    assert panel.port_button is not None

    dialog = PairingDialog(controller, 'project-1')
    assert dialog.offer_button.text() == 'QRコードを発行'
    assert dialog.scope_combo.count() == 2
    dialog.deleteLater()
    panel.deleteLater()


def test_material_and_treatment_dialogs_construct() -> None:
    _app()
    material = MaterialDialog()
    assert material.label is not None and material.wave_model.count() > 0
    treatment = TreatmentDefinitionDialog()
    assert treatment.name is not None and treatment.treatment_type.count() > 0
    material.deleteLater()
    treatment.deleteLater()


def test_surface_material_and_treatment_panels_refresh(tmp_path: Path) -> None:
    _app()
    repository = _repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)

    material_panel = SurfaceMaterialPanel(controller)
    treatment_panel = RoomTreatmentPanel(controller)
    material_panel.refresh()
    treatment_panel.refresh()
    assert material_panel.surface_tree.columnCount() >= 1
    material_panel.deleteLater()
    treatment_panel.deleteLater()


def test_room_acoustics_tabs_refresh_reaches_each_panel() -> None:
    _app()

    class _Panel(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.refreshed = 0

        def refresh(self) -> None:
            self.refreshed += 1

    panels = (_Panel(), _Panel(), _Panel())
    tabs = RoomAcousticsTabs(*panels)
    tabs.refresh()
    assert all(panel.refreshed == 1 for panel in panels)
    assert tabs.count() == 3
    tabs.deleteLater()


def test_system_expansion_room_panel_constructs_offscreen(
    tmp_path: Path,
) -> None:
    _app()
    repository = _repository(tmp_path)
    service = SystemExpansionWorkflowService(repository, F1_DOCUMENT_ID)
    panel = SystemExpansionRoomPanel(service)
    assert panel.service is service
    panel.deleteLater()


def test_optimization_workspace_selects_every_page(tmp_path: Path) -> None:
    _app()
    repository = _repository(tmp_path)
    from htdt.optimization_workflow_workspace import (
        OPTIMIZATION_PAGE_IDS,
        OptimizationWorkflowWorkspace,
    )

    workspace = OptimizationWorkflowWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeOptimizationViewport(parent),
    )
    for page_id in OPTIMIZATION_PAGE_IDS:
        workspace.select_section(page_id)
        assert workspace.current_page_id == page_id
    workspace.close()
    workspace.deleteLater()


def test_optimization_mount_binds_undo_redo_shortcuts(monkeypatch, tmp_path: Path) -> None:
    """Ctrl+Z/Ctrl+Y/Ctrl+S reach the Optimize controller (#tail).

    The deferral was that the Optimize workspace declared undo/redo command
    bindings but never wired the QShortcut surface the Room workspace has.
    """
    _app()
    events: list[str] = []

    class Working:
        is_dirty = True
        can_undo = True
        can_redo = True
        has_preview = False

    class Scene:
        recovery_candidate = None

    class Controller:
        def __init__(self) -> None:
            self.working = Working()
            self.scene = Scene()
            self.search_selected_spec_id = 'spec-1'
            self._rew_tasks = {}

        def active_search_worker_count(self) -> int:
            return 0

        def active_extended_worker_count(self) -> int:
            return 0

        def save(self) -> bool:
            events.append('save')
            return True

        def undo(self) -> bool:
            events.append('undo')
            return True

        def redo(self) -> bool:
            events.append('redo')
            return True

        def refresh_pareto_comparison(self) -> None:
            events.append('compare')

    class FakeWorkspace(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.controller = Controller()

    from PySide6.QtGui import QKeySequence, QShortcut

    import htdt.workflow_application as workflow_application
    from htdt.command_registry import (
        CommandRegistry,
        register_default_commands,
    )
    from htdt.workflow_shell import WorkspaceMount

    workspace = FakeWorkspace()
    fake_mount = WorkspaceMount.from_widget(workspace)
    monkeypatch.setattr(
        workflow_application,
        'build_optimization_workspace_mount',
        lambda _repository, _document_id, **_kwargs: fake_mount,
    )
    composition = object.__new__(workflow_application.WorkflowApplicationComposition)
    composition.repository = object()
    composition.document_id = 'document-1'
    composition.preferences = ApplicationPreferenceStore(
        tmp_path / 'preferences.json'
    )
    composition.registry = CommandRegistry()
    register_default_commands(composition.registry)

    mount = composition._make_optimization()
    mount.on_activate()

    shortcuts = workspace.findChildren(QShortcut)
    keys = {shortcut.key().toString() for shortcut in shortcuts}
    assert {'Ctrl+S', 'Ctrl+Z', 'Ctrl+Y'} <= keys

    for shortcut in shortcuts:
        sequence = shortcut.key().toString()
        if sequence == 'Ctrl+Z':
            shortcut.activated.emit()
    assert events == ['undo']

    workspace.close()
    workspace.deleteLater()
