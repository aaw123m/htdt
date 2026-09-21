from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
from typing import Any, Literal
from uuid import uuid4
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__
from .cad_schema import NativeSchemaError, check_native_schema_compatibility
from .limits import (
    MAX_NATIVE_BACKUP_ARCHIVE_BYTES,
    MAX_NATIVE_BACKUP_COMPRESSION_RATIO,
    MAX_NATIVE_BACKUP_EXPANDED_BYTES,
    MAX_NATIVE_BACKUP_MANIFEST_BYTES,
    MAX_NATIVE_BACKUP_MEMBER_BYTES,
    MAX_NATIVE_BACKUP_MEMBERS,
)


BACKUP_SCHEMA_VERSION = 1
DATABASE_NAME = 'cad-scenes.sqlite3'
MANIFEST_NAME = 'manifest.json'


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _sha256_bytes(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BackupFileEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1)
    kind: Literal['database', 'measurement_asset']
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class BackupManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = BACKUP_SCHEMA_VERSION
    application_version: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    files: tuple[BackupFileEntry, ...] = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_manifest(self) -> 'BackupManifest':
        paths = [entry.path for entry in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError('backup manifest file paths must be unique')
        databases = [entry for entry in self.files if entry.kind == 'database']
        if len(databases) != 1 or databases[0].path != DATABASE_NAME:
            raise ValueError('backup manifest must contain exactly one native database')
        for entry in self.files:
            _safe_archive_path(entry.path)
        if self.manifest_sha256 != _manifest_hash(self.identity_payload()):
            raise ValueError('backup manifest identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'application_version': self.application_version,
            'created_at_utc': self.created_at_utc,
            'files': [entry.model_dump(mode='json') for entry in self.files],
        }


def _manifest_hash(payload: dict[str, Any]) -> str:
    return _sha256_bytes(_canonical_json(payload).encode('utf-8'))


def _safe_archive_path(value: str) -> PurePosixPath:
    if '\\' in value:
        raise ValueError(f'backup path must use POSIX separators: {value}')
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {'', '.', '..'} for part in path.parts):
        raise ValueError(f'unsafe backup archive path: {value}')
    if ':' in path.parts[0]:
        raise ValueError(f'unsafe backup archive path: {value}')
    return path


def _safe_data_path(data_dir: Path, relative_path: str) -> Path:
    archive_path = _safe_archive_path(relative_path)
    target = data_dir.joinpath(*archive_path.parts)
    root = data_dir.resolve()
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f'backup file escapes native data root: {relative_path}') from exc
    return target


def _canonical_data_path(path: Path) -> Path:
    """Canonical absolute path for managed-data identity comparisons.

    resolve() follows symlinks and junctions and collapses dot segments even
    for missing leaves; normcase() additionally folds case on case-insensitive
    filesystems (Windows), so differently-spelled aliases compare equal.
    """
    return Path(os.path.normcase(str(path.expanduser().resolve())))


def _same_file(first: Path, second: Path) -> bool:
    """True when both paths identify the same existing filesystem object.

    os.path.samefile() also matches hard-link aliases that path resolution
    cannot detect; it raises OSError when either side does not exist.
    """
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _assert_safe_backup_destination(data_dir: Path, destination: Path) -> None:
    """Reject a backup destination that overlaps live managed data.

    create_backup() finishes with os.replace() at ``destination``, so a
    destination resolving to the live database or into the managed
    measurement-assets subtree would silently overwrite live data after
    validation succeeds. All comparisons run on canonical paths before any
    output is created.
    """
    source_database = _canonical_data_path(data_dir / DATABASE_NAME)
    if destination == source_database or _same_file(destination, source_database):
        raise ValueError(
            f'backup destination overlaps the live native database: {destination}'
        )
    assets_root = _canonical_data_path(data_dir / 'measurement-assets')
    if destination.is_relative_to(assets_root):
        raise ValueError(
            'backup destination is inside the managed measurement-assets '
            f'directory: {destination}'
        )
    if destination.is_dir():
        raise ValueError(f'backup destination is a directory: {destination}')
    if not source_database.is_file():
        return
    try:
        asset_rows = _asset_rows(source_database)
    except sqlite3.DatabaseError as exc:
        raise ValueError(f'native backup database is invalid: {exc}') from exc
    for _digest, relative_path, _size_bytes in asset_rows:
        asset_path = _canonical_data_path(_safe_data_path(data_dir, relative_path))
        if destination == asset_path or _same_file(destination, asset_path):
            raise ValueError(
                'backup destination overlaps a managed measurement asset: '
                f'{relative_path}'
            )


def _sqlite_health(path: Path) -> None:
    if not path.is_file():
        raise ValueError('native backup database is missing')
    try:
        with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)) as connection:
            integrity = connection.execute('PRAGMA integrity_check').fetchall()
            if integrity != [('ok',)]:
                raise ValueError(f'SQLite integrity check failed: {integrity!r}')
            foreign_keys = connection.execute('PRAGMA foreign_key_check').fetchall()
            if foreign_keys:
                raise ValueError(f'SQLite foreign-key check failed: {foreign_keys!r}')
        try:
            check_native_schema_compatibility(path)
        except NativeSchemaError as exc:
            raise ValueError(f'native backup database schema is incompatible: {exc}') from exc
    except sqlite3.DatabaseError as exc:
        raise ValueError(f'native backup database is invalid: {exc}') from exc


def _asset_rows(database_path: Path) -> tuple[tuple[str, str, int], ...]:
    with closing(sqlite3.connect(f'file:{database_path.as_posix()}?mode=ro', uri=True)) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cad_measurement_assets'"
        ).fetchone()
        if table is None:
            return ()
        rows = connection.execute(
            'SELECT sha256, relative_path, size_bytes FROM cad_measurement_assets ORDER BY sha256'
        ).fetchall()
    normalized: list[tuple[str, str, int]] = []
    for row in rows:
        relative_path = str(row[1]).replace('\\', '/')
        _safe_archive_path(relative_path)
        normalized.append((str(row[0]), relative_path, int(row[2])))
    return tuple(normalized)


def _validate_asset_contract(
    *,
    data_dir: Path,
    database_path: Path,
    manifest: BackupManifest | None = None,
) -> None:
    asset_rows = _asset_rows(database_path)
    manifest_assets = (
        {}
        if manifest is None
        else {
            entry.path: entry
            for entry in manifest.files
            if entry.kind == 'measurement_asset'
        }
    )
    seen_paths: set[str] = set()
    for digest, relative_path, size_bytes in asset_rows:
        if len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest):
            raise ValueError(f'invalid measurement asset digest in database: {digest}')
        _safe_archive_path(relative_path)
        if relative_path in seen_paths:
            raise ValueError(f'duplicate measurement asset path in database: {relative_path}')
        seen_paths.add(relative_path)
        asset_path = _safe_data_path(data_dir, relative_path)
        if asset_path.is_symlink() or not asset_path.is_file():
            raise ValueError(f'measurement asset is missing or not a regular file: {relative_path}')
        actual_size = asset_path.stat().st_size
        if actual_size != size_bytes:
            raise ValueError(f'measurement asset size mismatch: {relative_path}')
        actual_hash = _sha256_file(asset_path)
        if actual_hash != digest:
            raise ValueError(f'measurement asset SHA-256 mismatch: {relative_path}')
        if manifest is not None:
            entry = manifest_assets.get(relative_path)
            if entry is None:
                raise ValueError(f'measurement asset missing from backup manifest: {relative_path}')
            if entry.sha256 != digest or entry.size_bytes != size_bytes:
                raise ValueError(f'measurement asset manifest mismatch: {relative_path}')
    if manifest is not None and set(manifest_assets) != seen_paths:
        extras = sorted(set(manifest_assets) - seen_paths)
        raise ValueError(f'backup manifest contains unreferenced measurement assets: {extras}')


def _snapshot_database(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file():
        raise FileNotFoundError(f'native database does not exist: {source_path}')
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(destination_path)) as destination:
            source.backup(destination)
            destination.commit()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f'could not create consistent SQLite backup: {exc}') from exc
    _sqlite_health(destination_path)


def _build_manifest(snapshot_root: Path, database_path: Path) -> BackupManifest:
    entries: list[BackupFileEntry] = [
        BackupFileEntry(
            path=DATABASE_NAME,
            kind='database',
            size_bytes=database_path.stat().st_size,
            sha256=_sha256_file(database_path),
        )
    ]
    for digest, relative_path, size_bytes in _asset_rows(database_path):
        asset_path = _safe_data_path(snapshot_root, relative_path)
        if asset_path.is_symlink() or not asset_path.is_file():
            raise ValueError(f'measurement asset is missing or not a regular file: {relative_path}')
        if asset_path.stat().st_size != size_bytes:
            raise ValueError(f'measurement asset size mismatch: {relative_path}')
        if _sha256_file(asset_path) != digest:
            raise ValueError(f'measurement asset SHA-256 mismatch: {relative_path}')
        entries.append(BackupFileEntry(
            path=relative_path,
            kind='measurement_asset',
            size_bytes=size_bytes,
            sha256=digest,
        ))
    payload = {
        'schema_version': BACKUP_SCHEMA_VERSION,
        'application_version': __version__,
        'created_at_utc': _utc_now(),
        'files': [entry.model_dump(mode='json') for entry in entries],
    }
    return BackupManifest(
        **payload,
        manifest_sha256=_manifest_hash(payload),
    )


def create_backup(data_dir: Path, destination: Path) -> BackupManifest:
    """Create an atomic native-data backup without copying a live SQLite file directly."""

    data_dir = _canonical_data_path(Path(data_dir))
    destination = _canonical_data_path(Path(destination))
    _assert_safe_backup_destination(data_dir, destination)
    source_database = data_dir / DATABASE_NAME
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix='htdt-backup-', dir=destination.parent) as temp_name:
        temp_root = Path(temp_name)
        snapshot_root = temp_root / 'snapshot'
        snapshot_root.mkdir()
        snapshot_database = snapshot_root / DATABASE_NAME
        _snapshot_database(source_database, snapshot_database)

        for _digest, relative_path, _size_bytes in _asset_rows(snapshot_database):
            source_asset = _safe_data_path(data_dir, relative_path)
            target_asset = _safe_data_path(snapshot_root, relative_path)
            if source_asset.is_symlink() or not source_asset.is_file():
                raise ValueError(f'measurement asset is missing or not a regular file: {relative_path}')
            target_asset.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_asset, target_asset)

        _validate_asset_contract(
            data_dir=snapshot_root,
            database_path=snapshot_database,
        )
        manifest = _build_manifest(snapshot_root, snapshot_database)

        archive_temp = temp_root / 'backup.tmp'
        with ZipFile(archive_temp, 'w', compression=ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr(
                MANIFEST_NAME,
                _canonical_json(manifest.model_dump(mode='json')).encode('utf-8'),
            )
            for entry in manifest.files:
                archive.write(_safe_data_path(snapshot_root, entry.path), arcname=entry.path)

        validate_backup(archive_temp)
        os.replace(archive_temp, destination)
    return manifest


def _zip_entries(archive: ZipFile) -> dict[str, ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_NATIVE_BACKUP_MEMBERS:
        raise ValueError(
            f'backup archive has too many members: {len(infos)} '
            f'(limit={MAX_NATIVE_BACKUP_MEMBERS})'
        )
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise ValueError('backup archive contains duplicate member names')

    expanded_total = 0
    entries: dict[str, ZipInfo] = {}
    for info in infos:
        _safe_archive_path(info.filename)
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise ValueError(f'backup archive contains a symlink: {info.filename}')
        if info.is_dir():
            raise ValueError(f'backup archive contains an unexpected directory entry: {info.filename}')
        if info.file_size > MAX_NATIVE_BACKUP_MEMBER_BYTES:
            raise ValueError(
                f'backup member is too large: {info.filename} '
                f'({info.file_size} bytes)'
            )
        expanded_total += info.file_size
        if expanded_total > MAX_NATIVE_BACKUP_EXPANDED_BYTES:
            raise ValueError(
                'backup expanded size exceeds limit: '
                f'{expanded_total} > {MAX_NATIVE_BACKUP_EXPANDED_BYTES}'
            )
        if (
            info.file_size >= 1024 * 1024
            and info.file_size / max(1, info.compress_size)
            > MAX_NATIVE_BACKUP_COMPRESSION_RATIO
        ):
            raise ValueError(
                f'backup member compression ratio is excessive: {info.filename}'
            )
        entries[info.filename] = info
    return entries


def _read_manifest(archive: ZipFile, entries: dict[str, ZipInfo]) -> BackupManifest:
    info = entries.get(MANIFEST_NAME)
    if info is None:
        raise ValueError('backup manifest is missing')
    if info.file_size > MAX_NATIVE_BACKUP_MANIFEST_BYTES:
        raise ValueError('backup manifest exceeds size limit')
    try:
        with archive.open(info, 'r') as source:
            payload = source.read(MAX_NATIVE_BACKUP_MANIFEST_BYTES + 1)
        if len(payload) > MAX_NATIVE_BACKUP_MANIFEST_BYTES:
            raise ValueError('backup manifest exceeds size limit')
        return BackupManifest.model_validate_json(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f'backup manifest is invalid: {exc}') from exc


def _extract_verified_member(
    archive: ZipFile,
    info: ZipInfo,
    entry: BackupFileEntry,
    target: Path,
) -> None:
    digest = sha256()
    written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info, 'r') as source, target.open('xb') as output:
        while chunk := source.read(1024 * 1024):
            written += len(chunk)
            if written > entry.size_bytes:
                raise ValueError(f'backup member decoded size mismatch: {entry.path}')
            digest.update(chunk)
            output.write(chunk)
    if written != entry.size_bytes:
        raise ValueError(f'backup member decoded size mismatch: {entry.path}')
    if digest.hexdigest() != entry.sha256:
        raise ValueError(f'backup member SHA-256 mismatch: {entry.path}')


def _stage_backup(backup_path: Path, stage_root: Path) -> BackupManifest:
    if backup_path.stat().st_size > MAX_NATIVE_BACKUP_ARCHIVE_BYTES:
        raise ValueError(
            'backup archive exceeds size limit: '
            f'{backup_path.stat().st_size} > {MAX_NATIVE_BACKUP_ARCHIVE_BYTES}'
        )
    try:
        with ZipFile(backup_path, 'r') as archive:
            entries = _zip_entries(archive)
            manifest = _read_manifest(archive, entries)
            expected = {MANIFEST_NAME, *(entry.path for entry in manifest.files)}
            actual = set(entries)
            if actual != expected:
                raise ValueError(
                    f'backup archive members do not match manifest: '
                    f'missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}'
                )

            for entry in manifest.files:
                info = entries[entry.path]
                if info.file_size != entry.size_bytes:
                    raise ValueError(f'backup member size mismatch: {entry.path}')
                target = _safe_data_path(stage_root, entry.path)
                _extract_verified_member(archive, info, entry, target)
    except BadZipFile as exc:
        raise ValueError(f'backup archive is not a valid ZIP container: {exc}') from exc

    database_path = stage_root / DATABASE_NAME
    _sqlite_health(database_path)
    _validate_asset_contract(
        data_dir=stage_root,
        database_path=database_path,
        manifest=manifest,
    )
    return manifest


def validate_backup(backup_path: Path) -> BackupManifest:
    """Fully validate an archive, including SQLite integrity and raw-asset hashes."""

    backup_path = Path(backup_path)
    if not backup_path.is_file():
        raise FileNotFoundError(f'backup archive does not exist: {backup_path}')
    with tempfile.TemporaryDirectory(prefix='htdt-backup-validate-') as temp_name:
        return _stage_backup(backup_path, Path(temp_name))


def _remove_managed_data(data_dir: Path) -> None:
    database = data_dir / DATABASE_NAME
    if database.exists():
        database.unlink()
    assets = data_dir / 'measurement-assets'
    if assets.exists():
        shutil.rmtree(assets)


def restore_backup(
    data_dir: Path,
    backup_path: Path,
    *,
    pre_restore_backup: Path | None = None,
) -> tuple[BackupManifest, Path | None]:
    """Restore validated managed native data with rollback if the live swap fails."""

    data_dir = Path(data_dir)
    backup_path = Path(backup_path)
    parent = data_dir.parent
    parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix='htdt-restore-stage-', dir=parent) as stage_name:
        stage_root = Path(stage_name)
        manifest = _stage_backup(backup_path, stage_root)

        existing_database = data_dir / DATABASE_NAME
        pre_backup: Path | None = None
        if existing_database.is_file():
            pre_backup = (
                Path(pre_restore_backup)
                if pre_restore_backup is not None
                else parent / (
                    f'{data_dir.name}-pre-restore-'
                    f'{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}-'
                    f'{uuid4().hex[:8]}.htdt-backup'
                )
            )
            create_backup(data_dir, pre_backup)

        rollback_root = parent / f'.{data_dir.name}-restore-rollback-{uuid4().hex}'
        rollback_root.mkdir(parents=False, exist_ok=False)
        moved_database = False
        moved_assets = False
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            live_database = data_dir / DATABASE_NAME
            live_assets = data_dir / 'measurement-assets'
            rollback_database = rollback_root / DATABASE_NAME
            rollback_assets = rollback_root / 'measurement-assets'

            if live_database.exists():
                os.replace(live_database, rollback_database)
                moved_database = True
            if live_assets.exists():
                os.replace(live_assets, rollback_assets)
                moved_assets = True

            os.replace(stage_root / DATABASE_NAME, live_database)
            staged_assets = stage_root / 'measurement-assets'
            if staged_assets.exists():
                os.replace(staged_assets, live_assets)
            else:
                live_assets.mkdir(parents=True, exist_ok=True)

            _sqlite_health(live_database)
            _validate_asset_contract(
                data_dir=data_dir,
                database_path=live_database,
                manifest=manifest,
            )
        except Exception as restore_error:
            rollback_error: Exception | None = None
            try:
                _remove_managed_data(data_dir)
                if moved_database and (rollback_root / DATABASE_NAME).exists():
                    os.replace(rollback_root / DATABASE_NAME, data_dir / DATABASE_NAME)
                if moved_assets and (rollback_root / 'measurement-assets').exists():
                    os.replace(rollback_root / 'measurement-assets', data_dir / 'measurement-assets')
            except Exception as exc:
                rollback_error = exc

            if rollback_error is None:
                shutil.rmtree(rollback_root, ignore_errors=True)
                raise

            raise RuntimeError(
                'restore failed and rollback could not be completed; '
                f'original managed data is retained at {rollback_root}: {rollback_error}'
            ) from restore_error
        else:
            shutil.rmtree(rollback_root, ignore_errors=True)
        return manifest, pre_backup
