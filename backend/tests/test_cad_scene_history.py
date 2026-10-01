"""Structured SceneRevision history services (#485) — round 2 coverage.

``diff_scene_documents``/``diff_summary_lines``/``summarize_revision`` are the
read-side authority behind the history panel: they must report field-accurate
change lists and refuse cross-document diffs (lineage is per-document).
"""

from __future__ import annotations

import pytest

from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    scene_content_hash,
)
from htdt.cad_scene_history import (
    diff_scene_documents,
    diff_summary_lines,
    summarize_revision,
)


def _speaker(entity_id: str, name: str, *, x: float = 1.0, role: str = 'FL') -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=name,
        speaker_role=role,
        position=Position3(x_m=x, y_m=0.75, z_m=1.05),
        size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
    )


def _point(entity_id: str, name: str) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='measurement_point',
        name=name,
        position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
    )


def _doc(*entities: SceneEntity, document_id: str = 'doc-1', room=True) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4) if room else None,
        entities=tuple(entities),
    )


def test_diff_identical_documents_is_empty() -> None:
    doc = _doc(_speaker('sp-1', 'FL'))
    diff = diff_scene_documents(doc, doc)
    assert diff.is_empty
    assert diff.added_entity_ids == ()
    assert diff.removed_entity_ids == ()
    assert diff.entity_changes == ()
    assert not diff.room_changed


def test_diff_refuses_cross_document_comparison() -> None:
    before = _doc(document_id='doc-a')
    after = _doc(document_id='doc-b')
    with pytest.raises(ValueError, match='same document'):
        diff_scene_documents(before, after)


def test_diff_reports_added_and_removed_ids() -> None:
    before = _doc(_speaker('sp-1', 'FL'), _speaker('sp-2', 'FR', role='FR'))
    after = _doc(_speaker('sp-1', 'FL'), _point('pt-1', 'MLP'))
    diff = diff_scene_documents(before, after)
    assert diff.added_entity_ids == ('pt-1',)
    assert diff.removed_entity_ids == ('sp-2',)
    assert not diff.is_empty


def test_diff_reports_field_accurate_changes() -> None:
    before = _doc(_speaker('sp-1', 'FL', x=1.0))
    after_entity = _speaker('sp-1', 'FL renamed', x=2.5)
    after = _doc(after_entity)
    diff = diff_scene_documents(before, after)
    assert len(diff.entity_changes) == 1
    change = diff.entity_changes[0]
    assert change.entity_id == 'sp-1'
    assert change.entity_name == 'FL renamed'
    assert change.kind == 'speaker'
    # Only the fields that actually differ are reported.
    assert set(change.fields) == {'name', 'position'}


def test_diff_distinguishes_orientation_and_aim() -> None:
    before = _speaker('sp-1', 'FL')
    aimed = before.model_copy(
        update={'aim_xyz': Direction3(x=0.0, y=1.0, z=0.0)}
    )
    diff = diff_scene_documents(_doc(before), _doc(aimed))
    assert diff.entity_changes[0].fields == ('aim_xyz',)


def test_diff_room_flag_only_when_room_changes() -> None:
    before = _doc(_speaker('sp-1', 'FL'))
    after = SceneDocument(
        document_id='doc-1',
        room=RoomPrism(width_m=7.0, depth_m=4.0, height_m=2.4),
        entities=before.entities,
    )
    diff = diff_scene_documents(before, after)
    assert diff.room_changed
    assert not diff.wall_topology_changed
    assert not diff.entity_changes


def test_summary_lines_anchor_names_to_provided_document() -> None:
    before = _doc(_speaker('sp-1', 'FL'))
    after = _doc(_speaker('sp-1', 'FL renamed'), _point('pt-1', 'MLP'))
    diff = diff_scene_documents(before, after)
    lines = diff_summary_lines(diff, after)
    text = '\n'.join(lines)
    assert 'MLPを追加' in text
    assert 'FL renamed: 名称を変更' in text


def test_summary_lines_removed_entity_uses_other_side_names() -> None:
    before = _doc(_speaker('sp-1', 'FL'), _point('pt-1', 'MLP'))
    after = _doc(_speaker('sp-1', 'FL'))
    diff = diff_scene_documents(before, after)
    # Anchoring to the AFTER document: removed entity has no name there, so
    # the raw id is the label fallback.
    lines = diff_summary_lines(diff, after)
    assert any('pt-1を削除' in line for line in lines)


def test_summary_lines_empty_diff_reports_no_change() -> None:
    doc = _doc(_speaker('sp-1', 'FL'))
    lines = diff_summary_lines(diff_scene_documents(doc, doc), doc)
    assert lines == ('変更なし（同一内容）',)


def test_summarize_revision_counts_kinds_sorted() -> None:
    doc = _doc(
        _speaker('sp-1', 'FL'),
        _speaker('sp-2', 'FR', role='FR'),
        _point('pt-1', 'MLP'),
    )
    summary = summarize_revision(doc)
    assert summary.entity_count == 3
    assert summary.kind_counts == (('measurement_point', 1), ('speaker', 2))
    assert summary.has_room
    assert summary.wall_count == 0
    assert summary.content_hash == scene_content_hash(doc)


def test_summarize_revision_no_room_reports_false() -> None:
    doc = _doc(_speaker('sp-1', 'FL'), room=False)
    summary = summarize_revision(doc)
    assert not summary.has_room


def test_summarize_revision_hash_changes_with_content() -> None:
    a = summarize_revision(_doc(_speaker('sp-1', 'FL', x=1.0)))
    b = summarize_revision(_doc(_speaker('sp-1', 'FL', x=2.0)))
    assert a.content_hash != b.content_hash
