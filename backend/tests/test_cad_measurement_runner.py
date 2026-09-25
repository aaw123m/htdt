from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_measurement_disposition import build_measurement_disposition
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadMeasurementQualityEvidence,
    acquisition_context_binding,
    build_acquisition_context,
    build_measurement_observation,
    build_measurement_quality_profile,
    build_measurement_quality_report,
    observation_binding,
)
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
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
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    runner = CadMeasurementRunnerRepository(
        scene_repository, measurement_repository, quality_repository
    )
    return scene_repository, revision, measurement_repository, quality_repository, runner


def _plan(revision, **overrides):
    kwargs: dict = {
        'document_id': revision.document_id,
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


def _save_report(
    quality_repository: CadMeasurementQualityRepository,
    record,
    dataset,
    *,
    retake: bool = False,
):
    """Persist a replay-validated quality report for the runner to read.

    ``retake=True`` yields a FAIL clipping check (RETAKE); otherwise every
    check resolves PASS/NOT_EVALUATED (NOT_NEEDED).
    """
    acquisition = None
    observation = None
    if retake:
        obs = build_measurement_observation(
            measurement_id=record.measurement_id,
            source_kind='rew_metadata',
            source_asset_sha256=dataset.source_sha256,
            clipping_detected=True,
        )
        quality_repository.save_observation(obs)
        observation = observation_binding(obs)
        evidence = CadMeasurementQualityEvidence(
            clipping_detected=True,
            evidence_source='rew_metadata',
        )
    else:
        calibration_sha = quality_repository.save_calibration_file(
            filename='umik.txt',
            raw_bytes=b'runner-calibration',
        )
        context = build_acquisition_context(
            source_kind='native',
            subject_measurement_ids=(record.measurement_id,),
            timing_reference_valid=True,
            timing_reference_id='loopback-1',
            clock_source='umik-1-usb',
            sample_rate_hz=48000,
            delay_correction_s=0.00025,
        )
        quality_repository.save_acquisition_context(context)
        acquisition = acquisition_context_binding(context)
        obs = build_measurement_observation(
            measurement_id=record.measurement_id,
            source_kind='rew_metadata',
            source_asset_sha256=dataset.source_sha256,
            clipping_detected=False,
            peak_dbfs=-3.0,
            snr_db=40.0,
            usable_frequency_band_hz=(20.0, 80.0),
            polarity_correct=True,
            polarity_confidence=0.99,
            has_impulse_response=False,
        )
        quality_repository.save_observation(obs)
        observation = observation_binding(obs)
        evidence = CadMeasurementQualityEvidence(
            clipping_detected=False,
            peak_dbfs=-3.0,
            snr_db=40.0,
            usable_frequency_band_hz=(20.0, 80.0),
            timing_reference_valid=True,
            timing_reference_id='loopback-1',
            clock_source='umik-1-usb',
            sample_rate_hz=48000,
            delay_correction_s=0.00025,
            polarity_correct=True,
            polarity_confidence=0.99,
            has_impulse_response=False,
            calibration_filename='umik.txt',
            calibration_file_sha256=calibration_sha,
            expected_calibration_file_sha256=calibration_sha,
            evidence_source='rew_metadata',
        )
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=evidence,
        profile=build_measurement_quality_profile(),
        acquisition_context=acquisition,
        observation=observation,
    )
    quality_repository.save_report(report)
    return report


def test_plan_enumerates_matrix_deterministically(tmp_path: Path):
    _, revision, _, _, _ = _setup(tmp_path)
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


def test_plan_requires_document_scope(tmp_path: Path):
    scene_repository, revision, _, _, runner = _setup(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    # Plans only list inside their owning document (#853).
    assert runner.list_plans('doc-runner') == (plan,)
    assert runner.list_plans('doc-other') == ()
    # A plan binding a foreign document's scene revision is rejected.
    other_revision = scene_repository.save(
        _scene('doc-other'), parent_revision_id=None
    ).revision
    foreign = _plan(other_revision, document_id='doc-runner', plan_id='plan-foreign')
    with pytest.raises(RunnerError, match='foreign project scene'):
        runner.save_plan(foreign)
    # A plan naming entities that do not exist in the scene is rejected.
    ghost = _plan(
        revision,
        plan_id='plan-ghost',
        target_entity_ids=('point-missing',),
        sources=(('front_left', ('speaker-fl',)),),
    )
    with pytest.raises(RunnerError, match='not an eligible measurement point'):
        runner.save_plan(ghost)


def test_run_persists_and_next_incomplete_walks_cells(tmp_path: Path):
    scene_repository, revision, mrepo, qrepo, runner = _setup(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id, started_at='2026-09-23T00:00:00+00:00')
    assert runner.next_incomplete(run.run_id) == 0
    record, dataset = _save_measurement(mrepo, revision, 'point-mlp', 'm-1', 'front_left')
    # Without a quality report the cell stays quality_pending — the caller
    # cannot assert completion (#853).
    pending = runner.commit_cell(
        run.run_id,
        0,
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        created_at='2026-09-23T00:01:00+00:00',
    )
    assert pending.status == 'quality_pending'
    assert runner.next_incomplete(run.run_id) == 0
    _save_report(qrepo, record, dataset)
    event = runner.commit_cell(
        run.run_id,
        0,
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        created_at='2026-09-23T00:02:00+00:00',
    )
    assert event.status == 'completed'
    assert runner.next_incomplete(run.run_id) == 1
    # A fresh repository connection sees the same state (restart survival).
    reopened = CadMeasurementRunnerRepository(scene_repository, mrepo, qrepo)
    states = reopened.cell_states(run.run_id)
    assert states[0].status == 'completed'
    assert states[0].measurement_id == 'm-1'


def test_blocked_quality_marks_retake_never_completed(tmp_path: Path):
    _, revision, mrepo, qrepo, runner = _setup(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id)
    record, dataset = _save_measurement(mrepo, revision, 'point-mlp', 'm-bad', 'front_left')
    _save_report(qrepo, record, dataset, retake=True)
    event = runner.commit_cell(
        run.run_id,
        0,
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        reason='clipping detected',
    )
    assert event.status == 'retake_required'
    # next_incomplete does NOT skip the blocked cell.
    assert runner.next_incomplete(run.run_id) == 0
    # Retake commit supersedes the failed measurement.
    record2, dataset2 = _save_measurement(mrepo, revision, 'point-mlp', 'm-good', 'front_left')
    _save_report(qrepo, record2, dataset2)
    retake = runner.commit_cell(
        run.run_id,
        0,
        measurement_id=record2.measurement_id,
        dataset_id=dataset2.dataset_id,
        dataset_sha256=dataset2.dataset_sha256,
    )
    assert retake.status == 'completed'
    assert retake.supersedes_measurement_id == 'm-bad'


def test_terminal_cells_reject_further_events(tmp_path: Path):
    _, revision, mrepo, qrepo, runner = _setup(tmp_path)
    plan = _plan(revision, allow_skip=True)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id)
    record, dataset = _save_measurement(mrepo, revision, 'point-mlp', 'm-1', 'front_left')
    _save_report(qrepo, record, dataset)
    runner.commit_cell(
        run.run_id,
        0,
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
    )
    with pytest.raises(RunnerError, match='terminal'):
        runner.mark_cell(run.run_id, 0, 'staged')
    with pytest.raises(RunnerError, match='terminal'):
        runner.skip_cell(run.run_id, 0)
    runner.skip_cell(run.run_id, 1)
    with pytest.raises(RunnerError, match='terminal'):
        runner.commit_cell(
            run.run_id,
            1,
            measurement_id=record.measurement_id,
            dataset_id=dataset.dataset_id,
            dataset_sha256=dataset.dataset_sha256,
        )


def test_commit_rejects_wrong_binding_and_disposition(tmp_path: Path):
    _, revision, mrepo, qrepo, runner = _setup(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id)
    # Wrong channel role for cell 0 (front_left).
    wrong, wrong_dataset = _save_measurement(mrepo, revision, 'point-mlp', 'm-wrong', 'front_right')
    with pytest.raises(RunnerError, match='does not bind the planned cell'):
        runner.commit_cell(
            run.run_id,
            0,
            measurement_id=wrong.measurement_id,
            dataset_id=wrong_dataset.dataset_id,
            dataset_sha256=wrong_dataset.dataset_sha256,
        )
    # Excluded-from-normal-use evidence cannot complete a cell (#509/#853).
    record, dataset = _save_measurement(mrepo, revision, 'point-mlp', 'm-excl', 'front_left')
    qrepo.save_disposition(
        build_measurement_disposition(
            document_id=revision.document_id,
            measurement_id=record.measurement_id,
            disposition='excluded_from_normal_use',
            reason='calibration-sweep residue',
        )
    )
    with pytest.raises(RunnerError, match='not eligible'):
        runner.commit_cell(
            run.run_id,
            0,
            measurement_id=record.measurement_id,
            dataset_id=dataset.dataset_id,
            dataset_sha256=dataset.dataset_sha256,
        )


def test_runner_events_require_aware_timestamps(tmp_path: Path):
    _, revision, mrepo, qrepo, runner = _setup(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id)
    record, dataset = _save_measurement(mrepo, revision, 'point-mlp', 'm-1', 'front_left')
    _save_report(qrepo, record, dataset)
    with pytest.raises(ValueError, match='timezone-aware'):
        runner.commit_cell(
            run.run_id,
            0,
            measurement_id=record.measurement_id,
            dataset_id=dataset.dataset_id,
            dataset_sha256=dataset.dataset_sha256,
            created_at='2026-09-23T00:01:00',
        )


def test_skip_requires_plan_permission(tmp_path: Path):
    _, revision, _, _, runner = _setup(tmp_path)
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
    _, revision, mrepo, _, runner = _setup(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id)
    with pytest.raises(RunnerError, match='does not exist'):
        runner.commit_cell(
            run.run_id,
            0,
            measurement_id='ghost',
            dataset_id='d',
            dataset_sha256='0' * 64,
        )


def test_guided_step_describes_exact_cell(tmp_path: Path):
    _, revision, _, _, _ = _setup(tmp_path)
    plan = _plan(revision)
    step = guided_step(plan, 2)
    assert step.channel_role == 'front_right'
    assert step.target_entity_id == 'point-mlp'
    assert step.source_speaker_ids == ('speaker-fr',)
    # Guidance ids include source and target for 3D highlighting.
    assert 'speaker-fr' in step.guidance_entity_ids
    assert 'point-mlp' in step.guidance_entity_ids


def test_progress_counts_states_not_percentage(tmp_path: Path):
    _, revision, mrepo, qrepo, runner = _setup(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id)
    record, dataset = _save_measurement(mrepo, revision, 'point-mlp', 'm-1', 'front_left')
    _save_report(qrepo, record, dataset)
    runner.commit_cell(
        run.run_id,
        0,
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
    )
    runner.mark_cell(run.run_id, 1, 'staged')
    progress = runner_progress(plan, runner.cell_states(run.run_id))
    assert progress.total == 4
    assert progress.completed == 1
    assert progress.staged == 1
    assert progress.not_started == 2
