"""Portable HTDT project bundle — export/import one project (#488).

A ``.htdtproject`` archive is distinct from ``.htdt-backup``: a backup is a
whole-data-root disaster-recovery artifact, while a project bundle carries
exactly the dependency closure of one project/document — revisions,
document-scoped authority rows, and the reachable shared authority
(equipment/directivity/source assets, capture evidence) required to reopen
it elsewhere.

Archive layout::

    manifest.json          # ProjectBundleManifest, manifest_sha256-verified
    db/<table>.jsonl       # serialized rows, one JSON object per row
    assets/<sha256>        # content-addressed managed asset bytes

Export computes the closure by graph walk: every row in a table carrying a
``document_id`` column for the project, then every non-document row
reachable through record identities (UUID/SHA-256 values found anywhere in
exported rows, including inside canonical JSON payloads). Global/shared
authorities are only included when referenced — never the whole shared
library.

Import validates the manifest hash, every asset's SHA-256/size, and every
table's serialized row hash before writing, commits all rows in a single
transaction, and applies the collision contract:

* exact same object already present → reuse (idempotent re-import);
* same record identity with different content → reject;
* ``document_id`` collision with different content → requires explicit
  ``import_as_copy=True``, which remaps the document identity (and any
  record ids that collide) while recording provenance in
  ``htdt_project_imports``.
"""

from __future__ import annotations

from contextlib import closing
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from uuid import uuid4
import zipfile

from pydantic import BaseModel, Field

from . import __version__
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .managed_assets import (
    MANAGED_ASSETS_DIRNAME,
    ManagedAssetError,
    ManagedAssetStore,
)
from .native_row_integrity import verify_native_row_integrity
from .project_library_repository import ProjectLibraryRepository
from .clock import utc_now_iso as _utc_now


BUNDLE_SCHEMA = 'htdt.project-bundle'
BUNDLE_SCHEMA_VERSION = '1.0.0'
BUNDLE_EXTENSION = '.htdtproject'

_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
_DB_NAME_RE = re.compile(r'^[a-z0-9_]+$')

#: Managed-asset registry tables whose rows carry (sha256, relative_path,
#: size_bytes) for files inside the shared MANAGED_ASSETS_DIRNAME store —
#: bundle asset closure covers all of them.
_ASSET_REGISTRY_TABLES = (
    'cad_measurement_assets',
    'cad_quality_calibration_files',
)


def _is_identity_value(value: object) -> bool:
    """Record identities are UUIDs or SHA-256 digests — short names like
    'speaker-fl' or 'active' are never used to draw dependency edges."""
    return isinstance(value, str) and (
        bool(_UUID_RE.match(value)) or bool(_SHA256_RE.match(value))
    )


class ProjectBundleError(ValueError):
    """A project bundle violated the export/import contract."""


class BundleManifestInvalidError(ProjectBundleError):
    """The bundle's manifest failed schema/hash validation."""


class BundleImportConflictError(ProjectBundleError):
    """A record identity exists locally with different content."""


class BundleTableSummary(BaseModel):
    model_config = {'frozen': True}

    table: str
    row_count: int
    rows_sha256: str


class BundleAssetEntry(BaseModel):
    model_config = {'frozen': True}

    sha256: str
    size_bytes: int
    media_type: str


class BundleDependencyEdge(BaseModel):
    """One reachability edge: a row in ``table`` is included because its
    ``key_column`` value was already required by an exported authority."""

    model_config = {'frozen': True}

    table: str
    key_column: str
    key_value: str


class BundleOmission(BaseModel):
    model_config = {'frozen': True}

    subject: str
    reason: str


class BundleRootIdentity(BaseModel):
    model_config = {'frozen': True}

    document_id: str
    project_id: str | None
    display_name: str | None
    head_revision_id: str | None


class ProjectBundleManifest(BaseModel):
    model_config = {'frozen': True, 'serialize_by_alias': True}

    schema_: str = Field(default=BUNDLE_SCHEMA, alias='schema')
    schema_version: str = BUNDLE_SCHEMA_VERSION
    source_htdt_version: str
    exported_at_utc: str
    root: BundleRootIdentity
    tables: tuple[BundleTableSummary, ...]
    assets: tuple[BundleAssetEntry, ...]
    dependencies: tuple[BundleDependencyEdge, ...]
    omissions: tuple[BundleOmission, ...]
    import_events: tuple[str, ...] = ()
    manifest_sha256: str | None = None

    def identity_payload(self) -> dict:
        return self.model_dump(mode='json', exclude={'manifest_sha256'})

    def identity_hash(self) -> str:
        return _canonical_sha256(self.identity_payload())


class ProjectBundleExportResult(BaseModel):
    model_config = {'frozen': True}

    archive_path: str
    document_id: str
    table_count: int
    row_count: int
    asset_count: int
    asset_bytes: int
    manifest_sha256: str


class ProjectBundleImportResult(BaseModel):
    model_config = {'frozen': True}

    document_id: str
    project_id: str | None
    imported_rows: int
    reused_rows: int
    imported_assets: int
    reused_assets: int
    import_mode: str
    manifest_sha256: str


# Categories never carried by a project bundle — recorded as explicit
# omissions so the privacy review is visible in the manifest itself.
_STATIC_OMISSIONS = (
    BundleOmission(
        subject='application preferences',
        reason='user/machine-local configuration is not project data',
    ),
    BundleOmission(
        subject='paired device and REW endpoint state',
        reason='machine-local integration state',
    ),
    BundleOmission(
        subject='diagnostics and logs',
        reason='separate support bundle',
    ),
    BundleOmission(
        subject='UI view state and rebuildable caches',
        reason='regenerable presentation state',
    ),
)

# Columns whose values name revisions and therefore follow a remapped
# revision identity. Any other exported column value that happens to be a
# remapped id is rewritten by the generic value map too.
_MEDIA_TYPES = {
    '.wav': 'audio/wav',
    '.flac': 'audio/flac',
    '.txt': 'text/plain',
    '.csv': 'text/csv',
    '.json': 'application/json',
    '.pdf': 'application/pdf',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
}


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':')).encode(
            'utf-8'
        )
    ).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    return connect_sqlite(path)


def _all_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        row['name']
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )


def _table_columns(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[str, str, bool], ...]:
    """``(name, declared_type, is_pk)`` per column."""
    return tuple(
        (row['name'], (row['type'] or '').upper(), bool(row['pk']))
        for row in connection.execute(f'PRAGMA table_info({table})')
    )


def _pk_columns(
    connection: sqlite3.Connection, table: str
) -> tuple[str, ...]:
    return tuple(
        name
        for name, _type, is_pk in _table_columns(connection, table)
        if is_pk
    )


def _cell_to_json(value: object) -> object:
    if isinstance(value, bytes):
        return {'$blob': base64.b64encode(value).decode('ascii')}
    return value


def _cell_from_json(value: object) -> object:
    if isinstance(value, dict) and set(value) == {'$blob'}:
        return base64.b64decode(value['$blob'])
    return value


def _row_to_json(row: sqlite3.Row) -> dict:
    return {
        'columns': list(row.keys()),
        'values': [_cell_to_json(row[key]) for key in row.keys()],
    }


def _row_json_bytes(row: sqlite3.Row) -> bytes:
    return json.dumps(
        _row_to_json(row), sort_keys=True, separators=(',', ':')
    ).encode('utf-8')


def _row_identity_values(row: sqlite3.Row) -> set[str]:
    """Every record-identity value a row exposes: plain text columns plus
    strings embedded inside ``*_json``/``payload_json`` columns."""

    values: set[str] = set()

    def _collect(node: object) -> None:
        if isinstance(node, str) and _is_identity_value(node):
            values.add(node)
        elif isinstance(node, dict):
            for child in node.values():
                _collect(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                _collect(child)

    for key in row.keys():
        value = row[key]
        if isinstance(value, str):
            if _is_identity_value(value):
                values.add(value)
            elif key.endswith('_json'):
                try:
                    _collect(json.loads(value))
                except (ValueError, TypeError):
                    continue
    return values


def _remap_payload_json(payload: str, value_map: dict[str, str]) -> str:
    """Rewrite record-identity references inside a canonical JSON payload.

    Quoted-string replacement keeps every other byte canonical — the
    payload stays byte-identical to what the owning authority wrote, so
    row-level semantic/plan hash columns remain honest about content.
    """

    for old, new in value_map.items():
        payload = payload.replace(f'"{old}"', f'"{new}"')
    return payload


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_project_bundle(
    repository: SceneRepository,
    document_id: str,
    destination: Path,
    *,
    display_name: str | None = None,
) -> ProjectBundleExportResult:
    """Write ``destination`` as a ``.htdtproject`` for *document_id*.

    Fails closed when a referenced managed asset is missing or corrupt —
    that is an integrity error, never a silent omission.
    """

    destination = Path(destination)
    db_path = Path(repository.path)
    data_dir = db_path.parent

    # Safe-destination guard (#758): the bundle must never overwrite the
    # live database it is exporting from or land inside the managed asset
    # store — ``temp.replace(destination)`` would destroy either silently.
    resolved_destination = destination.resolve()
    if resolved_destination == db_path.resolve():
        raise ProjectBundleError(
            'bundle destination is the live database itself'
        )
    try:
        resolved_destination.relative_to(
            (data_dir / MANAGED_ASSETS_DIRNAME).resolve()
        )
    except ValueError:
        pass
    else:
        raise ProjectBundleError(
            'bundle destination is inside the managed asset store'
        )

    with closing(_connect(db_path)) as connection:
        require_native_tables(connection, 'scene_revisions')
        tables = _all_tables(connection)
        document_tables = {
            table: _table_columns(connection, table)
            for table in tables
            if any(name == 'document_id' for name, _, _ in _table_columns(connection, table))
        }

        exported: dict[str, list[dict]] = {}
        frontier: set[str] = set()

        for table in sorted(document_tables):
            rows = connection.execute(
                f'SELECT * FROM {table} WHERE document_id=? ORDER BY rowid ASC',
                (document_id,),
            ).fetchall()
            if not rows:
                continue
            exported[table] = [_row_to_json(row) for row in rows]
            for row in rows:
                frontier |= _row_identity_values(row)

        if 'scene_revisions' not in exported:
            raise ProjectBundleError(
                f'project has no scene content to export: {document_id}'
            )

        # Dependency closure: walk non-document tables for rows reachable
        # through exported record identities until fixpoint.
        dependencies: list[BundleDependencyEdge] = []
        non_document_tables = [
            table for table in tables if table not in document_tables
        ]
        for _iteration in range(16):
            added = False
            converged = True
            for table in non_document_tables:
                rows = connection.execute(
                    f'SELECT * FROM {table}'
                ).fetchall()
                pk = _pk_columns(connection, table)
                existing = {
                    _canonical_sha256(row)
                    for row in exported.get(table, [])
                }
                for row in rows:
                    row_json = _row_to_json(row)
                    identity = _canonical_sha256(row_json)
                    if identity in existing:
                        continue
                    refs = _row_identity_values(row)
                    hit = refs & frontier
                    if table in document_tables or not hit:
                        continue
                    exported.setdefault(table, []).append(row_json)
                    existing.add(identity)
                    frontier |= refs
                    dependencies.append(
                        BundleDependencyEdge(
                            table=table,
                            key_column=pk[0] if pk else 'rowid',
                            key_value=str(sorted(hit)[0]),
                        )
                    )
                    added = True
                    converged = False
            if not added:
                break
        if not converged:
            # The identity-reachability walk did not reach fixpoint inside
            # the iteration bound — shipping a partial closure would omit
            # rows a dependent authority needs. Fail closed rather than
            # write a silently incomplete bundle.
            raise ProjectBundleError(
                'dependency closure did not reach fixpoint — refusing to '
                'export a possibly-incomplete bundle'
            )

        # Asset closure: every managed-asset registry row pulled in by the
        # walk requires its managed file, hash/size-verified.
        asset_rows = [
            row
            for registry in _ASSET_REGISTRY_TABLES
            for row in exported.get(registry, [])
        ]
        asset_entries: list[BundleAssetEntry] = []
        asset_payloads: dict[str, bytes] = {}
        assets_root = data_dir / MANAGED_ASSETS_DIRNAME
        for row_json in asset_rows:
            record = dict(zip(row_json['columns'], row_json['values']))
            digest = str(record['sha256'])
            relative = str(record['relative_path'])
            asset_path = data_dir / relative
            if not asset_path.is_file():
                raise ProjectBundleError(
                    'referenced managed asset is missing from the data '
                    f'directory (integrity error, not an omission): {digest}'
                )
            raw = asset_path.read_bytes()
            size = int(record['size_bytes'])
            if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
                raise ProjectBundleError(
                    'referenced managed asset failed hash/size verification '
                    f'during export: {digest}'
                )
            filename = str(record.get('filename') or '')
            asset_entries.append(
                BundleAssetEntry(
                    sha256=digest,
                    size_bytes=size,
                    media_type=_MEDIA_TYPES.get(
                        Path(filename).suffix.lower(),
                        'application/octet-stream',
                    ),
                )
            )
            asset_payloads[digest] = raw

        library = ProjectLibraryRepository(repository)
        project_entry = library.get_by_document_id(document_id)
        head_row = connection.execute(
            'SELECT head_revision_id FROM scene_document_heads WHERE document_id=?',
            (document_id,),
        ).fetchone()

        table_summaries: list[BundleTableSummary] = []
        db_payloads: dict[str, bytes] = {}
        row_count = 0
        for table in sorted(exported):
            body = b'\n'.join(
                json.dumps(row, sort_keys=True, separators=(',', ':')).encode(
                    'utf-8'
                )
                for row in exported[table]
            )
            row_count += len(exported[table])
            db_payloads[f'db/{table}.jsonl'] = body
            table_summaries.append(
                BundleTableSummary(
                    table=table,
                    row_count=len(exported[table]),
                    rows_sha256=hashlib.sha256(body).hexdigest(),
                )
            )

        manifest = ProjectBundleManifest(
            source_htdt_version=__version__,
            exported_at_utc=_utc_now(),
            root=BundleRootIdentity(
                document_id=document_id,
                project_id=(
                    project_entry.project_id if project_entry else None
                ),
                display_name=display_name
                or (project_entry.display_name if project_entry else None),
                head_revision_id=(
                    str(head_row['head_revision_id']) if head_row else None
                ),
            ),
            tables=tuple(table_summaries),
            assets=tuple(
                sorted(asset_entries, key=lambda entry: entry.sha256)
            ),
            dependencies=tuple(dependencies),
            omissions=_STATIC_OMISSIONS,
        )
        manifest = manifest.model_copy(
            update={'manifest_sha256': manifest.identity_hash()}
        )

    _write_bundle(destination, manifest, db_payloads, asset_payloads)
    return ProjectBundleExportResult(
        archive_path=str(destination),
        document_id=document_id,
        table_count=len(table_summaries),
        row_count=row_count,
        asset_count=len(asset_entries),
        asset_bytes=sum(entry.size_bytes for entry in asset_entries),
        manifest_sha256=manifest.manifest_sha256,
    )


def _write_bundle(
    destination: Path,
    manifest: ProjectBundleManifest,
    db_payloads: dict[str, bytes],
    asset_payloads: dict[str, bytes],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix='.htdtproject-',
        suffix='.tmp',
    )
    os.close(descriptor)  # zipfile writes by path; a held fd blocks replace on Windows
    temp = Path(temp_name)
    try:
        with zipfile.ZipFile(
            temp, 'w', compression=zipfile.ZIP_DEFLATED
        ) as bundle:
            bundle.writestr(
                'manifest.json',
                json.dumps(
                    manifest.model_dump(mode='json'),
                    indent=2,
                    sort_keys=True,
                ),
            )
            for name, body in sorted(db_payloads.items()):
                bundle.writestr(name, body)
            for digest, raw in sorted(asset_payloads.items()):
                bundle.writestr(f'assets/{digest}', raw)
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


#: Bounds applied to external bundle archives before any member is
#: decompressed (#758): caps on member count, per-member and total expanded
#: size, and the compression ratio a member may claim.
_MAX_BUNDLE_MEMBERS = 4096
_MAX_BUNDLE_MEMBER_BYTES = 1 << 28  # 256 MiB
_MAX_BUNDLE_EXPANDED_BYTES = 1 << 30  # 1 GiB
_MAX_BUNDLE_COMPRESSION_RATIO = 100


def _validate_bundle_members(archive: zipfile.ZipFile) -> set[str]:
    """Traversal-proof member validation: only manifest.json, db/*.jsonl
    and assets/<sha256> entries are read — names are never extracted to the
    filesystem directly, so path escape is impossible by construction.
    Member count, expanded sizes and claimed compression ratios are bounded
    before any decompression runs (``file_size`` is the archive's declared
    uncompressed size; ``compress_size`` the stored size)."""
    names: set[str] = set()
    infos = archive.infolist()
    if len(infos) > _MAX_BUNDLE_MEMBERS:
        raise BundleManifestInvalidError(
            f'bundle has too many members: {len(infos)}'
        )
    expanded_total = 0
    for info in infos:
        name = info.filename
        if info.is_dir():
            raise BundleManifestInvalidError(
                f'unexpected bundle directory member: {name}'
            )
        if info.file_size > _MAX_BUNDLE_MEMBER_BYTES:
            raise BundleManifestInvalidError(
                f'bundle member exceeds the per-member size bound: {name}'
            )
        expanded_total += info.file_size
        if expanded_total > _MAX_BUNDLE_EXPANDED_BYTES:
            raise BundleManifestInvalidError(
                'bundle expanded size exceeds the bound'
            )
        if (
            info.file_size
            > max(info.compress_size, 1024) * _MAX_BUNDLE_COMPRESSION_RATIO
        ):
            raise BundleManifestInvalidError(
                f'bundle member claims an implausible compression ratio: {name}'
            )
        if name == 'manifest.json':
            names.add(name)
        elif name.startswith('db/') and name.endswith('.jsonl'):
            table = name[3:-6]
            if not _DB_NAME_RE.match(table):
                raise BundleManifestInvalidError(
                    f'unsafe bundle member name: {name}'
                )
            names.add(name)
        elif name.startswith('assets/'):
            digest = name[7:]
            if not _SHA256_RE.match(digest):
                raise BundleManifestInvalidError(
                    f'unsafe bundle member name: {name}'
                )
            names.add(name)
        else:
            raise BundleManifestInvalidError(
                f'unexpected bundle member: {name}'
            )
    if 'manifest.json' not in names:
        raise BundleManifestInvalidError('bundle is missing manifest.json')
    return names


def _read_member_bounded(
    archive: zipfile.ZipFile, name: str, bound: int
) -> bytes:
    """Stream one member, capped at ``bound`` bytes of *actual* output.

    ``archive.read(name)`` materializes the member's real decompressed
    size, which a forged header understates — the preflight bounds in
    ``_validate_bundle_members`` only inspect declared values. Reading the
    stream with a cap keeps allocation finite regardless of what the
    header claims.
    """
    try:
        member = archive.open(name, 'r')
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as exc:
        raise BundleManifestInvalidError(
            f'unreadable bundle member: {name}'
        ) from exc
    chunks = []
    remaining = bound + 1
    with member:
        while remaining > 0:
            try:
                chunk = member.read(min(1 << 20, remaining))
            except (
                zipfile.BadZipFile,
                OSError,
                RuntimeError,
                NotImplementedError,
            ) as exc:
                raise BundleManifestInvalidError(
                    f'unreadable bundle member: {name}'
                ) from exc
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    data = b''.join(chunks)
    if len(data) > bound:
        raise BundleManifestInvalidError(
            f'bundle member exceeds the read bound: {name}'
        )
    return data


def import_project_bundle(
    repository: SceneRepository,
    source: Path,
    *,
    import_as_copy: bool = False,
) -> ProjectBundleImportResult:
    """Import a ``.htdtproject`` into *repository*'s native database.

    All row writes happen inside one SQLite transaction — a failed import
    never leaves half a project. Managed assets are content-addressed
    installs before the commit; an aborted import can leave an unreferenced
    asset, which is the established safe failure mode (and reclaimable by
    #501 storage maintenance).
    """

    source = Path(source)
    db_path = Path(repository.path)

    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ProjectBundleError(
            f'not a readable project bundle: {source.name}'
        ) from exc
    with archive:
        members = _validate_bundle_members(archive)
        manifest = ProjectBundleManifest.model_validate(
            json.loads(
                _read_member_bounded(
                    archive, 'manifest.json', _MAX_BUNDLE_MEMBER_BYTES
                )
            )
        )
        if manifest.schema_ != BUNDLE_SCHEMA:
            raise BundleManifestInvalidError(
                f'unsupported bundle schema: {manifest.schema_}'
            )
        if manifest.schema_version != BUNDLE_SCHEMA_VERSION:
            raise BundleManifestInvalidError(
                f'unsupported bundle schema version: '
                f'{manifest.schema_version}'
            )
        if manifest.manifest_sha256 != manifest.identity_hash():
            raise BundleManifestInvalidError(
                'bundle manifest hash mismatch — archive was modified'
            )

        # Validate and load table payloads.
        exported: dict[str, list[dict]] = {}
        for summary in manifest.tables:
            member = f'db/{summary.table}.jsonl'
            if member not in members:
                raise BundleManifestInvalidError(
                    f'manifest lists missing table payload: {member}'
                )
            body = _read_member_bounded(
                archive, member, _MAX_BUNDLE_MEMBER_BYTES
            )
            if hashlib.sha256(body).hexdigest() != summary.rows_sha256:
                raise BundleManifestInvalidError(
                    f'table payload hash mismatch: {summary.table}'
                )
            rows = [json.loads(line) for line in body.splitlines() if line]
            if len(rows) != summary.row_count:
                raise BundleManifestInvalidError(
                    f'table payload row count mismatch: {summary.table}'
                )
            exported[summary.table] = rows
        for member in members:
            if member.startswith('db/') and member != 'manifest.json':
                table = member[3:-6]
                if table not in exported:
                    raise BundleManifestInvalidError(
                        f'bundle contains unmanifested table payload: {member}'
                    )

        # Verify every declared asset's hash and size before any write.
        asset_bytes: dict[str, bytes] = {}
        manifest_assets = {entry.sha256: entry for entry in manifest.assets}
        for member in members:
            if not member.startswith('assets/'):
                continue
            digest = member[7:]
            entry = manifest_assets.get(digest)
            if entry is None:
                raise BundleManifestInvalidError(
                    f'bundle contains unmanifested asset: {digest}'
                )
            raw = _read_member_bounded(
                archive, member, _MAX_BUNDLE_MEMBER_BYTES
            )
            if (
                len(raw) != entry.size_bytes
                or hashlib.sha256(raw).hexdigest() != digest
            ):
                raise BundleManifestInvalidError(
                    f'asset payload hash/size mismatch: {digest}'
                )
            asset_bytes[digest] = raw
        for digest in manifest_assets:
            if digest not in asset_bytes:
                raise BundleManifestInvalidError(
                    f'manifest declares missing asset payload: {digest}'
                )

    # Asset rows must match the manifest entries exactly — the archive's
    # database rows may not lie about what the asset bytes are.
    for registry in _ASSET_REGISTRY_TABLES:
        for row in exported.get(registry, []):
            record = dict(zip(row['columns'], row['values']))
            digest = str(record['sha256'])
            entry = manifest_assets.get(digest)
            if (
                entry is None
                or int(record['size_bytes']) != entry.size_bytes
            ):
                raise BundleManifestInvalidError(
                    f'{registry} row disagrees with the manifest: {digest}'
                )

    # Install managed assets before the database transaction publishes any
    # references — content-addressed installs are idempotent and the worst
    # partial state of a later rollback is an unreferenced orphan asset
    # (reclaimable by #501 storage maintenance), never a committed row
    # pointing at bytes that do not exist.
    store = ManagedAssetStore(db_path.parent / MANAGED_ASSETS_DIRNAME)
    imported_assets = 0
    reused_assets = 0
    for digest, raw in asset_bytes.items():
        try:
            existing = store.read_verified(digest)
        except ManagedAssetError:
            existing = None
        if existing == raw:
            reused_assets += 1
            continue
        store.ensure_installed(digest, raw)
        imported_assets += 1

    source_document_id = manifest.root.document_id
    document_id = source_document_id
    value_map: dict[str, str] = {}
    import_mode = 'straight'

    with closing(_connect(db_path)) as connection, connection:
        require_native_tables(
            connection, 'scene_revisions', 'htdt_project_imports'
        )
        existing_document = connection.execute(
            'SELECT document_id FROM scene_document_heads WHERE document_id=?',
            (source_document_id,),
        ).fetchone()
        if existing_document is not None:
            if not import_as_copy:
                # Idempotent when every exported row is already present
                # verbatim; anything else is a real collision.
                import_mode = 'reimport'
            else:
                document_id = str(uuid4())
                value_map[source_document_id] = document_id
                import_mode = 'copy'

        prepared_rows: list[tuple[str, dict]] = []
        for table in sorted(exported):
            columns_info = {
                name
                for name, _declared, _pk in _table_columns(connection, table)
            }
            for row_json in exported[table]:
                record = {
                    column: _cell_from_json(value)
                    for column, value in zip(
                        row_json['columns'], row_json['values']
                    )
                    if column in columns_info
                }
                prepared_rows.append((table, record))

        ordered = _order_rows_for_insert(prepared_rows)
        inserted = 0
        reused = 0
        inserted_records: list[tuple[str, dict]] = []
        pending = list(ordered)

        for _pass in range(64):
            stalled: list[tuple[str, dict]] = []
            progressed = False
            for table, record in pending:
                record = _apply_value_map(table, record, value_map)
                try:
                    outcome, record = _insert_row(
                        connection,
                        table,
                        record,
                        value_map,
                        import_as_copy,
                    )
                except sqlite3.IntegrityError:
                    stalled.append((table, record))
                    continue
                if outcome == 'inserted':
                    inserted += 1
                    inserted_records.append((table, record))
                else:
                    reused += 1
                progressed = True
            pending = stalled
            if not pending:
                break
            if not progressed:
                unresolved = sorted({t for t, _r in pending})
                raise BundleImportConflictError(
                    'bundle rows could not be imported — unresolved '
                    'dependency order for tables: ' + ', '.join(unresolved)
                )
        if pending:
            raise BundleImportConflictError(
                'bundle row insertion did not reach fixpoint'
            )

        if import_as_copy:
            _backfill_late_remaps(connection, inserted_records, value_map)

        # Register the imported project and record provenance — on this
        # connection, since the write transaction is already held.
        project_row = connection.execute(
            'SELECT project_id FROM htdt_project_documents WHERE document_id=?',
            (document_id,),
        ).fetchone()
        project_id: str | None = None
        if project_row is not None:
            project_id = str(project_row['project_id'])
            if import_as_copy:
                # The bundled project row arrives verbatim — on a copy the
                # manifest's display_name is the operator-chosen clone
                # identity, and copied timestamps belong to the source.
                now = _utc_now()
                connection.execute(
                    'UPDATE htdt_project_documents SET display_name=?, '
                    'created_at_utc=?, updated_at_utc=? WHERE document_id=?',
                    (
                        manifest.root.display_name or document_id,
                        now,
                        now,
                        document_id,
                    ),
                )
        else:
            project_id = str(uuid4())
            now = _utc_now()
            connection.execute(
                '''INSERT INTO htdt_project_documents(
                    project_id, document_id, display_name, description,
                    created_at_utc, updated_at_utc, last_opened_at_utc,
                    archived, cloned_from_project_id, source_revision_id
                ) VALUES (?, ?, ?, NULL, ?, ?, NULL, 0, NULL, ?)''',
                (
                    project_id,
                    document_id,
                    manifest.root.display_name or document_id,
                    now,
                    now,
                    value_map.get(
                        manifest.root.head_revision_id,
                        manifest.root.head_revision_id,
                    ),
                ),
            )
        connection.execute(
            '''INSERT INTO htdt_project_imports(
                import_id, bundle_manifest_sha256, source_document_id,
                imported_document_id, import_mode, imported_at_utc,
                imported_rows, reused_rows
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                str(uuid4()),
                manifest.manifest_sha256,
                source_document_id,
                document_id,
                import_mode,
                _utc_now(),
                inserted,
                reused,
            ),
        )

        # The imported rows must satisfy the same denormalized-column
        # contract as any local write (#313) before the commit publishes.
        verify_native_row_integrity(connection)

    return ProjectBundleImportResult(
        document_id=document_id,
        project_id=project_id,
        imported_rows=inserted,
        reused_rows=reused,
        imported_assets=imported_assets,
        reused_assets=reused_assets,
        import_mode=import_mode,
        manifest_sha256=manifest.manifest_sha256 or '',
    )


#: Copy-mode typed remap: the authorities whose semantic digest is a plain
#: sha256 over the persisted payload text. Identity references inside such
#: a payload may be rewritten as long as the row's digest column is
#: recomputed — and the old->new digest is chained through ``value_map``
#: so scene-bound rows (``scene_content_hash``, ``baseline_content_hash``,
#: …) follow the remapped hash instead of going stale.
#: Any other ``*_json`` rewrite on a hash-bearing row fails closed: those
#: payloads carry self-hashes or authority-level digests that only the
#: owning model can re-derive, and generic string replacement would leave
#: semantically invalid rows that row-integrity cannot always replay.
_COPY_TYPED_REMAP: dict[str, tuple[str, str]] = {
    'scene_revisions': ('payload_json', 'content_hash'),
    'capture_ingestion_runs': ('plan_json', 'plan_sha256'),
}


def _apply_value_map(
    table: str, record: dict, value_map: dict[str, str]
) -> dict:
    if not value_map:
        return record
    out: dict[str, object] = {}
    hash_columns = tuple(
        column
        for column in record
        if 'sha256' in column or column.endswith('_hash')
    )
    typed = _COPY_TYPED_REMAP.get(table)
    for column, value in record.items():
        if not isinstance(value, str):
            out[column] = value
            continue
        if value in value_map:
            out[column] = value_map[value]
            continue
        if not (
            column.endswith('_json')
            and any(key in value for key in value_map)
        ):
            out[column] = value
            continue
        if typed is not None and column == typed[0]:
            remapped = _remap_payload_json(value, value_map)
            out[column] = remapped
            hash_column = typed[1]
            old_hash = record.get(hash_column)
            if isinstance(old_hash, str):
                new_hash = hashlib.sha256(
                    remapped.encode('utf-8')
                ).hexdigest()
                out[hash_column] = new_hash
                value_map[old_hash] = new_hash
            continue
        if hash_columns:
            raise BundleImportConflictError(
                f'{table} copy import cannot remap identities inside '
                f'{column}: the row carries semantic hash columns '
                f'({", ".join(hash_columns)}) that only the owning '
                'authority model can re-derive — copy of this authority '
                'is not supported'
            )
        out[column] = _remap_payload_json(value, value_map)
    return out


def _natural_key_sets(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[str, ...], ...]:
    """Primary-key column set plus each UNIQUE index's column set — every
    column combination that can own a record identity for collision
    semantics (rowid INTEGER PKs are never natural record identity)."""

    columns_info = _table_columns(connection, table)
    key_sets: list[tuple[str, ...]] = []
    pk = tuple(name for name, declared, pk in columns_info
               if pk and declared != 'INTEGER')
    if pk:
        key_sets.append(pk)
    for index in connection.execute(f'PRAGMA index_list({table})').fetchall():
        if not index['unique']:
            continue
        cols = tuple(
            row['name']
            for row in connection.execute(
                f"PRAGMA index_info({index['name']})"
            ).fetchall()
        )
        if cols and cols not in key_sets:
            key_sets.append(cols)
    return tuple(key_sets)


def _backfill_late_remaps(
    connection: sqlite3.Connection,
    inserted_records: list[tuple[str, dict]],
    value_map: dict[str, str],
) -> None:
    """Rewrite rows committed before a later collision extended the remap.

    Copy import discovers remaps incrementally — a natural-key collision on
    row N can add X→Y after rows < N were already inserted carrying X. Once
    the value map is complete, revisit every inserted row and apply any
    remaining mappings in place (#758): the row's own natural identity is
    stable (a committed row can never be the value a later row collided on),
    so it is located by its natural key and only its referencing cells are
    rewritten.
    """

    for table, record in inserted_records:
        remapped = _apply_value_map(table, record, value_map)
        updates = {
            column: value
            for column, value in remapped.items()
            if record.get(column) != value
        }
        if not updates:
            continue
        where: str | None = None
        where_args: tuple[object, ...] = ()
        for keys in _natural_key_sets(connection, table):
            if all(record.get(column) is not None for column in keys):
                where = ' AND '.join(f'{column}=?' for column in keys)
                where_args = tuple(record[column] for column in keys)
                break
        if where is None:
            raise BundleImportConflictError(
                f'{table} copy import cannot backfill late remaps: '
                'no natural key locates the inserted row'
            )
        connection.execute(
            f'UPDATE {table} SET '
            + ', '.join(f'{column}=?' for column in updates)
            + f' WHERE {where}',
            (*updates.values(), *where_args),
        )


def _insert_row(
    connection: sqlite3.Connection,
    table: str,
    record: dict,
    value_map: dict[str, str],
    import_as_copy: bool,
) -> tuple[str, dict]:
    """Insert one row; returns 'inserted' or 'reused'.

    Collision contract (#488): the same record identity (primary key or any
    UNIQUE constraint column set) with identical content is a safe dedup
    reuse; the same identity with different content is rejected — except
    under ``import_as_copy``, where a colliding single-column text identity
    is remapped to a fresh UUID4 (recorded in ``value_map`` so references —
    including those inside payload JSON — follow).
    """

    # A surrogate AUTOINCREMENT seq key is never imported identity: it is a
    # global rowid shared by every document's rows, so importing it would
    # collide with unrelated records. Natural keys (UNIQUE/PK) carry the
    # real record identity for collision semantics.
    for name, declared, is_pk in _table_columns(connection, table):
        if is_pk and declared == 'INTEGER' and name == 'seq':
            record.pop('seq', None)

    for _attempt in range(16):
        conflict_column: str | None = None
        for keys in _natural_key_sets(connection, table):
            if not all(record.get(column) is not None for column in keys):
                continue
            where = ' AND '.join(f'{column}=?' for column in keys)
            existing = connection.execute(
                f'SELECT * FROM {table} WHERE {where}',
                tuple(record[column] for column in keys),
            ).fetchone()
            if existing is None:
                continue
            existing_dict = dict(existing)
            shared = set(existing_dict) & set(record)
            identical = all(
                existing_dict[column] == record[column]
                for column in shared
                if column in record
            ) and len(shared) == len(record) <= len(existing_dict)
            if identical:
                return 'reused', record
            if import_as_copy and len(keys) == 1:
                column = keys[0]
                declared = dict(
                    (name, typ)
                    for name, typ, _pk in _table_columns(connection, table)
                ).get(column, '')
                if declared == 'TEXT' and isinstance(record[column], str):
                    conflict_column = column
                    break
            raise BundleImportConflictError(
                f'{table} record already exists with different content: '
                f'{record.get(keys[0])}'
            )
        if conflict_column is None:
            columns = list(record)
            placeholders = ','.join('?' for _ in columns)
            connection.execute(
                f'INSERT INTO {table} ({",".join(columns)}) '
                f'VALUES ({placeholders})',
                tuple(record[column] for column in columns),
            )
            return 'inserted', record
        old_value = record[conflict_column]
        new_value = str(uuid4())
        value_map[str(old_value)] = new_value
        record[conflict_column] = new_value
        record = _apply_value_map(table, record, value_map)
    raise BundleImportConflictError(
        f'{table} copy import could not allocate fresh identities'
    )


def _order_rows_for_insert(
    prepared: list[tuple[str, dict]],
) -> list[tuple[str, dict]]:
    """Deterministic insert order: scene_revisions parents-before-children,
    then everything else; scene_document_heads / labels / snapshots last —
    foreign keys then resolve inside the retry-pass loop regardless."""

    revision_children: dict[str, list[tuple[str, dict]]] = {}
    revision_roots: list[tuple[str, dict]] = []
    heads: list[tuple[str, dict]] = []
    rest: list[tuple[str, dict]] = []
    for table, record in prepared:
        if table == 'scene_revisions':
            parent = record.get('parent_revision_id')
            if parent:
                revision_children.setdefault(str(parent), []).append(
                    (table, record)
                )
            else:
                revision_roots.append((table, record))
        elif table in {
            'scene_document_heads',
            'scene_revision_labels',
            'scene_recovery_snapshots',
        }:
            heads.append((table, record))
        else:
            rest.append((table, record))

    ordered_revisions: list[tuple[str, dict]] = []
    stack = list(revision_roots)
    while stack:
        entry = stack.pop(0)
        ordered_revisions.append(entry)
        stack[:0] = revision_children.get(
            str(entry[1].get('revision_id')), []
        )
    # Orphaned children (parent filtered out by a partial export) still get
    # inserted; the retry loop handles any residual FK ordering.
    for pending in revision_children.values():
        for entry in pending:
            if entry not in ordered_revisions:
                ordered_revisions.append(entry)

    return ordered_revisions + rest + heads


__all__ = [
    'BUNDLE_EXTENSION',
    'BUNDLE_SCHEMA',
    'BUNDLE_SCHEMA_VERSION',
    'BundleAssetEntry',
    'BundleDependencyEdge',
    'BundleImportConflictError',
    'BundleManifestInvalidError',
    'BundleOmission',
    'BundleRootIdentity',
    'BundleTableSummary',
    'ProjectBundleError',
    'ProjectBundleExportResult',
    'ProjectBundleImportResult',
    'ProjectBundleManifest',
    'export_project_bundle',
    'import_project_bundle',
]
