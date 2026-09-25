"""#725 measurement session orchestration (Stage A) tests."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadMeasurementQualityEvidence,
    build_measurement_observation,
    build_measurement_quality_profile,
    build_measurement_quality_report,
    observation_binding,
)
from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurement_runner import build_runner_plan
from htdt.cad_measurement_runner_repository import (
    CadMeasurementRunnerRepository,
    RunnerError,
)
from htdt.cad_measurement_session import (
    load_session_view,
    session_context_state,
    verify_cell_match,
)
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene

NOW = '2026-09-23T00:00:00+00:00'
LATER = '2026-09-23T01:00:00+00:00'


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    measurements = CadMeasurementRepository(scene_repository)
    quality = CadMeasurementQualityRepository(measurements)
    runner = CadMeasurementRunnerRepository(
        scene_repository, measurements, quality
    )
    return scene_repository, revision, measurements, quality, runner


def _plan(revision):
    return build_runner_plan(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        sources=(('FL', ('speaker-fl',)), ('FR', ('speaker-fr',))),
        target_entity_ids=('point-mlp',),
        repeat_count=2,
    )


def _save_measurement(measurements, revision, measurement_id, **over):
    processing = {'fixture_raw': f'raw-{measurement_id}'}
    raw = declared_fr_raw(
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_status='absent',
        level_reference='spl',
        processing=processing,
    )
    record = measurement_record_for_revision(
        revision,
        over.pop('target_entity_id', 'point-mlp'),
        measurement_id=measurement_id,
        evidence_type=over.pop('evidence_type', 'measured'),
        channel_role=over.pop('channel_role', 'FL'),
        source_speaker_ids=over.pop('source_speaker_ids', ('speaker-fl',)),
        radiation_scope=over.pop('radiation_scope', 'single'),
        routing_evidence='verified',
        imported_at=NOW,
        source_kind='unknown',
        external_source_id=f'rew-{measurement_id}',
        **over,
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_status='absent',
        level_reference='spl',
        processing_json=canonical_json(processing),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurements.save(
        record, dataset, raw_filename=f'{measurement_id}.json',
        raw_bytes=raw,
    )
    return record, dataset


def _save_retake_report(quality, record, dataset) -> None:
    """Persist a RETAKE-verdict report (clipping flagged)."""
    obs = build_measurement_observation(
        measurement_id=record.measurement_id,
        source_kind='rew_metadata',
        source_asset_sha256=dataset.source_sha256,
        clipping_detected=True,
    )
    quality.save_observation(obs)
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            clipping_detected=True,
            evidence_source='rew_metadata',
        ),
        profile=build_measurement_quality_profile(),
        observation=observation_binding(obs),
    )
    quality.save_report(report)


def test_session_view_rebuilds_resume_state(tmp_path: Path) -> None:
    scenes, revision, measurements, _quality, runner = _repositories(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id, started_at=NOW)
    record, dataset = _save_measurement(measurements, revision, 'm-1')
    runner.commit_cell(
        run.run_id, 0,
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
    )
    view = load_session_view(runner, scenes, run.run_id, resumed_at_utc=LATER)
    # No quality report yet — the committed cell is pending, not completed.
    assert view.pending_cells == (0,)
    assert view.next_cell_index == 0
    assert view.next_step is not None
    assert view.next_step.channel_role == 'FL'
    assert view.context_state == 'current'
    assert view.document_id == revision.document_id


def test_cell_match_verification(tmp_path: Path) -> None:
    _s, revision, measurements, _q, _r = _repositories(tmp_path)
    plan = _plan(revision)
    record, _dataset = _save_measurement(measurements, revision, 'm-1')
    cell = plan.cell(0)
    assert verify_cell_match(cell, record) == ()

    wrong_channel, _ = _save_measurement(
        measurements, revision, 'm-2', channel_role='FR'
    )
    violations = verify_cell_match(cell, wrong_channel)
    assert any('channel role' in item for item in violations)

    # A record that targeted a different measurement point is not this
    # cell's planned target (fixture has one point — retarget the record).
    wrong_target = record.model_copy(
        update={'measurement_entity_id': 'point-other'}
    )
    violations = verify_cell_match(cell, wrong_target)
    assert any('target' in item for item in violations)

    wrong_sources, _ = _save_measurement(
        measurements, revision, 'm-4',
        source_speaker_ids=('speaker-fr',),
    )
    violations = verify_cell_match(cell, wrong_sources)
    assert any('source set' in item for item in violations)


def test_commit_rejects_wrong_trace(tmp_path: Path) -> None:
    _s, revision, measurements, _q, runner = _repositories(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id, started_at=NOW)

    # Cell 1 is FL repeat 1; committing an FR trace is rejected — the
    # mismatch is proven, never silently bound.
    wrong, wrong_dataset = _save_measurement(
        measurements, revision, 'm-wrong', channel_role='FR',
        source_speaker_ids=('speaker-fr',),
    )
    with pytest.raises(RunnerError, match='does not bind the planned cell'):
        runner.commit_cell(
            run.run_id, 1,
            measurement_id=wrong.measurement_id,
            dataset_id=wrong_dataset.dataset_id,
            dataset_sha256=wrong_dataset.dataset_sha256,
        )


def test_commit_accepts_exact_trace(tmp_path: Path) -> None:
    _s, revision, measurements, _q, runner = _repositories(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id, started_at=NOW)
    record, dataset = _save_measurement(measurements, revision, 'm-ok')
    event = runner.commit_cell(
        run.run_id, 0,
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
    )
    # Accepted and parked until a replay-validated quality report lands.
    assert event.status == 'quality_pending'
    states = runner.cell_states(run.run_id)
    assert states[0].status == 'quality_pending'


def test_commit_rejects_foreign_revision(tmp_path: Path) -> None:
    scenes, revision, measurements, _q, runner = _repositories(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id, started_at=NOW)
    # A measurement pinned to a different revision of the same document —
    # the room is slightly modified so the revision id actually differs.
    modified = make_f1_scene().model_copy(
        update={'room': make_f1_scene().room.model_copy(update={'width_m': 6.1})}
    )
    other_revision = scenes.save(
        modified, parent_revision_id=revision.revision_id
    ).revision
    foreign, foreign_dataset = _save_measurement(
        measurements, other_revision, 'm-foreign'
    )
    with pytest.raises(RunnerError, match='different scene/document'):
        runner.commit_cell(
            run.run_id, 0,
            measurement_id=foreign.measurement_id,
            dataset_id=foreign_dataset.dataset_id,
            dataset_sha256=foreign_dataset.dataset_sha256,
        )


def test_session_context_flags_stale_plan(tmp_path: Path) -> None:
    scenes, revision, _m, _q, runner = _repositories(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id, started_at=NOW)
    # Plan pins the current revision -> current.
    assert session_context_state(
        plan, scenes.latest(revision.document_id)
    ) == 'current'
    # A newer revision lands -> the session reports STALE, not silent.
    modified = make_f1_scene().model_copy(
        update={'room': make_f1_scene().room.model_copy(update={'width_m': 6.1})}
    )
    scenes.save(modified, parent_revision_id=revision.revision_id)
    view = load_session_view(runner, scenes, run.run_id, resumed_at_utc=LATER)
    assert view.context_state == 'stale'


def test_session_surfaces_staged_and_blocked_cells(tmp_path: Path) -> None:
    scenes, revision, measurements, quality, runner = _repositories(tmp_path)
    plan = _plan(revision)
    runner.save_plan(plan)
    run = runner.start_run(plan.plan_id, started_at=NOW)
    runner.mark_cell(run.run_id, 0, 'staged', reason='staged trace')
    record, dataset = _save_measurement(
        measurements, revision, 'm-bad', channel_role='FR',
        source_speaker_ids=('speaker-fr',),
    )
    # A RETAKE-verdict report turns the commit into retake_required.
    _save_retake_report(quality, record, dataset)
    runner.commit_cell(
        run.run_id, 2,
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
    )
    view = load_session_view(runner, scenes, run.run_id, resumed_at_utc=LATER)
    assert view.staged_cells == (0,)
    assert view.blocked_cells == (2,)
    assert view.next_cell_index == 0  # staged cell is next to resolve
