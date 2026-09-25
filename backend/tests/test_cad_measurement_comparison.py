from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_measurement_models import (
    CadFrequencyResponseDataset,
    CadMeasurementComparison,
    _hash,
    replay_measurement_comparison,
)
from htdt.cad_measurement_quality import dataset_sha256
from htdt.cad_measurement_repository import CadMeasurementRepository, _pack
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.comparison import (
    ALGORITHM_VERSION,
    COMPARISON_ALGORITHM_SHA256,
    ComparisonResult,
    FrequencyResponse,
    compare_frequency_responses,
)


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    return revision, measurement_repository


def _save_dataset(
    repository: CadMeasurementRepository,
    revision,
    measurement_id: str,
    *,
    levels: tuple[float, ...] = (70.0, 71.0, 69.0, 72.0),
    frequencies: tuple[float, ...] = (20.0, 40.0, 80.0, 160.0),
):
    # The declared importer keeps fixture datasets honestly derived: the raw
    # asset literally states the samples the dataset persists.
    raw = declared_fr_raw(
        frequency_hz=frequencies,
        level_db=levels,
        phase_status='absent',
    )
    record = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id=measurement_id,
        evidence_type='measured',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        imported_at='2026-09-19T00:00:00+00:00',
        source_kind='unknown',
        external_source_id=f'rew-{measurement_id}',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=frequencies,
        level_db=levels,
        phase_status='absent',
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    repository.save(
        record,
        dataset,
        raw_filename=f'{measurement_id}.txt',
        raw_bytes=raw,
    )
    return record, dataset


def _canonical_result(
    dataset_a: CadFrequencyResponseDataset,
    dataset_b: CadFrequencyResponseDataset,
    **kwargs,
) -> ComparisonResult:
    return compare_frequency_responses(
        FrequencyResponse(dataset_a.frequency_hz, dataset_a.level_db),
        FrequencyResponse(dataset_b.frequency_hz, dataset_b.level_db),
        20.0,
        160.0,
        **kwargs,
    )


def _rehashed(comparison: CadMeasurementComparison) -> CadMeasurementComparison:
    """Recompute the seals so a tampered payload stays self-consistent."""
    provisional = comparison.model_copy(
        update={'spec_sha256': _hash(comparison.spec_identity_payload())}
    )
    return provisional.model_copy(
        update={'comparison_sha256': _hash(provisional.identity_payload())}
    )


def _rewrite_comparison_row(path: Path, comparison: CadMeasurementComparison) -> None:
    """Overwrite a persisted comparison row directly, bypassing save validation."""
    payload = comparison.model_dump(mode='json')
    for key in (
        'comparison_id',
        'document_id',
        'dataset_a_id',
        'dataset_b_id',
        'scene_revision_a_id',
        'scene_revision_b_id',
        'created_at',
    ):
        del payload[key]
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            'UPDATE cad_measurement_comparisons SET result_json=? WHERE comparison_id=?',
            (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(',', ':'),
                    allow_nan=False,
                ),
                comparison.comparison_id,
            ),
        )


def test_canonical_comparison_persists_and_reopens_with_verified_identity(
    tmp_path: Path,
) -> None:
    revision, repository = _repositories(tmp_path)
    _, dataset_a = _save_dataset(repository, revision, 'a')
    _, dataset_b = _save_dataset(
        repository, revision, 'b', levels=(69.0, 70.0, 68.0, 71.0)
    )
    result = _canonical_result(dataset_a, dataset_b, reference_band_hz=(40.0, 80.0))

    saved = repository.save_comparison(dataset_a.dataset_id, dataset_b.dataset_id, result)

    assert saved.algorithm_version == ALGORITHM_VERSION
    assert saved.algorithm_sha256 == COMPARISON_ALGORITHM_SHA256
    assert saved.dataset_a_sha256 == dataset_sha256(dataset_a)
    assert saved.dataset_b_sha256 == dataset_sha256(dataset_b)
    assert saved.reference_band_hz == (40.0, 80.0)
    assert saved.spec_sha256 == _hash(saved.spec_identity_payload())
    assert saved.comparison_sha256 == _hash(saved.identity_payload())
    assert saved.difference_db == result.difference_db

    reopened = repository.get_comparison(saved.comparison_id)
    assert reopened == saved
    assert repository.list_comparisons(revision.document_id) == (saved,)
    assert (
        replay_measurement_comparison(reopened, dataset_a=dataset_a, dataset_b=dataset_b)
        == reopened.comparison_result()
    )


def test_excluded_bands_round_trip_and_spec_is_bound(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    _, dataset_a = _save_dataset(repository, revision, 'a')
    _, dataset_b = _save_dataset(
        repository, revision, 'b', levels=(69.0, 70.0, 68.0, 71.0)
    )
    result = _canonical_result(dataset_a, dataset_b, excluded_bands=((60.0, 90.0),))

    saved = repository.save_comparison(dataset_a.dataset_id, dataset_b.dataset_id, result)

    assert saved.excluded_bands == ((60.0, 90.0),)
    assert repository.get_comparison(saved.comparison_id) == saved

    # The same arrays claiming a different spec are not canonical.
    with pytest.raises(ValueError, match='canonical algorithm output'):
        repository.save_comparison(
            dataset_a.dataset_id,
            dataset_b.dataset_id,
            replace(result, excluded_bands=()),
        )
    with pytest.raises(ValueError, match='canonical algorithm output'):
        repository.save_comparison(
            dataset_a.dataset_id,
            dataset_b.dataset_id,
            replace(result, reference_band_hz=(40.0, 80.0)),
        )


def test_fabricated_or_mismatched_comparison_values_are_rejected(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    _, dataset_a = _save_dataset(repository, revision, 'a')
    _, dataset_b = _save_dataset(
        repository, revision, 'b', levels=(69.0, 70.0, 68.0, 71.0)
    )
    _, dataset_c = _save_dataset(
        repository, revision, 'c', levels=(75.0, 75.0, 75.0, 75.0)
    )
    result = _canonical_result(dataset_a, dataset_b)
    dataset_ids = (dataset_a.dataset_id, dataset_b.dataset_id)

    fabricated = (
        replace(result, difference_db=tuple(v + 0.5 for v in result.difference_db)),
        replace(result, rms_difference_db=99.0),
        replace(result, mean_difference_db=-3.0),
        replace(result, level_offset_db=1.25),
        replace(result, shape_rms_db=0.0),
        replace(result, grid_hz=result.grid_hz[:-1]),
        replace(result, a_db=result.b_db, b_db=result.a_db),
        replace(result, actual_band_hz=(30.0, 160.0)),
        replace(result, requested_band_hz=(30.0, 160.0)),
        replace(result, valid_points=result.valid_points - 1),
        replace(result, total_grid_points=result.total_grid_points + 1),
        # A honestly computed result bound to different datasets is still not
        # canonical evidence for the (A, B) pair.
        _canonical_result(dataset_a, dataset_c),
        _canonical_result(dataset_b, dataset_a),
    )
    for forged in fabricated:
        with pytest.raises(ValueError, match='canonical algorithm output'):
            repository.save_comparison(*dataset_ids, forged)

    with pytest.raises(ValueError, match='algorithm version is not replayable'):
        repository.save_comparison(
            *dataset_ids,
            replace(result, algorithm_version='fr-compare-0'),
        )
    with pytest.raises(TypeError, match='ComparisonResult'):
        repository.save_comparison(*dataset_ids, asdict(result))

    # The honest result still persists after all rejections.
    saved = repository.save_comparison(*dataset_ids, result)
    assert repository.get_comparison(saved.comparison_id) == saved


def test_persisted_comparison_fails_closed_when_dataset_row_changes(
    tmp_path: Path,
) -> None:
    revision, repository = _repositories(tmp_path)
    _, dataset_a = _save_dataset(repository, revision, 'a')
    _, dataset_b = _save_dataset(
        repository, revision, 'b', levels=(69.0, 70.0, 68.0, 71.0)
    )
    saved = repository.save_comparison(
        dataset_a.dataset_id, dataset_b.dataset_id, _canonical_result(dataset_a, dataset_b)
    )
    assert repository.get_comparison(saved.comparison_id) == saved

    # A semantic change to a persisted dataset row invalidates the comparison
    # evidence bound to its hash instead of being silently reused. The
    # tampered row can no longer pass the dataset's own persisted semantic
    # hash, so the authoritative dataset read fails closed first.
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_frequency_responses SET level_blob=? WHERE dataset_id=?',
            (_pack((80.0, 81.0, 79.0, 82.0)), dataset_b.dataset_id),
        )

    with pytest.raises(ValueError, match='dataset semantic hash mismatch'):
        repository.get_comparison(saved.comparison_id)
    with pytest.raises(ValueError, match='dataset semantic hash mismatch'):
        repository.list_comparisons(revision.document_id)


def test_persisted_comparison_fails_closed_when_dataset_is_deleted(
    tmp_path: Path,
) -> None:
    revision, repository = _repositories(tmp_path)
    _, dataset_a = _save_dataset(repository, revision, 'a')
    _, dataset_b = _save_dataset(
        repository, revision, 'b', levels=(69.0, 70.0, 68.0, 71.0)
    )
    saved = repository.save_comparison(
        dataset_a.dataset_id, dataset_b.dataset_id, _canonical_result(dataset_a, dataset_b)
    )

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'DELETE FROM cad_frequency_responses WHERE dataset_id=?',
            (dataset_b.dataset_id,),
        )

    with pytest.raises(ValueError, match='unknown dataset'):
        repository.get_comparison(saved.comparison_id)
    with pytest.raises(ValueError, match='unknown dataset'):
        repository.list_comparisons(revision.document_id)


def test_rehashed_forged_payload_fails_closed_on_read(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    _, dataset_a = _save_dataset(repository, revision, 'a')
    _, dataset_b = _save_dataset(
        repository, revision, 'b', levels=(69.0, 70.0, 68.0, 71.0)
    )
    saved = repository.save_comparison(
        dataset_a.dataset_id, dataset_b.dataset_id, _canonical_result(dataset_a, dataset_b)
    )
    assert repository.get_comparison(saved.comparison_id) == saved

    # A coherently rehashed payload can keep every seal valid while upgrading
    # persisted arrays/metrics; reads must rerun the pinned algorithm instead
    # of trusting the payload.
    forged = _rehashed(
        saved.model_copy(
            update={
                'difference_db': tuple(v + 1.0 for v in saved.difference_db),
                'rms_difference_db': 42.0,
            }
        )
    )
    _rewrite_comparison_row(repository.path, forged)

    with pytest.raises(ValueError, match='canonical comparison algorithm output'):
        repository.get_comparison(saved.comparison_id)
    with pytest.raises(ValueError, match='canonical comparison algorithm output'):
        repository.list_comparisons(revision.document_id)

    _rewrite_comparison_row(repository.path, saved)
    assert repository.get_comparison(saved.comparison_id) == saved


def test_persisted_comparison_algorithm_identity_is_dispatched_on_read(
    tmp_path: Path,
) -> None:
    revision, repository = _repositories(tmp_path)
    _, dataset_a = _save_dataset(repository, revision, 'a')
    _, dataset_b = _save_dataset(
        repository, revision, 'b', levels=(69.0, 70.0, 68.0, 71.0)
    )
    saved = repository.save_comparison(
        dataset_a.dataset_id, dataset_b.dataset_id, _canonical_result(dataset_a, dataset_b)
    )

    # A valid persisted comparison replays through explicit version support.
    reopened = repository.get_comparison(saved.comparison_id)
    assert (
        replay_measurement_comparison(reopened, dataset_a=dataset_a, dataset_b=dataset_b)
        == reopened.comparison_result()
    )

    unknown_version = _rehashed(
        saved.model_copy(update={'algorithm_version': 'fr-compare-0'})
    )
    _rewrite_comparison_row(repository.path, unknown_version)
    with pytest.raises(ValueError, match='algorithm version is not replayable'):
        repository.get_comparison(saved.comparison_id)
    with pytest.raises(ValueError, match='algorithm version is not replayable'):
        repository.list_comparisons(revision.document_id)

    forged_algorithm_hash = _rehashed(
        saved.model_copy(update={'algorithm_sha256': 'f' * 64})
    )
    _rewrite_comparison_row(repository.path, forged_algorithm_hash)
    with pytest.raises(ValueError, match='algorithm hash mismatch'):
        repository.get_comparison(saved.comparison_id)
    with pytest.raises(ValueError, match='algorithm hash mismatch'):
        repository.list_comparisons(revision.document_id)

    _rewrite_comparison_row(repository.path, saved)
    assert repository.get_comparison(saved.comparison_id) == saved


def test_pre_identity_payload_fails_closed_on_read(tmp_path: Path) -> None:
    """Rows persisted before identity binding cannot verify and fail closed."""
    revision, repository = _repositories(tmp_path)
    _, dataset_a = _save_dataset(repository, revision, 'a')
    _, dataset_b = _save_dataset(
        repository, revision, 'b', levels=(69.0, 70.0, 68.0, 71.0)
    )
    saved = repository.save_comparison(
        dataset_a.dataset_id, dataset_b.dataset_id, _canonical_result(dataset_a, dataset_b)
    )

    # Rewrite result_json in the pre-identity format: the bare ComparisonResult
    # dump without dataset/spec/comparison seals.
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_measurement_comparisons SET result_json=? WHERE comparison_id=?',
            (
                json.dumps(
                    asdict(saved.comparison_result()),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(',', ':'),
                    allow_nan=False,
                ),
                saved.comparison_id,
            ),
        )

    with pytest.raises(ValueError):
        repository.get_comparison(saved.comparison_id)
    with pytest.raises(ValueError):
        repository.list_comparisons(revision.document_id)


# ---------------------------------------------------------------------
# #852 comparison semantic integrity


def _quality_repository(measurement_repository: CadMeasurementRepository):
    from htdt.cad_measurement_quality_repository import (
        CadMeasurementQualityRepository,
    )

    return CadMeasurementQualityRepository(measurement_repository)


def _level_calibration(
    quality_repository,
    *,
    method='acoustic_calibrator',
    acquisition_session_id=None,
):
    from htdt.cad_measurement_authorities import build_acoustic_level_calibration

    calibration = build_acoustic_level_calibration(
        method=method,
        reference_level_db_spl=94.0,
        reference_frequency_hz=1000.0,
        validity_scope=(
            'session' if acquisition_session_id is not None else 'unknown'
        ),
        acquisition_session_id=acquisition_session_id,
    )
    quality_repository.save_level_calibration(calibration)
    return calibration


def _session_context(quality_repository, session_id, *measurement_ids):
    from htdt.cad_measurement_quality import build_acquisition_context

    context = build_acquisition_context(
        source_kind='native',
        subject_measurement_ids=measurement_ids,
        acquisition_session_id=session_id,
    )
    quality_repository.save_acquisition_context(context)
    return context


def _bind_level_reference(
    quality_repository,
    dataset: CadFrequencyResponseDataset,
    kind: str,
    calibration=None,
) -> None:
    from htdt.cad_measurement_authorities import build_dataset_level_reference

    quality_repository.save_dataset_level_reference(
        build_dataset_level_reference(
            measurement_id=dataset.measurement_id,
            dataset_id=dataset.dataset_id,
            dataset_sha256=dataset.dataset_sha256,
            level_reference_kind=kind,
            calibration_id=(
                None if calibration is None else calibration.calibration_id
            ),
            calibration_sha256=(
                None if calibration is None else calibration.calibration_sha256
            ),
        )
    )


def _workflow_controller(
    revision, scene_repository, measurement_repository, quality_repository
):
    from htdt.measurement_workflow import MeasurementWorkflowController

    return MeasurementWorkflowController(
        scene_repository,
        revision.document_id,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )


def test_comparison_semantics_absolute_levels_comparable(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    quality_repository = _quality_repository(repository)
    _, dataset_a = _save_dataset(repository, revision, 'a')
    _, dataset_b = _save_dataset(
        repository, revision, 'b', levels=(69.0, 70.0, 68.0, 71.0)
    )
    _session_context(
        quality_repository,
        'session-1',
        dataset_a.measurement_id,
        dataset_b.measurement_id,
    )
    calibration = _level_calibration(
        quality_repository, acquisition_session_id='session-1'
    )
    _bind_level_reference(
        quality_repository, dataset_a, 'absolute_spl', calibration
    )
    _bind_level_reference(
        quality_repository, dataset_b, 'absolute_spl', calibration
    )

    controller = _workflow_controller(
        revision, repository.scene_repository, repository, quality_repository
    )
    semantics = controller.comparison_semantics(
        dataset_a.dataset_id, dataset_b.dataset_id
    )

    assert semantics is not None
    assert semantics.level_compatibility == 'absolute_level_comparable'
    assert semantics.absolute_level == 'available'
    assert semantics.normalized_shape == 'available'
    assert 'level_reference' not in semantics.mismatches
    assert semantics.label_a.startswith('MLP')
    assert 'front_left' in semantics.label_a
    assert 'measured' in semantics.label_b


def test_comparison_semantics_absolute_vs_relative_never_upgrades(
    tmp_path: Path,
) -> None:
    revision, repository = _repositories(tmp_path)
    quality_repository = _quality_repository(repository)
    _, dataset_a = _save_dataset(repository, revision, 'a')
    _, dataset_b = _save_dataset(
        repository, revision, 'b', levels=(69.0, 70.0, 68.0, 71.0)
    )
    _session_context(
        quality_repository,
        'session-1',
        dataset_a.measurement_id,
        dataset_b.measurement_id,
    )
    calibration = _level_calibration(
        quality_repository, acquisition_session_id='session-1'
    )
    _bind_level_reference(
        quality_repository, dataset_a, 'absolute_spl', calibration
    )
    _bind_level_reference(quality_repository, dataset_b, 'relative')

    controller = _workflow_controller(
        revision, repository.scene_repository, repository, quality_repository
    )

    # Without an explicit reference band the pair is diagnostic-only.
    semantics = controller.comparison_semantics(
        dataset_a.dataset_id, dataset_b.dataset_id
    )
    assert semantics is not None
    assert semantics.level_compatibility == 'diagnostic_only'
    assert semantics.absolute_level == 'unavailable'
    assert semantics.normalized_shape == 'unavailable'
    assert 'level_reference' in semantics.mismatches

    # Explicit reference-band normalization permits shape — never absolute.
    normalized = controller.comparison_semantics(
        dataset_a.dataset_id,
        dataset_b.dataset_id,
        reference_band_hz=(40.0, 80.0),
    )
    assert normalized is not None
    assert normalized.level_compatibility == 'normalized_shape_comparable'
    assert normalized.normalized_shape == 'available'
    assert normalized.absolute_level == 'unavailable'


def test_comparison_semantics_mismatch_axes_cover_routing_and_context(
    tmp_path: Path,
) -> None:
    revision, repository = _repositories(tmp_path)
    quality_repository = _quality_repository(repository)
    _, dataset_a = _save_dataset(repository, revision, 'a')
    record_b = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id='b',
        evidence_type='predicted',
        channel_role='front_right',
        source_speaker_ids=('speaker-fr',),
        radiation_scope='bass_managed',
        routing_evidence='inferred',
        imported_at='2026-09-19T00:00:00+00:00',
        source_kind='unknown',
        external_source_id='rew-b',
    )
    raw_b = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(69.0, 70.0, 68.0, 71.0),
        phase_status='absent',
    )
    dataset_b = CadFrequencyResponseDataset(
        dataset_id='dataset-b',
        measurement_id='b',
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(69.0, 70.0, 68.0, 71.0),
        phase_status='absent',
        source_sha256=sha256(raw_b).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    repository.save(record_b, dataset_b, raw_filename='b.txt', raw_bytes=raw_b)
    _bind_level_reference(quality_repository, dataset_a, 'spl_uncalibrated')
    _bind_level_reference(quality_repository, dataset_b, 'relative')

    controller = _workflow_controller(
        revision, repository.scene_repository, repository, quality_repository
    )
    codes = controller.comparison_mismatches(
        dataset_a.dataset_id, dataset_b.dataset_id
    )

    assert 'evidence_type' in codes
    assert 'channel_role' in codes
    assert 'source_speakers' in codes
    assert 'radiation_scope' in codes
    assert 'level_reference' in codes
    # Same scene revision and target — those axes stay quiet.
    assert 'scene_revision' not in codes
    assert 'target' not in codes


def test_compare_datasets_persists_semantics_for_history(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    quality_repository = _quality_repository(repository)
    _, dataset_a = _save_dataset(repository, revision, 'a')
    _, dataset_b = _save_dataset(
        repository, revision, 'b', levels=(69.0, 70.0, 68.0, 71.0)
    )
    _session_context(
        quality_repository,
        'session-1',
        dataset_a.measurement_id,
        dataset_b.measurement_id,
    )
    calibration = _level_calibration(
        quality_repository, acquisition_session_id='session-1'
    )
    _bind_level_reference(
        quality_repository, dataset_a, 'absolute_spl', calibration
    )
    _bind_level_reference(
        quality_repository, dataset_b, 'absolute_spl', calibration
    )

    controller = _workflow_controller(
        revision, repository.scene_repository, repository, quality_repository
    )
    saved = controller.compare_datasets(
        dataset_a.dataset_id,
        dataset_b.dataset_id,
        low_hz=20.0,
        high_hz=160.0,
    )

    assert saved.level_compatibility == 'absolute_level_comparable'
    assert saved.label_a is not None and 'front_left' in saved.label_a
    assert saved.semantics_json is not None
    from htdt.cad_comparison_semantics import ComparisonSemantics

    persisted = ComparisonSemantics.model_validate_json(saved.semantics_json)
    assert persisted.level_compatibility == 'absolute_level_comparable'
    assert persisted.side_a.dataset_id == dataset_a.dataset_id
    assert persisted.side_b.calibration_sha256 == calibration.calibration_sha256

    # History reopens the same record with the same semantic decisions.
    (reopened,) = controller.saved_comparisons()
    assert reopened == saved
    assert reopened.level_compatibility == 'absolute_level_comparable'

    # Mutating the persisted semantics invalidates the comparison seal:
    # the stored hash no longer covers the payload and reads fail closed.
    tampered = saved.model_copy(update={'level_compatibility': 'diagnostic_only'})
    _rewrite_comparison_row(repository.path, tampered)
    with pytest.raises(ValueError):
        repository.get_comparison(saved.comparison_id)
