"""Regression coverage for the round-17 Windows platform review.

Each test pins one verified round-17 fix:

- ``capture_bundle.DirectorySource`` trusted bundle scans rejected symlinked
  entries but followed Windows directory junctions (reparse points that
  ``Path.is_symlink`` does not report), so a junction inside the tree both
  enumerated and served files outside the bundle root.
- ``migration_guard._validate_archive_member`` relied on
  ``Path.is_absolute()`` + ``'..' in parts``; on Windows that misses
  root-relative ('/x', '\\\\x'), UNC and drive-relative ('C:x') member
  names, and ``staging / name`` then materializes outside staging.
- Managed-asset rows stored ``str(relative_to(...))`` verbatim, which is
  ``assets\\<sha>`` on Windows — the same logical row serialized two
  different ways depending on the host OS. Writes now store POSIX
  separators, matching every backup/archive reader that already
  normalizes ``\\`` to ``/``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import zipfile

import pytest

from htdt.cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from htdt.cad_repository import SceneRepository
from htdt.capture_bundle import (
    CaptureBundleError,
    DirectorySource,
    FrozenBundle,
)
from htdt.migration_guard import (
    MigrationOpenError,
    _restore_pre_migration_backup,
)


# --- helpers ---------------------------------------------------------------


def _make_junction(link: Path, target: Path) -> None:
    if os.name != 'nt':
        pytest.skip('directory junctions are Windows-specific')
    result = subprocess.run(
        ['cmd.exe', '/c', 'mklink', '/J', str(link), str(target)],
        capture_output=True,
    )
    if result.returncode != 0 or not link.exists():
        pytest.skip('directory junctions are not supported in this environment')


_V1_MANIFEST = {
    'schema_version': 1,
    'target_schema_version': 5,
    'reason': 'pre_migration',
}


def _v1_sqlite_bytes(tmp_path: Path) -> bytes:
    db_path = tmp_path / 'v1-source.sqlite3'
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            "CREATE TABLE metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO metadata VALUES ('schema_version', '1');"
        )
        connection.commit()
    finally:
        connection.close()
    return db_path.read_bytes()


def _rollback_archive(path: Path, db_bytes: bytes, member_name: str) -> Path:
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('htdt.sqlite3', db_bytes)
        archive.writestr('manifest.json', json.dumps(_V1_MANIFEST))
        archive.writestr('assets/', b'')
        info = zipfile.ZipInfo(member_name)
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        archive.writestr(info, b'escape-payload')
    return path


# --- capture bundle: junction escape -----------------------------------------


def test_capture_bundle_junction_dir_rejected(tmp_path: Path) -> None:
    root = tmp_path / 'bundle-dir'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.txt').write_bytes(b'out-of-root')
    (root / 'manifest.json').write_bytes(b'{}')
    _make_junction(root / 'junc', outside)

    with pytest.raises(CaptureBundleError, match='junction'):
        FrozenBundle(root)


def test_directory_source_list_files_rejects_junction(tmp_path: Path) -> None:
    root = tmp_path / 'bundle-dir'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.txt').write_bytes(b'out-of-root')
    _make_junction(root / 'junc', outside)

    with pytest.raises(CaptureBundleError, match='junction'):
        DirectorySource(root).list_files()


def test_directory_source_read_bytes_refuses_junction_escape(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'bundle-dir'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.txt').write_bytes(b'out-of-root')
    _make_junction(root / 'junc', outside)

    source = DirectorySource(root)
    with pytest.raises(CaptureBundleError, match='escapes bundle root'):
        source.read_bytes('junc/secret.txt')


# --- migration guard: rooted / drive-relative member names --------------------


@pytest.mark.parametrize(
    'member_name',
    ['/evil.txt', '\\evil.txt', 'c:\\evil.txt', 'C:relative.txt'],
)
def test_rollback_archive_rejects_windows_escape_member_names(
    tmp_path: Path, member_name: str
) -> None:
    archive = _rollback_archive(
        tmp_path / 'backup.zip', _v1_sqlite_bytes(tmp_path), member_name
    )
    root = tmp_path / 'data'
    root.mkdir(parents=True)
    with pytest.raises(MigrationOpenError, match='Unsafe path'):
        _restore_pre_migration_backup(root, archive, 1)
    assert not (root / 'htdt.sqlite3').exists()


def test_rollback_member_validator_rejects_backslash_names() -> None:
    # Hostile archives produced by non-Python writers (Explorer/WinRAR/.NET)
    # carry literal backslash member names; zipfile.writestr normalizes them
    # on write, so the rule is pinned at validator level.
    from htdt.migration_guard import _validate_archive_member

    for member_name in ('a\\b.txt', 'x\\..\\y.txt', '..\\evil.txt'):
        # ZipInfo.__init__ also normalizes '\\' to '/', so the hostile name
        # is assigned directly to keep the raw archive byte form.
        info = zipfile.ZipInfo('member')
        info.filename = member_name
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        with pytest.raises(MigrationOpenError, match='Unsafe path'):
            _validate_archive_member(info)


def test_rollback_archive_accepts_plain_member_names(tmp_path: Path) -> None:
    archive = _rollback_archive(
        tmp_path / 'backup.zip', _v1_sqlite_bytes(tmp_path), 'assets/' + 'ab' * 32
    )
    root = tmp_path / 'data'
    root.mkdir(parents=True)
    # The member is safe; restore may still fail later for unrelated
    # reasons, but the member name itself must not be refused.
    try:
        _restore_pre_migration_backup(root, archive, 1)
    except MigrationOpenError as exc:
        assert 'Unsafe path' not in str(exc)


# --- managed assets: platform-stable relative_path ----------------------------


def test_managed_asset_relative_path_uses_posix_separators(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad-scenes.sqlite3')
    treatment_repository = CadAcousticTreatmentRepository(scene_repository)
    digest = treatment_repository.save_source_asset(
        filename='treatment.csv', data=b'freq,spl\n100,80\n'
    )

    connection = sqlite3.connect(scene_repository.path)
    try:
        row = connection.execute(
            'SELECT relative_path FROM cad_measurement_assets WHERE sha256=?',
            (digest,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    relative_path = row[0]
    assert '\\' not in relative_path
    assert '/' in relative_path
    assert (tmp_path / relative_path).is_file()
