"""Tests for the #583 Precision Inspector redesign."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QFrame

from htdt.application_preferences import ApplicationPreferenceStore
from htdt.cad_display_units import (
    DEFAULT_DISPLAY_DECIMALS,
    display_to_si,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.room_workspace import (
    InspectorSection,
    MetricSpinBox,
    RoomWorkspace,
    SelectionInspector,
    Vector3Editor,
)
from htdt.workflow_application import bind_inspector_display_length_policy


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _f1_repository(tmp_path) -> SceneRepository:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


class FakeRoomViewport(QFrame):
    entitySelected = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.render_calls = []
        self.focused = []
        self.fit_count = 0

    def render_document(
        self,
        document,
        *,
        selected_id=None,
        selected_ids=(),
        hidden_ids=frozenset(),
        locked_ids=frozenset(),
        overlays=None,
        reset_camera=False,
    ) -> None:
        self.render_calls.append((selected_id, overlays, reset_camera))

    def fit_scene(self) -> None:
        self.fit_count += 1

    def focus_entity(self, entity_id) -> None:
        self.focused.append(entity_id)


def _workspace(tmp_path):
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    workspace.resize(1100, 700)
    workspace.show()
    app.processEvents()
    return app, workspace


def test_inspector_is_sectioned_and_collapses_advanced(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    inspector = workspace.inspector
    assert isinstance(inspector.identity_section, InspectorSection)
    assert isinstance(inspector.transform_section, InspectorSection)
    assert isinstance(inspector.geometry_section, InspectorSection)
    assert isinstance(inspector.speaker_section, InspectorSection)
    assert isinstance(inspector.advanced_section, InspectorSection)
    # Advanced provenance starts collapsed — L4 never dominates (#578 L4).
    assert not inspector.advanced_section.body.isVisible()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_metric_spinbox_display_unit_is_cosmetic_not_authority() -> None:
    _app()
    field = MetricSpinBox()
    field.set_value_m(1.5)
    field.set_display_unit("cm")
    assert field.value() == pytest.approx(150.0)
    assert field.value_m() == pytest.approx(1.5)
    field.set_display_unit("mm")
    assert field.value() == pytest.approx(1500.0)
    # Storage authority stays SI metres regardless of display choice.
    field.set_display_unit("m", decimals=4)
    assert field.value() == pytest.approx(1.5)


def test_metric_spinbox_wheel_requires_focus() -> None:
    app = _app()
    field = MetricSpinBox()
    field.set_value_m(1.0)
    event = QWheelEvent(
        QPointF(1, 1),
        QPointF(1, 1),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    # Unfocused wheel is scroll-safety ignored (#583).
    field.wheelEvent(event)
    assert field.value_m() == pytest.approx(1.0)
    field.show()
    app.processEvents()
    field.setFocus(Qt.FocusReason.MouseFocusReason)
    app.processEvents()
    focused_event = QWheelEvent(
        QPointF(1, 1),
        QPointF(1, 1),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    field.wheelEvent(focused_event)
    assert field.value_m() != pytest.approx(1.0)
    field.close()
    field.deleteLater()


def test_vector3_editor_tracks_edited_axes_and_merges_exact() -> None:
    _app()
    editor = Vector3Editor()
    exact = (1.234567, 2.0, 3.0)
    editor.set_values_m(exact)
    assert editor.edited_axes_m() == {}
    # Edit X only; Y/Z contribute their exact authority values on merge.
    editor.fields["X"].set_value_m(9.0)
    assert editor.edited_axes_m() == {0: pytest.approx(9.0)}
    merged = editor.merged_values_m(exact)
    assert merged == pytest.approx((9.0, 2.0, 3.0))
    # The display baseline itself never snaps unedited axes to rounding.
    assert editor._baseline_display[1] == pytest.approx(2.0)


def test_vector3_editor_unknown_and_mixed_states() -> None:
    _app()
    editor = Vector3Editor()
    editor.set_known(False)
    assert not editor._unknown_label.isHidden()
    assert editor._grid_host.isHidden()
    editor.set_known(True)
    editor.set_values_m((1.0, 2.0, 3.0))
    editor.set_mixed_axes({0})
    assert not editor._badges["X"].isHidden()
    assert editor._badges["Y"].isHidden()
    editor.set_mixed_axes(set())
    assert editor._badges["X"].isHidden()


def test_vector3_editor_read_only_locks_fields() -> None:
    _app()
    editor = Vector3Editor()
    editor.set_values_m((1.0, 2.0, 3.0))
    editor.set_read_only(True)
    for field in editor.fields.values():
        assert field.isReadOnly()
    editor.set_read_only(False)


def test_inspector_multi_select_mixed_axes_and_batch_commit(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    first = workspace.controller.add_object("seat")
    second = workspace.controller.add_object("seat")
    workspace.select_entity(first.entity_id)
    workspace.controller.set_selection(second.entity_id, additive=True)
    app.processEvents()

    inspector = workspace.inspector
    # Two seats at different positions → X/Y/Z axes that differ show 混在.
    doc = workspace.controller.document
    pos_a = doc.entity(first.entity_id).position
    pos_b = doc.entity(second.entity_id).position
    mixed = {
        i
        for i, pair in enumerate(
            zip(
                (pos_a.x_m, pos_a.y_m, pos_a.z_m),
                (pos_b.x_m, pos_b.y_m, pos_b.z_m),
            )
        )
        if abs(pair[0] - pair[1]) > 1e-6
    }
    for i, axis in enumerate(("X", "Y", "Z")):
        assert inspector.position_editor._badges[axis].isVisible() == (
            i in mixed
        )

    # Editing one axis applies to both entities without flattening others.
    inspector.position_editor.fields["X"].set_value_m(7.25)
    inspector.editCommitted.emit()
    app.processEvents()
    doc = workspace.controller.document
    for entity_id in (first.entity_id, second.entity_id):
        entity = doc.entity(entity_id)
        assert entity.position.x_m == pytest.approx(7.25)
        # Untouched axes keep each entity's own exact value — never averaged.
        assert entity.position.y_m in (pos_a.y_m, pos_b.y_m)
        assert entity.position.z_m in (pos_a.z_m, pos_b.z_m)

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_inspector_locked_entity_is_read_only(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    entity = workspace.controller.add_object("speaker")
    workspace.controller.set_entities_locked((entity.entity_id,), True)
    workspace.select_entity(entity.entity_id)
    app.processEvents()

    inspector = workspace.inspector
    assert inspector.name_field.isReadOnly()
    for field in inspector.position_editor.fields.values():
        assert field.isReadOnly()
    assert "読み取り専用" in inspector.state_label.text()

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_inspector_unsized_entity_shows_undefined_geometry(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    entity = workspace.controller.add_object("measurement_point")
    workspace.select_entity(entity.entity_id)
    app.processEvents()

    inspector = workspace.inspector
    assert not inspector.geometry_section.isVisible()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_inspector_rejected_commit_keeps_edits_and_flags_section(
    tmp_path,
) -> None:
    app, workspace = _workspace(tmp_path)
    entity = workspace.controller.add_object("furniture")
    workspace.select_entity(entity.entity_id)
    app.processEvents()
    inspector = workspace.inspector

    # Switch shape to extruded polygon and corrupt the footprint field.
    shape_index = inspector.shape_field.findData("extruded_polygon")
    assert shape_index >= 0
    inspector.shape_field.setCurrentIndex(shape_index)
    inspector.footprint_field.setText("not-a-footprint")
    inspector.name_field.setText("議長席")
    inspector.editCommitted.emit()
    app.processEvents()

    # Commit refused: entity unchanged, error pinned to the geometry section,
    # and the user's in-progress edits were not silently dropped (#583).
    entity_after = workspace.controller.document.entity(entity.entity_id)
    assert entity_after.body_geometry is None
    assert inspector.geometry_section.error_label.isVisible()
    assert inspector.footprint_field.text() == "not-a-footprint"
    assert inspector.name_field.text() == "議長席"

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_inspector_display_units_apply_to_length_fields(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    entity = workspace.controller.add_object("seat")
    workspace.select_entity(entity.entity_id)
    app.processEvents()
    inspector = workspace.inspector
    inspector.set_display_units(length_unit="cm", precision=1)
    app.processEvents()
    x_m = workspace.controller.document.entity(entity.entity_id).position.x_m
    assert inspector.position_editor.fields["X"].value() == pytest.approx(
        x_m * 100.0
    )
    # #496 hook: storage still answers in metres.
    assert inspector.position_editor.fields["X"].value_m() == pytest.approx(x_m)
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_inspector_edit_commits_exact_untouched_axes(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    entity = workspace.controller.add_object("speaker")
    workspace.select_entity(entity.entity_id)
    app.processEvents()
    inspector = workspace.inspector
    doc = workspace.controller.document
    before = doc.entity(entity.entity_id).position
    inspector.position_editor.fields["Z"].set_value_m(2.5)
    inspector.editCommitted.emit()
    app.processEvents()
    # `update_entities` replaces the working document — re-fetch post-commit.
    after = workspace.controller.document.entity(entity.entity_id).position
    assert after.z_m == pytest.approx(2.5)
    assert after.x_m == pytest.approx(before.x_m)
    assert after.y_m == pytest.approx(before.y_m)
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_selection_inspector_preserves_test_hooks() -> None:
    # Existing suite anchors must keep working after the restructure.
    _app()
    inspector = SelectionInspector()
    for name in (
        "name_field",
        "role_field",
        "shape_field",
        "radius_field",
        "footprint_field",
        "orientation_fields",
        "orientation_header",
        "aim_section",
        "aim_known_host",
        "aim_state_label",
        "aim_target_combo",
        "aim_apply_button",
        "aim_clear_button",
        "aim_align_button",
        "aim_yaw_field",
        "aim_pitch_field",
        "position_fields",
        "size_fields",
        "id_label",
        "values",
    ):
        assert getattr(inspector, name, None) is not None
    inspector.deleteLater()


def test_pending_text_survives_section_hide_show(tmp_path) -> None:
    """Typed-but-uninterpreted input must survive ancestor show events (#583).

    QAbstractSpinBox re-syncs the line text to the committed value on
    showEvent — section visibility toggles would otherwise drop in-flight
    edits before a Ctrl+S flush can commit them.
    """

    app, workspace = _workspace(tmp_path)
    entity = workspace.controller.add_object("speaker")
    workspace.select_entity(entity.entity_id)
    app.processEvents()

    field = workspace.inspector.position_fields["Y"]
    field.setFocus()
    field.lineEdit().setText("1.75")
    section = workspace.inspector.transform_section
    section.setVisible(False)
    app.processEvents()
    section.setVisible(True)
    app.processEvents()
    assert "1.75" in field.lineEdit().text()

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_metric_spinbox_conversion_matches_display_unit_authority() -> None:
    """#496: the spinbox must reuse cad_display_units conversion semantics."""

    _app()
    field = MetricSpinBox()
    field.set_display_unit("inch")
    field.setValue(10.0)
    # display_to_si uses the exact 0.0254 m/inch definition, not a divide by
    # the display scale (1/0.0254) — the two differ by a few ulp.
    assert field.value_m() == display_to_si(10.0, "inch")
    field.set_display_unit("mm")
    assert field.value_m() == display_to_si(field.value(), "mm")
    # Per-unit default decimals follow the authority policy (inch: 2).
    assert MetricSpinBox.UNIT_DECIMALS == DEFAULT_DISPLAY_DECIMALS


def test_inspector_display_units_follow_application_preferences(tmp_path) -> None:
    """#496 wiring: display_input.* preferences drive inspector fields."""

    app, workspace = _workspace(tmp_path)
    preferences = ApplicationPreferenceStore(tmp_path / "prefs.json")
    bind_inspector_display_length_policy(workspace.inspector, preferences)
    app.processEvents()

    field = workspace.inspector.position_fields["X"]
    # Store default is 'mm' at precision 2 — bound on apply.
    assert field.suffix() == " mm"
    assert field.decimals() == 2

    entity = workspace.controller.add_object("seat")
    workspace.select_entity(entity.entity_id)
    app.processEvents()
    x_m = workspace.controller.document.entity(entity.entity_id).position.x_m
    assert field.value() == pytest.approx(x_m * 1000.0)

    # A later preference commit re-renders fields in the new unit.
    preferences.set("display_input.length_unit", "cm")
    preferences.set("display_input.numeric_precision", 4)
    app.processEvents()
    assert field.suffix() == " cm"
    assert field.decimals() == 4
    assert field.value() == pytest.approx(x_m * 100.0)
    # Canonical storage stays SI metres.
    assert field.value_m() == pytest.approx(x_m)

    workspace.close()
    workspace.deleteLater()
    app.processEvents()
