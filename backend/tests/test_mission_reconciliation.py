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
