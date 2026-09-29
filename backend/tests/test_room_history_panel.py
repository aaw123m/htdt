"""Offscreen Qt coverage for RoomHistoryPanel (#485) — round 2.

The panel is the read surface for the immutable revision lineage: rows must
mark HEAD/detached, carry labels, keep selection across re-syncs, and gate
diff/restore/label emissions on a real selection.
"""

from __future__ import annotations

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication

from htdt.cad_repository import RevisionLabel, SceneRevision
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    scene_content_hash,
)
from htdt.room_history_panel import RoomHistoryPanel


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _doc(*entities: SceneEntity) -> SceneDocument:
    return SceneDocument(
        document_id='doc-1',
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=tuple(entities),
    )


def _speaker(entity_id: str, name: str) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=name,
        speaker_role='FL',
        position=Position3(x_m=1.0, y_m=0.75, z_m=1.05),
        size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
    )


def _revision(revision_id: str, document: SceneDocument, *, detached: bool = False) -> SceneRevision:
    return SceneRevision(
        revision_id=revision_id,
        document_id=document.document_id,
        parent_revision_id=None,
        created_at_utc='2026-09-26T10:00:00Z',
        content_hash=scene_content_hash(document),
        document=document,
        detached=detached,
    )


def test_sync_revisions_marks_head_detached_and_labels() -> None:
    _app()
    panel = RoomHistoryPanel()
    revisions = (
        _revision('rev-head', _doc(_speaker('sp-1', 'FL'))),
        _revision('rev-old', _doc(), detached=True),
    )
    labels = {
        'rev-old': RevisionLabel(
            revision_id='rev-old', label='v1 候補', note='', updated_at_utc='t'
        )
    }
    panel.sync_revisions(revisions, head_revision_id='rev-head', labels=labels)

    assert panel.tree.topLevelItemCount() == 2
    head_item = panel.tree.topLevelItem(0)
    detached_item = panel.tree.topLevelItem(1)
    assert '●HEAD' in head_item.text(0)
    assert '◇detached' in detached_item.text(0)
    assert detached_item.text(1) == 'v1 候補'
    assert '2 リビジョン' in panel.summary.text()
    assert 'ラベル 1 件' in panel.summary.text()


def test_selection_drives_diff_restore_and_preview_signals() -> None:
    _app()
    panel = RoomHistoryPanel()
    revisions = (_revision('rev-a', _doc(_speaker('sp-1', 'FL'))),)
    panel.sync_revisions(revisions, head_revision_id='rev-a', labels={})

    emitted: dict[str, list] = {'diff': [], 'restore': [], 'preview': []}
    panel.diffRequested.connect(lambda rid: emitted['diff'].append(rid))
    panel.restoreRequested.connect(lambda rid: emitted['restore'].append(rid))
    panel.previewRequested.connect(lambda rid: emitted['preview'].append(rid))

    item = panel.tree.topLevelItem(0)
    panel.tree.setCurrentItem(item)
    assert panel.selected_revision_id() == 'rev-a'
    # Selecting already emitted a diff request for the detail pane.
    assert emitted['diff'] == ['rev-a']

    panel.diff_button.click()
    assert emitted['diff'][-1] == 'rev-a'
    panel.restore_button.click()
    assert emitted['restore'] == ['rev-a']

    panel.preview_button.setChecked(True)
    assert emitted['preview'][-1] == 'rev-a'
    panel.preview_button.setChecked(False)
    assert emitted['preview'][-1] is None


def test_actions_without_selection_emit_nothing() -> None:
    _app()
    panel = RoomHistoryPanel()
    panel.sync_revisions((), head_revision_id=None, labels={})

    emitted: list = []
    panel.restoreRequested.connect(emitted.append)
    panel.restore_button.click()
    panel.diff_button.click()
    assert emitted == []
    assert panel.selected_revision_id() is None


def test_label_requires_selection_and_nonempty_text() -> None:
    _app()
    panel = RoomHistoryPanel()
    panel.sync_revisions(
        (_revision('rev-a', _doc()),), head_revision_id='rev-a', labels={}
    )
    emitted: list = []
    panel.labelRequested.connect(lambda *args: emitted.append(args))

    # No selection: nothing emitted.
    panel.set_label_fields('v2', 'note')
    panel.label_button.click()
    assert emitted == []

    panel.tree.setCurrentItem(panel.tree.topLevelItem(0))
    # Empty label text is refused.
    panel.set_label_fields('   ', 'note')
    panel.label_button.click()
    assert emitted == []

    panel.set_label_fields('v2', '  memo  ')
    panel.label_button.click()
    assert emitted == [('rev-a', 'v2', 'memo')]


def test_resync_preserves_selection_and_show_detail() -> None:
    _app()
    panel = RoomHistoryPanel()
    panel.sync_revisions(
        (_revision('rev-a', _doc()), _revision('rev-b', _doc())),
        head_revision_id='rev-a',
        labels={},
    )
    panel.tree.setCurrentItem(panel.tree.topLevelItem(1))
    assert panel.selected_revision_id() == 'rev-b'

    # A refresh keeps the same revision selected.
    panel.sync_revisions(
        (_revision('rev-a', _doc()), _revision('rev-b', _doc())),
        head_revision_id='rev-a',
        labels={},
    )
    assert panel.selected_revision_id() == 'rev-b'

    panel.show_detail('差分テキスト')
    assert panel.detail.toPlainText() == '差分テキスト'


def test_label_fields_reflect_stored_label_on_selection() -> None:
    _app()
    panel = RoomHistoryPanel()
    labels = {
        'rev-a': RevisionLabel(
            revision_id='rev-a',
            label='v1候補',
            note='初期状態',
            updated_at_utc='2026-09-26T10:00:00Z',
        )
    }
    panel.sync_revisions(
        (_revision('rev-a', _doc()), _revision('rev-b', _doc())),
        head_revision_id='rev-b',
        labels=labels,
    )

    panel.tree.setCurrentItem(panel.tree.topLevelItem(0))
    assert panel.label_field.text() == 'v1候補'
    assert panel.note_field.text() == '初期状態'

    panel.tree.setCurrentItem(panel.tree.topLevelItem(1))
    assert panel.label_field.text() == ''
    assert panel.note_field.text() == ''


def test_detached_row_names_its_true_parent() -> None:
    from htdt.cad_display_labels import saved_label

    _app()
    panel = RoomHistoryPanel()
    doc = _doc()
    parent = _revision('rev-p', doc)
    detached = SceneRevision(
        revision_id='rev-d',
        document_id=doc.document_id,
        parent_revision_id='rev-p',
        created_at_utc='2026-09-26T11:00:00Z',
        content_hash=scene_content_hash(doc),
        document=doc,
        detached=True,
    )
    panel.sync_revisions((parent, detached), head_revision_id='rev-p', labels={})

    row_text = panel.tree.topLevelItem(1).text(0)
    assert 'detached' in row_text
    assert '←' in row_text
    assert saved_label(parent.created_at_utc) in row_text
