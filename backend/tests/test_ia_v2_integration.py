from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QSpinBox

from htdt.application_preferences import (
    ApplicationPreferenceStore,
    PREFERENCES_SCHEMA_VERSION,
)
from htdt.cad_repository import SceneRepository
from htdt.overview_readiness import OverviewReadinessService
from htdt.palette_search import settings_destinations
from htdt.navigation_target import NavigationTarget, NavigationTargetKind
from htdt.workflow_application import WorkflowApplicationComposition
from htdt.workflow_navigation import ApplicationDestinationId
from htdt.workflow_settings import PreferencesWidget


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _composition(
    tmp_path: Path, **kwargs
) -> WorkflowApplicationComposition:
    repository = SceneRepository(tmp_path / "data" / "cad-scenes.sqlite3")
    return WorkflowApplicationComposition(repository, "document-1", **kwargs)


# ---------------------------------------------------------------------------
# Overview Trust wiring (#740)


@dataclass
class _SceneSource:
    revision: object = None

    def current_head(self, document_id: str):
        if self.revision is None or self.revision.document_id != document_id:
            return None
        return self.revision

    def get(self, revision_id: str):
        return self.revision if self.revision.revision_id == revision_id else None


@dataclass
class _MeasurementSource:
    measurements: tuple = ()

    def list_measurements(self, document_id: str) -> tuple:
        return self.measurements

    def dataset_for_measurement(self, measurement_id: str):
        return None


@dataclass
class _PredictionSource:
    results: tuple = ()

    def list_results(self, document_id: str) -> tuple:
        return self.results


@dataclass
class _SearchSource:
    specs: tuple = ()

    def list_specs(self, document_id: str) -> tuple:
        return self.specs


@dataclass
class _ValidationSource:
    records: dict | None = None

    def inspect_for_search_spec(self, search_spec_id: str) -> tuple:
        return (self.records or {}).get(search_spec_id, ())


def _revision():
    from htdt.cad_repository import SceneRevision

    document = SimpleNamespace(
        room=SimpleNamespace(room_id="room"),
        entities=(
            SimpleNamespace(
                entity_id="speaker-fl", kind="speaker", speaker_role="FL"
            ),
        ),
    )
    return SceneRevision(
        revision_id="revision-current",
        document_id="project-1",
        parent_revision_id=None,
        created_at_utc="2026-09-18T00:00:00+00:00",
        content_hash="a" * 64,
        document=document,
    )


def test_overview_surfaces_result_trust_lines_for_latest_evidence() -> None:
    revision = _revision()
    prediction = SimpleNamespace(
        status="completed",
        document_id="project-1",
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        model_id="model-1",
        model_version="1",
    )
    measurement = SimpleNamespace(
        measurement_id="measurement-1",
        document_id="project-1",
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        evidence_type="measured",
    )

    service = OverviewReadinessService(
        _SceneSource(revision),
        _MeasurementSource((measurement,)),
        _PredictionSource((prediction,)),
        _SearchSource(),
        _ValidationSource(),
    )
    view = service.read("project-1")

    assert len(view.trust_lines) == 2
    assert view.trust_lines[0].startswith("最新の予測")
    assert view.trust_lines[1].startswith("最新の測定")


def test_overview_has_no_trust_lines_without_evidence() -> None:
    service = OverviewReadinessService(
        _SceneSource(_revision()),
        _MeasurementSource(),
        _PredictionSource(),
        _SearchSource(),
        _ValidationSource(),
    )
    view = service.read("project-1")
    assert view.trust_lines == ()


# ---------------------------------------------------------------------------
# Preferences wired into the shell (#740)


def test_settings_preferences_palette_destination_opens_preferences_tab(
    tmp_path: Path,
) -> None:
    app = _app()
    composition = _composition(tmp_path)

    assert any(
        destination.destination_id == "settings.preferences"
        for destination in settings_destinations()
    )
    assert composition._open_settings_destination("settings.preferences")
    tabs = composition.settings_dialog._tabs
    assert tabs is not None
    assert (
        tabs.currentWidget() is composition.settings_dialog._preferences_panel
    )

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_preferences_widget_writes_through_to_store(tmp_path: Path) -> None:
    app = _app()
    store = ApplicationPreferenceStore(tmp_path / "prefs.json")
    widget = PreferencesWidget(store)

    checkbox = widget._editors["display_input.reduced_motion"]
    assert isinstance(checkbox, QCheckBox)
    checkbox.setChecked(True)
    assert store.get("display_input.reduced_motion") is True

    combo = widget._editors["display_input.length_unit"]
    assert isinstance(combo, QComboBox)
    combo.setCurrentIndex(combo.findData("cm"))
    assert store.get("display_input.length_unit") == "cm"

    spin = widget._editors["integrations.rew_port"]
    assert isinstance(spin, QSpinBox)
    spin.setValue(9000)
    assert store.get("integrations.rew_port") == 9000

    # Values are durable, not just in-memory widget state.
    reloaded = ApplicationPreferenceStore(tmp_path / "prefs.json")
    assert reloaded.get("display_input.reduced_motion") is True
    assert reloaded.get("display_input.length_unit") == "cm"
    assert reloaded.get("integrations.rew_port") == 9000

    widget.deleteLater()
    app.processEvents()


def test_preferences_widget_fails_closed_on_newer_schema_file(
    tmp_path: Path,
) -> None:
    app = _app()
    path = tmp_path / "prefs.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": PREFERENCES_SCHEMA_VERSION + 1,
                "values": {"general.language": "ja"},
            }
        ),
        encoding="utf-8",
    )
    store = ApplicationPreferenceStore(path)
    widget = PreferencesWidget(store)

    assert not store.write_allowed
    assert all(not editor.isEnabled() for editor in widget._editors.values())
    assert "上書きできません" in widget.status.text()

    # The sanctioned recovery preserves the file and re-enables writes
    # (pending keys stay disabled — see PENDING_PREFERENCE_KEYS). #987 gates
    # it behind an explicit warning — accept it here.
    widget._confirm_sanctioned_reset = lambda: True
    widget.reset_button.click()
    assert store.write_allowed
    assert path.with_name(path.name + ".recovery").is_file()
    assert widget._editors["display_input.length_unit"].isEnabled()

    widget.deleteLater()
    app.processEvents()


# ---------------------------------------------------------------------------
# Typed navigation focus (#766)


def test_application_focus_targets_resolve_the_requested_authority(
    tmp_path: Path,
) -> None:
    """#766: typed navigation reports focus only when the requested authority
    actually resolved — PROJECT selects the matching library row, HELP_TOPIC
    resolves a real topic, and unknown ids report a truthful failure."""
    app = _app()
    composition = _composition(tmp_path)
    router = composition.shell.router

    target = NavigationTarget(
        kind=NavigationTargetKind.PROJECT,
        object_ids=(composition.project_entry.project_id,),
    )
    result = router.focus_target(ApplicationDestinationId.PROJECTS, target)
    assert result.focused
    page = router.mount(ApplicationDestinationId.PROJECTS).widget
    hit_rows = {
        row
        for row in range(page.table.rowCount())
        if page.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        == composition.project_entry.project_id
    }
    assert hit_rows
    assert hit_rows <= {
        item.row() for item in page.table.selectedItems()
    }

    missing = router.focus_target(
        ApplicationDestinationId.PROJECTS,
        NavigationTarget(
            kind=NavigationTargetKind.PROJECT,
            object_ids=("proj-does-not-exist",),
        ),
    )
    assert not missing.focused
    assert missing.message

    library = router.focus_target(
        ApplicationDestinationId.LIBRARY,
        NavigationTarget(
            kind=NavigationTargetKind.EQUIPMENT_DEFINITION,
            object_ids=("def-missing",),
        ),
    )
    assert not library.focused
    assert library.message

    composition._open_help_topic = lambda topic_id: topic_id == "help.real"
    resolved = router.focus_target(
        ApplicationDestinationId.SUPPORT,
        NavigationTarget(
            kind=NavigationTargetKind.HELP_TOPIC,
            object_ids=("help.real",),
        ),
    )
    assert resolved.focused
    unknown = router.focus_target(
        ApplicationDestinationId.SUPPORT,
        NavigationTarget(
            kind=NavigationTargetKind.HELP_TOPIC,
            object_ids=("help.missing",),
        ),
    )
    assert not unknown.focused
    assert unknown.message
    app.processEvents()
