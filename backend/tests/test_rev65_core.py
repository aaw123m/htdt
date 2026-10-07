"""REV65-CORE regression tests — mission-return apply boundaries."""
from __future__ import annotations

import sqlite3
import uuid
from types import SimpleNamespace

import pytest

from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.capture_mission import build_mission, build_mission_package
from htdt.cad_repository import SceneRepository
from htdt.capture_receiver import CaptureReceiverService
from htdt.field_return_ingestion import FieldReturnManifest
from htdt.mission_reconciliation import (
    MissionReconciliationError,
    apply_returned_tasks,
    returned_task_applications,
)
from htdt.project_identity import new_project_reference


def _speaker(entity_id: str, role: str, x: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=entity_id.upper(),
        speaker_role=role,
        position=Position3(x_m=x, y_m=1.0, z_m=1.0),
        size_m=Size3(x_m=0.2, y_m=0.3, z_m=0.4),
    )


def _document(entities: tuple[SceneEntity, ...]) -> SceneDocument:
    return SceneDocument(
        document_id='rev65-doc-1',
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=entities,
    )


def _setup(tmp_path):
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = _document((_speaker('spk-fl', 'FL', 1.0),))
    repository.save(document, parent_revision_id=None)
    project = new_project_reference(document_id='rev65-doc-1')
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            'INSERT INTO htdt_project_documents('
            'project_id, document_id, display_name, created_at_utc'
            ") VALUES (?, ?, 'Theater', 'now')",
            (project.project_id, 'rev65-doc-1'),
        )
    service = CaptureReceiverService(repository, data_dir=tmp_path / 'rx')
    mission = build_mission(
        document,
        project=new_project_reference(document_id='rev65-doc-1'),
        purpose='design_verification',
        room_name='Theater',
    )
    service.queue_mission(build_mission_package(mission))
    task_id = next(
        task.task_id
        for task in mission.plan.tasks
        if task.target_entity_id == 'spk-fl'
    )
    return repository, project, mission, task_id


def _contribution(mission, project, task_id, record_id):
    manifest = FieldReturnManifest(
        schema_version=1,
        contribution_id=str(uuid.uuid4()),
        mission_id=mission.mission_id,
        records=({'record_id': record_id, 'kind': 'instrument_result'},),
        task_outcomes=(
            {
                'task_id': task_id,
                'outcome': 'fulfilled',
                'fulfilled_by_ref': record_id,
            },
        ),
    )
    return SimpleNamespace(
        contribution_id=str(uuid.uuid4()),
        mission_id=mission.mission_id,
        matched_project_id=project.project_id,
        manifest_json=manifest.model_dump_json(),
    )


def test_mission_task_binds_once_across_contributions(tmp_path):
    """Two contributions carrying the same mission task must not mint two
    applications — the task binds under the first one only."""
    repository, project, mission, task_id = _setup(tmp_path)
    contribution_a = _contribution(mission, project, task_id, str(uuid.uuid4()))
    contribution_b = _contribution(mission, project, task_id, str(uuid.uuid4()))

    first = apply_returned_tasks(
        contribution_a, repository, applied_by='operator-1'
    )
    assert len(first.applied) == 1

    # Bulk apply of the sibling contribution: the already-bound task is
    # silently skipped — no second application row may appear.
    second = apply_returned_tasks(
        contribution_b, repository, applied_by='operator-1'
    )
    assert second.applied == ()

    with sqlite3.connect(repository.path) as connection:
        rows = connection.execute(
            'SELECT contribution_id, applied_target_id '
            'FROM field_return_applications WHERE mission_id=?',
            (mission.mission_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == contribution_a.contribution_id
    assert rows[0][1] == 'spk-fl'


def test_explicit_apply_of_cross_contribution_task_fails_closed(tmp_path):
    """Naming a task already bound under a sibling contribution is an
    error, not a silent no-op or a duplicate row."""
    repository, project, mission, task_id = _setup(tmp_path)
    contribution_a = _contribution(mission, project, task_id, str(uuid.uuid4()))
    contribution_b = _contribution(mission, project, task_id, str(uuid.uuid4()))

    apply_returned_tasks(contribution_a, repository, applied_by='operator-1')

    with pytest.raises(MissionReconciliationError, match='already applied'):
        apply_returned_tasks(
            contribution_b,
            repository,
            applied_by='operator-1',
            task_ids=(task_id,),
        )

    with sqlite3.connect(repository.path) as connection:
        count = connection.execute(
            'SELECT COUNT(*) FROM field_return_applications WHERE mission_id=?',
            (mission.mission_id,),
        ).fetchone()[0]
    assert count == 1
