"""REV19-DEBT1 regression tests: small concrete deferred items.

- Persisted managed-asset ``relative_path`` values are POSIX-shaped on every
  platform (write boundary normalizes), and legacy backslash rows still
  resolve on read.
- ``BackupError`` subclasses ``ValueError``, is raised on backup-path
  failures, and maps to the backup-specific operator message.
- ``saved_label`` renders through ``localization.format_datetime``.
- Naive persisted timestamps are read as honest UTC, not host-local time.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
import sqlite3

import pytest

from htdt.cad_assumption_decision_repository import (
    _instant as _assumption_instant,
)
from htdt.cad_design_brief_repository import (
    _instant as _brief_instant,
)
from htdt.cad_display_labels import saved_label
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import normalize_rew_text
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.managed_assets import managed_asset_path, safe_managed_relative_path
from htdt.native_backup import BackupError, create_backup
from htdt.user_facing_error import to_user_facing_error


RAW = b'Frequency SPL\n20 70.0\n40 71.5\n80 69.0\n'


def _measurement_repo(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad-scenes.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    repository = CadMeasurementRepository(scene_repository)
    record, dataset, filename, source = normalize_rew_text(
        revision,
        'point-mlp',
        RAW,
        filename='measurement.txt',
        evidence_type='measured',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        routing_evidence='verified',
        imported_at='2026-09-17T09:30:00+00:00',
    )
    repository.save(record, dataset, raw_filename=filename, raw_bytes=source)
    return repository, dataset.source_sha256, source


def test_persisted_asset_relative_path_is_posix_shaped(tmp_path: Path) -> None:
    """The write boundary stores POSIX separators so a project moved between
    platforms resolves identically — never the host ``os.sep``."""
    repository, digest, _ = _measurement_repo(tmp_path)
    with closing(sqlite3.connect(repository.path)) as connection:
        (relative_path,) = connection.execute(
            'SELECT relative_path FROM cad_measurement_assets'
        ).fetchone()
    assert relative_path == f'measurement-assets/{digest}'
    assert '\\' not in relative_path
    assert PurePosixPath(relative_path).as_posix() == relative_path


def test_managed_asset_path_accepts_legacy_backslash_row(tmp_path: Path) -> None:
    """A row written with Windows separators by an older build still resolves
    to the same managed asset on read."""
    data_dir = tmp_path / 'data'
    digest = 'ab' * 32
    asset = data_dir / 'measurement-assets' / digest
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b'payload')
    legacy = 'measurement-assets\\' + digest
    assert safe_managed_relative_path(legacy) == PurePosixPath(
        'measurement-assets/' + digest
    )
    assert managed_asset_path(data_dir, legacy) == asset


def test_backup_error_is_raised_for_backup_path_failures(
    tmp_path: Path,
) -> None:
    """Corrupt backup manifests surface as the dedicated BackupError subclass
    (which stays a ValueError for existing handlers)."""
    assert issubclass(BackupError, ValueError)
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    repository, digest, _ = _measurement_repo(data_dir)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_measurement_assets SET relative_path=?',
            (f'measurement-assets/{digest}/../escape',),
        )
    with pytest.raises(BackupError):
        create_backup(data_dir, tmp_path / 'out.htdt-backup')


def test_backup_manifest_asset_failure_escapes_as_backup_error(
    tmp_path: Path,
) -> None:
    """verify_managed_asset failures inside the backup contract surface as
    BackupError, not the runtime ManagedAssetError."""
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    repository, digest, _ = _measurement_repo(data_dir)
    (repository.assets_dir / digest).write_bytes(b'tampered payload')
    with pytest.raises(BackupError, match=digest[:12]):
        create_backup(data_dir, tmp_path / 'out.htdt-backup')


def test_backup_error_maps_to_backup_specific_message() -> None:
    error = to_user_facing_error(
        BackupError('backup manifest identity hash mismatch'),
        title='バックアップを検証できませんでした',
    )
    assert error.code == 'backup.invalid'
    assert error.message == 'バックアップデータを処理できませんでした'


def test_saved_label_uses_locale_datetime_format() -> None:
    assert saved_label('2026-09-24T18:42:31+00:00') == (
        '2026年9月24日 18:42 UTC の保存'
    )
    assert saved_label('2026-09-24T18:42:31Z') == '2026年9月24日 18:42 UTC の保存'
    assert saved_label('not-a-timestamp') == 'not-a-timestamp'


@pytest.mark.parametrize(
    'instant',
    [_assumption_instant, _brief_instant],
    ids=['assumption_decision', 'design_brief'],
)
def test_instant_reads_naive_persisted_timestamp_as_utc(instant) -> None:
    """A naive (markerless) persisted timestamp is the UTC instant the writer
    meant — never reinterpreted as host-local wall time."""
    assert instant('2026-01-05T12:00:00') == datetime(
        2026, 1, 5, 12, 0, tzinfo=timezone.utc
    )
    assert instant('2026-01-05T12:00:00Z') == datetime(
        2026, 1, 5, 12, 0, tzinfo=timezone.utc
    )
