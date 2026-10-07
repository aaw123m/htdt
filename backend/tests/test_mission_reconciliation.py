from __future__ import annotations

import pytest

from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.capture_mission import build_mission
from htdt.mission_reconciliation import (
    MissionReconciliationError,
    outstanding_mission_drift_summary,
    record_rebase_decision,
    reconcile_mission_return,
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
        document_id='recon-doc-1',
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=entities,
    )


def _mission(document: SceneDocument):
    return build_mission(
        document,
        project=new_project_reference(document_id='recon-doc-1'),
        purpose='design_verification',
    )


def test_unchanged_document_marks_returned_evidence_applicable() -> None:
    document = _document((_speaker('spk-fl', 'FL', 1.0),))
    mission = _mission(document)
    report = reconcile_mission_return(mission, document)
    assert report.mission_id == mission.mission_id
    assert report.plan_sha256 == mission.plan.plan_sha256
    classes = {result.task_id: result.classification for result in report.results}
    assert set(classes.values()) == {'applicable'}
    assert report.applicable_count == len(report.results)


def test_drifted_target_needs_explicit_reconciliation() -> None:
    issued = _document((_speaker('spk-fl', 'FL', 1.0),))
    current = _document((_speaker('spk-fl', 'FL', 2.5),))
    mission = _mission(issued)
    report = reconcile_mission_return(mission, current)
    classes = {result.task_id: result.classification for result in report.results}
    speaker_task = next(
        task for task in mission.plan.tasks if task.target_entity_id == 'spk-fl'
    )
    assert classes[speaker_task.task_id] == 'needs_reconciliation'
    assert report.review_count == 1


def test_removed_target_is_historical_not_silently_rebound() -> None:
    issued = _document(
        (
            _speaker('spk-fl', 'FL', 1.0),
            _speaker('spk-fr', 'FR', 3.0),
        )
    )
    current = _document((_speaker('spk-fl', 'FL', 1.0),))
    mission = _mission(issued)
    report = reconcile_mission_return(mission, current)
    by_target = {
        result.mission_target_id: result.classification
        for result in report.results
        if result.mission_target_id is not None
    }
    assert by_target['spk-fr'] == 'historical_target_removed'
    # Unchanged speaker evidence stays applicable — name-level binding is
    # never used to rescue the removed entity's evidence.
    verify_tasks = [
        result
        for result in report.results
        if result.mission_target_id == 'spk-fl'
    ]
    assert any(
        result.classification == 'applicable' for result in verify_tasks
    )


def test_returned_task_restriction() -> None:
    document = _document((_speaker('spk-fl', 'FL', 1.0),))
    mission = _mission(document)
    first = mission.plan.tasks[0]
    report = reconcile_mission_return(
        mission, document, returned_task_ids=[first.task_id]
    )
    assert len(report.results) == 1
    assert report.results[0].task_id == first.task_id

    with pytest.raises(MissionReconciliationError):
        reconcile_mission_return(
            mission, document, returned_task_ids=['not-a-task']
        )


def test_rebase_decision_records_explicit_mapping() -> None:
    issued = _document((_speaker('spk-fl', 'FL', 1.0),))
    current = _document((_speaker('spk-fl', 'FL', 2.5),))
    mission = _mission(issued)
    task = next(
        task for task in mission.plan.tasks if task.target_entity_id == 'spk-fl'
    )
    decision = record_rebase_decision(
        mission=mission,
        task=task,
        current_target_id='spk-fl',
        mapping_reason='operator confirmed same physical speaker',
        decided_by='operator-1',
        resulting_authority='system-variant-rev-7',
    )
    assert decision.plan_sha256 == mission.plan.plan_sha256
    assert decision.source_target_id == 'spk-fl'
    assert decision.current_target_id == 'spk-fl'
    assert decision.resulting_authority == 'system-variant-rev-7'
    import uuid as _uuid
    assert _uuid.UUID(decision.decision_id).version == 4


def test_drift_summary_matches_reconciliation() -> None:
    issued = _document((_speaker('spk-fl', 'FL', 1.0),))
    current = _document((_speaker('spk-fl', 'FL', 9.0),))
    mission = _mission(issued)
    summary = outstanding_mission_drift_summary(mission, current)
    report = reconcile_mission_return(mission, current)
    assert summary['has_drift'] is True
    assert summary['needs_reconciliation'] == report.review_count
    assert summary['applicable'] == report.applicable_count


def test_mission_return_reconciliation_lines_e2e(tmp_path) -> None:
    import json
    import sqlite3
    import uuid
    from types import SimpleNamespace

    from htdt.cad_repository import SceneRepository
    from htdt.capture_mission import build_mission_package
    from htdt.capture_receiver import CaptureReceiverService
    from htdt.field_return_ingestion import FieldReturnManifest
    from htdt.mission_reconciliation import (
        mission_return_reconciliation_lines,
    )

    path = tmp_path / 'cad.sqlite3'
    repository = SceneRepository(path)
    document = _document((_speaker('spk-fl', 'FL', 1.0),))
    saved = repository.save(document, parent_revision_id=None)
    project = new_project_reference(document_id='recon-doc-1')
    with sqlite3.connect(path) as connection:
        connection.execute(
            'INSERT INTO htdt_project_documents('
            'project_id, document_id, display_name, created_at_utc'
            ") VALUES (?, ?, 'Theater', 'now')",
            (project.project_id, 'recon-doc-1'),
        )
    service = CaptureReceiverService(
        repository, data_dir=tmp_path / 'rx'
    )
    mission = build_mission(
        document,
        project=new_project_reference(document_id='recon-doc-1'),
        purpose='design_verification',
        room_name='Theater',
    )
    service.queue_mission(build_mission_package(mission))
    # The native package survives alongside the projected wire bytes.
    stored = service.native_mission_package(mission.mission_id)
    assert stored is not None
    assert stored.mission.mission_id == mission.mission_id

    record_id = str(uuid.uuid4())
    task_id = next(
        task.task_id
        for task in mission.plan.tasks
        if task.target_entity_id == 'spk-fl'
    )
    manifest = FieldReturnManifest(
        schema_version=1,
        contribution_id=str(uuid.uuid4()),
        mission_id=mission.mission_id,
        records=(
            {'record_id': record_id, 'kind': 'instrument_result'},
        ),
        task_outcomes=(
            {
                'task_id': task_id,
                'outcome': 'fulfilled',
                'fulfilled_by_ref': record_id,
            },
        ),
    )
    contribution = SimpleNamespace(
        mission_id=mission.mission_id,
        matched_project_id=project.project_id,
        manifest_json=manifest.model_dump_json(),
    )
    lines = mission_return_reconciliation_lines(
        contribution, repository
    )
    assert '適用可能 1件' in lines[0]
    assert len(lines) == 1

    # A contribution for a mission this HTDT never issued surfaces an
    # honest note instead of silence.
    foreign = SimpleNamespace(
        mission_id=str(uuid.uuid4()),
        matched_project_id=project.project_id,
        manifest_json='{}',
    )
    assert 'ミッション原本なし' in mission_return_reconciliation_lines(
        foreign, repository
    )[0]

    # Once the project drifts, the same evidence needs reconciliation.
    drifted = _document((_speaker('spk-fl', 'FL', 2.5),))
    repository.save(
        drifted, parent_revision_id=saved.revision.revision_id
    )
    lines = mission_return_reconciliation_lines(
        contribution, repository
    )
    assert '要調整 1件' in lines[0]
    assert any('… — ' in line for line in lines[1:])

    # A mission-less contribution reconciles to nothing.
    assert (
        mission_return_reconciliation_lines(
            SimpleNamespace(mission_id=None), repository
        )
        == ()
    )

    # The .htdtfieldreturn container the app actually ships resolves
    # through its task_fulfillment_ledger — task_item:<id> refs decode
    # to the mission's task ids.
    container_doc = {
        'schema': 'htdt.field_return',
        'schema_version': '2.0.0',
        'authority_binding_scope': 'contribution_id',
        'contribution_id': str(uuid.uuid4()),
        'mission_id': mission.mission_id,
        'created_at': '2026-01-01T00:00:00+00:00',
        'finalized_at': '2026-01-01T00:00:00+00:00',
        'provenance': {},
        'task_fulfillment_ledger': [
            {
                'item_ref': f'task_item:{task_id}',
                'title': 'verify',
                'requirement': 'required',
                'outcome': 'fulfilled',
                'fulfilled_by_refs': ['field_evidence:abc'],
            },
        ],
        'content_digest': '0' * 64,
    }
    container = SimpleNamespace(
        mission_id=mission.mission_id,
        matched_project_id=project.project_id,
        manifest_json=json.dumps(container_doc),
    )
    lines = mission_return_reconciliation_lines(
        container, repository
    )
    assert '要調整 1件' in lines[0]


def test_rebase_decision_persists_and_marks_drift_decided(
    tmp_path,
) -> None:
    import json
    import sqlite3
    import uuid
    from types import SimpleNamespace

    from htdt.cad_repository import SceneRepository
    from htdt.capture_mission import build_mission_package
    from htdt.capture_receiver import CaptureReceiverService
    from htdt.field_return_ingestion import FieldReturnManifest
    from htdt.mission_reconciliation import (
        MissionReconciliationError,
        MissionReconciliationRepository,
        mission_return_reconciliation_lines,
        record_return_rebase_decision,
    )

    path = tmp_path / 'cad.sqlite3'
    repository = SceneRepository(path)
    document = _document((_speaker('spk-fl', 'FL', 1.0),))
    saved = repository.save(document, parent_revision_id=None)
    project = new_project_reference(document_id='recon-doc-1')
    with sqlite3.connect(path) as connection:
        connection.execute(
            'INSERT INTO htdt_project_documents('
            'project_id, document_id, display_name, created_at_utc'
            ") VALUES (?, ?, 'Theater', 'now')",
            (project.project_id, 'recon-doc-1'),
        )
    service = CaptureReceiverService(
        repository, data_dir=tmp_path / 'rx'
    )
    mission = build_mission(
        document,
        project=new_project_reference(document_id='recon-doc-1'),
        purpose='design_verification',
        room_name='Theater',
    )
    service.queue_mission(build_mission_package(mission))
    task_id = next(
        task.task_id
        for task in mission.plan.tasks
        if task.target_entity_id == 'spk-fl'
    )
    record_id = str(uuid.uuid4())
    manifest = FieldReturnManifest(
        schema_version=1,
        contribution_id=str(uuid.uuid4()),
        mission_id=mission.mission_id,
        records=(
            {'record_id': record_id, 'kind': 'instrument_result'},
        ),
        task_outcomes=(
            {
                'task_id': task_id,
                'outcome': 'fulfilled',
                'fulfilled_by_ref': record_id,
            },
        ),
    )
    contribution = SimpleNamespace(
        mission_id=mission.mission_id,
        matched_project_id=project.project_id,
        manifest_json=manifest.model_dump_json(),
    )
    # Drift the pinned target before the return lands.
    drifted = repository.save(
        _document((_speaker('spk-fl', 'FL', 2.5),)),
        parent_revision_id=saved.revision.revision_id,
    )
    assert '要調整 1件' in mission_return_reconciliation_lines(
        contribution, repository
    )[0]

    # The operator decides the drifted evidence maps onto the renamed
    # current target — recorded once, surfaced on every later view.
    repository.save(
        _document((_speaker('spk-fl2', 'FL', 2.5),)),
        parent_revision_id=drifted.revision.revision_id,
    )
    decision = record_return_rebase_decision(
        contribution,
        repository,
        task_id=task_id,
        current_target_id='spk-fl2',
        mapping_reason='front-left moved to the new position',
        decided_by='operator-1',
    )
    assert decision.source_target_id == 'spk-fl'
    assert decision.current_target_id == 'spk-fl2'
    stored = MissionReconciliationRepository(
        repository.path
    ).decisions_for_mission(mission.mission_id)
    assert len(stored) == 1
    assert stored[0].decision_id == decision.decision_id

    lines = mission_return_reconciliation_lines(
        contribution, repository
    )
    assert '決定記録済み 1件' in lines[0]
    assert 'spk-fl→spk-fl2' in lines[1]

    # A re-decision replaces the earlier mapping, not appends.
    redecision = record_return_rebase_decision(
        contribution,
        repository,
        task_id=task_id,
        current_target_id='spk-fl2',
        mapping_reason='mapping corrected',
        decided_by='operator-2',
    )
    stored = MissionReconciliationRepository(
        repository.path
    ).decisions_for_mission(mission.mission_id)
    assert len(stored) == 1
    assert stored[0].current_target_id == 'spk-fl2'
    assert stored[0].decided_by == 'operator-2'
    assert stored[0].decision_id != decision.decision_id

    # A target that no longer exists is refused — evidence never binds
    # to a dead entity.
    with pytest.raises(MissionReconciliationError):
        record_return_rebase_decision(
            contribution,
            repository,
            task_id=task_id,
            current_target_id='spk-fl',
            mapping_reason='bogus',
            decided_by='operator-1',
        )

    # A task outside the issuing mission is refused.
    with pytest.raises(MissionReconciliationError):
        record_return_rebase_decision(
            contribution,
            repository,
            task_id='foreign-task',
            current_target_id='spk-fl2',
            mapping_reason='bogus',
            decided_by='operator-1',
        )


def test_apply_returned_tasks_binds_evidence_to_resolved_targets(
    tmp_path,
) -> None:
    import sqlite3
    import uuid
    from types import SimpleNamespace

    from htdt.cad_repository import SceneRepository
    from htdt.capture_mission import build_mission_package
    from htdt.capture_receiver import CaptureReceiverService
    from htdt.field_return_ingestion import FieldReturnManifest
    from htdt.mission_reconciliation import (
        MissionReconciliationError,
        MissionReconciliationRepository,
        apply_returned_tasks,
        mission_return_reconciliation_lines,
        record_return_rebase_decision,
        returned_task_applications,
    )

    path = tmp_path / 'cad.sqlite3'
    repository = SceneRepository(path)
    document = _document((_speaker('spk-fl', 'FL', 1.0),))
    saved = repository.save(document, parent_revision_id=None)
    project = new_project_reference(document_id='recon-doc-1')
    with sqlite3.connect(path) as connection:
        connection.execute(
            'INSERT INTO htdt_project_documents('
            'project_id, document_id, display_name, created_at_utc'
            ") VALUES (?, ?, 'Theater', 'now')",
            (project.project_id, 'recon-doc-1'),
        )
    service = CaptureReceiverService(
        repository, data_dir=tmp_path / 'rx'
    )
    mission = build_mission(
        document,
        project=new_project_reference(document_id='recon-doc-1'),
        purpose='design_verification',
        room_name='Theater',
    )
    service.queue_mission(build_mission_package(mission))
    task_id = next(
        task.task_id
        for task in mission.plan.tasks
        if task.target_entity_id == 'spk-fl'
    )
    record_id = str(uuid.uuid4())
    manifest = FieldReturnManifest(
        schema_version=1,
        contribution_id=str(uuid.uuid4()),
        mission_id=mission.mission_id,
        records=(
            {'record_id': record_id, 'kind': 'instrument_result'},
        ),
        task_outcomes=(
            {
                'task_id': task_id,
                'outcome': 'fulfilled',
                'fulfilled_by_ref': record_id,
            },
        ),
    )
    contribution = SimpleNamespace(
        contribution_id=str(uuid.uuid4()),
        mission_id=mission.mission_id,
        matched_project_id=project.project_id,
        manifest_json=manifest.model_dump_json(),
    )

    # Undrifted: the applicable task binds to its pinned target.
    outcome = apply_returned_tasks(
        contribution, repository, applied_by='operator-1'
    )
    assert len(outcome.applied) == 1
    application = outcome.applied[0]
    assert application.applied_target_id == 'spk-fl'
    assert application.source_target_id == 'spk-fl'
    assert application.decision_id is None
    assert application.record_refs == (record_id,)
    assert outcome.skipped_task_ids == ()
    stored = returned_task_applications(contribution, repository)
    assert len(stored) == 1
    assert stored[0].application_id == application.application_id
    lines = mission_return_reconciliation_lines(
        contribution, repository
    )
    assert '適用済み 1件' in lines[0]
    assert '適用: spk-fl' in lines[1]

    # Re-applying is a no-op — the binding stays exactly once.
    second = apply_returned_tasks(
        contribution, repository, applied_by='operator-1'
    )
    assert second.applied == ()
    assert len(returned_task_applications(contribution, repository)) == 1


def test_apply_consumes_rebase_decision_and_skips_undecided(
    tmp_path,
) -> None:
    import sqlite3
    import uuid
    from types import SimpleNamespace

    from htdt.cad_repository import SceneRepository
    from htdt.capture_mission import build_mission_package
    from htdt.capture_receiver import CaptureReceiverService
    from htdt.field_return_ingestion import FieldReturnManifest
    from htdt.mission_reconciliation import (
        MissionReconciliationError,
        MissionReconciliationRepository,
        apply_returned_tasks,
        record_return_rebase_decision,
    )

    path = tmp_path / 'cad.sqlite3'
    repository = SceneRepository(path)
    document = _document(
        (_speaker('spk-fl', 'FL', 1.0), _speaker('spk-fr', 'FR', 4.0))
    )
    saved = repository.save(document, parent_revision_id=None)
    project = new_project_reference(document_id='recon-doc-1')
    with sqlite3.connect(path) as connection:
        connection.execute(
            'INSERT INTO htdt_project_documents('
            'project_id, document_id, display_name, created_at_utc'
            ") VALUES (?, ?, 'Theater', 'now')",
            (project.project_id, 'recon-doc-1'),
        )
    service = CaptureReceiverService(
        repository, data_dir=tmp_path / 'rx'
    )
    mission = build_mission(
        document,
        project=new_project_reference(document_id='recon-doc-1'),
        purpose='design_verification',
        room_name='Theater',
    )
    service.queue_mission(build_mission_package(mission))
    task_fl = next(
        task.task_id
        for task in mission.plan.tasks
        if task.target_entity_id == 'spk-fl'
    )
    task_fr = next(
        task.task_id
        for task in mission.plan.tasks
        if task.target_entity_id == 'spk-fr'
    )
    record_id = str(uuid.uuid4())
    manifest = FieldReturnManifest(
        schema_version=1,
        contribution_id=str(uuid.uuid4()),
        mission_id=mission.mission_id,
        records=(
            {'record_id': record_id, 'kind': 'instrument_result'},
        ),
        task_outcomes=(
            {
                'task_id': task_fl,
                'outcome': 'fulfilled',
                'fulfilled_by_ref': record_id,
            },
            {
                'task_id': task_fr,
                'outcome': 'fulfilled',
                'fulfilled_by_ref': record_id,
            },
        ),
    )
    contribution = SimpleNamespace(
        contribution_id=str(uuid.uuid4()),
        mission_id=mission.mission_id,
        matched_project_id=project.project_id,
        manifest_json=manifest.model_dump_json(),
    )
    # Both targets drift; only FL gets a decision.
    drifted = repository.save(
        _document(
            (_speaker('spk-fl', 'FL', 2.5), _speaker('spk-fr', 'FR', 5.0))
        ),
        parent_revision_id=saved.revision.revision_id,
    )
    repository.save(
        _document(
            (_speaker('spk-fl2', 'FL', 2.5), _speaker('spk-fr', 'FR', 5.0))
        ),
        parent_revision_id=drifted.revision.revision_id,
    )
    decision = record_return_rebase_decision(
        contribution,
        repository,
        task_id=task_fl,
        current_target_id='spk-fl2',
        mapping_reason='renamed during the move',
        decided_by='operator-1',
    )


    # A current target that does not exist is refused — evidence must
    # never bind to a dead entity.
    with pytest.raises(MissionReconciliationError):
        record_return_rebase_decision(
            contribution,
            repository,
            task_id=task_fl,
            current_target_id='spk-ghost',
            mapping_reason='bogus',
            decided_by='operator-1',
        )
    outcome = apply_returned_tasks(
        contribution, repository, applied_by='operator-2'
    )
    assert {a.task_id for a in outcome.applied} == {task_fl}
    applied = outcome.applied[0]
    assert applied.applied_target_id == 'spk-fl2'
    assert applied.source_target_id == 'spk-fl'
    assert applied.decision_id == decision.decision_id
    # FR has no decision — skipped, never rebound onto a guess.
    assert outcome.skipped_task_ids == (task_fr,)
    # The decision's resulting_authority now names the application.
    stored = MissionReconciliationRepository(
        repository.path
    ).decisions_for_mission(mission.mission_id)
    assert stored[0].resulting_authority == applied.application_id

    # Scoping the apply to an unresolvable task fails closed.
    with pytest.raises(MissionReconciliationError):
        apply_returned_tasks(
            contribution,
            repository,
            applied_by='operator-2',
            task_ids=[task_fr],
        )


def test_mission_completes_when_every_plan_task_is_applied(
    tmp_path,
) -> None:
    import sqlite3
    import uuid
    from types import SimpleNamespace

    from htdt.cad_repository import SceneRepository
    from htdt.capture_mission import build_mission_package
    from htdt.capture_receiver import CaptureReceiverService
    from htdt.field_return_ingestion import FieldReturnManifest
    from htdt.mission_reconciliation import (
        apply_returned_tasks,
        mission_return_reconciliation_lines,
    )

    path = tmp_path / 'cad.sqlite3'
    repository = SceneRepository(path)
    document = _document((_speaker('spk-fl', 'FL', 1.0),))
    repository.save(document, parent_revision_id=None)
    project = new_project_reference(document_id='recon-doc-1')
    with sqlite3.connect(path) as connection:
        connection.execute(
            'INSERT INTO htdt_project_documents('
            'project_id, document_id, display_name, created_at_utc'
            ") VALUES (?, ?, 'Theater', 'now')",
            (project.project_id, 'recon-doc-1'),
        )
    service = CaptureReceiverService(
        repository, data_dir=tmp_path / 'rx'
    )
    mission = build_mission(
        document,
        project=new_project_reference(document_id='recon-doc-1'),
        purpose='design_verification',
        room_name='Theater',
    )
    service.queue_mission(build_mission_package(mission))
    task_ids = [task.task_id for task in mission.plan.tasks]
    manifest = FieldReturnManifest(
        schema_version=1,
        contribution_id=str(uuid.uuid4()),
        mission_id=mission.mission_id,
        records=(
            {'record_id': str(uuid.uuid4()), 'kind': 'instrument_result'},
        ),
        task_outcomes=tuple(
            {
                'task_id': task_id,
                'outcome': 'fulfilled',
                'fulfilled_by_ref': str(uuid.uuid4()),
            }
            for task_id in task_ids
        ),
    )
    contribution = SimpleNamespace(
        contribution_id=str(uuid.uuid4()),
        mission_id=mission.mission_id,
        matched_project_id=project.project_id,
        manifest_json=manifest.model_dump_json(),
    )

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT status FROM capture_mission_packages "
            'WHERE package_id=?',
            (mission.mission_id,),
        ).fetchone()[0] == 'pending'

    outcome = apply_returned_tasks(
        contribution, repository, applied_by='operator-1'
    )
    assert len(outcome.applied) == len(task_ids)
    # Entity-less tasks bind to the project document itself.
    by_task = {a.task_id: a for a in outcome.applied}
    for task in mission.plan.tasks:
        expected = task.target_entity_id or 'recon-doc-1'
        assert by_task[task.task_id].applied_target_id == expected

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT status FROM capture_mission_packages "
            'WHERE package_id=?',
            (mission.mission_id,),
        ).fetchone()[0] == 'completed'
    lines = mission_return_reconciliation_lines(
        contribution, repository
    )
    assert lines[0].startswith('ミッション完了')


def test_apply_flags_refs_the_container_cannot_resolve(
    tmp_path,
) -> None:
    """Applied refs the retained artifact can't resolve are reported,
    never silenced; retained-but-absent bytes stay honest too."""
    import io
    import json
    import sqlite3
    import uuid
    import zipfile
    import zlib
    from hashlib import sha256
    from types import SimpleNamespace

    from htdt.cad_repository import SceneRepository
    from htdt.capture_mission import build_mission_package
    from htdt.capture_receiver import CaptureReceiverService
    from htdt.field_return_ingestion import FieldReturnRepository
    from htdt.mission_reconciliation import apply_returned_tasks

    path = tmp_path / 'cad.sqlite3'
    repository = SceneRepository(path)
    document = _document((_speaker('spk-fl', 'FL', 1.0),))
    repository.save(document, parent_revision_id=None)
    project = new_project_reference(document_id='recon-doc-1')
    with sqlite3.connect(path) as connection:
        connection.execute(
            'INSERT INTO htdt_project_documents('
            'project_id, document_id, display_name, created_at_utc'
            ") VALUES (?, ?, 'Theater', 'now')",
            (project.project_id, 'recon-doc-1'),
        )
    service = CaptureReceiverService(
        repository, data_dir=tmp_path / 'rx'
    )
    mission = build_mission(
        document,
        project=new_project_reference(document_id='recon-doc-1'),
        purpose='design_verification',
        room_name='Theater',
    )
    service.queue_mission(build_mission_package(mission))
    task_id = next(
        task.task_id
        for task in mission.plan.tasks
        if task.target_entity_id == 'spk-fl'
    )

    evidence_id = str(uuid.uuid4())
    missing_item = str(uuid.uuid4())
    external_entity = str(uuid.uuid4())
    asset_payload = b'room-photo'
    evidence_payload = json.dumps(
        {
            'schema': 'htdt.field_return.field-evidence',
            'contribution_ref': {
                'kind': 'field_return',
                'id': 'placeholder',
            },
            'records': [
                {'evidence_id': evidence_id, 'channel_role': 'FL'}
            ],
        }
    ).encode('utf-8')
    contribution_id = str(uuid.uuid4())
    root = {
        'schema': 'htdt.field_return',
        'schema_version': '2.0.0',
        'authority_binding_scope': 'contribution_ref',
        'contribution_id': contribution_id,
        'contribution_ref': {
            'kind': 'field_return',
            'id': contribution_id,
        },
        'mission_id': mission.mission_id,
        'created_at': '2026-10-07T00:00:00Z',
        'finalized_at': '2026-10-07T00:01:00Z',
        'provenance': {},
        'task_fulfillment_ledger': [
            {
                'item_ref': f'task_item:{task_id}',
                'title': 'verify speaker placement',
                'requirement': 'required',
                'outcome': 'fulfilled',
                'fulfilled_by_refs': [
                    f'field_evidence:{evidence_id}',
                    f'inventory_item:{missing_item}',
                    f'sha256:{sha256(asset_payload).hexdigest()}',
                    f'entity:{external_entity}',
                ],
            }
        ],
        'authority_documents': [
            {
                'path': 'authority/field-evidence.json',
                'schema': 'htdt.field_return.field-evidence',
                'sha256': sha256(evidence_payload).hexdigest(),
                'bytes': len(evidence_payload),
            }
        ],
        'evidence_assets': [
            {
                'path': 'evidence/room-photo.bin',
                'schema': 'application/octet-stream',
                'sha256': sha256(asset_payload).hexdigest(),
                'bytes': len(asset_payload),
            }
        ],
        'content_digest': '0' * 64,
    }
    entries = {
        'field-return.json': json.dumps(root).encode('utf-8'),
        'authority/field-evidence.json': evidence_payload,
        'evidence/room-photo.bin': asset_payload,
    }
    manifest = {
        'entries': [
            {
                'path': name,
                'bytes': len(data),
                'crc32': zlib.crc32(data) & 0xFFFFFFFF,
                'sha256': sha256(data).hexdigest(),
            }
            for name, data in entries.items()
        ]
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        archive.writestr(
            'container-manifest.json',
            json.dumps(manifest).encode('utf-8'),
        )
    field_returns = FieldReturnRepository(path)
    field_returns.stage_artifact(
        buffer.getvalue(),
        (project,),
        channel_project_ref=project.project_id,
    )
    contribution = field_returns.get(contribution_id)
    assert contribution.matched_project_id == project.project_id

    outcome = apply_returned_tasks(
        contribution, repository, applied_by='ops'
    )
    # One container-bound ref names a record the container does not
    # carry — reported, not silenced; resolvable/external refs are not.
    assert outcome.unresolved_refs == (
        f'inventory_item:{missing_item}',
    )
    assert outcome.refs_unverifiable is False

    # A second contribution whose retained bytes are absent applies its
    # refs honestly as unverifiable. A mission task binds once across
    # contributions, so this one carries a different mission task.
    task_id_2 = next(
        task.task_id
        for task in mission.plan.tasks
        if task.task_id != task_id
    )
    contribution_id_2 = str(uuid.uuid4())
    root2 = dict(root)
    root2.update(
        {
            'contribution_id': contribution_id_2,
            'contribution_ref': {
                'kind': 'field_return',
                'id': contribution_id_2,
            },
            # entries2 drops room-photo.bin — the declaration must go
            # too or the container fails integrity verification.
            'evidence_assets': [],
            'task_fulfillment_ledger': [
                {
                    'item_ref': f'task_item:{task_id_2}',
                    'title': 'verify',
                    'requirement': 'required',
                    'outcome': 'fulfilled',
                    'fulfilled_by_refs': [
                        f'field_evidence:{evidence_id}'
                    ],
                }
            ],
        }
    )
    entries2 = {
        'field-return.json': json.dumps(root2).encode('utf-8'),
        'authority/field-evidence.json': evidence_payload,
    }
    manifest2 = {
        'entries': [
            {
                'path': name,
                'bytes': len(data),
                'crc32': zlib.crc32(data) & 0xFFFFFFFF,
                'sha256': sha256(data).hexdigest(),
            }
            for name, data in entries2.items()
        ]
    }
    buffer2 = io.BytesIO()
    with zipfile.ZipFile(buffer2, 'w', zipfile.ZIP_STORED) as archive:
        for name, data in entries2.items():
            archive.writestr(name, data)
        archive.writestr(
            'container-manifest.json',
            json.dumps(manifest2).encode('utf-8'),
        )
    field_returns.stage_artifact(
        buffer2.getvalue(),
        (project,),
        channel_project_ref=project.project_id,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            'DELETE FROM htdt_content_blobs WHERE payload_sha256=('
            'SELECT artifact_sha256 FROM field_return_contributions '
            'WHERE contribution_id=?)',
            (contribution_id_2,),
        )
    contribution2 = field_returns.get(contribution_id_2)
    outcome2 = apply_returned_tasks(
        contribution2, repository, applied_by='ops'
    )
    assert outcome2.unresolved_refs == ()
    assert outcome2.refs_unverifiable is True
