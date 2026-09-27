"""Offscreen Qt coverage for RoomObjectsPanel (#482) — round 2.

The object browser is the always-visible selection surface for the Room
workspace: rows must mirror document entities with visibility/lock markers,
selection must round-trip through the sync API without recursive emission,
and batch action buttons must emit the currently selected ids.
"""

from __future__ import annotations

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication

from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.room_objects_panel import RoomObjectsPanel


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _doc() -> SceneDocument:
    return SceneDocument(
        document_id='doc-1',
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='Front Left',
                speaker_role='FL',
                position=Position3(x_m=1.35, y_m=0.75, z_m=1.05),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
            ),
            SceneEntity(
                entity_id='speaker-fr',
                kind='speaker',
                name='Front Right',
                speaker_role='FR',
                position=Position3(x_m=4.65, y_m=0.75, z_m=1.05),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _sync(panel: RoomObjectsPanel, **overrides) -> None:
    args = dict(
        selected_ids=(),
        primary_id=None,
        hidden_ids=frozenset(),
        locked_ids=frozenset(),
        kind_labels={'speaker': 'スピーカー', 'measurement_point': '測定点'},
    )
    args.update(overrides)
    panel.sync_document(_doc(), **args)


def test_sync_document_rows_mark_hidden_locked_and_primary() -> None:
    _app()
    panel = RoomObjectsPanel()
    _sync(
        panel,
        selected_ids=('speaker-fl',),
        primary_id='speaker-fl',
        hidden_ids={'speaker-fr'},
        locked_ids={'point-mlp'},
    )
    assert panel.tree.topLevelItemCount() == 3

    fl = panel.tree.topLevelItem(0)
    assert fl.text(0) == '▶ Front Left'  # primary marker
    assert fl.text(1) == 'スピーカー'
    assert fl.isSelected()

    fr = panel.tree.topLevelItem(1)
    assert fr.text(2) == '—'  # hidden marker
    mlp = panel.tree.topLevelItem(2)
    assert mlp.text(3) == '🔒'  # locked marker

    summary = panel.summary.text()
    assert 'オブジェクト 3 件' in summary
    assert '選択 1 件' in summary
    assert '非表示 1' in summary
    assert 'ロック 1' in summary


def test_selection_emits_ordered_ids_and_primary() -> None:
    _app()
    panel = RoomObjectsPanel()
    _sync(panel)

    emitted: list = []
    panel.selectionRequested.connect(lambda ids, primary: emitted.append((ids, primary)))

    panel.tree.topLevelItem(0).setSelected(True)
    panel.tree.topLevelItem(2).setSelected(True)
    ids = panel.selected_entity_ids()
    assert set(ids) == {'speaker-fl', 'point-mlp'}
    assert emitted
    last_ids, last_primary = emitted[-1]
    assert last_primary == last_ids[-1]


def test_sync_does_not_reemit_selection() -> None:
    _app()
    panel = RoomObjectsPanel()
    emitted: list = []
    panel.selectionRequested.connect(lambda ids, primary: emitted.append((ids, primary)))
    # Programmatic selection inside sync must not bounce back as a user
    # selection request.
    _sync(panel, selected_ids=('speaker-fl',), primary_id='speaker-fl')
    assert emitted == []


def test_action_buttons_emit_current_selection() -> None:
    _app()
    panel = RoomObjectsPanel()
    _sync(panel)

    hide_calls: list = []
    lock_calls: list = []
    delete_calls: list = []
    panel.hideRequested.connect(lambda ids, hide: hide_calls.append((ids, hide)))
    panel.lockRequested.connect(lambda ids, lock: lock_calls.append((ids, lock)))
    panel.deleteRequested.connect(lambda ids: delete_calls.append(ids))

    panel.tree.topLevelItem(1).setSelected(True)
    panel.hide_button.click()
    assert hide_calls[-1] == (('speaker-fr',), True)
    panel.show_button.click()
    assert hide_calls[-1] == (('speaker-fr',), False)
    panel.lock_button.click()
    assert lock_calls[-1] == (('speaker-fr',), True)
    panel.unlock_button.click()
    assert lock_calls[-1] == (('speaker-fr',), False)
    panel.delete_button.click()
    assert delete_calls[-1] == ('speaker-fr',)
