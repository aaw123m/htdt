from datetime import timedelta, timezone
from pathlib import Path

import pytest

from htdt.cad_document import WorkingDocument
from htdt.cad_measurement_jobs import MeasurementJobApplyContext, MeasurementJobGuard
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import measurement_record_for_revision, normalize_rew_api_snapshot, normalize_rew_capture_timestamp, normalize_rew_text
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, make_f1_scene
from htdt.comparison import FrequencyResponse, compare_frequency_responses
from htdt.rew_api import RewFrequencyResponse, RewFrequencyResponseSnapshot


def _saved_f1(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    return scene_repository, revision


def test_measurement_round_trip_stays_bound_to_source_revision_after_later_scene_edit(tmp_path: Path) -> None:
    scene_repository, revision_a = _saved_f1(tmp_path)
    raw = b'Frequency SPL\n20 70.0\n40 71.5\n80 69.0\n'
    record, dataset, filename, source = normalize_rew_text(
        revision_a,
        'point-mlp',
        raw,
        filename='mlp-fl.txt',
        evidence_type='measured',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        routing_evidence='verified',
        imported_at='2026-09-17T09:30:00+00:00',
    )
    repository = CadMeasurementRepository(scene_repository)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=source)

    working = WorkingDocument(
        revision_a.document,
        source_revision_id=revision_a.revision_id,
        saved_content_hash=revision_a.content_hash,
    )
    working.move_entity('point-mlp', Position3(x_m=3.2, y_m=3.0, z_m=1.1))
    revision_b = scene_repository.save(
        working.committed_document,
        parent_revision_id=revision_a.revision_id,
    ).revision

    reopened = repository.get_measurement(record.measurement_id)
    reopened_dataset = repository.get_dataset(dataset.dataset_id)
    source_revision = repository.source_revision(record.measurement_id)

    assert reopened == record
    assert reopened_dataset == dataset
    assert reopened.scene_revision_id == revision_a.revision_id
    assert reopened.scene_content_hash == revision_a.content_hash
    assert reopened.measurement_position == Position3(x_m=3.0, y_m=3.0, z_m=1.1)
    assert revision_b.document.entity('point-mlp').position == Position3(x_m=3.2, y_m=3.0, z_m=1.1)
    assert source_revision.revision_id == revision_a.revision_id
    assert source_revision.document.entity('point-mlp').position == reopened.measurement_position


def test_repository_rejects_record_that_does_not_match_source_revision(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    repository = CadMeasurementRepository(scene_repository)
    raw = b'20 70\n40 71\n'
    record, dataset, filename, source = normalize_rew_text(
        revision,
        'point-mlp',
        raw,
        filename='measurement.txt',
    )

    wrong_position = record.model_copy(
        update={'measurement_position': Position3(x_m=3.1, y_m=3.0, z_m=1.1)}
    )
    with pytest.raises(ValueError, match='position snapshot'):
        repository.save(wrong_position, dataset, raw_filename=filename, raw_bytes=source)

    wrong_hash = record.model_copy(update={'scene_content_hash': '0' * 64})
    with pytest.raises(ValueError, match='content hash'):
        repository.save(wrong_hash, dataset, raw_filename=filename, raw_bytes=source)

    missing_entity = record.model_copy(update={'measurement_entity_id': 'missing-point'})
    with pytest.raises(ValueError, match='measurement entity'):
        repository.save(missing_entity, dataset, raw_filename=filename, raw_bytes=source)


def test_rew_api_normalization_keeps_external_uuid_as_provenance_not_primary_key(tmp_path: Path) -> None:
    _, revision = _saved_f1(tmp_path)
    decoded = RewFrequencyResponse(
        measurement_id='rew-external-uuid',
        unit='SPL',
        smoothing=None,
        start_frequency_hz=20.0,
        points_per_octave=None,
        frequency_step_hz=20.0,
        frequency_hz=(20.0, 40.0, 60.0),
        magnitude=(70.0, 71.0, 69.5),
        phase_deg=None,
        requested_unit='SPL',
        requested_ppo=None,
        requested_smoothing=None,
    )
    snapshot = RewFrequencyResponseSnapshot(
        measurement_summary={'uuid': 'rew-external-uuid', 'date': '2026-09-17T18:00:00+09:00'},
        query={'unit': 'SPL'},
        raw_frequency_response={'unit': 'SPL', 'startFreq': 20.0, 'freqStep': 20.0},
        decoded=decoded,
    )

    record, dataset, filename, raw = normalize_rew_api_snapshot(
        revision,
        'point-mlp',
        snapshot,
        evidence_type='measured',
    )

    assert record.measurement_id != 'rew-external-uuid'
    assert record.external_source_id == 'rew-external-uuid'
    assert record.source_kind == 'rew_api'
    assert record.captured_at == '2026-09-17T18:00:00+09:00'
    assert dataset.measurement_id == record.measurement_id
    assert dataset.level_reference == 'unknown'
    assert dataset.phase_status == 'absent'
    assert filename == 'rew-api-rew-external-uuid.json'
    assert raw



def test_rew_legacy_local_capture_time_becomes_offset_aware() -> None:
    normalized, source = normalize_rew_capture_timestamp(
        '2026-Sep-18 12:11:10',
        host_timezone=timezone(timedelta(hours=9)),
    )

    assert normalized == '2026-09-18T12:11:10+09:00'
    assert source == 'host_local_timezone'


def test_rew_campaign_import_records_exact_campaign_provenance(tmp_path: Path) -> None:
    _, revision = _saved_f1(tmp_path)
    decoded = RewFrequencyResponse(
        measurement_id='rew-campaign-uuid',
        unit='SPL',
        smoothing=None,
        start_frequency_hz=20.0,
        points_per_octave=None,
        frequency_step_hz=20.0,
        frequency_hz=(20.0, 40.0, 60.0),
        magnitude=(70.0, 71.0, 69.5),
        phase_deg=None,
        requested_unit='SPL',
        requested_ppo=None,
        requested_smoothing=None,
    )
    snapshot = RewFrequencyResponseSnapshot(
        measurement_summary={
            'uuid': 'rew-campaign-uuid',
            'date': '2026-Sep-18 12:11:10',
            'rewVersion': 'V5.40 beta 135',
        },
        query={'unit': 'SPL'},
        raw_frequency_response={'unit': 'SPL', 'startFreq': 20.0, 'freqStep': 20.0},
        decoded=decoded,
    )

    record, _dataset, _filename, _raw = normalize_rew_api_snapshot(
        revision,
        'point-mlp',
        snapshot,
        evidence_type='measured',
        validation_scope='owned_room',
        validation_campaign_id='campaign-1',
        captured_timezone=timezone(timedelta(hours=9)),
    )

    provenance = __import__('json').loads(record.provenance_json)
    assert record.captured_at == '2026-09-18T12:11:10+09:00'
    assert provenance['captured_at_raw'] == '2026-Sep-18 12:11:10'
    assert provenance['captured_at_source'] == 'host_local_timezone'
    assert provenance['validation_scope'] == 'owned_room'
    assert provenance['validation_campaign_id'] == 'campaign-1'


def test_rew_owned_room_import_requires_campaign_id(tmp_path: Path) -> None:
    _, revision = _saved_f1(tmp_path)
    decoded = RewFrequencyResponse(
        measurement_id='rew-campaign-uuid',
        unit='SPL',
        smoothing=None,
        start_frequency_hz=20.0,
        points_per_octave=None,
        frequency_step_hz=20.0,
        frequency_hz=(20.0, 40.0, 60.0),
        magnitude=(70.0, 71.0, 69.5),
        phase_deg=None,
        requested_unit='SPL',
        requested_ppo=None,
        requested_smoothing=None,
    )
    snapshot = RewFrequencyResponseSnapshot(
        measurement_summary={'uuid': 'rew-campaign-uuid', 'date': '2026-09-18T12:11:10+09:00'},
        query={'unit': 'SPL'},
        raw_frequency_response={'unit': 'SPL', 'startFreq': 20.0, 'freqStep': 20.0},
        decoded=decoded,
    )

    with pytest.raises(ValueError, match='validation_campaign_id'):
        normalize_rew_api_snapshot(
            revision,
            'point-mlp',
            snapshot,
            evidence_type='measured',
            validation_scope='owned_room',
        )

def test_saved_comparison_keeps_exact_dataset_and_scene_revision_ids(tmp_path: Path) -> None:
    scene_repository, revision_a = _saved_f1(tmp_path)
    repository = CadMeasurementRepository(scene_repository)
    record_a, dataset_a, filename_a, raw_a = normalize_rew_text(
        revision_a,
        'point-mlp',
        b'20 70\n40 71\n80 69\n',
        filename='a.txt',
        imported_at='2026-09-17T09:30:00+00:00',
    )
    repository.save(record_a, dataset_a, raw_filename=filename_a, raw_bytes=raw_a)

    working = WorkingDocument(
        revision_a.document,
        source_revision_id=revision_a.revision_id,
        saved_content_hash=revision_a.content_hash,
    )
    working.move_entity('speaker-fl', Position3(x_m=1.55, y_m=0.75, z_m=1.05))
    revision_b = scene_repository.save(working.committed_document, parent_revision_id=revision_a.revision_id).revision
    record_b, dataset_b, filename_b, raw_b = normalize_rew_text(
        revision_b,
        'point-mlp',
        b'20 69\n40 70\n80 68\n',
        filename='b.txt',
        imported_at='2026-09-17T09:31:00+00:00',
    )
    repository.save(record_b, dataset_b, raw_filename=filename_b, raw_bytes=raw_b)

    result = compare_frequency_responses(
        FrequencyResponse(dataset_a.frequency_hz, dataset_a.level_db),
        FrequencyResponse(dataset_b.frequency_hz, dataset_b.level_db),
        20.0,
        80.0,
    )
    saved = repository.save_comparison(dataset_a.dataset_id, dataset_b.dataset_id, result)
    reopened = repository.get_comparison(saved.comparison_id)

    assert reopened == saved
    assert reopened.dataset_a_id == dataset_a.dataset_id
    assert reopened.dataset_b_id == dataset_b.dataset_id
    assert reopened.scene_revision_a_id == revision_a.revision_id
    assert reopened.scene_revision_b_id == revision_b.revision_id
    assert reopened.difference_db == saved.difference_db
    assert repository.list_comparisons(revision_a.document_id) == (saved,)


def test_job_guard_rejects_cancelled_superseded_and_stale_results(tmp_path: Path) -> None:
    _, revision = _saved_f1(tmp_path)
    measurement = measurement_record_for_revision(
        revision,
        'point-mlp',
        source_kind='rew_api',
        external_source_id='rew-1',
        imported_at='2026-09-17T09:30:00+00:00',
    )
    guard = MeasurementJobGuard()
    active_a = MeasurementJobApplyContext(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
    )

    first = guard.submit(measurement, external_source_id='rew-1', query={'unit': 'SPL'})
    assert guard.can_apply(first, active_a)

    second = guard.submit(measurement, external_source_id='rew-2', query={'unit': 'SPL', 'ppo': 96})
    assert not guard.can_apply(first, active_a)
    assert guard.can_apply(second, active_a)

    guard.cancel(second)
    assert guard.is_cancelled(second)
    assert not guard.can_apply(second, active_a)

    third = guard.submit(measurement, external_source_id='rew-3', query={'unit': 'SPL'})
    stale_revision = MeasurementJobApplyContext(
        document_id=revision.document_id,
        scene_revision_id='later-revision',
        scene_content_hash='f' * 64,
    )
    other_document = MeasurementJobApplyContext(
        document_id='other-document',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
    )
    assert not guard.can_apply(third, stale_revision)
    assert not guard.can_apply(third, other_document)
    assert guard.can_apply(third, active_a)


def test_job_guard_rejects_stale_constraint_workspace(tmp_path: Path) -> None:
    _, revision = _saved_f1(tmp_path)
    measurement = measurement_record_for_revision(
        revision,
        'point-mlp',
        source_kind='rew_api',
        external_source_id='rew-1',
        imported_at='2026-09-17T09:30:00+00:00',
    )
    guard = MeasurementJobGuard()
    token = guard.submit(
        measurement,
        external_source_id='rew-1',
        query={'unit': 'SPL'},
        constraint_workspace_hash='1' * 64,
    )

    matching = MeasurementJobApplyContext(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        constraint_workspace_hash='1' * 64,
    )
    stale_constraint = MeasurementJobApplyContext(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        constraint_workspace_hash='2' * 64,
    )
    unbound_context = MeasurementJobApplyContext(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
    )

    assert guard.can_apply(token, matching)
    assert not guard.can_apply(token, stale_constraint)
    # A constraint-bound token must not apply where the workspace is unverified.
    assert not guard.can_apply(token, unbound_context)
