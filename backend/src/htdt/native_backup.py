from __future__ import annotations

from collections.abc import Callable
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
from .cad_schema import (
    NativeSchemaError,
    connect_sqlite,
    check_native_schema_compatibility,
    ensure_native_schema,
    read_native_schema_version,
)
from .native_row_integrity import verify_native_row_integrity
from .managed_assets import (
    MANAGED_ASSETS_DIRNAME,
    ManagedAssetError,
    canonical_data_path as _canonical_data_path,
    sha256_file as _sha256_file,
    verify_managed_asset,
)
from .persisted_data import (
    auxiliary_archive_path,
    auxiliary_component_for_archive_path,
    backup_included_components,
)
from .limits import (
    MAX_NATIVE_BACKUP_ARCHIVE_BYTES,
    MAX_NATIVE_BACKUP_COMPRESSION_RATIO,
    MAX_NATIVE_BACKUP_EXPANDED_BYTES,
    MAX_NATIVE_BACKUP_MANIFEST_BYTES,
    MAX_NATIVE_BACKUP_MEMBER_BYTES,
    MAX_NATIVE_BACKUP_MEMBERS,
)
from .canonical_json import canonical_json as _canonical_json
from .clock import utc_now_iso as _utc_now


BACKUP_SCHEMA_VERSION = 1
DATABASE_NAME = 'cad-scenes.sqlite3'
MANIFEST_NAME = 'manifest.json'
# Shared managed asset directory; also holds non-measurement content-addressed
# assets (treatment evidence, directivity sources) under the same contract.
MEASUREMENT_ASSETS_NAME = MANAGED_ASSETS_DIRNAME

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


class BackupError(ValueError):
    """A backup archive, manifest, or backup-path validation failure.

    Distinct from a bare ``ValueError`` so the user-facing error mapper can
    name the backup domain instead of reporting generic invalid data.
    """


def _sha256_bytes(payload: bytes) -> str:
    return sha256(payload).hexdigest()


class BackupFileEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1)
    kind: Literal['database', 'measurement_asset', 'auxiliary', 'legacy_archive']
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


class BackupStaleAuthority(BaseModel):
    """One authority row that failed semantic replay at backup time.

    A degraded (``--backup-allow-stale``) archive records every failing
    row here so the manifest honestly declares *which* records could not
    be re-verified under the writing build. Staging tolerates exactly
    this declared set — an undeclared or unexpected failure still refuses.
    """

    model_config = ConfigDict(frozen=True)

    authority: str = Field(min_length=1)
    record_ref: str = Field(min_length=1)
    failure_class: str = Field(min_length=1)
    dependency: str = Field(min_length=1)
    message: str = Field(min_length=1)


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
    # The native DB schema version read from the snapshotted database at
    # backup creation. Optional for pre-#325 archives; when present, staging
    # requires it to equal the actual version stored in the staged database.
    native_schema_version: int | None = Field(default=None, ge=0)
    # Authority rows that failed semantic replay when this archive was
    # written under ``allow_stale`` (round 14). Empty for a fully verified
    # archive — the field folds into the identity hash only when non-empty
    # so legacy manifests keep validating. A manifest carrying entries is
    # a degraded archive: staging tolerates exactly the declared rows and
    # refuses anything else.
    stale_authorities: tuple[BackupStaleAuthority, ...] = ()

    @property
    def degraded(self) -> bool:
        """True when the archive knowingly carries unverified evidence."""
        return bool(self.stale_authorities)

    @model_validator(mode='after')
    def valid_manifest(self) -> 'BackupManifest':
        paths = [entry.path for entry in self.files]
        if len(paths) != len(set(paths)):
            raise BackupError('backup manifest file paths must be unique')
        databases = [entry for entry in self.files if entry.kind == 'database']
        if len(databases) != 1 or databases[0].path != DATABASE_NAME:
            raise BackupError('backup manifest must contain exactly one native database')
        for entry in self.files:
            _safe_archive_path(entry.path)
        if self.manifest_sha256 != _manifest_hash(self.identity_payload()):
            raise BackupError('backup manifest identity hash mismatch')
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
        if self.native_schema_version is not None:
            payload['native_schema_version'] = self.native_schema_version
        if self.stale_authorities:
            payload['stale_authorities'] = [
                entry.model_dump(mode='json')
                for entry in self.stale_authorities
            ]
        return payload


def _manifest_hash(payload: dict[str, Any]) -> str:
    return _sha256_bytes(_canonical_json(payload).encode('utf-8'))


def _safe_archive_path(value: str) -> PurePosixPath:
    if '\\' in value:
        raise BackupError(f'backup path must use POSIX separators: {value}')
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {'', '.', '..'} for part in path.parts):
        raise BackupError(f'unsafe backup archive path: {value}')
    if ':' in path.parts[0]:
        raise BackupError(f'unsafe backup archive path: {value}')
    return path


def _safe_data_path(data_dir: Path, relative_path: str) -> Path:
    archive_path = _safe_archive_path(relative_path)
    target = data_dir.joinpath(*archive_path.parts)
    root = data_dir.resolve()
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise BackupError(f'backup file escapes native data root: {relative_path}') from exc
    return target


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
        raise BackupError(
            f'backup destination overlaps the live native database: {destination}'
        )
    assets_root = _canonical_data_path(data_dir / MEASUREMENT_ASSETS_NAME)
    if destination.is_relative_to(assets_root):
        raise BackupError(
            'backup destination is inside the managed measurement-assets '
            f'directory: {destination}'
        )
    if destination.is_dir():
        raise BackupError(f'backup destination is a directory: {destination}')
    if not source_database.is_file():
        return
    try:
        asset_rows = _asset_rows(source_database)
    except sqlite3.DatabaseError as exc:
        raise BackupError(f'native backup database is invalid: {exc}') from exc
    for _digest, relative_path, _size_bytes in asset_rows:
        asset_path = _canonical_data_path(_safe_data_path(data_dir, relative_path))
        if destination == asset_path or _same_file(destination, asset_path):
            raise BackupError(
                'backup destination overlaps a managed measurement asset: '
                f'{relative_path}'
            )


def _sqlite_health(path: Path) -> None:
    if not path.is_file():
        raise BackupError('native backup database is missing')
    try:
        with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)) as connection:
            integrity = connection.execute('PRAGMA integrity_check').fetchall()
            if integrity != [('ok',)]:
                raise BackupError(f'SQLite integrity check failed: {integrity!r}')
            foreign_keys = connection.execute('PRAGMA foreign_key_check').fetchall()
            if foreign_keys:
                raise BackupError(f'SQLite foreign-key check failed: {foreign_keys!r}')
            # Semantic health (#313): physical/FK checks cannot see a row
            # whose duplicated columns drifted from its canonical payload.
            verify_native_row_integrity(connection)
        try:
            check_native_schema_compatibility(path)
        except NativeSchemaError as exc:
            raise BackupError(f'native backup database schema is incompatible: {exc}') from exc
    except sqlite3.DatabaseError as exc:
        raise BackupError(f'native backup database is invalid: {exc}') from exc


def _assert_staged_database_openable(database_path: Path) -> None:
    """Prove a staged database actually opens under the product's authorities.

    ``_sqlite_health`` is a read-only gate: a staged database can satisfy it
    yet still fail the moment live data handles are reopened — a versioned
    database whose concrete shape breaks a migration statement, an unversioned
    database shadowing a migration target with a view, or a database already
    stamped at the current version whose objects trip repository
    initialization (``ensure_native_schema`` runs no migrations there).
    Failing only then would strand the restore: the destructive swap already
    moved live data out and the archive reported success.

    The staged bytes are pinned by the manifest hash (recovery verifies them
    against ``restored_manifest``), so the proof runs on a disposable clone
    exercised by the same authorities that open live data:
    ``ensure_native_schema`` plus the ``SceneRepository`` open the application
    performs when it rebinds data handles after a restore.
    """
    with tempfile.TemporaryDirectory(
        prefix='.staged-db-probe-',
        dir=database_path.parent,
        ignore_cleanup_errors=True,
    ) as probe_name:
        probe_path = Path(probe_name) / database_path.name
        shutil.copyfile(database_path, probe_path)
        try:
            ensure_native_schema(probe_path)
            # Deferred import keeps the backup authority independent of the
            # repository stack at module load, mirroring the deferred
            # native_backup import inside ensure_native_schema().
            from .cad_repository import SceneRepository

            SceneRepository(probe_path)
        except (NativeSchemaError, sqlite3.DatabaseError) as exc:
            raise BackupError(
                'native backup database cannot be opened by this application: '
                f'{exc}'
            ) from exc


_ASSET_MANIFEST_TABLES: tuple[tuple[str, str], ...] = (
    ('cad_measurement_assets', 'sha256'),
    ('cad_quality_calibration_files', 'sha256'),
    ('htdt_acceptance_evidence', 'sha256'),
)


def _asset_rows(database_path: Path) -> tuple[tuple[str, str, int], ...]:
    """Return every managed asset a backup must carry: (digest, path, size).

    The asset manifest tables are authoritative where they record a
    ``relative_path``; any other content-addressed file retained in the
    managed-assets directory (directivity sources, wave-excitation sources,
    projector-spec sources, equipment evidence, …) still ships by digest —
    a restored database whose evidence files were dropped would fail the
    authority-graph audit.
    """
    by_path: dict[str, tuple[str, str, int]] = {}
    with closing(sqlite3.connect(f'file:{database_path.as_posix()}?mode=ro', uri=True)) as connection:
        for table, sha_column in _ASSET_MANIFEST_TABLES:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if exists is None:
                continue
            for row in connection.execute(
                f'SELECT {sha_column}, relative_path, size_bytes '
                f'FROM {table} ORDER BY {sha_column}'
            ).fetchall():
                relative_path = str(row[1]).replace('\\', '/')
                _safe_archive_path(relative_path)
                by_path[relative_path] = (
                    str(row[0]),
                    relative_path,
                    int(row[2]),
                )
    assets_root = database_path.parent / MEASUREMENT_ASSETS_NAME
    if assets_root.is_dir():
        for candidate in sorted(assets_root.iterdir()):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            digest = candidate.name
            if len(digest) != 64 or any(
                char not in '0123456789abcdef' for char in digest
            ):
                continue
            relative_path = f'{MEASUREMENT_ASSETS_NAME}/{digest}'
            by_path.setdefault(
                relative_path,
                (digest, relative_path, candidate.stat().st_size),
            )
    return tuple(by_path[relative_path] for relative_path in sorted(by_path))


#: Archived legacy-store paths a backup must carry when present — the
#: immutable record of the retired browser authority (#598).
_LEGACY_ARCHIVE_DB = 'htdt.migrated.sqlite3'
_LEGACY_ARCHIVE_ASSETS_DIR = 'htdt.migrated.assets'


def _is_archive_generation(name: str, base: str) -> bool:
    """Match ``base`` itself or the numbered generations ``base.1``,
    ``base.2``, … that the migration's ``_next_available`` fallback
    creates (#759) — every generation is part of the immutable record."""

    if name == base:
        return True
    if not name.startswith(f'{base}.'):
        return False
    return name[len(base) + 1:].isdigit()


def _legacy_archive_members(data_dir: Path) -> tuple[str, ...]:
    """Posix relative paths of every archived legacy store generation.

    A re-migrated store lands as ``htdt.migrated.sqlite3.1`` (and assets
    as ``htdt.migrated.assets.1``) — numbered generations carry the same
    retired-authority record and ride the backup identically (#759).
    """

    members: list[str] = []
    if not data_dir.is_dir():
        return ()
    for candidate in sorted(data_dir.iterdir()):
        name = candidate.name
        if _is_archive_generation(name, _LEGACY_ARCHIVE_DB):
            if candidate.is_file() and not candidate.is_symlink():
                members.append(name)
        elif _is_archive_generation(name, _LEGACY_ARCHIVE_ASSETS_DIR):
            if candidate.is_dir():
                for child in sorted(candidate.rglob('*')):
                    if child.is_file() and not child.is_symlink():
                        members.append(
                            f'{name}/{child.relative_to(candidate).as_posix()}'
                        )
    return tuple(members)


def _validate_asset_contract(
    *,
    data_dir: Path,
    database_path: Path,
    manifest: BackupManifest | None = None,
    is_cancelled: Callable[[], bool] | None = None,
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
        _raise_if_backup_cancelled(is_cancelled)
        _safe_archive_path(relative_path)
        if relative_path in seen_paths:
            raise BackupError(f'duplicate measurement asset path in database: {relative_path}')
        seen_paths.add(relative_path)
        # The per-asset file contract (digest shape, containment, regular
        # file, stored size, streamed SHA-256) is the same core check the
        # N60 runtime measurement authority applies on evidence reads.
        try:
            verify_managed_asset(
                data_dir=data_dir,
                digest=digest,
                relative_path=relative_path,
                size_bytes=size_bytes,
            )
        except ManagedAssetError as exc:
            raise BackupError(str(exc)) from exc
        if manifest is not None:
            entry = manifest_assets.get(relative_path)
            if entry is None:
                raise BackupError(f'measurement asset missing from backup manifest: {relative_path}')
            if entry.sha256 != digest or entry.size_bytes != size_bytes:
                raise BackupError(f'measurement asset manifest mismatch: {relative_path}')
    if manifest is not None and set(manifest_assets) != seen_paths:
        extras = sorted(set(manifest_assets) - seen_paths)
        raise BackupError(f'backup manifest contains unreferenced measurement assets: {extras}')


def _snapshot_database(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file():
        raise FileNotFoundError(f'native database does not exist: {source_path}')
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(connect_sqlite(source_path)) as source, closing(connect_sqlite(destination_path)) as destination:
            source.backup(destination)
            destination.commit()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f'could not create consistent SQLite backup: {exc}') from exc
    _sqlite_health(destination_path)


def _build_manifest(
    snapshot_root: Path,
    database_path: Path,
    *,
    stale_authorities: tuple[BackupStaleAuthority, ...] = (),
    is_cancelled: Callable[[], bool] | None = None,
) -> BackupManifest:
    entries: list[BackupFileEntry] = [
        BackupFileEntry(
            path=DATABASE_NAME,
            kind='database',
            size_bytes=database_path.stat().st_size,
            sha256=_sha256_file(database_path),
        )
    ]
    for digest, relative_path, size_bytes in _asset_rows(database_path):
        _raise_if_backup_cancelled(is_cancelled)
        verify_managed_asset(
            data_dir=snapshot_root,
            digest=digest,
            relative_path=relative_path,
            size_bytes=size_bytes,
        )
        entries.append(BackupFileEntry(
            path=relative_path,
            kind='measurement_asset',
            size_bytes=size_bytes,
            sha256=digest,
        ))
    # Auxiliary registry components (backup_policy=INCLUDE) staged into the
    # snapshot are declared on the manifest so whole-data restore owns them
    # exactly — e.g. commissioning-plans.json survives PC migration (#769).
    for component in backup_included_components():
        _raise_if_backup_cancelled(is_cancelled)
        aux_path = snapshot_root / auxiliary_archive_path(component)
        if not aux_path.is_file():
            continue
        entries.append(BackupFileEntry(
            path=auxiliary_archive_path(component),
            kind='auxiliary',
            size_bytes=aux_path.stat().st_size,
            sha256=_sha256_file(aux_path),
        ))
    # Retired-authority archives (#598/#759): a migrated legacy store is
    # immutable user evidence — losing it in a backup/restore would silently
    # erase the pre-migration record.
    for member in _legacy_archive_members(snapshot_root):
        _raise_if_backup_cancelled(is_cancelled)
        member_path = _safe_data_path(snapshot_root, member)
        entries.append(BackupFileEntry(
            path=member,
            kind='legacy_archive',
            size_bytes=member_path.stat().st_size,
            sha256=_sha256_file(member_path),
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
        'native_schema_version': read_native_schema_version(database_path),
        # identity_payload() only folds stale_authorities in when the set is
        # non-empty — keep the hashed payload identical to what the model
        # re-serializes, or the identity hash will not match.
        **(
            {
                'stale_authorities': [
                    entry.model_dump(mode='json')
                    for entry in stale_authorities
                ]
            }
            if stale_authorities
            else {}
        ),
    }
    return BackupManifest(
        **payload,
        manifest_sha256=_manifest_hash(payload),
    )


def create_backup(
    data_dir: Path,
    destination: Path,
    *,
    allow_stale: bool = False,
    is_cancelled: Callable[[], bool] | None = None,
) -> BackupManifest:
    """Create an atomic native-data backup without copying a live SQLite file directly.

    ``allow_stale`` is the round-14 escape for the post-update trap: when
    persisted authority fails the semantic replay only because a newer
    build re-keyed its seals, a plain ``--backup`` still refuses — but the
    user may explicitly opt into a degraded archive that records every
    failing row in ``manifest.stale_authorities``. Nothing is hidden: the
    declaration joins the manifest identity hash, and restore staging
    tolerates exactly the declared set. Internal safety copies (the
    upgrade recovery snapshot, the automatic scheduler, the pre-restore
    snapshot) pass ``allow_stale=True`` — a flagged archive of the user's
    own data is always better than no export at all.
    """

    recover_interrupted_restore(Path(data_dir))
    return _create_backup(
        data_dir,
        destination,
        allow_stale=allow_stale,
        is_cancelled=is_cancelled,
    )


def _create_backup(
    data_dir: Path,
    destination: Path,
    *,
    allow_stale: bool = False,
    is_cancelled: Callable[[], bool] | None = None,
) -> BackupManifest:
    data_dir = _canonical_data_path(Path(data_dir))
    destination = _canonical_data_path(Path(destination))
    _assert_safe_backup_destination(data_dir, destination)
    source_database = data_dir / DATABASE_NAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    _sweep_backup_staging(destination.parent)

    with tempfile.TemporaryDirectory(prefix='htdt-backup-', dir=destination.parent) as temp_name:
        temp_root = Path(temp_name)
        snapshot_root = temp_root / 'snapshot'
        snapshot_root.mkdir()
        snapshot_database = snapshot_root / DATABASE_NAME
        _snapshot_database(source_database, snapshot_database)

        for _digest, relative_path, _size_bytes in _asset_rows(snapshot_database):
            _raise_if_backup_cancelled(is_cancelled)
            source_asset = _safe_data_path(data_dir, relative_path)
            target_asset = _safe_data_path(snapshot_root, relative_path)
            if source_asset.is_symlink() or not source_asset.is_file():
                raise BackupError(f'measurement asset is missing or not a regular file: {relative_path}')
            target_asset.parent.mkdir(parents=True, exist_ok=True)
            _copy_file_cancellable(source_asset, target_asset, is_cancelled)

        # Registry-declared auxiliary data (project-scoped metadata such as
        # commissioning plans) joins the backup: copy each included
        # component into the snapshot so the manifest hashes it too (#769).
        for component in backup_included_components():
            _raise_if_backup_cancelled(is_cancelled)
            source_aux = data_dir / component.path
            if not source_aux.is_file():
                continue
            target_aux = snapshot_root / auxiliary_archive_path(component)
            target_aux.parent.mkdir(parents=True, exist_ok=True)
            _copy_file_cancellable(source_aux, target_aux, is_cancelled)
        for member in _legacy_archive_members(data_dir):
            _raise_if_backup_cancelled(is_cancelled)
            source_member = _safe_data_path(data_dir, member)
            target_member = _safe_data_path(snapshot_root, member)
            if source_member.is_symlink() or not source_member.is_file():
                raise BackupError(f'legacy archive member is missing or not a regular file: {member}')
            target_member.parent.mkdir(parents=True, exist_ok=True)
            _copy_file_cancellable(source_member, target_member, is_cancelled)

        _validate_asset_contract(
            data_dir=snapshot_root,
            database_path=snapshot_database,
            is_cancelled=is_cancelled,
        )
        # A snapshot carrying stale, missing or non-canonical authority is
        # only packaged as a restorable archive when the caller explicitly
        # accepted a degraded manifest — and then every failing row is
        # recorded in ``stale_authorities`` so staging can re-check the
        # declaration against the staged bytes. Repository construction may
        # migrate the audited file, so the semantic replay runs on a
        # throwaway clone sharing the snapshot's managed-asset directory;
        # the snapshot bytes stay bit-identical to the source generation.
        from .native_authority_audit import (
            AuthorityAuditError,
            audit_native_authority_graph,
        )

        audit_probe = snapshot_root / f'.{DATABASE_NAME}.audit-{uuid4().hex}'
        shutil.copyfile(snapshot_database, audit_probe)
        try:
            audit_report = audit_native_authority_graph(
                audit_probe, is_cancelled=is_cancelled
            )
        finally:
            _remove_path_quiet(audit_probe)
        stale_authorities: tuple[BackupStaleAuthority, ...] = ()
        if not audit_report.ok:
            # Coverage gaps are never degradable: an unclassified table
            # means the audit cannot even claim it inspected the bytes.
            if audit_report.unclassified_tables or not allow_stale:
                raise AuthorityAuditError(audit_report)
            stale_authorities = tuple(
                BackupStaleAuthority(
                    authority=diagnostic.authority,
                    record_ref=diagnostic.record_ref,
                    failure_class=diagnostic.failure_class,
                    dependency=diagnostic.dependency,
                    message=diagnostic.message,
                )
                for diagnostic in audit_report.diagnostics
            )
        manifest = _build_manifest(
            snapshot_root,
            snapshot_database,
            stale_authorities=stale_authorities,
            is_cancelled=is_cancelled,
        )

        archive_temp = temp_root / 'backup.tmp'
        with ZipFile(archive_temp, 'w', compression=ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr(
                MANIFEST_NAME,
                _canonical_json(manifest.model_dump(mode='json')).encode('utf-8'),
            )
            for entry in manifest.files:
                _raise_if_backup_cancelled(is_cancelled)
                _write_zip_member(
                    archive,
                    _safe_data_path(snapshot_root, entry.path),
                    entry.path,
                    is_cancelled,
                )

        validate_backup(archive_temp, is_cancelled=is_cancelled)
        _raise_if_backup_cancelled(is_cancelled)
        os.replace(archive_temp, destination)
    return manifest


def _zip_entries(archive: ZipFile) -> dict[str, ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_NATIVE_BACKUP_MEMBERS:
        raise BackupError(
            f'backup archive has too many members: {len(infos)} '
            f'(limit={MAX_NATIVE_BACKUP_MEMBERS})'
        )
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise BackupError('backup archive contains duplicate member names')

    expanded_total = 0
    entries: dict[str, ZipInfo] = {}
    for info in infos:
        _safe_archive_path(info.filename)
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise BackupError(f'backup archive contains a symlink: {info.filename}')
        if info.is_dir():
            raise BackupError(f'backup archive contains an unexpected directory entry: {info.filename}')
        if info.file_size > MAX_NATIVE_BACKUP_MEMBER_BYTES:
            raise BackupError(
                f'backup member is too large: {info.filename} '
                f'({info.file_size} bytes)'
            )
        expanded_total += info.file_size
        if expanded_total > MAX_NATIVE_BACKUP_EXPANDED_BYTES:
            raise BackupError(
                'backup expanded size exceeds limit: '
                f'{expanded_total} > {MAX_NATIVE_BACKUP_EXPANDED_BYTES}'
            )
        if (
            info.file_size >= 1024 * 1024
            and info.file_size / max(1, info.compress_size)
            > MAX_NATIVE_BACKUP_COMPRESSION_RATIO
        ):
            raise BackupError(
                f'backup member compression ratio is excessive: {info.filename}'
            )
        entries[info.filename] = info
    return entries


def _read_manifest(archive: ZipFile, entries: dict[str, ZipInfo]) -> BackupManifest:
    info = entries.get(MANIFEST_NAME)
    if info is None:
        raise BackupError('backup manifest is missing')
    if info.file_size > MAX_NATIVE_BACKUP_MANIFEST_BYTES:
        raise BackupError('backup manifest exceeds size limit')
    try:
        with archive.open(info, 'r') as source:
            payload = source.read(MAX_NATIVE_BACKUP_MANIFEST_BYTES + 1)
        if len(payload) > MAX_NATIVE_BACKUP_MANIFEST_BYTES:
            raise BackupError('backup manifest exceeds size limit')
        return BackupManifest.model_validate_json(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BackupError(f'backup manifest is invalid: {exc}') from exc


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
                raise BackupError(f'backup member decoded size mismatch: {entry.path}')
            digest.update(chunk)
            output.write(chunk)
    if written != entry.size_bytes:
        raise BackupError(f'backup member decoded size mismatch: {entry.path}')
    if digest.hexdigest() != entry.sha256:
        raise BackupError(f'backup member SHA-256 mismatch: {entry.path}')


def _stage_backup(
    backup_path: Path,
    stage_root: Path,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[BackupManifest, int]:
    if backup_path.stat().st_size > MAX_NATIVE_BACKUP_ARCHIVE_BYTES:
        raise BackupError(
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
                raise BackupError(
                    f'backup archive members do not match manifest: '
                    f'missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}'
                )

            for entry in manifest.files:
                _raise_if_backup_cancelled(is_cancelled)
                info = entries[entry.path]
                if info.file_size != entry.size_bytes:
                    raise BackupError(f'backup member size mismatch: {entry.path}')
                target = _safe_data_path(stage_root, entry.path)
                _extract_verified_member(archive, info, entry, target)
    except BadZipFile as exc:
        raise BackupError(f'backup archive is not a valid ZIP container: {exc}') from exc

    database_path = stage_root / DATABASE_NAME
    _sqlite_health(database_path)
    _assert_staged_database_openable(database_path)
    # The native DB schema version is derived from the staged database
    # itself, never trusted from the manifest: when a manifest carries the
    # field it must equal the staged authority exactly.
    staged_schema_version = read_native_schema_version(database_path)
    if (
        manifest.native_schema_version is not None
        and manifest.native_schema_version != staged_schema_version
    ):
        raise BackupError(
            'backup manifest native schema version does not match the '
            'staged database: manifest='
            f'{manifest.native_schema_version} staged={staged_schema_version}'
        )
    _validate_asset_contract(
        data_dir=stage_root,
        database_path=database_path,
        manifest=manifest,
        is_cancelled=is_cancelled,
    )
    # A structurally valid archive can still carry a semantically corrupt
    # authority graph — stale references, tampered derived records or
    # missing evidence. Replay every persisted authority before the staged
    # bytes are trusted by preview or by the destructive restore swap.
    # Repository construction may migrate the audited database, so the
    # replay runs on a throwaway clone sharing the staged managed-asset
    # directory: the staged bytes stay bit-identical to the manifest hash.
    from .native_authority_audit import (
        AuthorityAuditError,
        audit_native_authority_graph,
    )

    audit_probe = stage_root / f'.{DATABASE_NAME}.audit-{uuid4().hex}'
    shutil.copyfile(database_path, audit_probe)
    try:
        audit_report = audit_native_authority_graph(
            audit_probe, is_cancelled=is_cancelled
        )
    finally:
        _remove_path_quiet(audit_probe)
    if not audit_report.ok:
        # A degraded archive may stage only when every failure the audit
        # now reports is one the manifest declared — undeclared or
        # differently failing rows still refuse.
        declared = {
            (entry.authority, entry.record_ref, entry.failure_class)
            for entry in manifest.stale_authorities
        }
        actual = {
            (
                diagnostic.authority,
                diagnostic.record_ref,
                diagnostic.failure_class,
            )
            for diagnostic in audit_report.diagnostics
        }
        # Subset, not equality: a row the writer declared but this build's
        # audit re-verifies cleanly is benign; only undeclared failures
        # refuse.
        if audit_report.unclassified_tables or not actual <= declared:
            raise AuthorityAuditError(audit_report)
    return manifest, staged_schema_version


def validate_backup(
    backup_path: Path,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> BackupManifest:
    """Fully validate an archive, including SQLite integrity and raw-asset hashes."""

    manifest, _staged_schema_version = inspect_backup(
        backup_path, is_cancelled=is_cancelled
    )
    return manifest


def inspect_backup(
    backup_path: Path,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[BackupManifest, int]:
    """Fully validate an archive and report the staged DB's native schema.

    The returned integer is the ``native_schema_metadata`` version actually
    stored in the staged database (``0`` for a pre-versioning database), so
    callers can present archive schema and DB schema as distinct values even
    for backups whose manifest predates the ``native_schema_version`` field.
    """

    backup_path = Path(backup_path)
    if not backup_path.is_file():
        raise FileNotFoundError(f'backup archive does not exist: {backup_path}')
    with tempfile.TemporaryDirectory(prefix='htdt-backup-validate-') as temp_name:
        return _stage_backup(
            backup_path, Path(temp_name), is_cancelled=is_cancelled
        )


def _remove_managed_data(data_dir: Path) -> None:
    database = data_dir / DATABASE_NAME
    if database.exists():
        database.unlink()
    assets = data_dir / MEASUREMENT_ASSETS_NAME
    if assets.exists():
        shutil.rmtree(assets)
    for component in backup_included_components():
        auxiliary = data_dir / component.path
        if auxiliary.exists():
            if auxiliary.is_dir():
                shutil.rmtree(auxiliary)
            else:
                auxiliary.unlink()


class RestoreRecoveryError(RuntimeError):
    """An interrupted restore swap could not be resolved to a valid state."""


class BackupCancelledError(RuntimeError):
    """A backup/restore/validation job honored a cooperative cancel request.

    Raised only at checkpoints where nothing durable has been written (or,
    for restore, only before the journal commits the swap); the caller maps
    it to a CANCELLED outcome, never a failure (#REV19/D2).
    """


def _raise_if_backup_cancelled(
    is_cancelled: Callable[[], bool] | None,
) -> None:
    if is_cancelled is not None and is_cancelled():
        raise BackupCancelledError('backup operation cancelled by the caller')


#: Copy/deflate chunk size. Cancellation latency stays inside one chunk so a
#: worker asked to stop lands within the pool shutdown budget even when a
#: single measurement asset is large.
_COPY_CHUNK_BYTES = 4 * 1024 * 1024


def _copy_file_cancellable(
    source: Path,
    target: Path,
    is_cancelled: Callable[[], bool] | None,
) -> None:
    """``shutil.copyfile`` semantics with a cancel poll per chunk."""

    with source.open('rb') as src, target.open('wb') as dst:
        while True:
            _raise_if_backup_cancelled(is_cancelled)
            chunk = src.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            dst.write(chunk)


def _write_zip_member(
    archive: ZipFile,
    source: Path,
    arcname: str,
    is_cancelled: Callable[[], bool] | None,
) -> None:
    """``ZipFile.write`` equivalent streamed in cancellable chunks.

    ``ZipInfo.from_file`` derives the same entry metadata ``write`` would
    (mtime, Unix mode bits, file size); only the streaming loop differs so
    archive bytes stay identical.
    """

    zinfo = ZipInfo.from_file(
        source, arcname, strict_timestamps=archive._strict_timestamps
    )
    zinfo.compress_type = archive.compression
    zinfo._compresslevel = archive.compresslevel
    with source.open('rb') as src, archive.open(zinfo, 'w') as dest:
        while True:
            _raise_if_backup_cancelled(is_cancelled)
            chunk = src.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            dest.write(chunk)


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
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
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


def _swap_legacy_archive_members(
    data_dir: Path,
    stage_root: Path,
    rollback_root: Path,
    manifest: BackupManifest,
) -> None:
    """Land every ``legacy_archive`` manifest member on the live root.

    A live file that differs from the manifest is evacuated (its
    pre-restore bytes are preserved in the rollback tree), then the
    staged member lands. Used by both the normal swap and the
    interrupted-swap completion — a restore that skipped these would
    silently erase the retired-authority record (#759).
    """

    for entry in manifest.files:
        if entry.kind != 'legacy_archive':
            continue
        live_member = _safe_data_path(data_dir, entry.path)
        staged_member = _safe_data_path(stage_root, entry.path)
        if live_member.exists():
            if (
                not live_member.is_file()
                or _sha256_file(live_member) != entry.sha256
            ):
                evacuate_root = (
                    rollback_root / 'legacy-archive' / PurePosixPath(entry.path).parent
                )
                evacuate_root.mkdir(parents=True, exist_ok=True)
                _evacuate_into(live_member, evacuate_root)
        if not live_member.exists():
            if not staged_member.is_file():
                raise RestoreRecoveryError(
                    f'staged legacy archive member is missing: {entry.path}'
                )
            # Record the fresh install: a rollback cannot restore a
            # pre-restore byte stream that never existed, so it removes
            # the member instead — and only members carrying this marker.
            marker = (
                rollback_root
                / 'legacy-archive'
                / '.installed'
                / entry.path
            )
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            live_member.parent.mkdir(parents=True, exist_ok=True)
            _replace_durable(staged_member, live_member)
        if _sha256_file(live_member) != entry.sha256:
            raise RestoreRecoveryError(
                f'live legacy archive member failed verification: {entry.path}'
            )


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

    # Auxiliary components: install every manifest-declared file exactly,
    # and evacuate live auxiliary files the archive does not carry — the
    # restored root must match the backup, not retain stale aux state.
    aux_entries = tuple(
        entry for entry in manifest.files if entry.kind == 'auxiliary'
    )
    aux_paths = {entry.path for entry in aux_entries}
    for entry in aux_entries:
        component = auxiliary_component_for_archive_path(entry.path)
        if component is None:
            raise RestoreRecoveryError(
                f'backup manifest auxiliary path is not a registered '
                f'component: {entry.path}'
            )
        live_aux = data_dir / component.path
        staged_aux = stage_root / entry.path
        if live_aux.exists() and (
            not live_aux.is_file()
            or _sha256_file(live_aux) != entry.sha256
        ):
            _evacuate_into(live_aux, rollback_root)
        if not live_aux.exists():
            if not staged_aux.is_file():
                raise RestoreRecoveryError(
                    f'staged auxiliary data is missing: {entry.path}'
                )
            _replace_durable(staged_aux, live_aux)
    for component in backup_included_components():
        live_aux = data_dir / component.path
        if (
            live_aux.exists()
            and auxiliary_archive_path(component) not in aux_paths
        ):
            _evacuate_into(live_aux, rollback_root)
    _swap_legacy_archive_members(
        data_dir, stage_root, rollback_root, manifest
    )

    _sqlite_health(live_database)
    _validate_asset_contract(
        data_dir=data_dir,
        database_path=live_database,
        manifest=manifest,
    )
    _fsync_directory(data_dir)


def _rollback_legacy_archive_members(data_dir: Path, rollback_root: Path) -> None:
    """Reverse the legacy-archive half of a swap that is being rolled back.

    The rollback mirror records exactly what the swap touched: evacuated
    pre-restore bytes return to their canonical slots (the post-swap
    generation parks under ``.superseded`` so nothing is destroyed) and
    members installed fresh — marked under ``.installed`` — are removed
    again (#759). Live members the swap never engaged (identical content,
    or generations absent from the manifest) stay untouched: parking
    them would erase pre-restore authority wholesale.
    """

    legacy_rollback = rollback_root / 'legacy-archive'
    if not legacy_rollback.is_dir():
        return
    # Fresh installs are removed first: a member the swap evacuated and
    # then re-landed carries both a marker and a parked copy, and the
    # parked pre-restore bytes must win over the removal.
    installed_root = legacy_rollback / '.installed'
    if installed_root.is_dir():
        for marker in sorted(installed_root.rglob('*')):
            if marker.is_dir():
                continue
            relative = marker.relative_to(installed_root)
            live_member = data_dir.joinpath(*relative.parts)
            _remove_path_quiet(live_member)
            parent = live_member.parent
            while (
                parent != data_dir
                and parent.is_dir()
                and not any(parent.iterdir())
            ):
                _remove_path_quiet(parent)
                parent = parent.parent
    for parked in sorted(legacy_rollback.rglob('*')):
        relative_parts = parked.relative_to(legacy_rollback).parts
        if (
            parked.is_dir()
            or '.superseded' in relative_parts
            or relative_parts[0] == '.installed'
        ):
            continue
        live_member = data_dir.joinpath(*relative_parts)
        if live_member.exists():
            evacuate_root = parked.parent / '.superseded'
            evacuate_root.mkdir(parents=True, exist_ok=True)
            _evacuate_into(live_member, evacuate_root)
        live_member.parent.mkdir(parents=True, exist_ok=True)
        _replace_durable(parked, live_member)


def _rollback_restore_swap(
    data_dir: Path,
    rollback_root: Path,
    *,
    pre_restore_live: dict[str, Any] | None = None,
    restored_database_sha256: str | None = None,
) -> None:
    """Restore the pre-swap live generation preserved in the rollback dir.

    ``pre_restore_live`` is the journal's record of which live objects the
    interrupted swap evacuated (``None`` = unknown, e.g. an orphan or a
    journal from an older build — fall back to the strictest reading). A
    crash before the first evacuation leaves the canonical rollback slot
    free, and a post-crash occupant may legitimately claim it — only the
    journal can tell "pre-restore truth was empty" from "pre-restore
    database is missing". Without that record the recovery either adopts
    a foreign database as the pre-restore generation or bricks the launch
    on an honestly-empty live root.

    ``restored_database_sha256`` is the interrupted swap's own staged
    database digest: when the journal says the pre-restore database was
    absent, the canonical slot can cycle that very file back into live —
    it is the payload the swap failed to commit, not recovered state, so
    it is parked again rather than blessed (REV53-PASS4).
    """
    live_database = data_dir / DATABASE_NAME
    live_assets = data_dir / MEASUREMENT_ASSETS_NAME
    rollback_database = rollback_root / DATABASE_NAME
    rollback_assets = rollback_root / MEASUREMENT_ASSETS_NAME
    expected_database = (
        True
        if pre_restore_live is None
        else bool(pre_restore_live.get('database', True))
    )
    expected_assets = (
        True
        if pre_restore_live is None
        else bool(pre_restore_live.get('measurement_assets', True))
    )

    data_dir.mkdir(parents=True, exist_ok=True)

    # Whatever the interrupted swap left live is not the pre-restore
    # generation; park it inside the rollback dir so nothing is destroyed.
    if live_database.exists():
        _evacuate_into(live_database, rollback_root)
    if live_assets.exists():
        _evacuate_into(live_assets, rollback_root)
    # Live auxiliary data is the partially-restored generation — park it in
    # a side directory so it can never claim the canonical pre-restore slot.
    evacuated_live = rollback_root / 'evacuated-live'
    for component in backup_included_components():
        live_aux = data_dir / component.path
        if live_aux.exists():
            evacuated_live.mkdir(exist_ok=True)
            _evacuate_into(live_aux, evacuated_live)
    # Legacy-archive members: evacuated pre-restore bytes come back live
    # and swap-installed members are removed again (#759).
    _rollback_legacy_archive_members(data_dir, rollback_root)
    if rollback_database.exists():
        _replace_durable(rollback_database, live_database)
    # Journaled-absent measurement assets stay parked: whatever occupied
    # the canonical slot was live only after the crash, so adopting it
    # would fabricate pre-restore content the journal says never existed.
    if rollback_assets.exists() and expected_assets:
        _replace_durable(rollback_assets, live_assets)
    for component in backup_included_components():
        live_aux = data_dir / component.path
        rollback_aux = rollback_root / component.path
        if rollback_aux.exists() and not live_aux.exists():
            _replace_durable(rollback_aux, live_aux)
    if not live_database.is_file():
        if expected_database:
            raise RestoreRecoveryError(
                'no restorable database remains live or in the rollback '
                'directory'
            )
    elif (
        not expected_database
        and restored_database_sha256 is not None
        and _sha256_file(live_database) == restored_database_sha256
    ):
        # The canonical slot cycled the interrupted swap's own staged
        # database back into live — the journal says the pre-restore
        # generation was absent, so this file is the payload the swap
        # failed to commit, not recovered data. Park it again.
        _evacuate_into(live_database, rollback_root)
    else:
        try:
            _sqlite_health(live_database)
            _validate_asset_contract(
                data_dir=data_dir,
                database_path=live_database,
            )
        except Exception:
            if expected_database:
                raise
            # A post-crash occupant that cannot serve as the live
            # generation is parked back in the rollback dir (never blessed
            # as recovered state); the honest pre-restore truth was empty.
            _evacuate_into(live_database, rollback_root)
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
        _rollback_restore_swap(
            data_dir,
            rollback_root,
            pre_restore_live=(
                journal.get('pre_restore_live')
                if isinstance(journal.get('pre_restore_live'), dict)
                else None
            ),
            restored_database_sha256=next(
                (
                    item.sha256
                    for item in manifest.files
                    if item.kind == 'database'
                ),
                None,
            ),
        )
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


_CRASH_RESIDUE_MIN_AGE_SECONDS = 60.0


def _sweep_open_residue(data_dir: Path, parent: Path) -> None:
    """Best-effort cleanup of crash residue around the managed root.

    Atomic writers publish by rename, so a ``.<name>.<pid>.tmp`` sibling or
    a ``htdt-restore-stage-*`` staging directory still present at open can
    only be remnant of a process death — the live paths never keep the
    temp name, and journaled restore stages are settled (and consumed) by
    the rollback pass above. Anything fresher than the age floor is left
    for the next pass so an in-flight write is never swept. Failures only
    warn: residue is junk, never a startup blocker.
    """
    cutoff = time.time() - _CRASH_RESIDUE_MIN_AGE_SECONDS
    try:
        for candidate in parent.iterdir():
            if not (
                candidate.is_dir()
                and candidate.name.startswith('htdt-restore-stage-')
            ):
                continue
            try:
                if candidate.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            _LOGGER.warning(
                'removing interrupted restore staging dir %s', candidate
            )
            _discard_dir_quiet(candidate)
    except OSError:
        pass
    for directory in (
        data_dir,
        data_dir / 'launch-intents' / 'incoming',
    ):
        try:
            if not directory.is_dir():
                continue
            for candidate in directory.iterdir():
                if not (
                    candidate.is_file()
                    and candidate.name.startswith('.')
                    and candidate.name.endswith('.tmp')
                ):
                    continue
                try:
                    if candidate.stat().st_mtime > cutoff:
                        continue
                    candidate.unlink()
                    _LOGGER.warning(
                        'removed interrupted-write temp file %s', candidate
                    )
                except OSError:
                    _LOGGER.warning(
                        'could not remove crash temp file %s', candidate
                    )
        except OSError:
            continue


def _sweep_backup_staging(parent: Path) -> None:
    """Remove ``htdt-backup-*`` staging dirs a crash left beside archives.

    Real backup archives are ``*.htdt-backup`` files; a directory with the
    same prefix is always an abandoned snapshot staging tree. Same age
    floor as the open-time sweep so an in-flight backup is untouched.
    """
    cutoff = time.time() - _CRASH_RESIDUE_MIN_AGE_SECONDS
    try:
        for candidate in parent.iterdir():
            if not (
                candidate.is_dir()
                and candidate.name.startswith('htdt-backup-')
            ):
                continue
            try:
                if candidate.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            _LOGGER.warning(
                'removing interrupted backup staging dir %s', candidate
            )
            _discard_dir_quiet(candidate)
    except OSError:
        pass


def recover_interrupted_restore(data_dir: Path) -> list[RestoreRecoveryEvent]:
    """Resolve interrupted managed-data restore swaps for *data_dir*.

    Every ``.<data-dir>-restore-rollback-*`` sibling directory is inspected:
    journaled directories are deterministically completed to the validated
    restored state or rolled back to the preserved pre-restore state, while
    unjournaled remnants are recovered only when live data is invalid. When
    no valid state can be produced a RestoreRecoveryError is raised so the
    caller never opens or seeds a database over ambiguous managed data.
    Orphan staging dirs and atomic-writer temp files left by a process
    death are then swept — they can never read as valid state.
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
    rollback_dirs = [
        candidate
        for candidate in candidates
        if candidate.is_dir() and candidate.name.startswith(prefix)
    ]
    if rollback_dirs:
        # The recovery swaps below replace and unlink sqlite files under
        # the data root; a live pooled read connection on them fails with
        # WinError 32 on Windows (the same class restore_backup guards
        # against). Gated on a real pending swap so callers on the hot
        # open path — every ensure_native_schema — never pay for it.
        # Deferred import, same reason as in restore_backup().
        from .cad_repository import (
            fenced_read_reopens_under,
            release_read_handles_under,
        )

        with fenced_read_reopens_under(data_dir):
            release_read_handles_under(data_dir)
            for candidate in rollback_dirs:
                events.append(_recover_rollback_dir(data_dir, candidate))
    if events:
        _fsync_directory(parent)
    _sweep_open_residue(data_dir, parent)
    return events


def restore_backup(
    data_dir: Path,
    backup_path: Path,
    *,
    pre_restore_backup: Path | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    on_commit_point: Callable[[], None] | None = None,
) -> tuple[BackupManifest, Path | None]:
    """Restore validated managed native data with rollback if the live swap fails.

    ``is_cancelled`` is honored while the archive stages and before the
    rollback journal commits the swap; ``on_commit_point`` fires once the
    durable intent record exists, after which the swap always runs its
    journaled course (success or rollback) — that is the CANCEL_UNTIL_COMMIT
    boundary the activity registry enforces (#REV19/D2).
    """

    # The journaled swaps below (and any interrupted-swap recovery) replace
    # and unlink sqlite files under the data root; a live pooled read
    # connection on them makes that fail with WinError 32 on Windows.
    # Releasing first is safe — the repositories re-open on their next
    # read — and the re-open fence keeps those lazy re-opens from winning
    # the race back open before the swap lands. Deferred import keeps the
    # backup authority independent of the native_backup import inside
    # ensure_native_schema().
    from .cad_repository import (
        fenced_read_reopens_under,
        release_read_handles_under,
    )

    with fenced_read_reopens_under(Path(data_dir)):
        release_read_handles_under(Path(data_dir))

        recover_interrupted_restore(Path(data_dir))
        return _restore_backup(
            data_dir,
            backup_path,
            pre_restore_backup=pre_restore_backup,
            is_cancelled=is_cancelled,
            on_commit_point=on_commit_point,
        )


def _restore_backup(
    data_dir: Path,
    backup_path: Path,
    *,
    pre_restore_backup: Path | None,
    is_cancelled: Callable[[], bool] | None = None,
    on_commit_point: Callable[[], None] | None = None,
) -> tuple[BackupManifest, Path | None]:
    data_dir = Path(data_dir)
    backup_path = Path(backup_path)
    parent = data_dir.parent
    parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix='htdt-restore-stage-', dir=parent) as stage_name:
        stage_root = Path(stage_name)
        manifest, _staged_schema_version = _stage_backup(
            backup_path, stage_root, is_cancelled=is_cancelled
        )

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
            try:
                # The live store may carry stale authority (e.g. a re-keyed
                # build awaiting revalidation) — the safety copy still
                # exports it, flagged in the manifest, rather than leaving
                # the pre-restore generation un-exportable.
                _create_backup(
                    data_dir,
                    pre_backup,
                    allow_stale=True,
                    is_cancelled=is_cancelled,
                )
            except BackupCancelledError:
                # A cancel request must never be swallowed by the
                # failed-safety-snapshot fallback below.
                raise
            except Exception as exc:
                # The live store may itself be corrupt — that is the main
                # reason this restore is running. A failed safety snapshot
                # must not abort the restore: the journaled swap below
                # still evacuates every live byte into the rollback dir,
                # and the raw database is preserved unverified so support
                # retains the failing bytes.
                _LOGGER.warning(
                    'pre-restore backup of live data failed (%s); '
                    'preserving raw copy instead', exc
                )
                forensic = parent / (
                    f'{data_dir.name}-pre-restore-unverified-'
                    f'{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}-'
                    f'{uuid4().hex[:8]}.sqlite3'
                )
                try:
                    shutil.copyfile(existing_database, forensic)
                    pre_backup = forensic
                except OSError:
                    _LOGGER.warning(
                        'raw pre-restore copy also failed', exc_info=True
                    )
                    pre_backup = None

        # Last cheap abort point: the rollback dir + journal only exist
        # once the swap is committed to be recoverable.
        _raise_if_backup_cancelled(is_cancelled)
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
            # Which live objects the swap is about to evacuate — journaled
            # BEFORE the first move so recovery can distinguish "the
            # pre-restore truth was empty" from "the pre-restore database
            # is missing" and never adopts a post-crash occupant as the
            # pre-restore generation (or bricks on an honestly-empty root).
            'pre_restore_live': {
                'database': existing_database.is_file(),
                'measurement_assets': (
                    data_dir / MEASUREMENT_ASSETS_NAME
                ).exists(),
                'auxiliary': [
                    component.path
                    for component in backup_included_components()
                    if (data_dir / component.path).exists()
                ],
            },
            'restored_manifest': manifest.model_dump(mode='json'),
        }
        # The durable intent record lands before any live byte moves, so a
        # crash at any later boundary is discoverable and recoverable.
        _write_restore_journal(rollback_root, journal)
        _fsync_directory(parent)
        # Commit point: the durable intent record exists — the swap now
        # owns its outcome (success or rollback) and cancel requests are
        # refused by the activity registry (#REV19/D2).
        if on_commit_point is not None:
            on_commit_point()

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
            # Auxiliary components: every live auxiliary file is parked at
            # its canonical rollback slot — whether the archive carries it
            # or not — so the restored root matches the backup exactly and
            # never retains stale aux state (#769). Entries not carried by
            # the archive are simply absent from the restored state.
            manifest_aux_paths = {
                entry.path
                for entry in manifest.files
                if entry.kind == 'auxiliary'
            }
            for aux_path in sorted(manifest_aux_paths):
                if auxiliary_component_for_archive_path(aux_path) is None:
                    raise RestoreRecoveryError(
                        'backup manifest auxiliary path is not a registered '
                        f'component: {aux_path}'
                    )
            for component in backup_included_components():
                live_aux = data_dir / component.path
                if live_aux.exists():
                    _replace_durable(live_aux, rollback_root / live_aux.name)
            _journal_phase(rollback_root, journal, 'live_evacuated')

            _replace_durable(stage_root / DATABASE_NAME, live_database)
            staged_assets = stage_root / MEASUREMENT_ASSETS_NAME
            if staged_assets.exists():
                _replace_durable(staged_assets, live_assets)
            else:
                live_assets.mkdir(parents=True, exist_ok=True)
            for aux_entry in (
                entry for entry in manifest.files if entry.kind == 'auxiliary'
            ):
                component = auxiliary_component_for_archive_path(aux_entry.path)
                assert component is not None
                staged_aux = stage_root / aux_entry.path
                if not staged_aux.is_file():
                    raise RestoreRecoveryError(
                        f'staged auxiliary data is missing: {aux_entry.path}'
                    )
                _replace_durable(staged_aux, data_dir / component.path)
            # Retired-authority archives ride the same swap: a restore
            # that skipped them would silently erase the record (#759).
            _swap_legacy_archive_members(
                data_dir, stage_root, rollback_root, manifest
            )
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
                # The legacy archive swap ran inside the try as well; its
                # evacuated pre-restore bytes must come back live or the
                # retired-authority record is silently erased (#759).
                _rollback_legacy_archive_members(data_dir, rollback_root)
                if moved_database and (rollback_root / DATABASE_NAME).exists():
                    _replace_durable(rollback_root / DATABASE_NAME, data_dir / DATABASE_NAME)
                if moved_assets and (rollback_root / MEASUREMENT_ASSETS_NAME).exists():
                    _replace_durable(rollback_root / MEASUREMENT_ASSETS_NAME, data_dir / MEASUREMENT_ASSETS_NAME)
                for component in backup_included_components():
                    parked = rollback_root / component.path
                    if parked.exists():
                        _replace_durable(parked, data_dir / component.path)
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
