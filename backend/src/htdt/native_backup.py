from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import time
from typing import Any, Literal
from uuid import uuid4
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__
from .build_info import get_build_info
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
MEASUREMENT_ASSETS_NAME = 'measurement-assets'

# An in-flight restore keeps a durable journal inside its rollback directory
# (``.<data-dir>-restore-rollback-<id>`` next to the managed data directory).
# The journal is written and fsynced before any live file is moved and its
# ``phase`` is advanced as the swap progresses, so a process or OS crash at
# any swap boundary leaves enough state for the next launch to finish the
# swap deterministically or restore the pre-swap generation.
RESTORE_JOURNAL_NAME = 'restore-journal.json'
RESTORE_JOURNAL_KIND = 'htdt-restore-journal'
RESTORE_JOURNAL_SCHEMA_VERSION = 1
RESTORE_ROLLBACK_SUFFIX = '-restore-rollback-'

_LOGGER = logging.getLogger('htdt.native')


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


class BackupBuildInfo(BaseModel):
    """Provenance of the application build that produced a backup."""

    model_config = ConfigDict(frozen=True)

    display_version: str = Field(min_length=1)
    commit_sha: str | None = None
    build_id: str | None = None
    dirty: bool = False
    source: str = Field(min_length=1)


class BackupManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = BACKUP_SCHEMA_VERSION
    application_version: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    files: tuple[BackupFileEntry, ...] = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    # Optional so schema-1 backups written before build provenance existed
    # still validate; it is folded into the identity hash only when present.
    build: BackupBuildInfo | None = None

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
        payload: dict[str, Any] = {
            'schema_version': self.schema_version,
            'application_version': self.application_version,
            'created_at_utc': self.created_at_utc,
            'files': [entry.model_dump(mode='json') for entry in self.files],
        }
        if self.build is not None:
            payload['build'] = self.build.model_dump(mode='json', exclude_none=True)
        return payload


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
    assets_root = _canonical_data_path(data_dir / MEASUREMENT_ASSETS_NAME)
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
    info = get_build_info()
    build = BackupBuildInfo(
        display_version=info.display_version,
        commit_sha=info.commit_sha,
        build_id=info.build_id,
        dirty=info.dirty,
        source=info.source,
    )
    payload = {
        'schema_version': BACKUP_SCHEMA_VERSION,
        'application_version': __version__,
        'created_at_utc': _utc_now(),
        'files': [entry.model_dump(mode='json') for entry in entries],
        'build': build.model_dump(mode='json', exclude_none=True),
    }
    return BackupManifest(
        **payload,
        manifest_sha256=_manifest_hash(payload),
    )


def create_backup(data_dir: Path, destination: Path) -> BackupManifest:
    """Create an atomic native-data backup without copying a live SQLite file directly."""

    recover_interrupted_restore(Path(data_dir))
    return _create_backup(data_dir, destination)


def _create_backup(data_dir: Path, destination: Path) -> BackupManifest:
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
    assets = data_dir / MEASUREMENT_ASSETS_NAME
    if assets.exists():
        shutil.rmtree(assets)


class RestoreRecoveryError(RuntimeError):
    """An interrupted restore swap could not be resolved to a valid state."""


RestoreRecoveryAction = Literal[
    'completed',
    'rolled_back',
    'restored_from_archive',
    'orphan_removed',
    'orphan_preserved',
    'skipped',
]


@dataclass(frozen=True)
class RestoreRecoveryEvent:
    """How one interrupted managed-data restore was resolved at recovery."""

    action: RestoreRecoveryAction
    data_dir: Path
    rollback_dir: Path
    detail: str


def _fsync_directory(directory: Path) -> None:
    # Making directory entries durable requires a directory fsync, which is
    # only meaningful on POSIX filesystems (same convention as the atomic
    # asset installer in cad_measurement_repository).
    if os.name != 'posix':
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_durable(source: Path, target: Path, *, attempts: int = 50) -> None:
    # Windows can transiently refuse a replace while antivirus or a dying
    # process still holds the destination; retry briefly like the #305
    # content-addressed asset installer instead of failing outright.
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.01)


def _remove_path_quiet(path: Path, *, attempts: int = 50) -> None:
    for attempt in range(attempts):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == attempts - 1:
                return
            time.sleep(0.01)
        except OSError:
            return


def _discard_dir_quiet(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _remove_rollback_artifacts(rollback_root: Path) -> None:
    """Best-effort removal that deletes the journal last.

    If removal itself is interrupted, the remnant still contains the journal
    (or is empty), so the next recovery pass can explain and finish it —
    unlike a data-bearing rollback tree with no marker.
    """
    try:
        children = sorted(rollback_root.iterdir())
    except OSError:
        return
    journal_path = rollback_root / RESTORE_JOURNAL_NAME
    for child in children:
        if child == journal_path:
            continue
        _remove_path_quiet(child)
    _remove_path_quiet(journal_path)
    try:
        rollback_root.rmdir()
    except OSError:
        shutil.rmtree(rollback_root, ignore_errors=True)


def _write_restore_journal(rollback_root: Path, journal: dict[str, Any]) -> Path:
    journal_path = rollback_root / RESTORE_JOURNAL_NAME
    temp_path = rollback_root / f'.{RESTORE_JOURNAL_NAME}.{uuid4().hex}.tmp'
    with temp_path.open('wb') as handle:
        handle.write(_canonical_json(journal).encode('utf-8'))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, journal_path)
    _fsync_directory(rollback_root)
    return journal_path


def _journal_phase(rollback_root: Path, journal: dict[str, Any], phase: str) -> None:
    journal['phase'] = phase
    journal['updated_at_utc'] = _utc_now()
    _write_restore_journal(rollback_root, journal)


def _read_restore_journal(journal_path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(journal_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get('kind') != RESTORE_JOURNAL_KIND:
        return None
    if payload.get('schema_version') != RESTORE_JOURNAL_SCHEMA_VERSION:
        return None
    return payload


def _report_recovery(
    action: RestoreRecoveryAction,
    data_dir: Path,
    rollback_root: Path,
    detail: str,
) -> RestoreRecoveryEvent:
    _LOGGER.warning(
        'interrupted restore recovered: action=%s data_dir=%s rollback=%s detail=%s',
        action,
        data_dir,
        rollback_root,
        detail,
    )
    return RestoreRecoveryEvent(
        action=action,
        data_dir=data_dir,
        rollback_dir=rollback_root,
        detail=detail,
    )


def _evacuate_into(path: Path, rollback_root: Path) -> Path:
    """Move *path* into the rollback directory without overwriting payload.

    The pre-restore copy already held by the rollback directory keeps its
    canonical name; later or foreign occupants are parked under a
    ``.superseded-<n>`` suffix so nothing is silently destroyed.
    """
    target = rollback_root / path.name
    if target.exists():
        suffix = 0
        while (rollback_root / f'{path.name}.superseded-{suffix}').exists():
            suffix += 1
        target = rollback_root / f'{path.name}.superseded-{suffix}'
    _replace_durable(path, target)
    return target


def _live_assets_match(
    data_dir: Path,
    live_assets: Path,
    asset_entries: tuple[BackupFileEntry, ...],
) -> bool:
    """True when live assets are exactly the restored generation.

    Every manifest asset must resolve to a file with the recorded size and
    SHA-256, and the managed assets directory must not carry extra files —
    a mix of generations is not the validated restored state.
    """
    if not live_assets.is_dir():
        return not asset_entries
    expected: set[Path] = set()
    for entry in asset_entries:
        target = _safe_data_path(data_dir, entry.path)
        if target.is_symlink() or not target.is_file():
            return False
        if target.stat().st_size != entry.size_bytes:
            return False
        if _sha256_file(target) != entry.sha256:
            return False
        try:
            target.relative_to(live_assets)
        except ValueError:
            continue
        expected.add(target)
    extras = {
        candidate
        for candidate in live_assets.rglob('*')
        if (candidate.is_file() or candidate.is_symlink()) and candidate not in expected
    }
    return not extras


def _complete_restore_swap(
    data_dir: Path,
    rollback_root: Path,
    stage_root: Path,
    manifest: BackupManifest,
) -> None:
    """Finish an interrupted swap so the validated restored state is live."""
    database_entry = next(
        entry for entry in manifest.files if entry.kind == 'database'
    )
    restored_sha = database_entry.sha256
    asset_entries = tuple(
        entry for entry in manifest.files if entry.kind == 'measurement_asset'
    )

    live_database = data_dir / DATABASE_NAME
    live_assets = data_dir / MEASUREMENT_ASSETS_NAME
    staged_database = stage_root / DATABASE_NAME
    staged_assets = stage_root / MEASUREMENT_ASSETS_NAME

    data_dir.mkdir(parents=True, exist_ok=True)

    if live_database.exists():
        if not live_database.is_file() or _sha256_file(live_database) != restored_sha:
            _evacuate_into(live_database, rollback_root)
    if not live_database.exists():
        if not staged_database.is_file():
            raise RestoreRecoveryError(
                'staged database is missing; cannot complete the swap'
            )
        _replace_durable(staged_database, live_database)

    if live_assets.exists() and (
        not live_assets.is_dir()
        or not _live_assets_match(data_dir, live_assets, asset_entries)
    ):
        _evacuate_into(live_assets, rollback_root)
    if not live_assets.exists():
        if staged_assets.is_dir():
            _replace_durable(staged_assets, live_assets)
        elif asset_entries:
            raise RestoreRecoveryError(
                'staged measurement assets are missing; cannot complete the swap'
            )
        else:
            live_assets.mkdir(parents=True, exist_ok=True)

    _sqlite_health(live_database)
    _validate_asset_contract(
        data_dir=data_dir,
        database_path=live_database,
        manifest=manifest,
    )
    _fsync_directory(data_dir)


def _rollback_restore_swap(data_dir: Path, rollback_root: Path) -> None:
    """Restore the pre-swap live generation preserved in the rollback dir."""
    live_database = data_dir / DATABASE_NAME
    live_assets = data_dir / MEASUREMENT_ASSETS_NAME
    rollback_database = rollback_root / DATABASE_NAME
    rollback_assets = rollback_root / MEASUREMENT_ASSETS_NAME

    data_dir.mkdir(parents=True, exist_ok=True)

    # Whatever the interrupted swap left live is not the pre-restore
    # generation; park it inside the rollback dir so nothing is destroyed.
    if live_database.exists():
        _evacuate_into(live_database, rollback_root)
    if live_assets.exists():
        _evacuate_into(live_assets, rollback_root)
    if rollback_database.exists():
        _replace_durable(rollback_database, live_database)
    if rollback_assets.exists():
        _replace_durable(rollback_assets, live_assets)
    if not live_database.is_file():
        raise RestoreRecoveryError(
            'no restorable database remains live or in the rollback directory'
        )
    _sqlite_health(live_database)
    _validate_asset_contract(
        data_dir=data_dir,
        database_path=live_database,
    )
    _fsync_directory(data_dir)


def _live_state_is_valid(data_dir: Path) -> bool:
    try:
        _sqlite_health(data_dir / DATABASE_NAME)
        _validate_asset_contract(
            data_dir=data_dir,
            database_path=data_dir / DATABASE_NAME,
        )
    except Exception:
        return False
    return True


def _recover_orphan_rollback(data_dir: Path, rollback_root: Path) -> RestoreRecoveryEvent:
    """Resolve a rollback directory that has no readable journal."""
    try:
        payload: list[Path] | None = [
            child
            for child in sorted(rollback_root.iterdir())
            if child.name != RESTORE_JOURNAL_NAME
        ]
    except OSError:
        payload = None
    if _live_state_is_valid(data_dir):
        if payload == []:
            _remove_rollback_artifacts(rollback_root)
            return _report_recovery(
                'orphan_removed',
                data_dir,
                rollback_root,
                'empty rollback remnant removed',
            )
        # Live data is valid, so the swap this remnant belonged to had
        # effectively finished — but without a journal the directory's
        # contents cannot be proven redundant, so keep them for inspection.
        return _report_recovery(
            'orphan_preserved',
            data_dir,
            rollback_root,
            'rollback directory without a readable journal was kept',
        )
    try:
        _rollback_restore_swap(data_dir, rollback_root)
    except Exception as exc:
        raise RestoreRecoveryError(
            'interrupted restore could not be recovered and no fresh database '
            f'was created; rollback data is preserved at {rollback_root}: {exc}'
        ) from exc
    _remove_rollback_artifacts(rollback_root)
    return _report_recovery(
        'rolled_back',
        data_dir,
        rollback_root,
        'pre-restore state recovered from an unjournaled rollback directory',
    )


def _recover_journaled_swap(
    data_dir: Path,
    rollback_root: Path,
    journal: dict[str, Any],
) -> RestoreRecoveryEvent:
    try:
        manifest = BackupManifest.model_validate(journal.get('restored_manifest'))
        stage_root = Path(str(journal.get('stage_dir')))
        recorded_data_dir = _canonical_data_path(Path(str(journal.get('data_dir'))))
        recorded_rollback = _canonical_data_path(Path(str(journal.get('rollback_dir'))))
    except Exception:
        return _recover_orphan_rollback(data_dir, rollback_root)

    if recorded_data_dir != data_dir or recorded_rollback != _canonical_data_path(rollback_root):
        return _report_recovery(
            'skipped',
            data_dir,
            rollback_root,
            'journal identity does not match this data directory; left untouched',
        )

    phase = str(journal.get('phase'))
    pre_backup_raw = journal.get('pre_restore_backup')
    pre_backup = Path(str(pre_backup_raw)) if pre_backup_raw else None
    failures: list[str] = []

    try:
        _complete_restore_swap(data_dir, rollback_root, stage_root, manifest)
    except Exception as exc:
        failures.append(f'complete swap failed: {exc}')
    else:
        _remove_rollback_artifacts(rollback_root)
        _discard_dir_quiet(stage_root)
        return _report_recovery(
            'completed',
            data_dir,
            rollback_root,
            f'validated restored state completed (phase={phase})',
        )

    try:
        _rollback_restore_swap(data_dir, rollback_root)
    except Exception as exc:
        failures.append(f'rollback failed: {exc}')
    else:
        _remove_rollback_artifacts(rollback_root)
        _discard_dir_quiet(stage_root)
        return _report_recovery(
            'rolled_back',
            data_dir,
            rollback_root,
            f'pre-restore state restored (phase={phase})',
        )

    if pre_backup is not None and pre_backup.is_file():
        try:
            validate_backup(pre_backup)
            # Live remnants are neither the restored nor the pre-restore
            # generation; park them inside the rollback directory so the
            # archive restore sees an empty live directory and does not try
            # to snapshot a broken live state.
            live_database = data_dir / DATABASE_NAME
            live_assets = data_dir / MEASUREMENT_ASSETS_NAME
            if live_database.exists():
                _evacuate_into(live_database, rollback_root)
            if live_assets.exists():
                _evacuate_into(live_assets, rollback_root)
            _restore_backup(data_dir, pre_backup, pre_restore_backup=None)
        except Exception as exc:
            failures.append(f'pre-restore archive restore failed: {exc}')
        else:
            _remove_rollback_artifacts(rollback_root)
            _discard_dir_quiet(stage_root)
            return _report_recovery(
                'restored_from_archive',
                data_dir,
                rollback_root,
                f'pre-restore archive {pre_backup} re-applied (phase={phase})',
            )

    raise RestoreRecoveryError(
        'interrupted restore could not be recovered and no fresh database was '
        f'created; rollback data is preserved at {rollback_root}'
        + (f' and the pre-restore archive at {pre_backup}' if pre_backup else '')
        + ('; ' + '; '.join(failures) if failures else '')
    )


def _recover_rollback_dir(data_dir: Path, rollback_root: Path) -> RestoreRecoveryEvent:
    journal = _read_restore_journal(rollback_root / RESTORE_JOURNAL_NAME)
    if journal is None:
        return _recover_orphan_rollback(data_dir, rollback_root)
    return _recover_journaled_swap(data_dir, rollback_root, journal)


def recover_interrupted_restore(data_dir: Path) -> list[RestoreRecoveryEvent]:
    """Resolve interrupted managed-data restore swaps for *data_dir*.

    Every ``.<data-dir>-restore-rollback-*`` sibling directory is inspected:
    journaled directories are deterministically completed to the validated
    restored state or rolled back to the preserved pre-restore state, while
    unjournaled remnants are recovered only when live data is invalid. When
    no valid state can be produced a RestoreRecoveryError is raised so the
    caller never opens or seeds a database over ambiguous managed data.
    """
    data_dir = _canonical_data_path(Path(data_dir))
    parent = data_dir.parent
    events: list[RestoreRecoveryEvent] = []
    if not parent.is_dir():
        return events
    prefix = f'.{data_dir.name}{RESTORE_ROLLBACK_SUFFIX}'
    try:
        candidates = sorted(parent.iterdir())
    except OSError:
        return events
    for candidate in candidates:
        if not candidate.is_dir() or not candidate.name.startswith(prefix):
            continue
        events.append(_recover_rollback_dir(data_dir, candidate))
    if events:
        _fsync_directory(parent)
    return events


def restore_backup(
    data_dir: Path,
    backup_path: Path,
    *,
    pre_restore_backup: Path | None = None,
) -> tuple[BackupManifest, Path | None]:
    """Restore validated managed native data with rollback if the live swap fails."""

    recover_interrupted_restore(Path(data_dir))
    return _restore_backup(data_dir, backup_path, pre_restore_backup=pre_restore_backup)


def _restore_backup(
    data_dir: Path,
    backup_path: Path,
    *,
    pre_restore_backup: Path | None,
) -> tuple[BackupManifest, Path | None]:
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
            _create_backup(data_dir, pre_backup)

        rollback_root = parent / f'.{data_dir.name}{RESTORE_ROLLBACK_SUFFIX}{uuid4().hex}'
        rollback_root.mkdir(parents=False, exist_ok=False)
        journal_time = _utc_now()
        journal: dict[str, Any] = {
            'kind': RESTORE_JOURNAL_KIND,
            'schema_version': RESTORE_JOURNAL_SCHEMA_VERSION,
            'restore_id': rollback_root.name.rsplit(RESTORE_ROLLBACK_SUFFIX, 1)[-1],
            'phase': 'prepared',
            'created_at_utc': journal_time,
            'updated_at_utc': journal_time,
            'data_dir': str(_canonical_data_path(data_dir)),
            'rollback_dir': str(_canonical_data_path(rollback_root)),
            'stage_dir': str(_canonical_data_path(stage_root)),
            'pre_restore_backup': (
                str(_canonical_data_path(pre_backup)) if pre_backup is not None else None
            ),
            'restored_manifest': manifest.model_dump(mode='json'),
        }
        # The durable intent record lands before any live byte moves, so a
        # crash at any later boundary is discoverable and recoverable.
        _write_restore_journal(rollback_root, journal)
        _fsync_directory(parent)

        moved_database = False
        moved_assets = False
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            live_database = data_dir / DATABASE_NAME
            live_assets = data_dir / MEASUREMENT_ASSETS_NAME
            rollback_database = rollback_root / DATABASE_NAME
            rollback_assets = rollback_root / MEASUREMENT_ASSETS_NAME

            if live_database.exists():
                _replace_durable(live_database, rollback_database)
                moved_database = True
            if live_assets.exists():
                _replace_durable(live_assets, rollback_assets)
                moved_assets = True
            _journal_phase(rollback_root, journal, 'live_evacuated')

            _replace_durable(stage_root / DATABASE_NAME, live_database)
            staged_assets = stage_root / MEASUREMENT_ASSETS_NAME
            if staged_assets.exists():
                _replace_durable(staged_assets, live_assets)
            else:
                live_assets.mkdir(parents=True, exist_ok=True)
            _journal_phase(rollback_root, journal, 'restored')

            _sqlite_health(live_database)
            _validate_asset_contract(
                data_dir=data_dir,
                database_path=live_database,
                manifest=manifest,
            )
            _journal_phase(rollback_root, journal, 'validated')
            _fsync_directory(data_dir)
        except Exception as restore_error:
            rollback_error: Exception | None = None
            try:
                _remove_managed_data(data_dir)
                if moved_database and (rollback_root / DATABASE_NAME).exists():
                    _replace_durable(rollback_root / DATABASE_NAME, data_dir / DATABASE_NAME)
                if moved_assets and (rollback_root / MEASUREMENT_ASSETS_NAME).exists():
                    _replace_durable(rollback_root / MEASUREMENT_ASSETS_NAME, data_dir / MEASUREMENT_ASSETS_NAME)
            except Exception as exc:
                rollback_error = exc

            if rollback_error is None:
                _remove_rollback_artifacts(rollback_root)
                raise

            raise RuntimeError(
                'restore failed and rollback could not be completed; '
                f'original managed data is retained at {rollback_root}: {rollback_error}'
            ) from restore_error
        else:
            _remove_rollback_artifacts(rollback_root)
        return manifest, pre_backup
