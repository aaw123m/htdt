"""REV40-OVERLAY: ``display_input.reflection_guidance_overlay`` — the
persisted off | auto | on setting from the 設定 dialog gates viewport
markers that project replayed deterministic path authority (never
invented geometry): honest empty state, acoustics-scoped 'auto',
live-apply, guides_visible deference, and a JA settings row.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QComboBox, QLabel

from htdt.application_preferences import (
    PREFERENCE_DEFINITIONS,
    ApplicationPreferenceStore,
)
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.cad_schema import connect_sqlite
from htdt.cad_repository import SceneRepository
from htdt.reflection_guidance_presentation import (
    ReflectionGuidanceView,
    guidance_overlay_markers,
)
from htdt.reflection_guidance_ui import ReflectionGuidancePanel
from htdt.room_viewport import RoomViewport3D
from htdt.room_workspace import RoomWorkspace
from htdt.workflow_settings import PreferencesWidget

import test_reflection_guidance_ui as guidance_ui
import test_room_cadux as cadux
import test_room_viewport_r14 as r14


_OVERLAY_KEY = 'display_input.reflection_guidance_overlay'


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _GuidanceViewport(cadux.FakeRoomViewport):
    """Viewport double recording the markers each render resolves."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.guidance_calls: list[tuple] = []

    def render_reflection_guidance_overlay(self, markers) -> None:
        self.guidance_calls.append(tuple(markers))


def _seeded_workspace(
    tmp_path,
) -> tuple[RoomWorkspace, ReflectionGuidancePanel]:
    """A real workspace + guidance panel over a repository carrying two
    persisted guidance entries (wall-left + floor)."""
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    revision = repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision.revision_id
    _app()
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: _GuidanceViewport(parent),
    )
    with connect_sqlite(repository.path) as connection, connection:
        guidance_ui._seed_guidance(
            connection,
            document_id=F1_DOCUMENT_ID,
            revision_id=revision,
        )
    panel = ReflectionGuidancePanel(workspace.controller)
    return workspace, panel


# -- persistence ---------------------------------------------------------------


def test_overlay_preference_registers_and_round_trips(tmp_path) -> None:
    definition = PREFERENCE_DEFINITIONS[_OVERLAY_KEY]
    assert definition.default == 'auto'
    assert definition.allowed_values == ('off', 'auto', 'on')
    store = ApplicationPreferenceStore.for_data_dir(tmp_path)
    assert store.get(_OVERLAY_KEY) == 'auto'
    change = store.set(_OVERLAY_KEY, 'on')
    assert change.old == 'auto' and change.new == 'on'
    reloaded = ApplicationPreferenceStore.for_data_dir(tmp_path)
    assert reloaded.get(_OVERLAY_KEY) == 'on'
    with pytest.raises(ValueError):
        store.set(_OVERLAY_KEY, 'sometimes')


def test_settings_dialog_exposes_ja_overlay_row(tmp_path) -> None:
    _app()
    store = ApplicationPreferenceStore.for_data_dir(tmp_path)
    widget = PreferencesWidget(store)
    editor = widget._editors[_OVERLAY_KEY]
    assert isinstance(editor, QComboBox)
    # Not a 準備中 stub — the viewport consumes this key, so it stays
    # enabled, and every value carries a JA display label.
    assert editor.isEnabled()
    assert [
        editor.itemText(index) for index in range(editor.count())
    ] == ['表示しない', '自動（音響コンテキストのみ）', '常に表示']
    assert editor.toolTip()
    assert editor.whatsThis() == editor.toolTip()
    label = next(
        lab
        for lab in widget.findChildren(QLabel)
        if lab.text() == '反射ガイダンスのオーバーレイ'
    )
    assert label.toolTip()
    assert label.whatsThis() == label.toolTip()


# -- marker projection ---------------------------------------------------------


def test_markers_project_only_persisted_authority(tmp_path) -> None:
    repository, revision_id = guidance_ui._seed_document(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        guidance_ui._seed_guidance(
            connection,
            document_id=F1_DOCUMENT_ID,
            revision_id=revision_id,
        )
    view = guidance_ui._load(repository, F1_DOCUMENT_ID)
    markers = guidance_overlay_markers(view)
    assert len(view.entries) == 2
    assert {m.surface_id for m in markers} == {'wall-left', 'floor'}
    for entry in view.entries:
        marker = next(
            m for m in markers if m.surface_id == entry.surface_id
        )
        top = entry.report.items[0]
        assert top.kind == 'treat_reflection_zone'
        assert marker.label == (
            f'1. 一次反射ゾーンの処理 · {entry.surface_id}'
        )
        assert marker.confidence == 'unverified_hypothesis'
        # The zone anchor is the treat item's proven centroid — nothing
        # else is synthesized.
        assert marker.zone_anchor == top.reflection_point
        assert marker.source_anchor == guidance_ui.SOURCE_REFERENCE_POINT
        assert marker.source_label == '2. 音源の再配置'
        assert marker.path_points == (
            guidance_ui.SOURCE_REFERENCE_POINT,
            entry.plane_point,
            guidance_ui.RECEIVER_POSITION,
        )


def test_markers_empty_when_nothing_persisted(tmp_path) -> None:
    repository, _revision_id = guidance_ui._seed_document(tmp_path)
    view = guidance_ui._load(repository, F1_DOCUMENT_ID)
    assert view.entries == () and view.issues == ()
    assert guidance_overlay_markers(view) == ()


def test_markers_degrade_honestly_without_snapshot_geometry(
    tmp_path,
) -> None:
    repository, revision_id = guidance_ui._seed_document(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        guidance_ui._seed_guidance(
            connection,
            document_id=F1_DOCUMENT_ID,
            revision_id=revision_id,
            include_geometry=False,
        )
    view = guidance_ui._load(repository, F1_DOCUMENT_ID)
    markers = guidance_overlay_markers(view)
    assert len(markers) == 2
    for marker in markers:
        # The treat-zone anchor survives a degraded snapshot binding;
        # source/path anchors do not — no fabricated positions.
        assert marker.zone_anchor is not None
        assert marker.source_anchor is None
        assert marker.source_label is None
        assert marker.path_points == ()


# -- workspace policy gating ---------------------------------------------------


def test_workspace_on_mode_draws_and_off_hides(tmp_path) -> None:
    workspace, panel = _seeded_workspace(tmp_path)
    preferences = ApplicationPreferenceStore.for_data_dir(tmp_path)
    preferences.set(_OVERLAY_KEY, 'on')
    workspace.bind_reflection_guidance(panel, preferences)
    last = workspace.viewport.guidance_calls[-1]
    assert len(last) == 2
    # Live-apply without restart: the store listener re-renders.
    preferences.set(_OVERLAY_KEY, 'off')
    assert workspace.viewport.guidance_calls[-1] == ()


def test_workspace_auto_scopes_to_acoustics_context(tmp_path) -> None:
    workspace, panel = _seeded_workspace(tmp_path)
    preferences = ApplicationPreferenceStore.for_data_dir(tmp_path)
    # 'auto' is the persisted default — no explicit set needed.
    workspace.bind_reflection_guidance(panel, preferences)
    assert workspace.viewport.guidance_calls[-1] == ()
    workspace.set_context('acoustics')
    assert len(workspace.viewport.guidance_calls[-1]) == 2
    workspace.set_context('geometry')
    assert workspace.viewport.guidance_calls[-1] == ()


def test_workspace_guides_toggle_suppresses_overlay(tmp_path) -> None:
    workspace, panel = _seeded_workspace(tmp_path)
    preferences = ApplicationPreferenceStore.for_data_dir(tmp_path)
    preferences.set(_OVERLAY_KEY, 'on')
    workspace.bind_reflection_guidance(panel, preferences)
    assert len(workspace.viewport.guidance_calls[-1]) == 2
    workspace.toggle_guides()
    assert workspace.viewport.guidance_calls[-1] == ()
    workspace.toggle_guides()
    assert len(workspace.viewport.guidance_calls[-1]) == 2


def test_workspace_unbound_and_empty_views_draw_nothing(tmp_path) -> None:
    workspace, panel = _seeded_workspace(tmp_path)
    preferences = ApplicationPreferenceStore.for_data_dir(tmp_path)
    preferences.set(_OVERLAY_KEY, 'on')
    # Unbound: no view + default 'off' policy — no markers ever.
    assert all(
        calls == () for calls in workspace.viewport.guidance_calls
    )
    workspace.bind_reflection_guidance(panel, preferences)
    assert len(workspace.viewport.guidance_calls[-1]) == 2
    # An emptied store view projects nothing — honest empty state.
    workspace._guidance_view = ReflectionGuidanceView(
        document_id=F1_DOCUMENT_ID, entries=(), issues=()
    )
    workspace._render()
    assert workspace.viewport.guidance_calls[-1] == ()


# -- viewport renderer ---------------------------------------------------------


def _marker(view, surface_id: str):
    return next(
        m for m in guidance_overlay_markers(view) if m.surface_id == surface_id
    )


def test_viewport_draws_named_guidance_actors(tmp_path) -> None:
    _app()
    repository, revision_id = guidance_ui._seed_document(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        guidance_ui._seed_guidance(
            connection,
            document_id=F1_DOCUMENT_ID,
            revision_id=revision_id,
        )
    view = guidance_ui._load(repository, F1_DOCUMENT_ID)
    viewport = RoomViewport3D()
    plotter = r14._CapturingPlotter()
    viewport.plotter = plotter
    viewport.render_reflection_guidance_overlay(
        guidance_overlay_markers(view)
    )
    names = [kwargs.get('name', '') for _mesh, kwargs in plotter.meshes]
    assert any(name.startswith('guidance-zone-') for name in names)
    assert any(name.startswith('guidance-source-') for name in names)
    assert any(name.startswith('guidance-path-') for name in names)
    # Every guidance actor joins the signature-skipped overlay lane so a
    # re-render clears it — nothing stale survives the next draw.
    assert all(
        name.startswith('guidance-')
        for name in names
        if name.startswith('guidance')
    )
    assert plotter.renders >= 1


def test_viewport_empty_markers_draw_nothing(tmp_path) -> None:
    _app()
    viewport = RoomViewport3D()
    plotter = r14._CapturingPlotter()
    viewport.plotter = plotter
    viewport.render_reflection_guidance_overlay(())
    assert plotter.meshes == []
    assert plotter.renders == 0


def test_panel_refresh_emits_guidance_view_changed(tmp_path) -> None:
    _app()
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    from htdt.room_workspace import RoomWorkspaceController

    panel = ReflectionGuidancePanel(
        RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    )
    assert panel.guidance_view.document_id == F1_DOCUMENT_ID
    fired: list[bool] = []
    panel.guidanceViewChanged.connect(lambda: fired.append(True))
    panel.refresh()
    assert fired == [True]
