"""Import-transformation verification authority — round 2 coverage.

``verify_imported_dataset`` is the fail-closed gate that re-runs the pinned
importer over the exact raw bytes and demands the rederived dataset equal
the persisted one. These tests pin the tamper-detection contract, the
canonical-payload strictness, the declared-FR field rules, and the REW
capture-timestamp normalization paths not covered in round 1.
"""

from __future__ import annotations

import json
from datetime import timezone, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    import_transformation_authority,
    import_transformation_sha256,
    normalize_rew_capture_timestamp,
    normalize_rew_text,
    verify_imported_dataset,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene


# --- normalize_rew_capture_timestamp -------------------------------------------

def test_capture_timestamp_missing_and_blank() -> None:
    assert normalize_rew_capture_timestamp(None) == (None, 'missing')
    assert normalize_rew_capture_timestamp('   ') == (None, 'missing')


def test_capture_timestamp_unparsed_garbage() -> None:
    assert normalize_rew_capture_timestamp('not-a-date') == (None, 'unparsed')
    assert normalize_rew_capture_timestamp('2026-13-99 99:99:99') == (None, 'unparsed')


def test_capture_timestamp_source_timezone_preserved() -> None:
    iso, source = normalize_rew_capture_timestamp('2026-09-26T10:00:00+09:00')
    assert source == 'source_timezone'
    assert '+09:00' in iso


def test_capture_timestamp_z_suffix_is_source_timezone() -> None:
    iso, source = normalize_rew_capture_timestamp('2026-09-26T10:00:00Z')
    assert source == 'source_timezone'
    assert iso.endswith('+00:00')


def test_capture_timestamp_naive_uses_host_timezone_arg() -> None:
    jst = timezone(timedelta(hours=9))
    iso, source = normalize_rew_capture_timestamp(
        '2026-09-26T10:00:00', host_timezone=jst
    )
    assert source == 'host_local_timezone'
    assert '+09:00' in iso


# --- verify_imported_dataset: fail-closed tamper detection ----------------------


def _saved_f1(tmp_path: Path):
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(make_f1_scene(), parent_revision_id=None).revision
    return revision


def test_verify_imported_dataset_accepts_canonical_declared(tmp_path: Path) -> None:
    revision = _saved_f1(tmp_path)
    raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.5, 69.0),
        phase_deg=(0.0, -5.0, -12.0),
        phase_status='valid',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id='ds-1',
        measurement_id='m-1',
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.5, 69.0),
        phase_deg=(0.0, -5.0, -12.0),
        phase_status='valid',
        level_reference='unknown',
        smoothing=None,
        processing_json=canonical_json({}),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    verify_imported_dataset(dataset, raw)  # must not raise


def test_verify_imported_dataset_rejects_tampered_samples(tmp_path: Path) -> None:
    raw = declared_fr_raw(frequency_hz=(20.0, 40.0), level_db=(70.0, 71.0))
    dataset = CadFrequencyResponseDataset(
        dataset_id='ds-1',
        measurement_id='m-1',
        frequency_hz=(20.0, 40.0),
        level_db=(70.0, 99.0),  # rewritten level
        phase_deg=None,
        phase_status='absent',
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    with pytest.raises(ValueError, match='canonical import transformation'):
        verify_imported_dataset(dataset, raw)


def test_verify_imported_dataset_rejects_unregistered_importer(tmp_path: Path) -> None:
    raw = declared_fr_raw(frequency_hz=(20.0, 40.0), level_db=(70.0, 71.0))
    dataset = CadFrequencyResponseDataset(
        dataset_id='ds-1',
        measurement_id='m-1',
        frequency_hz=(20.0, 40.0),
        level_db=(70.0, 71.0),
        phase_deg=None,
        phase_status='absent',
        source_sha256=sha256(raw).hexdigest(),
        importer_version='legacy-unregistered-v0',
    )
    with pytest.raises(ValueError, match='no registered import transformation'):
        verify_imported_dataset(dataset, raw)


def test_verify_imported_dataset_rejects_source_kind_mismatch(tmp_path: Path) -> None:
    raw = declared_fr_raw(frequency_hz=(20.0, 40.0), level_db=(70.0, 71.0))
    dataset = CadFrequencyResponseDataset(
        dataset_id='ds-1',
        measurement_id='m-1',
        frequency_hz=(20.0, 40.0),
        level_db=(70.0, 71.0),
        phase_deg=None,
        phase_status='absent',
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    # The declared importer authority has source_kind 'unknown'; claiming
    # 'rew_api' for the measurement record must fail closed.
    with pytest.raises(ValueError, match='source kind'):
        verify_imported_dataset(dataset, raw, source_kind='rew_api')


def test_verify_rew_text_round_trip_and_hash_pin(tmp_path: Path) -> None:
    revision = _saved_f1(tmp_path)
    raw = b'Frequency SPL\n20 70.0\n40 71.5\n80 69.0\n'
    record, dataset, _filename, source = normalize_rew_text(
        revision, 'point-mlp', raw, filename='mlp.txt'
    )
    verify_imported_dataset(dataset, source)
    # A dataset claiming this raw asset under a different importer fails.
    forged = dataset.model_copy(
        update={'importer_version': HTDT_DECLARED_IMPORTER_VERSION}
    )
    # Fail-closed: REW text cannot satisfy the declared-FR canonical decode.
    with pytest.raises(ValueError, match='not valid JSON'):
        verify_imported_dataset(forged, source)


def test_declared_rederive_rejects_noncanonical_json(tmp_path: Path) -> None:
    # Same content but pretty-printed → not the canonical form; fail closed.
    canonical = declared_fr_raw(frequency_hz=(20.0, 40.0), level_db=(70.0, 71.0))
    payload = json.loads(canonical.decode('utf-8'))
    pretty = json.dumps(payload, indent=2, sort_keys=True).encode('utf-8')
    dataset = CadFrequencyResponseDataset(
        dataset_id='ds-1',
        measurement_id='m-1',
        frequency_hz=(20.0, 40.0),
        level_db=(70.0, 71.0),
        phase_deg=None,
        phase_status='absent',
        source_sha256=sha256(pretty).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    with pytest.raises(ValueError, match='not in canonical form'):
        verify_imported_dataset(dataset, pretty)


def test_declared_rederive_rejects_extra_fields_and_bool_arrays(tmp_path: Path) -> None:
    payload = json.loads(
        declared_fr_raw(frequency_hz=(20.0, 40.0), level_db=(70.0, 71.0)).decode()
    )
    payload['extra'] = 'field'
    raw = canonical_json(payload).encode('utf-8')
    dataset = CadFrequencyResponseDataset(
        dataset_id='ds-1',
        measurement_id='m-1',
        frequency_hz=(20.0, 40.0),
        level_db=(70.0, 71.0),
        phase_deg=None,
        phase_status='absent',
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    with pytest.raises(ValueError, match='unexpected fields'):
        verify_imported_dataset(dataset, raw)

    # Booleans are not numbers in this contract.
    payload = json.loads(
        declared_fr_raw(frequency_hz=(20.0, 40.0), level_db=(70.0, 71.0)).decode()
    )
    payload['level_db'] = [True, 71.0]
    raw = canonical_json(payload).encode('utf-8')
    dataset = dataset.model_copy(update={'source_sha256': sha256(raw).hexdigest()})
    with pytest.raises(ValueError, match='only numbers'):
        verify_imported_dataset(dataset, raw)


def test_import_transformation_authority_unknown_version_fails() -> None:
    with pytest.raises(ValueError, match='no registered'):
        import_transformation_authority('does-not-exist')


def test_import_transformation_sha256_is_deterministic_and_input_sensitive() -> None:
    a = import_transformation_sha256(
        source_sha256='a' * 64,
        importer_version='v1',
        dataset_sha256='d' * 64,
    )
    assert a == import_transformation_sha256(
        source_sha256='a' * 64, importer_version='v1', dataset_sha256='d' * 64
    )
    assert a != import_transformation_sha256(
        source_sha256='b' * 64, importer_version='v1', dataset_sha256='d' * 64
    )
    assert len(a) == 64
