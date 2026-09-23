from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurement_runner import (
    build_runner_plan,
    guided_step,
    runner_progress,
)
from htdt.cad_measurement_runner_repository import (
    CadMeasurementRunnerRepository,
    RunnerError,
)
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3


def _scene(document_id: str) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
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
            SceneEntity(
                entity_id='point-left',
                kind='measurement_point',
                name='Left seat',
                position=Position3(x_m=2.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _setup(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene('doc-runner'), parent_revision_id=None).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    runner = CadMeasurementRunnerRepository(scene_repository, measurement_repository)
    return revision, measurement_repository, runner


def _plan(revision, **overrides):
    kwargs: dict = {
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'sources': (
            ('front_left', ('speaker-fl',)),
            ('front_right', ('speaker-fr',)),
        ),
        'target_entity_ids': ('point-mlp', 'point-left'),
        'repeat_count': 1,
    }
    kwargs.update(overrides)
    return build_runner_plan(**kwargs)


def _save_measurement(
    repository: CadMeasurementRepository,
    revision,
    entity_id: str,
    measurement_id: str,
    channel_role: str,
):
    declared_raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_status='absent',
        processing={'fixture': measurement_id},
    )
    record = measurement_record_for_revision(
        revision,
        entity_id,
        measurement_id=measurement_id,
        evidence_type='measured',
        channel_role=channel_role,
        source_speaker_ids=('speaker-fl',),
        imported_at='2026-09-19T00:00:00+00:00',
        source_kind='unknown',
        external_source_id=f'rew-{measurement_id}',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_status='absent',
        processing_json=canonical_json({'fixture': measurement_id}),
        source_sha256=sha256(declared_raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    repository.save(
        record,
        dataset,
        raw_filename=f'{measurement_id}.json',
        raw_bytes=declared_raw,
    )
    return record, dataset


def test_plan_enumerates_matrix_deterministically(tmp_path: Path):
    revision, _, _ = _setup(tmp_path)
    plan = _plan(revision)
    # 2 sources × 2 targets × 1 repeat = 4 cells, purpose-major order.
    assert len(plan.cells) == 4
    assert [(c.channel_role, c.target_entity_id) for c in plan.cells] == [
        ('front_left', 'point-mlp'),
        ('front_left', 'point-left'),
        ('front_right', 'point-mlp'),
        ('front_right', 'point-left'),
    ]
    other = _plan(revision, plan_id=plan.plan_id)
    assert other.plan_sha256 == plan.plan_sha256


def test_run_persists_and_next_incomplete_walks_cells(tmp_path: Path):
    revision, mrepo, runner = _setup(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id, started_at='2026-09-23T00:00:00+00:00')
    assert runner.next_incomplete(run.run_id) == 0
    record, dataset = _save_measurement(mrepo, revision, 'point-mlp', 'm-1', 'front_left')
    runner.commit_cell(
        run.run_id,
        0,
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        quality_decision='passed',
        created_at='2026-09-23T00:01:00+00:00',
    )
    assert runner.next_incomplete(run.run_id) == 1
    # A fresh repository connection sees the same state (restart survival).
    reopened = CadMeasurementRunnerRepository(
        runner._scene_repository if hasattr(runner, '_scene_repository') else type('S', (), {'path': runner.path})(),
        mrepo,
    )
    states = reopened.cell_states(run.run_id)
    assert states[0].status == 'completed'
    assert states[0].measurement_id == 'm-1'


def test_blocked_quality_marks_retake_never_completed(tmp_path: Path):
    revision, mrepo, runner = _setup(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id)
    record, dataset = _save_measurement(mrepo, revision, 'point-mlp', 'm-bad', 'front_left')
    event = runner.commit_cell(
        run.run_id,
        0,
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        quality_decision='blocked',
        reason='clipping detected',
    )
    assert event.status == 'retake_required'
    # next_incomplete does NOT skip the blocked cell.
    assert runner.next_incomplete(run.run_id) == 0
    # Retake commit supersedes the failed measurement.
    record2, dataset2 = _save_measurement(mrepo, revision, 'point-mlp', 'm-good', 'front_left')
    retake = runner.commit_cell(
        run.run_id,
        0,
        measurement_id=record2.measurement_id,
        dataset_id=dataset2.dataset_id,
        dataset_sha256=dataset2.dataset_sha256,
        quality_decision='passed',
    )
    assert retake.status == 'completed'
    assert retake.supersedes_measurement_id == 'm-bad'


def test_skip_requires_plan_permission(tmp_path: Path):
    revision, _, runner = _setup(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id)
    with pytest.raises(RunnerError, match='does not allow skipping'):
        runner.skip_cell(run.run_id, 0)
    skippable = _plan(revision, allow_skip=True, plan_id='plan-skip')
    runner.save_plan(skippable)
    run2 = runner.start_run(skippable.plan_id)
    event = runner.skip_cell(run2.run_id, 0, reason='seat blocked by furniture')
    assert event.status == 'skipped'
    assert runner.next_incomplete(run2.run_id) == 1


def test_commit_rejects_unknown_measurement(tmp_path: Path):
    revision, mrepo, runner = _setup(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id)
    with pytest.raises(RunnerError, match='not persisted'):
        runner.commit_cell(
            run.run_id,
            0,
            measurement_id='ghost',
            dataset_id='d',
            dataset_sha256='0' * 64,
        )


def test_guided_step_describes_exact_cell(tmp_path: Path):
    revision, _, runner = _setup(tmp_path)
    plan = _plan(revision)
    step = guided_step(plan, 2)
    assert step.channel_role == 'front_right'
    assert step.target_entity_id == 'point-mlp'
    assert step.source_speaker_ids == ('speaker-fr',)
    # Guidance ids include source and target for 3D highlighting.
    assert 'speaker-fr' in step.guidance_entity_ids
    assert 'point-mlp' in step.guidance_entity_ids


def test_progress_counts_states_not_percentage(tmp_path: Path):
    revision, mrepo, runner = _setup(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id)
    record, dataset = _save_measurement(mrepo, revision, 'point-mlp', 'm-1', 'front_left')
    runner.commit_cell(
        run.run_id,
        0,
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        quality_decision='passed',
    )
    runner.mark_cell(run.run_id, 1, 'staged')
    progress = runner_progress(plan, runner.cell_states(run.run_id))
    assert progress.total == 4
    assert progress.completed == 1
    assert progress.staged == 1
    assert progress.not_started == 2
