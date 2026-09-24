from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_project_activity import (
    ProjectActivityNote,
    build_activity_note,
)
from htdt.cad_project_activity_repository import (
    CadProjectActivityNoteRepository,
)
from htdt.cad_project_activity import CadProjectActivityService
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)

NOW = '2026-09-23T00:00:00+00:00'


def _scene(document_id: str = 'doc-1', fl_x: float = 1.2) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=fl_x, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
        ),
    )


def _service(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    notes = CadProjectActivityNoteRepository(scene_repository)
    service = CadProjectActivityService(
        scene_repository=scene_repository,
        notes_repository=notes,
    )
    return scene_repository, notes, service


def test_revision_history_projects_scene_and_creation_events(tmp_path: Path) -> None:
    scenes, _notes, service = _service(tmp_path)
    first = scenes.save(_scene(), parent_revision_id=None).revision
    second = scenes.save(
        _scene(fl_x=1.6), parent_revision_id=first.revision_id
    ).revision
    events = service.events('doc-1')
    kinds = [event.kind for event in events]
    assert kinds == ['project_created', 'scene_revision_saved']
    assert events[0].source_refs[0].ref_id == first.revision_id
    assert events[1].source_refs[0].ref_id == second.revision_id
    assert events[1].deep_link is not None
    assert 'workspace/room' in events[1].deep_link


def test_labels_project_as_distinct_events(tmp_path: Path) -> None:
    scenes, _notes, service = _service(tmp_path)
    revision = scenes.save(_scene(), parent_revision_id=None).revision
    scenes.set_revision_label(revision.revision_id, label='測定前')
    kinds = [event.kind for event in service.events('doc-1')]
    assert kinds == ['project_created', 'scene_revision_labeled']


def test_notes_are_documentation_never_authority(tmp_path: Path) -> None:
    scenes, notes, service = _service(tmp_path)
    scenes.save(_scene(), parent_revision_id=None)
    note = service.add_note(
        document_id='doc-1',
        title='搬入完了',
        body='機材設置を完了。セレクションはまだ。',
        created_at_utc='2026-09-23T03:00:00+00:00',
    )
    assert notes.get_note(note.note_id) == note
    kinds = [event.kind for event in service.events('doc-1')]
    assert 'project_note' in kinds
    note_event = [e for e in service.events('doc-1') if e.kind == 'project_note'][0]
    assert note_event.title == 'メモ「搬入完了」'


def test_projection_is_rebuildable_and_deterministic(tmp_path: Path) -> None:
    scenes, notes, service = _service(tmp_path)
    scenes.save(_scene(), parent_revision_id=None)
    service.add_note(
        document_id='doc-1', title='メモ', created_at_utc=NOW
    )
    first_pass = service.events('doc-1')
    # a fresh service on the same stores must yield identical event ids
    fresh = CadProjectActivityService(
        scene_repository=SceneRepository(tmp_path / 'cad.sqlite3'),
        notes_repository=CadProjectActivityNoteRepository(scenes),
    )
    second_pass = fresh.events('doc-1')
    assert [e.event_id for e in first_pass] == [e.event_id for e in second_pass]
    assert len({e.event_id for e in first_pass}) == len(first_pass)


def test_recent_returns_newest_first_with_limit(tmp_path: Path) -> None:
    scenes, _notes, service = _service(tmp_path)
    first = scenes.save(_scene(), parent_revision_id=None).revision
    scenes.save(_scene(fl_x=1.6), parent_revision_id=first.revision_id)
    service.add_note(
        document_id='doc-1',
        title='メモ',
        created_at_utc='2026-09-23T09:00:00+00:00',
    )
    recent = service.recent('doc-1', limit=2)
    assert len(recent) == 2
    assert recent[0].occurred_at_utc >= recent[1].occurred_at_utc
    assert {event.kind for event in service.events('doc-1')} >= {
        'project_created',
        'scene_revision_saved',
        'project_note',
    }


def test_note_rejects_unknown_document(tmp_path: Path) -> None:
    scenes = SceneRepository(tmp_path / 'cad.sqlite3')
    notes = CadProjectActivityNoteRepository(scenes)
    note = build_activity_note(
        document_id='ghost-doc', title='x', created_at_utc=NOW
    )
    with pytest.raises(ValueError, match='unknown document'):
        notes.save_note(note)


def test_note_hash_integrity() -> None:
    note = build_activity_note(
        document_id='doc-1', title='メモ', created_at_utc=NOW
    )
    payload = note.model_dump(mode='python')
    payload['title'] = 'tampered'
    with pytest.raises(ValidationError, match='hash mismatch'):
        ProjectActivityNote(**payload)


def test_detached_revisions_are_flagged_not_dropped(tmp_path: Path) -> None:
    scenes, _notes, service = _service(tmp_path)
    first = scenes.save(_scene(), parent_revision_id=None).revision
    scenes.save_detached_revision(
        _scene(fl_x=2.0),
        parent_revision_id=first.revision_id,
        reason='imported variant',
    )
    events = service.events('doc-1')
    detached = [e for e in events if e.detail == '非ヘッド履歴']
    assert len(detached) == 1
    assert detached[0].kind == 'scene_revision_saved'
