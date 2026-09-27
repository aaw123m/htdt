from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
from typing import Callable
from uuid import uuid4
import zipfile


class MigrationOpenError(RuntimeError):
    """Opening an existing HTDT data directory could not be completed safely."""


@dataclass(frozen=True)
class MigrationPreparation:
    source_version: int
    target_version: int
    backup_path: Path


def _read_schema_version(db_path: Path) -> int | None:
    if not db_path.exists() or db_path.stat().st_size == 0:
        return None
    connection = sqlite3.connect(db_path)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
        ).fetchone()
        if table is None:
            raise MigrationOpenError('Existing database has no metadata table; refusing to modify it')
        row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if row is None:
            raise MigrationOpenError('Existing database has no schema_version; refusing to modify it')
        try:
            return int(row[0])
        except (TypeError, ValueError) as exc:
            raise MigrationOpenError('Existing database has an invalid schema_version') from exc
    finally:
        connection.close()


def _legacy_integrity_problems(root: Path, db_path: Path) -> list[str]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    problems: list[str] = []
    try:
        integrity = connection.execute('PRAGMA integrity_check').fetchone()
        if integrity is None or str(integrity[0]).lower() != 'ok':
            problems.append(f"sqlite_integrity:{integrity[0] if integrity else 'unknown'}")
        for row in connection.execute('PRAGMA foreign_key_check').fetchall():
            problems.append(f'foreign_key:{row[0]}:{row[1]}')
        assets_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets'"
        ).fetchone()
        if assets_table is not None:
            for row in connection.execute('SELECT sha256, relative_path FROM assets'):
                relative = Path(str(row['relative_path']))
                if relative.is_absolute() or '..' in relative.parts:
                    problems.append(f'unsafe_asset_path:{row["sha256"]}')
                elif not (root / relative).is_file():
                    problems.append(f'missing_asset:{row["sha256"]}:{relative.as_posix()}')
    finally:
        connection.close()
    return problems


def _backup_name(source_version: int, target_version: int) -> str:
    return f'pre-migration-v{source_version}-to-v{target_version}-{uuid4().hex[:12]}.zip'


def _create_pre_migration_backup(
    root: Path,
    db_path: Path,
    source_version: int,
    target_version: int,
) -> Path:
    problems = _legacy_integrity_problems(root, db_path)
    if problems:
        raise MigrationOpenError('Pre-migration integrity check failed: ' + '; '.join(problems))

    backups_dir = root / 'backups'
    backups_dir.mkdir(parents=True, exist_ok=True)
    archive_path = backups_dir / _backup_name(source_version, target_version)

    with tempfile.TemporaryDirectory(dir=root) as temp_dir_name:
        snapshot_db = Path(temp_dir_name) / 'htdt.sqlite3'
        source = sqlite3.connect(db_path)
        destination = sqlite3.connect(snapshot_db)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

        manifest = {
            'schema_version': source_version,
            'target_schema_version': target_version,
            'reason': 'pre_migration',
        }
        with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot_db, 'htdt.sqlite3')
            archive.writestr('manifest.json', json.dumps(manifest, sort_keys=True))
            archive.writestr('assets/', b'')
            assets_dir = root / 'assets'
            if assets_dir.is_dir():
                for asset in assets_dir.rglob('*'):
                    if asset.is_file():
                        archive.write(asset, f'assets/{asset.relative_to(assets_dir).as_posix()}')
    return archive_path


# Rollback archives are app-written but live in the data directory where a
# local process could swap them; bound what the restore materializes.
_MAX_ROLLBACK_MEMBERS = 4096
_MAX_ROLLBACK_EXPANDED_BYTES = 64 * 1024 * 1024 * 1024
_MAX_ROLLBACK_MANIFEST_BYTES = 1024 * 1024


def _validate_archive_member(member: zipfile.ZipInfo) -> None:
    member_path = Path(member.filename)
    if member_path.is_absolute() or '..' in member_path.parts:
        raise MigrationOpenError('Unsafe path in pre-migration backup')
    mode = (member.external_attr >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        raise MigrationOpenError('Pre-migration backup contains a symlink member')
    if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise MigrationOpenError('Pre-migration backup contains a special file member')


def _extract_member_bounded(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    staging: Path,
    remaining: list[int],
) -> None:
    """Stream one archive member to ``staging`` under a shared byte budget.

    ``ZipFile.extractall`` trusts each member's decoded stream; a swapped
    archive could materialize far beyond its declared sizes. Extraction
    therefore writes through ``archive.open`` with per-member and
    archive-wide actual-byte caps (mirrors ``native_backup``).
    """
    target = staging / member.filename
    if member.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with archive.open(member, 'r') as source, target.open('xb') as output:
            while chunk := source.read(1024 * 1024):
                written += len(chunk)
                if written > member.file_size or len(chunk) > remaining[0]:
                    raise MigrationOpenError(
                        'Pre-migration backup member exceeds its declared size'
                    )
                remaining[0] -= len(chunk)
                output.write(chunk)
    except MigrationOpenError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as exc:
        raise MigrationOpenError('Unreadable member in pre-migration backup') from exc
    if written != member.file_size:
        raise MigrationOpenError(
            'Pre-migration backup member decoded size mismatch'
        )


def _read_manifest_bounded(archive: zipfile.ZipFile, bound: int) -> bytes:
    """Stream manifest.json capped at ``bound`` bytes of actual output.

    ``archive.read`` materializes the member's real decompressed size,
    which a forged header understates; a bounded stream keeps a swapped
    archive from forcing an unbounded allocation.
    """

    try:
        member = archive.open('manifest.json', 'r')
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as exc:
        raise MigrationOpenError('Unreadable manifest in pre-migration backup') from exc
    with member:
        data = member.read(bound + 1)
    if len(data) > bound:
        raise MigrationOpenError('Pre-migration backup manifest is too large')
    return data


def _restore_pre_migration_backup(root: Path, archive_path: Path, expected_version: int) -> None:
    with tempfile.TemporaryDirectory(dir=root) as staging_name:
        staging = Path(staging_name)
        with zipfile.ZipFile(archive_path, 'r') as archive:
            members = archive.infolist()
            if len(members) > _MAX_ROLLBACK_MEMBERS:
                raise MigrationOpenError('Pre-migration backup has too many members')
            declared_total = 0
            seen_names: set[str] = set()
            for member in members:
                _validate_archive_member(member)
                if member.filename in seen_names:
                    raise MigrationOpenError('Pre-migration backup contains duplicate member names')
                seen_names.add(member.filename)
                declared_total += member.file_size
                if declared_total > _MAX_ROLLBACK_EXPANDED_BYTES:
                    raise MigrationOpenError('Pre-migration backup exceeds the expanded-size bound')
            names = set(archive.namelist())
            if 'manifest.json' not in names or 'htdt.sqlite3' not in names:
                raise MigrationOpenError('Pre-migration backup is incomplete')
            manifest = json.loads(
                _read_manifest_bounded(archive, _MAX_ROLLBACK_MANIFEST_BYTES)
            )
            if int(manifest.get('schema_version', -1)) != expected_version:
                raise MigrationOpenError('Pre-migration backup schema does not match rollback target')
            remaining = [_MAX_ROLLBACK_EXPANDED_BYTES]
            for member in members:
                _extract_member_bounded(archive, member, staging, remaining)

        restored_db = staging / 'htdt.sqlite3'
        restored_assets = staging / 'assets'
        if _read_schema_version(restored_db) != expected_version:
            raise MigrationOpenError('Pre-migration backup database failed schema validation')

        db_path = root / 'htdt.sqlite3'
        source = sqlite3.connect(restored_db)
        destination = sqlite3.connect(db_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        if _read_schema_version(db_path) != expected_version:
            raise MigrationOpenError('Rolled-back database failed schema validation')

        assets_dir = root / 'assets'
        replacement_assets = root / f'.assets.rollback-{uuid4().hex}'
        if restored_assets.exists():
            shutil.copytree(restored_assets, replacement_assets)
        else:
            replacement_assets.mkdir()
        shutil.rmtree(assets_dir, ignore_errors=True)
        if assets_dir.exists():
            shutil.rmtree(replacement_assets, ignore_errors=True)
            raise MigrationOpenError('Could not remove assets from failed migration')
        replacement_assets.rename(assets_dir)


def _prepare(root: Path, target_version: int) -> MigrationPreparation | None:
    db_path = root / 'htdt.sqlite3'
    source_version = _read_schema_version(db_path)
    if source_version is None:
        return None
    if source_version > target_version:
        raise MigrationOpenError(
            f'Data schema v{source_version} is newer than this application supports (v{target_version}); refusing downgrade'
        )
    if source_version == target_version:
        return None
    backup = _create_pre_migration_backup(root, db_path, source_version, target_version)
    return MigrationPreparation(source_version, target_version, backup)


def install_migration_guard() -> None:
    """Wrap Store construction so an older database is snapshotted and recoverable before migration."""
    from . import database

    store_type = database.Store
    if getattr(store_type, '_htdt_migration_guard_installed', False):
        return

    original_init: Callable[..., None] = store_type.__init__

    def guarded_init(self, root: Path) -> None:  # type: ignore[no-untyped-def]
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        preparation = _prepare(root, database.SCHEMA_VERSION)
        try:
            original_init(self, root)
            if preparation is not None:
                actual_version = _read_schema_version(self.db_path)
                if actual_version != preparation.target_version:
                    raise MigrationOpenError(
                        f'Migration ended at schema v{actual_version}, expected v{preparation.target_version}'
                    )
                problems = self.integrity_problems()
                if problems:
                    raise MigrationOpenError('Post-migration integrity check failed: ' + '; '.join(problems))
                self.pre_migration_backup = preparation.backup_path
                self.migrated_from_schema_version = preparation.source_version
        except Exception as exc:
            if preparation is None:
                raise
            try:
                _restore_pre_migration_backup(root, preparation.backup_path, preparation.source_version)
            except Exception as rollback_exc:
                raise MigrationOpenError(
                    f'Migration v{preparation.source_version}->v{preparation.target_version} failed and rollback also failed '
                    f'({rollback_exc}); pre-migration backup remains at {preparation.backup_path}'
                ) from rollback_exc
            raise MigrationOpenError(
                f'Migration v{preparation.source_version}->v{preparation.target_version} failed; '
                f'previous data restored from {preparation.backup_path}'
            ) from exc

    store_type.__init__ = guarded_init  # type: ignore[method-assign]
    store_type._htdt_migration_guard_installed = True
