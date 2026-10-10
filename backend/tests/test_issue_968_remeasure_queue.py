"""#968 — sealed re-measurement queue generated from poor-quality verdicts.

The queue authority scans the document's sealed quality evaluations and
classifies every committed measurement:

* ``RETAKE`` (declared thresholds failed) → queued with verbatim failed
  criteria and the sealed re-capture inputs;
* ``UNKNOWN`` → soft re-evaluation when every missing evidence is
  completable from stored authorities, queued only when only another
  physical capture supplies the missing evidence;
* everything else → skipped with a stated reason. Unknown quality is
  never silently passed and never queued without a reason.

The queue is sealed and deterministic (same evaluation set → same queue,
idempotent re-generation) and lapses honestly: any evaluation/scene
drift blocks dismiss/convert until regeneration. Dismissals and
campaign conversions are append-only sealed events.
"""

from __future__ import annotations

import os
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from typing import Any
import sqlite3

import pytest
from pydantic import ValidationError

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from htdt.cad_measurement_quality import (
    CadMeasurementQualityEvidence,
    acquisition_context_binding,
    build_acquisition_context,
    build_measurement_quality_profile,
    build_measurement_quality_report,
)
from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.measurement.domain.cad_remeasure_queue import (
    build_queue_event,
    build_remeasure_queue,
)
from htdt.measurement.persistence.cad_remeasure_queue_repository import (
    CadRemeasureQueueRepository,
    RemeasureQueueError,
)
from htdt.measurement.services.cad_remeasure_queue_service import (
    CadRemeasureQueueService,
    RemeasureQueueServiceError,
)


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(
        measurement_repository
    )
    queue_repository = CadRemeasureQueueRepository(scene_repository)
    service = CadRemeasureQueueService(
        scene_repository,
        measurement_repository,
        quality_repository,
        queue_repository=queue_repository,
        producer=None,
    )
    return revision, measurement_repository, quality_repository, service


def _save_measurement(
    repository: CadMeasurementRepository,
    revision,
    measurement_id: str,
    *,
    entity: str = 'point-mlp',
    channel_role: str = 'front_left',
    source_speaker_ids: tuple[str, ...] = ('speaker-fl',),
    raw: bytes | None = None,
):
    raw = raw if raw is not None else measurement_id.encode('utf-8')
    processing = {'fixture_raw': raw.decode('utf-8', errors='replace')}
    declared_raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_deg=None,
        phase_status='absent',
        level_reference='unknown',
        processing=processing,
    )
    record = measurement_record_for_revision(
        revision,
        entity,
        measurement_id=measurement_id,
        evidence_type='measured',
        channel_role=channel_role,
        source_speaker_ids=source_speaker_ids,
        radiation_scope='single',
        routing_evidence='verified',
        imported_at='2026-09-19T00:00:00+00:00',
        source_kind='unknown',
        external_source_id=f'rew-{measurement_id}',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_deg=None,
        phase_status='absent',
        level_reference='unknown',
        processing_json=canonical_json(processing),
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


def _passing_evidence() -> CadMeasurementQualityEvidence:
    cal_sha = 'a' * 64
    return CadMeasurementQualityEvidence(
        clipping_detected=False,
        peak_dbfs=-3.0,
        snr_db=45.0,
        noise_floor_db_spl=30.0,
        signal_level_db_spl=75.0,
        usable_frequency_band_hz=(20.0, 20000.0),
        timing_reference_valid=True,
        timing_reference_id='tr-1',
        clock_source='internal',
        sample_rate_hz=48000,
        delay_correction_s=0.0,
        polarity_correct=True,
        polarity_confidence=0.99,
        has_impulse_response=True,
        ir_window_start_s=0.0,
        ir_window_end_s=0.5,
        ir_truncated=False,
        calibration_filename='mic.cal',
        calibration_file_sha256=cal_sha,
        expected_calibration_file_sha256=cal_sha,
    )


def _persist_report(
    quality_repository: CadMeasurementQualityRepository,
    record,
    dataset,
    report_id: str,
    evidence: CadMeasurementQualityEvidence,
    *,
    context_id: str | None = None,
    calibration_bytes: bytes | None = None,
    expected_calibration_bytes: bytes | None = None,
):
    """Persist an honestly-bound report: observation-typed evidence gets a
    persisted observation authority, timing evidence a bound acquisition
    context, calibration digests retained files — the repository
    re-validates all of them verbatim."""
    from htdt.cad_measurement_quality import (
        OBSERVATION_FIELD_DEFAULTS,
        build_measurement_observation,
        observation_binding,
    )

    profile = build_measurement_quality_profile()
    kwargs: dict = {}
    observation_fields = (
        'clipping_detected', 'peak_dbfs', 'noise_floor_db_spl',
        'signal_level_db_spl', 'snr_db', 'usable_frequency_band_hz',
        'polarity_correct', 'polarity_confidence', 'has_impulse_response',
        'ir_window_start_s', 'ir_window_end_s', 'ir_truncated',
    )
    timing_fields = (
        'timing_reference_valid', 'timing_reference_id', 'clock_source',
        'sample_rate_hz', 'delay_correction_s',
    )
    if (
        evidence.calibration_file_sha256 is not None
        or evidence.expected_calibration_file_sha256 is not None
    ):
        updates: dict[str, Any] = {}
        if evidence.calibration_file_sha256 is not None:
            updates['calibration_file_sha256'] = (
                quality_repository.save_calibration_file(
                    filename=(
                        evidence.calibration_filename or 'umik.txt'
                    ),
                    raw_bytes=(
                        calibration_bytes
                        or f'cal-{report_id}'.encode('utf-8')
                    ),
                )
            )
        if evidence.expected_calibration_file_sha256 is not None:
            if expected_calibration_bytes is None:
                updates['expected_calibration_file_sha256'] = updates[
                    'calibration_file_sha256'
                ]
            else:
                updates['expected_calibration_file_sha256'] = (
                    quality_repository.save_calibration_file(
                        filename='expected.txt',
                        raw_bytes=expected_calibration_bytes,
                    )
                )
        evidence = evidence.model_copy(update=updates)
    if any(
        getattr(evidence, field) != OBSERVATION_FIELD_DEFAULTS[field]
        for field in observation_fields
    ):
        evidence = evidence.model_copy(
            update={'evidence_source': 'manual'}
        )
        observation = build_measurement_observation(
            observation_id=f'obs-{report_id}',
            measurement_id=record.measurement_id,
            source_kind='manual',
            observed_at_utc='2026-09-19T00:00:30+00:00',
            **{
                field: getattr(evidence, field)
                for field in observation_fields
            },
        )
        quality_repository.save_observation(observation)
        kwargs['observation'] = observation_binding(observation)
        kwargs['observation_record'] = observation
    if any(
        getattr(evidence, field) is not None for field in timing_fields
    ) or context_id is not None:
        context = build_acquisition_context(
            acquisition_context_id=context_id or f'ctx-{report_id}',
            source_kind='native',
            subject_measurement_ids=(record.measurement_id,),
            created_at_utc='2026-09-19T00:00:00+00:00',
            **{
                field: getattr(evidence, field)
                for field in timing_fields
            },
        )
        quality_repository.save_acquisition_context(context)
        kwargs['acquisition_context'] = acquisition_context_binding(context)
        kwargs['acquisition_context_record'] = context
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=evidence,
        profile=profile,
        report_id=report_id,
        created_at_utc='2026-09-19T00:01:00+00:00',
        **kwargs,
    )
    quality_repository.save_report(report)
    return report


def test_queue_extracts_retake_rows_with_verbatim_criteria(
    tmp_path: Path,
) -> None:
    """Multi-fixture DoD: of three measurements only the threshold-failing
    one is queued, with verbatim criteria + sealed-input conditions."""
    revision, measurements, quality, service = _repositories(tmp_path)
    bad_record, bad_dataset = _save_measurement(
        measurements, revision, 'm-bad'
    )
    good_record, good_dataset = _save_measurement(
        measurements, revision, 'm-good'
    )
    _save_measurement(measurements, revision, 'm-none')
    bad_report = _persist_report(
        quality,
        bad_record,
        bad_dataset,
        'r-bad',
        CadMeasurementQualityEvidence(
            clipping_detected=True,
            snr_db=10.0,
            sample_rate_hz=48000,
            calibration_file_sha256='b' * 64,
            expected_calibration_file_sha256='a' * 64,
        ),
        context_id='ctx-bad',
        expected_calibration_bytes=b'expected-calibration',
    )
    assert bad_report.retake_recommendation == 'RETAKE'
    _persist_report(
        quality,
        good_record,
        good_dataset,
        'r-good',
        _passing_evidence(),
        context_id='ctx-good',
    )

    queue = service.generate(revision.document_id)

    assert queue.document_id == revision.document_id
    assert queue.scene_revision_id == revision.revision_id
    assert queue.scene_content_hash == revision.content_hash
    assert queue.queue_id.startswith('rqueue-')

    assert [item.measurement_id for item in queue.items] == ['m-bad']
    item = queue.items[0]
    # Verbatim criteria — never paraphrased or fabricated.
    assert 'clipping' in item.failed_checks
    assert 'calibration' in item.failed_checks
    assert any(
        reason.startswith('clipping:')
        and 'clipping' in reason
        for reason in item.retake_reasons
    )
    assert any(
        reason.startswith('calibration:') for reason in item.retake_reasons
    )
    assert item.report_id == 'r-bad'
    assert item.report_sha256 == bad_report.report_sha256

    # Required inputs resolved from the sealed context, not fabricated.
    inputs = item.inputs
    assert inputs.measurement_entity_id == 'point-mlp'
    assert inputs.channel_role == 'front_left'
    assert inputs.source_speaker_ids == ('speaker-fl',)
    assert inputs.acquisition_context_id == 'ctx-bad'
    assert inputs.calibration_file_sha256 == bad_report.evidence.calibration_file_sha256
    assert inputs.sample_rate_hz == 48000

    # The passing measurement counts as passed; the report-less one is
    # producer-derived (UNKNOWN verdict) → soft/skipped, never queued.
    assert queue.passed_count == 1
    assert 'm-good' not in {
        i.measurement_id for i in queue.items
    }
    non_queued = {
        c.measurement_id
        for c in (*queue.soft_candidates, *queue.skipped)
    }
    assert 'm-none' in non_queued


def test_queue_generation_is_deterministic_and_idempotent(
    tmp_path: Path,
) -> None:
    revision, measurements, quality, service = _repositories(tmp_path)
    record, dataset = _save_measurement(measurements, revision, 'm-1')
    _persist_report(
        quality, record, dataset, 'r-1',
        CadMeasurementQualityEvidence(clipping_detected=True),
    )

    first = service.generate(revision.document_id)
    second = service.generate(revision.document_id)
    assert first.queue_id == second.queue_id
    assert first.queue_sha256 == second.queue_sha256
    assert len(
        service.queue_repository.list_queues(revision.document_id)
    ) == 1
    assert service.status(first) == 'current'


def test_queue_lapses_when_evaluation_set_changes(tmp_path: Path) -> None:
    """A stale queue lapses honestly: any evaluation drift blocks
    dismissal/conversion until the queue is regenerated."""
    revision, measurements, quality, service = _repositories(tmp_path)
    record_a, dataset_a = _save_measurement(measurements, revision, 'm-a')
    record_b, dataset_b = _save_measurement(measurements, revision, 'm-b')
    _persist_report(
        quality, record_a, dataset_a, 'r-a',
        CadMeasurementQualityEvidence(clipping_detected=True),
    )

    queue = service.generate(revision.document_id)
    assert service.status(queue) == 'current'

    # A new sealed report epoch on ANY measurement lapses the queue —
    # even on a measurement that was not queued.
    _persist_report(
        quality, record_b, dataset_b, 'r-b',
        _passing_evidence(),
    )
    assert service.status(queue) == 'lapsed_evaluations'
    with pytest.raises(RemeasureQueueServiceError):
        service.dismiss_item(queue.queue_id, 'm-a', reason='stale')
    with pytest.raises(RemeasureQueueServiceError):
        service.convert_to_runner_plan(queue.queue_id)

    regenerated = service.generate(revision.document_id)
    assert regenerated.queue_id != queue.queue_id
    assert service.status(regenerated) == 'current'


def test_queue_lapses_on_scene_change(tmp_path: Path) -> None:
    revision, measurements, quality, service = _repositories(tmp_path)
    record, dataset = _save_measurement(measurements, revision, 'm-1')
    _persist_report(
        quality, record, dataset, 'r-1',
        CadMeasurementQualityEvidence(clipping_detected=True),
    )
    queue = service.generate(revision.document_id)
    assert service.status(queue) == 'current'

    scene = make_f1_scene()
    drifted = scene.model_copy(
        update={'room': scene.room.model_copy(update={'width_m': 6.1})}
    )
    service.scene_repository.save(
        drifted, parent_revision_id=revision.revision_id
    )
    assert service.status(queue) == 'lapsed_scene'
    with pytest.raises(RemeasureQueueServiceError):
        service.convert_to_runner_plan(queue.queue_id)


def test_dismiss_records_reason_and_is_terminal(tmp_path: Path) -> None:
    revision, measurements, quality, service = _repositories(tmp_path)
    record, dataset = _save_measurement(measurements, revision, 'm-1')
    _persist_report(
        quality, record, dataset, 'r-1',
        CadMeasurementQualityEvidence(clipping_detected=True),
    )
    queue = service.generate(revision.document_id)

    with pytest.raises(RemeasureQueueServiceError):
        service.dismiss_item(queue.queue_id, 'm-1', reason='   ')
    with pytest.raises(RemeasureQueueServiceError):
        service.dismiss_item(queue.queue_id, 'm-ghost', reason='gone')

    event = service.dismiss_item(
        queue.queue_id, 'm-1', reason='家具が移動したため再配置が必要'
    )
    assert event.event_id.startswith('rqev-')
    assert event.kind == 'dismissed'
    assert event.reason == '家具が移動したため再配置が必要'
    assert event.queue_sha256 == queue.queue_sha256

    states = service.snapshot(queue).item_states
    assert states['m-1'] == 'dismissed'
    # Terminal: a second transition on the same item is refused.
    with pytest.raises(RemeasureQueueError):
        service.dismiss_item(queue.queue_id, 'm-1', reason='again')
    with pytest.raises(RemeasureQueueServiceError):
        service.convert_to_runner_plan(queue.queue_id)


def test_convert_to_runner_plan_preserves_exact_binding(
    tmp_path: Path,
) -> None:
    revision, measurements, quality, service = _repositories(tmp_path)
    record, dataset = _save_measurement(measurements, revision, 'm-1')
    _persist_report(
        quality, record, dataset, 'r-1',
        CadMeasurementQualityEvidence(clipping_detected=True),
    )
    queue = service.generate(revision.document_id)

    plan = service.convert_to_runner_plan(queue.queue_id)
    assert plan.scene_revision_id == revision.revision_id
    assert plan.scene_content_hash == revision.content_hash
    assert len(plan.cells) == 1
    cell = plan.cells[0]
    assert cell.target_entity_id == 'point-mlp'
    assert cell.channel_role == 'front_left'
    assert cell.source_speaker_ids == ('speaker-fl',)
    assert 'm-1' in cell.notes

    snapshot = service.snapshot(queue)
    assert snapshot.item_states['m-1'] == 'converted'
    events = snapshot.events
    assert len(events) == 1
    assert events[0].runner_plan_id == plan.plan_id

    # The plan is materialized once — regeneration cannot double-register.
    plans = service.runner_repository.list_plans(revision.document_id)
    assert len(plans) == 1
    with pytest.raises(RemeasureQueueServiceError):
        service.convert_to_runner_plan(queue.queue_id)


def test_persisted_queue_event_requires_pending_member(
    tmp_path: Path,
) -> None:
    """Fail-closed event writes: foreign queue ids, foreign measurements
    and non-pending items are all refused."""
    revision, measurements, quality, service = _repositories(tmp_path)
    record, dataset = _save_measurement(measurements, revision, 'm-1')
    _persist_report(
        quality, record, dataset, 'r-1',
        CadMeasurementQualityEvidence(clipping_detected=True),
    )
    queue = service.generate(revision.document_id)

    foreign = build_queue_event(
        queue=queue.model_copy(update={'queue_sha256': 'c' * 64}),
        measurement_id='m-1',
        kind='dismissed',
        reason='x',
        created_at_utc='2026-09-19T01:00:00+00:00',
    )
    # _seal re-derives the id from the sha so the event is self-consistent
    # but still bound to a queue revision that is not persisted.
    with pytest.raises(RemeasureQueueError):
        service.queue_repository.append_event(foreign)

    with pytest.raises(RemeasureQueueError):
        service.queue_repository.append_event(
            build_queue_event(
                queue=queue,
                measurement_id='m-unqueued',
                kind='dismissed',
                reason='x',
                created_at_utc='2026-09-19T01:00:00+00:00',
            )
        )


def test_queue_rejects_foreign_or_mismatched_scene_binding(
    tmp_path: Path,
) -> None:
    """Cross-revision/cross-project binding drift fails closed at save."""
    revision, measurements, quality, service = _repositories(tmp_path)
    record, dataset = _save_measurement(measurements, revision, 'm-1')
    _persist_report(
        quality, record, dataset, 'r-1',
        CadMeasurementQualityEvidence(clipping_detected=True),
    )
    queue = service.generate(revision.document_id)

    tampered = queue.model_copy(
        update={'scene_content_hash': 'f' * 64}
    )
    with pytest.raises(ValidationError):
        # The sealed sha no longer matches — the model refuses outright.
        type(queue).model_validate(tampered.model_dump())
    # A queue whose scene revision is not persisted cannot save.
    fake = build_remeasure_queue(
        document_id=revision.document_id,
        scene_revision_id='rev-missing',
        scene_content_hash=revision.content_hash,
        evaluations=(),
    )
    with pytest.raises(RemeasureQueueError):
        service.queue_repository.save_queue(fake)


def test_unknown_quality_never_queues_silently(tmp_path: Path) -> None:
    """UNKNOWN verdicts are classified honestly: completable evidence →
    soft re-evaluation, capture-only evidence → queued, never passed."""
    revision, measurements, quality, service = _repositories(tmp_path)
    # All-None evidence: every check UNKNOWN → soft candidate (all
    # missing evidence is completable from stored authorities).
    rec_soft, ds_soft = _save_measurement(
        measurements, revision, 'm-soft'
    )
    _persist_report(
        quality, rec_soft, ds_soft, 'r-soft',
        CadMeasurementQualityEvidence(),
    )
    # Same all-missing evidence on a DIFFERENT binding: with no committed
    # same-binding sibling, 'repeat_measurements' only a new capture
    # supplies — the row stays queued even though the verdict is UNKNOWN.
    rec_rep, ds_rep = _save_measurement(
        measurements,
        revision,
        'm-repeat',
        channel_role='center',
        source_speaker_ids=('speaker-c',),
    )
    _persist_report(
        quality, rec_rep, ds_rep, 'r-repeat',
        CadMeasurementQualityEvidence(),
    )
    # NOT_EVALUATED-only: everything populated → NOT_NEEDED (passed).
    rec_ok, ds_ok = _save_measurement(measurements, revision, 'm-ok')
    _persist_report(
        quality, rec_ok, ds_ok, 'r-ok', _passing_evidence()
    )

    queue = service.generate(revision.document_id)

    item_ids = {item.measurement_id for item in queue.items}
    soft_ids = {c.measurement_id for c in queue.soft_candidates}
    skipped = {c.measurement_id: c for c in queue.skipped}
    assert 'm-soft' in soft_ids
    assert 'm-repeat' in item_ids  # repeat capture genuinely needed
    assert 'm-ok' not in item_ids | soft_ids | skipped.keys()
    assert queue.passed_count == 1
    # Every non-queued row carries a stated reason — never silent.
    for candidate in queue.soft_candidates:
        assert candidate.missing_evidence
    for row in queue.skipped:
        assert row.reason_code


def test_report_unreadable_lands_skipped_not_queued(tmp_path: Path) -> None:
    """Partial error: one measurement's unreadable report cannot poison
    the queue — it lands in skipped with its stated reason."""
    revision, measurements, quality, service = _repositories(tmp_path)
    rec_bad, ds_bad = _save_measurement(measurements, revision, 'm-bad')
    rec_ok, ds_ok = _save_measurement(measurements, revision, 'm-ok')
    _persist_report(
        quality, rec_bad, ds_bad, 'r-bad',
        CadMeasurementQualityEvidence(clipping_detected=True),
    )
    _persist_report(
        quality, rec_ok, ds_ok, 'r-ok', _passing_evidence()
    )
    # Corrupt the report payload directly so replay validation fails.
    with closing(sqlite3.connect(quality.path)) as connection, connection:
        connection.execute(
            "UPDATE cad_measurement_quality_reports SET payload_json='{bad'"
            " WHERE report_id='r-bad'"
        )

    queue = service.generate(revision.document_id)
    skipped = {row.measurement_id: row for row in queue.skipped}
    assert 'm-bad' in skipped
    assert skipped['m-bad'].reason_code == 'report_unreadable'
    assert {item.measurement_id for item in queue.items} == set()


def test_evaluation_set_pin_detects_removed_report(tmp_path: Path) -> None:
    revision, measurements, quality, service = _repositories(tmp_path)
    rec, ds = _save_measurement(measurements, revision, 'm-1')
    _persist_report(
        quality, rec, ds, 'r-1',
        CadMeasurementQualityEvidence(clipping_detected=True),
    )
    queue = service.generate(revision.document_id)
    assert service.status(queue) == 'current'
    with closing(sqlite3.connect(quality.path)) as connection, connection:
        connection.execute(
            "DELETE FROM cad_measurement_quality_reports WHERE report_id='r-1'"
        )
    assert service.status(queue) == 'lapsed_evaluations'


# ----------------------------------------------------------------------
# Qt surface (offscreen): generate → dismiss → convert drives the real
# workspace widgets; a cancelled dialog or lapsed queue does nothing.
# ----------------------------------------------------------------------


def _workspace(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from htdt.measurement_page_workspace import MeasurementPageWorkspace
    from htdt.measurement_workflow import MeasurementWorkflowController

    app = QApplication.instance() or QApplication([])
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    controller = MeasurementWorkflowController(
        scene_repository, revision.document_id
    )
    return app, revision, controller, MeasurementPageWorkspace(controller)


def test_remeasure_context_page_lists_verdicts(tmp_path: Path) -> None:
    app, revision, controller, workspace = _workspace(tmp_path)
    try:
        measurements = controller.measurement_repository
        quality = controller.quality_repository
        rec, ds = _save_measurement(measurements, revision, 'm-ui')
        _persist_report(
            quality, rec, ds, 'r-ui',
            CadMeasurementQualityEvidence(clipping_detected=True),
        )
        queue = controller.generate_remeasure_queue()
        workspace.refresh()
        workspace.set_context('remeasure')

        from PySide6.QtCore import Qt

        assert workspace.remeasure_table.rowCount() == 1
        cell = workspace.remeasure_table.item(0, 0)
        assert cell.data(Qt.ItemDataRole.UserRole) == 'm-ui'
        assert 'clipping' in workspace.remeasure_table.item(0, 4).text()
        assert workspace.remeasure_table.item(0, 5).text() == '保留中'
        assert workspace.remeasure_convert_button.isEnabled()
        assert 'rqueue-' in workspace.remeasure_status_label.text()
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_remeasure_dismiss_via_dialog_records_reason(
    tmp_path: Path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QInputDialog

    app, revision, controller, workspace = _workspace(tmp_path)
    try:
        measurements = controller.measurement_repository
        quality = controller.quality_repository
        rec, ds = _save_measurement(measurements, revision, 'm-ui')
        _persist_report(
            quality, rec, ds, 'r-ui',
            CadMeasurementQualityEvidence(clipping_detected=True),
        )
        queue = controller.generate_remeasure_queue()
        workspace.refresh()
        workspace.set_context('remeasure')
        workspace.remeasure_table.selectRow(0)

        # Cancel: nothing is recorded.
        monkeypatch.setattr(
            QInputDialog, 'getText',
            staticmethod(lambda *a, **k: ('', False)),
        )
        workspace._dismiss_remeasure_item()
        snapshot = controller.remeasure_queue()
        assert snapshot.item_states['m-ui'] == 'pending'

        # Accept with a reason: the sealed event is appended.
        monkeypatch.setattr(
            QInputDialog, 'getText',
            staticmethod(lambda *a, **k: ('機材を変更した', True)),
        )
        workspace._dismiss_remeasure_item()
        snapshot = controller.remeasure_queue()
        assert snapshot.item_states['m-ui'] == 'dismissed'
        assert snapshot.events[0].reason == '機材を変更した'
        workspace.refresh()
        assert workspace.remeasure_table.item(0, 5).text() == '却下済み'
        assert not workspace.remeasure_convert_button.isEnabled()
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_remeasure_convert_creates_selectable_campaign_plan(
    tmp_path: Path,
) -> None:
    app, revision, controller, workspace = _workspace(tmp_path)
    try:
        measurements = controller.measurement_repository
        quality = controller.quality_repository
        rec, ds = _save_measurement(measurements, revision, 'm-ui')
        _persist_report(
            quality, rec, ds, 'r-ui',
            CadMeasurementQualityEvidence(clipping_detected=True),
        )
        queue = controller.generate_remeasure_queue()
        workspace.refresh()
        workspace.set_context('remeasure')
        workspace._convert_remeasure_queue()

        plans = controller.runner_plans()
        assert len(plans) == 1
        assert plans[0].cells[0].target_entity_id == 'point-mlp'
        # The new plan is selectable on the campaign page.
        idx = workspace.campaign_plan_combo.findData(plans[0].plan_id)
        assert idx >= 0
        snapshot = controller.remeasure_queue()
        assert snapshot.item_states['m-ui'] == 'converted'
        assert not workspace.remeasure_convert_button.isEnabled()
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()
