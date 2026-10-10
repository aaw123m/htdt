"""Offscreen coverage for #978 — objects panel search, state filters,
kind groups, temporary isolation, and list↔3D sync.

The object browser must let an operator find and act on the right entity in
a dense room without ever resolving edits by row position: every emitted
id is a stable ``entity_id`` read back from item data, group headers are
non-selectable navigation affordances, and 「選択のみ表示」 (temporary view
isolation) stays visually distinct from 隠す/ロック (persistent view state).
"""

from __future__ import annotations

import os
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from htdt.cad_scene import (  # noqa: E402
    BodyMeshAsset,
    BodyMeshTriangle,
    BodyMeshVertex,
    EntityBodyGeometry,
    F1_DOCUMENT_ID,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.room_objects_panel import (  # noqa: E402
    OBJECT_FILTER_HIDDEN,
    OBJECT_FILTER_LOCKED,
    OBJECT_FILTER_PROBLEM,
    OBJECT_FILTER_SELECTED,
    RoomObjectsPanel,
    entity_problem_reasons,
)
from htdt.room_workspace import RoomWorkspace  # noqa: E402

from test_room_cadux import FakeRoomViewport, _f1_repository  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


KIND_LABELS = {
    'speaker': 'スピーカー',
    'seat': '座席',
    'screen': 'スクリーン',
    'display': 'ディスプレイ',
    'projector': 'プロジェクター',
    'riser': 'ライザー',
    'furniture': '家具',
    'av_equipment': 'AV機器',
    'measurement_point': '測定点',
}


def _entity(entity_id: str, kind: str, name: str, **overrides) -> SceneEntity:
    kwargs: dict = dict(
        entity_id=entity_id,
        kind=kind,
        name=name,
        position=overrides.pop(
            'position', Position3(x_m=1.0, y_m=1.0, z_m=0.5)
        ),
    )
    if kind != 'measurement_point':
        kwargs['size_m'] = overrides.pop(
            'size_m', Size3(x_m=0.3, y_m=0.3, z_m=0.4)
        )
    if kind == 'speaker':
        kwargs['speaker_role'] = overrides.pop('speaker_role', 'FL')
    kwargs.update(overrides)
    return SceneEntity(**kwargs)


def _doc(entities: tuple[SceneEntity, ...]) -> SceneDocument:
    return SceneDocument(
        document_id='doc-978',
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=entities,
    )


def _basic_doc() -> SceneDocument:
    return _doc(
        (
            _entity('speaker-fl', 'speaker', 'Front Left', speaker_role='FL'),
            _entity('speaker-fr', 'speaker', 'Front Right', speaker_role='SR'),
            _entity('seat-1', 'seat', 'Seat 1'),
            _entity('amp-1', 'av_equipment', 'AVR-1'),
            _entity('point-mlp', 'measurement_point', 'MLP'),
        )
    )


def _sync(
    panel: RoomObjectsPanel,
    document: SceneDocument | None = None,
    **overrides,
) -> None:
    args = dict(
        selected_ids=(),
        primary_id=None,
        hidden_ids=frozenset(),
        locked_ids=frozenset(),
        kind_labels=KIND_LABELS,
    )
    args.update(overrides)
    panel.sync_document(document if document is not None else _basic_doc(), **args)


def _set_filter(panel: RoomObjectsPanel, key: str) -> None:
    index = panel.filter_combo.findData(key)
    assert index >= 0, key
    panel.filter_combo.setCurrentIndex(index)


def _group_items(panel: RoomObjectsPanel) -> list:
    return [
        panel.tree.topLevelItem(i) for i in range(panel.tree.topLevelItemCount())
    ]


def test_search_matches_name_kind_label_role_and_entity_id() -> None:
    _app()
    panel = RoomObjectsPanel()
    _sync(panel)

    panel.search_edit.setText('front left')
    assert set(panel._entity_items) == {'speaker-fl'}
    panel.search_edit.setText('スピーカー')  # JA kind label
    assert set(panel._entity_items) == {'speaker-fl', 'speaker-fr'}
    panel.search_edit.setText('SR')  # speaker role (not in any name)
    assert set(panel._entity_items) == {'speaker-fr'}
    panel.search_edit.setText('amp-1')  # stable entity id
    assert set(panel._entity_items) == {'amp-1'}
    panel.search_edit.clear()
    assert len(panel._entity_items) == 5


def test_state_filters_select_hidden_locked_and_problem() -> None:
    _app()
    panel = RoomObjectsPanel()
    _sync(
        panel,
        selected_ids=('speaker-fl',),
        primary_id='speaker-fl',
        hidden_ids={'seat-1'},
        locked_ids={'amp-1'},
    )

    _set_filter(panel, OBJECT_FILTER_SELECTED)
    assert set(panel._entity_items) == {'speaker-fl'}
    _set_filter(panel, OBJECT_FILTER_HIDDEN)
    assert set(panel._entity_items) == {'seat-1'}
    _set_filter(panel, OBJECT_FILTER_LOCKED)
    assert set(panel._entity_items) == {'amp-1'}
    _set_filter(panel, OBJECT_FILTER_PROBLEM)
    assert set(panel._entity_items) == set()  # basic doc has no problems
    panel.reset_filters()
    assert len(panel._entity_items) == 5


def test_zero_state_shows_condition_and_reset_path() -> None:
    _app()
    panel = RoomObjectsPanel()
    _sync(panel)

    panel.search_edit.setText('存在しない名前')
    groups = _group_items(panel)
    assert len(groups) == 1  # the zero-state row, not a kind group
    zero = groups[0]
    assert '一致する項目はありません' in zero.text(0)
    assert '存在しない名前' in zero.toolTip(0)  # the active condition is named
    assert not (zero.flags() & Qt.ItemFlag.ItemIsSelectable)
    assert panel.reset_filter_button.isEnabled()

    panel.reset_filter_button.click()
    assert len(panel._entity_items) == 5
    assert not panel.reset_filter_button.isEnabled()


def test_kind_groups_expand_collapse_and_primary_reveal() -> None:
    _app()
    panel = RoomObjectsPanel()
    _sync(panel)

    groups = {item.text(0): item for item in _group_items(panel)}
    assert 'スピーカー（2）' in groups
    speaker_group = groups['スピーカー（2）']
    assert speaker_group.childCount() == 2
    # Group headers are navigation affordances, not selectable rows.
    assert not (speaker_group.flags() & Qt.ItemFlag.ItemIsSelectable)

    speaker_group.setExpanded(False)
    _sync(panel)  # collapse state survives a full re-sync
    speaker_group = _group_items(panel)[0]
    assert not speaker_group.isExpanded()

    # A 3D pick re-expands the group holding the primary.
    _sync(panel, selected_ids=('speaker-fr',), primary_id='speaker-fr')
    speaker_group = _group_items(panel)[0]
    assert speaker_group.isExpanded()
    assert panel.item_for_entity('speaker-fr').isSelected()


def test_isolation_buttons_emit_signals_and_track_state() -> None:
    _app()
    panel = RoomObjectsPanel()
    isolate_calls: list = []
    clear_calls: list = []
    focus_calls: list = []
    panel.isolationRequested.connect(lambda: isolate_calls.append(1))
    panel.isolationClearRequested.connect(lambda: clear_calls.append(1))
    panel.focusRequested.connect(lambda: focus_calls.append(1))

    _sync(panel)
    assert not panel.isolate_button.isEnabled()  # no selection
    assert not panel.clear_isolation_button.isEnabled()

    _sync(panel, selected_ids=('speaker-fl',), primary_id='speaker-fl')
    assert panel.isolate_button.isEnabled()
    assert panel.focus_button.isEnabled()
    assert not panel.clear_isolation_button.isEnabled()

    panel.isolate_button.click()
    panel.focus_button.click()
    assert isolate_calls == [1]
    assert focus_calls == [1]

    _sync(
        panel,
        selected_ids=('speaker-fl',),
        primary_id='speaker-fl',
        isolation_active=True,
    )
    assert panel.clear_isolation_button.isEnabled()
    assert '隔離中' in panel.summary.text()
    panel.clear_isolation_button.click()
    assert clear_calls == [1]

    # Double-click on an entity row also focuses; group headers do not.
    item = panel.item_for_entity('speaker-fl')
    panel._item_double_clicked(item, 0)
    group = _group_items(panel)[0]
    panel._item_double_clicked(group, 0)
    assert focus_calls == [1, 1]


def test_filtered_out_selection_is_reported() -> None:
    _app()
    panel = RoomObjectsPanel()
    _sync(panel, selected_ids=('speaker-fl', 'seat-1'), primary_id='seat-1')
    _set_filter(panel, OBJECT_FILTER_LOCKED)  # neither selected entity is locked
    assert panel._entity_items == {}
    assert 'フィルタ外の選択 2 件' in panel.summary.text()


def test_problem_reasons_flags_and_filter() -> None:
    _app()
    doc = _doc(
        (
            _entity(
                'speaker-unassigned',
                'speaker',
                'Unassigned',
                speaker_role='UNASSIGNED-1',
            ),
            _entity('speaker-fl', 'speaker', 'FL one', speaker_role='FL'),
            _entity('speaker-fl2', 'speaker', 'FL two', speaker_role='FL'),
            _entity(
                'seat-outside',
                'seat',
                'Outside',
                position=Position3(x_m=20.0, y_m=1.0, z_m=0.5),
            ),
            _entity('amp-1', 'av_equipment', 'AVR-1'),
        )
    )
    reasons = entity_problem_reasons(doc)
    assert set(reasons) == {'speaker-unassigned', 'speaker-fl', 'speaker-fl2', 'seat-outside'}
    assert any('未割当' in r for r in reasons['speaker-unassigned'])
    assert any('重複' in r for r in reasons['speaker-fl'])
    assert any('範囲外' in r for r in reasons['seat-outside'])

    panel = RoomObjectsPanel()
    _sync(panel, doc)
    _set_filter(panel, OBJECT_FILTER_PROBLEM)
    assert set(panel._entity_items) == set(reasons)
    assert '⚠' in panel.item_for_entity('seat-outside').text(0)
    assert panel.item_for_entity('seat-outside').toolTip(0)


def test_imported_mesh_entities_get_their_own_group() -> None:
    _app()
    mesh = BodyMeshAsset(
        asset_sha256='a' * 64,
        source_name='imported.stl',
        asset_format='stl',
        original_size_bytes=12,
        vertices=(
            BodyMeshVertex(x_m=0.0, y_m=0.0, z_m=0.0),
            BodyMeshVertex(x_m=0.1, y_m=0.0, z_m=0.0),
            BodyMeshVertex(x_m=0.0, y_m=0.1, z_m=0.1),
        ),
        triangles=(BodyMeshTriangle(a=0, b=1, c=2),),
    )
    doc = _doc(
        (
            _entity('speaker-fl', 'speaker', 'Front Left', speaker_role='FL'),
            _entity(
                'mesh-table',
                'furniture',
                'Imported table',
                body_geometry=EntityBodyGeometry(kind='mesh_asset', mesh=mesh),
            ),
            _entity('chair', 'furniture', 'Plain chair'),
        )
    )
    panel = RoomObjectsPanel()
    _sync(panel, doc)
    labels = [item.text(0) for item in _group_items(panel)]
    assert '取込メッシュ（1）' in labels
    mesh_group = _group_items(panel)[labels.index('取込メッシュ（1）')]
    assert mesh_group.child(0).data(0, Qt.ItemDataRole.UserRole) == 'mesh-table'
    # A plain furniture entity stays in the furniture group.
    furniture_group = _group_items(panel)[labels.index('家具（1）')]
    assert furniture_group.child(0).data(0, Qt.ItemDataRole.UserRole) == 'chair'


def test_actions_under_filter_emit_stable_entity_ids() -> None:
    _app()
    panel = RoomObjectsPanel()
    _sync(panel, hidden_ids={'speaker-fr', 'amp-1'})
    _set_filter(panel, OBJECT_FILTER_HIDDEN)

    delete_calls: list = []
    panel.deleteRequested.connect(lambda ids: delete_calls.append(ids))
    panel.item_for_entity('speaker-fr').setSelected(True)
    panel.delete_button.click()
    # The emitted id is the row's entity_id — never its visual position.
    assert delete_calls[-1] == ('speaker-fr',)


def test_mass_scene_keeps_entity_ids_correct() -> None:
    _app()
    entities = []
    for index in range(120):
        kind = ('speaker', 'seat', 'furniture', 'av_equipment')[index % 4]
        role = f'R{index}' if kind == 'speaker' else None
        entities.append(
            _entity(
                f'{kind}-{index:03d}',
                kind,
                f'Item {index:03d}',
                speaker_role=role,
            )
        )
    doc = _doc(tuple(entities))
    panel = RoomObjectsPanel()
    hidden = {e.entity_id for e in entities[::7]}

    start = time.monotonic()
    _sync(panel, doc, hidden_ids=hidden)
    elapsed = time.monotonic() - start
    assert len(panel._entity_items) == 120
    assert elapsed < 5.0, f'sync_document too slow: {elapsed:.2f}s'

    panel.search_edit.setText('Item 042')
    assert set(panel._entity_items) == {'furniture-042'}
    panel.search_edit.clear()

    _set_filter(panel, OBJECT_FILTER_HIDDEN)
    assert set(panel._entity_items) == hidden
    hide_calls: list = []
    panel.hideRequested.connect(lambda ids, h: hide_calls.append((ids, h)))
    panel.item_for_entity('av_equipment-007').setSelected(True)
    panel.show_button.click()
    assert hide_calls[-1] == (('av_equipment-007',), False)


def _room_workspace(tmp_path):
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    return app, workspace


def _close(app: QApplication, workspace: RoomWorkspace) -> None:
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_workspace_isolation_is_temporary_and_restorable(tmp_path) -> None:
    app, workspace = _room_workspace(tmp_path)
    try:
        controller = workspace.controller
        f1_ids = {
            'speaker-fl', 'speaker-c', 'speaker-fr', 'point-mlp', 'furniture-left'
        }
        assert f1_ids == {e.entity_id for e in controller.document.entities}

        # A pre-existing persistent hide must survive the isolation round-trip.
        controller.set_entities_hidden(('furniture-left',), True)
        controller.set_selection('speaker-fl')
        workspace._after_selection_changed()
        workspace.objects_panel.isolate_button.click()

        assert controller.view_state.hidden_ids == f1_ids - {'speaker-fl'}
        assert '隔離中' in workspace.objects_panel.summary.text()
        assert workspace.objects_panel.clear_isolation_button.isEnabled()

        workspace.objects_panel.clear_isolation_button.click()
        assert controller.view_state.hidden_ids == {'furniture-left'}
        assert '隔離中' not in workspace.objects_panel.summary.text()
        assert not workspace.objects_panel.clear_isolation_button.isEnabled()
        # The persistent hide is still a distinct — marker on the row.
        assert workspace.objects_panel.item_for_entity('furniture-left').text(2) == '—'
    finally:
        _close(app, workspace)


def test_workspace_focus_button_frames_selection(tmp_path) -> None:
    app, workspace = _room_workspace(tmp_path)
    try:
        workspace.controller.set_selection('speaker-fr')
        workspace._after_selection_changed()
        workspace.objects_panel.focus_button.click()
        assert workspace.viewport.focused_ids[-1] == ('speaker-fr',)
    finally:
        _close(app, workspace)


def test_workspace_pick_expands_collapsed_group_and_scrolls(tmp_path) -> None:
    app, workspace = _room_workspace(tmp_path)
    try:
        panel = workspace.objects_panel
        workspace._sync_objects_panel()
        groups = _group_items(panel)
        speaker_group = groups[0]
        speaker_group.setExpanded(False)
        workspace._sync_objects_panel()
        assert not _group_items(panel)[0].isExpanded()

        # Simulated 3D pick of an entity inside the collapsed group.
        workspace._entity_picked('speaker-fr')
        assert _group_items(panel)[0].isExpanded()
        assert panel.item_for_entity('speaker-fr').isSelected()
    finally:
        _close(app, workspace)
