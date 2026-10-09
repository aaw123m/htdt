"""#973: per-(workspace, context) view-state persistence.

``WorkspaceViewState`` carries bounded, non-secret UX values — scroll
offset, primary selection id, enabled filters, expanded panels, splitter
ratio — inside the per-project window-state record. These tests pin the
shell plumbing (capture on deactivate/context-switch, restore on
activate, deep-link focus wins, project isolation, vetoed navigation)
and the measurement quality page's id-keyed restore invariants:
vanished selection → deselect + parent focus, never a nearest-row guess.
"""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QTableWidget

from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.measurement_workflow import MeasurementWorkflowController
from htdt.navigation_target import NavigationTarget, NavigationTargetKind
from htdt.window_state import (
    PersistedWindowState,
    WorkspaceViewState,
    load_window_state,
    save_window_state,
    view_state_key,
    window_state_path,
)
from htdt.workflow_application import WorkflowApplicationComposition
from htdt.workflow_navigation import (
    ApplicationDestinationId,
    WorkspaceId,
)
from htdt.workflow_shell import (
    WorkflowShellWindow,
    WorkspaceMount,
    build_canonical_workspace_registrations,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


# -- model + file-level invariants ----------------------------------------


def test_view_state_model_roundtrip_and_bounds() -> None:
    state = WorkspaceViewState(
        scroll_offset=480,
        selected_entity="m-fl-mlp",
        filters={"verdict": "retake", "search": "mlp"},
        expanded_panels=("advanced",),
        splitter_ratio=0.62,
    )
    loaded = WorkspaceViewState.model_validate_json(state.model_dump_json())
    assert loaded == state

    # Bounded: oversized filter maps and panel lists are truncated.
    bloated = WorkspaceViewState.model_validate(
        {
            "filters": {f"k{i}": "v" for i in range(80)},
            "expanded_panels": [f"p{i}" for i in range(80)],
        }
    )
    assert len(bloated.filters) <= 32
    assert len(bloated.expanded_panels) <= 32


def test_view_state_persisted_roundtrip(tmp_path: Path) -> None:
    state = PersistedWindowState(
        workspace="measurement",
        contexts={"measurement": "quality"},
        view_states={
            "measurement:quality": WorkspaceViewState(
                scroll_offset=120,
                selected_entity="m-a",
                filters={"search": "mlp"},
            ),
        },
    )
    save_window_state(tmp_path, state)
    loaded = load_window_state(tmp_path)
    assert loaded is not None
    assert loaded == state


def test_view_state_corrupt_entry_drops_not_file(tmp_path: Path) -> None:
    path = window_state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
        {
          "schema_version": 1,
          "workspace": "room",
          "view_states": {
            "room:objects": {"scroll_offset": 42},
            "measurement:quality": {"scroll_offset": "not-an-int"}
          }
        }
        """,
        encoding="utf-8",
    )
    loaded = load_window_state(tmp_path)
    assert loaded is not None
    assert loaded.workspace == "room"
    assert "room:objects" in loaded.view_states
    assert "measurement:quality" not in loaded.view_states


def test_view_state_unknown_workspace_pruned(tmp_path: Path) -> None:
    save_window_state(
        tmp_path,
        PersistedWindowState(
            view_states={
                "gone-workspace:x": WorkspaceViewState(scroll_offset=1),
                "room:objects": WorkspaceViewState(scroll_offset=2),
            }
        ),
    )
    loaded = load_window_state(tmp_path)
    assert loaded is not None
    assert list(loaded.view_states) == ["room:objects"]


def test_view_state_corrupt_file_safe_default(tmp_path: Path) -> None:
    path = window_state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"view_states": 999}', encoding="utf-8")
    # view_states of the wrong shape degrades to empty — the rest of the
    # record still parses, and the whole corrupt file path stays None-safe.
    loaded = load_window_state(tmp_path)
    assert loaded is not None
    assert loaded.view_states == {}
    path.write_text("{totally broken", encoding="utf-8")
    assert load_window_state(tmp_path) is None


# -- shell plumbing --------------------------------------------------------


class _ViewStateSpy:
    """Mount-level capture/restore spy recording call order."""

    def __init__(self, calls: list[str], label: str) -> None:
        self.calls = calls
        self.label = label
        self.captured = WorkspaceViewState(
            scroll_offset=11, selected_entity=f"sel-{label}"
        )
        self.restored: list[WorkspaceViewState] = []
        self.focused: list[str] = []

    def capture(self) -> WorkspaceViewState:
        self.calls.append(f"capture:{self.label}")
        return self.captured

    def restore(self, state: WorkspaceViewState) -> None:
        self.calls.append(f"restore:{self.label}")
        self.restored.append(state)

    def focus_entity(self, entity_id: str) -> None:
        self.calls.append(f"focus:{entity_id}")
        self.focused.append(entity_id)


def _spy_factory(spy: _ViewStateSpy):
    def build() -> WorkspaceMount:
        return WorkspaceMount.from_widget(
            QLabel(spy.label),
            on_context_changed=lambda _ctx: None,
            on_entity_requested=spy.focus_entity,
            capture_view_state=spy.capture,
            restore_view_state=spy.restore,
        )

    return build


def _shell_with_spies(
    spies: dict[WorkspaceId, _ViewStateSpy],
) -> WorkflowShellWindow:
    def factory(workspace_id: WorkspaceId):
        spy = spies.get(workspace_id)
        if spy is None:
            return lambda: WorkspaceMount.from_widget(QLabel(str(workspace_id)))
        return _spy_factory(spy)

    registrations = build_canonical_workspace_registrations(
        {workspace_id: factory(workspace_id) for workspace_id in WorkspaceId}
    )
    return WorkflowShellWindow(registrations)


def test_view_state_roundtrip_on_workspace_switch() -> None:
    app = _app()
    calls: list[str] = []
    spy = _ViewStateSpy(calls, "measurement")
    shell = _shell_with_spies({WorkspaceId.MEASUREMENT: spy})
    shell.show()

    shell.navigate(WorkspaceId.MEASUREMENT)
    app.processEvents()
    assert spy.restored == []  # nothing stored yet
    shell.navigate(WorkspaceId.ROOM)
    app.processEvents()
    assert f"capture:measurement" in calls
    shell.navigate(WorkspaceId.MEASUREMENT)
    app.processEvents()
    assert spy.restored == [spy.captured]
    shell.close()


def test_view_state_context_switch_captures_old_context() -> None:
    app = _app()
    calls: list[str] = []
    spy = _ViewStateSpy(calls, "measurement")
    shell = _shell_with_spies({WorkspaceId.MEASUREMENT: spy})
    shell.show()
    shell.navigate(WorkspaceId.MEASUREMENT)
    app.processEvents()

    shell.select_context("quality")
    app.processEvents()
    # Switching context quality → comparison captures under the old key
    # and restores under the new key (empty at first).
    shell.select_context("comparison")
    app.processEvents()
    assert spy.restored == []
    shell.select_context("quality")
    app.processEvents()
    # Second arrival restores the state captured when leaving "quality".
    assert spy.restored == [spy.captured]
    shell.close()


def test_view_state_deep_link_focus_beats_restore() -> None:
    app = _app()
    calls: list[str] = []
    spy = _ViewStateSpy(calls, "measurement")
    shell = _shell_with_spies({WorkspaceId.MEASUREMENT: spy})
    shell.show()
    shell.navigate(WorkspaceId.MEASUREMENT)
    app.processEvents()
    # Store a stale selection under the assignment context key.
    shell.seed_view_states(
        {
            view_state_key("measurement", "assignment"): WorkspaceViewState(
                selected_entity="stale-selection"
            )
        }
    )
    shell.navigate(WorkspaceId.ROOM)
    app.processEvents()
    calls.clear()

    resolution = shell.navigate_to_target(
        NavigationTarget(
            kind=NavigationTargetKind.MEASUREMENT,
            object_ids=("m-explicit",),
        )
    )
    app.processEvents()
    assert resolution.ok
    # The explicit deep-link entity focus runs AFTER any restored view
    # state — the requested target always wins.
    assert calls[-1] == "focus:m-explicit"
    assert "focus:m-explicit" in calls
    shell.close()


def test_view_state_seed_drops_unknown_and_reset_clears() -> None:
    _app()
    spy = _ViewStateSpy([], "measurement")
    shell = _shell_with_spies({WorkspaceId.MEASUREMENT: spy})
    shell.seed_view_states(
        {
            "not-a-workspace:x": WorkspaceViewState(scroll_offset=1),
            view_state_key("measurement", "quality"): WorkspaceViewState(
                scroll_offset=2
            ),
        }
    )
    assert list(shell.view_states()) == ["measurement:quality"]
    shell.reset_view_states()
    assert shell.view_states() == {}
    shell.close()


def test_view_state_vetoed_navigation_applies_nothing() -> None:
    app = _app()
    calls: list[str] = []
    spy = _ViewStateSpy(calls, "measurement")

    def overview_factory() -> WorkspaceMount:
        # Veto every deactivation — the shell's dirty-state guard wins.
        return WorkspaceMount.from_widget(
            QLabel("overview"), before_deactivate=lambda: (False, "busy")
        )

    registrations = build_canonical_workspace_registrations(
        {
            workspace_id: (
                overview_factory
                if workspace_id is WorkspaceId.OVERVIEW
                else (
                    _spy_factory(spy)
                    if workspace_id is WorkspaceId.MEASUREMENT
                    else (lambda wid=workspace_id: WorkspaceMount.from_widget(
                        QLabel(str(wid))
                    ))
                )
            )
            for workspace_id in WorkspaceId
        }
    )
    shell = WorkflowShellWindow(registrations)
    shell.show()
    assert shell.navigate(WorkspaceId.MEASUREMENT) is False
    app.processEvents()
    # Vetoed switch never reached the measurement mount — nothing was
    # restored and the captured map has no stale entry under it.
    assert spy.restored == []
    # The veto also vetoes close — same as the existing close-guard test;
    # the window just stays unmounted offscreen.
    shell.close()


def test_collect_view_states_covers_active_mount() -> None:
    app = _app()
    calls: list[str] = []
    spy = _ViewStateSpy(calls, "measurement")
    shell = _shell_with_spies({WorkspaceId.MEASUREMENT: spy})
    shell.show()
    shell.navigate(WorkspaceId.MEASUREMENT)
    shell.select_context("quality")
    app.processEvents()
    shell.collect_view_states()
    states = shell.view_states()
    assert "measurement:quality" in states
    assert states["measurement:quality"].selected_entity == "sel-measurement"
    shell.close()


def test_composition_view_state_survives_close_and_reload(
    tmp_path: Path,
) -> None:
    app = _app()
    composition = WorkflowApplicationComposition(
        SceneRepository(tmp_path / "data" / "cad-scenes.sqlite3"),
        "document-1",
    )
    shell = composition.shell
    shell.show()
    shell.navigate(WorkspaceId.MEASUREMENT)
    shell.select_context("quality")
    app.processEvents()
    shell.close()
    app.processEvents()

    loaded = load_window_state(
        tmp_path / "data",
        project_ref=composition.project_entry.project_id,
    )
    assert loaded is not None
    # The real mount captured a view state for the live quality context
    # through the close hook → per-project save path.
    entry = loaded.view_states.get("measurement:quality")
    assert isinstance(entry, WorkspaceViewState)


# -- measurement quality page, real workspace ------------------------------


def _saved_scene(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    return scene_repository, revision


def _save_dataset(
    measurement_repository: CadMeasurementRepository,
    revision,
    measurement_id: str,
    *,
    channel_role: str = "front_left",
):
    processing = {"fixture_raw": measurement_id}
    source = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_deg=(10.0, 20.0, 30.0),
        phase_status="valid",
        processing=processing,
    )
    record = measurement_record_for_revision(
        revision,
        "point-mlp",
        measurement_id=measurement_id,
        evidence_type="measured",
        channel_role=channel_role,
        source_kind="unknown",
        quality_status="unknown",
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f"dataset-{measurement_id}",
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_deg=(10.0, 20.0, 30.0),
        phase_status="valid",
        processing_json=canonical_json(processing),
        source_sha256=sha256(source).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        record,
        dataset,
        raw_filename=f"{measurement_id}.json",
        raw_bytes=source,
    )
    return record


def _quality_workspace(tmp_path: Path, measurement_ids) -> MeasurementPageWorkspace:
    _app()
    scene_repository, revision = _saved_scene(tmp_path)
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    for measurement_id in measurement_ids:
        _save_dataset(measurement_repository, revision, measurement_id)
    controller = MeasurementWorkflowController(
        scene_repository,
        revision.document_id,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    workspace = MeasurementPageWorkspace(controller)
    workspace.set_context("quality")
    _app().processEvents()
    return workspace


def _selected_quality_id(workspace: MeasurementPageWorkspace) -> str | None:
    selected = workspace.quality_table.selectedItems()
    if not selected:
        return None
    return selected[0].data(Qt.ItemDataRole.UserRole)


def test_quality_view_state_roundtrip(tmp_path: Path) -> None:
    app = _app()
    workspace = _quality_workspace(tmp_path, ["m-a", "m-b", "m-c"])
    row = workspace._quality_row_index_for_id("m-b")
    assert row is not None
    workspace.quality_table.selectRow(row)
    workspace.quality_search_edit.setText("m-")
    app.processEvents()

    captured = workspace.capture_view_state()
    assert captured is not None
    assert captured.selected_entity == "m-b"
    assert captured.filters["search"] == "m-"

    # Perturb the live view before restoring.
    workspace._clear_quality_filters()
    workspace.quality_table.clearSelection()
    app.processEvents()
    assert _selected_quality_id(workspace) is None

    workspace.restore_view_state(captured)
    app.processEvents()
    assert _selected_quality_id(workspace) == "m-b"
    assert workspace.quality_search_edit.text() == "m-"


def test_quality_vanished_selection_deselects(tmp_path: Path) -> None:
    app = _app()
    workspace = _quality_workspace(tmp_path, ["m-a", "m-b"])
    workspace.restore_view_state(
        WorkspaceViewState(selected_entity="m-gone")
    )
    app.processEvents()
    assert workspace.quality_table.selectedItems() == []
    # Honest note states the vanished selection — no nearest-row guess.
    assert "ありません" in workspace.quality_selection_note.text()


def test_quality_index_reorder_restores_by_id(tmp_path: Path) -> None:
    app = _app()
    workspace = _quality_workspace(tmp_path, ["m-a", "m-b", "m-c"])
    row = workspace._quality_row_index_for_id("m-c")
    assert row is not None
    workspace.quality_table.selectRow(row)
    captured = workspace.capture_view_state()
    assert captured is not None and captured.selected_entity == "m-c"

    # Re-sort the visual order — restore resolves the record id, never
    # the visual index.
    workspace.quality_table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    workspace.quality_table.clearSelection()
    app.processEvents()

    workspace.restore_view_state(captured)
    app.processEvents()
    assert _selected_quality_id(workspace) == "m-c"


def test_quality_delayed_refresh_keeps_restored_selection(
    tmp_path: Path,
) -> None:
    app = _app()
    workspace = _quality_workspace(tmp_path, ["m-a", "m-b"])
    workspace.restore_view_state(
        WorkspaceViewState(selected_entity="m-b")
    )
    app.processEvents()
    assert _selected_quality_id(workspace) == "m-b"
    # A delayed refresh after restore keeps the id-keyed selection.
    workspace._refresh_quality()
    app.processEvents()
    assert _selected_quality_id(workspace) == "m-b"
