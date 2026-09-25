"""#863: a correction that relabels a measurement onto another target never
silently rewrites physical-position truth.

- Same acoustic position → ``assignment_label_only`` correction is allowed.
- Different position without pinned pose evidence → reject (never an
  implicit tolerance).
- Different position with an exact resolved pose observation → allowed and
  the observed position surfaces on the view, distinct from the immutable
  import position.
- Reads revalidate the corrected binding; corrupt rows and dispositions
  pinned to unverifiable corrections fail closed.
"""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_measurement_disposition import (
    CadMeasurementCorrection,
    build_measurement_correction,
    build_measurement_disposition,
)
from htdt.cad_measurement_quality import _hash
from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    SceneEntity,
    make_f1_scene,
)
from htdt.measurement_workflow import (
    AssignmentCorrection,
    MeasurementAssignment,
    MeasurementWorkflowController,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef

MLP = Position3(x_m=3.0, y_m=3.0, z_m=1.1)
AWAY = Position3(x_m=1.0, y_m=1.0, z_m=1.1)


def _scene_with_targets(document):
    return document.model_copy(
        update={
            'entities': (
                *document.entities,
                SceneEntity(
                    entity_id='point-twin',
                    kind='measurement_point',
                    name='Twin point',
                    position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
                ),
                SceneEntity(
                    entity_id='point-away',
                    kind='measurement_point',
                    name='Away point',
                    position=AWAY,
                ),
            )
        }
    )


def _controller(
    tmp_path: Path,
    pose_evidence_resolver=None,
):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        _scene_with_targets(make_f1_scene()), parent_revision_id=None
    ).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(
        measurement_repository,
        pose_evidence_resolver=pose_evidence_resolver,
    )
    controller = MeasurementWorkflowController(
        scene_repository,
        revision.document_id,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    controller.stage_rew_text(b'20 70\n40 71\n', 'm.txt')
    record = controller.commit_pending(
        MeasurementAssignment(
            measurement_entity_id='point-mlp',
            evidence_type='measured',
            channel_role='front_left',
            source_speaker_ids=('speaker-fl',),
            radiation_scope='single',
        )
    )
    return controller, quality_repository, record


def _pose_ref(authority_id: str = 'pose-1') -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=authority_id,
        authority_version='pose-observation-1',
        semantic_hash_sha256=sha256(b'pose-authority').hexdigest(),
    )


def test_label_only_correction_same_position_allowed(tmp_path: Path) -> None:
    controller, quality_repository, record = _controller(tmp_path)
    correction = controller.correct_assignment(
        record.measurement_id,
        AssignmentCorrection(
            measurement_entity_id='point-twin',
            correction_kind='assignment_label_only',
        ),
        reason='wrong point selected at import',
    )
    assert quality_repository.get_correction(correction.correction_id) == correction
    view = next(
        v for v in controller.measurement_views()
        if v.measurement_id == record.measurement_id
    )
    assert view.original_import_position == MLP
    assert view.observed_actual_position is None
    assert view.assignment_position_compatibility == 'exact'
    assert view.effective_target_entity_id == 'point-twin'


def test_different_position_without_pose_evidence_rejects(
    tmp_path: Path,
) -> None:
    controller, _, record = _controller(tmp_path)
    with pytest.raises(ValueError, match='pose evidence'):
        controller.correct_assignment(
            record.measurement_id,
            AssignmentCorrection(measurement_entity_id='point-away'),
            reason='wrong seat',
        )
    # Declared label-only kind must not sneak a spatial relabel through.
    with pytest.raises(ValueError, match='label-only|pose evidence'):
        controller.correct_assignment(
            record.measurement_id,
            AssignmentCorrection(
                measurement_entity_id='point-away',
                correction_kind='assignment_label_only',
            ),
            reason='wrong seat',
        )
    assert controller.corrections_for(record.measurement_id) == ()


def test_different_position_with_pose_evidence_allowed(
    tmp_path: Path,
) -> None:
    ref = _pose_ref()
    resolver = lambda evidence, document_id: AWAY  # noqa: E731
    controller, quality_repository, record = _controller(
        tmp_path, pose_evidence_resolver=resolver
    )
    correction = controller.correct_assignment(
        record.measurement_id,
        AssignmentCorrection(
            measurement_entity_id='point-away',
            correction_kind='assignment_with_pose_evidence',
            pose_evidence_ref=ref,
        ),
        reason='capsule was actually at point-away',
    )
    assert correction.pose_evidence_ref == ref
    assert quality_repository.get_correction(correction.correction_id) == correction
    view = next(
        v for v in controller.measurement_views()
        if v.measurement_id == record.measurement_id
    )
    assert view.original_import_position == MLP  # immutable history
    assert view.observed_actual_position == AWAY
    assert view.assignment_position_compatibility == 'pose_observed'


def test_pose_evidence_must_resolve_and_match_target(tmp_path: Path) -> None:
    ref = _pose_ref()
    # Resolver cannot prove the authority at all.
    controller, _, record = _controller(
        tmp_path, pose_evidence_resolver=lambda ref, doc: None
    )
    with pytest.raises(ValueError, match='does not resolve'):
        controller.correct_assignment(
            record.measurement_id,
            AssignmentCorrection(
                measurement_entity_id='point-away',
                pose_evidence_ref=ref,
            ),
            reason='wrong seat',
        )
    # Resolver proves a *different* position than the corrected target's
    # reference — the evidence does not support the reassignment.
    controller, _, record2 = _controller(
        tmp_path / 'mismatch',
        pose_evidence_resolver=lambda ref, doc: Position3(
            x_m=9.0, y_m=9.0, z_m=9.0
        ),
    )
    with pytest.raises(ValueError, match='does not match'):
        controller.correct_assignment(
            record2.measurement_id,
            AssignmentCorrection(
                measurement_entity_id='point-away',
                pose_evidence_ref=ref,
            ),
            reason='wrong seat',
        )


def test_unresolvable_resolver_fails_closed(tmp_path: Path) -> None:
    # No resolver configured: a spatially different correction can never
    # persist, even with a self-consistent pose ref pinned.
    controller, _, record = _controller(tmp_path)
    with pytest.raises(ValueError, match='resolver|pose evidence'):
        controller.correct_assignment(
            record.measurement_id,
            AssignmentCorrection(
                measurement_entity_id='point-away',
                pose_evidence_ref=_pose_ref(),
            ),
            reason='wrong seat',
        )


def test_moved_target_keeps_historical_correction(tmp_path: Path) -> None:
    ref = _pose_ref()
    controller, quality_repository, record = _controller(
        tmp_path, pose_evidence_resolver=lambda ref, doc: AWAY
    )
    correction = controller.correct_assignment(
        record.measurement_id,
        AssignmentCorrection(
            measurement_entity_id='point-away',
            pose_evidence_ref=ref,
        ),
        reason='capsule was actually at point-away',
    )
    # Move the corrected target later — the historical correction stays bound
    # to the pinned source revision, not current head.
    scene_repository = controller.scene_repository
    head = scene_repository.current_head(record.document_id)
    moved = head.document.model_copy(
        update={
            'entities': tuple(
                entity.model_copy(
                    update={
                        'position': Position3(x_m=8.0, y_m=8.0, z_m=8.0)
                    }
                )
                if entity.entity_id == 'point-away'
                else entity
                for entity in head.document.entities
            )
        }
    )
    scene_repository.save(moved, parent_revision_id=head.revision_id)
    assert quality_repository.get_correction(correction.correction_id) == correction


def test_channel_routing_correction_unaffected_by_spatial_gate(
    tmp_path: Path,
) -> None:
    controller, quality_repository, record = _controller(tmp_path)
    correction = controller.correct_assignment(
        record.measurement_id,
        AssignmentCorrection(
            channel_role='center',
            correction_kind='channel_routing_only',
        ),
        reason='channel was mislabeled',
    )
    view = next(
        v for v in controller.measurement_views()
        if v.measurement_id == record.measurement_id
    )
    assert view.assignment_position_compatibility == 'original'
    assert view.original_import_position == MLP
    assert view.effective_channel_role == 'center'


def test_forged_correction_row_fails_on_read(tmp_path: Path) -> None:
    controller, quality_repository, record = _controller(tmp_path)
    dataset = controller.measurement_repository.dataset_for_measurement(
        record.measurement_id
    )
    # A self-consistent payload pointing at a foreign entity — written
    # directly, bypassing save-time validation.
    forged = CadMeasurementCorrection.model_construct(
        correction_id='correction-forged',
        document_id=record.document_id,
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256='0' * 64,
        measurement_entity_id='seat-ghost',
        reason='forged',
        created_at_utc='2026-09-24T00:00:00+00:00',
        correction_sha256='',
    )
    forged = forged.model_copy(
        update={'correction_sha256': _hash(forged.identity_payload())}
    )
    connection = sqlite3.connect(str(quality_repository.path))
    try:
        connection.execute(
            'INSERT INTO cad_measurement_corrections('
            'correction_id, document_id, measurement_id, dataset_id,'
            ' dataset_sha256, correction_sha256, created_at_utc, payload_json)'
            ' VALUES(?,?,?,?,?,?,?,?)',
            (
                forged.correction_id,
                forged.document_id,
                forged.measurement_id,
                forged.dataset_id,
                forged.dataset_sha256,
                forged.correction_sha256,
                forged.created_at_utc,
                forged.model_dump_json(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError, match='dataset hash|does not match'):
        quality_repository.get_correction('correction-forged')
    with pytest.raises(ValueError):
        quality_repository.list_corrections(record.measurement_id)


def test_disposition_fails_closed_on_unverifiable_correction(
    tmp_path: Path,
) -> None:
    controller, quality_repository, record = _controller(tmp_path)
    # Persist a 'corrected' disposition pinning a correction id that does not
    # resolve — direct writes bypass save validation to simulate corruption.
    event = build_measurement_disposition(
        document_id=record.document_id,
        measurement_id=record.measurement_id,
        disposition='corrected',
        reason='marked corrected',
        correction_id='correction-ghost',
    )
    connection = sqlite3.connect(str(quality_repository.path))
    try:
        connection.execute(
            'INSERT INTO cad_measurement_dispositions('
            'disposition_id, document_id, measurement_id, disposition,'
            ' correction_id, disposition_sha256, created_at_utc, payload_json)'
            ' VALUES(?,?,?,?,?,?,?,?)',
            (
                event.disposition_id,
                event.document_id,
                event.measurement_id,
                event.disposition,
                event.correction_id,
                event.disposition_sha256,
                event.created_at_utc,
                event.model_dump_json(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError, match='correction'):
        quality_repository.get_disposition(event.disposition_id)
    with pytest.raises(ValueError, match='correction'):
        quality_repository.latest_disposition(record.measurement_id)
